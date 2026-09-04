"""Application startup 순서와 소유 subscription 종료 lifecycle을 정의한다."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from binance_auto_trader.application import (
    TradingSessionError,
    TradingSessionFailureCode,
    TradingSessionStatus,
)
from binance_auto_trader.bootstrap.application import (
    ApplicationRuntime,
    ApplicationStartupError,
    ApplicationStateSnapshot,
    ApplicationStatus,
    StartupFailure,
    StartupFailureCode,
    StartupStage,
    StartupTraceEntry,
    StartupTraceResult,
)


class ShutdownReceiptStatus(str, Enum):
    """
    클래스 이름: ShutdownReceiptStatus
    기능: 안전 종료 요청의 수락과 open exposure 차단 결과를 정규화한다.
    작성 날짜: 2026/08/24
    """

    ACCEPTED = "accepted"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class ShutdownSafetyReceipt:
    """
    클래스 이름: ShutdownSafetyReceipt
    기능: credential 없이 종료 수락 여부와 authoritative exposure 판정을 불변으로 보존한다.
    작성 날짜: 2026/08/24
    """

    accepted: bool
    status: ShutdownReceiptStatus
    version: int
    position_open: bool
    pending_order: bool
    reconciliation_required: bool

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 종료 receipt의 타입, version과 accepted/status 조합을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # bool의 int 상속을 배제하고 외부 optimistic version과 같은 정수 계약을 유지한다.
        if type(self.accepted) is not bool:
            raise TypeError("accepted must be a bool")
        if not isinstance(self.status, ShutdownReceiptStatus):
            raise TypeError("status must be a ShutdownReceiptStatus")
        if type(self.version) is not int:
            raise TypeError("version must be an int")
        if self.version < 0:
            raise ValueError("version must not be negative")

        # Exposure flag는 truthy 객체를 허용하지 않고 status와 accepted를 정확히 결합한다.
        for flag_name in (
            "position_open",
            "pending_order",
            "reconciliation_required",
        ):
            if type(getattr(self, flag_name)) is not bool:
                raise TypeError(f"{flag_name} must be a bool")
        expected_status = (
            ShutdownReceiptStatus.ACCEPTED
            if self.accepted
            else ShutdownReceiptStatus.BLOCKED
        )
        if self.status is not expected_status:
            raise ValueError("accepted and status must describe the same outcome")
        if self.accepted and (
            self.position_open
            or self.pending_order
            or self.reconciliation_required
        ):
            raise ValueError("accepted shutdown cannot retain open exposure")

    def to_blocked_details(self) -> dict[str, object]:
        """
        함수 이름: to_blocked_details()
        기능: HTTP 409 오류에 넣을 exact blocked safety receipt를 JSON object로 만든다.
        인자: 없음
        반환값: credential과 주문 ID가 없는 blocked detail dictionary
        작성 날짜: 2026/08/24
        """
        if self.accepted:
            raise ValueError("accepted receipt cannot be mapped as blocked details")

        # 사용자가 종료 전 해소할 blocker 종류만 공개하고 symbol·수량·주문 ID는 숨긴다.
        return {
            "accepted": False,
            "status": self.status.value,
            "version": self.version,
            "position_open": self.position_open,
            "pending_order": self.pending_order,
            "reconciliation_required": self.reconciliation_required,
        }


class ShutdownBlockedError(RuntimeError):
    """
    클래스 이름: ShutdownBlockedError
    기능: open position·pending order·재조정 상태가 안전 종료를 차단했음을 전달한다.
    작성 날짜: 2026/08/24
    """

    code = "SHUTDOWN_BLOCKED_BY_OPEN_EXPOSURE"

    def __init__(self, receipt: ShutdownSafetyReceipt) -> None:
        """
        함수 이름: __init__()
        기능: blocked 상태만 가진 secret-free safety receipt를 보존한다.
        인자: receipt -> authoritative exposure 판정으로 만든 blocked receipt
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        if not isinstance(receipt, ShutdownSafetyReceipt):
            raise TypeError("receipt must be a ShutdownSafetyReceipt")
        if receipt.accepted:
            raise ValueError("shutdown blocked error requires a blocked receipt")

        super().__init__("Open exposure must be resolved before shutdown.")
        self.receipt = receipt  # transport는 공개 가능한 bool과 version만 이 객체에서 읽는다.


class _RegimeReadinessError(RuntimeError):
    """
    클래스 이름: _RegimeReadinessError
    기능: 시장 load가 반환됐지만 최초 REGIME 결과가 준비되지 않은 내부 상태를 나타낸다.
    작성 날짜: 2026/08/21
    """


