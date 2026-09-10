"""장시간 반복된 시장 복구 후 명시적 중지와 프로그램 종료 재시도를 검증한다."""
from tempfile import TemporaryDirectory
import unittest
from binance_auto_trader.application.trading_controller import (
    ReconciliationCauseStatus, ReconciliationCauseCategory, TradingSessionStatus,
)
from binance_auto_trader.bootstrap import start_application, ApplicationStatus
from binance_auto_trader.bootstrap.lifecycle import request_application_shutdown, ShutdownBlockedError
from binance_auto_trader.domain.common import RegimeType
from tests.integration.test_deterministic_production_path_case2_flow import (
    _create_deterministic_production_path_fixture, _close_deterministic_fixture,
)


class RepeatedMarketRecoveryShutdownTests(unittest.TestCase):
    """
    클래스 이름: RepeatedMarketRecoveryShutdownTests
    기능: 실제 startup·worker·journal·shutdown 조립에서 반복 시장 장애의 종료 진행을 확인한다.
    작성 날짜: 2026/09/10
    """
    def test_repeated_recovery_and_exit_retry_reach_closed(self):
        """
        함수 이름: test_repeated_recovery_and_exit_retry_reach_closed()
        기능: 60회 장애·초기화 알림 뒤 복구 전후 중지 및 종료 재시도가 CLOSED까지 완료됨을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        for stop_before_recovery in (False, True):
            with self.subTest(stop_before_recovery=stop_before_recovery), TemporaryDirectory() as directory:
                fixture = _create_deterministic_production_path_fixture(directory)
                try:
                    runtime = fixture.runtime
                    start_application(runtime)
                    controller = runtime.trading_controller
                    selection = runtime.regime_controller.set_regime_type(RegimeType.TYPE_0, command_id='select', expected_version=controller.context.version)
                    controller.start_trading(command_id='start', expected_version=selection.version)
                    for cycle in range(60):
                        controller.mark_market_stream_reconciliation_required('kline_stream_invalid')
                        controller.mark_market_stream_reconciliation_required('market_stream_initializing')
                        if cycle < 59:
                            controller.complete_market_stream_reconciliation(runtime.market_snapshot.version)
                    self.assertIs(controller.reconciliation_cause_snapshot.status, ReconciliationCauseStatus.EXACT)
                    self.assertIs(controller.reconciliation_cause_snapshot.category, ReconciliationCauseCategory.MARKET_STREAM_FAILED)
                    self.assertIs(controller.status, TradingSessionStatus.RECONCILIATION_REQUIRED)
                    # 포지션이 없어도 시세 복구가 미완료인 동안 종료 요청은 거부되어야 한다.
                    with self.assertRaises(ShutdownBlockedError):
                        request_application_shutdown(runtime, command_id='exit-before-recovery', expected_version=controller.context.version)
                    if stop_before_recovery:
                        for attempt in range(3):
                            controller.stop_trading(command_id=f'stop-retry-{attempt}', expected_version=controller.context.version)
                        self.assertIs(controller.status, TradingSessionStatus.RECONCILIATION_REQUIRED)
                    controller.complete_market_stream_reconciliation(runtime.market_snapshot.version)
                    if not stop_before_recovery:
                        self.assertIs(controller.status, TradingSessionStatus.RECONCILIATION_REQUIRED)
                        controller.stop_trading(command_id='stop-after-recovery', expected_version=controller.context.version)
                    self.assertIs(controller.status, TradingSessionStatus.TERMINATED)
                    self.assertFalse(controller.reconciliation_required)
                    receipt = request_application_shutdown(runtime, command_id='exit-retry', expected_version=controller.context.version)
                    self.assertTrue(receipt.accepted)
                    self.assertFalse(receipt.position_open)
                    self.assertFalse(receipt.pending_order)
                    self.assertFalse(receipt.reconciliation_required)
                    self.assertIs(runtime.state.status, ApplicationStatus.CLOSED)
                    replay = request_application_shutdown(runtime, command_id='exit-again', expected_version=controller.context.version)
                    self.assertTrue(replay.accepted)
                finally:
                    _close_deterministic_fixture(fixture)
