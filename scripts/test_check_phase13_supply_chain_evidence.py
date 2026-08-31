"""Phase 13 공급망 NO_GO evidence의 lockfile·local inventory 결합을 검증한다."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from scripts.check_phase13_supply_chain_evidence import (
    EVIDENCE_PATH,
    EXPECTED_RETAINED_SCAN_INPUTS,
    LOCAL_INVENTORY_PATH,
    PROJECT_MANIFEST_SPECS,
    LOCKFILE_SPECS,
    SupplyChainEvidenceError,
    _read_regular_bytes,
    build_current_state_comparison,
    build_local_supply_chain_inventory,
    main,
    validate_supply_chain_evidence,
)
from scripts.phase13_local_supply_artifacts import (
    HISTORICAL_RELEASE_BINDING_PATH,
    LICENSE_INVENTORY_PATH as DEPENDENCY_LICENSE_INVENTORY_PATH,
    NOTICE_REVIEW_PATH,
    SBOM_PATH,
    build_dependency_license_inventory,
    build_exact_lockfile_sbom,
    build_third_party_notice_review,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class Phase13SupplyChainEvidenceTests(unittest.TestCase):
    """
    클래스 이름: Phase13SupplyChainEvidenceTests
    기능: 현재 lockfile binding, NO_GO 보존과 license inventory drift 차단을 검증한다.
    작성 날짜: 2026/08/29
    """

    def _copy_evidence_fixture(self, fixture_root: Path) -> None:
        """
        함수 이름: _copy_evidence_fixture()
        기능: 실제 공급망 evidence와 모든 bound input을 임시 repository에 복사한다.
        인자: fixture_root -> 생성할 임시 repository root
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        relative_paths = [
            EVIDENCE_PATH,
            LOCAL_INVENTORY_PATH,
            SBOM_PATH,
            DEPENDENCY_LICENSE_INVENTORY_PATH,
            NOTICE_REVIEW_PATH,
            HISTORICAL_RELEASE_BINDING_PATH,
            Path("LICENSE"),
            *(path for path, _ in LOCKFILE_SPECS),
            *(path for path, _ in PROJECT_MANIFEST_SPECS),
        ]

        # 각 파일은 같은 relative path에 byte-copy해 실제 hash binding을 그대로 재현한다.
        for relative_path in relative_paths:
            destination_path = fixture_root / relative_path
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPOSITORY_ROOT / relative_path, destination_path)

    def _rewrite_evidence(
        self,
        fixture_root: Path,
        mutation: Callable[[dict[str, object]], None],
    ) -> None:
        """
        함수 이름: _rewrite_evidence()
        기능: 임시 main evidence JSON에 지정한 test mutation을 적용해 canonical JSON으로 기록한다.
        인자: fixture_root -> 임시 repository root, mutation -> payload 변경 callable
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # Mutation 후에도 실제 evidence와 같은 UTF-8 형식과 trailing newline을 유지한다.
        evidence_path = fixture_root / EVIDENCE_PATH
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
        mutation(payload)
        evidence_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def test_bounded_reader_rejects_symlink_replacement_and_growth(self) -> None:
        """
        함수 이름: test_bounded_reader_rejects_symlink_replacement_and_growth()
        기능: 공급망 reader가 symlink·path 교체·read 중 증가를 fail closed하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_root = Path(temporary_directory)
            target_path = fixture_root / "target.bin"
            symlink_path = fixture_root / "symlink.bin"

            # 정상 regular file을 가리키는 final symlink도 열지 않는다.
            target_path.write_bytes(b"trusted")
            symlink_path.symlink_to(target_path.name)
            with self.assertRaisesRegex(
                SupplyChainEvidenceError,
                "path is invalid",
            ):
                _read_regular_bytes(fixture_root, Path("symlink.bin"), 16)

            # 열기 전부터 max를 초과한 regular file은 bounded read를 시작하지 않는다.
            oversized_path = fixture_root / "oversized.bin"
            oversized_path.write_bytes(b"O" * 17)
            with self.assertRaisesRegex(
                SupplyChainEvidenceError,
                "size is invalid",
            ):
                _read_regular_bytes(fixture_root, Path("oversized.bin"), 16)

            # FIFO leaf는 O_NONBLOCK open 뒤 즉시 regular-file 경계에서 거부되어야 한다.
            fifo_path = fixture_root / "artifact.fifo"
            os.mkfifo(fifo_path)
            with self.assertRaisesRegex(
                SupplyChainEvidenceError,
                "regular file",
            ):
                _read_regular_bytes(fixture_root, Path("artifact.fifo"), 16)

        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_root = Path(temporary_directory)
            artifact_path = fixture_root / "artifact.bin"
            replacement_path = fixture_root / "replacement.bin"
            artifact_path.write_bytes(b"A" * 16)
            replacement_path.write_bytes(b"B" * 16)
            original_os_read = os.read
            replacement_completed = False

            def replace_path_after_read(
                file_descriptor: int,
                maximum_bytes: int,
            ) -> bytes:
                """
                함수 이름: replace_path_after_read()
                기능: 첫 fd read 후 경로를 다른 inode로 교체하는 race를 재현한다.
                인자: file_descriptor -> reader fd, maximum_bytes -> 요청 read 크기
                반환값: 원래 os.read()의 bytes
                작성 날짜: 2026/08/31
                """
                nonlocal replacement_completed
                file_chunk = original_os_read(file_descriptor, maximum_bytes)

                # 열린 fd는 유지하고 repository path만 새 inode로 바꾸어 post-check를 검증한다.
                if not replacement_completed:
                    os.replace(replacement_path, artifact_path)
                    replacement_completed = True
                return file_chunk

            with patch(
                "scripts.check_phase13_supply_chain_evidence.os.read",
                side_effect=replace_path_after_read,
            ), self.assertRaisesRegex(SupplyChainEvidenceError, "changed"):
                _read_regular_bytes(fixture_root, Path("artifact.bin"), 16)

        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_root = Path(temporary_directory)
            artifact_path = fixture_root / "artifact.bin"
            artifact_path.write_bytes(b"C" * 16)
            original_os_read = os.read
            growth_completed = False

            def grow_path_after_read(
                file_descriptor: int,
                maximum_bytes: int,
            ) -> bytes:
                """
                함수 이름: grow_path_after_read()
                기능: 첫 fd read 후 파일을 증가시켜 max+1 bounded 경계를 재현한다.
                인자: file_descriptor -> reader fd, maximum_bytes -> 요청 read 크기
                반환값: 원래 os.read()의 bytes
                작성 날짜: 2026/08/31
                """
                nonlocal growth_completed
                file_chunk = original_os_read(file_descriptor, maximum_bytes)

                # 첫 chunk 후 한 byte를 늘려 reader가 최대로 max+1 byte만 관찰하고 거부하게 한다.
                if not growth_completed:
                    with artifact_path.open("ab") as artifact_file:
                        artifact_file.write(b"G")
                    growth_completed = True
                return file_chunk

            with patch(
                "scripts.check_phase13_supply_chain_evidence.os.read",
                side_effect=grow_path_after_read,
            ), self.assertRaisesRegex(SupplyChainEvidenceError, "changed"):
                _read_regular_bytes(fixture_root, Path("artifact.bin"), 16)

        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_root = Path(temporary_directory)
            parent_path = fixture_root / "parent"
            parent_path.mkdir()
            artifact_path = parent_path / "artifact.bin"
            artifact_path.write_bytes(b"D" * 16)
            replacement_parent = fixture_root / "replacement-parent"
            replacement_parent.mkdir()
            (replacement_parent / "artifact.bin").write_bytes(b"E" * 16)
            original_parent = fixture_root / "original-parent"
            original_os_read = os.read
            parent_replaced = False

            def replace_parent_after_read(
                file_descriptor: int,
                maximum_bytes: int,
            ) -> bytes:
                """
                함수 이름: replace_parent_after_read()
                기능: 첫 leaf read 뒤 parent directory entry를 symlink로 교체한다.
                인자: file_descriptor -> reader fd, maximum_bytes -> 요청 read 크기
                반환값: 원래 os.read()의 bytes
                작성 날짜: 2026/08/31
                """
                nonlocal parent_replaced
                file_chunk = original_os_read(file_descriptor, maximum_bytes)

                # 열린 original parent fd는 유지하되 repository entry만 다른 tree로 바꾼다.
                if not parent_replaced:
                    parent_path.rename(original_parent)
                    parent_path.symlink_to(
                        replacement_parent.name,
                        target_is_directory=True,
                    )
                    parent_replaced = True
                return file_chunk

            with patch(
                "scripts.check_phase13_supply_chain_evidence.os.read",
                side_effect=replace_parent_after_read,
            ), self.assertRaisesRegex(SupplyChainEvidenceError, "path changed"):
                _read_regular_bytes(
                    fixture_root,
                    Path("parent/artifact.bin"),
                    16,
                )

    def test_repository_evidence_is_bound_and_preserves_no_go(self) -> None:
        """
        함수 이름: test_repository_evidence_is_bound_and_preserves_no_go()
        기능: 실제 evidence가 현재 inventory와 868개 historical scan을 분리해 NO_GO로 보존하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # Repository evidence를 전체 검증한 후 모든 NO_GO 집계를 개별적으로 고정한다.
        summary = validate_supply_chain_evidence()

        self.assertEqual(summary.package_count, 868)
        self.assertEqual(summary.retained_scan_package_count, 868)
        self.assertFalse(summary.current_vs_scanned_match)
        self.assertEqual(summary.security_finding_count, 18)
        self.assertEqual(summary.unresolved_license_group_count, 2)
        self.assertEqual(summary.exact_sbom_component_count, 868)
        self.assertEqual(summary.third_party_license_declaration_count, 494)
        self.assertEqual(summary.third_party_noassertion_count, 372)
        self.assertFalse(summary.current_phase13_release_candidate_bound)
        self.assertEqual(summary.overall_status, "NO_GO")

    def test_cli_reports_and_blocks_on_validated_no_go(self) -> None:
        """
        함수 이름: test_cli_reports_and_blocks_on_validated_no_go()
        기능: 정확한 NO_GO evidence도 CLI readiness 성공으로 승격되지 않는지 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        standard_output = StringIO()
        standard_error = StringIO()

        # Valid binding도 미해결 findings를 보존하므로 상태와 exit code를 모두 NO_GO로 유지한다.
        with redirect_stdout(standard_output), redirect_stderr(standard_error):
            exit_code = main([])

        self.assertEqual(exit_code, 1)
        self.assertIn("blocked by retained NO_GO", standard_error.getvalue())
        self.assertIn("current_packages=868", standard_output.getvalue())
        self.assertIn("retained_scan_packages=868", standard_output.getvalue())
        self.assertIn("current_vs_scanned_match=false", standard_output.getvalue())
        self.assertIn("sbom_components=868", standard_output.getvalue())
        self.assertIn("third_party_license_declarations=494", standard_output.getvalue())
        self.assertIn("third_party_noassertion=372", standard_output.getvalue())
        self.assertIn("current_phase13_release_bound=false", standard_output.getvalue())
        self.assertIn("validated_status=NO_GO", standard_output.getvalue())
        self.assertNotIn("validated_status=GO", standard_output.getvalue())

    def test_lockfile_byte_drift_invalidates_local_inventory(self) -> None:
        """
        함수 이름: test_lockfile_byte_drift_invalidates_local_inventory()
        기능: Package graph가 같아 보여도 lockfile byte가 바뀌면 stale evidence를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_root = Path(temporary_directory)
            self._copy_evidence_fixture(fixture_root)
            python_lockfile = fixture_root / LOCKFILE_SPECS[0][0]

            # TOML 의미를 바꾸지 않는 newline도 retained artifact hash와 다르므로 drift다.
            python_lockfile.write_bytes(python_lockfile.read_bytes() + b"\n")

            with self.assertRaisesRegex(SupplyChainEvidenceError, "drifted"):
                validate_supply_chain_evidence(repository_root=fixture_root)

    def test_local_inventory_artifact_hash_mismatch_fails_closed(self) -> None:
        """
        함수 이름: test_local_inventory_artifact_hash_mismatch_fails_closed()
        기능: Main evidence가 가리킨 local inventory bytes가 변조되면 semantic parse 전에 차단한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_root = Path(temporary_directory)
            self._copy_evidence_fixture(fixture_root)
            inventory_path = fixture_root / LOCAL_INVENTORY_PATH

            # JSON 뒤 공백 한 byte도 main evidence가 승인한 artifact와 동일하지 않다.
            inventory_path.write_bytes(inventory_path.read_bytes() + b" ")

            with self.assertRaisesRegex(SupplyChainEvidenceError, "hash mismatch"):
                validate_supply_chain_evidence(repository_root=fixture_root)

    def test_exact_sbom_artifact_hash_mismatch_fails_closed(self) -> None:
        """
        함수 이름: test_exact_sbom_artifact_hash_mismatch_fails_closed()
        기능: Main evidence와 SBOM 원문의 한 byte 불일치를 재생성 전에 차단한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_root = Path(temporary_directory)
            self._copy_evidence_fixture(fixture_root)
            sbom_path = fixture_root / SBOM_PATH

            # JSON 끝 whitespace도 main evidence에 기록된 raw artifact hash와 다르다.
            sbom_path.write_bytes(sbom_path.read_bytes() + b" ")

            with self.assertRaisesRegex(SupplyChainEvidenceError, "hash mismatch"):
                validate_supply_chain_evidence(repository_root=fixture_root)

    def test_historical_release_binding_cannot_be_promoted_to_phase13(self) -> None:
        """
        함수 이름: test_historical_release_binding_cannot_be_promoted_to_phase13()
        기능: Historical artifact JSON과 main hash를 함께 바꿔도 current Phase 13 승격을 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_root = Path(temporary_directory)
            self._copy_evidence_fixture(fixture_root)
            binding_path = fixture_root / HISTORICAL_RELEASE_BINDING_PATH
            binding = json.loads(binding_path.read_text(encoding="utf-8"))
            binding["current_phase13_release_candidate"] = True
            binding_bytes = (
                json.dumps(binding, ensure_ascii=False, indent=2) + "\n"
            ).encode("utf-8")
            binding_path.write_bytes(binding_bytes)

            # Main hash도 같이 바꾸어 byte binding 단계를 지나 semantic 경계를 검증한다.
            def bind_promoted_release(payload: dict[str, object]) -> None:
                """
                함수 이름: bind_promoted_release()
                기능: Test main evidence를 변조된 historical binding hash에 결합한다.
                인자: payload -> 수정할 evidence object
                반환값: 없음
                작성 날짜: 2026/08/29
                """
                # Historical reference의 digest만 변조한 binding bytes에 맞춰 semantic 경계까지 도달한다.
                artifact_binding = payload["artifact_binding"]
                assert isinstance(artifact_binding, dict)
                historical_reference = artifact_binding[
                    "historical_release_artifact"
                ]
                assert isinstance(historical_reference, dict)
                historical_reference["sha256"] = hashlib.sha256(
                    binding_bytes
                ).hexdigest()

            self._rewrite_evidence(fixture_root, bind_promoted_release)

            with self.assertRaisesRegex(
                SupplyChainEvidenceError,
                "local supply artifact validation",
            ):
                validate_supply_chain_evidence(repository_root=fixture_root)

    def test_refreshed_current_inventory_does_not_rebind_retained_scan(self) -> None:
        """
        함수 이름: test_refreshed_current_inventory_does_not_rebind_retained_scan()
        기능: 현재 lock inventory를 새로 결합해도 historical scan hash와 count가 바뀌지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_root = Path(temporary_directory)
            self._copy_evidence_fixture(fixture_root)
            python_lockfile = fixture_root / LOCKFILE_SPECS[0][0]
            evidence_path = fixture_root / EVIDENCE_PATH
            historical_inputs = json.loads(
                evidence_path.read_text(encoding="utf-8")
            )["inputs"]

            # Package graph은 같지만 byte hash가 다른 현재 lock 상태를 만든다.
            python_lockfile.write_bytes(python_lockfile.read_bytes() + b"\n")
            current_inventory = build_local_supply_chain_inventory(
                fixture_root,
                observed_date="2026-08-29",
            )
            inventory_bytes = (
                json.dumps(current_inventory, ensure_ascii=False, indent=2) + "\n"
            ).encode("utf-8")
            (fixture_root / LOCAL_INVENTORY_PATH).write_bytes(inventory_bytes)

            # Lockfile-derived SBOM, license inventory와 notice review도 같은 current bytes로 재생성한다.
            refreshed_artifact_bytes = {
                SBOM_PATH: (
                    json.dumps(
                        build_exact_lockfile_sbom(fixture_root),
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n"
                ).encode("utf-8"),
                DEPENDENCY_LICENSE_INVENTORY_PATH: (
                    json.dumps(
                        build_dependency_license_inventory(fixture_root),
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n"
                ).encode("utf-8"),
                NOTICE_REVIEW_PATH: build_third_party_notice_review(
                    fixture_root
                ).encode("utf-8"),
            }
            for artifact_path, artifact_bytes in refreshed_artifact_bytes.items():
                (fixture_root / artifact_path).write_bytes(artifact_bytes)

            # Current artifact reference와 comparison만 새로 결합하고 scan input은 보존한다.
            def bind_current_inventory(payload: dict[str, object]) -> None:
                """
                함수 이름: bind_current_inventory()
                기능: Test evidence의 current inventory hash와 비교 object만 갱신한다.
                인자: payload -> 수정할 test evidence object
                반환값: 없음
                작성 날짜: 2026/08/29
                """
                # Current artifact hash와 comparison만 갱신하고 retained scan 영역은 변경하지 않는다.
                artifact_binding = payload["artifact_binding"]
                assert isinstance(artifact_binding, dict)
                local_reference = artifact_binding["local_inventory"]
                assert isinstance(local_reference, dict)
                local_reference["sha256"] = hashlib.sha256(
                    inventory_bytes
                ).hexdigest()
                for artifact_key, artifact_path in (
                    ("exact_lockfile_sbom", SBOM_PATH),
                    (
                        "dependency_license_inventory",
                        DEPENDENCY_LICENSE_INVENTORY_PATH,
                    ),
                    ("third_party_notice_review", NOTICE_REVIEW_PATH),
                ):
                    artifact_reference = artifact_binding[artifact_key]
                    assert isinstance(artifact_reference, dict)
                    artifact_reference["sha256"] = hashlib.sha256(
                        refreshed_artifact_bytes[artifact_path]
                    ).hexdigest()
                payload["current_state_comparison"] = (
                    build_current_state_comparison(current_inventory)
                )

            self._rewrite_evidence(fixture_root, bind_current_inventory)
            summary = validate_supply_chain_evidence(repository_root=fixture_root)
            refreshed_evidence = json.loads(
                evidence_path.read_text(encoding="utf-8")
            )

            self.assertEqual(
                refreshed_evidence["inputs"],
                historical_inputs,
            )
            self.assertEqual(
                historical_inputs,
                [
                    {"path": path, "sha256": sha256}
                    for path, sha256 in EXPECTED_RETAINED_SCAN_INPUTS
                ],
            )
            self.assertFalse(summary.current_vs_scanned_match)
            self.assertFalse(
                refreshed_evidence["current_state_comparison"][
                    "lockfile_inputs_match"
                ]
            )

    def test_project_license_manifest_drift_requires_inventory_refresh(self) -> None:
        """
        함수 이름: test_project_license_manifest_drift_requires_inventory_refresh()
        기능: Local project에 license가 추가돼도 stale 미선언 inventory를 그대로 통과시키지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_root = Path(temporary_directory)
            self._copy_evidence_fixture(fixture_root)
            ui_manifest_path = fixture_root / "UI" / "package.json"
            ui_manifest = json.loads(ui_manifest_path.read_text(encoding="utf-8"))

            # 승인된 private policy를 다른 expression으로 바꾸면 artifact drift로 거부한다.
            ui_manifest["license"] = "UNAPPROVED-TEST-VALUE"
            ui_manifest_path.write_text(
                json.dumps(ui_manifest, indent=2) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(SupplyChainEvidenceError, "drifted"):
                validate_supply_chain_evidence(repository_root=fixture_root)

    def test_go_status_and_release_exception_are_rejected(self) -> None:
        """
        함수 이름: test_go_status_and_release_exception_are_rejected()
        기능: 미해결 finding 상태에서 GO 또는 승인되지 않은 release exception 주입을 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # GO 승격과 release exception을 각각 독립된 변조로 적용한다.
        for mutation in (
            lambda payload: payload.__setitem__("overall_status", "GO"),
            lambda payload: payload["security_findings"]["triage"].__setitem__(
                "release_exception_approved",
                True,
            ),
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                fixture_root = Path(directory)
                self._copy_evidence_fixture(fixture_root)
                self._rewrite_evidence(fixture_root, mutation)

                with self.assertRaises(SupplyChainEvidenceError):
                    validate_supply_chain_evidence(repository_root=fixture_root)

    def test_raw_osv_command_cannot_be_presented_as_current_executable_evidence(
        self,
    ) -> None:
        """
        함수 이름: test_raw_osv_command_cannot_be_presented_as_current_executable_evidence()
        기능: Historical raw OSV 명령을 current 실행 경계로 바꾸는 evidence 변조를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_root = Path(temporary_directory)
            self._copy_evidence_fixture(fixture_root)

            def replace_wrapper_with_raw_command(
                payload: dict[str, object],
            ) -> None:
                """
                함수 이름: replace_wrapper_with_raw_command()
                기능: Current local wrapper 한 건을 sandbox 없는 raw scanner 명령으로 변조한다.
                인자: payload -> 수정할 test evidence object
                반환값: 없음
                작성 날짜: 2026/08/29
                """
                commands = payload["commands"]
                assert isinstance(commands, list)

                # Current 표시는 유지해 wrapper 우회 자체가 checker에 의해 차단되는지 확인한다.
                commands[3] = (
                    "CURRENT_LOCAL_ONLY: osv-scanner scan source "
                    "--lockfile backend/uv.lock"
                )

            self._rewrite_evidence(
                fixture_root,
                replace_wrapper_with_raw_command,
            )

            with self.assertRaisesRegex(
                SupplyChainEvidenceError,
                "raw current OSV command",
            ):
                validate_supply_chain_evidence(repository_root=fixture_root)

    def test_unapproved_current_local_command_is_rejected(self) -> None:
        """
        함수 이름: test_unapproved_current_local_command_is_rejected()
        기능: Local-only 접두사를 붙인 임의 network command도 evidence allowlist에서 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_root = Path(temporary_directory)
            self._copy_evidence_fixture(fixture_root)

            def append_unapproved_command(payload: dict[str, object]) -> None:
                """
                함수 이름: append_unapproved_command()
                기능: Test evidence에 허용되지 않은 current command를 추가한다.
                인자: payload -> 수정할 evidence object
                반환값: 없음
                작성 날짜: 2026/08/29
                """
                # 접두사는 맞지만 allowlist에 없는 network command를 삽입한다.
                commands = payload["commands"]
                assert isinstance(commands, list)
                commands.append("CURRENT_LOCAL_ONLY: curl https://api.osv.dev")

            self._rewrite_evidence(fixture_root, append_unapproved_command)

            with self.assertRaisesRegex(
                SupplyChainEvidenceError,
                "command allowlist",
            ):
                validate_supply_chain_evidence(repository_root=fixture_root)

    def test_duplicate_key_and_nonstandard_number_fail_closed(self) -> None:
        """
        함수 이름: test_duplicate_key_and_nonstandard_number_fail_closed()
        기능: Duplicate JSON key와 NaN이 supply evidence parser에서 overwrite되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        replacements = (
            (
                '"schema_version": 4,',
                '"schema_version": 4,\n  "schema_version": 4,',
                "duplicate keys",
            ),
            ('"schema_version": 4,', '"schema_version": NaN,', "non-standard"),
        )
        for old_text, new_text, expected_error in replacements:
            with self.subTest(expected_error=expected_error), tempfile.TemporaryDirectory() as directory:
                fixture_root = Path(directory)
                self._copy_evidence_fixture(fixture_root)
                evidence_path = fixture_root / EVIDENCE_PATH
                source_text = evidence_path.read_text(encoding="utf-8")

                # Parser 경계에서만 실패하도록 첫 schema token 하나를 malformed 값으로 바꾼다.
                evidence_path.write_text(
                    source_text.replace(old_text, new_text, 1),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(SupplyChainEvidenceError, expected_error):
                    validate_supply_chain_evidence(repository_root=fixture_root)


if __name__ == "__main__":
    unittest.main()
