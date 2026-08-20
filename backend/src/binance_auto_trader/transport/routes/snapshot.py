"""Application aggregate를 원자적으로 조합하는 snapshot route를 정의한다."""

from ..contracts import (
    TransportContractError,
    TransportResponse,
    build_snapshot_dto,
    error_response,
    success_response,
)
from . import RouteContext


def get_snapshot(request_id: str, context: RouteContext) -> TransportResponse:
    """
    함수 이름: get_snapshot()
    기능: startup 완료 state와 포함된 마지막 event sequence를 원자적으로 반환한다.
    인자: request_id -> 검증을 마친 요청 UUID
        context -> bootstrap runtime과 event stream context
    반환값: coherent snapshot 또는 BACKEND_NOT_READY 응답
    작성 날짜: 2026/08/21
    """
    # 반드시 application lock을 먼저 잡아 aggregate와 sequence의 publication 순서를 고정한다.
    with context.runtime.application_lock:
        if not context.runtime.ready:
            failure = TransportContractError(
                "BACKEND_NOT_READY",
                "Backend startup is not complete.",
                status=503,
                retryable=True,
            )
            return error_response(request_id, failure)

        last_sequence = context.event_stream.last_sequence  # 같은 임계 구역의 replay cursor이다.
        snapshot_dto = build_snapshot_dto(
            context.runtime,
            context.event_stream.session_id,
            last_sequence,
        )

    return success_response(request_id, snapshot_dto)
