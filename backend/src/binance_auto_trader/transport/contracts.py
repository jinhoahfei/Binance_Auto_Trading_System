"""Loopback transport의 versioned JSON DTO와 TypeScript 계약을 정의한다."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import json
import re
from typing import Any, Protocol
from urllib.parse import parse_qsl
from uuid import UUID

from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.history import HistoryPeriod, TradeSide
from binance_auto_trader.domain.trading.logic_registry import (
    list_trading_logic_configurations,
)


SCHEMA_VERSION = 2
MAX_HTTP_BODY_BYTES = 1024 * 1024
MAX_WEBSOCKET_FRAME_BYTES = 1024 * 1024
MAX_TRADE_PAGE_SIZE = 1_000
MAX_RECENT_TRADES = 50
MAX_UNSIGNED_SEQUENCE = (1 << 64) - 1

_PLAIN_DECIMAL_PATTERN = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_REGIME_TO_WIRE = {
    RegimeType.TYPE_0: "type0",
    RegimeType.TYPE_1: "type1",
    RegimeType.TYPE_2: "type2",
    RegimeType.TYPE_3: "type3",
    RegimeType.TYPE_4: "type4",
}
_WIRE_TO_REGIME = {
    wire_value: regime_type
    for regime_type, wire_value in _REGIME_TO_WIRE.items()
}
_HISTORY_PERIOD_FROM_WIRE = {
    "today": HistoryPeriod.TODAY,
    "last7days": HistoryPeriod.LAST_7_DAYS,
    "last30days": HistoryPeriod.LAST_30_DAYS,
    "all": HistoryPeriod.ALL,
}
_HISTORY_PERIOD_TO_WIRE = {
    period: wire_value
    for wire_value, period in _HISTORY_PERIOD_FROM_WIRE.items()
}
_HISTORY_SIDE_FROM_WIRE = {
    "all": TradeSide.ALL,
    "buy": TradeSide.BUY,
    "sell": TradeSide.SELL,
}
_HISTORY_SIDE_TO_WIRE = {
    side: wire_value
    for wire_value, side in _HISTORY_SIDE_FROM_WIRE.items()
}


JsonScalar = str | int | bool | None
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject = dict[str, JsonValue]


class LockContext(Protocol):
    """
    클래스 이름: LockContext
    기능: application snapshot publication lock의 context manager 계약을 정의한다.
    작성 날짜: 2026/08/21
    """

    def __enter__(self) -> object:
        """
        함수 이름: __enter__()
        기능: application publication 임계 구역에 진입한다.
        인자: 없음
        반환값: lock context 값
        작성 날짜: 2026/08/21
        """
        ...

    def __exit__(
        self,
        exception_type: object,
        exception_value: object,
        traceback: object,
    ) -> bool | None:
        """
        함수 이름: __exit__()
        기능: application publication 임계 구역을 종료한다.
        인자: exception_type -> 발생한 예외 타입 또는 None
            exception_value -> 발생한 예외 값 또는 None
            traceback -> 발생한 traceback 또는 None
        반환값: 예외 억제 여부 또는 None
        작성 날짜: 2026/08/21
        """
        ...


class RuntimeSnapshotSource(Protocol):
    """
    클래스 이름: RuntimeSnapshotSource
    기능: bootstrap runtime에서 coherent transport snapshot을 읽는 최소 계약을 정의한다.
    작성 날짜: 2026/08/21
    """

    application_lock: LockContext
    ready: bool
    execution_mode: object
    market_snapshot: object
    regime_controller: object
    trading_controller: object
    trade_history_controller: object


class TransportContractError(RuntimeError):
    """
    클래스 이름: TransportContractError
    기능: 외부에 안전하게 공개할 transport failure code와 HTTP 상태를 보존한다.
    작성 날짜: 2026/08/21
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        retryable: bool = False,
        details: Mapping[str, object] | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: credential과 내부 예외를 포함하지 않는 typed transport failure를 생성한다.
        인자: code -> 안정적인 대문자 오류 code
            message -> 사용자에게 공개 가능한 오류 설명
            status -> 반환할 HTTP 상태
            retryable -> 같은 요청을 재시도할 수 있는지 여부
            details -> 공개 가능한 구조화 세부 정보
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 오류 envelope에 들어가는 모든 값의 타입과 안전한 기본 범위를 검증한다.
        if not isinstance(code, str) or not code.strip():
            raise ValueError("code must be a non-empty string")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")
        if isinstance(status, bool) or not isinstance(status, int):
            raise TypeError("status must be an integer")
        if status < 400 or status > 599:
            raise ValueError("status must be an HTTP error status")
        if not isinstance(retryable, bool):
            raise TypeError("retryable must be a bool")

        normalized_details = normalize_json_object(details or {})
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.details = normalized_details


@dataclass(frozen=True, slots=True)
class TransportResponse:
    """
    클래스 이름: TransportResponse
    기능: route가 HTTP adapter에 반환할 상태와 공통 envelope를 묶는다.
    작성 날짜: 2026/08/21
    """

    status: int
    payload: JsonObject

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: HTTP 상태와 JSON object payload의 타입을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # bool이 int의 하위 타입인 Python 특성 때문에 명시적으로 배제한다.
        if isinstance(self.status, bool) or not isinstance(self.status, int):
            raise TypeError("status must be an integer")
        if self.status < 100 or self.status > 599:
            raise ValueError("status must be a valid HTTP status")
        if not isinstance(self.payload, dict):
            raise TypeError("payload must be a JSON object")


