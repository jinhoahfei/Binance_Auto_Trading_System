"""공식 Binance Spot REST Kline과 account payload를 정규화한다."""

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Protocol

from binance_auto_trader.domain.trading.account import (
    AccountSnapshot,
    AssetBalance,
    SUPPORTED_VALUATION_ASSET,
)
from binance_auto_trader.domain.market import (
    Interval,
    Kline,
    SUPPORTED_INTERVALS,
)


MINIMUM_KLINE_LIMIT = 1
MAXIMUM_KLINE_LIMIT = 1000
DEFAULT_KLINE_LIMIT = 500
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_INTERVAL_MILLISECONDS_BY_INTERVAL = {
    Interval.ONE_MINUTE: 60_000,
    Interval.THIRTY_MINUTES: 1_800_000,
    Interval.FOUR_HOURS: 14_400_000,
    Interval.ONE_DAY: 86_400_000,
}


class BinanceRESTClient(Protocol):
    """
    클래스 이름: BinanceRESTClient
    기능: Binance Spot Kline과 account REST 호출을 주입할 client 계약을 정의한다.
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
    기능: 공식 Spot account 응답을 가격 정보 없는 전체 AccountSnapshot으로 변환한다.
    인자: payload -> JSON으로 해석한 GET /api/v3/account 응답
        valuation_asset -> 응답에 반드시 포함되어야 할 ETH asset
    반환값: 정규화된 전체 AccountSnapshot
    작성 날짜: 2026/08/21
    """
    if not isinstance(payload, Mapping):
        raise TypeError("Binance account payload must be an object")
    if payload.get("accountType") != "SPOT":
        raise ValueError("Binance accountType must be SPOT")

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


class APIGateway:
    """
    클래스 이름: APIGateway
    기능: Binance Spot REST Kline과 account 응답을 canonical domain 타입으로 정규화한다.
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
        provides_kline_operation = callable(
            getattr(rest_client, "get_klines", None)
        )
        provides_account_operation = callable(
            getattr(rest_client, "get_account", None)
        )
        if rest_client is None or not (
            provides_kline_operation or provides_account_operation
        ):
            raise TypeError(
                "rest_client must provide get_klines or get_account"
            )

        selected_clock = _utc_now if clock is None else clock
        if not callable(selected_clock):
            raise TypeError("clock must be callable")

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

        _normalize_utc_datetime(self._clock(), "clock result")
        raw_payloads: dict[
            Interval,
            list[object] | tuple[object, ...],
        ] = {}

        for interval in SUPPORTED_INTERVALS:
            payload = get_klines(
                symbol=normalized_symbol,
                interval=interval.value,
                limit=limit,
            )
            if not isinstance(payload, (list, tuple)):
                raise TypeError(
                    f"Binance {interval.value} kline payload must be an array"
                )

            raw_payloads[interval] = payload

        current_time_milliseconds = _utc_to_milliseconds(self._clock())
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
