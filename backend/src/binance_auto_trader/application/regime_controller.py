"""4시간봉 지표 계산과 RegimeSTM Action 수행을 조정하는 controller를 정의한다."""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from enum import Enum
from threading import RLock

from binance_auto_trader.domain.common import Interval, RegimeType
from binance_auto_trader.domain.market import (
    IndicatorSnapshot,
    Kline,
    MarketSnapshot,
    SwingStructure,
)
from binance_auto_trader.domain.regime import (
    ApplyRecommendedRegime,
    RegimeEvaluationContext,
    RegimeEvaluationTrigger,
    RegimeEvent,
    RegimeEventType,
    RegimeResult,
    RegimeSTM,
    RegimeSTMResult,
    RegimeState,
    StartRegimeEvaluation,
)


EMA_PERIOD = 9
EMA_ALPHA = Decimal("0.2")
EMA_COMPLEMENT = Decimal("0.8")
MINIMUM_CLOSED_KLINES = 14
SLOPE_SAMPLE_SIZE = 6
SLOPE_QUANTUM = Decimal("0.00000001")
SWING_SIDE_SIZE = 2
SWING_CHANGE_THRESHOLD = Decimal("0.30")
DECIMAL_PRECISION = 34

_SUCCESS_COMMUNICATION_STEPS = (
    "1.4:MarketDataController->RegimeController.calculate_4h_indicators",
    "1.4.1:RegimeController->IndicatorSnapshot.update",
    "1.5:MarketDataController->RegimeController.recommend_regime",
    "1.5.1:RegimeController->RegimeSTM.handle:trigger",
    "1.5.1:RegimeController->RegimeSTM.handle:EVALUATION_READY",
)


class RegimeEvaluationFailureCode(str, Enum):
    """
    클래스 이름: RegimeEvaluationFailureCode
    기능: REGIME 평가를 적용하지 않은 typed failure와 dedup 사유를 정의한다.
    작성 날짜: 2026/08/20
    """

    MARKET_SNAPSHOT_MISMATCH = "MARKET_SNAPSHOT_MISMATCH"
    MARKET_SNAPSHOT_NOT_READY = "MARKET_SNAPSHOT_NOT_READY"
    INSUFFICIENT_CLOSED_KLINES = "INSUFFICIENT_CLOSED_KLINES"
    INSUFFICIENT_SWING_POINTS = "INSUFFICIENT_SWING_POINTS"
    INVALID_FOUR_HOUR_CANDLES = "INVALID_FOUR_HOUR_CANDLES"
    REPEATED_FAILED_MARKET_VERSION = "REPEATED_FAILED_MARKET_VERSION"
    STALE_MARKET_SNAPSHOT = "STALE_MARKET_SNAPSHOT"
    STALE_FOUR_HOUR_CANDLE = "STALE_FOUR_HOUR_CANDLE"
    DUPLICATE_FOUR_HOUR_CANDLE = "DUPLICATE_FOUR_HOUR_CANDLE"
    INVALID_TRIGGER_STATE = "INVALID_TRIGGER_STATE"
    INVALID_INDICATOR_SNAPSHOT = "INVALID_INDICATOR_SNAPSHOT"
    STM_TRANSITION_REJECTED = "STM_TRANSITION_REJECTED"
    ACTION_CONTRACT_MISMATCH = "ACTION_CONTRACT_MISMATCH"
    INDICATOR_CALCULATION_FAILED = "INDICATOR_CALCULATION_FAILED"


class RegimeEvaluationError(RuntimeError, ValueError):
    """
    클래스 이름: RegimeEvaluationError
    기능: 평가를 fail closed한 typed failure code와 설명을 전달한다.
    작성 날짜: 2026/08/20
    """

    def __init__(
        self,
        failure_code: RegimeEvaluationFailureCode,
        message: str,
    ) -> None:
        """
        함수 이름: __init__()
        기능: typed failure code를 가진 REGIME 평가 예외를 생성한다.
        인자: failure_code -> 평가를 적용하지 않은 정규화 사유
            message -> 운영 trace와 오류 확인에 사용할 설명
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        if not isinstance(failure_code, RegimeEvaluationFailureCode):
            raise TypeError(
                "failure_code must be a RegimeEvaluationFailureCode"
            )
        if not isinstance(message, str):
            raise TypeError("message must be a string")
        if not message.strip():
            raise ValueError("message must not be empty")

        super().__init__(message)
        self.failure_code = failure_code
        self.communication_steps: tuple[str, ...] = ()
        self.event_ids: tuple[str, ...] = ()
        self.transition_ids: tuple[str, ...] = ()
        self.state_before: RegimeState | None = None
        self.state_after: RegimeState | None = None
        self.source_market_version: int | None = None
        self.source_candle_id: str | None = None
        self.recorded_at: datetime | None = None

    def add_progress(
        self,
        communication_steps: tuple[str, ...],
        event_ids: tuple[str, ...],
        transition_ids: tuple[str, ...],
        state_before: RegimeState,
        state_after: RegimeState,
    ) -> None:
        """
        함수 이름: add_progress()
        기능: 실패 전에 실제 실행된 메시지, 이벤트, 전이와 상태를 예외에 기록한다.
        인자: communication_steps -> 실패 전까지 실행한 Communication 메시지
            event_ids -> STM에 전달한 event ID
            transition_ids -> 실패 전에 소비된 Event-Action ID
            state_before -> 평가 cycle 시작 전 STM 상태
            state_after -> 실패가 관측된 시점의 STM 상태
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self.communication_steps = communication_steps
        self.event_ids = event_ids
        self.transition_ids = transition_ids
        self.state_before = state_before
        self.state_after = state_after


