"""ADR-004 JSONL schema에서 복원되는 불변 Trade 값을 정의한다."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import re

from ..common import RegimeType
from ..trading.order import ExecutionSummary, Order, Fill, _aggregate_fills
from .fill_record import fill_from_record, fill_to_record
from ..trading.states import ExitReason, OrderSide, StrategyType


# 기존 durable row의 회계 의미와 신규 자산 흐름 회계를 version으로 명확히 분리한다.
LEGACY_TRADE_SCHEMA_VERSION = 1
TRADE_SCHEMA_VERSION = 2
SUPPORTED_TRADE_SCHEMA_VERSIONS = frozenset(
    {
        LEGACY_TRADE_SCHEMA_VERSION,
        TRADE_SCHEMA_VERSION,
        3,
    }
)
TRADE_RECORD_TYPE = "trade"
_DECIMAL_PATTERN = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")
_ORDER_ID_PATTERN = re.compile(r"^[1-9][0-9]*$")
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]+$")
_UTC_TIMESTAMP_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?Z$"
)
_RATE_QUANTUM = Decimal("0.00000001")
_BASE_ASSET = "ETH"
_QUOTE_ASSET = "USDT"
_TRADE_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "record_type",
        "trade_id",
        "order_id",
        "client_order_id",
        "symbol",
        "executed_at",
        "side",
        "regime_type",
        "strategy",
        "requested_quantity",
        "executed_quantity",
        "executed_amount",
        "average_fill_price",
        "market_price_at_decision",
        "fee_amount",
        "fee_asset",
        "fee_quote_amount",
        "allocated_cost_basis",
        "realized_pnl",
        "realized_return_rate",
        "exit_reason",
    }
)


class FeeAssetConversionRequiredError(ValueError):
    """
    클래스 이름: FeeAssetConversionRequiredError
    기능: 제3 fee asset을 임의 quote 가격으로 환산하지 못하게 reconciliation을 요구한다.
    작성 날짜: 2026/08/21
    """

    code = "FEE_ASSET_CONVERSION_REQUIRED"

    def __init__(self, fee_asset: str) -> None:
        """
        함수 이름: __init__()
        기능: quote 환산 근거가 필요한 fee asset을 안전한 typed failure로 보존한다.
        인자: fee_asset -> ETH 또는 USDT가 아닌 원래 수수료 asset
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 임의 환율이나 raw payload 없이 reconciliation 대상 자산 코드만 보존한다.
        self.fee_asset = fee_asset
        super().__init__(
            f"fee asset {fee_asset} requires quote conversion reconciliation"
        )


@dataclass(frozen=True, slots=True)
class RealizedResult:
    """
    클래스 이름: RealizedResult
    기능: 한 SELL execution의 배분 원가, fee 포함 실현손익과 수익률을 보존한다.
    작성 날짜: 2026/08/22
    """

    allocated_cost_basis: Decimal
    realized_pnl: Decimal
    realized_return_rate: Decimal

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 실현 결과가 유한 Decimal이고 원가와 수익률 표현이 유효한지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # 계산 결과 세 필드가 float 없이 유한 Decimal인지 먼저 검증한다.
        result_fields = (
            ("allocated_cost_basis", self.allocated_cost_basis),
            ("realized_pnl", self.realized_pnl),
            ("realized_return_rate", self.realized_return_rate),
        )
        for field_name, field_value in result_fields:
            _validate_finite_decimal(field_value, field_name)

        # SELL 수익률의 분모는 양수이고 wire 비율은 정확히 8자리여야 한다.
        if self.allocated_cost_basis <= Decimal("0"):
            raise ValueError("allocated_cost_basis must be greater than zero")
        if self.realized_return_rate.as_tuple().exponent != -8:
            raise ValueError(
                "realized_return_rate must have exactly 8 decimal places"
            )


