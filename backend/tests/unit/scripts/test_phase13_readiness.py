"""Phase 13 readiness manifest가 in-scope GAP과 영구 제외 soak를 구분하는지 검증한다."""

from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest


# Standard backend discovery에서도 repository-root scripts namespace를 import할 수 있게 test 경계만 보강한다.
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.phase13_readiness import (  # noqa: E402
    EXCLUDED_CHECKS,
    MANUAL_GAP_CHECKS,
    READINESS_CHECK_TOOL_CANDIDATES,
    ReadinessManifestError,
    build_gap_readiness_manifest,
    load_readiness_manifest,
    main,
    validate_readiness_manifest,
    write_readiness_manifest,
)


FIXED_TIME = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc)


class PhaseThirteenReadinessManifestTests(unittest.TestCase):
    """
    클래스 이름: PhaseThirteenReadinessManifestTests
    기능: tool discovery, in-scope GAP, 영구 제외과 PASS digest를 fail-closed 구분한다.
    작성 날짜: 2026/08/24
    """

    def test_unavailable_tools_and_manual_artifacts_are_explicit_gaps(self) -> None:
        """
        함수 이름: test_unavailable_tools_and_manual_artifacts_are_explicit_gaps()
        기능: tool과 THIRD_PARTY/SBOM 근거는 GAP, 영구 제외 soak는 EXCLUDED로 분리하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # Tool 미탐지와 수동 artifact 미제공 상태의 canonical GAP manifest를 만든다.
        manifest = build_gap_readiness_manifest(
            generated_at=FIXED_TIME,
            tool_finder=lambda _tool_name: None,
        )
        check_by_id = {  # Check별 reason과 evidence 상태를 직접 조회한다.
            check["check_id"]: check for check in manifest["checks"]
        }

        # 실행 가능한 GAP과 영구 scope 제외가 서로 다른 status로 유지되는지 검사한다.
        self.assertEqual(manifest["overall_status"], "GAP")
        for check_id in READINESS_CHECK_TOOL_CANDIDATES:
            self.assertEqual(check_by_id[check_id]["reason"], "TOOL_UNAVAILABLE")
            self.assertIsNone(check_by_id[check_id]["evidence_sha256"])
        for check_id in MANUAL_GAP_CHECKS:
            self.assertEqual(
                check_by_id[check_id]["reason"],
                "MANUAL_REVIEW_REQUIRED",
            )
        for check_id, exclusion_reason in EXCLUDED_CHECKS.items():
            self.assertEqual(check_by_id[check_id]["status"], "EXCLUDED")
            self.assertEqual(check_by_id[check_id]["reason"], exclusion_reason)
            self.assertIsNone(check_by_id[check_id]["evidence_sha256"])
        self.assertEqual(
            set(validate_readiness_manifest(manifest)),
            set(READINESS_CHECK_TOOL_CANDIDATES) | set(MANUAL_GAP_CHECKS),
        )

    def test_project_license_decision_is_not_reported_as_unresolved(self) -> None:
        """
        함수 이름: test_project_license_decision_is_not_reported_as_unresolved()
        기능: 완료된 private project 정책 대신 남은 third-party 검토만 manual GAP인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # Project policy는 supply byte-binding이 맡고 generic readiness에는 제3자 의무만 남긴다.
        self.assertNotIn("license_policy_review", MANUAL_GAP_CHECKS)
        self.assertIn("third_party_license_review", MANUAL_GAP_CHECKS)

    def test_permanent_soak_exclusion_cannot_be_rewritten_as_pass(self) -> None:
        """
        함수 이름: test_permanent_soak_exclusion_cannot_be_rewritten_as_pass()
        기능: 사용자가 영구 제외한 soak를 실행 증거 없이 PASS로 위장하지 못하게 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # Canonical generator가 만든 scope decision을 기준으로 status 변조를 시도한다.
        manifest = build_gap_readiness_manifest(
            generated_at=FIXED_TIME,
            tool_finder=lambda _tool_name: None,
        )

        # 영구 제외 soak 항목만 골라 임의 digest의 PASS 기록으로 바꾴본다.
        soak_check = next(
            check
            for check in manifest["checks"]
            if check["check_id"] == "phase13_soak_report"
        )
        soak_check["status"] = "PASS"
        soak_check["reason"] = None
        soak_check["evidence_sha256"] = "a" * 64

        # Scope 제외 결정은 PASS evidence가 아니므로 status 승격을 fail-closed로 거부한다.
        with self.assertRaisesRegex(ReadinessManifestError, "bound evidence"):
            validate_readiness_manifest(manifest)

    def test_available_tool_is_not_misreported_as_completed_scan(self) -> None:
        """
        함수 이름: test_available_tool_is_not_misreported_as_completed_scan()
        기능: executable 발견만으로 dependency/license 검사가 PASS가 되지 않고 NOT_RUN으로 남는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # 가짜 executable 경로를 반환해 tool discovery 성공만 재현한다.
        manifest = build_gap_readiness_manifest(
            generated_at=FIXED_TIME,
            tool_finder=lambda tool_name: f"/secret/host/path/{tool_name}",
        )
        encoded_manifest = json.dumps(manifest, sort_keys=True)  # 경로 비노출도 함께 확인한다.

        # 발견된 도구가 실제 scan 증거 없이 PASS로 승격되지 않는지 검사한다.
        for check in manifest["checks"]:
            if check["check_id"] in READINESS_CHECK_TOOL_CANDIDATES:
                self.assertEqual(check["status"], "GAP")
                self.assertEqual(check["reason"], "NOT_RUN")
                self.assertIsNotNone(check["tool"])
        self.assertNotIn("/secret/host/path", encoded_manifest)

    def test_written_gap_manifest_loads_but_checker_returns_gap(self) -> None:
        """
        함수 이름: test_written_gap_manifest_loads_but_checker_returns_gap()
        기능: canonical manifest file이 schema-valid여도 unresolved GAP 때문에 readiness exit 1을 반환하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            # Canonical GAP manifest를 임시 경로에 쓰고 같은 schema loader로 다시 읽는다.
            manifest_path = Path(temporary_directory) / "readiness.json"
            manifest = build_gap_readiness_manifest(
                generated_at=FIXED_TIME,
                tool_finder=lambda _tool_name: None,
            )
            write_readiness_manifest(manifest_path, manifest)

            loaded_manifest = load_readiness_manifest(manifest_path)  # 저장 bytes를 재검증한다.
            self.assertEqual(loaded_manifest, manifest)
            self.assertEqual(main(["check", str(manifest_path)]), 1)

    def test_runtime_gate_cannot_promote_unbound_current_gaps(self) -> None:
        """
        함수 이름: test_runtime_gate_cannot_promote_unbound_current_gaps()
        기능: 통합 실행기의 in-memory gate가 tool 존재만으로 manual·artifact GAP을 PASS로 바꾸지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # Evidence-binding schema가 없는 현재 gate의 status와 영구 제외 요약을 함께 캡처한다.
        captured_output = io.StringIO()
        with redirect_stdout(captured_output):
            exit_status = main(["gate"])

        # Gate는 in-scope GAP으로 실패하되 soak를 PASS가 아닌 EXCLUDED로 보고해야 한다.
        self.assertEqual(exit_status, 1)
        self.assertIn(
            "phase13_soak_report=USER_SCOPE_EXCLUSION",
            captured_output.getvalue(),
        )

    def test_digest_only_pass_manifest_is_rejected_without_bound_artifacts(
        self,
    ) -> None:
        """
        함수 이름: test_digest_only_pass_manifest_is_rejected_without_bound_artifacts()
        기능: 실제 artifact path 없이 임의 digest만 넣은 PASS manifest를 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/28
        """
        manifest = build_gap_readiness_manifest(
            generated_at=FIXED_TIME,
            tool_finder=lambda _tool_name: None,
        )
        manifest["overall_status"] = "PASS"
        for check in manifest["checks"]:
            check["status"] = "PASS"
            check["reason"] = None
            check["evidence_sha256"] = "a" * 64

        # 64자리 digest shape만으로는 실제 evidence byte나 경로의 존재를 증명할 수 없다.
        with self.assertRaisesRegex(ReadinessManifestError, "bound evidence"):
            validate_readiness_manifest(manifest)

    def test_duplicate_json_key_is_rejected(self) -> None:
        """
        함수 이름: test_duplicate_json_key_is_rejected()
        기능: duplicate root key가 decoder overwrite로 숨지 않고 manifest load를 중단하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            # 동일 root key를 두 번 기록해 일반 decoder의 last-write-wins 입력을 만든다.
            manifest_path = Path(temporary_directory) / "duplicate.json"
            manifest_path.write_text(
                '{"schema_version":1,"schema_version":1}\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(  # Strict loader는 중복 key를 즉시 거부해야 한다.
                ReadinessManifestError, "duplicate"
            ):
                load_readiness_manifest(manifest_path)


if __name__ == "__main__":
    unittest.main()
