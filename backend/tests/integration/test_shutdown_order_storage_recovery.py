"""종료 확인 후 완료 주문의 남은 복구 표시를 검증하고 한 번만 청산한다."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
import unittest
from unittest.mock import patch

from binance_auto_trader.adapters.persistence.trade_history_repository import TradeHistoryRepository
from binance_auto_trader.application.shutdown_recovery import ShutdownPreparationBlocked, prepare_shutdown_cycle, _clear_unsubmitted_intent
from binance_auto_trader.application.trading_controller import OrderExecutionFailureCode, TradingSessionStatus
from binance_auto_trader.bootstrap.shutdown_preparation import ShutdownPreparation
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.trading.order import OrderStatus
from binance_auto_trader.domain.trading.states import OrderSide, ExitReason, PositionReturnState, StrategyType
from binance_auto_trader.domain.trading.action_requests import patch as runtime_patch
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

    def test_unsubmitted_exit_cleanup_is_atomic_and_idempotent(self):
        """
        함수 이름: test_unsubmitted_exit_cleanup_is_atomic_and_idempotent()
        기능: 청산 사유·복귀 상태·주문 의도를 같은 version으로 지우고 반복해도 변경하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        self.buy()
        self.c._context.apply_runtime_patch(runtime_patch(pending_intent_id="unsent-sell",
            pending_strategy=StrategyType.CASE_B, pending_order_side=OrderSide.SELL,
            pending_exit_reason=ExitReason.TAKE_PROFIT, pending_return_state=PositionReturnState.CASE_B_HOLDING))
        self.c._unsubmitted_preparation_intent = "unsent-sell"
        version = self.c.context.version
        position = self.position.get_snapshot()
        _clear_unsubmitted_intent(self.c)
        self.assertEqual(self.c.context.version, version + 1)
        self.assertIsNone(self.c.context.runtime.pending_return_state)
        self.assertIsNone(self.c.context.runtime.pending_exit_reason)
        self.assertIsNone(self.c.context.runtime.pending_intent_id)
        _clear_unsubmitted_intent(self.c)
        self.assertEqual(self.c.context.version, version + 1)
        self.assertEqual(self.position.get_snapshot(), position)
        self.prepare()
        self.assert_liquidated_once()
        self.assertEqual(self.history.trade_history.trades[-1].exit_reason, ExitReason.FORCE_SELL)
        self.prepare()
        self.assert_liquidated_once()

    def test_shutdown_sell_does_not_inherit_stale_take_profit_reason(self):
        """
        함수 이름: test_shutdown_sell_does_not_inherit_stale_take_profit_reason()
        기능: 실패한 일반익절 사유가 남아 있어도 종료 청산과 재시작 기록에는 FORCE_SELL을 저장한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        self.buy()
        self.c._context.apply_runtime_patch(runtime_patch(
            pending_exit_reason=ExitReason.TAKE_PROFIT,
            pending_return_state=PositionReturnState.CASE_B_HOLDING,
        ))
        self.prepare()
        self.assert_liquidated_once()

        # 포지션 소유 전략은 유지하고 실제 종료 주문의 원인만 FORCE_SELL로 기록한다.
        trades = TradeHistoryRepository(self.path).get_trade_history()
        self.assertEqual(trades, self.history.trade_history.trades)
        self.assertEqual(trades[-1].strategy, StrategyType.CASE_B)
        self.assertEqual(trades[-1].exit_reason, ExitReason.FORCE_SELL)
        journal_path = self.path.with_name(self.path.name + ".pending-orders.jsonl")
        orders = [record["order"] for line in journal_path.read_text().splitlines()
                  if (record := json.loads(line))["operation"] == "UPSERT"]
        self.assertEqual(len(orders), 2)
        self.assertTrue(orders[-1]["intent_id"].startswith("force-sell:"))
        self.assertEqual(orders[-1]["exit_reason"], "FORCE_SELL")

    def test_invalid_final_cleanup_does_not_release_resources_before_validation(self):
        """
        함수 이름: test_invalid_final_cleanup_does_not_release_resources_before_validation()
        기능: 마지막 runtime 검증 실패가 processor를 먼저 제거하지 않고 재시도를 허용한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        processor = self.c._event_processor
        with patch.object(type(self.c._context), "clear_pending_order", side_effect=ValueError("invalid final runtime")):
            with self.assertRaises(ValueError):
                self.prepare()
        self.assertIs(self.c._event_processor, processor)
        self.assertFalse(self.c._cleanup_in_progress)
        self.assertIsNone(self.c._shutdown_verified_version)
        self.prepare()
        self.assertIs(self.c.status, TradingSessionStatus.TERMINATED)

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
