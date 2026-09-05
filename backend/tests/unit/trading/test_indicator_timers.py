"""전략 표시 타이머의 정확한 경계와 같은 단계 안의 회차 교체를 검증한다."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import unittest

from binance_auto_trader.application.trading_indicator_snapshot import TradingIndicatorStore
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.trading.context import MarketEvaluationSnapshot, PositionSnapshot, TradingRuntimeSnapshot
from binance_auto_trader.domain.trading.results import TradingSTMResult
from binance_auto_trader.domain.trading.states import TradingStateConfiguration, CaseBSignalState, CaseCSignalState, CaseBPositionState, CaseCPositionState, StrategyType, OwnershipState
from binance_auto_trader.domain.trading.stm import TradingSTM
from binance_auto_trader.transport.contracts import _map_trading_indicators
from tests.unit.trading.test_stm import create_test_context, TEST_EVALUATION_TIME


class TradingIndicatorTimerTests(unittest.TestCase):
    """
    클래스 이름: TradingIndicatorTimerTests
    기능: 타이머 상태·회차·서버 시각과 실제 조건 결과의 동시성을 확인한다.
    작성 날짜: 2026/09/05
    """

    def test_runtime_timer_boundaries_and_entry_pause(self) -> None:
        """
        함수 이름: test_runtime_timer_boundaries_and_entry_pause()
        기능: 3분·3시간·60분·6시간의 직전/동일/직후 상태와 B 진입 중지 중 시간 진행을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/05
        """
        cases = (
            ("b_signal_age", "signal_elapsed", 10800, ("running", "running", "expired")),
            ("c_recovery_window", "case_c_timer_elapsed", 180, ("running", "running", "expired")),
            ("b_time_exit", "holding_elapsed", 21600, ("running", "completed", "completed")),
            ("c_time_exit", "holding_elapsed", 3600, ("running", "completed", "completed")),
        )
        for condition_id, elapsed_field, duration, states in cases:
            for offset, expected_state in zip((-1, 0, 1), states):
                with self.subTest(condition=condition_id, offset=offset):
                    stm = TradingSTM(RegimeType.TYPE_0)
                    owner = (StrategyType.CASE_B if condition_id == "b_time_exit" else StrategyType.CASE_C) if condition_id.endswith("time_exit") else None
                    state = replace(TradingStateConfiguration.create_trade_management_initial_state(),
                        case_b_signal_state=CaseBSignalState.B_WAIT_PULLBACK, case_c_signal_state=CaseCSignalState.C_SETUP,
                        ownership_state=OwnershipState.NO_POSITION if owner is None else OwnershipState.CASE_B_POSITION_MANAGEMENT if owner is StrategyType.CASE_B else OwnershipState.CASE_C_POSITION_MANAGEMENT,
                        case_b_position_state=CaseBPositionState.CASE_B_HOLDING if owner is StrategyType.CASE_B else None,
                        case_c_position_state=CaseCPositionState.CASE_C_HOLDING if owner is StrategyType.CASE_C else None)
                    stm._state = state
                    context = create_test_context(runtime=TradingRuntimeSnapshot(
                        case_b_enabled=True, case_c_enabled=True, signal_created=True, signal_time=TEST_EVALUATION_TIME,
                        timer_base_time=TEST_EVALUATION_TIME, timer_base_pct_b=Decimal("-0.3"), flush_low=Decimal("90"),
                        entry_pct_b=Decimal("-0.24"), position_owner=owner, case_b_entry_paused=True),
                        position=PositionSnapshot(quantity=Decimal("1") if owner is not None else Decimal("0"), entry_price=Decimal("100")))
                    store = TradingIndicatorStore()
                    result = TradingSTMResult("timer", False, (), state, state, (), context.version)
                    store.observe(stm, result, context, context, 1, TEST_EVALUATION_TIME)
                    elapsed = timedelta(seconds=duration, microseconds=offset)
                    later = replace(context, evaluated_at=TEST_EVALUATION_TIME + elapsed,
                        market=replace(context.market, **{elapsed_field: elapsed}))
                    store.observe(stm, result, later, later, 2, TEST_EVALUATION_TIME)
                    snapshot = store.snapshot(stm, later)
                    row = next(row for row in snapshot.conditions if row.slot.condition_id == condition_id)
                    self.assertEqual(row.timer.state, expected_state)
                    self.assertEqual(row.timer.remaining_seconds, Decimal("0.000001") if offset == -1 else Decimal("0"))
                    payload = _map_trading_indicators(snapshot)
                    self.assertEqual(next(value for value in payload["conditions"] if value["condition_id"] == condition_id)["timer"]["remaining_seconds"], "0.000001" if offset == -1 else "0")
                    # 조회 시각만 바뀌어도 worker가 무한 publication을 시작하면 안 된다.
                    self.assertEqual(snapshot, store.snapshot(stm, replace(later, evaluated_at=later.evaluated_at + timedelta(seconds=1))))

    def test_recovery_reset_discards_old_epoch_until_new_market(self) -> None:
        """
        함수 이름: test_recovery_reset_discards_old_epoch_until_new_market()
        기능: 동일 C_SETUP 단계의 리셋 직후 180초와 새 회차를 보내고 이전 판정 재사용을 막는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/05
        """
        for reset_reason in ("timeout", "new_low"):
            stm = TradingSTM(RegimeType.TYPE_0)
            state = replace(TradingStateConfiguration.create_trade_management_initial_state(), case_c_signal_state=CaseCSignalState.C_SETUP)
            stm._state = state
            context = replace(create_test_context(runtime=TradingRuntimeSnapshot(
                case_c_enabled=True, flush_low=Decimal("90"), timer_base_time=TEST_EVALUATION_TIME,
                timer_base_pct_b=Decimal("-0.3"), entry_pct_b=Decimal("-0.24")),
                market=MarketEvaluationSnapshot(case_c_timer_elapsed=timedelta(seconds=181), realtime_pct_b=Decimal("-0.27"))),
                evaluated_at=TEST_EVALUATION_TIME + timedelta(seconds=181))
            after = replace(context, runtime=replace(context.runtime, timer_base_time=context.evaluated_at,
                timer_base_pct_b=Decimal("-0.27"), entry_pct_b=Decimal("-0.21"),
                flush_low=Decimal("89") if reset_reason == "new_low" else Decimal("90")))
            store = TradingIndicatorStore()
            result = TradingSTMResult("reset", True, ("C-11",), state, state, (), context.version)
            store.observe(stm, result, context, context, 1)
            old_snapshot = store.snapshot(stm, context)
            old_timer = next(row.timer for row in old_snapshot.conditions if row.slot.condition_id == "c_recovery_window")
            store.observe(stm, result, context, after, 1)
            store.observe(stm, result, after, after, 1)  # 같은 source의 내부 microstep도 이전 경과 시간을 재사용하면 안 된다.
            reset_snapshot = store.snapshot(stm, after)
            self.assertEqual(reset_snapshot.phase_key, old_snapshot.phase_key)
            for row in reset_snapshot.conditions:
                if row.slot.condition_id in ("c_rebound", "c_entry_limit", "c_recovery_window"):
                    self.assertIsNone(row.condition.satisfied)
                if row.slot.condition_id == "c_rebound":
                    self.assertEqual(row.condition.threshold, Decimal("-0.21"))  # 새 목표는 회색 판정과 원자적으로 표시한다.
            timer = next(row.timer for row in reset_snapshot.conditions if row.slot.condition_id == "c_recovery_window")
            self.assertNotEqual(timer.timer_id, old_timer.timer_id)
            self.assertEqual((timer.remaining_seconds, timer.state, timer.reset_reason), (Decimal("180"), "running", reset_reason))
            fresh = replace(after, evaluated_at=after.evaluated_at + timedelta(seconds=1),
                market=replace(after.market, case_c_timer_elapsed=timedelta(seconds=1)))
            store.observe(stm, result, fresh, fresh, 2)
            row = next(row for row in store.snapshot(stm, fresh).conditions if row.slot.condition_id == "c_recovery_window")
            self.assertTrue(row.condition.satisfied)
            self.assertEqual(row.timer.remaining_seconds, Decimal("179"))
