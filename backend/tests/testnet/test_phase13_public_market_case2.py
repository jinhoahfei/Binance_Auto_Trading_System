"""실제 public market 경계에서 Phase 13 Spot Testnet Case 2를 검증한다."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import fcntl
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
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

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
    floor_market_quantity,
    validate_account_relevant_filters,
    validate_market_notional,
)
from binance_auto_trader.application import TradingSessionStatus
from binance_auto_trader.bootstrap import (
    ApplicationRuntime,
    ApplicationStatus,
    TestnetConfiguration,
    close_application,
    create_testnet_application_runtime,
    load_testnet_configuration,
    require_phase13_public_case2_permission,
    start_application,
)
from binance_auto_trader.bootstrap.testnet import (
    BINANCE_RUN_PHASE13_PUBLIC_CASE2_ENV,
    BINANCE_RUN_TESTNET_ENV,
    BINANCE_RUN_TESTNET_ORDERS_ENV,
    BINANCE_TESTNET_API_KEY_ENV,
    BINANCE_TESTNET_API_SECRET_ENV,
    BINANCE_TESTNET_MAX_NOTIONAL_ENV,
)
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.history import Performance, Trade
from binance_auto_trader.domain.market import Kline
from binance_auto_trader.domain.trading import (
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
    canonical_actual_phase13_public_trace_bytes,
    seal_actual_phase13_public_trace,
    seal_phase13_public_trace,
    validate_actual_phase13_public_trace,
)
from tests.testnet._support import (
    PHASE13_PUBLIC_CASE2_REQUESTED,
    PHASE13_PUBLIC_CASE2_SKIP_REASON,
    require_empty_all_client_open_orders,
    seed_verified_closed_history,
    verify_exact_recent_order_baseline,
)
from tests.unit.history.factories import make_trade


# 실제 주문 target은 자연 market signal과 same-ID reconciliation을 기다리되 무한 대기는 허용하지 않는다.
_PUBLIC_SIGNAL_TIMEOUT_SECONDS = 180
_ORDER_SETTLEMENT_TIMEOUT_SECONDS = 60
_ACCOUNT_SETTLEMENT_TIMEOUT_SECONDS = 15
_POLL_INTERVAL_SECONDS = 0.25
_MAXIMUM_RECORDED_MARKET_EVENTS = 512
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
_KLINE_SOURCE_PATTERN = re.compile(
    r"kline:(?P<symbol>[A-Z0-9]+):(?P<interval>[0-9A-Za-z]+):"
    r"(?P<open_time>[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z):"
    r"(?P<event_time>[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z):"
    r"(?P<close_state>open|closed)\Z"
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
_PHASE13_FAILURE_EVIDENCE_SCHEMA_VERSION = 1
_PHASE13_FAILURE_EVIDENCE_RECORD_TYPE = (
    "phase13_public_case2_testnet_failure_evidence"
)
_FAILURE_EVIDENCE_BODY_FIELDS = frozenset(
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
_FAILURE_EVIDENCE_DOCUMENT_FIELDS = _FAILURE_EVIDENCE_BODY_FIELDS | {
    "failure_sha256"
}
_FAILURE_MUTATION_GUARD_FIELDS = frozenset(
    {"mutation_started", "submissions_blocked", "submission_attempts"}
)
_FAILURE_RECOVERY_FIELDS = frozenset(
    {"attempted", "outcome", "typed_reason"}
)
_FAILURE_SUBMISSION_ATTEMPT_FIELDS = frozenset(
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
_FAILURE_FRESH_VERIFICATION_FIELDS = frozenset(
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


class _StrictJsonError(ValueError):
    """
    클래스 이름: _StrictJsonError
    기능: test-only pending journal parser가 중복 key 또는 비표준 상수를 발견했음을 나타낸다.
    작성 날짜: 2026/08/31
    """


class _PhaseThirteenRecordedOutcomeFailure(AssertionError):
    """
    클래스 이름: _PhaseThirteenRecordedOutcomeFailure
    기능: NO_SIGNAL/BLOCKED trace가 이미 durable하게 기록된 의도된 test failure를 구분한다.
    작성 날짜: 2026/08/31
    """


def _assert_complete_success_order_trace(
    message_ids: Sequence[str],
    *,
    side: OrderSide,
) -> None:
    """
    함수 이름: _assert_complete_success_order_trace()
    기능: 성공 주문의 고정 prefix·same-ID query/fill branch·durable suffix를 exact grammar로 검증한다.
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
        if (
            tuple(
                branch_ids[
                    branch_cursor : branch_cursor + len(_SAME_ORDER_QUERY_TRACE)
                ]
            )
            != _SAME_ORDER_QUERY_TRACE
        ):
            raise AssertionError("successful order trace contains an illegal branch")
        branch_cursor += len(_SAME_ORDER_QUERY_TRACE)
        query_count += 1
        if query_count > 4:
            raise AssertionError("successful order trace exceeds the query budget")
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
            continue

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


