"""순차 REST 작업의 단조 시계 예산을 thread/context 안에서 공유한다."""

from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from functools import wraps
from math import isfinite
from numbers import Real
from time import monotonic


_REQUEST_DEADLINE: ContextVar[Real | None] = ContextVar("request_deadline", default=None)
_SUBMISSION_CHECK: ContextVar[Callable[[], bool] | None] = ContextVar("submission_check", default=None)
_SUBMISSION_GUARD: ContextVar[AbstractContextManager | None] = ContextVar("submission_guard", default=None)


class RequestDeadlineExceeded(TimeoutError):
    """
    클래스 이름: RequestDeadlineExceeded
    기능: 실제 socket 오류와 구분되는 로컬 작업 예산 만료를 전달한다.
    작성 날짜: 2026/09/22
    """


class SubmissionPreflightRejected(RuntimeError):
    """
    클래스 이름: SubmissionPreflightRejected
    기능: 전송 시도 시작 전에 세션 권한이 닫혔음을 adapter에 명시한다.
    작성 날짜: 2026/09/22
    """


@contextmanager
def submission_check_scope(check: Callable[[], bool] | None, *, guard: AbstractContextManager | None = None):
    """
    함수 이름: submission_check_scope()
    기능: 동일 thread의 실제 POST 경계에 예약된 세션 권한 검사를 전달한다.
    인자: check -> 주문 전송 직전 검사 또는 조회 작업의 None
        guard -> 권한 검사와 전송 시도 기록을 원자적으로 묶을 재진입 잠금
    반환값: 이전 검사 경계를 복원하는 context manager
    작성 날짜: 2026/09/22
    """
    token = _SUBMISSION_CHECK.set(check)
    guard_token = _SUBMISSION_GUARD.set(guard)
    try:
        yield
    finally:
        _SUBMISSION_GUARD.reset(guard_token)
        _SUBMISSION_CHECK.reset(token)


def check_submission_before_send(begin_attempt: Callable[[], None]) -> None:
    """
    함수 이름: check_submission_before_send()
    기능: 최신 제출 권한과 남은 예산을 검사한 뒤에만 실제 전송 시도를 기록한다.
    인자: begin_attempt -> adapter의 freshness·attempt 기록 함수
    반환값: 검사 통과 시 없음
    작성 날짜: 2026/09/22
    """
    with _SUBMISSION_GUARD.get() or nullcontext():
        check = _SUBMISSION_CHECK.get()
        if check is not None and check() is not True:
            raise SubmissionPreflightRejected("order submission permission changed")
        remaining_request_timeout(30)
        begin_attempt()  # 동일 잠금 아래 기록한 뒤의 실패는 거래소 수락 가능성을 보존한다.


@contextmanager
def request_deadline_scope(timeout_seconds: Real):
    """
    함수 이름: request_deadline_scope()
    기능: 중첩 작업이 기존 마감 시간을 연장하지 못하는 단조 시계 예산을 설정한다.
    인자: timeout_seconds -> 전체 외부 작업의 양수 초 제한
    반환값: 종료 시 이전 예산을 복원하는 context manager
    작성 날짜: 2026/09/22
    """
    if isinstance(timeout_seconds, bool) or not isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("operation timeout must be finite and positive")
    previous_deadline = _REQUEST_DEADLINE.get()
    deadline = monotonic() + timeout_seconds
    if previous_deadline is not None:
        deadline = min(deadline, previous_deadline)
    token = _REQUEST_DEADLINE.set(deadline)
    try:
        yield
    finally:
        _REQUEST_DEADLINE.reset(token)  # 다음 주문은 이전 작업의 소진 예산을 상속하지 않는다.


def remaining_request_timeout(timeout_seconds: Real) -> Real:
    """
    함수 이름: remaining_request_timeout()
    기능: 개별 요청 제한을 현재 작업의 남은 시간으로 줄이고 만료 시 전송을 차단한다.
    인자: timeout_seconds -> 개별 socket 작업의 초 제한
    반환값: 현재 허용되는 양수 대기 시간
    작성 날짜: 2026/09/22
    """
    deadline = _REQUEST_DEADLINE.get()
    if deadline is None:
        return timeout_seconds
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise RequestDeadlineExceeded("external operation deadline exceeded")
    return min(timeout_seconds, remaining)


def operation_deadline(timeout_seconds: Real):
    """
    함수 이름: operation_deadline()
    기능: 여러 잠금 구간으로 나뉜 동기 복구 전체에 하나의 요청 예산을 적용한다.
    인자: timeout_seconds -> 작업 전체의 초 제한
    반환값: 동기 operation decorator
    작성 날짜: 2026/09/22
    """
    def decorate(operation):
        """
        함수 이름: decorate()
        기능: 원 operation의 공개 이름과 설명을 보존한 예산 wrapper를 만든다.
        인자: operation -> 제한할 동기 함수
        반환값: 예산을 상속하는 함수
        작성 날짜: 2026/09/22
        """
        @wraps(operation)
        def run_with_deadline(*arguments, **keyword_arguments):
            """
            함수 이름: run_with_deadline()
            기능: 동기 작업의 전체 수명에 같은 마감 시간을 유지한다.
            인자: arguments -> 위치 인자, keyword_arguments -> 이름 인자
            반환값: 원 operation 결과
            작성 날짜: 2026/09/22
            """
            with request_deadline_scope(timeout_seconds):
                return operation(*arguments, **keyword_arguments)
        return run_with_deadline
    return decorate
