"""Actual Testnet run 사이의 닫힌 durable history baseline 인계를 검증한다."""

from decimal import Decimal
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from binance_auto_trader.adapters.persistence import TradeHistoryRepository
from binance_auto_trader.domain.trading.states import OrderSide

from tests.testnet._support import (
    TESTNET_BASELINE_HISTORY_PATH_ENV,
    build_testnet_order,
    seed_verified_closed_history,
)
from tests.unit.history.factories import make_trade


class TestnetBaselineHistoryTests(unittest.TestCase):
    """
    클래스 이름: TestnetBaselineHistoryTests
    기능: Position 0 history만 새 actual Testnet artifact로 복제되는지 검증한다.
    작성 날짜: 2026/08/24
    """

    def test_missing_baseline_keeps_new_artifact_empty(self) -> None:
        """
        함수 이름: test_missing_baseline_keeps_new_artifact_empty()
        기능: baseline 환경이 없으면 최초 Testnet run의 빈 history 동작을 유지하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with TemporaryDirectory() as temporary_directory:
            destination_path = Path(temporary_directory) / "history.jsonl"
            clean_environment = {
                key: value
                for key, value in os.environ.items()
                if key != TESTNET_BASELINE_HISTORY_PATH_ENV
            }

            # 환경 부재는 파일을 만들거나 임의 외부 주문 provenance를 합성하지 않는다.
            with patch.dict(os.environ, clean_environment, clear=True):
                baseline_trades = seed_verified_closed_history(
                    destination_path
                )

            self.assertEqual((), baseline_trades)
            self.assertFalse(destination_path.exists())

    def test_closed_baseline_is_durably_copied(self) -> None:
        """
        함수 이름: test_closed_baseline_is_durably_copied()
        기능: BUY/SELL로 Position 0인 canonical history를 새 Repository에 그대로 저장하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            source_path = temporary_root / "source.jsonl"
            destination_path = temporary_root / "destination.jsonl"
            source_repository = TradeHistoryRepository(source_path)
            buy_trade = make_trade(
                order_id="7101",
                requested_quantity=Decimal("1"),
                executed_quantity=Decimal("1"),
                executed_amount=Decimal("100"),
                average_fill_price=Decimal("100"),
                fee_amount=Decimal("0"),
                fee_asset="USDT",
                fee_quote_amount=Decimal("0"),
            )
            sell_trade = make_trade(
                order_id="7102",
                side=OrderSide.SELL,
                requested_quantity=Decimal("1"),
                executed_quantity=Decimal("1"),
                executed_amount=Decimal("110"),
                average_fill_price=Decimal("110"),
                fee_amount=Decimal("0"),
                fee_asset="USDT",
                fee_quote_amount=Decimal("0"),
                allocated_cost_basis=Decimal("100"),
                realized_pnl=Decimal("10"),
                realized_return_rate=Decimal("10.00000000"),
            )
            for trade in (buy_trade, sell_trade):
                source_repository.save_this_trade_by_order_id(
                    trade.order_id,
                    trade,
                )

            # Source pending 부재와 Position 0을 검증한 canonical row만 destination에 fsync한다.
            with patch.dict(
                os.environ,
                {TESTNET_BASELINE_HISTORY_PATH_ENV: str(source_path)},
            ):
                baseline_trades = seed_verified_closed_history(
                    destination_path
                )

            self.assertEqual((buy_trade, sell_trade), baseline_trades)
            copied_repository = TradeHistoryRepository(destination_path)
            self.assertEqual(
                baseline_trades,
                copied_repository.get_trade_history(),
            )
            self.assertEqual(
                (),
                copied_repository.get_pending_order_recovery_records(),
            )

    def test_open_baseline_is_rejected_before_destination_write(self) -> None:
        """
        함수 이름: test_open_baseline_is_rejected_before_destination_write()
        기능: BUY만 있는 open Position history가 새 actual run의 안전 provenance가 되지 못하게 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            source_path = temporary_root / "source.jsonl"
            destination_path = temporary_root / "destination.jsonl"
            source_repository = TradeHistoryRepository(source_path)
            open_buy = make_trade(
                order_id="7201",
                requested_quantity=Decimal("1"),
                executed_quantity=Decimal("1"),
                executed_amount=Decimal("100"),
                average_fill_price=Decimal("100"),
                fee_amount=Decimal("0"),
                fee_asset="USDT",
                fee_quote_amount=Decimal("0"),
            )
            source_repository.save_this_trade_by_order_id(
                open_buy.order_id,
                open_buy,
            )

            # Position replay가 0이 아니면 destination 생성 전에 fail closed한다.
            with patch.dict(
                os.environ,
                {TESTNET_BASELINE_HISTORY_PATH_ENV: str(source_path)},
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "must close Position to zero",
                ):
                    seed_verified_closed_history(destination_path)

            self.assertFalse(destination_path.exists())

    def test_pending_baseline_is_rejected_before_destination_write(self) -> None:
        """
        함수 이름: test_pending_baseline_is_rejected_before_destination_write()
        기능: Position 0 history라도 PREPARED 주문이 있으면 actual run baseline으로 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            source_path = temporary_root / "source.jsonl"
            destination_path = temporary_root / "destination.jsonl"
            source_repository = TradeHistoryRepository(source_path)
            buy_trade = make_trade(
                order_id="7301",
                requested_quantity=Decimal("1"),
                executed_quantity=Decimal("1"),
                executed_amount=Decimal("100"),
                average_fill_price=Decimal("100"),
                fee_amount=Decimal("0"),
                fee_asset="USDT",
                fee_quote_amount=Decimal("0"),
            )
            sell_trade = make_trade(
                order_id="7302",
                side=OrderSide.SELL,
                requested_quantity=Decimal("1"),
                executed_quantity=Decimal("1"),
                executed_amount=Decimal("100"),
                average_fill_price=Decimal("100"),
                fee_amount=Decimal("0"),
                fee_asset="USDT",
                fee_quote_amount=Decimal("0"),
                allocated_cost_basis=Decimal("100"),
                realized_pnl=Decimal("0"),
                realized_return_rate=Decimal("0.00000000"),
            )
            for trade in (buy_trade, sell_trade):
                source_repository.save_this_trade_by_order_id(
                    trade.order_id,
                    trade,
                )
            pending_order = build_testnet_order(
                side=OrderSide.BUY,
                quantity=Decimal("0.01"),
                decision_price=Decimal("100"),
            )
            source_repository.save_pending_order(pending_order)

            # Pending identity가 하나라도 있으면 closed Position보다 우선해 destination을 만들지 않는다.
            with patch.dict(
                os.environ,
                {TESTNET_BASELINE_HISTORY_PATH_ENV: str(source_path)},
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "must not contain pending orders",
                ):
                    seed_verified_closed_history(destination_path)

            self.assertFalse(destination_path.exists())


if __name__ == "__main__":
    unittest.main()
