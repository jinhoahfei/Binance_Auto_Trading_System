"""공식 Binance Spot WebSocket Kline stream을 buffer로 정규화한다."""

from collections.abc import Collection, Mapping
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


_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_INTERVAL_MILLISECONDS_BY_INTERVAL = {
    Interval.ONE_MINUTE: 60_000,
    Interval.THIRTY_MINUTES: 1_800_000,
    Interval.FOUR_HOURS: 14_400_000,
    Interval.ONE_DAY: 86_400_000,
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
    기능: 여러 공식 Binance Kline stream을 구독하기 위한 client 계약을 정의한다.
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


class KlineBufferStateError(RuntimeError):
    """
    클래스 이름: KlineBufferStateError
    기능: stale 또는 끊긴 Kline buffer를 배출하려는 상태 오류를 나타낸다.
    작성 날짜: 2026/08/20
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
        "_on_closed",
        "_transport_subscription",
    )

    def __init__(
        self,
        transport_subscription: Subscription,
        on_closed: Callable[[], None],
    ) -> None:
        """
        함수 이름: __init__()
        기능: 주입 client handle과 Gateway 종료 callback을 보존한다.
        인자: transport_subscription -> 주입 client가 반환한 구독 handle
            on_closed -> Gateway 수신 상태를 종료할 callback
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self._transport_subscription = transport_subscription
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

        try:
            self._transport_subscription.close()
        finally:
            self._on_closed()


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
    )


class WebSocketGateway:
    """
    클래스 이름: WebSocketGateway
    기능: 초기 REST 조회 동안 수신한 Kline을 세대별 thread-safe buffer로 보존한다.
    작성 날짜: 2026/08/20
    """

    def __init__(self, web_socket_client: BinanceWebSocketClient) -> None:
        """
        함수 이름: __init__()
        기능: 실제 구독을 만드는 WebSocket client와 빈 buffer 상태를 초기화한다.
        인자: web_socket_client -> Kline stream을 구독할 client
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        if web_socket_client is None or not callable(
            getattr(
                web_socket_client,
                "subscribe_all_kline_streams",
                None,
            )
        ):
            raise TypeError(
                "web_socket_client must provide subscribe_all_kline_streams"
            )

        self._web_socket_client = web_socket_client
        self._lock = RLock()
        self._generation = 0
        self._active_subscription: Subscription | None = None
        self._active_symbol: str | None = None
        self._active_intervals: tuple[Interval, ...] = ()
        self._connected = False
        self._buffer_error: Exception | None = None
        self._kline_buffer: dict[
            Interval,
            dict[datetime, Kline],
        ] = {}

    def start_all_kline_buffering(
        self,
        symbol: str,
        intervals: Collection[Interval],
    ) -> Subscription:
        """
        함수 이름: start_all_kline_buffering()
        기능: 새 구독 세대를 활성화하고 수신 Kline을 빈 초기화 buffer에 쌓는다.
        인자: symbol -> 구독할 Binance Spot symbol
            intervals -> 구독할 canonical interval set 또는 내부 tuple
        반환값: 새 WebSocket 구독 handle
        작성 날짜: 2026/08/20
        """
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

        with self._lock:
            self._generation += 1
            generation = self._generation
            previous_subscription = self._active_subscription
            self._active_subscription = None
            self._active_symbol = normalized_symbol
            self._active_intervals = normalized_intervals
            self._connected = True
            self._buffer_error = None
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
            self._buffer_kline_message(payload, generation)

        def on_disconnect() -> None:
            """
            함수 이름: on_disconnect()
            기능: 현재 구독 세대가 끊겼음을 기록해 불완전 buffer commit을 차단한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/08/20
            """
            self._mark_disconnected(generation)

        try:
            transport_subscription = (
                self._web_socket_client.subscribe_all_kline_streams(
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
            on_disconnect,
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
            self._kline_buffer = {
                interval: {}
                for interval in self._active_intervals
            }

        return drained_buffer

    def _buffer_kline_message(
        self,
        payload: object,
        generation: int,
    ) -> None:
        """
        함수 이름: _buffer_kline_message()
        기능: 현재 연결 세대의 정규화 Kline만 dedup buffer에 마지막 값으로 저장한다.
        인자: payload -> WebSocket에서 받은 raw 또는 combined payload
            generation -> callback이 속한 구독 세대
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        with self._lock:
            try:
                if generation != self._generation or not self._connected:
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

                self._kline_buffer[kline.interval][kline.open_time] = kline
            except Exception as error:
                if (
                    generation == self._generation
                    and self._connected
                    and self._buffer_error is None
                ):
                    self._buffer_error = error
                raise

    def _mark_disconnected(self, generation: int) -> None:
        """
        함수 이름: _mark_disconnected()
        기능: callback 세대가 현재 구독일 때만 buffer 연결을 끊긴 상태로 바꾼다.
        인자: generation -> 종료 callback이 속한 구독 세대
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        with self._lock:
            if generation == self._generation:
                self._connected = False
                self._active_subscription = None
