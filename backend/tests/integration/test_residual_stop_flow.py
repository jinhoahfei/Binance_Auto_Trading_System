"""실제 Controller BUY→STOP→장부→Context 종료를 외부 fixture로 검증한다."""

from dataclasses import replace
from decimal import Decimal, localcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from binance_auto_trader.adapters.persistence.residual_repository import ResidualRepository
from binance_auto_trader.application.residual_settlement import ResidualSettlement
from binance_auto_trader.domain.trading.position import Position
from binance_auto_trader.domain.trading.states import OrderSide
from tests.integration.test_buy_sell_flow import (
    FakeOrderScenario, _create_buy_flow_fixture, _execute_case_b_buy, _drain_controller,
)


class ResidualStopFlowTests(unittest.TestCase):
    """
    클래스 이름: ResidualStopFlowTests
    기능: ETH fee가 있는 실제 application STOP이 잔여를 숨기지 않고 종료되는지 검사한다.
    작성 날짜: 2026/09/08
    """

    def test_buy_stop_and_fresh_ledger_preserve_remaining_assets(self) -> None:
        """
        함수 이름: test_buy_stop_and_fresh_ledger_preserve_remaining_assets()
        기능: 실제 BUY·STOP history 저장 후 잔여 분리와 replay를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        with TemporaryDirectory() as directory:
            fixture = _create_buy_flow_fixture(directory, FakeOrderScenario.IMMEDIATE_FILLED)
            storage = ResidualRepository(Path(directory).resolve() / "residual-ledger.json")
            service = ResidualSettlement(storage)
            fixture.controller._residual_settlement = service
            original_fills = fixture.rest_client._build_fills
            original_result = fixture.rest_client._build_result

            def distinct_result(order, status, *, fill_count):
                """
                함수 이름: distinct_result()
                기능: BUY 전용 기존 fixture를 두 주문의 서로 다른 거래소 identity로 확장한다.
                인자: order -> 주문, status -> 결과, fill_count -> 누적 체결 수
                반환값: 원래 체결 의미를 보존한 OrderResult
                작성 날짜: 2026/09/08
                """
                result = original_result(order, status, fill_count=fill_count)
                if order.side is OrderSide.SELL:
                    return replace(result, exchange_order_id="2001", fills=tuple(replace(fill, exchange_order_id="2001") for fill in result.fills))
                return result

            def fee_fills(order, exchange_order_id):
                """
                함수 이름: fee_fills()
                기능: 외부 체결 fixture의 BUY에만 실제 ETH fee 의미를 적용한다.
                인자: order -> 실제 주문, exchange_order_id -> fixture 거래소 식별자
                반환값: 정규화 Fill tuple
                작성 날짜: 2026/09/08
                """
                fills = original_fills(order, exchange_order_id)
                if order.side is OrderSide.SELL:
                    return fills
                with localcontext() as context:
                    context.prec = 34
                    return tuple(replace(fill, fee_asset="ETH", fee_amount=fill.quantity * Decimal("0.00075"), fee_quote_amount=fill.quantity * Decimal("0.00075") * fill.price) for fill in fills)

            def prepare(order):
                """
                함수 이름: prepare()
                기능: 외부 symbol filter의 수량 내림만 fixture에서 수행한다.
                인자: order -> Controller 주문
                반환값: 요청량을 보존하고 제출량을 내린 Order
                작성 날짜: 2026/09/08
                """
                step = Decimal("0.0001")
                order.submitted_quantity = (order.submitted_quantity // step) * step
                return order

            try:
                with patch.object(fixture.rest_client, "_build_result", side_effect=distinct_result), patch.object(fixture.rest_client, "_build_fills", side_effect=fee_fills), patch.object(fixture.controller._api_gateway, "prepare_order", side_effect=prepare), patch.object(fixture.controller._api_gateway, "fetch_symbol_trading_rules", return_value=SimpleNamespace(lot_size=SimpleNamespace(step_size=Decimal("0.0001")))):
                    outcomes = _execute_case_b_buy(fixture, "residual-buy")
                    fixture.controller._enqueue_order_outcomes(outcomes)
                    _drain_controller(fixture.controller)
                    acquired = fixture.position.quantity
                    fixture.controller.stop_trading(command_id="residual-stop", expected_version=fixture.controller.context.version)
                    _drain_controller(fixture.controller)
                trades = fixture.repository.get_trade_history()
                self.assertEqual(len(trades), 2, [(entry.message_id, entry.failure_code) for entry in fixture.controller.order_execution_trace])
                self.assertEqual(len(fixture.rest_client.submitted_orders), 2)
                self.assertEqual(fixture.position.quantity, 0)
                self.assertTrue(0 < service.totals[0] < Decimal("0.0001"))
                self.assertEqual(acquired, trades[-1].executed_quantity + service.totals[0])
                snapshot = fixture.controller.snapshot_session()
                self.assertFalse(snapshot.has_open_position)
                self.assertEqual(snapshot.residual_quantity, service.totals[0])
                restored = Position()
                ResidualSettlement(storage).restore(restored, trades, Decimal("0.0001"))
                self.assertEqual(restored.quantity, 0)
            finally:
                fixture.controller.close_session_resources()  # 실제 Binance 연결·주문은 없다.
