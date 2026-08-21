"""기존 backend 객체를 한 application runtime으로 조립하는 bootstrap 경계를 정의한다."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import os
from threading import RLock
from uuid import uuid4

from binance_auto_trader.adapters.binance import (
    APIGateway,
    BinanceRESTClient,
    BinanceWebSocketClient,
    WebSocketGateway,
)
from binance_auto_trader.adapters.persistence import TradeHistoryRepository
from binance_auto_trader.application import (
    MarketDataController,
    RegimeController,
    TradeHistoryController,
    TradingController,
)
from binance_auto_trader.application.market_data_controller import (
    DEFAULT_KLINE_LIMIT,
)
from binance_auto_trader.application.trade_history_controller import (
    TradeHistoryRepositoryPort,
)
from binance_auto_trader.domain.history import Performance, TradeHistory
from binance_auto_trader.domain.market import MarketSnapshot
from binance_auto_trader.domain.regime import RegimeSTM
from binance_auto_trader.domain.trading import Account, AccountSnapshot


class ExecutionMode(str, Enum):
    """
    클래스 이름: ExecutionMode
    기능: bootstrap 외부 효과를 제한하는 네 가지 실행 모드를 정의한다.
    작성 날짜: 2026/08/21
    """

    DISABLED = "disabled"
    FAKE = "fake"
    TESTNET = "testnet"
    LIVE = "live"


class ApplicationStatus(str, Enum):
    """
    클래스 이름: ApplicationStatus
    기능: application startup과 자원 종료의 공개 lifecycle 상태를 정의한다.
    작성 날짜: 2026/08/21
    """

    CREATED = "CREATED"
    STARTING = "STARTING"
    READY = "READY"
    FAILED = "FAILED"
    CLOSED = "CLOSED"


class StartupStage(str, Enum):
    """
    클래스 이름: StartupStage
    기능: startup 실패가 발생한 조립 단계를 정규화한다.
    작성 날짜: 2026/08/21
    """

    APPLICATION = "APPLICATION"
    MARKET = "MARKET"
    REGIME = "REGIME"
    ACCOUNT = "ACCOUNT"
    HISTORY = "HISTORY"


class StartupFailureCode(str, Enum):
    """
    클래스 이름: StartupFailureCode
    기능: startup 실패와 준비 상태 위반을 transport-safe code로 정의한다.
    작성 날짜: 2026/08/21
    """

    APPLICATION_ALREADY_STARTING = "APPLICATION_ALREADY_STARTING"
    APPLICATION_CLOSED = "APPLICATION_CLOSED"
    MARKET_INITIALIZATION_FAILED = "MARKET_INITIALIZATION_FAILED"
    MARKET_NOT_READY = "MARKET_NOT_READY"
    REGIME_NOT_READY = "REGIME_NOT_READY"
    ACCOUNT_INITIALIZATION_FAILED = "ACCOUNT_INITIALIZATION_FAILED"
    ACCOUNT_NOT_READY = "ACCOUNT_NOT_READY"
    HISTORY_INITIALIZATION_FAILED = "HISTORY_INITIALIZATION_FAILED"


class StartupTraceResult(str, Enum):
    """
    클래스 이름: StartupTraceResult
    기능: 구조화 startup trace의 성공과 실패 결과를 정규화한다.
    작성 날짜: 2026/08/21
    """

    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


@dataclass(frozen=True, slots=True)
class StartupFailure:
    """
    클래스 이름: StartupFailure
    기능: secret과 raw payload 없이 startup 단계의 typed failure를 보존한다.
    작성 날짜: 2026/08/21
    """

    stage: StartupStage
    code: StartupFailureCode
    message: str
    retryable: bool = False

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: startup failure의 enum, 안전한 설명과 retry flag 형식을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # enum, 공개 문구와 retry flag를 construction 시점에 함께 검증한다.
        if not isinstance(self.stage, StartupStage):
            raise TypeError("stage must be a StartupStage")
        if not isinstance(self.code, StartupFailureCode):
            raise TypeError("code must be a StartupFailureCode")
        if not isinstance(self.message, str):
            raise TypeError("message must be a string")
        if not self.message.strip():
            raise ValueError("message must not be empty")
        if not isinstance(self.retryable, bool):
            raise TypeError("retryable must be a bool")


class ApplicationStartupError(RuntimeError):
    """
    클래스 이름: ApplicationStartupError
    기능: startup 호출자에게 안전한 typed failure를 예외로 전달한다.
    작성 날짜: 2026/08/21
    """

    def __init__(self, failure: StartupFailure) -> None:
        """
        함수 이름: __init__()
        기능: 검증된 StartupFailure를 code와 stage 접근이 가능한 예외로 보존한다.
        인자: failure -> 실패 단계와 code를 담은 안전한 값
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 검증되지 않은 예외가 typed startup failure로 노출되지 않게 한다.
        if not isinstance(failure, StartupFailure):
            raise TypeError("failure must be a StartupFailure")

        # 표준 예외 문구와 transport가 읽을 metadata를 같은 객체에 보존한다.
        super().__init__(failure.message)
        self.failure = failure
        self.stage = failure.stage
        self.code = failure.code


