"""각 Region transition 함수가 공유하는 내부 결과 타입을 정의한다."""

from dataclasses import dataclass

from ..action_requests import TradingActionRequest
from ..states import TradingStateConfiguration


@dataclass(frozen=True, slots=True)
class TransitionOutcome:
    """
    클래스 이름: TransitionOutcome
    기능: 한 Region이 선택한 transition ID, 다음 상태 및 action 후보를 보존한다.
    작성 날짜: 2026/08/14
    """

    transition_ids: tuple[str, ...]
    state_after: TradingStateConfiguration
    action_requests: tuple[TradingActionRequest, ...] = ()
    exclusive: bool = False


def create_transition_outcome(
    transition_id: str,
    state_after: TradingStateConfiguration,
    *actions: TradingActionRequest,
    extra_transition_ids: tuple[str, ...] = (),
    exclusive: bool = False,
) -> TransitionOutcome:
    """
    함수 이름: create_transition_outcome()
    기능: 단일 transition과 선택 action을 불변 TransitionOutcome으로 생성한다.
    인자: transition_id -> Event-Action Table transition ID
        state_after -> transition 이후 전체 상태 구성
        actions -> Controller가 순서대로 실행할 action request
        extra_transition_ids -> 같은 microstep의 추가 transition ID
        exclusive -> 이후 Region 평가를 중단할지 여부
    반환값: 생성된 TransitionOutcome
    작성 날짜: 2026/08/14
    """
    return TransitionOutcome(
        transition_ids=(transition_id, *extra_transition_ids),
        state_after=state_after,
        action_requests=tuple(actions),
        exclusive=exclusive,
    )
