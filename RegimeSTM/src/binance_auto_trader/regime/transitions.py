"""Event-Action Table의 13개 REGIME transition registry를 정의한다."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Mapping, TypeAlias

from ._validation import validate_non_empty_text
from .action_requests import (
    ApplyRecommendedRegime,
    RegimeActionRequest,
    StartRegimeEvaluation,
)
from .evaluation import RegimeEvaluationContext
from .events import (
    RegimeEvaluationTrigger,
    RegimeEvent,
    RegimeEventType,
)
from .guards import (
    matches_sideways_regime,
    matches_strong_down_fallback,
    matches_strong_down_regime,
    matches_strong_up_fallback,
    matches_strong_up_regime,
    matches_weak_down_regime,
    matches_weak_up_regime,
)
from .states import RegimeState, RegimeType, recommended_state_for


RegimeGuard: TypeAlias = Callable[[RegimeEvaluationContext], bool]
TransitionKey: TypeAlias = tuple[RegimeState, RegimeEventType]


@dataclass(frozen=True, slots=True)
class RegimeTransition:
    """
    클래스 이름: RegimeTransition
    기능: Event-Action Table 한 행의 상태, 이벤트, guard 및 Action 종류를 보존한다.
    작성 날짜: 2026/08/14
    """

    transition_id: str
    current_state: RegimeState
    event_type: RegimeEventType
    next_state: RegimeState
    guard: RegimeGuard | None = None
    evaluation_trigger: RegimeEvaluationTrigger | None = None
    regime_type: RegimeType | None = None

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: transition 행의 타입과 Action 결정 방식이 일관적인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        validate_non_empty_text(self.transition_id, "transition_id")

        if not self.transition_id.startswith("EA-"):
            raise ValueError("transition_id must start with EA-")

        if not isinstance(self.current_state, RegimeState):
            raise TypeError("current_state must be a RegimeState")

        if not isinstance(self.event_type, RegimeEventType):
            raise TypeError("event_type must be a RegimeEventType")

        if not isinstance(self.next_state, RegimeState):
            raise TypeError("next_state must be a RegimeState")

        if self.guard is not None and not callable(self.guard):
            raise TypeError("guard must be callable or None")

        has_evaluation_trigger = self.evaluation_trigger is not None
        has_regime_type = self.regime_type is not None
        if has_evaluation_trigger is has_regime_type:
            raise ValueError(
                "a transition requires exactly one Action request kind"
            )

        if self.event_type is RegimeEventType.EVALUATION_READY:
            if self.guard is None or self.regime_type is None:
                raise ValueError(
                    "EVALUATION_READY requires a guard and regime_type"
                )

            if self.next_state is not recommended_state_for(self.regime_type):
                raise ValueError("next_state must match regime_type")
        elif self.guard is not None or self.evaluation_trigger is None:
            raise ValueError(
                "evaluation start transitions require an evaluation_trigger"
            )

    def matches(self, context: RegimeEvaluationContext | None) -> bool:
        """
        함수 이름: matches()
        기능: 전달된 평가 Context가 transition guard를 충족하는지 반환한다.
        인자: context -> guard 판정에 사용할 불변 평가 Context
        반환값: transition 적용 가능 여부
        작성 날짜: 2026/08/14
        """
        if self.guard is None:
            return True

        if context is None:
            raise ValueError("a guarded transition requires context")

        return self.guard(context)

    def create_action(self, event: RegimeEvent) -> RegimeActionRequest:
        """
        함수 이름: create_action()
        기능: 선택된 transition과 이벤트로 불변 Action 요청을 생성한다.
        인자: event -> transition을 발생시킨 REGIME 이벤트
        반환값: Controller가 수행할 Action 요청
        작성 날짜: 2026/08/14
        """
        if event.event_type is not self.event_type:
            raise ValueError("event type does not match transition")

        if self.evaluation_trigger is not None:
            return StartRegimeEvaluation(
                evaluation_id=event.evaluation_id,
                trigger=self.evaluation_trigger,
                source_candle_id=event.source_candle_id,
            )

        if self.regime_type is None:
            raise RuntimeError("transition has no Action request kind")

        return ApplyRecommendedRegime(
            evaluation_id=event.evaluation_id,
            regime_type=self.regime_type,
            source_candle_id=event.source_candle_id,
        )


TRANSITION_REGISTRY: tuple[RegimeTransition, ...] = (
    RegimeTransition(
        transition_id="EA-001",
        current_state=RegimeState.INITIAL,
        event_type=RegimeEventType.INITIAL_EVALUATION_REQUESTED,
        next_state=RegimeState.FOUR_HOUR_CANDLE_EVALUATION,
        evaluation_trigger=RegimeEvaluationTrigger.INITIAL,
    ),
    RegimeTransition(
        transition_id="EA-002",
        current_state=RegimeState.FOUR_HOUR_CANDLE_EVALUATION,
        event_type=RegimeEventType.EVALUATION_READY,
        next_state=RegimeState.TYPE_0_RECOMMENDED,
        guard=matches_sideways_regime,
        regime_type=RegimeType.TYPE_0,
    ),
    RegimeTransition(
        transition_id="EA-003",
        current_state=RegimeState.FOUR_HOUR_CANDLE_EVALUATION,
        event_type=RegimeEventType.EVALUATION_READY,
        next_state=RegimeState.TYPE_1_RECOMMENDED,
        guard=matches_weak_up_regime,
        regime_type=RegimeType.TYPE_1,
    ),
    RegimeTransition(
        transition_id="EA-004",
        current_state=RegimeState.FOUR_HOUR_CANDLE_EVALUATION,
        event_type=RegimeEventType.EVALUATION_READY,
        next_state=RegimeState.TYPE_1_RECOMMENDED,
        guard=matches_strong_up_fallback,
        regime_type=RegimeType.TYPE_1,
    ),
    RegimeTransition(
        transition_id="EA-005",
        current_state=RegimeState.FOUR_HOUR_CANDLE_EVALUATION,
        event_type=RegimeEventType.EVALUATION_READY,
        next_state=RegimeState.TYPE_2_RECOMMENDED,
        guard=matches_strong_up_regime,
        regime_type=RegimeType.TYPE_2,
    ),
    RegimeTransition(
        transition_id="EA-006",
        current_state=RegimeState.FOUR_HOUR_CANDLE_EVALUATION,
        event_type=RegimeEventType.EVALUATION_READY,
        next_state=RegimeState.TYPE_3_RECOMMENDED,
        guard=matches_weak_down_regime,
        regime_type=RegimeType.TYPE_3,
    ),
    RegimeTransition(
        transition_id="EA-007",
        current_state=RegimeState.FOUR_HOUR_CANDLE_EVALUATION,
        event_type=RegimeEventType.EVALUATION_READY,
        next_state=RegimeState.TYPE_3_RECOMMENDED,
        guard=matches_strong_down_fallback,
        regime_type=RegimeType.TYPE_3,
    ),
    RegimeTransition(
        transition_id="EA-008",
        current_state=RegimeState.FOUR_HOUR_CANDLE_EVALUATION,
        event_type=RegimeEventType.EVALUATION_READY,
        next_state=RegimeState.TYPE_4_RECOMMENDED,
        guard=matches_strong_down_regime,
        regime_type=RegimeType.TYPE_4,
    ),
    RegimeTransition(
        transition_id="EA-101",
        current_state=RegimeState.TYPE_0_RECOMMENDED,
        event_type=RegimeEventType.FOUR_HOUR_CANDLE_CLOSED,
        next_state=RegimeState.FOUR_HOUR_CANDLE_EVALUATION,
        evaluation_trigger=RegimeEvaluationTrigger.FOUR_HOUR_CANDLE_CLOSE,
    ),
    RegimeTransition(
        transition_id="EA-102",
        current_state=RegimeState.TYPE_1_RECOMMENDED,
        event_type=RegimeEventType.FOUR_HOUR_CANDLE_CLOSED,
        next_state=RegimeState.FOUR_HOUR_CANDLE_EVALUATION,
        evaluation_trigger=RegimeEvaluationTrigger.FOUR_HOUR_CANDLE_CLOSE,
    ),
    RegimeTransition(
        transition_id="EA-103",
        current_state=RegimeState.TYPE_2_RECOMMENDED,
        event_type=RegimeEventType.FOUR_HOUR_CANDLE_CLOSED,
        next_state=RegimeState.FOUR_HOUR_CANDLE_EVALUATION,
        evaluation_trigger=RegimeEvaluationTrigger.FOUR_HOUR_CANDLE_CLOSE,
    ),
    RegimeTransition(
        transition_id="EA-104",
        current_state=RegimeState.TYPE_3_RECOMMENDED,
        event_type=RegimeEventType.FOUR_HOUR_CANDLE_CLOSED,
        next_state=RegimeState.FOUR_HOUR_CANDLE_EVALUATION,
        evaluation_trigger=RegimeEvaluationTrigger.FOUR_HOUR_CANDLE_CLOSE,
    ),
    RegimeTransition(
        transition_id="EA-105",
        current_state=RegimeState.TYPE_4_RECOMMENDED,
        event_type=RegimeEventType.FOUR_HOUR_CANDLE_CLOSED,
        next_state=RegimeState.FOUR_HOUR_CANDLE_EVALUATION,
        evaluation_trigger=RegimeEvaluationTrigger.FOUR_HOUR_CANDLE_CLOSE,
    ),
)

SPECIFICATION_TRANSITION_IDS = frozenset(
    {
        "EA-001",
        "EA-002",
        "EA-003",
        "EA-004",
        "EA-005",
        "EA-006",
        "EA-007",
        "EA-008",
        "EA-101",
        "EA-102",
        "EA-103",
        "EA-104",
        "EA-105",
    }
)


def _validate_transition_registry(
    transitions: tuple[RegimeTransition, ...],
) -> None:
    """
    함수 이름: _validate_transition_registry()
    기능: registry의 transition ID가 명세의 13개 ID와 정확히 일치하는지 검증한다.
    인자: transitions -> 검증할 transition 행 모음
    반환값: 없음
    작성 날짜: 2026/08/14
    """
    transition_ids = tuple(
        transition.transition_id for transition in transitions
    )
    if len(transition_ids) != len(set(transition_ids)):
        raise RuntimeError("transition registry contains duplicate IDs")

    if frozenset(transition_ids) != SPECIFICATION_TRANSITION_IDS:
        raise RuntimeError("transition registry does not match specification")


def _build_transition_index(
    transitions: tuple[RegimeTransition, ...],
) -> Mapping[TransitionKey, tuple[RegimeTransition, ...]]:
    """
    함수 이름: _build_transition_index()
    기능: 현재 상태와 이벤트로 transition 후보를 조회할 불변 index를 만든다.
    인자: transitions -> index에 등록할 transition 행 모음
    반환값: 상태와 이벤트별 transition 후보 mapping
    작성 날짜: 2026/08/14
    """
    mutable_index: dict[TransitionKey, list[RegimeTransition]] = {}

    for transition in transitions:
        transition_key = (
            transition.current_state,
            transition.event_type,
        )
        mutable_index.setdefault(transition_key, []).append(transition)

    immutable_index = {
        transition_key: tuple(candidates)
        for transition_key, candidates in mutable_index.items()
    }
    return MappingProxyType(immutable_index)


_validate_transition_registry(TRANSITION_REGISTRY)
TRANSITION_IDS = frozenset(
    transition.transition_id for transition in TRANSITION_REGISTRY
)
_TRANSITIONS_BY_STATE_AND_EVENT = _build_transition_index(
    TRANSITION_REGISTRY
)


def get_transition_candidates(
    state: RegimeState,
    event_type: RegimeEventType,
) -> tuple[RegimeTransition, ...]:
    """
    함수 이름: get_transition_candidates()
    기능: 현재 상태와 이벤트가 일치하는 transition 후보를 반환한다.
    인자: state -> 이벤트 처리 전 현재 상태
        event_type -> 처리할 REGIME 이벤트 타입
    반환값: registry 순서를 보존한 transition 후보 tuple
    작성 날짜: 2026/08/14
    """
    if not isinstance(state, RegimeState):
        raise TypeError("state must be a RegimeState")

    if not isinstance(event_type, RegimeEventType):
        raise TypeError("event_type must be a RegimeEventType")

    return _TRANSITIONS_BY_STATE_AND_EVENT.get((state, event_type), ())

