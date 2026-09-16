"""실계좌 접근 없이 가격 준비 실패와 실제 sidecar 정상 종료를 재현한다."""
import os
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from binance_auto_trader.bootstrap import start_application
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.adapters.binance.bnb_fee_valuator import BnbValuationInvalidError
from binance_auto_trader.sidecar_stdio import run_stdio_sidecar_process
from tests.integration.test_deterministic_production_path_case2_flow import _create_deterministic_production_path_fixture
from tests.integration.test_buy_sell_flow import _execute_case_b_buy
from tests.integration.test_earn_shutdown import seed_residual_history, redeemed_evidence
from binance_auto_trader.application.residual_settlement import ResidualSettlement
from binance_auto_trader.adapters.persistence.residual_repository import ResidualRepository


def create_factory(configuration):
    def factory(*_observers):
        f = _create_deterministic_production_path_fixture(str(Path(configuration.history_path).parent))
        client = f.rest_client
        for balance in client.account_payload['balances']:
            if balance['asset'] == 'ETH': balance['free'], balance['locked'] = '0', '0'
        client.list_all_open_order_results = client.list_open_order_results
        client.has_any_exchange_open_orders = lambda: False
        client.has_any_exchange_open_order_lists = lambda: False
        client.get_order_submission_attempt_evidence = lambda **_: None
        earn = os.environ.get('SHUTDOWN_FIXTURE_EARN') == '1'
        if earn:
            source = seed_residual_history(f.history_path.resolve())
            for balance in client.account_payload['balances']:
                if balance['asset'] == 'ETH': balance['free'] = '0.00009601'
            client.list_recent_order_results = source.list_recent_order_results
            client.fetch_earn_residual_evidence = lambda **_: redeemed_evidence()
            f.runtime.trading_controller._residual_settlement = ResidualSettlement(ResidualRepository(f.history_path.parent.resolve() / 'residual-ledger.json'))
            f.runtime.api_gateway.fetch_symbol_trading_rules = lambda *_: SimpleNamespace(lot_size=SimpleNamespace(step_size=Decimal('0.0001')))
        start_application(f.runtime)
        c = f.runtime.trading_controller
        selected = f.runtime.regime_controller.set_regime_type(RegimeType.TYPE_0, command_id='select-fixture', expected_version=c.context.version)
        c.start_trading(command_id='start-fixture', expected_version=selected.version)
        if not earn:
            with patch.object(c._api_gateway, 'prepare_order', side_effect=BnbValuationInvalidError('fixture candle')):
                _execute_case_b_buy(SimpleNamespace(controller=c), 'unsubmitted-fixture-price-intent')
        c.mark_event_runtime_failed()
        assert not client.submitted_orders
        return f.runtime
    return factory


if __name__ == '__main__':
    run_stdio_sidecar_process(create_factory)
