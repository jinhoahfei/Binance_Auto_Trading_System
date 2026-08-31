"""Binance 봉 데이터를 정규화한 불변 Kline 값 객체를 정의한다."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import re

from ..common import Interval


_NORMALIZED_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]+$")


def _validate_normalized_symbol(symbol: object) -> None:
    """
    함수 이름: _validate_normalized_symbol()
    기능: symbol이 공백 없는 ASCII 대문자와 숫자로 정규화됐는지 검증한다.
    인자: symbol -> 검증할 거래 symbol
    반환값: 없음
    작성 날짜: 2026/08/20
    """
    if not isinstance(symbol, str):
        raise TypeError("symbol must be a string")

    if _NORMALIZED_SYMBOL_PATTERN.fullmatch(symbol) is None:
        raise ValueError(
            "symbol must contain only uppercase ASCII letters and digits"
        )


def _normalize_utc_datetime(value: object, field_name: str) -> datetime:
    """
    함수 이름: _normalize_utc_datetime()
    기능: 시간대가 있는 UTC datetime인지 검증하고 canonical UTC로 변환한다.
    인자: value -> 검증하고 변환할 시각
        field_name -> 오류 메시지에 표시할 필드 이름
    반환값: timezone.utc로 정규화한 datetime
    작성 날짜: 2026/08/20
    """
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")

    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must use UTC")

    return value.astimezone(timezone.utc)


def _validate_finite_decimal(value: object, field_name: str) -> None:
    """
    함수 이름: _validate_finite_decimal()
    기능: 금융 수치가 float가 아닌 유한 Decimal인지 검증한다.
    인자: value -> 검증할 금융 수치
        field_name -> 오류 메시지에 표시할 필드 이름
    반환값: 없음
    작성 날짜: 2026/08/20
    """
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be a Decimal")

    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite")


@dataclass(frozen=True, slots=True)
class Kline:
    """
    클래스 이름: Kline
    기능: symbol과 시간 주기별 OHLCV 봉 데이터를 불변 값으로 보존한다.
    작성 날짜: 2026/08/20
    """

    symbol: str
    interval: Interval
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    closed: bool
    event_time: datetime | None = None

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: Kline의 symbol, 시각, 금융 수치와 OHLC 관계를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        _validate_normalized_symbol(self.symbol)

        if not isinstance(self.interval, Interval):
            raise TypeError("interval must be the canonical Interval")

        normalized_open_time = _normalize_utc_datetime(
            self.open_time,
            "open_time",
        )
        object.__setattr__(self, "open_time", normalized_open_time)

        # REST 봉에는 event 시각이 없지만 WebSocket 봉은 단조 순서와 provenance를 보존한다.
        if self.event_time is not None:
            normalized_event_time = _normalize_utc_datetime(
                self.event_time,
                "event_time",
            )
            object.__setattr__(self, "event_time", normalized_event_time)

        price_fields = (
            ("open", self.open),
            ("high", self.high),
            ("low", self.low),
            ("close", self.close),
        )
        for field_name, field_value in price_fields:
            _validate_finite_decimal(field_value, field_name)
            if field_value <= Decimal("0"):
                raise ValueError(f"{field_name} must be greater than zero")

        _validate_finite_decimal(self.volume, "volume")
        if self.volume < Decimal("0"):
            raise ValueError("volume must not be negative")

        if self.low > min(self.open, self.close):
            raise ValueError("low must not exceed open or close")

        if self.high < max(self.open, self.close):
            raise ValueError("high must not be below open or close")

        if not isinstance(self.closed, bool):
            raise TypeError("closed must be a bool")
