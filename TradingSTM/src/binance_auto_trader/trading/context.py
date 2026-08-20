"""TradingSTM이 한 평가 주기에서 사용하는 읽기 전용 Context snapshot을 정의한다."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

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

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 시장 수치, 경과 시간 및 이전 candle 목록의 형식을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
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

        # 거래소 오류가 아니면 포지션 수량은 음수가 될 수 없다.
        if self.quantity < ZERO_DECIMAL:
            raise ValueError("Position quantity cannot be negative")

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
