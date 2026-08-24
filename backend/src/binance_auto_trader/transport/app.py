"""인증된 stdlib loopback HTTP와 최소 RFC 6455 WebSocket server를 정의한다."""

from __future__ import annotations

import base64
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
import hashlib
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import select
import socket
import struct
from threading import Event, RLock, Thread
from time import monotonic, sleep
from urllib.parse import urlsplit
from uuid import uuid4

from .contracts import (
    JsonObject,
    MAX_HTTP_BODY_BYTES,
    MAX_WEBSOCKET_FRAME_BYTES,
    RuntimeSnapshotSource,
    SCHEMA_VERSION,
    TransportContractError,
    TransportResponse,
    error_response,
    json_bytes,
    map_account_snapshot,
    map_performance,
    map_trade,
    parse_trade_history_query_parameters,
    validate_uuid_text,
)
from .event_stream import BackendEventStream
from .routes import RouteContext
from .routes.csv_export import create_csv_export
from .routes.regime import select_regime
from .routes.snapshot import get_snapshot
from .routes.system import get_health, request_shutdown
from .routes.trade_history import get_trades
from .routes.trading import (
    start_trading,
    stop_trading,
    update_split_ratios,
)


_LOOPBACK_HOST = "127.0.0.1"
_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_WEBSOCKET_AUTH_TIMEOUT_SECONDS = 2.0
_WEBSOCKET_LIVE_WAIT_SECONDS = 0.1
_WEBSOCKET_SEND_TIMEOUT_SECONDS = 5.0
_MAX_REQUEST_TARGET_LENGTH = 8_192
_MAX_IDEMPOTENCY_RECORDS = 10_000
_SESSION_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_ORIGIN_PATTERN = re.compile(
    r"^(?:tauri://[A-Za-z0-9.-]+|https?://(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?)$"
)

_COMMAND_ENDPOINTS = frozenset(
    {
        ("POST", "/v1/regime/selection"),
        ("POST", "/v1/trading/start"),
        ("POST", "/v1/trading/stop"),
        ("PATCH", "/v1/trading/split-ratios"),
        ("POST", "/v1/csv-exports"),
        ("POST", "/v1/shutdown"),
    }
)
_BODY_COMMAND_ENDPOINTS = frozenset(
    {
        ("POST", "/v1/regime/selection"),
        ("POST", "/v1/trading/start"),
        ("POST", "/v1/trading/stop"),
        ("PATCH", "/v1/trading/split-ratios"),
        ("POST", "/v1/csv-exports"),
        ("POST", "/v1/shutdown"),
    }
)
_KNOWN_ENDPOINTS = frozenset(
    {
        ("GET", "/v1/health"),
        ("GET", "/v1/snapshot"),
        ("GET", "/v1/trades"),
        *_COMMAND_ENDPOINTS,
    }
)


