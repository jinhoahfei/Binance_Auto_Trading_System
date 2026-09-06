"""ADR-004 JSONL v1/v2 거래 이력을 streaming 방식으로 복원하고 v2를 기록한다."""

from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import stat
from threading import RLock
from typing import BinaryIO
from zoneinfo import ZoneInfo

from binance_auto_trader.adapters.platform.file_durability import (
    flush_created_file_metadata,
)
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.history import (
    FeeAssetConversionRequiredError,
    OrderHistoryConflictError,
    Trade,
    TradeHistoryQuery,
    TradeSide,
    trade_from_json_object,
    trade_to_json_object,
)
from binance_auto_trader.domain.trading.order import (
    Order,
    PendingOrderRecoveryLifecycle,
    PendingOrderRecoveryRecord,
    PendingOrderSubmissionProvenance,
)
from binance_auto_trader.domain.trading.risk import (
    ManualKillBehavior,
    ManualKillControlState,
)
from binance_auto_trader.domain.trading.states import (
    ExitReason,
    OrderSide,
    StrategyType,
)


_PENDING_ORDER_LEGACY_SCHEMA_VERSION = 1
_PENDING_ORDER_LIFECYCLE_SCHEMA_VERSION = 2
_PENDING_ORDER_POLICY_SCHEMA_VERSION = 3
_PENDING_ORDER_SCHEMA_VERSION = 4
_SUPPORTED_PENDING_ORDER_SCHEMA_VERSIONS = frozenset(
    {
        _PENDING_ORDER_LEGACY_SCHEMA_VERSION,
        _PENDING_ORDER_LIFECYCLE_SCHEMA_VERSION,
        _PENDING_ORDER_POLICY_SCHEMA_VERSION,
        _PENDING_ORDER_SCHEMA_VERSION,
    }
)
_PENDING_ORDER_RECORD_TYPE = "pending_order_event"
_PENDING_ORDER_UPSERT = "UPSERT"
_PENDING_ORDER_TRANSITION = "TRANSITION"
_PENDING_ORDER_REMOVE = "REMOVE"
_PENDING_ORDER_LEGACY_UPSERT_KEYS = frozenset(
    {"operation", "order", "record_type", "schema_version"}
)
_PENDING_ORDER_UPSERT_KEYS = frozenset(
    {
        "lifecycle",
        "operation",
        "order",
        "record_type",
        "schema_version",
    }
)
_PENDING_ORDER_TRANSITION_KEYS = frozenset(
    {
        "client_order_id",
        "lifecycle",
        "operation",
        "record_type",
        "schema_version",
    }
)
_PENDING_ORDER_REMOVE_KEYS = frozenset(
    {"client_order_id", "operation", "record_type", "schema_version"}
)
_MANUAL_KILL_CONTROL_LEGACY_SCHEMA_VERSION = 1
_MANUAL_KILL_CONTROL_SCHEMA_VERSION = 2
_SUPPORTED_MANUAL_KILL_CONTROL_SCHEMA_VERSIONS = frozenset(
    {
        _MANUAL_KILL_CONTROL_LEGACY_SCHEMA_VERSION,
        _MANUAL_KILL_CONTROL_SCHEMA_VERSION,
    }
)
_MANUAL_KILL_CONTROL_REPLAY_LIMIT = 1_024
_MANUAL_KILL_CONTROL_RECORD_TYPE = "manual_kill_control"
_MANUAL_KILL_CONTROL_KEYS = frozenset(
    {
        "active",
        "behavior",
        "command_id",
        "expected_version",
        "policy_version",
        "record_type",
        "schema_version",
        "version",
    }
)
_PENDING_ORDER_LEGACY_METADATA_KEYS = frozenset(
    {
        "client_order_id",
        "exit_reason",
        "intent_id",
        "market_price_at_decision",
        "regime_type",
        "requested_quantity",
        "side",
        "strategy",
        "submission_attempt",
        "submitted_quantity",
        "symbol",
    }
)
_PENDING_ORDER_POLICY_METADATA_KEYS = frozenset(
    {*_PENDING_ORDER_LEGACY_METADATA_KEYS, "risk_policy_version"}
)
_PENDING_ORDER_METADATA_KEYS = frozenset(
    {*_PENDING_ORDER_POLICY_METADATA_KEYS, "exit_pct_b_at_intent"}
)
# Lifecycle graph는 거래소 사실이 후퇴하지 않고 history commit 직전까지 전진하게 한다.
_PENDING_ORDER_LIFECYCLE_TRANSITIONS = {
    PendingOrderRecoveryLifecycle.PREPARED: frozenset(
        {
            PendingOrderRecoveryLifecycle.SUBMITTED,
            PendingOrderRecoveryLifecycle.UNKNOWN,
            PendingOrderRecoveryLifecycle.PARTIAL,
            PendingOrderRecoveryLifecycle.TERMINAL,
            PendingOrderRecoveryLifecycle.SUBMISSION_REJECTED_CONFIRMED,
        }
    ),
    PendingOrderRecoveryLifecycle.SUBMITTED: frozenset(
        {
            PendingOrderRecoveryLifecycle.UNKNOWN,
            PendingOrderRecoveryLifecycle.PARTIAL,
            PendingOrderRecoveryLifecycle.TERMINAL,
            PendingOrderRecoveryLifecycle.SUBMISSION_REJECTED_CONFIRMED,
        }
    ),
    PendingOrderRecoveryLifecycle.UNKNOWN: frozenset(
        {
            PendingOrderRecoveryLifecycle.PARTIAL,
            PendingOrderRecoveryLifecycle.TERMINAL,
        }
    ),
    PendingOrderRecoveryLifecycle.PARTIAL: frozenset(
        {PendingOrderRecoveryLifecycle.TERMINAL}
    ),
    PendingOrderRecoveryLifecycle.TERMINAL: frozenset(
        {PendingOrderRecoveryLifecycle.HISTORY_COMMITTED}
    ),
    PendingOrderRecoveryLifecycle.HISTORY_COMMITTED: frozenset(),
    PendingOrderRecoveryLifecycle.SUBMISSION_REJECTED_CONFIRMED: frozenset(),
}
_KOREA_TIME_ZONE = ZoneInfo("Asia/Seoul")


class HistoryCorruptedError(ValueError):
    """
    클래스 이름: HistoryCorruptedError
    기능: 자동 복구할 수 없는 완결 JSONL record 손상을 나타낸다.
    작성 날짜: 2026/08/21
    """

    code = "HISTORY_CORRUPTED"

    def __init__(self, line_number: int, reason: str) -> None:
        """
        함수 이름: __init__()
        기능: 손상된 line 번호와 사용자 secret을 포함하지 않는 원인을 보존한다.
        인자: line_number -> 1부터 시작하는 손상 JSONL line 번호
            reason -> 손상 유형을 설명하는 안전한 문자열
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # UI와 trace가 raw record 없이도 진단할 수 있는 line과 안전한 원인만 보존한다.
        self.line_number = line_number
        self.reason = reason
        super().__init__(
            f"history line {line_number} is corrupted: {reason}"
        )  # credential이나 원본 JSON은 예외 문자열에 포함하지 않는다.


class PendingOrderJournalCorruptedError(ValueError):
    """
    클래스 이름: PendingOrderJournalCorruptedError
    기능: 재시작 주문 sidecar에서 자동 해석할 수 없는 record 손상을 나타낸다.
    작성 날짜: 2026/08/22
    """

    code = "PENDING_ORDER_JOURNAL_CORRUPTED"

    def __init__(self, line_number: int, reason: str) -> None:
        """
        함수 이름: __init__()
        기능: 손상 line과 credential을 포함하지 않는 원인 분류만 보존한다.
        인자: line_number -> 1부터 시작하는 손상 sidecar line 번호
            reason -> 손상 유형을 설명하는 안전한 문자열
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # raw response나 credential이 예외 경계를 넘어가지 않도록 안전한 분류만 저장한다.
        self.line_number = line_number
        self.reason = reason
        super().__init__(
            f"pending-order journal line {line_number} is corrupted: {reason}"
        )  # 운영 log에는 sidecar 원문 대신 line과 오류 타입만 남긴다.


class PendingOrderJournalConflictError(ValueError):
    """
    클래스 이름: PendingOrderJournalConflictError
    기능: 하나의 client order ID가 서로 다른 제출 전 Order metadata에 연결되는 충돌을 나타낸다.
    작성 날짜: 2026/08/23
    """

    code = "PENDING_ORDER_JOURNAL_CONFLICT"

    def __init__(self, client_order_id: str) -> None:
        """
        함수 이름: __init__()
        기능: 충돌한 client order ID만 보존하고 Order payload 원문은 노출하지 않는다.
        인자: client_order_id -> 이미 다른 metadata에 연결된 application order ID
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        # Order payload나 credential 없이 멱등 identity만 안전한 진단 정보로 남긴다.
        if not isinstance(client_order_id, str) or not client_order_id:
            raise ValueError("client_order_id must be a non-empty string")
        self.client_order_id = client_order_id
        super().__init__(
            "pending-order client ID is already bound to different metadata"
        )  # 예외 문자열에는 충돌 payload와 client ID 자체를 포함하지 않는다.


class ManualKillControlJournalCorruptedError(ValueError):
    """
    클래스 이름: ManualKillControlJournalCorruptedError
    기능: 재시작 manual kill control journal의 자동 해석 불가능한 손상을 나타낸다.
    작성 날짜: 2026/08/29
    """

    code = "MANUAL_KILL_CONTROL_JOURNAL_CORRUPTED"

    def __init__(self, line_number: int, reason: str) -> None:
        """
        함수 이름: __init__()
        기능: 손상 line 번호와 원문을 포함하지 않는 안정적인 원인 분류를 보존한다.
        인자: line_number -> 1부터 시작하는 손상 JSONL line 번호
            reason -> credential이나 record 원문이 없는 오류 분류
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # 운영 오류에 control journal 원문이 섞이지 않도록 line과 예외 타입만 공개한다.
        if type(line_number) is not int or line_number < 1:
            raise ValueError("line_number must be a positive integer")
        if not isinstance(reason, str) or not reason:
            raise ValueError("reason must be a non-empty string")
        self.line_number = line_number
        self.reason = reason
        super().__init__(
            f"manual-kill control journal line {line_number} is corrupted: {reason}"
        )  # 사용자 UI에는 raw JSON이 아니라 typed corruption만 전달한다.


class _NonStandardJsonConstantError(ValueError):
    """
    클래스 이름: _NonStandardJsonConstantError
    기능: strict JSON parser가 거부한 NaN·Infinity 상수를 구문 해석 실패로 구분한다.
    작성 날짜: 2026/08/21
    """


class _DuplicateJsonKeyError(ValueError):
    """
    클래스 이름: _DuplicateJsonKeyError
    기능: decode된 JSON object의 중복 key로 인한 canonical schema 손상을 나타낸다.
    작성 날짜: 2026/08/21
    """


def _utc_now() -> datetime:
    """
    함수 이름: _utc_now()
    기능: corrupt tail backup 이름에 사용할 현재 UTC 시각을 반환한다.
    인자: 없음
    반환값: 현재 UTC datetime
    작성 날짜: 2026/08/21
    """
    return datetime.now(timezone.utc)  # backup 충돌 방지 이름은 canonical UTC를 사용한다.


def _normalize_utc_datetime(value: object, field_name: str) -> datetime:
    """
    함수 이름: _normalize_utc_datetime()
    기능: 주입 clock 결과가 timezone-aware UTC인지 검증하고 정규화한다.
    인자: value -> 검증할 datetime
        field_name -> 오류 메시지에 표시할 필드 이름
    반환값: timezone.utc datetime
    작성 날짜: 2026/08/21
    """
    # backup 이름에 쓰기 전에 naive 시각과 UTC 외 offset을 명시적으로 거부한다.
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must use UTC")

    return value.astimezone(timezone.utc)  # 동등한 UTC offset도 canonical 객체로 맞춘다.


def _reject_json_constant(constant_name: str) -> object:
    """
    함수 이름: _reject_json_constant()
    기능: Python JSON decoder가 허용하는 NaN과 Infinity 비표준 상수를 거부한다.
    인자: constant_name -> decoder가 발견한 비표준 상수 이름
    반환값: 정상 반환 없이 ValueError 발생
    작성 날짜: 2026/08/21
    """
    raise _NonStandardJsonConstantError(
        f"non-standard JSON constant is forbidden: {constant_name}"
    )  # json.loads parse_constant callback은 정상 값을 반환하지 않는다.