def decimal_to_wire(value: Decimal) -> str:
    """
    함수 이름: decimal_to_wire()
    기능: 유한 Decimal을 exponent가 없는 JSON 문자열로 변환한다.
    인자: value -> 직렬화할 금융 Decimal
    반환값: plain decimal 문자열
    작성 날짜: 2026/08/21
    """
    # float 유입과 NaN 또는 Infinity 직렬화를 fail closed한다.
    if not isinstance(value, Decimal):
        raise TypeError("financial values must use Decimal")
    if not value.is_finite():
        raise ValueError("financial Decimal must be finite")

    wire_value = format(value, "f")  # Decimal exponent를 wire 숫자 문자열에 남기지 않는다.
    if _PLAIN_DECIMAL_PATTERN.fullmatch(wire_value) is None:
        raise ValueError("financial Decimal must have a plain representation")

    return wire_value


def decimal_from_wire(value: object, field_name: str) -> Decimal:
    """
    함수 이름: decimal_from_wire()
    기능: exponent와 float를 허용하지 않는 wire Decimal 문자열을 복원한다.
    인자: value -> JSON request에서 읽은 값
        field_name -> typed 오류에 사용할 field 이름
    반환값: 유한한 canonical Decimal
    작성 날짜: 2026/08/21
    """
    # JSON number와 exponent 표기를 모두 거부해 금융값의 문자열 정밀도를 보존한다.
    if (
        not isinstance(value, str)
        or _PLAIN_DECIMAL_PATTERN.fullmatch(value) is None
    ):
        raise TransportContractError(
            "MALFORMED_REQUEST",
            f"{field_name} must be a plain decimal string.",
        )

    # 검증된 plain 문자열을 Decimal로 복원하고 유한한 금융값인지 다시 확인한다.
    try:
        decimal_value = Decimal(value)
    except InvalidOperation as error:
        raise TransportContractError(
            "MALFORMED_REQUEST",
            f"{field_name} must be a valid decimal string.",
        ) from error
    if not decimal_value.is_finite():
        raise TransportContractError(
            "MALFORMED_REQUEST",
            f"{field_name} must be finite.",
        )

    return decimal_value


def ratio_from_wire(value: object, field_name: str) -> Decimal:
    """
    함수 이름: ratio_from_wire()
    기능: 0 이상 1 이하인 split ratio Decimal 문자열을 엄격히 복원한다.
    인자: value -> JSON request의 ratio 값
        field_name -> typed 오류에 사용할 field 이름
    반환값: 범위를 검증한 Decimal ratio
    작성 날짜: 2026/08/21
    """
    ratio = decimal_from_wire(value, field_name)

    # 음수 부호가 있는 zero도 UI canonical ratio와 다르므로 관대하게 보정하지 않는다.
    if ratio.is_signed() or ratio > Decimal("1"):
        raise TransportContractError(
            "INVALID_SPLIT_RATIO",
            f"{field_name} must be between 0 and 1.",
            status=422,
        )

    return ratio


def require_command_fields(
    request_body: Mapping[str, object],
    required_fields: Sequence[str],
) -> None:
    """
    함수 이름: require_command_fields()
    기능: endpoint command DTO가 schema와 정확한 필드 집합만 가지는지 검증한다.
    인자: request_body -> JSON decoder가 만든 command object
        required_fields -> schema_version 외 필수 field 이름 순서
    반환값: 정확한 DTO이면 없음
    작성 날짜: 2026/08/21
    """
    # DTO container와 endpoint가 선언한 필드 목록 자체를 먼저 엄격히 검증한다.
    if not isinstance(request_body, Mapping):
        raise TransportContractError(
            "MALFORMED_REQUEST",
            "The request body must be a JSON object.",
        )
    if not isinstance(required_fields, Sequence) or isinstance(
        required_fields,
        (str, bytes, bytearray),
    ):
        raise TypeError("required_fields must be a sequence of strings")
    if any(not isinstance(field_name, str) for field_name in required_fields):
        raise TypeError("required_fields must contain strings")

    # Endpoint별 allowlist와 입력 key를 정확히 비교해 typo와 미래 field를 묵인하지 않는다.
    expected_fields = frozenset(("schema_version", *required_fields))
    actual_fields = frozenset(request_body)
    if actual_fields != expected_fields:
        raise TransportContractError(
            "MALFORMED_REQUEST",
            "The request body fields do not match the endpoint contract.",
            details={
                "missing_fields": sorted(expected_fields - actual_fields),
                "unexpected_fields": sorted(actual_fields - expected_fields),
            },
        )

    schema_version = request_body["schema_version"]  # exact key 집합 검증 뒤 안전하게 읽는다.
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != SCHEMA_VERSION
    ):
        raise TransportContractError(
            "UNSUPPORTED_SCHEMA_VERSION",
            "The request schema version is not supported.",
        )


def require_expected_version(request_body: Mapping[str, object]) -> int:
    """
    함수 이름: require_expected_version()
    기능: optimistic concurrency용 expected_version을 bool이 아닌 0 이상 정수로 읽는다.
    인자: request_body -> expected_version을 포함한 command object
    반환값: 검증된 context version
    작성 날짜: 2026/08/21
    """
    # bool을 정수로 묵인하지 않고 optimistic version의 타입과 범위를 함께 확인한다.
    expected_version = request_body.get("expected_version")
    if (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 0
    ):
        raise TransportContractError(
            "MALFORMED_REQUEST",
            "expected_version must be a non-negative integer.",
        )

    return expected_version


