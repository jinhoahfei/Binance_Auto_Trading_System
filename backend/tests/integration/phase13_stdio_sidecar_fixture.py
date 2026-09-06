"""Platform-neutral subprocess에서 Windows framed sidecar의 실제 lifecycle을 실행한다."""

import os
from pathlib import Path
from unittest.mock import patch

from binance_auto_trader.sidecar_stdio import run_stdio_sidecar_process
from binance_auto_trader.transport.app import _RuntimeOwnershipLock
from tests.integration.phase12_sidecar_fixture import _create_fixture_runtime_factory


# Test 완료 신호는 실제 production fsync 뒤에만 게시하며 ownership 파일 자체의 lock은 유지한다.
_ORIGINAL_MARK_ORPHANED = _RuntimeOwnershipLock.mark_orphaned


def _mark_orphaned_and_publish_completion(ownership_lock: _RuntimeOwnershipLock) -> None:
    """
    함수 이름: _mark_orphaned_and_publish_completion()
    기능: 실제 ORPHANED fsync가 끝난 뒤 test parent에게 별도 secret-free 완료 신호를 보낸다.
    인자: ownership_lock -> production runtime이 소유한 동일 lock instance
    반환값: 원래 mark_orphaned와 완료 marker 생성이 끝나면 없음
    작성 날짜: 2026/09/06
    """
    # Parent가 Windows mandatory byte lock이나 truncate/write 중간 상태를 읽지 않게 한다.
    _ORIGINAL_MARK_ORPHANED(ownership_lock)
    marker_path = Path.cwd() / ".fixture-orphaned-complete"
    marker_descriptor = os.open(marker_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(marker_descriptor)  # 비어 있는 marker에는 token, credential과 runtime identity를 쓰지 않는다.


def main() -> None:
    """
    함수 이름: main()
    기능: 실제 frame parser와 HTTP runtime에 network-free fixture factory만 주입한다.
    인자: 없음
    반환값: post-CLOSED framed acknowledgement 뒤 없음
    작성 날짜: 2026/09/06
    """
    # 실제 marker publication 완료만 관찰하고 원래 ownership·HTTP lifecycle 동작은 바꾸지 않는다.
    with patch.object(_RuntimeOwnershipLock, "mark_orphaned", _mark_orphaned_and_publish_completion):
        if os.name == "nt":
            # Test child에서만 KnownFolder 결과를 격리 cwd로 바꾸고 native lock/reparse 검증은 유지한다.
            with patch(
                "binance_auto_trader.adapters.platform.windows_runtime.get_local_app_data_directory",
                return_value=Path.cwd(),
            ):
                run_stdio_sidecar_process(_create_fixture_runtime_factory)
            return

        run_stdio_sidecar_process(_create_fixture_runtime_factory)  # macOS에서도 동일 frame/lifecycle 코드를 실행한다.


if __name__ == "__main__":
    main()
