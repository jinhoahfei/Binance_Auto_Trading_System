"""Production trading event runtime worker의 wake, fail-close와 종료 계약을 검증한다."""

from __future__ import annotations

from threading import Event, RLock
from time import monotonic
import unittest

from binance_auto_trader.bootstrap.application import (
    _TradingEventRuntimeWorker,
)


class TradingEventRuntimeWorkerTests(unittest.TestCase):
    """
    클래스 이름: TradingEventRuntimeWorkerTests
    기능: 단일 interruptible worker의 publication, failure와 close 동시성을 검증한다.
    작성 날짜: 2026/08/24
    """

    def test_start_does_not_publish_until_requested_cycle_changes_state(
        self,
    ) -> None:
        """
        함수 이름: test_start_does_not_publish_until_requested_cycle_changes_state()
        기능: READY start wake가 event를 오염시키지 않고 반복 요청은 한 state change로 합치는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        application_lock = RLock()
        mutable_state = {"pending": False, "version": 0}
        publication_versions: list[int] = []
        publication_completed = Event()

        async def run_cycle() -> tuple[str, ...]:
            """
            함수 이름: run_cycle()
            기능: pending work 한 건만 소비하고 결정적 state version을 증가시킨다.
            인자: 없음
            반환값: work를 처리했으면 marker tuple, 아니면 빈 tuple
            작성 날짜: 2026/08/24
            """
            if not mutable_state["pending"]:
                return ()

            mutable_state["pending"] = False
            mutable_state["version"] += 1
            return ("processed",)

        def read_state() -> int:
            """
            함수 이름: read_state()
            기능: worker 비교에 사용할 현재 version을 반환한다.
            인자: 없음
            반환값: 현재 정수 version
            작성 날짜: 2026/08/24
            """
            return mutable_state["version"]

        def publish_state() -> None:
            """
            함수 이름: publish_state()
            기능: 게시된 version을 기록하고 test waiter를 깨운다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/08/24
            """
            publication_versions.append(mutable_state["version"])
            publication_completed.set()

        worker = _TradingEventRuntimeWorker(
            run_cycle,
            lambda: None,
            lambda: True,
            read_state,
            application_lock,
            state_update_observer=publish_state,
            poll_interval_seconds=0.01,
        )
        try:
            self.assertTrue(worker.start())
            Event().wait(0.04)  # 여러 cadence가 지나도 빈 cycle publication은 없어야 한다.
            self.assertEqual(publication_versions, [])

            # 동일 pending work의 연속 wake는 Event 하나로 합쳐져 state를 한 번만 변경한다.
            mutable_state["pending"] = True
            self.assertTrue(worker.request_processing())
            self.assertTrue(worker.request_processing())
            self.assertTrue(publication_completed.wait(1.0))
            self.assertEqual(publication_versions, [1])
        finally:
            worker.close()

    def test_cycle_failure_marks_fail_closed_once_and_stops_worker(
        self,
    ) -> None:
        """
        함수 이름: test_cycle_failure_marks_fail_closed_once_and_stops_worker()
        기능: 최초 bounded cycle 실패가 reconciliation publication 한 번 뒤 worker를 멈추는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        application_lock = RLock()
        mutable_state = {"status": "stopping", "version": 7}
        fail_closed_calls: list[str] = []
        publication_completed = Event()

        async def fail_cycle() -> tuple[object, ...]:
            """
            함수 이름: fail_cycle()
            기능: worker fail-close 경계를 검증할 controlled cycle 오류를 발생시킨다.
            인자: 없음
            반환값: 반환하지 않음
            작성 날짜: 2026/08/24
            """
            raise RuntimeError("controlled event runtime failure")

        def mark_failed() -> None:
            """
            함수 이름: mark_failed()
            기능: typed reconciliation 상태와 version을 한 번 기록한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/08/24
            """
            fail_closed_calls.append("EVENT_RUNTIME_FAILED")
            mutable_state["status"] = "reconciliation_required"
            mutable_state["version"] += 1

        def publish_state() -> None:
            """
            함수 이름: publish_state()
            기능: fail-closed publication 완료를 test thread에 알린다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/08/24
            """
            publication_completed.set()

        worker = _TradingEventRuntimeWorker(
            fail_cycle,
            mark_failed,
            lambda: True,
            lambda: tuple(mutable_state.values()),
            application_lock,
            state_update_observer=publish_state,
            poll_interval_seconds=0.01,
        )
        try:
            self.assertTrue(worker.start())
            self.assertTrue(worker.request_processing())
            self.assertTrue(publication_completed.wait(1.0))
            self.assertTrue(worker.failed)
            self.assertEqual(fail_closed_calls, ["EVENT_RUNTIME_FAILED"])
            self.assertEqual(
                mutable_state["status"],
                "reconciliation_required",
            )
            self.assertFalse(worker.request_processing())
            self.assertFalse(worker.start())
        finally:
            worker.close()
            worker.close()  # 실패 뒤 중복 close도 join이나 callback을 반복하지 않는다.

    def test_close_interrupts_long_poll_without_waiting_for_timeout(self) -> None:
        """
        함수 이름: test_close_interrupts_long_poll_without_waiting_for_timeout()
        기능: close가 긴 cadence Event.wait를 즉시 깨우고 멱등 회수하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        async def empty_cycle() -> tuple[object, ...]:
            """
            함수 이름: empty_cycle()
            기능: close test에서 실행돼도 외부 효과가 없는 빈 cycle을 반환한다.
            인자: 없음
            반환값: 빈 tuple
            작성 날짜: 2026/08/24
            """
            return ()

        worker = _TradingEventRuntimeWorker(
            empty_cycle,
            lambda: None,
            lambda: False,
            lambda: 0,
            RLock(),
            poll_interval_seconds=30.0,
        )
        self.assertTrue(worker.start())

        close_started_at = monotonic()
        worker.close()
        close_duration = monotonic() - close_started_at

        self.assertLess(close_duration, 0.5)
        self.assertFalse(worker.request_processing())
        worker.close()  # 두 번째 close는 이미 회수한 thread를 다시 기다리지 않는다.


if __name__ == "__main__":
    unittest.main()