def datetime_to_wire(value: datetime) -> str:
    """
    함수 이름: datetime_to_wire()
    기능: timezone-aware datetime을 microsecond 정밀도의 UTC RFC 3339 Z로 변환한다.
    인자: value -> 직렬화할 backend 시각
    반환값: UTC RFC 3339 Z 문자열
    작성 날짜: 2026/08/21
    """
    # naive datetime은 실행 환경 timezone에 따라 달라지므로 거부한다.
    if not isinstance(value, datetime):
        raise TypeError("timestamps must use datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")

    utc_value = value.astimezone(timezone.utc)
    return utc_value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def regime_to_wire(regime_type: RegimeType) -> str:
    """
    함수 이름: regime_to_wire()
    기능: canonical backend REGIME을 유일한 lowercase wire 값으로 변환한다.
    인자: regime_type -> canonical RegimeType
    반환값: type0부터 type4 중 하나
    작성 날짜: 2026/08/21
    """
    try:
        return _REGIME_TO_WIRE[regime_type]
    except (KeyError, TypeError) as error:
        raise TransportContractError(
            "INVALID_REGIME_TYPE",
            "REGIME type is not canonical.",
            status=422,
        ) from error


def regime_from_wire(wire_value: str) -> RegimeType:
    """
    함수 이름: regime_from_wire()
    기능: lowercase wire REGIME을 canonical backend RegimeType으로 엄격히 변환한다.
    인자: wire_value -> type0부터 type4 중 하나인 요청 값
    반환값: canonical RegimeType
    작성 날짜: 2026/08/21
    """
    try:
        return _WIRE_TO_REGIME[wire_value]
    except (KeyError, TypeError) as error:
        raise TransportContractError(
            "INVALID_REGIME_TYPE",
            "REGIME type is not canonical.",
            status=422,
        ) from error


def _trade_history_query_error() -> TransportContractError:
    """
    함수 이름: _trade_history_query_error()
    기능: 상세 조회 query shape나 값이 비정규일 때 사용할 공개 안전 오류를 생성한다.
    인자: 없음
    반환값: 재시도 불가능한 MALFORMED_REQUEST 오류
    작성 날짜: 2026/08/23
    """
    return TransportContractError(
        "MALFORMED_REQUEST",
        "Trade history query must contain one canonical period and side.",
        status=400,
        retryable=False,
    )


def _history_period_to_wire(period: HistoryPeriod) -> str:
    """
    함수 이름: _history_period_to_wire()
    기능: canonical HistoryPeriod를 UI query와 동일한 lowercase wire 값으로 변환한다.
    인자: period -> 상세 조회에 적용한 canonical 기간
    반환값: today, last7days, last30days 또는 all
    작성 날짜: 2026/08/23
    """
    try:
        return _HISTORY_PERIOD_TO_WIRE[period]
    except (KeyError, TypeError) as error:
        raise TypeError("period must be a canonical HistoryPeriod") from error


def _history_side_to_wire(side: TradeSide) -> str:
    """
    함수 이름: _history_side_to_wire()
    기능: canonical TradeSide를 UI query와 동일한 lowercase wire 값으로 변환한다.
    인자: side -> 상세 조회에 적용한 canonical 거래 방향
    반환값: all, buy 또는 sell
    작성 날짜: 2026/08/23
    """
    try:
        return _HISTORY_SIDE_TO_WIRE[side]
    except (KeyError, TypeError) as error:
        raise TypeError("side must be a canonical TradeSide") from error


def parse_trade_history_query_parameters(
    query_parameters: str,
) -> tuple[HistoryPeriod, TradeSide]:
    """
    함수 이름: parse_trade_history_query_parameters()
    기능: raw URL query에서 exact period와 side를 canonical domain enum으로 복원한다.
    인자: query_parameters -> fragment를 제외한 percent-encoded raw query string
    반환값: canonical HistoryPeriod와 TradeSide tuple
    작성 날짜: 2026/08/23
    """
    if not isinstance(query_parameters, str):
        raise _trade_history_query_error()

    # 두 field 이외의 누락·중복·unknown 값은 parser 단계에서 모두 같은 typed 400으로 닫는다.
    try:
        query_pairs = parse_qsl(
            query_parameters,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=2,
            separator="&",
        )
    except (UnicodeError, ValueError) as error:
        raise _trade_history_query_error() from error
    if len(query_pairs) != 2:
        raise _trade_history_query_error()

    # Percent-decoding 뒤의 exact 이름과 cardinality만 허용하고 whitespace를 정규화하지 않는다.
    query_values: dict[str, str] = {}
    expected_field_names = {"period", "side"}
    for field_name, field_value in query_pairs:
        if field_name not in expected_field_names:
            raise _trade_history_query_error()
        if field_name in query_values:
            raise _trade_history_query_error()
        if not field_value:
            raise _trade_history_query_error()
        query_values[field_name] = field_value
    if set(query_values) != expected_field_names:
        raise _trade_history_query_error()

    # Wire enum lookup은 대소문자 변환이나 default 없이 canonical 값만 domain enum으로 바꾼다.
    try:
        period = _HISTORY_PERIOD_FROM_WIRE[query_values["period"]]
        side = _HISTORY_SIDE_FROM_WIRE[query_values["side"]]
    except KeyError as error:
        raise _trade_history_query_error() from error

    return period, side  # Controller에는 transport 문자열이 아니라 검증된 enum만 전달한다.


