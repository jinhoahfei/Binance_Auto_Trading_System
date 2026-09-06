"""Actual RFC 6455 handshake, first-frame auth, replay와 resync control을 검증한다."""

import base64
import json
import os
import secrets
import select
import socket
import struct
from time import monotonic, sleep
from types import SimpleNamespace
import unittest

from binance_auto_trader.transport import BackendEventStream, LoopbackTransportServer

from tests.unit.transport.test_contracts import _create_ready_runtime


TEST_ORIGIN = "http://127.0.0.1:5173"  # 허용 origin은 실제 UI 개발 origin과 일치시킨다.


def _receive_exact(client_socket: socket.socket, byte_count: int) -> bytes:
    """
    함수 이름: _receive_exact()
    기능: raw WebSocket test socket에서 지정된 길이의 bytes를 정확히 읽는다.
    인자: client_socket -> connected loopback socket
        byte_count -> 읽을 byte 수
    반환값: 요청 길이의 bytes
    작성 날짜: 2026/08/21
    """
    received_chunks = []
    remaining_count = byte_count

    # TCP packet 경계와 frame 경계를 분리해 server frame을 안정적으로 읽는다.
    while remaining_count > 0:
        received_chunk = client_socket.recv(remaining_count)
        if not received_chunk:
            raise ConnectionError("server closed before the frame completed")
        received_chunks.append(received_chunk)
        remaining_count -= len(received_chunk)

    return b"".join(received_chunks)


def _create_masked_frame(opcode: int, payload: bytes) -> bytes:
    """
    함수 이름: _create_masked_frame()
    기능: deterministic test payload를 RFC 6455 masked client frame으로 인코딩한다.
    인자: opcode -> text, close 또는 ping opcode
        payload -> client frame payload
    반환값: network에 기록할 masked frame bytes
    작성 날짜: 2026/08/21
    """
    payload_length = len(payload)
    masking_key = b"\x11\x22\x33\x44"
    if payload_length < 126:
        frame_header = bytes((0x80 | opcode, 0x80 | payload_length))
    elif payload_length <= 65_535:
        frame_header = bytes((0x80 | opcode, 0x80 | 126)) + struct.pack(
            "!H",
            payload_length,
        )
    else:
        frame_header = bytes((0x80 | opcode, 0x80 | 127)) + struct.pack(
            "!Q",
            payload_length,
        )
    masked_payload = bytes(
        byte ^ masking_key[index % 4]
        for index, byte in enumerate(payload)
    )
    return frame_header + masking_key + masked_payload


def _send_json_text(client_socket: socket.socket, payload: object) -> None:
    """
    함수 이름: _send_json_text()
    기능: JSON 값을 compact masked WebSocket text frame으로 전송한다.
    인자: client_socket -> connected loopback socket
        payload -> JSON encoder로 직렬화할 값
    반환값: 없음
    작성 날짜: 2026/08/21
    """
    encoded_payload = json.dumps(
        payload,
        separators=(",", ":"),
    ).encode("utf-8")
    client_socket.sendall(_create_masked_frame(0x1, encoded_payload))


def _receive_server_frame(client_socket: socket.socket) -> tuple[int, bytes]:
    """
    함수 이름: _receive_server_frame()
    기능: unmasked server frame 하나의 opcode와 payload를 읽는다.
    인자: client_socket -> connected loopback socket
    반환값: opcode와 payload tuple
    작성 날짜: 2026/08/21
    """
    initial_header = _receive_exact(client_socket, 2)
    first_byte, second_byte = initial_header
    if not first_byte & 0x80:
        raise AssertionError("server frame must set FIN")
    if second_byte & 0x80:
        raise AssertionError("server frame must not be masked")

    opcode = first_byte & 0x0F
    payload_length = second_byte & 0x7F
    if payload_length == 126:
        payload_length = struct.unpack("!H", _receive_exact(client_socket, 2))[0]
    elif payload_length == 127:
        payload_length = struct.unpack("!Q", _receive_exact(client_socket, 8))[0]

    return opcode, _receive_exact(client_socket, payload_length)