def _build_unique_json_object(
    object_pairs: list[tuple[str, object]],
) -> dict[str, object]:
    """
    함수 이름: _build_unique_json_object()
    기능: JSON object pair에서 중복 key를 거부하고 dictionary를 생성한다.
    인자: object_pairs -> decoder가 key 순서대로 전달한 pair 목록
    반환값: 중복 key가 없는 JSON dictionary
    작성 날짜: 2026/08/21
    """
    # pair 순서를 그대로 순회해야 Python dict 변환 전에 중복 key를 발견할 수 있다.
    decoded_object: dict[str, object] = {}
    for key, value in object_pairs:
        if key in decoded_object:
            raise _DuplicateJsonKeyError("duplicate JSON object key is forbidden")
        decoded_object[key] = value  # 최초 key의 wire 순서도 canonical decode에 보존한다.

    return decoded_object  # 중복이 없는 object만 schema 검증 단계로 넘긴다.


def _decode_json_line(raw_line: bytes) -> object:
    """
    함수 이름: _decode_json_line()
    기능: 한 JSONL line의 UTF-8과 strict JSON 구문만 해석한다.
    인자: raw_line -> LF를 제거한 단일 record bytes
    반환값: JSON decoder가 만든 값
    작성 날짜: 2026/08/21
    """
    # byte framing과 문자·JSON 구문 검증을 분리해 손상 원인을 정확히 분류한다.
    line_text = raw_line.decode("utf-8")
    return json.loads(
        line_text,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_build_unique_json_object,
    )


def _trade_from_decoded_json(decoded_record: object) -> Trade:
    """
    함수 이름: _trade_from_decoded_json()
    기능: decode가 끝난 JSON 값을 object shape와 Trade schema/domain으로 검증한다.
    인자: decoded_record -> strict JSON decoder가 반환한 값
    반환값: canonical Trade
    작성 날짜: 2026/08/21
    """
    # array나 scalar JSON이 Trade parser에 record처럼 들어가지 못하게 shape를 고정한다.
    if not isinstance(decoded_record, Mapping):
        raise TypeError("trade record must decode to a JSON object")

    return trade_from_json_object(decoded_record)  # exact JSONL v1/v2 schema는 domain이 검증한다.


def _fsync_parent_directory(file_path: Path) -> None:
    """
    함수 이름: _fsync_parent_directory()
    기능: POSIX parent fsync 또는 Windows file-buffer barrier로 생성 완료 파일의 durability를 확인한다.
    인자: file_path -> directory entry를 보존할 생성 완료 파일 경로
    반환값: 없음
    작성 날짜: 2026/08/21
    """
    # POSIX directory fsync와 Windows writable-file flush 차이는 기술 adapter가 소유한다.
    flush_created_file_metadata(file_path)  # 지원하지 않는 directory open을 Windows에서 시도하지 않는다.



def _normalize_order_id(order_id: object) -> str:
    """
    함수 이름: _normalize_order_id()
    기능: repository operation의 Long 또는 JSONL string order ID를 canonical string으로 만든다.
    인자: order_id -> 양의 정수 또는 양의 정수 형식 문자열
    반환값: canonical order ID 문자열
    작성 날짜: 2026/08/22
    """
    # bool은 int subclass이므로 정수 branch보다 먼저 차단한다.
    if isinstance(order_id, bool):
        raise TypeError("order_id must be an integer or string")
    if isinstance(order_id, int):
        if order_id <= 0:
            raise ValueError("order_id must be greater than zero")
        return str(order_id)  # memory와 JSONL index가 공유할 문자열 key로 통일한다.
    if not isinstance(order_id, str):
        raise TypeError("order_id must be an integer or string")
    if not order_id.isascii() or not order_id.isdigit() or order_id.startswith("0"):
        raise ValueError("order_id must be a positive integer string")

    return order_id  # 선행 0 없는 wire 표현은 그대로 canonical key가 된다.


def _normalize_client_order_id(client_order_id: object) -> str:
    """
    함수 이름: _normalize_client_order_id()
    기능: pending-order event key를 공백 없는 client order ID로 검증한다.
    인자: client_order_id -> 검증할 client order ID
    반환값: 검증을 마친 원래 문자열
    작성 날짜: 2026/08/22
    """
    # bool이나 임의 객체가 문자열 변환을 통해 journal key로 섞이지 않게 한다.
    if not isinstance(client_order_id, str):
        raise TypeError("client_order_id must be a string")
    if not client_order_id or client_order_id.strip() != client_order_id:
        raise ValueError("client_order_id must be non-empty without outer whitespace")

    return client_order_id  # Order 생성 검증과 동일한 원문 identity를 보존한다.


def _manual_kill_control_to_json_object(
    state: ManualKillControlState,
) -> dict[str, object]:
    """
    함수 이름: _manual_kill_control_to_json_object()
    기능: 검증된 manual kill command receipt를 current schema exact JSON object로 변환한다.
    인자: state -> fsync할 manual kill control state
    반환값: credential을 포함하지 않는 canonical JSON mapping
    작성 날짜: 2026/08/29
    """
    # Domain 생성자의 exact type 검증을 통과한 state만 persistence schema로 내린다.
    if not isinstance(state, ManualKillControlState):
        raise TypeError("state must be a ManualKillControlState")

    return {
        "record_type": _MANUAL_KILL_CONTROL_RECORD_TYPE,
        "schema_version": _MANUAL_KILL_CONTROL_SCHEMA_VERSION,
        "active": state.active,
        "version": state.version,
        "command_id": state.command_id,
        "expected_version": state.expected_version,
        "behavior": (
            None if state.behavior is None else state.behavior.value
        ),
        "policy_version": state.policy_version,
    }  # key 순서는 diff와 byte 증거가 안정적으로 유지되도록 고정한다.


def _manual_kill_control_from_json_object(
    decoded_record: object,
) -> tuple[int, ManualKillControlState]:
    """
    함수 이름: _manual_kill_control_from_json_object()
    기능: strict JSON 결과를 지원하는 manual kill schema와 command receipt로 검증해 복원한다.
    인자: decoded_record -> 중복 key와 비표준 상수를 거부한 JSON 값
    반환값: 검증된 schema version과 ManualKillControlState
    작성 날짜: 2026/08/29
    """
    # Array·scalar와 추가 field를 모두 거부해 미래 schema를 현재 writer로 추측 해석하지 않는다.
    if not isinstance(decoded_record, Mapping):
        raise TypeError("manual-kill control record must be an object")
    if frozenset(decoded_record) != _MANUAL_KILL_CONTROL_KEYS:
        raise ValueError("manual-kill control record keys are invalid")
    if decoded_record["record_type"] != _MANUAL_KILL_CONTROL_RECORD_TYPE:
        raise ValueError("manual-kill control record_type is invalid")
    schema_version = decoded_record["schema_version"]
    if (
        type(schema_version) is not int
        or schema_version
        not in _SUPPORTED_MANUAL_KILL_CONTROL_SCHEMA_VERSIONS
    ):
        raise ValueError("manual-kill control schema_version is unsupported")

    behavior_value = decoded_record["behavior"]
    if behavior_value is not None:
        if not isinstance(behavior_value, str):
            raise TypeError("manual-kill behavior must be a string or null")
        try:
            behavior = ManualKillBehavior(behavior_value)
        except ValueError as error:
            raise ValueError("manual-kill behavior is unsupported") from error
    else:
        behavior = None

    return (
        schema_version,
        ManualKillControlState(
            active=decoded_record["active"],
            version=decoded_record["version"],
            command_id=decoded_record["command_id"],
            expected_version=decoded_record["expected_version"],
            behavior=behavior,
            policy_version=decoded_record["policy_version"],
        ),
    )  # bool/version의 마지막 exact 검증은 domain value가 단일 owner로 수행한다.


def _pending_decimal_to_text(value: Decimal, field_name: str) -> str:
    """
    함수 이름: _pending_decimal_to_text()
    기능: Order의 금융 Decimal을 exponent 없는 sidecar 문자열로 변환한다.
    인자: value -> 직렬화할 canonical Decimal
        field_name -> 검증 오류에 사용할 metadata 필드 이름
    반환값: exponent 없는 Decimal 문자열
    작성 날짜: 2026/08/22
    """
    # Order 밖에서 호출되더라도 float나 비유한 Decimal이 journal에 기록되지 않게 한다.
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be a Decimal")
    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite")

    return format(value, "f")  # binary float를 거치지 않은 canonical 금융 문자열이다.


def _pending_decimal_from_text(value: object, field_name: str) -> Decimal:
    """
    함수 이름: _pending_decimal_from_text()
    기능: sidecar의 canonical Decimal 문자열을 정확한 Decimal로 복원한다.
    인자: value -> JSON metadata에서 읽은 값
        field_name -> 검증 오류에 사용할 metadata 필드 이름
    반환값: canonical finite Decimal
    작성 날짜: 2026/08/22
    """
    # JSON number와 exponent 표기는 runtime별 해석 차이를 만들 수 있어 문자열만 허용한다.
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a decimal string")
    try:
        decimal_value = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{field_name} must be a decimal string") from error
    if not decimal_value.is_finite() or format(decimal_value, "f") != value:
        raise ValueError(f"{field_name} must use canonical decimal text")

    return decimal_value  # Order constructor가 양수와 수량 관계를 추가로 검증한다.


def _require_exact_pending_keys(
    record: Mapping[str, object],
    expected_keys: frozenset[str],
    record_name: str,
) -> None:
    """
    함수 이름: _require_exact_pending_keys()
    기능: pending-order record가 credential 필드 없는 exact schema인지 검증한다.
    인자: record -> 검증할 decoded JSON object
        expected_keys -> 허용된 exact key 집합
        record_name -> 안전한 schema 오류 이름
    반환값: 없음
    작성 날짜: 2026/08/22
    """
    # 알 수 없는 필드는 raw response나 credential 유입일 수 있으므로 무시하지 않는다.
    if frozenset(record) != expected_keys:
        raise ValueError(f"{record_name} must use the exact schema")


def _pending_order_to_json_object(order: Order) -> dict[str, object]:
    """
    함수 이름: _pending_order_to_json_object()
    기능: 제출 전 복구에 필요한 Order metadata만 JSON object로 변환한다.
    인자: order -> 직렬화할 canonical Order
    반환값: credential과 raw response가 없는 metadata object
    작성 날짜: 2026/08/22
    """
    # mutable 체결 상태는 거래소 재조회 대상이므로 local intent metadata와 분리한다.
    return {
        "intent_id": order.intent_id,
        "client_order_id": order.client_order_id,
        "submission_attempt": order.submission_attempt,
        "symbol": order.symbol,
        "side": order.side.value,
        "strategy": order.strategy.value,
        "regime_type": order.regime_type.value,
        "requested_quantity": _pending_decimal_to_text(
            order.requested_quantity,
            "requested_quantity",
        ),
        "submitted_quantity": _pending_decimal_to_text(
            order.submitted_quantity,
            "submitted_quantity",
        ),
        "market_price_at_decision": _pending_decimal_to_text(
            order.market_price_at_decision,
            "market_price_at_decision",
        ),
        "risk_policy_version": order.risk_policy_version,
        "exit_reason": (
            None if order.exit_reason is None else order.exit_reason.value
        ),
        "exit_pct_b_at_intent": (
            None
            if order.exit_pct_b_at_intent is None
            else _pending_decimal_to_text(
                order.exit_pct_b_at_intent,
                "exit_pct_b_at_intent",
            )
        ),
    }  # exact key 목록은 decode 시 다시 검증해 sidecar가 확장 저장소가 되지 않게 한다.


