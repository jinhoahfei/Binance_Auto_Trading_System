"""Inherited FD token/ready/stop을 사용하는 actual transport child process를 검증한다."""

from http.client import HTTPConnection
import json
import os
import secrets
from types import SimpleNamespace
import unittest
from uuid import uuid4

from binance_auto_trader.transport import run_transport_process

from tests.unit.transport.test_contracts import _create_ready_runtime


TEST_ORIGIN = "http://127.0.0.1:5173"  # child process CORS 허용 목록을 결정론적으로 고정한다.


def _create_process_runtime(*, ready: bool) -> SimpleNamespace:
    """
    함수 이름: _create_process_runtime()
    기능: process runner readiness와 health route에 필요한 runtime test double을 만든다.
    인자: ready -> ready descriptor 공개 가능 여부
    반환값: RuntimeSnapshotSource compatible object
    작성 날짜: 2026/08/21
    """
    runtime = _create_ready_runtime()
    runtime.ready = ready
    runtime.state = SimpleNamespace(status="READY" if ready else "CREATED")
    return runtime


def _read_until_eof(file_descriptor: int) -> bytes:
    """
    함수 이름: _read_until_eof()
    기능: inherited pipe reader에서 writer close까지 모든 bytes를 읽는다.
    인자: file_descriptor -> 읽을 pipe descriptor
    반환값: pipe 전체 payload
    작성 날짜: 2026/08/21
    """
    received_chunks = []

    # Ready와 cleanup marker pipe의 짧은 payload를 EOF까지 수집한다.
    while True:
        received_chunk = os.read(file_descriptor, 4096)
        if not received_chunk:
            break
        received_chunks.append(received_chunk)

    return b"".join(received_chunks)


def _keep_runtime_state(current_runtime: object) -> object:
    """
    함수 이름: _keep_runtime_state()
    기능: process transport test double의 이미 준비된 lifecycle 상태를 그대로 반환한다.
    인자: current_runtime -> ready 여부가 설정된 runtime test double
    반환값: 입력 runtime
    작성 날짜: 2026/08/21
    """
    return current_runtime


