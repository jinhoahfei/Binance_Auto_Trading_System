"""종료 %B 근거가 누락되거나 중복 게시돼도 인계 판정이 바뀌지 않는지 검증한다."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tests.integration.test_public_market_case2_flow import (
    PublicCase2OrderScenario, _create_public_case2_fixture, _trigger_public_case_c_buy,
    _create_live_thirty_minute_kline, _create_closed_one_minute_kline,
)


class PreliveExitProvenanceFlowTests(unittest.TestCase):
    """
    클래스 이름: PreliveExitProvenanceFlowTests
    기능: partial 이후 terminal 종료 근거를 한 번만 고정하고 인계까지 검증한다.
    작성 날짜: 2026/09/09
    """

    def test_exit_evidence_is_frozen_including_missing_value(self):
        """
        함수 이름: test_exit_evidence_is_frozen_including_missing_value()
        기능: 종료 근거 재게시가 %B를 다시 계산하지 않고 None은 wait-only 인계로 처리한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        for exit_pct_b, expected_transition in ((Decimal("0.20"), "PC-27"), (None, "PC-28")):
            with self.subTest(exit_pct_b=exit_pct_b), TemporaryDirectory() as directory:
                fixture = _create_public_case2_fixture(
                    directory, PublicCase2OrderScenario.SELL_PARTIAL_THEN_FILLED,
                )
                controller = fixture.trading_controller
                _trigger_public_case_c_buy(fixture)
                fixture.clock.advance(timedelta(seconds=5))
                fixture.market_controller.observe_kline(_create_live_thirty_minute_kline(
                    close_price=Decimal("101"), candle_low=Decimal("84"), event_time=fixture.clock(),
                ))
                asyncio.run(controller.drain_events())
                fixture.clock.advance(timedelta(seconds=35))
                fixture.market_controller.observe_kline(_create_closed_one_minute_kline(
                    close_price=Decimal("90"), event_time=fixture.clock(),
                ))
                asyncio.run(controller.drain_events())
                sell_order = fixture.rest_client.submitted_orders[1]
                fixture.clock.advance(timedelta(seconds=5))
                fixture.market_controller.observe_kline(_create_live_thirty_minute_kline(
                    close_price=Decimal("115"), candle_low=Decimal("84"), event_time=fixture.clock(),
                ))
                asyncio.run(controller.drain_events())
                with patch(
                    "binance_auto_trader.application.trading_controller.calculate_execution_pct_b",
                    return_value=exit_pct_b,
                ) as calculate:
                    events = controller.trigger_order_reconciliation(
                        occurred_at=fixture.clock.advance(timedelta(seconds=6)),
                    )
                    self.assertEqual(1, len(events))
                    state = controller._order_states_by_client_id[sell_order.client_order_id]
                    # 저장 재시도/중복 terminal 게시에서 뒤늦은 다른 시장 근거를 채택하면 안 된다.
                    calculate.return_value = Decimal("0.80")
                    self.assertTrue(controller._publish_case_c_exit_result(state))
                    self.assertEqual(1, calculate.call_count)
                    self.assertEqual(exit_pct_b, controller.context.runtime.case_c_exit_pct_b)
                results = asyncio.run(controller.drain_events())
                transitions = tuple(t for result in results for t in result.transition_ids)
                self.assertIn(expected_transition, transitions)
                self.assertNotIn("PC-27" if expected_transition == "PC-28" else "PC-28", transitions)
