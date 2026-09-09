"""실거래 전 감사에서 확인한 상태·주문·시간 경계의 회귀를 검증한다."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
import unittest

from binance_auto_trader.application.trading_controller import TradingController
from binance_auto_trader.domain.trading.action_requests import (
    CancelPendingOrder, PatchRuntimeContext, QueueEvent, ReconcileOrder, SubmitOrder,
    ResetCaseCContext, OpenLowerEvent, CloseLowerEvent, ResetCaseBContext, patch,
)
from binance_auto_trader.domain.trading.context import (
    MarketEvaluationSnapshot, PositionSnapshot, TradingContext, TradingRuntimeSnapshot,
)
from binance_auto_trader.domain.trading.events import SellAttemptPayload, TradingEventType
from binance_auto_trader.domain.trading.states import (
    CaseBPositionState, CaseBSignalState, CaseCSignalState, ExitReason,
    OrderAttemptKind, OrderSide, OwnershipState, PositionReturnState, RootState,
    StrategyType, TradingStateConfiguration,
)
from binance_auto_trader.domain.trading.stm import TradingSTM
from binance_auto_trader.domain.common import RegimeType
from tests.unit.trading.test_stm import create_test_context, create_test_event, TEST_EVALUATION_TIME


def create_holding_stm(position_state=CaseBPositionState.CASE_B_HOLDING):
    """
    함수 이름: create_holding_stm()
    기능: 실제 체결 뒤의 Case B 상태를 통제된 입력으로 준비한다.
    인자: position_state -> 보유 substate
    반환값: TradingSTM
    작성 날짜: 2026/09/09
    """
    stm = TradingSTM(RegimeType.TYPE_0)
    stm._state = TradingStateConfiguration(
        root_state=RootState.TRADE_MANAGEMENT,
        ownership_state=OwnershipState.CASE_B_POSITION_MANAGEMENT,
        case_b_signal_state=CaseBSignalState.CASE_B_FINAL_STATE,
        case_c_signal_state=CaseCSignalState.CASE_C_FINAL_STATE,
        case_b_position_state=position_state,
    )
    return stm


class PreliveTradingRegressionTests(unittest.TestCase):
    """
    클래스 이름: PreliveTradingRegressionTests
    기능: 감사 보완의 신규 진입 차단과 청산 재시도를 검증한다.
    작성 날짜: 2026/09/09
    """

    def test_case_b_activation_uses_only_frozen_touch_bandwidth(self):
        """
        함수 이름: test_case_b_activation_uses_only_frozen_touch_bandwidth()
        기능: 공통 접촉 이후 B 활성화는 저가 없이 저장된 BBW만 읽고 최신 BBW를 재사용하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        for touch_bbw, latest_bbw, enabled in (("0.019", "0.03", True), ("0.02", "0.01", False)):
            with self.subTest(touch_bbw=touch_bbw):
                stm = TradingSTM(RegimeType.TYPE_0)
                stm._state = TradingStateConfiguration.create_trade_management_initial_state()
                context = create_test_context(
                    market=MarketEvaluationSnapshot(touch_candle_bbw=Decimal(latest_bbw)),
                    runtime=TradingRuntimeSnapshot(touch_candle_bbw=Decimal(touch_bbw)),
                )
                result = stm.handle(create_test_event(TradingEventType.ACTIVATE_TRADE_MANAGEMENT), context)
                self.assertIn("B-03" if enabled else "B-02", result.transition_ids)

    def test_new_candle_touch_uses_current_price_in_classifier_and_guard(self):
        """
        함수 이름: test_new_candle_touch_uses_current_price_in_classifier_and_guard()
        기능: 새 봉에서도 현재가 접촉으로 즉시 재추적하고 과거 저가만으로는 재진입하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        for price, touched in (("100.01", False), ("100", True), ("99.99", True)):
            with self.subTest(price=price):
                runtime = TradingRuntimeSnapshot(lower_event_id="old-event", touch_candle_id="old")
                market = MarketEvaluationSnapshot(realtime_price=Decimal(price), lower_band=Decimal("100"),
                    upper_band=Decimal("102"), current_30m_low=Decimal("99"), current_30m_candle_id="new")
                classifier = SimpleNamespace(_context=SimpleNamespace(runtime=runtime))
                event_type = TradingController._select_market_event_type(classifier, market)
                self.assertIs(event_type, TradingEventType.NEW_30M_LOWER_BAND_TOUCHED
                              if touched else TradingEventType.MARKET_DATA_UPDATED)
                stm = TradingSTM(RegimeType.TYPE_0)
                stm._state = TradingStateConfiguration.create_trade_management_initial_state()
                result = stm.handle(create_test_event(TradingEventType.NEW_30M_LOWER_BAND_TOUCHED),
                                    create_test_context(market=market, runtime=runtime))
                self.assertEqual("G-03" in result.transition_ids, touched)

    def test_trend_hold_uses_only_table_weakening_conditions(self):
        """
        함수 이름: test_trend_hold_uses_only_table_weakening_conditions()
        기능: PB-15/16은 보유 상태 손절·시간 조건과 무관하게 두 약화 조건으로만 분기한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        for slope_held, pct_held in ((False, False), (True, False), (False, True), (True, True)):
            for changes in (
                dict(realtime_price=Decimal("99")),
                dict(confirmed_30m_close=True, ema_slope_30m_close=Decimal("-0.09")),
                dict(holding_elapsed=timedelta(hours=6)),
            ):
                with self.subTest(slope=slope_held, pct=pct_held, changes=changes):
                    stm = create_holding_stm(CaseBPositionState.CASE_B_TREND_HOLD)
                    market = replace(MarketEvaluationSnapshot(
                        realtime_price=Decimal("100"),
                        realtime_slope_at_most_004_for_5s=slope_held,
                        pct_b_below_060_for_5s=pct_held,
                    ), **changes)
                    context = create_test_context(market=market,
                        position=PositionSnapshot(quantity=Decimal("1"), entry_price=Decimal("100")),
                        runtime=TradingRuntimeSnapshot(position_owner=StrategyType.CASE_B))
                    result = stm.handle(create_test_event(TradingEventType.MARKET_DATA_UPDATED), context)
                    weak = slope_held or pct_held
                    self.assertIn("PB-16" if weak else "PB-15", result.transition_ids)
                    self.assertEqual(
                        [TradingEventType.CASE_B_TREND_HOLD_SELL] if weak else [],
                        [action.event_type for action in result.action_requests if isinstance(action, QueueEvent)],
                    )
                    # 이전 보유 상태에서 지연 도착한 청산 이벤트도 Trend Hold에서는 주문을 만들지 않는다.
                    for event_type in (TradingEventType.CASE_B_EMERGENCY_STOP,
                                       TradingEventType.CASE_B_STOP, TradingEventType.CASE_B_TIME_EXIT):
                        stale = stm.handle(create_test_event(event_type), context)
                        self.assertFalse(stale.consumed)
                        self.assertFalse(any(isinstance(action, SubmitOrder) for action in stale.action_requests))

    def test_new_lower_close_is_evaluated_during_owner_or_recovery_lock(self):
        """
        함수 이름: test_new_lower_close_is_evaluated_during_owner_or_recovery_lock()
        기능: G-03 불가 시 Controller와 직접 STM 경로가 확정봉 손절을 누락하지 않는지 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        market = MarketEvaluationSnapshot(
            realtime_price=Decimal("99.5"), lower_band=Decimal("99.4"), upper_band=Decimal("101.4"),
            current_30m_low=Decimal("99.3"), confirmed_30m_close=True,
            current_30m_candle_id="new", ema_slope_30m_close=Decimal("-0.09"))
        runtime = TradingRuntimeSnapshot(lower_event_id="event", touch_candle_id="old", position_owner=StrategyType.CASE_B)
        classifier = SimpleNamespace(_context=SimpleNamespace(runtime=runtime))
        self.assertIs(TradingController._select_market_event_type(classifier, market), TradingEventType.MARKET_DATA_UPDATED)
        result = create_holding_stm().handle(create_test_event(TradingEventType.NEW_30M_LOWER_BAND_TOUCHED),
            create_test_context(market=market, runtime=runtime,
                                position=PositionSnapshot(quantity=Decimal("1"), entry_price=Decimal("100"))))
        self.assertIn(TradingEventType.CASE_B_STOP, [a.event_type for a in result.action_requests if isinstance(a, QueueEvent)])
        locked = replace(runtime, position_owner=None, case_c_consumed_for_event=True)
        classifier._context.runtime = locked
        self.assertIs(TradingController._select_market_event_type(classifier, market), TradingEventType.MARKET_DATA_UPDATED)
        classifier._context.runtime = replace(locked, case_c_recovery_confirmed=True)
        live = replace(market, confirmed_30m_close=False, realtime_price=Decimal("99.3"))
        self.assertIs(TradingController._select_market_event_type(classifier, live), TradingEventType.NEW_30M_LOWER_BAND_TOUCHED)

    def test_stop_cancels_buy_but_only_reconciles_sell(self):
        """
        함수 이름: test_stop_cancels_buy_but_only_reconciles_sell()
        기능: 일반 STOP의 pending 방향별 액션을 구분한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        for side in (OrderSide.BUY, OrderSide.SELL):
            runtime = TradingRuntimeSnapshot(pending_strategy=StrategyType.CASE_B, pending_order_side=side,
                                            pending_order_id="pending", pending_order_attempt_kind=OrderAttemptKind.INITIAL)
            result = create_holding_stm().handle(create_test_event(TradingEventType.STOP_CONFIRMED), create_test_context(runtime=runtime))
            self.assertEqual(side is OrderSide.BUY, any(isinstance(a, CancelPendingOrder) for a in result.action_requests))
            self.assertTrue(any(isinstance(a, ReconcileOrder) and a.stop_after_reconciliation for a in result.action_requests))

    def test_stop_reentry_keeps_setup_candle_but_next_candle_can_setup(self):
        """
        함수 이름: test_stop_reentry_keeps_setup_candle_but_next_candle_can_setup()
        기능: PB-23 초기화 뒤 같은 봉 setup은 차단하고 다음 봉에는 허용한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        context = TradingContext(clock=lambda: TEST_EVALUATION_TIME)
        context.update_market(MarketEvaluationSnapshot(realtime_price=Decimal("89"), lower_band=Decimal("90"),
            current_30m_low=Decimal("89"), current_30m_candle_id="same", realtime_pct_b=Decimal("-0.3"),
            cci_30m_realtime=Decimal("-150")))
        context.apply_runtime_patch(patch(last_case_c_setup_candle_id="same", case_b_exit_reason=ExitReason.STOP))
        stm = create_holding_stm(CaseBPositionState.CASE_B_CLOSED)
        result = stm.handle(create_test_event(TradingEventType.CASE_B_SELL_FINISHED), context.snapshot())
        handlers = {PatchRuntimeContext: context.apply_runtime_patch, ResetCaseBContext: context.reset_case_b_context,
                    ResetCaseCContext: context.reset_case_c_context, OpenLowerEvent: context.open_lower_event,
                    CloseLowerEvent: context.close_lower_event}
        for action in result.action_requests:
            if type(action) in handlers:
                handlers[type(action)](action)
        activation = stm.handle(create_test_event(TradingEventType.ACTIVATE_TRADE_MANAGEMENT), context.snapshot())
        self.assertNotIn("C-03", activation.transition_ids)
        context.update_market(replace(context.market, current_30m_candle_id="next"))
        next_setup = stm.handle(create_test_event(TradingEventType.RETRY_C_WAIT_SETUP), context.snapshot())
        self.assertIn("C-05", next_setup.transition_ids)
