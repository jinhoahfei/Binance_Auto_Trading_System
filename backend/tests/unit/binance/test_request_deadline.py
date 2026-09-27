"""REST 작업 예산·제출 권한·응답 소비의 시간 경계를 검증한다."""

from io import BytesIO
from threading import RLock
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from binance_auto_trader.adapters.binance.request_deadline import (
    RequestDeadlineExceeded,
    SubmissionPreflightRejected,
    check_submission_before_send,
    operation_deadline,
    remaining_request_timeout,
    request_deadline_scope,
    submission_check_scope,
)
from binance_auto_trader.adapters.binance.spot_rest_client import UrllibHTTPTransport


class DeadlineResponse:
    """
    클래스 이름: DeadlineResponse
    기능: 조각별 시계 진행과 socket 제한을 기록하는 메모리 응답을 제공한다.
    작성 날짜: 2026/09/27
    """

    def __init__(self, clock: Mock, chunks: list[bytes], elapsed_per_read: int) -> None:
        """
        함수 이름: __init__()
        기능: 네트워크 없이 응답과 읽기에 소요되는 시간을 설정한다.
        인자: clock -> 단조 시계, chunks -> 응답 조각, elapsed_per_read -> 읽기당 초
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        self.clock = clock
        self.chunks = list(chunks)
        self.elapsed_per_read = elapsed_per_read
        self.socket = Mock()
        self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=self.socket))
        self.headers = {"Content-Type": "application/json"}
        self.status = 200
        self.read_count = 0
        self.closed = False

    def read1(self, amount: int) -> bytes:
        """
        함수 이름: read1()
        기능: 한 조각만 반환하고 시계를 이동해 느린 연속 수신을 재현한다.
        인자: amount -> 최대 읽기 크기
        반환값: 다음 응답 조각
        작성 날짜: 2026/09/27
        """
        if amount != 64 * 1024:
            raise AssertionError("unexpected read size")
        self.read_count += 1
        self.clock.return_value += self.elapsed_per_read
        return self.chunks.pop(0)

    def read(self, amount: int) -> bytes:
        """
        함수 이름: read()
        기능: read1이 있는 응답에 누적 read를 사용하면 실패시킨다.
        인자: amount -> 읽기 크기
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        raise AssertionError("read1 must be preferred to a multi-receive read")

    def close(self) -> None:
        """
        함수 이름: close()
        기능: 응답 자원의 회수 여부를 기록한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        self.closed = True

    def __enter__(self):
        """
        함수 이름: __enter__()
        기능: urllib context manager 계약을 재현한다.
        인자: 없음
        반환값: 현재 응답
        작성 날짜: 2026/09/27
        """
        return self

    def __exit__(self, exception_type, exception_value, traceback) -> None:
        """
        함수 이름: __exit__()
        기능: 읽기 성공 여부와 관계없이 응답을 닫는다.
        인자: exception_type -> 종류, exception_value -> 예외, traceback -> 경로
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        self.close()


