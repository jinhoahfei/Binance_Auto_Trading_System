"""ADR-004의 4시간봉 지표 공식과 IndicatorSnapshot 계약을 검증한다."""

import json
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, localcontext
from pathlib import Path
from typing import Any

from binance_auto_trader.application import (
    RegimeController,
    RegimeEvaluationError,
    RegimeEvaluationFailureCode,
)
from binance_auto_trader.domain.market import (
    IndicatorSnapshot,
    Interval,
    Kline,
    MarketSnapshot,
    SUPPORTED_INTERVALS,
    SwingStructure,
)
from binance_auto_trader.domain.regime import RegimeSTM


GOLDEN_VECTOR_PATH = (
    Path(__file__).parents[2]
    / "fixtures"
    / "market_snapshots"
    / "indicator_golden_vector.json"
)
SNAPSHOT_UPDATED_AT = datetime(
    2026,
    8,
    20,
    1,
    0,
    tzinfo=timezone.utc,
)
CURRENT_FOUR_HOUR_OPEN = datetime(
    2026,
    8,
    20,
    0,
    0,
    tzinfo=timezone.utc,
)
FOUR_HOUR_DURATION = timedelta(hours=4)
LATEST_CLOSED_CANDLE_ID = "ETHUSDT:4h:2026-08-19T20:00:00Z"


def fixed_clock() -> datetime:
    """
    함수 이름: fixed_clock()
    기능: 시장 snapshot 테스트에 사용할 고정 UTC 시각을 반환한다.
    인자: 없음
    반환값: 고정 UTC datetime
    작성 날짜: 2026/08/20
    """
    return SNAPSHOT_UPDATED_AT


def load_golden_vector() -> dict[str, Any]:
    """
    함수 이름: load_golden_vector()
    기능: ADR-004의 지표 golden vector JSON을 읽어 반환한다.
    인자: 없음
    반환값: golden 입력과 기대 결과 mapping
    작성 날짜: 2026/08/20
    """
    with GOLDEN_VECTOR_PATH.open(encoding="utf-8") as fixture_file:
        return json.load(fixture_file)


def make_kline(
    interval: Interval,
    open_time: datetime,
    close_price: Decimal,
    *,
    high_price: Decimal | None = None,
    low_price: Decimal | None = None,
    closed: bool,
) -> Kline:
    """
    함수 이름: make_kline()
    기능: 명시한 가격과 확정 상태를 가진 테스트용 ETHUSDT Kline을 생성한다.
    인자: interval -> Kline 시간 주기
        open_time -> 봉 시작 UTC 시각
        close_price -> 봉 종가
        high_price -> 선택적 고가
        low_price -> 선택적 저가
        closed -> 확정봉 여부
    반환값: 지표 계산용 Kline
    작성 날짜: 2026/08/20
    """
    selected_high = (
        close_price + Decimal("1")
        if high_price is None
        else high_price
    )
    selected_low = (
        close_price - Decimal("1")
        if low_price is None
        else low_price
    )

    return Kline(
        symbol="ETHUSDT",
        interval=interval,
        open_time=open_time,
        open=close_price,
        high=selected_high,
        low=selected_low,
        close=close_price,
        volume=Decimal("1"),
        closed=closed,
    )


def make_non_four_hour_klines(
    interval: Interval,
) -> list[Kline]:
    """
    함수 이름: make_non_four_hour_klines()
    기능: MarketSnapshot 준비에 필요한 다른 주기의 연속된 확정봉과 진행봉을 만든다.
    인자: interval -> FOUR_HOURS가 아닌 canonical 시간 주기
    반환값: 연속된 두 Kline 목록
    작성 날짜: 2026/08/20
    """
    duration_by_interval = {
        Interval.ONE_MINUTE: timedelta(minutes=1),
        Interval.THIRTY_MINUTES: timedelta(minutes=30),
        Interval.ONE_DAY: timedelta(days=1),
    }
    latest_open_by_interval = {
        Interval.ONE_MINUTE: SNAPSHOT_UPDATED_AT,
        Interval.THIRTY_MINUTES: SNAPSHOT_UPDATED_AT,
        Interval.ONE_DAY: datetime(
            2026,
            8,
            20,
            0,
            0,
            tzinfo=timezone.utc,
        ),
    }
    duration = duration_by_interval[interval]
    latest_open = latest_open_by_interval[interval]

    return [
        make_kline(
            interval,
            latest_open - duration,
            Decimal("100"),
            closed=True,
        ),
        make_kline(
            interval,
            latest_open,
            Decimal("101"),
            closed=False,
        ),
    ]