@dataclass(frozen=True, slots=True)
class StartupTraceEntry:
    """
    클래스 이름: StartupTraceEntry
    기능: Communication 메시지의 호출자, 결과, failure와 state version을 불변 기록한다.
    작성 날짜: 2026/08/21
    """

    message_id: str
    caller: str
    receiver: str
    command_event_id: str
    state_version_before: int
    state_version_after: int
    related_id: str
    result: StartupTraceResult
    typed_failure_code: StartupFailureCode | None

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: startup trace의 식별자, version과 성공·실패 조합을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # Communication trace 식별자는 모두 비어 있지 않은 문자열이어야 한다.
        text_fields = (
            self.message_id,
            self.caller,
            self.receiver,
            self.command_event_id,
            self.related_id,
        )
        if any(not isinstance(value, str) for value in text_fields):
            raise TypeError("startup trace text fields must be strings")
        if any(not value.strip() for value in text_fields):
            raise ValueError("startup trace text fields must not be empty")
        if self.caller == self.receiver:
            raise ValueError("startup trace caller and receiver must differ")

        # 두 version은 bool이 아닌 0 이상 정수로 제한한다.
        for version in (
            self.state_version_before,
            self.state_version_after,
        ):
            if isinstance(version, bool) or not isinstance(version, int):
                raise TypeError("startup trace versions must be integers")
            if version < 0:
                raise ValueError("startup trace versions must not be negative")

        # 결과 enum과 failure code의 성공·실패 조합을 함께 제한한다.
        if not isinstance(self.result, StartupTraceResult):
            raise TypeError("result must be a StartupTraceResult")
        if self.typed_failure_code is not None and not isinstance(
            self.typed_failure_code,
            StartupFailureCode,
        ):
            raise TypeError(
                "typed_failure_code must be a StartupFailureCode or None"
            )

        succeeded = self.result is StartupTraceResult.SUCCESS
        if succeeded and self.typed_failure_code is not None:
            raise ValueError("successful trace cannot contain a failure code")
        if not succeeded and self.typed_failure_code is None:
            raise ValueError("failed trace requires a failure code")

    @property
    def failure_code(self) -> StartupFailureCode | None:
        """
        함수 이름: failure_code()
        기능: transport가 읽기 쉬운 이름으로 typed failure code를 반환한다.
        인자: 없음
        반환값: 실패 code 또는 성공 trace이면 None
        작성 날짜: 2026/08/21
        """
        return self.typed_failure_code

    @property
    def version_before(self) -> int:
        """
        함수 이름: version_before()
        기능: 호출 전 authoritative state version의 짧은 별칭을 반환한다.
        인자: 없음
        반환값: 호출 전 state version
        작성 날짜: 2026/08/21
        """
        return self.state_version_before

    @property
    def version_after(self) -> int:
        """
        함수 이름: version_after()
        기능: 호출 후 authoritative state version의 짧은 별칭을 반환한다.
        인자: 없음
        반환값: 호출 후 state version
        작성 날짜: 2026/08/21
        """
        return self.state_version_after


