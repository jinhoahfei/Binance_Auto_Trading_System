"""기존 backend 객체를 한 application runtime으로 조립하는 bootstrap 경계를 정의한다."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
import os
from threading import Event, Lock, RLock, Thread, current_thread
from uuid import uuid4

from binance_auto_trader.adapters.binance import (
    APIGateway,
    BinanceRESTClient,
    BinanceWebSocketClient,
    WebSocketGateway,
)
from binance_auto_trader.adapters.binance.api_gateway import (
    APP_CLIENT_ORDER_ID_PREFIX,
)
from binance_auto_trader.adapters.filesystem import CSVFileGateway
from binance_auto_trader.adapters.persistence import TradeHistoryRepository
from binance_auto_trader.application import (
    AccountStreamRecoveryBlockedError,
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
from binance_auto_trader.domain.history import Performance, Trade, TradeHistory
from binance_auto_trader.domain.market import MarketSnapshot
from binance_auto_trader.domain.regime import RegimeSTM
from binance_auto_trader.domain.trading import (
    Account,
    AccountSnapshot,
    OrderResult,
    Position,
)


# Generic dependency-injection factory가 mode 문자열만으로 외부 주문 권한을 만들지 못하게 한다.
_FAKE_ORDER_CAPABILITY = object()
_TESTNET_ORDER_CAPABILITY = object()


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
    SHUTTING_DOWN = "SHUTTING_DOWN"
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
    RECONCILIATION = "RECONCILIATION"


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
    ORDER_RECONCILIATION_FAILED = "ORDER_RECONCILIATION_FAILED"
    ORDER_RECONCILIATION_NOT_READY = "ORDER_RECONCILIATION_NOT_READY"


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


@dataclass(slots=True)
class _ApplicationShutdownStore:
    """
    클래스 이름: _ApplicationShutdownStore
    기능: 서로 다른 idempotency key의 동시 안전 종료를 한 owner와 terminal 결과로 합친다.
    작성 날짜: 2026/08/24
    """

    in_progress: bool = False
    expected_version: int | None = None
    completion_event: Event = field(
        default_factory=Event,
        repr=False,
        compare=False,
    )
    result: object | None = field(default=None, repr=False, compare=False)
    error: BaseException | None = field(default=None, repr=False, compare=False)


class _AccountStreamRecoveryWorker:
    """
    클래스 이름: _AccountStreamRecoveryWorker
    기능: transient 단절만 제한 재시도하고 결정적 blocker는 잠그는 단일 daemon 복구 worker다.
    작성 날짜: 2026/08/22
    """

    _BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0)

    def __init__(
        self,
        recovery_operation: Callable[[], object],
        recovery_allowed: Callable[[], bool],
        *,
        retry_waiter: Callable[[float], bool] | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 복구 Operation, readiness Guard와 중단 가능한 backoff 대기를 보존한다.
        인자: recovery_operation -> application lock 밖에서 실행할 full reconciliation Operation
            recovery_allowed -> READY와 startup reconciliation 완료 여부를 반환할 Guard
            retry_waiter -> 지연을 기다리고 종료 요청 여부를 반환할 optional 대기 함수
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # 외부 I/O를 시작하기 전에 두 callback 형식을 검증해 background 실패를 방지한다.
        if not callable(recovery_operation):
            raise TypeError("recovery_operation must be callable")
        if not callable(recovery_allowed):
            raise TypeError("recovery_allowed must be callable")
        if retry_waiter is not None and not callable(retry_waiter):
            raise TypeError("retry_waiter must be callable")

        # Worker 상태 lock은 application RLock과 분리해 callback과 종료의 lock 순서를 단순화한다.
        self._recovery_operation = recovery_operation
        self._recovery_allowed = recovery_allowed
        self._state_lock = Lock()
        self._stop_event = Event()
        self._retry_waiter = (
            self._stop_event.wait
            if retry_waiter is None
            else retry_waiter
        )
        self._active_thread: Thread | None = None
        self._rerun_requested = False
        self._deterministic_recovery_blocked = False
        self._closed = False

    def request_recovery(self) -> bool:
        """
        함수 이름: request_recovery()
        기능: 허용된 새 요청을 단일 daemon 실행 또는 실행 중 후속 latch 하나로 보존한다.
        인자: 없음
        반환값: 요청을 수락했으면 True, 중복·종료·결정적 차단 상태이면 False
        작성 날짜: 2026/08/22
        """
        # 실행 중 새 disconnect는 하나의 후속 full reconciliation latch로 병합한다.
        with self._state_lock:
            if self._closed or self._deterministic_recovery_blocked:
                return False
            if self._active_thread is not None:
                if self._rerun_requested:
                    return False

                self._rerun_requested = True
                return True  # 여러 active 요청은 후속 실행 하나까지만 예약한다.

            recovery_thread = Thread(
                target=self._run,
                name="binance-account-stream-recovery",
                daemon=True,
            )
            self._active_thread = recovery_thread
            try:
                recovery_thread.start()
            except Exception:
                self._active_thread = None
                raise

            return True  # Callback에는 REST/WS 실행 대신 thread 시작만 남긴다.

    def close(self) -> None:
        """
        함수 이름: close()
        기능: 새 복구 요청을 영구 차단하고 대기 중 worker를 깨운 뒤 현재 실행을 회수한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # 중단 신호와 현재 thread identity를 같은 worker lock 아래에서 원자적으로 읽는다.
        with self._state_lock:
            self._closed = True
            self._rerun_requested = False
            self._stop_event.set()
            active_thread = self._active_thread

        # REST/WS Operation이나 application RLock을 막지 않도록 worker lock 밖에서 join한다.
        if (
            active_thread is not None
            and active_thread is not current_thread()
        ):
            active_thread.join()

    def _run(self) -> None:
        """
        함수 이름: _run()
        기능: full reconciliation을 실행해 transient 실패만 최대 8초 간격으로 재시도한다.
        인자: 없음
        반환값: 성공, readiness 상실 또는 종료 요청 시 없음
        작성 날짜: 2026/08/22
        """
        retry_index = 0

        try:
            while not self._stop_event.is_set():
                # READY와 startup reconciliation을 잃은 runtime에서는 외부 복구를 시작하지 않는다.
                try:
                    recovery_allowed = self._recovery_allowed()
                except Exception:
                    return  # Readiness Guard 자체 실패도 외부 I/O 허용으로 fallback하지 않는다.
                if recovery_allowed is not True:
                    return

                # 실제 Controller Operation은 worker/application lock을 보유하지 않은 채 호출한다.
                try:
                    self._recovery_operation()
                except AccountStreamRecoveryBlockedError:
                    # 동일 runtime에서 개선될 수 없는 blocker는 latch와 후속 자동 시도를 영구 차단한다.
                    with self._state_lock:
                        self._deterministic_recovery_blocked = True
                        self._rerun_requested = False
                        self._active_thread = None
                    return
                except Exception:
                    retry_delay = self._BACKOFF_SECONDS[retry_index]
                    retry_index = min(
                        retry_index + 1,
                        len(self._BACKOFF_SECONDS) - 1,
                    )
                    if self._retry_waiter(retry_delay):
                        return  # Runtime close가 backoff를 즉시 깨우면 추가 요청을 보내지 않는다.
                    continue

                # 성공과 pending rerun 소비를 원자화해 완료 직전 disconnect를 유실하지 않는다.
                with self._state_lock:
                    if self._closed:
                        self._rerun_requested = False
                        self._active_thread = None
                        return
                    if self._rerun_requested:
                        self._rerun_requested = False
                        retry_index = 0
                        continue

                    self._active_thread = None
                    return
        finally:
            # 예외적 종료에서도 현재 thread identity만 해제하고 close 시 pending latch를 제거한다.
            with self._state_lock:
                if self._active_thread is current_thread():
                    self._active_thread = None
                if self._closed:
                    self._rerun_requested = False


