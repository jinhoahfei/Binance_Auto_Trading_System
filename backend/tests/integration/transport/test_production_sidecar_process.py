"""Production entrypoint의 fixed FD와 shutdown response-before-exit 계약을 검증한다."""

from __future__ import annotations

import fcntl
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import secrets
import signal
import sys
import tempfile
from time import monotonic, sleep
import unittest
from uuid import UUID, uuid4

from binance_auto_trader.transport import SCHEMA_VERSION
from tests.integration.transport.test_process_runner import _read_until_eof


TEST_ORIGIN = "tauri://localhost"


def _install_fixed_child_descriptors(
    source_descriptors: tuple[int, int, int, int],
) -> None:
    """
    함수 이름: _install_fixed_child_descriptors()
    기능: source collision 없이 token/ready/stop/config pipe를 child FD 3/4/5/6에 복제한다.
    인자: source_descriptors -> 네 pipe의 child-facing descriptor tuple
    반환값: 없음
    작성 날짜: 2026/08/24
    """
    fixed_descriptors = (3, 4, 5, 6)
    safe_sources = tuple(
        fcntl.fcntl(source_fd, fcntl.F_DUPFD, 20)
        for source_fd in source_descriptors
    )
    for source_fd in source_descriptors:
        os.close(source_fd)  # 원래 번호끼리의 dup2 overwrite 가능성을 먼저 제거한다.

    for safe_source, fixed_descriptor in zip(
        safe_sources,
        fixed_descriptors,
        strict=True,
    ):
        os.dup2(safe_source, fixed_descriptor, inheritable=True)
        os.close(safe_source)  # Entry point에는 합의된 네 fixed descriptor만 남긴다.


def _wait_for_child_exit(child_process_id: int, timeout_seconds: float) -> int:
    """
    함수 이름: _wait_for_child_exit()
    기능: bounded polling으로 child 정상 exit code를 회수하고 timeout이면 test를 실패시킨다.
    인자: child_process_id -> 기다릴 fork child PID
        timeout_seconds -> 최대 대기 monotonic 초
    반환값: os.waitstatus_to_exitcode로 변환한 process exit code
    작성 날짜: 2026/08/24
    """
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        waited_process_id, child_status = os.waitpid(
            child_process_id,
            os.WNOHANG,
        )
        if waited_process_id == child_process_id:
            return os.waitstatus_to_exitcode(child_status)
        sleep(0.02)  # HTTP response flush와 runner poll을 과도한 CPU 없이 기다린다.

    raise TimeoutError("production sidecar child did not exit")


