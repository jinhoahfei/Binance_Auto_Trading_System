"""실제 복구·시장 queue·가짜 주문을 통해 Case C의 회복 기준 보존을 검증한다."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import PropertyMock, patch

from binance_auto_trader.adapters.binance.websocket_gateway import WebSocketGateway
from binance_auto_trader.application.trading_controller import TradingSessionStatus
from binance_auto_trader.domain.trading.context import MarketEvaluationSnapshot
from binance_auto_trader.domain.trading.results import TradingSTMResult
from binance_auto_trader.domain.trading.states import CaseCSignalState, StrategyType
from binance_auto_trader.transport.contracts import map_trading_snapshot
from tests.integration.test_buy_sell_flow import FakeOrderScenario, _create_buy_flow_fixture


class CaseCMarketRecoveryTests(unittest.TestCase):
    """
    클래스 이름: CaseCMarketRecoveryTests
    기능: 시세 복구가 회복 기준을 변경하거나 3분 매수 허용 구간을 연장하지 않는지 검증한다.
    작성 날짜: 2026/09/10
    """

    def setUp(self) -> None:
        """
        함수 이름: setUp()
        기능: 외부 연결을 차단하고 임시 이력·가짜 REST·실제 Controller를 준비한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        self.enterContext(patch(
            "socket.create_connection", side_effect=AssertionError("external network forbidden"),
        ))
        directory = self.enterContext(TemporaryDirectory())
        self.fixture = _create_buy_flow_fixture(directory, FakeOrderScenario.IMMEDIATE_FILLED)
        self.controller = self.fixture.controller
        self.addCleanup(self.controller.close_session_resources)
        self.market = MarketEvaluationSnapshot(
            realtime_price=Decimal("2452.65"), lower_band=Decimal("2456.54"),
            upper_band=Decimal("2486.75"), realtime_pct_b=Decimal("-0.128902"),
            current_30m_candle_id="ETHUSDT:30m:recovery", touch_candle_bbw=Decimal("0.01222"),
            cci_30m_realtime=Decimal("-210.144928"),
        )

    def _observe(
        self, price: str, pct_b: str, *, delay: timedelta = timedelta(0),
    ) -> tuple[TradingSTMResult, ...]:
        """
        함수 이름: _observe()
        기능: 지정한 경과 시각의 시장 입력을 실제 관측·직렬 평가·주문 경로에 전달한다.
        인자: price -> 현재 가격, pct_b -> 실시간 %B, delay -> 직전 입력 이후 경과시간
        반환값: 실제 STM 전이 결과
        작성 날짜: 2026/09/10
        """
        self.fixture.clock.advance(delay)
        snapshot = self.controller._market_snapshot
        snapshot.update(snapshot.klines_by_interval)
        source_id = f"case-c-recovery-{snapshot.version}"
        for message_id, event_type in (("1L.1", "KLINE_OBSERVED"), ("1L.2", "MARKET_EVALUATED")):
            self.controller.observe_public_market_boundary(
                message_id=message_id, event_type=event_type,
                source_event_id=source_id, market_version=snapshot.version,
            )
        self.controller.observe_market_evaluation(
            replace(self.market, realtime_price=Decimal(price), realtime_pct_b=Decimal(pct_b)),
            source_event_id=source_id, market_version=snapshot.version,
        )
        return asyncio.run(self.controller.drain_events(max_microsteps=100))

    def _recover(self, delay: timedelta) -> None:
        """
        함수 이름: _recover()
        기능: 실제 시세 장애·계좌 및 주문 대조·자동 재개를 지정한 장애 시간으로 실행한다.
        인자: delay -> 시세를 관측하지 못한 기간
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        self.controller.mark_market_stream_reconciliation_required("kline_stream_invalid")
        self.controller.mark_market_stream_reconciliation_required("market_stream_initializing")
        self.assertIs(self.controller.status, TradingSessionStatus.RECONCILIATION_REQUIRED)
        self.fixture.clock.advance(delay)
        with patch.object(WebSocketGateway, "kline_live_ready", new_callable=PropertyMock, return_value=True):
            self.controller.complete_market_stream_reconciliation(self.controller._market_snapshot.version)
            self.controller.recover_interrupted_market_session()
        self.assertIs(self.controller.status, TradingSessionStatus.RUNNING)
        self.assertEqual(self.fixture.rest_client.submitted_orders, [])

    def _rows(self) -> dict[str, dict]:
        """
        함수 이름: _rows()
        기능: UI에 전달되는 실제 transport 지표를 조건 ID로 조회한다.
        인자: 없음
        반환값: 조건 ID별 지표 DTO
        작성 날짜: 2026/09/10
        """
        indicators = map_trading_snapshot(self.controller, "fake")["active_logic"]["indicators"]
        return {row["condition_id"]: row for row in indicators["conditions"]}

    def test_live_values_keep_original_basis_and_publish_original_remaining_time(self) -> None:
        """
        함수 이름: test_live_values_keep_original_basis_and_publish_original_remaining_time()
        기능: 9월 10일 실제 저점·복구·반등 값을 재생해 원 기준의 48초 판정과 단일 매수를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        self._observe("2452.65", "-0.1289018577731110722768614140103207")
        self.assertIs(self.controller._active_stm.current_state.case_c_signal_state, CaseCSignalState.C_WAIT_SETUP)
        self._observe("2451.01", "-0.1619154427931995575381995498211320", delay=timedelta(seconds=8))
        self.assertIsNone(self.controller.context.runtime.timer_base_time)
        self._observe("2445.36", "-0.2564185646046962868184779670093315", delay=timedelta(seconds=124))
        self._observe("2440.70", "-0.3157322977517897363818710868727719", delay=timedelta(seconds=2))
        self._observe("2439.98", "-0.3236635529213372712908670964037950", delay=timedelta(seconds=2))
        original = self.controller.context.runtime
        original_timer = self._rows()["c_recovery_window"]["timer"]

        self._recover(timedelta(seconds=32))
        results = self._observe("2442.70", "-0.2920547549500692436220628915022611")
        runtime = self.controller.context.runtime
        self.assertEqual(runtime.timer_base_time, original.timer_base_time)
        self.assertEqual(runtime.timer_base_pct_b, original.timer_base_pct_b)
        self.assertEqual(runtime.entry_pct_b, original.entry_pct_b)
        self.assertEqual(runtime.entry_pct_b, runtime.timer_base_pct_b + Decimal("0.06"))
        self.assertEqual(runtime.flush_low, original.flush_low)
        self.assertEqual(self.controller.context.market.case_c_timer_elapsed, timedelta(seconds=32))
        self.assertFalse(any("C-12" in result.transition_ids for result in results))
        rows = self._rows()
        self.assertEqual(rows["c_recovery_window"]["timer"]["timer_id"], original_timer["timer_id"])
        self.assertEqual(rows["c_recovery_window"]["timer"]["remaining_seconds"], "148")
        self.assertEqual(Decimal(rows["c_rebound"]["threshold"]), original.entry_pct_b)
        self.assertFalse(rows["c_rebound"]["satisfied"])
        self.assertEqual(self.fixture.rest_client.submitted_orders, [])

        results = self._observe("2444.98", "-0.2618221034394996491882431970337721", delay=timedelta(seconds=16))
        self.assertTrue(any("C-12" in result.transition_ids for result in results))
        self.assertEqual(self.controller.context.market.case_c_timer_elapsed, timedelta(seconds=48))
        self.assertIs(self.fixture.position.owner, StrategyType.CASE_C)
        self.assertEqual(len(self.fixture.rest_client.submitted_orders), 1)
        self._observe("2444.98", "-0.2618221034394996491882431970337721", delay=timedelta(seconds=1))
        self.assertEqual(len(self.fixture.rest_client.submitted_orders), 1)

    def test_repeated_recovery_does_not_extend_three_minute_window(self) -> None:
        """
        함수 이름: test_repeated_recovery_does_not_extend_three_minute_window()
        기능: 반복 장애의 시간을 누적하고 정확히 180초 이후에만 C-11로 기준 전체를 갱신한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        self._observe("2440", "-0.30")
        original = self.controller.context.runtime
        for cycle in range(1, 4):
            self._recover(timedelta(seconds=60))
            results = self._observe("2441", "-0.28")
            self.assertFalse(any("C-11" in result.transition_ids for result in results))
            self.assertEqual(self.controller.context.runtime.timer_base_time, original.timer_base_time)
            self.assertEqual(self.controller.context.runtime.timer_base_pct_b, Decimal("-0.30"))
            self.assertEqual(self._rows()["c_recovery_window"]["timer"]["remaining_seconds"], str(180 - 60 * cycle))
        results = self._observe("2441", "-0.28", delay=timedelta(microseconds=1))
        self.assertTrue(any("C-11" in result.transition_ids for result in results))
        runtime = self.controller.context.runtime
        self.assertEqual(runtime.timer_base_time, self.fixture.clock())
        self.assertEqual(runtime.timer_base_pct_b, Decimal("-0.28"))
        self.assertEqual(runtime.entry_pct_b, Decimal("-0.22"))
        self.assertEqual(runtime.flush_low_time, original.flush_low_time)
        self.assertEqual(self._rows()["c_recovery_window"]["timer"]["reset_reason"], "timeout")
        self.assertEqual(self._rows()["c_recovery_window"]["timer"]["remaining_seconds"], "180")
        self.assertEqual(self.fixture.rest_client.submitted_orders, [])

    def test_recovery_at_exact_deadline_can_buy(self) -> None:
        """
        함수 이름: test_recovery_at_exact_deadline_can_buy()
        기능: 복구 첫 관측이 정확히 180초이고 회복선에 도달하면 기존 기준으로 한 번 매수한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        self._observe("2440", "-0.30")
        self._recover(timedelta(seconds=180))
        results = self._observe("2445", "-0.24")
        self.assertTrue(any("C-12" in result.transition_ids for result in results))
        self.assertFalse(any("C-11" in result.transition_ids for result in results))
        self.assertEqual(self.controller.context.market.case_c_timer_elapsed, timedelta(seconds=180))
        self.assertEqual(len(self.fixture.rest_client.submitted_orders), 1)

    def test_recovery_just_before_deadline_waits_for_actual_rebound(self) -> None:
        """
        함수 이름: test_recovery_just_before_deadline_waits_for_actual_rebound()
        기능: 180초 직전에도 매수선에 못 미친 값으로 주문하지 않고 남은 1마이크로초를 표시한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        self._observe("2440", "-0.30")
        self._recover(timedelta(seconds=179, microseconds=999999))
        results = self._observe("2445", "-0.240001")
        self.assertFalse(any("C-11" in result.transition_ids or "C-12" in result.transition_ids for result in results))
        self.assertEqual(self._rows()["c_recovery_window"]["timer"]["remaining_seconds"], "0.000001")
        self.assertFalse(self._rows()["c_rebound"]["satisfied"])
        self.assertEqual(self.fixture.rest_client.submitted_orders, [])

    def test_recovery_after_deadline_cannot_buy_using_expired_entry(self) -> None:
        """
        함수 이름: test_recovery_after_deadline_cannot_buy_using_expired_entry()
        기능: 기존 매수선을 넘었어도 180초를 초과했으면 주문보다 C-11 재설정을 먼저 실행한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        self._observe("2440", "-0.30")
        self._recover(timedelta(seconds=180, microseconds=1))
        results = self._observe("2445", "-0.24")
        self.assertTrue(any("C-11" in result.transition_ids for result in results))
        self.assertFalse(any("C-12" in result.transition_ids for result in results))
        self.assertEqual(self.controller.context.runtime.timer_base_pct_b, Decimal("-0.24"))
        self.assertEqual(self.controller.context.runtime.entry_pct_b, Decimal("-0.18"))
        self.assertEqual(Decimal(self._rows()["c_rebound"]["threshold"]), Decimal("-0.18"))
        self.assertEqual(self.fixture.rest_client.submitted_orders, [])

    def test_new_low_after_long_outage_takes_priority_over_timeout(self) -> None:
        """
        함수 이름: test_new_low_after_long_outage_takes_priority_over_timeout()
        기능: 장애 중 3분이 지났어도 복구 시 더 낮은 가격이면 C-10으로 저점과 기준을 함께 갱신한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        self._observe("2440", "-0.30")
        self._recover(timedelta(seconds=240))
        results = self._observe("2439", "-0.33")
        self.assertTrue(any("C-10" in result.transition_ids for result in results))
        self.assertFalse(any("C-11" in result.transition_ids for result in results))
        runtime = self.controller.context.runtime
        self.assertEqual(runtime.flush_low, Decimal("2439"))
        self.assertEqual(runtime.flush_low_time, self.fixture.clock())
        self.assertEqual(runtime.timer_base_time, runtime.flush_low_time)
        self.assertEqual(runtime.timer_base_pct_b, Decimal("-0.33"))
        self.assertEqual(runtime.entry_pct_b, Decimal("-0.27"))
        self.assertEqual(self._rows()["c_recovery_window"]["timer"]["reset_reason"], "new_low")
        self.assertEqual(self.fixture.rest_client.submitted_orders, [])

    def test_setup_before_first_flush_does_not_gain_a_timer_on_recovery(self) -> None:
        """
        함수 이름: test_setup_before_first_flush_does_not_gain_a_timer_on_recovery()
        기능: 최초 flush 없는 SETUP은 긴 장애 뒤에도 타이머를 만들지 않고 C-09에서만 시작한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        self._observe("2451", "-0.20")
        self._recover(timedelta(seconds=240))
        self._observe("2450", "-0.21")
        runtime = self.controller.context.runtime
        self.assertIsNone(runtime.timer_base_time)
        self.assertIsNone(runtime.timer_base_pct_b)
        self.assertIsNone(runtime.entry_pct_b)
        results = self._observe("2445", "-0.26", delay=timedelta(seconds=1))
        self.assertTrue(any("C-09" in result.transition_ids for result in results))
        self.assertEqual(self.controller.context.runtime.entry_pct_b, Decimal("-0.20"))
        self.assertEqual(self.controller.context.runtime.timer_base_time, self.fixture.clock())
        self.assertEqual(self.fixture.rest_client.submitted_orders, [])

    def test_full_recovery_ends_setup_before_expired_timer_or_buy(self) -> None:
        """
        함수 이름: test_full_recovery_ends_setup_before_expired_timer_or_buy()
        기능: 복구 첫 %B가 0.25 이상이면 타이머 만료와 반등 매수보다 C-08 종료를 우선한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        self._observe("2440", "-0.30")
        self._recover(timedelta(seconds=240))
        results = self._observe("2470", "0.25")
        self.assertTrue(any("C-08" in result.transition_ids for result in results))
        self.assertFalse(any("C-11" in result.transition_ids or "C-12" in result.transition_ids for result in results))
        self.assertIs(self.controller._active_stm.current_state.case_c_signal_state, CaseCSignalState.CASE_C_FINAL_STATE)
        self.assertEqual(self.fixture.rest_client.submitted_orders, [])

    def test_unavailable_recovery_value_cannot_replace_valid_basis(self) -> None:
        """
        함수 이름: test_unavailable_recovery_value_cannot_replace_valid_basis()
        기능: 복구 첫 %B가 계산 불가이면 기존 기준을 보존하고 유효한 다음 평가에서만 만료 처리한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        self._observe("2440", "-0.30")
        original = self.controller.context.runtime
        self._recover(timedelta(seconds=240))
        self._observe("2445", "NaN")
        runtime = self.controller.context.runtime
        self.assertEqual(runtime.timer_base_time, original.timer_base_time)
        self.assertEqual(runtime.timer_base_pct_b, original.timer_base_pct_b)
        self.assertEqual(runtime.entry_pct_b, original.entry_pct_b)
        self.assertEqual(self.fixture.rest_client.submitted_orders, [])
        results = self._observe("2445", "-0.24", delay=timedelta(seconds=1))
        self.assertTrue(any("C-11" in result.transition_ids for result in results))
        self.assertEqual(self.controller.context.runtime.entry_pct_b, Decimal("-0.18"))
        self.assertEqual(self.fixture.rest_client.submitted_orders, [])
