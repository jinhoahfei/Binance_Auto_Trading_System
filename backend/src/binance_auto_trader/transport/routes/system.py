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
from binance_auto_trader.bootstrap.shutdown_preparation import get_shutdown_preparation, start_shutdown_preparation


def read_shutdown_preparation(request_id: str, context: RouteContext) -> TransportResponse:
    """
    함수 이름: read_shutdown_preparation()
    기능: 인증된 요청에 종료 준비 상태를 반환한다.
    인자: request_id, context -> 해당 작업에 필요한 입력 값
    반환값: 해당 단계의 처리 결과 또는 없음
    작성 날짜: 2026/09/16
    """
    state = get_shutdown_preparation(context.runtime)
    if state is None:
        return error_response(request_id, TransportContractError("SHUTDOWN_PREPARATION_NOT_STARTED",
            "Shutdown preparation has not started.", status=409))
    return success_response(request_id, state)


def prepare_shutdown(request_id: str, context: RouteContext, request_body: JsonObject, command_id: str) -> TransportResponse:
    """
    함수 이름: prepare_shutdown()
    기능: 정확한 요청 형식과 청산 동의를 검증한 후 종료 준비를 시작한다.
    인자: request_id, context, request_body, command_id -> 해당 작업에 필요한 입력 값
    반환값: 해당 단계의 처리 결과 또는 없음
    작성 날짜: 2026/09/16
    """
    require_command_fields(request_body, ("expected_version", "liquidation_confirmed"))
    version = require_expected_version(request_body)
    confirmed = request_body.get("liquidation_confirmed")
    if type(confirmed) is not bool:
        raise TransportContractError("MALFORMED_REQUEST", "liquidation_confirmed must be boolean", status=400)
    state = start_shutdown_preparation(context.runtime, expected_version=version, liquidation_confirmed=confirmed)
    return success_response(request_id, state, status=202)


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
    # 이 값은 종료 허가가 아닌 낙관적 기준이다. 공용 잠금이 막혀도 준비를 요청할 수 있어야 한다.
    if hasattr(context.runtime, "_state_store"):
        ready = context.runtime._state_store.state.ready
    else:
        ready = context.runtime.ready
    if not ready:
        return error_response(request_id, TransportContractError("BACKEND_NOT_READY", "Backend startup is not complete.", status=503, retryable=True))
    trading_controller = context.runtime.trading_controller
    read_basis = getattr(trading_controller, "shutdown_command_basis", None)
    if callable(read_basis):
        # Immutable publication을 읽는다. worker가 잠금을 보유해도 종료 요청을 시작할 수 있다.
        version, status = read_basis()
    else:
        version, status = trading_controller.context.version, trading_controller.status.value
    shutdown_state = {
        "session_id": context.event_stream.session_id,
        "version": version,
        "status": status,
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
