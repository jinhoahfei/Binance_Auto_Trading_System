"""Testnet account stream 자동 복구 worker의 retry와 lifecycle을 검증한다."""

from __future__ import annotations

from threading import Event
import unittest

from binance_auto_trader.application import (
    AccountStreamRecoveryBlockedError,
    StartupOrderReconciliationError,
)
from binance_auto_trader.bootstrap.application import (
    _AccountStreamRecoveryWorker,
)


class AccountStreamRecoveryWorkerTests(unittest.TestCase):
    """
    클래스 이름: AccountStreamRecoveryWorkerTests
    기능: 단일 worker의 retry, 중복 병합, readiness와 close Guard를 검증한다.
    작성 날짜: 2026/08/22
    """

    def test_transient_unknown_failure_retries_and_succeeds(
        self,
    ) -> None:
        """
        함수 이름: test_transient_unknown_failure_retries_and_succeeds()
        기능: UNKNOWN 계열 복구 실패가 1초 backoff slot을 거쳐 같은 worker에서 성공하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        attempts: list[int] = []
        retry_delays: list[float] = []
        recovery_completed = Event()

        def recover_account_stream() -> None:
            """
            함수 이름: recover_account_stream()
            기능: 첫 호출만 실패시키고 두 번째 호출의 성공을 test thread에 알린다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/08/22
            """
            attempts.append(len(attempts) + 1)
            if len(attempts) == 1:
                raise StartupOrderReconciliationError(
                    "injected transient query UNKNOWN"
                )

            recovery_completed.set()  # 두 번째 호출이 성공 경로에 도달했음을 알린다.

        def record_retry_delay(delay_seconds: float) -> bool:
            """
            함수 이름: record_retry_delay()
            기능: 실제 시간을 기다리지 않고 선택된 backoff 간격을 기록한다.
            인자: delay_seconds -> worker가 선택한 재시도 간격
            반환값: 종료 요청이 없으므로 False
            작성 날짜: 2026/08/22
            """
            retry_delays.append(delay_seconds)
            return False

        # 결정론 waiter를 주입해 production backoff 선택과 재시도 횟수만 검증한다.
        worker = _AccountStreamRecoveryWorker(
            recover_account_stream,
            lambda: True,
            retry_waiter=record_retry_delay,
        )
        try:
            self.assertTrue(worker.request_recovery())
            self.assertTrue(recovery_completed.wait(timeout=1.0))
        finally:
            worker.close()  # 실패 assertion에서도 daemon worker를 남기지 않는다.

        self.assertEqual(attempts, [1, 2])
        self.assertEqual(retry_delays, [1.0])

    def test_deterministic_blocker_stops_without_backoff_or_latched_rerun(
        self,
    ) -> None:
        """
        함수 이름: test_deterministic_blocker_stops_without_backoff_or_latched_rerun()
        기능: 결정적 recovery blocker가 pending 요청을 폐기하고 한 번의 시도 뒤 영구 중단되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        attempts: list[str] = []
        retry_delays: list[float] = []
        blocker_raised = Event()
        worker: _AccountStreamRecoveryWorker

        def recover_account_stream() -> None:
            """
            함수 이름: recover_account_stream()
            기능: 실행 중 후속 요청을 예약한 뒤 결정적 provenance blocker를 발생시킨다.
            인자: 없음
            반환값: 정상 반환 없이 AccountStreamRecoveryBlockedError 발생
            작성 날짜: 2026/08/23
            """
            attempts.append("recovery")
            if not worker.request_recovery():
                raise AssertionError("pending recovery request was not accepted")
            blocker_raised.set()
            raise AccountStreamRecoveryBlockedError(
                "injected deterministic provenance conflict"
            )

        def reject_retry(delay_seconds: float) -> bool:
            """
            함수 이름: reject_retry()
            기능: 결정적 blocker가 잘못 transient 분류되면 선택된 backoff를 기록하고 중단한다.
            인자: delay_seconds -> 잘못 선택된 재시도 간격
            반환값: 잘못된 반복을 즉시 끝내기 위해 True
            작성 날짜: 2026/08/23
            """
            retry_delays.append(delay_seconds)
            return True

        worker = _AccountStreamRecoveryWorker(
            recover_account_stream,
            lambda: True,
            retry_waiter=reject_retry,
        )
        try:
            self.assertTrue(worker.request_recovery())
            self.assertTrue(blocker_raised.wait(timeout=1.0))
            with worker._state_lock:
                active_thread = worker._active_thread
            if active_thread is not None:
                active_thread.join(timeout=1.0)  # Blocker 분류와 worker finalize까지 기다린다.
            self.assertFalse(worker.request_recovery())
        finally:
            worker.close()

        self.assertEqual(attempts, ["recovery"])
        self.assertEqual(retry_delays, [])

    def test_active_requests_are_coalesced_into_one_pending_rerun(
        self,
    ) -> None:
        """
        함수 이름: test_active_requests_are_coalesced_into_one_pending_rerun()
        기능: 진행 중 여러 account stream 요청이 후속 full reconciliation 하나로 합쳐지는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        recovery_started = Event()
        rerun_completed = Event()
        release_recovery = Event()
        attempts: list[str] = []

        def recover_account_stream() -> None:
            """
            함수 이름: recover_account_stream()
            기능: 중복 요청을 관찰할 때까지 첫 복구 호출을 test barrier에서 유지한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/08/22
            """
            attempts.append("recovery")
            recovery_started.set()
            release_recovery.wait(timeout=1.0)  # Test가 중복 요청을 보낸 뒤 성공시킨다.
            if len(attempts) == 2:
                rerun_completed.set()  # 병합된 후속 실행의 완료를 test thread에 알린다.

        worker = _AccountStreamRecoveryWorker(
            recover_account_stream,
            lambda: True,
        )
        try:
            self.assertTrue(worker.request_recovery())
            self.assertTrue(recovery_started.wait(timeout=1.0))
            self.assertTrue(worker.request_recovery())
            self.assertFalse(worker.request_recovery())
            release_recovery.set()
            self.assertTrue(rerun_completed.wait(timeout=1.0))
        finally:
            release_recovery.set()
            worker.close()

        self.assertEqual(attempts, ["recovery", "recovery"])

    def test_request_at_end_of_success_is_consumed_as_second_recovery(
        self,
    ) -> None:
        """
        함수 이름: test_request_at_end_of_success_is_consumed_as_second_recovery()
        기능: 첫 Operation 마지막의 재단절 요청이 finalize race에서 사라지지 않고 두 번째 복구를 실행하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        attempts: list[int] = []
        second_recovery_completed = Event()
        worker: _AccountStreamRecoveryWorker

        def recover_account_stream() -> None:
            """
            함수 이름: recover_account_stream()
            기능: 첫 성공 반환 직전에 같은 worker에 새 요청을 넣고 두 번째 실행을 알린다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/08/22
            """
            attempts.append(len(attempts) + 1)
            if len(attempts) == 1:
                accepted = worker.request_recovery()
                if not accepted:
                    raise AssertionError("end-of-success request was dropped")
                return

            second_recovery_completed.set()  # Pending latch가 실제 후속 실행으로 소비됐다.

        worker = _AccountStreamRecoveryWorker(
            recover_account_stream,
            lambda: True,
        )
        try:
            self.assertTrue(worker.request_recovery())
            self.assertTrue(second_recovery_completed.wait(timeout=1.0))
        finally:
            worker.close()

        self.assertEqual(attempts, [1, 2])

    def test_close_permanently_rejects_later_recovery_request(self) -> None:
        """
        함수 이름: test_close_permanently_rejects_later_recovery_request()
        기능: worker 종료 뒤 들어온 단절 요청이 새 thread나 외부 Operation을 만들지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        attempts: list[str] = []
        worker = _AccountStreamRecoveryWorker(
            lambda: attempts.append("recovery"),
            lambda: True,
        )

        # Runtime close와 같은 순서로 먼저 worker를 닫은 뒤 뒤늦은 callback 요청을 보낸다.
        worker.close()
        self.assertFalse(worker.request_recovery())
        self.assertEqual(attempts, [])

    def test_readiness_guard_blocks_external_recovery_operation(self) -> None:
        """
        함수 이름: test_readiness_guard_blocks_external_recovery_operation()
        기능: startup reconciliation이나 READY가 없는 runtime을 모사한 Guard가 I/O를 막는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        attempts: list[str] = []
        worker = _AccountStreamRecoveryWorker(
            lambda: attempts.append("recovery"),
            lambda: False,
        )
        try:
            self.assertTrue(worker.request_recovery())
        finally:
            worker.close()  # Guard 반환과 thread 종료가 끝날 때까지 join한다.

        self.assertEqual(attempts, [])


if __name__ == "__main__":
    unittest.main()
