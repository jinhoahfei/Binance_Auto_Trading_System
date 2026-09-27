"""화면과 거래 worker에 의존하지 않는 단일 안전 종료 준비 작업."""

from dataclasses import dataclass, field
from threading import RLock, Thread, Timer
from time import monotonic
from uuid import uuid4

from binance_auto_trader.adapters.binance.request_deadline import (
    RequestDeadlineExceeded, deadline_lock, request_deadline_scope,
)
from binance_auto_trader.application.shutdown_recovery import ShutdownPreparationBlocked, prepare_shutdown_cycle
from binance_auto_trader.bootstrap.resource_cleanup import close_runtime_resource


@dataclass
class ShutdownPreparation:
    """
    클래스 이름: ShutdownPreparation
    기능: 120초 기한을 가진 단일 종료 준비 작업과 진행 상태를 보관한다.
    작성 날짜: 2026/09/16
    """
    operation_id: str
    expected_version: int
    liquidation_confirmed: bool
    phase: str = "checking"
    step: str = "workers"
    version: int = 0
    reason_code: str | None = None
    retryable: bool = False
    balance_reconciliation: dict | None = None
    active: bool = True
    version_validated: bool = False
    deadline: float = field(default_factory=lambda: monotonic() + 120)
    lock: RLock = field(default_factory=RLock, repr=False)

    def remaining_seconds(self) -> float:
        """
        함수 이름: remaining_seconds()
        기능: 같은 종료 작업의 잠금·조회·정리에 사용할 남은 단조 시계 예산을 반환한다.
        인자: 없음
        반환값: 양수인 잔여 초, 만료이면 종료 차단 예외
        작성 날짜: 2026/09/27
        """
        self.check()
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise ShutdownPreparationBlocked("SHUTDOWN_PREPARATION_TIMEOUT", True)
        return remaining

    def snapshot(self) -> dict:
        """
        함수 이름: snapshot()
        기능: 별도 짧은 잠금으로 종료 진행 상태의 복사본을 반환한다.
        인자: 없음
        반환값: 해당 단계의 처리 결과 또는 없음
        작성 날짜: 2026/09/16
        """
        with self.lock:
            return {name: getattr(self, name) for name in (
                "operation_id", "phase", "step", "version", "reason_code", "retryable", "balance_reconciliation",
            )}

    def update_balance(self, details: dict | None) -> None:
        """
        함수 이름: update_balance()
        기능: 조회 결과는 짧은 상태 잠금으로만 게시한다. 기한 후에는 갱신하지 않는다.
        인자: details -> 원금·보상·잔고 대조 결과 또는 None
        반환값: 없음
        작성 날짜: 2026/09/16
        """
        with self.lock:
            if self.phase not in ("ready", "blocked"):
                self.balance_reconciliation = details

    def check(self) -> None:
        """
        함수 이름: check()
        기능: 기한이 지나거나 차단된 종료 작업의 후속 처리를 중단한다.
        인자: 없음
        반환값: 해당 단계의 처리 결과 또는 없음
        작성 날짜: 2026/09/16
        """
        with self.lock:
            if monotonic() >= self.deadline or self.phase == "blocked":
                raise ShutdownPreparationBlocked("SHUTDOWN_PREPARATION_TIMEOUT", True)

    def progress(self, phase: str, step: str, version: int | None = None) -> None:
        """
        함수 이름: progress()
        기능: 현재 작업 단계와 확인한 거래 버전을 게시한다.
        인자: phase, step, version -> 해당 작업에 필요한 입력 값
        반환값: 해당 단계의 처리 결과 또는 없음
        작성 날짜: 2026/09/16
        """
        with self.lock:
            self.check()
            self.phase, self.step = phase, step
            if version is not None:
                self.version = version

    def block(self, code: str, retryable: bool) -> None:
        """
        함수 이름: block()
        기능: 최초 종료 차단 이유를 보존하며 작업을 차단 상태로 전환한다.
        인자: code, retryable -> 해당 작업에 필요한 입력 값
        반환값: 해당 단계의 처리 결과 또는 없음
        작성 날짜: 2026/09/16
        """
        with self.lock:
            if self.phase == "ready" or self.phase == "blocked":
                return
            self.phase, self.reason_code, self.retryable = "blocked", code, retryable


def get_shutdown_preparation(runtime) -> dict | None:
    """
    함수 이름: get_shutdown_preparation()
    기능: 대시보드와 독립적으로 현재 종료 준비 작업을 조회한다.
    인자: runtime -> 해당 작업에 필요한 입력 값
    반환값: 해당 단계의 처리 결과 또는 없음
    작성 날짜: 2026/09/16
    """
    store = runtime._shutdown_store
    with store.preparation_lock:
        return None if store.preparation is None else store.preparation.snapshot()