def normalize_json_value(value: object) -> JsonValue:
    """
    함수 이름: normalize_json_value()
    기능: transport payload 값을 Decimal과 UTC 규칙을 지키는 JSON 값으로 정규화한다.
    인자: value -> DTO 또는 event payload의 값
    반환값: JSON encoder가 손실 없이 처리할 값
    작성 날짜: 2026/08/21
    """
    # str Enum이 일반 문자열 branch에 먼저 잡히지 않도록 canonical enum을 우선 변환한다.
    if isinstance(value, RegimeType):
        return regime_to_wire(value)
    if isinstance(value, Enum):
        return normalize_json_value(value.value)
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise TypeError("float values are forbidden in transport payloads")
    if isinstance(value, Decimal):
        return decimal_to_wire(value)
    if isinstance(value, datetime):
        return datetime_to_wire(value)

    # Mapping key와 sequence 원소를 재귀적으로 정규화한다.
    if isinstance(value, Mapping):
        normalized_mapping: JsonObject = {}
        for key, nested_value in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            normalized_mapping[key] = normalize_json_value(nested_value)
        return normalized_mapping
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [normalize_json_value(item) for item in value]

    raise TypeError(f"unsupported transport value type: {type(value).__name__}")


def normalize_json_object(value: Mapping[str, object]) -> JsonObject:
    """
    함수 이름: normalize_json_object()
    기능: 문자열 key Mapping을 안전한 JSON object로 정규화한다.
    인자: value -> 정규화할 object mapping
    반환값: 정규화된 JSON object
    작성 날짜: 2026/08/21
    """
    normalized_value = normalize_json_value(value)
    if not isinstance(normalized_value, dict):
        raise TypeError("value must normalize to a JSON object")

    return normalized_value


def validate_uuid_text(value: object, field_name: str) -> str:
    """
    함수 이름: validate_uuid_text()
    기능: UUID header와 session 식별자의 canonical 문자열 형태를 검증한다.
    인자: value -> 검증할 UUID 문자열
        field_name -> typed failure에 사용할 필드 이름
    반환값: 입력 UUID 문자열
    작성 날짜: 2026/08/21
    """
    if not isinstance(value, str) or not value:
        raise TransportContractError(
            "MALFORMED_REQUEST",
            f"{field_name} must be a UUID.",
        )

    try:
        parsed_value = UUID(value)
    except (ValueError, AttributeError) as error:
        raise TransportContractError(
            "MALFORMED_REQUEST",
            f"{field_name} must be a UUID.",
        ) from error
    if str(parsed_value) != value:
        raise TransportContractError(
            "MALFORMED_REQUEST",
            f"{field_name} must use canonical UUID text.",
        )

    return value


def success_response(
    request_id: str,
    data: Mapping[str, object],
    *,
    status: int = 200,
) -> TransportResponse:
    """
    함수 이름: success_response()
    기능: ADR-005 형식의 성공 HTTP envelope를 생성한다.
    인자: request_id -> 검증을 마친 요청 UUID
        data -> versioned response DTO
        status -> 성공 HTTP 상태
    반환값: HTTP adapter가 전송할 TransportResponse
    작성 날짜: 2026/08/21
    """
    validate_uuid_text(request_id, "X-Request-Id")
    normalized_data = normalize_json_object(data)

    return TransportResponse(
        status=status,
        payload={
            "schema_version": SCHEMA_VERSION,
            "request_id": request_id,
            "ok": True,
            "data": normalized_data,
        },
    )


def error_response(
    request_id: str,
    error: TransportContractError,
) -> TransportResponse:
    """
    함수 이름: error_response()
    기능: ADR-005 형식의 user-safe 실패 HTTP envelope를 생성한다.
    인자: request_id -> 요청 UUID 또는 server가 만든 correlation UUID
        error -> 공개 가능한 typed transport failure
    반환값: HTTP adapter가 전송할 TransportResponse
    작성 날짜: 2026/08/21
    """
    validate_uuid_text(request_id, "X-Request-Id")
    if not isinstance(error, TransportContractError):
        raise TypeError("error must be a TransportContractError")

    return TransportResponse(
        status=error.status,
        payload={
            "schema_version": SCHEMA_VERSION,
            "request_id": request_id,
            "ok": False,
            "error": {
                "code": error.code,
                "message": str(error),
                "retryable": error.retryable,
                "details": error.details,
            },
        },
    )


