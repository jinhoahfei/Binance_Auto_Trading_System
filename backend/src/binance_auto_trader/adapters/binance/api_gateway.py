"""Spot market/account payload와 fake·testnet 주문 결과를 정규화한다."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from typing import Protocol

from binance_auto_trader.adapters.binance.mappers import (
    AccountAssetFilter,
    AccountRelevantFilters,
    OrderPreparationFilterEvidence,
    OrderSubmissionAttemptEvidence,
    ReferencePrice,
    SymbolTradingRules,
    parse_account_asset_filters,
    parse_account_relevant_filters,
)
from binance_auto_trader.domain.trading.account import (
    AccountSnapshot,
    AssetBalance,
    SUPPORTED_VALUATION_ASSET,
)
from binance_auto_trader.domain.trading.order import Order, OrderResult
from binance_auto_trader.domain.trading.account_execution import AccountExecution
from binance_auto_trader.domain.trading.states import OrderSide
from binance_auto_trader.domain.market import (
    Interval,
    Kline,
    SUPPORTED_INTERVALS,
)


MINIMUM_KLINE_LIMIT = 1
MAXIMUM_KLINE_LIMIT = 1000
DEFAULT_KLINE_LIMIT = 500
APP_CLIENT_ORDER_ID_PREFIX = "bat-"
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_INTERVAL_MILLISECONDS_BY_INTERVAL = {
    Interval.ONE_MINUTE: 60_000,
    Interval.THIRTY_MINUTES: 1_800_000,
    Interval.FOUR_HOURS: 14_400_000,
    Interval.ONE_DAY: 86_400_000,
}


@dataclass(frozen=True, slots=True)
class CommissionDiscountPolicy:
    """
    클래스 이름: CommissionDiscountPolicy
    기능: 한 Spot symbol의 할인과 MARKET BUY 수신 자산 수수료 가능성을 raw payload 없이 보존한다.
    작성 날짜: 2026/08/24
    """

    symbol: str
    enabled_for_account: bool
    enabled_for_symbol: bool
    discount_asset: str | None
    discount_rate: Decimal
    standard_market_buy_rate: Decimal
    special_market_buy_rate: Decimal
    tax_market_buy_rate: Decimal

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: symbol, exact boolean, 할인 자산과 0~1 Decimal 비율을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # Symbol은 REST identity와 같은 trimmed ASCII 대문자 형식을 사용한다.
        if (
            not isinstance(self.symbol, str)
            or not self.symbol
            or self.symbol != self.symbol.strip().upper()
            or not self.symbol.isascii()
            or not self.symbol.isalnum()
        ):
            raise ValueError("symbol must be canonical uppercase ASCII")

        # 공식 string asset은 canonical하게 검증하고 all-zero Testnet absence만 None으로 보존한다.
        if self.discount_asset is not None and (
            not isinstance(self.discount_asset, str)
            or not self.discount_asset
            or self.discount_asset != self.discount_asset.strip().upper()
            or not self.discount_asset.isascii()
            or not self.discount_asset.isalnum()
        ):
            raise ValueError(
                "discount_asset must be canonical uppercase ASCII or None"
            )

        # JSON truthy 값을 허용하지 않고 두 공식 flag와 Decimal 할인 범위를 고정한다.
        if type(self.enabled_for_account) is not bool:
            raise TypeError("enabled_for_account must be a bool")
        if type(self.enabled_for_symbol) is not bool:
            raise TypeError("enabled_for_symbol must be a bool")
        if (
            not isinstance(self.discount_rate, Decimal)
            or not self.discount_rate.is_finite()
            or not Decimal("0") <= self.discount_rate <= Decimal("1")
        ):
            raise ValueError("discount_rate must be a finite Decimal from 0 to 1")

        # 공식 taker+buyer 합산은 음수·NaN을 허용하지 않고 각 수수료 유형을 분리한다.
        for field_name in (
            "standard_market_buy_rate",
            "special_market_buy_rate",
            "tax_market_buy_rate",
        ):
            field_value = getattr(self, field_name)
            if (
                not isinstance(field_value, Decimal)
                or not field_value.is_finite()
                or field_value < Decimal("0")
            ):
                raise ValueError(
                    f"{field_name} must be a non-negative finite Decimal"
                )

        # 정책 객체의 null asset은 할인율과 세 MARKET BUY 합도 0인 안전 부재만 허용한다.
        if self.discount_asset is None and (
            self.discount_rate != Decimal("0")
            or self.standard_market_buy_rate != Decimal("0")
            or self.special_market_buy_rate != Decimal("0")
            or self.tax_market_buy_rate != Decimal("0")
        ):
            raise ValueError(
                "discount_asset may be None only for an all-zero policy"
            )

    @property
    def can_charge_discount_asset(self) -> bool:
        """
        함수 이름: can_charge_discount_asset()
        기능: 계정과 symbol 양쪽에서 별도 discount asset 수수료가 적용 가능한지 반환한다.
        인자: 없음
        반환값: 별도 할인 자산이 실제 수수료 자산이 될 수 있으면 True
        작성 날짜: 2026/08/24
        """
        # String asset은 할인율 0이어도 tax/special 전환 가능성이 있어 두 flag로 판단한다.
        return (
            self.enabled_for_account
            and self.enabled_for_symbol
            and self.discount_asset is not None
        )  # Raw parser는 이 null 예외 전에 12개 공식 비율 전부가 0인지도 확인한다.

    @property
    def market_buy_received_asset_commission_rate(self) -> Decimal:
        """
        함수 이름: market_buy_received_asset_commission_rate()
        기능: MARKET BUY의 수신 base 자산에 적용될 수 있는 전체 taker+buyer 수수료율을 반환한다.
        인자: 없음
        반환값: standard, special과 tax 수수료율의 Decimal 합
        작성 날짜: 2026/08/24
        """
        # 전역 Decimal context와 무관하게 프로젝트 Decimal128 정밀도로 세 유형을 합산한다.
        with localcontext() as decimal_context:
            decimal_context.prec = 34
            decimal_context.rounding = ROUND_HALF_EVEN
            return (
                self.standard_market_buy_rate
                + self.special_market_buy_rate
                + self.tax_market_buy_rate
            )


@dataclass(frozen=True, slots=True)
class Phase13OrderSubmissionAttempt:
    """
    클래스 이름: Phase13OrderSubmissionAttempt
    기능: Phase 13 logical submit permit이 소비된 주문의 secret-free 최소 identity를 보존한다.
    작성 날짜: 2026/08/31
    """

    symbol: str
    side: OrderSide
    order_type: str
    intent_id: str
    submission_attempt: int
    attempted_at: datetime
    client_order_id: str

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 공개 주문 snapshot의 canonical identity, attempt와 UTC 시각을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        # Snapshot에는 raw parameter 대신 canonical Spot identity와 고정 MARKET type만 허용한다.
        if (
            not isinstance(self.symbol, str)
            or not self.symbol
            or self.symbol != self.symbol.strip().upper()
            or not self.symbol.isascii()
            or not self.symbol.isalnum()
        ):
            raise ValueError("symbol must be canonical uppercase ASCII")
        if not isinstance(self.side, OrderSide):
            raise TypeError("side must be an OrderSide")
        if self.order_type != "MARKET":
            raise ValueError("order_type must be MARKET")
        for field_name in ("intent_id", "client_order_id"):
            field_value = getattr(self, field_name)
            if (
                not isinstance(field_value, str)
                or not field_value
                or field_value != field_value.strip()
            ):
                raise ValueError(f"{field_name} must be non-empty canonical text")
        if isinstance(self.submission_attempt, bool) or not isinstance(
            self.submission_attempt,
            int,
        ):
            raise TypeError("submission_attempt must be an integer")
        if self.submission_attempt < 0:
            raise ValueError("submission_attempt must be a non-negative integer")
        if (
            not isinstance(self.attempted_at, datetime)
            or self.attempted_at.tzinfo is None
            or self.attempted_at.utcoffset() is None
            or self.attempted_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("attempted_at must be timezone-aware UTC")


@dataclass(frozen=True, slots=True)
class Phase13OrderSubmissionGuardSnapshot:
    """
    클래스 이름: Phase13OrderSubmissionGuardSnapshot
    기능: Phase 13 mutation 시작·차단 상태와 최대 두 logical submit identity를 불변 공개한다.
    작성 날짜: 2026/08/31
    """

    mutation_started: bool
    submissions_blocked: bool
    attempts: tuple[Phase13OrderSubmissionAttempt, ...]

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: guard boolean과 attempt tuple의 exact 상관관계를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        # Truthy 대체값이나 mutable collection이 failure finalizer의 판단을 흐리지 못하게 한다.
        if type(self.mutation_started) is not bool:
            raise TypeError("mutation_started must be a bool")
        if type(self.submissions_blocked) is not bool:
            raise TypeError("submissions_blocked must be a bool")
        if not isinstance(self.attempts, tuple) or any(
            type(attempt) is not Phase13OrderSubmissionAttempt
            for attempt in self.attempts
        ):
            raise TypeError(
                "attempts must be a Phase13OrderSubmissionAttempt tuple"
            )
        if len(self.attempts) > 2:
            raise ValueError("Phase 13 permits at most two logical submissions")
        if self.mutation_started is not bool(self.attempts):
            raise ValueError("mutation_started must match observed attempts")
        if len({attempt.client_order_id for attempt in self.attempts}) != len(
            self.attempts
        ):
            raise ValueError("Phase 13 client order IDs must be unique")


class BinanceRESTClient(Protocol):
    """
    클래스 이름: BinanceRESTClient
    기능: Spot Kline/account와 fake·testnet order 호출을 주입할 client 계약을 정의한다.
    작성 날짜: 2026/08/20
    """

    def get_klines(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int,
    ) -> object:
        """
        함수 이름: get_klines()
        기능: 공식 GET /api/v3/klines payload를 반환한다.
        인자: symbol -> 조회할 Binance Spot symbol
            interval -> 공식 Kline interval 문자열
            limit -> 반환할 최대 Kline 개수
        반환값: JSON으로 해석된 Binance REST payload
        작성 날짜: 2026/08/20
        """
        ...

    def get_account(self) -> object:
        """
        함수 이름: get_account()
        기능: 공식 GET /api/v3/account payload를 반환한다.
        인자: 없음
        반환값: JSON으로 해석된 Binance REST account payload
        작성 날짜: 2026/08/21
        """
        ...

    def get_account_commission(self, *, symbol: str) -> object:
        """
        함수 이름: get_account_commission()
        기능: 공식 GET /api/v3/account/commission payload를 반환한다.
        인자: symbol -> 수수료 할인 설정을 조회할 Spot symbol
        반환값: JSON으로 해석된 Binance commission payload
        작성 날짜: 2026/08/24
        """
        ...

    def get_account_filters(self, *, symbol: str) -> object:
        """
        함수 이름: get_account_filters()
        기능: 공식 signed GET /api/v3/myFilters payload를 반환한다.
        인자: symbol -> 계정 관련 filter를 조회할 Spot symbol
        반환값: JSON으로 해석된 Binance myFilters payload
        작성 날짜: 2026/08/31
        """
        ...

    def has_any_exchange_open_orders(self) -> bool:
        """
        함수 이름: has_any_exchange_open_orders()
        기능: symbol을 생략한 signed openOrders snapshot의 non-empty 여부를 반환한다.
        인자: 없음
        반환값: account 전체 open order가 하나라도 있으면 True
        작성 날짜: 2026/08/31
        """
        ...

    def has_any_exchange_open_order_lists(self) -> bool:
        """
        함수 이름: has_any_exchange_open_order_lists()
        기능: signed openOrderList snapshot의 non-empty 여부를 반환한다.
        인자: 없음
        반환값: account 전체 open order list가 하나라도 있으면 True
        작성 날짜: 2026/08/31
        """
        ...

    def fetch_reference_price(
        self,
        *,
        symbol: str,
    ) -> ReferencePrice:
        """
        함수 이름: fetch_reference_price()
        기능: 공식 GET /api/v3/referencePrice를 엄격 DTO로 반환한다.
        인자: symbol -> 조회할 Binance Spot symbol
        반환값: 요청 symbol에 결속된 non-null ReferencePrice
        작성 날짜: 2026/08/31
        """
        ...

    def fetch_symbol_trading_rules(
        self,
        *,
        symbol: str,
    ) -> SymbolTradingRules:
        """
        함수 이름: fetch_symbol_trading_rules()
        기능: 공식 exchangeInfo를 새로 조회한 엄격 Spot symbol rule을 반환한다.
        인자: symbol -> 조회할 Binance Spot symbol
        반환값: raw payload가 아닌 SymbolTradingRules
        작성 날짜: 2026/08/31
        """
        ...

    def get_order_preparation_filter_evidence(
        self,
        *,
        client_order_id: str,
    ) -> OrderPreparationFilterEvidence | None:
        """
        함수 이름: get_order_preparation_filter_evidence()
        기능: 성공한 prepare의 public filter provenance를 credential 없는 immutable DTO로 반환한다.
        인자: client_order_id -> 준비된 application Order identity
        반환값: exact OrderPreparationFilterEvidence 또는 prepare가 없으면 None
        작성 날짜: 2026/08/31
        """
        ...

    def get_order_submission_attempt_evidence(
        self,
        *,
        client_order_id: str,
    ) -> OrderSubmissionAttemptEvidence | None:
        """
        함수 이름: get_order_submission_attempt_evidence()
        기능: 실제 REST POST 시작 시각을 credential 없는 immutable DTO로 반환한다.
        인자: client_order_id -> 제출을 시작한 application Order identity
        반환값: exact OrderSubmissionAttemptEvidence 또는 POST 시작 전이면 None
        작성 날짜: 2026/08/31
        """
        ...

    def block_phase13_order_submissions(self) -> None:
        """
        함수 이름: block_phase13_order_submissions()
        기능: Phase 13 failure 이후 모든 후속 logical submit permit을 원자 차단한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        ...

    def get_phase13_order_submission_guard_snapshot(
        self,
    ) -> Phase13OrderSubmissionGuardSnapshot:
        """
        함수 이름: get_phase13_order_submission_guard_snapshot()
        기능: Phase 13 mutation 시작·차단과 secret-free attempt snapshot을 반환한다.
        인자: 없음
        반환값: immutable Phase13OrderSubmissionGuardSnapshot
        작성 날짜: 2026/08/31
        """
        ...

    def submit_order(self, *, order: Order) -> OrderResult:
        """
        함수 이름: submit_order()
        기능: Phase 8 fake client에서 정규화된 주문 제출 결과를 반환한다.
        인자: order -> 원래 의도와 제출 수량을 보존한 Order
        반환값: 정규화된 OrderResult
        작성 날짜: 2026/08/22
        """
        ...

    def query_order_result(self, *, order: Order) -> OrderResult:
        """
        함수 이름: query_order_result()
        기능: 새 주문 없이 같은 client 또는 exchange order ID의 최신 결과를 반환한다.
        인자: order -> 조회 식별자를 소유한 기존 Order
        반환값: 정규화된 OrderResult
        작성 날짜: 2026/08/22
        """
        ...

    def cancel_order(self, *, order: Order) -> OrderResult:
        """
        함수 이름: cancel_order()
        기능: 같은 주문 ID를 취소하고 취소 시점까지의 fill이 포함된 결과를 반환한다.
        인자: order -> 취소할 기존 Order
        반환값: 정규화된 OrderResult
        작성 날짜: 2026/08/22
        """
        ...

    def prepare_order(self, *, order: Order) -> Order:
        """
        함수 이름: prepare_order()
        기능: 최신 symbol filter로 제출 수량을 내림 정규화한 Order를 반환한다.
        인자: order -> filter 전 요청 수량을 보존한 Order
        반환값: 요청 수량과 identity를 유지한 제출용 Order
        작성 날짜: 2026/08/22
        """
        ...

    def list_open_order_results(self, *, symbol: str) -> tuple[OrderResult, ...]:
        """
        함수 이름: list_open_order_results()
        기능: 상품의 현재 미결 주문을 정규화된 결과 tuple로 반환한다.
        인자: symbol -> 조회할 Binance Spot symbol
        반환값: 현재 미결 OrderResult tuple
        작성 날짜: 2026/08/22
        """
        ...

    def list_all_open_order_results(
        self,
        *,
        symbol: str,
    ) -> tuple[OrderResult, ...]:
        """
        함수 이름: list_all_open_order_results()
        기능: 안전 preflight에 사용할 모든 client ID의 상품 미결 주문을 반환한다.
        인자: symbol -> 조회할 Binance Spot symbol
        반환값: 현재 전체 미결 OrderResult tuple
        작성 날짜: 2026/08/31
        """
        ...

    def list_recent_order_results(
        self,
        *,
        symbol: str,
        limit: int = 100,
    ) -> tuple[OrderResult, ...]:
        """
        함수 이름: list_recent_order_results()
        기능: 재시작 재조정에 사용할 최근 주문 결과를 시간순으로 반환한다.
        인자: symbol -> 조회할 Binance Spot symbol
            limit -> 반환할 최근 주문 최대 개수
        반환값: 최근 OrderResult tuple
        작성 날짜: 2026/08/22
        """
        ...

    def list_all_recent_order_results(
        self,
        *,
        symbol: str,
        limit: int = 100,
    ) -> tuple[OrderResult, ...]:
        """
        함수 이름: list_all_recent_order_results()
        기능: 격리 delta 검증에 사용할 모든 client ID의 최근 주문을 반환한다.
        인자: symbol -> 조회할 Binance Spot symbol
            limit -> 반환할 최근 주문 최대 개수
        반환값: 최근 전체 OrderResult tuple
        작성 날짜: 2026/08/31
        """
        ...


