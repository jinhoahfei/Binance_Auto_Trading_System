"""Binance Spot REST payload를 symbol rule과 주문 domain 값으로 정규화한다."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, localcontext
from math import gcd

from binance_auto_trader.domain.trading.order import (
    Fill,
    Order,
    OrderResult,
    OrderStatus,
)


_DECIMAL_ZERO = Decimal("0")
_DECIMAL_ONE = Decimal("1")
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ORDER_STATUS_BY_BINANCE_VALUE = {
    "PENDING_NEW": OrderStatus.PENDING_NEW,
    "NEW": OrderStatus.NEW,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "PENDING_CANCEL": OrderStatus.PENDING_CANCEL,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELED,
    "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.EXPIRED,
    "EXPIRED_IN_MATCH": OrderStatus.EXPIRED_IN_MATCH,
}


class BinancePayloadError(ValueError):
    """
    클래스 이름: BinancePayloadError
    기능: 공식 Binance payload가 필요한 필드나 일관성을 만족하지 못한 오류를 나타낸다.
    작성 날짜: 2026/08/22
    """


class SymbolFilterError(ValueError):
    """
    클래스 이름: SymbolFilterError
    기능: 주문이 현재 symbol 상태 또는 수량·notional filter를 통과하지 못했음을 나타낸다.
    작성 날짜: 2026/08/22
    """


@dataclass(frozen=True, slots=True)
class QuantityFilter:
    """
    클래스 이름: QuantityFilter
    기능: LOT_SIZE 또는 MARKET_LOT_SIZE의 수량 하한·상한·간격을 보존한다.
    작성 날짜: 2026/08/22
    """

    filter_type: str
    minimum_quantity: Decimal
    maximum_quantity: Decimal
    step_size: Decimal

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 공식 quantity filter 값이 유한한 음이 아닌 Decimal인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # Filter 종류는 두 공식 quantity filter 중 하나여야 한다.
        if self.filter_type not in {"LOT_SIZE", "MARKET_LOT_SIZE"}:
            raise ValueError("unsupported quantity filter type")

        # 0은 거래소가 해당 규칙을 비활성화한 값으로 허용하되 음수는 차단한다.
        for field_name, field_value in (
            ("minimum_quantity", self.minimum_quantity),
            ("maximum_quantity", self.maximum_quantity),
            ("step_size", self.step_size),
        ):
            _validate_non_negative_decimal(field_value, field_name)

        # 활성화된 상한은 하한보다 작을 수 없다.
        if (
            self.maximum_quantity > _DECIMAL_ZERO
            and self.maximum_quantity < self.minimum_quantity
        ):
            raise ValueError("maximum_quantity must not be below minimum_quantity")


@dataclass(frozen=True, slots=True)
class NotionalFilter:
    """
    클래스 이름: NotionalFilter
    기능: MIN_NOTIONAL 또는 NOTIONAL의 시장가 적용 범위와 금액 한계를 보존한다.
    작성 날짜: 2026/08/22
    """

    filter_type: str
    minimum_notional: Decimal | None
    maximum_notional: Decimal | None
    apply_minimum_to_market: bool
    apply_maximum_to_market: bool
    average_price_minutes: int

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: notional filter 종류, 금액 범위와 평균 가격 기간을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # Filter 종류에 따라 허용되는 하한·상한 구조를 구분한다.
        if self.filter_type not in {"MIN_NOTIONAL", "NOTIONAL"}:
            raise ValueError("unsupported notional filter type")
        if self.minimum_notional is not None:
            _validate_non_negative_decimal(
                self.minimum_notional,
                "minimum_notional",
            )
        if self.maximum_notional is not None:
            _validate_non_negative_decimal(
                self.maximum_notional,
                "maximum_notional",
            )

        # NOTIONAL의 활성 상한은 하한보다 작을 수 없다.
        if (
            self.minimum_notional is not None
            and self.maximum_notional is not None
            and self.maximum_notional < self.minimum_notional
        ):
            raise ValueError("maximum_notional must not be below minimum_notional")
        if (
            isinstance(self.average_price_minutes, bool)
            or not isinstance(self.average_price_minutes, int)
            or self.average_price_minutes < 0
        ):
            raise ValueError("average_price_minutes must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class SymbolTradingRules:
    """
    클래스 이름: SymbolTradingRules
    기능: exchangeInfo에서 주문 전 검증에 필요한 Spot MARKET 규칙만 불변으로 보존한다.
    작성 날짜: 2026/08/22
    """

    symbol: str
    status: str
    base_asset: str
    quote_asset: str
    base_asset_precision: int
    order_types: frozenset[str]
    is_spot_trading_allowed: bool
    lot_size: QuantityFilter
    market_lot_size: QuantityFilter
    notional_filters: tuple[NotionalFilter, ...]

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: symbol rule의 식별자, precision과 filter collection을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # 식별자는 Binance canonical 대문자 문자열로만 보존한다.
        for field_name, field_value in (
            ("symbol", self.symbol),
            ("status", self.status),
            ("base_asset", self.base_asset),
            ("quote_asset", self.quote_asset),
        ):
            _validate_canonical_text(field_value, field_name)

        # Base precision은 음이 아닌 정수이고 주문 유형 집합은 비어 있지 않아야 한다.
        if (
            isinstance(self.base_asset_precision, bool)
            or not isinstance(self.base_asset_precision, int)
            or self.base_asset_precision < 0
        ):
            raise ValueError("base_asset_precision must be a non-negative integer")
        if not isinstance(self.order_types, frozenset) or not self.order_types:
            raise ValueError("order_types must be a non-empty frozenset")
        if any(
            not isinstance(order_type, str) or not order_type
            for order_type in self.order_types
        ):
            raise ValueError("order_types must contain non-empty strings")

        # 고정 필터 종류가 바뀌어 섞이지 않도록 dataclass 생성 시점에 확인한다.
        if self.lot_size.filter_type != "LOT_SIZE":
            raise ValueError("lot_size must be a LOT_SIZE filter")
        if self.market_lot_size.filter_type != "MARKET_LOT_SIZE":
            raise ValueError("market_lot_size must be a MARKET_LOT_SIZE filter")
        if not isinstance(self.notional_filters, tuple):
            raise TypeError("notional_filters must be a tuple")


def _validate_non_negative_decimal(value: object, field_name: str) -> None:
    """
    함수 이름: _validate_non_negative_decimal()
    기능: 값이 유한하고 음이 아닌 Decimal인지 검증한다.
    인자: value -> 검증할 값
        field_name -> 오류에 사용할 필드 이름
    반환값: 없음
    작성 날짜: 2026/08/22
    """
    # Float 암시 변환을 허용하지 않아 filter 계산의 십진 정밀도를 지킨다.
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be a Decimal")
    if not value.is_finite() or value < _DECIMAL_ZERO:
        raise ValueError(f"{field_name} must be a finite non-negative Decimal")


def _validate_positive_decimal(value: object, field_name: str) -> None:
    """
    함수 이름: _validate_positive_decimal()
    기능: 값이 유한하고 양수인 Decimal인지 검증한다.
    인자: value -> 검증할 값
        field_name -> 오류에 사용할 필드 이름
    반환값: 없음
    작성 날짜: 2026/08/22
    """
    # 주문 수량과 가격은 0이거나 음수일 수 없다.
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be a Decimal")
    if not value.is_finite() or value <= _DECIMAL_ZERO:
        raise ValueError(f"{field_name} must be a finite positive Decimal")


def _validate_canonical_text(value: object, field_name: str) -> str:
    """
    함수 이름: _validate_canonical_text()
    기능: 공백 없는 비어 있지 않은 문자열을 검증해 그대로 반환한다.
    인자: value -> 검증할 문자열 후보
        field_name -> 오류에 사용할 필드 이름
    반환값: 검증된 문자열
    작성 날짜: 2026/08/22
    """
    # 거래소 식별자의 앞뒤 공백을 자동 보정하지 않고 schema 위반으로 거부한다.
    if not isinstance(value, str):
        raise BinancePayloadError(f"{field_name} must be a string")
    if not value or value != value.strip():
        raise BinancePayloadError(f"{field_name} must be non-empty and trimmed")

    return value  # 원문 식별자를 손실 없이 보존한다.


def _read_decimal_string(value: object, field_name: str) -> Decimal:
    """
    함수 이름: _read_decimal_string()
    기능: 공식 JSON decimal 문자열을 유한한 Decimal로 변환한다.
    인자: value -> 변환할 JSON 값
        field_name -> 오류에 사용할 필드 이름
    반환값: 유한한 Decimal
    작성 날짜: 2026/08/22
    """
    # Binance 금액 계약은 문자열이므로 JSON number와 float는 경계에서 거부한다.
    if not isinstance(value, str):
        raise BinancePayloadError(f"{field_name} must be a decimal string")
    try:
        parsed_value = Decimal(value)
    except InvalidOperation as error:
        raise BinancePayloadError(
            f"{field_name} must be a valid decimal string"
        ) from error
    if not parsed_value.is_finite():
        raise BinancePayloadError(f"{field_name} must be finite")

    return parsed_value  # float를 거치지 않은 십진 값을 반환한다.


def _read_non_negative_integer(value: object, field_name: str) -> int:
    """
    함수 이름: _read_non_negative_integer()
    기능: bool을 제외한 음이 아닌 JSON 정수를 검증해 반환한다.
    인자: value -> 검증할 JSON 값
        field_name -> 오류에 사용할 필드 이름
    반환값: 음이 아닌 정수
    작성 날짜: 2026/08/22
    """
    # bool은 int의 하위 타입이므로 명시적으로 배제한다.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BinancePayloadError(f"{field_name} must be a non-negative integer")

    return value  # 검증된 timestamp와 precision 정수만 외부로 전달한다.


def _read_boolean(value: object, field_name: str) -> bool:
    """
    함수 이름: _read_boolean()
    기능: 공식 JSON boolean 값을 다른 truthy 값과 구분해 반환한다.
    인자: value -> 검증할 JSON 값
        field_name -> 오류에 사용할 필드 이름
    반환값: 검증된 bool
    작성 날짜: 2026/08/22
    """
    # 문자열 true와 숫자 1은 계약상 boolean이 아니므로 수용하지 않는다.
    if not isinstance(value, bool):
        raise BinancePayloadError(f"{field_name} must be a boolean")

    return value  # JSON 원래 boolean 의미를 유지한다.


def _parse_quantity_filter(
    payload: Mapping[object, object],
    expected_type: str,
) -> QuantityFilter:
    """
    함수 이름: _parse_quantity_filter()
    기능: exchangeInfo의 quantity filter 하나를 typed rule로 변환한다.
    인자: payload -> filter JSON object
        expected_type -> LOT_SIZE 또는 MARKET_LOT_SIZE
    반환값: 검증된 QuantityFilter
    작성 날짜: 2026/08/22
    """
    # Filter type과 세 decimal 필드를 함께 읽어 서로 다른 필터 혼입을 차단한다.
    filter_type = _validate_canonical_text(payload.get("filterType"), "filterType")
    if filter_type != expected_type:
        raise BinancePayloadError(f"expected {expected_type} filter")

    return QuantityFilter(
        filter_type=filter_type,
        minimum_quantity=_read_decimal_string(payload.get("minQty"), "minQty"),
        maximum_quantity=_read_decimal_string(payload.get("maxQty"), "maxQty"),
        step_size=_read_decimal_string(payload.get("stepSize"), "stepSize"),
    )  # 필드별 Decimal 검증은 불변 filter 생성 시 다시 수행한다.


def _parse_notional_filter(
    payload: Mapping[object, object],
) -> NotionalFilter:
    """
    함수 이름: _parse_notional_filter()
    기능: MIN_NOTIONAL 또는 NOTIONAL JSON을 시장가 적용 규칙으로 변환한다.
    인자: payload -> filter JSON object
    반환값: 검증된 NotionalFilter
    작성 날짜: 2026/08/22
    """
    filter_type = _validate_canonical_text(payload.get("filterType"), "filterType")

    # 두 공식 filter는 시장가 적용 flag와 상한 필드 이름이 서로 다르다.
    if filter_type == "MIN_NOTIONAL":
        return NotionalFilter(
            filter_type=filter_type,
            minimum_notional=_read_decimal_string(
                payload.get("minNotional"),
                "minNotional",
            ),
            maximum_notional=None,
            apply_minimum_to_market=_read_boolean(
                payload.get("applyToMarket"),
                "applyToMarket",
            ),
            apply_maximum_to_market=False,
            average_price_minutes=_read_non_negative_integer(
                payload.get("avgPriceMins"),
                "avgPriceMins",
            ),
        )
    if filter_type == "NOTIONAL":
        return NotionalFilter(
            filter_type=filter_type,
            minimum_notional=_read_decimal_string(
                payload.get("minNotional"),
                "minNotional",
            ),
            maximum_notional=_read_decimal_string(
                payload.get("maxNotional"),
                "maxNotional",
            ),
            apply_minimum_to_market=_read_boolean(
                payload.get("applyMinToMarket"),
                "applyMinToMarket",
            ),
            apply_maximum_to_market=_read_boolean(
                payload.get("applyMaxToMarket"),
                "applyMaxToMarket",
            ),
            average_price_minutes=_read_non_negative_integer(
                payload.get("avgPriceMins"),
                "avgPriceMins",
            ),
        )

    raise BinancePayloadError("unsupported notional filter type")


def parse_symbol_trading_rules(
    payload: object,
    symbol: str,
) -> SymbolTradingRules:
    """
    함수 이름: parse_symbol_trading_rules()
    기능: exchangeInfo 응답에서 지정 symbol의 MARKET 주문 필수 규칙을 추출한다.
    인자: payload -> 공식 GET /api/v3/exchangeInfo JSON
        symbol -> 찾을 canonical Spot symbol
    반환값: 주문 전 검증에 사용할 SymbolTradingRules
    작성 날짜: 2026/08/22
    """
    expected_symbol = _validate_canonical_text(symbol, "symbol")
    if not isinstance(payload, Mapping):
        raise BinancePayloadError("exchangeInfo payload must be an object")

    # Symbol 배열에서 요청한 식별자와 정확히 일치하는 단일 항목만 선택한다.
    raw_symbols = payload.get("symbols")
    if not isinstance(raw_symbols, (list, tuple)):
        raise BinancePayloadError("exchangeInfo symbols must be an array")
    matching_symbols = tuple(
        raw_symbol
        for raw_symbol in raw_symbols
        if isinstance(raw_symbol, Mapping)
        and raw_symbol.get("symbol") == expected_symbol
    )
    if len(matching_symbols) != 1:
        raise BinancePayloadError("exchangeInfo must contain exactly one symbol match")
    symbol_payload = matching_symbols[0]

    # 주문 가능 상태, 자산, precision과 지원 주문 유형을 fail-closed 방식으로 읽는다.
    status = _validate_canonical_text(symbol_payload.get("status"), "status")
    base_asset = _validate_canonical_text(
        symbol_payload.get("baseAsset"),
        "baseAsset",
    )
    quote_asset = _validate_canonical_text(
        symbol_payload.get("quoteAsset"),
        "quoteAsset",
    )
    base_asset_precision = _read_non_negative_integer(
        symbol_payload.get("baseAssetPrecision"),
        "baseAssetPrecision",
    )
    is_spot_trading_allowed = _read_boolean(
        symbol_payload.get("isSpotTradingAllowed"),
        "isSpotTradingAllowed",
    )
    raw_order_types = symbol_payload.get("orderTypes")
    if not isinstance(raw_order_types, (list, tuple)) or not raw_order_types:
        raise BinancePayloadError("orderTypes must be a non-empty array")
    order_types = frozenset(
        _validate_canonical_text(order_type, "orderTypes item")
        for order_type in raw_order_types
    )

    # Filter 배열은 종류별 mapping으로 바꾸되 동일 종류 중복을 schema 오류로 처리한다.
    raw_filters = symbol_payload.get("filters")
    if not isinstance(raw_filters, (list, tuple)):
        raise BinancePayloadError("symbol filters must be an array")
    filters_by_type: dict[str, Mapping[object, object]] = {}
    for raw_filter in raw_filters:
        if not isinstance(raw_filter, Mapping):
            raise BinancePayloadError("each symbol filter must be an object")
        filter_type = _validate_canonical_text(
            raw_filter.get("filterType"),
            "filterType",
        )
        if filter_type in filters_by_type:
            raise BinancePayloadError("symbol filters must not contain duplicates")
        filters_by_type[filter_type] = raw_filter

    # MARKET 제출에는 일반 lot와 market 전용 lot를 모두 검사해야 한다.
    lot_size_payload = filters_by_type.get("LOT_SIZE")
    market_lot_size_payload = filters_by_type.get("MARKET_LOT_SIZE")
    if lot_size_payload is None or market_lot_size_payload is None:
        raise BinancePayloadError("LOT_SIZE and MARKET_LOT_SIZE are required")

    # 두 notional filter는 동시에 존재할 수 있으므로 존재하는 규칙을 모두 보존한다.
    notional_filters = tuple(
        _parse_notional_filter(filters_by_type[filter_type])
        for filter_type in ("MIN_NOTIONAL", "NOTIONAL")
        if filter_type in filters_by_type
    )
    if not notional_filters:
        raise BinancePayloadError("a notional filter is required")

    return SymbolTradingRules(
        symbol=expected_symbol,
        status=status,
        base_asset=base_asset,
        quote_asset=quote_asset,
        base_asset_precision=base_asset_precision,
        order_types=order_types,
        is_spot_trading_allowed=is_spot_trading_allowed,
        lot_size=_parse_quantity_filter(lot_size_payload, "LOT_SIZE"),
        market_lot_size=_parse_quantity_filter(
            market_lot_size_payload,
            "MARKET_LOT_SIZE",
        ),
        notional_filters=notional_filters,
    )  # quotePrecision은 v4 제거 예정이므로 거래 간격 계산에 사용하지 않는다.


def _least_common_decimal_step(step_sizes: Sequence[Decimal]) -> Decimal:
    """
    함수 이름: _least_common_decimal_step()
    기능: 여러 유한소수 step을 모두 만족하는 가장 작은 공통 수량 간격을 계산한다.
    인자: step_sizes -> 0보다 큰 Decimal step sequence
    반환값: 모든 입력 step의 정수배인 최소 Decimal 간격
    작성 날짜: 2026/08/22
    """
    if not step_sizes:
        raise ValueError("step_sizes must not be empty")
    for step_size in step_sizes:
        _validate_positive_decimal(step_size, "step_size")

    # 모든 소수를 같은 10진 scale의 정수로 옮겨 정수 최소공배수를 계산한다.
    common_exponent = min(step_size.as_tuple().exponent for step_size in step_sizes)
    scaling_factor = Decimal(10) ** (-common_exponent)
    scaled_steps = tuple(int(step_size * scaling_factor) for step_size in step_sizes)
    common_scaled_step = scaled_steps[0]
    for scaled_step in scaled_steps[1:]:
        common_scaled_step = (
            common_scaled_step * scaled_step
        ) // gcd(common_scaled_step, scaled_step)

    return Decimal(common_scaled_step) / scaling_factor  # 원래 10진 scale로 되돌린다.


def floor_market_quantity(
    quantity: Decimal,
    rules: SymbolTradingRules,
) -> Decimal:
    """
    함수 이름: floor_market_quantity()
    기능: 요청 수량을 precision·LOT_SIZE·MARKET_LOT_SIZE 공통 간격으로 내림한다.
    인자: quantity -> filter 전 제출 희망 수량
        rules -> 현재 exchangeInfo symbol 규칙
    반환값: 모든 활성 quantity 간격을 만족하는 내림 수량
    작성 날짜: 2026/08/22
    """
    _validate_positive_decimal(quantity, "quantity")
    if not isinstance(rules, SymbolTradingRules):
        raise TypeError("rules must be SymbolTradingRules")

    # Precision quantum과 0이 아닌 두 lot step을 한 공통 grid로 결합한다.
    precision_step = _DECIMAL_ONE.scaleb(-rules.base_asset_precision)
    active_steps = [precision_step]
    for quantity_filter in (rules.lot_size, rules.market_lot_size):
        if quantity_filter.step_size > _DECIMAL_ZERO:
            active_steps.append(quantity_filter.step_size)
    common_step = _least_common_decimal_step(tuple(active_steps))

    # ROUND_DOWN은 양수에서 거래소 허용 간격을 넘지 않도록 항상 0 방향으로 내린다.
    with localcontext() as decimal_context:
        decimal_context.prec = max(34, len(quantity.as_tuple().digits) + 18)
        step_count = (quantity / common_step).to_integral_value(
            rounding=ROUND_DOWN,
        )
        floored_quantity = step_count * common_step
    if floored_quantity <= _DECIMAL_ZERO:
        raise SymbolFilterError("FILTER_MARKET_QUANTITY_ZERO")

    # 각 필터의 활성 하한·상한과 modulo 조건을 독립적으로 재검증한다.
    for quantity_filter in (rules.lot_size, rules.market_lot_size):
        if (
            quantity_filter.minimum_quantity > _DECIMAL_ZERO
            and floored_quantity < quantity_filter.minimum_quantity
        ):
            raise SymbolFilterError(
                f"FILTER_{quantity_filter.filter_type}_MINIMUM"
            )
        if (
            quantity_filter.maximum_quantity > _DECIMAL_ZERO
            and floored_quantity > quantity_filter.maximum_quantity
        ):
            raise SymbolFilterError(
                f"FILTER_{quantity_filter.filter_type}_MAXIMUM"
            )
        if (
            quantity_filter.step_size > _DECIMAL_ZERO
            and floored_quantity % quantity_filter.step_size != _DECIMAL_ZERO
        ):
            raise SymbolFilterError(
                f"FILTER_{quantity_filter.filter_type}_STEP"
            )

    return floored_quantity  # 요청 수량보다 커지지 않은 제출 수량만 반환한다.


def validate_market_notional(
    quantity: Decimal,
    market_price: Decimal,
    rules: SymbolTradingRules,
) -> None:
    """
    함수 이름: validate_market_notional()
    기능: 현지 결정 가격으로 시장가 주문의 활성 MIN_NOTIONAL·NOTIONAL 한계를 사전 점검한다.
    인자: quantity -> quantity filter를 적용한 제출 수량
        market_price -> 주문 결정 시점의 양수 quote 가격
        rules -> 현재 exchangeInfo symbol 규칙
    반환값: 없음
    작성 날짜: 2026/08/22
    """
    _validate_positive_decimal(quantity, "quantity")
    _validate_positive_decimal(market_price, "market_price")
    if not isinstance(rules, SymbolTradingRules):
        raise TypeError("rules must be SymbolTradingRules")

    # 로컬 사전 점검은 결정 가격을 사용하며 거래소의 reference/average 가격 판정이 최종 권위다.
    with localcontext() as decimal_context:
        decimal_context.prec = 34
        notional = quantity * market_price

    # 동시에 제공될 수 있는 MIN_NOTIONAL과 NOTIONAL의 시장가 적용 flag를 모두 따른다.
    for notional_filter in rules.notional_filters:
        if (
            notional_filter.apply_minimum_to_market
            and notional_filter.minimum_notional is not None
            and notional < notional_filter.minimum_notional
        ):
            raise SymbolFilterError(
                f"FILTER_{notional_filter.filter_type}_MINIMUM"
            )
        if (
            notional_filter.apply_maximum_to_market
            and notional_filter.maximum_notional is not None
            and notional > notional_filter.maximum_notional
        ):
            raise SymbolFilterError(
                f"FILTER_{notional_filter.filter_type}_MAXIMUM"
            )


def prepare_market_order(
    order: Order,
    rules: SymbolTradingRules,
) -> Order:
    """
    함수 이름: prepare_market_order()
    기능: 현재 symbol 상태를 확인하고 같은 Order의 submitted_quantity만 안전하게 내림 보정한다.
    인자: order -> 원래 요청 수량과 제출 희망 수량을 보존한 Order
        rules -> 현재 exchangeInfo symbol 규칙
    반환값: submitted_quantity를 보정한 동일 Order
    작성 날짜: 2026/08/22
    """
    if not isinstance(order, Order):
        raise TypeError("order must be an Order")
    if not isinstance(rules, SymbolTradingRules):
        raise TypeError("rules must be SymbolTradingRules")

    # CANCEL_ONLY를 포함한 비거래 상태와 MARKET·Spot 미지원 symbol은 신규 제출을 막는다.
    if order.symbol != rules.symbol:
        raise SymbolFilterError("FILTER_SYMBOL_MISMATCH")
    if rules.status != "TRADING":
        raise SymbolFilterError(f"FILTER_SYMBOL_STATUS_{rules.status}")
    if "MARKET" not in rules.order_types:
        raise SymbolFilterError("FILTER_MARKET_ORDER_UNSUPPORTED")
    if not rules.is_spot_trading_allowed:
        raise SymbolFilterError("FILTER_SPOT_TRADING_DISABLED")

    # 기존 submitted_quantity를 내린 뒤 notional을 검사해 requested intent를 변경하지 않는다.
    prepared_quantity = floor_market_quantity(order.submitted_quantity, rules)
    validate_market_notional(
        prepared_quantity,
        order.market_price_at_decision,
        rules,
    )
    if prepared_quantity > order.requested_quantity:
        raise SymbolFilterError("FILTER_PREPARED_QUANTITY_EXCEEDS_REQUESTED")
    order.submitted_quantity = prepared_quantity  # mutable aggregate에 실제 제출량만 기록한다.

    return order  # Controller가 소유한 동일 identity를 유지한다.


def format_decimal_parameter(value: Decimal) -> str:
    """
    함수 이름: format_decimal_parameter()
    기능: 유한 Decimal을 지수 표기 없는 Binance request 문자열로 직렬화한다.
    인자: value -> 직렬화할 Decimal
    반환값: float를 거치지 않은 고정소수점 문자열
    작성 날짜: 2026/08/22
    """
    _validate_non_negative_decimal(value, "value")
    serialized_value = format(value, "f")

    return serialized_value  # 서명 payload와 HTTP body가 같은 문자열을 공유한다.


def milliseconds_to_utc(value: object, field_name: str) -> datetime:
    """
    함수 이름: milliseconds_to_utc()
    기능: 음이 아닌 epoch milliseconds를 timezone-aware UTC datetime으로 변환한다.
    인자: value -> 변환할 epoch millisecond 값
        field_name -> 오류에 사용할 필드 이름
    반환값: UTC datetime
    작성 날짜: 2026/08/22
    """
    milliseconds = _read_non_negative_integer(value, field_name)

    return _UNIX_EPOCH + timedelta(milliseconds=milliseconds)  # float timestamp 오차를 피한다.


def _read_order_processed_at(
    payload: Mapping[object, object],
    fallback_processed_at: datetime,
) -> datetime:
    """
    함수 이름: _read_order_processed_at()
    기능: 주문 응답의 가장 구체적인 처리 시각을 읽고 없으면 주입 UTC 시각을 사용한다.
    인자: payload -> 공식 order response object
        fallback_processed_at -> timestamp 필드가 없을 때 사용할 UTC 시각
    반환값: 정규화된 처리 시각
    작성 날짜: 2026/08/22
    """
    if (
        not isinstance(fallback_processed_at, datetime)
        or fallback_processed_at.tzinfo is None
        or fallback_processed_at.utcoffset() is None
    ):
        raise ValueError("fallback_processed_at must be timezone-aware")

    # Matching Engine transactTime을 우선하고 query의 updateTime·time 순으로 대체한다.
    for field_name in ("transactTime", "updateTime", "time"):
        if field_name in payload:
            return milliseconds_to_utc(payload[field_name], field_name)

    return fallback_processed_at.astimezone(timezone.utc)  # 외부 offset은 UTC로 통일한다.


def map_fill_payloads(
    payloads: object,
    *,
    symbol: str,
    exchange_order_id: str,
    base_asset: str,
    quote_asset: str,
    fallback_executed_at: datetime,
) -> tuple[Fill, ...]:
    """
    함수 이름: map_fill_payloads()
    기능: FULL order fills 또는 myTrades 행을 멱등 Fill tuple로 정규화한다.
    인자: payloads -> 공식 fill/trade JSON array
        symbol -> fill이 속한 Spot symbol
        exchange_order_id -> fill이 속한 거래소 order ID
        base_asset -> 수수료 직접 환산을 허용할 base asset
        quote_asset -> 수수료 직접 환산을 허용할 quote asset
        fallback_executed_at -> FULL fill에 time이 없을 때 사용할 처리 시각
    반환값: 거래소 order ID와 trade ID로 중복 제거한 Fill tuple
    작성 날짜: 2026/08/22
    """
    _validate_canonical_text(symbol, "symbol")
    _validate_canonical_text(exchange_order_id, "exchange_order_id")
    _validate_canonical_text(base_asset, "base_asset")
    _validate_canonical_text(quote_asset, "quote_asset")
    if not isinstance(payloads, (list, tuple)):
        raise BinancePayloadError("fill payloads must be an array")
    if (
        not isinstance(fallback_executed_at, datetime)
        or fallback_executed_at.tzinfo is None
        or fallback_executed_at.utcoffset() is None
    ):
        raise ValueError("fallback_executed_at must be timezone-aware")

    fills_by_key: dict[tuple[str, str], Fill] = {}
    for payload in payloads:
        if not isinstance(payload, Mapping):
            raise BinancePayloadError("each fill payload must be an object")

        # FULL 응답은 tradeId를, myTrades는 id를 사용하므로 두 공식 schema를 구분한다.
        trade_id_value = payload.get("tradeId", payload.get("id"))
        if isinstance(trade_id_value, bool) or not isinstance(trade_id_value, int):
            raise BinancePayloadError("fill trade id must be an integer")
        trade_id = str(trade_id_value)
        payload_order_id = payload.get("orderId")
        if payload_order_id is not None and str(payload_order_id) != exchange_order_id:
            raise BinancePayloadError("fill belongs to a different order")

        # 가격·수량·commission은 공식 decimal 문자열에서만 Decimal로 변환한다.
        price = _read_decimal_string(payload.get("price"), "fill.price")
        quantity = _read_decimal_string(payload.get("qty"), "fill.qty")
        fee_amount = _read_decimal_string(
            payload.get("commission"),
            "fill.commission",
        )
        fee_asset = _validate_canonical_text(
            payload.get("commissionAsset"),
            "fill.commissionAsset",
        )

        # Project domain이 직접 검증할 수 있는 base/quote fee만 결정론적으로 환산한다.
        if fee_asset == quote_asset:
            fee_quote_amount = fee_amount
        elif fee_asset == base_asset:
            with localcontext() as decimal_context:
                decimal_context.prec = 34
                fee_quote_amount = fee_amount * price
        else:
            fee_quote_amount = _DECIMAL_ZERO

        # myTrades의 time을 우선하고 FULL 응답은 주문 transactTime을 사용한다.
        executed_at = (
            milliseconds_to_utc(payload["time"], "fill.time")
            if "time" in payload
            else fallback_executed_at.astimezone(timezone.utc)
        )
        fill_value = Fill(
            exchange_order_id=exchange_order_id,
            trade_id=trade_id,
            quantity=quantity,
            price=price,
            fee_amount=fee_amount,
            fee_asset=fee_asset,
            fee_quote_amount=fee_quote_amount,
            executed_at=executed_at,
        )
        existing_fill = fills_by_key.get(fill_value.key)
        if existing_fill is not None and existing_fill != fill_value:
            raise BinancePayloadError("duplicate fill key has conflicting content")
        fills_by_key[fill_value.key] = fill_value  # 동일 재전송은 마지막 동일 값 하나로 축약한다.

    # 시간과 숫자 trade ID 순서를 고정해 query·stream 도착 순서 차이를 제거한다.
    return tuple(
        sorted(
            fills_by_key.values(),
            key=lambda fill_value: (
                fill_value.executed_at,
                int(fill_value.trade_id),
            ),
        )
    )


def map_order_result(
    payload: object,
    *,
    expected_symbol: str,
    expected_client_order_id: str,
    fills: tuple[Fill, ...],
    fallback_processed_at: datetime,
    failure_reason: str | None = None,
    retry_after: timedelta | None = None,
) -> OrderResult:
    """
    함수 이름: map_order_result()
    기능: order/query/cancel/openOrders/allOrders 응답과 병합 fill을 OrderResult로 변환한다.
    인자: payload -> 공식 order response object
        expected_symbol -> 호출자가 요청한 symbol
        expected_client_order_id -> 원 주문의 client order ID
        fills -> FULL 또는 myTrades에서 정규화한 누적 fill
        fallback_processed_at -> 응답 timestamp가 없을 때 사용할 UTC 시각
        failure_reason -> transport 밖에서 추가할 안전한 실패 사유
        retry_after -> rate limit 대기 시간
    반환값: 식별자와 누적 fill이 검증된 OrderResult
    작성 날짜: 2026/08/22
    """
    if not isinstance(payload, Mapping):
        raise BinancePayloadError("order payload must be an object")
    if not isinstance(fills, tuple):
        raise TypeError("fills must be a tuple")
    selected_symbol = _validate_canonical_text(payload.get("symbol"), "symbol")
    if selected_symbol != expected_symbol:
        raise BinancePayloadError("order symbol does not match request")

    # Cancel 응답은 원 주문 ID를 origClientOrderId에 두므로 두 공식 필드 중 하나를 확인한다.
    client_order_id_candidates = {
        client_order_id
        for client_order_id in (
            payload.get("clientOrderId"),
            payload.get("origClientOrderId"),
        )
        if isinstance(client_order_id, str)
    }
    if expected_client_order_id not in client_order_id_candidates:
        raise BinancePayloadError("client order ID does not match request")

    # Binance orderId는 양수 정수이며 domain에는 정밀도 손실 없는 문자열로 전달한다.
    exchange_order_id_value = payload.get("orderId")
    if (
        isinstance(exchange_order_id_value, bool)
        or not isinstance(exchange_order_id_value, int)
        or exchange_order_id_value <= 0
    ):
        raise BinancePayloadError("orderId must be a positive integer")
    exchange_order_id = str(exchange_order_id_value)
    raw_status = _validate_canonical_text(payload.get("status"), "status")
    status = _ORDER_STATUS_BY_BINANCE_VALUE.get(raw_status)
    if status is None:
        raise BinancePayloadError("unsupported Binance order status")

    # 누적 체결 수량은 myTrades fill 합계와 정확히 일치해야 회계 결과를 공개한다.
    executed_quantity = _read_decimal_string(
        payload.get("executedQty"),
        "executedQty",
    )
    with localcontext() as decimal_context:
        decimal_context.prec = 34
        mapped_fill_quantity = sum(
            (fill_value.quantity for fill_value in fills),
            start=_DECIMAL_ZERO,
        )
    if mapped_fill_quantity != executed_quantity:
        raise BinancePayloadError("executedQty does not match mapped fills")
    if any(
        fill_value.exchange_order_id != exchange_order_id
        for fill_value in fills
    ):
        raise BinancePayloadError("mapped fill belongs to a different order")

    # 2026 expiryReason은 별도 필드가 있을 때 원인 추적용 failure_reason으로 보존한다.
    selected_failure_reason = failure_reason
    if selected_failure_reason is None and "expiryReason" in payload:
        selected_failure_reason = _validate_canonical_text(
            payload.get("expiryReason"),
            "expiryReason",
        )

    return OrderResult(
        symbol=selected_symbol,
        client_order_id=expected_client_order_id,
        status=status,
        processed_at=_read_order_processed_at(payload, fallback_processed_at),
        exchange_order_id=exchange_order_id,
        fills=fills,
        failure_reason=selected_failure_reason,
        retry_after=retry_after,
    )  # Raw payload는 adapter 경계 밖으로 노출하지 않는다.
