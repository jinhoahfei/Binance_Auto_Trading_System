"""ADR-004 D-11 공식으로 startup Performance를 복원한다."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from zoneinfo import ZoneInfo

from ..trading.states import OrderSide
from .trade import Trade


_KOREA_TIME_ZONE = ZoneInfo("Asia/Seoul")
_RATE_QUANTUM = Decimal("0.00000001")


def _utc_now() -> datetime:
    """
    함수 이름: _utc_now()
    기능: Performance의 현재 KST account day를 결정할 UTC 시각을 반환한다.
    인자: 없음
    반환값: 현재 UTC datetime
    작성 날짜: 2026/08/21
    """
    return datetime.now(timezone.utc)


def _normalize_utc_datetime(value: object, field_name: str) -> datetime:
    """
    함수 이름: _normalize_utc_datetime()
    기능: 주입 clock 결과가 timezone-aware UTC인지 검증하고 정규화한다.
    인자: value -> 검증할 datetime
        field_name -> 오류 메시지에 표시할 필드 이름
    반환값: timezone.utc datetime
    작성 날짜: 2026/08/21
    """
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must use UTC")

    return value.astimezone(timezone.utc)


def _quantize_rate(value: Decimal) -> Decimal:
    """
    함수 이름: _quantize_rate()
    기능: 비율을 소수점 8자리 ROUND_HALF_EVEN으로 표현한다.
    인자: value -> 반올림할 Decimal 비율
    반환값: 소수점 8자리 Decimal
    작성 날짜: 2026/08/21
    """
    return value.quantize(_RATE_QUANTUM, rounding=ROUND_HALF_EVEN)


def _calculate_weighted_return(
    realized_pnl: Decimal,
    allocated_cost_basis: Decimal,
) -> Decimal:
    """
    함수 이름: _calculate_weighted_return()
    기능: 실현손익 합을 원가 합으로 나눈 fee 포함 수익률을 계산한다.
    인자: realized_pnl -> 집계 대상 SELL 실현손익 합
        allocated_cost_basis -> 같은 SELL의 배분 원가 합
    반환값: SELL이 없으면 0, 있으면 소수점 8자리 수익률
    작성 날짜: 2026/08/21
    """
    if allocated_cost_basis == Decimal("0"):
        return Decimal("0")

    return _quantize_rate(
        realized_pnl / allocated_cost_basis * Decimal("100")
    )


@dataclass(frozen=True, slots=True, init=False)
class Performance:
    """
    클래스 이름: Performance
    기능: durable Trade로부터 KST 당일과 전체 fee 포함 성과를 불변으로 복원한다.
    작성 날짜: 2026/08/21
    """

    daily_return_rate: Decimal
    cumulative_return_rate: Decimal
    realized_pnl: Decimal
    daily_fee: Decimal
    total_fee: Decimal
    average_sell_return_rate: Decimal
    total_profit: Decimal
    winning_sell_count: int
    losing_sell_count: int
    breakeven_sell_count: int
    completed_sell_count: int
    win_rate: Decimal | None

    def __init__(
        self,
        trades: Iterable[Trade] = (),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 모든 Trade를 한 번 순회해 D-11 startup aggregate를 계산한다.
        인자: trades -> 복원할 durable Trade iterable
            clock -> 현재 KST account day를 결정할 UTC clock
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        selected_clock = _utc_now if clock is None else clock
        if not callable(selected_clock):
            raise TypeError("clock must be callable")
        current_time = _normalize_utc_datetime(selected_clock(), "clock result")
        current_kst_date = current_time.astimezone(_KOREA_TIME_ZONE).date()

        try:
            restored_trades = tuple(trades)
        except TypeError as error:
            raise TypeError("trades must be an iterable of Trade") from error
        if any(not isinstance(trade, Trade) for trade in restored_trades):
            raise TypeError("trades must contain only Trade values")

        with localcontext() as decimal_context:
            decimal_context.prec = 34
            decimal_context.rounding = ROUND_HALF_EVEN
            aggregate_values = self._aggregate_trades(
                restored_trades,
                current_kst_date,
            )

        for field_name, field_value in aggregate_values.items():
            object.__setattr__(self, field_name, field_value)

    def get_performance(self) -> "Performance":
        """
        함수 이름: get_performance()
        기능: 현재 불변 Performance snapshot을 반환한다.
        인자: 없음
        반환값: 자기 자신인 Performance snapshot
        작성 날짜: 2026/08/21
        """
        return self

    def _aggregate_trades(
        self,
        trades: tuple[Trade, ...],
        current_kst_date: date,
    ) -> dict[str, Decimal | int | None]:
        """
        함수 이름: _aggregate_trades()
        기능: Trade tuple에서 fee, SELL 손익, 원가, 승패 및 당일 범위를 집계한다.
        인자: trades -> 검증을 마친 durable Trade tuple
            current_kst_date -> UTC clock에서 변환한 현재 KST date
        반환값: Performance field별 계산 결과 mapping
        작성 날짜: 2026/08/21
        """
        total_fee = Decimal("0")
        daily_fee = Decimal("0")
        total_realized_pnl = Decimal("0")
        total_allocated_cost_basis = Decimal("0")
        daily_realized_pnl = Decimal("0")
        daily_allocated_cost_basis = Decimal("0")
        sell_return_rate_sum = Decimal("0")
        winning_sell_count = 0
        losing_sell_count = 0
        breakeven_sell_count = 0
        completed_sell_count = 0

        for trade in trades:
            trade_kst_date = trade.executed_at.astimezone(_KOREA_TIME_ZONE).date()
            total_fee += trade.fee_quote_amount
            if trade_kst_date == current_kst_date:
                daily_fee += trade.fee_quote_amount
            if trade.side is not OrderSide.SELL:
                continue

            allocated_cost_basis = trade.allocated_cost_basis
            realized_pnl = trade.realized_pnl
            realized_return_rate = trade.realized_return_rate
            if (
                allocated_cost_basis is None
                or realized_pnl is None
                or realized_return_rate is None
            ):
                raise ValueError("SELL Trade must contain realized result fields")

            completed_sell_count += 1
            total_allocated_cost_basis += allocated_cost_basis
            total_realized_pnl += realized_pnl
            sell_return_rate_sum += realized_return_rate
            if realized_pnl > Decimal("0"):
                winning_sell_count += 1
            elif realized_pnl < Decimal("0"):
                losing_sell_count += 1
            else:
                breakeven_sell_count += 1

            if trade_kst_date == current_kst_date:
                daily_allocated_cost_basis += allocated_cost_basis
                daily_realized_pnl += realized_pnl

        cumulative_return_rate = _calculate_weighted_return(
            total_realized_pnl,
            total_allocated_cost_basis,
        )
        daily_return_rate = _calculate_weighted_return(
            daily_realized_pnl,
            daily_allocated_cost_basis,
        )
        average_sell_return_rate = Decimal("0")
        win_rate = None
        if completed_sell_count > 0:
            average_sell_return_rate = _quantize_rate(
                sell_return_rate_sum / Decimal(completed_sell_count)
            )
            win_rate = _quantize_rate(
                Decimal(winning_sell_count)
                / Decimal(completed_sell_count)
                * Decimal("100")
            )

        return {
            "daily_return_rate": daily_return_rate,
            "cumulative_return_rate": cumulative_return_rate,
            "realized_pnl": total_realized_pnl,
            "daily_fee": daily_fee,
            "total_fee": total_fee,
            "average_sell_return_rate": average_sell_return_rate,
            "total_profit": total_realized_pnl,
            "winning_sell_count": winning_sell_count,
            "losing_sell_count": losing_sell_count,
            "breakeven_sell_count": breakeven_sell_count,
            "completed_sell_count": completed_sell_count,
            "win_rate": win_rate,
        }
