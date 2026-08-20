"""RegimeSTM 테스트가 공유하는 불변 이벤트와 Context factory이다."""

from datetime import datetime, timezone
from decimal import Decimal

from binance_auto_trader.regime import (
    Interval,
    RegimeEvaluationContext,
    RegimeEvent,
    RegimeEventType,
)


TEST_INSTANT = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def make_context(
    ema9_slope: Decimal = Decimal("0"),
    has_higher_high: bool = False,
    has_higher_low: bool = False,
    has_lower_high: bool = False,
    has_lower_low: bool = False,
    current_price: Decimal = Decimal("100"),
    live_ema9: Decimal = Decimal("100"),
    evaluation_id: str = "evaluation-1",
    source_candle_id: str | None = None,
) -> RegimeEvaluationContext:
    """
    함수 이름: make_context()
    기능: guard와 STM 테스트용 4시간봉 평가 Context를 생성한다.
    인자: ema9_slope -> EMA9 기울기
        has_higher_high -> Higher High 성립 여부
        has_higher_low -> Higher Low 성립 여부
        has_lower_high -> Lower High 성립 여부
        has_lower_low -> Lower Low 성립 여부
        current_price -> 평가 시점 현재가
        live_ema9 -> 평가 시점 실시간 EMA9
        evaluation_id -> 평가 cycle 식별자
        source_candle_id -> 재평가 원본 candle 식별자
    반환값: 검증된 RegimeEvaluationContext
    작성 날짜: 2026/08/14
    """
    return RegimeEvaluationContext(
        evaluation_id=evaluation_id,
        symbol="ETHUSDT",
        timeframe=Interval.FOUR_HOURS,
        ema9_slope=ema9_slope,
        has_higher_high=has_higher_high,
        has_higher_low=has_higher_low,
        has_lower_high=has_lower_high,
        has_lower_low=has_lower_low,
        current_price=current_price,
        live_ema9=live_ema9,
        source_market_version=42,
        source_candle_id=source_candle_id,
        calculated_at=TEST_INSTANT,
    )


def make_event(
    event_type: RegimeEventType,
    evaluation_id: str = "evaluation-1",
    source_candle_id: str | None = None,
    event_id: str = "event-1",
) -> RegimeEvent:
    """
    함수 이름: make_event()
    기능: STM 테스트용 불변 REGIME 이벤트를 생성한다.
    인자: event_type -> 생성할 이벤트 타입
        evaluation_id -> 평가 cycle 식별자
        source_candle_id -> 재평가 원본 candle 식별자
        event_id -> 이벤트 식별자
    반환값: 검증된 RegimeEvent
    작성 날짜: 2026/08/14
    """
    return RegimeEvent(
        event_type=event_type,
        event_id=event_id,
        occurred_at=TEST_INSTANT,
        evaluation_id=evaluation_id,
        source_candle_id=source_candle_id,
    )

