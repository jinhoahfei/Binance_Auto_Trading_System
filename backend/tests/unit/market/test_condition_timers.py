"""Production 시장 평가 경계에서 유지 타이머의 시작·중단·리셋을 검증한다."""

from datetime import timedelta
from decimal import Decimal
import unittest

from binance_auto_trader.domain.common import Interval
from binance_auto_trader.domain.market import MarketSnapshot
from tests.unit.market.test_thirty_minute_market_evaluation_builder import (
    BASE_CLOSED_PRICES, CURRENT_CANDLE_OPEN, ControlledConditionBuilder,
    MutableMonotonicClock, MutableUtcClock, commit_market_snapshot, make_kline,
)


class MarketConditionTimerTests(unittest.TestCase):
    """
    클래스 이름: MarketConditionTimerTests
    기능: Guard flag와 타이머 숫자가 동일한 실제 monotonic 경계를 사용하는지 확인한다.
    작성 날짜: 2026/09/05
    """

    def test_all_hold_timers_share_exact_boundaries_and_reset(self) -> None:
        """
        함수 이름: test_all_hold_timers_share_exact_boundaries_and_reset()
        기능: 다섯 연속 조건의 1ns 경계·중단·재시작·stream 초기화를 public tick으로 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/05
        """
        # 기대 조건은 명세에서 독립적으로 고정하고 표시 모듈의 mapping을 가져오지 않는다.
        cases = (
            ("b_profit_zone", "pct_b_at_least_060_for_5s", "0.60", "0", "0.59", "0", 5),
            ("b_trend_slope", "realtime_slope_above_008_for_5s", "0.5", "0.08000001", "0.5", "0.08", 5),
            ("b_trend_exit_slope", "realtime_slope_at_most_004_for_5s", "0.5", "0.04", "0.5", "0.04000001", 5),
            ("b_trend_exit_pct_b", "pct_b_below_060_for_5s", "0.59", "0", "0.60", "0", 5),
            ("c_stop", "realtime_slope_at_most_minus_055_for_3m", "0.5", "-0.55", "0.5", "-0.54999999", 180),
        )
        for condition_id, flag, pct_b, slope, outside_pct_b, outside_slope, duration in cases:
            with self.subTest(condition=condition_id):
                utc_clock = MutableUtcClock(CURRENT_CANDLE_OPEN + timedelta(minutes=4))
                monotonic_clock = MutableMonotonicClock()
                market = MarketSnapshot(clock=utc_clock)
                candle = make_kline(Interval.THIRTY_MINUTES, CURRENT_CANDLE_OPEN, Decimal("120"), closed=False)
                commit_market_snapshot(market, utc_clock, utc_clock(), BASE_CLOSED_PRICES, candle)
                builder = ControlledConditionBuilder(monotonic_clock)
                builder.rebase(market)

                def tick(nanoseconds: int, active: bool, *, rollover: bool = False):
                    """
                    함수 이름: tick()
                    기능: 유효한 새 시장 version에서 실제 builder의 유지 시간과 판정을 함께 읽는다.
                    인자: nanoseconds -> 단조 시각, active -> 즉시 조건 충족 여부, rollover -> 새 봉 여부
                    반환값: 전체 시장 평가와 대상 타이머
                    작성 날짜: 2026/09/05
                    """
                    builder.set_condition_values(Decimal(pct_b if active else outside_pct_b), Decimal(slope if active else outside_slope))
                    monotonic_clock.set_time(nanoseconds)
                    opened_at = CURRENT_CANDLE_OPEN + timedelta(minutes=30) if rollover else CURRENT_CANDLE_OPEN
                    observed_at = opened_at + timedelta(minutes=5) if rollover else utc_clock() + timedelta(seconds=1)
                    observed = make_kline(Interval.THIRTY_MINUTES, opened_at, Decimal("120"), closed=False, event_time=observed_at)
                    commit_market_snapshot(market, utc_clock, observed_at, BASE_CLOSED_PRICES, observed, observed_kline=observed)
                    result = builder(market, observed)
                    return result, next(timer for timer in result.condition_timers if timer.condition_id == condition_id)

                _, waiting = tick(0, False)
                self.assertEqual(waiting.state, "waiting")
                _, started = tick(1_000_000_000, True)
                self.assertEqual((started.state, started.remaining_seconds), ("running", Decimal(duration)))
                before, almost = tick((duration + 1) * 1_000_000_000 - 1, True)
                self.assertFalse(getattr(before, flag))
                self.assertEqual(almost.remaining_seconds, Decimal("0.000000001"))
                exact, completed = tick((duration + 1) * 1_000_000_000, True)
                self.assertTrue(getattr(exact, flag))
                self.assertEqual((completed.state, completed.remaining_seconds), ("completed", Decimal("0")))
                _, stopped = tick((duration + 2) * 1_000_000_000, False)
                self.assertEqual((stopped.state, stopped.remaining_seconds), ("stopped", Decimal(duration)))
                _, restarted = tick((duration + 3) * 1_000_000_000, True)
                self.assertNotEqual(restarted.timer_id, started.timer_id)
                self.assertEqual(restarted.remaining_seconds, Decimal(duration))

                # 정상 봉 교체는 같은 연속 조건을 유지하고 stream reset만 새 회차를 시작한다.
                _, next_candle = tick((duration + 4) * 1_000_000_000, True, rollover=True)
                self.assertEqual(next_candle.reset_reason, restarted.reset_reason)
                self.assertEqual(next_candle.timer_id, restarted.timer_id)
                self.assertEqual(next_candle.remaining_seconds, Decimal(duration - 1))
                builder.reset()
                builder.rebase(market)
                _, reconnected = tick((duration + 5) * 1_000_000_000, True, rollover=True)
                self.assertEqual(reconnected.reset_reason, "stream_reset")
                self.assertNotEqual(reconnected.timer_id, next_candle.timer_id)
                self.assertEqual(reconnected.remaining_seconds, Decimal(duration))
