"""인증된 생존 조회의 독립성·무부작용 및 선택적 socket 상관관계를 검증한다."""

import json
import secrets
from threading import Event, Thread
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from uuid import uuid4

from binance_auto_trader.application.runtime_diagnostics import RuntimeDiagnostics
from binance_auto_trader.transport.app import LoopbackTransportServer, _WebSocketProtocolError
from tests.integration.transport.test_http_server import _request_json
from tests.unit.transport.test_contracts import _create_ready_runtime


class LivenessTests(unittest.TestCase):
    """클래스 이름: LivenessTests
    기능: 실제 로컬 HTTP와 인증 parser에서 읽기 전용 생존 계약을 검증한다.
    작성 날짜: 2026/09/16
    """

    def test_get_is_authenticated_read_only_and_independent_of_trade_lock(self):
        """함수 이름: test_get_is_authenticated_read_only_and_independent_of_trade_lock()
        기능: 거래 잠금이 점유된 채로 2초 이내 응답하며 거래소를 조회하지 않는지 검사한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/16
        """
        runtime = _create_ready_runtime()
        runtime.diagnostics = RuntimeDiagnostics()
        runtime.api_gateway = Mock()
        runtime.diagnostics.liveness.observe("runtime_cycle", {"status": "running"})
        token = secrets.token_urlsafe(32)
        server = LoopbackTransportServer(runtime, token, allowed_origins=("http://127.0.0.1:5173",))
        server.start()
        locked, release = Event(), Event()

        def hold_lock():
            """함수 이름: hold_lock()
            기능: 별도 thread가 긴 거래 작업을 모사한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/09/16
            """
            with runtime.application_lock:
                locked.set()
                release.wait(5)

        worker = Thread(target=hold_lock)
        worker.start()
        self.assertTrue(locked.wait(2))
        try:
            began = monotonic()
            status, payload, _ = _request_json(server, token, "GET", "/v1/diagnostics/liveness", request_id=str(uuid4()))
            self.assertLess(monotonic() - began, 2)
            self.assertEqual(status, 200)
            self.assertEqual(payload["data"]["runtime_status"], "running")
            self.assertEqual(payload["data"]["process_start_id"], server.descriptor.process_start_id)
            self.assertIsNone(payload["data"]["api_checked_at_ms"])
            self.assertNotIn(token, json.dumps(payload))
            runtime.api_gateway.assert_not_called()
            self.assertEqual(runtime.api_gateway.mock_calls, [])
            status, _, _ = _request_json(server, "wrong-token", "GET", "/v1/diagnostics/liveness", request_id=str(uuid4()))
            self.assertEqual(status, 401)
            status, _, _ = _request_json(server, token, "POST", "/v1/diagnostics/liveness", request_id=str(uuid4()))
            self.assertNotEqual(status, 200)
        finally:
            release.set()
            worker.join(2)
            server.stop()

    def test_old_and_new_authentication_schema_and_rejected_free_text(self):
        """함수 이름: test_old_and_new_authentication_schema_and_rejected_free_text()
        기능: 기존 AUTH와 UUID 연결 식별자를 허용하고 원문/비밀 필드를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/16
        """
        server = object.__new__(LoopbackTransportServer)
        server._session_token = "test-secret"
        base = dict(schema_version=3, type="AUTHENTICATE", token="test-secret", after_sequence=123)
        for optional in ({}, {"client_connection_id": str(uuid4())}):
            message = {**base, **optional}
            frame = SimpleNamespace(opcode=1, payload=json.dumps(message).encode())
            self.assertEqual(server._authenticate_websocket_frame(Mock(), frame), (123, optional.get("client_connection_id")))
        for optional in ({"client_connection_id": "CANARY"}, {"client_connection_id": None}, {"account": "CANARY"}):
            with self.assertRaises(_WebSocketProtocolError):
                server._authenticate_websocket_frame(Mock(), SimpleNamespace(opcode=1, payload=json.dumps({**base, **optional}).encode()))

    def test_cache_remains_available_when_file_sink_fails_and_handles_worker_markers(self):
        """함수 이름: test_cache_remains_available_when_file_sink_fails_and_handles_worker_markers()
        기능: 저장 장애가 생존 갱신을 막지 않고 시장·엔진 관측을 분리하는지 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/16
        """
        diagnostics = RuntimeDiagnostics(Mock(side_effect=OSError("CANARY")))
        for snapshot in (0, None, {}, SimpleNamespace(status="running", recovery=None)):
            diagnostics.liveness.observe("runtime_cycle", snapshot)
        diagnostics.record("market_input_observed")
        values = diagnostics.liveness.snapshot()
        self.assertIsNotNone(values["last_runtime_cycle_monotonic_ms"])
        self.assertIsNotNone(values["last_market_input_at_ms"])
        self.assertIsNone(values["last_strategy_evaluation_at_ms"])
        self.assertEqual(diagnostics.failure_count, 1)
        self.assertNotIn("CANARY", json.dumps(values))
