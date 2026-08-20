"""Performance startup 복원의 ADR-004 D-11 공식과 KST 경계를 검증한다."""

import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from decimal import Decimal

from binance_auto_trader.domain.history import Performance, Trade
from binance_auto_trader.domain.trading.states import ExitReason, OrderSide

from tests.unit.history.factories import make_trade


CURRENT_TIME = datetime(2026, 8, 21, 3, 0, tzinfo=timezone.utc)


def fixed_clock() -> datetime:
    """
    함수 이름: fixed_clock()
    기능: Performance 테스트의 현재 account day를 2026-08-21 KST로 고정한다.
    인자: 없음
    반환값: 고정 UTC datetime
    작성 날짜: 2026/08/21
    """
    return CURRENT_TIME


def make_second_golden_sell(
    executed_at: datetime | None = None,
) -> Trade:
    """
    함수 이름: make_second_golden_sell()
    기능: D-11 golden example의 90 USDT 손실 SELL Trade를 생성한다.
    인자: executed_at -> optional UTC 체결 시각
    반환값: 원가 100.10, 실현손익 -10.19인 SELL Trade
    작성 날짜: 2026/08/21
    """
    selected_time = (
        datetime(2026, 8, 20, 15, 2, tzinfo=timezone.utc)
        if executed_at is None
        else executed_at
    )
    return make_trade(
        trade_id="trade-sell-2",
        order_id="3",
        executed_at=selected_time,
        side=OrderSide.SELL,
        executed_amount=Decimal("90"),
        average_fill_price=Decimal("90"),
        fee_amount=Decimal("0.09"),
        fee_asset="USDT",
        fee_quote_amount=Decimal("0.09"),
        allocated_cost_basis=Decimal("100.10"),
        realized_pnl=Decimal("-10.19"),
        realized_return_rate=Decimal("-10.17982018"),
        exit_reason=ExitReason.STOP,
    )


class PerformanceTests(unittest.TestCase):
    """
    클래스 이름: PerformanceTests
    기능: durable Trade aggregate가 D-11 숫자와 계좌 날짜 범위를 재현하는지 테스트한다.
    작성 날짜: 2026/08/21
    """

    def test_reproduces_d11_performance_golden_vector_exactly(self) -> None:
        """
        함수 이름: test_reproduces_d11_performance_golden_vector_exactly()
        기능: 두 SELL의 손익·수익률·fee·승패 집계가 ADR golden 결과와 같은지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        buy_trade = make_trade(trade_id="trade-buy", order_id="1")
        first_sell = make_trade(
            trade_id="trade-sell-1",
            order_id="2",
            side=OrderSide.SELL,
        )
        second_sell = make_second_golden_sell()

        performance = Performance(
            (buy_trade, first_sell, second_sell),
            clock=fixed_clock,
        )

        self.assertEqual(performance.realized_pnl, Decimal("-0.40"))
        self.assertEqual(performance.total_profit, Decimal("-0.40"))
        self.assertEqual(
            performance.daily_return_rate,
            Decimal("-0.19980020"),
        )
        self.assertEqual(
            performance.cumulative_return_rate,
            Decimal("-0.19980020"),
        )
        self.assertEqual(
            performance.average_sell_return_rate,
            Decimal("-0.19980020"),
        )
        self.assertEqual(performance.daily_fee, Decimal("0.40"))
        self.assertEqual(performance.total_fee, Decimal("0.40"))
        self.assertEqual(performance.winning_sell_count, 1)
        self.assertEqual(performance.losing_sell_count, 1)
        self.assertEqual(performance.breakeven_sell_count, 0)
        self.assertEqual(performance.completed_sell_count, 2)
        self.assertEqual(performance.win_rate, Decimal("50.00000000"))

    def test_kst_midnight_separates_daily_from_cumulative_aggregates(self) -> None:
        """
        함수 이름: test_kst_midnight_separates_daily_from_cumulative_aggregates()
        기능: 14:59:59Z와 15:00:00Z SELL이 서로 다른 KST account day에 속하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        previous_day_sell = make_trade(
            trade_id="previous-sell",
            order_id="2",
            executed_at=datetime(
                2026,
                8,
                20,
                14,
                59,
                59,
                tzinfo=timezone.utc,
            ),
            side=OrderSide.SELL,
        )
        current_day_sell = make_second_golden_sell(
            datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)
        )

        performance = Performance(
            (previous_day_sell, current_day_sell),
            clock=fixed_clock,
        )

        self.assertEqual(
            performance.daily_return_rate,
            Decimal("-10.17982018"),
        )
        self.assertEqual(
            performance.cumulative_return_rate,
            Decimal("-0.19980020"),
        )
        self.assertEqual(performance.daily_fee, Decimal("0.09"))
        self.assertEqual(performance.total_fee, Decimal("0.20"))

    def test_empty_and_buy_only_history_have_zero_rates_and_null_win_rate(self) -> None:
        """
        함수 이름: test_empty_and_buy_only_history_have_zero_rates_and_null_win_rate()
        기능: SELL이 없을 때 수익률 0과 null win rate를 유지하면서 BUY fee를 집계한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        empty_performance = Performance(clock=fixed_clock)
        buy_performance = Performance((make_trade(),), clock=fixed_clock)

        self.assertEqual(empty_performance.daily_return_rate, Decimal("0"))
        self.assertEqual(
            empty_performance.cumulative_return_rate,
            Decimal("0"),
        )
        self.assertIsNone(empty_performance.win_rate)
        self.assertEqual(buy_performance.realized_pnl, Decimal("0"))
        self.assertEqual(buy_performance.daily_fee, Decimal("0.20"))
        self.assertEqual(buy_performance.total_fee, Decimal("0.20"))
        self.assertIsNone(buy_performance.win_rate)

    def test_counts_breakeven_as_completed_but_not_win(self) -> None:
        """
        함수 이름: test_counts_breakeven_as_completed_but_not_win()
        기능: 실현손익 0 SELL이 breakeven과 completed에만 포함되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        breakeven_sell = make_trade(
            side=OrderSide.SELL,
            executed_amount=Decimal("100.10"),
            average_fill_price=Decimal("100.10"),
            fee_amount=Decimal("0"),
            fee_quote_amount=Decimal("0"),
            realized_pnl=Decimal("0"),
            realized_return_rate=Decimal("0.00000000"),
        )

        performance = Performance((breakeven_sell,), clock=fixed_clock)

        self.assertEqual(performance.winning_sell_count, 0)
        self.assertEqual(performance.losing_sell_count, 0)
        self.assertEqual(performance.breakeven_sell_count, 1)
        self.assertEqual(performance.completed_sell_count, 1)
        self.assertEqual(performance.win_rate, Decimal("0E-8"))

    def test_performance_is_frozen_and_returns_same_snapshot(self) -> None:
        """
        함수 이름: test_performance_is_frozen_and_returns_same_snapshot()
        기능: startup Performance가 불변이고 get_performance가 같은 snapshot을 반환하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        performance = Performance(clock=fixed_clock)

        with self.assertRaises(FrozenInstanceError):
            performance.total_fee = Decimal("1")

        self.assertFalse(hasattr(performance, "__dict__"))
        self.assertIs(performance.get_performance(), performance)


if __name__ == "__main__":
    unittest.main()
