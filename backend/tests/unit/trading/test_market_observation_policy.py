"""시장 관측의 접촉 판단과 명시적 event·내부 재평가의 호환 경계를 검증한다."""

from dataclasses import replace
from decimal import Decimal
import unittest

from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.trading.context import MarketEvaluationSnapshot, TradingRuntimeSnapshot
from binance_auto_trader.domain.trading.events import TradingEventType
from binance_auto_trader.domain.trading.states import (
    OrderAttemptKind, OrderSide, RootState, StrategyType, TradingStateConfiguration,
)
from binance_auto_trader.domain.trading.stm import TradingSTM
from tests.unit.trading.test_stm import create_market_observation, create_test_context


class MarketObservationPolicyTests(unittest.TestCase):
    """Controller 없이도 접촉 정책이 결정되고 기존 Case 평가와 분리되는지 검사한다."""

    def setUp(self):
        self.lower = MarketEvaluationSnapshot(
            realtime_price=Decimal("100"), lower_band=Decimal("100"), upper_band=Decimal("120"),
            current_30m_candle_id="new", touch_candle_bbw=Decimal("0.01"),
        )
        self.initial = TradingStateConfiguration.create_trade_management_initial_state()
        self.watching = TradingStateConfiguration(root_state=RootState.LOWER_TOUCH_WATCH)
        self.runtime = TradingRuntimeSnapshot(lower_event_id="old-event", touch_candle_id="old")

    def _evaluate(self, state, market, runtime, event=None):
        stm = TradingSTM(RegimeType.TYPE_0)
        stm._state = state
        return stm.handle(event or create_market_observation(market),
                          create_test_context(market=market, runtime=runtime))

    def test_observed_and_explicit_touches_produce_identical_transitions_and_actions(self):
        """접촉 경로별로 같은 ID·상태·액션 순서를 사용한다."""
        for state, market, runtime, explicit_type, transition_id in (
            (self.watching, self.lower, TradingRuntimeSnapshot(), TradingEventType.LOWER_BAND_TOUCHED, "G-02"),
            (self.initial, self.lower, self.runtime, TradingEventType.NEW_30M_LOWER_BAND_TOUCHED, "G-03"),
            (self.initial, replace(self.lower, realtime_price=Decimal("120")), self.runtime,
             TradingEventType.UPPER_BAND_TOUCHED, "G-07"),
        ):
            with self.subTest(transition=transition_id):
                observation = create_market_observation(market)
                explicit = replace(observation, event_type=explicit_type, market_evaluation=None, market_version=None)
                observed_result = self._evaluate(state, market, runtime, observation)
                explicit_result = self._evaluate(state, market, runtime, explicit)
                self.assertIn(transition_id, observed_result.transition_ids)
                self.assertEqual(explicit_result, observed_result)

    def test_only_observations_require_positive_bands(self):
        """0 placeholder를 실제 접촉으로 읽지 않고 명시적 event의 기존 조건은 유지한다."""
        for state, price, transition_id, explicit_type in (
            (self.watching, "0", "G-02", TradingEventType.LOWER_BAND_TOUCHED),
            (self.initial, "100", "G-07", TradingEventType.UPPER_BAND_TOUCHED),
        ):
            with self.subTest(transition=transition_id):
                market = MarketEvaluationSnapshot(realtime_price=Decimal(price))
                observation = create_market_observation(market)
                result = self._evaluate(state, market, TradingRuntimeSnapshot(), observation)
                self.assertNotIn(transition_id, result.transition_ids)
                explicit = replace(observation, event_type=explicit_type, market_evaluation=None, market_version=None)
                self.assertIn(transition_id, self._evaluate(state, market, TradingRuntimeSnapshot(), explicit).transition_ids)

    def test_internal_retries_do_not_reinterpret_old_market_as_new_band_touch(self):
        """스냅샷 없는 일반 재평가·Case timer는 G-02/G-03/G-07을 만들지 않는다."""
        for state, market in ((self.watching, self.lower), (self.initial, self.lower),
                              (self.initial, replace(self.lower, realtime_price=Decimal("120")))):
            for event_type in (TradingEventType.MARKET_DATA_UPDATED, TradingEventType.RETRY_C_WAIT_SETUP):
                with self.subTest(state=state.root_state, price=market.realtime_price, event=event_type):
                    event = replace(create_market_observation(market), event_type=event_type,
                                    market_evaluation=None, market_version=None)
                    result = self._evaluate(state, market, self.runtime, event)
                    self.assertFalse({"G-02", "G-03", "G-07"}.intersection(result.transition_ids))
                    self.assertEqual(state.root_state, result.state_after.root_state)

    def test_new_lower_touch_requires_new_candle_scope_and_eligible_runtime(self):
        """봉·소유권·pending·C 소비 및 회복 경계의 G-03 동작을 보존한다."""
        for candle_id, changes, allowed in (
            ("new", {}, True), ("old", {}, False), (None, {}, False),
            ("new", {"lower_event_id": None}, False),
            ("new", {"position_owner": StrategyType.CASE_B}, False),
            ("new", {"pending_order_id": "pending", "pending_strategy": StrategyType.CASE_B,
                     "pending_order_side": OrderSide.BUY, "pending_order_attempt_kind": OrderAttemptKind.INITIAL}, False),
            ("new", {"case_c_consumed_for_event": True}, False),
            ("new", {"case_c_consumed_for_event": True, "case_c_recovery_confirmed": True}, True),
        ):
            with self.subTest(candle=candle_id, runtime=changes):
                result = self._evaluate(self.initial, replace(self.lower, current_30m_candle_id=candle_id),
                                        replace(self.runtime, **changes))
                self.assertEqual(allowed, "G-03" in result.transition_ids)

    def test_upper_observation_preserves_buy_sell_and_preparation_intents(self):
        """주문 ID 발급 전후 모두 실제 시장 관측에서 G-07이 기존 주문 처리를 가로막지 않는다."""
        market = replace(self.lower, realtime_price=Decimal("121"))
        for strategy in StrategyType:
            for side in OrderSide:
                for order_id in (None, "pending-order"):
                    with self.subTest(strategy=strategy, side=side, order_id=order_id):
                        runtime = replace(self.runtime, pending_strategy=strategy, pending_order_side=side,
                                          pending_intent_id="preparing", pending_order_id=order_id,
                                          pending_order_attempt_kind=OrderAttemptKind.INITIAL)
                        result = self._evaluate(self.initial, market, runtime)
                        self.assertNotIn("G-07", result.transition_ids)
                        self.assertEqual(self.initial, result.state_after)
                        ordinary = replace(create_market_observation(market), market_evaluation=None, market_version=None)
                        self.assertEqual(self._evaluate(self.initial, market, runtime, ordinary), result)


if __name__ == "__main__":
    unittest.main()
