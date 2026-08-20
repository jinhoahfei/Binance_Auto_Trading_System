"""Trade history 상세 조회 endpoint의 fail-closed route를 정의한다."""

from ..contracts import TransportResponse
from . import RouteContext, feature_not_available


def get_trades(request_id: str, context: RouteContext) -> TransportResponse:
    """
    함수 이름: get_trades()
    기능: Phase 10 상세 조회 owner operation 전에는 임의 필터링 결과를 만들지 않는다.
    인자: request_id -> 검증을 마친 요청 UUID
        context -> application readiness를 확인할 route context
    반환값: typed feature unavailable 응답
    작성 날짜: 2026/08/21
    """
    # Pagination과 filter 의미를 Phase 10보다 먼저 임의로 구현하지 않는다.
    return feature_not_available(  # Phase 10 query operation 전에는 상세 조회를 닫는다.
        request_id,
        context,
        "trade_details",
    )