@dataclass(frozen=True, slots=True)
class RegimeEvaluationTrace:
    """
    클래스 이름: RegimeEvaluationTrace
    기능: 한 추천 cycle의 메시지, 이벤트, 전이, version과 결과를 불변으로 기록한다.
    작성 날짜: 2026/08/20
    """

    evaluation_id: str
    trigger: RegimeEvaluationTrigger
    communication_steps: tuple[str, ...]
    event_ids: tuple[str, ...]
    transition_ids: tuple[str, ...]
    state_before: RegimeState
    state_after: RegimeState
    source_market_version: int
    source_candle_id: str | None
    recommended_regime: RegimeType | None
    changed: bool | None
    failure_code: RegimeEvaluationFailureCode | None
    recorded_at: datetime | None

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 평가 trace의 식별자, 상태, 결과와 failure 조합을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        if not isinstance(self.evaluation_id, str):
            raise TypeError("evaluation_id must be a string")
        if not self.evaluation_id.strip():
            raise ValueError("evaluation_id must not be empty")
        if not isinstance(self.trigger, RegimeEvaluationTrigger):
            raise TypeError("trigger must be a RegimeEvaluationTrigger")
        if not isinstance(self.communication_steps, tuple):
            raise TypeError("communication_steps must be a tuple")
        if not isinstance(self.event_ids, tuple):
            raise TypeError("event_ids must be a tuple")
        if not isinstance(self.transition_ids, tuple):
            raise TypeError("transition_ids must be a tuple")
        if not isinstance(self.state_before, RegimeState):
            raise TypeError("state_before must be a RegimeState")
        if not isinstance(self.state_after, RegimeState):
            raise TypeError("state_after must be a RegimeState")
        if (
            isinstance(self.source_market_version, bool)
            or not isinstance(self.source_market_version, int)
        ):
            raise TypeError("source_market_version must be an integer")
        if self.source_market_version < 0:
            raise ValueError("source_market_version must not be negative")
        if self.source_candle_id is not None:
            if not isinstance(self.source_candle_id, str):
                raise TypeError("source_candle_id must be a string or None")
            if not self.source_candle_id.strip():
                raise ValueError("source_candle_id must not be empty")
        if self.recommended_regime is not None and not isinstance(
            self.recommended_regime,
            RegimeType,
        ):
            raise TypeError(
                "recommended_regime must be a RegimeType or None"
            )
        if self.changed is not None and not isinstance(self.changed, bool):
            raise TypeError("changed must be a bool or None")
        if self.failure_code is not None and not isinstance(
            self.failure_code,
            RegimeEvaluationFailureCode,
        ):
            raise TypeError(
                "failure_code must be a RegimeEvaluationFailureCode or None"
            )
        if self.recorded_at is not None:
            if not isinstance(self.recorded_at, datetime):
                raise TypeError("recorded_at must be a datetime or None")
            if (
                self.recorded_at.tzinfo is None
                or self.recorded_at.utcoffset() is None
            ):
                raise ValueError("recorded_at must be timezone-aware")

        succeeded = self.failure_code is None
        if succeeded and len(self.transition_ids) != 2:
            raise ValueError("a successful trace requires two transitions")
        if succeeded and self.recommended_regime is None:
            raise ValueError("a successful trace requires a recommendation")
        if succeeded and self.changed is None:
            raise ValueError("a successful trace requires changed")
        if not succeeded and self.changed is not None:
            raise ValueError("a failed trace cannot report changed")


def _create_candle_id(kline: Kline) -> str:
    """
    함수 이름: _create_candle_id()
    기능: symbol, interval과 UTC open time으로 결정론적인 4시간봉 ID를 만든다.
    인자: kline -> 식별할 최신 확정 4시간봉
    반환값: 재전달 dedup에 사용할 candle ID
    작성 날짜: 2026/08/20
    """
    utc_text = kline.open_time.astimezone(timezone.utc).isoformat().replace(
        "+00:00",
        "Z",
    )
    return f"{kline.symbol}:{kline.interval.value}:{utc_text}"


def _find_latest_closed_candle_id(
    market_snapshot: MarketSnapshot | None,
) -> str | None:
    """
    함수 이름: _find_latest_closed_candle_id()
    기능: 실패 trace용으로 준비된 snapshot의 최신 확정 4시간봉 ID를 안전하게 찾는다.
    인자: market_snapshot -> 실패 provenance를 읽을 선택 시장 snapshot
    반환값: 최신 확정 4시간봉 ID 또는 읽을 수 없으면 None
    작성 날짜: 2026/08/20
    """
    if market_snapshot is None or not market_snapshot.ready:
        return None

    closed_klines = tuple(
        kline
        for kline in market_snapshot.klines_by_interval[
            Interval.FOUR_HOURS
        ]
        if kline.closed
    )
    if not closed_klines:
        return None

    latest_closed_kline = max(
        closed_klines,
        key=lambda kline: kline.open_time,
    )
    return _create_candle_id(latest_closed_kline)


def _calculate_ema9_series(closed_klines: tuple[Kline, ...]) -> tuple[Decimal, ...]:
    """
    함수 이름: _calculate_ema9_series()
    기능: 첫 아홉 종가 SMA seed와 alpha 0.2로 확정 EMA9 시계열을 계산한다.
    인자: closed_klines -> 시간순 확정 4시간봉
    반환값: 아홉 번째 확정봉부터 정렬된 EMA9 tuple
    작성 날짜: 2026/08/20
    """
    if len(closed_klines) < MINIMUM_CLOSED_KLINES:
        raise RegimeEvaluationError(
            RegimeEvaluationFailureCode.INSUFFICIENT_CLOSED_KLINES,
            "at least fourteen closed four-hour Klines are required",
        )

    seed_total = sum(
        (kline.close for kline in closed_klines[:EMA_PERIOD]),
        Decimal("0"),
    )
    current_ema9 = seed_total / Decimal(EMA_PERIOD)
    ema9_series = [current_ema9]

    for kline in closed_klines[EMA_PERIOD:]:
        current_ema9 = (
            kline.close * EMA_ALPHA
            + current_ema9 * EMA_COMPLEMENT
        )
        ema9_series.append(current_ema9)

    return tuple(ema9_series)


def _calculate_normalized_slope(
    ema9_series: tuple[Decimal, ...],
    current_price: Decimal,
) -> Decimal:
    """
    함수 이름: _calculate_normalized_slope()
    기능: 최근 여섯 EMA9의 OLS slope를 현재가 대비 %/4시간봉으로 계산한다.
    인자: ema9_series -> 확정봉 EMA9 시계열
        current_price -> 같은 MarketSnapshot version의 진행봉 현재가
    반환값: 소수점 여덟 자리 ROUND_HALF_EVEN slope
    작성 날짜: 2026/08/20
    """
    recent_ema9 = ema9_series[-SLOPE_SAMPLE_SIZE:]
    if len(recent_ema9) != SLOPE_SAMPLE_SIZE:
        raise RegimeEvaluationError(
            RegimeEvaluationFailureCode.INSUFFICIENT_CLOSED_KLINES,
            "six EMA9 values are required for slope calculation",
        )

    x_values = tuple(Decimal(index) for index in range(SLOPE_SAMPLE_SIZE))
    x_mean = Decimal("2.5")
    y_mean = sum(recent_ema9, Decimal("0")) / Decimal(SLOPE_SAMPLE_SIZE)
    numerator = sum(
        (
            (x_value - x_mean) * (y_value - y_mean)
            for x_value, y_value in zip(x_values, recent_ema9)
        ),
        Decimal("0"),
    )
    denominator = sum(
        ((x_value - x_mean) ** 2 for x_value in x_values),
        Decimal("0"),
    )
    raw_slope = numerator / denominator
    normalized_slope = raw_slope / current_price * Decimal("100")
    return normalized_slope.quantize(
        SLOPE_QUANTUM,
        rounding=ROUND_HALF_EVEN,
    )


