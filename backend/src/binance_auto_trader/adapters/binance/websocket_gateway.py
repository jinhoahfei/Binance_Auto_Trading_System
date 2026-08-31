"""공식 Binance Spot Kline과 account WebSocket stream을 정규화한다."""

from collections import OrderedDict
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
from threading import RLock
from typing import Callable, Protocol

from binance_auto_trader.domain.market import (
    Interval,
    Kline,
    SUPPORTED_INTERVALS,
)
from binance_auto_trader.domain.trading.account import (
    AccountSnapshot,
    AssetBalance,
)
from binance_auto_trader.domain.trading.order import (
    Fill,
    OrderResult,
    OrderStatus,
)


_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MAXIMUM_KLINE_EVENT_FINGERPRINTS = 4_096
_INTERVAL_MILLISECONDS_BY_INTERVAL = {
    Interval.ONE_MINUTE: 60_000,
    Interval.THIRTY_MINUTES: 1_800_000,
    Interval.FOUR_HOURS: 14_400_000,
    Interval.ONE_DAY: 86_400_000,
}
_ORDER_STATUS_PROGRESS = {
    OrderStatus.UNKNOWN: 0,
    OrderStatus.PENDING_NEW: 1,
    OrderStatus.NEW: 2,
    OrderStatus.PARTIALLY_FILLED: 3,
    OrderStatus.PENDING_CANCEL: 4,
    OrderStatus.FILLED: 5,
    OrderStatus.CANCELED: 5,
    OrderStatus.REJECTED: 5,
    OrderStatus.EXPIRED: 5,
    OrderStatus.EXPIRED_IN_MATCH: 5,
}


class Subscription(Protocol):
    """
    클래스 이름: Subscription
    기능: WebSocket client가 반환하는 구독 handle의 최소 종료 계약을 정의한다.
    작성 날짜: 2026/08/20
    """

    def close(self) -> None:
        """
        함수 이름: close()
        기능: 해당 WebSocket 구독을 종료한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        ...


class BinanceWebSocketClient(Protocol):
    """
    클래스 이름: BinanceWebSocketClient
    기능: 공식 Binance Kline과 account stream 구독 client 계약을 정의한다.
    작성 날짜: 2026/08/20
    """

    def subscribe_all_kline_streams(
        self,
        *,
        symbol: str,
        intervals: tuple[str, ...],
        on_message: Callable[[object], None],
        on_disconnect: Callable[[], None],
    ) -> Subscription:
        """
        함수 이름: subscribe_all_kline_streams()
        기능: 주어진 symbol과 interval의 Kline stream callback을 등록한다.
        인자: symbol -> 구독할 정규화 symbol
            intervals -> 구독할 공식 interval 문자열
            on_message -> 수신 payload callback
            on_disconnect -> 연결 종료 callback
        반환값: 생성된 구독 handle
        작성 날짜: 2026/08/20
        """
        ...

    def subscribe_account_info(
        self,
        *,
        on_message: Callable[[object], None],
        on_disconnect: Callable[[], None],
    ) -> Subscription:
        """
        함수 이름: subscribe_account_info()
        기능: 공식 Spot User Data Stream callback을 등록한다.
        인자: on_message -> 공식 WebSocket API event envelope callback
            on_disconnect -> 연결 종료 callback
        반환값: 생성된 account stream 구독 handle
        작성 날짜: 2026/08/21
        """
        ...


class KlineBufferStateError(RuntimeError):
    """
    클래스 이름: KlineBufferStateError
    기능: stale·끊김·손상 상태의 Kline buffer를 배출하거나 승격하려는 오류를 나타낸다.
    작성 날짜: 2026/08/20
    """


class AccountStreamStateError(RuntimeError):
    """
    클래스 이름: AccountStreamStateError
    기능: account stream이 stale·disconnect·callback 실패 상태임을 나타낸다.
    작성 날짜: 2026/08/21
    """


class _ManagedSubscription:
    """
    클래스 이름: _ManagedSubscription
    기능: transport 구독 종료를 멱등 처리하고 Gateway 상태와 동기화한다.
    작성 날짜: 2026/08/20
    """

    __slots__ = (
        "_close_lock",
        "_closed",
        "_on_closing",
        "_on_closed",
        "_transport_subscription",
    )

    def __init__(
        self,
        transport_subscription: Subscription,
        on_closed: Callable[[], None],
        *,
        on_closing: Callable[[], None] | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 주입 client handle과 Gateway 종료 callback을 보존한다.
        인자: transport_subscription -> 주입 client가 반환한 구독 handle
            on_closed -> Gateway 수신 상태를 종료할 callback
            on_closing -> transport close 전에 소유자 종료를 표시할 optional callback
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        if on_closing is not None and not callable(on_closing):
            raise TypeError("on_closing must be callable or None")

        # 소유자 close 표식은 transport가 동기 disconnect callback을 호출하기 전에 사용한다.
        self._transport_subscription = transport_subscription
        self._on_closing = on_closing
        self._on_closed = on_closed
        self._close_lock = RLock()
        self._closed = False

    def close(self) -> None:
        """
        함수 이름: close()
        기능: transport handle을 한 번만 닫고 성공 여부와 무관하게 Gateway를 종료한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        with self._close_lock:
            if self._closed:
                return
            self._closed = True

        # 일부 test double과 transport는 close 안에서 disconnect를 동기 호출하므로 먼저 의도를 기록한다.
        if self._on_closing is not None:
            self._on_closing()
        try:
            self._transport_subscription.close()
        finally:
            self._on_closed()

    @property
    def caught_up(self) -> bool:
        """
        함수 이름: caught_up()
        기능: transport가 공개한 account callback backlog readiness를 fail closed로 읽는다.
        인자: 없음
        반환값: handle이 열려 있고 transport backlog가 없으면 True
        작성 날짜: 2026/08/23
        """
        with self._close_lock:
            if self._closed:
                return False
            transport_subscription = self._transport_subscription

        # Phase 9 transport는 caught_up을 제공하며 기존 결정적 fake는 즉시 처리로 간주한다.
        try:
            transport_caught_up = getattr(
                transport_subscription,
                "caught_up",
                True,
            )
        except Exception:
            return False  # readiness 조회 자체가 실패하면 주문 가능 상태로 추측하지 않는다.

        return transport_caught_up is True


def _normalize_symbol(symbol: object) -> str:
    """
    함수 이름: _normalize_symbol()
    기능: Binance symbol을 공백 없는 ASCII 대문자로 정규화한다.
    인자: symbol -> 정규화할 runtime 값
    반환값: 정규화된 symbol
    작성 날짜: 2026/08/20
    """
    if not isinstance(symbol, str):
        raise TypeError("symbol must be a string")

    normalized_symbol = symbol.strip().upper()
    if (
        not normalized_symbol
        or not normalized_symbol.isascii()
        or not normalized_symbol.isalnum()
    ):
        raise ValueError(
            "symbol must contain only ASCII letters and digits"
        )

    return normalized_symbol