class _TradingEventRuntimeWorker:
    """
    클래스 이름: _TradingEventRuntimeWorker
    기능: Controller의 bounded event cycle을 process당 단일 interruptible thread에서 구동한다.
    작성 날짜: 2026/08/24
    """

    _POLL_INTERVAL_SECONDS = 0.25

    def __init__(
        self,
        runtime_cycle: Callable[[], object],
        fail_closed_operation: Callable[[], object],
        processing_allowed: Callable[[], bool],
        state_snapshot: Callable[[], object],
        application_lock: RLock,
        *,
        state_update_observer: Callable[[], object] | None = None,
        poll_interval_seconds: float = _POLL_INTERVAL_SECONDS,
    ) -> None:
        """
        함수 이름: __init__()
        기능: bounded cycle, lifecycle Guard, fail-close와 상태 publication callback을 보존한다.
        인자: runtime_cycle -> awaitable bounded Controller cycle을 만드는 callable
            fail_closed_operation -> cycle 실패를 typed reconciliation으로 잠그는 Operation
            processing_allowed -> READY lifecycle에서만 work를 허용하는 Guard
            state_snapshot -> cycle 전후 authoritative session snapshot을 만드는 callable
            application_lock -> lifecycle, Controller와 publication이 공유하는 RLock
            state_update_observer -> 상태 변경을 transport에 게시할 optional observer
            poll_interval_seconds -> due 작업을 확인하는 양수 interruptible cadence
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # Background thread를 만들기 전에 모든 callback과 shared lock 계약을 검증한다.
        callbacks = (
            runtime_cycle,
            fail_closed_operation,
            processing_allowed,
            state_snapshot,
        )
        if any(not callable(callback) for callback in callbacks):
            raise TypeError("trading event runtime callbacks must be callable")
        if state_update_observer is not None and not callable(
            state_update_observer
        ):
            raise TypeError("state_update_observer must be callable or None")
        if not hasattr(application_lock, "__enter__"):
            raise TypeError("application_lock must be a context manager")
        if (
            isinstance(poll_interval_seconds, bool)
            or not isinstance(poll_interval_seconds, (int, float))
            or poll_interval_seconds <= 0
        ):
            raise ValueError("poll_interval_seconds must be positive")

        # Worker lock과 wake/stop Event는 application RLock과 분리해 join lock 순서를 고정한다.
        self._runtime_cycle = runtime_cycle
        self._fail_closed_operation = fail_closed_operation
        self._processing_allowed = processing_allowed
        self._state_snapshot = state_snapshot
        self._application_lock = application_lock
        self._state_update_observer = state_update_observer
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._state_lock = Lock()
        self._wake_event = Event()
        self._stop_event = Event()
        self._active_thread: Thread | None = None
        self._closed = False
        self._failed = False

    @property
    def failed(self) -> bool:
        """
        함수 이름: failed()
        기능: runtime cycle이 실패해 worker가 fail closed됐는지 반환한다.
        인자: 없음
        반환값: worker failure 여부
        작성 날짜: 2026/08/24
        """
        with self._state_lock:
            return self._failed  # Raw 예외 대신 credential 없는 상태만 외부 진단에 제공한다.

    def start(self) -> bool:
        """
        함수 이름: start()
        기능: lifecycle READY 진입 뒤 단일 daemon event runtime thread를 멱등 시작한다.
        인자: 없음
        반환값: 새 thread를 시작했으면 True, 이미 실행·종료·실패 상태이면 False
        작성 날짜: 2026/08/24
        """
        # Thread identity를 먼저 고정해 동시 start가 두 event loop를 만들지 못하게 한다.
        with self._state_lock:
            if self._closed or self._failed or self._active_thread is not None:
                return False

            runtime_thread = Thread(
                target=self._run,
                name="binance-trading-event-runtime",
                daemon=True,
            )
            self._active_thread = runtime_thread
            try:
                runtime_thread.start()
            except Exception:
                self._active_thread = None
                raise

            return True

    def request_processing(self) -> bool:
        """
        함수 이름: request_processing()
        기능: queue 또는 scheduled work를 단일 coalescing wake Event로 worker에 알린다.
        인자: 없음
        반환값: wake를 수락했으면 True, 종료·실패 상태이면 False
        작성 날짜: 2026/08/24
        """
        # Event.set은 호출 thread에서 drain이나 REST를 실행하지 않는 non-blocking 경계다.
        with self._state_lock:
            if self._closed or self._failed:
                return False
            if self._active_thread is current_thread():
                return True  # 현재 bounded drain이 만든 내부 queue/schedule은 같은 cycle 또는 cadence가 잇는다.

            self._wake_event.set()
            return True  # 반복 wake는 Event 하나로 병합해 thread 수를 늘리지 않는다.

    def close(self) -> None:
        """
        함수 이름: close()
        기능: 새 wake를 차단하고 interruptible wait를 깨운 뒤 단일 worker thread를 회수한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # Stop과 현재 thread identity를 worker lock 아래에서 원자적으로 확정한다.
        with self._state_lock:
            self._closed = True
            self._stop_event.set()
            self._wake_event.set()
            active_thread = self._active_thread

        # Controller/application RLock을 기다리는 thread와 교착하지 않도록 lock 밖에서 join한다.
        if active_thread is not None and active_thread is not current_thread():
            active_thread.join()

    def _run(self) -> None:
        """
        함수 이름: _run()
        기능: explicit wake와 periodic due 확인을 bounded cycle 하나씩 직렬 실행한다.
        인자: 없음
        반환값: close 또는 최초 실패 뒤 없음
        작성 날짜: 2026/08/24
        """
        try:
            while not self._stop_event.is_set():
                # Event.wait는 close와 enqueue wake에 즉시 반응하며 polling busy loop를 만들지 않는다.
                self._wake_event.wait(self._poll_interval_seconds)
                self._wake_event.clear()
                if self._stop_event.is_set():
                    return

                try:
                    # Lifecycle Guard, Controller cycle과 publication을 같은 application snapshot에 묶는다.
                    with self._application_lock:
                        if self._processing_allowed() is not True:
                            continue

                        state_before = self._state_snapshot()
                        cycle_results = asyncio.run(self._runtime_cycle())
                        state_after = self._state_snapshot()
                        should_publish = (
                            bool(cycle_results) or state_after != state_before
                        )
                        if (
                            should_publish
                            and self._state_update_observer is not None
                        ):
                            self._state_update_observer()
                except BaseException:
                    # 최초 runtime/publication 실패는 raw 오류를 노출하지 않고 같은 lock에서 잠근다.
                    with self._application_lock:
                        if self._processing_allowed() is True:
                            self._fail_closed_operation()
                            if self._state_update_observer is not None:
                                try:
                                    self._state_update_observer()
                                except BaseException:
                                    pass  # 실패한 publication을 재귀 재시도하거나 thread로 늘리지 않는다.
                    with self._state_lock:
                        self._failed = True
                    return
        finally:
            # close와 failure 어느 경로에서도 현재 worker identity만 정확히 해제한다.
            with self._state_lock:
                if self._active_thread is current_thread():
                    self._active_thread = None


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
    _trading_event_runtime_worker: _TradingEventRuntimeWorker | None = field(
        repr=False,
        compare=False,
    )
    _account_stream_recovery_worker: _AccountStreamRecoveryWorker | None = field(
        repr=False,
        compare=False,
    )
    _state_store: _ApplicationStateStore = field(
        repr=False,
        compare=False,
    )
    _shutdown_store: _ApplicationShutdownStore = field(
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
        if self.trade_history_controller.account is not self.account:
            raise ValueError(
                "TradeHistoryController must share the runtime Account"
            )  # 상세 요약과 trading snapshot이 같은 Account owner를 보아야 한다.
        if not isinstance(self._state_store, _ApplicationStateStore):
            raise TypeError("_state_store must be an application state store")
        if not isinstance(self._shutdown_store, _ApplicationShutdownStore):
            raise TypeError("_shutdown_store must be an application shutdown store")
        if (
            self._trading_event_runtime_worker is not None
            and not isinstance(
                self._trading_event_runtime_worker,
                _TradingEventRuntimeWorker,
            )
        ):
            raise TypeError(
                "_trading_event_runtime_worker must be a trading event worker or None"
            )
        if (
            self._account_stream_recovery_worker is not None
            and not isinstance(
                self._account_stream_recovery_worker,
                _AccountStreamRecoveryWorker,
            )
        ):
            raise TypeError(
                "_account_stream_recovery_worker must be a recovery worker or None"
            )

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
    allow_testnet_orders: bool = False,
    testnet_maximum_order_notional: Decimal | None = None,
    _fake_order_capability: object | None = None,
    _testnet_order_capability: object | None = None,
    account_update_observer: Callable[[Account], object] | None = None,
    trade_history_update_observer: Callable[[Trade, Performance], object]
    | None = None,
    trading_session_update_observer: Callable[
        [TradingController, ExecutionMode],
        object,
    ]
    | None = None,
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
        allow_testnet_orders -> testnet mode 주문을 명시적으로 허용하는 별도 opt-in
        testnet_maximum_order_notional -> 주문 허용 testnet에 필수인 BUY decision-notional 진입 상한
        _fake_order_capability -> 검증된 in-process fake 조립기만 전달하는 내부 권한 표식
        _testnet_order_capability -> 고정 endpoint Testnet 조립기만 전달하는 내부 권한 표식
        account_update_observer -> 실제 Account 변경 뒤 호출할 optional observer
        trade_history_update_observer -> durable Trade와 전체 Performance 게시 후 호출할 observer
        trading_session_update_observer -> event cycle 뒤 authoritative session을 게시할 observer
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
    if trade_history_update_observer is not None and not callable(
        trade_history_update_observer
    ):
        raise TypeError(
            "trade_history_update_observer must be callable"
        )  # Durable publication 후에 실행할 호출 경계만 허용한다.
    if trading_session_update_observer is not None and not callable(
        trading_session_update_observer
    ):
        raise TypeError("trading_session_update_observer must be callable")
    if clock is not None and not callable(clock):
        raise TypeError("clock must be callable")
    if type(allow_testnet_orders) is not bool:
        raise TypeError("allow_testnet_orders must be a bool")
    if testnet_maximum_order_notional is not None and (
        not isinstance(testnet_maximum_order_notional, Decimal)
        or not testnet_maximum_order_notional.is_finite()
        or testnet_maximum_order_notional <= Decimal("0")
    ):
        raise ValueError(
            "testnet_maximum_order_notional must be a positive finite Decimal or None"
        )

    # Application lock과 entity를 만들며 실행 mode도 gate 조립 전에 canonicalize한다.
    application_lock = RLock()
    selected_execution_mode = parse_execution_mode(execution_mode)
    requested_fake_order_gate = (
        selected_execution_mode is ExecutionMode.FAKE
    )
    requested_testnet_order_gate = (
        selected_execution_mode is ExecutionMode.TESTNET
        and allow_testnet_orders
    )
    if (
        requested_fake_order_gate
        and _fake_order_capability is not _FAKE_ORDER_CAPABILITY
    ):
        raise ValueError(
            "fake orders require the dedicated in-process fake bootstrap"
        )
    if (
        _fake_order_capability is not None
        and _fake_order_capability is not _FAKE_ORDER_CAPABILITY
    ):
        raise ValueError("invalid fake order capability")
    if (
        requested_testnet_order_gate
        and _testnet_order_capability is not _TESTNET_ORDER_CAPABILITY
    ):
        raise ValueError(
            "testnet orders require the dedicated fixed-endpoint bootstrap"
        )
    if (
        _testnet_order_capability is not None
        and _testnet_order_capability is not _TESTNET_ORDER_CAPABILITY
    ):
        raise ValueError("invalid testnet order capability")
    testnet_order_gate = (
        requested_testnet_order_gate
        and _testnet_order_capability is _TESTNET_ORDER_CAPABILITY
    )
    fake_order_gate = (
        requested_fake_order_gate
        and _fake_order_capability is _FAKE_ORDER_CAPABILITY
    )
    if testnet_order_gate and testnet_maximum_order_notional is None:
        raise ValueError(
            "enabled testnet orders require testnet_maximum_order_notional"
        )
    if not testnet_order_gate and testnet_maximum_order_notional is not None:
        raise ValueError(
            "testnet_maximum_order_notional requires enabled testnet orders"
        )
    market_snapshot = MarketSnapshot(clock=clock)
    account = Account()
    regime_stm = RegimeSTM()
    initial_state = ApplicationStateSnapshot(
        status=ApplicationStatus.CREATED,
        version=0,
        failure=None,
        startup_trace=(),
    )
    application_state_store = _ApplicationStateStore(initial_state)

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
            if application_state_store.state.status in (
                ApplicationStatus.SHUTTING_DOWN,
                ApplicationStatus.CLOSED,
            ):
                return False  # 종료 gate 뒤 대기하던 callback은 Account를 다시 변경하지 못한다.

            account_changed = account.apply_stream_snapshot(snapshot)
            if account_changed and account_update_observer is not None:
                account_update_observer(account)  # 변경된 동일 Account만 알린다.

            return account_changed

    # WebSocket callback은 생성 뒤 할당될 Controller와 recovery worker를 안전하게 참조한다.
    trading_controller: TradingController
    trading_event_runtime_worker: _TradingEventRuntimeWorker | None = None
    account_stream_recovery_worker: _AccountStreamRecoveryWorker | None = None

    def request_trading_event_processing() -> bool:
        """
        함수 이름: request_trading_event_processing()
        기능: Controller queue·schedule 변경을 조립된 단일 runtime worker wake로 전달한다.
        인자: 없음
        반환값: worker가 wake를 수락했으면 True
        작성 날짜: 2026/08/24
        """
        # Controller 생성 중에는 worker가 아직 없으므로 신호만 안전하게 생략한다.
        runtime_worker = trading_event_runtime_worker
        if runtime_worker is None:
            return False

        return runtime_worker.request_processing()

    def apply_order_stream_result(result: OrderResult) -> bool:
        """
        함수 이름: apply_order_stream_result()
        기능: 정규화 주문 결과를 TradingController의 same-order pipeline에 전달한다.
        인자: result -> WebSocketGateway가 만든 OrderResult
        반환값: 현재 주문 state가 결과를 수락했으면 True
        작성 날짜: 2026/08/22
        """
        # Account와 order callback이 동일 entity graph를 동시에 변경하지 않게 직렬화한다.
        with application_lock:
            if application_state_store.state.status in (
                ApplicationStatus.SHUTTING_DOWN,
                ApplicationStatus.CLOSED,
            ):
                return False  # 종료 snapshot 뒤 도착한 executionReport를 새 pending으로 만들지 않는다.

            result_accepted = trading_controller.observe_order_result(result)
            unknown_application_order = (
                not result_accepted
                and result.client_order_id.startswith(
                    APP_CLIENT_ORDER_ID_PREFIX
                )
            )
            if unknown_application_order:
                # 알 수 없는 app 주문은 정상 callback으로 삼키지 않고 full REST 재조정 worker를 깨운다.
                require_stream_reconciliation(
                    "unknown_application_order_result"
                )
            if (
                result_accepted
                and trading_session_update_observer is not None
            ):
                trading_session_update_observer(
                    trading_controller,
                    selected_execution_mode,
                )  # 수락 결과만 여기서 게시하고 unknown app-order는 공통 reconciliation 경로가 게시한다.

            return result_accepted

    def require_stream_reconciliation(reason: str) -> None:
        """
        함수 이름: require_stream_reconciliation()
        기능: account stream 종료 사유를 Controller의 fail-closed reconciliation gate에 전달한다.
        인자: reason -> WebSocketGateway가 만든 credential 없는 종료 사유
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # Callback은 먼저 동일 application lock에서 command gate를 즉시 fail closed한다.
        with application_lock:
            if application_state_store.state.status in (
                ApplicationStatus.SHUTTING_DOWN,
                ApplicationStatus.CLOSED,
            ):
                return  # 종료 owner가 recovery worker를 닫은 뒤 새 rerun latch를 만들지 않는다.

            trading_controller.mark_account_stream_reconciliation_required(
                reason
            )
            if trading_session_update_observer is not None:
                trading_session_update_observer(
                    trading_controller,
                    selected_execution_mode,
                )  # 모든 stream 장애의 authoritative fail-close를 recovery 시작 전에 정확히 한 번 게시한다.
            recovery_can_start = (
                selected_execution_mode is ExecutionMode.TESTNET
                and application_state_store.state.status
                is ApplicationStatus.READY
                and trading_controller.startup_reconciliation_complete
            )

        # Blocking REST/WS는 callback thread에서 실행하지 않고 runtime worker에만 요청한다.
        if (
            recovery_can_start
            and account_stream_recovery_worker is not None
        ):
            account_stream_recovery_worker.request_recovery()

    web_socket_gateway = WebSocketGateway(
        web_socket_client,
        account_snapshot_callback=apply_account_stream_snapshot,
        order_result_callback=apply_order_stream_result,
        reconciliation_required_callback=require_stream_reconciliation,
    )

    # Persistence 구현 또는 주입 port를 먼저 조립해 order outcome 전에 durable owner를 준비한다.
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
        account=account,
        csv_export_writer=CSVFileGateway(),
        trade_update_observer=trade_history_update_observer,
    )  # 상세 조회, 실시간 event와 CSV export가 같은 durable history owner를 본다.
    position = Position()

    # 거래 Controller가 Position과 history를 공유해 Phase 8 outcome commit 순서를 소유한다.
    trading_controller = TradingController(
        api_gateway,
        web_socket_gateway,
        account,
        market_snapshot,
        command_gate=(
            fake_order_gate
            or testnet_order_gate
        ),  # live mode는 allow flag와 무관하게 Phase 13 전까지 항상 잠긴다.
        position=position,
        trade_history_controller=trade_history_controller,
        clock=clock,
        application_lock=application_lock,
        # 실제 전송이 가능한 testnet 경로만 제출 전 복구 저널을 강제한다.
        pending_order_recovery_enabled=(
            selected_execution_mode is ExecutionMode.TESTNET
        ),
        maximum_order_notional=testnet_maximum_order_notional,
        event_runtime_notifier=request_trading_event_processing,
    )

    async def run_trading_event_runtime_cycle() -> object:
        """
        함수 이름: run_trading_event_runtime_cycle()
        기능: worker가 Controller의 단일 bounded runtime-cycle Operation을 await하도록 연결한다.
        인자: 없음
        반환값: 이번 cycle에서 처리한 STM result tuple
        작성 날짜: 2026/08/24
        """
        return await trading_controller.run_event_runtime_cycle()

    def trading_event_processing_allowed() -> bool:
        """
        함수 이름: trading_event_processing_allowed()
        기능: worker cycle과 publication을 READY lifecycle에서만 허용한다.
        인자: 없음
        반환값: application이 READY이면 True
        작성 날짜: 2026/08/24
        """
        return (
            application_state_store.state.status
            is ApplicationStatus.READY
        )  # Caller가 application RLock을 보유하므로 한 lifecycle snapshot만 읽는다.

    def publish_trading_session_update() -> object | None:
        """
        함수 이름: publish_trading_session_update()
        기능: transport observer가 있으면 Controller와 execution mode의 authoritative 상태를 게시한다.
        인자: 없음
        반환값: observer 결과 또는 observer가 없으면 None
        작성 날짜: 2026/08/24
        """
        # Bootstrap은 DTO를 만들지 않고 transport가 제공한 publication 경계만 호출한다.
        if trading_session_update_observer is None:
            return None

        return trading_session_update_observer(
            trading_controller,
            selected_execution_mode,
        )

    # Transport publication이 있는 production 조립에만 process당 단일 worker를 만든다.
    if trading_session_update_observer is not None:
        trading_event_runtime_worker = _TradingEventRuntimeWorker(
            run_trading_event_runtime_cycle,
            trading_controller.mark_event_runtime_failed,
            trading_event_processing_allowed,
            trading_controller.snapshot_session,
            application_lock,
            state_update_observer=publish_trading_session_update,
        )

    def account_stream_recovery_allowed() -> bool:
        """
        함수 이름: account_stream_recovery_allowed()
        기능: worker 재시도 직전에 testnet runtime의 READY와 startup reconciliation을 재검사한다.
        인자: 없음
        반환값: 자동 account stream 복구가 계속 허용되면 True
        작성 날짜: 2026/08/22
        """
        # Lifecycle publication과 Controller readiness를 같은 application RLock에서 읽는다.
        with application_lock:
            return (
                selected_execution_mode is ExecutionMode.TESTNET
                and application_state_store.state.status
                is ApplicationStatus.READY
                and trading_controller.startup_reconciliation_complete
            )

    def publish_account_stream_recovery_success() -> None:
        """
        함수 이름: publish_account_stream_recovery_success()
        기능: Controller의 gate 재개와 같은 RLock에서 Account와 trading 상태를 게시한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # 종료 owner가 시작된 뒤에는 이미 진행한 재조정으로 새 transport event를 만들지 않는다.
        if (
            application_state_store.state.status
            is not ApplicationStatus.READY
        ):
            return

        try:
            if account_update_observer is not None:
                account_update_observer(
                    account
                )  # 두 번째 REST snapshot의 authoritative Account를 trading 상태보다 먼저 보낸다.
            if trading_session_update_observer is not None:
                trading_session_update_observer(
                    trading_controller,
                    selected_execution_mode,
                )  # Fail-close event 뒤 다시 열린 command gate와 lifecycle을 반드시 이어 게시한다.
        except Exception as error:
            # Publication 일부가 실패하면 backend만 주문 가능 상태로 남지 않도록 영구 gate를 닫는다.
            trading_controller.mark_event_runtime_failed()
            if trading_session_update_observer is not None:
                try:
                    trading_session_update_observer(
                        trading_controller,
                        selected_execution_mode,
                    )
                except Exception:
                    pass  # 실패한 transport를 재귀 호출하지 않고 기존 fail-close event를 신뢰한다.
            raise AccountStreamRecoveryBlockedError(
                "account stream recovery publication failed"
            ) from error

    def recover_account_stream() -> object:
        """
        함수 이름: recover_account_stream()
        기능: worker thread에서 full REST reconciliation을 실행하고 복구 snapshot을 게시한다.
        인자: 없음
        반환값: 새 account stream subscription
        작성 날짜: 2026/08/22
        """
        # Worker는 lock을 선점하지 않고 Controller가 commit hook까지 동일한 session RLock로 소유한다.
        return trading_controller.reconnect_account_stream_after_reconciliation(
            recovery_commit_observer=(
                publish_account_stream_recovery_success
            ),
        )  # UI가 Account와 열린 gate를 관찰한 뒤에만 Controller Operation이 반환한다.

    # 실제 authenticated stream을 사용하는 testnet에만 자동 복구 owner를 조립한다.
    if selected_execution_mode is ExecutionMode.TESTNET:
        account_stream_recovery_worker = _AccountStreamRecoveryWorker(
            recover_account_stream,
            account_stream_recovery_allowed,
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

    # Startup command와 runtime은 앞서 만든 not-ready state store identity를 공유한다.
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
        _trading_event_runtime_worker=trading_event_runtime_worker,
        _account_stream_recovery_worker=account_stream_recovery_worker,
        _state_store=application_state_store,
        _shutdown_store=_ApplicationShutdownStore(),
    )
