"""RegimeSTM이 Controller에 반환하는 불변 Action 요청을 정의한다."""

from dataclasses import dataclass
from typing import TypeAlias

from ..common import RegimeType
from ._validation import (
    validate_non_empty_text,
    validate_optional_non_empty_text,
)
from .events import RegimeEvaluationTrigger


@dataclass(frozen=True, slots=True)
class StartRegimeEvaluation:
    """
    클래스 이름: StartRegimeEvaluation
    기능: Controller에 REGIME 평가 microstep 시작을 요청한다.
    작성 날짜: 2026/08/14
    """

    evaluation_id: str
    trigger: RegimeEvaluationTrigger
    source_candle_id: str | None

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 평가 시작 Action 요청의 식별자와 trigger를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        validate_non_empty_text(self.evaluation_id, "evaluation_id")

        if not isinstance(self.trigger, RegimeEvaluationTrigger):
            raise TypeError("trigger must be a RegimeEvaluationTrigger")

        validate_optional_non_empty_text(
            self.source_candle_id,
            "source_candle_id",
        )

        if (
            self.trigger is RegimeEvaluationTrigger.FOUR_HOUR_CANDLE_CLOSE
            and self.source_candle_id is None
        ):
            raise ValueError(
                "FOUR_HOUR_CANDLE_CLOSE requires source_candle_id"
            )


@dataclass(frozen=True, slots=True)
class ApplyRecommendedRegime:
    """
    클래스 이름: ApplyRecommendedRegime
    기능: Controller에 결정된 추천 REGIME 적용을 요청한다.
    작성 날짜: 2026/08/14
    """

    evaluation_id: str
    regime_type: RegimeType
    source_candle_id: str | None

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 추천 적용 Action 요청의 식별자와 REGIME 타입을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        validate_non_empty_text(self.evaluation_id, "evaluation_id")

        if not isinstance(self.regime_type, RegimeType):
            raise TypeError("regime_type must be a RegimeType")

        validate_optional_non_empty_text(
            self.source_candle_id,
            "source_candle_id",
        )


RegimeActionRequest: TypeAlias = (
    StartRegimeEvaluation | ApplyRecommendedRegime
)
