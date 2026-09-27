"""종료 재시도·자원 정리·작업 소유권의 경계 실패를 네트워크 없이 검증한다."""

from threading import Event, Thread
from time import monotonic, sleep
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from binance_auto_trader.adapters.binance.request_deadline import (
    RequestDeadlineExceeded, remaining_request_timeout, request_deadline_scope,
)
from binance_auto_trader.bootstrap import ApplicationStatus, ShutdownBlockedError, request_application_shutdown
from binance_auto_trader.bootstrap import lifecycle
from binance_auto_trader.bootstrap.application import _ShutdownFlight
from binance_auto_trader.bootstrap.shutdown_preparation import ShutdownPreparation, _run
from binance_auto_trader.domain.trading.action_requests import patch as runtime_patch
from binance_auto_trader.transport.routes.system import get_shutdown_state
from tests.integration import test_shutdown_preparation as preparation_fixture


class ShutdownCompletionTests(unittest.TestCase):
    """
    클래스 이름: ShutdownCompletionTests
    기능: 준비 완료 이후에도 실제 정리·프로세스 종료의 안전 조건을 유지하는지 검증한다.
    작성 날짜: 2026/09/27
    """

    def setUp(self):
        """
        함수 이름: setUp()
        기능: 외부 socket을 금지하고 실제 runtime을 모의 거래소로 조립한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        self.enterContext(patch("socket.create_connection", side_effect=AssertionError("external network forbidden")))
        self.fixture = preparation_fixture.ShutdownPreparationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runtime = self.fixture.f.runtime
        self.controller = self.fixture.c

    def prepare_ready(self):
        """
        함수 이름: prepare_ready()
        기능: 종료 준비가 완료되고 해당 소유자가 반환했는지 함께 확인한다.
        인자: 없음
        반환값: 준비 완료 상태
        작성 날짜: 2026/09/27
        """
        self.fixture.prepare()
        result = self.fixture.finish()
        deadline = monotonic() + 2
        while self.runtime._shutdown_store.preparation.active and monotonic() < deadline:
            sleep(0.001)
        self.assertEqual(result["phase"], "ready", result)
        self.assertFalse(self.runtime._shutdown_store.preparation.active)
        return result

    def test_final_waiter_timeout_preserves_owner_and_previous_generation_result(self):
        """
        함수 이름: test_final_waiter_timeout_preserves_owner_and_previous_generation_result()
        기능: 대기자 시간초과는 소유권을 바꾸지 않고 새 재시도도 이전 결과를 덮어쓰지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        store = self.runtime._shutdown_store
        flight = _ShutdownFlight()
        store.flight, store.in_progress = flight, True
        with request_deadline_scope(0.01):
            with self.assertRaises(RequestDeadlineExceeded):
                lifecycle._await_shutdown_flight(flight)
        self.assertTrue(store.in_progress)
        failure = OSError("first shutdown failure")
        lifecycle._complete_shutdown_flight(self.runtime, error=failure)
        store.flight, store.in_progress = _ShutdownFlight(), True
        with self.assertRaises(OSError) as raised:
            lifecycle._await_shutdown_flight(flight)
        self.assertIs(raised.exception, failure)
        lifecycle._complete_shutdown_flight(self.runtime, error=OSError("retry failure"))

    def test_scheduler_cleanup_exception_releases_owner_for_retry(self):
        """
        함수 이름: test_scheduler_cleanup_exception_releases_owner_for_retry()
        기능: 구독 정리 이전 scheduler 실패도 소유권을 남기거나 다음 성공으로 오인하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        with patch.object(type(self.controller._scheduler), "clear", side_effect=OSError("scheduler busy")):
            with self.assertRaises(OSError):
                self.controller.close_session_resources()
        self.assertFalse(self.controller._cleanup_active)
        self.controller.close_session_resources()
        self.assertFalse(self.controller._cleanup_failures)

    def test_state_change_after_final_check_can_prepare_again_without_reopening_commands(self):
        """
        함수 이름: test_state_change_after_final_check_can_prepare_again_without_reopening_commands()
        기능: 최종 검증 사이 상태 변경으로 거부되어도 종료 조회·재준비·종료가 이어지는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        self.prepare_ready()
        original = lifecycle._read_shutdown_safety_receipt
        observations = []

        def change_after_first_check(runtime):
            """
            함수 이름: change_after_first_check()
            기능: 첫 안전 판정 직후 상태 버전을 변경해 최종 재검증 실패를 재현한다.
            인자: runtime -> 검사할 runtime
            반환값: 변경 전에 관측한 안전 판정
            작성 날짜: 2026/09/27
            """
            receipt = original(runtime)
            if not observations:
                self.controller._context.apply_runtime_patch(runtime_patch(signal_created=True))
            observations.append(receipt)
            return receipt

        with patch.object(lifecycle, "_read_shutdown_safety_receipt", side_effect=change_after_first_check):
            with self.assertRaises(ShutdownBlockedError):
                request_application_shutdown(self.runtime, command_id="changed-state",
                    expected_version=self.controller.context.version)
        self.assertIs(self.runtime.state.status, ApplicationStatus.SHUTTING_DOWN)
        response = get_shutdown_state(str(uuid4()), SimpleNamespace(runtime=self.runtime,
            event_stream=SimpleNamespace(session_id=str(uuid4()))))
        self.assertEqual(response.status, 200)
        self.assertFalse(self.controller.command_enabled)
        self.prepare_ready()
        receipt = request_application_shutdown(self.runtime, command_id="changed-state-retry",
            expected_version=self.controller.context.version)
        self.assertTrue(receipt.accepted)
        self.assertTrue(self.runtime._shutdown_store.cleanup_finished)
        self.assertFalse(self.fixture.f.rest_client.submitted_orders)

    def test_closed_after_cleanup_failure_is_not_accepted_until_resource_really_closes(self):
        """
        함수 이름: test_closed_after_cleanup_failure_is_not_accepted_until_resource_really_closes()
        기능: CLOSED 게시 뒤에도 실패 자원을 다시 정리해야 수락하며 성공 자원은 반복하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        self.prepare_ready()
        original = self.controller.close_session_resources
        with patch.object(self.controller, "close_session_resources", side_effect=OSError("synthetic cleanup")) as close:
            for attempt in range(2):
                with self.assertRaises(OSError):
                    request_application_shutdown(self.runtime, command_id=f"cleanup-{attempt}",
                        expected_version=self.controller.context.version)
                self.assertIs(self.runtime.state.status, ApplicationStatus.CLOSED)
                self.assertFalse(self.runtime._shutdown_store.cleanup_finished)
            self.assertEqual(close.call_count, 2)
        with patch.object(self.controller, "close_session_resources", wraps=original) as close:
            for attempt in range(2):
                receipt = request_application_shutdown(self.runtime, command_id=f"fixed-{attempt}",
                    expected_version=self.controller.context.version)
                self.assertTrue(receipt.accepted)
            self.assertEqual(close.call_count, 1)
        self.assertFalse(self.runtime._shutdown_store.cleanup_errors)

    def test_market_handle_survives_close_failure_and_retry_closes_it_once(self):
        """
        함수 이름: test_market_handle_survives_close_failure_and_retry_closes_it_once()
        기능: 구독 상태를 unavailable로 바꿔도 실패한 실제 handle을 잃지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        handle = Mock()
        handle.close.side_effect = [OSError("synthetic socket close"), None]
        market = self.runtime.market_data_controller
        market._live_subscription = handle
        self.fixture.prepare()
        self.assertEqual(self.fixture.finish()["reason_code"], "SHUTDOWN_RESOURCE_CLEANUP_FAILED")
        self.assertIn(handle, market._closing_subscriptions)
        self.prepare_ready()
        self.assertEqual(handle.close.call_count, 2)
        self.assertFalse(market._closing_subscriptions)
        request_application_shutdown(self.runtime, command_id="close-handle",
            expected_version=self.controller.context.version)
        self.assertEqual(handle.close.call_count, 2)

    def test_thread_and_timer_start_failures_release_the_preparation_owner(self):
        """
        함수 이름: test_thread_and_timer_start_failures_release_the_preparation_owner()
        기능: 실행하지 못한 작업이 active에 남지 않고 다음 명시적 종료가 성공하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        for target in ("Thread.start", "Timer.start"):
            with self.subTest(target=target):
                with patch(f"binance_auto_trader.bootstrap.shutdown_preparation.{target}",
                        side_effect=RuntimeError("synthetic start failure")):
                    self.fixture.prepare()
                    result = self.fixture.finish()
                deadline = monotonic() + 1
                while self.runtime._shutdown_store.preparation.active and monotonic() < deadline:
                    sleep(0.001)
                self.assertEqual(result["reason_code"], "SHUTDOWN_RESOURCE_CLEANUP_FAILED")
                self.assertTrue(result["retryable"])
                self.assertFalse(self.runtime._shutdown_store.preparation.active)
        self.prepare_ready()

    def test_application_lock_wait_obeys_the_same_preparation_deadline(self):
        """
        함수 이름: test_application_lock_wait_obeys_the_same_preparation_deadline()
        기능: 다른 thread가 공용 잠금을 보유해도 준비 소유자가 제한시간 안에 반환하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        for worker in (self.runtime._trading_event_runtime_worker,
                self.runtime._account_stream_recovery_worker, self.runtime._market_stream_recovery_worker):
            if worker is not None:
                worker.close()
        operation = ShutdownPreparation("lock-timeout", self.controller.context.version, False,
            deadline=monotonic() + 0.05)
        self.runtime._shutdown_store.preparation = operation
        self.controller._shutdown_preparing = True
        with self.runtime.application_lock:
            runner = Thread(target=_run, args=(self.runtime, operation), daemon=True)
            runner.start()
            runner.join(0.5)
            self.assertFalse(runner.is_alive())
        self.assertEqual(operation.reason_code, "SHUTDOWN_PREPARATION_TIMEOUT")
        self.assertFalse(operation.active)
        self.prepare_ready()

    def test_inflight_command_prevents_preparation_from_starting_another_effect(self):
        """
        함수 이름: test_inflight_command_prevents_preparation_from_starting_another_effect()
        기능: 잠금을 양보한 HTTP 주문 소유자가 남아 있으면 기한 안에서 기다리고 새 청산을 막는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        operation = ShutdownPreparation("command-timeout", self.controller.context.version, True,
            deadline=monotonic() + 0.05)
        self.runtime._shutdown_store.preparation = operation
        self.controller._shutdown_preparing = True
        self.controller._effect_owner = -1
        try:
            _run(self.runtime, operation)
            self.assertEqual(operation.reason_code, "SHUTDOWN_PREPARATION_TIMEOUT")
            self.assertFalse(operation.active)
            self.assertFalse(self.fixture.f.rest_client.submitted_orders)
        finally:
            with self.controller._effect_condition:
                self.controller._effect_owner = None
                self.controller._effect_condition.notify_all()
        self.prepare_ready()

    def test_late_network_result_keeps_single_owner_and_never_starts_liquidation(self):
        """
        함수 이름: test_late_network_result_keeps_single_owner_and_never_starts_liquidation()
        기능: 만료 뒤에도 I/O 중인 소유자는 유지하고 늦은 결과가 도착하면 안전하게 차단한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        entered, release = Event(), Event()
        observed_budgets = []
        operation = ShutdownPreparation("late-result", self.controller.context.version, False,
            deadline=monotonic() + 0.15)
        self.runtime._shutdown_store.preparation = operation
        self.controller._shutdown_preparing = True

        def blocked_orders(symbol):
            """
            함수 이름: blocked_orders()
            기능: adapter에 전달된 예산을 관측하고 기한 이후에만 조회 결과를 반환한다.
            인자: symbol -> 조회 종목
            반환값: 빈 미체결 목록
            작성 날짜: 2026/09/27
            """
            observed_budgets.append(remaining_request_timeout(120))
            entered.set()
            release.wait(2)
            return ()

        with patch.object(self.controller._api_gateway, "list_all_open_order_results", side_effect=blocked_orders):
            runner = Thread(target=_run, args=(self.runtime, operation), daemon=True)
            runner.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(self.fixture.finish()["reason_code"], "SHUTDOWN_PREPARATION_TIMEOUT")
                self.assertTrue(operation.active)
                self.assertEqual(self.fixture.prepare()["operation_id"], operation.operation_id)
            finally:
                release.set()
                runner.join(2)
        self.assertFalse(runner.is_alive())
        self.assertFalse(operation.active)
        self.assertLessEqual(observed_budgets[0], 0.15)
        self.assertIsNone(self.controller._shutdown_verified_version)
        self.assertFalse(self.fixture.f.rest_client.submitted_orders)
        self.prepare_ready()
