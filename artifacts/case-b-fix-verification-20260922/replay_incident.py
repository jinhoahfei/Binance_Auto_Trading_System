"""Replay Sep18 decision price using Sep22 public filters, fake fills, and no network."""
from pathlib import Path
runner = Path(__file__).with_name('verify.py')
exec(compile(runner.read_text().split('suite = unittest.')[0], str(runner), 'exec'))
from decimal import Decimal
from dataclasses import replace
from tests.integration.test_buy_remaining_budget import RemainingBuyBudgetTests
from tests.unit.binance.test_spot_rest_client import QueueHTTPTransport, _client, _preparation_responses, _reference_price_payload
from tests.unit.binance.test_symbol_filters import _exchange_info_payload
from tests.integration.test_buy_sell_flow import _drain_controller
from binance_auto_trader.domain.trading import TradingEvent, TradingEventType

class ExactIncident(RemainingBuyBudgetTests):
    def install_filters(self, minimum, *, repeats=1):
        rules = _exchange_info_payload()
        rules['symbols'][0]['filters'] = json.loads(runner.with_name('current-exchange-filters.json').read_text())['filters']
        self.transport = QueueHTTPTransport(_preparation_responses(exchange_info_payload=rules,
            account_filters_payload={'exchangeFilters': [], 'symbolFilters': [r for r in rules['symbols'][0]['filters'] if r['filterType'].startswith('MAX_NUM')], 'assetFilters': []},
            reference_price_payload=_reference_price_payload(price=str(self.price))))
        self.adapter = _client(self.transport, maximum_order_notional=Decimal('10'))
        self.f.rest_client.prepare_order = lambda *, order: self.adapter.prepare_order(order)
        for name in ['preview_cached_buy_quantity', 'discard_unsubmitted_preparation']:
            if hasattr(self.adapter, name):
                setattr(self.f.rest_client, name, getattr(self.adapter, name))
    def test_exact(self):
        self.price = Decimal('2446.45')
        self.c._context.update_market(replace(self.c.context.market, realtime_price=self.price))
        self.install_filters('5')
        outcomes = self.buy()
        decision = self.c.last_risk_decision
        result = {'target': mode, 'price': str(self.price), 'allowed': decision.allowed,
            'outcome_reasons': [str(getattr(e.payload, 'reason', None)) for e in outcomes],
            'order_count': len(self.f.rest_client.submitted_orders),
            'projected_position_notional': str(decision.budget.projected_position_notional)}
        if hasattr(TradingController, '_limit_buy_quantity'):
            self.assertTrue(decision.allowed)
            self.assertEqual(len(self.f.rest_client.submitted_orders), 1)
            self.assertEqual(self.f.rest_client.submitted_orders[0].submitted_quantity, Decimal('0.0039'))
            self.assertEqual(decision.budget.projected_position_notional, Decimal('9.7760142'))
            self.c._enqueue_order_outcomes(outcomes)
            _drain_controller(self.c)
            for i in range(31):
                self.c.enqueue_event(TradingEvent(event_type=TradingEventType.MARKET_DATA_UPDATED,
                    occurred_at=self.f.clock(),event_id=f'replay:{i}'))
                _drain_controller(self.c)
            self.assertEqual(len(self.f.rest_client.submitted_orders), 1)
            self.assertEqual(len(self.f.repository.get_trade_history()), 1)
            result['submitted_quantity'] = '0.0039'
            result['orders_after_31_ticks'] = 1
            result['history_rows'] = 1
        else:
            self.assertFalse(decision.allowed)
            self.assertEqual(len(self.f.rest_client.submitted_orders), 0)
            self.assertEqual(decision.budget.projected_position_notional, Decimal('10.0206592'))
        print('EXACT_INCIDENT', json.dumps(result), flush=True)

suite = unittest.TestSuite([ExactIncident('test_exact')])
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(not result.wasSuccessful())
