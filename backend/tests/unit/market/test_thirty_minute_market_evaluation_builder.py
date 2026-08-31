"""30분 EMA9·OLS 정규화와 production 시장 평가 builder 계약을 검증한다."""

import unittest
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from decimal import (
    Decimal,
    ROUND_DOWN,
    ROUND_HALF_EVEN,
    localcontext,
)

from binance_auto_trader.application.market_evaluation_builder import (
    MarketEvaluationCalculationError,
    ThirtyMinuteMarketEvaluationBuilder,
)
from binance_auto_trader.domain.common import Interval
from binance_auto_trader.domain.market import (
    Kline,
    MarketSnapshot,
    calculate_candidate_ema9_slope,
    calculate_ema9_series,
    calculate_normalized_ols_slope,
    calculate_raw_ols_slope,
    normalize_ols_slope,
)
from binance_auto_trader.domain.market.ema_slope import DECIMAL_PRECISION


# 모든 fixture는 UTC 12:00에 열린 같은 authoritative 30분봉을 기준으로 만든다.
CURRENT_CANDLE_OPEN = datetime(
    2026,
    8,
    29,
    12,
    0,
    tzinfo=timezone.utc,
)
FOUR_HOUR_CANDLE_OPEN = CURRENT_CANDLE_OPEN
ONE_DAY_CANDLE_OPEN = CURRENT_CANDLE_OPEN.replace(hour=0)
BASE_CLOSED_PRICES = tuple(
    Decimal(100 + index)
    for index in range(20)
)


class MutableUtcClock:
    """
    클래스 이름: MutableUtcClock
    기능: MarketSnapshot version별 검증 시각을 명시적으로 전진시키는 UTC clock을 제공한다.
    작성 날짜: 2026/08/29
    """

    def __init__(self, current_time: datetime) -> None:
        """
        함수 이름: __init__()
        기능: 첫 snapshot 갱신에 사용할 UTC 시각을 저장한다.
        인자: current_time -> 시간대가 있는 현재 UTC 시각
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        self.current_time = current_time  # 호출 전까지 유지할 exact snapshot 시각이다.

    def __call__(self) -> datetime:
        """
        함수 이름: __call__()
        기능: 현재 설정된 snapshot UTC 시각을 반환한다.
        인자: 없음
        반환값: 현재 UTC datetime
        작성 날짜: 2026/08/29
        """
        return self.current_time  # MarketSnapshot이 같은 객체를 clock으로 직접 호출한다.

    def set_time(self, current_time: datetime) -> None:
        """
        함수 이름: set_time()
        기능: 다음 MarketSnapshot update가 사용할 UTC 시각으로 전진시킨다.
        인자: current_time -> 새 UTC snapshot 시각
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        self.current_time = current_time  # 각 테스트가 event와 snapshot 시각을 함께 통제한다.


class MutableMonotonicClock:
    """
    클래스 이름: MutableMonotonicClock
    기능: 30분 tick만 유지 조건 시간을 소비하는지 확인할 monotonic clock을 제공한다.
    작성 날짜: 2026/08/29
    """

    def __init__(self, nanoseconds: int = 0) -> None:
        """
        함수 이름: __init__()
        기능: 초기 monotonic nanosecond와 호출 횟수를 저장한다.
        인자: nanoseconds -> 첫 호출에서 반환할 단조 시각
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        self.nanoseconds = nanoseconds
        self.call_count = 0  # 1분 close가 clock을 읽는 순간을 직접 탐지한다.

    def __call__(self) -> int:
        """
        함수 이름: __call__()
        기능: 호출 횟수를 기록하고 현재 monotonic nanosecond를 반환한다.
        인자: 없음
        반환값: 현재 monotonic nanosecond
        작성 날짜: 2026/08/29
        """
        self.call_count += 1
        return self.nanoseconds  # builder에는 wall clock 대신 이 값만 노출한다.

    def set_time(self, nanoseconds: int) -> None:
        """
        함수 이름: set_time()
        기능: 다음 30분 tick이 읽을 monotonic nanosecond를 설정한다.
        인자: nanoseconds -> 새 단조 시각
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        self.nanoseconds = nanoseconds  # 테스트가 5초와 3분 경계를 정수로 제어한다.


class ControlledConditionBuilder(ThirtyMinuteMarketEvaluationBuilder):
    """
    클래스 이름: ControlledConditionBuilder
    기능: 실제 public builder 경로에서 조건 입력 두 값만 exact 경계값으로 통제한다.
    작성 날짜: 2026/08/29
    """

    __slots__ = (
        "_controlled_pct_b",
        "_controlled_slope",
    )

    def __init__(self, monotonic_clock: MutableMonotonicClock) -> None:
        """
        함수 이름: __init__()
        기능: production builder와 동일한 상태에 제어할 %B·slope 기본값을 추가한다.
        인자: monotonic_clock -> duration 경계를 제어할 monotonic clock
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        super().__init__(monotonic_clock=monotonic_clock)
        self._controlled_pct_b = Decimal("0.50")
        self._controlled_slope = Decimal("0.05")

    def set_condition_values(
        self,
        realtime_pct_b: Decimal,
        realtime_slope: Decimal,
    ) -> None:
        """
        함수 이름: set_condition_values()
        기능: 다음 public builder tick이 조건 평가에 사용할 exact %B와 slope를 설정한다.
        인자: realtime_pct_b -> 통제할 실시간 Bollinger %B
            realtime_slope -> 통제할 정규화 EMA9 OLS slope
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # 금융 경계값을 float로 변환하지 않고 전달받은 Decimal 그대로 보존한다.
        self._controlled_pct_b = realtime_pct_b
        self._controlled_slope = realtime_slope

    def _calculate_evaluation_values(
        self,
        market_snapshot: MarketSnapshot,
        observed_kline: Kline,
    ) -> dict[str, object]:
        """
        함수 이름: _calculate_evaluation_values()
        기능: production 금융 계산을 수행한 뒤 조건 입력 두 필드만 테스트 경계값으로 치환한다.
        인자: market_snapshot -> 실제 source provenance를 가진 authoritative snapshot
            observed_kline -> public builder 호출을 발생시킨 exact 30분 Kline
        반환값: production 필드와 통제한 조건 값을 포함한 evaluation mapping
        작성 날짜: 2026/08/29
        """
        # source·Bollinger·EMA·CCI 계산은 production 구현을 그대로 통과시킨다.
        evaluation_values = super()._calculate_evaluation_values(
            market_snapshot,
            observed_kline,
        )
        evaluation_values["realtime_pct_b"] = self._controlled_pct_b
        evaluation_values["realtime_ema_slope"] = self._controlled_slope
        return evaluation_values  # public __call__이 실제 condition state와 DTO를 완성한다.


def make_kline(
    interval: Interval,
    open_time: datetime,
    close_price: Decimal,
    *,
    closed: bool,
    event_time: datetime | None = None,
) -> Kline:
    """
    함수 이름: make_kline()
    기능: OHLC 관계와 선택적 WebSocket provenance가 유효한 ETHUSDT Kline을 만든다.
    인자: interval -> 생성할 canonical 봉 주기
        open_time -> 봉 시작 UTC 시각
        close_price -> open과 close에 함께 사용할 양의 Decimal 가격
        closed -> 확정봉 여부
        event_time -> live source의 UTC event 시각 또는 REST fixture용 None
    반환값: 검증을 통과한 불변 Kline
    작성 날짜: 2026/08/29
    """
    # open과 close를 같게 두고 high·low를 한 가격 단위씩 벌려 모든 지표에 변동을 준다.
    return Kline(
        symbol="ETHUSDT",
        interval=interval,
        open_time=open_time,
        open=close_price,
        high=close_price + Decimal("1"),
        low=close_price - Decimal("1"),
        close=close_price,
        volume=Decimal("10"),
        closed=closed,
        event_time=event_time,
    )


def make_snapshot_klines(
    snapshot_time: datetime,
    closed_prices: Sequence[Decimal],
    current_thirty_minute: Kline,
    *,
    latest_one_minute: Kline | None = None,
) -> dict[Interval, tuple[Kline, ...]]:
    """
    함수 이름: make_snapshot_klines()
    기능: 계산용 30분 history와 snapshot 시각에 유효한 나머지 세 주기를 만든다.
    인자: snapshot_time -> open·closed 시간 검증의 기준 UTC 시각
        closed_prices -> 현재 30분봉 직전까지의 시간순 확정 종가
        current_thirty_minute -> latest authoritative 30분 Kline
        latest_one_minute -> source로 사용할 확정 1분봉 또는 자동 생성할 None
    반환값: 네 canonical 주기를 정확히 포함한 Kline tuple mapping
    작성 날짜: 2026/08/29
    """
    # 마지막 확정봉이 current open 직전에 끝나도록 연속 30분 history를 역산한다.
    closed_count = len(closed_prices)
    closed_thirty_minute_klines = tuple(
        make_kline(
            Interval.THIRTY_MINUTES,
            current_thirty_minute.open_time
            - timedelta(minutes=30 * (closed_count - index)),
            close_price,
            closed=True,
        )
        for index, close_price in enumerate(closed_prices)
    )

    # 1분 source가 없으면 snapshot 시각을 포함하는 진행 중 봉으로 다른 주기 검증을 채운다.
    selected_one_minute = latest_one_minute
    if selected_one_minute is None:
        one_minute_open = snapshot_time.replace(second=0, microsecond=0)
        selected_one_minute = make_kline(
            Interval.ONE_MINUTE,
            one_minute_open,
            current_thirty_minute.close,
            closed=False,
        )

    # 4시간봉은 current price source가 되고 1일봉은 같은 snapshot 시각을 포함한다.
    four_hour_kline = make_kline(
        Interval.FOUR_HOURS,
        FOUR_HOUR_CANDLE_OPEN,
        Decimal("5000"),
        closed=False,
    )
    one_day_kline = make_kline(
        Interval.ONE_DAY,
        ONE_DAY_CANDLE_OPEN,
        Decimal("5000"),
        closed=False,
    )
    return {
        Interval.ONE_MINUTE: (selected_one_minute,),
        Interval.THIRTY_MINUTES: (
            *closed_thirty_minute_klines,
            current_thirty_minute,
        ),
        Interval.FOUR_HOURS: (four_hour_kline,),
        Interval.ONE_DAY: (one_day_kline,),
    }