@dataclass(frozen=True, slots=True)
class ApplicationStateSnapshot:
    """
    클래스 이름: ApplicationStateSnapshot
    기능: lifecycle 상태, publication version, failure와 trace를 원자적으로 묶는다.
    작성 날짜: 2026/08/21
    """

    status: ApplicationStatus
    version: int
    failure: StartupFailure | None
    startup_trace: tuple[StartupTraceEntry, ...]

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: lifecycle 상태와 failure 조합 및 publication version을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # lifecycle enum, 단조 version과 immutable trace collection 형식을 검증한다.
        if not isinstance(self.status, ApplicationStatus):
            raise TypeError("status must be an ApplicationStatus")
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise TypeError("version must be an integer")
        if self.version < 0:
            raise ValueError("version must not be negative")
        if self.failure is not None and not isinstance(
            self.failure,
            StartupFailure,
        ):
            raise TypeError("failure must be a StartupFailure or None")
        if not isinstance(self.startup_trace, tuple):
            raise TypeError("startup_trace must be a tuple")
        if any(
            not isinstance(entry, StartupTraceEntry)
            for entry in self.startup_trace
        ):
            raise TypeError("startup_trace must contain StartupTraceEntry")

        # typed failure는 FAILED publication에만 존재하도록 상태 조합을 제한한다.
        failed = self.status is ApplicationStatus.FAILED
        if failed and self.failure is None:
            raise ValueError("FAILED state requires a startup failure")
        if not failed and self.failure is not None:
            raise ValueError("only FAILED state may contain a failure")

    @property
    def ready(self) -> bool:
        """
        함수 이름: ready()
        기능: 모든 startup 단계가 성공해 snapshot 공개가 가능한지 반환한다.
        인자: 없음
        반환값: READY 상태이면 True
        작성 날짜: 2026/08/21
        """
        return self.status is ApplicationStatus.READY

    @property
    def closed(self) -> bool:
        """
        함수 이름: closed()
        기능: application 소유 자원이 종료된 상태인지 반환한다.
        인자: 없음
        반환값: CLOSED 상태이면 True
        작성 날짜: 2026/08/21
        """
        return self.status is ApplicationStatus.CLOSED


@dataclass(slots=True)
class _ApplicationStateStore:
    """
    클래스 이름: _ApplicationStateStore
    기능: frozen runtime 내부에서 최신 불변 application state identity를 보존한다.
    작성 날짜: 2026/08/21
    """

    state: ApplicationStateSnapshot


