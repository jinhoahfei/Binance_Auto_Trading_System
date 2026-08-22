"""장시간 실행 중 KST account-day rollover의 Performance 갱신을 검증한다."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from binance_auto_trader.domain.history import Performance
from binance_auto_trader.domain.trading.states import OrderSide

from tests.unit.history.factories import make_trade


@dataclass(slots=True)
class MutableUtcClock:
    """
    클래스 이름: MutableUtcClock
    기능: 하나의 Performance 수명 중 KST 날짜 변경을 결정론적으로 제공한다.
    작성 날짜: 2026/08/22
    """

    current_time: datetime

    def __call__(self) -> datetime:
        """
        함수 이름: __call__()
        기능: 현재 timezone-aware UTC 시각을 반환한다.
        인자: 없음
        반환값: 현재 UTC datetime
        작성 날짜: 2026/08/22
        """
        return self.current_time  # 실제 sleep 없이 같은 instance의 날짜를 이동시킨다.

    def set(self, current_time: datetime) -> None:
        """
        함수 이름: set()
        기능: 다음 Performance 조회가 관찰할 UTC 시각을 교체한다.
        인자: current_time -> timezone-aware UTC 시각
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        if not isinstance(current_time, datetime):
            raise TypeError("current_time must be a datetime")
        if current_time.tzinfo is None or current_time.utcoffset() != timedelta(0):
            raise ValueError("current_time must be timezone-aware UTC")

        self.current_time = current_time.astimezone(timezone.utc)  # KST 변환 전 UTC를 고정한다.


class PerformanceDayRolloverTests(unittest.TestCase):
    """
    클래스 이름: PerformanceDayRolloverTests
    기능: 신규 Trade 없이 날짜가 바뀌어도 daily 성과가 현재 KST 날짜를 따르는지 검증한다.
    작성 날짜: 2026/08/22
    """

    def test_get_performance_refreshes_daily_values_after_kst_midnight(
        self,
    ) -> None:
        """
        함수 이름: test_get_performance_refreshes_daily_values_after_kst_midnight()
        기능: KST 자정을 넘긴 조회에서 전날 daily 값만 0으로 갱신한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        before_midnight = datetime(
            2026,
            8,
            22,
            14,
            59,
            59,
            tzinfo=timezone.utc,
        )
        clock = MutableUtcClock(before_midnight)
        sell_trade = make_trade(
            trade_id="rollover-sell",
            order_id="8101",
            executed_at=datetime(
                2026,
                8,
                22,
                14,
                0,
                tzinfo=timezone.utc,
            ),
            side=OrderSide.SELL,
        )
        performance = Performance((sell_trade,), clock=clock)
        self.assertEqual(Decimal("0.11"), performance.daily_fee)
        self.assertEqual(
            Decimal("9.78021978"),
            performance.daily_return_rate,
        )

        # 15:00 UTC는 다음 KST account day의 00:00이므로 전날 거래를 daily에서 제외한다.
        clock.set(
            datetime(
                2026,
                8,
                22,
                15,
                0,
                tzinfo=timezone.utc,
            )
        )
        refreshed_performance = performance.get_performance()

        self.assertEqual(Decimal("0"), refreshed_performance.daily_fee)
        self.assertEqual(
            Decimal("0"),
            refreshed_performance.daily_return_rate,
        )
        self.assertEqual(Decimal("0.11"), refreshed_performance.total_fee)
        self.assertEqual(
            Decimal("9.78021978"),
            refreshed_performance.cumulative_return_rate,
        )
        self.assertEqual(1, refreshed_performance.completed_sell_count)


if __name__ == "__main__":
    unittest.main()
