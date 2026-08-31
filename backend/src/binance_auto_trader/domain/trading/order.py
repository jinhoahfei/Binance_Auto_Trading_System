"""주문 의도와 정규화된 거래소 결과를 체결 요약으로 조정하는 Order aggregate를 정의한다."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from enum import Enum
import re

from ..common import RegimeType
from .states import ExitReason, OrderSide, StrategyType


# 지원 상품과 계산 정밀도는 Order, Fill과 ExecutionSummary가 같은 기준을 공유하게 한다.
_SUPPORTED_SYMBOL = "ETHUSDT"
_DECIMAL_ZERO = Decimal("0")
_DECIMAL_PRECISION = 34
_ORDER_ID_PATTERN = re.compile(r"^[1-9][0-9]*$")
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]+$")


class OrderStatus(str, Enum):
    """
    클래스 이름: OrderStatus
    기능: 정규화된 주문의 불명·활성·terminal 상태를 정의한다.
    작성 날짜: 2026/08/22
    """

    UNKNOWN = "UNKNOWN"
    PENDING_NEW = "PENDING_NEW"
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    EXPIRED_IN_MATCH = "EXPIRED_IN_MATCH"


class OrderResultFailureKind(str, Enum):
    """
    클래스 이름: OrderResultFailureKind
    기능: Controller가 문자열 parsing 없이 사용할 거래소 실패 사실의 정규화 종류를 정의한다.
    작성 날짜: 2026/08/23
    """

    SUBMISSION_REJECTED = "SUBMISSION_REJECTED"
    ORDER_NOT_VISIBLE = "ORDER_NOT_VISIBLE"


class PendingOrderRecoveryLifecycle(str, Enum):
    """
    클래스 이름: PendingOrderRecoveryLifecycle
    기능: durable sidecar가 증명한 주문 제출·체결·history commit lifecycle을 구분한다.
    작성 날짜: 2026/08/25
    """

    PREPARED = "PREPARED"
    SUBMITTED = "SUBMITTED"
    UNKNOWN = "UNKNOWN"
    PARTIAL = "PARTIAL"
    TERMINAL = "TERMINAL"
    HISTORY_COMMITTED = "HISTORY_COMMITTED"
    SUBMISSION_REJECTED_CONFIRMED = "SUBMISSION_REJECTED_CONFIRMED"


class PendingOrderSubmissionProvenance(str, Enum):
    """
    클래스 이름: PendingOrderSubmissionProvenance
    기능: PREPARED journal이 REST POST 시작 전 상태를 증명하는지 구분한다.
    작성 날짜: 2026/08/29
    """

    LEGACY_PREPARED_AMBIGUOUS = "LEGACY_PREPARED_AMBIGUOUS"
    SUBMITTED_FSYNC_PRECEDES_REST_POST = (
        "SUBMITTED_FSYNC_PRECEDES_REST_POST"
    )


# 상태 집합은 Controller가 신규 제출과 reconciliation branch를 문자열 추측 없이 판정하게 한다.
ACTIVE_ORDER_STATUSES = frozenset(
    {
        OrderStatus.PENDING_NEW,
        OrderStatus.NEW,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.PENDING_CANCEL,
    }
)
TERMINAL_ORDER_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
        OrderStatus.EXPIRED_IN_MATCH,
    }
)
_ACTIVE_STATUS_PROGRESS = {
    OrderStatus.PENDING_NEW: 0,
    OrderStatus.NEW: 1,
    OrderStatus.PARTIALLY_FILLED: 2,
    OrderStatus.PENDING_CANCEL: 3,
}


class OrderResultConflictError(ValueError):
    """
    클래스 이름: OrderResultConflictError
    기능: 같은 주문 또는 fill 식별자에 서로 다른 거래소 사실이 관찰된 충돌을 나타낸다.
    작성 날짜: 2026/08/22
    """

    code = "ORDER_RESULT_CONFLICT"


class DuplicateFillConflictError(ValueError):
    """
    클래스 이름: DuplicateFillConflictError
    기능: 하나의 실행 요약에 중복 fill 식별자가 포함된 충돌을 나타낸다.
    작성 날짜: 2026/08/22
    """

    code = "DUPLICATE_FILL_CONFLICT"


class OrderStateTransitionError(ValueError):
    """
    클래스 이름: OrderStateTransitionError
    기능: 최초 결과와 재조회 결과의 적용 순서 또는 terminal 상태 충돌을 나타낸다.
    작성 날짜: 2026/08/22
    """

    code = "ORDER_STATE_TRANSITION_INVALID"


class ExecutionSummaryUnavailableError(ValueError):
    """
    클래스 이름: ExecutionSummaryUnavailableError
    기능: 실제 fill이나 기본 terminal 조건이 없어 체결 요약을 만들 수 없음을 나타낸다.
    작성 날짜: 2026/08/22
    """

    code = "EXECUTION_SUMMARY_UNAVAILABLE"


class MixedFeeAssetError(ValueError):
    """
    클래스 이름: MixedFeeAssetError
    기능: 단일 fee asset schema로 안전하게 집계할 수 없는 서로 다른 수수료 자산을 나타낸다.
    작성 날짜: 2026/08/22
    """

    code = "MIXED_FEE_ASSETS"

    def __init__(self, fee_assets: frozenset[str]) -> None:
        """
        함수 이름: __init__()
        기능: 정렬한 수수료 자산 목록을 credential 없는 typed 오류로 보존한다.
        인자: fee_assets -> 한 체결 요약에서 관찰된 서로 다른 fee asset 집합
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # 호출자가 혼합 자산을 typed detail로 진단할 수 있도록 원래 집합과 표시 문구를 함께 만든다.
        self.fee_assets = fee_assets
        joined_assets = ", ".join(sorted(fee_assets))
        super().__init__(f"execution fills use mixed fee assets: {joined_assets}")


