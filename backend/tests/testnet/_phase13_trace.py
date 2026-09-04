"""Phase 13 public Case 2 actual Testnet trace를 secret 없이 canonicalize한다."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import hashlib
import json
import re
from uuid import UUID

from binance_auto_trader.adapters.binance.spot_rest_client import (
    _MAXIMUM_ORDER_PREPARATION_AGE,
)


# 새 actual evidence는 v3만 생성하되 보존한 v2 artifact의 offline 검증 계약은 유지한다.
PHASE13_PUBLIC_TRACE_SCHEMA_VERSION = 3
_SUPPORTED_PHASE13_PUBLIC_TRACE_SCHEMA_VERSIONS = frozenset({2, 3})
PHASE13_PUBLIC_TRACE_RECORD_TYPE = "phase13_public_case2_testnet_trace"
PHASE13_PUBLIC_TRACE_OUTCOMES = frozenset(
    {"SUCCESS", "NO_SIGNAL", "BLOCKED", "FAILED"}
)
PHASE13_PUBLIC_TRACE_ABSOLUTE_MAX_NOTIONAL = Decimal("100")
_MAXIMUM_EVENT_COUNT = 2048
_MAXIMUM_TEXT_LENGTH = 512
_MAXIMUM_INTEGER = 9_223_372_036_854_775_807
# Controller가 실제 소비하는 same-order scheduled REST query 예산만 별도로 제한한다.
_MAXIMUM_ORDER_QUERY_TRACE_COUNT = 4
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_PLAIN_DECIMAL_PATTERN = re.compile(
    r"-?(?:0|[1-9][0-9]{0,37})(?:\.[0-9]{1,18})?\Z"
)
_UTC_TIMESTAMP_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{3,6})?Z\Z"
)
_KLINE_SOURCE_PATTERN = re.compile(
    r"kline:(?P<symbol>[A-Z0-9]+):(?P<interval>[0-9A-Za-z]+):"
    r"(?P<open_time>[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z):"
    r"(?P<event_time>[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z):"
    r"(?P<close_state>open|closed)\Z"
)
_PHASE13_CLIENT_ORDER_ID_PATTERN = re.compile(
    r"bat-[0-9a-f]{24}-0\Z"
)
_CANONICAL_NONNEGATIVE_INTEGER_PATTERN = re.compile(
    r"(?:0|[1-9][0-9]*)\Z"
)
_SECRET_KEY_FRAGMENT_PATTERN = re.compile(
    r"(?:api[_-]?(?:key|secret)|credential|private[_-]?key|signature|"
    r"raw[_-]?(?:credential|header)s?|headers?|authorization|cookie|"
    r"session[_-]?token|query[_-]?(?:params?|parameters?))",
    re.IGNORECASE,
)
_SECRET_VALUE_PATTERN = re.compile(
    r"(?:-----BEGIN [A-Z ]+ PRIVATE KEY-----|"
    r"\bsk-[A-Za-z0-9_-]{12,}|"
    r"\bX-MBX-APIKEY\b|"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"(?:api[_-]?(?:key|secret)|credential|private[_-]?key|signature|"
    r"authorization|cookie|session[_-]?token|query[_-]?(?:params?|parameters?))"
    r"\s*[:=]|"
    r"https?://[^\s?]+\?[^\s=]+=[^\s]+)",
    re.IGNORECASE,
)

_TRACE_BODY_FIELDS = frozenset(
    {
        "schema_version",
        "record_type",
        "outcome",
        "typed_reason",
        "run_id",
        "timestamps",
        "preflight",
        "immutable_decision_fingerprint",
        "public_market_events",
        "public_account_events",
        "order_attempts",
        "order_execution_traces",
        "submit_time_filter_evidence",
        "order_results",
        "baseline_history_sha256",
        "baseline_history_count",
        "run_durable_trades",
        "transport_ui_event_batch",
        "recovery",
        "final_state",
    }
)
_TRACE_DOCUMENT_FIELDS = _TRACE_BODY_FIELDS | {"trace_sha256"}
_TIMESTAMP_FIELDS = frozenset({"started_at", "completed_at"})
_PREFLIGHT_FIELDS_V2 = frozenset(
    {
        "endpoint_set",
        "symbol",
        "can_trade",
        "account_stream_status",
        "market_stream_status",
        "account_version",
        "verified_at",
        "commission_policy",
        "fresh_filters",
        "account_asset_filters",
        "account_filters_observed_at",
        "reference_price",
        "reference_price_observed_at",
        "position_quantity",
        "pending_order_count",
        "unknown_order_count",
        "matching_open_order_count",
    }
)
_ACCOUNT_STATE_EVIDENCE_FIELDS = frozenset(
    {
        "account_relevant_filters",
        "public_relevant_filters",
        "account_open_orders_observed_at",
        "account_open_orders_verified_empty",
        "account_open_order_lists_observed_at",
        "account_open_order_lists_verified_empty",
    }
)
_PREFLIGHT_FIELDS_V3 = _PREFLIGHT_FIELDS_V2 | _ACCOUNT_STATE_EVIDENCE_FIELDS
_COMMISSION_POLICY_FIELDS = frozenset(
    {
        "symbol",
        "enabled_for_account",
        "enabled_for_symbol",
        "discount_asset",
        "discount_rate",
        "standard_market_buy_rate",
        "special_market_buy_rate",
        "tax_market_buy_rate",
        "market_buy_received_asset_commission_rate",
        "can_charge_discount_asset",
    }
)
_FRESH_FILTER_RULE_FIELDS = frozenset(
    {
        "symbol_status",
        "base_asset",
        "quote_asset",
        "base_asset_precision",
        "is_spot_trading_allowed",
        "market_order_allowed",
        "lot_size_minimum_quantity",
        "lot_size_maximum_quantity",
        "lot_size_step_size",
        "market_lot_size_minimum_quantity",
        "market_lot_size_maximum_quantity",
        "market_lot_size_step_size",
        "minimum_notional",
        "maximum_notional",
        "maximum_position",
    }
)
_FRESH_FILTER_FIELDS = _FRESH_FILTER_RULE_FIELDS | {"observed_at"}
_ACCOUNT_ASSET_FILTER_FIELDS = frozenset(
    {"filter_type", "asset", "maximum_quantity"}
)
_ACCOUNT_ORDER_COUNT_FILTER_FIELDS = frozenset(
    {"filter_type", "maximum_count"}
)
_ACCOUNT_QUANTITY_FILTER_FIELDS = frozenset(
    {
        "filter_type",
        "minimum_quantity",
        "maximum_quantity",
        "step_size",
    }
)
_ACCOUNT_NOTIONAL_FILTER_FIELDS = frozenset(
    {
        "filter_type",
        "minimum_notional",
        "maximum_notional",
        "apply_minimum_to_market",
        "apply_maximum_to_market",
        "average_price_minutes",
    }
)
_ACCOUNT_RELEVANT_FILTER_FIELDS = frozenset(
    {
        "symbol",
        "exchange_order_count_filters",
        "symbol_order_count_filters",
        "symbol_quantity_filters",
        "symbol_notional_filters",
        "symbol_maximum_position",
        "passive_symbol_filter_types",
        "asset_filters",
    }
)
_EXCHANGE_ORDER_COUNT_FILTER_TYPES = frozenset(
    {
        "EXCHANGE_MAX_NUM_ORDERS",
        "EXCHANGE_MAX_NUM_ALGO_ORDERS",
        "EXCHANGE_MAX_NUM_ICEBERG_ORDERS",
        "EXCHANGE_MAX_NUM_ORDER_LISTS",
    }
)
_SYMBOL_ORDER_COUNT_FILTER_TYPES = frozenset(
    {
        "MAX_NUM_ORDERS",
        "MAX_NUM_ALGO_ORDERS",
        "MAX_NUM_ICEBERG_ORDERS",
        "MAX_NUM_ORDER_AMENDS",
        "MAX_NUM_ORDER_LISTS",
    }
)
_PASSIVE_SYMBOL_FILTER_TYPES = frozenset(
    {
        "PRICE_FILTER",
        "PERCENT_PRICE",
        "PERCENT_PRICE_BY_SIDE",
        "ICEBERG_PARTS",
        "TRAILING_DELTA",
    }
)
_REFERENCE_PRICE_FIELDS = frozenset(
    {"symbol", "price", "exchange_timestamp"}
)
_SUBMIT_TIME_FILTER_EVIDENCE_FIELDS_V2 = frozenset(
    {
        "sequence",
        "intent_id",
        "client_order_id",
        "side",
        "observed_at",
        "rules",
        "account_asset_filters",
        "account_filters_observed_at",
        "reference_price",
        "reference_price_observed_at",
    }
)
_SUBMIT_TIME_FILTER_EVIDENCE_FIELDS_V3 = (
    _SUBMIT_TIME_FILTER_EVIDENCE_FIELDS_V2 | _ACCOUNT_STATE_EVIDENCE_FIELDS
)
_DECISION_FIELDS = frozenset(
    {
        "source_event_id",
        "source_kline_identity",
        "source_event_time",
        "evaluation_id",
        "market_version",
        "account_version",
        "context_version",
        "policy_version",
        "regime",
        "action_type",
        "side",
        "strategy",
        "intent_id",
        "client_order_id",
        "decision_price",
        "final_submitted_quantity",
        "final_notional",
        "configured_cap",
    }
)
_MARKET_EVENT_FIELDS = frozenset(
    {
        "sequence",
        "message_id",
        "event_type",
        "source_event_id",
        "source_kline_identity",
        "source_event_time",
        "market_version",
        "context_version",
        "evaluation_id",
        "regime",
        "action_type",
        "side",
        "strategy",
    }
)
_ACCOUNT_EVENT_FIELDS = frozenset(
    {
        "sequence",
        "message_id",
        "event_type",
        "source_event_id",
        "source_event_time",
        "account_version",
        "asset",
        "free_quantity",
        "locked_quantity",
    }
)
_ORDER_ATTEMPT_FIELDS = frozenset(
    {
        "sequence",
        "attempted_at",
        "evaluation_id",
        "intent_id",
        "client_order_id",
        "submission_attempt",
        "symbol",
        "regime",
        "strategy",
        "action_type",
        "side",
        "exit_reason",
        "decision_price",
        "final_submitted_quantity",
        "final_notional",
        "configured_cap",
        "policy_version",
    }
)
_ORDER_EXECUTION_TRACE_FIELDS = frozenset(
    {
        "sequence",
        "intent_id",
        "client_order_id",
        "side",
        "entries",
    }
)
_ORDER_EXECUTION_TRACE_ENTRY_FIELDS = frozenset(
    {
        "sequence",
        "message_id",
        "command_event_id",
        "order_id",
        "context_version_before",
        "context_version_after",
        "result",
        "failure_code",
    }
)
_ORDER_TRACE_MESSAGE_IDS = frozenset(
    {
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
    }
)
_BUY_ORDER_TRACE_PREFIX = ("1", "2", "3", "4", "5.1", "5", "6", "6.1", "7")
_SELL_ORDER_TRACE_PREFIX = ("1", "2", "3", "4", "5", "6", "6.1", "7")
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
# Terminal fill 적용과 durable 후처리는 최종 OrderResult의 exchange ID 없이 성공할 수 없다.
_ORDER_TRACE_EXCHANGE_ID_REQUIRED_MESSAGE_IDS = frozenset(
    _BUY_FILL_APPLICATION_TRACE
    + _SELL_FILL_APPLICATION_TRACE
    + _BUY_ORDER_TRACE_SUFFIX
    + _SELL_ORDER_TRACE_SUFFIX
)
# Exchange 응답 전 submit prefix는 미래 order ID를 미리 주장할 수 없다.
_ORDER_TRACE_PRE_EXCHANGE_ID_MESSAGE_IDS = frozenset(
    {"1", "2", "3", "4", "5.1", "5", "6", "6.1"}
)
_ORDER_RESULT_FIELDS = frozenset(
    {
        "sequence",
        "observed_at",
        "intent_id",
        "client_order_id",
        "exchange_order_id",
        "status",
        "failure_code",
        "incremental_fills",
    }
)
_FILL_FIELDS = frozenset(
    {
        "fill_id",
        "durable_trade_id",
        "event_time",
        "price",
        "quantity",
        "quote_amount",
        "fee_amount",
        "fee_asset",
        "fee_quote_amount",
    }
)
_TRANSPORT_UI_BATCH_FIELDS = frozenset(
    {"transport_session_id", "events"}
)
_TRANSPORT_UI_EVENT_FIELDS = frozenset(
    {
        "event_id",
        "transport_sequence",
        "aggregate",
        "event_type",
        "aggregate_version",
        "related_id",
        "published_at",
    }
)
_RECOVERY_FIELDS = frozenset(
    {
        "required",
        "attempted",
        "outcome",
        "intent_id",
        "client_order_id",
        "authoritative_position_quantity",
        "effective_free_quantity",
        "submitted_quantity",
        "final_position_quantity",
        "pending_order_count",
        "matching_open_order_count",
        "duplicate_order_count",
        "duplicate_trade_count",
    }
)
_FINAL_STATE_FIELDS = frozenset(
    {
        "position_quantity",
        "pending_order_count",
        "unknown_order_count",
        "matching_open_order_count",
        "actual_order_count",
        "duplicate_order_count",
        "duplicate_trade_count",
        "verified_at",
        "fresh_runtime_session_id",
        "performance",
    }
)
_PERFORMANCE_FIELDS = frozenset(
    {
        "baseline_trade_count",
        "run_trade_count",
        "total_trade_count",
        "baseline_realized_profit_loss",
        "run_realized_profit_loss",
        "total_realized_profit_loss",
        "baseline_fee_quote",
        "run_fee_quote",
        "total_fee_quote",
    }
)
_RUN_DURABLE_TRADE_FIELDS = frozenset(
    {
        "trade_id",
        "client_order_id",
        "exchange_order_id",
        "symbol",
        "side",
        "regime",
        "strategy",
        "executed_quantity",
        "executed_amount",
        "average_fill_price",
        "fee_amount",
        "fee_asset",
        "fee_quote_amount",
        "realized_profit_loss",
        "exit_reason",
        "executed_at",
    }
)
_ORDER_RESULT_STATUSES = frozenset(
    {
        "NEW",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELED",
        "REJECTED",
        "EXPIRED",
        "UNKNOWN",
    }
)
_RECOVERY_OUTCOMES = frozenset(
    {"NOT_REQUIRED", "BLOCKED", "SUCCESS", "FAILED"}
)
_TERMINAL_ORDER_RESULT_STATUSES = frozenset(
    {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}
)
_ACTIVE_ORDER_STATUS_PROGRESS = {
    "NEW": 1,
    "PARTIALLY_FILLED": 2,
    "FILLED": 3,
}
_TRANSPORT_EVENT_AGGREGATES = {
    "ACCOUNT_UPDATED": "ACCOUNT",
    "ORDER_EXECUTED": "TRADE_HISTORY",
    "PERFORMANCE_UPDATED": "TRADE_HISTORY",
    "TRADING_SESSION_UPDATED": "TRADING_SESSION",
}


class PhaseThirteenPublicTraceValidationError(RuntimeError):
    """
    클래스 이름: PhaseThirteenPublicTraceValidationError
    기능: Phase 13 public Case 2 trace의 schema, redaction 또는 digest 위반을 나타낸다.
    작성 날짜: 2026/08/31
    """

    code = "PHASE13_PUBLIC_TRACE_INVALID"


def _normalize_forbidden_values(
    forbidden_values: Sequence[str],
) -> tuple[str, ...]:
    """
    함수 이름: _normalize_forbidden_values()
    기능: 실제 credential canary를 오류 메시지에 반사하지 않는 검색 tuple로 검증한다.
    인자: forbidden_values -> trace 어디에도 나타나면 안 되는 실제 secret 원문 모음
    반환값: 중복을 제거한 secret 원문 tuple
    작성 날짜: 2026/08/31
    """
    if isinstance(forbidden_values, (str, bytes)) or not isinstance(
        forbidden_values,
        Sequence,
    ):
        raise TypeError("forbidden_values must be a sequence of strings")

    # 빈 canary는 모든 문자열과 일치하므로 허용하지 않고 동일 값은 한 번만 검색한다.
    normalized_values: list[str] = []
    for forbidden_value in forbidden_values:
        if not isinstance(forbidden_value, str):
            raise TypeError("forbidden_values must contain only strings")
        if not forbidden_value:
            raise ValueError("forbidden_values must not contain empty strings")
        if forbidden_value not in normalized_values:
            normalized_values.append(forbidden_value)

    return tuple(normalized_values)  # 순서는 caller가 제공한 canary 우선순위를 보존한다.


def _reject_secret_material(
    value: object,
    forbidden_values: tuple[str, ...],
    active_container_ids: set[int] | None = None,
) -> None:
    """
    함수 이름: _reject_secret_material()
    기능: secret-like key·value, 실제 canary, float와 순환 JSON tree를 재귀적으로 거부한다.
    인자: value -> 검사할 trace JSON tree
        forbidden_values -> trace에 포함할 수 없는 실제 secret 원문 tuple
        active_container_ids -> 현재 재귀 경로의 container identity 집합
    반환값: 없음
    작성 날짜: 2026/08/31
    """
    selected_container_ids = (
        set() if active_container_ids is None else active_container_ids
    )

    # Mapping key를 값보다 먼저 검사해 unknown secret field도 schema 오류보다 우선 차단한다.
    if isinstance(value, Mapping):
        container_identity = id(value)
        if container_identity in selected_container_ids:
            raise PhaseThirteenPublicTraceValidationError(
                "trace contains a cyclic container"
            )
        selected_container_ids.add(container_identity)
        try:
            for object_key, object_value in value.items():
                if not isinstance(object_key, str):
                    raise PhaseThirteenPublicTraceValidationError(
                        "trace object keys must be strings"
                    )
                if _SECRET_KEY_FRAGMENT_PATTERN.search(object_key) is not None:
                    raise PhaseThirteenPublicTraceValidationError(
                        "trace contains forbidden secret-like material"
                    )
                _reject_secret_material(
                    object_value,
                    forbidden_values,
                    selected_container_ids,
                )
        finally:
            selected_container_ids.remove(container_identity)
        return

    # JSON array도 같은 active-path cycle 방어를 적용하고 tuple 같은 비표준 container는 허용하지 않는다.
    if isinstance(value, list):
        container_identity = id(value)
        if container_identity in selected_container_ids:
            raise PhaseThirteenPublicTraceValidationError(
                "trace contains a cyclic container"
            )
        selected_container_ids.add(container_identity)
        try:
            for item_value in value:
                _reject_secret_material(
                    item_value,
                    forbidden_values,
                    selected_container_ids,
                )
        finally:
            selected_container_ids.remove(container_identity)
        return

    # 금융값은 plain decimal string만 사용하고 Python float·Decimal object의 NaN/Infinity 우회를 막는다.
    if isinstance(value, (float, Decimal)):
        raise PhaseThirteenPublicTraceValidationError(
            "trace must not contain non-canonical numeric values"
        )
    if isinstance(value, str):
        if _SECRET_VALUE_PATTERN.search(value) is not None:
            raise PhaseThirteenPublicTraceValidationError(
                "trace contains forbidden secret-like material"
            )
        if any(forbidden_value in value for forbidden_value in forbidden_values):
            raise PhaseThirteenPublicTraceValidationError(
                "trace contains forbidden secret-like material"
            )


def _require_exact_mapping(
    value: object,
    expected_fields: frozenset[str],
    location: str,
) -> Mapping[str, object]:
    """
    함수 이름: _require_exact_mapping()
    기능: JSON object의 field 집합을 지정 schema와 정확히 일치시킨다.
    인자: value -> 검증할 JSON 값
        expected_fields -> 허용할 exact field 집합
        location -> secret을 포함하지 않는 고정 schema 위치
    반환값: 검증된 mapping
    작성 날짜: 2026/08/31
    """
    if not isinstance(value, Mapping) or set(value) != set(expected_fields):
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} has an invalid exact schema"
        )

    return value  # unknown field를 무시하지 않아 schema drift가 digest에 섞이지 않는다.


def _require_list(value: object, location: str) -> list[object]:
    """
    함수 이름: _require_list()
    기능: bounded JSON array만 event sequence로 허용한다.
    인자: value -> 검증할 JSON 값
        location -> secret을 포함하지 않는 고정 schema 위치
    반환값: 검증된 list
    작성 날짜: 2026/08/31
    """
    if not isinstance(value, list) or len(value) > _MAXIMUM_EVENT_COUNT:
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} must be a bounded JSON array"
        )

    return value  # tuple 자동 변환 없이 실제 artifact JSON array identity를 고정한다.


def _require_canonical_text(
    value: object,
    location: str,
    *,
    allow_none: bool = False,
) -> str | None:
    """
    함수 이름: _require_canonical_text()
    기능: non-empty·trimmed·control-free bounded 문자열 또는 명시적 None을 검증한다.
    인자: value -> 검증할 JSON 값
        location -> secret을 포함하지 않는 고정 schema 위치
        allow_none -> None을 schema 값으로 허용할지 여부
    반환값: 검증된 문자열 또는 None
    작성 날짜: 2026/08/31
    """
    if allow_none and value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _MAXIMUM_TEXT_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} must be canonical text"
        )

    return value  # 식별자를 trim하거나 문자열로 자동 변환하지 않는다.


def _require_identifier(
    value: object,
    location: str,
    *,
    allow_none: bool = False,
) -> str | None:
    """
    함수 이름: _require_identifier()
    기능: enum·상태 값을 canonical 대문자 identifier 또는 명시적 None으로 검증한다.
    인자: value -> 검증할 JSON 값
        location -> secret을 포함하지 않는 고정 schema 위치
        allow_none -> None을 schema 값으로 허용할지 여부
    반환값: 검증된 identifier 또는 None
    작성 날짜: 2026/08/31
    """
    if allow_none and value is None:
        return None
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} must be a canonical identifier"
        )

    return value  # enum 이름의 대소문자나 구분자를 자동 보정하지 않는다.


def _require_uuid4(value: object, location: str) -> str:
    """
    함수 이름: _require_uuid4()
    기능: run·transport·event identity를 lowercase canonical UUIDv4 문자열로 검증한다.
    인자: value -> 검증할 JSON 값
        location -> secret을 포함하지 않는 고정 schema 위치
    반환값: 검증된 UUIDv4 문자열
    작성 날짜: 2026/08/31
    """
    uuid_text = _require_canonical_text(value, location)
    try:
        parsed_uuid = UUID(str(uuid_text))
    except (ValueError, AttributeError) as error:
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} must be a canonical UUIDv4"
        ) from error
    if parsed_uuid.version != 4 or str(parsed_uuid) != uuid_text:
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} must be a canonical UUIDv4"
        )

    return uuid_text  # UUID text를 새 값으로 교체하지 않아 artifact identity가 그대로 유지된다.


def _parse_public_market_command_event(
    command_event_id: str,
) -> dict[str, object]:
    """
    함수 이름: _parse_public_market_command_event()
    기능: production public market command ID의 version과 Kline provenance를 exact parsing한다.
    인자: command_event_id -> `market:version:source` canonical identity
    반환값: source event, 대표 Kline identity/time과 market version mapping
    작성 날짜: 2026/09/01
    """
    if not isinstance(command_event_id, str) or not command_event_id:
        raise TypeError("public market command identity is invalid")
    command_parts = command_event_id.split(":", 2)
    if len(command_parts) != 3 or command_parts[0] != "market":
        raise ValueError("public market command identity is invalid")
    try:
        market_version = int(command_parts[1])
    except ValueError as error:
        raise ValueError("public market version is invalid") from error
    if market_version < 1 or str(market_version) != command_parts[1]:
        raise ValueError("public market version is invalid")

    # Atomic boundary는 batch prefix 뒤의 모든 Kline component를 독립 exact parser로 확인한다.
    source_event_id = command_parts[2]
    is_batch = source_event_id.startswith("kline-batch|")
    source_components = (
        source_event_id.split("|")[1:]
        if is_batch
        else [source_event_id]
    )
    if (
        not source_components
        or any(not component for component in source_components)
        or (is_batch and len(source_components) < 2)
        or len(set(source_components)) != len(source_components)
    ):
        raise ValueError("public market source list is invalid")
    interval_durations = {
        "1m": timedelta(minutes=1),
        "30m": timedelta(minutes=30),
        "4h": timedelta(hours=4),
        "1d": timedelta(days=1),
    }
    parsed_components: list[dict[str, object]] = []
    for source_component in source_components:
        component_match = _KLINE_SOURCE_PATTERN.fullmatch(source_component)
        if component_match is None:
            raise ValueError("public market Kline source is invalid")
        parsed_component = component_match.groupdict()
        interval = parsed_component["interval"]
        if (
            parsed_component["symbol"] != "ETHUSDT"
            or interval not in interval_durations
        ):
            raise ValueError("public market Kline scope is invalid")
        try:
            open_time = datetime.fromisoformat(
                parsed_component["open_time"].replace("Z", "+00:00")
            )
            event_time = datetime.fromisoformat(
                parsed_component["event_time"].replace("Z", "+00:00")
            )
        except ValueError as error:
            raise ValueError("public market Kline time is invalid") from error
        close_boundary = open_time + interval_durations[interval]
        if event_time < open_time or (
            parsed_component["close_state"] == "closed"
            and event_time < close_boundary
        ):
            raise ValueError("public market Kline time is not causal")
        parsed_components.append(
            {
                **parsed_component,
                "open_timestamp": open_time,
                "event_timestamp": event_time,
                "close_boundary": close_boundary,
            }
        )

    # Production batch는 1m/30m close 뒤 UTC 경계에 필요한 4H·1D close/open pair만 붙인다.
    if is_batch:
        if len(parsed_components) not in {2, 4, 6}:
            raise ValueError("public market batch length is invalid")
        component_shapes = tuple(
            (component["interval"], component["close_state"])
            for component in parsed_components
        )
        expected_shapes = (("1m", "closed"), ("30m", "closed"))
        if len(parsed_components) >= 4:
            expected_shapes += (("4h", "closed"), ("4h", "open"))
        if len(parsed_components) == 6:
            expected_shapes += (("1d", "closed"), ("1d", "open"))
        if component_shapes != expected_shapes:
            raise ValueError("public market batch order is invalid")

        # 두 strategy close가 공유하는 30분 UTC 경계가 upper pair의 close/open에도 그대로 이어져야 한다.
        shared_boundary = parsed_components[0]["close_boundary"]
        if not isinstance(shared_boundary, datetime):
            raise AssertionError("parsed market boundary lost its datetime type")
        if (
            shared_boundary != parsed_components[1]["close_boundary"]
            or shared_boundary.minute not in {0, 30}
            or shared_boundary.second != 0
            or shared_boundary.microsecond != 0
        ):
            raise ValueError("public market batch boundary is invalid")
        four_hour_boundary = (
            shared_boundary.hour % 4 == 0
            and shared_boundary.minute == 0
        )
        one_day_boundary = (
            shared_boundary.hour == 0
            and shared_boundary.minute == 0
        )
        expected_component_count = (
            6 if one_day_boundary else 4 if four_hour_boundary else 2
        )
        if len(parsed_components) != expected_component_count:
            raise ValueError("public market batch omits a required UTC pair")
        for component_index in range(2, len(parsed_components), 2):
            closed_component = parsed_components[component_index]
            open_component = parsed_components[component_index + 1]
            if (
                closed_component["close_boundary"] != shared_boundary
                or open_component["open_timestamp"] != shared_boundary
                or closed_component["event_timestamp"] < shared_boundary
                or open_component["event_timestamp"] < shared_boundary
            ):
                raise ValueError("public market upper boundary pair is invalid")
    # Case 2 primary 30분봉이 있으면 대표 identity로 선택하고 없으면 첫 source를 쓴다.
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
    }  # Raw frame을 보존하지 않고 production normalized identity만 반환한다.


def _require_nonnegative_integer(value: object, location: str) -> int:
    """
    함수 이름: _require_nonnegative_integer()
    기능: bool을 제외한 bounded non-negative JSON integer를 검증한다.
    인자: value -> 검증할 JSON 값
        location -> secret을 포함하지 않는 고정 schema 위치
    반환값: 검증된 정수
    작성 날짜: 2026/08/31
    """
    if type(value) is not int or value < 0 or value > _MAXIMUM_INTEGER:
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} must be a non-negative integer"
        )

    return value  # True/False를 1/0 version으로 암묵 수용하지 않는다.


def _require_plain_decimal(
    value: object,
    location: str,
    *,
    minimum: Decimal | None = None,
    allow_none: bool = False,
) -> Decimal | None:
    """
    함수 이름: _require_plain_decimal()
    기능: exponent 없는 유한 decimal 문자열과 선택 하한을 손실 없이 검증한다.
    인자: value -> 검증할 JSON 값
        location -> secret을 포함하지 않는 고정 schema 위치
        minimum -> 허용할 최소 Decimal 또는 None
        allow_none -> None을 schema 값으로 허용할지 여부
    반환값: 검증된 Decimal 또는 None
    작성 날짜: 2026/08/31
    """
    if allow_none and value is None:
        return None
    if not isinstance(value, str) or _PLAIN_DECIMAL_PATTERN.fullmatch(value) is None:
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} must be a plain decimal string"
        )
    try:
        decimal_value = Decimal(value)
    except InvalidOperation as error:
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} must be a valid decimal string"
        ) from error
    if not decimal_value.is_finite() or (
        minimum is not None and decimal_value < minimum
    ):
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} is outside the allowed decimal range"
        )

    return decimal_value  # 원문 scale은 canonical bytes에 남기고 계산만 Decimal로 수행한다.


def _multiply_decimal128(
    multiplicand: Decimal,
    multiplier: Decimal,
) -> Decimal:
    """
    함수 이름: _multiply_decimal128()
    기능: trace 금융 곱셈을 production과 같은 precision 34, HALF_EVEN context에서 계산한다.
    인자: multiplicand -> 첫 번째 유한 Decimal
        multiplier -> 두 번째 유한 Decimal
    반환값: Decimal128 precision으로 계산한 곱
    작성 날짜: 2026/08/31
    """
    # Caller context와 무관하게 production 금융 곱셈의 precision·rounding을 한 블록에 고정한다.
    with localcontext() as decimal_context:
        decimal_context.prec = 34
        decimal_context.rounding = ROUND_HALF_EVEN
        return multiplicand * multiplier  # 두 operand를 한 Decimal128 연산에서 곱한다.


def _sum_decimal128(values: Sequence[Decimal]) -> Decimal:
    """
    함수 이름: _sum_decimal128()
    기능: trace 금융 합계를 production과 같은 precision 34, HALF_EVEN context에서 계산한다.
    인자: values -> 순서대로 합산할 유한 Decimal sequence
    반환값: Decimal128 precision으로 계산한 합계
    작성 날짜: 2026/08/31
    """
    with localcontext() as decimal_context:
        decimal_context.prec = 34
        decimal_context.rounding = ROUND_HALF_EVEN
        accumulated_value = Decimal("0")
        for decimal_value in values:
            accumulated_value += decimal_value

    return accumulated_value  # Fill/Trade 원래 순서를 유지해 production aggregate와 같은 rounding을 쓴다.


def _divide_decimal128(dividend: Decimal, divisor: Decimal) -> Decimal:
    """
    함수 이름: _divide_decimal128()
    기능: trace 평균 가격 나눗셈을 production과 같은 precision 34, HALF_EVEN context에서 계산한다.
    인자: dividend -> 나눌 유한 Decimal
        divisor -> 0이 아닌 유한 Decimal
    반환값: Decimal128 precision으로 계산한 몫
    작성 날짜: 2026/08/31
    """
    # 평균 가격 나눗셈도 caller context를 상속하지 않고 Decimal128 HALF_EVEN으로 수행한다.
    with localcontext() as decimal_context:
        decimal_context.prec = 34
        decimal_context.rounding = ROUND_HALF_EVEN
        return dividend / divisor  # 두 operand를 한 Decimal128 연산에서 나눈다.


def _require_utc_timestamp(value: object, location: str) -> datetime:
    """
    함수 이름: _require_utc_timestamp()
    기능: millisecond 또는 microsecond precision의 RFC 3339 UTC Z 시각을 검증한다.
    인자: value -> 검증할 JSON 값
        location -> secret을 포함하지 않는 고정 schema 위치
    반환값: timezone-aware UTC datetime
    작성 날짜: 2026/08/31
    """
    if not isinstance(value, str) or _UTC_TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} must be an RFC 3339 UTC timestamp"
        )
    try:
        timestamp = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as error:
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} must be a valid UTC timestamp"
        ) from error

    return timestamp.astimezone(timezone.utc)  # 다른 timezone 표현을 허용하지 않아 같은 instant의 bytes를 단일화한다.


def _require_contiguous_sequence(
    entries: list[object],
    expected_fields: frozenset[str],
    location: str,
) -> tuple[Mapping[str, object], ...]:
    """
    함수 이름: _require_contiguous_sequence()
    기능: event object schema와 1부터 시작하는 중복 없는 순서를 함께 검증한다.
    인자: entries -> 순서대로 기록된 JSON object list
        expected_fields -> 각 event의 exact field 집합
        location -> secret을 포함하지 않는 고정 sequence 위치
    반환값: 검증된 event mapping tuple
    작성 날짜: 2026/08/31
    """
    validated_entries: list[Mapping[str, object]] = []
    for expected_sequence, entry in enumerate(entries, start=1):
        validated_entry = _require_exact_mapping(
            entry,
            expected_fields,
            f"{location} entry",
        )
        if _require_nonnegative_integer(
            validated_entry["sequence"],
            f"{location} sequence",
        ) != expected_sequence:
            raise PhaseThirteenPublicTraceValidationError(
                f"{location} sequence must be contiguous"
            )
        validated_entries.append(validated_entry)

    return tuple(validated_entries)  # list 순서를 그대로 digest와 provenance 순서로 사용한다.


def _validate_timestamps(value: object) -> tuple[datetime, datetime]:
    """
    함수 이름: _validate_timestamps()
    기능: run 시작·종료 시각의 exact schema와 단조 순서를 검증한다.
    인자: value -> timestamps JSON object
    반환값: 시작과 종료 UTC datetime tuple
    작성 날짜: 2026/08/31
    """
    timestamps = _require_exact_mapping(value, _TIMESTAMP_FIELDS, "timestamps")
    started_at = _require_utc_timestamp(
        timestamps["started_at"],
        "timestamps started_at",
    )
    completed_at = _require_utc_timestamp(
        timestamps["completed_at"],
        "timestamps completed_at",
    )
    if completed_at < started_at:
        raise PhaseThirteenPublicTraceValidationError(
            "timestamps must be monotonic"
        )

    return started_at, completed_at  # 동일 instant의 zero-duration blocked trace도 허용한다.


def _validate_commission_policy(value: object) -> Mapping[str, object]:
    """
    함수 이름: _validate_commission_policy()
    기능: signed Testnet commission preflight를 raw 응답 없이 exact normalized 정책으로 검증한다.
    인자: value -> commission_policy JSON object
    반환값: 검증된 commission policy mapping
    작성 날짜: 2026/08/31
    """
    policy = _require_exact_mapping(
        value,
        _COMMISSION_POLICY_FIELDS,
        "preflight commission policy",
    )
    if policy["symbol"] != "ETHUSDT":
        raise PhaseThirteenPublicTraceValidationError(
            "commission policy must use ETHUSDT"
        )
    for field_name in ("enabled_for_account", "enabled_for_symbol"):
        if type(policy[field_name]) is not bool:
            raise PhaseThirteenPublicTraceValidationError(
                "commission enablement fields must be booleans"
            )
    discount_asset = _require_identifier(
        policy["discount_asset"],
        "commission discount asset",
        allow_none=True,
    )
    for field_name in (
        "discount_rate",
        "standard_market_buy_rate",
        "special_market_buy_rate",
        "tax_market_buy_rate",
        "market_buy_received_asset_commission_rate",
    ):
        _require_plain_decimal(
            policy[field_name],
            f"commission {field_name}",
            minimum=Decimal("0"),
        )
    if type(policy["can_charge_discount_asset"]) is not bool:
        raise PhaseThirteenPublicTraceValidationError(
            "commission charge flag must be a boolean"
        )

    # Derived discount-asset flag과 세 MARKET BUY rate 합을 기록값과 Decimal128로 대조한다.
    expected_discount_charge = (
        policy["enabled_for_account"]
        and policy["enabled_for_symbol"]
        and discount_asset is not None
    )
    if policy["can_charge_discount_asset"] != expected_discount_charge:
        raise PhaseThirteenPublicTraceValidationError(
            "commission discount policy is internally inconsistent"
        )
    component_rates = tuple(
        Decimal(str(policy[field_name]))
        for field_name in (
            "standard_market_buy_rate",
            "special_market_buy_rate",
            "tax_market_buy_rate",
        )
    )
    combined_rate = Decimal(
        str(policy["market_buy_received_asset_commission_rate"])
    )
    if _sum_decimal128(component_rates) != combined_rate:
        raise PhaseThirteenPublicTraceValidationError(
            "commission MARKET BUY rate does not match its components"
        )
    if expected_discount_charge or combined_rate != Decimal("0"):
        raise PhaseThirteenPublicTraceValidationError(
            "actual Testnet preflight requires a dust-free commission policy"
        )

    return policy  # API key와 raw 12-rate payload 대신 제출 안전성에 필요한 정책만 남긴다.


def _validate_fresh_filter_rule_values(
    filters: Mapping[str, object],
    location: str,
) -> None:
    """
    함수 이름: _validate_fresh_filter_rule_values()
    기능: preflight와 submit-time이 공유하는 ETHUSDT MARKET rule 값과 범위를 검증한다.
    인자: filters -> exact field 검사를 먼저 통과한 normalized filter rule mapping
        location -> secret을 포함하지 않는 고정 schema 위치
    반환값: 없음
    작성 날짜: 2026/08/31
    """
    if (
        filters["symbol_status"] != "TRADING"
        or filters["base_asset"] != "ETH"
        or filters["quote_asset"] != "USDT"
    ):
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} must describe trading ETHUSDT"
        )
    base_asset_precision = _require_nonnegative_integer(
        filters["base_asset_precision"],
        f"{location} base_asset_precision",
    )
    if base_asset_precision > 18:
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} base asset precision exceeds trace decimal precision"
        )
    if (
        type(filters["is_spot_trading_allowed"]) is not bool
        or type(filters["market_order_allowed"]) is not bool
        or not filters["is_spot_trading_allowed"]
        or not filters["market_order_allowed"]
    ):
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} must allow Spot MARKET orders"
        )

    # LOT_SIZE와 MARKET_LOT_SIZE를 float 없이 검증하며 0 step은 Binance 비활성 규칙으로 보존한다.
    quantity_field_names = (
        "lot_size_minimum_quantity",
        "lot_size_maximum_quantity",
        "lot_size_step_size",
        "market_lot_size_minimum_quantity",
        "market_lot_size_maximum_quantity",
        "market_lot_size_step_size",
    )
    for field_name in quantity_field_names:
        _require_plain_decimal(
            filters[field_name],
            f"{location} {field_name}",
            minimum=Decimal("0"),
        )
    for filter_prefix in ("lot_size", "market_lot_size"):
        minimum_quantity = Decimal(
            str(filters[f"{filter_prefix}_minimum_quantity"])
        )
        maximum_quantity = Decimal(
            str(filters[f"{filter_prefix}_maximum_quantity"])
        )
        if maximum_quantity > Decimal("0") and maximum_quantity < minimum_quantity:
            raise PhaseThirteenPublicTraceValidationError(
                f"{location} quantity filter range is invalid"
            )
    minimum_notional = _require_plain_decimal(
        filters["minimum_notional"],
        f"{location} minimum_notional",
        minimum=Decimal("0"),
        allow_none=True,
    )
    maximum_notional = _require_plain_decimal(
        filters["maximum_notional"],
        f"{location} maximum_notional",
        minimum=Decimal("0"),
        allow_none=True,
    )
    _require_plain_decimal(
        filters["maximum_position"],
        f"{location} maximum_position",
        minimum=Decimal("0"),
        allow_none=True,
    )
    if (
        minimum_notional is not None
        and maximum_notional is not None
        and maximum_notional < minimum_notional
    ):
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} notional filter range is invalid"
        )

    return None  # 공통 rule 검증은 caller의 preflight/submit-time timestamp 의미를 바꾸지 않는다.


def _validate_account_asset_filters(
    value: object,
    location: str,
) -> tuple[Mapping[str, object], ...]:
    """
    함수 이름: _validate_account_asset_filters()
    기능: signed myFilters의 MAX_ASSET만 exact normalized account filter tuple로 검증한다.
    인자: value -> account_asset_filters JSON array
        location -> secret을 포함하지 않는 고정 schema 위치
    반환값: 검증된 account asset filter mapping tuple
    작성 날짜: 2026/08/31
    """
    filter_entries = _require_list(value, location)
    validated_filters: list[Mapping[str, object]] = []
    observed_assets: set[str] = set()

    # Symbol-scoped signed 응답은 ETH/USDT MAX_ASSET만 허용하고 중복 asset은 거부한다.
    for filter_index, filter_value in enumerate(filter_entries):
        filter_location = f"{location}[{filter_index}]"
        account_filter = _require_exact_mapping(
            filter_value,
            _ACCOUNT_ASSET_FILTER_FIELDS,
            filter_location,
        )
        if account_filter["filter_type"] != "MAX_ASSET":
            raise PhaseThirteenPublicTraceValidationError(
                f"{filter_location} must be a MAX_ASSET filter"
            )
        asset = _require_identifier(
            account_filter["asset"],
            f"{filter_location} asset",
        )
        if asset not in {"ETH", "USDT"} or asset in observed_assets:
            raise PhaseThirteenPublicTraceValidationError(
                f"{location} contains an unsupported or duplicate asset"
            )
        observed_assets.add(asset)
        _require_plain_decimal(
            account_filter["maximum_quantity"],
            f"{filter_location} maximum_quantity",
            minimum=Decimal("0"),
        )
        validated_filters.append(account_filter)

    return tuple(validated_filters)  # Raw signed response와 credential은 trace에 보존하지 않는다.


def _validate_account_order_count_filters(
    value: object,
    *,
    allowed_filter_types: frozenset[str],
    location: str,
) -> tuple[Mapping[str, object], ...]:
    """
    함수 이름: _validate_account_order_count_filters()
    기능: AccountRelevantFilters의 scope별 count filter를 exact schema와 중복 없는 상한으로 검증한다.
    인자: value -> count filter JSON array
        allowed_filter_types -> 해당 scope에 허용된 공식 filter type 집합
        location -> secret을 포함하지 않는 고정 schema 위치
    반환값: 검증된 count filter mapping tuple
    작성 날짜: 2026/08/31
    """
    filter_entries = _require_list(value, location)
    validated_filters: list[Mapping[str, object]] = []
    observed_filter_types: set[str] = set()

    # 각 scope는 공식 type과 정수 상한을 한 번씩만 보존하고 다른 scope type을 수용하지 않는다.
    for filter_index, filter_value in enumerate(filter_entries):
        filter_location = f"{location}[{filter_index}]"
        count_filter = _require_exact_mapping(
            filter_value,
            _ACCOUNT_ORDER_COUNT_FILTER_FIELDS,
            filter_location,
        )
        filter_type = _require_identifier(
            count_filter["filter_type"],
            f"{filter_location} filter_type",
        )
        if (
            filter_type not in allowed_filter_types
            or filter_type in observed_filter_types
        ):
            raise PhaseThirteenPublicTraceValidationError(
                f"{location} contains an unsupported or duplicate type"
            )
        observed_filter_types.add(str(filter_type))
        maximum_count = _require_nonnegative_integer(
            count_filter["maximum_count"],
            f"{filter_location} maximum_count",
        )
        if (
            filter_type in {"EXCHANGE_MAX_NUM_ORDERS", "MAX_NUM_ORDERS"}
            and maximum_count < 1
        ):
            raise PhaseThirteenPublicTraceValidationError(
                f"{filter_location} cannot admit one plain MARKET order"
            )
        validated_filters.append(count_filter)

    return tuple(validated_filters)  # Response 순서는 canonical evidence에 그대로 유지한다.


def _validate_account_quantity_filters(
    value: object,
    location: str,
) -> tuple[Mapping[str, object], ...]:
    """
    함수 이름: _validate_account_quantity_filters()
    기능: Signed symbol LOT_SIZE 계열을 exact Decimal schema와 유효 범위로 검증한다.
    인자: value -> quantity filter JSON array
        location -> secret을 포함하지 않는 고정 schema 위치
    반환값: 검증된 quantity filter mapping tuple
    작성 날짜: 2026/08/31
    """
    filter_entries = _require_list(value, location)
    validated_filters: list[Mapping[str, object]] = []
    observed_filter_types: set[str] = set()

    # 두 공식 quantity type은 중복 없이 non-negative Decimal 하한·상한·step을 가져야 한다.
    for filter_index, filter_value in enumerate(filter_entries):
        filter_location = f"{location}[{filter_index}]"
        quantity_filter = _require_exact_mapping(
            filter_value,
            _ACCOUNT_QUANTITY_FILTER_FIELDS,
            filter_location,
        )
        filter_type = _require_identifier(
            quantity_filter["filter_type"],
            f"{filter_location} filter_type",
        )
        if (
            filter_type not in {"LOT_SIZE", "MARKET_LOT_SIZE"}
            or filter_type in observed_filter_types
        ):
            raise PhaseThirteenPublicTraceValidationError(
                f"{location} contains an unsupported or duplicate type"
            )
        observed_filter_types.add(str(filter_type))
        minimum_quantity = _require_plain_decimal(
            quantity_filter["minimum_quantity"],
            f"{filter_location} minimum_quantity",
            minimum=Decimal("0"),
        )
        maximum_quantity = _require_plain_decimal(
            quantity_filter["maximum_quantity"],
            f"{filter_location} maximum_quantity",
            minimum=Decimal("0"),
        )
        _require_plain_decimal(
            quantity_filter["step_size"],
            f"{filter_location} step_size",
            minimum=Decimal("0"),
        )
        if (
            maximum_quantity is not None
            and minimum_quantity is not None
            and maximum_quantity > Decimal("0")
            and maximum_quantity < minimum_quantity
        ):
            raise PhaseThirteenPublicTraceValidationError(
                f"{filter_location} quantity range is invalid"
            )
        validated_filters.append(quantity_filter)

    return tuple(validated_filters)  # Raw symbol filter mapping은 trace schema 밖으로 확장하지 않는다.


def _validate_account_notional_filters(
    value: object,
    location: str,
) -> tuple[Mapping[str, object], ...]:
    """
    함수 이름: _validate_account_notional_filters()
    기능: Signed MIN_NOTIONAL과 NOTIONAL의 exact 값·MARKET flag·기간을 검증한다.
    인자: value -> notional filter JSON array
        location -> secret을 포함하지 않는 고정 schema 위치
    반환값: 검증된 notional filter mapping tuple
    작성 날짜: 2026/08/31
    """
    filter_entries = _require_list(value, location)
    validated_filters: list[Mapping[str, object]] = []
    observed_filter_types: set[str] = set()

    # 각 notional type은 DTO projection의 null 구조와 exact boolean MARKET 적용식을 유지해야 한다.
    for filter_index, filter_value in enumerate(filter_entries):
        filter_location = f"{location}[{filter_index}]"
        notional_filter = _require_exact_mapping(
            filter_value,
            _ACCOUNT_NOTIONAL_FILTER_FIELDS,
            filter_location,
        )
        filter_type = _require_identifier(
            notional_filter["filter_type"],
            f"{filter_location} filter_type",
        )
        if (
            filter_type not in {"MIN_NOTIONAL", "NOTIONAL"}
            or filter_type in observed_filter_types
        ):
            raise PhaseThirteenPublicTraceValidationError(
                f"{location} contains an unsupported or duplicate type"
            )
        observed_filter_types.add(str(filter_type))
        minimum_notional = _require_plain_decimal(
            notional_filter["minimum_notional"],
            f"{filter_location} minimum_notional",
            minimum=Decimal("0"),
            allow_none=True,
        )
        maximum_notional = _require_plain_decimal(
            notional_filter["maximum_notional"],
            f"{filter_location} maximum_notional",
            minimum=Decimal("0"),
            allow_none=True,
        )
        for field_name in (
            "apply_minimum_to_market",
            "apply_maximum_to_market",
        ):
            if type(notional_filter[field_name]) is not bool:
                raise PhaseThirteenPublicTraceValidationError(
                    f"{filter_location} MARKET flags must be booleans"
                )
        _require_nonnegative_integer(
            notional_filter["average_price_minutes"],
            f"{filter_location} average_price_minutes",
        )

        # MIN_NOTIONAL에는 상한이 없고 NOTIONAL에는 공식 하한·상한이 모두 존재해야 한다.
        if filter_type == "MIN_NOTIONAL":
            if (
                minimum_notional is None
                or maximum_notional is not None
                or notional_filter["apply_maximum_to_market"]
            ):
                raise PhaseThirteenPublicTraceValidationError(
                    f"{filter_location} MIN_NOTIONAL structure is invalid"
                )
        elif minimum_notional is None or maximum_notional is None:
            raise PhaseThirteenPublicTraceValidationError(
                f"{filter_location} NOTIONAL limits must be present"
            )
        if (
            minimum_notional is not None
            and maximum_notional is not None
            and maximum_notional < minimum_notional
        ):
            raise PhaseThirteenPublicTraceValidationError(
                f"{filter_location} notional range is invalid"
            )
        validated_filters.append(notional_filter)

    return tuple(validated_filters)  # MARKET flag와 avgPriceMins는 normalized typed 값으로만 남긴다.


def _validate_passive_symbol_filter_types(
    value: object,
    location: str,
) -> frozenset[str]:
    """
    함수 이름: _validate_passive_symbol_filter_types()
    기능: Plain MARKET 비적용 symbol filter type 목록의 canonical 집합을 검증한다.
    인자: value -> passive_symbol_filter_types JSON array
        location -> secret을 포함하지 않는 고정 schema 위치
    반환값: 중복 없는 공식 passive filter type frozenset
    작성 날짜: 2026/08/31
    """
    passive_filter_values = _require_list(value, location)
    passive_filter_types = tuple(
        _require_identifier(
            filter_type,
            f"{location} filter type",
        )
        for filter_type in passive_filter_values
    )

    # Frozenset 직렬화는 sorted list만 허용해 같은 의미의 canonical bytes를 하나로 고정한다.
    if (
        tuple(sorted(str(filter_type) for filter_type in passive_filter_types))
        != passive_filter_types
        or len(set(passive_filter_types)) != len(passive_filter_types)
        or any(
            filter_type not in _PASSIVE_SYMBOL_FILTER_TYPES
            for filter_type in passive_filter_types
        )
    ):
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} is not canonical"
        )

    return frozenset(passive_filter_types)  # Raw price·trailing 값은 trace에 포함하지 않는다.


def _validate_account_relevant_filters(
    value: object,
    *,
    account_asset_filters: tuple[Mapping[str, object], ...],
    public_relevant_filters: object,
    public_rules: Mapping[str, object],
    location: str,
) -> Mapping[str, object]:
    """
    함수 이름: _validate_account_relevant_filters()
    기능: v3 signed/public relevant filter projection과 runtime overlap 계약을 검증한다.
    인자: value -> normalized AccountRelevantFilters JSON object
        account_asset_filters -> v2 호환용 signed MAX_ASSET projection
        public_relevant_filters -> 같은 exchangeInfo의 public AccountRelevantFilters projection
        public_rules -> 같은 관찰 구간의 normalized public symbol rules
        location -> secret을 포함하지 않는 고정 schema 위치
    반환값: 검증된 AccountRelevantFilters mapping
    작성 날짜: 2026/08/31
    """
    account_filters = _require_exact_mapping(
        value,
        _ACCOUNT_RELEVANT_FILTER_FIELDS,
        location,
    )
    if account_filters["symbol"] != "ETHUSDT":
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} must be bound to ETHUSDT"
        )

    # Signed collection은 scope별 exact parser를 먼저 통과해 비교 중 malformed 우회를 막는다.
    signed_exchange_count_filters = _validate_account_order_count_filters(
        account_filters["exchange_order_count_filters"],
        allowed_filter_types=_EXCHANGE_ORDER_COUNT_FILTER_TYPES,
        location=f"{location} exchange count filters",
    )
    signed_symbol_count_filters = _validate_account_order_count_filters(
        account_filters["symbol_order_count_filters"],
        allowed_filter_types=_SYMBOL_ORDER_COUNT_FILTER_TYPES,
        location=f"{location} symbol count filters",
    )
    signed_quantity_filters = _validate_account_quantity_filters(
        account_filters["symbol_quantity_filters"],
        f"{location} symbol quantity filters",
    )
    signed_notional_filters = _validate_account_notional_filters(
        account_filters["symbol_notional_filters"],
        f"{location} symbol notional filters",
    )
    signed_passive_filter_types = _validate_passive_symbol_filter_types(
        account_filters["passive_symbol_filter_types"],
        f"{location} passive symbol filter types",
    )

    # Public projection도 같은 exact union을 쓰되 exchangeInfo에 존재할 수 없는 asset scope는 비워 둔다.
    public_filters = _require_exact_mapping(
        public_relevant_filters,
        _ACCOUNT_RELEVANT_FILTER_FIELDS,
        f"{location} public relevant filters",
    )
    if public_filters["symbol"] != "ETHUSDT":
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} public relevant filters must be bound to ETHUSDT"
        )
    public_exchange_count_filters = _validate_account_order_count_filters(
        public_filters["exchange_order_count_filters"],
        allowed_filter_types=_EXCHANGE_ORDER_COUNT_FILTER_TYPES,
        location=f"{location} public exchange count filters",
    )
    public_symbol_count_filters = _validate_account_order_count_filters(
        public_filters["symbol_order_count_filters"],
        allowed_filter_types=_SYMBOL_ORDER_COUNT_FILTER_TYPES,
        location=f"{location} public symbol count filters",
    )
    public_quantity_filters = _validate_account_quantity_filters(
        public_filters["symbol_quantity_filters"],
        f"{location} public symbol quantity filters",
    )
    public_notional_filters = _validate_account_notional_filters(
        public_filters["symbol_notional_filters"],
        f"{location} public symbol notional filters",
    )
    public_passive_filter_types = _validate_passive_symbol_filter_types(
        public_filters["passive_symbol_filter_types"],
        f"{location} public passive symbol filter types",
    )
    public_asset_filters = _validate_account_asset_filters(
        public_filters["asset_filters"],
        f"{location} public asset filters",
    )
    if public_asset_filters:
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} public exchangeInfo must not contain asset filters"
        )

    # Decimal scale 차이는 runtime DTO equality와 같이 값으로 비교하고 type별 identity는 보존한다.
    signed_quantity_values = {
        str(filter_value["filter_type"]): (
            Decimal(str(filter_value["minimum_quantity"])),
            Decimal(str(filter_value["maximum_quantity"])),
            Decimal(str(filter_value["step_size"])),
        )
        for filter_value in signed_quantity_filters
    }
    public_quantity_values = {
        str(filter_value["filter_type"]): (
            Decimal(str(filter_value["minimum_quantity"])),
            Decimal(str(filter_value["maximum_quantity"])),
            Decimal(str(filter_value["step_size"])),
        )
        for filter_value in public_quantity_filters
    }
    expected_public_quantity_values = {
        "LOT_SIZE": (
            Decimal(str(public_rules["lot_size_minimum_quantity"])),
            Decimal(str(public_rules["lot_size_maximum_quantity"])),
            Decimal(str(public_rules["lot_size_step_size"])),
        ),
        "MARKET_LOT_SIZE": (
            Decimal(str(public_rules["market_lot_size_minimum_quantity"])),
            Decimal(str(public_rules["market_lot_size_maximum_quantity"])),
            Decimal(str(public_rules["market_lot_size_step_size"])),
        ),
    }
    if public_quantity_values != expected_public_quantity_values:
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} public quantity projection does not match rules"
        )
    if any(
        public_quantity_values.get(filter_type) != filter_values
        for filter_type, filter_values in signed_quantity_values.items()
    ):
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} signed quantity filters do not match public rules"
        )

    # Notional DTO의 null·MARKET flag·기간까지 비교하고 public effective 교집을 rules 요약에 결속한다.
    signed_notional_values = {
        str(filter_value["filter_type"]): (
            None
            if filter_value["minimum_notional"] is None
            else Decimal(str(filter_value["minimum_notional"])),
            None
            if filter_value["maximum_notional"] is None
            else Decimal(str(filter_value["maximum_notional"])),
            filter_value["apply_minimum_to_market"],
            filter_value["apply_maximum_to_market"],
            int(filter_value["average_price_minutes"]),
        )
        for filter_value in signed_notional_filters
    }
    public_notional_values = {
        str(filter_value["filter_type"]): (
            None
            if filter_value["minimum_notional"] is None
            else Decimal(str(filter_value["minimum_notional"])),
            None
            if filter_value["maximum_notional"] is None
            else Decimal(str(filter_value["maximum_notional"])),
            filter_value["apply_minimum_to_market"],
            filter_value["apply_maximum_to_market"],
            int(filter_value["average_price_minutes"]),
        )
        for filter_value in public_notional_filters
    }
    if not public_notional_values or any(
        public_notional_values.get(filter_type) != filter_values
        for filter_type, filter_values in signed_notional_values.items()
    ):
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} signed notional filters do not match public rules"
        )
    public_minimum_candidates = tuple(
        filter_values[0]
        for filter_values in public_notional_values.values()
        if filter_values[2] and filter_values[0] is not None
    )
    public_maximum_candidates = tuple(
        filter_values[1]
        for filter_values in public_notional_values.values()
        if filter_values[3] and filter_values[1] is not None
    )
    effective_public_minimum = (
        max(public_minimum_candidates) if public_minimum_candidates else None
    )
    effective_public_maximum = (
        min(public_maximum_candidates) if public_maximum_candidates else None
    )
    summarized_public_minimum = (
        None
        if public_rules["minimum_notional"] is None
        else Decimal(str(public_rules["minimum_notional"]))
    )
    summarized_public_maximum = (
        None
        if public_rules["maximum_notional"] is None
        else Decimal(str(public_rules["maximum_notional"]))
    )
    if (
        effective_public_minimum != summarized_public_minimum
        or effective_public_maximum != summarized_public_maximum
    ):
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} public notional projection does not match rules"
        )

    # Count는 runtime과 같이 양방향 exact이고 passive type은 signed 집합이 public의 부분집합이어야 한다.
    signed_exchange_count_values = {
        str(filter_value["filter_type"]): int(filter_value["maximum_count"])
        for filter_value in signed_exchange_count_filters
    }
    public_exchange_count_values = {
        str(filter_value["filter_type"]): int(filter_value["maximum_count"])
        for filter_value in public_exchange_count_filters
    }
    signed_symbol_count_values = {
        str(filter_value["filter_type"]): int(filter_value["maximum_count"])
        for filter_value in signed_symbol_count_filters
    }
    public_symbol_count_values = {
        str(filter_value["filter_type"]): int(filter_value["maximum_count"])
        for filter_value in public_symbol_count_filters
    }
    if (
        signed_exchange_count_values != public_exchange_count_values
        or signed_symbol_count_values != public_symbol_count_values
    ):
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} signed count filters do not match public rules"
        )
    if not signed_passive_filter_types.issubset(public_passive_filter_types):
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} signed passive filters do not match public rules"
        )

    # MAX_POSITION은 signed/public projection과 public scalar summary가 모두 같은 Decimal이어야 한다.
    signed_maximum_position = _require_plain_decimal(
        account_filters["symbol_maximum_position"],
        f"{location} symbol_maximum_position",
        minimum=Decimal("0"),
        allow_none=True,
    )
    public_maximum_position = _require_plain_decimal(
        public_rules["maximum_position"],
        f"{location} public maximum_position",
        minimum=Decimal("0"),
        allow_none=True,
    )
    projected_public_maximum_position = _require_plain_decimal(
        public_filters["symbol_maximum_position"],
        f"{location} public symbol_maximum_position",
        minimum=Decimal("0"),
        allow_none=True,
    )
    if (
        signed_maximum_position != projected_public_maximum_position
        or projected_public_maximum_position != public_maximum_position
    ):
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} MAX_POSITION does not match public rules"
        )

    # Composite의 asset collection은 v2 호환 projection과 순서·값까지 정확히 같아야 한다.
    composite_asset_filters = _validate_account_asset_filters(
        account_filters["asset_filters"],
        f"{location} asset filters",
    )
    if composite_asset_filters != account_asset_filters:
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} asset projection does not match"
        )

    return account_filters  # Credential·raw payload·account balance나 order identity는 포함하지 않는다.


def _validate_account_open_state_evidence(
    value: Mapping[str, object],
    *,
    location: str,
) -> tuple[datetime, datetime]:
    """
    함수 이름: _validate_account_open_state_evidence()
    기능: v3 all-symbol openOrders와 openOrderList의 signed empty 관찰시각을 검증한다.
    인자: value -> preflight 또는 submit-time evidence mapping
        location -> secret을 포함하지 않는 고정 schema 위치
    반환값: open orders와 open order lists UTC 관찰시각 tuple
    작성 날짜: 2026/08/31
    """
    # Empty proof는 bool True만 허용하고 raw order/list ID나 count를 새 schema에 추가하지 않는다.
    for field_name in (
        "account_open_orders_verified_empty",
        "account_open_order_lists_verified_empty",
    ):
        if type(value[field_name]) is not bool or not value[field_name]:
            raise PhaseThirteenPublicTraceValidationError(
                f"{location} account-wide open state must be verified empty"
            )
    open_orders_observed_at = _require_utc_timestamp(
        value["account_open_orders_observed_at"],
        f"{location} account open orders observed_at",
    )
    open_order_lists_observed_at = _require_utc_timestamp(
        value["account_open_order_lists_observed_at"],
        f"{location} account open order lists observed_at",
    )
    if open_order_lists_observed_at < open_orders_observed_at:
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} account-wide open state observations are out of order"
        )

    return open_orders_observed_at, open_order_lists_observed_at  # 두 signed GET 완료 시각을 합치지 않는다.


def _validate_reference_price(
    value: object,
    *,
    observed_at: datetime,
    location: str,
) -> Mapping[str, object]:
    """
    함수 이름: _validate_reference_price()
    기능: public referencePrice를 ETHUSDT 양의 Decimal과 최신 exchange 시각으로 검증한다.
    인자: value -> reference_price JSON object
        observed_at -> public 응답 관찰 UTC 시각
        location -> secret을 포함하지 않는 고정 schema 위치
    반환값: 검증된 reference price mapping
    작성 날짜: 2026/08/31
    """
    reference_price = _require_exact_mapping(
        value,
        _REFERENCE_PRICE_FIELDS,
        location,
    )
    if reference_price["symbol"] != "ETHUSDT":
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} must describe ETHUSDT"
        )
    price = _require_plain_decimal(
        reference_price["price"],
        f"{location} price",
        minimum=Decimal("0"),
    )
    if price == Decimal("0"):
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} price must be positive"
        )
    _require_nonnegative_integer(
        reference_price["exchange_timestamp"],
        f"{location} exchange_timestamp",
    )

    # Freshness는 local GET 완료 observed_at으로 증명하고 exchange timestamp는 payload provenance로 별도 보존한다.
    _ = observed_at  # Caller의 exact timestamp 검증을 함수 계약에 명시적으로 남긴다.

    return reference_price  # Raw endpoint payload 대신 notional 검증에 필요한 최소 정보만 반환한다.


def _validate_fresh_filters(value: object) -> Mapping[str, object]:
    """
    함수 이름: _validate_fresh_filters()
    기능: preflight 시점 ETHUSDT exchangeInfo의 MARKET 수량·notional filter snapshot을 검증한다.
    인자: value -> fresh_filters JSON object
    반환값: 검증된 filter mapping
    작성 날짜: 2026/08/31
    """
    filters = _require_exact_mapping(
        value,
        _FRESH_FILTER_FIELDS,
        "preflight fresh filters",
    )
    _require_utc_timestamp(filters["observed_at"], "filter observed_at")
    _validate_fresh_filter_rule_values(filters, "preflight fresh filters")
    if filters["maximum_position"] is not None:
        raise PhaseThirteenPublicTraceValidationError(
            "Phase 13 BUY preflight must not contain MAX_POSITION"
        )

    return filters  # Raw exchangeInfo와 endpoint query는 trace schema에 포함하지 않는다.


def _validate_order_attempt_against_fresh_filters(
    attempt: Mapping[str, object],
    filters: Mapping[str, object],
    account_asset_filters: tuple[Mapping[str, object], ...],
    reference_price: Mapping[str, object],
) -> None:
    """
    함수 이름: _validate_order_attempt_against_fresh_filters()
    기능: 최종 수량을 fresh symbol·account filter와 official MARKET reference notional로 검증한다.
    인자: attempt -> 검증된 order attempt mapping
        filters -> 검증된 fresh_filters mapping
        account_asset_filters -> 검증된 signed MAX_ASSET filter tuple
        reference_price -> 검증된 public reference price mapping
    반환값: 없음
    작성 날짜: 2026/08/31
    """
    submitted_quantity = Decimal(str(attempt["final_submitted_quantity"]))
    base_asset_precision = int(filters["base_asset_precision"])
    precision_step = Decimal("1").scaleb(-base_asset_precision)
    reference_notional = _multiply_decimal128(
        submitted_quantity,
        Decimal(str(reference_price["price"])),
    )

    # Production floor와 동일하게 precision quantum 및 0이 아닌 lot step을 모두 만족해야 한다.
    active_steps = [precision_step]
    for field_name in (
        "lot_size_step_size",
        "market_lot_size_step_size",
    ):
        filter_step = Decimal(str(filters[field_name]))
        if filter_step > Decimal("0"):
            active_steps.append(filter_step)
    with localcontext() as decimal_context:
        decimal_context.prec = max(
            34,
            len(submitted_quantity.as_tuple().digits) + 18,
        )
        if any(
            submitted_quantity % filter_step != Decimal("0")
            for filter_step in active_steps
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "final submitted quantity is outside the fresh filter grid"
            )

    # 활성 quantity/notional 하한·상한은 production MARKET 사전 점검과 같은 inclusive 경계다.
    for filter_prefix in ("lot_size", "market_lot_size"):
        minimum_quantity = Decimal(
            str(filters[f"{filter_prefix}_minimum_quantity"])
        )
        maximum_quantity = Decimal(
            str(filters[f"{filter_prefix}_maximum_quantity"])
        )
        if minimum_quantity > Decimal("0") and submitted_quantity < minimum_quantity:
            raise PhaseThirteenPublicTraceValidationError(
                "final submitted quantity is below a fresh filter minimum"
            )
        if maximum_quantity > Decimal("0") and submitted_quantity > maximum_quantity:
            raise PhaseThirteenPublicTraceValidationError(
                "final submitted quantity exceeds a fresh filter maximum"
            )
    minimum_notional = filters["minimum_notional"]
    maximum_notional = filters["maximum_notional"]
    if minimum_notional is not None and reference_notional < Decimal(
        str(minimum_notional)
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "final notional is below the fresh filter minimum"
        )
    if maximum_notional is not None and reference_notional > Decimal(
        str(maximum_notional)
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "final notional exceeds the fresh filter maximum"
        )

    # MAX_POSITION은 BUY에만 적용되며 이 실증 target은 해당 filter 부재를 요구한다.
    if attempt["side"] == "BUY" and filters["maximum_position"] is not None:
        raise PhaseThirteenPublicTraceValidationError(
            "Phase 13 BUY submit-time rules must not contain MAX_POSITION"
        )

    # Quantity 기반 MARKET에는 quote MAX_ASSET 공식 환산식이 없으므로 base 수량만 평가한다.
    for account_filter in account_asset_filters:
        if account_filter["asset"] != "ETH":
            raise PhaseThirteenPublicTraceValidationError(
                "quantity MARKET cannot prove a quote MAX_ASSET limit"
            )
        if submitted_quantity > Decimal(
            str(account_filter["maximum_quantity"])
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "final MARKET order exceeds a signed MAX_ASSET limit"
            )

    return None  # Filter snapshot과 제출 claim을 검증만 하고 caller evidence는 변경하지 않는다.


def _validate_preflight(
    value: object,
    *,
    schema_version: int,
) -> Mapping[str, object]:
    """
    함수 이름: _validate_preflight()
    기능: fixed Spot Testnet endpoint, readiness, local fetch order와 mutation 전 exact zero-state를 검증한다.
    인자: value -> preflight JSON object
        schema_version -> top-level trace schema version
    반환값: 검증된 preflight mapping
    작성 날짜: 2026/09/01
    """
    # V2는 보존 artifact의 asset-only schema, v3는 full filter와 account-wide state schema를 쓴다.
    expected_fields = (
        _PREFLIGHT_FIELDS_V3
        if schema_version == 3
        else _PREFLIGHT_FIELDS_V2
    )
    preflight = _require_exact_mapping(value, expected_fields, "preflight")
    if preflight["endpoint_set"] != "BINANCE_SPOT_TESTNET":
        raise PhaseThirteenPublicTraceValidationError(
            "preflight endpoint set must be Binance Spot Testnet"
        )
    if preflight["symbol"] != "ETHUSDT":
        raise PhaseThirteenPublicTraceValidationError(
            "preflight symbol must be ETHUSDT"
        )
    if type(preflight["can_trade"]) is not bool or not preflight["can_trade"]:
        raise PhaseThirteenPublicTraceValidationError(
            "preflight account must allow trading"
        )
    if (
        preflight["account_stream_status"] != "READY"
        or preflight["market_stream_status"] != "READY"
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "preflight account and market streams must be READY"
        )
    if _require_nonnegative_integer(
        preflight["account_version"],
        "preflight account_version",
    ) < 1:
        raise PhaseThirteenPublicTraceValidationError(
            "preflight account version must be positive"
        )
    _require_utc_timestamp(preflight["verified_at"], "preflight verified_at")
    _validate_commission_policy(preflight["commission_policy"])
    fresh_filters = _validate_fresh_filters(preflight["fresh_filters"])
    account_filters_observed_at = _require_utc_timestamp(
        preflight["account_filters_observed_at"],
        "preflight account filters observed_at",
    )
    account_asset_filters = _validate_account_asset_filters(
        preflight["account_asset_filters"],
        "preflight account asset filters",
    )
    account_open_orders_observed_at: datetime | None = None
    account_open_order_lists_observed_at: datetime | None = None
    if schema_version == 3:
        _validate_account_relevant_filters(
            preflight["account_relevant_filters"],
            account_asset_filters=account_asset_filters,
            public_relevant_filters=preflight["public_relevant_filters"],
            public_rules=fresh_filters,
            location="preflight account relevant filters",
        )
        (
            account_open_orders_observed_at,
            account_open_order_lists_observed_at,
        ) = _validate_account_open_state_evidence(
            preflight,
            location="preflight",
        )
    reference_price_observed_at = _require_utc_timestamp(
        preflight["reference_price_observed_at"],
        "preflight reference price observed_at",
    )
    _validate_reference_price(
        preflight["reference_price"],
        observed_at=reference_price_observed_at,
        location="preflight reference price",
    )

    # Top-level 관찰 시각은 각 REST 반환 직후 harness local clock이 찍으므로 verified_at과 함께 비교한다.
    verified_at = _require_utc_timestamp(
        preflight["verified_at"],
        "preflight verified_at",
    )
    filters_observed_at = _require_utc_timestamp(
        preflight["fresh_filters"]["observed_at"],
        "preflight filter observed_at",
    )
    ordered_safety_observations = [
        account_filters_observed_at,
        filters_observed_at,
    ]
    if (
        account_open_orders_observed_at is not None
        and account_open_order_lists_observed_at is not None
    ):
        ordered_safety_observations.extend(
            (
                account_open_orders_observed_at,
                account_open_order_lists_observed_at,
            )
        )
    ordered_safety_observations.extend(
        (reference_price_observed_at, verified_at)
    )
    if ordered_safety_observations != sorted(ordered_safety_observations):
        raise PhaseThirteenPublicTraceValidationError(
            "preflight safety observations are outside the fetch order"
        )

    # Fresh runtime의 Position/pending/UNKNOWN/open order가 모두 exact zero일 때만 evidence가 시작된다.
    position_quantity = _require_plain_decimal(
        preflight["position_quantity"],
        "preflight position quantity",
        minimum=Decimal("0"),
    )
    for field_name in (
        "pending_order_count",
        "unknown_order_count",
        "matching_open_order_count",
    ):
        if _require_nonnegative_integer(
            preflight[field_name],
            f"preflight {field_name}",
        ) != 0:
            raise PhaseThirteenPublicTraceValidationError(
                "preflight order state must be zero"
            )
    if position_quantity != Decimal("0"):
        raise PhaseThirteenPublicTraceValidationError(
            "preflight Position must be zero"
        )

    return preflight  # Credential와 실제 endpoint URL 대신 fixed enum과 normalized truth만 반환한다.


def _validate_decision_fingerprint(
    value: object,
) -> Mapping[str, object] | None:
    """
    함수 이름: _validate_decision_fingerprint()
    기능: public evaluation부터 최종 제출 notional까지 immutable decision claim을 검증한다.
    인자: value -> decision fingerprint object 또는 signal 부재 None
    반환값: 검증된 fingerprint mapping 또는 None
    작성 날짜: 2026/08/31
    """
    if value is None:
        return None
    fingerprint = _require_exact_mapping(
        value,
        _DECISION_FIELDS,
        "immutable decision fingerprint",
    )

    # Source Kline, evaluation과 모든 authoritative version을 하나의 immutable identity로 고정한다.
    for field_name in (
        "source_event_id",
        "source_kline_identity",
        "evaluation_id",
        "intent_id",
        "client_order_id",
    ):
        _require_canonical_text(
            fingerprint[field_name],
            f"decision {field_name}",
        )
    _require_utc_timestamp(
        fingerprint["source_event_time"],
        "decision source_event_time",
    )
    for field_name in (
        "market_version",
        "account_version",
        "context_version",
        "policy_version",
    ):
        version = _require_nonnegative_integer(
            fingerprint[field_name],
            f"decision {field_name}",
        )
        if version < 1:
            raise PhaseThirteenPublicTraceValidationError(
                "decision versions must be positive"
            )
    _require_identifier(fingerprint["regime"], "decision regime")
    if (
        fingerprint["action_type"] != "SUBMIT_ORDER"
        or fingerprint["side"] != "BUY"
        or fingerprint["strategy"] != "CASE_C"
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "decision must be a CASE_C BUY SUBMIT_ORDER action"
        )

    # Production 금융 계산과 같은 precision 34에서 가격·최종 수량·notional을 재계산한다.
    decision_price = _require_plain_decimal(
        fingerprint["decision_price"],
        "decision price",
        minimum=Decimal("0.000000000000000001"),
    )
    submitted_quantity = _require_plain_decimal(
        fingerprint["final_submitted_quantity"],
        "decision final submitted quantity",
        minimum=Decimal("0.000000000000000001"),
    )
    final_notional = _require_plain_decimal(
        fingerprint["final_notional"],
        "decision final notional",
        minimum=Decimal("0"),
    )
    configured_cap = _require_plain_decimal(
        fingerprint["configured_cap"],
        "decision configured cap",
        minimum=Decimal("0.000000000000000001"),
    )
    if (
        decision_price is None
        or submitted_quantity is None
        or final_notional is None
        or configured_cap is None
    ):
        raise AssertionError("required decision decimal unexpectedly resolved to None")
    calculated_notional = _multiply_decimal128(
        decision_price,
        submitted_quantity,
    )
    if calculated_notional != final_notional:
        raise PhaseThirteenPublicTraceValidationError(
            "decision notional does not match price and final quantity"
        )
    if (
        final_notional > configured_cap
        or configured_cap > PHASE13_PUBLIC_TRACE_ABSOLUTE_MAX_NOTIONAL
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "decision notional exceeds the configured or absolute cap"
        )

    return fingerprint  # Caller는 동일 값을 public event와 order attempt에 다시 결속한다.


def _validate_market_events(
    value: object,
) -> tuple[Mapping[str, object], ...]:
    """
    함수 이름: _validate_market_events()
    기능: public Kline·evaluation·Action event의 exact schema와 version을 검증한다.
    인자: value -> public market event JSON array
    반환값: 검증된 market event tuple
    작성 날짜: 2026/08/31
    """
    entries = _require_contiguous_sequence(
        _require_list(value, "public market events"),
        _MARKET_EVENT_FIELDS,
        "public market events",
    )
    for entry in entries:
        _require_canonical_text(entry["message_id"], "market message_id")
        _require_identifier(entry["event_type"], "market event_type")
        _require_canonical_text(entry["source_event_id"], "market source_event_id")
        _require_canonical_text(
            entry["source_kline_identity"],
            "market source_kline_identity",
        )
        _require_utc_timestamp(entry["source_event_time"], "market source_event_time")
        for field_name in ("market_version", "context_version"):
            _require_nonnegative_integer(entry[field_name], f"market {field_name}")
        _require_canonical_text(
            entry["evaluation_id"],
            "market evaluation_id",
            allow_none=True,
        )
        _require_identifier(
            entry["regime"],
            "market regime",
            allow_none=True,
        )
        _require_identifier(
            entry["action_type"],
            "market action_type",
            allow_none=True,
        )
        _require_identifier(entry["side"], "market side", allow_none=True)
        _require_identifier(
            entry["strategy"],
            "market strategy",
            allow_none=True,
        )

    return entries  # 원래 event 순서는 transport trace의 provenance 증거로 유지한다.


def _validate_account_events(
    value: object,
    *,
    schema_version: int,
    run_id: str,
) -> tuple[Mapping[str, object], ...]:
    """
    함수 이름: _validate_account_events()
    기능: public account snapshot·stream event의 exact schema, 생성 순서와 잔액 Decimal을 검증한다.
    인자: value -> public account event JSON array
        schema_version -> top-level trace schema version
        run_id -> startup account source를 결속할 run UUIDv4
    반환값: 검증된 account event tuple
    작성 날짜: 2026/08/31
    """
    entries = _require_contiguous_sequence(
        _require_list(value, "public account events"),
        _ACCOUNT_EVENT_FIELDS,
        "public account events",
    )
    previous_source_event_time: datetime | None = None
    previous_account_version: int | None = None
    for entry_index, entry in enumerate(entries):
        _require_canonical_text(entry["message_id"], "account message_id")
        _require_identifier(entry["event_type"], "account event_type")
        source_event_id = _require_canonical_text(
            entry["source_event_id"],
            "account source_event_id",
        )
        source_event_time = _require_utc_timestamp(
            entry["source_event_time"],
            "account source_event_time",
        )
        account_version = _require_nonnegative_integer(
            entry["account_version"],
            "account version",
        )
        _require_identifier(entry["asset"], "account asset")
        for field_name in ("free_quantity", "locked_quantity"):
            _require_plain_decimal(
                entry[field_name],
                f"account {field_name}",
                minimum=Decimal("0"),
            )

        # Shared run clock과 Account aggregate는 도착 순서대로 시각·version을 전진시킨다.
        if (
            previous_source_event_time is not None
            and source_event_time < previous_source_event_time
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "account event times must be monotonic"
            )
        if (
            previous_account_version is not None
            and account_version <= previous_account_version
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "account event versions must strictly increase"
            )
        previous_source_event_time = source_event_time
        previous_account_version = account_version

        # V3의 첫 행은 startup runtime snapshot이고 후속 행은 Backend envelope에서만 생성된다.
        if schema_version == 3 and entry_index == 0:
            expected_source_event_id = (
                f"startup-account-{run_id}-{account_version}"
            )
            if (
                account_version < 1
                or entry["message_id"] != "2"
                or entry["event_type"] != "ACCOUNT_SNAPSHOT_APPLIED"
                or source_event_id != expected_source_event_id
                or entry["asset"] != "ETH"
            ):
                raise PhaseThirteenPublicTraceValidationError(
                    "v3 startup account event does not match its run snapshot"
                )
        elif schema_version == 3:
            if (
                entry["message_id"] != "2.2.1"
                or entry["event_type"] != "ACCOUNT_POSITION_APPLIED"
                or entry["asset"] != "ETH"
            ):
                raise PhaseThirteenPublicTraceValidationError(
                    "v3 account stream event has invalid production provenance"
                )
            _require_uuid4(source_event_id, "account source_event_id")

    return entries  # 전체 balance payload 대신 필요한 absolute asset 사실만 남긴다.


def _validate_order_attempts(
    value: object,
) -> tuple[Mapping[str, object], ...]:
    """
    함수 이름: _validate_order_attempts()
    기능: POST 직전 submission-start 시각과 주문별 immutable identity·최종 수량을 검증한다.
    인자: value -> order attempt JSON array
    반환값: 검증된 order attempt tuple
    작성 날짜: 2026/08/31
    """
    entries = _require_contiguous_sequence(
        _require_list(value, "order attempts"),
        _ORDER_ATTEMPT_FIELDS,
        "order attempts",
    )
    observed_client_order_ids: set[str] = set()
    previous_attempted_at: datetime | None = None
    for entry in entries:
        attempted_at = _require_utc_timestamp(
            entry["attempted_at"],
            "order attempted_at",
        )
        if previous_attempted_at is not None and attempted_at < previous_attempted_at:
            raise PhaseThirteenPublicTraceValidationError(
                "order attempt times must be monotonic"
            )
        previous_attempted_at = attempted_at
        _require_canonical_text(entry["evaluation_id"], "order evaluation_id")
        _require_canonical_text(entry["intent_id"], "order intent_id")
        client_order_id = _require_canonical_text(
            entry["client_order_id"],
            "order client_order_id",
        )
        if client_order_id is None or client_order_id in observed_client_order_ids:
            raise PhaseThirteenPublicTraceValidationError(
                "order attempts contain duplicate client identities"
            )
        observed_client_order_ids.add(client_order_id)
        _require_nonnegative_integer(entry["submission_attempt"], "order submission_attempt")
        if entry["symbol"] != "ETHUSDT":
            raise PhaseThirteenPublicTraceValidationError(
                "order attempt symbol must be ETHUSDT"
            )
        _require_identifier(entry["regime"], "order regime")
        if (
            entry["strategy"] != "CASE_C"
            or entry["action_type"] != "SUBMIT_ORDER"
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "order attempt must be a CASE_C SUBMIT_ORDER action"
            )
        side = _require_identifier(entry["side"], "order side")
        if side not in {"BUY", "SELL"}:
            raise PhaseThirteenPublicTraceValidationError("order side is unsupported")
        exit_reason = _require_identifier(
            entry["exit_reason"],
            "order exit_reason",
            allow_none=True,
        )
        if side == "BUY" and exit_reason is not None:
            raise PhaseThirteenPublicTraceValidationError(
                "BUY attempt must not contain an exit reason"
            )
        if _require_nonnegative_integer(
            entry["policy_version"],
            "order policy_version",
        ) < 1:
            raise PhaseThirteenPublicTraceValidationError(
                "order policy version must be positive"
            )

        # STOP SELL만 exposure-reducing recovery로 cap 예외이고 나머지는 configured·absolute cap을 지킨다.
        decision_price = _require_plain_decimal(
            entry["decision_price"],
            "order decision_price",
            minimum=Decimal("0.000000000000000001"),
        )
        submitted_quantity = _require_plain_decimal(
            entry["final_submitted_quantity"],
            "order final_submitted_quantity",
            minimum=Decimal("0.000000000000000001"),
        )
        final_notional = _require_plain_decimal(
            entry["final_notional"],
            "order final_notional",
            minimum=Decimal("0"),
        )
        configured_cap = _require_plain_decimal(
            entry["configured_cap"],
            "order configured_cap",
            minimum=Decimal("0.000000000000000001"),
        )
        if (
            decision_price is None
            or submitted_quantity is None
            or final_notional is None
            or configured_cap is None
        ):
            raise AssertionError("required order decimal unexpectedly resolved to None")
        calculated_notional = _multiply_decimal128(
            decision_price,
            submitted_quantity,
        )
        if calculated_notional != final_notional:
            raise PhaseThirteenPublicTraceValidationError(
                "order notional does not match price and final quantity"
            )
        if configured_cap > PHASE13_PUBLIC_TRACE_ABSOLUTE_MAX_NOTIONAL:
            raise PhaseThirteenPublicTraceValidationError(
                "order configured cap exceeds the absolute cap"
            )
        if not (side == "SELL" and exit_reason == "STOP") and (
            final_notional > configured_cap
            or final_notional > PHASE13_PUBLIC_TRACE_ABSOLUTE_MAX_NOTIONAL
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "order notional exceeds the configured or absolute cap"
            )

    return entries  # 순서와 exact client identity는 result correlation에서 다시 사용한다.


def _validate_success_order_message_sequence(
    message_ids: tuple[str, ...],
    *,
    side: str,
) -> None:
    """
    함수 이름: _validate_success_order_message_sequence()
    기능: BUY/SELL 고정 경계와 same-ID query·stream reapply·partial fill 성공 grammar를 검증한다.
    인자: message_ids -> 한 actual order의 Communication message ID 순서
        side -> BUY 또는 SELL
    반환값: complete grammar이면 없음
    작성 날짜: 2026/08/31
    """
    if side not in {"BUY", "SELL"}:
        raise PhaseThirteenPublicTraceValidationError(
            "order execution trace side is unsupported"
        )
    expected_prefix = (
        _BUY_ORDER_TRACE_PREFIX if side == "BUY" else _SELL_ORDER_TRACE_PREFIX
    )
    fill_application_trace = (
        _BUY_FILL_APPLICATION_TRACE
        if side == "BUY"
        else _SELL_FILL_APPLICATION_TRACE
    )
    expected_suffix = (
        _BUY_ORDER_TRACE_SUFFIX if side == "BUY" else _SELL_ORDER_TRACE_SUFFIX
    )
    if message_ids[: len(expected_prefix)] != expected_prefix:
        raise PhaseThirteenPublicTraceValidationError(
            "successful order execution trace prefix is incomplete"
        )
    if message_ids[-len(expected_suffix) :] != expected_suffix:
        raise PhaseThirteenPublicTraceValidationError(
            "successful order execution trace suffix is incomplete"
        )

    # Prefix·suffix 사이만 branch parser에 넘겨 whitelist ID의 누락·중복 우회를 막는다.
    branch_ids = message_ids[len(expected_prefix) : -len(expected_suffix)]
    branch_cursor = 0
    applied_fill_count = 0
    query_count = 0
    terminal_evidence_complete = False
    if (
        branch_ids[: len(fill_application_trace)]
        == fill_application_trace
    ):
        branch_cursor += len(fill_application_trace)
        applied_fill_count += 1
        terminal_evidence_complete = True

    # 최초 응답 뒤에는 최대 네 query group과 그 query가 새로 적용한 fill delta만 올 수 있다.
    while branch_cursor < len(branch_ids):
        # User Data Stream reapply는 query 8/8.1/8.2 없이 message 9로 직접 도착할 수 있다.
        if branch_ids[branch_cursor] == "9":
            branch_cursor += 1
        else:
            if (
                branch_ids[
                    branch_cursor : branch_cursor + len(_SAME_ORDER_QUERY_TRACE)
                ]
                != _SAME_ORDER_QUERY_TRACE
            ):
                raise PhaseThirteenPublicTraceValidationError(
                    "successful order execution trace contains an illegal branch"
                )
            branch_cursor += len(_SAME_ORDER_QUERY_TRACE)
            query_count += 1
            if query_count > _MAXIMUM_ORDER_QUERY_TRACE_COUNT:
                raise PhaseThirteenPublicTraceValidationError(
                    "successful order execution trace exceeds the query budget"
                )
        terminal_evidence_complete = False

        if (
            branch_ids[
                branch_cursor : branch_cursor + len(fill_application_trace)
            ]
            == fill_application_trace
        ):
            branch_cursor += len(fill_application_trace)
            applied_fill_count += 1
            terminal_evidence_complete = True
            continue  # Partial stream 결과 뒤에도 예약 query 또는 다음 stream 결과가 이어질 수 있다.
        if branch_ids[branch_cursor:] == ("10",) and applied_fill_count > 0:
            branch_cursor += 1
            terminal_evidence_complete = True  # 기존 partial 뒤 새 delta 없는 terminal summary다.

    if applied_fill_count < 1 or not terminal_evidence_complete:
        raise PhaseThirteenPublicTraceValidationError(
            "successful order execution trace lacks terminal fill evidence"
        )


def _validate_order_execution_traces(
    value: object,
    attempts: tuple[Mapping[str, object], ...],
    results: tuple[Mapping[str, object], ...],
    *,
    require_complete_success: bool,
) -> tuple[Mapping[str, object], ...]:
    """
    함수 이름: _validate_order_execution_traces()
    기능: Artifact의 주문별 실제 1~14 entry를 attempt/result identity·version·result와 독립 결속한다.
    인자: value -> order_execution_traces JSON array
        attempts -> 먼저 검증한 실제 submit attempt tuple
        results -> 먼저 검증한 same-client terminal result tuple
        require_complete_success -> SUCCESS용 exact branch grammar와 전 entry 성공을 요구할지 여부
    반환값: 검증된 주문별 trace mapping tuple
    작성 날짜: 2026/08/31
    """
    if type(require_complete_success) is not bool:
        raise TypeError("require_complete_success must be a bool")
    trace_groups = _require_contiguous_sequence(
        _require_list(value, "order execution traces"),
        _ORDER_EXECUTION_TRACE_FIELDS,
        "order execution traces",
    )
    if len(trace_groups) != len(attempts):
        raise PhaseThirteenPublicTraceValidationError(
            "order execution traces must correspond one-to-one with attempts"
        )
    latest_results_by_client_id: dict[str, Mapping[str, object]] = {}
    for result in results:
        latest_results_by_client_id[str(result["client_order_id"])] = result

    for trace_group, attempt in zip(trace_groups, attempts, strict=True):
        intent_id = _require_canonical_text(
            trace_group["intent_id"],
            "order execution trace intent_id",
        )
        client_order_id = _require_canonical_text(
            trace_group["client_order_id"],
            "order execution trace client_order_id",
        )
        side = _require_identifier(
            trace_group["side"],
            "order execution trace side",
        )
        if (
            intent_id != attempt["intent_id"]
            or client_order_id != attempt["client_order_id"]
            or side != attempt["side"]
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "order execution trace identity does not match its attempt"
            )
        trace_entries = _require_contiguous_sequence(
            _require_list(
                trace_group["entries"],
                "order execution trace entries",
            ),
            _ORDER_EXECUTION_TRACE_ENTRY_FIELDS,
            "order execution trace entries",
        )
        if not trace_entries:
            raise PhaseThirteenPublicTraceValidationError(
                "order execution trace entries must not be empty"
            )
        matching_result = latest_results_by_client_id.get(str(client_order_id))
        matching_exchange_order_id = (
            None
            if matching_result is None
            else matching_result["exchange_order_id"]
        )
        previous_version_before = -1
        previous_version_after = -1
        exchange_identity_observed = False
        message_ids: list[str] = []
        for trace_entry in trace_entries:
            message_id = _require_canonical_text(
                trace_entry["message_id"],
                "order execution trace message_id",
            )
            if message_id not in _ORDER_TRACE_MESSAGE_IDS:
                raise PhaseThirteenPublicTraceValidationError(
                    "order execution trace contains an unknown message ID"
                )
            message_ids.append(str(message_id))
            _require_canonical_text(
                trace_entry["command_event_id"],
                "order execution trace command_event_id",
            )
            order_id = _require_canonical_text(
                trace_entry["order_id"],
                "order execution trace order_id",
                allow_none=True,
            )

            # Submit prefix는 exchange 미래 사실을 주장할 수 없고 최초 ID는 apply/reapply 7·9에서만 나타난다.
            if (
                message_id in _ORDER_TRACE_PRE_EXCHANGE_ID_MESSAGE_IDS
                and order_id is not None
            ):
                raise PhaseThirteenPublicTraceValidationError(
                    "order execution trace exposes exchange identity before its result"
                )
            if order_id is None:
                if exchange_identity_observed:
                    raise PhaseThirteenPublicTraceValidationError(
                        "order execution trace exchange identity regressed to None"
                    )
            else:
                if not exchange_identity_observed and message_id not in {"7", "9"}:
                    raise PhaseThirteenPublicTraceValidationError(
                        "order execution trace exchange identity first appeared outside result application"
                    )
                if order_id != matching_exchange_order_id:
                    raise PhaseThirteenPublicTraceValidationError(
                        "order execution trace exchange identity is inconsistent"
                    )
                exchange_identity_observed = True

            # Message 10 fill summary와 durable suffix는 matching terminal result의 concrete ID를 필수로 갖는다.
            if (
                message_id in _ORDER_TRACE_EXCHANGE_ID_REQUIRED_MESSAGE_IDS
                and (
                    matching_exchange_order_id is None
                    or order_id != matching_exchange_order_id
                )
            ):
                raise PhaseThirteenPublicTraceValidationError(
                    "terminal order trace lacks its matching exchange identity"
                )
            context_version_before = _require_nonnegative_integer(
                trace_entry["context_version_before"],
                "order execution trace context_version_before",
            )
            context_version_after = _require_nonnegative_integer(
                trace_entry["context_version_after"],
                "order execution trace context_version_after",
            )
            # 다음 entry의 before는 직전 after보다 작을 수 없고 같은-version microstep은 그대로 허용한다.
            if (
                context_version_after < context_version_before
                or context_version_before < previous_version_before
                or context_version_before < previous_version_after
                or context_version_after < previous_version_after
            ):
                raise PhaseThirteenPublicTraceValidationError(
                    "order execution trace Context version moved backward"
                )
            previous_version_before = context_version_before
            previous_version_after = context_version_after
            trace_result = _require_identifier(
                trace_entry["result"],
                "order execution trace result",
            )
            failure_code = _require_identifier(
                trace_entry["failure_code"],
                "order execution trace failure_code",
                allow_none=True,
            )
            if (trace_result == "SUCCESS") != (failure_code is None):
                raise PhaseThirteenPublicTraceValidationError(
                    "order execution trace result and failure code disagree"
                )
            if trace_result not in {"SUCCESS", "FAILURE"}:
                raise PhaseThirteenPublicTraceValidationError(
                    "order execution trace result is unsupported"
                )
            if require_complete_success and trace_result != "SUCCESS":
                raise PhaseThirteenPublicTraceValidationError(
                    "successful artifact contains a failed order trace entry"
                )

        if require_complete_success:
            _validate_success_order_message_sequence(
                tuple(message_ids),
                side=str(side),
            )
            # Production submit prefix는 message 1에서 v를 읽고 message 2가 정확히 v→v+1 mutation한다.
            initial_entry = trace_entries[0]
            mutation_entry = trace_entries[1]
            initial_version = int(initial_entry["context_version_before"])
            if (
                int(initial_entry["context_version_after"])
                != initial_version
                or int(mutation_entry["context_version_before"])
                != initial_version
                or int(mutation_entry["context_version_after"])
                != initial_version + 1
            ):
                raise PhaseThirteenPublicTraceValidationError(
                    "successful order trace does not contain the exact submit mutation"
                )
            expected_prefix = (
                _BUY_ORDER_TRACE_PREFIX
                if side == "BUY"
                else _SELL_ORDER_TRACE_PREFIX
            )
            evaluation_id = str(attempt["evaluation_id"])
            attempt_intent_id = str(attempt["intent_id"])
            if any(
                trace_entry["command_event_id"] != evaluation_id
                for trace_entry in trace_entries[: len(expected_prefix)]
            ):
                raise PhaseThirteenPublicTraceValidationError(
                    "order submit trace does not match its attempt evaluation"
                )

            # Message 8/9 비동기 경계 전은 evaluation, 경계부터는 intent만 사용한다.
            asynchronous_identity_observed = False
            for trace_entry in trace_entries[len(expected_prefix) : -1]:
                command_event_id = str(trace_entry["command_event_id"])
                if trace_entry["message_id"] in {"8", "9"}:
                    asynchronous_identity_observed = True
                expected_command_event_id = (
                    attempt_intent_id
                    if asynchronous_identity_observed
                    else evaluation_id
                )
                if command_event_id != expected_command_event_id:
                    raise PhaseThirteenPublicTraceValidationError(
                        "successful order trace producer identity changed outside its async boundary"
                    )

    return trace_groups  # Canonical group와 entry 순서는 digest에 기록된 그대로 유지한다.


def _validate_submit_time_filter_evidence(
    value: object,
    attempts: tuple[Mapping[str, object], ...],
    *,
    schema_version: int,
) -> tuple[Mapping[str, object], ...]:
    """
    함수 이름: _validate_submit_time_filter_evidence()
    기능: production prepare-time filter 관찰을 matching attempt와 1:1 결속하고 최종 제출 산술을 검증한다.
    인자: value -> submit_time_filter_evidence JSON array
        attempts -> 먼저 검증한 order attempt tuple
        schema_version -> top-level trace schema version
    반환값: 검증된 submit-time filter evidence tuple
    작성 날짜: 2026/08/31
    """
    # V3 entry만 full account filter와 all-symbol open state 증거를 exact 필드로 요구한다.
    expected_fields = (
        _SUBMIT_TIME_FILTER_EVIDENCE_FIELDS_V3
        if schema_version == 3
        else _SUBMIT_TIME_FILTER_EVIDENCE_FIELDS_V2
    )
    entries = _require_contiguous_sequence(
        _require_list(value, "submit-time filter evidence"),
        expected_fields,
        "submit-time filter evidence",
    )
    attempts_by_client_id = {
        str(attempt["client_order_id"]): attempt for attempt in attempts
    }
    observed_client_order_ids: set[str] = set()
    previous_observed_at: datetime | None = None
    for entry in entries:
        intent_id = _require_canonical_text(
            entry["intent_id"],
            "submit-time filter intent_id",
        )
        client_order_id = _require_canonical_text(
            entry["client_order_id"],
            "submit-time filter client_order_id",
        )
        side = _require_identifier(entry["side"], "submit-time filter side")
        observed_at = _require_utc_timestamp(
            entry["observed_at"],
            "submit-time filter observed_at",
        )
        account_filters_observed_at = _require_utc_timestamp(
            entry["account_filters_observed_at"],
            "submit-time account filters observed_at",
        )
        reference_price_observed_at = _require_utc_timestamp(
            entry["reference_price_observed_at"],
            "submit-time reference price observed_at",
        )
        ordered_safety_observations = [
            account_filters_observed_at,
            observed_at,
        ]
        if schema_version == 3:
            (
                account_open_orders_observed_at,
                account_open_order_lists_observed_at,
            ) = _validate_account_open_state_evidence(
                entry,
                location="submit-time filter evidence",
            )
            ordered_safety_observations.extend(
                (
                    account_open_orders_observed_at,
                    account_open_order_lists_observed_at,
                )
            )
        ordered_safety_observations.append(reference_price_observed_at)
        latest_observed_at = max(ordered_safety_observations)
        if ordered_safety_observations != sorted(
            ordered_safety_observations
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "submit-time safety observations are outside the fetch order"
            )
        if (
            previous_observed_at is not None
            and min(ordered_safety_observations) < previous_observed_at
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "submit-time filter observation times must be monotonic"
            )
        previous_observed_at = latest_observed_at
        if (
            client_order_id is None
            or client_order_id not in attempts_by_client_id
            or client_order_id in observed_client_order_ids
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "submit-time filter evidence has no unique matching attempt"
            )
        observed_client_order_ids.add(client_order_id)
        matching_attempt = attempts_by_client_id[client_order_id]
        if (
            entry["sequence"] != matching_attempt["sequence"]
            or intent_id != matching_attempt["intent_id"]
            or side != matching_attempt["side"]
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "submit-time filter evidence changed attempt provenance"
            )
        attempted_at = _require_utc_timestamp(
            matching_attempt["attempted_at"],
            "matching order attempted_at",
        )
        if latest_observed_at > attempted_at:
            raise PhaseThirteenPublicTraceValidationError(
                "submit-time safety observation follows POST submission start"
            )
        # Composite의 첫 fetch부터 POST 시작까지 고정 freshness 경계를 넘기면
        # 중간 account drift를 배제할 수 없다.
        if (
            schema_version == 3
            and attempted_at - min(ordered_safety_observations)
            > _MAXIMUM_ORDER_PREPARATION_AGE
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "submit-time filter evidence exceeded its maximum age"
            )

        # Rules·signed account filter·reference price는 preflight과 별개이며 matching final quantity에만 권위를 갖는다.
        rules = _require_exact_mapping(
            entry["rules"],
            _FRESH_FILTER_RULE_FIELDS,
            "submit-time filter rules",
        )
        _validate_fresh_filter_rule_values(rules, "submit-time filter rules")
        account_asset_filters = _validate_account_asset_filters(
            entry["account_asset_filters"],
            "submit-time account asset filters",
        )
        if schema_version == 3:
            _validate_account_relevant_filters(
                entry["account_relevant_filters"],
                account_asset_filters=account_asset_filters,
                public_relevant_filters=entry["public_relevant_filters"],
                public_rules=rules,
                location="submit-time account relevant filters",
            )
        reference_price = _validate_reference_price(
            entry["reference_price"],
            observed_at=reference_price_observed_at,
            location="submit-time reference price",
        )
        _validate_order_attempt_against_fresh_filters(
            matching_attempt,
            rules,
            account_asset_filters,
            reference_price,
        )

    if observed_client_order_ids != set(attempts_by_client_id):
        raise PhaseThirteenPublicTraceValidationError(
            "every order attempt requires one submit-time filter observation"
        )

    return entries  # Prepare-time rule은 adapter의 POST 직전 submission-start evidence와 결속한다.


def _validate_incremental_fill(
    value: object,
) -> Mapping[str, object]:
    """
    함수 이름: _validate_incremental_fill()
    기능: 한 incremental fill과 durable Trade 대응을 exact schema로 검증한다.
    인자: value -> incremental fill JSON object
    반환값: 검증된 fill mapping
    작성 날짜: 2026/08/31
    """
    fill = _require_exact_mapping(value, _FILL_FIELDS, "incremental fill")
    fill_id = _require_canonical_text(fill["fill_id"], "fill fill_id")
    _require_canonical_text(fill["durable_trade_id"], "fill durable_trade_id")
    _require_utc_timestamp(fill["event_time"], "fill event_time")
    price = _require_plain_decimal(
        fill["price"],
        "fill price",
        minimum=Decimal("0.000000000000000001"),
    )
    quantity = _require_plain_decimal(
        fill["quantity"],
        "fill quantity",
        minimum=Decimal("0.000000000000000001"),
    )
    quote_amount = _require_plain_decimal(
        fill["quote_amount"],
        "fill quote_amount",
        minimum=Decimal("0"),
    )
    fee_amount = _require_plain_decimal(
        fill["fee_amount"],
        "fill fee_amount",
        minimum=Decimal("0"),
    )
    fee_quote_amount = _require_plain_decimal(
        fill["fee_quote_amount"],
        "fill fee_quote_amount",
        minimum=Decimal("0"),
    )
    if (
        fill_id is None
        or price is None
        or quantity is None
        or quote_amount is None
        or fee_amount is None
        or fee_quote_amount is None
    ):
        raise AssertionError("required fill decimal unexpectedly resolved to None")
    if _CANONICAL_NONNEGATIVE_INTEGER_PATTERN.fullmatch(fill_id) is None:
        raise PhaseThirteenPublicTraceValidationError(
            "fill ID must be a canonical non-negative integer string"
        )
    if _multiply_decimal128(price, quantity) != quote_amount:
        raise PhaseThirteenPublicTraceValidationError(
            "fill quote amount does not match price and quantity"
        )
    fee_asset = _require_identifier(
        fill["fee_asset"],
        "fill fee_asset",
    )
    if fee_asset not in {"ETH", "USDT"}:
        raise PhaseThirteenPublicTraceValidationError(
            "fill fee asset must be ETH or USDT"
        )

    # Domain Fill과 같은 Decimal128 환산을 재실행해 quote fee 위조를 차단한다.
    expected_fee_quote_amount = (
        fee_amount
        if fee_asset == "USDT"
        else _multiply_decimal128(fee_amount, price)
    )
    if fee_quote_amount != expected_fee_quote_amount:
        raise PhaseThirteenPublicTraceValidationError(
            "fill quote fee does not match its asset conversion"
        )

    return fill  # Raw execution payload 없이 accounting에 필요한 normalized 사실만 반환한다.


def _validate_order_results(
    value: object,
    attempts: tuple[Mapping[str, object], ...],
    *,
    schema_version: int,
) -> tuple[Mapping[str, object], ...]:
    """
    함수 이름: _validate_order_results()
    기능: terminal-monotonic result, global exchange identity와 final fill aggregate를 검증한다.
    인자: value -> order result JSON array
        attempts -> 먼저 검증한 order attempt tuple
        schema_version -> top-level trace schema version
    반환값: 검증된 order result tuple
    작성 날짜: 2026/08/31
    """
    entries = _require_contiguous_sequence(
        _require_list(value, "order results"),
        _ORDER_RESULT_FIELDS,
        "order results",
    )
    attempts_by_client_id = {
        str(attempt["client_order_id"]): attempt for attempt in attempts
    }
    exchange_ids_by_client_id: dict[str, str] = {}
    client_ids_by_exchange_id: dict[str, str] = {}
    latest_known_status_by_client_id: dict[str, str] = {}
    latest_observed_at_by_client_id: dict[str, datetime] = {}
    fill_quantities_by_client_id: dict[str, list[Decimal]] = {
        client_order_id: [] for client_order_id in attempts_by_client_id
    }
    observed_fill_keys: set[tuple[str, str]] = set()
    for entry in entries:
        observed_at = _require_utc_timestamp(
            entry["observed_at"],
            "order result observed_at",
        )
        intent_id = _require_canonical_text(entry["intent_id"], "result intent_id")
        client_order_id = _require_canonical_text(
            entry["client_order_id"],
            "result client_order_id",
        )
        if client_order_id is None or client_order_id not in attempts_by_client_id:
            raise PhaseThirteenPublicTraceValidationError(
                "order result has no matching attempt"
            )
        if attempts_by_client_id[client_order_id]["intent_id"] != intent_id:
            raise PhaseThirteenPublicTraceValidationError(
                "order result changed intent provenance"
            )
        previous_observed_at = latest_observed_at_by_client_id.get(client_order_id)
        if previous_observed_at is not None and observed_at < previous_observed_at:
            raise PhaseThirteenPublicTraceValidationError(
                "order result times must be monotonic per client"
            )
        latest_observed_at_by_client_id[client_order_id] = observed_at
        exchange_order_id = _require_canonical_text(
            entry["exchange_order_id"],
            "result exchange_order_id",
            allow_none=True,
        )
        if exchange_order_id is not None:
            if (
                not exchange_order_id.isdigit()
                or exchange_order_id == "0"
                or str(int(exchange_order_id)) != exchange_order_id
            ):
                raise PhaseThirteenPublicTraceValidationError(
                    "result exchange order ID must be a positive canonical integer string"
                )
            previous_exchange_id = exchange_ids_by_client_id.setdefault(
                client_order_id,
                exchange_order_id,
            )
            if previous_exchange_id != exchange_order_id:
                raise PhaseThirteenPublicTraceValidationError(
                    "order result changed exchange identity"
                )
            previous_client_id = client_ids_by_exchange_id.setdefault(
                exchange_order_id,
                client_order_id,
            )
            if previous_client_id != client_order_id:
                raise PhaseThirteenPublicTraceValidationError(
                    "exchange order identity maps to multiple clients"
                )
        status = _require_identifier(entry["status"], "result status")
        if status not in _ORDER_RESULT_STATUSES:
            raise PhaseThirteenPublicTraceValidationError(
                "order result status is unsupported"
            )
        failure_code = _require_identifier(
            entry["failure_code"],
            "result failure_code",
            allow_none=True,
        )
        if status in {"UNKNOWN", "REJECTED"} and failure_code is None:
            raise PhaseThirteenPublicTraceValidationError(
                "uncertain or rejected result requires a failure code"
            )
        if status not in {"UNKNOWN", "REJECTED"} and failure_code is not None:
            raise PhaseThirteenPublicTraceValidationError(
                "successful result must not contain a failure code"
            )

        # UNKNOWN은 기존 concrete 상태를 지우지 않고 known active·terminal 상태만 단조 진행한다.
        previous_status = latest_known_status_by_client_id.get(client_order_id)
        if status != "UNKNOWN":
            if previous_status in _TERMINAL_ORDER_RESULT_STATUSES:
                if status != previous_status:
                    raise PhaseThirteenPublicTraceValidationError(
                        "terminal order result changed or regressed"
                    )
            elif (
                previous_status is not None
                and status in _ACTIVE_ORDER_STATUS_PROGRESS
                and previous_status in _ACTIVE_ORDER_STATUS_PROGRESS
                and _ACTIVE_ORDER_STATUS_PROGRESS[status]
                < _ACTIVE_ORDER_STATUS_PROGRESS[previous_status]
            ):
                raise PhaseThirteenPublicTraceValidationError(
                    "active order result regressed"
                )
            latest_known_status_by_client_id[client_order_id] = str(status)

        # Result 배열은 이미 적용한 fill을 반복하지 않고 각 fill을 durable Trade ID와 연결한다.
        incremental_fills = _require_list(
            entry["incremental_fills"],
            "incremental fills",
        )
        previous_incremental_fill_key: tuple[datetime, int] | None = None
        for fill_value in incremental_fills:
            fill = _validate_incremental_fill(fill_value)
            fill_event_time = _require_utc_timestamp(
                fill["event_time"],
                "fill event_time",
            )
            attempted_at = _require_utc_timestamp(
                attempts_by_client_id[client_order_id]["attempted_at"],
                "matching order attempted_at",
            )
            if fill_event_time < attempted_at or fill_event_time > observed_at:
                raise PhaseThirteenPublicTraceValidationError(
                    "incremental fill lies outside its attempt and observation"
                )
            incremental_fill_key = (
                fill_event_time,
                int(str(fill["fill_id"])),
            )
            if (
                schema_version == 3
                and previous_incremental_fill_key is not None
                and incremental_fill_key <= previous_incremental_fill_key
            ):
                raise PhaseThirteenPublicTraceValidationError(
                    "v3 incremental fills must strictly follow exchange order"
                )
            previous_incremental_fill_key = incremental_fill_key
            fill_key = (client_order_id, str(fill["fill_id"]))
            if fill_key in observed_fill_keys:
                raise PhaseThirteenPublicTraceValidationError(
                    "order results contain duplicate incremental fills"
                )
            observed_fill_keys.add(fill_key)
            fill_quantities_by_client_id[client_order_id].append(
                Decimal(str(fill["quantity"]))
            )
        if status == "FILLED" and (
            exchange_order_id is None or not incremental_fills
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "FILLED result requires exchange identity and fills"
            )

    # FILLED client의 모든 incremental fill 합은 filter 뒤 final submitted quantity와 exact해야 한다.
    for client_order_id, latest_status in latest_known_status_by_client_id.items():
        if latest_status != "FILLED":
            continue
        submitted_quantity = Decimal(
            str(
                attempts_by_client_id[client_order_id][
                    "final_submitted_quantity"
                ]
            )
        )
        if _sum_decimal128(
            tuple(fill_quantities_by_client_id[client_order_id])
        ) != submitted_quantity:
            raise PhaseThirteenPublicTraceValidationError(
                "FILLED quantity does not match final submitted quantity"
            )

    return entries  # Query와 stream result가 같은 attempt identity에만 귀속된다.


def _validate_transport_ui_events(
    value: object,
    *,
    schema_version: int,
) -> tuple[str, tuple[Mapping[str, object], ...]]:
    """
    함수 이름: _validate_transport_ui_events()
    기능: 실제 envelope session/event/sequence와 atomic Trade publication truth를 검증한다.
    인자: value -> transport_session_id와 event array를 가진 exact batch object
        schema_version -> top-level trace schema version
    반환값: transport session UUID와 검증된 event tuple
    작성 날짜: 2026/08/31
    """
    batch = _require_exact_mapping(
        value,
        _TRANSPORT_UI_BATCH_FIELDS,
        "transport UI event batch",
    )
    transport_session_id = _require_uuid4(
        batch["transport_session_id"],
        "transport session_id",
    )
    event_values = _require_list(batch["events"], "transport UI events")
    entries = tuple(
        _require_exact_mapping(
            event_value,
            _TRANSPORT_UI_EVENT_FIELDS,
            "transport UI event",
        )
        for event_value in event_values
    )
    observed_event_ids: set[str] = set()
    previous_transport_sequence = 0
    previous_published_at: datetime | None = None
    latest_version_by_aggregate: dict[str, int] = {}
    trading_session_related_id: str | None = None
    for entry in entries:
        event_id = _require_uuid4(entry["event_id"], "transport event_id")
        if event_id in observed_event_ids:
            raise PhaseThirteenPublicTraceValidationError(
                "transport events contain duplicate event identities"
            )
        observed_event_ids.add(event_id)
        transport_sequence = _require_nonnegative_integer(
            entry["transport_sequence"],
            "transport sequence",
        )
        if transport_sequence <= previous_transport_sequence:
            raise PhaseThirteenPublicTraceValidationError(
                "transport sequences must strictly increase"
            )
        previous_transport_sequence = transport_sequence
        if schema_version == 3 and transport_sequence != len(observed_event_ids):
            raise PhaseThirteenPublicTraceValidationError(
                "v3 transport sequences must be contiguous from one"
            )
        aggregate = _require_identifier(entry["aggregate"], "UI aggregate")
        if aggregate is None:
            raise AssertionError("required UI aggregate unexpectedly resolved to None")
        event_type = _require_identifier(entry["event_type"], "UI event_type")
        if (
            event_type not in _TRANSPORT_EVENT_AGGREGATES
            or _TRANSPORT_EVENT_AGGREGATES[str(event_type)] != aggregate
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "transport event aggregate does not match its event type"
            )
        aggregate_version = entry["aggregate_version"]
        if event_type in {"ORDER_EXECUTED", "PERFORMANCE_UPDATED"}:
            if aggregate_version is not None:
                raise PhaseThirteenPublicTraceValidationError(
                    "atomic Trade events must not synthesize aggregate versions"
                )
        else:
            normalized_aggregate_version = _require_nonnegative_integer(
                aggregate_version,
                "transport aggregate_version",
            )
            previous_aggregate_version = latest_version_by_aggregate.get(
                aggregate
            )
            if (
                previous_aggregate_version is not None
                and normalized_aggregate_version < previous_aggregate_version
            ):
                raise PhaseThirteenPublicTraceValidationError(
                    "transport aggregate versions must not regress"
                )
            latest_version_by_aggregate[aggregate] = (
                normalized_aggregate_version
            )  # 동일 version의 recovery 재발행은 허용하되 감소는 차단한다.
        related_id = _require_canonical_text(
            entry["related_id"],
            "UI related_id",
            allow_none=True,
        )
        if event_type in {"ORDER_EXECUTED", "PERFORMANCE_UPDATED"} and (
            related_id is None
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "atomic Trade events require a durable Trade ID"
            )
        if event_type == "ACCOUNT_UPDATED" and related_id is not None:
            raise PhaseThirteenPublicTraceValidationError(
                "ACCOUNT_UPDATED must not contain a related identity"
            )
        if event_type == "TRADING_SESSION_UPDATED":
            if related_id is None:
                raise PhaseThirteenPublicTraceValidationError(
                    "TRADING_SESSION_UPDATED requires a session identity"
                )
            _require_uuid4(related_id, "trading session related_id")
            if trading_session_related_id is None:
                trading_session_related_id = related_id
            elif related_id != trading_session_related_id:
                raise PhaseThirteenPublicTraceValidationError(
                    "transport events changed trading session identity"
                )
        # Case 2 observer는 shared clock과 publication lock 안에서 sequence·published_at을 함께 고정한다.
        published_at = _require_utc_timestamp(
            entry["published_at"],
            "UI published_at",
        )
        if (
            previous_published_at is not None
            and published_at < previous_published_at
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "transport publication times must be monotonic"
            )
        previous_published_at = published_at

    # ORDER_EXECUTED와 PERFORMANCE_UPDATED는 N/N+1, 같은 occurred_at과 같은 Trade ID여야 한다.
    for event_index, entry in enumerate(entries):
        event_type = entry["event_type"]
        if event_type == "ORDER_EXECUTED":
            if event_index + 1 >= len(entries):
                raise PhaseThirteenPublicTraceValidationError(
                    "ORDER_EXECUTED lacks its atomic Performance event"
                )
            performance_event = entries[event_index + 1]
            if (
                performance_event["event_type"] != "PERFORMANCE_UPDATED"
                or performance_event["transport_sequence"]
                != entry["transport_sequence"] + 1
                or performance_event["published_at"] != entry["published_at"]
                or performance_event["related_id"] != entry["related_id"]
                or performance_event["aggregate_version"] is not None
            ):
                raise PhaseThirteenPublicTraceValidationError(
                    "Trade transport publication pair is not atomic"
                )
        if event_type == "PERFORMANCE_UPDATED" and (
            event_index == 0
            or entries[event_index - 1]["event_type"] != "ORDER_EXECUTED"
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "PERFORMANCE_UPDATED lacks its ORDER_EXECUTED owner"
            )

    return transport_session_id, entries  # Envelope truth는 synthetic state version 없이 그대로 보존한다.


def _validate_recovery(value: object) -> Mapping[str, object]:
    """
    함수 이름: _validate_recovery()
    기능: same-run authoritative Position만 사용하는 recovery와 최종 zero-state를 검증한다.
    인자: value -> recovery JSON object
    반환값: 검증된 recovery mapping
    작성 날짜: 2026/08/31
    """
    recovery = _require_exact_mapping(value, _RECOVERY_FIELDS, "recovery")
    if type(recovery["required"]) is not bool or type(recovery["attempted"]) is not bool:
        raise PhaseThirteenPublicTraceValidationError(
            "recovery flags must be booleans"
        )
    recovery_outcome = _require_identifier(recovery["outcome"], "recovery outcome")
    if recovery_outcome not in _RECOVERY_OUTCOMES:
        raise PhaseThirteenPublicTraceValidationError(
            "recovery outcome is unsupported"
        )
    intent_id = _require_canonical_text(
        recovery["intent_id"],
        "recovery intent_id",
        allow_none=True,
    )
    client_order_id = _require_canonical_text(
        recovery["client_order_id"],
        "recovery client_order_id",
        allow_none=True,
    )

    # Position·free·submitted 수량은 float 없이 원문 Decimal이고 count는 모두 non-negative 정수다.
    authoritative_quantity = _require_plain_decimal(
        recovery["authoritative_position_quantity"],
        "recovery authoritative position quantity",
        minimum=Decimal("0"),
    )
    effective_free_quantity = _require_plain_decimal(
        recovery["effective_free_quantity"],
        "recovery effective free quantity",
        minimum=Decimal("0"),
    )
    submitted_quantity = _require_plain_decimal(
        recovery["submitted_quantity"],
        "recovery submitted quantity",
        minimum=Decimal("0.000000000000000001"),
        allow_none=True,
    )
    final_position_quantity = _require_plain_decimal(
        recovery["final_position_quantity"],
        "recovery final position quantity",
        minimum=Decimal("0"),
    )
    for field_name in (
        "pending_order_count",
        "matching_open_order_count",
        "duplicate_order_count",
        "duplicate_trade_count",
    ):
        _require_nonnegative_integer(recovery[field_name], f"recovery {field_name}")

    # required/attempted/outcome 조합을 exact하게 묶어 recovery 성공을 합성하지 못하게 한다.
    required = bool(recovery["required"])
    attempted = bool(recovery["attempted"])
    if not required and (
        attempted
        or recovery_outcome != "NOT_REQUIRED"
        or intent_id is not None
        or client_order_id is not None
        or submitted_quantity is not None
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "non-required recovery contains mutation evidence"
        )
    if required and attempted and (
        recovery_outcome not in {"SUCCESS", "FAILED"}
        or intent_id is None
        or client_order_id is None
        or submitted_quantity is None
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "attempted recovery is incomplete"
        )
    if required and not attempted and recovery_outcome != "BLOCKED":
        raise PhaseThirteenPublicTraceValidationError(
            "unattempted required recovery must be blocked"
        )
    if (
        submitted_quantity is not None
        and authoritative_quantity is not None
        and effective_free_quantity is not None
        and submitted_quantity > min(authoritative_quantity, effective_free_quantity)
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "recovery submitted quantity exceeds authoritative exposure"
        )
    if recovery_outcome == "SUCCESS" and final_position_quantity != Decimal("0"):
        raise PhaseThirteenPublicTraceValidationError(
            "successful recovery must close the position"
        )

    return recovery  # Caller는 final_state와 동일 zero-state인지 한 번 더 비교한다.


def _validate_run_durable_trades(
    value: object,
) -> tuple[Mapping[str, object], ...]:
    """
    함수 이름: _validate_run_durable_trades()
    기능: baseline과 분리한 이번 run의 normalized durable Trade schema와 회계 수치를 검증한다.
    인자: value -> run_durable_trades JSON array
    반환값: 검증된 durable Trade tuple
    작성 날짜: 2026/08/31
    """
    trade_values = _require_list(value, "run durable trades")
    trades = tuple(
        _require_exact_mapping(
            trade_value,
            _RUN_DURABLE_TRADE_FIELDS,
            "run durable Trade",
        )
        for trade_value in trade_values
    )
    for trade in trades:
        for field_name in ("trade_id", "client_order_id"):
            _require_canonical_text(trade[field_name], f"Trade {field_name}")
        exchange_order_id = _require_canonical_text(
            trade["exchange_order_id"],
            "Trade exchange_order_id",
        )
        if (
            exchange_order_id is None
            or not exchange_order_id.isdigit()
            or exchange_order_id == "0"
            or str(int(exchange_order_id)) != exchange_order_id
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "Trade exchange order ID must be a positive canonical integer string"
            )
        if trade["trade_id"] != f"trade-{exchange_order_id}":
            raise PhaseThirteenPublicTraceValidationError(
                "Trade identity must derive from its exchange order ID"
            )
        if trade["symbol"] != "ETHUSDT":
            raise PhaseThirteenPublicTraceValidationError(
                "run durable Trade symbol must be ETHUSDT"
            )
        side = _require_identifier(trade["side"], "Trade side")
        if side not in {"BUY", "SELL"}:
            raise PhaseThirteenPublicTraceValidationError(
                "run durable Trade side is unsupported"
            )
        _require_identifier(trade["regime"], "Trade regime")
        if trade["strategy"] != "CASE_C":
            raise PhaseThirteenPublicTraceValidationError(
                "run durable Trade strategy must be CASE_C"
            )
        _require_utc_timestamp(trade["executed_at"], "Trade executed_at")

        # Executed amount와 average price를 precision 34로 재계산하고 fee quote도 별도 보존한다.
        executed_quantity = _require_plain_decimal(
            trade["executed_quantity"],
            "Trade executed_quantity",
            minimum=Decimal("0.000000000000000001"),
        )
        executed_amount = _require_plain_decimal(
            trade["executed_amount"],
            "Trade executed_amount",
            minimum=Decimal("0.000000000000000001"),
        )
        average_fill_price = _require_plain_decimal(
            trade["average_fill_price"],
            "Trade average_fill_price",
            minimum=Decimal("0.000000000000000001"),
        )
        fee_amount = _require_plain_decimal(
            trade["fee_amount"],
            "Trade fee_amount",
            minimum=Decimal("0"),
        )
        fee_quote_amount = _require_plain_decimal(
            trade["fee_quote_amount"],
            "Trade fee_quote_amount",
            minimum=Decimal("0"),
        )
        fee_asset = _require_identifier(trade["fee_asset"], "Trade fee_asset")
        if (
            executed_quantity is None
            or executed_amount is None
            or average_fill_price is None
            or fee_amount is None
            or fee_quote_amount is None
            or fee_asset is None
        ):
            raise AssertionError("required Trade value unexpectedly resolved to None")
        if _divide_decimal128(
            executed_amount,
            executed_quantity,
        ) != average_fill_price:
            raise PhaseThirteenPublicTraceValidationError(
                "Trade amount does not match average price and quantity"
            )
        if fee_asset not in {"ETH", "USDT"}:
            raise PhaseThirteenPublicTraceValidationError(
                "Trade fee asset must be ETH or USDT"
            )
        if fee_asset == "USDT" and fee_quote_amount != fee_amount:
            raise PhaseThirteenPublicTraceValidationError(
                "USDT Trade fee must equal its quote amount"
            )

        # BUY realized/exit은 null이고 SELL은 normalized realized PnL과 exit reason을 함께 가진다.
        realized_profit_loss = _require_plain_decimal(
            trade["realized_profit_loss"],
            "Trade realized profit loss",
            allow_none=True,
        )
        exit_reason = _require_identifier(
            trade["exit_reason"],
            "Trade exit reason",
            allow_none=True,
        )
        if side == "BUY" and (
            realized_profit_loss is not None or exit_reason is not None
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "BUY Trade must not contain realized or exit fields"
            )
        if side == "SELL" and (
            realized_profit_loss is None or exit_reason is None
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "SELL Trade requires realized PnL and exit reason"
            )

    return trades  # Duplicate와 terminal/fill 상관관계는 전체 run sequence에서 계산한다.


def _validate_final_state(
    value: object,
    *,
    baseline_history_count: int,
    attempts: tuple[Mapping[str, object], ...],
    run_trades: tuple[Mapping[str, object], ...],
) -> Mapping[str, object]:
    """
    함수 이름: _validate_final_state()
    기능: fresh runtime zero-state와 baseline+run Performance 산술을 exact하게 검증한다.
    인자: value -> final_state JSON object
        baseline_history_count -> 별도 top-level baseline Trade 개수
        attempts -> 검증된 실제 order attempt tuple
        run_trades -> 검증된 이번 run durable Trade tuple
    반환값: 검증된 final state mapping
    작성 날짜: 2026/08/31
    """
    final_state = _require_exact_mapping(value, _FINAL_STATE_FIELDS, "final state")
    _require_plain_decimal(
        final_state["position_quantity"],
        "final position quantity",
        minimum=Decimal("0"),
    )
    for field_name in (
        "pending_order_count",
        "unknown_order_count",
        "matching_open_order_count",
        "actual_order_count",
        "duplicate_order_count",
        "duplicate_trade_count",
    ):
        _require_nonnegative_integer(final_state[field_name], f"final {field_name}")
    _require_utc_timestamp(final_state["verified_at"], "final verified_at")
    _require_uuid4(
        final_state["fresh_runtime_session_id"],
        "final fresh runtime session_id",
    )

    # 실제 주문·Trade duplicate는 normalized sequence identity에서 계산해 reported count와 대조한다.
    attempt_client_ids = [str(attempt["client_order_id"]) for attempt in attempts]
    duplicate_order_count = len(attempt_client_ids) - len(set(attempt_client_ids))
    trade_identity_columns = (
        [str(trade["trade_id"]) for trade in run_trades],
        [str(trade["client_order_id"]) for trade in run_trades],
        [str(trade["exchange_order_id"]) for trade in run_trades],
    )
    duplicate_trade_count = max(
        len(identity_values) - len(set(identity_values))
        for identity_values in trade_identity_columns
    ) if trade_identity_columns else 0
    if final_state["actual_order_count"] != len(attempts):
        raise PhaseThirteenPublicTraceValidationError(
            "final actual order count does not match attempts"
        )
    if (
        final_state["duplicate_order_count"] != duplicate_order_count
        or final_state["duplicate_trade_count"] != duplicate_trade_count
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "final duplicate counts do not match normalized evidence"
        )

    performance = _require_exact_mapping(
        final_state["performance"],
        _PERFORMANCE_FIELDS,
        "performance",
    )
    for field_name in (
        "baseline_trade_count",
        "run_trade_count",
        "total_trade_count",
    ):
        _require_nonnegative_integer(performance[field_name], f"performance {field_name}")
    if (
        performance["baseline_trade_count"] != baseline_history_count
        or performance["run_trade_count"] != len(run_trades)
        or performance["total_trade_count"]
        != baseline_history_count + len(run_trades)
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "Performance trade counts do not match baseline and run evidence"
        )

    # Run 합계는 Trade에서 계산하고 total은 baseline+run을 precision 34로 결속한다.
    baseline_realized = _require_plain_decimal(
        performance["baseline_realized_profit_loss"],
        "performance baseline_realized_profit_loss",
    )
    run_realized = _require_plain_decimal(
        performance["run_realized_profit_loss"],
        "performance run_realized_profit_loss",
    )
    total_realized = _require_plain_decimal(
        performance["total_realized_profit_loss"],
        "performance total_realized_profit_loss",
    )
    baseline_fee = _require_plain_decimal(
        performance["baseline_fee_quote"],
        "performance baseline_fee_quote",
        minimum=Decimal("0"),
    )
    run_fee = _require_plain_decimal(
        performance["run_fee_quote"],
        "performance run_fee_quote",
        minimum=Decimal("0"),
    )
    total_fee = _require_plain_decimal(
        performance["total_fee_quote"],
        "performance total_fee_quote",
        minimum=Decimal("0"),
    )
    if any(
        decimal_value is None
        for decimal_value in (
            baseline_realized,
            run_realized,
            total_realized,
            baseline_fee,
            run_fee,
            total_fee,
        )
    ):
        raise AssertionError("required Performance value unexpectedly resolved to None")
    if baseline_history_count == 0 and (
        baseline_realized != Decimal("0")
        or baseline_fee != Decimal("0")
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "empty baseline history must have zero Performance totals"
        )
    calculated_run_realized = _sum_decimal128(
        tuple(
            Decimal(str(trade["realized_profit_loss"]))
            for trade in run_trades
            if trade["realized_profit_loss"] is not None
        )
    )
    calculated_run_fee = _sum_decimal128(
        tuple(Decimal(str(trade["fee_quote_amount"])) for trade in run_trades)
    )
    if run_realized != calculated_run_realized or run_fee != calculated_run_fee:
        raise PhaseThirteenPublicTraceValidationError(
            "Performance run totals do not match durable Trades"
        )
    if (
        total_realized != _sum_decimal128((baseline_realized, run_realized))
        or total_fee != _sum_decimal128((baseline_fee, run_fee))
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "Performance totals do not match baseline plus run"
        )

    return final_state  # Fresh restart와 durable history 산술을 같은 artifact에서 독립 검증한다.


def _derive_phase13_client_order_id(
    session_id: str,
    intent_id: str,
) -> str:
    """
    함수 이름: _derive_phase13_client_order_id()
    기능: production Controller와 같은 session·intent SHA-256로 최초 client order ID를 만든다.
    인자: session_id -> 실제 TradingSession UUID
        intent_id -> BUY 또는 force-sell 주문 의도 ID
    반환값: submission attempt 0의 canonical Binance client order ID
    작성 날짜: 2026/09/01
    """
    digest_input = f"{session_id}\x00{intent_id}".encode("utf-8")
    digest = hashlib.sha256(digest_input).hexdigest()[:24]

    return f"bat-{digest}-0"  # Phase 13은 intent별 최초 제출 한 번만 허용한다.


def _validate_v3_public_market_provenance(
    fingerprint: Mapping[str, object] | None,
    market_events: tuple[Mapping[str, object], ...],
) -> None:
    """
    함수 이름: _validate_v3_public_market_provenance()
    기능: 실제 Kline command identity와 V3 SUCCESS/NO_SIGNAL public boundary를 exact 결속한다.
    인자: fingerprint -> 성공 BUY decision fingerprint 또는 None
        market_events -> 검증된 public market event tuple
    반환값: 없음
    작성 날짜: 2026/09/01
    """
    if fingerprint is None:
        previous_market_version = 0
        previous_context_version = -1
        observed_source_event_ids: set[str] = set()
        for market_event in market_events:
            market_version = int(market_event["market_version"])
            context_version = int(market_event["context_version"])
            source_event_id = str(market_event["source_event_id"])
            if (
                market_event["message_id"] != "1L.1"
                or market_event["event_type"] != "KLINE_OBSERVED"
                or market_event["regime"] != "TYPE_0"
                or any(
                    market_event[field_name] is not None
                    for field_name in (
                        "evaluation_id",
                        "action_type",
                        "side",
                        "strategy",
                    )
                )
                or market_version <= previous_market_version
                or context_version < previous_context_version
                or source_event_id in observed_source_event_ids
            ):
                raise PhaseThirteenPublicTraceValidationError(
                    "v3 NO_SIGNAL market evidence is not an observed Kline snapshot"
                )

            # NO_SIGNAL에는 evaluation ID가 없으므로 production 형식을 재구성해 source를 parsing한다.
            reconstructed_command_id = (
                f"market:{market_version}:{market_event['source_event_id']}"
            )
            try:
                parsed_source = _parse_public_market_command_event(
                    reconstructed_command_id
                )
            except (TypeError, ValueError) as error:
                raise PhaseThirteenPublicTraceValidationError(
                    "v3 NO_SIGNAL market source is not a production Kline identity"
                ) from error
            if any(
                market_event[field_name] != parsed_source[field_name]
                for field_name in (
                    "source_event_id",
                    "source_kline_identity",
                    "source_event_time",
                    "market_version",
                )
            ):
                raise PhaseThirteenPublicTraceValidationError(
                    "v3 NO_SIGNAL market source fields disagree"
                )
            previous_market_version = market_version
            previous_context_version = context_version
            observed_source_event_ids.add(source_event_id)
        return  # Kline이 전혀 도착하지 않은 timeout도 실제 NO_SIGNAL로 보존한다.

    # SUCCESS evaluation은 실제 market:version:Kline source 문법과 네 파생 필드가 같아야 한다.
    try:
        parsed_source = _parse_public_market_command_event(
            str(fingerprint["evaluation_id"])
        )
    except (TypeError, ValueError) as error:
        raise PhaseThirteenPublicTraceValidationError(
            "v3 decision source is not a production Kline identity"
        ) from error
    if any(
        fingerprint[field_name] != parsed_source[field_name]
        for field_name in (
            "source_event_id",
            "source_kline_identity",
            "source_event_time",
            "market_version",
        )
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "v3 decision source fields disagree with its evaluation identity"
        )

    # Action producer는 30분 source, closed 1분 source 또는 canonical atomic batch에서만 BUY를 낸다.
    source_event_id = str(fingerprint["source_event_id"])
    if not source_event_id.startswith("kline-batch|"):
        source_match = _KLINE_SOURCE_PATTERN.fullmatch(source_event_id)
        if source_match is None:
            raise AssertionError("validated decision source lost its Kline shape")
        source_fields = source_match.groupdict()
        if not (
            source_fields["interval"] == "30m"
            or (
                source_fields["interval"] == "1m"
                and source_fields["close_state"] == "closed"
            )
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "v3 decision source cannot produce a public Case 2 Action"
            )


def _validate_v3_success_producer_contract(
    run_id: str,
    preflight: Mapping[str, object],
    fingerprint: Mapping[str, object],
    account_events: tuple[Mapping[str, object], ...],
    attempts: tuple[Mapping[str, object], ...],
    results: tuple[Mapping[str, object], ...],
    order_execution_traces: tuple[Mapping[str, object], ...],
    run_trades: tuple[Mapping[str, object], ...],
    ui_events: tuple[Mapping[str, object], ...],
    recovery: Mapping[str, object],
) -> None:
    """
    함수 이름: _validate_v3_success_producer_contract()
    기능: V3 성공 증거를 실제 Case 2 주문·회계·worker publication 순서와 exact 결속한다.
    인자: run_id -> STOP command provenance를 결속할 top-level run UUID
        preflight -> mutation 전 account snapshot
        fingerprint -> public CASE_C BUY decision
        account_events -> startup 및 stream account snapshot tuple
        attempts -> 실제 BUY/STOP SELL attempt tuple
        results -> fresh query의 exact terminal result tuple
        order_execution_traces -> 두 주문의 Communication trace tuple
        run_trades -> 이번 run durable Trade tuple
        ui_events -> Backend transport publication tuple
        recovery -> same-run recovery evidence
    반환값: 없음
    작성 날짜: 2026/09/01
    """
    if len(attempts) != 2 or len(results) != 2 or len(run_trades) != 2:
        raise PhaseThirteenPublicTraceValidationError(
            "v3 SUCCESS requires exactly two attempts, results, and Trades"
        )
    buy_attempt, sell_attempt = attempts
    buy_result, sell_result = results
    buy_trade, sell_trade = run_trades
    buy_trace, sell_trace = order_execution_traces

    # 모든 worker publication이 공유한 실제 TradingSession UUID가 주문 identity derivation의 유일한 seed다.
    session_events = tuple(
        event
        for event in ui_events
        if event["event_type"] == "TRADING_SESSION_UPDATED"
    )
    session_identities = {
        str(event["related_id"]) for event in session_events
    }
    if not session_events or len(session_identities) != 1:
        raise PhaseThirteenPublicTraceValidationError(
            "v3 SUCCESS lacks one consistent trading session publication"
        )
    trading_session_id = next(iter(session_identities))

    # Phase 13 actual permission은 TYPE_0, policy 13과 한 configured cap만 허용한다.
    expected_cap = fingerprint["configured_cap"]
    if (
        fingerprint["regime"] != "TYPE_0"
        or fingerprint["policy_version"] != 13
        or any(attempt["regime"] != "TYPE_0" for attempt in attempts)
        or any(attempt["policy_version"] != 13 for attempt in attempts)
        or any(attempt["configured_cap"] != expected_cap for attempt in attempts)
        or any(trade["regime"] != "TYPE_0" for trade in run_trades)
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "v3 SUCCESS changed the approved TYPE_0 policy or configured cap"
        )
    for client_order_id in (
        fingerprint["client_order_id"],
        *(attempt["client_order_id"] for attempt in attempts),
    ):
        if (
            not isinstance(client_order_id, str)
            or _PHASE13_CLIENT_ORDER_ID_PATTERN.fullmatch(client_order_id)
            is None
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "v3 SUCCESS client order ID is not production-derived"
            )

    # BUY는 public intent를, STOP SELL은 session 고유 force intent와 실제 stop command를 그대로 사용한다.
    expected_buy_client_order_id = _derive_phase13_client_order_id(
        trading_session_id,
        str(fingerprint["intent_id"]),
    )
    expected_sell_intent_id = f"force-sell:{trading_session_id}"
    expected_sell_client_order_id = _derive_phase13_client_order_id(
        trading_session_id,
        expected_sell_intent_id,
    )
    expected_stop_evaluation_id = (
        f"stop-{trading_session_id}-phase13-stop-{run_id}"
    )
    buy_trace_entries = tuple(buy_trace["entries"])
    sell_trace_entries = tuple(sell_trace["entries"])
    expected_buy_outcome_id = (
        f"order-outcome-{expected_buy_client_order_id}-"
        "CASE_C_POSITION_OPENED"
    )
    expected_sell_outcome_id = (
        f"order-outcome-{expected_sell_client_order_id}-"
        "FORCE_SELL_FINISHED"
    )
    if (
        buy_attempt["intent_id"] != fingerprint["intent_id"]
        or buy_attempt["evaluation_id"] != fingerprint["evaluation_id"]
        or fingerprint["client_order_id"] != expected_buy_client_order_id
        or buy_attempt["client_order_id"] != expected_buy_client_order_id
        or buy_trace["intent_id"] != fingerprint["intent_id"]
        or buy_trace["client_order_id"] != expected_buy_client_order_id
        or buy_trace_entries[0]["command_event_id"]
        != fingerprint["evaluation_id"]
        or buy_trace_entries[-1]["command_event_id"]
        != expected_buy_outcome_id
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "v3 SUCCESS BUY identity is not derived from its session and decision"
        )
    if int(buy_trace_entries[1]["context_version_after"]) != int(
        fingerprint["context_version"]
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "v3 SUCCESS BUY submit mutation does not match its public decision Context"
        )
    if (
        sell_attempt["intent_id"] != expected_sell_intent_id
        or sell_attempt["evaluation_id"] != expected_stop_evaluation_id
        or sell_attempt["client_order_id"] != expected_sell_client_order_id
        or sell_trace["intent_id"] != expected_sell_intent_id
        or sell_trace["client_order_id"] != expected_sell_client_order_id
        or sell_trace_entries[0]["command_event_id"]
        != expected_stop_evaluation_id
        or sell_trace_entries[-1]["command_event_id"]
        != expected_sell_outcome_id
        or recovery["intent_id"] != expected_sell_intent_id
        or recovery["client_order_id"] != expected_sell_client_order_id
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "v3 SUCCESS STOP identity is not derived from its session and run"
        )

    # Fresh query 결과와 durable Trade는 attempt의 BUY→SELL 순서를 그대로 보존해야 한다.
    for attempt, result, trade in zip(
        attempts,
        results,
        run_trades,
        strict=True,
    ):
        if (
            result["sequence"] != attempt["sequence"]
            or result["intent_id"] != attempt["intent_id"]
            or result["client_order_id"] != attempt["client_order_id"]
            or result["status"] != "FILLED"
            or result["failure_code"] is not None
            or trade["client_order_id"] != attempt["client_order_id"]
            or trade["side"] != attempt["side"]
            or trade["exchange_order_id"] != result["exchange_order_id"]
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "v3 SUCCESS result or Trade order differs from its attempt"
            )

    # Preflight/fingerprint account version과 recovery free 수량은 공개 snapshot에서 증명한다.
    required_account_version = max(
        int(preflight["account_version"]),
        int(fingerprint["account_version"]),
    )
    effective_free_quantity = Decimal(
        str(recovery["effective_free_quantity"])
    )
    if not any(
        int(account_event["account_version"]) >= required_account_version
        and Decimal(str(account_event["free_quantity"]))
        >= effective_free_quantity
        for account_event in account_events
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "v3 SUCCESS recovery free quantity lacks public account evidence"
        )

    # Dust-free MARKET BUY preflight는 result fill과 durable Trade에도 exact zero fee로 이어져야 한다.
    buy_fills = tuple(buy_result["incremental_fills"])
    if (
        any(
            Decimal(str(fill[fee_field])) != Decimal("0")
            for fill in buy_fills
            for fee_field in ("fee_amount", "fee_quote_amount")
        )
        or Decimal(str(buy_trade["fee_amount"])) != Decimal("0")
        or Decimal(str(buy_trade["fee_quote_amount"])) != Decimal("0")
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "v3 SUCCESS BUY fee contradicts its zero-commission preflight"
        )

    # Single BUY/full-close SELL의 Position 원가와 realized PnL을 domain Decimal128 공식으로 재계산한다.
    if (
        sell_trade["fee_asset"] == "ETH"
        and Decimal(str(sell_trade["fee_amount"])) > Decimal("0")
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "v3 SUCCESS cannot apply a positive base-asset SELL fee"
        )
    buy_fee_quote = Decimal(str(buy_trade["fee_quote_amount"]))
    buy_cost_basis = Decimal(str(buy_trade["executed_amount"]))
    if buy_trade["fee_asset"] != "ETH":
        buy_cost_basis = _sum_decimal128((buy_cost_basis, buy_fee_quote))
    buy_acquired_quantity = Decimal(str(buy_trade["executed_quantity"]))
    if buy_trade["fee_asset"] == "ETH":
        buy_acquired_quantity -= Decimal(str(buy_trade["fee_amount"]))
    if (
        buy_acquired_quantity <= Decimal("0")
        or Decimal(str(sell_trade["executed_quantity"]))
        != buy_acquired_quantity
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "v3 SUCCESS SELL quantity does not full-close its BUY acquisition"
        )
    expected_realized = _sum_decimal128(
        (
            Decimal(str(sell_trade["executed_amount"])),
            -Decimal(str(sell_trade["fee_quote_amount"])),
            -buy_cost_basis,
        )
    )
    if Decimal(str(sell_trade["realized_profit_loss"])) != expected_realized:
        raise PhaseThirteenPublicTraceValidationError(
            "v3 SUCCESS realized PnL does not match net proceeds and BUY cost"
        )

    # 각 atomic Trade pair 뒤 다음 Trade 전 worker session publication과 충분한 Context version을 요구한다.
    order_event_indices = tuple(
        event_index
        for event_index, event in enumerate(ui_events)
        if event["event_type"] == "ORDER_EXECUTED"
    )
    if tuple(
        ui_events[event_index]["related_id"]
        for event_index in order_event_indices
    ) != tuple(trade["trade_id"] for trade in run_trades):
        raise PhaseThirteenPublicTraceValidationError(
            "v3 SUCCESS Trade publications are outside durable order"
        )
    for trace_index, (order_event_index, trace_group) in enumerate(
        zip(order_event_indices, order_execution_traces, strict=True)
    ):
        next_order_index = (
            order_event_indices[trace_index + 1]
            if trace_index + 1 < len(order_event_indices)
            else len(ui_events)
        )
        following_session_events = tuple(
            event
            for event in ui_events[order_event_index + 2 : next_order_index]
            if event["event_type"] == "TRADING_SESSION_UPDATED"
        )
        maximum_trace_version = max(
            int(entry["context_version_after"])
            for entry in trace_group["entries"]
        )
        if (
            not following_session_events
            or int(following_session_events[0]["aggregate_version"])
            < maximum_trace_version
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "v3 SUCCESS Trade cycle lacks its authoritative session update"
            )


def _validate_cross_trace_contract(
    schema_version: int,
    run_id: str,
    outcome: str,
    typed_reason: str | None,
    started_at: datetime,
    completed_at: datetime,
    preflight: Mapping[str, object],
    fingerprint: Mapping[str, object] | None,
    market_events: tuple[Mapping[str, object], ...],
    account_events: tuple[Mapping[str, object], ...],
    attempts: tuple[Mapping[str, object], ...],
    submit_time_filter_evidence: tuple[Mapping[str, object], ...],
    results: tuple[Mapping[str, object], ...],
    order_execution_traces: tuple[Mapping[str, object], ...],
    baseline_history_count: int,
    run_trades: tuple[Mapping[str, object], ...],
    transport_session_id: str,
    ui_events: tuple[Mapping[str, object], ...],
    recovery: Mapping[str, object],
    final_state: Mapping[str, object],
) -> None:
    """
    함수 이름: _validate_cross_trace_contract()
    기능: local run clock과 Binance server/exchange clock을 분리해 전체 provenance와 각 축 인과를 결속한다.
    인자: schema_version -> top-level trace schema version
        run_id -> top-level run UUID
        outcome -> top-level normalized outcome
        typed_reason -> BLOCKED/FAILED의 safe typed reason 또는 None
        started_at -> run 시작 UTC 시각
        completed_at -> run 완료 UTC 시각
        preflight -> 검증된 mutation 전 Testnet 사실
        fingerprint -> immutable decision fingerprint 또는 None
        market_events -> 검증된 public market event tuple
        account_events -> 검증된 public account event tuple
        attempts -> 검증된 order attempt tuple
        submit_time_filter_evidence -> attempt별 production prepare-time rule 관찰 tuple
        results -> 검증된 order result tuple
        order_execution_traces -> 주문별 검증된 Communication trace tuple
        baseline_history_count -> 별도 검증한 baseline Trade 수
        run_trades -> 이번 run의 normalized durable Trade tuple
        transport_session_id -> 실제 BackendEventStream session UUID
        ui_events -> 검증된 transport/UI event tuple
        recovery -> 검증된 recovery mapping
        final_state -> 검증된 final state mapping
    반환값: 없음
    작성 날짜: 2026/09/01
    """
    # Outcome별 typed reason은 성공·신호 부재와 운영상 blocker/failure를 혼동하지 않게 고정한다.
    if outcome in {"SUCCESS", "NO_SIGNAL"} and typed_reason is not None:
        raise PhaseThirteenPublicTraceValidationError(
            "SUCCESS and NO_SIGNAL must not contain a typed reason"
        )
    if outcome in {"BLOCKED", "FAILED"} and typed_reason is None:
        raise PhaseThirteenPublicTraceValidationError(
            "BLOCKED and FAILED require a typed reason"
        )

    # Recovery와 fresh final state는 같은 Position·pending·open-order·duplicate 사실을 기록한다.
    recovery_final_quantity = Decimal(
        str(recovery["final_position_quantity"])
    )
    final_position_quantity = Decimal(str(final_state["position_quantity"]))
    if recovery_final_quantity != final_position_quantity:
        raise PhaseThirteenPublicTraceValidationError(
            "recovery and final Position do not match"
        )
    if recovery["pending_order_count"] != final_state["pending_order_count"]:
        raise PhaseThirteenPublicTraceValidationError(
            "recovery and final pending count do not match"
        )
    if (
        recovery["matching_open_order_count"]
        != final_state["matching_open_order_count"]
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "recovery and final open-order count do not match"
        )
    if (
        recovery["duplicate_order_count"] != final_state["duplicate_order_count"]
        or recovery["duplicate_trade_count"] != final_state["duplicate_trade_count"]
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "recovery and final duplicate counts do not match"
        )

    # V3 top-level account-wide state 관찰은 각 REST 반환 뒤 harness가 찍은 local 시각이다.
    preflight_account_state_timestamps: tuple[datetime, ...] = ()
    submit_time_account_state_timestamp_fields: tuple[str, ...] = ()
    if schema_version == 3:
        preflight_account_state_timestamps = (
            _require_utc_timestamp(
                preflight["account_open_orders_observed_at"],
                "preflight account open orders observed_at",
            ),
            _require_utc_timestamp(
                preflight["account_open_order_lists_observed_at"],
                "preflight account open order lists observed_at",
            ),
        )
        submit_time_account_state_timestamp_fields = (
            "account_open_orders_observed_at",
            "account_open_order_lists_observed_at",
        )

    # Local run window에는 harness가 직접 찍은 완료·account·transport 시각만 두고 server/exchange 축은 제외한다.
    local_causal_timestamps = [
        _require_utc_timestamp(preflight["verified_at"], "preflight verified_at"),
        _require_utc_timestamp(
            preflight["fresh_filters"]["observed_at"],
            "filter observed_at",
        ),
        _require_utc_timestamp(
            preflight["account_filters_observed_at"],
            "preflight account filters observed_at",
        ),
        _require_utc_timestamp(
            preflight["reference_price_observed_at"],
            "preflight reference price observed_at",
        ),
        *preflight_account_state_timestamps,
        *(
            _require_utc_timestamp(
                event["source_event_time"],
                "account source_event_time",
            )
            for event in account_events
        ),
        *(
            _require_utc_timestamp(event["published_at"], "UI published_at")
            for event in ui_events
        ),
        _require_utc_timestamp(final_state["verified_at"], "final verified_at"),
    ]
    if any(
        timestamp < started_at or timestamp > completed_at
        for timestamp in local_causal_timestamps
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "local trace evidence lies outside the run time window"
        )
    preflight_verified_at = _require_utc_timestamp(
        preflight["verified_at"],
        "preflight verified_at",
    )
    filter_observed_at = _require_utc_timestamp(
        preflight["fresh_filters"]["observed_at"],
        "filter observed_at",
    )
    account_filters_observed_at = _require_utc_timestamp(
        preflight["account_filters_observed_at"],
        "preflight account filters observed_at",
    )
    reference_price_observed_at = _require_utc_timestamp(
        preflight["reference_price_observed_at"],
        "preflight reference price observed_at",
    )
    # Top-level preflight 관찰은 local verified_at보다 늦을 수 없고 server attempt와는 직접 비교하지 않는다.
    if max(
        filter_observed_at,
        account_filters_observed_at,
        reference_price_observed_at,
        *preflight_account_state_timestamps,
    ) > preflight_verified_at:
        raise PhaseThirteenPublicTraceValidationError(
            "fresh safety observation follows preflight verification"
        )

    # V3 startup Account는 start_application 반환 직후에 생성되며 후속 행만 transport envelope를 복사한다.
    if schema_version == 3:
        account_ui_events = tuple(
            event
            for event in ui_events
            if event["event_type"] == "ACCOUNT_UPDATED"
        )
        if not account_events:
            raise PhaseThirteenPublicTraceValidationError(
                "v3 trace lacks its startup account snapshot"
            )
        startup_account_event = account_events[0]
        startup_account_time = _require_utc_timestamp(
            startup_account_event["source_event_time"],
            "startup account source_event_time",
        )
        first_preflight_observation = min(
            filter_observed_at,
            account_filters_observed_at,
            reference_price_observed_at,
            *preflight_account_state_timestamps,
        )
        if startup_account_time > first_preflight_observation:
            raise PhaseThirteenPublicTraceValidationError(
                "startup account snapshot is outside its production order"
            )
        if not any(
            account_event["account_version"] == preflight["account_version"]
            for account_event in account_events
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "v3 preflight account version lacks public snapshot evidence"
            )

        # Producer처럼 startup version 이하를 건너뛰고 이후 전진 ACCOUNT_UPDATED를 하나도 빠짐없이 복사한다.
        latest_captured_account_version = int(
            startup_account_event["account_version"]
        )
        expected_account_provenance: list[tuple[object, object, object]] = []
        for account_ui_event in account_ui_events:
            account_ui_version = int(account_ui_event["aggregate_version"])
            if account_ui_version <= latest_captured_account_version:
                continue
            expected_account_provenance.append(
                (
                    account_ui_event["event_id"],
                    account_ui_event["published_at"],
                    account_ui_event["aggregate_version"],
                )
            )
            latest_captured_account_version = account_ui_version
        actual_account_provenance = [
            (
                account_event["source_event_id"],
                account_event["source_event_time"],
                account_event["account_version"],
            )
            for account_event in account_events[1:]
        ]
        if actual_account_provenance != expected_account_provenance:
            raise PhaseThirteenPublicTraceValidationError(
                "account stream evidence is not the exact advancing transport sequence"
            )

    # Order/Performance publication은 preflight가 mutation 허용을 연 뒤에만 발행될 수 있다.
    if any(
        event["event_type"] in {"ORDER_EXECUTED", "PERFORMANCE_UPDATED"}
        and _require_utc_timestamp(
            event["published_at"],
            "order UI published_at",
        )
        < preflight_verified_at
        for event in ui_events
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "order publication precedes preflight verification"
        )
    attempts_by_client_id = {
        str(attempt["client_order_id"]): attempt for attempt in attempts
    }
    for result in results:
        attempted_at = _require_utc_timestamp(
            attempts_by_client_id[str(result["client_order_id"])]["attempted_at"],
            "matching order attempted_at",
        )
        if _require_utc_timestamp(
            result["observed_at"],
            "result observed_at",
        ) < attempted_at:
            raise PhaseThirteenPublicTraceValidationError(
                "order result precedes its attempt"
            )

    # Final fresh runtime은 local publication 뒤 검증되고 원 transport session을 재사용하지 않는다.
    final_verified_at = _require_utc_timestamp(
        final_state["verified_at"],
        "final verified_at",
    )
    local_publication_end_times = [
        preflight_verified_at,
        *(
            _require_utc_timestamp(
                event["source_event_time"],
                "account source_event_time",
            )
            for event in account_events
        ),
        *(
            _require_utc_timestamp(event["published_at"], "UI published_at")
            for event in ui_events
        ),
    ]
    if final_verified_at < max(local_publication_end_times):
        raise PhaseThirteenPublicTraceValidationError(
            "fresh final verification precedes local run evidence"
        )
    if final_state["fresh_runtime_session_id"] == transport_session_id:
        raise PhaseThirteenPublicTraceValidationError(
            "fresh runtime must use a distinct session identity"
        )

    if outcome == "SUCCESS":
        # Actual 성공은 정확히 public CASE_C BUY 한 건과 same-run STOP SELL 한 건만 허용한다.
        if (
            fingerprint is None
            or len(market_events) != 3
            or not account_events
            or len(attempts) != 2
            or len(run_trades) != 2
            or not ui_events
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "successful trace is missing required evidence"
            )
        buy_attempt, sell_attempt = attempts
        if (
            buy_attempt["side"] != "BUY"
            or buy_attempt["exit_reason"] is not None
            or sell_attempt["side"] != "SELL"
            or sell_attempt["exit_reason"] != "STOP"
            or buy_attempt["submission_attempt"] != 0
            or sell_attempt["submission_attempt"] != 0
            or buy_attempt["intent_id"] == sell_attempt["intent_id"]
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "successful trace requires one initial BUY and one initial STOP SELL"
            )
        latest_results_by_client_id: dict[str, Mapping[str, object]] = {}
        for result in results:
            latest_results_by_client_id[str(result["client_order_id"])] = result
        if set(latest_results_by_client_id) != set(attempts_by_client_id) or any(
            result["status"] != "FILLED"
            for result in latest_results_by_client_id.values()
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "all successful actual orders must terminate FILLED"
            )
        buy_result_time = _require_utc_timestamp(
            latest_results_by_client_id[str(buy_attempt["client_order_id"])][
                "observed_at"
            ],
            "BUY terminal observed_at",
        )
        sell_attempt_time = _require_utc_timestamp(
            sell_attempt["attempted_at"],
            "STOP SELL attempted_at",
        )
        if sell_attempt_time < buy_result_time:
            raise PhaseThirteenPublicTraceValidationError(
                "STOP SELL attempt precedes authoritative BUY terminal state"
            )

        # Position을 만든 BUY terminal 후에만 STOP SELL의 신규 composite safety fetch가 시작된다.
        sell_filter_evidence = tuple(
            evidence
            for evidence in submit_time_filter_evidence
            if evidence["client_order_id"] == sell_attempt["client_order_id"]
        )
        if len(sell_filter_evidence) != 1:
            raise PhaseThirteenPublicTraceValidationError(
                "STOP SELL lacks one submit-time safety observation"
            )
        selected_sell_filter_evidence = sell_filter_evidence[0]
        sell_safety_observations = [
            _require_utc_timestamp(
                selected_sell_filter_evidence["account_filters_observed_at"],
                "STOP SELL account filters observed_at",
            ),
            _require_utc_timestamp(
                selected_sell_filter_evidence["observed_at"],
                "STOP SELL filters observed_at",
            ),
        ]
        sell_safety_observations.extend(
            _require_utc_timestamp(
                selected_sell_filter_evidence[field_name],
                f"STOP SELL {field_name}",
            )
            for field_name in submit_time_account_state_timestamp_fields
        )
        sell_safety_observations.append(
            _require_utc_timestamp(
                selected_sell_filter_evidence["reference_price_observed_at"],
                "STOP SELL reference price observed_at",
            )
        )
        if min(sell_safety_observations) < buy_result_time:
            raise PhaseThirteenPublicTraceValidationError(
                "STOP SELL safety observation precedes authoritative BUY terminal state"
            )
        if (
            not recovery["required"]
            or not recovery["attempted"]
            or recovery["outcome"] != "SUCCESS"
            or recovery["intent_id"] != sell_attempt["intent_id"]
            or recovery["client_order_id"] != sell_attempt["client_order_id"]
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "successful recovery identity does not match STOP SELL"
            )
        sell_quantity = Decimal(str(sell_attempt["final_submitted_quantity"]))
        if not (
            sell_quantity
            == Decimal(str(buy_attempt["final_submitted_quantity"]))
            == Decimal(str(recovery["authoritative_position_quantity"]))
            == Decimal(str(recovery["effective_free_quantity"]))
            == Decimal(str(recovery["submitted_quantity"]))
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "STOP SELL quantity must equal authoritative and free exposure"
            )
        final_position_quantity = Decimal(str(final_state["position_quantity"]))
        if (
            final_position_quantity != Decimal("0")
            or final_state["pending_order_count"] != 0
            or final_state["unknown_order_count"] != 0
            or final_state["matching_open_order_count"] != 0
            or final_state["actual_order_count"] != 2
            or final_state["duplicate_order_count"] != 0
            or final_state["duplicate_trade_count"] != 0
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "successful trace must finish at exact zero exposure"
            )
    if outcome == "NO_SIGNAL":
        if fingerprint is not None or attempts or results or run_trades:
            raise PhaseThirteenPublicTraceValidationError(
                "NO_SIGNAL trace must not contain order evidence"
            )
    if outcome == "BLOCKED" and (attempts or results or run_trades):
        raise PhaseThirteenPublicTraceValidationError(
            "BLOCKED trace must not contain order mutation evidence"
        )
    has_trade_transport_events = any(
        event["event_type"] in {"ORDER_EXECUTED", "PERFORMANCE_UPDATED"}
        for event in ui_events
    )
    if outcome in {"NO_SIGNAL", "BLOCKED"} and (
        recovery["required"]
        or recovery["attempted"]
        or recovery["outcome"] != "NOT_REQUIRED"
        or Decimal(str(recovery["authoritative_position_quantity"]))
        != Decimal("0")
        or Decimal(str(recovery["effective_free_quantity"])) != Decimal("0")
        or Decimal(str(recovery["final_position_quantity"])) != Decimal("0")
        or final_position_quantity != Decimal("0")
        or final_state["pending_order_count"] != 0
        or final_state["unknown_order_count"] != 0
        or final_state["matching_open_order_count"] != 0
        or has_trade_transport_events
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "non-mutating outcome must preserve exact zero state"
        )
    if outcome == "FAILED" and attempts:
        # 모든 mutation이 durable FILLED·recovery SUCCESS·zero-state면 FAILED로 축소할 수 없다.
        latest_results_for_failure: dict[str, Mapping[str, object]] = {}
        for result in results:
            if result["status"] != "UNKNOWN":
                latest_results_for_failure[str(result["client_order_id"])] = result
        all_attempts_filled = set(latest_results_for_failure) == set(
            attempts_by_client_id
        ) and all(
            result["status"] == "FILLED"
            for result in latest_results_for_failure.values()
        )
        if (
            all_attempts_filled
            and len(run_trades) == len(attempts)
            and recovery["outcome"] == "SUCCESS"
            and final_position_quantity == Decimal("0")
            and final_state["pending_order_count"] == 0
            and final_state["unknown_order_count"] == 0
            and final_state["matching_open_order_count"] == 0
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "completed successful mutation cannot use FAILED outcome"
            )
    if fingerprint is None and (
        attempts or results or run_trades or has_trade_transport_events
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "order evidence requires an immutable decision fingerprint"
        )

    if fingerprint is None:
        if schema_version == 3:
            _validate_v3_public_market_provenance(None, market_events)
        return  # FAILED/BLOCKED/NO_SIGNAL은 decision 이전에도 종료될 수 있다.

    if schema_version == 3:
        _validate_v3_public_market_provenance(fingerprint, market_events)

    # Action을 낸 모든 outcome은 exact 1L.1→1L.2→1L.3와 마지막 CASE_C BUY를 사용한다.
    if (
        tuple(event["message_id"] for event in market_events)
        != ("1L.1", "1L.2", "1L.3")
        or tuple(event["event_type"] for event in market_events)
        != ("KLINE_OBSERVED", "MARKET_EVALUATED", "ACTION_EMITTED")
        or any(
            event[field_name] != fingerprint[field_name]
            for event in market_events
            for field_name in (
                "source_event_id",
                "source_kline_identity",
                "source_event_time",
                "market_version",
                "evaluation_id",
                "regime",
            )
        )
        or tuple(event["context_version"] for event in market_events)
        != tuple(
            sorted(event["context_version"] for event in market_events)
        )
        or market_events[2]["context_version"]
        != fingerprint["context_version"]
        or any(
            event[field_name] is not None
            for event in market_events[:2]
            for field_name in ("action_type", "side", "strategy")
        )
        or market_events[2]["action_type"] != "SUBMIT_ORDER"
        or market_events[2]["side"] != "BUY"
        or market_events[2]["strategy"] != "CASE_C"
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "trace does not contain the exact observed public 1L path"
        )
    if not any(
        event["account_version"] == fingerprint["account_version"]
        for event in account_events
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "decision fingerprint has no matching public account event"
        )
    if preflight["account_version"] != fingerprint["account_version"]:
        raise PhaseThirteenPublicTraceValidationError(
            "decision account version does not match preflight"
        )
    buy_filter_evidence = next(
        (
            evidence
            for evidence in submit_time_filter_evidence
            if evidence["client_order_id"] == fingerprint["client_order_id"]
        ),
        None,
    )
    if buy_filter_evidence is not None:
        source_event_time = _require_utc_timestamp(
            fingerprint["source_event_time"],
            "decision source_event_time",
        )
        buy_safety_observations = (
            _require_utc_timestamp(
                buy_filter_evidence["observed_at"],
                "BUY submit-time filter observed_at",
            ),
            _require_utc_timestamp(
                buy_filter_evidence["account_filters_observed_at"],
                "BUY submit-time account filters observed_at",
            ),
            _require_utc_timestamp(
                buy_filter_evidence["reference_price_observed_at"],
                "BUY submit-time reference price observed_at",
            ),
            *(
                _require_utc_timestamp(
                    buy_filter_evidence[field_name],
                    f"BUY submit-time {field_name}",
                )
                for field_name in submit_time_account_state_timestamp_fields
            ),
        )
        if any(
            observation_time < source_event_time
            for observation_time in buy_safety_observations
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "BUY submit-time safety observation precedes its public source event"
            )

    # 신규 BUY attempt가 immutable evaluation, Action type과 cap claim을 바꾸지 않았는지 비교한다.
    matching_attempt = any(
        attempt["intent_id"] == fingerprint["intent_id"]
        and attempt["client_order_id"] == fingerprint["client_order_id"]
        and attempt["evaluation_id"] == fingerprint["evaluation_id"]
        and attempt["policy_version"] == fingerprint["policy_version"]
        and attempt["symbol"] == "ETHUSDT"
        and attempt["regime"] == fingerprint["regime"]
        and attempt["strategy"] == fingerprint["strategy"]
        and attempt["action_type"] == fingerprint["action_type"]
        and attempt["side"] == "BUY"
        and attempt["decision_price"] == fingerprint["decision_price"]
        and attempt["final_submitted_quantity"]
        == fingerprint["final_submitted_quantity"]
        and attempt["final_notional"] == fingerprint["final_notional"]
        and attempt["configured_cap"] == fingerprint["configured_cap"]
        for attempt in attempts
    )
    if attempts and not matching_attempt:
        raise PhaseThirteenPublicTraceValidationError(
            "decision fingerprint has no matching BUY attempt"
        )

    # 각 terminal FILLED order는 정확히 한 durable Trade와 전체 incremental fill aggregate를 가진다.
    trades_by_client_id: dict[str, list[Mapping[str, object]]] = {}
    for trade in run_trades:
        trades_by_client_id.setdefault(str(trade["client_order_id"]), []).append(trade)
    results_by_client_id: dict[str, list[Mapping[str, object]]] = {}
    for result in results:
        results_by_client_id.setdefault(str(result["client_order_id"]), []).append(result)
    for client_order_id, attempt in attempts_by_client_id.items():
        client_results = results_by_client_id.get(client_order_id, [])
        latest_concrete_result = next(
            (
                result
                for result in reversed(client_results)
                if result["status"] != "UNKNOWN"
            ),
            None,
        )
        client_trades = trades_by_client_id.get(client_order_id, [])
        fills = [
            fill
            for result in client_results
            for fill in result["incremental_fills"]
        ]
        has_terminal_fills = (
            latest_concrete_result is not None
            and latest_concrete_result["status"]
            in _TERMINAL_ORDER_RESULT_STATUSES
            and bool(fills)
        )
        if not has_terminal_fills:
            if client_trades:
                raise PhaseThirteenPublicTraceValidationError(
                    "order without terminal fills must not own a durable Trade"
                )
            continue
        if len(client_trades) != 1:
            raise PhaseThirteenPublicTraceValidationError(
                "each terminal fill-bearing order requires exactly one durable Trade"
            )
        trade = client_trades[0]
        if (
            trade["exchange_order_id"] != latest_concrete_result["exchange_order_id"]
            or trade["symbol"] != attempt["symbol"]
            or trade["side"] != attempt["side"]
            or trade["regime"] != attempt["regime"]
            or trade["strategy"] != attempt["strategy"]
            or trade["exit_reason"] != attempt["exit_reason"]
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "durable Trade changed terminal order provenance"
            )
        fill_quantities = tuple(Decimal(str(fill["quantity"])) for fill in fills)
        fill_quote_amounts = tuple(
            Decimal(str(fill["quote_amount"])) for fill in fills
        )
        fill_fee_amounts = tuple(Decimal(str(fill["fee_amount"])) for fill in fills)
        fill_fee_quote_amounts = tuple(
            Decimal(str(fill["fee_quote_amount"])) for fill in fills
        )
        latest_fill_time = max(
            _require_utc_timestamp(fill["event_time"], "fill event_time")
            for fill in fills
        )
        if (
            any(fill["durable_trade_id"] != trade["trade_id"] for fill in fills)
            or any(fill["fee_asset"] != trade["fee_asset"] for fill in fills)
            or _sum_decimal128(fill_quantities)
            != Decimal(str(trade["executed_quantity"]))
            or _sum_decimal128(fill_quote_amounts)
            != Decimal(str(trade["executed_amount"]))
            or _sum_decimal128(fill_fee_amounts)
            != Decimal(str(trade["fee_amount"]))
            or _sum_decimal128(fill_fee_quote_amounts)
            != Decimal(str(trade["fee_quote_amount"]))
            or _require_utc_timestamp(trade["executed_at"], "Trade executed_at")
            != latest_fill_time
        ):
            raise PhaseThirteenPublicTraceValidationError(
                "durable Trade does not match its fill aggregate"
            )

    # Run Trade마다 실제 ORDER/PERFORMANCE pair가 정확히 하나 있고 baseline은 별도 count로만 남는다.
    if baseline_history_count < 0:
        raise AssertionError("validated baseline history count became negative")
    order_event_trade_ids = [
        str(event["related_id"])
        for event in ui_events
        if event["event_type"] == "ORDER_EXECUTED"
    ]
    run_trade_ids = [str(trade["trade_id"]) for trade in run_trades]
    if sorted(order_event_trade_ids) != sorted(run_trade_ids):
        raise PhaseThirteenPublicTraceValidationError(
            "transport Trade publications do not match run durable Trades"
        )
    trades_by_id = {
        str(trade["trade_id"]): trade
        for trade in run_trades
    }
    # UI publication은 local run clock, Trade execution은 exchange clock이므로 identity만 결속한다.
    if any(
        str(event["related_id"]) not in trades_by_id
        for event in ui_events
        if event["event_type"] == "ORDER_EXECUTED"
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "ORDER_EXECUTED publication lacks its durable Trade"
        )
    if schema_version == 3 and outcome == "SUCCESS":
        _validate_v3_success_producer_contract(
            run_id,
            preflight,
            fingerprint,
            account_events,
            attempts,
            results,
            order_execution_traces,
            run_trades,
            ui_events,
            recovery,
        )


def _validate_trace_body(
    trace_body: object,
    forbidden_values: tuple[str, ...],
) -> Mapping[str, object]:
    """
    함수 이름: _validate_trace_body()
    기능: digest를 제외한 Phase 13 trace body의 exact schema와 전체 provenance를 검증한다.
    인자: trace_body -> 검증할 trace body JSON object
        forbidden_values -> 포함할 수 없는 실제 secret 원문 tuple
    반환값: 검증된 trace body mapping
    작성 날짜: 2026/08/31
    """
    _reject_secret_material(trace_body, forbidden_values)
    trace = _require_exact_mapping(trace_body, _TRACE_BODY_FIELDS, "trace body")
    schema_version = _require_nonnegative_integer(
        trace["schema_version"],
        "trace schema version",
    )
    if schema_version not in _SUPPORTED_PHASE13_PUBLIC_TRACE_SCHEMA_VERSIONS:
        raise PhaseThirteenPublicTraceValidationError(
            "trace schema version is unsupported"
        )
    if trace["record_type"] != PHASE13_PUBLIC_TRACE_RECORD_TYPE:
        raise PhaseThirteenPublicTraceValidationError(
            "trace record type is unsupported"
        )
    outcome = _require_identifier(trace["outcome"], "trace outcome")
    if outcome not in PHASE13_PUBLIC_TRACE_OUTCOMES:
        raise PhaseThirteenPublicTraceValidationError(
            "trace outcome is unsupported"
        )
    if schema_version == 3 and outcome not in {"SUCCESS", "NO_SIGNAL"}:
        raise PhaseThirteenPublicTraceValidationError(
            "v3 full trace supports only SUCCESS or NO_SIGNAL"
        )
    typed_reason = _require_identifier(
        trace["typed_reason"],
        "trace typed_reason",
        allow_none=True,
    )

    # UUIDv4 run identity와 bounded run timestamps는 artifact끼리의 accidental 합성을 막는다.
    run_id = _require_uuid4(trace["run_id"], "trace run_id")
    started_at, completed_at = _validate_timestamps(trace["timestamps"])
    preflight = _validate_preflight(
        trace["preflight"],
        schema_version=schema_version,
    )
    baseline_history_sha256 = trace["baseline_history_sha256"]
    if not isinstance(
        baseline_history_sha256,
        str,
    ) or _SHA256_PATTERN.fullmatch(baseline_history_sha256) is None:
        raise PhaseThirteenPublicTraceValidationError(
            "baseline history digest must be lowercase SHA-256"
        )
    baseline_history_count = _require_nonnegative_integer(
        trace["baseline_history_count"],
        "baseline history count",
    )
    empty_history_digest = hashlib.sha256(b"").hexdigest()
    if baseline_history_count == 0 and baseline_history_sha256 != empty_history_digest:
        raise PhaseThirteenPublicTraceValidationError(
            "empty baseline history must use the empty-file digest"
        )
    if baseline_history_count > 0 and baseline_history_sha256 == empty_history_digest:
        raise PhaseThirteenPublicTraceValidationError(
            "non-empty baseline history must not use the empty-file digest"
        )

    # 각 sequence를 독립 검증한 뒤 source, order, durable state 관계를 마지막에 결속한다.
    fingerprint = _validate_decision_fingerprint(
        trace["immutable_decision_fingerprint"]
    )
    market_events = _validate_market_events(trace["public_market_events"])
    account_events = _validate_account_events(
        trace["public_account_events"],
        schema_version=schema_version,
        run_id=run_id,
    )
    attempts = _validate_order_attempts(trace["order_attempts"])
    submit_time_filter_evidence = _validate_submit_time_filter_evidence(
        trace["submit_time_filter_evidence"],
        attempts,
        schema_version=schema_version,
    )
    results = _validate_order_results(
        trace["order_results"],
        attempts,
        schema_version=schema_version,
    )
    order_execution_traces = _validate_order_execution_traces(
        trace["order_execution_traces"],
        attempts,
        results,
        require_complete_success=(outcome == "SUCCESS"),
    )
    run_trades = _validate_run_durable_trades(trace["run_durable_trades"])
    transport_session_id, ui_events = _validate_transport_ui_events(
        trace["transport_ui_event_batch"],
        schema_version=schema_version,
    )
    recovery = _validate_recovery(trace["recovery"])
    final_state = _validate_final_state(
        trace["final_state"],
        baseline_history_count=baseline_history_count,
        attempts=attempts,
        run_trades=run_trades,
    )
    _validate_cross_trace_contract(
        schema_version,
        run_id,
        str(outcome),
        typed_reason,
        started_at,
        completed_at,
        preflight,
        fingerprint,
        market_events,
        account_events,
        attempts,
        submit_time_filter_evidence,
        results,
        order_execution_traces,
        baseline_history_count,
        run_trades,
        transport_session_id,
        ui_events,
        recovery,
        final_state,
    )

    return trace  # 검증 전 입력을 수정하지 않고 canonical digest 계산에 그대로 사용한다.


def _canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    """
    함수 이름: _canonical_json_bytes()
    기능: sorted key, compact separator, UTF-8와 trailing newline으로 byte-stable JSON을 만든다.
    인자: value -> canonical encoding할 JSON object
    반환값: 마지막 newline을 포함한 UTF-8 bytes
    작성 날짜: 2026/08/31
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
        raise PhaseThirteenPublicTraceValidationError(
            "trace is not canonical JSON"
        ) from error

    return f"{encoded_text}\n".encode("utf-8")  # Digest와 artifact writer가 같은 newline framing을 사용한다.


