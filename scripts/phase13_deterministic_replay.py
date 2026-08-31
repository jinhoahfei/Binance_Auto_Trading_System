#!/usr/bin/env python3
"""Phase 13 fault trace를 network와 wall clock 없이 결정론적으로 replay한다."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.history.trade import (
    Trade,
    trade_to_json_object,
)
from binance_auto_trader.domain.history.trade_history import TradeHistory
from binance_auto_trader.domain.trading.order import (
    Fill,
    Order,
    OrderResult,
    OrderStatus,
)
from binance_auto_trader.domain.trading.position import Position
from binance_auto_trader.domain.trading.states import (
    ExitReason,
    OrderSide,
    StrategyType,
)
from binance_auto_trader.transport.event_stream import BackendEventStream


# Fixture와 결과는 strict schema, bounded 크기와 canonical SHA-256 형식을 함께 사용한다.
TRACE_SCHEMA_VERSION = 1
TRACE_RECORD_TYPE = "phase13_fault_trace"
REPLAY_RECORD_TYPE = "phase13_deterministic_replay"
EVIDENCE_SCOPE = "OFFLINE_CONTRACT_REPLAY"
MAXIMUM_TRACE_BYTES = 1024 * 1024
MAXIMUM_SCENARIOS = 128
MAXIMUM_STEPS_PER_SCENARIO = 128
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
IDENTIFIER_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
SCENARIO_ID_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
PLAIN_DECIMAL_PATTERN = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
UTC_TIMESTAMP_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z"
)
SECRET_KEY_FRAGMENT_PATTERN = re.compile(
    r"(?:api[_-]?key|api[_-]?secret|credential|private[_-]?key|session[_-]?token)",
    re.IGNORECASE,
)
SECRET_VALUE_PATTERN = re.compile(
    r"(?:-----BEGIN [A-Z ]+ PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{12,})"
)
FIXED_EVENT_TIME = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc)
FIXED_SESSION_ID = "00000000-0000-4000-8000-000000000013"


@dataclass(frozen=True, slots=True)
class FaultPolicy:
    """
    클래스 이름: FaultPolicy
    기능: 한 fault의 owner, fail-closed state와 recovery 계약을 고정한다.
    작성 날짜: 2026/08/25
    """

    category: str
    owner: str
    state: str
    new_order_allowed: bool
    recovery_action: str


# P13-05 최소 행렬의 모든 fault를 하나의 typed lookup으로 고정한다.
FAULT_POLICIES: dict[str, FaultPolicy] = {
    "REST_TIMEOUT": FaultPolicy(
        "REST",
        "APIGateway/TradingController",
        "RECONCILIATION_REQUIRED",
        False,
        "QUERY_SAME_CLIENT_ORDER_ID",
    ),
    "REST_5XX": FaultPolicy(
        "REST",
        "APIGateway/TradingController",
        "RECONCILIATION_REQUIRED",
        False,
        "QUERY_SAME_CLIENT_ORDER_ID",
    ),
    "REST_RATE_LIMIT": FaultPolicy(
        "REST",
        "APIGateway/TradingController",
        "RECONCILIATION_REQUIRED",
        False,
        "HONOR_RETRY_AFTER_THEN_QUERY_SAME_ID",
    ),
    "REST_RESPONSE_DECODE_FAILURE": FaultPolicy(
        "REST",
        "APIGateway/TradingController",
        "RECONCILIATION_REQUIRED",
        False,
        "QUERY_SAME_CLIENT_ORDER_ID",
    ),
    "WS_DISCONNECT_RECONNECT": FaultPolicy(
        "WEBSOCKET",
        "WebSocketGateway/TradingController",
        "RECONCILIATION_REQUIRED",
        False,
        "REST_SNAPSHOT_THEN_NEW_GENERATION",
    ),
    "WS_STALE_GENERATION": FaultPolicy(
        "WEBSOCKET",
        "WebSocketGateway",
        "RUNNING",
        True,
        "DROP_STALE_GENERATION",
    ),
    "WS_SEQUENCE_GAP": FaultPolicy(
        "WEBSOCKET",
        "WebSocketGateway/TradingController",
        "RECONCILIATION_REQUIRED",
        False,
        "FULL_SNAPSHOT_RESYNC",
    ),
    "WS_DUPLICATE_EVENT": FaultPolicy(
        "WEBSOCKET",
        "Order/TradeHistory",
        "RUNNING",
        True,
        "DEDUPLICATE_BY_ORDER_AND_FILL_ID",
    ),
    "WS_OUT_OF_ORDER_EVENT": FaultPolicy(
        "WEBSOCKET",
        "Order",
        "RUNNING",
        True,
        "PRESERVE_MONOTONIC_ORDER_STATE",
    ),
    "CRASH_BEFORE_SUBMIT": FaultPolicy(
        "PROCESS",
        "TradingController/TradeHistoryRepository",
        "RECONCILIATION_REQUIRED",
        False,
        "QUERY_PREPARED_IDENTITY_BEFORE_SUBMIT",
    ),
    "CRASH_AFTER_SUBMIT": FaultPolicy(
        "PROCESS",
        "TradingController/TradeHistoryRepository",
        "RECONCILIATION_REQUIRED",
        False,
        "QUERY_SAME_CLIENT_ORDER_ID",
    ),
    "PENDING_JOURNAL_FSYNC_FAILURE": FaultPolicy(
        "REPOSITORY",
        "TradeHistoryRepository/TradingController",
        "RECONCILIATION_REQUIRED",
        False,
        "REPAIR_JOURNAL_BEFORE_SUBMIT",
    ),
    "PENDING_JOURNAL_REMOVE_FAILURE": FaultPolicy(
        "REPOSITORY",
        "TradeHistoryRepository/TradingController",
        "RECONCILIATION_REQUIRED",
        False,
        "RETRY_DURABLE_REMOVE",
    ),
    "HISTORY_APPEND_FAILURE": FaultPolicy(
        "REPOSITORY",
        "TradeHistoryRepository/TradingController",
        "RECONCILIATION_REQUIRED",
        False,
        "RETRY_HISTORY_APPEND_THEN_REMOVE",
    ),
    "UNKNOWN_RECENT_APP_ORDER": FaultPolicy(
        "EXCHANGE_STATE",
        "TradingController/APIGateway",
        "RECONCILIATION_REQUIRED",
        False,
        "QUERY_AND_CLASSIFY_APP_ORDER",
    ),
    "TESTNET_RESET": FaultPolicy(
        "EXCHANGE_STATE",
        "TradingController/APIGateway",
        "RECONCILIATION_REQUIRED",
        False,
        "REQUIRE_OPERATOR_RECONCILIATION",
    ),
    "BALANCE_DECREASE": FaultPolicy(
        "EXCHANGE_STATE",
        "TradingController/Account",
        "RECONCILIATION_REQUIRED",
        False,
        "RECONCILE_ACCOUNT_AND_POSITION",
    ),
    "FEE_DUST_MISMATCH": FaultPolicy(
        "EXCHANGE_STATE",
        "TradingController/Position",
        "RECONCILIATION_REQUIRED",
        False,
        "RECONCILE_FILL_FEE_AND_FREE_BALANCE",
    ),
    "SIDECAR_PRE_READY_EXIT": FaultPolicy(
        "SIDECAR",
        "NativeSidecarLifecycle",
        "RECONCILIATION_REQUIRED",
        False,
        "REAP_CHILD_AND_SCAN_OWNERSHIP",
    ),
    "SIDECAR_LATE_READY": FaultPolicy(
        "SIDECAR",
        "NativeSidecarLifecycle",
        "WAITING_READY",
        False,
        "WAIT_WITHIN_READY_DEADLINE",
    ),
    "SIDECAR_ABNORMAL_EXIT": FaultPolicy(
        "SIDECAR",
        "NativeSidecarLifecycle",
        "RECONCILIATION_REQUIRED",
        False,
        "REAP_CHILD_AND_SCAN_OWNERSHIP",
    ),
    "MAIN_CRASH_LISTENER_ORPHAN": FaultPolicy(
        "PROCESS",
        "ProcessWatchdog/TradingController",
        "RECONCILIATION_REQUIRED",
        False,
        "VERIFY_PID_START_IDENTITY_AND_LOCK",
    ),
    "SHUTDOWN_POSITION_DRIFT": FaultPolicy(
        "SHUTDOWN",
        "TradingController",
        "RECONCILIATION_REQUIRED",
        False,
        "RECONCILE_POSITION_BEFORE_CLOSED",
    ),
    "SHUTDOWN_ORDER_DRIFT": FaultPolicy(
        "SHUTDOWN",
        "TradingController",
        "RECONCILIATION_REQUIRED",
        False,
        "RECONCILE_ORDER_BEFORE_CLOSED",
    ),
    "SHUTDOWN_CLOSED_ACK_LOSS": FaultPolicy(
        "SHUTDOWN",
        "NativeSidecarLifecycle",
        "RECONCILIATION_REQUIRED",
        False,
        "VERIFY_CHILD_EXIT_AND_OWNERSHIP_ARTIFACT",
    ),
}


class DeterministicReplayError(RuntimeError):
    """
    클래스 이름: DeterministicReplayError
    기능: canonical trace schema, replay 불변식 또는 digest 불일치를 나타낸다.
    작성 날짜: 2026/08/25
    """


@dataclass(slots=True)
class ReplayRuntime:
    """
    클래스 이름: ReplayRuntime
    기능: 공개 domain aggregate와 정규화 UI stream의 한 scenario 상태를 보존한다.
    작성 날짜: 2026/08/25
    """

    position: Position
    trade_history: TradeHistory
    event_stream: BackendEventStream
    state: str
    new_order_allowed: bool
    recovery_action: str
    owner: str
    order: Order | None = None
    order_mutation_count: int = 0
    fault_observed: bool = False


def _load_unique_json_object(
    object_pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    """
    함수 이름: _load_unique_json_object()
    기능: JSON duplicate key를 마지막 값으로 덮지 않고 즉시 거부한다.
    인자: object_pairs -> decoder가 전달한 key-value pair 목록
    반환값: duplicate key 없는 dictionary
    작성 날짜: 2026/08/25
    """
    loaded_object: dict[str, Any] = {}
    for object_key, object_value in object_pairs:
        if object_key in loaded_object:
            raise DeterministicReplayError("trace contains duplicate JSON keys")
        loaded_object[object_key] = object_value

    return loaded_object  # decoder가 전달한 key 순서와 값은 digest 전에 schema로 다시 고정한다.


def _reject_nonstandard_constant(constant_name: str) -> None:
    """
    함수 이름: _reject_nonstandard_constant()
    기능: NaN과 Infinity 같은 JSON 표준 밖 숫자를 fail-closed로 거부한다.
    인자: constant_name -> decoder가 발견한 비표준 constant 이름
    반환값: 정상 반환하지 않음
    작성 날짜: 2026/08/25
    """
    # 표준 JSON 밖의 숫자 token은 Decimal이나 Python float로 변환하지 않고 즉시 중단한다.
    raise DeterministicReplayError(
        f"trace contains non-standard JSON constant: {constant_name}"
    )


def _require_exact_keys(
    value: object,
    expected_keys: set[str],
    location: str,
) -> Mapping[str, object]:
    """
    함수 이름: _require_exact_keys()
    기능: 값이 JSON object이고 key 집합이 schema와 정확히 같은지 검증한다.
    인자: value -> 검증할 JSON 값
        expected_keys -> 허용할 exact key 집합
        location -> 오류에 표시할 schema 위치
    반환값: 검증된 mapping
    작성 날짜: 2026/08/25
    """
    if not isinstance(value, Mapping):
        raise DeterministicReplayError(f"{location} must be a JSON object")
    if set(value.keys()) != expected_keys:
        raise DeterministicReplayError(f"{location} has an invalid exact schema")

    return value  # caller는 exact key를 직접 읽어도 누락이나 확장 field가 없다.


def _require_identifier(value: object, location: str) -> str:
    """
    함수 이름: _require_identifier()
    기능: fault, category와 operation 값을 canonical 대문자 identifier로 검증한다.
    인자: value -> 검증할 값
        location -> 오류에 표시할 schema 위치
    반환값: 검증된 문자열
    작성 날짜: 2026/08/25
    """
    if not isinstance(value, str) or IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise DeterministicReplayError(f"{location} must be a canonical identifier")

    return value  # 문자열 변환이나 trim 없이 fixture의 canonical 값을 보존한다.


def _require_scenario_id(value: object, location: str) -> str:
    """
    함수 이름: _require_scenario_id()
    기능: scenario ID를 공백 없는 소문자 snake_case로 검증한다.
    인자: value -> 검증할 값
        location -> 오류에 표시할 schema 위치
    반환값: 검증된 scenario ID
    작성 날짜: 2026/08/25
    """
    if not isinstance(value, str) or SCENARIO_ID_PATTERN.fullmatch(value) is None:
        raise DeterministicReplayError(f"{location} must be snake_case")

    return value  # 사람이 읽는 행 ID와 digest 입력이 같은 문자열을 공유한다.


def _require_text(value: object, location: str) -> str:
    """
    함수 이름: _require_text()
    기능: 문자열이 비어 있지 않고 앞뒤 공백이 없는지 검증한다.
    인자: value -> 검증할 값
        location -> 오류에 표시할 schema 위치
    반환값: 검증된 문자열
    작성 날짜: 2026/08/25
    """
    if not isinstance(value, str) or not value or value != value.strip():
        raise DeterministicReplayError(f"{location} must be canonical text")

    return value  # 자동 trim으로 서로 다른 trace를 같은 입력으로 바꾸지 않는다.


def _require_plain_decimal(value: object, location: str) -> Decimal:
    """
    함수 이름: _require_plain_decimal()
    기능: 금융 문자열을 exponent 없는 유한 Decimal로 변환한다.
    인자: value -> 검증할 JSON 값
        location -> 오류에 표시할 schema 위치
    반환값: 유한 Decimal
    작성 날짜: 2026/08/25
    """
    if not isinstance(value, str) or PLAIN_DECIMAL_PATTERN.fullmatch(value) is None:
        raise DeterministicReplayError(f"{location} must be a plain decimal string")
    try:
        decimal_value = Decimal(value)
    except InvalidOperation as error:
        raise DeterministicReplayError(f"{location} is not a valid Decimal") from error
    if not decimal_value.is_finite():
        raise DeterministicReplayError(f"{location} must be finite")

    return decimal_value  # fixture의 decimal scale은 domain 생성까지 그대로 전달한다.


def _require_utc_timestamp(value: object, location: str) -> datetime:
    """
    함수 이름: _require_utc_timestamp()
    기능: microsecond 없는 RFC 3339 UTC Z 시각을 timezone-aware datetime으로 변환한다.
    인자: value -> 검증할 JSON 값
        location -> 오류에 표시할 schema 위치
    반환값: timezone-aware UTC datetime
    작성 날짜: 2026/08/25
    """
    if not isinstance(value, str) or UTC_TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise DeterministicReplayError(f"{location} must be an RFC 3339 UTC timestamp")
    try:
        timestamp = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as error:
        raise DeterministicReplayError(f"{location} is not a valid UTC timestamp") from error

    return timestamp.astimezone(timezone.utc)  # 모든 event 시간을 하나의 canonical timezone으로 맞춘다.


def _require_sha256(value: object, location: str) -> str:
    """
    함수 이름: _require_sha256()
    기능: expected digest가 lowercase SHA-256 문자열인지 검증한다.
    인자: value -> 검증할 JSON 값
        location -> 오류에 표시할 schema 위치
    반환값: 검증된 digest
    작성 날짜: 2026/08/25
    """
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise DeterministicReplayError(f"{location} must be lowercase SHA-256")

    return value  # placeholder 추측 없이 fixture에 기록된 exact digest를 반환한다.


def _reject_secret_material(value: object, location: str = "trace") -> None:
    """
    함수 이름: _reject_secret_material()
    기능: canonical fixture의 secret-like key와 명백한 private material을 재귀적으로 거부한다.
    인자: value -> 검사할 JSON tree
        location -> 오류에 표시할 tree 위치
    반환값: 없음
    작성 날짜: 2026/08/25
    """
    # Mapping과 list를 재귀 순회하며 key/value secret과 JSON float를 각각 차단한다.
    if isinstance(value, Mapping):
        for object_key, object_value in value.items():
            if SECRET_KEY_FRAGMENT_PATTERN.search(str(object_key)) is not None:
                raise DeterministicReplayError(f"{location} contains a secret-like key")
            _reject_secret_material(object_value, f"{location}.{object_key}")
        return
    if isinstance(value, list):
        for item_index, item_value in enumerate(value):
            _reject_secret_material(item_value, f"{location}[{item_index}]")
        return
    if isinstance(value, float):
        raise DeterministicReplayError(f"{location} must not contain JSON floats")
    if isinstance(value, str) and SECRET_VALUE_PATTERN.search(value) is not None:
        raise DeterministicReplayError(f"{location} contains secret-like material")


def _canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    """
    함수 이름: _canonical_json_bytes()
    기능: 정렬 key, compact separator와 UTF-8로 byte-stable JSON 한 줄을 만든다.
    인자: value -> canonical encoding할 JSON object
    반환값: 마지막 newline을 포함한 UTF-8 bytes
    작성 날짜: 2026/08/25
    """
    try:
        encoded_text = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise DeterministicReplayError("replay output is not canonical JSON") from error

    return f"{encoded_text}\n".encode("utf-8")  # CLI와 digest가 같은 framing을 사용한다.


def _calculate_digest(value: Mapping[str, object]) -> str:
    """
    함수 이름: _calculate_digest()
    기능: canonical JSON bytes의 lowercase SHA-256 digest를 계산한다.
    인자: value -> digest할 JSON object
    반환값: lowercase SHA-256 문자열
    작성 날짜: 2026/08/25
    """
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()  # newline까지 evidence에 결합한다.


def _load_trace_document(trace_path: Path) -> Mapping[str, object]:
    """
    함수 이름: _load_trace_document()
    기능: bounded regular JSON file을 duplicate key와 비표준 상수 없이 읽는다.
    인자: trace_path -> canonical fault trace 경로
    반환값: JSON root mapping
    작성 날짜: 2026/08/25
    """
    # Symlink와 크기 경계를 먼저 확인해 replay 입력을 하나의 bounded regular file로 고정한다.
    if not isinstance(trace_path, Path):
        raise TypeError("trace_path must be a Path")
    if not trace_path.is_file() or trace_path.is_symlink():
        raise DeterministicReplayError("trace path must be a regular non-symlink file")
    file_size = trace_path.stat().st_size
    if file_size <= 0 or file_size > MAXIMUM_TRACE_BYTES:
        raise DeterministicReplayError("trace file size is outside the allowed range")

    # UTF-8 decode와 strict JSON parse 뒤 전체 tree에서 secret-like material을 재귀 검사한다.
    try:
        trace_text = trace_path.read_text(encoding="utf-8")
        loaded_value = json.loads(
            trace_text,
            object_pairs_hook=_load_unique_json_object,
            parse_constant=_reject_nonstandard_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DeterministicReplayError("trace file is not canonical UTF-8 JSON") from error
    _reject_secret_material(loaded_value)

    return _require_exact_keys(
        loaded_value,
        {
            "schema_version",
            "record_type",
            "trace_id",
            "evidence_scope",
            "scenarios",
            "expected_replay_digest",
        },
        "trace",
    )


def _build_order(step: Mapping[str, object], location: str) -> Order:
    """
    함수 이름: _build_order()
    기능: BEGIN_ORDER step을 공개 Order aggregate로 변환한다.
    인자: step -> exact schema 검증 전 step mapping
        location -> 오류에 표시할 scenario step 위치
    반환값: 검증된 Order aggregate
    작성 날짜: 2026/08/25
    """
    validated_step = _require_exact_keys(
        step,
        {
            "operation",
            "intent_id",
            "client_order_id",
            "submission_attempt",
            "symbol",
            "side",
            "strategy",
            "regime_type",
            "requested_quantity",
            "submitted_quantity",
            "market_price_at_decision",
            "exit_reason",
        },
        location,
    )
    submission_attempt = validated_step["submission_attempt"]
    if type(submission_attempt) is not int or submission_attempt < 0:
        raise DeterministicReplayError(f"{location}.submission_attempt is invalid")
    try:
        side = OrderSide(_require_identifier(validated_step["side"], f"{location}.side"))
        strategy = StrategyType(
            _require_identifier(validated_step["strategy"], f"{location}.strategy")
        )
        regime_type = RegimeType(
            _require_identifier(validated_step["regime_type"], f"{location}.regime_type")
        )
        exit_reason_value = validated_step["exit_reason"]
        exit_reason = (
            None
            if exit_reason_value is None
            else ExitReason(_require_identifier(exit_reason_value, f"{location}.exit_reason"))
        )
    except ValueError as error:
        raise DeterministicReplayError(f"{location} contains an unsupported enum") from error

    return Order(
        intent_id=_require_text(validated_step["intent_id"], f"{location}.intent_id"),
        client_order_id=_require_text(
            validated_step["client_order_id"],
            f"{location}.client_order_id",
        ),
        submission_attempt=submission_attempt,
        symbol=_require_text(validated_step["symbol"], f"{location}.symbol"),
        side=side,
        strategy=strategy,
        regime_type=regime_type,
        requested_quantity=_require_plain_decimal(
            validated_step["requested_quantity"],
            f"{location}.requested_quantity",
        ),
        submitted_quantity=_require_plain_decimal(
            validated_step["submitted_quantity"],
            f"{location}.submitted_quantity",
        ),
        market_price_at_decision=_require_plain_decimal(
            validated_step["market_price_at_decision"],
            f"{location}.market_price_at_decision",
        ),
        exit_reason=exit_reason,
    )  # test-only private Action 없이 공개 aggregate 생성 경계만 사용한다.


def _build_fill(
    fill_value: object,
    location: str,
) -> Fill:
    """
    함수 이름: _build_fill()
    기능: ORDER_RESULT fill object를 공개 Fill value로 변환한다.
    인자: fill_value -> exact schema 검증 전 JSON 값
        location -> 오류에 표시할 fill 위치
    반환값: 검증된 Fill
    작성 날짜: 2026/08/25
    """
    validated_fill = _require_exact_keys(
        fill_value,
        {
            "exchange_order_id",
            "trade_id",
            "quantity",
            "price",
            "fee_amount",
            "fee_asset",
            "fee_quote_amount",
            "executed_at",
        },
        location,
    )

    return Fill(
        exchange_order_id=_require_text(
            validated_fill["exchange_order_id"],
            f"{location}.exchange_order_id",
        ),
        trade_id=_require_text(validated_fill["trade_id"], f"{location}.trade_id"),
        quantity=_require_plain_decimal(validated_fill["quantity"], f"{location}.quantity"),
        price=_require_plain_decimal(validated_fill["price"], f"{location}.price"),
        fee_amount=_require_plain_decimal(
            validated_fill["fee_amount"],
            f"{location}.fee_amount",
        ),
        fee_asset=_require_text(validated_fill["fee_asset"], f"{location}.fee_asset"),
        fee_quote_amount=_require_plain_decimal(
            validated_fill["fee_quote_amount"],
            f"{location}.fee_quote_amount",
        ),
        executed_at=_require_utc_timestamp(
            validated_fill["executed_at"],
            f"{location}.executed_at",
        ),
    )  # fee와 time 불변식은 production domain value가 다시 검증한다.


def _build_order_result(
    step: Mapping[str, object],
    location: str,
) -> OrderResult:
    """
    함수 이름: _build_order_result()
    기능: ORDER_RESULT step을 공개 OrderResult value로 변환한다.
    인자: step -> exact schema 검증 전 step mapping
        location -> 오류에 표시할 scenario step 위치
    반환값: 검증된 OrderResult
    작성 날짜: 2026/08/25
    """
    validated_step = _require_exact_keys(
        step,
        {
            "operation",
            "symbol",
            "client_order_id",
            "status",
            "processed_at",
            "exchange_order_id",
            "fills",
        },
        location,
    )
    fill_values = validated_step["fills"]
    if not isinstance(fill_values, list):
        raise DeterministicReplayError(f"{location}.fills must be a list")
    fills = tuple(
        _build_fill(fill_value, f"{location}.fills[{fill_index}]")
        for fill_index, fill_value in enumerate(fill_values)
    )
    try:
        order_status = OrderStatus(
            _require_identifier(validated_step["status"], f"{location}.status")
        )
    except ValueError as error:
        raise DeterministicReplayError(f"{location}.status is unsupported") from error
    exchange_order_id_value = validated_step["exchange_order_id"]
    exchange_order_id = (
        None
        if exchange_order_id_value is None
        else _require_text(exchange_order_id_value, f"{location}.exchange_order_id")
    )

    return OrderResult(
        symbol=_require_text(validated_step["symbol"], f"{location}.symbol"),
        client_order_id=_require_text(
            validated_step["client_order_id"],
            f"{location}.client_order_id",
        ),
        status=order_status,
        processed_at=_require_utc_timestamp(
            validated_step["processed_at"],
            f"{location}.processed_at",
        ),
        exchange_order_id=exchange_order_id,
        fills=fills,
    )  # 실제 REST/WS raw payload 대신 이미 정규화된 public result만 replay한다.


def _apply_step(
    runtime: ReplayRuntime,
    step_value: object,
    policy: FaultPolicy,
    location: str,
) -> None:
    """
    함수 이름: _apply_step()
    기능: 한 canonical step을 순서대로 공개 aggregate 또는 fault contract에 반영한다.
    인자: runtime -> 현재 scenario replay 상태
        step_value -> 적용할 JSON step
        policy -> scenario fault의 고정 정책
        location -> 오류에 표시할 step 위치
    반환값: 없음
    작성 날짜: 2026/08/25
    """
    if not isinstance(step_value, Mapping):
        raise DeterministicReplayError(f"{location} must be a JSON object")
    operation = _require_identifier(step_value.get("operation"), f"{location}.operation")

    # 각 operation은 서로 다른 exact schema와 선행 상태를 가져 암묵적 기본값을 만들지 않는다.
    if operation == "BEGIN_ORDER":
        if runtime.order is not None:
            raise DeterministicReplayError(f"{location} cannot replace an active order")
        runtime.order = _build_order(step_value, location)
        return
    if operation == "SUBMIT_ORDER":
        _require_exact_keys(step_value, {"operation"}, location)
        if runtime.order is None:
            raise DeterministicReplayError(f"{location} requires BEGIN_ORDER")
        if not runtime.new_order_allowed:
            raise DeterministicReplayError(
                f"{location} cannot submit while new orders are blocked"
            )
        if runtime.order_mutation_count != 0:
            raise DeterministicReplayError(f"{location} would duplicate an order mutation")
        runtime.order_mutation_count += 1  # offline counter만 변경하며 network port는 호출하지 않는다.
        return
    if operation == "ORDER_RESULT":
        if runtime.order is None:
            raise DeterministicReplayError(f"{location} requires BEGIN_ORDER")
        result = _build_order_result(step_value, location)
        if runtime.order.status is None:
            runtime.order.apply_order_result(result)
        else:
            runtime.order.reapply_order_result(result)
        return
    if operation == "APPLY_NEW_FILLS":
        _require_exact_keys(step_value, {"operation"}, location)
        if runtime.order is None or not runtime.order.unapplied_fills:
            raise DeterministicReplayError(f"{location} requires unapplied order fills")

        # 공개 Order summary와 Position mutation을 사용해 fill delta를 한 번만 반영한다.
        new_fills = runtime.order.unapplied_fills
        summary = runtime.order.build_execution_summary(
            fills=new_fills,
            require_terminal=False,
        )
        runtime.position.apply_execution(summary)
        runtime.order.mark_fills_applied(new_fills)
        return
    if operation == "COMMIT_TERMINAL_TRADE":
        _require_exact_keys(step_value, {"operation"}, location)
        if runtime.order is None or not runtime.order.is_terminal:
            raise DeterministicReplayError(f"{location} requires a terminal order")
        if runtime.order.unapplied_fills:
            raise DeterministicReplayError(f"{location} requires applied Position fills")

        # terminal cumulative summary에서 canonical Trade를 만들고 public history idempotency를 쓴다.
        summary = runtime.order.build_execution_summary()
        trade = Trade.from_order_execution(runtime.order, summary)
        runtime.trade_history.add_trade(trade)
        return
    if operation == "PUBLISH_UI_EVENT":
        validated_step = _require_exact_keys(
            step_value,
            {
                "operation",
                "event_type",
                "aggregate_version",
                "correlation_id",
                "payload",
            },
            location,
        )
        aggregate_version = validated_step["aggregate_version"]
        if aggregate_version is not None and (
            type(aggregate_version) is not int or aggregate_version < 0
        ):
            raise DeterministicReplayError(f"{location}.aggregate_version is invalid")
        payload = validated_step["payload"]
        if not isinstance(payload, Mapping):
            raise DeterministicReplayError(f"{location}.payload must be an object")
        correlation_id_value = validated_step["correlation_id"]
        correlation_id = (
            None
            if correlation_id_value is None
            else _require_text(correlation_id_value, f"{location}.correlation_id")
        )
        runtime.event_stream.publish(
            _require_identifier(validated_step["event_type"], f"{location}.event_type"),
            payload,
            aggregate_version=aggregate_version,
            correlation_id=correlation_id,
            occurred_at=FIXED_EVENT_TIME,
        )
        return
    if operation == "FAULT_OBSERVED":
        _require_exact_keys(step_value, {"operation"}, location)
        if runtime.fault_observed:
            raise DeterministicReplayError(f"{location} duplicates the scenario fault")
        runtime.owner = policy.owner
        runtime.state = policy.state
        runtime.new_order_allowed = policy.new_order_allowed
        runtime.recovery_action = policy.recovery_action
        runtime.fault_observed = True
        return

    raise DeterministicReplayError(f"{location} uses an unsupported operation")


def _normalize_decimal(value: Decimal) -> str:
    """
    함수 이름: _normalize_decimal()
    기능: Decimal을 exponent와 trailing zero 없는 canonical plain 문자열로 변환한다.
    인자: value -> 정규화할 유한 Decimal
    반환값: canonical plain decimal 문자열
    작성 날짜: 2026/08/25
    """
    if not isinstance(value, Decimal) or not value.is_finite():
        raise DeterministicReplayError("normalized financial value must be finite Decimal")
    if value == Decimal("0"):
        return "0"  # 음의 0과 서로 다른 scale의 0을 하나로 통일한다.
    normalized_text = format(value.normalize(), "f")

    return normalized_text  # exponent 없는 domain 결과만 digest input으로 사용한다.


def _normalize_trade(trade: Trade) -> dict[str, object]:
    """
    함수 이름: _normalize_trade()
    기능: public Trade JSON을 digest용 canonical Decimal 표현으로 정규화한다.
    인자: trade -> 정규화할 canonical Trade
    반환값: 정규화된 Trade object
    작성 날짜: 2026/08/25
    """
    trade_object = trade_to_json_object(trade)
    decimal_fields = (
        "requested_quantity",
        "executed_quantity",
        "executed_amount",
        "average_fill_price",
        "market_price_at_decision",
        "fee_amount",
        "fee_quote_amount",
        "allocated_cost_basis",
        "realized_pnl",
        "realized_return_rate",
    )
    for field_name in decimal_fields:
        field_value = trade_object[field_name]
        if field_value is not None:
            trade_object[field_name] = _normalize_decimal(Decimal(field_value))

    return trade_object  # 저장 schema의 의미는 보존하고 입력 scale 차이만 제거한다.


def _build_scenario_outcome(
    scenario_id: str,
    category: str,
    fault_kind: str,
    input_trace: Sequence[object],
) -> dict[str, object]:
    """
    함수 이름: _build_scenario_outcome()
    기능: 한 scenario를 처음부터 replay해 normalized state, Trade와 UI sequence를 만든다.
    인자: scenario_id -> 고유 matrix 행 ID
        category -> fixture의 fault category
        fault_kind -> FAULT_POLICIES key
        input_trace -> 순서가 고정된 canonical step 목록
    반환값: digest를 제외한 normalized scenario outcome
    작성 날짜: 2026/08/25
    """
    policy = FAULT_POLICIES[fault_kind]
    if category != policy.category:
        raise DeterministicReplayError(f"scenario {scenario_id} category mismatched")

    # 실제 wall clock 대신 고정 UTC와 monotonic 값을 주입해 retention과 event time을 고정한다.
    runtime = ReplayRuntime(
        position=Position(),
        trade_history=TradeHistory(),
        event_stream=BackendEventStream(
            session_id=FIXED_SESSION_ID,
            clock=lambda: FIXED_EVENT_TIME,
            monotonic_clock=lambda: 0.0,
        ),
        state="RUNNING",
        new_order_allowed=True,
        recovery_action="NONE",
        owner="NONE",
    )
    for step_index, step_value in enumerate(input_trace):
        _apply_step(
            runtime,
            step_value,
            policy,
            f"scenario[{scenario_id}].input_trace[{step_index}]",
        )
    if not runtime.fault_observed:
        raise DeterministicReplayError(f"scenario {scenario_id} did not observe its fault")

    # UUID와 occurred_at은 명시 정규화하고 sequence/type/payload 순서만 UI evidence에 남긴다.
    replayed_events = runtime.event_stream.replay_after(0).events
    ui_event_sequence = [
        {
            "sequence": event.sequence,
            "type": event.event_type,
            "aggregate_version": event.aggregate_version,
            "correlation_id": event.correlation_id,
            "payload": event.payload,
        }
        for event in replayed_events
    ]
    position_snapshot = runtime.position.get_snapshot()
    order_state = None
    if runtime.order is not None:
        order_state = {
            "client_order_id": runtime.order.client_order_id,
            "status": None if runtime.order.status is None else runtime.order.status.value,
            "filled_quantity": _normalize_decimal(runtime.order.filled_quantity),
            "unapplied_fill_count": len(runtime.order.unapplied_fills),
        }

    return {
        "scenario_id": scenario_id,
        "category": category,
        "fault_kind": fault_kind,
        "evidence_scope": EVIDENCE_SCOPE,
        "owner": runtime.owner,
        "state": runtime.state,
        "new_order_allowed": runtime.new_order_allowed,
        "recovery_action": runtime.recovery_action,
        "order_mutation_count": runtime.order_mutation_count,
        "order": order_state,
        "position": {
            "status": position_snapshot.status.value,
            "owner": None if position_snapshot.owner is None else position_snapshot.owner.value,
            "quantity": _normalize_decimal(position_snapshot.quantity),
            "cost_basis": _normalize_decimal(position_snapshot.cost_basis),
        },
        "trades": [
            _normalize_trade(trade_value)
            for trade_value in runtime.trade_history.trades
        ],
        "ui_event_sequence": ui_event_sequence,
        "network_mutation_port_calls": 0,
        "private_action_calls": 0,
        "input_step_count": len(input_trace),
    }  # offline contract evidence를 production/Testnet E2E 성공으로 확대 해석하지 않는다.


def _validate_expected_outcome(
    expected_value: object,
    policy: FaultPolicy,
    location: str,
) -> None:
    """
    함수 이름: _validate_expected_outcome()
    기능: fixture matrix 열이 code-owned fault policy와 정확히 같은지 검증한다.
    인자: expected_value -> scenario expected object
        policy -> fault kind의 authoritative policy
        location -> 오류에 표시할 scenario 위치
    반환값: 없음
    작성 날짜: 2026/08/25
    """
    # Fixture 열을 typed 값으로 정규화해 code-owned policy와 tuple 단위로 비교한다.
    expected = _require_exact_keys(
        expected_value,
        {"owner", "state", "new_order_allowed", "recovery_action"},
        f"{location}.expected",
    )
    if type(expected["new_order_allowed"]) is not bool:
        raise DeterministicReplayError(f"{location}.expected.new_order_allowed is invalid")
    actual_contract = (
        _require_text(expected["owner"], f"{location}.expected.owner"),
        _require_identifier(expected["state"], f"{location}.expected.state"),
        expected["new_order_allowed"],
        _require_identifier(
            expected["recovery_action"],
            f"{location}.expected.recovery_action",
        ),
    )
    policy_contract = (
        policy.owner,
        policy.state,
        policy.new_order_allowed,
        policy.recovery_action,
    )
    if actual_contract != policy_contract:
        raise DeterministicReplayError(f"{location}.expected mismatched fault policy")


def replay_trace(trace_path: Path) -> dict[str, object]:
    """
    함수 이름: replay_trace()
    기능: canonical fixture 전체를 replay하고 scenario 및 aggregate expected digest를 검증한다.
    인자: trace_path -> canonical fault trace JSON 경로
    반환값: byte-stable replay result object
    작성 날짜: 2026/08/25
    """
    trace_document = _load_trace_document(trace_path)
    if trace_document["schema_version"] != TRACE_SCHEMA_VERSION:
        raise DeterministicReplayError("trace schema_version is unsupported")
    if trace_document["record_type"] != TRACE_RECORD_TYPE:
        raise DeterministicReplayError("trace record_type is invalid")
    trace_id = _require_scenario_id(trace_document["trace_id"], "trace.trace_id")
    if trace_document["evidence_scope"] != EVIDENCE_SCOPE:
        raise DeterministicReplayError("trace evidence_scope is invalid")
    expected_replay_digest = _require_sha256(
        trace_document["expected_replay_digest"],
        "trace.expected_replay_digest",
    )
    scenario_values = trace_document["scenarios"]
    if not isinstance(scenario_values, list):
        raise DeterministicReplayError("trace.scenarios must be a list")
    if not scenario_values or len(scenario_values) > MAXIMUM_SCENARIOS:
        raise DeterministicReplayError("trace.scenarios count is outside the allowed range")

    # 행 ID와 fault kind를 중복 없이 replay하고 각 expected digest를 즉시 대조한다.
    scenario_ids: set[str] = set()
    observed_fault_kinds: set[str] = set()
    normalized_scenarios: list[dict[str, object]] = []
    for scenario_index, scenario_value in enumerate(scenario_values):
        location = f"trace.scenarios[{scenario_index}]"
        scenario = _require_exact_keys(
            scenario_value,
            {
                "scenario_id",
                "category",
                "fault_kind",
                "expected",
                "input_trace",
                "expected_normalized_digest",
            },
            location,
        )
        scenario_id = _require_scenario_id(scenario["scenario_id"], f"{location}.scenario_id")
        category = _require_identifier(scenario["category"], f"{location}.category")
        fault_kind = _require_identifier(scenario["fault_kind"], f"{location}.fault_kind")
        if scenario_id in scenario_ids or fault_kind in observed_fault_kinds:
            raise DeterministicReplayError("trace scenario IDs and fault kinds must be unique")
        if fault_kind not in FAULT_POLICIES:
            raise DeterministicReplayError(f"{location}.fault_kind is unsupported")
        scenario_ids.add(scenario_id)
        observed_fault_kinds.add(fault_kind)
        policy = FAULT_POLICIES[fault_kind]
        _validate_expected_outcome(scenario["expected"], policy, location)
        input_trace = scenario["input_trace"]
        if not isinstance(input_trace, list) or not input_trace:
            raise DeterministicReplayError(f"{location}.input_trace must be a non-empty list")
        if len(input_trace) > MAXIMUM_STEPS_PER_SCENARIO:
            raise DeterministicReplayError(f"{location}.input_trace exceeds the step limit")

        scenario_outcome = _build_scenario_outcome(
            scenario_id,
            category,
            fault_kind,
            input_trace,
        )
        scenario_digest = _calculate_digest(scenario_outcome)
        expected_scenario_digest = _require_sha256(
            scenario["expected_normalized_digest"],
            f"{location}.expected_normalized_digest",
        )
        if scenario_digest != expected_scenario_digest:
            raise DeterministicReplayError(f"scenario {scenario_id} digest mismatched")
        scenario_outcome["normalized_digest"] = scenario_digest
        normalized_scenarios.append(scenario_outcome)

    if observed_fault_kinds != set(FAULT_POLICIES):
        raise DeterministicReplayError("trace does not cover the complete P13-05 fault matrix")

    # Aggregate digest는 scenario 순서를 포함한 전체 normalized output에 결합한다.
    replay_core: dict[str, object] = {
        "schema_version": TRACE_SCHEMA_VERSION,
        "record_type": REPLAY_RECORD_TYPE,
        "trace_id": trace_id,
        "evidence_scope": EVIDENCE_SCOPE,
        "scenarios": normalized_scenarios,
    }
    replay_digest = _calculate_digest(replay_core)
    if replay_digest != expected_replay_digest:
        raise DeterministicReplayError("aggregate replay digest mismatched")
    replay_core["normalized_digest"] = replay_digest

    return replay_core  # expected와 일치한 immutable 의미만 caller가 직렬화한다.


def replay_trace_bytes(trace_path: Path, *, repeat: int = 1) -> bytes:
    """
    함수 이름: replay_trace_bytes()
    기능: 동일 fixture를 반복 실행해 byte equality를 검사하고 canonical 결과 bytes를 반환한다.
    인자: trace_path -> canonical fault trace JSON 경로
        repeat -> 같은 process에서 replay할 양의 횟수
    반환값: byte-stable canonical JSON bytes
    작성 날짜: 2026/08/25
    """
    if isinstance(repeat, bool) or not isinstance(repeat, int) or repeat <= 0:
        raise ValueError("repeat must be a positive integer")
    first_result = _canonical_json_bytes(replay_trace(trace_path))
    for _repeat_index in range(1, repeat):
        repeated_result = _canonical_json_bytes(replay_trace(trace_path))
        if repeated_result != first_result:
            raise DeterministicReplayError("repeated replay output is not byte-stable")

    return first_result  # 모든 반복이 같은 byte를 만든 뒤 최초 결과만 공개한다.


def build_argument_parser() -> argparse.ArgumentParser:
    """
    함수 이름: build_argument_parser()
    기능: fixture 경로와 반복 횟수를 받는 deterministic replay CLI parser를 생성한다.
    인자: 없음
    반환값: 구성된 ArgumentParser
    작성 날짜: 2026/08/25
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace_path", type=Path)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="검증된 digest만 출력합니다.",
    )

    return parser  # 실행 정책은 main에서 적용하고 parser는 입력 문법만 소유한다.


def main(arguments: Sequence[str] | None = None) -> int:
    """
    함수 이름: main()
    기능: canonical replay를 반복 검증하고 JSON 또는 digest를 stdout에 출력한다.
    인자: arguments -> CLI 인자 또는 process argv를 사용하면 None
    반환값: 성공 0, schema/replay 실패 1
    작성 날짜: 2026/08/25
    """
    parser = build_argument_parser()
    parsed_arguments = parser.parse_args(arguments)
    try:
        replay_bytes = replay_trace_bytes(
            parsed_arguments.trace_path,
            repeat=parsed_arguments.repeat,
        )
    except (DeterministicReplayError, OSError, TypeError, ValueError) as error:
        print(f"phase13-replay: ERROR: {error}", file=sys.stderr)
        return 1
    if parsed_arguments.quiet:
        replay_object = json.loads(replay_bytes)
        print(replay_object["normalized_digest"])
    else:
        sys.stdout.buffer.write(replay_bytes)  # 이미 newline을 가진 canonical evidence를 그대로 쓴다.

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
