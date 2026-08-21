"""Trading lifecycle와 split ratio command를 TradingController에 연결한다."""

from ..contracts import (
    JsonObject,
    TransportResponse,
    map_trading_snapshot,
    ratio_from_wire,
    require_command_fields,
    require_expected_version,
    success_response,
)
from . import (
    RouteContext,
    application_error_response,
    require_ready_runtime,
)


def _publish_trading_session(
    context: RouteContext,
    command_id: str,
) -> JsonObject:
    """
    함수 이름: _publish_trading_session()
    기능: 최신 lifecycle snapshot을 event stream에 발행하고 같은 DTO를 반환한다.
    인자: context -> application runtime과 event stream route context
        command_id -> lifecycle event의 correlation ID
    반환값: 발행한 authoritative trading snapshot DTO
    작성 날짜: 2026/08/21
    """
    # Controller의 authoritative session snapshot을 만들고 동일 version으로 event를 발행한다.
    trading_controller = context.runtime.trading_controller
    trading_snapshot = map_trading_snapshot(
        trading_controller,
        context.runtime.execution_mode,
    )
    context.event_stream.publish(
        "TRADING_SESSION_UPDATED",
        {"trading": trading_snapshot},
        aggregate_version=trading_snapshot["version"],
        correlation_id=command_id,
    )  # payload 뒤 Context가 바뀌어도 envelope는 같은 snapshot version을 쓴다.

    return trading_snapshot


def start_trading(
    request_id: str,
    context: RouteContext,
    request_body: JsonObject,
    command_id: str,
) -> TransportResponse:
    """
    함수 이름: start_trading()
    기능: 준비 조건과 version을 검증한 뒤 session Context 초기화와 STM 시작을 조정한다.
    인자: request_id -> 검증을 마친 요청 UUID
        context -> application runtime과 event stream route context
        request_body -> schema와 expected_version command DTO
        command_id -> Idempotency-Key에서 얻은 stable command ID
    반환값: 시작된 session status, ID와 새 version 응답
    작성 날짜: 2026/08/21
    """
    # Start DTO의 exact field와 optimistic Context version을 Controller 호출 전에 검증한다.
    require_command_fields(request_body, ("expected_version",))
    expected_version = require_expected_version(
        request_body
    )  # start가 관찰한 Context version을 그대로 사용한다.

    # Context 초기화, G-01 Action 적용과 event publication을 한 임계 구역에서 묶는다.
    with context.runtime.application_lock:
        readiness_response = require_ready_runtime(request_id, context)
        if readiness_response is not None:
            return readiness_response

        trading_controller = context.runtime.trading_controller
        try:
            result = trading_controller.start_trading(
                command_id=command_id,
                expected_version=expected_version,
            )
        except Exception as error:
            return application_error_response(request_id, error)

        _publish_trading_session(context, command_id)

    # Controller가 commit한 lifecycle 결과를 start 성공 envelope로 반환한다.
    return success_response(
        request_id,
        {
            "status": result.status,
            "session_id": result.session_id,
            "version": result.version,
        },
    )


def stop_trading(
    request_id: str,
    context: RouteContext,
    request_body: JsonObject,
    command_id: str,
) -> TransportResponse:
    """
    함수 이름: stop_trading()
    기능: STOP_CONFIRMED를 우선 전달하고 authoritative position/pending 분기를 실행한다.
    인자: request_id -> 검증을 마친 요청 UUID
        context -> application runtime과 event stream route context
        request_body -> schema와 expected_version command DTO
        command_id -> Idempotency-Key에서 얻은 stable command ID
    반환값: stopping, terminated 또는 reconciliation 상태 응답
    작성 날짜: 2026/08/21
    """
    # Stop DTO의 exact field와 optimistic Context version을 Controller 호출 전에 검증한다.
    require_command_fields(request_body, ("expected_version",))
    expected_version = require_expected_version(
        request_body
    )  # stop command의 stale 여부를 STM 호출 전에 판정한다.

    # STOP 전이와 ordered Action 기록 뒤에만 외부에 새 lifecycle snapshot을 발행한다.
    with context.runtime.application_lock:
        readiness_response = require_ready_runtime(request_id, context)
        if readiness_response is not None:
            return readiness_response

        trading_controller = context.runtime.trading_controller
        try:
            result = trading_controller.stop_trading(
                command_id=command_id,
                expected_version=expected_version,
            )
        except Exception as error:
            return application_error_response(request_id, error)

        _publish_trading_session(context, command_id)

    # 비동기 정리 상태는 202, 완전 종료 상태는 200으로 lifecycle 결과를 매핑한다.
    status_text = getattr(result.status, "value", result.status)
    response_status = (
        202
        if status_text in ("stopping", "reconciliation_required")
        else 200
    )
    return success_response(
        request_id,
        {
            "status": result.status,
            "session_id": result.session_id,
            "version": result.version,
        },
        status=response_status,
    )


def update_split_ratios(
    request_id: str,
    context: RouteContext,
    request_body: JsonObject,
    command_id: str,
) -> TransportResponse:
    """
    함수 이름: update_split_ratios()
    기능: 두 Decimal split ratio를 optimistic version으로 TradingContext에 적용한다.
    인자: request_id -> 검증을 마친 요청 UUID
        context -> application runtime과 event stream route context
        request_body -> schema, 두 ratio와 expected_version command DTO
        command_id -> Idempotency-Key에서 얻은 stable command ID
    반환값: 적용된 ratio 문자열과 새 version 응답
    작성 날짜: 2026/08/21
    """
    # Exact split DTO를 확인하고 두 ratio와 optimistic version을 typed 값으로 복원한다.
    require_command_fields(
        request_body,
        ("scale_in", "scale_out", "expected_version"),
    )
    scale_in_ratio = ratio_from_wire(
        request_body["scale_in"],
        "scale_in",
    )  # BUY pending 주문이 읽을 비율을 Decimal로 보존한다.
    scale_out_ratio = ratio_from_wire(
        request_body["scale_out"],
        "scale_out",
    )
    expected_version = require_expected_version(request_body)

    # 두 ratio는 하나의 Context version 전이로 적용한 뒤 같은 snapshot으로 발행한다.
    with context.runtime.application_lock:
        readiness_response = require_ready_runtime(request_id, context)
        if readiness_response is not None:
            return readiness_response

        trading_controller = context.runtime.trading_controller
        try:
            result = trading_controller.update_split_ratios(
                command_id=command_id,
                expected_version=expected_version,
                scale_in=scale_in_ratio,
                scale_out=scale_out_ratio,
            )
        except Exception as error:
            return application_error_response(request_id, error)

        _publish_trading_session(context, command_id)

    # Controller가 commit한 두 canonical Decimal 문자열과 version을 반환한다.
    return success_response(
        request_id,
        {
            "scale_in": result.scale_in,
            "scale_out": result.scale_out,
            "version": result.version,
        },
    )
