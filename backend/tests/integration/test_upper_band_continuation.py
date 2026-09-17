"""상단 접촉 후 세션 유지·하단 재진입·기존 포지션 관리를 외부 연결 없이 검증한다."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from binance_auto_trader.application.trading_controller import TradingSessionStatus
from binance_auto_trader.domain.trading.action_requests import (
    CancelPendingOrder, ForceSellAll, ReconcileOrder, ReevaluationTrigger, StopTradingRuntime,
)
from binance_auto_trader.domain.trading.context import MarketEvaluationSnapshot
from binance_auto_trader.domain.trading.states import (
    CaseBPositionState, CaseBSignalState, CaseCPositionState, CaseCSignalState, TradingPhase,
)
from binance_auto_trader.transport import BackendEventStream, create_trading_session_update_observer
from binance_auto_trader.transport.contracts import map_trading_snapshot
from tests.integration.active_trading_logic_replay import replay_market_evaluation
from tests.integration.test_buy_sell_flow import FakeOrderScenario, _create_buy_flow_fixture


class UpperBandContinuationTests(unittest.TestCase):
    """
    클래스 이름: UpperBandContinuationTests
    기능: 실제 Controller·STM·event queue·표시 DTO에서 상단 no-op과 하단 연결을 검증한다.
    작성 날짜: 2026/09/17
    """

    def setUp(self) -> None:
        """
        함수 이름: setUp()
        기능: 네트워크를 차단하고 가짜 거래소·임시 저장소의 실행 세션을 준비한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/17
        """
        self.enterContext(patch("socket.create_connection", side_effect=AssertionError("external network forbidden")))
        directory = self.enterContext(TemporaryDirectory())
        self.fixture = _create_buy_flow_fixture(directory, FakeOrderScenario.NEW_THEN_FILLED)
        self.controller = self.fixture.controller
        self.addCleanup(self.controller.close_session_resources)
        self.session_id = self.controller.snapshot_session().session_id
        self.observer = create_trading_session_update_observer(BackendEventStream(clock=self.fixture.clock))
        # 이번 실제 장애의 최초 하단 접촉 수치를 입력하고 판단은 production STM이 수행한다.
        self.lower = MarketEvaluationSnapshot(
            realtime_price=Decimal("2378.06"), lower_band=Decimal("2379.255257312860646591250699812834"),
            upper_band=Decimal("2417.962742687139353408749300187166"),
            realtime_pct_b=Decimal("-0.03087922920601037800580434129727135"),
            touch_candle_bbw=Decimal("0.01613747191571394371383522715637772"),
            cci_30m_realtime=Decimal("-154.1297038031478241765695071397004"),
            current_30m_candle_id="ETHUSDT:30m:2026-09-16T17:30:00Z",
            current_30m_low=Decimal("2375.82"),
        )
        self.upper = replace(
            self.lower, realtime_price=Decimal("2419.61"),
            lower_band=Decimal("2382.013651422941852421671688544776"),
            upper_band=Decimal("2419.557348577058147578328311455224"),
            realtime_pct_b=Decimal("1.001402403783668911088296888302099"),
            cci_30m_realtime=Decimal("35.64354878039187924267094571741342"),
        )

    def _observe(self, market: MarketEvaluationSnapshot, name: str) -> tuple:
        """
        함수 이름: _observe()
        기능: 공개 시장 입력 경로로 처리하고 polling·실시간 게시의 실행 세션 일치를 검사한다.
        인자: market -> 재현할 평가값, name -> 입력 식별자
        반환값: 실제 STM 처리 결과
        작성 날짜: 2026/09/17
        """
        results = replay_market_evaluation(self.fixture, market, name)
        snapshot = map_trading_snapshot(self.controller, "fake")
        self.assertEqual("running", snapshot["status"])
        self.assertEqual(self.session_id, snapshot["session_id"])
        self.assertEqual(snapshot, self.observer(self.controller, "fake").payload["trading"])
        self.assertIsNotNone(snapshot["active_logic"])
        self.assertIsNotNone(snapshot["active_logic"]["indicators"])
        self.assertFalse(any(isinstance(action, (StopTradingRuntime, ForceSellAll, CancelPendingOrder, ReconcileOrder))
                             for result in results for action in result.action_requests))
        return results

    def _assert_both_watching(self) -> None:
        """
        함수 이름: _assert_both_watching()
        기능: B/C 상태와 UI 전송 지표가 모두 신규 진입 감시를 나타내는지 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/17
        """
        state = self.controller._active_stm.current_state
        self.assertIs(state.case_b_signal_state, CaseBSignalState.B_WAIT_SIGNAL)
        self.assertIs(state.case_c_signal_state, CaseCSignalState.C_WAIT_SETUP)
        logic = map_trading_snapshot(self.controller, "fake")["active_logic"]
        self.assertEqual(["CASE_B", "CASE_C"], logic["active_strategies"])
        self.assertEqual({"CASE_B", "CASE_C"}, {row["strategy"] for row in logic["indicators"]["conditions"] if row["strategy"]})

    def test_incident_upper_touch_then_repeated_lower_touches_keep_both_cases_running(self) -> None:
        """
        함수 이름: test_incident_upper_touch_then_repeated_lower_touches_keep_both_cases_running()
        기능: 실제 종료 수치와 같은 봉·다음 봉의 재접촉을 반복해 세션 유지 및 B/C 재활성화를 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/17
        """
        self._observe(self.lower, "incident-lower")
        self._assert_both_watching()
        for index, candle_id in enumerate((self.lower.current_30m_candle_id, "next-30m-candle")):
            old_lower_id = self.controller.context.runtime.lower_event_id
            results = self._observe(self.upper, f"incident-upper-{index}")
            self.assertIn("G-07", [tid for result in results for tid in result.transition_ids])
            self.assertIsNone(self.controller.context.runtime.lower_event_id)
            self.assertIs(self.controller.context.runtime.trading_phase, TradingPhase.IDLE)
            logic = map_trading_snapshot(self.controller, "fake")["active_logic"]
            self.assertEqual("LOWER_TOUCH_WATCH", logic["root_state"])
            self.assertEqual(["lower_price"], [row["condition_id"] for row in logic["indicators"]["conditions"]])
            self._observe(self.upper, f"repeated-upper-{index}")
            # 상단 위에서 계속 수신한 뒤 같은 30분봉 안의 재접촉도 새 이벤트로 받아야 한다.
            results = self._observe(replace(self.lower, current_30m_candle_id=candle_id), f"next-lower-{index}")
            self.assertIn("G-02", [tid for result in results for tid in result.transition_ids])
            self.assertNotEqual(old_lower_id, self.controller.context.runtime.lower_event_id)
            self._assert_both_watching()
        self.assertEqual([], self.fixture.rest_client.submitted_orders)
        self.assertFalse(self.fixture.position.quantity)

    def test_upper_touch_clears_setup_timers_and_next_touch_rechecks_bandwidth(self) -> None:
        """
        함수 이름: test_upper_touch_clears_setup_timers_and_next_touch_rechecks_bandwidth()
        기능: C setup·회복 타이머를 정리하고 다음 터치의 BBW로 B 활성 여부를 다시 판단한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/17
        """
        self._observe(replace(self.lower, realtime_pct_b=Decimal("-0.3")), "c-flush")
        self.assertIsNotNone(self.controller.context.runtime.timer_base_time)
        self._observe(self.upper, "upper-after-flush")
        runtime = self.controller.context.runtime
        for value in (runtime.signal_time, runtime.flush_low, runtime.timer_base_time, runtime.entry_pct_b,
                      runtime.last_case_c_setup_candle_id):
            self.assertIsNone(value)
        self.assertFalse(runtime.case_c_consumed_for_event)
        for trigger in ReevaluationTrigger:
            self.assertEqual((), self.controller.trigger_scheduled_evaluations(trigger, occurred_at=self.fixture.clock()))
        self._observe(replace(self.lower, touch_candle_bbw=Decimal("0.02")), "wide-next-touch")
        self.assertEqual(["CASE_C"], map_trading_snapshot(self.controller, "fake")["active_logic"]["active_strategies"])
        self.assertEqual([], self.fixture.rest_client.submitted_orders)

    def test_upper_touch_discards_old_b_signal_before_next_lower_entry_watch(self) -> None:
        """
        함수 이름: test_upper_touch_discards_old_b_signal_before_next_lower_entry_watch()
        기능: 이전 B 확정 신호를 지워 재접촉 후 새 신호 없이 눌림 매수를 하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/17
        """
        self._observe(self.lower, "b-old-lower")
        signal = replace(self.lower, realtime_price=Decimal("2390"), realtime_pct_b=Decimal("0.5"),
                         confirmed_30m_close=True, pct_b_close=Decimal("0.5"),
                         current_closed_candle_low=Decimal("2375"),
                         previous_3_closed_candle_lows=(Decimal("2370"), Decimal("2371"), Decimal("2372")))
        self._observe(signal, "b-old-signal")
        self.assertIs(self.controller._active_stm.current_state.case_b_signal_state, CaseBSignalState.B_WAIT_PULLBACK)
        self.assertTrue(self.controller.context.runtime.signal_created)
        self._observe(self.upper, "upper-after-b-signal")
        self.assertFalse(self.controller.context.runtime.signal_created)
        self.assertIsNone(self.controller.context.runtime.signal_time)
        self._observe(self.lower, "b-new-lower")
        self._observe(replace(self.lower, realtime_price=Decimal("2385"), realtime_pct_b=Decimal("0.2")), "pullback-without-new-signal")
        self._assert_both_watching()
        self.assertEqual([], self.fixture.rest_client.submitted_orders)

    def _enter_case(self, strategy: str) -> MarketEvaluationSnapshot:
        """
        함수 이름: _enter_case()
        기능: 실제 B 회복 신호·눌림 또는 C flush·반등으로 가짜 BUY 미결 주문을 만든다.
        인자: strategy -> CASE_B 또는 CASE_C
        반환값: 매수 조건의 시장 평가
        작성 날짜: 2026/09/17
        """
        market = replace(self.lower, realtime_price=Decimal("4320"), lower_band=Decimal("4350"),
                         upper_band=Decimal("4500"), realtime_pct_b=Decimal("-0.3"),
                         cci_30m_realtime=Decimal("-150") if strategy == "CASE_C" else Decimal("0"))
        self._observe(market, "entry-lower")
        if strategy == "CASE_B":
            market = replace(market, realtime_price=Decimal("4400"), realtime_pct_b=Decimal("0.5"),
                             confirmed_30m_close=True, pct_b_close=Decimal("0.5"),
                             current_closed_candle_low=Decimal("4360"),
                             previous_3_closed_candle_lows=(Decimal("4310"), Decimal("4320"), Decimal("4330")))
            self._observe(market, "b-signal")
        entry = replace(market, realtime_price=Decimal("4380") if strategy == "CASE_B" else Decimal("4327"),
                        realtime_pct_b=Decimal("0.2") if strategy == "CASE_B" else Decimal("-0.24"), confirmed_30m_close=False)
        self._observe(entry, "entry")
        self.assertEqual(1, len(self.fixture.rest_client.submitted_orders))
        return entry

    def test_upper_touch_preserves_pending_buy_and_order_completion(self) -> None:
        """
        함수 이름: test_upper_touch_preserves_pending_buy_and_order_completion()
        기능: 상단 접촉에도 미결 BUY를 취소하지 않고 같은 주문 체결을 정상 반영한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/17
        """
        entry = self._enter_case("CASE_B")
        pending = self.controller.context.pending_order
        lower_id = self.controller.context.runtime.lower_event_id
        self._observe(replace(entry, realtime_price=Decimal("4501"), realtime_pct_b=Decimal("1.01")), "upper-pending")
        self.assertEqual(pending, self.controller.context.pending_order)
        self.assertEqual(lower_id, self.controller.context.runtime.lower_event_id)
        self.assertEqual(1, len(self.fixture.rest_client.submitted_orders))
        self.fixture.clock.advance(timedelta(seconds=1))
        self.controller.trigger_order_reconciliation(occurred_at=self.fixture.clock())
        asyncio.run(self.controller.drain_events())
        self.assertGreater(self.fixture.position.quantity, Decimal("0"))
        self.assertIs(self.controller.status, TradingSessionStatus.RUNNING)

    def test_case_b_conditions_continue_above_upper_band(self) -> None:
        """
        함수 이름: test_case_b_conditions_continue_above_upper_band()
        기능: 상단 위에서도 B 추세 유지 조건을 처리하며 강제매도·세션 종료하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/17
        """
        entry = self._enter_case("CASE_B")
        self.fixture.clock.advance(timedelta(seconds=1))
        self.controller.trigger_order_reconciliation(occurred_at=self.fixture.clock())
        asyncio.run(self.controller.drain_events())
        quantity = self.fixture.position.quantity
        self._observe(replace(entry, realtime_price=Decimal("4501"), realtime_pct_b=Decimal("1.01"),
                              realtime_ema_slope=Decimal("0.09"), pct_b_at_least_060_for_5s=True,
                              realtime_slope_above_008_for_5s=True), "upper-b-trend")
        self.assertIs(self.controller._active_stm.current_state.case_b_position_state, CaseBPositionState.CASE_B_TREND_HOLD)
        self.assertEqual(quantity, self.fixture.position.quantity)
        self.assertEqual(1, len(self.fixture.rest_client.submitted_orders))

    def test_case_c_conditions_continue_above_upper_band(self) -> None:
        """
        함수 이름: test_case_c_conditions_continue_above_upper_band()
        기능: 상단 위에서도 C 익절 추적 진입을 처리하며 강제매도·세션 종료하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/17
        """
        entry = self._enter_case("CASE_C")
        self.fixture.clock.advance(timedelta(seconds=1))
        self.controller.trigger_order_reconciliation(occurred_at=self.fixture.clock())
        asyncio.run(self.controller.drain_events())
        quantity = self.fixture.position.quantity
        self._observe(replace(entry, realtime_price=Decimal("4501"), realtime_pct_b=Decimal("1.01")), "upper-c-trail")
        self.assertIs(self.controller._active_stm.current_state.case_c_position_state, CaseCPositionState.CASE_C_TP_TRAILING)
        self.assertEqual(quantity, self.fixture.position.quantity)
        self.assertEqual(1, len(self.fixture.rest_client.submitted_orders))

    def test_user_stop_still_terminates_after_upper_return(self) -> None:
        """
        함수 이름: test_user_stop_still_terminates_after_upper_return()
        기능: 자동 복귀 이후에도 명시적인 사용자 중지가 기존 종료 경로를 유지한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/17
        """
        self._observe(self.lower, "lower-before-stop")
        self._observe(self.upper, "upper-before-stop")
        result = self.controller.stop_trading(command_id="explicit-stop", expected_version=self.controller.context.version)
        self.assertIn("G-05", result.transition_ids)
        self.assertIs(self.controller.status, TradingSessionStatus.TERMINATED)
        self.assertEqual([], self.fixture.rest_client.submitted_orders)


if __name__ == "__main__":
    unittest.main()