def _create_failure(
    stage: StartupStage,
    code: StartupFailureCode,
    message: str,
) -> StartupFailure:
    """
    함수 이름: _create_failure()
    기능: lifecycle helper에서 secret 없는 공통 StartupFailure를 생성한다.
    인자: stage -> 실패한 startup 단계
        code -> transport가 분기할 typed failure code
        message -> 사용자에게 노출 가능한 일반 설명
    반환값: 검증된 StartupFailure
    작성 날짜: 2026/08/21
    """
    return StartupFailure(
        stage=stage,
        code=code,
        message=message,
        retryable=False,
    )


def _record_startup_trace(
    runtime: ApplicationRuntime,
    *,
    message_id: str,
    receiver: str,
    related_id: str,
    state_version_before: int,
    state_version_after: int,
    failure: StartupFailure | None = None,
) -> None:
    """
    함수 이름: _record_startup_trace()
    기능: 한 Communication startup 호출의 성공 또는 typed failure trace를 추가한다.
    인자: runtime -> trace를 publish할 application runtime
        message_id -> Communication Diagram의 최상위 메시지 번호
        receiver -> 호출을 소유한 기존 Controller 이름
        related_id -> raw payload가 아닌 안전한 논리 대상 ID
        state_version_before -> 호출 전 authoritative state version
        state_version_after -> 호출 후 authoritative state version
        failure -> 실패 trace의 typed failure 또는 성공이면 None
    반환값: 없음
    작성 날짜: 2026/08/21
    """
    # 성공 여부를 enum과 optional failure code의 일관된 조합으로 정규화한다.
    trace_result = (
        StartupTraceResult.SUCCESS
        if failure is None
        else StartupTraceResult.FAILURE
    )
    typed_failure_code = None if failure is None else failure.code
    # raw payload 대신 command, version과 논리 식별자만 trace에 보존한다.
    trace_entry = StartupTraceEntry(
        message_id=message_id,
        caller="UIStateController",
        receiver=receiver,
        command_event_id=runtime.startup_command_id,
        state_version_before=state_version_before,
        state_version_after=state_version_after,
        related_id=related_id,
        result=trace_result,
        typed_failure_code=typed_failure_code,
    )
    runtime._append_startup_trace(trace_entry)  # 같은 RLock 아래 trace를 publish한다.


def _raise_stage_failure(
    runtime: ApplicationRuntime,
    *,
    message_id: str,
    receiver: str,
    related_id: str,
    state_version_before: int,
    state_version_after: int,
    failure: StartupFailure,
    cause: BaseException | None = None,
) -> None:
    """
    함수 이름: _raise_stage_failure()
    기능: 실패 trace를 먼저 보존한 뒤 typed ApplicationStartupError를 발생시킨다.
    인자: runtime -> 실패 trace를 보존할 runtime
        message_id -> 실패한 Communication 메시지 번호
        receiver -> 실패한 Controller 이름
        related_id -> 안전한 논리 대상 ID
        state_version_before -> 실패 호출 전 state version
        state_version_after -> 실패 호출 뒤 state version
        failure -> 호출자에게 전달할 typed failure
        cause -> traceback chain에만 보존할 원래 예외 또는 None
    반환값: 정상 반환 없이 ApplicationStartupError 발생
    작성 날짜: 2026/08/21
    """
    # 예외를 전파하기 전에 실패한 Communication 호출의 trace를 먼저 publish한다.
    _record_startup_trace(
        runtime,
        message_id=message_id,
        receiver=receiver,
        related_id=related_id,
        state_version_before=state_version_before,
        state_version_after=state_version_after,
        failure=failure,
    )
    # 원인 예외가 있을 때만 traceback chain을 연결하고 공개 오류는 동일하게 유지한다.
    startup_error = ApplicationStartupError(failure)
    if cause is None:
        raise startup_error

    raise startup_error from cause


def _validate_regime_readiness(runtime: ApplicationRuntime) -> None:
    """
    함수 이름: _validate_regime_readiness()
    기능: fail-closed RegimeController 반환 뒤 결과, 추천과 indicator 준비를 명시 검사한다.
    인자: runtime -> 검사할 RegimeController와 MarketSnapshot을 가진 runtime
    반환값: 준비 조건이 모두 맞으면 없음
    작성 날짜: 2026/08/21
    """
    # 최초 판정 결과와 추천 REGIME이 모두 publish됐는지 확인한다.
    regime_result = runtime.regime_controller.last_regime_result
    if regime_result is None:
        raise _RegimeReadinessError("last regime result is not ready")
    if runtime.regime_controller.recommended_regime is None:
        raise _RegimeReadinessError("recommended regime is not ready")

    # 판정에 사용한 indicator snapshot도 같은 startup에서 준비돼야 한다.
    indicator_snapshot = runtime.regime_controller.indicator_snapshot
    if indicator_snapshot is None or not indicator_snapshot.ready:
        raise _RegimeReadinessError("indicator snapshot is not ready")


