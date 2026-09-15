"""시작 실패의 native pipe 전달과 cleanup·READY 경계를 검증한다."""

from io import BytesIO
import json
import os
import secrets
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from binance_auto_trader.application.trading_controller import ResidualBalanceMismatchError
from binance_auto_trader.bootstrap.application import ApplicationStartupError, StartupFailure, StartupFailureCode, StartupStage
from binance_auto_trader.transport.app import run_transport_process
from binance_auto_trader.transport.framing import read_json_frame
from binance_auto_trader.transport.startup_failure import startup_failure_payload
from binance_auto_trader.transport.stdio_process import run_framed_transport_process


def failure():
    """
    함수 이름: failure()
    기능: 실제 lifecycle처럼 startup 실패와 잔여 오류 원인을 연결한다.
    인자: 없음
    반환값: 원문을 노출하면 안 되는 예외
    작성 날짜: 2026/09/15
    """
    error = ApplicationStartupError(StartupFailure(StartupStage.RECONCILIATION, StartupFailureCode.ORDER_RECONCILIATION_FAILED, 'secret fixture text', False))
    error.__cause__ = ResidualBalanceMismatchError('secret signature fixture')
    return error


class StartupFailureChannelTests(unittest.TestCase):
    """
    클래스 이름: StartupFailureChannelTests
    기능: 실제 pipe·framed channel과 cleanup 및 비밀 필드 차단을 검사한다.
    작성 날짜: 2026/09/15
    """
    @unittest.skipUnless(os.name == 'posix', 'requires POSIX pipes')
    def test_posix_failure_reaches_parent_after_cleanup(self):
        """
        함수 이름: test_posix_failure_reaches_parent_after_cleanup()
        기능: POSIX 오류 채널에서 고정 코드와 runtime 자원 정리를 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/15
        """
        token_read, token_write = os.pipe()
        ready_read, ready_write = os.pipe()
        stop_read, stop_write = os.pipe()
        os.write(token_write, secrets.token_urlsafe(32).encode())
        os.close(token_write)
        runtime = SimpleNamespace()
        close = Mock()
        try:
            with self.assertRaises(ApplicationStartupError):
                run_transport_process(lambda *args: runtime, token_fd=token_read, ready_fd=ready_write, stop_fd=stop_read, allowed_origins=('tauri://localhost',), start_runtime=Mock(side_effect=failure()), close_runtime=close)
            close.assert_called_once_with(runtime)
            payload = os.read(ready_read, 4096)
            self.assertEqual(json.loads(payload), {'type': 'STARTUP_FAILED', 'schema_version': 3, 'code': 'RESIDUAL_BALANCE_MISMATCH'})
            self.assertNotIn(b'secret', payload)
            self.assertEqual(os.read(ready_read, 1), b'')
        finally:
            os.close(ready_read)
            os.close(stop_write)

    def test_framed_failure_preserves_code_and_releases_owner(self):
        """
        함수 이름: test_framed_failure_preserves_code_and_releases_owner()
        기능: framed 시작 실패에서도 원인 코드와 소유권 해제를 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/15
        """
        output = BytesIO()
        runtime = SimpleNamespace()
        close, owner = Mock(), Mock()
        with patch('binance_auto_trader.transport.stdio_process._RuntimeOwnershipLock.acquire', return_value=owner):
            with self.assertRaises(ApplicationStartupError):
                run_framed_transport_process(lambda *args: runtime, session_token=secrets.token_urlsafe(32), input_stream=BytesIO(), output_stream=output, allowed_origins=('tauri://localhost',), start_runtime=Mock(side_effect=failure()), close_runtime=close)
        close.assert_called_once_with(runtime)
        owner.release.assert_called_once()
        output.seek(0)
        self.assertEqual(read_json_frame(output)['code'], 'RESIDUAL_BALANCE_MISMATCH')
        self.assertIsNone(read_json_frame(output))
        self.assertNotIn(b'secret', output.getvalue())

    def test_ready_is_never_followed_by_startup_failure(self):
        """
        함수 이름: test_ready_is_never_followed_by_startup_failure()
        기능: READY 이후 오류가 두 번째 시작 응답으로 게시되지 않는지 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/15
        """
        output, application = BytesIO(), Mock()
        application.start.return_value.to_dto.return_value = {'port': 1234}
        with patch('binance_auto_trader.transport.stdio_process._RuntimeOwnershipLock.acquire'), patch('binance_auto_trader.transport.stdio_process.create_loopback_transport_application', return_value=application), patch('binance_auto_trader.transport.stdio_process._wait_for_safe_framed_ack', side_effect=RuntimeError('after-ready')):
            with self.assertRaises(RuntimeError):
                run_framed_transport_process(Mock(), session_token=secrets.token_urlsafe(32), input_stream=BytesIO(), output_stream=output, allowed_origins=('tauri://localhost',))
        application.stop.assert_called_once()
        output.seek(0)
        self.assertEqual(read_json_frame(output), {'port': 1234})
        self.assertIsNone(read_json_frame(output))

    def test_unknown_exception_and_cyclic_causes_do_not_publish_text(self):
        """
        함수 이름: test_unknown_exception_and_cyclic_causes_do_not_publish_text()
        기능: 임의 오류 원문과 순환 원인이 native 오류 채널로 새지 않게 한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/15
        """
        error = ValueError('token secret path')
        error.code = 'injected arbitrary code'
        error.__cause__ = error
        self.assertEqual(startup_failure_payload(error), {'type': 'STARTUP_FAILED', 'schema_version': 3, 'code': 'BACKEND_SIDECAR_STARTUP_FAILED'})
