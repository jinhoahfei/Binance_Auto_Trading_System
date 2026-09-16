"""자동 Earn 예치 뒤 실제 Controller 재시작과 장부·현물 수량 보존을 검증한다."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from binance_auto_trader.adapters.persistence import TradeHistoryRepository
from binance_auto_trader.adapters.persistence.residual_repository import ResidualRepository
from binance_auto_trader.application.residual_settlement import ResidualSettlement
from binance_auto_trader.application.runtime_diagnostics import RuntimeDiagnostics
from binance_auto_trader.application.trading_controller import ResidualBalanceMismatchError
from binance_auto_trader.application.balance_reconciliation import BalanceObservationChangedError
from binance_auto_trader.domain.trading.residual import EarnResidualEvidence
from tests.integration.test_external_exit_recovery import ExternalExitRecoveryTests, recovery_facts
from tests.integration.test_testnet_restart_reconciliation_flow import _create_recovery_controller, FIXED_TIME


class EarnRecoveryTests(unittest.TestCase):
    """
    클래스 이름: EarnRecoveryTests
    기능: 예치로 이동한 잔여만 복구하며 주문·장부 변조·전략 수량 오인을 막는다.
    작성 날짜: 2026/09/15
    """
    def test_restart_preserves_files_and_does_not_make_earn_tradable(self):
        """
        함수 이름: test_restart_preserves_files_and_does_not_make_earn_tradable()
        기능: 예치·상환 후 재시작해 원금·이력을 보존하고 전략 수량을 분리한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/15
        """
        with TemporaryDirectory() as directory:
            path = Path(directory).resolve()/'history.jsonl'
            buy, client = recovery_facts()
            TradeHistoryRepository(path).save_this_trade_by_order_id(buy.order_id,buy)
            initial = ExternalExitRecoveryTests()
            controller, _, _ = initial.reconcile(path,client)
            controller.close_session_resources()
            original = {p.name:p.read_bytes() for p in path.parent.iterdir() if p.is_file()}
            client.balance = '0'
            evidence = EarnResidualEvidence((('123',int(FIXED_TIME.timestamp()*1000),Decimal('0.000096')),),Decimal('0.00009601'),Decimal('0.00000001'))
            redeemed = replace(evidence,quantity=Decimal('0'),redemptions=(('456',int((FIXED_TIME+timedelta(seconds=1)).timestamp()*1000),Decimal('0.00009601')),))
            resubscribed = replace(redeemed, quantity=Decimal('0.00009601'), subscriptions=(*redeemed.subscriptions, ('456', int((FIXED_TIME+timedelta(seconds=2)).timestamp()*1000), Decimal('0.00009601'))))
            for evidence, balance in ((evidence,'0'), (evidence,'0'), (redeemed,'0.00009601'), (resubscribed,'0')):
                client.balance = balance
                controller, _, position = _create_recovery_controller(path,client)
                self.addCleanup(controller.close_session_resources)
                controller._residual_settlement = ResidualSettlement(ResidualRepository(path.parent/'residual-ledger.json'))
                records=[]
                controller._diagnostics=RuntimeDiagnostics(records.append)
                with patch.object(controller._api_gateway,'fetch_earn_residual_evidence',return_value=evidence) as reader, patch.object(controller._api_gateway,'fetch_symbol_trading_rules',return_value=SimpleNamespace(lot_size=SimpleNamespace(step_size=Decimal('0.0001')))):
                    controller.reconcile_startup_state()
                self.assertEqual(reader.call_count,2)
                self.assertTrue(controller.startup_reconciliation_complete)
                self.assertEqual(position.quantity,0)
                self.assertEqual(controller.account.get_holdings('ETH'),Decimal(balance))
                self.assertEqual(controller.residual_totals[0],Decimal('0.000096'))
                self.assertEqual(controller.snapshot_session().status.value,'not_started')
                self.assertTrue(any(r['event']=='residual_custody_reconciled' for r in records))
                controller.close_session_resources()
            self.assertEqual(client.submit_count,0)
            for name,content in original.items():
                self.assertEqual((path.parent/name).read_bytes(),content)

    def test_missing_changed_or_unrelated_earn_evidence_remains_blocked(self):
        """
        함수 이름: test_missing_changed_or_unrelated_earn_evidence_remains_blocked()
        기능: 예치 조회 실패·변경·다른 출처의 자산이 시작 차단을 풀지 못하게 한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/15
        """
        with TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'history.jsonl'
            buy,client=recovery_facts()
            TradeHistoryRepository(path).save_this_trade_by_order_id(buy.order_id,buy)
            initial=ExternalExitRecoveryTests()
            controller,_,_=initial.reconcile(path,client)
            controller.close_session_resources()
            client.balance='0'
            evidence=EarnResidualEvidence((('123',int(FIXED_TIME.timestamp()*1000),Decimal('0.000096')),),Decimal('0.000096'),Decimal('0'))
            early=replace(evidence,subscriptions=(('123',int((FIXED_TIME-timedelta(days=1)).timestamp()*1000),Decimal('0.000096')),))
            changed=replace(evidence,subscriptions=(('124',int(FIXED_TIME.timestamp()*1000),Decimal('0.000096')),))
            too_much=replace(evidence,quantity=Decimal('0.000192'),subscriptions=(*evidence.subscriptions,('125',int((FIXED_TIME+timedelta(seconds=1)).timestamp()*1000),Decimal('0.000096'))))
            cases=[(None,None),(evidence,changed),(early,early),(too_much,too_much),(OSError('unavailable'),None)]
            original=(path.parent/'residual-ledger.json').read_bytes()
            for index, values in enumerate(cases):
                controller,_,position=_create_recovery_controller(path,client)
                self.addCleanup(controller.close_session_resources)
                controller._residual_settlement=ResidualSettlement(ResidualRepository(path.parent/'residual-ledger.json'))
                records=[]
                controller._diagnostics=RuntimeDiagnostics(records.append)
                with patch.object(controller._api_gateway,'fetch_earn_residual_evidence',side_effect=values), patch.object(controller._api_gateway,'fetch_symbol_trading_rules',return_value=SimpleNamespace(lot_size=SimpleNamespace(step_size=Decimal('0.0001')))):
                    expected_error = BalanceObservationChangedError if index == 1 else OSError if index == 4 else ResidualBalanceMismatchError
                    with self.assertRaises(expected_error):
                        controller.reconcile_startup_state()
                self.assertFalse(controller.startup_reconciliation_complete)
                self.assertEqual(position.quantity,0)
                self.assertEqual((path.parent/'residual-ledger.json').read_bytes(),original)
                result=controller.balance_reconciliation_snapshot()
                self.assertEqual(result['exchange_spot_quantity'], '0')
                self.assertEqual(result['status'], 'stale' if index == 1 else 'unavailable' if index == 4 else 'mismatch')
                self.assertEqual(result['retryable'], index in (1, 4))
                controller.close_session_resources()
            self.assertEqual(client.submit_count,0)
