"""동일 시장 version에서 계산한 4시간봉 REGIME 지표 snapshot을 정의한다."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable

from ..common import Interval
from .kline import _validate_normalized_symbol


def _validate_finite_decimal(value: object, field_name: str) -> None:
    """
    함수 이름: _validate_finite_decimal()
    기능: 지표 금융 수치가 float가 아닌 유한 Decimal인지 검증한다.
    인자: value -> 검증할 금융 수치
        field_name -> 오류 메시지에 표시할 필드 이름
    반환값: 없음
    작성 날짜: 2026/08/20
    """
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be a Decimal")

    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite")


def _validate_utc_datetime(value: object, field_name: str) -> datetime:
    """
    함수 이름: _validate_utc_datetime()
    기능: 시각이 시간대 정보를 가진 UTC datetime인지 검증하고 정규화한다.
    인자: value -> 검증하고 정규화할 시각
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


def _validate_non_empty_text(value: object, field_name: str) -> None:
    """
    함수 이름: _validate_non_empty_text()
    기능: provenance 문자열이 공백이 아닌지 검증한다.
    인자: value -> 검증할 문자열
        field_name -> 오류 메시지에 표시할 필드 이름
    반환값: 없음
    작성 날짜: 2026/08/20
    """
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")

    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")


@dataclass(frozen=True, slots=True)
class SwingStructure:
    """
    클래스 이름: SwingStructure
    기능: 확정 swing high와 low 및 HH, HL, LH, LL 판정을 불변으로 보존한다.
    작성 날짜: 2026/08/20
    """

    swing_highs: tuple[Decimal, ...]
    swing_lows: tuple[Decimal, ...]
    has_higher_high: bool
    has_higher_low: bool
    has_lower_high: bool
    has_lower_low: bool

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: swing point와 구조 방향 boolean의 불변식을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        if not isinstance(self.swing_highs, tuple):
            raise TypeError("swing_highs must be a tuple")
        if not isinstance(self.swing_lows, tuple):
            raise TypeError("swing_lows must be a tuple")

        if len(self.swing_highs) < 2:
            raise ValueError("swing_highs must contain at least two points")
        if len(self.swing_lows) < 2:
            raise ValueError("swing_lows must contain at least two points")

        for swing_high in self.swing_highs:
            _validate_finite_decimal(swing_high, "swing_high")
            if swing_high <= Decimal("0"):
                raise ValueError("swing_high must be greater than zero")

        for swing_low in self.swing_lows:
            _validate_finite_decimal(swing_low, "swing_low")
            if swing_low <= Decimal("0"):
                raise ValueError("swing_low must be greater than zero")

        structure_fields = (
            ("has_higher_high", self.has_higher_high),
            ("has_higher_low", self.has_higher_low),
            ("has_lower_high", self.has_lower_high),
            ("has_lower_low", self.has_lower_low),
        )
        for field_name, field_value in structure_fields:
            if not isinstance(field_value, bool):
                raise TypeError(f"{field_name} must be a bool")

        if self.has_higher_high and self.has_lower_high:
            raise ValueError("high structure directions must be exclusive")
        if self.has_higher_low and self.has_lower_low:
            raise ValueError("low structure directions must be exclusive")


@dataclass(frozen=True, slots=True)
class _IndicatorSnapshotState:
    """
    클래스 이름: _IndicatorSnapshotState
    기능: 한 계산 결과의 지표 값을 단일 불변 상태로 묶는다.
    작성 날짜: 2026/08/20
    """

    ema9_series: tuple[Decimal, ...]
    ema9_slope: Decimal | None
    swing_structure: SwingStructure | None
    live_ema9: Decimal | None
    ready: bool


