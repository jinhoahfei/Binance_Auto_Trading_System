"""실제 child process로 framed READY, CLOSED ACK와 parent EOF 안전 계약을 검증한다."""

from __future__ import annotations

from http.client import HTTPConnection
import json
import os
from pathlib import Path
import secrets
import struct
import subprocess
import sys
import tempfile
from time import monotonic, sleep
import unittest
from uuid import uuid4

from binance_auto_trader.transport import SCHEMA_VERSION
from binance_auto_trader.transport.framing import read_json_frame, write_json_frame


TEST_ORIGIN = "http://localhost:5173"


class FramedSidecarProcessTests(unittest.TestCase):
    """
    클래스 이름: FramedSidecarProcessTests
    기능: 외부 client 대신 fixture만 사용해 실제 stdio/HTTP/ownership 결합을 검증한다.
    작성 날짜: 2026/09/06
    """

    def _request(
        self,
        descriptor: dict[str, object],
        token: str,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, object]]:
        """
        함수 이름: _request()
        기능: 실제 loopback HTTP로 authenticated query 또는 shutdown을 끝까지 읽는다.
        인자: descriptor -> child가 반환한 secret-free READY
            token -> test memory 안의 launch token
            method -> HTTP method
            path -> fixed local route
            body -> optional request DTO
        반환값: HTTP status와 완전히 읽은 JSON response
        작성 날짜: 2026/09/06
        """
        # Runtime에 정확히 허용된 loopback host와 Origin만 요청에 사용한다.
        connection = HTTPConnection("127.0.0.1", descriptor["port"], timeout=3.0)
        headers = {
            "Host": f"127.0.0.1:{descriptor['port']}",
            "Origin": TEST_ORIGIN,
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Request-Id": str(uuid4()),
            "Idempotency-Key": str(uuid4()),
        }
        try:
            connection.request(method, path, body=json.dumps(body) if body else None, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()  # Shutdown ACK는 response body 소비 뒤에만 호출자가 보낸다.

    def _run_round_trip(self, failure_mode: str | None) -> None:
        """
        함수 이름: _run_round_trip()
        기능: fresh subprocess에서 정상 종료 또는 parent-loss의 durable 결과를 검사한다.
        인자: failure_mode -> None, ready_eof, closed_eof 또는 malformed_control
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        backend_root = Path(__file__).resolve().parents[3]
        token = secrets.token_urlsafe(32)
        with tempfile.TemporaryDirectory() as temporary_directory:
            # Runtime fixture 외의 credential/order 환경은 child로 전달하지 않는다.
            child_environment = {
                "PYTHONPATH": os.pathsep.join((str(backend_root / "src"), str(backend_root))),
                "PYTHONUNBUFFERED": "1",
            }
            if os.name == "nt":
                child_environment["SystemRoot"] = os.environ["SystemRoot"]
            child = subprocess.Popen(
                [sys.executable, "-m", "tests.integration.phase13_stdio_sidecar_fixture"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=temporary_directory,
                env=child_environment,
            )
            try:
                write_json_frame(child.stdin, {
                    "type": "BOOTSTRAP",
                    "token": token,
                    "configuration": {
                        "schema_version": SCHEMA_VERSION,
                        "allowed_origin": TEST_ORIGIN,
                        "history_path": str(Path(temporary_directory) / "history.jsonl"),
                        "api_key": "fixture-key",
                        "api_secret": "fixture-secret",
                        "allow_testnet_orders": False,
                        "max_notional": None,
                    },
                })
                descriptor = read_json_frame(child.stdout, maximum_bytes=4096)
                self.assertIsNotNone(descriptor)
                self.assertEqual(set(descriptor), {
                    "port", "session_id", "runtime_pid", "process_start_id", "schema_version",
                })
                self.assertEqual(descriptor["runtime_pid"], child.pid)
                self.assertNotIn(token, json.dumps(descriptor))
                self.assertNotIn("fixture-secret", json.dumps(descriptor))

                # READY 시점의 control ACK는 나중 CLOSED를 승인하는 데 재사용되지 않는다.
                write_json_frame(child.stdin, {"type": "CLOSED_ACK"})
                sleep(0.15)
                self.assertIsNone(child.poll())
                if failure_mode not in {"ready_eof", "malformed_control"}:
                    status, response = self._request(
                        descriptor, token, "POST", "/v1/shutdown",
                        {"schema_version": SCHEMA_VERSION, "expected_version": 0},
                    )
                    self.assertEqual(status, 202)
                    self.assertIs(response["data"]["accepted"], True)
                    sleep(0.1)
                    self.assertIsNone(child.poll())  # Early ACK만으로 CLOSED child가 종료되면 실패다.

                ownership_path = Path(temporary_directory) / ".backend-runtime.lock"
                if failure_mode is None:
                    write_json_frame(child.stdin, {"type": "CLOSED_ACK"})
                    self.assertEqual(child.wait(timeout=5), 0)
                    artifact = json.loads(ownership_path.read_text(encoding="utf-8"))
                    self.assertEqual(artifact["owner_state"], "RELEASED")
                    self.assertEqual(child.stdout.read(), b"")  # Protocol stream에 log/secret frame이 없어야 한다.
                else:
                    if failure_mode == "malformed_control":
                        child.stdin.write(struct.pack(">I", 3) + b"bad")
                        child.stdin.flush()
                    else:
                        child.stdin.close()  # READY와 CLOSED 모두 EOF는 정상 ACK가 아니다.

                    # 실제 fsync 뒤의 fixture marker를 기다려 locked artifact의 동시 read를 피한다.
                    completion_path = Path(temporary_directory) / ".fixture-orphaned-complete"
                    deadline = monotonic() + 5.0
                    while monotonic() < deadline:
                        if completion_path.is_file():
                            break
                        sleep(0.02)
                    self.assertTrue(completion_path.is_file())
                    self.assertIsNone(child.poll())
                    status, _ = self._request(descriptor, token, "GET", "/v1/health")
                    self.assertEqual(status, 200)
                    if failure_mode != "closed_eof":
                        # Ownership fsync와 BUY gate는 독립 장벽이므로 authoritative publication도 기다린다.
                        deadline = monotonic() + 5.0
                        while monotonic() < deadline:
                            status, snapshot = self._request(descriptor, token, "GET", "/v1/snapshot")
                            self.assertEqual(status, 200)
                            if snapshot["data"]["trading"]["process_ownership_ambiguous"] is True:
                                break
                            sleep(0.02)
                        self.assertIs(snapshot["data"]["trading"]["process_ownership_ambiguous"], True)

                    # Test 소유 orphan child를 회수한 뒤 OS lock이 해제된 actual artifact를 확인한다.
                    child.terminate()
                    child.wait(timeout=5)
                    artifact = json.loads(ownership_path.read_text(encoding="utf-8"))
                    self.assertEqual(artifact["owner_state"], "ORPHANED")
            finally:
                # Fixture orphan는 production의 자동 relaunch 대상이 아니며 test만 child를 회수한다.
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=5)
                for stream in (child.stdin, child.stdout, child.stderr):
                    stream.close()

    def test_ready_early_ack_and_complete_shutdown_round_trip(self) -> None:
        """
        함수 이름: test_ready_early_ack_and_complete_shutdown_round_trip()
        기능: READY가 secret-free이고 early ACK를 버리며 202 뒤 ACK로만 정상 release한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        self._run_round_trip(None)  # 정상 종료도 실제 child exit와 RELEASED fsync까지 검증한다.

    def test_stdin_eof_while_ready_preserves_orphan_and_read_only_listener(self) -> None:
        """
        함수 이름: test_stdin_eof_while_ready_preserves_orphan_and_read_only_listener()
        기능: 부모 EOF가 READY runtime의 BUY를 잠그고 orphan listener를 유지한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        self._run_round_trip("ready_eof")  # 실제 pipe close를 liveness loss로 사용한다.

    def test_stdin_eof_after_closed_is_not_shutdown_ack(self) -> None:
        """
        함수 이름: test_stdin_eof_after_closed_is_not_shutdown_ack()
        기능: 완전한 202 뒤에도 stdin EOF를 정상 parent ACK로 승격하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        self._run_round_trip("closed_eof")  # FD5 기존 계약과 같은 orphan 결과를 고정한다.

    def test_malformed_control_fails_closed_as_parent_channel_loss(self) -> None:
        """
        함수 이름: test_malformed_control_fails_closed_as_parent_channel_loss()
        기능: malformed control이 process cleanup으로 owner를 해제하지 않고 orphan gate를 적용한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        self._run_round_trip("malformed_control")  # Invalid JSON도 안전 경계 우회가 되지 않는다.


if __name__ == "__main__":
    unittest.main()
