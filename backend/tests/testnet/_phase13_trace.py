"""Phase 13 public Case 2 actual Testnet trace를 secret 없이 canonicalize한다."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import hashlib
import json
import re
from uuid import UUID


# Actual evidence는 한 schema와 byte framing만 사용해 확장·축약을 자동 허용하지 않는다.
PHASE13_PUBLIC_TRACE_SCHEMA_VERSION = 1
PHASE13_PUBLIC_TRACE_RECORD_TYPE = "phase13_public_case2_testnet_trace"
PHASE13_PUBLIC_TRACE_OUTCOMES = frozenset(
    {"SUCCESS", "NO_SIGNAL", "BLOCKED", "FAILED"}
)
PHASE13_PUBLIC_TRACE_ABSOLUTE_MAX_NOTIONAL = Decimal("100")
_MAXIMUM_EVENT_COUNT = 2048
_MAXIMUM_TEXT_LENGTH = 512
_MAXIMUM_INTEGER = 9_223_372_036_854_775_807
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
_PREFLIGHT_FIELDS = frozenset(
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
        "position_quantity",
        "pending_order_count",
        "unknown_order_count",
        "matching_open_order_count",
    }
)
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
    }
)
_FRESH_FILTER_FIELDS = _FRESH_FILTER_RULE_FIELDS | {"observed_at"}
_SUBMIT_TIME_FILTER_EVIDENCE_FIELDS = frozenset(
    {
        "sequence",
        "intent_id",
        "client_order_id",
        "side",
        "observed_at",
        "rules",
    }
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
    if (
        minimum_notional is not None
        and maximum_notional is not None
        and maximum_notional < minimum_notional
    ):
        raise PhaseThirteenPublicTraceValidationError(
            f"{location} notional filter range is invalid"
        )

    return None  # 공통 rule 검증은 caller의 preflight/submit-time timestamp 의미를 바꾸지 않는다.


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

    return filters  # Raw exchangeInfo와 endpoint query는 trace schema에 포함하지 않는다.


def _validate_order_attempt_against_fresh_filters(
    attempt: Mapping[str, object],
    filters: Mapping[str, object],
) -> None:
    """
    함수 이름: _validate_order_attempt_against_fresh_filters()
    기능: filter 뒤 최종 제출 수량·notional이 기록된 fresh exchangeInfo grid와 범위를 만족하는지 검증한다.
    인자: attempt -> 검증된 order attempt mapping
        filters -> 검증된 fresh_filters mapping
    반환값: 없음
    작성 날짜: 2026/08/31
    """
    submitted_quantity = Decimal(str(attempt["final_submitted_quantity"]))
    final_notional = Decimal(str(attempt["final_notional"]))
    base_asset_precision = int(filters["base_asset_precision"])
    precision_step = Decimal("1").scaleb(-base_asset_precision)

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
    if minimum_notional is not None and final_notional < Decimal(
        str(minimum_notional)
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "final notional is below the fresh filter minimum"
        )
    if maximum_notional is not None and final_notional > Decimal(
        str(maximum_notional)
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "final notional exceeds the fresh filter maximum"
        )

    return None  # Filter snapshot과 제출 claim을 검증만 하고 caller evidence는 변경하지 않는다.


def _validate_preflight(value: object) -> Mapping[str, object]:
    """
    함수 이름: _validate_preflight()
    기능: fixed Spot Testnet endpoint, readiness와 mutation 전 exact zero-state를 검증한다.
    인자: value -> preflight JSON object
    반환값: 검증된 preflight mapping
    작성 날짜: 2026/08/31
    """
    preflight = _require_exact_mapping(value, _PREFLIGHT_FIELDS, "preflight")
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
    _validate_fresh_filters(preflight["fresh_filters"])

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
) -> tuple[Mapping[str, object], ...]:
    """
    함수 이름: _validate_account_events()
    기능: public account snapshot·stream event의 exact schema와 잔액 Decimal을 검증한다.
    인자: value -> public account event JSON array
    반환값: 검증된 account event tuple
    작성 날짜: 2026/08/31
    """
    entries = _require_contiguous_sequence(
        _require_list(value, "public account events"),
        _ACCOUNT_EVENT_FIELDS,
        "public account events",
    )
    for entry in entries:
        _require_canonical_text(entry["message_id"], "account message_id")
        _require_identifier(entry["event_type"], "account event_type")
        _require_canonical_text(entry["source_event_id"], "account source_event_id")
        _require_utc_timestamp(entry["source_event_time"], "account source_event_time")
        _require_nonnegative_integer(entry["account_version"], "account version")
        _require_identifier(entry["asset"], "account asset")
        for field_name in ("free_quantity", "locked_quantity"):
            _require_plain_decimal(
                entry[field_name],
                f"account {field_name}",
                minimum=Decimal("0"),
            )

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
    기능: BUY/SELL 고정 경계와 bounded same-ID query·partial fill branch의 complete 성공 grammar를 검증한다.
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
        if query_count > 4:
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
            continue
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

            # Exchange ID가 한 번 관찰된 뒤에는 후속 query·fill·durable trace가 None으로 후퇴할 수 없다.
            if order_id is None:
                if exchange_identity_observed:
                    raise PhaseThirteenPublicTraceValidationError(
                        "order execution trace exchange identity regressed to None"
                    )
            else:
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
            if trace_entries[0]["command_event_id"] != attempt["evaluation_id"]:
                raise PhaseThirteenPublicTraceValidationError(
                    "order trace source event does not match attempt evaluation"
                )

    return trace_groups  # Canonical group와 entry 순서는 digest에 기록된 그대로 유지한다.


def _validate_submit_time_filter_evidence(
    value: object,
    attempts: tuple[Mapping[str, object], ...],
) -> tuple[Mapping[str, object], ...]:
    """
    함수 이름: _validate_submit_time_filter_evidence()
    기능: production prepare-time filter 관찰을 matching attempt와 1:1 결속하고 최종 제출 산술을 검증한다.
    인자: value -> submit_time_filter_evidence JSON array
        attempts -> 먼저 검증한 order attempt tuple
    반환값: 검증된 submit-time filter evidence tuple
    작성 날짜: 2026/08/31
    """
    entries = _require_contiguous_sequence(
        _require_list(value, "submit-time filter evidence"),
        _SUBMIT_TIME_FILTER_EVIDENCE_FIELDS,
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
        if previous_observed_at is not None and observed_at < previous_observed_at:
            raise PhaseThirteenPublicTraceValidationError(
                "submit-time filter observation times must be monotonic"
            )
        previous_observed_at = observed_at
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
        if observed_at > attempted_at:
            raise PhaseThirteenPublicTraceValidationError(
                "submit-time filter observation follows POST submission start"
            )

        # Rules는 preflight snapshot과 별개 exact object이며 matching final quantity에만 권위를 갖는다.
        rules = _require_exact_mapping(
            entry["rules"],
            _FRESH_FILTER_RULE_FIELDS,
            "submit-time filter rules",
        )
        _validate_fresh_filter_rule_values(rules, "submit-time filter rules")
        _validate_order_attempt_against_fresh_filters(matching_attempt, rules)

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
    _require_canonical_text(fill["fill_id"], "fill fill_id")
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
    _require_plain_decimal(
        fill["fee_quote_amount"],
        "fill fee_quote_amount",
        minimum=Decimal("0"),
    )
    if price is None or quantity is None or quote_amount is None:
        raise AssertionError("required fill decimal unexpectedly resolved to None")
    if _multiply_decimal128(price, quantity) != quote_amount:
        raise PhaseThirteenPublicTraceValidationError(
            "fill quote amount does not match price and quantity"
        )
    fee_asset = _require_identifier(
        fill["fee_asset"],
        "fill fee_asset",
        allow_none=True,
    )
    if fee_amount is not None and fee_amount > Decimal("0") and fee_asset is None:
        raise PhaseThirteenPublicTraceValidationError(
            "positive fill fee requires an asset"
        )

    return fill  # Raw execution payload 없이 accounting에 필요한 normalized 사실만 반환한다.


def _validate_order_results(
    value: object,
    attempts: tuple[Mapping[str, object], ...],
) -> tuple[Mapping[str, object], ...]:
    """
    함수 이름: _validate_order_results()
    기능: terminal-monotonic result, global exchange identity와 final fill aggregate를 검증한다.
    인자: value -> order result JSON array
        attempts -> 먼저 검증한 order attempt tuple
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
) -> tuple[str, tuple[Mapping[str, object], ...]]:
    """
    함수 이름: _validate_transport_ui_events()
    기능: 실제 envelope session/event/sequence와 atomic Trade publication truth를 검증한다.
    인자: value -> transport_session_id와 event array를 가진 exact batch object
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
            _require_nonnegative_integer(
                aggregate_version,
                "transport aggregate_version",
            )
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
        # Envelope sequence가 ordering truth이며 지연 event의 원 발생 시각은 전역 단조일 필요가 없다.
        _require_utc_timestamp(
            entry["published_at"],
            "UI published_at",
        )

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


def _validate_cross_trace_contract(
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
    baseline_history_count: int,
    run_trades: tuple[Mapping[str, object], ...],
    transport_session_id: str,
    ui_events: tuple[Mapping[str, object], ...],
    recovery: Mapping[str, object],
    final_state: Mapping[str, object],
) -> None:
    """
    함수 이름: _validate_cross_trace_contract()
    기능: preflight→1L path→order/fill→Trade/transport→fresh final 전체 provenance를 결속한다.
    인자: outcome -> top-level normalized outcome
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
        baseline_history_count -> 별도 검증한 baseline Trade 수
        run_trades -> 이번 run의 normalized durable Trade tuple
        transport_session_id -> 실제 BackendEventStream session UUID
        ui_events -> 검증된 transport/UI event tuple
        recovery -> 검증된 recovery mapping
        final_state -> 검증된 final state mapping
    반환값: 없음
    작성 날짜: 2026/08/31
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

    # Preflight, event, attempt, result, fill, Trade, transport와 final verification을 run window에 둔다.
    causal_timestamps = [
        _require_utc_timestamp(preflight["verified_at"], "preflight verified_at"),
        _require_utc_timestamp(
            preflight["fresh_filters"]["observed_at"],
            "filter observed_at",
        ),
        *(
            _require_utc_timestamp(
                event["source_event_time"],
                "market source_event_time",
            )
            for event in market_events
        ),
        *(
            _require_utc_timestamp(
                event["source_event_time"],
                "account source_event_time",
            )
            for event in account_events
        ),
        *(
            _require_utc_timestamp(attempt["attempted_at"], "order attempted_at")
            for attempt in attempts
        ),
        *(
            _require_utc_timestamp(
                evidence["observed_at"],
                "submit-time filter observed_at",
            )
            for evidence in submit_time_filter_evidence
        ),
        *(
            _require_utc_timestamp(result["observed_at"], "result observed_at")
            for result in results
        ),
        *(
            _require_utc_timestamp(fill["event_time"], "fill event_time")
            for result in results
            for fill in result["incremental_fills"]
        ),
        *(
            _require_utc_timestamp(trade["executed_at"], "Trade executed_at")
            for trade in run_trades
        ),
        *(
            _require_utc_timestamp(event["published_at"], "UI published_at")
            for event in ui_events
        ),
        _require_utc_timestamp(final_state["verified_at"], "final verified_at"),
    ]
    if any(
        timestamp < started_at or timestamp > completed_at
        for timestamp in causal_timestamps
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "trace evidence lies outside the run time window"
        )
    preflight_verified_at = _require_utc_timestamp(
        preflight["verified_at"],
        "preflight verified_at",
    )
    filter_observed_at = _require_utc_timestamp(
        preflight["fresh_filters"]["observed_at"],
        "filter observed_at",
    )
    if filter_observed_at > preflight_verified_at:
        raise PhaseThirteenPublicTraceValidationError(
            "fresh filter observation follows preflight verification"
        )
    if attempts and preflight_verified_at > _require_utc_timestamp(
        attempts[0]["attempted_at"],
        "first order attempted_at",
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "order attempt precedes its preflight"
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

    # Final fresh runtime은 evidence publication 뒤 검증되고 원 transport session을 재사용하지 않는다.
    final_verified_at = _require_utc_timestamp(
        final_state["verified_at"],
        "final verified_at",
    )
    publication_end_times = [
        *(
            _require_utc_timestamp(result["observed_at"], "result observed_at")
            for result in results
        ),
        *(
            _require_utc_timestamp(trade["executed_at"], "Trade executed_at")
            for trade in run_trades
        ),
        *(
            _require_utc_timestamp(event["published_at"], "UI published_at")
            for event in ui_events
        ),
    ]
    if publication_end_times and final_verified_at < max(publication_end_times):
        raise PhaseThirteenPublicTraceValidationError(
            "fresh final verification precedes run evidence"
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
        return  # FAILED/BLOCKED/NO_SIGNAL은 decision 이전에도 종료될 수 있다.

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
    if buy_filter_evidence is not None and _require_utc_timestamp(
        fingerprint["source_event_time"],
        "decision source_event_time",
    ) > _require_utc_timestamp(
        buy_filter_evidence["observed_at"],
        "BUY submit-time filter observed_at",
    ):
        raise PhaseThirteenPublicTraceValidationError(
            "BUY submit-time filter observation precedes its public source event"
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
    for event in ui_events:
        if event["event_type"] != "ORDER_EXECUTED":
            continue
        trade = trades_by_id[str(event["related_id"])]
        if _require_utc_timestamp(
            event["published_at"],
            "ORDER_EXECUTED published_at",
        ) < _require_utc_timestamp(trade["executed_at"], "Trade executed_at"):
            raise PhaseThirteenPublicTraceValidationError(
                "ORDER_EXECUTED publication precedes its durable Trade"
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
    if trace["schema_version"] != PHASE13_PUBLIC_TRACE_SCHEMA_VERSION:
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
    typed_reason = _require_identifier(
        trace["typed_reason"],
        "trace typed_reason",
        allow_none=True,
    )

    # UUIDv4 run identity와 bounded run timestamps는 artifact끼리의 accidental 합성을 막는다.
    _require_uuid4(trace["run_id"], "trace run_id")
    started_at, completed_at = _validate_timestamps(trace["timestamps"])
    preflight = _validate_preflight(trace["preflight"])
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
    account_events = _validate_account_events(trace["public_account_events"])
    attempts = _validate_order_attempts(trace["order_attempts"])
    submit_time_filter_evidence = _validate_submit_time_filter_evidence(
        trace["submit_time_filter_evidence"],
        attempts,
    )
    results = _validate_order_results(trace["order_results"], attempts)
    _validate_order_execution_traces(
        trace["order_execution_traces"],
        attempts,
        results,
        require_complete_success=(outcome == "SUCCESS"),
    )
    run_trades = _validate_run_durable_trades(trace["run_durable_trades"])
    transport_session_id, ui_events = _validate_transport_ui_events(
        trace["transport_ui_event_batch"]
    )
    recovery = _validate_recovery(trace["recovery"])
    final_state = _validate_final_state(
        trace["final_state"],
        baseline_history_count=baseline_history_count,
        attempts=attempts,
        run_trades=run_trades,
    )
    _validate_cross_trace_contract(
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
