"""Phase 12 release evidence gate의 schema, go/no-go와 redacted CLI 경계를 검증한다."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import hashlib
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import scripts.phase12_release_gate as release_gate

from scripts.phase12_release_gate import (
    EvidenceLoadError,
    EvidenceValidationError,
    GENERIC_ERROR_MESSAGE,
    GENERIC_PASS_MESSAGE,
    load_evidence_manifest,
    main,
    calculate_regular_file_sha256,
    read_app_release_metadata,
    validate_release_evidence,
    verify_notarization_submissions,
    verify_release_evidence_against_artifacts,
)


VALID_DIGEST = "0123456789abcdef" * 4


def build_valid_manifest() -> dict[str, object]:
    """
    함수 이름: build_valid_manifest()
    기능: Phase 12 release gate의 모든 필수 invariant를 충족하는 non-secret fixture를 만든다.
    인자: 없음
    반환값: test별로 독립 수정할 수 있는 evidence mapping
    작성 날짜: 2026/08/24
    """
    signed_artifact = {
        "team_id": "TEAMID1234",
        "hardened_runtime": True,
        "min_os": "11.0",
    }
    return {
        "schema_version": 2,
        "release_candidate": {
            "commit": "a" * 40,
            "dirty": False,
            "build_host_id": "build-host-a",
            "version": "0.1.0",
            "build_version": "1",
        },
        "signing": {
            "identity": "Developer ID Application: Example Organization (TEAMID1234)",
            "team_id": "TEAMID1234",
            "library_validation_disabled": False,
            "provenance_sha256": "89abcdef01234567" * 4,
            "app": deepcopy(signed_artifact),
            "sidecar": deepcopy(signed_artifact),
            "extracted_native": [
                {
                    **deepcopy(signed_artifact),
                    "kind": "libpython",
                    "sha256": "1234567890abcdef" * 4,
                },
                {
                    **deepcopy(signed_artifact),
                    "kind": "native-extension",
                    "sha256": "abcdef0123456789" * 4,
                },
            ],
        },
        "notarization": {
            "app": {
                "status": "Accepted",
                "submission_id": "11111111-2222-4333-8444-555555555555",
                "stapler_valid": True,
                "gatekeeper_accepted": True,
            },
            "dmg": {
                "status": "Accepted",
                "submission_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                "stapler_valid": True,
                "gatekeeper_accepted": True,
                "hdiutil_verified": True,
            },
        },
        "clean_mac": {
            "host_id": "clean-host-b",
            "quarantine_bypassed": False,
            "missing_keychain_sidecar_started": False,
            "read_only_state": "READY",
            "allow_testnet_orders": False,
            "max_notional": None,
            "orders_created": 0,
            "pending_orders": 0,
            "position_quantity": "0",
            "safe_shutdown": True,
            "shutdown_http_status": 202,
            "shutdown_receipt": "CLOSED",
            "native_exit_code": 0,
            "orphan_processes": 0,
            "relaunch_state": "READY",
        },
        "regressions": {
            "backend": {
                "total": 643,
                "passed": 637,
                "skipped": 6,
                "failed": 0,
                "errors": 0,
            },
            "ui": {
                "total": 280,
                "passed": 280,
                "skipped": 0,
                "failed": 0,
                "errors": 0,
            },
            "rust": {
                "total": 31,
                "passed": 31,
                "skipped": 0,
                "failed": 0,
                "errors": 0,
            },
            "scripts": {
                "total": 114,
                "passed": 114,
                "skipped": 0,
                "failed": 0,
                "errors": 0,
            },
        },
        "checks": {
            "typescript": True,
            "vite_build": True,
            "cargo_fmt": True,
            "cargo_check": True,
            "cargo_clippy": True,
            "shell_syntax": True,
            "git_diff_check": True,
            "mounted_dmg_verification": True,
        },
        "secret_scan": {
            "passed": True,
            "canaries": 2,
            "files": 2505,
            "repository": True,
            "signed_app": True,
            "dmg": True,
            "mounted_dmg": True,
            "application_support": True,
            "diagnostic_reports": True,
        },
        "sha256": {
            "build": VALID_DIGEST,
            "final": VALID_DIGEST,
            "clean_mac": VALID_DIGEST,
        },
    }


class PhaseTwelveReleaseGateTests(unittest.TestCase):
    """
    클래스 이름: PhaseTwelveReleaseGateTests
    기능: Phase 13 진입 전에 모든 Phase 12 release evidence invariant를 fail-closed로 검증한다.
    작성 날짜: 2026/08/24
    """

    def setUp(self) -> None:
        """
        함수 이름: setUp()
        기능: 각 test가 독립적으로 수정할 수 있는 valid manifest를 준비한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        self.valid_manifest = build_valid_manifest()

    def _assert_manifest_rejected(self, manifest: object) -> None:
        """
        함수 이름: _assert_manifest_rejected()
        기능: evidence mutation이 validation error로 거부되는지 확인한다.
        인자: manifest -> 거부되어야 하는 evidence root value
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with self.assertRaises(EvidenceValidationError):
            validate_release_evidence(manifest)

    def _run_json_manifest(
        self,
        manifest: object,
    ) -> tuple[int, str, str]:
        """
        함수 이름: _run_json_manifest()
        기능: temporary JSON file로 CLI main을 실행해 status와 두 output stream을 포착한다.
        인자: manifest -> JSON으로 기록할 evidence value
        반환값: exit status, stdout과 stderr tuple
        작성 날짜: 2026/08/24
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = (
                Path(temporary_directory).resolve() / "release-evidence.json"
            )
            manifest_path.write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            captured_stdout = StringIO()
            captured_stderr = StringIO()
            with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
                exit_status = main(
                    [str(manifest_path), "/release/app.app", "/release/app.dmg"],
                    runtime_verifier=lambda manifest, app, dmg: None,
                )
        return exit_status, captured_stdout.getvalue(), captured_stderr.getvalue()

    def _run_raw_manifest(
        self,
        manifest_text: str,
    ) -> tuple[int, str, str]:
        """
        함수 이름: _run_raw_manifest()
        기능: raw temporary evidence text로 CLI load failure 경계를 실행한다.
        인자: manifest_text -> 그대로 기록할 JSON 또는 malformed text
        반환값: exit status, stdout과 stderr tuple
        작성 날짜: 2026/08/24
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = (
                Path(temporary_directory).resolve() / "release-evidence.json"
            )
            manifest_path.write_text(manifest_text, encoding="utf-8")
            captured_stdout = StringIO()
            captured_stderr = StringIO()
            with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
                exit_status = main(
                    [str(manifest_path), "/release/app.app", "/release/app.dmg"],
                    runtime_verifier=lambda manifest, app, dmg: None,
                )
        return exit_status, captured_stdout.getvalue(), captured_stderr.getvalue()

    def _write_release_artifact_fixture(
        self,
        temporary_path: Path,
        *,
        version: str = "0.1.0",
        build_version: str = "1",
    ) -> tuple[Path, Path, str]:
        """
        함수 이름: _write_release_artifact_fixture()
        기능: runtime binding test용 app plist와 final DMG byte fixture를 만든다.
        인자: temporary_path -> test 소유 root
            version -> CFBundleShortVersionString
            build_version -> CFBundleVersion
        반환값: app path, DMG path와 실제 digest
        작성 날짜: 2026/08/24
        """
        temporary_path.mkdir(parents=True, exist_ok=True)
        temporary_path = temporary_path.resolve(strict=True)
        app_path = temporary_path / "Release.app"
        contents_path = app_path / "Contents"
        contents_path.mkdir(parents=True)
        (contents_path / "Info.plist").write_bytes(
            plistlib.dumps(
                {
                    "CFBundleIdentifier": "com.binance-auto.trader",
                    "CFBundleShortVersionString": version,
                    "CFBundleVersion": build_version,
                }
            )
        )
        dmg_path = temporary_path / "Release.dmg"
        dmg_bytes = b"phase12-final-dmg-fixture-bytes"
        dmg_path.write_bytes(dmg_bytes)
        return app_path, dmg_path, hashlib.sha256(dmg_bytes).hexdigest()

    def _verified_artifact_evidence(
        self,
        manifest: dict[str, object],
        dmg_path: Path,
        *,
        extracted_native: tuple[object, ...] | None = None,
    ) -> object:
        """
        함수 이름: _verified_artifact_evidence()
        기능: live verifier의 provenance와 pinned-DMG evidence shape를 runtime gate test에 제공한다.
        인자: manifest -> expected signing/release evidence
            dmg_path -> verifier가 고정했다고 가정할 regular DMG
            extracted_native -> 선택적 native evidence override
        반환값: SignedArtifactEvidence와 같은 non-secret namespace
        작성 날짜: 2026/08/24
        """
        dmg_status = os.lstat(dmg_path)
        native_evidence = extracted_native
        if native_evidence is None:
            native_evidence = tuple(
                SimpleNamespace(
                    kind=artifact["kind"],
                    sha256=artifact["sha256"],
                )
                for artifact in manifest["signing"]["extracted_native"]
            )
        return SimpleNamespace(
            team_id="TEAMID1234",
            dmg_verified=True,
            extracted_native=native_evidence,
            commit=manifest["release_candidate"]["commit"],
            provenance_sha256=manifest["signing"]["provenance_sha256"],
            dmg_sha256=calculate_regular_file_sha256(dmg_path),
            dmg_snapshot=SimpleNamespace(
                device=dmg_status.st_dev,
                inode=dmg_status.st_ino,
                size=dmg_status.st_size,
                mtime_ns=dmg_status.st_mtime_ns,
                ctime_ns=dmg_status.st_ctime_ns,
            ),
        )

    def test_happy_path_validates_and_cli_prints_only_generic_pass(self) -> None:
        """
        함수 이름: test_happy_path_validates_and_cli_prints_only_generic_pass()
        기능: 완전한 evidence가 exception 없이 통과하고 generic PASS 한 줄만 출력하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        validate_release_evidence(self.valid_manifest)

        exit_status, captured_stdout, captured_stderr = self._run_json_manifest(
            self.valid_manifest
        )
        self.assertEqual(exit_status, 0)
        self.assertEqual(captured_stdout, f"{GENERIC_PASS_MESSAGE}\n")
        self.assertEqual(captured_stderr, "")

    def test_schema_version_and_exact_keys_fail_closed(self) -> None:
        """
        함수 이름: test_schema_version_and_exact_keys_fail_closed()
        기능: unknown version, boolean version, 누락 key와 unknown key를 모두 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        invalid_manifests: list[tuple[str, object]] = []

        unsupported_version = deepcopy(self.valid_manifest)
        unsupported_version["schema_version"] = 3
        invalid_manifests.append(("unsupported version", unsupported_version))

        boolean_version = deepcopy(self.valid_manifest)
        boolean_version["schema_version"] = True
        invalid_manifests.append(("boolean version", boolean_version))

        missing_section = deepcopy(self.valid_manifest)
        del missing_section["secret_scan"]
        invalid_manifests.append(("missing section", missing_section))

        unknown_section = deepcopy(self.valid_manifest)
        unknown_section["unreviewed_evidence"] = True
        invalid_manifests.append(("unknown section", unknown_section))

        for case_name, invalid_manifest in invalid_manifests:
            with self.subTest(case_name=case_name):
                self._assert_manifest_rejected(invalid_manifest)

    def test_release_candidate_requires_full_commit_and_clean_worktree(self) -> None:
        """
        함수 이름: test_release_candidate_requires_full_commit_and_clean_worktree()
        기능: commit 형식, dirty exact false와 non-empty build host를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        invalid_values = (
            ("commit", "a" * 39),
            ("commit", "g" * 40),
            ("commit", "A" * 40),
            ("dirty", True),
            ("dirty", 0),
            ("build_host_id", ""),
        )

        for field_name, invalid_value in invalid_values:
            with self.subTest(field_name=field_name, invalid_value=invalid_value):
                invalid_manifest = deepcopy(self.valid_manifest)
                invalid_manifest["release_candidate"][field_name] = invalid_value
                self._assert_manifest_rejected(invalid_manifest)

    def test_release_candidate_requires_canonical_version_and_build_version(self) -> None:
        """
        함수 이름: test_release_candidate_requires_canonical_version_and_build_version()
        기능: explicit SemVer와 nonzero 1~3 component CFBundleVersion 형식을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        invalid_release_versions = (
            "1.0",
            "01.0.0",
            "1.0.0-01",
            "v1.0.0",
            "1.0.0+",
            1,
            True,
            "",
        )
        for invalid_version in invalid_release_versions:
            with self.subTest(field_name="version", invalid_value=invalid_version):
                invalid_manifest = deepcopy(self.valid_manifest)
                invalid_manifest["release_candidate"]["version"] = invalid_version
                self._assert_manifest_rejected(invalid_manifest)

        invalid_build_versions = (
            "0",
            "0.0",
            "0.0.0",
            "01",
            "1.02",
            "1.2.3.4",
            "1..2",
            1,
            True,
            "",
        )
        for invalid_build_version in invalid_build_versions:
            with self.subTest(
                field_name="build_version",
                invalid_value=invalid_build_version,
            ):
                invalid_manifest = deepcopy(self.valid_manifest)
                invalid_manifest["release_candidate"][
                    "build_version"
                ] = invalid_build_version
                self._assert_manifest_rejected(invalid_manifest)

        for missing_field in ("version", "build_version"):
            with self.subTest(missing_field=missing_field):
                invalid_manifest = deepcopy(self.valid_manifest)
                del invalid_manifest["release_candidate"][missing_field]
                self._assert_manifest_rejected(invalid_manifest)

        canonical_prerelease = deepcopy(self.valid_manifest)
        canonical_prerelease["release_candidate"]["version"] = (
            "1.2.3-rc.1+build.5"
        )
        canonical_prerelease["release_candidate"]["build_version"] = "1.2.3"
        validate_release_evidence(canonical_prerelease)

    def test_developer_application_identity_and_team_id_are_required(self) -> None:
        """
        함수 이름: test_developer_application_identity_and_team_id_are_required()
        기능: 다른 certificate class, 비어 있는 identity와 Team ID를 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        invalid_values = (
            ("identity", "Developer ID Installer: Example (TEAMID1234)"),
            ("identity", "Developer ID Application:"),
            ("team_id", ""),
            ("team_id", " TEAMID1234 "),
        )

        for field_name, invalid_value in invalid_values:
            with self.subTest(field_name=field_name):
                invalid_manifest = deepcopy(self.valid_manifest)
                invalid_manifest["signing"][field_name] = invalid_value
                self._assert_manifest_rejected(invalid_manifest)

        identity_team_mismatch = deepcopy(self.valid_manifest)
        identity_team_mismatch["signing"]["team_id"] = "OTHERTEAM1"
        for artifact_name in ("app", "sidecar"):
            identity_team_mismatch["signing"][artifact_name]["team_id"] = (
                "OTHERTEAM1"
            )
        for native_artifact in identity_team_mismatch["signing"][
            "extracted_native"
        ]:
            native_artifact["team_id"] = "OTHERTEAM1"
        self._assert_manifest_rejected(identity_team_mismatch)

        for invalid_team_id in ("teamid1234", "TEAM-ID123", "TEAMID123"):
            with self.subTest(invalid_team_id=invalid_team_id):
                noncanonical_team = deepcopy(self.valid_manifest)
                noncanonical_team["signing"]["team_id"] = invalid_team_id
                noncanonical_team["signing"]["identity"] = (
                    "Developer ID Application: Example Organization "
                    f"({invalid_team_id})"
                )
                for artifact_name in ("app", "sidecar"):
                    noncanonical_team["signing"][artifact_name]["team_id"] = (
                        invalid_team_id
                    )
                for native_artifact in noncanonical_team["signing"][
                    "extracted_native"
                ]:
                    native_artifact["team_id"] = invalid_team_id
                self._assert_manifest_rejected(noncanonical_team)

    def test_all_signed_artifacts_require_same_team_id_and_native_evidence(self) -> None:
        """
        함수 이름: test_all_signed_artifacts_require_same_team_id_and_native_evidence()
        기능: app, sidecar와 extracted native의 Team ID drift 및 빈 native evidence를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        artifact_locations = ("app", "sidecar")
        for artifact_location in artifact_locations:
            with self.subTest(artifact_location=artifact_location):
                invalid_manifest = deepcopy(self.valid_manifest)
                invalid_manifest["signing"][artifact_location]["team_id"] = "OTHERTEAM1"
                self._assert_manifest_rejected(invalid_manifest)

        native_mismatch = deepcopy(self.valid_manifest)
        native_mismatch["signing"]["extracted_native"][1]["team_id"] = "OTHERTEAM1"
        self._assert_manifest_rejected(native_mismatch)

        missing_native_evidence = deepcopy(self.valid_manifest)
        missing_native_evidence["signing"]["extracted_native"] = []
        self._assert_manifest_rejected(missing_native_evidence)

        only_libpython_evidence = deepcopy(self.valid_manifest)
        only_libpython_evidence["signing"]["extracted_native"] = (
            only_libpython_evidence["signing"]["extracted_native"][:1]
        )
        validate_release_evidence(only_libpython_evidence)

        missing_libpython_evidence = deepcopy(self.valid_manifest)
        missing_libpython_evidence["signing"]["extracted_native"] = (
            missing_libpython_evidence["signing"]["extracted_native"][1:]
        )
        self._assert_manifest_rejected(missing_libpython_evidence)

        duplicate_native_digest = deepcopy(self.valid_manifest)
        duplicate_native_digest["signing"]["extracted_native"][1]["sha256"] = (
            duplicate_native_digest["signing"]["extracted_native"][0]["sha256"]
        )
        self._assert_manifest_rejected(duplicate_native_digest)

        duplicate_native_kind = deepcopy(self.valid_manifest)
        duplicate_native_kind["signing"]["extracted_native"][1]["kind"] = (
            "libpython"
        )
        self._assert_manifest_rejected(duplicate_native_kind)

        placeholder_native_digest = deepcopy(self.valid_manifest)
        placeholder_native_digest["signing"]["extracted_native"][0]["sha256"] = (
            "0" * 64
        )
        self._assert_manifest_rejected(placeholder_native_digest)

        for invalid_provenance_digest in ("0" * 64, "A" * 64, "a" * 63):
            with self.subTest(provenance_digest=invalid_provenance_digest):
                invalid_manifest = deepcopy(self.valid_manifest)
                invalid_manifest["signing"]["provenance_sha256"] = (
                    invalid_provenance_digest
                )
                self._assert_manifest_rejected(invalid_manifest)

    def test_artifacts_require_hardened_runtime_and_exact_minimum_os(self) -> None:
        """
        함수 이름: test_artifacts_require_hardened_runtime_and_exact_minimum_os()
        기능: app, sidecar와 extracted native 모두 runtime true와 minOS 11.0인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        artifact_paths = (
            ("app", None),
            ("sidecar", None),
            ("extracted_native", 0),
        )
        for artifact_name, native_index in artifact_paths:
            for field_name, invalid_value in (
                ("hardened_runtime", False),
                ("hardened_runtime", 1),
                ("min_os", "10.15"),
                ("min_os", 11.0),
            ):
                with self.subTest(
                    artifact_name=artifact_name,
                    field_name=field_name,
                    invalid_value=invalid_value,
                ):
                    invalid_manifest = deepcopy(self.valid_manifest)
                    artifact = invalid_manifest["signing"][artifact_name]
                    if native_index is not None:
                        artifact = artifact[native_index]
                    artifact[field_name] = invalid_value
                    self._assert_manifest_rejected(invalid_manifest)

    def test_library_validation_disable_must_be_exact_false(self) -> None:
        """
        함수 이름: test_library_validation_disable_must_be_exact_false()
        기능: library-validation-disable true와 integer 대용값을 모두 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        for invalid_value in (True, 0, "false"):
            with self.subTest(invalid_value=invalid_value):
                invalid_manifest = deepcopy(self.valid_manifest)
                invalid_manifest["signing"][
                    "library_validation_disabled"
                ] = invalid_value
                self._assert_manifest_rejected(invalid_manifest)

    def test_notarization_requires_accepted_status_and_uuid_submission_ids(self) -> None:
        """
        함수 이름: test_notarization_requires_accepted_status_and_uuid_submission_ids()
        기능: app과 DMG 각각 exact Accepted status와 canonical UUID를 요구하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        for artifact_name in ("app", "dmg"):
            for field_name, invalid_value in (
                ("status", "In Progress"),
                ("status", "accepted"),
                ("submission_id", "not-a-uuid"),
                ("submission_id", "11111111222243338444555555555555"),
            ):
                with self.subTest(
                    artifact_name=artifact_name,
                    field_name=field_name,
                    invalid_value=invalid_value,
                ):
                    invalid_manifest = deepcopy(self.valid_manifest)
                    invalid_manifest["notarization"][artifact_name][
                        field_name
                    ] = invalid_value
                    self._assert_manifest_rejected(invalid_manifest)

        duplicate_submission = deepcopy(self.valid_manifest)
        duplicate_submission["notarization"]["dmg"]["submission_id"] = (
            duplicate_submission["notarization"]["app"]["submission_id"].upper()
        )
        self._assert_manifest_rejected(duplicate_submission)

    def test_stapler_gatekeeper_and_hdiutil_results_must_be_true_booleans(self) -> None:
        """
        함수 이름: test_stapler_gatekeeper_and_hdiutil_results_must_be_true_booleans()
        기능: app·DMG stapler/Gatekeeper와 DMG hdiutil 결과의 false 또는 non-boolean을 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        verification_fields = (
            ("app", "stapler_valid"),
            ("app", "gatekeeper_accepted"),
            ("dmg", "stapler_valid"),
            ("dmg", "gatekeeper_accepted"),
            ("dmg", "hdiutil_verified"),
        )
        for artifact_name, field_name in verification_fields:
            for invalid_value in (False, 1):
                with self.subTest(
                    artifact_name=artifact_name,
                    field_name=field_name,
                    invalid_value=invalid_value,
                ):
                    invalid_manifest = deepcopy(self.valid_manifest)
                    invalid_manifest["notarization"][artifact_name][
                        field_name
                    ] = invalid_value
                    self._assert_manifest_rejected(invalid_manifest)

    def test_clean_mac_must_be_distinct_and_must_not_bypass_quarantine(self) -> None:
        """
        함수 이름: test_clean_mac_must_be_distinct_and_must_not_bypass_quarantine()
        기능: build host 재사용, 빈 host와 quarantine 우회 evidence를 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        invalid_values = (
            ("host_id", "build-host-a"),
            ("host_id", ""),
            ("quarantine_bypassed", True),
            ("quarantine_bypassed", 0),
        )
        for field_name, invalid_value in invalid_values:
            with self.subTest(field_name=field_name, invalid_value=invalid_value):
                invalid_manifest = deepcopy(self.valid_manifest)
                invalid_manifest["clean_mac"][field_name] = invalid_value
                self._assert_manifest_rejected(invalid_manifest)

    def test_clean_mac_requires_missing_keychain_block_and_read_only_ready(self) -> None:
        """
        함수 이름: test_clean_mac_requires_missing_keychain_block_and_read_only_ready()
        기능: missing-Keychain 차단, orders-disabled config와 position/order 0 read-only 상태를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        invalid_values = (
            ("missing_keychain_sidecar_started", True),
            ("missing_keychain_sidecar_started", 0),
            ("read_only_state", "STARTING"),
            ("allow_testnet_orders", True),
            ("allow_testnet_orders", 0),
            ("max_notional", 10),
            ("max_notional", False),
            ("orders_created", 1),
            ("orders_created", False),
            ("pending_orders", 1),
            ("pending_orders", False),
            ("position_quantity", "0.0"),
            ("position_quantity", 0),
            ("position_quantity", False),
        )
        for field_name, invalid_value in invalid_values:
            with self.subTest(field_name=field_name, invalid_value=invalid_value):
                invalid_manifest = deepcopy(self.valid_manifest)
                invalid_manifest["clean_mac"][field_name] = invalid_value
                self._assert_manifest_rejected(invalid_manifest)

    def test_clean_mac_requires_safe_shutdown_zero_orphans_and_ready_relaunch(self) -> None:
        """
        함수 이름: test_clean_mac_requires_safe_shutdown_zero_orphans_and_ready_relaunch()
        기능: shutdown 202/CLOSED/exit 0, orphan 0과 relaunch READY가 모두 필수인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        invalid_values = (
            ("safe_shutdown", False),
            ("safe_shutdown", 1),
            ("shutdown_http_status", 200),
            ("shutdown_http_status", True),
            ("shutdown_receipt", "READY"),
            ("shutdown_receipt", 202),
            ("native_exit_code", 1),
            ("native_exit_code", False),
            ("orphan_processes", 1),
            ("orphan_processes", False),
            ("relaunch_state", "OFFLINE"),
        )
        for field_name, invalid_value in invalid_values:
            with self.subTest(field_name=field_name, invalid_value=invalid_value):
                invalid_manifest = deepcopy(self.valid_manifest)
                invalid_manifest["clean_mac"][field_name] = invalid_value
                self._assert_manifest_rejected(invalid_manifest)

        additional_required_fields = (
            "allow_testnet_orders",
            "max_notional",
            "pending_orders",
            "position_quantity",
            "shutdown_http_status",
            "shutdown_receipt",
            "native_exit_code",
        )
        for missing_field in additional_required_fields:
            with self.subTest(missing_field=missing_field):
                invalid_manifest = deepcopy(self.valid_manifest)
                del invalid_manifest["clean_mac"][missing_field]
                self._assert_manifest_rejected(invalid_manifest)

    def test_regression_suites_require_complete_balanced_failure_free_counts(self) -> None:
        """
        함수 이름: test_regression_suites_require_complete_balanced_failure_free_counts()
        기능: 필수 suite 누락, 불완전 count, failure/error와 실행 없는 suite를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        for suite_name in ("backend", "ui", "rust", "scripts"):
            with self.subTest(case_name="missing suite", suite_name=suite_name):
                invalid_manifest = deepcopy(self.valid_manifest)
                del invalid_manifest["regressions"][suite_name]
                self._assert_manifest_rejected(invalid_manifest)

        count_mutations = (
            ("total", 642),
            ("failed", 1),
            ("errors", 1),
            ("passed", 0),
            ("passed", True),
        )
        for field_name, invalid_value in count_mutations:
            with self.subTest(case_name="invalid count", field_name=field_name):
                invalid_manifest = deepcopy(self.valid_manifest)
                invalid_manifest["regressions"]["backend"][field_name] = invalid_value
                self._assert_manifest_rejected(invalid_manifest)

        missing_count = deepcopy(self.valid_manifest)
        del missing_count["regressions"]["backend"]["errors"]
        self._assert_manifest_rejected(missing_count)

        for suite_name, below_baseline_total in (
            ("backend", 642),
            ("ui", 279),
            ("rust", 29),
            ("scripts", 83),
        ):
            with self.subTest(case_name="below baseline", suite_name=suite_name):
                invalid_manifest = deepcopy(self.valid_manifest)
                invalid_manifest["regressions"][suite_name] = {
                    "total": below_baseline_total,
                    "passed": below_baseline_total,
                    "skipped": 0,
                    "failed": 0,
                    "errors": 0,
                }
                self._assert_manifest_rejected(invalid_manifest)

    def test_all_non_counted_release_checks_must_be_exact_true(self) -> None:
        """
        함수 이름: test_all_non_counted_release_checks_must_be_exact_true()
        기능: build, static, shell과 mounted-DMG check 8종의 누락·false·non-boolean을 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        required_checks = (
            "typescript",
            "vite_build",
            "cargo_fmt",
            "cargo_check",
            "cargo_clippy",
            "shell_syntax",
            "git_diff_check",
            "mounted_dmg_verification",
        )
        for check_name in required_checks:
            for invalid_value in (False, 1):
                with self.subTest(
                    check_name=check_name,
                    invalid_value=invalid_value,
                ):
                    invalid_manifest = deepcopy(self.valid_manifest)
                    invalid_manifest["checks"][check_name] = invalid_value
                    self._assert_manifest_rejected(invalid_manifest)

        missing_check = deepcopy(self.valid_manifest)
        del missing_check["checks"]["mounted_dmg_verification"]
        self._assert_manifest_rejected(missing_check)

    def test_secret_scan_requires_pass_two_canaries_and_nonzero_files(self) -> None:
        """
        함수 이름: test_secret_scan_requires_pass_two_canaries_and_nonzero_files()
        기능: scan PASS, 최소 두 canary와 실제 file count를 모두 요구하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        invalid_values = (
            ("passed", False),
            ("passed", 1),
            ("canaries", 1),
            ("canaries", True),
            ("files", 0),
            ("files", False),
        )
        for field_name, invalid_value in invalid_values:
            with self.subTest(field_name=field_name, invalid_value=invalid_value):
                invalid_manifest = deepcopy(self.valid_manifest)
                invalid_manifest["secret_scan"][field_name] = invalid_value
                self._assert_manifest_rejected(invalid_manifest)

        coverage_targets = (
            "repository",
            "signed_app",
            "dmg",
            "mounted_dmg",
            "application_support",
            "diagnostic_reports",
        )
        for coverage_target in coverage_targets:
            for invalid_value in (False, 1):
                with self.subTest(
                    coverage_target=coverage_target,
                    invalid_value=invalid_value,
                ):
                    invalid_manifest = deepcopy(self.valid_manifest)
                    invalid_manifest["secret_scan"][coverage_target] = invalid_value
                    self._assert_manifest_rejected(invalid_manifest)

        missing_coverage = deepcopy(self.valid_manifest)
        del missing_coverage["secret_scan"]["diagnostic_reports"]
        self._assert_manifest_rejected(missing_coverage)

    def test_sha256_values_must_be_lowercase_canonical_and_identical(self) -> None:
        """
        함수 이름: test_sha256_values_must_be_lowercase_canonical_and_identical()
        기능: build/final/clean Mac digest의 lowercase SHA-256 형식과 exact equality를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        invalid_values = (
            ("build", VALID_DIGEST.upper()),
            ("final", "a" * 63),
            ("clean_mac", "f" * 64),
            ("build", "0" * 64),
        )
        for digest_location, invalid_value in invalid_values:
            with self.subTest(digest_location=digest_location):
                invalid_manifest = deepcopy(self.valid_manifest)
                invalid_manifest["sha256"][digest_location] = invalid_value
                self._assert_manifest_rejected(invalid_manifest)

    def test_runtime_gate_binds_live_identity_version_artifacts_and_dmg_digest(
        self,
    ) -> None:
        """
        함수 이름: test_runtime_gate_binds_live_identity_version_artifacts_and_dmg_digest()
        기능: final PASS가 live preflight, exact app version, signed verifier와 실제 DMG byte를 요구하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            app_path, dmg_path, actual_digest = self._write_release_artifact_fixture(
                Path(temporary_directory).resolve()
            )
            manifest = deepcopy(self.valid_manifest)
            manifest["sha256"] = {
                "build": actual_digest,
                "final": actual_digest,
                "clean_mac": actual_digest,
            }
            captured_artifact_call: dict[str, object] = {}

            def identity_verifier(environment: object) -> object:
                self.assertEqual(environment, {"release": "test"})
                return SimpleNamespace(
                    commit_id="a" * 40,
                    identity=manifest["signing"]["identity"],
                    team_id="TEAMID1234",
                )

            def artifact_verifier(
                received_app_path: Path,
                received_team_id: str,
                **keyword_arguments: object,
            ) -> object:
                captured_artifact_call.update(
                    {
                        "app": received_app_path,
                        "team": received_team_id,
                        **keyword_arguments,
                    }
                )
                return self._verified_artifact_evidence(manifest, dmg_path)

            verify_release_evidence_against_artifacts(
                manifest,
                app_path,
                dmg_path,
                source_environment={"release": "test"},
                identity_verifier=identity_verifier,
                artifact_verifier=artifact_verifier,
                notarization_verifier=lambda manifest, environment: None,
            )

            self.assertEqual(captured_artifact_call["app"], app_path)
            self.assertEqual(captured_artifact_call["dmg_path"], dmg_path)
            self.assertEqual(
                captured_artifact_call["expected_identity"],
                manifest["signing"]["identity"],
            )
            self.assertEqual(captured_artifact_call["expected_version"], "0.1.0")
            self.assertEqual(captured_artifact_call["expected_build_version"], "1")
            self.assertEqual(captured_artifact_call["expected_commit"], "a" * 40)
            self.assertEqual(calculate_regular_file_sha256(dmg_path), actual_digest)
            self.assertEqual(read_app_release_metadata(app_path), ("0.1.0", "1"))

    def test_notarization_submission_ids_are_bound_to_live_accepted_info(self) -> None:
        """
        함수 이름: test_notarization_submission_ids_are_bound_to_live_accepted_info()
        기능: app/DMG UUID 각각을 sanitized notarytool info Accepted 결과와 대조하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        observed_commands: list[list[str]] = []

        def command_runner(
            command: list[str],
            working_directory: Path,
            environment: dict[str, str],
        ) -> subprocess.CompletedProcess[bytes]:
            del working_directory
            self.assertNotIn("APPLE_PASSWORD", environment)
            self.assertNotIn("NOTARY_PROFILE", environment)
            observed_commands.append(command)
            submission_id = command[3]
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {"id": submission_id, "status": "Accepted"}
                ).encode("utf-8"),
                stderr=b"",
            )

        verify_notarization_submissions(
            self.valid_manifest,
            {
                "NOTARY_PROFILE": "phase12-release",
                "APPLE_PASSWORD": "private-notary-password",
            },
            command_runner=command_runner,
        )
        self.assertEqual(len(observed_commands), 2)
        self.assertEqual(
            [command[2] for command in observed_commands],
            ["info", "info"],
        )

        def rejected_runner(
            command: list[str],
            working_directory: Path,
            environment: dict[str, str],
        ) -> subprocess.CompletedProcess[bytes]:
            del working_directory, environment
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {"id": command[3], "status": "In Progress"}
                ).encode("utf-8"),
                stderr=b"",
            )

        with self.assertRaises(EvidenceValidationError):
            verify_notarization_submissions(
                self.valid_manifest,
                {"NOTARY_PROFILE": "phase12-release"},
                command_runner=rejected_runner,
            )

    def test_runtime_gate_rejects_live_binding_drift(self) -> None:
        """
        함수 이름: test_runtime_gate_rejects_live_binding_drift()
        기능: commit, version, verifier result와 actual DMG drift가 각각 final gate를 닫는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory).resolve()
            app_path, dmg_path, actual_digest = self._write_release_artifact_fixture(
                temporary_path
            )
            manifest = deepcopy(self.valid_manifest)
            manifest["sha256"] = {
                "build": actual_digest,
                "final": actual_digest,
                "clean_mac": actual_digest,
            }

            def valid_identity(environment: object) -> object:
                del environment
                return SimpleNamespace(
                    commit_id="a" * 40,
                    identity=manifest["signing"]["identity"],
                    team_id="TEAMID1234",
                )

            def valid_artifact(app: Path, team: str, **kwargs: object) -> object:
                del app, team, kwargs
                return self._verified_artifact_evidence(manifest, dmg_path)

            def drifted_artifact(field_name: str, field_value: object) -> object:
                def verifier(app: Path, team: str, **kwargs: object) -> object:
                    del app, team, kwargs
                    evidence = self._verified_artifact_evidence(manifest, dmg_path)
                    setattr(evidence, field_name, field_value)
                    return evidence

                return verifier

            invalid_cases = []
            invalid_cases.append(
                (
                    "commit",
                    app_path,
                    dmg_path,
                    lambda environment: SimpleNamespace(
                        commit_id="b" * 40,
                        identity=manifest["signing"]["identity"],
                        team_id="TEAMID1234",
                    ),
                    valid_artifact,
                )
            )
            mismatched_app, _, _ = self._write_release_artifact_fixture(
                temporary_path / "mismatched-version",
                version="0.1.1",
            )
            invalid_cases.append(
                ("version", mismatched_app, dmg_path, valid_identity, valid_artifact)
            )
            invalid_cases.append(
                (
                    "artifact",
                    app_path,
                    dmg_path,
                    valid_identity,
                    lambda app, team, **kwargs: SimpleNamespace(
                        team_id="TEAMID1234",
                        dmg_verified=False,
                    ),
                )
            )
            invalid_cases.append(
                (
                    "native evidence",
                    app_path,
                    dmg_path,
                    valid_identity,
                    lambda app, team, **kwargs: SimpleNamespace(
                        team_id="TEAMID1234",
                        dmg_verified=True,
                        commit="a" * 40,
                        provenance_sha256=manifest["signing"][
                            "provenance_sha256"
                        ],
                        dmg_sha256=actual_digest,
                        dmg_snapshot=self._verified_artifact_evidence(
                            manifest,
                            dmg_path,
                        ).dmg_snapshot,
                        extracted_native=(
                            SimpleNamespace(
                                kind="libpython",
                                sha256="fedcba9876543210" * 4,
                            ),
                            SimpleNamespace(
                                kind="native-extension",
                                sha256="abcdef0123456789" * 4,
                            ),
                        ),
                    ),
                )
            )
            duplicated_native = tuple(
                SimpleNamespace(
                    kind=native_evidence["kind"],
                    sha256=native_evidence["sha256"],
                )
                for native_evidence in manifest["signing"]["extracted_native"]
            )
            invalid_cases.append(
                (
                    "duplicated verifier native evidence",
                    app_path,
                    dmg_path,
                    valid_identity,
                    lambda app, team, **kwargs: self._verified_artifact_evidence(
                        manifest,
                        dmg_path,
                        extracted_native=(duplicated_native[0], *duplicated_native),
                    ),
                )
            )
            invalid_cases.extend(
                (
                    drift_name,
                    app_path,
                    dmg_path,
                    valid_identity,
                    drifted_artifact(drift_field, drift_value),
                )
                for drift_name, drift_field, drift_value in (
                    ("artifact commit", "commit", "b" * 40),
                    (
                        "archive provenance",
                        "provenance_sha256",
                        "fedcba9876543210" * 4,
                    ),
                    ("verifier DMG digest", "dmg_sha256", "1234567890abcdef" * 4),
                    (
                        "verifier DMG snapshot",
                        "dmg_snapshot",
                        SimpleNamespace(
                            device=os.lstat(dmg_path).st_dev,
                            inode=os.lstat(dmg_path).st_ino + 1,
                            size=os.lstat(dmg_path).st_size,
                            mtime_ns=os.lstat(dmg_path).st_mtime_ns,
                            ctime_ns=os.lstat(dmg_path).st_ctime_ns,
                        ),
                    ),
                )
            )
            changed_dmg_path = temporary_path / "changed.dmg"
            changed_dmg_path.write_bytes(b"different-final-dmg")
            invalid_cases.append(
                ("digest", app_path, changed_dmg_path, valid_identity, valid_artifact)
            )

            for (
                case_name,
                case_app_path,
                case_dmg_path,
                case_identity_verifier,
                case_artifact_verifier,
            ) in invalid_cases:
                with self.subTest(case_name=case_name):
                    with self.assertRaises(EvidenceValidationError):
                        verify_release_evidence_against_artifacts(
                            manifest,
                            case_app_path,
                            case_dmg_path,
                            identity_verifier=case_identity_verifier,
                            artifact_verifier=case_artifact_verifier,
                            notarization_verifier=lambda manifest, environment: None,
                        )

    def test_runtime_gate_rejects_dmg_symlink(self) -> None:
        """
        함수 이름: test_runtime_gate_rejects_dmg_symlink()
        기능: digest binding이 final DMG symlink를 따라가지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory).resolve()
            _, dmg_path, _ = self._write_release_artifact_fixture(temporary_path)
            linked_dmg_path = temporary_path / "linked.dmg"
            linked_dmg_path.symlink_to(dmg_path)
            with self.assertRaises(EvidenceValidationError):
                calculate_regular_file_sha256(linked_dmg_path)

    def test_runtime_gate_requires_physical_absolute_artifact_paths(self) -> None:
        """
        함수 이름: test_runtime_gate_requires_physical_absolute_artifact_paths()
        기능: app/DMG의 relative path와 ancestor-directory symlink를 live 검증 전에 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory).resolve()
            physical_directory = temporary_path / "physical-artifacts"
            app_path, dmg_path, actual_digest = self._write_release_artifact_fixture(
                physical_directory
            )
            manifest = deepcopy(self.valid_manifest)
            manifest["sha256"] = {
                "build": actual_digest,
                "final": actual_digest,
                "clean_mac": actual_digest,
            }

            linked_directory = temporary_path / "linked-artifacts"
            linked_directory.symlink_to(physical_directory, target_is_directory=True)
            relative_app_path = Path(os.path.relpath(app_path, Path.cwd()))
            relative_dmg_path = Path(os.path.relpath(dmg_path, Path.cwd()))

            def valid_identity(environment: object) -> object:
                del environment
                return SimpleNamespace(
                    commit_id="a" * 40,
                    identity=manifest["signing"]["identity"],
                    team_id="TEAMID1234",
                )

            def valid_artifact(app: Path, team: str, **kwargs: object) -> object:
                del app, team, kwargs
                return self._verified_artifact_evidence(manifest, dmg_path)

            invalid_path_pairs = (
                (linked_directory / app_path.name, dmg_path),
                (app_path, linked_directory / dmg_path.name),
                (relative_app_path, dmg_path),
                (app_path, relative_dmg_path),
            )
            for invalid_app_path, invalid_dmg_path in invalid_path_pairs:
                with self.subTest(
                    app_absolute=invalid_app_path.is_absolute(),
                    dmg_absolute=invalid_dmg_path.is_absolute(),
                    app_parent=invalid_app_path.parent.name,
                    dmg_parent=invalid_dmg_path.parent.name,
                ):
                    with self.assertRaises(EvidenceValidationError):
                        verify_release_evidence_against_artifacts(
                            manifest,
                            invalid_app_path,
                            invalid_dmg_path,
                            identity_verifier=valid_identity,
                            artifact_verifier=valid_artifact,
                            notarization_verifier=lambda manifest, environment: None,
                        )

    def test_runtime_gate_rejects_same_digest_artifact_path_replacement(self) -> None:
        """
        함수 이름: test_runtime_gate_rejects_same_digest_artifact_path_replacement()
        기능: verifier 실행 중 app 또는 same-byte DMG path inode가 교체되면 fail closed한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        for replacement_target in ("app", "app-content", "app-mode", "dmg"):
            with self.subTest(replacement_target=replacement_target):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    temporary_path = Path(temporary_directory).resolve()
                    app_path, dmg_path, actual_digest = (
                        self._write_release_artifact_fixture(
                            temporary_path / "candidate"
                        )
                    )
                    replacement_app, replacement_dmg, _ = (
                        self._write_release_artifact_fixture(
                            temporary_path / "replacement"
                        )
                    )
                    manifest = deepcopy(self.valid_manifest)
                    manifest["sha256"] = {
                        "build": actual_digest,
                        "final": actual_digest,
                        "clean_mac": actual_digest,
                    }
                    replacement_completed = False

                    def valid_identity(environment: object) -> object:
                        del environment
                        return SimpleNamespace(
                            commit_id="a" * 40,
                            identity=manifest["signing"]["identity"],
                            team_id="TEAMID1234",
                        )

                    def replacing_artifact_verifier(
                        received_app_path: Path,
                        received_team_id: str,
                        **keyword_arguments: object,
                    ) -> object:
                        del received_team_id, keyword_arguments
                        nonlocal replacement_completed
                        if replacement_target == "app":
                            received_app_path.rename(
                                temporary_path / "original-app-backup.app"
                            )
                            replacement_app.rename(received_app_path)
                        elif replacement_target == "app-content":
                            info_plist_path = (
                                received_app_path / "Contents" / "Info.plist"
                            )
                            info_plist_path.write_bytes(
                                info_plist_path.read_bytes() + b"\n"
                            )
                        elif replacement_target == "app-mode":
                            info_plist_path = (
                                received_app_path / "Contents" / "Info.plist"
                            )
                            info_plist_path.chmod(0o600)
                        else:
                            os.replace(replacement_dmg, dmg_path)
                        replacement_completed = True
                        return self._verified_artifact_evidence(
                            manifest,
                            dmg_path,
                        )

                    with self.assertRaises(EvidenceValidationError):
                        verify_release_evidence_against_artifacts(
                            manifest,
                            app_path,
                            dmg_path,
                            identity_verifier=valid_identity,
                            artifact_verifier=replacing_artifact_verifier,
                            notarization_verifier=lambda manifest, environment: None,
                        )
                    self.assertTrue(replacement_completed)

    def test_cli_never_passes_schema_only_manifest_without_live_preflight(self) -> None:
        """
        함수 이름: test_cli_never_passes_schema_only_manifest_without_live_preflight()
        기능: 완전한 자기신고 JSON도 실제 identity/notary/git/artifact preflight 없이는 PASS하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory).resolve()
            app_path, dmg_path, actual_digest = self._write_release_artifact_fixture(
                temporary_path
            )
            manifest = deepcopy(self.valid_manifest)
            manifest["sha256"] = {
                "build": actual_digest,
                "final": actual_digest,
                "clean_mac": actual_digest,
            }
            manifest_path = temporary_path / "release-evidence.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            captured_stdout = StringIO()
            captured_stderr = StringIO()
            with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
                exit_status = main(
                    [str(manifest_path), str(app_path), str(dmg_path)]
                )

        self.assertNotEqual(exit_status, 0)
        self.assertEqual(captured_stdout.getvalue(), "")
        self.assertEqual(captured_stderr.getvalue(), f"{GENERIC_ERROR_MESSAGE}\n")

    def test_duplicate_keys_malformed_json_and_nonstandard_numbers_are_load_errors(
        self,
    ) -> None:
        """
        함수 이름: test_duplicate_keys_malformed_json_and_nonstandard_numbers_are_load_errors()
        기능: ambiguous duplicate key, malformed text와 NaN을 schema 검증 전에 status 2로 닫는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        invalid_documents = (
            '{"schema_version": 1, "schema_version": 1}',
            '{"schema_version":',
            '{"schema_version": NaN}',
        )
        for invalid_document in invalid_documents:
            with self.subTest(invalid_document_kind=invalid_document[:20]):
                exit_status, captured_stdout, captured_stderr = self._run_raw_manifest(
                    invalid_document
                )
                self.assertEqual(exit_status, 2)
                self.assertEqual(captured_stdout, "")
                self.assertEqual(captured_stderr, f"{GENERIC_ERROR_MESSAGE}\n")

    def test_missing_file_and_invalid_usage_return_generic_status_two(self) -> None:
        """
        함수 이름: test_missing_file_and_invalid_usage_return_generic_status_two()
        기능: unreadable target와 잘못된 argument count가 usage/path detail 없이 status 2인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing_path = (
                Path(temporary_directory).resolve() / "missing-private-evidence.json"
            )
            with self.assertRaises(EvidenceLoadError):
                load_evidence_manifest(missing_path)

            captured_stdout = StringIO()
            captured_stderr = StringIO()
            with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
                exit_status = main(
                    [str(missing_path), "/release/app.app", "/release/app.dmg"],
                    runtime_verifier=lambda manifest, app, dmg: None,
                )
            self.assertEqual(exit_status, 2)
            self.assertEqual(captured_stdout.getvalue(), "")
            self.assertEqual(captured_stderr.getvalue(), f"{GENERIC_ERROR_MESSAGE}\n")
            self.assertNotIn(str(missing_path), captured_stderr.getvalue())

        for invalid_arguments in (
            [],
            ["one.json"],
            ["one.json", "two.json"],
            ["one.json", "two.app", "three.dmg", "four"],
        ):
            with self.subTest(invalid_arguments=invalid_arguments):
                captured_stdout = StringIO()
                captured_stderr = StringIO()
                with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
                    exit_status = main(invalid_arguments)
                self.assertEqual(exit_status, 2)
                self.assertEqual(captured_stdout.getvalue(), "")
                self.assertEqual(
                    captured_stderr.getvalue(),
                    f"{GENERIC_ERROR_MESSAGE}\n",
                )

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "requires symlink support")
    def test_manifest_symlink_is_rejected_without_following_target(self) -> None:
        """
        함수 이름: test_manifest_symlink_is_rejected_without_following_target()
        기능: valid target을 가리켜도 evidence symlink를 single-FD loader가 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory).resolve()
            real_manifest_path = temporary_path / "real-evidence.json"
            real_manifest_path.write_text(
                json.dumps(self.valid_manifest),
                encoding="utf-8",
            )
            symlink_manifest_path = temporary_path / "linked-evidence.json"
            symlink_manifest_path.symlink_to(real_manifest_path)

            with self.assertRaises(EvidenceLoadError):
                load_evidence_manifest(symlink_manifest_path)

            captured_stdout = StringIO()
            captured_stderr = StringIO()
            with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
                exit_status = main(
                    [
                        str(symlink_manifest_path),
                        "/release/app.app",
                        "/release/app.dmg",
                    ],
                    runtime_verifier=lambda manifest, app, dmg: None,
                )
        self.assertEqual(exit_status, 2)
        self.assertEqual(captured_stdout.getvalue(), "")
        self.assertEqual(captured_stderr.getvalue(), f"{GENERIC_ERROR_MESSAGE}\n")

    def test_manifest_requires_physical_absolute_path(self) -> None:
        """
        함수 이름: test_manifest_requires_physical_absolute_path()
        기능: existing manifest의 relative path와 ancestor-directory symlink를 open 전에 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory).resolve()
            physical_directory = temporary_path / "physical-evidence"
            physical_directory.mkdir()
            manifest_path = physical_directory / "release-evidence.json"
            manifest_path.write_text(
                json.dumps(self.valid_manifest),
                encoding="utf-8",
            )

            linked_directory = temporary_path / "linked-evidence"
            linked_directory.symlink_to(physical_directory, target_is_directory=True)
            linked_manifest_path = linked_directory / manifest_path.name
            relative_manifest_path = Path(
                os.path.relpath(manifest_path, Path.cwd())
            )

            self.assertFalse(relative_manifest_path.is_absolute())
            for invalid_manifest_path in (
                linked_manifest_path,
                relative_manifest_path,
            ):
                with self.subTest(
                    absolute=invalid_manifest_path.is_absolute(),
                    parent=invalid_manifest_path.parent.name,
                ):
                    with self.assertRaises(EvidenceLoadError):
                        load_evidence_manifest(invalid_manifest_path)

    def test_manifest_rejects_descriptor_and_final_path_snapshot_drift(self) -> None:
        """
        함수 이름: test_manifest_rejects_descriptor_and_final_path_snapshot_drift()
        기능: read 전/후 동일 FD 또는 final lstat의 mutation-sensitive metadata drift를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = (
                Path(temporary_directory).resolve() / "release-evidence.json"
            )
            manifest_path.write_text(
                json.dumps(self.valid_manifest),
                encoding="utf-8",
            )

            def changed_status(file_status: object) -> object:
                return SimpleNamespace(
                    st_mode=file_status.st_mode,
                    st_dev=file_status.st_dev,
                    st_ino=file_status.st_ino,
                    st_size=file_status.st_size,
                    st_mtime_ns=file_status.st_mtime_ns,
                    st_ctime_ns=file_status.st_ctime_ns + 1,
                )

            real_fstat = os.fstat
            descriptor_stat_calls = 0

            def drifted_fstat(file_descriptor: int) -> object:
                nonlocal descriptor_stat_calls
                descriptor_stat_calls += 1
                actual_status = real_fstat(file_descriptor)
                if descriptor_stat_calls == 2:
                    return changed_status(actual_status)
                return actual_status

            with patch.object(release_gate.os, "fstat", side_effect=drifted_fstat):
                with self.assertRaises(EvidenceLoadError):
                    load_evidence_manifest(manifest_path)
            self.assertEqual(descriptor_stat_calls, 2)

            real_path_inspector = release_gate._inspect_physical_absolute_path
            path_inspection_calls = 0

            def drifted_path_inspector(file_path: Path) -> object:
                nonlocal path_inspection_calls
                path_inspection_calls += 1
                actual_status = real_path_inspector(file_path)
                if path_inspection_calls == 2:
                    return changed_status(actual_status)
                return actual_status

            with patch.object(
                release_gate,
                "_inspect_physical_absolute_path",
                side_effect=drifted_path_inspector,
            ):
                with self.assertRaises(EvidenceLoadError):
                    load_evidence_manifest(manifest_path)
            self.assertEqual(path_inspection_calls, 2)

    def test_validation_error_output_redacts_manifest_values_and_path(self) -> None:
        """
        함수 이름: test_validation_error_output_redacts_manifest_values_and_path()
        기능: invalid evidence의 marker와 secret-bearing path가 CLI failure output에 없는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        private_marker = "private-release-marker-92741"
        invalid_manifest = deepcopy(self.valid_manifest)
        invalid_manifest["signing"]["identity"] = (
            f"Developer ID Installer: {private_marker}"
        )

        with tempfile.TemporaryDirectory(
            prefix=f"{private_marker}-"
        ) as temporary_directory:
            manifest_path = (
                Path(temporary_directory).resolve() / f"{private_marker}.json"
            )
            manifest_path.write_text(json.dumps(invalid_manifest), encoding="utf-8")
            captured_stdout = StringIO()
            captured_stderr = StringIO()
            with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
                exit_status = main(
                    [str(manifest_path), "/release/app.app", "/release/app.dmg"],
                    runtime_verifier=lambda manifest, app, dmg: None,
                )

        combined_output = captured_stdout.getvalue() + captured_stderr.getvalue()
        self.assertEqual(exit_status, 1)
        self.assertEqual(captured_stdout.getvalue(), "")
        self.assertEqual(captured_stderr.getvalue(), f"{GENERIC_ERROR_MESSAGE}\n")
        self.assertNotIn(private_marker, combined_output)
        self.assertNotIn("identity", combined_output)

    def test_load_error_output_redacts_raw_content_and_path(self) -> None:
        """
        함수 이름: test_load_error_output_redacts_raw_content_and_path()
        기능: malformed evidence 원문과 path marker가 generic load 오류에 반사되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        private_marker = "private-json-marker-58302"
        with tempfile.TemporaryDirectory(
            prefix=f"{private_marker}-"
        ) as temporary_directory:
            manifest_path = (
                Path(temporary_directory).resolve() / f"{private_marker}.json"
            )
            manifest_path.write_text(
                f'{{"private": "{private_marker}"',
                encoding="utf-8",
            )
            captured_stdout = StringIO()
            captured_stderr = StringIO()
            with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
                exit_status = main(
                    [str(manifest_path), "/release/app.app", "/release/app.dmg"],
                    runtime_verifier=lambda manifest, app, dmg: None,
                )

        combined_output = captured_stdout.getvalue() + captured_stderr.getvalue()
        self.assertEqual(exit_status, 2)
        self.assertEqual(captured_stdout.getvalue(), "")
        self.assertEqual(captured_stderr.getvalue(), f"{GENERIC_ERROR_MESSAGE}\n")
        self.assertNotIn(private_marker, combined_output)
        self.assertNotIn(str(manifest_path), combined_output)


if __name__ == "__main__":
    unittest.main()