def _start_market_and_regime(runtime: ApplicationRuntime) -> None:
    """
    함수 이름: _start_market_and_regime()
    기능: Communication 1의 시장 초기화 후 REGIME readiness를 명시 검증한다.
    인자: runtime -> market과 regime Controller가 조립된 runtime
    반환값: 시장과 REGIME이 모두 준비되면 없음
    작성 날짜: 2026/08/21
    """
    # Communication 1의 시작 version을 보존하고 시장 초기화를 먼저 수행한다.
    market_version_before = runtime.market_snapshot.version
    try:
        runtime.market_data_controller.initialize_market_data()
    except Exception as error:
        failure = _create_failure(
            StartupStage.MARKET,
            StartupFailureCode.MARKET_INITIALIZATION_FAILED,
            "Market startup initialization failed.",
        )
        _raise_stage_failure(
            runtime,
            message_id="1",
            receiver="MarketDataController",
            related_id=runtime.market_snapshot.symbol,
            state_version_before=market_version_before,
            state_version_after=runtime.market_snapshot.version,
            failure=failure,
            cause=error,
        )

    # Controller 반환만 신뢰하지 않고 authoritative MarketSnapshot readiness를 재검사한다.
    if not runtime.market_snapshot.ready:
        failure = _create_failure(
            StartupStage.MARKET,
            StartupFailureCode.MARKET_NOT_READY,
            "Market startup completed without a ready snapshot.",
        )
        _raise_stage_failure(
            runtime,
            message_id="1",
            receiver="MarketDataController",
            related_id=runtime.market_snapshot.symbol,
            state_version_before=market_version_before,
            state_version_after=runtime.market_snapshot.version,
            failure=failure,
        )

    # 시장과 연쇄 실행된 REGIME 판정의 세 가지 완료조건을 함께 검증한다.
    try:
        _validate_regime_readiness(runtime)
    except _RegimeReadinessError as error:
        failure = _create_failure(
            StartupStage.REGIME,
            StartupFailureCode.REGIME_NOT_READY,
            "Regime startup completed without a ready recommendation.",
        )
        _raise_stage_failure(
            runtime,
            message_id="1",
            receiver="MarketDataController",
            related_id=runtime.market_snapshot.symbol,
            state_version_before=market_version_before,
            state_version_after=runtime.market_snapshot.version,
            failure=failure,
            cause=error,
        )

    # 시장과 REGIME이 모두 준비된 뒤에만 Communication 1 성공을 기록한다.
    _record_startup_trace(
        runtime,
        message_id="1",
        receiver="MarketDataController",
        related_id=runtime.market_snapshot.symbol,
        state_version_before=market_version_before,
        state_version_after=runtime.market_snapshot.version,
    )


def _start_account(runtime: ApplicationRuntime) -> None:
    """
    함수 이름: _start_account()
    기능: Communication 2의 REST Account 적용과 stream 시작을 수행하고 readiness를 검사한다.
    인자: runtime -> TradingController와 Account가 조립된 runtime
    반환값: Account가 준비되고 subscription이 열리면 없음
    작성 날짜: 2026/08/21
    """
    # Communication 2의 시작 version을 보존하고 REST→stream 순서의 load를 실행한다.
    account_version_before = runtime.account.version
    try:
        runtime.trading_controller.load_account()
    except Exception as error:
        failure = _create_failure(
            StartupStage.ACCOUNT,
            StartupFailureCode.ACCOUNT_INITIALIZATION_FAILED,
            "Account startup initialization failed.",
        )
        _raise_stage_failure(
            runtime,
            message_id="2",
            receiver="TradingController",
            related_id=runtime.account.valuation_asset,
            state_version_before=account_version_before,
            state_version_after=runtime.account.version,
            failure=failure,
            cause=error,
        )

    # Application RLock 아래에서 REST snapshot과 stream handle의 완료조건을 함께 읽는다.
    account_ready = runtime.account.ready
    account_subscription = runtime.trading_controller.account_subscription
    if not account_ready or account_subscription is None:
        failure = _create_failure(
            StartupStage.ACCOUNT,
            StartupFailureCode.ACCOUNT_NOT_READY,
            "Account startup completed without a ready snapshot and stream.",
        )
        _raise_stage_failure(
            runtime,
            message_id="2",
            receiver="TradingController",
            related_id=runtime.account.valuation_asset,
            state_version_before=account_version_before,
            state_version_after=runtime.account.version,
            failure=failure,
        )

    # Account snapshot과 stream handle이 모두 준비된 뒤 성공 trace를 기록한다.
    _record_startup_trace(
        runtime,
        message_id="2",
        receiver="TradingController",
        related_id=runtime.account.valuation_asset,
        state_version_before=account_version_before,
        state_version_after=runtime.account.version,
    )


