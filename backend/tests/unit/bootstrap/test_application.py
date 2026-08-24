"""Application runtime factory와 fail-closed execution mode 경계를 검증한다."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from threading import Event, Thread
import unittest
from unittest.mock import AsyncMock, Mock, patch

from binance_auto_trader.application import (
    AccountStreamRecoveryBlockedError,
    TradingController,
    TradingSessionStatus,
)
from binance_auto_trader.bootstrap import (
    ApplicationStatus,
    ExecutionMode,
    close_application,
    create_application_runtime,
    parse_execution_mode,
)
from binance_auto_trader.bootstrap.application import _FAKE_ORDER_CAPABILITY
from binance_auto_trader.domain.trading import OrderResult, OrderStatus


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
        trade_history_observer = Mock()
        runtime = create_application_runtime(
            _StubRestClient(),
            _StubWebSocketClient(),
            history_repository=history_repository,
            execution_mode="fake",
            _fake_order_capability=_FAKE_ORDER_CAPABILITY,
            trade_history_update_observer=trade_history_observer,
            clock=lambda: FIXED_TIME,
        )

        # Runtime 공개 field와 기존 Controller의 내부 owner가 한 identity인지 확인한다.
        self.assertIs(runtime.lock, runtime.application_lock)
        self.assertIs(runtime.trading_controller.account, runtime.account)
        self.assertIs(runtime.trade_history_controller.account, runtime.account)
        self.assertIs(
            runtime.trade_history_controller._trade_update_observer,
            trade_history_observer,
        )
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
        self.assertIsNone(runtime._trading_event_runtime_worker)
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

        # Trade event publication 경계는 callable이 아닌 값을 runtime에 보존하지 않는다.
        with self.assertRaisesRegex(
            TypeError,
            "trade_history_update_observer must be callable",
        ):
            create_application_runtime(
                rest_client,
                web_socket_client,
                history_repository=repository,
                trade_history_update_observer=object(),  # type: ignore[arg-type]
            )

        # Trading lifecycle publication 경계도 callable이 아닌 객체를 worker에 보존하지 않는다.
        with self.assertRaisesRegex(
            TypeError,
            "trading_session_update_observer must be callable",
        ):
            create_application_runtime(
                rest_client,
                web_socket_client,
                history_repository=repository,
                trading_session_update_observer=object(),  # type: ignore[arg-type]
            )

    def test_transport_observer_composes_worker_without_initial_publication(
        self,
    ) -> None:
        """
        함수 이름: test_transport_observer_composes_worker_without_initial_publication()
        기능: production observer가 단일 worker를 만들되 READY start만으로 event를 게시하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        trading_observer = Mock()
        runtime = create_application_runtime(
            _StubRestClient(),
            _StubWebSocketClient(),
            history_repository=_StubHistoryRepository(),
            trading_session_update_observer=trading_observer,
            clock=lambda: FIXED_TIME,
        )
        worker = runtime._trading_event_runtime_worker
        if worker is None:
            raise AssertionError("transport observer must compose an event worker")

        # Lifecycle READY만 직접 게시하고 여러 cadence 동안 빈 Controller cycle을 관찰한다.
        with runtime.application_lock:
            runtime._publish_state(
                status=ApplicationStatus.READY,
                failure=None,
                startup_trace=(),
            )
        try:
            self.assertTrue(worker.start())
            Event().wait(0.3)
            trading_observer.assert_not_called()
        finally:
            close_application(runtime)

    def test_inactive_cycle_failure_keeps_runtime_command_gate_closed(
        self,
    ) -> None:
        """
        함수 이름: test_inactive_cycle_failure_keeps_runtime_command_gate_closed()
        기능: session 전 terminal worker failure도 process lifetime reconciliation blocker로 남는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        failure_published = Event()

        def observe_failure(
            controller: object,
            execution_mode: object,
        ) -> None:
            """
            함수 이름: observe_failure()
            기능: fail-closed snapshot publication 완료를 test thread에 알린다.
            인자: controller -> fail-closed TradingController
                execution_mode -> runtime ExecutionMode
            반환값: 없음
            작성 날짜: 2026/08/24
            """
            if controller is None or execution_mode is None:
                raise AssertionError("observer inputs must be authoritative")

            failure_published.set()

        runtime = create_application_runtime(
            _StubRestClient(),
            _StubWebSocketClient(),
            history_repository=_StubHistoryRepository(),
            trading_session_update_observer=observe_failure,
            clock=lambda: FIXED_TIME,
        )
        worker = runtime._trading_event_runtime_worker
        if worker is None:
            raise AssertionError("transport observer must compose an event worker")
        with runtime.application_lock:
            runtime.trading_controller._startup_reconciliation_complete = True
            runtime._publish_state(
                status=ApplicationStatus.READY,
                failure=None,
                startup_trace=(),
            )

        # Controlled cycle failure는 active session이 없어도 runtime 전체 command gate를 잠근다.
        try:
            with patch.object(
                runtime.trading_controller,
                "run_event_runtime_cycle",
                new=AsyncMock(
                    side_effect=RuntimeError("controlled inactive cycle failure")
                ),
            ):
                self.assertTrue(worker.start())
                self.assertTrue(worker.request_processing())
                self.assertTrue(failure_published.wait(1.0))

            self.assertTrue(worker.failed)
            self.assertTrue(runtime.trading_controller.reconciliation_required)
            self.assertFalse(runtime.trading_controller.command_enabled)
        finally:
            close_application(runtime)

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

    def test_unknown_application_order_result_wakes_recovery_and_publishes_lock(
        self,
    ) -> None:
        """
        함수 이름: test_unknown_application_order_result_wakes_recovery_and_publishes_lock()
        기능: 알 수 없는 app 주문 event가 REST 복구를 깨우고 fail-closed lifecycle을 게시하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        published_states: list[
            tuple[TradingSessionStatus, bool, ExecutionMode]
        ] = []

        def observe_trading_session(
            controller: TradingController,
            execution_mode: ExecutionMode,
        ) -> None:
            """
            함수 이름: observe_trading_session()
            기능: callback 시점의 status와 command gate를 transport publication 증거로 보존한다.
            인자: controller -> fail-closed 상태를 소유한 TradingController
                execution_mode -> runtime의 고정 Testnet 실행 mode
            반환값: 없음
            작성 날짜: 2026/08/24
            """
            published_states.append(
                (
                    controller.status,
                    controller.command_enabled,
                    execution_mode,
                )
            )

        runtime = create_application_runtime(
            _StubRestClient(),
            _StubWebSocketClient(),
            history_repository=_StubHistoryRepository(),
            execution_mode="testnet",
            trading_session_update_observer=observe_trading_session,
            clock=lambda: FIXED_TIME,
        )
        recovery_worker = runtime._account_stream_recovery_worker
        if recovery_worker is None:
            raise AssertionError("testnet runtime must compose a recovery worker")

        # 실제 REST 없이 active callback 조건과 startup 완료 상태만 application lock 아래 준비한다.
        with runtime.application_lock:
            runtime.trading_controller._command_gate = True
            runtime.trading_controller._startup_reconciliation_complete = True
            runtime.trading_controller._status = TradingSessionStatus.RUNNING
            with runtime.web_socket_gateway._lock:
                runtime.web_socket_gateway._account_connected = True
                runtime.web_socket_gateway._account_subscription = (
                    _StubSubscription()
                )
            runtime._publish_state(
                status=ApplicationStatus.READY,
                failure=None,
                startup_trace=(),
            )

        unknown_result = OrderResult(
            symbol="ETHUSDT",
            client_order_id="bat-unknown-runtime-order",
            exchange_order_id="92001",
            status=OrderStatus.NEW,
            processed_at=FIXED_TIME,
        )
        order_callback = runtime.web_socket_gateway._order_result_callback
        if order_callback is None:
            raise AssertionError("runtime must compose an order-result callback")

        try:
            # Recovery thread는 mock wake로 대체해 network 없이 callback의 두 외부 결과를 검증한다.
            with patch.object(
                recovery_worker,
                "request_recovery",
                return_value=True,
            ) as request_recovery:
                result_accepted = order_callback(unknown_result)

            self.assertFalse(result_accepted)
            request_recovery.assert_called_once_with()
            self.assertIs(
                runtime.trading_controller.status,
                TradingSessionStatus.RECONCILIATION_REQUIRED,
            )
            self.assertFalse(runtime.trading_controller.command_enabled)
            self.assertEqual(
                published_states,
                [
                    (
                        TradingSessionStatus.RECONCILIATION_REQUIRED,
                        False,
                        ExecutionMode.TESTNET,
                    )
                ],
            )
        finally:
            close_application(runtime)

    def test_active_disconnect_wakes_recovery_and_publishes_lock_once(
        self,
    ) -> None:
        """
        함수 이름: test_active_disconnect_wakes_recovery_and_publishes_lock_once()
        기능: 일반 account stream 장애가 active session 잠금 lifecycle을 정확히 한 번 게시하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        published_states: list[
            tuple[TradingSessionStatus, bool, ExecutionMode]
        ] = []

        def observe_trading_session(
            controller: TradingController,
            execution_mode: ExecutionMode,
        ) -> None:
            """
            함수 이름: observe_trading_session()
            기능: 일반 stream 장애 callback 시점의 authoritative lifecycle을 보존한다.
            인자: controller -> fail-closed 상태를 소유한 TradingController
                execution_mode -> runtime의 고정 Testnet 실행 mode
            반환값: 없음
            작성 날짜: 2026/08/24
            """
            published_states.append(
                (
                    controller.status,
                    controller.command_enabled,
                    execution_mode,
                )
            )

        runtime = create_application_runtime(
            _StubRestClient(),
            _StubWebSocketClient(),
            history_repository=_StubHistoryRepository(),
            execution_mode="testnet",
            trading_session_update_observer=observe_trading_session,
            clock=lambda: FIXED_TIME,
        )
        recovery_worker = runtime._account_stream_recovery_worker
        if recovery_worker is None:
            raise AssertionError("testnet runtime must compose a recovery worker")
        recovery_callback = (
            runtime.web_socket_gateway._reconciliation_required_callback
        )

        # 실제 REST 없이 RUNNING session과 READY recovery Guard를 같은 application lock에서 준비한다.
        with runtime.application_lock:
            runtime.trading_controller._command_gate = True
            runtime.trading_controller._startup_reconciliation_complete = True
            runtime.trading_controller._status = TradingSessionStatus.RUNNING
            with runtime.web_socket_gateway._lock:
                runtime.web_socket_gateway._account_connected = True
                runtime.web_socket_gateway._account_subscription = (
                    _StubSubscription()
                )
            runtime._publish_state(
                status=ApplicationStatus.READY,
                failure=None,
                startup_trace=(),
            )

        try:
            # Recovery thread는 mock wake로 바꾸고 공통 disconnect callback의 즉시 publication을 격리한다.
            with patch.object(
                recovery_worker,
                "request_recovery",
                return_value=True,
            ) as request_recovery:
                recovery_callback("account_dispatcher_overflow")

            request_recovery.assert_called_once_with()
            self.assertIs(
                runtime.trading_controller.status,
                TradingSessionStatus.RECONCILIATION_REQUIRED,
            )
            self.assertFalse(runtime.trading_controller.command_enabled)
            self.assertEqual(
                published_states,
                [
                    (
                        TradingSessionStatus.RECONCILIATION_REQUIRED,
                        False,
                        ExecutionMode.TESTNET,
                    )
                ],
            )
        finally:
            close_application(runtime)

    def test_callback_recovers_only_after_ready_and_publishes_atomically(
        self,
    ) -> None:
        """
        함수 이름: test_callback_recovers_only_after_ready_and_publishes_atomically()
        기능: callback이 gate를 닫고 READY 복구와 성공 publication을 한 lock 구간에서 수행하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        account_publications: list[int] = []
        trading_publications: list[
            tuple[TradingSessionStatus, bool, ExecutionMode]
        ] = []
        recovery_publication_completed = Event()

        def observe_recovered_account(selected_account: object) -> None:
            """
            함수 이름: observe_recovered_account()
            기능: account stream 복구 뒤 transport에 게시한 Account version을 기록한다.
            인자: selected_account -> runtime의 authoritative Account
            반환값: 없음
            작성 날짜: 2026/08/24
            """
            account_publications.append(
                selected_account.version
            )  # 두 번째 REST snapshot이 transport observer까지 도달했음을 기록한다.

        def observe_recovered_trading_state(
            selected_controller: TradingController,
            execution_mode: ExecutionMode,
        ) -> None:
            """
            함수 이름: observe_recovered_trading_state()
            기능: fail-close와 복구 성공의 lifecycle·command gate publication을 기록한다.
            인자: selected_controller -> authoritative TradingController
                execution_mode -> runtime의 고정 Testnet mode
            반환값: 없음
            작성 날짜: 2026/08/24
            """
            trading_publications.append(
                (
                    selected_controller.status,
                    selected_controller.command_enabled,
                    execution_mode,
                )
            )
            if selected_controller.command_enabled:
                recovery_publication_completed.set()  # Backend gate 재개가 UI observer까지 도달해야 성공이다.

        runtime = create_application_runtime(
            _StubRestClient(),
            _StubWebSocketClient(),
            history_repository=_StubHistoryRepository(),
            execution_mode="testnet",
            account_update_observer=observe_recovered_account,
            trading_session_update_observer=(
                observe_recovered_trading_state
            ),
            clock=lambda: FIXED_TIME,
        )
        recovery_callback = (
            runtime.web_socket_gateway._reconciliation_required_callback
        )
        recovery_calls: list[TradingController] = []
        application_lock_probe_results: list[bool] = []
        recovery_started = Event()
        release_recovery = Event()
        recovery_completed = Event()

        def recover_account_stream(
            selected_controller: TradingController,
            *,
            recovery_commit_observer: Callable[[], object] | None = None,
        ) -> _StubSubscription:
            """
            함수 이름: recover_account_stream()
            기능: test barrier 뒤 gate commit과 observer가 같은 application lock을 쓰는지 검증한다.
            인자: selected_controller -> runtime worker가 호출한 TradingController
                recovery_commit_observer -> Controller commit 직후 호출할 application hook
            반환값: 복구 성공을 나타내는 fake subscription
            작성 날짜: 2026/08/22
            """
            recovery_calls.append(selected_controller)
            recovery_started.set()
            release_recovery.wait(timeout=1.0)
            with runtime.application_lock:
                selected_controller._stream_reconciliation_required = False
                with runtime.web_socket_gateway._lock:
                    runtime.web_socket_gateway._account_connected = True
                    runtime.web_socket_gateway._account_subscription = (
                        _StubSubscription()
                    )

                def probe_application_lock() -> None:
                    """
                    함수 이름: probe_application_lock()
                    기능: gate commit과 publication 사이 application lock이 점유됐는지 확인한다.
                    인자: 없음
                    반환값: 없음
                    작성 날짜: 2026/08/24
                    """
                    acquired = runtime.application_lock.acquire(blocking=False)
                    application_lock_probe_results.append(acquired)
                    if acquired:
                        runtime.application_lock.release()  # 예상 밖 성공도 lock을 누수하지 않는다.

                # 별도 thread가 gate-open과 observer 사이에 command lock을 얻지 못해야 한다.
                lock_probe = Thread(
                    target=probe_application_lock,
                    daemon=True,
                )
                lock_probe.start()
                lock_probe.join(timeout=1.0)
                if lock_probe.is_alive():
                    raise AssertionError("application lock probe did not complete")
                if application_lock_probe_results != [False]:
                    raise AssertionError(
                        "recovery commit and publication must share one lock"
                    )
                if recovery_commit_observer is None:
                    raise AssertionError("recovery commit observer is required")
                recovery_commit_observer()
            recovery_completed.set()  # Gate와 publication이 모두 끝난 뒤 worker 성공을 알린다.

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
                self.assertEqual(account_publications, [])
                self.assertEqual(
                    trading_publications,
                    [
                        (
                            TradingSessionStatus.NOT_STARTED,
                            False,
                            ExecutionMode.TESTNET,
                        )
                    ],
                )
                trading_publications.clear()  # READY 이후 fail-close와 recovery pair만 격리한다.

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
                self.assertEqual(
                    application_lock_probe_results,
                    [],
                )  # Release 전에는 아직 gate commit/publication 임계 구역에 진입하지 않는다.

                release_recovery.set()
                self.assertTrue(recovery_completed.wait(timeout=1.0))
                self.assertTrue(
                    recovery_publication_completed.wait(timeout=1.0)
                )
                self.assertTrue(runtime.trading_controller.command_enabled)
                self.assertEqual(recovery_calls, [runtime.trading_controller])
                self.assertEqual(application_lock_probe_results, [False])
                self.assertEqual(
                    account_publications,
                    [runtime.account.version],
                )
                self.assertEqual(
                    trading_publications,
                    [
                        (
                            TradingSessionStatus.NOT_STARTED,
                            False,
                            ExecutionMode.TESTNET,
                        ),
                        (
                            TradingSessionStatus.NOT_STARTED,
                            True,
                            ExecutionMode.TESTNET,
                        ),
                    ],
                )
        finally:
            release_recovery.set()
            close_application(runtime)  # Test 실패 시에도 worker와 runtime 자원을 회수한다.

    def test_recovery_publication_failure_permanently_fail_closes_commands(
        self,
    ) -> None:
        """
        함수 이름: test_recovery_publication_failure_permanently_fail_closes_commands()
        기능: 복구 성공 publication 실패가 backend-only 주문 재개로 남지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        account_observer = Mock(
            side_effect=RuntimeError("controlled account publication failure")
        )
        trading_observer = Mock()
        runtime = create_application_runtime(
            _StubRestClient(),
            _StubWebSocketClient(),
            history_repository=_StubHistoryRepository(),
            execution_mode="testnet",
            account_update_observer=account_observer,
            trading_session_update_observer=trading_observer,
            clock=lambda: FIXED_TIME,
        )
        recovery_worker = runtime._account_stream_recovery_worker
        if recovery_worker is None:
            raise AssertionError("testnet runtime must compose a recovery worker")

        # 복구 REST/WS가 gate를 연 직후라고 가정하고 READY publication 상태를 조립한다.
        with runtime.application_lock:
            runtime.trading_controller._command_gate = True
            runtime.trading_controller._startup_reconciliation_complete = True
            runtime.trading_controller._stream_reconciliation_required = False
            with runtime.web_socket_gateway._lock:
                runtime.web_socket_gateway._account_connected = True
                runtime.web_socket_gateway._account_subscription = (
                    _StubSubscription()
                )
            runtime._publish_state(
                status=ApplicationStatus.READY,
                failure=None,
                startup_trace=(),
            )
        self.assertTrue(runtime.trading_controller.command_enabled)

        def commit_recovery_with_observer_failure(
            selected_controller: TradingController,
            *,
            recovery_commit_observer: Callable[[], object] | None = None,
        ) -> _StubSubscription:
            """
            함수 이름: commit_recovery_with_observer_failure()
            기능: 열린 gate와 실패하는 application observer를 같은 Controller lock 구간으로 재현한다.
            인자: selected_controller -> runtime TradingController
                recovery_commit_observer -> 복구 성공 publication hook
            반환값: 도달하지 않는 fake subscription
            작성 날짜: 2026/08/24
            """
            if selected_controller is not runtime.trading_controller:
                raise AssertionError("unexpected TradingController identity")
            if recovery_commit_observer is None:
                raise AssertionError("recovery commit observer is required")

            # 실제 Controller처럼 gate commit과 observer callback을 같은 application RLock에 둔다.
            with runtime.application_lock:
                recovery_commit_observer()
            return _StubSubscription()

        try:
            with patch.object(
                TradingController,
                "reconnect_account_stream_after_reconciliation",
                autospec=True,
                side_effect=commit_recovery_with_observer_failure,
            ):
                with self.assertRaises(AccountStreamRecoveryBlockedError):
                    recovery_worker._recovery_operation()

            # Account publication 실패 뒤 fail-close trading snapshot을 한 번만 시도한다.
            account_observer.assert_called_once_with(runtime.account)
            trading_observer.assert_called_once_with(
                runtime.trading_controller,
                ExecutionMode.TESTNET,
            )
            self.assertTrue(runtime.trading_controller.reconciliation_required)
            self.assertFalse(runtime.trading_controller.command_enabled)
        finally:
            close_application(runtime)

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
            *,
            recovery_commit_observer: Callable[[], object] | None = None,
        ) -> _StubSubscription:
            """
            함수 이름: recover_account_stream()
            기능: runtime close의 join과 중복 병합을 관찰할 때까지 단일 복구 호출을 유지한다.
            인자: selected_controller -> worker가 호출한 runtime TradingController
                recovery_commit_observer -> gate commit과 같은 lock에서 실행할 application hook
            반환값: release 이후 fake subscription
            작성 날짜: 2026/08/22
            """
            if selected_controller is not runtime.trading_controller:
                raise AssertionError("unexpected TradingController identity")
            recovery_calls.append("recovery")
            recovery_started.set()
            release_recovery.wait(timeout=1.0)  # Close thread가 join에서 대기할 시간을 만든다.
            if recovery_commit_observer is None:
                raise AssertionError("recovery commit observer is required")
            with runtime.application_lock:
                recovery_commit_observer()  # 실제 Controller의 atomic commit callback 경계를 재현한다.

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
