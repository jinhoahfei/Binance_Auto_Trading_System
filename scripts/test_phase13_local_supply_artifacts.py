"""Phase 13 exact-lockfile local supply artifact의 재생성·NO_GO 경계를 검증한다."""

from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from scripts.phase13_local_supply_artifacts import (
    EXPECTED_HISTORICAL_DMG_SHA256,
    HISTORICAL_RELEASE_BINDING_PATH,
    LICENSE_INVENTORY_PATH,
    LOCKFILE_SPECS,
    NOTICE_REVIEW_PATH,
    SBOM_PATH,
    LocalSupplyArtifactError,
    _calculate_app_content_tree,
    _read_local_metadata_bytes,
    _read_regular_bytes,
    build_dependency_license_inventory,
    build_exact_lockfile_sbom,
    build_third_party_notice_review,
    read_exact_lockfile_components,
    validate_local_supply_artifacts,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class Phase13LocalSupplyArtifactTests(unittest.TestCase):
    """
    클래스 이름: Phase13LocalSupplyArtifactTests
    기능: SBOM·license·notice·historical release 산출물의 정직한 NO_GO를 검증한다.
    작성 날짜: 2026/08/29
    """

    def _copy_lock_derived_fixture(self, fixture_root: Path) -> None:
        """
        함수 이름: _copy_lock_derived_fixture()
        기능: Lockfile-derived 산출물과 historical binding record를 임시 root에 복사한다.
        인자: fixture_root -> 생성할 임시 repository root
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        relative_paths = [
            *(lockfile_path for lockfile_path, _ecosystem in LOCKFILE_SPECS),
            SBOM_PATH,
            LICENSE_INVENTORY_PATH,
            NOTICE_REVIEW_PATH,
            HISTORICAL_RELEASE_BINDING_PATH,
        ]

        # Ignore된 app/DMG binary는 복사하지 않고 retained binding JSON만 검증한다.
        for relative_path in relative_paths:
            destination_path = fixture_root / relative_path
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPOSITORY_ROOT / relative_path, destination_path)

    def _assert_bounded_path_reader_rejects_races(
        self,
        bounded_reader: Callable[[Path, int], bytes],
    ) -> None:
        """
        함수 이름: _assert_bounded_path_reader_rejects_races()
        기능: 주입된 local reader에 symlink·path 교체·read 중 증가 거부를 공통 검증한다.
        인자: bounded_reader -> path와 최대 byte를 받는 reader
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_root = Path(temporary_directory).resolve()
            target_path = fixture_root / "target.bin"
            symlink_path = fixture_root / "symlink.bin"

            # 정상 regular file을 가리키는 final symlink도 열지 않는다.
            target_path.write_bytes(b"trusted")
            symlink_path.symlink_to(target_path.name)
            with self.assertRaisesRegex(LocalSupplyArtifactError, "path is invalid"):
                bounded_reader(symlink_path, 16)

            # 열기 전부터 max를 초과한 regular file은 bounded read를 시작하지 않는다.
            oversized_path = fixture_root / "oversized.bin"
            oversized_path.write_bytes(b"O" * 17)
            with self.assertRaisesRegex(LocalSupplyArtifactError, "size is invalid"):
                bounded_reader(oversized_path, 16)

            # FIFO leaf도 O_NONBLOCK으로 열려 writer를 기다리지 않고 즉시 거부되어야 한다.
            fifo_path = fixture_root / "artifact.fifo"
            os.mkfifo(fifo_path)
            with self.assertRaisesRegex(LocalSupplyArtifactError, "regular file"):
                bounded_reader(fifo_path, 16)

        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_root = Path(temporary_directory).resolve()
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
                기능: 첫 fd read 후 local path를 다른 inode로 교체하는 race를 재현한다.
                인자: file_descriptor -> reader fd, maximum_bytes -> 요청 read 크기
                반환값: 원래 os.read()의 bytes
                작성 날짜: 2026/08/31
                """
                nonlocal replacement_completed
                file_chunk = original_os_read(file_descriptor, maximum_bytes)

                # 열린 fd는 유지하고 path만 새 inode로 바꾸어 post-check를 검증한다.
                if not replacement_completed:
                    os.replace(replacement_path, artifact_path)
                    replacement_completed = True
                return file_chunk

            with patch(
                "scripts.phase13_local_supply_artifacts.os.read",
                side_effect=replace_path_after_read,
            ), self.assertRaisesRegex(LocalSupplyArtifactError, "changed"):
                bounded_reader(artifact_path, 16)

        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_root = Path(temporary_directory).resolve()
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
                기능: 첫 fd read 후 local file을 증가시켜 max+1 bounded 경계를 재현한다.
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
                "scripts.phase13_local_supply_artifacts.os.read",
                side_effect=grow_path_after_read,
            ), self.assertRaisesRegex(LocalSupplyArtifactError, "changed"):
                bounded_reader(artifact_path, 16)

    def test_repository_and_metadata_readers_fail_closed_on_file_races(self) -> None:
        """
        함수 이름: test_repository_and_metadata_readers_fail_closed_on_file_races()
        기능: Repository artifact와 package metadata reader 모두 같은 race 규약을 지키는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        def read_repository_artifact(
            artifact_path: Path,
            maximum_bytes: int,
        ) -> bytes:
            """
            함수 이름: read_repository_artifact()
            기능: 공통 path-reader fixture를 repository reader 인자 형식으로 변환한다.
            인자: artifact_path -> test artifact, maximum_bytes -> 최대 byte
            반환값: Repository reader가 읽은 bytes
            작성 날짜: 2026/08/31
            """
            # Test artifact의 parent를 repository root로, file name을 상대 경로로 전달한다.
            return _read_regular_bytes(
                artifact_path.parent,
                Path(artifact_path.name),
                maximum_bytes,
            )

        reader_specs = (
            ("repository", read_repository_artifact),
            ("metadata", _read_local_metadata_bytes),
        )

        # 두 reader를 동일한 negative fixture에 순차적으로 적용한다.
        for reader_name, bounded_reader in reader_specs:
            with self.subTest(reader_name=reader_name):
                self._assert_bounded_path_reader_rejects_races(bounded_reader)

    def test_same_size_metadata_drift_is_rejected(self) -> None:
        """
        함수 이름: test_same_size_metadata_drift_is_rejected()
        기능: Metadata bytes가 같은 길이로 바뀌어도 mtime·ctime drift로 거부되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_root = Path(temporary_directory).resolve()
            metadata_path = fixture_root / "METADATA"
            metadata_path.write_bytes(b"A" * 16)
            original_status = metadata_path.stat()
            original_os_read = os.read
            metadata_changed = False

            def rewrite_metadata_after_read(
                file_descriptor: int,
                maximum_bytes: int,
            ) -> bytes:
                """
                함수 이름: rewrite_metadata_after_read()
                기능: 첫 read 뒤 같은 inode의 metadata를 동일 길이 bytes로 다시 쓴다.
                인자: file_descriptor -> reader fd, maximum_bytes -> 요청 read 크기
                반환값: 원래 os.read()의 bytes
                작성 날짜: 2026/08/31
                """
                nonlocal metadata_changed
                file_chunk = original_os_read(file_descriptor, maximum_bytes)

                # 길이는 유지하되 mtime을 명시적으로 이동해 content drift를 안정적으로 재현한다.
                if not metadata_changed:
                    metadata_path.write_bytes(b"B" * 16)
                    os.utime(
                        metadata_path,
                        ns=(
                            original_status.st_atime_ns,
                            original_status.st_mtime_ns + 1_000_000_000,
                        ),
                    )
                    metadata_changed = True
                return file_chunk

            with patch(
                "scripts.phase13_local_supply_artifacts.os.read",
                side_effect=rewrite_metadata_after_read,
            ), self.assertRaisesRegex(LocalSupplyArtifactError, "changed"):
                _read_local_metadata_bytes(metadata_path, 16)

    def test_historical_app_replacement_and_size_caps_fail_closed(self) -> None:
        """
        함수 이름: test_historical_app_replacement_and_size_caps_fail_closed()
        기능: App file 교체와 per-file·total byte 상한 위반을 모두 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_root = Path(temporary_directory).resolve()
            app_path = fixture_root / "Historical.app"
            contents_path = app_path / "Contents"
            contents_path.mkdir(parents=True)
            executable_path = contents_path / "binary"
            replacement_path = contents_path / "replacement"
            executable_path.write_bytes(b"A" * 16)
            replacement_path.write_bytes(b"B" * 16)
            original_os_read = os.read
            file_replaced = False

            def replace_app_file_after_read(
                file_descriptor: int,
                maximum_bytes: int,
            ) -> bytes:
                """
                함수 이름: replace_app_file_after_read()
                기능: 첫 streaming read 뒤 app file path를 같은 크기의 다른 inode로 교체한다.
                인자: file_descriptor -> app file fd, maximum_bytes -> 요청 read 크기
                반환값: 원래 os.read()의 bytes
                작성 날짜: 2026/08/31
                """
                nonlocal file_replaced
                file_chunk = original_os_read(file_descriptor, maximum_bytes)

                # 열린 fd와 path의 inode를 갈라 post identity 검사를 직접 실행한다.
                if not file_replaced:
                    os.replace(replacement_path, executable_path)
                    file_replaced = True
                return file_chunk

            with patch(
                "scripts.phase13_local_supply_artifacts.os.read",
                side_effect=replace_app_file_after_read,
            ), self.assertRaisesRegex(LocalSupplyArtifactError, "changed"):
                _calculate_app_content_tree(fixture_root, Path("Historical.app"))

        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_root = Path(temporary_directory).resolve()
            app_path = fixture_root / "Historical.app"
            app_path.mkdir()
            (app_path / "first.bin").write_bytes(b"1234")
            (app_path / "second.bin").write_bytes(b"5678")

            # 개별 file과 app 전체 상한을 각각 낮춰 두 독립 cap을 검증한다.
            with patch(
                "scripts.phase13_local_supply_artifacts.MAXIMUM_APP_FILE_BYTES",
                3,
            ), self.assertRaisesRegex(LocalSupplyArtifactError, "too large"):
                _calculate_app_content_tree(fixture_root, Path("Historical.app"))
            with patch(
                "scripts.phase13_local_supply_artifacts.MAXIMUM_APP_TOTAL_BYTES",
                7,
            ), self.assertRaisesRegex(LocalSupplyArtifactError, "too large"):
                _calculate_app_content_tree(fixture_root, Path("Historical.app"))

    def test_repository_artifacts_cover_all_exact_coordinates(self) -> None:
        """
        함수 이름: test_repository_artifacts_cover_all_exact_coordinates()
        기능: 세 lockfile 868개와 SBOM/license inventory가 1:1인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # Repository lockfile에서 재생성한 세 산출물의 집계를 함께 대조한다.
        components = read_exact_lockfile_components()
        sbom = build_exact_lockfile_sbom()
        # License inventory도 같은 exact lockfile scope에서 재생성한다.
        license_inventory = build_dependency_license_inventory()
        summary = validate_local_supply_artifacts()

        self.assertEqual(len(components), 868)
        self.assertEqual(len(sbom["components"]), 868)
        self.assertEqual(len(license_inventory["packages"]), 868)
        self.assertEqual(summary.component_count, 868)
        self.assertEqual(summary.third_party_component_count, 866)
        self.assertEqual(summary.license_declaration_count, 494)

        # SBOM과 license inventory는 같은 bom-ref/coordinate set을 공유해 scope 누락을 막는다.
        sbom_references = {
            component["bom-ref"] for component in sbom["components"]
        }
        inventory_references = {
            package["bom_ref"] for package in license_inventory["packages"]
        }
        self.assertEqual(sbom_references, inventory_references)

    def test_license_and_notice_remain_explicitly_incomplete(self) -> None:
        """
        함수 이름: test_license_and_notice_remain_explicitly_incomplete()
        기능: 미확인 third-party license가 PASS나 완성 NOTICE로 승격되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # 미확인 license 수치와 notice의 NO_GO 문구를 함께 고정한다.
        license_inventory = build_dependency_license_inventory()
        inventory_summary = license_inventory["summary"]
        notice_review = build_third_party_notice_review()

        self.assertEqual(inventory_summary["third_party_noassertion_count"], 372)
        self.assertEqual(
            inventory_summary["third_party_license_declaration_observed_count"],
            494,
        )
        self.assertFalse(inventory_summary["license_metadata_complete"])
        self.assertTrue(inventory_summary["manual_review_required"])
        self.assertIn("NOT RELEASE-READY", notice_review)
        self.assertIn("Status: **NO_GO", notice_review)
        self.assertIn("Third-party `NOASSERTION`: 372", notice_review)

    def test_historical_release_record_cannot_claim_phase13_candidate(self) -> None:
        """
        함수 이름: test_historical_release_record_cannot_claim_phase13_candidate()
        기능: Phase 12 app/DMG digest가 current Phase 13 release 결속으로 오인되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # Checked-in historical binding의 분류·provenance flag·DMG digest를 직접 검증한다.
        binding = json.loads(
            (REPOSITORY_ROOT / HISTORICAL_RELEASE_BINDING_PATH).read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(binding["classification"], "HISTORICAL_PHASE12_LOCAL_FIXED")
        self.assertFalse(binding["current_phase13_release_candidate"])
        self.assertFalse(binding["current_phase13_source_provenance_bound"])
        self.assertFalse(binding["exact_lockfile_build_provenance_bound"])
        self.assertEqual(binding["dmg"]["sha256"], EXPECTED_HISTORICAL_DMG_SHA256)

    def test_lockfile_byte_drift_rejects_stale_generated_artifacts(self) -> None:
        """
        함수 이름: test_lockfile_byte_drift_rejects_stale_generated_artifacts()
        기능: Semantic package graph이 같아도 lockfile byte drift에 stale SBOM을 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_root = Path(temporary_directory)
            self._copy_lock_derived_fixture(fixture_root)
            python_lockfile = fixture_root / LOCKFILE_SPECS[0][0]

            # TOML의 package 의미는 같지만 exact byte hash가 바뀌 stale 산출물을 만든다.
            python_lockfile.write_bytes(python_lockfile.read_bytes() + b"\n")

            with self.assertRaisesRegex(LocalSupplyArtifactError, "drifted"):
                validate_local_supply_artifacts(fixture_root)

    def test_notice_byte_tampering_is_rejected(self) -> None:
        """
        함수 이름: test_notice_byte_tampering_is_rejected()
        기능: Notice 검토본의 한 byte 변조도 canonical 재생성 대조에서 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_root = Path(temporary_directory)
            self._copy_lock_derived_fixture(fixture_root)
            notice_path = fixture_root / NOTICE_REVIEW_PATH

            # Trailing whitespace도 checked-in canonical bytes와 다르므로 fail closed한다.
            notice_path.write_bytes(notice_path.read_bytes() + b" ")

            with self.assertRaisesRegex(LocalSupplyArtifactError, "drifted"):
                validate_local_supply_artifacts(fixture_root)


if __name__ == "__main__":
    unittest.main()