def start_shutdown_preparation(runtime, *, expected_version: int, liquidation_confirmed: bool) -> dict:
    """
    함수 이름: start_shutdown_preparation()
    기능: 신규 거래를 막고 중복 요청을 단일 종료 준비 작업에 연결한다.
    인자: runtime, expected_version, liquidation_confirmed -> 해당 작업에 필요한 입력 값
    반환값: 해당 단계의 처리 결과 또는 없음
    작성 날짜: 2026/09/16
    """
    store = runtime._shutdown_store
    with store.preparation_lock:
        previous = store.preparation
        if previous is not None and previous.active:
            return previous.snapshot()
        operation = ShutdownPreparation(str(uuid4()), expected_version, liquidation_confirmed,
            version=expected_version)
        store.preparation = operation
        # 즉시 새 command를 막는다. 진행 중 effect의 완료 확인은 아래 worker join이 담당한다.
        runtime.trading_controller._shutdown_preparing = True
        runtime.trading_controller._market_resume_suppressed = True
        try:
            Thread(target=_run, args=(runtime, operation), name="binance-shutdown-prepare", daemon=True).start()
        except Exception as error:
            operation.block("SHUTDOWN_RESOURCE_CLEANUP_FAILED", True)
            with operation.lock:
                operation.active = False
            runtime.diagnostics.record_exception("shutdown_worker_start", error, operation_id=operation.operation_id)
            runtime.diagnostics.record("shutdown_preparation_finished", **operation.snapshot())
        return operation.snapshot()


def _run(runtime, operation: ShutdownPreparation) -> None:
    """
    함수 이름: _run()
    기능: 작업자를 정리하고 계좌 검증 결과와 제한 시간 초과를 게시한다.
    인자: runtime, operation -> 해당 작업에 필요한 입력 값
    반환값: 해당 단계의 처리 결과 또는 없음
    작성 날짜: 2026/09/16
    """
    timer = None
    try:
        try:
            timer = Timer(operation.remaining_seconds(),
                lambda: operation.block("SHUTDOWN_PREPARATION_TIMEOUT", True))
            timer.daemon = True
            timer.start()
        except ShutdownPreparationBlocked:
            raise
        except Exception as error:
            runtime.diagnostics.record_exception("shutdown_timer_start", error, operation_id=operation.operation_id)
            raise ShutdownPreparationBlocked("SHUTDOWN_RESOURCE_CLEANUP_FAILED", True) from error
        with request_deadline_scope(operation.remaining_seconds()):
            _prepare_with_deadline(runtime, operation)
        operation.progress("ready", "complete", runtime.trading_controller.context.version)
    except RequestDeadlineExceeded:
        operation.block("SHUTDOWN_PREPARATION_TIMEOUT", True)
    except ShutdownPreparationBlocked as error:
        operation.block(error.code, error.retryable)
    except Exception as error:
        runtime.diagnostics.record_exception("shutdown_preparation", error,
            operation_id=operation.operation_id, step=operation.snapshot()["step"])
        operation.block("SHUTDOWN_PREPARATION_INTERNAL_ERROR", False)
    finally:
        if timer is not None:
            timer.cancel()
        with operation.lock:
            operation.active = False  # 기한 표시와 달리 실제 소유자의 반환 시점에만 해제한다.
        runtime.diagnostics.record("shutdown_preparation_finished", **operation.snapshot())


def _prepare_with_deadline(runtime, operation: ShutdownPreparation) -> None:
    """
    함수 이름: _prepare_with_deadline()
    기능: 작업자·구독·계좌 검증을 하나의 종료 예산과 단일 소유자로 실행한다.
    인자: runtime -> 정리할 runtime, operation -> 현재 작업과 남은 기한
    반환값: 종료 준비를 검증하면 없음
    작성 날짜: 2026/09/27
    """
    # join과 REST를 application_lock 밖에서 수행한다. GET 진행 조회는 별도 짧은 lock만 쓴다.
    for resource_name, worker in (
        ("trading event worker", runtime._trading_event_runtime_worker),
        ("account stream recovery worker", runtime._account_stream_recovery_worker),
        ("market stream recovery worker", runtime._market_stream_recovery_worker),
    ):
        operation.check()
        if worker is not None:
            try:
                close_runtime_resource(runtime, resource_name,
                    lambda: worker.close(timeout=operation.remaining_seconds()))
            except ShutdownPreparationBlocked:
                raise
            except TimeoutError as error:
                raise ShutdownPreparationBlocked("SHUTDOWN_PREPARATION_TIMEOUT", True) from error
            except Exception as error:
                raise ShutdownPreparationBlocked("SHUTDOWN_RESOURCE_CLEANUP_FAILED", True) from error
    operation.check()
    controller = runtime.trading_controller
    # Validate the user's basis before our own subscription cleanup changes context version.
    with deadline_lock(runtime.application_lock):
        # HTTP 명령이 관측 잠금을 양보한 채 I/O 중이어도 두 청산 소유자가 겹치지 않는다.
        while controller._effect_owner is not None:
            controller._effect_condition.wait(timeout=operation.remaining_seconds())
        operation.check()
        if controller.context.version != operation.expected_version:
            raise ShutdownPreparationBlocked("STALE_CONTEXT_VERSION", True)
        operation.version_validated = True
    try:
        close_runtime_resource(runtime, "market stream", runtime.market_data_controller.close_market_stream)
    except RequestDeadlineExceeded:
        raise
    except Exception as error:
        raise ShutdownPreparationBlocked("SHUTDOWN_RESOURCE_CLEANUP_FAILED", True) from error
    operation.check()
    previous = controller.account_subscription
    if previous is not None:
        try:
            close_runtime_resource(runtime, "account subscription", previous.close)
        except RequestDeadlineExceeded:
            raise
        except Exception as error:
            raise ShutdownPreparationBlocked("SHUTDOWN_RESOURCE_CLEANUP_FAILED", True) from error
        controller._account_subscription = None
    # Drain any already-entered callback/command before the sole owner begins.
    with deadline_lock(runtime.application_lock):
        operation.check()
    prepare_shutdown_cycle(controller, operation)
