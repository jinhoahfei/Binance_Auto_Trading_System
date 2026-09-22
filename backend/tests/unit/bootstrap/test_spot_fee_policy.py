"""BNB 미사용·편도 0.1% 정책과 주문 직전 설정 변경 차단을 검증한다."""

from decimal import localcontext
import unittest
from unittest.mock import Mock

from binance_auto_trader.adapters.binance.api_gateway import APIGateway
from binance_auto_trader.bootstrap.live_configuration import LiveConfigurationError
from binance_auto_trader.bootstrap.live_permission import LiveOrderPermissionRESTClient
from binance_auto_trader.domain.trading.spot_fee_policy import SPOT_FEE_RATE
from binance_auto_trader.domain.trading.states import ExitReason, OrderSide
from tests.unit.bootstrap.test_live_bootstrap import (
    live_configuration,
    live_order,
    spot_commission_payload,
)


class SpotFeePolicyTests(unittest.TestCase):
    """
    클래스 이름: SpotFeePolicyTests
    기능: 공식 commission 양방향 합산과 live 주문의 비할인 정책 경계를 검증한다.
    작성 날짜: 2026/09/22
    """

    def test_market_rates_include_side_specific_tax_and_special(self) -> None:
        """
        함수 이름: test_market_rates_include_side_specific_tax_and_special()
        기능: buyer와 seller 및 세 수수료 유형을 Decimal 정밀도로 각각 합산한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        payload = spot_commission_payload()
        payload["standardCommission"].update(
            taker="0.0004", buyer="0.0001", seller="0.0002"
        )
        payload["specialCommission"].update(taker="0.0001", seller="0.0001")
        payload["taxCommission"].update(buyer="0.0004", seller="0.0002")
        delegate = Mock()
        delegate.get_account_commission.return_value = payload

        # 전역 정밀도가 낮아도 side별 최종 요율은 정확히 편도 0.1%다.
        with localcontext() as decimal_context:
            decimal_context.prec = 2
            policy = APIGateway(delegate).fetch_commission_discount_policy("ETHUSDT")
            self.assertEqual(policy.market_buy_received_asset_commission_rate, SPOT_FEE_RATE)
            self.assertEqual(policy.market_sell_received_asset_commission_rate, SPOT_FEE_RATE)
            self.assertTrue(policy.matches_spot_fee_policy)

    def test_bnb_disabled_orders_need_no_bnb_balance_or_price(self) -> None:
        """
        함수 이름: test_bnb_disabled_orders_need_no_bnb_balance_or_price()
        기능: 양방향 주문이 commission과 실제 prepare·submit만으로 진행됨을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        for side in OrderSide:
            with self.subTest(side=side):
                delegate = Mock(spec=["get_account_commission", "prepare_order", "submit_order"])
                delegate.get_account_commission.return_value = spot_commission_payload()
                order = live_order()
                order.side = side
                delegate.prepare_order.return_value = order
                permission = LiveOrderPermissionRESTClient(
                    delegate, live_configuration(orders=True), base_fee_residual_enabled=True
                )
                self.assertIs(permission.prepare_order(order=order), order)
                permission.submit_order(order=order)
                delegate.prepare_order.assert_called_once_with(order=order)
                delegate.submit_order.assert_called_once_with(order=order)
                self.assertEqual(delegate.get_account_commission.call_count, 2)

    def test_changed_rates_and_discount_payment_block_both_order_sides(self) -> None:
        """
        함수 이름: test_changed_rates_and_discount_payment_block_both_order_sides()
        기능: 할인·무수수료·추가 수수료를 BNB 잔액 조회 없이 신규 주문 전에 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        changes = (
            ("discount", "enabledForAccount", True),
            ("standardCommission", "taker", "0.00075"),
            ("standardCommission", "taker", "0"),
            ("standardCommission", "buyer", "0.0001"),
            ("standardCommission", "seller", "0.0001"),
            ("specialCommission", "seller", "0.0001"),
            ("taxCommission", "buyer", "0.0001"),
        )
        for side in OrderSide:
            for group, field, value in changes:
                with self.subTest(side=side, group=group, field=field, value=value):
                    payload = spot_commission_payload()
                    payload[group][field] = value
                    delegate = Mock()
                    delegate.get_account_commission.return_value = payload
                    order = live_order()
                    order.side = side
                    permission = LiveOrderPermissionRESTClient(
                        delegate, live_configuration(orders=True), base_fee_residual_enabled=True
                    )
                    with self.assertRaises(LiveConfigurationError):
                        permission.prepare_order(order=order)
                    delegate.prepare_order.assert_not_called()
                    delegate.submit_order.assert_not_called()
                    delegate.resolve_bnb_fee.assert_not_called()

    def test_zero_discount_still_blocks_enabled_bnb_payment(self) -> None:
        """
        함수 이름: test_zero_discount_still_blocks_enabled_bnb_payment()
        기능: 숫자 할인율이 0이어도 BNB 납부 설정을 허용하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        payload = spot_commission_payload()
        payload["discount"].update(enabledForAccount=True, discount="0")
        delegate = Mock()
        delegate.get_account_commission.return_value = payload
        self.assertFalse(APIGateway(delegate).fetch_commission_discount_policy("ETHUSDT").matches_spot_fee_policy)

    def test_preparation_rechecks_policy_after_filter_reads(self) -> None:
        """
        함수 이름: test_preparation_rechecks_policy_after_filter_reads()
        기능: filter 조회 중 BNB 설정 또는 SELL 요율 변경을 journal 전 준비 단계에서 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        for group, field, value in (
            ("discount", "enabledForAccount", True),
            ("taxCommission", "seller", "0.0001"),
        ):
            with self.subTest(group=group):
                delegate = Mock()
                payload = spot_commission_payload()
                delegate.get_account_commission.return_value = payload
                order = live_order()

                def prepare_order(*, order):
                    """
                    함수 이름: prepare_order()
                    기능: filter 조회가 수행되는 동안 거래소 수수료 설정 변경을 재현한다.
                    인자: order -> 준비 중인 주문
                    반환값: 동일 주문
                    작성 날짜: 2026/09/22
                    """
                    payload[group][field] = value
                    return order

                delegate.prepare_order.side_effect = prepare_order
                permission = LiveOrderPermissionRESTClient(
                    delegate, live_configuration(orders=True), base_fee_residual_enabled=True
                )
                with self.assertRaises(LiveConfigurationError):
                    permission.prepare_order(order=order)
                self.assertEqual(delegate.get_account_commission.call_count, 2)
                delegate.submit_order.assert_not_called()

    def test_submission_performs_no_additional_fee_query(self) -> None:
        """
        함수 이름: test_submission_performs_no_additional_fee_query()
        기능: 준비 후 journal이 기록되는 submit 경계에는 실패 가능한 수수료 GET을 추가하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        delegate = Mock()
        delegate.get_account_commission.return_value = spot_commission_payload()
        order = live_order()
        delegate.prepare_order.return_value = order
        permission = LiveOrderPermissionRESTClient(
            delegate, live_configuration(orders=True), base_fee_residual_enabled=True
        )
        permission.prepare_order(order=order)
        delegate.get_account_commission.reset_mock()
        permission.submit_order(order=order)
        delegate.get_account_commission.assert_not_called()
        delegate.submit_order.assert_called_once_with(order=order)

    def test_stop_sell_rechecks_fee_policy_after_filter_reads(self) -> None:
        """
        함수 이름: test_stop_sell_rechecks_fee_policy_after_filter_reads()
        기능: 금액 상한에서 제외되는 STOP SELL도 마지막 수수료 조회를 생략하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        delegate = Mock()
        changed_policy = spot_commission_payload()
        changed_policy["discount"]["enabledForAccount"] = True
        delegate.get_account_commission.side_effect = [spot_commission_payload(), changed_policy]
        order = live_order(quantity="1")
        order.side = OrderSide.SELL
        order.exit_reason = ExitReason.STOP
        delegate.prepare_order.return_value = order
        permission = LiveOrderPermissionRESTClient(delegate, live_configuration(orders=True))
        with self.assertRaises(LiveConfigurationError):
            permission.prepare_order(order=order)
        delegate.prepare_order.assert_called_once_with(order=order)
        self.assertEqual(delegate.get_account_commission.call_count, 2)
        delegate.submit_order.assert_not_called()

    def test_base_fee_requires_residual_ledger_but_cancel_remains_available(self) -> None:
        """
        함수 이름: test_base_fee_requires_residual_ledger_but_cancel_remains_available()
        기능: ETH 수수료 잔여 장부 없는 BUY는 막고 기존 주문 취소는 유지한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        delegate = Mock()
        delegate.get_account_commission.return_value = spot_commission_payload()
        permission = LiveOrderPermissionRESTClient(delegate, live_configuration(orders=True))
        with self.assertRaisesRegex(LiveConfigurationError, "durable residual"):
            permission.prepare_order(order=live_order())
        delegate.get_account_commission.reset_mock()
        permission.cancel_order(order=live_order())
        delegate.cancel_order.assert_called_once()
        delegate.get_account_commission.assert_not_called()
