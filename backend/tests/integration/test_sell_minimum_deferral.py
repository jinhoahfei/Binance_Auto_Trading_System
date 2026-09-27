"""최소 주문 미달 이후 실제 전략·지표·종료 흐름을 외부 주문 없이 검증한다."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from binance_auto_trader.adapters.binance.mappers import SymbolFilterError, parse_symbol_trading_rules, prepare_market_order
from binance_auto_trader.application.shutdown_recovery import _clear_unsubmitted_intent
from binance_auto_trader.application.trading_controller import TradingSessionStatus
from binance_auto_trader.domain.trading.context import MarketEvaluationSnapshot
from binance_auto_trader.domain.trading.order import Order, OrderResult, OrderStatus
from binance_auto_trader.domain.trading.states import ExitReason, OrderSide, StrategyType
from tests.integration.active_trading_logic_replay import replay_market_evaluation
from tests.integration.phase13_risk_fixture import create_test_risk_policy
from tests.integration.test_buy_sell_flow import FakeOrderScenario, _create_buy_flow_fixture_with_risk_state
from tests.unit.binance.test_symbol_filters import _exchange_info_payload


class SellMinimumDeferralTests(unittest.TestCase):
    """
    클래스 이름: SellMinimumDeferralTests
    기능: 매수·매도 보류·새 시세·종료를 production STM과 가짜 체결로 검증한다.
    작성 날짜: 2026/09/27
    """

    def setUp(self) -> None:
        """
        함수 이름: setUp()
        기능: 네트워크를 차단하고 최대 9 USDT 매수와 최소 5 USDT 규칙을 준비한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        self.enterContext(patch("socket.create_connection", side_effect=AssertionError("external network forbidden")))
        directory = self.enterContext(TemporaryDirectory())
        policy = replace(create_test_risk_policy(), max_order_notional=Decimal("9"))
        self.fixture = _create_buy_flow_fixture_with_risk_state(directory, FakeOrderScenario.IMMEDIATE_FILLED, policy)
        self.controller = self.fixture.controller
        self.addCleanup(self.controller.close_session_resources)
        self.controller.update_split_ratios(command_id="half-sell", expected_version=self.controller.context.version,
            scale_in=Decimal("0.5"), scale_out=Decimal("0.5"))
        self.rules = parse_symbol_trading_rules(_exchange_info_payload(
            market_step="0.0001", market_minimum="0.0001", minimum_notional="5"), "ETHUSDT")
        self.prepared = []
        self.enterContext(patch.object(self.controller._api_gateway, "prepare_order", side_effect=self._prepare))
        self._original_submit = self.fixture.rest_client.submit_order
        self.enterContext(patch.object(self.fixture.rest_client, "submit_order", side_effect=self._submit))
        self.market = MarketEvaluationSnapshot(realtime_price=Decimal("4320"), lower_band=Decimal("4350"),
            upper_band=Decimal("9000"), realtime_pct_b=Decimal("-0.3"),
            current_30m_candle_id="ETHUSDT:30m:sell-minimum", current_30m_low=Decimal("4310"),
            current_30m_high=Decimal("4360"), touch_candle_bbw=Decimal("0.01"))

    def _submit(self, *, order):
        """
        함수 이름: _submit()
        기능: 한 주문용 기존 fixture에 주문별 고유 체결 ID를 부여한다.
        인자: order -> 가짜 거래소에 제출할 주문
        반환값: 같은 주문의 고유 FILLED 결과
        작성 날짜: 2026/09/27
        """
        result = self._original_submit(order=order)
        exchange_id = str(1000 + len(self.fixture.rest_client.submitted_orders))
        return replace(result, exchange_order_id=exchange_id,
            fills=tuple(replace(fill, exchange_order_id=exchange_id) for fill in result.fills))

    def _prepare(self, order):
        """
        함수 이름: _prepare()
        기능: 추가 네트워크 없이 실제 수량·최소금액 필터를 적용한다.
        인자: order -> 후보 주문
        반환값: 필터 적용 주문
        작성 날짜: 2026/09/27
        """
        self.prepared.append(order)
        return prepare_market_order(order, self.rules, reference_price=order.market_price_at_decision)

    def _buy(self, strategy: StrategyType) -> None:
        """
        함수 이름: _buy()
        기능: B 신호·눌림 또는 C flush·회복으로 실제 매수 체결을 만든다.
        인자: strategy -> 매수할 전략
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        self.market = replace(self.market, cci_30m_realtime=Decimal("-150") if strategy is StrategyType.CASE_C else Decimal("0"))
        replay_market_evaluation(self.fixture, self.market, "lower")
        if strategy is StrategyType.CASE_B:
            self.market = replace(self.market, realtime_price=Decimal("4400"), realtime_pct_b=Decimal("0.5"),
                confirmed_30m_close=True, current_30m_candle_id="ETHUSDT:30m:signal", current_30m_low=Decimal("4360"),
                pct_b_close=Decimal("0.5"), current_closed_candle_low=Decimal("4360"),
                previous_3_closed_candle_lows=(Decimal("4310"), Decimal("4320"), Decimal("4330")))
            replay_market_evaluation(self.fixture, self.market, "signal")
        self.market = replace(self.market, realtime_price=Decimal("4380") if strategy is StrategyType.CASE_B else Decimal("4327"),
            realtime_pct_b=Decimal("0.2") if strategy is StrategyType.CASE_B else Decimal("-0.24"), confirmed_30m_close=False)
        replay_market_evaluation(self.fixture, self.market, "buy")
        self.assertIs(self.fixture.position.owner, strategy)
        self.assertEqual(len(self.fixture.rest_client.submitted_orders), 1)

    def _exit_market(self, strategy: StrategyType) -> MarketEvaluationSnapshot:
        """
        함수 이름: _exit_market()
        기능: B 익절과 C 손절 조건을 독립적으로 구성한다.
        인자: strategy -> 보유 전략
        반환값: 매도 조건을 만족하는 평가 입력
        작성 날짜: 2026/09/27
        """
        if strategy is StrategyType.CASE_B:
            return replace(self.market, realtime_pct_b=Decimal("0.65"), realtime_ema_slope=Decimal("0.05"),
                pct_b_at_least_060_for_5s=True)
        return replace(self.market, realtime_pct_b=Decimal("0.05"), realtime_ema_slope=Decimal("-0.6"),
            realtime_slope_at_most_minus_055_for_3m=True)

    def test_half_sell_is_deferred_and_new_indicators_and_stop_continue(self) -> None:
        """
        함수 이름: test_half_sell_is_deferred_and_new_indicators_and_stop_continue()
        기능: 사고 순서에서 비율·포지션을 보존하고 지표 갱신 후 STOP 전량 청산만 한 번 허용한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        self._buy(StrategyType.CASE_B)
        position = self.fixture.position.get_snapshot()
        entered_at = self.fixture.position.entered_at
        market = self._exit_market(StrategyType.CASE_B)
        replay_market_evaluation(self.fixture, market, "minimum-sell")
        self.assertIs(self.controller.status, TradingSessionStatus.RUNNING)
        self.assertIsNone(self.controller.context.runtime.pending_intent_id)
        self.assertIsNone(self.controller.context.runtime.pending_return_state)
        self.assertEqual(self.fixture.position.get_snapshot(), position)
        self.assertEqual(self.fixture.position.entered_at, entered_at)
        self.assertEqual(self.controller.context.scale_out_ratio, Decimal("0.5"))
        self.assertEqual(len(self.fixture.rest_client.submitted_orders), 1)
        self.assertEqual(len(self.prepared), 2)
        self.assertIn((StrategyType.CASE_B, ExitReason.TAKE_PROFIT), self.controller._sell_minimum_waits)
        for index in range(3):
            replay_market_evaluation(self.fixture, replace(market, realtime_pct_b=Decimal("0.66") + index / Decimal("100")), f"wait-{index}")
        self.assertEqual(len(self.prepared), 2)
        rows = self.controller.snapshot_session().active_logic.indicators.conditions
        case_rows = [row for row in rows if row.slot.strategy is StrategyType.CASE_B]
        self.assertEqual(len(case_rows), 6)
        self.assertTrue(all(row.evaluation_state == "active" for row in case_rows))
        self.assertEqual(next(row.condition.value for row in case_rows if row.slot.condition_id == "b_profit_zone"), Decimal("0.68"))
        self.controller.stop_trading(command_id="stop-after-minimum", expected_version=self.controller.context.version)
        asyncio.run(self.controller.drain_events())
        self.assertEqual(len(self.fixture.rest_client.submitted_orders), 2)
        self.assertEqual(self.fixture.rest_client.submitted_orders[-1].exit_reason, ExitReason.STOP)
        self.assertEqual(self.fixture.position.quantity, 0)
        self.assertIs(self.controller.status, TradingSessionStatus.TERMINATED)

    def test_case_c_deferred_exit_keeps_owner_and_clears_exit_provenance(self) -> None:
        """
        함수 이름: test_case_c_deferred_exit_keeps_owner_and_clears_exit_provenance()
        기능: C 손절 보류가 소유권·보유 시각을 유지하고 새 판단 %B를 허용하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        self._buy(StrategyType.CASE_C)
        original = self.fixture.position.get_snapshot()
        market = self._exit_market(StrategyType.CASE_C)
        replay_market_evaluation(self.fixture, market, "c-minimum")
        self.assertIs(self.controller.status, TradingSessionStatus.RUNNING)
        self.assertEqual(self.fixture.position.get_snapshot(), original)
        self.assertIsNone(self.controller.context.runtime.pending_exit_pct_b)
        self.assertIsNone(self.controller.context.runtime.pending_exit_reason)
        self.assertEqual(len(self.prepared), 2)
        replay_market_evaluation(self.fixture, replace(market, realtime_ema_slope=Decimal("-0.7")), "c-next")
        rows = self.controller.snapshot_session().active_logic.indicators.conditions
        self.assertEqual(next(row.condition.value for row in rows if row.slot.condition_id == "c_stop"), Decimal("-0.7"))
        self.assertEqual(len(self.prepared), 2)

    def test_retry_deadline_and_different_exit_are_independent(self) -> None:
        """
        함수 이름: test_retry_deadline_and_different_exit_are_independent()
        기능: 동일 사유는 30초 뒤 재검사하고 비상손절은 즉시 별도 검사를 수행한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        self._buy(StrategyType.CASE_B)
        market = self._exit_market(StrategyType.CASE_B)
        replay_market_evaluation(self.fixture, market, "take-profit")
        self.fixture.clock.advance(timedelta(seconds=28))
        replay_market_evaluation(self.fixture, market, "before-deadline")
        self.assertEqual(len(self.prepared), 2)
        replay_market_evaluation(self.fixture, market, "at-deadline")
        self.assertEqual(len(self.prepared), 3)
        replay_market_evaluation(self.fixture, replace(market, realtime_price=Decimal("4000")), "emergency")
        self.assertEqual(len(self.prepared), 4)
        self.assertEqual(self.prepared[-1].exit_reason, ExitReason.EMERGENCY_STOP)
        self.assertIs(self.controller.status, TradingSessionStatus.RUNNING)

    def test_ratio_change_bypasses_wait_without_automatic_quantity_increase(self) -> None:
        """
        함수 이름: test_ratio_change_bypasses_wait_without_automatic_quantity_increase()
        기능: 사용자의 명시적인 비율 변경만 미달 주문의 즉시 재검사를 허용한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        self._buy(StrategyType.CASE_B)
        market = self._exit_market(StrategyType.CASE_B)
        replay_market_evaluation(self.fixture, market, "wait")
        self.controller.update_split_ratios(command_id="user-changed-ratio", expected_version=self.controller.context.version,
            scale_in=Decimal("0.5"), scale_out=Decimal("1"))
        replay_market_evaluation(self.fixture, market, "ratio-changed")
        self.assertEqual(len(self.fixture.rest_client.submitted_orders), 2)
        self.assertEqual(self.fixture.position.quantity, 0)

    def test_other_filter_error_remains_blocked_with_last_indicators(self) -> None:
        """
        함수 이름: test_other_filter_error_remains_blocked_with_last_indicators()
        기능: 최소 조건 이외의 오류는 중단을 유지하고 모든 보유 지표를 보존한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        self._buy(StrategyType.CASE_B)
        replay_market_evaluation(self.fixture, replace(self.market, confirmed_30m_close=True), "holding")
        with patch.object(self.controller._api_gateway, "prepare_order", side_effect=SymbolFilterError("FILTER_CONFIGURED_MAXIMUM_NOTIONAL")):
            replay_market_evaluation(self.fixture, self._exit_market(StrategyType.CASE_B), "other-filter")
        self.assertIs(self.controller.status, TradingSessionStatus.RECONCILIATION_REQUIRED)
        snapshot = self.controller.snapshot_session().active_logic.indicators
        self.assertEqual(len(snapshot.conditions), 7)
        self.assertTrue(all(row.evaluation_state == "paused" for row in snapshot.conditions))
        self.assertTrue(all(row.evaluated_at is not None for row in snapshot.conditions))
        self.fixture.clock.advance(timedelta(minutes=5))
        self.assertEqual(snapshot, self.controller.snapshot_session().active_logic.indicators)
        self.assertEqual(len(self.fixture.rest_client.submitted_orders), 1)

    def test_zero_quantity_records_non_submission_before_reconciliation(self) -> None:
        """
        함수 이름: test_zero_quantity_records_non_submission_before_reconciliation()
        기능: 잔고 불일치의 0 수량은 포지션을 보존하며 종료에서 미제출임을 구별한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        self._buy(StrategyType.CASE_B)
        position = self.fixture.position.get_snapshot()
        with patch.object(self.controller, "_calculate_order_quantity", return_value=Decimal("0")):
            replay_market_evaluation(self.fixture, self._exit_market(StrategyType.CASE_B), "zero-quantity")
        self.assertEqual(self.controller._unsubmitted_preparation_intent, self.controller.context.runtime.pending_intent_id)
        self.assertIsNotNone(self.controller._unsubmitted_preparation_intent)
        with patch.object(self.controller._api_gateway, "get_order_submission_attempt_evidence", return_value=None):
            _clear_unsubmitted_intent(self.controller)
        self.assertIsNone(self.controller.context.runtime.pending_return_state)
        self.assertIsNone(self.controller.context.runtime.pending_intent_id)
        self.assertEqual(self.fixture.position.get_snapshot(), position)
        self.assertEqual(len(self.fixture.rest_client.submitted_orders), 1)

    def test_lost_sell_response_queries_same_order_without_resubmission(self) -> None:
        """
        함수 이름: test_lost_sell_response_queries_same_order_without_resubmission()
        기능: 실제 체결 응답을 잃은 일반 매도는 같은 주문을 대조하고 중복 체결을 반영하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        self._buy(StrategyType.CASE_B)
        self.controller.update_split_ratios(command_id="full-sell-for-response-test",
            expected_version=self.controller.context.version, scale_in=Decimal("0.5"), scale_out=Decimal("1"))
        exchange_results = []

        def lose_response(*, order):
            """
            함수 이름: lose_response()
            기능: 가짜 거래소 체결은 보존하고 호출자에게는 전송 응답 유실만 전달한다.
            인자: order -> 제출한 동일 주문
            반환값: 없음, TimeoutError 발생
            작성 날짜: 2026/09/27
            """
            exchange_results.append(self._submit(order=order))
            raise TimeoutError("sell response lost")

        with patch.object(self.fixture.rest_client, "submit_order", side_effect=lose_response):
            replay_market_evaluation(self.fixture, self._exit_market(StrategyType.CASE_B), "lost-sell-response")
        sell = self.fixture.rest_client.submitted_orders[-1]
        self.assertIs(sell.status, OrderStatus.UNKNOWN)
        self.assertEqual(len(self.fixture.rest_client.submitted_orders), 2)
        self.assertIsNone(self.controller._unsubmitted_preparation_intent)
        rows = self.controller.snapshot_session().active_logic.indicators.conditions
        self.assertEqual(len(rows), 7)
        self.assertTrue(all(row.evaluation_state == "paused" for row in rows if row.slot.strategy is StrategyType.CASE_B))
        unknown = OrderResult(symbol=sell.symbol, client_order_id=sell.client_order_id,
            status=OrderStatus.UNKNOWN, processed_at=self.fixture.clock())
        with patch.object(self.fixture.rest_client, "query_order_result", side_effect=[unknown, exchange_results[0]]) as query:
            self.fixture.clock.advance(timedelta(seconds=1))
            self.assertEqual(self.controller.trigger_order_reconciliation(), ())
            self.assertEqual(len(self.fixture.rest_client.submitted_orders), 2)
            self.fixture.clock.advance(timedelta(seconds=2))
            self.controller._enqueue_order_outcomes(self.controller.trigger_order_reconciliation())
            asyncio.run(self.controller.drain_events())
            self.assertEqual([call.kwargs["order"] for call in query.call_args_list], [sell, sell])
        self.assertEqual(self.fixture.position.quantity, 0)
        self.assertEqual(len(self.fixture.rest_client.submitted_orders), 2)
        self.assertEqual([trade.side for trade in self.fixture.history_controller.trade_history.trades],
            [OrderSide.BUY, OrderSide.SELL])

    def test_minimum_boundary_rounding_and_fee_adjusted_half_quantity(self) -> None:
        """
        함수 이름: test_minimum_boundary_rounding_and_fee_adjusted_half_quantity()
        기능: 정확한 최소금액·내림·수수료 차감 후 반액의 제출 가능 경계를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        self._buy(StrategyType.CASE_B)
        for quantity, expected in (("0.00199999", None), ("0.002", "0.0020"),
                                   ("0.0021", "0.0021"), ("0.001998", None), ("0.00005", None)):
            with self.subTest(quantity=quantity):
                order = Order(intent_id="boundary", client_order_id="boundary-0", submission_attempt=0,
                    symbol="ETHUSDT", side=OrderSide.SELL, strategy=StrategyType.CASE_B,
                    regime_type=self.controller._active_stm.regime_type, requested_quantity=Decimal(quantity),
                    submitted_quantity=Decimal(quantity), market_price_at_decision=Decimal("2500"), exit_reason=ExitReason.TAKE_PROFIT)
                if expected is None:
                    with self.assertRaises(SymbolFilterError):
                        self._prepare(order)
                else:
                    self.assertEqual(self._prepare(order).submitted_quantity, Decimal(expected))
        self.assertEqual(len(self.fixture.rest_client.submitted_orders), 1)

    def test_balance_rules_and_local_minimum_pass_bypass_wait(self) -> None:
        """
        함수 이름: test_balance_rules_and_local_minimum_pass_bypass_wait()
        기능: 동일 대기 중 잔고·규칙 변경과 로컬 최소 조건 해소를 즉시 재검사한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        self._buy(StrategyType.CASE_B)
        market = self._exit_market(StrategyType.CASE_B)
        with patch.object(self.controller._api_gateway, "get_cached_symbol_trading_rules", side_effect=lambda: self.rules), patch.object(
            self.controller._api_gateway, "preview_cached_sell_quantity", return_value=Decimal("0"),
        ) as preview:
            replay_market_evaluation(self.fixture, market, "wait")
            self.rules = replace(self.rules, base_asset_precision=self.rules.base_asset_precision + 1)
            replay_market_evaluation(self.fixture, market, "new-rules")
            self.assertEqual(len(self.prepared), 3)
            free_balance = self.controller._get_effective_free_balance("ETH")
            with patch.object(self.controller, "_get_effective_free_balance", return_value=free_balance + Decimal("0.1")):
                replay_market_evaluation(self.fixture, market, "new-balance")
                self.assertEqual(len(self.prepared), 4)
                preview.return_value = Decimal("0.001")
                replay_market_evaluation(self.fixture, market, "local-minimum-pass")
                self.assertEqual(len(self.prepared), 5)
        self.assertEqual(len(self.fixture.rest_client.submitted_orders), 1)
