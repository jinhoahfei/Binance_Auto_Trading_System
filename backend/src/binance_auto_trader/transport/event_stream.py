"""Backend session event의 sequence, replay와 live wait를 관리한다."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import re
from threading import Condition, RLock
from time import monotonic
from typing import Literal
from uuid import UUID, uuid4

from .contracts import (
    JsonObject,
    MAX_UNSIGNED_SEQUENCE,
    MAX_WEBSOCKET_FRAME_BYTES,
    SCHEMA_VERSION,
    datetime_to_wire,
    json_bytes,
    normalize_json_object,
    validate_uuid_text,
)


MAX_REPLAY_EVENTS = 10_000
MAX_REPLAY_AGE = timedelta(minutes=15)
_EVENT_TYPE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_MAX_CORRELATION_ID_LENGTH = 256

ResyncReason = Literal["REPLAY_GAP", "SEQUENCE_AHEAD"]


def _utc_now() -> datetime:
    """
    함수 이름: _utc_now()
    기능: event 발생 및 replay 보존 시각에 사용할 현재 UTC를 반환한다.
    인자: 없음
    반환값: timezone-aware UTC datetime
    작성 날짜: 2026/08/21
    """
    return datetime.now(timezone.utc)  # backend process의 현재 UTC를 읽는다.


def _normalize_event_time(value: object, field_name: str) -> datetime:
    """
    함수 이름: _normalize_event_time()
    기능: event stream clock 값을 timezone-aware UTC datetime으로 정규화한다.
    인자: value -> 검증할 clock 또는 event 시각
        field_name -> 오류 메시지에 표시할 필드 이름
    반환값: timezone.utc datetime
    작성 날짜: 2026/08/21
    """
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")

    return value.astimezone(timezone.utc)


def _validate_sequence(value: object, field_name: str) -> int:
    """
    함수 이름: _validate_sequence()
    기능: transport sequence가 unsigned 64-bit 정수인지 검증한다.
    인자: value -> 검증할 sequence
        field_name -> 오류 메시지에 표시할 필드 이름
    반환값: 검증된 sequence 정수
    작성 날짜: 2026/08/21
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < 0 or value > MAX_UNSIGNED_SEQUENCE:
        raise ValueError(f"{field_name} must be an unsigned 64-bit integer")

    return value


@dataclass(frozen=True, slots=True)
class BackendEventEnvelope:
    """
    클래스 이름: BackendEventEnvelope
    기능: ADR-005의 모든 필드를 가진 단일 immutable backend event를 표현한다.
    작성 날짜: 2026/08/21
    """

    session_id: str
    event_id: str
    sequence: int
    occurred_at: datetime
    event_type: str
    aggregate_version: int | None
    correlation_id: str | None
    payload: JsonObject

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: event 식별자, sequence, 시각, type과 payload 계약을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        validate_uuid_text(self.session_id, "session_id")
        validate_uuid_text(self.event_id, "event_id")
        _validate_sequence(self.sequence, "sequence")
        if self.sequence == 0:
            raise ValueError("event sequence must start at one")

        normalized_time = _normalize_event_time(self.occurred_at, "occurred_at")
        object.__setattr__(self, "occurred_at", normalized_time)

        # 제한된 대문자 event type만 허용해 route와 log template을 안정화한다.
        if (
            not isinstance(self.event_type, str)
            or _EVENT_TYPE_PATTERN.fullmatch(self.event_type) is None
        ):
            raise ValueError("event_type must use canonical uppercase text")
        if self.aggregate_version is not None:
            _validate_sequence(self.aggregate_version, "aggregate_version")
        if self.correlation_id is not None:
            if not isinstance(self.correlation_id, str):
                raise TypeError("correlation_id must be a string or None")
            if (
                not self.correlation_id.strip()
                or len(self.correlation_id) > _MAX_CORRELATION_ID_LENGTH
                or any(ord(character) < 32 for character in self.correlation_id)
            ):
                raise ValueError("correlation_id is not safe transport text")

        normalized_payload = normalize_json_object(self.payload)
        object.__setattr__(self, "payload", normalized_payload)

    def to_dto(self) -> JsonObject:
        """
        함수 이름: to_dto()
        기능: event envelope를 WebSocket에 전송할 versioned JSON object로 변환한다.
        인자: 없음
        반환값: ADR-005 event envelope DTO
        작성 날짜: 2026/08/21
        """
        return {
            "schema_version": SCHEMA_VERSION,
            "session_id": self.session_id,
            "event_id": self.event_id,
            "sequence": self.sequence,
            "occurred_at": datetime_to_wire(self.occurred_at),
            "type": self.event_type,
            "aggregate_version": self.aggregate_version,
            "correlation_id": self.correlation_id,
            "payload": self.payload,
        }


