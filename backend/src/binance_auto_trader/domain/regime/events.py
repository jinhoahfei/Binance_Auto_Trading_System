"""RegimeSTM에 전달되는 이벤트 값 타입을 정의한다."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto

from ._validation import (
    validate_aware_datetime,
    validate_non_empty_text,
    validate_optional_non_empty_text,
)


class RegimeEventType(Enum):
    """
    클래스 이름: RegimeEventType
    기능: REGIME 최초 평가, 재평가 및 평가 준비 완료 이벤트를 정의한다.
    작성 날짜: 2026/08/14
    """

    INITIAL_EVALUATION_REQUESTED = auto()
    FOUR_HOUR_CANDLE_CLOSED = auto()
    EVALUATION_READY = auto()


class RegimeEvaluationTrigger(Enum):
    """
    클래스 이름: RegimeEvaluationTrigger
    기능: REGIME 평가를 시작하게 한 외부 원인을 정의한다.
    작성 날짜: 2026/08/14
    """

    INITIAL = auto()
    FOUR_HOUR_CANDLE_CLOSE = auto()


@dataclass(frozen=True, slots=True)
class RegimeEvent:
    """
    클래스 이름: RegimeEvent
    기능: 한 평가 cycle을 식별하는 불변 REGIME 이벤트를 보존한다.
    작성 날짜: 2026/08/14
    """

    event_type: RegimeEventType
    event_id: str
    occurred_at: datetime
    evaluation_id: str
    source_candle_id: str | None = None

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: REGIME 이벤트의 타입, 식별자 및 시각 불변식을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        if not isinstance(self.event_type, RegimeEventType):
            raise TypeError("event_type must be a RegimeEventType")

        validate_non_empty_text(self.event_id, "event_id")
        validate_aware_datetime(self.occurred_at, "occurred_at")
        validate_non_empty_text(self.evaluation_id, "evaluation_id")
        validate_optional_non_empty_text(
            self.source_candle_id,
            "source_candle_id",
        )

        if (
            self.event_type is RegimeEventType.FOUR_HOUR_CANDLE_CLOSED
            and self.source_candle_id is None
        ):
            raise ValueError(
                "FOUR_HOUR_CANDLE_CLOSED requires source_candle_id"
            )

