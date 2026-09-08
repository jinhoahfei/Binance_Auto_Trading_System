"""Event-Action Table 감사. production/명세 수정 및 외부 주문 없이 불일치를 재현한다."""

import asyncio
import re
import unittest
from dataclasses import replace
from datetime import timedelta
from tempfile import TemporaryDirectory

import lower_bb_spec_audit as prior
from binance_auto_trader.domain.trading.action_requests import (
    CancelPendingOrder, CloseLowerEvent, OpenLowerEvent, PatchRuntimeContext,
    ReconcileOrder, ResetCaseBContext, ResetCaseCContext, patch,
)
from binance_auto_trader.domain.trading.context import TradingContext, TradingRuntimeSnapshot
from binance_auto_trader.domain.trading.states import ExitReason, OrderSide, OrderAttemptKind
from binance_auto_trader.domain.trading.transitions.catalog import TRANSITION_IDS

D, E, NOW = prior.D, prior.E, prior.NOW


class EventActionAudit(unittest.TestCase):
    def test_control_all_109_table_ids_exist(self):
        text = (prior.ROOT / "Design/Trading_Logic/Event_Action_Table/Trading_Logic_Event_Action_Table.md").read_text()
        ids = re.findall(r"^\| ((?:G|O|PB|PC|B|C)-\d{2}[A-Z]?) \|", text, re.M)
        self.assertEqual(109, len(ids))
        self.assertEqual(set(ids), set(TRANSITION_IDS))

    def test_control_pb15_holds_before_common_exit_deadline(self):
        result = prior.b_stm(prior.BP.CASE_B_TREND_HOLD).handle(
            prior.TradingEvent(event_type=E.MARKET_DATA_UPDATED, occurred_at=NOW),
            prior.context(prior.market(realtime_pct_b=D("0.7"), realtime_ema_slope=D("0.09"),
                                       holding_elapsed=timedelta(hours=5))),
        )
        self.assertEqual(("PB-15",), result.transition_ids)

    def test_control_g07_pending_precedes_position(self):
        view = prior.context(prior.market(realtime_price=D("110")),
                             TradingRuntimeSnapshot(position_owner=prior.S.CASE_B,
                                                    pending_strategy=prior.S.CASE_B,
                                                    pending_order_attempt_kind=OrderAttemptKind.INITIAL,
                                                    pending_order_id="pending", pending_order_side=OrderSide.SELL))
        result = prior.b_stm().handle(prior.TradingEvent(event_type=E.UPPER_BAND_TOUCHED, occurred_at=NOW), view)
        self.assertEqual(("G-07",), result.transition_ids)
        self.assertEqual(["PatchRuntimeContext", "CancelPendingOrder", "ReconcileOrder"],
                         [type(a).__name__ for a in result.action_requests])

    def test_ea01_close_event_must_reach_pb05_stop_check(self):
        prior.LowerBBSpecAudit().test_lower_touch_close_must_not_hide_confirmed_stop()

    def test_ea02_pc06_three_minutes_must_survive_normal_rollover(self):
        prior.LowerBBSpecAudit().test_continuous_stop_timer_must_cross_thirty_minute_boundary()

    def test_ea03_pc27_must_not_use_request_pct_b_as_fill_pct_b(self):
        from tests.integration.test_public_market_case2_flow import (
            PublicCase2OrderScenario, _create_public_case2_fixture, _trigger_public_case_c_buy,
            _create_live_thirty_minute_kline, _create_closed_one_minute_kline,
        )
        with TemporaryDirectory() as directory:
            fixture = _create_public_case2_fixture(directory, PublicCase2OrderScenario.SELL_PARTIAL_THEN_FILLED)
            original_build_fills = fixture.rest_client._build_fills

            def build_fills_at_observed_market(order, exchange_order_id):
                fills = original_build_fills(order, exchange_order_id)
                if order.side is not OrderSide.SELL:
                    return fills
                # 기존 fixture의 고정 과거 fill 시각을 사용하지 않는다.
                # 이미 관측한 partial은 보존하고 새 fill은 실제 fake 관측시각/시장가격으로 만든다.
                known = {f.trade_id: f for f in order.fills}
                return tuple(known.get(f.trade_id) or replace(
                    f, executed_at=fixture.clock(),
                    price=fixture.trading_controller.context.market.realtime_price) for f in fills)

            fixture.rest_client._build_fills = build_fills_at_observed_market
            _trigger_public_case_c_buy(fixture)
            fixture.clock.advance(timedelta(seconds=5))
            fixture.market_controller.observe_kline(_create_live_thirty_minute_kline(
                close_price=D("101"), candle_low=D("84"), event_time=fixture.clock()))
            asyncio.run(fixture.trading_controller.drain_events())
            fixture.clock.advance(timedelta(seconds=35))
            fixture.market_controller.observe_kline(_create_closed_one_minute_kline(
                close_price=D("90"), event_time=fixture.clock()))
            asyncio.run(fixture.trading_controller.drain_events())
            requested_pct_b = fixture.rest_client.submitted_orders[1].exit_pct_b_at_intent
            self.assertLess(requested_pct_b, D("0.40"))
            fixture.clock.advance(timedelta(seconds=5))
            fixture.market_controller.observe_kline(_create_live_thirty_minute_kline(
                close_price=D("115"), candle_low=D("84"), event_time=fixture.clock()))
            asyncio.run(fixture.trading_controller.drain_events())
            terminal_market_pct_b = fixture.trading_controller.context.market.realtime_pct_b
            self.assertGreaterEqual(terminal_market_pct_b, D("0.40"))
            fixture.trading_controller.trigger_order_reconciliation(
                occurred_at=fixture.clock.advance(timedelta(seconds=1)))
            self.assertEqual(fixture.clock(), max(
                f.executed_at for f in fixture.rest_client.submitted_orders[1].fills))
            results = asyncio.run(fixture.trading_controller.drain_events())
            ids = tuple(t for r in results for t in r.transition_ids)
            self.assertNotIn("PC-27", ids,
                             f"request={requested_pct_b}, terminal_market={terminal_market_pct_b}, "
                             f"published={fixture.trading_controller.context.runtime.case_c_exit_pct_b}")

    def test_ea04_g03_must_receive_live_new_candle_touch(self):
        prior.LowerBBSpecAudit().test_recovered_case_c_must_rearm_on_next_realtime_lower_touch()

    def test_ea05_g06p_pending_sell_should_be_reconciled_without_cancel(self):
        view = prior.context(prior.market(), TradingRuntimeSnapshot(
            position_owner=prior.S.CASE_B, pending_strategy=prior.S.CASE_B,
            pending_order_attempt_kind=OrderAttemptKind.INITIAL,
            pending_order_id="pending-sell", pending_order_side=OrderSide.SELL))
        result = prior.b_stm().handle(prior.TradingEvent(event_type=E.STOP_CONFIRMED, occurred_at=NOW), view)
        self.assertEqual(("G-06P",), result.transition_ids)
        self.assertTrue(any(isinstance(a, ReconcileOrder) for a in result.action_requests))
        self.assertFalse(any(isinstance(a, CancelPendingOrder) for a in result.action_requests))

    def test_ea06_b05_signal_time_should_be_candle_close_not_delivery(self):
        delivered_at = NOW + timedelta(seconds=2)
        stm = prior.TradingSTM(prior.RegimeType.TYPE_0)
        stm._state = prior.TradingStateConfiguration(
            root_state=prior.R.TRADE_MANAGEMENT, ownership_state=prior.O.NO_POSITION,
            case_b_signal_state=prior.BS.B_WAIT_SIGNAL, case_c_signal_state=prior.CS.CASE_C_FINAL_STATE)
        view = prior.context(prior.market(
            confirmed_30m_close=True, confirmed_30m_close_time=NOW,
            ema_slope_30m_close=D("0.01"), pct_b_close=D("0.4"),
            current_closed_candle_low=D("100"), previous_3_closed_candle_lows=(D("99"), D("99"), D("99"))),
            TradingRuntimeSnapshot())
        event = prior.TradingEvent(event_type=E.THIRTY_MINUTE_CANDLE_CLOSED, occurred_at=delivered_at,
                                  candle_id="ETHUSDT:30m:2026-09-09T00:00:00Z")
        result = stm.handle(event, view)
        self.assertEqual(("B-05",), result.transition_ids)
        changes = {c.field.value: c.value for a in result.action_requests
                   if isinstance(a, PatchRuntimeContext) for c in a.changes}
        self.assertEqual(NOW, changes["signal_time"])

    def test_ea07_pb23_must_preserve_one_setup_per_candle(self):
        ctx = TradingContext(clock=lambda: NOW)
        ctx.update_market(prior.market(realtime_price=D("89"), current_30m_low=D("89"),
                                      realtime_pct_b=D("-0.3"), cci_30m_realtime=D("-150")))
        ctx.open_lower_event(OpenLowerEvent(
            lower_event_id="old-event", candle_id="new-candle", touch_time=NOW,
            touch_candle_low=D("89"), lower_band_at_touch=D("90"), touch_candle_bbw=D("0.03")))
        ctx.apply_runtime_patch(patch(
            last_case_c_setup_candle_id="new-candle", case_b_exit_reason=ExitReason.STOP))
        stm = prior.b_stm(prior.BP.CASE_B_CLOSED)
        result = stm.handle(prior.TradingEvent(event_type=E.CASE_B_SELL_FINISHED, occurred_at=NOW), ctx.snapshot())
        self.assertIn("PB-23", result.transition_ids)
        # production Context 메서드로 STM이 반환한 mutation을 순서대로 적용한다.
        handlers = {
            PatchRuntimeContext: ctx.apply_runtime_patch, CloseLowerEvent: ctx.close_lower_event,
            OpenLowerEvent: ctx.open_lower_event, ResetCaseBContext: ctx.reset_case_b_context,
            ResetCaseCContext: ctx.reset_case_c_context,
        }
        for action in result.action_requests:
            handler = handlers.get(type(action))
            if handler is not None:
                handler(action)
        activation = stm.handle(prior.TradingEvent(event_type=E.ACTIVATE_TRADE_MANAGEMENT, occurred_at=NOW), ctx.snapshot())
        self.assertNotIn("C-03", activation.transition_ids,
                         f"same-candle previous setup ID became {ctx.runtime.last_case_c_setup_candle_id}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
