"""운영 로그 저장 경로와 application 진단 sink를 composition root에서 연결한다."""

from pathlib import Path

from binance_auto_trader.adapters.filesystem.diagnostic_log_writer import DiagnosticLogWriter
from binance_auto_trader.application.runtime_diagnostics import RuntimeDiagnostics


def resolve_log_directory() -> Path:
    """
    함수 이름: resolve_log_directory()
    기능: 실행 작업 디렉터리와 무관하게 프로젝트 Log_History 경로를 선택한다.
    인자: 없음
    반환값: 소스 checkout 또는 설치 환경의 로그 디렉터리
    작성 날짜: 2026/09/09
    """
    # 소스 실행은 프로젝트 표식을 찾아 backend 하위나 임의 cwd에서도 같은 위치를 사용한다.
    for parent in Path(__file__).resolve().parents:
        if (parent / "CODING_CONVENTIONS.md").is_file():
            return parent / "Log_History"

    # Packaged sidecar에서도 사용자가 지정한 Desktop 프로젝트가 있으면 그 경로를 유지한다.
    desktop_project = Path.home() / "Desktop" / "Binance_Auto"
    if desktop_project.is_dir():
        return desktop_project / "Log_History"
    return Path.home() / "Binance_Auto" / "Log_History"  # 프로젝트가 없는 설치의 사용자별 기본값이다.


def create_runtime_diagnostics(execution_mode: str, log_directory: Path | None = None) -> RuntimeDiagnostics:
    """
    함수 이름: create_runtime_diagnostics()
    기능: LIVE·Testnet은 기본 파일을 연결하고 offline 시험은 명시한 디렉터리에만 기록한다.
    인자: execution_mode -> 정규화 실행 환경, log_directory -> 검증용 명시 경로 또는 기본값
    반환값: runtime마다 독립된 진단 출력 경계
    작성 날짜: 2026/09/09
    """
    if execution_mode not in ("live", "testnet") and log_directory is None:
        return RuntimeDiagnostics()

    # 파일 생성 실패는 이 factory에서 즉시 알려 기록 없는 LIVE 시작을 방지한다.
    writer = DiagnosticLogWriter(log_directory if log_directory is not None else resolve_log_directory(), execution_mode)
    diagnostics = RuntimeDiagnostics(writer)
    diagnostics.record("logging_started", log_path=str(writer.path), rotation_bytes=16 * 1024 * 1024)
    return diagnostics  # 실제 주문 권한이나 금융 정책을 이 조립기에서 변경하지 않는다.
