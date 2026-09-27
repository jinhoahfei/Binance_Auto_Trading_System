"""주문 대기·평가 중단의 확정 지표와 타이머 수명을 검증한다."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import unittest

from binance_auto_trader.application.trading_indicator_snapshot import TradingIndicatorStore
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.trading.context import MarketEvaluationSnapshot, PositionSnapshot, TradingRuntimeSnapshot
from binance_auto_trader.domain.trading.results import TradingSTMResult
from binance_auto_trader.domain.trading.states import (
    CaseBPositionState, CaseCPositionState, ExitReason, OrderSide, OwnershipState,
    PositionReturnState, RootState, StrategyType, TradingStateConfiguration,
)
from binance_auto_trader.domain.trading.stm import TradingSTM
from tests.unit.trading.test_stm import create_test_context


class IndicatorPauseTests(unittest.TestCase):
    """
    클래스 이름: IndicatorPauseTests
    기능: 중단 표시를 실행 조건과 분리하고 새 평가만 활성 표시를 복구하는지 검증한다.
    작성 날짜: 2026/09/27
    """

    def _create_holding(self, strategy=StrategyType.CASE_B):
        """
        함수 이름: _create_holding()
        기능: 같은 포지션에서 평가된 지표와 실제 경과시간 타이머를 만든다.
        인자: strategy -> B 또는 C 보유 전략
        반환값: STM, store, context, result, entered_at
        작성 날짜: 2026/09/27
        """
        stm = TradingSTM(RegimeType.TYPE_0)
        state = replace(TradingStateConfiguration.create_trade_management_initial_state(),
            ownership_state=OwnershipState.CASE_B_POSITION_MANAGEMENT if strategy is StrategyType.CASE_B else OwnershipState.CASE_C_POSITION_MANAGEMENT,
            case_b_position_state=CaseBPositionState.CASE_B_HOLDING if strategy is StrategyType.CASE_B else None,
            case_c_position_state=CaseCPositionState.CASE_C_HOLDING if strategy is StrategyType.CASE_C else None)
        stm._state = state
        context = create_test_context(runtime=TradingRuntimeSnapshot(lower_event_id="lower:1", position_owner=strategy),
            market=MarketEvaluationSnapshot(realtime_price=Decimal("100"), upper_band=Decimal("110"),
                realtime_pct_b=Decimal("0.2"), realtime_ema_slope=Decimal("0.1"), confirmed_30m_close=True,
                holding_elapsed=timedelta(seconds=90)), position=PositionSnapshot(quantity=Decimal("1"), entry_price=Decimal("100")))
        entered_at = context.evaluated_at - timedelta(seconds=90)
        store = TradingIndicatorStore()
        result = TradingSTMResult("market:pause-test", False, (), state, state, (), context.version)
        store.observe(stm, result, context, context, 1, entered_at)
        return stm, store, context, result, entered_at

    def test_pending_reads_keep_confirmed_rows_and_do_not_mutate_store(self) -> None:
        """
        함수 이름: test_pending_reads_keep_confirmed_rows_and_do_not_mutate_store()
        기능: 매도 대기 조회가 B·C 지표를 제거하거나 마지막 평가를 파괴하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        for strategy in StrategyType:
            stm, store, context, result, entered_at = self._create_holding(strategy)
            before = store.snapshot(stm, context, entered_at=entered_at)
            pending = replace(context, runtime=replace(context.runtime, pending_intent_id="unsent",
                pending_strategy=strategy, pending_order_side=OrderSide.SELL, pending_exit_reason=ExitReason.STOP,
                pending_return_state=PositionReturnState.CASE_B_HOLDING if strategy is StrategyType.CASE_B else PositionReturnState.CASE_C_HOLDING))
            stored = dict(store._evaluations)
            for offset in (0, 30, 600):
                snapshot = store.snapshot(stm, replace(pending, evaluated_at=pending.evaluated_at + timedelta(seconds=offset)), entered_at=entered_at)
                self.assertEqual([row.slot for row in snapshot.conditions], [row.slot for row in before.conditions])
                self.assertEqual([row.condition for row in snapshot.conditions], [row.condition for row in before.conditions])
                self.assertTrue(all(row.evaluation_state == "paused" for row in snapshot.conditions if row.slot.strategy is strategy))
                self.assertEqual(store._evaluations, stored)
            self.assertEqual(before, store.snapshot(stm, context, entered_at=entered_at))

    def test_paused_timer_and_values_wait_for_a_new_market_evaluation(self) -> None:
        """
        함수 이름: test_paused_timer_and_values_wait_for_a_new_market_evaluation()
        기능: 반복 조회·RUNNING 복구만으로 시간을 진행하지 않고 실제 새 평가에서 경과를 반영한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        stm, store, context, result, entered_at = self._create_holding()
        store.pause()
        frozen = store.snapshot(stm, context, evaluation_running=False, entered_at=entered_at)
        timer = next(row.timer for row in frozen.conditions if row.slot.condition_id == "b_time_exit")
        self.assertEqual(timer.remaining_seconds, Decimal("21510"))
        later = replace(context, evaluated_at=context.evaluated_at + timedelta(seconds=120),
            market=replace(context.market, realtime_pct_b=Decimal("0.7"), holding_elapsed=timedelta(seconds=210)))
        self.assertEqual(frozen, store.snapshot(stm, later, entered_at=entered_at))
        store.observe(stm, result, later, later, 1, entered_at, fresh_market_evaluation=False)
        self.assertEqual(frozen, store.snapshot(stm, later, entered_at=entered_at))
        store.observe(stm, result, later, later, 2, entered_at)
        resumed = store.snapshot(stm, later, entered_at=entered_at)
        self.assertTrue(all(row.evaluation_state == "active" for row in resumed.conditions))
        self.assertEqual(next(row.condition.value for row in resumed.conditions if row.slot.condition_id == "b_profit_zone"), Decimal("0.7"))
        self.assertEqual(next(row.timer.remaining_seconds for row in resumed.conditions if row.slot.condition_id == "b_time_exit"), Decimal("21390"))

    def test_common_evaluation_does_not_advance_pending_position_timer(self) -> None:
        """
        함수 이름: test_common_evaluation_does_not_advance_pending_position_timer()
        기능: 주문 중 공통 조건이 평가되어도 보류된 보유 지표의 시각·타이머는 고정한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        stm, store, context, result, entered_at = self._create_holding()
        pending = replace(context, runtime=replace(context.runtime, pending_intent_id="waiting", pending_exit_reason=ExitReason.STOP))
        before = store.snapshot(stm, pending, entered_at=entered_at)
        later = replace(pending, evaluated_at=pending.evaluated_at + timedelta(seconds=60),
            market=replace(pending.market, holding_elapsed=timedelta(seconds=150)))
        store.observe(stm, result, later, later, 2, entered_at)
        after = store.snapshot(stm, later, entered_at=entered_at)
        self.assertEqual([row for row in before.conditions if row.slot.strategy is StrategyType.CASE_B],
                         [row for row in after.conditions if row.slot.strategy is StrategyType.CASE_B])

    def test_closed_candle_row_stays_paused_until_its_own_new_evaluation(self) -> None:
        """
        함수 이름: test_closed_candle_row_stays_paused_until_its_own_new_evaluation()
        기능: 복구된 실시간 tick이 미평가 확정봉 지표의 색상과 평가 시각을 복구하지 않게 한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        stm, store, context, result, entered_at = self._create_holding()
        store.pause()
        original = next(row for row in store.snapshot(stm, context).conditions if row.slot.condition_id == "b_stop")
        tick = replace(context, evaluated_at=context.evaluated_at + timedelta(seconds=60),
            market=replace(context.market, confirmed_30m_close=False))
        store.observe(stm, result, tick, tick, 2, entered_at)
        resumed = store.snapshot(stm, tick, entered_at=entered_at)
        self.assertEqual(next(row for row in resumed.conditions if row.slot.condition_id == "b_stop"), original)
        self.assertEqual(next(row.evaluation_state for row in resumed.conditions if row.slot.condition_id == "b_profit_zone"), "active")
        store.observe(stm, result, tick, tick, 2, entered_at, fresh_market_evaluation=False)
        self.assertEqual(next(row for row in store.snapshot(stm, tick).conditions if row.slot.condition_id == "b_stop"), original)
        closed = replace(tick, market=replace(tick.market, confirmed_30m_close=True))
        store.observe(stm, result, closed, closed, 3, entered_at)
        refreshed = next(row for row in store.snapshot(stm, closed).conditions if row.slot.condition_id == "b_stop")
        self.assertEqual(refreshed.evaluation_state, "active")
        self.assertEqual(refreshed.market_version, 3)

    def test_changed_scope_position_and_phase_do_not_reuse_previous_values(self) -> None:
        """
        함수 이름: test_changed_scope_position_and_phase_do_not_reuse_previous_values()
        기능: 다른 세션·하단 이벤트·포지션·실제 단계에 이전 값을 표시하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        stm, store, context, result, entered_at = self._create_holding()
        changed = replace(context, runtime=replace(context.runtime, lower_event_id="lower:2"))
        self.assertTrue(all(row.evaluated_at is None for row in store.snapshot(stm, changed).conditions))
        replacement = store.snapshot(stm, context, entered_at=entered_at + timedelta(seconds=1))
        self.assertTrue(all(row.evaluated_at is None for row in replacement.conditions if row.slot.strategy is StrategyType.CASE_B))
        new_stm = TradingSTM(RegimeType.TYPE_0)
        new_stm._state = stm.current_state
        self.assertTrue(all(row.evaluated_at is None for row in store.snapshot(new_stm, context).conditions))
        stm._state = replace(stm.current_state, case_b_position_state=CaseBPositionState.CASE_B_TREND_HOLD)
        self.assertTrue(all(row.evaluated_at is None for row in store.snapshot(stm, context).conditions if row.slot.strategy is StrategyType.CASE_B))

    def test_stopping_retains_display_until_terminal_state(self) -> None:
        """
        함수 이름: test_stopping_retains_display_until_terminal_state()
        기능: 종료 중 보유 지표를 보존하되 종료 완료 후에는 이전 행을 노출하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        stm, store, context, result, entered_at = self._create_holding()
        store.pause()
        stm._state = TradingStateConfiguration(root_state=RootState.STOPPING)
        snapshot = store.snapshot(stm, context, evaluation_running=False, entered_at=entered_at)
        self.assertEqual(len(snapshot.conditions), 7)
        self.assertTrue(all(row.evaluation_state == "paused" for row in snapshot.conditions))
        stopping = replace(result, state_before=stm.current_state, state_after=stm.current_state)
        store.observe(stm, stopping, context, context, 1, entered_at, evaluation_running=False,
            fresh_market_evaluation=False)
        self.assertEqual(snapshot, store.snapshot(stm, context, evaluation_running=False, entered_at=entered_at))
        stm._state = TradingStateConfiguration(root_state=RootState.LOGIC_TERMINATED)
        self.assertEqual(store.snapshot(stm, context).conditions, ())
