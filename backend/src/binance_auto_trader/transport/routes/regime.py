"""REGIME 선택 command를 sole owner인 RegimeController에 연결한다."""

from ..contracts import (
    JsonObject,
    TransportResponse,
    regime_from_wire,
    require_command_fields,
    require_expected_version,
    success_response,
)
from . import (
    RouteContext,
    application_error_response,
    require_ready_runtime,
)


def select_regime(
    request_id: str,
    context: RouteContext,
    request_body: JsonObject,
    command_id: str,
) -> TransportResponse:
    """
    함수 이름: select_regime()
    기능: strict DTO와 version을 검증하고 사용자 REGIME 선택을 원자 적용한다.
    인자: request_id -> 검증을 마친 요청 UUID
        context -> application runtime과 event stream route context
        request_body -> schema, regime_type과 expected_version command DTO
        command_id -> Idempotency-Key에서 얻은 stable command ID
    반환값: selected/support 상태와 새 Context version 응답
    작성 날짜: 2026/08/21
    """
    # Exact command schema를 확인하고 REGIME와 optimistic version을 typed 값으로 복원한다.
    require_command_fields(
        request_body,
        ("regime_type", "expected_version"),
    )
    selected_regime = regime_from_wire(
        request_body["regime_type"]
    )  # wire 값은 normalization 없이 canonical enum으로 바꾼다.
    expected_version = require_expected_version(request_body)

    # Selection과 event publication을 같은 application 임계 구역에서 직렬화한다.
    with context.runtime.application_lock:
        readiness_response = require_ready_runtime(request_id, context)
        if readiness_response is not None:
            return readiness_response

        regime_controller = context.runtime.regime_controller
        try:
            selection_result = regime_controller.set_regime_type(
                selected_regime,
                command_id=command_id,
                expected_version=expected_version,
            )
        except Exception as error:
            return application_error_response(request_id, error)

        # Commit 결과의 support 상태와 exact version을 한 REGIME_SELECTED event로 발행한다.
        support_status = getattr(selection_result, "support_status")
        context_version = getattr(
            selection_result,
            "version",
        )  # command가 commit한 exact version을 response와 event에 함께 사용한다.
        context.event_stream.publish(
            "REGIME_SELECTED",
            {
                "selected": selected_regime,
                "support_status": support_status,
                "version": context_version,
            },
            aggregate_version=context_version,
            correlation_id=command_id,
        )

    # Event와 동일한 선택 결과를 command 성공 envelope로 반환한다.
    return success_response(
        request_id,
        {
            "selected": selected_regime,
            "support_status": support_status,
            "version": context_version,
        },
    )