def _find_swing_values(
    closed_klines: tuple[Kline, ...],
    price_field: str,
    find_highs: bool,
) -> tuple[Decimal, ...]:
    """
    함수 이름: _find_swing_values()
    기능: strict left 2/right 2 규칙으로 확정 swing high 또는 low를 찾는다.
    인자: closed_klines -> 시간순 확정 4시간봉
        price_field -> 비교할 Kline의 high 또는 low 필드 이름
        find_highs -> high pivot이면 True, low pivot이면 False
    반환값: 시간순 확정 swing 값 tuple
    작성 날짜: 2026/08/20
    """
    swing_values: list[Decimal] = []
    for candle_index in range(
        SWING_SIDE_SIZE,
        len(closed_klines) - SWING_SIDE_SIZE,
    ):
        candidate_value = getattr(closed_klines[candle_index], price_field)
        neighboring_klines = (
            *closed_klines[
                candle_index - SWING_SIDE_SIZE:candle_index
            ],
            *closed_klines[
                candle_index + 1:candle_index + SWING_SIDE_SIZE + 1
            ],
        )
        neighboring_values = tuple(
            getattr(kline, price_field)
            for kline in neighboring_klines
        )
        if find_highs:
            is_swing = all(
                candidate_value > neighboring_value
                for neighboring_value in neighboring_values
            )
        else:
            is_swing = all(
                candidate_value < neighboring_value
                for neighboring_value in neighboring_values
            )

        if is_swing:
            swing_values.append(candidate_value)

    return tuple(swing_values)


def _calculate_swing_structure(
    closed_klines: tuple[Kline, ...],
) -> SwingStructure:
    """
    함수 이름: _calculate_swing_structure()
    기능: 최근 확정 swing high와 low의 0.30% 변화로 HH, HL, LH, LL을 만든다.
    인자: closed_klines -> 시간순 확정 4시간봉
    반환값: 확정 point와 구조 boolean을 가진 SwingStructure
    작성 날짜: 2026/08/20
    """
    swing_highs = _find_swing_values(
        closed_klines,
        price_field="high",
        find_highs=True,
    )
    swing_lows = _find_swing_values(
        closed_klines,
        price_field="low",
        find_highs=False,
    )
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        raise RegimeEvaluationError(
            RegimeEvaluationFailureCode.INSUFFICIENT_SWING_POINTS,
            "two confirmed swing highs and lows are required",
        )

    previous_high, latest_high = swing_highs[-2:]
    previous_low, latest_low = swing_lows[-2:]
    high_change = (
        (latest_high - previous_high)
        / abs(previous_high)
        * Decimal("100")
    )
    low_change = (
        (latest_low - previous_low)
        / abs(previous_low)
        * Decimal("100")
    )
    return SwingStructure(
        swing_highs=swing_highs,
        swing_lows=swing_lows,
        has_higher_high=high_change >= SWING_CHANGE_THRESHOLD,
        has_higher_low=low_change >= SWING_CHANGE_THRESHOLD,
        has_lower_high=high_change <= -SWING_CHANGE_THRESHOLD,
        has_lower_low=low_change <= -SWING_CHANGE_THRESHOLD,
    )


def _calculate_live_ema9(
    latest_closed_ema9: Decimal,
    current_price: Decimal,
) -> Decimal:
    """
    함수 이름: _calculate_live_ema9()
    기능: 진행봉 현재가를 최신 확정 EMA9에 한 번만 적용한다.
    인자: latest_closed_ema9 -> 최신 확정봉 EMA9
        current_price -> 같은 MarketSnapshot version의 진행봉 현재가
    반환값: 확정 EMA9 series에 포함하지 않는 live EMA9
    작성 날짜: 2026/08/20
    """
    return (
        current_price * EMA_ALPHA
        + latest_closed_ema9 * EMA_COMPLEMENT
    )


