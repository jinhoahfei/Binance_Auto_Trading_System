"""원본 signal 마감시각이 내부 후속 주문 직전까지 유지되는지 검증한다."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest

from binance_auto_trader.domain.trading.context import MarketEvaluationSnapshot
from binance_auto_trader.domain.trading.states import (
    CaseBSignalState, CaseCSignalState, OwnershipState, RootState, TradingStateConfiguration,
)
from tests.integration.test_public_market_case2_flow import _create_public_case2_fixture


class PreliveSignalTimeFlowTests(unittest.TestCase):
    """
    클래스 이름: PreliveSignalTimeFlowTests
    기능: 시장 observer와 실제 직렬 queue에서 신호 만료 및 후속 주문 차단을 검증한다.
    작성 날짜: 2026/09/09
    """

    def test_late_signal_is_expired_before_internal_pullback_can_submit(self):
        """
        함수 이름: test_late_signal_is_expired_before_internal_pullback_can_submit()
        기능: 늦은 확정봉에서 signal 생성 직후 내부 microstep이 원본 시각 기준 3시간 초과를 검사한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        with TemporaryDirectory() as directory:
            fixture = _create_public_case2_fixture(directory)
            controller = fixture.trading_controller
            controller._active_stm._state = TradingStateConfiguration(
                root_state=RootState.TRADE_MANAGEMENT, ownership_state=OwnershipState.NO_POSITION,
                case_b_signal_state=CaseBSignalState.B_WAIT_SIGNAL,
                case_c_signal_state=CaseCSignalState.CASE_C_FINAL_STATE,
            )
            close_time = fixture.clock() - timedelta(hours=3, seconds=1)
            evaluation = MarketEvaluationSnapshot(
                realtime_price=Decimal("100"), lower_band=Decimal("90"), upper_band=Decimal("110"),
                realtime_pct_b=Decimal("0.30"), current_30m_low=Decimal("99"),
                confirmed_30m_close=True, confirmed_30m_close_time=close_time,
                ema_slope_30m_close=Decimal("0.01"), pct_b_close=Decimal("0.26"),
                current_closed_candle_low=Decimal("99"),
                previous_3_closed_candle_lows=(Decimal("98"),) * 3,
            )
            controller.observe_market_evaluation(evaluation, source_event_id="delayed-signal",
                                                 market_version=controller._market_snapshot.version)
            results = asyncio.run(controller.drain_events())
            transitions = tuple(t for result in results for t in result.transition_ids)
            self.assertIn("B-05", transitions)
            self.assertIn("B-08", transitions)
            self.assertEqual([], fixture.rest_client.submitted_orders)
