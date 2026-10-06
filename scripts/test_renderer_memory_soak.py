"""격리 renderer 검증 runner의 부분 기록·메모리 상한을 외부 앱 없이 검사한다."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.run_renderer_memory_soak import SoakObservations, read_new_records


class RendererMemorySoakTests(unittest.TestCase):
    """
    클래스 이름: RendererMemorySoakTests
    기능: 부분 파일 복구와 잘못된 메모리 판정을 방지하는 회귀 검증이다.
    작성 날짜: 2026/10/04
    """

    def test_partial_record_is_retried_without_replaying_complete_rows(self):
        """
        함수 이름: test_partial_record_is_retried_without_replaying_complete_rows()
        기능: 아직 쓰는 중인 행을 버리지 않고 완성된 이전 행은 중복 처리하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/10/04
        """
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "runtime_part0001.log"
            path.write_bytes(b'{"sequence":1}\n{"sequence":')
            offsets = {}
            self.assertEqual(list(read_new_records(directory, offsets)), [{"sequence": 1}])
            with path.open("ab") as stream:
                stream.write(b'2}\n')
            self.assertEqual(list(read_new_records(directory, offsets)), [{"sequence": 2}])

    def test_memory_guard_uses_renderer_private_commit_not_gpu_or_working_set(self):
        """
        함수 이름: test_memory_guard_uses_renderer_private_commit_not_gpu_or_working_set()
        기능: renderer별 전용 commit만 상한에 적용하고 PID 재사용은 별개로 요약한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/10/04
        """
        observations = SoakObservations(100)
        sample = {"status": "ok", "process_id": 10, "kind": "renderer",
                  "private_commit_bytes": 90, "working_set_bytes": 200, "creation_filetime_100ns": 1}
        observations.observe({"event": "runtime_sample", "resources": {"webview2": {"processes": [
            sample, {**sample, "kind": "gpu", "private_commit_bytes": 300},
        ]}}})
        self.assertIsNone(observations.stop_reason)
        observations.observe({"event": "runtime_sample", "resources": {"webview2": {"processes": [
            {**sample, "private_commit_bytes": 101, "creation_filetime_100ns": 2},
        ]}}})
        self.assertEqual(observations.stop_reason, "renderer_private_commit_limit")
        self.assertEqual(len(observations.processes), 3)


if __name__ == "__main__":
    unittest.main()