def _start_history_and_performance(runtime: ApplicationRuntime) -> None:
    """
    함수 이름: _start_history_and_performance()
    기능: Communication 3의 history publish 뒤 주문·Position startup 재조정까지 수행한다.
    인자: runtime -> history repository와 Controller가 조립된 runtime
    반환값: history와 performance가 함께 publish되면 없음
    작성 날짜: 2026/08/21
    """
    # Communication 3 전 application version을 보존하고 history publication을 실행한다.
    publication_version_before = runtime.state.version
    try:
        runtime.trade_history_controller.load_trade_history()
    except Exception as error:
        failure = _create_failure(
            StartupStage.HISTORY,
            StartupFailureCode.HISTORY_INITIALIZATION_FAILED,
            "History and performance startup initialization failed.",
        )
        _raise_stage_failure(
            runtime,
            message_id="3",
            receiver="TradeHistoryController",
            related_id="trade-history",
            state_version_before=publication_version_before,
            state_version_after=runtime.state.version,
            failure=failure,
            cause=error,
        )

    # History가 authoritative local 기준으로 publish된 뒤에만 Binance 주문과 Position을 대조한다.
    try:
        runtime.trading_controller.reconcile_startup_state()
    except Exception as error:
        failure = _create_failure(
            StartupStage.RECONCILIATION,
            StartupFailureCode.ORDER_RECONCILIATION_FAILED,
            "Order and position startup reconciliation failed.",
        )
        _raise_stage_failure(
            runtime,
            message_id="3",
            receiver="TradingController",
            related_id=runtime.market_snapshot.symbol,
            state_version_before=publication_version_before,
            state_version_after=runtime.state.version,
            failure=failure,
            cause=error,
        )

    # Controller가 정상 반환해도 full reconciliation 완료 flag를 별도로 확인해 fail closed한다.
    if not runtime.trading_controller.startup_reconciliation_complete:
        failure = _create_failure(
            StartupStage.RECONCILIATION,
            StartupFailureCode.ORDER_RECONCILIATION_NOT_READY,
            "Order and position startup reconciliation is not ready.",
        )
        _raise_stage_failure(
            runtime,
            message_id="3",
            receiver="TradingController",
            related_id=runtime.market_snapshot.symbol,
            state_version_before=publication_version_before,
            state_version_after=runtime.state.version,
            failure=failure,
        )

    # Durable CANCEL_AND_LIQUIDATE는 startup query/history가 끝난 뒤에만 recovery SELL을 재개한다.
    runtime.trading_controller.resume_manual_kill_cleanup()

    # Message 3은 local history와 exchange reconciliation이 모두 끝난 startup version을 기록한다.
    _record_startup_trace(
        runtime,
        message_id="3",
        receiver="TradeHistoryController",
        related_id="trade-history",
        state_version_before=publication_version_before,
        state_version_after=publication_version_before + 1,
    )


def _close_account_subscription_safely(
    runtime: ApplicationRuntime,
) -> BaseException | None:
    """
    함수 이름: _close_account_subscription_safely()
    기능: startup 원인 예외를 가리지 않고 열린 account subscription 정리를 시도한다.
    인자: runtime -> Account subscription을 소유한 TradingController runtime
    반환값: 정리 실패 예외 또는 정상·구독 없음이면 None
    작성 날짜: 2026/08/21
    """
    # 열린 handle이 있을 때만 cleanup을 시도하고 실패는 원인 예외와 분리해 반환한다.
    account_subscription = runtime.trading_controller.account_subscription
    if account_subscription is None:
        return None

    try:
        account_subscription.close()
    except Exception as error:
        return error

    return None