def _calculate_trace_body_digest(trace_body: Mapping[str, object]) -> str:
    """
    함수 이름: _calculate_trace_body_digest()
    기능: 검증된 trace body canonical bytes의 lowercase SHA-256를 계산한다.
    인자: trace_body -> digest field를 제외한 exact trace body
    반환값: lowercase SHA-256 문자열
    작성 날짜: 2026/08/31
    """
    return hashlib.sha256(_canonical_json_bytes(trace_body)).hexdigest()  # Body 전체와 newline을 digest에 결합한다.


def seal_phase13_public_trace(
    trace_body: Mapping[str, object],
    *,
    forbidden_values: Sequence[str] = (),
) -> dict[str, object]:
    """
    함수 이름: seal_phase13_public_trace()
    기능: secret-free exact trace body를 검증하고 canonical SHA-256를 붙인 독립 사본을 반환한다.
    인자: trace_body -> trace_sha256를 제외한 Phase 13 trace body
        forbidden_values -> API key·secret·local token 등 실제 canary 원문 모음
    반환값: trace_sha256를 포함한 검증 완료 trace document
    작성 날짜: 2026/08/31
    """
    normalized_forbidden_values = _normalize_forbidden_values(forbidden_values)
    validated_body = _validate_trace_body(
        trace_body,
        normalized_forbidden_values,
    )

    # 검증된 JSON-native tree만 deepcopy한 뒤 digest를 추가해 caller의 mutable 수집 buffer와 분리한다.
    sealed_trace = deepcopy(dict(validated_body))
    sealed_trace["trace_sha256"] = _calculate_trace_body_digest(validated_body)

    return sealed_trace  # 실제 파일 쓰기는 validate/canonical bytes 성공 뒤에만 수행할 수 있다.


