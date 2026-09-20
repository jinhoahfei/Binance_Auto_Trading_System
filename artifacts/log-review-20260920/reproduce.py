import json
from decimal import Decimal
from unittest.mock import patch
from tests.integration.test_order_fault_matrix import (
    OrderFaultMatrixIntegrationTests, OrderResultSpecification, FillSpecification,
    OrderStatus, OrderResult, MARKET_UPDATED_AT, TradeHistoryRepository,
)

case = OrderFaultMatrixIntegrationTests()
case.setUp()
errors = []
original = TradeHistoryRepository.transition_pending_order_lifecycle

def record_transition(self, client_id, lifecycle):
    try:
        return original(self, client_id, lifecycle)
    except Exception as error:
        errors.append({'type': type(error).__name__, 'message': str(error), 'lifecycle': lifecycle.name})
        raise

try:
    spec = OrderResultSpecification('100', OrderStatus.FILLED, MARKET_UPDATED_AT, (
        FillSpecification('fill-a', Decimal('0.5'), Decimal('2500.50'), Decimal('0'), MARKET_UPDATED_AT),
        FillSpecification('fill-b', None, Decimal('2500.50'), Decimal('0'), MARKET_UPDATED_AT),
    ))
    fixture = case._create_pipeline(submit_specifications=(spec,), pending_order_recovery_enabled=True)
    rows=[]
    def snapshot(stage):
        state=fixture.controller._order_states_by_client_id[order.client_order_id]
        rows.append({'stage': stage, 'status':fixture.controller.status.value,
                     'reconciliation_required':fixture.controller.reconciliation_required,
                     'history_count':len(fixture.history_controller.trade_history.trades),
                     'pending_count':len(fixture.history_controller.get_pending_orders()),
                     'position':str(fixture.position.get_snapshot().quantity),
                     'pending_recovery_pending':state.pending_recovery_pending,
                     'lifecycle':state.recovery_lifecycle.name})
    with patch.object(TradeHistoryRepository,'transition_pending_order_lifecycle',record_transition):
        case._execute_actions(fixture.controller,case._entry_actions(fixture,sequence_number=1))
        order=fixture.rest_client.submitted_orders[0]
        full=spec.build(order)
        snapshot('REST FILLED and durable history complete')
        for name,status,fills in [('late NEW',OrderStatus.NEW,()),('late PARTIALLY_FILLED',OrderStatus.PARTIALLY_FILLED,full.fills[:1]),('late FILLED',OrderStatus.FILLED,full.fills)]:
            fixture.controller.observe_order_result(OrderResult(symbol=order.symbol,client_order_id=order.client_order_id,exchange_order_id='100',status=status,processed_at=MARKET_UPDATED_AT,fills=fills))
            snapshot(name)
    result={'mode':'offline fake clients; isolated temporary history', 'states':rows,'caught_repository_errors':errors,'fake_submits':len(fixture.rest_client.submitted_orders)}
    assert not rows[0]['reconciliation_required'] and not rows[1]['reconciliation_required']
    assert rows[2]['reconciliation_required'] and rows[3]['reconciliation_required']
    assert len(errors)==2 and all(x['message']=='pending-order transition requires an active pending order' for x in errors)
    assert len({x['position'] for x in rows})==1 and all(x['history_count']==1 and x['pending_count']==0 for x in rows)
    print(json.dumps(result,ensure_ascii=False,indent=2))
finally:
    case.doCleanups()
