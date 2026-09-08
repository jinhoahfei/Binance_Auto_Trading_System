"""TradingSTM snapshot과 single-writer mutable TradingContext를 정의한다."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import RLock

from ..common import RegimeType
from .account import Account
from .action_requests import (
    CloseLowerEvent,
    OpenLowerEvent,
    PatchRuntimeContext,
    ResetCaseBContext,
    ResetCaseCContext,
    RuntimeField,
    RuntimeFieldChange,
)
from .results import TradingSTMResult
from .timers import TradingTimerSnapshot
from .states import (
    ExitReason,
    OrderAttemptKind,
    OrderSide,
    PositionReturnState,
    StrategyType,
    TradingPhase,
)


# 모든 기본 시장·포지션 수치가 float 대신 동일한 Decimal 영점을 공유한다.
ZERO_DECIMAL = Decimal("0")


@dataclass(frozen=True, slots=True)
class MarketEvaluationSnapshot:
    """
    클래스 이름: MarketEvaluationSnapshot
    기능: 한 원자적 평가 시점의 시장 값과 monotonic 경과 시간을 불변으로 보존한다.
    작성 날짜: 2026/08/14
    """

    realtime_price: Decimal = ZERO_DECIMAL
    lower_band: Decimal = ZERO_DECIMAL
    upper_band: Decimal = ZERO_DECIMAL
    realtime_pct_b: Decimal = ZERO_DECIMAL
    current_30m_candle_id: str | None = None
    current_30m_low: Decimal = ZERO_DECIMAL
    current_30m_high: Decimal = ZERO_DECIMAL
    touch_candle_bbw: Decimal = ZERO_DECIMAL
    confirmed_30m_close: bool = False
    confirmed_30m_close_time: datetime | None = None
    confirmed_1m_close: bool = False
    ema_slope_30m_close: Decimal = ZERO_DECIMAL
    realtime_ema_slope: Decimal = ZERO_DECIMAL
    current_close_ema_slope: Decimal = ZERO_DECIMAL
    tp_reference_ema_slope: Decimal = ZERO_DECIMAL
    cci_30m_realtime: Decimal = ZERO_DECIMAL
    pct_b_close: Decimal = ZERO_DECIMAL
    current_closed_candle_low: Decimal = ZERO_DECIMAL
    previous_3_closed_candle_lows: tuple[Decimal, ...] = ()
    holding_elapsed: timedelta = timedelta(0)
    signal_elapsed: timedelta = timedelta(0)
    case_c_timer_elapsed: timedelta = timedelta(0)
    pct_b_at_least_060_for_5s: bool = False
    realtime_slope_above_008_for_5s: bool = False
    realtime_slope_at_most_004_for_5s: bool = False
    pct_b_below_060_for_5s: bool = False
    realtime_slope_at_most_minus_055_for_3m: bool = False
    condition_timers: tuple[TradingTimerSnapshot, ...] = ()

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 시장 수치, 경과 시간 및 이전 candle 목록의 형식을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        # 원본 봉 마감 경계는 로컬 수신시각과 별도로 전달한다.
        if self.confirmed_30m_close_time is not None:
            if (
                not isinstance(self.confirmed_30m_close_time, datetime)
                or self.confirmed_30m_close_time.utcoffset() is None
                or not self.confirmed_30m_close
            ):
                raise ValueError("confirmed close time requires a closed candle and aware datetime")

        # 가격과 지표 계산에서 float 오차가 섞이지 않도록 Decimal만 허용한다.
        decimal_fields = (
            self.realtime_price,
            self.lower_band,
            self.upper_band,
            self.realtime_pct_b,
            self.current_30m_low,
            self.current_30m_high,
            self.touch_candle_bbw,
            self.ema_slope_30m_close,
            self.realtime_ema_slope,
            self.current_close_ema_slope,
            self.tp_reference_ema_slope,
            self.cci_30m_realtime,
            self.pct_b_close,
            self.current_closed_candle_low,
            *self.previous_3_closed_candle_lows,
        )
        if any(not isinstance(value, Decimal) for value in decimal_fields):
            raise TypeError("Market numeric values must use Decimal")

        # 시스템 시각 변경과 무관한 경과 시간은 음수가 될 수 없다.
        durations = (
            self.holding_elapsed,
            self.signal_elapsed,
            self.case_c_timer_elapsed,
        )
        if any(duration < timedelta(0) for duration in durations):
            raise ValueError("Monotonic durations cannot be negative")

        if len(self.previous_3_closed_candle_lows) not in (0, 3):
            raise ValueError("Previous candle lows must be empty or contain exactly 3 values")

        # 표시 정보가 mutable collection이나 중복 조건을 통해 다른 평가에 섞이지 않게 한다.
        if not isinstance(self.condition_timers, tuple) or any(
            not isinstance(timer, TradingTimerSnapshot) for timer in self.condition_timers
        ):
            raise TypeError("Condition timers must be an immutable timer tuple")
        if len({timer.condition_id for timer in self.condition_timers}) != len(self.condition_timers):
            raise ValueError("Condition timer IDs must be unique")  # 한 평가에는 조건별 회차 하나만 허용한다.


@dataclass(frozen=True, slots=True)
class TradingRuntimeSnapshot:
    """
    클래스 이름: TradingRuntimeSnapshot
    기능: TradingController가 소유하며 STM Guard가 읽는 전략 runtime 값을 보존한다.
    작성 날짜: 2026/08/14
    """

    position_owner: StrategyType | None = None
    pending_strategy: StrategyType | None = None
    pending_order_side: OrderSide | None = None
    pending_order_id: str | None = None
    pending_order_attempt_kind: OrderAttemptKind | None = None
    pending_intent_id: str | None = None
    trading_phase: TradingPhase = TradingPhase.IDLE

    lower_event_id: str | None = None
    touch_time: datetime | None = None
    touch_candle_id: str | None = None
    touch_candle_low: Decimal | None = None
    lower_band_at_touch: Decimal | None = None
    touch_candle_bbw: Decimal | None = None

    case_b_enabled: bool = False
    case_c_enabled: bool = False
    case_b_entry_paused: bool = False
    case_b_only_until_next_lower_touch: bool = False
    allow_new_case_c_setup: bool = False
    case_c_consumed_for_event: bool = False
    case_c_recovery_confirmed: bool = False

    signal_created: bool = False
    signal_candle_id: str | None = None
    signal_time: datetime | None = None

    last_case_c_setup_candle_id: str | None = None
    flush_low: Decimal | None = None
    flush_low_pct_b: Decimal | None = None
    flush_low_time: datetime | None = None
    current_open_pct_b: Decimal | None = None
    timer_base_pct_b: Decimal | None = None
    timer_base_time: datetime | None = None
    entry_pct_b: Decimal | None = None

    tp_price: Decimal | None = None
    previous_trail_ema_slope: Decimal | None = None

    pending_exit_reason: ExitReason | None = None
    pending_exit_pct_b: Decimal | None = None
    pending_return_state: PositionReturnState | None = None
    case_b_exit_reason: ExitReason | None = None
    case_c_exit_reason: ExitReason | None = None
    case_c_exit_pct_b: Decimal | None = None

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 주문·청산·시간 필드의 타입과 상호 의존 불변식을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        # 상태 비교에 사용하는 값은 문자열 대신 명시된 enum 타입만 허용한다.
        _require_optional_type("position_owner", self.position_owner, StrategyType)
        _require_optional_type("pending_strategy", self.pending_strategy, StrategyType)
        _require_optional_type("pending_order_side", self.pending_order_side, OrderSide)
        _require_optional_type(
            "pending_order_attempt_kind",
            self.pending_order_attempt_kind,
            OrderAttemptKind,
        )
        if not isinstance(self.trading_phase, TradingPhase):
            raise TypeError("trading_phase must be TradingPhase")
        _require_optional_type("pending_exit_reason", self.pending_exit_reason, ExitReason)
        _require_optional_type(
            "pending_return_state",
            self.pending_return_state,
            PositionReturnState,
        )
        _require_optional_type("case_b_exit_reason", self.case_b_exit_reason, ExitReason)
        _require_optional_type("case_c_exit_reason", self.case_c_exit_reason, ExitReason)

        # event-local 가격과 지표 snapshot에도 Decimal 정밀도를 강제한다.
        decimal_fields = (
            self.touch_candle_low,
            self.lower_band_at_touch,
            self.touch_candle_bbw,
            self.flush_low,
            self.flush_low_pct_b,
            self.current_open_pct_b,
            self.timer_base_pct_b,
            self.entry_pct_b,
            self.tp_price,
            self.previous_trail_ema_slope,
            self.pending_exit_pct_b,
            self.case_c_exit_pct_b,
        )
        if any(
            value is not None and not isinstance(value, Decimal)
            for value in decimal_fields
        ):
            raise TypeError("Runtime numeric values must use Decimal")

        # 주문 ID가 있으면 reconciliation에 필요한 전략·방향·시도 유형도 있어야 한다.
        pending_fields = (
            self.pending_strategy,
            self.pending_order_side,
            self.pending_order_attempt_kind,
        )
        if self.pending_order_id is not None and any(
            value is None for value in pending_fields
        ):
            raise ValueError("A pending order ID requires strategy, side, and attempt kind")
        if self.pending_exit_reason is None and self.pending_return_state is not None:
            raise ValueError("pending_return_state requires pending_exit_reason")
        if self.pending_exit_reason is None and self.pending_exit_pct_b is not None:
            raise ValueError("pending_exit_pct_b requires pending_exit_reason")

        # 저장되는 모든 runtime 시각은 timezone-aware 값이어야 한다.
        runtime_timestamps = (
            self.touch_time,
            self.signal_time,
            self.flush_low_time,
            self.timer_base_time,
        )
        for timestamp in runtime_timestamps:
            if timestamp is not None and (
                timestamp.tzinfo is None or timestamp.utcoffset() is None
            ):
                raise ValueError("Runtime timestamps must be timezone-aware")


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    """
    클래스 이름: PositionSnapshot
    기능: 청산 Guard에 필요한 실제 포지션 수량과 평균 진입가를 불변으로 보존한다.
    작성 날짜: 2026/08/14
    """

    quantity: Decimal = ZERO_DECIMAL
    entry_price: Decimal | None = None

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 포지션 수치가 Decimal이며 수량이 음수가 아닌지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        # 수량과 평균 진입가는 거래 계산 정밀도를 위해 Decimal만 허용한다.
        if not isinstance(self.quantity, Decimal):
            raise TypeError("Position quantity must use Decimal")
        if self.entry_price is not None and not isinstance(self.entry_price, Decimal):
            raise TypeError("Position entry_price must use Decimal")

        # NaN·Infinity와 음수는 authoritative 포지션 분기에 사용할 수 없다.
        if not self.quantity.is_finite():
            raise ValueError("Position quantity must be finite")
        if self.quantity < ZERO_DECIMAL:
            raise ValueError("Position quantity cannot be negative")
        if self.entry_price is not None:
            if not self.entry_price.is_finite():
                raise ValueError("Position entry_price must be finite")
            if self.entry_price < ZERO_DECIMAL:
                raise ValueError("Position entry_price cannot be negative")

    @property
    def is_open(self) -> bool:
        """
        함수 이름: is_open()
        기능: 실제 체결 후 남은 포지션 수량이 있는지 판정한다.
        인자: 없음
        반환값: 포지션 보유 여부
        작성 날짜: 2026/08/14
        """
        return self.quantity > ZERO_DECIMAL


@dataclass(frozen=True, slots=True)
class PendingOrderSnapshot:
    """
    클래스 이름: PendingOrderSnapshot
    기능: 중복 제출 방지와 reconciliation에 필요한 거래소 주문 정보를 보존한다.
    작성 날짜: 2026/08/14
    """

    order_id: str
    strategy: StrategyType
    side: OrderSide
    attempt_kind: OrderAttemptKind
    has_partial_fill: bool = False
    status_unknown: bool = False

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: pending 주문 식별자·enum·boolean 필드의 typed 계약을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 거래소 주문 식별자는 공백 보정 없이 non-empty canonical 문자열만 허용한다.
        if not isinstance(self.order_id, str):
            raise TypeError("Pending order_id must be a string")
        if not self.order_id or self.order_id != self.order_id.strip():
            raise ValueError("Pending order_id must be a non-empty trimmed string")

        # wire 문자열을 암묵적으로 enum으로 바꾸지 않고 canonical domain type만 받는다.
        if not isinstance(self.strategy, StrategyType):
            raise TypeError("Pending strategy must be a StrategyType")
        if not isinstance(self.side, OrderSide):
            raise TypeError("Pending side must be an OrderSide")
        if not isinstance(self.attempt_kind, OrderAttemptKind):
            raise TypeError("Pending attempt_kind must be an OrderAttemptKind")
        if not isinstance(self.has_partial_fill, bool):
            raise TypeError("has_partial_fill must be a bool")
        if not isinstance(self.status_unknown, bool):
            raise TypeError("status_unknown must be a bool")


@dataclass(frozen=True, slots=True)
class TradingContextView:
    """
    클래스 이름: TradingContextView
    기능: 한 STM 결정에서 함께 읽을 시장·runtime·포지션 snapshot과 version을 묶는다.
    작성 날짜: 2026/08/14
    """

    version: int
    evaluated_at: datetime
    market: MarketEvaluationSnapshot
    runtime: TradingRuntimeSnapshot
    position: PositionSnapshot = PositionSnapshot()
    pending_order: PendingOrderSnapshot | None = None
    initialized: bool = False
    selected_regime: RegimeType | None = None
    scale_in_ratio: Decimal = Decimal("0.5")
    scale_out_ratio: Decimal = Decimal("0.5")

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: Context version·시각과 pending 주문 snapshot의 일치 여부를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        # Context trace에 사용하는 version은 감소하거나 음수가 될 수 없다.
        if self.version < 0:
            raise ValueError("Context version cannot be negative")
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() is None:
            raise ValueError("evaluated_at must be timezone-aware")

        # 공개 read model도 mutable owner와 같은 선택·비율 타입 계약을 유지한다.
        if not isinstance(self.initialized, bool):
            raise TypeError("initialized must be a bool")
        if self.selected_regime is not None and not isinstance(
            self.selected_regime,
            RegimeType,
        ):
            raise TypeError("selected_regime must be a RegimeType or None")
        _validate_split_ratio(self.scale_in_ratio, "scale_in_ratio")
        _validate_split_ratio(self.scale_out_ratio, "scale_out_ratio")

        # runtime 예약 값과 거래소 주문 snapshot은 같은 주문을 가리켜야 한다.
        if self.pending_order is not None:
            runtime = self.runtime
            if runtime.pending_order_id != self.pending_order.order_id:
                raise ValueError("Pending order ID differs from runtime snapshot")
            if runtime.pending_strategy is not self.pending_order.strategy:
                raise ValueError("Pending strategy differs from runtime snapshot")
            if runtime.pending_order_side is not self.pending_order.side:
                raise ValueError("Pending side differs from runtime snapshot")
            if runtime.pending_order_attempt_kind is not self.pending_order.attempt_kind:
                raise ValueError("Pending attempt kind differs from runtime snapshot")


@dataclass(frozen=True, slots=True)
class _TradingContextCheckpoint:
    """
    클래스 이름: _TradingContextCheckpoint
    기능: Controller command 실패 시 mutable Context 전체를 원자 복원할 내부 상태를 보존한다.
    작성 날짜: 2026/08/21
    """

    owner_token: object
    account: Account | None
    initialized: bool
    selected_regime: RegimeType | None
    scale_in_ratio: Decimal
    scale_out_ratio: Decimal
    market: MarketEvaluationSnapshot
    runtime: TradingRuntimeSnapshot
    position: PositionSnapshot
    pending_order: PendingOrderSnapshot | None
    version: int


class ContextVersionConflictError(RuntimeError):
    """
    클래스 이름: ContextVersionConflictError
    기능: STM 결정 version과 최신 mutable Context version이 다를 때 발생한다.
    작성 날짜: 2026/08/21
    """


class TradingContextStateError(RuntimeError):
    """
    클래스 이름: TradingContextStateError
    기능: Context 초기화나 pending side 선행 조건이 충족되지 않을 때 발생한다.
    작성 날짜: 2026/08/21
    """


class TradingContext:
    """
    클래스 이름: TradingContext
    기능: 선택 REGIME·분할 비율·전략 runtime을 typed mutation과 version으로 보존한다.
    작성 날짜: 2026/08/21
    """

    __slots__ = (
        "_account",
        "_clock",
        "_checkpoint_owner",
        "_initialized",
        "_lock",
        "_market",
        "_pending_order",
        "_position",
        "_runtime",
        "_scale_in_ratio",
        "_scale_out_ratio",
        "_selected_regime",
        "_version",
    )

    def __init__(
        self,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 세션 전 설정을 받을 수 있는 비초기화 mutable Context를 생성한다.
        인자: clock -> snapshot 평가 시각을 제공할 timezone-aware clock
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")

        # 초기 state는 외부 참조 없이 읽을 수 있는 불변 snapshot들로 구성한다.
        self._clock = clock or _utc_now
        self._checkpoint_owner = object()
        self._lock = RLock()
        self._account: Account | None = None
        self._selected_regime: RegimeType | None = None
        self._scale_in_ratio = Decimal("0.5")
        self._scale_out_ratio = Decimal("0.5")
        self._market = MarketEvaluationSnapshot()
        self._runtime = TradingRuntimeSnapshot()
        self._position = PositionSnapshot()
        self._pending_order: PendingOrderSnapshot | None = None
        self._version = 0  # 첫 성공 mutation이 version 1을 만든다.
        self._initialized = False

    @property
    def version(self) -> int:
        """
        함수 이름: version()
        기능: 성공한 Context 변경에 따라 단조 증가하는 version을 반환한다.
        인자: 없음
        반환값: 현재 Context version
        작성 날짜: 2026/08/21
        """
        with self._lock:
            return self._version  # lock으로 보장된 최신 version을 노출한다.

    @property
    def initialized(self) -> bool:
        """
        함수 이름: initialized()
        기능: 세션 시작 Context가 initialize로 구성되었는지 반환한다.
        인자: 없음
        반환값: 초기화 완료 여부
        작성 날짜: 2026/08/21
        """
        with self._lock:
            return self._initialized  # session 경계 생성 여부를 읽는다.

    @property
    def account(self) -> Account | None:
        """
        함수 이름: account()
        기능: initialize에서 연결한 authoritative Account를 반환한다.
        인자: 없음
        반환값: 연결된 Account 또는 초기화 전 None
        작성 날짜: 2026/08/21
        """
        with self._lock:
            return self._account  # Account 자체의 mutation owner는 Account에 남겨 둔다.

    @property
    def selected_regime(self) -> RegimeType | None:
        """
        함수 이름: selected_regime()
        기능: 사용자가 선택한 canonical REGIME을 반환한다.
        인자: 없음
        반환값: 선택 REGIME 또는 미선택 None
        작성 날짜: 2026/08/21
        """
        with self._lock:
            return self._selected_regime  # 세션 전·중 선택을 같은 경계로 읽는다.

    @property
    def scale_in_ratio(self) -> Decimal:
        """
        함수 이름: scale_in_ratio()
        기능: BUY 주문 수량 계산에 사용할 분할 매수 비율을 반환한다.
        인자: 없음
        반환값: 0에서 1 사이 Decimal 비율
        작성 날짜: 2026/08/21
        """
        with self._lock:
            return self._scale_in_ratio  # Decimal 정밀도를 그대로 유지한다.

    @property
    def scale_out_ratio(self) -> Decimal:
        """
        함수 이름: scale_out_ratio()
        기능: SELL 주문 수량 계산에 사용할 분할 매도 비율을 반환한다.
        인자: 없음
        반환값: 0에서 1 사이 Decimal 비율
        작성 날짜: 2026/08/21
        """
        with self._lock:
            return self._scale_out_ratio  # Decimal 정밀도를 그대로 유지한다.

    @property
    def market(self) -> MarketEvaluationSnapshot:
        """
        함수 이름: market()
        기능: 다음 STM 평가에 사용할 최신 시장 snapshot을 반환한다.
        인자: 없음
        반환값: 최신 MarketEvaluationSnapshot
        작성 날짜: 2026/08/21
        """
        with self._lock:
            return self._market  # immutable snapshot 참조를 반환한다.

    @property
    def runtime(self) -> TradingRuntimeSnapshot:
        """
        함수 이름: runtime()
        기능: STM Guard가 읽는 최신 runtime snapshot을 반환한다.
        인자: 없음
        반환값: 최신 TradingRuntimeSnapshot
        작성 날짜: 2026/08/21
        """
        with self._lock:
            return self._runtime  # immutable runtime 참조를 반환한다.

    @property
    def position(self) -> PositionSnapshot:
        """
        함수 이름: position()
        기능: stop·safe-termination이 판정할 authoritative Position snapshot을 반환한다.
        인자: 없음
        반환값: 최신 PositionSnapshot
        작성 날짜: 2026/08/21
        """
        with self._lock:
            return self._position  # stop Guard의 authoritative 입력이다.

    @property
    def pending_order(self) -> PendingOrderSnapshot | None:
        """
        함수 이름: pending_order()
        기능: 중복 제출 방지와 reconciliation에 사용할 pending 주문을 반환한다.
        인자: 없음
        반환값: 최신 PendingOrderSnapshot 또는 없으면 None
        작성 날짜: 2026/08/21
        """
        with self._lock:
            return self._pending_order  # 없는 상태도 명시적 None으로 반환한다.

    def select_regime(self, regime_type: RegimeType) -> None:
        """
        함수 이름: select_regime()
        기능: Controller가 검증한 사용자 REGIME 선택을 Context에 적용한다.
        인자: regime_type -> 적용할 canonical RegimeType
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        if not isinstance(regime_type, RegimeType):
            raise TypeError("regime_type must be a RegimeType")

        # 동일 선택 재적용은 상태 변경이 아니므로 version을 소비하지 않는다.
        with self._lock:
            if self._selected_regime is regime_type:
                return
            self._selected_regime = regime_type  # RegimeController만 이 method를 호출한다.
            self._version += 1

    def set_split_ratios(
        self,
        scale_in_ratio: Decimal,
        scale_out_ratio: Decimal,
    ) -> None:
        """
        함수 이름: set_split_ratios()
        기능: 분할 매수·매도 비율을 하나의 versioned mutation으로 적용한다.
        인자: scale_in_ratio -> BUY에 사용할 0에서 1 사이 Decimal
            scale_out_ratio -> SELL에 사용할 0에서 1 사이 Decimal
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        _validate_split_ratio(scale_in_ratio, "scale_in_ratio")
        _validate_split_ratio(scale_out_ratio, "scale_out_ratio")

        # 두 비율은 UI가 중간 상태를 관찰하지 않도록 동일 lock에서 교체한다.
        with self._lock:
            if (
                self._scale_in_ratio == scale_in_ratio
                and self._scale_out_ratio == scale_out_ratio
            ):
                return
            self._scale_in_ratio = scale_in_ratio
            self._scale_out_ratio = scale_out_ratio
            self._version += 1  # 성공한 비율 command 한 번을 하나의 version으로 표시한다.

    def initialize(
        self,
        account: Account,
        selected_regime: RegimeType,
        position: PositionSnapshot,
        scale_in_ratio: Decimal,
        scale_out_ratio: Decimal,
    ) -> None:
        """
        함수 이름: initialize()
        기능: start 직전 계좌·REGIME·포지션·비율을 연결하고 runtime을 초기화한다.
        인자: account -> 준비 검증을 끝낸 Account
            selected_regime -> 세션에 고정할 canonical REGIME
            position -> 시작 시점 authoritative Position snapshot
            scale_in_ratio -> BUY 분할 비율
            scale_out_ratio -> SELL 분할 비율
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        if not isinstance(account, Account):
            raise TypeError("account must be an Account")
        if not isinstance(selected_regime, RegimeType):
            raise TypeError("selected_regime must be a RegimeType")
        if not isinstance(position, PositionSnapshot):
            raise TypeError("position must be a PositionSnapshot")
        _validate_split_ratio(scale_in_ratio, "scale_in_ratio")
        _validate_split_ratio(scale_out_ratio, "scale_out_ratio")

        # 새 session은 이전 lower event·주문 의도·전략 owner를 전혀 재사용하지 않는다.
        with self._lock:
            self._account = account
            self._selected_regime = selected_regime
            self._scale_in_ratio = scale_in_ratio
            self._scale_out_ratio = scale_out_ratio
            self._position = position
            self._runtime = TradingRuntimeSnapshot()
            self._pending_order = None
            self._initialized = True
            self._version += 1  # initialize는 세션 경계 생성을 표시하므로 항상 증가한다.

    def update_market(self, market: MarketEvaluationSnapshot) -> None:
        """
        함수 이름: update_market()
        기능: Controller가 원자적으로 만든 시장 평가 snapshot을 적용한다.
        인자: market -> 다음 STM 판정에 사용할 MarketEvaluationSnapshot
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        if not isinstance(market, MarketEvaluationSnapshot):
            raise TypeError("market must be a MarketEvaluationSnapshot")

        # 중복 market callback은 불필요한 stale-version 충돌을 만들지 않는다.
        with self._lock:
            if self._market == market:
                return
            self._market = market  # 불변 snapshot 참조만 교체한다.
            self._version += 1

    def update_position(
        self,
        position: PositionSnapshot,
        position_owner: StrategyType | None = None,
    ) -> None:
        """
        함수 이름: update_position()
        기능: 체결·reconciliation 결과의 authoritative 수량과 전략 owner를 함께 적용한다.
        인자: position -> 최신 PositionSnapshot
            position_owner -> 남은 포지션을 소유한 전략 또는 없으면 None
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        if not isinstance(position, PositionSnapshot):
            raise TypeError("position must be a PositionSnapshot")
        _require_optional_type("position_owner", position_owner, StrategyType)
        if not position.is_open and position_owner is not None:
            raise ValueError("A zero position cannot have a strategy owner")

        # 실제 수량과 logical owner를 같은 version으로 발행해 Guard 불일치를 방지한다.
        with self._lock:
            next_runtime = replace(self._runtime, position_owner=position_owner)
            if self._position == position and self._runtime == next_runtime:
                return
            self._position = position
            self._runtime = next_runtime
            self._version += 1  # Position과 owner 교체를 한 mutation으로 간주한다.

    def update_pending_order(
        self,
        pending_order: PendingOrderSnapshot | None,
        *,
        preserve_intent_id: bool = False,
    ) -> None:
        """
        함수 이름: update_pending_order()
        기능: 주문 실행·reconciliation 결과와 runtime pending 필드를 원자적으로 맞춘다.
        인자: pending_order -> 최신 pending 주문 또는 terminal 확정 후 None
            preserve_intent_id -> terminal 실패 뒤 같은 intent 재시도를 위해 ID를 남길지 여부
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # pending snapshot과 intent 보존 flag를 mutation 전에 정확한 타입으로 검증한다.
        if pending_order is not None and not isinstance(
            pending_order,
            PendingOrderSnapshot,
        ):
            raise TypeError("pending_order must be a PendingOrderSnapshot or None")
        if type(preserve_intent_id) is not bool:
            raise TypeError("preserve_intent_id must be a bool")

        # authoritative order snapshot과 Guard용 runtime 식별자를 하나의 lock에서 동기화한다.
        with self._lock:
            if pending_order is None:
                preserved_intent_id = (
                    self._runtime.pending_intent_id
                    if preserve_intent_id
                    else None
                )
                next_runtime = replace(
                    self._runtime,
                    pending_strategy=None,
                    pending_order_side=None,
                    pending_order_id=None,
                    pending_order_attempt_kind=None,
                    pending_intent_id=preserved_intent_id,
                )
            else:
                next_runtime = replace(
                    self._runtime,
                    pending_strategy=pending_order.strategy,
                    pending_order_side=pending_order.side,
                    pending_order_id=pending_order.order_id,
                    pending_order_attempt_kind=pending_order.attempt_kind,
                )
            if self._pending_order == pending_order and self._runtime == next_runtime:
                return
            self._pending_order = pending_order
            self._runtime = next_runtime
            self._version += 1  # 주문 snapshot과 runtime 패치는 항상 한 version을 공유한다.

    def apply_runtime_patch(self, action: PatchRuntimeContext) -> None:
        """
        함수 이름: apply_runtime_patch()
        기능: 허용 목록으로 제한된 PatchRuntimeContext를 하나의 mutation으로 적용한다.
        인자: action -> STM이 반환한 typed runtime patch
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        if not isinstance(action, PatchRuntimeContext):
            raise TypeError("action must be a PatchRuntimeContext")

        # 패치 전체를 검증한 뒤 한 번에 교체해 중간 runtime이 노출되지 않게 한다.
        with self._lock:
            self._apply_runtime_changes_unlocked(action.changes)  # 하나의 patch가 version 단위다.

    def apply_trading_stm_result(self, result: TradingSTMResult) -> None:
        """
        함수 이름: apply_trading_stm_result()
        기능: 동일 Context version에서 결정된 결과의 runtime patch만 적용한다.
        인자: result -> TradingSTM이 반환한 versioned TradingSTMResult
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        if not isinstance(result, TradingSTMResult):
            raise TypeError("result must be a TradingSTMResult")

        # stale 결정은 최신 주문·포지션 state를 덮어쓰기 전에 거부한다.
        with self._lock:
            if result.context_version != self._version:
                raise ContextVersionConflictError(
                    "TradingSTM result context version is stale"
                )

            # runtime patch만 결과 순서로 계산하고 lower·queue·schedule·주문은 실행하지 않는다.
            next_runtime = self._runtime
            for action in result.action_requests:
                if not isinstance(action, PatchRuntimeContext):
                    continue
                _validate_runtime_changes(action.changes)
                field_names = tuple(
                    change.field.value
                    for change in action.changes
                )
                if len(set(field_names)) != len(field_names):
                    raise ValueError("Runtime patch must not contain duplicate fields")
                next_runtime = replace(
                    next_runtime,
                    **{
                        change.field.value: change.value
                        for change in action.changes
                    },
                )
            _validate_pending_alignment(next_runtime, self._pending_order)
            self._replace_runtime_unlocked(next_runtime)

    def open_lower_event(self, action: OpenLowerEvent) -> None:
        """
        함수 이름: open_lower_event()
        기능: 새 lower-touch scope와 고정 candle 값을 typed action으로 연다.
        인자: action -> lower event ID와 touch snapshot을 담은 OpenLowerEvent
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        if not isinstance(action, OpenLowerEvent):
            raise TypeError("action must be an OpenLowerEvent")

        # event 범위와 touch 수치를 같은 runtime version에 고정한다.
        with self._lock:
            next_runtime = replace(
                self._runtime,
                lower_event_id=action.lower_event_id,
                touch_time=action.touch_time,
                touch_candle_id=action.candle_id,
                touch_candle_low=action.touch_candle_low,
                lower_band_at_touch=action.lower_band_at_touch,
                touch_candle_bbw=action.touch_candle_bbw,
            )
            self._replace_runtime_unlocked(next_runtime)  # typed snapshot 검증을 재사용한다.

    def close_lower_event(self, action: CloseLowerEvent) -> None:
        """
        함수 이름: close_lower_event()
        기능: 현재 lower-touch scope와 고정 touch candle 값을 정리한다.
        인자: action -> 종료 사유를 담은 CloseLowerEvent
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        if not isinstance(action, CloseLowerEvent):
            raise TypeError("action must be a CloseLowerEvent")
        if not action.reason:
            raise ValueError("CloseLowerEvent reason must not be empty")

        # Case별 초기화는 별도 action으로 남기고 lower event 소유 값만 제거한다.
        with self._lock:
            next_runtime = replace(
                self._runtime,
                lower_event_id=None,
                touch_time=None,
                touch_candle_id=None,
                touch_candle_low=None,
                lower_band_at_touch=None,
                touch_candle_bbw=None,
            )
            self._replace_runtime_unlocked(next_runtime)  # 중복 close는 version을 증가시키지 않는다.

    def reset_case_b_context(self, action: ResetCaseBContext) -> None:
        """
        함수 이름: reset_case_b_context()
        기능: Case B signal·pause·exit runtime을 action 정책에 따라 초기화한다.
        인자: action -> signal 보존 여부를 포함한 ResetCaseBContext
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        if not isinstance(action, ResetCaseBContext):
            raise TypeError("action must be a ResetCaseBContext")

        # preserve_signal은 signal 식별자만 남기고 기타 Case B 제어 값은 정리한다.
        with self._lock:
            signal_changes = (
                {}
                if action.preserve_signal
                else {
                    "signal_created": False,
                    "signal_candle_id": None,
                    "signal_time": None,
                }
            )
            next_runtime = replace(
                self._runtime,
                case_b_enabled=False,
                case_b_entry_paused=False,
                case_b_only_until_next_lower_touch=False,
                case_b_exit_reason=None,
                **signal_changes,
            )
            self._replace_runtime_unlocked(next_runtime)  # reset 실제 변경이 있을 때만 version을 올린다.

    def reset_case_c_context(self, action: ResetCaseCContext) -> None:
        """
        함수 이름: reset_case_c_context()
        기능: Case C setup·timer·trailing runtime을 action 정책에 따라 초기화한다.
        인자: action -> exit 결과 보존 여부를 포함한 ResetCaseCContext
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        if not isinstance(action, ResetCaseCContext):
            raise TypeError("action must be a ResetCaseCContext")

        # preserve_exit_result인 경우만 Case C 종료 사유와 판정 %B를 유지한다.
        with self._lock:
            exit_changes = (
                {}
                if action.preserve_exit_result
                else {
                    "case_c_exit_reason": None,
                    "case_c_exit_pct_b": None,
                }
            )
            next_runtime = replace(
                self._runtime,
                case_c_enabled=False,
                allow_new_case_c_setup=False,
                case_c_consumed_for_event=False,
                case_c_recovery_confirmed=False,
                last_case_c_setup_candle_id=(
                    self._runtime.last_case_c_setup_candle_id
                    if action.preserve_setup_candle else None
                ),
                flush_low=None,
                flush_low_pct_b=None,
                flush_low_time=None,
                current_open_pct_b=None,
                timer_base_pct_b=None,
                timer_base_time=None,
                entry_pct_b=None,
                tp_price=None,
                previous_trail_ema_slope=None,
                **exit_changes,
            )
            self._replace_runtime_unlocked(next_runtime)  # Case C 초기화도 하나의 mutation이다.

    def get_split_ratio(self) -> Decimal:
        """
        함수 이름: get_split_ratio()
        기능: 현재 pending 주문 방향에 맞는 분할 매수·매도 비율을 반환한다.
        인자: 없음
        반환값: BUY면 scale-in, SELL이면 scale-out Decimal 비율
        작성 날짜: 2026/08/21
        """
        with self._lock:
            pending_side = self._runtime.pending_order_side
            if pending_side is OrderSide.BUY:
                return self._scale_in_ratio  # BUY 의도에 scale-in 비율을 적용한다.
            if pending_side is OrderSide.SELL:
                return self._scale_out_ratio  # SELL 의도에 scale-out 비율을 적용한다.

        # 주문 방향 없이 기본 비율을 추측하는 동작은 금지한다.
        raise TradingContextStateError("A pending order side is required")

    def snapshot(self) -> TradingContextView:
        """
        함수 이름: snapshot()
        기능: 한 lock 범위의 market·runtime·position·pending·version을 불변 view로 만든다.
        인자: 없음
        반환값: TradingSTM에 전달할 TradingContextView
        작성 날짜: 2026/08/21
        """
        # 시각까지 lock 안에서 읽어 version과 동일한 평가 시점으로 묶는다.
        with self._lock:
            evaluated_at = self._clock()
            _validate_evaluation_time(evaluated_at)
            return TradingContextView(
                version=self._version,
                evaluated_at=evaluated_at,
                market=self._market,
                runtime=self._runtime,
                position=self._position,
                pending_order=self._pending_order,
                initialized=self._initialized,
                selected_regime=self._selected_regime,
                scale_in_ratio=self._scale_in_ratio,
                scale_out_ratio=self._scale_out_ratio,
            )

    def _create_checkpoint(self) -> _TradingContextCheckpoint:
        """
        함수 이름: _create_checkpoint()
        기능: Controller가 아직 publish하지 않은 command를 rollback할 전체 Context 상태를 만든다.
        인자: 없음
        반환값: 외부에 공개하지 않는 불변 Context checkpoint
        작성 날짜: 2026/08/21
        """
        # account 참조와 모든 불변 snapshot·설정을 같은 Context lock에서 함께 읽는다.
        with self._lock:
            return _TradingContextCheckpoint(
                owner_token=self._checkpoint_owner,
                account=self._account,
                initialized=self._initialized,
                selected_regime=self._selected_regime,
                scale_in_ratio=self._scale_in_ratio,
                scale_out_ratio=self._scale_out_ratio,
                market=self._market,
                runtime=self._runtime,
                position=self._position,
                pending_order=self._pending_order,
                version=self._version,
            )

    def _restore_checkpoint(self, checkpoint: _TradingContextCheckpoint) -> None:
        """
        함수 이름: _restore_checkpoint()
        기능: 실패한 Controller command가 publish하기 전 Context 전체를 이전 상태로 복원한다.
        인자: checkpoint -> 같은 Context가 command 전에 만든 내부 checkpoint
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 외부 위조나 다른 Context 인스턴스에서 만든 private checkpoint를 거부한다.
        if not isinstance(checkpoint, _TradingContextCheckpoint):
            raise TypeError("checkpoint must be a TradingContext checkpoint")
        if checkpoint.owner_token is not self._checkpoint_owner:
            raise ValueError("checkpoint belongs to a different TradingContext")

        # 새 version을 만들지 않고 실패 command 이전의 모든 필드를 한 번에 되돌린다.
        with self._lock:
            self._account = checkpoint.account
            self._initialized = checkpoint.initialized
            self._selected_regime = checkpoint.selected_regime
            self._scale_in_ratio = checkpoint.scale_in_ratio
            self._scale_out_ratio = checkpoint.scale_out_ratio
            self._market = checkpoint.market
            self._runtime = checkpoint.runtime
            self._position = checkpoint.position
            self._pending_order = checkpoint.pending_order
            self._version = checkpoint.version

    def _apply_runtime_changes_unlocked(
        self,
        changes: tuple[RuntimeFieldChange, ...],
    ) -> None:
        """
        함수 이름: _apply_runtime_changes_unlocked()
        기능: lock 보유 상태에서 typed 필드 변경을 검증하고 runtime을 교체한다.
        인자: changes -> 적용 순서가 보존된 RuntimeFieldChange tuple
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        _validate_runtime_changes(changes)  # state 계산 전에 patch container 전체를 검증한다.

        # 중복 필드는 패치 의도를 모호하게 하므로 적용 전에 거부한다.
        field_names = tuple(change.field.value for change in changes)
        if len(set(field_names)) != len(field_names):
            raise ValueError("Runtime patch must not contain duplicate fields")
        next_runtime = replace(
            self._runtime,
            **{
                change.field.value: change.value
                for change in changes
            },
        )
        _validate_pending_alignment(next_runtime, self._pending_order)
        self._replace_runtime_unlocked(next_runtime)  # validation 완료 후에만 state를 교체한다.

    def _replace_runtime_unlocked(
        self,
        next_runtime: TradingRuntimeSnapshot,
    ) -> None:
        """
        함수 이름: _replace_runtime_unlocked()
        기능: 변경이 있는 유효한 runtime snapshot만 적용하고 version을 증가시킨다.
        인자: next_runtime -> 교체할 검증된 TradingRuntimeSnapshot
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        if not isinstance(next_runtime, TradingRuntimeSnapshot):
            raise TypeError("next_runtime must be a TradingRuntimeSnapshot")
        if next_runtime == self._runtime:
            return

        # 실제 변경이 있는 immutable runtime만 최신 state로 발행한다.
        self._runtime = next_runtime
        self._version += 1  # 모든 runtime 교체는 stale 결정을 가르는 version을 생성한다.


