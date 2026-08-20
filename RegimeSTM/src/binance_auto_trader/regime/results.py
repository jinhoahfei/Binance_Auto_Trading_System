"""RegimeSTM 결정 결과와 Controller 적용 결과 값 타입을 정의한다."""

from dataclasses import dataclass
from datetime import datetime

from ._validation import (
    validate_aware_datetime,
    validate_non_empty_text,
    validate_optional_non_empty_text,
)
from .action_requests import (
    ApplyRecommendedRegime,
    RegimeActionRequest,
    StartRegimeEvaluation,
)
from .states import RegimeState, RegimeType, recommended_state_for


@dataclass(frozen=True, slots=True)
class RegimeSTMResult:
    """
    클래스 이름: RegimeSTMResult
    기능: 한 이벤트 처리의 전이와 순서화된 Action 요청을 불변으로 보존한다.
    작성 날짜: 2026/08/14
    """

    decision_id: str
    consumed: bool
    transition_id: str | None
    state_before: RegimeState
    state_after: RegimeState
    action_requests: tuple[RegimeActionRequest, ...]
    evaluation_id: str

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: STM 결과의 전이 여부, 상태 및 Action 요청 불변식을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        validate_non_empty_text(self.decision_id, "decision_id")

        if not isinstance(self.consumed, bool):
            raise TypeError("consumed must be a bool")

        validate_optional_non_empty_text(
            self.transition_id,
            "transition_id",
        )

        if not isinstance(self.state_before, RegimeState):
            raise TypeError("state_before must be a RegimeState")

        if not isinstance(self.state_after, RegimeState):
            raise TypeError("state_after must be a RegimeState")

        if not isinstance(self.action_requests, tuple):
            raise TypeError("action_requests must be a tuple")

        for action_request in self.action_requests:
            if not isinstance(
                action_request,
                (StartRegimeEvaluation, ApplyRecommendedRegime),
            ):
                raise TypeError(
                    "action_requests must contain RegimeActionRequest values"
                )

        validate_non_empty_text(self.evaluation_id, "evaluation_id")

        if self.consumed and self.transition_id is None:
            raise ValueError("a consumed result requires transition_id")

        if self.consumed and not self.action_requests:
            raise ValueError("a consumed result requires action_requests")

        if not self.consumed and self.transition_id is not None:
            raise ValueError("a no-op result cannot have transition_id")

        if not self.consumed and self.action_requests:
            raise ValueError("a no-op result cannot have action_requests")

        if not self.consumed and self.state_before is not self.state_after:
            raise ValueError("a no-op result cannot change state")


@dataclass(frozen=True, slots=True)
class RegimeResult:
    """
    클래스 이름: RegimeResult
    기능: Controller가 추천 Action을 적용한 결과 metadata를 불변으로 보존한다.
    작성 날짜: 2026/08/14
    """

    evaluation_id: str
    recommended_type: RegimeType
    previous_recommended_type: RegimeType | None
    changed: bool
    transition_id: str
    state: RegimeState
    source_candle_id: str | None
    calculated_at: datetime

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: Controller 적용 결과의 추천값, 상태 및 변경 여부를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        validate_non_empty_text(self.evaluation_id, "evaluation_id")

        if not isinstance(self.recommended_type, RegimeType):
            raise TypeError("recommended_type must be a RegimeType")

        if (
            self.previous_recommended_type is not None
            and not isinstance(self.previous_recommended_type, RegimeType)
        ):
            raise TypeError(
                "previous_recommended_type must be a RegimeType or None"
            )

        if not isinstance(self.changed, bool):
            raise TypeError("changed must be a bool")

        expected_changed = (
            self.previous_recommended_type is not self.recommended_type
        )
        if self.changed is not expected_changed:
            raise ValueError("changed must match the recommendation difference")

        validate_non_empty_text(self.transition_id, "transition_id")

        if self.state is not recommended_state_for(self.recommended_type):
            raise ValueError("state must match recommended_type")

        validate_optional_non_empty_text(
            self.source_candle_id,
            "source_candle_id",
        )
        validate_aware_datetime(self.calculated_at, "calculated_at")
