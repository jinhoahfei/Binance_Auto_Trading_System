"""Performance startup 복원의 ADR-004 D-11 공식과 KST 경계를 검증한다."""

import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from decimal import Decimal

from binance_auto_trader.domain.history import (
    InvalidZeroCostBasisError,
    OrderHistoryConflictError,
    Performance,
    RealizedResult,
    Trade,
)
from binance_auto_trader.domain.trading.states import ExitReason, OrderSide

from tests.unit.history.factories import make_order_execution, make_trade


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

    def test_constructor_rejects_invalid_clock_and_non_trade_values(self) -> None:
        """
        함수 이름: test_constructor_rejects_invalid_clock_and_non_trade_values()
        기능: Communication 3.3 생성자가 잘못된 clock과 복원 원소를 성과 0으로 숨기지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        # Startup 복원 근거가 유효하지 않으면 어떤 aggregate도 게시하기 전에 거부해야 한다.
        with self.assertRaisesRegex(TypeError, "clock must be callable"):
            Performance(clock="not-callable")  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "only Trade"):
            Performance((object(),), clock=fixed_clock)  # type: ignore[arg-type]

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

    def test_apply_new_trade_matches_full_rebuild_and_is_idempotent(self) -> None:
        """
        함수 이름: test_apply_new_trade_matches_full_rebuild_and_is_idempotent()
        기능: 신규 Trade 증분 반영이 전체 복원과 같고 동일 order 재적용은 no-op인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        buy_trade = make_trade(trade_id="buy", order_id="1")
        sell_trade = make_trade(
            trade_id="sell",
            order_id="2",
            side=OrderSide.SELL,
        )
        performance = Performance((buy_trade,), clock=fixed_clock)
        expected_performance = Performance(
            (buy_trade, sell_trade),
            clock=fixed_clock,
        )

        # 새 Trade를 한 번 반영한 뒤 동일 객체 재적용으로 수치가 변하지 않아야 한다.
        performance.apply_new_trade(sell_trade)
        performance.apply_new_trade(sell_trade)

        self.assertEqual(performance.realized_pnl, expected_performance.realized_pnl)
        self.assertEqual(performance.total_fee, expected_performance.total_fee)
        self.assertEqual(
            performance.cumulative_return_rate,
            expected_performance.cumulative_return_rate,
        )
        self.assertEqual(
            performance.completed_sell_count,
            expected_performance.completed_sell_count,
        )

    def test_apply_new_trade_rejects_same_order_with_different_content(self) -> None:
        """
        함수 이름: test_apply_new_trade_rejects_same_order_with_different_content()
        기능: 이미 집계한 order ID를 다른 Trade 내용으로 덮어쓰지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        original_trade = make_trade(trade_id="original", order_id="81")
        conflicting_trade = make_trade(trade_id="different", order_id="81")
        performance = Performance((original_trade,), clock=fixed_clock)

        with self.assertRaises(OrderHistoryConflictError):
            performance.apply_new_trade(conflicting_trade)

        self.assertEqual(performance.total_fee, original_trade.fee_quote_amount)

    def test_calculate_realized_result_reproduces_d11_sell_formula(self) -> None:
        """
        함수 이름: test_calculate_realized_result_reproduces_d11_sell_formula()
        기능: SELL amount·fee·allocated cost가 D-11 PnL과 8자리 수익률을 만드는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        _, summary = make_order_execution(side=OrderSide.SELL)
        performance = Performance(clock=fixed_clock)

        # Position mutation 전에 고정한 원가를 ADR-004 net proceeds 공식에 대입한다.
        result = performance.calculate_realized_result(
            summary,
            Decimal("100.10"),
        )

        self.assertIsInstance(result, RealizedResult)
        self.assertEqual(result.allocated_cost_basis, Decimal("100.10"))
        self.assertEqual(result.realized_pnl, Decimal("9.79"))
        self.assertEqual(
            result.realized_return_rate,
            Decimal("9.78021978"),
        )

    def test_calculate_realized_result_rejects_zero_cost_as_typed_failure(
        self,
    ) -> None:
        """
        함수 이름: test_calculate_realized_result_rejects_zero_cost_as_typed_failure()
        기능: SELL 원가 0을 수익률 0으로 숨기지 않고 reconciliation typed error로 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        _, summary = make_order_execution(side=OrderSide.SELL)
        performance = Performance(clock=fixed_clock)

        with self.assertRaises(InvalidZeroCostBasisError) as captured_error:
            performance.calculate_realized_result(summary, Decimal("0"))

        self.assertEqual(
            captured_error.exception.code,
            "INVALID_ZERO_COST_BASIS",
        )


if __name__ == "__main__":
    unittest.main()
