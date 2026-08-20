"""Trading lifecycle와 split ratio endpoint의 fail-closed route를 정의한다."""

from ..contracts import TransportResponse
from . import RouteContext, feature_not_available


def start_trading(
    request_id: str,
    context: RouteContext,
) -> TransportResponse:
    """
    함수 이름: start_trading()
    기능: Phase 7 TradingController start operation 전에는 command를 실행하지 않는다.
    인자: request_id -> 검증을 마친 요청 UUID
        context -> application readiness를 확인할 route context
    반환값: typed feature unavailable 응답
    작성 날짜: 2026/08/21
    """
    # Phase 5 route는 TradingController owner command가 생길 때까지 business 전이를 만들지 않는다.
    return feature_not_available(  # TradingSTM을 route에서 직접 실행하지 않는다.
        request_id,
        context,
        "trading_start",
    )


def stop_trading(
    request_id: str,
    context: RouteContext,
) -> TransportResponse:
    """
    함수 이름: stop_trading()
    기능: Position과 reconciliation owner가 없으므로 stop command를 실행하지 않는다.
    인자: request_id -> 검증을 마친 요청 UUID
        context -> application readiness를 확인할 route context
    반환값: typed feature unavailable 응답
    작성 날짜: 2026/08/21
    """
    return feature_not_available(  # Position owner가 추가될 때까지 stop을 닫는다.
        request_id,
        context,
        "trading_stop",
    )


def update_split_ratios(
    request_id: str,
    context: RouteContext,
) -> TransportResponse:
    """
    함수 이름: update_split_ratios()
    기능: Phase 7 TradingContext owner 전에는 split ratio를 변경하지 않는다.
    인자: request_id -> 검증을 마친 요청 UUID
        context -> application readiness를 확인할 route context
    반환값: typed feature unavailable 응답
    작성 날짜: 2026/08/21
    """
    return feature_not_available(  # TradingContext owner 전에는 비율을 변경하지 않는다.
        request_id,
        context,
        "split_ratios",
    )
