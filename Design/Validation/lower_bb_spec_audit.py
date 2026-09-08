"""명세 기준의 독립 감사 재현. 네트워크/주문 실행 없음. 불일치는 FAIL로 표시한다."""

import sys
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "backend"))

from binance_auto_trader.application.market_evaluation_builder import ThirtyMinuteMarketEvaluationBuilder
from binance_auto_trader.application.trading_controller import TradingController
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.trading.action_requests import QueueEvent
from binance_auto_trader.domain.trading.context import (
    MarketEvaluationSnapshot, PositionSnapshot, TradingContextView, TradingRuntimeSnapshot,
)
from binance_auto_trader.domain.trading.events import TradingEvent, TradingEventType as E
from binance_auto_trader.domain.trading.states import (
    CaseBPositionState as BP, CaseBSignalState as BS, CaseCSignalState as CS,
    OwnershipState as O, RootState as R, StrategyType as S, TradingStateConfiguration,
)
from binance_auto_trader.domain.trading.stm import TradingSTM

NOW = datetime(2026, 9, 9, 0, 30, tzinfo=UTC)


def context(market, runtime=None):
    return TradingContextView(
        version=1, evaluated_at=NOW, market=market,
        runtime=runtime or TradingRuntimeSnapshot(
            position_owner=S.CASE_B, lower_event_id="old-event", touch_candle_id="old-candle",
        ),
        position=PositionSnapshot(quantity=D("1"), entry_price=D("100")),
    )


def b_stm(position_state=BP.CASE_B_HOLDING):
    stm = TradingSTM(RegimeType.TYPE_0)
    stm._state = TradingStateConfiguration(
        root_state=R.TRADE_MANAGEMENT, ownership_state=O.CASE_B_POSITION_MANAGEMENT,
        case_b_signal_state=BS.CASE_B_FINAL_STATE, case_c_signal_state=CS.CASE_C_FINAL_STATE,
        case_b_position_state=position_state,
    )
    return stm


def queued(result):
    return [a.event_type for a in result.action_requests if isinstance(a, QueueEvent)]


def market(**values):
    defaults = dict(
        realtime_price=D("100"), lower_band=D("90"), upper_band=D("110"),
        realtime_pct_b=D("0.5"), current_30m_candle_id="new-candle",
        current_30m_low=D("99"), current_30m_high=D("101"),
    )
    defaults.update(values)
    return MarketEvaluationSnapshot(**defaults)