def _read_non_negative_integer(value: object, field_name: str) -> int:
    """
    함수 이름: _read_non_negative_integer()
    기능: WebSocket timestamp가 bool이 아닌 0 이상 정수인지 검증한다.
    인자: value -> 검증할 payload 값
        field_name -> 오류에 표시할 필드 이름
    반환값: 검증된 정수
    작성 날짜: 2026/08/20
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{field_name} must not be negative")

    return value


def _read_integer(value: object, field_name: str) -> int:
    """
    함수 이름: _read_integer()
    기능: WebSocket 정수 필드가 bool이 아닌 실제 정수인지 검증한다.
    인자: value -> 검증할 payload 값
        field_name -> 오류에 표시할 필드 이름
    반환값: 검증된 정수
    작성 날짜: 2026/08/22
    """
    # bool은 int 하위 타입이므로 명시적으로 제외해 wire 정수와 구분한다.
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")

    return value


def _read_positive_integer(value: object, field_name: str) -> int:
    """
    함수 이름: _read_positive_integer()
    기능: 거래소 주문 식별자가 0보다 큰 정수인지 검증한다.
    인자: value -> 검증할 payload 값
        field_name -> 오류에 표시할 필드 이름
    반환값: 검증된 양의 정수
    작성 날짜: 2026/08/22
    """
    integer_value = _read_integer(value, field_name)
    if integer_value <= 0:
        raise ValueError(f"{field_name} must be greater than zero")

    return integer_value


def _read_decimal_string(value: object, field_name: str) -> Decimal:
    """
    함수 이름: _read_decimal_string()
    기능: WebSocket decimal 문자열을 float 없이 유한 Decimal로 변환한다.
    인자: value -> 변환할 payload 값
        field_name -> 오류에 표시할 필드 이름
    반환값: 검증된 유한 Decimal
    작성 날짜: 2026/08/20
    """
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{field_name} must be a decimal string")

    try:
        decimal_value = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(
            f"{field_name} must contain a valid decimal"
        ) from error

    if not decimal_value.is_finite():
        raise ValueError(f"{field_name} must contain a finite decimal")

    return decimal_value


def _milliseconds_to_utc(value: object, field_name: str) -> datetime:
    """
    함수 이름: _milliseconds_to_utc()
    기능: Binance Unix millisecond를 float 없이 UTC datetime으로 변환한다.
    인자: value -> 변환할 timestamp 값
        field_name -> 오류에 표시할 필드 이름
    반환값: 변환된 UTC datetime
    작성 날짜: 2026/08/20
    """
    milliseconds = _read_non_negative_integer(value, field_name)

    try:
        return _UNIX_EPOCH + timedelta(milliseconds=milliseconds)
    except OverflowError as error:
        raise ValueError(f"{field_name} is outside datetime range") from error


def _parse_json_payload(payload: object) -> object:
    """
    함수 이름: _parse_json_payload()
    기능: WebSocket text JSON을 해석하고 이미 해석된 payload는 그대로 반환한다.
    인자: payload -> text 또는 JSON 호환 runtime 값
    반환값: 해석된 payload
    작성 날짜: 2026/08/20
    """
    if not isinstance(payload, str):
        return payload

    try:
        return json.loads(payload)
    except json.JSONDecodeError as error:
        raise ValueError(
            "Binance WebSocket payload must contain valid JSON"
        ) from error


def _read_kline_event(payload: object) -> tuple[Mapping[str, object], str | None]:
    """
    함수 이름: _read_kline_event()
    기능: raw 또는 combined stream payload에서 Kline event와 stream 이름을 분리한다.
    인자: payload -> 해석 전 또는 해석된 WebSocket payload
    반환값: Kline event mapping과 optional combined stream 이름
    작성 날짜: 2026/08/20
    """
    parsed_payload = _parse_json_payload(payload)
    if not isinstance(parsed_payload, Mapping):
        raise TypeError("Binance WebSocket payload must be an object")

    has_combined_field = (
        "stream" in parsed_payload or "data" in parsed_payload
    )
    if not has_combined_field:
        return parsed_payload, None

    stream_name = parsed_payload.get("stream")
    event_payload = parsed_payload.get("data")
    if not isinstance(stream_name, str) or not isinstance(
        event_payload,
        Mapping,
    ):
        raise TypeError(
            "combined payload must contain string stream and object data"
        )

    return event_payload, stream_name


def _validate_websocket_metadata(
    event_payload: Mapping[str, object],
    kline_payload: Mapping[str, object],
) -> None:
    """
    함수 이름: _validate_websocket_metadata()
    기능: 사용하지 않는 공식 Kline event metadata도 schema대로 검증한다.
    인자: event_payload -> 공식 Kline event object
        kline_payload -> event 내부의 공식 k object
    반환값: 없음
    작성 날짜: 2026/08/20
    """
    _read_non_negative_integer(event_payload.get("E"), "E")

    for field_name in ("f", "L", "n"):
        _read_non_negative_integer(
            kline_payload.get(field_name),
            f"k.{field_name}",
        )

    for field_name in ("q", "V", "Q"):
        decimal_value = _read_decimal_string(
            kline_payload.get(field_name),
            f"k.{field_name}",
        )
        if decimal_value < Decimal("0"):
            raise ValueError(f"k.{field_name} must not be negative")

    if not isinstance(kline_payload.get("B"), str):
        raise TypeError("k.B must be a string")


def _validate_kline_time_range(
    open_time_milliseconds: int,
    close_time_milliseconds: int,
    interval: Interval,
) -> None:
    """
    함수 이름: _validate_kline_time_range()
    기능: UTC Kline stream의 시작·종료 시각이 event interval과 일치하는지 검증한다.
    인자: open_time_milliseconds -> 봉 시작 Unix millisecond
        close_time_milliseconds -> 봉 종료 Unix millisecond
        interval -> event에 표시된 canonical interval
    반환값: 없음
    작성 날짜: 2026/08/20
    """
    interval_milliseconds = _INTERVAL_MILLISECONDS_BY_INTERVAL[interval]
    if open_time_milliseconds % interval_milliseconds != 0:
        raise ValueError(
            "Binance WebSocket Kline open time is not UTC-aligned"
        )

    expected_close_time = (
        open_time_milliseconds + interval_milliseconds - 1
    )
    if close_time_milliseconds != expected_close_time:
        raise ValueError(
            "Binance WebSocket Kline close time does not match its interval"
        )


def _parse_websocket_kline(payload: object) -> Kline:
    """
    함수 이름: _parse_websocket_kline()
    기능: 공식 raw 또는 combined Kline event를 내부 불변 Kline으로 정규화한다.
    인자: payload -> WebSocket에서 받은 payload
    반환값: 정규화된 Kline
    작성 날짜: 2026/08/20
    """
    event_payload, stream_name = _read_kline_event(payload)
    if event_payload.get("e") != "kline":
        raise ValueError("Binance WebSocket event type must be kline")

    event_symbol = _normalize_symbol(event_payload.get("s"))
    kline_payload = event_payload.get("k")
    if not isinstance(kline_payload, Mapping):
        raise TypeError("Binance WebSocket event must contain object k")

    kline_symbol = _normalize_symbol(kline_payload.get("s"))
    if event_symbol != kline_symbol:
        raise ValueError("event and Kline symbols must match")

    interval_value = kline_payload.get("i")
    if not isinstance(interval_value, str):
        raise TypeError("k.i must be a string")
    try:
        interval = Interval(interval_value)
    except ValueError as error:
        raise ValueError("k.i is not a supported interval") from error

    if stream_name is not None:
        expected_stream_name = (
            f"{event_symbol.lower()}@kline_{interval.value}"
        )
        if stream_name != expected_stream_name:
            raise ValueError("combined stream name must match its Kline")

    open_time_milliseconds = _read_non_negative_integer(
        kline_payload.get("t"),
        "k.t",
    )
    close_time_milliseconds = _read_non_negative_integer(
        kline_payload.get("T"),
        "k.T",
    )
    if close_time_milliseconds < open_time_milliseconds:
        raise ValueError("Binance WebSocket Kline closes before it opens")
    _validate_kline_time_range(
        open_time_milliseconds,
        close_time_milliseconds,
        interval,
    )

    closed = kline_payload.get("x")
    if not isinstance(closed, bool):
        raise TypeError("k.x must be a bool")

    _validate_websocket_metadata(event_payload, kline_payload)

    return Kline(
        symbol=event_symbol,
        interval=interval,
        open_time=_milliseconds_to_utc(kline_payload.get("t"), "k.t"),
        open=_read_decimal_string(kline_payload.get("o"), "k.o"),
        high=_read_decimal_string(kline_payload.get("h"), "k.h"),
        low=_read_decimal_string(kline_payload.get("l"), "k.l"),
        close=_read_decimal_string(kline_payload.get("c"), "k.c"),
        volume=_read_decimal_string(kline_payload.get("v"), "k.v"),
        closed=closed,
        event_time=_milliseconds_to_utc(event_payload.get("E"), "E"),
    )


def _read_account_event(payload: object) -> Mapping[str, object]:
    """
    함수 이름: _read_account_event()
    기능: 공식 WebSocket API envelope에서 User Data Stream event object를 꺼낸다.
    인자: payload -> JSON text 또는 해석된 공식 account stream envelope
    반환값: envelope의 event mapping
    작성 날짜: 2026/08/21
    """
    parsed_payload = _parse_json_payload(payload)
    if not isinstance(parsed_payload, Mapping):
        raise TypeError("Binance account stream payload must be an object")

    if "subscriptionId" in parsed_payload:
        _read_non_negative_integer(
            parsed_payload.get("subscriptionId"),
            "subscriptionId",
        )

    event_payload = parsed_payload.get("event")
    if not isinstance(event_payload, Mapping):
        raise TypeError("account stream envelope must contain object event")

    return event_payload


def _parse_account_stream_balance(payload: object) -> AssetBalance:
    """
    함수 이름: _parse_account_stream_balance()
    기능: outboundAccountPosition의 단일 B 항목을 AssetBalance로 정규화한다.
    인자: payload -> 공식 account balance patch object
    반환값: 정규화된 불변 AssetBalance
    작성 날짜: 2026/08/21
    """
    if not isinstance(payload, Mapping):
        raise TypeError("account stream balance must be an object")

    asset = payload.get("a")
    if not isinstance(asset, str):
        raise TypeError("account stream asset must be a string")

    free = _read_decimal_string(payload.get("f"), "B.f")
    locked = _read_decimal_string(payload.get("l"), "B.l")
    if free < Decimal("0") or locked < Decimal("0"):
        raise ValueError("account stream balances must not be negative")

    return AssetBalance(
        asset=asset,
        free=free,
        locked=locked,
    )


def _parse_account_info_snapshot(
    payload: object,
) -> tuple[
    AccountSnapshot,
    int,
    tuple[tuple[str, Decimal, Decimal], ...],
] | None:
    """
    함수 이름: _parse_account_info_snapshot()
    기능: 공식 outboundAccountPosition event를 부분 AccountSnapshot과 dedup key로 변환한다.
    인자: payload -> JSON text 또는 해석된 공식 WebSocket API envelope
    반환값: account snapshot, update time, canonical balance fingerprint 또는 다른 event면 None
    작성 날짜: 2026/08/21
    """
    event_payload = _read_account_event(payload)
    event_type = event_payload.get("e")
    if not isinstance(event_type, str):
        raise TypeError("account event type must be a string")
    if not event_type or event_type != event_type.strip():
        raise ValueError("account event type must be non-empty and trimmed")
    if event_type == "eventStreamTerminated":
        _read_non_negative_integer(event_payload.get("E"), "event.E")
        raise AccountStreamStateError(
            "Binance account user data stream terminated"
        )
    if event_type != "outboundAccountPosition":
        return None

    _read_non_negative_integer(event_payload.get("E"), "event.E")
    update_time_milliseconds = _read_non_negative_integer(
        event_payload.get("u"),
        "event.u",
    )
    raw_balances = event_payload.get("B")
    if not isinstance(raw_balances, (list, tuple)):
        raise TypeError("outboundAccountPosition B must be an array")

    balances = tuple(
        _parse_account_stream_balance(raw_balance)
        for raw_balance in raw_balances
    )
    snapshot = AccountSnapshot(
        balances=balances,
        updated_at=_milliseconds_to_utc(
            update_time_milliseconds,
            "event.u",
        ),
        is_full_snapshot=False,
    )
    fingerprint = tuple(
        sorted(
            (
                balance.asset,
                balance.free,
                balance.locked,
            )
            for balance in balances
        )
    )

    return snapshot, update_time_milliseconds, fingerprint


@dataclass(frozen=True, slots=True)
class _ExecutionReportObservation:
    """
    클래스 이름: _ExecutionReportObservation
    기능: executionReport의 주문 상태·누적 수량·선택 체결을 정규화 전 단계로 보존한다.
    작성 날짜: 2026/08/22
    """

    symbol: str
    client_order_id: str
    status: OrderStatus
    processed_at: datetime
    exchange_order_id: str
    cumulative_quantity: Decimal
    source_cursor: tuple[int, int]
    fill: Fill | None
    failure_reason: str | None


def _read_trimmed_text(value: object, field_name: str) -> str:
    """
    함수 이름: _read_trimmed_text()
    기능: User Data Stream 식별자와 enum 문자열의 비어 있지 않은 canonical 형식을 검증한다.
    인자: value -> 검증할 payload 값
        field_name -> 오류에 표시할 필드 이름
    반환값: 검증된 원래 문자열
    작성 날짜: 2026/08/22
    """
    # 식별자를 임의 trim해 다른 주문과 합치지 않고 wire 값 자체를 검증한다.
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value or value != value.strip():
        raise ValueError(f"{field_name} must be non-empty and trimmed")

    return value


def _resolve_execution_client_order_id(
    event_payload: Mapping[str, object],
) -> str:
    """
    함수 이름: _resolve_execution_client_order_id()
    기능: 일반 주문 ID와 취소 event의 원 주문 client ID를 하나의 상관 ID로 선택한다.
    인자: event_payload -> 공식 executionReport event object
    반환값: domain Order가 사용하는 원 주문 client order ID
    작성 날짜: 2026/08/22
    """
    current_client_order_id = _read_trimmed_text(
        event_payload.get("c"),
        "event.c",
    )
    original_client_order_id = event_payload.get("C")
    if original_client_order_id in (None, ""):
        return current_client_order_id
    if not isinstance(original_client_order_id, str):
        raise TypeError("event.C must be a string")
    if original_client_order_id != original_client_order_id.strip():
        raise ValueError("event.C must not contain outer whitespace")

    return original_client_order_id  # 취소 응답의 c는 취소 요청 ID이므로 C로 원 주문을 상관시킨다.


def _parse_execution_failure_reason(
    event_payload: Mapping[str, object],
) -> str | None:
    """
    함수 이름: _parse_execution_failure_reason()
    기능: 주문 거절 또는 만료 이유를 credential 없는 선택 failure 문자열로 정규화한다.
    인자: event_payload -> 공식 executionReport event object
    반환값: 구체 failure reason 또는 정상 상태이면 None
    작성 날짜: 2026/08/22
    """
    reject_reason = _read_trimmed_text(
        event_payload.get("r"),
        "event.r",
    )
    if reject_reason != "NONE":
        return reject_reason

    expiry_reason = event_payload.get("eR")
    if expiry_reason is None:
        return None
    parsed_expiry_reason = _read_trimmed_text(expiry_reason, "event.eR")
    if parsed_expiry_reason == "NONE":
        return None

    return parsed_expiry_reason


def _parse_execution_fill(
    event_payload: Mapping[str, object],
    *,
    exchange_order_id: str,
    execution_type: str,
) -> Fill | None:
    """
    함수 이름: _parse_execution_fill()
    기능: TRADE execution의 마지막 체결과 수수료를 Fill로 만들고 비체결 event를 검증한다.
    인자: event_payload -> 공식 executionReport event object
        exchange_order_id -> 검증을 마친 거래소 주문 ID
        execution_type -> event.x의 공식 실행 유형
    반환값: 실제 TRADE이면 Fill, 상태 event이면 None
    작성 날짜: 2026/08/22
    """
    last_quantity = _read_decimal_string(event_payload.get("l"), "event.l")
    last_price = _read_decimal_string(event_payload.get("L"), "event.L")
    fee_amount = _read_decimal_string(event_payload.get("n"), "event.n")
    trade_id = _read_integer(event_payload.get("t"), "event.t")
    if (
        last_quantity < Decimal("0")
        or last_price < Decimal("0")
        or fee_amount < Decimal("0")
    ):
        raise ValueError("execution quantities, price and commission must not be negative")

    # 비체결 상태 event에 실제 trade 식별자나 수량이 섞이면 누적 결과를 추측하지 않는다.
    if execution_type != "TRADE":
        if last_quantity != Decimal("0") or trade_id != -1:
            raise ValueError("non-TRADE execution must not contain a fill")
        return None

    if last_quantity <= Decimal("0") or last_price <= Decimal("0"):
        raise ValueError("TRADE execution quantity and price must be positive")
    if trade_id < 0:
        raise ValueError("TRADE execution must contain a non-negative trade ID")
    fee_asset = _read_trimmed_text(event_payload.get("N"), "event.N")
    if fee_asset == "USDT":
        fee_quote_amount = fee_amount
    elif fee_asset == "ETH":
        fee_quote_amount = fee_amount * last_price
    else:
        fee_quote_amount = Decimal("0")  # Fill이 미지원 fee asset을 typed reconciliation 오류로 닫는다.

    return Fill(
        exchange_order_id=exchange_order_id,
        trade_id=str(trade_id),
        quantity=last_quantity,
        price=last_price,
        fee_amount=fee_amount,
        fee_asset=fee_asset,
        fee_quote_amount=fee_quote_amount,
        executed_at=_milliseconds_to_utc(event_payload.get("T"), "event.T"),
    )


def _parse_execution_report(payload: object) -> _ExecutionReportObservation:
    """
    함수 이름: _parse_execution_report()
    기능: 공식 Spot executionReport envelope를 주문 관찰 값과 선택 Fill로 정규화한다.
    인자: payload -> JSON text 또는 해석된 WebSocket API envelope
    반환값: source cursor와 cumulative quantity를 포함한 execution 관찰 값
    작성 날짜: 2026/08/22
    """
    event_payload = _read_account_event(payload)
    if event_payload.get("e") != "executionReport":
        raise ValueError("account event type must be executionReport")

    # event time과 transaction time을 모두 검증하되 주문 순서는 T와 execution ID로 판정한다.
    _read_non_negative_integer(event_payload.get("E"), "event.E")
    transaction_time = _read_non_negative_integer(
        event_payload.get("T"),
        "event.T",
    )
    execution_id = _read_non_negative_integer(
        event_payload.get("I"),
        "event.I",
    )
    exchange_order_id = str(
        _read_positive_integer(event_payload.get("i"), "event.i")
    )

    status_text = _read_trimmed_text(event_payload.get("X"), "event.X")
    try:
        status = OrderStatus(status_text)
    except ValueError as error:
        raise ValueError("event.X is not a supported order status") from error
    execution_type = _read_trimmed_text(
        event_payload.get("x"),
        "event.x",
    )
    cumulative_quantity = _read_decimal_string(
        event_payload.get("z"),
        "event.z",
    )
    if cumulative_quantity < Decimal("0"):
        raise ValueError("event.z must not be negative")

    fill = _parse_execution_fill(
        event_payload,
        exchange_order_id=exchange_order_id,
        execution_type=execution_type,
    )
    if fill is not None and fill.quantity > cumulative_quantity:
        raise ValueError("last executed quantity must not exceed cumulative quantity")

    return _ExecutionReportObservation(
        symbol=_normalize_symbol(event_payload.get("s")),
        client_order_id=_resolve_execution_client_order_id(event_payload),
        status=status,
        processed_at=_milliseconds_to_utc(transaction_time, "event.T"),
        exchange_order_id=exchange_order_id,
        cumulative_quantity=cumulative_quantity,
        source_cursor=(transaction_time, execution_id),
        fill=fill,
        failure_reason=_parse_execution_failure_reason(event_payload),
    )


class WebSocketGateway:
    """
    클래스 이름: WebSocketGateway
    기능: Kline buffer와 account stream을 독립 세대·callback 계약으로 정규화한다.
    작성 날짜: 2026/08/20
    """

    def __init__(
        self,
        web_socket_client: BinanceWebSocketClient,
        account_snapshot_callback: Callable[[AccountSnapshot], object]
        | None = None,
        order_result_callback: Callable[[OrderResult], object] | None = None,
        reconciliation_required_callback: Callable[[str], object]
        | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 실제 구독 client와 Kline/account stream의 독립 초기 상태를 준비한다.
        인자: web_socket_client -> Kline 또는 account stream을 구독할 client
            account_snapshot_callback -> 정규화 account patch를 적용할 callback
            order_result_callback -> 정규화 executionReport를 적용할 callback
            reconciliation_required_callback -> stream gap을 복원하도록 알릴 callback
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        provides_kline_operation = callable(
            getattr(
                web_socket_client,
                "subscribe_all_kline_streams",
                None,
            )
        )
        provides_account_operation = callable(
            getattr(web_socket_client, "subscribe_account_info", None)
        )
        if web_socket_client is None or not (
            provides_kline_operation or provides_account_operation
        ):
            raise TypeError(
                "web_socket_client must provide a supported subscription"
            )
        if (
            account_snapshot_callback is not None
            and not callable(account_snapshot_callback)
        ):
            raise TypeError("account_snapshot_callback must be callable")
        if order_result_callback is not None and not callable(
            order_result_callback
        ):
            raise TypeError("order_result_callback must be callable")
        if (
            reconciliation_required_callback is not None
            and not callable(reconciliation_required_callback)
        ):
            raise TypeError(
                "reconciliation_required_callback must be callable"
            )

        self._web_socket_client = web_socket_client
        self._account_snapshot_callback = account_snapshot_callback
        self._order_result_callback = order_result_callback
        self._reconciliation_required_callback = (
            reconciliation_required_callback
        )
        self._lock = RLock()
        self._kline_delivery_lock = RLock()
        self._generation = 0
        self._active_subscription: Subscription | None = None
        self._active_symbol: str | None = None
        self._active_intervals: tuple[Interval, ...] = ()
        self._connected = False
        self._buffer_error: Exception | None = None
        # Kline mode와 wire event fingerprint를 현재 generation 안에서 함께 교체한다.
        self._kline_live_observer: Callable[[Kline], object] | None = None
        self._kline_event_fingerprints: OrderedDict[
            tuple[str, Interval, datetime, datetime],
            Kline,
        ] = OrderedDict()
        self._kline_cursor_by_interval: dict[Interval, Kline] = {}
        self._kline_buffer: dict[
            Interval,
            dict[datetime, Kline],
        ] = {}
        # Account readiness는 현재 generation의 transport handle과 연결 flag를 함께 추적한다.
        self._account_generation = 0
        self._account_subscription: Subscription | None = None
        self._account_connected = False
        self._account_error: Exception | None = None
        self._account_update_time_milliseconds: int | None = None
        self._account_fingerprints: set[
            tuple[tuple[str, Decimal, Decimal], ...]
        ] = set()
        self._reconciliation_notified_generation: int | None = None

        # Order stream 사실은 reconnect 뒤 중복 event도 제거하도록 Gateway 수명 동안 보존한다.
        self._order_client_ids_by_exchange_id: dict[str, str] = {}
        self._order_source_cursors: dict[str, tuple[int, int]] = {}
        self._order_latest_observations: dict[
            str,
            _ExecutionReportObservation,
        ] = {}
        self._order_latest_statuses: dict[str, OrderStatus] = {}
        self._order_failure_reasons: dict[str, str | None] = {}
        self._order_fills_by_exchange_id: dict[
            str,
            dict[tuple[str, str], Fill],
        ] = {}

    @property
    def kline_replay_buffer_size(self) -> int:
        """
        함수 이름: kline_replay_buffer_size()
        기능: 현재 Kline generation이 보존한 bounded duplicate fingerprint 수를 반환한다.
        인자: 없음
        반환값: 0에서 고정 상한 사이의 fingerprint 수
        작성 날짜: 2026/08/25
        """
        # Soak observer가 내부 container를 직접 참조하지 않고 누수 상한만 검증하게 한다.
        with self._lock:
            return len(self._kline_event_fingerprints)

    @property
    def kline_live_ready(self) -> bool:
        """
        함수 이름: kline_live_ready()
        기능: 현재 Kline 세대가 연결되고 손상 없이 live observer까지 승격됐는지 반환한다.
        인자: 없음
        반환값: 새 시장 effect가 현재 세대를 신뢰할 수 있으면 True
        작성 날짜: 2026/08/25
        """
        # 연결 flag만으로 초기 buffer 세대를 거래 가능 상태로 오인하지 않게 네 조건을 결합한다.
        with self._lock:
            return (
                self._connected
                and self._active_subscription is not None
                and self._buffer_error is None
                and self._kline_live_observer is not None
            )

    @property
    def account_connected(self) -> bool:
        """
        함수 이름: account_connected()
        기능: 현재 account stream 세대가 연결 상태인지 thread-safe 방식으로 반환한다.
        인자: 없음
        반환값: account stream이 연결되어 있으면 True
        작성 날짜: 2026/08/21
        """
        # start 실패, disconnect와 close callback이 갱신하는 동일 lock의 값을 읽는다.
        with self._lock:
            return (
                self._account_connected
                and self._account_subscription is not None
            )  # transport handle이 실제 성립한 뒤에만 command readiness를 공개한다.

    @property
    def account_caught_up(self) -> bool:
        """
        함수 이름: account_caught_up()
        기능: 현재 account stream 세대의 downstream callback backlog가 비었는지 반환한다.
        인자: 없음
        반환값: 현재 transport가 모든 수신 account event를 적용했으면 True
        작성 날짜: 2026/08/23
        """
        with self._lock:
            if (
                not self._account_connected
                or self._account_subscription is None
            ):
                return False
            subscription = self._account_subscription

        # 실제 managed handle은 backlog를 공개하고 기존 즉시 fake handle은 backlog가 없다고 본다.
        try:
            caught_up = getattr(subscription, "caught_up", True) is True
        except Exception:
            caught_up = False  # readiness accessor 실패도 주문 허용으로 해석하지 않는다.
        with self._lock:
            # 조회 중 reconnect된 이전 handle의 readiness는 현재 세대에 재사용하지 않는다.
            return (
                self._account_connected
                and self._account_subscription is subscription
                and caught_up
            )

    @property
    def account_ready(self) -> bool:
        """
        함수 이름: account_ready()
        기능: account transport 연결과 event callback 최신성을 하나의 주문 gate로 결합한다.
        인자: 없음
        반환값: 연결된 현재 세대에 미처리 account event가 없으면 True
        작성 날짜: 2026/08/23
        """
        return self.account_connected and self.account_caught_up

    def rebase_order_results(
        self,
        results: Collection[OrderResult],
    ) -> None:
        """
        함수 이름: rebase_order_results()
        기능: REST full reconciliation 결과로 stream 주문 fill 누적기와 상태 기준을 원자 갱신한다.
        인자: results -> 같은 주문 ID 조회로 확인한 authoritative OrderResult 모음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        if not isinstance(results, (tuple, list)):
            raise TypeError("results must be a tuple or list")
        if any(not isinstance(result, OrderResult) for result in results):
            raise TypeError("results must contain OrderResult values")

        with self._lock:
            # 전체 모음 검증 실패가 기존 누적기를 일부만 바꾸지 않도록 복사본에서 먼저 병합한다.
            next_client_ids = dict(self._order_client_ids_by_exchange_id)
            next_cursors = dict(self._order_source_cursors)
            next_observations = dict(self._order_latest_observations)
            next_statuses = dict(self._order_latest_statuses)
            next_failure_reasons = dict(self._order_failure_reasons)
            next_fills = {
                exchange_order_id: dict(fills_by_key)
                for exchange_order_id, fills_by_key in (
                    self._order_fills_by_exchange_id.items()
                )
            }

            for result in results:
                exchange_order_id = result.exchange_order_id
                if exchange_order_id is None:
                    raise AccountStreamStateError(
                        "REST order rebase requires an exchange order ID"
                    )
                known_client_order_id = next_client_ids.get(exchange_order_id)
                if (
                    known_client_order_id is not None
                    and known_client_order_id != result.client_order_id
                ):
                    raise AccountStreamStateError(
                        "REST order rebase changed a client order ID"
                    )

                fills_by_key = next_fills.setdefault(exchange_order_id, {})
                for fill in result.fills:
                    existing_fill = fills_by_key.get(fill.key)
                    if existing_fill is not None and existing_fill != fill:
                        raise AccountStreamStateError(
                            "REST order rebase has conflicting fill content"
                        )
                    fills_by_key[fill.key] = fill

                # REST 상태는 기존 stream 사실과 단조 병합하고 다음 세대 source cursor만 비운다.
                next_client_ids[exchange_order_id] = result.client_order_id
                next_statuses[exchange_order_id] = (
                    self._resolve_stream_order_status(
                        next_statuses.get(exchange_order_id),
                        result.status,
                    )
                )
                next_failure_reasons[exchange_order_id] = (
                    result.failure_reason
                )
                next_cursors.pop(exchange_order_id, None)
                next_observations.pop(exchange_order_id, None)

            # 모든 상관관계와 fill 충돌 검증을 통과한 뒤 일곱 map identity를 함께 교체한다.
            self._order_client_ids_by_exchange_id = next_client_ids
            self._order_source_cursors = next_cursors
            self._order_latest_observations = next_observations
            self._order_latest_statuses = next_statuses
            self._order_failure_reasons = next_failure_reasons
            self._order_fills_by_exchange_id = next_fills

    def start_all_kline_buffering(
        self,
        symbol: str,
        intervals: Collection[Interval],
        *,
        reconciliation_required_callback: Callable[[str], object]
        | None = None,
    ) -> Subscription:
        """
        함수 이름: start_all_kline_buffering()
        기능: 새 구독 세대를 활성화하고 수신 Kline을 빈 초기화 buffer에 쌓는다.
        인자: symbol -> 구독할 Binance Spot symbol
            intervals -> 구독할 canonical interval set 또는 내부 tuple
            reconciliation_required_callback -> 현재 세대 장애를 full-resync owner에 알릴 callback
        반환값: 새 WebSocket 구독 handle
        작성 날짜: 2026/08/20
        """
        subscribe_all_kline_streams = getattr(
            self._web_socket_client,
            "subscribe_all_kline_streams",
            None,
        )
        if not callable(subscribe_all_kline_streams):
            raise TypeError(
                "web_socket_client must provide subscribe_all_kline_streams"
            )
        if (
            reconciliation_required_callback is not None
            and not callable(reconciliation_required_callback)
        ):
            raise TypeError(
                "reconciliation_required_callback must be callable or None"
            )

        normalized_symbol = _normalize_symbol(symbol)
        if not isinstance(intervals, (set, frozenset, tuple)):
            raise TypeError("intervals must be a set, frozenset, or tuple")
        if not intervals:
            raise ValueError("intervals must not be empty")
        if any(
            not isinstance(interval, Interval)
            for interval in intervals
        ):
            raise TypeError("intervals must contain canonical Intervals")
        if len(set(intervals)) != len(intervals):
            raise ValueError("intervals must not contain duplicates")
        if any(
            interval not in SUPPORTED_INTERVALS
            for interval in intervals
        ):
            raise ValueError("intervals contain an unsupported interval")
        normalized_intervals = tuple(
            interval
            for interval in SUPPORTED_INTERVALS
            if interval in intervals
        )

        # 진행 중인 이전 observer가 snapshot을 바꾼 뒤에만 새 generation identity를 공개한다.
        with self._kline_delivery_lock:
            with self._lock:
                self._generation += 1
                generation = self._generation
                previous_subscription = self._active_subscription
                self._active_subscription = None
                self._active_symbol = normalized_symbol
                self._active_intervals = normalized_intervals
                self._connected = True
                self._buffer_error = None
                self._kline_live_observer = None
                self._kline_event_fingerprints = OrderedDict()
                self._kline_cursor_by_interval = {}
                self._kline_buffer = {
                    interval: {}
                    for interval in normalized_intervals
                }

        if previous_subscription is not None:
            try:
                previous_subscription.close()
            except Exception:
                self._mark_disconnected(generation)
                raise

        def on_message(payload: object) -> None:
            """
            함수 이름: on_message()
            기능: 현재 구독 세대 payload를 검증해 Gateway buffer에 저장한다.
            인자: payload -> WebSocket client가 전달한 Kline payload
            반환값: 없음
            작성 날짜: 2026/08/20
            """
            self._buffer_kline_message(
                payload,
                generation,
                reconciliation_required_callback,
            )

        owner_close_started = False

        def on_closing() -> None:
            """
            함수 이름: on_closing()
            기능: transport close보다 먼저 현재 handle의 소유자 종료 의도를 표시한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/08/25
            """
            # Flag를 먼저 보이게 한 뒤 같은 generation의 connected 상태를 멱등 종료한다.
            nonlocal owner_close_started
            owner_close_started = True
            self._mark_disconnected(generation)

        def on_disconnect() -> None:
            """
            함수 이름: on_disconnect()
            기능: 현재 구독 세대가 끊겼음을 기록해 불완전 buffer commit을 차단한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/08/20
            """
            disconnected = self._mark_disconnected(generation)
            if (
                disconnected
                and not owner_close_started
                and reconciliation_required_callback is not None
            ):
                reconciliation_required_callback(
                    "kline_stream_disconnected"
                )  # 현재 세대의 최초 비정상 종료만 상위 full-resync owner에 전달한다.

        def on_closed() -> None:
            """
            함수 이름: on_closed()
            기능: 소유자가 닫은 Kline handle을 장애 알림 없이 종료 상태로 확정한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/08/25
            """
            self._mark_disconnected(generation)

        try:
            transport_subscription = (
                subscribe_all_kline_streams(
                    symbol=normalized_symbol,
                    intervals=tuple(
                        interval.value
                        for interval in normalized_intervals
                    ),
                    on_message=on_message,
                    on_disconnect=on_disconnect,
                )
            )
        except Exception:
            self._mark_disconnected(generation)
            raise

        if transport_subscription is None:
            self._mark_disconnected(generation)
            raise TypeError("WebSocket client returned no subscription")
        subscription = _ManagedSubscription(
            transport_subscription,
            on_closed,
            on_closing=on_closing,
        )

        with self._lock:
            subscription_start_failed = (
                generation != self._generation or not self._connected
            )
            if not subscription_start_failed:
                self._active_subscription = subscription

        if subscription_start_failed:
            subscription.close()
            raise KlineBufferStateError(
                "Kline subscription became stale or disconnected during startup"
            )

        return subscription

    def start_account_info_stream(self) -> Subscription:
        """
        함수 이름: start_account_info_stream()
        기능: 새 account 구독 세대를 시작하고 정규화된 balance patch callback을 전달한다.
        인자: 없음
        반환값: 새 account stream 구독 handle
        작성 날짜: 2026/08/21
        """
        if (
            self._account_snapshot_callback is None
            and self._order_result_callback is None
        ):
            raise AccountStreamStateError(
                "account or order stream callback is not configured"
            )
        subscribe_account_info = getattr(
            self._web_socket_client,
            "subscribe_account_info",
            None,
        )
        if not callable(subscribe_account_info):
            raise TypeError(
                "web_socket_client must provide subscribe_account_info"
            )

        with self._lock:
            self._account_generation += 1
            generation = self._account_generation
            previous_subscription = self._account_subscription
            self._account_subscription = None
            self._account_connected = True
            self._account_error = None
            self._account_update_time_milliseconds = None
            self._account_fingerprints = set()
            self._reconciliation_notified_generation = None

        if previous_subscription is not None:
            try:
                previous_subscription.close()
            except Exception:
                self._mark_account_disconnected(generation)
                raise

        def on_message(payload: object) -> None:
            """
            함수 이름: on_message()
            기능: 현재 account 구독 세대 payload를 검증·dedup한 뒤 callback에 전달한다.
            인자: payload -> WebSocket client가 전달한 User Data Stream envelope
            반환값: 없음
            작성 날짜: 2026/08/21
            """
            self._handle_account_info_message(payload, generation)

        def on_disconnect() -> None:
            """
            함수 이름: on_disconnect()
            기능: 현재 account 구독 세대가 끊겼음을 기록한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/08/21
            """
            self._mark_account_disconnected(
                generation,
                require_reconciliation=True,
                reason="account_stream_disconnected",
            )

        def on_closed() -> None:
            """
            함수 이름: on_closed()
            기능: 소유자가 닫은 현재 account 구독을 재조정 알림 없이 종료한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/08/22
            """
            self._mark_account_disconnected(
                generation,
                require_reconciliation=False,
                reason="account_stream_closed",
            )

        try:
            transport_subscription = subscribe_account_info(
                on_message=on_message,
                on_disconnect=on_disconnect,
            )
        except Exception:
            self._mark_account_disconnected(generation)
            raise

        if transport_subscription is None:
            self._mark_account_disconnected(generation)
            raise TypeError("WebSocket client returned no subscription")

        subscription = _ManagedSubscription(
            transport_subscription,
            on_closed,
        )
        with self._lock:
            subscription_start_failed = (
                generation != self._account_generation
                or not self._account_connected
                or self._account_error is not None
            )
            if not subscription_start_failed:
                self._account_subscription = subscription

        if subscription_start_failed:
            subscription.close()
            raise AccountStreamStateError(
                "account subscription became stale or failed during startup"
            )

        return subscription

    def drain_kline_buffer(
        self,
        subscription: Subscription,
    ) -> dict[Interval, tuple[Kline, ...]]:
        """
        함수 이름: drain_kline_buffer()
        기능: 현재 초기화 buffer를 원자적으로 배출하고 해당 수신 세대를 종료한다.
        인자: subscription -> start가 반환한 현재 구독 handle
        반환값: active interval별 시간순 Kline tuple mapping
        작성 날짜: 2026/08/20
        """
        # Buffer 수신과 generation 종료를 같은 delivery 순서에 넣어 drain 뒤 mutation을 막는다.
        with self._kline_delivery_lock:
            with self._lock:
                if subscription is not self._active_subscription:
                    raise KlineBufferStateError(
                        "subscription is not the active Kline buffer"
                    )
                if not self._connected:
                    raise KlineBufferStateError(
                        "Kline stream disconnected before buffer drain"
                    )
                if self._buffer_error is not None:
                    raise KlineBufferStateError(
                        "invalid Kline payload was received before buffer drain"
                    ) from self._buffer_error
                if self._kline_live_observer is not None:
                    raise KlineBufferStateError(
                        "live Kline observer is already active"
                    )

                drained_buffer = {
                    interval: tuple(
                        sorted(
                            self._kline_buffer[interval].values(),
                            key=lambda kline: kline.open_time,
                        )
                    )
                    for interval in self._active_intervals
                }
                self._generation += 1
                self._active_subscription = None
                self._connected = False
                self._kline_event_fingerprints = OrderedDict()
                self._kline_cursor_by_interval = {}
                self._kline_buffer = {
                    interval: {}
                    for interval in self._active_intervals
                }

        return drained_buffer

    def promote_kline_buffer_to_live(
        self,
        observer: Callable[[Kline], object],
        subscription: Subscription | None = None,
    ) -> Subscription:
        """
        함수 이름: promote_kline_buffer_to_live()
        기능: 현재 세대 buffer를 순서대로 전달한 뒤 같은 구독을 live observer로 무손실 승격한다.
        인자: observer -> 정규화 Kline을 연속 처리할 callback
            subscription -> 선택적으로 검증할 현재 구독 handle
        반환값: 재구독하지 않은 동일한 현재 구독 handle
        작성 날짜: 2026/08/25
        """
        # callback 형식 오류는 현재 구독 상태를 읽기 전에 닫는다.
        if not callable(observer):
            raise TypeError("observer must be callable")

        # Delivery lock을 먼저 잡아 buffer replay가 이후 live callback보다 반드시 앞서게 한다.
        with self._kline_delivery_lock:
            with self._lock:
                # 선택 handle, 연결, latched parse 오류와 중복 promotion을 한 lock에서 확인한다.
                active_subscription = self._active_subscription
                if active_subscription is None:
                    raise KlineBufferStateError(
                        "no active Kline buffer is available"
                    )
                if (
                    subscription is not None
                    and subscription is not active_subscription
                ):
                    raise KlineBufferStateError(
                        "subscription is not the active Kline buffer"
                    )
                if not self._connected:
                    raise KlineBufferStateError(
                        "Kline stream disconnected before live promotion"
                    )
                if self._buffer_error is not None:
                    raise KlineBufferStateError(
                        "invalid Kline payload was received before live promotion"
                    ) from self._buffer_error
                if self._kline_live_observer is not None:
                    raise KlineBufferStateError(
                        "live Kline observer is already active"
                    )

                # interval 간에도 Binance E를 우선해 결정적 buffer 전달 순서를 만든다.
                buffered_klines = tuple(
                    sorted(
                        (
                            kline
                            for interval_buffer in self._kline_buffer.values()
                            for kline in interval_buffer.values()
                        ),
                        key=self._kline_delivery_key,
                    )
                )
                self._kline_buffer = {
                    interval: {}
                    for interval in self._active_intervals
                }
                self._kline_live_observer = observer

            try:
                # Gateway lock은 callback 전에 놓아 application lock과의 역순 교착을 방지한다.
                for buffered_kline in buffered_klines:
                    observer(buffered_kline)
            except Exception as error:
                with self._lock:
                    if (
                        active_subscription is self._active_subscription
                        and self._buffer_error is None
                    ):
                        self._buffer_error = error
                raise KlineBufferStateError(
                    "Kline observer failed during live promotion"
                ) from error

            return active_subscription

    @staticmethod
    def _kline_delivery_key(
        kline: Kline,
    ) -> tuple[datetime, datetime, str]:
        """
        함수 이름: _kline_delivery_key()
        기능: buffer 승격 시 event 시각, 봉 시각, interval 순의 결정적 전달 key를 만든다.
        인자: kline -> 정렬할 WebSocket Kline
        반환값: UTC event 시각과 봉 시각 및 interval 문자열 tuple
        작성 날짜: 2026/08/25
        """
        # REST Kline처럼 E가 없는 값은 live replay 순서 key로 사용할 수 없다.
        if kline.event_time is None:
            raise KlineBufferStateError(
                "WebSocket Kline must contain an event time"
            )

        return (
            kline.event_time,
            kline.open_time,
            kline.interval.value,
        )  # 동일 E에서도 open time과 interval로 결정적 순서를 완성한다.

    def _buffer_kline_message(
        self,
        payload: object,
        generation: int,
        reconciliation_required_callback: Callable[[str], object]
        | None = None,
    ) -> None:
        """
        함수 이름: _buffer_kline_message()
        기능: 현재 연결 세대의 정규화 Kline만 dedup buffer에 마지막 값으로 저장한다.
        인자: payload -> WebSocket에서 받은 raw 또는 combined payload
            generation -> callback이 속한 구독 세대
            reconciliation_required_callback -> 현재 세대 오류를 full-resync owner에 알릴 callback
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        callback_error: Exception | None = None
        # Delivery와 state lock 순서를 고정해 live callback 순서와 generation 교체를 직렬화한다.
        with self._kline_delivery_lock:
            try:
                with self._lock:
                    if generation != self._generation:
                        return
                    if self._buffer_error is not None:
                        raise self._buffer_error  # 첫 손상 원인을 유지해 이후 payload도 같은 세대에서 fail closed한다.
                    if not self._connected:
                        return

                    kline = _parse_websocket_kline(payload)
                    if kline.symbol != self._active_symbol:
                        raise ValueError(
                            "WebSocket Kline symbol is not subscribed"
                        )
                    if kline.interval not in self._active_intervals:
                        raise ValueError(
                            "WebSocket Kline interval is not subscribed"
                        )
                    if kline.event_time is None:
                        raise ValueError(
                            "WebSocket Kline event time is required"
                        )

                    # symbol·interval·openTime·eventTime 전체 key로 wire 재전달을 멱등 처리한다.
                    event_key = (
                        kline.symbol,
                        kline.interval,
                        kline.open_time,
                        kline.event_time,
                    )
                    existing_kline = self._kline_event_fingerprints.get(
                        event_key
                    )
                    if existing_kline is not None:
                        if existing_kline != kline:
                            raise ValueError(
                                "duplicate Kline event key changed payload"
                            )
                        return  # 완전히 같은 wire event 재전달은 observer와 snapshot에 다시 반영하지 않는다.

                    # Bounded fingerprint에서 제거된 오래된 event도 interval cursor 뒤로 역행하지 못한다.
                    previous_kline = self._kline_cursor_by_interval.get(
                        kline.interval
                    )
                    if previous_kline is not None:
                        if previous_kline.event_time is None:
                            raise RuntimeError(
                                "Kline stream cursor lost its event time"
                            )
                        if kline == previous_kline:
                            return  # 다른 interval traffic으로 fingerprint가 제거된 최신 duplicate도 멱등이다.
                        if (
                            kline.event_time <= previous_kline.event_time
                            or kline.open_time < previous_kline.open_time
                        ):
                            raise ValueError(
                                "Kline stream event moved backwards"
                            )

                    # 정상 stream에서는 fingerprint 수를 고정해 24시간 이상 연결의 메모리가 단조 증가하지 않게 한다.
                    self._kline_event_fingerprints[event_key] = kline
                    self._kline_event_fingerprints.move_to_end(event_key)
                    while (
                        len(self._kline_event_fingerprints)
                        > _MAXIMUM_KLINE_EVENT_FINGERPRINTS
                    ):
                        self._kline_event_fingerprints.popitem(last=False)
                    self._kline_cursor_by_interval[kline.interval] = kline
                    live_observer = self._kline_live_observer
                    if live_observer is None:
                        self._kline_buffer[kline.interval][
                            kline.open_time
                        ] = kline
                        return

                # Application callback은 Gateway state lock 밖에서 실행하되 delivery lock으로 순서를 유지한다.
                live_observer(kline)
            except Exception as error:
                with self._lock:
                    if (
                        generation == self._generation
                        and self._buffer_error is None
                    ):
                        self._buffer_error = error
                callback_error = error

        if callback_error is None:
            return

        # Payload·observer 오류는 transport close를 기다리지 않고 즉시 같은 full-resync 경로를 연다.
        disconnected = self._mark_disconnected(generation)
        if disconnected and reconciliation_required_callback is not None:
            try:
                reconciliation_required_callback(
                    "kline_stream_invalid"
                )
            except Exception as reconciliation_error:
                callback_error.add_note(
                    "Kline reconciliation callback failed: "
                    f"{type(reconciliation_error).__name__}"
                )
        raise callback_error

    def _mark_disconnected(self, generation: int) -> bool:
        """
        함수 이름: _mark_disconnected()
        기능: callback 세대가 현재 구독일 때만 buffer 연결을 끊긴 상태로 바꾼다.
        인자: generation -> 종료 callback이 속한 구독 세대
        반환값: 현재 연결 세대를 최초로 종료했으면 True
        작성 날짜: 2026/08/20
        """
        # 진행 중 observer가 끝난 뒤 세대를 닫아 callback 반환 후 snapshot mutation이 남지 않게 한다.
        with self._kline_delivery_lock:
            with self._lock:
                if generation != self._generation or not self._connected:
                    return False

                self._connected = False
                self._active_subscription = None
                self._kline_live_observer = None
                return True

    def _handle_account_info_message(
        self,
        payload: object,
        generation: int,
    ) -> None:
        """
        함수 이름: _handle_account_info_message()
        기능: 현재 세대 account·order event를 source 순서와 fill 멱등성에 따라 전달한다.
        인자: payload -> 공식 User Data Stream envelope
            generation -> callback이 속한 account 구독 세대
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        subscription_to_close: Subscription | None = None
        try:
            # Gateway lock은 세대 확인에만 사용하고 application callback 중에는 보유하지 않는다.
            with self._lock:
                if (
                    generation != self._account_generation
                    or not self._account_connected
                ):
                    return

            event_payload = _read_account_event(payload)
            event_type = _read_trimmed_text(
                event_payload.get("e"),
                "event.e",
            )
            if event_type in (
                "eventStreamTerminated",
                "serverShutdown",
            ):
                _read_non_negative_integer(
                    event_payload.get("E"),
                    "event.E",
                )
                raise AccountStreamStateError(
                    f"Binance user data stream ended: {event_type}"
                )

            # 잔고 patch와 주문 execution은 같은 인증 stream에서 callback owner별로 분기한다.
            if event_type == "outboundAccountPosition":
                self._handle_account_snapshot_payload(payload, generation)
                return
            if event_type == "executionReport":
                self._handle_execution_report_payload(payload, generation)
                return

            return  # balanceUpdate 등 현재 domain 계약 밖의 유효 event는 무시한다.
        except Exception as error:
            with self._lock:
                if (
                    generation == self._account_generation
                    and self._account_connected
                ):
                    self._account_error = error
                    self._account_connected = False
                    subscription_to_close = self._account_subscription
                    self._account_subscription = None

            # 불완전 stream을 먼저 닫은 뒤 REST full reconciliation owner를 정확히 한 번 깨운다.
            if subscription_to_close is not None:
                try:
                    subscription_to_close.close()
                except Exception as close_error:
                    error.add_note(
                        "account subscription cleanup failed: "
                        f"{type(close_error).__name__}"
                    )
            self._notify_reconciliation_required(
                generation,
                "account_stream_processing_failed",
                source_error=error,
            )
            raise

    def _handle_account_snapshot_payload(
        self,
        payload: object,
        generation: int,
    ) -> None:
        """
        함수 이름: _handle_account_snapshot_payload()
        기능: account balance patch를 source time과 fingerprint로 dedup해 callback에 전달한다.
        인자: payload -> 공식 outboundAccountPosition envelope
            generation -> callback이 속한 account 구독 세대
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        callback = self._account_snapshot_callback
        if callback is None:
            return
        parsed_snapshot = _parse_account_info_snapshot(payload)
        if parsed_snapshot is None:
            raise ValueError("account payload is not outboundAccountPosition")

        snapshot, update_time_milliseconds, fingerprint = parsed_snapshot
        with self._lock:
            # Parse 중 재구독된 이전 세대는 dedup state와 application에 모두 전달하지 않는다.
            if (
                generation != self._account_generation
                or not self._account_connected
            ):
                return
            current_update_time = self._account_update_time_milliseconds
            if (
                current_update_time is not None
                and update_time_milliseconds < current_update_time
            ):
                return
            if (
                update_time_milliseconds == current_update_time
                and fingerprint in self._account_fingerprints
            ):
                return

            # 더 최신 source time은 같은 timestamp 안의 fingerprint 집합을 새로 시작한다.
            if (
                current_update_time is None
                or update_time_milliseconds > current_update_time
            ):
                self._account_update_time_milliseconds = (
                    update_time_milliseconds
                )
                self._account_fingerprints = set()

            self._account_fingerprints.add(fingerprint)

        callback(snapshot)  # Application lock을 얻기 전에 Gateway lock을 항상 해제한다.

    def _handle_execution_report_payload(
        self,
        payload: object,
        generation: int,
    ) -> None:
        """
        함수 이름: _handle_execution_report_payload()
        기능: executionReport를 누적 Fill OrderResult로 만들고 새 사실만 callback에 전달한다.
        인자: payload -> 공식 executionReport envelope
            generation -> callback이 속한 account 구독 세대
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        callback = self._order_result_callback
        if callback is None:
            return

        observation = _parse_execution_report(payload)
        with self._lock:
            # Parse 중 종료·교체된 세대는 누적 fill map을 변경하지 않는다.
            if (
                generation != self._account_generation
                or not self._account_connected
            ):
                return
            result = self._merge_execution_observation(observation)

        if result is not None:
            callback(result)  # Controller에는 dedup된 누적 fill과 단조 상태만 전달한다.

    def _merge_execution_observation(
        self,
        observation: _ExecutionReportObservation,
    ) -> OrderResult | None:
        """
        함수 이름: _merge_execution_observation()
        기능: 같은 exchange order의 source cursor와 Fill을 누적해 안전한 OrderResult를 만든다.
        인자: observation -> 검증을 마친 단일 executionReport 관찰 값
        반환값: 새 domain 사실이 있으면 OrderResult, 완전 중복이면 None
        작성 날짜: 2026/08/22
        """
        exchange_order_id = observation.exchange_order_id
        known_client_order_id = self._order_client_ids_by_exchange_id.get(
            exchange_order_id
        )
        if (
            known_client_order_id is not None
            and known_client_order_id != observation.client_order_id
        ):
            raise AccountStreamStateError(
                "executionReport client order ID changed for one exchange order"
            )

        latest_cursor = self._order_source_cursors.get(exchange_order_id)
        latest_observation = self._order_latest_observations.get(
            exchange_order_id
        )
        fills_by_key = self._order_fills_by_exchange_id.setdefault(
            exchange_order_id,
            {},
        )
        new_fill = observation.fill
        if new_fill is not None:
            existing_fill = fills_by_key.get(new_fill.key)
            if existing_fill is not None and existing_fill != new_fill:
                raise AccountStreamStateError(
                    "duplicate executionReport fill key has conflicting content"
                )
        else:
            existing_fill = None

        # 같은 source cursor의 동일 event는 버리고 내용이 달라졌으면 임의 우선순위를 두지 않는다.
        if latest_cursor == observation.source_cursor:
            if latest_observation == observation:
                return None
            raise AccountStreamStateError(
                "executionReport source cursor has conflicting content"
            )

        # 더 오래된 event의 미관찰 fill은 gap을 뜻하므로 보존하되 REST 재조정을 요구한다.
        if (
            latest_cursor is not None
            and observation.source_cursor < latest_cursor
        ):
            if new_fill is None or existing_fill is not None:
                return None
            fills_by_key[new_fill.key] = new_fill
            raise AccountStreamStateError(
                "out-of-order executionReport introduced an unseen fill"
            )

        if new_fill is not None and existing_fill is None:
            fills_by_key[new_fill.key] = new_fill
        ordered_fills = tuple(
            sorted(
                fills_by_key.values(),
                key=lambda fill_value: (
                    fill_value.executed_at,
                    int(fill_value.trade_id),
                ),
            )
        )
        observed_quantity = sum(
            (fill_value.quantity for fill_value in ordered_fills),
            start=Decimal("0"),
        )
        if observed_quantity != observation.cumulative_quantity:
            raise AccountStreamStateError(
                "executionReport cumulative quantity requires REST reconciliation"
            )

        previous_status = self._order_latest_statuses.get(exchange_order_id)
        effective_status = self._resolve_stream_order_status(
            previous_status,
            observation.status,
        )
        previous_failure_reason = self._order_failure_reasons.get(
            exchange_order_id
        )
        has_new_fill = new_fill is not None and existing_fill is None
        has_new_fact = (
            previous_status != effective_status
            or previous_failure_reason != observation.failure_reason
            or has_new_fill
        )

        # 검증이 모두 끝난 뒤 source cursor와 누적 주문 사실을 한 번에 commit한다.
        self._order_client_ids_by_exchange_id[exchange_order_id] = (
            observation.client_order_id
        )
        self._order_source_cursors[exchange_order_id] = (
            observation.source_cursor
        )
        self._order_latest_observations[exchange_order_id] = observation
        self._order_latest_statuses[exchange_order_id] = effective_status
        self._order_failure_reasons[exchange_order_id] = (
            observation.failure_reason
        )
        if not has_new_fact:
            return None

        return OrderResult(
            symbol=observation.symbol,
            client_order_id=observation.client_order_id,
            status=effective_status,
            processed_at=observation.processed_at,
            exchange_order_id=exchange_order_id,
            fills=ordered_fills,
            failure_reason=observation.failure_reason,
        )

    @staticmethod
    def _resolve_stream_order_status(
        current_status: OrderStatus | None,
        observed_status: OrderStatus,
    ) -> OrderStatus:
        """
        함수 이름: _resolve_stream_order_status()
        기능: 늦은 active 상태가 이미 관찰한 terminal 또는 더 진행된 상태를 되돌리지 않게 한다.
        인자: current_status -> Gateway가 보존한 최신 상태 또는 최초이면 None
            observed_status -> 이번 executionReport의 공식 주문 상태
        반환값: 단조 진행을 보존한 상태
        작성 날짜: 2026/08/22
        """
        if current_status is None:
            return observed_status
        if _ORDER_STATUS_PROGRESS[observed_status] < _ORDER_STATUS_PROGRESS[
            current_status
        ]:
            return current_status
        if (
            _ORDER_STATUS_PROGRESS[current_status] == 5
            and _ORDER_STATUS_PROGRESS[observed_status] == 5
            and current_status is not observed_status
        ):
            raise AccountStreamStateError(
                "terminal executionReport status changed"
            )

        return observed_status

    def _notify_reconciliation_required(
        self,
        generation: int,
        reason: str,
        *,
        source_error: Exception | None = None,
    ) -> None:
        """
        함수 이름: _notify_reconciliation_required()
        기능: 현재 실패 세대의 full reconciliation callback을 최대 한 번 호출한다.
        인자: generation -> 장애가 발생한 account stream 세대
            reason -> credential을 포함하지 않는 정규화 장애 사유
            source_error -> callback 실패 note를 붙일 원래 예외 또는 None
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        callback: Callable[[str], object] | None = None
        with self._lock:
            if generation != self._account_generation:
                return
            if self._reconciliation_notified_generation == generation:
                return
            self._reconciliation_notified_generation = generation
            callback = self._reconciliation_required_callback

        if callback is None:
            return
        try:
            callback(reason)
        except Exception as callback_error:
            if source_error is not None:
                source_error.add_note(
                    "reconciliation callback failed: "
                    f"{type(callback_error).__name__}"
                )
                return
            with self._lock:
                self._account_error = callback_error

    def _mark_account_disconnected(
        self,
        generation: int,
        *,
        require_reconciliation: bool = False,
        reason: str = "account_stream_disconnected",
    ) -> None:
        """
        함수 이름: _mark_account_disconnected()
        기능: 현재 account 세대만 fail-closed하고 비정상 종료이면 재조정을 알린다.
        인자: generation -> 종료 callback이 속한 account 구독 세대
            require_reconciliation -> REST full reconciliation 필요 여부
            reason -> credential을 포함하지 않는 정규화 장애 사유
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        should_notify = False
        with self._lock:
            if generation == self._account_generation:
                self._account_connected = False
                self._account_subscription = None
                should_notify = require_reconciliation

        if should_notify:
            self._notify_reconciliation_required(generation, reason)
