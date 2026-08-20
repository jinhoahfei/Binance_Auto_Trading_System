"""Application 조립, startup 상태와 lifecycle의 공개 bootstrap API를 제공한다."""

# 외부 조립 caller가 내부 module 경로에 결합하지 않도록 안정된 lifecycle surface만 모은다.
from .application import (
    ApplicationRuntime,
    ApplicationStartupError,
    ApplicationStateSnapshot,
    ApplicationStatus,
    ExecutionMode,
    StartupFailure,
    StartupFailureCode,
    StartupStage,
    StartupTraceEntry,
    StartupTraceResult,
    create_application_runtime,
    parse_execution_mode,
)
from .lifecycle import close_application, start_application


__all__ = [  # wildcard import도 명시된 composition-root API 밖으로 확장되지 않는다.
    "ApplicationRuntime",
    "ApplicationStartupError",
    "ApplicationStateSnapshot",
    "ApplicationStatus",
    "ExecutionMode",
    "StartupFailure",
    "StartupFailureCode",
    "StartupStage",
    "StartupTraceEntry",
    "StartupTraceResult",
    "close_application",
    "create_application_runtime",
    "parse_execution_mode",
    "start_application",
]
