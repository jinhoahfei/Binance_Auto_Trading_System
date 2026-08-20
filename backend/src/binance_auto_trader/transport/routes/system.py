"""Process health와 미래 shutdown endpoint route를 정의한다."""

from ..contracts import SCHEMA_VERSION, TransportResponse, success_response
from . import RouteContext, feature_not_available


def get_health(request_id: str, context: RouteContext) -> TransportResponse:
    """
    함수 이름: get_health()
    기능: token이나 credential 없이 process, session, schema와 readiness를 반환한다.
    인자: request_id -> 검증을 마친 요청 UUID
        context -> bootstrap runtime과 event stream context
    반환값: versioned health 성공 응답
    작성 날짜: 2026/08/21
    """
    # lifecycle state와 ready를 동일 application publication 임계 구역에서 읽는다.
    with context.runtime.application_lock:
        runtime_state = getattr(context.runtime, "state")
        status_value = getattr(runtime_state, "status", "UNKNOWN")
        status_text = getattr(status_value, "value", status_value)
        ready = context.runtime.ready

    return success_response(
        request_id,
        {
            "process": "running",
            "session_id": context.event_stream.session_id,
            "schema_version": SCHEMA_VERSION,
            "ready": ready,
            "state": str(status_text),
        },
    )


def request_shutdown(
    request_id: str,
    context: RouteContext,
) -> TransportResponse:
    """
    함수 이름: request_shutdown()
    기능: Phase 12 안전 종료 owner가 없으므로 shutdown 요청을 fail closed한다.
    인자: request_id -> 검증을 마친 요청 UUID
        context -> application readiness를 확인할 route context
    반환값: typed feature unavailable 응답
    작성 날짜: 2026/08/21
    """
    return feature_not_available(  # Phase 12 안전 종료 owner 전까지 process를 유지한다.
        request_id,
        context,
        "shutdown",
    )
