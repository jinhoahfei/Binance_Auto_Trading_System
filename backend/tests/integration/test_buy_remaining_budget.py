"""2026-09-18 잔여 ETH 차단 사고를 실제 수량 필터·주문·체결 경로에서 재현한다."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, localcontext
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import PropertyMock, patch

from binance_auto_trader.application.runtime_diagnostics import RuntimeDiagnostics
from binance_auto_trader.application.trading_controller import TradingController, TradingSessionStatus
from binance_auto_trader.bootstrap.live_configuration import create_live_risk_policy
from binance_auto_trader.domain.trading import RiskBlockReason, TradingEvent, TradingEventType
from binance_auto_trader.domain.trading.action_requests import SubmitOrder, patch as context_patch
from binance_auto_trader.domain.trading.risk import RiskDecision
from binance_auto_trader.domain.trading.states import (
    CaseBSignalState, CaseCSignalState, OrderAttemptKind, OrderSide, OwnershipState,
    RootState, StrategyType, TradingPhase,
)
from tests.integration.test_buy_sell_flow import (
    FakeOrderScenario, _create_buy_flow_fixture_with_risk_state, _drain_controller, _execute_case_b_buy,
)
from tests.unit.binance.test_spot_rest_client import (
    QueueHTTPTransport, _client, _preparation_responses, _reference_price_payload,
)
from tests.unit.binance.test_symbol_filters import _exchange_info_payload, _order


class RemainingBuyBudgetTests(unittest.TestCase):
    """클래스 이름: RemainingBuyBudgetTests
    기능: 위험 한도를 늘리지 않고 실제 가능한 BUY만 실행되는지 검증한다.
    작성 날짜: 2026/09/18
    """

    def setUp(self):
        """테스트 전용 저장소와 실제 필터를 사용하는 오프라인 주문 경계를 준비한다."""
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.f = _create_buy_flow_fixture_with_risk_state(
            directory.name, FakeOrderScenario.IMMEDIATE_FILLED, create_live_risk_policy(),
        )
        self.c = self.f.controller
        self.addCleanup(self.c.close_session_resources)
        self.c._maximum_order_notional = Decimal("10")
        self.price = Decimal("2446.43")
        self.c._latest_market_evaluation_version = self.c._market_snapshot.version
        self.c._context.update_market(replace(self.c.context.market,
            realtime_price=self.price, realtime_pct_b=Decimal("0.29")))
        self.c._context.apply_runtime_patch(context_patch(signal_created=True, signal_time=self.f.clock()))
        self.c._active_stm._state = replace(self.c._active_stm.current_state,
            root_state=RootState.TRADE_MANAGEMENT, ownership_state=OwnershipState.NO_POSITION,
            case_b_signal_state=CaseBSignalState.B_POSITION_OPEN_SIGNALLED,
            case_c_signal_state=CaseCSignalState.C_WAIT_SETUP)
        self.records = []
        self.c._diagnostics = RuntimeDiagnostics(self.records.append)
        self.residual = patch.object(TradingController, "residual_totals", new_callable=PropertyMock)
        self.residual_value = self.residual.start()
        self.addCleanup(self.residual.stop)
        self.residual_value.return_value = (Decimal("0.000096"), Decimal("0.234862702702702702702702702702703"))
        self.install_filters("5")

    def install_filters(self, minimum, *, repeats=1):
        """실제 REST 준비 요청의 응답만 메모리에서 제공한다."""
        responses = []
        for index in range(repeats):
            rules = _exchange_info_payload(
                market_step="0.0001", market_minimum="0.0001", minimum_notional=minimum)
            for rule in rules["symbols"][0]["filters"]:
                if rule["filterType"] == "MIN_NOTIONAL":
                    rule["minNotional"] = minimum
            responses.extend(_preparation_responses(
                exchange_info_payload=rules,
                reference_price_payload=_reference_price_payload(price=str(self.price)),
                include_server_time=index == 0,
            ))
        self.transport = QueueHTTPTransport(responses)
        self.adapter = _client(self.transport, maximum_order_notional=Decimal("10"))
        self.f.rest_client.prepare_order = lambda *, order: self.adapter.prepare_order(order)
        self.f.rest_client.preview_cached_buy_quantity = self.adapter.preview_cached_buy_quantity
        self.f.rest_client.discard_unsubmitted_preparation = self.adapter.discard_unsubmitted_preparation

    def buy(self, intent="incident-buy"):
        """동일 production action 경로에 의도와 실제 queue feedback을 전달한다."""
        return _execute_case_b_buy(self.f, intent)

    def test_incident_resizes_and_records_one_fill(self):
        """잔여 0.000096 ETH 때문에 0.004 대신 0.0039 ETH를 매수한다."""
        outcomes = self.buy()
        self.c._enqueue_order_outcomes(outcomes)
        _drain_controller(self.c)
        self.assertEqual(len(self.f.rest_client.submitted_orders), 1)
        order = self.f.rest_client.submitted_orders[0]
        self.assertEqual(order.submitted_quantity, Decimal("0.0039"))
        self.assertEqual(self.f.position.quantity, Decimal("0.0039"))
        self.assertEqual(len(self.f.repository.get_trade_history()), 1)
        budget = self.c.last_risk_decision.budget
        self.assertTrue(self.c.last_risk_decision.allowed)
        self.assertEqual(budget.current_position_notional, Decimal("0.23485728"))
        self.assertEqual(budget.remaining_position_notional, Decimal("9.76514272"))
        self.assertEqual(budget.candidate_order_notional, Decimal("9.541077"))
        self.assertEqual(budget.projected_position_notional, Decimal("9.77593428"))
        self.assertEqual(budget.evaluated_at, self.f.clock())
        for index in range(31):
            self.c.enqueue_event(TradingEvent(event_type=TradingEventType.MARKET_DATA_UPDATED,
                occurred_at=self.f.clock(), event_id=f"after-fill:{index}"))
            _drain_controller(self.c)
        self.assertEqual(len(self.f.rest_client.submitted_orders), 1)

    def test_minimum_failure_waits_without_repeated_preparation(self):
        """31회 반복 후보도 동일 최소 금액 미달의 원격 준비는 한 번만 수행한다."""
        self.install_filters("10", repeats=2)
        for index in range(31):
            outcomes = self.buy(f"blocked:{index}")
            self.assertEqual(outcomes[0].payload.reason, RiskBlockReason.RISK_BUY_BUDGET_INSUFFICIENT)
        self.assertEqual(len(self.transport.requests), 6)
        self.assertEqual(self.f.rest_client.submitted_orders, [])
        self.assertEqual(self.c._submission_attempts_by_intent, {})
        self.assertEqual(self.adapter._prepared_orders_by_client_id, {})
        self.assertEqual(self.adapter._preparation_filter_evidence_by_client_id, {})
        self.assertIs(self.c.status, TradingSessionStatus.RUNNING)
        self.assertEqual(sum(r['event'] == 'risk_evaluated' for r in self.records), 1)
        self.f.clock.advance(timedelta(seconds=30))
        self.buy("filter-refresh")
        self.assertEqual(len(self.transport.requests), 11)

    def test_budget_becoming_viable_resumes_immediately(self):
        """잔여 예산이 늘면 보류 재확인 주기를 기다리지 않고 fresh 준비를 수행한다."""
        self.residual_value.return_value = (Decimal("0.003"), Decimal("7.3"))
        self.install_filters("5", repeats=2)
        self.assertEqual(self.buy()[0].payload.reason, RiskBlockReason.RISK_BUY_BUDGET_INSUFFICIENT)
        self.residual_value.return_value = (Decimal("0.000096"), Decimal("0.2348"))
        self.buy("funding-restored")
        self.assertEqual(len(self.f.rest_client.submitted_orders), 1)
        self.assertEqual(self.c._buy_budget_waits, {})
        self.assertEqual(len(self.transport.requests), 11)

    def test_price_improving_minimum_condition_rechecks_before_deadline(self):
        """계좌 변경이 없어도 가격·수량 격자가 최소 주문 조건을 충족하면 즉시 재평가한다."""
        self.residual_value.return_value = (Decimal("0.002"), Decimal("4.9"))
        self.install_filters("5", repeats=2)
        self.assertEqual(self.buy()[0].payload.reason, RiskBlockReason.RISK_BUY_BUDGET_INSUFFICIENT)
        self.c._context.update_market(replace(self.c.context.market, realtime_price=Decimal("2300")))
        self.buy("price-improved")
        self.assertEqual(len(self.transport.requests), 11)
        self.assertEqual(self.f.rest_client.submitted_orders[0].submitted_quantity, Decimal("0.0023"))
        self.assertEqual(self.c.last_risk_decision.budget.projected_position_notional, Decimal("9.89"))

    def test_filter_change_is_observed_at_thirty_second_recheck(self):
        """같은 신호·잔고에서도 30초 뒤 최신 거래소 필터를 확인하고 보류를 해소한다."""
        self.install_filters("10")
        self.buy()
        self.transport.responses.extend(_preparation_responses(
            exchange_info_payload=_exchange_info_payload(
                market_step="0.0001", market_minimum="0.0001", minimum_notional="5"),
            reference_price_payload=_reference_price_payload(price=str(self.price)),
            include_server_time=False))
        self.f.clock.advance(timedelta(seconds=29))
        self.buy("before-refresh")
        self.assertEqual(len(self.transport.requests), 6)
        self.f.clock.advance(timedelta(seconds=1))
        self.buy("fresh-filter")
        self.assertEqual(len(self.transport.requests), 11)
        self.assertEqual(len(self.f.rest_client.submitted_orders), 1)
        self.assertEqual(self.c._buy_budget_waits, {})

    def test_quantity_rounding_to_zero_is_a_normal_budget_wait(self):
        """잔여 예산이 양수라도 수량 단위 내림 후 0이면 전송 없이 보류한다."""
        self.price = Decimal("2500")
        self.c._context.update_market(replace(self.c.context.market, realtime_price=self.price))
        self.residual_value.return_value = (Decimal("0.003999"), Decimal("9.9975"))
        self.install_filters("5")
        for index in range(3):
            self.assertEqual(self.buy(f"quantity-zero:{index}")[0].payload.reason,
                             RiskBlockReason.RISK_BUY_BUDGET_INSUFFICIENT)
        self.assertEqual(len(self.transport.requests), 6)
        self.assertEqual(self.c._order_states_by_client_id, {})
        self.assertEqual(self.c._submission_attempts_by_intent, {})
        self.assertIs(self.c.status, TradingSessionStatus.RUNNING)

    def test_zero_budget_does_not_query_prepare_or_require_reconciliation(self):
        """한도를 이미 차지한 잔고는 원격 조회·journal 없이 감시를 계속한다."""
        self.price = Decimal("2500")
        self.c._context.update_market(replace(self.c.context.market, realtime_price=self.price))
        self.residual_value.return_value = (Decimal("0.004"), Decimal("10"))
        self.buy()
        self.assertEqual(self.transport.requests, [])
        self.assertEqual(self.c._submission_attempts_by_intent, {})
        self.assertIs(self.c.status, TradingSessionStatus.RUNNING)
        self.assertFalse(self.c.last_risk_decision.allowed)
        self.assertIs(self.c.last_risk_decision.block_reason, RiskBlockReason.RISK_BUY_BUDGET_INSUFFICIENT)

    def test_policy_and_manual_kill_still_block_before_preparation(self):
        """사전 수량 조정이 정책·긴급정지 gate를 우회하지 않는다."""
        self.c._manual_kill_active = True
        self.assertEqual(self.buy()[0].payload.reason, RiskBlockReason.MANUAL_KILL_SWITCH_ACTIVE)
        self.assertEqual(self.transport.requests, [])

    def test_final_risk_guard_rejects_oversized_candidate(self):
        """자동 축소와 별도로 최종 위험 gate는 과도한 후보를 계속 거절한다."""
        order = _order(quantity="0.004", market_price=str(self.price))
        self.assertEqual(self.c._evaluate_buy_order_risk(order).block_reason,
                         RiskBlockReason.RISK_POSITION_NOTIONAL_EXCEEDED)

    def test_post_preparation_rejection_discards_only_unsubmitted_cache(self):
        """준비 뒤 정책이 바뀌어 거절돼도 준비 객체가 누적되지 않는다."""
        original = self.c._evaluate_buy_order_risk
        def reject(order, **kwargs):
            allowed = original(order, **kwargs)
            return RiskDecision(False, allowed.budget, RiskBlockReason.MANUAL_KILL_SWITCH_ACTIVE)
        with patch.object(self.c, '_evaluate_buy_order_risk', side_effect=reject):
            self.buy()
        self.assertEqual(self.adapter._prepared_orders_by_client_id, {})
        self.assertEqual(self.adapter._preparation_filter_evidence_by_client_id, {})
        self.assertEqual(self.c._submission_attempts_by_intent, {})

    def test_stop_clears_wait_and_preserves_trade_history(self):
        """정상 보류 중 STOP은 거래·잔여 장부 변경 없이 대기를 정리한다."""
        self.install_filters("10")
        self.c._enqueue_order_outcomes(self.buy())
        _drain_controller(self.c)
        self.assertTrue(self.c._buy_budget_waits)
        self.c.stop_trading(command_id="stop-budget-wait", expected_version=self.c.context.version)
        self.assertEqual(self.c._buy_budget_waits, {})
        self.assertEqual(self.f.repository.get_trade_history(), ())

    def test_quantity_override_obeys_remaining_budget_and_decimal_context(self):
        """부분체결 재시도 수량도 남은 예산 안으로 내리고 전역 정밀도에 독립적이다."""
        with localcontext() as context:
            context.prec = 8
            limited = self.c._limit_buy_quantity(Decimal("0.004"), self.price)
        with localcontext() as context:
            context.prec = 34
            self.assertLessEqual(limited * self.price, Decimal("9.76514272"))

    def test_partial_fill_position_and_unfilled_reservation_share_one_budget(self):
        """부분체결분과 미체결분을 각각 합산하고 명시적 재시도 수량도 축소한다."""
        self.f.rest_client.scenario = FakeOrderScenario.PARTIALS_THEN_FILLED
        self.install_filters("1", repeats=2)
        self.c._quantity_overrides_by_intent["partial-first"] = Decimal("0.001")
        self.buy("partial-first")
        filled = self.f.position.quantity
        self.assertGreater(filled, Decimal("0"))
        self.f.rest_client.scenario = FakeOrderScenario.NEW_THEN_FILLED
        self.c._quantity_overrides_by_intent["partial-retry"] = Decimal("0.004")
        self.buy("partial-retry")
        budget = self.c.last_risk_decision.budget
        self.assertEqual(budget.strategy_position_notional, filled * self.price)
        self.assertEqual(budget.reserved_buy_notional, (Decimal("0.001") - filled) * self.price)
        self.assertEqual(budget.residual_position_notional, Decimal("0.23485728"))
        self.assertEqual(self.f.rest_client.submitted_orders[1].submitted_quantity, Decimal("0.0029"))
        self.assertLessEqual(budget.projected_position_notional, Decimal("10"))

    def test_expired_case_b_wait_is_cleared_and_cannot_resume(self):
        """예산이 회복돼도 3시간이 지난 Case B 신호로 주문하지 않는다."""
        self.install_filters("10")
        self.c._enqueue_order_outcomes(self.buy())
        _drain_controller(self.c)
        self.assertTrue(self.c._buy_budget_waits)
        self.f.clock.advance(timedelta(hours=3, seconds=1))
        self.residual_value.return_value = (Decimal("0"), Decimal("0"))
        self.c.enqueue_event(TradingEvent(event_type=TradingEventType.MARKET_DATA_UPDATED,
            occurred_at=self.f.clock(), event_id="expired-signal-budget-restored"))
        _drain_controller(self.c)
        self.assertEqual(self.c._buy_budget_waits, {})
        self.assertEqual(self.f.rest_client.submitted_orders, [])
        self.assertEqual(len(self.transport.requests), 6)

    def test_case_c_budget_wait_returns_to_setup_and_expires(self):
        """Case C도 최소 금액 미달을 정상 setup 감시로 되돌리고 180초 뒤 정리한다."""
        self.install_filters("10")
        self.c._active_stm._state = replace(self.c._active_stm.current_state,
            case_b_signal_state=CaseBSignalState.B_WAIT_PULLBACK,
            case_c_signal_state=CaseCSignalState.C_POSITION_OPEN_SIGNALLED)
        self.c._context.apply_runtime_patch(context_patch(
            allow_new_case_c_setup=True, case_c_consumed_for_event=False,
            timer_base_time=self.f.clock(), pending_strategy=StrategyType.CASE_C,
            pending_order_side=OrderSide.BUY, pending_order_attempt_kind=OrderAttemptKind.INITIAL,
            pending_intent_id="case-c-budget", trading_phase=TradingPhase.ENTRY_ORDER_PENDING))
        outcomes = self.c._execute_action(SubmitOrder(strategy=StrategyType.CASE_C,
            side=OrderSide.BUY, attempt_kind=OrderAttemptKind.INITIAL, idempotency_key="case-c-budget"))
        self.c._enqueue_order_outcomes(outcomes)
        _drain_controller(self.c)
        self.assertIs(self.c._active_stm.current_state.case_c_signal_state, CaseCSignalState.C_SETUP)
        self.assertIn(StrategyType.CASE_C, self.c._buy_budget_waits)
        self.f.clock.advance(timedelta(seconds=181))
        self.c.enqueue_event(TradingEvent(event_type=TradingEventType.MARKET_DATA_UPDATED,
            occurred_at=self.f.clock(), event_id="expired-case-c-budget"))
        _drain_controller(self.c)
        self.assertNotIn(StrategyType.CASE_C, self.c._buy_budget_waits)
        self.assertEqual(self.f.rest_client.submitted_orders, [])

    def test_policy_mismatch_and_invalid_filters_remain_errors(self):
        """예산 보류가 정책 불일치나 잘못된 거래소 규칙을 정상 조건으로 바꾸지 않는다."""
        self.c._session_risk_policy_version = 999
        self.assertEqual(self.buy()[0].payload.reason, RiskBlockReason.RISK_POLICY_VERSION_MISMATCH)
        self.assertEqual(self.transport.requests, [])
        self.c._session_risk_policy_version = 1
        from binance_auto_trader.adapters.binance.mappers import SymbolFilterError
        with patch.object(self.f.rest_client, "prepare_order", side_effect=SymbolFilterError("FILTER_SYMBOL_MISMATCH")):
            self.buy("invalid-exchange-filter")
        self.assertIs(self.c.status, TradingSessionStatus.RECONCILIATION_REQUIRED)
        self.assertEqual(self.f.rest_client.submitted_orders, [])

    def test_no_residual_and_exact_limit(self):
        """잔여가 없고 가격이 수량 단위와 맞으면 정확히 10 USDT까지 허용한다."""
        self.residual_value.return_value = (Decimal("0"), Decimal("0"))
        self.price = Decimal("2500")
        self.c._context.update_market(replace(self.c.context.market, realtime_price=self.price))
        self.install_filters("5")
        self.buy()
        self.assertEqual(self.c.last_risk_decision.budget.projected_position_notional, Decimal("10"))
        self.assertEqual(self.f.rest_client.submitted_orders[0].submitted_quantity, Decimal("0.004"))
