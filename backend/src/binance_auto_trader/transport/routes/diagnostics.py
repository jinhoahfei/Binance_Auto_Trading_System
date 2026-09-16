"""인증된 local reader에 부작용 없는 생존 관측만 제공한다."""

from ..contracts import success_response


def get_liveness(request_id, context):
    """함수 이름: get_liveness()
    기능: 긴 거래 잠금과 거래소 호출 없이 기존 진단 snapshot을 반환한다.
    인자: request_id -> 검증된 UUID, context -> 인증된 runtime
    반환값: 생존 진단 envelope
    작성 날짜: 2026/09/16
    """
    # 진단 전용 작은 상태만 읽고 trading application_lock에는 진입하지 않는다.
    snapshot = context.runtime.diagnostics.liveness.snapshot()
    if context.diagnostic_process_start_id is not None:
        snapshot["process_start_id"] = context.diagnostic_process_start_id
    return success_response(request_id, {
        "session_id": context.event_stream.session_id,
        **snapshot,  # 계좌·인증 정보는 원래부터 이 snapshot에 포함되지 않는다.
    })