def commit_market_snapshot(
    market_snapshot: MarketSnapshot,
    utc_clock: MutableUtcClock,
    snapshot_time: datetime,
    closed_prices: Sequence[Decimal],
    current_thirty_minute: Kline,
    *,
    observed_kline: Kline | None = None,
    latest_one_minute: Kline | None = None,
) -> None:
    """
    함수 이름: commit_market_snapshot()
    기능: full 또는 live provenance를 가진 새 authoritative MarketSnapshot version을 commit한다.
    인자: market_snapshot -> 갱신할 mutable snapshot aggregate
        utc_clock -> update 시각을 제공하는 fixture clock
        snapshot_time -> 이번 version의 UTC 갱신 시각
        closed_prices -> 현재 30분봉 이전의 확정 종가
        current_thirty_minute -> 최신 30분 Kline
        observed_kline -> 이번 live version을 발생시킨 exact Kline 또는 full update용 None
        latest_one_minute -> mapping의 최신 1분 Kline 또는 자동 생성용 None
    반환값: 없음
    작성 날짜: 2026/08/29
    """
    # MarketSnapshot 시각과 네 interval payload를 먼저 같은 version 후보로 준비한다.
    utc_clock.set_time(snapshot_time)
    klines_by_interval = make_snapshot_klines(
        snapshot_time,
        closed_prices,
        current_thirty_minute,
        latest_one_minute=latest_one_minute,
    )

    # Live 호출만 source_klines를 기록하고 rebase용 full update는 provenance를 비워 둔다.
    if observed_kline is None:
        market_snapshot.update(klines_by_interval)
        return
    market_snapshot.update(
        klines_by_interval,
        source_klines=(observed_kline,),
    )


def make_rebased_builder(
    *,
    current_price: Decimal = Decimal("120"),
    monotonic_clock: MutableMonotonicClock | None = None,
) -> tuple[
    ThirtyMinuteMarketEvaluationBuilder,
    MarketSnapshot,
    MutableUtcClock,
]:
    """
    함수 이름: make_rebased_builder()
    기능: 20개 확정 30분봉과 한 진행봉으로 version 1 builder baseline을 만든다.
    인자: current_price -> 진행 중 30분봉의 최초 가격
        monotonic_clock -> 유지 조건을 제어할 clock 또는 기본 clock용 None
    반환값: rebased builder, authoritative snapshot과 mutable UTC clock tuple
    작성 날짜: 2026/08/29
    """
    # Full snapshot에는 event provenance를 넣지 않아 live source와 bootstrap을 구분한다.
    initial_time = CURRENT_CANDLE_OPEN + timedelta(minutes=4)
    utc_clock = MutableUtcClock(initial_time)
    market_snapshot = MarketSnapshot(clock=utc_clock)
    current_thirty_minute = make_kline(
        Interval.THIRTY_MINUTES,
        CURRENT_CANDLE_OPEN,
        current_price,
        closed=False,
    )
    commit_market_snapshot(
        market_snapshot,
        utc_clock,
        initial_time,
        BASE_CLOSED_PRICES,
        current_thirty_minute,
    )
    builder = ThirtyMinuteMarketEvaluationBuilder(
        monotonic_clock=monotonic_clock,
    )
    builder.rebase(market_snapshot)
    return builder, market_snapshot, utc_clock


def make_rebased_upper_boundary_builder(
    boundary_time: datetime,
    *,
    include_one_day: bool,
) -> tuple[
    ThirtyMinuteMarketEvaluationBuilder,
    MarketSnapshot,
    MutableUtcClock,
    Kline,
    tuple[Kline, ...],
]:
    """
    함수 이름: make_rebased_upper_boundary_builder()
    기능: 4H 또는 UTC 자정 canonical source tuple이 commit된 production builder fixture를 만든다.
    인자: boundary_time -> 4시간 정각 또는 UTC 자정 boundary
        include_one_day -> closed/open 1D source까지 포함할지 여부
    반환값: builder, snapshot/clock, primary 30분 close와 canonical source tuple
    작성 날짜: 2026/08/29
    """
    initial_time = boundary_time - timedelta(seconds=1)
    utc_clock = MutableUtcClock(initial_time)
    market_snapshot = MarketSnapshot(clock=utc_clock)
    current_thirty_minute_open = boundary_time - timedelta(minutes=30)
    current_thirty_minute = make_kline(
        Interval.THIRTY_MINUTES,
        current_thirty_minute_open,
        Decimal("120"),
        closed=False,
    )
    closed_count = len(BASE_CLOSED_PRICES)
    closed_thirty_minute_klines = tuple(
        make_kline(
            Interval.THIRTY_MINUTES,
            current_thirty_minute_open
            - timedelta(minutes=30 * (closed_count - index)),
            close_price,
            closed=True,
        )
        for index, close_price in enumerate(BASE_CLOSED_PRICES)
    )
    one_minute_open = make_kline(
        Interval.ONE_MINUTE,
        boundary_time - timedelta(minutes=1),
        Decimal("121"),
        closed=False,
    )
    four_hour_open = make_kline(
        Interval.FOUR_HOURS,
        boundary_time - timedelta(hours=4),
        Decimal("5000"),
        closed=False,
    )
    one_day_open_time = boundary_time.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    if include_one_day:
        one_day_open_time -= timedelta(days=1)
    one_day_open = make_kline(
        Interval.ONE_DAY,
        one_day_open_time,
        Decimal("5000"),
        closed=False,
    )

    # Source 없는 pre-boundary snapshot으로 EMA baseline과 현재 open invariants를 먼저 세운다.
    initial_klines = {
        Interval.ONE_MINUTE: (one_minute_open,),
        Interval.THIRTY_MINUTES: (
            *closed_thirty_minute_klines,
            current_thirty_minute,
        ),
        Interval.FOUR_HOURS: (four_hour_open,),
        Interval.ONE_DAY: (one_day_open,),
    }
    market_snapshot.update(initial_klines)
    builder = ThirtyMinuteMarketEvaluationBuilder()
    builder.rebase(market_snapshot)

    # 각 close/open E는 같은 boundary 뒤의 서로 다른 millisecond로 canonical provenance를 만든다.
    one_minute_close = make_kline(
        Interval.ONE_MINUTE,
        one_minute_open.open_time,
        Decimal("121"),
        closed=True,
        event_time=boundary_time + timedelta(milliseconds=100),
    )
    thirty_minute_close = make_kline(
        Interval.THIRTY_MINUTES,
        current_thirty_minute_open,
        Decimal("120"),
        closed=True,
        event_time=boundary_time + timedelta(milliseconds=200),
    )
    four_hour_close = make_kline(
        Interval.FOUR_HOURS,
        four_hour_open.open_time,
        Decimal("5000"),
        closed=True,
        event_time=boundary_time + timedelta(milliseconds=300),
    )
    next_four_hour_open = make_kline(
        Interval.FOUR_HOURS,
        boundary_time,
        Decimal("5001"),
        closed=False,
        event_time=boundary_time + timedelta(milliseconds=400),
    )
    canonical_sources: tuple[Kline, ...] = (
        one_minute_close,
        thirty_minute_close,
        four_hour_close,
        next_four_hour_open,
    )
    boundary_one_day_klines = (one_day_open,)
    if include_one_day:
        one_day_close = make_kline(
            Interval.ONE_DAY,
            one_day_open.open_time,
            Decimal("5000"),
            closed=True,
            event_time=boundary_time + timedelta(milliseconds=500),
        )
        next_one_day_open = make_kline(
            Interval.ONE_DAY,
            boundary_time,
            Decimal("5001"),
            closed=False,
            event_time=boundary_time + timedelta(milliseconds=600),
        )
        canonical_sources = (
            *canonical_sources,
            one_day_close,
            next_one_day_open,
        )
        boundary_one_day_klines = (
            one_day_close,
            next_one_day_open,
        )

    # Upper close/open과 strategy pair를 같은 MarketSnapshot version source로 원자 commit한다.
    utc_clock.set_time(boundary_time + timedelta(seconds=1))
    market_snapshot.update(
        {
            Interval.ONE_MINUTE: (one_minute_close,),
            Interval.THIRTY_MINUTES: (
                *closed_thirty_minute_klines,
                thirty_minute_close,
            ),
            Interval.FOUR_HOURS: (
                four_hour_close,
                next_four_hour_open,
            ),
            Interval.ONE_DAY: boundary_one_day_klines,
        },
        source_klines=canonical_sources,
    )
    return (
        builder,
        market_snapshot,
        utc_clock,
        thirty_minute_close,
        canonical_sources,
    )


