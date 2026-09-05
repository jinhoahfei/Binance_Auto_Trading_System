"""Process health와 안전한 shutdown endpoint route를 정의한다."""

from binance_auto_trader.bootstrap import (
    ShutdownBlockedError,
    request_application_shutdown,
)

from ..contracts import (
    JsonObject,
    SCHEMA_VERSION,
    TransportContractError,
    TransportResponse,
    error_response,
    require_command_fields,
    require_expected_version,
    success_response,
)
from . import RouteContext, application_error_response, require_ready_runtime


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


def get_shutdown_state(request_id: str, context: RouteContext) -> TransportResponse:
    """
    함수 이름: get_shutdown_state()
    기능: 대시보드 mapping과 독립적으로 종료 명령에 필요한 session, version과 lifecycle을 조회한다.
    인자: request_id -> 검증된 요청 UUID
        context -> application runtime과 event stream context
    반환값: 안전 종료 허가가 아닌 최소 optimistic command 기준값
    작성 날짜: 2026/09/05
    """
    # 화면 데이터에 오류가 있어도 종료 기준은 동일 application publication에서 읽는다.
    with context.runtime.application_lock:
        readiness_failure = require_ready_runtime(request_id, context)
        if readiness_failure is not None:
            return readiness_failure
        trading_controller = context.runtime.trading_controller
        shutdown_state = {
            "session_id": context.event_stream.session_id,
            "version": trading_controller.context.version,
            "status": trading_controller.status.value,
        }

    return success_response(request_id, shutdown_state)  # Exposure 검사는 POST owner가 다시 수행한다.


def request_shutdown(
    request_id: str,
    context: RouteContext,
    request_body: JsonObject,
    command_id: str,
) -> TransportResponse:
    """
    함수 이름: request_shutdown()
    기능: expected version과 exposure를 검증하고 fsync·stream close까지 안전 종료를 조정한다.
    인자: request_id -> 검증을 마친 요청 UUID
        context -> application runtime과 event stream route context
        request_body -> schema와 expected_version shutdown DTO
        command_id -> Idempotency-Key에서 얻은 stable command ID
    반환값: HTTP 202 accepted 또는 typed blocked/version 실패 응답
    작성 날짜: 2026/08/24
    """
    # Shutdown DTO도 다른 mutation과 같은 exact schema 및 optimistic version 계약을 사용한다.
    require_command_fields(request_body, ("expected_version",))
    expected_version = require_expected_version(request_body)

    try:
        receipt = request_application_shutdown(
            context.runtime,
            command_id=command_id,
            expected_version=expected_version,
        )
    except ShutdownBlockedError as error:
        failure = TransportContractError(
            error.code,
            str(error),
            status=409,
            retryable=False,
            details=error.receipt.to_blocked_details(),
        )
        return error_response(
            request_id,
            failure,
        )  # 주문·수량·credential 없이 해소할 blocker 종류만 반환한다.
    except Exception as error:
        return application_error_response(
            request_id,
            error,
        )  # STALE_CONTEXT_VERSION만 기존 application allowlist로 변환한다.

    # Runtime 자원이 CLOSED인 뒤 shared WebSocket stream을 닫아 새 event publication을 차단한다.
    context.event_stream.close()
    return success_response(
        request_id,
        {
            "accepted": receipt.accepted,
            "status": receipt.status.value,
            "version": receipt.version,
        },
        status=202,
    )
