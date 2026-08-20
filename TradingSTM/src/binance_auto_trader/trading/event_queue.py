"""Controller 측 직렬 event queue와 run-to-completion 처리기를 제공한다."""

from __future__ import annotations

import heapq
import inspect
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from threading import Lock
from typing import Protocol, TypeAlias

from .action_requests import QueueEvent, TradingActionRequest
from .context import TradingContextView
from .events import EventPriority, TradingEvent, TradingEventType
from .results import TradingSTMResult
from .stm import TradingSTM


class ContextVersionError(RuntimeError):
    """
    클래스 이름: ContextVersionError
    기능: STM 평가 중 context snapshot 버전이 바뀌었음을 알린다.
    작성 날짜: 2026/08/14
    """


class ReentrantProcessingError(RuntimeError):
    """
    클래스 이름: ReentrantProcessingError
    기능: action 실행 중 재귀적인 event 처리가 시도되었음을 알린다.
    작성 날짜: 2026/08/14
    """


@dataclass(order=True, slots=True)
class _QueueEntry:
    """
    클래스 이름: _QueueEntry
    기능: 우선순위 구간과 전역 FIFO 순번으로 정렬되는 heap 항목을 표현한다.
    작성 날짜: 2026/08/14
    """

    priority: int
    sequence_number: int
    event: TradingEvent = field(compare=False)