def validate_phase13_public_trace(
    trace_document: Mapping[str, object],
    *,
    forbidden_values: Sequence[str] = (),
) -> str:
    """
    함수 이름: validate_phase13_public_trace()
    기능: sealed trace의 redaction, exact schema, provenance와 canonical SHA-256를 fail-closed로 검증한다.
    인자: trace_document -> trace_sha256를 포함한 Phase 13 trace document
        forbidden_values -> API key·secret·local token 등 실제 canary 원문 모음
    반환값: 검증된 lowercase SHA-256 문자열
    작성 날짜: 2026/08/31
    """
    normalized_forbidden_values = _normalize_forbidden_values(forbidden_values)
    _reject_secret_material(trace_document, normalized_forbidden_values)
    document = _require_exact_mapping(
        trace_document,
        _TRACE_DOCUMENT_FIELDS,
        "trace document",
    )
    recorded_digest = document["trace_sha256"]
    if not isinstance(recorded_digest, str) or _SHA256_PATTERN.fullmatch(
        recorded_digest
    ) is None:
        raise PhaseThirteenPublicTraceValidationError(
            "trace digest must be lowercase SHA-256"
        )

    # Digest field를 제외한 새 mapping을 exact body validator와 동일 canonical encoder에 전달한다.
    trace_body = {
        field_name: document[field_name]
        for field_name in _TRACE_BODY_FIELDS
    }
    validated_body = _validate_trace_body(
        trace_body,
        normalized_forbidden_values,
    )
    calculated_digest = _calculate_trace_body_digest(validated_body)
    if calculated_digest != recorded_digest:
        raise PhaseThirteenPublicTraceValidationError(
            "trace digest does not match canonical body"
        )

    return recorded_digest  # Caller는 이 값만 artifact index에 기록하고 raw trace를 로그하지 않는다.


