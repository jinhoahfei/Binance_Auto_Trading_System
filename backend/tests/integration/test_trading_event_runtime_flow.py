"""Production event runtime이 복구 포지션 청산을 수동 drain 없이 완료하는지 검증한다."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, RLock
from types import SimpleNamespace
import unittest

from binance_auto_trader.application.trading_controller import (
    TradingSessionStatus,
)
from binance_auto_trader.bootstrap.application import (
    _TradingEventRuntimeWorker,
)
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.transport import (
    BackendEventStream,
    create_trading_session_update_observer,
)
from binance_auto_trader.transport.routes import RouteContext
from binance_auto_trader.transport.routes.trading import (
    liquidate_recovered_position,
)
from tests.integration.test_order_reconciliation_flow import (
    _submit_case_b_buy,
)
from tests.integration.test_testnet_restart_reconciliation_flow import (
    _FilledSubmissionTestnetRESTClient,
    _create_recovery_controller,
)


class TradingEventRuntimeFlowTests(unittest.TestCase):
    """
    클래스 이름: TradingEventRuntimeFlowTests
    기능: public recovery route, bounded worker와 terminal publication의 최소 E2E를 검증한다.
    작성 날짜: 2026/08/24
    """

    def test_sync_filled_recovery_reaches_terminated_without_manual_drain(
        self,
    ) -> None:
        """
        함수 이름: test_sync_filled_recovery_reaches_terminated_without_manual_drain()
        기능: 동기 FILLED recovery SELL이 202 뒤 worker를 통해 G-06F와 TERMINATED를 게시하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"

            # 첫 process는 durable BUY lot을 남겨 다음 process의 명시 복구 청산 입력을 만든다.
            first_client = _FilledSubmissionTestnetRESTClient(
                submission_exchange_order_id="95001",
                submission_trade_id="55001",
            )
            first_controller, first_history, first_position = (
                _create_recovery_controller(
                    history_path,
                    first_client,
                    command_gate=True,
                    order_retry_waiter=lambda _delay: None,
                )
            )
            first_controller.reconcile_startup_state()
            selected_stm = first_controller.fetch_selected_trading_logic(
                RegimeType.TYPE_0
            )
            selection = first_controller.commit_regime_selection(
                RegimeType.TYPE_0,
                selected_stm,
                command_id="runtime-select",
                expected_version=0,
            )
            first_controller.start_trading(
                command_id="runtime-start",
                expected_version=selection.version,
            )
            _submit_case_b_buy(
                first_controller,
                intent_id="runtime-recovery-buy",
            )
            buy_result = first_client.result

            self.assertGreater(first_position.quantity, Decimal("0"))
            self.assertEqual(len(first_history.trade_history.trades), 1)

            # 둘째 process의 Controller notifier를 아직 생성 중인 worker holder에 연결한다.
            application_lock = RLock()
            worker_holder: list[_TradingEventRuntimeWorker] = []

            def request_processing() -> bool:
                """
                함수 이름: request_processing()
                기능: Controller wake를 조립 완료된 단일 worker에 전달한다.
                인자: 없음
                반환값: worker가 준비됐고 요청을 수락했으면 True
                작성 날짜: 2026/08/24
                """
                if not worker_holder:
                    return False

                return worker_holder[0].request_processing()

            liquidation_client = _FilledSubmissionTestnetRESTClient(
                startup_result=buy_result,
                submission_exchange_order_id="95002",
                submission_trade_id="55002",
            )
            controller, history_controller, position = (
                _create_recovery_controller(
                    history_path,
                    liquidation_client,
                    command_gate=True,
                    order_retry_waiter=lambda _delay: None,
                    event_runtime_notifier=request_processing,
                    application_lock=application_lock,
                )
            )
            controller.reconcile_startup_state()

            # Route의 STOPPING event와 worker의 TERMINATED event는 같은 replay stream을 공유한다.
            event_stream = BackendEventStream()
            transport_observer = create_trading_session_update_observer(
                event_stream
            )
            terminal_publication = Event()

            def publish_trading_state() -> object:
                """
                함수 이름: publish_trading_state()
                기능: worker 상태를 transport stream에 게시하고 terminal waiter를 깨운다.
                인자: 없음
                반환값: 발급된 BackendEventEnvelope
                작성 날짜: 2026/08/24
                """
                published_event = transport_observer(controller, "testnet")
                trading_payload = published_event.payload["trading"]
                if trading_payload["status"] == "terminated":
                    terminal_publication.set()

                return published_event

            worker = _TradingEventRuntimeWorker(
                controller.run_event_runtime_cycle,
                controller.mark_event_runtime_failed,
                lambda: True,
                controller.snapshot_session,
                application_lock,
                state_update_observer=publish_trading_state,
                poll_interval_seconds=0.01,
            )
            worker_holder.append(worker)
            runtime = SimpleNamespace(
                application_lock=application_lock,
                ready=True,
                execution_mode="testnet",
                trading_controller=controller,
            )
            route_context = RouteContext(runtime, event_stream)  # type: ignore[arg-type]

            try:
                self.assertTrue(worker.start())
                self.assertEqual(event_stream.last_sequence, 0)

                # Public route는 queue를 drain하지 않고 202 receipt와 STOPPING snapshot만 반환한다.
                expected_version = controller.context.version
                response = liquidate_recovered_position(
                    "743237dc-c630-4c29-8892-93301dc9b350",
                    route_context,
                    {
                        "schema_version": 3,
                        "expected_version": expected_version,
                    },
                    "runtime-recovered-liquidation",
                )

                self.assertEqual(response.status, 202)
                self.assertEqual(response.payload["data"]["status"], "stopping")
                self.assertTrue(terminal_publication.wait(1.0))
            finally:
                worker.close()

            # Worker cycle만으로 terminal state, zero Position과 durable SELL 한 건이 완결된다.
            self.assertFalse(worker.failed)
            self.assertIs(controller.status, TradingSessionStatus.TERMINATED)
            self.assertEqual(position.quantity, Decimal("0"))
            self.assertEqual(liquidation_client.submit_count, 1)
            self.assertEqual(len(history_controller.trade_history.trades), 2)
            self.assertEqual(
                history_controller.get_pending_order_recovery_records(),
                (),
            )
            published_events = event_stream.replay_after(0).events
            self.assertEqual(
                tuple(
                    event.payload["trading"]["status"]
                    for event in published_events
                    if event.event_type == "TRADING_SESSION_UPDATED"
                ),
                ("stopping", "terminated"),
            )
            action_type_names = tuple(
                type(action).__name__ for action in controller.action_trace
            )
            self.assertEqual(action_type_names.count("ForceSellAll"), 1)
            self.assertNotIn(
                "SubmitOrder",
                action_type_names,
            )  # Strategy run의 BUY Action 없이 recovery force-sell path만 사용한다.


if __name__ == "__main__":
    unittest.main()