class SerialEventQueue:
    """
    클래스 이름: SerialEventQueue
    기능: 내부 microstep을 외부 event보다 먼저 처리하는 thread-safe 직렬 queue이다.
    작성 날짜: 2026/08/14
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 비어 있는 event heap과 순번·중복·동시성 관리 상태를 초기화한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        self._event_heap: list[_QueueEntry] = []
        self._next_sequence = 1
        self._seen_event_ids: set[str] = set()
        self._lock = Lock()

    def enqueue(
        self,
        event: TradingEvent,
        *,
        internal: bool = False,
    ) -> TradingEvent | None:
        """
        함수 이름: enqueue()
        기능: event에 FIFO 순번을 부여하고 중복을 제거한 뒤 원자적으로 저장한다.
        인자: event -> queue에 추가할 원본 TradingEvent
            internal -> 내부 microstep 우선순위를 강제로 적용할지 여부
        반환값: 식별 정보가 부여된 event 또는 중복 event일 때 None
        작성 날짜: 2026/08/14
        """
        # 순번 할당, 중복 검사, heap 삽입을 하나의 임계 구역에서 수행한다.
        with self._lock:
            sequence_number = self._next_sequence
            queued_event = event.with_queue_identity(sequence_number)

            # 같은 event ID를 이미 한 번 수락했다면 재수신한 입력을 버린다.
            if queued_event.event_id in self._seen_event_ids:
                return None

            self._next_sequence += 1
            self._seen_event_ids.add(queued_event.event_id)

            # 내부 후속 event는 아직 대기 중인 모든 외부 event보다 먼저 처리한다.
            if internal:
                queued_event = replace(queued_event, priority=EventPriority.INTERNAL)

            heapq.heappush(
                self._event_heap,
                _QueueEntry(
                    priority=int(queued_event.priority),
                    sequence_number=sequence_number,
                    event=queued_event,
                ),
            )
            return queued_event

    def pop(self) -> TradingEvent | None:
        """
        함수 이름: pop()
        기능: 우선순위와 FIFO 순번상 가장 앞선 event를 비차단 방식으로 꺼낸다.
        인자: 없음
        반환값: 다음 TradingEvent 또는 queue가 비었을 때 None
        작성 날짜: 2026/08/14
        """
        with self._lock:
            if not self._event_heap:
                return None

            return heapq.heappop(self._event_heap).event

    def __len__(self) -> int:
        """
        함수 이름: __len__()
        기능: 현재 처리 대기 중인 event 수를 thread-safe 방식으로 조회한다.
        인자: 없음
        반환값: queue에 저장된 event 수
        작성 날짜: 2026/08/14
        """
        with self._lock:
            return len(self._event_heap)


ActionExecutorResult: TypeAlias = Iterable[TradingEvent] | None


class ActionExecutor(Protocol):
    """
    클래스 이름: ActionExecutor
    기능: QueueEvent가 아닌 하나의 action을 실행할 Controller callback 규약이다.
    작성 날짜: 2026/08/14
    """

    def __call__(
        self,
        action: TradingActionRequest,
    ) -> ActionExecutorResult | Awaitable[ActionExecutorResult]:
        """
        함수 이름: __call__()
        기능: 하나의 action을 실행하고 선택적으로 정규화 결과 event를 반환한다.
        인자: action -> Controller가 실행할 TradingActionRequest
        반환값: 결과 event iterable, 이를 반환하는 awaitable 또는 None
        작성 날짜: 2026/08/14
        """
        ...


class RunToCompletionEventProcessor:
    """
    클래스 이름: RunToCompletionEventProcessor
    기능: 재귀 STM 호출 없이 한 event와 그 전체 action batch를 끝까지 처리한다.
    작성 날짜: 2026/08/14
    """

    def __init__(
        self,
        *,
        stm: TradingSTM,
        context_provider: Callable[[], TradingContextView],
        action_executor: ActionExecutor,
        event_queue: SerialEventQueue | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: STM, context 공급자, action 실행기와 세션 event queue를 연결한다.
        인자: stm -> 전이 판단을 수행할 TradingSTM
            context_provider -> 최신 불변 context snapshot을 제공하는 함수
            action_executor -> 외부 부수 효과 action을 실행하는 callback
            event_queue -> 사용할 직렬 queue, 생략하면 새 queue 생성
            clock -> 내부 event 발생 시각 공급 함수, 생략하면 현재 UTC 시각 사용
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        self._stm = stm
        self._context_provider = context_provider
        self._action_executor = action_executor
        self._queue = event_queue or SerialEventQueue()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._processing = False

    @property
    def event_queue(self) -> SerialEventQueue:
        """
        함수 이름: event_queue()
        기능: 외부 경계 adapter가 event를 넣을 수 있도록 세션 queue를 조회한다.
        인자: 없음
        반환값: 현재 세션의 SerialEventQueue
        작성 날짜: 2026/08/14
        """
        return self._queue

    async def process_next(self) -> TradingSTMResult | None:
        """
        함수 이름: process_next()
        기능: queue의 event 하나와 그 action batch를 처리한 뒤 내부 event를 예약한다.
        인자: 없음
        반환값: 처리한 TradingSTMResult 또는 대기 event가 없거나 stale이면 None
        작성 날짜: 2026/08/14
        """
        # action executor가 이 처리기를 다시 호출해 순서를 꼬이게 하지 못하도록 막는다.
        if self._processing:
            raise ReentrantProcessingError("Event processing cannot be recursive")

        event = self._queue.pop()
        if event is None:
            return None

        self._processing = True
        try:
            # 한 microstep은 동일한 context snapshot만 사용하며 이전 lower event는 버린다.
            context = self._context_provider()
            if _is_stale_lower_event(event, context):
                return None

            result = self._stm.handle(event, context)

            # 상태 판단 뒤 context가 바뀌면 어떤 외부 부수 효과도 실행하지 않는다.
            if self._context_provider().version != result.context_version:
                raise ContextVersionError(
                    "Context changed between snapshot creation and action execution"
                )

            # 전체 action batch를 실행하되 내부 QueueEvent는 후속 삽입용으로 보류한다.
            queued_event_requests: list[QueueEvent] = []
            returned_events: list[TradingEvent] = []
            for action in result.action_requests:
                if isinstance(action, QueueEvent):
                    queued_event_requests.append(action)
                    continue

                action_result = self._action_executor(action)
                if inspect.isawaitable(action_result):
                    action_result = await action_result
                if action_result is not None:
                    returned_events.extend(action_result)

            # action batch가 끝난 뒤에만 후속 event를 INTERNAL 우선순위로 삽입한다.
            latest_context = self._context_provider()
            for queued_event_request in queued_event_requests:
                self._queue.enqueue(
                    TradingEvent(
                        event_type=queued_event_request.event_type,
                        occurred_at=self._clock(),
                        priority=EventPriority.INTERNAL,
                        lower_event_id=(
                            queued_event_request.lower_event_id
                            or latest_context.runtime.lower_event_id
                            or event.lower_event_id
                        ),
                        candle_id=queued_event_request.candle_id,
                        order_id=queued_event_request.order_id,
                        payload=queued_event_request.payload,
                    ),
                    internal=True,
                )

            # 외부 action의 정규화 결과도 같은 run-to-completion 연쇄에 포함한다.
            for returned_event in returned_events:
                self._queue.enqueue(returned_event, internal=True)

            return result
        finally:
            # 실패 여부와 무관하게 다음 top-level event가 처리될 수 있도록 복원한다.
            self._processing = False

    async def drain(self, *, max_microsteps: int = 10_000) -> list[TradingSTMResult]:
        """
        함수 이름: drain()
        기능: 잘못된 adapter의 무한 event 생성을 제한하며 현재 queue를 모두 처리한다.
        인자: max_microsteps -> 한 번의 호출에서 허용할 최대 처리 횟수
        반환값: 실제로 처리한 TradingSTMResult 목록
        작성 날짜: 2026/08/14
        """
        results: list[TradingSTMResult] = []

        # 매 회 queue 길이를 다시 확인해 처리 중 생성된 내부 event까지 이어서 소비한다.
        for _ in range(max_microsteps):
            if len(self._queue) == 0:
                return results

            result = await self.process_next()
            if result is not None:
                results.append(result)

        raise RuntimeError("Event queue exceeded max_microsteps")


def _is_stale_lower_event(
    event: TradingEvent,
    context: TradingContextView,
) -> bool:
    """
    함수 이름: _is_stale_lower_event()
    기능: 이미 교체된 lower-touch event에 종속된 오래된 작업인지 판단한다.
    인자: event -> 유효 범위를 확인할 처리 대상 event
        context -> 현재 활성 lower event 식별자를 가진 context snapshot
    반환값: event가 이전 lower event에 속해 버려야 하면 True
    작성 날짜: 2026/08/14
    """
    # 세션 수명주기를 제어하는 전역 event는 lower event 범위와 무관하게 처리한다.
    if event.event_type in {
        TradingEventType.LOGIC_STARTED,
        TradingEventType.STOP_CONFIRMED,
        TradingEventType.FORCE_SELL_FINISHED,
        TradingEventType.FORCE_SELL_FAILED,
    }:
        return False

    # 양쪽 식별자가 모두 있을 때만 불일치를 stale로 판정한다.
    active_lower_event_id = context.runtime.lower_event_id
    return (
        event.lower_event_id is not None
        and active_lower_event_id is not None
        and event.lower_event_id != active_lower_event_id
    )
