"""Phase 12 history shutdown barrier의 file·directory fsync 의무를 검증한다."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from binance_auto_trader.adapters.persistence import TradeHistoryRepository


class ShutdownDurabilityTests(unittest.TestCase):
    """
    클래스 이름: ShutdownDurabilityTests
    기능: 기존 history 파일도 종료 시 parent directory durability를 재확인하는지 검증한다.
    작성 날짜: 2026/08/24
    """

    def test_preexisting_history_always_fsyncs_parent_directory(self) -> None:
        """
        함수 이름: test_preexisting_history_always_fsyncs_parent_directory()
        기능: 첫 append가 이미 끝난 파일에서도 shutdown barrier가 directory fsync를 생략하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "history.jsonl"
            history_path.write_bytes(b"")  # Shutdown 호출 전부터 존재하는 directory entry를 만든다.
            repository = TradeHistoryRepository(history_path)

            with patch(
                "binance_auto_trader.adapters.persistence.trade_history_repository._fsync_parent_directory"
            ) as fsync_parent_directory:
                repository.flush_durable_state()

            fsync_parent_directory.assert_called_once_with(history_path)


if __name__ == "__main__":
    unittest.main()