def _utc_now() -> datetime:
    """
    함수 이름: _utc_now()
    기능: trace와 bounded observation에 사용할 timezone-aware UTC 현재 시각을 반환한다.
    인자: 없음
    반환값: timezone.utc 기반 현재 datetime
    작성 날짜: 2026/08/31
    """
    return datetime.now(timezone.utc)  # 로컬 timezone이나 naive 시각이 evidence에 섞이지 않게 한다.


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


def _parse_public_market_command_event(
    command_event_id: str,
) -> dict[str, object]:
    """
    함수 이름: _parse_public_market_command_event()
    기능: production order trace의 public market event ID에서 version과 Kline provenance를 추출한다.
    인자: command_event_id -> TradingController가 기록한 원 event identity
    반환값: source event, 대표 Kline identity/time과 market version mapping
    작성 날짜: 2026/08/31
    """
    if not isinstance(command_event_id, str) or not command_event_id:
        raise TypeError("command_event_id must be a non-empty string")
    command_parts = command_event_id.split(":", 2)
    if len(command_parts) != 3 or command_parts[0] != "market":
        raise ValueError("order trace is not rooted in a public market event")
    try:
        market_version = int(command_parts[1])
    except ValueError as error:
        raise ValueError("public market event has an invalid version") from error
    if market_version < 1 or str(market_version) != command_parts[1]:
        raise ValueError("public market event version must be canonical and positive")

    # Atomic boundary source는 `kline-batch|...`이며 각 component를 독립 exact parser로 검증한다.
    source_event_id = command_parts[2]
    source_components = (
        source_event_id.split("|")[1:]
        if source_event_id.startswith("kline-batch|")
        else [source_event_id]
    )
    if not source_components or any(not component for component in source_components):
        raise ValueError("public market source list must not be empty")
    parsed_components = []
    for source_component in source_components:
        component_match = _KLINE_SOURCE_PATTERN.fullmatch(source_component)
        if component_match is None:
            raise ValueError("public market source has an invalid Kline identity")
        parsed_components.append(component_match.groupdict())

    # Case 2 계산의 primary 30분봉이 batch에 있으면 그것을 선택하고 없으면 유일/첫 source를 보존한다.
    selected_component = next(
        (
            component
            for component in parsed_components
            if component["interval"] == "30m"
        ),
        parsed_components[0],
    )
    return {
        "source_event_id": source_event_id,
        "source_kline_identity": (
            f"{selected_component['symbol']}:"
            f"{selected_component['interval']}:"
            f"{selected_component['open_time']}"
        ),
        "source_event_time": selected_component["event_time"],
        "market_version": market_version,
    }  # Raw WebSocket payload 대신 production이 이미 정규화한 source identity만 남긴다.


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
    기능: actual order가 0인 NO_SIGNAL 또는 pre-submit BLOCKED run을 baseline·fresh state와 결속한다.
    인자: outcome -> NO_SIGNAL 또는 BLOCKED
        typed_reason -> BLOCKED typed reason 또는 NO_SIGNAL의 None
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
    if outcome not in {"NO_SIGNAL", "BLOCKED"}:
        raise ValueError("non-mutating trace outcome must be NO_SIGNAL or BLOCKED")
    if (outcome == "NO_SIGNAL") != (typed_reason is None):
        raise ValueError("only BLOCKED requires a typed reason")
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

    # 세 사용자 policy cap은 None을 보존하고 Testnet 100 USDT 상한은 bootstrap/permission 경계만 소유한다.
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
    if set(trace_body) != _FAILURE_EVIDENCE_BODY_FIELDS:
        raise ValueError("failure evidence body fields are not exact")
    if (
        trace_body["schema_version"] != _PHASE13_FAILURE_EVIDENCE_SCHEMA_VERSION
        or trace_body["record_type"] != _PHASE13_FAILURE_EVIDENCE_RECORD_TYPE
        or trace_body["outcome"] != "FAILED"
    ):
        raise ValueError("failure evidence identity is invalid")
    typed_reason = trace_body["typed_reason"]
    if (
        not isinstance(typed_reason, str)
        or _FAILURE_TOKEN_PATTERN.fullmatch(typed_reason) is None
    ):
        raise ValueError("failure typed_reason must be a canonical token")
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
    for sequence, attempt in enumerate(attempts, start=1):
        if (
            not isinstance(attempt, Mapping)
            or set(attempt) != _FAILURE_SUBMISSION_ATTEMPT_FIELDS
            or attempt["sequence"] != sequence
            or attempt["symbol"] != "ETHUSDT"
            or attempt["order_type"] != "MARKET"
            or attempt["side"] not in {"BUY", "SELL"}
            or type(attempt["submission_attempt"]) is not int
            or attempt["submission_attempt"] != 0
        ):
            raise ValueError("failure submission attempt is invalid")
        for field_name in ("intent_id", "client_order_id"):
            field_value = attempt[field_name]
            if (
                not isinstance(field_value, str)
                or _FAILURE_SAFE_TEXT_PATTERN.fullmatch(field_value) is None
            ):
                raise ValueError("failure submission identity is invalid")
        _require_failure_timestamp(attempt["attempted_at"], "attempted_at")
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
        or recovery["outcome"]
        not in {"NOT_REQUIRED", "SKIPPED", "SUCCESS", "FAILED"}
    ):
        raise ValueError("failure recovery evidence is invalid")
    recovery_reason = recovery["typed_reason"]
    if recovery_reason is not None and (
        not isinstance(recovery_reason, str)
        or _FAILURE_TOKEN_PATTERN.fullmatch(recovery_reason) is None
    ):
        raise ValueError("failure recovery reason must be a canonical token")
    if recovery["outcome"] in {"NOT_REQUIRED", "SUCCESS"}:
        if recovery_reason is not None:
            raise ValueError("completed failure recovery must not have a reason")
    elif recovery_reason is None:
        raise ValueError("skipped or failed recovery requires a typed reason")
    if recovery["attempted"] != (
        recovery["outcome"] in {"SUCCESS", "FAILED"}
    ):
        raise ValueError("failure recovery attempted flag is inconsistent")
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
    runtime_reconciliation = runtime_state["reconciliation_required"]
    if runtime_reconciliation is not None and type(runtime_reconciliation) is not bool:
        raise TypeError("failure reconciliation flag must be bool or None")
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

    fresh_verification = trace_body["fresh_verification"]
    if not isinstance(fresh_verification, Mapping) or set(fresh_verification) != (
        _FAILURE_FRESH_VERIFICATION_FIELDS
    ):
        raise ValueError("failure fresh verification fields are not exact")
    if fresh_verification["status"] not in {"VERIFIED", "INCOMPLETE"}:
        raise ValueError("failure fresh verification status is invalid")
    fresh_reason = fresh_verification["typed_reason"]
    if (fresh_verification["status"] == "VERIFIED") != (fresh_reason is None):
        raise ValueError("failure fresh status and reason are inconsistent")
    if fresh_reason is not None and (
        not isinstance(fresh_reason, str)
        or _FAILURE_TOKEN_PATTERN.fullmatch(fresh_reason) is None
    ):
        raise ValueError("failure fresh typed reason is invalid")
    verified_at = fresh_verification["verified_at"]
    if verified_at is not None:
        _require_failure_timestamp(verified_at, "fresh verified_at")
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
    reconciliation_required = fresh_verification["reconciliation_required"]
    if reconciliation_required is not None and type(reconciliation_required) is not bool:
        raise TypeError("failure fresh reconciliation flag must be bool or None")
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
    if not isinstance(trace_body["evidence_errors"], list) or any(
        not isinstance(error_code, str)
        or _FAILURE_TOKEN_PATTERN.fullmatch(error_code) is None
        for error_code in trace_body["evidence_errors"]
    ):
        raise ValueError("failure evidence errors must be typed tokens")


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
    if set(sealed_trace) != _FAILURE_EVIDENCE_DOCUMENT_FIELDS:
        raise AssertionError("sealed failure evidence fields changed unexpectedly")
    canonical_bytes = _canonical_failure_evidence_bytes(sealed_trace)
    _reject_failure_secret_material(
        sealed_trace,
        normalized_forbidden_values,
    )

    return sealed_trace, canonical_bytes, failure_digest


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
    finally:
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        if temporary_exists:
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass  # Publish 전후 다른 cleanup 경로가 이미 이름을 회수했으면 멱등 종료한다.
        os.close(directory_descriptor)

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
    close_state = "closed" if kline.closed else "open"

    return (
        f"kline:{kline.symbol}:{kline.interval.value}:"
        f"{_datetime_to_wire(kline.open_time)}:"
        f"{_datetime_to_wire(kline.event_time)}:{close_state}"
    )  # OHLCV나 raw frame은 identity에 포함하지 않아 public provenance만 기록한다.


