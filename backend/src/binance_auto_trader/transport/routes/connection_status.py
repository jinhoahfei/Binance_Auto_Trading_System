"""백엔드와 Binance 사이의 API 및 WebSocket 연결 진단을 제공한다."""

from time import time_ns

from ..contracts import TransportResponse, success_response
from . import RouteContext, require_ready_runtime


def get_binance_connection_status(
    request_id: str,
    context: RouteContext,
) -> TransportResponse:
    """
    함수 이름: get_binance_connection_status()
    기능: Binance 인증 API 조회와 백엔드 소유 시세·계좌 WebSocket 연결 상태를 반환한다.
    인자: request_id -> 검증된 요청 UUID
        context -> 백엔드 gateway와 readiness를 제공하는 route context
    반환값: 계좌 내용이나 credential이 없는 연결 상태 응답
    작성 날짜: 2026/09/05
    """
    with context.runtime.application_lock:
        readiness_failure = require_ready_runtime(request_id, context)
        if readiness_failure is not None:
            return readiness_failure
        api_gateway = getattr(context.runtime, "api_gateway")
        web_socket_gateway = getattr(context.runtime, "web_socket_gateway")

    # 진단용 GET은 application lock 밖에서 실행하고 Account aggregate에는 적용하지 않는다.
    try:
        api_gateway.fetch_account_snapshot()
        api_status = "online"
    except Exception:
        api_status = "offline"  # 인증·네트워크 오류의 원문이나 계좌 정보는 UI에 보내지 않는다.

    diagnostics = getattr(context.runtime, "diagnostics", None)
    if diagnostics is not None:
        diagnostics.liveness.observe("api_observed", {"api": api_status})

    return success_response(
        request_id,
        {
            "api": api_status,
            "checked_at_ms": time_ns() // 1_000_000,
            "market_stream": (
                "online" if web_socket_gateway.kline_connected else "offline"
            ),
            "account_stream": (
                "online" if web_socket_gateway.account_connected else "offline"
            ),
        },
    )
