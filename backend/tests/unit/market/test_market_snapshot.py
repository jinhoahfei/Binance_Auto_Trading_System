"""MarketSnapshot의 전체 갱신, dedup, 연속성과 원자성을 검증한다."""

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import MappingProxyType

from binance_auto_trader.domain.market import (
    Interval,
    Kline,
    MarketSnapshot,
    SUPPORTED_INTERVALS,
)


TEST_START = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)
TEST_UPDATED_AT = datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc)
INTERVAL_DURATION_BY_INTERVAL = {
    Interval.ONE_MINUTE: timedelta(minutes=1),
    Interval.THIRTY_MINUTES: timedelta(minutes=30),
    Interval.FOUR_HOURS: timedelta(hours=4),
    Interval.ONE_DAY: timedelta(days=1),
}


def fixed_clock() -> datetime:
    """
    함수 이름: fixed_clock()
    기능: snapshot update 테스트에 사용할 고정 UTC 시각을 반환한다.
    인자: 없음
    반환값: 고정 UTC datetime
    작성 날짜: 2026/08/20
    """
    return TEST_UPDATED_AT


def naive_clock() -> datetime:
    """
    함수 이름: naive_clock()
    기능: snapshot의 clock 검증 실패를 만들 naive 시각을 반환한다.
    인자: 없음
    반환값: 시간대가 없는 datetime
    작성 날짜: 2026/08/20
    """
    return datetime(2026, 8, 20, 1, 0)


def make_kline(
    interval: Interval,
    open_time: datetime,
    *,
    symbol: str = "ETHUSDT",
    close_price: Decimal = Decimal("101"),
    closed: bool = True,
) -> Kline:
    """
    함수 이름: make_kline()
    기능: snapshot 검증용으로 OHLC 관계가 일관된 Kline을 생성한다.
    인자: interval -> Kline 시간 주기
        open_time -> 봉 시작 UTC 시각
        symbol -> 거래 symbol
        close_price -> 생성할 봉의 종가
        closed -> 확정봉 여부
    반환값: snapshot 테스트용 Kline
    작성 날짜: 2026/08/20
    """
    open_price = close_price - Decimal("1")

    return Kline(
        symbol=symbol,
        interval=interval,
        open_time=open_time,
        open=open_price,
        high=max(open_price, close_price) + Decimal("1"),
        low=min(open_price, close_price) - Decimal("1"),
        close=close_price,
        volume=Decimal("10"),
        closed=closed,
    )


def make_full_klines(
    *,
    symbol: str = "ETHUSDT",
    latest_four_hour_close: Decimal = Decimal("202"),
) -> dict[Interval, list[Kline]]:
    """
    함수 이름: make_full_klines()
    기능: 모든 canonical 주기에 두 개씩 연속된 Kline 입력을 생성한다.
    인자: symbol -> 모든 Kline에 사용할 거래 symbol
        latest_four_hour_close -> 최신 4시간봉 종가
    반환값: 주기별 연속 Kline list mapping
    작성 날짜: 2026/08/20
    """
    klines_by_interval: dict[Interval, list[Kline]] = {}

    for interval_index, interval in enumerate(SUPPORTED_INTERVALS):
        duration = INTERVAL_DURATION_BY_INTERVAL[interval]
        first_close = Decimal("100") + Decimal(interval_index * 10)
        second_close = first_close + Decimal("1")
        second_open_time = {
            Interval.ONE_MINUTE: TEST_UPDATED_AT,
            Interval.THIRTY_MINUTES: TEST_UPDATED_AT,
            Interval.FOUR_HOURS: TEST_START,
            Interval.ONE_DAY: TEST_START,
        }[interval]
        first_open_time = second_open_time - duration
        if interval is Interval.FOUR_HOURS:
            second_close = latest_four_hour_close

        klines_by_interval[interval] = [
            make_kline(
                interval,
                first_open_time,
                close_price=first_close,
                symbol=symbol,
            ),
            make_kline(
                interval,
                second_open_time,
                close_price=second_close,
                symbol=symbol,
                closed=False,
            ),
        ]

    return klines_by_interval