@dataclass(frozen=True, slots=True)
class ServerDescriptor:
    """
    클래스 이름: ServerDescriptor
    기능: token을 제외하고 Tauri에 알릴 assigned port, session과 schema를 보존한다.
    작성 날짜: 2026/08/21
    """

    port: int
    session_id: str
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: random loopback port, session UUID와 schema version을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        if isinstance(self.port, bool) or not isinstance(self.port, int):
            raise TypeError("port must be an integer")
        if self.port <= 0 or self.port > 65_535:
            raise ValueError("port must be an assigned TCP port")
        validate_uuid_text(self.session_id, "session_id")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("schema_version must match the transport schema")

    def to_dto(self) -> JsonObject:
        """
        함수 이름: to_dto()
        기능: inherited ready pipe에 기록할 secret 없는 descriptor를 반환한다.
        인자: 없음
        반환값: port, session_id와 schema_version JSON object
        작성 날짜: 2026/08/21
        """
        return {
            "port": self.port,
            "session_id": self.session_id,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class LoopbackTransportApplication:
    """
    클래스 이름: LoopbackTransportApplication
    기능: 동일 observer/event stream을 공유하는 runtime과 loopback server를 조립한다.
    작성 날짜: 2026/08/21
    """

    runtime: RuntimeSnapshotSource
    event_stream: BackendEventStream
    server: "LoopbackTransportServer"
    close_runtime: Callable[[RuntimeSnapshotSource], object]

    def start(self) -> ServerDescriptor:
        """
        함수 이름: start()
        기능: 조립된 ready runtime의 loopback server를 actual background thread에서 시작한다.
        인자: 없음
        반환값: secret 없는 ready descriptor
        작성 날짜: 2026/08/21
        """
        # Ready 의미가 사라진 runtime의 port를 외부 process에 공개하지 않는다.
        _require_runtime_ready(self.runtime)
        return self.server.start()

    def stop(self) -> None:
        """
        함수 이름: stop()
        기능: account callback source인 runtime을 먼저 닫고 transport stream과 server를 종료한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        try:
            self.close_runtime(self.runtime)
        finally:
            self.server.stop()


@dataclass(frozen=True, slots=True)
class _IdempotencyRecord:
    """
    클래스 이름: _IdempotencyRecord
    기능: 한 command key의 요청 fingerprint와 최초 응답을 불변으로 보존한다.
    작성 날짜: 2026/08/21
    """

    fingerprint: bytes
    response: TransportResponse


@dataclass(frozen=True, slots=True)
class _IdempotencyFlight:
    """
    클래스 이름: _IdempotencyFlight
    기능: 실행 중인 한 command key의 fingerprint와 공유 terminal 결과를 보존한다.
    작성 날짜: 2026/08/24
    """

    fingerprint: bytes
    response_future: Future[TransportResponse]


def _replay_response_for_request(
    response: TransportResponse,
    request_id: str,
) -> TransportResponse:
    """
    함수 이름: _replay_response_for_request()
    기능: 최초 command 결과를 보존하면서 envelope correlation ID만 현재 요청에 맞춘다.
    인자: response -> 최초 또는 single-flight leader의 terminal 응답
        request_id -> replay를 요청한 현재 HTTP 요청 UUID
    반환값: 현재 request ID와 최초 status/data/error를 가진 새 TransportResponse
    작성 날짜: 2026/08/24
    """
    replay_payload = dict(response.payload)
    replay_payload[
        "request_id"
    ] = request_id  # 업무 결과는 재사용하되 transport correlation은 각 요청마다 고유하다.
    return TransportResponse(
        status=response.status,
        payload=replay_payload,
    )


@dataclass(frozen=True, slots=True)
class _WebSocketFrame:
    """
    클래스 이름: _WebSocketFrame
    기능: 검증을 마친 단일 RFC 6455 client frame opcode와 payload를 보존한다.
    작성 날짜: 2026/08/21
    """

    opcode: int
    payload: bytes


class _WebSocketClosed(ConnectionError):
    """
    클래스 이름: _WebSocketClosed
    기능: peer socket EOF로 WebSocket 처리가 정상 종료됐음을 나타낸다.
    작성 날짜: 2026/08/21
    """


class _WebSocketProtocolError(RuntimeError):
    """
    클래스 이름: _WebSocketProtocolError
    기능: client frame 위반에 사용할 close code와 token 없는 reason을 보존한다.
    작성 날짜: 2026/08/21
    """

    def __init__(self, close_code: int, reason: str) -> None:
        """
        함수 이름: __init__()
        기능: RFC 6455 protocol close code와 안전한 reason을 생성한다.
        인자: close_code -> WebSocket close status code
            reason -> credential을 포함하지 않는 짧은 설명
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        super().__init__(reason)
        self.close_code = close_code
        self.reason = reason


class _WebSocketConnection:
    """
    클래스 이름: _WebSocketConnection
    기능: client masking을 검증하고 server text, pong과 close frame을 직렬화한다.
    작성 날짜: 2026/08/21
    """

    def __init__(self, client_socket: socket.socket) -> None:
        """
        함수 이름: __init__()
        기능: HTTP upgrade가 끝난 connected socket을 WebSocket frame 경계로 감싼다.
        인자: client_socket -> BaseHTTPRequestHandler가 소유한 connected socket
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        if not isinstance(client_socket, socket.socket):
            raise TypeError("client_socket must be a socket")

        self._socket = client_socket
        self._close_sent = False

    @property
    def socket(self) -> socket.socket:
        """
        함수 이름: socket()
        기능: select로 client control frame readiness를 확인할 underlying socket을 반환한다.
        인자: 없음
        반환값: connected client socket
        작성 날짜: 2026/08/21
        """
        return self._socket  # socket identity는 connection lifetime 동안 고정된다.

    def receive_frame(
        self,
        *,
        deadline: float | None = None,
    ) -> _WebSocketFrame:
        """
        함수 이름: receive_frame()
        기능: masking, FIN, opcode와 1 MiB 제한을 검증한 client frame 하나를 읽는다.
        인자: deadline -> slowloris를 막을 absolute monotonic deadline 또는 None
        반환값: 검증된 WebSocket frame
        작성 날짜: 2026/08/21
        """
        initial_header = self._receive_exact(2, deadline=deadline)
        first_byte, second_byte = initial_header
        final_frame = bool(first_byte & 0x80)
        reserved_bits = first_byte & 0x70
        opcode = first_byte & 0x0F
        masked = bool(second_byte & 0x80)
        payload_length = second_byte & 0x7F

        # 최소 transport는 fragmentation과 extension을 협상하지 않으므로 둘 다 거부한다.
        if not final_frame or reserved_bits != 0:
            raise _WebSocketProtocolError(1002, "fragmented frames are not supported")
        if opcode not in (0x1, 0x8, 0x9, 0xA):
            close_code = 1003 if opcode == 0x2 else 1002
            raise _WebSocketProtocolError(close_code, "unsupported frame opcode")
        if not masked:
            raise _WebSocketProtocolError(1002, "client frames must be masked")

        # Extended payload length를 network byte order로 읽고 최상위 reserved bit를 차단한다.
        if payload_length == 126:
            payload_length = struct.unpack(
                "!H",
                self._receive_exact(2, deadline=deadline),
            )[0]
        elif payload_length == 127:
            encoded_length = self._receive_exact(8, deadline=deadline)
            if encoded_length[0] & 0x80:
                raise _WebSocketProtocolError(1002, "invalid frame length")
            payload_length = struct.unpack("!Q", encoded_length)[0]
        if payload_length > MAX_WEBSOCKET_FRAME_BYTES:
            raise _WebSocketProtocolError(1009, "frame exceeds one MiB")
        if opcode in (0x8, 0x9, 0xA) and payload_length > 125:
            raise _WebSocketProtocolError(1002, "control frame is too large")

        masking_key = self._receive_exact(4, deadline=deadline)
        masked_payload = self._receive_exact(
            payload_length,
            deadline=deadline,
        )
        payload = bytes(
            byte ^ masking_key[index % 4]
            for index, byte in enumerate(masked_payload)
        )
        return _WebSocketFrame(opcode=opcode, payload=payload)

    def send_text_object(self, payload: Mapping[str, object]) -> None:
        """
        함수 이름: send_text_object()
        기능: JSON object를 unmasked server text frame으로 전송한다.
        인자: payload -> 전송할 versioned event 또는 control object
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        encoded_payload = json_bytes(payload)
        if len(encoded_payload) > MAX_WEBSOCKET_FRAME_BYTES:
            raise _WebSocketProtocolError(1009, "server frame exceeds one MiB")

        self._send_frame(0x1, encoded_payload)

    def send_pong(self, payload: bytes) -> None:
        """
        함수 이름: send_pong()
        기능: client ping payload를 그대로 포함한 pong control frame을 전송한다.
        인자: payload -> 125 bytes 이하 ping payload
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        if len(payload) > 125:
            raise ValueError("pong payload must not exceed 125 bytes")

        self._send_frame(0xA, payload)

    def send_close(self, close_code: int = 1000, reason: str = "") -> None:
        """
        함수 이름: send_close()
        기능: connection마다 최대 한 번 UTF-8 close frame을 전송한다.
        인자: close_code -> RFC 6455 close status code
            reason -> token 없는 짧은 종료 설명
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        if self._close_sent:
            return

        encoded_reason = reason.encode("utf-8")
        if len(encoded_reason) > 123:
            encoded_reason = encoded_reason[:123]
        close_payload = struct.pack("!H", close_code) + encoded_reason
        self._send_frame(0x8, close_payload)
        self._close_sent = True

    def _receive_exact(
        self,
        byte_count: int,
        *,
        deadline: float | None = None,
    ) -> bytes:
        """
        함수 이름: _receive_exact()
        기능: socket EOF를 구분하며 frame 구성 bytes를 지정 길이만큼 읽는다.
        인자: byte_count -> 읽어야 하는 정확한 byte 수
            deadline -> 모든 partial recv가 공유할 absolute monotonic deadline
        반환값: 요청한 길이의 bytes
        작성 날짜: 2026/08/21
        """
        received_chunks: list[bytes] = []
        remaining_count = byte_count

        # TCP packet 경계가 frame 경계와 다르므로 필요한 길이까지 반복 수신한다.
        while remaining_count > 0:
            if deadline is not None:
                remaining_seconds = deadline - monotonic()
                if remaining_seconds <= 0:
                    raise socket.timeout("WebSocket frame deadline elapsed")
                self._socket.settimeout(remaining_seconds)
            received_chunk = self._socket.recv(remaining_count)
            if not received_chunk:
                raise _WebSocketClosed("peer closed the socket")
            received_chunks.append(received_chunk)
            remaining_count -= len(received_chunk)

        return b"".join(received_chunks)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        """
        함수 이름: _send_frame()
        기능: FIN이 설정된 unmasked server frame header와 payload를 기록한다.
        인자: opcode -> text, pong 또는 close opcode
            payload -> frame payload bytes
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        payload_length = len(payload)
        if payload_length < 126:
            frame_header = bytes((0x80 | opcode, payload_length))
        elif payload_length <= 65_535:
            frame_header = bytes((0x80 | opcode, 126)) + struct.pack(
                "!H",
                payload_length,
            )
        else:
            frame_header = bytes((0x80 | opcode, 127)) + struct.pack(
                "!Q",
                payload_length,
            )

        self._socket.sendall(frame_header + payload)


class _LoopbackHttpServer(ThreadingHTTPServer):
    """
    클래스 이름: _LoopbackHttpServer
    기능: request handler thread와 transport owner 참조를 가진 loopback HTTP server다.
    작성 날짜: 2026/08/21
    """

    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, owner: "LoopbackTransportServer") -> None:
        """
        함수 이름: __init__()
        기능: IPv4 127.0.0.1과 OS random port에만 HTTP server를 bind한다.
        인자: owner -> 인증, route와 event stream을 소유한 transport server
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.transport_owner = owner
        super().__init__(
            (_LOOPBACK_HOST, 0),
            _LoopbackRequestHandler,
            bind_and_activate=True,
        )


class _LoopbackRequestHandler(BaseHTTPRequestHandler):
    """
    클래스 이름: _LoopbackRequestHandler
    기능: body나 header를 기록하지 않고 HTTP method를 transport owner에 위임한다.
    작성 날짜: 2026/08/21
    """

    protocol_version = "HTTP/1.1"
    server_version = "BinanceAutoLoopback/1"
    sys_version = ""

    def __getattr__(self, attribute_name: str) -> object:
        """
        함수 이름: __getattr__()
        기능: stdlib 기본 501 HTML 대신 모든 unknown HTTP method를 transport dispatcher로 보낸다.
        인자: attribute_name -> BaseHTTPRequestHandler가 찾는 do_METHOD 이름
        반환값: unsupported method dispatcher
        작성 날짜: 2026/08/21
        """
        if attribute_name.startswith("do_"):
            return self._dispatch_unsupported_method

        raise AttributeError(attribute_name)

    def _dispatch_unsupported_method(self) -> None:
        """
        함수 이름: _dispatch_unsupported_method()
        기능: PUT, DELETE, HEAD, TRACE 등 unknown method도 인증과 공통 envelope를 거치게 한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self._transport_owner.handle_request(self)

    def do_GET(self) -> None:
        """
        함수 이름: do_GET()
        기능: query 또는 WebSocket GET을 공통 transport dispatcher에 전달한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self._transport_owner.handle_request(self)

    def do_POST(self) -> None:
        """
        함수 이름: do_POST()
        기능: POST command를 공통 transport dispatcher에 전달한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self._transport_owner.handle_request(self)

    def do_PATCH(self) -> None:
        """
        함수 이름: do_PATCH()
        기능: PATCH command를 공통 transport dispatcher에 전달한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self._transport_owner.handle_request(self)

    def do_OPTIONS(self) -> None:
        """
        함수 이름: do_OPTIONS()
        기능: exact Origin의 browser CORS preflight를 transport dispatcher에 전달한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self._transport_owner.handle_options(self)

    def log_message(self, format_text: str, *arguments: object) -> None:
        """
        함수 이름: log_message()
        기능: stdlib의 raw path/header 기반 stderr access log를 비활성화한다.
        인자: format_text -> 사용하지 않는 log format
            arguments -> 사용하지 않는 log 인자
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        return  # token, query와 raw path가 기본 stderr log에 남지 않는다.

    @property
    def _transport_owner(self) -> "LoopbackTransportServer":
        """
        함수 이름: _transport_owner()
        기능: typed HTTP server에서 transport owner 참조를 반환한다.
        인자: 없음
        반환값: 현재 request의 LoopbackTransportServer
        작성 날짜: 2026/08/21
        """
        http_server = self.server
        if not isinstance(http_server, _LoopbackHttpServer):
            raise RuntimeError("request handler is attached to an invalid server")

        return http_server.transport_owner


class LoopbackTransportServer:
    """
    클래스 이름: LoopbackTransportServer
    기능: loopback bind, HTTP 인증, route, idempotency와 WebSocket replay를 소유한다.
    작성 날짜: 2026/08/21
    """

    def __init__(
        self,
        runtime: RuntimeSnapshotSource,
        session_token: str,
        *,
        allowed_origins: Sequence[str],
        event_stream: BackendEventStream | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 주입 token과 runtime으로 random-port loopback server를 준비한다.
        인자: runtime -> bootstrap ApplicationRuntime compatible source
            session_token -> inherited pipe 또는 memory로 받은 32-byte base64url token
            allowed_origins -> wildcard 없는 exact Tauri와 optional dev origin
            event_stream -> runtime observer와 공유할 event stream 또는 새 stream
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        if not hasattr(runtime, "application_lock"):
            raise TypeError("runtime must expose application_lock")
        validated_token = _validate_session_token(session_token)
        normalized_origins = _validate_allowed_origins(allowed_origins)
        selected_event_stream = (
            BackendEventStream()
            if event_stream is None
            else event_stream
        )
        if not isinstance(selected_event_stream, BackendEventStream):
            raise TypeError("event_stream must be a BackendEventStream")

        # HTTP server가 bind한 뒤에만 실제 Host allowlist와 ready descriptor를 계산한다.
        self._runtime = runtime
        self._session_token = validated_token
        self._allowed_origins = normalized_origins
        self._event_stream = selected_event_stream
        self._route_context = RouteContext(runtime, selected_event_stream)
        self._lifecycle_lock = RLock()
        self._idempotency_lock = RLock()
        self._idempotency_records: dict[str, _IdempotencyRecord] = {}
        self._idempotency_order: deque[str] = deque()
        self._idempotency_flights: dict[str, _IdempotencyFlight] = {}
        self._shutdown_response_flushed = Event()
        self._active_request_count = 0
        self._http_server = _LoopbackHttpServer(self)
        self._server_thread: Thread | None = None
        self._started = False
        self._stopped = False

    @property
    def descriptor(self) -> ServerDescriptor:
        """
        함수 이름: descriptor()
        기능: assigned random port와 event session을 secret 없이 반환한다.
        인자: 없음
        반환값: Tauri ready pipe용 ServerDescriptor
        작성 날짜: 2026/08/21
        """
        assigned_host, assigned_port = self._http_server.server_address
        if assigned_host != _LOOPBACK_HOST:
            raise RuntimeError("transport server is not bound to IPv4 loopback")

        return ServerDescriptor(
            port=assigned_port,
            session_id=self._event_stream.session_id,
        )

    @property
    def event_stream(self) -> BackendEventStream:
        """
        함수 이름: event_stream()
        기능: bootstrap observer와 WebSocket이 공유하는 event stream을 반환한다.
        인자: 없음
        반환값: BackendEventStream
        작성 날짜: 2026/08/21
        """
        return self._event_stream  # server lifetime 동안 stream identity를 유지한다.

    @property
    def shutdown_response_flushed(self) -> bool:
        """
        함수 이름: shutdown_response_flushed()
        기능: accepted shutdown HTTP body가 client socket buffer까지 기록됐는지 반환한다.
        인자: 없음
        반환값: HTTP 202 response flush가 끝났으면 True
        작성 날짜: 2026/08/24
        """
        with self._lifecycle_lock:
            return (
                self._shutdown_response_flushed.is_set()
                and self._active_request_count == 0
            )  # Accepted body와 이미 수락한 concurrent handler가 모두 끝나야 한다.

    def start(self) -> ServerDescriptor:
        """
        함수 이름: start()
        기능: background thread에서 actual loopback HTTP server를 멱등 시작한다.
        인자: 없음
        반환값: token을 포함하지 않는 ready descriptor
        작성 날짜: 2026/08/21
        """
        with self._lifecycle_lock:
            if self._stopped:
                raise RuntimeError("stopped transport server cannot be restarted")
            if self._started:
                return self.descriptor

            # daemon server thread는 명시 stop에서 shutdown되고 process exit를 막지 않는다.
            self._server_thread = Thread(
                target=self._http_server.serve_forever,
                name="binance-auto-loopback",
                daemon=True,
            )
            self._server_thread.start()
            self._started = True

        return self.descriptor

    def stop(self) -> None:
        """
        함수 이름: stop()
        기능: 신규 event를 막고 HTTP accept loop와 active handler를 멱등 종료한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        with self._lifecycle_lock:
            if self._stopped:
                return

            self._stopped = True
            started = self._started
            server_thread = self._server_thread
            self._event_stream.close()
            self._session_token = ""  # shutdown 시 장기 token 참조를 제거한다.

        # shutdown은 serve_forever thread 밖에서 호출해야 하므로 lock 밖에서 수행한다.
        if started:
            self._http_server.shutdown()
        self._http_server.server_close()
        if server_thread is not None:
            server_thread.join(timeout=5.0)

    def handle_request(self, handler: _LoopbackRequestHandler) -> None:
        """
        함수 이름: handle_request()
        기능: process exit가 진행 중 handler를 자르지 않도록 request lifetime을 계수한다.
        인자: handler -> 현재 stdlib HTTP request handler
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with self._lifecycle_lock:
            self._active_request_count += 1  # 첫 byte 검증 전부터 handler lifetime을 소유한다.

        try:
            self._handle_authenticated_request(handler)
        finally:
            with self._lifecycle_lock:
                self._active_request_count -= 1
                if self._active_request_count < 0:
                    raise RuntimeError("active request count cannot be negative")

    def _handle_authenticated_request(
        self,
        handler: _LoopbackRequestHandler,
    ) -> None:
        """
        함수 이름: _handle_authenticated_request()
        기능: Host/Origin/auth/header/body 검증 뒤 HTTP route 또는 WebSocket을 실행한다.
        인자: handler -> 현재 stdlib HTTP request handler
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        response_request_id = _safe_request_id(handler)
        response_origin: str | None = None
        response_path: str | None = None

        try:
            request_target = _parse_request_target(handler.path)
            response_path = request_target.path
            response_origin = self._validate_host_and_origin(handler)
            if request_target.path == "/v1/events":
                self._handle_websocket_upgrade(handler, request_target)
                return

            # HTTP endpoint는 WebSocket과 달리 Bearer와 request UUID를 header로 검증한다.
            self._validate_http_authentication(handler)
            request_id = _require_request_id(handler)
            response_request_id = request_id
            if request_target.query and request_target.path != "/v1/trades":
                raise TransportContractError(
                    "MALFORMED_REQUEST",
                    "This endpoint does not accept query parameters.",
                    status=400,
                )
            response = self._dispatch_http_request(
                handler,
                request_target.path,
                request_id,
                request_query=request_target.query,
            )
        except TransportContractError as error:
            response = error_response(response_request_id, error)
        except Exception:
            # raw exception, body, header와 stack trace를 network response나 log에 넣지 않는다.
            internal_error = TransportContractError(
                "INTERNAL_TRANSPORT_ERROR",
                "The loopback transport could not process the request.",
                status=500,
                retryable=False,
            )
            response = error_response(response_request_id, internal_error)

        _send_http_response(handler, response, response_origin)
        if (
            response_path == "/v1/shutdown"
            and response.status == 202
            and response.payload.get("ok") is True
        ):
            self._shutdown_response_flushed.set()  # body flush 이후에만 process exit를 허용한다.

    def handle_options(self, handler: _LoopbackRequestHandler) -> None:
        """
        함수 이름: handle_options()
        기능: credential 없이 exact Host/Origin과 허용 method/header만 CORS preflight한다.
        인자: handler -> 현재 OPTIONS request handler
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        request_id = _safe_request_id(handler)
        response_origin: str | None = None

        try:
            request_target = _parse_request_target(handler.path)
            if not request_target.path.startswith("/v1/"):
                raise TransportContractError(
                    "ROUTE_NOT_FOUND",
                    "The requested route does not exist.",
                    status=404,
                )
            if request_target.query:
                if request_target.path != "/v1/trades":
                    raise TransportContractError(
                        "MALFORMED_REQUEST",
                        "CORS preflight does not accept query parameters.",
                        status=400,
                    )
                parse_trade_history_query_parameters(
                    request_target.query
                )  # Browser preflight도 실제 GET과 동일한 exact query만 허용한다.
            response_origin = self._validate_host_and_origin(handler)
            _require_empty_request_body(handler)
            requested_method = _require_single_header(
                handler,
                "Access-Control-Request-Method",
            ).upper()
            if requested_method not in ("GET", "POST", "PATCH"):
                raise TransportContractError(
                    "CORS_PREFLIGHT_REJECTED",
                    "The requested CORS method is not allowed.",
                    status=403,
                )
            if (requested_method, request_target.path) not in _KNOWN_ENDPOINTS:
                raise TransportContractError(
                    "CORS_PREFLIGHT_REJECTED",
                    "The requested CORS route and method are not allowed.",
                    status=403,
                )

            requested_headers_text = _optional_single_header(
                handler,
                "Access-Control-Request-Headers",
            )
            requested_headers = {
                header_name.strip().lower()
                for header_name in requested_headers_text.split(",")
                if header_name.strip()
            }
            allowed_headers = {
                "authorization",
                "content-type",
                "idempotency-key",
                "x-request-id",
            }
            if not requested_headers.issubset(allowed_headers):
                raise TransportContractError(
                    "CORS_PREFLIGHT_REJECTED",
                    "The requested CORS headers are not allowed.",
                    status=403,
                )
        except TransportContractError as error:
            _send_http_response(
                handler,
                error_response(request_id, error),
                response_origin,
            )
            return

        # Browser cookie credential을 허용하지 않고 exact Origin만 echo한다.
        handler.send_response(HTTPStatus.NO_CONTENT)
        handler.send_header("Access-Control-Allow-Origin", response_origin)
        handler.send_header("Vary", "Origin")
        handler.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH")
        handler.send_header(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type, Idempotency-Key, X-Request-Id",
        )
        handler.send_header("Access-Control-Max-Age", "600")
        handler.send_header("Content-Length", "0")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.close_connection = True  # 읽지 않은 body가 다음 request로 이어지지 않게 한다.

    def _validate_host_and_origin(
        self,
        handler: _LoopbackRequestHandler,
    ) -> str:
        """
        함수 이름: _validate_host_and_origin()
        기능: 실제 assigned Host와 configured exact Origin만 허용한다.
        인자: handler -> 검사할 request header를 가진 handler
        반환값: 검증된 exact Origin
        작성 날짜: 2026/08/21
        """
        expected_host = f"{_LOOPBACK_HOST}:{self.descriptor.port}"
        received_host = _require_single_header(handler, "Host")
        received_origin = _require_single_header(handler, "Origin")
        if received_host != expected_host:
            raise TransportContractError(
                "LOOPBACK_REQUEST_REJECTED",
                "The request host or origin is not allowed.",
                status=403,
            )
        if received_origin not in self._allowed_origins:
            raise TransportContractError(
                "LOOPBACK_REQUEST_REJECTED",
                "The request host or origin is not allowed.",
                status=403,
            )

        return received_origin

    def _validate_http_authentication(
        self,
        handler: _LoopbackRequestHandler,
    ) -> None:
        """
        함수 이름: _validate_http_authentication()
        기능: Bearer token을 malformed 여부와 관계없이 constant-time 비교한다.
        인자: handler -> Authorization header를 가진 request handler
        반환값: token이 일치하면 없음
        작성 날짜: 2026/08/21
        """
        authorization_values = handler.headers.get_all("Authorization") or []
        authorization_text = (
            authorization_values[0]
            if len(authorization_values) == 1
            else ""
        )
        has_bearer_prefix = authorization_text.startswith("Bearer ")
        candidate_token = (
            authorization_text[len("Bearer "):]
            if has_bearer_prefix
            else ""
        )

        # prefix와 중복 여부를 별도로 검사하되 token bytes 비교는 항상 실행한다.
        token_matches = hmac.compare_digest(candidate_token, self._session_token)
        if (
            len(authorization_values) != 1
            or not has_bearer_prefix
            or not token_matches
        ):
            raise TransportContractError(
                "AUTHENTICATION_REQUIRED",
                "Loopback authentication is required.",
                status=401,
            )

    def _dispatch_http_request(
        self,
        handler: _LoopbackRequestHandler,
        request_path: str,
        request_id: str,
        *,
        request_query: str = "",
    ) -> TransportResponse:
        """
        함수 이름: _dispatch_http_request()
        기능: body와 idempotency shape를 검증하고 고정 endpoint route를 호출한다.
        인자: handler -> method, header와 body를 가진 request handler
            request_path -> query를 제거한 canonical request path
            request_id -> 검증을 마친 request UUID
            request_query -> percent-encoding을 보존한 raw query 문자열
        반환값: route의 공통 envelope 응답
        작성 날짜: 2026/08/21
        """
        method = handler.command.upper()
        endpoint_key = (method, request_path)
        if endpoint_key not in _KNOWN_ENDPOINTS:
            matching_paths = {
                path
                for _, path in _KNOWN_ENDPOINTS
                if path == request_path
            }
            if matching_paths:
                raise TransportContractError(
                    "METHOD_NOT_ALLOWED",
                    "The request method is not allowed for this route.",
                    status=405,
                )
            raise TransportContractError(
                "ROUTE_NOT_FOUND",
                "The requested route does not exist.",
                status=404,
            )

        # GET route는 request body를 허용하지 않고 command만 versioned JSON을 읽는다.
        if method == "GET":
            _require_empty_request_body(handler)
            return self._route_http_request(
                endpoint_key,
                request_id,
                request_query=request_query,
            )  # Trade history만 검증 전 raw query를 route에 전달한다.

        request_body, raw_body = _read_json_request_body(handler)
        _validate_request_schema(request_body)
        idempotency_key = _require_idempotency_key(handler)
        request_fingerprint = hashlib.sha256(
            method.encode("ascii")
            + b"\x00"
            + request_path.encode("ascii")
            + b"\x00"
            + raw_body
        ).digest()

        # 짧은 global 임계 구역에서는 cache 조회와 key별 single-flight 예약만 수행한다.
        with self._idempotency_lock:
            cached_response = self._lookup_idempotency_response(
                idempotency_key,
                request_fingerprint,
                request_id,
            )
            if cached_response is not None:
                return cached_response

            existing_flight = self._idempotency_flights.get(idempotency_key)
            if existing_flight is None:
                command_flight = _IdempotencyFlight(
                    fingerprint=request_fingerprint,
                    response_future=Future(),
                )
                self._idempotency_flights[idempotency_key] = command_flight
                owns_command_execution = True
            elif hmac.compare_digest(
                existing_flight.fingerprint,
                request_fingerprint,
            ):
                command_flight = existing_flight
                owns_command_execution = False
            else:
                conflict = TransportContractError(
                    "IDEMPOTENCY_CONFLICT",
                    "The idempotency key was already used for different content.",
                    status=409,
                )
                return error_response(request_id, conflict)

        if not owns_command_execution:
            # 같은 key/body만 route 밖에서 첫 실행의 terminal 결과를 기다린다.
            shared_response = command_flight.response_future.result()
            return _replay_response_for_request(shared_response, request_id)

        try:
            # 서로 다른 key의 장기 export와 stop/shutdown은 global lock 없이 병행할 수 있다.
            response = self._route_http_request(
                endpoint_key,
                request_id,
                request_body=request_body,
                command_id=idempotency_key,
            )
        except BaseException as error:
            # 예기치 않은 route 실패도 동일 key waiter를 영구 대기시키지 않고 함께 종료한다.
            with self._idempotency_lock:
                if self._idempotency_flights.get(idempotency_key) is command_flight:
                    self._idempotency_flights.pop(idempotency_key, None)
                command_flight.response_future.set_exception(error)
            raise

        with self._idempotency_lock:
            # Terminal 응답은 cache에 먼저 게시한 뒤 waiter를 깨워 새 동일-key race를 막는다.
            self._store_idempotency_response(
                idempotency_key,
                request_fingerprint,
                response,
            )
            if self._idempotency_flights.get(idempotency_key) is command_flight:
                self._idempotency_flights.pop(idempotency_key, None)
            command_flight.response_future.set_result(response)

        return response

    def _route_http_request(
        self,
        endpoint_key: tuple[str, str],
        request_id: str,
        *,
        request_body: JsonObject | None = None,
        command_id: str | None = None,
        request_query: str = "",
    ) -> TransportResponse:
        """
        함수 이름: _route_http_request()
        기능: 검증된 method/path를 business-free route 함수에 전달한다.
        인자: endpoint_key -> canonical HTTP method와 path
            request_id -> 검증된 request UUID
            request_body -> command의 검증된 JSON object 또는 GET이면 None
            command_id -> 검증된 Idempotency-Key 또는 GET이면 None
            request_query -> Trade history route의 raw query 문자열
        반환값: route TransportResponse
        작성 날짜: 2026/08/21
        """
        route_by_endpoint = {
            ("GET", "/v1/health"): get_health,
            ("GET", "/v1/snapshot"): get_snapshot,
            ("GET", "/v1/trades"): get_trades,
            ("POST", "/v1/regime/selection"): select_regime,
            ("POST", "/v1/trading/start"): start_trading,
            ("POST", "/v1/trading/stop"): stop_trading,
            ("PATCH", "/v1/trading/split-ratios"): update_split_ratios,
            ("POST", "/v1/csv-exports"): create_csv_export,
            ("POST", "/v1/shutdown"): request_shutdown,
        }
        route_function = route_by_endpoint[endpoint_key]

        # Versioned mutation command는 body와 command ID를 각 application owner 경계에 전달한다.
        if endpoint_key in _BODY_COMMAND_ENDPOINTS:
            if request_body is None or command_id is None:
                raise RuntimeError("command route requires body and command ID")
            return route_function(
                request_id,
                self._route_context,
                request_body,
                command_id,
            )

        # Trade history는 period와 side를 한 번에 검증하도록 raw query를 보존한다.
        if endpoint_key == ("GET", "/v1/trades"):
            return route_function(
                request_id,
                self._route_context,
                request_query,
            )  # Percent-decoding과 duplicate 판정은 contract owner가 수행한다.

        return route_function(
            request_id,
            self._route_context,
        )  # Query가 없는 GET route는 기존 두 인자 계약을 유지한다.

    def _lookup_idempotency_response(
        self,
        idempotency_key: str,
        request_fingerprint: bytes,
        request_id: str,
    ) -> TransportResponse | None:
        """
        함수 이름: _lookup_idempotency_response()
        기능: 동일 key/body 응답을 반환하고 동일 key의 다른 body를 409로 거부한다.
        인자: idempotency_key -> 검증된 command idempotency key
            request_fingerprint -> method, path와 raw body SHA-256
            request_id -> conflict envelope에 사용할 요청 UUID
        반환값: cached 응답, cache miss면 None, conflict면 409 응답
        작성 날짜: 2026/08/21
        """
        with self._idempotency_lock:
            existing_record = self._idempotency_records.get(idempotency_key)
            if existing_record is None:
                return None
            if hmac.compare_digest(
                existing_record.fingerprint,
                request_fingerprint,
            ):
                return _replay_response_for_request(
                    existing_record.response,
                    request_id,
                )

        conflict = TransportContractError(
            "IDEMPOTENCY_CONFLICT",
            "The idempotency key was already used for different content.",
            status=409,
        )
        return error_response(request_id, conflict)

    def _store_idempotency_response(
        self,
        idempotency_key: str,
        request_fingerprint: bytes,
        response: TransportResponse,
    ) -> None:
        """
        함수 이름: _store_idempotency_response()
        기능: bounded process-session cache에 최초 command 응답을 저장한다.
        인자: idempotency_key -> command idempotency key
            request_fingerprint -> method, path와 raw body SHA-256
            response -> 재요청에 그대로 반환할 최초 응답
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 재시도 가능한 준비 상태 실패는 같은 key가 정상 재평가될 수 있도록 저장하지 않는다.
        response_error = response.payload.get("error")
        if (
            response.payload.get("ok") is False
            and isinstance(response_error, Mapping)
            and response_error.get("retryable") is True
        ):
            return

        # 최초 terminal 응답만 key와 fingerprint에 결합해 process-session cache에 저장한다.
        with self._idempotency_lock:
            if idempotency_key in self._idempotency_records:
                return

            self._idempotency_records[idempotency_key] = _IdempotencyRecord(
                fingerprint=request_fingerprint,
                response=response,
            )
            self._idempotency_order.append(idempotency_key)

            # adversarial unique key 요청이 process memory를 무제한 늘리지 못하게 제한한다.
            while len(self._idempotency_order) > _MAX_IDEMPOTENCY_RECORDS:
                expired_key = self._idempotency_order.popleft()
                self._idempotency_records.pop(expired_key, None)

    def _handle_websocket_upgrade(
        self,
        handler: _LoopbackRequestHandler,
        request_target: object,
    ) -> None:
        """
        함수 이름: _handle_websocket_upgrade()
        기능: exact `/v1/events`를 RFC 6455로 upgrade하고 first-frame auth를 수행한다.
        인자: handler -> upgrade header와 connected socket을 가진 handler
            request_target -> query 유무를 검사할 parsed URL target
        반환값: WebSocket connection 종료 시 없음
        작성 날짜: 2026/08/21
        """
        if handler.command != "GET":
            raise TransportContractError(
                "METHOD_NOT_ALLOWED",
                "WebSocket events require GET.",
                status=405,
            )
        if getattr(request_target, "query", ""):
            raise TransportContractError(
                "WEBSOCKET_QUERY_NOT_ALLOWED",
                "WebSocket authentication data must not use the URL.",
                status=400,
            )
        _require_empty_request_body(handler)
        if _optional_single_header(handler, "Sec-WebSocket-Protocol"):
            raise TransportContractError(
                "WEBSOCKET_SUBPROTOCOL_NOT_ALLOWED",
                "WebSocket subprotocol authentication is not allowed.",
                status=400,
            )

        # Browser extension offer는 선택하지 않으면 되므로 값을 검증해 읽되 협상하지 않는다.
        _optional_single_header(handler, "Sec-WebSocket-Extensions")

        # Upgrade, Connection, version과 16-byte nonce를 RFC 6455 규칙대로 검증한다.
        upgrade_header = _require_single_header(handler, "Upgrade")
        connection_header = _require_single_header(handler, "Connection")
        websocket_version = _require_single_header(handler, "Sec-WebSocket-Version")
        websocket_key = _require_single_header(handler, "Sec-WebSocket-Key")
        if upgrade_header.lower() != "websocket":
            raise TransportContractError(
                "WEBSOCKET_UPGRADE_REQUIRED",
                "A valid WebSocket upgrade is required.",
                status=400,
            )
        connection_tokens = {
            token.strip().lower()
            for token in connection_header.split(",")
        }
        if "upgrade" not in connection_tokens or websocket_version != "13":
            raise TransportContractError(
                "WEBSOCKET_UPGRADE_REQUIRED",
                "A valid WebSocket upgrade is required.",
                status=400,
            )
        try:
            decoded_key = base64.b64decode(websocket_key, validate=True)
        except (ValueError, base64.binascii.Error) as error:
            raise TransportContractError(
                "WEBSOCKET_UPGRADE_REQUIRED",
                "A valid WebSocket upgrade is required.",
                status=400,
            ) from error
        if len(decoded_key) != 16:
            raise TransportContractError(
                "WEBSOCKET_UPGRADE_REQUIRED",
                "A valid WebSocket upgrade is required.",
                status=400,
            )

        accept_digest = hashlib.sha1(
            (websocket_key + _WEBSOCKET_GUID).encode("ascii")
        ).digest()
        accept_value = base64.b64encode(accept_digest).decode("ascii")
        handler.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        handler.send_header("Upgrade", "websocket")
        handler.send_header("Connection", "Upgrade")
        handler.send_header("Sec-WebSocket-Accept", accept_value)
        handler.end_headers()
        handler.close_connection = True

        self._serve_websocket(handler.connection)

    def _serve_websocket(self, client_socket: socket.socket) -> None:
        """
        함수 이름: _serve_websocket()
        기능: 2초 first-frame auth 뒤 replay와 live event를 sequence 순서로 전송한다.
        인자: client_socket -> upgrade가 완료된 connected client socket
        반환값: peer 또는 server가 connection을 닫으면 없음
        작성 날짜: 2026/08/21
        """
        websocket = _WebSocketConnection(client_socket)
        try:
            authentication_deadline = (
                monotonic() + _WEBSOCKET_AUTH_TIMEOUT_SECONDS
            )
            authentication_frame = websocket.receive_frame(
                deadline=authentication_deadline
            )
            after_sequence = self._authenticate_websocket_frame(
                websocket,
                authentication_frame,
            )
            client_socket.settimeout(_WEBSOCKET_SEND_TIMEOUT_SECONDS)

            # 인증이 성공하기 전에는 replay를 포함한 application event를 보내지 않는다.
            current_sequence = after_sequence
            while True:
                replay_batch = self._event_stream.wait_for_events(
                    current_sequence,
                    timeout=_WEBSOCKET_LIVE_WAIT_SECONDS,
                )
                if replay_batch.requires_resync:
                    if replay_batch.resync_reason is None:
                        raise RuntimeError("resync batch has no reason")
                    websocket.send_text_object(
                        self._event_stream.build_resync_control(
                            replay_batch.resync_reason
                        )
                    )
                    websocket.send_close(1012, "resync required")
                    return

                # replay와 live event 모두 하나의 ordered tuple 경로로 전송한다.
                for event_envelope in replay_batch.events:
                    websocket.send_text_object(event_envelope.to_dto())
                    current_sequence = event_envelope.sequence
                if replay_batch.closed:
                    websocket.send_close(1001, "server shutting down")
                    return

                readable_sockets, _, _ = select.select(
                    [websocket.socket],
                    [],
                    [],
                    0,
                )
                if readable_sockets and self._handle_client_websocket_frame(websocket):
                    return
        except socket.timeout:
            _send_close_safely(websocket, 1008, "authentication timeout")
        except _WebSocketProtocolError as error:
            _send_close_safely(websocket, error.close_code, error.reason)
        except (_WebSocketClosed, ConnectionError, OSError):
            return
        except Exception:
            # Upgrade 뒤의 내부 오류는 HTTP envelope를 쓰지 않고 generic close로만 끝낸다.
            _send_close_safely(websocket, 1011, "internal transport error")
        finally:
            # socket shutdown 오류는 이미 종료 중인 connection에서 무시한다.
            try:
                client_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client_socket.close()

    def _authenticate_websocket_frame(
        self,
        websocket: _WebSocketConnection,
        authentication_frame: _WebSocketFrame,
    ) -> int:
        """
        함수 이름: _authenticate_websocket_frame()
        기능: 첫 masked text frame의 exact AUTHENTICATE schema와 token을 검증한다.
        인자: websocket -> policy close를 보낼 connection
            authentication_frame -> 연결 후 처음 수신한 client frame
        반환값: 검증된 after_sequence
        작성 날짜: 2026/08/21
        """
        if authentication_frame.opcode == 0x8:
            raise _WebSocketClosed("client closed before authentication")
        if authentication_frame.opcode != 0x1:
            raise _WebSocketProtocolError(1008, "first frame must authenticate")
        try:
            authentication_text = authentication_frame.payload.decode("utf-8")
            authentication_value = json.loads(
                authentication_text,
                object_pairs_hook=_strict_json_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, ValueError) as error:
            raise _WebSocketProtocolError(1008, "malformed authentication frame") from error
        if not isinstance(authentication_value, dict):
            raise _WebSocketProtocolError(1008, "malformed authentication frame")

        required_keys = {
            "schema_version",
            "type",
            "token",
            "after_sequence",
        }
        if set(authentication_value) != required_keys:
            raise _WebSocketProtocolError(1008, "malformed authentication frame")
        authentication_schema_version = authentication_value.get("schema_version")
        if (
            isinstance(authentication_schema_version, bool)
            or not isinstance(authentication_schema_version, int)
            or authentication_schema_version != SCHEMA_VERSION
        ):
            raise _WebSocketProtocolError(1008, "unsupported schema version")
        if authentication_value.get("type") != "AUTHENTICATE":
            raise _WebSocketProtocolError(1008, "first frame must authenticate")

        candidate_token_value = authentication_value.get("token")
        candidate_token = (
            candidate_token_value
            if isinstance(candidate_token_value, str)
            else ""
        )
        token_matches = hmac.compare_digest(candidate_token, self._session_token)
        if not isinstance(candidate_token_value, str) or not token_matches:
            raise _WebSocketProtocolError(1008, "authentication required")

        after_sequence = authentication_value.get("after_sequence")
        if (
            isinstance(after_sequence, bool)
            or not isinstance(after_sequence, int)
            or after_sequence < 0
            or after_sequence > (1 << 64) - 1
        ):
            raise _WebSocketProtocolError(1008, "invalid after_sequence")

        # 인증 payload와 token 참조를 live loop에 보존하지 않는다.
        authentication_value.clear()
        candidate_token = ""
        return after_sequence

    def _handle_client_websocket_frame(
        self,
        websocket: _WebSocketConnection,
    ) -> bool:
        """
        함수 이름: _handle_client_websocket_frame()
        기능: 인증 뒤 ping, pong, close를 처리하고 두 번째 인증과 text를 거부한다.
        인자: websocket -> readable client connection
        반환값: connection을 종료해야 하면 True
        작성 날짜: 2026/08/21
        """
        client_frame = websocket.receive_frame()
        if client_frame.opcode == 0x8:
            websocket.send_close(1000)
            return True
        if client_frame.opcode == 0x9:
            websocket.send_pong(client_frame.payload)
            return False
        if client_frame.opcode == 0xA:
            return False

        # 인증 후에는 client application message가 없는 server-push stream으로 제한한다.
        raise _WebSocketProtocolError(1008, "client text frames are not allowed")


def create_account_update_observer(
    event_stream: BackendEventStream,
) -> Callable[[object], object]:
    """
    함수 이름: create_account_update_observer()
    기능: bootstrap의 application RLock 안에서 실제 Account 변경 event를 publish한다.
    인자: event_stream -> WebSocket server와 공유할 BackendEventStream
    반환값: create_application_runtime에 주입할 account observer
    작성 날짜: 2026/08/21
    """
    if not isinstance(event_stream, BackendEventStream):
        raise TypeError("event_stream must be a BackendEventStream")

    def publish_account_updated(account: object) -> object:
        """
        함수 이름: publish_account_updated()
        기능: 변경된 Account DTO와 Account.version을 ACCOUNT_UPDATED event로 공개한다.
        인자: account -> stream patch가 실제 적용된 authoritative Account
        반환값: 발급된 BackendEventEnvelope
        작성 날짜: 2026/08/21
        """
        account_version = getattr(account, "version")
        account_payload = map_account_snapshot(account)
        return event_stream.publish(
            "ACCOUNT_UPDATED",
            {"account": account_payload},
            aggregate_version=account_version,
        )

    return publish_account_updated


def create_trade_history_update_observer(
    event_stream: BackendEventStream,
) -> Callable[[object, object], object]:
    """
    함수 이름: create_trade_history_update_observer()
    기능: durable Trade publication을 주문과 전체 Performance event로 연속 발행한다.
    인자: event_stream -> WebSocket server와 공유할 BackendEventStream
    반환값: TradeHistoryController에 주입할 trade history observer
    작성 날짜: 2026/08/23
    """
    if not isinstance(event_stream, BackendEventStream):
        raise TypeError("event_stream must be a BackendEventStream")

    def publish_trade_history_updated(
        trade: object,
        performance: object,
    ) -> object:
        """
        함수 이름: publish_trade_history_updated()
        기능: 신규 체결과 필터 범위와 독립적인 전체 성과를 순서대로 공개한다.
        인자: trade -> persistence 성공 후 publish된 authoritative Trade
            performance -> 같은 history publication의 authoritative Performance
        반환값: ORDER_EXECUTED와 PERFORMANCE_UPDATED event tuple
        작성 날짜: 2026/08/23
        """
        # 두 DTO를 먼저 완성해 Performance mapping 실패가 ORDER event만 남기지 않게 한다.
        order_payload = {"trade": map_trade(trade)}
        performance_payload = {
            "performance": map_performance(performance),
        }  # 필터링하지 않은 D-12 전체 성과를 별도 event payload로 만든다.

        # 테이블 재조회 trigger와 성과 갱신을 하나의 atomic consecutive batch로 공개한다.
        return event_stream.publish_many(
            (
                ("ORDER_EXECUTED", order_payload),
                ("PERFORMANCE_UPDATED", performance_payload),
            )
        )

    return publish_trade_history_updated


def create_loopback_transport_application(
    runtime_factory: Callable[
        [Callable[[object], object], Callable[[object, object], object]],
        RuntimeSnapshotSource,
    ],
    session_token: str,
    *,
    allowed_origins: Sequence[str],
    start_runtime: Callable[[RuntimeSnapshotSource], object] | None = None,
    close_runtime: Callable[[RuntimeSnapshotSource], object] | None = None,
) -> LoopbackTransportApplication:
    """
    함수 이름: create_loopback_transport_application()
    기능: 동일 event stream observer를 runtime factory와 loopback server에 top-level 조립한다.
    인자: runtime_factory -> account와 trade history observer를 받아 runtime을 만드는 factory
        session_token -> inherited pipe 또는 memory로 받은 session token
        allowed_origins -> exact Tauri와 optional dev Origin 목록
        start_runtime -> runtime startup 함수 또는 bootstrap 기본 함수
        close_runtime -> server 조립 실패 때 bootstrap 자원을 정리할 함수
    반환값: runtime, event stream과 server가 조립된 application
    작성 날짜: 2026/08/21
    """
    if not callable(runtime_factory):
        raise TypeError("runtime_factory must be callable")

    # Token과 Origin 오류는 외부 client와 account subscription을 열기 전에 거부한다.
    validated_token = _validate_session_token(session_token)
    validated_origins = tuple(_validate_allowed_origins(allowed_origins))

    # Lifecycle callback 자체를 먼저 검증해 조립 중간의 미소유 stream을 만들지 않는다.
    if start_runtime is None:
        from binance_auto_trader.bootstrap import start_application

        selected_start_runtime: Callable[[RuntimeSnapshotSource], object] = start_application
    else:
        if not callable(start_runtime):
            raise TypeError("start_runtime must be callable")
        selected_start_runtime = start_runtime
    if close_runtime is None:
        from binance_auto_trader.bootstrap import close_application

        selected_close_runtime: Callable[[RuntimeSnapshotSource], object] = close_application
    else:
        if not callable(close_runtime):
            raise TypeError("close_runtime must be callable")
        selected_close_runtime = close_runtime

    # Event stream을 먼저 만들어 두 observer와 WebSocket이 같은 instance를 공유한다.
    event_stream = BackendEventStream()
    account_observer = create_account_update_observer(event_stream)
    trade_history_observer = create_trade_history_update_observer(event_stream)
    runtime: RuntimeSnapshotSource | None = None
    try:
        runtime = runtime_factory(
            account_observer,
            trade_history_observer,
        )  # Entity callback과 loopback replay가 하나의 sequence owner를 공유한다.
        selected_start_runtime(runtime)

        # Startup 함수의 반환만 믿지 않고 descriptor 공개에 필요한 ready 사후조건을 확인한다.
        _require_runtime_ready(runtime)
        server = LoopbackTransportServer(
            runtime,
            validated_token,
            allowed_origins=validated_origins,
            event_stream=event_stream,
        )
    except Exception as creation_error:
        # Callback source를 먼저 닫아 이미 닫힌 stream으로 account event가 들어오지 않게 한다.
        if runtime is not None:
            try:
                selected_close_runtime(runtime)
            except Exception:
                creation_error.add_note(
                    "Application cleanup failed after transport assembly failure."
                )
        event_stream.close()
        raise

    if runtime is None:
        raise RuntimeError("runtime factory did not return an application runtime")
    return LoopbackTransportApplication(
        runtime=runtime,
        event_stream=event_stream,
        server=server,
        close_runtime=selected_close_runtime,
    )


def read_session_token_from_fd(token_fd: int) -> str:
    """
    함수 이름: read_session_token_from_fd()
    기능: inherited anonymous pipe FD에서 session token을 한 번 읽고 FD를 닫는다.
    인자: token_fd -> token bytes 전용 inherited read descriptor
    반환값: 검증된 base64url no-padding session token
    작성 날짜: 2026/08/21
    """
    _validate_file_descriptor(token_fd, "token_fd")
    token_chunks: list[bytes] = []
    token_size = 0

    try:
        # Token frame은 writer EOF로 끝나며 작은 상한으로 accidental payload 노출을 막는다.
        while True:
            token_chunk = os.read(token_fd, 128)
            if not token_chunk:
                break
            token_size += len(token_chunk)
            if token_size > 128:
                raise ValueError("session token pipe payload is too large")
            token_chunks.append(token_chunk)
    finally:
        os.close(token_fd)

    try:
        token_text = b"".join(token_chunks).decode("ascii")
    except UnicodeDecodeError as error:
        raise ValueError("session token pipe payload is invalid") from error

    return _validate_session_token(token_text)


def run_transport_process(
    runtime_factory: Callable[
        [Callable[[object], object], Callable[[object, object], object]],
        RuntimeSnapshotSource,
    ],
    *,
    token_fd: int,
    ready_fd: int,
    stop_fd: int,
    allowed_origins: Sequence[str],
    start_runtime: Callable[[RuntimeSnapshotSource], object] | None = None,
    close_runtime: Callable[[RuntimeSnapshotSource], object] | None = None,
    require_closed_before_stop: bool = False,
) -> None:
    """
    함수 이름: run_transport_process()
    기능: inherited token과 runtime factory로 단일 소유 application process를 실행한다.
    인자: runtime_factory -> shared account와 trade history observer를 받아 runtime을 만드는 factory
        token_fd -> session token을 한 번 읽을 inherited pipe FD
        ready_fd -> secret 없는 descriptor를 기록할 inherited pipe FD
        stop_fd -> 한 byte 또는 EOF로 종료를 알릴 inherited pipe FD
        allowed_origins -> exact Origin allowlist
        start_runtime -> runtime startup 함수 또는 bootstrap 기본 함수
        close_runtime -> account subscription 등 bootstrap 자원을 닫는 함수
        require_closed_before_stop -> stop pipe만으로 runtime을 강제 종료하지 않을지 여부
    반환값: stop 신호 뒤 server가 종료되면 없음
    작성 날짜: 2026/08/21
    """
    _validate_file_descriptor(token_fd, "token_fd")
    _validate_file_descriptor(ready_fd, "ready_fd")
    _validate_file_descriptor(stop_fd, "stop_fd")
    if len({token_fd, ready_fd, stop_fd}) != 3:
        raise ValueError("token, ready and stop file descriptors must differ")
    if type(require_closed_before_stop) is not bool:
        raise TypeError("require_closed_before_stop must be a bool")

    transport_application: LoopbackTransportApplication | None = None
    session_token = ""
    token_fd_open = True
    ready_fd_open = True
    stop_fd_open = True
    try:
        session_token = read_session_token_from_fd(token_fd)
        token_fd_open = False

        # Factory, observer, runtime과 server를 한 조립 경로에 묶어 stream identity를 보장한다.
        transport_application = create_loopback_transport_application(
            runtime_factory,
            session_token,
            allowed_origins=allowed_origins,
            start_runtime=start_runtime,
            close_runtime=close_runtime,
        )
        descriptor = transport_application.start()
        ready_payload = json_bytes(descriptor.to_dto()) + b"\n"
        _write_all_to_fd(ready_fd, ready_payload)
        os.close(ready_fd)
        ready_fd_open = False

        # 기존 test runner는 pipe 신호를 유지하고 production sidecar는 CLOSED 전 강제 종료를 거부한다.
        if not require_closed_before_stop:
            os.read(stop_fd, 1)
        else:
            _wait_for_safe_process_ack(
                transport_application.runtime,
                transport_application.server,
                stop_fd,
            )
    finally:
        if token_fd_open:
            _close_file_descriptor_safely(token_fd)
        if ready_fd_open:
            _close_file_descriptor_safely(ready_fd)
        if stop_fd_open:
            _close_file_descriptor_safely(stop_fd)

        # Application 조립체가 runtime을 먼저 닫은 뒤 server와 shared stream을 정리한다.
        if transport_application is not None:
            transport_application.stop()
        session_token = ""  # process runner도 shutdown 시 token 참조를 제거한다.


def _wait_for_safe_process_ack(
    runtime: RuntimeSnapshotSource,
    server: LoopbackTransportServer,
    stop_fd: int,
) -> None:
    """
    함수 이름: _wait_for_safe_process_ack()
    기능: CLOSED 뒤 수신한 FD5 ack를 latch하고 accepted response handler 완료 뒤에만 반환한다.
    인자: runtime -> authoritative CLOSED publication을 제공하는 runtime
        server -> shutdown response flush와 active handler 완료를 제공하는 server
        stop_fd -> Tauri waiter가 acknowledgement를 쓰는 inherited read FD
    반환값: post-CLOSED ack와 response 완료가 모두 충족되면 없음
    작성 날짜: 2026/08/24
    """
    _validate_file_descriptor(stop_fd, "stop_fd")
    stop_pipe_reached_eof = False
    post_closed_ack_received = False

    while True:
        # Handler가 active-count를 내리기 전에 온 유효 ack도 잃지 않고 완료조건을 나중에 결합한다.
        if post_closed_ack_received and server.shutdown_response_flushed:
            return
        if stop_pipe_reached_eof:
            sleep(0.05)  # Parent crash EOF는 unsafe exit 권한 없이 process만 유지한다.
            continue

        readable_descriptors, _, _ = select.select(
            (stop_fd,),
            (),
            (),
            0.05,
        )
        if not readable_descriptors:
            continue

        stop_signal = os.read(stop_fd, 1)
        if not stop_signal:
            stop_pipe_reached_eof = True
            continue

        # READY 중 byte는 버리고 오직 읽은 시점에 CLOSED인 non-empty byte만 latch한다.
        if _runtime_is_closed(runtime):
            post_closed_ack_received = True


def _runtime_is_closed(runtime: RuntimeSnapshotSource) -> bool:
    """
    함수 이름: _runtime_is_closed()
    기능: production process가 unsafe pipe 신호 대신 application CLOSED publication을 기다리게 한다.
    인자: runtime -> application state와 publication lock을 제공하는 runtime
    반환값: authoritative lifecycle이 CLOSED이면 True
    작성 날짜: 2026/08/24
    """
    if not hasattr(runtime, "application_lock"):
        raise TypeError("runtime must expose application_lock")

    # State와 closed 판정은 bootstrap publication과 동일한 application lock에서 읽는다.
    with runtime.application_lock:
        runtime_state = getattr(runtime, "state", None)
        return getattr(runtime_state, "closed", False) is True


def _require_runtime_ready(runtime: RuntimeSnapshotSource) -> None:
    """
    함수 이름: _require_runtime_ready()
    기능: startup 뒤 runtime이 descriptor를 공개할 수 있는 ready 상태인지 원자적으로 확인한다.
    인자: runtime -> application publication lock을 제공하는 runtime
    반환값: ready이면 없음
    작성 날짜: 2026/08/21
    """
    if not hasattr(runtime, "application_lock"):
        raise TypeError("runtime must expose application_lock")

    # Ready와 lifecycle state는 bootstrap의 단일 publication lock 아래에서 확인한다.
    with runtime.application_lock:
        if runtime.ready is not True:
            raise RuntimeError(
                "transport process requires a ready application runtime"
            )


def _write_all_to_fd(file_descriptor: int, payload: bytes) -> None:
    """
    함수 이름: _write_all_to_fd()
    기능: inherited ready pipe에 short write 없이 descriptor payload 전체를 기록한다.
    인자: file_descriptor -> ready pipe write descriptor
        payload -> 기록할 secret 없는 descriptor bytes
    반환값: 없음
    작성 날짜: 2026/08/21
    """
    written_count = 0

    # os.write는 short write를 허용하므로 모든 bytes가 기록될 때까지 반복한다.
    while written_count < len(payload):
        current_count = os.write(file_descriptor, payload[written_count:])
        if current_count <= 0:
            raise OSError("ready descriptor pipe write made no progress")
        written_count += current_count


def _close_file_descriptor_safely(file_descriptor: int) -> None:
    """
    함수 이름: _close_file_descriptor_safely()
    기능: setup failure cleanup에서 이미 닫힌 inherited FD 오류를 무시한다.
    인자: file_descriptor -> 닫을 OS file descriptor
    반환값: 없음
    작성 날짜: 2026/08/21
    """
    try:
        os.close(file_descriptor)
    except OSError:
        pass


def _validate_session_token(session_token: object) -> str:
    """
    함수 이름: _validate_session_token()
    기능: CSPRNG 32 bytes의 base64url no-padding session token 형식을 검증한다.
    인자: session_token -> memory 또는 inherited pipe에서 받은 token
    반환값: 검증된 token 문자열
    작성 날짜: 2026/08/21
    """
    if (
        not isinstance(session_token, str)
        or _SESSION_TOKEN_PATTERN.fullmatch(session_token) is None
    ):
        raise ValueError("session token must be a 32-byte base64url value")

    try:
        decoded_token = base64.b64decode(
            session_token + "=",
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, base64.binascii.Error) as error:
        raise ValueError("session token must be a 32-byte base64url value") from error
    if len(decoded_token) != 32:
        raise ValueError("session token must contain exactly 32 random bytes")

    return session_token


def _validate_allowed_origins(allowed_origins: Sequence[str]) -> frozenset[str]:
    """
    함수 이름: _validate_allowed_origins()
    기능: wildcard와 path가 없는 exact Tauri 또는 loopback dev Origin 목록을 검증한다.
    인자: allowed_origins -> server launch에 명시한 Origin sequence
    반환값: 중복을 제거한 immutable exact Origin set
    작성 날짜: 2026/08/21
    """
    if isinstance(allowed_origins, (str, bytes)):
        raise TypeError("allowed_origins must be a sequence of origins")
    if not isinstance(allowed_origins, Sequence):
        raise TypeError("allowed_origins must be a sequence")

    normalized_origins = frozenset(allowed_origins)
    if not normalized_origins:
        raise ValueError("at least one exact Origin is required")
    for origin in normalized_origins:
        if not isinstance(origin, str) or _ORIGIN_PATTERN.fullmatch(origin) is None:
            raise ValueError("allowed origins must be exact Tauri or loopback origins")
        if "*" in origin:
            raise ValueError("wildcard origins are forbidden")

    return normalized_origins


def _validate_file_descriptor(file_descriptor: object, field_name: str) -> int:
    """
    함수 이름: _validate_file_descriptor()
    기능: inherited pipe descriptor가 bool이 아닌 0 이상 정수인지 검증한다.
    인자: file_descriptor -> 검증할 OS file descriptor
        field_name -> 오류 메시지에 표시할 필드 이름
    반환값: 검증된 descriptor 정수
    작성 날짜: 2026/08/21
    """
    if isinstance(file_descriptor, bool) or not isinstance(file_descriptor, int):
        raise TypeError(f"{field_name} must be an integer")
    if file_descriptor < 0:
        raise ValueError(f"{field_name} must not be negative")

    return file_descriptor


def _parse_request_target(request_target: object) -> object:
    """
    함수 이름: _parse_request_target()
    기능: request target 길이와 absolute-form 사용을 제한하고 URL parts를 반환한다.
    인자: request_target -> BaseHTTPRequestHandler가 파싱한 raw target
    반환값: urllib parsed target
    작성 날짜: 2026/08/21
    """
    if not isinstance(request_target, str):
        raise TransportContractError(
            "MALFORMED_REQUEST",
            "The request target is malformed.",
        )
    if len(request_target) > _MAX_REQUEST_TARGET_LENGTH:
        raise TransportContractError(
            "REQUEST_TOO_LARGE",
            "The request target exceeds its size limit.",
            status=413,
        )

    # RFC request-target에 존재할 수 없는 raw fragment delimiter는 query parsing 전에 거부한다.
    if "#" in request_target:
        raise TransportContractError(
            "MALFORMED_REQUEST",
            "Request target fragments are not allowed.",
        )

    parsed_target = urlsplit(request_target)
    if parsed_target.scheme or parsed_target.netloc:
        raise TransportContractError(
            "MALFORMED_REQUEST",
            "Absolute request targets are not allowed.",
        )
    if not parsed_target.path.startswith("/v1/"):
        raise TransportContractError(
            "ROUTE_NOT_FOUND",
            "The requested route does not exist.",
            status=404,
        )

    return parsed_target


def _safe_request_id(handler: _LoopbackRequestHandler) -> str:
    """
    함수 이름: _safe_request_id()
    기능: valid single request UUID를 보존하고 없거나 malformed면 server UUID를 만든다.
    인자: handler -> X-Request-Id header를 가진 request handler
    반환값: error envelope에도 안전하게 사용할 UUID
    작성 날짜: 2026/08/21
    """
    request_id_values = handler.headers.get_all("X-Request-Id") or []
    if len(request_id_values) == 1:
        try:
            return validate_uuid_text(request_id_values[0], "X-Request-Id")
        except TransportContractError:
            pass

    return str(uuid4())  # malformed header 자체를 response에 반사하지 않는다.


def _require_request_id(handler: _LoopbackRequestHandler) -> str:
    """
    함수 이름: _require_request_id()
    기능: HTTP request의 single canonical UUID X-Request-Id를 요구한다.
    인자: handler -> 검사할 request handler
    반환값: 검증된 request UUID
    작성 날짜: 2026/08/21
    """
    request_id_text = _require_single_header(handler, "X-Request-Id")
    return validate_uuid_text(request_id_text, "X-Request-Id")


def _require_single_header(
    handler: _LoopbackRequestHandler,
    header_name: str,
) -> str:
    """
    함수 이름: _require_single_header()
    기능: security-sensitive HTTP header가 정확히 하나 있고 비어 있지 않은지 검증한다.
    인자: handler -> 검사할 request handler
        header_name -> exact single 값을 요구할 header 이름
    반환값: 공백 제거한 header 값
    작성 날짜: 2026/08/21
    """
    header_values = handler.headers.get_all(header_name) or []
    if len(header_values) != 1:
        raise TransportContractError(
            "MALFORMED_REQUEST",
            f"{header_name} must be provided exactly once.",
        )

    header_value = header_values[0].strip()
    if not header_value or any(ord(character) < 32 for character in header_value):
        raise TransportContractError(
            "MALFORMED_REQUEST",
            f"{header_name} is malformed.",
        )

    return header_value


def _optional_single_header(
    handler: _LoopbackRequestHandler,
    header_name: str,
) -> str:
    """
    함수 이름: _optional_single_header()
    기능: optional header가 없으면 빈 문자열, 하나면 trimmed 값을 반환하고 중복은 거부한다.
    인자: handler -> 검사할 request handler
        header_name -> optional single header 이름
    반환값: header 값 또는 빈 문자열
    작성 날짜: 2026/08/21
    """
    header_values = handler.headers.get_all(header_name) or []
    if not header_values:
        return ""
    if len(header_values) != 1:
        raise TransportContractError(
            "MALFORMED_REQUEST",
            f"{header_name} must not be duplicated.",
        )

    return header_values[0].strip()


def _require_empty_request_body(handler: _LoopbackRequestHandler) -> None:
    """
    함수 이름: _require_empty_request_body()
    기능: body 없는 request의 transfer encoding과 non-zero Content-Length를 거부한다.
    인자: handler -> 검사할 GET 또는 OPTIONS request handler
    반환값: body가 없으면 없음
    작성 날짜: 2026/08/21
    """
    if _optional_single_header(handler, "Transfer-Encoding"):
        raise TransportContractError(
            "MALFORMED_REQUEST",
            "Chunked request bodies are not supported.",
        )
    content_length_text = _optional_single_header(handler, "Content-Length")
    if content_length_text and content_length_text != "0":
        raise TransportContractError(
            "MALFORMED_REQUEST",
            "This request must not contain a body.",
        )


def _read_json_request_body(
    handler: _LoopbackRequestHandler,
) -> tuple[JsonObject, bytes]:
    """
    함수 이름: _read_json_request_body()
    기능: exact Content-Length의 1 MiB 이하 application/json object를 읽는다.
    인자: handler -> body와 content header를 가진 command request
    반환값: parsed JSON object와 idempotency fingerprint용 raw bytes
    작성 날짜: 2026/08/21
    """
    if _optional_single_header(handler, "Transfer-Encoding"):
        raise TransportContractError(
            "MALFORMED_REQUEST",
            "Chunked request bodies are not supported.",
        )
    content_type = _require_single_header(handler, "Content-Type")
    if content_type.split(";", maxsplit=1)[0].strip().lower() != "application/json":
        raise TransportContractError(
            "UNSUPPORTED_CONTENT_TYPE",
            "Command bodies must use application/json.",
            status=415,
        )
    content_length_text = _require_single_header(handler, "Content-Length")
    if re.fullmatch(r"[0-9]+", content_length_text) is None:
        raise TransportContractError(
            "MALFORMED_REQUEST",
            "Content-Length must be a decimal integer.",
        )
    content_length = int(content_length_text)
    if content_length <= 0:
        raise TransportContractError(
            "MALFORMED_REQUEST",
            "Command requests require a JSON body.",
        )
    if content_length > MAX_HTTP_BODY_BYTES:
        raise TransportContractError(
            "REQUEST_TOO_LARGE",
            "The request body exceeds one MiB.",
            status=413,
        )

    raw_body = handler.rfile.read(content_length)
    if len(raw_body) != content_length:
        raise TransportContractError(
            "MALFORMED_REQUEST",
            "The request body ended before Content-Length.",
        )
    try:
        parsed_body = json.loads(
            raw_body.decode("utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise TransportContractError(
            "MALFORMED_REQUEST",
            "The request body must be a valid JSON object.",
        ) from error
    if not isinstance(parsed_body, dict):
        raise TransportContractError(
            "MALFORMED_REQUEST",
            "The request body must be a JSON object.",
        )

    return parsed_body, raw_body


def _strict_json_object(
    field_pairs: list[tuple[str, object]],
) -> dict[str, object]:
    """
    함수 이름: _strict_json_object()
    기능: JSON object의 duplicate field를 거부하면서 입력 순서대로 mapping을 만든다.
    인자: field_pairs -> decoder가 전달한 object key-value pair 목록
    반환값: 중복 key가 없는 JSON object
    작성 날짜: 2026/08/21
    """
    strict_object: dict[str, object] = {}

    # nested object를 포함해 같은 key가 두 번 나타나면 마지막 값으로 덮지 않는다.
    for field_name, field_value in field_pairs:
        if field_name in strict_object:
            raise ValueError("duplicate JSON object field")
        strict_object[field_name] = field_value

    return strict_object


def _reject_json_constant(constant_text: str) -> object:
    """
    함수 이름: _reject_json_constant()
    기능: JSON 표준에 없는 NaN과 Infinity numeric constant를 명시적으로 거부한다.
    인자: constant_text -> decoder가 발견한 비표준 numeric token
    반환값: 정상 반환 없이 ValueError 발생
    작성 날짜: 2026/08/21
    """
    raise ValueError("non-standard JSON numeric constant is forbidden")


def _validate_request_schema(request_body: JsonObject) -> None:
    """
    함수 이름: _validate_request_schema()
    기능: command DTO의 major schema_version이 현재 version과 정확히 일치하는지 검사한다.
    인자: request_body -> shape 검증을 마친 JSON object
    반환값: schema가 지원되면 없음
    작성 날짜: 2026/08/21
    """
    schema_version = request_body.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != SCHEMA_VERSION
    ):
        raise TransportContractError(
            "UNSUPPORTED_SCHEMA_VERSION",
            "The request schema version is not supported.",
            status=400,
        )


def _require_idempotency_key(handler: _LoopbackRequestHandler) -> str:
    """
    함수 이름: _require_idempotency_key()
    기능: command의 bounded printable single Idempotency-Key shape를 검증한다.
    인자: handler -> Idempotency-Key header를 가진 command handler
    반환값: 검증된 idempotency key
    작성 날짜: 2026/08/21
    """
    idempotency_key = _require_single_header(handler, "Idempotency-Key")
    if _IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key) is None:
        raise TransportContractError(
            "MALFORMED_REQUEST",
            "Idempotency-Key has an invalid shape.",
        )

    return idempotency_key


def _send_http_response(
    handler: _LoopbackRequestHandler,
    response: TransportResponse,
    origin: str | None,
) -> None:
    """
    함수 이름: _send_http_response()
    기능: 공통 envelope와 exact CORS/cache header를 HTTP response로 기록한다.
    인자: handler -> response를 기록할 request handler
        response -> status와 JSON envelope
        origin -> 검증된 exact Origin 또는 검증 전 오류이면 None
    반환값: 없음
    작성 날짜: 2026/08/21
    """
    encoded_payload = json_bytes(response.payload)
    handler.send_response(response.status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(encoded_payload)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Connection", "close")
    if origin is not None:
        handler.send_header("Access-Control-Allow-Origin", origin)
        handler.send_header("Vary", "Origin")
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(encoded_payload)
        handler.wfile.flush()  # accepted shutdown body가 process exit 전에 socket 경계로 내려가게 한다.
    handler.close_connection = True  # unread malformed body가 다음 HTTP request를 오염시키지 않는다.


def _send_close_safely(
    websocket: _WebSocketConnection,
    close_code: int,
    reason: str,
) -> None:
    """
    함수 이름: _send_close_safely()
    기능: 이미 끊긴 peer의 socket 오류가 원래 protocol 종료를 가리지 않게 close를 시도한다.
    인자: websocket -> 종료할 connection
        close_code -> RFC 6455 close status
        reason -> credential이 없는 close reason
    반환값: 없음
    작성 날짜: 2026/08/21
    """
    try:
        websocket.send_close(close_code, reason)
    except (ConnectionError, OSError):
        pass
