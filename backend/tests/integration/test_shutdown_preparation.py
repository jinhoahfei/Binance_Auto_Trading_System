"""Network-free production shutdown and pre-submission failure regressions."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic, sleep
import unittest
from unittest.mock import Mock, patch as mock_patch

from binance_auto_trader.adapters.binance.bnb_fee_valuator import BnbValuationUnavailableError, BnbValuationInvalidError
from binance_auto_trader.application.shutdown_recovery import prepare_shutdown_cycle, ShutdownPreparationBlocked
from binance_auto_trader.bootstrap import start_application, ApplicationStatus
from binance_auto_trader.bootstrap.lifecycle import request_application_shutdown, ShutdownBlockedError
from binance_auto_trader.bootstrap.shutdown_preparation import ShutdownPreparation, start_shutdown_preparation, get_shutdown_preparation, _run
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.trading import OrderAttemptKind, StrategyType, SubmitOrder
from binance_auto_trader.domain.trading.action_requests import patch, ScheduleReevaluation, ReevaluationTrigger
from binance_auto_trader.domain.trading.events import TradingEventType
from binance_auto_trader.domain.trading.states import TradingPhase
from tests.integration.test_deterministic_production_path_case2_flow import _create_deterministic_production_path_fixture, _close_deterministic_fixture
from tests.integration.test_buy_sell_flow import _create_buy_flow_fixture, _execute_case_b_buy, _drain_controller, FakeOrderScenario


class ShutdownPreparationTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.f = _create_deterministic_production_path_fixture(self.directory.name)
        self.addCleanup(_close_deterministic_fixture, self.f)
        for balance in self.f.rest_client.account_payload['balances']:
            if balance['asset'] == 'ETH':
                balance['free'] = '0'
                balance['locked'] = '0'
        self.f.rest_client.list_all_open_order_results = self.f.rest_client.list_open_order_results
        self.f.rest_client.has_any_exchange_open_orders = lambda: False
        self.f.rest_client.has_any_exchange_open_order_lists = lambda: False
        start_application(self.f.runtime)
        self.c = self.f.runtime.trading_controller
        selection = self.f.runtime.regime_controller.set_regime_type(RegimeType.TYPE_0, command_id='select', expected_version=self.c.context.version)
        self.c.start_trading(command_id='start', expected_version=selection.version)

    def prepare(self, consent=False):
        return start_shutdown_preparation(self.f.runtime, expected_version=self.c.context.version, liquidation_confirmed=consent)

    def finish(self):
        deadline = monotonic() + 3
        while monotonic() < deadline:
            state = get_shutdown_preparation(self.f.runtime)
            if state['phase'] in ('ready', 'blocked'):
                return state
            sleep(.005)
        self.fail('preparation did not finish')

    def test_failed_worker_flat_account_verifies_and_closes(self):
        self.c.mark_event_runtime_failed()
        self.prepare()
        self.assertEqual(self.finish()['phase'], 'ready')
        self.assertTrue(self.c._event_runtime_failed)  # Error evidence survives.
        self.assertFalse(self.c.command_enabled)
        receipt = request_application_shutdown(self.f.runtime, command_id='finish', expected_version=self.c.context.version)
        self.assertTrue(receipt.accepted)
        self.assertIs(self.f.runtime.state.status, ApplicationStatus.CLOSED)
        self.assertEqual(self.f.rest_client.submitted_orders, [])

    def test_permanent_preflight_intent_can_be_cleared_only_with_no_submission(self):
        self.c._context.apply_runtime_patch(patch(pending_intent_id='preflight-only', trading_phase=TradingPhase.ENTRY_ORDER_PENDING))
        self.c._unsubmitted_preparation_intent = 'preflight-only'
        self.f.rest_client.get_order_submission_attempt_evidence = lambda **_: None
        self.c.mark_event_runtime_failed()
        self.prepare()
        self.assertEqual(self.finish()['phase'], 'ready')
        self.assertIsNone(self.c.context.runtime.pending_intent_id)
        self.assertEqual(self.f.rest_client.submitted_orders, [])

    def test_unknown_intent_is_not_erased(self):
        self.c._context.apply_runtime_patch(patch(pending_intent_id='unexplained', trading_phase=TradingPhase.ENTRY_ORDER_PENDING))
        self.prepare()
        self.assertEqual(self.finish()['reason_code'], 'SHUTDOWN_ORDER_UNRESOLVED')
        self.assertEqual(self.c.context.runtime.pending_intent_id, 'unexplained')

    def test_unexplained_balance_keeps_process_alive(self):
        for balance in self.f.rest_client.account_payload['balances']:
            if balance['asset'] == 'ETH': balance['free'] = '1'
        self.prepare()
        self.assertEqual(self.finish()['reason_code'], 'SHUTDOWN_BALANCE_MISMATCH')
        self.assertIs(self.f.runtime.state.status, ApplicationStatus.READY)

    def test_ownership_blocks_without_network_or_order(self):
        self.c.mark_process_ownership_ambiguous('test ownership loss')
        with mock_patch.object(self.c._api_gateway, 'fetch_account_snapshot') as fetch:
            self.prepare()
            self.assertEqual(self.finish()['reason_code'], 'SHUTDOWN_OWNERSHIP_UNVERIFIED')
            fetch.assert_not_called()

    def test_history_disagreement_and_save_failure_have_distinct_reasons(self):
        with mock_patch.object(type(self.f.runtime.trade_history_controller), 'verify_durable_history', return_value=False):
            self.prepare()
            self.assertEqual(self.finish()['reason_code'], 'SHUTDOWN_HISTORY_MISMATCH')
        with mock_patch.object(type(self.f.runtime.trade_history_controller), 'flush_durable_state', side_effect=OSError('disk full')):
            self.prepare()
            self.assertEqual(self.finish()['reason_code'], 'SHUTDOWN_HISTORY_SAVE_FAILED')

    def test_same_job_and_progress_do_not_wait_for_rest_or_application_lock(self):
        entered, release = Event(), Event()
        original = self.c._api_gateway.fetch_account_snapshot
        def fetch(*args):
            entered.set()
            if not release.wait(2): raise TimeoutError('test cleanup')
            return original(*args)
        with mock_patch.object(self.c._api_gateway, 'fetch_account_snapshot', side_effect=fetch):
            initial = self.prepare()
            self.assertTrue(entered.wait(1))
            try:
                self.assertEqual(self.prepare()['operation_id'], initial['operation_id'])
                self.assertEqual(get_shutdown_preparation(self.f.runtime)['phase'], 'checking')
                acquired = self.f.runtime.application_lock.acquire(blocking=False)
                self.assertTrue(acquired, 'network wait held the global lock')
                if acquired: self.f.runtime.application_lock.release()
                with self.assertRaises(Exception):
                    self.c.stop_trading(command_id='competing-stop', expected_version=self.c.context.version)
            finally: release.set()
            self.assertEqual(self.finish()['phase'], 'ready')

    def test_deadline_blocks_late_network_completion_and_no_final_exit(self):
        original = self.c._api_gateway.fetch_account_snapshot
        def slow(*args):
            sleep(.06)
            return original(*args)
        op = ShutdownPreparation('timeout-test', self.c.context.version, False, deadline=monotonic()+.025)
        self.f.runtime._shutdown_store.preparation = op
        self.c._shutdown_preparing = True
        with mock_patch.object(self.c._api_gateway, 'fetch_account_snapshot', side_effect=slow):
            _run(self.f.runtime, op)
        self.assertEqual(op.snapshot()['reason_code'], 'SHUTDOWN_PREPARATION_TIMEOUT')
        self.assertIsNone(self.c._shutdown_verified_version)
        with self.assertRaises(ShutdownBlockedError):
            request_application_shutdown(self.f.runtime, command_id='late-final', expected_version=self.c.context.version)

    def test_stale_version_has_specific_retryable_result(self):
        start_shutdown_preparation(self.f.runtime, expected_version=self.c.context.version+1, liquidation_confirmed=False)
        state = self.finish()
        self.assertEqual(state['reason_code'], 'STALE_CONTEXT_VERSION')
        self.assertTrue(state['retryable'])

    def test_worker_cleanup_failure_retains_specific_step_and_keeps_app_open(self):
        worker = self.f.runtime._trading_event_runtime_worker
        with mock_patch.object(worker, 'close', side_effect=RuntimeError('worker cleanup failed')):
            self.prepare()
            state = self.finish()
        self.assertEqual((state['reason_code'], state['step']), ('SHUTDOWN_RESOURCE_CLEANUP_FAILED', 'workers'))
        self.assertTrue(state['retryable'])
        self.assertFalse(self.f.runtime.state.closed)

    def test_unverified_session_handle_cleanup_does_not_claim_safe_retry(self):
        handle = Mock()
        handle.close.side_effect = OSError('subscription close failed')
        self.c.register_session_subscription(handle)
        self.prepare()
        state = self.finish()
        self.assertEqual(state['reason_code'], 'SHUTDOWN_RESOURCE_CLEANUP_FAILED')
        self.assertFalse(state['retryable'])
        self.assertFalse(self.f.runtime.state.closed)
        self.assertTrue(self.c.cleanup_failures)


class OrderPreparationTests(unittest.TestCase):
    def test_temporary_failures_use_one_backoff_and_no_submission_budget(self):
        with TemporaryDirectory() as directory:
            f = _create_buy_flow_fixture(directory, FakeOrderScenario.IMMEDIATE_FILLED)
            try:
                f.controller._context.apply_runtime_patch(patch(signal_created=True, signal_time=f.clock()))
                f.controller._active_stm._state = replace(f.controller._active_stm.current_state,
                    root_state=RootState.TRADE_MANAGEMENT, ownership_state=OwnershipState.NO_POSITION,
                    case_b_signal_state=CaseBSignalState.B_POSITION_OPEN_SIGNALLED,
                    case_c_signal_state=CaseCSignalState.C_WAIT_SETUP)
                with mock_patch.object(f.controller._api_gateway, 'prepare_order', side_effect=BnbValuationUnavailableError('empty')):
                    for attempt, seconds in enumerate((1,2,5,10,30,30)):
                        outcomes = _execute_case_b_buy(f, 'same-intent')
                        self.assertEqual(len(outcomes), 1)
                        self.assertEqual((f.controller._preparation_retry_due_at-f.clock()).total_seconds(), seconds)
                        self.assertEqual(f.controller._submission_attempts_by_intent.get('same-intent',0),0)
                        f.controller._enqueue_order_outcomes(outcomes)
                        _drain_controller(f.controller)
                        self.assertEqual(len(f.controller._scheduler), 1)
                        self.assertEqual(f.controller.status.value, 'running')
                        f.clock.advance(timedelta(seconds=seconds))
                self.assertEqual(f.rest_client.submitted_orders, [])
                self.assertEqual(f.repository.get_trade_history(), ())
                f.controller.stop_trading(command_id='stop-waiting', expected_version=f.controller.context.version)
                self.assertIsNone(f.controller._preparation_retry_due_at)
                self.assertEqual(len(f.controller._scheduler), 0)
            finally: f.controller.close_session_resources()

    def test_invalid_payload_is_not_reported_as_symbol_filter(self):
        with TemporaryDirectory() as directory:
            f = _create_buy_flow_fixture(directory, FakeOrderScenario.IMMEDIATE_FILLED)
            try:
                with mock_patch.object(f.controller._api_gateway, 'prepare_order', side_effect=BnbValuationInvalidError('bad candle')):
                    self.assertEqual(_execute_case_b_buy(f, 'bad-price'), ())
                self.assertEqual(f.rest_client.submitted_orders, [])
                self.assertTrue(f.controller.reconciliation_required)
                self.assertIn('ORDER_PREPARATION_INVALID_DATA', [e.failure_code for e in f.controller.order_execution_trace])
            finally: f.controller.close_session_resources()

    def test_expired_signal_is_discarded_and_market_watching_continues(self):
        with TemporaryDirectory() as directory:
            f = _create_buy_flow_fixture(directory, FakeOrderScenario.IMMEDIATE_FILLED)
            c = f.controller
            try:
                c._context.apply_runtime_patch(patch(signal_created=True, signal_time=f.clock()))
                c._active_stm._state = replace(c._active_stm.current_state, root_state=RootState.TRADE_MANAGEMENT, ownership_state=OwnershipState.NO_POSITION, case_b_signal_state=CaseBSignalState.B_POSITION_OPEN_SIGNALLED, case_c_signal_state=CaseCSignalState.C_WAIT_SETUP)
                with mock_patch.object(c._api_gateway, 'prepare_order', side_effect=BnbValuationUnavailableError('offline')):
                    outcomes = _execute_case_b_buy(f, 'expire-me')
                c._enqueue_order_outcomes(outcomes); _drain_controller(c)
                f.clock.advance(timedelta(hours=3,seconds=1))
                c.trigger_scheduled_evaluations(ReevaluationTrigger.RETRY_BACKOFF, occurred_at=f.clock())
                _drain_controller(c)
                self.assertEqual(f.rest_client.submitted_orders, [])
                self.assertIsNone(c.context.runtime.pending_intent_id)
                self.assertIsNone(c._preparation_retry_due_at)
                self.assertEqual(len(c._scheduler), 1)  # Waiting for fresh market data, not another old order.
            finally:c.close_session_resources()

    def test_recovery_recomputes_price_and_quantity_then_submits_once(self):
        with TemporaryDirectory() as directory:
            f = _create_buy_flow_fixture(directory, FakeOrderScenario.IMMEDIATE_FILLED)
            c = f.controller
            try:
                c._context.apply_runtime_patch(patch(signal_created=True, signal_time=f.clock()))
                with mock_patch.object(c._api_gateway, 'prepare_order', side_effect=BnbValuationUnavailableError('offline')):
                    _execute_case_b_buy(f, 'fresh-price')
                    self.assertEqual(_execute_case_b_buy(f, 'fresh-price'), ()) # Duplicate cannot bypass backoff.
                f.clock.advance(timedelta(seconds=1))
                c._context.update_market(replace(c.context.market, realtime_price=Decimal('2000')))
                c._latest_market_evaluation_version = 1
                outcomes = _execute_case_b_buy(f, 'fresh-price')
                self.assertEqual(len(f.rest_client.submitted_orders), 1)
                order = f.rest_client.submitted_orders[0]
                self.assertEqual(order.market_price_at_decision, Decimal('2000'))
                self.assertEqual(order.submission_attempt, 0)
                self.assertEqual(c._submission_attempts_by_intent['fresh-price'], 1)
                self.assertIsNone(c._preparation_retry_due_at)
                self.assertEqual(len(outcomes), 1)
            finally:c.close_session_resources()

    def test_shutdown_arriving_during_preflight_blocks_journal_and_post(self):
        with TemporaryDirectory() as directory:
            f = _create_buy_flow_fixture(directory, FakeOrderScenario.IMMEDIATE_FILLED)
            def prepare(order):
                f.controller._shutdown_preparing=True
                return order
            try:
                with mock_patch.object(f.controller._api_gateway,'prepare_order',side_effect=prepare):
                    self.assertEqual(_execute_case_b_buy(f,'shutdown-during-query'),())
                self.assertEqual(f.rest_client.submitted_orders,[])
                self.assertEqual(f.controller._submission_attempts_by_intent,{})
                self.assertEqual(f.controller._unsubmitted_preparation_intent,'shutdown-during-query')
            finally:f.controller.close_session_resources()

    def test_case_c_preparation_expires_after_three_minute_recovery_window(self):
        with TemporaryDirectory() as directory:
            f = _create_buy_flow_fixture(directory, FakeOrderScenario.IMMEDIATE_FILLED)
            c = f.controller
            try:
                c._active_stm._state = replace(c._active_stm.current_state,
                    root_state=RootState.TRADE_MANAGEMENT, ownership_state=OwnershipState.NO_POSITION,
                    case_b_signal_state=CaseBSignalState.B_WAIT_SIGNAL,
                    case_c_signal_state=CaseCSignalState.C_POSITION_OPEN_SIGNALLED)
                c._context.apply_runtime_patch(patch(allow_new_case_c_setup=True,
                    flush_low=Decimal('1800'), timer_base_time=f.clock(), pending_strategy=StrategyType.CASE_C,
                    pending_order_side=OrderSide.BUY, pending_order_attempt_kind=OrderAttemptKind.INITIAL,
                    pending_intent_id='c-expiring', trading_phase=TradingPhase.ENTRY_ORDER_PENDING))
                with mock_patch.object(c._api_gateway, 'prepare_order', side_effect=TimeoutError('price unavailable')):
                    outcomes = c._execute_action(SubmitOrder(strategy=StrategyType.CASE_C, side=OrderSide.BUY,
                        attempt_kind=OrderAttemptKind.INITIAL, idempotency_key='c-expiring'))
                c._enqueue_order_outcomes(outcomes)
                _drain_controller(c)
                f.clock.advance(timedelta(seconds=181))
                c.trigger_scheduled_evaluations(ReevaluationTrigger.RETRY_BACKOFF, occurred_at=f.clock())
                _drain_controller(c)
                self.assertIsNone(c.context.runtime.pending_intent_id)
                self.assertIsNone(c._preparation_retry_due_at)
                self.assertEqual(f.rest_client.submitted_orders, [])
                self.assertEqual(c._submission_attempts_by_intent, {})
            finally:
                c.close_session_resources()


from tests.integration.test_testnet_restart_reconciliation_flow import (
    _FilledSubmissionTestnetRESTClient, _create_recovery_controller, _submit_case_b_buy,
    _make_filled_result, _account_rest_payload,
)
from binance_auto_trader.domain.trading.order import OrderStatus
from binance_auto_trader.domain.trading.states import OrderSide, CaseBSignalState, RootState, OwnershipState, CaseCSignalState


class ShutdownExchange(_FilledSubmissionTestnetRESTClient):
    """Independent in-memory exchange balances and order identities; never uses a network."""
    def __init__(self):
        super().__init__()
        self.results = {}
        self.sides = {}
        self.cancelled = []
        self.queries = []
        self.partial_next = False
        self.lose_cancel_response = False
        self.lose_submit_response = False

    def prepare_order(self, *, order):
        return replace(order, submitted_quantity=order.submitted_quantity.quantize(Decimal("0.00000001"), rounding="ROUND_DOWN"))

    def get_account(self):
        payload = _account_rest_payload()
        quantity = sum((sum((f.quantity for f in result.fills), Decimal(0)) * (1 if self.sides[key] is OrderSide.BUY else -1)
            for key,result in self.results.items()), Decimal(0))
        for balance in payload['balances']:
            if balance['asset']=='ETH': balance['free'],balance['locked']=str(quantity),'0'
        return payload

    def submit_order(self, *, order):
        self.submit_count += 1
        result = _make_filled_result(order, exchange_order_id=str(94000+self.submit_count), trade_id=str(54000+self.submit_count))
        if self.partial_next:
            self.partial_next=False
            result=replace(result,status=OrderStatus.PARTIALLY_FILLED,fills=(replace(result.fills[0],quantity=result.fills[0].quantity/2),))
        self.results[order.client_order_id]=result
        self.sides[order.client_order_id]=order.side
        self.result=result
        if self.lose_submit_response:
            self.lose_submit_response=False
            raise TimeoutError('exchange accepted, response lost')
        return result

    def sell_all_position(self, *, order):
        return self.submit_order(order=order)

    def query_order_result(self, *, order):
        self.queries.append(order.client_order_id)
        return self.results[order.client_order_id]

    def list_open_order_results(self, *, symbol):
        return tuple(r for r in self.results.values() if r.status is OrderStatus.PARTIALLY_FILLED)

    def list_all_open_order_results(self, *, symbol):
        return self.list_open_order_results(symbol=symbol)

    def has_any_exchange_open_orders(self):
        return bool(self.list_open_order_results(symbol="ETHUSDT"))

    def has_any_exchange_open_order_lists(self): return False

    def list_recent_order_results(self, *, symbol, limit=100):
        return tuple(self.results.values())

    def cancel_order(self, *, order):
        self.cancelled.append(order.client_order_id)
        result=replace(self.results[order.client_order_id],status=OrderStatus.CANCELED)
        self.results[order.client_order_id]=result
        if self.lose_cancel_response:
            self.lose_cancel_response=False
            raise TimeoutError('cancel accepted, response lost')
        return result

    def get_order_submission_attempt_evidence(self, **_): return None


class ShutdownLiquidationTests(unittest.TestCase):
    def test_running_or_recovered_position_requires_consent_and_sells_once(self):
        for recovered in (False,True):
            with self.subTest(recovered=recovered), TemporaryDirectory() as directory:
                exchange=ShutdownExchange()
                path=Path(directory)/'trades.jsonl'
                c,h,position=_create_recovery_controller(path,exchange,command_gate=True)
                c.reconcile_startup_state()
                stm=c.fetch_selected_trading_logic(RegimeType.TYPE_0)
                selected=c.commit_regime_selection(RegimeType.TYPE_0,stm,command_id='select',expected_version=c.context.version)
                c.start_trading(command_id='start',expected_version=selected.version)
                outcomes=_submit_case_b_buy(c,intent_id='buy-before-shutdown')
                c._enqueue_order_outcomes(outcomes);asyncio.run(c.drain_events())
                if recovered:
                    c.close_session_resources();c.account_subscription.close()
                    c,h,position=_create_recovery_controller(path,exchange,command_gate=True)
                    c.reconcile_startup_state()
                c._shutdown_preparing=True
                c.mark_event_runtime_failed()
                c.account_subscription.close();c._account_subscription=None
                try:
                    with self.assertRaises(ShutdownPreparationBlocked) as blocked:
                        prepare_shutdown_cycle(c,ShutdownPreparation('no-consent',c.context.version,False))
                    self.assertEqual(blocked.exception.code,'SHUTDOWN_LIQUIDATION_CONFIRMATION_REQUIRED')
                    self.assertEqual(exchange.submit_count,1)
                    prepare_shutdown_cycle(c,ShutdownPreparation('consent',c.context.version,True,deadline=monotonic()+2))
                    self.assertEqual(position.quantity,0)
                    self.assertEqual(exchange.submit_count,2)
                    self.assertEqual([t.side for t in h.trade_history.trades],[OrderSide.BUY,OrderSide.SELL])
                    self.assertEqual(h.get_pending_order_recovery_records(),())
                    self.assertTrue(c._event_runtime_failed)
                finally:c.close_session_resources()

    def test_partial_order_cancel_response_loss_queries_same_id_before_liquidation(self):
        with TemporaryDirectory() as directory:
            exchange=ShutdownExchange();exchange.partial_next=True;exchange.lose_cancel_response=True
            c,h,position=_create_recovery_controller(Path(directory)/'trades.jsonl',exchange,command_gate=True)
            c.reconcile_startup_state()
            stm=c.fetch_selected_trading_logic(RegimeType.TYPE_0)
            selected=c.commit_regime_selection(RegimeType.TYPE_0,stm,command_id='select',expected_version=c.context.version)
            c.start_trading(command_id='start',expected_version=selected.version)
            _submit_case_b_buy(c,intent_id='partial-buy')
            original_id=next(iter(exchange.results))
            c._shutdown_preparing=True;c.account_subscription.close();c._account_subscription=None
            try:
                prepare_shutdown_cycle(c,ShutdownPreparation('partial',c.context.version,True,deadline=monotonic()+2))
                self.assertEqual(position.quantity,0)
                self.assertEqual(exchange.cancelled,[original_id])
                self.assertGreaterEqual(exchange.queries.count(original_id),2)
                self.assertEqual(exchange.submit_count,2) # Original BUY plus one SELL, never another BUY.
                self.assertEqual(h.get_pending_order_recovery_records(),())
            finally:c.close_session_resources()

    def test_liquidation_price_retry_and_lost_submission_response_do_not_duplicate_sell(self):
        for recovered in (False, True):
            with self.subTest(recovered=recovered), TemporaryDirectory() as directory:
                exchange = ShutdownExchange()
                history_path = Path(directory) / 'trades.jsonl'
                c, h, position = _create_recovery_controller(history_path, exchange, command_gate=True)
                c.reconcile_startup_state()
                stm = c.fetch_selected_trading_logic(RegimeType.TYPE_0)
                selected = c.commit_regime_selection(RegimeType.TYPE_0, stm, command_id='select', expected_version=c.context.version)
                c.start_trading(command_id='start', expected_version=selected.version)
                c._enqueue_order_outcomes(_submit_case_b_buy(c, intent_id='buy-before-retry'))
                asyncio.run(c.drain_events())
                if recovered:
                    c.close_session_resources()
                    c.account_subscription.close()
                    c, h, position = _create_recovery_controller(history_path, exchange, command_gate=True)
                    c.reconcile_startup_state()
                c._shutdown_preparing = True
                c.mark_event_runtime_failed()
                c.account_subscription.close()
                c._account_subscription = None
                base_time, base_monotonic = c._clock(), monotonic()
                c._clock = lambda: base_time + timedelta(seconds=monotonic() - base_monotonic)
                original_prepare = exchange.prepare_order
                prepare_count = 0

                def temporary_price_failure(*, order):
                    nonlocal prepare_count
                    prepare_count += 1
                    if prepare_count == 1:
                        raise BnbValuationUnavailableError('official candle temporarily missing')
                    return original_prepare(order=order)

                exchange.lose_submit_response = True
                try:
                    with mock_patch.object(exchange, 'prepare_order', side_effect=temporary_price_failure):
                        prepare_shutdown_cycle(c, ShutdownPreparation('retry-and-response-loss', c.context.version,
                            True, deadline=monotonic() + 5))
                    self.assertEqual(prepare_count, 2)
                    self.assertEqual(exchange.submit_count, 2)  # One original BUY and exactly one SELL.
                    sell_id = next(key for key, side in exchange.sides.items() if side is OrderSide.SELL)
                    self.assertIn(sell_id, exchange.queries)
                    self.assertEqual(position.quantity, 0)
                    self.assertEqual(h.get_pending_order_recovery_records(), ())
                    self.assertEqual([t.side for t in h.trade_history.trades], [OrderSide.BUY, OrderSide.SELL])
                finally:
                    c.close_session_resources()