def _pending_order_from_json_object(
    record: object,
    *,
    legacy_policy_version: bool = False,
    legacy_exit_pct_b: bool = False,
) -> Order:
    """
    함수 이름: _pending_order_from_json_object()
    기능: exact metadata object를 검증된 canonical Order로 복원한다.
    인자: record -> UPSERT event의 decoded order 값
        legacy_policy_version -> schema v1/v2에 policy version 필드가 없음을 허용할지 여부
        legacy_exit_pct_b -> schema v1~v3에 매도 의도 %B 필드가 없음을 허용할지 여부
    반환값: 제출 전 metadata만 가진 Order
    작성 날짜: 2026/08/25
    """
    # list나 scalar가 mapping처럼 처리되지 않도록 outer shape부터 고정한다.
    if not isinstance(record, Mapping):
        raise TypeError("pending order metadata must be a JSON object")
    if type(legacy_policy_version) is not bool:
        raise TypeError("legacy_policy_version must be a bool")
    if type(legacy_exit_pct_b) is not bool:
        raise TypeError("legacy_exit_pct_b must be a bool")
    if legacy_policy_version and not legacy_exit_pct_b:
        raise ValueError("legacy policy schema must also omit exit pct B")
    _require_exact_pending_keys(
        record,
        (
            _PENDING_ORDER_LEGACY_METADATA_KEYS
            if legacy_policy_version
            else (
                _PENDING_ORDER_POLICY_METADATA_KEYS
                if legacy_exit_pct_b
                else _PENDING_ORDER_METADATA_KEYS
            )
        ),
        "pending order metadata",
    )

    # enum constructor와 Order 불변식이 잘못된 wire 문자열을 fail closed한다.
    exit_reason_value = record["exit_reason"]
    exit_reason = (
        None
        if exit_reason_value is None
        else ExitReason(exit_reason_value)
    )
    exit_pct_b_at_intent = (
        None
        if legacy_exit_pct_b or record["exit_pct_b_at_intent"] is None
        else _pending_decimal_from_text(
            record["exit_pct_b_at_intent"],
            "exit_pct_b_at_intent",
        )
    )
    return Order(
        intent_id=record["intent_id"],
        client_order_id=record["client_order_id"],
        submission_attempt=record["submission_attempt"],
        symbol=record["symbol"],
        side=OrderSide(record["side"]),
        strategy=StrategyType(record["strategy"]),
        regime_type=RegimeType(record["regime_type"]),
        requested_quantity=_pending_decimal_from_text(
            record["requested_quantity"],
            "requested_quantity",
        ),
        submitted_quantity=_pending_decimal_from_text(
            record["submitted_quantity"],
            "submitted_quantity",
        ),
        market_price_at_decision=_pending_decimal_from_text(
            record["market_price_at_decision"],
            "market_price_at_decision",
        ),
        risk_policy_version=(
            None
            if legacy_policy_version
            else record["risk_policy_version"]
        ),
        exit_reason=exit_reason,
        exit_pct_b_at_intent=exit_pct_b_at_intent,
    )  # domain 생성 성공 자체가 startup 복구 가능한 metadata의 마지막 검증 경계다.


def _build_pending_upsert_event(order: Order) -> dict[str, object]:
    """
    함수 이름: _build_pending_upsert_event()
    기능: 한 Order metadata를 활성화하는 canonical UPSERT event를 만든다.
    인자: order -> 활성 pending 상태로 저장할 Order
    반환값: PREPARED lifecycle을 명시한 sidecar schema v4 UPSERT object
    작성 날짜: 2026/08/25
    """
    # envelope에는 schema 식별자와 복구 operation 외의 운영 정보를 저장하지 않는다.
    return {
        "schema_version": _PENDING_ORDER_SCHEMA_VERSION,
        "record_type": _PENDING_ORDER_RECORD_TYPE,
        "operation": _PENDING_ORDER_UPSERT,
        "lifecycle": PendingOrderRecoveryLifecycle.PREPARED.value,
        "order": _pending_order_to_json_object(order),
    }  # credential 없이 재시작 query에 필요한 metadata만 포함한다.


def _build_pending_transition_event(
    client_order_id: str,
    lifecycle: PendingOrderRecoveryLifecycle,
) -> dict[str, object]:
    """
    함수 이름: _build_pending_transition_event()
    기능: active pending order의 durable lifecycle을 전진시키는 canonical event를 만든다.
    인자: client_order_id -> lifecycle을 변경할 application order ID
        lifecycle -> fsync할 다음 recovery lifecycle
    반환값: sidecar schema v4 TRANSITION object
    작성 날짜: 2026/08/25
    """
    if not isinstance(lifecycle, PendingOrderRecoveryLifecycle):
        raise TypeError("lifecycle must be a PendingOrderRecoveryLifecycle")
    if lifecycle is PendingOrderRecoveryLifecycle.PREPARED:
        raise ValueError("pending-order transition must advance from PREPARED")

    # 제출 응답 payload 대신 정규화 lifecycle과 client ID만 durable 증거로 남긴다.
    return {
        "schema_version": _PENDING_ORDER_SCHEMA_VERSION,
        "record_type": _PENDING_ORDER_RECORD_TYPE,
        "operation": _PENDING_ORDER_TRANSITION,
        "client_order_id": _normalize_client_order_id(client_order_id),
        "lifecycle": lifecycle.value,
    }


def _pending_lifecycle_can_transition(
    current_lifecycle: PendingOrderRecoveryLifecycle,
    next_lifecycle: PendingOrderRecoveryLifecycle,
) -> bool:
    """
    함수 이름: _pending_lifecycle_can_transition()
    기능: durable 주문 lifecycle의 멱등 재실행과 단조 전진 허용 여부를 판정한다.
    인자: current_lifecycle -> journal이 현재 증명한 lifecycle
        next_lifecycle -> caller가 fsync하려는 lifecycle
    반환값: 멱등 또는 허용된 단조 전진이면 True
    작성 날짜: 2026/08/25
    """
    # Enum 외 값이 transition graph의 dictionary lookup으로 숨지 않게 먼저 거부한다.
    if not isinstance(current_lifecycle, PendingOrderRecoveryLifecycle):
        raise TypeError("current_lifecycle must be a PendingOrderRecoveryLifecycle")
    if not isinstance(next_lifecycle, PendingOrderRecoveryLifecycle):
        raise TypeError("next_lifecycle must be a PendingOrderRecoveryLifecycle")

    return (
        current_lifecycle is next_lifecycle
        or next_lifecycle
        in _PENDING_ORDER_LIFECYCLE_TRANSITIONS[current_lifecycle]
    )  # 불명·partial에서 제출 전으로 되돌아가는 상태는 허용하지 않는다.


def _build_pending_remove_event(client_order_id: str) -> dict[str, object]:
    """
    함수 이름: _build_pending_remove_event()
    기능: 한 활성 Order metadata를 제거하는 canonical REMOVE event를 만든다.
    인자: client_order_id -> 비활성화할 pending order key
    반환값: sidecar schema v4 REMOVE object
    작성 날짜: 2026/08/25
    """
    # terminal 처리 뒤에는 식별자 하나만 tombstone으로 남겨 metadata 중복을 피한다.
    return {
        "schema_version": _PENDING_ORDER_SCHEMA_VERSION,
        "record_type": _PENDING_ORDER_RECORD_TYPE,
        "operation": _PENDING_ORDER_REMOVE,
        "client_order_id": client_order_id,
    }  # replay는 같은 REMOVE가 반복되어도 안전한 dict.pop으로 처리한다.


def _pending_event_from_decoded_json(
    decoded_record: object,
) -> tuple[
    str,
    PendingOrderRecoveryRecord
    | str
    | tuple[str, PendingOrderRecoveryLifecycle],
]:
    """
    함수 이름: _pending_event_from_decoded_json()
    기능: decoded sidecar object를 schema 제출 근거가 포함된 recovery payload로 변환한다.
    인자: decoded_record -> strict JSON decoder가 반환한 값
    반환값: operation과 검증된 recovery payload tuple
    작성 날짜: 2026/08/29
    """
    # scalar나 array event는 envelope field를 조회하기 전에 명시적으로 거부한다.
    if not isinstance(decoded_record, Mapping):
        raise TypeError("pending-order event must be a JSON object")
    schema_version = decoded_record.get("schema_version")
    if type(schema_version) is not int or (
        schema_version not in _SUPPORTED_PENDING_ORDER_SCHEMA_VERSIONS
    ):
        raise ValueError("pending-order schema_version is unsupported")
    if decoded_record.get("record_type") != _PENDING_ORDER_RECORD_TYPE:
        raise ValueError("pending-order record_type is unsupported")

    # operation별 exact envelope를 적용해 숨은 raw response 필드도 즉시 차단한다.
    operation = decoded_record.get("operation")
    if operation == _PENDING_ORDER_UPSERT:
        expected_keys = (
            _PENDING_ORDER_LEGACY_UPSERT_KEYS
            if schema_version == _PENDING_ORDER_LEGACY_SCHEMA_VERSION
            else _PENDING_ORDER_UPSERT_KEYS
        )
        _require_exact_pending_keys(
            decoded_record,
            expected_keys,
            "pending-order UPSERT event",
        )
        lifecycle = (
            PendingOrderRecoveryLifecycle.PREPARED
            if schema_version == _PENDING_ORDER_LEGACY_SCHEMA_VERSION
            else PendingOrderRecoveryLifecycle(decoded_record["lifecycle"])
        )
        if lifecycle is not PendingOrderRecoveryLifecycle.PREPARED:
            raise ValueError("pending-order UPSERT lifecycle must be PREPARED")
        return operation, PendingOrderRecoveryRecord(
            order=_pending_order_from_json_object(
                decoded_record["order"],
                legacy_policy_version=(
                    schema_version < _PENDING_ORDER_POLICY_SCHEMA_VERSION
                ),
                legacy_exit_pct_b=(
                    schema_version < _PENDING_ORDER_SCHEMA_VERSION
                ),
            ),
            lifecycle=lifecycle,
            submission_provenance=(
                PendingOrderSubmissionProvenance.SUBMITTED_FSYNC_PRECEDES_REST_POST
                if schema_version >= _PENDING_ORDER_POLICY_SCHEMA_VERSION
                else PendingOrderSubmissionProvenance.LEGACY_PREPARED_AMBIGUOUS
            ),
        )
    if operation == _PENDING_ORDER_TRANSITION:
        if schema_version == _PENDING_ORDER_LEGACY_SCHEMA_VERSION:
            raise ValueError(
                "pending-order TRANSITION requires lifecycle schema"
            )
        _require_exact_pending_keys(
            decoded_record,
            _PENDING_ORDER_TRANSITION_KEYS,
            "pending-order TRANSITION event",
        )
        lifecycle = PendingOrderRecoveryLifecycle(
            decoded_record["lifecycle"]
        )
        if lifecycle is PendingOrderRecoveryLifecycle.PREPARED:
            raise ValueError(
                "pending-order TRANSITION must advance from PREPARED"
            )
        if (
            schema_version == _PENDING_ORDER_LIFECYCLE_SCHEMA_VERSION
            and lifecycle
            is not PendingOrderRecoveryLifecycle.SUBMISSION_REJECTED_CONFIRMED
        ):
            raise ValueError(
                "pending-order schema v2 supports only confirmed rejection"
            )
        return operation, (
            _normalize_client_order_id(decoded_record["client_order_id"]),
            lifecycle,
        )
    if operation == _PENDING_ORDER_REMOVE:
        _require_exact_pending_keys(
            decoded_record,
            _PENDING_ORDER_REMOVE_KEYS,
            "pending-order REMOVE event",
        )
        return operation, _normalize_client_order_id(
            decoded_record["client_order_id"]
        )

    raise ValueError("pending-order operation is unsupported")  # 알 수 없는 event는 skip하지 않는다.


