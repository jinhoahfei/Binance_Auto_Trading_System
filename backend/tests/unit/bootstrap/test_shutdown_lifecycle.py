"""Phase 12 application shutdown의 exposure Guard와 fsync lifecycle을 검증한다."""

from collections.abc import Callable
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import PropertyMock, patch

from binance_auto_trader.application import (
    TradeHistoryController,
    TradingSessionError,
    TradingSessionFailureCode,
)
from binance_auto_trader.bootstrap import (
    ApplicationStatus,
    ShutdownBlockedError,
    close_application,
    create_application_runtime,
    request_application_shutdown,
)
from binance_auto_trader.domain.trading import (
    AccountSnapshot,
    AssetBalance,
    Position,
)
from tests.unit.bootstrap.test_application import (
    _StubRestClient,
    _StubWebSocketClient,
)


def _create_ready_runtime(
    history_path: Path,
    *,
    trading_session_update_observer: Callable[[object, object], object]
    | None = None,
) -> object:
    """
    함수 이름: _create_ready_runtime()
    기능: network startup 없이 shutdown owner를 검증할 READY application runtime을 만든다.
    인자: history_path -> 종료 fsync가 생성할 JSONL 경로
        trading_session_update_observer -> production event worker를 조립할 optional observer
    반환값: startup reconciliation 완료 상태의 ApplicationRuntime
    작성 날짜: 2026/08/24
    """
    runtime = create_application_runtime(
        _StubRestClient(),
        _StubWebSocketClient(),
        history_path=history_path,
        trading_session_update_observer=trading_session_update_observer,
    )
    with runtime.application_lock:
        # 실제 start lifecycle의 shutdown 관련 사후조건만 명시해 network fixture를 만들지 않는다.
        runtime.trading_controller._startup_reconciliation_complete = True
        runtime._publish_state(
            status=ApplicationStatus.READY,
            failure=None,
            startup_trace=(),
        )

    return runtime


