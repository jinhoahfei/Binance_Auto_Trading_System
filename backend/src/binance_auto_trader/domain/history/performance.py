"""ADR-004 D-11 공식으로 startup Performance를 복원한다."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from zoneinfo import ZoneInfo

from ..trading.order import ExecutionSummary
from ..trading.states import OrderSide
from .trade import RealizedResult, Trade
from .trade_history import OrderHistoryConflictError


_KOREA_TIME_ZONE = ZoneInfo("Asia/Seoul")
_RATE_QUANTUM = Decimal("0.00000001")


class InvalidZeroCostBasisError(ValueError):
    """
    클래스 이름: InvalidZeroCostBasisError
    기능: SELL 원가가 0인 비정상 execution을 reconciliation 대상으로 표시한다.
    작성 날짜: 2026/08/22
    """

    code = "INVALID_ZERO_COST_BASIS"


def _utc_now() -> datetime:
    """
    함수 이름: _utc_now()
    기능: Performance의 현재 KST account day를 결정할 UTC 시각을 반환한다.
    인자: 없음
    반환값: 현재 UTC datetime
    작성 날짜: 2026/08/21
    """
    return datetime.now(timezone.utc)  # account-day 경계는 항상 UTC clock에서 시작한다.


def _normalize_utc_datetime(value: object, field_name: str) -> datetime:
    """
    함수 이름: _normalize_utc_datetime()
    기능: 주입 clock 결과가 timezone-aware UTC인지 검증하고 정규화한다.
    인자: value -> 검증할 datetime
        field_name -> 오류 메시지에 표시할 필드 이름
    반환값: timezone.utc datetime
    작성 날짜: 2026/08/21
    """
    # KST 변환 전에 naive 시각과 UTC가 아닌 offset을 모두 차단한다.
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must use UTC")

    return value.astimezone(timezone.utc)  # 동등한 UTC 표현도 canonical timezone으로 맞춘다.


def _quantize_rate(value: Decimal) -> Decimal:
    """
    함수 이름: _quantize_rate()
    기능: 비율을 소수점 8자리 ROUND_HALF_EVEN으로 표현한다.
    인자: value -> 반올림할 Decimal 비율
    반환값: 소수점 8자리 Decimal
    작성 날짜: 2026/08/21
    """
    return value.quantize(
        _RATE_QUANTUM,
        rounding=ROUND_HALF_EVEN,
    )  # 금액은 보존하고 외부에 보이는 비율만 8자리로 반올림한다.


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
    # 완료 SELL이 없으면 0으로 정의해 0 나눗셈과 임의 None 의미를 피한다.
    if allocated_cost_basis == Decimal("0"):
        return Decimal("0")  # 원가 합계 0은 수익률 0이라는 D-11 기본값이다.

    return _quantize_rate(
        realized_pnl / allocated_cost_basis * Decimal("100")
    )  # 개별 수익률 평균이 아니라 손익 합계를 원가 합계로 가중한다.


@dataclass(frozen=True, slots=True, init=False)
class Performance:
    """
    클래스 이름: Performance
    기능: durable Trade 복원과 controlled 신규 Trade 적용으로 KST 당일·전체 성과를 보존한다.
    작성 날짜: 2026/08/22
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
    _clock: Callable[[], datetime] = field(repr=False, compare=False)
    _trades: tuple[Trade, ...] = field(repr=False, compare=False)
    _current_kst_date: date = field(repr=False, compare=False)

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
        작성 날짜: 2026/08/22
        """
        # 같은 clock을 startup 복원과 후속 Trade의 KST account-day 계산에 사용한다.
        selected_clock = _utc_now if clock is None else clock
        if not callable(selected_clock):
            raise TypeError("clock must be callable")
        current_time = _normalize_utc_datetime(selected_clock(), "clock result")
        current_kst_date = current_time.astimezone(_KOREA_TIME_ZONE).date()

        # iterable을 한 번 materialize해 타입 검증과 집계가 같은 거래 순서를 보게 한다.
        try:
            restored_trades = tuple(trades)
        except TypeError as error:
            raise TypeError("trades must be an iterable of Trade") from error
        if any(not isinstance(trade, Trade) for trade in restored_trades):
            raise TypeError("trades must contain only Trade values")

        # 계산 context 안에서 모든 aggregate를 먼저 완성한 뒤 snapshot을 게시한다.
        with localcontext() as decimal_context:
            decimal_context.prec = 34
            decimal_context.rounding = ROUND_HALF_EVEN
            aggregate_values = self._aggregate_trades(
                restored_trades,
                current_kst_date,
            )

        object.__setattr__(self, "_clock", selected_clock)
        object.__setattr__(self, "_trades", restored_trades)
        object.__setattr__(self, "_current_kst_date", current_kst_date)
        for field_name, field_value in aggregate_values.items():
            object.__setattr__(self, field_name, field_value)  # 검증된 값을 한 번만 반영한다.

    def calculate_realized_result(
        self,
        summary: ExecutionSummary,
        allocated_cost_basis: Decimal,
    ) -> RealizedResult:
        """
        함수 이름: calculate_realized_result()
        기능: SELL 체결 금액과 quote fee 및 사전 고정 원가로 fee 포함 실현 결과를 계산한다.
        인자: summary -> terminal SELL fill을 집계한 ExecutionSummary
            allocated_cost_basis -> Position 변경 전에 고정한 체결 수량의 취득원가
        반환값: ADR-004 공식과 8자리 수익률을 담은 RealizedResult
        작성 날짜: 2026/08/22
        """
        # SELL summary와 사전 고정 원가의 canonical type 및 양수 불변식을 검증한다.
        if not isinstance(summary, ExecutionSummary):
            raise TypeError("summary must be an ExecutionSummary")
        if summary.side is not OrderSide.SELL:
            raise ValueError("realized result is available only for SELL")
        if not isinstance(allocated_cost_basis, Decimal):
            raise TypeError("allocated_cost_basis must be a Decimal")
        if not allocated_cost_basis.is_finite():
            raise ValueError("allocated_cost_basis must be finite")
        if allocated_cost_basis == Decimal("0"):
            raise InvalidZeroCostBasisError(
                "SELL allocated_cost_basis must not be zero"
            )
        if allocated_cost_basis < Decimal("0"):
            raise ValueError("allocated_cost_basis must be greater than zero")

        # 원금과 손익은 반올림하지 않고 수익률 표현만 8자리 half-even으로 고정한다.
        with localcontext() as decimal_context:
            decimal_context.prec = 34
            decimal_context.rounding = ROUND_HALF_EVEN
            realized_pnl = (
                summary.executed_amount
                - summary.fee_quote_amount
                - allocated_cost_basis
            )
            realized_return_rate = _quantize_rate(
                realized_pnl / allocated_cost_basis * Decimal("100")
            )

        return RealizedResult(
            allocated_cost_basis=allocated_cost_basis,
            realized_pnl=realized_pnl,
            realized_return_rate=realized_return_rate,
        )

    def apply_new_trade(self, trade: Trade) -> None:
        """
        함수 이름: apply_new_trade()
        기능: 새 durable 후보 Trade를 idempotent하게 반영해 모든 Performance aggregate를 갱신한다.
        인자: trade -> 누적 성과에 반영할 canonical Trade
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # duck typing으로 잘못된 거래가 누적 성과에 들어오지 않게 canonical 타입을 요구한다.
        if not isinstance(trade, Trade):
            raise TypeError("trade must be a Trade")

        # 같은 order의 동일 Trade는 no-op이고 다른 내용은 자동 덮어쓰지 않는다.
        existing_trade = next(
            (
                current_trade
                for current_trade in self._trades
                if current_trade.order_id == trade.order_id
            ),
            None,
        )
        if existing_trade is not None:
            if existing_trade == trade:
                return  # 같은 order와 같은 내용은 재처리해도 aggregate를 변경하지 않는다.
            raise OrderHistoryConflictError(
                f"order_id {trade.order_id} has conflicting trade content"
            )

        # 새 거래를 반영하는 시점의 KST 날짜로 daily aggregate 범위를 다시 계산한다.
        current_time = _normalize_utc_datetime(
            self._clock(),
            "clock result",
        )
        current_kst_date = current_time.astimezone(_KOREA_TIME_ZONE).date()
        next_trades = self._trades + (trade,)  # 기존 순서 뒤에 terminal Trade를 추가한다.

        # 계산이 전부 성공하기 전에는 기존 aggregate field를 하나도 변경하지 않는다.
        with localcontext() as decimal_context:
            decimal_context.prec = 34
            decimal_context.rounding = ROUND_HALF_EVEN
            aggregate_values = self._aggregate_trades(
                next_trades,
                current_kst_date,
            )

        # 모든 계산이 성공한 뒤 frozen snapshot field와 근거 tuple을 함께 commit한다.
        for field_name, field_value in aggregate_values.items():
            object.__setattr__(self, field_name, field_value)
        object.__setattr__(self, "_trades", next_trades)  # 후속 idempotency의 기준도 같이 교체한다.
        object.__setattr__(self, "_current_kst_date", current_kst_date)

    def get_performance(self) -> "Performance":
        """
        함수 이름: get_performance()
        기능: clock의 현재 KST account day를 반영한 Performance snapshot을 반환한다.
        인자: 없음
        반환값: 자기 자신인 Performance snapshot
        작성 날짜: 2026/08/21
        """
        current_time = _normalize_utc_datetime(
            self._clock(),
            "clock result",
        )
        current_kst_date = current_time.astimezone(_KOREA_TIME_ZONE).date()
        if current_kst_date == self._current_kst_date:
            return self  # 같은 account day에서는 기존 aggregate identity를 그대로 재사용한다.

        # 날짜만 바뀐 조회는 durable Trade 근거를 유지하고 daily field를 새 날로 재계산한다.
        with localcontext() as decimal_context:
            decimal_context.prec = 34
            decimal_context.rounding = ROUND_HALF_EVEN
            aggregate_values = self._aggregate_trades(
                self._trades,
                current_kst_date,
            )
        for field_name, field_value in aggregate_values.items():
            object.__setattr__(self, field_name, field_value)
        object.__setattr__(self, "_current_kst_date", current_kst_date)
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
        # 전체·당일 금액과 SELL 승패 counter를 한 순회에서 누적할 초기값을 준비한다.
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

        # BUY는 fee에만, SELL은 fee와 realized aggregate 모두에 반영한다.
        for trade in trades:
            trade_kst_date = trade.executed_at.astimezone(_KOREA_TIME_ZONE).date()
            total_fee += trade.fee_quote_amount
            if trade_kst_date == current_kst_date:
                daily_fee += trade.fee_quote_amount
            if trade.side is not OrderSide.SELL:
                continue  # BUY 원가는 Position에 속하며 실현손익 집계 대상이 아니다.

            # canonical SELL의 nullable field를 재확인해 손상된 복원 상태를 fail closed한다.
            allocated_cost_basis = trade.allocated_cost_basis
            realized_pnl = trade.realized_pnl
            realized_return_rate = trade.realized_return_rate
            if (
                allocated_cost_basis is None
                or realized_pnl is None
                or realized_return_rate is None
            ):
                raise ValueError("SELL Trade must contain realized result fields")

            # 전체 realized 금액·원가·개별 수익률과 손익 부호별 횟수를 함께 누적한다.
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

            # daily 수익률 분모와 분자는 동일 KST 날짜의 SELL만 사용한다.
            if trade_kst_date == current_kst_date:
                daily_allocated_cost_basis += allocated_cost_basis
                daily_realized_pnl += realized_pnl

        # 누적·당일 수익률은 각각의 손익 합계와 원가 합계로 가중 계산한다.
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
        # 완료 SELL이 있을 때만 산술 평균 수익률과 nullable win rate를 계산한다.
        if completed_sell_count > 0:
            average_sell_return_rate = _quantize_rate(
                sell_return_rate_sum / Decimal(completed_sell_count)
            )
            win_rate = _quantize_rate(
                Decimal(winning_sell_count)
                / Decimal(completed_sell_count)
                * Decimal("100")
            )

        return {  # 호출자가 계산 성공 뒤 한 번에 게시할 field mapping을 반환한다.
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
