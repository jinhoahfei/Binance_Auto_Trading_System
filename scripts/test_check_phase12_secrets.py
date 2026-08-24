"""Phase 12 secret scanner의 fail-closed target 경계와 chunk 검사를 검증한다."""

from pathlib import Path
import tempfile
import unittest

from scripts.check_phase12_secrets import (
    SCAN_CHUNK_SIZE,
    file_contains_canary,
    iter_regular_files,
)


class PhaseTwelveSecretScannerTests(unittest.TestCase):
    """
    클래스 이름: PhaseTwelveSecretScannerTests
    기능: 잘못된 target의 false PASS와 binary chunk 경계 누출 회귀를 차단한다.
    작성 날짜: 2026/08/24
    """

    def test_missing_explicit_target_fails_closed(self) -> None:
        """
        함수 이름: test_missing_explicit_target_fails_closed()
        기능: 존재하지 않는 산출물 경로가 빈 iterator로 성공하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            missing_target = temporary_path / "missing.app"
            env_path = temporary_path / ".env"

            # Generator를 실제 소비해 target validation이 실행되는 지점을 고정한다.
            with self.assertRaisesRegex(RuntimeError, "존재하지 않거나 읽을 수 없습니다"):
                tuple(iter_regular_files(missing_target, env_path))

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "requires symlink support")
    def test_explicit_symlink_target_fails_closed(self) -> None:
        """
        함수 이름: test_explicit_symlink_target_fails_closed()
        기능: 검사 root를 다른 위치로 바꾸는 symlink target을 명시적으로 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            real_target = temporary_path / "real-artifact"
            real_target.mkdir()
            symlink_target = temporary_path / "artifact-link"
            symlink_target.symlink_to(real_target, target_is_directory=True)

            # Root symlink는 내부 dependency symlink skip과 달리 운영자 입력 오류로 처리한다.
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                tuple(iter_regular_files(symlink_target, temporary_path / ".env"))

    def test_binary_canary_crossing_chunk_boundary_is_detected(self) -> None:
        """
        함수 이름: test_binary_canary_crossing_chunk_boundary_is_detected()
        기능: credential bytes가 두 read chunk에 걸쳐 있어도 exact match를 찾는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        credential_canary = b"phase12-secret-canary"
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact_path = Path(temporary_directory) / "artifact.bin"
            prefix = b"x" * (SCAN_CHUNK_SIZE - 5)
            artifact_path.write_bytes(prefix + credential_canary + b"suffix")

            self.assertTrue(
                file_contains_canary(artifact_path, credential_canary)
            )  # 첫 chunk의 마지막 5 byte와 다음 chunk를 overlap해 탐지한다.


if __name__ == "__main__":
    unittest.main()  # 단독 실행과 unittest discovery가 같은 검증 집합을 사용한다.