class ShutdownLifecycleTests(unittest.TestCase):
    """
    클래스 이름: ShutdownLifecycleTests
    기능: stale, exposure blocked와 command-block→fsync→CLOSED 종료 순서를 검증한다.
    작성 날짜: 2026/08/24
    """

    def test_zero_exposure_shutdown_fsyncs_history_and_closes_runtime(self) -> None:
        """
        함수 이름: test_zero_exposure_shutdown_fsyncs_history_and_closes_runtime()
        기능: 안전한 NOT_STARTED runtime이 history 파일을 fsync하고 CLOSED로 끝나는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "history.jsonl"
            runtime = _create_ready_runtime(history_path)
            receipt = request_application_shutdown(
                runtime,
                command_id="shutdown-safe",
                expected_version=0,
            )

            # Empty history도 실제 파일과 CLOSED publication을 남겨 종료 durability를 증명한다.
            self.assertTrue(receipt.accepted)
            self.assertEqual(receipt.status.value, "accepted")
            self.assertEqual(receipt.version, 0)
            self.assertTrue(history_path.is_file())
            self.assertIs(runtime.state.status, ApplicationStatus.CLOSED)
            self.assertFalse(runtime.ready)

    def test_shutdown_interrupts_active_event_worker_before_final_close(
        self,
    ) -> None:
        """
        함수 이름: test_shutdown_interrupts_active_event_worker_before_final_close()
        기능: READY event worker가 있는 zero-exposure runtime도 교착 없이 CLOSED로 회수되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            published_states: list[tuple[object, object]] = []

            def observe_trading_state(
                controller: object,
                execution_mode: object,
            ) -> None:
                """
                함수 이름: observe_trading_state()
                기능: shutdown test에서 unexpected background publication을 기록한다.
                인자: controller -> publication 대상 TradingController
                    execution_mode -> runtime ExecutionMode
                반환값: 없음
                작성 날짜: 2026/08/24
                """
                published_states.append((controller, execution_mode))

            runtime = _create_ready_runtime(
                Path(temporary_directory) / "history.jsonl",
                trading_session_update_observer=observe_trading_state,
            )
            event_worker = runtime._trading_event_runtime_worker
            if event_worker is None:
                raise AssertionError("observer must compose the production worker")
            self.assertTrue(event_worker.start())

            # Worker가 interruptible cadence에서 대기하는 동안 safe shutdown owner를 실행한다.
            receipt = request_application_shutdown(
                runtime,
                command_id="shutdown-active-event-worker",
                expected_version=0,
            )

            self.assertTrue(receipt.accepted)
            self.assertIs(runtime.state.status, ApplicationStatus.CLOSED)
            self.assertFalse(event_worker.request_processing())
            self.assertEqual(published_states, [])

    def test_open_position_blocks_without_lowering_ready_state(self) -> None:
        """
        함수 이름: test_open_position_blocks_without_lowering_ready_state()
        기능: 실제 Position 수량이 있으면 종료 side effect 전에 typed 409 receipt 근거를 만드는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime = _create_ready_runtime(
                Path(temporary_directory) / "history.jsonl"
            )
            try:
                with patch.object(
                    Position,
                    "quantity",
                    new_callable=PropertyMock,
                    return_value=Decimal("0.1"),
                ):
                    with self.assertRaises(ShutdownBlockedError) as error_context:
                        request_application_shutdown(
                            runtime,
                            command_id="shutdown-open-position",
                            expected_version=0,
                        )

                blocked_details = error_context.exception.receipt.to_blocked_details()
                self.assertEqual(
                    blocked_details,
                    {
                        "accepted": False,
                        "status": "blocked",
                        "version": 0,
                        "position_open": True,
                        "pending_order": False,
                        "reconciliation_required": False,
                    },
                )
                self.assertIs(runtime.state.status, ApplicationStatus.READY)
            finally:
                close_application(runtime)  # 차단 test가 남긴 runtime resource를 명시 회수한다.

    def test_reconciliation_and_stale_version_fail_before_shutdown(self) -> None:
        """
        함수 이름: test_reconciliation_and_stale_version_fail_before_shutdown()
        기능: stale version을 우선 거부하고 최신 요청도 unresolved reconciliation에서는 막는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime = _create_ready_runtime(
                Path(temporary_directory) / "history.jsonl"
            )
            try:
                with self.assertRaises(TradingSessionError) as stale_context:
                    request_application_shutdown(
                        runtime,
                        command_id="shutdown-stale",
                        expected_version=1,
                    )
                self.assertIs(
                    stale_context.exception.code,
                    TradingSessionFailureCode.STALE_CONTEXT_VERSION,
                )

                runtime.trading_controller._stream_reconciliation_required = True
                with self.assertRaises(ShutdownBlockedError) as blocked_context:
                    request_application_shutdown(
                        runtime,
                        command_id="shutdown-reconciliation",
                        expected_version=0,
                    )

                self.assertTrue(
                    blocked_context.exception.receipt.reconciliation_required
                )
                self.assertIs(runtime.state.status, ApplicationStatus.READY)
            finally:
                close_application(runtime)  # 각 fail-closed 경로 이후에도 test 자원을 회수한다.

    def test_flush_failure_keeps_commands_blocked_and_runtime_alive(self) -> None:
        """
        함수 이름: test_flush_failure_keeps_commands_blocked_and_runtime_alive()
        기능: history fsync 실패가 process close로 진행하지 않고 SHUTTING_DOWN gate를 유지하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime = _create_ready_runtime(
                Path(temporary_directory) / "history.jsonl"
            )
            try:
                with patch.object(
                    TradeHistoryController,
                    "flush_durable_state",
                    autospec=True,
                    side_effect=OSError("synthetic fsync failure"),
                ):
                    with self.assertRaisesRegex(OSError, "synthetic fsync"):
                        request_application_shutdown(
                            runtime,
                            command_id="shutdown-fsync-failure",
                            expected_version=0,
                        )

                # Durability가 증명되지 않으면 READY로 되돌리거나 CLOSED로 위장하지 않는다.
                self.assertIs(
                    runtime.state.status,
                    ApplicationStatus.SHUTTING_DOWN,
                )
                self.assertFalse(runtime.ready)
            finally:
                close_application(runtime)  # 고의 fsync 실패 뒤 test process 자원만 회수한다.

    def test_concurrent_different_keys_share_one_shutdown_tail(self) -> None:
        """
        함수 이름: test_concurrent_different_keys_share_one_shutdown_tail()
        기능: 서로 다른 command ID의 동시 종료가 fsync와 close owner를 한 번만 실행하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime = _create_ready_runtime(
                Path(temporary_directory) / "history.jsonl"
            )
            flush_entered = Event()
            release_flush = Event()
            shutdown_results = []
            shutdown_errors = []
            flush_call_count = 0
            original_flush = TradeHistoryController.flush_durable_state

            def blocking_flush(controller: TradeHistoryController) -> None:
                """
                함수 이름: blocking_flush()
                기능: 두 번째 command가 single-flight owner를 관찰할 때까지 첫 fsync를 대기시킨다.
                인자: controller -> owner thread가 flush하는 TradeHistoryController
                반환값: release 뒤 production flush 결과
                작성 날짜: 2026/08/24
                """
                # 호출 수를 기록하고 waiter가 join할 때까지 production flush를 barrier에서 멈춘다.
                nonlocal flush_call_count
                flush_call_count += 1
                flush_entered.set()  # Waiter가 owner의 in-progress flight를 관찰할 barrier를 연다.
                release_flush.wait(timeout=2.0)
                original_flush(controller)

            def shutdown_worker(command_id: str) -> None:
                """
                함수 이름: shutdown_worker()
                기능: 한 command ID의 shutdown 결과 또는 예외를 thread-safe list에 기록한다.
                인자: command_id -> 서로 다른 shutdown command identity
                반환값: 없음
                작성 날짜: 2026/08/24
                """
                # 각 thread의 결과와 예외를 분리해 waiter hang과 owner 실패를 동시에 진단한다.
                try:
                    shutdown_results.append(
                        request_application_shutdown(
                            runtime,
                            command_id=command_id,
                            expected_version=0,
                        )
                    )
                except BaseException as error:
                    shutdown_errors.append(error)

            # 첫 flush를 barrier에서 멈추고 다른 key의 요청이 동일 flight에 join하도록 만든다.
            with patch.object(
                TradeHistoryController,
                "flush_durable_state",
                autospec=True,
                side_effect=blocking_flush,
            ):
                owner_thread = Thread(
                    target=shutdown_worker,
                    args=("shutdown-owner",),
                    daemon=True,
                )
                waiter_thread = Thread(
                    target=shutdown_worker,
                    args=("shutdown-waiter",),
                    daemon=True,
                )
                owner_thread.start()
                self.assertTrue(flush_entered.wait(timeout=1.0))
                waiter_thread.start()  # Owner tail이 열린 동안 두 번째 command를 시작한다.
                release_flush.set()  # 두 command가 합쳐진 뒤 production flush를 완료시킨다.
                owner_thread.join(timeout=2.0)
                waiter_thread.join(timeout=2.0)

            # 두 thread가 종료되고 fsync/close owner가 정확히 한 번만 실행됐는지 확인한다.
            self.assertFalse(owner_thread.is_alive())
            self.assertFalse(waiter_thread.is_alive())
            self.assertEqual(shutdown_errors, [])
            self.assertEqual(len(shutdown_results), 2)
            self.assertTrue(all(result.accepted for result in shutdown_results))
            self.assertEqual(flush_call_count, 1)
            self.assertIs(runtime.state.status, ApplicationStatus.CLOSED)

    def test_callback_waiting_on_shutdown_gate_cannot_mutate_account(self) -> None:
        """
        함수 이름: test_callback_waiting_on_shutdown_gate_cannot_mutate_account()
        기능: SHUTTING_DOWN publication 전에 대기한 account callback도 gate 뒤에는 적용되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime = _create_ready_runtime(
                Path(temporary_directory) / "history.jsonl"
            )
            callback_started = Event()
            callback_results = []
            account_snapshot = AccountSnapshot(
                balances=(
                    AssetBalance(
                        asset="ETH",
                        free=Decimal("1"),
                        locked=Decimal("0"),
                    ),
                ),
                updated_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
                is_full_snapshot=False,
            )
            callback = runtime.web_socket_gateway._account_snapshot_callback

            def apply_waiting_callback() -> None:
                """
                함수 이름: apply_waiting_callback()
                기능: 시작 barrier 뒤 production account callback 결과를 기록한다.
                인자: 없음
                반환값: 없음
                작성 날짜: 2026/08/24
                """
                # Callback 시작을 publish한 뒤 실제 gate 결과를 같은 worker에서 기록한다.
                callback_started.set()  # Main thread가 lock을 유지한 채 callback 대기를 관찰하게 한다.
                callback_results.append(callback(account_snapshot))

            # Callback을 SHUTTING_DOWN publication 뒤 같은 lock에서 시작해 mutation gate 순서를 고정한다.
            with runtime.application_lock:
                runtime._publish_state(
                    status=ApplicationStatus.SHUTTING_DOWN,
                    failure=None,
                    startup_trace=(),
                )
                callback_thread = Thread(
                    target=apply_waiting_callback,
                    daemon=True,
                )
                callback_thread.start()
                self.assertTrue(callback_started.wait(timeout=1.0))

            # Lock 해제 뒤 callback이 false로 끝나고 Account version을 바꾸지 않았는지 확인한다.
            callback_thread.join(timeout=1.0)
            self.assertFalse(callback_thread.is_alive())
            self.assertEqual(callback_results, [False])
            self.assertEqual(runtime.account.version, 0)
            close_application(runtime)  # 차단된 callback 뒤 runtime resource를 회수한다.


if __name__ == "__main__":
    unittest.main()
