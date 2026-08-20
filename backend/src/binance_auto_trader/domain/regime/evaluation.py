"""RegimeSTM guard가 사용하는 불변 4시간봉 평가 입력을 정의한다."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from ._validation import (
    validate_aware_datetime,
    validate_finite_decimal,
    validate_non_empty_text,
    validate_non_negative_integer,
    validate_optional_non_empty_text,
)


class Interval(Enum):
    """
    클래스 이름: Interval
    기능: 시스템에서 사용하는 Kline 시간 주기를 정의한다.
    작성 날짜: 2026/08/14
    """

    ONE_MINUTE = "1m"
    THIRTY_MINUTES = "30m"
    FOUR_HOURS = "4h"
    ONE_DAY = "1d"


@dataclass(frozen=True, slots=True)
class RegimeEvaluationContext:
    """
    클래스 이름: RegimeEvaluationContext
    기능: 동일 MarketSnapshot에서 파생한 REGIME guard 입력을 불변으로 보존한다.
    작성 날짜: 2026/08/14
    """

    evaluation_id: str
    symbol: str
    timeframe: Interval
    ema9_slope: Decimal
    has_higher_high: bool
    has_higher_low: bool
    has_lower_high: bool
    has_lower_low: bool
    current_price: Decimal
    live_ema9: Decimal
    source_market_version: int
    source_candle_id: str | None
    calculated_at: datetime

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 평가 Context의 시간 주기, 수치 및 snapshot 불변식을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        validate_non_empty_text(self.evaluation_id, "evaluation_id")
        validate_non_empty_text(self.symbol, "symbol")

        if not isinstance(self.timeframe, Interval):
            raise TypeError("timeframe must be an Interval")

        if self.timeframe is not Interval.FOUR_HOURS:
            raise ValueError("timeframe must be Interval.FOUR_HOURS")

        validate_finite_decimal(self.ema9_slope, "ema9_slope")
        validate_finite_decimal(self.current_price, "current_price")
        validate_finite_decimal(self.live_ema9, "live_ema9")

        boolean_fields = (
            ("has_higher_high", self.has_higher_high),
            ("has_higher_low", self.has_higher_low),
            ("has_lower_high", self.has_lower_high),
            ("has_lower_low", self.has_lower_low),
        )
        for field_name, field_value in boolean_fields:
            if not isinstance(field_value, bool):
                raise TypeError(f"{field_name} must be a bool")

        if self.current_price <= Decimal("0"):
            raise ValueError("current_price must be greater than zero")

        if self.live_ema9 <= Decimal("0"):
            raise ValueError("live_ema9 must be greater than zero")

        validate_non_negative_integer(
            self.source_market_version,
            "source_market_version",
        )
        validate_optional_non_empty_text(
            self.source_candle_id,
            "source_candle_id",
        )
        validate_aware_datetime(self.calculated_at, "calculated_at")

