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
from binance_auto_trader.domain.trading.states import OrderSide


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
_EXCHANGE_ORDER_COUNT_FILTER_TYPES = frozenset(
    {
        "EXCHANGE_MAX_NUM_ORDERS",
        "EXCHANGE_MAX_NUM_ALGO_ORDERS",
        "EXCHANGE_MAX_NUM_ICEBERG_ORDERS",
        "EXCHANGE_MAX_NUM_ORDER_LISTS",
    }
)
_SYMBOL_ORDER_COUNT_FILTER_TYPES = frozenset(
    {
        "MAX_NUM_ORDERS",
        "MAX_NUM_ALGO_ORDERS",
        "MAX_NUM_ICEBERG_ORDERS",
        "MAX_NUM_ORDER_AMENDS",
        "MAX_NUM_ORDER_LISTS",
    }
)
_PLAIN_MARKET_PASSIVE_SYMBOL_FILTER_TYPES = frozenset(
    {
        "PRICE_FILTER",
        "PERCENT_PRICE",
        "PERCENT_PRICE_BY_SIDE",
        "ICEBERG_PARTS",
        "TRAILING_DELTA",
    }
)


class BinancePayloadError(ValueError):
    """
    클래스 이름: BinancePayloadError
    기능: 공식 Binance payload가 필요한 필드나 일관성을 만족하지 못한 오류를 나타낸다.
    작성 날짜: 2026/08/22
    """


