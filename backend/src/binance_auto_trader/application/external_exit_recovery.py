"""실제 외부 매도 체결과 전체 ETH 잔고가 일치하는 복구 후보만 만든다."""

from decimal import Decimal, ROUND_HALF_EVEN, localcontext

from binance_auto_trader.domain.history.trade import Trade
from binance_auto_trader.domain.trading.account_execution import AccountExecution
from binance_auto_trader.domain.trading.order import OrderStatus, _aggregate_fills
from binance_auto_trader.domain.trading.position import Position
from binance_auto_trader.domain.trading.residual import allocate_external_residual
from binance_auto_trader.domain.trading.states import ExitReason, OrderSide


def build_external_exit_trades(
    history: tuple[Trade, ...],
    position: Position,
    executions: tuple[AccountExecution, ...],
    *,
    eth_balance: Decimal,
    residual_quantity: Decimal,
    residual_cost_basis: Decimal = Decimal("0"),
) -> tuple[Trade, ...]:
    """
    함수 이름: build_external_exit_trades()
    기능: 단일 앱 lot 이후 외부 SELL만 후보에 반영하고 실제 잔고와 수량·수수료를 대조한다.
    인자: history -> 원본 이력, position -> 복원된 앱 Position, executions -> 신규 외부 체결,
        eth_balance -> Earn 보관·보상을 대조한 ETH 원금, residual_quantity/residual_cost_basis -> 잔여 장부
    반환값: 원본 이력·Position을 변경하지 않은 v4 외부 매도 Trade tuple
    작성 날짜: 2026/09/10
    """
    if not history or position.quantity <= 0 or not executions:
        raise ValueError("external exit requires an open durable position")
    candidate = position.clone()
    candidate.require_history_accounting_compatibility()
    owner = candidate.owner
    latest_buy = next(trade for trade in reversed(history) if trade.side is OrderSide.BUY)
    if latest_buy.strategy is not owner:
        raise ValueError("external exit has ambiguous position ownership")
    latest_time = history[-1].executed_at
    trades = []
    for execution in sorted(executions, key=lambda item: (min(fill.executed_at for fill in item.result.fills), int(item.result.exchange_order_id))):
        result = execution.result
        if execution.side is not OrderSide.SELL or result.status not in {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.EXPIRED_IN_MATCH}:
            raise ValueError("external exit requires terminal SELL evidence")
        if result.client_order_id.startswith("bat-") or execution.created_at < latest_time or min(fill.executed_at for fill in result.fills) <= latest_time:
            raise ValueError("external exit overlaps existing history")
        quantity, amount, price, fee, fee_asset, fee_quote, executed_at = _aggregate_fills(result.fills)
        with localcontext() as context:
            context.prec = 34
            context.rounding = ROUND_HALF_EVEN
            if execution.requested_quantity > candidate.quantity + residual_quantity or any(fill.fee_asset == "ETH" and fill.fee_amount > 0 for fill in result.fills):
                raise ValueError("external exit quantity or base commission is ambiguous")
            consumed_quantity, consumed_cost = allocate_external_residual(
                candidate.quantity, quantity, residual_quantity, residual_cost_basis,
            )
            allocated_cost = candidate.get_cost_basis(quantity - consumed_quantity) + consumed_cost
            residual_quantity -= consumed_quantity
            residual_cost_basis -= consumed_cost
            pnl = amount - fee_quote - allocated_cost
            rate = (pnl / allocated_cost * 100).quantize(Decimal("0.00000001"))
        trade = Trade(
            schema_version=4,
            trade_id=f"trade-{result.exchange_order_id}",
            order_id=result.exchange_order_id,
            client_order_id=result.client_order_id,
            symbol=result.symbol,
            executed_at=executed_at,
            side=OrderSide.SELL,
            regime_type=latest_buy.regime_type,
            strategy=owner,
            requested_quantity=execution.requested_quantity,
            executed_quantity=quantity,
            executed_amount=amount,
            average_fill_price=price,
            market_price_at_decision=None,
            fee_amount=fee,
            fee_asset=fee_asset,
            fee_quote_amount=fee_quote,
            allocated_cost_basis=allocated_cost,
            realized_pnl=pnl,
            realized_return_rate=rate,
            exit_reason=ExitReason.EXTERNAL_MANUAL,
            fee_fills=result.fills,
        )
        candidate.apply_historical_trade(trade, residual_quantity=consumed_quantity, residual_cost_basis=consumed_cost)
        trades.append(trade)
        latest_time = executed_at
    with localcontext() as context:
        context.prec = 34
        if candidate.quantity + residual_quantity != eth_balance:
            raise ValueError("external exits do not explain the complete ETH balance")
    return tuple(trades)
