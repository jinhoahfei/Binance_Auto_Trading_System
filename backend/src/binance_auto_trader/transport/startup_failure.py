"""시작 실패의 고정 코드만 native 부모에게 전달하며 예외 원문은 공개하지 않는다."""

from binance_auto_trader.application.trading_controller import ResidualBalanceMismatchError
from binance_auto_trader.bootstrap.application import ApplicationStartupError
from .contracts import SCHEMA_VERSION


def startup_failure_payload(error: Exception) -> dict[str, object]:
    """
    함수 이름: startup_failure_payload()
    기능: 실제 startup 예외의 원인에서 허용된 코드만 선택한다.
    인자: error -> READY 이전에 발생한 예외
    반환값: token·경로·원문을 포함하지 않는 bounded STARTUP_FAILED 메시지
    작성 날짜: 2026/09/15
    """
    code = "BACKEND_SIDECAR_STARTUP_FAILED"
    current: BaseException | None = error
    visited: set[int] = set()
    # 원인 chain의 순환·과도한 깊이를 차단하며 실제 application 타입만 해석한다.
    while current is not None and id(current) not in visited and len(visited) < 16:
        visited.add(id(current))
        if isinstance(current, ResidualBalanceMismatchError):
            code = "RESIDUAL_BALANCE_MISMATCH"
            break
        if isinstance(current, ApplicationStartupError):
            code = current.failure.code.value
        current = current.__cause__
    return {"type": "STARTUP_FAILED", "schema_version": SCHEMA_VERSION, "code": code}  # 예외 메시지는 native wire에 복사하지 않는다.