def _create_observed_market_event(
    runtime: ApplicationRuntime,
    *,
    sequence: int,
) -> dict[str, object] | None:
    """
    함수 이름: _create_observed_market_event()
    기능: 최신 MarketSnapshot version을 만든 public source Kline을 non-signal trace event로 정규화한다.
    인자: runtime -> 실제 public market runtime
        sequence -> trace public market event 순서
    반환값: source가 있는 normalized event 또는 REST-only snapshot이면 None
    작성 날짜: 2026/08/31
    """
    if not isinstance(runtime, ApplicationRuntime):
        raise TypeError("runtime must be an ApplicationRuntime")
    if type(sequence) is not int or sequence < 1:
        raise ValueError("sequence must be a positive integer")
    source_klines = runtime.market_snapshot.update_source_klines
    if not source_klines:
        return None  # Startup REST merge는 public WebSocket signal 관찰로 승격하지 않는다.

    # Atomic boundary는 production과 동일한 component 순서를 유지하고 대표 30분 source를 parser로 선택한다.
    source_event_id = (
        _kline_source_component(source_klines[0])
        if len(source_klines) == 1
        else "kline-batch|"
        + "|".join(_kline_source_component(kline) for kline in source_klines)
    )
    parsed_source = _parse_public_market_command_event(
        f"market:{runtime.market_snapshot.version}:{source_event_id}"
    )
    return {
        "sequence": sequence,
        "message_id": "1L.1",
        "event_type": "KLINE_OBSERVED",
        "source_event_id": parsed_source["source_event_id"],
        "source_kline_identity": parsed_source["source_kline_identity"],
        "source_event_time": parsed_source["source_event_time"],
        "market_version": parsed_source["market_version"],
        "context_version": runtime.trading_controller.context.version,
        "evaluation_id": None,
        "regime": "TYPE_0",
        "action_type": None,
        "side": None,
        "strategy": None,
    }


