"""동일 세션 자동 복구: 가짜 거래소, 임시 JSONL, 제어 시계만 사용한다."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest
from unittest.mock import patch, PropertyMock
from binance_auto_trader.adapters.binance.websocket_gateway import WebSocketGateway
from binance_auto_trader.adapters.persistence.trade_history_repository import TradeHistoryRepository
from binance_auto_trader.application.session_recovery import SessionRecoveryRetry
from binance_auto_trader.application.runtime_diagnostics import RuntimeDiagnostics
from binance_auto_trader.application.trading_controller import (
    TradingSessionStatus, OrderExecutionFailureCode, AccountStreamRecoveryBlockedError,
)
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.trading.context import MarketEvaluationSnapshot
from binance_auto_trader.domain.trading.order import OrderStatus
from tests.integration.test_order_reconciliation_flow import _submit_case_b_buy, MutableUtcClock
from tests.integration.test_testnet_restart_reconciliation_flow import (
    _create_recovery_controller, _FilledSubmissionTestnetRESTClient, FIXED_TIME,
)

class SessionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("socket.create_connection", side_effect=AssertionError("external network forbidden")))
        self.path = Path(self.enterContext(TemporaryDirectory())) / "history.jsonl"
        self.client = _FilledSubmissionTestnetRESTClient()
        self.controller, self.history, self.position = _create_recovery_controller(self.path, self.client, command_gate=True)
        self.addCleanup(self.controller.close_session_resources)
        self.clock = MutableUtcClock(FIXED_TIME)
        self.controller._clock = self.clock
        self.controller.reconcile_startup_state()
        selected = self.controller.commit_regime_selection(
            RegimeType.TYPE_0, self.controller.fetch_selected_trading_logic(RegimeType.TYPE_0),
            command_id="select", expected_version=0,
        )
        self.controller.start_trading(command_id="start", expected_version=selected.version)
        self.logs = []
        self.controller._diagnostics = RuntimeDiagnostics(self.logs.append)
        self.requests = []
        self.controller.configure_session_recovery(lambda: self.requests.append("request"))
        self.enterContext(patch.object(WebSocketGateway, "kline_live_ready", new_callable=PropertyMock, return_value=True))
        self.evaluations_published = 0

    def interrupt(self, state=None, code=OrderExecutionFailureCode.HISTORY_PERSISTENCE_FAILED):
        self.controller._enter_order_reconciliation(state, code, message_id=None)

    def reconcile_market(self):
        c = self.controller
        c.mark_market_stream_reconciliation_required("market_stream_initializing")
        c._market_snapshot.update(c._market_snapshot.klines_by_interval)
        c.complete_market_stream_reconciliation(c._market_snapshot.version)

    def publish(self):
        c = self.controller
        self.evaluations_published += 1
        version = c._market_snapshot.version
        source = f"recovery-test:{version}"
        for message, kind in (("1L.1", "KLINE_OBSERVED"), ("1L.2", "MARKET_EVALUATED")):
            c.observe_public_market_boundary(message_id=message, event_type=kind, source_event_id=source, market_version=version)
        c.observe_market_evaluation(MarketEvaluationSnapshot(realtime_price=Decimal("2500")), source_event_id=source, market_version=version)

    def recover(self):
        return self.controller.recover_interrupted_trading_session(reconcile_market=self.reconcile_market, publish_evaluation=self.publish)

    def assert_resumes(self):
        completed_before = sum(r["event"] == "trading_session_resumed" for r in self.logs)
        self.recover()
        self.assertEqual(sum(r["event"] == "trading_session_resumed" for r in self.logs), completed_before)
        self.assertIs(self.controller.status, TradingSessionStatus.RUNNING)
        self.assertNotEqual(self.controller.market_recovery_snapshot()["phase"], "resumed")
        results = asyncio.run(self.controller.run_event_runtime_cycle())
        self.assertTrue(any(r.decision_id.startswith("market:") for r in results))
        self.assertEqual(self.controller.market_recovery_snapshot()["phase"], "resumed")
        names = [r["event"] for r in self.logs]
        self.assertEqual(names.count("trading_session_resumed"), completed_before + 1)
        self.assertLess(max(i for i, name in enumerate(names) if name == "strategy_evaluated"), names.index("trading_session_resumed") if completed_before == 0 else max(i for i, name in enumerate(names) if name == "trading_session_resumed"))

    def test_storage_failure_retries_same_order_without_new_submission(self):
        with patch.object(TradeHistoryRepository, "save_this_trade_by_order_id", side_effect=OSError("temporary disk fault")):
            _submit_case_b_buy(self.controller)
        self.assertTrue(self.history.dirty_order_ids)
        self.assertTrue(self.controller.session_recovery_pending)
        original_position = self.position.get_snapshot()
        self.assert_resumes()
        self.assertFalse(self.history.dirty_order_ids)
        self.assertEqual(self.history.get_pending_orders(), ())
        self.assertEqual(len(self.history.trade_history.trades), 1)
        self.assertEqual(self.position.get_snapshot(), original_position)
        self.assertEqual(self.client.submit_count, 1)
        self.assertEqual(set(self.client.query_client_order_ids), {self.client.result.client_order_id})

    def test_remove_failure_and_already_removed_memory_marker(self):
        with patch.object(TradeHistoryRepository, "delete_pending_order", side_effect=OSError("remove fsync fault")):
            _submit_case_b_buy(self.controller)
        state = self.controller._order_states_by_client_id[self.client.result.client_order_id]
        for already_removed in (False, True):
            if already_removed:
                state.pending_recovery_pending = True
                self.interrupt(state)
            self.assert_resumes()
            self.assertFalse(state.pending_recovery_pending)
            self.assertEqual(self.history.get_pending_orders(), ())
            self.assertEqual(len(self.history.trade_history.trades), 1)
            self.assertEqual(self.client.submit_count, 1)

    def test_duplicate_and_composite_faults_are_independently_retained(self):
        _submit_case_b_buy(self.controller)
        state = self.controller._order_states_by_client_id[self.client.result.client_order_id]
        for _ in range(6):
            self.interrupt(state)
        self.controller.mark_account_stream_reconciliation_required("stream_disconnected")
        self.controller.mark_market_stream_reconciliation_required("kline_stream_invalid")
        issues = self.controller._session_recovery_issues
        self.assertEqual(len(issues), 3)
        self.assertEqual(max(issue.occurrences for issue in issues.values()), 6)
        with patch.object(type(self.controller._api_gateway), "fetch_account_snapshot", side_effect=TimeoutError("account unavailable")):
            with self.assertRaises(TimeoutError):
                self.recover()
        self.assertEqual(len(issues), 3)
        self.assertIs(self.controller.status, TradingSessionStatus.RECONCILIATION_REQUIRED)
        self.assert_resumes()
        self.assertEqual(self.client.submit_count, 1)

    def test_storage_corruption_or_runtime_failure_is_permanently_blocked(self):
        self.interrupt(code=OrderExecutionFailureCode.GATEWAY_REQUEST_FAILED)
        with patch.object(TradeHistoryRepository, "get_trade_history", side_effect=ValueError("corrupt record")):
            with self.assertRaises(AccountStreamRecoveryBlockedError):
                self.recover()
        self.assertFalse(self.controller.session_recovery_pending)
        self.assertEqual(self.controller.market_recovery_snapshot()["phase"], "blocked")
        self.controller.mark_event_runtime_failed()
        self.assertFalse(self.controller.session_recovery_pending)
        self.assertEqual(self.client.submit_count, 0)

    def test_pending_query_continues_after_fast_budget_exhaustion(self):
        original_submit = self.client.submit_order
        def uncertain_submit(*, order):
            result = original_submit(order=order)
            return replace(result, status=OrderStatus.UNKNOWN, fills=())
        with patch.object(self.client, "submit_order", side_effect=uncertain_submit):
            _submit_case_b_buy(self.controller)
        state = self.controller._order_states_by_client_id[self.client.result.client_order_id]
        state.reconciliation_attempts = 100
        self.interrupt(state, OrderExecutionFailureCode.QUERY_BUDGET_EXHAUSTED)
        unknown = replace(self.client.result, status=OrderStatus.UNKNOWN, fills=())
        with patch.object(self.client, "query_order_result", return_value=unknown), patch.object(self.client, "list_recent_order_results", return_value=()):
            for _ in range(4):
                with self.assertRaises(SessionRecoveryRetry):
                    self.recover()
                self.assertTrue(self.controller.session_recovery_pending)
        try:
            self.recover()
        except SessionRecoveryRetry:
            self.recover()
        asyncio.run(self.controller.run_event_runtime_cycle())
        self.assertEqual(self.client.submit_count, 1)
        self.assertEqual(len(self.history.trade_history.trades), 1)
        self.assertEqual(self.controller.market_recovery_snapshot()["phase"], "resumed")

    def test_shutdown_during_market_recovery_prevents_late_resume(self):
        self.controller.mark_market_stream_reconciliation_required("kline_stream_invalid")
        entered, release = Event(), Event()
        errors = []
        def market():
            entered.set()
            if not release.wait(2):
                raise AssertionError("test did not release recovery")
            self.reconcile_market()
        def recovery():
            try:
                self.controller.recover_interrupted_trading_session(reconcile_market=market, publish_evaluation=self.publish)
            except Exception as error:
                errors.append(error)
        thread = Thread(target=recovery)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            self.controller.suppress_market_auto_resume()
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertFalse(self.controller.session_recovery_pending)
        self.assertIsNot(self.controller.status, TradingSessionStatus.RUNNING)
        self.assertEqual(self.evaluations_published, 0)
        self.assertEqual(self.client.submit_count, 0)

    def test_dispatch_failure_after_barrier_never_leaves_running_gate(self):
        self.interrupt(code=OrderExecutionFailureCode.GATEWAY_REQUEST_FAILED)
        def bad_publish():
            raise RuntimeError("evaluation dispatch bug")
        with self.assertRaises(AccountStreamRecoveryBlockedError):
            self.controller.recover_interrupted_trading_session(reconcile_market=self.reconcile_market, publish_evaluation=bad_publish)
        self.assertIs(self.controller.status, TradingSessionStatus.RECONCILIATION_REQUIRED)
        self.assertFalse(self.controller.session_recovery_pending)
        self.assertEqual(self.client.submit_count, 0)

    def test_rate_limit_wait_not_before_survives_immediate_recovery_request(self):
        original_submit = self.client.submit_order
        def rate_limited(*, order):
            result = original_submit(order=order)
            return replace(result, status=OrderStatus.UNKNOWN, fills=(), retry_after=timedelta(seconds=90))
        with patch.object(self.client, "submit_order", side_effect=rate_limited):
            _submit_case_b_buy(self.controller)
        self.assertTrue(self.controller.session_recovery_pending)
        self.assertTrue(self.requests)
        for seconds in (0, 89):
            self.clock.set(FIXED_TIME + timedelta(seconds=seconds))
            with self.assertRaises(SessionRecoveryRetry) as error:
                self.recover()
            self.assertEqual(error.exception.retry_after, timedelta(seconds=90-seconds))
            self.assertEqual(self.client.query_client_order_ids, [])
        self.clock.set(FIXED_TIME + timedelta(seconds=90))
        self.assert_resumes()
        self.assertEqual(self.client.submit_count, 1)

    def test_terminal_execution_conflict_remains_blocked(self):
        _submit_case_b_buy(self.controller)
        state = self.controller._order_states_by_client_id[self.client.result.client_order_id]
        state.pending_recovery_pending = True
        self.interrupt(state)
        original = self.position.get_snapshot()
        changed = replace(self.client.result, fills=tuple(replace(f, price=f.price+1) for f in self.client.result.fills))
        with patch.object(self.client, "query_order_result", return_value=changed):
            with self.assertRaises(AccountStreamRecoveryBlockedError):
                self.recover()
        self.assertFalse(self.controller.session_recovery_pending)
        self.assertEqual(self.position.get_snapshot(), original)
        self.assertEqual(self.client.submit_count, 1)
        self.assertIn("session_recovery_verification_difference", [r["event"] for r in self.logs])

    def test_new_cause_during_verification_requires_another_complete_pass(self):
        self.interrupt(code=OrderExecutionFailureCode.GATEWAY_REQUEST_FAILED)
        def market():
            self.reconcile_market()
            self.interrupt(code=OrderExecutionFailureCode.HISTORY_PERSISTENCE_FAILED)
        with self.assertRaises(SessionRecoveryRetry):
            self.controller.recover_interrupted_trading_session(reconcile_market=market, publish_evaluation=self.publish)
        self.assertEqual(self.evaluations_published, 0)
        self.assert_resumes()
        self.assertEqual(self.client.submit_count, 0)

    def test_user_stop_during_verification_never_resumes(self):
        self.controller.mark_market_stream_reconciliation_required("kline_stream_invalid")
        def market():
            self.controller.stop_trading(command_id="stop", expected_version=self.controller.context.version)
            self.reconcile_market()
        self.controller.recover_interrupted_trading_session(reconcile_market=market, publish_evaluation=self.publish)
        self.assertIsNot(self.controller.status, TradingSessionStatus.RUNNING)
        self.assertFalse(self.controller.session_recovery_pending)
        self.assertEqual(self.evaluations_published, 0)
        self.assertEqual(self.client.submit_count, 0)

    def test_pre_session_connection_recovery_does_not_start_a_strategy(self):
        c, history, position = _create_recovery_controller(self.path.with_name("unstarted.jsonl"), _FilledSubmissionTestnetRESTClient(), command_gate=True)
        self.addCleanup(c.close_session_resources)
        c.reconcile_startup_state()
        c.configure_session_recovery(lambda: None)
        c.mark_account_stream_reconciliation_required("stream_disconnected")
        def market():
            c._market_snapshot.update(c._market_snapshot.klines_by_interval)
            c.complete_market_stream_reconciliation(c._market_snapshot.version)
        c.recover_interrupted_trading_session(reconcile_market=market, publish_evaluation=lambda: self.fail("unstarted session must not evaluate"))
        self.assertIs(c.status, TradingSessionStatus.NOT_STARTED)
        self.assertIsNone(c.session_id)
        self.assertEqual(c.market_recovery_snapshot()["phase"], "idle")

    def test_completed_stop_replay_does_not_cancel_a_new_session(self):
        c = self.controller
        stop_version = c.context.version
        result = c.stop_trading(command_id="old-stop", expected_version=stop_version)
        c.load_account()
        selected = c.commit_regime_selection(RegimeType.TYPE_0, c.fetch_selected_trading_logic(RegimeType.TYPE_0), command_id="reselect", expected_version=c.context.version)
        c.start_trading(command_id="second-start", expected_version=selected.version)
        self.assertEqual(c.stop_trading(command_id="old-stop", expected_version=stop_version), result)
        self.assertFalse(c._session_recovery_cancelled.is_set())
        self.interrupt(code=OrderExecutionFailureCode.GATEWAY_REQUEST_FAILED)
        self.assert_resumes()

    def test_external_execution_never_requests_or_resumes_recovery(self):
        _submit_case_b_buy(self.controller)
        original = self.position.get_snapshot()
        self.requests.clear()
        external = replace(self.client.result, client_order_id="manual-order")
        self.assertFalse(self.controller.observe_order_result(external))
        self.assertFalse(self.requests)
        self.assertFalse(self.controller.session_recovery_pending)
        self.recover()
        self.assertEqual(self.position.get_snapshot(), original)
        self.assertEqual(self.evaluations_published, 0)

    def test_connection_lost_after_verification_retries_without_killing_evaluator(self):
        self.interrupt(code=OrderExecutionFailureCode.GATEWAY_REQUEST_FAILED)
        def disconnect_at_publish():
            raise ConnectionError("market disconnected after validation")
        with self.assertRaises(ConnectionError):
            self.controller.recover_interrupted_trading_session(reconcile_market=self.reconcile_market, publish_evaluation=disconnect_at_publish)
        self.assertTrue(self.controller.session_recovery_pending)
        self.assertFalse(self.controller._event_runtime_failed)
        self.assertIs(self.controller.status, TradingSessionStatus.RECONCILIATION_REQUIRED)
        self.assert_resumes()
        self.assertEqual(self.client.submit_count, 0)

    def test_late_partial_and_duplicate_terminal_during_dirty_save_do_not_poison_recovery(self):
        with patch.object(TradeHistoryRepository, "save_this_trade_by_order_id", side_effect=OSError("disk temporarily unavailable")):
            _submit_case_b_buy(self.controller)
        position = self.position.get_snapshot()
        for repeat in range(5):
            for result in (self.client.result, replace(self.client.result, status=OrderStatus.PARTIALLY_FILLED)):
                self.assertTrue(self.controller.observe_order_result(result))
                self.assertTrue(self.controller.session_recovery_pending)
                self.assertEqual(self.position.get_snapshot(), position)
                self.assertEqual(self.history.trade_history.trades, ())
        self.assert_resumes()
        self.assertEqual(self.position.get_snapshot(), position)
        self.assertEqual(self.client.submit_count, 1)
        self.assertEqual(len(self.history.trade_history.trades), 1)
