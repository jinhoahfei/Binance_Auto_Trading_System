"""UI 연결의 최초 오류·단계와 부모 소유권 상실 진단을 검증한다."""

import json
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from binance_auto_trader.application.runtime_diagnostics import RuntimeDiagnostics
from binance_auto_trader.transport.app import LoopbackTransportServer
from tests.integration.test_trading_session_flow import _create_ready_controller


class ConnectionDiagnosticsTests(unittest.TestCase):
    """
    클래스 이름: ConnectionDiagnosticsTests
    기능: 인증·송신 timeout을 구분하고 프로세스 소유권 상실의 최초 증거를 보존한다.
    작성 날짜: 2026/09/13
    """

    def test_socket_timeout_logs_actual_stage_and_safe_exception(self):
        """
        함수 이름: test_socket_timeout_logs_actual_stage_and_safe_exception()
        기능: 송신 timeout을 인증 timeout으로 오분류하지 않고 연결 식별자와 예외 타입을 남긴다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/13
        """
        for stage, close_code in (("authentication", 1008), ("send", 1011)):
            with self.subTest(stage=stage):
                records = []
                server = object.__new__(LoopbackTransportServer)
                server._runtime = SimpleNamespace(diagnostics=RuntimeDiagnostics(records.append))
                event = Mock(sequence=1)
                server._event_stream = Mock(session_id="test-session")
                server._event_stream.wait_for_events.return_value = SimpleNamespace(
                    requires_resync=False, events=(event,), closed=False)
                server._authenticate_websocket_frame = Mock(return_value=0)
                websocket = Mock()
                if stage == "authentication":
                    websocket.receive_frame.side_effect = socket.timeout("SECRET_CANARY")
                else:
                    websocket.send_text_object.side_effect = socket.timeout("SECRET_CANARY")
                with patch("binance_auto_trader.transport.app._WebSocketConnection", return_value=websocket):
                    server._serve_websocket(Mock())
                failure = next(record for record in records if record["event"] == "ui_stream_timeout")
                self.assertEqual(failure["details"]["stage"], stage)
                self.assertEqual(failure["details"]["close_code"], close_code)
                closed = next(record for record in records if record["event"] == "ui_stream_closed")
                self.assertEqual(failure["details"]["connection_id"], closed["details"]["connection_id"])
                self.assertNotIn("SECRET_CANARY", json.dumps(records, default=str))
                websocket.send_close.assert_called_once()

    def test_parent_loss_marks_recovery_blocked_and_logs_once(self):
        """
        함수 이름: test_parent_loss_marks_recovery_blocked_and_logs_once()
        기능: parent EOF로 복구가 차단된 이유를 최초 한 번 기록하고 idle로 표시하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/13
        """
        controller, _, _ = _create_ready_controller()
        records = []
        controller._diagnostics = RuntimeDiagnostics(records.append)
        controller.mark_process_ownership_ambiguous("parent_stop_pipe_eof")
        controller.mark_process_ownership_ambiguous("parent_identity_lost")
        failures = [record for record in records if record["event"] == "process_ownership_lost"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["details"]["reason"], "parent_stop_pipe_eof")
        self.assertEqual(controller.snapshot_session().recovery["phase"], "blocked")
        self.assertEqual(controller.snapshot_session().recovery["block_reason"], "PROCESS_OWNERSHIP_AMBIGUOUS")
