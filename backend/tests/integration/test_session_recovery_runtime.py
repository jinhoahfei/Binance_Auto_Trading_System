"""실제 composition root와 단일 워커를 외부 연결 없이 종단 검증한다."""
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest
from unittest.mock import patch
from binance_auto_trader.bootstrap import create_application_runtime, start_application, close_application, ApplicationStatus
from binance_auto_trader.bootstrap.application import _TESTNET_ORDER_CAPABILITY
from binance_auto_trader.domain.common import RegimeType
from tests.integration.test_deterministic_production_path_case2_flow import (
    DeterministicProductionPathRESTClient, DeterministicProductionPathWebSocketClient,
)
from tests.integration.test_buy_sell_flow import MutableUtcClock
from tests.integration.test_public_market_case2_flow import INITIAL_TIME
from tests.integration.phase13_risk_fixture import create_test_risk_policy

class SessionRecoveryRuntimeTests(unittest.TestCase):
    def test_account_disconnect_resumes_via_single_worker_and_real_fresh_evaluation(self):
        with TemporaryDirectory() as directory, patch("socket.create_connection", side_effect=AssertionError("external network forbidden")):
            clock = MutableUtcClock(INITIAL_TIME)
            client = DeterministicProductionPathRESTClient(clock)
            websocket = DeterministicProductionPathWebSocketClient()
            runtime = create_application_runtime(client, websocket, history_path=Path(directory) / "history.jsonl",
                execution_mode="testnet", _testnet_order_capability=_TESTNET_ORDER_CAPABILITY,
                allow_testnet_orders=True, testnet_maximum_order_notional=Decimal("50"),
                risk_policy_state=create_test_risk_policy(), trading_session_update_observer=lambda *args: None, clock=clock, monotonic_clock=lambda: 0, kline_limit=21)
            try:
                self.assertIs(start_application(runtime).status, ApplicationStatus.READY)
                self.assertIs(runtime._account_stream_recovery_worker, runtime._market_stream_recovery_worker)
                c = runtime.trading_controller
                selected = c.commit_regime_selection(RegimeType.TYPE_0, c.fetch_selected_trading_logic(RegimeType.TYPE_0), command_id="select", expected_version=c.context.version)
                c.start_trading(command_id="start", expected_version=selected.version)
                session_id = c.session_id
                original_version = runtime.market_snapshot.version
                # 정상 tick을 보내지 않아도 복구 snapshot 자체가 실제 전략 평가를 깨워야 한다.
                c.mark_account_stream_reconciliation_required("stream_disconnected")
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and c.market_recovery_snapshot()["phase"] != "resumed":
                    Event().wait(.01)
                self.assertEqual(c.market_recovery_snapshot()["phase"], "resumed", str((c.status, c._session_recovery_waiting_evaluation, c._session_recovery_issues, c._session_recovery_in_progress, c._latest_market_evaluation_version, runtime.market_snapshot.version, c._event_runtime_failed, c._event_queue)))
                self.assertEqual(c.session_id, session_id)
                self.assertGreater(c._latest_market_evaluation_version, original_version)
                self.assertIsNotNone(c.market_recovery_snapshot()["last_strategy_evaluation_at"])
                self.assertEqual(client.submitted_orders, [])
            finally:
                close_application(runtime)