def calculate_expected_candidate_slope(
    committed_ema_series: Sequence[Decimal],
    candidate_price: Decimal,
) -> Decimal:
    """
    함수 이름: calculate_expected_candidate_slope()
    기능: 후보 EMA를 한 번만 적용하고 그 후보 가격을 분모로 쓴 독립 기대 slope를 계산한다.
    인자: committed_ema_series -> 확정봉만 반영한 EMA9 시계열
        candidate_price -> realtime, TP 또는 확정 1분봉 실제 대입 가격
    반환값: raw slope / candidate_price * 100의 여덟 자리 Decimal
    작성 날짜: 2026/08/29
    """
    # 기대값 계산도 외부 Decimal context에 의존하지 않도록 production 정밀도를 명시한다.
    with localcontext() as decimal_context:
        decimal_context.prec = DECIMAL_PRECISION
        decimal_context.rounding = ROUND_HALF_EVEN
        candidate_ema = (
            Decimal("0.2") * candidate_price
            + Decimal("0.8") * committed_ema_series[-1]
        )
        candidate_values = (
            *committed_ema_series[-5:],
            candidate_ema,
        )
        raw_ols_slope = calculate_raw_ols_slope(candidate_values)
        return normalize_ols_slope(raw_ols_slope, candidate_price)


