"""Verify market recovery with real controller/account/order collaborators."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import PropertyMock, patch
from binance_auto_trader.adapters.binance.websocket_gateway import WebSocketGateway
from binance_auto_trader.application.trading_controller import TradingSessionStatus
from binance_auto_trader.domain.common import RegimeType
from tests.integration.test_order_reconciliation_flow import _submit_case_b_buy
from tests.integration.test_testnet_restart_reconciliation_flow import _create_recovery_controller, _FilledSubmissionTestnetRESTClient

class MarketSessionAutoRecoveryTests(unittest.TestCase):
    def test_verified_recovery_preserves_position_and_never_resubmits(self):
        for with_position in (False, True, "terminal_partial"):
            with self.subTest(with_position=with_position), TemporaryDirectory() as directory:
                client = _FilledSubmissionTestnetRESTClient()
                controller, history, position = _create_recovery_controller(Path(directory) / 'history.jsonl', client, command_gate=True)
                try:
                    controller.reconcile_startup_state()
                    selected = controller.commit_regime_selection(RegimeType.TYPE_0, controller.fetch_selected_trading_logic(RegimeType.TYPE_0), command_id='select', expected_version=0)
                    controller.start_trading(command_id='start', expected_version=selected.version)
                    if with_position == "terminal_partial":
                        from decimal import Decimal
                        client.terminal_partial_quantity = Decimal("0.1")
                    if with_position:
                        _submit_case_b_buy(controller, intent_id='buy')
                        asyncio.run(controller.drain_events())
                    quantity, phase, submits = position.quantity, controller.context.runtime.trading_phase, client.submit_count
                    if with_position == "terminal_partial":
                        # Existing partial-fill ambiguity is an independent blocker, not a market-only outage.
                        controller.mark_market_stream_reconciliation_required('kline_stream_invalid')
                        with patch.object(WebSocketGateway, 'kline_live_ready', new_callable=PropertyMock, return_value=True):
                            controller.complete_market_stream_reconciliation(controller._market_snapshot.version)
                            controller.recover_interrupted_market_session()
                        self.assertIs(controller.status, TradingSessionStatus.RECONCILIATION_REQUIRED)
                        self.assertEqual(position.quantity, quantity)
                        self.assertEqual(client.submit_count, submits)
                        continue
                    for cycle in range(100):
                        controller.mark_market_stream_reconciliation_required('kline_stream_invalid')
                        controller.mark_market_stream_reconciliation_required('market_stream_initializing')
                        with patch.object(WebSocketGateway, 'kline_live_ready', new_callable=PropertyMock, return_value=True):
                            controller.complete_market_stream_reconciliation(controller._market_snapshot.version)
                            self.assertIs(controller.status, TradingSessionStatus.RECONCILIATION_REQUIRED)
                            controller.recover_interrupted_market_session()
                            controller.recover_interrupted_market_session()
                        asyncio.run(controller.run_event_runtime_cycle())
                        self.assertIs(controller.status, TradingSessionStatus.RUNNING)
                        self.assertFalse(controller.reconciliation_required)
                        self.assertEqual(controller.context.runtime.trading_phase, phase)
                        self.assertEqual(position.quantity, quantity)
                        self.assertEqual(client.submit_count, submits)
                finally:
                    controller.close_session_resources()

    def test_stop_or_worker_failure_is_never_resumed(self):
        for reason in ('stop', 'worker'):
            with self.subTest(reason=reason), TemporaryDirectory() as directory:
                client = _FilledSubmissionTestnetRESTClient()
                controller, _, _ = _create_recovery_controller(Path(directory) / 'history.jsonl', client, command_gate=True)
                try:
                    controller.reconcile_startup_state()
                    selected = controller.commit_regime_selection(RegimeType.TYPE_0, controller.fetch_selected_trading_logic(RegimeType.TYPE_0), command_id='select', expected_version=0)
                    controller.start_trading(command_id='start', expected_version=selected.version)
                    controller.mark_market_stream_reconciliation_required('kline_stream_invalid')
                    if reason == 'stop':
                        controller.stop_trading(command_id='stop', expected_version=controller.context.version)
                    else:
                        controller.mark_event_runtime_failed()
                    with patch.object(WebSocketGateway, 'kline_live_ready', new_callable=PropertyMock, return_value=True):
                        controller.complete_market_stream_reconciliation(controller._market_snapshot.version)
                        controller.recover_interrupted_market_session()
                    self.assertIsNot(controller.status, TradingSessionStatus.RUNNING)
                    self.assertEqual(client.submit_count, 0)
                finally:
                    controller.close_session_resources()

    def test_account_mismatch_keeps_position_blocked(self):
        from binance_auto_trader.application.trading_controller import AccountStreamRecoveryBlockedError
        with TemporaryDirectory() as directory:
            client = _FilledSubmissionTestnetRESTClient()
            controller, _, position = _create_recovery_controller(Path(directory) / 'history.jsonl', client, command_gate=True)
            try:
                controller.reconcile_startup_state()
                selected = controller.commit_regime_selection(RegimeType.TYPE_0, controller.fetch_selected_trading_logic(RegimeType.TYPE_0), command_id='select', expected_version=0)
                controller.start_trading(command_id='start', expected_version=selected.version)
                _submit_case_b_buy(controller, intent_id='buy')
                asyncio.run(controller.drain_events())
                quantity = position.quantity
                # The exchange can no longer prove the durable BUY.
                client.include_recent_result = False
                controller.mark_market_stream_reconciliation_required('kline_stream_invalid')
                with patch.object(WebSocketGateway, 'kline_live_ready', new_callable=PropertyMock, return_value=True):
                    controller.complete_market_stream_reconciliation(controller._market_snapshot.version)
                    with self.assertRaises(AccountStreamRecoveryBlockedError):
                        controller.recover_interrupted_market_session()
                self.assertIs(controller.status, TradingSessionStatus.RECONCILIATION_REQUIRED)
                self.assertEqual(controller.market_recovery_snapshot()['phase'], 'blocked')
                self.assertEqual(position.quantity, quantity)
                self.assertEqual(client.submit_count, 1)
            finally:
                controller.close_session_resources()

    def test_virtual_72_hour_market_queue_is_bounded_and_deduplicates(self):
        from datetime import datetime, timezone, timedelta
        from binance_auto_trader.domain.trading.event_queue import SerialEventQueue
        from binance_auto_trader.domain.trading.events import TradingEvent, TradingEventType, EventPriority
        import tracemalloc
        queue = SerialEventQueue(max_seen_event_ids=100)
        start = datetime(2026, 9, 10, tzinfo=timezone.utc)
        # The queue treats evaluations as opaque immutable values; strategy tests cover their contents.
        marker = object()
        tracemalloc.start()
        initial, _ = tracemalloc.get_traced_memory()
        for second in range(0, 72 * 3600, 2):
            event = TradingEvent(event_type=TradingEventType.MARKET_DATA_UPDATED,
                occurred_at=start + timedelta(seconds=second), priority=EventPriority.MARKET,
                event_id=f'market:{second}', market_version=second + 1, market_evaluation=marker)
            self.assertIsNotNone(queue.enqueue(event))
            self.assertIsNotNone(queue.pop())
            self.assertIsNone(queue.enqueue(event))
        retained, _ = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertEqual(len(queue), 0)
        self.assertLess(retained - initial, 1024 * 1024)

    def test_liveness_uses_evaluations_not_the_existence_of_a_process(self):
        from datetime import timedelta
        from tests.integration.test_order_reconciliation_flow import MutableUtcClock
        with TemporaryDirectory() as directory:
            client = _FilledSubmissionTestnetRESTClient()
            controller, _, _ = _create_recovery_controller(Path(directory) / 'history.jsonl', client, command_gate=True)
            try:
                controller.reconcile_startup_state()
                selected = controller.commit_regime_selection(RegimeType.TYPE_0, controller.fetch_selected_trading_logic(RegimeType.TYPE_0), command_id='select', expected_version=0)
                controller.start_trading(command_id='start', expected_version=selected.version)
                clock = MutableUtcClock(controller._clock())
                controller._clock = clock
                controller._market_stream_recovery_enabled = True
                self.assertFalse(controller.check_market_liveness())
                clock.set(clock() + timedelta(seconds=59))
                controller.note_market_input()
                self.assertFalse(controller.check_market_liveness())
                clock.set(clock() + timedelta(seconds=1))
                self.assertTrue(controller.check_market_liveness())
                self.assertFalse(controller.check_market_liveness())
                clock.set(clock() + timedelta(seconds=60))
                self.assertTrue(controller.market_recovery_snapshot()['prolonged'])
                controller.suppress_market_auto_resume()
                with patch.object(WebSocketGateway, 'kline_live_ready', new_callable=PropertyMock, return_value=True):
                    controller.complete_market_stream_reconciliation(controller._market_snapshot.version)
                    controller.recover_interrupted_market_session()
                self.assertIs(controller.status, TradingSessionStatus.RECONCILIATION_REQUIRED)
                self.assertEqual(client.submit_count, 0)
            finally:
                controller.close_session_resources()