def make_market_snapshot(
    golden_vector: dict[str, Any],
    *,
    closed_count: int = 14,
    high_overrides: dict[int, Decimal] | None = None,
    low_overrides: dict[int, Decimal] | None = None,
    current_price: Decimal | None = None,
    current_high: Decimal = Decimal("120"),
    current_low: Decimal = Decimal("100"),
) -> MarketSnapshot:
    """
    함수 이름: make_market_snapshot()
    기능: golden 4시간봉과 다른 세 주기를 포함하는 준비된 MarketSnapshot을 만든다.
    인자: golden_vector -> fixture에서 읽은 golden 입력
        closed_count -> 사용할 확정 4시간봉 개수
        high_overrides -> index별 확정봉 고가 대체값
        low_overrides -> index별 확정봉 저가 대체값
        current_price -> 진행봉 종가 대체값
        current_high -> 진행봉 고가
        current_low -> 진행봉 저가
    반환값: 지표 계산이 가능한 MarketSnapshot
    작성 날짜: 2026/08/20
    """
    closed_values = golden_vector["closed"]
    close_prices = [
        Decimal(value)
        for value in closed_values["close"][:closed_count]
    ]
    high_prices = [
        Decimal(value)
        for value in closed_values["high"][:closed_count]
    ]
    low_prices = [
        Decimal(value)
        for value in closed_values["low"][:closed_count]
    ]
    for index, override in (high_overrides or {}).items():
        high_prices[index] = override
    for index, override in (low_overrides or {}).items():
        low_prices[index] = override

    first_four_hour_open = (
        CURRENT_FOUR_HOUR_OPEN - FOUR_HOUR_DURATION * closed_count
    )
    four_hour_klines = [
        make_kline(
            Interval.FOUR_HOURS,
            first_four_hour_open + FOUR_HOUR_DURATION * index,
            close_price,
            high_price=high_prices[index],
            low_price=low_prices[index],
            closed=True,
        )
        for index, close_price in enumerate(close_prices)
    ]
    selected_current_price = (
        Decimal(golden_vector["current_price"])
        if current_price is None
        else current_price
    )
    four_hour_klines.append(
        make_kline(
            Interval.FOUR_HOURS,
            CURRENT_FOUR_HOUR_OPEN,
            selected_current_price,
            high_price=current_high,
            low_price=current_low,
            closed=False,
        )
    )

    klines_by_interval = {
        interval: (
            four_hour_klines
            if interval is Interval.FOUR_HOURS
            else make_non_four_hour_klines(interval)
        )
        for interval in SUPPORTED_INTERVALS
    }
    market_snapshot = MarketSnapshot(clock=fixed_clock)
    market_snapshot.update(klines_by_interval)

    return market_snapshot


def make_regime_controller() -> RegimeController:
    """
    함수 이름: make_regime_controller()
    기능: 지표 공식 단위 테스트에 사용할 RegimeController를 생성한다.
    인자: 없음
    반환값: 새 RegimeController
    작성 날짜: 2026/08/20
    """
    return RegimeController(RegimeSTM())


def make_swing_structure() -> SwingStructure:
    """
    함수 이름: make_swing_structure()
    기능: IndicatorSnapshot 직접 계약 테스트에 사용할 정상 swing 구조를 만든다.
    인자: 없음
    반환값: HH와 HL이 성립하는 불변 SwingStructure
    작성 날짜: 2026/08/20
    """
    return SwingStructure(
        swing_highs=(Decimal("115"), Decimal("116"), Decimal("118")),
        swing_lows=(Decimal("95"), Decimal("96")),
        has_higher_high=True,
        has_higher_low=True,
        has_lower_high=False,
        has_lower_low=False,
    )


def make_empty_indicator_snapshot() -> IndicatorSnapshot:
    """
    함수 이름: make_empty_indicator_snapshot()
    기능: 직접 update 계약을 검증할 준비 전 IndicatorSnapshot을 생성한다.
    인자: 없음
    반환값: provenance만 있고 아직 준비되지 않은 IndicatorSnapshot
    작성 날짜: 2026/08/20
    """
    return IndicatorSnapshot(
        symbol="ETHUSDT",
        current_price=Decimal("110"),
        source_market_version=1,
        source_candle_id=LATEST_CLOSED_CANDLE_ID,
        calculated_at=SNAPSHOT_UPDATED_AT,
    )


