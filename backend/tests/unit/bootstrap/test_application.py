"""Application runtime factory와 fail-closed execution mode 경계를 검증한다."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from threading import Event, Thread
import unittest
from unittest.mock import patch

from binance_auto_trader.application import TradingController
from binance_auto_trader.bootstrap import (
    ApplicationStatus,
    ExecutionMode,
    close_application,
    create_application_runtime,
    parse_execution_mode,
)
from binance_auto_trader.bootstrap.application import _FAKE_ORDER_CAPABILITY


FIXED_TIME = datetime(2026, 8, 21, 2, 0, tzinfo=timezone.utc)  # 시간 의존 상태를 고정한다.


class _StubSubscription:
    """
    클래스 이름: _StubSubscription
    기능: factory 조립 중 client Protocol을 만족하는 최소 subscription을 제공한다.
    작성 날짜: 2026/08/21
    """

    def close(self) -> None:
        """
        함수 이름: close()
        기능: 조립 identity test에서 외부 효과 없이 종료한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        return None


class _StubRestClient:
    """
    클래스 이름: _StubRestClient
    기능: APIGateway의 Kline과 account client Protocol shape만 제공한다.
    작성 날짜: 2026/08/21
    """

    def get_klines(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int,
    ) -> object:
        """
        함수 이름: get_klines()
        기능: 실행하지 않는 factory test용 빈 Kline payload를 반환한다.
        인자: symbol -> 요청 symbol
            interval -> 요청 interval
            limit -> 요청 최대 행 수
        반환값: 빈 payload
        작성 날짜: 2026/08/21
        """
        return []

    def get_account(self) -> object:
        """
        함수 이름: get_account()
        기능: 실행하지 않는 factory test용 빈 account payload를 반환한다.
        인자: 없음
        반환값: 빈 mapping
        작성 날짜: 2026/08/21
        """
        return {}


class _StubWebSocketClient:
    """
    클래스 이름: _StubWebSocketClient
    기능: WebSocketGateway의 두 subscription client Protocol shape를 제공한다.
    작성 날짜: 2026/08/21
    """

    def subscribe_all_kline_streams(
        self,
        *,
        symbol: str,
        intervals: tuple[str, ...],
        on_message: Callable[[object], None],
        on_disconnect: Callable[[], None],
    ) -> _StubSubscription:
        """
        함수 이름: subscribe_all_kline_streams()
        기능: factory test에서 호출 가능한 Kline subscription handle을 반환한다.
        인자: symbol -> 구독 symbol
            intervals -> 구독 interval tuple
            on_message -> message callback
            on_disconnect -> disconnect callback
        반환값: 최소 subscription handle
        작성 날짜: 2026/08/21
        """
        return _StubSubscription()

    def subscribe_account_info(
        self,
        *,
        on_message: Callable[[object], None],
        on_disconnect: Callable[[], None],
    ) -> _StubSubscription:
        """
        함수 이름: subscribe_account_info()
        기능: factory test에서 호출 가능한 account subscription handle을 반환한다.
        인자: on_message -> message callback
            on_disconnect -> disconnect callback
        반환값: 최소 subscription handle
        작성 날짜: 2026/08/21
        """
        return _StubSubscription()


class _StubHistoryRepository:
    """
    클래스 이름: _StubHistoryRepository
    기능: TradeHistoryController 조립에 필요한 read port만 제공한다.
    작성 날짜: 2026/08/21
    """

    def get_trade_history(self) -> tuple[object, ...]:
        """
        함수 이름: get_trade_history()
        기능: factory test에서 복원할 거래가 없음을 반환한다.
        인자: 없음
        반환값: 빈 tuple
        작성 날짜: 2026/08/21
        """
        return ()


