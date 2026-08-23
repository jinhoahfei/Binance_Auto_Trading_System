"""Backend event sequence, bounded replay, gap와 Condition live wait를 검증한다."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Thread
import unittest

from binance_auto_trader.transport.contracts import MAX_WEBSOCKET_FRAME_BYTES
from binance_auto_trader.transport.event_stream import BackendEventStream


TEST_SESSION_ID = "0fc07e66-b879-41f8-b6ab-fc74821911ce"  # replay 범위를 한 session으로 고정한다.
INITIAL_TIME = datetime(2026, 8, 21, 3, 0, tzinfo=timezone.utc)


class _ControlledClock:
    """
    클래스 이름: _ControlledClock
    기능: event retention test가 현재 UTC를 결정론적으로 이동하게 한다.
    작성 날짜: 2026/08/21
    """

    def __init__(self, current_time: datetime) -> None:
        """
        함수 이름: __init__()
        기능: clock이 처음 반환할 timezone-aware UTC를 보존한다.
        인자: current_time -> 초기 UTC 시각
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.current_time = current_time

    def __call__(self) -> datetime:
        """
        함수 이름: __call__()
        기능: test가 설정한 현재 UTC를 반환한다.
        인자: 없음
        반환값: 현재 test UTC
        작성 날짜: 2026/08/21
        """
        return self.current_time