def _validate_non_empty_text(value: object, field_name: str) -> None:
    """
    함수 이름: _validate_non_empty_text()
    기능: 식별자와 코드 문자열이 앞뒤 공백 없는 비어 있지 않은 문자열인지 검증한다.
    인자: value -> 검증할 값
        field_name -> 오류 메시지에 표시할 필드 이름
    반환값: 없음
    작성 날짜: 2026/08/21
    """
    # 식별자와 enum wire 값에는 암묵적 문자열 변환이나 trim을 적용하지 않는다.
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value or value != value.strip():
        raise ValueError(f"{field_name} must be non-empty without outer whitespace")


def _validate_finite_decimal(value: object, field_name: str) -> None:
    """
    함수 이름: _validate_finite_decimal()
    기능: 금융 수치가 float가 아닌 유한 Decimal인지 검증한다.
    인자: value -> 검증할 금융 수치
        field_name -> 오류 메시지에 표시할 필드 이름
    반환값: 없음
    작성 날짜: 2026/08/21
    """
    # float와 NaN·Infinity가 durable 금융 수치로 들어오지 못하게 순서대로 검사한다.
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be a Decimal")
    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite")


def _normalize_utc_datetime(value: object, field_name: str) -> datetime:
    """
    함수 이름: _normalize_utc_datetime()
    기능: 시각이 timezone-aware UTC datetime인지 검증하고 canonical UTC로 변환한다.
    인자: value -> 검증할 시각
        field_name -> 오류 메시지에 표시할 필드 이름
    반환값: timezone.utc로 정규화한 datetime
    작성 날짜: 2026/08/21
    """
    # naive 시각과 UTC 외 offset을 모두 거부해 JSONL의 Z 표현과 identity를 맞춘다.
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must use UTC")

    return value.astimezone(timezone.utc)  # 동등한 UTC 표현도 canonical timezone으로 통일한다.


def _parse_plain_decimal(value: object, field_name: str) -> Decimal:
    """
    함수 이름: _parse_plain_decimal()
    기능: JSON string 금융 수치를 exponent와 locale 구분자 없는 Decimal로 해석한다.
    인자: value -> JSON record에서 읽은 값
        field_name -> 오류 메시지에 표시할 필드 이름
    반환값: 검증된 유한 Decimal
    작성 날짜: 2026/08/21
    """
    # exponent·선행 0·locale separator를 허용하지 않는 plain wire grammar를 먼저 확인한다.
    if not isinstance(value, str) or _DECIMAL_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a plain decimal string")

    try:
        decimal_value = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{field_name} must be a valid decimal string") from error

    if not decimal_value.is_finite():
        raise ValueError(f"{field_name} must be finite")

    return decimal_value  # 입력 문자열의 Decimal scale을 보존한 채 반환한다.


def _parse_optional_plain_decimal(
    value: object,
    field_name: str,
) -> Decimal | None:
    """
    함수 이름: _parse_optional_plain_decimal()
    기능: nullable JSON Decimal string을 None 또는 유한 Decimal로 해석한다.
    인자: value -> JSON record에서 읽은 nullable 값
        field_name -> 오류 메시지에 표시할 필드 이름
    반환값: 검증된 Decimal 또는 None
    작성 날짜: 2026/08/21
    """
    if value is None:
        return None  # BUY realized nullable field의 JSON null을 그대로 보존한다.

    return _parse_plain_decimal(value, field_name)


def _parse_utc_timestamp(value: object, field_name: str) -> datetime:
    """
    함수 이름: _parse_utc_timestamp()
    기능: JSON timestamp를 RFC 3339 UTC Z 형식의 datetime으로 해석한다.
    인자: value -> JSON record에서 읽은 timestamp
        field_name -> 오류 메시지에 표시할 필드 이름
    반환값: timezone.utc datetime
    작성 날짜: 2026/08/21
    """
    # parser의 관대한 ISO 지원 전에 RFC 3339 UTC Z wire 형식을 정확히 제한한다.
    if not isinstance(value, str) or _UTC_TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be an RFC 3339 UTC Z timestamp")

    try:
        parsed_value = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as error:
        raise ValueError(f"{field_name} must be a valid UTC timestamp") from error

    return _normalize_utc_datetime(
        parsed_value,
        field_name,
    )  # calendar validity와 timezone identity를 마지막으로 검증한다.