class IndicatorSnapshot:
    """
    클래스 이름: IndicatorSnapshot
    기능: 동일 MarketSnapshot version의 4시간봉 REGIME 지표와 provenance를 보존한다.
    작성 날짜: 2026/08/20
    """

    __slots__ = (
        "_calculated_at",
        "_current_price",
        "_source_candle_id",
        "_source_market_version",
        "_state",
        "_symbol",
        "_timeframe",
    )

    def __init__(
        self,
        symbol: str,
        current_price: Decimal,
        source_market_version: int,
        source_candle_id: str,
        calculated_at: datetime,
        timeframe: Interval = Interval.FOUR_HOURS,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 계산 provenance가 고정된 비어 있는 IndicatorSnapshot을 만든다.
        인자: symbol -> 지표 원본의 정규화된 거래 symbol
            current_price -> 같은 version의 진행 중 4시간봉 현재가
            source_market_version -> 지표를 파생한 MarketSnapshot version
            source_candle_id -> 최신 확정 4시간봉 식별자
            calculated_at -> 원본 MarketSnapshot의 UTC 갱신 시각
            timeframe -> 지표의 canonical 4시간 주기
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        _validate_normalized_symbol(symbol)
        _validate_finite_decimal(current_price, "current_price")
        if current_price <= Decimal("0"):
            raise ValueError("current_price must be greater than zero")

        if (
            isinstance(source_market_version, bool)
            or not isinstance(source_market_version, int)
        ):
            raise TypeError("source_market_version must be an integer")
        if source_market_version <= 0:
            raise ValueError("source_market_version must be greater than zero")

        _validate_non_empty_text(source_candle_id, "source_candle_id")
        normalized_calculated_at = _validate_utc_datetime(
            calculated_at,
            "calculated_at",
        )
        if not isinstance(timeframe, Interval):
            raise TypeError("timeframe must be the canonical Interval")
        if timeframe is not Interval.FOUR_HOURS:
            raise ValueError("timeframe must be Interval.FOUR_HOURS")

        self._symbol = symbol
        self._current_price = current_price
        self._source_market_version = source_market_version
        self._source_candle_id = source_candle_id
        self._calculated_at = normalized_calculated_at
        self._timeframe = timeframe
        self._state = _IndicatorSnapshotState(
            ema9_series=(),
            ema9_slope=None,
            swing_structure=None,
            live_ema9=None,
            ready=False,
        )

    @property
    def symbol(self) -> str:
        """
        함수 이름: symbol()
        기능: 지표 원본의 거래 symbol을 반환한다.
        인자: 없음
        반환값: 정규화된 거래 symbol
        작성 날짜: 2026/08/20
        """
        return self._symbol

    @property
    def timeframe(self) -> Interval:
        """
        함수 이름: timeframe()
        기능: 지표 계산의 canonical 4시간 주기를 반환한다.
        인자: 없음
        반환값: Interval.FOUR_HOURS
        작성 날짜: 2026/08/20
        """
        return self._timeframe

    @property
    def ema9_series(self) -> tuple[Decimal, ...]:
        """
        함수 이름: ema9_series()
        기능: 확정 4시간봉으로 계산한 EMA9 시계열을 반환한다.
        인자: 없음
        반환값: 외부에서 변경할 수 없는 EMA9 tuple
        작성 날짜: 2026/08/20
        """
        return self._state.ema9_series

    @property
    def ema9_slope(self) -> Decimal:
        """
        함수 이름: ema9_slope()
        기능: 최근 여섯 EMA9의 정규화된 기울기를 반환한다.
        인자: 없음
        반환값: 소수점 여덟 자리의 %/4시간봉 Decimal
        작성 날짜: 2026/08/20
        """
        value = self._state.ema9_slope
        if value is None:
            raise RuntimeError("IndicatorSnapshot is not ready")

        return value

    @property
    def swing_structure(self) -> SwingStructure:
        """
        함수 이름: swing_structure()
        기능: 확정 swing point와 HH, HL, LH, LL 구조를 반환한다.
        인자: 없음
        반환값: 불변 SwingStructure
        작성 날짜: 2026/08/20
        """
        value = self._state.swing_structure
        if value is None:
            raise RuntimeError("IndicatorSnapshot is not ready")

        return value

    @property
    def live_ema9(self) -> Decimal:
        """
        함수 이름: live_ema9()
        기능: 진행 중인 4시간봉 현재가를 한 번 적용한 live EMA9을 반환한다.
        인자: 없음
        반환값: 같은 version의 live EMA9 Decimal
        작성 날짜: 2026/08/20
        """
        value = self._state.live_ema9
        if value is None:
            raise RuntimeError("IndicatorSnapshot is not ready")

        return value

    @property
    def current_price(self) -> Decimal:
        """
        함수 이름: current_price()
        기능: 지표와 함께 고정한 진행 중 4시간봉 현재가를 반환한다.
        인자: 없음
        반환값: 같은 MarketSnapshot version의 현재가
        작성 날짜: 2026/08/20
        """
        return self._current_price

    @property
    def source_market_version(self) -> int:
        """
        함수 이름: source_market_version()
        기능: 지표가 파생된 MarketSnapshot version을 반환한다.
        인자: 없음
        반환값: 1 이상의 source version
        작성 날짜: 2026/08/20
        """
        return self._source_market_version

    @property
    def source_candle_id(self) -> str:
        """
        함수 이름: source_candle_id()
        기능: 평가 기준인 최신 확정 4시간봉 ID를 반환한다.
        인자: 없음
        반환값: symbol, interval, open time을 포함한 candle ID
        작성 날짜: 2026/08/20
        """
        return self._source_candle_id

    @property
    def calculated_at(self) -> datetime:
        """
        함수 이름: calculated_at()
        기능: 원본 MarketSnapshot과 공유하는 UTC 계산 시각을 반환한다.
        인자: 없음
        반환값: timezone-aware UTC datetime
        작성 날짜: 2026/08/20
        """
        return self._calculated_at

    @property
    def ready(self) -> bool:
        """
        함수 이름: ready()
        기능: 네 지표 값이 원자적으로 반영됐는지 반환한다.
        인자: 없음
        반환값: IndicatorSnapshot 준비 여부
        작성 날짜: 2026/08/20
        """
        return self._state.ready

    def update(
        self,
        ema9_series: Iterable[Decimal],
        ema9_slope: Decimal,
        swing_structure: SwingStructure,
        live_ema9: Decimal,
    ) -> None:
        """
        함수 이름: update()
        기능: 계산된 EMA9, 기울기, swing 구조와 live EMA9을 원자적으로 반영한다.
        인자: ema9_series -> 확정 4시간봉 EMA9 시계열
            ema9_slope -> 최근 여섯 EMA9의 정규화된 기울기
            swing_structure -> 확정 swing 구조 판정
            live_ema9 -> 진행 중인 현재가를 한 번 적용한 EMA9
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        if isinstance(ema9_series, (str, bytes)):
            raise TypeError("ema9_series must be an iterable of Decimal")

        try:
            normalized_ema9_series = tuple(ema9_series)
        except TypeError as error:
            raise TypeError(
                "ema9_series must be an iterable of Decimal"
            ) from error

        if len(normalized_ema9_series) < 6:
            raise ValueError("ema9_series must contain at least six values")

        for ema9_value in normalized_ema9_series:
            _validate_finite_decimal(ema9_value, "ema9_series value")
            if ema9_value <= Decimal("0"):
                raise ValueError(
                    "ema9_series values must be greater than zero"
                )

        _validate_finite_decimal(ema9_slope, "ema9_slope")
        if not isinstance(swing_structure, SwingStructure):
            raise TypeError("swing_structure must be a SwingStructure")
        _validate_finite_decimal(live_ema9, "live_ema9")
        if live_ema9 <= Decimal("0"):
            raise ValueError("live_ema9 must be greater than zero")

        next_state = _IndicatorSnapshotState(
            ema9_series=normalized_ema9_series,
            ema9_slope=ema9_slope,
            swing_structure=swing_structure,
            live_ema9=live_ema9,
            ready=True,
        )
        self._state = next_state
