"""전략 조건 경계와 단계별 불변 표시 결과를 검증한다."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import unittest

from binance_auto_trader.application.trading_indicator_snapshot import TradingIndicatorStore, select_indicator_slots
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.trading.conditions import evaluate_condition
from binance_auto_trader.domain.trading.context import MarketEvaluationSnapshot, TradingRuntimeSnapshot, PositionSnapshot
from binance_auto_trader.domain.trading.states import (
    TradingStateConfiguration, RootState, StrategyType, OwnershipState, ExitReason,
    CaseBSignalState, CaseCSignalState, CaseBPositionState, CaseCPositionState,
)
from binance_auto_trader.domain.trading.stm import TradingSTM
from binance_auto_trader.domain.trading.events import TradingEventType
from binance_auto_trader.domain.trading.results import TradingSTMResult
from binance_auto_trader.domain.trading.transitions.case_b_signal_transitions import _is_signal_candle_guard_satisfied
from binance_auto_trader.domain.trading.transitions.case_c_position_transitions import _select_trailing_event
from tests.unit.trading.test_stm import create_test_context


class TradingIndicatorTests(unittest.TestCase):
    """
    클래스 이름: TradingIndicatorTests
    기능: 명세의 숫자 경계와 실제 Guard 및 단계별 평가 보존 계약을 확인한다.
    작성 날짜: 2026/09/05
    """

    def test_numeric_condition_boundaries(self) -> None:
        """
        함수 이름: test_numeric_condition_boundaries()
        기능: 명세의 strict·inclusive 비교를 임계값 직전·동일·직후에서 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/05
        """
        # 기대 결과는 구현의 비교 연산자를 가져오지 않고 승인된 명세에서 독립적으로 고정한다.
        boundaries = (
            ("b_signal_slope", "ema_slope_30m_close", "-0.03", (False, False, True)),
            ("b_signal_pct_b", "pct_b_close", "0.25", (False, False, True)),
            ("b_pullback", "realtime_pct_b", "0.30", (True, True, False)),
            ("b_take_profit_slope", "realtime_ema_slope", "0.08", (True, True, False)),
            ("b_stop", "ema_slope_30m_close", "-0.08", (True, False, False)),
            ("c_setup_pct_b", "realtime_pct_b", "-0.15", (True, True, False)),
            ("c_setup_cci", "cci_30m_realtime", "-140", (True, True, False)),
            ("c_flush", "realtime_pct_b", "-0.25", (True, True, False)),
            ("c_recovery", "realtime_pct_b", "0.25", (False, True, True)),
            ("c_profit_zone", "realtime_pct_b", "0.10", (False, True, True)),
            ("c_trail_fallback", "realtime_pct_b", "0.10", (True, False, False)),
        )
        for condition_id, field, boundary, expected in boundaries:
            for offset, satisfied in zip(("-0.00000001", "0", "0.00000001"), expected):
                value = Decimal(boundary) + Decimal(offset)
                context = create_test_context(market=replace(MarketEvaluationSnapshot(), **{field: value}))
                with self.subTest(condition=condition_id, value=value):
                    result = evaluate_condition(condition_id, context)
                    self.assertIs(result.satisfied, satisfied)
                    self.assertEqual(result.value, value)
                    self.assertEqual(result.threshold, Decimal(boundary))

    def test_elapsed_and_backend_continuity_results(self) -> None:
        """
        함수 이름: test_elapsed_and_backend_continuity_results()
        기능: 3분·3시간·6시간·60분 경계 및 유지시간 완료 전후 판정을 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/05
        """
        # 1마이크로초 차이도 Decimal 초 단위 전송에서 보존되어야 한다.
        for condition_id, field, seconds, expected in (
            ("b_signal_age", "signal_elapsed", 10800, (True, True, False)),
            ("c_recovery_window", "case_c_timer_elapsed", 180, (True, True, False)),
            ("b_time_exit", "holding_elapsed", 21600, (False, True, True)),
            ("c_time_exit", "holding_elapsed", 3600, (False, True, True)),
        ):
            for offset, satisfied in zip((-1, 0, 1), expected):
                elapsed = timedelta(seconds=seconds, microseconds=offset)
                context = create_test_context(market=replace(MarketEvaluationSnapshot(), **{field: elapsed}))
                self.assertIs(evaluate_condition(condition_id, context).satisfied, satisfied)
        for condition_id, field, duration in (
            ("b_profit_zone", "pct_b_at_least_060_for_5s", 5),
            ("b_trend_exit_slope", "realtime_slope_at_most_004_for_5s", 5),
            ("b_trend_exit_pct_b", "pct_b_below_060_for_5s", 5),
            ("c_stop", "realtime_slope_at_most_minus_055_for_3m", 180),
        ):
            for held in (False, True):
                context = create_test_context(market=replace(MarketEvaluationSnapshot(), **{field: held}))
                evaluation = evaluate_condition(condition_id, context)
                self.assertIs(evaluation.satisfied, held)  # UI용 재계산이 기존 timer flag를 대체하지 않는다.
                self.assertEqual(evaluation.hold_seconds, duration)

    def test_dynamic_references_and_actual_signal_guard(self) -> None:
        """
        함수 이름: test_dynamic_references_and_actual_signal_guard()
        기능: touch·직전 저가·회복 기준·손절가 및 실제 B signal Guard와 표시 판정의 일치를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/05
        """
        context = create_test_context(
            market=MarketEvaluationSnapshot(confirmed_30m_close=True, ema_slope_30m_close=Decimal("0"),
                pct_b_close=Decimal("0.3"), current_closed_candle_low=Decimal("90"),
                previous_3_closed_candle_lows=(Decimal("90"), Decimal("91"), Decimal("92"))),
            runtime=TradingRuntimeSnapshot(touch_candle_low=Decimal("90"), lower_band_at_touch=Decimal("90"),
                touch_candle_bbw=Decimal("0.01999999"), flush_low=Decimal("89"), entry_pct_b=Decimal("-0.24")),
            position=PositionSnapshot(quantity=Decimal("1"), entry_price=Decimal("100")),
        )
        self.assertTrue(evaluate_condition("b_touch_bbw", context).satisfied)
        self.assertEqual(evaluate_condition("b_emergency_stop", context).threshold, Decimal("99"))
        self.assertEqual(evaluate_condition("c_rebound", context).threshold, Decimal("-0.24"))
        for low in ("89.99999999", "90", "90.00000001"):
            candidate = replace(context, market=replace(context.market, current_closed_candle_low=Decimal(low)))
            self.assertEqual(_is_signal_candle_guard_satisfied(candidate), evaluate_condition("b_signal_low", candidate).satisfied)
        missing = create_test_context()
        self.assertIsNone(evaluate_condition("b_signal_low", missing).satisfied)
        self.assertIsNone(evaluate_condition("b_emergency_stop", missing).satisfied)

    def test_every_phase_selects_only_current_conditions(self) -> None:
        """
        함수 이름: test_every_phase_selects_only_current_conditions()
        기능: B·C 단계와 병렬·주문·회복·종료의 목록 및 공통 조건 중복을 검사한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/05
        """
        initial = TradingStateConfiguration.create_trade_management_initial_state()
        context = create_test_context(runtime=TradingRuntimeSnapshot(case_b_enabled=True, case_c_enabled=True))
        for phase, first_id in (
            (CaseBSignalState.B_WAIT_TOUCH, "b_touch_bbw"),
            (CaseBSignalState.B_WAIT_SIGNAL, "b_signal_slope"),
            (CaseBSignalState.B_WAIT_PULLBACK, "b_pullback"),
            (CaseBSignalState.B_POSITION_OPEN_SIGNALLED, "b_signal_age"),
        ):
            slots, _, _ = select_indicator_slots(replace(initial, case_b_signal_state=phase), context)
            self.assertEqual(slots[0].condition_id, first_id)
            self.assertEqual(sum(slot.condition_id == "upper_safe_exit" for slot in slots), 1)
            self.assertIn("c_setup_cci", [slot.condition_id for slot in slots])
        for strategy, ownership, field, phases in (
            (StrategyType.CASE_B, OwnershipState.CASE_B_POSITION_MANAGEMENT, "case_b_position_state", (
                (CaseBPositionState.CASE_B_HOLDING, "b_profit_zone"),
                (CaseBPositionState.CASE_B_TREND_HOLD, "b_trend_exit_slope"))),
            (StrategyType.CASE_C, OwnershipState.CASE_C_POSITION_MANAGEMENT, "case_c_position_state", (
                (CaseCPositionState.CASE_C_HOLDING, "c_profit_zone"),
                (CaseCPositionState.CASE_C_TP_TRAILING, "c_trail_fallback"),
                (CaseCPositionState.CASE_C_CLOSED, "c_recovery"),
                (CaseCPositionState.CASE_C_RECOVERY_SUCCEEDED, "c_handoff"))),
        ):
            for phase, first_id in phases:
                state = replace(initial, ownership_state=ownership, **{field: phase})
                owned = replace(context, runtime=replace(context.runtime, position_owner=strategy))
                slots, _, _ = select_indicator_slots(state, owned)
                self.assertEqual(slots[0].condition_id, first_id)
                if phase is CaseBPositionState.CASE_B_TREND_HOLD:
                    self.assertEqual([slot.condition_id for slot in slots if slot.strategy is strategy],
                                     ["b_trend_exit_slope", "b_trend_exit_pct_b"])

                self.assertTrue(all(slot.strategy in (strategy, None) for slot in slots))
        setup = replace(initial, case_c_signal_state=CaseCSignalState.C_SETUP)
        slots, _, _ = select_indicator_slots(setup, context)
        self.assertIn("c_flush", [slot.condition_id for slot in slots])
        recovering = replace(context, runtime=replace(context.runtime, flush_low=Decimal("90")))
        slots, _, _ = select_indicator_slots(setup, recovering)
        self.assertIn("c_rebound", [slot.condition_id for slot in slots])
        self.assertNotIn("c_flush", [slot.condition_id for slot in slots])
        pending = replace(context, runtime=replace(context.runtime, pending_exit_reason=ExitReason.STOP))
        slots, _, notice = select_indicator_slots(initial, pending)
        self.assertEqual([slot.condition_id for slot in slots], ["upper_safe_exit"])
        self.assertEqual(notice, "order_pending")
        for root in (RootState.NOT_STARTED, RootState.LOGIC_TERMINATED, RootState.STOPPING):
            self.assertEqual(select_indicator_slots(TradingStateConfiguration(root_state=root), context)[0], ())

    def test_confirmed_values_and_previous_trailing_reference_are_preserved(self) -> None:
        """
        함수 이름: test_confirmed_values_and_previous_trailing_reference_are_preserved()
        기능: PC-15가 기준을 갱신한 뒤에도 원래 1분봉 비교 결과와 출처가 유지되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/05
        """
        # 상태 fixture는 평가 저장소만 격리하며 Guard는 실제 trailing 선택 함수를 사용한다.
        stm = TradingSTM(RegimeType.TYPE_0)
        state = replace(TradingStateConfiguration.create_trade_management_initial_state(),
            ownership_state=OwnershipState.CASE_C_POSITION_MANAGEMENT,
            case_c_position_state=CaseCPositionState.CASE_C_TP_TRAILING)
        stm._state = state
        context = create_test_context(market=MarketEvaluationSnapshot(confirmed_1m_close=True,
            realtime_pct_b=Decimal("0.2"), current_close_ema_slope=Decimal("0.3")),
            runtime=TradingRuntimeSnapshot(position_owner=StrategyType.CASE_C, previous_trail_ema_slope=Decimal("0.2")))
        store = TradingIndicatorStore()
        self.assertTrue(all(row.condition.satisfied is None for row in store.snapshot(stm, context).conditions))
        self.assertIs(_select_trailing_event(context), TradingEventType.CASE_C_EMA_INCREASEMENT)
        after = replace(context, version=2, runtime=replace(context.runtime, previous_trail_ema_slope=Decimal("0.3")))
        result = TradingSTMResult("test", True, ("PC-15",), state, state, (), context.version)
        store.observe(stm, result, context, after, 10)
        store.observe(stm, replace(result, context_version=2), after, after, 10)
        row = next(row for row in store.snapshot(stm, after).conditions if row.slot.condition_id == "c_trail_increase")
        self.assertEqual(row.condition.threshold, Decimal("0.2"))
        self.assertTrue(row.condition.satisfied)
        tick = replace(after, market=replace(after.market, confirmed_1m_close=False, current_close_ema_slope=Decimal("-99")))
        store.observe(stm, result, tick, tick, 11)
        self.assertEqual(row, next(value for value in store.snapshot(stm, tick).conditions if value.slot == row.slot))
        next_close = replace(after, market=replace(after.market, current_close_ema_slope=Decimal("0.3")))
        self.assertIs(_select_trailing_event(next_close), TradingEventType.CASE_C_EMA_DECREASEMENT)
        store.observe(stm, result, next_close, next_close, 12)
        latest = next(value for value in store.snapshot(stm, next_close).conditions if value.slot == row.slot)
        self.assertFalse(latest.condition.satisfied)  # 동일 기울기는 증가 조건의 미충족이다.
        self.assertEqual(latest.condition.threshold, Decimal("0.3"))

    def test_decimal_wire_format_and_phase_invalidation(self) -> None:
        """
        함수 이름: test_decimal_wire_format_and_phase_invalidation()
        기능: 지수 표기의 Decimal 정밀도와 단계 전환 직후의 미평가 상태를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/05
        """
        from binance_auto_trader.transport.contracts import _map_trading_indicators

        # 실제 시장 계산의 0E-8도 JSON 숫자나 지수 표기로 변환하지 않는다.
        stm = TradingSTM(RegimeType.TYPE_0)
        state = replace(TradingStateConfiguration.create_trade_management_initial_state(),
            case_b_signal_state=CaseBSignalState.B_WAIT_SIGNAL)
        stm._state = state
        context = create_test_context(market=MarketEvaluationSnapshot(confirmed_30m_close=True,
            ema_slope_30m_close=Decimal("0E-8")), runtime=TradingRuntimeSnapshot(case_b_enabled=True))
        store = TradingIndicatorStore()
        result = TradingSTMResult("wire", False, (), state, state, (), context.version)
        store.observe(stm, result, context, context, 1)
        payload = _map_trading_indicators(store.snapshot(stm, context))
        row = next(row for row in payload["conditions"] if row["condition_id"] == "b_signal_slope")
        self.assertEqual(row["value"], "0.00000000")
        self.assertTrue(row["satisfied"])
        tick = replace(context, market=replace(context.market, confirmed_30m_close=False, ema_slope_30m_close=Decimal("-9")))
        store.observe(stm, result, tick, tick, 2)
        self.assertEqual(row, next(value for value in _map_trading_indicators(store.snapshot(stm, tick))["conditions"]
            if value["condition_id"] == "b_signal_slope"))
        next_state = replace(state, case_b_signal_state=CaseBSignalState.B_WAIT_PULLBACK)
        stm._state = next_state
        next_snapshot = store.snapshot(stm, context)
        case_rows = [row for row in next_snapshot.conditions if row.slot.strategy is StrategyType.CASE_B]
        self.assertEqual([row.slot.condition_id for row in case_rows], ["b_pullback", "b_signal_age"])
        self.assertTrue(all(row.condition.satisfied is None for row in case_rows))
        invalid_context = replace(context, market=replace(context.market, realtime_ema_slope=Decimal("NaN")))
        self.assertIsNone(evaluate_condition("b_take_profit_slope", invalid_context).satisfied)

    def test_touch_price_recovery_and_handoff_boundaries(self) -> None:
        """
        함수 이름: test_touch_price_recovery_and_handoff_boundaries()
        기능: 고정 touch 값과 동적 가격·회복·인계 기준의 직전·동일·직후를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/05
        """
        context = create_test_context(
            market=MarketEvaluationSnapshot(lower_band=Decimal("100"), upper_band=Decimal("120"),
                realtime_slope_above_008_for_5s=True),
            runtime=TradingRuntimeSnapshot(lower_band_at_touch=Decimal("90"), flush_low=Decimal("90"),
                entry_pct_b=Decimal("-0.24"), previous_trail_ema_slope=Decimal("0.2")),
            position=PositionSnapshot(quantity=Decimal("1"), entry_price=Decimal("100")),
        )
        # 가격과 runtime 기준은 하드코딩된 UI 숫자 대신 실제 평가 입력을 사용해야 한다.
        for condition_id, owner, field, boundary, expected in (
            ("lower_price", "market", "realtime_price", "100", (True, True, False)),
            ("upper_safe_exit", "market", "realtime_price", "120", (False, True, True)),
            ("b_touch_bbw", "runtime", "touch_candle_bbw", "0.02", (True, False, False)),
            ("b_emergency_stop", "market", "realtime_price", "99", (True, True, False)),
            ("b_trend_slope", "market", "realtime_ema_slope", "0.08", (False, False, True)),
            ("c_new_low", "market", "realtime_price", "90", (True, False, False)),
            ("c_rebound", "market", "realtime_pct_b", "-0.24", (False, True, True)),
            ("c_entry_limit", "runtime", "entry_pct_b", "-0.15", (True, False, False)),
            ("c_handoff", "runtime", "case_c_exit_pct_b", "0.40", (True, False, False)),
            ("c_trail_increase", "market", "current_close_ema_slope", "0.2", (False, False, True)),
        ):
            for offset, expected_satisfied in zip(("-0.00000001", "0", "0.00000001"), expected):
                value = Decimal(boundary) + Decimal(offset)
                candidate = replace(context, **{owner: replace(getattr(context, owner), **{field: value})})
                with self.subTest(condition=condition_id, value=value):
                    self.assertIs(evaluate_condition(condition_id, candidate).satisfied, expected_satisfied)
