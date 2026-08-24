"""Phase 5 UI 교차 process test가 실행할 실제 loopback backend fixture이다."""

from __future__ import annotations

from collections.abc import Callable
import os
from unittest.mock import patch

from binance_auto_trader.application import MarketDataController
from binance_auto_trader.bootstrap import ApplicationRuntime, start_application
from binance_auto_trader.domain.trading import Account
from binance_auto_trader.transport import run_transport_process
from binance_auto_trader.transport.contracts import json_bytes
from tests.integration.test_startup_flow import (
    _MarketStartupBehavior,
    _create_test_runtime,
)


TOKEN_FILE_DESCRIPTOR = 3
STOP_FILE_DESCRIPTOR = 4
TRACE_FILE_DESCRIPTOR = 5  # 이 test-only pipe에는 secret이 아닌 message ID만 기록한다.
READY_FILE_DESCRIPTOR = 1
TEST_UI_ORIGIN = "http://127.0.0.1:5173"


def _create_runtime(
    account_update_observer: Callable[[Account], object],
    trade_history_update_observer: Callable[[object, object], object],
    trading_session_update_observer: Callable[[object, object], object],
) -> ApplicationRuntime:
    """
    함수 이름: _create_runtime()
    기능: 실제 process transport와 같은 event stream observer를 공유하는 fake runtime을 만든다.
    인자: account_update_observer -> transport가 소유하는 Account 변경 observer
        trade_history_update_observer -> transport가 소유하는 Trade·Performance observer
        trading_session_update_observer -> transport가 소유하는 trading lifecycle observer
    반환값: fake Binance 경계와 조립된 application runtime
    작성 날짜: 2026/08/21
    """
    runtime, _, _, _ = _create_test_runtime(
        [],
        account_update_observer=account_update_observer,
        trade_history_update_observer=trade_history_update_observer,
        trading_session_update_observer=trading_session_update_observer,
    )
    return runtime


def _start_runtime_and_write_trace(runtime: ApplicationRuntime) -> object:
    """
    함수 이름: _start_runtime_and_write_trace()
    기능: 메시지 1~3 startup을 완료하고 secret 없는 trace ID를 test pipe에 기록한다.
    인자: runtime -> transport가 조립한 application runtime
    반환값: start_application이 공개한 ready state
    작성 날짜: 2026/08/21
    """
    operation_trace: list[str] = []
    market_behavior = _MarketStartupBehavior(runtime, operation_trace)

    # 실제 lifecycle을 보존하되 network market slice만 검증된 golden vector로 대체한다.
    with patch.object(
        MarketDataController,
        "initialize_market_data",
        autospec=True,
        side_effect=market_behavior,
    ):
        ready_state = start_application(runtime)

    # 별도 test FD에는 UI가 이어 검증할 최상위 Communication message ID만 보낸다.
    trace_payload = {
        "message_ids": [entry.message_id for entry in runtime.startup_trace],
    }
    os.write(TRACE_FILE_DESCRIPTOR, json_bytes(trace_payload) + b"\n")
    os.close(TRACE_FILE_DESCRIPTOR)

    return ready_state


def main() -> None:
    """
    함수 이름: main()
    기능: inherited pipe handshake로 실제 loopback process를 실행하고 stop까지 대기한다.
    인자: 없음
    반환값: parent stop 신호 뒤 정상 종료하면 없음
    작성 날짜: 2026/08/21
    """
    # Token은 source, argv와 environment가 아닌 anonymous inherited FD에서만 읽힌다.
    run_transport_process(
        _create_runtime,
        token_fd=TOKEN_FILE_DESCRIPTOR,
        ready_fd=READY_FILE_DESCRIPTOR,
        stop_fd=STOP_FILE_DESCRIPTOR,
        allowed_origins=(TEST_UI_ORIGIN,),
        start_runtime=_start_runtime_and_write_trace,
    )


if __name__ == "__main__":
    main()
