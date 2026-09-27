"""Application aggregate를 원자적으로 조합하는 snapshot route를 정의한다."""

from ..contracts import (
    TransportContractError,
    TransportResponse,
    build_snapshot_dto,
    error_response,
    success_response,
)
from . import RouteContext


_SNAPSHOT_LOCK_TIMEOUT_SECONDS = 1.0


def _snapshot_busy_response(request_id: str) -> TransportResponse:
    """
    함수 이름: _snapshot_busy_response()
    기능: snapshot의 대기 상한 초과를 읽기 전용 재시도가 가능한 응답으로 반환한다.
    인자: request_id -> 검증을 마친 요청 UUID
    반환값: 동일 요청 식별자를 보존한 retryable 503 응답
    작성 날짜: 2026/09/22
    """
    failure = TransportContractError(
        "BACKEND_NOT_READY",
        "Backend snapshot is temporarily unavailable.",
        status=503,
        retryable=True,
        details={"reason": "SNAPSHOT_BUSY"},
    )
    return error_response(request_id, failure)


def get_snapshot(request_id: str, context: RouteContext) -> TransportResponse:
    """
    함수 이름: get_snapshot()
    기능: 요청 수와 잠금 대기를 제한하며 완료 state와 마지막 event sequence를 원자적으로 반환한다.
    인자: request_id -> 검증을 마친 요청 UUID
        context -> bootstrap runtime과 event stream context
    반환값: coherent snapshot 또는 BACKEND_NOT_READY 응답
    작성 날짜: 2026/08/21
    """
    # 이전 UI 읽기가 취소됐어도 서버에서 대기하는 요청 수는 고정된 상한을 넘지 않는다.
    if not context.snapshot_request_slots.acquire(blocking=False):
        return _snapshot_busy_response(request_id)

    try:
        application_lock = context.runtime.application_lock
        if not application_lock.acquire(timeout=_SNAPSHOT_LOCK_TIMEOUT_SECONDS):
            return _snapshot_busy_response(request_id)

        # Application 잠금 다음에 event cursor를 읽는 기존 publication 순서를 유지한다.
        try:
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
        finally:
            application_lock.release()

        return success_response(request_id, snapshot_dto)
    finally:
        context.snapshot_request_slots.release()
