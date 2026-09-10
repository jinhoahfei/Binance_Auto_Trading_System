"""TradingSTM의 실행 전략이 스냅샷과 실시간 event에 동일하게 공개되는지 검증한다."""

import asyncio
from dataclasses import replace
from decimal import Decimal
import unittest

from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.trading.context import (
    MarketEvaluationSnapshot,
    PendingOrderSnapshot,
    PositionSnapshot,
)
from binance_auto_trader.domain.trading.states import (
    CaseBSignalState,
    OrderAttemptKind,
    OrderSide,
    StrategyType,
)
from binance_auto_trader.domain.trading.action_requests import SubmitOrder
from binance_auto_trader.transport import (
    BackendEventStream,
    create_trading_session_update_observer,
)
from binance_auto_trader.transport.contracts import map_trading_snapshot
from tests.integration.test_trading_session_flow import _create_ready_controller


class ActiveTradingLogicFlowTests(unittest.TestCase):
    """
    클래스 이름: ActiveTradingLogicFlowTests
    기능: 외부 주문 없이 실제 session·시장 전이·주문 및 포지션 snapshot의 전략 표시를 검증한다.
    작성 날짜: 2026/09/05
    """

    def test_realtime_touch_starts_tracking_and_freezes_touch_bandwidth(self) -> None:
        """
        함수 이름: test_realtime_touch_starts_tracking_and_freezes_touch_bandwidth()
        기능: 봉 저가만의 접촉은 무시하고 실시간 접촉 즉시 추적하며 터치 순간 BBW를 고정한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        for bandwidth, enables_b in (("0.01999999", True), ("0.02", False), ("0.02000001", False)):
            for touch_price in ("100", "99.99"):
                with self.subTest(bandwidth=bandwidth, touch_price=touch_price):
                    controller, regime_controller, _ = _create_ready_controller()
                    self.addCleanup(controller.close_session_resources)
                    selection = regime_controller.set_regime_type(
                        RegimeType.TYPE_0, command_id="select-touch", expected_version=0,
                    )
                    controller.start_trading(command_id="start-touch", expected_version=selection.version)
                    market = MarketEvaluationSnapshot(
                        realtime_price=Decimal("101"), lower_band=Decimal("100"),
                        upper_band=Decimal("102"), current_30m_low=Decimal("99"),
                        current_30m_candle_id="closed", confirmed_30m_close=True,
                        touch_candle_bbw=Decimal(bandwidth),
                    )
                    inputs = (
                        market,
                        replace(market, realtime_price=Decimal("100.5")),
                        replace(market, realtime_price=Decimal(touch_price), confirmed_30m_close=False,
                                current_30m_candle_id="touch"),
                        replace(market, realtime_price=Decimal("101"), confirmed_30m_close=False,
                                current_30m_candle_id="touch",
                                touch_candle_bbw=Decimal("0.03") if enables_b else Decimal("0.01")),
                    )
                    for index, candidate in enumerate(inputs):
                        controller._market_snapshot.update(controller._market_snapshot.klines_by_interval)
                        controller.observe_market_evaluation(
                            candidate, source_event_id=f"touch-{index}",
                            market_version=controller._market_snapshot.version,
                        )
                        results = asyncio.run(controller.drain_events())
                        self.assertFalse(any(isinstance(action, SubmitOrder)
                                             for result in results for action in result.action_requests))
                        logic = map_trading_snapshot(controller, "fake")["active_logic"]
                        if index < 2:
                            self.assertEqual(logic["root_state"], "LOWER_TOUCH_WATCH")
                            rows = logic["indicators"]["conditions"]
                            self.assertEqual([row["condition_id"] for row in rows], ["lower_price"])
                            self.assertEqual(Decimal(rows[0]["value"]), candidate.realtime_price)
                            self.assertFalse(rows[0]["satisfied"])
                        else:
                            self.assertEqual(logic["root_state"], "TRADE_MANAGEMENT")
                            self.assertEqual(logic["active_strategies"],
                                             ["CASE_B", "CASE_C"] if enables_b else ["CASE_C"])
                            self.assertEqual(controller.context.runtime.touch_candle_bbw, Decimal(bandwidth))
                            self.assertEqual(controller.context.runtime.touch_candle_id, "touch")
                            if enables_b:
                                self.assertIs(controller._active_stm.current_state.case_b_signal_state,
                                              CaseBSignalState.B_WAIT_SIGNAL)
                            indicators = logic["indicators"]
                            phases = {phase["strategy"]: phase for phase in indicators["phases"]}
                            self.assertEqual(phases["CASE_B"]["phase"], "B_WAIT_SIGNAL" if enables_b else "CASE_B_FINAL_STATE")
                            self.assertEqual(phases["CASE_C"]["phase"], "C_WAIT_SETUP")
                            case_b_rows = [row["condition_id"] for row in indicators["conditions"] if row["strategy"] == "CASE_B"]
                            self.assertEqual(case_b_rows, ["b_signal_slope", "b_signal_pct_b", "b_signal_low"] if enables_b else [])
                            self.assertNotIn("b_touch_bbw", case_b_rows)

    def test_case_b_signal_observation_continues_during_case_c_position(self) -> None:
        """
        함수 이름: test_case_b_signal_observation_continues_during_case_c_position()
        기능: C 체결 뒤 B의 확정봉 평가·단계 표시는 유지하되 추가 매수는 차단한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        from tempfile import TemporaryDirectory
        from tests.integration.test_buy_sell_flow import _create_buy_flow_fixture, FakeOrderScenario
        from tests.integration.active_trading_logic_replay import replay_market_evaluation

        with TemporaryDirectory() as directory:
            fixture = _create_buy_flow_fixture(directory, FakeOrderScenario.IMMEDIATE_FILLED)
            controller = fixture.controller
            self.addCleanup(controller.close_session_resources)
            market = MarketEvaluationSnapshot(
                realtime_price=Decimal("4320"), lower_band=Decimal("4350"), upper_band=Decimal("4500"),
                realtime_pct_b=Decimal("-0.30"), current_30m_candle_id="parallel-touch",
                touch_candle_bbw=Decimal("0.01"), cci_30m_realtime=Decimal("-150"),
            )
            replay_market_evaluation(fixture, market, "parallel-touch")
            entry = replay_market_evaluation(fixture, replace(market,
                realtime_price=Decimal("4327"), realtime_pct_b=Decimal("-0.24")), "c-entry")
            self.assertTrue(any("C-12" in result.transition_ids for result in entry))
            self.assertIs(fixture.position.owner, StrategyType.CASE_C)
            failed_signal = replace(market, realtime_price=Decimal("4400"), realtime_pct_b=Decimal("0.09"),
                confirmed_30m_close=True, pct_b_close=Decimal("0.5"), ema_slope_30m_close=Decimal("-0.04"),
                current_closed_candle_low=Decimal("4360"),
                previous_3_closed_candle_lows=(Decimal("4310"), Decimal("4320"), Decimal("4330")))
            replay_market_evaluation(fixture, failed_signal, "b-close-during-c")
            snapshot = map_trading_snapshot(controller, "fake")["active_logic"]["indicators"]
            case_b = next(phase for phase in snapshot["phases"] if phase["strategy"] == "CASE_B")
            self.assertEqual(case_b, {"strategy": "CASE_B", "phase": "B_WAIT_SIGNAL", "notice": "entry_paused"})
            slope = next(row for row in snapshot["conditions"] if row["condition_id"] == "b_signal_slope")
            self.assertEqual(slope["value"], "-0.04")
            self.assertFalse(slope["satisfied"])
            replay_market_evaluation(fixture, replace(failed_signal, confirmed_30m_close=False), "b-intrabar-during-c")
            rows = map_trading_snapshot(controller, "fake")["active_logic"]["indicators"]["conditions"]
            self.assertEqual(slope, next(row for row in rows if row["condition_id"] == "b_signal_slope"))
            results = replay_market_evaluation(fixture, replace(failed_signal, ema_slope_30m_close=Decimal("0")), "b-signal-during-c")
            self.assertTrue(any("B-05" in result.transition_ids for result in results))
            self.assertFalse(any(isinstance(action, SubmitOrder) for result in results for action in result.action_requests))
            snapshot = map_trading_snapshot(controller, "fake")["active_logic"]["indicators"]
            self.assertEqual([row["condition_id"] for row in snapshot["conditions"] if row["strategy"] == "CASE_B"],
                             ["b_pullback", "b_signal_age"])
            self.assertEqual(next(phase for phase in snapshot["phases"] if phase["strategy"] == "CASE_B")["phase"], "B_WAIT_PULLBACK")

    def test_actual_recovery_timer_transitions_and_publications(self) -> None:
        """
        함수 이름: test_actual_recovery_timer_transitions_and_publications()
        기능: 실제 C-10·C-11 재생의 첫 publication이 새 회차와 180초를 함께 보내는지 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/05
        """
        from tests.integration.active_trading_logic_replay import replay_recovery_timer_scenario

        # 외부 주문 없이 수행한 실제 STM 결과를 UI와 같은 replay DTO로 검사한다.
        report = replay_recovery_timer_scenario()
        self.assertEqual(report["fake_order_count"], 0)
        steps = report["steps"]
        previous_id = None
        for step in steps[:-1]:
            rows = step["event"]["payload"]["trading"]["active_logic"]["indicators"]["conditions"]
            row = next(row for row in rows if row["condition_id"] == step["timer_condition_id"])
            if any(transition in step["transition_ids"] for transition in ("C-10", "C-11")):
                self.assertNotEqual(row["timer"]["timer_id"], previous_id)
                self.assertEqual(row["timer"]["remaining_seconds"], "180")
                self.assertIsNone(row["satisfied"])  # 이전 회차의 실패 색상을 즉시 해제한다.
            previous_id = row["timer"]["timer_id"]
        self.assertIsNone(steps[-1]["event"]["payload"]["trading"]["active_logic"])

    def test_selection_alone_never_publishes_an_active_strategy(self) -> None:
        """
        함수 이름: test_selection_alone_never_publishes_an_active_strategy()
        기능: 다섯 REGIME의 선택만으로 실행 Case가 만들어지지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/05
        """
        # 지원 여부와 무관하게 선택은 시작 명령과 별개의 동작임을 확인한다.
        for regime_type in RegimeType:
            with self.subTest(regime_type=regime_type):
                controller, regime_controller, _ = _create_ready_controller()
                regime_controller.set_regime_type(
                    regime_type,
                    command_id=f"select-{regime_type.value}",
                    expected_version=0,
                )
                self.assertIsNone(
                    map_trading_snapshot(controller, "fake")["active_logic"]
                )  # 미지원 REGIME에도 TYPE_0의 Case를 대체 표시하지 않는다.

    def test_market_entry_owners_and_stop_publish_current_strategy(self) -> None:
        """
        함수 이름: test_market_entry_owners_and_stop_publish_current_strategy()
        기능: 하단 대기·병렬 감시·단일 Case·종료가 현재 상태에 따라 공개되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/05
        """
        # BBW에 따라 Case B가 활성화되거나 비활성화되는 실제 G-02 경로를 각각 실행한다.
        for bandwidth, expected_strategies in (
            ("0.01", ["CASE_B", "CASE_C"]),
            ("0.03", ["CASE_C"]),
        ):
            with self.subTest(bandwidth=bandwidth):
                controller, regime_controller, _ = _create_ready_controller()
                self.addCleanup(controller.close_session_resources)
                selection = regime_controller.set_regime_type(
                    RegimeType.TYPE_0,
                    command_id="select-active-logic",
                    expected_version=0,
                )
                controller.start_trading(
                    command_id="start-active-logic",
                    expected_version=selection.version,
                )
                waiting_logic = map_trading_snapshot(controller, "fake")["active_logic"]
                self.assertEqual({key: value for key, value in waiting_logic.items() if key != "indicators"}, {
                    "regime_type": "type0",
                    "root_state": "LOWER_TOUCH_WATCH",
                    "active_strategies": [],
                })
                self.assertEqual(
                    [row["condition_id"] for row in waiting_logic["indicators"]["conditions"]],
                    ["lower_price"],
                )  # 시작 직후에는 이전 전략의 지표를 재사용하지 않는다.
                self.assertTrue(all(row["satisfied"] is None for row in waiting_logic["indicators"]["conditions"]))

                # 실제 시장 입력을 queue에서 처리해 Case enable flag와 신호 Region을 함께 전이시킨다.
                controller.observe_market_evaluation(
                    MarketEvaluationSnapshot(
                        realtime_price=Decimal("90"),
                        lower_band=Decimal("100"),
                        upper_band=Decimal("120"),
                        realtime_pct_b=Decimal("-0.5"),
                        current_30m_candle_id="ETHUSDT:30m:2026-09-05T00:00:00Z",
                        current_30m_low=Decimal("89"),
                        current_30m_high=Decimal("101"),
                        touch_candle_bbw=Decimal(bandwidth),
                    ),
                    source_event_id="active-logic-lower-touch",
                    market_version=controller._market_snapshot.version,
                )
                asyncio.run(controller.process_next_event())
                signal_snapshot = controller.snapshot_session()
                signal_logic = map_trading_snapshot(controller, "fake")["active_logic"]
                self.assertEqual(signal_logic["active_strategies"], expected_strategies)
                self.assertEqual(signal_logic["root_state"], "TRADE_MANAGEMENT")

                # 주문 대기와 체결 후 소유권은 마지막 체결 목록 없이도 해당 Case를 특정해야 한다.
                observer = create_trading_session_update_observer(BackendEventStream())
                for strategy in (StrategyType.CASE_B, StrategyType.CASE_C):
                    controller.update_pending_order_snapshot(PendingOrderSnapshot(
                        order_id=f"pending-{strategy.value}",
                        strategy=strategy,
                        side=OrderSide.BUY,
                        attempt_kind=OrderAttemptKind.INITIAL,
                    ))
                    pending_event = observer(controller, "fake")
                    self.assertEqual(
                        pending_event.payload["trading"]["active_logic"]["active_strategies"],
                        [strategy.value],
                    )
                    controller.update_pending_order_snapshot(None)
                    controller.update_position_snapshot(
                        PositionSnapshot(quantity=Decimal("1"), entry_price=Decimal("90")),
                        owner=strategy,
                    )
                    owner_snapshot = map_trading_snapshot(controller, "fake")
                    owner_event = observer(controller, "fake")
                    self.assertEqual(owner_event.payload["trading"], owner_snapshot)
                    self.assertEqual(
                        owner_snapshot["active_logic"]["active_strategies"],
                        [strategy.value],
                    )
                    controller.update_position_snapshot(PositionSnapshot())

                # 종료 시 과거 Case를 지우되 이미 발행된 frozen snapshot은 변하지 않아야 한다.
                controller.stop_trading(
                    command_id="stop-active-logic",
                    expected_version=controller.context.version,
                )
                terminated_event = observer(controller, "fake")
                self.assertIsNone(terminated_event.payload["trading"]["active_logic"])
                self.assertEqual(
                    tuple(strategy.value for strategy in signal_snapshot.active_logic.active_strategies),
                    tuple(expected_strategies),
                )  # 후속 상태 전이가 이전 publication을 소급 변경하지 않는다.