def _open_websocket(
    server: LoopbackTransportServer,
    *,
    path: str = "/v1/events",
    subprotocol: str | None = None,
    extensions: str | None = None,
) -> tuple[socket.socket, str]:
    """
    함수 이름: _open_websocket()
    기능: raw HTTP upgrade를 보내고 server 101 response header를 반환한다.
    인자: server -> 실행 중인 loopback server
        path -> query token negative test를 포함할 request target
        subprotocol -> 금지된 subprotocol negative test 값 또는 None
        extensions -> browser가 제안할 optional extension header 또는 None
    반환값: upgraded socket과 HTTP response header text
    작성 날짜: 2026/08/21
    """
    descriptor = server.descriptor
    client_socket = socket.create_connection(
        ("127.0.0.1", descriptor.port),
        timeout=3.0,
    )
    client_socket.settimeout(4.0)
    websocket_key = base64.b64encode(os.urandom(16)).decode("ascii")
    request_lines = [
        f"GET {path} HTTP/1.1",
        f"Host: 127.0.0.1:{descriptor.port}",
        f"Origin: {TEST_ORIGIN}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Version: 13",
        f"Sec-WebSocket-Key: {websocket_key}",
    ]
    if subprotocol is not None:
        request_lines.append(f"Sec-WebSocket-Protocol: {subprotocol}")
    if extensions is not None:
        request_lines.append(f"Sec-WebSocket-Extensions: {extensions}")
    request_bytes = ("\r\n".join(request_lines) + "\r\n\r\n").encode("ascii")
    client_socket.sendall(request_bytes)

    response_bytes = b""
    while b"\r\n\r\n" not in response_bytes:
        response_bytes += client_socket.recv(4096)
    response_header, unexpected_remainder = response_bytes.split(b"\r\n\r\n", 1)
    decoded_response_header = response_header.decode("iso-8859-1")
    if "101 Switching Protocols" in decoded_response_header and unexpected_remainder:
        raise AssertionError("server sent an event before first-frame authentication")

    return client_socket, decoded_response_header


