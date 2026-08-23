"""Trade history 상세 조회 endpoint의 fail-closed route를 정의한다."""

from ..contracts import (
    TransportContractError,
    TransportResponse,
    error_response,
    map_trade_details,
    parse_trade_history_query_parameters,
    success_response,
)
from . import RouteContext, require_ready_runtime


def get_trades(
    request_id: str,
    context: RouteContext,
    query_parameters: str,
) -> TransportResponse:
    """
    함수 이름: get_trades()
    기능: strict 기간·방향 query를 Controller 상세 조회와 composite 응답에 연결한다.
    인자: request_id -> 검증을 마친 요청 UUID
        context -> application runtime과 event stream route context
        query_parameters -> request target에서 분리한 raw percent-encoded query string
    반환값: 상세 조회 성공 또는 typed validation/readiness/query failure 응답
    작성 날짜: 2026/08/23
    """
    # Application owner 진입 전 exact field와 wire enum을 검증해 fallback 조회를 차단한다.
    try:
        period, side = parse_trade_history_query_parameters(query_parameters)
    except TransportContractError as error:
        return error_response(request_id, error)

    # Query와 DTO mapping을 같은 application lock 아래 두어 Account·History provenance를 보존한다.
    with context.runtime.application_lock:
        readiness_response = require_ready_runtime(request_id, context)
        if readiness_response is not None:
            return readiness_response

        try:
            trade_details = (
                context.runtime.trade_history_controller.get_trade_details(
                    period,
                    side,
                )
            )
            response_dto = map_trade_details(
                trade_details,
                period,
            )  # Controller가 적용한 날짜·side와 요청 preset을 composite query로 묶는다.
        except TransportContractError as error:
            return error_response(
                request_id,
                error,
            )  # Page 상한 등 이미 검증된 transport 오류는 정확한 status를 보존한다.
        except Exception:
            failure = TransportContractError(
                "TRADE_HISTORY_QUERY_FAILED",
                "Trade history details could not be queried.",
                status=503,
                retryable=True,
            )
            return error_response(  # Repository 및 내부 DTO 오류 내용은 외부에 공개하지 않는다.
                request_id,
                failure,
            )

    return success_response(request_id, response_dto)
