"""Production trading event runtime worker의 wake, fail-close와 종료 계약을 검증한다."""

from __future__ import annotations

from decimal import Decimal
from threading import Event, RLock
from time import monotonic
import unittest
from unittest.mock import patch

from binance_auto_trader.bootstrap.application import (
    _TradingEventRuntimeFailureStage,
    _TradingEventRuntimeWorker,
)
from binance_auto_trader.transport.contracts import decimal_to_wire


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
            failure_snapshot = worker.failure_snapshot
            self.assertIsNotNone(failure_snapshot)
            assert failure_snapshot is not None
            self.assertIs(
                failure_snapshot.stage,
                _TradingEventRuntimeFailureStage.RUNTIME_CYCLE,
            )
            self.assertEqual(failure_snapshot.exception_type, "RuntimeError")
            self.assertEqual(
                failure_snapshot.exception_origin,
                "EXTERNAL_OR_UNKNOWN",
            )
            self.assertNotIn(
                "controlled event runtime failure",
                repr(failure_snapshot),
            )  # Raw 예외 원문은 immutable diagnostic에도 복사하지 않는다.
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

    def test_publication_failure_records_exact_stage_without_raw_message(
        self,
    ) -> None:
        """
        함수 이름: test_publication_failure_records_exact_stage_without_raw_message()
        기능: 상태 publication 실패가 cycle 실패와 구분되고 raw 원문 없이 봉인되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/04
        """
        mutable_state = {"version": 0}
        fail_closed_completed = Event()

        async def change_state() -> tuple[str, ...]:
            """
            함수 이름: change_state()
            기능: publication이 필요한 단일 state change를 만든다.
            인자: 없음
            반환값: 처리 marker tuple
            작성 날짜: 2026/09/04
            """
            mutable_state["version"] += 1
            return ("changed",)

        def mark_failed() -> None:
            """
            함수 이름: mark_failed()
            기능: publication 예외 뒤 fail-close callback 진입을 기록한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/09/04
            """
            fail_closed_completed.set()

        def fail_publication() -> None:
            """
            함수 이름: fail_publication()
            기능: credential canary가 포함된 controlled publication 오류를 발생시킨다.
            인자: 없음
            반환값: 반환하지 않음
            작성 날짜: 2026/09/04
            """
            raise ValueError("credential-like-publication-canary")

        worker = _TradingEventRuntimeWorker(
            change_state,
            mark_failed,
            lambda: True,
            lambda: mutable_state["version"],
            RLock(),
            state_update_observer=fail_publication,
            poll_interval_seconds=0.01,
        )
        try:
            self.assertTrue(worker.start())
            self.assertTrue(worker.request_processing())
            self.assertTrue(fail_closed_completed.wait(1.0))

            # Fail-close publication의 재실패까지 끝난 worker snapshot을 bounded하게 기다린다.
            deadline = monotonic() + 1.0
            while not worker.failed and monotonic() < deadline:
                Event().wait(0.01)
            self.assertTrue(worker.failed)

            failure_snapshot = worker.failure_snapshot
            self.assertIsNotNone(failure_snapshot)
            assert failure_snapshot is not None
            self.assertIs(
                failure_snapshot.stage,
                _TradingEventRuntimeFailureStage.STATE_PUBLICATION,
            )
            self.assertEqual(failure_snapshot.exception_type, "ValueError")
            self.assertNotIn(
                "credential-like-publication-canary",
                repr(failure_snapshot),
            )  # Exception type과 stage만 남겨 secret-like 원문을 배제한다.
        finally:
            worker.close()

    def test_fail_closed_callback_failure_still_permanently_stops_worker(
        self,
    ) -> None:
        """
        함수 이름: test_fail_closed_callback_failure_still_permanently_stops_worker()
        기능: 2차 fail-close callback도 예외이면 worker가 재시작되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/05
        """
        fail_closed_attempted = Event()

        async def fail_cycle() -> tuple[object, ...]:
            """
            함수 이름: fail_cycle()
            기능: 최초 worker failure를 만들기 위한 controlled 예외를 발생시킨다.
            인자: 없음
            반환값: 반환하지 않음
            작성 날짜: 2026/09/05
            """
            raise RuntimeError("primary-runtime-failure-canary")

        def fail_while_closing() -> None:
            """
            함수 이름: fail_while_closing()
            기능: fail-close callback 자체의 2차 예외를 재현한다.
            인자: 없음
            반환값: 반환하지 않음
            작성 날짜: 2026/09/05
            """
            fail_closed_attempted.set()
            raise ValueError("secondary-fail-close-canary")

        worker = _TradingEventRuntimeWorker(
            fail_cycle,
            fail_while_closing,
            lambda: True,
            lambda: 0,
            RLock(),
            poll_interval_seconds=0.01,
        )
        with patch("threading.excepthook") as thread_exception_hook:
            try:
                self.assertTrue(worker.start())
                self.assertTrue(worker.request_processing())
                self.assertTrue(fail_closed_attempted.wait(1.0))

                # Callback 실패도 thread 밖으로 누출하지 않고 최초 failure 뒤 권한을 영구 회수한다.
                deadline = monotonic() + 1.0
                while not worker.failed and monotonic() < deadline:
                    Event().wait(0.01)
                self.assertTrue(worker.failed)
                self.assertFalse(worker.request_processing())
                self.assertFalse(worker.start())
                self.assertNotIn(
                    "secondary-fail-close-canary",
                    repr(worker.failure_snapshot),
                )  # 최초 immutable snapshot은 2차 callback 원문으로 오염하지 않는다.
            finally:
                worker.close()
        thread_exception_hook.assert_not_called()  # Raw traceback이 Python thread hook에도 전달되지 않는다.

    def test_cycle_failure_records_only_package_module_and_function_origin(
        self,
    ) -> None:
        """
        함수 이름: test_cycle_failure_records_only_package_module_and_function_origin()
        기능: package 내부 예외 위치가 파일·line·원문 없이 module과 함수로만 정규화되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/04
        """
        fail_closed_completed = Event()

        async def fail_inside_package() -> tuple[object, ...]:
            """
            함수 이름: fail_inside_package()
            기능: transport Decimal validator에서 controlled package 내부 오류를 발생시킨다.
            인자: 없음
            반환값: 반환하지 않음
            작성 날짜: 2026/09/04
            """
            decimal_to_wire(Decimal("NaN"))
            return ()  # Validator가 fail closed하지 않으면 test가 이 비정상 경로에 도달한다.

        worker = _TradingEventRuntimeWorker(
            fail_inside_package,
            fail_closed_completed.set,
            lambda: True,
            lambda: 0,
            RLock(),
            poll_interval_seconds=0.01,
        )
        try:
            self.assertTrue(worker.start())
            self.assertTrue(worker.request_processing())
            self.assertTrue(fail_closed_completed.wait(1.0))

            # Reconciliation callback보다 먼저 저장된 immutable snapshot만 읽어 origin을 검증한다.
            failure_snapshot = worker.failure_snapshot
            self.assertIsNotNone(failure_snapshot)
            assert failure_snapshot is not None
            self.assertIs(
                failure_snapshot.stage,
                _TradingEventRuntimeFailureStage.RUNTIME_CYCLE,
            )
            self.assertEqual(failure_snapshot.exception_type, "ValueError")
            self.assertEqual(
                failure_snapshot.exception_origin,
                "binance_auto_trader.transport.contracts:decimal_to_wire",
            )
            self.assertNotIn("NaN", repr(failure_snapshot))
            self.assertNotIn("/Users/", repr(failure_snapshot))
        finally:
            worker.close()

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