def _attempt_shutdown_cleanup(
    operation: Callable[[], object],
    *,
    resource_name: str,
    cleanup_failures: list[tuple[str, BaseException]],
) -> None:
    """
    함수 이름: _attempt_shutdown_cleanup()
    기능: 하나의 application 종료 Operation을 시도하고 실패를 순서대로 보존한다.
    인자: operation -> worker, stream, session 또는 subscription 종료 Operation
        resource_name -> 후속 진단 note에 사용할 credential 없는 자원 이름
        cleanup_failures -> 최초와 후속 실패를 호출 순서대로 담을 목록
    반환값: 성공·실패 모두 없음
    작성 날짜: 2026/09/04
    """
    # BaseException을 전파하기 전에 모든 후속 소유 자원을 회수할 수 있게 분리한다.
    try:
        operation()
    except BaseException as error:
        cleanup_failures.append(
            (resource_name, error)
        )  # 예외 identity와 traceback은 바꾸지 않고 진단 이름만 같이 보존한다.


def start_application(runtime: ApplicationRuntime) -> ApplicationStateSnapshot:
    """
    함수 이름: start_application()
    기능: market, REGIME, account, history/performance 순서로 초기화한 뒤에만 READY를 publish한다.
    인자: runtime -> create_application_runtime이 조립한 application runtime
    반환값: 성공한 READY ApplicationStateSnapshot
    작성 날짜: 2026/08/21
    """
    # factory가 조립한 runtime 외 객체는 lifecycle mutation 전에 거부한다.
    if not isinstance(runtime, ApplicationRuntime):
        raise TypeError("runtime must be an ApplicationRuntime")

    with runtime.application_lock:
        # 현재 lifecycle에 따라 duplicate, 동시 시작, 종료 후 시작과 이전 실패를 구분한다.
        current_state = runtime.state
        if current_state.status is ApplicationStatus.READY:
            event_runtime_worker = runtime._trading_event_runtime_worker
            if event_runtime_worker is not None:
                event_runtime_worker.start()
            return current_state  # 성공한 runtime의 중복 startup과 worker 시작은 멱등 no-op이다.
        if current_state.status is ApplicationStatus.STARTING:
            failure = _create_failure(
                StartupStage.APPLICATION,
                StartupFailureCode.APPLICATION_ALREADY_STARTING,
                "Application startup is already in progress.",
            )
            raise ApplicationStartupError(failure)
        if current_state.status in (
            ApplicationStatus.SHUTTING_DOWN,
            ApplicationStatus.CLOSED,
        ):
            failure = _create_failure(
                StartupStage.APPLICATION,
                StartupFailureCode.APPLICATION_CLOSED,
                "Closing or closed application runtime cannot be started.",
            )
            raise ApplicationStartupError(failure)
        if current_state.status is ApplicationStatus.FAILED:
            if current_state.failure is None:
                raise RuntimeError("FAILED application state has no failure")
            raise ApplicationStartupError(current_state.failure)

        # 초기 trace를 비우고 STARTING을 먼저 publish하되 ready는 계속 false로 유지한다.
        runtime._publish_state(
            status=ApplicationStatus.STARTING,
            failure=None,
            startup_trace=(),
        )
        try:
            _start_market_and_regime(runtime)
            _start_account(runtime)
            _start_history_and_performance(runtime)
        except ApplicationStartupError as startup_error:
            cleanup_error = _close_account_subscription_safely(runtime)
            if cleanup_error is not None:
                startup_error.add_note(
                    "Account subscription cleanup failed after startup failure."
                )

            # 실패 state는 완료된 trace만 보존하고 READY를 한 번도 publish하지 않는다.
            runtime._publish_state(
                status=ApplicationStatus.FAILED,
                failure=startup_error.failure,
                startup_trace=runtime.startup_trace,
            )
            raise

        # 세 단계가 모두 성공한 이 지점에서만 transport 공개가 가능한 READY가 된다.
        ready_state = runtime._publish_state(
            status=ApplicationStatus.READY,
            failure=None,
            startup_trace=runtime.startup_trace,
        )
        event_runtime_worker = runtime._trading_event_runtime_worker
        if event_runtime_worker is not None:
            event_runtime_worker.start()
        return ready_state  # READY 뒤 단일 worker를 시작하되 startup thread에서 cycle을 직접 실행하지 않는다.