def canonical_phase13_public_trace_bytes(
    trace_document: Mapping[str, object],
    *,
    forbidden_values: Sequence[str] = (),
) -> bytes:
    """
    함수 이름: canonical_phase13_public_trace_bytes()
    기능: sealed trace를 완전히 검증한 뒤 artifact에 쓸 canonical UTF-8 JSON bytes를 반환한다.
    인자: trace_document -> trace_sha256를 포함한 Phase 13 trace document
        forbidden_values -> API key·secret·local token 등 실제 canary 원문 모음
    반환값: 마지막 newline을 포함한 canonical UTF-8 JSON bytes
    작성 날짜: 2026/08/31
    """
    validate_phase13_public_trace(
        trace_document,
        forbidden_values=forbidden_values,
    )

    return _canonical_json_bytes(trace_document)  # 검증 실패 시 bytes를 만들지 않아 stdout·파일 write를 차단한다.


def _require_actual_forbidden_values(
    forbidden_values: Sequence[str],
) -> tuple[str, ...]:
    """
    함수 이름: _require_actual_forbidden_values()
    기능: actual artifact API가 최소 한 개의 실제 credential/token canary를 반드시 받게 한다.
    인자: forbidden_values -> artifact 어디에도 포함하면 안 되는 실제 secret 원문 모음
    반환값: 비어 있지 않은 normalized canary tuple
    작성 날짜: 2026/08/31
    """
    normalized_values = _normalize_forbidden_values(forbidden_values)
    if not normalized_values:
        raise PhaseThirteenPublicTraceValidationError(
            "actual trace validation requires forbidden values"
        )

    return normalized_values  # 값 자체는 오류·stdout·artifact에 반환하지 않는다.


