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


# 주문 adapter가 만든 구체 결과만 TradingSTM.order_finished 경계를 통과시킨다.
_ORDER_FINISHED_EVENT_TYPES = frozenset(
    {
        TradingEventType.CASE_B_POSITION_OPENED,
        TradingEventType.CASE_B_BUY_FAILED,
        TradingEventType.CASE_B_SELL_FILLED,
        TradingEventType.CASE_B_SELL_FAILED,
        TradingEventType.CASE_C_POSITION_OPENED,
        TradingEventType.CASE_C_BUY_FAILED,
        TradingEventType.CASE_C_SELL_FILLED,
        TradingEventType.CASE_C_SELL_FAILED,
        TradingEventType.FORCE_SELL_FINISHED,
        TradingEventType.FORCE_SELL_FAILED,
    }
)


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


class EventQueueCapacityError(RuntimeError):
    """
    클래스 이름: EventQueueCapacityError
    기능: pending event나 dedup identity 한도를 넘은 세션을 fail closed한다.
    작성 날짜: 2026/08/21
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

    def __init__(
        self,
        *,
        max_pending_events: int = 10_000,
        max_seen_event_ids: int = 100_000,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 비어 있는 event heap과 순번·중복·동시성 관리 상태를 초기화한다.
        인자: max_pending_events -> 동시에 대기할 event 상한
            max_seen_event_ids -> 한 session에서 수락할 고유 event ID 상한
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        # bool을 정수로 인정하지 않고 운영 한도를 1 이상의 exact int로 제한한다.
        for limit_name, limit_value in (
            ("max_pending_events", max_pending_events),
            ("max_seen_event_ids", max_seen_event_ids),
        ):
            if isinstance(limit_value, bool) or not isinstance(limit_value, int):
                raise TypeError(f"{limit_name} must be an integer")
            if limit_value < 1:
                raise ValueError(f"{limit_name} must be positive")

        self._event_heap: list[_QueueEntry] = []
        self._next_sequence = 1
        self._seen_event_ids: set[str] = set()
        self._max_pending_events = max_pending_events
        self._max_seen_event_ids = max_seen_event_ids
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

            # 메모리를 늘리는 새 identity는 pending·session dedup 상한에서 fail closed한다.
            if len(self._event_heap) >= self._max_pending_events:
                raise EventQueueCapacityError("Pending event capacity was reached")
            if len(self._seen_event_ids) >= self._max_seen_event_ids:
                raise EventQueueCapacityError("Event identity capacity was reached")

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

    def restore_claimed(self, event: TradingEvent) -> None:
        """
        함수 이름: restore_claimed()
        기능: Context race로 publish하지 못한 claimed event를 기존 identity와 순서로 복구한다.
        인자: event -> pop됐지만 action이 실행되지 않은 queue event
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # Queue가 발급한 identity를 검사하기 전에 typed event 입력만 허용한다.
        if not isinstance(event, TradingEvent):
            raise TypeError("event must be a TradingEvent")

        # dedup set은 유지하면서 원래 우선순위와 FIFO sequence를 heap에 되돌린다.
        with self._lock:
            if event.event_id is None or event.event_id not in self._seen_event_ids:
                raise ValueError("Only a previously accepted event can be restored")
            if any(
                entry.event.event_id == event.event_id
                for entry in self._event_heap
            ):
                raise ValueError("Claimed event is already queued")
            heapq.heappush(
                self._event_heap,
                _QueueEntry(
                    priority=int(event.priority),
                    sequence_number=event.sequence_number,
                    event=event,
                ),
            )

    def clear(self) -> int:
        """
        함수 이름: clear()
        기능: 세션 중지 시 아직 처리하지 않은 event를 원자적으로 모두 폐기한다.
        인자: 없음
        반환값: queue에서 제거한 event 수
        작성 날짜: 2026/08/21
        """
        # 이미 처리한 ID dedup 기록은 유지해 stop 이후 같은 outcome 재수신도 차단한다.
        with self._lock:
            removed_count = len(self._event_heap)
            self._event_heap.clear()  # 대기 market·timer event 참조를 즉시 해제한다.
            return removed_count

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
OrderFinishedObserver: TypeAlias = Callable[
    [TradingEvent, TradingContextView],
    None,
]
EventProcessingObserver: TypeAlias = Callable[[TradingEvent | None], None]
EventContextPreparer: TypeAlias = Callable[[TradingEvent], TradingEvent]


