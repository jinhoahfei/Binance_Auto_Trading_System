"""실제 체결을 평균 원가 기반 ETHUSDT Position 상태에 반영한다."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from enum import Enum
from threading import RLock

from .order import ExecutionSummary
from .states import ExitReason, OrderSide, StrategyType


# Position의 0 표현, 지원 상품과 Decimal 계산 정밀도를 module 전반에서 공유한다.
ZERO_DECIMAL = Decimal("0")
SUPPORTED_POSITION_SYMBOL = "ETHUSDT"
DECIMAL_CALCULATION_PRECISION = 34


def _validate_decimal(
    value: object,
    field_name: str,
    *,
    allow_zero: bool,
) -> None:
    """
    함수 이름: _validate_decimal()
    기능: Position 금융 수치가 유한한 Decimal이며 허용 범위 안인지 검증한다.
    인자: value -> 검증할 금융 수치
        field_name -> 오류 메시지에 사용할 필드 이름
        allow_zero -> 0을 유효한 값으로 허용할지 여부
    반환값: 없음
    작성 날짜: 2026/08/22
    """
    # 금융 계산 경계에는 float, 무한대와 NaN이 들어오지 못하게 한다.
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be a Decimal")
    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite")

    # 체결 수치는 양수, 닫힌 Position의 누적 수치는 0 이상이어야 한다.
    minimum_is_valid = value >= ZERO_DECIMAL if allow_zero else value > ZERO_DECIMAL
    if not minimum_is_valid:
        comparison = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{field_name} must be {comparison}")


def _validate_symbol(symbol: object) -> None:
    """
    함수 이름: _validate_symbol()
    기능: Phase 8 Position이 지원하는 정확한 ETHUSDT symbol인지 검증한다.
    인자: symbol -> 검증할 거래 symbol
    반환값: 없음
    작성 날짜: 2026/08/22
    """
    # 대소문자 보정이나 다른 상품 fallback 없이 정확한 지원 symbol만 받는다.
    if not isinstance(symbol, str):
        raise TypeError("symbol must be a string")
    if symbol != SUPPORTED_POSITION_SYMBOL:
        raise ValueError("Position supports only ETHUSDT")


def _normalize_utc_datetime(value: object, field_name: str) -> datetime:
    """
    함수 이름: _normalize_utc_datetime()
    기능: 체결 시각이 timezone-aware UTC인지 검증하고 canonical UTC로 정규화한다.
    인자: value -> 검증할 datetime 값
        field_name -> 오류 메시지에 사용할 필드 이름
    반환값: timezone.utc로 정규화한 datetime
    작성 날짜: 2026/08/22
    """
    # naive 시각과 UTC가 아닌 offset은 거래 이력 경계를 모호하게 하므로 거부한다.
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must use UTC")

    return value.astimezone(timezone.utc)  # UTC identity를 snapshot 전반에서 통일한다.


class PositionStatus(str, Enum):
    """
    클래스 이름: PositionStatus
    기능: 실제 보유 수량을 기준으로 Position의 열림과 닫힘 상태를 구분한다.
    작성 날짜: 2026/08/22
    """

    OPEN = "OPEN"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class PositionStateSnapshot:
    """
    클래스 이름: PositionStateSnapshot
    기능: mutable Position의 한 시점 상태를 외부 변경이 불가능한 값으로 공개한다.
    작성 날짜: 2026/08/22
    """

    symbol: str
    owner: StrategyType | None
    quantity: Decimal
    average_entry_price: Decimal
    cost_basis: Decimal
    entered_at: datetime | None
    status: PositionStatus
    exit_reason: ExitReason | None

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 공개 snapshot의 타입과 OPEN/CLOSED 조합 불변식을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # 공통 값은 상태 조합을 검사하기 전에 모두 독립적으로 검증한다.
        _validate_symbol(self.symbol)
        _validate_decimal(self.quantity, "quantity", allow_zero=True)
        _validate_decimal(
            self.average_entry_price,
            "average_entry_price",
            allow_zero=True,
        )
        _validate_decimal(self.cost_basis, "cost_basis", allow_zero=True)
        if not isinstance(self.status, PositionStatus):
            raise TypeError("status must be a PositionStatus")
        if self.owner is not None and not isinstance(self.owner, StrategyType):
            raise TypeError("owner must be a StrategyType or None")
        if self.exit_reason is not None and not isinstance(
            self.exit_reason,
            ExitReason,
        ):
            raise TypeError("exit_reason must be an ExitReason or None")

        # OPEN 상태는 실제 양수 보유량, 원가, owner와 최초 체결 시각을 모두 요구한다.
        if self.status is PositionStatus.OPEN:
            if self.quantity <= ZERO_DECIMAL:
                raise ValueError("An OPEN Position requires positive quantity")
            if self.cost_basis <= ZERO_DECIMAL:
                raise ValueError("An OPEN Position requires positive cost_basis")
            if self.average_entry_price <= ZERO_DECIMAL:
                raise ValueError(
                    "An OPEN Position requires positive average_entry_price"
                )
            if self.owner is None:
                raise ValueError("An OPEN Position requires an owner")
            if self.entered_at is None:
                raise ValueError("An OPEN Position requires entered_at")

            normalized_entered_at = _normalize_utc_datetime(
                self.entered_at,
                "entered_at",
            )
            object.__setattr__(
                self,
                "entered_at",
                normalized_entered_at,
            )  # frozen snapshot 안의 UTC 표현만 canonical 값으로 교체한다.

            # 공개 평균가는 같은 cost basis와 quantity에서 다시 계산한 값과 일치해야 한다.
            with localcontext() as decimal_context:
                decimal_context.prec = DECIMAL_CALCULATION_PRECISION
                decimal_context.rounding = ROUND_HALF_EVEN
                expected_average_entry_price = self.cost_basis / self.quantity
            if self.average_entry_price != expected_average_entry_price:
                raise ValueError(
                    "average_entry_price must equal cost_basis / quantity"
                )
            return

        # CLOSED 상태는 과거 청산 사유 외에 현재 포지션 값을 남기지 않는다.
        if self.quantity != ZERO_DECIMAL:
            raise ValueError("A CLOSED Position requires zero quantity")
        if self.cost_basis != ZERO_DECIMAL:
            raise ValueError("A CLOSED Position requires zero cost_basis")
        if self.average_entry_price != ZERO_DECIMAL:
            raise ValueError("A CLOSED Position requires zero average_entry_price")
        if self.owner is not None:
            raise ValueError("A CLOSED Position cannot have an owner")
        if self.entered_at is not None:
            raise ValueError("A CLOSED Position cannot have entered_at")


class Position:
    """
    클래스 이름: Position
    기능: ETHUSDT 실제 체결 수량과 average-cost 취득원가를 원자적으로 관리한다.
    작성 날짜: 2026/08/22
    """

    __slots__ = (
        "_lock",
        "_state",
    )

    def __init__(self, symbol: str = SUPPORTED_POSITION_SYMBOL) -> None:
        """
        함수 이름: __init__()
        기능: owner와 보유량이 없는 CLOSED Position을 생성한다.
        인자: symbol -> Position이 관리할 고정 Spot 거래 symbol
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        _validate_symbol(symbol)  # 생성 뒤 symbol 교체가 없으므로 최초 경계에서 고정한다.

        # 최초 상태도 이후 mutation과 같은 immutable snapshot 계약을 사용한다.
        self._lock = RLock()
        self._state = PositionStateSnapshot(
            symbol=symbol,
            owner=None,
            quantity=ZERO_DECIMAL,
            average_entry_price=ZERO_DECIMAL,
            cost_basis=ZERO_DECIMAL,
            entered_at=None,
            status=PositionStatus.CLOSED,
            exit_reason=None,
        )

    @property
    def symbol(self) -> str:
        """
        함수 이름: symbol()
        기능: Position이 관리하는 고정 거래 symbol을 반환한다.
        인자: 없음
        반환값: ETHUSDT symbol
        작성 날짜: 2026/08/22
        """
        return self._state.symbol  # Position lifetime 동안 같은 symbol을 반환한다.

    @property
    def owner(self) -> StrategyType | None:
        """
        함수 이름: owner()
        기능: 실제 보유 Position을 소유하는 전략을 반환한다.
        인자: 없음
        반환값: Case 전략 또는 CLOSED이면 None
        작성 날짜: 2026/08/22
        """
        return self._state.owner  # 실제 BUY fill이 적용된 뒤의 owner만 공개한다.

    @property
    def quantity(self) -> Decimal:
        """
        함수 이름: quantity()
        기능: 실제 체결로 누적된 현재 ETH 수량을 반환한다.
        인자: 없음
        반환값: 현재 Position 수량
        작성 날짜: 2026/08/22
        """
        return self._state.quantity  # float 변환 없는 authoritative Decimal이다.

    @property
    def average_entry_price(self) -> Decimal:
        """
        함수 이름: average_entry_price()
        기능: BUY quote 수수료를 포함한 average-cost 진입가를 반환한다.
        인자: 없음
        반환값: 현재 평균 진입가 또는 CLOSED이면 Decimal 0
        작성 날짜: 2026/08/22
        """
        return self._state.average_entry_price  # BUY quote fee를 포함한 평균 원가다.

    @property
    def cost_basis(self) -> Decimal:
        """
        함수 이름: cost_basis()
        기능: 남은 Position에 배분된 quote 취득원가를 반환한다.
        인자: 없음
        반환값: 현재 취득원가 또는 CLOSED이면 Decimal 0
        작성 날짜: 2026/08/22
        """
        return self._state.cost_basis  # 남은 수량에만 대응하는 quote 원가다.

    @property
    def entered_at(self) -> datetime | None:
        """
        함수 이름: entered_at()
        기능: 현재 Position을 처음 연 BUY 체결 UTC 시각을 반환한다.
        인자: 없음
        반환값: 최초 진입 시각 또는 CLOSED이면 None
        작성 날짜: 2026/08/22
        """
        return self._state.entered_at  # 같은 Position의 추가 BUY에는 바뀌지 않는다.

    @property
    def status(self) -> PositionStatus:
        """
        함수 이름: status()
        기능: 실제 수량 기준 OPEN 또는 CLOSED 상태를 반환한다.
        인자: 없음
        반환값: 현재 PositionStatus
        작성 날짜: 2026/08/22
        """
        return self._state.status  # 수량과 함께 생성된 immutable state의 값이다.

    @property
    def exit_reason(self) -> ExitReason | None:
        """
        함수 이름: exit_reason()
        기능: 가장 최근 SELL 체결에 연결된 청산 사유를 반환한다.
        인자: 없음
        반환값: 청산 사유 또는 아직 매도하지 않았으면 None
        작성 날짜: 2026/08/22
        """
        return self._state.exit_reason  # 부분·전량 SELL의 최신 사유를 보존한다.

    def get_snapshot(self) -> PositionStateSnapshot:
        """
        함수 이름: get_snapshot()
        기능: 한 mutation 시점의 전체 Position 상태를 immutable snapshot으로 반환한다.
        인자: 없음
        반환값: 외부에서 변경할 수 없는 PositionStateSnapshot
        작성 날짜: 2026/08/22
        """
        with self._lock:
            return self._state  # frozen snapshot identity는 후속 mutation 뒤에도 안전하다.

    def get_cost_basis(self, executed_quantity: Decimal) -> Decimal:
        """
        함수 이름: get_cost_basis()
        기능: SELL 적용 전 현재 average-cost에서 매도 수량에 배분할 원가를 계산한다.
        인자: executed_quantity -> 원가를 배분할 실제 SELL 체결 수량
        반환값: Position mutation 전에 고정할 quote 취득원가
        작성 날짜: 2026/08/22
        """
        # 원가를 나누기 전에 실제 SELL fill 수량이 양수 Decimal인지 검증한다.
        _validate_decimal(
            executed_quantity,
            "executed_quantity",
            allow_zero=False,
        )

        with self._lock:
            state = self._state
            if state.status is not PositionStatus.OPEN:
                raise ValueError("Cannot allocate cost basis from a CLOSED Position")
            if executed_quantity > state.quantity:
                raise ValueError("executed_quantity exceeds Position quantity")

            # 전량 청산은 나눗셈 오차 없이 남은 cost basis 전체를 정확히 배분한다.
            if executed_quantity == state.quantity:
                return state.cost_basis

            # 계산 context를 고정해 process 전역 Decimal 설정과 무관한 결과를 만든다.
            with localcontext() as decimal_context:
                decimal_context.prec = DECIMAL_CALCULATION_PRECISION
                decimal_context.rounding = ROUND_HALF_EVEN
                return (
                    state.cost_basis * executed_quantity / state.quantity
                )  # ADR-004의 average-cost 배분 공식을 Decimal로만 계산한다.

    def apply_execution(self, summary: ExecutionSummary) -> None:
        """
        함수 이름: apply_execution()
        기능: 하나의 실제 execution summary를 검증한 뒤 Position 전체 상태에 원자적으로 반영한다.
        인자: summary -> Order가 실제 fill들로 만든 정규화 ExecutionSummary
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # Order 이외 객체가 우연히 같은 attribute를 가져도 mutation source로 받지 않는다.
        if not isinstance(summary, ExecutionSummary):
            raise TypeError("summary must be an ExecutionSummary")

        # mutation 전에 summary 공통 계약을 모두 검증해 부분 반영을 차단한다.
        _validate_symbol(summary.symbol)
        _validate_decimal(
            summary.executed_quantity,
            "executed_quantity",
            allow_zero=False,
        )
        _validate_decimal(
            summary.executed_amount,
            "executed_amount",
            allow_zero=False,
        )
        _validate_decimal(
            summary.fee_quote_amount,
            "fee_quote_amount",
            allow_zero=True,
        )
        executed_at = _normalize_utc_datetime(summary.executed_at, "executed_at")
        if not isinstance(summary.side, OrderSide):
            raise TypeError("summary.side must be an OrderSide")
        if not isinstance(summary.strategy, StrategyType):
            raise TypeError("summary.strategy must be a StrategyType")
        if summary.exit_reason is not None and not isinstance(
            summary.exit_reason,
            ExitReason,
        ):
            raise TypeError("summary.exit_reason must be an ExitReason or None")

        # next snapshot을 완전히 만든 뒤 한 번만 교체해 모든 실패를 원자적 no-op으로 만든다.
        with self._lock:
            if summary.symbol != self._state.symbol:
                raise ValueError("Execution symbol does not match Position symbol")
            if summary.side is OrderSide.BUY:
                next_state = self._build_buy_state(summary, executed_at)
            elif summary.side is OrderSide.SELL:
                next_state = self._build_sell_state(summary)
            else:
                raise ValueError("Unsupported execution side")

            self._state = next_state  # 유효한 전체 snapshot 하나만 authoritative state로 발행한다.

    def _build_buy_state(
        self,
        summary: ExecutionSummary,
        executed_at: datetime,
    ) -> PositionStateSnapshot:
        """
        함수 이름: _build_buy_state()
        기능: BUY 체결 금액과 quote 수수료를 더한 다음 OPEN snapshot을 계산한다.
        인자: summary -> 검증을 마친 BUY execution summary
            executed_at -> canonical UTC로 정규화한 실제 체결 시각
        반환값: 아직 commit하지 않은 다음 PositionStateSnapshot
        작성 날짜: 2026/08/22
        """
        # 기존 Position owner가 있으면 같은 전략의 추가 BUY만 평균 원가에 합산한다.
        state = self._state
        if state.owner is not None and state.owner is not summary.strategy:
            raise ValueError("BUY strategy does not own the open Position")

        # ADR-004에 따라 BUY quote 수수료를 취득원가에 포함해 새 평균가를 계산한다.
        with localcontext() as decimal_context:
            decimal_context.prec = DECIMAL_CALCULATION_PRECISION
            decimal_context.rounding = ROUND_HALF_EVEN
            next_quantity = state.quantity + summary.executed_quantity
            next_cost_basis = (
                state.cost_basis
                + summary.executed_amount
                + summary.fee_quote_amount
            )
            next_average_entry_price = next_cost_basis / next_quantity
        entered_at = (
            state.entered_at if state.entered_at is not None else executed_at
        )  # 최초 BUY 시각은 이후 추가 체결에도 바꾸지 않는다.

        return PositionStateSnapshot(
            symbol=state.symbol,
            owner=summary.strategy,
            quantity=next_quantity,
            average_entry_price=next_average_entry_price,
            cost_basis=next_cost_basis,
            entered_at=entered_at,
            status=PositionStatus.OPEN,
            exit_reason=None,
        )

    def _build_sell_state(
        self,
        summary: ExecutionSummary,
    ) -> PositionStateSnapshot:
        """
        함수 이름: _build_sell_state()
        기능: SELL 적용 전 배분 원가를 고정하고 잔여 Position snapshot을 계산한다.
        인자: summary -> 검증을 마친 SELL execution summary
        반환값: 아직 commit하지 않은 다음 PositionStateSnapshot
        작성 날짜: 2026/08/22
        """
        # OPEN 상태, owner와 보유 수량을 확인한 뒤에만 SELL 원가를 배분한다.
        state = self._state
        if state.status is not PositionStatus.OPEN:
            raise ValueError("Cannot apply SELL to a CLOSED Position")
        if state.owner is not summary.strategy:
            raise ValueError("SELL strategy does not own the open Position")
        if summary.executed_quantity > state.quantity:
            raise ValueError("SELL executed_quantity exceeds Position quantity")

        # Position을 바꾸기 전에 현재 수량 기준 배분 원가를 먼저 고정한다.
        allocated_cost_basis = self.get_cost_basis(summary.executed_quantity)
        with localcontext() as decimal_context:
            decimal_context.prec = DECIMAL_CALCULATION_PRECISION
            decimal_context.rounding = ROUND_HALF_EVEN
            next_quantity = state.quantity - summary.executed_quantity
        if next_quantity == ZERO_DECIMAL:
            return PositionStateSnapshot(
                symbol=state.symbol,
                owner=None,
                quantity=ZERO_DECIMAL,
                average_entry_price=ZERO_DECIMAL,
                cost_basis=ZERO_DECIMAL,
                entered_at=None,
                status=PositionStatus.CLOSED,
                exit_reason=summary.exit_reason,
            )

        # 부분 매도는 동일하게 배분한 원가를 정확히 한 번 차감하고 평균가를 다시 계산한다.
        with localcontext() as decimal_context:
            decimal_context.prec = DECIMAL_CALCULATION_PRECISION
            decimal_context.rounding = ROUND_HALF_EVEN
            next_cost_basis = state.cost_basis - allocated_cost_basis
            next_average_entry_price = next_cost_basis / next_quantity
        return PositionStateSnapshot(
            symbol=state.symbol,
            owner=state.owner,
            quantity=next_quantity,
            average_entry_price=next_average_entry_price,
            cost_basis=next_cost_basis,
            entered_at=state.entered_at,
            status=PositionStatus.OPEN,
            exit_reason=summary.exit_reason,
        )
