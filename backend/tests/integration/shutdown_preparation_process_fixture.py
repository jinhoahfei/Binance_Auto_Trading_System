"""실계좌 접근 없이 가격 준비 실패와 실제 sidecar 정상 종료를 재현한다."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from binance_auto_trader.bootstrap import start_application
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.adapters.binance.bnb_fee_valuator import BnbValuationInvalidError
from binance_auto_trader.sidecar_stdio import run_stdio_sidecar_process
from tests.integration.test_deterministic_production_path_case2_flow import _create_deterministic_production_path_fixture
from tests.integration.test_buy_sell_flow import _execute_case_b_buy


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
        start_application(f.runtime)
        c = f.runtime.trading_controller
        selected = f.runtime.regime_controller.set_regime_type(RegimeType.TYPE_0, command_id='select-fixture', expected_version=c.context.version)
        c.start_trading(command_id='start-fixture', expected_version=selected.version)
        with patch.object(c._api_gateway, 'prepare_order', side_effect=BnbValuationInvalidError('fixture candle')):
            _execute_case_b_buy(SimpleNamespace(controller=c), 'unsubmitted-fixture-price-intent')
        c.mark_event_runtime_failed()
        assert not client.submitted_orders
        return f.runtime
    return factory


if __name__ == '__main__':
    run_stdio_sidecar_process(create_factory)
