"""실제 체결을 평균 원가 기반 ETHUSDT Position 상태에 반영한다."""

from __future__ import annotations

from dataclasses import dataclass, field
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


class LegacyFeeAccountingMigrationRequiredError(ValueError):
    """
    클래스 이름: LegacyFeeAccountingMigrationRequiredError
    기능: 열린 Position이 legacy base-fee 회계에 의존해 자동 거래를 재개할 수 없음을 나타낸다.
    작성 날짜: 2026/08/23
    """

    code = "HISTORY_ACCOUNTING_MIGRATION_REQUIRED"

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: history 원문이나 주문 식별자를 노출하지 않는 migration 오류를 생성한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        # 운영 오류에는 durable row 원문 대신 고정된 migration 분류만 포함한다.
        super().__init__(
            "open Position uses legacy base-fee accounting and requires migration"
        )


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


def _calculate_net_buy_quantity(
    executed_quantity: Decimal,
    fee_amount: Decimal,
    fee_asset: str,
) -> Decimal:
    """
    함수 이름: _calculate_net_buy_quantity()
    기능: BUY gross 체결 수량에서 base-asset 수수료를 차감한 실제 취득 ETH를 계산한다.
    인자: executed_quantity -> 거래소가 보고한 BUY gross 체결 수량
        fee_amount -> 원래 수수료 자산 단위의 수수료
        fee_asset -> ETH 또는 USDT 수수료 자산
    반환값: Position에 더할 양수 net base 수량
    작성 날짜: 2026/08/22
    """
    # USDT 수수료는 base 취득량을 바꾸지 않고 ETH 수수료만 실제 보유량에서 차감한다.
    net_quantity = (
        executed_quantity - fee_amount
        if fee_asset == "ETH"
        else executed_quantity
    )
    if net_quantity <= ZERO_DECIMAL:
        raise ValueError("BUY base fee must be smaller than executed quantity")

    return net_quantity


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
    # 남은 포지션에 비례 배분한 gross 체결값으로 표시 평단가에서 수수료를 제외한다.
    # 표시용 비례 배분의 반올림 차이가 기존 회계 상태 대조를 바꾸지 않도록 비교에서 제외한다.
    entry_executed_quantity: Decimal = field(default=ZERO_DECIMAL, compare=False)
    entry_executed_amount: Decimal = field(default=ZERO_DECIMAL, compare=False)

    @property
    def average_fill_price(self) -> Decimal:
        """
        함수 이름: average_fill_price()
        기능: 수수료 제외 체결 수량 가중 평균가를 손익용 평균 원가와 별도로 공개한다.
        인자: 없음
        반환값: 매수 체결 금액을 체결 수량으로 나눈 평균가
        작성 날짜: 2026/09/20
        """
        if self.entry_executed_quantity == ZERO_DECIMAL:
            return ZERO_DECIMAL
        with localcontext() as decimal_context:
            decimal_context.prec = DECIMAL_CALCULATION_PRECISION
            decimal_context.rounding = ROUND_HALF_EVEN
            return self.entry_executed_amount / self.entry_executed_quantity

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
        for name in ("entry_executed_quantity", "entry_executed_amount"):
            _validate_decimal(
                getattr(self, name), name, allow_zero=self.status is PositionStatus.CLOSED,
            )
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
        if self.entry_executed_quantity != ZERO_DECIMAL or self.entry_executed_amount != ZERO_DECIMAL:
            raise ValueError("A CLOSED Position requires zero entry execution values")
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
        "_legacy_base_fee_history_open",
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
        self._legacy_base_fee_history_open = False
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

    @property
    def requires_legacy_fee_accounting_migration(self) -> bool:
        """
        함수 이름: requires_legacy_fee_accounting_migration()
        기능: 현재 열린 lot에 JSONL v1 base-fee BUY 회계가 남았는지 반환한다.
        인자: 없음
        반환값: 명시적 history migration이 필요하면 True
        작성 날짜: 2026/08/23
        """
        with self._lock:
            return self._legacy_base_fee_history_open  # startup owner가 network 재개 전에 읽는 고정 gate다.

    def require_history_accounting_compatibility(self) -> None:
        """
        함수 이름: require_history_accounting_compatibility()
        기능: 현재 열린 Position이 신규 fee 회계로 안전하게 거래 가능한지 검증한다.
        인자: 없음
        반환값: migration이 필요하지 않으면 없음
        작성 날짜: 2026/08/23
        """
        # 이전 gross 수량으로 열린 lot은 account 여유 잔액과 무관하게 명시적 migration을 요구한다.
        with self._lock:
            if self._legacy_base_fee_history_open:
                raise LegacyFeeAccountingMigrationRequiredError()

    def detach_residual(self, quantity: Decimal, cost_basis: Decimal, step_size: Decimal) -> None:
        """
        함수 이름: detach_residual()
        기능: durable 잔여 장부와 정확히 같은 수량·원가만 전략 Position에서 분리한다.
        인자: quantity -> 장부의 ETH, cost_basis -> 미실현 원가, step_size -> 공식 주문 단위
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        for value in (quantity, cost_basis, step_size):
            _validate_decimal(value, "residual value", allow_zero=False)
        with self._lock:
            state = self._state
            if quantity >= step_size or state.quantity != quantity or state.cost_basis != cost_basis:
                raise ValueError("residual does not match the entire sub-step Position")
            self.require_history_accounting_compatibility()
            # 자산을 매도하거나 손실로 처리하지 않고 외부 잔여 장부에 보존한 전략 상태만 닫는다.
            self._state = PositionStateSnapshot(
                symbol=state.symbol, owner=None, quantity=ZERO_DECIMAL,
                average_entry_price=ZERO_DECIMAL, cost_basis=ZERO_DECIMAL,
                entered_at=None, status=PositionStatus.CLOSED, exit_reason=state.exit_reason,
            )

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
            if self._legacy_base_fee_history_open:
                raise LegacyFeeAccountingMigrationRequiredError()
            if summary.symbol != self._state.symbol:
                raise ValueError("Execution symbol does not match Position symbol")
            if summary.side is OrderSide.BUY:
                next_state = self._build_buy_state(summary, executed_at)
            elif summary.side is OrderSide.SELL:
                next_state = self._build_sell_state(summary)
            else:
                raise ValueError("Unsupported execution side")

            self._state = next_state  # 유효한 전체 snapshot 하나만 authoritative state로 발행한다.

    def clone(self) -> "Position":
        """
        함수 이름: clone()
        기능: 공개 상태를 바꾸지 않고 복구 후보를 재생할 독립 Position을 만든다.
        인자: 없음
        반환값: 불변 snapshot과 legacy 회계 표식을 복사한 Position
        작성 날짜: 2026/09/10
        """
        with self._lock:
            candidate = Position(self.symbol)
            candidate._state = self._state
            candidate._legacy_base_fee_history_open = self._legacy_base_fee_history_open
            return candidate

    def apply_historical_trade(
        self, trade: object, *, residual_quantity: Decimal = ZERO_DECIMAL,
        residual_cost_basis: Decimal = ZERO_DECIMAL,
    ) -> None:
        """
        함수 이름: apply_historical_trade()
        기능: durable Trade의 version별 aggregate 회계를 사용해 재시작 Position을 복원한다.
        인자: trade -> JSONL 검증을 통과한 canonical Trade,
            residual_quantity/residual_cost_basis -> 외부 매도에 소비된 별도 장부 수량·원가
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # Module import cycle을 만들지 않으면서도 structural object와 version을 함께 검증한다.
        from binance_auto_trader.domain.history.trade import (
            LEGACY_TRADE_SCHEMA_VERSION,
            Trade,
        )

        if not isinstance(trade, Trade):
            raise TypeError("trade must be a Trade")
        if trade.symbol != self.symbol:
            raise ValueError("historical Trade symbol does not match Position")
        for value in (residual_quantity, residual_cost_basis):
            _validate_decimal(value, "external residual allocation", allow_zero=True)
        if residual_quantity or residual_cost_basis:
            if (trade.schema_version != 4 or trade.exit_reason is not ExitReason.EXTERNAL_MANUAL
                    or trade.side is not OrderSide.SELL or residual_quantity <= 0 or residual_cost_basis <= 0):
                raise ValueError("residual allocation requires a verified external SELL")

        # History aggregate는 fill 재합성 없이 row version에 고정된 회계 공식을 적용한다.
        with self._lock:
            state = self._state
            is_legacy_trade = (
                trade.schema_version == LEGACY_TRADE_SCHEMA_VERSION
            )
            if self._legacy_base_fee_history_open and not is_legacy_trade:
                raise LegacyFeeAccountingMigrationRequiredError()
            if trade.side is OrderSide.BUY:
                if state.owner is not None and state.owner is not trade.strategy:
                    raise ValueError(
                        "historical BUY strategy does not own the Position"
                    )
                with localcontext() as decimal_context:
                    decimal_context.prec = DECIMAL_CALCULATION_PRECISION
                    decimal_context.rounding = ROUND_HALF_EVEN
                    acquired_quantity = (
                        trade.executed_quantity
                        if is_legacy_trade
                        else _calculate_net_buy_quantity(
                            trade.executed_quantity,
                            trade.base_fee_amount,
                            "ETH",
                        )
                    )
                    acquisition_fee = (
                        trade.fee_quote_amount
                        if is_legacy_trade
                        else trade.non_base_fee_quote_amount
                    )
                    next_quantity = state.quantity + acquired_quantity
                    next_cost_basis = (
                        state.cost_basis
                        + trade.executed_amount
                        + acquisition_fee
                    )
                    next_average_entry_price = next_cost_basis / next_quantity
                    next_entry_quantity = state.entry_executed_quantity + trade.executed_quantity
                    next_entry_amount = state.entry_executed_amount + trade.executed_amount
                next_state = PositionStateSnapshot(
                    symbol=state.symbol,
                    owner=trade.strategy,
                    quantity=next_quantity,
                    average_entry_price=next_average_entry_price,
                    entry_executed_quantity=next_entry_quantity,
                    entry_executed_amount=next_entry_amount,
                    cost_basis=next_cost_basis,
                    entered_at=(
                        state.entered_at
                        if state.entered_at is not None
                        else trade.executed_at
                    ),
                    status=PositionStatus.OPEN,
                    exit_reason=None,
                )
                legacy_base_fee_buy = (
                    is_legacy_trade
                    and trade.fee_asset == "ETH"
                    and trade.fee_amount > ZERO_DECIMAL
                )
                self._state = next_state
                self._legacy_base_fee_history_open = (
                    self._legacy_base_fee_history_open
                    or legacy_base_fee_buy
                )
                return  # BUY durable aggregate 한 건을 정확히 한 번 복원했다.

            # v2 SELL base fee는 미지원이며 v1은 기존 gross 회계 의미 그대로만 재생한다.
            if (
                trade.schema_version != LEGACY_TRADE_SCHEMA_VERSION
                and trade.base_fee_amount > ZERO_DECIMAL
            ):
                raise ValueError(
                    "historical SELL base fee requires asset-flow reconciliation"
                )
            if state.status is not PositionStatus.OPEN:
                raise ValueError("historical SELL requires an open Position")
            if state.owner is not trade.strategy:
                raise ValueError("historical SELL strategy does not own Position")
            with localcontext() as decimal_context:
                decimal_context.prec = DECIMAL_CALCULATION_PRECISION
                position_sold = trade.executed_quantity - residual_quantity
            if residual_quantity and position_sold != state.quantity:
                raise ValueError("external residual is used only after exhausting the open lot")
            if position_sold <= 0 or position_sold > state.quantity:
                raise ValueError("historical SELL exceeds Position quantity")
            with localcontext() as decimal_context:
                decimal_context.prec = DECIMAL_CALCULATION_PRECISION
                decimal_context.rounding = ROUND_HALF_EVEN
                allocated_cost_basis = (
                    state.cost_basis
                    if position_sold == state.quantity
                    else (
                        state.cost_basis
                        * position_sold
                        / state.quantity
                    )
                )
                if residual_quantity and trade.allocated_cost_basis != allocated_cost_basis + residual_cost_basis:
                    raise ValueError("external SELL cost conflicts with position and residual ledger")
                next_quantity = state.quantity - position_sold
                next_cost_basis = state.cost_basis - allocated_cost_basis
            if next_quantity == ZERO_DECIMAL:
                next_state = PositionStateSnapshot(
                    symbol=state.symbol,
                    owner=None,
                    quantity=ZERO_DECIMAL,
                    average_entry_price=ZERO_DECIMAL,
                    cost_basis=ZERO_DECIMAL,
                    entered_at=None,
                    status=PositionStatus.CLOSED,
                    exit_reason=trade.exit_reason,
                )
                self._state = next_state
                self._legacy_base_fee_history_open = False
                return  # 전량 SELL은 남은 원가를 반올림 없이 모두 제거한다.

            # 부분 SELL은 이전 평균원가를 유지하는 average-cost 잔여 상태를 게시한다.
            with localcontext() as decimal_context:
                decimal_context.prec = DECIMAL_CALCULATION_PRECISION
                decimal_context.rounding = ROUND_HALF_EVEN
                next_average_entry_price = next_cost_basis / next_quantity
                next_entry_quantity = state.entry_executed_quantity * next_quantity / state.quantity
                next_entry_amount = state.entry_executed_amount * next_quantity / state.quantity
            next_state = PositionStateSnapshot(
                symbol=state.symbol,
                owner=state.owner,
                quantity=next_quantity,
                average_entry_price=next_average_entry_price,
                entry_executed_quantity=next_entry_quantity,
                entry_executed_amount=next_entry_amount,
                cost_basis=next_cost_basis,
                entered_at=state.entered_at,
                status=PositionStatus.OPEN,
                exit_reason=trade.exit_reason,
            )
            self._state = next_state  # legacy migration flag는 부분 SELL 뒤 열린 lot과 함께 유지한다.

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

        # ADR-004의 fee 포함 원가는 유지하되 base fee는 실제 취득 ETH 수량에서도 차감한다.
        with localcontext() as decimal_context:
            decimal_context.prec = DECIMAL_CALCULATION_PRECISION
            decimal_context.rounding = ROUND_HALF_EVEN
            acquired_quantity = _calculate_net_buy_quantity(
                summary.executed_quantity,
                summary.base_fee_amount,
                "ETH",
            )
            next_quantity = state.quantity + acquired_quantity
            next_cost_basis = (
                state.cost_basis
                + summary.executed_amount
                + (
                    summary.non_base_fee_quote_amount
                )
            )
            next_average_entry_price = next_cost_basis / next_quantity
            next_entry_quantity = state.entry_executed_quantity + summary.executed_quantity
            next_entry_amount = state.entry_executed_amount + summary.executed_amount
        entered_at = (
            state.entered_at if state.entered_at is not None else executed_at
        )  # 최초 BUY 시각은 이후 추가 체결에도 바꾸지 않는다.

        return PositionStateSnapshot(
            symbol=state.symbol,
            owner=summary.strategy,
            quantity=next_quantity,
            average_entry_price=next_average_entry_price,
            entry_executed_quantity=next_entry_quantity,
            entry_executed_amount=next_entry_amount,
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
        if summary.base_fee_amount > ZERO_DECIMAL:
            raise ValueError(
                "SELL base fee requires asset-flow reconciliation"
            )
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
            next_entry_quantity = state.entry_executed_quantity * next_quantity / state.quantity
            next_entry_amount = state.entry_executed_amount * next_quantity / state.quantity
        return PositionStateSnapshot(
            symbol=state.symbol,
            owner=state.owner,
            quantity=next_quantity,
            average_entry_price=next_average_entry_price,
            entry_executed_quantity=next_entry_quantity,
            entry_executed_amount=next_entry_amount,
            cost_basis=next_cost_basis,
            entered_at=state.entered_at,
            status=PositionStatus.OPEN,
            exit_reason=summary.exit_reason,
        )
