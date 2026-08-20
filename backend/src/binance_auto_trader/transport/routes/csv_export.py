"""CSV export endpoint의 Phase 5 fail-closed route를 정의한다."""

from ..contracts import TransportResponse
from . import RouteContext, feature_not_available


def create_csv_export(
    request_id: str,
    context: RouteContext,
) -> TransportResponse:
    """
    함수 이름: create_csv_export()
    기능: Phase 11 export owner operation 전에는 파일을 생성하지 않는다.
    인자: request_id -> 검증을 마친 요청 UUID
        context -> application readiness를 확인할 route context
    반환값: typed feature unavailable 응답
    작성 날짜: 2026/08/21
    """
    # Phase 11 owner가 경로와 원자적 write를 정하기 전에는 파일 side effect를 만들지 않는다.
    return feature_not_available(  # Phase 11 owner 전에는 filesystem write를 만들지 않는다.
        request_id,
        context,
        "csv_export",
    )