def _utc_now() -> datetime:
    """
    함수 이름: _utc_now()
    기능: REST Kline 확정 여부 계산에 사용할 현재 UTC 시각을 반환한다.
    인자: 없음
    반환값: 현재 UTC datetime
    작성 날짜: 2026/08/20
    """
    return datetime.now(timezone.utc)


def _normalize_symbol(symbol: object) -> str:
    """
    함수 이름: _normalize_symbol()
    기능: 호출자의 Binance symbol을 공백 없는 ASCII 대문자 형식으로 정규화한다.
    인자: symbol -> 정규화할 symbol 값
    반환값: 대문자로 정규화된 symbol
    작성 날짜: 2026/08/20
    """
    if not isinstance(symbol, str):
        raise TypeError("symbol must be a string")

    normalized_symbol = symbol.strip().upper()
    if (
        not normalized_symbol
        or not normalized_symbol.isascii()
        or not normalized_symbol.isalnum()
    ):
        raise ValueError(
            "symbol must contain only ASCII letters and digits"
        )

    return normalized_symbol


def _normalize_utc_datetime(value: object, field_name: str) -> datetime:
    """
    함수 이름: _normalize_utc_datetime()
    기능: 주입된 clock 값이 UTC aware datetime인지 확인하고 UTC로 정규화한다.
    인자: value -> 검증할 datetime 값
        field_name -> 오류에 표시할 필드 이름
    반환값: timezone.utc로 정규화된 datetime
    작성 날짜: 2026/08/20
    """
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must use UTC")

    return value.astimezone(timezone.utc)