class SymbolFilterError(ValueError):
    """
    클래스 이름: SymbolFilterError
    기능: 주문이 symbol 상태·수량·notional·account asset/position filter를 통과하지 못했음을 나타낸다.
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
class AccountAssetFilter:
    """
    클래스 이름: AccountAssetFilter
    기능: 계정에 적용된 공식 MAX_ASSET 단일 주문 자산 상한을 불변 보존한다.
    작성 날짜: 2026/08/31
    """

    filter_type: str
    asset: str
    maximum_quantity: Decimal

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: MAX_ASSET 종류, canonical 자산과 음이 아닌 유한 상한을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        # 이 DTO는 /myFilters의 공식 MAX_ASSET 항목만 표현하도록 종류를 고정한다.
        if self.filter_type != "MAX_ASSET":
            raise ValueError("filter_type must be MAX_ASSET")
        if (
            not isinstance(self.asset, str)
            or not self.asset
            or self.asset != self.asset.strip().upper()
            or not self.asset.isascii()
            or not self.asset.isalnum()
        ):
            raise ValueError("asset must be canonical uppercase ASCII")

        # 공식 limit 문자열을 변환한 Decimal은 0 상한도 실제 제한으로 보존한다.
        _validate_non_negative_decimal(
            self.maximum_quantity,
            "maximum_quantity",
        )  # 검증된 값은 frozen field에 원문 Decimal 그대로 남긴다.


@dataclass(frozen=True, slots=True)
class AccountOrderCountFilter:
    """
    클래스 이름: AccountOrderCountFilter
    기능: /myFilters의 exchange 또는 symbol 주문 개수 상한 하나를 불변 보존한다.
    작성 날짜: 2026/08/31
    """

    filter_type: str
    maximum_count: int

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 공식 주문 개수 filter 종류와 음이 아닌 정수 상한을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        # Exchange와 symbol scope의 공식 count filter만 한 DTO에 허용한다.
        allowed_filter_types = (
            _EXCHANGE_ORDER_COUNT_FILTER_TYPES
            | _SYMBOL_ORDER_COUNT_FILTER_TYPES
        )
        if self.filter_type not in allowed_filter_types:
            raise ValueError("unsupported account order count filter type")
        if (
            isinstance(self.maximum_count, bool)
            or not isinstance(self.maximum_count, int)
            or self.maximum_count < 0
        ):
            raise ValueError("maximum_count must be a non-negative integer")

        return None  # Scope와 count를 모두 통과한 frozen DTO만 생성 완료한다.


@dataclass(frozen=True, slots=True)
class AccountRelevantFilters:
    """
    클래스 이름: AccountRelevantFilters
    기능: signed /myFilters의 exchange·symbol·asset 규칙을 raw JSON 없이 보존한다.
    작성 날짜: 2026/08/31
    """

    symbol: str
    exchange_order_count_filters: tuple[AccountOrderCountFilter, ...]
    symbol_order_count_filters: tuple[AccountOrderCountFilter, ...]
    symbol_quantity_filters: tuple[QuantityFilter, ...]
    symbol_notional_filters: tuple[NotionalFilter, ...]
    symbol_maximum_position: Decimal | None
    passive_symbol_filter_types: frozenset[str]
    asset_filters: tuple[AccountAssetFilter, ...]

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 각 filter scope의 exact DTO 타입, 중복과 값 범위를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        # Body에 없는 요청 symbol은 signed request context에서 canonical 값으로 반드시 결속한다.
        if (
            not isinstance(self.symbol, str)
            or not self.symbol
            or self.symbol != self.symbol.strip().upper()
            or not self.symbol.isascii()
            or not self.symbol.isalnum()
        ):
            raise ValueError("symbol must be canonical uppercase ASCII")

        # Scope별 collection은 immutable exact tuple만 받아 raw mapping 혼입을 막는다.
        for field_name in (
            "exchange_order_count_filters",
            "symbol_order_count_filters",
        ):
            filter_values = getattr(self, field_name)
            if not isinstance(filter_values, tuple) or any(
                type(filter_value) is not AccountOrderCountFilter
                for filter_value in filter_values
            ):
                raise TypeError(
                    f"{field_name} must be an AccountOrderCountFilter tuple"
                )
            if len({value.filter_type for value in filter_values}) != len(
                filter_values
            ):
                raise ValueError(f"{field_name} must not contain duplicates")

        # Exchange와 symbol count 종류가 서로 잘못 배치되면 생성 시점에 거부한다.
        if any(
            value.filter_type not in _EXCHANGE_ORDER_COUNT_FILTER_TYPES
            for value in self.exchange_order_count_filters
        ):
            raise ValueError("exchange count filter has an invalid scope")
        if any(
            value.filter_type not in _SYMBOL_ORDER_COUNT_FILTER_TYPES
            for value in self.symbol_order_count_filters
        ):
            raise ValueError("symbol count filter has an invalid scope")

        # MARKET 수량·notional filter는 기존 strict DTO를 그대로 재사용한다.
        if not isinstance(self.symbol_quantity_filters, tuple) or any(
            type(filter_value) is not QuantityFilter
            for filter_value in self.symbol_quantity_filters
        ):
            raise TypeError(
                "symbol_quantity_filters must be a QuantityFilter tuple"
            )
        if not isinstance(self.symbol_notional_filters, tuple) or any(
            type(filter_value) is not NotionalFilter
            for filter_value in self.symbol_notional_filters
        ):
            raise TypeError(
                "symbol_notional_filters must be a NotionalFilter tuple"
            )
        if len(
            {value.filter_type for value in self.symbol_quantity_filters}
        ) != len(self.symbol_quantity_filters):
            raise ValueError("symbol_quantity_filters must not contain duplicates")
        if len(
            {value.filter_type for value in self.symbol_notional_filters}
        ) != len(self.symbol_notional_filters):
            raise ValueError("symbol_notional_filters must not contain duplicates")

        # MAX_POSITION은 선택 규칙이지만 존재하면 0을 포함한 유한 Decimal이어야 한다.
        if self.symbol_maximum_position is not None:
            _validate_non_negative_decimal(
                self.symbol_maximum_position,
                "symbol_maximum_position",
            )
        if not isinstance(self.passive_symbol_filter_types, frozenset):
            raise TypeError("passive_symbol_filter_types must be a frozenset")
        if not self.passive_symbol_filter_types.issubset(
            _PLAIN_MARKET_PASSIVE_SYMBOL_FILTER_TYPES
        ):
            raise ValueError("passive_symbol_filter_types contains an unknown type")

        # Asset scope는 MAX_ASSET exact DTO와 자산별 단일 항목만 허용한다.
        if not isinstance(self.asset_filters, tuple) or any(
            type(filter_value) is not AccountAssetFilter
            for filter_value in self.asset_filters
        ):
            raise TypeError("asset_filters must be an AccountAssetFilter tuple")
        if len({value.asset for value in self.asset_filters}) != len(
            self.asset_filters
        ):
            raise ValueError("asset_filters must not contain duplicate assets")

        return None  # 세 scope의 type·중복·범위 검증이 끝난 경우만 생성 완료한다.


@dataclass(frozen=True, slots=True)
class ReferencePrice:
    """
    클래스 이름: ReferencePrice
    기능: 공식 referencePrice의 symbol·양수 가격·거래소 유효 시각을 불변 보존한다.
    작성 날짜: 2026/08/31
    """

    symbol: str
    price: Decimal
    exchange_timestamp: int

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: canonical symbol, 양수 Decimal 가격과 음이 아닌 millisecond timestamp를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        # Public 응답의 symbol은 주문 규칙과 같은 canonical 대문자 ASCII 형식이어야 한다.
        if (
            not isinstance(self.symbol, str)
            or not self.symbol
            or self.symbol != self.symbol.strip().upper()
            or not self.symbol.isascii()
            or not self.symbol.isalnum()
        ):
            raise ValueError("symbol must be canonical uppercase ASCII")
        _validate_positive_decimal(self.price, "price")

        # bool은 int 하위 타입이므로 거래소 timestamp 계약에서 명시적으로 배제한다.
        if (
            isinstance(self.exchange_timestamp, bool)
            or not isinstance(self.exchange_timestamp, int)
            or self.exchange_timestamp < 0
        ):
            raise ValueError(
                "exchange_timestamp must be a non-negative integer"
            )

        return None  # Symbol·price·timestamp가 모두 canonical인 DTO만 유지한다.


@dataclass(frozen=True, slots=True)
class SymbolTradingRules:
    """
    클래스 이름: SymbolTradingRules
    기능: exchangeInfo에서 주문 전 검증에 필요한 Spot MARKET과 MAX_POSITION 규칙을 보존한다.
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
    maximum_position: Decimal | None = None
    public_relevant_filters: AccountRelevantFilters | None = None

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

        # MAX_POSITION은 없을 수 있지만 있으면 0을 포함한 유한 비음수 상한이어야 한다.
        if self.maximum_position is not None:
            _validate_non_negative_decimal(
                self.maximum_position,
                "maximum_position",
            )
        if self.public_relevant_filters is not None:
            if type(self.public_relevant_filters) is not AccountRelevantFilters:
                raise TypeError(
                    "public_relevant_filters must be an exact AccountRelevantFilters"
                )
            if self.public_relevant_filters.symbol != self.symbol:
                raise ValueError(
                    "public_relevant_filters symbol must match rules symbol"
                )
            if self.public_relevant_filters.asset_filters:
                raise ValueError(
                    "public exchangeInfo filters must not contain asset filters"
                )


