"""체결 시점 BB 복원과 누락 이력의 보수적 처리를 검증한다."""

from datetime import timedelta
from decimal import Decimal, localcontext
import unittest

from binance_auto_trader.application.market_evaluation_builder import calculate_execution_pct_b
from binance_auto_trader.domain.common import Interval
from tests.unit.market.test_thirty_minute_market_evaluation_builder import (
    BASE_CLOSED_PRICES, CURRENT_CANDLE_OPEN, make_rebased_builder, make_kline, commit_market_snapshot,
)


class ExecutionPctBTests(unittest.TestCase):
    """
    클래스 이름: ExecutionPctBTests
    기능: 실제 체결가와 과거 봉 경계가 현재 시세·조회 시각과 분리되는지 확인한다.
    작성 날짜: 2026/09/09
    """

    def test_late_query_uses_execution_price_and_original_candle_history(self):
        """
        함수 이름: test_late_query_uses_execution_price_and_original_candle_history()
        기능: 다음 봉에서 늦게 조회해도 과거 체결 BB 결과가 바뀌지 않는지 독립 산식과 비교한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        builder, snapshot, clock = make_rebased_builder()
        executed_at = CURRENT_CANDLE_OPEN + timedelta(minutes=5)
        execution_price = Decimal("123")
        original = calculate_execution_pct_b(snapshot, executed_at, execution_price)
        with localcontext() as decimal_context:
            decimal_context.prec = 34
            prices = (*BASE_CLOSED_PRICES[-19:], execution_price)
            mean = sum(prices) / Decimal("20")
            deviation = (sum((price - mean) ** 2 for price in prices) / Decimal("20")).sqrt()
            lower = mean - Decimal("2") * deviation
            upper = mean + Decimal("2") * deviation
            expected = (execution_price - lower) / (upper - lower)
        self.assertEqual(expected, original)
        next_open = CURRENT_CANDLE_OPEN + timedelta(minutes=30)
        next_candle = make_kline(Interval.THIRTY_MINUTES, next_open, Decimal("160"), closed=False)
        commit_market_snapshot(snapshot, clock, next_open + timedelta(minutes=5),
                               (*BASE_CLOSED_PRICES, Decimal("120")), next_candle)
        self.assertEqual(original, calculate_execution_pct_b(snapshot, executed_at, execution_price))

    def test_missing_historical_window_and_zero_width_return_no_evidence(self):
        """
        함수 이름: test_missing_historical_window_and_zero_width_return_no_evidence()
        기능: 19개 연속 과거봉이나 유효 밴드 폭이 없으면 추정 %B를 반환하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        builder, snapshot, clock = make_rebased_builder()
        self.assertIsNone(calculate_execution_pct_b(snapshot, CURRENT_CANDLE_OPEN - timedelta(hours=1), Decimal("100")))
        flat = make_kline(Interval.THIRTY_MINUTES, CURRENT_CANDLE_OPEN, Decimal("100"), closed=False)
        commit_market_snapshot(snapshot, clock, clock(), (Decimal("100"),) * 20, flat)
        self.assertIsNone(calculate_execution_pct_b(snapshot, CURRENT_CANDLE_OPEN, Decimal("100")))

    def test_execution_at_next_boundary_uses_previous_candle_as_closed(self):
        """
        함수 이름: test_execution_at_next_boundary_uses_previous_candle_as_closed()
        기능: 정확한 30분 경계의 체결을 종료된 이전봉 대신 새 후보봉으로 계산한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        builder, snapshot, clock = make_rebased_builder()
        next_open = CURRENT_CANDLE_OPEN + timedelta(minutes=30)
        self.assertIsNone(calculate_execution_pct_b(snapshot, next_open, Decimal("130")))
        next_candle = make_kline(Interval.THIRTY_MINUTES, next_open, Decimal("160"), closed=False)
        commit_market_snapshot(snapshot, clock, next_open + timedelta(seconds=1),
                               (*BASE_CLOSED_PRICES, Decimal("120")), next_candle)
        self.assertIsNotNone(calculate_execution_pct_b(snapshot, next_open, Decimal("130")))

    def test_builder_preserves_exchange_close_boundary(self):
        """
        함수 이름: test_builder_preserves_exchange_close_boundary()
        기능: 지연 수신된 확정 30분봉에도 원본 마감 경계가 실린다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        builder, snapshot, clock = make_rebased_builder()
        close_time = CURRENT_CANDLE_OPEN + timedelta(minutes=30)
        source = make_kline(Interval.THIRTY_MINUTES, CURRENT_CANDLE_OPEN, Decimal("120"),
                            closed=True, event_time=close_time + timedelta(seconds=2))
        commit_market_snapshot(snapshot, clock, source.event_time, BASE_CLOSED_PRICES, source, observed_kline=source)
        self.assertEqual(close_time, builder(snapshot, source).confirmed_30m_close_time)