def close_application(runtime: ApplicationRuntime) -> ApplicationStateSnapshot:
    """
    함수 이름: close_application()
    기능: trading/account worker와 subscription을 순서대로 회수하고 lifecycle을 멱등 종료한다.
    인자: runtime -> 종료할 application runtime
    반환값: CLOSED ApplicationStateSnapshot
    작성 날짜: 2026/08/21
    """
    # factory가 조립한 runtime 외 객체는 자원 cleanup 전에 거부한다.
    if not isinstance(runtime, ApplicationRuntime):
        raise TypeError("runtime must be an ApplicationRuntime")

    cleanup_failures: list[tuple[str, BaseException]] = []

    # Worker stop/join은 application lock 밖에서 수행하되 하나의 실패로 뒤 worker를 누수시키지 않는다.
    event_runtime_worker = runtime._trading_event_runtime_worker
    if event_runtime_worker is not None:
        _attempt_shutdown_cleanup(
            event_runtime_worker.close,
            resource_name="trading event worker",
            cleanup_failures=cleanup_failures,
        )
    recovery_worker = runtime._account_stream_recovery_worker
    if recovery_worker is not None:
        _attempt_shutdown_cleanup(
            recovery_worker.close,
            resource_name="account stream recovery worker",
            cleanup_failures=cleanup_failures,
        )
    market_recovery_worker = runtime._market_stream_recovery_worker
    if market_recovery_worker is not None:
        _attempt_shutdown_cleanup(
            market_recovery_worker.close,
            resource_name="market stream recovery worker",
            cleanup_failures=cleanup_failures,
        )

    # Market callback이 application lock을 재사용하므로 Kline handle도 lock 밖에서 먼저 종료를 시도한다.
    _attempt_shutdown_cleanup(
        runtime.market_data_controller.close_market_stream,
        resource_name="market stream",
        cleanup_failures=cleanup_failures,
    )

    with runtime.application_lock:
        # 이미 CLOSED이면 상태를 재게시하지 않고 이번 호출의 worker·market 실패만 전파한다.
        current_state = runtime.state
        if current_state.status is ApplicationStatus.CLOSED:
            closed_state = current_state
        else:
            # 거래 상태를 강제 종료하지 않고 callback·timer·session 구독 차단을 독립 시도한다.
            _attempt_shutdown_cleanup(
                runtime.trading_controller.close_session_resources,
                resource_name="trading session resources",
                cleanup_failures=cleanup_failures,
            )

            # Session 정리 실패와 무관하게 마지막 authenticated account handle도 닫는다.
            account_subscription = (
                runtime.trading_controller.account_subscription
            )
            if account_subscription is not None:
                _attempt_shutdown_cleanup(
                    account_subscription.close,
                    resource_name="account subscription",
                    cleanup_failures=cleanup_failures,
                )

            # 자원 정리 실패가 있어도 terminal gate를 열지 않도록 CLOSED를 마지막에 게시한다.
            try:
                closed_state = runtime._publish_state(
                    status=ApplicationStatus.CLOSED,
                    failure=None,
                    startup_trace=current_state.startup_trace,
                )
            except BaseException as error:
                cleanup_failures.append(
                    ("application CLOSED publication", error)
                )  # Publication 실패도 최초 정리 오류 뒤에 순서대로 집계한다.

    if cleanup_failures:
        # 기존 단일 예외 계약을 유지하고 후속 오류는 credential 없는 note로 집계한다.
        _, first_error = cleanup_failures[0]
        for resource_name, later_error in cleanup_failures[1:]:
            first_error.add_note(
                "Additional shutdown cleanup failure: "
                f"{resource_name} ({type(later_error).__name__})."
            )
        raise first_error  # 최초 예외 identity와 원래 traceback을 호출자에게 그대로 보존한다.

    return closed_state


def _read_shutdown_safety_receipt(
    runtime: ApplicationRuntime,
) -> ShutdownSafetyReceipt:
    """
    함수 이름: _read_shutdown_safety_receipt()
    기능: Position, memory/durable pending 주문과 reconciliation을 한 종료 판정으로 모은다.
    인자: runtime -> application lock을 이미 소유한 runtime
    반환값: 현재 Context version의 accepted 또는 blocked safety receipt
    작성 날짜: 2026/08/24
    """
    trading_controller = runtime.trading_controller
    context_snapshot = trading_controller.context

    # Context 이전 startup Position과 session Context를 함께 읽어 어느 쪽의 open 수량도 놓치지 않는다.
    position = trading_controller.position
    position_open = context_snapshot.position.is_open or (
        position is not None and position.quantity > 0
    )
    pending_order = (
        context_snapshot.pending_order is not None
        or trading_controller.pending_order_query_count > 0
    )
    reconciliation_required = trading_controller.reconciliation_required
    if trading_controller.status is TradingSessionStatus.STOPPING:
        reconciliation_required = True  # 미완료 강제 청산 outcome도 안전한 terminal 상태가 아니다.

    # Durable journal은 memory snapshot이 비어도 재시작 시 복원될 수 있으므로 별도로 검사한다.
    history_controller = runtime.trade_history_controller
    if history_controller.supports_pending_order_recovery:
        try:
            durable_pending_orders = (
                history_controller.get_pending_order_recovery_records()
            )
        except Exception:
            reconciliation_required = True  # 읽을 수 없는 journal을 비어 있다고 추측하지 않는다.
        else:
            pending_order = pending_order or bool(durable_pending_orders)

    blocked = position_open or pending_order or reconciliation_required
    return ShutdownSafetyReceipt(
        accepted=not blocked,
        status=(
            ShutdownReceiptStatus.BLOCKED
            if blocked
            else ShutdownReceiptStatus.ACCEPTED
        ),
        version=context_snapshot.version,
        position_open=position_open,
        pending_order=pending_order,
        reconciliation_required=reconciliation_required,
    )