def _parse_required_text(record: Mapping[str, object], field_name: str) -> str:
    """
    함수 이름: _parse_required_text()
    기능: JSON record의 필수 문자열 필드를 읽고 공백 불변식을 검증한다.
    인자: record -> schema 검증을 마친 JSON object
        field_name -> 읽을 필드 이름
    반환값: 검증된 문자열
    작성 날짜: 2026/08/21
    """
    # exact schema 검증이 끝난 mapping에서 필수 값을 읽고 text 불변식을 재사용한다.
    field_value = record[field_name]
    _validate_non_empty_text(field_value, field_name)

    return field_value  # validator 통과 뒤 type narrowing된 canonical 문자열이다.


def _parse_optional_exit_reason(value: object) -> ExitReason | None:
    """
    함수 이름: _parse_optional_exit_reason()
    기능: nullable exit_reason wire 값을 canonical ExitReason으로 변환한다.
    인자: value -> JSON record의 nullable exit_reason 값
    반환값: canonical ExitReason 또는 None
    작성 날짜: 2026/08/21
    """
    if value is None:
        return None  # BUY record의 nullable exit reason을 보존한다.
    if not isinstance(value, str):
        raise TypeError("exit_reason must be a string or null")

    try:
        return ExitReason(value)
    except ValueError as error:
        raise ValueError("exit_reason is not canonical") from error


