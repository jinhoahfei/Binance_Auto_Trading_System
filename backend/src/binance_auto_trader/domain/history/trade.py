"""ADR-004 JSONL schema에서 복원되는 불변 Trade 값을 정의한다."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import re

from ..common import RegimeType
from ..trading.states import ExitReason, OrderSide, StrategyType


TRADE_SCHEMA_VERSION = 1
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
        self.fee_asset = fee_asset
        super().__init__(
            f"fee asset {fee_asset} requires quote conversion reconciliation"
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
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must use UTC")

    return value.astimezone(timezone.utc)


def _parse_plain_decimal(value: object, field_name: str) -> Decimal:
    """
    함수 이름: _parse_plain_decimal()
    기능: JSON string 금융 수치를 exponent와 locale 구분자 없는 Decimal로 해석한다.
    인자: value -> JSON record에서 읽은 값
        field_name -> 오류 메시지에 표시할 필드 이름
    반환값: 검증된 유한 Decimal
    작성 날짜: 2026/08/21
    """
    if not isinstance(value, str) or _DECIMAL_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a plain decimal string")

    try:
        decimal_value = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{field_name} must be a valid decimal string") from error

    if not decimal_value.is_finite():
        raise ValueError(f"{field_name} must be finite")

    return decimal_value


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
        return None

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
    if not isinstance(value, str) or _UTC_TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be an RFC 3339 UTC Z timestamp")

    try:
        parsed_value = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as error:
        raise ValueError(f"{field_name} must be a valid UTC timestamp") from error

    return _normalize_utc_datetime(parsed_value, field_name)


def _parse_required_text(record: Mapping[str, object], field_name: str) -> str:
    """
    함수 이름: _parse_required_text()
    기능: JSON record의 필수 문자열 필드를 읽고 공백 불변식을 검증한다.
    인자: record -> schema 검증을 마친 JSON object
        field_name -> 읽을 필드 이름
    반환값: 검증된 문자열
    작성 날짜: 2026/08/21
    """
    field_value = record[field_name]
    _validate_non_empty_text(field_value, field_name)

    return field_value


def _parse_optional_exit_reason(value: object) -> ExitReason | None:
    """
    함수 이름: _parse_optional_exit_reason()
    기능: nullable exit_reason wire 값을 canonical ExitReason으로 변환한다.
    인자: value -> JSON record의 nullable exit_reason 값
    반환값: canonical ExitReason 또는 None
    작성 날짜: 2026/08/21
    """
    if value is None:
        return None
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
    작성 날짜: 2026/08/21
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

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: Trade 식별자, enum, UTC 시각, 금융 수치와 BUY/SELL schema를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        _validate_non_empty_text(self.trade_id, "trade_id")
        _validate_non_empty_text(self.order_id, "order_id")
        _validate_non_empty_text(self.client_order_id, "client_order_id")
        if _ORDER_ID_PATTERN.fullmatch(self.order_id) is None:
            raise ValueError("order_id must be a positive integer string")

        _validate_non_empty_text(self.symbol, "symbol")
        if _SYMBOL_PATTERN.fullmatch(self.symbol) is None:
            raise ValueError("symbol must contain only uppercase ASCII letters and digits")
        if self.symbol != "ETHUSDT":
            raise ValueError("Trade supports only ETHUSDT")

        normalized_executed_at = _normalize_utc_datetime(
            self.executed_at,
            "executed_at",
        )
        object.__setattr__(self, "executed_at", normalized_executed_at)

        if not isinstance(self.side, OrderSide):
            raise TypeError("side must be the canonical OrderSide")
        if not isinstance(self.regime_type, RegimeType):
            raise TypeError("regime_type must be the canonical RegimeType")
        if not isinstance(self.strategy, StrategyType):
            raise TypeError("strategy must be the canonical StrategyType")

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

        fee_fields = (
            ("fee_amount", self.fee_amount),
            ("fee_quote_amount", self.fee_quote_amount),
        )
        for field_name, field_value in fee_fields:
            _validate_finite_decimal(field_value, field_name)
            if field_value < Decimal("0"):
                raise ValueError(f"{field_name} must not be negative")

        _validate_non_empty_text(self.fee_asset, "fee_asset")
        if _SYMBOL_PATTERN.fullmatch(self.fee_asset) is None:
            raise ValueError("fee_asset must contain only uppercase ASCII letters and digits")

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
        if self.fee_asset == _QUOTE_ASSET:
            if self.fee_quote_amount != self.fee_amount:
                raise ValueError(
                    "fee_quote_amount does not match the USDT fee amount"
                )
            return
        elif self.fee_asset == _BASE_ASSET:
            if (self.fee_amount == Decimal("0")) != (
                self.fee_quote_amount == Decimal("0")
            ):
                raise ValueError(
                    "ETH fee amount and per-fill quote aggregate must "
                    "be zero together"
                )
            return

        raise FeeAssetConversionRequiredError(self.fee_asset)

    def _validate_realized_fields(self) -> None:
        """
        함수 이름: _validate_realized_fields()
        기능: BUY nullable 필드와 SELL fee 포함 실현 결과의 일관성을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        realized_fields = (
            self.allocated_cost_basis,
            self.realized_pnl,
            self.realized_return_rate,
            self.exit_reason,
        )
        if self.side is OrderSide.BUY:
            if any(field_value is not None for field_value in realized_fields):
                raise ValueError("BUY realized and exit fields must all be null")
            return

        if any(field_value is None for field_value in realized_fields):
            raise ValueError("SELL realized and exit fields must all be present")

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

        expected_realized_pnl = (
            self.executed_amount
            - self.fee_quote_amount
            - allocated_cost_basis
        )
        if realized_pnl != expected_realized_pnl:
            raise ValueError("realized_pnl does not match net sell proceeds")

        with localcontext() as decimal_context:
            decimal_context.prec = 34
            decimal_context.rounding = ROUND_HALF_EVEN
            expected_return_rate = (
                realized_pnl / allocated_cost_basis * Decimal("100")
            ).quantize(_RATE_QUANTUM, rounding=ROUND_HALF_EVEN)
        if realized_return_rate != expected_return_rate:
            raise ValueError("realized_return_rate does not match realized_pnl")


def trade_from_json_object(record: object) -> Trade:
    """
    함수 이름: trade_from_json_object()
    기능: ADR-004 JSONL v1 object의 exact schema와 값을 검증해 Trade로 변환한다.
    인자: record -> JSON decoder가 반환한 단일 record
    반환값: 검증된 불변 Trade
    작성 날짜: 2026/08/21
    """
    if not isinstance(record, Mapping):
        raise TypeError("trade record must be a JSON object")
    if set(record.keys()) != _TRADE_RECORD_FIELDS:
        raise ValueError("trade record must contain exactly the JSONL v1 fields")

    schema_version = record["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != TRADE_SCHEMA_VERSION
    ):
        raise ValueError("schema_version must be integer 1")
    if record["record_type"] != TRADE_RECORD_TYPE:
        raise ValueError("record_type must be trade")

    order_id = _parse_required_text(record, "order_id")
    if _ORDER_ID_PATTERN.fullmatch(order_id) is None:
        raise ValueError("order_id must be a positive integer string")

    try:
        side = OrderSide(_parse_required_text(record, "side"))
        regime_type = RegimeType(_parse_required_text(record, "regime_type"))
        strategy = StrategyType(_parse_required_text(record, "strategy"))
    except ValueError as error:
        raise ValueError("trade record contains a non-canonical enum value") from error

    return Trade(
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
    )
