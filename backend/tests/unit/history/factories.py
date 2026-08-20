"""History unit test에서 공유하는 Trade와 JSONL record factory이다."""

from datetime import datetime, timezone
from decimal import Decimal

from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.history import Trade
from binance_auto_trader.domain.trading.states import (
    ExitReason,
    OrderSide,
    StrategyType,
)


TEST_INSTANT = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)


def make_trade(
    *,
    trade_id: str = "trade-1",
    order_id: str = "1",
    executed_at: datetime = TEST_INSTANT,
    side: OrderSide = OrderSide.BUY,
    requested_quantity: Decimal | None = None,
    executed_quantity: Decimal | None = None,
    executed_amount: Decimal | None = None,
    average_fill_price: Decimal | None = None,
    fee_amount: Decimal | None = None,
    fee_asset: str | None = None,
    fee_quote_amount: Decimal | None = None,
    allocated_cost_basis: Decimal | None = None,
    realized_pnl: Decimal | None = None,
    realized_return_rate: Decimal | None = None,
    exit_reason: ExitReason | None = None,
) -> Trade:
    """
    함수 이름: make_trade()
    기능: BUY 또는 D-11 첫 SELL을 기본값으로 사용하는 검증된 Trade를 생성한다.
    인자: trade_id -> 거래 식별자
        order_id -> Binance order 식별자 문자열
        executed_at -> UTC 체결 시각
        side -> 매수 또는 매도 방향
        requested_quantity -> 요청 수량 override
        executed_quantity -> 체결 수량 override
        executed_amount -> quote 체결 금액 override
        average_fill_price -> 평균 체결가 override
        fee_amount -> 원래 수수료 override
        fee_asset -> 원래 수수료 asset override
        fee_quote_amount -> quote 환산 수수료 override
        allocated_cost_basis -> SELL 배분 원가 override
        realized_pnl -> SELL 실현손익 override
        realized_return_rate -> SELL 실현수익률 override
        exit_reason -> SELL 종료 사유 override
    반환값: 입력값으로 생성한 Trade
    작성 날짜: 2026/08/21
    """
    is_sell = side is OrderSide.SELL
    selected_requested_quantity = requested_quantity
    if selected_requested_quantity is None:
        selected_requested_quantity = Decimal("1") if is_sell else Decimal("2")
    selected_executed_quantity = executed_quantity
    if selected_executed_quantity is None:
        selected_executed_quantity = Decimal("1") if is_sell else Decimal("2")
    selected_executed_amount = executed_amount
    if selected_executed_amount is None:
        selected_executed_amount = Decimal("110") if is_sell else Decimal("200")
    selected_average_fill_price = average_fill_price
    if selected_average_fill_price is None:
        selected_average_fill_price = Decimal("110") if is_sell else Decimal("100")
    selected_fee_amount = fee_amount
    if selected_fee_amount is None:
        selected_fee_amount = Decimal("0.11") if is_sell else Decimal("0.002")
    selected_fee_asset = fee_asset
    if selected_fee_asset is None:
        selected_fee_asset = "USDT" if is_sell else "ETH"
    selected_fee_quote_amount = fee_quote_amount
    if selected_fee_quote_amount is None:
        selected_fee_quote_amount = Decimal("0.11") if is_sell else Decimal("0.20")

    if is_sell:
        selected_cost_basis = (
            Decimal("100.10")
            if allocated_cost_basis is None
            else allocated_cost_basis
        )
        selected_realized_pnl = (
            Decimal("9.79") if realized_pnl is None else realized_pnl
        )
        selected_return_rate = (
            Decimal("9.78021978")
            if realized_return_rate is None
            else realized_return_rate
        )
        selected_exit_reason = (
            ExitReason.TAKE_PROFIT if exit_reason is None else exit_reason
        )
    else:
        selected_cost_basis = allocated_cost_basis
        selected_realized_pnl = realized_pnl
        selected_return_rate = realized_return_rate
        selected_exit_reason = exit_reason

    return Trade(
        trade_id=trade_id,
        order_id=order_id,
        client_order_id=f"client-{order_id}",
        symbol="ETHUSDT",
        executed_at=executed_at,
        side=side,
        regime_type=RegimeType.TYPE_0,
        strategy=StrategyType.CASE_B,
        requested_quantity=selected_requested_quantity,
        executed_quantity=selected_executed_quantity,
        executed_amount=selected_executed_amount,
        average_fill_price=selected_average_fill_price,
        market_price_at_decision=selected_average_fill_price,
        fee_amount=selected_fee_amount,
        fee_asset=selected_fee_asset,
        fee_quote_amount=selected_fee_quote_amount,
        allocated_cost_basis=selected_cost_basis,
        realized_pnl=selected_realized_pnl,
        realized_return_rate=selected_return_rate,
        exit_reason=selected_exit_reason,
    )


def make_trade_record(trade: Trade | None = None) -> dict[str, object]:
    """
    함수 이름: make_trade_record()
    기능: Trade를 ADR-004 exact JSONL v1 object로 직렬화한 test record를 만든다.
    인자: trade -> 직렬화할 Trade이며 생략 시 기본 BUY를 사용한다.
    반환값: JSON encoder에 전달할 schema v1 dictionary
    작성 날짜: 2026/08/21
    """
    selected_trade = make_trade() if trade is None else trade

    def decimal_text(value: Decimal | None) -> str | None:
        """
        함수 이름: decimal_text()
        기능: optional Decimal을 exponent 없는 JSON test string으로 변환한다.
        인자: value -> 변환할 Decimal 또는 None
        반환값: plain decimal string 또는 None
        작성 날짜: 2026/08/21
        """
        return None if value is None else format(value, "f")

    executed_at_text = selected_trade.executed_at.strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    return {
        "schema_version": 1,
        "record_type": "trade",
        "trade_id": selected_trade.trade_id,
        "order_id": selected_trade.order_id,
        "client_order_id": selected_trade.client_order_id,
        "symbol": selected_trade.symbol,
        "executed_at": executed_at_text,
        "side": selected_trade.side.value,
        "regime_type": selected_trade.regime_type.value,
        "strategy": selected_trade.strategy.value,
        "requested_quantity": decimal_text(selected_trade.requested_quantity),
        "executed_quantity": decimal_text(selected_trade.executed_quantity),
        "executed_amount": decimal_text(selected_trade.executed_amount),
        "average_fill_price": decimal_text(selected_trade.average_fill_price),
        "market_price_at_decision": decimal_text(
            selected_trade.market_price_at_decision
        ),
        "fee_amount": decimal_text(selected_trade.fee_amount),
        "fee_asset": selected_trade.fee_asset,
        "fee_quote_amount": decimal_text(selected_trade.fee_quote_amount),
        "allocated_cost_basis": decimal_text(selected_trade.allocated_cost_basis),
        "realized_pnl": decimal_text(selected_trade.realized_pnl),
        "realized_return_rate": decimal_text(
            selected_trade.realized_return_rate
        ),
        "exit_reason": (
            None
            if selected_trade.exit_reason is None
            else selected_trade.exit_reason.value
        ),
    }
