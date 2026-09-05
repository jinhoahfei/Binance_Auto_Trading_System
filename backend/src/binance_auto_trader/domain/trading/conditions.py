"""전략 전이와 실시간 표시가 함께 사용하는 부수 효과 없는 조건 평가를 정의한다."""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from operator import ge, gt, le, lt

from .context import TradingContextView


@dataclass(frozen=True, slots=True)
class TradingCondition:
    """
    클래스 이름: TradingCondition
    기능: 비교 당시의 수치·기준·유지시간 판정을 불변 값으로 보존한다.
    작성 날짜: 2026/09/05
    """

    condition_id: str
    value: Decimal | None
    threshold: Decimal | None
    comparison: str
    satisfied: bool | None
    source: str = "realtime"
    hold_seconds: int | None = None


# 조건 ID마다 실제 Guard가 읽는 입력과 비교 연산을 한 곳에서 정의한다.
# 유지시간은 시장 평가기가 계산한 연속 유지 결과를 그대로 사용한다.
_DEFINITIONS = {
    "lower_price": ("market.realtime_price", "<=", "market.lower_band", "realtime", None, None),
    "lower_close": ("market.current_30m_low", "<=", "market.lower_band", "close_30m", None, None),
    "upper_safe_exit": ("market.realtime_price", ">=", "market.upper_band", "realtime", None, None),
    "b_touch_low": ("runtime.touch_candle_low", "<=", "runtime.lower_band_at_touch", "touch", None, None),
    "b_touch_bbw": ("runtime.touch_candle_bbw", "<", "0.02", "touch", None, None),
    "b_signal_slope": ("market.ema_slope_30m_close", ">", "-0.03", "close_30m", None, None),
    "b_signal_pct_b": ("market.pct_b_close", ">", "0.25", "close_30m", None, None),
    "b_signal_low": ("market.current_closed_candle_low", ">=", "previous_low", "close_30m", None, None),
    "b_pullback": ("market.realtime_pct_b", "<=", "0.30", "realtime", None, None),
    "b_signal_age": ("market.signal_elapsed", "<=", "10800", "elapsed", None, None),
    "b_profit_zone": ("market.realtime_pct_b", ">=", "0.60", "realtime", "pct_b_at_least_060_for_5s", 5),
    "b_take_profit_slope": ("market.realtime_ema_slope", "<=", "0.08", "realtime", None, None),
    "b_trend_slope": ("market.realtime_ema_slope", ">", "0.08", "realtime", "realtime_slope_above_008_for_5s", 5),
    "b_stop": ("market.ema_slope_30m_close", "<", "-0.08", "close_30m", None, None),
    "b_emergency_stop": ("market.realtime_price", "<=", "emergency_price", "realtime", None, None),
    "b_time_exit": ("market.holding_elapsed", ">=", "21600", "elapsed", None, None),
    "b_trend_exit_slope": ("market.realtime_ema_slope", "<=", "0.04", "realtime", "realtime_slope_at_most_004_for_5s", 5),
    "b_trend_exit_pct_b": ("market.realtime_pct_b", "<", "0.60", "realtime", "pct_b_below_060_for_5s", 5),
    "c_setup_pct_b": ("market.realtime_pct_b", "<=", "-0.15", "realtime", None, None),
    "c_setup_cci": ("market.cci_30m_realtime", "<=", "-140", "realtime", None, None),
    "c_flush": ("market.realtime_pct_b", "<=", "-0.25", "realtime", None, None),
    "c_new_low": ("market.realtime_price", "<", "runtime.flush_low", "realtime", None, None),
    "c_rebound": ("market.realtime_pct_b", ">=", "runtime.entry_pct_b", "realtime", None, None),
    "c_recovery_window": ("market.case_c_timer_elapsed", "<=", "180", "elapsed", None, None),
    "c_entry_limit": ("runtime.entry_pct_b", "<", "-0.15", "runtime", None, None),
    "c_recovery": ("market.realtime_pct_b", ">=", "0.25", "realtime", None, None),
    "c_profit_zone": ("market.realtime_pct_b", ">=", "0.10", "realtime", None, None),
    "c_stop": ("market.realtime_ema_slope", "<=", "-0.55", "realtime", "realtime_slope_at_most_minus_055_for_3m", 180),
    "c_time_exit": ("market.holding_elapsed", ">=", "3600", "elapsed", None, None),
    "c_trail_fallback": ("market.realtime_pct_b", "<", "0.10", "realtime", None, None),
    "c_trail_increase": ("market.current_close_ema_slope", ">", "runtime.previous_trail_ema_slope", "close_1m", None, None),
    "c_handoff": ("runtime.case_c_exit_pct_b", "<", "0.40", "runtime", None, None),
}
_COMPARISONS = {"<": lt, "<=": le, ">": gt, ">=": ge}


