"""CSV export endpoint를 strict option과 실제 filesystem writer에 연결한다."""

from ..contracts import (
    JsonObject,
    TransportContractError,
    TransportResponse,
    error_response,
    map_csv_export_result,
    parse_csv_export_options,
    success_response,
)
from . import RouteContext, require_ready_runtime


_CSV_FAILURE_STATUS_BY_CODE = {
    "CSV_EXPORT_IO_FAILED": 503,
    "CSV_EXPORT_UNAVAILABLE": 503,
    "DESTINATION_EXISTS": 409,
    "INVALID_CSV_EXPORT_OPTIONS": 422,
    "NO_TRADES_TO_EXPORT": 422,
    "TRADE_HISTORY_PERSISTENCE_PENDING": 409,
}
_CSV_FAILURE_MESSAGE_BY_CODE = {
    "CSV_EXPORT_IO_FAILED": "The CSV file could not be written safely.",
    "CSV_EXPORT_UNAVAILABLE": "CSV export is not available.",
    "DESTINATION_EXISTS": "The destination CSV file already exists.",
    "INVALID_CSV_EXPORT_OPTIONS": "CSV export options are invalid.",
    "NO_TRADES_TO_EXPORT": "No trades match the CSV export range.",
    "TRADE_HISTORY_PERSISTENCE_PENDING": (
        "Pending trade persistence must be resolved before CSV export."
    ),
}


def _csv_export_error_response(
    request_id: str,
    error: Exception,
) -> TransportResponse:
    """
    함수 이름: _csv_export_error_response()
    기능: allowlist의 CSV domain/application 오류만 안전한 transport failure로 변환한다.
    인자: request_id -> 공통 envelope의 요청 UUID
        error -> CSV export 경계에서 발생한 typed 오류
    반환값: 내부 경로와 OS 원인을 숨긴 실패 응답
    작성 날짜: 2026/08/23
    """
    # Enum 또는 문자열 code를 HTTP allowlist에 대조해 예기치 않은 내부 예외를 분리한다.
    raw_code = getattr(error, "code", None)
    error_code = getattr(raw_code, "value", raw_code)
    if not isinstance(error_code, str):
        error_code = "CSV_EXPORT_IO_FAILED"
    if error_code not in _CSV_FAILURE_STATUS_BY_CODE:
        error_code = "CSV_EXPORT_IO_FAILED"

    # Filesystem 예외 원문과 실제 path는 공개하지 않고 안정적인 code와 문구만 보낸다.
    status = _CSV_FAILURE_STATUS_BY_CODE[error_code]
    failure = TransportContractError(
        error_code,
        _CSV_FAILURE_MESSAGE_BY_CODE[error_code],
        status=status,
        retryable=status == 503,
    )
    return error_response(request_id, failure)  # UI는 code로 수정·재시도 분기를 결정한다.


def create_csv_export(
    request_id: str,
    context: RouteContext,
    request_body: JsonObject,
    command_id: str,
) -> TransportResponse:
    """
    함수 이름: create_csv_export()
    기능: strict CSV option을 Controller streaming export와 typed receipt에 연결한다.
    인자: request_id -> 검증을 마친 요청 UUID
        context -> application runtime과 event stream route context
        request_body -> schema와 CSV option을 담은 exact command DTO
        command_id -> Idempotency-Key에서 얻은 stable command ID
    반환값: 실제 파일 receipt 또는 validation/readiness/filesystem 실패 응답
    작성 날짜: 2026/08/23
    """
    # Idempotency wrapper를 우회한 직접 route 호출도 빈 command identity를 허용하지 않는다.
    if not isinstance(command_id, str) or not command_id:
        raise TypeError("command_id must be a non-empty string")

    # Renderer draft와 무관하게 backend가 exact field, timezone, date와 filename을 다시 검증한다.
    try:
        options = parse_csv_export_options(request_body)
    except TransportContractError as error:
        return error_response(request_id, error)

    # 긴 파일 쓰기 동안 global application publication lock을 점유하지 않고 readiness만 읽는다.
    with context.runtime.application_lock:
        readiness_response = require_ready_runtime(request_id, context)
        if readiness_response is not None:
            return readiness_response
        trade_history_controller = context.runtime.trade_history_controller

    # Controller operation lock이 durable stream과 execution publication의 export 순서를 조정한다.
    try:
        result = trade_history_controller.export_csv(options)
        response_dto = map_csv_export_result(result)
    except Exception as error:
        return _csv_export_error_response(
            request_id,
            error,
        )  # 예상 밖 filesystem/controller 결함도 raw 예외 없이 retryable failure로 닫는다.

    return success_response(
        request_id,
        response_dto,
        status=201,
    )  # 파일 publication과 receipt mapping이 모두 끝난 뒤에만 created를 반환한다.
