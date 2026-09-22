"""실계좌 접근 없이 가격 준비 실패와 실제 sidecar 정상 종료를 재현한다."""
import os
import asyncio
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from binance_auto_trader.bootstrap import start_application
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.adapters.binance.mappers import BinancePayloadError
from binance_auto_trader.sidecar_stdio import run_stdio_sidecar_process
from tests.integration.test_deterministic_production_path_case2_flow import _create_deterministic_production_path_fixture
from tests.integration.test_buy_sell_flow import _execute_case_b_buy
from tests.integration.test_earn_shutdown import seed_residual_history, redeemed_evidence
from binance_auto_trader.application.residual_settlement import ResidualSettlement
from binance_auto_trader.adapters.persistence.residual_repository import ResidualRepository
from binance_auto_trader.application.trading_controller import OrderExecutionFailureCode
from tests.integration.test_shutdown_preparation import ShutdownExchange, _submit_case_b_buy


def create_factory(configuration):
    """
    함수 이름: create_factory()
    기능: 수수료 응답 오류 또는 잔여 청산을 검증할 로컬 runtime 생성 함수를 만든다.
    인자: configuration -> 테스트 sidecar 설정
    반환값: 외부 주문을 수행하지 않는 runtime 생성 함수
    작성 날짜: 2026/09/22
    """
    def factory(*_observers):
        """
        함수 이름: factory()
        기능: 저장소와 메모리 거래소에 종료 시나리오를 구성한다.
        인자: _observers -> 테스트에서 사용하지 않는 publication callback
        반환값: 종료 경로를 재현하는 application runtime
        작성 날짜: 2026/09/22
        """
        f = _create_deterministic_production_path_fixture(str(Path(configuration.history_path).parent))
        client = f.rest_client
        for balance in client.account_payload['balances']:
            if balance['asset'] == 'ETH': balance['free'], balance['locked'] = '0', '0'
        client.list_all_open_order_results = client.list_open_order_results
        client.has_any_exchange_open_orders = lambda: False
        client.has_any_exchange_open_order_lists = lambda: False
        client.get_order_submission_attempt_evidence = lambda **_: None
        earn = os.environ.get('SHUTDOWN_FIXTURE_EARN') == '1'
        stale_order = os.environ.get('SHUTDOWN_FIXTURE_STALE_ORDER') == '1'
        if stale_order:
            exchange = ShutdownExchange()
            for method in ('get_account', 'prepare_order', 'submit_order', 'sell_all_position',
                           'query_order_result', 'list_open_order_results', 'list_all_open_order_results',
                           'list_recent_order_results', 'has_any_exchange_open_orders',
                           'has_any_exchange_open_order_lists', 'get_order_submission_attempt_evidence'):
                setattr(client, method, getattr(exchange, method))
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
        if stale_order:
            # 실제 runtime에 완료 BUY와 제거된 journal 뒤의 stale marker를 남긴다.
            f.runtime._trading_event_runtime_worker.close()
            if f.runtime._account_stream_recovery_worker is not None:
                f.runtime._account_stream_recovery_worker.close()
            c._enqueue_order_outcomes(_submit_case_b_buy(c, intent_id='completed-buy-before-exit'))
            asyncio.run(c.drain_events())
            state = c._order_states_by_client_id[exchange.result.client_order_id]
            assert len(f.runtime.trade_history.trades) == 1
            assert not f.runtime.trade_history_controller.get_pending_order_recovery_records()
            state.pending_recovery_pending = True
            c._enter_order_reconciliation(state, OrderExecutionFailureCode.HISTORY_PERSISTENCE_FAILED, message_id=None)
        elif not earn:
            with patch.object(c._api_gateway, 'prepare_order', side_effect=BinancePayloadError('fixture commission')):
                _execute_case_b_buy(SimpleNamespace(controller=c), 'unsubmitted-fixture-price-intent')
        c.mark_event_runtime_failed()
        assert not client.submitted_orders
        return f.runtime
    return factory


if __name__ == '__main__':
    run_stdio_sidecar_process(create_factory)