class TradeHistoryRepository:
    """
    클래스 이름: TradeHistoryRepository
    기능: local trade JSONL v1/v2/v3와 pending sidecar v1~v4의 streaming 복구 및 durable idempotent append를 관리한다.
    작성 날짜: 2026/08/29
    """

    __slots__ = (
        "_clock",
        "_index_loaded",
        "_lock",
        "_manual_kill_control_loaded",
        "_manual_kill_control_replay",
        "_manual_kill_control_state",
        "_manual_kill_control_storage_path",
        "_ordered_order_ids",
        "_pending_order_storage_path",
        "_pending_client_ids_by_intent_attempt",
        "_pending_order_lifecycles_by_client_id",
        "_pending_orders_by_client_id",
        "_pending_orders_loaded",
        "_pending_exit_pct_b_by_intent",
        "_pending_risk_policy_versions_by_intent",
        "_pending_submission_provenance_by_client_id",
        "_pending_submission_counts_by_intent",
        "_storage_path",
        "_trades_by_order_id",
        "_uncertain_order_ids",
    )

    def __init__(
        self,
        storage_path: str | os.PathLike[str],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: JSONL storage 경로와 recovery backup용 UTC clock을 보존한다.
        인자: storage_path -> 거래 이력 JSONL 파일 경로
            clock -> corrupt backup timestamp를 제공할 optional UTC callable
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # Path 변환과 clock callable을 내부 mutable 상태를 만들기 전에 검증한다.
        try:
            normalized_path = Path(storage_path)
        except TypeError as error:
            raise TypeError("storage_path must be path-like") from error
        if normalized_path.name in {"", ".", ".."}:
            raise ValueError("storage_path must identify a file")

        selected_clock = _utc_now if clock is None else clock
        if not callable(selected_clock):
            raise TypeError("clock must be callable")

        # disk index는 최초 load/save 시점에 만들고 생성자에서는 빈 비공개 상태로 둔다.
        self._storage_path = normalized_path
        self._pending_order_storage_path = normalized_path.with_name(
            f"{normalized_path.name}.pending-orders.jsonl"
        )
        self._manual_kill_control_storage_path = normalized_path.with_name(
            f"{normalized_path.name}.manual-kill-control.jsonl"
        )
        self._clock = selected_clock
        self._lock = RLock()
        self._trades_by_order_id: dict[str, Trade] = {}
        self._ordered_order_ids: list[str] = []
        self._index_loaded = False  # 첫 save도 기존 파일 index를 먼저 확인하게 한다.
        self._uncertain_order_ids: set[str] = set()
        self._pending_orders_by_client_id: dict[str, Order] = {}
        self._pending_order_lifecycles_by_client_id: dict[
            str,
            PendingOrderRecoveryLifecycle,
        ] = {}
        self._pending_submission_provenance_by_client_id: dict[
            str,
            PendingOrderSubmissionProvenance,
        ] = {}
        self._pending_exit_pct_b_by_intent: dict[
            str,
            Decimal | None,
        ] = {}
        self._pending_risk_policy_versions_by_intent: dict[
            str,
            int | None,
        ] = {}
        self._pending_submission_counts_by_intent: dict[str, int] = {}
        self._pending_client_ids_by_intent_attempt: dict[
            tuple[str, int],
            str,
        ] = {}
        self._pending_orders_loaded = False  # 첫 mutation도 disk event 전체를 먼저 replay한다.
        self._manual_kill_control_replay: tuple[
            ManualKillControlState,
            ...,
        ] = ()
        self._manual_kill_control_state = ManualKillControlState()
        self._manual_kill_control_loaded = False  # 첫 command 전에 disk journal을 항상 replay한다.

    @property
    def storage_path(self) -> Path:
        """
        함수 이름: storage_path()
        기능: repository가 읽는 JSONL 파일 경로를 반환한다.
        인자: 없음
        반환값: 거래 이력 Path
        작성 날짜: 2026/08/22
        """
        return self._storage_path  # immutable Path identity만 공개한다.

    @property
    def loaded_order_ids(self) -> frozenset[str]:
        """
        함수 이름: loaded_order_ids()
        기능: 마지막 성공 startup load에서 재구성한 order ID 집합을 반환한다.
        인자: 없음
        반환값: 불변 order ID 집합
        작성 날짜: 2026/08/21
        """
        with self._lock:
            return frozenset(
                self._trades_by_order_id
            )  # caller가 repository index를 변경하지 못하게 snapshot을 만든다.

    @property
    def pending_order_storage_path(self) -> Path:
        """
        함수 이름: pending_order_storage_path()
        기능: 기존 history와 분리된 재시작 주문 sidecar 경로를 반환한다.
        인자: 없음
        반환값: pending-order JSONL Path
        작성 날짜: 2026/08/22
        """
        return self._pending_order_storage_path  # 파생 Path identity만 공개하고 파일 내용은 감춘다.

    @property
    def manual_kill_control_storage_path(self) -> Path:
        """
        함수 이름: manual_kill_control_storage_path()
        기능: manual kill 재시작 상태를 보존하는 별도 JSONL 경로를 반환한다.
        인자: 없음
        반환값: manual kill control journal Path
        작성 날짜: 2026/08/29
        """
        return self._manual_kill_control_storage_path  # 비밀이 없는 파생 경로 identity만 공개한다.

    @property
    def supports_manual_kill_control_recovery(self) -> bool:
        """
        함수 이름: supports_manual_kill_control_recovery()
        기능: concrete repository가 manual kill durable 복구 capability를 제공함을 알린다.
        인자: 없음
        반환값: 항상 True
        작성 날짜: 2026/08/29
        """
        return True  # 실제 fsync journal을 소유한 adapter만 capability를 선언한다.

    @property
    def supports_pending_order_recovery(self) -> bool:
        """
        함수 이름: supports_pending_order_recovery()
        기능: concrete repository가 durable pending-order operation을 제공함을 알린다.
        인자: 없음
        반환값: 항상 True
        작성 날짜: 2026/08/22
        """
        return True  # 실제 sidecar 구현이 있는 adapter만 capability를 선언한다.

    def get_manual_kill_control_state(self) -> ManualKillControlState:
        """
        함수 이름: get_manual_kill_control_state()
        기능: manual kill journal 전체를 strict replay해 마지막 durable state를 반환한다.
        인자: 없음
        반환값: journal이 없으면 inactive version 0, 있으면 마지막 canonical state
        작성 날짜: 2026/08/29
        """
        with self._lock:
            previous_loaded = self._manual_kill_control_loaded
            previous_replay = self._manual_kill_control_replay
            previous_state = self._manual_kill_control_state

            # 같은 descriptor에서 strict replay와 fsync를 끝내 path 교체·truncate를 inactive로 완화하지 않는다.
            open_flags = (
                os.O_RDWR
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                control_descriptor = os.open(
                    self._manual_kill_control_storage_path,
                    open_flags,
                )
            except FileNotFoundError:
                if (
                    previous_state != ManualKillControlState()
                    or previous_replay
                ):
                    self._manual_kill_control_loaded = False
                    raise ManualKillControlJournalCorruptedError(
                        1,
                        "MissingJournal",
                    )
                self._manual_kill_control_replay = ()
                self._manual_kill_control_state = ManualKillControlState()
                self._manual_kill_control_loaded = True
                return self._manual_kill_control_state  # 파일 부재만 명시적 초기 상태로 해석한다.

            # 완전한 journal replay와 durability 확인이 모두 끝난 뒤에만 in-memory state를 게시한다.
            try:
                with os.fdopen(
                    control_descriptor,
                    "r+b",
                    buffering=0,
                ) as control_file:
                    self._validate_manual_kill_control_descriptor(
                        control_file.fileno()
                    )
                    replay_stat = os.fstat(control_file.fileno())
                    (
                        loaded_state,
                        loaded_replay,
                    ) = self._read_manual_kill_control_file(
                        control_file
                    )
                    os.fsync(control_file.fileno())
                    verified_stat = os.fstat(control_file.fileno())
                    if (
                        replay_stat.st_size != verified_stat.st_size
                        or replay_stat.st_mtime_ns
                        != verified_stat.st_mtime_ns
                        or replay_stat.st_ctime_ns
                        != verified_stat.st_ctime_ns
                    ):
                        raise ManualKillControlJournalCorruptedError(
                            1,
                            "ConcurrentJournalMutation",
                        )
                    self._validate_manual_kill_control_descriptor(
                        control_file.fileno()
                    )
                _fsync_parent_directory(
                    self._manual_kill_control_storage_path
                )
                if previous_loaded and (
                    loaded_state != previous_state
                    or loaded_replay != previous_replay
                ):
                    raise ManualKillControlJournalCorruptedError(
                        1,
                        "JournalDrift",
                    )
            except Exception:
                self._manual_kill_control_loaded = False
                raise

            self._manual_kill_control_replay = loaded_replay
            self._manual_kill_control_state = loaded_state
            self._manual_kill_control_loaded = True
            return loaded_state  # frozen state라 caller가 repository replay 결과를 변경할 수 없다.

    def get_manual_kill_control_replay(
        self,
    ) -> tuple[ManualKillControlState, ...]:
        """
        함수 이름: get_manual_kill_control_replay()
        기능: restart command cache에 복원할 bounded manual kill receipt를 순서대로 반환한다.
        인자: 없음
        반환값: 마지막 1,024개 이하의 검증된 command receipt tuple
        작성 날짜: 2026/08/29
        """
        with self._lock:
            self.get_manual_kill_control_state()  # 같은 lock의 strict 전체 replay를 먼저 완료한다.
            return self._manual_kill_control_replay  # tuple이라 caller가 repository cache를 바꾸지 못한다.

    def save_manual_kill_control_state(
        self,
        state: ManualKillControlState,
    ) -> None:
        """
        함수 이름: save_manual_kill_control_state()
        기능: manual kill 성공 command receipt와 결과 control version을 append·fsync한다.
        인자: state -> 현재 durable state에서 실행된 exact 다음 command 결과
        반환값: file과 directory fsync가 끝나면 없음
        작성 날짜: 2026/08/29
        """
        # Domain value가 아닌 tuple이나 mapping을 persistence 경계에서 암묵 변환하지 않는다.
        if not isinstance(state, ManualKillControlState):
            raise TypeError("state must be a ManualKillControlState")

        with self._lock:
            self.get_manual_kill_control_state()  # 매 append 직전 disk와 cache를 strict 재결합한다.
            current_state = self._manual_kill_control_state
            existing_receipt = next(
                (
                    receipt
                    for receipt in self._manual_kill_control_replay
                    if receipt.command_id == state.command_id
                ),
                None,
            )
            if existing_receipt is not None:
                if existing_receipt != state:
                    raise ValueError(
                        "manual-kill command ID cannot be reused"
                    )
                return  # 직전 strict replay/fsync가 same receipt의 durability도 다시 확정했다.
            if state.command_id is None or state.expected_version is None:
                raise ValueError(
                    "manual-kill durable receipt requires command provenance"
                )
            if state.expected_version != current_state.version:
                raise ValueError(
                    "manual-kill expected version must match durable state"
                )
            expected_result_version = current_state.version + int(
                state.active is not current_state.active
            )
            if state.version != expected_result_version:
                raise ValueError(
                    "manual-kill result version does not match command outcome"
                )
            if (
                current_state.active
                and state.active
                and (
                    state.behavior != current_state.behavior
                    or state.policy_version != current_state.policy_version
                )
            ):
                raise ValueError(
                    "active manual-kill no-op cannot change policy provenance"
                )

            self._append_manual_kill_control_state(state)
            self._manual_kill_control_replay = (
                *self._manual_kill_control_replay,
                state,
            )[-_MANUAL_KILL_CONTROL_REPLAY_LIMIT:]
            self._manual_kill_control_state = state  # fsync 성공 뒤에만 새 control state를 게시한다.
            self._manual_kill_control_loaded = True

    def save_pending_order(self, order: Order) -> None:
        """
        함수 이름: save_pending_order()
        기능: 외부 제출 전 Order metadata와 v4 제출 경계를 sidecar에 durable append한다.
        인자: order -> 제출 직전 intent와 client ID를 가진 canonical Order
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # credential이나 raw response를 구조적으로 가질 수 없는 Order만 public 경계에서 받는다.
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")
        order_snapshot = _pending_order_from_json_object(
            _pending_order_to_json_object(order)
        )  # caller가 이후 체결 상태를 mutate해도 journal index에는 제출 전 metadata만 남긴다.

        with self._lock:
            # 독립 process가 남긴 event를 먼저 replay한 뒤 현재 active key와 비교한다.
            if not self._pending_orders_loaded:
                self.get_pending_orders()
            existing_order = self._pending_orders_by_client_id.get(
                order_snapshot.client_order_id
            )
            if existing_order == order_snapshot:
                return  # 동일 UPSERT 재시도는 sidecar를 불필요하게 늘리지 않는다.
            if existing_order is not None:
                raise PendingOrderJournalConflictError(
                    order_snapshot.client_order_id
                )  # 동일 idempotency key가 다른 제출 intent를 덮어쓰지 못하게 한다.

            # REMOVE된 attempt를 동일 client ID로 다시 활성화하거나 active intent를 겹쳐 제출하지 못하게 한다.
            intent_attempt = (
                order_snapshot.intent_id,
                order_snapshot.submission_attempt,
            )
            historical_client_order_id = (
                self._pending_client_ids_by_intent_attempt.get(
                    intent_attempt
                )
            )
            if historical_client_order_id is not None:
                raise PendingOrderJournalConflictError(
                    order_snapshot.client_order_id
                )
            if any(
                active_order.intent_id == order_snapshot.intent_id
                for active_order in self._pending_orders_by_client_id.values()
            ):
                raise PendingOrderJournalConflictError(
                    order_snapshot.client_order_id
                )

            # 이 intent가 이미 남긴 journal이 있으면 바로 다음 attempt만 소비한다.
            durable_submission_count = (
                self._pending_submission_counts_by_intent.get(
                    order_snapshot.intent_id
                )
            )
            if (
                durable_submission_count is not None
                and order_snapshot.submission_attempt
                != durable_submission_count
            ):
                raise PendingOrderJournalConflictError(
                    order_snapshot.client_order_id
                )

            # REMOVE·restart 후 attempt도 최초 intent의 exact risk policy version을 계속 사용한다.
            if (
                order_snapshot.intent_id
                in self._pending_risk_policy_versions_by_intent
                and self._pending_risk_policy_versions_by_intent[
                    order_snapshot.intent_id
                ]
                != order_snapshot.risk_policy_version
            ):
                raise PendingOrderJournalConflictError(
                    order_snapshot.client_order_id
                )

            # REMOVE·restart 후 Case C retry도 최초 SELL 의도 %B를 바꿔 쓰지 못한다.
            if (
                order_snapshot.intent_id
                in self._pending_exit_pct_b_by_intent
                and self._pending_exit_pct_b_by_intent[
                    order_snapshot.intent_id
                ]
                != order_snapshot.exit_pct_b_at_intent
            ):
                raise PendingOrderJournalConflictError(
                    order_snapshot.client_order_id
                )

            self._append_pending_order_event(
                _build_pending_upsert_event(order_snapshot)
            )
            self._pending_orders_by_client_id[
                order_snapshot.client_order_id
            ] = order_snapshot  # fsync 성공 뒤에만 caller와 분리한 제출 전 관점을 index에 게시한다.
            self._pending_order_lifecycles_by_client_id[
                order_snapshot.client_order_id
            ] = PendingOrderRecoveryLifecycle.PREPARED
            self._pending_submission_provenance_by_client_id[
                order_snapshot.client_order_id
            ] = (
                PendingOrderSubmissionProvenance.SUBMITTED_FSYNC_PRECEDES_REST_POST
            )  # Schema v3 이상 UPSERT는 SUBMITTED fsync 전에 REST POST가 불가능한 writer 계약을 증명한다.
            self._pending_submission_counts_by_intent[
                order_snapshot.intent_id
            ] = order_snapshot.submission_attempt + 1
            self._pending_risk_policy_versions_by_intent.setdefault(
                order_snapshot.intent_id,
                order_snapshot.risk_policy_version,
            )  # 첫 UPSERT의 None도 legacy provenance로 보존해 후속 attempt가 새 version을 추측하지 못한다.
            self._pending_exit_pct_b_by_intent.setdefault(
                order_snapshot.intent_id,
                order_snapshot.exit_pct_b_at_intent,
            )  # 첫 UPSERT의 None도 legacy 미상 provenance로 보존해 retry가 시장값을 추측하지 못한다.
            self._pending_client_ids_by_intent_attempt[
                intent_attempt
            ] = order_snapshot.client_order_id

    def transition_pending_order_lifecycle(
        self,
        client_order_id: str,
        lifecycle: PendingOrderRecoveryLifecycle,
    ) -> None:
        """
        함수 이름: transition_pending_order_lifecycle()
        기능: active sidecar 주문의 lifecycle을 멱등 또는 허용된 단조 상태로 fsync한다.
        인자: client_order_id -> 전이할 application order ID
            lifecycle -> 남길 다음 durable lifecycle
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        normalized_client_order_id = _normalize_client_order_id(client_order_id)
        if not isinstance(lifecycle, PendingOrderRecoveryLifecycle):
            raise TypeError("lifecycle must be a PendingOrderRecoveryLifecycle")
        if lifecycle is PendingOrderRecoveryLifecycle.PREPARED:
            raise ValueError("pending-order transition must advance from PREPARED")

        with self._lock:
            # TRANSITION은 반드시 먼저 fsync된 active Order metadata 하나를 대상으로 한다.
            if not self._pending_orders_loaded:
                self.get_pending_orders()
            if normalized_client_order_id not in self._pending_orders_by_client_id:
                raise ValueError(
                    "pending-order transition requires an active pending order"
                )
            current_lifecycle = self._pending_order_lifecycles_by_client_id[
                normalized_client_order_id
            ]
            if current_lifecycle is lifecycle:
                return  # fsync 반환 후 같은 사실을 재관찰해도 journal을 늘리지 않는다.
            if not _pending_lifecycle_can_transition(
                current_lifecycle,
                lifecycle,
            ):
                raise ValueError("pending-order lifecycle transition is invalid")

            self._append_pending_order_event(
                _build_pending_transition_event(
                    normalized_client_order_id,
                    lifecycle,
                )
            )
            self._pending_order_lifecycles_by_client_id[
                normalized_client_order_id
            ] = lifecycle  # file·directory fsync 후에만 복구 snapshot을 전진시킨다.

    def mark_pending_order_submission_rejected(
        self,
        client_order_id: str,
    ) -> None:
        """
        함수 이름: mark_pending_order_submission_rejected()
        기능: 공식 pre-matching 제출 거부 사실을 active sidecar lifecycle에 durable 기록한다.
        인자: client_order_id -> 거부 응답과 같은 application order ID
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        # 기존 application port는 generic transition의 typed 호환 wrapper로 유지한다.
        self.transition_pending_order_lifecycle(
            client_order_id,
            PendingOrderRecoveryLifecycle.SUBMISSION_REJECTED_CONFIRMED,
        )

    def delete_pending_order(self, client_order_id: str) -> None:
        """
        함수 이름: delete_pending_order()
        기능: terminal 처리한 client order ID의 REMOVE tombstone을 durable append한다.
        인자: client_order_id -> 제거할 active pending order key
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # sidecar 조회 전에 caller key를 canonical Order identity 규칙으로 검증한다.
        normalized_client_order_id = _normalize_client_order_id(client_order_id)
        with self._lock:
            if not self._pending_orders_loaded:
                self.get_pending_orders()
            if normalized_client_order_id not in self._pending_orders_by_client_id:
                return  # 이미 제거됐거나 저장되지 않은 ID의 REMOVE는 멱등 no-op이다.

            self._append_pending_order_event(
                _build_pending_remove_event(normalized_client_order_id)
            )
            del self._pending_orders_by_client_id[normalized_client_order_id]
            del self._pending_order_lifecycles_by_client_id[
                normalized_client_order_id
            ]
            del self._pending_submission_provenance_by_client_id[
                normalized_client_order_id
            ]

    def get_pending_orders(self) -> tuple[Order, ...]:
        """
        함수 이름: get_pending_orders()
        기능: sidecar event 전체를 replay해 startup active Order tuple을 재생성한다.
        인자: 없음
        반환값: event 순서와 마지막 active 상태를 반영한 immutable Order tuple
        작성 날짜: 2026/08/22
        """
        with self._lock:
            # sidecar가 없으면 history 존재 여부와 무관한 빈 pending 상태로 초기화한다.
            try:
                pending_order_file = self._pending_order_storage_path.open("rb")
            except FileNotFoundError:
                self._pending_orders_by_client_id = {}
                self._pending_order_lifecycles_by_client_id = {}
                self._pending_submission_provenance_by_client_id = {}
                self._pending_exit_pct_b_by_intent = {}
                self._pending_risk_policy_versions_by_intent = {}
                self._pending_submission_counts_by_intent = {}
                self._pending_client_ids_by_intent_attempt = {}
                self._pending_orders_loaded = True
                return ()  # 첫 startup은 기존 history JSONL을 pending source로 사용하지 않는다.

            # 모든 event가 검증되기 전에는 이전 active index를 공개하지 않는다.
            try:
                with pending_order_file:
                    (
                        loaded_orders,
                        loaded_lifecycles,
                        loaded_submission_provenance,
                        loaded_submission_counts,
                        loaded_client_ids_by_intent_attempt,
                        loaded_risk_policy_versions,
                        loaded_exit_pct_b_by_intent,
                    ) = self._read_pending_order_file(pending_order_file)
                self._confirm_pending_order_storage_durability()
            except Exception:
                self._pending_orders_loaded = False
                raise

            self._pending_orders_by_client_id = loaded_orders
            self._pending_order_lifecycles_by_client_id = loaded_lifecycles
            self._pending_submission_provenance_by_client_id = (
                loaded_submission_provenance
            )
            self._pending_submission_counts_by_intent = (
                loaded_submission_counts
            )
            self._pending_risk_policy_versions_by_intent = (
                loaded_risk_policy_versions
            )
            self._pending_exit_pct_b_by_intent = (
                loaded_exit_pct_b_by_intent
            )
            self._pending_client_ids_by_intent_attempt = (
                loaded_client_ids_by_intent_attempt
            )
            self._pending_orders_loaded = True
            return tuple(loaded_orders.values())  # caller가 내부 replay index를 변경하지 못하게 한다.

    def get_pending_order_recovery_records(
        self,
    ) -> tuple[PendingOrderRecoveryRecord, ...]:
        """
        함수 이름: get_pending_order_recovery_records()
        기능: active Order, durable lifecycle과 제출 경계를 같은 replay snapshot으로 반환한다.
        인자: 없음
        반환값: sidecar 순서를 보존한 immutable recovery record tuple
        작성 날짜: 2026/08/29
        """
        with self._lock:
            # 재시작 검증과 다른 process의 durable tombstone도 관찰하도록 매번 disk를 replay한다.
            self.get_pending_orders()

            # 세 index는 같은 replay에서 만들어져 Order·lifecycle·제출 경계가 일치한다.
            return tuple(
                PendingOrderRecoveryRecord(
                    order=order,
                    lifecycle=self._pending_order_lifecycles_by_client_id[
                        client_order_id
                    ],
                    submission_provenance=(
                        self._pending_submission_provenance_by_client_id[
                            client_order_id
                        ]
                    ),
                )
                for client_order_id, order in (
                    self._pending_orders_by_client_id.items()
                )
            )

    def get_pending_order_submission_counts(
        self,
    ) -> tuple[tuple[str, int], ...]:
        """
        함수 이름: get_pending_order_submission_counts()
        기능: REMOVE된 attempt를 포함해 intent별 durable 제출 소비 횟수를 replay한다.
        인자: 없음
        반환값: 첫 UPSERT 순서를 보존한 intent·submission count tuple
        작성 날짜: 2026/08/25
        """
        with self._lock:
            # 다른 process의 최신 UPSERT·REMOVE를 모두 반영하도록 매번 disk를 replay한다.
            self.get_pending_orders()

            return tuple(
                self._pending_submission_counts_by_intent.items()
            )  # 내부 dictionary를 노출하지 않고 immutable snapshot으로 반환한다.

    def get_trade_history(self) -> tuple[Trade, ...]:
        """
        함수 이름: get_trade_history()
        기능: JSONL을 line 단위로 읽어 Trade tuple과 order ID index를 원자적으로 복원한다.
        인자: 없음
        반환값: 파일 순서를 보존하고 동일 내용 중복을 제거한 Trade tuple
        작성 날짜: 2026/08/21
        """
        with self._lock:
            # 파일이 없으면 빈 durable history로 취급하고 이후 save를 위한 index를 확정한다.
            try:
                history_file = self._storage_path.open("rb")
            except FileNotFoundError:
                self._trades_by_order_id = {}
                self._ordered_order_ids = []
                self._index_loaded = True
                return ()  # 존재하지 않는 첫 startup은 손상이나 복구 대상이 아니다.

            # 완전한 load가 끝나기 전에는 이전 공개 index를 유지한다.
            try:
                with history_file:
                    loaded_index, loaded_order_ids = self._read_history_file(
                        history_file
                    )
                self._confirm_storage_durability()
            except Exception:
                self._index_loaded = False
                raise

            # streaming parse가 완전히 성공한 뒤에만 이전 공개 index를 한 번에 교체한다.
            self._trades_by_order_id = loaded_index
            self._ordered_order_ids = loaded_order_ids
            self._index_loaded = True
            return tuple(
                loaded_index.values()
            )  # dedup index의 insertion order로 immutable file 순서를 반환한다.

    def stream_trades(self, query: TradeHistoryQuery) -> Iterator[Trade]:
        """
        함수 이름: stream_trades()
        기능: JSONL byte snapshot을 line 단위로 읽어 KST 날짜와 side가 맞는 Trade를 지연 반환한다.
        인자: query -> KST 양끝 포함 날짜 범위와 거래 방향 조건
        반환값: 파일 순서를 보존하며 전체 Trade collection을 복제하지 않는 iterator
        작성 날짜: 2026/08/23
        """
        # 문자열이나 유사 객체가 query 불변식을 우회하지 못하게 파일 접근 전에 검증한다.
        if not isinstance(query, TradeHistoryQuery):
            raise TypeError("query must be a TradeHistoryQuery")

        with self._lock:
            # 같은 process가 소유한 durable index와 JSONL descriptor를 하나의 lock 경계에서 고정한다.
            try:
                history_file = self._storage_path.open("rb")
            except FileNotFoundError:
                self._trades_by_order_id = {}
                self._ordered_order_ids = []
                self._index_loaded = True
                return iter(())  # 파일 없음은 close할 descriptor가 없는 빈 durable stream이다.

            try:
                # startup 또는 불확실 append 뒤에만 index를 재생성해 정상 export의 전체-history 복제를 피한다.
                if not self._index_loaded:
                    loaded_index, loaded_order_ids = self._read_history_file(
                        history_file
                    )
                    self._confirm_storage_durability()
                    self._trades_by_order_id = loaded_index
                    self._ordered_order_ids = loaded_order_ids
                    self._index_loaded = True

                history_file.seek(0)
                snapshot_length = os.fstat(
                    history_file.fileno()
                ).st_size  # 같은 descriptor의 현재 byte 경계를 lock 아래에서 고정한다.
                snapshot_index = self._trades_by_order_id
                ordered_order_ids = self._ordered_order_ids
                snapshot_order_count = len(ordered_order_ids)
                history_file.close()  # index replay용 descriptor는 iterator를 반환하기 전에 닫는다.
            except Exception:
                history_file.close()
                self._index_loaded = False
                raise

        return self._stream_history_snapshot(
            snapshot_length,
            query,
            snapshot_index,
            ordered_order_ids,
            snapshot_order_count,
        )  # iterator가 EOF·오류·close에서 열린 descriptor를 정리한다.

    def save_this_trade_by_order_id(
        self,
        order_id: int | str,
        trade: Trade,
    ) -> None:
        """
        함수 이름: save_this_trade_by_order_id()
        기능: order ID idempotency를 확인하고 canonical UTF-8 JSON line을 durable append한다.
        인자: order_id -> exchange order ID인 양의 정수 또는 canonical 문자열
            trade -> 같은 order ID의 terminal Trade
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # lock을 잡기 전에 호출 인자와 Trade identity의 정확한 일치를 검증한다.
        normalized_order_id = _normalize_order_id(order_id)
        if not isinstance(trade, Trade):
            raise TypeError("trade must be a Trade")
        if normalized_order_id != trade.order_id:
            raise ValueError("order_id must match trade.order_id")

        with self._lock:
            # 새 Repository instance의 첫 append도 disk의 기존 order index를 조회한다.
            if not self._index_loaded:
                self.get_trade_history()
            existing_trade = self._trades_by_order_id.get(normalized_order_id)
            if existing_trade is not None:
                if existing_trade == trade:
                    if normalized_order_id in self._uncertain_order_ids:
                        self._confirm_uncertain_append(normalized_order_id)
                    return  # durable 동일 내용은 중복 line을 추가하지 않는다.
                raise OrderHistoryConflictError(
                    f"order_id {normalized_order_id} has conflicting trade content"
                )

            # canonical object 하나와 LF를 단일 bytes write 대상으로 준비한다.
            encoded_record = json.dumps(
                trade_to_json_object(trade),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
            append_payload = self._build_append_payload(encoded_record)
            try:
                with self._storage_path.open("ab") as history_file:
                    written_length = history_file.write(append_payload)
                    if written_length != len(append_payload):
                        raise OSError("history append wrote an incomplete record")
                    history_file.flush()  # 사용자 공간과 OS buffer 사이의 내용을 내린다.
                    os.fsync(history_file.fileno())
            except Exception:
                # write 여부가 불명인 모든 실패는 disk 재조회와 same-line fsync 대상으로 남긴다.
                self._uncertain_order_ids.add(normalized_order_id)
                self._index_loaded = False
                raise

            self._trades_by_order_id[normalized_order_id] = trade  # fsync 성공 뒤에만 index를 게시한다.
            self._ordered_order_ids.append(
                normalized_order_id
            )  # durable append와 동일한 file order를 streaming dedup 기준에 추가한다.
            self._uncertain_order_ids.discard(normalized_order_id)

    def _append_manual_kill_control_state(
        self,
        state: ManualKillControlState,
    ) -> None:
        """
        함수 이름: _append_manual_kill_control_state()
        기능: canonical manual kill state 한 줄을 write·file fsync·directory fsync한다.
        인자: state -> 이전 durable state에서 실행된 toggle 또는 no-op command receipt
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # 비밀 필드가 없는 exact schema를 한 번의 binary write 대상으로 먼저 완성한다.
        encoded_record = json.dumps(
            _manual_kill_control_to_json_object(state),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        open_flags = (
            os.O_RDWR
            | os.O_APPEND
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        created_file = False
        try:
            try:
                control_descriptor = os.open(
                    self._manual_kill_control_storage_path,
                    open_flags,
                )
            except FileNotFoundError:
                if (
                    self._manual_kill_control_state
                    != ManualKillControlState()
                    or self._manual_kill_control_replay
                ):
                    raise ManualKillControlJournalCorruptedError(
                        1,
                        "MissingJournal",
                    )
                control_descriptor = os.open(
                    self._manual_kill_control_storage_path,
                    open_flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                created_file = True

            # 검증한 inode와 실제 append·post-write replay가 같은 descriptor를 공유한다.
            with os.fdopen(
                control_descriptor,
                "r+b",
                buffering=0,
            ) as control_file:
                self._validate_manual_kill_control_descriptor(
                    control_file.fileno()
                )
                replay_stat = os.fstat(control_file.fileno())
                if created_file:
                    loaded_state = ManualKillControlState()
                    loaded_replay: tuple[
                        ManualKillControlState,
                        ...,
                    ] = ()
                else:
                    control_file.seek(0)
                    (
                        loaded_state,
                        loaded_replay,
                    ) = self._read_manual_kill_control_file(control_file)
                if (
                    loaded_state != self._manual_kill_control_state
                    or loaded_replay != self._manual_kill_control_replay
                ):
                    raise ManualKillControlJournalCorruptedError(
                        1,
                        "JournalDrift",
                    )

                # O_APPEND descriptor에 한 record를 쓰고 같은 inode를 즉시 strict replay한다.
                pre_write_stat = os.fstat(control_file.fileno())
                if (
                    replay_stat.st_size != pre_write_stat.st_size
                    or replay_stat.st_mtime_ns != pre_write_stat.st_mtime_ns
                    or replay_stat.st_ctime_ns != pre_write_stat.st_ctime_ns
                ):
                    raise ManualKillControlJournalCorruptedError(
                        1,
                        "ConcurrentJournalMutation",
                    )
                written_length = control_file.write(encoded_record)
                if written_length != len(encoded_record):
                    raise OSError(
                        "manual-kill control append wrote an incomplete record"
                    )
                control_file.flush()  # 사용자 공간 buffer를 OS write 경계까지 내린다.
                os.fsync(control_file.fileno())
                control_file.seek(0)
                (
                    verified_state,
                    verified_replay,
                ) = self._read_manual_kill_control_file(control_file)
                expected_replay = (
                    *self._manual_kill_control_replay,
                    state,
                )[-_MANUAL_KILL_CONTROL_REPLAY_LIMIT:]
                post_write_stat = os.fstat(control_file.fileno())
                if (
                    post_write_stat.st_size
                    != pre_write_stat.st_size + len(encoded_record)
                    or verified_state != state
                    or verified_replay != expected_replay
                ):
                    raise ManualKillControlJournalCorruptedError(
                        1,
                        "PostWriteReplayMismatch",
                    )
                self._validate_manual_kill_control_descriptor(
                    control_file.fileno()
                )
            _fsync_parent_directory(self._manual_kill_control_storage_path)
        except Exception:
            self._manual_kill_control_loaded = False
            raise  # append 여부가 불명인 실패는 다음 호출에서 journal 전체 replay를 강제한다.

    def _validate_manual_kill_control_descriptor(
        self,
        control_descriptor: int,
    ) -> None:
        """
        함수 이름: _validate_manual_kill_control_descriptor()
        기능: 열린 manual-kill journal이 현재 path의 단일 regular inode인지 검증한다.
        인자: control_descriptor -> strict replay 또는 append에 사용하는 열린 file descriptor
        반환값: 안전한 동일 inode면 없음
        작성 날짜: 2026/08/29
        """
        # Symlink·hardlink·path 교체가 검증한 bytes와 append 대상을 분리하지 못하게 identity를 묶는다.
        descriptor_stat = os.fstat(control_descriptor)
        try:
            path_stat = os.stat(
                self._manual_kill_control_storage_path,
                follow_symlinks=False,
            )
        except OSError as error:
            raise ManualKillControlJournalCorruptedError(
                1,
                "JournalIdentityUnavailable",
            ) from error
        if (
            not stat.S_ISREG(descriptor_stat.st_mode)
            or not stat.S_ISREG(path_stat.st_mode)
            or descriptor_stat.st_nlink != 1
            or path_stat.st_nlink != 1
            or descriptor_stat.st_dev != path_stat.st_dev
            or descriptor_stat.st_ino != path_stat.st_ino
        ):
            raise ManualKillControlJournalCorruptedError(
                1,
                "JournalIdentityMismatch",
            )

    def _confirm_manual_kill_control_storage_durability(self) -> None:
        """
        함수 이름: _confirm_manual_kill_control_storage_durability()
        기능: 현재 보이는 manual kill journal와 directory entry를 다시 fsync한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # 같은 strict replay 경계를 재사용해 fsync 전에 path 교체·truncate도 함께 탐지한다.
        self.get_manual_kill_control_state()

    def _read_manual_kill_control_file(
        self,
        control_file: BinaryIO,
    ) -> tuple[
        ManualKillControlState,
        tuple[ManualKillControlState, ...],
    ]:
        """
        함수 이름: _read_manual_kill_control_file()
        기능: manual kill JSONL을 strict replay하고 schema별 command/version 계약을 검증한다.
        인자: control_file -> binary read로 열린 control journal
        반환값: 마지막 canonical state와 bounded command receipt tuple
        작성 날짜: 2026/08/29
        """
        # 공개 state와 분리된 local 값에 모든 line을 적용한 뒤 한 번에 반환한다.
        loaded_state = ManualKillControlState()
        replay_records: list[ManualKillControlState] = []
        line_number = 0
        while True:
            raw_line = control_file.readline()
            if raw_line == b"":
                break
            line_number += 1

            # Crash 중간의 partial tail도 kill 해제로 오인하지 않고 typed corruption으로 차단한다.
            if not raw_line.endswith(b"\n") or raw_line.endswith(b"\r\n"):
                raise ManualKillControlJournalCorruptedError(
                    line_number,
                    "InvalidJsonlFraming",
                )
            try:
                (
                    schema_version,
                    next_state,
                ) = _manual_kill_control_from_json_object(
                    _decode_json_line(raw_line[:-1]),
                )
                existing_receipt = next(
                    (
                        receipt
                        for receipt in replay_records
                        if receipt.command_id == next_state.command_id
                    ),
                    None,
                )
                if existing_receipt is not None:
                    if existing_receipt != next_state:
                        raise ValueError("command ID was reused")
                    continue  # 동일 receipt의 uncertain duplicate line은 state를 다시 전진시키지 않는다.
                if next_state.expected_version != loaded_state.version:
                    raise ValueError("expected version is not authoritative")
                if (
                    schema_version
                    == _MANUAL_KILL_CONTROL_LEGACY_SCHEMA_VERSION
                ):
                    if next_state.version != loaded_state.version + 1:
                        raise ValueError("legacy control version is not consecutive")
                    if next_state.active is loaded_state.active:
                        raise ValueError("legacy control state did not toggle")
                else:
                    expected_result_version = loaded_state.version + int(
                        next_state.active is not loaded_state.active
                    )
                    if next_state.version != expected_result_version:
                        raise ValueError(
                            "control result version does not match command outcome"
                        )
                    if (
                        loaded_state.active
                        and next_state.active
                        and (
                            next_state.behavior != loaded_state.behavior
                            or next_state.policy_version
                            != loaded_state.policy_version
                        )
                    ):
                        raise ValueError(
                            "active no-op changed policy provenance"
                        )
            except (
                _DuplicateJsonKeyError,
                _NonStandardJsonConstantError,
                TypeError,
                UnicodeDecodeError,
                ValueError,
                json.JSONDecodeError,
            ) as error:
                raise ManualKillControlJournalCorruptedError(
                    line_number,
                    type(error).__name__,
                ) from error
            loaded_state = next_state  # 검증된 line만 다음 version의 기준으로 전진시킨다.
            replay_records.append(next_state)
            if len(replay_records) > _MANUAL_KILL_CONTROL_REPLAY_LIMIT:
                replay_records.pop(0)  # Controller의 bounded command cache와 같은 최근 범위만 보존한다.

        if line_number == 0:
            raise ManualKillControlJournalCorruptedError(
                1,
                "EmptyJournal",
            )  # 존재하는 빈 파일은 이전 active state truncation일 수 있어 inactive로 완화하지 않는다.

        return loaded_state, tuple(replay_records)

    def _append_pending_order_event(
        self,
        event_record: Mapping[str, object],
    ) -> None:
        """
        함수 이름: _append_pending_order_event()
        기능: canonical sidecar event 한 줄을 write·file fsync·directory fsync한다.
        인자: event_record -> UPSERT 또는 REMOVE exact-schema object
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # 한 write 대상으로 canonical UTF-8 JSON과 LF를 먼저 완성한다.
        encoded_record = json.dumps(
            event_record,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        try:
            with self._pending_order_storage_path.open("ab") as pending_file:
                written_length = pending_file.write(encoded_record)
                if written_length != len(encoded_record):
                    raise OSError(
                        "pending-order append wrote an incomplete record"
                    )
                pending_file.flush()  # 사용자 공간 buffer를 OS write 경계까지 내린다.
                os.fsync(pending_file.fileno())
            _fsync_parent_directory(self._pending_order_storage_path)
        except Exception:
            self._pending_orders_loaded = False
            raise  # write 성공 여부가 불명인 실패는 다음 호출의 disk replay를 강제한다.

    def _confirm_pending_order_storage_durability(self) -> None:
        """
        함수 이름: _confirm_pending_order_storage_durability()
        기능: startup replay로 보이는 sidecar와 directory entry를 다시 durable sync한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # 이전 process가 append 후 반환 전에 실패했어도 같은 event를 중복 추가하지 않는다.
        with self._pending_order_storage_path.open("ab") as pending_file:
            pending_file.flush()
            os.fsync(pending_file.fileno())
        _fsync_parent_directory(
            self._pending_order_storage_path
        )  # file 이름까지 재부팅 이후 보존되도록 directory도 동기화한다.

    def _read_pending_order_file(
        self,
        pending_order_file: BinaryIO,
    ) -> tuple[
        dict[str, Order],
        dict[str, PendingOrderRecoveryLifecycle],
        dict[str, PendingOrderSubmissionProvenance],
        dict[str, int],
        dict[tuple[str, int], str],
        dict[str, int | None],
        dict[str, Decimal | None],
    ]:
        """
        함수 이름: _read_pending_order_file()
        기능: sidecar를 streaming 검증하고 active client-ID Order index를 replay한다.
        인자: pending_order_file -> 처음부터 읽을 open binary sidecar
        반환값: active Order·lifecycle·제출 경계와 durable intent 소비·attempt·policy·매도 %B dictionary
        작성 날짜: 2026/08/29
        """
        # 완전한 replay 전까지 공개 index와 분리된 local dictionary만 변경한다.
        loaded_orders: dict[str, Order] = {}
        loaded_lifecycles: dict[str, PendingOrderRecoveryLifecycle] = {}
        loaded_submission_provenance: dict[
            str,
            PendingOrderSubmissionProvenance,
        ] = {}
        loaded_submission_counts: dict[str, int] = {}
        loaded_client_ids_by_intent_attempt: dict[
            tuple[str, int],
            str,
        ] = {}
        loaded_risk_policy_versions: dict[str, int | None] = {}
        loaded_exit_pct_b_by_intent: dict[str, Decimal | None] = {}
        line_number = 0
        while True:
            raw_line = pending_order_file.readline()
            if raw_line == b"":
                break
            line_number += 1

            # writer가 항상 붙이는 LF가 없으면 crash tail로 보고 임의 복구하지 않는다.
            if not raw_line.endswith(b"\n") or raw_line.endswith(b"\r\n"):
                framing_error = ValueError(
                    "pending-order JSONL record separator must be LF"
                )
                raise PendingOrderJournalCorruptedError(
                    line_number,
                    type(framing_error).__name__,
                ) from None
            record_bytes = raw_line[:-1]

            # strict decode, exact schema, enum과 Order 불변식 중 하나라도 실패하면 startup을 중단한다.
            try:
                decoded_record = _decode_json_line(record_bytes)
                operation, payload = _pending_event_from_decoded_json(
                    decoded_record
                )
            except (
                _DuplicateJsonKeyError,
                _NonStandardJsonConstantError,
                TypeError,
                UnicodeDecodeError,
                ValueError,
                json.JSONDecodeError,
            ) as error:
                raise PendingOrderJournalCorruptedError(
                    line_number,
                    type(error).__name__,
                ) from None

            # 동일 UPSERT만 key로 수렴시키고 metadata·lifecycle·제출 근거 변조는 중단한다.
            if operation == _PENDING_ORDER_UPSERT:
                if not isinstance(payload, PendingOrderRecoveryRecord):
                    raise AssertionError(
                        "validated UPSERT payload must be a recovery record"
                    )
                order = payload.order
                existing_order = loaded_orders.get(order.client_order_id)
                if (
                    existing_order is not None
                    and existing_order != order
                ):
                    raise PendingOrderJournalCorruptedError(
                        line_number,
                        PendingOrderJournalConflictError.__name__,
                    ) from None
                existing_lifecycle = loaded_lifecycles.get(
                    order.client_order_id
                )
                if (
                    existing_lifecycle is not None
                    and existing_lifecycle is not payload.lifecycle
                ):
                    raise PendingOrderJournalCorruptedError(
                        line_number,
                        "PendingOrderLifecycleRegression",
                    ) from None
                existing_submission_provenance = (
                    loaded_submission_provenance.get(
                        order.client_order_id
                    )
                )
                if (
                    existing_submission_provenance is not None
                    and existing_submission_provenance
                    is not payload.submission_provenance
                ):
                    raise PendingOrderJournalCorruptedError(
                        line_number,
                        "PendingOrderSubmissionProvenanceConflict",
                    ) from None

                # REMOVE 후에도 intent attempt identity를 보존해 재시작 예산이 초기화되지 않게 한다.
                intent_attempt = (
                    order.intent_id,
                    order.submission_attempt,
                )
                existing_client_order_id = (
                    loaded_client_ids_by_intent_attempt.get(
                        intent_attempt
                    )
                )
                if (
                    existing_client_order_id is not None
                    and existing_client_order_id != order.client_order_id
                ):
                    raise PendingOrderJournalCorruptedError(
                        line_number,
                        "PendingOrderAttemptIdentityConflict",
                    ) from None
                if (
                    existing_client_order_id == order.client_order_id
                    and order.client_order_id not in loaded_orders
                ):
                    raise PendingOrderJournalCorruptedError(
                        line_number,
                        "PendingOrderAttemptResurrection",
                    ) from None
                if any(
                    active_order.intent_id == order.intent_id
                    and active_order.client_order_id
                    != order.client_order_id
                    for active_order in loaded_orders.values()
                ):
                    raise PendingOrderJournalCorruptedError(
                        line_number,
                        "PendingOrderConcurrentIntentAttempts",
                    ) from None
                durable_submission_count = loaded_submission_counts.get(
                    order.intent_id
                )
                if (
                    durable_submission_count is not None
                    and order.submission_attempt > durable_submission_count
                ):
                    raise PendingOrderJournalCorruptedError(
                        line_number,
                        "PendingOrderSubmissionAttemptGap",
                    ) from None

                # Tombstone 후에도 최초 attempt의 policy version을 유지해 restart 변경을 손상으로 분류한다.
                if (
                    order.intent_id in loaded_risk_policy_versions
                    and loaded_risk_policy_versions[order.intent_id]
                    != order.risk_policy_version
                ):
                    raise PendingOrderJournalCorruptedError(
                        line_number,
                        "PendingOrderRiskPolicyVersionConflict",
                    ) from None

                # Tombstone 뒤에도 최초 Case C SELL %B를 보존해 replay drift를 손상으로 분류한다.
                if (
                    order.intent_id in loaded_exit_pct_b_by_intent
                    and loaded_exit_pct_b_by_intent[order.intent_id]
                    != order.exit_pct_b_at_intent
                ):
                    raise PendingOrderJournalCorruptedError(
                        line_number,
                        "PendingOrderExitPctBConflict",
                    ) from None
                loaded_orders[order.client_order_id] = order
                loaded_lifecycles[order.client_order_id] = payload.lifecycle
                loaded_submission_provenance[
                    order.client_order_id
                ] = payload.submission_provenance
                loaded_client_ids_by_intent_attempt[
                    intent_attempt
                ] = order.client_order_id
                loaded_submission_counts[order.intent_id] = max(
                    order.submission_attempt + 1,
                    loaded_submission_counts.get(order.intent_id, 0),
                )
                loaded_risk_policy_versions.setdefault(
                    order.intent_id,
                    order.risk_policy_version,
                )
                loaded_exit_pct_b_by_intent.setdefault(
                    order.intent_id,
                    order.exit_pct_b_at_intent,
                )
            elif operation == _PENDING_ORDER_TRANSITION:
                if not isinstance(payload, tuple) or len(payload) != 2:
                    raise AssertionError(
                        "validated TRANSITION payload must contain ID and lifecycle"
                    )
                client_order_id, lifecycle = payload
                if (
                    not isinstance(client_order_id, str)
                    or not isinstance(lifecycle, PendingOrderRecoveryLifecycle)
                ):
                    raise AssertionError(
                        "validated TRANSITION payload has invalid types"
                    )
                if client_order_id not in loaded_orders:
                    raise PendingOrderJournalCorruptedError(
                        line_number,
                        "PendingOrderTransitionWithoutPrepared",
                    ) from None
                current_lifecycle = loaded_lifecycles[client_order_id]
                if current_lifecycle is lifecycle:
                    continue  # 불확실 fsync 뒤 반복된 같은 transition은 멱등 수렴한다.
                if not _pending_lifecycle_can_transition(
                    current_lifecycle,
                    lifecycle,
                ):
                    raise PendingOrderJournalCorruptedError(
                        line_number,
                        "PendingOrderLifecycleRegression",
                    ) from None
                loaded_lifecycles[client_order_id] = lifecycle
            else:
                if not isinstance(payload, str):
                    raise AssertionError("validated REMOVE payload must be a string")
                loaded_orders.pop(payload, None)
                loaded_lifecycles.pop(payload, None)
                loaded_submission_provenance.pop(payload, None)

        return (
            loaded_orders,
            loaded_lifecycles,
            loaded_submission_provenance,
            loaded_submission_counts,
            loaded_client_ids_by_intent_attempt,
            loaded_risk_policy_versions,
            loaded_exit_pct_b_by_intent,
        )  # 전체 검증 후 active 근거와 durable 예산·policy·매도 %B index를 함께 게시한다.

    def _build_append_payload(self, encoded_record: bytes) -> bytes:
        """
        함수 이름: _build_append_payload()
        기능: 유효하지만 LF가 없던 기존 마지막 record와 새 canonical line을 안전하게 구분한다.
        인자: encoded_record -> LF로 끝나는 새 JSON record bytes
        반환값: 한 번의 append write에 사용할 bytes
        작성 날짜: 2026/08/22
        """
        # 빈 파일은 canonical record 자체만 append하면 완전한 첫 line이 된다.
        if not self._storage_path.exists() or self._storage_path.stat().st_size == 0:
            return encoded_record  # 불필요한 선행 LF를 만들지 않는다.

        # startup에서 허용한 valid non-LF 마지막 record는 선행 LF로 먼저 완결한다.
        with self._storage_path.open("rb") as history_file:
            history_file.seek(-1, os.SEEK_END)
            final_byte = history_file.read(1)
        if final_byte == b"\n":
            return encoded_record  # 이미 완결된 JSONL 뒤에는 새 line만 이어 붙인다.

        return b"\n" + encoded_record  # valid non-LF tail을 먼저 완결한 뒤 새 record를 쓴다.

    def _confirm_uncertain_append(self, order_id: str) -> None:
        """
        함수 이름: _confirm_uncertain_append()
        기능: 이전 fsync failure 뒤 disk에서 발견한 동일 line을 다시 fsync해 durable로 확정한다.
        인자: order_id -> 불명확한 이전 append의 canonical order ID
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # 같은 line을 다시 append하지 않고 기존 file descriptor 자체를 durable sync한다.
        try:
            self._confirm_storage_durability()
        except Exception:
            self._index_loaded = False
            raise

        self._uncertain_order_ids.discard(order_id)  # 재fsync 성공 뒤에만 불명 상태를 해제한다.

    def _confirm_storage_durability(self) -> None:
        """
        함수 이름: _confirm_storage_durability()
        기능: startup 재생성 후에도 현재 보이는 JSONL bytes를 fsync해 이전 process의 불확실 append를 확정한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # append mode는 내용을 추가하지 않은 채 write-capable descriptor의 durability 요청을 보장한다.
        with self._storage_path.open("ab") as history_file:
            history_file.flush()
            os.fsync(history_file.fileno())

    def flush_durable_state(self) -> None:
        """
        함수 이름: flush_durable_state()
        기능: history 파일과 존재하는 pending/manual-kill journal 및 directory entry를 종료 전에 fsync한다.
        인자: 없음
        반환값: 현재 보이는 local 거래·control 상태의 durability 확인이 끝나면 없음
        작성 날짜: 2026/08/24
        """
        # 동일 repository mutation과 종료 장벽을 직렬화해 fsync 뒤 새 append가 끼어들지 않게 한다.
        with self._lock:
            self._confirm_storage_durability()
            _fsync_parent_directory(
                self._storage_path
            )  # 기존 파일도 최초 append의 directory entry durability를 종료 시 다시 확정한다.

            # Pending journal이 없다는 사실은 같은 directory fsync로 충분하고 빈 파일은 만들지 않는다.
            if self._pending_order_storage_path.exists():
                self._confirm_pending_order_storage_durability()
            # Cached receipt가 있으면 unlink된 path도 정상 종료로 우회하지 못하게 strict replay한다.
            if (
                self._manual_kill_control_loaded
                or self._manual_kill_control_state
                != ManualKillControlState()
                or self._manual_kill_control_replay
                or self._manual_kill_control_storage_path.exists()
            ):
                self._confirm_manual_kill_control_storage_durability()

    def _read_history_file(
        self,
        history_file: BinaryIO,
    ) -> tuple[dict[str, Trade], list[str]]:
        """
        함수 이름: _read_history_file()
        기능: open binary file을 streaming parse하고 malformed partial tail만 복구한다.
        인자: history_file -> streaming read할 binary file
        반환값: rebuilt order ID index와 최초 record의 file-order ID 목록
        작성 날짜: 2026/08/23
        """
        # order-id dedup index와 작은 순서 metadata를 공개 state와 분리된 local 값으로 만든다.
        loaded_index: dict[str, Trade] = {}
        loaded_order_ids: list[str] = []
        line_number = 0

        # readline 하나씩 처리해 전체 JSONL을 메모리에 올리지 않고 마지막 offset을 보존한다.
        while True:
            line_start_offset = history_file.tell()
            raw_line = history_file.readline()
            if raw_line == b"":
                break

            # LF/CR framing은 JSON decoder보다 먼저 검사해 JSONL separator 오류를 구분한다.
            line_number += 1
            has_line_feed = raw_line.endswith(b"\n")
            record_bytes = raw_line[:-1] if has_line_feed else raw_line
            if record_bytes.endswith(b"\r"):
                framing_error = ValueError("JSONL record separator must be LF")
                raise HistoryCorruptedError(
                    line_number,
                    type(framing_error).__name__,
                ) from framing_error

            # UTF-8, strict JSON constant와 중복 key를 domain schema보다 먼저 검증한다.
            try:
                decoded_record = _decode_json_line(record_bytes)
            except (
                _NonStandardJsonConstantError,
                UnicodeDecodeError,
                json.JSONDecodeError,
            ) as error:
                if not has_line_feed:
                    self._recover_partial_tail(
                        line_start_offset,
                        raw_line,
                    )
                    break
                raise HistoryCorruptedError(
                    line_number,
                    type(error).__name__,
                ) from error
            except _DuplicateJsonKeyError as error:
                raise HistoryCorruptedError(
                    line_number,
                    type(error).__name__,
                ) from error

            # decode가 끝난 object는 exact Trade schema와 fee 환산 정책으로 변환한다.
            try:
                trade = _trade_from_decoded_json(decoded_record)
            except FeeAssetConversionRequiredError:
                raise
            except (
                TypeError,
                ValueError,
            ) as error:
                raise HistoryCorruptedError(
                    line_number,
                    type(error).__name__,
                ) from error

            # 같은 order의 동일 record는 복구 시 no-op이고 다른 내용은 conflict로 중단한다.
            existing_trade = loaded_index.get(trade.order_id)
            if existing_trade is not None:
                if existing_trade == trade:
                    continue
                raise OrderHistoryConflictError(
                    f"order_id {trade.order_id} has conflicting trade content"
                )

            loaded_index[trade.order_id] = trade
            loaded_order_ids.append(
                trade.order_id
            )  # 최초 durable record의 순서만 별도 metadata에 보존한다.

        return (
            loaded_index,
            loaded_order_ids,
        )  # 완전한 local replay 결과만 caller가 repository state로 게시한다.

    def _stream_history_snapshot(
        self,
        snapshot_length: int,
        query: TradeHistoryQuery,
        snapshot_index: Mapping[str, Trade],
        ordered_order_ids: list[str],
        snapshot_order_count: int,
    ) -> Iterator[Trade]:
        """
        함수 이름: _stream_history_snapshot()
        기능: 고정 byte 경계의 JSONL을 검증하며 중복 제거와 query filter를 지연 적용한다.
        인자: snapshot_length -> 읽기를 중단할 exclusive file byte offset
            query -> KST 날짜와 side 조회 조건
            snapshot_index -> 같은 byte snapshot을 replay한 canonical order index
            ordered_order_ids -> 최초 record 순서를 보존한 repository metadata
            snapshot_order_count -> snapshot에 포함된 unique order 개수
        반환값: 조건에 맞는 Trade iterator
        작성 날짜: 2026/08/23
        """
        # generator가 실제 소비될 때만 descriptor를 열어 미소비 iterator의 resource 누수를 막는다.
        with self._storage_path.open("rb") as history_file:
            line_number = 0
            next_order_index = 0

            # 각 readline 크기를 남은 snapshot byte로 제한해 이후 append를 같은 export에 섞지 않는다.
            while history_file.tell() < snapshot_length:
                remaining_length = snapshot_length - history_file.tell()
                raw_line = history_file.readline(remaining_length)
                if raw_line == b"":
                    raise HistoryCorruptedError(
                        line_number + 1,
                        "SnapshotChanged",
                    )  # truncate나 file 교체로 snapshot 경계 전에 EOF가 오면 fail closed한다.

                # 원본 JSONL과 동일하게 CRLF를 거부하고 snapshot 끝의 valid non-LF record는 허용한다.
                line_number += 1
                has_line_feed = raw_line.endswith(b"\n")
                record_bytes = raw_line[:-1] if has_line_feed else raw_line
                if record_bytes.endswith(b"\r"):
                    framing_error = ValueError(
                        "JSONL record separator must be LF"
                    )
                    raise HistoryCorruptedError(
                        line_number,
                        type(framing_error).__name__,
                    ) from framing_error

                # strict UTF-8·JSON과 exact Trade schema를 startup reader와 같은 순서로 검증한다.
                try:
                    decoded_record = _decode_json_line(record_bytes)
                    trade = _trade_from_decoded_json(decoded_record)
                except FeeAssetConversionRequiredError:
                    raise
                except (
                    _DuplicateJsonKeyError,
                    _NonStandardJsonConstantError,
                    TypeError,
                    UnicodeDecodeError,
                    ValueError,
                    json.JSONDecodeError,
                ) as error:
                    raise HistoryCorruptedError(
                        line_number,
                        type(error).__name__,
                    ) from error

                # replay index와 같은 canonical record만 받아 duplicate line을 추가 row 없이 건너뛴다.
                canonical_trade = snapshot_index.get(trade.order_id)
                if canonical_trade is None or canonical_trade != trade:
                    raise OrderHistoryConflictError(
                        f"order_id {trade.order_id} has conflicting trade content"
                    )
                is_first_record = (
                    next_order_index < snapshot_order_count
                    and ordered_order_ids[next_order_index] == trade.order_id
                )
                if not is_first_record:
                    continue  # 같은 canonical order의 후속 durable duplicate는 export하지 않는다.
                next_order_index += 1

                # UTC execution instant를 KST LocalDate로 바꾼 뒤 양끝 포함 범위를 적용한다.
                executed_date = trade.executed_at.astimezone(
                    _KOREA_TIME_ZONE
                ).date()
                if not query.start_date <= executed_date <= query.end_date:
                    continue
                if query.side is TradeSide.BUY and trade.side is not OrderSide.BUY:
                    continue
                if query.side is TradeSide.SELL and trade.side is not OrderSide.SELL:
                    continue

                yield trade  # 원본 최초 record 순서대로 한 건씩 downstream CSV writer에 전달한다.

            # snapshot metadata와 실제 byte replay의 unique record 수가 다르면 조용히 누락하지 않는다.
            if next_order_index != snapshot_order_count:
                raise HistoryCorruptedError(
                    line_number + 1,
                    "SnapshotChanged",
                )

    def _recover_partial_tail(
        self,
        valid_length: int,
        corrupt_tail: bytes,
    ) -> None:
        """
        함수 이름: _recover_partial_tail()
        기능: malformed non-LF tail을 backup한 뒤 원본을 마지막 정상 LF까지 truncate한다.
        인자: valid_length -> 마지막 정상 LF 다음 byte offset
            corrupt_tail -> 보존할 malformed tail bytes
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 원본을 바꾸기 전에 손상 tail을 exclusive backup과 directory fsync로 보존한다.
        backup_path = self._create_corrupt_backup(corrupt_tail)
        try:
            with self._storage_path.open("r+b") as writable_file:
                # 최초 관찰 offset의 tail이 그대로인지 재확인한 뒤 마지막 정상 LF에서 자른다.
                writable_file.seek(valid_length)
                current_tail = writable_file.read()
                if current_tail != corrupt_tail:
                    raise RuntimeError(
                        "history changed while malformed tail was recovered"
                    )
                writable_file.seek(valid_length)
                writable_file.truncate()
                writable_file.flush()
                os.fsync(writable_file.fileno())
        except Exception as error:
            error.add_note(f"corrupt tail was preserved at {backup_path.name}")
            raise

    def _create_corrupt_backup(self, corrupt_tail: bytes) -> Path:
        """
        함수 이름: _create_corrupt_backup()
        기능: UTC timestamp를 가진 exclusive backup 파일에 malformed tail bytes를 보존한다.
        인자: corrupt_tail -> 원본 JSONL에서 분리한 malformed tail bytes
        반환값: 생성한 backup Path
        작성 날짜: 2026/08/21
        """
        # microsecond UTC 이름과 collision suffix를 결합해 기존 backup을 덮어쓰지 않는다.
        backup_time = _normalize_utc_datetime(self._clock(), "clock result")
        timestamp_text = backup_time.strftime("%Y%m%dT%H%M%S%fZ")
        backup_stem = f"{self._storage_path.name}.corrupt-{timestamp_text}"
        collision_index = 0

        # exclusive create가 성공할 때까지 suffix만 증가시키고 같은 tail bytes를 유지한다.
        while True:
            backup_name = backup_stem if collision_index == 0 else (
                f"{backup_stem}-{collision_index}"
            )
            backup_path = self._storage_path.with_name(backup_name)
            try:
                with backup_path.open("xb") as backup_file:
                    backup_file.write(corrupt_tail)
                    backup_file.flush()
                    os.fsync(backup_file.fileno())
                _fsync_parent_directory(backup_path)
                return backup_path  # file과 directory entry가 모두 durable해진 경로다.
            except FileExistsError:
                collision_index += 1  # 이미 있는 backup은 건드리지 않고 다음 이름을 시도한다.