class MarketSnapshotTests(unittest.TestCase):
    """
    클래스 이름: MarketSnapshotTests
    기능: MarketSnapshot의 초기 상태와 원자적인 전체 갱신 계약을 테스트한다.
    작성 날짜: 2026/08/20
    """

    def test_starts_not_ready_at_version_zero(self) -> None:
        """
        함수 이름: test_starts_not_ready_at_version_zero()
        기능: 기본 snapshot이 ETHUSDT와 네 빈 tuple을 가진 미준비 상태인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        snapshot = MarketSnapshot(clock=fixed_clock)

        self.assertEqual(snapshot.symbol, "ETHUSDT")
        self.assertFalse(snapshot.ready)
        self.assertEqual(snapshot.version, 0)
        self.assertIsNone(snapshot.current_eth_price)
        self.assertIsNone(snapshot.updated_at)
        self.assertEqual(tuple(snapshot.klines_by_interval), SUPPORTED_INTERVALS)
        self.assertTrue(
            all(
                interval_klines == ()
                for interval_klines in snapshot.klines_by_interval.values()
            )
        )
        with self.assertRaises(RuntimeError):
            snapshot.get_current_eth_price()

    def test_rejects_non_normalized_snapshot_symbol(self) -> None:
        """
        함수 이름: test_rejects_non_normalized_snapshot_symbol()
        기능: MarketSnapshot의 엄격한 ETHUSDT 고정 상품 계약을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        with self.assertRaises(ValueError):
            MarketSnapshot(symbol="ethusdt")

        with self.assertRaises(ValueError):
            MarketSnapshot(symbol="BTCUSDT")

        with self.assertRaises(TypeError):
            MarketSnapshot(clock="not-callable")

    def test_successful_update_commits_complete_read_only_snapshot(self) -> None:
        """
        함수 이름: test_successful_update_commits_complete_read_only_snapshot()
        기능: 네 주기 전체 update가 가격, 시각, version과 불변 collection을 반영하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        snapshot = MarketSnapshot(clock=fixed_clock)
        full_klines = make_full_klines(latest_four_hour_close=Decimal("4321.50"))

        snapshot.update(full_klines)

        self.assertTrue(snapshot.ready)
        self.assertEqual(snapshot.version, 1)
        self.assertEqual(snapshot.updated_at, TEST_UPDATED_AT)
        self.assertIs(snapshot.updated_at.tzinfo, timezone.utc)
        self.assertEqual(snapshot.current_eth_price, Decimal("4321.50"))
        self.assertEqual(snapshot.get_current_eth_price(), Decimal("4321.50"))
        self.assertIsInstance(snapshot.klines_by_interval, MappingProxyType)
        self.assertTrue(
            all(
                isinstance(interval_klines, tuple)
                for interval_klines in snapshot.klines_by_interval.values()
            )
        )

        with self.assertRaises(TypeError):
            snapshot.klines_by_interval[Interval.ONE_MINUTE] = ()

    def test_sorts_and_later_duplicate_wins(self) -> None:
        """
        함수 이름: test_sorts_and_later_duplicate_wins()
        기능: 입력 순서와 무관하게 정렬하고 같은 key에서는 뒤 Kline을 보존하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        snapshot = MarketSnapshot(clock=fixed_clock)
        full_klines = make_full_klines()
        first_kline, second_kline = full_klines[Interval.ONE_MINUTE]
        replacement_kline = make_kline(
            Interval.ONE_MINUTE,
            first_kline.open_time,
            close_price=Decimal("999"),
            closed=True,
        )
        full_klines[Interval.ONE_MINUTE] = [
            second_kline,
            first_kline,
            replacement_kline,
        ]

        snapshot.update(full_klines)

        stored_klines = snapshot.klines_by_interval[Interval.ONE_MINUTE]
        self.assertEqual(
            tuple(kline.open_time for kline in stored_klines),
            (first_kline.open_time, second_kline.open_time),
        )
        self.assertIs(stored_klines[0], replacement_kline)
        self.assertEqual(stored_klines[0].close, Decimal("999"))

    def test_rejects_missing_extra_and_empty_intervals(self) -> None:
        """
        함수 이름: test_rejects_missing_extra_and_empty_intervals()
        기능: 네 주기 중 누락, raw extra key 또는 빈 Kline 입력을 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        snapshot = MarketSnapshot(clock=fixed_clock)

        missing_interval_klines = make_full_klines()
        del missing_interval_klines[Interval.ONE_DAY]
        with self.assertRaises(ValueError):
            snapshot.update(missing_interval_klines)

        extra_interval_klines = make_full_klines()
        extra_interval_klines["5m"] = []
        with self.assertRaises(TypeError):
            snapshot.update(extra_interval_klines)

        empty_interval_klines = make_full_klines()
        empty_interval_klines[Interval.THIRTY_MINUTES] = []
        with self.assertRaises(ValueError):
            snapshot.update(empty_interval_klines)

        self.assertFalse(snapshot.ready)
        self.assertEqual(snapshot.version, 0)

    def test_rejects_wrong_symbol_interval_and_value_type(self) -> None:
        """
        함수 이름: test_rejects_wrong_symbol_interval_and_value_type()
        기능: snapshot symbol, mapping 주기와 맞지 않거나 Kline이 아닌 입력을 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        snapshot = MarketSnapshot(clock=fixed_clock)

        wrong_symbol_klines = make_full_klines()
        wrong_symbol_klines[Interval.ONE_DAY][0] = make_kline(
            Interval.ONE_DAY,
            TEST_START,
            symbol="BTCUSDT",
        )
        with self.assertRaises(ValueError):
            snapshot.update(wrong_symbol_klines)

        wrong_interval_klines = make_full_klines()
        wrong_interval_klines[Interval.ONE_DAY][0] = make_kline(
            Interval.FOUR_HOURS,
            TEST_START,
        )
        with self.assertRaises(ValueError):
            snapshot.update(wrong_interval_klines)

        wrong_value_klines = make_full_klines()
        wrong_value_klines[Interval.ONE_MINUTE][0] = object()
        with self.assertRaises(TypeError):
            snapshot.update(wrong_value_klines)

        self.assertEqual(snapshot.version, 0)

    def test_rejects_interval_gap(self) -> None:
        """
        함수 이름: test_rejects_interval_gap()
        기능: dedup과 정렬 후 open_time 차이가 정확한 주기와 다르면 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        snapshot = MarketSnapshot(clock=fixed_clock)
        discontinuous_klines = make_full_klines()
        discontinuous_klines[Interval.THIRTY_MINUTES][1] = make_kline(
            Interval.THIRTY_MINUTES,
            TEST_START + timedelta(minutes=31),
        )

        with self.assertRaises(ValueError):
            snapshot.update(discontinuous_klines)

        self.assertFalse(snapshot.ready)
        self.assertEqual(snapshot.version, 0)

    def test_requires_one_current_four_hour_kline_at_snapshot_time(self) -> None:
        """
        함수 이름: test_requires_one_current_four_hour_kline_at_snapshot_time()
        기능: current ETH price 원천이 snapshot 시각을 포함하는 유일한 진행 4시간봉인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        snapshot = MarketSnapshot(clock=fixed_clock)
        no_current_four_hour_kline = make_full_klines()
        latest_four_hour_kline = no_current_four_hour_kline[
            Interval.FOUR_HOURS
        ][-1]
        no_current_four_hour_kline[Interval.FOUR_HOURS][-1] = make_kline(
            Interval.FOUR_HOURS,
            latest_four_hour_kline.open_time,
            close_price=latest_four_hour_kline.close,
            closed=True,
        )

        with self.assertRaises(ValueError):
            snapshot.update(no_current_four_hour_kline)

        stale_open_four_hour_kline = make_full_klines()
        first_four_hour_kline = stale_open_four_hour_kline[
            Interval.FOUR_HOURS
        ][0]
        stale_open_four_hour_kline[Interval.FOUR_HOURS][0] = make_kline(
            Interval.FOUR_HOURS,
            first_four_hour_kline.open_time,
            close_price=first_four_hour_kline.close,
            closed=False,
        )

        with self.assertRaises(ValueError):
            snapshot.update(stale_open_four_hour_kline)

        future_candle_snapshot = MarketSnapshot(
            clock=lambda: TEST_START - timedelta(seconds=1)
        )
        with self.assertRaises(ValueError):
            future_candle_snapshot.update(make_full_klines())

        expired_candle_snapshot = MarketSnapshot(
            clock=lambda: TEST_START + timedelta(hours=4)
        )
        with self.assertRaises(ValueError):
            expired_candle_snapshot.update(make_full_klines())

        self.assertFalse(snapshot.ready)
        self.assertEqual(snapshot.version, 0)

    def test_rejects_future_non_four_hour_klines(self) -> None:
        """
        함수 이름: test_rejects_future_non_four_hour_klines()
        기능: 4시간봉 외 주기도 snapshot 시각 이후의 열린 봉을 게시하지 못하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        snapshot = MarketSnapshot(clock=fixed_clock)
        future_klines = make_full_klines()
        future_klines[Interval.ONE_MINUTE] = [
            make_kline(
                Interval.ONE_MINUTE,
                kline.open_time + timedelta(minutes=1),
                close_price=kline.close,
                closed=kline.closed,
            )
            for kline in future_klines[Interval.ONE_MINUTE]
        ]

        with self.assertRaises(ValueError):
            snapshot.update(future_klines)

        self.assertFalse(snapshot.ready)
        self.assertEqual(snapshot.version, 0)

    def test_failed_update_preserves_previous_state_atomically(self) -> None:
        """
        함수 이름: test_failed_update_preserves_previous_state_atomically()
        기능: 준비된 snapshot의 후속 전체 update 실패가 모든 상태와 version을 보존하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        snapshot = MarketSnapshot(clock=fixed_clock)
        snapshot.update(make_full_klines())
        previous_mapping = snapshot.klines_by_interval
        previous_price = snapshot.current_eth_price
        previous_updated_at = snapshot.updated_at
        previous_version = snapshot.version

        invalid_klines = make_full_klines(latest_four_hour_close=Decimal("9999"))
        invalid_klines[Interval.ONE_DAY][1] = make_kline(
            Interval.ONE_DAY,
            TEST_START + timedelta(days=2),
        )
        with self.assertRaises(ValueError):
            snapshot.update(invalid_klines)

        self.assertIs(snapshot.klines_by_interval, previous_mapping)
        self.assertEqual(snapshot.current_eth_price, previous_price)
        self.assertEqual(snapshot.updated_at, previous_updated_at)
        self.assertEqual(snapshot.version, previous_version)
        self.assertTrue(snapshot.ready)

    def test_invalid_clock_keeps_initial_state_unmodified(self) -> None:
        """
        함수 이름: test_invalid_clock_keeps_initial_state_unmodified()
        기능: 입력 검증 뒤 clock이 UTC를 반환하지 않아도 snapshot이 부분 갱신되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        snapshot = MarketSnapshot(clock=naive_clock)

        with self.assertRaises(ValueError):
            snapshot.update(make_full_klines())

        self.assertFalse(snapshot.ready)
        self.assertEqual(snapshot.version, 0)
        self.assertIsNone(snapshot.updated_at)
        self.assertIsNone(snapshot.current_eth_price)

    def test_version_increments_only_after_each_successful_full_update(self) -> None:
        """
        함수 이름: test_version_increments_only_after_each_successful_full_update()
        기능: 성공한 전체 교체마다 version이 정확히 한 번 증가하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        snapshot = MarketSnapshot(clock=fixed_clock)

        snapshot.update(make_full_klines())
        snapshot.update(
            make_full_klines(latest_four_hour_close=Decimal("303"))
        )

        self.assertEqual(snapshot.version, 2)
        self.assertEqual(snapshot.get_current_eth_price(), Decimal("303"))


if __name__ == "__main__":
    unittest.main()
