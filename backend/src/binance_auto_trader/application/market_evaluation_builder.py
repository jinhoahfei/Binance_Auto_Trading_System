"""30분 전략 지표를 same-version 시장 평가 snapshot으로 계산한다."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from time import monotonic_ns

from binance_auto_trader.domain.common import Interval
from binance_auto_trader.domain.market import (
    Kline,
    MarketSnapshot,
    calculate_candidate_ema9_slope,
    calculate_ema9_series,
    calculate_normalized_ols_slope,
)
from binance_auto_trader.domain.market.ema_slope import (
    DECIMAL_PRECISION,
    SLOPE_SAMPLE_SIZE,
)
from binance_auto_trader.domain.trading import MarketEvaluationSnapshot
from binance_auto_trader.application.market_condition_timers import create_hold_timer_snapshots


# 30분 Bollinger/CCI와 monotonic 유지 조건의 고정 상수를 한곳에서 공유한다.
BOLLINGER_PERIOD = 20
BOLLINGER_STANDARD_DEVIATIONS = Decimal("2")
CCI_PERIOD = 20
CCI_SCALING_CONSTANT = Decimal("0.015")
FIVE_SECONDS_IN_NANOSECONDS = 5_000_000_000
THREE_MINUTES_IN_NANOSECONDS = 180_000_000_000
MINIMUM_PRODUCTION_KLINE_LIMIT = BOLLINGER_PERIOD + 1


class MarketEvaluationCalculationError(RuntimeError, ValueError):
    """
    클래스 이름: MarketEvaluationCalculationError
    기능: 불충분·stale·모순 시장 입력을 거래 event 없이 fail closed한 계산 오류를 나타낸다.
    작성 날짜: 2026/08/29
    """


def _calculate_bollinger_bands(
    close_prices: Sequence[Decimal],
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """
    함수 이름: _calculate_bollinger_bands()
    기능: 20개 종가의 population 표준편차 2배로 중단·하단·상단·BBW를 계산한다.
    인자: close_prices -> 시간순 종가 또는 현재 후보 종가를 포함한 정확히 20개 값
    반환값: middle, lower, upper와 (upper-lower)/middle BBW
    작성 날짜: 2026/08/29
    """
    if len(close_prices) != BOLLINGER_PERIOD:
        raise MarketEvaluationCalculationError(
            "exactly twenty prices are required for Bollinger Bands"
        )

    # UI와 전략 명세가 공유하는 20기간 2σ를 Decimal population 분산으로 계산한다.
    middle_band = sum(close_prices, Decimal("0")) / Decimal(
        BOLLINGER_PERIOD
    )
    variance = sum(
        (
            (close_price - middle_band) ** 2
            for close_price in close_prices
        ),
        Decimal("0"),
    ) / Decimal(BOLLINGER_PERIOD)
    standard_deviation = variance.sqrt()
    band_offset = standard_deviation * BOLLINGER_STANDARD_DEVIATIONS
    lower_band = middle_band - band_offset
    upper_band = middle_band + band_offset
    if middle_band <= Decimal("0") or upper_band <= lower_band:
        raise MarketEvaluationCalculationError(
            "Bollinger Bands require a positive middle and non-zero width"
        )

    band_width = upper_band - lower_band
    return (
        middle_band,
        lower_band,
        upper_band,
        band_width / middle_band,
    )


def _calculate_pct_b(
    candidate_price: Decimal,
    lower_band: Decimal,
    upper_band: Decimal,
) -> Decimal:
    """
    함수 이름: _calculate_pct_b()
    기능: 후보 가격이 같은 계산의 Bollinger 폭에서 차지하는 %B를 계산한다.
    인자: candidate_price -> %B에 대입할 가격
        lower_band -> 같은 20기간 계산의 하단 Band
        upper_band -> 같은 20기간 계산의 상단 Band
    반환값: Bollinger 폭 기준 무차원 %B
    작성 날짜: 2026/08/29
    """
    band_width = upper_band - lower_band
    if band_width <= Decimal("0"):
        raise MarketEvaluationCalculationError(
            "Bollinger width must be positive for percent-B"
        )

    return (candidate_price - lower_band) / band_width


def calculate_execution_pct_b(
    market_snapshot: MarketSnapshot,
    executed_at: datetime,
    execution_price: Decimal,
) -> Decimal | None:
    """
    함수 이름: calculate_execution_pct_b()
    기능: 실제 체결가와 체결봉 직전 19개 확정봉으로 체결 순간의 후보 BB와 %B를 복원한다.
    인자: market_snapshot -> authoritative 30분 이력
        executed_at -> 거래소 실제 체결시각, execution_price -> 해당 fill 가격
    반환값: 체결 %B 또는 연속 이력을 증명할 수 없을 때 None
    작성 날짜: 2026/09/09
    """
    # 조회 시점의 최신 밴드를 과거 체결에 대입하지 않는다.
    if not market_snapshot.ready or executed_at.utcoffset() is None:
        return None
    execution_time = executed_at.astimezone(timezone.utc)
    candle_open = execution_time.replace(
        minute=(execution_time.minute // 30) * 30, second=0, microsecond=0,
    )
    previous_candles = tuple(
        candle for candle in market_snapshot.klines_by_interval[Interval.THIRTY_MINUTES]
        if candle.closed and candle.open_time < candle_open
    )[-(BOLLINGER_PERIOD - 1):]
    if len(previous_candles) != BOLLINGER_PERIOD - 1 or any(
        candle.open_time != candle_open - timedelta(minutes=30 * (BOLLINGER_PERIOD - 1 - index))
        for index, candle in enumerate(previous_candles)
    ):
        return None  # 누락·보존기간 밖의 이력은 적극 인계를 허용하는 추정값으로 바꾸지 않는다.
    with localcontext() as decimal_context:
        decimal_context.prec = DECIMAL_PRECISION
        decimal_context.rounding = ROUND_HALF_EVEN
        try:
            _middle, lower, upper, _width = _calculate_bollinger_bands(
                (*tuple(candle.close for candle in previous_candles), execution_price)
            )
        except MarketEvaluationCalculationError:
            return None
        return _calculate_pct_b(execution_price, lower, upper)


def _calculate_cci(
    typical_prices: Sequence[Decimal],
) -> Decimal:
    """
    함수 이름: _calculate_cci()
    기능: 20개 typical price의 평균 절대편차를 사용해 표준 CCI 20을 계산한다.
    인자: typical_prices -> (high+low+close)/3으로 만든 정확히 20개 값
    반환값: 무차원 CCI 값이며 변동이 전혀 없으면 0
    작성 날짜: 2026/08/29
    """
    if len(typical_prices) != CCI_PERIOD:
        raise MarketEvaluationCalculationError(
            "exactly twenty typical prices are required for CCI"
        )

    # 평균 절대편차가 0이면 방향 정보가 없으므로 setup을 만들지 않는 중립 CCI 0을 반환한다.
    typical_average = sum(typical_prices, Decimal("0")) / Decimal(
        CCI_PERIOD
    )
    mean_deviation = sum(
        (
            abs(typical_price - typical_average)
            for typical_price in typical_prices
        ),
        Decimal("0"),
    ) / Decimal(CCI_PERIOD)
    if mean_deviation == Decimal("0"):
        return Decimal("0")

    return (
        (typical_prices[-1] - typical_average)
        / (CCI_SCALING_CONSTANT * mean_deviation)
    )


def _create_candle_id(kline: Kline) -> str:
    """
    함수 이름: _create_candle_id()
    기능: 30분봉 symbol·interval·UTC open time으로 결정적 candle ID를 만든다.
    인자: kline -> 식별할 authoritative 30분봉
    반환값: 공백 없는 canonical candle ID
    작성 날짜: 2026/08/29
    """
    open_time = kline.open_time.astimezone(timezone.utc).isoformat().replace(
        "+00:00",
        "Z",
    )
    return f"{kline.symbol}:{kline.interval.value}:{open_time}"


def _calculate_typical_price(kline: Kline) -> Decimal:
    """
    함수 이름: _calculate_typical_price()
    기능: 한 30분봉의 high·low·close 평균 가격을 계산한다.
    인자: kline -> typical price를 만들 30분 Kline
    반환값: (high + low + close) / 3 Decimal
    작성 날짜: 2026/08/29
    """
    return (kline.high + kline.low + kline.close) / Decimal("3")


class ThirtyMinuteMarketEvaluationBuilder:
    """
    클래스 이름: ThirtyMinuteMarketEvaluationBuilder
    기능: 30분봉 지표와 연속 조건을 public Kline별 MarketEvaluationSnapshot으로 만든다.
    작성 날짜: 2026/08/29
    """

    __slots__ = (
        "_condition_started_at",
        "_current_candle_id",
        "_last_market_version",
        "_last_monotonic_time",
        "_monotonic_clock",
        "_condition_timers",
        "_timer_generation",
    )

    def __init__(
        self,
        monotonic_clock: Callable[[], int] | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 주입 가능한 nanosecond monotonic clock과 빈 연속 조건 상태를 준비한다.
        인자: monotonic_clock -> 단조 증가 nanosecond 정수를 반환할 callable 또는 None
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        selected_clock = monotonic_ns if monotonic_clock is None else monotonic_clock
        if not callable(selected_clock):
            raise TypeError("monotonic_clock must be callable")

        # Stream generation이 시작되기 전에는 어떤 threshold도 이미 유지된 것으로 보지 않는다.
        self._monotonic_clock = selected_clock
        self._last_market_version: int | None = None
        self._last_monotonic_time: int | None = None
        self._current_candle_id: str | None = None
        self._condition_started_at: dict[str, int | None] = {}
        self._condition_timers = ()  # 마지막 30분 tick의 측정 시각까지 함께 보존한다.
        self._timer_generation = 0

    def reset(self) -> None:
        """
        함수 이름: reset()
        기능: disconnect·gap·full-resync에서 후보 누적과 모든 연속 조건 시작점을 폐기한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # 새 generation은 이전 version과 monotonic 유지 시간을 증거로 재사용하지 않는다.
        self._last_market_version = None
        self._last_monotonic_time = None
        self._current_candle_id = None
        self._condition_started_at = {}
        self._condition_timers = ()
        self._timer_generation += 1  # 재연결 뒤 같은 봉·같은 조건도 새로운 타이머 회차다.

    def rebase(self, market_snapshot: MarketSnapshot) -> None:
        """
        함수 이름: rebase()
        기능: 초기화·full-resync snapshot의 30분 계산 가능성을 검증하고 새 generation 기준을 세운다.
        인자: market_snapshot -> REST·WebSocket 병합과 REGIME 평가를 마친 authoritative snapshot
        반환값: 계산 baseline이 유효하면 없음
        작성 날짜: 2026/08/29
        """
        if not isinstance(market_snapshot, MarketSnapshot):
            raise TypeError("market_snapshot must be a MarketSnapshot")
        if not market_snapshot.ready or market_snapshot.version <= 0:
            raise MarketEvaluationCalculationError(
                "MarketSnapshot must be ready with a positive version"
            )

        # 진행 중 30분봉과 최소 20개 확정봉이 모두 있어야 live event 전에 gate를 열 수 있다.
        thirty_minute_klines = market_snapshot.klines_by_interval[
            Interval.THIRTY_MINUTES
        ]
        latest_thirty_minute = thirty_minute_klines[-1]
        if latest_thirty_minute.closed:
            raise MarketEvaluationCalculationError(
                "rebased MarketSnapshot requires a current open 30-minute Kline"
            )
        self.reset()
        with localcontext() as decimal_context:
            decimal_context.prec = DECIMAL_PRECISION
            decimal_context.rounding = ROUND_HALF_EVEN
            evaluation_values = self._calculate_evaluation_values(
                market_snapshot,
                latest_thirty_minute,
            )

        # Baseline 계산은 threshold 시간을 시작하지 않고 version과 현재 candle identity만 고정한다.
        candle_id = evaluation_values["current_30m_candle_id"]
        if not isinstance(candle_id, str):
            raise RuntimeError("calculated 30-minute candle ID must be a string")
        self._last_market_version = market_snapshot.version
        self._current_candle_id = candle_id

    def __call__(
        self,
        market_snapshot: MarketSnapshot,
        observed_kline: Kline,
    ) -> MarketEvaluationSnapshot | None:
        """
        함수 이름: __call__()
        기능: same-version snapshot과 원본 Kline에서 production 거래 평가를 계산한다.
        인자: market_snapshot -> observed_kline 반영을 끝낸 authoritative MarketSnapshot
            observed_kline -> 이번 version을 발생시킨 public Kline
        반환값: 계산된 불변 시장 평가 또는 30분 close→next open 대기 중이면 None
        작성 날짜: 2026/08/29
        """
        should_evaluate = self._validate_source(
            market_snapshot,
            observed_kline,
        )
        if not should_evaluate:
            return None  # 정상 rollover 대기에는 Trading event나 stream 복구를 만들지 않는다.

        # 모든 금융 계산은 유효숫자 34와 HALF_EVEN context에서 중간 quantize 없이 수행한다.
        with localcontext() as decimal_context:
            decimal_context.prec = DECIMAL_PRECISION
            decimal_context.rounding = ROUND_HALF_EVEN
            evaluation_values = self._calculate_evaluation_values(
                market_snapshot,
                observed_kline,
            )

        # 연속 조건은 30분 stream tick만 전진시키며 1분 close는 마지막 상태를 읽기만 한다.
        candle_id = evaluation_values["current_30m_candle_id"]
        if not isinstance(candle_id, str):
            raise RuntimeError("calculated 30-minute candle ID must be a string")
        advances_conditions = (
            observed_kline.interval is Interval.THIRTY_MINUTES
        )
        monotonic_time = (
            self._read_monotonic_time()
            if advances_conditions
            else (self._last_monotonic_time or 0)
        )
        condition_flags, next_condition_starts, condition_contracts = self._evaluate_conditions(
            candle_id,
            monotonic_time,
            evaluation_values,
        )
        # 1분 close는 30분 유지시간의 측정 시각도 다시 찍지 않아 UI 시간을 되돌리지 않는다.
        condition_timers = self._condition_timers
        if advances_conditions:
            sampled_at = market_snapshot.updated_at
            if sampled_at is None:
                raise MarketEvaluationCalculationError("Timer evaluation requires a server sample time")
            condition_timers = create_hold_timer_snapshots(
                condition_contracts, condition_flags, next_condition_starts, self._condition_timers,
                monotonic_time=monotonic_time, sampled_at=sampled_at,
                candle_id=candle_id, generation=self._timer_generation,
                candle_changed=self._current_candle_id is not None and candle_id != self._current_candle_id,
            )
        market_evaluation = MarketEvaluationSnapshot(
            **evaluation_values,
            **condition_flags,
            condition_timers=condition_timers,
        )

        # DTO 검증까지 성공한 뒤에만 builder state를 commit해 실패한 계산이 시간을 소비하지 않게 한다.
        if advances_conditions:
            self._current_candle_id = candle_id
            self._condition_started_at = next_condition_starts
            self._last_monotonic_time = monotonic_time
            self._condition_timers = condition_timers
        self._last_market_version = market_snapshot.version
        return market_evaluation

    def _validate_source(
        self,
        market_snapshot: MarketSnapshot,
        observed_kline: Kline,
    ) -> bool:
        """
        함수 이름: _validate_source()
        기능: ready snapshot, 새 version, 원본 Kline 포함 여부와 30분 시간축 일치를 검증한다.
        인자: market_snapshot -> 계산 source snapshot
            observed_kline -> snapshot version을 만든 원본 Kline
        반환값: 즉시 평가할 수 있으면 True, 정상 30분 rollover 대기이면 False
        작성 날짜: 2026/08/29
        """
        if not isinstance(market_snapshot, MarketSnapshot):
            raise TypeError("market_snapshot must be a MarketSnapshot")
        if not isinstance(observed_kline, Kline):
            raise TypeError("observed_kline must be a Kline")
        if not market_snapshot.ready or market_snapshot.version <= 0:
            raise MarketEvaluationCalculationError(
                "MarketSnapshot must be ready with a positive version"
            )

        # WebSocket source는 봉이 열린 뒤에만 그 봉의 변화를 관찰할 수 있어 인과 시각 역전을 거부한다.
        if observed_kline.event_time is None:
            raise MarketEvaluationCalculationError(
                "observed Kline must contain event provenance"
            )
        if observed_kline.event_time < observed_kline.open_time:
            raise MarketEvaluationCalculationError(
                "observed Kline event time must not precede its open time"
            )
        if observed_kline.closed and observed_kline.interval in (
            Interval.ONE_MINUTE,
            Interval.THIRTY_MINUTES,
        ):
            interval_duration = (
                timedelta(minutes=1)
                if observed_kline.interval is Interval.ONE_MINUTE
                else timedelta(minutes=30)
            )
            close_boundary = observed_kline.open_time + interval_duration
            if observed_kline.event_time < close_boundary:
                raise MarketEvaluationCalculationError(
                    "closed strategy Kline event time must reach its close boundary"
                )
        if observed_kline.symbol != market_snapshot.symbol:
            raise MarketEvaluationCalculationError(
                "observed Kline symbol differs from MarketSnapshot"
            )
        if (
            self._last_market_version is not None
            and market_snapshot.version <= self._last_market_version
        ):
            raise MarketEvaluationCalculationError(
                "market version must strictly advance between evaluations"
            )

        # 모든 source는 자기 interval의 최신 exact Kline이어야 이전 callback을 새 version으로 재사용할 수 없다.
        interval_klines = market_snapshot.klines_by_interval[
            observed_kline.interval
        ]
        if not interval_klines or observed_kline != interval_klines[-1]:
            raise MarketEvaluationCalculationError(
                "observed Kline must be the latest exact interval Kline"
            )
        if observed_kline not in market_snapshot.update_source_klines:
            raise MarketEvaluationCalculationError(
                "observed Kline did not cause the authoritative market version"
            )

        # Strategy source가 있는 version은 단일 source 또는 exact boundary pair만 허용한다.
        has_strategy_source = any(
            source_kline.interval
            in (Interval.ONE_MINUTE, Interval.THIRTY_MINUTES)
            for source_kline in market_snapshot.update_source_klines
        )
        if has_strategy_source:
            self._resolve_boundary_one_minute_close_source(
                market_snapshot,
                observed_kline,
            )

        # 30분 event만 실시간 후보와 유지 시간을 전진시키며 close도 정확히 한 번 평가한다.
        thirty_minute_klines = market_snapshot.klines_by_interval[
            Interval.THIRTY_MINUTES
        ]
        latest_thirty_minute = thirty_minute_klines[-1]
        if observed_kline.interval is Interval.THIRTY_MINUTES:
            return True

        # 4시간·1일 및 진행 중 1분 update는 30분 전략 평가의 가격·시간 source가 아니다.
        if observed_kline.interval is not Interval.ONE_MINUTE:
            return False
        if not observed_kline.closed:
            return False

        # 확정 1분봉은 자신의 open time으로 현재 열린 30분봉에 속할 때만 trailing 후보가 된다.
        current_close_boundary = latest_thirty_minute.open_time + timedelta(
            minutes=30
        )
        if latest_thirty_minute.closed:
            return False  # 다음 30분 OPEN event가 snapshot에 들어올 때까지 계산을 명시적으로 보류한다.
        if not (
            latest_thirty_minute.open_time
            <= observed_kline.open_time
            < current_close_boundary
        ):
            return False  # 늦은 이전 1분 close와 다음 30분 OPEN 선행 도착을 모두 안전하게 보류한다.

        return True

    @staticmethod
    def _resolve_boundary_one_minute_close_source(
        market_snapshot: MarketSnapshot,
        observed_kline: Kline,
    ) -> Kline | None:
        """
        함수 이름: _resolve_boundary_one_minute_close_source()
        기능: 단일 전략 source와 canonical 30분·4시간·일 경계 tuple의 1분 source를 검증한다.
        인자: market_snapshot -> source tuple과 interval history를 가진 authoritative snapshot
            observed_kline -> builder가 primary source로 받은 Kline
        반환값: 확정 1분 source가 있으면 그 Kline, 없으면 None
        작성 날짜: 2026/08/29
        """
        source_klines = market_snapshot.update_source_klines
        if source_klines == (observed_kline,):
            if observed_kline.closed and observed_kline.interval in (
                Interval.ONE_MINUTE,
                Interval.THIRTY_MINUTES,
            ):
                interval_duration = (
                    timedelta(minutes=1)
                    if observed_kline.interval is Interval.ONE_MINUTE
                    else timedelta(minutes=30)
                )
                boundary_time = observed_kline.open_time + interval_duration
                is_upper_boundary = (
                    boundary_time.minute == 0
                    and boundary_time.second == 0
                    and boundary_time.microsecond == 0
                    and boundary_time.hour % 4 == 0
                )
                if is_upper_boundary:
                    raise MarketEvaluationCalculationError(
                        "an upper UTC boundary requires canonical upper sources"
                    )
            if (
                observed_kline.interval is Interval.ONE_MINUTE
                and observed_kline.closed
            ):
                return observed_kline
            return None

        # 복합 version은 boundary 종류별 exact 길이와 1분, 30분 prefix 순서를 요구한다.
        if len(source_klines) not in (2, 4, 6):
            raise MarketEvaluationCalculationError(
                "a market version has ambiguous strategy source Klines"
            )
        one_minute_close, thirty_minute_close = source_klines[:2]
        if (
            one_minute_close.interval is not Interval.ONE_MINUTE
            or thirty_minute_close.interval is not Interval.THIRTY_MINUTES
            or not one_minute_close.closed
            or not thirty_minute_close.closed
            or observed_kline != thirty_minute_close
        ):
            raise MarketEvaluationCalculationError(
                "a composite strategy source must be a canonical closed boundary pair"
            )

        one_minute_boundary = one_minute_close.open_time + timedelta(
            minutes=1
        )
        thirty_minute_boundary = thirty_minute_close.open_time + timedelta(
            minutes=30
        )
        if one_minute_boundary != thirty_minute_boundary:
            raise MarketEvaluationCalculationError(
                "composite source Klines must share one close boundary"
            )

        # UTC 4H와 자정 여부가 요구하는 tuple 길이를 고정해 upper source 임의 혼입을 막는다.
        is_four_hour_boundary = (
            one_minute_boundary.minute == 0
            and one_minute_boundary.second == 0
            and one_minute_boundary.microsecond == 0
            and one_minute_boundary.hour % 4 == 0
        )
        is_one_day_boundary = (
            is_four_hour_boundary
            and one_minute_boundary.hour == 0
        )
        expected_source_count = (
            6
            if is_one_day_boundary
            else (4 if is_four_hour_boundary else 2)
        )
        if len(source_klines) != expected_source_count:
            raise MarketEvaluationCalculationError(
                "composite source tuple does not match its UTC boundary"
            )

        # 모든 source는 snapshot history의 exact 값이며 open 이후 event provenance를 가져야 한다.
        for source_kline in source_klines:
            if source_kline.symbol != market_snapshot.symbol:
                raise MarketEvaluationCalculationError(
                    "composite source symbol differs from MarketSnapshot"
                )
            if (
                source_kline.event_time is None
                or source_kline.event_time < source_kline.open_time
            ):
                raise MarketEvaluationCalculationError(
                    "composite source event time must not precede its open time"
                )
            if source_kline not in market_snapshot.klines_by_interval[
                source_kline.interval
            ]:
                raise MarketEvaluationCalculationError(
                    "composite source must be an exact interval Kline"
                )
        if (
            one_minute_close
            != market_snapshot.klines_by_interval[Interval.ONE_MINUTE][-1]
            or thirty_minute_close
            != market_snapshot.klines_by_interval[Interval.THIRTY_MINUTES][-1]
        ):
            raise MarketEvaluationCalculationError(
                "strategy boundary sources must be latest exact Klines"
            )

        # Canonical tuple의 모든 close/open event는 shared boundary 이전일 수 없다.
        for source_kline in source_klines:
            if (
                source_kline.event_time is None
                or source_kline.event_time < one_minute_boundary
            ):
                raise MarketEvaluationCalculationError(
                    "composite close event time must reach the shared boundary"
                )

        # 4H source는 closed Kline과 같은 boundary에서 시작한 exact latest OPEN 순서다.
        if is_four_hour_boundary:
            four_hour_close, four_hour_open = source_klines[2:4]
            if (
                four_hour_close.interval is not Interval.FOUR_HOURS
                or four_hour_open.interval is not Interval.FOUR_HOURS
                or not four_hour_close.closed
                or four_hour_open.closed
                or four_hour_close.open_time + timedelta(hours=4)
                != one_minute_boundary
                or four_hour_open.open_time != one_minute_boundary
                or market_snapshot.klines_by_interval[
                    Interval.FOUR_HOURS
                ][-2:] != (four_hour_close, four_hour_open)
            ):
                raise MarketEvaluationCalculationError(
                    "four-hour boundary sources are not canonical"
                )

        # UTC 자정 source는 4H pair 뒤에 closed 1D와 exact next-open 1D를 이어 붙인다.
        if is_one_day_boundary:
            one_day_close, one_day_open = source_klines[4:6]
            if (
                one_day_close.interval is not Interval.ONE_DAY
                or one_day_open.interval is not Interval.ONE_DAY
                or not one_day_close.closed
                or one_day_open.closed
                or one_day_close.open_time + timedelta(days=1)
                != one_minute_boundary
                or one_day_open.open_time != one_minute_boundary
                or market_snapshot.klines_by_interval[
                    Interval.ONE_DAY
                ][-2:] != (one_day_close, one_day_open)
            ):
                raise MarketEvaluationCalculationError(
                    "one-day boundary sources are not canonical"
                )

        return one_minute_close

    def _calculate_evaluation_values(
        self,
        market_snapshot: MarketSnapshot,
        observed_kline: Kline,
    ) -> dict[str, object]:
        """
        함수 이름: _calculate_evaluation_values()
        기능: 네 slope, Bollinger/%B/BBW, CCI와 candle provenance 필드를 계산한다.
        인자: market_snapshot -> 검증을 마친 same-version snapshot
            observed_kline -> 이번 version과 확정 1분·30분 여부를 제공한 source
        반환값: MarketEvaluationSnapshot 생성자에 전달할 시장 필드 mapping
        작성 날짜: 2026/08/29
        """
        thirty_minute_klines = market_snapshot.klines_by_interval[
            Interval.THIRTY_MINUTES
        ]
        one_minute_close_source = (
            self._resolve_boundary_one_minute_close_source(
                market_snapshot,
                observed_kline,
            )
            if market_snapshot.update_source_klines
            else None
        )  # Source-less full snapshot은 rebase 계산이며 어떤 close trigger도 만들지 않는다.
        closed_klines = tuple(
            kline
            for kline in thirty_minute_klines
            if kline.closed
        )
        if len(closed_klines) < BOLLINGER_PERIOD:
            raise MarketEvaluationCalculationError(
                "at least twenty closed 30-minute Klines are required"
            )

        # 확정 EMA slope는 마지막 확정 종가를 candidate_price 분모로 사용한다.
        close_prices = tuple(kline.close for kline in closed_klines)
        committed_ema_series = calculate_ema9_series(close_prices)
        if len(committed_ema_series) < SLOPE_SAMPLE_SIZE:
            raise MarketEvaluationCalculationError(
                "at least fourteen closed 30-minute Klines are required for slope"
            )
        latest_closed_kline = closed_klines[-1]
        confirmed_slope = calculate_normalized_ols_slope(
            committed_ema_series[-SLOPE_SAMPLE_SIZE:],
            latest_closed_kline.close,
        )

        # 실시간 지표는 다른 interval event 가격이 아니라 authoritative 최신 30분봉 close를 사용한다.
        latest_thirty_minute = thirty_minute_klines[-1]
        realtime_price = latest_thirty_minute.close
        confirmed_30m_close = (
            observed_kline.interval is Interval.THIRTY_MINUTES
            and observed_kline.closed
        )
        confirmed_1m_close = one_minute_close_source is not None
        if confirmed_30m_close:
            realtime_slope = confirmed_slope
        else:
            realtime_slope = calculate_candidate_ema9_slope(
                committed_ema_series,
                realtime_price,
            )

        # 실시간 Bollinger는 열린 30분봉 종가만 후보 가격으로 치환하고 확정봉에서는 그대로 commit한다.
        if confirmed_30m_close:
            realtime_band_prices = close_prices[-BOLLINGER_PERIOD:]
        else:
            realtime_band_prices = (
                *close_prices[-(BOLLINGER_PERIOD - 1):],
                realtime_price,
            )
        (
            _middle_band,
            lower_band,
            upper_band,
            touch_candle_bbw,
        ) = _calculate_bollinger_bands(realtime_band_prices)
        realtime_pct_b = _calculate_pct_b(
            realtime_price,
            lower_band,
            upper_band,
        )

        # 확정 signal %B는 실시간 후보와 섞지 않고 마지막 20개 확정 종가로 독립 계산한다.
        (
            _closed_middle_band,
            closed_lower_band,
            closed_upper_band,
            _closed_bbw,
        ) = _calculate_bollinger_bands(
            close_prices[-BOLLINGER_PERIOD:]
        )
        pct_b_close = _calculate_pct_b(
            latest_closed_kline.close,
            closed_lower_band,
            closed_upper_band,
        )

        # CCI의 진행봉 high·low·close도 같은 authoritative 30분 Kline 한 개에서 읽는다.
        candidate_high = latest_thirty_minute.high
        candidate_low = latest_thirty_minute.low
        if confirmed_30m_close:
            cci_typical_prices = tuple(
                _calculate_typical_price(kline)
                for kline in closed_klines[-CCI_PERIOD:]
            )
        else:
            candidate_typical_price = (
                candidate_high + candidate_low + realtime_price
            ) / Decimal("3")
            cci_typical_prices = (
                *(
                    _calculate_typical_price(kline)
                    for kline in closed_klines[-(CCI_PERIOD - 1):]
                ),
                candidate_typical_price,
            )
        cci_30m_realtime = _calculate_cci(cci_typical_prices)

        # Case C TP 기준은 같은 Band version의 %B 0.10 가격이며 그 가격 자체가 정규화 분모다.
        tp_price = lower_band + Decimal("0.10") * (
            upper_band - lower_band
        )
        tp_candidate_base = (
            calculate_ema9_series(close_prices[:-1])
            if confirmed_30m_close
            else committed_ema_series
        )  # Close event에서도 같은 봉의 actual EMA를 다시 seed로 써 다음 봉 후보처럼 누적하지 않는다.
        tp_reference_slope = calculate_candidate_ema9_slope(
            tp_candidate_base,
            tp_price,
        )
        if confirmed_1m_close:
            if one_minute_close_source is None:
                raise RuntimeError(
                    "confirmed one-minute close requires its source Kline"
                )
            one_minute_candidate_base = (
                calculate_ema9_series(close_prices[:-1])
                if confirmed_30m_close
                else committed_ema_series
            )
            current_close_slope = calculate_candidate_ema9_slope(
                one_minute_candidate_base,
                one_minute_close_source.close,
            )
        else:
            current_close_slope = realtime_slope

        # Signal 저가 guard는 현재 확정봉과 그 직전 세 확정봉을 서로 겹치지 않게 분리한다.
        previous_closed_lows = tuple(
            kline.low
            for kline in closed_klines[-4:-1]
        )
        return {
            "realtime_price": realtime_price,
            "lower_band": lower_band,
            "upper_band": upper_band,
            "realtime_pct_b": realtime_pct_b,
            "current_30m_candle_id": _create_candle_id(
                latest_thirty_minute
            ),
            "current_30m_low": candidate_low,
            "current_30m_high": candidate_high,
            "touch_candle_bbw": touch_candle_bbw,
            "confirmed_30m_close": confirmed_30m_close,
            "confirmed_30m_close_time": (
                latest_thirty_minute.open_time + timedelta(minutes=30)
                if confirmed_30m_close else None
            ),
            "confirmed_1m_close": confirmed_1m_close,
            "ema_slope_30m_close": confirmed_slope,
            "realtime_ema_slope": realtime_slope,
            "current_close_ema_slope": current_close_slope,
            "tp_reference_ema_slope": tp_reference_slope,
            "cci_30m_realtime": cci_30m_realtime,
            "pct_b_close": pct_b_close,
            "current_closed_candle_low": latest_closed_kline.low,
            "previous_3_closed_candle_lows": previous_closed_lows,
        }

    def _read_monotonic_time(self) -> int:
        """
        함수 이름: _read_monotonic_time()
        기능: 주입 clock의 exact non-negative int와 builder 내부 단조성을 검증한다.
        인자: 없음
        반환값: 현재 monotonic nanosecond
        작성 날짜: 2026/08/29
        """
        monotonic_time = self._monotonic_clock()
        if type(monotonic_time) is not int:
            raise TypeError("monotonic_clock must return an exact int")
        if monotonic_time < 0:
            raise ValueError("monotonic_clock must not return a negative value")
        if (
            self._last_monotonic_time is not None
            and monotonic_time < self._last_monotonic_time
        ):
            raise MarketEvaluationCalculationError(
                "monotonic clock moved backwards"
            )

        return monotonic_time

    def _evaluate_conditions(
        self,
        candle_id: str,
        monotonic_time: int,
        evaluation_values: dict[str, object],
    ) -> tuple[dict[str, bool], dict[str, int | None], dict[str, tuple[bool, int]]]:
        """
        함수 이름: _evaluate_conditions()
        기능: 5초·3분 조건의 연속 유지 시작점과 현재 flag를 원자 계산한다.
        인자: candle_id -> 현재 authoritative 30분봉 ID
            monotonic_time -> 이번 평가의 monotonic nanosecond
            evaluation_values -> 계산을 끝낸 실시간 %B와 slope 값
        반환값: boolean flag, 다음 시작점 및 실제 즉시 조건·지속시간 mapping
        작성 날짜: 2026/08/29
        """
        realtime_pct_b = evaluation_values["realtime_pct_b"]
        realtime_slope = evaluation_values["realtime_ema_slope"]
        if not isinstance(realtime_pct_b, Decimal):
            raise RuntimeError("realtime_pct_b must be a Decimal")
        if not isinstance(realtime_slope, Decimal):
            raise RuntimeError("realtime_ema_slope must be a Decimal")

        # 정상 봉 교체는 연속성을 끊지 않는다. 불충족 또는 reset/rebase에서만 초기화한다.
        previous_starts = self._condition_started_at
        condition_contracts = {
            "pct_b_at_least_060_for_5s": (
                realtime_pct_b >= Decimal("0.60"),
                FIVE_SECONDS_IN_NANOSECONDS,
            ),
            "realtime_slope_above_008_for_5s": (
                realtime_slope > Decimal("0.08"),
                FIVE_SECONDS_IN_NANOSECONDS,
            ),
            "realtime_slope_at_most_004_for_5s": (
                realtime_slope <= Decimal("0.04"),
                FIVE_SECONDS_IN_NANOSECONDS,
            ),
            "pct_b_below_060_for_5s": (
                realtime_pct_b < Decimal("0.60"),
                FIVE_SECONDS_IN_NANOSECONDS,
            ),
            "realtime_slope_at_most_minus_055_for_3m": (
                realtime_slope <= Decimal("-0.55"),
                THREE_MINUTES_IN_NANOSECONDS,
            ),
        }
        flags: dict[str, bool] = {}
        next_starts: dict[str, int | None] = {}
        for condition_name, (
            condition_satisfied,
            duration_nanoseconds,
        ) in condition_contracts.items():
            if not condition_satisfied:
                next_starts[condition_name] = None
                flags[condition_name] = False
                continue

            started_at = previous_starts.get(condition_name)
            if started_at is None:
                started_at = monotonic_time
            next_starts[condition_name] = started_at
            flags[condition_name] = (
                monotonic_time - started_at >= duration_nanoseconds
            )

        return flags, next_starts, condition_contracts  # 타이머 표시도 이 평가가 사용한 지속시간을 읽는다.
