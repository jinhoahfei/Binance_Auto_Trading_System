"""Canonical Interval과 불변 Kline 값 객체의 계약을 검증한다."""

import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from binance_auto_trader.domain.common import (
    Interval as CommonInterval,
    SUPPORTED_INTERVALS,
)
from binance_auto_trader.domain.market import Interval, Kline
from binance_auto_trader.domain.regime import Interval as RegimeInterval


TEST_OPEN_TIME = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)


def make_kline(
    *,
    symbol: str = "ETHUSDT",
    interval: object = Interval.FOUR_HOURS,
    open_time: datetime = TEST_OPEN_TIME,
    open_price: object = Decimal("100"),
    high_price: object = Decimal("110"),
    low_price: object = Decimal("90"),
    close_price: object = Decimal("105"),
    volume: object = Decimal("12.5"),
    closed: object = True,
) -> Kline:
    """
    함수 이름: make_kline()
    기능: 개별 Kline 계약을 검증할 기본 봉과 선택적 변형을 생성한다.
    인자: symbol -> 거래 symbol
        interval -> Kline 시간 주기
        open_time -> 봉 시작 UTC 시각
        open_price -> 시가
        high_price -> 고가
        low_price -> 저가
        close_price -> 종가
        volume -> 거래량
        closed -> 확정봉 여부
    반환값: 입력값으로 생성한 Kline
    작성 날짜: 2026/08/20
    """
    return Kline(
        symbol=symbol,
        interval=interval,
        open_time=open_time,
        open=open_price,
        high=high_price,
        low=low_price,
        close=close_price,
        volume=volume,
        closed=closed,
    )


class IntervalContractTests(unittest.TestCase):
    """
    클래스 이름: IntervalContractTests
    기능: 시스템 전체가 하나의 canonical Interval과 고정 순서를 공유하는지 테스트한다.
    작성 날짜: 2026/08/20
    """

    def test_common_market_and_regime_export_same_interval(self) -> None:
        """
        함수 이름: test_common_market_and_regime_export_same_interval()
        기능: common, market, regime 공개 API의 Interval class identity를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self.assertIs(Interval, CommonInterval)
        self.assertIs(RegimeInterval, CommonInterval)

    def test_supported_intervals_have_exact_names_values_and_order(self) -> None:
        """
        함수 이름: test_supported_intervals_have_exact_names_values_and_order()
        기능: 네 canonical 시간 주기의 이름, wire 값과 순서를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self.assertEqual(
            tuple(interval.name for interval in SUPPORTED_INTERVALS),
            (
                "ONE_MINUTE",
                "THIRTY_MINUTES",
                "FOUR_HOURS",
                "ONE_DAY",
            ),
        )
        self.assertEqual(
            tuple(interval.value for interval in SUPPORTED_INTERVALS),
            ("1m", "30m", "4h", "1d"),
        )
        self.assertEqual(SUPPORTED_INTERVALS, tuple(Interval))


