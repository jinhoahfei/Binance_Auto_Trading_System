"""HTTP route가 공유하는 context와 fail-closed helper를 정의한다."""

from dataclasses import dataclass

from ..contracts import (
    RuntimeSnapshotSource,
    TransportContractError,
    TransportResponse,
    error_response,
)
from ..event_stream import BackendEventStream


@dataclass(frozen=True, slots=True)
class RouteContext:
    """
    클래스 이름: RouteContext
    기능: route가 읽을 bootstrap runtime과 transport event stream을 조립한다.
    작성 날짜: 2026/08/21
    """

    runtime: RuntimeSnapshotSource
    event_stream: BackendEventStream

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: runtime lock과 event stream의 최소 transport 계약을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # Route가 의존하는 runtime lock과 event stream의 최소 타입 계약을 함께 확인한다.
        if not hasattr(self.runtime, "application_lock"):
            raise TypeError("runtime must expose application_lock")
        if not isinstance(self.event_stream, BackendEventStream):
            raise TypeError("event_stream must be a BackendEventStream")


def feature_not_available(
    request_id: str,
    context: RouteContext,
    feature_name: str,
) -> TransportResponse:
    """
    함수 이름: feature_not_available()
    기능: 미래 Phase owner operation을 흉내 내지 않고 typed failure로 닫는다.
    인자: request_id -> 공통 envelope의 요청 UUID
        context -> application readiness를 확인할 route context
        feature_name -> 안전한 기능 식별자
    반환값: BACKEND_NOT_READY 또는 FEATURE_NOT_AVAILABLE 응답
    작성 날짜: 2026/08/21
    """
    # command/query owner를 호출하기 전에 application startup 완료를 확인한다.
    with context.runtime.application_lock:
        if not context.runtime.ready:
            failure = TransportContractError(
                "BACKEND_NOT_READY",
                "Backend startup is not complete.",
                status=503,
                retryable=True,
            )
            return error_response(  # owner command 진입 전 동일 typed readiness 오류를 보낸다.
                request_id,
                failure,
            )

    # 아직 owner가 없는 기능은 재시도 불가능한 typed unavailable 응답으로 닫는다.
    failure = TransportContractError(
        "FEATURE_NOT_AVAILABLE",
        "This feature is not available in the current application phase.",
        status=503,
        retryable=False,
        details={"feature": feature_name},
    )
    return error_response(request_id, failure)


def require_ready_runtime(
    request_id: str,
    context: RouteContext,
) -> TransportResponse | None:
    """
    함수 이름: require_ready_runtime()
    기능: command owner 호출 전에 application startup readiness를 공통 검사한다.
    인자: request_id -> 공통 오류 envelope의 요청 UUID
        context -> readiness를 가진 route context
    반환값: 준비됐으면 None, 아니면 BACKEND_NOT_READY 응답
    작성 날짜: 2026/08/21
    """
    # Startup 완료 여부만 읽고 Controller 진입 전에 공통 readiness 응답을 결정한다.
    if context.runtime.ready:
        return None

    failure = TransportContractError(
        "BACKEND_NOT_READY",
        "Backend startup is not complete.",
        status=503,
        retryable=True,
    )
    return error_response(  # Controller state를 변경하기 전에 fail-closed 응답을 만든다.
        request_id,
        failure,
    )


def application_error_response(
    request_id: str,
    error: Exception,
) -> TransportResponse:
    """
    함수 이름: application_error_response()
    기능: 알려진 lifecycle domain 오류만 안정적인 HTTP code와 status로 변환한다.
    인자: request_id -> 공통 오류 envelope의 요청 UUID
        error -> application Controller가 발생시킨 typed 오류
    반환값: credential과 내부 상태를 숨긴 typed 실패 응답
    작성 날짜: 2026/08/21
    """
    # Application 오류 code를 HTTP status allowlist에 대조해 예상 오류만 공개한다.
    raw_code = getattr(error, "code", None)
    error_code = getattr(raw_code, "value", raw_code)
    status_by_code = {
        "ACCOUNT_NOT_READY": 503,
        "COMMAND_DISABLED": 403,
        "COMMAND_ID_REUSED": 409,
        "CONNECTION_NOT_READY": 503,
        "INVALID_SESSION_STATE": 409,
        "MARKET_NOT_READY": 503,
        "NO_SELECTED_REGIME": 422,
        "OPEN_POSITION": 409,
        "PENDING_RECONCILIATION": 409,
        "POSITION_RECONCILIATION_REQUIRED": 409,
        "REGIME_NOT_SELECTED": 422,
        "STALE_CONTEXT_VERSION": 409,
        "STALE_RISK_CONTROL_VERSION": 409,
        "TRADING_ACTIVE": 409,
        "TRADING_ALREADY_ACTIVE": 409,
        "TRADING_NOT_STARTED": 409,
        "UNSUPPORTED_TRADING_LOGIC": 422,
    }
    if not isinstance(error_code, str) or error_code not in status_by_code:
        raise error  # 예상하지 않은 결함을 사용자 오류로 위장하지 않는다.

    # Optimistic version 충돌에만 현재와 요청 version을 안전한 정수 detail로 공개한다.
    details = {}
    for attribute_name in ("current_version", "expected_version"):
        attribute_value = getattr(error, attribute_name, None)
        if (
            isinstance(attribute_value, int)
            and not isinstance(attribute_value, bool)
        ):
            details[attribute_name] = attribute_value

    # Status에 맞는 retryability와 안전한 detail만 공통 transport 오류로 변환한다.
    retryable = status_by_code[error_code] == 503
    failure = TransportContractError(
        error_code,
        str(error) or "The trading lifecycle command was rejected.",
        status=status_by_code[error_code],
        retryable=retryable,
        details=details,
    )
    return error_response(request_id, failure)


__all__ = [
    "RouteContext",
    "application_error_response",
    "feature_not_available",
    "require_ready_runtime",
]