@dataclass(frozen=True, slots=True)
class OrderPreparationFilterEvidence:
    """
    클래스 이름: OrderPreparationFilterEvidence
    기능: 한 prepare가 사용한 symbol·account filter와 reference price 및 관찰 시각을 보존한다.
    작성 날짜: 2026/08/31
    """

    intent_id: str
    client_order_id: str
    side: OrderSide
    observed_at: datetime
    rules: SymbolTradingRules
    account_filters: AccountRelevantFilters
    account_filters_observed_at: datetime
    account_open_orders_observed_at: datetime | None
    account_open_order_lists_observed_at: datetime | None
    account_open_state_verified_empty: bool
    reference_price: ReferencePrice
    reference_price_observed_at: datetime

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 주문 identity와 submit-time public·account filter 증거 및 UTC 관찰 시각을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        # Credential이나 raw 응답을 담을 field가 없으며 correlation identity도 canonical text만 허용한다.
        for field_name, field_value in (
            ("intent_id", self.intent_id),
            ("client_order_id", self.client_order_id),
        ):
            _validate_canonical_text(field_value, field_name)
        if not isinstance(self.side, OrderSide):
            raise TypeError("side must be an OrderSide")
        if (
            not isinstance(self.observed_at, datetime)
            or self.observed_at.tzinfo is None
            or self.observed_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("observed_at must be a timezone-aware UTC datetime")
        if type(self.rules) is not SymbolTradingRules:
            raise TypeError("rules must be an exact SymbolTradingRules")

        # 계정 필터는 세 scope를 모두 보존한 exact frozen DTO만 허용한다.
        if type(self.account_filters) is not AccountRelevantFilters:
            raise TypeError(
                "account_filters must be an exact AccountRelevantFilters"
            )
        if self.account_filters.symbol != self.rules.symbol:
            raise ValueError("account_filters symbol must match rules symbol")

        # 각 fetch 완료 시각은 서로 합성하지 않고 timezone-aware UTC로 따로 보존한다.
        for field_name in (
            "account_filters_observed_at",
            "reference_price_observed_at",
        ):
            observed_value = getattr(self, field_name)
            if (
                not isinstance(observed_value, datetime)
                or observed_value.tzinfo is None
                or observed_value.utcoffset() != timedelta(0)
            ):
                raise ValueError(
                    f"{field_name} must be a timezone-aware UTC datetime"
                )

        # Account-wide isolation은 count filter 유무와 관계없이 두 signed snapshot을 모두 완전한 empty로 고정한다.
        if type(self.account_open_state_verified_empty) is not bool:
            raise TypeError("account_open_state_verified_empty must be a bool")
        for field_name in (
            "account_open_orders_observed_at",
            "account_open_order_lists_observed_at",
        ):
            observed_value = getattr(self, field_name)
            if observed_value is not None and (
                not isinstance(observed_value, datetime)
                or observed_value.tzinfo is None
                or observed_value.utcoffset() != timedelta(0)
            ):
                raise ValueError(
                    f"{field_name} must be None or a timezone-aware UTC datetime"
                )
        if (
            not self.account_open_state_verified_empty
            or self.account_open_orders_observed_at is None
            or self.account_open_order_lists_observed_at is None
        ):
            raise ValueError(
                "prepare requires complete empty account open-state evidence"
            )
        if type(self.reference_price) is not ReferencePrice:
            raise TypeError("reference_price must be an exact ReferencePrice")
        if self.reference_price.symbol != self.rules.symbol:
            raise ValueError("reference_price symbol must match rules symbol")

    @property
    def account_asset_filters(self) -> tuple[AccountAssetFilter, ...]:
        """
        함수 이름: account_asset_filters()
        기능: 기존 trace 소비자에 full signed DTO의 MAX_ASSET projection을 제공한다.
        인자: 없음
        반환값: immutable AccountAssetFilter tuple
        작성 날짜: 2026/08/31
        """
        return self.account_filters.asset_filters  # Raw payload 없이 호환 projection만 반환한다.


@dataclass(frozen=True, slots=True)
class OrderSubmissionAttemptEvidence:
    """
    클래스 이름: OrderSubmissionAttemptEvidence
    기능: 한 prepared Order의 실제 REST POST 시작 identity와 서버 정렬 UTC 시각을 불변 보존한다.
    작성 날짜: 2026/08/31
    """

    intent_id: str
    client_order_id: str
    side: OrderSide
    attempted_at: datetime

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 주문 identity, side와 timezone-aware UTC submission 시각을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        # Raw request, signature와 credential field를 구조적으로 배제하고 safe correlation만 보존한다.
        for field_name, field_value in (
            ("intent_id", self.intent_id),
            ("client_order_id", self.client_order_id),
        ):
            _validate_canonical_text(field_value, field_name)
        if not isinstance(self.side, OrderSide):
            raise TypeError("side must be an OrderSide")
        if (
            not isinstance(self.attempted_at, datetime)
            or self.attempted_at.tzinfo is None
            or self.attempted_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("attempted_at must be a timezone-aware UTC datetime")


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


def _parse_account_order_count_filter(
    payload: Mapping[object, object],
    *,
    allowed_filter_types: frozenset[str],
) -> AccountOrderCountFilter:
    """
    함수 이름: _parse_account_order_count_filter()
    기능: /myFilters의 scope별 주문 개수 filter를 exact 필드와 typed 상한으로 변환한다.
    인자: payload -> exchangeFilters 또는 symbolFilters의 한 JSON object
        allowed_filter_types -> 현재 scope에 허용된 공식 filter type 집합
    반환값: 검증된 AccountOrderCountFilter
    작성 날짜: 2026/08/31
    """
    # 현재 scope의 공식 type을 먼저 고정한 뒤 type별 유일 상한 field를 선택한다.
    filter_type = _validate_canonical_text(
        payload.get("filterType"),
        "filterType",
    )
    if filter_type not in allowed_filter_types:
        raise BinancePayloadError("unsupported account order count filter type")
    maximum_field_by_filter_type = {
        "EXCHANGE_MAX_NUM_ORDERS": "maxNumOrders",
        "EXCHANGE_MAX_NUM_ALGO_ORDERS": "maxNumAlgoOrders",
        "EXCHANGE_MAX_NUM_ICEBERG_ORDERS": "maxNumIcebergOrders",
        "EXCHANGE_MAX_NUM_ORDER_LISTS": "maxNumOrderLists",
        "MAX_NUM_ORDERS": "maxNumOrders",
        "MAX_NUM_ALGO_ORDERS": "maxNumAlgoOrders",
        "MAX_NUM_ICEBERG_ORDERS": "maxNumIcebergOrders",
        "MAX_NUM_ORDER_AMENDS": "maxNumOrderAmends",
        "MAX_NUM_ORDER_LISTS": "maxNumOrderLists",
    }
    maximum_field = maximum_field_by_filter_type[filter_type]
    if set(payload) != {"filterType", maximum_field}:
        raise BinancePayloadError(
            "account order count filter fields do not match official schema"
        )

    # bool을 정수로 수용하지 않는 공통 reader로 공식 non-negative count를 읽는다.
    return AccountOrderCountFilter(
        filter_type=filter_type,
        maximum_count=_read_non_negative_integer(
            payload.get(maximum_field),
            maximum_field,
        ),
    )  # Exact schema의 count 하나만 immutable DTO로 반환한다.


def _validate_plain_market_passive_symbol_filter(
    payload: Mapping[object, object],
    filter_type: str,
) -> None:
    """
    함수 이름: _validate_plain_market_passive_symbol_filter()
    기능: price·iceberg·trailing 전용 filter의 exact schema를 검증하고 plain MARKET 비적용을 확정한다.
    인자: payload -> symbolFilters의 한 JSON object
        filter_type -> 검증할 공식 filter type
    반환값: 없음
    작성 날짜: 2026/08/31
    """
    # Price·iceberg·trailing filter는 각 type의 decimal과 integer field 조합을 exact map으로 유지한다.
    decimal_fields_by_filter_type = {
        "PRICE_FILTER": ("minPrice", "maxPrice", "tickSize"),
        "PERCENT_PRICE": ("multiplierUp", "multiplierDown"),
        "PERCENT_PRICE_BY_SIDE": (
            "bidMultiplierUp",
            "bidMultiplierDown",
            "askMultiplierUp",
            "askMultiplierDown",
        ),
    }
    integer_fields_by_filter_type = {
        "PERCENT_PRICE": ("avgPriceMins",),
        "PERCENT_PRICE_BY_SIDE": ("avgPriceMins",),
        "ICEBERG_PARTS": ("limit",),
        "TRAILING_DELTA": (
            "minTrailingAboveDelta",
            "maxTrailingAboveDelta",
            "minTrailingBelowDelta",
            "maxTrailingBelowDelta",
        ),
    }
    decimal_fields = decimal_fields_by_filter_type.get(filter_type, ())
    integer_fields = integer_fields_by_filter_type.get(filter_type, ())
    if set(payload) != {"filterType", *decimal_fields, *integer_fields}:
        raise BinancePayloadError(
            "passive symbol filter fields do not match official schema"
        )

    # 모든 numeric field를 공식 JSON type대로 읽어 malformed 또는 추가 field를 차단한다.
    parsed_decimals = {
        field_name: _read_decimal_string(payload.get(field_name), field_name)
        for field_name in decimal_fields
    }
    parsed_integers = {
        field_name: _read_non_negative_integer(
            payload.get(field_name),
            field_name,
        )
        for field_name in integer_fields
    }
    if any(value < _DECIMAL_ZERO for value in parsed_decimals.values()):
        raise BinancePayloadError(
            "passive symbol filter decimals must be non-negative"
        )
    if filter_type == "PRICE_FILTER":
        maximum_price = parsed_decimals["maxPrice"]
        minimum_price = parsed_decimals["minPrice"]
        if maximum_price > _DECIMAL_ZERO and maximum_price < minimum_price:
            raise BinancePayloadError("PRICE_FILTER range is invalid")
    if filter_type == "PERCENT_PRICE" and (
        parsed_decimals["multiplierUp"]
        < parsed_decimals["multiplierDown"]
    ):
        raise BinancePayloadError("PERCENT_PRICE range is invalid")
    if filter_type == "PERCENT_PRICE_BY_SIDE" and (
        parsed_decimals["bidMultiplierUp"]
        < parsed_decimals["bidMultiplierDown"]
        or parsed_decimals["askMultiplierUp"]
        < parsed_decimals["askMultiplierDown"]
    ):
        raise BinancePayloadError("PERCENT_PRICE_BY_SIDE range is invalid")
    if filter_type == "TRAILING_DELTA" and (
        parsed_integers["maxTrailingAboveDelta"]
        < parsed_integers["minTrailingAboveDelta"]
        or parsed_integers["maxTrailingBelowDelta"]
        < parsed_integers["minTrailingBelowDelta"]
    ):
        raise BinancePayloadError("TRAILING_DELTA range is invalid")

    # Plain quantity MARKET은 price·stopPrice·icebergQty·trailingDelta를 보내지 않는다.
    return None  # Exact schema와 범위를 통과한 passive type만 dispatcher에 승인한다.


def _parse_account_asset_filter(
    payload: Mapping[object, object],
) -> AccountAssetFilter:
    """
    함수 이름: _parse_account_asset_filter()
    기능: assetFilters의 MAX_ASSET 한 항목을 exact DTO로 변환한다.
    인자: payload -> assetFilters의 한 JSON object
    반환값: 검증된 AccountAssetFilter
    작성 날짜: 2026/08/31
    """
    # Asset scope는 공식 MAX_ASSET의 세 field만 허용해 schema drift를 먼저 차단한다.
    if set(payload) != {"filterType", "asset", "limit"}:
        raise BinancePayloadError(
            "MAX_ASSET fields do not match official schema"
        )
    filter_type = _validate_canonical_text(
        payload.get("filterType"),
        "filterType",
    )
    if filter_type != "MAX_ASSET":
        raise BinancePayloadError("unsupported account asset filter type")
    asset = _validate_canonical_text(payload.get("asset"), "asset")
    if asset != asset.upper() or not asset.isascii() or not asset.isalnum():
        raise BinancePayloadError("asset must be canonical uppercase ASCII")

    # MAX_ASSET limit은 float를 거치지 않은 단일 주문 한도로 보존한다.
    return AccountAssetFilter(
        filter_type=filter_type,
        asset=asset,
        maximum_quantity=_read_decimal_string(
            payload.get("limit"),
            "limit",
        ),
    )  # Asset identity와 Decimal limit을 검증한 exact DTO만 반환한다.


def parse_account_relevant_filters(
    payload: object,
    symbol: str,
) -> AccountRelevantFilters:
    """
    함수 이름: parse_account_relevant_filters()
    기능: 공식 /myFilters의 세 scope를 type별 strict DTO와 plain MARKET 적용 집합으로 변환한다.
    인자: payload -> 공식 GET /api/v3/myFilters JSON object
        symbol -> body에 없는 signed request의 canonical Spot symbol
    반환값: raw 값이나 credential을 포함하지 않는 AccountRelevantFilters
    작성 날짜: 2026/08/31
    """
    expected_symbol = _validate_canonical_text(symbol, "symbol")
    if (
        expected_symbol != expected_symbol.upper()
        or not expected_symbol.isascii()
        or not expected_symbol.isalnum()
    ):
        raise BinancePayloadError("symbol must be canonical uppercase ASCII")
    if not isinstance(payload, Mapping):
        raise BinancePayloadError("myFilters payload must be an object")

    # 최신 공식 response root는 세 collection을 정확히 한 번씩 요구한다.
    expected_top_level_fields = {
        "exchangeFilters",
        "symbolFilters",
        "assetFilters",
    }
    if set(payload) != expected_top_level_fields:
        raise BinancePayloadError("myFilters payload fields do not match schema")
    for field_name in expected_top_level_fields:
        raw_filters = payload.get(field_name)
        if not isinstance(raw_filters, (list, tuple)):
            raise BinancePayloadError(f"{field_name} must be an array")
        if any(not isinstance(raw_filter, Mapping) for raw_filter in raw_filters):
            raise BinancePayloadError(
                f"each {field_name} item must be an object"
            )

    # Exchange scope는 공식 네 count filter 외의 type을 추측하지 않는다.
    exchange_count_filters: list[AccountOrderCountFilter] = []
    seen_exchange_filter_types: set[str] = set()
    for raw_filter in payload["exchangeFilters"]:
        filter_type = _validate_canonical_text(
            raw_filter.get("filterType"),
            "filterType",
        )
        if filter_type in seen_exchange_filter_types:
            raise BinancePayloadError(
                "exchangeFilters must not contain duplicate types"
            )
        seen_exchange_filter_types.add(filter_type)
        exchange_count_filters.append(
            _parse_account_order_count_filter(
                raw_filter,
                allowed_filter_types=_EXCHANGE_ORDER_COUNT_FILTER_TYPES,
            )
        )

    # Symbol scope는 공식 type마다 exact schema와 plain MARKET 적용 의미를 구분한다.
    symbol_count_filters: list[AccountOrderCountFilter] = []
    symbol_quantity_filters: list[QuantityFilter] = []
    symbol_notional_filters: list[NotionalFilter] = []
    symbol_maximum_position: Decimal | None = None
    passive_symbol_filter_types: set[str] = set()
    seen_symbol_filter_types: set[str] = set()
    for raw_filter in payload["symbolFilters"]:
        filter_type = _validate_canonical_text(
            raw_filter.get("filterType"),
            "filterType",
        )
        if filter_type in seen_symbol_filter_types:
            raise BinancePayloadError(
                "symbolFilters must not contain duplicate types"
            )
        seen_symbol_filter_types.add(filter_type)
        if filter_type in _SYMBOL_ORDER_COUNT_FILTER_TYPES:
            symbol_count_filters.append(
                _parse_account_order_count_filter(
                    raw_filter,
                    allowed_filter_types=_SYMBOL_ORDER_COUNT_FILTER_TYPES,
                )
            )
        elif filter_type in {"LOT_SIZE", "MARKET_LOT_SIZE"}:
            if set(raw_filter) != {
                "filterType",
                "minQty",
                "maxQty",
                "stepSize",
            }:
                raise BinancePayloadError(
                    "quantity filter fields do not match official schema"
                )
            symbol_quantity_filters.append(
                _parse_quantity_filter(raw_filter, filter_type)
            )
        elif filter_type in {"MIN_NOTIONAL", "NOTIONAL"}:
            expected_fields = (
                {
                    "filterType",
                    "minNotional",
                    "applyToMarket",
                    "avgPriceMins",
                }
                if filter_type == "MIN_NOTIONAL"
                else {
                    "filterType",
                    "minNotional",
                    "applyMinToMarket",
                    "maxNotional",
                    "applyMaxToMarket",
                    "avgPriceMins",
                }
            )
            if set(raw_filter) != expected_fields:
                raise BinancePayloadError(
                    "notional filter fields do not match official schema"
                )
            symbol_notional_filters.append(
                _parse_notional_filter(raw_filter)
            )
        elif filter_type == "MAX_POSITION":
            if set(raw_filter) != {"filterType", "maxPosition"}:
                raise BinancePayloadError(
                    "MAX_POSITION fields do not match official schema"
                )
            symbol_maximum_position = _read_decimal_string(
                raw_filter.get("maxPosition"),
                "maxPosition",
            )
            if symbol_maximum_position < _DECIMAL_ZERO:
                raise BinancePayloadError("maxPosition must be non-negative")
        elif filter_type in _PLAIN_MARKET_PASSIVE_SYMBOL_FILTER_TYPES:
            _validate_plain_market_passive_symbol_filter(
                raw_filter,
                filter_type,
            )
            passive_symbol_filter_types.add(filter_type)
        elif filter_type == "T_PLUS_SELL":
            # 최신 공식 SBE에는 type만 있으나 공개 filter 문서에 적용식이 없어 추측을 금지한다.
            raise BinancePayloadError(
                "T_PLUS_SELL evaluation semantics are not documented"
            )
        else:
            raise BinancePayloadError("unsupported relevant symbol filter type")

    # Asset scope는 MAX_ASSET만 허용하고 같은 자산의 중복 한도를 거부한다.
    asset_filters: list[AccountAssetFilter] = []
    seen_assets: set[str] = set()
    for raw_filter in payload["assetFilters"]:
        asset_filter = _parse_account_asset_filter(raw_filter)
        if asset_filter.asset in seen_assets:
            raise BinancePayloadError(
                "account asset filters must not contain duplicate assets"
            )
        seen_assets.add(asset_filter.asset)
        asset_filters.append(asset_filter)

    return AccountRelevantFilters(
        symbol=expected_symbol,
        exchange_order_count_filters=tuple(exchange_count_filters),
        symbol_order_count_filters=tuple(symbol_count_filters),
        symbol_quantity_filters=tuple(symbol_quantity_filters),
        symbol_notional_filters=tuple(symbol_notional_filters),
        symbol_maximum_position=symbol_maximum_position,
        passive_symbol_filter_types=frozenset(passive_symbol_filter_types),
        asset_filters=tuple(asset_filters),
    )  # 모든 공식 scope가 비어 있어도 exact empty DTO로 안전하게 표현한다.


def parse_account_asset_filters(
    payload: object,
    symbol: str,
) -> tuple[AccountAssetFilter, ...]:
    """
    함수 이름: parse_account_asset_filters()
    기능: full /myFilters strict 검증 뒤 호환용 MAX_ASSET tuple만 반환한다.
    인자: payload -> 공식 GET /api/v3/myFilters JSON object
        symbol -> body에 없는 signed request의 canonical Spot symbol
    반환값: 응답 순서를 보존한 AccountAssetFilter tuple
    작성 날짜: 2026/08/31
    """
    # 호환 projection도 먼저 full composite parser를 통과시켜 다른 scope의 schema drift를 숨기지 않는다.
    relevant_filters = parse_account_relevant_filters(payload, symbol)

    return relevant_filters.asset_filters  # 다른 scope도 검증한 뒤 asset projection만 공개한다.


def parse_reference_price(
    payload: object,
    symbol: str,
) -> ReferencePrice:
    """
    함수 이름: parse_reference_price()
    기능: 공식 referencePrice 응답을 요청 symbol에 결속된 non-null 양수 DTO로 변환한다.
    인자: payload -> 공식 GET /api/v3/referencePrice JSON object
        symbol -> 요청한 canonical Spot symbol
    반환값: 엄격히 검증한 ReferencePrice
    작성 날짜: 2026/08/31
    """
    expected_symbol = _validate_canonical_text(symbol, "symbol")
    if not isinstance(payload, Mapping):
        raise BinancePayloadError("referencePrice payload must be an object")
    if set(payload) != {"symbol", "referencePrice", "timestamp"}:
        raise BinancePayloadError(
            "referencePrice payload fields do not match schema"
        )

    # Null은 거래소의 VWAP fallback 가능 상태지만 로컬 Phase 13 경계에서는 추정하지 않는다.
    response_symbol = _validate_canonical_text(
        payload.get("symbol"),
        "symbol",
    )
    if response_symbol != expected_symbol:
        raise BinancePayloadError(
            "referencePrice symbol does not match request"
        )
    raw_reference_price = payload.get("referencePrice")
    if raw_reference_price is None:
        raise BinancePayloadError("referencePrice must be non-null")

    return ReferencePrice(
        symbol=response_symbol,
        price=_read_decimal_string(raw_reference_price, "referencePrice"),
        exchange_timestamp=_read_non_negative_integer(
            payload.get("timestamp"),
            "timestamp",
        ),
    )  # DTO 생성이 0, NaN과 무한 가격을 추가로 차단한다.


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

    # Public exchangeInfo도 myFilters와 같은 공식 union dispatcher로 unknown·extra field를 거부한다.
    raw_exchange_filters = payload.get("exchangeFilters")
    if not isinstance(raw_exchange_filters, (list, tuple)):
        raise BinancePayloadError("exchangeInfo exchangeFilters must be an array")
    public_relevant_filters = parse_account_relevant_filters(
        {
            "exchangeFilters": raw_exchange_filters,
            "symbolFilters": raw_filters,
            "assetFilters": [],
        },
        expected_symbol,
    )

    # MARKET 제출에는 일반 lot와 market 전용 lot를 모두 검사해야 한다.
    lot_size_payload = filters_by_type.get("LOT_SIZE")
    market_lot_size_payload = filters_by_type.get("MARKET_LOT_SIZE")
    if lot_size_payload is None or market_lot_size_payload is None:
        raise BinancePayloadError("LOT_SIZE and MARKET_LOT_SIZE are required")

    # 두 notional filter는 동시에 존재할 수 있으므로 존재하는 규칙을 모두 보존한다.
    notional_filters = public_relevant_filters.symbol_notional_filters
    if not notional_filters:
        raise BinancePayloadError("a notional filter is required")

    # MAX_POSITION은 BUY account exposure 계산 없이는 안전하게 통과시킬 수 있도록 원문을 보존한다.
    maximum_position = public_relevant_filters.symbol_maximum_position

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
        maximum_position=maximum_position,
        public_relevant_filters=public_relevant_filters,
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
    reference_price: Decimal,
    rules: SymbolTradingRules,
) -> None:
    """
    함수 이름: validate_market_notional()
    기능: 공식 non-null reference price로 MARKET 주문의 활성 notional 한계를 사전 점검한다.
    인자: quantity -> quantity filter를 적용한 제출 수량
        reference_price -> /referencePrice에서 받은 양수 quote 가격
        rules -> 현재 exchangeInfo symbol 규칙
    반환값: 없음
    작성 날짜: 2026/08/22
    """
    _validate_positive_decimal(quantity, "quantity")
    _validate_positive_decimal(reference_price, "reference_price")
    if not isinstance(rules, SymbolTradingRules):
        raise TypeError("rules must be SymbolTradingRules")

    # 공식 문서와 같은 non-null reference price를 사용해 stale decision price 추정을 배제한다.
    with localcontext() as decimal_context:
        decimal_context.prec = 34
        notional = quantity * reference_price

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


def validate_account_asset_filters(
    quantity: Decimal,
    reference_price: Decimal,
    rules: SymbolTradingRules,
    account_asset_filters: tuple[AccountAssetFilter, ...],
) -> None:
    """
    함수 이름: validate_account_asset_filters()
    기능: MAX_ASSET를 base 수량에 적용하고 공식 가격식이 없는 quantity MARKET quote 한도를 차단한다.
    인자: quantity -> quantity filter를 적용한 제출 수량
        reference_price -> 다른 MARKET filter와 provenance를 공유하는 공식 non-null reference price
        rules -> base·quote asset이 결속된 최신 symbol 규칙
        account_asset_filters -> signed /myFilters의 MAX_ASSET tuple
    반환값: 없음
    작성 날짜: 2026/08/31
    """
    _validate_positive_decimal(quantity, "quantity")
    _validate_positive_decimal(reference_price, "reference_price")
    if not isinstance(rules, SymbolTradingRules):
        raise TypeError("rules must be SymbolTradingRules")
    if not isinstance(account_asset_filters, tuple) or any(
        type(account_filter) is not AccountAssetFilter
        for account_filter in account_asset_filters
    ):
        raise TypeError(
            "account_asset_filters must be an AccountAssetFilter tuple"
        )

    for account_filter in account_asset_filters:
        if account_filter.asset == rules.base_asset:
            filtered_quantity = quantity
            failure_scope = "BASE"
        elif account_filter.asset == rules.quote_asset:
            # 공식 MAX_ASSET 문서는 quantity MARKET의 quote 환산 가격식을 명시하지 않는다.
            raise SymbolFilterError(
                "FILTER_MAX_ASSET_QUOTE_MARKET_PRICE_UNDEFINED"
            )
        else:
            raise SymbolFilterError("FILTER_MAX_ASSET_SYMBOL_MISMATCH")

        # MAX_ASSET limit 0도 비활성 표식이 아니므로 모든 양수 주문을 정확히 거부한다.
        if filtered_quantity > account_filter.maximum_quantity:
            raise SymbolFilterError(
                f"FILTER_MAX_ASSET_{failure_scope}_MAXIMUM"
            )

    return None  # 모든 account asset 상한을 통과한 MARKET 수량만 승인한다.


def validate_account_relevant_filters(
    quantity: Decimal,
    reference_price: Decimal,
    rules: SymbolTradingRules,
    account_filters: AccountRelevantFilters,
    *,
    side: OrderSide,
    account_open_state_verified_empty: bool,
) -> None:
    """
    함수 이름: validate_account_relevant_filters()
    기능: signed relevant filter를 plain quantity MARKET 주문과 complete zero-open state에 적용한다.
    인자: quantity -> quantity filter를 적용한 제출 수량
        reference_price -> 공식 non-null reference price
        rules -> fresh public exchangeInfo의 symbol 규칙
        account_filters -> fresh signed /myFilters의 strict composite DTO
        side -> 실제 Binance MARKET side
        account_open_state_verified_empty -> all-symbol open order와 open list가 모두 0인 증거
    반환값: 없음
    작성 날짜: 2026/08/31
    """
    _validate_positive_decimal(quantity, "quantity")
    _validate_positive_decimal(reference_price, "reference_price")
    if type(rules) is not SymbolTradingRules:
        raise TypeError("rules must be an exact SymbolTradingRules")
    if type(account_filters) is not AccountRelevantFilters:
        raise TypeError(
            "account_filters must be an exact AccountRelevantFilters"
        )
    if account_filters.symbol != rules.symbol:
        raise SymbolFilterError("FILTER_ACCOUNT_SYMBOL_MISMATCH")
    if not isinstance(side, OrderSide):
        raise TypeError("side must be an OrderSide")
    if type(account_open_state_verified_empty) is not bool:
        raise TypeError("account_open_state_verified_empty must be a bool")

    # Signed quantity/notional 규칙은 public exchangeInfo의 같은 type·값과 exact 일치해야 한다.
    public_quantity_filters = {
        rules.lot_size.filter_type: rules.lot_size,
        rules.market_lot_size.filter_type: rules.market_lot_size,
    }
    for signed_filter in account_filters.symbol_quantity_filters:
        if public_quantity_filters.get(signed_filter.filter_type) != signed_filter:
            raise SymbolFilterError("FILTER_SIGNED_SYMBOL_RULE_MISMATCH")
    public_notional_filters = {
        filter_value.filter_type: filter_value
        for filter_value in rules.notional_filters
    }
    for signed_filter in account_filters.symbol_notional_filters:
        if public_notional_filters.get(signed_filter.filter_type) != signed_filter:
            raise SymbolFilterError("FILTER_SIGNED_SYMBOL_RULE_MISMATCH")

    # Plain MARKET에 수치를 적용하지 않는 signed type도 public symbol에 존재해야 한다.
    public_filters = rules.public_relevant_filters
    if (
        public_filters is None
        and account_filters.passive_symbol_filter_types
    ):
        raise SymbolFilterError("FILTER_PUBLIC_ACCOUNT_RULES_UNAVAILABLE")
    if (
        public_filters is not None
        and not account_filters.passive_symbol_filter_types.issubset(
            public_filters.passive_symbol_filter_types
        )
    ):
        raise SymbolFilterError("FILTER_SIGNED_SYMBOL_RULE_MISMATCH")

    # Account-dependent public count filter는 signed myFilters와 type·limit가 양방향 exact 일치해야 한다.
    if public_filters is None:
        if (
            account_filters.exchange_order_count_filters
            or account_filters.symbol_order_count_filters
        ):
            raise SymbolFilterError("FILTER_PUBLIC_ACCOUNT_RULES_UNAVAILABLE")
    else:
        public_exchange_counts = {
            filter_value.filter_type: filter_value.maximum_count
            for filter_value in public_filters.exchange_order_count_filters
        }
        signed_exchange_counts = {
            filter_value.filter_type: filter_value.maximum_count
            for filter_value in account_filters.exchange_order_count_filters
        }
        public_symbol_counts = {
            filter_value.filter_type: filter_value.maximum_count
            for filter_value in public_filters.symbol_order_count_filters
        }
        signed_symbol_counts = {
            filter_value.filter_type: filter_value.maximum_count
            for filter_value in account_filters.symbol_order_count_filters
        }
        if (
            public_exchange_counts != signed_exchange_counts
            or public_symbol_counts != signed_symbol_counts
        ):
            raise SymbolFilterError(
                "FILTER_SIGNED_ACCOUNT_COUNT_RULE_MISMATCH"
            )

    # MAX_POSITION은 두 endpoint가 같은 값을 보존해야 하며 BUY exposure state 없이는 통과시키지 않는다.
    if account_filters.symbol_maximum_position != rules.maximum_position:
        raise SymbolFilterError("FILTER_SIGNED_MAX_POSITION_MISMATCH")
    if side is OrderSide.BUY and rules.maximum_position is not None:
        raise SymbolFilterError(
            "FILTER_MAX_POSITION_REQUIRES_ACCOUNT_EXPOSURE"
        )

    # Account count filter가 하나라도 있으면 all-symbol order/list snapshot의 완전한 zero를 요구한다.
    count_filters = (
        account_filters.exchange_order_count_filters
        + account_filters.symbol_order_count_filters
    )
    if count_filters and not account_open_state_verified_empty:
        raise SymbolFilterError(
            "FILTER_ACCOUNT_OPEN_STATE_REQUIRES_COMPLETE_EMPTY_SNAPSHOT"
        )
    for count_filter in count_filters:
        if (
            count_filter.filter_type
            in {"EXCHANGE_MAX_NUM_ORDERS", "MAX_NUM_ORDERS"}
            and count_filter.maximum_count < 1
        ):
            raise SymbolFilterError(
                f"FILTER_{count_filter.filter_type}_MAXIMUM"
            )

    # Plain MARKET에는 algo·iceberg·list·amend와 price/trailing parameter가 없어 추가 상한을 소비하지 않는다.
    validate_account_asset_filters(
        quantity,
        reference_price,
        rules,
        account_filters.asset_filters,
    )

    return None  # 모든 signed account 규칙과 public 중복 규칙이 일치한 경우만 준비를 계속한다.


def prepare_market_order(
    order: Order,
    rules: SymbolTradingRules,
    *,
    reference_price: Decimal,
) -> Order:
    """
    함수 이름: prepare_market_order()
    기능: 현재 symbol과 reference price를 확인하고 같은 Order의 제출 수량만 안전하게 보정한다.
    인자: order -> 원래 요청 수량과 제출 희망 수량을 보존한 Order
        rules -> 현재 exchangeInfo symbol 규칙
        reference_price -> 공식 non-null reference price
    반환값: submitted_quantity를 보정한 동일 Order
    작성 날짜: 2026/08/22
    """
    if not isinstance(order, Order):
        raise TypeError("order must be an Order")
    if not isinstance(rules, SymbolTradingRules):
        raise TypeError("rules must be SymbolTradingRules")
    _validate_positive_decimal(reference_price, "reference_price")

    # CANCEL_ONLY를 포함한 비거래 상태와 MARKET·Spot 미지원 symbol은 신규 제출을 막는다.
    if order.symbol != rules.symbol:
        raise SymbolFilterError("FILTER_SYMBOL_MISMATCH")
    if rules.status != "TRADING":
        raise SymbolFilterError(f"FILTER_SYMBOL_STATUS_{rules.status}")
    if "MARKET" not in rules.order_types:
        raise SymbolFilterError("FILTER_MARKET_ORDER_UNSUPPORTED")
    if not rules.is_spot_trading_allowed:
        raise SymbolFilterError("FILTER_SPOT_TRADING_DISABLED")
    if order.side is OrderSide.BUY and rules.maximum_position is not None:
        raise SymbolFilterError(
            "FILTER_MAX_POSITION_REQUIRES_ACCOUNT_EXPOSURE"
        )

    # 기존 submitted_quantity를 내린 뒤 공식 reference-price notional을 검사한다.
    prepared_quantity = floor_market_quantity(order.submitted_quantity, rules)
    validate_market_notional(
        prepared_quantity,
        reference_price,
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
