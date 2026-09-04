"""현재 시장 snapshot에서 결정론적 public Case C Kline 입력을 계산한다."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_EVEN, localcontext

from binance_auto_trader.domain.common import Interval
from binance_auto_trader.domain.market import Kline, MarketStateSnapshot


# Fixture 계산은 production 지표와 같은 Decimal128 정밀도와 Case C threshold를 사용한다.
_DECIMAL_PRECISION = 34
_BOLLINGER_PERIOD = 20
_BOLLINGER_STANDARD_DEVIATIONS = Decimal("2")
_CCI_PERIOD = 20
_CCI_SCALING_CONSTANT = Decimal("0.015")
_SETUP_PERCENT_B_MAXIMUM = Decimal("-0.15")
_SETUP_CCI_MAXIMUM = Decimal("-140")
_FLUSH_PERCENT_B_MAXIMUM = Decimal("-0.25")
_RECOVERY_PERCENT_B_INCREMENT = Decimal("0.06")
_PRICE_SEARCH_DIVISOR = Decimal("1000")
_MAXIMUM_RECOVERY_EXPANSIONS = 32
_MAXIMUM_RECOVERY_BISECTIONS = 160
_EVENT_TIME_STEP = timedelta(microseconds=1)


@dataclass(frozen=True, slots=True)
class DeterministicPublicCase2Klines:
    """
    클래스 이름: DeterministicPublicCase2Klines
    기능: 같은 진행 30분봉의 SETUP, FLUSH와 RECOVERY public Kline을 보존한다.
    작성 날짜: 2026/09/04
    """

    setup: Kline
    flush: Kline
    recovery: Kline

    def as_tuple(self) -> tuple[Kline, Kline, Kline]:
        """
        함수 이름: as_tuple()
        기능: 세 Kline을 production 관찰 순서대로 반환한다.
        인자: 없음
        반환값: SETUP, FLUSH, RECOVERY Kline tuple
        작성 날짜: 2026/09/04
        """
        return (self.setup, self.flush, self.recovery)  # 공개 입력 순서를 한곳에서 고정한다.


def create_deterministic_public_case2_klines(
    market_state: MarketStateSnapshot,
) -> DeterministicPublicCase2Klines:
    """
    함수 이름: create_deterministic_public_case2_klines()
    기능: 현재 ready snapshot에서 Case C BUY를 유도할 세 public Kline을 계산한다.
    인자: market_state -> production MarketSnapshot이 공개한 한 version의 불변 상태
    반환값: 같은 열린 30분봉에 속하는 SETUP, FLUSH와 RECOVERY Kline
    작성 날짜: 2026/09/04
    """
    closed_klines, current_kline = _require_fixture_market_state(
        market_state
    )
    event_times = _create_event_times(market_state, current_kline)

    # 첫 입력은 현재 누적 OHLC를 보존하면서 production setup threshold를 모두 만족해야 한다.
    setup_kline = _find_setup_kline(
        closed_klines,
        current_kline,
        event_times[0],
    )

    # 둘째 입력은 더 낮은 누적 저가에서 flush threshold를 열어 recovery 기준을 만든다.
    flush_kline = _find_flush_kline(
        closed_klines,
        setup_kline,
        event_times[1],
    )

    # 셋째 입력은 flush 저가를 유지한 채 +0.06 %B만 회복해 추격매수 금지선 아래에 남는다.
    recovery_kline = _find_recovery_kline(
        closed_klines,
        flush_kline,
        event_times[2],
    )
    return DeterministicPublicCase2Klines(
        setup=setup_kline,
        flush=flush_kline,
        recovery=recovery_kline,
    )


def _require_fixture_market_state(
    market_state: MarketStateSnapshot,
) -> tuple[tuple[Kline, ...], Kline]:
    """
    함수 이름: _require_fixture_market_state()
    기능: fixture가 사용할 ready ETHUSDT 30분 history와 현재 열린 봉을 검증한다.
    인자: market_state -> 검증할 production market state
    반환값: 시간순 확정 30분봉 tuple과 최신 진행 30분봉
    작성 날짜: 2026/09/04
    """
    if not isinstance(market_state, MarketStateSnapshot):
        raise TypeError("market_state must be a MarketStateSnapshot")
    if not market_state.ready or market_state.version <= 0:
        raise ValueError("market_state must be ready with a positive version")
    if market_state.updated_at is None:
        raise ValueError("market_state must have an updated_at value")

    # 마지막 한 개만 진행봉이어야 fixture가 같은 candle identity를 안전하게 재사용할 수 있다.
    thirty_minute_klines = market_state.klines_by_interval.get(
        Interval.THIRTY_MINUTES,
        (),
    )
    if not thirty_minute_klines:
        raise ValueError("market_state must contain thirty-minute Klines")
    current_kline = thirty_minute_klines[-1]
    closed_klines = tuple(
        kline for kline in thirty_minute_klines if kline.closed
    )
    if current_kline.closed or any(
        not kline.closed for kline in thirty_minute_klines[:-1]
    ):
        raise ValueError("exactly the latest thirty-minute Kline must be open")
    if len(closed_klines) < _BOLLINGER_PERIOD:
        raise ValueError("at least twenty closed thirty-minute Klines are required")
    if current_kline.symbol != "ETHUSDT":
        raise ValueError("deterministic Case 2 supports only ETHUSDT")

    return closed_klines, current_kline


def _create_event_times(
    market_state: MarketStateSnapshot,
    current_kline: Kline,
) -> tuple[datetime, datetime, datetime]:
    """
    함수 이름: _create_event_times()
    기능: 현재 source보다 늦고 같은 30분봉 안에 있는 세 UTC event 시각을 만든다.
    인자: market_state -> snapshot 갱신 시각을 제공할 불변 상태
        current_kline -> optional source event 시각과 open time을 제공할 진행봉
    반환값: 엄격히 증가하는 SETUP, FLUSH와 RECOVERY event 시각 tuple
    작성 날짜: 2026/09/04
    """
    if market_state.updated_at is None:
        raise ValueError("market_state must have an updated_at value")

    # REST-only snapshot과 live snapshot 모두에서 마지막 인과 시각보다 정확히 뒤에서 시작한다.
    source_event_time = current_kline.event_time
    latest_observed_time = max(
        current_kline.open_time,
        market_state.updated_at,
        (
            current_kline.open_time
            if source_event_time is None
            else source_event_time
        ),
    )
    event_times = tuple(
        latest_observed_time + _EVENT_TIME_STEP * event_index
        for event_index in (1, 2, 3)
    )
    close_boundary = current_kline.open_time + timedelta(minutes=30)
    if event_times[-1] >= close_boundary:
        raise ValueError(
            "current thirty-minute Kline lacks room for deterministic events"
        )

    return event_times


def _find_setup_kline(
    closed_klines: tuple[Kline, ...],
    current_kline: Kline,
    event_time: datetime,
) -> Kline:
    """
    함수 이름: _find_setup_kline()
    기능: %B -0.15와 CCI -140 이하를 함께 만족하는 첫 양수 close를 찾는다.
    인자: closed_klines -> production 계산에 쓰일 확정 30분봉 history
        current_kline -> open, high, low와 volume을 보존할 현재 진행봉
        event_time -> setup public source의 UTC event 시각
    반환값: setup threshold를 만족하는 진행 30분 Kline
    작성 날짜: 2026/09/04
    """
    # 현재 close의 0.1% 단위 하향 후보만 사용해 threshold 자체를 patch하지 않는다.
    for candidate_numerator in range(999, 0, -1):
        candidate_price = (
            current_kline.close
            * Decimal(candidate_numerator)
            / _PRICE_SEARCH_DIVISOR
        )
        if candidate_price >= current_kline.low:
            continue  # Setup도 기존 누적 low보다 낮은 실제 candle 변화를 만들어야 한다.
        candidate_kline = _create_candidate_kline(
            current_kline,
            close_price=candidate_price,
            candle_low=candidate_price,
            event_time=event_time,
        )
        percent_b, cci = _calculate_fixture_indicators(
            closed_klines,
            candidate_kline,
        )
        if (
            percent_b <= _SETUP_PERCENT_B_MAXIMUM
            and cci <= _SETUP_CCI_MAXIMUM
        ):
            return candidate_kline

    raise ValueError(
        "current MarketStateSnapshot cannot produce a valid Case C setup"
    )


def _find_flush_kline(
    closed_klines: tuple[Kline, ...],
    setup_kline: Kline,
    event_time: datetime,
) -> Kline:
    """
    함수 이름: _find_flush_kline()
    기능: setup보다 낮은 price와 low에서 %B -0.25 이하인 flush 입력을 찾는다.
    인자: closed_klines -> production 계산에 쓰일 확정 30분봉 history
        setup_kline -> 첫 public setup 입력
        event_time -> flush public source의 UTC event 시각
    반환값: 더 낮은 누적 저가와 flush threshold를 가진 진행 30분 Kline
    작성 날짜: 2026/09/04
    """
    # Setup close에서 다시 0.1%씩 낮춰 최초 valid flush를 선택한다.
    for candidate_numerator in range(999, 0, -1):
        candidate_price = (
            setup_kline.close
            * Decimal(candidate_numerator)
            / _PRICE_SEARCH_DIVISOR
        )
        candidate_kline = _create_candidate_kline(
            setup_kline,
            close_price=candidate_price,
            candle_low=candidate_price,
            event_time=event_time,
        )
        percent_b, _ = _calculate_fixture_indicators(
            closed_klines,
            candidate_kline,
        )
        if (
            candidate_kline.low < setup_kline.low
            and percent_b <= _FLUSH_PERCENT_B_MAXIMUM
        ):
            return candidate_kline

    raise ValueError(
        "current MarketStateSnapshot cannot produce a valid Case C flush"
    )


def _find_recovery_kline(
    closed_klines: tuple[Kline, ...],
    flush_kline: Kline,
    event_time: datetime,
) -> Kline:
    """
    함수 이름: _find_recovery_kline()
    기능: flush 저가를 유지하고 %B를 0.06 이상 회복한 양수 close를 찾는다.
    인자: closed_klines -> production 계산에 쓰일 확정 30분봉 history
        flush_kline -> recovery 기준 price, low와 %B를 제공할 flush 입력
        event_time -> recovery public source의 UTC event 시각
    반환값: entry %B -0.15 미만에서 recovery threshold를 만족하는 Kline
    작성 날짜: 2026/09/04
    """
    flush_percent_b, _ = _calculate_fixture_indicators(
        closed_klines,
        flush_kline,
    )
    entry_percent_b = flush_percent_b + _RECOVERY_PERCENT_B_INCREMENT
    if entry_percent_b >= _SETUP_PERCENT_B_MAXIMUM:
        raise ValueError("flush does not leave room below the Case C chase guard")

    # 기존 candle high까지를 우선 상한으로 사용하고 부족할 때만 valid OHLC 범위에서 확장한다.
    lower_price = flush_kline.close
    upper_price = max(flush_kline.open, flush_kline.high)
    upper_kline = _create_candidate_kline(
        flush_kline,
        close_price=upper_price,
        candle_low=flush_kline.low,
        event_time=event_time,
    )
    upper_percent_b, _ = _calculate_fixture_indicators(
        closed_klines,
        upper_kline,
    )
    for _ in range(_MAXIMUM_RECOVERY_EXPANSIONS):
        if upper_percent_b >= entry_percent_b:
            break
        upper_price *= Decimal("2")
        upper_kline = _create_candidate_kline(
            flush_kline,
            close_price=upper_price,
            candle_low=flush_kline.low,
            event_time=event_time,
        )
        upper_percent_b, _ = _calculate_fixture_indicators(
            closed_klines,
            upper_kline,
        )
    else:
        raise ValueError("Case C recovery search could not bracket entry percent-B")

    # Decimal bisection은 entry threshold 바로 위의 가격을 찾아 recovery가 -0.15 아래에 남게 한다.
    recovery_kline: Kline | None = None
    recovery_percent_b: Decimal | None = None
    for _ in range(_MAXIMUM_RECOVERY_BISECTIONS):
        candidate_price = (lower_price + upper_price) / Decimal("2")
        if candidate_price in (lower_price, upper_price):
            break
        candidate_kline = _create_candidate_kline(
            flush_kline,
            close_price=candidate_price,
            candle_low=flush_kline.low,
            event_time=event_time,
        )
        candidate_percent_b, _ = _calculate_fixture_indicators(
            closed_klines,
            candidate_kline,
        )
        if candidate_percent_b >= entry_percent_b:
            recovery_kline = candidate_kline
            recovery_percent_b = candidate_percent_b
            upper_price = candidate_price
        else:
            lower_price = candidate_price

    if (
        recovery_kline is None
        or recovery_percent_b is None
        or recovery_kline.close <= flush_kline.close
        or recovery_kline.low != flush_kline.low
        or recovery_percent_b < entry_percent_b
        or recovery_percent_b >= _SETUP_PERCENT_B_MAXIMUM
    ):
        raise ValueError("Case C recovery candidate violates its entry contract")

    return recovery_kline


def _create_candidate_kline(
    current_kline: Kline,
    *,
    close_price: Decimal,
    candle_low: Decimal,
    event_time: datetime,
) -> Kline:
    """
    함수 이름: _create_candidate_kline()
    기능: 현재 open과 volume 및 누적 high/low 의미를 보존한 진행봉을 만든다.
    인자: current_kline -> 직전 같은-candle Kline
        close_price -> 이번 public tick의 양수 close
        candle_low -> 이번 tick까지 보존할 누적 저가 후보
        event_time -> 엄격히 증가하는 UTC source event 시각
    반환값: production observe_kline에 전달할 불변 Kline
    작성 날짜: 2026/09/04
    """
    if (
        not isinstance(close_price, Decimal)
        or not close_price.is_finite()
        or close_price <= Decimal("0")
    ):
        raise ValueError("close_price must be a finite positive Decimal")
    if (
        not isinstance(candle_low, Decimal)
        or not candle_low.is_finite()
        or candle_low <= Decimal("0")
    ):
        raise ValueError("candle_low must be a finite positive Decimal")

    # 같은 open candle의 고가·저가는 되돌리지 않고 새 close를 포함하도록 누적한다.
    cumulative_high = max(current_kline.high, close_price)
    cumulative_low = min(current_kline.low, candle_low, close_price)
    return Kline(
        symbol=current_kline.symbol,
        interval=Interval.THIRTY_MINUTES,
        open_time=current_kline.open_time,
        open=current_kline.open,
        high=cumulative_high,
        low=cumulative_low,
        close=close_price,
        volume=current_kline.volume,
        closed=False,
        event_time=event_time,
    )


def _calculate_fixture_indicators(
    closed_klines: tuple[Kline, ...],
    candidate_kline: Kline,
) -> tuple[Decimal, Decimal]:
    """
    함수 이름: _calculate_fixture_indicators()
    기능: 후보 탐색에만 사용할 realtime Bollinger %B와 CCI 20을 Decimal로 계산한다.
    인자: closed_klines -> 최소 20개의 확정 30분봉
        candidate_kline -> 마지막 realtime 후보로 대입할 진행봉
    반환값: 후보의 realtime %B와 CCI tuple
    작성 날짜: 2026/09/04
    """
    if len(closed_klines) < _BOLLINGER_PERIOD:
        raise ValueError("at least twenty closed Klines are required")

    # Production과 같은 마지막 19개 확정값 + 현재 후보 구조를 독립 계산한다.
    with localcontext() as decimal_context:
        decimal_context.prec = _DECIMAL_PRECISION
        decimal_context.rounding = ROUND_HALF_EVEN
        close_prices = (
            *(kline.close for kline in closed_klines[-19:]),
            candidate_kline.close,
        )
        middle_band = sum(close_prices, Decimal("0")) / Decimal(
            _BOLLINGER_PERIOD
        )
        variance = sum(
            (
                (close_price - middle_band) ** 2
                for close_price in close_prices
            ),
            Decimal("0"),
        ) / Decimal(_BOLLINGER_PERIOD)
        standard_deviation = variance.sqrt()
        band_offset = standard_deviation * _BOLLINGER_STANDARD_DEVIATIONS
        lower_band = middle_band - band_offset
        upper_band = middle_band + band_offset
        band_width = upper_band - lower_band
        if middle_band <= Decimal("0") or band_width <= Decimal("0"):
            raise ValueError("fixture Bollinger Bands require positive width")
        percent_b = (candidate_kline.close - lower_band) / band_width

        # CCI도 마지막 19개 확정 typical price와 현재 누적 OHLC 후보만 사용한다.
        typical_prices = (
            *(
                (kline.high + kline.low + kline.close) / Decimal("3")
                for kline in closed_klines[-19:]
            ),
            (
                candidate_kline.high
                + candidate_kline.low
                + candidate_kline.close
            )
            / Decimal("3"),
        )
        typical_average = sum(typical_prices, Decimal("0")) / Decimal(
            _CCI_PERIOD
        )
        mean_deviation = sum(
            (
                abs(typical_price - typical_average)
                for typical_price in typical_prices
            ),
            Decimal("0"),
        ) / Decimal(_CCI_PERIOD)
        cci = (
            Decimal("0")
            if mean_deviation == Decimal("0")
            else (
                (typical_prices[-1] - typical_average)
                / (_CCI_SCALING_CONSTANT * mean_deviation)
            )
        )

    return percent_b, cci