def _read_non_negative_integer(value: object, field_name: str) -> int:
    """
    함수 이름: _read_non_negative_integer()
    기능: Binance timestamp와 count 필드가 bool이 아닌 0 이상 정수인지 검증한다.
    인자: value -> 검증할 payload 값
        field_name -> 오류에 표시할 필드 이름
    반환값: 검증된 정수
    작성 날짜: 2026/08/20
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{field_name} must not be negative")

    return value


def _read_decimal_string(value: object, field_name: str) -> Decimal:
    """
    함수 이름: _read_decimal_string()
    기능: Binance decimal 문자열을 float 변환 없이 유한 Decimal로 해석한다.
    인자: value -> 해석할 payload 값
        field_name -> 오류에 표시할 필드 이름
    반환값: 검증된 유한 Decimal
    작성 날짜: 2026/08/20
    """
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{field_name} must be a decimal string")

    try:
        decimal_value = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(
            f"{field_name} must contain a valid decimal"
        ) from error

    if not decimal_value.is_finite():
        raise ValueError(f"{field_name} must contain a finite decimal")

    return decimal_value


def _milliseconds_to_utc(value: object, field_name: str) -> datetime:
    """
    함수 이름: _milliseconds_to_utc()
    기능: Binance Unix millisecond를 float 없이 UTC datetime으로 변환한다.
    인자: value -> 변환할 Unix millisecond
        field_name -> 오류에 표시할 필드 이름
    반환값: 변환된 UTC datetime
    작성 날짜: 2026/08/20
    """
    milliseconds = _read_non_negative_integer(value, field_name)

    try:
        return _UNIX_EPOCH + timedelta(milliseconds=milliseconds)
    except OverflowError as error:
        raise ValueError(f"{field_name} is outside datetime range") from error


def _utc_to_milliseconds(value: datetime) -> int:
    """
    함수 이름: _utc_to_milliseconds()
    기능: UTC datetime을 float timestamp 없이 Unix millisecond로 변환한다.
    인자: value -> 변환할 UTC datetime
    반환값: Unix millisecond 정수
    작성 날짜: 2026/08/20
    """
    utc_value = _normalize_utc_datetime(value, "clock result")
    elapsed = utc_value - _UNIX_EPOCH

    return (
        elapsed.days * 86_400_000
        + elapsed.seconds * 1_000
        + elapsed.microseconds // 1_000
    )


def _validate_rest_metadata(row: list[object] | tuple[object, ...]) -> None:
    """
    함수 이름: _validate_rest_metadata()
    기능: 사용하지 않는 공식 REST Kline metadata 필드도 schema대로 검증한다.
    인자: row -> 공식 12필드 Kline row
    반환값: 없음
    작성 날짜: 2026/08/20
    """
    for field_index in (7, 9, 10):
        decimal_value = _read_decimal_string(
            row[field_index],
            f"row[{field_index}]",
        )
        if decimal_value < Decimal("0"):
            raise ValueError(f"row[{field_index}] must not be negative")

    _read_non_negative_integer(row[8], "row[8]")
    if not isinstance(row[11], str):
        raise TypeError("row[11] must be a string")


def _validate_kline_time_range(
    open_time_milliseconds: int,
    close_time_milliseconds: int,
    interval: Interval,
) -> None:
    """
    함수 이름: _validate_kline_time_range()
    기능: 기본 UTC Kline의 시작·종료 시각이 요청 interval과 일치하는지 검증한다.
    인자: open_time_milliseconds -> 봉 시작 Unix millisecond
        close_time_milliseconds -> 봉 종료 Unix millisecond
        interval -> REST 요청에 사용한 canonical interval
    반환값: 없음
    작성 날짜: 2026/08/20
    """
    interval_milliseconds = _INTERVAL_MILLISECONDS_BY_INTERVAL[interval]
    if open_time_milliseconds % interval_milliseconds != 0:
        raise ValueError("Binance REST Kline open time is not UTC-aligned")

    expected_close_time = (
        open_time_milliseconds + interval_milliseconds - 1
    )
    if close_time_milliseconds != expected_close_time:
        raise ValueError(
            "Binance REST Kline close time does not match its interval"
        )


def _parse_rest_kline_row(
    row: object,
    symbol: str,
    interval: Interval,
    current_time_milliseconds: int,
) -> Kline:
    """
    함수 이름: _parse_rest_kline_row()
    기능: 공식 Binance 12필드 REST row를 내부 Kline으로 정규화한다.
    인자: row -> JSON에서 해석한 단일 Kline row
        symbol -> 조회한 정규화 symbol
        interval -> 조회한 canonical interval
        current_time_milliseconds -> 확정 여부 기준 Unix millisecond
    반환값: 정규화된 불변 Kline
    작성 날짜: 2026/08/20
    """
    if not isinstance(row, (list, tuple)) or len(row) != 12:
        raise ValueError("Binance REST kline row must contain 12 fields")

    open_time_milliseconds = _read_non_negative_integer(row[0], "row[0]")
    close_time_milliseconds = _read_non_negative_integer(row[6], "row[6]")
    if close_time_milliseconds < open_time_milliseconds:
        raise ValueError("Binance REST kline closes before it opens")
    _validate_kline_time_range(
        open_time_milliseconds,
        close_time_milliseconds,
        interval,
    )

    _validate_rest_metadata(row)

    return Kline(
        symbol=symbol,
        interval=interval,
        open_time=_milliseconds_to_utc(row[0], "row[0]"),
        open=_read_decimal_string(row[1], "row[1]"),
        high=_read_decimal_string(row[2], "row[2]"),
        low=_read_decimal_string(row[3], "row[3]"),
        close=_read_decimal_string(row[4], "row[4]"),
        volume=_read_decimal_string(row[5], "row[5]"),
        closed=close_time_milliseconds < current_time_milliseconds,
    )


def _normalize_account_asset(asset: object) -> str:
    """
    함수 이름: _normalize_account_asset()
    기능: Phase 4에서 평가를 지원하는 ETH asset 입력을 정규화하고 검증한다.
    인자: asset -> 호출자가 전달한 기준 asset
    반환값: 정규화된 ETH asset 이름
    작성 날짜: 2026/08/21
    """
    if not isinstance(asset, str):
        raise TypeError("asset must be a string")

    normalized_asset = asset.strip().upper()
    if normalized_asset != SUPPORTED_VALUATION_ASSET:
        raise ValueError("account valuation supports only ETH")

    return normalized_asset


def _parse_account_balance(payload: object) -> AssetBalance:
    """
    함수 이름: _parse_account_balance()
    기능: 공식 account balances 항목을 내부 AssetBalance로 정규화한다.
    인자: payload -> balances 배열의 단일 asset object
    반환값: 정규화된 불변 AssetBalance
    작성 날짜: 2026/08/21
    """
    if not isinstance(payload, Mapping):
        raise TypeError("account balance must be an object")

    asset = payload.get("asset")
    if not isinstance(asset, str):
        raise TypeError("account balance asset must be a string")

    free = _read_decimal_string(payload.get("free"), "balance.free")
    locked = _read_decimal_string(payload.get("locked"), "balance.locked")
    if free < Decimal("0") or locked < Decimal("0"):
        raise ValueError("account balances must not be negative")

    return AssetBalance(
        asset=asset,
        free=free,
        locked=locked,
    )


def _parse_account_snapshot(
    payload: object,
    valuation_asset: str,
) -> AccountSnapshot:
    """
    함수 이름: _parse_account_snapshot()
    기능: 거래 가능한 공식 Spot account 응답을 가격 정보 없는 전체 AccountSnapshot으로 변환한다.
    인자: payload -> JSON으로 해석한 GET /api/v3/account 응답
        valuation_asset -> 응답에 반드시 포함되어야 할 ETH asset
    반환값: 정규화된 전체 AccountSnapshot
    작성 날짜: 2026/08/21
    """
    if not isinstance(payload, Mapping):
        raise TypeError("Binance account payload must be an object")
    if payload.get("accountType") != "SPOT":
        raise ValueError("Binance accountType must be SPOT")

    # 주문 가능 여부는 truthy 변환 없이 공식 boolean을 요구하고 비활성 계정은 startup에서 닫는다.
    can_trade = payload.get("canTrade")
    if type(can_trade) is not bool:
        raise TypeError("Binance account canTrade must be a bool")
    if not can_trade:
        raise ValueError("Binance Spot account must permit trading")

    raw_balances = payload.get("balances")
    if not isinstance(raw_balances, (list, tuple)):
        raise TypeError("Binance account balances must be an array")

    balances = tuple(
        _parse_account_balance(raw_balance)
        for raw_balance in raw_balances
    )
    if valuation_asset not in {
        balance.asset
        for balance in balances
    }:
        raise ValueError("Binance account payload is missing ETH balance")

    return AccountSnapshot(
        balances=balances,
        updated_at=_milliseconds_to_utc(
            payload.get("updateTime"),
            "updateTime",
        ),
        is_full_snapshot=True,
    )


def _parse_commission_rate_group(
    payload: object,
    field_name: str,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """
    함수 이름: _parse_commission_rate_group()
    기능: 공식 commission 유형의 maker·taker·buyer·seller 비음수 Decimal을 엄격히 해석한다.
    인자: payload -> standard, special 또는 tax commission object
        field_name -> 오류에 사용할 공식 상위 필드 이름
    반환값: maker, taker, buyer, seller 순서의 Decimal tuple
    작성 날짜: 2026/08/24
    """
    if not isinstance(payload, Mapping):
        raise TypeError(f"Binance {field_name} must be an object")

    # 일부 필드 누락을 0으로 추측하지 않고 공식 네 비율을 모두 요구한다.
    rates = tuple(
        _read_decimal_string(
            payload.get(rate_name),
            f"{field_name}.{rate_name}",
        )
        for rate_name in ("maker", "taker", "buyer", "seller")
    )
    if any(rate < Decimal("0") for rate in rates):
        raise ValueError(f"{field_name} rates must not be negative")

    return rates


def _sum_market_buy_commission_rate(
    rates: tuple[Decimal, Decimal, Decimal, Decimal],
) -> Decimal:
    """
    함수 이름: _sum_market_buy_commission_rate()
    기능: MARKET BUY에 적용되는 공식 taker와 buyer 비율을 Decimal128로 합산한다.
    인자: rates -> maker, taker, buyer, seller 순서의 commission tuple
    반환값: taker + buyer Decimal 비율
    작성 날짜: 2026/08/24
    """
    # 수신 base 수량 수수료에는 order role의 taker와 side의 buyer 비율이 함께 적용된다.
    with localcontext() as decimal_context:
        decimal_context.prec = 34
        decimal_context.rounding = ROUND_HALF_EVEN
        return rates[1] + rates[2]


def _parse_commission_discount_policy(
    payload: object,
    expected_symbol: str,
) -> CommissionDiscountPolicy:
    """
    함수 이름: _parse_commission_discount_policy()
    기능: 공식 account commission 응답의 할인과 MARKET BUY 수신 자산 비율을 엄격 정규화한다.
    인자: payload -> GET /api/v3/account/commission JSON object
        expected_symbol -> 요청에 사용한 canonical Spot symbol
    반환값: raw 수수료율을 노출하지 않는 CommissionDiscountPolicy
    작성 날짜: 2026/08/24
    """
    if not isinstance(payload, Mapping):
        raise TypeError("Binance commission payload must be an object")
    if payload.get("symbol") != expected_symbol:
        raise ValueError("Binance commission symbol does not match request")

    # 공식 네 비율을 먼저 보존해 Testnet null asset compatibility도 전체 12개로 검증한다.
    standard_rates = _parse_commission_rate_group(
        payload.get("standardCommission"),
        "standardCommission",
    )
    special_rates = _parse_commission_rate_group(
        payload.get("specialCommission"),
        "specialCommission",
    )
    tax_rates = _parse_commission_rate_group(
        payload.get("taxCommission"),
        "taxCommission",
    )

    # 공식 FAQ의 MARKET BUY 산식에 필요한 세 유형의 taker+buyer 비율을 각각 계산한다.
    standard_market_buy_rate = _sum_market_buy_commission_rate(
        standard_rates
    )
    special_market_buy_rate = _sum_market_buy_commission_rate(special_rates)
    tax_market_buy_rate = _sum_market_buy_commission_rate(tax_rates)

    # 제3 자산 가능성을 결정하는 공식 discount object와 네 필드를 모두 요구한다.
    discount_payload = payload.get("discount")
    if not isinstance(discount_payload, Mapping):
        raise TypeError("Binance commission discount must be an object")
    enabled_for_account = discount_payload.get("enabledForAccount")
    enabled_for_symbol = discount_payload.get("enabledForSymbol")
    if type(enabled_for_account) is not bool:
        raise TypeError("discount.enabledForAccount must be a bool")
    if type(enabled_for_symbol) is not bool:
        raise TypeError("discount.enabledForSymbol must be a bool")
    if "discountAsset" not in discount_payload:
        raise TypeError("discount.discountAsset is required")
    discount_asset = discount_payload.get("discountAsset")
    if discount_asset is not None and not isinstance(discount_asset, str):
        raise TypeError("discount.discountAsset must be a string or null")
    discount_rate = _read_decimal_string(
        discount_payload.get("discount"),
        "discount.discount",
    )

    # 공식 schema 밖의 Testnet null은 12개 수수료와 할인율이 모두 0일 때만 무수수료 부재로 받는다.
    if discount_asset is None and (
        discount_rate != Decimal("0")
        or any(
            rate != Decimal("0")
            for rates in (standard_rates, special_rates, tax_rates)
            for rate in rates
        )
    ):
        raise ValueError(
            "null discountAsset requires every commission rate to be zero"
        )

    # Dataclass가 canonical asset과 공식 0~1 할인율 범위를 마지막으로 검증한다.
    return CommissionDiscountPolicy(
        symbol=expected_symbol,
        enabled_for_account=enabled_for_account,
        enabled_for_symbol=enabled_for_symbol,
        discount_asset=discount_asset,
        discount_rate=discount_rate,
        standard_market_buy_rate=standard_market_buy_rate,
        special_market_buy_rate=special_market_buy_rate,
        tax_market_buy_rate=tax_market_buy_rate,
    )


class APIGateway:
    """
    클래스 이름: APIGateway
    기능: Spot Kline/account와 fake·testnet order 응답을 canonical domain 타입으로 제한한다.
    작성 날짜: 2026/08/20
    """

    def __init__(
        self,
        rest_client: BinanceRESTClient,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 실제 I/O를 수행할 REST client와 UTC clock을 주입받는다.
        인자: rest_client -> 공식 Kline 또는 account payload를 반환할 REST client
            clock -> REST row 확정 여부를 계산할 UTC clock
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        # 주입 client가 Kline, account, public rule 또는 fake order operation을 하나 이상 갖는지 확인한다.
        provides_kline_operation = callable(
            getattr(rest_client, "get_klines", None)
        )
        provides_account_operation = callable(
            getattr(rest_client, "get_account", None)
        )
        provides_order_operation = callable(
            getattr(rest_client, "submit_order", None)
        )
        provides_rules_operation = callable(
            getattr(rest_client, "fetch_symbol_trading_rules", None)
        )
        provides_account_filters_operation = callable(
            getattr(rest_client, "get_account_filters", None)
        )
        provides_reference_price_operation = callable(
            getattr(rest_client, "fetch_reference_price", None)
        )
        if rest_client is None or not (
            provides_kline_operation
            or provides_account_operation
            or provides_order_operation
            or provides_rules_operation
            or provides_account_filters_operation
            or provides_reference_price_operation
        ):
            raise TypeError(
                "rest_client must provide a supported REST operation"
            )

        # 명시 clock이 없으면 UTC 기본 clock을 선택하고 호출 가능한 dependency만 보존한다.
        selected_clock = _utc_now if clock is None else clock
        if not callable(selected_clock):
            raise TypeError("clock must be callable")

        # 검증을 마친 REST port와 clock identity를 adapter 수명 동안 그대로 사용한다.
        self._rest_client = rest_client
        self._clock = selected_clock

    def load_all_klines(
        self,
        symbol: str,
        limit: int = DEFAULT_KLINE_LIMIT,
    ) -> dict[Interval, tuple[Kline, ...]]:
        """
        함수 이름: load_all_klines()
        기능: 네 공식 interval의 과거 봉을 모두 조회하고 내부 Kline으로 정규화한다.
        인자: symbol -> 조회할 Binance Spot symbol
            limit -> 주기별 조회 개수
        반환값: canonical interval별 Kline tuple mapping
        작성 날짜: 2026/08/20
        """
        get_klines = getattr(self._rest_client, "get_klines", None)
        if not callable(get_klines):
            raise TypeError("rest_client must provide get_klines")

        normalized_symbol = _normalize_symbol(symbol)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if not MINIMUM_KLINE_LIMIT <= limit <= MAXIMUM_KLINE_LIMIT:
            raise ValueError("limit must be between 1 and 1000")

        sweep_started = _utc_to_milliseconds(_normalize_utc_datetime(self._clock(), "clock result"))
        raw_payloads: dict[
            Interval,
            list[object] | tuple[object, ...],
        ] = {}

        for attempt in range(3):
            if attempt:
                sweep_started = _utc_to_milliseconds(self._clock())
            for interval in SUPPORTED_INTERVALS:
                payload = get_klines(
                    symbol=normalized_symbol, interval=interval.value, limit=limit,
                )
                if not isinstance(payload, (list, tuple)):
                    raise TypeError(f"Binance {interval.value} kline payload must be an array")
                raw_payloads[interval] = payload
            current_time_milliseconds = _utc_to_milliseconds(self._clock())
            # 경계를 가로지른 REST 조회는 서로 다른 기준 시각을 섞지 않고 다시 읽는다.
            stale_intervals = [
                interval for interval in SUPPORTED_INTERVALS
                if sweep_started // _INTERVAL_MILLISECONDS_BY_INTERVAL[interval]
                != current_time_milliseconds // _INTERVAL_MILLISECONDS_BY_INTERVAL[interval]
            ]
            if not stale_intervals:
                break
            if attempt == 2:
                raise ValueError("REST candle sweep remained stale across a boundary")

        normalized_klines: dict[Interval, tuple[Kline, ...]] = {}
        for interval in SUPPORTED_INTERVALS:
            normalized_klines[interval] = tuple(
                _parse_rest_kline_row(
                    row,
                    normalized_symbol,
                    interval,
                    current_time_milliseconds,
                )
                for row in raw_payloads[interval]
            )

        return normalized_klines

    def fetch_account_snapshot(
        self,
        asset: str = SUPPORTED_VALUATION_ASSET,
    ) -> AccountSnapshot:
        """
        함수 이름: fetch_account_snapshot()
        기능: 공식 Spot account 응답의 전체 잔액과 갱신 시각을 정규화한다.
        인자: asset -> MarketSnapshot 가격으로 평가할 기준 asset
        반환값: 가격을 포함하지 않는 전체 AccountSnapshot
        작성 날짜: 2026/08/21
        """
        normalized_asset = _normalize_account_asset(asset)
        get_account = getattr(self._rest_client, "get_account", None)
        if not callable(get_account):
            raise TypeError("rest_client must provide get_account")

        return _parse_account_snapshot(
            get_account(),
            normalized_asset,
        )

    def fetch_commission_discount_policy(
        self,
        symbol: str,
    ) -> CommissionDiscountPolicy:
        """
        함수 이름: fetch_commission_discount_policy()
        기능: signed account commission 응답을 수수료 할인 가능성 정책으로 정규화한다.
        인자: symbol -> 조회할 Binance Spot symbol
        반환값: credential과 raw 전체 payload가 없는 할인 정책
        작성 날짜: 2026/08/24
        """
        normalized_symbol = _normalize_symbol(symbol)
        get_account_commission = getattr(
            self._rest_client,
            "get_account_commission",
            None,
        )
        if not callable(get_account_commission):
            raise TypeError(
                "rest_client must provide get_account_commission"
            )

        # REST client에는 canonical symbol만 전달하고 raw response는 이 adapter 안에서 소비한다.
        return _parse_commission_discount_policy(
            get_account_commission(symbol=normalized_symbol),
            normalized_symbol,
        )

    def fetch_symbol_trading_rules(
        self,
        symbol: str,
    ) -> SymbolTradingRules:
        """
        함수 이름: fetch_symbol_trading_rules()
        기능: public REST port의 최신 exchangeInfo rule을 domain 타입과 symbol에 결속해 반환한다.
        인자: symbol -> 조회할 Binance Spot symbol
        반환값: canonical symbol에 해당하는 SymbolTradingRules
        작성 날짜: 2026/08/31
        """
        normalized_symbol = _normalize_symbol(symbol)
        fetch_symbol_trading_rules = getattr(
            self._rest_client,
            "fetch_symbol_trading_rules",
            None,
        )
        if not callable(fetch_symbol_trading_rules):
            raise TypeError(
                "rest_client must provide fetch_symbol_trading_rules"
            )

        # Gateway는 raw exchangeInfo나 cache를 보지 않고 REST port가 새로 해석한 공개 DTO만 검증한다.
        rules = fetch_symbol_trading_rules(symbol=normalized_symbol)
        if type(rules) is not SymbolTradingRules:
            raise TypeError(
                "fetch_symbol_trading_rules must return SymbolTradingRules"
            )
        if rules.symbol != normalized_symbol:
            raise ValueError("symbol trading rules do not match request")

        return rules

    def fetch_account_asset_filters(
        self,
        symbol: str,
    ) -> tuple[AccountAssetFilter, ...]:
        """
        함수 이름: fetch_account_asset_filters()
        기능: signed myFilters raw 응답을 strict MAX_ASSET tuple로 정규화한다.
        인자: symbol -> 조회할 Binance Spot symbol
        반환값: 해당 계정과 symbol에 적용되는 AccountAssetFilter tuple
        작성 날짜: 2026/08/31
        """
        normalized_symbol = _normalize_symbol(symbol)
        get_account_filters = getattr(
            self._rest_client,
            "get_account_filters",
            None,
        )
        if not callable(get_account_filters):
            raise TypeError("rest_client must provide get_account_filters")

        # Raw USER_DATA payload는 Gateway 밖으로 내보내지 않고 credential-free DTO로만 축약한다.
        return parse_account_asset_filters(
            get_account_filters(symbol=normalized_symbol),
            normalized_symbol,
        )  # 다른 account scope도 strict parser를 통과한 MAX_ASSET projection만 반환한다.

    def fetch_account_relevant_filters(
        self,
        symbol: str,
    ) -> AccountRelevantFilters:
        """
        함수 이름: fetch_account_relevant_filters()
        기능: signed myFilters 세 scope를 raw JSON 없는 strict composite DTO로 정규화한다.
        인자: symbol -> 조회할 Binance Spot symbol
        반환값: 해당 account와 symbol의 AccountRelevantFilters
        작성 날짜: 2026/08/31
        """
        normalized_symbol = _normalize_symbol(symbol)
        get_account_filters = getattr(
            self._rest_client,
            "get_account_filters",
            None,
        )
        if not callable(get_account_filters):
            raise TypeError("rest_client must provide get_account_filters")

        # 세 scope를 함께 parse해 non-empty symbol/exchange filter도 type별로 검증한다.
        return parse_account_relevant_filters(
            get_account_filters(symbol=normalized_symbol),
            normalized_symbol,
        )  # Raw mapping 대신 세 scope가 결속된 immutable DTO만 반환한다.

    def has_any_exchange_open_orders(self) -> bool:
        """
        함수 이름: has_any_exchange_open_orders()
        기능: all-symbol signed openOrders의 non-empty 여부를 exact bool로 반환한다.
        인자: 없음
        반환값: account 전체 open order가 하나라도 있으면 True
        작성 날짜: 2026/08/31
        """
        # Port capability와 반환 type을 함께 검증해 누락·malformed 상태를 empty로 완화하지 않는다.
        read_open_state = getattr(
            self._rest_client,
            "has_any_exchange_open_orders",
            None,
        )
        if not callable(read_open_state):
            raise TypeError(
                "rest_client must provide has_any_exchange_open_orders"
            )
        has_open_orders = read_open_state()
        if type(has_open_orders) is not bool:
            raise TypeError("exchange open order state must be a bool")

        return has_open_orders  # Order mapping과 ID는 Gateway 경계 밖으로 내보내지 않는다.

    def has_any_exchange_open_order_lists(self) -> bool:
        """
        함수 이름: has_any_exchange_open_order_lists()
        기능: signed openOrderList의 non-empty 여부를 exact bool로 반환한다.
        인자: 없음
        반환값: account 전체 open order list가 하나라도 있으면 True
        작성 날짜: 2026/08/31
        """
        # 전용 order-list reader의 존재와 exact bool 결과를 각각 확인해 fail-closed로 전달한다.
        read_open_list_state = getattr(
            self._rest_client,
            "has_any_exchange_open_order_lists",
            None,
        )
        if not callable(read_open_list_state):
            raise TypeError(
                "rest_client must provide has_any_exchange_open_order_lists"
            )
        has_open_order_lists = read_open_list_state()
        if type(has_open_order_lists) is not bool:
            raise TypeError("exchange open order list state must be a bool")

        return has_open_order_lists  # List ID나 child order raw payload는 반환하지 않는다.

    def fetch_reference_price(
        self,
        symbol: str,
    ) -> ReferencePrice:
        """
        함수 이름: fetch_reference_price()
        기능: public REST port의 non-null reference price를 exact DTO와 symbol에 결속한다.
        인자: symbol -> 조회할 Binance Spot symbol
        반환값: canonical symbol에 해당하는 ReferencePrice
        작성 날짜: 2026/08/31
        """
        normalized_symbol = _normalize_symbol(symbol)
        fetch_reference_price = getattr(
            self._rest_client,
            "fetch_reference_price",
            None,
        )
        if not callable(fetch_reference_price):
            raise TypeError("rest_client must provide fetch_reference_price")

        # REST parser의 exact DTO만 허용하고 요청하지 않은 symbol 응답은 별도로 차단한다.
        reference_price = fetch_reference_price(symbol=normalized_symbol)
        if type(reference_price) is not ReferencePrice:
            raise TypeError(
                "fetch_reference_price must return ReferencePrice"
            )
        if reference_price.symbol != normalized_symbol:
            raise ValueError("reference price does not match request")

        return reference_price  # 요청 symbol과 일치한 exact DTO만 caller에 전달한다.

    def get_order_preparation_filter_evidence(
        self,
        client_order_id: str,
    ) -> OrderPreparationFilterEvidence | None:
        """
        함수 이름: get_order_preparation_filter_evidence()
        기능: REST port가 보존한 submit-time public filter evidence를 identity와 타입에 결속한다.
        인자: client_order_id -> 준비된 application Order identity
        반환값: immutable filter evidence 또는 해당 prepare가 없으면 None
        작성 날짜: 2026/08/31
        """
        if (
            not isinstance(client_order_id, str)
            or not client_order_id
            or client_order_id != client_order_id.strip()
        ):
            raise ValueError("client_order_id must be non-empty canonical text")
        get_filter_evidence = getattr(
            self._rest_client,
            "get_order_preparation_filter_evidence",
            None,
        )
        if not callable(get_filter_evidence):
            raise TypeError(
                "rest_client must provide get_order_preparation_filter_evidence"
            )

        # Gateway는 raw response나 mutable cache 대신 한 client ID에 이미 고정된 frozen DTO만 공개한다.
        evidence = get_filter_evidence(client_order_id=client_order_id)
        if evidence is None:
            return None
        if type(evidence) is not OrderPreparationFilterEvidence:
            raise TypeError(
                "filter evidence must be an OrderPreparationFilterEvidence"
            )
        if evidence.client_order_id != client_order_id:
            raise ValueError("filter evidence does not match requested order")

        return evidence

    def get_order_submission_attempt_evidence(
        self,
        client_order_id: str,
    ) -> OrderSubmissionAttemptEvidence | None:
        """
        함수 이름: get_order_submission_attempt_evidence()
        기능: REST port의 submission-start evidence를 요청 client ID와 exact 타입에 결속한다.
        인자: client_order_id -> 제출을 시작한 application Order identity
        반환값: immutable submission evidence 또는 POST 시작 전이면 None
        작성 날짜: 2026/08/31
        """
        if (
            not isinstance(client_order_id, str)
            or not client_order_id
            or client_order_id != client_order_id.strip()
        ):
            raise ValueError("client_order_id must be non-empty canonical text")
        get_submission_evidence = getattr(
            self._rest_client,
            "get_order_submission_attempt_evidence",
            None,
        )
        if not callable(get_submission_evidence):
            raise TypeError(
                "rest_client must provide get_order_submission_attempt_evidence"
            )

        # POST raw request가 아니라 server-aligned time과 correlation만 가진 frozen DTO를 검증한다.
        evidence = get_submission_evidence(client_order_id=client_order_id)
        if evidence is None:
            return None
        if type(evidence) is not OrderSubmissionAttemptEvidence:
            raise TypeError(
                "submission evidence must be an OrderSubmissionAttemptEvidence"
            )
        if evidence.client_order_id != client_order_id:
            raise ValueError("submission evidence does not match requested order")

        return evidence

    def block_phase13_order_submissions(self) -> None:
        """
        함수 이름: block_phase13_order_submissions()
        기능: actual harness failure 뒤 REST port의 Phase 13 submit permit을 원자 폐쇄한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        block_submissions = getattr(
            self._rest_client,
            "block_phase13_order_submissions",
            None,
        )
        if not callable(block_submissions):
            raise TypeError(
                "rest_client must provide block_phase13_order_submissions"
            )

        # Gateway는 raw client state를 읽지 않고 단방향 fail-closed operation만 위임한다.
        block_submissions()

    def get_phase13_order_submission_guard_snapshot(
        self,
    ) -> Phase13OrderSubmissionGuardSnapshot:
        """
        함수 이름: get_phase13_order_submission_guard_snapshot()
        기능: REST port의 Phase 13 mutation·차단 상태를 secret-free frozen DTO로 반환한다.
        인자: 없음
        반환값: exact Phase13OrderSubmissionGuardSnapshot
        작성 날짜: 2026/08/31
        """
        get_guard_snapshot = getattr(
            self._rest_client,
            "get_phase13_order_submission_guard_snapshot",
            None,
        )
        if not callable(get_guard_snapshot):
            raise TypeError(
                "rest_client must provide get_phase13_order_submission_guard_snapshot"
            )

        # Exact DTO 검증으로 delegate가 raw request mapping을 공개하는 fallback을 허용하지 않는다.
        snapshot = get_guard_snapshot()
        if type(snapshot) is not Phase13OrderSubmissionGuardSnapshot:
            raise TypeError(
                "guard snapshot must be a Phase13OrderSubmissionGuardSnapshot"
            )

        return snapshot

    def submit_order(self, order: Order) -> OrderResult:
        """
        함수 이름: submit_order()
        기능: 주입 REST port에 Order를 한 번 제출하고 정규화 결과만 반환한다.
        인자: order -> 제출할 Order aggregate
        반환값: client가 반환한 검증된 OrderResult
        작성 날짜: 2026/08/22
        """
        # Fake와 실제 adapter 모두 raw payload가 아닌 동일 domain Order 계약만 받는다.
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")
        submit_order = getattr(self._rest_client, "submit_order", None)
        if not callable(submit_order):
            raise TypeError("rest_client must provide submit_order")

        # Adapter 밖으로 raw dict가 새지 않도록 결과 타입과 client ID 상관관계를 확인한다.
        result = submit_order(order=order)
        return self._validate_order_result(order, result)

    def prepare_order(self, order: Order) -> Order:
        """
        함수 이름: prepare_order()
        기능: 실제 client의 symbol filter를 적용하되 fake client에서는 원 Order를 보존한다.
        인자: order -> filter 전 요청 수량을 가진 Order
        반환값: identity와 원 요청량을 보존한 제출용 Order
        작성 날짜: 2026/08/22
        """
        # 실제 Binance client의 filter operation은 선택적으로 호출해 기존 fake를 보존한다.
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")
        prepare_order = getattr(self._rest_client, "prepare_order", None)
        if not callable(prepare_order):
            return order  # Phase 8 fake fixture에는 거래소 filter가 없으므로 원 identity를 유지한다.

        # Client가 같은 mutable Order를 반환해도 원 identity와 수량 상한을 검증하도록 먼저 복사한다.
        immutable_identity = (
            "intent_id",
            "client_order_id",
            "submission_attempt",
            "symbol",
            "side",
            "strategy",
            "regime_type",
            "requested_quantity",
            "market_price_at_decision",
            "exit_reason",
            "exit_pct_b_at_intent",
        )
        original_identity = tuple(
            getattr(order, field_name) for field_name in immutable_identity
        )
        original_submitted_quantity = order.submitted_quantity
        prepared_order = prepare_order(order=order)
        if not isinstance(prepared_order, Order):
            raise TypeError("prepare_order must return an Order")
        if any(
            getattr(prepared_order, field_name) != original_value
            for field_name, original_value in zip(
                immutable_identity,
                original_identity,
                strict=True,
            )
        ):
            raise ValueError("prepared Order changed immutable order intent")
        if prepared_order.submitted_quantity > original_submitted_quantity:
            raise ValueError("prepared Order increased submitted quantity")

        return prepared_order  # Order domain 검증을 통과한 내림 수량만 Controller에 공개한다.

    def query_order_result(self, order: Order) -> OrderResult:
        """
        함수 이름: query_order_result()
        기능: 같은 Order 식별자로 상태와 fill을 조회하며 신규 제출을 절대 수행하지 않는다.
        인자: order -> 이미 제출된 Order aggregate
        반환값: client가 반환한 검증된 OrderResult
        작성 날짜: 2026/08/22
        """
        # 조회 대상이 새 Order로 바뀌지 않도록 기존 aggregate만 받는다.
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")
        query_order_result = getattr(
            self._rest_client,
            "query_order_result",
            None,
        )
        if not callable(query_order_result):
            raise TypeError("rest_client must provide query_order_result")

        # 조회 결과도 제출과 동일한 normalized contract 및 식별자 검증을 거친다.
        result = query_order_result(order=order)
        return self._validate_order_result(order, result)

    def cancel_order(self, order: Order) -> OrderResult:
        """
        함수 이름: cancel_order()
        기능: 기존 Order를 취소하고 취소 응답의 실제 fill까지 정규화 결과로 보존한다.
        인자: order -> 취소할 기존 Order aggregate
        반환값: client가 반환한 검증된 OrderResult
        작성 날짜: 2026/08/22
        """
        # cancel 역시 원 주문 식별자 이외의 새 제출 정보를 만들 수 없다.
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")
        cancel_order = getattr(self._rest_client, "cancel_order", None)
        if not callable(cancel_order):
            raise TypeError("rest_client must provide cancel_order")

        # CANCELED 상태보다 응답에 포함된 실제 fill이 우선되도록 그대로 aggregate에 넘긴다.
        result = cancel_order(order=order)
        return self._validate_order_result(order, result)

    def sell_all_position(self, order: Order) -> OrderResult:
        """
        함수 이름: sell_all_position()
        기능: 전량 매도 Order를 일반 주문 제출 경계로 보내 동일 pipeline을 재사용한다.
        인자: order -> 잔여 Position 전량을 요청한 SELL Order
        반환값: 검증된 OrderResult
        작성 날짜: 2026/08/22
        """
        # Force sell이 별도 회계·저장 경로로 갈라지지 않도록 SELL만 같은 submit으로 위임한다.
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")
        if order.side is not OrderSide.SELL:
            raise ValueError("sell_all_position requires a SELL order")

        return self.submit_order(order)  # 일반 SELL과 같은 ID·fill·history 계약을 공유한다.

    def list_open_order_results(
        self,
        symbol: str,
    ) -> tuple[OrderResult, ...]:
        """
        함수 이름: list_open_order_results()
        기능: 재시작 시 상품의 미결 주문을 raw payload 없이 정규화해 반환한다.
        인자: symbol -> 조회할 Binance Spot symbol
        반환값: 정규화된 현재 미결 OrderResult tuple
        작성 날짜: 2026/08/22
        """
        # 실제 client의 조회 operation과 상품 값을 외부 I/O 전에 검증한다.
        normalized_symbol = _normalize_symbol(symbol)
        list_open_order_results = getattr(
            self._rest_client,
            "list_open_order_results",
            None,
        )
        if not callable(list_open_order_results):
            return ()  # 복구 port가 없는 fake runtime에는 거래소 미결 주문도 존재하지 않는다.

        # Collection 전체를 한 번 검증해 부분적으로 잘못된 결과를 공개하지 않는다.
        results = list_open_order_results(symbol=normalized_symbol)
        return self._validate_order_result_collection(
            normalized_symbol,
            results,
            "open order",
        )

    def list_all_open_order_results(
        self,
        symbol: str,
    ) -> tuple[OrderResult, ...]:
        """
        함수 이름: list_all_open_order_results()
        기능: 실제 계정 격리 검증을 위해 상품의 모든 client ID 미결 주문을 반환한다.
        인자: symbol -> 조회할 Binance Spot symbol
        반환값: 정규화된 현재 전체 미결 OrderResult tuple
        작성 날짜: 2026/08/31
        """
        normalized_symbol = _normalize_symbol(symbol)
        list_all_open_results = getattr(
            self._rest_client,
            "list_all_open_order_results",
            None,
        )
        if not callable(list_all_open_results):
            raise TypeError("rest_client must provide all-open-order query")

        # 안전 preflight surface는 capability 부재를 빈 계정으로 완화하지 않고 전체 collection을 검증한다.
        results = list_all_open_results(symbol=normalized_symbol)
        return self._validate_order_result_collection(
            normalized_symbol,
            results,
            "all open order",
        )  # Manual client ID까지 포함한 검증 완료 tuple만 preflight에 제공한다.

    def list_recent_order_results(
        self,
        symbol: str,
        limit: int = 100,
    ) -> tuple[OrderResult, ...]:
        """
        함수 이름: list_recent_order_results()
        기능: 재시작 누락 체결 탐지에 사용할 최근 주문 결과를 정규화해 반환한다.
        인자: symbol -> 조회할 Binance Spot symbol
            limit -> 반환할 최근 주문 최대 개수
        반환값: 정규화된 최근 OrderResult tuple
        작성 날짜: 2026/08/22
        """
        # bool을 정수 limit로 받지 않고 공식 allOrders 범위 안의 값만 허용한다.
        normalized_symbol = _normalize_symbol(symbol)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        list_recent_order_results = getattr(
            self._rest_client,
            "list_recent_order_results",
            None,
        )
        if not callable(list_recent_order_results):
            return ()  # fake port에는 외부 recent execution source가 없다.

        # Recent 결과도 open-order 조회와 같은 normalized collection guard를 통과시킨다.
        results = list_recent_order_results(
            symbol=normalized_symbol,
            limit=limit,
        )
        return self._validate_order_result_collection(
            normalized_symbol,
            results,
            "recent order",
        )

    def fetch_earn_residual_evidence(self, since: datetime):
        """
        함수 이름: fetch_earn_residual_evidence()
        기능: live 조회 근거의 domain 타입을 검증하며 미지원과 확인 성공을 구분한다.
        인자: since -> 최초 잔여 발생 시각
        반환값: EarnResidualEvidence 또는 None
        작성 날짜: 2026/09/15
        """
        from binance_auto_trader.domain.trading.residual import EarnResidualEvidence

        reader = getattr(self._rest_client, "fetch_earn_residual_evidence", None)
        evidence = reader(since=since) if callable(reader) else None
        if evidence is not None and type(evidence) is not EarnResidualEvidence:
            raise ValueError("invalid Earn residual evidence")
        return evidence

    def list_account_executions_since(self, symbol: str, order_id: str) -> tuple[AccountExecution, ...] | None:
        """
        함수 이름: list_account_executions_since()
        기능: 완전한 외부 체결 조회 port만 호출하고 미지원과 빈 결과를 구분한다.
        인자: symbol -> 상품, order_id -> 마지막 durable 주문 ID
        반환값: 검증된 AccountExecution tuple 또는 미지원 None
        작성 날짜: 2026/09/10
        """
        normalized_symbol = _normalize_symbol(symbol)
        reader = getattr(self._rest_client, "list_account_executions_since", None)
        if not callable(reader):
            return None
        results = reader(symbol=normalized_symbol, order_id=order_id)
        if results is None:
            return None
        if not isinstance(results, tuple) or any(not isinstance(item, AccountExecution) or item.result.symbol != normalized_symbol for item in results):
            raise ValueError("invalid account execution collection")
        if len({item.result.exchange_order_id for item in results}) != len(results) or len({item.result.client_order_id for item in results}) != len(results):
            raise ValueError("duplicate account execution identity")
        return results

    def list_all_recent_order_results(
        self,
        symbol: str,
        limit: int = 100,
    ) -> tuple[OrderResult, ...]:
        """
        함수 이름: list_all_recent_order_results()
        기능: 실제 계정 격리 delta를 위해 모든 client ID의 최근 주문 결과를 반환한다.
        인자: symbol -> 조회할 Binance Spot symbol
            limit -> 반환할 최근 주문 최대 개수
        반환값: 정규화된 최근 전체 OrderResult tuple
        작성 날짜: 2026/08/31
        """
        normalized_symbol = _normalize_symbol(symbol)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        list_all_recent_results = getattr(
            self._rest_client,
            "list_all_recent_order_results",
            None,
        )
        if not callable(list_all_recent_results):
            raise TypeError("rest_client must provide all-recent-order query")

        # Prefix 없는 recent collection도 symbol과 immutable OrderResult 계약 전체를 통과시킨다.
        results = list_all_recent_results(
            symbol=normalized_symbol,
            limit=limit,
        )
        return self._validate_order_result_collection(
            normalized_symbol,
            results,
            "all recent order",
        )  # Prefix 없는 recent 결과도 같은 immutable collection 계약으로 반환한다.

    @staticmethod
    def _validate_order_result_collection(
        symbol: str,
        results: object,
        field_name: str,
    ) -> tuple[OrderResult, ...]:
        """
        함수 이름: _validate_order_result_collection()
        기능: 복수 주문 조회가 같은 상품의 immutable OrderResult tuple인지 검증한다.
        인자: symbol -> 요청한 canonical 상품
            results -> client가 반환한 collection
            field_name -> 안전한 오류 문구에 사용할 논리 이름
        반환값: 검증된 OrderResult tuple
        작성 날짜: 2026/08/22
        """
        # Generator 재평가와 mutable list 유출을 막기 위해 client 계약을 tuple로 고정한다.
        if not isinstance(results, tuple):
            raise TypeError(f"{field_name} results must be a tuple")
        if any(not isinstance(result, OrderResult) for result in results):
            raise TypeError(f"{field_name} results must contain OrderResult")
        if any(result.symbol != symbol for result in results):
            raise ValueError(f"{field_name} result symbol does not match request")

        return results  # 모든 원소 검증이 끝난 뒤에만 동일 immutable tuple을 공개한다.

    @staticmethod
    def _validate_order_result(
        order: Order,
        result: object,
    ) -> OrderResult:
        """
        함수 이름: _validate_order_result()
        기능: fake client 결과가 같은 client order ID의 normalized OrderResult인지 검증한다.
        인자: order -> 호출에 사용한 원 Order
            result -> fake client가 반환한 값
        반환값: 검증된 OrderResult
        작성 날짜: 2026/08/22
        """
        # Raw Binance payload나 다른 주문의 결과가 domain으로 침투하기 전에 차단한다.
        if not isinstance(result, OrderResult):
            raise TypeError("order REST operations must return OrderResult")
        if result.client_order_id != order.client_order_id:
            raise ValueError("order result client_order_id does not match Order")

        return result  # 식별자가 일치한 immutable normalized 결과만 공개한다.