@dataclass(frozen=True, slots=True)
class ApplicationRuntime:
    """
    클래스 이름: ApplicationRuntime
    기능: application lifetime의 entity, controller, gateway, lock과 상태를 조립해 공개한다.
    작성 날짜: 2026/08/21
    """

    execution_mode: ExecutionMode
    application_lock: RLock
    startup_command_id: str
    api_gateway: APIGateway
    web_socket_gateway: WebSocketGateway
    market_snapshot: MarketSnapshot
    account: Account
    regime_stm: RegimeSTM
    regime_controller: RegimeController
    market_data_controller: MarketDataController
    trading_controller: TradingController
    trade_history_repository: TradeHistoryRepositoryPort
    trade_history_controller: TradeHistoryController
    _state_store: _ApplicationStateStore = field(
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: runtime의 mode, command ID, lock과 조립 identity의 핵심 불변식을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 외부 설정과 application lifetime identity의 기본 형식을 먼저 검증한다.
        if not isinstance(self.execution_mode, ExecutionMode):
            raise TypeError("execution_mode must be an ExecutionMode")
        if not isinstance(self.startup_command_id, str):
            raise TypeError("startup_command_id must be a string")
        if not self.startup_command_id.strip():
            raise ValueError("startup_command_id must not be empty")
        if not hasattr(self.application_lock, "__enter__"):
            raise TypeError("application_lock must be a context manager")
        # Controller와 runtime은 authoritative Account 및 state store identity를 공유해야 한다.
        if self.trading_controller.account is not self.account:
            raise ValueError("TradingController must share the runtime Account")
        if not isinstance(self._state_store, _ApplicationStateStore):
            raise TypeError("_state_store must be an application state store")

    @property
    def lock(self) -> RLock:
        """
        함수 이름: lock()
        기능: callback, startup과 snapshot publication이 공유하는 단일 RLock을 반환한다.
        인자: 없음
        반환값: application lifetime의 동일한 RLock
        작성 날짜: 2026/08/21
        """
        return self.application_lock

    @property
    def state(self) -> ApplicationStateSnapshot:
        """
        함수 이름: state()
        기능: application RLock 아래에서 최신 불변 lifecycle state를 반환한다.
        인자: 없음
        반환값: 최신 ApplicationStateSnapshot
        작성 날짜: 2026/08/21
        """
        with self.application_lock:
            return self._state_store.state

    @property
    def ready(self) -> bool:
        """
        함수 이름: ready()
        기능: transport가 일관된 startup snapshot을 공개할 수 있는지 반환한다.
        인자: 없음
        반환값: application READY 여부
        작성 날짜: 2026/08/21
        """
        return self.state.ready

    @property
    def failure(self) -> StartupFailure | None:
        """
        함수 이름: failure()
        기능: 마지막 startup typed failure 또는 실패가 없으면 None을 반환한다.
        인자: 없음
        반환값: StartupFailure 또는 None
        작성 날짜: 2026/08/21
        """
        return self.state.failure

    @property
    def startup_trace(self) -> tuple[StartupTraceEntry, ...]:
        """
        함수 이름: startup_trace()
        기능: Communication 메시지 순서대로 기록한 불변 startup trace를 반환한다.
        인자: 없음
        반환값: StartupTraceEntry tuple
        작성 날짜: 2026/08/21
        """
        return self.state.startup_trace

    @property
    def trade_history(self) -> TradeHistory:
        """
        함수 이름: trade_history()
        기능: 마지막 성공 history load에서 publish된 TradeHistory를 반환한다.
        인자: 없음
        반환값: 현재 TradeHistory
        작성 날짜: 2026/08/21
        """
        return self.trade_history_controller.trade_history

    @property
    def performance(self) -> Performance:
        """
        함수 이름: performance()
        기능: 마지막 성공 history load에서 함께 publish된 Performance를 반환한다.
        인자: 없음
        반환값: 현재 Performance
        작성 날짜: 2026/08/21
        """
        return self.trade_history_controller.performance

    def _publish_state(
        self,
        status: ApplicationStatus,
        failure: StartupFailure | None,
        startup_trace: tuple[StartupTraceEntry, ...],
    ) -> ApplicationStateSnapshot:
        """
        함수 이름: _publish_state()
        기능: 호출자가 application RLock을 보유한 상태에서 다음 lifecycle state를 publish한다.
        인자: status -> 새 lifecycle 상태
            failure -> FAILED 상태의 typed failure 또는 None
            startup_trace -> 새 state에 보존할 완료 trace
        반환값: publish된 ApplicationStateSnapshot
        작성 날짜: 2026/08/21
        """
        # 현재 publication에서 version을 한 번 올린 불변 snapshot을 만든다.
        current_state = self._state_store.state
        next_state = ApplicationStateSnapshot(
            status=status,
            version=current_state.version + 1,
            failure=failure,
            startup_trace=startup_trace,
        )
        self._state_store.state = next_state  # RLock 아래에서 identity를 교체한다.

        return next_state

    def _append_startup_trace(
        self,
        trace_entry: StartupTraceEntry,
    ) -> ApplicationStateSnapshot:
        """
        함수 이름: _append_startup_trace()
        기능: 호출자가 application RLock을 보유한 상태에서 완료 trace를 원자 추가한다.
        인자: trace_entry -> 새로 완료된 Communication trace 항목
        반환값: trace가 추가된 ApplicationStateSnapshot
        작성 날짜: 2026/08/21
        """
        # 검증된 trace만 현재 lifecycle 값을 보존한 새 publication에 추가한다.
        if not isinstance(trace_entry, StartupTraceEntry):
            raise TypeError("trace_entry must be a StartupTraceEntry")

        current_state = self._state_store.state
        return self._publish_state(
            status=current_state.status,
            failure=current_state.failure,
            startup_trace=(*current_state.startup_trace, trace_entry),
        )


def parse_execution_mode(value: object = None) -> ExecutionMode:
    """
    함수 이름: parse_execution_mode()
    기능: 설정 누락, unknown 값과 비문자 값을 disabled로 fail closed한다.
    인자: value -> 외부 설정에서 읽은 실행 모드 값
    반환값: 네 canonical ExecutionMode 중 하나
    작성 날짜: 2026/08/21
    """
    # exact 문자열 외 입력은 더 강한 실행 권한으로 해석하지 않는다.
    if not isinstance(value, str):
        return ExecutionMode.DISABLED

    try:
        return ExecutionMode(value)
    except ValueError:
        return ExecutionMode.DISABLED  # 다른 실행 모드로 fallback하지 않는다.


def create_application_runtime(
    rest_client: BinanceRESTClient,
    web_socket_client: BinanceWebSocketClient,
    *,
    history_path: str | os.PathLike[str] | None = None,
    history_repository: TradeHistoryRepositoryPort | None = None,
    execution_mode: object = None,
    account_update_observer: Callable[[Account], object] | None = None,
    clock: Callable[[], datetime] | None = None,
    kline_limit: int = DEFAULT_KLINE_LIMIT,
) -> ApplicationRuntime:
    """
    함수 이름: create_application_runtime()
    기능: 주입 client와 history port로 기존 entity, gateway와 controller를 순환 없이 조립한다.
    인자: rest_client -> Kline과 account payload를 제공할 REST client
        web_socket_client -> Kline과 account 구독을 제공할 WebSocket client
        history_path -> local JSONL storage 경로 또는 None
        history_repository -> 주입할 TradeHistory repository port 또는 None
        execution_mode -> fail-closed parser에 전달할 외부 mode 값
        account_update_observer -> 실제 Account 변경 뒤 호출할 optional observer
        clock -> market, gateway, repository와 performance가 공유할 optional UTC clock
        kline_limit -> 각 market interval에서 조회할 Kline 개수
    반환값: 동일 객체 identity와 단일 RLock을 보존하는 ApplicationRuntime
    작성 날짜: 2026/08/21
    """
    # 외부 의존성과 선택 port 조합을 객체 생성 전에 fail fast로 검증한다.
    has_history_path = history_path is not None
    has_history_repository = history_repository is not None
    if has_history_path == has_history_repository:
        raise ValueError(
            "exactly one of history_path or history_repository is required"
        )
    if account_update_observer is not None and not callable(
        account_update_observer
    ):
        raise TypeError("account_update_observer must be callable")
    if clock is not None and not callable(clock):
        raise TypeError("clock must be callable")

    # Application lock과 entity를 만들며 실행 mode도 gate 조립 전에 canonicalize한다.
    application_lock = RLock()
    selected_execution_mode = parse_execution_mode(execution_mode)
    market_snapshot = MarketSnapshot(clock=clock)
    account = Account()
    regime_stm = RegimeSTM()

    # Gateway는 외부 client를 캡슐화하고 Account callback만 application lock에 연결한다.
    api_gateway = APIGateway(rest_client, clock=clock)

    def apply_account_stream_snapshot(snapshot: AccountSnapshot) -> bool:
        """
        함수 이름: apply_account_stream_snapshot()
        기능: stream patch를 단일 application RLock 아래 적용하고 실제 변경만 알린다.
        인자: snapshot -> WebSocketGateway가 정규화한 부분 AccountSnapshot
        반환값: Account state가 실제 변경됐으면 True
        작성 날짜: 2026/08/21
        """
        # 부분 snapshot 적용과 observer 알림을 같은 application publication으로 묶는다.
        with application_lock:
            account_changed = account.apply_stream_snapshot(snapshot)
            if account_changed and account_update_observer is not None:
                account_update_observer(account)  # 변경된 동일 Account만 알린다.

            return account_changed

    web_socket_gateway = WebSocketGateway(
        web_socket_client,
        account_snapshot_callback=apply_account_stream_snapshot,
    )

    # 거래 Controller를 먼저 만들어 Regime 선택의 유일한 commit port로 연결한다.
    trading_controller = TradingController(
        api_gateway,
        web_socket_gateway,
        account,
        market_snapshot,
        command_gate=selected_execution_mode is ExecutionMode.FAKE,
        clock=clock,
        application_lock=application_lock,
    )
    regime_controller = RegimeController(
        regime_stm,
        market_snapshot,
        trading_controller,
        application_lock=application_lock,
    )
    market_data_controller = MarketDataController(
        api_gateway,
        web_socket_gateway,
        market_snapshot,
        regime_controller,
        kline_limit=kline_limit,
    )

    # Persistence 구현 또는 주입 port 중 정확히 하나로 history startup 경계를 조립한다.
    selected_history_repository: TradeHistoryRepositoryPort
    if history_repository is not None:
        selected_history_repository = history_repository
    else:
        selected_history_repository = TradeHistoryRepository(
            history_path,
            clock=clock,
        )
    trade_history_controller = TradeHistoryController(
        selected_history_repository,
        clock=clock,
    )

    # 초기 state는 모든 외부 초기화가 끝나기 전까지 명시적으로 not-ready다.
    initial_state = ApplicationStateSnapshot(
        status=ApplicationStatus.CREATED,
        version=0,
        failure=None,
        startup_trace=(),
    )
    startup_command_id = f"startup-{uuid4().hex}"

    return ApplicationRuntime(
        execution_mode=selected_execution_mode,
        application_lock=application_lock,
        startup_command_id=startup_command_id,
        api_gateway=api_gateway,
        web_socket_gateway=web_socket_gateway,
        market_snapshot=market_snapshot,
        account=account,
        regime_stm=regime_stm,
        regime_controller=regime_controller,
        market_data_controller=market_data_controller,
        trading_controller=trading_controller,
        trade_history_repository=selected_history_repository,
        trade_history_controller=trade_history_controller,
        _state_store=_ApplicationStateStore(initial_state),
    )