class RegimeController:
    """
    클래스 이름: RegimeController
    기능: same-version 지표 준비, RegimeSTM microstep과 추천 Action을 직렬 조정한다.
    작성 날짜: 2026/08/20
    """

    __slots__ = (
        "_evaluation_lock",
        "_evaluation_traces",
        "_failed_market_snapshot",
        "_failed_market_version",
        "_failed_recorded_at",
        "_failed_source_candle_id",
        "_indicator_snapshot",
        "_indicator_source_candle_open_time",
        "_indicator_source_market_snapshot",
        "_last_regime_result",
        "_latest_processed_candle_open_time",
        "_market_snapshot",
        "_processed_candle_ids",
        "_recommended_regime",
        "_regime_stm",
        "_selected_regime",
    )

    def __init__(
        self,
        regime_stm: RegimeSTM,
        market_snapshot: MarketSnapshot | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 순수 RegimeSTM과 선택적인 authoritative MarketSnapshot을 조립한다.
        인자: regime_stm -> 전이와 Action 요청을 결정할 순수 상태 머신
            market_snapshot -> same-version 검증에 사용할 시장 snapshot
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        if not isinstance(regime_stm, RegimeSTM):
            raise TypeError("regime_stm must be a RegimeSTM")
        if market_snapshot is not None and not isinstance(
            market_snapshot,
            MarketSnapshot,
        ):
            raise TypeError(
                "market_snapshot must be a MarketSnapshot or None"
            )

        self._regime_stm = regime_stm
        self._market_snapshot = market_snapshot
        self._failed_market_snapshot: MarketSnapshot | None = None
        self._failed_market_version: int | None = None
        self._failed_recorded_at: datetime | None = None
        self._failed_source_candle_id: str | None = None
        self._indicator_snapshot: IndicatorSnapshot | None = None
        self._indicator_source_candle_open_time: datetime | None = None
        self._indicator_source_market_snapshot: MarketSnapshot | None = None
        self._recommended_regime: RegimeType | None = None
        self._selected_regime: RegimeType | None = None
        self._last_regime_result: RegimeResult | None = None
        self._latest_processed_candle_open_time: datetime | None = None
        self._processed_candle_ids: set[str] = set()
        self._evaluation_traces: list[RegimeEvaluationTrace] = []
        self._evaluation_lock = RLock()

    @property
    def indicator_snapshot(self) -> IndicatorSnapshot | None:
        """
        함수 이름: indicator_snapshot()
        기능: 마지막으로 정상 계산된 IndicatorSnapshot을 반환한다.
        인자: 없음
        반환값: 마지막 지표 snapshot 또는 아직 없으면 None
        작성 날짜: 2026/08/20
        """
        with self._evaluation_lock:
            return self._indicator_snapshot

    @property
    def recommended_regime(self) -> RegimeType | None:
        """
        함수 이름: recommended_regime()
        기능: 마지막으로 성공 적용한 추천 REGIME을 반환한다.
        인자: 없음
        반환값: 마지막 정상 추천 또는 아직 없으면 None
        작성 날짜: 2026/08/20
        """
        with self._evaluation_lock:
            return self._recommended_regime

    @property
    def selected_regime(self) -> RegimeType | None:
        """
        함수 이름: selected_regime()
        기능: 추천과 분리된 사용자 선택 REGIME을 반환한다.
        인자: 없음
        반환값: Phase 3에서 변경하지 않는 사용자 선택값
        작성 날짜: 2026/08/20
        """
        with self._evaluation_lock:
            return self._selected_regime

    @property
    def last_regime_result(self) -> RegimeResult | None:
        """
        함수 이름: last_regime_result()
        기능: 마지막 정상 추천의 상세 결과 metadata를 반환한다.
        인자: 없음
        반환값: 마지막 RegimeResult 또는 아직 없으면 None
        작성 날짜: 2026/08/20
        """
        with self._evaluation_lock:
            return self._last_regime_result

    @property
    def evaluation_traces(self) -> tuple[RegimeEvaluationTrace, ...]:
        """
        함수 이름: evaluation_traces()
        기능: 성공, 실패와 dedup 평가 trace를 append 순서로 반환한다.
        인자: 없음
        반환값: 외부에서 변경할 수 없는 trace tuple
        작성 날짜: 2026/08/20
        """
        with self._evaluation_lock:
            return tuple(self._evaluation_traces)

    @property
    def last_error(self) -> RegimeEvaluationTrace | None:
        """
        함수 이름: last_error()
        기능: 가장 최근의 typed failure 또는 dedup trace를 반환한다.
        인자: 없음
        반환값: 최근 오류 trace 또는 오류가 없으면 None
        작성 날짜: 2026/08/20
        """
        with self._evaluation_lock:
            for evaluation_trace in reversed(self._evaluation_traces):
                if evaluation_trace.failure_code is not None:
                    return evaluation_trace

            return None

    def calculate_4h_indicators(
        self,
        snapshot: MarketSnapshot,
    ) -> IndicatorSnapshot:
        """
        함수 이름: calculate_4h_indicators()
        기능: 한 MarketSnapshot version에서 EMA9, slope, swing과 live EMA9을 계산한다.
        인자: snapshot -> 네 주기의 authoritative 시장 snapshot
        반환값: provenance와 지표가 원자적으로 준비된 IndicatorSnapshot
        작성 날짜: 2026/08/20
        """
        if not isinstance(snapshot, MarketSnapshot):
            raise TypeError("snapshot must be a MarketSnapshot")

        with self._evaluation_lock:
            self._bind_market_snapshot(snapshot)
            if not snapshot.ready:
                raise RegimeEvaluationError(
                    RegimeEvaluationFailureCode.MARKET_SNAPSHOT_NOT_READY,
                    "MarketSnapshot must be ready before indicator calculation",
                )

            source_market_version = snapshot.version
            if (
                self._failed_market_snapshot is snapshot
                and self._failed_market_version == source_market_version
            ):
                failed_recorded_at = self._failed_recorded_at
                if failed_recorded_at is None:
                    raise RuntimeError(
                        "failed calculation must preserve recorded_at"
                    )
                repeated_failure = self._create_calculation_error(
                    RegimeEvaluationFailureCode.REPEATED_FAILED_MARKET_VERSION,
                    "failed MarketSnapshot version cannot be retried",
                    source_market_version,
                    self._failed_source_candle_id,
                    failed_recorded_at,
                )
                raise repeated_failure
            four_hour_klines = tuple(
                snapshot.klines_by_interval[Interval.FOUR_HOURS]
            )
            calculated_at = snapshot.updated_at
            if calculated_at is None:
                raise RegimeEvaluationError(
                    RegimeEvaluationFailureCode.MARKET_SNAPSHOT_NOT_READY,
                    "ready MarketSnapshot must have updated_at",
                )

            deduplicated_klines = {
                kline.open_time: kline
                for kline in four_hour_klines
            }
            sorted_klines = tuple(
                sorted(
                    deduplicated_klines.values(),
                    key=lambda kline: kline.open_time,
                )
            )
            closed_klines = tuple(
                kline for kline in sorted_klines if kline.closed
            )
            open_klines = tuple(
                kline for kline in sorted_klines if not kline.closed
            )
            failure_source_candle_id = (
                _create_candle_id(closed_klines[-1])
                if closed_klines
                else None
            )
            if (
                len(open_klines) != 1
                or not sorted_klines
                or sorted_klines[-1] is not open_klines[0]
            ):
                self._raise_calculation_failure(
                    snapshot,
                    source_market_version,
                    RegimeEvaluationFailureCode.INVALID_FOUR_HOUR_CANDLES,
                    "exactly the latest four-hour Kline must be open",
                    failure_source_candle_id,
                    calculated_at,
                )
            if len(closed_klines) < MINIMUM_CLOSED_KLINES:
                self._raise_calculation_failure(
                    snapshot,
                    source_market_version,
                    RegimeEvaluationFailureCode.INSUFFICIENT_CLOSED_KLINES,
                    "at least fourteen closed four-hour Klines are required",
                    failure_source_candle_id,
                    calculated_at,
                )

            current_price = open_klines[0].close
            if failure_source_candle_id is None:
                raise RuntimeError(
                    "valid indicator input requires a closed candle ID"
                )
            source_candle_id = failure_source_candle_id
            try:
                with localcontext() as decimal_context:
                    decimal_context.prec = DECIMAL_PRECISION
                    decimal_context.rounding = ROUND_HALF_EVEN
                    ema9_series = _calculate_ema9_series(closed_klines)
                    ema9_slope = _calculate_normalized_slope(
                        ema9_series,
                        current_price,
                    )
                    swing_structure = _calculate_swing_structure(
                        closed_klines
                    )
                    live_ema9 = _calculate_live_ema9(
                        ema9_series[-1],
                        current_price,
                    )
            except RegimeEvaluationError as error:
                self._raise_calculation_failure(
                    snapshot,
                    source_market_version,
                    error.failure_code,
                    str(error),
                    source_candle_id,
                    calculated_at,
                    cause=error,
                )
            except ArithmeticError as error:
                self._raise_calculation_failure(
                    snapshot,
                    source_market_version,
                    RegimeEvaluationFailureCode.INDICATOR_CALCULATION_FAILED,
                    "Decimal indicator calculation failed",
                    source_candle_id,
                    calculated_at,
                    cause=error,
                )

            if snapshot.version != source_market_version:
                stale_error = self._create_calculation_error(
                    RegimeEvaluationFailureCode.STALE_MARKET_SNAPSHOT,
                    "MarketSnapshot version changed during calculation",
                    source_market_version,
                    source_candle_id,
                    calculated_at,
                )
                raise stale_error

            next_indicator_snapshot = IndicatorSnapshot(
                symbol=snapshot.symbol,
                current_price=current_price,
                source_market_version=source_market_version,
                source_candle_id=source_candle_id,
                calculated_at=calculated_at,
            )
            next_indicator_snapshot.update(
                ema9_series=ema9_series,
                ema9_slope=ema9_slope,
                swing_structure=swing_structure,
                live_ema9=live_ema9,
            )
            self._failed_market_snapshot = None
            self._failed_market_version = None
            self._failed_recorded_at = None
            self._failed_source_candle_id = None
            self._indicator_snapshot = next_indicator_snapshot
            self._indicator_source_candle_open_time = (
                closed_klines[-1].open_time
            )
            self._indicator_source_market_snapshot = snapshot
            return next_indicator_snapshot

    def recommend_regime(
        self,
        indicators: IndicatorSnapshot,
    ) -> RegimeType:
        """
        함수 이름: recommend_regime()
        기능: 준비된 지표로 canonical 두 microstep Action 경로를 실행하고 타입을 반환한다.
        인자: indicators -> calculate_4h_indicators가 만든 same-version 지표
        반환값: ApplyRecommendedRegime이 성공 적용한 추천 REGIME
        작성 날짜: 2026/08/20
        """
        if not isinstance(indicators, IndicatorSnapshot):
            raise TypeError("indicators must be an IndicatorSnapshot")

        with self._evaluation_lock:
            trigger = self._infer_evaluation_trigger()
            try:
                regime_result = self._run_prepared_evaluation(
                    trigger,
                    indicators,
                )
            except RegimeEvaluationError as error:
                self._record_failure(
                    trigger,
                    indicators,
                    error.failure_code,
                    progress_error=error,
                )
                raise

            return regime_result.recommended_type

    def evaluate_regime(
        self,
        trigger: RegimeEvaluationTrigger,
        market_snapshot: MarketSnapshot,
    ) -> RegimeResult | None:
        """
        함수 이름: evaluate_regime()
        기능: 외부 trigger에서 최신 version 지표 준비와 두 STM microstep을 직렬 실행한다.
        인자: trigger -> 최초 평가 또는 확정 4시간봉 마감 원인
            market_snapshot -> 평가할 authoritative 시장 snapshot
        반환값: 정상 추천 RegimeResult 또는 fail closed와 dedup이면 None
        작성 날짜: 2026/08/20
        """
        if not isinstance(trigger, RegimeEvaluationTrigger):
            raise TypeError("trigger must be a RegimeEvaluationTrigger")
        if not isinstance(market_snapshot, MarketSnapshot):
            raise TypeError("market_snapshot must be a MarketSnapshot")

        with self._evaluation_lock:
            stale_repreparation_attempted = False
            while True:
                indicators: IndicatorSnapshot | None = None
                trace_count_before = len(self._evaluation_traces)
                try:
                    indicators = self.calculate_4h_indicators(
                        market_snapshot
                    )
                    if (
                        indicators.source_candle_id
                        in self._processed_candle_ids
                    ):
                        duplicate_error = RegimeEvaluationError(
                            RegimeEvaluationFailureCode.DUPLICATE_FOUR_HOUR_CANDLE,
                            "four-hour candle was already evaluated successfully",
                        )
                        duplicate_error.add_progress(
                            communication_steps=(
                                _SUCCESS_COMMUNICATION_STEPS[:2]
                            ),
                            event_ids=(),
                            transition_ids=(),
                            state_before=self._regime_stm.current_state,
                            state_after=self._regime_stm.current_state,
                        )
                        raise duplicate_error
                    expected_trigger = self._infer_evaluation_trigger()
                    if trigger is not expected_trigger:
                        trigger_error = RegimeEvaluationError(
                            RegimeEvaluationFailureCode.INVALID_TRIGGER_STATE,
                            "trigger does not match the current RegimeSTM state",
                        )
                        trigger_error.add_progress(
                            communication_steps=(
                                _SUCCESS_COMMUNICATION_STEPS[:2]
                            ),
                            event_ids=(),
                            transition_ids=(),
                            state_before=self._regime_stm.current_state,
                            state_after=self._regime_stm.current_state,
                        )
                        raise trigger_error
                    self.recommend_regime(indicators)
                    if self._last_regime_result is None:
                        raise RuntimeError(
                            "successful recommendation must create RegimeResult"
                        )

                    return self._last_regime_result
                except RegimeEvaluationError as error:
                    if (
                        error.failure_code
                        is RegimeEvaluationFailureCode.STALE_MARKET_SNAPSHOT
                    ):
                        if not stale_repreparation_attempted:
                            stale_repreparation_attempted = True
                            continue
                        if len(self._evaluation_traces) == trace_count_before:
                            self._record_failure(
                                trigger,
                                indicators,
                                error.failure_code,
                                market_snapshot,
                                error,
                            )
                        return None
                    if len(self._evaluation_traces) == trace_count_before:
                        self._record_failure(
                            trigger,
                            indicators,
                            error.failure_code,
                            market_snapshot,
                            error,
                        )
                    return None

    def _infer_evaluation_trigger(self) -> RegimeEvaluationTrigger:
        """
        함수 이름: _infer_evaluation_trigger()
        기능: 현재 RegimeSTM 상태에서 허용되는 최초 또는 4시간봉 재평가 trigger를 반환한다.
        인자: 없음
        반환값: 현재 상태와 일치하는 RegimeEvaluationTrigger
        작성 날짜: 2026/08/20
        """
        if self._regime_stm.current_state is RegimeState.INITIAL:
            return RegimeEvaluationTrigger.INITIAL
        if self._regime_stm.current_state is RegimeState.FOUR_HOUR_CANDLE_EVALUATION:
            raise RegimeEvaluationError(
                RegimeEvaluationFailureCode.INVALID_TRIGGER_STATE,
                "RegimeSTM cannot begin a cycle from evaluation state",
            )

        return RegimeEvaluationTrigger.FOUR_HOUR_CANDLE_CLOSE

    def _bind_market_snapshot(self, snapshot: MarketSnapshot) -> None:
        """
        함수 이름: _bind_market_snapshot()
        기능: Controller가 한 authoritative MarketSnapshot identity만 사용하게 고정한다.
        인자: snapshot -> 이번 계산에 전달된 시장 snapshot
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        if self._market_snapshot is None:
            self._market_snapshot = snapshot
            return

        if self._market_snapshot is not snapshot:
            raise RegimeEvaluationError(
                RegimeEvaluationFailureCode.MARKET_SNAPSHOT_MISMATCH,
                "RegimeController cannot switch MarketSnapshot identity",
            )

    def _remember_failed_calculation(
        self,
        snapshot: MarketSnapshot,
        source_market_version: int,
        source_candle_id: str | None,
        recorded_at: datetime,
    ) -> None:
        """
        함수 이름: _remember_failed_calculation()
        기능: 같은 MarketSnapshot version의 즉시 계산 재시도를 막도록 실패를 기억한다.
        인자: snapshot -> 계산에 실패한 authoritative 시장 snapshot
            source_market_version -> 계산 입력으로 캡처했던 version
            source_candle_id -> 계산 입력의 최신 확정 4H candle ID
            recorded_at -> 계산 입력 MarketSnapshot의 UTC 갱신 시각
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self._failed_market_snapshot = snapshot
        self._failed_market_version = source_market_version
        self._failed_source_candle_id = source_candle_id
        self._failed_recorded_at = recorded_at

    def _raise_calculation_failure(
        self,
        snapshot: MarketSnapshot,
        source_market_version: int,
        failure_code: RegimeEvaluationFailureCode,
        message: str,
        source_candle_id: str | None,
        recorded_at: datetime,
        cause: Exception | None = None,
    ) -> None:
        """
        함수 이름: _raise_calculation_failure()
        기능: 계산 중 version 변경을 stale로 우선 처리하고 안정된 version 실패만 기억한다.
        인자: snapshot -> 계산에 사용한 authoritative 시장 snapshot
            source_market_version -> 계산 시작 시 캡처한 version
            failure_code -> 안정된 version이면 기록할 typed failure
            message -> typed failure 설명
            source_candle_id -> 실패 입력의 최신 확정 4H candle ID
            recorded_at -> 실패 입력 MarketSnapshot의 UTC 갱신 시각
            cause -> 원래 계산 예외 또는 없으면 None
        반환값: 정상 반환하지 않고 RegimeEvaluationError 발생
        작성 날짜: 2026/08/20
        """
        if snapshot.version != source_market_version:
            stale_error = self._create_calculation_error(
                RegimeEvaluationFailureCode.STALE_MARKET_SNAPSHOT,
                "MarketSnapshot version changed during failed calculation",
                source_market_version,
                source_candle_id,
                recorded_at,
            )
            raise stale_error from cause

        self._remember_failed_calculation(
            snapshot,
            source_market_version,
            source_candle_id,
            recorded_at,
        )
        if snapshot.version != source_market_version:
            self._failed_market_snapshot = None
            self._failed_market_version = None
            self._failed_source_candle_id = None
            self._failed_recorded_at = None
            stale_error = self._create_calculation_error(
                RegimeEvaluationFailureCode.STALE_MARKET_SNAPSHOT,
                "MarketSnapshot version changed while recording failure",
                source_market_version,
                source_candle_id,
                recorded_at,
            )
            raise stale_error from cause

        calculation_error = self._create_calculation_error(
            failure_code,
            message,
            source_market_version,
            source_candle_id,
            recorded_at,
        )
        raise calculation_error from cause

    def _create_calculation_error(
        self,
        failure_code: RegimeEvaluationFailureCode,
        message: str,
        source_market_version: int,
        source_candle_id: str | None,
        recorded_at: datetime,
    ) -> RegimeEvaluationError:
        """
        함수 이름: _create_calculation_error()
        기능: 계산 당시 source provenance를 고정한 typed 평가 예외를 만든다.
        인자: failure_code -> 평가를 중단한 typed failure
            message -> failure 설명
            source_market_version -> 계산 시작 시 캡처한 MarketSnapshot version
            source_candle_id -> 계산 입력의 최신 확정 4H candle ID
            recorded_at -> 계산 입력 MarketSnapshot의 UTC 갱신 시각
        반환값: source provenance가 고정된 RegimeEvaluationError
        작성 날짜: 2026/08/20
        """
        calculation_error = RegimeEvaluationError(failure_code, message)
        calculation_error.source_market_version = source_market_version
        calculation_error.source_candle_id = source_candle_id
        calculation_error.recorded_at = recorded_at
        return calculation_error

    def _run_prepared_evaluation(
        self,
        trigger: RegimeEvaluationTrigger,
        indicators: IndicatorSnapshot,
    ) -> RegimeResult:
        """
        함수 이름: _run_prepared_evaluation()
        기능: 완성된 Context로 시작 transition과 EVALUATION_READY transition을 직렬 실행한다.
        인자: trigger -> 최초 또는 확정 4시간봉 마감 평가 원인
            indicators -> 같은 source version에서 준비된 지표 snapshot
        반환값: 추천 Action을 적용한 RegimeResult
        작성 날짜: 2026/08/20
        """
        if not indicators.ready:
            raise RegimeEvaluationError(
                RegimeEvaluationFailureCode.INVALID_INDICATOR_SNAPSHOT,
                "IndicatorSnapshot must be ready",
            )
        source_market_snapshot = (
            self._market_snapshot
            if self._market_snapshot is not None
            else self._indicator_source_market_snapshot
        )
        if source_market_snapshot is None:
            raise RegimeEvaluationError(
                RegimeEvaluationFailureCode.MARKET_SNAPSHOT_MISMATCH,
                "RegimeController has no authoritative MarketSnapshot",
            )
        if self._indicator_snapshot is not indicators:
            raise RegimeEvaluationError(
                RegimeEvaluationFailureCode.INVALID_INDICATOR_SNAPSHOT,
                "indicators must be the latest Controller calculation",
            )
        if source_market_snapshot.version != indicators.source_market_version:
            raise RegimeEvaluationError(
                RegimeEvaluationFailureCode.STALE_MARKET_SNAPSHOT,
                "IndicatorSnapshot source version is stale",
            )
        if indicators.source_candle_id in self._processed_candle_ids:
            raise RegimeEvaluationError(
                RegimeEvaluationFailureCode.DUPLICATE_FOUR_HOUR_CANDLE,
                "four-hour candle was already evaluated successfully",
            )
        source_candle_open_time = self._indicator_source_candle_open_time
        if source_candle_open_time is None:
            raise RuntimeError(
                "latest IndicatorSnapshot must have a source candle time"
            )
        if (
            self._latest_processed_candle_open_time is not None
            and source_candle_open_time
            <= self._latest_processed_candle_open_time
        ):
            raise RegimeEvaluationError(
                RegimeEvaluationFailureCode.STALE_FOUR_HOUR_CANDLE,
                "four-hour candle is older than the processed watermark",
            )

        state_before = self._regime_stm.current_state
        if (
            trigger is RegimeEvaluationTrigger.INITIAL
            and state_before is not RegimeState.INITIAL
        ):
            raise RegimeEvaluationError(
                RegimeEvaluationFailureCode.INVALID_TRIGGER_STATE,
                "INITIAL trigger requires RegimeState.INITIAL",
            )
        if (
            trigger is RegimeEvaluationTrigger.FOUR_HOUR_CANDLE_CLOSE
            and state_before in (
                RegimeState.INITIAL,
                RegimeState.FOUR_HOUR_CANDLE_EVALUATION,
            )
        ):
            raise RegimeEvaluationError(
                RegimeEvaluationFailureCode.INVALID_TRIGGER_STATE,
                "candle-close trigger requires a recommended state",
            )

        evaluation_id = self._create_evaluation_id(trigger, indicators)
        evaluation_context = self._create_evaluation_context(
            evaluation_id,
            indicators,
        )
        external_event = self._create_external_event(
            trigger,
            evaluation_id,
            indicators,
        )

        # version 확인 직후 두 동기 microstep을 실행하는 지점을 평가의 선형화 시점으로 둔다.
        if source_market_snapshot.version != indicators.source_market_version:
            raise RegimeEvaluationError(
                RegimeEvaluationFailureCode.STALE_MARKET_SNAPSHOT,
                "IndicatorSnapshot became stale before STM evaluation",
            )
        start_result = self._regime_stm.handle(external_event)
        try:
            start_action = self._require_action(
                start_result,
                StartRegimeEvaluation,
                evaluation_id,
            )
            self._validate_start_action(
                start_action,
                evaluation_context,
                trigger,
            )
        except RegimeEvaluationError as error:
            start_transition_ids = (
                (start_result.transition_id,)
                if start_result.transition_id is not None
                else ()
            )
            error.add_progress(
                communication_steps=_SUCCESS_COMMUNICATION_STEPS[:4],
                event_ids=(external_event.event_id,),
                transition_ids=start_transition_ids,
                state_before=state_before,
                state_after=self._regime_stm.current_state,
            )
            raise

        ready_event = RegimeEvent(
            event_type=RegimeEventType.EVALUATION_READY,
            event_id=f"{evaluation_id}:EVALUATION_READY",
            occurred_at=indicators.calculated_at,
            evaluation_id=evaluation_id,
            source_candle_id=indicators.source_candle_id,
        )
        recommendation_result = self._regime_stm.handle(
            ready_event,
            evaluation_context,
        )
        try:
            recommendation_action = self._require_action(
                recommendation_result,
                ApplyRecommendedRegime,
                evaluation_id,
            )
            self._validate_recommendation_action(
                recommendation_action,
                evaluation_context,
            )
        except RegimeEvaluationError as error:
            transition_ids = tuple(
                transition_id
                for transition_id in (
                    start_result.transition_id,
                    recommendation_result.transition_id,
                )
                if transition_id is not None
            )
            error.add_progress(
                communication_steps=_SUCCESS_COMMUNICATION_STEPS,
                event_ids=(external_event.event_id, ready_event.event_id),
                transition_ids=transition_ids,
                state_before=state_before,
                state_after=self._regime_stm.current_state,
            )
            raise
        return self._apply_recommended_regime(
            recommendation_action,
            start_result,
            recommendation_result,
            external_event,
            ready_event,
            indicators,
        )

    def _create_evaluation_id(
        self,
        trigger: RegimeEvaluationTrigger,
        indicators: IndicatorSnapshot,
    ) -> str:
        """
        함수 이름: _create_evaluation_id()
        기능: trigger, market version과 candle ID로 replay 가능한 평가 ID를 만든다.
        인자: trigger -> 평가 시작 원인
            indicators -> source version과 candle ID를 가진 지표 snapshot
        반환값: 한 cycle의 두 microstep과 Action을 묶는 evaluation ID
        작성 날짜: 2026/08/20
        """
        return (
            f"regime-evaluation:{trigger.name}:"
            f"{indicators.source_market_version}:"
            f"{indicators.source_candle_id}"
        )

    def _create_evaluation_context(
        self,
        evaluation_id: str,
        indicators: IndicatorSnapshot,
    ) -> RegimeEvaluationContext:
        """
        함수 이름: _create_evaluation_context()
        기능: IndicatorSnapshot provenance와 guard 입력을 하나의 불변 Context로 만든다.
        인자: evaluation_id -> 현재 평가 cycle 식별자
            indicators -> same-version 4시간봉 지표 snapshot
        반환값: RegimeSTM EVALUATION_READY에 전달할 Context
        작성 날짜: 2026/08/20
        """
        swing_structure = indicators.swing_structure
        return RegimeEvaluationContext(
            evaluation_id=evaluation_id,
            symbol=indicators.symbol,
            timeframe=indicators.timeframe,
            ema9_slope=indicators.ema9_slope,
            has_higher_high=swing_structure.has_higher_high,
            has_higher_low=swing_structure.has_higher_low,
            has_lower_high=swing_structure.has_lower_high,
            has_lower_low=swing_structure.has_lower_low,
            current_price=indicators.current_price,
            live_ema9=indicators.live_ema9,
            source_market_version=indicators.source_market_version,
            source_candle_id=indicators.source_candle_id,
            calculated_at=indicators.calculated_at,
        )

    def _create_external_event(
        self,
        trigger: RegimeEvaluationTrigger,
        evaluation_id: str,
        indicators: IndicatorSnapshot,
    ) -> RegimeEvent:
        """
        함수 이름: _create_external_event()
        기능: 최초 또는 확정 4시간봉 마감 trigger를 canonical RegimeEvent로 변환한다.
        인자: trigger -> 외부 평가 시작 원인
            evaluation_id -> 현재 평가 cycle 식별자
            indicators -> source candle과 계산 시각을 가진 지표 snapshot
        반환값: 첫 RegimeSTM microstep에 전달할 불변 event
        작성 날짜: 2026/08/20
        """
        event_type = (
            RegimeEventType.INITIAL_EVALUATION_REQUESTED
            if trigger is RegimeEvaluationTrigger.INITIAL
            else RegimeEventType.FOUR_HOUR_CANDLE_CLOSED
        )
        return RegimeEvent(
            event_type=event_type,
            event_id=f"{evaluation_id}:{event_type.name}",
            occurred_at=indicators.calculated_at,
            evaluation_id=evaluation_id,
            source_candle_id=indicators.source_candle_id,
        )

    def _require_action(
        self,
        stm_result: RegimeSTMResult,
        expected_action_type: type[
            StartRegimeEvaluation | ApplyRecommendedRegime
        ],
        expected_evaluation_id: str,
    ) -> StartRegimeEvaluation | ApplyRecommendedRegime:
        """
        함수 이름: _require_action()
        기능: STM 결과가 소비된 정확히 한 개의 예상 Action을 포함하는지 검증한다.
        인자: stm_result -> 한 microstep의 STM 결정 결과
            expected_action_type -> 현재 단계에서 허용되는 Action class
            expected_evaluation_id -> 현재 cycle의 canonical evaluation ID
        반환값: 타입과 개수가 검증된 Action 요청
        작성 날짜: 2026/08/20
        """
        if not isinstance(stm_result, RegimeSTMResult):
            raise TypeError("stm_result must be a RegimeSTMResult")
        if stm_result.evaluation_id != expected_evaluation_id:
            raise RegimeEvaluationError(
                RegimeEvaluationFailureCode.ACTION_CONTRACT_MISMATCH,
                "RegimeSTM result evaluation_id does not match the cycle",
            )
        if not stm_result.consumed or stm_result.transition_id is None:
            raise RegimeEvaluationError(
                RegimeEvaluationFailureCode.STM_TRANSITION_REJECTED,
                "RegimeSTM did not consume an expected evaluation event",
            )
        if len(stm_result.action_requests) != 1:
            raise RegimeEvaluationError(
                RegimeEvaluationFailureCode.ACTION_CONTRACT_MISMATCH,
                "RegimeSTM result must contain exactly one Action request",
            )

        action_request = stm_result.action_requests[0]
        if not isinstance(action_request, expected_action_type):
            raise RegimeEvaluationError(
                RegimeEvaluationFailureCode.ACTION_CONTRACT_MISMATCH,
                "RegimeSTM returned an unexpected Action request type",
            )

        return action_request

    def _validate_start_action(
        self,
        action: StartRegimeEvaluation | ApplyRecommendedRegime,
        context: RegimeEvaluationContext,
        expected_trigger: RegimeEvaluationTrigger,
    ) -> None:
        """
        함수 이름: _validate_start_action()
        기능: StartRegimeEvaluation과 준비 Context의 evaluation/candle ID를 대조한다.
        인자: action -> 첫 microstep이 요청한 Action
            context -> 다음 EVALUATION_READY에 사용할 준비 Context
            expected_trigger -> 외부 event와 일치해야 하는 평가 trigger
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        if not isinstance(action, StartRegimeEvaluation):
            raise RegimeEvaluationError(
                RegimeEvaluationFailureCode.ACTION_CONTRACT_MISMATCH,
                "start microstep requires StartRegimeEvaluation",
            )
        if (
            action.evaluation_id != context.evaluation_id
            or action.source_candle_id != context.source_candle_id
            or action.trigger is not expected_trigger
        ):
            raise RegimeEvaluationError(
                RegimeEvaluationFailureCode.ACTION_CONTRACT_MISMATCH,
                "start Action provenance does not match prepared Context",
            )

    def _validate_recommendation_action(
        self,
        action: StartRegimeEvaluation | ApplyRecommendedRegime,
        context: RegimeEvaluationContext,
    ) -> None:
        """
        함수 이름: _validate_recommendation_action()
        기능: ApplyRecommendedRegime과 평가 Context의 evaluation/candle ID를 대조한다.
        인자: action -> 두 번째 microstep이 요청한 Action
            context -> 추천 guard에 사용한 불변 Context
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        if not isinstance(action, ApplyRecommendedRegime):
            raise RegimeEvaluationError(
                RegimeEvaluationFailureCode.ACTION_CONTRACT_MISMATCH,
                "recommendation microstep requires ApplyRecommendedRegime",
            )
        if (
            action.evaluation_id != context.evaluation_id
            or action.source_candle_id != context.source_candle_id
        ):
            raise RegimeEvaluationError(
                RegimeEvaluationFailureCode.ACTION_CONTRACT_MISMATCH,
                "recommendation Action provenance does not match Context",
            )

    def _apply_recommended_regime(
        self,
        action: ApplyRecommendedRegime,
        start_result: RegimeSTMResult,
        recommendation_result: RegimeSTMResult,
        external_event: RegimeEvent,
        ready_event: RegimeEvent,
        indicators: IndicatorSnapshot,
    ) -> RegimeResult:
        """
        함수 이름: _apply_recommended_regime()
        기능: 검증된 추천 Action의 값과 metadata를 Controller 상태와 trace에 원자 반영한다.
        인자: action -> 적용할 추천 타입과 evaluation provenance
            start_result -> 첫 microstep의 transition 결과
            recommendation_result -> 추천 microstep의 transition 결과
            external_event -> 최초 또는 candle-close RegimeEvent
            ready_event -> 같은 evaluation의 EVALUATION_READY event
            indicators -> 추천 Context의 source version 지표
        반환값: 적용한 추천의 RegimeResult
        작성 날짜: 2026/08/20
        """
        if start_result.transition_id is None:
            raise RuntimeError("start result must have a transition ID")
        if recommendation_result.transition_id is None:
            raise RuntimeError(
                "recommendation result must have a transition ID"
            )

        previous_recommended_regime = self._recommended_regime
        changed = previous_recommended_regime is not action.regime_type
        next_regime_result = RegimeResult(
            evaluation_id=action.evaluation_id,
            recommended_type=action.regime_type,
            previous_recommended_type=previous_recommended_regime,
            changed=changed,
            transition_id=recommendation_result.transition_id,
            state=recommendation_result.state_after,
            source_market_version=indicators.source_market_version,
            source_candle_id=indicators.source_candle_id,
            calculated_at=indicators.calculated_at,
        )
        next_trace = RegimeEvaluationTrace(
            evaluation_id=action.evaluation_id,
            trigger=(
                RegimeEvaluationTrigger.INITIAL
                if external_event.event_type
                is RegimeEventType.INITIAL_EVALUATION_REQUESTED
                else RegimeEvaluationTrigger.FOUR_HOUR_CANDLE_CLOSE
            ),
            communication_steps=_SUCCESS_COMMUNICATION_STEPS,
            event_ids=(external_event.event_id, ready_event.event_id),
            transition_ids=(
                start_result.transition_id,
                recommendation_result.transition_id,
            ),
            state_before=start_result.state_before,
            state_after=recommendation_result.state_after,
            source_market_version=indicators.source_market_version,
            source_candle_id=indicators.source_candle_id,
            recommended_regime=action.regime_type,
            changed=changed,
            failure_code=None,
            recorded_at=indicators.calculated_at,
        )

        self._recommended_regime = action.regime_type
        self._last_regime_result = next_regime_result
        self._processed_candle_ids.add(indicators.source_candle_id)
        self._latest_processed_candle_open_time = (
            self._indicator_source_candle_open_time
        )
        self._evaluation_traces.append(next_trace)
        return next_regime_result

    def _record_failure(
        self,
        trigger: RegimeEvaluationTrigger,
        indicators: IndicatorSnapshot | None,
        failure_code: RegimeEvaluationFailureCode,
        market_snapshot: MarketSnapshot | None = None,
        progress_error: RegimeEvaluationError | None = None,
    ) -> None:
        """
        함수 이름: _record_failure()
        기능: 추천과 STM 상태를 바꾸지 않고 typed failure provenance를 trace에 추가한다.
        인자: trigger -> 실패한 평가 시작 원인
            indicators -> 준비됐다면 실패한 지표 snapshot
            failure_code -> 적용을 중단한 정규화 사유
            market_snapshot -> 지표 준비 전에 실패했다면 원본 시장 snapshot
            progress_error -> 실제 microstep 진행 metadata를 기록한 평가 예외
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        if indicators is not None:
            source_market_version = indicators.source_market_version
            source_candle_id = indicators.source_candle_id
            recorded_at = indicators.calculated_at
            evaluation_id = self._create_evaluation_id(
                trigger,
                indicators,
            )
        else:
            source_market_version = (
                progress_error.source_market_version
                if progress_error is not None
                and progress_error.source_market_version is not None
                else (
                    market_snapshot.version
                    if market_snapshot is not None
                    else 0
                )
            )
            source_candle_id = (
                progress_error.source_candle_id
                if progress_error is not None
                and progress_error.source_market_version is not None
                else _find_latest_closed_candle_id(market_snapshot)
            )
            recorded_at = (
                progress_error.recorded_at
                if progress_error is not None
                and progress_error.source_market_version is not None
                else (
                    market_snapshot.updated_at
                    if market_snapshot is not None
                    else None
                )
            )
            evaluation_id = (
                f"regime-evaluation:{trigger.name}:"
                f"{source_market_version}:"
                f"{source_candle_id or 'UNAVAILABLE'}"
            )

        default_communication_steps = (
            _SUCCESS_COMMUNICATION_STEPS[:3]
            if indicators is not None
            else _SUCCESS_COMMUNICATION_STEPS[:1]
        )
        communication_steps = (
            progress_error.communication_steps
            if progress_error is not None
            and progress_error.communication_steps
            else default_communication_steps
        )
        event_ids = (
            progress_error.event_ids
            if progress_error is not None
            else ()
        )
        transition_ids = (
            progress_error.transition_ids
            if progress_error is not None
            else ()
        )
        state_before = (
            progress_error.state_before
            if progress_error is not None
            and progress_error.state_before is not None
            else self._regime_stm.current_state
        )
        state_after = (
            progress_error.state_after
            if progress_error is not None
            and progress_error.state_after is not None
            else self._regime_stm.current_state
        )

        failure_trace = RegimeEvaluationTrace(
            evaluation_id=evaluation_id,
            trigger=trigger,
            communication_steps=communication_steps,
            event_ids=event_ids,
            transition_ids=transition_ids,
            state_before=state_before,
            state_after=state_after,
            source_market_version=source_market_version,
            source_candle_id=source_candle_id,
            recommended_regime=self._recommended_regime,
            changed=None,
            failure_code=failure_code,
            recorded_at=recorded_at,
        )
        self._evaluation_traces.append(failure_trace)