def _utc_now() -> datetime:
    """
    함수 이름: _utc_now()
    기능: TradingContext snapshot에 사용할 timezone-aware UTC 시각을 반환한다.
    인자: 없음
    반환값: 현재 UTC datetime
    작성 날짜: 2026/08/21
    """
    return datetime.now(UTC)  # 시스템 local timezone에 의존하지 않는다.


def _validate_split_ratio(value: object, field_name: str) -> None:
    """
    함수 이름: _validate_split_ratio()
    기능: 분할 주문 비율이 유한한 0에서 1 사이 Decimal인지 검증한다.
    인자: value -> 검증할 비율
        field_name -> 오류 메시지에 사용할 필드 이름
    반환값: 없음
    작성 날짜: 2026/08/21
    """
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be a Decimal")

    # NaN·Infinity, signed zero와 범위 밖 비율은 주문 수량 의도로 저장하지 않는다.
    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite")
    if value.is_signed() or value > Decimal("1"):
        raise ValueError(f"{field_name} must be between 0 and 1")


def _validate_evaluation_time(value: object) -> None:
    """
    함수 이름: _validate_evaluation_time()
    기능: Context clock이 timezone-aware datetime을 반환했는지 검증한다.
    인자: value -> clock이 반환한 평가 시각
    반환값: 없음
    작성 날짜: 2026/08/21
    """
    if not isinstance(value, datetime):
        raise TypeError("TradingContext clock must return a datetime")

    # 절대 시각을 잃은 naive datetime은 trace·replay 자료로 사용하지 않는다.
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("TradingContext clock must return a timezone-aware datetime")