class _ControlledMonotonicClock:
    """
    클래스 이름: _ControlledMonotonicClock
    기능: wall-clock event 시각과 독립적으로 retention seconds를 제어한다.
    작성 날짜: 2026/08/21
    """

    def __init__(self, current_seconds: float = 100.0) -> None:
        """
        함수 이름: __init__()
        기능: monotonic clock의 초기 seconds를 보존한다.
        인자: current_seconds -> 초기 monotonic seconds
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.current_seconds = current_seconds

    def __call__(self) -> float:
        """
        함수 이름: __call__()
        기능: test가 설정한 monotonic seconds를 반환한다.
        인자: 없음
        반환값: 현재 monotonic seconds
        작성 날짜: 2026/08/21
        """
        return self.current_seconds


class BackendEventStreamTests(unittest.TestCase):
    """
    클래스 이름: BackendEventStreamTests
    기능: per-session sequence와 reconnect replay의 핵심 불변식을 검증한다.
    작성 날짜: 2026/08/21
    """

    def test_publish_assigns_full_envelope_and_monotonic_sequence(self) -> None:
        """
        함수 이름: test_publish_assigns_full_envelope_and_monotonic_sequence()
        기능: sequence 1 시작, UUID, Decimal string과 aggregate metadata를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        clock = _ControlledClock(INITIAL_TIME)
        event_stream = BackendEventStream(
            session_id=TEST_SESSION_ID,
            clock=clock,
        )

        first_event = event_stream.publish(
            "APPLICATION_READY",
            {"price": Decimal("4321.5000")},
            aggregate_version=7,
            correlation_id="startup-1",
        )
        second_event = event_stream.publish(
            "ACCOUNT_UPDATED",
            {"valuation": Decimal("7000.00")},
            aggregate_version=8,
        )

        self.assertEqual((first_event.sequence, second_event.sequence), (1, 2))
        self.assertEqual(first_event.to_dto()["payload"]["price"], "4321.5000")
        self.assertEqual(first_event.to_dto()["schema_version"], 2)
        self.assertEqual(first_event.to_dto()["aggregate_version"], 7)
        self.assertEqual(event_stream.last_sequence, 2)
        self.assertNotEqual(first_event.event_id, second_event.event_id)

    def test_publish_many_rejects_later_invalid_event_without_partial_commit(
        self,
    ) -> None:
        """
        함수 이름: test_publish_many_rejects_later_invalid_event_without_partial_commit()
        기능: batch 후반 envelope 오류가 앞 event의 sequence와 replay 공개를 남기지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        event_stream = BackendEventStream(
            session_id=TEST_SESSION_ID,
            clock=_ControlledClock(INITIAL_TIME),
        )

        # 첫 event는 정상이지만 두 번째 type을 비정규 값으로 만들어 commit 전 검증 실패를 유도한다.
        with self.assertRaisesRegex(
            ValueError,
            "event_type must use canonical uppercase text",
        ):
            event_stream.publish_many(
                (
                    ("ORDER_EXECUTED", {"order_id": "100"}),
                    ("performance-updated", {"daily_fee": "0.1"}),
                )
            )

        self.assertEqual(event_stream.last_sequence, 0)
        self.assertEqual(event_stream.replay_after(0).events, ())

    def test_count_retention_replays_or_requires_gap_resync(self) -> None:
        """
        함수 이름: test_count_retention_replays_or_requires_gap_resync()
        기능: count 한도 내 sequence는 replay하고 오래된 cursor는 REPLAY_GAP인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        event_stream = BackendEventStream(
            session_id=TEST_SESSION_ID,
            clock=_ControlledClock(INITIAL_TIME),
            max_replay_events=2,
        )
        for account_version in range(1, 4):
            event_stream.publish(
                "ACCOUNT_UPDATED",
                {"version": account_version},
                aggregate_version=account_version,
            )

        # Sequence 1은 제거됐으므로 1 이후는 2~3 replay, 0 이후는 full resync다.
        replay_batch = event_stream.replay_after(1)
        self.assertEqual(
            tuple(event.sequence for event in replay_batch.events),
            (2, 3),
        )
        self.assertFalse(replay_batch.requires_resync)
        gap_batch = event_stream.replay_after(0)
        self.assertTrue(gap_batch.requires_resync)
        self.assertEqual(gap_batch.resync_reason, "REPLAY_GAP")

    def test_time_retention_and_sequence_ahead_require_resync(self) -> None:
        """
        함수 이름: test_time_retention_and_sequence_ahead_require_resync()
        기능: 15분보다 오래된 event와 server보다 앞선 cursor가 각각 resync인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        clock = _ControlledClock(INITIAL_TIME)
        monotonic_clock = _ControlledMonotonicClock()
        event_stream = BackendEventStream(
            session_id=TEST_SESSION_ID,
            clock=clock,
            monotonic_clock=monotonic_clock,
        )
        event_stream.publish("APPLICATION_READY", {})

        clock.current_time = INITIAL_TIME + timedelta(minutes=15, microseconds=1)
        monotonic_clock.current_seconds += 15 * 60 + 0.000001
        aged_batch = event_stream.replay_after(0)
        ahead_batch = event_stream.replay_after(2)

        self.assertEqual(aged_batch.resync_reason, "REPLAY_GAP")
        self.assertEqual(ahead_batch.resync_reason, "SEQUENCE_AHEAD")
        self.assertEqual(
            event_stream.build_resync_control("REPLAY_GAP"),
            {
                "schema_version": 2,
                "session_id": TEST_SESSION_ID,
                "type": "RESYNC_REQUIRED",
                "reason": "REPLAY_GAP",
                "last_sequence": 1,
            },
        )

    def test_non_monotonic_occurred_at_does_not_corrupt_replay_retention(
        self,
    ) -> None:
        """
        함수 이름: test_non_monotonic_occurred_at_does_not_corrupt_replay_retention()
        기능: 지연 event의 과거 occurred_at이 publication 순서와 retention을 바꾸지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        monotonic_clock = _ControlledMonotonicClock()
        event_stream = BackendEventStream(
            session_id=TEST_SESSION_ID,
            monotonic_clock=monotonic_clock,
        )
        later_time = INITIAL_TIME + timedelta(hours=1)
        earlier_time = INITIAL_TIME - timedelta(days=1)

        event_stream.publish("ACCOUNT_UPDATED", {}, occurred_at=later_time)
        monotonic_clock.current_seconds += 1
        event_stream.publish("ACCOUNT_UPDATED", {}, occurred_at=earlier_time)
        replay_batch = event_stream.replay_after(0)

        self.assertEqual(
            tuple(event.sequence for event in replay_batch.events),
            (1, 2),
        )
        self.assertEqual(replay_batch.events[0].occurred_at, later_time)
        self.assertEqual(replay_batch.events[1].occurred_at, earlier_time)

    def test_condition_wait_wakes_for_live_publish_and_close(self) -> None:
        """
        함수 이름: test_condition_wait_wakes_for_live_publish_and_close()
        기능: live waiter가 publish와 close notify에서 즉시 깨어나는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        event_stream = BackendEventStream(session_id=TEST_SESSION_ID)
        received_batches = []

        def wait_for_first_event() -> None:
            """
            함수 이름: wait_for_first_event()
            기능: 별도 thread에서 sequence 0 이후 live event를 기다린다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/08/21
            """
            received_batches.append(
                event_stream.wait_for_events(0, timeout=2.0)
            )

        waiter_thread = Thread(target=wait_for_first_event)
        waiter_thread.start()
        event_stream.publish("ACCOUNT_UPDATED", {"version": 1})
        waiter_thread.join(timeout=2.0)

        self.assertFalse(waiter_thread.is_alive())
        self.assertEqual(received_batches[0].events[0].sequence, 1)
        event_stream.close()
        closed_batch = event_stream.wait_for_events(1, timeout=2.0)
        self.assertTrue(closed_batch.closed)

    def test_oversized_event_fails_without_consuming_sequence(self) -> None:
        """
        함수 이름: test_oversized_event_fails_without_consuming_sequence()
        기능: 1 MiB를 넘는 event frame이 sequence gap을 만들지 않고 거부되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        event_stream = BackendEventStream(session_id=TEST_SESSION_ID)
        oversized_payload = {"data": "x" * MAX_WEBSOCKET_FRAME_BYTES}

        with self.assertRaisesRegex(ValueError, "frame limit"):
            event_stream.publish("ACCOUNT_UPDATED", oversized_payload)

        self.assertEqual(event_stream.last_sequence, 0)
        valid_event = event_stream.publish("ACCOUNT_UPDATED", {"version": 1})
        self.assertEqual(valid_event.sequence, 1)


if __name__ == "__main__":
    unittest.main()
