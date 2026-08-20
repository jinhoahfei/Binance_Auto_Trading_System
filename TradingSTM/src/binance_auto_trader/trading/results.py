"""TradingSTM의 한 microstep이 반환하는 불변 결과를 정의한다."""

from dataclasses import dataclass

from .action_requests import TradingActionRequest
from .states import TradingStateConfiguration


@dataclass(frozen=True, slots=True)
class TradingSTMResult:
    """
    클래스 이름: TradingSTMResult
    기능: 원자적 상태 전이 trace와 순서가 보존된 Controller action request를 보존한다.
    작성 날짜: 2026/08/14
    """

    decision_id: str
    consumed: bool
    transition_ids: tuple[str, ...]
    state_before: TradingStateConfiguration
    state_after: TradingStateConfiguration
    action_requests: tuple[TradingActionRequest, ...]
    context_version: int
