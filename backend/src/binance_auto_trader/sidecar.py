"""Packaged Tauri external binary가 호출하는 production sidecar 진입점을 제공한다."""

from binance_auto_trader.bootstrap.sidecar import (
    READY_DESCRIPTOR_FD,
    SESSION_TOKEN_FD,
    SIDECAR_CONFIGURATION_FD,
    STOP_SIGNAL_FD,
    _create_sidecar_runtime_factory,
    read_sidecar_configuration_from_fd,
)
from binance_auto_trader.transport import SCHEMA_VERSION, run_transport_process


def run_sidecar_process() -> None:
    """
    함수 이름: run_sidecar_process()
    기능: 고정 FD 3/4/5/6 계약으로 production loopback sidecar를 시작하고 안전 종료까지 기다린다.
    인자: 없음
    반환값: application CLOSED 뒤 transport process가 종료되면 없음
    작성 날짜: 2026/08/24
    """
    # FD 번호 자체가 launcher와 child의 ABI이므로 argv나 환경변수 fallback을 제공하지 않는다.
    configuration = read_sidecar_configuration_from_fd(
        SIDECAR_CONFIGURATION_FD,
        expected_schema_version=SCHEMA_VERSION,
    )
    configuration.history_path.parent.mkdir(
        mode=0o700,
        parents=True,
        exist_ok=True,
    )  # Tauri가 선택한 per-user app-data directory만 필요한 시점에 생성한다.

    runtime_factory = _create_sidecar_runtime_factory(configuration)
    run_transport_process(
        runtime_factory,
        token_fd=SESSION_TOKEN_FD,
        ready_fd=READY_DESCRIPTOR_FD,
        stop_fd=STOP_SIGNAL_FD,
        allowed_origins=(configuration.allowed_origin,),
        require_closed_before_stop=True,
    )  # Parent pipe만 끊겨도 exposure 검증 없는 close로 내려가지 않는다.


def main() -> None:
    """
    함수 이름: main()
    기능: argv와 환경변수를 읽지 않고 bootstrap의 고정 inherited FD sidecar를 실행한다.
    인자: 없음
    반환값: 안전 종료 뒤 process runner가 반환하면 없음
    작성 날짜: 2026/08/24
    """
    run_sidecar_process()  # Credential을 argv, environment, stdout 또는 log로 복사하지 않는다.


if __name__ == "__main__":
    main()  # Frozen binary와 `python -m binance_auto_trader.sidecar`가 같은 경계를 공유한다.