@dataclass(frozen=True, slots=True)
class Trade:
    """
    클래스 이름: Trade
    기능: 한 완료 주문의 실제 체결과 fee 포함 실현 결과를 불변으로 보존한다.
    작성 날짜: 2026/08/22
    """

    trade_id: str
    order_id: str
    client_order_id: str
    symbol: str
    executed_at: datetime
    side: OrderSide
    regime_type: RegimeType
    strategy: StrategyType
    requested_quantity: Decimal
    executed_quantity: Decimal
    executed_amount: Decimal
    average_fill_price: Decimal
    market_price_at_decision: Decimal
    fee_amount: Decimal
    fee_asset: str
    fee_quote_amount: Decimal
    allocated_cost_basis: Decimal | None = None
    realized_pnl: Decimal | None = None
    realized_return_rate: Decimal | None = None
    exit_reason: ExitReason | None = None
    schema_version: int = TRADE_SCHEMA_VERSION
    fee_fills: tuple[Fill, ...] = ()

    @classmethod
    def from_order_execution(
        cls,
        order: Order,
        summary: ExecutionSummary,
        realized_result: RealizedResult | None = None,
    ) -> "Trade":
        """
        함수 이름: from_order_execution()
        기능: 로컬 주문 의도와 terminal 누적 fill 및 선택적 SELL 실현 결과로 Trade를 만든다.
        인자: order -> 요청 정보와 전략 의미를 보존한 terminal Order
            summary -> 같은 주문의 누적 fill을 집계한 ExecutionSummary
            realized_result -> SELL인 경우 계산을 마친 RealizedResult
        반환값: 현재 JSONL schema 불변식을 만족하는 Trade
        작성 날짜: 2026/08/22
        """
        # factory 입력은 canonical mutable Order와 immutable aggregate summary로 제한한다.
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")
        if not isinstance(summary, ExecutionSummary):
            raise TypeError("summary must be an ExecutionSummary")
        if realized_result is not None and not isinstance(
            realized_result,
            RealizedResult,
        ):
            raise TypeError("realized_result must be a RealizedResult or None")

        # 주문 의도와 execution이 같은 거래소 주문을 가리키는지 모두 확인한다.
        if not order.is_terminal:
            raise ValueError("Trade requires a terminal Order")
        if order.exchange_order_id is None:
            raise ValueError("order must contain an exchange_order_id")
        if summary.exchange_order_id != order.exchange_order_id:
            raise ValueError("summary exchange_order_id does not match order")
        if summary.client_order_id != order.client_order_id:
            raise ValueError("summary client_order_id does not match order")
        if summary.symbol != order.symbol or summary.side is not order.side:
            raise ValueError("summary symbol and side must match order")
        if summary.strategy is not order.strategy:
            raise ValueError("summary strategy does not match order")
        if summary.regime_type is not order.regime_type:
            raise ValueError("summary regime_type does not match order")
        if summary.exit_reason is not order.exit_reason:
            raise ValueError("summary exit_reason does not match order")
        if summary.requested_quantity != order.requested_quantity:
            raise ValueError("summary requested_quantity does not match order")
        if summary.submitted_quantity != order.submitted_quantity:
            raise ValueError("summary submitted_quantity does not match order")
        if summary.fills != order.fills:
            raise ValueError("Trade requires the terminal Order cumulative fills")

        # exchange order ID를 사용해 저장 재시도에도 동일한 trade ID를 재생성한다.
        order_id = str(order.exchange_order_id)
        trade_id = f"trade-{order_id}"  # 한 terminal order에 하나의 안정된 ID를 부여한다.
        # BUY는 세 realized field를 null로, SELL은 계산된 결과 세 값을 함께 복사한다.
        allocated_cost_basis = None
        realized_pnl = None
        realized_return_rate = None
        if realized_result is not None:
            allocated_cost_basis = realized_result.allocated_cost_basis
            realized_pnl = realized_result.realized_pnl
            realized_return_rate = realized_result.realized_return_rate

        return cls(
            trade_id=trade_id,
            order_id=order_id,
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            executed_at=summary.executed_at,
            side=order.side,
            regime_type=order.regime_type,
            strategy=order.strategy,
            requested_quantity=order.requested_quantity,
            executed_quantity=summary.executed_quantity,
            executed_amount=summary.executed_amount,
            average_fill_price=summary.average_fill_price,
            market_price_at_decision=order.market_price_at_decision,
            fee_amount=summary.fee_amount,
            fee_asset=summary.fee_asset,
            fee_quote_amount=summary.fee_quote_amount,
            allocated_cost_basis=allocated_cost_basis,
            realized_pnl=realized_pnl,
            realized_return_rate=realized_return_rate,
            exit_reason=order.exit_reason,
            schema_version=3 if any(fill.fee_asset == "BNB" for fill in summary.fills) else TRADE_SCHEMA_VERSION,
            fee_fills=summary.fills if any(fill.fee_asset == "BNB" for fill in summary.fills) else (),
        )


    @property
    def base_fee_amount(self) -> Decimal:
        """
        함수 이름: base_fee_amount()
        기능: 혼합 수수료에서도 실제 ETH 차감과 추가 취득 비용을 구분한다.
        인자: 없음
        반환값: 해당 자산 흐름의 Decimal 합계
        작성 날짜: 2026/09/09
        """
        # 단일 자산 기존 row는 원래 의미를 보존하고 v3는 fill별 원자산을 사용한다.
        if not self.fee_fills:
            return self.fee_amount if self.fee_asset == "ETH" else Decimal("0")
        with localcontext() as context:
            context.prec = 34
            context.rounding = ROUND_HALF_EVEN
            return sum((fill.fee_amount for fill in self.fee_fills if fill.fee_asset == "ETH"), Decimal("0"))

    @property
    def non_base_fee_quote_amount(self) -> Decimal:
        """
        함수 이름: non_base_fee_quote_amount()
        기능: 혼합 수수료에서도 실제 ETH 차감과 추가 취득 비용을 구분한다.
        인자: 없음
        반환값: 해당 자산 흐름의 Decimal 합계
        작성 날짜: 2026/09/09
        """
        # 단일 자산 기존 row는 원래 의미를 보존하고 v3는 fill별 원자산을 사용한다.
        if not self.fee_fills:
            return self.fee_quote_amount if self.fee_asset != "ETH" else Decimal("0")
        with localcontext() as context:
            context.prec = 34
            context.rounding = ROUND_HALF_EVEN
            return sum((fill.fee_quote_amount for fill in self.fee_fills if fill.fee_asset != "ETH"), Decimal("0"))

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: Trade 식별자, enum, UTC 시각, 금융 수치와 BUY/SELL schema를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # bool을 정수로 받지 않고 지원하는 회계 schema version만 durable Trade에 허용한다.
        if (
            type(self.schema_version) is not int
            or self.schema_version not in SUPPORTED_TRADE_SCHEMA_VERSIONS
        ):
            raise ValueError("schema_version must be supported integer 1 or 2")

        # durable identity 세 필드와 exchange order의 양의 정수 wire 형식을 먼저 검증한다.
        _validate_non_empty_text(self.trade_id, "trade_id")
        _validate_non_empty_text(self.order_id, "order_id")
        _validate_non_empty_text(self.client_order_id, "client_order_id")
        if _ORDER_ID_PATTERN.fullmatch(self.order_id) is None:
            raise ValueError("order_id must be a positive integer string")

        # symbol은 대문자 영숫자 형식뿐 아니라 현재 지원 상품 ETHUSDT로 제한한다.
        _validate_non_empty_text(self.symbol, "symbol")
        if _SYMBOL_PATTERN.fullmatch(self.symbol) is None:
            raise ValueError("symbol must contain only uppercase ASCII letters and digits")
        if self.symbol != "ETHUSDT":
            raise ValueError("Trade supports only ETHUSDT")

        # frozen Trade 내부에도 UTC canonical datetime만 남기도록 검증 뒤 교체한다.
        normalized_executed_at = _normalize_utc_datetime(
            self.executed_at,
            "executed_at",
        )
        object.__setattr__(self, "executed_at", normalized_executed_at)

        # 문자열 enum 대체값이 identity 기반 도메인 분기를 우회하지 못하게 한다.
        if not isinstance(self.side, OrderSide):
            raise TypeError("side must be the canonical OrderSide")
        if not isinstance(self.regime_type, RegimeType):
            raise TypeError("regime_type must be the canonical RegimeType")
        if not isinstance(self.strategy, StrategyType):
            raise TypeError("strategy must be the canonical StrategyType")

        # 주문·체결·가격 필드는 모두 양수인 유한 Decimal이어야 한다.
        positive_fields = (
            ("requested_quantity", self.requested_quantity),
            ("executed_quantity", self.executed_quantity),
            ("executed_amount", self.executed_amount),
            ("average_fill_price", self.average_fill_price),
            ("market_price_at_decision", self.market_price_at_decision),
        )
        for field_name, field_value in positive_fields:
            _validate_finite_decimal(field_value, field_name)
            if field_value <= Decimal("0"):
                raise ValueError(f"{field_name} must be greater than zero")

        # 원자산과 quote 환산 수수료는 0을 허용하되 음수는 거부한다.
        fee_fields = (
            ("fee_amount", self.fee_amount),
            ("fee_quote_amount", self.fee_quote_amount),
        )
        for field_name, field_value in fee_fields:
            _validate_finite_decimal(field_value, field_name)
            if field_value < Decimal("0"):
                raise ValueError(f"{field_name} must not be negative")

        # fee asset 코드 형식을 확인한 뒤 자산별 환산 정책과 realized schema를 검증한다.
        _validate_non_empty_text(self.fee_asset, "fee_asset")
        if _SYMBOL_PATTERN.fullmatch(self.fee_asset) is None:
            raise ValueError("fee_asset must contain only uppercase ASCII letters and digits")

        # v3는 원 fill과 평가 근거를 함께 저장하고 aggregate를 재계산해 변조를 거부한다.
        if not isinstance(self.fee_fills, tuple):
            raise TypeError("fee_fills must be a tuple")
        if self.schema_version == 3:
            if not self.fee_fills or any(not isinstance(fill, Fill) or fill.exchange_order_id != self.order_id for fill in self.fee_fills):
                raise ValueError("v3 requires order-bound fee fills")
            if len({fill.key for fill in self.fee_fills}) != len(self.fee_fills):
                raise ValueError("duplicate fee fill evidence")
            aggregate = _aggregate_fills(self.fee_fills)
            if aggregate != (self.executed_quantity, self.executed_amount, self.average_fill_price, self.fee_amount, self.fee_asset, self.fee_quote_amount, self.executed_at):
                raise ValueError("v3 aggregate does not match fee evidence")
        elif self.fee_fills:
            raise ValueError("legacy schema cannot contain fee evidence")
        self._validate_fee_conversion()

        self._validate_realized_fields()

    def _validate_fee_conversion(self) -> None:
        """
        함수 이름: _validate_fee_conversion()
        기능: USDT fee 동일값, ETH aggregate의 0 일관성과 제3 asset 정책을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        if self.schema_version == 3:
            return  # 개별 Fill과 aggregate의 정확한 일치를 위에서 검증했다.
        # USDT fee는 별도 환율 없이 원 수수료와 quote 수수료가 정확히 같아야 한다.
        if self.fee_asset == _QUOTE_ASSET:
            if self.fee_quote_amount != self.fee_amount:
                raise ValueError(
                    "fee_quote_amount does not match the USDT fee amount"
                )
            return
        # ETH fee aggregate는 per-fill 환산 근거를 쓰므로 두 zero 표현의 일관성을 요구한다.
        elif self.fee_asset == _BASE_ASSET:
            if (self.fee_amount == Decimal("0")) != (
                self.fee_quote_amount == Decimal("0")
            ):
                raise ValueError(
                    "ETH fee amount and per-fill quote aggregate must "
                    "be zero together"
                )
            return

        raise FeeAssetConversionRequiredError(
            self.fee_asset
        )  # 제3 자산은 임의 가격 없이 typed reconciliation으로 보낸다.

    def _validate_realized_fields(self) -> None:
        """
        함수 이름: _validate_realized_fields()
        기능: BUY nullable 필드와 SELL fee 포함 실현 결과의 일관성을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # side에 따라 네 nullable field가 모두 없거나 모두 존재하는지 한 묶음으로 검사한다.
        realized_fields = (
            self.allocated_cost_basis,
            self.realized_pnl,
            self.realized_return_rate,
            self.exit_reason,
        )
        if self.side is OrderSide.BUY:
            if any(field_value is not None for field_value in realized_fields):
                raise ValueError("BUY realized and exit fields must all be null")
            return  # BUY는 realized 원가·손익·수익률·청산 사유를 기록하지 않는다.

        if any(field_value is None for field_value in realized_fields):
            raise ValueError("SELL realized and exit fields must all be present")

        # None 제거 뒤 세 금융 field의 canonical Decimal 타입과 표현을 재검증한다.
        allocated_cost_basis = self.allocated_cost_basis
        realized_pnl = self.realized_pnl
        realized_return_rate = self.realized_return_rate
        if (
            not isinstance(allocated_cost_basis, Decimal)
            or not isinstance(realized_pnl, Decimal)
            or not isinstance(realized_return_rate, Decimal)
        ):
            raise TypeError("SELL realized fields must be Decimals")
        _validate_finite_decimal(allocated_cost_basis, "allocated_cost_basis")
        _validate_finite_decimal(realized_pnl, "realized_pnl")
        _validate_finite_decimal(realized_return_rate, "realized_return_rate")
        if allocated_cost_basis <= Decimal("0"):
            raise ValueError("allocated_cost_basis must be greater than zero")
        if realized_return_rate.as_tuple().exponent != -8:
            raise ValueError("realized_return_rate must have exactly 8 decimal places")
        if not isinstance(self.exit_reason, ExitReason):
            raise TypeError("exit_reason must be the canonical ExitReason")

        # 실현손익과 수익률 검증도 생성 계산과 동일한 Decimal128 context 안에서 수행한다.
        with localcontext() as decimal_context:
            decimal_context.prec = 34
            decimal_context.rounding = ROUND_HALF_EVEN
            expected_realized_pnl = (
                self.executed_amount
                - self.fee_quote_amount
                - allocated_cost_basis
            )
            expected_return_rate = (
                realized_pnl / allocated_cost_basis * Decimal("100")
            ).quantize(_RATE_QUANTUM, rounding=ROUND_HALF_EVEN)
        if realized_pnl != expected_realized_pnl:
            raise ValueError("realized_pnl does not match net sell proceeds")
        if realized_return_rate != expected_return_rate:
            raise ValueError("realized_return_rate does not match realized_pnl")