@dataclass(frozen=True, slots=True)
class ReplayBatch:
    """
    클래스 이름: ReplayBatch
    기능: reconnect replay 결과와 full resync 필요 여부를 함께 반환한다.
    작성 날짜: 2026/08/21
    """

    events: tuple[BackendEventEnvelope, ...]
    last_sequence: int
    resync_reason: ResyncReason | None = None
    closed: bool = False

    @property
    def requires_resync(self) -> bool:
        """
        함수 이름: requires_resync()
        기능: replay gap 또는 sequence 역행으로 snapshot 재조회가 필요한지 반환한다.
        인자: 없음
        반환값: RESYNC_REQUIRED control frame 필요 여부
        작성 날짜: 2026/08/21
        """
        return self.resync_reason is not None  # reason 존재 여부가 resync 상태다.


@dataclass(frozen=True, slots=True)
class _StoredEvent:
    """
    클래스 이름: _StoredEvent
    기능: 공개 occurred_at과 분리된 monotonic publication 시각으로 retention을 판정한다.
    작성 날짜: 2026/08/21
    """

    envelope: BackendEventEnvelope
    published_at_monotonic: float


class BackendEventStream:
    """
    클래스 이름: BackendEventStream
    기능: 한 process session의 event sequence, bounded replay와 live Condition을 관리한다.
    작성 날짜: 2026/08/21
    """

    def __init__(
        self,
        *,
        session_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        max_replay_events: int = MAX_REPLAY_EVENTS,
        max_replay_age: timedelta = MAX_REPLAY_AGE,
    ) -> None:
        """
        함수 이름: __init__()
        기능: sequence 0에서 시작하는 per-launch replay stream을 생성한다.
        인자: session_id -> 주입하거나 새로 생성할 process session UUID
            clock -> event 발생 및 retention 판정용 UTC clock
            monotonic_clock -> wall clock 조정과 무관한 retention clock
            max_replay_events -> replay에 보존할 최대 event 수
            max_replay_age -> replay에 보존할 최대 event 나이
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        selected_session_id = str(uuid4()) if session_id is None else session_id
        validate_uuid_text(selected_session_id, "session_id")
        selected_clock = _utc_now if clock is None else clock
        selected_monotonic_clock = monotonic if monotonic_clock is None else monotonic_clock
        if not callable(selected_clock):
            raise TypeError("clock must be callable")
        if not callable(selected_monotonic_clock):
            raise TypeError("monotonic_clock must be callable")
        if isinstance(max_replay_events, bool) or not isinstance(
            max_replay_events,
            int,
        ):
            raise TypeError("max_replay_events must be an integer")
        if max_replay_events <= 0 or max_replay_events > MAX_REPLAY_EVENTS:
            raise ValueError("max_replay_events must be between 1 and 10000")
        if not isinstance(max_replay_age, timedelta):
            raise TypeError("max_replay_age must be a timedelta")
        if max_replay_age <= timedelta(0) or max_replay_age > MAX_REPLAY_AGE:
            raise ValueError("max_replay_age must be between zero and 15 minutes")

        # Condition과 buffer는 동일 RLock을 사용해 publish와 replay를 선형화한다.
        self._session_id = selected_session_id
        self._clock = selected_clock
        self._monotonic_clock = selected_monotonic_clock
        self._max_replay_events = max_replay_events
        self._max_replay_age = max_replay_age
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._events: deque[_StoredEvent] = deque()
        self._last_sequence = 0
        self._closed = False

    @property
    def session_id(self) -> str:
        """
        함수 이름: session_id()
        기능: 이 stream에만 유효한 backend process session UUID를 반환한다.
        인자: 없음
        반환값: per-launch session UUID
        작성 날짜: 2026/08/21
        """
        return self._session_id  # session ID는 생성 뒤 바뀌지 않는다.

    @property
    def last_sequence(self) -> int:
        """
        함수 이름: last_sequence()
        기능: 지금까지 발급한 마지막 transport sequence를 원자적으로 반환한다.
        인자: 없음
        반환값: event가 없으면 0, 있으면 마지막 sequence
        작성 날짜: 2026/08/21
        """
        with self._lock:
            return self._last_sequence

    @property
    def closed(self) -> bool:
        """
        함수 이름: closed()
        기능: 신규 publish와 live wait가 종료됐는지 반환한다.
        인자: 없음
        반환값: stream 종료 여부
        작성 날짜: 2026/08/21
        """
        with self._lock:
            return self._closed

    def publish(
        self,
        event_type: str,
        payload: Mapping[str, object],
        *,
        aggregate_version: int | None = None,
        correlation_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> BackendEventEnvelope:
        """
        함수 이름: publish()
        기능: 다음 sequence를 발급해 application event를 replay buffer와 live reader에 공개한다.
        인자: event_type -> canonical 대문자 backend event type
            payload -> DTO mapper를 거친 event payload
            aggregate_version -> 관련 aggregate version 또는 None
            correlation_id -> command 또는 decision ID 또는 None
            occurred_at -> 테스트에서 주입할 UTC event 시각
        반환값: 발급한 immutable BackendEventEnvelope
        작성 날짜: 2026/08/21
        """
        normalized_payload = normalize_json_object(payload)

        with self._condition:
            if self._closed:
                raise RuntimeError("event stream is closed")
            if self._last_sequence == MAX_UNSIGNED_SEQUENCE:
                raise OverflowError("transport sequence exhausted unsigned 64-bit range")

            # sequence와 event time을 같은 임계 구역에서 정해 publication 순서를 고정한다.
            event_time = _normalize_event_time(
                self._clock() if occurred_at is None else occurred_at,
                "occurred_at",
            )
            publication_time = self._read_monotonic_time()
            next_sequence = self._last_sequence + 1
            envelope = BackendEventEnvelope(
                session_id=self._session_id,
                event_id=str(uuid4()),
                sequence=next_sequence,
                occurred_at=event_time,
                event_type=event_type,
                aggregate_version=aggregate_version,
                correlation_id=correlation_id,
                payload=normalized_payload,
            )
            encoded_envelope = json_bytes(envelope.to_dto())
            if len(encoded_envelope) > MAX_WEBSOCKET_FRAME_BYTES:
                raise ValueError("event envelope exceeds the WebSocket frame limit")
            self._events.append(
                _StoredEvent(
                    envelope=envelope,
                    published_at_monotonic=publication_time,
                )
            )
            self._last_sequence = next_sequence
            self._prune_locked(publication_time)
            self._condition.notify_all()

        return envelope

    def replay_after(self, after_sequence: int) -> ReplayBatch:
        """
        함수 이름: replay_after()
        기능: 지정 sequence 다음의 보존 event 또는 RESYNC_REQUIRED 판정을 반환한다.
        인자: after_sequence -> UI가 마지막으로 적용한 sequence
        반환값: ordered replay event와 gap 판정을 담은 ReplayBatch
        작성 날짜: 2026/08/21
        """
        validated_sequence = _validate_sequence(after_sequence, "after_sequence")

        with self._lock:
            current_time = self._read_monotonic_time()
            self._prune_locked(current_time)
            return self._replay_after_locked(validated_sequence)

    def wait_for_events(
        self,
        after_sequence: int,
        *,
        timeout: float | None = None,
    ) -> ReplayBatch:
        """
        함수 이름: wait_for_events()
        기능: replay 가능한 다음 event, resync 또는 stream 종료까지 Condition으로 대기한다.
        인자: after_sequence -> reader가 마지막으로 전송한 sequence
            timeout -> 최대 대기 초 또는 무기한이면 None
        반환값: replay/live event와 종료 상태를 담은 ReplayBatch
        작성 날짜: 2026/08/21
        """
        validated_sequence = _validate_sequence(after_sequence, "after_sequence")
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                raise TypeError("timeout must be a number or None")
            if timeout < 0:
                raise ValueError("timeout must not be negative")

        with self._condition:
            # 이미 replay 또는 resync 결과가 있으면 대기하지 않고 즉시 반환한다.
            current_time = self._read_monotonic_time()
            self._prune_locked(current_time)
            replay_batch = self._replay_after_locked(validated_sequence)
            if replay_batch.events or replay_batch.requires_resync or self._closed:
                return replay_batch

            self._condition.wait(timeout)
            current_time = self._read_monotonic_time()
            self._prune_locked(current_time)
            return self._replay_after_locked(validated_sequence)

    def close(self) -> None:
        """
        함수 이름: close()
        기능: 신규 publish를 차단하고 대기 중인 모든 live reader를 깨운다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        with self._condition:
            self._closed = True  # 여러 번 호출해도 같은 종료 상태를 유지한다.
            self._condition.notify_all()

    def build_resync_control(self, reason: ResyncReason) -> JsonObject:
        """
        함수 이름: build_resync_control()
        기능: application sequence를 소비하지 않는 RESYNC_REQUIRED control DTO를 생성한다.
        인자: reason -> replay 불가능한 이유
        반환값: UI가 snapshot-first를 다시 시작할 control frame
        작성 날짜: 2026/08/21
        """
        if reason not in ("REPLAY_GAP", "SEQUENCE_AHEAD"):
            raise ValueError("reason must be a canonical resync reason")

        with self._lock:
            return {
                "schema_version": SCHEMA_VERSION,
                "session_id": self._session_id,
                "type": "RESYNC_REQUIRED",
                "reason": reason,
                "last_sequence": self._last_sequence,
            }

    def _read_monotonic_time(self) -> float:
        """
        함수 이름: _read_monotonic_time()
        기능: retention clock이 유한한 0 이상 seconds인지 검증한다.
        인자: 없음
        반환값: monotonic seconds
        작성 날짜: 2026/08/21
        """
        monotonic_value = self._monotonic_clock()
        if isinstance(monotonic_value, bool) or not isinstance(
            monotonic_value,
            (int, float),
        ):
            raise TypeError("monotonic clock must return a number")
        normalized_value = float(monotonic_value)
        if not math.isfinite(normalized_value) or normalized_value < 0:
            raise ValueError("monotonic clock must return finite non-negative seconds")

        return normalized_value

    def _prune_locked(self, current_time: float) -> None:
        """
        함수 이름: _prune_locked()
        기능: 호출자가 lock을 보유한 상태에서 15분과 count retention을 함께 적용한다.
        인자: current_time -> replay retention을 판정할 monotonic seconds
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        replay_cutoff = current_time - self._max_replay_age.total_seconds()

        # 둘 중 먼저 도달한 한도를 적용하도록 age와 count를 모두 제거 조건으로 사용한다.
        while (
            self._events
            and self._events[0].published_at_monotonic < replay_cutoff
        ):
            self._events.popleft()
        while len(self._events) > self._max_replay_events:
            self._events.popleft()

    def _replay_after_locked(self, after_sequence: int) -> ReplayBatch:
        """
        함수 이름: _replay_after_locked()
        기능: 호출자가 lock을 보유한 상태에서 ordered replay와 gap을 판정한다.
        인자: after_sequence -> UI가 마지막으로 적용한 sequence
        반환값: 현재 buffer 기준 ReplayBatch
        작성 날짜: 2026/08/21
        """
        if after_sequence > self._last_sequence:
            return ReplayBatch(
                events=(),
                last_sequence=self._last_sequence,
                resync_reason="SEQUENCE_AHEAD",
                closed=self._closed,
            )
        if after_sequence == self._last_sequence:
            return ReplayBatch(
                events=(),
                last_sequence=self._last_sequence,
                closed=self._closed,
            )

        # 요청한 다음 sequence가 retention buffer보다 오래됐으면 이어 붙이지 않는다.
        if not self._events:
            return ReplayBatch(
                events=(),
                last_sequence=self._last_sequence,
                resync_reason="REPLAY_GAP",
                closed=self._closed,
            )
        earliest_sequence = self._events[0].envelope.sequence
        if after_sequence + 1 < earliest_sequence:
            return ReplayBatch(
                events=(),
                last_sequence=self._last_sequence,
                resync_reason="REPLAY_GAP",
                closed=self._closed,
            )

        replay_events = tuple(
            stored_event.envelope
            for stored_event in self._events
            if stored_event.envelope.sequence > after_sequence
        )
        return ReplayBatch(
            events=replay_events,
            last_sequence=self._last_sequence,
            closed=self._closed,
        )