class IndicatorCalculationTests(unittest.TestCase):
    """
    클래스 이름: IndicatorCalculationTests
    기능: ADR-004 golden 공식과 확정봉·진행봉 분리 및 strict swing을 테스트한다.
    작성 날짜: 2026/08/20
    """

    def test_calculates_exact_adr_004_golden_vector(self) -> None:
        """
        함수 이름: test_calculates_exact_adr_004_golden_vector()
        기능: 낮은 외부 Decimal 정밀도에서도 EMA, slope, swing과 live EMA가 정확한지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        golden_vector = load_golden_vector()
        expected = golden_vector["expected"]
        market_snapshot = make_market_snapshot(golden_vector)

        with localcontext() as caller_context:
            caller_context.prec = 6
            caller_context.rounding = ROUND_DOWN
            indicator_snapshot = make_regime_controller().calculate_4h_indicators(
                market_snapshot
            )

        self.assertIsInstance(indicator_snapshot, IndicatorSnapshot)
        self.assertTrue(indicator_snapshot.ready)
        self.assertIs(indicator_snapshot.timeframe, Interval.FOUR_HOURS)
        self.assertEqual(
            indicator_snapshot.ema9_series,
            tuple(Decimal(value) for value in expected["ema9_series"]),
        )
        self.assertIsInstance(indicator_snapshot.ema9_series, tuple)
        self.assertEqual(
            indicator_snapshot.ema9_slope,
            Decimal(expected["ema9_slope"]),
        )
        self.assertEqual(
            indicator_snapshot.live_ema9,
            Decimal(expected["live_ema9"]),
        )
        self.assertEqual(indicator_snapshot.current_price, Decimal("110"))
        self.assertEqual(
            indicator_snapshot.source_market_version,
            market_snapshot.version,
        )
        self.assertEqual(
            indicator_snapshot.source_candle_id,
            LATEST_CLOSED_CANDLE_ID,
        )
        self.assertEqual(indicator_snapshot.calculated_at, SNAPSHOT_UPDATED_AT)

        swing_structure = indicator_snapshot.swing_structure
        self.assertIsInstance(swing_structure, SwingStructure)
        self.assertEqual(
            swing_structure.swing_highs,
            tuple(Decimal(value) for value in expected["swing_highs"]),
        )
        self.assertEqual(
            swing_structure.swing_lows,
            tuple(Decimal(value) for value in expected["swing_lows"]),
        )
        self.assertEqual(
            swing_structure.has_higher_high,
            expected["has_higher_high"],
        )
        self.assertEqual(
            swing_structure.has_higher_low,
            expected["has_higher_low"],
        )
        self.assertEqual(
            swing_structure.has_lower_high,
            expected["has_lower_high"],
        )
        self.assertEqual(
            swing_structure.has_lower_low,
            expected["has_lower_low"],
        )

    def test_uses_only_closed_candles_for_ema_and_swing(self) -> None:
        """
        함수 이름: test_uses_only_closed_candles_for_ema_and_swing()
        기능: 진행봉의 극단 고저가가 EMA series와 swing에 들어가지 않고 종가만 live 계산에 쓰이는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        golden_vector = load_golden_vector()
        baseline = make_regime_controller().calculate_4h_indicators(
            make_market_snapshot(golden_vector)
        )
        extreme_open_candle = make_regime_controller().calculate_4h_indicators(
            make_market_snapshot(
                golden_vector,
                current_high=Decimal("999"),
                current_low=Decimal("1"),
            )
        )
        changed_current_price = make_regime_controller().calculate_4h_indicators(
            make_market_snapshot(
                golden_vector,
                current_price=Decimal("120"),
                current_high=Decimal("999"),
                current_low=Decimal("1"),
            )
        )

        self.assertEqual(
            extreme_open_candle.ema9_series,
            baseline.ema9_series,
        )
        self.assertEqual(
            extreme_open_candle.swing_structure,
            baseline.swing_structure,
        )
        self.assertNotIn(
            Decimal("999"),
            extreme_open_candle.swing_structure.swing_highs,
        )
        self.assertNotIn(
            Decimal("1"),
            extreme_open_candle.swing_structure.swing_lows,
        )
        self.assertEqual(
            changed_current_price.ema9_series,
            baseline.ema9_series,
        )
        self.assertEqual(
            changed_current_price.swing_structure,
            baseline.swing_structure,
        )
        self.assertEqual(
            changed_current_price.ema9_slope,
            Decimal("0.83333333"),
        )
        self.assertEqual(
            changed_current_price.live_ema9,
            Decimal("111.2"),
        )

    def test_strict_swing_excludes_ties_and_rejects_missing_pivots(self) -> None:
        """
        함수 이름: test_strict_swing_excludes_ties_and_rejects_missing_pivots()
        기능: 같은 고저가 tie를 pivot으로 세지 않고 swing 쌍이 부족하면 실패하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        golden_vector = load_golden_vector()
        controller = make_regime_controller()
        tied_high_snapshot = make_market_snapshot(
            golden_vector,
            high_overrides={6: Decimal("116")},
        )

        tied_high_indicators = controller.calculate_4h_indicators(
            tied_high_snapshot
        )

        self.assertEqual(
            tied_high_indicators.swing_structure.swing_highs,
            (Decimal("115"), Decimal("118")),
        )
        tied_low_snapshot = make_market_snapshot(
            golden_vector,
            low_overrides={7: Decimal("96")},
        )
        with self.assertRaises(RegimeEvaluationError) as caught_error:
            make_regime_controller().calculate_4h_indicators(
                tied_low_snapshot
            )
        self.assertIs(
            caught_error.exception.failure_code,
            RegimeEvaluationFailureCode.INSUFFICIENT_SWING_POINTS,
        )

    def test_rejects_insufficient_closed_candles_and_swing_points(self) -> None:
        """
        함수 이름: test_rejects_insufficient_closed_candles_and_swing_points()
        기능: EMA six-point 입력이나 두 swing 쌍이 부족한 snapshot을 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        golden_vector = load_golden_vector()
        with self.assertRaises(RegimeEvaluationError) as closed_error:
            make_regime_controller().calculate_4h_indicators(
                make_market_snapshot(golden_vector, closed_count=13)
            )
        self.assertIs(
            closed_error.exception.failure_code,
            RegimeEvaluationFailureCode.INSUFFICIENT_CLOSED_KLINES,
        )

        monotonic_highs = {
            index: Decimal("200") + Decimal(index)
            for index in range(14)
        }
        monotonic_lows = {
            index: Decimal("50") + Decimal(index)
            for index in range(14)
        }
        with self.assertRaises(RegimeEvaluationError) as swing_error:
            make_regime_controller().calculate_4h_indicators(
                make_market_snapshot(
                    golden_vector,
                    high_overrides=monotonic_highs,
                    low_overrides=monotonic_lows,
                )
            )
        self.assertIs(
            swing_error.exception.failure_code,
            RegimeEvaluationFailureCode.INSUFFICIENT_SWING_POINTS,
        )

    def test_rejects_market_snapshot_that_is_not_ready(self) -> None:
        """
        함수 이름: test_rejects_market_snapshot_that_is_not_ready()
        기능: 준비되지 않은 MarketSnapshot에서는 부분 지표를 만들지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        with self.assertRaises(RegimeEvaluationError) as caught_error:
            make_regime_controller().calculate_4h_indicators(
                MarketSnapshot(clock=fixed_clock)
            )
        self.assertIs(
            caught_error.exception.failure_code,
            RegimeEvaluationFailureCode.MARKET_SNAPSHOT_NOT_READY,
        )


class IndicatorSnapshotContractTests(unittest.TestCase):
    """
    클래스 이름: IndicatorSnapshotContractTests
    기능: IndicatorSnapshot과 SwingStructure의 검증, 불변성과 원자적 update를 테스트한다.
    작성 날짜: 2026/08/20
    """

    def test_validates_indicator_provenance_at_construction(self) -> None:
        """
        함수 이름: test_validates_indicator_provenance_at_construction()
        기능: symbol, 현재가, version, candle ID, UTC 시각과 4H 주기만 허용하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        indicator_snapshot = make_empty_indicator_snapshot()

        self.assertFalse(indicator_snapshot.ready)
        self.assertEqual(indicator_snapshot.ema9_series, ())
        with self.assertRaises(RuntimeError):
            _ = indicator_snapshot.ema9_slope
        with self.assertRaises(RuntimeError):
            _ = indicator_snapshot.swing_structure
        with self.assertRaises(RuntimeError):
            _ = indicator_snapshot.live_ema9

        invalid_overrides = (
            {"symbol": "ethusdt"},
            {"current_price": 110.0},
            {"current_price": Decimal("0")},
            {"source_market_version": True},
            {"source_market_version": -1},
            {"source_candle_id": ""},
            {"calculated_at": datetime(2026, 8, 20, 1, 0)},
            {"timeframe": Interval.ONE_MINUTE},
        )
        valid_arguments = {
            "symbol": "ETHUSDT",
            "current_price": Decimal("110"),
            "source_market_version": 1,
            "source_candle_id": LATEST_CLOSED_CANDLE_ID,
            "calculated_at": SNAPSHOT_UPDATED_AT,
        }
        for invalid_override in invalid_overrides:
            with self.subTest(invalid_override=invalid_override):
                arguments = valid_arguments | invalid_override
                with self.assertRaises((TypeError, ValueError)):
                    IndicatorSnapshot(**arguments)

    def test_public_state_is_read_only_and_defensively_copied(self) -> None:
        """
        함수 이름: test_public_state_is_read_only_and_defensively_copied()
        기능: update 입력 collection과 공개 snapshot 및 swing 상태를 외부에서 변경할 수 없는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        indicator_snapshot = make_empty_indicator_snapshot()
        swing_structure = make_swing_structure()
        mutable_ema9_series = [
            Decimal("104"),
            Decimal("105"),
            Decimal("106"),
            Decimal("107"),
            Decimal("108"),
            Decimal("109"),
        ]

        indicator_snapshot.update(
            mutable_ema9_series,
            Decimal("0.90909091"),
            swing_structure,
            Decimal("109.2"),
        )
        mutable_ema9_series[0] = Decimal("999")

        self.assertTrue(indicator_snapshot.ready)
        self.assertEqual(indicator_snapshot.ema9_series[0], Decimal("104"))
        with self.assertRaises(TypeError):
            indicator_snapshot.ema9_series[0] = Decimal("999")
        with self.assertRaises(AttributeError):
            indicator_snapshot.ema9_slope = Decimal("1")
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            swing_structure.has_higher_high = False

    def test_failed_update_is_atomic_and_rejects_float_values(self) -> None:
        """
        함수 이름: test_failed_update_is_atomic_and_rejects_float_values()
        기능: update의 어떤 입력 검증이 실패해도 기존 정상 상태를 유지하고 float를 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        indicator_snapshot = make_empty_indicator_snapshot()
        swing_structure = make_swing_structure()
        valid_series = (
            Decimal("104"),
            Decimal("105"),
            Decimal("106"),
            Decimal("107"),
            Decimal("108"),
            Decimal("109"),
        )
        indicator_snapshot.update(
            valid_series,
            Decimal("0.90909091"),
            swing_structure,
            Decimal("109.2"),
        )
        expected_state = (
            indicator_snapshot.ema9_series,
            indicator_snapshot.ema9_slope,
            indicator_snapshot.swing_structure,
            indicator_snapshot.live_ema9,
            indicator_snapshot.ready,
        )
        invalid_updates = (
            {
                "ema9_series": (*valid_series[:-1], 109.0),
                "ema9_slope": Decimal("0.90909091"),
                "swing_structure": swing_structure,
                "live_ema9": Decimal("109.2"),
            },
            {
                "ema9_series": valid_series,
                "ema9_slope": 0.90909091,
                "swing_structure": swing_structure,
                "live_ema9": Decimal("109.2"),
            },
            {
                "ema9_series": valid_series,
                "ema9_slope": Decimal("0.90909091"),
                "swing_structure": object(),
                "live_ema9": Decimal("109.2"),
            },
            {
                "ema9_series": valid_series,
                "ema9_slope": Decimal("0.90909091"),
                "swing_structure": swing_structure,
                "live_ema9": 109.2,
            },
        )

        for invalid_update in invalid_updates:
            with self.subTest(invalid_update=invalid_update):
                with self.assertRaises(TypeError):
                    indicator_snapshot.update(**invalid_update)
                self.assertEqual(
                    (
                        indicator_snapshot.ema9_series,
                        indicator_snapshot.ema9_slope,
                        indicator_snapshot.swing_structure,
                        indicator_snapshot.live_ema9,
                        indicator_snapshot.ready,
                    ),
                    expected_state,
                )

    def test_swing_structure_rejects_float_and_non_boolean_values(self) -> None:
        """
        함수 이름: test_swing_structure_rejects_float_and_non_boolean_values()
        기능: SwingStructure가 float pivot과 bool이 아닌 방향 flag를 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        swing_structure = make_swing_structure()

        with self.assertRaises(TypeError):
            replace(
                swing_structure,
                swing_highs=(Decimal("115"), Decimal("116"), 118.0),
            )
        with self.assertRaises(TypeError):
            replace(swing_structure, has_higher_high=1)


if __name__ == "__main__":
    unittest.main()