class LowerBBSpecAudit(unittest.TestCase):
    def test_control_holding_time_exit_is_implemented(self):
        result = b_stm().handle(
            TradingEvent(event_type=E.MARKET_DATA_UPDATED, occurred_at=NOW),
            context(market(holding_elapsed=timedelta(hours=6))),
        )
        self.assertIn(E.CASE_B_TIME_EXIT, queued(result))

    def test_trend_hold_must_still_exit_after_six_hours(self):
        result = b_stm(BP.CASE_B_TREND_HOLD).handle(
            TradingEvent(event_type=E.MARKET_DATA_UPDATED, occurred_at=NOW),
            context(market(realtime_pct_b=D("0.7"), realtime_ema_slope=D("0.09"),
                           holding_elapsed=timedelta(hours=6))),
        )
        self.assertIn(E.CASE_B_TIME_EXIT, queued(result), str(result.transition_ids))

    def test_trend_hold_must_emergency_exit_without_waiting_five_seconds(self):
        result = b_stm(BP.CASE_B_TREND_HOLD).handle(
            TradingEvent(event_type=E.MARKET_DATA_UPDATED, occurred_at=NOW),
            context(market(realtime_price=D("98.9"), realtime_pct_b=D("0.445"))),
        )
        self.assertIn(E.CASE_B_EMERGENCY_STOP, queued(result), str(result.transition_ids))

    def test_lower_touch_close_must_not_hide_confirmed_stop(self):
        view = context(market(
            realtime_price=D("99.5"), lower_band=D("99.4"), upper_band=D("101.4"),
            realtime_pct_b=D("0.05"), current_30m_low=D("99.3"),
            confirmed_30m_close=True, ema_slope_30m_close=D("-0.09"),
        ))
        classifier = SimpleNamespace(_context=SimpleNamespace(runtime=view.runtime))
        event_type = TradingController._select_market_event_type(classifier, view.market)
        result = b_stm().handle(TradingEvent(event_type=event_type, occurred_at=NOW), view)
        # 같은 시장 값의 일반 MARKET 이벤트에서는 손절을 선택한다.
        control = b_stm().handle(TradingEvent(event_type=E.MARKET_DATA_UPDATED, occurred_at=NOW), view)
        self.assertIn(E.CASE_B_STOP, queued(control))
        self.assertIn(E.CASE_B_STOP, queued(result), f"classified={event_type}, transitions={result.transition_ids}")

    def test_continuous_stop_timer_must_cross_thirty_minute_boundary(self):
        from tests.unit.market.test_thirty_minute_market_evaluation_builder import (
            BASE_CLOSED_PRICES, CURRENT_CANDLE_OPEN, ControlledConditionBuilder,
            MutableMonotonicClock, MutableUtcClock, commit_market_snapshot, make_kline,
        )
        from binance_auto_trader.domain.common import Interval
        from binance_auto_trader.domain.market import MarketSnapshot

        monotonic_clock = MutableMonotonicClock()
        utc_clock = MutableUtcClock(CURRENT_CANDLE_OPEN)
        snapshot = MarketSnapshot(clock=utc_clock)
        builder = ControlledConditionBuilder(monotonic_clock)
        builder.set_condition_values(D("-0.2"), D("-0.6"))
        flag = "realtime_slope_at_most_minus_055_for_3m"
        # 실제 builder __call__과 MarketSnapshot을 사용한다. 두 조건 수치만 고정한다.
        # 12:28 최초 충족, 12:30 다음 봉에서도 동일 조건, 12:31 총 180초.
        for minute, seconds in ((28, 0), (30, 120), (31, 180)):
            opened = CURRENT_CANDLE_OPEN + timedelta(minutes=30 if minute >= 30 else 0)
            observed_at = CURRENT_CANDLE_OPEN + timedelta(minutes=minute)
            event = make_kline(Interval.THIRTY_MINUTES, opened, D("120"),
                               closed=False, event_time=observed_at)
            prices = (*BASE_CLOSED_PRICES, D("120")) if minute >= 30 else BASE_CLOSED_PRICES
            monotonic_clock.set_time(seconds * 1_000_000_000)
            commit_market_snapshot(snapshot, utc_clock, observed_at, prices, event, observed_kline=event)
            evaluation = builder(snapshot, event)
        self.assertTrue(getattr(evaluation, flag), "180 seconds continuous, but timer restarted at 120 seconds")

    def test_recovered_case_c_must_rearm_on_next_realtime_lower_touch(self):
        runtime = TradingRuntimeSnapshot(
            lower_event_id="old-event", touch_candle_id="old-candle",
            case_c_consumed_for_event=True, case_c_recovery_confirmed=True,
            case_b_only_until_next_lower_touch=True, allow_new_case_c_setup=False,
        )
        view = replace(context(market(
            realtime_price=D("89"), current_30m_low=D("89"), realtime_pct_b=D("-0.05"),
        ), runtime), position=PositionSnapshot())
        classifier = SimpleNamespace(_context=SimpleNamespace(runtime=runtime))
        event_type = TradingController._select_market_event_type(classifier, view.market)
        stm = TradingSTM(RegimeType.TYPE_0)
        stm._state = TradingStateConfiguration(
            root_state=R.TRADE_MANAGEMENT, ownership_state=O.NO_POSITION,
            case_b_signal_state=BS.B_WAIT_SIGNAL, case_c_signal_state=CS.CASE_C_FINAL_STATE,
        )
        result = stm.handle(TradingEvent(event_type=event_type, occurred_at=NOW), view)
        self.assertIn("G-03", result.transition_ids, f"classified={event_type}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