class ThirtyMinuteMarketEvaluationBuilderTests(unittest.TestCase):
    """
    클래스 이름: ThirtyMinuteMarketEvaluationBuilderTests
    기능: 30분 EMA9·OLS 수치와 source·시간·generation 경계 계약을 테스트한다.
    작성 날짜: 2026/08/29
    """

    def test_golden_linear_series_normalizes_by_candidate_price(self) -> None:
        """
        함수 이름: test_golden_linear_series_normalizes_by_candidate_price()
        기능: closed 100..112와 candidate 113의 raw 1 및 0.88495575 정규화를 고정한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # 첫 13개 선형 종가의 확정 EMA는 104..108이고 후보 EMA는 정확히 109다.
        close_prices = tuple(
            Decimal(100 + index)
            for index in range(13)
        )
        committed_ema_series = calculate_ema9_series(close_prices)
        candidate_values = (
            *committed_ema_series,
            Decimal("109"),
        )

        # x=0..5 raw slope와 실제 candidate 113 분모의 percent/30분봉 결과를 각각 검증한다.
        raw_ols_slope = calculate_raw_ols_slope(candidate_values)
        normalized_slope = calculate_candidate_ema9_slope(
            committed_ema_series,
            Decimal("113"),
        )
        self.assertEqual(
            committed_ema_series,
            tuple(Decimal(104 + index) for index in range(5)),
        )
        self.assertEqual(raw_ols_slope, Decimal("1"))
        self.assertEqual(normalized_slope, Decimal("0.88495575"))

    def test_ema_seed_and_slope_minimum_boundaries(self) -> None:
        """
        함수 이름: test_ema_seed_and_slope_minimum_boundaries()
        기능: EMA9 seed 9개, 후보 slope 13개와 확정 slope 14개 최소 입력을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # 아홉 종가의 산술 평균만 첫 EMA seed가 되며 여덟 종가는 즉시 거부한다.
        seed_prices = tuple(Decimal(index) for index in range(1, 10))
        self.assertEqual(
            calculate_ema9_series(seed_prices),
            (Decimal("5"),),
        )
        with self.assertRaises(ValueError):
            calculate_ema9_series(seed_prices[:-1])

        # 13개 종가는 후보용 다섯 EMA를, 14개 종가는 확정용 여섯 EMA를 정확히 만든다.
        thirteen_prices = tuple(
            Decimal(100 + index)
            for index in range(13)
        )
        fourteen_prices = (
            *thirteen_prices,
            Decimal("113"),
        )
        candidate_ema_series = calculate_ema9_series(thirteen_prices)
        fixed_ema_series = calculate_ema9_series(fourteen_prices)
        self.assertEqual(len(candidate_ema_series), 5)
        self.assertEqual(len(fixed_ema_series), 6)
        self.assertEqual(
            calculate_candidate_ema9_slope(
                candidate_ema_series,
                Decimal("113"),
            ),
            Decimal("0.88495575"),
        )
        self.assertEqual(
            calculate_normalized_ols_slope(
                fixed_ema_series,
                Decimal("113"),
            ),
            Decimal("0.88495575"),
        )

        # 다섯 EMA 미만의 후보와 여섯 EMA 미만의 OLS는 불충분 입력으로 닫힌다.
        with self.assertRaises(ValueError):
            calculate_candidate_ema9_slope(
                candidate_ema_series[:-1],
                Decimal("113"),
            )
        with self.assertRaises(ValueError):
            calculate_raw_ols_slope(candidate_ema_series)

    def test_candidate_updates_never_accumulate_previous_realtime_ema(self) -> None:
        """
        함수 이름: test_candidate_updates_never_accumulate_previous_realtime_ema()
        기능: 연속 30분 update가 매번 같은 마지막 확정 EMA에서 후보를 한 번만 적용하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        builder, market_snapshot, utc_clock = make_rebased_builder()

        # 첫 후보 130을 live source version으로 commit해 builder 내부 후보 계산을 한 번 실행한다.
        first_event_time = CURRENT_CANDLE_OPEN + timedelta(minutes=5)
        first_candidate = make_kline(
            Interval.THIRTY_MINUTES,
            CURRENT_CANDLE_OPEN,
            Decimal("130"),
            closed=False,
            event_time=first_event_time,
        )
        commit_market_snapshot(
            market_snapshot,
            utc_clock,
            first_event_time,
            BASE_CLOSED_PRICES,
            first_candidate,
            observed_kline=first_candidate,
        )
        first_evaluation = builder(market_snapshot, first_candidate)
        self.assertIsNotNone(first_evaluation)

        # 다음 후보 140은 첫 후보 EMA가 아니라 같은 확정 EMA 시계열에서 다시 계산해야 한다.
        second_event_time = CURRENT_CANDLE_OPEN + timedelta(minutes=6)
        second_candidate = make_kline(
            Interval.THIRTY_MINUTES,
            CURRENT_CANDLE_OPEN,
            Decimal("140"),
            closed=False,
            event_time=second_event_time,
        )
        commit_market_snapshot(
            market_snapshot,
            utc_clock,
            second_event_time,
            BASE_CLOSED_PRICES,
            second_candidate,
            observed_kline=second_candidate,
        )
        second_evaluation = builder(market_snapshot, second_candidate)
        self.assertIsNotNone(second_evaluation)

        # 독립 기대값과 일치하고 이전 후보를 누적한 반례와는 달라야 비누적 계약이 성립한다.
        committed_ema_series = calculate_ema9_series(BASE_CLOSED_PRICES)
        expected_first = calculate_expected_candidate_slope(
            committed_ema_series,
            Decimal("130"),
        )
        expected_second = calculate_expected_candidate_slope(
            committed_ema_series,
            Decimal("140"),
        )
        first_candidate_ema = (
            Decimal("0.2") * Decimal("130")
            + Decimal("0.8") * committed_ema_series[-1]
        )
        incorrectly_accumulated_ema = (
            Decimal("0.2") * Decimal("140")
            + Decimal("0.8") * first_candidate_ema
        )
        incorrectly_accumulated_slope = normalize_ols_slope(
            calculate_raw_ols_slope(
                (
                    *committed_ema_series[-5:],
                    incorrectly_accumulated_ema,
                )
            ),
            Decimal("140"),
        )
        self.assertEqual(first_evaluation.realtime_ema_slope, expected_first)
        self.assertEqual(second_evaluation.realtime_ema_slope, expected_second)
        self.assertNotEqual(
            second_evaluation.realtime_ema_slope,
            incorrectly_accumulated_slope,
        )

    def test_closed_thirty_minute_candle_is_committed_once(self) -> None:
        """
        함수 이름: test_closed_thirty_minute_candle_is_committed_once()
        기능: 실제 30분 close가 확정 EMA에 한 번만 들어가고 realtime 후보로 재적용되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        builder, market_snapshot, utc_clock = make_rebased_builder()

        # 12:30 close event는 기존 진행봉을 exact closed Kline으로 승격한 새 version을 만든다.
        close_event_time = CURRENT_CANDLE_OPEN + timedelta(minutes=30)
        closed_thirty_minute = make_kline(
            Interval.THIRTY_MINUTES,
            CURRENT_CANDLE_OPEN,
            Decimal("120"),
            closed=True,
            event_time=close_event_time,
        )
        commit_market_snapshot(
            market_snapshot,
            utc_clock,
            close_event_time,
            BASE_CLOSED_PRICES,
            closed_thirty_minute,
            observed_kline=closed_thirty_minute,
        )
        evaluation = builder(market_snapshot, closed_thirty_minute)
        self.assertIsNotNone(evaluation)

        # 확정 series는 close를 포함하고 realtime slope는 그 확정값과 exact 동일해야 한다.
        all_closed_prices = (
            *BASE_CLOSED_PRICES,
            Decimal("120"),
        )
        committed_ema_series = calculate_ema9_series(all_closed_prices)
        expected_confirmed = normalize_ols_slope(
            calculate_raw_ols_slope(committed_ema_series[-6:]),
            Decimal("120"),
        )
        incorrectly_double_applied = calculate_expected_candidate_slope(
            committed_ema_series,
            Decimal("120"),
        )
        tp_price = evaluation.lower_band + Decimal("0.10") * (
            evaluation.upper_band - evaluation.lower_band
        )
        pre_close_ema_series = calculate_ema9_series(BASE_CLOSED_PRICES)
        expected_tp_reference = calculate_expected_candidate_slope(
            pre_close_ema_series,
            tp_price,
        )
        incorrectly_committed_tp_reference = calculate_expected_candidate_slope(
            committed_ema_series,
            tp_price,
        )
        self.assertTrue(evaluation.confirmed_30m_close)
        self.assertEqual(evaluation.ema_slope_30m_close, expected_confirmed)
        self.assertEqual(evaluation.realtime_ema_slope, expected_confirmed)
        self.assertNotEqual(
            evaluation.realtime_ema_slope,
            incorrectly_double_applied,
        )

        # 같은 close event의 TP 후보는 해당 봉 actual EMA가 아니라 직전 확정 EMA에서 치환한다.
        self.assertEqual(
            evaluation.tp_reference_ema_slope,
            expected_tp_reference,
        )
        self.assertNotEqual(
            evaluation.tp_reference_ema_slope,
            incorrectly_committed_tp_reference,
        )

    def test_lower_bb_bands_pct_b_bbw_and_cci_use_standard_formulas(self) -> None:
        """
        함수 이름: test_lower_bb_bands_pct_b_bbw_and_cci_use_standard_formulas()
        기능: Lower_bb의 20기간 population 2σ, %B, BBW와 표준 CCI20 수식을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        builder, market_snapshot, utc_clock = make_rebased_builder()

        # 현재 30분 임시 close 130을 마지막 19개 확정 종가 뒤에 넣은 public tick을 만든다.
        event_time = CURRENT_CANDLE_OPEN + timedelta(minutes=5)
        current_thirty_minute = make_kline(
            Interval.THIRTY_MINUTES,
            CURRENT_CANDLE_OPEN,
            Decimal("130"),
            closed=False,
            event_time=event_time,
        )
        commit_market_snapshot(
            market_snapshot,
            utc_clock,
            event_time,
            BASE_CLOSED_PRICES,
            current_thirty_minute,
            observed_kline=current_thirty_minute,
        )
        evaluation = builder(market_snapshot, current_thirty_minute)
        self.assertIsNotNone(evaluation)

        # Fixture high·low가 close±1이므로 typical price도 close와 같아 독립 CCI 계산이 단순해진다.
        realtime_window = (
            *BASE_CLOSED_PRICES[-19:],
            Decimal("130"),
        )
        confirmed_window = BASE_CLOSED_PRICES[-20:]
        with localcontext() as decimal_context:
            decimal_context.prec = DECIMAL_PRECISION
            decimal_context.rounding = ROUND_HALF_EVEN
            realtime_middle = sum(
                realtime_window,
                Decimal("0"),
            ) / Decimal("20")
            realtime_variance = sum(
                (
                    (price - realtime_middle) ** 2
                    for price in realtime_window
                ),
                Decimal("0"),
            ) / Decimal("20")
            realtime_deviation = realtime_variance.sqrt()
            expected_lower = realtime_middle - Decimal("2") * realtime_deviation
            expected_upper = realtime_middle + Decimal("2") * realtime_deviation
            expected_bbw = (
                expected_upper - expected_lower
            ) / realtime_middle
            expected_realtime_pct_b = (
                Decimal("130") - expected_lower
            ) / (expected_upper - expected_lower)
            mean_deviation = sum(
                (
                    abs(price - realtime_middle)
                    for price in realtime_window
                ),
                Decimal("0"),
            ) / Decimal("20")
            expected_cci = (
                Decimal("130") - realtime_middle
            ) / (Decimal("0.015") * mean_deviation)

            confirmed_middle = sum(
                confirmed_window,
                Decimal("0"),
            ) / Decimal("20")
            confirmed_variance = sum(
                (
                    (price - confirmed_middle) ** 2
                    for price in confirmed_window
                ),
                Decimal("0"),
            ) / Decimal("20")
            confirmed_deviation = confirmed_variance.sqrt()
            confirmed_lower = (
                confirmed_middle - Decimal("2") * confirmed_deviation
            )
            confirmed_upper = (
                confirmed_middle + Decimal("2") * confirmed_deviation
            )
            expected_pct_b_close = (
                BASE_CLOSED_PRICES[-1] - confirmed_lower
            ) / (confirmed_upper - confirmed_lower)

        # Builder 결과가 표준 식의 독립 Decimal 계산과 중간 quantize 없이 exact 일치해야 한다.
        self.assertEqual(evaluation.lower_band, expected_lower)
        self.assertEqual(evaluation.upper_band, expected_upper)
        self.assertEqual(evaluation.touch_candle_bbw, expected_bbw)
        self.assertEqual(
            evaluation.realtime_pct_b,
            expected_realtime_pct_b,
        )
        self.assertEqual(evaluation.cci_30m_realtime, expected_cci)
        self.assertEqual(evaluation.pct_b_close, expected_pct_b_close)

    def test_four_slopes_use_their_exact_candidate_price_denominators(self) -> None:
        """
        함수 이름: test_four_slopes_use_their_exact_candidate_price_denominators()
        기능: 확정·realtime·TP·확정 1분 slope가 각각 실제 대입 가격을 분모로 쓰는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        builder, market_snapshot, utc_clock = make_rebased_builder(
            current_price=Decimal("130")
        )

        # 현재 30분 가격 130과 구별되는 확정 1분 종가 125를 live provenance로 commit한다.
        one_minute_open = CURRENT_CANDLE_OPEN + timedelta(minutes=5)
        one_minute_event_time = one_minute_open + timedelta(minutes=1)
        closed_one_minute = make_kline(
            Interval.ONE_MINUTE,
            one_minute_open,
            Decimal("125"),
            closed=True,
            event_time=one_minute_event_time,
        )
        current_thirty_minute = make_kline(
            Interval.THIRTY_MINUTES,
            CURRENT_CANDLE_OPEN,
            Decimal("130"),
            closed=False,
        )
        commit_market_snapshot(
            market_snapshot,
            utc_clock,
            one_minute_event_time,
            BASE_CLOSED_PRICES,
            current_thirty_minute,
            observed_kline=closed_one_minute,
            latest_one_minute=closed_one_minute,
        )
        evaluation = builder(market_snapshot, closed_one_minute)
        self.assertIsNotNone(evaluation)

        # 네 numerator를 독립 계산한 뒤 각 calculation에 실제 대입된 가격으로만 정규화한다.
        committed_ema_series = calculate_ema9_series(BASE_CLOSED_PRICES)
        fixed_raw_slope = calculate_raw_ols_slope(
            committed_ema_series[-6:]
        )
        tp_price = evaluation.lower_band + Decimal("0.10") * (
            evaluation.upper_band - evaluation.lower_band
        )
        expected_confirmed = normalize_ols_slope(
            fixed_raw_slope,
            BASE_CLOSED_PRICES[-1],
        )
        expected_realtime = calculate_expected_candidate_slope(
            committed_ema_series,
            Decimal("130"),
        )
        expected_tp_reference = calculate_expected_candidate_slope(
            committed_ema_series,
            tp_price,
        )
        expected_closed_one_minute = calculate_expected_candidate_slope(
            committed_ema_series,
            Decimal("125"),
        )
        self.assertEqual(evaluation.realtime_price, Decimal("130"))
        self.assertEqual(evaluation.ema_slope_30m_close, expected_confirmed)
        self.assertEqual(evaluation.realtime_ema_slope, expected_realtime)
        self.assertEqual(
            evaluation.tp_reference_ema_slope,
            expected_tp_reference,
        )
        self.assertEqual(
            evaluation.current_close_ema_slope,
            expected_closed_one_minute,
        )
        self.assertTrue(evaluation.confirmed_1m_close)

    def test_decimal_calculation_isolated_from_external_context(self) -> None:
        """
        함수 이름: test_decimal_calculation_isolated_from_external_context()
        기능: 낮은 외부 Decimal precision과 다른 rounding이 golden slope를 바꾸지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        close_prices = tuple(
            Decimal(100 + index)
            for index in range(13)
        )

        # 호출자 context를 고의로 precision 6·ROUND_DOWN으로 낮춰 내부 localcontext 경계를 시험한다.
        with localcontext() as decimal_context:
            decimal_context.prec = 6
            decimal_context.rounding = ROUND_DOWN
            committed_ema_series = calculate_ema9_series(close_prices)
            raw_ols_slope = calculate_raw_ols_slope(
                (
                    *committed_ema_series,
                    Decimal("109"),
                )
            )
            normalized_slope = normalize_ols_slope(
                raw_ols_slope,
                Decimal("113"),
            )

        # 내부 유효숫자 34와 최종 HALF_EVEN quantize가 외부 설정을 완전히 차단해야 한다.
        self.assertEqual(raw_ols_slope, Decimal("1"))
        self.assertEqual(normalized_slope, Decimal("0.88495575"))

    def test_normalized_slope_uses_exact_half_even_quantization(self) -> None:
        """
        함수 이름: test_normalized_slope_uses_exact_half_even_quantization()
        기능: 소수점 아홉째 자리 exact tie를 1e-8 ROUND_HALF_EVEN으로 반올림하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # candidate 100은 raw/100*100을 그대로 보존해 quantize tie만 독립적으로 드러낸다.
        quantization_cases = (
            (Decimal("1.234567845"), Decimal("1.23456784")),
            (Decimal("1.234567855"), Decimal("1.23456786")),
            (Decimal("-1.234567845"), Decimal("-1.23456784")),
            (Decimal("-1.234567855"), Decimal("-1.23456786")),
        )

        # 양수·음수의 even/down과 odd/up tie를 표 기반으로 모두 고정한다.
        for raw_ols_slope, expected_slope in quantization_cases:
            with self.subTest(raw_ols_slope=raw_ols_slope):
                normalized_slope = normalize_ols_slope(
                    raw_ols_slope,
                    Decimal("100"),
                )
                self.assertEqual(normalized_slope, expected_slope)
                self.assertEqual(
                    normalized_slope.as_tuple().exponent,
                    -8,
                )

    def test_condition_thresholds_and_durations_through_public_ticks(self) -> None:
        """
        함수 이름: test_condition_thresholds_and_durations_through_public_ticks()
        기능: 다섯 조건의 strict·inclusive 경계와 5초·3분 exact duration을 public tick으로 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # 각 행은 대상 flag, 만족 경계측 값, 불만족 반대측 값과 exact 유지 시간을 정의한다.
        condition_cases = (
            (
                "pct_b_at_least_060_for_5s",
                Decimal("0.60"),
                Decimal("0.05"),
                Decimal("0.59999999"),
                Decimal("0.05"),
                5_000_000_000,
            ),
            (
                "pct_b_below_060_for_5s",
                Decimal("0.59999999"),
                Decimal("0.05"),
                Decimal("0.60"),
                Decimal("0.05"),
                5_000_000_000,
            ),
            (
                "realtime_slope_above_008_for_5s",
                Decimal("0.50"),
                Decimal("0.08000001"),
                Decimal("0.50"),
                Decimal("0.08"),
                5_000_000_000,
            ),
            (
                "realtime_slope_at_most_004_for_5s",
                Decimal("0.50"),
                Decimal("0.04"),
                Decimal("0.50"),
                Decimal("0.04000001"),
                5_000_000_000,
            ),
            (
                "realtime_slope_at_most_minus_055_for_3m",
                Decimal("0.50"),
                Decimal("-0.55"),
                Decimal("0.50"),
                Decimal("-0.54999999"),
                180_000_000_000,
            ),
        )

        # 각 조건은 독립 generation으로 실행해 다른 threshold의 timer 상태가 섞이지 않게 한다.
        for (
            flag_name,
            satisfied_pct_b,
            satisfied_slope,
            outside_pct_b,
            outside_slope,
            duration_nanoseconds,
        ) in condition_cases:
            with self.subTest(flag_name=flag_name):
                initial_time = CURRENT_CANDLE_OPEN + timedelta(minutes=4)
                utc_clock = MutableUtcClock(initial_time)
                monotonic_clock = MutableMonotonicClock()
                market_snapshot = MarketSnapshot(clock=utc_clock)
                initial_thirty_minute = make_kline(
                    Interval.THIRTY_MINUTES,
                    CURRENT_CANDLE_OPEN,
                    Decimal("120"),
                    closed=False,
                )
                commit_market_snapshot(
                    market_snapshot,
                    utc_clock,
                    initial_time,
                    BASE_CLOSED_PRICES,
                    initial_thirty_minute,
                )
                builder = ControlledConditionBuilder(monotonic_clock)
                builder.rebase(market_snapshot)

                # 첫 만족 tick은 시작점만 만들므로 inclusive 여부와 무관하게 duration flag는 False다.
                builder.set_condition_values(
                    satisfied_pct_b,
                    satisfied_slope,
                )
                first_event_time = CURRENT_CANDLE_OPEN + timedelta(minutes=5)
                first_event = make_kline(
                    Interval.THIRTY_MINUTES,
                    CURRENT_CANDLE_OPEN,
                    Decimal("120"),
                    closed=False,
                    event_time=first_event_time,
                )
                commit_market_snapshot(
                    market_snapshot,
                    utc_clock,
                    first_event_time,
                    BASE_CLOSED_PRICES,
                    first_event,
                    observed_kline=first_event,
                )
                first_evaluation = builder(market_snapshot, first_event)
                self.assertIsNotNone(first_evaluation)
                self.assertFalse(getattr(first_evaluation, flag_name))

                # exact duration 1ns 전까지는 동일한 만족 값이 지속돼도 flag가 열리지 않는다.
                monotonic_clock.set_time(duration_nanoseconds - 1)
                before_event_time = CURRENT_CANDLE_OPEN + timedelta(minutes=6)
                before_event = make_kline(
                    Interval.THIRTY_MINUTES,
                    CURRENT_CANDLE_OPEN,
                    Decimal("120"),
                    closed=False,
                    event_time=before_event_time,
                )
                commit_market_snapshot(
                    market_snapshot,
                    utc_clock,
                    before_event_time,
                    BASE_CLOSED_PRICES,
                    before_event,
                    observed_kline=before_event,
                )
                before_evaluation = builder(market_snapshot, before_event)
                self.assertIsNotNone(before_evaluation)
                self.assertFalse(getattr(before_evaluation, flag_name))

                # exact 5초 또는 3분 tick에서는 경계를 포함해 처음 True가 된다.
                monotonic_clock.set_time(duration_nanoseconds)
                exact_event_time = CURRENT_CANDLE_OPEN + timedelta(minutes=7)
                exact_event = make_kline(
                    Interval.THIRTY_MINUTES,
                    CURRENT_CANDLE_OPEN,
                    Decimal("120"),
                    closed=False,
                    event_time=exact_event_time,
                )
                commit_market_snapshot(
                    market_snapshot,
                    utc_clock,
                    exact_event_time,
                    BASE_CLOSED_PRICES,
                    exact_event,
                    observed_kline=exact_event,
                )
                exact_evaluation = builder(market_snapshot, exact_event)
                self.assertIsNotNone(exact_evaluation)
                self.assertTrue(getattr(exact_evaluation, flag_name))

                # 반대쪽 한 단위 값은 이미 성숙한 timer도 즉시 reset해 strict·inclusive 방향을 고정한다.
                builder.set_condition_values(
                    outside_pct_b,
                    outside_slope,
                )
                monotonic_clock.set_time(duration_nanoseconds + 1)
                outside_event_time = CURRENT_CANDLE_OPEN + timedelta(minutes=8)
                outside_event = make_kline(
                    Interval.THIRTY_MINUTES,
                    CURRENT_CANDLE_OPEN,
                    Decimal("120"),
                    closed=False,
                    event_time=outside_event_time,
                )
                commit_market_snapshot(
                    market_snapshot,
                    utc_clock,
                    outside_event_time,
                    BASE_CLOSED_PRICES,
                    outside_event,
                    observed_kline=outside_event,
                )
                outside_evaluation = builder(
                    market_snapshot,
                    outside_event,
                )
                self.assertIsNotNone(outside_evaluation)
                self.assertFalse(getattr(outside_evaluation, flag_name))

    def test_only_thirty_minute_ticks_advance_monotonic_conditions(self) -> None:
        """
        함수 이름: test_only_thirty_minute_ticks_advance_monotonic_conditions()
        기능: 확정 1분봉은 조건 상태를 읽기만 하고 30분 tick만 5초 timer를 전진시키는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        monotonic_clock = MutableMonotonicClock()
        builder, market_snapshot, utc_clock = make_rebased_builder(
            monotonic_clock=monotonic_clock
        )

        # 첫 30분 tick은 상승 slope 조건의 시작점을 0ns에 만들지만 즉시 만족시키지는 않는다.
        first_event_time = CURRENT_CANDLE_OPEN + timedelta(minutes=5)
        first_thirty_minute_tick = make_kline(
            Interval.THIRTY_MINUTES,
            CURRENT_CANDLE_OPEN,
            Decimal("120"),
            closed=False,
            event_time=first_event_time,
        )
        commit_market_snapshot(
            market_snapshot,
            utc_clock,
            first_event_time,
            BASE_CLOSED_PRICES,
            first_thirty_minute_tick,
            observed_kline=first_thirty_minute_tick,
        )
        first_evaluation = builder(
            market_snapshot,
            first_thirty_minute_tick,
        )
        self.assertIsNotNone(first_evaluation)
        self.assertFalse(first_evaluation.realtime_slope_above_008_for_5s)
        self.assertEqual(monotonic_clock.call_count, 1)

        # clock 값을 100초로 바꿔도 중간 1분 close는 clock을 호출하거나 flag를 성숙시키면 안 된다.
        monotonic_clock.set_time(100_000_000_000)
        one_minute_open = CURRENT_CANDLE_OPEN + timedelta(minutes=5)
        one_minute_event_time = one_minute_open + timedelta(minutes=1)
        closed_one_minute = make_kline(
            Interval.ONE_MINUTE,
            one_minute_open,
            Decimal("121"),
            closed=True,
            event_time=one_minute_event_time,
        )
        current_thirty_minute = make_kline(
            Interval.THIRTY_MINUTES,
            CURRENT_CANDLE_OPEN,
            Decimal("120"),
            closed=False,
            event_time=first_event_time,
        )
        commit_market_snapshot(
            market_snapshot,
            utc_clock,
            one_minute_event_time,
            BASE_CLOSED_PRICES,
            current_thirty_minute,
            observed_kline=closed_one_minute,
            latest_one_minute=closed_one_minute,
        )
        one_minute_evaluation = builder(
            market_snapshot,
            closed_one_minute,
        )
        self.assertIsNotNone(one_minute_evaluation)
        self.assertFalse(
            one_minute_evaluation.realtime_slope_above_008_for_5s
        )
        self.assertEqual(monotonic_clock.call_count, 1)

        # 다음 30분 tick이 처음으로 100초를 읽으면 같은 candle 조건이 5초를 넘겨 True가 된다.
        second_event_time = CURRENT_CANDLE_OPEN + timedelta(minutes=7)
        second_thirty_minute_tick = make_kline(
            Interval.THIRTY_MINUTES,
            CURRENT_CANDLE_OPEN,
            Decimal("120"),
            closed=False,
            event_time=second_event_time,
        )
        commit_market_snapshot(
            market_snapshot,
            utc_clock,
            second_event_time,
            BASE_CLOSED_PRICES,
            second_thirty_minute_tick,
            observed_kline=second_thirty_minute_tick,
        )
        second_evaluation = builder(
            market_snapshot,
            second_thirty_minute_tick,
        )
        self.assertIsNotNone(second_evaluation)
        self.assertTrue(
            second_evaluation.realtime_slope_above_008_for_5s
        )
        self.assertEqual(monotonic_clock.call_count, 2)

    def test_closed_one_minute_binds_only_to_current_thirty_minute(self) -> None:
        """
        함수 이름: test_closed_one_minute_binds_only_to_current_thirty_minute()
        기능: 현재 30분봉 내부의 확정 1분봉만 평가하고 늦은 직전 봉 close는 보류하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        builder, market_snapshot, utc_clock = make_rebased_builder()

        # 12:05 1분봉은 12:00~12:30 진행봉에 속하므로 trailing slope 평가를 생성한다.
        bound_one_minute_open = CURRENT_CANDLE_OPEN + timedelta(minutes=5)
        bound_event_time = bound_one_minute_open + timedelta(minutes=1)
        bound_one_minute = make_kline(
            Interval.ONE_MINUTE,
            bound_one_minute_open,
            Decimal("121"),
            closed=True,
            event_time=bound_event_time,
        )
        current_thirty_minute = make_kline(
            Interval.THIRTY_MINUTES,
            CURRENT_CANDLE_OPEN,
            Decimal("120"),
            closed=False,
        )
        commit_market_snapshot(
            market_snapshot,
            utc_clock,
            bound_event_time,
            BASE_CLOSED_PRICES,
            current_thirty_minute,
            observed_kline=bound_one_minute,
            latest_one_minute=bound_one_minute,
        )
        bound_evaluation = builder(market_snapshot, bound_one_minute)
        self.assertIsNotNone(bound_evaluation)
        self.assertTrue(bound_evaluation.confirmed_1m_close)

        # 12:30 진행봉이 먼저 들어온 뒤 도착한 12:29 close는 직전 30분봉 provenance이므로 None이다.
        next_candle_open = CURRENT_CANDLE_OPEN + timedelta(minutes=30)
        late_one_minute_open = next_candle_open - timedelta(minutes=1)
        late_event_time = next_candle_open + timedelta(minutes=1)
        late_one_minute = make_kline(
            Interval.ONE_MINUTE,
            late_one_minute_open,
            Decimal("122"),
            closed=True,
            event_time=late_event_time,
        )
        next_thirty_minute = make_kline(
            Interval.THIRTY_MINUTES,
            next_candle_open,
            Decimal("121"),
            closed=False,
        )
        next_closed_prices = (
            *BASE_CLOSED_PRICES,
            Decimal("120"),
        )
        commit_market_snapshot(
            market_snapshot,
            utc_clock,
            late_event_time,
            next_closed_prices,
            next_thirty_minute,
            observed_kline=late_one_minute,
            latest_one_minute=late_one_minute,
        )
        late_evaluation = builder(market_snapshot, late_one_minute)
        self.assertIsNone(late_evaluation)

    def test_rejects_ambiguous_strategy_sources_without_committing_state(self) -> None:
        """
        함수 이름: test_rejects_ambiguous_strategy_sources_without_committing_state()
        기능: 같은 version의 1분·30분 source 충돌을 거부하고 다음 정상 version을 처리하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        builder, market_snapshot, utc_clock = make_rebased_builder()

        # 같은 snapshot version에 30분 tick과 확정 1분봉을 함께 원인으로 기록해 모호성을 만든다.
        conflict_time = CURRENT_CANDLE_OPEN + timedelta(minutes=6)
        thirty_minute_source = make_kline(
            Interval.THIRTY_MINUTES,
            CURRENT_CANDLE_OPEN,
            Decimal("120"),
            closed=False,
            event_time=conflict_time,
        )
        one_minute_source = make_kline(
            Interval.ONE_MINUTE,
            CURRENT_CANDLE_OPEN + timedelta(minutes=5),
            Decimal("121"),
            closed=True,
            event_time=conflict_time,
        )
        utc_clock.set_time(conflict_time)
        conflicting_klines = make_snapshot_klines(
            conflict_time,
            BASE_CLOSED_PRICES,
            thirty_minute_source,
            latest_one_minute=one_minute_source,
        )
        market_snapshot.update(
            conflicting_klines,
            source_klines=(thirty_minute_source, one_minute_source),
        )

        # 어느 source를 먼저 전달해도 confirmed 의미가 달라질 수 있으므로 계산 전 fail closed한다.
        with self.assertRaises(MarketEvaluationCalculationError):
            builder(market_snapshot, thirty_minute_source)
        with self.assertRaises(MarketEvaluationCalculationError):
            builder(market_snapshot, one_minute_source)

        # 거부된 version은 builder state를 commit하지 않아 다음 단일-source version을 정상 처리한다.
        valid_time = CURRENT_CANDLE_OPEN + timedelta(minutes=7)
        valid_source = make_kline(
            Interval.THIRTY_MINUTES,
            CURRENT_CANDLE_OPEN,
            Decimal("122"),
            closed=False,
            event_time=valid_time,
        )
        commit_market_snapshot(
            market_snapshot,
            utc_clock,
            valid_time,
            BASE_CLOSED_PRICES,
            valid_source,
            observed_kline=valid_source,
        )
        valid_evaluation = builder(market_snapshot, valid_source)
        self.assertIsNotNone(valid_evaluation)
        self.assertEqual(valid_evaluation.realtime_price, Decimal("122"))

    def test_atomic_boundary_pair_confirms_both_closes_without_double_apply(
        self,
    ) -> None:
        """
        함수 이름: test_atomic_boundary_pair_confirms_both_closes_without_double_apply()
        기능: canonical 경계 pair가 두 close를 확인하고 1분 후보에 새 30분 EMA를 중복 적용하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        builder, market_snapshot, utc_clock = make_rebased_builder()

        # 12:29 1분 close와 12:00 30분 close를 서로 다른 E와 canonical source 순서로 만든다.
        boundary_time = CURRENT_CANDLE_OPEN + timedelta(minutes=30)
        one_minute_close = make_kline(
            Interval.ONE_MINUTE,
            boundary_time - timedelta(minutes=1),
            Decimal("121"),
            closed=True,
            event_time=boundary_time + timedelta(milliseconds=100),
        )
        thirty_minute_close = make_kline(
            Interval.THIRTY_MINUTES,
            CURRENT_CANDLE_OPEN,
            Decimal("120"),
            closed=True,
            event_time=boundary_time + timedelta(milliseconds=200),
        )
        utc_clock.set_time(boundary_time + timedelta(seconds=1))
        boundary_klines = make_snapshot_klines(
            boundary_time + timedelta(seconds=1),
            BASE_CLOSED_PRICES,
            thirty_minute_close,
            latest_one_minute=one_minute_close,
        )
        market_snapshot.update(
            boundary_klines,
            source_klines=(one_minute_close, thirty_minute_close),
        )

        # 한 evaluation이 actual 30분 close와 같은 경계의 1분 trailing close를 함께 확인한다.
        evaluation = builder(market_snapshot, thirty_minute_close)
        self.assertIsNotNone(evaluation)
        self.assertTrue(evaluation.confirmed_30m_close)
        self.assertTrue(evaluation.confirmed_1m_close)
        self.assertEqual(evaluation.realtime_price, Decimal("120"))
        self.assertEqual(
            market_snapshot.update_source_klines,
            (one_minute_close, thirty_minute_close),
        )

        # Actual slope는 새 close를 commit하지만 1분 후보는 직전 확정 EMA base에서 한 번만 계산한다.
        committed_ema_series = calculate_ema9_series(
            (*BASE_CLOSED_PRICES, Decimal("120"))
        )
        expected_actual_slope = calculate_normalized_ols_slope(
            committed_ema_series[-6:],
            Decimal("120"),
        )
        previous_ema_series = calculate_ema9_series(BASE_CLOSED_PRICES)
        expected_one_minute_slope = calculate_expected_candidate_slope(
            previous_ema_series,
            Decimal("121"),
        )
        self.assertEqual(
            evaluation.ema_slope_30m_close,
            expected_actual_slope,
        )
        self.assertEqual(
            evaluation.current_close_ema_slope,
            expected_one_minute_slope,
        )

    def test_canonical_upper_boundary_tuples_evaluate_once_without_double_apply(
        self,
    ) -> None:
        """
        함수 이름: test_canonical_upper_boundary_tuples_evaluate_once_without_double_apply()
        기능: exact 4H·1D tuple이 strategy pair를 한 번 사용하고 30분 close를 중복 적용하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        boundary_cases = (
            (
                datetime(2026, 8, 29, 16, 0, tzinfo=timezone.utc),
                False,
                4,
            ),
            (
                datetime(2026, 8, 30, 0, 0, tzinfo=timezone.utc),
                True,
                6,
            ),
        )
        for boundary_time, include_one_day, source_count in boundary_cases:
            with self.subTest(boundary_time=boundary_time):
                (
                    builder,
                    market_snapshot,
                    _utc_clock,
                    thirty_minute_close,
                    canonical_sources,
                ) = make_rebased_upper_boundary_builder(
                    boundary_time,
                    include_one_day=include_one_day,
                )

                # Builder는 upper source를 독립 trigger로 재평가하지 않고 primary 30분 close 한 번만 읽는다.
                evaluation = builder(
                    market_snapshot,
                    thirty_minute_close,
                )
                self.assertIsNotNone(evaluation)
                self.assertEqual(len(canonical_sources), source_count)
                self.assertTrue(evaluation.confirmed_1m_close)
                self.assertTrue(evaluation.confirmed_30m_close)
                self.assertEqual(
                    market_snapshot.update_source_klines,
                    canonical_sources,
                )

                # 1분 trailing 후보는 새 30분 close 전 EMA base에서 한 번만 계산한다.
                expected_one_minute_slope = (
                    calculate_expected_candidate_slope(
                        calculate_ema9_series(BASE_CLOSED_PRICES),
                        Decimal("121"),
                    )
                )
                self.assertEqual(
                    evaluation.current_close_ema_slope,
                    expected_one_minute_slope,
                )

    def test_rejects_noncanonical_upper_boundary_source_shapes(self) -> None:
        """
        함수 이름: test_rejects_noncanonical_upper_boundary_source_shapes()
        기능: 4H close/open 순서 변경과 UTC 자정의 1D pair 누락을 임의 extra source로 허용하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        invalid_cases = (
            (
                datetime(2026, 8, 29, 16, 0, tzinfo=timezone.utc),
                False,
                "swapped_four_hour_pair",
            ),
            (
                datetime(2026, 8, 30, 0, 0, tzinfo=timezone.utc),
                True,
                "missing_one_day_pair",
            ),
        )
        for boundary_time, include_one_day, invalid_shape in invalid_cases:
            with self.subTest(invalid_shape=invalid_shape):
                (
                    builder,
                    market_snapshot,
                    utc_clock,
                    thirty_minute_close,
                    canonical_sources,
                ) = make_rebased_upper_boundary_builder(
                    boundary_time,
                    include_one_day=include_one_day,
                )
                if invalid_shape == "swapped_four_hour_pair":
                    invalid_sources = (
                        *canonical_sources[:2],
                        canonical_sources[3],
                        canonical_sources[2],
                    )
                else:
                    invalid_sources = canonical_sources[:4]

                # 동일 history를 새 version으로 유지하되 source tuple 형태만 손상시켜 resolver를 시험한다.
                utc_clock.set_time(boundary_time + timedelta(seconds=2))
                market_snapshot.update(
                    market_snapshot.klines_by_interval,
                    source_klines=invalid_sources,
                )
                with self.assertRaises(MarketEvaluationCalculationError):
                    builder(market_snapshot, thirty_minute_close)

    def test_rejects_closed_source_before_actual_close_boundary(self) -> None:
        """
        함수 이름: test_rejects_closed_source_before_actual_close_boundary()
        기능: open 이후지만 실제 close 전인 x=true source를 거부하고 정상 수정본을 처리한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        builder, market_snapshot, utc_clock = make_rebased_builder()

        # 12:05 1분봉의 E를 12:06 close boundary 직전으로 두어 형식만 맞는 조기 확정을 만든다.
        one_minute_open = CURRENT_CANDLE_OPEN + timedelta(minutes=5)
        invalid_one_minute_close = make_kline(
            Interval.ONE_MINUTE,
            one_minute_open,
            Decimal("121"),
            closed=True,
            event_time=one_minute_open + timedelta(seconds=59),
        )
        current_thirty_minute = make_kline(
            Interval.THIRTY_MINUTES,
            CURRENT_CANDLE_OPEN,
            Decimal("120"),
            closed=False,
        )
        snapshot_time = one_minute_open + timedelta(minutes=1, seconds=1)
        commit_market_snapshot(
            market_snapshot,
            utc_clock,
            snapshot_time,
            BASE_CLOSED_PRICES,
            current_thirty_minute,
            observed_kline=invalid_one_minute_close,
            latest_one_minute=invalid_one_minute_close,
        )
        with self.assertRaises(MarketEvaluationCalculationError):
            builder(market_snapshot, invalid_one_minute_close)

        # 같은 봉의 E만 close boundary 이후로 고친 다음 version은 실패 version에 막히지 않는다.
        valid_one_minute_close = make_kline(
            Interval.ONE_MINUTE,
            one_minute_open,
            Decimal("121"),
            closed=True,
            event_time=one_minute_open + timedelta(minutes=1),
        )
        commit_market_snapshot(
            market_snapshot,
            utc_clock,
            snapshot_time,
            BASE_CLOSED_PRICES,
            current_thirty_minute,
            observed_kline=valid_one_minute_close,
            latest_one_minute=valid_one_minute_close,
        )
        valid_evaluation = builder(
            market_snapshot,
            valid_one_minute_close,
        )
        self.assertIsNotNone(valid_evaluation)
        self.assertTrue(valid_evaluation.confirmed_1m_close)

    def test_rejects_boundary_pair_companion_before_shared_close(self) -> None:
        """
        함수 이름: test_rejects_boundary_pair_companion_before_shared_close()
        기능: primary 30분 source가 정상이어도 조기 final 1분 companion이 있는 pair를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        builder, market_snapshot, utc_clock = make_rebased_builder()
        boundary_time = CURRENT_CANDLE_OPEN + timedelta(minutes=30)

        # Companion E는 자신의 open 이후지만 공통 close boundary보다 1ms 빠르다.
        invalid_one_minute_close = make_kline(
            Interval.ONE_MINUTE,
            boundary_time - timedelta(minutes=1),
            Decimal("121"),
            closed=True,
            event_time=boundary_time - timedelta(milliseconds=1),
        )
        thirty_minute_close = make_kline(
            Interval.THIRTY_MINUTES,
            CURRENT_CANDLE_OPEN,
            Decimal("120"),
            closed=True,
            event_time=boundary_time + timedelta(milliseconds=200),
        )
        utc_clock.set_time(boundary_time + timedelta(seconds=1))
        boundary_klines = make_snapshot_klines(
            boundary_time + timedelta(seconds=1),
            BASE_CLOSED_PRICES,
            thirty_minute_close,
            latest_one_minute=invalid_one_minute_close,
        )
        market_snapshot.update(
            boundary_klines,
            source_klines=(
                invalid_one_minute_close,
                thirty_minute_close,
            ),
        )
        with self.assertRaises(MarketEvaluationCalculationError):
            builder(market_snapshot, thirty_minute_close)

        # Companion E를 boundary 이후로 고친 exact pair는 두 confirmed flag를 모두 만든다.
        valid_one_minute_close = make_kline(
            Interval.ONE_MINUTE,
            boundary_time - timedelta(minutes=1),
            Decimal("121"),
            closed=True,
            event_time=boundary_time + timedelta(milliseconds=100),
        )
        corrected_boundary_klines = make_snapshot_klines(
            boundary_time + timedelta(seconds=1),
            BASE_CLOSED_PRICES,
            thirty_minute_close,
            latest_one_minute=valid_one_minute_close,
        )
        market_snapshot.update(
            corrected_boundary_klines,
            source_klines=(valid_one_minute_close, thirty_minute_close),
        )
        valid_evaluation = builder(market_snapshot, thirty_minute_close)
        self.assertIsNotNone(valid_evaluation)
        self.assertTrue(valid_evaluation.confirmed_1m_close)
        self.assertTrue(valid_evaluation.confirmed_30m_close)

    def test_rejects_boundary_pair_with_extra_or_mixed_primary_source(
        self,
    ) -> None:
        """
        함수 이름: test_rejects_boundary_pair_with_extra_or_mixed_primary_source()
        기능: exact 1분·30분 pair 외 추가 4시간 source와 4시간 primary 혼합을 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        for source_case in ("extra_source", "mixed_primary"):
            with self.subTest(source_case=source_case):
                builder, market_snapshot, utc_clock = make_rebased_builder()
                boundary_time = CURRENT_CANDLE_OPEN + timedelta(minutes=30)

                # 정상 close pair과 latest exact 4시간 live source를 같은 candidate mapping에 준비한다.
                one_minute_close = make_kline(
                    Interval.ONE_MINUTE,
                    boundary_time - timedelta(minutes=1),
                    Decimal("121"),
                    closed=True,
                    event_time=boundary_time + timedelta(milliseconds=100),
                )
                thirty_minute_close = make_kline(
                    Interval.THIRTY_MINUTES,
                    CURRENT_CANDLE_OPEN,
                    Decimal("120"),
                    closed=True,
                    event_time=boundary_time + timedelta(milliseconds=200),
                )
                four_hour_source = make_kline(
                    Interval.FOUR_HOURS,
                    FOUR_HOUR_CANDLE_OPEN,
                    Decimal("5000"),
                    closed=False,
                    event_time=boundary_time + timedelta(milliseconds=300),
                )
                utc_clock.set_time(boundary_time + timedelta(seconds=1))
                boundary_klines = make_snapshot_klines(
                    boundary_time + timedelta(seconds=1),
                    BASE_CLOSED_PRICES,
                    thirty_minute_close,
                    latest_one_minute=one_minute_close,
                )
                boundary_klines[Interval.FOUR_HOURS] = (
                    four_hour_source,
                )
                source_klines = (
                    (one_minute_close, thirty_minute_close, four_hour_source)
                    if source_case == "extra_source"
                    else (four_hour_source, thirty_minute_close)
                )
                observed_kline = (
                    thirty_minute_close
                    if source_case == "extra_source"
                    else four_hour_source
                )
                market_snapshot.update(
                    boundary_klines,
                    source_klines=source_klines,
                )

                # Full source tuple이 exact canonical pair가 아니면 primary interval에 관계없이 fail closed한다.
                with self.assertRaises(MarketEvaluationCalculationError):
                    builder(market_snapshot, observed_kline)

    def test_rejects_event_time_before_candle_open_without_state_commit(self) -> None:
        """
        함수 이름: test_rejects_event_time_before_candle_open_without_state_commit()
        기능: 봉 open보다 이른 source event time을 거부하고 이후 정상 version의 처리를 보존한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        builder, market_snapshot, utc_clock = make_rebased_builder()

        # Domain 값 형식은 맞지만 인과 시각이 역전된 30분 source를 authoritative version에 넣는다.
        invalid_event_time = CURRENT_CANDLE_OPEN - timedelta(seconds=1)
        invalid_source = make_kline(
            Interval.THIRTY_MINUTES,
            CURRENT_CANDLE_OPEN,
            Decimal("120"),
            closed=False,
            event_time=invalid_event_time,
        )
        snapshot_time = CURRENT_CANDLE_OPEN + timedelta(minutes=5)
        commit_market_snapshot(
            market_snapshot,
            utc_clock,
            snapshot_time,
            BASE_CLOSED_PRICES,
            invalid_source,
            observed_kline=invalid_source,
        )
        with self.assertRaises(MarketEvaluationCalculationError):
            builder(market_snapshot, invalid_source)

        # 실패 뒤의 정상 source는 이전 실패 version이나 timer 상태에 막히지 않아야 한다.
        valid_event_time = CURRENT_CANDLE_OPEN + timedelta(minutes=6)
        valid_source = make_kline(
            Interval.THIRTY_MINUTES,
            CURRENT_CANDLE_OPEN,
            Decimal("121"),
            closed=False,
            event_time=valid_event_time,
        )
        commit_market_snapshot(
            market_snapshot,
            utc_clock,
            valid_event_time,
            BASE_CLOSED_PRICES,
            valid_source,
            observed_kline=valid_source,
        )
        valid_evaluation = builder(market_snapshot, valid_source)
        self.assertIsNotNone(valid_evaluation)
        self.assertEqual(valid_evaluation.realtime_price, Decimal("121"))

    def test_reset_and_rebase_clear_version_and_duration_state(self) -> None:
        """
        함수 이름: test_reset_and_rebase_clear_version_and_duration_state()
        기능: reset과 source 없는 rebase가 이전 version gate와 성숙한 유지 시간을 폐기하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        monotonic_clock = MutableMonotonicClock()
        builder, market_snapshot, utc_clock = make_rebased_builder(
            monotonic_clock=monotonic_clock
        )

        # 같은 30분봉의 두 live tick으로 상승 slope 5초 조건을 먼저 성숙시킨다.
        first_event_time = CURRENT_CANDLE_OPEN + timedelta(minutes=5)
        first_event = make_kline(
            Interval.THIRTY_MINUTES,
            CURRENT_CANDLE_OPEN,
            Decimal("120"),
            closed=False,
            event_time=first_event_time,
        )
        commit_market_snapshot(
            market_snapshot,
            utc_clock,
            first_event_time,
            BASE_CLOSED_PRICES,
            first_event,
            observed_kline=first_event,
        )
        first_evaluation = builder(market_snapshot, first_event)
        self.assertIsNotNone(first_evaluation)

        monotonic_clock.set_time(5_000_000_000)
        second_event_time = CURRENT_CANDLE_OPEN + timedelta(minutes=6)
        second_event = make_kline(
            Interval.THIRTY_MINUTES,
            CURRENT_CANDLE_OPEN,
            Decimal("120"),
            closed=False,
            event_time=second_event_time,
        )
        commit_market_snapshot(
            market_snapshot,
            utc_clock,
            second_event_time,
            BASE_CLOSED_PRICES,
            second_event,
            observed_kline=second_event,
        )
        mature_evaluation = builder(market_snapshot, second_event)
        self.assertIsNotNone(mature_evaluation)
        self.assertTrue(mature_evaluation.realtime_slope_above_008_for_5s)

        # reset은 동일 version 재평가와 낮은 새 monotonic baseline을 허용하되 timer를 승계하지 않는다.
        builder.reset()
        monotonic_clock.set_time(0)
        reset_evaluation = builder(market_snapshot, second_event)
        self.assertIsNotNone(reset_evaluation)
        self.assertFalse(reset_evaluation.realtime_slope_above_008_for_5s)

        # source 없는 full update를 rebase해도 clock을 읽지 않고 다음 live tick부터 시간을 다시 센다.
        full_update_time = CURRENT_CANDLE_OPEN + timedelta(minutes=7)
        rebased_current = make_kline(
            Interval.THIRTY_MINUTES,
            CURRENT_CANDLE_OPEN,
            Decimal("121"),
            closed=False,
        )
        commit_market_snapshot(
            market_snapshot,
            utc_clock,
            full_update_time,
            BASE_CLOSED_PRICES,
            rebased_current,
        )
        call_count_before_rebase = monotonic_clock.call_count
        builder.rebase(market_snapshot)
        self.assertEqual(
            monotonic_clock.call_count,
            call_count_before_rebase,
        )

        monotonic_clock.set_time(20_000_000_000)
        post_rebase_time = CURRENT_CANDLE_OPEN + timedelta(minutes=8)
        post_rebase_event = make_kline(
            Interval.THIRTY_MINUTES,
            CURRENT_CANDLE_OPEN,
            Decimal("121"),
            closed=False,
            event_time=post_rebase_time,
        )
        commit_market_snapshot(
            market_snapshot,
            utc_clock,
            post_rebase_time,
            BASE_CLOSED_PRICES,
            post_rebase_event,
            observed_kline=post_rebase_event,
        )
        post_rebase_evaluation = builder(
            market_snapshot,
            post_rebase_event,
        )
        self.assertIsNotNone(post_rebase_evaluation)
        self.assertFalse(
            post_rebase_evaluation.realtime_slope_above_008_for_5s
        )


if __name__ == "__main__":
    unittest.main()  # 직접 실행과 unittest discovery가 같은 suite를 사용한다.