class ActionExecutor(Protocol):
    """
    클래스 이름: ActionExecutor
    기능: 하나의 action을 순서대로 관찰·실행할 Controller callback 규약이다.
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
        order_finished_observer: OrderFinishedObserver | None = None,
        event_processing_observer: EventProcessingObserver | None = None,
        event_context_preparer: EventContextPreparer | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: STM, context 공급자, action 실행기와 세션 event queue를 연결한다.
        인자: stm -> 전이 판단을 수행할 TradingSTM
            context_provider -> 최신 불변 context snapshot을 제공하는 함수
            action_executor -> 외부 부수 효과 action을 실행하는 callback
            event_queue -> 사용할 직렬 queue, 생략하면 새 queue 생성
            clock -> 내부 event 발생 시각 공급 함수, 생략하면 현재 UTC 시각 사용
            order_finished_observer -> order_finished 성공을 관찰할 optional callback
            event_processing_observer -> action batch의 원 event 식별자를 열고 닫는 optional callback
            event_context_preparer -> claimed event의 immutable market 평가를 Context에 적용할 callback
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        # 선택 callback도 호출 가능한 값만 허용해 주문 microstep 중 타입 실패를 막는다.
        if order_finished_observer is not None and not callable(
            order_finished_observer
        ):
            raise TypeError("order_finished_observer must be callable or None")
        if event_processing_observer is not None and not callable(
            event_processing_observer
        ):
            raise TypeError("event_processing_observer must be callable or None")
        if event_context_preparer is not None and not callable(
            event_context_preparer
        ):
            raise TypeError("event_context_preparer must be callable or None")

        # 주입된 STM·Context·executor와 빈 queue까지 그대로 한 processor 세션에 보존한다.
        self._stm = stm
        self._context_provider = context_provider
        self._action_executor = action_executor
        self._queue = (
            event_queue
            if event_queue is not None
            else SerialEventQueue()
        )  # 빈 주입 queue도 동일 session identity로 반드시 보존한다.
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._order_finished_observer = order_finished_observer
        self._event_processing_observer = event_processing_observer
        self._event_context_preparer = event_context_preparer
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
            # Claimed market event의 immutable 평가를 먼저 적용해 뒤에 도착한 Kline과 source를 섞지 않는다.
            if self._event_context_preparer is not None:
                prepared_event = self._event_context_preparer(event)
                if not isinstance(prepared_event, TradingEvent):
                    raise TypeError(
                        "event_context_preparer must return a TradingEvent"
                    )
                event = prepared_event

            # 한 microstep은 준비된 동일 context snapshot만 사용하며 이전 lower event는 버린다.
            context = self._context_provider()
            if _is_stale_lower_event(event, context):
                return None
            if self._event_processing_observer is not None:
                self._event_processing_observer(
                    event
                )  # 이 microstep의 모든 action trace가 실제 event ID를 공유한다.

            # 주문 결과는 일반 event 처리와 구분해 Communication 메시지 14 adapter를 지난다.
            is_order_finished_event = event.event_type in _ORDER_FINISHED_EVENT_TYPES
            if is_order_finished_event:
                result = self._stm.order_finished(
                    event,
                    context,
                )  # Position과 durable history가 반영된 최신 Context만 전달한다.
            else:
                result = self._stm.handle(
                    event,
                    context,
                )  # 시장·timer·사용자 event는 기존 일반 경계를 유지한다.

            # 상태 판단 뒤 context가 바뀌면 어떤 외부 부수 효과도 실행하지 않는다.
            if self._context_provider().version != result.context_version:
                # Publish되지 않은 STM state를 되돌리고 claimed event identity도 queue에 복구한다.
                self._stm.rollback_unpublished_result(
                    result
                )  # Context와 STM이 서로 다른 version으로 남지 않게 원상 복구한다.
                self._queue.restore_claimed(
                    event
                )  # stable terminal outcome도 최신 Context에서 다시 처리할 수 있게 한다.
                raise ContextVersionError(
                    "Context changed between snapshot creation and action execution"
                )

            # STM 호출과 Context version 검증이 모두 성공한 경우에만 메시지 14 관찰을 알린다.
            if (
                is_order_finished_event
                and self._order_finished_observer is not None
            ):
                self._order_finished_observer(
                    event,
                    context,
                )  # queue 수락만으로 성공 trace를 만들지 않고 실제 microstep에서 기록한다.

            # 전체 action batch를 실행하되 내부 QueueEvent는 후속 삽입용으로 보류한다.
            queued_event_requests: list[QueueEvent] = []
            returned_events: list[TradingEvent] = []
            for action in result.action_requests:
                if isinstance(action, QueueEvent):
                    # QueueEvent도 원본 batch 순서로 executor에 노출한 뒤 내부 삽입만 보류한다.
                    action_result = self._action_executor(action)
                    if inspect.isawaitable(action_result):
                        action_result = await action_result
                    if action_result is not None:
                        returned_events.extend(action_result)
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
            if self._event_processing_observer is not None:
                self._event_processing_observer(None)

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