def _require_shutdown_expected_version(
    runtime: ApplicationRuntime,
    expected_version: int,
) -> None:
    """
    함수 이름: _require_shutdown_expected_version()
    기능: shutdown의 optimistic version이 최신 TradingContext와 정확히 같은지 검사한다.
    인자: runtime -> 최신 Context를 소유한 runtime
        expected_version -> renderer가 관측한 non-negative Context version
    반환값: version이 일치하면 없음
    작성 날짜: 2026/08/24
    """
    if type(expected_version) is not int:
        raise TypeError("expected_version must be an int")
    if expected_version < 0:
        raise ValueError("expected_version must not be negative")

    # Exposure 판정보다 stale command를 먼저 거부해 과거 화면의 종료 의도를 실행하지 않는다.
    current_version = runtime.trading_controller.context.version
    if expected_version != current_version:
        raise TradingSessionError(
            TradingSessionFailureCode.STALE_CONTEXT_VERSION,
            "Trading context version is stale",
            current_version=current_version,
            expected_version=expected_version,
        )


def _complete_shutdown_flight(
    runtime: ApplicationRuntime,
    *,
    result: ShutdownSafetyReceipt | None = None,
    error: BaseException | None = None,
) -> None:
    """
    함수 이름: _complete_shutdown_flight()
    기능: 단일 shutdown owner의 성공 또는 실패 하나를 waiter에게 원자적으로 게시한다.
    인자: runtime -> shutdown single-flight store를 소유한 runtime
        result -> 성공한 accepted receipt 또는 실패면 None
        error -> owner가 받은 실패 또는 성공이면 None
    반환값: terminal 결과 publication 뒤 없음
    작성 날짜: 2026/08/24
    """
    if (result is None) == (error is None):
        raise ValueError("shutdown flight requires exactly one result or error")

    with runtime.application_lock:
        shutdown_store = runtime._shutdown_store
        if not shutdown_store.in_progress:
            raise RuntimeError("shutdown flight is not in progress")

        # Result metadata를 먼저 게시하고 Event를 마지막에 set해 waiter가 반쪽 상태를 읽지 않게 한다.
        shutdown_store.result = result
        shutdown_store.error = error
        shutdown_store.in_progress = False
        shutdown_store.completion_event.set()


def _await_shutdown_flight(runtime: ApplicationRuntime) -> ShutdownSafetyReceipt:
    """
    함수 이름: _await_shutdown_flight()
    기능: 기존 shutdown owner의 terminal 결과를 application lock 밖에서 기다려 재사용한다.
    인자: runtime -> 진행 중 single-flight store를 소유한 runtime
    반환값: owner가 게시한 accepted ShutdownSafetyReceipt
    작성 날짜: 2026/08/24
    """
    completion_event = runtime._shutdown_store.completion_event
    completion_event.wait()  # Owner는 BaseException 경로도 finally publication해 waiter를 깨운다.

    with runtime.application_lock:
        shutdown_result = runtime._shutdown_store.result
        shutdown_error = runtime._shutdown_store.error
    if shutdown_error is not None:
        raise shutdown_error
    if not isinstance(shutdown_result, ShutdownSafetyReceipt):
        raise RuntimeError("shutdown flight completed without a typed result")

    return shutdown_result


