"""시장 연결 오류 후 명시적 중지가 안전 종료 가능한 상태로 이어지는지 검증한다."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import PropertyMock, patch

from binance_auto_trader.application.trading_controller import TradingSessionStatus
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.adapters.binance.websocket_gateway import WebSocketGateway
from tests.integration.test_order_reconciliation_flow import _submit_case_b_buy
from tests.integration.test_testnet_restart_reconciliation_flow import (
    _create_recovery_controller, _FilledSubmissionTestnetRESTClient,
)


class MarketInterruptionStopTests(TestCase):
    """
    클래스 이름: MarketInterruptionStopTests
    기능: 시장 복구 전후의 STOP 처리와 다른 오류의 차단 유지를 검증한다.
    작성 날짜: 2026/09/10
    """
    def test_explicit_stop_survives_market_recovery_without_auto_resume(self):
        """
        함수 이름: test_explicit_stop_survives_market_recovery_without_auto_resume()
        기능: 복구 전 중지 요청을 보존하고 다른 원인이 없을 때만 TERMINATED로 전이한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        for stop_before_recovery in (False, True):
            for worker_failed in (False, True):
                with self.subTest(early=stop_before_recovery, worker_failed=worker_failed):
                    directory = TemporaryDirectory()
                    self.addCleanup(directory.cleanup)
                    controller, _, _ = _create_recovery_controller(
                        Path(directory.name) / 'history.jsonl', _FilledSubmissionTestnetRESTClient(), command_gate=True,
                    )
                    self.addCleanup(controller.close_session_resources)
                    controller.reconcile_startup_state()
                    selected = controller.commit_regime_selection(
                        RegimeType.TYPE_0, controller.fetch_selected_trading_logic(RegimeType.TYPE_0),
                        command_id='select', expected_version=0,
                    )
                    controller.start_trading(command_id='start', expected_version=selected.version)
                    for _ in range(3):
                        controller.mark_market_stream_reconciliation_required('kline_stream_invalid')
                        controller.mark_market_stream_reconciliation_required('market_stream_initializing')
                    if worker_failed:
                        controller.mark_event_runtime_failed()
                    if stop_before_recovery:
                        controller.stop_trading(command_id='stop', expected_version=controller.context.version)
                        self.assertIs(controller.status, TradingSessionStatus.RECONCILIATION_REQUIRED)
                    with patch.object(WebSocketGateway, 'kline_live_ready', new_callable=PropertyMock, return_value=True):
                        controller.complete_market_stream_reconciliation(controller._market_snapshot.version)
                    if not stop_before_recovery:
                        # 데이터 복구만으로 매매가 자동 재개되거나 임의 종료되어서는 안 된다.
                        self.assertIs(controller.status, TradingSessionStatus.RECONCILIATION_REQUIRED)
                        controller.stop_trading(command_id='stop', expected_version=controller.context.version)
                    self.assertIs(controller.status, TradingSessionStatus.RECONCILIATION_REQUIRED
                                  if worker_failed else TradingSessionStatus.TERMINATED)
                    self.assertEqual(controller.reconciliation_required, worker_failed)

    def test_requested_stop_liquidates_position_once_after_market_recovery(self):
        """
        함수 이름: test_requested_stop_liquidates_position_once_after_market_recovery()
        기능: 실제 주문 pipeline의 fake 매수 포지션을 시세 복구 후 한 번만 청산하여 종료한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        with TemporaryDirectory() as directory:
            client = _FilledSubmissionTestnetRESTClient()
            controller, history, position = _create_recovery_controller(
                Path(directory) / 'history.jsonl', client, command_gate=True,
            )
            try:
                controller.reconcile_startup_state()
                selected = controller.commit_regime_selection(
                    RegimeType.TYPE_0, controller.fetch_selected_trading_logic(RegimeType.TYPE_0),
                    command_id='select', expected_version=0,
                )
                controller.start_trading(command_id='start', expected_version=selected.version)
                _submit_case_b_buy(controller, intent_id='buy')
                asyncio.run(controller.drain_events())
                self.assertGreater(position.quantity, 0)
                for _ in range(3):
                    controller.mark_market_stream_reconciliation_required('kline_stream_invalid')
                    controller.mark_market_stream_reconciliation_required('market_stream_initializing')
                controller.stop_trading(command_id='stop', expected_version=controller.context.version)
                client.submission_exchange_order_id = '93002'
                client.submission_trade_id = '43002'
                with patch.object(WebSocketGateway, 'kline_live_ready', new_callable=PropertyMock, return_value=True):
                    controller.complete_market_stream_reconciliation(controller._market_snapshot.version)
                asyncio.run(controller.run_event_runtime_cycle())
                self.assertIs(controller.status, TradingSessionStatus.TERMINATED)
                self.assertFalse(controller.reconciliation_required)
                self.assertEqual(position.quantity, 0)
                controller.stop_trading(command_id='stop-again', expected_version=controller.context.version)
                self.assertEqual(client.submit_count, 2)  # fake 매수 1회와 청산 매도 1회뿐이다.
                self.assertEqual(len(history.trade_history.trades), 2)
            finally:
                controller.close_session_resources()
