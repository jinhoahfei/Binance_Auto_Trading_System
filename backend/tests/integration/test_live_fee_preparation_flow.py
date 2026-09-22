"""실계정 수수료 조회와 durable 제출 경계를 메모리 거래소로 검증한다."""

from datetime import timedelta
from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from binance_auto_trader.bootstrap.live_permission import LiveOrderPermissionRESTClient
from binance_auto_trader.domain.trading.action_requests import patch
from binance_auto_trader.domain.trading.order import OrderStatus
from tests.integration.test_buy_sell_flow import (
    BuyFlowFixture,
    FakeOrderScenario,
    _create_buy_flow_fixture,
    _execute_case_b_buy,
)
from tests.unit.bootstrap.test_live_bootstrap import (
    live_configuration,
    spot_commission_payload,
)


def _install_live_fee_permission(fixture: BuyFlowFixture) -> Mock:
    """
    함수 이름: _install_live_fee_permission()
    기능: 이미 준비된 메모리 Controller에 실제 live permission과 durable journal 경계를 연결한다.
    인자: fixture -> 실제 네트워크가 없는 BUY 통합 환경
    반환값: commission 조회와 주문 전송을 관찰할 메모리 REST 대역
    작성 날짜: 2026/09/22
    """
    controller = fixture.controller
    controller._maximum_order_notional = Decimal("9")
    controller._pending_order_recovery_enabled = True
    controller._startup_reconciliation_complete = True
    controller._context.apply_runtime_patch(
        patch(signal_created=True, signal_time=fixture.clock())
    )
    delegate = Mock(
        spec=[
            "get_account_commission", "prepare_order", "submit_order",
            "discard_unsubmitted_preparation",
        ]
    )
    delegate.prepare_order.side_effect = lambda *, order: order
    delegate.submit_order.side_effect = fixture.rest_client.submit_order
    delegate.get_account_commission.return_value = spot_commission_payload()
    controller._api_gateway._rest_client = LiveOrderPermissionRESTClient(
        delegate,
        live_configuration(orders=True),
        base_fee_residual_enabled=True,
    )
    return delegate