def _create_account_event_from_runtime(
    runtime: ApplicationRuntime,
    *,
    run_id: str,
    sequence: int,
) -> dict[str, object]:
    """
    함수 이름: _create_account_event_from_runtime()
    기능: startup REST로 적용된 authoritative ETH balance/version을 secret-free public account event로 만든다.
    인자: runtime -> account READY actual Testnet runtime
        run_id -> source identity를 다른 run과 구분할 UUIDv4 문자열
        sequence -> trace public account event 순서
    반환값: exact account event dictionary
    작성 날짜: 2026/08/31
    """
    if not isinstance(runtime, ApplicationRuntime):
        raise TypeError("runtime must be an ApplicationRuntime")
    if type(sequence) is not int or sequence < 1:
        raise ValueError("sequence must be a positive integer")
    account = runtime.account
    if not account.ready or account.updated_at is None or account.version < 1:
        raise RuntimeError("account must be ready before trace capture")
    eth_balance = account.balances.get("ETH")
    free_quantity = Decimal("0") if eth_balance is None else eth_balance.free
    locked_quantity = Decimal("0") if eth_balance is None else eth_balance.locked

    # 전체 account payload 대신 recovery에 필요한 ETH absolute balance와 version만 보존한다.
    return {
        "sequence": sequence,
        "message_id": "2",
        "event_type": "ACCOUNT_SNAPSHOT_APPLIED",
        "source_event_id": f"startup-account-{run_id}-{account.version}",
        "source_event_time": _datetime_to_wire(_utc_now()),
        "account_version": account.version,
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
    observed_versions = {event["account_version"] for event in account_events}

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
        if account_version in observed_versions:
            continue  # 같은 absolute Account version을 여러 transport 관찰로 부풀리지 않는다.
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
        observed_versions.add(account_version)


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


class PhaseThirteenPublicHarnessHelperTests(unittest.TestCase):
    """
    클래스 이름: PhaseThirteenPublicHarnessHelperTests
    기능: network 없이 actual harness의 source, journal, UI와 NO_SIGNAL evidence 경계를 검증한다.
    작성 날짜: 2026/08/31
    """

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
            "kline:ETHUSDT:1m:2026-08-31T00:00:00Z:2026-08-31T00:01:00Z:closed|"
            "kline:ETHUSDT:30m:2026-08-31T00:00:00Z:2026-08-31T00:01:01Z:open"
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
            "2026-08-31T00:01:01Z",
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
        기능: envelope DTO만으로 ORDER/PERFORMANCE pair와 aggregate별 version을 보존하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
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
        policy = _create_actual_risk_policy(Decimal("100"))

        # 사용자 확정 세 cap의 None을 Testnet execution cap 100으로 덮지 않고 recovery behavior도 결속한다.
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
        기능: immediate·query·partial 성공을 허용하고 누락·중복·불법 branch·예산 초과를 거부한다.
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
            account_events=[],
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
                        "intent_id": "intent-1",
                        "submission_attempt": 0,
                        "attempted_at": _datetime_to_wire(observed_at),
                        "client_order_id": "bat-p13-client-1",
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
                "typed_reason": "FRESH_VERIFICATION_INCOMPLETE",
                "verified_at": None,
                "position_quantity": None,
                "pending_order_count": None,
                "reconciliation_required": None,
                "matching_open_order_count": None,
                "run_exchange_order_count": None,
                "durable_trade_count": None,
            },
            "evidence_errors": ["FRESH_VERIFICATION_FAILED"],
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
                "intent_id": "intent-2",
                "client_order_id": "bat-p13-client-2",
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
                "verified_at": _datetime_to_wire(observed_at),
                "position_quantity": "0",
                "pending_order_count": 0,
                "reconciliation_required": False,
                "matching_open_order_count": 0,
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
        contradictory_not_required_body = json.loads(json.dumps(failure_body))
        contradictory_not_required_body["recovery"] = {
            "attempted": False,
            "outcome": "NOT_REQUIRED",
            "typed_reason": None,
        }

        # Completed claim의 nonzero/UNKNOWN facts와 timestamp 없는 VERIFIED는 각각 독립 거부한다.
        for case_name, invalid_body in (
            ("contradictory-success", contradictory_success_body),
            ("missing-verified-at", missing_verified_at_body),
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

        # 실제 canary가 identifier field에 섞이면 publish bytes 생성 전 재귀 scan이 거부한다.
        secret_body = json.loads(_canonical_failure_evidence_bytes(failure_body))
        secret_body["mutation_guard"]["submission_attempts"][0][
            "intent_id"
        ] = redaction_canaries[1]
        with self.assertRaises(ValueError):
            _seal_failure_evidence(
                secret_body,
                forbidden_values=redaction_canaries,
            )

    def test_actual_failure_finalizer_recovers_then_blocks_and_preserves_error(
        self,
    ) -> None:
        """
        함수 이름: test_actual_failure_finalizer_recovers_then_blocks_and_preserves_error()
        기능: recovery 판정 뒤 제출·scheduler를 닫고 원 TimeoutError에 sealed path만 추가하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        observed_at = _utc_now()
        submission_attempt = Phase13OrderSubmissionAttempt(
            symbol="ETHUSDT",
            side=OrderSide.BUY,
            order_type="MARKET",
            intent_id="intent-1",
            submission_attempt=0,
            attempted_at=observed_at,
            client_order_id="bat-p13-client-1",
        )
        guard_snapshot = Phase13OrderSubmissionGuardSnapshot(
            mutation_started=True,
            submissions_blocked=True,
            attempts=(submission_attempt,),
        )
        gateway = Mock()
        gateway.get_phase13_order_submission_guard_snapshot.return_value = (
            guard_snapshot
        )
        controller = Mock()
        controller.status = TradingSessionStatus.RECONCILIATION_REQUIRED
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
            harness.started_at = observed_at
            harness.artifact_directory = Path(temporary_directory)
            harness.failure_handling_started = False
            harness.mutation_started = False
            harness.failure_artifact_path = None
            harness._collect_runtime_observations = Mock()
            harness._attempt_known_safe_failure_recovery = Mock(
                return_value={
                    "attempted": False,
                    "outcome": "SKIPPED",
                    "typed_reason": "FAILURE_RECOVERY_STATE_AMBIGUOUS",
                }
            )
            harness._capture_failure_runtime_state = Mock(
                return_value={
                    "application_status": "READY",
                    "trading_status": "RECONCILIATION_REQUIRED",
                    "reconciliation_required": True,
                    "position_quantity": None,
                    "pending_order_count": 1,
                    "durable_trade_count": 0,
                }
            )
            harness._capture_fresh_failure_verification = Mock(
                return_value={
                    "status": "INCOMPLETE",
                    "typed_reason": "FRESH_VERIFICATION_INCOMPLETE",
                    "verified_at": None,
                    "position_quantity": None,
                    "pending_order_count": None,
                    "reconciliation_required": None,
                    "matching_open_order_count": None,
                    "run_exchange_order_count": None,
                    "durable_trade_count": None,
                }
            )

            # close/fresh network 경계는 local typed fakes로 바꾸고 finalizer의 ordering state만 검증한다.
            with patch(
                f"{__name__}.close_application",
                return_value=SimpleNamespace(status=ApplicationStatus.CLOSED),
            ):
                harness._record_actual_failure_evidence(original_error)

            gateway.block_phase13_order_submissions.assert_called_once_with()
            controller.mark_event_runtime_failed.assert_called_once_with()
            harness._attempt_known_safe_failure_recovery.assert_called_once_with(
                guard_snapshot,
                [],
            )
            harness._capture_fresh_failure_verification.assert_called_once_with(
                frozenset({"bat-p13-client-1"}),
                [],
            )
            self.assertIsInstance(original_error, TimeoutError)
            self.assertTrue(harness.failure_artifact_path.is_file())
            artifact_bytes = harness.failure_artifact_path.read_bytes()
            self.assertNotIn(b"secret-bearing detail", artifact_bytes)
            self.assertIn(b'"outcome":"FAILED"', artifact_bytes)
            self.assertIn(
                b'"recovery":{"attempted":false,"outcome":"SKIPPED",'
                b'"typed_reason":"FAILURE_RECOVERY_STATE_AMBIGUOUS"}',
                artifact_bytes,
            )
            self.assertTrue(
                any("sealed FAILED evidence" in note for note in original_error.__notes__)
            )

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
        self.assertEqual([], evidence_errors)
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
class BinanceTestnetPhaseThirteenPublicMarketCase2Tests(unittest.TestCase):
    """
    클래스 이름: BinanceTestnetPhaseThirteenPublicMarketCase2Tests
    기능: 세 opt-in에서 자연 public Case C BUY와 same-run STOP recovery를 실제 Testnet에서 검증한다.
    작성 날짜: 2026/08/31

    주의: 이 class는 private Action, threshold patch 또는 성공 상태 주입을 전혀 사용하지 않는다.
    실제 주문은 이 module 하나를 세 opt-in과 100 USDT 이하 cap으로 직접 지정할 때만 실행한다.
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
        self.started_at = _utc_now()
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
        self.event_stream = BackendEventStream()
        self.runtime = create_testnet_application_runtime(
            create_account_update_observer(self.event_stream),
            create_trade_history_update_observer(self.event_stream),
            create_trading_session_update_observer(self.event_stream),
            history_path=self.history_path,
            environment=self.testnet_environment,
            risk_policy_state=_create_actual_risk_policy(self.maximum_notional),
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

        # Snapshot version당 한 번만 source Kline을 저장하고 bounded trace가 오래된 event를 무한히 보존하지 않게 한다.
        current_market_version = self.runtime.market_snapshot.version
        if current_market_version > self.last_market_version:
            market_event = _create_observed_market_event(
                self.runtime,
                sequence=len(self.public_market_events) + 1,
            )
            if market_event is not None:
                self.public_market_events.append(market_event)
                if len(self.public_market_events) > _MAXIMUM_RECORDED_MARKET_EVENTS:
                    self.public_market_events.pop(0)
                    for sequence, retained_event in enumerate(
                        self.public_market_events,
                        start=1,
                    ):
                        retained_event["sequence"] = sequence
            self.last_market_version = current_market_version

    def _read_phase13_submission_guard(
        self,
    ) -> tuple[Phase13OrderSubmissionGuardSnapshot, list[dict[str, object]]]:
        """
        함수 이름: _read_phase13_submission_guard()
        기능: permission REST 경계의 frozen logical submit snapshot을 exact secret-free evidence로 정규화한다.
        인자: 없음
        반환값: 원 frozen snapshot과 contiguous normalized attempt 목록 tuple
        작성 날짜: 2026/08/31
        """
        snapshot = (
            self.runtime.api_gateway.get_phase13_order_submission_guard_snapshot()
        )
        if type(snapshot) is not Phase13OrderSubmissionGuardSnapshot:
            raise TypeError("Phase 13 gateway returned an invalid guard snapshot")

        # Raw request parameter 없이 permission layer가 승인한 logical identity와 시각만 복제한다.
        normalized_attempts = [
            {
                "sequence": sequence,
                "symbol": attempt.symbol,
                "side": attempt.side.value,
                "order_type": attempt.order_type,
                "intent_id": attempt.intent_id,
                "submission_attempt": attempt.submission_attempt,
                "attempted_at": _datetime_to_wire(attempt.attempted_at),
                "client_order_id": attempt.client_order_id,
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

    def _wait_for_public_buy(self) -> Trade | None:
        """
        함수 이름: _wait_for_public_buy()
        기능: 자연 Kline 평가가 만든 최초 durable BUY를 기다리고 signal 부재면 None을 반환한다.
        인자: 없음
        반환값: run 첫 BUY Trade 또는 bounded timeout의 None
        작성 날짜: 2026/08/31
        """
        deadline = time.monotonic() + _PUBLIC_SIGNAL_TIMEOUT_SECONDS

        # Production background worker가 public queue를 drain하며 test는 state를 관찰할 뿐 직접 실행하지 않는다.
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
            if self.runtime.trading_controller.reconciliation_required:
                raise AssertionError(
                    "public Case 2 entered reconciliation before a durable BUY"
                )

        return None  # NO_SIGNAL은 private trigger로 우회하지 않고 실제 주문 0을 별도 증거화한다.

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
        fresh_event_stream = BackendEventStream()
        fresh_runtime = create_testnet_application_runtime(
            create_account_update_observer(fresh_event_stream),
            create_trade_history_update_observer(fresh_event_stream),
            create_trading_session_update_observer(fresh_event_stream),
            history_path=self.history_path,
            environment=read_only_environment,
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
            open_results = fresh_runtime.api_gateway.list_all_open_order_results(
                "ETHUSDT"
            )
            require_empty_all_client_open_orders(open_results)
            recent_results = fresh_runtime.api_gateway.list_all_recent_order_results(
                "ETHUSDT",
                limit=1000,
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

            verified_at = _utc_now()
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
        except Exception:
            evidence_errors.append("RUNTIME_RECONCILIATION_UNAVAILABLE")
        try:
            position = controller.position
            position_quantity = (
                None if position is None else _decimal_to_wire(position.quantity)
            )
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
        기능: 주문 권한 없는 별도 runtime에서 가능한 final truth를 읽고 실패하면 null로 정직하게 남긴다.
        인자: run_client_order_ids -> permission guard가 관찰한 이번 run client identity 집합
            evidence_errors -> fresh startup/cleanup 오류의 typed code를 추가할 mutable 목록
        반환값: VERIFIED 또는 INCOMPLETE exact fresh_verification mapping
        작성 날짜: 2026/08/31
        """
        incomplete_verification: dict[str, object] = {
            "status": "INCOMPLETE",
            "typed_reason": "FRESH_VERIFICATION_INCOMPLETE",
            "verified_at": None,
            "position_quantity": None,
            "pending_order_count": None,
            "reconciliation_required": None,
            "matching_open_order_count": None,
            "run_exchange_order_count": None,
            "durable_trade_count": None,
        }
        fresh_event_stream = BackendEventStream()
        fresh_runtime: ApplicationRuntime | None = None
        try:
            # Third flag와 cap을 제거한 별도 client만 사용해 failure evidence 수집이 mutation을 만들지 않게 한다.
            fresh_runtime = create_testnet_application_runtime(
                create_account_update_observer(fresh_event_stream),
                create_trade_history_update_observer(fresh_event_stream),
                create_trading_session_update_observer(fresh_event_stream),
                history_path=self.history_path,
                environment=_create_read_only_testnet_environment(
                    self.configuration
                ),
            )
            ready_state = start_application(fresh_runtime)
            if ready_state.status is not ApplicationStatus.READY:
                evidence_errors.append("FRESH_RUNTIME_NOT_READY")
                return incomplete_verification

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
            open_results = fresh_runtime.api_gateway.list_all_open_order_results(
                "ETHUSDT"
            )
            matching_open_order_count = sum(
                result.client_order_id in run_client_order_ids
                for result in open_results
            )
            recent_results = fresh_runtime.api_gateway.list_all_recent_order_results(
                "ETHUSDT",
                limit=1000,
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
                evidence_errors.append("FRESH_EXCHANGE_BASELINE_UNAVAILABLE")

            fresh_status = "VERIFIED"
            fresh_reason: str | None = None
            if position_quantity is None:
                evidence_errors.append("FRESH_POSITION_UNAVAILABLE")
                fresh_status = "INCOMPLETE"
                fresh_reason = "FRESH_POSITION_UNAVAILABLE"
            elif run_exchange_order_count is None:
                fresh_status = "INCOMPLETE"
                fresh_reason = "FRESH_EXCHANGE_BASELINE_UNAVAILABLE"

            # VERIFIED는 success 판정이 아니라 required 관찰값을 모두 concrete하게 읽었다는 뜻이다.
            return {
                "status": fresh_status,
                "typed_reason": fresh_reason,
                "verified_at": _datetime_to_wire(_utc_now()),
                "position_quantity": position_quantity,
                "pending_order_count": pending_order_count,
                "reconciliation_required": reconciliation_required,
                "matching_open_order_count": matching_open_order_count,
                "run_exchange_order_count": run_exchange_order_count,
                "durable_trade_count": max(
                    0,
                    len(fresh_runtime.trade_history.trades)
                    - len(self.baseline_trades),
                ),
            }
        except Exception:
            evidence_errors.append("FRESH_VERIFICATION_FAILED")
            return incomplete_verification
        finally:
            if fresh_runtime is not None:
                try:
                    close_application(fresh_runtime)
                except Exception:
                    evidence_errors.append("FRESH_RUNTIME_CLOSE_FAILED")
            fresh_event_stream.close()  # Fresh observer waiter는 startup 성공 여부와 무관하게 항상 회수한다.

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
        self.failure_handling_started = True
        evidence_errors: list[str] = []

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
        self.runtime.trading_controller.mark_event_runtime_failed()
        if self.runtime.trading_controller.status in (
            TradingSessionStatus.RUNNING,
            TradingSessionStatus.STOPPING,
        ):
            raise AssertionError("Phase 13 failure finalizer left the scheduler gate open")

        try:
            self._collect_runtime_observations()
        except Exception:
            evidence_errors.append("RUNTIME_OBSERVATION_INCOMPLETE")
        runtime_state = self._capture_failure_runtime_state(evidence_errors)

        # Block가 닫힌 뒤 worker와 stream을 먼저 멈춰 fresh reader가 같은 history와 동시에 변경되지 않게 한다.
        runtime_closed = False
        try:
            closed_state = close_application(self.runtime)
            runtime_closed = closed_state.status is ApplicationStatus.CLOSED
        except Exception:
            evidence_errors.append("PRIMARY_RUNTIME_CLOSE_FAILED")
        if not runtime_closed:
            evidence_errors.append("PRIMARY_RUNTIME_NOT_CLOSED")
            fresh_verification = {
                "status": "INCOMPLETE",
                "typed_reason": "PRIMARY_RUNTIME_NOT_CLOSED",
                "verified_at": None,
                "position_quantity": None,
                "pending_order_count": None,
                "reconciliation_required": None,
                "matching_open_order_count": None,
                "run_exchange_order_count": None,
                "durable_trade_count": None,
            }
        else:
            run_client_order_ids = frozenset(
                str(attempt["client_order_id"])
                for attempt in submission_attempts
            )
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
                "completed_at": _datetime_to_wire(_utc_now()),
            },
            "mutation_guard": {
                "mutation_started": guard_snapshot.mutation_started,
                "submissions_blocked": guard_snapshot.submissions_blocked,
                "submission_attempts": submission_attempts,
            },
            "recovery": recovery_evidence,
            "runtime_state": runtime_state,
            "fresh_verification": fresh_verification,
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
        기능: 주문 0과 fresh zero exposure를 seal한 뒤 NO_SIGNAL/BLOCKED를 성공으로 승격하지 않고 실패한다.
        인자: 없음
        반환값: 정상 반환하지 않음
        작성 날짜: 2026/08/31
        """
        controller = self.runtime.trading_controller
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
        outcome = (
            "BLOCKED" if controller.order_execution_trace else "NO_SIGNAL"
        )
        typed_reason = (
            "PUBLIC_ACTION_BLOCKED" if outcome == "BLOCKED" else None
        )
        if self.preflight is None:
            raise AssertionError("actual trace requires completed preflight evidence")
        trace_body = _create_non_mutating_trace_body(
            outcome=outcome,
            typed_reason=typed_reason,
            run_id=self.run_id,
            started_at=self.started_at,
            completed_at=_utc_now(),
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
            f"{outcome}: no actual order was submitted; "
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
                "completed_at": _datetime_to_wire(_utc_now()),
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
            raise  # NO_SIGNAL/BLOCKED는 이미 full normalized trace를 fsync했으므로 이중 FAILED를 만들지 않는다.
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
        self.assertFalse(controller.reconciliation_required)
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
        account_filters_observed_at = _utc_now()
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
        filters_observed_at = _utc_now()

        # EXCHANGE_* count와 account 격리를 symbol 생략 signed snapshot 두 개의 exact empty로 증명한다.
        if self.runtime.api_gateway.has_any_exchange_open_orders():
            raise AssertionError(
                "Phase 13 preflight requires zero exchange-wide open orders"
            )
        account_open_orders_observed_at = _utc_now()
        if self.runtime.api_gateway.has_any_exchange_open_order_lists():
            raise AssertionError(
                "Phase 13 preflight requires zero exchange-wide open order lists"
            )
        account_open_order_lists_observed_at = _utc_now()
        reference_price = self.runtime.api_gateway.fetch_reference_price(
            "ETHUSDT"
        )
        reference_price_observed_at = _utc_now()
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
            verified_at=_utc_now(),
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

        # TYPE_0과 split은 public optimistic-version command만 사용하고 BUY 수량은 production cap이 제한한다.
        selection = self.runtime.regime_controller.set_regime_type(
            RegimeType.TYPE_0,
            command_id=f"phase13-select-{self.run_id}",
            expected_version=controller.context.version,
        )
        split_result = controller.update_split_ratios(
            command_id=f"phase13-split-{self.run_id}",
            expected_version=selection.version,
            scale_in=Decimal("1"),
            scale_out=Decimal("1"),
        )
        session_result = controller.start_trading(
            command_id=f"phase13-start-{self.run_id}",
            expected_version=split_result.version,
        )
        self.assertIs(session_result.status, TradingSessionStatus.RUNNING)

        # 실제 Kline 조건이 자연히 성립할 때만 production worker가 BUY를 만들며 timeout은 mutation 없이 기록한다.
        buy_trade = self._wait_for_public_buy()
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