class ProductionSidecarProcessTests(unittest.TestCase):
    """
    클래스 이름: ProductionSidecarProcessTests
    기능: actual fork child의 FD6 credential, abnormal stop과 response flush 후 exit를 검증한다.
    작성 날짜: 2026/08/24
    """

    @unittest.skipUnless(hasattr(os, "fork"), "requires inherited POSIX FDs")
    def test_fixed_fds_ignore_early_stop_and_exit_after_complete_202(self) -> None:
        """
        함수 이름: test_fixed_fds_ignore_early_stop_and_exit_after_complete_202()
        기능: FD 3/4/5/6 child가 pipe stop으로 죽지 않고 완전한 202 뒤 code 0으로 반복 종료하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # 반복 실행은 CLOSED publication과 HTTP handler flush 사이의 timing race를 재현할 확률을 높인다.
        for attempt_index in range(3):
            with self.subTest(attempt_index=attempt_index):
                self._run_one_sidecar_round_trip(attempt_index)

    @unittest.skipUnless(hasattr(os, "fork"), "requires inherited POSIX FDs")
    def test_parent_eof_after_closed_does_not_authorize_process_exit(self) -> None:
        """
        함수 이름: test_parent_eof_after_closed_does_not_authorize_process_exit()
        기능: CLOSED와 완전한 202 뒤에도 non-empty ack 없는 FD5 EOF는 process를 유지하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        self._run_one_sidecar_round_trip(
            99,
            send_post_closed_ack=False,
        )

    def _run_one_sidecar_round_trip(
        self,
        attempt_index: int,
        *,
        send_post_closed_ack: bool = True,
    ) -> None:
        """
        함수 이름: _run_one_sidecar_round_trip()
        기능: 한 production child handshake, early stop, shutdown response와 exit code를 끝까지 검증한다.
        인자: attempt_index -> 반복별 history 파일 identity를 만들 index
            send_post_closed_ack -> 202 뒤 non-empty FD5 byte를 보낼지 여부
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        token = secrets.token_urlsafe(32)
        token_read_fd, token_write_fd = os.pipe()
        ready_read_fd, ready_write_fd = os.pipe()
        stop_read_fd, stop_write_fd = os.pipe()
        config_read_fd, config_write_fd = os.pipe()

        with tempfile.TemporaryDirectory() as temporary_directory:
            history_path = (
                Path(temporary_directory)
                / f"history-{attempt_index}.jsonl"
            )
            child_process_id = os.fork()
            if child_process_id == 0:
                # Child는 parent-facing pipe 끝을 먼저 닫고 합의한 번호로 네 source를 복제한다.
                os.close(token_write_fd)
                os.close(ready_read_fd)
                os.close(stop_write_fd)
                os.close(config_write_fd)
                _install_fixed_child_descriptors(
                    (
                        token_read_fd,
                        ready_write_fd,
                        stop_read_fd,
                        config_read_fd,
                    )
                )

                # Fresh exec는 선행 socket 객체의 finalizer가 fixed FD 3~6을 닫는 fork hazard를 없앤다.
                try:
                    backend_root = Path(__file__).resolve().parents[3]
                    os.chdir(temporary_directory)
                    child_environment = {
                        "PYTHONPATH": os.pathsep.join(
                            (str(backend_root / "src"), str(backend_root))
                        ),
                        "PYTHONUNBUFFERED": "1",
                    }  # Test child도 API credential을 environment로 상속하지 않는다.
                    os.execve(
                        sys.executable,
                        (
                            sys.executable,
                            "-m",
                            "tests.integration.phase12_sidecar_fixture",
                        ),
                        child_environment,
                    )
                except BaseException as error:
                    os.write(
                        2,
                        (
                            "production sidecar child failure: "
                            f"{type(error).__name__}\n"
                        ).encode("ascii"),
                    )  # Credential 값 없이 child failure type만 test stderr에 남긴다.
                    os._exit(1)

            # Parent는 child-facing pipe 끝을 닫고 token과 strict FD6 JSON을 EOF frame으로 전달한다.
            os.close(token_read_fd)
            os.close(ready_write_fd)
            os.close(stop_read_fd)
            os.close(config_read_fd)
            configuration = {
                "schema_version": SCHEMA_VERSION,
                "allowed_origin": TEST_ORIGIN,
                "history_path": str(history_path),
                "api_key": "testnet-api-key",
                "api_secret": "testnet-api-secret",
                "allow_testnet_orders": False,
                "max_notional": None,
            }
            os.write(token_write_fd, token.encode("ascii"))
            os.close(token_write_fd)
            os.write(
                config_write_fd,
                json.dumps(configuration).encode("utf-8"),
            )
            os.close(config_write_fd)

            child_reaped = False
            try:
                ready_payload = _read_until_eof(ready_read_fd)
                os.close(ready_read_fd)
                descriptor = json.loads(ready_payload.decode("utf-8"))
                self.assertEqual(
                    frozenset(descriptor),
                    frozenset(
                        {
                            "port",
                            "session_id",
                            "runtime_pid",
                            "process_start_id",
                            "schema_version",
                        }
                    ),
                )
                self.assertEqual(descriptor["runtime_pid"], child_process_id)
                self.assertEqual(
                    str(UUID(descriptor["process_start_id"])),
                    descriptor["process_start_id"],
                )
                self.assertNotIn(token, ready_payload.decode("utf-8"))
                self.assertNotIn("testnet-api-secret", ready_payload.decode("utf-8"))
                ownership_path = (
                    Path(temporary_directory) / ".backend-runtime.lock"
                )
                ownership_artifact = json.loads(
                    ownership_path.read_text(encoding="utf-8")
                )
                self.assertEqual(
                    ownership_artifact,
                    {
                        "schema_version": SCHEMA_VERSION,
                        "runtime_pid": descriptor["runtime_pid"],
                        "process_start_id": descriptor["process_start_id"],
                        "owner_state": "ACTIVE",
                    },
                )  # Lock artifact는 session token과 API credential을 포함하지 않는다.

                # Parent stop byte는 안전 종료 receipt 없이 process를 강제 종료할 권한이 없다.
                try:
                    os.write(stop_write_fd, b"S")
                except BrokenPipeError:
                    self.fail(
                        "sidecar closed stop FD before shutdown acknowledgement"
                    )
                sleep(0.1)
                early_process_id, early_status = os.waitpid(
                    child_process_id,
                    os.WNOHANG,
                )
                if early_process_id == child_process_id:
                    child_reaped = True
                    self.fail(
                        "sidecar exited from stop FD before safe shutdown "
                        f"with {os.waitstatus_to_exitcode(early_status)}"
                    )

                connection = HTTPConnection(
                    "127.0.0.1",
                    descriptor["port"],
                    timeout=3.0,
                )
                request_body = json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "expected_version": 0,
                    }
                )
                connection.request(
                    "POST",
                    "/v1/shutdown",
                    body=request_body,
                    headers={
                        "Host": f"127.0.0.1:{descriptor['port']}",
                        "Origin": TEST_ORIGIN,
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "X-Request-Id": str(uuid4()),
                        "Idempotency-Key": f"shutdown-{attempt_index}",
                    },
                )
                response = connection.getresponse()
                response_payload = json.loads(response.read().decode("utf-8"))
                connection.close()

                # Body 전체를 읽은 뒤에 child가 code 0으로 exit하는 순서를 반복해서 검증한다.
                self.assertEqual(response.status, 202)
                self.assertEqual(
                    response_payload["data"],
                    {"accepted": True, "status": "accepted", "version": 0},
                )
                if not send_post_closed_ack:
                    os.close(stop_write_fd)
                    sleep(0.2)
                    unexpected_exit_id, _ = os.waitpid(
                        child_process_id,
                        os.WNOHANG,
                    )
                    if unexpected_exit_id == child_process_id:
                        child_reaped = True
                    self.assertEqual(
                        unexpected_exit_id,
                        0,
                    )  # Parent crash EOF는 CLOSED process도 스스로 끝내지 못한다.

                    # Ownership ambiguity와 durable flush 뒤에도 기존 listener는 조회 경로를 유지한다.
                    health_connection = HTTPConnection(
                        "127.0.0.1",
                        descriptor["port"],
                        timeout=3.0,
                    )
                    health_connection.request(
                        "GET",
                        "/v1/health",
                        headers={
                            "Host": f"127.0.0.1:{descriptor['port']}",
                            "Origin": TEST_ORIGIN,
                            "Authorization": f"Bearer {token}",
                            "X-Request-Id": str(uuid4()),
                        },
                    )
                    health_response = health_connection.getresponse()
                    health_response.read()
                    health_connection.close()
                    self.assertEqual(health_response.status, 200)
                    orphaned_artifact = json.loads(
                        ownership_path.read_text(encoding="utf-8")
                    )
                    self.assertEqual(
                        orphaned_artifact["owner_state"],
                        "ORPHANED",
                    )  # Parent EOF와 같은 iteration에 durable orphan marker를 게시한다.

                    # Parent-loss 상태의 actual runtime이 advisory lock을 계속 쥐어 새 owner 시작을 막는다.
                    ownership_descriptor = os.open(
                        ownership_path,
                        os.O_RDWR,
                    )
                    try:
                        with self.assertRaises(OSError):
                            fcntl.flock(
                                ownership_descriptor,
                                fcntl.LOCK_EX | fcntl.LOCK_NB,
                            )
                    finally:
                        os.close(ownership_descriptor)
                    self.assertTrue(history_path.is_file())
                    return

                os.write(stop_write_fd, b"A")
                os.close(
                    stop_write_fd
                )  # Renderer가 202를 parse한 뒤의 두 번째 byte만 exit acknowledgement다.
                self.assertEqual(
                    _wait_for_child_exit(child_process_id, 5.0),
                    0,
                )
                child_reaped = True
                self.assertTrue(history_path.is_file())
                released_artifact = json.loads(
                    ownership_path.read_text(encoding="utf-8")
                )
                self.assertEqual(
                    released_artifact["owner_state"],
                    "RELEASED",
                )  # Safe shutdown process만 다음 launch에 필요한 정상 release marker를 fsync한다.
            finally:
                try:
                    os.close(stop_write_fd)
                except OSError:
                    pass  # 정상 ack 경로에서 이미 닫힌 writer도 멱등 정리한다.
                if not child_reaped:
                    try:
                        os.kill(child_process_id, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        os.waitpid(child_process_id, 0)
                    except ChildProcessError:
                        pass  # 실패 경로에서도 fork child zombie를 남기지 않는다.


if __name__ == "__main__":
    unittest.main()
