"""TradeHistoryRepository의 fsync와 독립 reader 가시성을 검증한다."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from binance_auto_trader.adapters.persistence.trade_history_repository import (
    TradeHistoryRepository,
)

from tests.unit.history.factories import make_trade


class RepositoryCrossInstanceDurabilityTests(unittest.TestCase):
    """
    클래스 이름: RepositoryCrossInstanceDurabilityTests
    기능: writer의 durable append를 독립 Repository instance가 같은 내용으로 복원하는지 검증한다.
    작성 날짜: 2026/08/22
    """

    def test_successful_fsync_is_visible_to_fresh_repository_reader(self) -> None:
        """
        함수 이름: test_successful_fsync_is_visible_to_fresh_repository_reader()
        기능: writer가 fsync 성공을 반환한 뒤 새 reader가 정확히 한 Trade를 읽는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        with TemporaryDirectory() as temporary_directory:
            storage_path = Path(temporary_directory) / "trades.jsonl"
            writer = TradeHistoryRepository(storage_path)
            trade = make_trade(trade_id="durable-trade", order_id="8201")
            original_fsync = os.fsync

            # 실제 file fsync 호출을 보존하면서 성공 반환 전 실행 여부를 검증한다.
            with patch(
                "binance_auto_trader.adapters.persistence."
                "trade_history_repository.os.fsync",
                wraps=original_fsync,
            ) as fsync_mock:
                writer.save_this_trade_by_order_id(trade.order_id, trade)

            fresh_reader = TradeHistoryRepository(storage_path)
            self.assertEqual(1, fsync_mock.call_count)
            self.assertEqual((trade,), fresh_reader.get_trade_history())
            self.assertEqual(frozenset({"8201"}), fresh_reader.loaded_order_ids)
            self.assertEqual(1, storage_path.read_bytes().count(b"\n"))

    def test_fresh_reader_fsyncs_line_after_writer_fsync_failure(
        self,
    ) -> None:
        """
        함수 이름: test_fresh_reader_fsyncs_line_after_writer_fsync_failure()
        기능: writer가 남긴 uncertain line을 독립 reader가 fsync한 뒤 중복 없이 복원한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        with TemporaryDirectory() as temporary_directory:
            storage_path = Path(temporary_directory) / "trades.jsonl"
            writer = TradeHistoryRepository(storage_path)
            trade = make_trade(trade_id="retried-trade", order_id="8202")

            # write·flush 후 fsync만 실패시켜 writer의 uncertain-order 경계를 만든다.
            with patch(
                "binance_auto_trader.adapters.persistence."
                "trade_history_repository.os.fsync",
                side_effect=OSError("controlled fsync failure"),
            ):
                with self.assertRaisesRegex(OSError, "controlled fsync failure"):
                    writer.save_this_trade_by_order_id(trade.order_id, trade)

            fresh_reader = TradeHistoryRepository(storage_path)
            original_fsync = os.fsync

            # 새 process 역할의 reader가 보이는 file bytes를 다시 fsync해 durable로 확정한다.
            with patch(
                "binance_auto_trader.adapters.persistence."
                "trade_history_repository.os.fsync",
                wraps=original_fsync,
            ) as fsync_mock:
                restored_history = fresh_reader.get_trade_history()

            self.assertEqual(1, fsync_mock.call_count)
            self.assertEqual((trade,), restored_history)
            self.assertEqual(frozenset({"8202"}), fresh_reader.loaded_order_ids)
            self.assertEqual(1, storage_path.read_bytes().count(b"\n"))


if __name__ == "__main__":
    unittest.main()