def trade_from_json_object(record: object) -> Trade:
    """
    함수 이름: trade_from_json_object()
    기능: ADR-004 JSONL v1 또는 v2 object의 exact schema와 값을 검증해 Trade로 변환한다.
    인자: record -> JSON decoder가 반환한 단일 record
    반환값: 검증된 불변 Trade
    작성 날짜: 2026/08/21
    """
    # domain 생성 전에 object shape와 허용 key 집합을 exact match로 고정한다.
    if not isinstance(record, Mapping):
        raise TypeError("trade record must be a JSON object")
    expected_fields = _TRADE_RECORD_FIELDS | {"fee_fills"} if record.get("schema_version") == 3 else _TRADE_RECORD_FIELDS
    if set(record.keys()) != expected_fields:
        raise ValueError("trade record must contain exactly the JSONL fields")

    # bool을 integer version으로 오인하지 않고 지원하는 회계 version인지 확인한다.
    schema_version = record["schema_version"]
    if (
        type(schema_version) is not int
        or schema_version not in SUPPORTED_TRADE_SCHEMA_VERSIONS
    ):
        raise ValueError("schema_version must be supported integer 1 or 2")
    if record["record_type"] != TRADE_RECORD_TYPE:
        raise ValueError("record_type must be trade")

    # exchange order ID는 문자열 identity와 선행 0 없는 양의 정수 형식을 모두 지킨다.
    order_id = _parse_required_text(record, "order_id")
    if _ORDER_ID_PATTERN.fullmatch(order_id) is None:
        raise ValueError("order_id must be a positive integer string")

    # wire enum 문자열은 fallback 없이 현재 canonical enum member로 변환한다.
    try:
        side = OrderSide(_parse_required_text(record, "side"))
        regime_type = RegimeType(_parse_required_text(record, "regime_type"))
        strategy = StrategyType(_parse_required_text(record, "strategy"))
    except ValueError as error:
        raise ValueError("trade record contains a non-canonical enum value") from error

    return Trade(  # scalar parser 결과도 Trade 생성자의 전체 도메인 불변식을 다시 거친다.
        trade_id=_parse_required_text(record, "trade_id"),
        order_id=order_id,
        client_order_id=_parse_required_text(record, "client_order_id"),
        symbol=_parse_required_text(record, "symbol"),
        executed_at=_parse_utc_timestamp(record["executed_at"], "executed_at"),
        side=side,
        regime_type=regime_type,
        strategy=strategy,
        requested_quantity=_parse_plain_decimal(
            record["requested_quantity"],
            "requested_quantity",
        ),
        executed_quantity=_parse_plain_decimal(
            record["executed_quantity"],
            "executed_quantity",
        ),
        executed_amount=_parse_plain_decimal(
            record["executed_amount"],
            "executed_amount",
        ),
        average_fill_price=_parse_plain_decimal(
            record["average_fill_price"],
            "average_fill_price",
        ),
        market_price_at_decision=_parse_plain_decimal(
            record["market_price_at_decision"],
            "market_price_at_decision",
        ),
        fee_amount=_parse_plain_decimal(record["fee_amount"], "fee_amount"),
        fee_asset=_parse_required_text(record, "fee_asset"),
        fee_quote_amount=_parse_plain_decimal(
            record["fee_quote_amount"],
            "fee_quote_amount",
        ),
        allocated_cost_basis=_parse_optional_plain_decimal(
            record["allocated_cost_basis"],
            "allocated_cost_basis",
        ),
        realized_pnl=_parse_optional_plain_decimal(
            record["realized_pnl"],
            "realized_pnl",
        ),
        realized_return_rate=_parse_optional_plain_decimal(
            record["realized_return_rate"],
            "realized_return_rate",
        ),
        exit_reason=_parse_optional_exit_reason(record["exit_reason"]),
        schema_version=schema_version,
        fee_fills=tuple(fill_from_record(fill) for fill in _read_fee_fills(record)) if schema_version == 3 else (),
    )


