"""실제 원금·보상 수치로 시작부터 연결 종료·재대조·재시작을 검증한다."""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import get_ident
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from binance_auto_trader.adapters.persistence import TradeHistoryRepository
from binance_auto_trader.adapters.persistence.residual_repository import ResidualRepository
from binance_auto_trader.application.residual_settlement import ResidualSettlement
from binance_auto_trader.application.shutdown_recovery import prepare_shutdown_cycle, ShutdownPreparationBlocked, _reconcile, _retry_reconciliation
from binance_auto_trader.bootstrap.shutdown_preparation import ShutdownPreparation
from binance_auto_trader.domain.trading.residual import EarnResidualEvidence
from tests.integration.test_external_exit_recovery import ExternalExitRecoveryTests, recovery_facts
from tests.integration.test_testnet_restart_reconciliation_flow import _create_recovery_controller, FIXED_TIME


def seed_residual_history(path):
    """네트워크 없는 기존 체결 fixture로 원금 0.000096 ETH 장부를 만든다."""
    buy, client = recovery_facts()
    TradeHistoryRepository(path).save_this_trade_by_order_id(buy.order_id, buy)
    fixture = ExternalExitRecoveryTests()
    controller, _, _ = fixture.reconcile(path, client)
    controller.close_session_resources()
    return client


def redeemed_evidence():
    """확인된 실시간 보상 1E-8 ETH가 현물로 돌아온 근거다."""
    timestamp = int(FIXED_TIME.timestamp() * 1000)
    return EarnResidualEvidence((("123", timestamp, Decimal("0.000096")),), Decimal("0"),
        Decimal("0.00000001"), (("456", timestamp + 1000, Decimal("0.00009601")),))


class EarnShutdownTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name).resolve() / 'history.jsonl'
        self.client = seed_residual_history(self.path)
        self.original = {p.name: p.read_bytes() for p in self.path.parent.iterdir() if p.is_file()}
        self.evidence = redeemed_evidence()

    def controller(self, evidence=None, balance='0.00009601'):
        self.client.balance = balance
        c, _, _ = _create_recovery_controller(self.path, self.client)
        self.addCleanup(c.close_session_resources)
        c._residual_settlement = ResidualSettlement(ResidualRepository(self.path.parent / 'residual-ledger.json'))
        self.client.list_all_open_order_results = self.client.list_open_order_results
        rules = patch.object(c._api_gateway, 'fetch_symbol_trading_rules', return_value=SimpleNamespace(lot_size=SimpleNamespace(step_size=Decimal('0.0001'))))
        rules.start(); self.addCleanup(rules.stop)
        reader = patch.object(c._api_gateway, 'fetch_earn_residual_evidence', return_value=evidence or self.evidence).start()
        self.addCleanup(patch.stopall)
        c.reconcile_startup_state()
        reader.reset_mock()
        return c, reader

    def close_stream(self, c):
        c.account_subscription.close()
        c._account_subscription = None
        c._shutdown_preparing = True
        c._shutdown_cleanup_owner = get_ident()
        self.assertFalse(c._web_socket_gateway.account_ready)

    def assert_unchanged(self):
        self.assertEqual(self.client.submit_count, 0)
        for name, content in self.original.items():
            self.assertEqual((self.path.parent / name).read_bytes(), content)

    def test_shutdown_and_restart_preserve_principal_rewards_and_history(self):
        redeemed = self.evidence
        deposited = replace(redeemed, quantity=Decimal('0.00009601'), redemptions=())
        resubscribed = replace(redeemed, quantity=Decimal('0.00009601'), subscriptions=(*redeemed.subscriptions,
            ('789', redeemed.redemptions[0][1] + 1000, Decimal('0.00009601'))))
        for evidence, balance in ((redeemed, '0.00009601'), (deposited, '0'), (resubscribed, '0'), (redeemed, '0.00009601')):
            for failed_worker in (False, True):
                with self.subTest(balance=balance, worker=failed_worker):
                    c, reader = self.controller(evidence, balance)
                    self.close_stream(c)
                    c._event_runtime_failed = failed_worker
                    operation = ShutdownPreparation('earn-shutdown', c.context.version, False)
                    prepare_shutdown_cycle(c, operation)
                    self.assertEqual(c.status.value, 'terminated')
                    self.assertFalse(c.command_enabled)
                    self.assertFalse(c.reconciliation_required)
                    self.assertEqual(reader.call_count, 2)  # 같은 cycle의 후속 검사는 영수증을 재사용한다.
                    details = operation.snapshot()['balance_reconciliation']
                    self.assertEqual(details['status'], 'verified')
                    self.assertEqual(Decimal(details['difference_quantity']), 0)
                    self.assertEqual(details['earn_rewards_quantity'], '0.00000001')
                    self.assertEqual(c.residual_totals[0], Decimal('0.000096'))
                    c.close_session_resources()
                    self.assert_unchanged()

    def test_network_failure_is_retryable_and_later_verified(self):
        c, reader = self.controller()
        self.close_stream(c)
        reader.side_effect = [OSError('temporary'), self.evidence, self.evidence]
        operation = ShutdownPreparation('retry', c.context.version, False)
        _retry_reconciliation(c, operation)
        self.assertEqual(reader.call_count, 3)
        self.assertEqual(operation.snapshot()['balance_reconciliation']['status'], 'verified')
        self.assert_unchanged()

    def test_changed_balances_and_evidence_are_not_permanent_mismatches(self):
        for change in ('earn', 'account'):
            c, reader = self.controller()
            self.close_stream(c)
            if change == 'earn':
                reader.side_effect = [self.evidence, replace(self.evidence,
                    subscriptions=(('124', *self.evidence.subscriptions[0][1:]),))]
            else:
                self.client.balance = '0.00009602'
            operation = ShutdownPreparation('changed', c.context.version, False)
            if change == 'account':
                # 첫 REST 이후에 수량이 변하는 실제 race를 재현한다.
                original = c._api_gateway.fetch_account_snapshot
                calls = 0
                def fetch(asset):
                    nonlocal calls
                    calls += 1
                    self.client.balance = '0.00009601' if calls < 4 else '0.00009602'
                    return original(asset)
                with patch.object(c._api_gateway, 'fetch_account_snapshot', side_effect=fetch):
                    with self.assertRaises(ShutdownPreparationBlocked) as caught:
                        _reconcile(c, operation)
            else:
                with self.assertRaises(ShutdownPreparationBlocked) as caught:
                    _reconcile(c, operation)
            self.assertEqual(caught.exception.code, 'SHUTDOWN_ACCOUNT_SNAPSHOT_CHANGED')
            self.assertTrue(caught.exception.retryable)
            self.assertEqual(operation.snapshot()['balance_reconciliation']['status'], 'stale')
            c.close_session_resources()

    def test_verified_receipt_invalidates_on_account_or_history_change(self):
        c, _ = self.controller()
        self.close_stream(c)
        operation = ShutdownPreparation('basis', c.context.version, False)
        prepare_shutdown_cycle(c, operation)
        self.assertFalse(c.reconciliation_required)
        self.client.balance = '0.00009602'
        c.load_account()
        self.assertTrue(c.reconciliation_required)
        self.assertEqual(c.balance_reconciliation_snapshot()['status'], 'stale')
        self.assert_unchanged()

    def test_real_mismatch_preserves_exact_difference(self):
        c, _ = self.controller()
        self.close_stream(c)
        self.client.balance = '0.00009602'
        operation = ShutdownPreparation('mismatch', c.context.version, False)
        with self.assertRaises(ShutdownPreparationBlocked) as caught:
            prepare_shutdown_cycle(c, operation)
        self.assertEqual(caught.exception.code, 'SHUTDOWN_BALANCE_MISMATCH')
        details = operation.snapshot()['balance_reconciliation']
        self.assertEqual(details['difference_quantity'], '0.00000001')
        self.assertEqual(details['reason_code'], 'BALANCE_UNEXPLAINED')
        self.assert_unchanged()

    def test_expired_owner_cannot_publish_success_after_earn_response(self):
        c, reader = self.controller()
        self.close_stream(c)
        operation = ShutdownPreparation('late', c.context.version, False)
        def delayed(*_):
            operation.deadline = monotonic() - 1
            return self.evidence
        reader.side_effect = delayed
        with self.assertRaises(ShutdownPreparationBlocked) as caught:
            prepare_shutdown_cycle(c, operation)
        self.assertEqual(caught.exception.code, 'SHUTDOWN_PREPARATION_TIMEOUT')
        self.assertIsNone(c._shutdown_verified_version)
        self.assert_unchanged()
