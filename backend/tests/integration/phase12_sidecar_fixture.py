"""Fresh exec child에서 production sidecar FD ABI를 검증할 test runtime을 조립한다."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import patch

from binance_auto_trader.bootstrap import (
    ApplicationRuntime,
    ApplicationStatus,
    create_application_runtime,
)
from binance_auto_trader.bootstrap.sidecar import SidecarConfiguration
from binance_auto_trader.sidecar import run_sidecar_process
from binance_auto_trader.domain.trading import Account
from tests.unit.bootstrap.test_application import (
    _StubRestClient,
    _StubWebSocketClient,
)


def _create_fixture_runtime_factory(
    configuration: SidecarConfiguration,
) -> Callable[[Callable[[Account], object], Callable[[object, object], object]], ApplicationRuntime]:
    """
    함수 이름: _create_fixture_runtime_factory()
    기능: FD6 history 경로와 transport observer를 사용하는 READY test runtime factory를 만든다.
    인자: configuration -> production parser가 검증한 sidecar configuration
    반환값: observer 두 개를 받아 ApplicationRuntime을 반환하는 factory
    작성 날짜: 2026/08/24
    """
    if not isinstance(configuration, SidecarConfiguration):
        raise TypeError("configuration must be a SidecarConfiguration")

    def runtime_factory(
        account_update_observer: Callable[[Account], object],
        trade_history_update_observer: Callable[[object, object], object],
    ) -> ApplicationRuntime:
        """
        함수 이름: runtime_factory()
        기능: network startup 없이 실제 shutdown과 fsync를 수행할 READY runtime을 조립한다.
        인자: account_update_observer -> shared Account event observer
            trade_history_update_observer -> shared Trade event observer
        반환값: production transport가 소유할 ApplicationRuntime
        작성 날짜: 2026/08/24
        """
        runtime = create_application_runtime(
            _StubRestClient(),
            _StubWebSocketClient(),
            history_path=configuration.history_path,
            account_update_observer=account_update_observer,
            trade_history_update_observer=trade_history_update_observer,
        )
        with runtime.application_lock:
            # Production start callback이 network I/O 없이 멱등 READY를 관찰하게 한다.
            runtime.trading_controller._startup_reconciliation_complete = True
            runtime._publish_state(
                status=ApplicationStatus.READY,
                failure=None,
                startup_trace=(),
            )

        return runtime

    return runtime_factory


def main() -> None:
    """
    함수 이름: main()
    기능: fresh interpreter에서 runtime factory만 test double로 바꾸고 production FD entrypoint를 실행한다.
    인자: 없음
    반환값: post-CLOSED FD5 acknowledgement 뒤 process runner가 반환하면 없음
    작성 날짜: 2026/08/24
    """
    # Config parser, fixed FD와 transport runner는 production 코드를 그대로 실행한다.
    with patch(
        "binance_auto_trader.sidecar._create_sidecar_runtime_factory",
        side_effect=_create_fixture_runtime_factory,
    ):
        run_sidecar_process()


if __name__ == "__main__":
    main()  # Fork 이후 즉시 exec된 child만 이 test fixture entrypoint를 호출한다.
