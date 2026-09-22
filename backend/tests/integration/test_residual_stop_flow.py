"""현물 0.1% BUY·SELL 수수료와 잔여 원가의 저장·재시작 흐름을 검증한다."""

from dataclasses import replace
from decimal import Decimal, localcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from binance_auto_trader.adapters.persistence import TradeHistoryRepository
from binance_auto_trader.adapters.persistence.residual_repository import ResidualRepository
from binance_auto_trader.application.residual_settlement import ResidualSettlement
from binance_auto_trader.domain.history.performance import Performance
from binance_auto_trader.domain.trading.position import Position
from binance_auto_trader.domain.trading.spot_fee_policy import SPOT_FEE_RATE
from binance_auto_trader.domain.trading.states import OrderSide
from tests.integration.test_buy_sell_flow import (
    FakeOrderScenario, _create_buy_flow_fixture, _execute_case_b_buy, _drain_controller,
)


class ResidualStopFlowTests(unittest.TestCase):
    """
    클래스 이름: ResidualStopFlowTests
    기능: 양방향 현물 수수료와 실제 ETH·USDT 흐름 및 잔여 원가의 보존을 검사한다.
    작성 날짜: 2026/09/22
    """

    def test_buy_stop_and_fresh_ledger_preserve_remaining_assets(self) -> None:
        """
        함수 이름: test_buy_stop_and_fresh_ledger_preserve_remaining_assets()
        기능: 0.1% ETH 매수·USDT 매도 수수료가 현금·성과·잔여에 한 번씩 반영되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        with TemporaryDirectory() as directory:
            fixture = _create_buy_flow_fixture(directory, FakeOrderScenario.IMMEDIATE_FILLED)
            storage = ResidualRepository(Path(directory).resolve() / "residual-ledger.json")
            service = ResidualSettlement(storage)
            fixture.controller._residual_settlement = service
            original_fills = fixture.rest_client._build_fills
            original_result = fixture.rest_client._build_result
            initial_base = fixture.controller._get_effective_free_balance("ETH")
            initial_quote = fixture.controller._get_effective_free_balance("USDT")
            step_size = Decimal("0.0001")

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
                기능: 매수 ETH와 매도 USDT에 같은 현물 0.1% 수수료율을 적용한다.
                인자: order -> 실제 주문, exchange_order_id -> fixture 거래소 식별자
                반환값: 정규화 Fill tuple
                작성 날짜: 2026/09/22
                """
                fills = original_fills(order, exchange_order_id)
                with localcontext() as context:
                    context.prec = 34
                    if order.side is OrderSide.SELL:
                        return tuple(
                            replace(
                                fill,
                                fee_asset="USDT",
                                fee_amount=fill.executed_amount * SPOT_FEE_RATE,
                                fee_quote_amount=fill.executed_amount * SPOT_FEE_RATE,
                            )
                            for fill in fills
                        )
                    return tuple(
                        replace(
                            fill,
                            fee_asset="ETH",
                            fee_amount=fill.quantity * SPOT_FEE_RATE,
                            fee_quote_amount=fill.quantity * SPOT_FEE_RATE * fill.price,
                        )
                        for fill in fills
                    )

            def prepare(order):
                """
                함수 이름: prepare()
                기능: 외부 symbol filter의 수량 내림만 fixture에서 수행한다.
                인자: order -> Controller 주문
                반환값: 요청량을 보존하고 제출량을 내린 Order
                작성 날짜: 2026/09/08
                """
                order.submitted_quantity = (order.submitted_quantity // step_size) * step_size
                return order

            try:
                with (
                    patch.object(fixture.rest_client, "_build_result", side_effect=distinct_result),
                    patch.object(fixture.rest_client, "_build_fills", side_effect=fee_fills),
                    patch.object(fixture.controller._api_gateway, "prepare_order", side_effect=prepare),
                    patch.object(
                        fixture.controller._api_gateway,
                        "fetch_symbol_trading_rules",
                        return_value=SimpleNamespace(lot_size=SimpleNamespace(step_size=step_size)),
                    ),
                ):
                    outcomes = _execute_case_b_buy(fixture, "residual-buy")
                    fixture.controller._enqueue_order_outcomes(outcomes)
                    _drain_controller(fixture.controller)
                    acquired = fixture.position.quantity
                    buy = fixture.repository.get_trade_history()[0]

                    # ETH 수수료는 실수령량만 줄이며 같은 환산 비용을 원가·USDT에서 중복 차감하지 않는다.
                    self.assertEqual(SPOT_FEE_RATE, Decimal("0.001"))
                    self.assertEqual(buy.fee_asset, "ETH")
                    self.assertEqual(buy.fee_amount, buy.executed_quantity * Decimal("0.001"))
                    self.assertEqual(acquired, buy.executed_quantity * Decimal("0.999"))
                    self.assertEqual(fixture.position.cost_basis, buy.executed_amount)
                    self.assertEqual(fixture.position.get_snapshot().average_fill_price, buy.average_fill_price)
                    with localcontext() as context:
                        context.prec = 34
                        self.assertEqual(fixture.position.average_entry_price, buy.executed_amount / acquired)
                    self.assertEqual(
                        fixture.controller._get_effective_free_balance("ETH"),
                        initial_base + acquired,
                    )
                    self.assertEqual(
                        fixture.controller._get_effective_free_balance("USDT"),
                        initial_quote - buy.executed_amount,
                    )
                    fixture.controller.stop_trading(command_id="residual-stop", expected_version=fixture.controller.context.version)
                    _drain_controller(fixture.controller)
                trades = fixture.repository.get_trade_history()
                self.assertEqual(len(trades), 2, [(entry.message_id, entry.failure_code) for entry in fixture.controller.order_execution_trace])
                self.assertEqual(len(fixture.rest_client.submitted_orders), 2)
                sell = trades[1]
                self.assertEqual(sell.fee_asset, "USDT")
                self.assertEqual(sell.fee_amount, sell.executed_amount * Decimal("0.001"))
                self.assertEqual(sell.fee_quote_amount, sell.fee_amount)
                self.assertTrue(all(trade.schema_version == 2 for trade in trades))
                self.assertTrue(all(not trade.fee_fills for trade in trades))
                self.assertNotIn("BNB", fixture.controller._account_free_overlays)
                self.assertEqual(fixture.position.quantity, 0)
                residual_quantity, residual_cost = service.totals
                self.assertTrue(0 < residual_quantity < step_size)
                self.assertEqual(acquired, sell.executed_quantity + residual_quantity)

                # 매도 현금 유입과 매수 원가는 실현분·잔여분으로 나누어 정확히 보존한다.
                with localcontext() as context:
                    context.prec = 34
                    net_proceeds = sell.executed_amount - sell.fee_amount
                    self.assertEqual(sell.allocated_cost_basis + residual_cost, buy.executed_amount)
                    self.assertEqual(sell.realized_pnl, net_proceeds - sell.allocated_cost_basis)
                    self.assertEqual(
                        sell.realized_pnl - residual_cost,
                        net_proceeds - buy.executed_amount,
                    )
                    self.assertEqual(
                        fixture.controller._get_effective_free_balance("USDT"),
                        initial_quote - buy.executed_amount + net_proceeds,
                    )
                self.assertEqual(
                    fixture.controller._get_effective_free_balance("ETH"),
                    initial_base + residual_quantity,
                )
                performance = fixture.history_controller.performance
                self.assertEqual(performance.total_fee, buy.fee_quote_amount + sell.fee_quote_amount)
                self.assertEqual(performance.daily_fee, performance.total_fee)
                self.assertEqual(performance.realized_pnl, sell.realized_pnl)
                self.assertEqual(performance.completed_sell_count, 1)
                self.assertEqual(performance.losing_sell_count, 1)
                snapshot = fixture.controller.snapshot_session()
                self.assertFalse(snapshot.has_open_position)
                self.assertEqual(snapshot.residual_quantity, residual_quantity)

                # 새 저장소·원장·성과 객체로 읽어도 과거 체결액과 미실현 잔여를 그대로 복원한다.
                restored_repository = TradeHistoryRepository(fixture.history_path, clock=fixture.clock)
                restored_trades = restored_repository.get_trade_history()
                self.assertEqual(restored_trades, trades)
                restored = Position()
                restored_service = ResidualSettlement(
                    ResidualRepository(Path(directory).resolve() / "residual-ledger.json")
                )
                restored_service.restore(restored, restored_trades, step_size)
                self.assertEqual(restored.quantity, 0)
                self.assertEqual(restored_service.totals, service.totals)
                self.assertEqual(Performance(restored_trades, clock=fixture.clock), performance)
            finally:
                fixture.controller.close_session_resources()  # 실제 Binance 연결·주문은 없다.
