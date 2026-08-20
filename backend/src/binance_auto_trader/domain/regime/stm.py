"""Event-Action registry를 실행하는 동기식 순수 RegimeSTM을 정의한다."""

from .evaluation import RegimeEvaluationContext
from .events import RegimeEvent, RegimeEventType
from .results import RegimeSTMResult
from .states import RegimeState
from .transitions import RegimeTransition, get_transition_candidates


class RegimeSTM:
    """
    클래스 이름: RegimeSTM
    기능: 현재 상태와 불변 입력으로 전이와 Action 요청을 결정한다.
    작성 날짜: 2026/08/14
    """

    def __init__(
        self,
        initial_state: RegimeState = RegimeState.INITIAL,
    ) -> None:
        """
        함수 이름: __init__()
        기능: REGIME 상태 머신을 지정된 초기 상태로 초기화한다.
        인자: initial_state -> 상태 머신이 시작할 REGIME 상태
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        if not isinstance(initial_state, RegimeState):
            raise TypeError("initial_state must be a RegimeState")

        self._current_state = initial_state

    @property
    def current_state(self) -> RegimeState:
        """
        함수 이름: current_state()
        기능: 상태 머신의 현재 상태를 읽기 전용으로 반환한다.
        인자: 없음
        반환값: 현재 REGIME 상태
        작성 날짜: 2026/08/14
        """
        return self._current_state

    def handle(
        self,
        event: RegimeEvent,
        context: RegimeEvaluationContext | None = None,
    ) -> RegimeSTMResult:
        """
        함수 이름: handle()
        기능: 한 이벤트에서 최대 하나의 transition과 Action 요청을 결정한다.
        인자: event -> 처리할 불변 REGIME 이벤트
            context -> EVALUATION_READY guard에 사용할 불변 평가 Context
        반환값: 전이와 Action 요청을 담은 RegimeSTMResult
        작성 날짜: 2026/08/14
        """
        if not isinstance(event, RegimeEvent):
            raise TypeError("event must be a RegimeEvent")

        state_before = self._current_state
        candidates = get_transition_candidates(
            state_before,
            event.event_type,
        )
        if not candidates:
            return self._create_no_op_result(event, state_before)

        self._validate_context(event, context)
        matching_transitions = tuple(
            transition
            for transition in candidates
            if transition.matches(context)
        )

        if not matching_transitions:
            return self._create_no_op_result(event, state_before)

        if len(matching_transitions) > 1:
            matching_ids = ", ".join(
                transition.transition_id
                for transition in matching_transitions
            )
            raise RuntimeError(
                f"multiple transitions matched one event: {matching_ids}"
            )

        selected_transition = matching_transitions[0]
        result = self._create_transition_result(
            event,
            state_before,
            selected_transition,
        )

        # 결과와 Action 요청 생성이 모두 성공한 뒤 상태를 원자적으로 적용한다.
        self._current_state = selected_transition.next_state
        return result

    def _validate_context(
        self,
        event: RegimeEvent,
        context: RegimeEvaluationContext | None,
    ) -> None:
        """
        함수 이름: _validate_context()
        기능: 이벤트와 평가 Context의 cycle 및 candle 식별자가 일치하는지 검증한다.
        인자: event -> 현재 처리할 REGIME 이벤트
            context -> 이벤트와 연결된 불변 평가 Context
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        if (
            event.event_type is RegimeEventType.EVALUATION_READY
            and context is None
        ):
            raise ValueError("EVALUATION_READY requires context")

        if context is None:
            return

        if not isinstance(context, RegimeEvaluationContext):
            raise TypeError("context must be a RegimeEvaluationContext or None")

        if event.evaluation_id != context.evaluation_id:
            raise ValueError("event and context evaluation_id must match")

        if event.source_candle_id != context.source_candle_id:
            raise ValueError("event and context source_candle_id must match")

    def _create_transition_result(
        self,
        event: RegimeEvent,
        state_before: RegimeState,
        transition: RegimeTransition,
    ) -> RegimeSTMResult:
        """
        함수 이름: _create_transition_result()
        기능: 선택된 transition의 추적 정보와 Action 요청을 결과로 만든다.
        인자: event -> transition을 발생시킨 REGIME 이벤트
            state_before -> transition 적용 전 상태
            transition -> registry에서 선택된 transition 행
        반환값: 소비된 이벤트의 RegimeSTMResult
        작성 날짜: 2026/08/14
        """
        action_request = transition.create_action(event)
        decision_id = self._create_decision_id(
            event,
            state_before,
            transition.transition_id,
        )
        return RegimeSTMResult(
            decision_id=decision_id,
            consumed=True,
            transition_id=transition.transition_id,
            state_before=state_before,
            state_after=transition.next_state,
            action_requests=(action_request,),
            evaluation_id=event.evaluation_id,
        )

    def _create_no_op_result(
        self,
        event: RegimeEvent,
        state: RegimeState,
    ) -> RegimeSTMResult:
        """
        함수 이름: _create_no_op_result()
        기능: 적용할 transition이 없는 이벤트의 명시적 no-op 결과를 만든다.
        인자: event -> 소비되지 않은 REGIME 이벤트
            state -> 이벤트 처리 전후에 유지할 현재 상태
        반환값: 소비되지 않은 이벤트의 RegimeSTMResult
        작성 날짜: 2026/08/14
        """
        decision_id = self._create_decision_id(
            event,
            state,
            None,
        )
        return RegimeSTMResult(
            decision_id=decision_id,
            consumed=False,
            transition_id=None,
            state_before=state,
            state_after=state,
            action_requests=(),
            evaluation_id=event.evaluation_id,
        )

    def _create_decision_id(
        self,
        event: RegimeEvent,
        state_before: RegimeState,
        transition_id: str | None,
    ) -> str:
        """
        함수 이름: _create_decision_id()
        기능: 동일 event와 상태에서 재현 가능한 결정 식별자를 만든다.
        인자: event -> 결정 대상 REGIME 이벤트
            state_before -> 결정 전 상태
            transition_id -> 선택된 Event-Action ID 또는 None
        반환값: 결정론적으로 생성한 decision ID
        작성 날짜: 2026/08/14
        """
        selected_id = transition_id or "NO-TRANSITION"
        return f"{event.event_id}:{state_before.name}:{selected_id}"