def _read_operand(operand: str, context: TradingContextView) -> Decimal | None:
    """
    함수 이름: _read_operand()
    기능: 고정 임계값 또는 불변 Context의 수치 입력을 Decimal로 읽는다.
    인자: operand -> 정의에 명시한 필드 경로 또는 상수
        context -> 실제 전략 판단에 사용한 Context
    반환값: 수치 또는 입력이 없는 경우 None
    작성 날짜: 2026/09/05
    """
    # 계산 기준도 Context의 동일 시점 값으로 만들며 누락 값을 영점으로 바꾸지 않는다.
    if operand == "previous_low":
        lows = context.market.previous_3_closed_candle_lows
        return min(lows) if len(lows) == 3 and all(low.is_finite() for low in lows) else None
    if operand == "emergency_price":
        entry_price = context.position.entry_price
        return entry_price * Decimal("0.99") if entry_price is not None else None
    if operand.startswith(("market.", "runtime.")):
        owner, field = operand.split(".")
        value = getattr(getattr(context, owner), field)
        if isinstance(value, timedelta):
            return Decimal(value // timedelta(microseconds=1)) / Decimal("1000000")
        return value if value is not None and value.is_finite() else None  # 판정 불가도 미수신과 구분 없이 회색으로 보낸다.
    return Decimal(operand)


def evaluate_condition(condition_id: str, context: TradingContextView) -> TradingCondition:
    """
    함수 이름: evaluate_condition()
    기능: 실제 전이와 표시 양쪽에서 동일한 수치 비교와 연속 유지 판정을 계산한다.
    인자: condition_id -> 조건 정의 식별자
        context -> 하나의 microstep에서 읽은 불변 Context
    반환값: 값과 비교 당시 기준을 포함하는 불변 조건 결과
    작성 날짜: 2026/09/05
    """
    # Decimal 비교는 순수하게 수행하고 시계·주문·runtime에는 접근하거나 쓰지 않는다.
    operand, comparison, reference, source, hold_flag, hold_seconds = _DEFINITIONS[condition_id]
    value = _read_operand(operand, context)
    threshold = _read_operand(reference, context)
    satisfied = None
    if value is not None and threshold is not None:
        satisfied = _COMPARISONS[comparison](value, threshold)
        if hold_flag is not None:
            satisfied = getattr(context.market, hold_flag)  # 기존 5초·3분 판정을 재사용한다.
            if condition_id == "b_trend_slope":
                satisfied = satisfied and value > threshold  # 기존 Guard의 추가 즉시 비교를 유지한다.
    return TradingCondition(condition_id, value, threshold, comparison, satisfied, source, hold_seconds)


def condition_met(condition_id: str, context: TradingContextView) -> bool:
    """
    함수 이름: condition_met()
    기능: 전이 Guard가 공유 조건 결과의 충족 여부를 읽도록 한다.
    인자: condition_id -> 평가할 조건 ID
        context -> 전이 판단 Context
    반환값: 조건이 충족되었을 때만 True
    작성 날짜: 2026/09/05
    """
    return evaluate_condition(condition_id, context).satisfied is True  # 미수신은 진입을 허용하지 않는다.


def unevaluated_condition(condition_id: str) -> TradingCondition:
    """
    함수 이름: unevaluated_condition()
    기능: 첫 평가 전 고정 기준만 공개하고 동적 기준·현재 값·판정은 비운다.
    인자: condition_id -> 아직 평가하지 않은 조건 ID
    반환값: 명시적인 미수신 조건 결과
    작성 날짜: 2026/09/05
    """
    # 시장과 runtime 없이 확정 가능한 수치 임계값만 표시한다.
    _, comparison, reference, source, _, hold_seconds = _DEFINITIONS[condition_id]
    threshold = Decimal(reference) if reference[0] in "-0123456789" else None
    return TradingCondition(condition_id, None, threshold, comparison, None, source, hold_seconds)


def condition_unmet(condition_id: str, context: TradingContextView) -> bool:
    """
    함수 이름: condition_unmet()
    기능: 반대쪽 전이도 판정 불가를 충족으로 오해하지 않고 확정적인 미충족만 읽는다.
    인자: condition_id -> 평가할 조건 ID
        context -> 전이 판단 Context
    반환값: 조건이 유효하게 미충족으로 판정된 경우만 True
    작성 날짜: 2026/09/05
    """
    return evaluate_condition(condition_id, context).satisfied is False  # null은 반대 조건의 충족도 아니다.