def json_bytes(payload: Mapping[str, object]) -> bytes:
    """
    함수 이름: json_bytes()
    기능: 정규화된 transport object를 compact UTF-8 JSON bytes로 인코딩한다.
    인자: payload -> 전송할 JSON object
    반환값: 네트워크에 기록할 UTF-8 bytes
    작성 날짜: 2026/08/21
    """
    normalized_payload = normalize_json_object(payload)
    return json.dumps(
        normalized_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def map_market_snapshot(market_snapshot: object) -> JsonObject:
    """
    함수 이름: map_market_snapshot()
    기능: authoritative MarketSnapshot 공개 상태를 versioned market DTO로 변환한다.
    인자: market_snapshot -> bootstrap runtime의 MarketSnapshot
    반환값: 금융값을 문자열로 보존한 market DTO
    작성 날짜: 2026/08/21
    """
    current_price = getattr(market_snapshot, "current_eth_price")
    updated_at = getattr(market_snapshot, "updated_at")

    return normalize_json_object(
        {
            "symbol": getattr(market_snapshot, "symbol"),
            "current_price": current_price,
            "version": getattr(market_snapshot, "version"),
            "updated_at": updated_at,
        }
    )


def map_indicator_snapshot(indicator_snapshot: object | None) -> JsonObject | None:
    """
    함수 이름: map_indicator_snapshot()
    기능: authoritative IndicatorSnapshot과 swing 구조를 nullable DTO로 변환한다.
    인자: indicator_snapshot -> 준비된 IndicatorSnapshot 또는 None
    반환값: 지표 DTO 또는 아직 평가되지 않았으면 None
    작성 날짜: 2026/08/21
    """
    if indicator_snapshot is None:
        return None
    if not getattr(indicator_snapshot, "ready"):
        return None

    # ready snapshot에서만 값 접근 시 발생하는 domain RuntimeError를 피한다.
    swing_structure = getattr(indicator_snapshot, "swing_structure")
    return normalize_json_object(
        {
            "symbol": getattr(indicator_snapshot, "symbol"),
            "timeframe": getattr(indicator_snapshot, "timeframe"),
            "ema9_series": getattr(indicator_snapshot, "ema9_series"),
            "ema9_slope": getattr(indicator_snapshot, "ema9_slope"),
            "live_ema9": getattr(indicator_snapshot, "live_ema9"),
            "current_price": getattr(indicator_snapshot, "current_price"),
            "source_market_version": getattr(
                indicator_snapshot,
                "source_market_version",
            ),
            "source_candle_id": getattr(indicator_snapshot, "source_candle_id"),
            "calculated_at": getattr(indicator_snapshot, "calculated_at"),
            "swing": {
                "highs": getattr(swing_structure, "swing_highs"),
                "lows": getattr(swing_structure, "swing_lows"),
                "has_higher_high": getattr(swing_structure, "has_higher_high"),
                "has_higher_low": getattr(swing_structure, "has_higher_low"),
                "has_lower_high": getattr(swing_structure, "has_lower_high"),
                "has_lower_low": getattr(swing_structure, "has_lower_low"),
            },
        }
    )


def map_account_snapshot(account: object) -> JsonObject:
    """
    함수 이름: map_account_snapshot()
    기능: authoritative Account 잔액과 USDT 평가 상태를 account DTO로 변환한다.
    인자: account -> TradingController가 소유한 Account
    반환값: 자산별 free, locked와 평가 상태 DTO
    작성 날짜: 2026/08/21
    """
    balances = getattr(account, "balances")

    # asset 이름으로 정렬해 같은 state가 항상 같은 JSON 표현을 만들도록 한다.
    balance_rows = []
    for asset_name in sorted(balances):
        balance = balances[asset_name]
        balance_rows.append(
            {
                "asset": getattr(balance, "asset"),
                "free": getattr(balance, "free"),
                "locked": getattr(balance, "locked"),
                "total": getattr(balance, "total"),
            }
        )

    return normalize_json_object(
        {
            "valuation_asset": getattr(account, "valuation_asset"),
            "quote_asset": "USDT",
            "current_price": getattr(account, "current_price"),
            "valuation": getattr(account, "valuation"),
            "version": getattr(account, "version"),
            "updated_at": getattr(account, "updated_at"),
            "balances": balance_rows,
        }
    )


def map_trade(trade: object) -> JsonObject:
    """
    함수 이름: map_trade()
    기능: durable Trade의 실제 체결 필드만 recent trade DTO로 변환한다.
    인자: trade -> persistence에서 복원한 canonical Trade
    반환값: entry price나 slippage를 추측하지 않은 trade DTO
    작성 날짜: 2026/08/21
    """
    exit_reason = getattr(trade, "exit_reason")

    return normalize_json_object(
        {
            "trade_id": getattr(trade, "trade_id"),
            "order_id": getattr(trade, "order_id"),
            "client_order_id": getattr(trade, "client_order_id"),
            "symbol": getattr(trade, "symbol"),
            "executed_at": getattr(trade, "executed_at"),
            "side": getattr(trade, "side"),
            "regime_type": getattr(trade, "regime_type"),
            "strategy": getattr(trade, "strategy"),
            "requested_quantity": getattr(trade, "requested_quantity"),
            "executed_quantity": getattr(trade, "executed_quantity"),
            "executed_amount": getattr(trade, "executed_amount"),
            "average_fill_price": getattr(trade, "average_fill_price"),
            "market_price_at_decision": getattr(
                trade,
                "market_price_at_decision",
            ),
            "fee_amount": getattr(trade, "fee_amount"),
            "fee_asset": getattr(trade, "fee_asset"),
            "fee_quote_amount": getattr(trade, "fee_quote_amount"),
            "allocated_cost_basis": getattr(trade, "allocated_cost_basis"),
            "realized_pnl": getattr(trade, "realized_pnl"),
            "realized_return_rate": getattr(trade, "realized_return_rate"),
            "exit_reason": None if exit_reason is None else exit_reason.value,
        }
    )


def map_performance(performance: object) -> JsonObject:
    """
    함수 이름: map_performance()
    기능: ADR-004 Performance의 저장된 집계 필드를 그대로 DTO로 변환한다.
    인자: performance -> TradeHistoryController가 공개한 Performance
    반환값: 계산식을 재구현하지 않은 performance DTO
    작성 날짜: 2026/08/21
    """
    field_names = (
        "daily_return_rate",
        "cumulative_return_rate",
        "realized_pnl",
        "daily_fee",
        "total_fee",
        "average_sell_return_rate",
        "total_profit",
        "winning_sell_count",
        "losing_sell_count",
        "breakeven_sell_count",
        "completed_sell_count",
        "win_rate",
    )

    return normalize_json_object(
        {
            field_name: getattr(performance, field_name)
            for field_name in field_names
        }
    )


def map_trade_details(
    trade_details: object,
    period: HistoryPeriod,
) -> JsonObject:
    """
    함수 이름: map_trade_details()
    기능: 상세 조회 결과를 applied query, filtered rows와 별도 전체 summary DTO로 변환한다.
    인자: trade_details -> TradeHistoryController가 반환한 TradeDetailsResult
        period -> Controller query에 전달한 canonical 기간 preset
    반환값: 금융값을 문자열로 보존한 composite trade-history DTO
    작성 날짜: 2026/08/23
    """
    if not isinstance(period, HistoryPeriod):
        raise TypeError("period must be a canonical HistoryPeriod")

    # Result의 query 날짜·side와 filtered row tuple을 한 application snapshot에서 읽는다.
    query = getattr(trade_details, "query")
    rows = getattr(trade_details, "rows")
    start_date = getattr(query, "start_date")
    end_date = getattr(query, "end_date")
    side = getattr(query, "side")
    if type(start_date) is not date or type(end_date) is not date:
        raise TypeError("trade detail query dates must be date values")
    if not isinstance(rows, tuple):
        raise TypeError("trade detail rows must be a tuple")
    if len(rows) > MAX_TRADE_PAGE_SIZE:
        raise TransportContractError(
            "TRADE_HISTORY_PAGE_TOO_LARGE",
            "Trade history query exceeds the maximum page size.",
            status=413,
            retryable=False,
            details={"maximum_rows": MAX_TRADE_PAGE_SIZE},
        )

    # D-12에 따라 row filter 결과와 Account·Performance 전체 summary 범위를 분리한다.
    row_dtos = [map_trade(row) for row in rows]  # 검증된 상한 안의 행만 일괄 mapping한다.
    performance_dto = map_performance(
        getattr(trade_details, "performance")
    )  # Controller가 계산한 aggregate를 transport에서 다시 계산하지 않는다.
    return normalize_json_object(
        {
            "query": {
                "period": _history_period_to_wire(period),
                "side": _history_side_to_wire(side),
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
            "rows": row_dtos,
            "row_count": len(row_dtos),
            "summary": {
                "holdings_asset": getattr(trade_details, "holdings_asset"),
                "holdings": getattr(trade_details, "holdings"),
                "account_version": getattr(
                    trade_details,
                    "account_version",
                ),
                "performance": performance_dto,
            },
        }
    )


def map_trading_logic_coverage() -> list[JsonObject]:
    """
    함수 이름: map_trading_logic_coverage()
    기능: 불변 domain registry를 UI 시작 Gate에 필요한 공개 지원 상태 목록으로 변환한다.
    인자: 없음
    반환값: canonical REGIME 순서의 다섯 거래 로직 지원 상태 DTO
    작성 날짜: 2026/08/21
    """
    configurations = list_trading_logic_configurations()  # domain canonical 순서를 유지한다.

    # 내부 transition source와 ID는 숨기고 UI 판단에 필요한 최소 계약만 공개한다.
    return [
        normalize_json_object(
            {
                "regime_type": configuration.regime_type,
                "support_status": configuration.support_status,
                "start_guard": configuration.start_guard,
            }
        )
        for configuration in configurations
    ]


def map_trading_snapshot(
    trading_controller: object,
    execution_mode: object,
) -> JsonObject:
    """
    함수 이름: map_trading_snapshot()
    기능: authoritative session Context와 mode gate를 Phase 7 trading DTO로 변환한다.
    인자: trading_controller -> session과 mutable TradingContext를 소유한 Controller
        execution_mode -> bootstrap이 검증한 실행 mode enum 또는 문자열
    반환값: lifecycle, version, ratio와 position 상태를 포함한 trading DTO
    작성 날짜: 2026/08/21
    """
    # Controller가 원자적으로 만든 session snapshot과 execution mode를 wire 값으로 읽는다.
    session_snapshot = getattr(trading_controller, "snapshot_session")()
    session_status = getattr(session_snapshot, "status")
    status_text = getattr(session_status, "value", session_status)
    mode_text = getattr(execution_mode, "value", execution_mode)

    # Controller lock 아래 만든 snapshot 하나로 lifecycle과 optimistic version을 일치시킨다.
    return normalize_json_object(
        {
            "mode": mode_text,
            "status": status_text,
            "version": getattr(session_snapshot, "version"),
            "command_enabled": getattr(session_snapshot, "command_enabled"),
            "scale_in": getattr(session_snapshot, "scale_in"),
            "scale_out": getattr(session_snapshot, "scale_out"),
            "has_open_position": getattr(
                session_snapshot,
                "has_open_position",
            ),
            "session_id": getattr(session_snapshot, "session_id"),
            "logic_coverage": map_trading_logic_coverage(),
        }
    )


def build_snapshot_dto(
    runtime: RuntimeSnapshotSource,
    session_id: str,
    last_sequence: int,
) -> JsonObject:
    """
    함수 이름: build_snapshot_dto()
    기능: runtime domain state를 UI가 snapshot-first로 적용할 coherent DTO로 조합한다.
    인자: runtime -> application lock을 이미 보유한 bootstrap runtime
        session_id -> transport process launch UUID
        last_sequence -> 같은 임계 구역에서 읽은 event sequence
    반환값: version 1 전체 snapshot DTO
    작성 날짜: 2026/08/21
    """
    validate_uuid_text(session_id, "session_id")
    if (
        isinstance(last_sequence, bool)
        or not isinstance(last_sequence, int)
        or last_sequence < 0
        or last_sequence > MAX_UNSIGNED_SEQUENCE
    ):
        raise ValueError("last_sequence must be an unsigned 64-bit integer")

    # Controller가 소유한 현재 entity 참조를 publication 임계 구역에서 한 번씩 읽는다.
    regime_controller = runtime.regime_controller
    trading_controller = runtime.trading_controller
    trade_history_controller = runtime.trade_history_controller
    account = getattr(trading_controller, "account")
    indicator_snapshot = getattr(regime_controller, "indicator_snapshot")
    recommended_regime = getattr(regime_controller, "recommended_regime")
    selected_regime = getattr(regime_controller, "selected_regime")
    trade_history = getattr(trade_history_controller, "trade_history")
    performance = getattr(trade_history_controller, "performance")
    current_trades = getattr(trade_history, "trades")

    # 최신 체결만 제한해서 startup snapshot의 크기를 일정하게 유지한다.
    recent_trades = sorted(
        current_trades,
        key=lambda trade: getattr(trade, "executed_at"),
        reverse=True,
    )[:MAX_RECENT_TRADES]
    return normalize_json_object(
        {
            "session_id": session_id,
            "last_sequence": last_sequence,
            "connection": {
                "status": "online",
                "ready": runtime.ready,
                "schema_version": SCHEMA_VERSION,
            },
            "market": map_market_snapshot(runtime.market_snapshot),
            "regime": {
                "indicator": map_indicator_snapshot(indicator_snapshot),
                "recommended": recommended_regime,
                "selected": selected_regime,
            },
            "trading": map_trading_snapshot(
                trading_controller,
                runtime.execution_mode,
            ),
            "account": map_account_snapshot(account),
            "recent_trades": [map_trade(trade) for trade in recent_trades],
            "performance": map_performance(performance),
        }
    )


def render_typescript_contracts() -> str:
    """
    함수 이름: render_typescript_contracts()
    기능: Python schema에서 deterministic TypeScript transport 계약을 생성한다.
    인자: 없음
    반환값: generated 파일에 그대로 기록할 TypeScript source
    작성 날짜: 2026/08/21
    """
    # schema와 enum 값이 변경되면 이 단일 renderer 출력도 함께 변경된다.
    regime_values = " | ".join(
        f"'{wire_value}'"
        for wire_value in _REGIME_TO_WIRE.values()
    )
    return f"""/* 이 파일은 Python transport schema에서 생성됩니다. 직접 수정하지 마세요. */

export const BACKEND_SCHEMA_VERSION = {SCHEMA_VERSION} as const;
export const BACKEND_MAX_TRADE_PAGE_SIZE = {MAX_TRADE_PAGE_SIZE} as const;

export type BackendDecimalString = string;
export type BackendRegimeType = {regime_values};
export type BackendExecutionMode = 'disabled' | 'fake' | 'testnet' | 'live';
export type BackendTradingStatus =
    | 'not_started'
    | 'running'
    | 'stopping'
    | 'reconciliation_required'
    | 'terminated';
export type BackendTradingLogicSupportStatus = 'supported' | 'unsupported';
export type BackendTradingLogicStartGuard =
    | 'READY'
    | 'UNSUPPORTED_TRADING_LOGIC';
export type BackendTradeSide = 'BUY' | 'SELL';
export type BackendHistoryPeriod = 'today' | 'last7days' | 'last30days' | 'all';
export type BackendTradeSideFilter = 'all' | 'buy' | 'sell';
export type BackendStrategyType = 'CASE_B' | 'CASE_C';

export interface BackendConnectionSnapshot {{
    readonly status: 'online';
    readonly ready: boolean;
    readonly schema_version: typeof BACKEND_SCHEMA_VERSION;
}}

export interface BackendMarketSnapshot {{
    readonly symbol: string;
    readonly current_price: BackendDecimalString | null;
    readonly version: number;
    readonly updated_at: string | null;
}}

export interface BackendSwingSnapshot {{
    readonly highs: ReadonlyArray<BackendDecimalString>;
    readonly lows: ReadonlyArray<BackendDecimalString>;
    readonly has_higher_high: boolean;
    readonly has_higher_low: boolean;
    readonly has_lower_high: boolean;
    readonly has_lower_low: boolean;
}}

export interface BackendIndicatorSnapshot {{
    readonly symbol: string;
    readonly timeframe: '4h';
    readonly ema9_series: ReadonlyArray<BackendDecimalString>;
    readonly ema9_slope: BackendDecimalString;
    readonly live_ema9: BackendDecimalString;
    readonly current_price: BackendDecimalString;
    readonly source_market_version: number;
    readonly source_candle_id: string;
    readonly calculated_at: string;
    readonly swing: BackendSwingSnapshot;
}}

export interface BackendRegimeSnapshot {{
    readonly indicator: BackendIndicatorSnapshot | null;
    readonly recommended: BackendRegimeType | null;
    readonly selected: BackendRegimeType | null;
}}

export interface BackendTradingLogicCoverage {{
    readonly regime_type: BackendRegimeType;
    readonly support_status: BackendTradingLogicSupportStatus;
    readonly start_guard: BackendTradingLogicStartGuard;
}}

export interface BackendTradingSnapshot {{
    readonly mode: BackendExecutionMode;
    readonly status: BackendTradingStatus;
    readonly version: number;
    readonly command_enabled: boolean;
    readonly scale_in: BackendDecimalString;
    readonly scale_out: BackendDecimalString;
    readonly has_open_position: boolean;
    readonly session_id: string | null;
    readonly logic_coverage: ReadonlyArray<BackendTradingLogicCoverage>;
}}

export interface BackendBalanceSnapshot {{
    readonly asset: string;
    readonly free: BackendDecimalString;
    readonly locked: BackendDecimalString;
    readonly total: BackendDecimalString;
}}

export interface BackendAccountSnapshot {{
    readonly valuation_asset: string;
    readonly quote_asset: 'USDT';
    readonly current_price: BackendDecimalString | null;
    readonly valuation: BackendDecimalString | null;
    readonly version: number;
    readonly updated_at: string | null;
    readonly balances: ReadonlyArray<BackendBalanceSnapshot>;
}}

export interface BackendTradeSnapshot {{
    readonly trade_id: string;
    readonly order_id: string;
    readonly client_order_id: string;
    readonly symbol: string;
    readonly executed_at: string;
    readonly side: BackendTradeSide;
    readonly regime_type: BackendRegimeType;
    readonly strategy: BackendStrategyType;
    readonly requested_quantity: BackendDecimalString;
    readonly executed_quantity: BackendDecimalString;
    readonly executed_amount: BackendDecimalString;
    readonly average_fill_price: BackendDecimalString;
    readonly market_price_at_decision: BackendDecimalString;
    readonly fee_amount: BackendDecimalString;
    readonly fee_asset: string;
    readonly fee_quote_amount: BackendDecimalString;
    readonly allocated_cost_basis: BackendDecimalString | null;
    readonly realized_pnl: BackendDecimalString | null;
    readonly realized_return_rate: BackendDecimalString | null;
    readonly exit_reason: string | null;
}}

export interface BackendPerformanceSnapshot {{
    readonly daily_return_rate: BackendDecimalString;
    readonly cumulative_return_rate: BackendDecimalString;
    readonly realized_pnl: BackendDecimalString;
    readonly daily_fee: BackendDecimalString;
    readonly total_fee: BackendDecimalString;
    readonly average_sell_return_rate: BackendDecimalString;
    readonly total_profit: BackendDecimalString;
    readonly winning_sell_count: number;
    readonly losing_sell_count: number;
    readonly breakeven_sell_count: number;
    readonly completed_sell_count: number;
    readonly win_rate: BackendDecimalString | null;
}}

export interface BackendTradeDetailsQuery {{
    readonly period: BackendHistoryPeriod;
    readonly side: BackendTradeSideFilter;
    readonly start_date: string;
    readonly end_date: string;
}}

export interface BackendTradeDetailsSummary {{
    readonly holdings_asset: 'ETH';
    readonly holdings: BackendDecimalString;
    readonly account_version: number;
    readonly performance: BackendPerformanceSnapshot;
}}

export interface BackendTradeDetails {{
    readonly query: BackendTradeDetailsQuery;
    readonly rows: ReadonlyArray<BackendTradeSnapshot>;
    readonly row_count: number;
    readonly summary: BackendTradeDetailsSummary;
}}

export interface BackendSnapshot {{
    readonly session_id: string;
    readonly last_sequence: number;
    readonly connection: BackendConnectionSnapshot;
    readonly market: BackendMarketSnapshot;
    readonly regime: BackendRegimeSnapshot;
    readonly trading: BackendTradingSnapshot;
    readonly account: BackendAccountSnapshot;
    readonly recent_trades: ReadonlyArray<BackendTradeSnapshot>;
    readonly performance: BackendPerformanceSnapshot;
}}

export interface BackendHealth {{
    readonly process: 'running';
    readonly session_id: string;
    readonly schema_version: typeof BACKEND_SCHEMA_VERSION;
    readonly ready: boolean;
    readonly state: string;
}}

export interface BackendSuccessEnvelope<TData> {{
    readonly schema_version: typeof BACKEND_SCHEMA_VERSION;
    readonly request_id: string;
    readonly ok: true;
    readonly data: TData;
}}

export interface BackendFailureDetails {{
    readonly [key: string]: unknown;
}}

export interface BackendFailureEnvelope {{
    readonly schema_version: typeof BACKEND_SCHEMA_VERSION;
    readonly request_id: string;
    readonly ok: false;
    readonly error: {{
        readonly code: string;
        readonly message: string;
        readonly retryable: boolean;
        readonly details: BackendFailureDetails;
    }};
}}

export type BackendHttpEnvelope<TData> =
    | BackendSuccessEnvelope<TData>
    | BackendFailureEnvelope;

export interface BackendAuthenticateMessage {{
    readonly schema_version: typeof BACKEND_SCHEMA_VERSION;
    readonly type: 'AUTHENTICATE';
    readonly token: string;
    readonly after_sequence: number;
}}

export interface BackendEventEnvelope<TPayload = Readonly<Record<string, unknown>>> {{
    readonly schema_version: typeof BACKEND_SCHEMA_VERSION;
    readonly session_id: string;
    readonly event_id: string;
    readonly sequence: number;
    readonly occurred_at: string;
    readonly type: string;
    readonly aggregate_version: number | null;
    readonly correlation_id: string | null;
    readonly payload: TPayload;
}}

export interface BackendAccountUpdatedPayload {{
    readonly account: BackendAccountSnapshot;
}}

export interface BackendOrderExecutedPayload {{
    readonly trade: BackendTradeSnapshot;
}}

export interface BackendPerformanceUpdatedPayload {{
    readonly performance: BackendPerformanceSnapshot;
}}

export type BackendResyncReason = 'REPLAY_GAP' | 'SEQUENCE_AHEAD';

export interface BackendResyncRequired {{
    readonly schema_version: typeof BACKEND_SCHEMA_VERSION;
    readonly session_id: string;
    readonly type: 'RESYNC_REQUIRED';
    readonly reason: BackendResyncReason;
    readonly last_sequence: number;
}}
"""
