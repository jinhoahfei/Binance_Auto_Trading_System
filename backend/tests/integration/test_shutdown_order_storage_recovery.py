"""종료 확인 후 완료 주문의 남은 복구 표시를 검증하고 한 번만 청산한다."""

import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
import unittest
from unittest.mock import patch

from binance_auto_trader.adapters.persistence.trade_history_repository import TradeHistoryRepository
from binance_auto_trader.application.shutdown_recovery import ShutdownPreparationBlocked, prepare_shutdown_cycle
from binance_auto_trader.application.trading_controller import OrderExecutionFailureCode, TradingSessionStatus
from binance_auto_trader.bootstrap.shutdown_preparation import ShutdownPreparation
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.trading.order import OrderStatus
from binance_auto_trader.domain.trading.states import OrderSide
from tests.integration.test_shutdown_preparation import ShutdownExchange
from tests.integration.test_testnet_restart_reconciliation_flow import _create_recovery_controller, _submit_case_b_buy


class ShutdownOrderStorageRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('socket.create_connection', side_effect=AssertionError('external network forbidden')))
        self.path = Path(self.enterContext(TemporaryDirectory())) / 'history.jsonl'
        self.exchange = ShutdownExchange()
        self.c, self.history, self.position = _create_recovery_controller(self.path, self.exchange, command_gate=True)
        self.addCleanup(self.c.close_session_resources)
        self.c.reconcile_startup_state()
        selected = self.c.commit_regime_selection(RegimeType.TYPE_0,
            self.c.fetch_selected_trading_logic(RegimeType.TYPE_0), command_id='select', expected_version=self.c.context.version)
        self.c.start_trading(command_id='start', expected_version=selected.version)
        # 운영 구성과 같이 자동 복구가 있어도 종료 작업만 청산을 소유한다.
        self.recovery_requests = []
        self.c.configure_session_recovery(lambda: self.recovery_requests.append(True))

    def buy(self):
        outcomes = _submit_case_b_buy(self.c, intent_id='buy-before-window-close')
        self.c._enqueue_order_outcomes(outcomes)
        asyncio.run(self.c.drain_events())
        return self.c._order_states_by_client_id[self.exchange.result.client_order_id]

    def prepare(self, consent=True):
        self.c._shutdown_preparing = True
        if self.c.account_subscription is not None:
            self.c.account_subscription.close()
            self.c._account_subscription = None
        operation = ShutdownPreparation('window-close', self.c.context.version, consent, deadline=monotonic() + 3)
        prepare_shutdown_cycle(self.c, operation)

    def assert_liquidated_once(self):
        self.assertIs(self.c.status, TradingSessionStatus.TERMINATED)
        self.assertEqual(self.position.quantity, 0)
        self.assertEqual(self.exchange.submit_count, 2)
        self.assertEqual([trade.side for trade in self.history.trade_history.trades], [OrderSide.BUY, OrderSide.SELL])
        self.assertEqual(self.history.get_pending_order_recovery_records(), ())
        self.assertFalse(self.history.dirty_order_ids)
        self.assertFalse(self.c._persistence_states_by_order_id)
        self.assertFalse(any(state.pending_recovery_pending or state.persistence_pending
            for state in self.c._order_states_by_client_id.values()))
        self.assertEqual(self.c._shutdown_verified_version, self.c.context.version)
        self.assertFalse(self.c.command_enabled)

    def test_completed_buy_with_removed_journal_and_stale_memory_flags_sells_once(self):
        state = self.buy()
        self.assertEqual(self.history.get_pending_order_recovery_records(), ())
        state.pending_recovery_pending = True
        state.persistence_pending = True
        self.c._persistence_states_by_order_id[state.order.exchange_order_id] = state
        self.c._enter_order_reconciliation(state, OrderExecutionFailureCode.HISTORY_PERSISTENCE_FAILED, message_id=None)
        requests_before = len(self.recovery_requests)
        self.prepare()
        self.assert_liquidated_once()
        self.assertIn(state.order.client_order_id, self.exchange.queries)
        self.assertEqual(len(self.recovery_requests), requests_before)
        # 재확인 요청도 이미 종료한 포지션을 다시 매도하지 않는다.
        self.prepare()
        self.assert_liquidated_once()

    def test_completed_buy_pending_journal_removal_is_recovered_before_sell(self):
        with patch.object(TradeHistoryRepository, 'delete_pending_order', side_effect=OSError('temporary remove fault')):
            state = self.buy()
        self.assertTrue(state.pending_recovery_pending)
        self.assertTrue(self.history.get_pending_order_recovery_records())
        self.prepare()
        self.assert_liquidated_once()

    def test_unsaved_buy_is_saved_before_liquidation(self):
        with patch.object(TradeHistoryRepository, 'save_this_trade_by_order_id', side_effect=OSError('temporary save fault')):
            self.buy()
        self.assertTrue(self.history.dirty_order_ids)
        self.prepare()
        self.assert_liquidated_once()

    def test_liquidation_save_failure_recovers_without_another_sell(self):
        self.buy()
        save = TradeHistoryRepository.save_this_trade_by_order_id
        failed = []

        def fail_first_sell(repository, order_id, trade):
            if trade.side is OrderSide.SELL and not failed:
                failed.append(order_id)
                raise OSError('temporary sell save fault')
            return save(repository, order_id, trade)

        with patch.object(TradeHistoryRepository, 'save_this_trade_by_order_id', fail_first_sell):
            self.prepare()
        self.assertEqual(len(failed), 1)
        self.assert_liquidated_once()

    def test_exchange_execution_conflict_retains_marker_and_prevents_liquidation(self):
        state = self.buy()
        state.pending_recovery_pending = True
        previous = self.position.get_snapshot()
        result = self.exchange.result
        changed = replace(result, fills=tuple(replace(fill, price=fill.price + 1) for fill in result.fills))
        with patch.object(self.exchange, 'query_order_result', return_value=changed):
            with self.assertRaises(ShutdownPreparationBlocked) as blocked:
                self.prepare()
        self.assertEqual(blocked.exception.code, 'SHUTDOWN_HISTORY_MISMATCH')
        self.assertFalse(blocked.exception.retryable)
        self.assertTrue(state.pending_recovery_pending)
        self.assertEqual(self.position.get_snapshot(), previous)
        self.assertEqual(self.exchange.submit_count, 1)
        self.assertIsNone(self.c._shutdown_verified_version)

    def test_marker_recovery_does_not_imply_liquidation_consent(self):
        state = self.buy()
        state.pending_recovery_pending = True
        with self.assertRaises(ShutdownPreparationBlocked) as blocked:
            self.prepare(consent=False)
        self.assertEqual(blocked.exception.code, 'SHUTDOWN_LIQUIDATION_CONFIRMATION_REQUIRED')
        self.assertEqual(self.exchange.submit_count, 1)
        self.assertGreater(self.position.quantity, 0)
        self.prepare(consent=True)
        self.assert_liquidated_once()

    def test_unconfirmed_same_id_query_keeps_position_until_next_confirmed_attempt(self):
        state = self.buy()
        state.pending_recovery_pending = True
        unknown = replace(self.exchange.result, status=OrderStatus.UNKNOWN, fills=())
        with patch.object(self.exchange, 'query_order_result', return_value=unknown):
            with self.assertRaises(ShutdownPreparationBlocked) as blocked:
                self.prepare()
        self.assertEqual(blocked.exception.code, 'SHUTDOWN_ORDER_UNRESOLVED')
        self.assertTrue(blocked.exception.retryable)
        self.assertTrue(state.pending_recovery_pending)
        self.assertEqual(self.exchange.submit_count, 1)
        self.prepare()
        self.assert_liquidated_once()

    def test_ongoing_storage_failure_blocks_without_selling_and_next_attempt_recovers(self):
        with patch.object(TradeHistoryRepository, 'save_this_trade_by_order_id', side_effect=OSError('disk unavailable')):
            self.buy()
            with self.assertRaises(ShutdownPreparationBlocked) as blocked:
                self.prepare()
        self.assertTrue(blocked.exception.retryable)
        self.assertTrue(self.history.dirty_order_ids)
        self.assertEqual(self.exchange.submit_count, 1)
        self.assertIsNone(self.c._shutdown_verified_version)
        self.prepare()
        self.assert_liquidated_once()

    def test_corrupt_durable_history_is_not_treated_as_a_stale_marker(self):
        state = self.buy()
        state.pending_recovery_pending = True
        self.path.write_text('{broken history\n')
        with self.assertRaises(ShutdownPreparationBlocked) as blocked:
            self.prepare()
        self.assertEqual(blocked.exception.code, 'SHUTDOWN_HISTORY_MISMATCH')
        self.assertFalse(blocked.exception.retryable)
        self.assertTrue(state.pending_recovery_pending)
        self.assertEqual(self.exchange.submit_count, 1)
        self.assertIsNone(self.c._shutdown_verified_version)
