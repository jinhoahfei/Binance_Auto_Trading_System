"""Phase 13 actual browser visual gate의 artifact binding과 fail-closed 동작을 검증한다."""

from __future__ import annotations

from contextlib import redirect_stderr
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.check_phase13_visual_regression import (
    BASELINE_MANIFEST_PATH,
    COMPARISON_POLICY_PATH,
    CURRENT_MANIFEST_PATH,
    EXPECTED_FRAME_KEYS,
    REPOSITORY_ROOT,
    VisualRegressionError,
    _read_bounded_regular_file,
    _read_jpeg_dimensions,
    _write_verified_image_copy,
    parse_arguments,
    run_visual_regression_gate,
)


class Phase13VisualRegressionGateTests(unittest.TestCase):
    """
    클래스 이름: Phase13VisualRegressionGateTests
    기능: JPEG capture, FFmpeg SSIM threshold와 baseline 불변 정책을 검증한다.
    작성 날짜: 2026/08/29
    """

    def _build_png_header(self) -> bytes:
        """
        함수 이름: _build_png_header()
        기능: Gate의 framing·dimension 검사에 필요한 1440×1024 PNG header를 만든다.
        인자: 없음
        반환값: Minimal PNG header bytes
        작성 날짜: 2026/08/29
        """
        return (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR"
            + (1440).to_bytes(4, "big")
            + (1024).to_bytes(4, "big")
        )

    def _build_jpeg_fixture(self) -> bytes:
        """
        함수 이름: _build_jpeg_fixture()
        기능: 8-bit 3-component 1440×1024 baseline JPEG/JFIF framing을 만든다.
        인자: 없음
        반환값: Minimal JPEG marker bytes
        작성 날짜: 2026/08/29
        """
        app_zero_data = b"JFIF\x00" + b"\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        start_of_frame_data = (
            b"\x08"
            + (1024).to_bytes(2, "big")
            + (1440).to_bytes(2, "big")
            + b"\x03"
            + b"\x01\x11\x00\x02\x11\x00\x03\x11\x00"
        )
        return (
            b"\xff\xd8"
            + b"\xff\xe0"
            + (16).to_bytes(2, "big")
            + app_zero_data
            + b"\xff\xc0"
            + (17).to_bytes(2, "big")
            + start_of_frame_data
            + b"\xff\xd9"
        )

    def _write_fake_ffmpeg(
        self,
        executable_path: Path,
        *,
        score: str = "0.990000",
        version: str = "8.0 test",
    ) -> None:
        """
        함수 이름: _write_fake_ffmpeg()
        기능: Version과 고정 SSIM 한 건만 출력하는 local fake executable을 만든다.
        인자: executable_path -> 생성할 path, score -> SSIM All, version -> version suffix
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        executable_path.write_text(
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = \"-version\" ]; then\n"
            f"    echo \"ffmpeg version {version}\"\n"
            "    exit 0\n"
            "fi\n"
            f"echo \"[Parsed_ssim] All:{score} (20.000000)\" >&2\n"
            "exit 0\n",
            encoding="utf-8",
        )
        executable_path.chmod(0o755)

    def _prepare_fixture_repository(
        self,
        fixture_root: Path,
    ) -> tuple[Path, Path]:
        """
        함수 이름: _prepare_fixture_repository()
        기능: 16개 baseline/current와 strict manifests를 임시 repository에 만든다.
        인자: fixture_root -> 임시 repository root
        반환값: current directory와 fake FFmpeg path
        작성 날짜: 2026/08/29
        """
        visual_directory = fixture_root / "UI" / "visual-regression"
        baseline_directory = visual_directory / "figma"
        current_directory = visual_directory / "current"
        baseline_directory.mkdir(parents=True)
        current_directory.mkdir(parents=True)
        shutil.copyfile(
            REPOSITORY_ROOT / COMPARISON_POLICY_PATH,
            fixture_root / COMPARISON_POLICY_PATH,
        )
        png_bytes = self._build_png_header()
        jpeg_bytes = self._build_jpeg_fixture()
        baseline_frames = []
        current_frames = []

        # 모든 frame은 이름만 다르고 동일한 minimal image framing으로 artifact 검사를 재현한다.
        for frame_index, frame_key in enumerate(EXPECTED_FRAME_KEYS, start=1):
            file_stem = f"{frame_index:02d}-{frame_key}"
            reference_file = f"{file_stem}.png"
            current_file = f"{file_stem}.jpg"
            (baseline_directory / reference_file).write_bytes(png_bytes)
            (current_directory / current_file).write_bytes(jpeg_bytes)
            baseline_frames.append(
                {
                    "frame_key": frame_key,
                    "reference_file": reference_file,
                    "sha256": hashlib.sha256(png_bytes).hexdigest(),
                }
            )
            current_frames.append(
                {
                    "frame_key": frame_key,
                    "current_file": current_file,
                    "sha256": hashlib.sha256(jpeg_bytes).hexdigest(),
                }
            )
        viewport = {"width": 1440, "height": 1024, "device_scale_factor": 1}
        clip = {"x": 0, "y": 0, "width": 1440, "height": 1024}
        baseline_manifest = {
            "schema_version": 1,
            "source": {
                "kind": "figma_export",
                "file_key": "kzt9vOXj0QmPeYxO8SVPax",
                "captured_on": "2026-08-12",
            },
            "viewport": viewport,
            "frames": baseline_frames,
        }
        current_manifest = {
            "schema_version": 1,
            "source": {
                "kind": "actual_browser_capture",
                "capture_method": "in_app_browser_explicit_clip",
                "captured_on": "2026-08-29",
            },
            "viewport": viewport,
            "clip": clip,
            "image_format": "jpeg_jfif",
            "frames": current_frames,
        }

        # Canonical JSON은 production parser가 검증하는 manifests와 같은 key 구조를 유지한다.
        (fixture_root / BASELINE_MANIFEST_PATH).write_text(
            json.dumps(baseline_manifest, indent=2) + "\n",
            encoding="utf-8",
        )
        (fixture_root / CURRENT_MANIFEST_PATH).write_text(
            json.dumps(current_manifest, indent=2) + "\n",
            encoding="utf-8",
        )
        fake_ffmpeg = fixture_root / "ffmpeg"
        self._write_fake_ffmpeg(fake_ffmpeg)
        return current_directory, fake_ffmpeg

    def test_policy_freezes_threshold_normalization_and_no_update_option(self) -> None:
        """
        함수 이름: test_policy_freezes_threshold_normalization_and_no_update_option()
        기능: 0.98·대칭 blur·JPEG 계약과 baseline update CLI 부재를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        policy = json.loads(
            (REPOSITORY_ROOT / COMPARISON_POLICY_PATH).read_text(encoding="utf-8")
        )
        self.assertEqual(
            policy["comparison_contract"]["minimum_score"],
            "0.980000",
        )
        self.assertTrue(
            policy["comparison_contract"]["anti_alias_normalization"][
                "apply_symmetrically"
            ]
        )
        self.assertFalse(
            policy["capture_contract"]["automatic_baseline_update"]
        )

        # Parser에 update option 자체가 없어 기준 이미지 자동 재생성 요청을 거부해야 한다.
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parse_arguments(["--update-baselines"])

    def test_exact_sixteen_scores_at_or_above_threshold_pass(self) -> None:
        """
        함수 이름: test_exact_sixteen_scores_at_or_above_threshold_pass()
        기능: Bound artifact 16개가 모두 threshold 이상일 때만 전부 PASS인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # 16개 exact manifest와 고정 score executable을 함께 만들어 전체 PASS 경계를 확인한다.
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_root = Path(temporary_directory)
            current_directory, fake_ffmpeg = self._prepare_fixture_repository(
                fixture_root
            )
            results = run_visual_regression_gate(
                repository_root=fixture_root,
                current_directory=current_directory,
                ffmpeg_path=fake_ffmpeg,
            )

            self.assertEqual(len(results), 16)
            self.assertTrue(all(result.passed for result in results))

    def test_score_below_threshold_remains_no_go(self) -> None:
        """
        함수 이름: test_score_below_threshold_remains_no_go()
        기능: Anti-alias normalization 뒤 0.98 미만인 frame을 완화하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # 동일 artifact에서도 threshold 바로 아래 score는 모든 frame을 NO_GO로 유지해야 한다.
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_root = Path(temporary_directory)
            current_directory, fake_ffmpeg = self._prepare_fixture_repository(
                fixture_root
            )
            self._write_fake_ffmpeg(fake_ffmpeg, score="0.979999")
            results = run_visual_regression_gate(
                repository_root=fixture_root,
                current_directory=current_directory,
                ffmpeg_path=fake_ffmpeg,
            )

            self.assertTrue(all(not result.passed for result in results))

    def test_missing_current_capture_fails_closed(self) -> None:
        """
        함수 이름: test_missing_current_capture_fails_closed()
        기능: Current JPEG 하나가 누락되면 부분 comparison을 시작하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_root = Path(temporary_directory)
            current_directory, fake_ffmpeg = self._prepare_fixture_repository(
                fixture_root
            )
            first_capture = next(iter(sorted(current_directory.iterdir())))
            first_capture.unlink()

            # Exact file-set 검증이 빠진 screenshot을 평균 SSIM으로 숨기지 않아야 한다.
            with self.assertRaisesRegex(VisualRegressionError, "incomplete"):
                run_visual_regression_gate(
                    repository_root=fixture_root,
                    current_directory=current_directory,
                    ffmpeg_path=fake_ffmpeg,
                )

    def test_jpeg_jfif_and_digest_are_required(self) -> None:
        """
        함수 이름: test_jpeg_jfif_and_digest_are_required()
        기능: PNG masquerade와 current byte 교체를 FFmpeg 실행 전에 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # PNG masquerade는 manifest나 FFmpeg 실행 전에 JPEG framing 경계에서 거부한다.
        with self.assertRaisesRegex(VisualRegressionError, "JPEG/JFIF"):
            _read_jpeg_dimensions(self._build_png_header())

        # Valid fixture 한 장의 bytes만 바꿔 current manifest digest 결속을 직접 검증한다.
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_root = Path(temporary_directory)
            current_directory, fake_ffmpeg = self._prepare_fixture_repository(
                fixture_root
            )
            first_capture = next(iter(sorted(current_directory.iterdir())))
            first_capture.write_bytes(first_capture.read_bytes() + b"drift")

            with self.assertRaisesRegex(VisualRegressionError, "digest"):
                run_visual_regression_gate(
                    repository_root=fixture_root,
                    current_directory=current_directory,
                    ffmpeg_path=fake_ffmpeg,
                )

    def test_ffmpeg_version_mismatch_fails_closed(self) -> None:
        """
        함수 이름: test_ffmpeg_version_mismatch_fails_closed()
        기능: 다른 또는 누락된 FFmpeg를 동일 SSIM evidence로 수용하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # 동일 score를 출력해도 승인하지 않은 FFmpeg version은 comparison 전에 차단한다.
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_root = Path(temporary_directory)
            current_directory, fake_ffmpeg = self._prepare_fixture_repository(
                fixture_root
            )
            self._write_fake_ffmpeg(fake_ffmpeg, version="7.1 test")

            with self.assertRaisesRegex(VisualRegressionError, "version"):
                run_visual_regression_gate(
                    repository_root=fixture_root,
                    current_directory=current_directory,
                    ffmpeg_path=fake_ffmpeg,
                )

    def test_bounded_reader_rejects_symlink_oversize_and_mid_read_change(
        self,
    ) -> None:
        """
        함수 이름: test_bounded_reader_rejects_symlink_oversize_and_mid_read_change()
        기능: no-follow, max+1 bound와 descriptor post-stat 변경 감지를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            regular_path = temporary_path / "visual.bin"
            regular_path.write_bytes(b"bound")
            symlink_path = temporary_path / "visual-link.bin"
            symlink_path.symlink_to(regular_path)

            # Symlink와 maximum+1 bytes는 descriptor read 전·중 경계에서 각각 거부한다.
            with self.assertRaises(VisualRegressionError):
                _read_bounded_regular_file(temporary_path, Path("visual-link.bin"), 5)
            with self.assertRaisesRegex(VisualRegressionError, "size"):
                _read_bounded_regular_file(temporary_path, Path("visual.bin"), 4)

            # FIFO leaf도 O_NONBLOCK으로 열려 writer를 기다리지 않고 regular-file 경계에서 거부한다.
            fifo_path = temporary_path / "visual.fifo"
            os.mkfifo(fifo_path)
            with self.assertRaisesRegex(VisualRegressionError, "regular file"):
                _read_bounded_regular_file(
                    temporary_path,
                    Path("visual.fifo"),
                    5,
                )

            # 두 번째 fstat에서 metadata가 바뀐 것처럼 보이면 읽은 bytes도 승격하지 않는다.
            real_fstat = os.fstat
            fstat_call_count = 0

            def changed_fstat(file_descriptor: int) -> os.stat_result:
                """
                함수 이름: changed_fstat()
                기능: 두 번째 descriptor 상태의 mtime을 변조해 read 경합을 재현한다.
                인자: file_descriptor -> 실제 open regular file descriptor
                반환값: 최초 또는 mtime을 바꾼 os.stat_result
                작성 날짜: 2026/08/31
                """
                nonlocal fstat_call_count
                current_status = real_fstat(file_descriptor)
                fstat_call_count += 1
                if fstat_call_count != 2:
                    return current_status

                # stat_result tuple의 mtime도 변경해 post-read metadata drift를 만든다.
                changed_values = list(current_status)
                changed_values[stat.ST_MTIME] = current_status.st_mtime + 1
                return os.stat_result(changed_values)

            with patch(
                "scripts.check_phase13_visual_regression.os.fstat",
                side_effect=changed_fstat,
            ), self.assertRaisesRegex(VisualRegressionError, "changed"):
                _read_bounded_regular_file(temporary_path, Path("visual.bin"), 5)

    def test_verified_copy_rejects_existing_file_and_symlink_collision(self) -> None:
        """
        함수 이름: test_verified_copy_rejects_existing_file_and_symlink_collision()
        기능: 0600 O_EXCL copy가 기존 file이나 symlink target을 덮어쓰지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)

            # 첫 owner-only copy 뒤 같은 basename 재사용은 기존 bytes를 보존하며 실패해야 한다.
            verified_path = _write_verified_image_copy(
                temporary_path,
                "verified.png",
                b"verified",
            )
            self.assertEqual(0o600, stat.S_IMODE(verified_path.stat().st_mode))
            with self.assertRaisesRegex(VisualRegressionError, "created"):
                _write_verified_image_copy(
                    temporary_path,
                    "verified.png",
                    b"replacement",
                )
            self.assertEqual(b"verified", verified_path.read_bytes())

            # Existing symlink basename도 O_EXCL에서 거부되어 외부 target bytes를 바꾸지 않는다.
            target_path = temporary_path / "target.bin"
            target_path.write_bytes(b"target")
            symlink_path = temporary_path / "verified-link.png"
            symlink_path.symlink_to(target_path.name)
            with self.assertRaisesRegex(VisualRegressionError, "created"):
                _write_verified_image_copy(
                    temporary_path,
                    symlink_path.name,
                    b"replacement",
                )
            self.assertEqual(b"target", target_path.read_bytes())

    def test_comparison_exception_removes_verified_temporary_inputs(self) -> None:
        """
        함수 이름: test_comparison_exception_removes_verified_temporary_inputs()
        기능: FFmpeg comparison 예외 뒤에도 검증된 임시 image copy가 모두 삭제되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_root = Path(temporary_directory)
            current_directory, fake_ffmpeg = self._prepare_fixture_repository(
                fixture_root
            )
            observed_copy_paths: list[Path] = []

            def failing_subprocess_run(
                command: list[str],
                **keyword_arguments: object,
            ) -> subprocess.CompletedProcess[str]:
                """
                함수 이름: failing_subprocess_run()
                기능: Version preflight 뒤 첫 comparison에서 고정 예외를 발생시킨다.
                인자: command -> subprocess argv,
                    keyword_arguments -> 사용하지 않는 subprocess keyword
                반환값: Version 호출에서만 CompletedProcess
                작성 날짜: 2026/08/31
                """
                del keyword_arguments  # Raw subprocess 옵션은 failure message에 반사하지 않는다.
                if "-version" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout="ffmpeg version 8.0 test\n",
                        stderr="",
                    )

                # Exception 직전 argv의 두 verified copy를 보존해 context cleanup 뒤 존재를 확인한다.
                observed_copy_paths.extend(
                    Path(command[argument_index + 1])
                    for argument_index, argument in enumerate(command)
                    if argument == "-i"
                )
                raise OSError("fixed comparison failure")

            with patch(
                "scripts.check_phase13_visual_regression.subprocess.run",
                side_effect=failing_subprocess_run,
            ), self.assertRaisesRegex(VisualRegressionError, "comparison failed"):
                run_visual_regression_gate(
                    repository_root=fixture_root,
                    current_directory=current_directory,
                    ffmpeg_path=fake_ffmpeg,
                )
            self.assertEqual(2, len(observed_copy_paths))
            self.assertTrue(
                all(not input_path.exists() for input_path in observed_copy_paths)
            )  # TemporaryDirectory는 comparison failure에서도 owner-only copies를 정리한다.

    def test_source_path_replacement_cannot_change_verified_ffmpeg_inputs(
        self,
    ) -> None:
        """
        함수 이름: test_source_path_replacement_cannot_change_verified_ffmpeg_inputs()
        기능: digest 검증 뒤 original path가 바뀌어도 FFmpeg가 0600 snapshot만 읽는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_root = Path(temporary_directory)
            current_directory, fake_ffmpeg = self._prepare_fixture_repository(
                fixture_root
            )
            source_capture = next(iter(sorted(current_directory.iterdir())))
            expected_png = self._build_png_header()
            expected_jpeg = self._build_jpeg_fixture()
            observed_copy_paths: list[Path] = []
            comparison_count = 0

            def guarded_subprocess_run(
                command: list[str],
                **keyword_arguments: object,
            ) -> subprocess.CompletedProcess[str]:
                """
                함수 이름: guarded_subprocess_run()
                기능: FFmpeg 호출 직전 source를 교체하고 실제 argv snapshot을 검증한다.
                인자: command -> version 또는 comparison exact argv
                    keyword_arguments -> subprocess.run keyword 사본
                반환값: 고정 version 또는 SSIM CompletedProcess
                작성 날짜: 2026/08/31
                """
                nonlocal comparison_count
                del keyword_arguments  # Environment·stderr를 복제하지 않고 argv 경계만 검증한다.
                if "-version" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout="ffmpeg version 8.0 test\n",
                        stderr="",
                    )

                # 첫 comparison 직전 original current path를 바꾸어 TOCTOU 시나리오를 만든다.
                if comparison_count == 0:
                    source_capture.write_bytes(b"replaced-after-validation")
                comparison_count += 1
                input_paths = [
                    Path(command[argument_index + 1])
                    for argument_index, argument in enumerate(command)
                    if argument == "-i"
                ]
                self.assertEqual(2, len(input_paths))
                self.assertNotIn(source_capture, input_paths)
                self.assertEqual(expected_png, input_paths[0].read_bytes())
                self.assertEqual(expected_jpeg, input_paths[1].read_bytes())
                self.assertTrue(
                    all(
                        stat.S_IMODE(input_path.stat().st_mode) == 0o600
                        for input_path in input_paths
                    )
                )
                observed_copy_paths.extend(input_paths)
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout="",
                    stderr="[Parsed_ssim] All:0.990000 (20.000000)",
                )

            # Original path replacement는 검증된 copy를 바꾸지 못하고 임시 파일은 gate 후 삭제된다.
            with patch(
                "scripts.check_phase13_visual_regression.subprocess.run",
                side_effect=guarded_subprocess_run,
            ):
                results = run_visual_regression_gate(
                    repository_root=fixture_root,
                    current_directory=current_directory,
                    ffmpeg_path=fake_ffmpeg,
                )
            self.assertEqual(16, len(results))
            self.assertTrue(all(result.passed for result in results))
            self.assertTrue(observed_copy_paths)
            self.assertTrue(
                all(not input_path.exists() for input_path in observed_copy_paths)
            )

    def test_baseline_source_provenance_drift_fails_closed(self) -> None:
        """
        함수 이름: test_baseline_source_provenance_drift_fails_closed()
        기능: Figma file key가 바뀐 baseline manifest를 같은 image bytes로 수용하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_root = Path(temporary_directory)
            current_directory, fake_ffmpeg = self._prepare_fixture_repository(
                fixture_root
            )
            baseline_manifest_path = fixture_root / BASELINE_MANIFEST_PATH
            baseline_manifest = json.loads(
                baseline_manifest_path.read_text(encoding="utf-8")
            )
            baseline_manifest["source"]["file_key"] = "substituted-file"
            baseline_manifest_path.write_text(
                json.dumps(baseline_manifest, indent=2) + "\n",
                encoding="utf-8",
            )

            # 같은 PNG digest라도 provenance가 다른 기준선은 비교 전에 거부해야 한다.
            with self.assertRaisesRegex(VisualRegressionError, "provenance"):
                run_visual_regression_gate(
                    repository_root=fixture_root,
                    current_directory=current_directory,
                    ffmpeg_path=fake_ffmpeg,
                )


if __name__ == "__main__":
    unittest.main()
