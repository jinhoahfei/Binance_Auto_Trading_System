"""실제 public market 경계에서 Phase 13 Spot Testnet Case 2를 검증한다."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, localcontext
from enum import Enum
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Lock, RLock, Thread
import time
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

# Historical evidence harness는 POSIX descriptor·flock 계약이므로 Windows import에서 격리한다.
if os.name == "posix":
    import fcntl


from binance_auto_trader.adapters.binance import (
    AccountAssetFilter,
    AccountOrderCountFilter,
    AccountRelevantFilters,
    CommissionDiscountPolicy,
    OrderPreparationFilterEvidence,
    OrderSubmissionAttemptEvidence,
    Phase13OrderSubmissionAttempt,
    Phase13OrderSubmissionGuardSnapshot,
    ReferencePrice,
    SymbolTradingRules,
)
from binance_auto_trader.adapters.binance.mappers import (
    NotionalFilter,
    QuantityFilter,
    SymbolFilterError,
    floor_market_quantity,
    validate_account_relevant_filters,
    validate_market_notional,
)
from binance_auto_trader.application import (
    MarketDataController,
    OrderExecutionFailureCode,
    PublicMarketBoundaryTraceEntry,
    ReconciliationCauseCategory,
    ReconciliationCauseSnapshot,
    ReconciliationCauseStatus,
    TradingSessionStatus,
)
from binance_auto_trader.bootstrap import (
    ApplicationStartupError,
    ApplicationRuntime,
    ApplicationStateSnapshot,
    ApplicationStatus,
    StartupFailure,
    StartupFailureCode,
    StartupStage,
    TestnetConfiguration,
    close_application,
    create_testnet_application_runtime,
    load_testnet_configuration,
    require_phase13_public_case2_permission,
    start_application,
)
from binance_auto_trader.bootstrap.application import (
    _TradingEventRuntimeFailureSnapshot,
    _TradingEventRuntimeFailureStage,
    _normalize_trading_event_runtime_exception_origin,
    _normalize_trading_event_runtime_exception_type,
)
from binance_auto_trader.bootstrap.testnet import (
    BINANCE_RUN_PHASE13_PUBLIC_CASE2_ENV,
    BINANCE_RUN_TESTNET_ENV,
    BINANCE_RUN_TESTNET_ORDERS_ENV,
    BINANCE_TESTNET_API_KEY_ENV,
    BINANCE_TESTNET_API_SECRET_ENV,
    BINANCE_TESTNET_MAX_NOTIONAL_ENV,
)
from binance_auto_trader.domain.common import Interval, RegimeType
from binance_auto_trader.domain.history import Performance, Trade
from binance_auto_trader.domain.market import Kline, MarketStateSnapshot
from binance_auto_trader.domain.trading import (
    Account,
    AccountSnapshot,
    AccountStateSnapshot,
    AssetBalance,
    DailyLossScope,
    ExitReason,
    Fill,
    ManualKillBehavior,
    OrderResult,
    OrderSide,
    OrderStatus,
    RiskPolicy,
    StrategyType,
)
from binance_auto_trader.transport import (
    BackendEventStream,
    create_account_update_observer,
    create_trade_history_update_observer,
    create_trading_session_update_observer,
)

from tests.testnet._phase13_trace import (
    PHASE13_PUBLIC_TRACE_RECORD_TYPE,
    PHASE13_PUBLIC_TRACE_SCHEMA_VERSION,
    _parse_public_market_command_event,
    canonical_actual_phase13_public_trace_bytes,
    seal_actual_phase13_public_trace,
    seal_phase13_public_trace,
    validate_actual_phase13_public_trace,
)
from tests.testnet._deterministic_public_case2_fixture import (
    DeterministicPublicCase2Klines,
    create_deterministic_public_case2_klines,
)
from tests.testnet._support import (
    PHASE13_PUBLIC_CASE2_REQUESTED,
    PHASE13_PUBLIC_CASE2_SKIP_REASON,
    require_empty_all_client_open_orders,
    seed_verified_closed_history,
    verify_exact_recent_order_baseline,
)
from tests.unit.history.factories import make_trade


# 실제 주문 target은 결정론적 public signal과 same-ID reconciliation을 기다리되 무한 대기는 허용하지 않는다.
_DETERMINISTIC_BUY_TIMEOUT_SECONDS = 180
_DETERMINISTIC_EVALUATION_TIMEOUT_SECONDS = 15
_ORDER_SETTLEMENT_TIMEOUT_SECONDS = 60
_ACCOUNT_SETTLEMENT_TIMEOUT_SECONDS = 15
_POLL_INTERVAL_SECONDS = 0.25
_MAXIMUM_RECORDED_MARKET_EVENTS = 512
# Public artifact의 구조 한도와 Controller의 scheduled REST query 예산을 동일하게 검증한다.
_MAXIMUM_ORDER_TRACE_ENTRY_COUNT = 2048
_MAXIMUM_ORDER_QUERY_TRACE_COUNT = 4
_ARTIFACT_ROOT = Path(__file__).resolve().parents[2] / ".testnet-artifacts"
_PHASE13_PROCESS_LEASE_PATH = (
    _ARTIFACT_ROOT / ".phase13-public-case2.lock"
)
_PENDING_ORDER_FIELDS = frozenset(
    {
        "client_order_id",
        "exit_reason",
        "exit_pct_b_at_intent",
        "intent_id",
        "market_price_at_decision",
        "regime_type",
        "requested_quantity",
        "risk_policy_version",
        "side",
        "strategy",
        "submission_attempt",
        "submitted_quantity",
        "symbol",
    }
)
_TRANSPORT_AGGREGATES = {
    "ACCOUNT_UPDATED": "ACCOUNT",
    "ORDER_EXECUTED": "TRADE_HISTORY",
    "PERFORMANCE_UPDATED": "TRADE_HISTORY",
    "TRADING_SESSION_UPDATED": "TRADING_SESSION",
}
_ORDER_TRACE_MESSAGE_ORDER = (
    "1",
    "2",
    "3",
    "4",
    "5.1",
    "5",
    "6",
    "6.1",
    "7",
    "8",
    "8.1",
    "8.2",
    "9",
    "10",
    "11",
    "12",
    "13",
    "13.1",
    "13.2",
    "13.3",
    "13.4",
    "13.5",
    "13.5.1",
    "14",
)
_ZERO_POSITION_FAILURE_MESSAGE = (
    "Phase 13 requires a verified zero Position"
)
_ZERO_MARKET_BUY_COMMISSION_FAILURE_MESSAGE = (
    "Phase 13 requires zero MARKET BUY commission"
)
_BUY_ORDER_TRACE_PREFIX = (
    "1",
    "2",
    "3",
    "4",
    "5.1",
    "5",
    "6",
    "6.1",
    "7",
)
_SELL_ORDER_TRACE_PREFIX = (
    "1",
    "2",
    "3",
    "4",
    "5",
    "6",
    "6.1",
    "7",
)
_SAME_ORDER_QUERY_TRACE = ("8", "8.1", "8.2", "9")
_BUY_FILL_APPLICATION_TRACE = ("10", "12")
_SELL_FILL_APPLICATION_TRACE = ("10", "11", "12")
_BUY_ORDER_TRACE_SUFFIX = (
    "13",
    "13.2",
    "13.3",
    "13.4",
    "13.5",
    "13.5.1",
    "14",
)
_SELL_ORDER_TRACE_SUFFIX = (
    "13",
    "13.1",
    "13.2",
    "13.3",
    "13.4",
    "13.5",
    "13.5.1",
    "14",
)
_ARTIFACT_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9.-]{0,127}\.json\Z")
_FAILURE_TOKEN_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_FAILURE_SAFE_TEXT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z")
_FAILURE_SECRET_FRAGMENT_PATTERN = re.compile(
    r"(?:api[_-]?(?:key|secret)|credential|authorization|cookie|signature|"
    r"private[_-]?key|session[_-]?token|raw[_-]?(?:header|payload)|"
    r"query[_-]?(?:param|parameter))",
    re.IGNORECASE,
)
# 새 failure artifact는 v2만 쓰되 보존된 v1 bytes는 별도 exact branch로 계속 검증한다.
_PHASE13_FAILURE_EVIDENCE_SCHEMA_VERSION = 2
_SUPPORTED_PHASE13_FAILURE_EVIDENCE_SCHEMA_VERSIONS = frozenset({1, 2})
_PHASE13_FAILURE_EVIDENCE_RECORD_TYPE = (
    "phase13_public_case2_testnet_failure_evidence"
)
_FAILURE_EVIDENCE_BODY_FIELDS_V1 = frozenset(
    {
        "schema_version",
        "record_type",
        "outcome",
        "typed_reason",
        "run_id",
        "timestamps",
        "mutation_guard",
        "recovery",
        "runtime_state",
        "fresh_verification",
        "evidence_errors",
    }
)
_FAILURE_EVIDENCE_BODY_FIELDS_V2 = _FAILURE_EVIDENCE_BODY_FIELDS_V1 | {
    "first_cause"
}
_FAILURE_EVIDENCE_DOCUMENT_FIELDS_V1 = _FAILURE_EVIDENCE_BODY_FIELDS_V1 | {
    "failure_sha256"
}
_FAILURE_EVIDENCE_DOCUMENT_FIELDS_V2 = _FAILURE_EVIDENCE_BODY_FIELDS_V2 | {
    "failure_sha256"
}
_FAILURE_MUTATION_GUARD_FIELDS = frozenset(
    {"mutation_started", "submissions_blocked", "submission_attempts"}
)
_FAILURE_RECOVERY_FIELDS = frozenset(
    {"attempted", "outcome", "typed_reason"}
)
_FAILURE_SUBMISSION_ATTEMPT_FIELDS_V1 = frozenset(
    {
        "sequence",
        "symbol",
        "side",
        "order_type",
        "intent_id",
        "submission_attempt",
        "attempted_at",
        "client_order_id",
    }
)
_FAILURE_SUBMISSION_ATTEMPT_FIELDS_V2 = (
    _FAILURE_SUBMISSION_ATTEMPT_FIELDS_V1
    - {"intent_id", "client_order_id"}
)
_FAILURE_RUNTIME_STATE_FIELDS = frozenset(
    {
        "application_status",
        "trading_status",
        "reconciliation_required",
        "position_quantity",
        "pending_order_count",
        "durable_trade_count",
    }
)
_FAILURE_FRESH_VERIFICATION_FIELDS_V1 = frozenset(
    {
        "status",
        "typed_reason",
        "verified_at",
        "position_quantity",
        "pending_order_count",
        "reconciliation_required",
        "matching_open_order_count",
        "run_exchange_order_count",
        "durable_trade_count",
    }
)
_FAILURE_FRESH_VERIFICATION_FIELDS_V2 = (
    _FAILURE_FRESH_VERIFICATION_FIELDS_V1
    | {
        "failure_stage",
        "account_open_orders_empty",
        "account_open_order_lists_empty",
    }
)
_FAILURE_FRESH_OBSERVATION_FIELDS_V2 = (
    "verified_at",
    "position_quantity",
    "pending_order_count",
    "reconciliation_required",
    "matching_open_order_count",
    "account_open_orders_empty",
    "account_open_order_lists_empty",
    "run_exchange_order_count",
    "durable_trade_count",
)
_FAILURE_FIRST_CAUSE_FIELDS = frozenset(
    {"reconciliation_required", "status", "category"}
)
_FAILURE_FIRST_CAUSE_STATUSES = frozenset(
    {"MISSING", "EXACT", "DUPLICATE", "CONFLICT"}
)
_FAILURE_FIRST_CAUSE_CATEGORIES = frozenset(
    {
        "ACCOUNT_STREAM_UNKNOWN_OR_EXTERNAL_EXECUTION",
        "PREPARE_FILTER_OR_CAP_REJECTED",
        "PREPARE_DATA_INVALID",
        "PREPARE_INTERNAL_ERROR",
        "EVENT_WORKER_OR_RUNTIME_FAILED",
        "MARKET_STREAM_FAILED",
        "ORDER_OR_PERSISTENCE_AMBIGUOUS",
        "PROCESS_OWNERSHIP_AMBIGUOUS",
    }
)
_FAILURE_FRESH_VERIFICATION_STATUSES = frozenset({"VERIFIED", "INCOMPLETE"})
_FAILURE_FRESH_VERIFICATION_STAGES = frozenset(
    {
        "RUNTIME_CREATION",
        "STARTUP_APPLICATION",
        "MARKET",
        "REGIME",
        "ACCOUNT",
        "HISTORY",
        "RECONCILIATION",
        "STREAM",
        "FILTER",
        "LOCAL_STATE",
        "OPEN_ORDERS",
        "OPEN_ORDER_LISTS",
        "RECENT_ORDERS",
        "DURABILITY",
        "CLEANUP",
    }
)
_FAILURE_APPLICATION_STATUSES = frozenset(
    {"CREATED", "STARTING", "READY", "SHUTTING_DOWN", "FAILED", "CLOSED"}
)
_FAILURE_TRADING_STATUSES = frozenset(
    {
        "NOT_STARTED",
        "RUNNING",
        "STOPPING",
        "RECONCILIATION_REQUIRED",
        "TERMINATED",
    }
)
_FAILURE_ACTUAL_REASONS = frozenset(
    {
        "ACTUAL_TIMEOUT",
        "ACTUAL_ASSERTION_FAILED",
        "ACTUAL_PERMISSION_FAILED",
        "ACTUAL_IO_FAILED",
        "ACTUAL_VALIDATION_FAILED",
        "ACTUAL_RUNTIME_FAILED",
        "ACTUAL_UNEXPECTED_FAILURE",
    }
)
_FAILURE_RECOVERY_OUTCOMES = frozenset(
    {"NOT_REQUIRED", "SKIPPED", "SUCCESS", "FAILED"}
)
_FAILURE_RECOVERY_REASONS = frozenset(
    {
        "FAILURE_RECOVERY_TIMEOUT",
        "FAILURE_RECOVERY_ASSERTION_FAILED",
        "FAILURE_RECOVERY_PERMISSION_FAILED",
        "FAILURE_RECOVERY_IO_FAILED",
        "FAILURE_RECOVERY_VALIDATION_FAILED",
        "FAILURE_RECOVERY_RUNTIME_FAILED",
        "FAILURE_RECOVERY_UNEXPECTED_FAILURE",
        "FAILURE_RECOVERY_STATE_UNAVAILABLE",
        "FAILURE_RECOVERY_STATE_AMBIGUOUS",
        "FAILURE_RECOVERY_EFFECTIVE_FREE_UNCONFIRMED",
        "FAILURE_RECOVERY_STATE_CHANGED",
        "FAILURE_RECOVERY_FINAL_VERIFICATION_INCOMPLETE",
    }
)
_FAILURE_RECOVERY_FAILED_REASONS = frozenset(
    {
        "FAILURE_RECOVERY_TIMEOUT",
        "FAILURE_RECOVERY_ASSERTION_FAILED",
        "FAILURE_RECOVERY_PERMISSION_FAILED",
        "FAILURE_RECOVERY_IO_FAILED",
        "FAILURE_RECOVERY_VALIDATION_FAILED",
        "FAILURE_RECOVERY_RUNTIME_FAILED",
        "FAILURE_RECOVERY_UNEXPECTED_FAILURE",
        "FAILURE_RECOVERY_FINAL_VERIFICATION_INCOMPLETE",
    }
)
_FAILURE_STARTUP_REASONS = frozenset(
    {
        "APPLICATION_ALREADY_STARTING",
        "APPLICATION_CLOSED",
        "MARKET_INITIALIZATION_FAILED",
        "MARKET_NOT_READY",
        "REGIME_NOT_READY",
        "ACCOUNT_INITIALIZATION_FAILED",
        "ACCOUNT_NOT_READY",
        "HISTORY_INITIALIZATION_FAILED",
        "ORDER_RECONCILIATION_FAILED",
        "ORDER_RECONCILIATION_NOT_READY",
    }
)
_FAILURE_FRESH_STAGE_ALLOWED_REASONS = {
    "RUNTIME_CREATION": frozenset({"FRESH_VERIFICATION_FAILED"}),
    "STARTUP_APPLICATION": frozenset(
        {
            "FRESH_VERIFICATION_FAILED",
            "FRESH_RUNTIME_NOT_READY",
            "APPLICATION_ALREADY_STARTING",
            "APPLICATION_CLOSED",
        }
    ),
    "MARKET": frozenset(
        {"MARKET_INITIALIZATION_FAILED", "MARKET_NOT_READY"}
    ),
    "REGIME": frozenset({"REGIME_NOT_READY"}),
    "ACCOUNT": frozenset(
        {"ACCOUNT_INITIALIZATION_FAILED", "ACCOUNT_NOT_READY"}
    ),
    "HISTORY": frozenset({"HISTORY_INITIALIZATION_FAILED"}),
    "RECONCILIATION": frozenset(
        {
            "ORDER_RECONCILIATION_FAILED",
            "ORDER_RECONCILIATION_NOT_READY",
        }
    ),
    "STREAM": frozenset({"FRESH_STREAM_NOT_READY"}),
    "FILTER": frozenset({"FRESH_FILTER_REJECTED"}),
    "LOCAL_STATE": frozenset({"FRESH_LOCAL_STATE_INCOMPLETE"}),
    "OPEN_ORDERS": frozenset(
        {"FRESH_OPEN_ORDERS_NOT_EMPTY", "FRESH_OPEN_ORDERS_CHANGED"}
    ),
    "OPEN_ORDER_LISTS": frozenset(
        {"FRESH_OPEN_ORDER_LISTS_NOT_EMPTY"}
    ),
    "RECENT_ORDERS": frozenset(
        {
            "FRESH_EXCHANGE_BASELINE_UNAVAILABLE",
            "FRESH_RECENT_ORDERS_CHANGED",
        }
    ),
    "DURABILITY": frozenset({"FRESH_DURABILITY_CHANGED"}),
    "CLEANUP": frozenset(
        {
            "FRESH_RUNTIME_CLOSE_FAILED",
            "FRESH_SNAPSHOT_CLEANUP_FAILED",
            "PRIMARY_RUNTIME_NOT_CLOSED",
        }
    ),
}
_FAILURE_FRESH_REASONS = frozenset().union(
    *_FAILURE_FRESH_STAGE_ALLOWED_REASONS.values()
)
_FAILURE_FRESH_SECONDARY_REASONS = frozenset(
    {
        "FRESH_RUNTIME_CLOSE_FAILED",
        "FRESH_DURABILITY_CHANGED",
        "FRESH_SNAPSHOT_CLEANUP_FAILED",
    }
)
_FAILURE_POST_VERIFICATION_STAGE_REASONS = frozenset(
    {
        ("DURABILITY", "FRESH_DURABILITY_CHANGED"),
        ("CLEANUP", "FRESH_RUNTIME_CLOSE_FAILED"),
        ("CLEANUP", "FRESH_SNAPSHOT_CLEANUP_FAILED"),
    }
)
_FAILURE_FRESH_ALLOWED_SECONDARY_BY_PRIMARY = {
    "FRESH_RUNTIME_CLOSE_FAILED": frozenset(
        {
            "FRESH_DURABILITY_CHANGED",
            "FRESH_SNAPSHOT_CLEANUP_FAILED",
        }
    ),
    "FRESH_DURABILITY_CHANGED": frozenset(
        {"FRESH_SNAPSHOT_CLEANUP_FAILED"}
    ),
    "FRESH_SNAPSHOT_CLEANUP_FAILED": frozenset(),
    "PRIMARY_RUNTIME_NOT_CLOSED": frozenset(),
}
_FAILURE_CAUSE_ERRORS = frozenset(
    {
        "RECONCILIATION_CAUSE_MISSING",
        "RECONCILIATION_CAUSE_DUPLICATE",
        "RECONCILIATION_CAUSE_CONFLICT",
    }
)
_FAILURE_RUNTIME_EVIDENCE_ERRORS = frozenset(
    {
        "RUNTIME_RECONCILIATION_UNAVAILABLE",
        "RUNTIME_POSITION_UNAVAILABLE",
        "RUNTIME_PENDING_UNAVAILABLE",
        "RUNTIME_HISTORY_UNAVAILABLE",
    }
)
_FAILURE_FINALIZER_EVIDENCE_ERRORS = frozenset(
    {
        "FAILURE_SCHEDULER_GATE_PUBLICATION_FAILED",
    }
)
_FAILURE_EVIDENCE_ERRORS = (
    _FAILURE_RECOVERY_REASONS
    | _FAILURE_FRESH_REASONS
    | _FAILURE_CAUSE_ERRORS
    | _FAILURE_RUNTIME_EVIDENCE_ERRORS
    | _FAILURE_FINALIZER_EVIDENCE_ERRORS
)
# Error 목록은 producer의 cause→recovery→gate→runtime→fresh→final downgrade 순서를 보존한다.
_FAILURE_EVIDENCE_ERROR_PHASES = (
    _FAILURE_CAUSE_ERRORS,
    _FAILURE_RECOVERY_REASONS
    - {"FAILURE_RECOVERY_FINAL_VERIFICATION_INCOMPLETE"},
    _FAILURE_FINALIZER_EVIDENCE_ERRORS,
    frozenset({"RUNTIME_RECONCILIATION_UNAVAILABLE"}),
    frozenset({"RUNTIME_POSITION_UNAVAILABLE"}),
    frozenset({"RUNTIME_PENDING_UNAVAILABLE"}),
    frozenset({"RUNTIME_HISTORY_UNAVAILABLE"}),
    _FAILURE_FRESH_REASONS - _FAILURE_FRESH_SECONDARY_REASONS,
    frozenset({"FRESH_RUNTIME_CLOSE_FAILED"}),
    frozenset({"FRESH_DURABILITY_CHANGED"}),
    frozenset({"FRESH_SNAPSHOT_CLEANUP_FAILED"}),
    frozenset({"FAILURE_RECOVERY_FINAL_VERIFICATION_INCOMPLETE"}),
)
_STARTUP_STAGE_TO_FAILURE_STAGE = {
    StartupStage.APPLICATION: "STARTUP_APPLICATION",
    StartupStage.MARKET: "MARKET",
    StartupStage.REGIME: "REGIME",
    StartupStage.ACCOUNT: "ACCOUNT",
    StartupStage.HISTORY: "HISTORY",
    StartupStage.RECONCILIATION: "RECONCILIATION",
}
_STARTUP_STAGE_ALLOWED_FAILURE_CODES = {
    StartupStage.APPLICATION: frozenset(
        {
            StartupFailureCode.APPLICATION_ALREADY_STARTING,
            StartupFailureCode.APPLICATION_CLOSED,
        }
    ),
    StartupStage.MARKET: frozenset(
        {
            StartupFailureCode.MARKET_INITIALIZATION_FAILED,
            StartupFailureCode.MARKET_NOT_READY,
        }
    ),
    StartupStage.REGIME: frozenset(
        {StartupFailureCode.REGIME_NOT_READY}
    ),
    StartupStage.ACCOUNT: frozenset(
        {
            StartupFailureCode.ACCOUNT_INITIALIZATION_FAILED,
            StartupFailureCode.ACCOUNT_NOT_READY,
        }
    ),
    StartupStage.HISTORY: frozenset(
        {StartupFailureCode.HISTORY_INITIALIZATION_FAILED}
    ),
    StartupStage.RECONCILIATION: frozenset(
        {
            StartupFailureCode.ORDER_RECONCILIATION_FAILED,
            StartupFailureCode.ORDER_RECONCILIATION_NOT_READY,
        }
    ),
}
_FAILURE_DURABILITY_FILE_SUFFIXES = (
    "",
    ".pending-orders.jsonl",
    ".manual-kill-control.jsonl",
)


class _StrictJsonError(ValueError):
    """
    클래스 이름: _StrictJsonError
    기능: test-only pending journal parser가 중복 key 또는 비표준 상수를 발견했음을 나타낸다.
    작성 날짜: 2026/08/31
    """


class _PhaseThirteenRecordedOutcomeFailure(AssertionError):
    """
    클래스 이름: _PhaseThirteenRecordedOutcomeFailure
    기능: NO_SIGNAL trace가 이미 durable하게 기록된 의도된 test failure를 구분한다.
    작성 날짜: 2026/08/31
    """


class _PhaseThirteenMarketFailureReason(str, Enum):
    """
    클래스 이름: _PhaseThirteenMarketFailureReason
    기능: actual이 관찰한 Kline stream 실패를 원문 없는 두 안정 범주로 구분한다.
    작성 날짜: 2026/09/04
    """

    KLINE_STREAM_INVALID = "KLINE_STREAM_INVALID"
    KLINE_STREAM_DISCONNECTED_OR_UNAVAILABLE = (
        "KLINE_STREAM_DISCONNECTED_OR_UNAVAILABLE"
    )


@dataclass(frozen=True, slots=True)
class _PhaseThirteenMarketFailureSnapshot:
    """
    클래스 이름: _PhaseThirteenMarketFailureSnapshot
    기능: Gateway의 Kline 실패 범주와 optional secret-free 예외 진단을 불변 보존한다.
    작성 날짜: 2026/09/04
    """

    reason: _PhaseThirteenMarketFailureReason
    exception_type: str | None
    exception_origin: str | None

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: market failure snapshot의 enum과 optional 진단 쌍을 엄격히 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/04
        """
        if not isinstance(self.reason, _PhaseThirteenMarketFailureReason):
            raise TypeError("reason must be a Phase 13 market failure reason")
        if (self.exception_type is None) is not (
            self.exception_origin is None
        ):
            raise ValueError("market exception diagnostics must be paired")
        if self.exception_type is not None and (
            not self.exception_type.isascii()
            or not self.exception_type.isidentifier()
        ):
            raise ValueError("market exception type must be an ASCII identifier")
        if self.exception_origin is not None and (
            self.exception_origin != "EXTERNAL_OR_UNKNOWN"
            and not self.exception_origin.startswith("binance_auto_trader.")
        ):
            raise ValueError("market exception origin must be package-scoped")


class _PhaseThirteenReconciliationFailure(AssertionError):
    """
    클래스 이름: _PhaseThirteenReconciliationFailure
    기능: reconciliation 감지 순간의 원자 cause snapshot을 원문 없는 고정 실패에 결속한다.
    작성 날짜: 2026/08/31
    """

    def __init__(
        self,
        cause_snapshot: ReconciliationCauseSnapshot,
        order_failure_code: OrderExecutionFailureCode | None = None,
        worker_failure_snapshot: _TradingEventRuntimeFailureSnapshot
        | None = None,
        market_failure_snapshot: _PhaseThirteenMarketFailureSnapshot
        | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: frozen cause와 optional typed worker·order 진단을 secret-free 고정 assertion에 결속한다.
        인자: cause_snapshot -> Controller lock에서 한 번에 읽은 reconciliation cause snapshot
            order_failure_code -> 마지막 typed order trace failure 또는 trace가 없으면 None
            worker_failure_snapshot -> event worker의 원문 없는 immutable failure 진단 또는 None
            market_failure_snapshot -> Kline stream의 원문 없는 immutable failure 진단 또는 None
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        if type(cause_snapshot) is not ReconciliationCauseSnapshot:
            raise TypeError("cause_snapshot must be exact")
        if order_failure_code is not None and not isinstance(
            order_failure_code,
            OrderExecutionFailureCode,
        ):
            raise TypeError(
                "order_failure_code must be an OrderExecutionFailureCode or None"
            )
        if worker_failure_snapshot is not None and type(
            worker_failure_snapshot
        ) is not _TradingEventRuntimeFailureSnapshot:
            raise TypeError(
                "worker_failure_snapshot must be an exact runtime failure snapshot or None"
            )
        if market_failure_snapshot is not None and type(
            market_failure_snapshot
        ) is not _PhaseThirteenMarketFailureSnapshot:
            raise TypeError(
                "market_failure_snapshot must be an exact market failure snapshot or None"
            )

        # EXACT enum과 정규화된 worker 진단만 노출하고 raw callback reason이나 예외 문구는 포함하지 않는다.
        diagnostic_parts: list[str] = []
        if cause_snapshot.status is ReconciliationCauseStatus.EXACT:
            cause_category = cause_snapshot.category
            if cause_category is None:
                raise AssertionError("EXACT cause snapshot requires a category")
            diagnostic_parts.append(f"cause_category={cause_category.value}")
        if order_failure_code is not None:
            diagnostic_parts.append(
                f"order_failure_code={order_failure_code.value}"
            )
        if worker_failure_snapshot is not None:
            diagnostic_parts.extend(
                (
                    f"worker_failure_stage={worker_failure_snapshot.stage.value}",
                    f"worker_failure_type={worker_failure_snapshot.exception_type}",
                    f"worker_failure_origin={worker_failure_snapshot.exception_origin}",
                )
            )
        if market_failure_snapshot is not None:
            diagnostic_parts.append(
                f"market_failure_reason={market_failure_snapshot.reason.value}"
            )
            if market_failure_snapshot.exception_type is not None:
                diagnostic_parts.extend(
                    (
                        "market_failure_type="
                        f"{market_failure_snapshot.exception_type}",
                        "market_failure_origin="
                        f"{market_failure_snapshot.exception_origin}",
                    )
                )
        diagnostic_suffix = "".join(
            f"; {diagnostic_part}"
            for diagnostic_part in diagnostic_parts
        )
        super().__init__(
            "public Case 2 entered reconciliation before a durable BUY"
            f"{diagnostic_suffix}"
        )
        self.cause_snapshot = cause_snapshot  # Finalizer는 오염 전 frozen identity만 다시 읽는다.
        self.order_failure_code = order_failure_code
        self.worker_failure_snapshot = worker_failure_snapshot
        self.market_failure_snapshot = market_failure_snapshot


@dataclass(frozen=True, slots=True)
class _DeterministicBuyCandidate:
    """
    클래스 이름: _DeterministicBuyCandidate
    기능: actual 시작 전 account·cap·filter를 통과한 public split과 BUY 수량 근거를 보존한다.
    작성 날짜: 2026/09/04
    """

    scale_in: Decimal
    submitted_quantity: Decimal
    decision_notional: Decimal
    reference_notional: Decimal


def _read_phase13_market_failure_snapshot(
    runtime: ApplicationRuntime,
) -> _PhaseThirteenMarketFailureSnapshot:
    """
    함수 이름: _read_phase13_market_failure_snapshot()
    기능: Gateway의 최초 Kline buffer 오류를 raw 원문 없이 actual 진단 snapshot으로 읽는다.
    인자: runtime -> 현재 actual ApplicationRuntime
    반환값: invalid 또는 disconnected/unavailable로 정규화한 market failure snapshot
    작성 날짜: 2026/09/04
    """
    if not isinstance(runtime, ApplicationRuntime):
        raise TypeError("runtime must be an ApplicationRuntime")
    gateway = runtime.web_socket_gateway
    gateway_lock = getattr(gateway, "_lock", None)
    if not hasattr(gateway_lock, "__enter__"):
        raise TypeError("WebSocket Gateway must expose its diagnostic lock")

    # Gateway lock 아래 최초 buffer error identity만 읽고 raw message·payload는 반환하지 않는다.
    with gateway_lock:
        buffer_error = getattr(gateway, "_buffer_error", None)
    if not isinstance(buffer_error, BaseException):
        return _PhaseThirteenMarketFailureSnapshot(
            reason=(
                _PhaseThirteenMarketFailureReason.KLINE_STREAM_DISCONNECTED_OR_UNAVAILABLE
            ),
            exception_type=None,
            exception_origin=None,
        )

    return _PhaseThirteenMarketFailureSnapshot(
        reason=_PhaseThirteenMarketFailureReason.KLINE_STREAM_INVALID,
        exception_type=(
            _normalize_trading_event_runtime_exception_type(buffer_error)
        ),
        exception_origin=(
            _normalize_trading_event_runtime_exception_origin(buffer_error)
        ),
    )  # Raw exception은 Gateway memory에만 남기고 정규화된 세 필드만 actual assertion에 전달한다.


def _assert_complete_success_order_trace(
    message_ids: Sequence[str],
    *,
    side: OrderSide,
) -> None:
    """
    함수 이름: _assert_complete_success_order_trace()
    기능: 성공 주문의 고정 prefix·same-ID query/stream/fill branch·durable suffix를 exact 검증한다.
    인자: message_ids -> 한 client order ID에 귀속된 실제 message ID 순서
        side -> BUY 또는 SELL에 따른 위험·원가 trace branch
    반환값: complete 성공 grammar이면 없음
    작성 날짜: 2026/08/31
    """
    if isinstance(message_ids, (str, bytes)) or not isinstance(
        message_ids,
        Sequence,
    ):
        raise TypeError("message_ids must be a non-string sequence")
    if not isinstance(side, OrderSide):
        raise TypeError("side must be an OrderSide")
    normalized_ids = tuple(message_ids)
    if len(normalized_ids) > _MAXIMUM_ORDER_TRACE_ENTRY_COUNT:
        raise AssertionError("successful order trace exceeds the structural limit")
    if any(
        not isinstance(message_id, str)
        or message_id not in _ORDER_TRACE_MESSAGE_ORDER
        for message_id in normalized_ids
    ):
        raise AssertionError("successful order trace contains an unknown message ID")

    # BUY만 5.1 risk 허용을 요구하고 SELL만 11·13.1 원가/실현 경계를 요구한다.
    expected_prefix = (
        _BUY_ORDER_TRACE_PREFIX
        if side is OrderSide.BUY
        else _SELL_ORDER_TRACE_PREFIX
    )
    fill_application_trace = (
        _BUY_FILL_APPLICATION_TRACE
        if side is OrderSide.BUY
        else _SELL_FILL_APPLICATION_TRACE
    )
    expected_suffix = (
        _BUY_ORDER_TRACE_SUFFIX
        if side is OrderSide.BUY
        else _SELL_ORDER_TRACE_SUFFIX
    )
    if normalized_ids[: len(expected_prefix)] != expected_prefix:
        raise AssertionError("successful order trace prefix is missing or duplicated")
    if normalized_ids[-len(expected_suffix) :] != expected_suffix:
        raise AssertionError("successful order trace durable suffix is incomplete")

    branch_ids = normalized_ids[
        len(expected_prefix) : -len(expected_suffix)
    ]
    branch_cursor = 0
    applied_fill_count = 0
    query_count = 0
    terminal_evidence_complete = False

    # 최초 submit이 partial/filled를 반환한 경우에만 query group 앞에 fill application이 올 수 있다.
    if (
        tuple(
            branch_ids[
                branch_cursor : branch_cursor + len(fill_application_trace)
            ]
        )
        == fill_application_trace
    ):
        branch_cursor += len(fill_application_trace)
        applied_fill_count += 1
        terminal_evidence_complete = True

    # 이후에는 최대 네 same-ID query와 그 query가 새로 관찰한 fill delta만 허용한다.
    while branch_cursor < len(branch_ids):
        # Account stream reapply는 REST query prefix 없이 message 9로 직접 들어올 수 있다.
        if branch_ids[branch_cursor] == "9":
            branch_cursor += 1
        else:
            if (
                tuple(
                    branch_ids[
                        branch_cursor : branch_cursor
                        + len(_SAME_ORDER_QUERY_TRACE)
                    ]
                )
                != _SAME_ORDER_QUERY_TRACE
            ):
                raise AssertionError(
                    "successful order trace contains an illegal branch"
                )
            branch_cursor += len(_SAME_ORDER_QUERY_TRACE)
            query_count += 1
            if query_count > _MAXIMUM_ORDER_QUERY_TRACE_COUNT:
                raise AssertionError(
                    "successful order trace exceeds the query budget"
                )
        terminal_evidence_complete = False

        if (
            tuple(
                branch_ids[
                    branch_cursor : branch_cursor + len(fill_application_trace)
                ]
            )
            == fill_application_trace
        ):
            branch_cursor += len(fill_application_trace)
            applied_fill_count += 1
            terminal_evidence_complete = True
            continue  # Partial stream 결과 뒤 예약 query나 다음 stream 결과가 이어질 수 있다.

        # 앞선 partial fill 뒤 terminal 응답에 새 delta가 없으면 summary 10만 suffix 직전에 추가된다.
        if (
            branch_ids[branch_cursor:] == ("10",)
            and applied_fill_count > 0
        ):
            branch_cursor += 1
            terminal_evidence_complete = True

    if applied_fill_count < 1 or not terminal_evidence_complete:
        raise AssertionError("successful order trace lacks terminal fill evidence")


def _acquire_phase13_process_lease(lock_path: Path) -> int:
    """
    함수 이름: _acquire_phase13_process_lease()
    기능: owner 전용 regular lockfile에 process-wide nonblocking exclusive lease를 획득한다.
    인자: lock_path -> 고정 Phase 13 lease leaf 경로
    반환값: teardown까지 열어 둘 locked file descriptor
    작성 날짜: 2026/08/31
    """
    if not isinstance(lock_path, Path):
        raise TypeError("lock_path must be a Path")
    if not lock_path.name or lock_path.name in {".", ".."}:
        raise ValueError("lock_path must contain a canonical leaf name")
    no_follow_flag = getattr(os, "O_NOFOLLOW", None)
    if no_follow_flag is None:
        raise RuntimeError("Phase 13 process lease requires O_NOFOLLOW")
    parent_path_state = os.stat(
        lock_path.parent,
        follow_symlinks=False,
    )
    if not stat.S_ISDIR(parent_path_state.st_mode):
        raise PermissionError(
            "Phase 13 lease parent must be a non-symlink private directory"
        )
    if parent_path_state.st_uid != os.geteuid():
        raise PermissionError("Phase 13 lease parent must be owned by this user")

    # Parent와 lock leaf를 no-follow descriptor로 열어 symlink 교체나 다른 inode lock을 거부한다.
    parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    parent_flags |= getattr(os, "O_CLOEXEC", 0) | no_follow_flag
    parent_descriptor = os.open(lock_path.parent, parent_flags)
    lease_descriptor: int | None = None
    try:
        parent_descriptor_state = os.fstat(parent_descriptor)
        if (
            parent_descriptor_state.st_dev,
            parent_descriptor_state.st_ino,
        ) != (
            parent_path_state.st_dev,
            parent_path_state.st_ino,
        ):
            raise RuntimeError("Phase 13 lease parent identity changed")
        os.fchmod(parent_descriptor, 0o700)
        private_parent_state = os.fstat(parent_descriptor)
        if (
            not stat.S_ISDIR(private_parent_state.st_mode)
            or private_parent_state.st_uid != os.geteuid()
            or private_parent_state.st_mode & 0o777 != 0o700
        ):
            raise PermissionError(
                "Phase 13 lease parent could not be made owner-only"
            )
        lease_flags = os.O_RDWR | os.O_CREAT
        lease_flags |= getattr(os, "O_CLOEXEC", 0) | no_follow_flag
        lease_descriptor = os.open(
            lock_path.name,
            lease_flags,
            0o600,
            dir_fd=parent_descriptor,
        )
        lease_state_before_mode_change = os.fstat(lease_descriptor)
        if (
            not stat.S_ISREG(lease_state_before_mode_change.st_mode)
            or lease_state_before_mode_change.st_uid != os.geteuid()
            or lease_state_before_mode_change.st_nlink != 1
        ):
            raise PermissionError(
                "Phase 13 lease must be a singly linked owner regular file"
            )

        # Type·owner·link 검증 전에는 공격자가 가리킨 unrelated inode의 mode를 절대 변경하지 않는다.
        os.fchmod(lease_descriptor, 0o600)
        lease_state = os.fstat(lease_descriptor)
        if (
            not stat.S_ISREG(lease_state.st_mode)
            or lease_state.st_mode & 0o777 != 0o600
            or lease_state.st_uid != os.geteuid()
            or lease_state.st_nlink != 1
            or (
                lease_state.st_dev,
                lease_state.st_ino,
            )
            != (
                lease_state_before_mode_change.st_dev,
                lease_state_before_mode_change.st_ino,
            )
        ):
            raise PermissionError(
                "Phase 13 lease must be an owner-only regular file"
            )
        lease_path_state = os.stat(lock_path, follow_symlinks=False)
        if (
            lease_path_state.st_dev,
            lease_path_state.st_ino,
        ) != (lease_state.st_dev, lease_state.st_ino):
            raise RuntimeError("Phase 13 lease path identity changed")
        try:
            fcntl.flock(
                lease_descriptor,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as error:
            raise RuntimeError(
                "another Phase 13 public Case 2 process owns the execution lease"
            ) from error

        # Lock 획득 뒤 같은 parent dirfd의 leaf를 다시 확인해 unlink/recreate 이중 lease race를 닫는다.
        locked_path_state = os.stat(
            lock_path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        parent_path_state_after = os.stat(
            lock_path.parent,
            follow_symlinks=False,
        )
        if (
            locked_path_state.st_dev,
            locked_path_state.st_ino,
        ) != (lease_state.st_dev, lease_state.st_ino):
            raise RuntimeError("Phase 13 lease inode changed after lock")
        if (
            parent_path_state_after.st_dev,
            parent_path_state_after.st_ino,
        ) != (
            private_parent_state.st_dev,
            private_parent_state.st_ino,
        ):
            raise RuntimeError("Phase 13 lease parent changed after lock")
    except BaseException:
        if lease_descriptor is not None:
            os.close(lease_descriptor)
        raise
    finally:
        os.close(parent_descriptor)

    if lease_descriptor is None:
        raise RuntimeError("Phase 13 process lease was not initialized")
    return lease_descriptor  # 열린 descriptor 자체가 process 종료까지 advisory lease를 소유한다.


def _release_phase13_process_lease(lease_descriptor: int) -> None:
    """
    함수 이름: _release_phase13_process_lease()
    기능: Phase 13 process-wide lease를 명시적으로 해제하고 descriptor를 회수한다.
    인자: lease_descriptor -> acquire가 반환한 locked file descriptor
    반환값: 없음
    작성 날짜: 2026/08/31
    """
    if type(lease_descriptor) is not int or lease_descriptor < 0:
        raise ValueError("lease_descriptor must be a non-negative exact integer")

    # Unlock 실패에서도 descriptor close를 보장해 process-local 권한을 남기지 않는다.
    try:
        fcntl.flock(lease_descriptor, fcntl.LOCK_UN)
    finally:
        os.close(lease_descriptor)


def _acquire_deterministic_kline_delivery_guard(
    web_socket_gateway: object,
) -> object:
    """
    함수 이름: _acquire_deterministic_kline_delivery_guard()
    기능: actual fixture 수명 동안 실제 Kline callback 전달을 test-only 경계에서 직렬화한다.
    인자: web_socket_gateway -> 현재 actual runtime의 Binance WebSocket Gateway
    반환값: unittest cleanup에서 같은 thread가 해제할 획득된 delivery guard
    작성 날짜: 2026/09/04
    """
    delivery_guard = getattr(
        web_socket_gateway,
        "_kline_delivery_lock",
        None,
    )
    acquire = getattr(delivery_guard, "acquire", None)
    release = getattr(delivery_guard, "release", None)
    if not callable(acquire) or not callable(release):
        raise TypeError(
            "web_socket_gateway must expose the Kline delivery guard"
        )

    # 이미 진행 중인 live callback이 끝난 stable snapshot 경계부터 fixture 수명을 시작한다.
    acquire()
    return delivery_guard  # Production observer나 order seam을 바꾸지 않고 transport 전달만 직렬화한다.


def _release_deterministic_kline_delivery_guard(
    delivery_guard: object,
) -> None:
    """
    함수 이름: _release_deterministic_kline_delivery_guard()
    기능: actual runtime 종료 뒤 test-only Kline delivery guard를 같은 unittest thread에서 해제한다.
    인자: delivery_guard -> acquire helper가 반환한 획득된 reentrant guard
    반환값: 없음
    작성 날짜: 2026/09/04
    """
    release = getattr(delivery_guard, "release", None)
    if not callable(release):
        raise TypeError("delivery_guard must provide release")

    # Runtime close가 generation을 먼저 닫은 뒤 대기 중 callback이 stale 상태만 관찰하게 한다.
    release()  # addCleanup이 실행되는 동일 unittest thread에서 ownership을 정확히 반납한다.


def _prepare_deterministic_buy_candidate(
    *,
    free_quote_quantity: Decimal,
    decision_price: Decimal,
    maximum_notional: Decimal,
    symbol_rules: SymbolTradingRules,
    account_filters: AccountRelevantFilters,
    reference_price: ReferencePrice,
) -> _DeterministicBuyCandidate:
    """
    함수 이름: _prepare_deterministic_buy_candidate()
    기능: actual 시작 전에 free quote·base MAX_ASSET·공개 filter를 만족하는 BUY split과 수량을 계산한다.
    인자: free_quote_quantity -> startup Account의 현재 free USDT
        decision_price -> deterministic RECOVERY Kline의 양수 종가
        maximum_notional -> 사용자가 승인한 BUY decision-notional 상한
        symbol_rules -> fresh public ETHUSDT 거래 규칙
        account_filters -> fresh signed account relevant filter 합성값
        reference_price -> MARKET notional 검증용 공식 reference price
    반환값: 공개 split command와 filter 후 수량·notional을 묶은 candidate
    작성 날짜: 2026/09/04
    """
    decimal_inputs = (
        ("free_quote_quantity", free_quote_quantity, True),
        ("decision_price", decision_price, False),
        ("maximum_notional", maximum_notional, False),
    )
    for field_name, field_value, zero_allowed in decimal_inputs:
        if not isinstance(field_value, Decimal) or not field_value.is_finite():
            raise TypeError(f"{field_name} must be a finite Decimal")
        if field_value < Decimal("0") or (
            not zero_allowed and field_value == Decimal("0")
        ):
            raise ValueError(f"{field_name} must be positive")
    if type(symbol_rules) is not SymbolTradingRules:
        raise TypeError("symbol_rules must be exact")
    if type(account_filters) is not AccountRelevantFilters:
        raise TypeError("account_filters must be exact")
    if type(reference_price) is not ReferencePrice:
        raise TypeError("reference_price must be exact")
    if reference_price.symbol != symbol_rules.symbol:
        raise ValueError("reference_price must match symbol_rules")
    if free_quote_quantity == Decimal("0"):
        raise ValueError("deterministic BUY requires free quote balance")

    # Base MAX_ASSET가 outer cap보다 작으면 public split만 줄이고 private quantity seam은 만들지 않는다.
    base_asset_limits = tuple(
        account_filter.maximum_quantity
        for account_filter in account_filters.asset_filters
        if account_filter.asset == symbol_rules.base_asset
    )
    with localcontext() as decimal_context:
        decimal_context.prec = 34
        decimal_context.rounding = ROUND_DOWN
        scale_in = Decimal("1")
        if base_asset_limits:
            maximum_base_quantity = min(base_asset_limits)
            base_limited_scale = (
                maximum_base_quantity
                * decision_price
                / free_quote_quantity
            )
            scale_in = min(scale_in, base_limited_scale)
        if scale_in <= Decimal("0"):
            raise ValueError(
                "deterministic BUY account limit permits no positive quantity"
            )
        natural_quantity = (
            free_quote_quantity * scale_in / decision_price
        )
        capped_quantity = min(
            natural_quantity,
            maximum_notional / decision_price,
        )

    # Production과 같은 floor·reference-price·signed account validator를 session 시작 전에 재사용한다.
    submitted_quantity = floor_market_quantity(
        capped_quantity,
        symbol_rules,
    )
    validate_market_notional(
        submitted_quantity,
        reference_price.price,
        symbol_rules,
    )
    validate_account_relevant_filters(
        submitted_quantity,
        reference_price.price,
        symbol_rules,
        account_filters,
        side=OrderSide.BUY,
        account_open_state_verified_empty=True,
    )
    with localcontext() as decimal_context:
        decimal_context.prec = 34
        decision_notional = submitted_quantity * decision_price
        reference_notional = submitted_quantity * reference_price.price
    if decision_notional > maximum_notional:
        raise ValueError("deterministic BUY exceeds the approved notional cap")

    return _DeterministicBuyCandidate(
        scale_in=scale_in,
        submitted_quantity=submitted_quantity,
        decision_notional=decision_notional,
        reference_notional=reference_notional,
    )  # Candidate 수치 자체는 성공 trace의 production order evidence로만 최종 확정한다.


def _utc_now() -> datetime:
    """
    함수 이름: _utc_now()
    기능: trace와 bounded observation에 사용할 timezone-aware UTC 현재 시각을 반환한다.
    인자: 없음
    반환값: timezone.utc 기반 현재 datetime
    작성 날짜: 2026/08/31
    """
    return datetime.now(timezone.utc)  # 로컬 timezone이나 naive 시각이 evidence에 섞이지 않게 한다.


class _RunScopedEvidenceClock:
    """
    클래스 이름: _RunScopedEvidenceClock
    기능: 단일 UTC anchor와 monotonic 경과 시간으로 한 run의 비후퇴 증거 시각을 만든다.
    작성 날짜: 2026/09/01
    """

    __slots__ = (
        "_anchor_monotonic_ns",
        "_anchor_utc",
        "_last_utc",
        "_lock",
        "_monotonic_clock",
    )

    def __init__(
        self,
        *,
        wall_clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], int] | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: wall UTC를 한 번만 읽고 대응하는 monotonic 기준점을 고정한다.
        인자: wall_clock -> 최초 UTC anchor provider 또는 None
            monotonic_clock -> 경과 nanosecond provider 또는 None
        반환값: 없음
        작성 날짜: 2026/09/01
        """
        selected_wall_clock = _utc_now if wall_clock is None else wall_clock
        selected_monotonic_clock = (
            time.monotonic_ns
            if monotonic_clock is None
            else monotonic_clock
        )
        if not callable(selected_wall_clock):
            raise TypeError("wall_clock must be callable or None")
        if not callable(selected_monotonic_clock):
            raise TypeError("monotonic_clock must be callable or None")
        anchor_utc = selected_wall_clock()
        anchor_monotonic_ns = selected_monotonic_clock()
        if (
            not isinstance(anchor_utc, datetime)
            or anchor_utc.tzinfo is None
            or anchor_utc.utcoffset() != timedelta(0)
        ):
            raise ValueError("wall_clock must return a timezone-aware UTC datetime")
        if type(anchor_monotonic_ns) is not int:
            raise TypeError("monotonic_clock must return an exact integer")

        # 이후 wall clock은 다시 읽지 않아 NTP·수동 시각 역행이 artifact ordering을 깨지 못한다.
        self._anchor_utc = anchor_utc.astimezone(timezone.utc)
        self._anchor_monotonic_ns = anchor_monotonic_ns
        self._monotonic_clock = selected_monotonic_clock
        self._last_utc = self._anchor_utc
        self._lock = Lock()

    def __call__(self) -> datetime:
        """
        함수 이름: __call__()
        기능: monotonic 경과를 UTC anchor에 더하고 concurrent·source regression을 직전 시각으로 clamp한다.
        인자: 없음
        반환값: 이전 반환보다 이르지 않은 timezone-aware UTC datetime
        작성 날짜: 2026/09/01
        """
        with self._lock:
            monotonic_now_ns = self._monotonic_clock()
            if type(monotonic_now_ns) is not int:
                raise TypeError("monotonic_clock must return an exact integer")
            elapsed_ns = max(
                0,
                monotonic_now_ns - self._anchor_monotonic_ns,
            )
            candidate_utc = self._anchor_utc + timedelta(
                microseconds=elapsed_ns // 1_000,
            )
            if candidate_utc < self._last_utc:
                candidate_utc = self._last_utc
            self._last_utc = candidate_utc
            return candidate_utc  # 같은 microsecond 반환도 허용하되 어떤 call도 뒤로 가지 않는다.


def _datetime_to_wire(value: datetime) -> str:
    """
    함수 이름: _datetime_to_wire()
    기능: timezone-aware UTC datetime을 trace schema의 microsecond Z 표현으로 직렬화한다.
    인자: value -> 직렬화할 UTC datetime
    반환값: RFC 3339 UTC 문자열
    작성 날짜: 2026/08/31
    """
    if not isinstance(value, datetime):
        raise TypeError("value must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("value must be timezone-aware")

    # 입력 instant를 UTC로 정규화한 뒤 trace validator가 허용하는 고정 microsecond precision을 사용한다.
    normalized_value = value.astimezone(timezone.utc)
    return normalized_value.isoformat(timespec="microseconds").replace(
        "+00:00",
        "Z",
    )


def _decimal_to_wire(value: Decimal) -> str:
    """
    함수 이름: _decimal_to_wire()
    기능: 유한 Decimal을 exponent 없는 trace 금융 문자열로 직렬화한다.
    인자: value -> 직렬화할 금융 Decimal
    반환값: plain decimal 문자열
    작성 날짜: 2026/08/31
    """
    if not isinstance(value, Decimal) or not value.is_finite():
        raise TypeError("value must be a finite Decimal")

    return format(value, "f")  # Float와 exponent 표기를 모두 우회하지 못하게 원 Decimal scale을 보존한다.


def _reject_json_constant(constant_name: str) -> object:
    """
    함수 이름: _reject_json_constant()
    기능: pending journal의 NaN과 Infinity 같은 비표준 JSON 상수를 거부한다.
    인자: constant_name -> JSON decoder가 관찰한 상수 이름
    반환값: 정상 반환하지 않음
    작성 날짜: 2026/08/31
    """
    raise _StrictJsonError("pending journal contains a non-standard constant")


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """
    함수 이름: _strict_json_object()
    기능: JSON object pair를 중복 key 없는 dictionary로 변환한다.
    인자: pairs -> decoder가 원래 순서대로 제공한 key/value pair
    반환값: 중복이 없는 JSON dictionary
    작성 날짜: 2026/08/31
    """
    decoded_object: dict[str, object] = {}
    for object_key, object_value in pairs:
        if object_key in decoded_object:
            raise _StrictJsonError("pending journal contains a duplicate key")
        decoded_object[object_key] = object_value  # 모든 key 검증 뒤 exact schema가 별도로 판정한다.

    return decoded_object


def _extract_pending_order_upserts(
    pending_order_path: Path,
) -> tuple[dict[str, object], ...]:
    """
    함수 이름: _extract_pending_order_upserts()
    기능: append-only pending sidecar에서 현재 schema의 최초 ORDER UPSERT metadata만 추출한다.
    인자: pending_order_path -> TradeHistoryRepository가 공개한 sidecar 경로
    반환값: journal 순서의 secret-free Order metadata dictionary tuple
    작성 날짜: 2026/08/31
    """
    if not isinstance(pending_order_path, Path):
        raise TypeError("pending_order_path must be a Path")
    if not pending_order_path.exists():
        return ()  # 주문이 한 번도 없었던 run은 sidecar 자체가 없는 것이 정상이다.

    # Raw line을 오류에 반사하지 않고 strict JSON과 exact current UPSERT schema만 허용한다.
    extracted_upserts: list[dict[str, object]] = []
    observed_orders_by_client_id: dict[str, dict[str, object]] = {}
    with pending_order_path.open("r", encoding="utf-8", newline="") as pending_file:
        for line_number, raw_line in enumerate(pending_file, start=1):
            if not raw_line.endswith("\n"):
                raise _StrictJsonError(
                    f"pending journal line {line_number} is incomplete"
                )
            try:
                decoded_event = json.loads(
                    raw_line,
                    object_pairs_hook=_strict_json_object,
                    parse_constant=_reject_json_constant,
                )
            except (json.JSONDecodeError, UnicodeError) as error:
                raise _StrictJsonError(
                    f"pending journal line {line_number} is invalid"
                ) from error
            if not isinstance(decoded_event, dict):
                raise _StrictJsonError(
                    f"pending journal line {line_number} is not an object"
                )
            if decoded_event.get("operation") != "UPSERT":
                continue  # TRANSITION과 REMOVE는 submitted metadata를 바꾸지 않는 별도 lifecycle 증거다.
            if set(decoded_event) != {
                "schema_version",
                "record_type",
                "operation",
                "lifecycle",
                "order",
            }:
                raise _StrictJsonError("pending UPSERT has an invalid exact schema")
            if (
                decoded_event.get("schema_version") != 4
                or decoded_event.get("record_type") != "pending_order_event"
                or decoded_event.get("lifecycle") != "PREPARED"
            ):
                raise _StrictJsonError("pending UPSERT uses an unsupported schema")

            # 최종 filter 수량과 immutable decision provenance가 있는 current Order schema만 증거로 쓴다.
            order_metadata = decoded_event.get("order")
            if not isinstance(order_metadata, dict) or set(order_metadata) != set(
                _PENDING_ORDER_FIELDS
            ):
                raise _StrictJsonError("pending Order metadata has an invalid exact schema")
            client_order_id = order_metadata.get("client_order_id")
            if not isinstance(client_order_id, str) or not client_order_id:
                raise _StrictJsonError("pending Order client identity is invalid")
            previous_metadata = observed_orders_by_client_id.get(client_order_id)
            if previous_metadata is not None:
                if previous_metadata != order_metadata:
                    raise _StrictJsonError("pending Order identity has conflicting metadata")
                continue  # Crash가 남긴 byte-identical UPSERT는 한 실제 attempt로 수렴한다.
            copied_metadata = dict(order_metadata)
            observed_orders_by_client_id[client_order_id] = copied_metadata
            extracted_upserts.append(copied_metadata)

    return tuple(extracted_upserts)  # Journal의 최초 submit 순서는 actual 주문 수 증거가 된다.


def _transport_related_id(event_dto: Mapping[str, object]) -> str | None:
    """
    함수 이름: _transport_related_id()
    기능: normalized backend event payload에서 secret 없는 durable Trade 또는 session identity만 선택한다.
    인자: event_dto -> BackendEventEnvelope.to_dto() 결과
    반환값: client order/session ID 또는 관련 identity가 없으면 None
    작성 날짜: 2026/08/31
    """
    payload = event_dto.get("payload")
    if not isinstance(payload, Mapping):
        return None
    trade_payload = payload.get("trade")
    if isinstance(trade_payload, Mapping):
        trade_id = trade_payload.get("trade_id")
        if isinstance(trade_id, str) and trade_id:
            return trade_id
    trading_payload = payload.get("trading")
    if isinstance(trading_payload, Mapping):
        session_id = trading_payload.get("session_id")
        if isinstance(session_id, str) and session_id:
            return session_id

    return None  # Account와 Performance event에는 별도 correlation ID를 합성하지 않는다.


def _normalize_transport_ui_events(
    event_dtos: Sequence[Mapping[str, object]],
    *,
    transport_session_id: str,
) -> dict[str, object]:
    """
    함수 이름: _normalize_transport_ui_events()
    기능: 공개 envelope DTO의 실제 session/sequence/nullable aggregate version을 secret-free UI trace로 축약한다.
    인자: event_dtos -> BackendEventEnvelope.to_dto()만 담은 sequence
        transport_session_id -> event가 없어도 보존할 BackendEventStream session UUID
    반환값: transport session과 normalized event list를 가진 exact batch dictionary
    작성 날짜: 2026/08/31
    """
    if isinstance(event_dtos, (str, bytes)) or not isinstance(event_dtos, Sequence):
        raise TypeError("event_dtos must be a sequence")
    if not isinstance(transport_session_id, str) or not transport_session_id:
        raise ValueError("transport_session_id must be a non-empty string")

    # DTO의 aggregate_version=None을 임의 state version으로 바꾸지 않고 transport truth 그대로 보존한다.
    normalized_events: list[dict[str, object]] = []
    previous_transport_sequence = 0
    pending_trade_related_id: str | None = None
    for event_dto in event_dtos:
        if not isinstance(event_dto, Mapping):
            raise TypeError("event_dtos must contain mappings")
        event_type = event_dto.get("type")
        if event_type not in _TRANSPORT_AGGREGATES:
            continue  # Snapshot control 등 Phase 13 evidence schema 밖 event는 확대 해석하지 않는다.
        transport_sequence = event_dto.get("sequence")
        if type(transport_sequence) is not int or transport_sequence <= previous_transport_sequence:
            raise ValueError("transport event sequence must increase")
        previous_transport_sequence = transport_sequence
        aggregate_version = event_dto.get("aggregate_version")
        if aggregate_version is not None and (
            type(aggregate_version) is not int or aggregate_version < 0
        ):
            raise ValueError("transport aggregate version must be non-negative or None")
        event_session_id = event_dto.get("session_id")
        if event_session_id != transport_session_id:
            raise ValueError("transport event belongs to another session")
        occurred_at = event_dto.get("occurred_at")
        if not isinstance(occurred_at, str) or not occurred_at:
            raise ValueError("transport event lacks a publication time")
        event_id = event_dto.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("transport event lacks an event identity")
        related_id = _transport_related_id(event_dto)
        if event_type == "ORDER_EXECUTED":
            if related_id is None:
                raise ValueError("ORDER_EXECUTED lacks a normalized Trade identity")
            pending_trade_related_id = related_id
        elif event_type == "PERFORMANCE_UPDATED":
            if pending_trade_related_id is None:
                raise ValueError("performance event requires a preceding order event")
            related_id = pending_trade_related_id
            pending_trade_related_id = None  # Atomic pair의 second event 이후 correlation을 다음 pair로 넘기지 않는다.

        normalized_events.append(
            {
                "event_id": event_id,
                "transport_sequence": transport_sequence,
                "aggregate": _TRANSPORT_AGGREGATES[str(event_type)],
                "event_type": event_type,
                "aggregate_version": aggregate_version,
                "related_id": related_id,
                "published_at": occurred_at,
            }
        )

    if pending_trade_related_id is not None:
        raise ValueError("ORDER_EXECUTED lacks its atomic performance event")

    return {
        "transport_session_id": transport_session_id,
        "events": normalized_events,
    }  # Credential, raw payload와 local transport token은 출력 schema에 존재하지 않는다.


def _normalize_commission_policy(
    policy: CommissionDiscountPolicy,
) -> dict[str, object]:
    """
    함수 이름: _normalize_commission_policy()
    기능: credential과 raw rate payload 없이 제출 전 commission 정책을 exact trace 필드로 변환한다.
    인자: policy -> APIGateway가 엄격 해석한 commission 정책
    반환값: secret-free commission policy dictionary
    작성 날짜: 2026/08/31
    """
    if not isinstance(policy, CommissionDiscountPolicy):
        raise TypeError("policy must be a CommissionDiscountPolicy")

    # Derived MARKET BUY 비율과 discount-asset 가능성도 domain property에서 같이 보존한다.
    return {
        "symbol": policy.symbol,
        "enabled_for_account": policy.enabled_for_account,
        "enabled_for_symbol": policy.enabled_for_symbol,
        "discount_asset": policy.discount_asset,
        "discount_rate": _decimal_to_wire(policy.discount_rate),
        "standard_market_buy_rate": _decimal_to_wire(
            policy.standard_market_buy_rate
        ),
        "special_market_buy_rate": _decimal_to_wire(
            policy.special_market_buy_rate
        ),
        "tax_market_buy_rate": _decimal_to_wire(
            policy.tax_market_buy_rate
        ),
        "market_buy_received_asset_commission_rate": _decimal_to_wire(
            policy.market_buy_received_asset_commission_rate
        ),
        "can_charge_discount_asset": policy.can_charge_discount_asset,
    }


def _normalize_symbol_filter_rules(
    rules: SymbolTradingRules,
) -> dict[str, object]:
    """
    함수 이름: _normalize_symbol_filter_rules()
    기능: public SymbolTradingRules를 MARKET 수량·notional exact trace object로 축약한다.
    인자: rules -> 엄격 해석을 마친 public symbol rule
    반환값: observation 시각과 raw exchangeInfo가 없는 exact rule dictionary
    작성 날짜: 2026/08/31
    """
    if type(rules) is not SymbolTradingRules:
        raise TypeError("rules must be an exact SymbolTradingRules")

    # 여러 notional filter가 존재하면 MARKET에 적용되는 가장 엄격한 교집을 기록한다.
    minimum_candidates = tuple(
        notional_filter.minimum_notional
        for notional_filter in rules.notional_filters
        if notional_filter.apply_minimum_to_market
        and notional_filter.minimum_notional is not None
    )
    maximum_candidates = tuple(
        notional_filter.maximum_notional
        for notional_filter in rules.notional_filters
        if notional_filter.apply_maximum_to_market
        and notional_filter.maximum_notional is not None
    )
    effective_minimum = (
        max(minimum_candidates) if minimum_candidates else None
    )
    effective_maximum = (
        min(maximum_candidates) if maximum_candidates else None
    )

    return {
        "symbol_status": rules.status,
        "base_asset": rules.base_asset,
        "quote_asset": rules.quote_asset,
        "base_asset_precision": rules.base_asset_precision,
        "is_spot_trading_allowed": rules.is_spot_trading_allowed,
        "market_order_allowed": "MARKET" in rules.order_types,
        "lot_size_minimum_quantity": _decimal_to_wire(
            rules.lot_size.minimum_quantity
        ),
        "lot_size_maximum_quantity": _decimal_to_wire(
            rules.lot_size.maximum_quantity
        ),
        "lot_size_step_size": _decimal_to_wire(rules.lot_size.step_size),
        "market_lot_size_minimum_quantity": _decimal_to_wire(
            rules.market_lot_size.minimum_quantity
        ),
        "market_lot_size_maximum_quantity": _decimal_to_wire(
            rules.market_lot_size.maximum_quantity
        ),
        "market_lot_size_step_size": _decimal_to_wire(
            rules.market_lot_size.step_size
        ),
        "minimum_notional": (
            None
            if effective_minimum is None
            else _decimal_to_wire(effective_minimum)
        ),
        "maximum_notional": (
            None
            if effective_maximum is None
            else _decimal_to_wire(effective_maximum)
        ),
        "maximum_position": (
            None
            if rules.maximum_position is None
            else _decimal_to_wire(rules.maximum_position)
        ),
    }


def _normalize_fresh_filters(
    rules: SymbolTradingRules,
    *,
    observed_at: datetime,
) -> dict[str, object]:
    """
    함수 이름: _normalize_fresh_filters()
    기능: explicit preflight GET의 public rule과 응답 관찰 시각을 exact snapshot으로 묶는다.
    인자: rules -> 매번 새 exchangeInfo에서 해석한 symbol rule
        observed_at -> public GET 완료 후 UTC 관찰 시각
    반환값: raw exchangeInfo가 없는 exact preflight filter dictionary
    작성 날짜: 2026/08/31
    """
    # Preflight observation은 submit-time evidence와 구별하면서 같은 normalized rule schema를 재사용한다.
    return {
        "observed_at": _datetime_to_wire(observed_at),
        **_normalize_symbol_filter_rules(rules),
    }


def _normalize_account_asset_filters(
    account_asset_filters: Sequence[AccountAssetFilter],
) -> list[dict[str, object]]:
    """
    함수 이름: _normalize_account_asset_filters()
    기능: signed myFilters의 normalized MAX_ASSET tuple을 exact secret-free trace array로 변환한다.
    인자: account_asset_filters -> APIGateway가 엄격 해석한 account filter sequence
    반환값: filter type·asset·Decimal 상한만 담은 dictionary list
    작성 날짜: 2026/08/31
    """
    normalized_filters: list[dict[str, object]] = []

    # Raw signed payload와 query는 버리고 adapter value object의 안전 필드만 순서대로 보존한다.
    for account_filter in account_asset_filters:
        if type(account_filter) is not AccountAssetFilter:
            raise TypeError("account filters must contain exact AccountAssetFilter values")
        normalized_filters.append(
            {
                "filter_type": account_filter.filter_type,
                "asset": account_filter.asset,
                "maximum_quantity": _decimal_to_wire(
                    account_filter.maximum_quantity
                ),
            }
        )

    return normalized_filters  # Credential, request signature와 raw response는 artifact에 포함하지 않는다.


def _normalize_account_relevant_filters(
    account_filters: AccountRelevantFilters,
) -> dict[str, object]:
    """
    함수 이름: _normalize_account_relevant_filters()
    기능: signed myFilters의 full composite DTO를 exact secret-free trace object로 변환한다.
    인자: account_filters -> symbol에 결속된 strict AccountRelevantFilters
    반환값: scope별 filter 요약과 적용 집합을 담은 dictionary
    작성 날짜: 2026/08/31
    """
    if type(account_filters) is not AccountRelevantFilters:
        raise TypeError(
            "account_filters must be an exact AccountRelevantFilters"
        )

    # Count filter는 scope를 혼합하지 않고 공식 type과 integer limit만 보존한다.
    def normalize_count_filters(
        filter_values: Sequence[AccountOrderCountFilter],
    ) -> list[dict[str, object]]:
        """
        함수 이름: normalize_count_filters()
        기능: 하나의 count-filter scope를 JSON-safe exact entry 목록으로 축약한다.
        인자: filter_values -> AccountOrderCountFilter sequence
        반환값: filter type과 maximum count dictionary list
        작성 날짜: 2026/08/31
        """
        # Scope 내부 exact DTO만 순서대로 JSON-safe count entry로 축약한다.
        normalized_counts: list[dict[str, object]] = []
        for filter_value in filter_values:
            if type(filter_value) is not AccountOrderCountFilter:
                raise TypeError(
                    "count filters must contain strict normalized values"
                )
            normalized_counts.append(
                {
                    "filter_type": filter_value.filter_type,
                    "maximum_count": filter_value.maximum_count,
                }
            )

        return normalized_counts  # Signed raw mapping은 어떤 scope에서도 trace로 넘기지 않는다.

    # Quantity와 notional DTO는 Decimal을 canonical string으로 변환해 float 오차를 막는다.
    normalized_quantities = [
        {
            "filter_type": filter_value.filter_type,
            "minimum_quantity": _decimal_to_wire(
                filter_value.minimum_quantity
            ),
            "maximum_quantity": _decimal_to_wire(
                filter_value.maximum_quantity
            ),
            "step_size": _decimal_to_wire(filter_value.step_size),
        }
        for filter_value in account_filters.symbol_quantity_filters
    ]
    normalized_notionals = [
        {
            "filter_type": filter_value.filter_type,
            "minimum_notional": (
                None
                if filter_value.minimum_notional is None
                else _decimal_to_wire(filter_value.minimum_notional)
            ),
            "maximum_notional": (
                None
                if filter_value.maximum_notional is None
                else _decimal_to_wire(filter_value.maximum_notional)
            ),
            "apply_minimum_to_market": (
                filter_value.apply_minimum_to_market
            ),
            "apply_maximum_to_market": (
                filter_value.apply_maximum_to_market
            ),
            "average_price_minutes": filter_value.average_price_minutes,
        }
        for filter_value in account_filters.symbol_notional_filters
    ]

    return {
        "symbol": account_filters.symbol,
        "exchange_order_count_filters": normalize_count_filters(
            account_filters.exchange_order_count_filters
        ),
        "symbol_order_count_filters": normalize_count_filters(
            account_filters.symbol_order_count_filters
        ),
        "symbol_quantity_filters": normalized_quantities,
        "symbol_notional_filters": normalized_notionals,
        "symbol_maximum_position": (
            None
            if account_filters.symbol_maximum_position is None
            else _decimal_to_wire(
                account_filters.symbol_maximum_position
            )
        ),
        "passive_symbol_filter_types": sorted(
            account_filters.passive_symbol_filter_types
        ),
        "asset_filters": _normalize_account_asset_filters(
            account_filters.asset_filters
        ),
    }  # Symbol context와 검증된 값만 남기고 credential·signature·raw payload는 배제한다.


def _normalize_public_relevant_filters(
    rules: SymbolTradingRules,
) -> dict[str, object]:
    """
    함수 이름: _normalize_public_relevant_filters()
    기능: exchangeInfo의 public relevant filter union을 signed overlap 검증용 trace object로 만든다.
    인자: rules -> raw exchangeInfo와 public relevant projection을 함께 보존한 symbol rules
    반환값: asset scope가 비어 있는 exact AccountRelevantFilters shape dictionary
    작성 날짜: 2026/08/31
    """
    if type(rules) is not SymbolTradingRules:
        raise TypeError("rules must be an exact SymbolTradingRules")
    public_filters = rules.public_relevant_filters
    if public_filters is None:
        raise ValueError("public relevant filters must be available")

    # Public exchangeInfo에는 asset scope가 없으므로 empty projection만 trace에 결속한다.
    normalized_filters = _normalize_account_relevant_filters(public_filters)
    if normalized_filters["asset_filters"]:
        raise ValueError("public relevant filters must not contain asset filters")

    return normalized_filters  # Raw exchangeInfo 대신 runtime overlap에 사용한 typed union만 남긴다.


def _normalize_reference_price(
    reference_price: ReferencePrice,
) -> dict[str, object]:
    """
    함수 이름: _normalize_reference_price()
    기능: public referencePrice value object를 MARKET notional 검증용 exact trace object로 변환한다.
    인자: reference_price -> APIGateway가 엄격 해석한 public reference price
    반환값: symbol·Decimal price·exchange timestamp dictionary
    작성 날짜: 2026/08/31
    """
    if type(reference_price) is not ReferencePrice:
        raise TypeError("reference_price must be an exact ReferencePrice")

    # Local observed_at은 caller가 별도 보존하므로 official 응답 사실만 이 object에 남긴다.
    return {
        "symbol": reference_price.symbol,
        "price": _decimal_to_wire(reference_price.price),
        "exchange_timestamp": reference_price.exchange_timestamp,
    }  # Raw response 없이 evaluator가 사용한 exact public 값만 보존한다.


def _normalize_submit_time_filter_evidence(
    evidence: OrderPreparationFilterEvidence,
    *,
    sequence: int,
) -> dict[str, object]:
    """
    함수 이름: _normalize_submit_time_filter_evidence()
    기능: production prepare가 보존한 filter provenance를 주문 identity와 exact trace entry로 만든다.
    인자: evidence -> public read-only prepare filter evidence
        sequence -> matching order attempt와 같은 1-based 순서
    반환값: credential과 raw payload가 없는 submit-time filter evidence dictionary
    작성 날짜: 2026/08/31
    """
    if type(evidence) is not OrderPreparationFilterEvidence:
        raise TypeError("evidence must be an exact OrderPreparationFilterEvidence")
    if type(sequence) is not int or sequence < 1:
        raise ValueError("sequence must be a positive integer")
    if (
        not evidence.account_open_state_verified_empty
        or evidence.account_open_orders_observed_at is None
        or evidence.account_open_order_lists_observed_at is None
    ):
        raise ValueError(
            "submit-time account open-state evidence must be complete"
        )

    # Side·correlation identity와 두 empty observation은 pending UPSERT/attempt 결속을 위해 원 DTO를 보존한다.
    return {
        "sequence": sequence,
        "intent_id": evidence.intent_id,
        "client_order_id": evidence.client_order_id,
        "side": evidence.side.value,
        "observed_at": _datetime_to_wire(evidence.observed_at),
        "rules": _normalize_symbol_filter_rules(evidence.rules),
        "account_asset_filters": _normalize_account_asset_filters(
            evidence.account_asset_filters
        ),
        "account_relevant_filters": _normalize_account_relevant_filters(
            evidence.account_filters
        ),
        "public_relevant_filters": _normalize_public_relevant_filters(
            evidence.rules
        ),
        "account_filters_observed_at": _datetime_to_wire(
            evidence.account_filters_observed_at
        ),
        "account_open_orders_observed_at": _datetime_to_wire(
            evidence.account_open_orders_observed_at
        ),
        "account_open_orders_verified_empty": (
            evidence.account_open_state_verified_empty
        ),
        "account_open_order_lists_observed_at": _datetime_to_wire(
            evidence.account_open_order_lists_observed_at
        ),
        "account_open_order_lists_verified_empty": (
            evidence.account_open_state_verified_empty
        ),
        "reference_price": _normalize_reference_price(
            evidence.reference_price
        ),
        "reference_price_observed_at": _datetime_to_wire(
            evidence.reference_price_observed_at
        ),
    }


def _create_preflight_evidence(
    *,
    account_version: int,
    verified_at: datetime,
    commission_policy: CommissionDiscountPolicy,
    symbol_rules: SymbolTradingRules,
    filters_observed_at: datetime,
    account_filters: AccountRelevantFilters,
    account_filters_observed_at: datetime,
    account_open_orders_observed_at: datetime,
    account_open_order_lists_observed_at: datetime,
    reference_price: ReferencePrice,
    reference_price_observed_at: datetime,
) -> dict[str, object]:
    """
    함수 이름: _create_preflight_evidence()
    기능: actual mutation 전 READY stream·zero state·commission·fresh filter 사실을 exact object로 고정한다.
    인자: account_version -> startup account snapshot version
        verified_at -> zero-state 교차 검증 완료 UTC 시각
        commission_policy -> signed read-only commission 정책
        symbol_rules -> fresh public exchangeInfo rule
        filters_observed_at -> exchangeInfo 응답 관찰 UTC 시각
        account_filters -> fresh signed myFilters의 full composite DTO
        account_filters_observed_at -> signed myFilters 응답 관찰 UTC 시각
        account_open_orders_observed_at -> all-symbol openOrders empty 확인 UTC 시각
        account_open_order_lists_observed_at -> all-symbol openOrderList empty 확인 UTC 시각
        reference_price -> fresh public MARKET notional reference price
        reference_price_observed_at -> referencePrice 응답 관찰 UTC 시각
    반환값: Phase 13 exact preflight dictionary
    작성 날짜: 2026/08/31
    """
    if type(account_version) is not int or account_version < 1:
        raise ValueError("account_version must be a positive integer")

    # Startup account parser가 official canTrade=true를 이미 강제하므로 READY 성공을 True claim으로 보존한다.
    return {
        "endpoint_set": "BINANCE_SPOT_TESTNET",
        "symbol": "ETHUSDT",
        "can_trade": True,
        "account_stream_status": "READY",
        "market_stream_status": "READY",
        "account_version": account_version,
        "verified_at": _datetime_to_wire(verified_at),
        "commission_policy": _normalize_commission_policy(
            commission_policy
        ),
        "fresh_filters": _normalize_fresh_filters(
            symbol_rules,
            observed_at=filters_observed_at,
        ),
        "account_asset_filters": _normalize_account_asset_filters(
            account_filters.asset_filters
        ),
        "account_relevant_filters": _normalize_account_relevant_filters(
            account_filters
        ),
        "public_relevant_filters": _normalize_public_relevant_filters(
            symbol_rules
        ),
        "account_filters_observed_at": _datetime_to_wire(
            account_filters_observed_at
        ),
        "account_open_orders_observed_at": _datetime_to_wire(
            account_open_orders_observed_at
        ),
        "account_open_orders_verified_empty": True,
        "account_open_order_lists_observed_at": _datetime_to_wire(
            account_open_order_lists_observed_at
        ),
        "account_open_order_lists_verified_empty": True,
        "reference_price": _normalize_reference_price(reference_price),
        "reference_price_observed_at": _datetime_to_wire(
            reference_price_observed_at
        ),
        "position_quantity": "0",
        "pending_order_count": 0,
        "unknown_order_count": 0,
        "matching_open_order_count": 0,
    }


def _normalize_run_trade(trade: Trade) -> dict[str, object]:
    """
    함수 이름: _normalize_run_trade()
    기능: 이번 run의 durable Trade를 fill·Performance 교차 검증용 exact trace object로 변환한다.
    인자: trade -> JSONL 복원을 통과한 canonical Trade
    반환값: raw order payload가 없는 normalized Trade dictionary
    작성 날짜: 2026/08/31
    """
    if not isinstance(trade, Trade):
        raise TypeError("trade must be a Trade")

    # BUY의 realized/exit null과 SELL의 typed 값을 임의 default 없이 그대로 직렬화한다.
    return {
        "trade_id": trade.trade_id,
        "client_order_id": trade.client_order_id,
        "exchange_order_id": trade.order_id,
        "symbol": trade.symbol,
        "side": trade.side.value,
        "regime": trade.regime_type.value,
        "strategy": trade.strategy.value,
        "executed_quantity": _decimal_to_wire(trade.executed_quantity),
        "executed_amount": _decimal_to_wire(trade.executed_amount),
        "average_fill_price": _decimal_to_wire(trade.average_fill_price),
        "fee_amount": _decimal_to_wire(trade.fee_amount),
        "fee_asset": trade.fee_asset,
        "fee_quote_amount": _decimal_to_wire(trade.fee_quote_amount),
        "realized_profit_loss": (
            None
            if trade.realized_pnl is None
            else _decimal_to_wire(trade.realized_pnl)
        ),
        "exit_reason": (
            None if trade.exit_reason is None else trade.exit_reason.value
        ),
        "executed_at": _datetime_to_wire(trade.executed_at),
    }


def _create_performance_evidence(
    *,
    baseline_trades: Sequence[Trade],
    total_trades: Sequence[Trade],
    performance: Performance,
    clock_time: datetime,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """
    함수 이름: _create_performance_evidence()
    기능: immutable baseline, run Trade 차분과 fresh Performance 총계를 exact 산술로 교차 검증한다.
    인자: baseline_trades -> run 전 복사한 closed history
        total_trades -> fresh runtime이 복원한 baseline+run history
        performance -> 같은 fresh runtime의 authoritative Performance
        clock_time -> baseline Performance의 KST date 계산에 사용할 UTC 시각
    반환값: normalized run Trade list와 baseline/run/total Performance mapping
    작성 날짜: 2026/08/31
    """
    baseline_values = tuple(baseline_trades)
    total_values = tuple(total_trades)
    if any(not isinstance(trade, Trade) for trade in baseline_values + total_values):
        raise TypeError("trade sequences must contain only Trade values")
    if total_values[: len(baseline_values)] != baseline_values:
        raise AssertionError("fresh history does not preserve the exact baseline prefix")
    if not isinstance(performance, Performance):
        raise TypeError("performance must be a Performance")

    # Run totals은 baseline suffix durable Trade에서 다시 계산하고 fresh aggregate와 exact 대조한다.
    run_values = total_values[len(baseline_values) :]
    baseline_performance = Performance(
        baseline_values,
        clock=lambda: clock_time,
    )
    with localcontext() as decimal_context:
        decimal_context.prec = 34
        run_realized = sum(
            (
                trade.realized_pnl
                for trade in run_values
                if trade.realized_pnl is not None
            ),
            Decimal("0"),
        )
        run_fee = sum(
            (trade.fee_quote_amount for trade in run_values),
            Decimal("0"),
        )
        expected_total_realized = baseline_performance.realized_pnl + run_realized
        expected_total_fee = baseline_performance.total_fee + run_fee
    if (
        performance.realized_pnl != expected_total_realized
        or performance.total_fee != expected_total_fee
    ):
        raise AssertionError("fresh Performance does not match baseline plus run Trades")

    return (
        [_normalize_run_trade(trade) for trade in run_values],
        {
            "baseline_trade_count": len(baseline_values),
            "run_trade_count": len(run_values),
            "total_trade_count": len(total_values),
            "baseline_realized_profit_loss": _decimal_to_wire(
                baseline_performance.realized_pnl
            ),
            "run_realized_profit_loss": _decimal_to_wire(run_realized),
            "total_realized_profit_loss": _decimal_to_wire(
                performance.realized_pnl
            ),
            "baseline_fee_quote": _decimal_to_wire(
                baseline_performance.total_fee
            ),
            "run_fee_quote": _decimal_to_wire(run_fee),
            "total_fee_quote": _decimal_to_wire(performance.total_fee),
        },
    )


def _create_non_mutating_trace_body(
    *,
    outcome: str,
    typed_reason: str | None,
    run_id: str,
    started_at: datetime,
    completed_at: datetime,
    preflight: Mapping[str, object],
    market_events: list[dict[str, object]],
    account_events: list[dict[str, object]],
    transport_events: Mapping[str, object],
    baseline_history_sha256: str,
    baseline_trades: Sequence[Trade],
    total_trades: Sequence[Trade],
    performance: Performance,
    final_verified_at: datetime,
    fresh_runtime_session_id: str,
) -> dict[str, object]:
    """
    함수 이름: _create_non_mutating_trace_body()
    기능: actual order가 0인 NO_SIGNAL run을 baseline·fresh state와 결속한다.
    인자: outcome -> exact NO_SIGNAL
        typed_reason -> exact None
        run_id -> UUIDv4 실행 identity
        started_at -> 실제 관찰 시작 UTC 시각
        completed_at -> 관찰·fresh 검증 완료 UTC 시각
        preflight -> mutation 전 zero-state·filter 증거
        market_events -> 관찰한 public Kline event
        account_events -> 관찰한 public account event
        transport_events -> normalized UI publication batch
        baseline_history_sha256 -> run 전 history bytes SHA-256
        baseline_trades -> run 전 immutable Trade sequence
        total_trades -> fresh runtime의 전체 durable Trade sequence
        performance -> 같은 fresh runtime의 전체 Performance
        final_verified_at -> fresh zero-state 교차 검증 시각
        fresh_runtime_session_id -> 주문 권한 없는 재시작 session UUID
    반환값: trace_sha256를 제외한 exact trace body
    작성 날짜: 2026/08/31
    """
    if outcome != "NO_SIGNAL" or typed_reason is not None:
        raise ValueError("v3 non-mutating trace must be exact NO_SIGNAL")
    run_trades, performance_evidence = _create_performance_evidence(
        baseline_trades=baseline_trades,
        total_trades=total_trades,
        performance=performance,
        clock_time=final_verified_at,
    )
    if run_trades:
        raise AssertionError("non-mutating trace must not contain run Trades")

    # Mutation 관련 배열과 recovery identity를 exact empty/None으로 고정해 성공 주문을 합성하지 못하게 한다.
    return {
        "schema_version": PHASE13_PUBLIC_TRACE_SCHEMA_VERSION,
        "record_type": PHASE13_PUBLIC_TRACE_RECORD_TYPE,
        "outcome": outcome,
        "typed_reason": typed_reason,
        "run_id": run_id,
        "timestamps": {
            "started_at": _datetime_to_wire(started_at),
            "completed_at": _datetime_to_wire(completed_at),
        },
        "preflight": dict(preflight),
        "immutable_decision_fingerprint": None,
        "public_market_events": market_events,
        "public_account_events": account_events,
        "order_attempts": [],
        "order_execution_traces": [],
        "submit_time_filter_evidence": [],
        "order_results": [],
        "baseline_history_sha256": baseline_history_sha256,
        "baseline_history_count": len(tuple(baseline_trades)),
        "run_durable_trades": [],
        "transport_ui_event_batch": dict(transport_events),
        "recovery": {
            "required": False,
            "attempted": False,
            "outcome": "NOT_REQUIRED",
            "intent_id": None,
            "client_order_id": None,
            "authoritative_position_quantity": "0",
            "effective_free_quantity": "0",
            "submitted_quantity": None,
            "final_position_quantity": "0",
            "pending_order_count": 0,
            "matching_open_order_count": 0,
            "duplicate_order_count": 0,
            "duplicate_trade_count": 0,
        },
        "final_state": {
            "position_quantity": "0",
            "pending_order_count": 0,
            "unknown_order_count": 0,
            "matching_open_order_count": 0,
            "actual_order_count": 0,
            "duplicate_order_count": 0,
            "duplicate_trade_count": 0,
            "verified_at": _datetime_to_wire(final_verified_at),
            "fresh_runtime_session_id": fresh_runtime_session_id,
            "performance": performance_evidence,
        },
    }


def _create_actual_testnet_environment(
    configuration: TestnetConfiguration,
    maximum_notional: Decimal,
) -> dict[str, str]:
    """
    함수 이름: _create_actual_testnet_environment()
    기능: 검증된 configuration을 세 opt-in과 cap이 명시된 actual target 전용 mapping으로 격리한다.
    인자: configuration -> 실제 process 환경에서 읽고 검증한 Testnet configuration
        maximum_notional -> public Case 2 permission gate가 반환한 absolute-ceiling 이내 cap
    반환값: actual runtime factory 하나에만 전달할 최소 환경 mapping
    작성 날짜: 2026/08/31
    """
    if not isinstance(configuration, TestnetConfiguration):
        raise TypeError("configuration must be a TestnetConfiguration")
    if not isinstance(maximum_notional, Decimal) or not maximum_notional.is_finite():
        raise TypeError("maximum_notional must be a finite Decimal")
    if maximum_notional <= Decimal("0") or maximum_notional > Decimal("100"):
        raise ValueError("maximum_notional must be greater than 0 and no greater than 100")
    if (
        not configuration.allow_testnet_orders
        or not configuration.allow_phase13_public_case2
        or configuration.max_notional != maximum_notional
    ):
        raise ValueError("configuration does not carry the exact public Case 2 permission")

    # Mapping 밖의 hostile base URL/live 값은 전달하지 않고 세 권한을 exact `1`로 다시 고정한다.
    return {
        BINANCE_RUN_TESTNET_ENV: "1",
        BINANCE_TESTNET_API_KEY_ENV: configuration.api_key,
        BINANCE_TESTNET_API_SECRET_ENV: configuration.api_secret,
        BINANCE_RUN_TESTNET_ORDERS_ENV: "1",
        BINANCE_RUN_PHASE13_PUBLIC_CASE2_ENV: "1",
        BINANCE_TESTNET_MAX_NOTIONAL_ENV: _decimal_to_wire(maximum_notional),
    }


def _create_read_only_testnet_environment(
    configuration: TestnetConfiguration,
) -> dict[str, str]:
    """
    함수 이름: _create_read_only_testnet_environment()
    기능: fresh restart가 order permission, public opt-in과 cap을 상속하지 않는 최소 mapping을 만든다.
    인자: configuration -> 같은 Testnet credential을 보존한 검증 configuration
    반환값: 주문 mutation이 불가능한 read-only Testnet 환경 mapping
    작성 날짜: 2026/08/31
    """
    if not isinstance(configuration, TestnetConfiguration):
        raise TypeError("configuration must be a TestnetConfiguration")

    return {
        BINANCE_RUN_TESTNET_ENV: "1",
        BINANCE_TESTNET_API_KEY_ENV: configuration.api_key,
        BINANCE_TESTNET_API_SECRET_ENV: configuration.api_secret,
        BINANCE_RUN_TESTNET_ORDERS_ENV: "0",
        BINANCE_RUN_PHASE13_PUBLIC_CASE2_ENV: "0",
    }  # Cap key 자체를 제거해 read-only client가 latent order 설정을 소유하지 않게 한다.


def _create_actual_risk_policy(maximum_notional: Decimal) -> RiskPolicy:
    """
    함수 이름: _create_actual_risk_policy()
    기능: 별도 Testnet outer cap을 검증하되 사용자 확정 unbounded Phase 13 위험 정책을 만든다.
    인자: maximum_notional -> bootstrap permission layer가 별도로 강제할 Testnet execution cap
    반환값: versioned production RiskPolicy
    작성 날짜: 2026/08/31
    """
    if not isinstance(maximum_notional, Decimal) or not maximum_notional.is_finite():
        raise TypeError("maximum_notional must be a finite Decimal")
    if maximum_notional <= Decimal("0") or maximum_notional > Decimal("100"):
        raise ValueError("maximum_notional must be within the approved ceiling")

    # 세 가지 사용자 policy cap은 None을 보존하고 Session 3의 10 USDT 상한은 bootstrap/permission 경계만 소유한다.
    return RiskPolicy(
        version=13,
        max_order_notional=None,
        max_position_notional=None,
        max_daily_loss=None,
        daily_loss_scope=DailyLossScope.REALIZED_ONLY,
        manual_kill_behavior=ManualKillBehavior.CANCEL_AND_LIQUIDATE,
    )


def _canonical_failure_evidence_bytes(value: Mapping[str, object]) -> bytes:
    """
    함수 이름: _canonical_failure_evidence_bytes()
    기능: secret 검사를 마친 failure evidence를 deterministic UTF-8 JSON과 단일 newline으로 만든다.
    인자: value -> JSON-safe exact failure evidence mapping
    반환값: key 정렬과 compact separator가 적용된 canonical bytes
    작성 날짜: 2026/08/31
    """
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _classify_actual_failure(error: Exception) -> str:
    """
    함수 이름: _classify_actual_failure()
    기능: 원 예외 message를 artifact에 복사하지 않고 stable secret-free failure category로 축약한다.
    인자: error -> actual orchestration이 포착한 정상 Exception
    반환값: exact uppercase typed reason
    작성 날짜: 2026/08/31
    """
    if isinstance(error, TimeoutError):
        return "ACTUAL_TIMEOUT"
    if isinstance(error, AssertionError):
        return "ACTUAL_ASSERTION_FAILED"
    if isinstance(error, PermissionError):
        return "ACTUAL_PERMISSION_FAILED"
    if isinstance(error, OSError):
        return "ACTUAL_IO_FAILED"
    if isinstance(error, (TypeError, ValueError)):
        return "ACTUAL_VALIDATION_FAILED"
    if isinstance(error, RuntimeError):
        return "ACTUAL_RUNTIME_FAILED"

    return "ACTUAL_UNEXPECTED_FAILURE"  # 사용자 정의 예외의 class/message를 evidence에 복사하지 않는다.


def _classify_failure_recovery_error(error: Exception) -> str:
    """
    함수 이름: _classify_failure_recovery_error()
    기능: known-safe STOP recovery 실패를 원문 없이 안정적인 evidence token으로 축약한다.
    인자: error -> recovery orchestration이 포착한 정상 Exception
    반환값: exact uppercase typed recovery reason
    작성 날짜: 2026/08/31
    """
    if isinstance(error, TimeoutError):
        return "FAILURE_RECOVERY_TIMEOUT"
    if isinstance(error, AssertionError):
        return "FAILURE_RECOVERY_ASSERTION_FAILED"
    if isinstance(error, PermissionError):
        return "FAILURE_RECOVERY_PERMISSION_FAILED"
    if isinstance(error, OSError):
        return "FAILURE_RECOVERY_IO_FAILED"
    if isinstance(error, (TypeError, ValueError)):
        return "FAILURE_RECOVERY_VALIDATION_FAILED"
    if isinstance(error, RuntimeError):
        return "FAILURE_RECOVERY_RUNTIME_FAILED"

    return "FAILURE_RECOVERY_UNEXPECTED_FAILURE"  # 원 exception class와 message는 artifact에 넣지 않는다.


def _normalize_failure_first_cause(
    cause_snapshot: ReconciliationCauseSnapshot,
    evidence_errors: list[str],
) -> dict[str, object]:
    """
    함수 이름: _normalize_failure_first_cause()
    기능: Controller의 frozen cause snapshot을 v2 exact mapping과 secondary error로 변환한다.
    인자: cause_snapshot -> failure 감지 시 application lock에서 읽은 원자 snapshot
        evidence_errors -> missing/duplicate/conflict code를 추가할 mutable 목록
    반환값: raw reason 없는 first_cause mapping
    작성 날짜: 2026/08/31
    """
    if type(cause_snapshot) is not ReconciliationCauseSnapshot:
        evidence_errors.append("RECONCILIATION_CAUSE_MISSING")
        return {
            "reconciliation_required": True,
            "status": ReconciliationCauseStatus.MISSING.value,
            "category": None,
        }  # Unknown object는 category를 추측하지 않고 conservative reconciliation으로 봉인한다.

    cause_status = cause_snapshot.status
    cause_category = cause_snapshot.category
    if cause_status is ReconciliationCauseStatus.EXACT:
        if not isinstance(cause_category, ReconciliationCauseCategory):
            evidence_errors.append("RECONCILIATION_CAUSE_MISSING")
            return {
                "reconciliation_required": cause_snapshot.reconciliation_required,
                "status": ReconciliationCauseStatus.MISSING.value,
                "category": None,
            }
        normalized_category: str | None = cause_category.value
    else:
        secondary_error = {
            ReconciliationCauseStatus.MISSING: "RECONCILIATION_CAUSE_MISSING",
            ReconciliationCauseStatus.DUPLICATE: (
                "RECONCILIATION_CAUSE_DUPLICATE"
            ),
            ReconciliationCauseStatus.CONFLICT: (
                "RECONCILIATION_CAUSE_CONFLICT"
            ),
        }.get(cause_status)
        if secondary_error is None:
            secondary_error = "RECONCILIATION_CAUSE_MISSING"
            cause_status = ReconciliationCauseStatus.MISSING
        evidence_errors.append(secondary_error)
        normalized_category = None  # 불확정 latch는 최초 후보 category도 artifact에 노출하지 않는다.

    return {
        "reconciliation_required": cause_snapshot.reconciliation_required,
        "status": cause_status.value,
        "category": normalized_category,
    }


def _create_incomplete_fresh_verification() -> dict[str, object]:
    """
    함수 이름: _create_incomplete_fresh_verification()
    기능: 관찰 전 값을 추측한 zero로 채우지 않는 v2 fresh failure mapping을 만든다.
    인자: 없음
    반환값: stage와 모든 선택 truth가 아직 None인 INCOMPLETE mapping
    작성 날짜: 2026/08/31
    """
    return {
        "status": "INCOMPLETE",
        "typed_reason": "FRESH_VERIFICATION_FAILED",
        "failure_stage": None,
        "verified_at": None,
        "position_quantity": None,
        "pending_order_count": None,
        "reconciliation_required": None,
        "matching_open_order_count": None,
        "account_open_orders_empty": None,
        "account_open_order_lists_empty": None,
        "run_exchange_order_count": None,
        "durable_trade_count": None,
    }


def _set_fresh_verification_failure(
    fresh_verification: dict[str, object],
    failure_stage: str,
    typed_reason: str,
    evidence_errors: list[str],
) -> None:
    """
    함수 이름: _set_fresh_verification_failure()
    기능: 최초 fresh failure stage를 고정하고 후속 cleanup 오류는 stage를 덮지 않게 한다.
    인자: fresh_verification -> 갱신할 v2 partial truth mapping
        failure_stage -> 실패 operation 직전에 고정한 stable stage
        typed_reason -> raw exception을 포함하지 않는 exact reason
        evidence_errors -> secondary evidence error를 추가할 mutable 목록
    반환값: 없음
    작성 날짜: 2026/08/31
    """
    if failure_stage not in _FAILURE_FRESH_VERIFICATION_STAGES:
        raise ValueError("failure_stage must be supported")
    if typed_reason not in _FAILURE_FRESH_STAGE_ALLOWED_REASONS[failure_stage]:
        raise ValueError("typed_reason must match failure_stage")

    # 첫 실패가 이미 있으면 cleanup reason만 누적하고 인과 stage는 바꾸지 않는다.
    if fresh_verification["failure_stage"] is None:
        fresh_verification["failure_stage"] = failure_stage
        fresh_verification["typed_reason"] = typed_reason
    if typed_reason not in evidence_errors:
        evidence_errors.append(typed_reason)
    fresh_verification["status"] = "INCOMPLETE"


@dataclass(frozen=True, slots=True)
class _FailureDurabilityFileFingerprint:
    """
    클래스 이름: _FailureDurabilityFileFingerprint
    기능: durable leaf의 identity·owner·mode·link·content·change time을 불변 비교 값으로 보존한다.
    작성 날짜: 2026/09/01
    """

    exists: bool
    device: int | None
    inode: int | None
    mode: int | None
    user_id: int | None
    group_id: int | None
    link_count: int | None
    size: int | None
    change_time_ns: int | None
    modification_time_ns: int | None
    sha256: str | None


_MISSING_FAILURE_DURABILITY_FINGERPRINT = (
    _FailureDurabilityFileFingerprint(
        exists=False,
        device=None,
        inode=None,
        mode=None,
        user_id=None,
        group_id=None,
        link_count=None,
        size=None,
        change_time_ns=None,
        modification_time_ns=None,
        sha256=None,
    )
)


@dataclass(slots=True)
class _FailureDurabilitySnapshotHandle:
    """
    클래스 이름: _FailureDurabilitySnapshotHandle
    기능: source와 isolated directory descriptor를 fresh runtime 종료까지 고정하고 ABA를 검증한다.
    작성 날짜: 2026/09/01
    """

    history_path: Path
    source_directory_path: Path
    source_directory_descriptor: int
    source_directory_identity: tuple[int, int, int, int]
    source_directory_change_time_ns: int
    source_ancestor_descriptors: tuple[int, ...]
    source_ancestor_identities: tuple[tuple[int, int, int], ...]
    directory_descriptor: int
    directory_identity: tuple[int, int, int, int]
    directory_change_time_ns: int
    ancestor_descriptors: tuple[int, ...]
    ancestor_identities: tuple[tuple[int, int, int], ...]
    closed: bool = False

    def verify(self) -> None:
        """
        함수 이름: verify()
        기능: source/copy pinned directory와 공개 path의 identity·change time을 함께 확인한다.
        인자: 없음
        반환값: directory가 같은 inode와 change time이면 없음
        작성 날짜: 2026/09/01
        """
        if self.closed:
            raise RuntimeError("durability snapshot handle is closed")
        _verify_failure_durability_ancestor_chain(
            self.source_ancestor_descriptors,
            self.source_ancestor_identities,
        )
        _verify_failure_durability_directory_identity(
            self.source_directory_path,
            self.source_directory_descriptor,
            self.source_directory_identity,
            expected_change_time_ns=self.source_directory_change_time_ns,
        )
        _verify_failure_durability_ancestor_chain(
            self.ancestor_descriptors,
            self.ancestor_identities,
        )
        _verify_failure_durability_directory_identity(
            self.history_path.parent,
            self.directory_descriptor,
            self.directory_identity,
            expected_change_time_ns=self.directory_change_time_ns,
        )

    def close(self) -> None:
        """
        함수 이름: close()
        기능: fresh runtime 검증이 끝난 source/copy directory descriptor를 멱등 해제한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/01
        """
        if self.closed:
            return
        close_error: OSError | None = None
        try:
            _close_failure_durability_directory_chain(
                self.ancestor_descriptors
            )
        except OSError as error:
            close_error = error
        finally:
            try:
                _close_failure_durability_directory_chain(
                    self.source_ancestor_descriptors
                )
            except OSError as error:
                if close_error is None:
                    close_error = error
            finally:
                self.closed = True  # TemporaryDirectory cleanup 전에 두 descriptor chain을 정확히 한 번 해제한다.
        if close_error is not None:
            raise close_error


def _failure_durability_state_identity(
    file_state: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int, int]:
    """
    함수 이름: _failure_durability_state_identity()
    기능: lstat·open·fstat 경계에서 바뀐 inode와 metadata를 검사할 exact tuple을 만든다.
    인자: file_state -> 비교할 stat_result
    반환값: device, inode, mode, uid, gid, nlink, size, ctime, mtime tuple
    작성 날짜: 2026/09/01
    """
    return (
        file_state.st_dev,
        file_state.st_ino,
        stat.S_IMODE(file_state.st_mode),
        file_state.st_uid,
        file_state.st_gid,
        file_state.st_nlink,
        file_state.st_size,
        file_state.st_ctime_ns,
        file_state.st_mtime_ns,
    )


def _require_failure_durability_file_state(
    file_state: os.stat_result,
) -> None:
    """
    함수 이름: _require_failure_durability_file_state()
    기능: source와 copy leaf를 현재 user의 mode 0600 단일-link regular file로 제한한다.
    인자: file_state -> descriptor 또는 no-follow path의 stat_result
    반환값: 안전한 leaf이면 없음
    작성 날짜: 2026/08/31
    """
    if (
        not stat.S_ISREG(file_state.st_mode)
        or file_state.st_uid != os.geteuid()
        or stat.S_IMODE(file_state.st_mode) != 0o600
        or file_state.st_nlink != 1
    ):
        raise PermissionError(
            "durability leaf must be an owner-only singly linked regular file"
        )


def _close_failure_durability_directory_chain(
    directory_descriptors: tuple[int, ...],
) -> None:
    """
    함수 이름: _close_failure_durability_directory_chain()
    기능: root부터 leaf까지 고정한 directory descriptor를 역순으로 모두 닫는다.
    인자: directory_descriptors -> 소유 중인 descriptor tuple
    반환값: 모두 닫히면 없음
    작성 날짜: 2026/09/01
    """
    first_error: OSError | None = None
    for directory_descriptor in reversed(directory_descriptors):
        try:
            os.close(directory_descriptor)
        except OSError as error:
            if first_error is None:
                first_error = error  # 한 close가 실패해도 나머지 ancestor FD를 누수시키지 않는다.
    if first_error is not None:
        raise first_error


def _verify_failure_durability_ancestor_chain(
    directory_descriptors: tuple[int, ...],
    expected_identities: tuple[tuple[int, int, int], ...],
) -> None:
    """
    함수 이름: _verify_failure_durability_ancestor_chain()
    기능: pinned absolute directory chain의 device·inode·ctime을 root부터 순서대로 재검증한다.
    인자: directory_descriptors -> root부터 leaf까지 열린 descriptor tuple
        expected_identities -> 각 descriptor의 device, inode, ctime tuple
    반환값: 전체 chain이 처음과 같으면 없음
    작성 날짜: 2026/09/01
    """
    if (
        not directory_descriptors
        or len(directory_descriptors) != len(expected_identities)
    ):
        raise RuntimeError("durability ancestor chain is incomplete")
    for directory_descriptor, expected_identity in zip(
        directory_descriptors,
        expected_identities,
        strict=True,
    ):
        directory_state = os.fstat(directory_descriptor)
        observed_identity = (
            directory_state.st_dev,
            directory_state.st_ino,
            directory_state.st_ctime_ns,
        )
        if (
            not stat.S_ISDIR(directory_state.st_mode)
            or observed_identity != expected_identity
        ):
            raise RuntimeError("durability ancestor directory identity changed")


def _open_failure_durability_directory_chain(
    directory_path: Path,
    *,
    require_private_mode: bool,
) -> tuple[
    tuple[int, ...],
    tuple[tuple[int, int, int], ...],
    tuple[int, int, int, int],
]:
    """
    함수 이름: _open_failure_durability_directory_chain()
    기능: 절대 root부터 source/copy leaf directory까지 모든 no-follow descriptor를 고정한다.
    인자: directory_path -> 열 directory path
        require_private_mode -> mode 0700을 요구할지 여부
    반환값: descriptor chain, 각 device/inode/ctime, final device/inode/uid/mode tuple
    작성 날짜: 2026/09/01
    """
    if not isinstance(directory_path, Path):
        raise TypeError("directory_path must be a Path")
    no_follow_flag = getattr(os, "O_NOFOLLOW", None)
    if no_follow_flag is None:
        raise RuntimeError("durability access requires O_NOFOLLOW")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0) | no_follow_flag
    normalized_path = Path(os.path.abspath(directory_path))
    if (
        normalized_path.parts[:2] == (os.sep, "var")
        and os.path.islink("/var")
        and os.readlink("/var") == "private/var"
    ):
        normalized_path = Path("/private/var").joinpath(
            *normalized_path.parts[2:]
        )  # macOS의 고정 root alias만 lexical canonical path로 바꾸고 임의 symlink는 허용하지 않는다.
    if (
        normalized_path.parts[:2] == (os.sep, "tmp")
        and os.path.islink("/tmp")
        and os.readlink("/tmp") == "private/tmp"
    ):
        # 최소 runner 환경에서 tempfile이 선택하는 macOS 고정 /tmp alias도 같은 방식으로 정규화한다.
        normalized_path = Path("/private/tmp").joinpath(
            *normalized_path.parts[2:]
        )
    directory_descriptors: list[int] = []
    directory_identities: list[tuple[int, int, int]] = []
    try:
        root_descriptor = os.open(os.sep, directory_flags)
        directory_descriptors.append(root_descriptor)
        root_state = os.fstat(root_descriptor)
        directory_identities.append(
            (root_state.st_dev, root_state.st_ino, root_state.st_ctime_ns)
        )

        # Root부터 각 component를 dir_fd+O_NOFOLLOW로 열고 ancestor FD를 닫지 않는다.
        for path_component in normalized_path.parts[1:]:
            next_descriptor = os.open(
                path_component,
                directory_flags,
                dir_fd=directory_descriptors[-1],
            )
            directory_descriptors.append(next_descriptor)
            next_state = os.fstat(next_descriptor)
            directory_identities.append(
                (
                    next_state.st_dev,
                    next_state.st_ino,
                    next_state.st_ctime_ns,
                )
            )
    except Exception:
        _close_failure_durability_directory_chain(
            tuple(directory_descriptors)
        )
        raise

    directory_descriptor = directory_descriptors[-1]
    descriptor_state = os.fstat(directory_descriptor)
    descriptor_identity = (
        descriptor_state.st_dev,
        descriptor_state.st_ino,
        descriptor_state.st_uid,
        stat.S_IMODE(descriptor_state.st_mode),
    )
    if (
        not stat.S_ISDIR(descriptor_state.st_mode)
        or descriptor_state.st_uid != os.geteuid()
        or descriptor_state.st_mode & 0o022
        or (
            require_private_mode
            and stat.S_IMODE(descriptor_state.st_mode) != 0o700
        )
    ):
        _close_failure_durability_directory_chain(
            tuple(directory_descriptors)
        )
        raise PermissionError("durability directory identity is unsafe")

    return (
        tuple(directory_descriptors),
        tuple(directory_identities),
        descriptor_identity,
    )


def _open_failure_durability_directory(
    directory_path: Path,
    *,
    require_private_mode: bool,
) -> tuple[int, tuple[int, int, int, int]]:
    """
    함수 이름: _open_failure_durability_directory()
    기능: 짧은 operation용 final directory FD를 no-follow absolute chain으로 안전하게 연다.
    인자: directory_path -> 열 directory path
        require_private_mode -> mode 0700을 요구할지 여부
    반환값: 열린 final descriptor와 device, inode, uid, mode identity tuple
    작성 날짜: 2026/09/01
    """
    (
        directory_descriptors,
        _directory_identities,
        descriptor_identity,
    ) = _open_failure_durability_directory_chain(
        directory_path,
        require_private_mode=require_private_mode,
    )
    directory_descriptor = directory_descriptors[-1]
    try:
        _close_failure_durability_directory_chain(
            directory_descriptors[:-1]
        )
    except OSError:
        os.close(directory_descriptor)
        raise

    return directory_descriptor, descriptor_identity


def _verify_failure_durability_directory_identity(
    directory_path: Path,
    directory_descriptor: int,
    expected_identity: tuple[int, int, int, int],
    *,
    expected_change_time_ns: int | None = None,
) -> None:
    """
    함수 이름: _verify_failure_durability_directory_identity()
    기능: operation 뒤 path와 pinned directory descriptor가 여전히 같은 identity인지 확인한다.
    인자: directory_path -> 재검사할 directory path
        directory_descriptor -> operation 동안 열어 둔 descriptor
        expected_identity -> open 직후 고정한 identity
        expected_change_time_ns -> ABA rename·entry 변경을 탐지할 optional directory ctime
    반환값: 일치하면 없음
    작성 날짜: 2026/08/31
    """
    descriptor_state = os.fstat(directory_descriptor)
    path_descriptor, path_identity = _open_failure_durability_directory(
        directory_path,
        require_private_mode=(expected_identity[3] == 0o700),
    )
    try:
        path_state = os.fstat(path_descriptor)
    finally:
        os.close(path_descriptor)
    descriptor_identity = (
        descriptor_state.st_dev,
        descriptor_state.st_ino,
        descriptor_state.st_uid,
        stat.S_IMODE(descriptor_state.st_mode),
    )
    if (
        not stat.S_ISDIR(descriptor_state.st_mode)
        or not stat.S_ISDIR(path_state.st_mode)
        or descriptor_identity != expected_identity
        or path_identity != expected_identity
        or (
            expected_change_time_ns is not None
            and (
                descriptor_state.st_ctime_ns != expected_change_time_ns
                or path_state.st_ctime_ns != expected_change_time_ns
            )
        )
    ):
        raise RuntimeError("durability directory path identity changed")


def _read_failure_durability_leaf(
    directory_descriptor: int,
    leaf_name: str,
) -> tuple[bytes, _FailureDurabilityFileFingerprint]:
    """
    함수 이름: _read_failure_durability_leaf()
    기능: pinned directory leaf를 no-follow로 읽고 identity·ctime·mtime을 재검사한다.
    인자: directory_descriptor -> pinned parent directory descriptor
        leaf_name -> separator 없는 durability leaf 이름
    반환값: exact bytes와 rich immutable fingerprint tuple
    작성 날짜: 2026/09/01
    """
    if not isinstance(leaf_name, str) or not leaf_name or Path(leaf_name).name != leaf_name:
        raise ValueError("durability leaf_name must be canonical")
    try:
        path_state_before = os.stat(
            leaf_name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        try:
            os.stat(
                leaf_name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return b"", _MISSING_FAILURE_DURABILITY_FINGERPRINT
        raise RuntimeError("durability leaf appeared while checking absence")
    _require_failure_durability_file_state(path_state_before)

    descriptor_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    descriptor_flags |= getattr(os, "O_NOFOLLOW", 0)
    leaf_descriptor = os.open(
        leaf_name,
        descriptor_flags,
        dir_fd=directory_descriptor,
    )
    try:
        descriptor_state_before = os.fstat(leaf_descriptor)
        _require_failure_durability_file_state(descriptor_state_before)
        if _failure_durability_state_identity(
            descriptor_state_before
        ) != _failure_durability_state_identity(path_state_before):
            raise RuntimeError("durability leaf changed between lstat and open")

        # Descriptor read는 short read와 signal interruption을 처리하고 path를 다시 열지 않는다.
        byte_chunks: list[bytes] = []
        while True:
            try:
                byte_chunk = os.read(leaf_descriptor, 1024 * 1024)
            except InterruptedError:
                continue
            if not byte_chunk:
                break
            byte_chunks.append(byte_chunk)
        leaf_bytes = b"".join(byte_chunks)
        descriptor_state_after = os.fstat(leaf_descriptor)
        path_state_after = os.stat(
            leaf_name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        expected_identity = _failure_durability_state_identity(
            descriptor_state_before
        )
        if (
            _failure_durability_state_identity(descriptor_state_after)
            != expected_identity
            or _failure_durability_state_identity(path_state_after)
            != expected_identity
            or len(leaf_bytes) != descriptor_state_before.st_size
        ):
            raise RuntimeError("durability leaf changed while reading")
    finally:
        os.close(leaf_descriptor)

    return leaf_bytes, _FailureDurabilityFileFingerprint(
        exists=True,
        device=descriptor_state_before.st_dev,
        inode=descriptor_state_before.st_ino,
        mode=stat.S_IMODE(descriptor_state_before.st_mode),
        user_id=descriptor_state_before.st_uid,
        group_id=descriptor_state_before.st_gid,
        link_count=descriptor_state_before.st_nlink,
        size=descriptor_state_before.st_size,
        change_time_ns=descriptor_state_before.st_ctime_ns,
        modification_time_ns=descriptor_state_before.st_mtime_ns,
        sha256=hashlib.sha256(leaf_bytes).hexdigest(),
    )


def _capture_failure_durability_fingerprint(
    history_path: Path,
) -> tuple[_FailureDurabilityFileFingerprint, ...]:
    """
    함수 이름: _capture_failure_durability_fingerprint()
    기능: pinned parent의 세 leaf identity·SHA-256·change time을 fail-closed로 읽는다.
    인자: history_path -> actual run 또는 isolated copy의 history JSONL 경로
    반환값: 고정 leaf 순서의 rich immutable fingerprint tuple
    작성 날짜: 2026/09/01
    """
    if not isinstance(history_path, Path):
        raise TypeError("history_path must be a Path")
    directory_descriptor, directory_identity = (
        _open_failure_durability_directory(
            history_path.parent,
            require_private_mode=True,
        )
    )
    try:
        fingerprints = tuple(
            _read_failure_durability_leaf(
                directory_descriptor,
                f"{history_path.name}{file_suffix}",
            )[1]
            for file_suffix in _FAILURE_DURABILITY_FILE_SUFFIXES
        )
        _verify_failure_durability_directory_identity(
            history_path.parent,
            directory_descriptor,
            directory_identity,
        )
    finally:
        os.close(directory_descriptor)

    return fingerprints


def _require_failure_durability_snapshot_equivalence(
    source_fingerprints: tuple[_FailureDurabilityFileFingerprint, ...],
    copy_fingerprints: tuple[_FailureDurabilityFileFingerprint, ...],
) -> None:
    """
    함수 이름: _require_failure_durability_snapshot_equivalence()
    기능: source와 copy의 존재·size·SHA는 같고 모든 존재 leaf inode는 다름을 검증한다.
    인자: source_fingerprints -> copy 직후 재읽은 source tuple
        copy_fingerprints -> runtime 시작 전 isolated copy tuple
    반환값: exact isolated copy이면 없음
    작성 날짜: 2026/08/31
    """
    if (
        not source_fingerprints
        or len(source_fingerprints) != len(copy_fingerprints)
        or len(source_fingerprints) > len(_FAILURE_DURABILITY_FILE_SUFFIXES)
    ):
        raise ValueError("durability fingerprint count is invalid")
    for source_fingerprint, copy_fingerprint in zip(
        source_fingerprints,
        copy_fingerprints,
        strict=True,
    ):
        if source_fingerprint.exists != copy_fingerprint.exists:
            raise RuntimeError("durability copy existence differs from source")
        if not source_fingerprint.exists:
            continue
        if (
            source_fingerprint.size != copy_fingerprint.size
            or source_fingerprint.sha256 != copy_fingerprint.sha256
            or (
                source_fingerprint.device,
                source_fingerprint.inode,
            )
            == (
                copy_fingerprint.device,
                copy_fingerprint.inode,
            )
        ):
            raise RuntimeError("durability copy is not an exact distinct inode")


def _copy_failure_durability_snapshot(
    source_history_path: Path,
    snapshot_directory: Path,
) -> _FailureDurabilitySnapshotHandle:
    """
    함수 이름: _copy_failure_durability_snapshot()
    기능: 세 durable leaf를 private inode로 fsync하고 source/copy parent descriptor를 넘긴다.
    인자: source_history_path -> primary runtime이 닫힌 뒤의 authoritative history 경로
        snapshot_directory -> fresh runtime만 사용할 mode 0700 임시 directory
    반환값: fresh runtime 종료까지 directory FD를 소유할 snapshot handle
    작성 날짜: 2026/09/01
    """
    if not isinstance(source_history_path, Path):
        raise TypeError("source_history_path must be a Path")
    if not isinstance(snapshot_directory, Path):
        raise TypeError("snapshot_directory must be a Path")
    (
        source_ancestor_descriptors,
        source_ancestor_identities,
        source_directory_identity,
    ) = _open_failure_durability_directory_chain(
        source_history_path.parent,
        require_private_mode=True,
    )
    source_descriptor = source_ancestor_descriptors[-1]
    destination_ancestor_descriptors: tuple[int, ...] | None = None
    snapshot_handle: _FailureDurabilitySnapshotHandle | None = None
    try:
        source_change_time_ns = os.fstat(source_descriptor).st_ctime_ns
        (
            destination_ancestor_descriptors,
            destination_ancestor_identities,
            destination_directory_identity,
        ) = _open_failure_durability_directory_chain(
            snapshot_directory,
            require_private_mode=True,
        )
        destination_descriptor = destination_ancestor_descriptors[-1]
        if source_directory_identity[:2] == destination_directory_identity[:2]:
            raise RuntimeError("durability source and copy directories must differ")
        isolated_history_path = snapshot_directory / source_history_path.name
        destination_open_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        destination_open_flags |= getattr(os, "O_CLOEXEC", 0)
        destination_open_flags |= getattr(os, "O_NOFOLLOW", 0)

        # 각 source leaf를 descriptor로 읽은 뒤 O_EXCL destination에 쓰고 두 path identity를 즉시 재검사한다.
        for file_suffix in _FAILURE_DURABILITY_FILE_SUFFIXES:
            leaf_name = f"{source_history_path.name}{file_suffix}"
            source_bytes, source_fingerprint = (
                _read_failure_durability_leaf(
                    source_descriptor,
                    leaf_name,
                )
            )
            if not source_fingerprint.exists:
                try:
                    os.stat(
                        leaf_name,
                        dir_fd=destination_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    continue
                raise FileExistsError("absent source leaf exists in copy")
            copied_descriptor = os.open(
                leaf_name,
                destination_open_flags,
                0o600,
                dir_fd=destination_descriptor,
            )
            try:
                remaining_bytes = memoryview(source_bytes)
                while remaining_bytes:
                    try:
                        written_count = os.write(
                            copied_descriptor,
                            remaining_bytes,
                        )
                    except InterruptedError:
                        continue
                    if written_count < 1:
                        raise OSError(
                            "durability snapshot copy made no progress"
                        )
                    remaining_bytes = remaining_bytes[written_count:]
                os.fsync(copied_descriptor)
                copied_state = os.fstat(copied_descriptor)
                _require_failure_durability_file_state(copied_state)
                copied_path_state = os.stat(
                    leaf_name,
                    dir_fd=destination_descriptor,
                    follow_symlinks=False,
                )
                if (
                    _failure_durability_state_identity(copied_state)
                    != _failure_durability_state_identity(copied_path_state)
                    or copied_state.st_size != len(source_bytes)
                ):
                    raise RuntimeError(
                        "durability copy changed before publication"
                    )
            finally:
                os.close(copied_descriptor)

            copied_bytes, copied_fingerprint = (
                _read_failure_durability_leaf(
                    destination_descriptor,
                    leaf_name,
                )
            )
            source_bytes_after, source_fingerprint_after = (
                _read_failure_durability_leaf(
                    source_descriptor,
                    leaf_name,
                )
            )
            if (
                source_fingerprint_after != source_fingerprint
                or source_bytes_after != source_bytes
                or copied_bytes != source_bytes
            ):
                raise RuntimeError("durability source changed during copy")
            _require_failure_durability_snapshot_equivalence(
                (source_fingerprint_after,),
                (copied_fingerprint,),
            )

        os.fsync(destination_descriptor)
        _verify_failure_durability_ancestor_chain(
            source_ancestor_descriptors,
            source_ancestor_identities,
        )
        _verify_failure_durability_directory_identity(
            source_history_path.parent,
            source_descriptor,
            source_directory_identity,
            expected_change_time_ns=source_change_time_ns,
        )
        destination_state = os.fstat(destination_descriptor)
        destination_ancestor_identities = (
            *destination_ancestor_identities[:-1],
            (
                destination_state.st_dev,
                destination_state.st_ino,
                destination_state.st_ctime_ns,
            ),
        )  # Snapshot leaf publication으로 의도적으로 바뀐 final directory ctime만 새 기준으로 고정한다.
        _verify_failure_durability_ancestor_chain(
            destination_ancestor_descriptors,
            destination_ancestor_identities,
        )
        _verify_failure_durability_directory_identity(
            snapshot_directory,
            destination_descriptor,
            destination_directory_identity,
        )
        snapshot_handle = _FailureDurabilitySnapshotHandle(
            history_path=isolated_history_path,
            source_directory_path=source_history_path.parent,
            source_directory_descriptor=source_descriptor,
            source_directory_identity=source_directory_identity,
            source_directory_change_time_ns=source_change_time_ns,
            source_ancestor_descriptors=source_ancestor_descriptors,
            source_ancestor_identities=source_ancestor_identities,
            directory_descriptor=destination_descriptor,
            directory_identity=destination_directory_identity,
            directory_change_time_ns=destination_state.st_ctime_ns,
            ancestor_descriptors=destination_ancestor_descriptors,
            ancestor_identities=destination_ancestor_identities,
        )
        source_ancestor_descriptors = None  # Handle이 source ancestor chain을 fresh runtime 종료까지 소유한다.
        destination_ancestor_descriptors = None  # Handle이 copy ancestor chain도 cleanup까지 소유한다.
    finally:
        cleanup_error: OSError | None = None
        if destination_ancestor_descriptors is not None:
            try:
                _close_failure_durability_directory_chain(
                    destination_ancestor_descriptors
                )
            except OSError as error:
                cleanup_error = error
        if source_ancestor_descriptors is not None:
            try:
                _close_failure_durability_directory_chain(
                    source_ancestor_descriptors
                )
            except OSError as error:
                if cleanup_error is None:
                    cleanup_error = error
        if cleanup_error is not None:
            raise cleanup_error  # Body가 실패해도 두 chain을 모두 닫고 첫 cleanup 오류를 보존한다.

    if snapshot_handle is None:
        raise RuntimeError("durability snapshot handle was not created")

    return snapshot_handle  # Plain Path만 넘기지 않고 pinned descriptor를 runtime 검증 종료까지 유지한다.


def _require_failure_timestamp(value: object, location: str) -> str:
    """
    함수 이름: _require_failure_timestamp()
    기능: failure evidence의 UTC timestamp가 canonical microsecond Z 문자열인지 검증한다.
    인자: value -> 검증할 외부 값
        location -> credential 없는 고정 schema 위치
    반환값: 검증된 timestamp 문자열
    작성 날짜: 2026/08/31
    """
    if not isinstance(value, str):
        raise TypeError(f"{location} must be a string")
    try:
        parsed_value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{location} must be an ISO timestamp") from error
    if (
        not value.endswith("Z")
        or parsed_value.tzinfo is None
        or parsed_value.utcoffset() != timezone.utc.utcoffset(parsed_value)
        or _datetime_to_wire(parsed_value) != value
    ):
        raise ValueError(f"{location} must be canonical UTC")

    return value


def _require_failure_decimal_or_none(value: object, location: str) -> None:
    """
    함수 이름: _require_failure_decimal_or_none()
    기능: failure snapshot의 선택 수량이 finite non-negative plain Decimal 문자열인지 검증한다.
    인자: value -> plain Decimal 문자열 또는 관찰 불가를 뜻하는 None
        location -> credential 없는 고정 schema 위치
    반환값: 없음
    작성 날짜: 2026/08/31
    """
    if value is None:
        return
    if not isinstance(value, str) or not value or value != value.strip():
        raise TypeError(f"{location} must be a plain decimal string or None")
    try:
        decimal_value = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{location} must be a Decimal") from error
    if not decimal_value.is_finite() or decimal_value < Decimal("0"):
        raise ValueError(f"{location} must be finite and non-negative")
    if format(decimal_value, "f") != value:
        raise ValueError(f"{location} must not use exponent notation")


def _is_zero_failure_decimal(value: object) -> bool:
    """
    함수 이름: _is_zero_failure_decimal()
    기능: 검증 전후 failure snapshot 값이 finite Decimal zero인지 예외 없이 판정한다.
    인자: value -> plain Decimal 문자열 후보
    반환값: finite zero이면 True, 그 밖에는 False
    작성 날짜: 2026/08/31
    """
    if not isinstance(value, str):
        return False
    try:
        decimal_value = Decimal(value)
    except InvalidOperation:
        return False

    return decimal_value.is_finite() and decimal_value == Decimal("0")


def _failure_recovery_has_safe_terminal_facts(
    recovery_outcome: object,
    runtime_state: Mapping[str, object],
    fresh_verification: Mapping[str, object],
) -> bool:
    """
    함수 이름: _failure_recovery_has_safe_terminal_facts()
    기능: completed recovery 주장을 first-runtime과 fresh read-only zero-exposure 사실에 교차 결속한다.
    인자: recovery_outcome -> SUCCESS 또는 NOT_REQUIRED 후보
        runtime_state -> failure 직전 first-runtime snapshot
        fresh_verification -> close 뒤 read-only runtime snapshot
    반환값: 두 snapshot이 completed recovery의 safe terminal 조건을 모두 만족하면 True
    작성 날짜: 2026/08/31
    """
    if recovery_outcome not in {"SUCCESS", "NOT_REQUIRED"}:
        return False
    if not isinstance(runtime_state, Mapping) or not isinstance(
        fresh_verification,
        Mapping,
    ):
        return False

    # First runtime은 terminal/non-started 상태, zero Position, empty pending과 no reconciliation이어야 한다.
    runtime_terminal_statuses = (
        {"TERMINATED"}
        if recovery_outcome == "SUCCESS"
        else {"NOT_STARTED", "TERMINATED"}
    )
    if (
        runtime_state.get("trading_status") not in runtime_terminal_statuses
        or not _is_zero_failure_decimal(runtime_state.get("position_quantity"))
        or runtime_state.get("pending_order_count") != 0
        or runtime_state.get("reconciliation_required") is not False
        or type(runtime_state.get("durable_trade_count")) is not int
    ):
        return False

    # Fresh VERIFIED는 모든 required 관찰값이 concrete이고 exchange open exposure도 zero여야 한다.
    required_fresh_counts = (
        fresh_verification.get("pending_order_count"),
        fresh_verification.get("matching_open_order_count"),
        fresh_verification.get("run_exchange_order_count"),
        fresh_verification.get("durable_trade_count"),
    )
    if (
        fresh_verification.get("status") != "VERIFIED"
        or not isinstance(fresh_verification.get("verified_at"), str)
        or not _is_zero_failure_decimal(
            fresh_verification.get("position_quantity")
        )
        or fresh_verification.get("pending_order_count") != 0
        or fresh_verification.get("reconciliation_required") is not False
        or fresh_verification.get("matching_open_order_count") != 0
        or (
            "account_open_orders_empty" in fresh_verification
            and fresh_verification.get("account_open_orders_empty") is not True
        )
        or (
            "account_open_order_lists_empty" in fresh_verification
            and fresh_verification.get("account_open_order_lists_empty")
            is not True
        )
        or any(type(value) is not int for value in required_fresh_counts)
    ):
        return False
    if recovery_outcome == "SUCCESS" and (
        runtime_state.get("durable_trade_count") != 2
        or fresh_verification.get("durable_trade_count") != 2
        or fresh_verification.get("run_exchange_order_count") != 2
    ):
        return False

    return True


def _reject_failure_secret_material(
    value: object,
    forbidden_values: tuple[str, ...],
    *,
    location: str = "failure evidence",
) -> None:
    """
    함수 이름: _reject_failure_secret_material()
    기능: fallback failure tree에서 secret-like key/value, canary, float와 bytes를 재귀 거부한다.
    인자: value -> 검사할 JSON tree 값
        forbidden_values -> artifact에 절대 포함하면 안 되는 실제 canary
        location -> credential 없는 고정 오류 위치
    반환값: 없음
    작성 날짜: 2026/08/31
    """
    if isinstance(value, Mapping):
        for key, child_value in value.items():
            if not isinstance(key, str) or _FAILURE_SECRET_FRAGMENT_PATTERN.search(key):
                raise ValueError(f"{location} contains a forbidden key")
            _reject_failure_secret_material(
                child_value,
                forbidden_values,
                location=location,
            )
        return
    if isinstance(value, list):
        for child_value in value:
            _reject_failure_secret_material(
                child_value,
                forbidden_values,
                location=location,
            )
        return
    if isinstance(value, str):
        if _FAILURE_SECRET_FRAGMENT_PATTERN.search(value) or any(
            forbidden_value in value for forbidden_value in forbidden_values
        ):
            raise ValueError(f"{location} contains forbidden secret-like material")
        return
    if value is None or type(value) in {bool, int}:
        return

    raise TypeError(f"{location} contains a non-JSON-safe value")


def _validate_failure_evidence_body(trace_body: Mapping[str, object]) -> None:
    """
    함수 이름: _validate_failure_evidence_body()
    기능: 정상 trace를 완성할 수 없는 mutation failure의 최소 exact schema와 안전 상태를 검증한다.
    인자: trace_body -> failure_sha256가 없는 failure evidence body
    반환값: 없음
    작성 날짜: 2026/08/31
    """
    if not isinstance(trace_body, Mapping):
        raise TypeError("failure evidence body must be a mapping")
    schema_version = trace_body.get("schema_version")
    if type(schema_version) is not int or schema_version not in (
        _SUPPORTED_PHASE13_FAILURE_EVIDENCE_SCHEMA_VERSIONS
    ):
        raise ValueError("failure evidence schema version is unsupported")
    expected_body_fields = (
        _FAILURE_EVIDENCE_BODY_FIELDS_V2
        if schema_version == 2
        else _FAILURE_EVIDENCE_BODY_FIELDS_V1
    )
    if set(trace_body) != expected_body_fields:
        raise ValueError("failure evidence body fields are not exact")
    if (
        trace_body["record_type"] != _PHASE13_FAILURE_EVIDENCE_RECORD_TYPE
        or trace_body["outcome"] != "FAILED"
    ):
        raise ValueError("failure evidence identity is invalid")
    typed_reason = trace_body["typed_reason"]
    if (
        not isinstance(typed_reason, str)
        or _FAILURE_TOKEN_PATTERN.fullmatch(typed_reason) is None
    ):
        raise ValueError("failure typed_reason must be a canonical token")
    if schema_version == 2 and typed_reason not in _FAILURE_ACTUAL_REASONS:
        raise ValueError("failure typed_reason is unsupported")
    try:
        parsed_run_id = UUID(str(trace_body["run_id"]))
    except ValueError as error:
        raise ValueError("failure run_id must be a UUID") from error
    if parsed_run_id.version != 4 or str(parsed_run_id) != trace_body["run_id"]:
        raise ValueError("failure run_id must be canonical UUIDv4")

    # Run timestamp는 시작보다 완료가 빠를 수 없고 exact 두 field 외 확장을 허용하지 않는다.
    timestamps = trace_body["timestamps"]
    if not isinstance(timestamps, Mapping) or set(timestamps) != {
        "started_at",
        "completed_at",
    }:
        raise ValueError("failure timestamps fields are not exact")
    started_at = _require_failure_timestamp(timestamps["started_at"], "started_at")
    completed_at = _require_failure_timestamp(
        timestamps["completed_at"],
        "completed_at",
    )
    if completed_at < started_at:
        raise ValueError("failure completed_at precedes started_at")

    # V2 first cause는 Controller의 한 lock snapshot만 허용하고 불확정 상태에는 category를 남기지 않는다.
    if schema_version == 2:
        first_cause = trace_body["first_cause"]
        if (
            not isinstance(first_cause, Mapping)
            or set(first_cause) != _FAILURE_FIRST_CAUSE_FIELDS
            or type(first_cause["reconciliation_required"]) is not bool
            or first_cause["status"] not in _FAILURE_FIRST_CAUSE_STATUSES
        ):
            raise ValueError("failure first cause is invalid")
        first_cause_status = first_cause["status"]
        first_cause_category = first_cause["category"]
        if first_cause_status == ReconciliationCauseStatus.EXACT.value:
            if first_cause_category not in _FAILURE_FIRST_CAUSE_CATEGORIES:
                raise ValueError("exact failure cause requires a category")
            if (
                first_cause_category
                in {
                    ReconciliationCauseCategory.EVENT_WORKER_OR_RUNTIME_FAILED.value,
                    ReconciliationCauseCategory.PROCESS_OWNERSHIP_AMBIGUOUS.value,
                }
                and first_cause["reconciliation_required"] is not True
            ):
                raise ValueError("permanent failure cause lacks its blocker")
        elif first_cause_category is not None:
            raise ValueError("non-exact failure cause must omit category")
    # Mutation guard는 failure handler가 추가 제출을 차단한 뒤의 snapshot만 seal한다.
    mutation_guard = trace_body["mutation_guard"]
    if (
        not isinstance(mutation_guard, Mapping)
        or set(mutation_guard) != _FAILURE_MUTATION_GUARD_FIELDS
        or type(mutation_guard["mutation_started"]) is not bool
        or mutation_guard["submissions_blocked"] is not True
        or not isinstance(mutation_guard["submission_attempts"], list)
    ):
        raise ValueError("failure mutation guard is incomplete")
    attempts = mutation_guard["submission_attempts"]
    if len(attempts) > 2:
        raise ValueError("failure evidence permits at most two order attempts")
    expected_attempt_fields = (
        _FAILURE_SUBMISSION_ATTEMPT_FIELDS_V2
        if schema_version == 2
        else _FAILURE_SUBMISSION_ATTEMPT_FIELDS_V1
    )
    latest_attempted_at: str | None = None
    for sequence, attempt in enumerate(attempts, start=1):
        if (
            not isinstance(attempt, Mapping)
            or set(attempt) != expected_attempt_fields
            or attempt["sequence"] != sequence
            or attempt["symbol"] != "ETHUSDT"
            or attempt["order_type"] != "MARKET"
            or attempt["side"] not in {"BUY", "SELL"}
            or type(attempt["submission_attempt"]) is not int
            or attempt["submission_attempt"] != 0
        ):
            raise ValueError("failure submission attempt is invalid")
        if schema_version == 1:
            for field_name in ("intent_id", "client_order_id"):
                field_value = attempt[field_name]
                if (
                    not isinstance(field_value, str)
                    or _FAILURE_SAFE_TEXT_PATTERN.fullmatch(field_value) is None
                ):
                    raise ValueError("failure submission identity is invalid")
        attempted_at = _require_failure_timestamp(
            attempt["attempted_at"],
            "attempted_at",
        )
        if schema_version == 2 and (
            attempted_at < started_at
            or attempted_at > completed_at
            or (
                latest_attempted_at is not None
                and attempted_at < latest_attempted_at
            )
        ):
            raise ValueError("failure attempt timestamp is outside run order")
        latest_attempted_at = attempted_at
    if bool(attempts) != mutation_guard["mutation_started"]:
        raise ValueError("failure mutation flag does not match submission attempts")
    if tuple(attempt["side"] for attempt in attempts) not in {
        (),
        ("BUY",),
        ("BUY", "SELL"),
    }:
        raise ValueError("failure submission attempts must preserve BUY then STOP SELL order")

    # Recovery는 known-safe STOP을 실제 시도했는지와 실패·skip 이유를 원문 없는 typed 값으로 남긴다.
    recovery = trace_body["recovery"]
    if (
        not isinstance(recovery, Mapping)
        or set(recovery) != _FAILURE_RECOVERY_FIELDS
        or type(recovery["attempted"]) is not bool
        or recovery["outcome"] not in _FAILURE_RECOVERY_OUTCOMES
    ):
        raise ValueError("failure recovery evidence is invalid")
    recovery_reason = recovery["typed_reason"]
    if recovery_reason is not None and (
        not isinstance(recovery_reason, str)
        or _FAILURE_TOKEN_PATTERN.fullmatch(recovery_reason) is None
    ):
        raise ValueError("failure recovery reason must be a canonical token")
    if (
        schema_version == 2
        and recovery_reason is not None
        and recovery_reason not in _FAILURE_RECOVERY_REASONS
    ):
        raise ValueError("failure recovery reason is unsupported")
    if recovery["outcome"] in {"NOT_REQUIRED", "SUCCESS"}:
        if recovery_reason is not None:
            raise ValueError("completed failure recovery must not have a reason")
    elif recovery_reason is None:
        raise ValueError("skipped or failed recovery requires a typed reason")
    if recovery["attempted"] != (
        recovery["outcome"] in {"SUCCESS", "FAILED"}
    ):
        raise ValueError("failure recovery attempted flag is inconsistent")
    if schema_version == 2 and recovery["outcome"] == "FAILED" and (
        recovery_reason not in _FAILURE_RECOVERY_FAILED_REASONS
    ):
        raise ValueError("failed recovery reason precedes any STOP attempt")
    if schema_version == 2 and recovery["attempted"] and (
        not attempts or attempts[0]["side"] != "BUY"
    ):
        raise ValueError("attempted recovery lacks an initial BUY attempt")
    if recovery["outcome"] == "SUCCESS" and tuple(
        attempt["side"] for attempt in attempts
    ) != ("BUY", "SELL"):
        raise ValueError("successful failure recovery requires BUY then STOP SELL")

    # Runtime과 fresh snapshot은 관찰 불가 field를 None으로 남기되 추측한 zero로 채우지 않는다.
    runtime_state = trace_body["runtime_state"]
    if not isinstance(runtime_state, Mapping) or set(runtime_state) != (
        _FAILURE_RUNTIME_STATE_FIELDS
    ):
        raise ValueError("failure runtime state fields are not exact")
    for field_name in ("application_status", "trading_status"):
        field_value = runtime_state[field_name]
        if (
            not isinstance(field_value, str)
            or _FAILURE_TOKEN_PATTERN.fullmatch(field_value) is None
        ):
            raise ValueError("failure runtime status is invalid")
    if schema_version == 2 and (
        runtime_state["application_status"] not in _FAILURE_APPLICATION_STATUSES
        or runtime_state["trading_status"] not in _FAILURE_TRADING_STATUSES
    ):
        raise ValueError("failure runtime status is unsupported")
    if schema_version == 2 and runtime_state["trading_status"] in {
        "RUNNING",
        "STOPPING",
    }:
        raise ValueError("failure runtime scheduler gate is still open")
    if (
        schema_version == 2
        and recovery["attempted"]
        and runtime_state["application_status"] != "READY"
    ):
        raise ValueError("attempted recovery lacks a READY application")
    if (
        schema_version == 2
        and attempts
        and runtime_state["application_status"] not in {"READY", "CLOSED"}
    ):
        raise ValueError("submission attempts lack an observable application")
    if (
        schema_version == 2
        and attempts
        and runtime_state["trading_status"] == "NOT_STARTED"
    ):
        raise ValueError("submission attempts lack an active trading session")
    runtime_reconciliation = runtime_state["reconciliation_required"]
    if runtime_reconciliation is not None and type(runtime_reconciliation) is not bool:
        raise TypeError("failure reconciliation flag must be bool or None")
    if (
        schema_version == 2
        and runtime_state["trading_status"] == "RECONCILIATION_REQUIRED"
        and runtime_reconciliation is False
    ):
        raise ValueError("reconciliation status contradicts runtime blocker")
    if schema_version == 2 and (
        first_cause_status == ReconciliationCauseStatus.EXACT.value
        and first_cause_category
        in {
            ReconciliationCauseCategory.EVENT_WORKER_OR_RUNTIME_FAILED.value,
            ReconciliationCauseCategory.PROCESS_OWNERSHIP_AMBIGUOUS.value,
        }
    ):
        if recovery["attempted"] or recovery["outcome"] != "SKIPPED":
            raise ValueError("permanent first cause cannot complete recovery")
        if recovery_reason in {
            "FAILURE_RECOVERY_EFFECTIVE_FREE_UNCONFIRMED",
            "FAILURE_RECOVERY_STATE_CHANGED",
            "FAILURE_RECOVERY_FINAL_VERIFICATION_INCOMPLETE",
        }:
            raise ValueError("permanent first cause cannot reach late recovery")
        if runtime_reconciliation is False:
            raise ValueError("permanent first cause lost its runtime blocker")
    _require_failure_decimal_or_none(
        runtime_state["position_quantity"],
        "runtime position_quantity",
    )
    for field_name in ("pending_order_count", "durable_trade_count"):
        field_value = runtime_state[field_name]
        if field_value is not None and (
            type(field_value) is not int or field_value < 0
        ):
            raise ValueError("failure runtime count must be non-negative or None")
    if (
        schema_version == 2
        and runtime_state["durable_trade_count"] is not None
        and runtime_state["durable_trade_count"] > len(attempts)
    ):
        raise ValueError("failure runtime durable count exceeds order attempts")
    if (
        schema_version == 2
        and runtime_state["pending_order_count"] is not None
        and runtime_state["pending_order_count"] > 1
    ):
        raise ValueError("failure runtime pending count exceeds serial owner cap")
    if (
        schema_version == 2
        and recovery["attempted"]
        and runtime_state["durable_trade_count"] is not None
        and runtime_state["durable_trade_count"] < 1
    ):
        raise ValueError("attempted recovery lacks a durable BUY")
    if (
        schema_version == 2
        and len(attempts) == 2
        and runtime_state["durable_trade_count"] is not None
        and runtime_state["durable_trade_count"] < 1
    ):
        raise ValueError("SELL attempt lacks a runtime durable BUY")
    if (
        schema_version == 2
        and not attempts
        and runtime_state["position_quantity"] is not None
        and not _is_zero_failure_decimal(runtime_state["position_quantity"])
    ):
        raise ValueError("failure runtime position lacks an order attempt")
    if (
        schema_version == 2
        and len(attempts) == 1
        and runtime_state["durable_trade_count"] == 1
        and runtime_state["position_quantity"] is not None
        and _is_zero_failure_decimal(runtime_state["position_quantity"])
    ):
        raise ValueError("single durable BUY lacks runtime position")

    fresh_verification = trace_body["fresh_verification"]
    expected_fresh_fields = (
        _FAILURE_FRESH_VERIFICATION_FIELDS_V2
        if schema_version == 2
        else _FAILURE_FRESH_VERIFICATION_FIELDS_V1
    )
    if (
        not isinstance(fresh_verification, Mapping)
        or set(fresh_verification) != expected_fresh_fields
    ):
        raise ValueError("failure fresh verification fields are not exact")
    if (
        fresh_verification["status"]
        not in _FAILURE_FRESH_VERIFICATION_STATUSES
    ):
        raise ValueError("failure fresh verification status is invalid")
    fresh_reason = fresh_verification["typed_reason"]
    if (fresh_verification["status"] == "VERIFIED") != (fresh_reason is None):
        raise ValueError("failure fresh status and reason are inconsistent")
    if fresh_reason is not None and (
        not isinstance(fresh_reason, str)
        or _FAILURE_TOKEN_PATTERN.fullmatch(fresh_reason) is None
    ):
        raise ValueError("failure fresh typed reason is invalid")
    if (
        schema_version == 2
        and fresh_reason is not None
        and fresh_reason not in _FAILURE_FRESH_REASONS
    ):
        raise ValueError("failure fresh typed reason is unsupported")
    if schema_version == 2:
        failure_stage = fresh_verification["failure_stage"]
        if fresh_verification["status"] == "VERIFIED":
            if failure_stage is not None:
                raise ValueError("VERIFIED fresh evidence must omit failure stage")
        elif failure_stage not in _FAILURE_FRESH_VERIFICATION_STAGES:
            raise ValueError("INCOMPLETE fresh evidence requires an exact stage")
        elif fresh_reason not in _FAILURE_FRESH_STAGE_ALLOWED_REASONS[
            failure_stage
        ]:
            raise ValueError("fresh reason does not match its failure stage")
        for field_name in (
            "account_open_orders_empty",
            "account_open_order_lists_empty",
        ):
            field_value = fresh_verification[field_name]
            if field_value is not None and type(field_value) is not bool:
                raise TypeError("failure fresh empty-state flag is invalid")
    verified_at = fresh_verification["verified_at"]
    if verified_at is not None:
        verified_at = _require_failure_timestamp(
            verified_at,
            "fresh verified_at",
        )
        if schema_version == 2 and not (
            started_at <= verified_at <= completed_at
        ):
            raise ValueError("fresh verified_at is outside the run window")
        if (
            schema_version == 2
            and latest_attempted_at is not None
            and verified_at < latest_attempted_at
        ):
            raise ValueError("fresh verified_at precedes the last order attempt")
    for field_name in ("position_quantity",):
        _require_failure_decimal_or_none(
            fresh_verification[field_name],
            f"fresh {field_name}",
        )
    for field_name in (
        "pending_order_count",
        "matching_open_order_count",
        "run_exchange_order_count",
        "durable_trade_count",
    ):
        field_value = fresh_verification[field_name]
        if field_value is not None and (
            type(field_value) is not int or field_value < 0
        ):
            raise ValueError("failure fresh count must be non-negative or None")
    fresh_run_order_count = fresh_verification["run_exchange_order_count"]
    fresh_durable_trade_count = fresh_verification["durable_trade_count"]
    if schema_version == 2:
        if (
            fresh_verification["pending_order_count"] is not None
            and fresh_verification["pending_order_count"] > 1
        ):
            raise ValueError("failure fresh pending count exceeds serial owner cap")
        if (
            fresh_verification["matching_open_order_count"] is not None
            and not attempts
            and fresh_verification["matching_open_order_count"] != 0
        ):
            raise ValueError(
                "fresh matching open-order count lacks an order attempt"
            )
        if (
            fresh_run_order_count is not None
            and fresh_durable_trade_count is None
        ):
            raise ValueError("fresh run count lacks durable truth")
        if (
            fresh_durable_trade_count is not None
            and fresh_durable_trade_count > len(attempts)
        ):
            raise ValueError("fresh durable count exceeds order attempts")
        if (
            fresh_durable_trade_count is not None
            and runtime_state["durable_trade_count"] is not None
            and fresh_durable_trade_count
            < runtime_state["durable_trade_count"]
        ):
            raise ValueError("fresh durable truth regresses runtime history")
        if (
            recovery["attempted"]
            and fresh_durable_trade_count is not None
            and fresh_durable_trade_count < 1
        ):
            raise ValueError("fresh recovery truth lacks a durable BUY")
        if (
            len(attempts) == 2
            and fresh_durable_trade_count is not None
            and fresh_durable_trade_count < 1
        ):
            raise ValueError("SELL attempt lacks a fresh durable BUY")
        if fresh_run_order_count is not None and (
            fresh_run_order_count != fresh_durable_trade_count
            or fresh_run_order_count > len(attempts)
        ):
            raise ValueError("fresh run count contradicts durable attempts")
        if (
            not attempts
            and fresh_verification["position_quantity"] is not None
            and not _is_zero_failure_decimal(
                fresh_verification["position_quantity"]
            )
        ):
            raise ValueError("fresh position lacks a durable trade")
        if (
            fresh_durable_trade_count == 1
            and fresh_verification["position_quantity"] is not None
            and _is_zero_failure_decimal(
                fresh_verification["position_quantity"]
            )
        ):
            raise ValueError("single durable BUY lacks fresh position")
    reconciliation_required = fresh_verification["reconciliation_required"]
    if reconciliation_required is not None and type(reconciliation_required) is not bool:
        raise TypeError("failure fresh reconciliation flag must be bool or None")
    fresh_truth_complete_v2 = (
        schema_version == 2
        and verified_at is not None
        and fresh_verification["position_quantity"] is not None
        and fresh_verification["pending_order_count"] == 0
        and reconciliation_required is False
        and fresh_verification["matching_open_order_count"] == 0
        and fresh_verification["account_open_orders_empty"] is True
        and fresh_verification["account_open_order_lists_empty"] is True
        and fresh_run_order_count is not None
        and fresh_durable_trade_count is not None
    )
    if schema_version == 2 and fresh_verification["status"] == "INCOMPLETE":
        stage_reason_pair = (
            fresh_verification["failure_stage"],
            fresh_reason,
        )
        all_fresh_observations_missing = all(
            fresh_verification[field_name] is None
            for field_name in _FAILURE_FRESH_OBSERVATION_FIELDS_V2
        )
        local_observations_missing = all(
            fresh_verification[field_name] is None
            for field_name in (
                "position_quantity",
                "pending_order_count",
                "reconciliation_required",
            )
        )
        local_observations_published = (
            fresh_verification["pending_order_count"] is not None
            and reconciliation_required is not None
        )
        if not (
            local_observations_missing or local_observations_published
        ):
            raise ValueError("fresh local observation group is partial")
        local_state_ready = (
            fresh_verification["position_quantity"] is not None
            and fresh_verification["pending_order_count"] == 0
            and reconciliation_required is False
        )
        open_order_group_consistent = not (
            fresh_verification["account_open_orders_empty"] is None
            and fresh_verification["matching_open_order_count"] is not None
        )
        if not open_order_group_consistent:
            raise ValueError("fresh open-order observation group is partial")
        open_orders_ready = (
            fresh_verification["account_open_orders_empty"] is True
            and fresh_verification["matching_open_order_count"] == 0
        )
        pre_recent_prefix_ready = (
            local_state_ready
            and open_orders_ready
            and fresh_verification["account_open_order_lists_empty"] is True
        )
        counts_missing = (
            fresh_run_order_count is None
            and fresh_durable_trade_count is None
        )
        counts_complete = (
            fresh_run_order_count is not None
            and fresh_durable_trade_count is not None
        )
        failure_stage = fresh_verification["failure_stage"]
        if fresh_verification["failure_stage"] in {
            "RUNTIME_CREATION",
            "STARTUP_APPLICATION",
            "MARKET",
            "REGIME",
            "ACCOUNT",
            "HISTORY",
            "RECONCILIATION",
        } and not all_fresh_observations_missing:
            raise ValueError("startup-stage failure contains downstream truth")
        if failure_stage == "LOCAL_STATE" and not (
            verified_at is None
            and (
                local_observations_missing
                or (
                    local_observations_published
                    and not local_state_ready
                )
            )
            and fresh_verification["account_open_orders_empty"] is None
            and fresh_verification["matching_open_order_count"] is None
            and fresh_verification["account_open_order_lists_empty"] is None
            and counts_missing
        ):
            raise ValueError("local-state failure has a downstream prefix")
        if failure_stage == "FILTER" and not (
            all_fresh_observations_missing
            or (
                verified_at is None
                and pre_recent_prefix_ready
                and counts_missing
            )
        ):
            raise ValueError("filter failure has an impossible observation prefix")
        if failure_stage == "STREAM" and not (
            all_fresh_observations_missing
            or (
                verified_at is None
                and pre_recent_prefix_ready
                and counts_complete
            )
        ):
            raise ValueError("stream failure has an impossible observation prefix")
        if failure_stage == "OPEN_ORDERS":
            first_open_order_shape = (
                fresh_verification["account_open_order_lists_empty"] is None
                and counts_missing
            )
            final_open_order_shape = (
                fresh_verification["account_open_order_lists_empty"] is True
                and counts_complete
                and type(
                    fresh_verification["account_open_orders_empty"]
                )
                is bool
                and fresh_verification["matching_open_order_count"] is not None
            )
            if not (
                verified_at is None
                and local_state_ready
                and open_order_group_consistent
                and (first_open_order_shape or final_open_order_shape)
                and (
                    fresh_reason != "FRESH_OPEN_ORDERS_CHANGED"
                    or (
                        final_open_order_shape
                        and fresh_verification["account_open_orders_empty"]
                        is True
                        and fresh_verification[
                            "matching_open_order_count"
                        ]
                        is not None
                    )
                )
                and not (
                    fresh_reason == "FRESH_OPEN_ORDERS_NOT_EMPTY"
                    and final_open_order_shape
                    and fresh_verification["account_open_orders_empty"]
                    is True
                    and fresh_verification["matching_open_order_count"] != 0
                )
            ):
                raise ValueError("open-order failure lacks its observed prefix")
        if failure_stage == "OPEN_ORDER_LISTS" and not (
            verified_at is None
            and local_state_ready
            and open_orders_ready
            and (
                (
                    counts_missing
                    and fresh_verification[
                        "account_open_order_lists_empty"
                    ]
                    is not True
                )
                or (
                    counts_complete
                    and type(
                        fresh_verification[
                            "account_open_order_lists_empty"
                        ]
                    )
                    is bool
                )
            )
        ):
            raise ValueError("open-order-list failure lacks its observed prefix")
        if failure_stage == "RECENT_ORDERS" and not (
            verified_at is None
            and pre_recent_prefix_ready
            and (
                (
                    fresh_reason == "FRESH_EXCHANGE_BASELINE_UNAVAILABLE"
                    and counts_missing
                )
                or (
                    fresh_reason == "FRESH_RECENT_ORDERS_CHANGED"
                    and (
                        (
                            fresh_run_order_count is None
                            and fresh_durable_trade_count is not None
                        )
                        or counts_complete
                    )
                )
            )
        ):
            raise ValueError("recent-order failure lacks its observed prefix")
        if failure_stage == "DURABILITY" and not (
            all_fresh_observations_missing or fresh_truth_complete_v2
        ):
            raise ValueError("durability failure has a partial observation prefix")
        if verified_at is not None and (
            stage_reason_pair not in _FAILURE_POST_VERIFICATION_STAGE_REASONS
            or not fresh_truth_complete_v2
        ):
            raise ValueError(
                "incomplete verified_at lacks post-verification failure truth"
            )
        if stage_reason_pair in {
            ("CLEANUP", "FRESH_RUNTIME_CLOSE_FAILED"),
            ("CLEANUP", "FRESH_SNAPSHOT_CLEANUP_FAILED"),
        } and not fresh_truth_complete_v2:
            raise ValueError("cleanup failure lacks complete fresh truth")
        if fresh_reason == "PRIMARY_RUNTIME_NOT_CLOSED" and (
            not all_fresh_observations_missing
        ):
            raise ValueError("primary close failure must omit fresh observations")
    if fresh_verification["status"] == "VERIFIED" and (
        verified_at is None
        or fresh_verification["position_quantity"] is None
        or reconciliation_required is None
        or any(
            fresh_verification[field_name] is None
            for field_name in (
                "pending_order_count",
                "matching_open_order_count",
                "run_exchange_order_count",
                "durable_trade_count",
            )
        )
        or (schema_version == 2 and not fresh_truth_complete_v2)
    ):
        raise ValueError(
            "VERIFIED fresh evidence requires every observed field"
        )
    if recovery["outcome"] in {"SUCCESS", "NOT_REQUIRED"} and not (
        _failure_recovery_has_safe_terminal_facts(
            recovery["outcome"],
            runtime_state,
            fresh_verification,
        )
    ):
        raise ValueError(
            "completed failure recovery contradicts terminal state evidence"
        )
    if schema_version == 2 and recovery["outcome"] == "NOT_REQUIRED":
        not_required_truth = (
            runtime_state["durable_trade_count"],
            fresh_durable_trade_count,
            len(attempts),
        )
        if not_required_truth not in {(0, 0, 0), (2, 2, 2)}:
            raise ValueError("NOT_REQUIRED recovery has an impossible trade suffix")
        if (
            not_required_truth == (2, 2, 2)
            and runtime_state["trading_status"] != "TERMINATED"
        ):
            raise ValueError("completed round trip lacks terminal runtime truth")
    if schema_version == 2 and recovery_reason in {
        "FAILURE_RECOVERY_EFFECTIVE_FREE_UNCONFIRMED",
        "FAILURE_RECOVERY_STATE_CHANGED",
    }:
        if not attempts or runtime_state["application_status"] != "READY":
            raise ValueError("late recovery skip lacks its known-safe BUY")
        if (
            runtime_state["durable_trade_count"] is not None
            and runtime_state["durable_trade_count"] < 1
        ) or (
            fresh_durable_trade_count is not None
            and fresh_durable_trade_count < 1
        ):
            raise ValueError("late recovery skip contradicts durable BUY truth")
    if (
        schema_version == 2
        and recovery_reason
        == "FAILURE_RECOVERY_FINAL_VERIFICATION_INCOMPLETE"
    ):
        if recovery["attempted"]:
            attempted_runtime_is_terminal = (
                len(attempts) == 2
                and runtime_state["application_status"] == "READY"
                and runtime_state["trading_status"] == "TERMINATED"
                and (
                    runtime_state["position_quantity"] is None
                    or _is_zero_failure_decimal(
                        runtime_state["position_quantity"]
                    )
                )
                and runtime_state["pending_order_count"] in {None, 0}
                and runtime_state["durable_trade_count"] in {None, 2}
            )
            fresh_terminal_values_are_consistent = (
                (
                    fresh_run_order_count is None
                    or fresh_run_order_count == 2
                )
                and (
                    fresh_durable_trade_count is None
                    or fresh_durable_trade_count == 2
                )
                and (
                    fresh_verification["position_quantity"] is None
                    or _is_zero_failure_decimal(
                        fresh_verification["position_quantity"]
                    )
                )
            )
            if not (
                attempted_runtime_is_terminal
                and fresh_terminal_values_are_consistent
            ):
                raise ValueError(
                    "attempted final verification lacks terminal recovery truth"
                )
        candidate_outcome = (
            "SUCCESS" if recovery["attempted"] else "NOT_REQUIRED"
        )
        if _failure_recovery_has_safe_terminal_facts(
            candidate_outcome,
            runtime_state,
            fresh_verification,
        ):
            raise ValueError("final verification reason contradicts safe facts")
    evidence_errors = trace_body["evidence_errors"]
    if not isinstance(evidence_errors, list) or any(
        not isinstance(error_code, str)
        or _FAILURE_TOKEN_PATTERN.fullmatch(error_code) is None
        for error_code in evidence_errors
    ):
        raise ValueError("failure evidence errors must be typed tokens")
    if schema_version == 2:
        if len(evidence_errors) != len(set(evidence_errors)):
            raise ValueError("failure evidence errors must be unique")
        if any(
            error_code not in _FAILURE_EVIDENCE_ERRORS
            for error_code in evidence_errors
        ):
            raise ValueError("failure evidence error is unsupported")
        observed_error_phases = [
            next(
                phase_index
                for phase_index, phase_errors in enumerate(
                    _FAILURE_EVIDENCE_ERROR_PHASES
                )
                if error_code in phase_errors
            )
            for error_code in evidence_errors
        ]
        if observed_error_phases != sorted(observed_error_phases):
            raise ValueError("failure evidence errors violate producer order")
        observed_error_set = set(evidence_errors)
        recovery_errors = _FAILURE_RECOVERY_REASONS.intersection(
            observed_error_set
        )
        expected_recovery_errors = (
            set()
            if recovery["outcome"] in {"SUCCESS", "NOT_REQUIRED"}
            else {recovery_reason}
        )
        if recovery_errors != expected_recovery_errors:
            raise ValueError("failure recovery reason contradicts evidence errors")

        # Active mark의 publication 오류는 completed recovery가 아니고 runtime gate가 닫힌 경우에만 가능하다.
        scheduler_publication_failed = (
            "FAILURE_SCHEDULER_GATE_PUBLICATION_FAILED"
            in observed_error_set
        )
        if scheduler_publication_failed and (
            recovery["outcome"] in {"SUCCESS", "NOT_REQUIRED"}
            or runtime_state["application_status"] not in {"READY", "CLOSED"}
            or runtime_state["trading_status"]
            != "RECONCILIATION_REQUIRED"
            or runtime_reconciliation is not True
        ):
            raise ValueError(
                "scheduler publication error contradicts terminal evidence"
            )
        if scheduler_publication_failed and first_cause_category in {
            ReconciliationCauseCategory.EVENT_WORKER_OR_RUNTIME_FAILED.value,
            ReconciliationCauseCategory.PROCESS_OWNERSHIP_AMBIGUOUS.value,
        }:
            raise ValueError(
                "permanent first cause cannot publish a scheduler gate"
            )

        # Nullable runtime truth와 owner별 unavailable code는 양방향 exact iff로 결속한다.
        runtime_error_fields = {
            "reconciliation_required": "RUNTIME_RECONCILIATION_UNAVAILABLE",
            "position_quantity": "RUNTIME_POSITION_UNAVAILABLE",
            "pending_order_count": "RUNTIME_PENDING_UNAVAILABLE",
            "durable_trade_count": "RUNTIME_HISTORY_UNAVAILABLE",
        }
        for field_name, error_code in runtime_error_fields.items():
            if (runtime_state[field_name] is None) != (
                error_code in observed_error_set
            ):
                raise ValueError("runtime nullable truth lacks exact error code")

        fresh_errors = _FAILURE_FRESH_REASONS.intersection(
            observed_error_set
        )
        fresh_error_sequence = [
            error_code
            for error_code in evidence_errors
            if error_code in _FAILURE_FRESH_REASONS
        ]
        if fresh_verification["status"] == "VERIFIED":
            if fresh_errors:
                raise ValueError("VERIFIED fresh evidence contains failure errors")
        elif fresh_reason == "PRIMARY_RUNTIME_NOT_CLOSED":
            if fresh_errors != {"PRIMARY_RUNTIME_NOT_CLOSED"}:
                raise ValueError("primary runtime close truth is inconsistent")
        else:
            allowed_secondary_errors = (
                _FAILURE_FRESH_ALLOWED_SECONDARY_BY_PRIMARY.get(
                    fresh_reason,
                    _FAILURE_FRESH_SECONDARY_REASONS,
                )
            )
            if (
                fresh_reason not in fresh_errors
                or fresh_error_sequence[0] != fresh_reason
                or not (fresh_errors - {fresh_reason}).issubset(
                    allowed_secondary_errors
                )
                or "PRIMARY_RUNTIME_NOT_CLOSED" in fresh_errors
            ):
                raise ValueError(
                    "fresh primary and secondary errors are inconsistent"
                )

        first_cause_status = trace_body["first_cause"]["status"]
        expected_cause_error = {
            ReconciliationCauseStatus.MISSING.value: (
                "RECONCILIATION_CAUSE_MISSING"
            ),
            ReconciliationCauseStatus.DUPLICATE.value: (
                "RECONCILIATION_CAUSE_DUPLICATE"
            ),
            ReconciliationCauseStatus.CONFLICT.value: (
                "RECONCILIATION_CAUSE_CONFLICT"
            ),
        }.get(first_cause_status)
        observed_cause_errors = _FAILURE_CAUSE_ERRORS.intersection(
            evidence_errors
        )
        if expected_cause_error is None:
            if observed_cause_errors:
                raise ValueError("exact failure cause contradicts evidence errors")
        elif observed_cause_errors != {expected_cause_error}:
            raise ValueError("non-exact failure cause lacks its secondary error")


def _seal_failure_evidence(
    trace_body: Mapping[str, object],
    *,
    forbidden_values: Sequence[str],
) -> tuple[dict[str, object], bytes, str]:
    """
    함수 이름: _seal_failure_evidence()
    기능: 최소 FAILED evidence를 exact schema·secret scan 후 canonical digest와 bytes로 seal한다.
    인자: trace_body -> failure_sha256가 없는 exact body
        forbidden_values -> 실제 API credential canary sequence
    반환값: sealed 독립 mapping, canonical bytes와 lowercase SHA-256 tuple
    작성 날짜: 2026/08/31
    """
    if isinstance(forbidden_values, (str, bytes)) or not isinstance(
        forbidden_values,
        Sequence,
    ):
        raise TypeError("forbidden_values must be a sequence")
    normalized_forbidden_values = tuple(dict.fromkeys(forbidden_values))
    if len(normalized_forbidden_values) < 2 or any(
        not isinstance(value, str) or not value
        for value in normalized_forbidden_values
    ):
        raise RuntimeError("actual failure evidence requires non-empty redaction canaries")
    if not isinstance(trace_body, Mapping):
        raise TypeError("trace_body must be a mapping")

    # Caller mapping을 변경하지 않고 검증된 JSON round-trip 사본만 digest 대상으로 사용한다.
    detached_body = json.loads(_canonical_failure_evidence_bytes(trace_body))
    _validate_failure_evidence_body(detached_body)
    _reject_failure_secret_material(
        detached_body,
        normalized_forbidden_values,
    )
    failure_digest = hashlib.sha256(
        _canonical_failure_evidence_bytes(detached_body)
    ).hexdigest()
    sealed_trace = {**detached_body, "failure_sha256": failure_digest}
    expected_document_fields = (
        _FAILURE_EVIDENCE_DOCUMENT_FIELDS_V2
        if detached_body["schema_version"] == 2
        else _FAILURE_EVIDENCE_DOCUMENT_FIELDS_V1
    )
    if set(sealed_trace) != expected_document_fields:
        raise AssertionError("sealed failure evidence fields changed unexpectedly")
    canonical_bytes = _canonical_failure_evidence_bytes(sealed_trace)
    _reject_failure_secret_material(
        sealed_trace,
        normalized_forbidden_values,
    )

    return sealed_trace, canonical_bytes, failure_digest


def _validate_failure_evidence_document(
    trace_document: Mapping[str, object],
    *,
    forbidden_values: Sequence[str],
) -> str:
    """
    함수 이름: _validate_failure_evidence_document()
    기능: 보존 v1과 신규 v2 sealed document의 exact body digest를 필드 합성 없이 검증한다.
    인자: trace_document -> failure_sha256를 포함한 failure evidence document
        forbidden_values -> artifact에서 거부할 credential canary sequence
    반환값: 검증된 lowercase body SHA-256
    작성 날짜: 2026/08/31
    """
    if not isinstance(trace_document, Mapping):
        raise TypeError("trace_document must be a mapping")
    schema_version = trace_document.get("schema_version")
    if type(schema_version) is not int or schema_version not in (
        _SUPPORTED_PHASE13_FAILURE_EVIDENCE_SCHEMA_VERSIONS
    ):
        raise ValueError("failure evidence schema version is unsupported")
    expected_document_fields = (
        _FAILURE_EVIDENCE_DOCUMENT_FIELDS_V2
        if schema_version == 2
        else _FAILURE_EVIDENCE_DOCUMENT_FIELDS_V1
    )
    if set(trace_document) != expected_document_fields:
        raise ValueError("failure evidence document fields are not exact")
    recorded_digest = trace_document["failure_sha256"]
    if (
        not isinstance(recorded_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", recorded_digest) is None
    ):
        raise ValueError("failure evidence digest is invalid")

    # Version별 body를 그대로 떼어 다시 seal해 v1을 v2 의미로 채우는 implicit migration을 막는다.
    trace_body = {
        field_name: field_value
        for field_name, field_value in trace_document.items()
        if field_name != "failure_sha256"
    }
    sealed_trace, _, validated_digest = _seal_failure_evidence(
        trace_body,
        forbidden_values=forbidden_values,
    )
    if validated_digest != recorded_digest or sealed_trace != trace_document:
        raise ValueError("failure evidence digest does not match its body")

    return validated_digest  # Caller는 body나 credential 없이 검증된 digest만 사용한다.


def _write_failure_evidence_artifact(
    artifact_directory: Path,
    trace_body: Mapping[str, object],
    *,
    forbidden_values: Sequence[str],
) -> tuple[Path, str]:
    """
    함수 이름: _write_failure_evidence_artifact()
    기능: 불완전 fresh 검증도 숨기지 않는 secret-free sealed FAILED evidence를 원자 publish한다.
    인자: artifact_directory -> run별 owner-only artifact directory
        trace_body -> failure_sha256가 없는 exact failure body
        forbidden_values -> 실제 API credential canary sequence
    반환값: durable failure artifact 경로와 canonical SHA-256 tuple
    작성 날짜: 2026/08/31
    """
    if not isinstance(trace_body, Mapping):
        raise TypeError("trace_body must be a mapping")
    if trace_body.get("schema_version") != _PHASE13_FAILURE_EVIDENCE_SCHEMA_VERSION:
        raise ValueError("failure artifact writer requires current schema v2")

    # Offline validator는 v1을 읽지만 새 final 이름에는 current v2 bytes만 publish한다.
    _, canonical_bytes, failure_digest = _seal_failure_evidence(
        trace_body,
        forbidden_values=forbidden_values,
    )
    failure_path = _publish_new_artifact_bytes(
        artifact_directory,
        "phase13-public-case2-failed.json",
        canonical_bytes,
    )

    return failure_path, failure_digest


def _publish_new_artifact_bytes(
    artifact_directory: Path,
    artifact_name: str,
    canonical_bytes: bytes,
) -> Path:
    """
    함수 이름: _publish_new_artifact_bytes()
    기능: 검증 완료 bytes를 same-directory durable inode와 no-clobber final 이름으로 원자 publish한다.
    인자: artifact_directory -> owner-only run artifact directory
        artifact_name -> path separator가 없는 canonical JSON final 이름
        canonical_bytes -> schema·secret 검사를 이미 통과한 newline 종결 bytes
    반환값: 원자 publish가 완료된 final Path
    작성 날짜: 2026/08/31
    """
    if not isinstance(artifact_directory, Path):
        raise TypeError("artifact_directory must be a Path")
    if (
        not isinstance(artifact_name, str)
        or _ARTIFACT_NAME_PATTERN.fullmatch(artifact_name) is None
    ):
        raise ValueError("artifact_name must be a canonical JSON leaf name")
    if not isinstance(canonical_bytes, bytes) or not canonical_bytes.endswith(b"\n"):
        raise ValueError("canonical_bytes must be newline-terminated bytes")

    # Path 조회와 directory descriptor가 같은 non-symlink inode를 가리켜야 publish를 시작한다.
    directory_path_state = os.stat(
        artifact_directory,
        follow_symlinks=False,
    )
    if not stat.S_ISDIR(directory_path_state.st_mode):
        raise ValueError("artifact_directory must be a non-symlink directory")
    if (
        directory_path_state.st_uid != os.geteuid()
        or directory_path_state.st_mode & 0o077
    ):
        raise PermissionError("artifact_directory must be owner-only")
    no_follow_flag = getattr(os, "O_NOFOLLOW", None)
    if no_follow_flag is None:
        raise RuntimeError("secure artifact publication requires O_NOFOLLOW")

    trace_path = artifact_directory / artifact_name
    temporary_name = f".{artifact_name}.{uuid4().hex}.tmp"
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    directory_flags |= no_follow_flag
    directory_descriptor = os.open(artifact_directory, directory_flags)
    temporary_descriptor: int | None = None
    temporary_exists = False
    try:
        directory_descriptor_state = os.fstat(directory_descriptor)
        if (
            directory_descriptor_state.st_dev,
            directory_descriptor_state.st_ino,
        ) != (
            directory_path_state.st_dev,
            directory_path_state.st_ino,
        ):
            raise RuntimeError(
                "artifact directory identity changed before publication"
            )
        if not stat.S_ISDIR(directory_descriptor_state.st_mode):
            raise RuntimeError("artifact directory descriptor is not a directory")
        if (
            directory_descriptor_state.st_uid != os.geteuid()
            or directory_descriptor_state.st_mode & 0o077
        ):
            raise PermissionError("artifact directory descriptor is not owner-only")

        # 같은 directory FD 아래 owner-only 임시 inode를 만들고 short write까지 전부 완료한 뒤 fsync한다.
        temporary_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        temporary_flags |= getattr(os, "O_CLOEXEC", 0)
        temporary_flags |= no_follow_flag
        temporary_descriptor = os.open(
            temporary_name,
            temporary_flags,
            0o600,
            dir_fd=directory_descriptor,
        )
        temporary_exists = True
        os.fchmod(temporary_descriptor, 0o600)
        remaining_bytes = memoryview(canonical_bytes)
        while remaining_bytes:
            try:
                written_count = os.write(temporary_descriptor, remaining_bytes)
            except InterruptedError:
                continue  # Signal interruption은 같은 immutable byte suffix를 다시 기록한다.
            if written_count <= 0:
                raise OSError("trace artifact write made no forward progress")
            remaining_bytes = remaining_bytes[written_count:]
        os.fsync(temporary_descriptor)
        temporary_state = os.fstat(temporary_descriptor)
        if (
            not stat.S_ISREG(temporary_state.st_mode)
            or temporary_state.st_uid != os.geteuid()
            or temporary_state.st_mode & 0o777 != 0o600
            or temporary_state.st_nlink != 1
            or temporary_state.st_size != len(canonical_bytes)
        ):
            raise RuntimeError("temporary artifact inode failed metadata validation")
        os.close(temporary_descriptor)
        temporary_descriptor = None

        # 완성된 inode만 hard-link로 publish해 기존 final을 덮지 않고 partial final 이름을 노출하지 않는다.
        os.link(
            temporary_name,
            artifact_name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        os.unlink(temporary_name, dir_fd=directory_descriptor)
        temporary_exists = False
        os.fsync(directory_descriptor)  # Link 생성과 임시 이름 제거를 같은 directory journal에 확정한다.

        # Publish된 이름을 같은 directory FD에서 다시 열어 inode·mode·size와 전체 bytes를 검증한다.
        final_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        final_flags |= no_follow_flag
        final_descriptor = os.open(
            artifact_name,
            final_flags,
            dir_fd=directory_descriptor,
        )
        try:
            final_descriptor_state = os.fstat(final_descriptor)
            if (
                final_descriptor_state.st_dev,
                final_descriptor_state.st_ino,
            ) != (temporary_state.st_dev, temporary_state.st_ino):
                raise RuntimeError("published artifact inode identity changed")
            if (
                not stat.S_ISREG(final_descriptor_state.st_mode)
                or final_descriptor_state.st_uid != os.geteuid()
                or final_descriptor_state.st_mode & 0o777 != 0o600
                or final_descriptor_state.st_nlink != 1
                or final_descriptor_state.st_size != len(canonical_bytes)
            ):
                raise RuntimeError("published artifact metadata is not canonical")

            published_bytes = bytearray()
            while len(published_bytes) < len(canonical_bytes):
                try:
                    byte_chunk = os.read(
                        final_descriptor,
                        len(canonical_bytes) - len(published_bytes),
                    )
                except InterruptedError:
                    continue  # 검증 read도 signal interruption 뒤 같은 offset에서 이어 간다.
                if not byte_chunk:
                    break
                published_bytes.extend(byte_chunk)
            if bytes(published_bytes) != canonical_bytes:
                raise RuntimeError("published artifact bytes changed after link")
            if os.read(final_descriptor, 1):
                raise RuntimeError("published artifact contains trailing bytes")
        finally:
            os.close(final_descriptor)

        # 호출자에게 돌려줄 Path도 여전히 열어 둔 directory inode와 같은 final inode를 해석해야 한다.
        final_path_state = os.stat(trace_path, follow_symlinks=False)
        directory_path_state_after = os.stat(
            artifact_directory,
            follow_symlinks=False,
        )
        if (
            directory_path_state_after.st_dev,
            directory_path_state_after.st_ino,
        ) != (
            directory_descriptor_state.st_dev,
            directory_descriptor_state.st_ino,
        ):
            raise RuntimeError("artifact directory path changed after publication")
        if (
            directory_path_state_after.st_uid != os.geteuid()
            or directory_path_state_after.st_mode & 0o077
        ):
            raise PermissionError("artifact directory path is no longer owner-only")
        if (
            final_path_state.st_dev,
            final_path_state.st_ino,
        ) != (
            final_descriptor_state.st_dev,
            final_descriptor_state.st_ino,
        ):
            raise RuntimeError("artifact final path changed after publication")
        if final_path_state.st_nlink != 1:
            raise RuntimeError("artifact final path has an external hardlink")
    finally:
        cleanup_error: OSError | None = None
        if temporary_descriptor is not None:
            try:
                os.close(temporary_descriptor)
            except OSError as error:
                cleanup_error = error
        if temporary_exists:
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass  # Publish 전후 다른 cleanup 경로가 이미 이름을 회수했으면 멱등 종료한다.
            except OSError as error:
                if cleanup_error is None:
                    cleanup_error = error
        try:
            os.close(directory_descriptor)
        except OSError as error:
            if cleanup_error is None:
                cleanup_error = error
        if cleanup_error is not None:
            raise cleanup_error  # Body 실패와 무관하게 세 cleanup을 독립 시도하고 첫 cleanup 오류를 보존한다.

    return trace_path


def _write_trace_artifact(
    artifact_directory: Path,
    trace_body: Mapping[str, object],
    *,
    forbidden_values: Sequence[str],
) -> tuple[Path, str]:
    """
    함수 이름: _write_trace_artifact()
    기능: secret scan과 canonical digest를 통과한 trace만 mode 0600 새 파일로 원자 생성한다.
    인자: artifact_directory -> run별 mode 0700 artifact directory
        trace_body -> trace_sha256가 아직 없는 exact trace body
        forbidden_values -> 파일 어디에도 포함하면 안 되는 실제 credential 원문
    반환값: 생성한 trace 경로와 검증된 SHA-256 tuple
    작성 날짜: 2026/08/31
    """
    if not isinstance(trace_body, Mapping):
        raise TypeError("trace_body must be a mapping")

    # Bytes 생성까지 validator에 맡겨 secret·schema·digest 실패가 partial artifact를 만들지 않게 한다.
    sealed_trace = seal_actual_phase13_public_trace(
        trace_body,
        forbidden_values=forbidden_values,
    )
    validated_digest = validate_actual_phase13_public_trace(
        sealed_trace,
        forbidden_values=forbidden_values,
    )
    canonical_bytes = canonical_actual_phase13_public_trace_bytes(
        sealed_trace,
        forbidden_values=forbidden_values,
    )
    trace_path = _publish_new_artifact_bytes(
        artifact_directory,
        "phase13-public-case2-trace.json",
        canonical_bytes,
    )

    return trace_path, validated_digest  # stdout에는 검증된 digest만 전달하고 trace 본문은 반환하지 않는다.


def _kline_source_component(kline: Kline) -> str:
    """
    함수 이름: _kline_source_component()
    기능: public Kline을 production source_event_id와 같은 secret-free canonical component로 만든다.
    인자: kline -> MarketSnapshot version을 만든 typed Kline
    반환값: symbol, interval, open/event time과 close 상태 문자열
    작성 날짜: 2026/08/31
    """
    if not isinstance(kline, Kline) or kline.event_time is None:
        raise TypeError("kline must be a source Kline with an event time")

    # Production formatter를 직접 재사용해 0 microsecond 생략 규칙까지 같은 identity를 만든다.
    return MarketDataController._create_source_event_id(
        kline
    )  # OHLCV나 raw frame은 identity에 포함하지 않아 public provenance만 기록한다.


def _create_observed_market_event(
    runtime: ApplicationRuntime,
    *,
    sequence: int,
    after_market_version: int,
) -> tuple[int, tuple[dict[str, object], ...]]:
    """
    함수 이름: _create_observed_market_event()
    기능: 한 불변 시장 상태 상한 안의 실제 production 1L.1 trace만 정규화한다.
    인자: runtime -> 실제 public market runtime
        sequence -> 첫 normalized public market event 순서
        after_market_version -> 이미 수집한 마지막 production market version
    반환값: 실제로 수집한 마지막 version과 normalized event tuple
    작성 날짜: 2026/09/01
    """
    if not isinstance(runtime, ApplicationRuntime):
        raise TypeError("runtime must be an ApplicationRuntime")
    if type(sequence) is not int or sequence < 1:
        raise ValueError("sequence must be a positive integer")
    if type(after_market_version) is not int or after_market_version < 0:
        raise ValueError("after_market_version must be non-negative")

    # Application publication 경계에서 최신 시장 pointer, Context와 production trace를 한 번씩 고정한다.
    with runtime.application_lock:
        market_state = runtime.market_snapshot.get_snapshot()
        context_version = runtime.trading_controller.context.version
        boundary_trace = (
            runtime.trading_controller.public_market_boundary_trace
        )
    if type(market_state) is not MarketStateSnapshot:
        raise TypeError("market snapshot must return an exact state snapshot")
    if type(context_version) is not int or context_version < 0:
        raise TypeError("context version must be a non-negative integer")
    if not isinstance(boundary_trace, tuple):
        raise TypeError("public market boundary trace must be a tuple")

    # Snapshot publish→1L.1 observer 사이 gap은 무시하고 Controller가 실제 기록한 신규 1L.1만 선택한다.
    observed_boundaries: list[PublicMarketBoundaryTraceEntry] = []
    for boundary_entry in boundary_trace:
        if type(boundary_entry) is not PublicMarketBoundaryTraceEntry:
            raise TypeError("public market boundary trace contains an invalid entry")
        if (
            boundary_entry.message_id == "1L.1"
            and boundary_entry.market_version > after_market_version
        ):
            if (
                boundary_entry.market_version > market_state.version
                or boundary_entry.context_version > context_version
            ):
                raise RuntimeError(
                    "public market boundary exceeds captured runtime state"
                )
            observed_boundaries.append(boundary_entry)

    if not observed_boundaries:
        return after_market_version, ()
    if any(
        current_entry.market_version <= previous_entry.market_version
        for previous_entry, current_entry in zip(
            observed_boundaries,
            observed_boundaries[1:],
        )
    ):
        raise RuntimeError("public market boundary versions must increase")

    # Frozen production entry의 evaluation identity를 parser로 확인하고 NO_SIGNAL 전용 1L.1로만 축약한다.
    normalized_events: list[dict[str, object]] = []
    for event_offset, boundary_entry in enumerate(observed_boundaries):
        parsed_source = _parse_public_market_command_event(
            boundary_entry.evaluation_id
        )
        if (
            boundary_entry.source_event_id
            != parsed_source["source_event_id"]
            or boundary_entry.market_version
            != parsed_source["market_version"]
        ):
            raise RuntimeError("public market boundary provenance changed")
        normalized_events.append(
            {
                "sequence": sequence + event_offset,
                "message_id": boundary_entry.message_id,
                "event_type": boundary_entry.event_type,
                "source_event_id": parsed_source["source_event_id"],
                "source_kline_identity": parsed_source[
                    "source_kline_identity"
                ],
                "source_event_time": parsed_source["source_event_time"],
                "market_version": parsed_source["market_version"],
                "context_version": boundary_entry.context_version,
                "evaluation_id": None,
                "regime": boundary_entry.regime.value,
                "action_type": None,
                "side": None,
                "strategy": None,
            }
        )

    return (
        observed_boundaries[-1].market_version,
        tuple(normalized_events),
    )  # Cursor는 snapshot version이 아니라 실제 공개된 마지막 1L.1 version만 따라간다.


def _create_account_event_from_runtime(
    runtime: ApplicationRuntime,
    *,
    run_id: str,
    sequence: int,
    clock: Callable[[], datetime],
) -> dict[str, object]:
    """
    함수 이름: _create_account_event_from_runtime()
    기능: startup REST로 적용된 authoritative ETH balance/version을 secret-free public account event로 만든다.
    인자: runtime -> account READY actual Testnet runtime
        run_id -> source identity를 다른 run과 구분할 UUIDv4 문자열
        sequence -> trace public account event 순서
        clock -> Account state 읽기 뒤 같은 application lock에서 호출할 run clock
    반환값: exact account event dictionary
    작성 날짜: 2026/09/01
    """
    if not isinstance(runtime, ApplicationRuntime):
        raise TypeError("runtime must be an ApplicationRuntime")
    if type(sequence) is not int or sequence < 1:
        raise ValueError("sequence must be a positive integer")
    if not callable(clock):
        raise TypeError("clock must be callable")

    # Account pointer를 먼저 한 번 읽고 같은 application RLock에서 관찰 시각을 그 다음에 고정한다.
    with runtime.application_lock:
        account_state = runtime.account.get_snapshot()
        observed_at = clock()
    if type(account_state) is not AccountStateSnapshot:
        raise TypeError("account must return an exact state snapshot")
    observed_at_wire = _datetime_to_wire(observed_at)
    if (
        not account_state.ready
        or account_state.updated_at is None
        or account_state.version < 1
    ):
        raise RuntimeError("account must be ready before trace capture")
    eth_balance = account_state.balances.get("ETH")
    free_quantity = Decimal("0") if eth_balance is None else eth_balance.free
    locked_quantity = Decimal("0") if eth_balance is None else eth_balance.locked

    # 전체 account payload 대신 recovery에 필요한 ETH absolute balance와 version만 보존한다.
    return {
        "sequence": sequence,
        "message_id": "2",
        "event_type": "ACCOUNT_SNAPSHOT_APPLIED",
        "source_event_id": f"startup-account-{run_id}-{account_state.version}",
        "source_event_time": observed_at_wire,
        "account_version": account_state.version,
        "asset": "ETH",
        "free_quantity": _decimal_to_wire(free_quantity),
        "locked_quantity": _decimal_to_wire(locked_quantity),
    }


def _append_account_events_from_transport(
    account_events: list[dict[str, object]],
    event_dtos: Sequence[Mapping[str, object]],
) -> None:
    """
    함수 이름: _append_account_events_from_transport()
    기능: 실제 ACCOUNT_UPDATED DTO의 ETH balance와 version을 중복 없이 account trace에 추가한다.
    인자: account_events -> startup event부터 누적 중인 mutable normalized event list
        event_dtos -> BackendEventEnvelope.to_dto() 결과 sequence
    반환값: 없음
    작성 날짜: 2026/08/31
    """
    if not isinstance(account_events, list):
        raise TypeError("account_events must be a list")
    existing_account_versions: list[int] = []
    for account_event in account_events:
        if not isinstance(account_event, Mapping):
            raise TypeError("account_events must contain mappings")
        account_version = account_event.get("account_version")
        if type(account_version) is not int or account_version < 1:
            raise ValueError("account event has an invalid account version")
        existing_account_versions.append(account_version)
    latest_observed_version = max(
        existing_account_versions,
        default=0,
    )  # Producer가 만든 양의 정수 version만 replay 경계로 사용한다.

    # DTO mapper가 이미 Decimal/time을 wire 문자열로 바꿨으므로 raw account stream frame은 읽지 않는다.
    for event_dto in event_dtos:
        if event_dto.get("type") != "ACCOUNT_UPDATED":
            continue
        payload = event_dto.get("payload")
        account_payload = payload.get("account") if isinstance(payload, Mapping) else None
        if not isinstance(account_payload, Mapping):
            raise ValueError("ACCOUNT_UPDATED lacks a normalized account payload")
        account_version = account_payload.get("version")
        if type(account_version) is not int or account_version < 1:
            raise ValueError("ACCOUNT_UPDATED has an invalid account version")
        if account_version <= latest_observed_version:
            continue  # Startup보다 오래된 buffered 행과 같은 version 재전송을 모두 제거한다.
        balance_rows = account_payload.get("balances")
        if not isinstance(balance_rows, list):
            raise ValueError("ACCOUNT_UPDATED balances must be a list")
        eth_row = next(
            (
                balance_row
                for balance_row in balance_rows
                if isinstance(balance_row, Mapping)
                and balance_row.get("asset") == "ETH"
            ),
            None,
        )
        free_quantity = "0" if eth_row is None else eth_row.get("free")
        locked_quantity = "0" if eth_row is None else eth_row.get("locked")
        if not isinstance(free_quantity, str) or not isinstance(locked_quantity, str):
            raise ValueError("ACCOUNT_UPDATED ETH balances must be Decimal strings")
        source_event_id = event_dto.get("event_id")
        source_event_time = event_dto.get("occurred_at")
        if not isinstance(source_event_id, str) or not isinstance(source_event_time, str):
            raise ValueError("ACCOUNT_UPDATED lacks normalized event provenance")
        account_events.append(
            {
                "sequence": len(account_events) + 1,
                "message_id": "2.2.1",
                "event_type": "ACCOUNT_POSITION_APPLIED",
                "source_event_id": source_event_id,
                "source_event_time": source_event_time,
                "account_version": account_version,
                "asset": "ETH",
                "free_quantity": free_quantity,
                "locked_quantity": locked_quantity,
            }
        )
        latest_observed_version = account_version  # 후속에는 더 큰 Account version만 연결한다.


def _assert_atomic_trade_publications(event_dtos: Sequence[Mapping[str, object]]) -> None:
    """
    함수 이름: _assert_atomic_trade_publications()
    기능: 각 ORDER_EXECUTED가 바로 다음 sequence의 PERFORMANCE_UPDATED와 같은 시각에 발행됐는지 검증한다.
    인자: event_dtos -> 실제 BackendEventEnvelope.to_dto() sequence
    반환값: 없음
    작성 날짜: 2026/08/31
    """
    # ORDER_EXECUTED만 골라 바로 뒤 envelope의 type·sequence·occurred_at을 한번에 결속한다.
    for event_index, event_dto in enumerate(event_dtos):
        if event_dto.get("type") != "ORDER_EXECUTED":
            continue  # Trade publication이 아닌 event는 pair 검사 대상에서 제외한다.
        if event_index + 1 >= len(event_dtos):
            raise AssertionError("ORDER_EXECUTED lacks its atomic performance event")
        performance_event = event_dtos[event_index + 1]
        if (
            performance_event.get("type") != "PERFORMANCE_UPDATED"
            or performance_event.get("sequence") != event_dto.get("sequence") + 1
            or performance_event.get("occurred_at") != event_dto.get("occurred_at")
        ):
            raise AssertionError("trade publication pair is not atomic and consecutive")


def _metadata_decimal(order_metadata: Mapping[str, object], field_name: str) -> Decimal:
    """
    함수 이름: _metadata_decimal()
    기능: strict pending UPSERT의 금융 문자열 하나를 유한 Decimal로 다시 검증한다.
    인자: order_metadata -> exact v4 Order metadata mapping
        field_name -> 읽을 금융 field 이름
    반환값: exponent나 float를 거치지 않은 finite Decimal
    작성 날짜: 2026/08/31
    """
    if not isinstance(order_metadata, Mapping):
        raise TypeError("order_metadata must be a mapping")
    raw_value = order_metadata.get(field_name)
    if not isinstance(raw_value, str) or not raw_value or raw_value != raw_value.strip():
        raise ValueError("pending Order financial field must be canonical text")
    try:
        decimal_value = Decimal(raw_value)
    except InvalidOperation as error:
        raise ValueError("pending Order financial field must be a Decimal") from error
    if not decimal_value.is_finite():
        raise ValueError("pending Order financial field must be finite")

    return decimal_value  # Trace에는 아래 canonical plain serializer를 거친 값만 사용한다.


def _require_zero_position_quantity(position_quantity: object) -> None:
    """
    함수 이름: _require_zero_position_quantity()
    기능: 실제 Position 수량을 출력하지 않고 exact Decimal zero인지 검증한다.
    인자: position_quantity -> runtime Position에서 읽은 수량
    반환값: exact Decimal zero이면 없음
    작성 날짜: 2026/08/31
    """
    # Type drift와 non-zero exposure를 같은 고정 문장으로 닫아 balance repr를 숨긴다.
    if (
        type(position_quantity) is not Decimal
        or position_quantity != Decimal("0")
    ):
        raise AssertionError(_ZERO_POSITION_FAILURE_MESSAGE)

    return None  # 검증한 실제 수량은 unittest result나 trace에 새로 복제하지 않는다.


def _require_zero_market_buy_commission(
    commission_policy: CommissionDiscountPolicy,
) -> None:
    """
    함수 이름: _require_zero_market_buy_commission()
    기능: 실제 account MARKET BUY 수수료율을 출력하지 않고 모두 zero인지 검증한다.
    인자: commission_policy -> signed account 응답에서 정규화한 수수료 정책
    반환값: 네 MARKET BUY 관련 비율이 모두 zero이면 없음
    작성 날짜: 2026/08/31
    """
    # Exact policy와 네 derived Decimal 비율을 한 번에 검사해 어느 실제 값도 실패문에 넣지 않는다.
    if type(commission_policy) is not CommissionDiscountPolicy or any(
        commission_rate != Decimal("0")
        for commission_rate in (
            commission_policy.standard_market_buy_rate,
            commission_policy.special_market_buy_rate,
            commission_policy.tax_market_buy_rate,
            commission_policy.market_buy_received_asset_commission_rate,
        )
    ):
        raise AssertionError(_ZERO_MARKET_BUY_COMMISSION_FAILURE_MESSAGE)

    return None  # 성공 여부만 남기고 account-specific rate 원문은 출력하지 않는다.


def _create_failure_v2_test_body() -> dict[str, object]:
    """
    함수 이름: _create_failure_v2_test_body()
    기능: v2 schema 상관·drift 테스트가 공유할 exact INCOMPLETE body를 만든다.
    인자: 없음
    반환값: 두 ID field가 없고 timestamp·error truth가 일치하는 독립 mapping
    작성 날짜: 2026/08/31
    """
    started_at = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    first_attempted_at = started_at + timedelta(seconds=10)
    second_attempted_at = started_at + timedelta(seconds=20)
    completed_at = started_at + timedelta(seconds=30)

    # BUY→SELL 시간 순서는 포함하되 logical·exchange ID는 v2 mapping에 아예 생성하지 않는다.
    return {
        "schema_version": 2,
        "record_type": _PHASE13_FAILURE_EVIDENCE_RECORD_TYPE,
        "outcome": "FAILED",
        "typed_reason": "ACTUAL_RUNTIME_FAILED",
        "run_id": "00000000-0000-4000-8000-000000000041",
        "timestamps": {
            "started_at": _datetime_to_wire(started_at),
            "completed_at": _datetime_to_wire(completed_at),
        },
        "mutation_guard": {
            "mutation_started": True,
            "submissions_blocked": True,
            "submission_attempts": [
                {
                    "sequence": 1,
                    "symbol": "ETHUSDT",
                    "side": "BUY",
                    "order_type": "MARKET",
                    "submission_attempt": 0,
                    "attempted_at": _datetime_to_wire(first_attempted_at),
                },
                {
                    "sequence": 2,
                    "symbol": "ETHUSDT",
                    "side": "SELL",
                    "order_type": "MARKET",
                    "submission_attempt": 0,
                    "attempted_at": _datetime_to_wire(second_attempted_at),
                },
            ],
        },
        "recovery": {
            "attempted": False,
            "outcome": "SKIPPED",
            "typed_reason": "FAILURE_RECOVERY_STATE_AMBIGUOUS",
        },
        "runtime_state": {
            "application_status": "READY",
            "trading_status": "RECONCILIATION_REQUIRED",
            "reconciliation_required": True,
            "position_quantity": "1",
            "pending_order_count": 1,
            "durable_trade_count": 1,
        },
        "fresh_verification": {
            "status": "INCOMPLETE",
            "typed_reason": "FRESH_VERIFICATION_FAILED",
            "failure_stage": "RUNTIME_CREATION",
            "verified_at": None,
            "position_quantity": None,
            "pending_order_count": None,
            "reconciliation_required": None,
            "matching_open_order_count": None,
            "account_open_orders_empty": None,
            "account_open_order_lists_empty": None,
            "run_exchange_order_count": None,
            "durable_trade_count": None,
        },
        "first_cause": {
            "reconciliation_required": True,
            "status": ReconciliationCauseStatus.EXACT.value,
            "category": (
                ReconciliationCauseCategory.EVENT_WORKER_OR_RUNTIME_FAILED.value
            ),
        },
        "evidence_errors": [
            "FAILURE_RECOVERY_STATE_AMBIGUOUS",
            "FRESH_VERIFICATION_FAILED",
        ],
    }


@unittest.skipUnless(os.name == "posix", "historical evidence harness requires POSIX filesystem primitives")
class PhaseThirteenPublicHarnessHelperTests(unittest.TestCase):
    """
    클래스 이름: PhaseThirteenPublicHarnessHelperTests
    기능: network 없이 actual harness의 source, journal, UI와 NO_SIGNAL evidence 경계를 검증한다.
    작성 날짜: 2026/08/31
    """

    def test_kline_source_component_matches_production_datetime_precision(
        self,
    ) -> None:
        """
        함수 이름: test_kline_source_component_matches_production_datetime_precision()
        기능: 정각 시각을 포함한 fixture source ID가 production canonical ID와 exact 일치하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/05
        """
        source_kline = Kline(
            symbol="ETHUSDT",
            interval=Interval.THIRTY_MINUTES,
            open_time=datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc),
            open=Decimal("2400"),
            high=Decimal("2410"),
            low=Decimal("2390"),
            close=Decimal("2405"),
            volume=Decimal("1"),
            closed=False,
            event_time=datetime(2026, 9, 5, 0, 1, tzinfo=timezone.utc),
        )

        # Production은 microsecond가 0인 시각에 소수부를 붙이지 않으므로 helper도 동일해야 한다.
        self.assertEqual(
            MarketDataController._create_source_event_id(source_kline),
            _kline_source_component(source_kline),
        )

    def test_deterministic_kline_delivery_guard_blocks_live_callback_until_cleanup(
        self,
    ) -> None:
        """
        함수 이름: test_deterministic_kline_delivery_guard_blocks_live_callback_until_cleanup()
        기능: actual fixture guard가 concurrent live Kline 전달을 막고 cleanup 뒤 정확히 해제하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/04
        """
        delivery_lock = RLock()
        web_socket_gateway = SimpleNamespace(
            _kline_delivery_lock=delivery_lock
        )
        callback_started = Event()
        callback_completed = Event()

        def emulate_live_kline_callback() -> None:
            """
            함수 이름: emulate_live_kline_callback()
            기능: Gateway callback thread처럼 같은 delivery lock 뒤에서 완료 신호를 기록한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/09/04
            """
            callback_started.set()
            with delivery_lock:
                callback_completed.set()  # Guard 해제 뒤에만 live callback 완료가 공개된다.

        delivery_guard = _acquire_deterministic_kline_delivery_guard(
            web_socket_gateway
        )
        callback_thread = Thread(
            target=emulate_live_kline_callback,
            daemon=True,
        )
        try:
            # Callback이 lock 획득을 시도한 뒤에도 fixture guard가 잡혀 있으면 완료할 수 없다.
            callback_thread.start()
            self.assertTrue(callback_started.wait(timeout=1))
            self.assertFalse(callback_completed.wait(timeout=0.05))
        finally:
            _release_deterministic_kline_delivery_guard(delivery_guard)

        self.assertTrue(callback_completed.wait(timeout=1))
        callback_thread.join(timeout=1)
        self.assertFalse(callback_thread.is_alive())  # Cleanup 뒤 blocked callback thread가 남지 않아야 한다.

    def test_deterministic_buy_candidate_respects_account_limit_and_notional(
        self,
    ) -> None:
        """
        함수 이름: test_deterministic_buy_candidate_respects_account_limit_and_notional()
        기능: BUY candidate가 base MAX_ASSET에는 split을 줄이고 free·NOTIONAL 불가능 상태는 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/04
        """
        lot_size_filter = QuantityFilter(
            filter_type="LOT_SIZE",
            minimum_quantity=Decimal("0.0001"),
            maximum_quantity=Decimal("100"),
            step_size=Decimal("0.0001"),
        )
        market_lot_size_filter = QuantityFilter(
            filter_type="MARKET_LOT_SIZE",
            minimum_quantity=Decimal("0"),
            maximum_quantity=Decimal("100"),
            step_size=Decimal("0"),
        )
        notional_filter = NotionalFilter(
            filter_type="NOTIONAL",
            minimum_notional=Decimal("5"),
            maximum_notional=Decimal("9000000"),
            apply_minimum_to_market=True,
            apply_maximum_to_market=False,
            average_price_minutes=5,
        )
        public_filters = AccountRelevantFilters(
            symbol="ETHUSDT",
            exchange_order_count_filters=(),
            symbol_order_count_filters=(),
            symbol_quantity_filters=(
                lot_size_filter,
                market_lot_size_filter,
            ),
            symbol_notional_filters=(notional_filter,),
            symbol_maximum_position=None,
            passive_symbol_filter_types=frozenset(),
            asset_filters=(),
        )
        symbol_rules = SymbolTradingRules(
            symbol="ETHUSDT",
            status="TRADING",
            base_asset="ETH",
            quote_asset="USDT",
            base_asset_precision=8,
            order_types=frozenset({"MARKET"}),
            is_spot_trading_allowed=True,
            lot_size=lot_size_filter,
            market_lot_size=market_lot_size_filter,
            notional_filters=(notional_filter,),
            maximum_position=None,
            public_relevant_filters=public_filters,
        )
        reference_price = ReferencePrice(
            symbol="ETHUSDT",
            price=Decimal("2450"),
            exchange_timestamp=1,
        )

        def create_account_filters(
            maximum_quantity: Decimal,
        ) -> AccountRelevantFilters:
            """
            함수 이름: create_account_filters()
            기능: 한 base MAX_ASSET limit을 가진 signed account filter 합성값을 만든다.
            인자: maximum_quantity -> 한 주문에서 허용할 최대 ETH 수량
            반환값: candidate 검증용 AccountRelevantFilters
            작성 날짜: 2026/09/04
            """
            return AccountRelevantFilters(
                symbol="ETHUSDT",
                exchange_order_count_filters=(),
                symbol_order_count_filters=(),
                symbol_quantity_filters=(),
                symbol_notional_filters=(),
                symbol_maximum_position=None,
                passive_symbol_filter_types=frozenset(),
                asset_filters=(
                    AccountAssetFilter(
                        filter_type="MAX_ASSET",
                        asset="ETH",
                        maximum_quantity=maximum_quantity,
                    ),
                ),
            )

        # 0.003 ETH account limit은 10 USDT outer cap보다 작지만 NOTIONAL 5 이상이므로 split 축소로 통과한다.
        candidate = _prepare_deterministic_buy_candidate(
            free_quote_quantity=Decimal("100"),
            decision_price=Decimal("2450"),
            maximum_notional=Decimal("10"),
            symbol_rules=symbol_rules,
            account_filters=create_account_filters(Decimal("0.003")),
            reference_price=reference_price,
        )
        self.assertEqual(Decimal("0.0735"), candidate.scale_in)
        self.assertEqual(Decimal("0.003"), candidate.submitted_quantity)
        self.assertEqual(Decimal("7.350"), candidate.decision_notional)
        self.assertEqual(Decimal("7.350"), candidate.reference_notional)

        # 0.001 ETH limit과 zero quote는 최소 MARKET notional을 만들 수 없어 session 시작 전에 닫힌다.
        with self.assertRaises(SymbolFilterError):
            _prepare_deterministic_buy_candidate(
                free_quote_quantity=Decimal("100"),
                decision_price=Decimal("2450"),
                maximum_notional=Decimal("10"),
                symbol_rules=symbol_rules,
                account_filters=create_account_filters(Decimal("0.001")),
                reference_price=reference_price,
            )
        with self.assertRaises(ValueError):
            _prepare_deterministic_buy_candidate(
                free_quote_quantity=Decimal("0"),
                decision_price=Decimal("2450"),
                maximum_notional=Decimal("10"),
                symbol_rules=symbol_rules,
                account_filters=create_account_filters(Decimal("1")),
                reference_price=reference_price,
            )

    def test_reconciliation_failure_exposes_only_typed_order_diagnostic(
        self,
    ) -> None:
        """
        함수 이름: test_reconciliation_failure_exposes_only_typed_order_diagnostic()
        기능: CONFLICT failure가 raw 예외 없이 마지막 typed order failure code만 출력하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/04
        """
        cause_snapshot = ReconciliationCauseSnapshot(
            reconciliation_required=True,
            status=ReconciliationCauseStatus.CONFLICT,
            category=None,
        )
        failure = _PhaseThirteenReconciliationFailure(
            cause_snapshot,
            OrderExecutionFailureCode.SYMBOL_FILTER_REJECTED,
        )

        # Assertion 문자열에는 typed enum만 있고 filter payload·quantity·credential 입력 위치는 없다.
        self.assertIs(cause_snapshot, failure.cause_snapshot)
        self.assertIs(
            OrderExecutionFailureCode.SYMBOL_FILTER_REJECTED,
            failure.order_failure_code,
        )
        self.assertEqual(
            "public Case 2 entered reconciliation before a durable BUY; "
            "order_failure_code=SYMBOL_FILTER_REJECTED",
            str(failure),
        )

    def test_reconciliation_failure_exposes_exact_cause_category(self) -> None:
        """
        함수 이름: test_reconciliation_failure_exposes_exact_cause_category()
        기능: EXACT failure가 raw 원문 없이 stable cause category를 출력하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/04
        """
        cause_snapshot = ReconciliationCauseSnapshot(
            reconciliation_required=True,
            status=ReconciliationCauseStatus.EXACT,
            category=ReconciliationCauseCategory.EVENT_WORKER_OR_RUNTIME_FAILED,
        )
        worker_failure_snapshot = _TradingEventRuntimeFailureSnapshot(
            stage=_TradingEventRuntimeFailureStage.RUNTIME_CYCLE,
            exception_type="RuntimeError",
            exception_origin=(
                "binance_auto_trader.application.trading_controller:"
                "run_event_runtime_cycle"
            ),
        )

        # 외부에 노출되는 문구는 원문 대신 schema에 고정된 enum value만 포함한다.
        failure = _PhaseThirteenReconciliationFailure(
            cause_snapshot,
            worker_failure_snapshot=worker_failure_snapshot,
        )
        self.assertIs(cause_snapshot, failure.cause_snapshot)
        self.assertIsNone(failure.order_failure_code)
        self.assertIs(
            worker_failure_snapshot,
            failure.worker_failure_snapshot,
        )
        self.assertEqual(
            "public Case 2 entered reconciliation before a durable BUY; "
            "cause_category=EVENT_WORKER_OR_RUNTIME_FAILED; "
            "worker_failure_stage=RUNTIME_CYCLE; "
            "worker_failure_type=RuntimeError; "
            "worker_failure_origin=binance_auto_trader.application."
            "trading_controller:run_event_runtime_cycle",
            str(failure),
        )

    def test_reconciliation_failure_exposes_only_normalized_market_diagnostic(
        self,
    ) -> None:
        """
        함수 이름: test_reconciliation_failure_exposes_only_normalized_market_diagnostic()
        기능: 시장 실패 assertion이 raw 원문 없이 reason·type·package origin만 출력하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/04
        """
        cause_snapshot = ReconciliationCauseSnapshot(
            reconciliation_required=True,
            status=ReconciliationCauseStatus.EXACT,
            category=ReconciliationCauseCategory.MARKET_STREAM_FAILED,
        )
        market_failure_snapshot = _PhaseThirteenMarketFailureSnapshot(
            reason=_PhaseThirteenMarketFailureReason.KLINE_STREAM_INVALID,
            exception_type="ValueError",
            exception_origin=(
                "binance_auto_trader.application.market_data_controller:"
                "_observe_validated_kline"
            ),
        )

        # Stable diagnostic 세 필드만 assertion suffix에 포함하고 raw payload 위치는 만들지 않는다.
        failure = _PhaseThirteenReconciliationFailure(
            cause_snapshot,
            market_failure_snapshot=market_failure_snapshot,
        )
        self.assertIs(
            market_failure_snapshot,
            failure.market_failure_snapshot,
        )
        self.assertEqual(
            "public Case 2 entered reconciliation before a durable BUY; "
            "cause_category=MARKET_STREAM_FAILED; "
            "market_failure_reason=KLINE_STREAM_INVALID; "
            "market_failure_type=ValueError; "
            "market_failure_origin=binance_auto_trader.application."
            "market_data_controller:_observe_validated_kline",
            str(failure),
        )

    def test_market_failure_reader_distinguishes_invalid_from_disconnect(
        self,
    ) -> None:
        """
        함수 이름: test_market_failure_reader_distinguishes_invalid_from_disconnect()
        기능: Gateway buffer error 유무가 invalid와 disconnected 진단으로 안전하게 구분되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/04
        """
        gateway = SimpleNamespace(
            _lock=RLock(),
            _buffer_error=ValueError("credential-like-kline-payload-canary"),
        )
        runtime = object.__new__(ApplicationRuntime)
        object.__setattr__(runtime, "web_socket_gateway", gateway)

        # Raw error가 있으면 type만 남기고 test module origin과 canary 원문은 모두 축약한다.
        invalid_snapshot = _read_phase13_market_failure_snapshot(runtime)
        self.assertIs(
            invalid_snapshot.reason,
            _PhaseThirteenMarketFailureReason.KLINE_STREAM_INVALID,
        )
        self.assertEqual("ValueError", invalid_snapshot.exception_type)
        self.assertEqual(
            "EXTERNAL_OR_UNKNOWN",
            invalid_snapshot.exception_origin,
        )
        self.assertNotIn("credential-like", repr(invalid_snapshot))

        # Transport disconnect는 raw buffer error가 없으므로 optional 예외 진단을 합성하지 않는다.
        gateway._buffer_error = None
        disconnected_snapshot = _read_phase13_market_failure_snapshot(runtime)
        self.assertIs(
            disconnected_snapshot.reason,
            (
                _PhaseThirteenMarketFailureReason.KLINE_STREAM_DISCONNECTED_OR_UNAVAILABLE
            ),
        )
        self.assertIsNone(disconnected_snapshot.exception_type)
        self.assertIsNone(disconnected_snapshot.exception_origin)

    def test_actual_injection_waits_for_each_prior_evaluation_before_next_kline(
        self,
    ) -> None:
        """
        함수 이름: test_actual_injection_waits_for_each_prior_evaluation_before_next_kline()
        기능: actual fixture가 이전 평가 commit 뒤에만 다음 occurred_at Kline을 생성하는 순서를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/04
        """
        open_time = datetime(2026, 9, 4, 5, 0, tzinfo=timezone.utc)
        fixture_klines = tuple(
            Kline(
                symbol="ETHUSDT",
                interval=Interval.THIRTY_MINUTES,
                open_time=open_time,
                open=Decimal("2450"),
                high=Decimal("2451"),
                low=Decimal("2440"),
                close=Decimal(2445 + sequence),
                volume=Decimal("1"),
                closed=False,
                event_time=open_time + timedelta(seconds=sequence),
            )
            for sequence in (1, 2, 3)
        )
        deterministic_klines = DeterministicPublicCase2Klines(
            setup=fixture_klines[0],
            flush=fixture_klines[1],
            recovery=fixture_klines[2],
        )
        observed_order: list[tuple[str, Kline | None]] = []

        def observe_kline(kline: Kline) -> None:
            """
            함수 이름: observe_kline()
            기능: actual helper가 전달한 Kline 순서를 기록한다.
            인자: kline -> 전달된 public Kline
            반환값: 없음
            작성 날짜: 2026/09/04
            """
            observed_order.append(("observe", kline))

        harness = object.__new__(
            BinanceTestnetPhaseThirteenPublicMarketCase2Tests
        )
        harness.runtime = SimpleNamespace(
            market_data_controller=SimpleNamespace(
                observe_kline=observe_kline,
            ),
        )
        harness._wait_for_public_market_evaluation = Mock(
            side_effect=lambda kline: observed_order.append(("wait", kline))
        )
        harness._wait_for_public_buy = Mock(
            side_effect=lambda: observed_order.append(("wait_buy", None))
            or None
        )

        # SETUP·FLUSH는 각각 commit을 기다리고 RECOVERY 뒤에만 BUY waiter로 넘어가야 한다.
        result = harness._inject_deterministic_public_case2_and_wait_for_buy(
            deterministic_klines
        )
        self.assertIsNone(result)
        self.assertEqual(
            [
                ("observe", fixture_klines[0]),
                ("wait", fixture_klines[0]),
                ("observe", fixture_klines[1]),
                ("wait", fixture_klines[1]),
                ("observe", fixture_klines[2]),
                ("wait_buy", None),
            ],
            observed_order,
        )

    def test_all_client_preflight_canaries_block_actual_without_repr(
        self,
    ) -> None:
        """
        함수 이름: test_all_client_preflight_canaries_block_actual_without_repr()
        기능: manual open/recent canary가 actual 진입 전에 막히고 unittest 출력에 identity를 남기지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        canary_instant = datetime(
            2026, 8, 31, tzinfo=timezone.utc
        )  # 출력 redaction을 반복 검증할 공개 가능한 고정 시각이다.
        canary_fill = Fill(
            exchange_order_id="99887766",
            trade_id="manual-fill-canary",
            quantity=Decimal("0.01"),
            price=Decimal("4321.12345678"),
            fee_amount=Decimal("0"),
            fee_asset="USDT",
            fee_quote_amount=Decimal("0"),
            executed_at=canary_instant,
        )
        canary_result = OrderResult(
            symbol="ETHUSDT",
            client_order_id="manual-client-canary",
            status=OrderStatus.FILLED,
            processed_at=canary_instant,
            exchange_order_id=canary_fill.exchange_order_id,
            fills=(canary_fill,),
        )
        baseline_trade = make_trade(order_id="7401")
        matching_baseline_result = OrderResult(
            symbol="ETHUSDT",
            client_order_id=baseline_trade.client_order_id,
            status=OrderStatus.FILLED,
            processed_at=canary_instant,
            exchange_order_id=baseline_trade.order_id,
        )
        preflight_cases = (
            (
                "open",
                (canary_result,),
                (matching_baseline_result,),
                "Testnet preflight requires zero open orders",
                False,
            ),
            (
                "recent",
                (),
                (matching_baseline_result, canary_result),
                "Testnet recent orders do not match verified closed history",
                True,
            ),
        )
        for (
            case_name,
            open_results,
            recent_results,
            fixed_message,
            recent_query_expected,
        ) in preflight_cases:
            with self.subTest(case_name=case_name):
                update_split_ratios = Mock()
                start_trading = Mock()
                set_regime_type = Mock()
                controller = SimpleNamespace(
                    position=SimpleNamespace(quantity=Decimal("0")),
                    reconciliation_required=False,
                    context=SimpleNamespace(version=0),
                    update_split_ratios=update_split_ratios,
                    start_trading=start_trading,
                )
                api_gateway = Mock()
                api_gateway.list_all_open_order_results.return_value = (
                    open_results
                )
                api_gateway.list_all_recent_order_results.return_value = (
                    recent_results
                )
                runtime = SimpleNamespace(
                    market_snapshot=SimpleNamespace(ready=True, version=1),
                    account=SimpleNamespace(ready=True),
                    market_data_controller=SimpleNamespace(
                        market_available=True
                    ),
                    web_socket_gateway=SimpleNamespace(
                        kline_live_ready=True,
                        account_ready=True,
                    ),
                    trading_controller=controller,
                    trade_history_controller=SimpleNamespace(
                        get_pending_orders=Mock(return_value=())
                    ),
                    trade_history=SimpleNamespace(trades=(baseline_trade,)),
                    api_gateway=api_gateway,
                    regime_controller=SimpleNamespace(
                        set_regime_type=set_regime_type
                    ),
                )
                harness = (
                    BinanceTestnetPhaseThirteenPublicMarketCase2Tests(
                        "test_actual_public_market_case2_buy_and_exact_stop_recovery"
                    )
                )
                harness.runtime = runtime
                harness.baseline_trades = (baseline_trade,)
                harness.baseline_recent_exchange_order_ids = None
                harness.public_account_events = []
                harness.run_id = "00000000-0000-4000-8000-000000000041"
                harness.evidence_clock = (
                    lambda: canary_instant
                )  # Preflight fixture도 actual run과 같은 시계 seam을 제공한다.

                def run_production_actual_entry() -> None:
                    """
                    함수 이름: run_production_actual_entry()
                    기능: production actual orchestration을 canary preflight 결과로 실행한다.
                    인자: 없음
                    반환값: 없음
                    작성 날짜: 2026/08/31
                    """
                    # Production 순서를 직접 지나 all-client gate 뒤 mutation seam이 닫히는지 확인한다.
                    harness._execute_actual_public_market_case2()  # 실제 preflight 순서를 호출한다.

                # 독립 unittest failure를 캡처해 fixed 문장과 production mutation-before-gate 부재를 증명한다.
                failure_output = StringIO()
                with patch(
                    f"{__name__}.start_application",
                    return_value=SimpleNamespace(
                        status=ApplicationStatus.READY
                    ),
                ), patch(
                    f"{__name__}._create_account_event_from_runtime",
                    return_value=SimpleNamespace(),
                ):
                    failure_result = unittest.TextTestRunner(
                        stream=failure_output,
                        verbosity=2,
                        failfast=True,
                    ).run(
                        unittest.FunctionTestCase(
                            run_production_actual_entry
                        )
                    )
                self.assertFalse(failure_result.wasSuccessful())
                set_regime_type.assert_not_called()
                update_split_ratios.assert_not_called()
                start_trading.assert_not_called()

                # Controller 우회 direct REST mutation도 all-client gate 전에 한 번도 허용하지 않는다.
                api_gateway.submit_order.assert_not_called()
                api_gateway.sell_all_position.assert_not_called()
                api_gateway.cancel_order.assert_not_called()  # 세 mutation surface를 모두 0회로 고정한다.
                self.assertEqual(
                    int(recent_query_expected),
                    api_gateway.list_all_recent_order_results.call_count,
                )
                rendered_failure = failure_output.getvalue()
                self.assertIn(fixed_message, rendered_failure)
                for sensitive_canary in (
                    "manual-client-canary",
                    "99887766",
                    "manual-fill-canary",
                    "4321.12345678",
                    baseline_trade.client_order_id,
                    baseline_trade.order_id,
                ):
                    self.assertNotIn(sensitive_canary, rendered_failure)

    def test_sensitive_actual_numeric_guards_use_fixed_failure_output(
        self,
    ) -> None:
        """
        함수 이름: test_sensitive_actual_numeric_guards_use_fixed_failure_output()
        기능: Position과 commission 실패가 실제 Decimal 값을 unittest 출력에 반사하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        nonzero_commission_policy = CommissionDiscountPolicy(
            symbol="ETHUSDT",
            enabled_for_account=False,
            enabled_for_symbol=False,
            discount_asset="USDT",
            discount_rate=Decimal("0"),
            standard_market_buy_rate=Decimal("0.87654321"),
            special_market_buy_rate=Decimal("0"),
            tax_market_buy_rate=Decimal("0"),
        )

        # 두 민감 Decimal canary를 독립 failure로 실행해 traceback 전체의 고정 문장 경계를 확인한다.
        guarded_cases = (
            (
                lambda: _require_zero_position_quantity(
                    Decimal("7654.321098")
                ),
                _ZERO_POSITION_FAILURE_MESSAGE,
                "7654.321098",
            ),
            (
                lambda: _require_zero_market_buy_commission(
                    nonzero_commission_policy
                ),
                _ZERO_MARKET_BUY_COMMISSION_FAILURE_MESSAGE,
                "0.87654321",
            ),
        )
        for guarded_check, fixed_message, forbidden_value in guarded_cases:
            with self.subTest(fixed_message=fixed_message):

                def run_guarded_check() -> None:
                    """
                    함수 이름: run_guarded_check()
                    기능: 민감 수치 guard 하나를 독립 unittest failure 경계에서 호출한다.
                    인자: 없음
                    반환값: 없음
                    작성 날짜: 2026/08/31
                    """
                    guarded_check()  # Production과 같은 fixed-message helper를 직접 실행한다.

                failure_output = StringIO()
                failure_result = unittest.TextTestRunner(
                    stream=failure_output,
                    verbosity=2,
                    failfast=True,
                ).run(unittest.FunctionTestCase(run_guarded_check))
                captured_output = failure_output.getvalue()

                # 실패 사실과 generic message만 관찰하고 Position·rate canary는 출력 전체에서 금지한다.
                self.assertFalse(failure_result.wasSuccessful())
                self.assertIn(fixed_message, captured_output)
                self.assertNotIn(
                    forbidden_value,
                    captured_output,
                )  # Synthetic Decimal 원문조차 stderr contract 밖으로 내보내지 않는다.

    def test_public_market_command_parser_accepts_single_and_atomic_sources(self) -> None:
        """
        함수 이름: test_public_market_command_parser_accepts_single_and_atomic_sources()
        기능: 단일 Kline과 atomic batch에서 30분 public provenance를 정확히 선택하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        single_source = (
            "market:7:kline:ETHUSDT:30m:2026-08-31T00:00:00Z:"
            "2026-08-31T00:01:00.123456Z:open"
        )
        batch_source = (
            "market:8:kline-batch|"
            "kline:ETHUSDT:1m:2026-08-31T00:29:00Z:2026-08-31T00:30:00Z:closed|"
            "kline:ETHUSDT:30m:2026-08-31T00:00:00Z:2026-08-31T00:30:01Z:closed"
        )

        # Batch의 첫 source가 1분이어도 Case 2 decision identity는 포함된 30분 source를 선택한다.
        parsed_single = _parse_public_market_command_event(single_source)
        parsed_batch = _parse_public_market_command_event(batch_source)
        self.assertEqual(7, parsed_single["market_version"])
        self.assertEqual(
            "ETHUSDT:30m:2026-08-31T00:00:00Z",
            parsed_single["source_kline_identity"],
        )
        self.assertEqual(8, parsed_batch["market_version"])
        self.assertEqual(
            "2026-08-31T00:30:01Z",
            parsed_batch["source_event_time"],
        )

    def test_public_market_command_parser_rejects_private_or_malformed_sources(self) -> None:
        """
        함수 이름: test_public_market_command_parser_rejects_private_or_malformed_sources()
        기능: private intent ID, noncanonical version과 raw-looking source를 public evidence로 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        invalid_sources = (
            "phase13-private-intent",
            "market:01:kline:ETHUSDT:30m:2026-08-31T00:00:00Z:2026-08-31T00:01:00Z:open",
            "market:1:raw-payload",
        )

        # 각 실패는 threshold나 Action seam으로 fallback하지 않고 public provenance 자체를 차단한다.
        for invalid_source in invalid_sources:
            with self.subTest(invalid_source=invalid_source[:12]):
                with self.assertRaises((TypeError, ValueError)):
                    _parse_public_market_command_event(invalid_source)

    def test_pending_upsert_extraction_is_exact_and_deduplicated(self) -> None:
        """
        함수 이름: test_pending_upsert_extraction_is_exact_and_deduplicated()
        기능: current v4 UPSERT를 읽고 동일 crash duplicate만 한 attempt로 수렴하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        order_metadata = {
            "client_order_id": "bat-p13-client-1",
            "exit_reason": None,
            "exit_pct_b_at_intent": None,
            "intent_id": "intent-1",
            "market_price_at_decision": "2500",
            "regime_type": "TYPE_0",
            "requested_quantity": "0.04",
            "risk_policy_version": 13,
            "side": "BUY",
            "strategy": "CASE_C",
            "submission_attempt": 0,
            "submitted_quantity": "0.04",
            "symbol": "ETHUSDT",
        }
        upsert_event = {
            "schema_version": 4,
            "record_type": "pending_order_event",
            "operation": "UPSERT",
            "lifecycle": "PREPARED",
            "order": order_metadata,
        }
        transition_event = {
            "schema_version": 4,
            "record_type": "pending_order_event",
            "operation": "TRANSITION",
            "client_order_id": "bat-p13-client-1",
            "lifecycle": "TERMINAL",
        }
        with TemporaryDirectory() as temporary_directory:
            pending_path = Path(temporary_directory) / "pending.jsonl"
            encoded_lines = (
                json.dumps(upsert_event, separators=(",", ":")),
                json.dumps(upsert_event, separators=(",", ":")),
                json.dumps(transition_event, separators=(",", ":")),
            )
            pending_path.write_text(
                "".join(f"{encoded_line}\n" for encoded_line in encoded_lines),
                encoding="utf-8",
            )

            # Lifecycle line은 metadata를 바꾸지 않고 byte-identical UPSERT 두 개는 한 실제 submit이다.
            extracted_orders = _extract_pending_order_upserts(pending_path)
            self.assertEqual(1, len(extracted_orders))
            self.assertEqual(order_metadata, extracted_orders[0])

    def test_pending_upsert_extraction_rejects_unknown_and_conflicting_metadata(self) -> None:
        """
        함수 이름: test_pending_upsert_extraction_rejects_unknown_and_conflicting_metadata()
        기능: unknown Order field와 같은 client ID의 수량 drift를 actual evidence 전에 차단한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        # Exact v4 PREPARED Order를 기준으로 unknown field와 same-client 수량 drift를 독립 생성한다.
        base_order = {
            "client_order_id": "bat-p13-client-1",
            "exit_reason": None,
            "exit_pct_b_at_intent": None,
            "intent_id": "intent-1",
            "market_price_at_decision": "2500",
            "regime_type": "TYPE_0",
            "requested_quantity": "0.04",
            "risk_policy_version": 13,
            "side": "BUY",
            "strategy": "CASE_C",
            "submission_attempt": 0,
            "submitted_quantity": "0.04",
            "symbol": "ETHUSDT",
        }
        invalid_orders = []  # 각 fail-closed mutation을 독립 case로 보존한다.
        unknown_field_order = dict(base_order)
        unknown_field_order["raw_response"] = "forbidden"
        invalid_orders.append([unknown_field_order])
        conflicting_order = dict(base_order)
        conflicting_order["submitted_quantity"] = "0.03"
        invalid_orders.append([base_order, conflicting_order])

        # 각 mutation을 별도 JSONL artifact로 쓰고 actual evidence extractor가 둘 다 거부하는지 확인한다.
        for case_index, candidate_orders in enumerate(invalid_orders):
            with self.subTest(case_index=case_index), TemporaryDirectory() as temporary_directory:
                pending_path = Path(temporary_directory) / "pending.jsonl"
                events = [
                    {
                        "schema_version": 4,
                        "record_type": "pending_order_event",
                        "operation": "UPSERT",
                        "lifecycle": "PREPARED",
                        "order": candidate_order,
                    }
                    for candidate_order in candidate_orders
                ]
                pending_path.write_text(
                    "".join(
                        f"{json.dumps(event, separators=(',', ':'))}\n"
                        for event in events
                    ),
                    encoding="utf-8",
                )

                with self.assertRaises(_StrictJsonError):
                    _extract_pending_order_upserts(pending_path)

    def test_transport_dto_normalization_keeps_atomic_trade_pair_and_aggregate_versions(self) -> None:
        """
        함수 이름: test_transport_dto_normalization_keeps_atomic_trade_pair_and_aggregate_versions()
        기능: envelope DTO와 run 시계로 ORDER/PERFORMANCE pair, aggregate version 및 causal 시각을 보존하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/01
        """
        transport_session_id = "00000000-0000-4000-8000-000000000031"
        event_dtos = [
            {
                "session_id": transport_session_id,
                "event_id": "00000000-0000-4000-8000-000000000041",
                "sequence": 9,
                "occurred_at": "2026-08-31T00:00:01.000000Z",
                "type": "TRADING_SESSION_UPDATED",
                "aggregate_version": 12,
                "payload": {"trading": {"session_id": "session-1"}},
            },
            {
                "session_id": transport_session_id,
                "event_id": "00000000-0000-4000-8000-000000000042",
                "sequence": 10,
                "occurred_at": "2026-08-31T00:00:02.000000Z",
                "type": "ORDER_EXECUTED",
                "aggregate_version": None,
                "payload": {"trade": {"trade_id": "trade-9001"}},
            },
            {
                "session_id": transport_session_id,
                "event_id": "00000000-0000-4000-8000-000000000043",
                "sequence": 11,
                "occurred_at": "2026-08-31T00:00:02.000000Z",
                "type": "PERFORMANCE_UPDATED",
                "aggregate_version": None,
                "payload": {"performance": {"total_fee": "0"}},
            },
            {
                "session_id": transport_session_id,
                "event_id": "00000000-0000-4000-8000-000000000044",
                "sequence": 12,
                "occurred_at": "2026-08-31T00:00:03.000000Z",
                "type": "ACCOUNT_UPDATED",
                "aggregate_version": 3,
                "payload": {"account": {"version": 3}},
            },
        ]

        # 서로 다른 aggregate version은 감소할 수 있고 atomic pair의 None은 인위 숫자로 바꾸지 않는다.
        normalized_batch = _normalize_transport_ui_events(
            event_dtos,
            transport_session_id=transport_session_id,
        )
        normalized_events = normalized_batch["events"]
        self.assertEqual(transport_session_id, normalized_batch["transport_session_id"])
        self.assertEqual(
            [9, 10, 11, 12],
            [event["transport_sequence"] for event in normalized_events],
        )
        self.assertIsNone(normalized_events[1]["aggregate_version"])
        self.assertIsNone(normalized_events[2]["aggregate_version"])
        self.assertEqual("trade-9001", normalized_events[2]["related_id"])
        self.assertEqual("ACCOUNT", normalized_events[3]["aggregate"])
        self.assertNotIn("payload", normalized_events[1])

        # Wall UTC가 뒤로 바뀌어도 stream publication과 startup account source는 같은 run 구간을 사용한다.
        anchor_utc = datetime(2026, 9, 1, tzinfo=timezone.utc)
        monotonic_values = iter(
            (
                1_000_000_000,
                1_000_000_000,
                1_002_000_000,
                1_003_000_000,
                1_004_000_000,
            )
        )
        wall_clock = Mock(return_value=anchor_utc)
        evidence_clock = _RunScopedEvidenceClock(
            wall_clock=wall_clock,
            monotonic_clock=lambda: next(monotonic_values),
        )
        started_at = evidence_clock()
        wall_clock.return_value = anchor_utc - timedelta(seconds=20)
        event_stream = BackendEventStream(clock=evidence_clock)
        published_envelope = event_stream.publish(
            "ACCOUNT_UPDATED",
            {
                "account": {
                    "version": 1,
                    "balances": [],
                }
            },
            aggregate_version=1,
        )
        application_lock = RLock()
        account = Account()
        account.apply_initial_snapshot(
            AccountSnapshot(
                balances=(
                    AssetBalance(
                        asset="ETH",
                        free=Decimal("0.001"),
                        locked=Decimal("0"),
                    ),
                ),
                updated_at=anchor_utc - timedelta(seconds=3),
                is_full_snapshot=True,
            ),
            current_price=Decimal("4000"),
        )
        for seconds_before_anchor, free_quantity in (
            (2, Decimal("0.002")),
            (1, Decimal("0.003")),
        ):
            account.apply_stream_snapshot(
                AccountSnapshot(
                    balances=(
                        AssetBalance(
                            asset="ETH",
                            free=free_quantity,
                            locked=Decimal("0"),
                        ),
                    ),
                    updated_at=(
                        anchor_utc
                        - timedelta(seconds=seconds_before_anchor)
                    ),
                    is_full_snapshot=False,
                )
            )

        def advance_account_during_clock() -> datetime:
            """
            함수 이름: advance_account_during_clock()
            기능: Account pointer 읽기 뒤 lock 내 clock 호출에서 후속 version을 적용한다.
            인자: 없음
            반환값: run-scoped clock이 만든 관찰 UTC 시각
            작성 날짜: 2026/09/01
            """
            self.assertTrue(
                getattr(application_lock, "_is_owned")()
            )  # Clock은 Account state 읽기와 같은 application RLock에서 호출돼야 한다.
            account.apply_stream_snapshot(
                AccountSnapshot(
                    balances=(
                        AssetBalance(
                            asset="ETH",
                            free=Decimal("0.004"),
                            locked=Decimal("0"),
                        ),
                    ),
                    updated_at=anchor_utc,
                    is_full_snapshot=False,
                )
            )

            return evidence_clock()

        runtime = object.__new__(ApplicationRuntime)
        object.__setattr__(runtime, "application_lock", application_lock)
        object.__setattr__(
            runtime,
            "account",
            account,
        )
        startup_account_event = _create_account_event_from_runtime(
            runtime,
            run_id="00000000-0000-4000-8000-000000000031",
            sequence=1,
            clock=advance_account_during_clock,
        )
        completed_at = evidence_clock()

        # 두 공개 시각을 다시 UTC로 해석해 started_at 이상 completed_at 이하의 causal window를 고정한다.
        published_at = datetime.fromisoformat(
            str(published_envelope.to_dto()["occurred_at"]).replace(
                "Z",
                "+00:00",
            )
        )
        account_source_time = datetime.fromisoformat(
            str(startup_account_event["source_event_time"]).replace(
                "Z",
                "+00:00",
            )
        )
        self.assertLessEqual(started_at, published_at)
        self.assertLessEqual(published_at, account_source_time)
        self.assertLessEqual(account_source_time, completed_at)
        self.assertEqual(3, startup_account_event["account_version"])
        self.assertEqual("0.003", startup_account_event["free_quantity"])
        self.assertEqual(4, account.get_snapshot().version)
        wall_clock.assert_called_once_with()

        # Snapshot publish 뒤 production 1L.1 기록 전 barrier에서는 시장 source를 NO_SIGNAL event로 합성하지 않는다.
        captured_source_kline = Kline(
            symbol="ETHUSDT",
            interval=Interval.THIRTY_MINUTES,
            open_time=anchor_utc - timedelta(minutes=30),
            open=Decimal("3990"),
            high=Decimal("4010"),
            low=Decimal("3980"),
            close=Decimal("4000"),
            volume=Decimal("10"),
            closed=True,
            event_time=anchor_utc,
        )
        newer_source_kline = Kline(
            symbol="ETHUSDT",
            interval=Interval.FOUR_HOURS,
            open_time=anchor_utc,
            open=Decimal("4000"),
            high=Decimal("4020"),
            low=Decimal("3990"),
            close=Decimal("4010"),
            volume=Decimal("11"),
            closed=False,
            event_time=anchor_utc + timedelta(seconds=1),
        )
        captured_market_state = MarketStateSnapshot(
            klines_by_interval=MappingProxyType(
                {Interval.THIRTY_MINUTES: (captured_source_kline,)}
            ),
            current_eth_price=Decimal("4000"),
            version=7,
            updated_at=anchor_utc,
            ready=True,
            update_source_klines=(captured_source_kline,),
        )
        newer_market_state = MarketStateSnapshot(
            klines_by_interval=MappingProxyType(
                {Interval.FOUR_HOURS: (newer_source_kline,)}
            ),
            current_eth_price=Decimal("4010"),
            version=8,
            updated_at=anchor_utc + timedelta(seconds=1),
            ready=True,
            update_source_klines=(newer_source_kline,),
        )
        market_state_reads = iter(
            (captured_market_state, newer_market_state)
        )

        def read_captured_market_state() -> MarketStateSnapshot:
            """
            함수 이름: read_captured_market_state()
            기능: Application lock 소유를 확인하고 현재 불변 시장 state pointer를 한 번 반환한다.
            인자: 없음
            반환값: source와 version이 같이 고정된 불변 시장 state
            작성 날짜: 2026/09/01
            """
            self.assertTrue(getattr(application_lock, "_is_owned")())

            return next(market_state_reads)

        market_snapshot = SimpleNamespace(
            get_snapshot=Mock(side_effect=read_captured_market_state),
            version=8,
            update_source_klines=(newer_source_kline,),
        )
        object.__setattr__(runtime, "market_snapshot", market_snapshot)
        trading_controller = SimpleNamespace(
            context=SimpleNamespace(version=41),
            public_market_boundary_trace=(),
        )
        object.__setattr__(
            runtime,
            "trading_controller",
            trading_controller,
        )
        unchanged_market_version, missing_boundary_events = (
            _create_observed_market_event(
                runtime,
                sequence=1,
                after_market_version=6,
            )
        )
        self.assertEqual(6, unchanged_market_version)
        self.assertEqual((), missing_boundary_events)

        # Controller trace가 실제로 추가된 뒤에만 그 frozen source/version/context를 새 event로 수집한다.
        captured_source_event_id = _kline_source_component(
            captured_source_kline
        )
        trading_controller.public_market_boundary_trace = (
            PublicMarketBoundaryTraceEntry(
                message_id="1L.1",
                event_type="KLINE_OBSERVED",
                source_event_id=captured_source_event_id,
                evaluation_id=f"market:7:{captured_source_event_id}",
                market_version=7,
                context_version=40,
                regime=RegimeType.TYPE_0,
            ),
        )
        captured_market_version, observed_market_events = (
            _create_observed_market_event(
                runtime,
                sequence=1,
                after_market_version=6,
            )
        )
        observed_market_event = observed_market_events[0]
        self.assertEqual(7, captured_market_version)
        self.assertEqual(1, len(observed_market_events))
        self.assertEqual(7, observed_market_event["market_version"])
        self.assertEqual(40, observed_market_event["context_version"])
        self.assertEqual(
            captured_source_event_id,
            observed_market_event["source_event_id"],
        )
        self.assertIsNone(observed_market_event["evaluation_id"])
        self.assertEqual(2, market_snapshot.get_snapshot.call_count)

        # Collector는 helper가 반환한 frozen trace event tuple만 쓰고 mutable snapshot version을 별도로 다시 읽지 않는다.
        collector_harness = (
            BinanceTestnetPhaseThirteenPublicMarketCase2Tests(
                "test_actual_public_market_case2_buy_and_exact_stop_recovery"
            )
        )
        collector_harness.event_stream = SimpleNamespace(
            wait_for_events=Mock(
                return_value=SimpleNamespace(
                    requires_resync=False,
                    events=(),
                )
            )
        )
        collector_harness.runtime = SimpleNamespace(
            market_snapshot=SimpleNamespace(version=object())
        )
        collector_harness.after_transport_sequence = 0
        collector_harness.transport_event_dtos = []
        collector_harness.public_account_events = []
        collector_harness.public_market_events = []
        collector_harness.last_market_version = 6
        with patch(
            f"{__name__}._create_observed_market_event",
            return_value=(7, observed_market_events),
        ) as capture_market_event:
            collector_harness._collect_runtime_observations()

        capture_market_event.assert_called_once_with(
            collector_harness.runtime,
            sequence=1,
            after_market_version=6,
        )
        self.assertEqual(7, collector_harness.last_market_version)
        self.assertEqual(
            [observed_market_event],
            collector_harness.public_market_events,
        )

        # Startup version 이하의 buffered/replayed Account DTO는 버리고 엄격히 더 큰 version만 후속 증거로 추가한다.
        account_events = [startup_account_event]
        replay_and_advance_dtos = [
            {
                "type": "ACCOUNT_UPDATED",
                "event_id": "00000000-0000-4000-8000-000000000051",
                "occurred_at": "2026-09-01T00:00:00.003000Z",
                "payload": {
                    "account": {
                        "version": 2,
                        "balances": [],
                    }
                },
            },
            {
                "type": "ACCOUNT_UPDATED",
                "event_id": "00000000-0000-4000-8000-000000000052",
                "occurred_at": "2026-09-01T00:00:00.004000Z",
                "payload": {
                    "account": {
                        "version": 3,
                        "balances": [],
                    }
                },
            },
            {
                "type": "ACCOUNT_UPDATED",
                "event_id": "00000000-0000-4000-8000-000000000053",
                "occurred_at": "2026-09-01T00:00:00.005000Z",
                "payload": {
                    "account": {
                        "version": 4,
                        "balances": [
                            {
                                "asset": "ETH",
                                "free": "0.004",
                                "locked": "0",
                            }
                        ],
                    }
                },
            },
        ]
        _append_account_events_from_transport(
            account_events,
            replay_and_advance_dtos,
        )

        self.assertEqual(
            [3, 4],
            [event["account_version"] for event in account_events],
        )
        self.assertEqual(
            replay_and_advance_dtos[2]["event_id"],
            account_events[1]["source_event_id"],
        )

    def test_transport_dto_normalization_rejects_orphan_performance_and_sequence_regression(self) -> None:
        """
        함수 이름: test_transport_dto_normalization_rejects_orphan_performance_and_sequence_regression()
        기능: atomic ORDER 없는 Performance와 transport sequence 역행을 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        orphan_performance = [
            {
                "session_id": "00000000-0000-4000-8000-000000000031",
                "event_id": "00000000-0000-4000-8000-000000000041",
                "sequence": 1,
                "occurred_at": "2026-08-31T00:00:00.000000Z",
                "type": "PERFORMANCE_UPDATED",
                "aggregate_version": None,
                "payload": {},
            }
        ]
        sequence_regression = [
            {
                "session_id": "00000000-0000-4000-8000-000000000031",
                "event_id": "00000000-0000-4000-8000-000000000041",
                "sequence": 2,
                "occurred_at": "2026-08-31T00:00:00.000000Z",
                "type": "ACCOUNT_UPDATED",
                "aggregate_version": 1,
                "payload": {},
            },
            {
                "session_id": "00000000-0000-4000-8000-000000000031",
                "event_id": "00000000-0000-4000-8000-000000000042",
                "sequence": 1,
                "occurred_at": "2026-08-31T00:00:01.000000Z",
                "type": "ACCOUNT_UPDATED",
                "aggregate_version": 2,
                "payload": {},
            },
        ]

        # 두 오류 모두 DTO payload를 artifact로 넘기기 전에 fail closed한다.
        for invalid_events in (orphan_performance, sequence_regression):
            with self.subTest(first_type=invalid_events[0]["type"]):
                with self.assertRaises(ValueError):
                    _normalize_transport_ui_events(
                        invalid_events,
                        transport_session_id=(
                            "00000000-0000-4000-8000-000000000031"
                        ),
                    )

    def test_actual_risk_policy_uses_fixed_realized_only_scope(self) -> None:
        """
        함수 이름: test_actual_risk_policy_uses_fixed_realized_only_scope()
        기능: Testnet outer cap과 분리된 unbounded REALIZED_ONLY/manual liquidation 정책을 network 없이 고정한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        policy = _create_actual_risk_policy(Decimal("10"))

        # 사용자 확정 세 cap의 None을 Session 3 execution cap 10으로 덮지 않고 recovery behavior도 결속한다.
        self.assertEqual(13, policy.version)
        self.assertIsNone(policy.max_order_notional)
        self.assertIsNone(policy.max_position_notional)
        self.assertIsNone(policy.max_daily_loss)
        self.assertIs(DailyLossScope.REALIZED_ONLY, policy.daily_loss_scope)
        self.assertIs(
            ManualKillBehavior.CANCEL_AND_LIQUIDATE,
            policy.manual_kill_behavior,
        )

    def test_success_order_trace_grammar_accepts_only_complete_branches(
        self,
    ) -> None:
        """
        함수 이름: test_success_order_trace_grammar_accepts_only_complete_branches()
        기능: immediate·query·stream partial 성공과 불법 branch·query 예산 초과를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        valid_traces = (
            (
                "immediate-buy",
                OrderSide.BUY,
                _BUY_ORDER_TRACE_PREFIX
                + _BUY_FILL_APPLICATION_TRACE
                + _BUY_ORDER_TRACE_SUFFIX,
            ),
            (
                "immediate-sell",
                OrderSide.SELL,
                _SELL_ORDER_TRACE_PREFIX
                + _SELL_FILL_APPLICATION_TRACE
                + _SELL_ORDER_TRACE_SUFFIX,
            ),
            (
                "query-then-buy-fill",
                OrderSide.BUY,
                _BUY_ORDER_TRACE_PREFIX
                + _SAME_ORDER_QUERY_TRACE
                + _SAME_ORDER_QUERY_TRACE
                + _BUY_FILL_APPLICATION_TRACE
                + _BUY_ORDER_TRACE_SUFFIX,
            ),
            (
                "sell-partial-then-terminal-without-new-delta",
                OrderSide.SELL,
                _SELL_ORDER_TRACE_PREFIX
                + _SELL_FILL_APPLICATION_TRACE
                + _SAME_ORDER_QUERY_TRACE
                + _SAME_ORDER_QUERY_TRACE
                + ("10",)
                + _SELL_ORDER_TRACE_SUFFIX,
            ),
            (
                "stream-partial-then-query-terminal",
                OrderSide.BUY,
                _BUY_ORDER_TRACE_PREFIX
                + ("9",)
                + _BUY_FILL_APPLICATION_TRACE
                + _SAME_ORDER_QUERY_TRACE
                + ("10",)
                + _BUY_ORDER_TRACE_SUFFIX,
            ),
        )

        # Production에서 실제 발생 가능한 initial fill과 bounded same-ID reconciliation 조합만 통과한다.
        for case_name, side, trace_ids in valid_traces:
            with self.subTest(case_name=case_name):
                _assert_complete_success_order_trace(trace_ids, side=side)

        invalid_traces = (
            (
                "missing-prefix-step",
                OrderSide.BUY,
                tuple(
                    message_id
                    for message_id in _BUY_ORDER_TRACE_PREFIX
                    if message_id != "6.1"
                )
                + _BUY_FILL_APPLICATION_TRACE
                + _BUY_ORDER_TRACE_SUFFIX,
            ),
            (
                "duplicate-prefix-step",
                OrderSide.BUY,
                _BUY_ORDER_TRACE_PREFIX[:2]
                + ("2",)
                + _BUY_ORDER_TRACE_PREFIX[2:]
                + _BUY_FILL_APPLICATION_TRACE
                + _BUY_ORDER_TRACE_SUFFIX,
            ),
            (
                "sell-risk-branch",
                OrderSide.SELL,
                _SELL_ORDER_TRACE_PREFIX[:4]
                + ("5.1",)
                + _SELL_ORDER_TRACE_PREFIX[4:]
                + _SELL_FILL_APPLICATION_TRACE
                + _SELL_ORDER_TRACE_SUFFIX,
            ),
            (
                "incomplete-query-branch",
                OrderSide.BUY,
                _BUY_ORDER_TRACE_PREFIX
                + ("8", "8.2", "9")
                + _BUY_FILL_APPLICATION_TRACE
                + _BUY_ORDER_TRACE_SUFFIX,
            ),
            (
                "duplicate-fill-without-query",
                OrderSide.BUY,
                _BUY_ORDER_TRACE_PREFIX
                + _BUY_FILL_APPLICATION_TRACE
                + _BUY_FILL_APPLICATION_TRACE
                + _BUY_ORDER_TRACE_SUFFIX,
            ),
            (
                "query-budget-exceeded",
                OrderSide.BUY,
                _BUY_ORDER_TRACE_PREFIX
                + _SAME_ORDER_QUERY_TRACE * 5
                + _BUY_FILL_APPLICATION_TRACE
                + _BUY_ORDER_TRACE_SUFFIX,
            ),
            (
                "terminal-without-fill",
                OrderSide.BUY,
                _BUY_ORDER_TRACE_PREFIX
                + _SAME_ORDER_QUERY_TRACE
                + _BUY_ORDER_TRACE_SUFFIX,
            ),
        )

        # Whitelist에 있는 ID만 사용해도 complete grammar를 어기면 실제 SUCCESS artifact로 승격하지 않는다.
        for case_name, side, trace_ids in invalid_traces:
            with self.subTest(case_name=case_name):
                with self.assertRaises(AssertionError):
                    _assert_complete_success_order_trace(trace_ids, side=side)

    def test_phase13_process_lease_blocks_parallel_process_until_release(
        self,
    ) -> None:
        """
        함수 이름: test_phase13_process_lease_blocks_parallel_process_until_release()
        기능: 별도 process의 동시 lease를 network 전에 차단하고 owner release 뒤에만 재획득을 허용한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        backend_root = Path(__file__).resolve().parents[2]
        child_environment = {
            "PYTHONPATH": os.pathsep.join(
                (str(backend_root / "src"), str(backend_root))
            )
        }  # Lease-only child에는 API credential, cap이나 세 실행 opt-in을 하나도 상속하지 않는다.
        verification_source = "\n".join(
            (
                "from pathlib import Path",
                "import os",
                "import sys",
                "from tests.testnet.test_phase13_public_market_case2 import _acquire_phase13_process_lease, _release_phase13_process_lease",
                "for variable_name in ('BINANCE_TESTNET_API_KEY', 'BINANCE_TESTNET_API_SECRET', 'BINANCE_TESTNET_MAX_NOTIONAL', 'BINANCE_RUN_TESTNET', 'BINANCE_RUN_TESTNET_ORDERS', 'BINANCE_RUN_PHASE13_PUBLIC_CASE2'):",
                "    if variable_name in os.environ:",
                "        raise SystemExit(24)",
                "try:",
                "    lease_descriptor = _acquire_phase13_process_lease(Path(sys.argv[1]))",
                "except RuntimeError:",
                "    raise SystemExit(23)",
                "_release_phase13_process_lease(lease_descriptor)",
            )
        )

        with TemporaryDirectory() as temporary_directory:
            lease_parent = Path(temporary_directory) / "artifacts"
            lease_parent.mkdir(mode=0o755)
            os.chmod(lease_parent, 0o755)
            lock_path = lease_parent / "phase13.lock"
            lease_descriptor = _acquire_phase13_process_lease(lock_path)
            self.assertEqual(
                0o700,
                os.stat(lease_parent, follow_symlinks=False).st_mode & 0o777,
            )  # 기존 workspace와 같은 0755 root도 열린 directory FD로만 안전하게 축소한다.

            # Parent가 FD를 유지하는 동안 exec된 child는 같은 regular inode의 LOCK_NB를 얻지 못한다.
            blocked_process = subprocess.run(
                [sys.executable, "-c", verification_source, str(lock_path)],
                cwd=backend_root,
                env=child_environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(23, blocked_process.returncode)
            self.assertEqual("", blocked_process.stdout)

            # 명시적 release 뒤 같은 child가 정상 획득·해제하고 lockfile은 0600 regular inode로 남는다.
            _release_phase13_process_lease(lease_descriptor)
            released_process = subprocess.run(
                [sys.executable, "-c", verification_source, str(lock_path)],
                cwd=backend_root,
                env=child_environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(
                0,
                released_process.returncode,
                msg=released_process.stderr,
            )
            lock_state = os.stat(lock_path, follow_symlinks=False)
            self.assertTrue(stat.S_ISREG(lock_state.st_mode))
            self.assertEqual(0o600, lock_state.st_mode & 0o777)

    def test_phase13_process_lease_rejects_symlink_leaf(self) -> None:
        """
        함수 이름: test_phase13_process_lease_rejects_symlink_leaf()
        기능: lease leaf symlink를 network 전에 거부하고 symlink 대상 bytes를 보존한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        with TemporaryDirectory() as temporary_directory:
            lease_parent = Path(temporary_directory) / "artifacts"
            lease_parent.mkdir(mode=0o700)
            target_path = Path(temporary_directory) / "unrelated-target"
            target_path.write_bytes(b"unchanged")
            os.chmod(target_path, 0o600)
            lock_path = lease_parent / "phase13.lock"

            # 공격자가 둔 symlink leaf는 O_NOFOLLOW open에서 닫히며 대상 파일은 건드리지 않는다.
            os.symlink(target_path, lock_path)
            with self.assertRaises(OSError):
                _acquire_phase13_process_lease(lock_path)

            self.assertTrue(lock_path.is_symlink())
            self.assertEqual(b"unchanged", target_path.read_bytes())
            self.assertEqual(0o600, target_path.stat().st_mode & 0o777)

    def test_phase13_process_lease_rejects_hardlink_without_mode_mutation(
        self,
    ) -> None:
        """
        함수 이름: test_phase13_process_lease_rejects_hardlink_without_mode_mutation()
        기능: unrelated hardlink lease를 거부하기 전에 대상 bytes와 0644 mode를 변경하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        with TemporaryDirectory() as temporary_directory:
            lease_parent = Path(temporary_directory) / "artifacts"
            lease_parent.mkdir(mode=0o700)
            target_path = Path(temporary_directory) / "unrelated-target"
            target_path.write_bytes(b"unchanged-hardlink-target")
            os.chmod(target_path, 0o644)
            lock_path = lease_parent / "phase13.lock"
            os.link(target_path, lock_path)

            # nlink 검사는 fchmod보다 먼저 실행되어 거부된 unrelated inode에 side effect를 남기지 않는다.
            with self.assertRaises(PermissionError):
                _acquire_phase13_process_lease(lock_path)

            target_state = target_path.stat()
            self.assertEqual(b"unchanged-hardlink-target", target_path.read_bytes())
            self.assertEqual(0o644, target_state.st_mode & 0o777)
            self.assertEqual(2, target_state.st_nlink)

    def test_no_signal_trace_body_seals_without_order_evidence(self) -> None:
        """
        함수 이름: test_no_signal_trace_body_seals_without_order_evidence()
        기능: 빈 baseline의 NO_SIGNAL body가 order identity 없이 canonical seal되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        started_at = datetime(2026, 8, 31, tzinfo=timezone.utc)
        performance = Performance((), clock=lambda: started_at)
        commission_policy = CommissionDiscountPolicy(
            symbol="ETHUSDT",
            enabled_for_account=False,
            enabled_for_symbol=False,
            discount_asset=None,
            discount_rate=Decimal("0"),
            standard_market_buy_rate=Decimal("0"),
            special_market_buy_rate=Decimal("0"),
            tax_market_buy_rate=Decimal("0"),
        )
        lot_size_filter = QuantityFilter(
            filter_type="LOT_SIZE",
            minimum_quantity=Decimal("0.0001"),
            maximum_quantity=Decimal("100"),
            step_size=Decimal("0.0001"),
        )
        market_lot_size_filter = QuantityFilter(
            filter_type="MARKET_LOT_SIZE",
            minimum_quantity=Decimal("0.001"),
            maximum_quantity=Decimal("100"),
            step_size=Decimal("0.001"),
        )
        notional_filter = NotionalFilter(
            filter_type="NOTIONAL",
            minimum_notional=Decimal("10"),
            maximum_notional=Decimal("100000"),
            apply_minimum_to_market=True,
            apply_maximum_to_market=True,
            average_price_minutes=5,
        )
        public_filters = AccountRelevantFilters(
            symbol="ETHUSDT",
            exchange_order_count_filters=(),
            symbol_order_count_filters=(),
            symbol_quantity_filters=(
                lot_size_filter,
                market_lot_size_filter,
            ),
            symbol_notional_filters=(notional_filter,),
            symbol_maximum_position=None,
            passive_symbol_filter_types=frozenset(),
            asset_filters=(),
        )

        # Public scalar rules와 full union은 같은 DTO instance를 공유해 trace overlap을 재현한다.
        symbol_rules = SymbolTradingRules(
            symbol="ETHUSDT",
            status="TRADING",
            base_asset="ETH",
            quote_asset="USDT",
            base_asset_precision=8,
            order_types=frozenset({"MARKET"}),
            is_spot_trading_allowed=True,
            lot_size=lot_size_filter,
            market_lot_size=market_lot_size_filter,
            notional_filters=(notional_filter,),
            maximum_position=None,
            public_relevant_filters=public_filters,
        )
        account_filters = AccountRelevantFilters(
            symbol="ETHUSDT",
            exchange_order_count_filters=(),
            symbol_order_count_filters=(),
            symbol_quantity_filters=(),
            symbol_notional_filters=(),
            symbol_maximum_position=None,
            passive_symbol_filter_types=frozenset(),
            asset_filters=(
                AccountAssetFilter(
                    filter_type="MAX_ASSET",
                    asset="ETH",
                    maximum_quantity=Decimal("100"),
                ),
            ),
        )
        reference_price = ReferencePrice(
            symbol="ETHUSDT",
            price=Decimal("2500"),
            exchange_timestamp=int(started_at.timestamp() * 1000),
        )
        preflight = _create_preflight_evidence(
            account_version=1,
            verified_at=started_at,
            commission_policy=commission_policy,
            symbol_rules=symbol_rules,
            filters_observed_at=started_at,
            account_filters=account_filters,
            account_filters_observed_at=started_at,
            account_open_orders_observed_at=started_at,
            account_open_order_lists_observed_at=started_at,
            reference_price=reference_price,
            reference_price_observed_at=started_at,
        )
        trace_body = _create_non_mutating_trace_body(
            outcome="NO_SIGNAL",
            typed_reason=None,
            run_id="00000000-0000-4000-8000-000000000031",
            started_at=started_at,
            completed_at=started_at,
            preflight=preflight,
            market_events=[],
            account_events=[
                {
                    "sequence": 1,
                    "message_id": "2",
                    "event_type": "ACCOUNT_SNAPSHOT_APPLIED",
                    "source_event_id": (
                        "startup-account-"
                        "00000000-0000-4000-8000-000000000031-1"
                    ),
                    "source_event_time": _datetime_to_wire(started_at),
                    "account_version": 1,
                    "asset": "ETH",
                    "free_quantity": "0",
                    "locked_quantity": "0",
                }
            ],  # Actual NO_SIGNAL도 preflight 전 startup Account absolute snapshot을 한 건 보존한다.
            transport_events={
                "transport_session_id": (
                    "00000000-0000-4000-8000-000000000032"
                ),
                "events": [],
            },
            baseline_history_sha256=hashlib.sha256(b"").hexdigest(),
            baseline_trades=(),
            total_trades=(),
            performance=performance,
            final_verified_at=started_at,
            fresh_runtime_session_id=(
                "00000000-0000-4000-8000-000000000033"
            ),
        )

        # Seal validator가 order/recovery evidence 없는 zero exposure body를 그대로 받아야 한다.
        sealed_trace = seal_phase13_public_trace(trace_body)
        self.assertEqual("NO_SIGNAL", sealed_trace["outcome"])
        self.assertEqual([], sealed_trace["order_attempts"])
        self.assertEqual(
            0,
            sealed_trace["final_state"]["performance"]["total_trade_count"],
        )

        # Actual writer는 credential·local-session canary가 비어 있으면 file 생성 전에 fail closed한다.
        redaction_canaries = (
            "unit-testnet-key-canary-31c8dcb0",
            "unit-testnet-secret-canary-5f498c2a",
            "unit-local-session-canary-19d83e64",
        )
        with TemporaryDirectory() as temporary_directory:
            artifact_directory = Path(temporary_directory)
            trace_path = artifact_directory / "phase13-public-case2-trace.json"
            with self.assertRaises(RuntimeError):
                _write_trace_artifact(
                    artifact_directory,
                    trace_body,
                    forbidden_values=(),
                )
            self.assertFalse(trace_path.exists())

            # Non-empty canary를 받은 actual API만 canonical newline과 owner-only mode로 artifact를 만든다.
            written_path, written_digest = _write_trace_artifact(
                artifact_directory,
                trace_body,
                forbidden_values=redaction_canaries,
            )
            self.assertEqual(trace_path, written_path)
            self.assertEqual(sealed_trace["trace_sha256"], written_digest)
            self.assertEqual(0o600, written_path.stat().st_mode & 0o777)
            self.assertTrue(written_path.read_bytes().endswith(b"\n"))

        # Write, file fsync와 publish link 실패는 final 이름이나 orphan 임시 파일을 남기지 않는다.
        for failing_operation in ("write", "fsync", "link"):
            with self.subTest(failing_operation=failing_operation), TemporaryDirectory() as temporary_directory:
                artifact_directory = Path(temporary_directory)
                trace_path = artifact_directory / "phase13-public-case2-trace.json"
                with patch.object(
                    os,
                    failing_operation,
                    side_effect=OSError(f"injected {failing_operation} failure"),
                ):
                    with self.assertRaises(OSError):
                        _write_trace_artifact(
                            artifact_directory,
                            trace_body,
                            forbidden_values=redaction_canaries,
                        )
                self.assertFalse(trace_path.exists())
                self.assertEqual((), tuple(artifact_directory.iterdir()))

        # 기존 final은 hard-link publish가 EEXIST로 끝나도 byte-for-byte 보존되고 임시 inode만 회수된다.
        with TemporaryDirectory() as temporary_directory:
            artifact_directory = Path(temporary_directory)
            trace_path = artifact_directory / "phase13-public-case2-trace.json"
            collision_canary = b"existing-final-must-not-change\n"
            trace_path.write_bytes(collision_canary)
            with self.assertRaises(FileExistsError):
                _write_trace_artifact(
                    artifact_directory,
                    trace_body,
                    forbidden_values=redaction_canaries,
                )
            self.assertEqual(collision_canary, trace_path.read_bytes())
            self.assertEqual((trace_path,), tuple(artifact_directory.iterdir()))

        # Temp inode에 외부 hardlink가 끼면 final bytes가 같아도 단일-owner publication으로 인정하지 않는다.
        with TemporaryDirectory() as temporary_directory:
            artifact_directory = Path(temporary_directory)
            real_link = os.link

            def _publish_with_external_hardlink(
                source_name: str,
                destination_name: str,
                *,
                src_dir_fd: int,
                dst_dir_fd: int,
                follow_symlinks: bool,
            ) -> None:
                """
                함수 이름: _publish_with_external_hardlink()
                기능: 정상 final link 직전에 같은 temp inode의 공격자 alias를 하나 더 만든다.
                인자: source_name -> publisher temp leaf
                    destination_name -> publisher final leaf
                    src_dir_fd -> pinned source directory descriptor
                    dst_dir_fd -> pinned destination directory descriptor
                    follow_symlinks -> publisher가 전달한 no-follow 옵션
                반환값: 없음
                작성 날짜: 2026/09/01
                """
                real_link(
                    source_name,
                    "external-hardlink.json",
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                    follow_symlinks=follow_symlinks,
                )
                real_link(
                    source_name,
                    destination_name,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                    follow_symlinks=follow_symlinks,
                )

            with patch.object(
                os,
                "link",
                side_effect=_publish_with_external_hardlink,
            ):
                with self.assertRaises(RuntimeError):
                    _write_trace_artifact(
                        artifact_directory,
                        trace_body,
                        forbidden_values=redaction_canaries,
                    )
            self.assertTrue(
                (artifact_directory / "external-hardlink.json").exists()
            )

        # Publish 뒤 directory fsync가 실패해도 final은 항상 완전한 canonical inode이며 temp 이름은 없다.
        with TemporaryDirectory() as temporary_directory:
            artifact_directory = Path(temporary_directory)
            trace_path = artifact_directory / "phase13-public-case2-trace.json"
            real_fsync = os.fsync
            fsync_call_count = 0

            def _fail_directory_fsync(file_descriptor: int) -> None:
                """
                함수 이름: _fail_directory_fsync()
                기능: file fsync는 통과시키고 두 번째 directory fsync만 실패시키는 fault를 주입한다.
                인자: file_descriptor -> writer가 fsync할 file 또는 directory descriptor
                반환값: 첫 호출은 없음, 두 번째 호출은 OSError
                작성 날짜: 2026/08/31
                """
                nonlocal fsync_call_count
                fsync_call_count += 1
                if fsync_call_count == 2:
                    raise OSError("injected directory fsync failure")
                real_fsync(file_descriptor)

            with patch.object(os, "fsync", side_effect=_fail_directory_fsync):
                with self.assertRaises(OSError):
                    _write_trace_artifact(
                        artifact_directory,
                        trace_body,
                        forbidden_values=redaction_canaries,
                    )
            self.assertEqual(
                canonical_actual_phase13_public_trace_bytes(
                    sealed_trace,
                    forbidden_values=redaction_canaries,
                ),
                trace_path.read_bytes(),
            )
            self.assertEqual((trace_path,), tuple(artifact_directory.iterdir()))

        # Directory leaf symlink는 target이 owner-only 실제 directory여도 descriptor open 전에 거부한다.
        with TemporaryDirectory() as temporary_directory:
            test_root = Path(temporary_directory)
            artifact_directory = test_root / "real-artifacts"
            artifact_directory.mkdir(mode=0o700)
            symlink_directory = test_root / "artifact-link"
            symlink_directory.symlink_to(artifact_directory, target_is_directory=True)
            with self.assertRaises(ValueError):
                _write_trace_artifact(
                    symlink_directory,
                    trace_body,
                    forbidden_values=redaction_canaries,
                )
            self.assertEqual((), tuple(artifact_directory.iterdir()))

        # Directory path가 fsync 뒤 교체되면 열린 inode에 완전한 파일이 있어도 다른 반환 Path를 내주지 않는다.
        with TemporaryDirectory() as temporary_directory:
            test_root = Path(temporary_directory)
            artifact_directory = test_root / "artifacts"
            artifact_directory.mkdir(mode=0o700)
            moved_directory = test_root / "moved-artifacts"
            real_fsync = os.fsync
            fsync_call_count = 0

            def _swap_directory_after_publish(file_descriptor: int) -> None:
                """
                함수 이름: _swap_directory_after_publish()
                기능: directory fsync 완료 직후 공개 Path의 directory inode를 테스트 replacement로 바꾼다.
                인자: file_descriptor -> writer가 동기화하는 file 또는 directory descriptor
                반환값: 없음
                작성 날짜: 2026/08/31
                """
                nonlocal fsync_call_count
                fsync_call_count += 1
                real_fsync(file_descriptor)
                if fsync_call_count == 2:
                    artifact_directory.rename(moved_directory)
                    artifact_directory.mkdir(mode=0o700)

            with patch.object(
                os,
                "fsync",
                side_effect=_swap_directory_after_publish,
            ):
                with self.assertRaises((FileNotFoundError, RuntimeError)):
                    _write_trace_artifact(
                        artifact_directory,
                        trace_body,
                        forbidden_values=redaction_canaries,
                    )
            moved_trace_path = (
                moved_directory / "phase13-public-case2-trace.json"
            )
            self.assertEqual(
                canonical_actual_phase13_public_trace_bytes(
                    sealed_trace,
                    forbidden_values=redaction_canaries,
                ),
                moved_trace_path.read_bytes(),
            )
            self.assertEqual((), tuple(artifact_directory.iterdir()))
            self.assertEqual((moved_trace_path,), tuple(moved_directory.iterdir()))

    def test_artifact_cleanup_attempts_every_resource_and_preserves_first_error(
        self,
    ) -> None:
        """
        함수 이름: test_artifact_cleanup_attempts_every_resource_and_preserves_first_error()
        기능: artifact body와 세 cleanup이 함께 실패해도 전체 정리와 첫 cleanup 오류 보존을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/04
        """
        with TemporaryDirectory() as temporary_directory:
            artifact_directory = Path(temporary_directory) / "artifacts"
            artifact_directory.mkdir(mode=0o700)
            body_error = OSError("artifact body failed")
            temporary_cleanup_error = OSError(
                "temporary descriptor cleanup failed"
            )
            unlink_cleanup_error = OSError("temporary unlink cleanup failed")
            directory_cleanup_error = OSError(
                "directory descriptor cleanup failed"
            )
            real_close = os.close
            real_unlink = os.unlink
            cleanup_trace: list[str] = []
            closed_descriptors: list[int] = []

            def close_then_report_cleanup_error(file_descriptor: int) -> None:
                """
                함수 이름: close_then_report_cleanup_error()
                기능: 실제 descriptor를 닫은 뒤 temp와 directory cleanup 오류를 순서대로 주입한다.
                인자: file_descriptor -> artifact helper가 정리할 descriptor
                반환값: 없음, 순서에 맞는 OSError 발생
                작성 날짜: 2026/09/04
                """
                cleanup_label = (
                    "temporary-close"
                    if not closed_descriptors
                    else "directory-close"
                )
                cleanup_trace.append(cleanup_label)
                closed_descriptors.append(file_descriptor)
                real_close(file_descriptor)
                if len(closed_descriptors) == 1:
                    raise temporary_cleanup_error
                raise directory_cleanup_error

            def unlink_then_report_cleanup_error(
                artifact_name: str,
                *,
                dir_fd: int,
            ) -> None:
                """
                함수 이름: unlink_then_report_cleanup_error()
                기능: 임시 이름을 실제 회수한 뒤 unlink cleanup 오류를 주입한다.
                인자: artifact_name -> directory FD 기준 임시 artifact 이름
                    dir_fd -> 임시 artifact를 소유한 pinned directory descriptor
                반환값: 없음, unlink_cleanup_error 발생
                작성 날짜: 2026/09/04
                """
                cleanup_trace.append("temporary-unlink")
                real_unlink(artifact_name, dir_fd=dir_fd)
                raise unlink_cleanup_error

            # Body write 실패 뒤 세 cleanup도 모두 실패하게 해 후속 정리 시도와 오류 우선순위를 고정한다.
            with patch.object(
                os,
                "write",
                side_effect=body_error,
            ) as write_mock, patch.object(
                os,
                "close",
                side_effect=close_then_report_cleanup_error,
            ), patch.object(
                os,
                "unlink",
                side_effect=unlink_then_report_cleanup_error,
            ):
                with self.assertRaises(OSError) as cleanup_context:
                    _publish_new_artifact_bytes(
                        artifact_directory,
                        "cleanup-regression.json",
                        b"{}\n",
                    )

            self.assertIs(temporary_cleanup_error, cleanup_context.exception)
            self.assertIs(body_error, cleanup_context.exception.__context__)
            write_mock.assert_called_once()
            self.assertEqual(
                [
                    "temporary-close",
                    "temporary-unlink",
                    "directory-close",
                ],
                cleanup_trace,
            )
            self.assertEqual(2, len(closed_descriptors))
            for file_descriptor in closed_descriptors:
                with self.assertRaises(OSError):
                    os.fstat(file_descriptor)
            self.assertEqual((), tuple(artifact_directory.iterdir()))

    def test_failed_evidence_seals_incomplete_fresh_state_without_exception_text(
        self,
    ) -> None:
        """
        함수 이름: test_failed_evidence_seals_incomplete_fresh_state_without_exception_text()
        기능: mutation 차단 뒤 fresh 검증 실패도 null truth와 typed reason만 가진 별도 artifact로 봉인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        observed_at = datetime(2026, 8, 31, tzinfo=timezone.utc)
        redaction_canaries = (
            "unit-testnet-key-canary-failure-31c8dcb0",
            "unit-testnet-secret-canary-failure-5f498c2a",
        )
        failure_body = {
            "schema_version": _PHASE13_FAILURE_EVIDENCE_SCHEMA_VERSION,
            "record_type": _PHASE13_FAILURE_EVIDENCE_RECORD_TYPE,
            "outcome": "FAILED",
            "typed_reason": "ACTUAL_TIMEOUT",
            "run_id": "00000000-0000-4000-8000-000000000031",
            "timestamps": {
                "started_at": _datetime_to_wire(observed_at),
                "completed_at": _datetime_to_wire(observed_at),
            },
            "mutation_guard": {
                "mutation_started": True,
                "submissions_blocked": True,
                "submission_attempts": [
                    {
                        "sequence": 1,
                        "symbol": "ETHUSDT",
                        "side": "BUY",
                        "order_type": "MARKET",
                        "submission_attempt": 0,
                        "attempted_at": _datetime_to_wire(observed_at),
                    }
                ],
            },
            "recovery": {
                "attempted": False,
                "outcome": "SKIPPED",
                "typed_reason": "FAILURE_RECOVERY_STATE_AMBIGUOUS",
            },
            "runtime_state": {
                "application_status": "READY",
                "trading_status": "RECONCILIATION_REQUIRED",
                "reconciliation_required": True,
                "position_quantity": None,
                "pending_order_count": 1,
                "durable_trade_count": 0,
            },
            "fresh_verification": {
                "status": "INCOMPLETE",
                "typed_reason": "FRESH_VERIFICATION_FAILED",
                "failure_stage": "STARTUP_APPLICATION",
                "verified_at": None,
                "position_quantity": None,
                "pending_order_count": None,
                "reconciliation_required": None,
                "matching_open_order_count": None,
                "account_open_orders_empty": None,
                "account_open_order_lists_empty": None,
                "run_exchange_order_count": None,
                "durable_trade_count": None,
            },
            "first_cause": {
                "reconciliation_required": True,
                "status": ReconciliationCauseStatus.EXACT.value,
                "category": (
                    ReconciliationCauseCategory.EVENT_WORKER_OR_RUNTIME_FAILED.value
                ),
            },
            "evidence_errors": [
                "FAILURE_RECOVERY_STATE_AMBIGUOUS",
                "RUNTIME_POSITION_UNAVAILABLE",
                "FRESH_VERIFICATION_FAILED",
            ],
        }

        # 원 exception text를 받는 field가 없고 canonical digest만 final document에 추가된다.
        sealed_trace, canonical_bytes, failure_digest = _seal_failure_evidence(
            failure_body,
            forbidden_values=redaction_canaries,
        )
        self.assertEqual("FAILED", sealed_trace["outcome"])
        self.assertEqual(failure_digest, sealed_trace["failure_sha256"])
        self.assertNotIn(b"controlled timeout detail", canonical_bytes)
        false_recovery_body = json.loads(json.dumps(failure_body))
        false_recovery_body["recovery"] = {
            "attempted": True,
            "outcome": "SUCCESS",
            "typed_reason": None,
        }
        # STOP SELL attempt가 없는 artifact는 recovery SUCCESS를 주장할 수 없다.
        with self.assertRaises(ValueError):
            _seal_failure_evidence(
                false_recovery_body,
                forbidden_values=redaction_canaries,
            )

        completed_recovery_body = json.loads(json.dumps(failure_body))
        sell_attempt = json.loads(
            json.dumps(
                completed_recovery_body["mutation_guard"][
                    "submission_attempts"
                ][0]
            )
        )
        sell_attempt.update(
            {
                "sequence": 2,
                "side": "SELL",
            }
        )
        completed_recovery_body["mutation_guard"][
            "submission_attempts"
        ].append(sell_attempt)
        completed_recovery_body["recovery"] = {
            "attempted": True,
            "outcome": "SUCCESS",
            "typed_reason": None,
        }
        completed_recovery_body["first_cause"]["category"] = (
            ReconciliationCauseCategory.ORDER_OR_PERSISTENCE_AMBIGUOUS.value
        )
        completed_recovery_body["runtime_state"].update(
            {
                "trading_status": "TERMINATED",
                "reconciliation_required": False,
                "position_quantity": "0",
                "pending_order_count": 0,
                "durable_trade_count": 2,
            }
        )
        completed_recovery_body["fresh_verification"].update(
            {
                "status": "VERIFIED",
                "typed_reason": None,
                "failure_stage": None,
                "verified_at": _datetime_to_wire(observed_at),
                "position_quantity": "0",
                "pending_order_count": 0,
                "reconciliation_required": False,
                "matching_open_order_count": 0,
                "account_open_orders_empty": True,
                "account_open_order_lists_empty": True,
                "run_exchange_order_count": 2,
                "durable_trade_count": 2,
            }
        )
        completed_recovery_body["evidence_errors"] = []
        _seal_failure_evidence(
            completed_recovery_body,
            forbidden_values=redaction_canaries,
        )  # Exact BUY→SELL과 두 zero-exposure snapshot은 completed recovery로 봉인된다.

        contradictory_success_body = json.loads(
            json.dumps(completed_recovery_body)
        )
        contradictory_success_body["runtime_state"].update(
            {
                "trading_status": "RECONCILIATION_REQUIRED",
                "reconciliation_required": True,
                "position_quantity": "1",
                "pending_order_count": 1,
            }
        )
        contradictory_success_body["fresh_verification"].update(
            {
                "position_quantity": "1",
                "pending_order_count": 1,
                "reconciliation_required": True,
                "matching_open_order_count": 1,
            }
        )
        missing_verified_at_body = json.loads(
            json.dumps(completed_recovery_body)
        )
        missing_verified_at_body["fresh_verification"]["verified_at"] = None
        completed_with_publication_error = json.loads(
            json.dumps(completed_recovery_body)
        )
        completed_with_publication_error["evidence_errors"].append(
            "FAILURE_SCHEDULER_GATE_PUBLICATION_FAILED"
        )
        permanent_cause_with_completed_recovery = json.loads(
            json.dumps(completed_recovery_body)
        )
        permanent_cause_with_completed_recovery["first_cause"]["category"] = (
            ReconciliationCauseCategory.EVENT_WORKER_OR_RUNTIME_FAILED.value
        )
        false_not_required_single_buy = json.loads(
            json.dumps(completed_recovery_body)
        )
        false_not_required_single_buy["mutation_guard"][
            "submission_attempts"
        ] = false_not_required_single_buy["mutation_guard"][
            "submission_attempts"
        ][:1]
        false_not_required_single_buy["recovery"] = {
            "attempted": False,
            "outcome": "NOT_REQUIRED",
            "typed_reason": None,
        }
        false_not_required_single_buy["runtime_state"][
            "durable_trade_count"
        ] = 1
        false_not_required_single_buy["fresh_verification"].update(
            {
                "run_exchange_order_count": 1,
                "durable_trade_count": 1,
            }
        )
        nonterminal_not_required_round_trip = json.loads(
            json.dumps(completed_recovery_body)
        )
        nonterminal_not_required_round_trip["recovery"] = {
            "attempted": False,
            "outcome": "NOT_REQUIRED",
            "typed_reason": None,
        }
        nonterminal_not_required_round_trip["runtime_state"][
            "trading_status"
        ] = "NOT_STARTED"
        contradictory_not_required_body = json.loads(json.dumps(failure_body))
        contradictory_not_required_body["recovery"] = {
            "attempted": False,
            "outcome": "NOT_REQUIRED",
            "typed_reason": None,
        }

        # Completed claim의 nonzero/UNKNOWN, timestamp 누락과 생성 불가 publication error를 독립 거부한다.
        for case_name, invalid_body in (
            ("contradictory-success", contradictory_success_body),
            ("missing-verified-at", missing_verified_at_body),
            (
                "completed-with-publication-error",
                completed_with_publication_error,
            ),
            (
                "permanent-cause-with-completed-recovery",
                permanent_cause_with_completed_recovery,
            ),
            (
                "false-not-required-single-buy",
                false_not_required_single_buy,
            ),
            (
                "nonterminal-not-required-round-trip",
                nonterminal_not_required_round_trip,
            ),
            ("contradictory-not-required", contradictory_not_required_body),
        ):
            with self.subTest(case_name=case_name):
                with self.assertRaises(ValueError):
                    _seal_failure_evidence(
                        invalid_body,
                        forbidden_values=redaction_canaries,
                    )
        with TemporaryDirectory() as temporary_directory:
            artifact_directory = Path(temporary_directory)
            failure_path, written_digest = _write_failure_evidence_artifact(
                artifact_directory,
                failure_body,
                forbidden_values=redaction_canaries,
            )
            self.assertEqual(failure_digest, written_digest)
            self.assertEqual(canonical_bytes, failure_path.read_bytes())
            self.assertEqual(0o600, failure_path.stat().st_mode & 0o777)

        # V2 attempt는 ID field 자체를 허용하지 않으므로 safe token과 secret canary를 독립 거부한다.
        for field_name, field_value in (
            ("intent_id", "safe-looking-intent"),
            ("client_order_id", redaction_canaries[1]),
        ):
            with self.subTest(field_name=field_name):
                identifier_body = json.loads(
                    _canonical_failure_evidence_bytes(failure_body)
                )
                identifier_body["mutation_guard"][
                    "submission_attempts"
                ][0][field_name] = field_value
                with self.assertRaises(ValueError):
                    _seal_failure_evidence(
                        identifier_body,
                        forbidden_values=redaction_canaries,
                    )

    def test_preserved_failure_schema_v1_remains_byte_exact(self) -> None:
        """
        함수 이름: test_preserved_failure_schema_v1_remains_byte_exact()
        기능: v2 writer 도입 뒤에도 보존 FAILED v1의 schema·body/file digest와 canonical bytes를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        preserved_path = _ARTIFACT_ROOT / (
            "phase13-public-case2-20260831T095958280425Z-"
            "b2c3cd9008584a539acf71703050c743"
        ) / "phase13-public-case2-failed.json"
        preserved_bytes = preserved_path.read_bytes()
        preserved_document = json.loads(
            preserved_bytes,
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
        redaction_canaries = (
            "preserved-v1-key-canary-31c8dcb0",
            "preserved-v1-secret-canary-5f498c2a",
        )

        # V1에는 v2 cause/stage field를 합성하지 않고 원 body와 완성 문서를 각각 재해시한다.
        validated_digest = _validate_failure_evidence_document(
            preserved_document,
            forbidden_values=redaction_canaries,
        )
        preserved_body = {
            field_name: field_value
            for field_name, field_value in preserved_document.items()
            if field_name != "failure_sha256"
        }
        self.assertEqual(1, preserved_document["schema_version"])
        self.assertNotIn("first_cause", preserved_document)
        self.assertNotIn(
            "failure_stage",
            preserved_document["fresh_verification"],
        )
        self.assertEqual(
            "3a3fd9475099dfab5f5ef75397470d9d3fb2acb8591af2bbf96679db999ba029",
            validated_digest,
        )
        self.assertEqual(
            "c177ddd6f16d4a53285c42d962b97e47416de37cc39e436e0b61cd06519b6e90",
            hashlib.sha256(preserved_bytes).hexdigest(),
        )
        self.assertEqual(
            validated_digest,
            hashlib.sha256(
                _canonical_failure_evidence_bytes(preserved_body)
            ).hexdigest(),
        )
        self.assertEqual(
            preserved_bytes,
            _canonical_failure_evidence_bytes(preserved_document),
        )
        with self.assertRaises(ValueError):
            _write_failure_evidence_artifact(
                preserved_path.parent,
                preserved_body,
                forbidden_values=redaction_canaries,
            )  # Reader만 v1을 지원하고 current writer는 final path 접근 전에 v1을 거부한다.

    def test_failure_schema_v2_dispatch_rejects_cause_and_stage_drift(
        self,
    ) -> None:
        """
        함수 이름: test_failure_schema_v2_dispatch_rejects_cause_and_stage_drift()
        기능: explicit v1→v2 fixture에서 exact cause/stage allowlist와 version field 분리를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        preserved_path = _ARTIFACT_ROOT / (
            "phase13-public-case2-20260831T095958280425Z-"
            "b2c3cd9008584a539acf71703050c743"
        ) / "phase13-public-case2-failed.json"
        preserved_document = json.loads(
            preserved_path.read_bytes(),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
        failure_body = {
            field_name: field_value
            for field_name, field_value in preserved_document.items()
            if field_name != "failure_sha256"
        }
        failure_body["schema_version"] = 2
        failure_body["first_cause"] = {
            "reconciliation_required": True,
            "status": ReconciliationCauseStatus.EXACT.value,
            "category": (
                ReconciliationCauseCategory.PREPARE_FILTER_OR_CAP_REJECTED.value
            ),
        }
        failure_body["fresh_verification"].update(
            {
                "typed_reason": "FRESH_VERIFICATION_FAILED",
                "failure_stage": "STARTUP_APPLICATION",
                "account_open_orders_empty": None,
                "account_open_order_lists_empty": None,
            }
        )
        failure_body["evidence_errors"] = [
            "FAILURE_RECOVERY_STATE_AMBIGUOUS",
            "FRESH_VERIFICATION_FAILED",
        ]
        redaction_canaries = (
            "schema-v2-key-canary-31c8dcb0",
            "schema-v2-secret-canary-5f498c2a",
        )

        # Production enum 확장이 v2 allowlist를 묵시적으로 넓히지 못하고 schema bump 검토를 강제한다.
        self.assertEqual(
            frozenset(
                cause_status.value
                for cause_status in ReconciliationCauseStatus
            ),
            _FAILURE_FIRST_CAUSE_STATUSES,
        )
        self.assertEqual(
            frozenset(
                cause_category.value
                for cause_category in ReconciliationCauseCategory
            ),
            _FAILURE_FIRST_CAUSE_CATEGORIES,
        )
        self.assertEqual(
            frozenset(status.name for status in ApplicationStatus),
            _FAILURE_APPLICATION_STATUSES,
        )
        self.assertEqual(
            frozenset(status.name for status in TradingSessionStatus),
            _FAILURE_TRADING_STATUSES,
        )
        self.assertEqual(
            frozenset(
                failure_code.value for failure_code in StartupFailureCode
            ),
            _FAILURE_STARTUP_REASONS,
        )
        self.assertEqual(
            frozenset(StartupStage),
            frozenset(_STARTUP_STAGE_TO_FAILURE_STAGE),
        )
        _seal_failure_evidence(
            failure_body,
            forbidden_values=redaction_canaries,
        )
        resolved_latch_body = json.loads(json.dumps(failure_body))
        resolved_latch_body["first_cause"]["reconciliation_required"] = False
        _seal_failure_evidence(
            resolved_latch_body,
            forbidden_values=redaction_canaries,
        )  # Process-lifetime EXACT latch와 현재 해소된 reconciliation bool은 서로 독립한 truth다.

        # Unsupported version, mixed field 집합과 unknown cause/stage/reason은 모두 digest 전에 거부한다.
        invalid_bodies: list[tuple[str, dict[str, object]]] = []
        boolean_version = json.loads(json.dumps(failure_body))
        boolean_version["schema_version"] = True
        invalid_bodies.append(("boolean-version", boolean_version))
        unsupported_version = json.loads(json.dumps(failure_body))
        unsupported_version["schema_version"] = 3
        invalid_bodies.append(("unsupported-version", unsupported_version))
        v1_with_v2_fields = json.loads(json.dumps(failure_body))
        v1_with_v2_fields["schema_version"] = 1
        invalid_bodies.append(("v1-with-v2-fields", v1_with_v2_fields))
        missing_first_cause = json.loads(json.dumps(failure_body))
        del missing_first_cause["first_cause"]
        invalid_bodies.append(("missing-first-cause", missing_first_cause))
        unknown_category = json.loads(json.dumps(failure_body))
        unknown_category["first_cause"]["category"] = "UNKNOWN_CAUSE"
        invalid_bodies.append(("unknown-category", unknown_category))
        unknown_cause_status = json.loads(json.dumps(failure_body))
        unknown_cause_status["first_cause"]["status"] = "UNKNOWN_STATUS"
        invalid_bodies.append(("unknown-cause-status", unknown_cause_status))
        non_exact_category = json.loads(json.dumps(failure_body))
        non_exact_category["first_cause"]["status"] = (
            ReconciliationCauseStatus.CONFLICT.value
        )
        non_exact_category["first_cause"]["category"] = (
            ReconciliationCauseCategory.PREPARE_FILTER_OR_CAP_REJECTED.value
        )
        invalid_bodies.append(("non-exact-category", non_exact_category))
        for permanent_category in (
            ReconciliationCauseCategory.EVENT_WORKER_OR_RUNTIME_FAILED,
            ReconciliationCauseCategory.PROCESS_OWNERSHIP_AMBIGUOUS,
        ):
            resolved_permanent_cause = json.loads(json.dumps(failure_body))
            resolved_permanent_cause["first_cause"].update(
                {
                    "reconciliation_required": False,
                    "category": permanent_category.value,
                }
            )
            invalid_bodies.append(
                (
                    f"resolved-{permanent_category.value.lower()}",
                    resolved_permanent_cause,
                )
            )
        unknown_stage = json.loads(json.dumps(failure_body))
        unknown_stage["fresh_verification"]["failure_stage"] = "UNKNOWN_STAGE"
        invalid_bodies.append(("unknown-stage", unknown_stage))
        unknown_reason = json.loads(json.dumps(failure_body))
        unknown_reason["fresh_verification"]["typed_reason"] = "UNKNOWN_REASON"
        invalid_bodies.append(("unknown-reason", unknown_reason))
        unknown_runtime_status = json.loads(json.dumps(failure_body))
        unknown_runtime_status["runtime_state"]["application_status"] = (
            "UNKNOWN_STATUS"
        )
        invalid_bodies.append(("unknown-runtime-status", unknown_runtime_status))
        for impossible_attempt_application_status in (
            "CREATED",
            "STARTING",
            "FAILED",
            "SHUTTING_DOWN",
        ):
            attempts_outside_observable_application = (
                _create_failure_v2_test_body()
            )
            attempts_outside_observable_application["runtime_state"][
                "application_status"
            ] = impossible_attempt_application_status
            invalid_bodies.append(
                (
                    f"attempts-during-{impossible_attempt_application_status.lower()}",
                    attempts_outside_observable_application,
                )
            )
        for active_status in ("RUNNING", "STOPPING"):
            active_runtime_status = json.loads(json.dumps(failure_body))
            active_runtime_status["runtime_state"]["trading_status"] = (
                active_status
            )
            invalid_bodies.append(
                (f"active-runtime-{active_status.lower()}", active_runtime_status)
            )
        unknown_recovery_reason = json.loads(json.dumps(failure_body))
        unknown_recovery_reason["recovery"]["typed_reason"] = "UNKNOWN_REASON"
        invalid_bodies.append(("unknown-recovery-reason", unknown_recovery_reason))
        unknown_evidence_error = json.loads(json.dumps(failure_body))
        unknown_evidence_error["evidence_errors"] = ["UNKNOWN_ERROR"]
        invalid_bodies.append(("unknown-evidence-error", unknown_evidence_error))
        duplicate_evidence_error = json.loads(json.dumps(failure_body))
        duplicate_evidence_error["evidence_errors"] *= 2
        invalid_bodies.append(
            ("duplicate-evidence-error", duplicate_evidence_error)
        )
        reversed_evidence_error = json.loads(json.dumps(failure_body))
        reversed_evidence_error["evidence_errors"].reverse()
        invalid_bodies.append(
            ("reversed-evidence-error", reversed_evidence_error)
        )  # Set이 같아도 producer가 생성할 수 없는 fresh→recovery 순서는 거부한다.
        for case_name, invalid_body in invalid_bodies:
            with self.subTest(case_name=case_name):
                with self.assertRaises((TypeError, ValueError)):
                    _seal_failure_evidence(
                        invalid_body,
                        forbidden_values=redaction_canaries,
                    )

    def test_failure_schema_v2_binds_stage_reason_and_error_truth(self) -> None:
        """
        함수 이름: test_failure_schema_v2_binds_stage_reason_and_error_truth()
        기능: 15 stage의 생성 가능 reason과 recovery·runtime·fresh error iff를 exact 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        redaction_canaries = (
            "stage-reason-key-canary-31c8dcb0",
            "stage-reason-secret-canary-5f498c2a",
        )

        # 모든 producer pair는 통과하고 각 stage에 다른 stage reason을 주입하면 거부한다.
        all_stage_reasons = tuple(
            reason
            for allowed_reasons in _FAILURE_FRESH_STAGE_ALLOWED_REASONS.values()
            for reason in allowed_reasons
        )
        for failure_stage, allowed_reasons in (
            _FAILURE_FRESH_STAGE_ALLOWED_REASONS.items()
        ):
            for fresh_reason in allowed_reasons:
                with self.subTest(
                    case_name="positive-pair",
                    failure_stage=failure_stage,
                    fresh_reason=fresh_reason,
                ):
                    valid_body = _create_failure_v2_test_body()
                    valid_body["fresh_verification"].update(
                        {
                            "failure_stage": failure_stage,
                            "typed_reason": fresh_reason,
                        }
                    )
                    if failure_stage == "LOCAL_STATE":
                        valid_body["fresh_verification"].update(
                            {
                                "position_quantity": "1",
                                "pending_order_count": 1,
                                "reconciliation_required": False,
                            }
                        )
                    elif failure_stage == "OPEN_ORDERS":
                        valid_body["fresh_verification"].update(
                            {
                                "position_quantity": "1",
                                "pending_order_count": 0,
                                "reconciliation_required": False,
                                "account_open_orders_empty": False,
                                "matching_open_order_count": 0,
                            }
                        )
                        if fresh_reason == "FRESH_OPEN_ORDERS_CHANGED":
                            valid_body["fresh_verification"].update(
                                {
                                    "account_open_orders_empty": True,
                                    "account_open_order_lists_empty": True,
                                    "run_exchange_order_count": 1,
                                    "durable_trade_count": 1,
                                }
                            )
                    elif failure_stage == "OPEN_ORDER_LISTS":
                        valid_body["fresh_verification"].update(
                            {
                                "position_quantity": "1",
                                "pending_order_count": 0,
                                "reconciliation_required": False,
                                "account_open_orders_empty": True,
                                "matching_open_order_count": 0,
                                "account_open_order_lists_empty": False,
                            }
                        )
                    elif failure_stage == "RECENT_ORDERS":
                        valid_body["fresh_verification"].update(
                            {
                                "position_quantity": "1",
                                "pending_order_count": 0,
                                "reconciliation_required": False,
                                "account_open_orders_empty": True,
                                "matching_open_order_count": 0,
                                "account_open_order_lists_empty": True,
                            }
                        )
                        if fresh_reason == "FRESH_RECENT_ORDERS_CHANGED":
                            valid_body["fresh_verification"][
                                "durable_trade_count"
                            ] = 1
                    if (failure_stage, fresh_reason) in {
                        ("CLEANUP", "FRESH_RUNTIME_CLOSE_FAILED"),
                        ("CLEANUP", "FRESH_SNAPSHOT_CLEANUP_FAILED"),
                    }:
                        valid_body["fresh_verification"].update(
                            {
                                "verified_at": "2026-08-31T12:00:25.000000Z",
                                "position_quantity": "1",
                                "pending_order_count": 0,
                                "reconciliation_required": False,
                                "matching_open_order_count": 0,
                                "account_open_orders_empty": True,
                                "account_open_order_lists_empty": True,
                                "run_exchange_order_count": 1,
                                "durable_trade_count": 1,
                            }
                        )  # Primary cleanup 오류는 VERIFIED 직전 complete truth 뒤에만 생성된다.
                    valid_body["evidence_errors"] = [
                        "FAILURE_RECOVERY_STATE_AMBIGUOUS",
                        fresh_reason,
                    ]
                    _seal_failure_evidence(
                        valid_body,
                        forbidden_values=redaction_canaries,
                    )
                    if fresh_reason == "FRESH_RECENT_ORDERS_CHANGED":
                        recent_read_failed_body = deepcopy(valid_body)
                        recent_read_failed_body["fresh_verification"][
                            "run_exchange_order_count"
                        ] = 1
                        _seal_failure_evidence(
                            recent_read_failed_body,
                            forbidden_values=redaction_canaries,
                        )  # Final recent GET·exact 검증 예외는 직전 concrete count를 보존한다.
            mismatched_reason = next(
                reason
                for reason in all_stage_reasons
                if reason not in allowed_reasons
            )
            mismatched_body = _create_failure_v2_test_body()
            mismatched_body["fresh_verification"].update(
                {
                    "failure_stage": failure_stage,
                    "typed_reason": mismatched_reason,
                }
            )
            mismatched_body["evidence_errors"] = [
                "FAILURE_RECOVERY_STATE_AMBIGUOUS",
                mismatched_reason,
            ]
            with self.subTest(
                case_name="negative-pair",
                failure_stage=failure_stage,
            ):
                with self.assertRaises(ValueError):
                    _seal_failure_evidence(
                        mismatched_body,
                        forbidden_values=redaction_canaries,
                    )

        expected_startup_pairs = {
            StartupStage.APPLICATION: frozenset(
                {
                    StartupFailureCode.APPLICATION_ALREADY_STARTING,
                    StartupFailureCode.APPLICATION_CLOSED,
                }
            ),
            StartupStage.MARKET: frozenset(
                {
                    StartupFailureCode.MARKET_INITIALIZATION_FAILED,
                    StartupFailureCode.MARKET_NOT_READY,
                }
            ),
            StartupStage.REGIME: frozenset(
                {StartupFailureCode.REGIME_NOT_READY}
            ),
            StartupStage.ACCOUNT: frozenset(
                {
                    StartupFailureCode.ACCOUNT_INITIALIZATION_FAILED,
                    StartupFailureCode.ACCOUNT_NOT_READY,
                }
            ),
            StartupStage.HISTORY: frozenset(
                {StartupFailureCode.HISTORY_INITIALIZATION_FAILED}
            ),
            StartupStage.RECONCILIATION: frozenset(
                {
                    StartupFailureCode.ORDER_RECONCILIATION_FAILED,
                    StartupFailureCode.ORDER_RECONCILIATION_NOT_READY,
                }
            ),
        }
        self.assertEqual(
            expected_startup_pairs,
            _STARTUP_STAGE_ALLOWED_FAILURE_CODES,
        )  # Enum 추가나 stage/code 재배치는 자동 허용하지 않는다.

        valid_attempted_final_verification = _create_failure_v2_test_body()
        valid_attempted_final_verification["runtime_state"].update(
            {
                "application_status": "READY",
                "trading_status": "TERMINATED",
                "reconciliation_required": False,
                "position_quantity": "0",
                "pending_order_count": 0,
                "durable_trade_count": 2,
            }
        )
        valid_attempted_final_verification["recovery"] = {
            "attempted": True,
            "outcome": "FAILED",
            "typed_reason": "FAILURE_RECOVERY_FINAL_VERIFICATION_INCOMPLETE",
        }
        valid_attempted_final_verification["first_cause"]["category"] = (
            ReconciliationCauseCategory.ORDER_OR_PERSISTENCE_AMBIGUOUS.value
        )
        valid_attempted_final_verification["evidence_errors"] = [
            "FRESH_VERIFICATION_FAILED",
            "FAILURE_RECOVERY_FINAL_VERIFICATION_INCOMPLETE",
        ]
        _seal_failure_evidence(
            valid_attempted_final_verification,
            forbidden_values=redaction_canaries,
        )  # STOP recovery 자체의 exact terminal truth와 별개로 fresh runtime 생성은 실패할 수 있다.
        valid_nullable_runtime_after_recovery = deepcopy(
            valid_attempted_final_verification
        )
        valid_nullable_runtime_after_recovery["runtime_state"][
            "reconciliation_required"
        ] = None
        valid_nullable_runtime_after_recovery["evidence_errors"].insert(
            0,
            "RUNTIME_RECONCILIATION_UNAVAILABLE",
        )
        _seal_failure_evidence(
            valid_nullable_runtime_after_recovery,
            forbidden_values=redaction_canaries,
        )  # Recovery 성공 뒤 독립 runtime getter 하나가 실패해도 exact unavailable code로 봉인한다.
        valid_reblocked_runtime_after_recovery = deepcopy(
            valid_attempted_final_verification
        )
        valid_reblocked_runtime_after_recovery["runtime_state"][
            "reconciliation_required"
        ] = True
        _seal_failure_evidence(
            valid_reblocked_runtime_after_recovery,
            forbidden_values=redaction_canaries,
        )  # Terminal recovery 직후 stream callback이 blocker를 다시 세워도 immutable 거래 사실은 유지된다.
        valid_closed_scheduler_publication_failure = (
            _create_failure_v2_test_body()
        )
        valid_closed_scheduler_publication_failure["runtime_state"][
            "application_status"
        ] = "CLOSED"
        valid_closed_scheduler_publication_failure["first_cause"][
            "category"
        ] = ReconciliationCauseCategory.ORDER_OR_PERSISTENCE_AMBIGUOUS.value
        valid_closed_scheduler_publication_failure["evidence_errors"].insert(
            1,
            "FAILURE_SCHEDULER_GATE_PUBLICATION_FAILED",
        )
        _seal_failure_evidence(
            valid_closed_scheduler_publication_failure,
            forbidden_values=redaction_canaries,
        )  # tearDown은 runtime CLOSED 뒤에도 남은 active status gate를 원자적으로 닫을 수 있다.

        # Primary omission·irrelevant fresh code·recovery drift와 runtime None/code 불일치를 각각 거부한다.
        invalid_bodies: list[tuple[str, dict[str, object]]] = []
        missing_primary = _create_failure_v2_test_body()
        missing_primary["evidence_errors"] = [
            "FAILURE_RECOVERY_STATE_AMBIGUOUS"
        ]
        invalid_bodies.append(("missing-fresh-primary", missing_primary))
        irrelevant_fresh = _create_failure_v2_test_body()
        irrelevant_fresh["evidence_errors"].append("FRESH_FILTER_REJECTED")
        invalid_bodies.append(("irrelevant-fresh-reason", irrelevant_fresh))
        missing_recovery = _create_failure_v2_test_body()
        missing_recovery["evidence_errors"].remove(
            "FAILURE_RECOVERY_STATE_AMBIGUOUS"
        )
        invalid_bodies.append(("missing-recovery-reason", missing_recovery))
        extra_recovery = _create_failure_v2_test_body()
        extra_recovery["evidence_errors"].append(
            "FAILURE_RECOVERY_STATE_CHANGED"
        )
        invalid_bodies.append(("extra-recovery-reason", extra_recovery))

        # Baseline zero 상태에서 attempt가 없으면 history 관찰 실패와 무관하게 새 Position·matching은 0이다.
        no_attempt_runtime_position = _create_failure_v2_test_body()
        no_attempt_runtime_position["mutation_guard"].update(
            {
                "mutation_started": False,
                "submission_attempts": [],
            }
        )
        no_attempt_runtime_position["runtime_state"].update(
            {
                "position_quantity": "1",
                "pending_order_count": 0,
                "durable_trade_count": None,
            }
        )
        no_attempt_runtime_position["evidence_errors"] = [
            "FAILURE_RECOVERY_STATE_AMBIGUOUS",
            "RUNTIME_HISTORY_UNAVAILABLE",
            "FRESH_VERIFICATION_FAILED",
        ]
        invalid_bodies.append(
            ("no-attempt-runtime-position", no_attempt_runtime_position)
        )
        no_attempt_fresh_position = deepcopy(no_attempt_runtime_position)
        no_attempt_fresh_position["runtime_state"].update(
            {
                "position_quantity": "0",
                "durable_trade_count": 0,
            }
        )
        no_attempt_fresh_position["fresh_verification"].update(
            {
                "typed_reason": "FRESH_LOCAL_STATE_INCOMPLETE",
                "failure_stage": "LOCAL_STATE",
                "position_quantity": "1",
                "pending_order_count": 1,
                "reconciliation_required": False,
            }
        )
        no_attempt_fresh_position["evidence_errors"] = [
            "FAILURE_RECOVERY_STATE_AMBIGUOUS",
            "FRESH_LOCAL_STATE_INCOMPLETE",
        ]
        invalid_bodies.append(
            ("no-attempt-fresh-position", no_attempt_fresh_position)
        )
        no_attempt_matching_order = deepcopy(no_attempt_fresh_position)
        no_attempt_matching_order["fresh_verification"].update(
            {
                "typed_reason": "FRESH_OPEN_ORDERS_NOT_EMPTY",
                "failure_stage": "OPEN_ORDERS",
                "position_quantity": "0",
                "pending_order_count": 0,
                "account_open_orders_empty": False,
                "matching_open_order_count": 1,
            }
        )
        no_attempt_matching_order["evidence_errors"][-1] = (
            "FRESH_OPEN_ORDERS_NOT_EMPTY"
        )
        invalid_bodies.append(
            ("no-attempt-matching-open-order", no_attempt_matching_order)
        )
        inflated_runtime_pending = _create_failure_v2_test_body()
        inflated_runtime_pending["runtime_state"]["pending_order_count"] = 2
        invalid_bodies.append(
            ("inflated-runtime-pending", inflated_runtime_pending)
        )
        inflated_fresh_pending = _create_failure_v2_test_body()
        inflated_fresh_pending["fresh_verification"].update(
            {
                "typed_reason": "FRESH_LOCAL_STATE_INCOMPLETE",
                "failure_stage": "LOCAL_STATE",
                "position_quantity": "1",
                "pending_order_count": 2,
                "reconciliation_required": False,
            }
        )
        inflated_fresh_pending["evidence_errors"][-1] = (
            "FRESH_LOCAL_STATE_INCOMPLETE"
        )
        invalid_bodies.append(
            ("inflated-fresh-pending", inflated_fresh_pending)
        )
        failed_with_pre_attempt_reason = _create_failure_v2_test_body()
        failed_with_pre_attempt_reason["recovery"].update(
            {
                "attempted": True,
                "outcome": "FAILED",
            }
        )
        failed_with_pre_attempt_reason["runtime_state"][
            "application_status"
        ] = "READY"
        invalid_bodies.append(
            ("failed-with-pre-attempt-reason", failed_with_pre_attempt_reason)
        )
        attempted_recovery_without_buy = _create_failure_v2_test_body()
        attempted_recovery_without_buy["mutation_guard"].update(
            {
                "mutation_started": False,
                "submission_attempts": [],
            }
        )
        attempted_recovery_without_buy["runtime_state"].update(
            {
                "application_status": "READY",
                "position_quantity": "0",
                "pending_order_count": 0,
                "durable_trade_count": 0,
            }
        )
        attempted_recovery_without_buy["recovery"] = {
            "attempted": True,
            "outcome": "FAILED",
            "typed_reason": "FAILURE_RECOVERY_RUNTIME_FAILED",
        }
        attempted_recovery_without_buy["evidence_errors"][0] = (
            "FAILURE_RECOVERY_RUNTIME_FAILED"
        )
        invalid_bodies.append(
            ("attempted-recovery-without-buy", attempted_recovery_without_buy)
        )
        attempted_recovery_without_runtime_buy = _create_failure_v2_test_body()
        attempted_recovery_without_runtime_buy["mutation_guard"][
            "submission_attempts"
        ] = attempted_recovery_without_runtime_buy["mutation_guard"][
            "submission_attempts"
        ][:1]
        attempted_recovery_without_runtime_buy["runtime_state"][
            "application_status"
        ] = "READY"
        attempted_recovery_without_runtime_buy["runtime_state"][
            "durable_trade_count"
        ] = 0
        attempted_recovery_without_runtime_buy["recovery"] = {
            "attempted": True,
            "outcome": "FAILED",
            "typed_reason": "FAILURE_RECOVERY_RUNTIME_FAILED",
        }
        attempted_recovery_without_runtime_buy["evidence_errors"][0] = (
            "FAILURE_RECOVERY_RUNTIME_FAILED"
        )
        invalid_bodies.append(
            (
                "attempted-recovery-without-runtime-buy",
                attempted_recovery_without_runtime_buy,
            )
        )
        attempted_recovery_without_fresh_buy = _create_failure_v2_test_body()
        attempted_recovery_without_fresh_buy["mutation_guard"][
            "submission_attempts"
        ] = attempted_recovery_without_fresh_buy["mutation_guard"][
            "submission_attempts"
        ][:1]
        attempted_recovery_without_fresh_buy["recovery"] = {
            "attempted": True,
            "outcome": "FAILED",
            "typed_reason": "FAILURE_RECOVERY_RUNTIME_FAILED",
        }
        attempted_recovery_without_fresh_buy["runtime_state"][
            "application_status"
        ] = "READY"
        attempted_recovery_without_fresh_buy["fresh_verification"].update(
            {
                "run_exchange_order_count": 0,
                "durable_trade_count": 0,
            }
        )
        attempted_recovery_without_fresh_buy["evidence_errors"][0] = (
            "FAILURE_RECOVERY_RUNTIME_FAILED"
        )
        invalid_bodies.append(
            (
                "attempted-recovery-without-fresh-buy",
                attempted_recovery_without_fresh_buy,
            )
        )
        attempted_recovery_without_ready = _create_failure_v2_test_body()
        attempted_recovery_without_ready["runtime_state"][
            "application_status"
        ] = "FAILED"
        attempted_recovery_without_ready["recovery"] = {
            "attempted": True,
            "outcome": "FAILED",
            "typed_reason": "FAILURE_RECOVERY_RUNTIME_FAILED",
        }
        attempted_recovery_without_ready["evidence_errors"][0] = (
            "FAILURE_RECOVERY_RUNTIME_FAILED"
        )
        invalid_bodies.append(
            ("attempted-recovery-without-ready", attempted_recovery_without_ready)
        )
        attempted_recovery_without_active_session = (
            _create_failure_v2_test_body()
        )
        attempted_recovery_without_active_session["runtime_state"].update(
            {
                "application_status": "READY",
                "trading_status": "NOT_STARTED",
            }
        )
        attempted_recovery_without_active_session["recovery"] = {
            "attempted": True,
            "outcome": "FAILED",
            "typed_reason": "FAILURE_RECOVERY_RUNTIME_FAILED",
        }
        attempted_recovery_without_active_session["evidence_errors"][0] = (
            "FAILURE_RECOVERY_RUNTIME_FAILED"
        )
        invalid_bodies.append(
            (
                "attempted-recovery-without-active-session",
                attempted_recovery_without_active_session,
            )
        )
        skipped_recovery_without_active_session = (
            _create_failure_v2_test_body()
        )
        skipped_recovery_without_active_session["runtime_state"][
            "trading_status"
        ] = "NOT_STARTED"
        invalid_bodies.append(
            (
                "skipped-recovery-without-active-session",
                skipped_recovery_without_active_session,
            )
        )
        attempted_final_verification_without_sell = (
            _create_failure_v2_test_body()
        )
        attempted_final_verification_without_sell["mutation_guard"][
            "submission_attempts"
        ] = attempted_final_verification_without_sell["mutation_guard"][
            "submission_attempts"
        ][:1]
        attempted_final_verification_without_sell["runtime_state"][
            "application_status"
        ] = "READY"
        attempted_final_verification_without_sell["recovery"] = {
            "attempted": True,
            "outcome": "FAILED",
            "typed_reason": "FAILURE_RECOVERY_FINAL_VERIFICATION_INCOMPLETE",
        }
        attempted_final_verification_without_sell["evidence_errors"][0] = (
            "FAILURE_RECOVERY_FINAL_VERIFICATION_INCOMPLETE"
        )
        attempted_final_verification_without_sell["first_cause"][
            "category"
        ] = ReconciliationCauseCategory.ORDER_OR_PERSISTENCE_AMBIGUOUS.value
        invalid_bodies.append(
            (
                "attempted-final-verification-without-sell",
                attempted_final_verification_without_sell,
            )
        )
        attempted_final_verification_without_terminal_runtime = (
            _create_failure_v2_test_body()
        )
        attempted_final_verification_without_terminal_runtime[
            "runtime_state"
        ]["application_status"] = "READY"
        attempted_final_verification_without_terminal_runtime["recovery"] = {
            "attempted": True,
            "outcome": "FAILED",
            "typed_reason": "FAILURE_RECOVERY_FINAL_VERIFICATION_INCOMPLETE",
        }
        attempted_final_verification_without_terminal_runtime[
            "evidence_errors"
        ][0] = "FAILURE_RECOVERY_FINAL_VERIFICATION_INCOMPLETE"
        attempted_final_verification_without_terminal_runtime["first_cause"][
            "category"
        ] = ReconciliationCauseCategory.ORDER_OR_PERSISTENCE_AMBIGUOUS.value
        invalid_bodies.append(
            (
                "attempted-final-verification-without-terminal-runtime",
                attempted_final_verification_without_terminal_runtime,
            )
        )
        for late_skip_reason in (
            "FAILURE_RECOVERY_EFFECTIVE_FREE_UNCONFIRMED",
            "FAILURE_RECOVERY_STATE_CHANGED",
        ):
            late_skip_without_buy = _create_failure_v2_test_body()
            late_skip_without_buy["mutation_guard"].update(
                {
                    "mutation_started": False,
                    "submission_attempts": [],
                }
            )
            late_skip_without_buy["runtime_state"].update(
                {
                    "position_quantity": "0",
                    "durable_trade_count": 0,
                }
            )
            late_skip_without_buy["recovery"] = {
                "attempted": False,
                "outcome": "SKIPPED",
                "typed_reason": late_skip_reason,
            }
            late_skip_without_buy["evidence_errors"][0] = late_skip_reason
            invalid_bodies.append(
                (
                    f"{late_skip_reason.lower()}-without-buy",
                    late_skip_without_buy,
                )
            )
        late_skip_without_ready = _create_failure_v2_test_body()
        late_skip_without_ready["runtime_state"][
            "application_status"
        ] = "FAILED"
        late_skip_without_ready["mutation_guard"]["submission_attempts"] = (
            late_skip_without_ready["mutation_guard"]["submission_attempts"][
                :1
            ]
        )
        late_skip_without_ready["recovery"] = {
            "attempted": False,
            "outcome": "SKIPPED",
            "typed_reason": "FAILURE_RECOVERY_STATE_CHANGED",
        }
        late_skip_without_ready["evidence_errors"][0] = (
            "FAILURE_RECOVERY_STATE_CHANGED"
        )
        invalid_bodies.append(
            ("late-skip-without-ready", late_skip_without_ready)
        )
        publication_error_without_reconciliation = (
            _create_failure_v2_test_body()
        )
        publication_error_without_reconciliation["runtime_state"][
            "trading_status"
        ] = "TERMINATED"
        publication_error_without_reconciliation["evidence_errors"].append(
            "FAILURE_SCHEDULER_GATE_PUBLICATION_FAILED"
        )
        invalid_bodies.append(
            (
                "publication-error-without-reconciliation-status",
                publication_error_without_reconciliation,
            )
        )
        reconciliation_status_without_blocker = _create_failure_v2_test_body()
        reconciliation_status_without_blocker["runtime_state"][
            "reconciliation_required"
        ] = False
        invalid_bodies.append(
            (
                "reconciliation-status-without-blocker",
                reconciliation_status_without_blocker,
            )
        )
        permanent_cause_without_runtime_blocker = (
            _create_failure_v2_test_body()
        )
        permanent_cause_without_runtime_blocker["runtime_state"].update(
            {
                "trading_status": "TERMINATED",
                "reconciliation_required": False,
            }
        )
        invalid_bodies.append(
            (
                "permanent-cause-without-runtime-blocker",
                permanent_cause_without_runtime_blocker,
            )
        )
        for impossible_permanent_reason in (
            "FAILURE_RECOVERY_EFFECTIVE_FREE_UNCONFIRMED",
            "FAILURE_RECOVERY_STATE_CHANGED",
            "FAILURE_RECOVERY_FINAL_VERIFICATION_INCOMPLETE",
        ):
            permanent_cause_with_late_recovery = (
                _create_failure_v2_test_body()
            )
            permanent_cause_with_late_recovery["recovery"][
                "typed_reason"
            ] = impossible_permanent_reason
            permanent_cause_with_late_recovery["evidence_errors"][0] = (
                impossible_permanent_reason
            )
            invalid_bodies.append(
                (
                    f"permanent-cause-with-{impossible_permanent_reason.lower()}",
                    permanent_cause_with_late_recovery,
                )
            )
        publication_error_without_concrete_blocker = (
            _create_failure_v2_test_body()
        )
        publication_error_without_concrete_blocker["runtime_state"][
            "reconciliation_required"
        ] = None
        publication_error_without_concrete_blocker["evidence_errors"].extend(
            (
                "FAILURE_SCHEDULER_GATE_PUBLICATION_FAILED",
                "RUNTIME_RECONCILIATION_UNAVAILABLE",
            )
        )
        invalid_bodies.append(
            (
                "publication-error-without-concrete-blocker",
                publication_error_without_concrete_blocker,
            )
        )
        publication_error_without_ready = _create_failure_v2_test_body()
        publication_error_without_ready["runtime_state"].update(
            {
                "application_status": "FAILED",
                "trading_status": "RECONCILIATION_REQUIRED",
                "reconciliation_required": True,
            }
        )
        publication_error_without_ready["evidence_errors"].append(
            "FAILURE_SCHEDULER_GATE_PUBLICATION_FAILED"
        )
        invalid_bodies.append(
            ("publication-error-without-ready", publication_error_without_ready)
        )
        runtime_durable_over_attempts = _create_failure_v2_test_body()
        runtime_durable_over_attempts["runtime_state"][
            "durable_trade_count"
        ] = 999
        invalid_bodies.append(
            ("runtime-durable-over-attempts", runtime_durable_over_attempts)
        )
        sell_attempt_without_runtime_buy = _create_failure_v2_test_body()
        sell_attempt_without_runtime_buy["runtime_state"][
            "durable_trade_count"
        ] = 0
        invalid_bodies.append(
            ("sell-attempt-without-runtime-buy", sell_attempt_without_runtime_buy)
        )
        sell_attempt_without_fresh_buy = _create_failure_v2_test_body()
        sell_attempt_without_fresh_buy["fresh_verification"].update(
            {
                "run_exchange_order_count": 0,
                "durable_trade_count": 0,
            }
        )
        invalid_bodies.append(
            ("sell-attempt-without-fresh-buy", sell_attempt_without_fresh_buy)
        )
        one_fresh_buy_with_zero_position = _create_failure_v2_test_body()
        one_fresh_buy_with_zero_position["fresh_verification"].update(
            {
                "status": "VERIFIED",
                "typed_reason": None,
                "failure_stage": None,
                "verified_at": "2026-08-31T12:00:25.000000Z",
                "position_quantity": "0",
                "pending_order_count": 0,
                "reconciliation_required": False,
                "matching_open_order_count": 0,
                "account_open_orders_empty": True,
                "account_open_order_lists_empty": True,
                "run_exchange_order_count": 1,
                "durable_trade_count": 1,
            }
        )
        one_fresh_buy_with_zero_position["evidence_errors"] = [
            "FAILURE_RECOVERY_STATE_AMBIGUOUS"
        ]
        invalid_bodies.append(
            (
                "one-fresh-buy-with-zero-position",
                one_fresh_buy_with_zero_position,
            )
        )
        regressed_fresh_durable_history = _create_failure_v2_test_body()
        regressed_fresh_durable_history["runtime_state"].update(
            {
                "trading_status": "TERMINATED",
                "reconciliation_required": False,
                "position_quantity": "0",
                "pending_order_count": 0,
                "durable_trade_count": 2,
            }
        )
        regressed_fresh_durable_history["fresh_verification"].update(
            {
                "status": "VERIFIED",
                "typed_reason": None,
                "failure_stage": None,
                "verified_at": "2026-08-31T12:00:25.000000Z",
                "position_quantity": "1",
                "pending_order_count": 0,
                "reconciliation_required": False,
                "matching_open_order_count": 0,
                "account_open_orders_empty": True,
                "account_open_order_lists_empty": True,
                "run_exchange_order_count": 1,
                "durable_trade_count": 1,
            }
        )
        regressed_fresh_durable_history["evidence_errors"] = [
            "FAILURE_RECOVERY_STATE_AMBIGUOUS"
        ]
        invalid_bodies.append(
            ("regressed-fresh-durable-history", regressed_fresh_durable_history)
        )
        runtime_position_without_attempt = _create_failure_v2_test_body()
        runtime_position_without_attempt["mutation_guard"].update(
            {
                "mutation_started": False,
                "submission_attempts": [],
            }
        )
        runtime_position_without_attempt["runtime_state"].update(
            {
                "position_quantity": "7.5",
                "durable_trade_count": 0,
            }
        )
        invalid_bodies.append(
            ("runtime-position-without-attempt", runtime_position_without_attempt)
        )
        incomplete_count_mismatch = _create_failure_v2_test_body()
        incomplete_count_mismatch["fresh_verification"].update(
            {
                "typed_reason": "FRESH_STREAM_NOT_READY",
                "failure_stage": "STREAM",
                "run_exchange_order_count": 1,
                "durable_trade_count": 2,
            }
        )
        incomplete_count_mismatch["evidence_errors"][1] = (
            "FRESH_STREAM_NOT_READY"
        )
        invalid_bodies.append(
            ("incomplete-count-mismatch", incomplete_count_mismatch)
        )
        partial_local_group = _create_failure_v2_test_body()
        partial_local_group["fresh_verification"].update(
            {
                "typed_reason": "FRESH_LOCAL_STATE_INCOMPLETE",
                "failure_stage": "LOCAL_STATE",
                "position_quantity": "1",
            }
        )
        partial_local_group["evidence_errors"][1] = (
            "FRESH_LOCAL_STATE_INCOMPLETE"
        )
        invalid_bodies.append(("partial-local-group", partial_local_group))
        complete_local_group = _create_failure_v2_test_body()
        complete_local_group["fresh_verification"].update(
            {
                "typed_reason": "FRESH_LOCAL_STATE_INCOMPLETE",
                "failure_stage": "LOCAL_STATE",
                "position_quantity": "1",
                "pending_order_count": 0,
                "reconciliation_required": False,
            }
        )
        complete_local_group["evidence_errors"][1] = (
            "FRESH_LOCAL_STATE_INCOMPLETE"
        )
        invalid_bodies.append(("complete-local-group", complete_local_group))
        open_list_without_prefix = _create_failure_v2_test_body()
        open_list_without_prefix["fresh_verification"].update(
            {
                "typed_reason": "FRESH_OPEN_ORDER_LISTS_NOT_EMPTY",
                "failure_stage": "OPEN_ORDER_LISTS",
                "account_open_order_lists_empty": True,
            }
        )
        open_list_without_prefix["evidence_errors"][1] = (
            "FRESH_OPEN_ORDER_LISTS_NOT_EMPTY"
        )
        invalid_bodies.append(
            ("open-list-without-prefix", open_list_without_prefix)
        )
        open_list_counts_without_flag = _create_failure_v2_test_body()
        open_list_counts_without_flag["fresh_verification"].update(
            {
                "typed_reason": "FRESH_OPEN_ORDER_LISTS_NOT_EMPTY",
                "failure_stage": "OPEN_ORDER_LISTS",
                "position_quantity": "1",
                "pending_order_count": 0,
                "reconciliation_required": False,
                "account_open_orders_empty": True,
                "matching_open_order_count": 0,
                "run_exchange_order_count": 1,
                "durable_trade_count": 1,
            }
        )
        open_list_counts_without_flag["evidence_errors"][1] = (
            "FRESH_OPEN_ORDER_LISTS_NOT_EMPTY"
        )
        invalid_bodies.append(
            ("open-list-counts-without-flag", open_list_counts_without_flag)
        )
        open_list_initial_empty_without_counts = (
            _create_failure_v2_test_body()
        )
        open_list_initial_empty_without_counts["fresh_verification"].update(
            {
                "typed_reason": "FRESH_OPEN_ORDER_LISTS_NOT_EMPTY",
                "failure_stage": "OPEN_ORDER_LISTS",
                "position_quantity": "1",
                "pending_order_count": 0,
                "reconciliation_required": False,
                "account_open_orders_empty": True,
                "matching_open_order_count": 0,
                "account_open_order_lists_empty": True,
            }
        )
        open_list_initial_empty_without_counts["evidence_errors"][1] = (
            "FRESH_OPEN_ORDER_LISTS_NOT_EMPTY"
        )
        invalid_bodies.append(
            (
                "open-list-initial-empty-without-counts",
                open_list_initial_empty_without_counts,
            )
        )
        changed_open_orders_with_nonempty_flag = _create_failure_v2_test_body()
        changed_open_orders_with_nonempty_flag["fresh_verification"].update(
            {
                "typed_reason": "FRESH_OPEN_ORDERS_CHANGED",
                "failure_stage": "OPEN_ORDERS",
                "position_quantity": "1",
                "pending_order_count": 0,
                "reconciliation_required": False,
                "account_open_orders_empty": False,
                "matching_open_order_count": 0,
                "account_open_order_lists_empty": True,
                "run_exchange_order_count": 1,
                "durable_trade_count": 1,
            }
        )
        changed_open_orders_with_nonempty_flag["evidence_errors"][1] = (
            "FRESH_OPEN_ORDERS_CHANGED"
        )
        invalid_bodies.append(
            (
                "changed-open-orders-with-nonempty-flag",
                changed_open_orders_with_nonempty_flag,
            )
        )
        changed_open_orders_without_matching_count = (
            _create_failure_v2_test_body()
        )
        changed_open_orders_without_matching_count[
            "fresh_verification"
        ].update(
            {
                "typed_reason": "FRESH_OPEN_ORDERS_CHANGED",
                "failure_stage": "OPEN_ORDERS",
                "position_quantity": "1",
                "pending_order_count": 0,
                "reconciliation_required": False,
                "account_open_orders_empty": True,
                "account_open_order_lists_empty": True,
                "run_exchange_order_count": 1,
                "durable_trade_count": 1,
            }
        )
        changed_open_orders_without_matching_count["evidence_errors"][1] = (
            "FRESH_OPEN_ORDERS_CHANGED"
        )
        invalid_bodies.append(
            (
                "changed-open-orders-without-matching-count",
                changed_open_orders_without_matching_count,
            )
        )
        final_open_orders_not_empty_with_matching = (
            _create_failure_v2_test_body()
        )
        final_open_orders_not_empty_with_matching["fresh_verification"].update(
            {
                "typed_reason": "FRESH_OPEN_ORDERS_NOT_EMPTY",
                "failure_stage": "OPEN_ORDERS",
                "position_quantity": "1",
                "pending_order_count": 0,
                "reconciliation_required": False,
                "account_open_orders_empty": True,
                "matching_open_order_count": 2,
                "account_open_order_lists_empty": True,
                "run_exchange_order_count": 1,
                "durable_trade_count": 1,
            }
        )
        final_open_orders_not_empty_with_matching["evidence_errors"][1] = (
            "FRESH_OPEN_ORDERS_NOT_EMPTY"
        )
        invalid_bodies.append(
            (
                "final-open-orders-not-empty-with-matching",
                final_open_orders_not_empty_with_matching,
            )
        )
        for retained_account_flag in (None, False):
            final_open_orders_without_matching = _create_failure_v2_test_body()
            final_open_orders_without_matching["fresh_verification"].update(
                {
                    "typed_reason": "FRESH_OPEN_ORDERS_NOT_EMPTY",
                    "failure_stage": "OPEN_ORDERS",
                    "position_quantity": "1",
                    "pending_order_count": 0,
                    "reconciliation_required": False,
                    "account_open_orders_empty": retained_account_flag,
                    "account_open_order_lists_empty": True,
                    "run_exchange_order_count": 1,
                    "durable_trade_count": 1,
                }
            )
            final_open_orders_without_matching["evidence_errors"][1] = (
                "FRESH_OPEN_ORDERS_NOT_EMPTY"
            )
            invalid_bodies.append(
                (
                    f"final-open-orders-{retained_account_flag}-without-matching",
                    final_open_orders_without_matching,
                )
            )
        recent_without_prefix = _create_failure_v2_test_body()
        recent_without_prefix["fresh_verification"].update(
            {
                "typed_reason": "FRESH_RECENT_ORDERS_CHANGED",
                "failure_stage": "RECENT_ORDERS",
                "durable_trade_count": 1,
            }
        )
        recent_without_prefix["evidence_errors"][1] = (
            "FRESH_RECENT_ORDERS_CHANGED"
        )
        invalid_bodies.append(("recent-without-prefix", recent_without_prefix))
        early_stage_with_verified_at = _create_failure_v2_test_body()
        early_stage_with_verified_at["fresh_verification"]["verified_at"] = (
            "2026-08-31T12:00:25.000000Z"
        )
        invalid_bodies.append(
            ("early-stage-with-verified-at", early_stage_with_verified_at)
        )
        for early_stage in ("RUNTIME_CREATION", "STARTUP_APPLICATION"):
            early_stage_with_downstream_truth = _create_failure_v2_test_body()
            early_stage_with_downstream_truth["fresh_verification"].update(
                {
                    "failure_stage": early_stage,
                    "position_quantity": "1",
                    "pending_order_count": 0,
                    "reconciliation_required": False,
                    "matching_open_order_count": 0,
                    "account_open_orders_empty": True,
                    "account_open_order_lists_empty": True,
                    "run_exchange_order_count": 1,
                    "durable_trade_count": 1,
                }
            )
            invalid_bodies.append(
                (
                    f"{early_stage.lower()}-with-downstream-truth",
                    early_stage_with_downstream_truth,
                )
            )
        primary_close_with_observation = _create_failure_v2_test_body()
        primary_close_with_observation["fresh_verification"].update(
            {
                "typed_reason": "PRIMARY_RUNTIME_NOT_CLOSED",
                "failure_stage": "CLEANUP",
                "position_quantity": "0",
            }
        )
        primary_close_with_observation["evidence_errors"][1] = (
            "PRIMARY_RUNTIME_NOT_CLOSED"
        )
        invalid_bodies.append(
            ("primary-close-with-observation", primary_close_with_observation)
        )
        cleanup_without_complete_truth = _create_failure_v2_test_body()
        cleanup_without_complete_truth["fresh_verification"].update(
            {
                "typed_reason": "FRESH_RUNTIME_CLOSE_FAILED",
                "failure_stage": "CLEANUP",
            }
        )
        cleanup_without_complete_truth["evidence_errors"][1] = (
            "FRESH_RUNTIME_CLOSE_FAILED"
        )
        invalid_bodies.append(
            ("cleanup-without-complete-truth", cleanup_without_complete_truth)
        )
        snapshot_primary_after_close_error = _create_failure_v2_test_body()
        snapshot_primary_after_close_error["fresh_verification"].update(
            {
                "typed_reason": "FRESH_SNAPSHOT_CLEANUP_FAILED",
                "failure_stage": "CLEANUP",
                "verified_at": "2026-08-31T12:00:25.000000Z",
                "position_quantity": "1",
                "pending_order_count": 0,
                "reconciliation_required": False,
                "matching_open_order_count": 0,
                "account_open_orders_empty": True,
                "account_open_order_lists_empty": True,
                "run_exchange_order_count": 1,
                "durable_trade_count": 1,
            }
        )
        snapshot_primary_after_close_error["evidence_errors"] = [
            "FAILURE_RECOVERY_STATE_AMBIGUOUS",
            "FRESH_SNAPSHOT_CLEANUP_FAILED",
            "FRESH_RUNTIME_CLOSE_FAILED",
        ]
        invalid_bodies.append(
            (
                "snapshot-primary-after-close-error",
                snapshot_primary_after_close_error,
            )
        )
        durability_primary_after_close_error = _create_failure_v2_test_body()
        durability_primary_after_close_error["fresh_verification"].update(
            {
                "typed_reason": "FRESH_DURABILITY_CHANGED",
                "failure_stage": "DURABILITY",
            }
        )
        durability_primary_after_close_error["evidence_errors"] = [
            "FAILURE_RECOVERY_STATE_AMBIGUOUS",
            "FRESH_DURABILITY_CHANGED",
            "FRESH_RUNTIME_CLOSE_FAILED",
        ]
        invalid_bodies.append(
            (
                "durability-primary-after-close-error",
                durability_primary_after_close_error,
            )
        )
        false_final_verification_reason = _create_failure_v2_test_body()
        false_final_verification_reason["mutation_guard"].update(
            {
                "mutation_started": False,
                "submission_attempts": [],
            }
        )
        false_final_verification_reason["recovery"] = {
            "attempted": False,
            "outcome": "SKIPPED",
            "typed_reason": "FAILURE_RECOVERY_FINAL_VERIFICATION_INCOMPLETE",
        }
        false_final_verification_reason["runtime_state"].update(
            {
                "trading_status": "NOT_STARTED",
                "reconciliation_required": False,
                "position_quantity": "0",
                "pending_order_count": 0,
                "durable_trade_count": 0,
            }
        )
        false_final_verification_reason["fresh_verification"].update(
            {
                "status": "VERIFIED",
                "typed_reason": None,
                "failure_stage": None,
                "verified_at": "2026-08-31T12:00:25.000000Z",
                "position_quantity": "0",
                "pending_order_count": 0,
                "reconciliation_required": False,
                "matching_open_order_count": 0,
                "account_open_orders_empty": True,
                "account_open_order_lists_empty": True,
                "run_exchange_order_count": 0,
                "durable_trade_count": 0,
            }
        )
        false_final_verification_reason["evidence_errors"] = [
            "FAILURE_RECOVERY_FINAL_VERIFICATION_INCOMPLETE"
        ]
        invalid_bodies.append(
            ("false-final-verification-reason", false_final_verification_reason)
        )
        runtime_error_fields = {
            "reconciliation_required": "RUNTIME_RECONCILIATION_UNAVAILABLE",
            "position_quantity": "RUNTIME_POSITION_UNAVAILABLE",
            "pending_order_count": "RUNTIME_PENDING_UNAVAILABLE",
            "durable_trade_count": "RUNTIME_HISTORY_UNAVAILABLE",
        }
        for field_name, error_code in runtime_error_fields.items():
            missing_runtime_error = _create_failure_v2_test_body()
            missing_runtime_error["runtime_state"][field_name] = None
            invalid_bodies.append(
                (f"missing-{error_code}", missing_runtime_error)
            )
            irrelevant_runtime_error = _create_failure_v2_test_body()
            irrelevant_runtime_error["evidence_errors"].append(error_code)
            invalid_bodies.append(
                (f"irrelevant-{error_code}", irrelevant_runtime_error)
            )
        for case_name, invalid_body in invalid_bodies:
            with self.subTest(case_name=case_name):
                with self.assertRaises(ValueError):
                    _seal_failure_evidence(
                        invalid_body,
                        forbidden_values=redaction_canaries,
                    )

        # Producer가 첫 실패 뒤 관찰할 수 있는 durability·cleanup 후속 error만 추가 허용한다.
        valid_secondary_body = _create_failure_v2_test_body()
        valid_secondary_body["evidence_errors"].extend(
            (
                "FRESH_RUNTIME_CLOSE_FAILED",
                "FRESH_DURABILITY_CHANGED",
                "FRESH_SNAPSHOT_CLEANUP_FAILED",
            )
        )
        _seal_failure_evidence(
            valid_secondary_body,
            forbidden_values=redaction_canaries,
        )
        valid_late_durability = _create_failure_v2_test_body()
        valid_late_durability["fresh_verification"].update(
            {
                "typed_reason": "FRESH_DURABILITY_CHANGED",
                "failure_stage": "DURABILITY",
                "verified_at": "2026-08-31T12:00:25.000000Z",
                "position_quantity": "1",
                "pending_order_count": 0,
                "reconciliation_required": False,
                "matching_open_order_count": 0,
                "account_open_orders_empty": True,
                "account_open_order_lists_empty": True,
                "run_exchange_order_count": 1,
                "durable_trade_count": 1,
            }
        )
        valid_late_durability["evidence_errors"][1] = (
            "FRESH_DURABILITY_CHANGED"
        )
        _seal_failure_evidence(
            valid_late_durability,
            forbidden_values=redaction_canaries,
        )  # VERIFIED 뒤 source fingerprint drift는 complete truth를 보존한 DURABILITY primary다.

    def test_failure_schema_v2_binds_verified_truth_and_timestamps(self) -> None:
        """
        함수 이름: test_failure_schema_v2_binds_verified_truth_and_timestamps()
        기능: VERIFIED terminal truth와 run·attempt·verification 시간 순서의 exact 불변식을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        redaction_canaries = (
            "verified-key-canary-31c8dcb0",
            "verified-secret-canary-5f498c2a",
        )
        verified_body = _create_failure_v2_test_body()
        verified_body["fresh_verification"].update(
            {
                "status": "VERIFIED",
                "typed_reason": None,
                "failure_stage": None,
                "verified_at": "2026-08-31T12:00:25.000000Z",
                "position_quantity": "7.5",
                "pending_order_count": 0,
                "reconciliation_required": False,
                "matching_open_order_count": 0,
                "account_open_orders_empty": True,
                "account_open_order_lists_empty": True,
                "run_exchange_order_count": 1,
                "durable_trade_count": 1,
            }
        )
        verified_body["evidence_errors"] = [
            "FAILURE_RECOVERY_STATE_AMBIGUOUS"
        ]
        _seal_failure_evidence(
            verified_body,
            forbidden_values=redaction_canaries,
        )  # Failure artifact의 complete observation은 nonzero position을 숨기지 않고 허용한다.

        verified_drifts = {
            "pending": ("pending_order_count", 1),
            "reconciliation": ("reconciliation_required", True),
            "matching-open": ("matching_open_order_count", 1),
            "account-open": ("account_open_orders_empty", False),
            "account-list": ("account_open_order_lists_empty", False),
            "exchange-durable": ("run_exchange_order_count", 2),
            "position-unavailable": ("position_quantity", None),
        }
        for case_name, (field_name, field_value) in verified_drifts.items():
            invalid_body = json.loads(json.dumps(verified_body))
            invalid_body["fresh_verification"][field_name] = field_value
            with self.subTest(case_name=case_name):
                with self.assertRaises(ValueError):
                    _seal_failure_evidence(
                        invalid_body,
                        forbidden_values=redaction_canaries,
                    )
        inflated_run_counts = json.loads(json.dumps(verified_body))
        inflated_run_counts["fresh_verification"].update(
            {
                "run_exchange_order_count": 999,
                "durable_trade_count": 999,
            }
        )
        with self.assertRaises(ValueError):
            _seal_failure_evidence(
                inflated_run_counts,
                forbidden_values=redaction_canaries,
            )  # Equal count도 이번 run의 최대 submission attempt 수를 넘으면 생성 불가다.
        nonzero_position_without_durable_trade = json.loads(
            json.dumps(verified_body)
        )
        nonzero_position_without_durable_trade["mutation_guard"].update(
            {
                "mutation_started": False,
                "submission_attempts": [],
            }
        )
        nonzero_position_without_durable_trade["runtime_state"][
            "durable_trade_count"
        ] = 0
        nonzero_position_without_durable_trade["fresh_verification"].update(
            {
                "run_exchange_order_count": 0,
                "durable_trade_count": 0,
            }
        )
        with self.assertRaises(ValueError):
            _seal_failure_evidence(
                nonzero_position_without_durable_trade,
                forbidden_values=redaction_canaries,
            )  # Verified baseline에서 durable suffix가 0이면 새 nonzero Position을 만들 수 없다.
        zero_position_after_single_buy = json.loads(json.dumps(verified_body))
        zero_position_after_single_buy["mutation_guard"][
            "submission_attempts"
        ] = zero_position_after_single_buy["mutation_guard"][
            "submission_attempts"
        ][:1]
        zero_position_after_single_buy["runtime_state"][
            "position_quantity"
        ] = "0"
        zero_position_after_single_buy["fresh_verification"][
            "position_quantity"
        ] = "0"
        with self.assertRaises(ValueError):
            _seal_failure_evidence(
                zero_position_after_single_buy,
                forbidden_values=redaction_canaries,
            )  # SELL attempt 없는 단일 durable BUY는 두 concrete Position을 zero로 만들 수 없다.
        verified_with_fresh_error = json.loads(json.dumps(verified_body))
        verified_with_fresh_error["evidence_errors"].append(
            "FRESH_RUNTIME_CLOSE_FAILED"
        )
        with self.assertRaises(ValueError):
            _seal_failure_evidence(
                verified_with_fresh_error,
                forbidden_values=redaction_canaries,
            )

        timestamp_drifts: list[tuple[str, dict[str, object]]] = []
        attempted_before = _create_failure_v2_test_body()
        attempted_before["mutation_guard"]["submission_attempts"][0][
            "attempted_at"
        ] = "2026-08-31T11:59:59.000000Z"
        timestamp_drifts.append(("attempt-before-run", attempted_before))
        attempted_after = _create_failure_v2_test_body()
        attempted_after["mutation_guard"]["submission_attempts"][1][
            "attempted_at"
        ] = "2026-08-31T12:00:31.000000Z"
        timestamp_drifts.append(("attempt-after-run", attempted_after))
        reversed_attempts = _create_failure_v2_test_body()
        reversed_attempts["mutation_guard"]["submission_attempts"][0][
            "attempted_at"
        ] = "2026-08-31T12:00:21.000000Z"
        timestamp_drifts.append(("attempt-order-reversed", reversed_attempts))
        verified_before = json.loads(json.dumps(verified_body))
        verified_before["fresh_verification"]["verified_at"] = (
            "2026-08-31T11:59:59.000000Z"
        )
        timestamp_drifts.append(("verified-before-run", verified_before))
        verified_before_last_attempt = json.loads(json.dumps(verified_body))
        verified_before_last_attempt["fresh_verification"]["verified_at"] = (
            "2026-08-31T12:00:15.000000Z"
        )
        timestamp_drifts.append(
            ("verified-before-last-attempt", verified_before_last_attempt)
        )
        verified_after = json.loads(json.dumps(verified_body))
        verified_after["fresh_verification"]["verified_at"] = (
            "2026-08-31T12:00:31.000000Z"
        )
        timestamp_drifts.append(("verified-after-run", verified_after))
        for case_name, invalid_body in timestamp_drifts:
            with self.subTest(case_name=case_name):
                with self.assertRaises(ValueError):
                    _seal_failure_evidence(
                        invalid_body,
                        forbidden_values=redaction_canaries,
                    )

    def test_non_exact_first_causes_are_null_and_fail_closed(self) -> None:
        """
        함수 이름: test_non_exact_first_causes_are_null_and_fail_closed()
        기능: missing·동일 duplicate·서로 다른 conflict snapshot이 category 없이 secondary error를 남긴다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        cases = (
            (
                ReconciliationCauseStatus.MISSING,
                False,
                "RECONCILIATION_CAUSE_MISSING",
            ),
            (
                ReconciliationCauseStatus.DUPLICATE,
                True,
                "RECONCILIATION_CAUSE_DUPLICATE",
            ),
            (
                ReconciliationCauseStatus.CONFLICT,
                True,
                "RECONCILIATION_CAUSE_CONFLICT",
            ),
        )
        for cause_status, reconciliation_required, expected_error in cases:
            with self.subTest(cause_status=cause_status.value):
                evidence_errors: list[str] = []
                first_cause = _normalize_failure_first_cause(
                    ReconciliationCauseSnapshot(
                        reconciliation_required=reconciliation_required,
                        status=cause_status,
                        category=None,
                    ),
                    evidence_errors,
                )
                self.assertEqual(cause_status.value, first_cause["status"])
                self.assertIsNone(first_cause["category"])
                self.assertEqual([expected_error], evidence_errors)

        # Cause latch는 process-lifetime이므로 해소된 현재 bool이 false여도 최초 exact category를 유지한다.
        resolved_errors: list[str] = []
        resolved_exact = _normalize_failure_first_cause(
            ReconciliationCauseSnapshot(
                reconciliation_required=False,
                status=ReconciliationCauseStatus.EXACT,
                category=(
                    ReconciliationCauseCategory.ORDER_OR_PERSISTENCE_AMBIGUOUS
                ),
            ),
            resolved_errors,
        )
        self.assertEqual(
            {
                "reconciliation_required": False,
                "status": ReconciliationCauseStatus.EXACT.value,
                "category": (
                    ReconciliationCauseCategory.ORDER_OR_PERSISTENCE_AMBIGUOUS.value
                ),
            },
            resolved_exact,
        )
        self.assertEqual([], resolved_errors)

    def test_fresh_failure_stage_is_first_failure_wins(self) -> None:
        """
        함수 이름: test_fresh_failure_stage_is_first_failure_wins()
        기능: 최초 stage는 cleanup에 덮어쓰지 않고 VERIFIED cleanup 실패는 CLEANUP으로 강등됨을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        first_failure = _create_incomplete_fresh_verification()
        first_errors: list[str] = []
        _set_fresh_verification_failure(
            first_failure,
            "LOCAL_STATE",
            "FRESH_LOCAL_STATE_INCOMPLETE",
            first_errors,
        )
        _set_fresh_verification_failure(
            first_failure,
            "CLEANUP",
            "FRESH_RUNTIME_CLOSE_FAILED",
            first_errors,
        )
        self.assertEqual("LOCAL_STATE", first_failure["failure_stage"])
        self.assertEqual(
            "FRESH_LOCAL_STATE_INCOMPLETE",
            first_failure["typed_reason"],
        )
        self.assertEqual(
            [
                "FRESH_LOCAL_STATE_INCOMPLETE",
                "FRESH_RUNTIME_CLOSE_FAILED",
            ],
            first_errors,
        )

        # 본 검증이 완료된 mapping은 cleanup 실패 순간 성공 주장을 유지하지 않는다.
        verified_then_cleanup = _create_incomplete_fresh_verification()
        verified_then_cleanup.update(
            {
                "status": "VERIFIED",
                "typed_reason": None,
                "verified_at": _datetime_to_wire(_utc_now()),
            }
        )
        cleanup_errors: list[str] = []
        _set_fresh_verification_failure(
            verified_then_cleanup,
            "CLEANUP",
            "FRESH_SNAPSHOT_CLEANUP_FAILED",
            cleanup_errors,
        )
        self.assertEqual("INCOMPLETE", verified_then_cleanup["status"])
        self.assertEqual("CLEANUP", verified_then_cleanup["failure_stage"])
        self.assertEqual(
            "FRESH_SNAPSHOT_CLEANUP_FAILED",
            verified_then_cleanup["typed_reason"],
        )

    def test_fresh_setup_and_typed_startup_failures_are_stage_safe(self) -> None:
        """
        함수 이름: test_fresh_setup_and_typed_startup_failures_are_stage_safe()
        기능: temporary copy 생성 실패와 typed startup failure가 raw 문구 없이 exact stage·code로 남는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        with TemporaryDirectory() as root:
            source_history_path = Path(root) / "trades.jsonl"
            source_history_path.write_bytes(b"")
            os.chmod(source_history_path, 0o600)
            harness = object.__new__(
                BinanceTestnetPhaseThirteenPublicMarketCase2Tests
            )
            harness.history_path = source_history_path
            harness.configuration = SimpleNamespace()
            harness.baseline_recent_exchange_order_ids = frozenset()
            harness.baseline_trades = ()
            harness.evidence_clock = lambda: datetime(
                2026,
                9,
                1,
                tzinfo=timezone.utc,
            )

            # TemporaryDirectory 생성 자체가 실패해도 runtime factory에 도달하지 않고 DURABILITY로 고정한다.
            directory_errors: list[str] = []
            with patch(
                f"{__name__}.TemporaryDirectory",
                side_effect=PermissionError("raw directory detail"),
            ), patch(
                f"{__name__}.create_testnet_application_runtime"
            ) as create_runtime:
                directory_failure = (
                    harness._capture_fresh_failure_verification(
                        frozenset(),
                        directory_errors,
                    )
                )
            self.assertEqual("DURABILITY", directory_failure["failure_stage"])
            self.assertEqual(
                "FRESH_DURABILITY_CHANGED",
                directory_failure["typed_reason"],
            )
            self.assertEqual(
                ["FRESH_DURABILITY_CHANGED"],
                directory_errors,
            )
            create_runtime.assert_not_called()

            runtime = SimpleNamespace()
            startup_failure = ApplicationStartupError(
                StartupFailure(
                    stage=StartupStage.ACCOUNT,
                    code=StartupFailureCode.ACCOUNT_INITIALIZATION_FAILED,
                    message="raw credential-like startup detail",
                )
            )
            startup_errors: list[str] = []
            with patch(
                f"{__name__}.create_testnet_application_runtime",
                return_value=runtime,
            ), patch(
                f"{__name__}.start_application",
                side_effect=startup_failure,
            ), patch(
                f"{__name__}.close_application",
                return_value=ApplicationStateSnapshot(
                    status=ApplicationStatus.CLOSED,
                    version=1,
                    failure=None,
                    startup_trace=(),
                ),
            ) as close_runtime, patch(
                f"{__name__}._create_read_only_testnet_environment",
                return_value={},
            ):
                startup_verification = (
                    harness._capture_fresh_failure_verification(
                        frozenset(),
                        startup_errors,
                    )
                )
            self.assertEqual(
                "ACCOUNT",
                startup_verification["failure_stage"],
            )
            self.assertEqual(
                StartupFailureCode.ACCOUNT_INITIALIZATION_FAILED.value,
                startup_verification["typed_reason"],
            )
            self.assertEqual(
                [StartupFailureCode.ACCOUNT_INITIALIZATION_FAILED.value],
                startup_errors,
            )
            self.assertNotIn(
                "raw credential-like startup detail",
                json.dumps(
                    [startup_verification, startup_errors],
                    sort_keys=True,
                ),
            )
            close_runtime.assert_called_once_with(runtime)

    def test_failure_durability_directory_chain_accepts_fixed_macos_tmp_alias(
        self,
    ) -> None:
        """
        함수 이름: test_failure_durability_directory_chain_accepts_fixed_macos_tmp_alias()
        기능: 최소 runner 환경의 macOS /tmp alias를 private root chain으로 여는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/04
        """
        if not (
            os.path.islink("/tmp")
            and os.readlink("/tmp") == "private/tmp"
        ):
            self.skipTest("fixed macOS /tmp alias is unavailable")

        # 실제 private temporary directory를 만든 뒤 lexical /tmp alias로 같은 inode를 연다.
        with TemporaryDirectory(dir="/private/tmp") as private_root:
            private_root_path = Path(private_root)
            alias_root_path = Path("/tmp") / private_root_path.name
            (
                directory_descriptors,
                directory_identities,
                descriptor_identity,
            ) = _open_failure_durability_directory_chain(
                alias_root_path,
                require_private_mode=True,
            )
            try:
                private_root_state = os.stat(
                    private_root_path,
                    follow_symlinks=False,
                )
                self.assertEqual(
                    private_root_state.st_ino,
                    descriptor_identity[1],
                )  # Alias와 canonical path가 같은 final directory inode를 가리켜야 한다.
                self.assertEqual(
                    len(private_root_path.parts),
                    len(directory_identities),
                )
            finally:
                _close_failure_durability_directory_chain(
                    directory_descriptors
                )  # 검증 성공과 assertion 실패 모두에서 전체 pinned chain을 닫는다.

    def test_failure_durability_snapshot_is_descriptor_bound_and_fsynced(
        self,
    ) -> None:
        """
        함수 이름: test_failure_durability_snapshot_is_descriptor_bound_and_fsynced()
        기능: distinct inode copy와 link·inode/content ABA·drift·fsync 실패 차단을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/01
        """
        with TemporaryDirectory() as root:
            root_path = Path(root)
            source_directory = root_path / "source"
            source_directory.mkdir(mode=0o700)
            source_history_path = source_directory / "history.jsonl"
            for file_suffix, file_bytes in zip(
                _FAILURE_DURABILITY_FILE_SUFFIXES,
                (b"history\n", b"pending\n", b"manual\n"),
                strict=True,
            ):
                source_leaf = source_history_path.with_name(
                    f"{source_history_path.name}{file_suffix}"
                )
                source_leaf.write_bytes(file_bytes)
                os.chmod(source_leaf, 0o600)

            copy_directory = root_path / "copy"
            copy_directory.mkdir(mode=0o700)
            snapshot_handle = _copy_failure_durability_snapshot(
                source_history_path,
                copy_directory,
            )
            isolated_history_path = snapshot_handle.history_path
            snapshot_handle.verify()
            source_fingerprints = _capture_failure_durability_fingerprint(
                source_history_path
            )
            copy_fingerprints = _capture_failure_durability_fingerprint(
                isolated_history_path
            )
            _require_failure_durability_snapshot_equivalence(
                source_fingerprints,
                copy_fingerprints,
            )
            self.assertTrue(all(item.exists for item in source_fingerprints))
            self.assertTrue(
                all(
                    item.mode == 0o600
                    and item.user_id == os.geteuid()
                    and item.link_count == 1
                    and item.device is not None
                    and item.inode is not None
                    and item.change_time_ns is not None
                    and item.modification_time_ns is not None
                    and item.sha256 is not None
                    for item in source_fingerprints
                )
            )

            # 같은 inode의 bytes와 mtime까지 복원해도 owner가 되돌릴 수 없는 ctime drift는 남는다.
            content_aba_before = _capture_failure_durability_fingerprint(
                isolated_history_path
            )
            original_copy_bytes = isolated_history_path.read_bytes()
            original_copy_state = os.stat(
                isolated_history_path,
                follow_symlinks=False,
            )
            isolated_history_path.write_bytes(b"x" * len(original_copy_bytes))
            isolated_history_path.write_bytes(original_copy_bytes)
            os.chmod(isolated_history_path, 0o600)
            os.utime(
                isolated_history_path,
                ns=(
                    original_copy_state.st_atime_ns,
                    original_copy_state.st_mtime_ns,
                ),
                follow_symlinks=False,
            )
            snapshot_handle.verify()
            content_aba_after = _capture_failure_durability_fingerprint(
                isolated_history_path
            )
            self.assertEqual(
                (
                    content_aba_before[0].inode,
                    content_aba_before[0].size,
                    content_aba_before[0].modification_time_ns,
                    content_aba_before[0].sha256,
                ),
                (
                    content_aba_after[0].inode,
                    content_aba_after[0].size,
                    content_aba_after[0].modification_time_ns,
                    content_aba_after[0].sha256,
                ),
            )
            self.assertNotEqual(
                content_aba_before[0].change_time_ns,
                content_aba_after[0].change_time_ns,
            )
            self.assertNotEqual(content_aba_before, content_aba_after)

            # Same-byte path replacement은 SHA가 같아도 inode identity 변경으로 감지한다.
            original_fingerprint = _capture_failure_durability_fingerprint(
                source_history_path
            )
            replacement_path = source_directory / "replacement.jsonl"
            replacement_path.write_bytes(source_history_path.read_bytes())
            os.chmod(replacement_path, 0o600)
            os.replace(replacement_path, source_history_path)
            replaced_fingerprint = _capture_failure_durability_fingerprint(
                source_history_path
            )
            self.assertEqual(
                original_fingerprint[0].sha256,
                replaced_fingerprint[0].sha256,
            )
            self.assertNotEqual(
                original_fingerprint[0].inode,
                replaced_fingerprint[0].inode,
            )
            self.assertNotEqual(
                original_fingerprint,
                replaced_fingerprint,
            )

            # Source와 isolated copy의 content 변조도 각 before fingerprint와 exact 다르다.
            source_before_mutation = replaced_fingerprint
            source_history_path.write_bytes(b"changed-source\n")
            os.chmod(source_history_path, 0o600)
            self.assertNotEqual(
                source_before_mutation,
                _capture_failure_durability_fingerprint(source_history_path),
            )
            copy_before_mutation = copy_fingerprints
            isolated_history_path.write_bytes(b"changed-copy\n")
            os.chmod(isolated_history_path, 0o600)
            self.assertNotEqual(
                copy_before_mutation,
                _capture_failure_durability_fingerprint(isolated_history_path),
            )

            symlink_target = root_path / "symlink-target.jsonl"
            symlink_target.write_bytes(b"target\n")
            os.chmod(symlink_target, 0o600)
            symlink_directory = root_path / "symlink-source"
            symlink_directory.mkdir(mode=0o700)
            os.symlink(symlink_target, symlink_directory / "history.jsonl")
            with self.assertRaises(PermissionError):
                _capture_failure_durability_fingerprint(
                    symlink_directory / "history.jsonl"
                )

            hardlink_directory = root_path / "hardlink-source"
            hardlink_directory.mkdir(mode=0o700)
            hardlink_history = hardlink_directory / "history.jsonl"
            hardlink_history.write_bytes(b"hardlink\n")
            os.chmod(hardlink_history, 0o600)
            os.link(hardlink_history, hardlink_directory / "alias.jsonl")
            with self.assertRaises(PermissionError):
                _capture_failure_durability_fingerprint(hardlink_history)
            snapshot_handle.close()

            # 중간 symlink와 group-writable source parent는 leaf가 안전해도 descriptor walk 전에 거부한다.
            real_intermediate = root_path / "real-intermediate"
            real_intermediate.mkdir(mode=0o700)
            intermediate_history = real_intermediate / "history.jsonl"
            intermediate_history.write_bytes(b"intermediate\n")
            os.chmod(intermediate_history, 0o600)
            intermediate_alias = root_path / "intermediate-alias"
            intermediate_alias.symlink_to(
                real_intermediate,
                target_is_directory=True,
            )
            with self.assertRaises(OSError):
                _capture_failure_durability_fingerprint(
                    intermediate_alias / "history.jsonl"
                )
            writable_source = root_path / "writable-source"
            writable_source.mkdir(mode=0o700)
            writable_history = writable_source / "history.jsonl"
            writable_history.write_bytes(b"writable\n")
            os.chmod(writable_history, 0o600)
            os.chmod(writable_source, 0o770)
            with self.assertRaises(PermissionError):
                _capture_failure_durability_fingerprint(writable_history)

            # Directory를 다른 inode로 바꿨다가 되돌려도 pinned inode ctime이 달라져 handoff가 실패한다.
            aba_directory = root_path / "aba-copy"
            aba_directory.mkdir(mode=0o700)
            aba_handle = _copy_failure_durability_snapshot(
                source_history_path,
                aba_directory,
            )
            parked_aba_directory = root_path / "aba-copy-parked"
            replacement_aba_directory = root_path / "aba-copy-replacement"
            aba_directory.rename(parked_aba_directory)
            aba_directory.mkdir(mode=0o700)
            aba_directory.rename(replacement_aba_directory)
            parked_aba_directory.rename(aba_directory)
            with self.assertRaises(RuntimeError):
                aba_handle.verify()
            aba_handle.close()

            # Source의 absent sidecar를 만들었다 지워 leaf tuple이 복원돼도 parent ctime은 복원되지 않는다.
            source_aba_directory = root_path / "source-entry-aba"
            source_aba_directory.mkdir(mode=0o700)
            source_aba_history = source_aba_directory / "history.jsonl"
            source_aba_history.write_bytes(b"source-entry\n")
            os.chmod(source_aba_history, 0o600)
            source_aba_copy = root_path / "source-entry-aba-copy"
            source_aba_copy.mkdir(mode=0o700)
            source_aba_handle = _copy_failure_durability_snapshot(
                source_aba_history,
                source_aba_copy,
            )
            source_aba_before = _capture_failure_durability_fingerprint(
                source_aba_history
            )
            transient_pending = source_aba_history.with_name(
                f"{source_aba_history.name}.pending-orders.jsonl"
            )
            transient_pending.write_bytes(b"transient\n")
            os.chmod(transient_pending, 0o600)
            transient_pending.unlink()
            source_aba_after = _capture_failure_durability_fingerprint(
                source_aba_history
            )
            self.assertEqual(source_aba_before, source_aba_after)
            with self.assertRaises(RuntimeError):
                source_aba_handle.verify()
            source_aba_handle.close()

            # Final directory는 그대로 복원하더라도 source/copy 중간 ancestor의 rename ABA는 chain ctime에 남는다.
            for ancestor_owner in ("source", "copy"):
                with self.subTest(ancestor_owner=ancestor_owner):
                    ancestor_directory = (
                        root_path / f"{ancestor_owner}-ancestor-owner"
                    )
                    ancestor_directory.mkdir(mode=0o700)
                    mutable_parent = ancestor_directory / "stable-parent"
                    mutable_parent.mkdir(mode=0o700)
                    mutable_ancestor = mutable_parent / "mutable-ancestor"
                    mutable_ancestor.mkdir(mode=0o700)
                    nested_directory = mutable_ancestor / "nested"
                    nested_directory.mkdir(mode=0o700)
                    peer_directory = (
                        root_path / f"{ancestor_owner}-ancestor-peer"
                    )
                    peer_directory.mkdir(mode=0o700)
                    if ancestor_owner == "source":
                        ancestor_source_directory = nested_directory
                        ancestor_copy_directory = peer_directory
                    else:
                        ancestor_source_directory = peer_directory
                        ancestor_copy_directory = nested_directory
                    ancestor_history = (
                        ancestor_source_directory / "history.jsonl"
                    )
                    ancestor_history.write_bytes(b"ancestor-chain\n")
                    os.chmod(ancestor_history, 0o600)
                    ancestor_handle = _copy_failure_durability_snapshot(
                        ancestor_history,
                        ancestor_copy_directory,
                    )
                    watched_final_directory = (
                        ancestor_source_directory
                        if ancestor_owner == "source"
                        else ancestor_copy_directory
                    )
                    watched_state_before = os.stat(
                        watched_final_directory,
                        follow_symlinks=False,
                    )
                    watched_identity_before = (
                        watched_state_before.st_dev,
                        watched_state_before.st_ino,
                        watched_state_before.st_ctime_ns,
                    )
                    parked_ancestor = mutable_parent / "parked-ancestor"
                    try:
                        mutable_ancestor.rename(parked_ancestor)
                        mutable_ancestor.mkdir(mode=0o700)
                        mutable_ancestor.rmdir()
                        parked_ancestor.rename(mutable_ancestor)
                        watched_state_after = os.stat(
                            watched_final_directory,
                            follow_symlinks=False,
                        )
                        self.assertEqual(
                            watched_identity_before,
                            (
                                watched_state_after.st_dev,
                                watched_state_after.st_ino,
                                watched_state_after.st_ctime_ns,
                            ),
                        )
                        with self.assertRaises(RuntimeError):
                            ancestor_handle.verify()
                    finally:
                        ancestor_handle.close()

            # Destination file·directory fsync 어느 경계라도 실패하면 snapshot을 공개하지 않는다.
            fsync_file_directory = root_path / "fsync-file"
            fsync_file_directory.mkdir(mode=0o700)
            with patch.object(
                os,
                "fsync",
                side_effect=OSError("raw file fsync detail"),
            ):
                with self.assertRaises(OSError):
                    _copy_failure_durability_snapshot(
                        source_history_path,
                        fsync_file_directory,
                    )

            real_fsync = os.fsync

            def reject_directory_fsync(file_descriptor: int) -> None:
                """
                함수 이름: reject_directory_fsync()
                기능: regular file publish는 허용하고 destination directory fsync만 실패시킨다.
                인자: file_descriptor -> helper가 fsync할 pinned descriptor
                반환값: regular file은 실제 fsync 완료, directory는 OSError
                작성 날짜: 2026/08/31
                """
                if stat.S_ISDIR(os.fstat(file_descriptor).st_mode):
                    raise OSError("raw directory fsync detail")
                real_fsync(file_descriptor)

            fsync_directory = root_path / "fsync-directory"
            fsync_directory.mkdir(mode=0o700)
            with patch.object(
                os,
                "fsync",
                side_effect=reject_directory_fsync,
            ):
                with self.assertRaises(OSError):
                    _copy_failure_durability_snapshot(
                        source_history_path,
                        fsync_directory,
                    )

            # Body와 destination cleanup이 함께 실패해도 source chain까지 닫고 첫 cleanup 오류를 반환한다.
            body_cleanup_directory = root_path / "body-cleanup"
            body_cleanup_directory.mkdir(mode=0o700)
            real_close_chain = _close_failure_durability_directory_chain
            closed_descriptor_chains: list[tuple[int, ...]] = []
            destination_cleanup_error = OSError(
                "destination chain cleanup failed"
            )

            def close_chain_with_destination_failure(
                directory_descriptors: tuple[int, ...],
            ) -> None:
                """
                함수 이름: close_chain_with_destination_failure()
                기능: 실제 chain을 닫은 뒤 첫 destination cleanup만 실패로 보고한다.
                인자: directory_descriptors -> cleanup 대상 descriptor chain
                반환값: source cleanup이면 없음, 첫 destination cleanup이면 OSError
                작성 날짜: 2026/09/04
                """
                closed_descriptor_chains.append(directory_descriptors)
                real_close_chain(directory_descriptors)
                if len(closed_descriptor_chains) == 1:
                    raise destination_cleanup_error

            with patch.object(
                os,
                "fsync",
                side_effect=OSError("snapshot body failed"),
            ), patch(
                f"{__name__}._close_failure_durability_directory_chain",
                side_effect=close_chain_with_destination_failure,
            ):
                with self.assertRaises(OSError) as cleanup_context:
                    _copy_failure_durability_snapshot(
                        source_history_path,
                        body_cleanup_directory,
                    )
            self.assertIs(
                destination_cleanup_error,
                cleanup_context.exception,
            )
            self.assertEqual(2, len(closed_descriptor_chains))
            for descriptor_chain in closed_descriptor_chains:
                for directory_descriptor in descriptor_chain:
                    with self.assertRaises(OSError):
                        os.fstat(directory_descriptor)

    def test_fresh_verification_uses_isolated_durability_and_rest_sandwich(
        self,
    ) -> None:
        """
        함수 이름: test_fresh_verification_uses_isolated_durability_and_rest_sandwich()
        기능: source 복제 경계와 final REST drift·copy mutation의 exact 최초 stage를 무네트워크 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        drift_order = SimpleNamespace(
            client_order_id="fresh-rest-drift",
            exchange_order_id="9001",
        )
        closed_snapshot = ApplicationStateSnapshot(
            status=ApplicationStatus.CLOSED,
            version=1,
            failure=None,
            startup_trace=(),
        )
        failed_snapshot = ApplicationStateSnapshot(
            status=ApplicationStatus.FAILED,
            version=2,
            failure=StartupFailure(
                stage=StartupStage.APPLICATION,
                code=StartupFailureCode.APPLICATION_CLOSED,
                message="safe typed close fixture",
            ),
            startup_trace=(),
        )
        shutting_down_snapshot = ApplicationStateSnapshot(
            status=ApplicationStatus.SHUTTING_DOWN,
            version=2,
            failure=None,
            startup_trace=(),
        )
        cases = (
            (
                "stable",
                (False, False),
                (False, False),
                ((), ()),
                False,
                None,
                "VERIFIED",
                None,
                None,
                closed_snapshot,
            ),
            (
                "open-orders-drift",
                (False, True),
                (False, False),
                ((), ()),
                False,
                None,
                "INCOMPLETE",
                "OPEN_ORDERS",
                "FRESH_OPEN_ORDERS_NOT_EMPTY",
                closed_snapshot,
            ),
            (
                "open-order-lists-drift",
                (False, False),
                (False, True),
                ((), ()),
                False,
                None,
                "INCOMPLETE",
                "OPEN_ORDER_LISTS",
                "FRESH_OPEN_ORDER_LISTS_NOT_EMPTY",
                closed_snapshot,
            ),
            (
                "recent-orders-drift",
                (False, False),
                (False, False),
                ((), (drift_order,)),
                False,
                None,
                "INCOMPLETE",
                "RECENT_ORDERS",
                "FRESH_RECENT_ORDERS_CHANGED",
                closed_snapshot,
            ),
            (
                "isolated-copy-mutation",
                (False, False),
                (False, False),
                ((), ()),
                True,
                None,
                "INCOMPLETE",
                "DURABILITY",
                "FRESH_DURABILITY_CHANGED",
                closed_snapshot,
            ),
            (
                "final-absent-sidecar-aba",
                (False, False),
                (False, False),
                ((), ()),
                False,
                "absent-sidecar",
                "INCOMPLETE",
                "DURABILITY",
                "FRESH_DURABILITY_CHANGED",
                closed_snapshot,
            ),
            (
                "final-source-leaf-aba",
                (False, False),
                (False, False),
                ((), ()),
                False,
                "source-leaf",
                "INCOMPLETE",
                "DURABILITY",
                "FRESH_DURABILITY_CHANGED",
                closed_snapshot,
            ),
            (
                "close-failed",
                (False, False),
                (False, False),
                ((), ()),
                False,
                None,
                "INCOMPLETE",
                "CLEANUP",
                "FRESH_RUNTIME_CLOSE_FAILED",
                failed_snapshot,
            ),
            (
                "close-shutting-down",
                (False, False),
                (False, False),
                ((), ()),
                False,
                None,
                "INCOMPLETE",
                "CLEANUP",
                "FRESH_RUNTIME_CLOSE_FAILED",
                shutting_down_snapshot,
            ),
            (
                "close-non-exact",
                (False, False),
                (False, False),
                ((), ()),
                False,
                None,
                "INCOMPLETE",
                "CLEANUP",
                "FRESH_RUNTIME_CLOSE_FAILED",
                SimpleNamespace(status=ApplicationStatus.CLOSED),
            ),
            (
                "close-exception",
                (False, False),
                (False, False),
                ((), ()),
                False,
                None,
                "INCOMPLETE",
                "CLEANUP",
                "FRESH_RUNTIME_CLOSE_FAILED",
                RuntimeError("raw close detail"),
            ),
        )
        for (
            case_name,
            open_order_states,
            open_order_list_states,
            recent_order_states,
            mutate_isolated_copy,
            final_durability_mutation,
            expected_status,
            expected_stage,
            expected_reason,
            close_outcome,
        ) in cases:
            with self.subTest(case_name=case_name), TemporaryDirectory() as root:
                source_history_path = Path(root) / "trades.jsonl"
                source_leaves = {
                    source_history_path: b'{"history":"source"}\n',
                    source_history_path.with_name(
                        f"{source_history_path.name}.pending-orders.jsonl"
                    ): b'{"pending":"source"}\n',
                    source_history_path.with_name(
                        f"{source_history_path.name}.manual-kill-control.jsonl"
                    ): b'{"manual_kill":"source"}\n',
                }
                if final_durability_mutation == "absent-sidecar":
                    source_leaves.pop(
                        source_history_path.with_name(
                            f"{source_history_path.name}.manual-kill-control.jsonl"
                        )
                    )  # Final fingerprint 중 create→delete할 leaf는 initial snapshot에서 absent다.
                for source_path, source_bytes in source_leaves.items():
                    source_path.write_bytes(source_bytes)
                    os.chmod(source_path, 0o600)
                source_bytes_before = {
                    source_path: source_path.read_bytes()
                    for source_path in source_leaves
                }

                gateway = Mock()
                gateway.fetch_account_relevant_filters.return_value = (
                    SimpleNamespace(symbol="ETHUSDT")
                )
                gateway.fetch_symbol_trading_rules.return_value = SimpleNamespace(
                    symbol="ETHUSDT",
                    base_asset_precision=8,
                    lot_size=SimpleNamespace(
                        minimum_quantity=Decimal("0.001"),
                        step_size=Decimal("0.001"),
                    ),
                    market_lot_size=SimpleNamespace(
                        minimum_quantity=Decimal("0.001"),
                        step_size=Decimal("0.001"),
                    ),
                )
                gateway.fetch_reference_price.return_value = SimpleNamespace(
                    symbol="ETHUSDT",
                    price=Decimal("2500"),
                )
                gateway.has_any_exchange_open_orders.side_effect = (
                    open_order_states
                )
                gateway.list_all_open_order_results.side_effect = ((), ())
                gateway.has_any_exchange_open_order_lists.side_effect = (
                    open_order_list_states
                )
                gateway.list_all_recent_order_results.side_effect = (
                    recent_order_states
                )
                runtime = SimpleNamespace(
                    api_gateway=gateway,
                    web_socket_gateway=SimpleNamespace(
                        account_ready=True,
                        kline_live_ready=True,
                    ),
                    trading_controller=SimpleNamespace(
                        position=SimpleNamespace(quantity=Decimal("0")),
                        reconciliation_required=False,
                    ),
                    trade_history_controller=SimpleNamespace(
                        get_pending_orders=Mock(return_value=())
                    ),
                    trade_history=SimpleNamespace(trades=()),
                )
                harness = object.__new__(
                    BinanceTestnetPhaseThirteenPublicMarketCase2Tests
                )
                harness.history_path = source_history_path
                harness.configuration = SimpleNamespace()
                harness.baseline_recent_exchange_order_ids = frozenset()
                harness.baseline_trades = ()
                harness.evidence_clock = lambda: datetime(
                    2026,
                    9,
                    1,
                    tzinfo=timezone.utc,
                )
                isolated_paths: list[Path] = []

                def create_runtime_from_snapshot(
                    *observers: object,
                    history_path: Path,
                    environment: Mapping[str, object],
                    clock: Callable[[], datetime],
                ) -> object:
                    """
                    함수 이름: create_runtime_from_snapshot()
                    기능: runtime factory에 source가 아닌 owner-only isolated history가 전달되었는지 고정한다.
                    인자: observers -> fresh transport observer tuple
                        history_path -> harness가 선택한 durability copy path
                        environment -> read-only Testnet environment mapping
                        clock -> run-scoped evidence clock
                    반환값: local fake ApplicationRuntime
                    작성 날짜: 2026/08/31
                    """
                    if (
                        not observers
                        or not isinstance(environment, Mapping)
                        or clock is not harness.evidence_clock
                    ):
                        raise AssertionError("fresh runtime inputs are incomplete")
                    if history_path == source_history_path:
                        raise AssertionError("fresh runtime received source history")
                    if history_path.stat().st_mode & 0o777 != 0o600:
                        raise AssertionError("fresh history copy is not owner-only")
                    isolated_paths.append(history_path)
                    return runtime

                def start_runtime_from_snapshot(
                    selected_runtime: object,
                ) -> object:
                    """
                    함수 이름: start_runtime_from_snapshot()
                    기능: 선택 case에서 isolated manual-kill copy만 변경한 뒤 READY를 반환한다.
                    인자: selected_runtime -> factory가 반환한 local fake runtime
                    반환값: READY status object
                    작성 날짜: 2026/08/31
                    """
                    if selected_runtime is not runtime or not isolated_paths:
                        raise AssertionError("fresh runtime identity is invalid")
                    if mutate_isolated_copy:
                        isolated_manual_path = isolated_paths[0].with_name(
                            f"{isolated_paths[0].name}.manual-kill-control.jsonl"
                        )
                        isolated_manual_path.write_bytes(
                            b'{"manual_kill":"changed-copy"}\n'
                        )
                        os.chmod(isolated_manual_path, 0o600)
                    return SimpleNamespace(status=ApplicationStatus.READY)

                def close_runtime_from_snapshot(
                    selected_runtime: object,
                ) -> object:
                    """
                    함수 이름: close_runtime_from_snapshot()
                    기능: exact CLOSED, non-CLOSED, type drift와 close 예외을 결정적으로 반환한다.
                    인자: selected_runtime -> 회수할 fresh fake runtime
                    반환값: case가 지정한 close publication 또는 예외
                    작성 날짜: 2026/08/31
                    """
                    if selected_runtime is not runtime:
                        raise AssertionError("fresh close runtime changed")
                    if isinstance(close_outcome, Exception):
                        raise close_outcome
                    return close_outcome

                evidence_errors: list[str] = []
                real_capture_durability_fingerprint = (
                    _capture_failure_durability_fingerprint
                )
                fingerprint_call_count = 0

                def capture_with_final_durability_aba(
                    selected_history_path: Path,
                ) -> tuple[_FailureDurabilityFileFingerprint, ...]:
                    """
                    함수 이름: capture_with_final_durability_aba()
                    기능: final source capture 전 parent ABA 또는 source→copy 사이 leaf ABA를 삽입한다.
                    인자: selected_history_path -> fingerprint할 history path
                    반환값: 실제 helper가 만든 세 leaf fingerprint tuple
                    작성 날짜: 2026/09/04
                    """
                    nonlocal fingerprint_call_count
                    fingerprint_call_count += 1
                    if (
                        final_durability_mutation == "absent-sidecar"
                        and fingerprint_call_count == 4
                    ):
                        transient_sidecar = source_history_path.with_name(
                            f"{source_history_path.name}.manual-kill-control.jsonl"
                        )
                        transient_sidecar.write_bytes(b"transient-final-aba\n")
                        os.chmod(transient_sidecar, 0o600)
                        transient_sidecar.unlink()
                    if (
                        final_durability_mutation == "source-leaf"
                        and fingerprint_call_count == 5
                    ):
                        source_bytes = source_history_path.read_bytes()
                        source_state = os.stat(
                            source_history_path,
                            follow_symlinks=False,
                        )
                        source_history_path.write_bytes(
                            b"x" * len(source_bytes)
                        )
                        source_history_path.write_bytes(source_bytes)
                        os.chmod(source_history_path, 0o600)
                        os.utime(
                            source_history_path,
                            ns=(
                                source_state.st_atime_ns,
                                source_state.st_mtime_ns,
                            ),
                            follow_symlinks=False,
                        )  # Bytes·inode·mtime을 복원해도 leaf ctime은 복원할 수 없다.

                    return real_capture_durability_fingerprint(
                        selected_history_path
                    )  # 호출 위치는 최종 source→copy→source fingerprint 순서를 그대로 따른다.

                with patch(
                    f"{__name__}.create_testnet_application_runtime",
                    side_effect=create_runtime_from_snapshot,
                ), patch(
                    f"{__name__}.start_application",
                    side_effect=start_runtime_from_snapshot,
                ), patch(
                    f"{__name__}.close_application",
                    side_effect=close_runtime_from_snapshot,
                ) as close_runtime, patch(
                    f"{__name__}._create_read_only_testnet_environment",
                    return_value={},
                ), patch(
                    f"{__name__}.AccountRelevantFilters",
                    SimpleNamespace,
                ), patch(
                    f"{__name__}.SymbolTradingRules",
                    SimpleNamespace,
                ), patch(
                    f"{__name__}.ReferencePrice",
                    SimpleNamespace,
                ), patch(
                    f"{__name__}.validate_account_relevant_filters",
                ), patch(
                    f"{__name__}.verify_exact_recent_order_baseline",
                ) as verify_recent, patch(
                    f"{__name__}._capture_failure_durability_fingerprint",
                    side_effect=capture_with_final_durability_aba,
                ):
                    verification = harness._capture_fresh_failure_verification(
                        frozenset(),
                        evidence_errors,
                    )

                self.assertEqual(expected_status, verification["status"])
                self.assertEqual(expected_stage, verification["failure_stage"])
                self.assertEqual(expected_reason, verification["typed_reason"])
                self.assertEqual(
                    [] if expected_reason is None else [expected_reason],
                    evidence_errors,
                )
                self.assertEqual(
                    6,
                    fingerprint_call_count,
                )  # Initial 3회와 final source→copy→source 3회를 정확히 소비한다.
                self.assertEqual(
                    2
                    if case_name
                    in {
                        "stable",
                        "isolated-copy-mutation",
                        "final-absent-sidecar-aba",
                        "final-source-leaf-aba",
                        "close-failed",
                        "close-shutting-down",
                        "close-non-exact",
                        "close-exception",
                    }
                    else 1,
                    verify_recent.call_count,
                )
                self.assertEqual(1, len(isolated_paths))
                self.assertNotEqual(source_history_path, isolated_paths[0])
                close_runtime.assert_called_once_with(runtime)
                for source_path, source_bytes in source_bytes_before.items():
                    self.assertEqual(
                        source_bytes,
                        source_path.read_bytes(),
                    )  # Startup fault도 authoritative source의 3개 leaf를 한 byte도 변경하지 못한다.

    def test_actual_failure_finalizer_recovers_then_blocks_and_preserves_error(
        self,
    ) -> None:
        """
        함수 이름: test_actual_failure_finalizer_recovers_then_blocks_and_preserves_error()
        기능: terminal recovery와 wall-clock 역행에도 제출 차단·FAILED artifact를 보존하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/01
        """
        observed_at = _utc_now()
        monotonic_values = iter((100, 100, 101))
        wall_clock = Mock(return_value=observed_at)
        evidence_clock = _RunScopedEvidenceClock(
            wall_clock=wall_clock,
            monotonic_clock=lambda: next(monotonic_values),
        )
        started_at = evidence_clock()
        buy_attempt = Phase13OrderSubmissionAttempt(
            symbol="ETHUSDT",
            side=OrderSide.BUY,
            order_type="MARKET",
            intent_id="intent-buy-1",
            submission_attempt=0,
            attempted_at=observed_at,
            client_order_id="bat-p13-client-buy-1",
        )
        sell_attempt = Phase13OrderSubmissionAttempt(
            symbol="ETHUSDT",
            side=OrderSide.SELL,
            order_type="MARKET",
            intent_id="intent-sell-1",
            submission_attempt=0,
            attempted_at=observed_at,
            client_order_id="bat-p13-client-sell-1",
        )
        guard_snapshot = Phase13OrderSubmissionGuardSnapshot(
            mutation_started=True,
            submissions_blocked=True,
            attempts=(buy_attempt, sell_attempt),
        )
        gateway = Mock()
        gateway.get_phase13_order_submission_guard_snapshot.return_value = (
            guard_snapshot
        )
        controller = Mock()
        controller.status = TradingSessionStatus.TERMINATED
        controller.reconciliation_required = False
        controller.reconciliation_cause_snapshot = ReconciliationCauseSnapshot(
            reconciliation_required=False,
            status=ReconciliationCauseStatus.EXACT,
            category=(
                ReconciliationCauseCategory.ACCOUNT_STREAM_UNKNOWN_OR_EXTERNAL_EXECUTION
            ),
        )

        def reopen_terminal_reconciliation() -> None:
            """
            함수 이름: reopen_terminal_reconciliation()
            기능: 과거 무조건 mark 구현이 terminal recovery를 다시 blocker로 바꾸는 production 부작용을 모사한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/08/31
            """
            controller.reconciliation_required = True

        controller.mark_event_runtime_failed.side_effect = (
            reopen_terminal_reconciliation
        )
        controller.seal_event_runtime_failure_gate.return_value = False
        runtime = SimpleNamespace(
            api_gateway=gateway,
            trading_controller=controller,
        )
        original_error = TimeoutError("secret-bearing detail must not be copied")

        with TemporaryDirectory() as temporary_directory:
            harness = object.__new__(
                BinanceTestnetPhaseThirteenPublicMarketCase2Tests
            )
            harness.runtime = runtime
            harness.configuration = SimpleNamespace(
                api_key="unit-testnet-key-canary-finalizer-31c8dcb0",
                api_secret="unit-testnet-secret-canary-finalizer-5f498c2a",
            )
            harness.run_id = "00000000-0000-4000-8000-000000000031"
            harness.started_at = started_at
            harness.evidence_clock = evidence_clock
            harness.artifact_directory = Path(temporary_directory)
            harness.failure_handling_started = False
            harness.mutation_started = False
            harness.failure_artifact_path = None
            harness._collect_runtime_observations = Mock()

            def capture_successful_recovery(
                selected_guard: Phase13OrderSubmissionGuardSnapshot,
                evidence_errors: list[str],
            ) -> dict[str, object]:
                """
                함수 이름: capture_successful_recovery()
                기능: exact BUY·STOP SELL terminal recovery를 finalizer fixture에 고정한다.
                인자: selected_guard -> finalizer가 전달한 frozen guard
                    evidence_errors -> 비어 있어야 하는 error 목록
                반환값: successful recovery mapping
                작성 날짜: 2026/08/31
                """
                if selected_guard is not guard_snapshot or evidence_errors:
                    raise AssertionError("failure guard identity changed")
                return {
                    "attempted": True,
                    "outcome": "SUCCESS",
                    "typed_reason": None,
                }

            def capture_terminal_runtime(
                evidence_errors: list[str],
            ) -> dict[str, object]:
                """
                함수 이름: capture_terminal_runtime()
                기능: mark 부작용이 있으면 즉시 드러나는 first-runtime terminal truth를 읽는다.
                인자: evidence_errors -> 비어 있어야 하는 error 목록
                반환값: completed recovery runtime state mapping
                작성 날짜: 2026/08/31
                """
                if evidence_errors:
                    raise AssertionError("terminal runtime gained an error")
                return {
                    "application_status": "READY",
                    "trading_status": controller.status.name,
                    "reconciliation_required": (
                        controller.reconciliation_required
                    ),
                    "position_quantity": "0",
                    "pending_order_count": 0,
                    "durable_trade_count": 2,
                }

            def capture_verified_fresh(
                run_client_order_ids: frozenset[str],
                evidence_errors: list[str],
            ) -> dict[str, object]:
                """
                함수 이름: capture_verified_fresh()
                기능: recovered BUY·SELL을 internal ID로만 맞춘 fresh zero-exposure truth를 반환한다.
                인자: run_client_order_ids -> fresh matching에만 쓰는 internal ID 집합
                    evidence_errors -> 비어 있어야 하는 error 목록
                반환값: VERIFIED fresh verification mapping
                작성 날짜: 2026/08/31
                """
                if run_client_order_ids != frozenset(
                    {
                        "bat-p13-client-buy-1",
                        "bat-p13-client-sell-1",
                    }
                ) or evidence_errors:
                    raise AssertionError("fresh internal client IDs changed")
                return {
                    "status": "VERIFIED",
                    "typed_reason": None,
                    "failure_stage": None,
                    "verified_at": _datetime_to_wire(observed_at),
                    "position_quantity": "0",
                    "pending_order_count": 0,
                    "reconciliation_required": False,
                    "matching_open_order_count": 0,
                    "account_open_orders_empty": True,
                    "account_open_order_lists_empty": True,
                    "run_exchange_order_count": 2,
                    "durable_trade_count": 2,
                }

            harness._attempt_known_safe_failure_recovery = Mock(
                side_effect=capture_successful_recovery
            )
            harness._capture_failure_runtime_state = Mock(
                side_effect=capture_terminal_runtime
            )
            harness._capture_fresh_failure_verification = Mock(
                side_effect=capture_verified_fresh
            )

            # close/fresh network 경계는 local typed fakes로 바꾸고 terminal truth 보존 순서만 검증한다.
            with patch(
                f"{__name__}.close_application",
                return_value=SimpleNamespace(status=ApplicationStatus.CLOSED),
            ), patch(
                f"{__name__}._utc_now",
                return_value=observed_at - timedelta(seconds=20),
            ):
                harness._record_actual_failure_evidence(original_error)

            gateway.block_phase13_order_submissions.assert_called_once_with()
            controller.seal_event_runtime_failure_gate.assert_called_once_with()
            controller.mark_event_runtime_failed.assert_not_called()
            self.assertEqual(
                1,
                harness._attempt_known_safe_failure_recovery.call_count,
            )
            self.assertEqual(
                1,
                harness._capture_fresh_failure_verification.call_count,
            )
            self.assertIsInstance(original_error, TimeoutError)
            self.assertTrue(harness.failure_artifact_path.is_file())
            artifact_bytes = harness.failure_artifact_path.read_bytes()
            failure_document = json.loads(
                artifact_bytes,
                object_pairs_hook=_strict_json_object,
                parse_constant=_reject_json_constant,
            )
            self.assertLessEqual(
                failure_document["timestamps"]["started_at"],
                failure_document["timestamps"]["completed_at"],
            )
            wall_clock.assert_called_once_with()
            self.assertNotIn(b"secret-bearing detail", artifact_bytes)
            self.assertNotIn(b"intent-buy-1", artifact_bytes)
            self.assertNotIn(b"intent-sell-1", artifact_bytes)
            self.assertNotIn(b"bat-p13-client-buy-1", artifact_bytes)
            self.assertNotIn(b"bat-p13-client-sell-1", artifact_bytes)
            self.assertIn(b'"outcome":"FAILED"', artifact_bytes)
            self.assertIn(
                b'"recovery":{"attempted":true,"outcome":"SUCCESS",'
                b'"typed_reason":null}',
                artifact_bytes,
            )
            self.assertTrue(
                any("sealed FAILED evidence" in note for note in original_error.__notes__)
            )

    def test_failure_finalizer_seals_cause_mismatch_before_its_own_mutation(
        self,
    ) -> None:
        """
        함수 이름: test_failure_finalizer_seals_cause_mismatch_before_its_own_mutation()
        기능: exception frozen cause와 finalizer 진입 snapshot의 차이를 mark 전 CONFLICT/null로 고정한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        observed_at = _utc_now()
        guard_snapshot = Phase13OrderSubmissionGuardSnapshot(
            mutation_started=False,
            submissions_blocked=True,
            attempts=(),
        )
        frozen_snapshot = ReconciliationCauseSnapshot(
            reconciliation_required=True,
            status=ReconciliationCauseStatus.EXACT,
            category=(
                ReconciliationCauseCategory.ACCOUNT_STREAM_UNKNOWN_OR_EXTERNAL_EXECUTION
            ),
        )
        current_snapshot = ReconciliationCauseSnapshot(
            reconciliation_required=True,
            status=ReconciliationCauseStatus.EXACT,
            category=ReconciliationCauseCategory.EVENT_WORKER_OR_RUNTIME_FAILED,
        )
        gateway = Mock()
        gateway.get_phase13_order_submission_guard_snapshot.return_value = (
            guard_snapshot
        )
        controller = Mock()
        controller.status = TradingSessionStatus.RUNNING
        controller.reconciliation_cause_snapshot = current_snapshot

        # mark side effect와 뒤 snapshot은 frozen과 같게 만들어 대조 시점이 뒤면 테스트가 반드시 실패한다.
        def mutate_active_scheduler_gate() -> None:
            """
            함수 이름: mutate_active_scheduler_gate()
            기능: active mark가 gate를 먼저 닫고 Context publication에서 실패하는 production 순서를 모사한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/08/31
            """
            controller.status = TradingSessionStatus.RECONCILIATION_REQUIRED
            controller.reconciliation_cause_snapshot = frozen_snapshot
            raise RuntimeError("controlled-finalizer-publication-failure")

        controller.seal_event_runtime_failure_gate.side_effect = (
            mutate_active_scheduler_gate
        )
        runtime = SimpleNamespace(
            api_gateway=gateway,
            trading_controller=controller,
        )
        original_error = _PhaseThirteenReconciliationFailure(frozen_snapshot)

        with TemporaryDirectory() as temporary_directory:
            harness = object.__new__(
                BinanceTestnetPhaseThirteenPublicMarketCase2Tests
            )
            harness.runtime = runtime
            harness.configuration = SimpleNamespace(
                api_key="cause-toctou-key-canary-31c8dcb0",
                api_secret="cause-toctou-secret-canary-5f498c2a",
            )
            harness.run_id = "00000000-0000-4000-8000-000000000032"
            harness.started_at = observed_at
            harness.evidence_clock = lambda: observed_at
            harness.artifact_directory = Path(temporary_directory)
            harness.failure_handling_started = False
            harness.mutation_started = False
            harness.failure_artifact_path = None
            harness._collect_runtime_observations = Mock()

            def capture_conflict_recovery(
                selected_guard: Phase13OrderSubmissionGuardSnapshot,
                evidence_errors: list[str],
            ) -> dict[str, object]:
                """
                함수 이름: capture_conflict_recovery()
                기능: cause TOCTOU fixture의 recovery reason과 error code를 exact 결속한다.
                인자: selected_guard -> finalizer가 전달한 frozen guard
                    evidence_errors -> producer가 갱신할 error 목록
                반환값: ambiguous recovery mapping
                작성 날짜: 2026/08/31
                """
                if selected_guard is not guard_snapshot:
                    raise AssertionError("conflict guard identity changed")
                evidence_errors.append("FAILURE_RECOVERY_STATE_AMBIGUOUS")
                return {
                    "attempted": False,
                    "outcome": "SKIPPED",
                    "typed_reason": "FAILURE_RECOVERY_STATE_AMBIGUOUS",
                }

            def capture_conflict_runtime(
                evidence_errors: list[str],
            ) -> dict[str, object]:
                """
                함수 이름: capture_conflict_runtime()
                기능: cause TOCTOU fixture의 None position과 unavailable code를 결속한다.
                인자: evidence_errors -> producer가 갱신할 error 목록
                반환값: partial runtime state mapping
                작성 날짜: 2026/08/31
                """
                evidence_errors.append("RUNTIME_POSITION_UNAVAILABLE")
                return {
                    "application_status": "READY",
                    "trading_status": "RECONCILIATION_REQUIRED",
                    "reconciliation_required": True,
                    "position_quantity": None,
                    "pending_order_count": 0,
                    "durable_trade_count": 0,
                }

            def capture_conflict_fresh(
                _run_client_order_ids: frozenset[str],
                evidence_errors: list[str],
            ) -> dict[str, object]:
                """
                함수 이름: capture_conflict_fresh()
                기능: cause TOCTOU fixture의 fresh primary reason과 error code를 결속한다.
                인자: _run_client_order_ids -> 빈 internal ID 집합
                    evidence_errors -> producer가 갱신할 error 목록
                반환값: incomplete fresh verification mapping
                작성 날짜: 2026/08/31
                """
                evidence_errors.append("FRESH_VERIFICATION_FAILED")
                fresh_mapping = _create_incomplete_fresh_verification()
                fresh_mapping["failure_stage"] = "STARTUP_APPLICATION"
                return fresh_mapping

            harness._attempt_known_safe_failure_recovery = Mock(
                side_effect=capture_conflict_recovery
            )
            harness._capture_failure_runtime_state = Mock(
                side_effect=capture_conflict_runtime
            )
            harness._capture_fresh_failure_verification = Mock(
                side_effect=capture_conflict_fresh
            )

            with patch(
                f"{__name__}.close_application",
                return_value=SimpleNamespace(status=ApplicationStatus.CLOSED),
            ):
                harness._record_actual_failure_evidence(original_error)

            failure_document = json.loads(
                harness.failure_artifact_path.read_bytes(),
                object_pairs_hook=_strict_json_object,
                parse_constant=_reject_json_constant,
            )
            self.assertEqual(
                {
                    "reconciliation_required": True,
                    "status": ReconciliationCauseStatus.CONFLICT.value,
                    "category": None,
                },
                failure_document["first_cause"],
            )
            self.assertIn(
                "RECONCILIATION_CAUSE_CONFLICT",
                failure_document["evidence_errors"],
            )
            self.assertIn(
                "FAILURE_SCHEDULER_GATE_PUBLICATION_FAILED",
                failure_document["evidence_errors"],
            )
            controller.seal_event_runtime_failure_gate.assert_called_once_with()
            controller.mark_event_runtime_failed.assert_not_called()
            self.assertIs(
                frozen_snapshot,
                controller.reconciliation_cause_snapshot,
            )  # Mark 후 snapshot은 exact로 돌아왔어도 sealed first cause를 역으로 바꾸지 못한다.

    def test_failure_recovery_submits_one_stop_only_for_exact_durable_buy(
        self,
    ) -> None:
        """
        함수 이름: test_failure_recovery_submits_one_stop_only_for_exact_durable_buy()
        기능: known-safe 단일 BUY failure가 public STOP 한 번으로 zero exposure에 도달하는지 무네트워크 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        observed_at = _utc_now()
        buy_attempt = Phase13OrderSubmissionAttempt(
            symbol="ETHUSDT",
            side=OrderSide.BUY,
            order_type="MARKET",
            intent_id="intent-buy",
            submission_attempt=0,
            attempted_at=observed_at,
            client_order_id="bat-p13-client-buy",
        )
        sell_attempt = Phase13OrderSubmissionAttempt(
            symbol="ETHUSDT",
            side=OrderSide.SELL,
            order_type="MARKET",
            intent_id="intent-sell",
            submission_attempt=0,
            attempted_at=observed_at,
            client_order_id="bat-p13-client-sell",
        )
        initial_guard = Phase13OrderSubmissionGuardSnapshot(
            mutation_started=True,
            submissions_blocked=False,
            attempts=(buy_attempt,),
        )
        final_guard = Phase13OrderSubmissionGuardSnapshot(
            mutation_started=True,
            submissions_blocked=True,
            attempts=(buy_attempt, sell_attempt),
        )
        position = SimpleNamespace(quantity=Decimal("0.25"))
        buy_trade = SimpleNamespace(
            symbol="ETHUSDT",
            side=OrderSide.BUY,
            strategy=StrategyType.CASE_C,
            regime_type=RegimeType.TYPE_0,
            exit_reason=None,
            client_order_id=buy_attempt.client_order_id,
            executed_quantity=Decimal("0.25"),
        )
        sell_trade = SimpleNamespace(
            symbol="ETHUSDT",
            side=OrderSide.SELL,
            strategy=StrategyType.CASE_C,
            regime_type=RegimeType.TYPE_0,
            exit_reason=ExitReason.STOP,
            client_order_id=sell_attempt.client_order_id,
            executed_quantity=Decimal("0.25"),
        )
        stop_trading = Mock()
        controller = SimpleNamespace(
            position=position,
            status=TradingSessionStatus.RUNNING,
            reconciliation_required=False,
            context=SimpleNamespace(version=7),
            stop_trading=stop_trading,
        )
        trade_history = SimpleNamespace(trades=[buy_trade])
        gateway = Mock()
        gateway.get_phase13_order_submission_guard_snapshot.side_effect = (
            initial_guard,
            final_guard,
        )
        runtime = SimpleNamespace(
            state=SimpleNamespace(status=ApplicationStatus.READY),
            api_gateway=gateway,
            trading_controller=controller,
            trade_history=trade_history,
            trade_history_controller=SimpleNamespace(
                get_pending_orders=Mock(return_value=())
            ),
        )
        harness = object.__new__(
            BinanceTestnetPhaseThirteenPublicMarketCase2Tests
        )
        harness.runtime = runtime
        harness.baseline_trades = ()
        harness.run_id = "00000000-0000-4000-8000-000000000031"
        harness._wait_for_effective_free_eth = Mock(
            return_value=Decimal("0.25")
        )
        harness._collect_runtime_observations = Mock()

        def _complete_stop_recovery() -> None:
            """
            함수 이름: _complete_stop_recovery()
            기능: production worker 완료를 모사해 SELL durable truth와 zero Position을 동시에 공개한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/08/31
            """
            controller.status = TradingSessionStatus.TERMINATED
            position.quantity = Decimal("0")
            trade_history.trades.append(sell_trade)

        stop_trading.side_effect = lambda **_: SimpleNamespace(
            status=TradingSessionStatus.STOPPING
        )
        harness._wait_for_session_termination = Mock(
            side_effect=_complete_stop_recovery
        )
        evidence_errors: list[str] = []

        # Fake는 실제 REST delegate가 없으며 exact state만 public Controller recovery 경로로 통과시킨다.
        recovery = harness._attempt_known_safe_failure_recovery(
            initial_guard,
            evidence_errors,
        )

        self.assertEqual(
            {"attempted": True, "outcome": "SUCCESS", "typed_reason": None},
            recovery,
        )
        self.assertEqual([], evidence_errors)
        stop_trading.assert_called_once_with(
            command_id=(
                "phase13-failure-recovery-"
                "00000000-0000-4000-8000-000000000031"
            ),
            expected_version=7,
        )
        harness._wait_for_session_termination.assert_called_once_with()

        mismatched_sell_trade = SimpleNamespace(
            symbol="ETHUSDT",
            side=OrderSide.SELL,
            strategy=StrategyType.CASE_C,
            regime_type=RegimeType.TYPE_0,
            exit_reason=ExitReason.STOP,
            client_order_id="bat-p13-unrelated-sell",
            executed_quantity=Decimal("0.25"),
        )
        position.quantity = Decimal("0.25")
        controller.status = TradingSessionStatus.RUNNING
        trade_history.trades[:] = [buy_trade]
        gateway.get_phase13_order_submission_guard_snapshot.side_effect = (
            initial_guard,
            final_guard,
        )
        stop_trading.reset_mock()

        def _complete_mismatched_stop_recovery() -> None:
            """
            함수 이름: _complete_mismatched_stop_recovery()
            기능: guard와 다른 client ID의 forged durable SELL 완료를 테스트 state에 공개한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/08/31
            """
            controller.status = TradingSessionStatus.TERMINATED
            position.quantity = Decimal("0")
            trade_history.trades.append(mismatched_sell_trade)

        harness._wait_for_session_termination = Mock(
            side_effect=_complete_mismatched_stop_recovery
        )
        mismatched_errors: list[str] = []

        # 동일 side·수량이라도 permission attempt와 durable SELL client ID가 다르면 SUCCESS가 아니다.
        mismatched_recovery = harness._attempt_known_safe_failure_recovery(
            initial_guard,
            mismatched_errors,
        )

        self.assertEqual(
            {
                "attempted": True,
                "outcome": "FAILED",
                "typed_reason": "FAILURE_RECOVERY_ASSERTION_FAILED",
            },
            mismatched_recovery,
        )
        self.assertEqual(
            ["FAILURE_RECOVERY_ASSERTION_FAILED"],
            mismatched_errors,
        )
        stop_trading.assert_called_once()

    def test_failure_recovery_skips_ambiguous_buy_without_stop(self) -> None:
        """
        함수 이름: test_failure_recovery_skips_ambiguous_buy_without_stop()
        기능: pending·UNKNOWN이 있는 BUY failure에서는 STOP·SELL 호출을 하나도 만들지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        observed_at = _utc_now()
        buy_attempt = Phase13OrderSubmissionAttempt(
            symbol="ETHUSDT",
            side=OrderSide.BUY,
            order_type="MARKET",
            intent_id="intent-buy",
            submission_attempt=0,
            attempted_at=observed_at,
            client_order_id="bat-p13-client-buy",
        )
        initial_guard = Phase13OrderSubmissionGuardSnapshot(
            mutation_started=True,
            submissions_blocked=False,
            attempts=(buy_attempt,),
        )
        stop_trading = Mock()
        controller = SimpleNamespace(
            position=SimpleNamespace(quantity=Decimal("0.25")),
            status=TradingSessionStatus.RECONCILIATION_REQUIRED,
            reconciliation_required=True,
            context=SimpleNamespace(version=7),
            stop_trading=stop_trading,
        )
        runtime = SimpleNamespace(
            state=SimpleNamespace(status=ApplicationStatus.READY),
            api_gateway=Mock(),
            trading_controller=controller,
            trade_history=SimpleNamespace(
                trades=[
                    SimpleNamespace(
                        symbol="ETHUSDT",
                        side=OrderSide.BUY,
                        strategy=StrategyType.CASE_C,
                        regime_type=RegimeType.TYPE_0,
                        exit_reason=None,
                        client_order_id=buy_attempt.client_order_id,
                        executed_quantity=Decimal("0.25"),
                    )
                ]
            ),
            trade_history_controller=SimpleNamespace(
                get_pending_orders=Mock(return_value=(object(),))
            ),
        )
        harness = object.__new__(
            BinanceTestnetPhaseThirteenPublicMarketCase2Tests
        )
        harness.runtime = runtime
        harness.baseline_trades = ()
        harness._wait_for_effective_free_eth = Mock()
        harness._wait_for_session_termination = Mock()
        evidence_errors: list[str] = []

        recovery = harness._attempt_known_safe_failure_recovery(
            initial_guard,
            evidence_errors,
        )

        self.assertEqual(
            {
                "attempted": False,
                "outcome": "SKIPPED",
                "typed_reason": "FAILURE_RECOVERY_STATE_AMBIGUOUS",
            },
            recovery,
        )
        self.assertEqual(
            ["FAILURE_RECOVERY_STATE_AMBIGUOUS"],
            evidence_errors,
        )  # Skip reason은 recovery mapping과 evidence_errors에 동일하게 결속된다.
        stop_trading.assert_not_called()
        harness._wait_for_effective_free_eth.assert_not_called()
        harness._wait_for_session_termination.assert_not_called()

    def test_module_suite_runs_helper_regression_before_actual_mutation(
        self,
    ) -> None:
        """
        함수 이름: test_module_suite_runs_helper_regression_before_actual_mutation()
        기능: Standalone actual module이 모든 network-free helper 뒤에 mutation class를 배치하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        ordered_suite = load_tests(
            unittest.TestLoader(),
            unittest.TestSuite(),
            None,
        )
        suite_groups = tuple(ordered_suite)

        # 첫 group은 helper만, 마지막 group은 exact actual class만 포함해야 순서가 이름 정렬과 무관하다.
        self.assertEqual(2, len(suite_groups))
        self.assertTrue(
            all(
                isinstance(test_case, PhaseThirteenPublicHarnessHelperTests)
                for test_case in suite_groups[0]
            )
        )
        self.assertTrue(
            all(
                isinstance(
                    test_case,
                    BinanceTestnetPhaseThirteenPublicMarketCase2Tests,
                )
                for test_case in suite_groups[1]
            )
        )  # Actual class가 network-free helper group 뒤에만 오는 순서를 고정한다.

@unittest.skipUnless(
    PHASE13_PUBLIC_CASE2_REQUESTED,
    PHASE13_PUBLIC_CASE2_SKIP_REASON,
)
@unittest.skipUnless(os.name == "posix", "historical evidence harness requires POSIX filesystem primitives")
class BinanceTestnetPhaseThirteenPublicMarketCase2Tests(unittest.TestCase):
    """
    클래스 이름: BinanceTestnetPhaseThirteenPublicMarketCase2Tests
    기능: 세 opt-in에서 결정론적 public Case C BUY와 same-run STOP recovery를 실제 Testnet에서 검증한다.
    작성 날짜: 2026/08/31

    주의: 이 class는 private Action, threshold patch 또는 성공 상태 주입을 전혀 사용하지 않는다.
    실제 주문은 이 module 하나를 세 opt-in과 10 USDT 이하 cap으로 직접 지정할 때만 실행한다.
    """

    def setUp(self) -> None:
        """
        함수 이름: setUp()
        기능: 세 번째 exact opt-in과 cap을 검증하고 격리된 runtime·artifact 수집기를 조립한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        # Permission 확인이 artifact나 client 생성보다 먼저 실행되어 broad discovery의 mutation을 막는다.
        self.configuration = load_testnet_configuration()
        self.maximum_notional = require_phase13_public_case2_permission(
            self.configuration
        )

        # 고정 lease를 runtime/client 조립보다 먼저 잡아 다른 process의 동일 actual target을 network 전에 막는다.
        _ARTIFACT_ROOT.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.process_lease_descriptor = _acquire_phase13_process_lease(
            _PHASE13_PROCESS_LEASE_PATH
        )
        self.addCleanup(
            _release_phase13_process_lease,
            self.process_lease_descriptor,
        )  # Cleanup은 tearDown 뒤 실행되어 recovery와 fresh verification 전체 동안 lease를 유지한다.
        self.run_id = str(uuid4())
        self.evidence_clock = _RunScopedEvidenceClock()
        self.started_at = self.evidence_clock()
        run_timestamp = self.started_at.strftime("%Y%m%dT%H%M%S%fZ")
        self.artifact_directory = _ARTIFACT_ROOT / (
            f"phase13-public-case2-{run_timestamp}-{self.run_id.replace('-', '')}"
        )
        self.artifact_directory.mkdir(parents=True, mode=0o700)
        os.chmod(self.artifact_directory, 0o700)  # 기존 umask와 무관하게 credential-free 증거도 owner 전용으로 둔다.
        self.history_path = self.artifact_directory / "history.jsonl"
        self.baseline_trades = seed_verified_closed_history(self.history_path)
        baseline_bytes = (
            self.history_path.read_bytes()
            if self.history_path.exists()
            else b""
        )
        self.baseline_history_sha256 = hashlib.sha256(
            baseline_bytes
        ).hexdigest()
        self.testnet_environment = _create_actual_testnet_environment(
            self.configuration,
            self.maximum_notional,
        )
        self.event_stream = BackendEventStream(clock=self.evidence_clock)
        self.runtime = create_testnet_application_runtime(
            create_account_update_observer(self.event_stream),
            create_trade_history_update_observer(self.event_stream),
            create_trading_session_update_observer(self.event_stream),
            history_path=self.history_path,
            environment=self.testnet_environment,
            risk_policy_state=_create_actual_risk_policy(self.maximum_notional),
            clock=self.evidence_clock,
        )

        # 모든 buffer는 normalized domain/DTO 값만 보존하며 environment나 client 객체를 절대 추가하지 않는다.
        self.transport_event_dtos: list[dict[str, object]] = []
        self.public_market_events: list[dict[str, object]] = []
        self.public_account_events: list[dict[str, object]] = []
        self.after_transport_sequence = self.event_stream.last_sequence
        self.last_market_version = self.runtime.market_snapshot.version
        self.preflight: dict[str, object] | None = None
        self.baseline_recent_exchange_order_ids: frozenset[str] | None = None
        self.failure_handling_started = False
        self.mutation_started = False
        self.failure_artifact_path: Path | None = None

    def tearDown(self) -> None:
        """
        함수 이름: tearDown()
        기능: assertion 실패 시에도 새 주문 없이 public STOP을 우선하고 모든 runtime/stream 자원을 닫는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        cleanup_error: BaseException | None = None
        try:
            if (
                self.runtime.state.status is not ApplicationStatus.CLOSED
                and not self.failure_handling_started
            ):
                self._stop_current_run_for_safety()
        except BaseException as error:
            cleanup_error = error  # 아래 close를 건너뛰지 않고 durable recovery artifact를 그대로 보존한다.
        finally:
            try:
                if self.runtime.state.status is not ApplicationStatus.CLOSED:
                    close_application(self.runtime)
            finally:
                self.event_stream.close()  # Runtime observer publication이 끝난 다음 stream waiter를 닫는다.
        if cleanup_error is not None:
            if not self.failure_handling_started:
                try:
                    self._record_actual_failure_evidence(cleanup_error)
                except Exception as evidence_error:
                    cleanup_error.add_note(
                        "Phase 13 cleanup FAILED evidence finalization failed "
                        f"with typed class {type(evidence_error).__name__}"
                    )
            cleanup_error.add_note(
                "Phase 13 recovery artifacts preserved at "
                f"{self.artifact_directory}"
            )
            raise cleanup_error

    def _collect_runtime_observations(self, timeout: float = 0.0) -> None:
        """
        함수 이름: _collect_runtime_observations()
        기능: public transport DTO, account patch와 source Kline을 한 번 수집한다.
        인자: timeout -> 새 backend event를 기다릴 최대 초
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TypeError("timeout must be a number")
        if timeout < 0:
            raise ValueError("timeout must not be negative")

        # EventStream의 public replay API만 사용하고 gap은 실제 UI evidence 상실로 즉시 중단한다.
        replay_batch = self.event_stream.wait_for_events(
            self.after_transport_sequence,
            timeout=timeout,
        )
        if replay_batch.requires_resync:
            raise AssertionError("backend event trace requires resynchronization")
        new_event_dtos: list[dict[str, object]] = []
        for event_envelope in replay_batch.events:
            event_dto = event_envelope.to_dto()
            new_event_dtos.append(event_dto)
            self.transport_event_dtos.append(event_dto)
            self.after_transport_sequence = event_envelope.sequence
        _append_account_events_from_transport(
            self.public_account_events,
            new_event_dtos,
        )

        # 불변 state에서 version과 source event를 같이 읽어 별도 current-version 조회와의 혼합을 막는다.
        captured_market_version, market_events = _create_observed_market_event(
            self.runtime,
            sequence=len(self.public_market_events) + 1,
            after_market_version=self.last_market_version,
        )
        if captured_market_version > self.last_market_version:
            if not market_events:
                raise AssertionError(
                    "market cursor advanced without a production boundary"
                )
            self.public_market_events.extend(market_events)
            if len(self.public_market_events) > _MAXIMUM_RECORDED_MARKET_EVENTS:
                self.public_market_events = self.public_market_events[
                    -_MAXIMUM_RECORDED_MARKET_EVENTS:
                ]
            for sequence, retained_event in enumerate(
                self.public_market_events,
                start=1,
            ):
                retained_event["sequence"] = sequence
            self.last_market_version = captured_market_version

    def _read_phase13_submission_guard(
        self,
    ) -> tuple[Phase13OrderSubmissionGuardSnapshot, list[dict[str, object]]]:
        """
        함수 이름: _read_phase13_submission_guard()
        기능: permission REST 경계의 frozen snapshot과 ID 없는 current v2 attempt evidence를 분리한다.
        인자: 없음
        반환값: 원 frozen snapshot과 contiguous normalized attempt 목록 tuple
        작성 날짜: 2026/08/31
        """
        snapshot = (
            self.runtime.api_gateway.get_phase13_order_submission_guard_snapshot()
        )
        if type(snapshot) is not Phase13OrderSubmissionGuardSnapshot:
            raise TypeError("Phase 13 gateway returned an invalid guard snapshot")

        # Recovery는 frozen snapshot의 ID를 내부에서만 쓰고 artifact mapping은 identity field를 절대 복사하지 않는다.
        normalized_attempts = [
            {
                "sequence": sequence,
                "symbol": attempt.symbol,
                "side": attempt.side.value,
                "order_type": attempt.order_type,
                "submission_attempt": attempt.submission_attempt,
                "attempted_at": _datetime_to_wire(attempt.attempted_at),
            }
            for sequence, attempt in enumerate(snapshot.attempts, start=1)
        ]

        return snapshot, normalized_attempts

    def _observe_phase13_mutation_boundary(self) -> None:
        """
        함수 이름: _observe_phase13_mutation_boundary()
        기능: polling cycle마다 첫 REST submit 시작과 production order trace 실패를 즉시 감시한다.
        인자: 없음
        반환값: 정상 guard이면 없음
        작성 날짜: 2026/08/31
        """
        snapshot, _ = self._read_phase13_submission_guard()
        self.mutation_started = self.mutation_started or snapshot.mutation_started
        if snapshot.submissions_blocked and len(snapshot.attempts) < 2:
            raise AssertionError(
                "Phase 13 submission guard closed before its expected order sequence"
            )

        # Durable Trade 전 pipeline 실패도 다음 polling tick에서 발견해 retry scheduler가 새 ID를 만들 시간을 주지 않는다.
        if snapshot.mutation_started and any(
            trace_entry.result.value == "FAILURE"
            for trace_entry in self.runtime.trading_controller.order_execution_trace
        ):
            raise AssertionError("Phase 13 logical submission entered a failed order trace")

    def _inject_deterministic_public_case2_and_wait_for_buy(
        self,
        deterministic_klines: DeterministicPublicCase2Klines,
    ) -> Trade | None:
        """
        함수 이름: _inject_deterministic_public_case2_and_wait_for_buy()
        기능: 세 fixture Kline을 public market 경계에만 넣고 production BUY publication을 기다린다.
        인자: deterministic_klines -> current snapshot에서 계산한 SETUP, FLUSH와 RECOVERY 입력
        반환값: production strategy가 만든 run 첫 BUY Trade 또는 bounded timeout의 None
        작성 날짜: 2026/09/04
        """
        if not isinstance(
            deterministic_klines,
            DeterministicPublicCase2Klines,
        ):
            raise TypeError(
                "deterministic_klines must be DeterministicPublicCase2Klines"
            )

        # Kline delivery guard가 live tick을 막으므로 각 stage commit 뒤 다음 occurred_at을 생성한다.
        ordered_klines = deterministic_klines.as_tuple()
        for kline in ordered_klines[:-1]:
            self.runtime.market_data_controller.observe_kline(kline)
            self._wait_for_public_market_evaluation(kline)

        recovery_kline = ordered_klines[-1]
        self.runtime.market_data_controller.observe_kline(recovery_kline)

        buy_trade = self._wait_for_public_buy()
        if buy_trade is None:
            return None

        # Exact 1L.3 source를 recovery event에 결속해 SETUP·FLUSH 조기 BUY와 자연 tick 대체를 차단한다.
        injected_source_ids = tuple(
            _kline_source_component(kline)
            for kline in ordered_klines
        )
        injected_action_boundaries = tuple(
            trace_entry
            for trace_entry in (
                self.runtime.trading_controller.public_market_boundary_trace
            )
            if (
                trace_entry.message_id == "1L.3"
                and trace_entry.source_event_id in injected_source_ids
            )
        )
        if (
            len(injected_action_boundaries) != 1
            or injected_action_boundaries[0].source_event_id
            != injected_source_ids[-1]
        ):
            raise AssertionError(
                "deterministic BUY must originate from the recovery Kline"
            )

        return buy_trade

    def _wait_for_public_market_evaluation(self, expected_kline: Kline) -> None:
        """
        함수 이름: _wait_for_public_market_evaluation()
        기능: 이전 fixture Kline의 production Context commit을 기다려 다음 event 시각 역전을 막는다.
        인자: expected_kline -> 이번 stage에서 전달한 immutable public Kline
        반환값: exact price와 low가 Context에 commit되면 없음
        작성 날짜: 2026/09/04
        """
        if not isinstance(expected_kline, Kline):
            raise TypeError("expected_kline must be a Kline")
        deadline = time.monotonic() + _DETERMINISTIC_EVALUATION_TIMEOUT_SECONDS

        # Background worker publication을 기다리며 직접 drain하거나 runtime clock을 보정하지 않는다.
        while time.monotonic() < deadline:
            remaining_seconds = max(0.0, deadline - time.monotonic())
            self._collect_runtime_observations(
                timeout=min(_POLL_INTERVAL_SECONDS, remaining_seconds)
            )
            self._observe_phase13_mutation_boundary()
            controller = self.runtime.trading_controller
            market = controller.context.market
            if (
                market.realtime_price == expected_kline.close
                and market.current_30m_low == expected_kline.low
            ):
                return

            cause_snapshot = controller.reconciliation_cause_snapshot
            if cause_snapshot.reconciliation_required:
                worker = self.runtime._trading_event_runtime_worker
                worker_failure_snapshot = (
                    None if worker is None else worker.failure_snapshot
                )
                market_failure_snapshot = (
                    _read_phase13_market_failure_snapshot(self.runtime)
                    if cause_snapshot.category
                    is ReconciliationCauseCategory.MARKET_STREAM_FAILED
                    else None
                )
                raise _PhaseThirteenReconciliationFailure(
                    cause_snapshot,
                    worker_failure_snapshot=worker_failure_snapshot,
                    market_failure_snapshot=market_failure_snapshot,
                )  # Stage commit 전 실패도 같은 secret-free 최초 원인으로 즉시 중단한다.

        raise TimeoutError(
            "production worker did not commit deterministic public evaluation"
        )

    def _wait_for_public_buy(self) -> Trade | None:
        """
        함수 이름: _wait_for_public_buy()
        기능: 결정론적 recovery 평가가 만든 최초 durable BUY를 기다리고 timeout이면 None을 반환한다.
        인자: 없음
        반환값: run 첫 BUY Trade 또는 bounded timeout의 None
        작성 날짜: 2026/09/04
        """
        deadline = time.monotonic() + _DETERMINISTIC_BUY_TIMEOUT_SECONDS

        # Production background worker가 recovery queue를 drain하며 test는 state를 관찰할 뿐 직접 실행하지 않는다.
        while time.monotonic() < deadline:
            remaining_seconds = max(0.0, deadline - time.monotonic())
            self._collect_runtime_observations(
                timeout=min(_POLL_INTERVAL_SECONDS, remaining_seconds)
            )
            self._observe_phase13_mutation_boundary()
            run_trades = self.runtime.trade_history.trades[len(self.baseline_trades) :]
            buy_trades = tuple(
                trade for trade in run_trades if trade.side is OrderSide.BUY
            )
            if buy_trades:
                if len(buy_trades) != 1 or any(
                    trade.side is OrderSide.SELL for trade in run_trades
                ):
                    raise AssertionError(
                        "public Case 2 must expose one BUY before any autonomous SELL"
                    )
                return buy_trades[0]
            cause_snapshot = (
                self.runtime.trading_controller.reconciliation_cause_snapshot
            )
            if cause_snapshot.reconciliation_required:
                order_failure_code = next(
                    (
                        trace_entry.failure_code
                        for trace_entry in reversed(
                            self.runtime.trading_controller.order_execution_trace
                        )
                        if trace_entry.failure_code is not None
                    ),
                    None,
                )
                worker = self.runtime._trading_event_runtime_worker
                worker_failure_snapshot = (
                    None if worker is None else worker.failure_snapshot
                )  # Reconciliation publication 전에 고정된 immutable worker 진단만 읽는다.
                raise _PhaseThirteenReconciliationFailure(
                    cause_snapshot,
                    order_failure_code,
                    worker_failure_snapshot,
                )  # 한 lock snapshot을 예외에 고정해 finalizer 자체 fail-close가 원인을 바꾸지 못한다.

        return None  # Timeout은 private trigger로 우회하지 않고 실제 주문 상태를 별도 증거화한다.

    def _wait_for_effective_free_eth(self, position_quantity: Decimal) -> Decimal:
        """
        함수 이름: _wait_for_effective_free_eth()
        기능: public account stream에서 recovery Position 이상인 실제 free ETH absolute balance를 기다린다.
        인자: position_quantity -> same-run BUY가 연 authoritative Position 수량
        반환값: 관찰 free와 Position의 교집인 same-run effective free ETH 수량
        작성 날짜: 2026/08/31
        """
        if not isinstance(position_quantity, Decimal) or position_quantity <= Decimal("0"):
            raise ValueError("position_quantity must be a positive Decimal")
        deadline = time.monotonic() + _ACCOUNT_SETTLEMENT_TIMEOUT_SECONDS

        # Pre-existing ETH를 매도 대상으로 삼지 않고 Position 이하 SELL gate에 필요한 free 하한만 확인한다.
        while time.monotonic() < deadline:
            self._collect_runtime_observations(timeout=_POLL_INTERVAL_SECONDS)
            self._observe_phase13_mutation_boundary()
            eth_balance = self.runtime.account.balances.get("ETH")
            observed_free = Decimal("0") if eth_balance is None else eth_balance.free
            if self.runtime.web_socket_gateway.account_ready and observed_free >= position_quantity:
                return position_quantity  # Pre-existing ETH는 배제하고 same-run authoritative exposure만 effective free로 고정한다.

        raise AssertionError(
            "public account stream did not prove enough free ETH for exact recovery"
        )

    def _wait_for_session_termination(self) -> None:
        """
        함수 이름: _wait_for_session_termination()
        기능: production worker의 same-ID query와 recovery outcome으로 TERMINATED가 될 때까지 기다린다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        deadline = time.monotonic() + _ORDER_SETTLEMENT_TIMEOUT_SECONDS

        # Test가 Controller drain/reconcile 함수를 직접 부르지 않아 actual evidence가 production worker를 통과한다.
        while time.monotonic() < deadline:
            self._collect_runtime_observations(timeout=_POLL_INTERVAL_SECONDS)
            self._observe_phase13_mutation_boundary()
            controller = self.runtime.trading_controller
            if controller.status is TradingSessionStatus.TERMINATED:
                return
            if controller.reconciliation_required:
                raise AssertionError("recovery order requires manual reconciliation")

        raise TimeoutError("public STOP recovery did not terminate within the bound")

    def _stop_current_run_for_safety(self) -> None:
        """
        함수 이름: _stop_current_run_for_safety()
        기능: current runtime이 안전하게 중지 가능한 경우 public stop 한 번만 요청하고 terminal을 기다린다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        controller = self.runtime.trading_controller
        if controller.status in (
            TradingSessionStatus.NOT_STARTED,
            TradingSessionStatus.TERMINATED,
        ):
            return
        if controller.reconciliation_required:
            return  # UNKNOWN 위에 새 recovery ID를 만들지 않고 journal을 다음 fresh reconciliation에 남긴다.
        if controller.status is TradingSessionStatus.RUNNING:
            controller.stop_trading(
                command_id=f"phase13-safety-stop-{self.run_id}",
                expected_version=controller.context.version,
            )
        self._wait_for_session_termination()  # STOPPING에는 같은 command를 중복 제출하지 않는다.

    def _fresh_read_only_verification(
        self,
        run_client_order_ids: frozenset[str],
    ) -> tuple[
        tuple[Trade, ...],
        Performance,
        tuple[OrderResult, ...],
        str,
        datetime,
    ]:
        """
        함수 이름: _fresh_read_only_verification()
        기능: 주문 권한 없는 새 runtime에서 Position/pending/open과 durable exchange 대응을 재검증한다.
        인자: run_client_order_ids -> 이번 run의 append-only UPSERT client identity 집합
        반환값: fresh Trade, Performance, matching result, session UUID와 검증 시각 tuple
        작성 날짜: 2026/08/31
        """
        read_only_environment = _create_read_only_testnet_environment(
            self.configuration
        )
        fresh_event_stream = BackendEventStream(clock=self.evidence_clock)
        fresh_runtime = create_testnet_application_runtime(
            create_account_update_observer(fresh_event_stream),
            create_trade_history_update_observer(fresh_event_stream),
            create_trading_session_update_observer(fresh_event_stream),
            history_path=self.history_path,
            environment=read_only_environment,
            clock=self.evidence_clock,
        )
        try:
            ready_state = start_application(fresh_runtime)
            self.assertIs(ready_state.status, ApplicationStatus.READY)
            self.assertTrue(fresh_runtime.market_snapshot.ready)
            self.assertTrue(fresh_runtime.account.ready)
            self.assertTrue(fresh_runtime.web_socket_gateway.kline_live_ready)
            self.assertTrue(fresh_runtime.web_socket_gateway.account_ready)
            position = fresh_runtime.trading_controller.position
            self.assertIsNotNone(position)
            _require_zero_position_quantity(position.quantity)
            if fresh_runtime.trade_history_controller.get_pending_orders():
                raise AssertionError(
                    "fresh verification requires zero pending orders"
                )
            self.assertFalse(
                fresh_runtime.trading_controller.reconciliation_required
            )

            # Fresh read-only restart에서는 application·manual client ID 전체 open order가 0이어야 한다.
            open_results = tuple(
                fresh_runtime.api_gateway.list_all_open_order_results(
                    "ETHUSDT"
                )
            )
            require_empty_all_client_open_orders(open_results)
            recent_results = tuple(
                fresh_runtime.api_gateway.list_all_recent_order_results(
                    "ETHUSDT",
                    limit=1000,
                )
            )
            if self.baseline_recent_exchange_order_ids is None:
                raise AssertionError("exchange recent-order baseline was not captured")

            # Fresh durable Trade 전체와 all-client recent identity가 같아야 manual delta를 숨기지 않는다.
            fresh_trades = fresh_runtime.trade_history.trades
            verify_exact_recent_order_baseline(
                fresh_trades,
                recent_results,
            )

            # Baseline 이후 새 exchange identity 전체가 정확히 이번 run client 집합이어야 외부 주문을 숨기지 않는다.
            matching_recent_results = tuple(
                result
                for result in recent_results
                if result.exchange_order_id
                not in self.baseline_recent_exchange_order_ids
            )
            self.assertEqual(
                len(run_client_order_ids),
                len(matching_recent_results),
            )
            matching_recent_client_order_ids = frozenset(
                result.client_order_id
                for result in matching_recent_results
            )
            if matching_recent_client_order_ids != run_client_order_ids:
                raise AssertionError(
                    "fresh recent orders do not match current run identity"
                )
            self.assertEqual(
                len(matching_recent_results),
                len(
                    {
                        result.exchange_order_id
                        for result in matching_recent_results
                    }
                ),
            )
            self.assertEqual(
                len(fresh_trades),
                len({trade.trade_id for trade in fresh_trades}),
            )
            self.assertEqual(
                len(fresh_trades),
                len({trade.order_id for trade in fresh_trades}),
            )

            verified_at = self.evidence_clock()
            return (
                fresh_trades,
                fresh_runtime.performance,
                matching_recent_results,
                fresh_event_stream.session_id,
                verified_at,
            )
        finally:
            try:
                close_application(fresh_runtime)
            finally:
                fresh_event_stream.close()  # Runtime publication 종료 후 별도 session의 waiter를 회수한다.

    def _capture_failure_runtime_state(
        self,
        evidence_errors: list[str],
    ) -> dict[str, object]:
        """
        함수 이름: _capture_failure_runtime_state()
        기능: failure 시점 first runtime의 exposure·pending·durable count를 추측 없이 최소 snapshot한다.
        인자: evidence_errors -> 관찰 불가 field의 typed code를 추가할 mutable 목록
        반환값: fallback failure schema의 exact runtime_state mapping
        작성 날짜: 2026/08/31
        """
        controller = self.runtime.trading_controller
        application_status = self.runtime.state.status.name
        trading_status = controller.status.name
        reconciliation_required: bool | None = None
        position_quantity: str | None = None
        pending_order_count: int | None = None
        durable_trade_count: int | None = None

        # 각 owner를 독립 읽어 한 실패가 다른 이미 관찰 가능한 truth까지 지우지 않게 한다.
        try:
            reconciliation_required = controller.reconciliation_required
            if type(reconciliation_required) is not bool:
                raise TypeError("runtime reconciliation must be bool")
        except Exception:
            evidence_errors.append("RUNTIME_RECONCILIATION_UNAVAILABLE")
            reconciliation_required = None
        try:
            position = controller.position
            if position is None:
                raise RuntimeError("runtime position is unavailable")
            position_quantity = _decimal_to_wire(position.quantity)
        except Exception:
            evidence_errors.append("RUNTIME_POSITION_UNAVAILABLE")
        try:
            pending_order_count = len(
                self.runtime.trade_history_controller.get_pending_orders()
            )
        except Exception:
            evidence_errors.append("RUNTIME_PENDING_UNAVAILABLE")
        try:
            durable_trade_count = max(
                0,
                len(self.runtime.trade_history.trades) - len(self.baseline_trades),
            )
        except Exception:
            evidence_errors.append("RUNTIME_HISTORY_UNAVAILABLE")

        return {
            "application_status": application_status,
            "trading_status": trading_status,
            "reconciliation_required": reconciliation_required,
            "position_quantity": position_quantity,
            "pending_order_count": pending_order_count,
            "durable_trade_count": durable_trade_count,
        }

    def _capture_fresh_failure_verification(
        self,
        run_client_order_ids: frozenset[str],
        evidence_errors: list[str],
    ) -> dict[str, object]:
        """
        함수 이름: _capture_fresh_failure_verification()
        기능: 주문 권한 없는 별도 runtime의 단계별 final truth와 durability 불변을 exact stage로 기록한다.
        인자: run_client_order_ids -> permission guard가 관찰한 이번 run client identity 집합
            evidence_errors -> fresh startup/cleanup 오류의 typed code를 추가할 mutable 목록
        반환값: VERIFIED 또는 INCOMPLETE exact fresh_verification mapping
        작성 날짜: 2026/08/31
        """
        fresh_verification = _create_incomplete_fresh_verification()
        active_stage = "DURABILITY"
        active_reason = "FRESH_DURABILITY_CHANGED"
        source_durability_before: tuple[
            _FailureDurabilityFileFingerprint, ...
        ] | None = None
        copy_durability_before: tuple[
            _FailureDurabilityFileFingerprint, ...
        ] | None = None
        snapshot_handle: _FailureDurabilitySnapshotHandle | None = None
        isolated_history_path: Path | None = None
        snapshot_directory_owner: TemporaryDirectory | None = None
        fresh_event_stream: BackendEventStream | None = None
        fresh_runtime: ApplicationRuntime | None = None
        try:
            snapshot_directory_owner = TemporaryDirectory(
                prefix="phase13-failure-fresh-"
            )
            source_durability_before = _capture_failure_durability_fingerprint(
                self.history_path
            )
            snapshot_handle = _copy_failure_durability_snapshot(
                self.history_path,
                Path(snapshot_directory_owner.name),
            )
            isolated_history_path = snapshot_handle.history_path
            snapshot_handle.verify()
            source_durability_after_copy = (
                _capture_failure_durability_fingerprint(self.history_path)
            )
            copy_durability_before = _capture_failure_durability_fingerprint(
                isolated_history_path
            )
            if source_durability_after_copy != source_durability_before:
                raise RuntimeError("durability source changed after copy")
            _require_failure_durability_snapshot_equivalence(
                source_durability_after_copy,
                copy_durability_before,
            )
            active_stage = "RUNTIME_CREATION"
            active_reason = "FRESH_VERIFICATION_FAILED"
            fresh_event_stream = BackendEventStream(clock=self.evidence_clock)

            # Third flag와 cap을 제거한 별도 client만 사용해 failure evidence 수집이 mutation을 만들지 않게 한다.
            fresh_runtime = create_testnet_application_runtime(
                create_account_update_observer(fresh_event_stream),
                create_trade_history_update_observer(fresh_event_stream),
                create_trading_session_update_observer(fresh_event_stream),
                history_path=isolated_history_path,
                environment=_create_read_only_testnet_environment(
                    self.configuration
                ),
                clock=self.evidence_clock,
            )
            active_stage = "STARTUP_APPLICATION"
            ready_state = start_application(fresh_runtime)
            snapshot_handle.verify()  # Runtime path open 전후 directory ABA가 있으면 startup truth를 사용하지 않는다.
            if ready_state.status is not ApplicationStatus.READY:
                active_reason = "FRESH_RUNTIME_NOT_READY"
                raise RuntimeError("fresh runtime did not reach READY")

            # READY 직후 두 stream의 현재 세대가 모두 caught-up인지 별도 단계로 다시 확인한다.
            active_stage = "STREAM"
            active_reason = "FRESH_STREAM_NOT_READY"
            if (
                fresh_runtime.web_socket_gateway.account_ready is not True
                or fresh_runtime.web_socket_gateway.kline_live_ready is not True
            ):
                raise RuntimeError("fresh stream readiness is incomplete")

            # Composite filter 입력은 raw payload 없이 exact DTO만 보존하고 empty-state 확인 뒤 평가한다.
            active_stage = "FILTER"
            active_reason = "FRESH_FILTER_REJECTED"
            account_filters = (
                fresh_runtime.api_gateway.fetch_account_relevant_filters(
                    "ETHUSDT"
                )
            )
            symbol_rules = fresh_runtime.api_gateway.fetch_symbol_trading_rules(
                "ETHUSDT"
            )
            reference_price = fresh_runtime.api_gateway.fetch_reference_price(
                "ETHUSDT"
            )
            if (
                type(account_filters) is not AccountRelevantFilters
                or type(symbol_rules) is not SymbolTradingRules
                or type(reference_price) is not ReferencePrice
                or account_filters.symbol != "ETHUSDT"
                or symbol_rules.symbol != "ETHUSDT"
                or reference_price.symbol != "ETHUSDT"
            ):
                raise TypeError("fresh filter DTO identity is invalid")

            active_stage = "LOCAL_STATE"
            active_reason = "FRESH_LOCAL_STATE_INCOMPLETE"
            position = fresh_runtime.trading_controller.position
            position_quantity = (
                None if position is None else _decimal_to_wire(position.quantity)
            )
            pending_order_count = len(
                fresh_runtime.trade_history_controller.get_pending_orders()
            )
            reconciliation_required = (
                fresh_runtime.trading_controller.reconciliation_required
            )
            fresh_verification.update(
                {
                    "position_quantity": position_quantity,
                    "pending_order_count": pending_order_count,
                    "reconciliation_required": reconciliation_required,
                }
            )
            if (
                position_quantity is None
                or pending_order_count != 0
                or reconciliation_required is not False
            ):
                raise RuntimeError("fresh local state is incomplete")

            # Account-wide openOrders와 symbol 전체 client 결과를 각각 empty로 확인한다.
            active_stage = "OPEN_ORDERS"
            active_reason = "FRESH_OPEN_ORDERS_NOT_EMPTY"
            has_any_open_orders = (
                fresh_runtime.api_gateway.has_any_exchange_open_orders()
            )
            if type(has_any_open_orders) is not bool:
                raise TypeError("fresh account open-order state must be bool")
            fresh_verification["account_open_orders_empty"] = (
                not has_any_open_orders
            )
            open_results = tuple(
                fresh_runtime.api_gateway.list_all_open_order_results(
                    "ETHUSDT"
                )
            )
            matching_open_order_count = sum(
                result.client_order_id in run_client_order_ids
                for result in open_results
            )
            fresh_verification["matching_open_order_count"] = (
                matching_open_order_count
            )
            if has_any_open_orders:
                raise RuntimeError("fresh account has open orders")
            require_empty_all_client_open_orders(open_results)

            # Order-list child ID를 기록하지 않고 account-wide boolean만 exact empty로 축약한다.
            active_stage = "OPEN_ORDER_LISTS"
            active_reason = "FRESH_OPEN_ORDER_LISTS_NOT_EMPTY"
            has_any_open_order_lists = (
                fresh_runtime.api_gateway.has_any_exchange_open_order_lists()
            )
            if type(has_any_open_order_lists) is not bool:
                raise TypeError("fresh open-order-list state must be bool")
            fresh_verification["account_open_order_lists_empty"] = (
                not has_any_open_order_lists
            )
            if has_any_open_order_lists:
                raise RuntimeError("fresh account has open order lists")

            # 두 account-wide empty snapshot 뒤에만 candidate composite evaluator를 통과시킨다.
            active_stage = "FILTER"
            active_reason = "FRESH_FILTER_REJECTED"
            minimum_candidate_quantity = max(
                Decimal("1").scaleb(-symbol_rules.base_asset_precision),
                symbol_rules.lot_size.minimum_quantity,
                symbol_rules.market_lot_size.minimum_quantity,
                symbol_rules.lot_size.step_size,
                symbol_rules.market_lot_size.step_size,
            )
            validate_account_relevant_filters(
                minimum_candidate_quantity,
                reference_price.price,
                symbol_rules,
                account_filters,
                side=OrderSide.BUY,
                account_open_state_verified_empty=True,
            )

            active_stage = "RECENT_ORDERS"
            active_reason = "FRESH_EXCHANGE_BASELINE_UNAVAILABLE"
            recent_results = tuple(
                fresh_runtime.api_gateway.list_all_recent_order_results(
                    "ETHUSDT",
                    limit=1000,
                )
            )
            run_exchange_order_count: int | None = None
            if self.baseline_recent_exchange_order_ids is not None and all(
                result.exchange_order_id is not None for result in recent_results
            ):
                run_exchange_order_count = sum(
                    str(result.exchange_order_id)
                    not in self.baseline_recent_exchange_order_ids
                    for result in recent_results
                )
            else:
                raise RuntimeError("fresh exchange baseline is unavailable")
            fresh_trades = fresh_runtime.trade_history.trades
            verify_exact_recent_order_baseline(fresh_trades, recent_results)
            fresh_verification["run_exchange_order_count"] = (
                run_exchange_order_count
            )
            fresh_verification["durable_trade_count"] = max(
                0,
                len(fresh_trades) - len(self.baseline_trades),
            )

            # Publication 직전 account-wide·symbol open truth를 다시 읽어 REST 사이 TOCTOU를 닫는다.
            active_stage = "OPEN_ORDERS"
            active_reason = "FRESH_OPEN_ORDERS_NOT_EMPTY"
            final_has_any_open_orders = (
                fresh_runtime.api_gateway.has_any_exchange_open_orders()
            )
            if type(final_has_any_open_orders) is not bool:
                raise TypeError("final account open-order state must be bool")
            final_open_results = tuple(
                fresh_runtime.api_gateway.list_all_open_order_results(
                    "ETHUSDT"
                )
            )
            fresh_verification["account_open_orders_empty"] = (
                not final_has_any_open_orders
            )
            fresh_verification["matching_open_order_count"] = sum(
                result.client_order_id in run_client_order_ids
                for result in final_open_results
            )
            if final_has_any_open_orders:
                raise RuntimeError("fresh final open-order state changed")
            if final_open_results != open_results:
                active_reason = "FRESH_OPEN_ORDERS_CHANGED"
                raise RuntimeError("fresh final open-order results changed")
            require_empty_all_client_open_orders(final_open_results)

            active_stage = "OPEN_ORDER_LISTS"
            active_reason = "FRESH_OPEN_ORDER_LISTS_NOT_EMPTY"
            final_has_any_open_order_lists = (
                fresh_runtime.api_gateway.has_any_exchange_open_order_lists()
            )
            if type(final_has_any_open_order_lists) is not bool:
                raise TypeError("final open-order-list state must be bool")
            fresh_verification["account_open_order_lists_empty"] = (
                not final_has_any_open_order_lists
            )
            if (
                final_has_any_open_order_lists
                or final_has_any_open_order_lists != has_any_open_order_lists
            ):
                raise RuntimeError("fresh final open-order-list state changed")

            # Recent order tuple과 durable baseline도 같은 runtime에서 두 번 읽어 drift를 숨기지 않는다.
            active_stage = "RECENT_ORDERS"
            active_reason = "FRESH_RECENT_ORDERS_CHANGED"
            final_recent_results = tuple(
                fresh_runtime.api_gateway.list_all_recent_order_results(
                    "ETHUSDT",
                    limit=1000,
                )
            )
            if final_recent_results != recent_results:
                fresh_verification["run_exchange_order_count"] = None
                raise RuntimeError("fresh recent orders changed")
            verify_exact_recent_order_baseline(
                fresh_trades,
                final_recent_results,
            )

            # 최종 publication 직전 stream과 reconciliation을 재검사해 앞 snapshot의 TOCTOU를 닫는다.
            active_stage = "STREAM"
            active_reason = "FRESH_STREAM_NOT_READY"
            if (
                fresh_runtime.web_socket_gateway.account_ready is not True
                or fresh_runtime.web_socket_gateway.kline_live_ready is not True
                or fresh_runtime.trading_controller.reconciliation_required
                is not False
            ):
                raise RuntimeError("fresh final stream state is incomplete")
            fresh_verification.update(
                {
                    "status": "VERIFIED",
                    "typed_reason": None,
                    "failure_stage": None,
                    "verified_at": _datetime_to_wire(
                        self.evidence_clock()
                    ),
                }
            )  # VERIFIED는 성공 판정이 아니라 모든 required read-only truth가 concrete하다는 뜻이다.
        except ApplicationStartupError as startup_error:
            allowed_startup_codes = _STARTUP_STAGE_ALLOWED_FAILURE_CODES.get(
                startup_error.stage,
                frozenset(),
            )
            if startup_error.code in allowed_startup_codes:
                startup_stage = _STARTUP_STAGE_TO_FAILURE_STAGE[
                    startup_error.stage
                ]
                startup_reason = startup_error.code.value
            else:
                startup_stage = "STARTUP_APPLICATION"
                startup_reason = "FRESH_VERIFICATION_FAILED"
            _set_fresh_verification_failure(
                fresh_verification,
                startup_stage,
                startup_reason,
                evidence_errors,
            )
        except Exception:
            _set_fresh_verification_failure(
                fresh_verification,
                active_stage,
                active_reason,
                evidence_errors,
            )
        finally:
            if fresh_runtime is not None:
                try:
                    fresh_closed_state = close_application(fresh_runtime)
                    if (
                        type(fresh_closed_state) is not ApplicationStateSnapshot
                        or fresh_closed_state.status is not ApplicationStatus.CLOSED
                    ):
                        raise RuntimeError(
                            "fresh runtime close did not publish CLOSED"
                        )
                except Exception:
                    _set_fresh_verification_failure(
                        fresh_verification,
                        "CLEANUP",
                        "FRESH_RUNTIME_CLOSE_FAILED",
                        evidence_errors,
                    )
            if fresh_event_stream is not None:
                try:
                    fresh_event_stream.close()
                except Exception:
                    _set_fresh_verification_failure(
                        fresh_verification,
                        "CLEANUP",
                        "FRESH_RUNTIME_CLOSE_FAILED",
                        evidence_errors,
                    )

            # Source와 isolated 세 durable leaf 중 하나라도 변하면 VERIFIED를 강등한다.
            try:
                if snapshot_handle is None:
                    raise RuntimeError("durability snapshot handle is unavailable")
                snapshot_handle.verify()
                source_durability_after = (
                    _capture_failure_durability_fingerprint(
                        self.history_path
                    )
                )
                copy_durability_after = (
                    None
                    if isolated_history_path is None
                    else _capture_failure_durability_fingerprint(
                        isolated_history_path
                    )
                )
                source_durability_after_final_copy = (
                    _capture_failure_durability_fingerprint(self.history_path)
                )  # Copy fingerprint 사이의 source leaf in-place ABA를 source→copy→source로 잡는다.

                # Path fingerprint 중 absent sidecar ABA와 sandwich 직후 directory 변경을 final pinned-parent barrier에서 다시 잡는다.
                snapshot_handle.verify()
                durability_changed = (
                    source_durability_before is None
                    or source_durability_after != source_durability_before
                    or source_durability_after_final_copy
                    != source_durability_before
                    or copy_durability_before is None
                    or copy_durability_after != copy_durability_before
                )
                if durability_changed:
                    _set_fresh_verification_failure(
                        fresh_verification,
                        "DURABILITY",
                        "FRESH_DURABILITY_CHANGED",
                        evidence_errors,
                    )
            except Exception:
                _set_fresh_verification_failure(
                    fresh_verification,
                    "DURABILITY",
                    "FRESH_DURABILITY_CHANGED",
                    evidence_errors,
                )

            # Pinned descriptor를 fingerprint 검사 뒤 닫고 그 다음에만 임시 directory를 파기한다.
            if snapshot_handle is not None:
                try:
                    snapshot_handle.close()
                except Exception:
                    _set_fresh_verification_failure(
                        fresh_verification,
                        "CLEANUP",
                        "FRESH_SNAPSHOT_CLEANUP_FAILED",
                        evidence_errors,
                    )
            if snapshot_directory_owner is not None:
                try:
                    snapshot_directory_owner.cleanup()
                except Exception:
                    _set_fresh_verification_failure(
                        fresh_verification,
                        "CLEANUP",
                        "FRESH_SNAPSHOT_CLEANUP_FAILED",
                        evidence_errors,
                    )

        return fresh_verification  # Partial truth와 최초 failure stage를 cleanup 뒤 최종 mapping으로 반환한다.

    def _attempt_known_safe_failure_recovery(
        self,
        initial_guard_snapshot: Phase13OrderSubmissionGuardSnapshot,
        evidence_errors: list[str],
    ) -> dict[str, object]:
        """
        함수 이름: _attempt_known_safe_failure_recovery()
        기능: 단일 durable BUY와 비모호 exposure가 증명된 경우에만 public STOP recovery를 한 번 완결한다.
        인자: initial_guard_snapshot -> failure 처리 진입 직후 아직 닫지 않은 permission snapshot
            evidence_errors -> recovery 오류의 typed code를 추가할 mutable 목록
        반환값: failure artifact에 넣을 exact recovery outcome mapping
        작성 날짜: 2026/08/31
        """
        if type(initial_guard_snapshot) is not Phase13OrderSubmissionGuardSnapshot:
            raise TypeError("initial_guard_snapshot must be the exact frozen type")
        if not isinstance(evidence_errors, list):
            raise TypeError("evidence_errors must be a list")

        controller = self.runtime.trading_controller
        try:
            position = controller.position
            position_quantity = None if position is None else position.quantity
            pending_orders = tuple(
                self.runtime.trade_history_controller.get_pending_orders()
            )
            reconciliation_required = controller.reconciliation_required
            run_trades = tuple(
                self.runtime.trade_history.trades[len(self.baseline_trades) :]
            )
            application_ready = (
                self.runtime.state.status is ApplicationStatus.READY
            )
        except Exception:
            evidence_errors.append("FAILURE_RECOVERY_STATE_UNAVAILABLE")
            return {
                "attempted": False,
                "outcome": "SKIPPED",
                "typed_reason": "FAILURE_RECOVERY_STATE_UNAVAILABLE",
            }

        # 이미 zero exposure이고 pending·UNKNOWN이 없으면 새 SELL 없이 failure evidence만 남긴다.
        if (
            isinstance(position_quantity, Decimal)
            and position_quantity == Decimal("0")
            and not pending_orders
            and reconciliation_required is False
        ):
            return {
                "attempted": False,
                "outcome": "NOT_REQUIRED",
                "typed_reason": None,
            }

        initial_attempts = initial_guard_snapshot.attempts
        durable_buy = run_trades[0] if len(run_trades) == 1 else None
        initial_attempt = initial_attempts[0] if len(initial_attempts) == 1 else None
        exact_known_safe_state = (
            application_ready
            and isinstance(position_quantity, Decimal)
            and position_quantity.is_finite()
            and position_quantity > Decimal("0")
            and not pending_orders
            and reconciliation_required is False
            and controller.status
            in {TradingSessionStatus.RUNNING, TradingSessionStatus.STOPPING}
            and initial_guard_snapshot.mutation_started
            and not initial_guard_snapshot.submissions_blocked
            and initial_attempt is not None
            and initial_attempt.symbol == "ETHUSDT"
            and initial_attempt.side is OrderSide.BUY
            and initial_attempt.submission_attempt == 0
            and durable_buy is not None
            and durable_buy.symbol == "ETHUSDT"
            and durable_buy.side is OrderSide.BUY
            and durable_buy.strategy is StrategyType.CASE_C
            and durable_buy.regime_type is RegimeType.TYPE_0
            and durable_buy.exit_reason is None
            and durable_buy.client_order_id == initial_attempt.client_order_id
            and durable_buy.executed_quantity == position_quantity
        )
        if not exact_known_safe_state:
            evidence_errors.append("FAILURE_RECOVERY_STATE_AMBIGUOUS")
            return {
                "attempted": False,
                "outcome": "SKIPPED",
                "typed_reason": "FAILURE_RECOVERY_STATE_AMBIGUOUS",
            }  # UNKNOWN·pending·추가 attempt에서는 중복 SELL보다 관찰 가능한 exposure 보존을 우선한다.

        try:
            effective_free_quantity = self._wait_for_effective_free_eth(
                position_quantity
            )
        except Exception:
            evidence_errors.append(
                "FAILURE_RECOVERY_EFFECTIVE_FREE_UNCONFIRMED"
            )
            return {
                "attempted": False,
                "outcome": "SKIPPED",
                "typed_reason": "FAILURE_RECOVERY_EFFECTIVE_FREE_UNCONFIRMED",
            }
        if effective_free_quantity != position_quantity:
            evidence_errors.append(
                "FAILURE_RECOVERY_EFFECTIVE_FREE_UNCONFIRMED"
            )
            return {
                "attempted": False,
                "outcome": "SKIPPED",
                "typed_reason": "FAILURE_RECOVERY_EFFECTIVE_FREE_UNCONFIRMED",
            }

        # Account wait 중 scheduler가 전진했을 수 있으므로 STOP 직전 guard·Position·pending을 다시 고정한다.
        try:
            refreshed_guard, _ = self._read_phase13_submission_guard()
            refreshed_position = controller.position
            refreshed_pending_orders = tuple(
                self.runtime.trade_history_controller.get_pending_orders()
            )
            refreshed_state_is_exact = (
                refreshed_guard.mutation_started
                and not refreshed_guard.submissions_blocked
                and refreshed_guard.attempts == initial_attempts
                and refreshed_position is not None
                and refreshed_position.quantity == position_quantity
                and not refreshed_pending_orders
                and controller.reconciliation_required is False
                and controller.status
                in {TradingSessionStatus.RUNNING, TradingSessionStatus.STOPPING}
            )
        except Exception:
            refreshed_state_is_exact = False
        if not refreshed_state_is_exact:
            evidence_errors.append("FAILURE_RECOVERY_STATE_CHANGED")
            return {
                "attempted": False,
                "outcome": "SKIPPED",
                "typed_reason": "FAILURE_RECOVERY_STATE_CHANGED",
            }

        try:
            # RUNNING에서만 public command를 한 번 발행하고 이미 STOPPING이면 기존 command의 terminal만 기다린다.
            if controller.status is TradingSessionStatus.RUNNING:
                stop_result = controller.stop_trading(
                    command_id=f"phase13-failure-recovery-{self.run_id}",
                    expected_version=controller.context.version,
                )
                if stop_result.status not in {
                    TradingSessionStatus.STOPPING,
                    TradingSessionStatus.TERMINATED,
                }:
                    raise AssertionError(
                        "known-safe failure recovery did not enter a terminal path"
                    )
            self._wait_for_session_termination()
            self._collect_runtime_observations()

            # SUCCESS는 exact BUY→STOP SELL, zero Position과 empty pending을 모두 다시 읽은 경우뿐이다.
            final_guard, _ = self._read_phase13_submission_guard()
            final_position = controller.position
            final_pending_orders = tuple(
                self.runtime.trade_history_controller.get_pending_orders()
            )
            final_run_trades = tuple(
                self.runtime.trade_history.trades[len(self.baseline_trades) :]
            )
            if (
                controller.status is not TradingSessionStatus.TERMINATED
                or controller.reconciliation_required
                or final_position is None
                or final_position.quantity != Decimal("0")
                or final_pending_orders
                or not final_guard.submissions_blocked
                or tuple(attempt.side for attempt in final_guard.attempts)
                != (OrderSide.BUY, OrderSide.SELL)
                or final_guard.attempts[0] != initial_attempt
                or len(final_run_trades) != 2
                or tuple(trade.side for trade in final_run_trades)
                != (OrderSide.BUY, OrderSide.SELL)
                or final_run_trades[0].client_order_id
                != final_guard.attempts[0].client_order_id
                or final_run_trades[1].client_order_id
                != final_guard.attempts[1].client_order_id
                or final_run_trades[1].symbol != "ETHUSDT"
                or final_run_trades[1].strategy is not StrategyType.CASE_C
                or final_run_trades[1].exit_reason is not ExitReason.STOP
                or final_run_trades[1].executed_quantity != position_quantity
            ):
                raise AssertionError(
                    "known-safe failure recovery lacks exact terminal evidence"
                )
        except Exception as recovery_error:
            recovery_reason = _classify_failure_recovery_error(recovery_error)
            evidence_errors.append(recovery_reason)
            return {
                "attempted": True,
                "outcome": "FAILED",
                "typed_reason": recovery_reason,
            }

        return {
            "attempted": True,
            "outcome": "SUCCESS",
            "typed_reason": None,
        }

    def _record_actual_failure_evidence(self, original_error: Exception) -> None:
        """
        함수 이름: _record_actual_failure_evidence()
        기능: known-safe exposure만 먼저 복구한 뒤 submit을 영구 차단하고 secret-free FAILED artifact를 기록한다.
        인자: original_error -> caller가 그대로 다시 발생시킬 최초 정상 Exception
        반환값: failure artifact가 기록되면 없음
        작성 날짜: 2026/08/31
        """
        if not isinstance(original_error, Exception):
            raise TypeError("original_error must be an Exception")

        # Finalizer mutation 전 현재 snapshot을 한 번만 읽어 exception-bound 최초 cause와 TOCTOU 대조한다.
        try:
            current_cause_snapshot = (
                self.runtime.trading_controller.reconciliation_cause_snapshot
            )
        except Exception:
            current_cause_snapshot = object()
        if isinstance(original_error, _PhaseThirteenReconciliationFailure):
            frozen_cause_snapshot = original_error.cause_snapshot
            if (
                type(current_cause_snapshot) is ReconciliationCauseSnapshot
                and current_cause_snapshot == frozen_cause_snapshot
            ):
                cause_snapshot = frozen_cause_snapshot
            else:
                current_reconciliation_required = (
                    current_cause_snapshot.reconciliation_required
                    if type(current_cause_snapshot)
                    is ReconciliationCauseSnapshot
                    else False
                )
                cause_snapshot = ReconciliationCauseSnapshot(
                    reconciliation_required=(
                        frozen_cause_snapshot.reconciliation_required
                        or current_reconciliation_required
                    ),
                    status=ReconciliationCauseStatus.CONFLICT,
                    category=None,
                )  # Mismatch에서 category를 선택하지 않고 typed secondary conflict로 fail-close한다.
        else:
            cause_snapshot = current_cause_snapshot
        self.failure_handling_started = True
        evidence_errors: list[str] = []
        first_cause = _normalize_failure_first_cause(
            cause_snapshot,
            evidence_errors,
        )

        # Permission proxy가 exact next STOP SELL만 허용하는 동안 known-safe 단일 BUY exposure를 먼저 회수한다.
        try:
            initial_guard_snapshot, _ = self._read_phase13_submission_guard()
            self.mutation_started = (
                self.mutation_started
                or initial_guard_snapshot.mutation_started
            )
            recovery_evidence = self._attempt_known_safe_failure_recovery(
                initial_guard_snapshot,
                evidence_errors,
            )
        except Exception as recovery_error:
            recovery_reason = _classify_failure_recovery_error(recovery_error)
            evidence_errors.append(recovery_reason)
            recovery_evidence = {
                "attempted": False,
                "outcome": "SKIPPED",
                "typed_reason": recovery_reason,
            }
        finally:
            # 성공·skip·예외 모두에서 다음 scheduler mutation보다 먼저 logical submit permit을 영구 닫는다.
            self.runtime.api_gateway.block_phase13_order_submissions()

        guard_snapshot, submission_attempts = self._read_phase13_submission_guard()
        if not guard_snapshot.submissions_blocked:
            raise AssertionError("Phase 13 failure finalizer did not close submissions")
        self.mutation_started = self.mutation_started or guard_snapshot.mutation_started
        controller = self.runtime.trading_controller

        # Terminal check와 active/reconciliation blocker commit을 Controller의 같은 session lock에서 수행한다.
        try:
            controller.seal_event_runtime_failure_gate()
        except Exception:
            evidence_errors.append(
                "FAILURE_SCHEDULER_GATE_PUBLICATION_FAILED"
            )  # Production은 status/event blocker를 Context publication보다 먼저 commit한다.
        if controller.status in (
            TradingSessionStatus.RUNNING,
            TradingSessionStatus.STOPPING,
        ):
            raise AssertionError(
                "Phase 13 failure finalizer left the scheduler gate open"
            )  # Production mark는 Context publication보다 먼저 공개 status를 닫아야 한다.

        try:
            self._collect_runtime_observations()
        except Exception:
            pass  # 각 authoritative runtime field는 아래에서 독립 iff error로 다시 읽는다.
        runtime_state = self._capture_failure_runtime_state(evidence_errors)

        # Block가 닫힌 뒤 worker와 stream을 먼저 멈춰 fresh reader가 같은 history와 동시에 변경되지 않게 한다.
        runtime_closed = False
        try:
            closed_state = close_application(self.runtime)
            runtime_closed = closed_state.status is ApplicationStatus.CLOSED
        except Exception:
            runtime_closed = False
        if not runtime_closed:
            evidence_errors.append("PRIMARY_RUNTIME_NOT_CLOSED")
            fresh_verification = _create_incomplete_fresh_verification()
            fresh_verification["typed_reason"] = "PRIMARY_RUNTIME_NOT_CLOSED"
            fresh_verification["failure_stage"] = "CLEANUP"
        else:
            run_client_order_ids = frozenset(
                attempt.client_order_id
                for attempt in guard_snapshot.attempts
            )  # Sensitive logical ID는 fresh matching에만 사용하고 v2 mapping으로 넘기지 않는다.
            fresh_verification = self._capture_fresh_failure_verification(
                run_client_order_ids,
                evidence_errors,
            )

        # Completed 주장은 두 runtime의 safe terminal facts가 모두 concrete할 때만 seal한다.
        if recovery_evidence["outcome"] in {"SUCCESS", "NOT_REQUIRED"} and not (
            _failure_recovery_has_safe_terminal_facts(
                recovery_evidence["outcome"],
                runtime_state,
                fresh_verification,
            )
        ):
            final_verification_reason = (
                "FAILURE_RECOVERY_FINAL_VERIFICATION_INCOMPLETE"
            )
            evidence_errors.append(final_verification_reason)
            recovery_evidence = {
                "attempted": recovery_evidence["attempted"],
                "outcome": (
                    "FAILED" if recovery_evidence["attempted"] else "SKIPPED"
                ),
                "typed_reason": final_verification_reason,
            }

        # Exception text/traceback 대신 stable category와 관찰 가능 상태만 별도 fallback schema에 넣는다.
        failure_body = {
            "schema_version": _PHASE13_FAILURE_EVIDENCE_SCHEMA_VERSION,
            "record_type": _PHASE13_FAILURE_EVIDENCE_RECORD_TYPE,
            "outcome": "FAILED",
            "typed_reason": _classify_actual_failure(original_error),
            "run_id": self.run_id,
            "timestamps": {
                "started_at": _datetime_to_wire(self.started_at),
                "completed_at": _datetime_to_wire(self.evidence_clock()),
            },
            "mutation_guard": {
                "mutation_started": guard_snapshot.mutation_started,
                "submissions_blocked": guard_snapshot.submissions_blocked,
                "submission_attempts": submission_attempts,
            },
            "recovery": recovery_evidence,
            "runtime_state": runtime_state,
            "fresh_verification": fresh_verification,
            "first_cause": first_cause,
            "evidence_errors": list(dict.fromkeys(evidence_errors)),
        }
        failure_path, failure_digest = _write_failure_evidence_artifact(
            self.artifact_directory,
            failure_body,
            forbidden_values=(
                self.configuration.api_key,
                self.configuration.api_secret,
            ),
        )
        self.failure_artifact_path = failure_path
        original_error.add_note(
            "Phase 13 sealed FAILED evidence preserved at "
            f"{failure_path}; sha256={failure_digest}"
        )  # Note에는 credential·원 예외 message가 아니라 검증된 local path와 digest만 남긴다.

    def _record_non_signal_and_fail(self) -> None:
        """
        함수 이름: _record_non_signal_and_fail()
        기능: 주문 0과 fresh zero exposure를 seal한 뒤 NO_SIGNAL을 성공으로 승격하지 않고 실패한다.
        인자: 없음
        반환값: 정상 반환하지 않음
        작성 날짜: 2026/08/31
        """
        controller = self.runtime.trading_controller
        public_boundary_trace = controller.public_market_boundary_trace
        public_action_observed = any(
            boundary_entry.message_id == "1L.3"
            for boundary_entry in public_boundary_trace
        )
        if controller.order_execution_trace or public_action_observed:
            raise RuntimeError(
                "public action without a terminal order requires failure evidence"
            )  # Action 이후 blocker는 원인을 버린 full trace가 아니라 outer failure v2로 보존한다.
        self.assertEqual(
            len(self.baseline_trades),
            len(self.runtime.trade_history.trades),
        )
        pending_upserts = _extract_pending_order_upserts(
            self.runtime.trade_history_repository.pending_order_storage_path
        )
        if pending_upserts:
            raise AssertionError(
                "NO_SIGNAL requires zero pending order submissions"
            )  # Pending metadata와 client ID를 unittest failure repr에 반사하지 않는다.
        position = controller.position
        self.assertIsNotNone(position)
        _require_zero_position_quantity(position.quantity)

        # Position 없는 RUNNING session도 public stop으로 TERMINATED한 뒤 network resources를 닫는다.
        self._stop_current_run_for_safety()
        self._collect_runtime_observations()
        closed_state = close_application(self.runtime)
        self.assertIs(closed_state.status, ApplicationStatus.CLOSED)
        (
            fresh_trades,
            fresh_performance,
            matching_results,
            fresh_runtime_session_id,
            final_verified_at,
        ) = self._fresh_read_only_verification(frozenset())
        if matching_results:
            raise AssertionError(
                "NO_SIGNAL fresh verification found unexpected run orders"
            )  # Fresh OrderResult identity와 fill은 고정 문장 뒤에 숨긴다.
        if self.preflight is None:
            raise AssertionError("actual trace requires completed preflight evidence")
        trace_body = _create_non_mutating_trace_body(
            outcome="NO_SIGNAL",
            typed_reason=None,
            run_id=self.run_id,
            started_at=self.started_at,
            completed_at=self.evidence_clock(),
            preflight=self.preflight,
            market_events=self.public_market_events,
            account_events=self.public_account_events,
            transport_events=_normalize_transport_ui_events(
                self.transport_event_dtos,
                transport_session_id=self.event_stream.session_id,
            ),
            baseline_history_sha256=self.baseline_history_sha256,
            baseline_trades=self.baseline_trades,
            total_trades=fresh_trades,
            performance=fresh_performance,
            final_verified_at=final_verified_at,
            fresh_runtime_session_id=fresh_runtime_session_id,
        )
        trace_path, trace_digest = _write_trace_artifact(
            self.artifact_directory,
            trace_body,
            forbidden_values=(
                self.configuration.api_key,
                self.configuration.api_secret,
            ),
        )

        raise _PhaseThirteenRecordedOutcomeFailure(
            "NO_SIGNAL: no actual order was submitted; "
            f"trace={trace_path}; sha256={trace_digest}"
        )  # Signal 부재를 skip/PASS로 바꾸지 않아 Phase 13 readiness를 계속 잠근다.

    def _create_success_trace_body(
        self,
        *,
        buy_trade: Trade,
        sell_trade: Trade,
        upserts_by_client_id: Mapping[str, Mapping[str, object]],
        matching_recent_results: Sequence[OrderResult],
        fresh_trades: Sequence[Trade],
        fresh_performance: Performance,
        fresh_runtime_session_id: str,
        final_verified_at: datetime,
        risk_decision: object,
        authoritative_position_quantity: Decimal,
        effective_free_quantity: Decimal,
    ) -> dict[str, object]:
        """
        함수 이름: _create_success_trace_body()
        기능: production trace, journal, exchange, durable state와 UI DTO를 하나의 SUCCESS body로 결속한다.
        인자: buy_trade -> public signal이 만든 durable BUY Trade
            sell_trade -> public STOP이 만든 durable SELL Trade
            upserts_by_client_id -> 두 append-only Order metadata mapping
            matching_recent_results -> fresh signed query의 두 normalized terminal 결과
            fresh_trades -> fresh runtime의 전체 durable Trade sequence
            fresh_performance -> 같은 fresh runtime의 전체 Performance
            fresh_runtime_session_id -> read-only restart의 별도 BackendEventStream UUID
            final_verified_at -> fresh zero-state 교차 검증 UTC 시각
            risk_decision -> BUY 직전 production RiskDecision
            authoritative_position_quantity -> STOP 전에 고정한 exact Position 수량
            effective_free_quantity -> public account stream이 증명한 STOP 전 free ETH
        반환값: trace_sha256를 제외한 exact SUCCESS trace body
        작성 날짜: 2026/08/31
        """
        if not isinstance(buy_trade, Trade) or not isinstance(sell_trade, Trade):
            raise TypeError("buy_trade and sell_trade must be Trade values")
        if not isinstance(fresh_performance, Performance):
            raise TypeError("fresh_performance must be a Performance")
        if self.preflight is None:
            raise AssertionError("successful trace requires completed preflight")
        budget = getattr(risk_decision, "budget", None)
        if budget is None or getattr(risk_decision, "allowed", None) is not True:
            raise AssertionError("successful trace requires an allowed production risk decision")

        # 각 durable Trade는 정확히 하나의 current sidecar UPSERT와 하나의 terminal exchange result를 가져야 한다.
        ordered_trades = (buy_trade, sell_trade)
        order_metadata_by_client_id = dict(upserts_by_client_id)
        results_by_client_id = {
            result.client_order_id: result for result in matching_recent_results
        }
        if set(order_metadata_by_client_id) != {
            trade.client_order_id for trade in ordered_trades
        }:
            raise AssertionError("pending UPSERT identities do not match run Trades")
        if set(results_by_client_id) != set(order_metadata_by_client_id):
            raise AssertionError("recent exchange identities do not match pending UPSERTs")

        # Communication trace의 최초 message 1이 BUY의 public source와 두 주문의 evaluation identity를 고정한다.
        execution_traces_by_client_id: dict[str, tuple[object, ...]] = {}
        order_execution_traces: list[dict[str, object]] = []
        for trade in ordered_trades:
            matching_traces = tuple(
                trace_entry
                for trace_entry in self.runtime.trading_controller.order_execution_trace
                if trace_entry.client_order_id == trade.client_order_id
            )
            if not matching_traces or matching_traces[0].message_id != "1":
                raise AssertionError("each actual order requires a complete Communication trace")
            if matching_traces[-1].message_id != "14":
                raise AssertionError("each actual order trace must finish at message 14")
            trace_message_ids = tuple(
                trace_entry.message_id for trace_entry in matching_traces
            )
            if any(trace_entry.result.value != "SUCCESS" for trace_entry in matching_traces):
                raise AssertionError("successful evidence cannot contain a failed order trace")
            _assert_complete_success_order_trace(
                trace_message_ids,
                side=trade.side,
            )
            execution_traces_by_client_id[trade.client_order_id] = matching_traces

        # Runtime 검증에 사용한 같은 frozen entry 전부를 artifact에도 넣어 독립 validator가 1~14를 재검증한다.
        for order_sequence, trade in enumerate(ordered_trades, start=1):
            matching_traces = execution_traces_by_client_id[
                trade.client_order_id
            ]
            order_metadata = order_metadata_by_client_id[trade.client_order_id]
            if any(
                trace_entry.intent_id != order_metadata["intent_id"]
                or trace_entry.client_order_id != trade.client_order_id
                for trace_entry in matching_traces
            ):
                raise AssertionError(
                    "order execution trace identity changed within an order"
                )
            order_execution_traces.append(
                {
                    "sequence": order_sequence,
                    "intent_id": order_metadata["intent_id"],
                    "client_order_id": trade.client_order_id,
                    "side": trade.side.value,
                    "entries": [
                        {
                            "sequence": entry_sequence,
                            "message_id": trace_entry.message_id,
                            "command_event_id": trace_entry.command_event_id,
                            "order_id": trace_entry.order_id,
                            "context_version_before": (
                                trace_entry.context_version_before
                            ),
                            "context_version_after": (
                                trace_entry.context_version_after
                            ),
                            "result": trace_entry.result.value,
                            "failure_code": (
                                None
                                if trace_entry.failure_code is None
                                else trace_entry.failure_code.value
                            ),
                        }
                        for entry_sequence, trace_entry in enumerate(
                            matching_traces,
                            start=1,
                        )
                    ],
                }
            )

        buy_trace = execution_traces_by_client_id[buy_trade.client_order_id][0]
        parsed_market_source = _parse_public_market_command_event(
            buy_trace.command_event_id
        )
        if parsed_market_source["market_version"] != budget.market_version:
            raise AssertionError("public source market version does not match risk decision")
        if budget.account_version not in {
            account_event["account_version"] for account_event in self.public_account_events
        }:
            raise AssertionError("risk decision account version has no public account event")

        # BUY journal claim must equal the post-filter risk candidate and never exceed configured/absolute cap.
        buy_metadata = order_metadata_by_client_id[buy_trade.client_order_id]
        buy_decision_price = _metadata_decimal(
            buy_metadata,
            "market_price_at_decision",
        )
        buy_submitted_quantity = _metadata_decimal(
            buy_metadata,
            "submitted_quantity",
        )
        with localcontext() as decimal_context:
            decimal_context.prec = 34
            buy_notional = buy_decision_price * buy_submitted_quantity
        if buy_notional != budget.candidate_order_notional:
            raise AssertionError("pending BUY claim does not match the risk budget candidate")
        if buy_notional > self.maximum_notional or self.maximum_notional > Decimal("100"):
            raise AssertionError("BUY notional exceeds the configured or absolute cap")
        # Preflight filter는 조회 시점 snapshot이고 prepare_order의 재조회 후 pending UPSERT 수량이 submit-time 권위다.
        if buy_submitted_quantity != buy_trade.executed_quantity:
            raise AssertionError("pending BUY final quantity does not match durable execution")

        # Production observer가 실제로 기록한 같은 evaluation의 세 frozen entry만 normalized 1L chain으로 사용한다.
        observed_boundary_entries = tuple(
            trace_entry
            for trace_entry in (
                self.runtime.trading_controller.public_market_boundary_trace
            )
            if trace_entry.evaluation_id == buy_trace.command_event_id
        )
        if tuple(
            trace_entry.message_id
            for trace_entry in observed_boundary_entries
        ) != ("1L.1", "1L.2", "1L.3"):
            raise AssertionError(
                "successful BUY requires an actually observed exact public 1L chain"
            )
        if observed_boundary_entries[-1].context_version != budget.context_version:
            raise AssertionError(
                "public Action observer context does not match the risk decision"
            )
        success_market_events: list[dict[str, object]] = []
        for sequence, boundary_entry in enumerate(
            observed_boundary_entries,
            start=1,
        ):
            parsed_boundary_source = _parse_public_market_command_event(
                boundary_entry.evaluation_id
            )
            if (
                parsed_boundary_source != parsed_market_source
                or boundary_entry.market_version
                != parsed_boundary_source["market_version"]
                or boundary_entry.regime is not buy_trade.regime_type
            ):
                raise AssertionError(
                    "public boundary observer changed immutable BUY provenance"
                )
            success_market_events.append(
                {
                    "sequence": sequence,
                    "message_id": boundary_entry.message_id,
                    "event_type": boundary_entry.event_type,
                    "source_event_id": parsed_boundary_source["source_event_id"],
                    "source_kline_identity": parsed_boundary_source[
                        "source_kline_identity"
                    ],
                    "source_event_time": parsed_boundary_source[
                        "source_event_time"
                    ],
                    "market_version": boundary_entry.market_version,
                    "context_version": boundary_entry.context_version,
                    "evaluation_id": boundary_entry.evaluation_id,
                    "regime": boundary_entry.regime.value,
                    "action_type": boundary_entry.action_type,
                    "side": (
                        None
                        if boundary_entry.side is None
                        else boundary_entry.side.value
                    ),
                    "strategy": (
                        None
                        if boundary_entry.strategy is None
                        else boundary_entry.strategy.value
                    ),
                }
            )

        immutable_decision_fingerprint = {
            "source_event_id": parsed_market_source["source_event_id"],
            "source_kline_identity": parsed_market_source["source_kline_identity"],
            "source_event_time": parsed_market_source["source_event_time"],
            "evaluation_id": buy_trace.command_event_id,
            "market_version": budget.market_version,
            "account_version": budget.account_version,
            "context_version": budget.context_version,
            "policy_version": budget.policy_version,
            "regime": buy_trade.regime_type.value,
            "action_type": "SUBMIT_ORDER",
            "side": "BUY",
            "strategy": "CASE_C",
            "intent_id": buy_metadata["intent_id"],
            "client_order_id": buy_trade.client_order_id,
            "decision_price": _decimal_to_wire(buy_decision_price),
            "final_submitted_quantity": _decimal_to_wire(buy_submitted_quantity),
            "final_notional": _decimal_to_wire(buy_notional),
            "configured_cap": _decimal_to_wire(self.maximum_notional),
        }

        # Attempt sequence는 append-only journal order이며 result sequence는 같은 두 client ID의 terminal fact다.
        order_attempts: list[dict[str, object]] = []
        submit_time_filter_evidence: list[dict[str, object]] = []
        order_results: list[dict[str, object]] = []
        for sequence, trade in enumerate(ordered_trades, start=1):
            order_metadata = order_metadata_by_client_id[trade.client_order_id]
            order_trace = execution_traces_by_client_id[trade.client_order_id][0]
            decision_price = _metadata_decimal(
                order_metadata,
                "market_price_at_decision",
            )
            submitted_quantity = _metadata_decimal(
                order_metadata,
                "submitted_quantity",
            )
            requested_quantity = _metadata_decimal(
                order_metadata,
                "requested_quantity",
            )
            with localcontext() as decimal_context:
                decimal_context.prec = 34
                final_notional = decision_price * submitted_quantity
            policy_version = order_metadata.get("risk_policy_version")
            if type(policy_version) is not int or policy_version != budget.policy_version:
                raise AssertionError("actual order policy version changed within the session")
            filter_evidence = (
                self.runtime.api_gateway.get_order_preparation_filter_evidence(
                    trade.client_order_id
                )
            )
            submission_evidence = (
                self.runtime.api_gateway.get_order_submission_attempt_evidence(
                    trade.client_order_id
                )
            )
            if type(filter_evidence) is not OrderPreparationFilterEvidence:
                raise AssertionError("actual order lacks submit-time filter evidence")
            if type(submission_evidence) is not OrderSubmissionAttemptEvidence:
                raise AssertionError("actual order lacks submission-start evidence")
            if (
                filter_evidence.intent_id != order_metadata["intent_id"]
                or filter_evidence.client_order_id != trade.client_order_id
                or filter_evidence.side is not trade.side
                or filter_evidence.rules.symbol != order_metadata["symbol"]
                or filter_evidence.account_filters.symbol
                != order_metadata["symbol"]
                or filter_evidence.reference_price.symbol
                != order_metadata["symbol"]
                or submission_evidence.intent_id != order_metadata["intent_id"]
                or submission_evidence.client_order_id != trade.client_order_id
                or submission_evidence.side is not trade.side
            ):
                raise AssertionError("production provenance changed pending Order identity")

            # Pending requested 수량을 production과 같은 rule로 다시 준비해 최종 UPSERT claim을 독립 확인한다.
            recomputed_quantity = floor_market_quantity(
                requested_quantity,
                filter_evidence.rules,
            )
            if recomputed_quantity != submitted_quantity:
                raise AssertionError("submit-time rules do not reproduce pending final quantity")
            validate_market_notional(
                submitted_quantity,
                filter_evidence.reference_price.price,
                filter_evidence.rules,
            )
            validate_account_relevant_filters(
                submitted_quantity,
                filter_evidence.reference_price.price,
                filter_evidence.rules,
                filter_evidence.account_filters,
                side=filter_evidence.side,
                account_open_state_verified_empty=(
                    filter_evidence.account_open_state_verified_empty
                ),
            )
            if (
                trade.side is OrderSide.BUY
                and filter_evidence.rules.maximum_position is not None
            ):
                raise AssertionError("actual BUY submit-time rules contain MAX_POSITION")
            if not all(
                type(account_filter) is AccountAssetFilter
                for account_filter in filter_evidence.account_asset_filters
            ):
                raise AssertionError("submit-time account filters changed normalized type")
            if (
                not filter_evidence.account_open_state_verified_empty
                or filter_evidence.account_open_orders_observed_at is None
                or filter_evidence.account_open_order_lists_observed_at is None
            ):
                raise AssertionError(
                    "submit-time account open-state evidence is incomplete"
                )

            # Signed account filter와 두 empty snapshot은 REST POST 시작보다 늦게 생성될 수 없다.
            if max(
                filter_evidence.observed_at,
                filter_evidence.account_filters_observed_at,
                filter_evidence.account_open_orders_observed_at,
                filter_evidence.account_open_order_lists_observed_at,
                filter_evidence.reference_price_observed_at,
            ) > submission_evidence.attempted_at:
                raise AssertionError("submission started before its safety observations")
            submit_time_filter_evidence.append(
                _normalize_submit_time_filter_evidence(
                    filter_evidence,
                    sequence=sequence,
                )
            )
            order_attempts.append(
                {
                    "sequence": sequence,
                    "attempted_at": _datetime_to_wire(
                        submission_evidence.attempted_at
                    ),
                    "evaluation_id": order_trace.command_event_id,
                    "intent_id": order_metadata["intent_id"],
                    "client_order_id": trade.client_order_id,
                    "submission_attempt": order_metadata["submission_attempt"],
                    "symbol": order_metadata["symbol"],
                    "regime": order_metadata["regime_type"],
                    "strategy": order_metadata["strategy"],
                    "action_type": "SUBMIT_ORDER",
                    "side": order_metadata["side"],
                    "exit_reason": order_metadata["exit_reason"],
                    "decision_price": _decimal_to_wire(decision_price),
                    "final_submitted_quantity": _decimal_to_wire(
                        submitted_quantity
                    ),
                    "final_notional": _decimal_to_wire(final_notional),
                    "configured_cap": _decimal_to_wire(self.maximum_notional),
                    "policy_version": policy_version,
                }
            )

            # Every normalized fill remains attached to the one durable Trade produced for its terminal Order.
            exchange_result = results_by_client_id[trade.client_order_id]
            if exchange_result.status is not OrderStatus.FILLED or not exchange_result.fills:
                raise AssertionError("successful actual order requires normalized FILLED evidence")
            if exchange_result.exchange_order_id != trade.order_id:
                raise AssertionError("exchange and durable order identity changed")
            incremental_fills = []
            for fill in exchange_result.fills:
                incremental_fills.append(
                    {
                        "fill_id": fill.trade_id,
                        "durable_trade_id": trade.trade_id,
                        "event_time": _datetime_to_wire(fill.executed_at),
                        "price": _decimal_to_wire(fill.price),
                        "quantity": _decimal_to_wire(fill.quantity),
                        "quote_amount": _decimal_to_wire(fill.executed_amount),
                        "fee_amount": _decimal_to_wire(fill.fee_amount),
                        "fee_asset": fill.fee_asset,
                        "fee_quote_amount": _decimal_to_wire(
                            fill.fee_quote_amount
                        ),
                    }
                )
            latest_fill_time = max(
                fill.executed_at for fill in exchange_result.fills
            )
            if (
                submission_evidence.attempted_at > latest_fill_time
                or latest_fill_time > exchange_result.processed_at
            ):
                raise AssertionError("submission, fill and terminal result times are not causal")
            order_results.append(
                {
                    "sequence": sequence,
                    "observed_at": _datetime_to_wire(exchange_result.processed_at),
                    "intent_id": order_metadata["intent_id"],
                    "client_order_id": trade.client_order_id,
                    "exchange_order_id": exchange_result.exchange_order_id,
                    "status": exchange_result.status.value,
                    "failure_code": None,
                    "incremental_fills": incremental_fills,
                }
            )

        sell_metadata = order_metadata_by_client_id[sell_trade.client_order_id]
        submitted_sell_quantity = _metadata_decimal(
            sell_metadata,
            "submitted_quantity",
        )
        if submitted_sell_quantity != authoritative_position_quantity:
            raise AssertionError("STOP submitted quantity must equal the authoritative Position")
        if submitted_sell_quantity != effective_free_quantity:
            raise AssertionError("STOP submitted quantity must equal effective free exposure")
        durable_fresh_trades = tuple(fresh_trades)
        run_durable_trades, performance_evidence = _create_performance_evidence(
            baseline_trades=self.baseline_trades,
            total_trades=durable_fresh_trades,
            performance=fresh_performance,
            clock_time=final_verified_at,
        )
        if len(run_durable_trades) != 2:
            raise AssertionError("successful fresh history requires exactly two run Trades")
        if {
            run_trade["client_order_id"] for run_trade in run_durable_trades
        } != {buy_trade.client_order_id, sell_trade.client_order_id}:
            raise AssertionError("fresh run Trades do not match the two actual orders")

        return {
            "schema_version": PHASE13_PUBLIC_TRACE_SCHEMA_VERSION,
            "record_type": PHASE13_PUBLIC_TRACE_RECORD_TYPE,
            "outcome": "SUCCESS",
            "typed_reason": None,
            "run_id": self.run_id,
            "timestamps": {
                "started_at": _datetime_to_wire(self.started_at),
                "completed_at": _datetime_to_wire(self.evidence_clock()),
            },
            "preflight": dict(self.preflight),
            "immutable_decision_fingerprint": immutable_decision_fingerprint,
            "public_market_events": success_market_events,
            "public_account_events": self.public_account_events,
            "order_attempts": order_attempts,
            "order_execution_traces": order_execution_traces,
            "submit_time_filter_evidence": submit_time_filter_evidence,
            "order_results": order_results,
            "baseline_history_sha256": self.baseline_history_sha256,
            "baseline_history_count": len(self.baseline_trades),
            "run_durable_trades": run_durable_trades,
            "transport_ui_event_batch": _normalize_transport_ui_events(
                self.transport_event_dtos,
                transport_session_id=self.event_stream.session_id,
            ),
            "recovery": {
                "required": True,
                "attempted": True,
                "outcome": "SUCCESS",
                "intent_id": sell_metadata["intent_id"],
                "client_order_id": sell_trade.client_order_id,
                "authoritative_position_quantity": _decimal_to_wire(
                    authoritative_position_quantity
                ),
                "effective_free_quantity": _decimal_to_wire(
                    effective_free_quantity
                ),
                "submitted_quantity": _decimal_to_wire(
                    submitted_sell_quantity
                ),
                "final_position_quantity": "0",
                "pending_order_count": 0,
                "matching_open_order_count": 0,
                "duplicate_order_count": 0,
                "duplicate_trade_count": 0,
            },
            "final_state": {
                "position_quantity": "0",
                "pending_order_count": 0,
                "unknown_order_count": 0,
                "matching_open_order_count": 0,
                "actual_order_count": 2,
                "duplicate_order_count": 0,
                "duplicate_trade_count": 0,
                "verified_at": _datetime_to_wire(final_verified_at),
                "fresh_runtime_session_id": fresh_runtime_session_id,
                "performance": performance_evidence,
            },
        }

    def test_actual_public_market_case2_buy_and_exact_stop_recovery(self) -> None:
        """
        함수 이름: test_actual_public_market_case2_buy_and_exact_stop_recovery()
        기능: actual orchestration의 모든 정상 예외를 submit 차단·sealed failure evidence 뒤 원형대로 전파한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        try:
            self._execute_actual_public_market_case2()
        except _PhaseThirteenRecordedOutcomeFailure:
            raise  # NO_SIGNAL은 이미 full normalized trace를 fsync했으므로 이중 FAILED를 만들지 않는다.
        except Exception as original_error:
            try:
                self._record_actual_failure_evidence(original_error)
            except Exception as evidence_error:
                original_error.add_note(
                    "Phase 13 FAILED evidence finalization failed with typed class "
                    f"{type(evidence_error).__name__}; artifacts="
                    f"{self.artifact_directory}"
                )
            raise  # Bare raise로 최초 traceback, type과 message를 교체하지 않는다.

    def _execute_actual_public_market_case2(self) -> None:
        """
        함수 이름: _execute_actual_public_market_case2()
        기능: public stream→Case C BUY→durable publication→public STOP SELL→fresh zero exposure를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        # Production startup의 READY는 market/account REST와 두 signed/public stream이 모두 성립한 뒤다.
        ready_state = start_application(self.runtime)
        self.assertIs(ready_state.status, ApplicationStatus.READY)
        self.assertTrue(self.runtime.market_snapshot.ready)
        self.assertTrue(self.runtime.account.ready)
        self.assertTrue(self.runtime.market_data_controller.market_available)
        self.assertTrue(self.runtime.web_socket_gateway.kline_live_ready)
        self.assertTrue(self.runtime.web_socket_gateway.account_ready)
        self.public_account_events.append(
            _create_account_event_from_runtime(
                self.runtime,
                run_id=self.run_id,
                sequence=1,
                clock=self.evidence_clock,
            )
        )
        self.last_market_version = self.runtime.market_snapshot.version

        # Fresh preflight는 local replay, pending, Position과 모든 client ID의 ETHUSDT open order가 zero다.
        controller = self.runtime.trading_controller
        position = controller.position
        self.assertIsNotNone(position)
        _require_zero_position_quantity(position.quantity)
        pending_orders = (
            self.runtime.trade_history_controller.get_pending_orders()
        )
        if pending_orders:
            raise AssertionError(
                "actual preflight requires zero pending orders"
            )
        if tuple(self.baseline_trades) != self.runtime.trade_history.trades:
            raise AssertionError(
                "actual preflight history differs from verified baseline"
            )
        open_results = self.runtime.api_gateway.list_all_open_order_results(
            "ETHUSDT"
        )
        require_empty_all_client_open_orders(open_results)
        baseline_recent_results = (
            self.runtime.api_gateway.list_all_recent_order_results(
                "ETHUSDT",
                limit=1000,
            )
        )
        self.baseline_recent_exchange_order_ids = (
            verify_exact_recent_order_baseline(
                self.baseline_trades,
                baseline_recent_results,
            )
        )  # Verified Trade로 설명할 수 없는 manual·외부 recent order는 mutation 전에 차단한다.
        cause_snapshot = controller.reconciliation_cause_snapshot
        if cause_snapshot.reconciliation_required:
            worker = self.runtime._trading_event_runtime_worker
            worker_failure_snapshot = (
                None if worker is None else worker.failure_snapshot
            )
            market_failure_snapshot = (
                _read_phase13_market_failure_snapshot(self.runtime)
                if cause_snapshot.category
                is ReconciliationCauseCategory.MARKET_STREAM_FAILED
                else None
            )
            raise _PhaseThirteenReconciliationFailure(
                cause_snapshot,
                worker_failure_snapshot=worker_failure_snapshot,
                market_failure_snapshot=market_failure_snapshot,
            )  # 주문 전 blocker도 최초 원인과 raw 없는 stream/worker 진단을 같은 예외에 결속한다.
        commission_policy = self.runtime.api_gateway.fetch_commission_discount_policy(
            "ETHUSDT"
        )
        _require_zero_market_buy_commission(commission_policy)
        self.assertFalse(commission_policy.can_charge_discount_asset)

        # Signed account filter와 public symbol rule을 순서대로 fresh 관찰해 full evaluator input을 고정한다.
        account_filters = (
            self.runtime.api_gateway.fetch_account_relevant_filters(
                "ETHUSDT"
            )
        )
        account_filters_observed_at = self.evidence_clock()
        self.assertIs(type(account_filters), AccountRelevantFilters)
        self.assertEqual("ETHUSDT", account_filters.symbol)
        self.assertTrue(
            all(
                type(account_filter) is AccountAssetFilter
                for account_filter in account_filters.asset_filters
            )
        )
        symbol_rules = self.runtime.api_gateway.fetch_symbol_trading_rules(
            "ETHUSDT"
        )
        filters_observed_at = self.evidence_clock()

        # EXCHANGE_* count와 account 격리를 symbol 생략 signed snapshot 두 개의 exact empty로 증명한다.
        if self.runtime.api_gateway.has_any_exchange_open_orders():
            raise AssertionError(
                "Phase 13 preflight requires zero exchange-wide open orders"
            )
        account_open_orders_observed_at = self.evidence_clock()
        if self.runtime.api_gateway.has_any_exchange_open_order_lists():
            raise AssertionError(
                "Phase 13 preflight requires zero exchange-wide open order lists"
            )
        account_open_order_lists_observed_at = self.evidence_clock()
        reference_price = self.runtime.api_gateway.fetch_reference_price(
            "ETHUSDT"
        )
        reference_price_observed_at = self.evidence_clock()
        self.assertEqual("ETHUSDT", symbol_rules.symbol)
        self.assertEqual("TRADING", symbol_rules.status)
        self.assertTrue(symbol_rules.is_spot_trading_allowed)
        self.assertIn("MARKET", symbol_rules.order_types)
        self.assertIs(type(reference_price), ReferencePrice)
        self.assertEqual("ETHUSDT", reference_price.symbol)
        if reference_price.price <= Decimal("0"):
            raise AssertionError(
                "Phase 13 reference price must be positive"
            )  # 실제 reference price 대신 고정 문장만 출력한다.

        # Precision quantum을 포함한 최소 양수 BUY 후보로 quote/MAX_POSITION/count blocker를 actual child 전에 평가한다.
        minimum_candidate_quantity = max(
            Decimal("1").scaleb(-symbol_rules.base_asset_precision),
            symbol_rules.lot_size.minimum_quantity,
            symbol_rules.market_lot_size.minimum_quantity,
            symbol_rules.lot_size.step_size,
            symbol_rules.market_lot_size.step_size,
        )
        validate_account_relevant_filters(
            minimum_candidate_quantity,
            reference_price.price,
            symbol_rules,
            account_filters,
            side=OrderSide.BUY,
            account_open_state_verified_empty=True,
        )
        self.preflight = _create_preflight_evidence(
            account_version=self.runtime.account.version,
            verified_at=self.evidence_clock(),
            commission_policy=commission_policy,
            symbol_rules=symbol_rules,
            filters_observed_at=filters_observed_at,
            account_filters=account_filters,
            account_filters_observed_at=account_filters_observed_at,
            account_open_orders_observed_at=(
                account_open_orders_observed_at
            ),
            account_open_order_lists_observed_at=(
                account_open_order_lists_observed_at
            ),
            reference_price=reference_price,
            reference_price_observed_at=reference_price_observed_at,
        )

        # 진행 중 live callback을 먼저 끝내 candidate snapshot과 actual fixture 수명을 하나로 고정한다.
        market_delivery_guard = (
            _acquire_deterministic_kline_delivery_guard(
                self.runtime.web_socket_gateway
            )
        )
        self.addCleanup(
            _release_deterministic_kline_delivery_guard,
            market_delivery_guard,
        )  # TearDown의 close 뒤 guard를 풀어 queued callback이 stale generation만 관찰하게 한다.
        deterministic_klines = create_deterministic_public_case2_klines(
            self.runtime.market_snapshot.get_snapshot()
        )
        quote_balance = self.runtime.account.balances.get(
            symbol_rules.quote_asset
        )
        if quote_balance is None:
            raise AssertionError(
                "actual preflight requires a free quote balance"
            )
        buy_candidate = _prepare_deterministic_buy_candidate(
            free_quote_quantity=quote_balance.free,
            decision_price=deterministic_klines.recovery.close,
            maximum_notional=self.maximum_notional,
            symbol_rules=symbol_rules,
            account_filters=account_filters,
            reference_price=reference_price,
        )

        # TYPE_0과 candidate split은 public optimistic-version command만 사용하고 outer cap은 그대로 유지한다.
        selection = self.runtime.regime_controller.set_regime_type(
            RegimeType.TYPE_0,
            command_id=f"phase13-select-{self.run_id}",
            expected_version=controller.context.version,
        )
        split_result = controller.update_split_ratios(
            command_id=f"phase13-split-{self.run_id}",
            expected_version=selection.version,
            scale_in=buy_candidate.scale_in,
            scale_out=Decimal("1"),
        )
        session_result = controller.start_trading(
            command_id=f"phase13-start-{self.run_id}",
            expected_version=split_result.version,
        )
        self.assertIs(session_result.status, TradingSessionStatus.RUNNING)

        # Preflight와 같은 세 public Kline만 넣고 production builder·STM이 BUY를 만들게 한다.
        buy_trade = self._inject_deterministic_public_case2_and_wait_for_buy(
            deterministic_klines
        )
        if buy_trade is None:
            self._record_non_signal_and_fail()
        self.assertIs(buy_trade.side, OrderSide.BUY)
        self.assertIs(buy_trade.regime_type, RegimeType.TYPE_0)
        risk_decision = controller.last_risk_decision
        self.assertIsNotNone(risk_decision)
        self.assertTrue(risk_decision.allowed)
        self.assertEqual(13, risk_decision.budget.policy_version)
        if (
            risk_decision.budget.candidate_order_notional
            > self.maximum_notional
        ):
            raise AssertionError(
                "actual BUY candidate exceeds approved notional cap"
            )  # 후보 notional과 승인 상한의 Decimal repr를 숨긴다.
        authoritative_position_quantity = position.quantity
        if authoritative_position_quantity <= Decimal("0"):
            raise AssertionError(
                "actual BUY did not create a positive Position"
            )
        if buy_trade.executed_quantity != authoritative_position_quantity:
            raise AssertionError(
                "actual BUY fill does not match authoritative Position"
            )  # BUY fill과 Position 수량은 고정 문장 뒤에 숨긴다.
        effective_free_quantity = self._wait_for_effective_free_eth(
            authoritative_position_quantity
        )

        # Public STOP은 이번 BUY가 연 exact Position과 확인된 free ETH의 작은 값만 recovery 대상으로 사용한다.
        stop_result = controller.stop_trading(
            command_id=f"phase13-stop-{self.run_id}",
            expected_version=controller.context.version,
        )
        self.assertIn(
            stop_result.status,
            (
                TradingSessionStatus.STOPPING,
                TradingSessionStatus.TERMINATED,
            ),
        )
        self._wait_for_session_termination()
        self._collect_runtime_observations()
        self.assertIs(controller.status, TradingSessionStatus.TERMINATED)
        if position.quantity != Decimal("0"):
            raise AssertionError(
                "actual completion requires zero Position"
            )  # 잔여 Position 수량을 unittest failure repr에 반사하지 않는다.
        if self.runtime.trade_history_controller.get_pending_orders():
            raise AssertionError(
                "actual completion requires zero pending orders"
            )

        # Normal Testnet MARKET fill에서는 이번 run이 정확히 BUY 하나와 STOP SELL 하나로 끝나야 한다.
        run_trades = self.runtime.trade_history.trades[len(self.baseline_trades) :]
        self.assertEqual(2, len(run_trades))
        self.assertEqual((OrderSide.BUY, OrderSide.SELL), tuple(trade.side for trade in run_trades))
        sell_trade = run_trades[1]
        self.assertEqual("STOP", sell_trade.exit_reason.value)
        if sell_trade.executed_quantity != authoritative_position_quantity:
            raise AssertionError(
                "STOP SELL fill does not match the authoritative Position"
            )
        if effective_free_quantity < sell_trade.executed_quantity:
            raise AssertionError(
                "STOP SELL fill exceeds the verified effective free quantity"
            )  # SELL fill과 free balance 수량은 고정 문장 뒤에 숨긴다.
        pending_upserts = _extract_pending_order_upserts(
            self.runtime.trade_history_repository.pending_order_storage_path
        )
        self.assertEqual(2, len(pending_upserts))
        upserts_by_client_id = {
            str(order_metadata["client_order_id"]): order_metadata
            for order_metadata in pending_upserts
        }
        expected_trade_client_ids = {
            buy_trade.client_order_id,
            sell_trade.client_order_id,
        }
        if set(upserts_by_client_id) != expected_trade_client_ids:
            raise AssertionError(
                "actual pending upserts do not match durable trade identity"
            )  # 실제 client ID set은 unittest failure repr에 반사하지 않는다.
        _assert_atomic_trade_publications(self.transport_event_dtos)

        # First runtime을 닫은 뒤 같은 history와 read-only permission의 fresh runtime으로 최종 exposure를 증명한다.
        closed_state = close_application(self.runtime)
        self.assertIs(closed_state.status, ApplicationStatus.CLOSED)
        run_client_order_ids = frozenset(upserts_by_client_id)
        (
            fresh_trades,
            fresh_performance,
            matching_recent_results,
            fresh_runtime_session_id,
            final_verified_at,
        ) = self._fresh_read_only_verification(run_client_order_ids)
        self.assertEqual(2, len(matching_recent_results))
        self.assertTrue(
            all(result.status is OrderStatus.FILLED for result in matching_recent_results)
        )

        # 모든 cross-check가 끝난 뒤에만 secret-free normalized success trace와 canonical digest를 기록한다.
        trace_body = self._create_success_trace_body(
            buy_trade=buy_trade,
            sell_trade=sell_trade,
            upserts_by_client_id=upserts_by_client_id,
            matching_recent_results=matching_recent_results,
            fresh_trades=fresh_trades,
            fresh_performance=fresh_performance,
            fresh_runtime_session_id=fresh_runtime_session_id,
            final_verified_at=final_verified_at,
            risk_decision=risk_decision,
            authoritative_position_quantity=authoritative_position_quantity,
            effective_free_quantity=effective_free_quantity,
        )
        trace_path, trace_digest = _write_trace_artifact(
            self.artifact_directory,
            trace_body,
            forbidden_values=(
                self.configuration.api_key,
                self.configuration.api_secret,
            ),
        )
        self.assertTrue(trace_path.is_file())
        self.assertEqual(64, len(trace_digest))  # Digest만 stdout-safe assertion에 남기고 trace body는 출력하지 않는다.


def load_tests(
    loader: unittest.TestLoader,
    standard_tests: unittest.TestSuite,
    pattern: str | None,
) -> unittest.TestSuite:
    """
    함수 이름: load_tests()
    기능: Network-free harness 회귀 전체를 actual mutation TestCase보다 먼저 실행한다.
    인자: loader -> unittest가 전달한 module loader
        standard_tests -> default 이름 정렬 suite; 안전 순서를 위해 사용하지 않음
        pattern -> discovery pattern 또는 direct module 실행의 None
    반환값: helper suite 다음 exact actual suite가 오는 ordered TestSuite
    작성 날짜: 2026/08/31
    """
    del standard_tests, pattern  # Default alphabetical class ordering은 actual-first가 될 수 있어 폐기한다.

    # 두 class를 명시 순서로 추가해 standalone secure runner에서도 local gate를 먼저 완료한다.
    ordered_suite = unittest.TestSuite()
    ordered_suite.addTest(
        loader.loadTestsFromTestCase(PhaseThirteenPublicHarnessHelperTests)
    )
    ordered_suite.addTest(
        loader.loadTestsFromTestCase(
            BinanceTestnetPhaseThirteenPublicMarketCase2Tests
        )
    )
    return ordered_suite  # Actual opt-in 여부와 무관하게 helper failure가 먼저 process를 실패시킨다.