def seal_actual_phase13_public_trace(
    trace_body: Mapping[str, object],
    *,
    forbidden_values: Sequence[str],
) -> dict[str, object]:
    """
    함수 이름: seal_actual_phase13_public_trace()
    기능: non-empty redaction canary를 강제해 actual Testnet trace body를 검증·seal한다.
    인자: trace_body -> trace_sha256를 제외한 actual Phase 13 trace body
        forbidden_values -> API key·secret·local token 등 실제 canary 원문 모음
    반환값: trace_sha256를 포함한 검증 완료 actual trace document
    작성 날짜: 2026/08/31
    """
    normalized_values = _require_actual_forbidden_values(forbidden_values)

    return seal_phase13_public_trace(
        trace_body,
        forbidden_values=normalized_values,
    )  # Unit용 permissive API와 달리 빈 redaction 검증으로 artifact를 만들 수 없다.


def validate_actual_phase13_public_trace(
    trace_document: Mapping[str, object],
    *,
    forbidden_values: Sequence[str],
) -> str:
    """
    함수 이름: validate_actual_phase13_public_trace()
    기능: non-empty redaction canary와 canonical digest를 actual sealed trace에 재검증한다.
    인자: trace_document -> trace_sha256를 포함한 actual trace document
        forbidden_values -> API key·secret·local token 등 실제 canary 원문 모음
    반환값: 검증된 lowercase SHA-256 문자열
    작성 날짜: 2026/08/31
    """
    normalized_values = _require_actual_forbidden_values(forbidden_values)

    return validate_phase13_public_trace(
        trace_document,
        forbidden_values=normalized_values,
    )  # Digest index만 반환하고 credential canary는 외부로 전달하지 않는다.


def canonical_actual_phase13_public_trace_bytes(
    trace_document: Mapping[str, object],
    *,
    forbidden_values: Sequence[str],
) -> bytes:
    """
    함수 이름: canonical_actual_phase13_public_trace_bytes()
    기능: actual 전용 redaction·schema·digest 검증 뒤에만 canonical artifact bytes를 반환한다.
    인자: trace_document -> trace_sha256를 포함한 actual trace document
        forbidden_values -> API key·secret·local token 등 실제 canary 원문 모음
    반환값: 마지막 newline을 포함한 canonical UTF-8 JSON bytes
    작성 날짜: 2026/08/31
    """
    normalized_values = _require_actual_forbidden_values(forbidden_values)
    validate_actual_phase13_public_trace(
        trace_document,
        forbidden_values=normalized_values,
    )

    return _canonical_json_bytes(trace_document)  # Artifact writer는 이 bytes 외 raw trace를 기록하지 않는다.