class FeeAssetReconciliationRequiredError(ValueError):
    """
    클래스 이름: FeeAssetReconciliationRequiredError
    기능: ETH와 USDT 외 fee asset의 quote 환산 근거가 없어 reconciliation이 필요함을 나타낸다.
    작성 날짜: 2026/08/22
    """

    code = "FEE_ASSET_CONVERSION_REQUIRED"

    def __init__(self, fee_asset: str) -> None:
        """
        함수 이름: __init__()
        기능: 임의 환율 없이 환산이 필요한 fee asset을 typed 오류에 보존한다.
        인자: fee_asset -> ETH와 USDT가 아닌 수수료 자산
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # 임의 환율을 적용하지 않고 reconciliation 대상 자산을 오류 객체에 그대로 남긴다.
        self.fee_asset = fee_asset
        super().__init__(
            f"fee asset {fee_asset} requires quote conversion reconciliation"
        )


def _validate_non_empty_text(value: object, field_name: str) -> None:
    """
    함수 이름: _validate_non_empty_text()
    기능: 식별자와 코드가 앞뒤 공백 없는 비어 있지 않은 문자열인지 검증한다.
    인자: value -> 검증할 값
        field_name -> 오류에 표시할 필드 이름
    반환값: 없음
    작성 날짜: 2026/08/22
    """
    # 자동 문자열 변환이나 trim 없이 canonical 문자열 형식을 그대로 요구한다.
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value or value != value.strip():
        raise ValueError(f"{field_name} must be non-empty without outer whitespace")


def _validate_exchange_order_id(value: object, field_name: str) -> None:
    """
    함수 이름: _validate_exchange_order_id()
    기능: 거래소 주문 ID를 durable Trade와 호환되는 양의 정수 문자열로 검증한다.
    인자: value -> 검증할 거래소 주문 ID
        field_name -> 오류에 표시할 필드 이름
    반환값: 없음
    작성 날짜: 2026/08/22
    """
    # 공통 문자열 조건을 통과한 뒤 durable ID의 양의 정수 표기까지 제한한다.
    _validate_non_empty_text(value, field_name)
    if _ORDER_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a positive integer string")


def _validate_symbol(value: object, field_name: str) -> None:
    """
    함수 이름: _validate_symbol()
    기능: symbol 형식과 현재 지원하는 ETHUSDT 상품을 검증한다.
    인자: value -> 검증할 symbol
        field_name -> 오류에 표시할 필드 이름
    반환값: 없음
    작성 날짜: 2026/08/22
    """
    # 대문자 영숫자 wire 형식과 현재 지원 상품을 순서대로 검증한다.
    _validate_non_empty_text(value, field_name)
    if _SYMBOL_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must contain uppercase ASCII letters and digits")
    if value != _SUPPORTED_SYMBOL:
        raise ValueError(f"{field_name} must be {_SUPPORTED_SYMBOL}")


def _validate_fee_asset(value: object, field_name: str) -> None:
    """
    함수 이름: _validate_fee_asset()
    기능: 수수료 자산 코드를 대문자 영숫자 문자열로 검증한다.
    인자: value -> 검증할 fee asset
        field_name -> 오류에 표시할 필드 이름
    반환값: 없음
    작성 날짜: 2026/08/22
    """
    # 공백 없는 대문자 영숫자 asset code만 수수료 집계에 허용한다.
    _validate_non_empty_text(value, field_name)
    if _SYMBOL_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must contain uppercase ASCII letters and digits")


def _validate_finite_decimal(value: object, field_name: str) -> None:
    """
    함수 이름: _validate_finite_decimal()
    기능: 금융 수치가 float가 아닌 유한 Decimal인지 검증한다.
    인자: value -> 검증할 금융 수치
        field_name -> 오류에 표시할 필드 이름
    반환값: 없음
    작성 날짜: 2026/08/22
    """
    # 금융 수치에는 float 변환 없이 유한 Decimal만 허용한다.
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be a Decimal")
    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite")


def _normalize_utc_datetime(value: object, field_name: str) -> datetime:
    """
    함수 이름: _normalize_utc_datetime()
    기능: 시각을 timezone-aware UTC datetime으로 검증하고 canonical UTC로 변환한다.
    인자: value -> 검증할 시각
        field_name -> 오류에 표시할 필드 이름
    반환값: timezone.utc로 정규화한 datetime
    작성 날짜: 2026/08/22
    """
    # naive 또는 UTC가 아닌 시각을 거부한 뒤 canonical timezone identity로 맞춘다.
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must use UTC")

    return value.astimezone(timezone.utc)  # 동일 instant의 UTC 표현만 domain에 남긴다.


def _calculate_product(first_value: Decimal, second_value: Decimal) -> Decimal:
    """
    함수 이름: _calculate_product()
    기능: 프로젝트 Decimal128 정밀도와 half-even 정책으로 두 금융 수치를 곱한다.
    인자: first_value -> 첫 번째 Decimal
        second_value -> 두 번째 Decimal
    반환값: Decimal 곱
    작성 날짜: 2026/08/22
    """
    # 호출자 전역 context와 무관하게 모든 체결 곱셈에 Decimal128 정책을 적용한다.
    with localcontext() as decimal_context:
        decimal_context.prec = _DECIMAL_PRECISION
        decimal_context.rounding = ROUND_HALF_EVEN
        return first_value * second_value


@dataclass(frozen=True, slots=True)
class Fill:
    """
    클래스 이름: Fill
    기능: 한 거래소 주문에서 확인한 실제 체결과 수수료를 불변으로 보존한다.
    작성 날짜: 2026/08/22
    """

    exchange_order_id: str
    trade_id: str
    quantity: Decimal
    price: Decimal
    fee_amount: Decimal
    fee_asset: str
    fee_quote_amount: Decimal
    executed_at: datetime

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: Fill 식별자, Decimal, fee 환산 근거와 UTC 시각을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # Fill dedup key의 두 식별자를 canonical 문자열로 먼저 검증한다.
        _validate_exchange_order_id(self.exchange_order_id, "exchange_order_id")
        _validate_non_empty_text(self.trade_id, "trade_id")

        # 체결 수량·가격은 양수이고 두 fee 표현은 음수가 아니어야 한다.
        for field_name, field_value in (
            ("quantity", self.quantity),
            ("price", self.price),
        ):
            _validate_finite_decimal(field_value, field_name)
            if field_value <= _DECIMAL_ZERO:
                raise ValueError(f"{field_name} must be greater than zero")
        for field_name, field_value in (
            ("fee_amount", self.fee_amount),
            ("fee_quote_amount", self.fee_quote_amount),
        ):
            _validate_finite_decimal(field_value, field_name)
            if field_value < _DECIMAL_ZERO:
                raise ValueError(f"{field_name} must not be negative")

        # 직접 검증 가능한 ETH와 USDT fee는 fill 가격과 일관된 quote 값을 요구한다.
        _validate_fee_asset(self.fee_asset, "fee_asset")
        if self.fee_asset not in ("ETH", "USDT"):
            raise FeeAssetReconciliationRequiredError(self.fee_asset)
        if self.fee_asset == "USDT" and self.fee_quote_amount != self.fee_amount:
            raise ValueError("USDT fee_quote_amount must equal fee_amount")
        if self.fee_asset == "ETH" and self.fee_quote_amount != _calculate_product(
            self.fee_amount,
            self.price,
        ):
            raise ValueError("ETH fee_quote_amount must use the fill price")

        normalized_time = _normalize_utc_datetime(self.executed_at, "executed_at")
        object.__setattr__(self, "executed_at", normalized_time)  # frozen 값에는 canonical UTC만 남긴다.

    @property
    def key(self) -> tuple[str, str]:
        """
        함수 이름: key()
        기능: ADR-002 fill 중복 제거에 사용할 거래소 주문·거래 ID 묶음을 반환한다.
        인자: 없음
        반환값: exchange_order_id와 trade_id tuple
        작성 날짜: 2026/08/22
        """
        return (self.exchange_order_id, self.trade_id)  # 두 ID가 fill 멱등 key를 이룬다.

    @property
    def executed_amount(self) -> Decimal:
        """
        함수 이름: executed_amount()
        기능: 실제 fill 수량과 가격으로 quote 체결 금액을 Decimal 계산한다.
        인자: 없음
        반환값: quantity와 price의 곱
        작성 날짜: 2026/08/22
        """
        return _calculate_product(self.quantity, self.price)  # float 없는 quote 금액을 만든다.


@dataclass(frozen=True, slots=True)
class OrderResult:
    """
    클래스 이름: OrderResult
    기능: fake 또는 실제 Binance Gateway가 반환할 주문 상태와 fill을 불변 정규화한다.
    작성 날짜: 2026/08/22
    """

    symbol: str
    client_order_id: str
    status: OrderStatus
    processed_at: datetime
    exchange_order_id: str | None = None
    fills: tuple[Fill, ...] = ()
    failure_reason: str | None = None
    failure_kind: OrderResultFailureKind | None = None
    retry_after: timedelta | None = None

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: OrderResult 식별자, enum, fill 소속, failure와 UTC 시각을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # Gateway 결과가 다른 상품이나 client intent로 섞이지 않게 식별자를 검증한다.
        _validate_symbol(self.symbol, "symbol")
        _validate_non_empty_text(self.client_order_id, "client_order_id")
        if self.exchange_order_id is not None:
            _validate_exchange_order_id(self.exchange_order_id, "exchange_order_id")
        if not isinstance(self.status, OrderStatus):
            raise TypeError("status must be an OrderStatus")

        # 불변 결과는 tuple Fill만 허용하고 모든 fill이 같은 exchange order를 가리켜야 한다.
        if not isinstance(self.fills, tuple):
            raise TypeError("fills must be a tuple of Fill values")
        if any(not isinstance(fill_value, Fill) for fill_value in self.fills):
            raise TypeError("fills must contain only Fill values")
        if self.fills and self.exchange_order_id is None:
            raise ValueError("fills require exchange_order_id")
        if any(
            fill_value.exchange_order_id != self.exchange_order_id
            for fill_value in self.fills
        ):
            raise ValueError("every fill must belong to exchange_order_id")

        # 실패 설명은 공백 보정으로 다른 결과를 같게 만들지 않고 원문 식별성을 지킨다.
        if self.failure_reason is not None:
            _validate_non_empty_text(self.failure_reason, "failure_reason")
        if self.failure_kind is not None and not isinstance(
            self.failure_kind,
            OrderResultFailureKind,
        ):
            raise TypeError(
                "failure_kind must be an OrderResultFailureKind or None"
            )

        # 두 typed 실패는 fill 없는 정확한 상태 조합에만 붙여 잘못된 zero-fill 확정을 막는다.
        if self.failure_kind is OrderResultFailureKind.SUBMISSION_REJECTED and (
            self.status is not OrderStatus.REJECTED or self.fills
        ):
            raise ValueError(
                "SUBMISSION_REJECTED requires a fill-free REJECTED result"
            )
        if self.failure_kind is OrderResultFailureKind.ORDER_NOT_VISIBLE and (
            self.status is not OrderStatus.UNKNOWN or self.fills
        ):
            raise ValueError(
                "ORDER_NOT_VISIBLE requires a fill-free UNKNOWN result"
            )
        if self.retry_after is not None:
            if not isinstance(self.retry_after, timedelta):
                raise TypeError("retry_after must be a timedelta or None")
            if self.retry_after < timedelta(0):
                raise ValueError("retry_after must be non-negative")
        normalized_time = _normalize_utc_datetime(self.processed_at, "processed_at")
        object.__setattr__(self, "processed_at", normalized_time)  # 외부 offset을 저장하지 않는다.


def _aggregate_fills(
    fills: tuple[Fill, ...],
) -> tuple[Decimal, Decimal, Decimal, Decimal, str, Decimal, datetime]:
    """
    함수 이름: _aggregate_fills()
    기능: 같은 fee asset의 fill을 수량·금액·가중평균·수수료·마지막 시각으로 집계한다.
    인자: fills -> 중복 제거와 주문 소속 검증을 마친 실제 Fill tuple
    반환값: 수량, 금액, 가중평균가, fee, fee asset, quote fee와 마지막 UTC 시각
    작성 날짜: 2026/08/22
    """
    # 실제 체결 근거가 하나도 없으면 0 값으로 요약을 위조하지 않는다.
    if not fills:
        raise ExecutionSummaryUnavailableError("execution summary requires fills")

    # 단일 fee asset durable schema가 서로 다른 자산의 명목 수수료를 더하지 못하게 차단한다.
    fee_assets = frozenset(fill_value.fee_asset for fill_value in fills)
    if len(fee_assets) != 1:
        raise MixedFeeAssetError(fee_assets)
    fee_asset = next(iter(fee_assets))

    # 모든 금융 합계와 나눗셈은 float를 거치지 않고 Decimal128 정책으로 수행한다.
    with localcontext() as decimal_context:
        decimal_context.prec = _DECIMAL_PRECISION
        decimal_context.rounding = ROUND_HALF_EVEN
        executed_quantity = sum(
            (fill_value.quantity for fill_value in fills),
            start=_DECIMAL_ZERO,
        )
        executed_amount = sum(
            (fill_value.executed_amount for fill_value in fills),
            start=_DECIMAL_ZERO,
        )
        average_fill_price = executed_amount / executed_quantity
        fee_amount = sum(
            (fill_value.fee_amount for fill_value in fills),
            start=_DECIMAL_ZERO,
        )
        fee_quote_amount = sum(
            (fill_value.fee_quote_amount for fill_value in fills),
            start=_DECIMAL_ZERO,
        )
    executed_at = max(
        fill_value.executed_at for fill_value in fills
    )  # 요약 시각은 포함된 마지막 실제 체결 시각이다.

    return (
        executed_quantity,
        executed_amount,
        average_fill_price,
        fee_amount,
        fee_asset,
        fee_quote_amount,
        executed_at,
    )


@dataclass(frozen=True, slots=True)
class ExecutionSummary:
    """
    클래스 이름: ExecutionSummary
    기능: 한 주문 또는 새 partial fill subset의 실제 체결 집계를 불변으로 보존한다.
    작성 날짜: 2026/08/22
    """

    exchange_order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    strategy: StrategyType
    regime_type: RegimeType
    exit_reason: ExitReason | None
    requested_quantity: Decimal
    submitted_quantity: Decimal
    executed_quantity: Decimal
    executed_amount: Decimal
    average_fill_price: Decimal
    fee_amount: Decimal
    fee_asset: str
    fee_quote_amount: Decimal
    executed_at: datetime
    fills: tuple[Fill, ...]

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: ExecutionSummary의 주문 identity, aggregate와 immutable fill 근거를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # Position과 Trade가 공유할 주문 identity와 전략 enum을 canonical type으로 제한한다.
        _validate_exchange_order_id(self.exchange_order_id, "exchange_order_id")
        _validate_non_empty_text(self.client_order_id, "client_order_id")
        _validate_symbol(self.symbol, "symbol")
        if not isinstance(self.side, OrderSide):
            raise TypeError("side must be an OrderSide")
        if not isinstance(self.strategy, StrategyType):
            raise TypeError("strategy must be a StrategyType")
        if not isinstance(self.regime_type, RegimeType):
            raise TypeError("regime_type must be a RegimeType")
        if self.exit_reason is not None and not isinstance(self.exit_reason, ExitReason):
            raise TypeError("exit_reason must be an ExitReason or None")

        # 원래 intent와 제출 수량을 분리하되 거래소 제출이 intent를 초과하지 못하게 한다.
        for field_name, field_value in (
            ("requested_quantity", self.requested_quantity),
            ("submitted_quantity", self.submitted_quantity),
            ("executed_quantity", self.executed_quantity),
            ("executed_amount", self.executed_amount),
            ("average_fill_price", self.average_fill_price),
        ):
            _validate_finite_decimal(field_value, field_name)
            if field_value <= _DECIMAL_ZERO:
                raise ValueError(f"{field_name} must be greater than zero")
        if self.submitted_quantity > self.requested_quantity:
            raise ValueError("submitted_quantity must not exceed requested_quantity")
        if self.executed_quantity > self.submitted_quantity:
            raise ValueError("executed_quantity must not exceed submitted_quantity")

        # 수수료 aggregate와 실제 fill tuple이 같은 단일 자산 근거를 가져야 한다.
        for field_name, field_value in (
            ("fee_amount", self.fee_amount),
            ("fee_quote_amount", self.fee_quote_amount),
        ):
            _validate_finite_decimal(field_value, field_name)
            if field_value < _DECIMAL_ZERO:
                raise ValueError(f"{field_name} must not be negative")
        _validate_fee_asset(self.fee_asset, "fee_asset")
        if not isinstance(self.fills, tuple) or not self.fills:
            raise ValueError("fills must be a non-empty tuple")
        if any(not isinstance(fill_value, Fill) for fill_value in self.fills):
            raise TypeError("fills must contain only Fill values")
        if any(
            fill_value.exchange_order_id != self.exchange_order_id
            for fill_value in self.fills
        ):
            raise ValueError("summary fills must belong to exchange_order_id")
        fill_keys = tuple(fill_value.key for fill_value in self.fills)
        if len(set(fill_keys)) != len(fill_keys):
            raise DuplicateFillConflictError(
                "execution summary cannot contain duplicate fill keys"
            )  # 공개 Summary 경계도 Order aggregate와 같은 fill 멱등성을 보장한다.

        # 공개 aggregate가 fill 근거와 달라지는 수동 DTO 생성을 fail closed한다.
        aggregate = _aggregate_fills(self.fills)
        expected_values = (
            self.executed_quantity,
            self.executed_amount,
            self.average_fill_price,
            self.fee_amount,
            self.fee_asset,
            self.fee_quote_amount,
            self.executed_at,
        )
        if aggregate != expected_values:
            raise ValueError("execution aggregate does not match fills")
        normalized_time = _normalize_utc_datetime(self.executed_at, "executed_at")
        object.__setattr__(self, "executed_at", normalized_time)  # summary에도 canonical UTC만 남긴다.

    @property
    def order_id(self) -> str:
        """
        함수 이름: order_id()
        기능: durable Trade의 기존 필드명과 호환되는 거래소 주문 ID alias를 반환한다.
        인자: 없음
        반환값: exchange_order_id
        작성 날짜: 2026/08/22
        """
        return self.exchange_order_id  # 동일 exchange ID를 history의 order_id로 공개한다.


def _resolve_order_status(
    current_status: OrderStatus | None,
    observed_status: OrderStatus,
) -> OrderStatus:
    """
    함수 이름: _resolve_order_status()
    기능: stale active/unknown 결과는 무시하고 concrete 진행과 terminal 충돌만 판정한다.
    인자: current_status -> Order에 이미 보존된 상태 또는 최초이면 None
        observed_status -> 이번 Gateway 결과의 정규화 상태
    반환값: 역행하지 않는 다음 OrderStatus
    작성 날짜: 2026/08/22
    """
    # 최초 관찰, terminal 고정, UNKNOWN 보존과 active 단조 진행을 순서대로 판정한다.
    if current_status is None:
        return observed_status
    if current_status in TERMINAL_ORDER_STATUSES:
        if observed_status in (current_status, OrderStatus.UNKNOWN):
            return current_status
        if observed_status not in TERMINAL_ORDER_STATUSES:
            return current_status  # terminal 뒤 늦게 도착한 active snapshot은 상태를 되돌리지 않는다.
        raise OrderStateTransitionError(
            f"terminal status cannot change from {current_status} to {observed_status}"
        )
    if observed_status is OrderStatus.UNKNOWN:
        return current_status  # timeout 관찰이 이미 확인한 NEW/partial 사실을 지우지 않는다.
    if current_status is OrderStatus.UNKNOWN:
        return observed_status
    if (
        current_status in ACTIVE_ORDER_STATUSES
        and observed_status in ACTIVE_ORDER_STATUSES
        and _ACTIVE_STATUS_PROGRESS[observed_status]
        < _ACTIVE_STATUS_PROGRESS[current_status]
    ):
        return current_status  # 순서가 늦은 active 응답은 이미 확인한 주문 진행을 되돌리지 않는다.

    return observed_status  # 역행 조건이 아니면 더 최근의 구체 상태를 채택한다.


@dataclass(slots=True)
class Order:
    """
    클래스 이름: Order
    기능: 로컬 주문 intent와 중복 제거한 거래소 결과를 mutable aggregate로 보존한다.
    작성 날짜: 2026/08/22
    """

    intent_id: str
    client_order_id: str
    submission_attempt: int
    symbol: str
    side: OrderSide
    strategy: StrategyType
    regime_type: RegimeType
    requested_quantity: Decimal
    submitted_quantity: Decimal
    market_price_at_decision: Decimal
    risk_policy_version: int | None = None
    exit_reason: ExitReason | None = None
    exit_pct_b_at_intent: Decimal | None = None
    exchange_order_id: str | None = field(init=False, default=None)
    status: OrderStatus | None = field(init=False, default=None)
    filled_quantity: Decimal = field(init=False, default=_DECIMAL_ZERO)
    filled_amount: Decimal = field(init=False, default=_DECIMAL_ZERO)
    fill_price: Decimal | None = field(init=False, default=None)
    fee_amount: Decimal = field(init=False, default=_DECIMAL_ZERO)
    fee_asset: str | None = field(init=False, default=None)
    fee_quote_amount: Decimal = field(init=False, default=_DECIMAL_ZERO)
    processed_at: datetime | None = field(init=False, default=None)
    fills: tuple[Fill, ...] = field(init=False, default=())
    failure_reason: str | None = field(init=False, default=None)
    _has_applied_result: bool = field(init=False, default=False, repr=False)
    _fills_by_key: dict[tuple[str, str], Fill] = field(
        init=False,
        default_factory=dict,
        repr=False,
    )
    _position_applied_fill_keys: set[tuple[str, str]] = field(
        init=False,
        default_factory=set,
        repr=False,
    )

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: Order intent ID, 전략, 원래·제출 수량과 결정 가격을 mutation 전에 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # retry 전체가 공유할 intent와 attempt별 client ID를 공백 없는 문자열로 보존한다.
        _validate_non_empty_text(self.intent_id, "intent_id")
        _validate_non_empty_text(self.client_order_id, "client_order_id")
        if (
            isinstance(self.submission_attempt, bool)
            or not isinstance(self.submission_attempt, int)
        ):
            raise TypeError("submission_attempt must be an integer")
        if self.submission_attempt < 0:
            raise ValueError("submission_attempt must not be negative")

        # Order는 현재 Spot 상품과 canonical side/strategy/regime enum만 받는다.
        _validate_symbol(self.symbol, "symbol")
        if not isinstance(self.side, OrderSide):
            raise TypeError("side must be an OrderSide")
        if not isinstance(self.strategy, StrategyType):
            raise TypeError("strategy must be a StrategyType")
        if not isinstance(self.regime_type, RegimeType):
            raise TypeError("regime_type must be a RegimeType")
        if self.exit_reason is not None and not isinstance(self.exit_reason, ExitReason):
            raise TypeError("exit_reason must be an ExitReason or None")
        if self.side is OrderSide.BUY and self.exit_reason is not None:
            raise ValueError("BUY order must not have exit_reason")

        # Case C SELL 판단 %B는 float 변환 없이 유한 Decimal로만 intent에 고정한다.
        if self.exit_pct_b_at_intent is not None:
            _validate_finite_decimal(
                self.exit_pct_b_at_intent,
                "exit_pct_b_at_intent",
            )
            if self.side is not OrderSide.SELL or self.exit_reason is None:
                raise ValueError(
                    "exit_pct_b_at_intent requires a SELL exit reason"
                )
            if self.strategy is not StrategyType.CASE_C:
                raise ValueError(
                    "exit_pct_b_at_intent is supported only for Case C"
                )

        # filter 전 intent와 실제 제출 수량을 모두 양수 Decimal로 보존한다.
        for field_name, field_value in (
            ("requested_quantity", self.requested_quantity),
            ("submitted_quantity", self.submitted_quantity),
            ("market_price_at_decision", self.market_price_at_decision),
        ):
            _validate_finite_decimal(field_value, field_name)
            if field_value <= _DECIMAL_ZERO:
                raise ValueError(f"{field_name} must be greater than zero")
        if self.submitted_quantity > self.requested_quantity:
            raise ValueError("submitted_quantity must not exceed requested_quantity")

        # BUY risk provenance는 bool을 int로 받지 않고 configured policy의 양수 version만 보존한다.
        if self.risk_policy_version is not None:
            if type(self.risk_policy_version) is not int:
                raise TypeError(
                    "risk_policy_version must be an integer or None"
                )
            if self.risk_policy_version < 1:
                raise ValueError(
                    "risk_policy_version must be a positive integer or None"
                )

    @property
    def is_terminal(self) -> bool:
        """
        함수 이름: is_terminal()
        기능: 현재 주문 상태가 더 이상 active하지 않은 terminal인지 판정한다.
        인자: 없음
        반환값: terminal 상태 여부
        작성 날짜: 2026/08/22
        """
        return self.status in TERMINAL_ORDER_STATUSES  # typed terminal 집합만 종료로 판정한다.

    @property
    def order_id(self) -> str | None:
        """
        함수 이름: order_id()
        기능: durable Trade와 ExecutionSummary 명칭에 맞춘 거래소 주문 ID alias를 반환한다.
        인자: 없음
        반환값: exchange_order_id 또는 아직 불명이면 None
        작성 날짜: 2026/08/22
        """
        return self.exchange_order_id  # 제출 전에는 None이고 확인 뒤 canonical ID를 반환한다.

    @property
    def unapplied_fills(self) -> tuple[Fill, ...]:
        """
        함수 이름: unapplied_fills()
        기능: Position에 성공적으로 반영됐다고 표시되지 않은 실제 fill을 순서대로 반환한다.
        인자: 없음
        반환값: 아직 Position에 반영하지 않은 Fill tuple
        작성 날짜: 2026/08/22
        """
        return tuple(
            fill_value
            for fill_value in self.fills
            if fill_value.key not in self._position_applied_fill_keys
        )  # 누적 fill 순서를 보존한 실제 Position delta만 공개한다.

    def apply_order_result(self, result: OrderResult) -> None:
        """
        함수 이름: apply_order_result()
        기능: 최초 Gateway 결과를 주문 identity와 함께 원자 반영한다.
        인자: result -> submit 직후 정규화한 최초 OrderResult
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # 최초 submit 결과는 aggregate 수명 동안 정확히 한 번만 initial 경계로 받는다.
        if self._has_applied_result:
            raise OrderStateTransitionError("initial order result was already applied")

        self._apply_result(result)  # 검증과 aggregate 계산이 모두 끝난 뒤에만 mutable state를 commit한다.

    def reapply_order_result(self, result: OrderResult) -> None:
        """
        함수 이름: reapply_order_result()
        기능: 같은 주문 재조회 결과의 새 fill과 전진 상태만 idempotent하게 반영한다.
        인자: result -> 같은 ID로 query한 후속 OrderResult
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # 최초 결과가 identity를 고정하기 전에는 query 결과를 후속 사실로 적용하지 않는다.
        if not self._has_applied_result:
            raise OrderStateTransitionError("initial order result must be applied first")

        self._apply_result(result)  # stale result도 fill 사실은 dedup한 뒤 상태만 역행시키지 않는다.

    def build_execution_summary(
        self,
        *,
        fills: tuple[Fill, ...] | None = None,
        require_terminal: bool = True,
    ) -> ExecutionSummary:
        """
        함수 이름: build_execution_summary()
        기능: 전체 terminal fill 또는 검증된 active partial subset을 하나의 체결 요약으로 만든다.
        인자: fills -> 집계할 Order 소속 Fill tuple 또는 전체 누적이면 None
            require_terminal -> 기본 terminal 사후조건을 적용할지 여부
        반환값: 실제 fill 근거를 가진 immutable ExecutionSummary
        작성 날짜: 2026/08/22
        """
        # 전체 summary의 terminal 조건과 모든 summary의 exchange identity를 먼저 확인한다.
        if not isinstance(require_terminal, bool):
            raise TypeError("require_terminal must be a bool")
        if require_terminal and not self.is_terminal:
            raise ExecutionSummaryUnavailableError(
                "terminal order status is required for the full execution summary"
            )
        if self.exchange_order_id is None:
            raise ExecutionSummaryUnavailableError(
                "execution summary requires exchange_order_id"
            )

        # 기본 경로는 terminal 주문의 전체 누적 fill이고 partial 경로는 명시 subset만 받는다.
        selected_fills = self.fills if fills is None else self._validate_fill_subset(fills)
        if not selected_fills:
            raise ExecutionSummaryUnavailableError("execution summary requires fills")
        aggregate = _aggregate_fills(selected_fills)

        # Position과 Trade가 같은 normalized DTO를 소비하도록 intent와 aggregate를 함께 묶는다.
        return ExecutionSummary(
            exchange_order_id=self.exchange_order_id,
            client_order_id=self.client_order_id,
            symbol=self.symbol,
            side=self.side,
            strategy=self.strategy,
            regime_type=self.regime_type,
            exit_reason=self.exit_reason,
            requested_quantity=self.requested_quantity,
            submitted_quantity=self.submitted_quantity,
            executed_quantity=aggregate[0],
            executed_amount=aggregate[1],
            average_fill_price=aggregate[2],
            fee_amount=aggregate[3],
            fee_asset=aggregate[4],
            fee_quote_amount=aggregate[5],
            executed_at=aggregate[6],
            fills=selected_fills,
        )

    def mark_fills_applied(self, fills: tuple[Fill, ...]) -> None:
        """
        함수 이름: mark_fills_applied()
        기능: Position mutation이 성공한 Order 소속 fill만 applied key로 멱등 기록한다.
        인자: fills -> Position에 성공적으로 반영한 Fill tuple
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        validated_fills = self._validate_fill_subset(fills)  # 현재 Order 소속 fill만 표시한다.

        # Position 실패 전에는 호출하지 않는 Controller 경계에서 key만 commit한다.
        self._position_applied_fill_keys.update(
            fill_value.key for fill_value in validated_fills
        )  # 같은 fill을 다시 표시해도 applied 상태는 변하지 않는다.

    def _apply_result(self, result: OrderResult) -> None:
        """
        함수 이름: _apply_result()
        기능: 연관성·fill conflict·aggregate·상태 전이를 local 값에서 검증한 뒤 한 번에 commit한다.
        인자: result -> 최초 또는 후속 normalized OrderResult
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # normalized 결과 타입과 현재 Order 상관관계를 fill 계산 전에 검증한다.
        if not isinstance(result, OrderResult):
            raise TypeError("result must be an OrderResult")
        self._validate_result_identity(result)

        # 기존 dict 사본에서 새 fill을 dedup해 검증 실패가 aggregate를 부분 변경하지 않게 한다.
        next_fills_by_key = dict(self._fills_by_key)
        for fill_value in result.fills:
            existing_fill = next_fills_by_key.get(fill_value.key)
            if existing_fill is None:
                next_fills_by_key[fill_value.key] = fill_value
            elif existing_fill != fill_value:
                raise OrderResultConflictError(
                    f"fill key {fill_value.key} has conflicting content"
                )
        next_fills = tuple(next_fills_by_key.values())

        # 상태와 aggregate를 모두 계산하고 terminal/full-fill 불변식까지 확인한다.
        next_status = _resolve_order_status(self.status, result.status)
        if next_fills:
            aggregate = _aggregate_fills(next_fills)
            if aggregate[0] > self.submitted_quantity:
                raise OrderResultConflictError(
                    "executed quantity exceeds submitted_quantity"
                )
        else:
            aggregate = None
        if next_status is OrderStatus.PARTIALLY_FILLED and (
            aggregate is None or aggregate[0] >= self.submitted_quantity
        ):
            raise OrderResultConflictError(
                "PARTIALLY_FILLED requires a positive quantity below submitted_quantity"
            )
        if next_status is OrderStatus.FILLED and (
            aggregate is None or aggregate[0] != self.submitted_quantity
        ):
            raise OrderResultConflictError(
                "FILLED quantity must equal submitted_quantity"
            )

        # 모든 validation을 통과한 뒤에만 mutable Order의 public state를 일괄 교체한다.
        if self.exchange_order_id is None:
            self.exchange_order_id = result.exchange_order_id
        self.status = next_status
        self._fills_by_key = next_fills_by_key
        self.fills = next_fills
        self.processed_at = (
            result.processed_at
            if self.processed_at is None
            else max(self.processed_at, result.processed_at)
        )
        if result.failure_reason is not None and (
            self.failure_reason is None or result.processed_at >= self.processed_at
        ):
            self.failure_reason = result.failure_reason
        self._has_applied_result = True

        # fill이 없으면 초기 zero aggregate를 유지하고 있으면 같은 tuple 계산을 공개한다.
        if aggregate is None:
            return
        self.filled_quantity = aggregate[0]
        self.filled_amount = aggregate[1]
        self.fill_price = aggregate[2]
        self.fee_amount = aggregate[3]
        self.fee_asset = aggregate[4]
        self.fee_quote_amount = aggregate[5]

    def _validate_result_identity(self, result: OrderResult) -> None:
        """
        함수 이름: _validate_result_identity()
        기능: OrderResult가 현재 Order의 symbol, client ID와 exchange ID를 가리키는지 검증한다.
        인자: result -> 연관성을 검증할 normalized OrderResult
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # symbol과 client/exchange ID가 모두 현재 aggregate를 가리켜야 결과를 받을 수 있다.
        if result.symbol != self.symbol:
            raise OrderResultConflictError("result symbol does not match order")
        if result.client_order_id != self.client_order_id:
            raise OrderResultConflictError("result client_order_id does not match order")
        if (
            self.exchange_order_id is not None
            and result.exchange_order_id is not None
            and result.exchange_order_id != self.exchange_order_id
        ):
            raise OrderResultConflictError(
                "result exchange_order_id does not match order"
            )
        if self.exchange_order_id is not None and any(
            fill_value.exchange_order_id != self.exchange_order_id
            for fill_value in result.fills
        ):
            raise OrderResultConflictError("result fill belongs to another order")

    def _validate_fill_subset(self, fills: tuple[Fill, ...]) -> tuple[Fill, ...]:
        """
        함수 이름: _validate_fill_subset()
        기능: partial Position 반영 대상이 중복 없는 현재 Order 소속 fill인지 검증한다.
        인자: fills -> summary 또는 applied 표시 대상 Fill tuple
        반환값: 검증한 원래 Fill tuple
        작성 날짜: 2026/08/22
        """
        # subset은 비어 있지 않은 immutable Fill collection이어야 한다.
        if not isinstance(fills, tuple):
            raise TypeError("fills must be a tuple of Fill values")
        if not fills:
            raise ExecutionSummaryUnavailableError("fill subset must not be empty")
        if any(not isinstance(fill_value, Fill) for fill_value in fills):
            raise TypeError("fills must contain only Fill values")

        # 같은 key를 subset에서 두 번 계산하거나 다른 내용으로 위장하지 못하게 한다.
        selected_keys = tuple(fill_value.key for fill_value in fills)
        if len(set(selected_keys)) != len(selected_keys):
            raise OrderResultConflictError("fill subset contains duplicate keys")
        for fill_value in fills:
            stored_fill = self._fills_by_key.get(fill_value.key)
            if stored_fill is None or stored_fill != fill_value:
                raise OrderResultConflictError(
                    "fill subset must contain exact fills owned by the order"
                )

        return fills


@dataclass(frozen=True, slots=True)
class PendingOrderRecoveryRecord:
    """
    클래스 이름: PendingOrderRecoveryRecord
    기능: 재시작 same-ID 조회에 필요한 Order, lifecycle과 제출 경계 근거를 보존한다.
    작성 날짜: 2026/08/29
    """

    order: Order
    lifecycle: PendingOrderRecoveryLifecycle
    submission_provenance: PendingOrderSubmissionProvenance = (
        PendingOrderSubmissionProvenance.LEGACY_PREPARED_AMBIGUOUS
    )

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: recovery record가 canonical Order, lifecycle과 제출 경계 근거만 포함하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        if not isinstance(self.order, Order):
            raise TypeError("order must be an Order")
        if not isinstance(self.lifecycle, PendingOrderRecoveryLifecycle):
            raise TypeError(
                "lifecycle must be a PendingOrderRecoveryLifecycle"
            )
        if not isinstance(
            self.submission_provenance,
            PendingOrderSubmissionProvenance,
        ):
            raise TypeError(
                "submission_provenance must be a "
                "PendingOrderSubmissionProvenance"
            )
