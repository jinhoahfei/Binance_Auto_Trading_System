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

    failure = TransportContractError(
        "FEATURE_NOT_AVAILABLE",
        "This feature is not available in the current application phase.",
        status=503,
        retryable=False,
        details={"feature": feature_name},
    )
    return error_response(request_id, failure)


__all__ = [
    "RouteContext",
    "feature_not_available",
]
