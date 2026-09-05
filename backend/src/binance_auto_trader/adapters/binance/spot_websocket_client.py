"""Binance Spot Testnet의 공개 Kline과 서명 User Data Stream transport를 제공한다."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import hashlib
import hmac
import json
from queue import Full, Queue
from threading import Event, RLock, Thread, current_thread
from typing import Protocol
from uuid import uuid4


_TESTNET_COMBINED_STREAM_URL = (
    "wss://stream.testnet.binance.vision/stream?streams="
)
_PUBLIC_COMBINED_STREAM_URL = "wss://data-stream.binance.vision/stream?streams="
_TESTNET_WEBSOCKET_API_URL = (
    "wss://ws-api.testnet.binance.vision/ws-api/v3"
)
_SUPPORTED_INTERVALS = frozenset({"1m", "30m", "4h", "1d"})
_DEFAULT_RECV_WINDOW_MILLISECONDS = 5_000
_MAX_RECV_WINDOW_MILLISECONDS = 60_000
_DEFAULT_STARTUP_TIMEOUT_SECONDS = 10
_DEFAULT_ACCOUNT_EVENT_QUEUE_CAPACITY = 1_024
_DISPATCHER_STOP = object()


class WebSocketClientConfigurationError(ValueError):
    """
    클래스 이름: WebSocketClientConfigurationError
    기능: credential 또는 transport 설정이 안전한 testnet 계약을 만족하지 않음을 나타낸다.
    작성 날짜: 2026/08/22
    """


class WebSocketTransportUnavailableError(RuntimeError):
    """
    클래스 이름: WebSocketTransportUnavailableError
    기능: 실제 WebSocket transport dependency를 불러오거나 만들 수 없음을 나타낸다.
    작성 날짜: 2026/08/22
    """


class WebSocketSubscriptionError(RuntimeError):
    """
    클래스 이름: WebSocketSubscriptionError
    기능: WebSocket 연결·서명 구독·frame 계약이 성립하지 않았음을 나타낸다.
    작성 날짜: 2026/08/22
    """


class _WebSocketApplication(Protocol):
    """
    클래스 이름: _WebSocketApplication
    기능: websocket-client와 단위 테스트 fake가 공유하는 최소 socket 실행 계약을 정의한다.
    작성 날짜: 2026/08/22
    """

    def send(self, payload: str) -> object:
        """
        함수 이름: send()
        기능: 연결된 WebSocket에 text frame을 전송한다.
        인자: payload -> 전송할 JSON text
        반환값: transport별 전송 결과
        작성 날짜: 2026/08/22
        """
        ...

    def run_forever(self) -> object:
        """
        함수 이름: run_forever()
        기능: close될 때까지 WebSocket 수신 loop를 실행한다.
        인자: 없음
        반환값: transport별 종료 결과
        작성 날짜: 2026/08/22
        """
        ...

    def close(self) -> object:
        """
        함수 이름: close()
        기능: WebSocket 연결을 멱등 종료한다.
        인자: 없음
        반환값: transport별 종료 결과
        작성 날짜: 2026/08/22
        """
        ...


class _SocketFactory(Protocol):
    """
    클래스 이름: _SocketFactory
    기능: URL과 websocket-client 호환 callback으로 socket application을 생성한다.
    작성 날짜: 2026/08/22
    """

    def __call__(
        self,
        url: str,
        *,
        on_open: Callable[[object], None],
        on_message: Callable[[object, object], None],
        on_error: Callable[[object, object], None],
        on_close: Callable[[object, object, object], None],
    ) -> _WebSocketApplication:
        """
        함수 이름: __call__()
        기능: 실제 또는 fake WebSocket application을 조립한다.
        인자: url -> credential을 포함하지 않는 고정 testnet endpoint
            on_open -> transport 연결 callback
            on_message -> text frame 수신 callback
            on_error -> transport 오류 callback
            on_close -> transport 종료 callback
        반환값: 실행 가능한 WebSocket application
        작성 날짜: 2026/08/22
        """
        ...


def _default_socket_factory(
    url: str,
    *,
    on_open: Callable[[object], None],
    on_message: Callable[[object, object], None],
    on_error: Callable[[object, object], None],
    on_close: Callable[[object, object, object], None],
) -> _WebSocketApplication:
    """
    함수 이름: _default_socket_factory()
    기능: websocket-client를 실제 구독 시점에만 import해 WebSocketApp을 만든다.
    인자: url -> credential을 포함하지 않는 testnet endpoint
        on_open -> transport 연결 callback
        on_message -> text frame 수신 callback
        on_error -> transport 오류 callback
        on_close -> transport 종료 callback
    반환값: websocket-client WebSocketApp
    작성 날짜: 2026/08/22
    """
    # 기본 unit suite가 network dependency 없이 import되도록 실제 구독 시점까지 지연한다.
    try:
        import websocket
    except ModuleNotFoundError as error:
        raise WebSocketTransportUnavailableError(
            "websocket-client package is required for Binance WebSocket transport"
        ) from error

    return websocket.WebSocketApp(
        url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )


def _validate_secret_text(value: object, field_name: str) -> str:
    """
    함수 이름: _validate_secret_text()
    기능: credential을 오류 문자열에 노출하지 않고 비어 있지 않은 문자열 형식만 검증한다.
    인자: value -> 검증할 credential 값
        field_name -> credential 값이 아닌 안전한 설정 이름
    반환값: 검증된 원래 문자열
    작성 날짜: 2026/08/22
    """
    # trim이나 문자열 변환으로 다른 credential을 만들지 않고 원래 값을 그대로 요구한다.
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value or value != value.strip():
        raise WebSocketClientConfigurationError(
            f"{field_name} must be non-empty without outer whitespace"
        )

    return value


def _normalize_symbol(symbol: object) -> str:
    """
    함수 이름: _normalize_symbol()
    기능: public stream symbol을 공백 없는 ASCII 대문자 형식으로 검증한다.
    인자: symbol -> 검증할 symbol
    반환값: 정규화된 대문자 symbol
    작성 날짜: 2026/08/22
    """
    if not isinstance(symbol, str):
        raise TypeError("symbol must be a string")
    normalized_symbol = symbol.strip().upper()
    if (
        not normalized_symbol
        or not normalized_symbol.isascii()
        or not normalized_symbol.isalnum()
    ):
        raise ValueError("symbol must contain only ASCII letters and digits")

    return normalized_symbol


def _validate_intervals(intervals: object) -> tuple[str, ...]:
    """
    함수 이름: _validate_intervals()
    기능: project가 지원하는 중복 없는 Binance Kline interval tuple을 검증한다.
    인자: intervals -> 공식 interval 문자열 tuple
    반환값: 검증된 원래 interval tuple
    작성 날짜: 2026/08/22
    """
    if not isinstance(intervals, tuple):
        raise TypeError("intervals must be a tuple")
    if not intervals:
        raise ValueError("intervals must not be empty")
    if any(not isinstance(interval, str) for interval in intervals):
        raise TypeError("intervals must contain strings")
    if len(set(intervals)) != len(intervals):
        raise ValueError("intervals must not contain duplicates")
    if any(interval not in _SUPPORTED_INTERVALS for interval in intervals):
        raise ValueError("intervals contain an unsupported interval")

    return intervals


def _utc_timestamp_milliseconds() -> int:
    """
    함수 이름: _utc_timestamp_milliseconds()
    기능: 현재 aware UTC 시각을 WebSocket SIGNED 요청용 Unix millisecond로 반환한다.
    인자: 없음
    반환값: 현재 Unix millisecond 정수
    작성 날짜: 2026/08/22
    """
    return int(datetime.now(timezone.utc).timestamp() * 1_000)


def _create_request_id() -> str:
    """
    함수 이름: _create_request_id()
    기능: credential과 무관한 36자 UUID request ID를 만든다.
    인자: 없음
    반환값: canonical UUID 문자열
    작성 날짜: 2026/08/22
    """
    return str(uuid4())


def _create_hmac_signature(secret: str, params: Mapping[str, object]) -> str:
    """
    함수 이름: _create_hmac_signature()
    기능: parameter 이름 정렬 후 HMAC-SHA-256 hex WebSocket API signature를 만든다.
    인자: secret -> 메모리에만 보존한 HMAC secret
        params -> signature 필드를 제외한 SIGNED 요청 parameter
    반환값: 소문자 hex signature
    작성 날짜: 2026/08/22
    """
    # 공식 WebSocket API 규칙대로 이름을 정렬하고 UTF-8 payload를 한 번만 서명한다.
    signature_payload = "&".join(
        f"{name}={params[name]}"
        for name in sorted(params)
    )
    return hmac.new(
        secret.encode("utf-8"),
        signature_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


class _ConnectionLifecycle:
    """
    클래스 이름: _ConnectionLifecycle
    기능: 구독 startup ACK, 소유자 close와 비정상 disconnect를 thread-safe하게 구분한다.
    작성 날짜: 2026/08/22
    """

    def __init__(self, on_disconnect: Callable[[], None]) -> None:
        """
        함수 이름: __init__()
        기능: startup 대기 event와 disconnect 단일 알림 상태를 초기화한다.
        인자: on_disconnect -> 비정상 transport 종료 callback
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self._on_disconnect = on_disconnect
        self._lock = RLock()
        self._startup_event = Event()
        self._established = False
        self._owner_closed = False
        self._disconnect_notified = False
        self._startup_failure: Exception | None = None

    @property
    def established(self) -> bool:
        """
        함수 이름: established()
        기능: public open 또는 authenticated subscription ACK 완료 여부를 반환한다.
        인자: 없음
        반환값: startup 계약이 성립했으면 True
        작성 날짜: 2026/08/22
        """
        with self._lock:
            return self._established and not self._owner_closed

    def mark_established(self) -> None:
        """
        함수 이름: mark_established()
        기능: startup 성공을 한 번 기록하고 대기 중인 호출자를 깨운다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        with self._lock:
            if self._owner_closed or self._startup_failure is not None:
                return
            self._established = True
            self._startup_event.set()

    def record_transport_failure(self, error: Exception) -> None:
        """
        함수 이름: record_transport_failure()
        기능: credential 없는 transport 오류를 기록하고 disconnect callback을 최대 한 번 호출한다.
        인자: error -> 호출자에게 전달할 정규화된 transport 오류
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        should_notify = False
        with self._lock:
            if self._owner_closed:
                return
            if not self._established and self._startup_failure is None:
                self._startup_failure = error
            self._startup_event.set()
            if not self._disconnect_notified:
                self._disconnect_notified = True
                should_notify = True

        if should_notify:
            try:
                self._on_disconnect()
            except Exception:
                return  # transport thread에서는 상위 callback 오류를 credential 포함 log로 바꾸지 않는다.

    def mark_owner_closed(self) -> None:
        """
        함수 이름: mark_owner_closed()
        기능: 명시적 close를 표시해 뒤따르는 on_close를 장애로 알리지 않게 한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        with self._lock:
            self._owner_closed = True
            self._startup_event.set()

    def wait_until_established(self, timeout_seconds: int) -> None:
        """
        함수 이름: wait_until_established()
        기능: 제한 시간 안의 startup 성공을 확인하고 실패·timeout을 동기 전파한다.
        인자: timeout_seconds -> startup을 기다릴 최대 초
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        if not self._startup_event.wait(timeout_seconds):
            raise WebSocketSubscriptionError(
                "Binance WebSocket subscription startup timed out"
            )

        with self._lock:
            if self._startup_failure is not None:
                raise self._startup_failure
            if not self._established or self._owner_closed:
                raise WebSocketSubscriptionError(
                    "Binance WebSocket subscription closed during startup"
                )