class LiveFeePreparationFlowTests(unittest.TestCase):
    """
    클래스 이름: LiveFeePreparationFlowTests
    기능: 마지막 수수료 조회 실패를 미전송 준비 실패로 처리하고 실제 전송 불명은 보존한다.
    작성 날짜: 2026/09/22
    """

    def test_final_fee_query_timeout_retries_without_pending_or_attempt(self) -> None:
        """
        함수 이름: test_final_fee_query_timeout_retries_without_pending_or_attempt()
        기능: 두 번째 commission 조회 timeout은 journal·제출 예산 없이 대기하고 복구 후 한 번 제출한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        with TemporaryDirectory() as directory:
            fixture = _create_buy_flow_fixture(directory, FakeOrderScenario.IMMEDIATE_FILLED)
            controller = fixture.controller
            delegate = _install_live_fee_permission(fixture)
            delegate.get_account_commission.side_effect = [
                spot_commission_payload(), TimeoutError("commission unavailable"),
                spot_commission_payload(), spot_commission_payload(),
            ]
            try:
                outcomes = _execute_case_b_buy(fixture, "fee-timeout")
                self.assertEqual(len(outcomes), 1)
                self.assertEqual(delegate.get_account_commission.call_count, 2)
                delegate.submit_order.assert_not_called()
                self.assertEqual(controller._submission_attempts_by_intent, {})
                self.assertEqual(controller._order_states_by_client_id, {})
                self.assertIsNone(controller.context.pending_order)
                self.assertEqual(fixture.history_controller.get_pending_order_recovery_records(), ())
                self.assertEqual(controller._preparation_retry_due_at, fixture.clock() + timedelta(seconds=1))

                fixture.clock.advance(timedelta(seconds=1))
                _execute_case_b_buy(fixture, "fee-timeout")
                delegate.submit_order.assert_called_once()
                self.assertEqual(controller._submission_attempts_by_intent, {"fee-timeout": 1})
                self.assertEqual(fixture.rest_client.submitted_orders[0].submission_attempt, 0)
                self.assertIsNone(controller._preparation_retry_due_at)
                self.assertEqual(fixture.history_controller.get_pending_order_recovery_records(), ())
            finally:
                controller.close_session_resources()

    def test_final_fee_policy_change_blocks_before_pending_journal(self) -> None:
        """
        함수 이름: test_final_fee_policy_change_blocks_before_pending_journal()
        기능: filter 후 할인 납부 또는 SELL 요율 변경을 제출 불명 주문을 남기지 않고 차단한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        for group, field, value in (
            ("discount", "enabledForAccount", True),
            ("taxCommission", "seller", "0.0001"),
        ):
            with self.subTest(group=group), TemporaryDirectory() as directory:
                fixture = _create_buy_flow_fixture(directory, FakeOrderScenario.IMMEDIATE_FILLED)
                controller = fixture.controller
                delegate = _install_live_fee_permission(fixture)
                changed_policy = spot_commission_payload()
                changed_policy[group][field] = value
                delegate.get_account_commission.side_effect = [spot_commission_payload(), changed_policy]
                try:
                    self.assertEqual(_execute_case_b_buy(fixture, "fee-changed"), ())
                    self.assertEqual(delegate.get_account_commission.call_count, 2)
                    delegate.submit_order.assert_not_called()
                    self.assertTrue(controller.reconciliation_required)
                    self.assertEqual(controller._submission_attempts_by_intent, {})
                    self.assertEqual(controller._order_states_by_client_id, {})
                    self.assertIsNone(controller.context.pending_order)
                    self.assertEqual(fixture.history_controller.get_pending_order_recovery_records(), ())
                finally:
                    controller.close_session_resources()

    def test_shutdown_during_final_fee_query_blocks_pending_and_post(self) -> None:
        """
        함수 이름: test_shutdown_during_final_fee_query_blocks_pending_and_post()
        기능: 마지막 commission GET 도중 도착한 종료 의도를 journal 및 POST 전에 반영한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        with TemporaryDirectory() as directory:
            fixture = _create_buy_flow_fixture(directory, FakeOrderScenario.IMMEDIATE_FILLED)
            controller = fixture.controller
            delegate = _install_live_fee_permission(fixture)

            def get_commission(*, symbol: str) -> dict[str, object]:
                """
                함수 이름: get_commission()
                기능: 두 번째 수수료 조회 중 종료 요청이 접수되는 상황을 만든다.
                인자: symbol -> 조회 대상 거래쌍
                반환값: 정상 비할인 수수료 응답
                작성 날짜: 2026/09/22
                """
                if delegate.get_account_commission.call_count == 2:
                    controller._shutdown_preparing = True
                return spot_commission_payload()

            delegate.get_account_commission.side_effect = get_commission
            try:
                self.assertEqual(_execute_case_b_buy(fixture, "fee-shutdown"), ())
                self.assertEqual(delegate.get_account_commission.call_count, 2)
                delegate.submit_order.assert_not_called()
                self.assertEqual(controller._submission_attempts_by_intent, {})
                self.assertEqual(controller._order_states_by_client_id, {})
                self.assertIsNone(controller.context.pending_order)
                self.assertEqual(fixture.history_controller.get_pending_order_recovery_records(), ())
                self.assertEqual(controller._unsubmitted_preparation_intent, "fee-shutdown")
            finally:
                controller.close_session_resources()

    def test_actual_submit_timeout_retains_unknown_journal_and_attempt(self) -> None:
        """
        함수 이름: test_actual_submit_timeout_retains_unknown_journal_and_attempt()
        기능: 실제 주문 제출 경계를 지난 timeout은 미전송 재시도로 되돌리지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        with TemporaryDirectory() as directory:
            fixture = _create_buy_flow_fixture(directory, FakeOrderScenario.IMMEDIATE_FILLED)
            controller = fixture.controller
            delegate = _install_live_fee_permission(fixture)
            delegate.submit_order.side_effect = TimeoutError("order response unavailable")
            try:
                self.assertEqual(_execute_case_b_buy(fixture, "submit-timeout"), ())
                delegate.submit_order.assert_called_once()
                self.assertEqual(delegate.get_account_commission.call_count, 2)
                self.assertEqual(controller._submission_attempts_by_intent, {"submit-timeout": 1})
                self.assertIsNone(controller._preparation_retry_due_at)
                state = next(iter(controller._order_states_by_client_id.values()))
                self.assertIs(state.order.status, OrderStatus.UNKNOWN)
                records = fixture.history_controller.get_pending_order_recovery_records()
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0].lifecycle.value, "UNKNOWN")

                _execute_case_b_buy(fixture, "submit-timeout")
                delegate.submit_order.assert_called_once()
                self.assertEqual(delegate.get_account_commission.call_count, 2)
            finally:
                controller.close_session_resources()