def _validate_runtime_changes(changes: object) -> None:
    """
    함수 이름: _validate_runtime_changes()
    기능: runtime patch가 순서가 보존된 typed RuntimeFieldChange tuple인지 검증한다.
    인자: changes -> PatchRuntimeContext에 담긴 변경 목록
    반환값: 없음
    작성 날짜: 2026/08/21
    """
    if not isinstance(changes, tuple):
        raise TypeError("Runtime patch changes must be a tuple")

    # arbitrary object가 필드 접근 중 예외를 내기 전에 typed action 경계에서 거부한다.
    if any(not isinstance(change, RuntimeFieldChange) for change in changes):
        raise TypeError("changes must contain RuntimeFieldChange values")
    if any(not isinstance(change.field, RuntimeField) for change in changes):
        raise TypeError("Runtime changes must use RuntimeField values")


def _validate_pending_alignment(
    runtime: TradingRuntimeSnapshot,
    pending_order: PendingOrderSnapshot | None,
) -> None:
    """
    함수 이름: _validate_pending_alignment()
    기능: authoritative pending snapshot과 runtime Guard 필드가 같은 주문을 가리키는지 검증한다.
    인자: runtime -> 적용할 TradingRuntimeSnapshot
        pending_order -> 현재 authoritative PendingOrderSnapshot 또는 None
    반환값: 없음
    작성 날짜: 2026/08/21
    """
    if pending_order is None:
        return

    # pending Entity가 있는 동안 runtime 패치만으로 식별자를 바꾸거나 지우지 못하게 한다.
    if runtime.pending_order_id != pending_order.order_id:
        raise ValueError("Pending order ID differs from runtime snapshot")
    if runtime.pending_strategy is not pending_order.strategy:
        raise ValueError("Pending strategy differs from runtime snapshot")
    if runtime.pending_order_side is not pending_order.side:
        raise ValueError("Pending side differs from runtime snapshot")
    if runtime.pending_order_attempt_kind is not pending_order.attempt_kind:
        raise ValueError("Pending attempt kind differs from runtime snapshot")


def _require_optional_type(
    field_name: str,
    value: object,
    expected_type: type[object],
) -> None:
    """
    함수 이름: _require_optional_type()
    기능: 선택 값이 None이거나 지정된 domain 타입인지 검증한다.
    인자: field_name -> 오류 메시지에 사용할 필드 이름
        value -> 검증할 선택 값
        expected_type -> 허용할 타입
    반환값: 없음
    작성 날짜: 2026/08/14
    """
    if value is not None and not isinstance(value, expected_type):
        raise TypeError(f"{field_name} must be {expected_type.__name__}")