def trade_to_json_object(trade: Trade) -> dict[str, object]:
    """
    함수 이름: trade_to_json_object()
    기능: Trade를 ADR-004 key 순서와 plain Decimal string을 가진 versioned JSONL object로 만든다.
    인자: trade -> 직렬화할 canonical Trade
    반환값: JSON encoder에 전달할 JSON-compatible dictionary
    작성 날짜: 2026/08/22
    """
    # 구조가 비슷한 객체의 임의 직렬화를 막고 검증 완료 Trade만 받는다.
    if not isinstance(trade, Trade):
        raise TypeError("trade must be a Trade")

    def decimal_text(value: Decimal | None) -> str | None:
        """
        함수 이름: decimal_text()
        기능: optional Decimal을 exponent 없는 canonical string 또는 null로 변환한다.
        인자: value -> 직렬화할 Decimal 또는 None
        반환값: plain Decimal string 또는 None
        작성 날짜: 2026/08/22
        """
        return (
            None if value is None else format(value, "f")
        )  # exponent 없이 원래 Decimal scale을 plain string으로 보존한다.

    # UTC timestamp와 enum을 wire 값으로 바꾸고 schema의 canonical key 순서를 유지한다.
    executed_at_text = trade.executed_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return {
        **({"fee_fills": [fill_to_record(fill) for fill in trade.fee_fills]} if trade.schema_version == 3 else {}),
        "schema_version": trade.schema_version,
        "record_type": TRADE_RECORD_TYPE,
        "trade_id": trade.trade_id,
        "order_id": trade.order_id,
        "client_order_id": trade.client_order_id,
        "symbol": trade.symbol,
        "executed_at": executed_at_text,
        "side": trade.side.value,
        "regime_type": trade.regime_type.value,
        "strategy": trade.strategy.value,
        "requested_quantity": decimal_text(trade.requested_quantity),
        "executed_quantity": decimal_text(trade.executed_quantity),
        "executed_amount": decimal_text(trade.executed_amount),
        "average_fill_price": decimal_text(trade.average_fill_price),
        "market_price_at_decision": decimal_text(
            trade.market_price_at_decision
        ),
        "fee_amount": decimal_text(trade.fee_amount),
        "fee_asset": trade.fee_asset,
        "fee_quote_amount": decimal_text(trade.fee_quote_amount),
        "allocated_cost_basis": decimal_text(trade.allocated_cost_basis),
        "realized_pnl": decimal_text(trade.realized_pnl),
        "realized_return_rate": decimal_text(trade.realized_return_rate),
        "exit_reason": (
            None if trade.exit_reason is None else trade.exit_reason.value
        ),
    }


def _read_fee_fills(record: Mapping) -> list:
    """
    함수 이름: _read_fee_fills()
    기능: v3 증거 배열의 shape와 최대 크기를 제한한다.
    인자: record -> 검증 중인 Trade record
    반환값: 실제 fill record 목록
    작성 날짜: 2026/09/09
    """
    fills = record["fee_fills"]
    if not isinstance(fills, list) or not 1 <= len(fills) <= 1000:
        raise ValueError("v3 requires 1..1000 fee fills")
    return fills  # 상세 필드는 전용 decoder가 검증한다.
