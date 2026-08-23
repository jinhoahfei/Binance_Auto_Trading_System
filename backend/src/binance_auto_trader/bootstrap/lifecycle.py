"""Application startup 순서와 소유 subscription 종료 lifecycle을 정의한다."""

from __future__ import annotations

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
            return current_state  # 성공한 runtime의 중복 startup은 멱등 no-op이다.
        if current_state.status is ApplicationStatus.STARTING:
            failure = _create_failure(
                StartupStage.APPLICATION,
                StartupFailureCode.APPLICATION_ALREADY_STARTING,
                "Application startup is already in progress.",
            )
            raise ApplicationStartupError(failure)
        if current_state.status is ApplicationStatus.CLOSED:
            failure = _create_failure(
                StartupStage.APPLICATION,
                StartupFailureCode.APPLICATION_CLOSED,
                "Closed application runtime cannot be started.",
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
        return runtime._publish_state(
            status=ApplicationStatus.READY,
            failure=None,
            startup_trace=runtime.startup_trace,
        )


def close_application(runtime: ApplicationRuntime) -> ApplicationStateSnapshot:
    """
    함수 이름: close_application()
    기능: account recovery worker와 subscription을 순서대로 회수하고 lifecycle을 멱등 종료한다.
    인자: runtime -> 종료할 application runtime
    반환값: CLOSED ApplicationStateSnapshot
    작성 날짜: 2026/08/21
    """
    # factory가 조립한 runtime 외 객체는 자원 cleanup 전에 거부한다.
    if not isinstance(runtime, ApplicationRuntime):
        raise TypeError("runtime must be an ApplicationRuntime")

    # Worker stop/join은 application lock 밖에서 수행해 진행 중 Controller 복구와 교착하지 않는다.
    recovery_worker = runtime._account_stream_recovery_worker
    if recovery_worker is not None:
        recovery_worker.close()

    with runtime.application_lock:
        # CLOSED publication은 멱등 반환하고 다른 상태만 소유 자원을 정리한다.
        current_state = runtime.state
        if current_state.status is ApplicationStatus.CLOSED:
            return current_state  # 이미 닫힌 runtime은 자원을 다시 만지지 않는다.

        # 거래 상태를 강제 종료하지 않고 callback·timer·session 구독부터 차단한다.
        runtime.trading_controller.close_session_resources()

        # Kline startup 구독은 MarketDataController가 이미 닫으므로 account만 정리한다.
        account_subscription = runtime.trading_controller.account_subscription
        if account_subscription is not None:
            account_subscription.close()

        return runtime._publish_state(
            status=ApplicationStatus.CLOSED,
            failure=None,
            startup_trace=current_state.startup_trace,
        )