class KlineTests(unittest.TestCase):
    """
    클래스 이름: KlineTests
    기능: Kline의 타입, 수치 불변식과 불변성을 테스트한다.
    작성 날짜: 2026/08/20
    """

    def test_accepts_normalized_decimal_kline(self) -> None:
        """
        함수 이름: test_accepts_normalized_decimal_kline()
        기능: 정상 symbol, UTC 시각과 Decimal OHLCV로 Kline이 생성되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        kline = make_kline()

        self.assertEqual(kline.symbol, "ETHUSDT")
        self.assertIs(kline.interval, Interval.FOUR_HOURS)
        self.assertEqual(kline.open_time, TEST_OPEN_TIME)
        self.assertIs(kline.open_time.tzinfo, timezone.utc)
        self.assertEqual(kline.close, Decimal("105"))
        self.assertTrue(kline.closed)

    def test_is_frozen_and_uses_slots(self) -> None:
        """
        함수 이름: test_is_frozen_and_uses_slots()
        기능: 생성된 Kline 필드가 변경되지 않고 instance dictionary가 없는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        kline = make_kline()

        with self.assertRaises(FrozenInstanceError):
            kline.close = Decimal("106")

        self.assertFalse(hasattr(kline, "__dict__"))

    def test_rejects_non_normalized_symbols(self) -> None:
        """
        함수 이름: test_rejects_non_normalized_symbols()
        기능: 빈 값, 공백, 소문자와 비 ASCII symbol을 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        invalid_symbols = ("", " ETHUSDT", "ETHUSDT ", "ethusdt", "이더USDT")

        for invalid_symbol in invalid_symbols:
            with self.subTest(invalid_symbol=invalid_symbol):
                with self.assertRaises(ValueError):
                    make_kline(symbol=invalid_symbol)

        with self.assertRaises(TypeError):
            make_kline(symbol=123)

    def test_rejects_non_canonical_interval(self) -> None:
        """
        함수 이름: test_rejects_non_canonical_interval()
        기능: canonical Interval instance가 아닌 raw 시간 주기 값을 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        with self.assertRaises(TypeError):
            make_kline(interval="4h")

    def test_requires_utc_open_time(self) -> None:
        """
        함수 이름: test_requires_utc_open_time()
        기능: naive 또는 UTC가 아닌 open_time을 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        with self.assertRaises(ValueError):
            make_kline(open_time=datetime(2026, 8, 20, 0, 0))

        non_utc_time = datetime(
            2026,
            8,
            20,
            9,
            0,
            tzinfo=timezone(timedelta(hours=9)),
        )
        with self.assertRaises(ValueError):
            make_kline(open_time=non_utc_time)

        with self.assertRaises(TypeError):
            make_kline(open_time="2026-08-20T00:00:00Z")

    def test_rejects_float_ohlcv_values(self) -> None:
        """
        함수 이름: test_rejects_float_ohlcv_values()
        기능: 모든 OHLCV 필드에서 float 사용을 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        invalid_overrides = (
            {"open_price": 100.0},
            {"high_price": 110.0},
            {"low_price": 90.0},
            {"close_price": 105.0},
            {"volume": 12.5},
        )

        for invalid_override in invalid_overrides:
            with self.subTest(invalid_override=invalid_override):
                with self.assertRaises(TypeError):
                    make_kline(**invalid_override)

    def test_rejects_non_finite_and_out_of_range_values(self) -> None:
        """
        함수 이름: test_rejects_non_finite_and_out_of_range_values()
        기능: 비유한 수치, 0 이하 가격과 음수 거래량을 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        for invalid_value in (
            Decimal("NaN"),
            Decimal("Infinity"),
            Decimal("-Infinity"),
        ):
            with self.subTest(invalid_value=invalid_value):
                with self.assertRaises(ValueError):
                    make_kline(close_price=invalid_value)

        with self.assertRaises(ValueError):
            make_kline(open_price=Decimal("0"))

        with self.assertRaises(ValueError):
            make_kline(low_price=Decimal("-1"))

        with self.assertRaises(ValueError):
            make_kline(volume=Decimal("-0.1"))

        zero_volume_kline = make_kline(volume=Decimal("0"))
        self.assertEqual(zero_volume_kline.volume, Decimal("0"))

    def test_rejects_inconsistent_ohlc_relationships(self) -> None:
        """
        함수 이름: test_rejects_inconsistent_ohlc_relationships()
        기능: 저가와 고가가 시가·종가를 감싸지 않는 Kline을 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        with self.assertRaises(ValueError):
            make_kline(low_price=Decimal("101"))

        with self.assertRaises(ValueError):
            make_kline(high_price=Decimal("104"))

    def test_requires_boolean_closed_flag(self) -> None:
        """
        함수 이름: test_requires_boolean_closed_flag()
        기능: closed flag가 정확한 bool 타입이어야 하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        with self.assertRaises(TypeError):
            make_kline(closed=1)


if __name__ == "__main__":
    unittest.main()