class _SocketWorker:
    """
    클래스 이름: _SocketWorker
    기능: WebSocket run loop 예외와 예상 밖 정상 반환을 lifecycle 장애로 변환한다.
    작성 날짜: 2026/08/22
    """

    def __init__(
        self,
        socket_application: _WebSocketApplication,
        lifecycle: _ConnectionLifecycle,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 실행할 socket과 장애 상태 owner를 보존한다.
        인자: socket_application -> 실행할 WebSocket application
            lifecycle -> startup과 disconnect 상태 owner
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self._socket_application = socket_application
        self._lifecycle = lifecycle

    def run(self) -> None:
        """
        함수 이름: run()
        기능: socket loop를 실행하고 예상 밖 종료를 credential 없는 typed 오류로 닫는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        try:
            self._socket_application.run_forever()
        except Exception as error:
            self._lifecycle.record_transport_failure(
                WebSocketSubscriptionError(
                    "Binance WebSocket run loop failed: "
                    f"{type(error).__name__}"
                )
            )
            return

        self._lifecycle.record_transport_failure(
            WebSocketSubscriptionError(
                "Binance WebSocket run loop ended unexpectedly"
            )
        )


class _BoundedMessageDispatcher:
    """
    클래스 이름: _BoundedMessageDispatcher
    기능: WebSocket 수신 loop와 downstream callback을 bounded FIFO worker로 분리한다.
    작성 날짜: 2026/08/23
    """

    def __init__(
        self,
        *,
        consumer: Callable[[object], None],
        socket_application: _WebSocketApplication,
        lifecycle: _ConnectionLifecycle,
        capacity: int,
        worker_name: str,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 단일 consumer와 실패 시 닫을 transport를 가진 bounded FIFO를 준비한다.
        인자: consumer -> 검증된 event를 순서대로 처리할 downstream callback
            socket_application -> overflow 또는 callback 실패 시 닫을 socket
            lifecycle -> disconnect 단일 알림과 startup 상태 owner
            capacity -> 동시에 대기할 수 있는 최대 event 수
            worker_name -> credential을 포함하지 않는 dispatcher thread 이름
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        self._consumer = consumer
        self._socket_application = socket_application
        self._lifecycle = lifecycle
        self._queue: Queue[object] = Queue(maxsize=capacity)
        self._lock = RLock()
        self._closed = False
        self._pending_count = 0
        self._caught_up_event = Event()
        self._caught_up_event.set()  # worker 시작 전에는 처리할 account event가 없다.
        self._worker_thread = Thread(
            target=self.run,
            name=worker_name,
            daemon=True,
        )

    @property
    def caught_up(self) -> bool:
        """
        함수 이름: caught_up()
        기능: queued 또는 실행 중인 account event가 하나도 없는지 반환한다.
        인자: 없음
        반환값: dispatcher가 열려 있고 모든 event 처리를 마쳤으면 True
        작성 날짜: 2026/08/23
        """
        with self._lock:
            return not self._closed and self._pending_count == 0

    def wait_until_caught_up(self, timeout_seconds: int) -> bool:
        """
        함수 이름: wait_until_caught_up()
        기능: 제한 시간 안에 dispatcher backlog가 모두 처리되는지 기다린다.
        인자: timeout_seconds -> 기다릴 최대 초
        반환값: 제한 시간 안에 열린 dispatcher가 최신 상태가 되면 True
        작성 날짜: 2026/08/23
        """
        if isinstance(timeout_seconds, bool) or not isinstance(
            timeout_seconds,
            int,
        ):
            raise TypeError("timeout_seconds must be an integer")
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must not be negative")

        if not self._caught_up_event.wait(timeout_seconds):
            return False

        return self.caught_up  # close와 마지막 event 완료가 경합해도 현재 상태를 재확인한다.

    def start(self) -> None:
        """
        함수 이름: start()
        기능: socket 수신 loop보다 먼저 daemon FIFO worker를 시작한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        self._worker_thread.start()

    def enqueue(self, payload: object) -> None:
        """
        함수 이름: enqueue()
        기능: 수신 loop를 기다리게 하지 않고 event를 FIFO에 넣거나 overflow로 fail closed한다.
        인자: payload -> 검증이 끝난 account event envelope
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        failure: WebSocketSubscriptionError | None = None
        with self._lock:
            if self._closed:
                failure = WebSocketSubscriptionError(
                    "account event dispatcher is closed"
                )
            else:
                try:
                    # pending_count에는 queue 대기뿐 아니라 consumer가 처리 중인 event도 포함한다.
                    self._caught_up_event.clear()
                    self._queue.put_nowait(payload)
                    self._pending_count += 1
                except Full:
                    failure = WebSocketSubscriptionError(
                        "account event dispatcher capacity was exceeded"
                    )

        if failure is not None:
            self._fail(failure)  # backlog 손실을 허용하지 않고 stream 전체를 재조정한다.

    def run(self) -> None:
        """
        함수 이름: run()
        기능: event를 한 번에 하나씩 순서대로 처리하고 consumer 실패를 transport 장애로 바꾼다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        try:
            while True:
                payload = self._queue.get()
                if payload is _DISPATCHER_STOP:
                    self._queue.task_done()
                    return  # owner close가 넣은 sentinel 뒤의 event는 처리하지 않는다.
                with self._lock:
                    dispatcher_closed = self._closed
                if dispatcher_closed:
                    self._queue.task_done()
                    return  # dequeue와 owner close가 경합하면 새 application callback을 시작하지 않는다.

                try:
                    self._consumer(payload)
                except BaseException as error:
                    self._queue.task_done()
                    self._fail(
                        WebSocketSubscriptionError(
                            "account event consumer failed: "
                            f"{type(error).__name__}"
                        )
                    )
                    self._complete_one_event()  # closed 상태에서 count만 정리해 readiness 재개 창을 막는다.
                    return

                self._queue.task_done()
                if not self._complete_one_event():
                    return  # owner close 뒤에는 이미 queued된 account event도 폐기한다.
        except BaseException as error:
            self._fail(
                WebSocketSubscriptionError(
                    "account event dispatcher failed: "
                    f"{type(error).__name__}"
                )
            )

    def close(self) -> None:
        """
        함수 이름: close()
        기능: 새 event를 거부하고 idle worker를 깨운 뒤 bounded join으로 정리한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._caught_up_event.clear()
            try:
                self._queue.put_nowait(_DISPATCHER_STOP)
            except Full:
                pass  # active consumer가 끝나면 closed 상태를 보고 남은 queue를 처리하지 않는다.

        if current_thread() is not self._worker_thread:
            self._worker_thread.join(timeout=1)  # 막힌 application callback을 무기한 기다리지 않는다.

    def _complete_one_event(self) -> bool:
        """
        함수 이름: _complete_one_event()
        기능: consumer가 끝낸 event를 pending 수에서 제거하고 다음 처리 허용 여부를 반환한다.
        인자: 없음
        반환값: dispatcher가 계속 열려 있으면 True
        작성 날짜: 2026/08/23
        """
        with self._lock:
            if self._pending_count <= 0:
                raise WebSocketSubscriptionError(
                    "account event dispatcher pending count became invalid"
                )
            self._pending_count -= 1
            if self._pending_count == 0 and not self._closed:
                self._caught_up_event.set()

            return not self._closed

    def _fail(self, error: WebSocketSubscriptionError) -> None:
        """
        함수 이름: _fail()
        기능: dispatcher를 한 번 닫고 account disconnect 및 REST 재조정 경로를 깨운다.
        인자: error -> credential을 포함하지 않는 dispatcher 실패
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        should_close_socket = False
        with self._lock:
            if not self._closed:
                self._closed = True
                self._caught_up_event.clear()
                should_close_socket = True

        if not should_close_socket:
            return

        # 연결 상태를 먼저 fail closed한 뒤 socket close로 수신 loop까지 중단한다.
        self._lifecycle.record_transport_failure(error)
        try:
            self._socket_application.close()
        except Exception:
            return  # close 오류가 원래 dispatcher 장애나 credential 정보를 덮지 않게 한다.


class _SocketSubscription:
    """
    클래스 이름: _SocketSubscription
    기능: socket application과 daemon worker를 소유하고 close를 멱등 처리한다.
    작성 날짜: 2026/08/22
    """

    def __init__(
        self,
        socket_application: _WebSocketApplication,
        lifecycle: _ConnectionLifecycle,
        worker_thread: Thread,
        message_dispatcher: _BoundedMessageDispatcher | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: transport 자원과 명시적 종료 상태를 보존한다.
        인자: socket_application -> 종료할 WebSocket application
            lifecycle -> owner close를 표시할 lifecycle
            worker_thread -> 수신 loop daemon thread
            message_dispatcher -> account callback FIFO 또는 public stream이면 None
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self._socket_application = socket_application
        self._lifecycle = lifecycle
        self._worker_thread = worker_thread
        self._message_dispatcher = message_dispatcher
        self._lock = RLock()
        self._closed = False

    @property
    def caught_up(self) -> bool:
        """
        함수 이름: caught_up()
        기능: account callback FIFO가 최신 event까지 처리됐는지 반환한다.
        인자: 없음
        반환값: pending account event가 없거나 dispatcher를 쓰지 않으면 True
        작성 날짜: 2026/08/23
        """
        with self._lock:
            if self._closed:
                return False
            message_dispatcher = self._message_dispatcher

        if message_dispatcher is None:
            return True  # public stream은 account 주문 readiness에 참여하지 않는다.

        return message_dispatcher.caught_up

    def wait_until_caught_up(self, timeout_seconds: int) -> bool:
        """
        함수 이름: wait_until_caught_up()
        기능: account callback FIFO가 제한 시간 안에 최신 상태가 되는지 기다린다.
        인자: timeout_seconds -> 기다릴 최대 초
        반환값: 열린 subscription의 pending account event가 모두 처리되면 True
        작성 날짜: 2026/08/23
        """
        with self._lock:
            if self._closed:
                return False
            message_dispatcher = self._message_dispatcher

        if message_dispatcher is None:
            return True  # public stream에는 account callback backlog가 없다.

        return message_dispatcher.wait_until_caught_up(timeout_seconds)

    def close(self) -> None:
        """
        함수 이름: close()
        기능: 명시적 종료를 표시하고 socket과 worker를 한 번만 정리한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._lifecycle.mark_owner_closed()

        try:
            if self._message_dispatcher is not None:
                self._message_dispatcher.close()
            self._socket_application.close()
        finally:
            if current_thread() is not self._worker_thread:
                self._worker_thread.join(timeout=1)  # close를 무기한 기다리지 않는다.


class BinanceSpotWebSocketClient:
    """
    클래스 이름: BinanceSpotWebSocketClient
    기능: 고정 Binance Spot Testnet endpoint에 public Kline과 HMAC User Data 구독을 연다.
    작성 날짜: 2026/08/22
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        socket_factory: _SocketFactory | None = None,
        timestamp_provider: Callable[[], int] | None = None,
        request_id_factory: Callable[[], str] | None = None,
        recv_window_milliseconds: int = _DEFAULT_RECV_WINDOW_MILLISECONDS,
        startup_timeout_seconds: int = _DEFAULT_STARTUP_TIMEOUT_SECONDS,
        account_event_queue_capacity: int = (
            _DEFAULT_ACCOUNT_EVENT_QUEUE_CAPACITY
        ),
        use_mainnet_market_data: bool = False,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 메모리 credential과 주입 가능한 clock·socket seam을 검증해 보존한다.
        인자: api_key -> testnet USER_STREAM 권한 API key
            api_secret -> testnet HMAC secret
            socket_factory -> websocket-client 호환 factory 또는 None
            timestamp_provider -> server time 보정이 반영된 millisecond provider 또는 None
            request_id_factory -> 결정적 request ID provider 또는 None
            recv_window_milliseconds -> SIGNED 요청 허용 시간 창
            startup_timeout_seconds -> open 또는 subscription ACK 최대 대기 시간
            account_event_queue_capacity -> downstream 처리 전 대기할 최대 account event 수
            use_mainnet_market_data -> 공개 Kline을 실제 시장 stream에서 구독할지 여부
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self._api_key = _validate_secret_text(api_key, "api_key")
        self._api_secret = _validate_secret_text(api_secret, "api_secret")
        if socket_factory is not None and not callable(socket_factory):
            raise TypeError("socket_factory must be callable")
        if timestamp_provider is not None and not callable(timestamp_provider):
            raise TypeError("timestamp_provider must be callable")
        if request_id_factory is not None and not callable(request_id_factory):
            raise TypeError("request_id_factory must be callable")
        if (
            isinstance(recv_window_milliseconds, bool)
            or not isinstance(recv_window_milliseconds, int)
        ):
            raise TypeError("recv_window_milliseconds must be an integer")
        if not 0 < recv_window_milliseconds <= _MAX_RECV_WINDOW_MILLISECONDS:
            raise ValueError(
                "recv_window_milliseconds must be between 1 and 60000"
            )
        if (
            isinstance(startup_timeout_seconds, bool)
            or not isinstance(startup_timeout_seconds, int)
        ):
            raise TypeError("startup_timeout_seconds must be an integer")
        if startup_timeout_seconds <= 0:
            raise ValueError("startup_timeout_seconds must be positive")
        if (
            isinstance(account_event_queue_capacity, bool)
            or not isinstance(account_event_queue_capacity, int)
        ):
            raise TypeError("account_event_queue_capacity must be an integer")
        if account_event_queue_capacity <= 0:
            raise ValueError("account_event_queue_capacity must be positive")

        # 공개 시세만 두 공식 host 중 선택하며 서명 계좌 구독은 고정 Testnet URL을 사용한다.
        if type(use_mainnet_market_data) is not bool:
            raise TypeError("use_mainnet_market_data must be a bool")
        self._market_stream_url = (
            _PUBLIC_COMBINED_STREAM_URL
            if use_mainnet_market_data
            else _TESTNET_COMBINED_STREAM_URL
        )
        self._socket_factory = socket_factory or _default_socket_factory
        self._timestamp_provider = (
            timestamp_provider or _utc_timestamp_milliseconds
        )
        self._request_id_factory = request_id_factory or _create_request_id
        self._recv_window_milliseconds = recv_window_milliseconds
        self._startup_timeout_seconds = startup_timeout_seconds
        self._account_event_queue_capacity = account_event_queue_capacity

    def subscribe_all_kline_streams(
        self,
        *,
        symbol: str,
        intervals: tuple[str, ...],
        on_message: Callable[[object], None],
        on_disconnect: Callable[[], None],
    ) -> _SocketSubscription:
        """
        함수 이름: subscribe_all_kline_streams()
        기능: 선택한 공식 시장의 combined URL에서 한 symbol의 모든 Kline stream을 구독한다.
        인자: symbol -> 구독할 정규화 Spot symbol
            intervals -> 구독할 공식 interval 문자열 tuple
            on_message -> combined Kline payload callback
            on_disconnect -> 비정상 연결 종료 callback
        반환값: 활성 public stream 구독 handle
        작성 날짜: 2026/08/22
        """
        normalized_symbol = _normalize_symbol(symbol)
        normalized_intervals = _validate_intervals(intervals)
        self._validate_callbacks(on_message, on_disconnect)
        stream_names = "/".join(
            f"{normalized_symbol.lower()}@kline_{interval}"
            for interval in normalized_intervals
        )
        url = f"{self._market_stream_url}{stream_names}"  # 재연결도 최초와 같은 시세 환경을 사용한다.
        lifecycle = _ConnectionLifecycle(on_disconnect)

        def handle_open(_socket: object) -> None:
            """
            함수 이름: handle_open()
            기능: public combined transport open을 startup 성공으로 표시한다.
            인자: _socket -> websocket-client가 전달한 socket application
            반환값: 없음
            작성 날짜: 2026/08/22
            """
            lifecycle.mark_established()

        def handle_message(socket: object, payload: object) -> None:
            """
            함수 이름: handle_message()
            기능: public payload를 Gateway callback에 전달하고 거부되면 연결을 닫는다.
            인자: socket -> 현재 socket application
                payload -> 수신한 combined text frame
            반환값: 없음
            작성 날짜: 2026/08/22
            """
            try:
                on_message(payload)
            except Exception as error:
                lifecycle.record_transport_failure(
                    WebSocketSubscriptionError(
                        "public Kline consumer rejected payload: "
                        f"{type(error).__name__}"
                    )
                )
                socket.close()

        def handle_error(_socket: object, error: object) -> None:
            """
            함수 이름: handle_error()
            기능: public transport 오류를 credential 없는 typed 실패로 변환한다.
            인자: _socket -> websocket-client가 전달한 socket application
                error -> transport 원래 오류 값
            반환값: 없음
            작성 날짜: 2026/08/22
            """
            lifecycle.record_transport_failure(
                WebSocketSubscriptionError(
                    "public Kline transport failed: "
                    f"{type(error).__name__}"
                )
            )

        def handle_close(
            _socket: object,
            _status_code: object,
            _message: object,
        ) -> None:
            """
            함수 이름: handle_close()
            기능: 예상 밖 public close를 disconnect 실패로 표시한다.
            인자: _socket -> websocket-client가 전달한 socket application
                _status_code -> credential 없는 WebSocket close code
                _message -> 사용하지 않는 transport close message
            반환값: 없음
            작성 날짜: 2026/08/22
            """
            lifecycle.record_transport_failure(
                WebSocketSubscriptionError(
                    "public Kline transport disconnected"
                )
            )

        # URL에 credential을 넣지 않고 주입 factory가 같은 callback 계약을 받게 한다.
        socket_application = self._socket_factory(
            url,
            on_open=handle_open,
            on_message=handle_message,
            on_error=handle_error,
            on_close=handle_close,
        )
        return self._start_subscription(
            socket_application,
            lifecycle,
            worker_name="binance-public-kline",
        )

    def subscribe_account_info(
        self,
        *,
        on_message: Callable[[object], None],
        on_disconnect: Callable[[], None],
    ) -> _SocketSubscription:
        """
        함수 이름: subscribe_account_info()
        기능: 현행 userDataStream.subscribe.signature HMAC 방식으로 testnet account stream을 연다.
        인자: on_message -> 검증된 User Data Stream envelope callback
            on_disconnect -> 비정상 종료 callback
        반환값: subscription ACK가 완료된 account stream handle
        작성 날짜: 2026/08/22
        """
        self._validate_callbacks(on_message, on_disconnect)
        request_id = self._read_request_id()
        lifecycle = _ConnectionLifecycle(on_disconnect)
        subscription_id: list[int | None] = [None]
        subscription_lock = RLock()
        message_dispatcher_holder: list[
            _BoundedMessageDispatcher | None
        ] = [None]

        def handle_open(socket: object) -> None:
            """
            함수 이름: handle_open()
            기능: transport open 직후 HMAC signature subscription 요청을 한 번 전송한다.
            인자: socket -> 연결된 WebSocket application
            반환값: 없음
            작성 날짜: 2026/08/22
            """
            try:
                request_payload = self._create_account_subscription_request(
                    request_id
                )
                socket.send(
                    json.dumps(
                        request_payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
            except Exception as error:
                lifecycle.record_transport_failure(
                    WebSocketSubscriptionError(
                        "account subscription request failed: "
                        f"{type(error).__name__}"
                    )
                )
                socket.close()

        def handle_message(socket: object, payload: object) -> None:
            """
            함수 이름: handle_message()
            기능: subscription ACK와 이후 account event를 상관·검증해 분기한다.
            인자: socket -> 현재 WebSocket application
                payload -> 수신한 WebSocket API text frame
            반환값: 없음
            작성 날짜: 2026/08/22
            """
            try:
                message_dispatcher = message_dispatcher_holder[0]
                if message_dispatcher is None:
                    raise WebSocketSubscriptionError(
                        "account event dispatcher is not initialized"
                    )
                self._handle_account_message(
                    payload,
                    request_id=request_id,
                    subscription_id=subscription_id,
                    subscription_lock=subscription_lock,
                    lifecycle=lifecycle,
                    on_message=message_dispatcher.enqueue,
                )
            except Exception as error:
                lifecycle.record_transport_failure(
                    WebSocketSubscriptionError(
                        "account stream frame validation failed: "
                        f"{type(error).__name__}"
                    )
                )
                socket.close()

        def handle_error(_socket: object, error: object) -> None:
            """
            함수 이름: handle_error()
            기능: authenticated transport 오류를 credential 없는 typed 실패로 변환한다.
            인자: _socket -> websocket-client가 전달한 socket application
                error -> transport 원래 오류 값
            반환값: 없음
            작성 날짜: 2026/08/22
            """
            lifecycle.record_transport_failure(
                WebSocketSubscriptionError(
                    "account stream transport failed: "
                    f"{type(error).__name__}"
                )
            )

        def handle_close(
            _socket: object,
            _status_code: object,
            _message: object,
        ) -> None:
            """
            함수 이름: handle_close()
            기능: 예상 밖 authenticated close를 disconnect 실패로 표시한다.
            인자: _socket -> websocket-client가 전달한 socket application
                _status_code -> credential 없는 WebSocket close code
                _message -> 사용하지 않는 transport close message
            반환값: 없음
            작성 날짜: 2026/08/22
            """
            lifecycle.record_transport_failure(
                WebSocketSubscriptionError(
                    "account stream transport disconnected"
                )
            )

        # API key는 공식 JSON params에만 두고 secret과 두 credential 모두 URL에 넣지 않는다.
        socket_application = self._socket_factory(
            _TESTNET_WEBSOCKET_API_URL,
            on_open=handle_open,
            on_message=handle_message,
            on_error=handle_error,
            on_close=handle_close,
        )
        # 단일 FIFO worker가 downstream callback을 맡아 socket ping·close 수신을 막지 않는다.
        message_dispatcher = _BoundedMessageDispatcher(
            consumer=on_message,
            socket_application=socket_application,
            lifecycle=lifecycle,
            capacity=self._account_event_queue_capacity,
            worker_name="binance-account-events",
        )
        message_dispatcher_holder[0] = message_dispatcher
        return self._start_subscription(
            socket_application,
            lifecycle,
            worker_name="binance-account-stream",
            message_dispatcher=message_dispatcher,
        )

    @staticmethod
    def _validate_callbacks(
        on_message: object,
        on_disconnect: object,
    ) -> None:
        """
        함수 이름: _validate_callbacks()
        기능: transport를 만들기 전에 message와 disconnect callback 형식을 검증한다.
        인자: on_message -> 수신 payload callback 후보
            on_disconnect -> 비정상 종료 callback 후보
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        if not callable(on_message):
            raise TypeError("on_message must be callable")
        if not callable(on_disconnect):
            raise TypeError("on_disconnect must be callable")

    def _read_request_id(self) -> str:
        """
        함수 이름: _read_request_id()
        기능: 주입 factory가 만든 request ID를 공백 없는 최대 36자 ASCII 문자열로 검증한다.
        인자: 없음
        반환값: 검증된 request ID
        작성 날짜: 2026/08/22
        """
        request_id = self._request_id_factory()
        if not isinstance(request_id, str):
            raise TypeError("request ID must be a string")
        if (
            not request_id
            or request_id != request_id.strip()
            or not request_id.isascii()
            or len(request_id) > 36
        ):
            raise ValueError(
                "request ID must be a non-empty ASCII string up to 36 characters"
            )

        return request_id

    def _read_timestamp_milliseconds(self) -> int:
        """
        함수 이름: _read_timestamp_milliseconds()
        기능: server time 보정 provider 결과가 0 이상 정수 millisecond인지 검증한다.
        인자: 없음
        반환값: SIGNED 요청 timestamp
        작성 날짜: 2026/08/22
        """
        timestamp = self._timestamp_provider()
        if isinstance(timestamp, bool) or not isinstance(timestamp, int):
            raise TypeError("timestamp provider must return an integer")
        if timestamp < 0:
            raise ValueError("timestamp provider must not return a negative value")

        return timestamp

    def _create_account_subscription_request(
        self,
        request_id: str,
    ) -> dict[str, object]:
        """
        함수 이름: _create_account_subscription_request()
        기능: legacy listenKey 없이 현행 signature subscription JSON 요청을 만든다.
        인자: request_id -> ACK를 상관할 credential 없는 request ID
        반환값: HMAC signature를 포함한 WebSocket API 요청 mapping
        작성 날짜: 2026/08/22
        """
        params: dict[str, object] = {
            "apiKey": self._api_key,
            "recvWindow": self._recv_window_milliseconds,
            "timestamp": self._read_timestamp_milliseconds(),
        }
        params["signature"] = _create_hmac_signature(
            self._api_secret,
            params,
        )

        return {
            "id": request_id,
            "method": "userDataStream.subscribe.signature",
            "params": params,
        }

    @staticmethod
    def _parse_json_object(payload: object) -> Mapping[str, object]:
        """
        함수 이름: _parse_json_object()
        기능: WebSocket text frame을 JSON object로 해석하고 다른 top-level 형식을 거부한다.
        인자: payload -> 수신한 text 또는 이미 해석된 mapping
        반환값: 검증된 JSON object mapping
        작성 날짜: 2026/08/22
        """
        parsed_payload = payload
        if isinstance(payload, str):
            try:
                parsed_payload = json.loads(payload)
            except json.JSONDecodeError as error:
                raise ValueError("WebSocket frame must contain valid JSON") from error
        if not isinstance(parsed_payload, Mapping):
            raise TypeError("WebSocket frame must contain a JSON object")

        return parsed_payload

    def _handle_account_message(
        self,
        payload: object,
        *,
        request_id: str,
        subscription_id: list[int | None],
        subscription_lock: RLock,
        lifecycle: _ConnectionLifecycle,
        on_message: Callable[[object], None],
    ) -> None:
        """
        함수 이름: _handle_account_message()
        기능: signature subscription ACK 또는 해당 subscription의 event만 callback에 전달한다.
        인자: payload -> 수신 WebSocket API frame
            request_id -> 현재 signature subscription 요청 ID
            subscription_id -> ACK가 확정한 단일 subscription ID holder
            subscription_lock -> subscription ID 읽기·쓰기 lock
            lifecycle -> ACK startup과 disconnect owner
            on_message -> 검증된 User Data Stream을 bounded FIFO에 넣을 callback
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        parsed_payload = self._parse_json_object(payload)
        if parsed_payload.get("id") == request_id:
            self._accept_account_subscription_response(
                parsed_payload,
                subscription_id=subscription_id,
                subscription_lock=subscription_lock,
                lifecycle=lifecycle,
            )
            return
        if not lifecycle.established:
            raise WebSocketSubscriptionError(
                "account event arrived before subscription ACK"
            )

        event_payload = parsed_payload.get("event")
        if not isinstance(event_payload, Mapping):
            raise TypeError("account stream frame must contain object event")
        event_type = event_payload.get("e")
        if not isinstance(event_type, str) or not event_type:
            raise TypeError("account stream event type must be a string")

        # serverShutdown은 connection event라 subscriptionId 없이도 즉시 Gateway에 전달한다.
        if event_type != "serverShutdown":
            received_subscription_id = parsed_payload.get("subscriptionId")
            with subscription_lock:
                expected_subscription_id = subscription_id[0]
            if received_subscription_id != expected_subscription_id:
                raise WebSocketSubscriptionError(
                    "account event subscription ID does not match ACK"
                )

        on_message(parsed_payload)

    @staticmethod
    def _accept_account_subscription_response(
        payload: Mapping[str, object],
        *,
        subscription_id: list[int | None],
        subscription_lock: RLock,
        lifecycle: _ConnectionLifecycle,
    ) -> None:
        """
        함수 이름: _accept_account_subscription_response()
        기능: status 200 ACK의 subscriptionId를 한 번 확정하고 startup readiness를 연다.
        인자: payload -> request ID가 일치하는 WebSocket API 응답
            subscription_id -> 확정할 단일 subscription ID holder
            subscription_lock -> holder 변경 lock
            lifecycle -> startup readiness owner
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        status = payload.get("status")
        if isinstance(status, bool) or not isinstance(status, int):
            raise TypeError("subscription response status must be an integer")
        if status != 200:
            # 공식 integer code만 진단에 허용하고 비정상 문자열은 credential 반사를 막아 고정 치환한다.
            error_payload = payload.get("error")
            error_code_label = "missing"
            if isinstance(error_payload, Mapping):
                error_code = error_payload.get("code")
                if (
                    not isinstance(error_code, bool)
                    and isinstance(error_code, int)
                ):
                    error_code_label = str(error_code)
                elif error_code is not None:
                    error_code_label = "invalid"
            raise WebSocketSubscriptionError(
                "signature subscription was rejected with status "
                f"{status} and code {error_code_label}"
            )
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise TypeError("subscription response result must be an object")
        received_subscription_id = result.get("subscriptionId")
        if (
            isinstance(received_subscription_id, bool)
            or not isinstance(received_subscription_id, int)
        ):
            raise TypeError("subscriptionId must be an integer")
        if received_subscription_id < 0:
            raise ValueError("subscriptionId must not be negative")

        with subscription_lock:
            if subscription_id[0] is not None:
                raise WebSocketSubscriptionError(
                    "duplicate signature subscription response"
                )
            subscription_id[0] = received_subscription_id
        lifecycle.mark_established()

    def _start_subscription(
        self,
        socket_application: _WebSocketApplication,
        lifecycle: _ConnectionLifecycle,
        *,
        worker_name: str,
        message_dispatcher: _BoundedMessageDispatcher | None = None,
    ) -> _SocketSubscription:
        """
        함수 이름: _start_subscription()
        기능: daemon 수신 loop를 시작하고 startup 계약이 성립한 handle만 반환한다.
        인자: socket_application -> callback 조립이 끝난 실제 또는 fake socket
            lifecycle -> startup ACK와 disconnect 상태 owner
            worker_name -> credential을 포함하지 않는 daemon thread 이름
            message_dispatcher -> account callback FIFO 또는 public stream이면 None
        반환값: established subscription handle
        작성 날짜: 2026/08/22
        """
        if socket_application is None:
            raise WebSocketTransportUnavailableError(
                "socket_factory returned no WebSocket application"
            )

        worker = _SocketWorker(socket_application, lifecycle)
        worker_thread = Thread(
            target=worker.run,
            name=worker_name,
            daemon=True,
        )
        subscription = _SocketSubscription(
            socket_application,
            lifecycle,
            worker_thread,
            message_dispatcher,
        )
        if message_dispatcher is not None:
            message_dispatcher.start()  # ACK 뒤 event가 오기 전에 consumer worker를 준비한다.
        worker_thread.start()
        try:
            lifecycle.wait_until_established(
                self._startup_timeout_seconds
            )
        except Exception:
            subscription.close()
            raise

        return subscription