class RequestDeadlineTests(unittest.TestCase):
    """
    클래스 이름: RequestDeadlineTests
    기능: 전체 예산·미전송 경계·응답 자원 회수를 실제 연결 없이 검증한다.
    작성 날짜: 2026/09/27
    """

    def setUp(self) -> None:
        """
        함수 이름: setUp()
        기능: 독립 mock 시계와 네트워크를 사용하지 않는 opener를 설치한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        clock_patch = patch(
            "binance_auto_trader.adapters.binance.request_deadline.monotonic", return_value=100,
        )
        self.clock = clock_patch.start()
        self.addCleanup(clock_patch.stop)
        self.transport = UrllibHTTPTransport()
        self.transport._opener = Mock()

    def _request(self, *, before_send=None):
        """
        함수 이름: _request()
        기능: 고정된 요청을 fake opener에 전달한다.
        인자: before_send -> 선택 전송 직전 검사
        반환값: transport 응답
        작성 날짜: 2026/09/27
        """
        return self.transport.request(
            method="POST", url="https://deadline.invalid/api/v3/order", headers={},
            body=b"fixture-only", timeout_seconds=6, before_send=before_send,
        )

    def test_nested_longer_budget_cannot_extend_parent_deadline(self) -> None:
        """
        함수 이름: test_nested_longer_budget_cannot_extend_parent_deadline()
        기능: 후속 요청이 원 작업의 잔여 시간만 사용하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        with request_deadline_scope(30):
            self.clock.return_value = 120
            with request_deadline_scope(30):
                self.assertEqual(remaining_request_timeout(30), 10)
                self.clock.return_value = 130
                with self.assertRaises(RequestDeadlineExceeded):
                    remaining_request_timeout(30)

    def test_inner_expiry_restores_parent_and_next_order_budget(self) -> None:
        """
        함수 이름: test_inner_expiry_restores_parent_and_next_order_budget()
        기능: 하위 예외 뒤 상위 예산을 복원하고 다음 주문에 만료를 누출하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        with request_deadline_scope(30):
            with self.assertRaises(RequestDeadlineExceeded):
                with request_deadline_scope(5):
                    self.clock.return_value = 105
                    remaining_request_timeout(30)
            self.assertEqual(remaining_request_timeout(30), 25)
            self.assertEqual(remaining_request_timeout(4), 4)
        self.clock.return_value = 1000
        self.assertEqual(remaining_request_timeout(7), 7)

    def test_invalid_operation_budgets_are_rejected(self) -> None:
        """
        함수 이름: test_invalid_operation_budgets_are_rejected()
        기능: 무한·비수·음수·bool 예산으로 제한이 무력화되지 않게 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        for timeout_seconds in (0, -1, True, False, float("inf"), float("nan")):
            with self.subTest(timeout_seconds=timeout_seconds):
                with self.assertRaises(ValueError):
                    with request_deadline_scope(timeout_seconds):
                        self.fail("invalid timeout was admitted")
        self.assertEqual(remaining_request_timeout(9), 9)

    def test_operation_decorator_shares_budget_across_separate_scopes(self) -> None:
        """
        함수 이름: test_operation_decorator_shares_budget_across_separate_scopes()
        기능: 잠금·요청 구간이 나뉘어도 복구 전체 예산이 초기화되지 않게 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        @operation_deadline(30)
        def recover_order() -> None:
            """
            함수 이름: recover_order()
            기능: 서로 다른 요청 scope가 있는 복구를 재현한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/09/27
            """
            with request_deadline_scope(30):
                self.clock.return_value = 125
            with request_deadline_scope(30):
                self.assertEqual(remaining_request_timeout(30), 5)
                self.clock.return_value = 130
                remaining_request_timeout(30)

        with self.assertRaises(RequestDeadlineExceeded):
            recover_order()
        self.assertEqual(recover_order.__name__, "recover_order")
        self.assertEqual(remaining_request_timeout(30), 30)

    def test_expired_budget_does_not_record_transport_attempt(self) -> None:
        """
        함수 이름: test_expired_budget_does_not_record_transport_attempt()
        기능: POST 직전 만료는 전송 시도 증거를 남기지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        begin_attempt = Mock()
        with request_deadline_scope(1):
            self.clock.return_value = 101
            with self.assertRaises(RequestDeadlineExceeded):
                check_submission_before_send(begin_attempt)
        begin_attempt.assert_not_called()

    def test_revoked_permission_does_not_record_transport_attempt(self) -> None:
        """
        함수 이름: test_revoked_permission_does_not_record_transport_attempt()
        기능: 명시적 True 이외의 권한 결과는 전송을 시작하지 않는지 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        begin_attempt = Mock()
        for permission in (False, None, 1):
            with self.subTest(permission=permission):
                with submission_check_scope(Mock(return_value=permission)):
                    with self.assertRaises(SubmissionPreflightRejected):
                        check_submission_before_send(begin_attempt)
        begin_attempt.assert_not_called()

    def test_nested_read_scope_restores_outer_submission_check(self) -> None:
        """
        함수 이름: test_nested_read_scope_restores_outer_submission_check()
        기능: 조회의 None 검사 경계 종료 후 외부 거절 검사가 다시 적용되는지 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        begin_attempt = Mock()
        with submission_check_scope(Mock(return_value=False)):
            with submission_check_scope(None):
                check_submission_before_send(begin_attempt)
            with self.assertRaises(SubmissionPreflightRejected):
                check_submission_before_send(begin_attempt)
        check_submission_before_send(begin_attempt)
        self.assertEqual(begin_attempt.call_count, 2)

    def test_permission_and_attempt_share_guard_and_release_on_failure(self) -> None:
        """
        함수 이름: test_permission_and_attempt_share_guard_and_release_on_failure()
        기능: 권한 검사부터 시도 기록까지 같은 잠금을 유지하고 실패 뒤 회수하는지 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        guard = RLock()

        def check_permission() -> bool:
            """
            함수 이름: check_permission()
            기능: 권한 검사 때 공유 잠금이 유지되는지 검증한다.
            인자: 없음
            반환값: 허용 여부
            작성 날짜: 2026/09/27
            """
            self.assertTrue(guard._is_owned())
            return True

        def begin_attempt() -> None:
            """
            함수 이름: begin_attempt()
            기능: 같은 잠금 아래 전송 기록 실패를 재현한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/09/27
            """
            self.assertTrue(guard._is_owned())
            raise RuntimeError("attempt fixture failed")

        with submission_check_scope(check_permission, guard=guard):
            with self.assertRaises(RuntimeError):
                check_submission_before_send(begin_attempt)
        self.assertFalse(guard._is_owned())
        check_submission_before_send(Mock())

    def test_permission_exception_restores_prior_context(self) -> None:
        """
        함수 이름: test_permission_exception_restores_prior_context()
        기능: 검사 예외 뒤 이전 검사나 시도 증거가 다음 주문으로 누출되지 않게 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        begin_attempt = Mock()
        with self.assertRaises(RuntimeError):
            with submission_check_scope(Mock(side_effect=RuntimeError("policy unavailable"))):
                check_submission_before_send(begin_attempt)
        begin_attempt.assert_not_called()
        check_submission_before_send(begin_attempt)
        begin_attempt.assert_called_once_with()

    def test_body_reads_shrink_socket_timeout_and_prefer_read1(self) -> None:
        """
        함수 이름: test_body_reads_shrink_socket_timeout_and_prefer_read1()
        기능: 조각마다 잔여 예산으로 socket 제한을 줄이고 누적 read를 피하는지 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        response = DeadlineResponse(self.clock, [b"first", b"second", b""], 1)
        self.transport._opener.open.return_value = response
        self.assertEqual(self._request().body, b"firstsecond")
        self.assertEqual([call.args[0] for call in response.socket.settimeout.call_args_list], [6, 5, 4])
        self.assertTrue(response.closed)

    def test_slow_body_cannot_extend_operation_deadline(self) -> None:
        """
        함수 이름: test_slow_body_cannot_extend_operation_deadline()
        기능: 짧은 간격의 수신도 누적 예산이 끝나면 응답을 폐기하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        response = DeadlineResponse(self.clock, [b"first", b"second", b"third"], 2)
        self.transport._opener.open.return_value = response
        with self.assertRaises(RequestDeadlineExceeded):
            self._request()
        self.assertEqual(response.read_count, 3)
        self.assertEqual([call.args[0] for call in response.socket.settimeout.call_args_list], [6, 4, 2])
        self.assertTrue(response.closed)

    def test_parent_deadline_bounds_response_and_closes_it_on_expiry(self) -> None:
        """
        함수 이름: test_parent_deadline_bounds_response_and_closes_it_on_expiry()
        기능: 직전 요청이 소진한 시간을 새 HTTP 요청이 다시 확보하지 못하게 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        response = DeadlineResponse(self.clock, [b"late"], 2)
        self.transport._opener.open.return_value = response
        with request_deadline_scope(6):
            self.clock.return_value = 104
            with self.assertRaises(RequestDeadlineExceeded):
                self._request()
        self.assertEqual(self.transport._opener.open.call_args.kwargs["timeout"], 2)
        self.assertTrue(response.closed)

    def test_http_error_body_shrinks_underlying_socket_timeout(self) -> None:
        """
        함수 이름: test_http_error_body_shrinks_underlying_socket_timeout()
        기능: HTTPError의 더 깊은 body stream에도 잔여 예산을 적용하는지 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        response = DeadlineResponse(self.clock, [b"error", b""], 2)
        error = HTTPError("https://deadline.invalid", 503, "Busy", response.headers, response)
        self.transport._opener.open.side_effect = error
        result = self._request()
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.body, b"error")
        self.assertEqual([call.args[0] for call in response.socket.settimeout.call_args_list], [6, 4])
        self.assertTrue(response.closed)

    def test_http_error_body_closes_when_deadline_expires(self) -> None:
        """
        함수 이름: test_http_error_body_closes_when_deadline_expires()
        기능: 오류 body 읽기가 만료돼도 응답 자원이 닫히는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        response = DeadlineResponse(self.clock, [b"error", b"again", b"late"], 2)
        error = HTTPError("https://deadline.invalid", 503, "Busy", response.headers, response)
        self.transport._opener.open.side_effect = error
        with self.assertRaises(RequestDeadlineExceeded):
            self._request()
        self.assertTrue(response.closed)

    def test_response_without_read1_keeps_compatibility(self) -> None:
        """
        함수 이름: test_response_without_read1_keeps_compatibility()
        기능: read1 없는 주입 응답도 read fallback으로 정상 해석되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        response = SimpleNamespace(read=BytesIO(b"fixture").read)
        with request_deadline_scope(6):
            self.assertEqual(UrllibHTTPTransport._read_response_body(response, 6), b"fixture")

    def test_submission_guard_failure_never_enters_opener(self) -> None:
        """
        함수 이름: test_submission_guard_failure_never_enters_opener()
        기능: opener 앞 검사가 실패하면 HTTP 전송에 진입하지 않는지 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        with self.assertRaises(SubmissionPreflightRejected):
            self._request(before_send=Mock(side_effect=SubmissionPreflightRejected("stopped")))
        self.transport._opener.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