class LoopbackWebSocketServerTests(unittest.TestCase):
    """
    클래스 이름: LoopbackWebSocketServerTests
    기능: browser-compatible first-frame auth와 replay/resync network path를 검증한다.
    작성 날짜: 2026/08/21
    """

    def setUp(self) -> None:
        """
        함수 이름: setUp()
        기능: ready runtime, shared event stream과 random-token server를 시작한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.token = secrets.token_urlsafe(32)
        self.runtime = _create_ready_runtime()
        self.runtime.state = SimpleNamespace(status="READY")
        self.event_stream = BackendEventStream()
        self.server = LoopbackTransportServer(
            self.runtime,
            self.token,
            allowed_origins=(TEST_ORIGIN,),
            event_stream=self.event_stream,
        )
        self.server.start()

    def tearDown(self) -> None:
        """
        함수 이름: tearDown()
        기능: WebSocket handler와 shared event stream을 server stop으로 종료한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.server.stop()

    def test_first_frame_authentication_replays_event_and_handles_ping(self) -> None:
        """
        함수 이름: test_first_frame_authentication_replays_event_and_handles_ping()
        기능: URL/header token 없이 AUTHENTICATE 뒤 retained event와 pong을 받는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        published_event = self.event_stream.publish(
            "ACCOUNT_UPDATED",
            {"account": {"version": 4}},
            aggregate_version=4,
        )
        client_socket, response_header = _open_websocket(self.server)
        self.assertIn("101 Switching Protocols", response_header)
        self.assertNotIn(self.token, response_header)

        try:
            _send_json_text(
                client_socket,
                {
                    "schema_version": 3,
                    "type": "AUTHENTICATE",
                    "token": self.token,
                    "after_sequence": 0,
                },
            )
            event_opcode, event_payload = _receive_server_frame(client_socket)
            event_value = json.loads(event_payload.decode("utf-8"))
            self.assertEqual(event_opcode, 0x1)
            self.assertEqual(event_value["event_id"], published_event.event_id)
            self.assertEqual(event_value["sequence"], 1)
            self.assertEqual(event_value["type"], "ACCOUNT_UPDATED")

            ping_payload = b"health"
            client_socket.sendall(_create_masked_frame(0x9, ping_payload))
            pong_opcode, pong_payload = _receive_server_frame(client_socket)
            self.assertEqual(pong_opcode, 0xA)
            self.assertEqual(pong_payload, ping_payload)
        finally:
            client_socket.close()

    def test_browser_compression_offer_is_ignored_without_negotiation(self) -> None:
        """
        함수 이름: test_browser_compression_offer_is_ignored_without_negotiation()
        기능: Chromium의 permessage-deflate 제안을 거부하지 않고 선택 없이 연결하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        client_socket, response_header = _open_websocket(
            self.server,
            extensions="permessage-deflate; client_max_window_bits",
        )

        try:
            self.assertIn("101 Switching Protocols", response_header)
            self.assertNotIn(
                "sec-websocket-extensions:",
                response_header.lower(),
            )

            # Extension이 선택되지 않았으므로 인증 frame도 RSV bit 없는 평문 frame을 쓴다.
            _send_json_text(
                client_socket,
                {
                    "schema_version": 3,
                    "type": "AUTHENTICATE",
                    "token": self.token,
                    "after_sequence": 0,
                },
            )
            client_socket.sendall(_create_masked_frame(0x8, b""))
            close_opcode, _ = _receive_server_frame(client_socket)
            self.assertEqual(close_opcode, 0x8)
        finally:
            client_socket.close()

    def test_sequence_ahead_sends_resync_control_before_close(self) -> None:
        """
        함수 이름: test_sequence_ahead_sends_resync_control_before_close()
        기능: server sequence보다 앞선 cursor에 RESYNC_REQUIRED control과 close를 보내는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.event_stream.publish("ACCOUNT_UPDATED", {"version": 1})
        client_socket, _ = _open_websocket(self.server)

        try:
            _send_json_text(
                client_socket,
                {
                    "schema_version": 3,
                    "type": "AUTHENTICATE",
                    "token": self.token,
                    "after_sequence": 2,
                },
            )
            control_opcode, control_payload = _receive_server_frame(client_socket)
            control_value = json.loads(control_payload.decode("utf-8"))
            close_opcode, close_payload = _receive_server_frame(client_socket)

            self.assertEqual(control_opcode, 0x1)
            self.assertEqual(control_value["type"], "RESYNC_REQUIRED")
            self.assertEqual(control_value["reason"], "SEQUENCE_AHEAD")
            self.assertEqual(control_value["last_sequence"], 1)
            self.assertEqual(close_opcode, 0x8)
            self.assertEqual(struct.unpack("!H", close_payload[:2])[0], 1012)
        finally:
            client_socket.close()

    def test_malformed_or_unknown_auth_schema_closes_without_event(self) -> None:
        """
        함수 이름: test_malformed_or_unknown_auth_schema_closes_without_event()
        기능: duplicate field와 unknown schema 인증이 application event 전에 policy close인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.event_stream.publish("ACCOUNT_UPDATED", {"version": 1})
        invalid_authentication_texts = (
            (
                '{"schema_version":3,"schema_version":3,'
                f'"type":"AUTHENTICATE","token":"{self.token}",'
                '"after_sequence":0}'
            ),
            json.dumps(
                {
                    "schema_version": 1,
                    "type": "AUTHENTICATE",
                    "token": self.token,
                    "after_sequence": 0,
                }
            ),
            json.dumps(
                {
                    "schema_version": True,
                    "type": "AUTHENTICATE",
                    "token": self.token,
                    "after_sequence": 0,
                }
            ),
        )

        for authentication_text in invalid_authentication_texts:
            with self.subTest(authentication_text=authentication_text[:20]):
                client_socket, _ = _open_websocket(self.server)
                try:
                    client_socket.sendall(
                        _create_masked_frame(
                            0x1,
                            authentication_text.encode("utf-8"),
                        )
                    )
                    first_opcode, close_payload = _receive_server_frame(client_socket)
                    self.assertEqual(first_opcode, 0x8)
                    self.assertEqual(
                        struct.unpack("!H", close_payload[:2])[0],
                        1008,
                    )
                finally:
                    client_socket.close()

    def test_url_and_subprotocol_authentication_are_rejected(self) -> None:
        """
        함수 이름: test_url_and_subprotocol_authentication_are_rejected()
        기능: token query와 token subprotocol이 upgrade 전에 공통 HTTP 오류로 거부되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        query_socket, query_response = _open_websocket(
            self.server,
            path=f"/v1/events?token={self.token}",
        )
        subprotocol_socket, subprotocol_response = _open_websocket(
            self.server,
            subprotocol=self.token,
        )

        try:
            self.assertIn("400 Bad Request", query_response)
            self.assertIn("400 Bad Request", subprotocol_response)
        finally:
            query_socket.close()
            subprotocol_socket.close()

    def test_slowloris_authentication_uses_absolute_two_second_deadline(self) -> None:
        """
        함수 이름: test_slowloris_authentication_uses_absolute_two_second_deadline()
        기능: 각 byte를 2초보다 빨리 보내도 전체 first frame이 2초를 넘으면 닫는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        authentication_payload = json.dumps(
            {
                "schema_version": 3,
                "type": "AUTHENTICATE",
                "token": self.token,
                "after_sequence": 0,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        slow_frame = _create_masked_frame(0x1, authentication_payload)
        client_socket, _ = _open_websocket(self.server)
        start_time = monotonic()

        try:
            # 0.35초 간격은 기존 per-recv 2초 timeout을 계속 갱신하지만 absolute deadline은 못 넘는다.
            for frame_byte in slow_frame[:12]:
                try:
                    client_socket.sendall(bytes((frame_byte,)))
                except OSError:
                    break
                # Windows에서는 close 이후 추가 write가 unread close frame을 reset으로
                # 덮을 수 있다. Byte 간격 동안 응답을 관찰해 server의 실제 close를 먼저 읽는다.
                readable, _, _ = select.select((client_socket,), (), (), 0.35)
                if readable:
                    break

            close_opcode, close_payload = _receive_server_frame(client_socket)
            elapsed_seconds = monotonic() - start_time
            self.assertEqual(close_opcode, 0x8)
            self.assertEqual(struct.unpack("!H", close_payload[:2])[0], 1008)
            self.assertLess(elapsed_seconds, 3.5)
        finally:
            client_socket.close()


if __name__ == "__main__":
    unittest.main()
