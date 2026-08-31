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
from .lifecycle import (
    ShutdownBlockedError,
    ShutdownReceiptStatus,
    ShutdownSafetyReceipt,
    close_application,
    request_application_shutdown,
    start_application,
)
from .testnet import (
    TestnetConfiguration,
    TestnetConfigurationError,
    create_testnet_application_runtime,
    load_testnet_configuration,
    require_phase13_public_case2_permission,
    require_testnet_order_permission,
)


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
    "ShutdownBlockedError",
    "ShutdownReceiptStatus",
    "ShutdownSafetyReceipt",
    "TestnetConfiguration",
    "TestnetConfigurationError",
    "close_application",
    "create_application_runtime",
    "create_testnet_application_runtime",
    "load_testnet_configuration",
    "parse_execution_mode",
    "request_application_shutdown",
    "require_phase13_public_case2_permission",
    "require_testnet_order_permission",
    "start_application",
]
