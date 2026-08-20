"""REGIME 선택 endpoint의 Phase 5 fail-closed route를 정의한다."""

from ..contracts import TransportResponse
from . import RouteContext, feature_not_available


def select_regime(
    request_id: str,
    context: RouteContext,
) -> TransportResponse:
    """
    함수 이름: select_regime()
    기능: Phase 7 RegimeController operation 전에는 선택 command를 실행하지 않는다.
    인자: request_id -> 검증을 마친 요청 UUID
        context -> application readiness를 확인할 route context
    반환값: typed feature unavailable 응답
    작성 날짜: 2026/08/21
    """
    # Phase 5 transport는 selected regime의 상태 전이나 Controller version을 추측하지 않는다.
    return feature_not_available(  # Phase 7 owner operation 전까지 command를 닫는다.
        request_id,
        context,
        "regime_selection",
    )