class ApplicationFactoryTests(unittest.TestCase):
    """
    클래스 이름: ApplicationFactoryTests
    기능: mode parser, history port 선택과 runtime 객체 identity 조립을 검증한다.
    작성 날짜: 2026/08/21
    """

    def test_execution_mode_parser_accepts_only_exact_canonical_strings(
        self,
    ) -> None:
        """
        함수 이름: test_execution_mode_parser_accepts_only_exact_canonical_strings()
        기능: 네 canonical 문자열만 해당 ExecutionMode로 해석되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        expected_modes = {
            "disabled": ExecutionMode.DISABLED,
            "fake": ExecutionMode.FAKE,
            "testnet": ExecutionMode.TESTNET,
            "live": ExecutionMode.LIVE,
        }

        # 정확한 wire 문자열과 이미 검증된 Enum은 값을 보존한다.
        for raw_mode, expected_mode in expected_modes.items():
            with self.subTest(raw_mode=raw_mode):
                self.assertIs(parse_execution_mode(raw_mode), expected_mode)
                self.assertIs(parse_execution_mode(expected_mode), expected_mode)

    def test_execution_mode_parser_fails_closed_for_invalid_values(self) -> None:
        """
        함수 이름: test_execution_mode_parser_fails_closed_for_invalid_values()
        기능: 누락, unknown, 공백·대소문자 변형과 비문자 설정이 disabled인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        invalid_values = (
            None,
            "",
            "LIVE",
            " live ",
            "production",
            1,
            True,
            b"fake",
            object(),
        )

        # 설정 parsing 실패는 더 권한이 큰 모드로 절대 fallback하지 않는다.
        for invalid_value in invalid_values:
            with self.subTest(invalid_value=invalid_value):
                self.assertIs(
                    parse_execution_mode(invalid_value),
                    ExecutionMode.DISABLED,
                )

    def test_factory_preserves_controller_entity_and_lock_identity(self) -> None:
        """
        함수 이름: test_factory_preserves_controller_entity_and_lock_identity()
        기능: 조립된 Controller가 runtime의 동일 entity, gateway, repository와 RLock을 공유하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 주입 repository와 fake mode client로 identity 검사용 runtime을 조립한다.
        history_repository = _StubHistoryRepository()
        runtime = create_application_runtime(
            _StubRestClient(),
            _StubWebSocketClient(),
            history_repository=history_repository,
            execution_mode="fake",
            _fake_order_capability=_FAKE_ORDER_CAPABILITY,
            clock=lambda: FIXED_TIME,
        )

        # Runtime 공개 field와 기존 Controller의 내부 owner가 한 identity인지 확인한다.
        self.assertIs(runtime.lock, runtime.application_lock)
        self.assertIs(runtime.trading_controller.account, runtime.account)
        self.assertIs(runtime.trade_history_repository, history_repository)
        self.assertIs(
            runtime.market_data_controller._market_snapshot,
            runtime.market_snapshot,
        )
        self.assertIs(
            runtime.market_data_controller._regime_controller,
            runtime.regime_controller,
        )
        self.assertIs(
            runtime.regime_controller._regime_stm,
            runtime.regime_stm,
        )
        self.assertIs(
            runtime.regime_controller._trading_selection_port,
            runtime.trading_controller,
        )
        self.assertIs(
            runtime.trading_controller._session_lock,
            runtime.application_lock,
        )
        self.assertIs(
            runtime.regime_controller._evaluation_lock,
            runtime.application_lock,
        )
        self.assertIs(
            runtime.trade_history_controller._repository,
            history_repository,
        )
        self.assertIs(runtime.execution_mode, ExecutionMode.FAKE)
        self.assertIs(runtime.trading_controller._command_gate, True)
        self.assertFalse(runtime.trading_controller.command_enabled)
        self.assertIs(runtime.state.status, ApplicationStatus.CREATED)
        self.assertFalse(runtime.ready)
        self.assertIsNone(runtime.failure)

    def test_factory_requires_exactly_one_history_source(self) -> None:
        """
        함수 이름: test_factory_requires_exactly_one_history_source()
        기능: path와 repository 누락 또는 동시 주입을 조립 오류로 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 두 실패 조합에서 재사용할 client와 repository test double을 준비한다.
        rest_client = _StubRestClient()
        web_socket_client = _StubWebSocketClient()
        repository = _StubHistoryRepository()

        # History owner가 모호하거나 없으면 runtime을 생성하지 않는다.
        with self.assertRaisesRegex(ValueError, "exactly one"):
            create_application_runtime(rest_client, web_socket_client)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            create_application_runtime(
                rest_client,
                web_socket_client,
                history_path="history.jsonl",
                history_repository=repository,
            )

    def test_testnet_orders_require_separate_opt_in_and_live_stays_locked(
        self,
    ) -> None:
        """
        함수 이름: test_testnet_orders_require_separate_opt_in_and_live_stays_locked()
        기능: testnet 이중 gate와 Phase 13 전 live 불변 잠금을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # Generic factory는 testnet mode를 만들 수 있어도 고정 endpoint 권한을 발급하지 않는다.
        common_arguments = {
            "rest_client": _StubRestClient(),
            "web_socket_client": _StubWebSocketClient(),
            "history_repository": _StubHistoryRepository(),
            "clock": lambda: FIXED_TIME,
        }
        locked_testnet = create_application_runtime(
            **common_arguments,
            execution_mode="testnet",
        )
        locked_live = create_application_runtime(
            **common_arguments,
            execution_mode="live",
            allow_testnet_orders=True,
        )

        self.assertFalse(locked_testnet.trading_controller.command_enabled)
        self.assertFalse(locked_live.trading_controller.command_enabled)

        # Generic factory의 fake 문자열도 검증된 in-process 조립 권한 없이 주문 gate를 열지 못한다.
        with self.assertRaisesRegex(ValueError, "dedicated in-process fake"):
            create_application_runtime(
                **common_arguments,
                execution_mode="fake",
            )

        # Mode·bool·cap을 모두 전달해도 dedicated Testnet bootstrap을 우회할 수 없다.
        with self.assertRaisesRegex(ValueError, "dedicated fixed-endpoint"):
            create_application_runtime(
                **common_arguments,
                execution_mode="testnet",
                allow_testnet_orders=True,
                testnet_maximum_order_notional=Decimal("12.50"),
            )
        for invalid_mode in ("disabled", "live"):
            with self.subTest(invalid_mode=invalid_mode):
                with self.assertRaisesRegex(ValueError, "requires"):
                    create_application_runtime(
                        **common_arguments,
                        execution_mode=invalid_mode,
                        allow_testnet_orders=True,
                        testnet_maximum_order_notional=Decimal("12.50"),
                    )

        # Truthy 문자열이나 정수는 명시적 bool 권한으로 해석하지 않는다.
        for invalid_opt_in in (1, "true", None):
            with self.subTest(invalid_opt_in=invalid_opt_in):
                with self.assertRaises(TypeError):
                    create_application_runtime(
                        **common_arguments,
                        execution_mode="testnet",
                        allow_testnet_orders=invalid_opt_in,
                    )


class AccountStreamRecoveryRuntimeTests(unittest.TestCase):
    """
    클래스 이름: AccountStreamRecoveryRuntimeTests
    기능: testnet callback, runtime worker와 application 종료의 조립 계약을 검증한다.
    작성 날짜: 2026/08/22
    """

    def _create_ready_testnet_runtime(self):
        """
        함수 이름: _create_ready_testnet_runtime()
        기능: 외부 startup I/O 없이 자동 복구 Guard만 READY인 testnet runtime을 만든다.
        인자: 없음
        반환값: startup reconciliation 완료와 READY를 표시한 ApplicationRuntime
        작성 날짜: 2026/08/22
        """
        runtime = create_application_runtime(
            _StubRestClient(),
            _StubWebSocketClient(),
            history_repository=_StubHistoryRepository(),
            execution_mode="testnet",
            clock=lambda: FIXED_TIME,
        )

        # 이 test는 startup 자체가 아니라 callback 이후 복구 조건만 격리해 준비한다.
        with runtime.application_lock:
            runtime.trading_controller._command_gate = True
            with runtime.web_socket_gateway._lock:
                runtime.web_socket_gateway._account_connected = True
                runtime.web_socket_gateway._account_subscription = (
                    _StubSubscription()
                )
            runtime.trading_controller._startup_reconciliation_complete = True
            runtime._publish_state(
                status=ApplicationStatus.READY,
                failure=None,
                startup_trace=(),
            )

        return runtime

    def test_callback_recovers_only_after_ready_and_outside_application_lock(
        self,
    ) -> None:
        """
        함수 이름: test_callback_recovers_only_after_ready_and_outside_application_lock()
        기능: callback이 gate만 즉시 닫고 READY 이후 worker가 application lock 밖에서 복구하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        runtime = create_application_runtime(
            _StubRestClient(),
            _StubWebSocketClient(),
            history_repository=_StubHistoryRepository(),
            execution_mode="testnet",
            clock=lambda: FIXED_TIME,
        )
        recovery_callback = (
            runtime.web_socket_gateway._reconciliation_required_callback
        )
        recovery_calls: list[TradingController] = []
        application_lock_was_available = Event()
        recovery_started = Event()
        release_recovery = Event()
        recovery_completed = Event()

        def recover_account_stream(
            selected_controller: TradingController,
        ) -> _StubSubscription:
            """
            함수 이름: recover_account_stream()
            기능: 별도 probe가 application lock을 획득한 뒤 test barrier에서 복구 성공을 제어한다.
            인자: selected_controller -> runtime worker가 호출한 TradingController
            반환값: 복구 성공을 나타내는 fake subscription
            작성 날짜: 2026/08/22
            """
            recovery_calls.append(selected_controller)

            def acquire_application_lock() -> None:
                """
                함수 이름: acquire_application_lock()
                기능: worker의 Controller 호출 시 application lock이 비어 있는지 별도 thread로 확인한다.
                인자: 없음
                반환값: 없음
                작성 날짜: 2026/08/22
                """
                with runtime.application_lock:
                    application_lock_was_available.set()

            # 같은 worker thread의 RLock 재진입이 아니라 별도 thread의 실제 획득을 검사한다.
            lock_probe = Thread(
                target=acquire_application_lock,
                daemon=True,
            )
            lock_probe.start()
            if not application_lock_was_available.wait(timeout=1.0):
                raise AssertionError(
                    "recovery operation was called while application lock was held"
                )
            lock_probe.join()

            recovery_started.set()
            release_recovery.wait(timeout=1.0)
            with runtime.application_lock:
                selected_controller._stream_reconciliation_required = False
                with runtime.web_socket_gateway._lock:
                    runtime.web_socket_gateway._account_connected = True
                    runtime.web_socket_gateway._account_subscription = (
                        _StubSubscription()
                    )
            recovery_completed.set()  # 원래 Controller 성공처럼 fail-closed gate를 연 뒤 알린다.

            return _StubSubscription()

        try:
            with patch.object(
                TradingController,
                "reconnect_account_stream_after_reconciliation",
                autospec=True,
                side_effect=recover_account_stream,
            ):
                # STARTING 이전 callback은 gate만 닫고 worker 외부 I/O를 절대 시작하지 않는다.
                recovery_callback("disconnect_before_ready")
                recovery_worker = runtime._account_stream_recovery_worker
                self.assertIsNotNone(recovery_worker)
                with recovery_worker._state_lock:
                    self.assertIsNone(recovery_worker._active_thread)
                self.assertEqual(recovery_calls, [])

                # Startup reconciliation과 READY가 모두 공개된 뒤의 단절만 worker에 전달한다.
                with runtime.application_lock:
                    runtime.trading_controller._command_gate = True
                    with runtime.web_socket_gateway._lock:
                        runtime.web_socket_gateway._account_connected = True
                        runtime.web_socket_gateway._account_subscription = (
                            _StubSubscription()
                        )
                    runtime.trading_controller._startup_reconciliation_complete = True
                    runtime.trading_controller._stream_reconciliation_required = False
                    runtime._publish_state(
                        status=ApplicationStatus.READY,
                        failure=None,
                        startup_trace=(),
                    )
                recovery_callback("disconnect_after_ready")
                self.assertTrue(recovery_started.wait(timeout=1.0))
                self.assertFalse(runtime.trading_controller.command_enabled)
                self.assertTrue(application_lock_was_available.is_set())

                release_recovery.set()
                self.assertTrue(recovery_completed.wait(timeout=1.0))
                self.assertTrue(runtime.trading_controller.command_enabled)
                self.assertEqual(recovery_calls, [runtime.trading_controller])
        finally:
            release_recovery.set()
            close_application(runtime)  # Test 실패 시에도 worker와 runtime 자원을 회수한다.

    def test_duplicate_disconnects_coalesce_and_close_joins_worker(
        self,
    ) -> None:
        """
        함수 이름: test_duplicate_disconnects_coalesce_and_close_joins_worker()
        기능: 중복 disconnect가 한 복구로 합쳐지고 runtime close가 이를 join한 뒤 재실행을 막는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        runtime = self._create_ready_testnet_runtime()
        recovery_callback = (
            runtime.web_socket_gateway._reconciliation_required_callback
        )
        recovery_started = Event()
        release_recovery = Event()
        close_started = Event()
        recovery_calls: list[str] = []
        closed_states = []
        close_thread: Thread | None = None

        def recover_account_stream(
            selected_controller: TradingController,
        ) -> _StubSubscription:
            """
            함수 이름: recover_account_stream()
            기능: runtime close의 join과 중복 병합을 관찰할 때까지 단일 복구 호출을 유지한다.
            인자: selected_controller -> worker가 호출한 runtime TradingController
            반환값: release 이후 fake subscription
            작성 날짜: 2026/08/22
            """
            if selected_controller is not runtime.trading_controller:
                raise AssertionError("unexpected TradingController identity")
            recovery_calls.append("recovery")
            recovery_started.set()
            release_recovery.wait(timeout=1.0)  # Close thread가 join에서 대기할 시간을 만든다.

            return _StubSubscription()

        def close_runtime() -> None:
            """
            함수 이름: close_runtime()
            기능: 별도 thread에서 runtime close 진입을 알리고 반환 state를 보존한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/08/22
            """
            close_started.set()
            closed_states.append(close_application(runtime))

        try:
            with patch.object(
                TradingController,
                "reconnect_account_stream_after_reconciliation",
                autospec=True,
                side_effect=recover_account_stream,
            ):
                recovery_callback("first_disconnect")
                self.assertTrue(recovery_started.wait(timeout=1.0))
                recovery_callback("duplicate_disconnect")
                recovery_callback("another_duplicate_disconnect")
                self.assertEqual(recovery_calls, ["recovery"])

                # Close는 진행 중 worker를 건너뛰지 않고 Operation 반환까지 join한다.
                close_thread = Thread(target=close_runtime, daemon=True)
                close_thread.start()
                self.assertTrue(close_started.wait(timeout=1.0))
                self.assertTrue(close_thread.is_alive())
                release_recovery.set()
                close_thread.join(timeout=1.0)
                self.assertFalse(close_thread.is_alive())
                self.assertIs(closed_states[0].status, ApplicationStatus.CLOSED)
                self.assertEqual(
                    recovery_calls,
                    ["recovery"],
                )  # Close는 실행 전 pending rerun latch도 함께 폐기한다.

                # CLOSED publication 뒤 도착한 stale callback은 새 recovery thread를 만들지 않는다.
                recovery_callback("late_disconnect_after_close")
                self.assertEqual(recovery_calls, ["recovery"])
        finally:
            release_recovery.set()
            if close_thread is not None:
                close_thread.join(timeout=1.0)
            close_application(runtime)


if __name__ == "__main__":
    unittest.main()
