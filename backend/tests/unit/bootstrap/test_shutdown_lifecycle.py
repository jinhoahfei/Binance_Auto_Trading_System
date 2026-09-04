"""Phase 12 application shutdown의 exposure Guard와 fsync lifecycle을 검증한다."""

from collections.abc import Callable
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import Mock, PropertyMock, patch

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
from binance_auto_trader.bootstrap.application import (
    _AccountStreamRecoveryWorker,
)
from binance_auto_trader.domain.trading import (
    AccountSnapshot,
    AssetBalance,
    ManualKillBehavior,
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

    def test_close_failure_attempts_all_resources_and_publishes_closed(
        self,
    ) -> None:
        """
        함수 이름: test_close_failure_attempts_all_resources_and_publishes_closed()
        기능: 최초 close 실패를 보존하면서 후속 자원·CLOSED 게시를 모두 시도하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/04
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            def observe_trading_state(
                controller: object,
                execution_mode: object,
            ) -> None:
                """
                함수 이름: observe_trading_state()
                기능: 종료 오류 test에 실제 event worker를 조립할 observer shape을 제공한다.
                인자: controller -> worker가 게시할 TradingController
                    execution_mode -> runtime의 ExecutionMode
                반환값: 없음
                작성 날짜: 2026/09/04
                """
                return None  # 이 test는 publication payload가 아니라 자원 종료 순서만 관찰한다.

            cleanup_order: list[str] = []

            def create_cleanup_operation(
                resource_name: str,
                failure: BaseException | None = None,
            ) -> Callable[[], None]:
                """
                함수 이름: create_cleanup_operation()
                기능: 자원 종료 시도 순서를 기록하고 선택 예외를 발생시킨다.
                인자: resource_name -> 기록할 안정적 자원 이름
                    failure -> close 기록 뒤 발생시킬 선택 예외
                반환값: 인자 없이 호출할 deterministic close Operation
                작성 날짜: 2026/09/04
                """

                def run_cleanup_operation() -> None:
                    """
                    함수 이름: run_cleanup_operation()
                    기능: 해당 자원 시도를 기록하고 fixture의 예외를 그대로 발생시킨다.
                    인자: 없음
                    반환값: 성공하면 없음
                    작성 날짜: 2026/09/04
                    """
                    cleanup_order.append(resource_name)
                    if failure is not None:
                        raise failure  # 실제 예외 identity를 재생성하지 않고 lifecycle에 전달한다.

                return run_cleanup_operation

            runtime = _create_ready_runtime(
                Path(temporary_directory) / "history.jsonl",
                trading_session_update_observer=observe_trading_state,
            )
            event_worker = runtime._trading_event_runtime_worker
            if event_worker is None:
                raise AssertionError("observer must compose the event worker")

            # Network effect 없는 실제 worker 타입을 주입해 FAKE runtime에서도 두 recovery slot을 검증한다.
            account_recovery_worker = _AccountStreamRecoveryWorker(
                Mock(),
                Mock(return_value=False),
                worker_name="test-account-cleanup",
            )
            market_recovery_worker = _AccountStreamRecoveryWorker(
                Mock(),
                Mock(return_value=False),
                worker_name="test-market-cleanup",
            )
            object.__setattr__(
                runtime,
                "_account_stream_recovery_worker",
                account_recovery_worker,
            )  # Frozen runtime의 소유 slot만 deterministic worker로 교체한다.
            object.__setattr__(
                runtime,
                "_market_stream_recovery_worker",
                market_recovery_worker,
            )  # Production close 순서를 유지하며 market worker를 별도로 관찰한다.

            # 최초 worker 오류와 세 후속 오류를 다른 타입으로 고정해 집계 순서를 본다.
            first_error = RuntimeError("synthetic event worker close failure")
            market_error = OSError("synthetic market close failure")
            session_error = ValueError("synthetic session close failure")
            account_error = LookupError("synthetic account close failure")
            account_subscription = Mock()
            account_subscription.close.side_effect = create_cleanup_operation(
                "account",
                account_error,
            )
            runtime.trading_controller._account_subscription = (
                account_subscription
            )  # Production property이 읽는 소유 handle에 deterministic fixture를 주입한다.

            with (
                patch.object(
                    event_worker,
                    "close",
                    side_effect=create_cleanup_operation(
                        "event-worker",
                        first_error,
                    ),
                ),
                patch.object(
                    account_recovery_worker,
                    "close",
                    side_effect=create_cleanup_operation("account-recovery"),
                ),
                patch.object(
                    market_recovery_worker,
                    "close",
                    side_effect=create_cleanup_operation("market-recovery"),
                ),
                patch.object(
                    runtime.market_data_controller,
                    "close_market_stream",
                    side_effect=create_cleanup_operation(
                        "market",
                        market_error,
                    ),
                ),
                patch.object(
                    runtime.trading_controller,
                    "close_session_resources",
                    side_effect=create_cleanup_operation(
                        "session",
                        session_error,
                    ),
                ),
            ):
                with self.assertRaises(RuntimeError) as error_context:
                    close_application(runtime)

            # 오류 경계 뒤의 모든 close와 terminal publication이 실행된 뒤 최초 identity가 돌아와야 한다.
            self.assertIs(error_context.exception, first_error)
            self.assertEqual(
                cleanup_order,
                [
                    "event-worker",
                    "account-recovery",
                    "market-recovery",
                    "market",
                    "session",
                    "account",
                ],
            )
            self.assertIs(runtime.state.status, ApplicationStatus.CLOSED)
            self.assertFalse(runtime.ready)
            self.assertEqual(
                first_error.__notes__,
                [
                    "Additional shutdown cleanup failure: market stream (OSError).",
                    "Additional shutdown cleanup failure: "
                    "trading session resources (ValueError).",
                    "Additional shutdown cleanup failure: "
                    "account subscription (LookupError).",
                ],
            )

    def test_shutdown_joins_market_recovery_before_reacquiring_app_lock(
        self,
    ) -> None:
        """
        함수 이름: test_shutdown_joins_market_recovery_before_reacquiring_app_lock()
        기능: publication app lock을 기다리는 market worker와 shutdown owner가 교착하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime = _create_ready_runtime(
                Path(temporary_directory) / "history.jsonl"
            )
            recovery_started = Event()
            release_publication = Event()
            publication_completed = Event()
            close_entered = Event()
            shutdown_results: list[object] = []
            shutdown_errors: list[BaseException] = []

            def recover_market_stream() -> None:
                """
                함수 이름: recover_market_stream()
                기능: shutdown close가 허용할 때까지 기다린 뒤 production publication lock을 획득한다.
                인자: 없음
                반환값: 없음
                작성 날짜: 2026/08/25
                """
                # 실제 recovery와 같이 외부 작업 뒤 application publication lock으로 진입한다.
                recovery_started.set()
                release_publication.wait(timeout=2.0)
                with runtime.application_lock:
                    publication_completed.set()

            market_recovery_worker = _AccountStreamRecoveryWorker(
                recover_market_stream,
                lambda: True,
                worker_name="test-market-recovery",
            )
            object.__setattr__(
                runtime,
                "_market_stream_recovery_worker",
                market_recovery_worker,
            )  # Frozen runtime의 production worker slot만 deterministic fixture로 교체한다.
            original_close = _AccountStreamRecoveryWorker.close

            def close_and_release(
                worker: _AccountStreamRecoveryWorker,
            ) -> None:
                """
                함수 이름: close_and_release()
                기능: shutdown이 market worker close에 도달한 순간 publication 대기를 해제한다.
                인자: worker -> shutdown이 닫는 recovery worker
                반환값: production close 결과
                작성 날짜: 2026/08/25
                """
                # 선택한 market worker만 barrier를 열고 production join 구현을 그대로 실행한다.
                if worker is market_recovery_worker:
                    close_entered.set()
                    release_publication.set()
                original_close(worker)

            def run_shutdown() -> None:
                """
                함수 이름: run_shutdown()
                기능: 교착 여부를 관찰할 daemon thread에서 실제 shutdown owner를 실행한다.
                인자: 없음
                반환값: 없음
                작성 날짜: 2026/08/25
                """
                # 결과와 예외를 분리해 timeout과 production 실패를 함께 진단한다.
                try:
                    shutdown_results.append(
                        request_application_shutdown(
                            runtime,
                            command_id="shutdown-market-recovery",
                            expected_version=0,
                        )
                    )
                except BaseException as error:
                    shutdown_errors.append(error)

            self.assertTrue(market_recovery_worker.request_recovery())
            self.assertTrue(recovery_started.wait(timeout=1.0))
            with patch.object(
                _AccountStreamRecoveryWorker,
                "close",
                autospec=True,
                side_effect=close_and_release,
            ):
                shutdown_thread = Thread(target=run_shutdown, daemon=True)
                shutdown_thread.start()
                shutdown_thread.join(timeout=2.0)

            # Market publication이 app lock을 얻고 종료한 뒤 shutdown도 CLOSED까지 완료돼야 한다.
            self.assertFalse(shutdown_thread.is_alive())
            self.assertTrue(close_entered.is_set())
            self.assertTrue(publication_completed.is_set())
            self.assertEqual(shutdown_errors, [])
            self.assertEqual(len(shutdown_results), 1)
            self.assertTrue(shutdown_results[0].accepted)
            self.assertIs(runtime.state.status, ApplicationStatus.CLOSED)

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

    def test_incomplete_manual_kill_cleanup_blocks_safe_shutdown(self) -> None:
        """
        함수 이름: test_incomplete_manual_kill_cleanup_blocks_safe_shutdown()
        기능: local Position·pending이 없어도 active C&L의 fresh cleanup 미완료가 종료를 차단하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime = _create_ready_runtime(
                Path(temporary_directory) / "history.jsonl"
            )
            try:
                controller = runtime.trading_controller
                with controller._session_lock:
                    # Fresh REST에만 남은 app order를 검출한 직후의 authoritative C&L 상태를 재현한다.
                    controller._manual_kill_active = True
                    controller._manual_kill_behavior_at_activation = (
                        ManualKillBehavior.CANCEL_AND_LIQUIDATE
                    )
                    controller._manual_kill_cleanup_verified = False

                with self.assertRaises(ShutdownBlockedError) as error_context:
                    request_application_shutdown(
                        runtime,
                        command_id="shutdown-incomplete-manual-kill",
                        expected_version=0,
                    )

                receipt = error_context.exception.receipt
                self.assertFalse(receipt.position_open)
                self.assertFalse(receipt.pending_order)
                self.assertTrue(receipt.reconciliation_required)
                self.assertIs(runtime.state.status, ApplicationStatus.READY)
            finally:
                close_application(runtime)  # 차단된 C&L fixture의 background 자원만 명시적으로 회수한다.

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