class TransportProcessRunnerTests(unittest.TestCase):
    """
    클래스 이름: TransportProcessRunnerTests
    기능: token 비노출 ready handshake와 runner cleanup을 process 경계에서 검증한다.
    작성 날짜: 2026/08/21
    """

    @unittest.skipUnless(hasattr(os, "fork"), "requires os.fork inherited FD semantics")
    def test_child_process_serves_health_and_closes_runtime_before_stream(
        self,
    ) -> None:
        """
        함수 이름: test_child_process_serves_health_and_closes_runtime_before_stream()
        기능: inherited token으로 actual child server를 띄우고 secret 없는 ready와 종료 순서를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        token = secrets.token_urlsafe(32)
        runtime = _create_process_runtime(ready=True)
        token_read_fd, token_write_fd = os.pipe()
        ready_read_fd, ready_write_fd = os.pipe()
        stop_read_fd, stop_write_fd = os.pipe()
        close_read_fd, close_write_fd = os.pipe()
        child_process_id = os.fork()

        if child_process_id == 0:
            # Child는 각 pipe에서 runner가 사용하는 방향만 남긴다.
            os.close(token_write_fd)
            os.close(ready_read_fd)
            os.close(stop_write_fd)
            os.close(close_read_fd)
            captured_observers = []

            def runtime_factory(
                account_observer: object,
                trade_history_observer: object,
                trading_session_observer: object,
            ) -> SimpleNamespace:
                """
                함수 이름: runtime_factory()
                기능: process가 만든 shared event observer를 받아 ready runtime을 반환한다.
                인자: account_observer -> transport-owned account callback
                    trade_history_observer -> transport-owned history callback
                    trading_session_observer -> transport-owned trading callback
                반환값: ready runtime test double
                작성 날짜: 2026/08/21
                """
                captured_observers.append(account_observer)
                if not callable(trade_history_observer):
                    raise TypeError("trade history observer must be callable")
                if not callable(trading_session_observer):
                    raise TypeError("trading session observer must be callable")
                return runtime

            def close_runtime(current_runtime: object) -> None:
                """
                함수 이름: close_runtime()
                기능: stream이 열려 있을 때 runtime cleanup이 먼저 호출됐음을 marker로 알린다.
                인자: current_runtime -> 종료할 runtime test double
                반환값: 없음
                작성 날짜: 2026/08/21
                """
                try:
                    captured_observers[0](
                        current_runtime.trading_controller.account
                    )
                except RuntimeError:
                    marker = b"wrong-order"
                else:
                    marker = b"runtime-before-stream"
                os.write(close_write_fd, marker)

            child_exit_code = 0
            try:
                run_transport_process(
                    runtime_factory,
                    token_fd=token_read_fd,
                    ready_fd=ready_write_fd,
                    stop_fd=stop_read_fd,
                    allowed_origins=(TEST_ORIGIN,),
                    start_runtime=_keep_runtime_state,
                    close_runtime=close_runtime,
                )
            except BaseException:
                child_exit_code = 1
            finally:
                os.close(close_write_fd)
                os._exit(child_exit_code)

        # Parent는 token을 raw pipe bytes로 한 번 전달하고 ready descriptor만 읽는다.
        os.close(token_read_fd)
        os.close(ready_write_fd)
        os.close(stop_read_fd)
        os.close(close_write_fd)
        os.write(token_write_fd, token.encode("ascii"))
        os.close(token_write_fd)
        ready_payload = _read_until_eof(ready_read_fd)
        os.close(ready_read_fd)
        ready_descriptor = json.loads(ready_payload.decode("utf-8"))

        try:
            self.assertNotIn(token, ready_payload.decode("utf-8"))
            self.assertEqual(ready_descriptor["schema_version"], 2)
            self.assertGreater(ready_descriptor["port"], 0)

            # Ready 뒤 actual child process의 authenticated health endpoint를 조회한다.
            connection = HTTPConnection(
                "127.0.0.1",
                ready_descriptor["port"],
                timeout=3.0,
            )
            connection.request(
                "GET",
                "/v1/health",
                headers={
                    "Host": f"127.0.0.1:{ready_descriptor['port']}",
                    "Origin": TEST_ORIGIN,
                    "Authorization": f"Bearer {token}",
                    "X-Request-Id": str(uuid4()),
                },
            )
            response = connection.getresponse()
            health_payload = json.loads(response.read().decode("utf-8"))
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertTrue(health_payload["data"]["ready"])
            self.assertEqual(
                health_payload["data"]["session_id"],
                ready_descriptor["session_id"],
            )
        finally:
            os.write(stop_write_fd, b"S")
            os.close(stop_write_fd)

        close_marker = _read_until_eof(close_read_fd)
        os.close(close_read_fd)
        _, child_status = os.waitpid(child_process_id, 0)
        self.assertEqual(close_marker, b"runtime-before-stream")
        self.assertEqual(os.waitstatus_to_exitcode(child_status), 0)

    def test_runner_rejects_not_ready_runtime_without_descriptor(self) -> None:
        """
        함수 이름: test_runner_rejects_not_ready_runtime_without_descriptor()
        기능: CREATED runtime이 ready pipe를 쓰지 않고 runtime과 stream을 정리하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        token = secrets.token_urlsafe(32)
        runtime = _create_process_runtime(ready=False)
        cleanup_calls = []
        captured_observers = []
        token_read_fd, token_write_fd = os.pipe()
        ready_read_fd, ready_write_fd = os.pipe()
        stop_read_fd, stop_write_fd = os.pipe()
        os.write(token_write_fd, token.encode("ascii"))
        os.close(token_write_fd)

        def runtime_factory(
            account_observer: object,
            trade_history_observer: object,
            trading_session_observer: object,
        ) -> SimpleNamespace:
            """
            함수 이름: runtime_factory()
            기능: not-ready failure가 shared stream 정리를 확인할 observer를 캡처한다.
            인자: account_observer -> transport-owned account callback
                trade_history_observer -> transport-owned history callback
                trading_session_observer -> transport-owned trading callback
            반환값: not-ready runtime test double
            작성 날짜: 2026/08/21
            """
            captured_observers.append(account_observer)
            if not callable(trade_history_observer):
                raise TypeError("trade history observer must be callable")
            if not callable(trading_session_observer):
                raise TypeError("trading session observer must be callable")
            return runtime

        def close_runtime(current_runtime: object) -> None:
            """
            함수 이름: close_runtime()
            기능: not-ready setup failure에서도 runtime cleanup 호출을 기록한다.
            인자: current_runtime -> 정리할 runtime test double
            반환값: 없음
            작성 날짜: 2026/08/21
            """
            cleanup_calls.append(current_runtime)

        with self.assertRaisesRegex(RuntimeError, "ready application"):
            run_transport_process(
                runtime_factory,
                token_fd=token_read_fd,
                ready_fd=ready_write_fd,
                stop_fd=stop_read_fd,
                allowed_origins=(TEST_ORIGIN,),
                start_runtime=_keep_runtime_state,
                close_runtime=close_runtime,
            )

        os.close(stop_write_fd)
        self.assertEqual(os.read(ready_read_fd, 1), b"")
        os.close(ready_read_fd)
        self.assertEqual(cleanup_calls, [runtime])
        with self.assertRaisesRegex(RuntimeError, "closed"):
            captured_observers[0](runtime.trading_controller.account)

    def test_broken_ready_pipe_still_closes_runtime_and_shared_stream(self) -> None:
        """
        함수 이름: test_broken_ready_pipe_still_closes_runtime_and_shared_stream()
        기능: descriptor write 실패 뒤에도 조립된 runtime과 동일 observer stream을 모두 정리한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        token = secrets.token_urlsafe(32)
        runtime = _create_process_runtime(ready=True)
        cleanup_calls = []
        captured_observers = []
        token_read_fd, token_write_fd = os.pipe()
        ready_read_fd, ready_write_fd = os.pipe()
        stop_read_fd, stop_write_fd = os.pipe()
        os.write(token_write_fd, token.encode("ascii"))
        os.close(token_write_fd)
        os.close(ready_read_fd)

        def runtime_factory(
            account_observer: object,
            trade_history_observer: object,
            trading_session_observer: object,
        ) -> SimpleNamespace:
            """
            함수 이름: runtime_factory()
            기능: ready pipe failure 뒤 closure를 검사할 shared observer를 캡처한다.
            인자: account_observer -> transport-owned account callback
                trade_history_observer -> transport-owned history callback
                trading_session_observer -> transport-owned trading callback
            반환값: ready runtime test double
            작성 날짜: 2026/08/21
            """
            captured_observers.append(account_observer)
            if not callable(trade_history_observer):
                raise TypeError("trade history observer must be callable")
            if not callable(trading_session_observer):
                raise TypeError("trading session observer must be callable")
            return runtime

        with self.assertRaises(BrokenPipeError):
            run_transport_process(
                runtime_factory,
                token_fd=token_read_fd,
                ready_fd=ready_write_fd,
                stop_fd=stop_read_fd,
                allowed_origins=(TEST_ORIGIN,),
                start_runtime=_keep_runtime_state,
                close_runtime=cleanup_calls.append,
            )

        os.close(stop_write_fd)
        self.assertEqual(cleanup_calls, [runtime])
        with self.assertRaisesRegex(RuntimeError, "closed"):
            captured_observers[0](runtime.trading_controller.account)


if __name__ == "__main__":
    unittest.main()
