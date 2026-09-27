"""Snapshot의 원자성을 유지하면서 잠금 대기와 요청 수를 제한하는지 검증한다."""

from threading import Condition, Event, Lock, Thread
import unittest
from unittest.mock import patch

from binance_auto_trader.transport.event_stream import BackendEventStream
from binance_auto_trader.transport.routes import RouteContext
from binance_auto_trader.transport.routes.snapshot import get_snapshot

from tests.unit.transport.test_contracts import _create_ready_runtime


REQUEST_ID = "9f9408c9-c9a3-4e80-82b3-3573054aeb40"


class _ContendedLock:
    """
    클래스 이름: _ContendedLock
    기능: 실제 잠금 대기를 만들고 진입한 요청 수를 조건 변수로 관찰한다.
    작성 날짜: 2026/09/22
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 테스트가 해제할 잠금을 선점하고 요청 관찰 조건을 준비한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        self._lock = Lock()
        self._lock.acquire()
        self._condition = Condition()
        self.attempts = 0
        self.timeouts: list[float] = []

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        """
        함수 이름: acquire()
        기능: 제한 시간을 보존해 실제 잠금을 기다리고 요청 진입을 통지한다.
        인자: blocking -> 잠금 대기 여부, timeout -> 대기 상한 초
        반환값: 잠금 획득 성공 여부
        작성 날짜: 2026/09/22
        """
        with self._condition:
            self.attempts += 1
            self.timeouts.append(timeout)
            self._condition.notify_all()

        return self._lock.acquire(blocking=blocking, timeout=timeout)

    def release(self) -> None:
        """
        함수 이름: release()
        기능: 테스트 또는 route가 보유한 실제 잠금을 해제한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        self._lock.release()

    def wait_for_attempts(self, count: int) -> bool:
        """
        함수 이름: wait_for_attempts()
        기능: 지정한 수의 route 요청이 잠금 획득을 시작할 때까지 기다린다.
        인자: count -> 관찰할 누적 요청 수
        반환값: 제한 시간 안에 요청이 도착했는지 여부
        작성 날짜: 2026/09/22
        """
        with self._condition:
            return self._condition.wait_for(lambda: self.attempts >= count, 1.0)

    def __enter__(self) -> object:
        """
        함수 이름: __enter__()
        기능: 기존 무제한 대기 구현도 같은 테스트 대역으로 재현한다.
        인자: 없음
        반환값: 잠금을 보유한 자기 자신
        작성 날짜: 2026/09/22
        """
        self.acquire()
        return self

    def __exit__(self, *exception: object) -> None:
        """
        함수 이름: __exit__()
        기능: context manager로 획득한 잠금을 해제한다.
        인자: exception -> context에서 발생한 예외 정보
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        self.release()


class SnapshotRouteTests(unittest.TestCase):
    """
    클래스 이름: SnapshotRouteTests
    기능: 외부 통신 없이 snapshot의 잠금 상한·동시 진입·예외 정리를 검증한다.
    작성 날짜: 2026/09/22
    """

    def test_contended_snapshot_returns_retryable_failure_before_lock_release(self) -> None:
        """
        함수 이름: test_contended_snapshot_returns_retryable_failure_before_lock_release()
        기능: application 잠금이 계속 점유돼도 1초 제한으로 503을 반환한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        runtime = _create_ready_runtime()
        application_lock = _ContendedLock()
        runtime.application_lock = application_lock
        context = RouteContext(runtime, BackendEventStream())
        responses = []
        finished = Event()

        def read_snapshot() -> None:
            """
            함수 이름: read_snapshot()
            기능: 별도 thread의 snapshot 응답과 완료 시점을 보존한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/09/22
            """
            try:
                responses.append(get_snapshot(REQUEST_ID, context))
            finally:
                finished.set()

        worker = Thread(target=read_snapshot)
        worker.start()
        try:
            self.assertTrue(application_lock.wait_for_attempts(1))
            self.assertTrue(finished.wait(1.5))
            self.assertEqual(application_lock.timeouts, [1.0])
            self.assertEqual(responses[0].status, 503)
            self.assertEqual(responses[0].payload["request_id"], REQUEST_ID)
            self.assertEqual(responses[0].payload["error"]["code"], "BACKEND_NOT_READY")
            self.assertTrue(responses[0].payload["error"]["retryable"])
            self.assertEqual(responses[0].payload["error"]["details"], {"reason": "SNAPSHOT_BUSY"})
        finally:
            application_lock.release()
            worker.join(2.0)

        self.assertEqual(get_snapshot(REQUEST_ID, context).status, 200)

    def test_only_four_requests_can_wait_and_a_different_context_is_independent(self) -> None:
        """
        함수 이름: test_only_four_requests_can_wait_and_a_different_context_is_independent()
        기능: 한 server의 다섯 번째 대기를 거절하고 다른 server의 조회는 유지한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        runtime = _create_ready_runtime()
        application_lock = _ContendedLock()
        runtime.application_lock = application_lock
        context = RouteContext(runtime, BackendEventStream())
        responses = []
        workers = [Thread(target=lambda: responses.append(get_snapshot(REQUEST_ID, context))) for _ in range(4)]
        rejected_responses = []
        rejection_finished = Event()

        def read_overloaded_snapshot() -> None:
            """
            함수 이름: read_overloaded_snapshot()
            기능: 다섯 번째 요청이 application 잠금 대기 없이 반환하는지 관찰한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/09/22
            """
            try:
                rejected_responses.append(get_snapshot(REQUEST_ID, context))
            finally:
                rejection_finished.set()

        overload_worker = Thread(target=read_overloaded_snapshot)
        for worker in workers:
            worker.start()
        try:
            self.assertTrue(application_lock.wait_for_attempts(4))
            overload_worker.start()
            self.assertTrue(rejection_finished.wait(0.5))
            rejected = rejected_responses[0]
            self.assertEqual(rejected.status, 503)
            self.assertEqual(application_lock.attempts, 4)
            self.assertTrue(rejected.payload["error"]["retryable"])
            other_context = RouteContext(_create_ready_runtime(), BackendEventStream())
            self.assertEqual(get_snapshot(REQUEST_ID, other_context).status, 200)
        finally:
            application_lock.release()
            for worker in workers:
                worker.join(2.0)
            if overload_worker.ident is not None:
                overload_worker.join(2.0)

        self.assertEqual([response.status for response in responses], [200] * 4)
        self.assertEqual(get_snapshot(REQUEST_ID, context).status, 200)

    def test_readiness_and_mapping_failures_release_the_lock_and_request_slots(self) -> None:
        """
        함수 이름: test_readiness_and_mapping_failures_release_the_lock_and_request_slots()
        기능: 준비 실패와 mapper 예외가 제한된 요청 자리를 영구 점유하지 않게 한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        runtime = _create_ready_runtime()
        runtime.application_lock = Lock()
        context = RouteContext(runtime, BackendEventStream())
        runtime.ready = False
        for _ in range(8):
            self.assertEqual(get_snapshot(REQUEST_ID, context).status, 503)
        runtime.ready = True
        with patch("binance_auto_trader.transport.routes.snapshot.build_snapshot_dto", side_effect=RuntimeError("mapping failed")):
            for _ in range(8):
                with self.assertRaisesRegex(RuntimeError, "mapping failed"):
                    get_snapshot(REQUEST_ID, context)

        self.assertTrue(runtime.application_lock.acquire(blocking=False))
        runtime.application_lock.release()
        self.assertEqual(get_snapshot(REQUEST_ID, context).status, 200)

    def test_snapshot_builder_and_cursor_stay_inside_the_application_lock(self) -> None:
        """
        함수 이름: test_snapshot_builder_and_cursor_stay_inside_the_application_lock()
        기능: 제한된 대기 이후에도 원자적 aggregate와 event cursor 조합을 유지한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        runtime = _create_ready_runtime()
        runtime.application_lock = Lock()
        event_stream = BackendEventStream()
        event_stream.publish("ACCOUNT_UPDATED", {"version": 1})
        context = RouteContext(runtime, event_stream)

        def build_snapshot(source: object, session_id: str, sequence: int) -> dict[str, object]:
            """
            함수 이름: build_snapshot()
            기능: mapper 호출 중 application 잠금과 같은 event cursor를 확인한다.
            인자: source -> snapshot runtime, session_id -> event stream 세션, sequence -> 마지막 순번
            반환값: 검증용 최소 snapshot payload
            작성 날짜: 2026/09/22
            """
            self.assertIs(source, runtime)
            self.assertTrue(runtime.application_lock.locked())
            self.assertEqual(session_id, event_stream.session_id)
            self.assertEqual(sequence, event_stream.last_sequence)
            return {"last_sequence": sequence}

        with patch("binance_auto_trader.transport.routes.snapshot.build_snapshot_dto", side_effect=build_snapshot):
            response = get_snapshot(REQUEST_ID, context)

        self.assertEqual(response.payload["data"]["last_sequence"], 1)
        self.assertFalse(runtime.application_lock.locked())