def request_application_shutdown(
    runtime: ApplicationRuntime,
    *,
    command_id: str,
    expected_version: int,
) -> ShutdownSafetyReceipt:
    """
    함수 이름: request_application_shutdown()
    기능: exposure를 fail closed로 검사하고 command 차단, 거래 중지, fsync와 자원 종료를 조정한다.
    인자: runtime -> 종료할 application runtime
        command_id -> transport idempotency key에서 얻은 안정적인 command ID
        expected_version -> renderer가 관측한 TradingContext version
    반환값: CLOSED publication 뒤의 accepted safety receipt
    작성 날짜: 2026/08/24
    """
    if not isinstance(runtime, ApplicationRuntime):
        raise TypeError("runtime must be an ApplicationRuntime")
    if (
        not isinstance(command_id, str)
        or not command_id
        or command_id != command_id.strip()
    ):
        raise ValueError("command_id must be a non-empty trimmed string")

    joins_existing_flight = False
    with runtime.application_lock:
        shutdown_store = runtime._shutdown_store
        if shutdown_store.in_progress:
            if expected_version != shutdown_store.expected_version:
                _require_shutdown_expected_version(runtime, expected_version)
            joins_existing_flight = True
        else:
            # 같은 Context version에서만 현재 exposure와 lifecycle을 판단한다.
            _require_shutdown_expected_version(runtime, expected_version)
            current_state = runtime.state
            if current_state.status is ApplicationStatus.CLOSED:
                return ShutdownSafetyReceipt(
                    accepted=True,
                    status=ShutdownReceiptStatus.ACCEPTED,
                    version=runtime.trading_controller.context.version,
                    position_open=False,
                    pending_order=False,
                    reconciliation_required=False,
                )  # 다른 command ID의 안전한 중복 종료도 외부 effect 없는 accepted 결과다.
            if current_state.status not in (
                ApplicationStatus.READY,
                ApplicationStatus.SHUTTING_DOWN,
            ):
                raise RuntimeError("application is not ready for safe shutdown")

            safety_receipt = _read_shutdown_safety_receipt(runtime)
            if not safety_receipt.accepted:
                raise ShutdownBlockedError(safety_receipt)

            # Single-flight owner를 READY publication보다 먼저 고정해 tail operation 중복을 막는다.
            shutdown_store.in_progress = True
            shutdown_store.expected_version = expected_version
            shutdown_store.result = None
            shutdown_store.error = None
            shutdown_store.completion_event.clear()
            try:
                if current_state.status is ApplicationStatus.READY:
                    runtime._publish_state(
                        status=ApplicationStatus.SHUTTING_DOWN,
                        failure=None,
                        startup_trace=current_state.startup_trace,
                    )

                # 열린 exposure가 없는 RUNNING session만 정상 terminal 전이시킨다.
                trading_controller = runtime.trading_controller
                if trading_controller.status is TradingSessionStatus.RUNNING:
                    stop_result = trading_controller.stop_trading(
                        command_id=f"shutdown:{command_id}",
                        expected_version=expected_version,
                    )
                    if stop_result.status is not TradingSessionStatus.TERMINATED:
                        raise ShutdownBlockedError(
                            _read_shutdown_safety_receipt(runtime)
                        )
            except BaseException as error:
                _complete_shutdown_flight(runtime, error=error)
                raise

    if joins_existing_flight:
        return _await_shutdown_flight(runtime)  # 다른 key도 owner tail과 fsync를 반복하지 않는다.

    try:
        # Worker join은 application lock 밖에서 수행해 진행 중 event/recovery cycle과 교착하지 않는다.
        event_runtime_worker = runtime._trading_event_runtime_worker
        if event_runtime_worker is not None:
            event_runtime_worker.close()
        recovery_worker = runtime._account_stream_recovery_worker
        if recovery_worker is not None:
            recovery_worker.close()
        market_recovery_worker = runtime._market_stream_recovery_worker
        if market_recovery_worker is not None:
            market_recovery_worker.close()

        # 두 recovery worker를 모두 join한 뒤 callback mutation과 새 exposure를 마지막으로 재검사한다.
        with runtime.application_lock:
            final_safety_receipt = _read_shutdown_safety_receipt(runtime)
            if not final_safety_receipt.accepted:
                raise ShutdownBlockedError(final_safety_receipt)

            runtime.trade_history_controller.flush_durable_state()
            close_application(runtime)
            accepted_receipt = ShutdownSafetyReceipt(
                accepted=True,
                status=ShutdownReceiptStatus.ACCEPTED,
                version=runtime.trading_controller.context.version,
                position_open=False,
                pending_order=False,
                reconciliation_required=False,
            )
    except BaseException as error:
        _complete_shutdown_flight(runtime, error=error)
        raise

    _complete_shutdown_flight(runtime, result=accepted_receipt)
    return accepted_receipt
