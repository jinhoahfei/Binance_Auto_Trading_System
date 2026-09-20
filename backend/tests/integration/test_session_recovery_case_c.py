"""동일 세션 복구에서 Case C 타이머와 시간 청산이 유지되는지 검증한다."""
import asyncio
from datetime import timedelta
from binance_auto_trader.application.trading_controller import TradingSessionStatus
from binance_auto_trader.domain.trading.states import ExitReason, OrderSide
from tests.integration import test_case_c_market_recovery as legacy

class SessionRecoveryCaseCTests(legacy.CaseCMarketRecoveryTests):
    def setUp(self):
        super().setUp()
        self.controller._startup_reconciliation_complete = True
        self.controller.configure_session_recovery(lambda: None)

    def _recover(self, delay):
        from unittest.mock import patch, PropertyMock
        from binance_auto_trader.adapters.binance.websocket_gateway import WebSocketGateway
        c = self.controller
        c.mark_market_stream_reconciliation_required("kline_stream_invalid")
        self.fixture.clock.advance(delay)
        def market():
            c._market_snapshot.update(c._market_snapshot.klines_by_interval)
            c.complete_market_stream_reconciliation(c._market_snapshot.version)
        with patch.object(WebSocketGateway, "kline_live_ready", new_callable=PropertyMock, return_value=True):
            c.recover_interrupted_trading_session(reconcile_market=market, publish_evaluation=lambda: None)
        self.assertIs(c.status, TradingSessionStatus.RUNNING)

    def test_holding_deadline_elapsed_during_outage_executes_existing_time_exit(self):
        from unittest.mock import patch, PropertyMock
        from binance_auto_trader.adapters.binance.websocket_gateway import WebSocketGateway
        self._observe("2440", "-0.30")
        self._observe("2445", "-0.24", delay=timedelta(seconds=1))
        self.assertEqual(len(self.fixture.rest_client.submitted_orders), 1)
        entered_at = self.fixture.position.entered_at
        quantity_before = self.fixture.position.quantity
        original_submit = self.fixture.rest_client.submit_order
        def submit_with_unique_exchange_identity(*, order):
            from dataclasses import replace
            result = original_submit(order=order)
            if order.side is OrderSide.SELL:
                result = replace(result, exchange_order_id="1004", fills=tuple(
                    replace(fill, exchange_order_id="1004", trade_id="time-exit", executed_at=self.fixture.clock())
                    for fill in result.fills
                ))
            return result
        self.enterContext(patch.object(self.fixture.rest_client, "submit_order", side_effect=submit_with_unique_exchange_identity))
        c = self.controller
        c.mark_market_stream_reconciliation_required("kline_stream_invalid")
        self.fixture.clock.advance(timedelta(hours=1, seconds=1))
        def market():
            c._market_snapshot.update(c._market_snapshot.klines_by_interval)
            c.complete_market_stream_reconciliation(c._market_snapshot.version)
        def publish():
            # 동일 원본 보유 시각을 사용한 첫 복구 평가에서 기존 C-15 TIME 경로가 동작한다.
            self.assertEqual(self.fixture.position.entered_at, entered_at)
            self._observe("2445", "-0.24")
        with patch.object(WebSocketGateway, "kline_live_ready", new_callable=PropertyMock, return_value=True):
            c.recover_interrupted_trading_session(reconcile_market=market, publish_evaluation=publish)
        asyncio.run(c.run_event_runtime_cycle())
        orders = self.fixture.rest_client.submitted_orders
        self.assertEqual(len(orders), 2)
        self.assertIs(orders[1].side, OrderSide.SELL)
        self.assertIs(orders[1].exit_reason, ExitReason.TIME)
        self.assertLess(self.fixture.position.quantity, quantity_before)
        self.assertEqual(c.market_recovery_snapshot()["phase"], "resumed")
