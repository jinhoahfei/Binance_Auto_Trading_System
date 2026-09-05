"""시장 평가기의 monotonic 유지 구간을 지표 타이머 결과로 변환한다."""

from datetime import datetime
from decimal import Decimal

from ..domain.trading.timers import TradingTimerSnapshot


# 기존 Guard flag의 이름만 연결하며 임계값과 지속시간은 평가기의 실제 계약에서 받는다.
HOLD_CONDITION_IDS = {
    "pct_b_at_least_060_for_5s": "b_profit_zone",
    "realtime_slope_above_008_for_5s": "b_trend_slope",
    "realtime_slope_at_most_004_for_5s": "b_trend_exit_slope",
    "pct_b_below_060_for_5s": "b_trend_exit_pct_b",
    "realtime_slope_at_most_minus_055_for_3m": "c_stop",
}


def create_hold_timer_snapshots(
    contracts: dict[str, tuple[bool, int]],
    flags: dict[str, bool],
    starts: dict[str, int | None],
    previous: tuple[TradingTimerSnapshot, ...],
    *,
    monotonic_time: int,
    sampled_at: datetime,
    candle_id: str,
    generation: int,
    candle_changed: bool,
) -> tuple[TradingTimerSnapshot, ...]:
    """
    함수 이름: create_hold_timer_snapshots()
    기능: Guard와 동일한 시작점·flag에서 연속 유지의 남은 시간과 중단 상태를 만든다.
    인자: contracts -> 평가기가 사용한 즉시 조건과 nanosecond 지속시간
        flags -> 같은 평가의 완료 판정, starts -> 다음 연속 유지 시작점
        previous -> 직전 30분 tick의 표시 결과
        monotonic_time -> 이번 30분 tick의 단조 시각, sampled_at -> 같은 tick의 서버 시각
        candle_id -> 현재 봉, generation -> 스트림 초기화 회차, candle_changed -> 봉 변경 여부
    반환값: 다섯 유지 조건의 불변 타이머 tuple
    작성 날짜: 2026/09/05
    """
    previous_by_id = {timer.condition_id: timer for timer in previous}
    timers = []

    # 가격 조건을 다시 비교하지 않고 이미 선택된 flag와 시작점으로만 시간을 계산한다.
    for flag_name, (_, duration_ns) in contracts.items():
        condition_id = HOLD_CONDITION_IDS[flag_name]
        duration = Decimal(duration_ns) / Decimal("1000000000")
        started_at = starts[flag_name]
        old_timer = previous_by_id.get(condition_id)
        reason = "candle_changed" if candle_changed else ("stream_reset" if generation > 0 and not previous else None)
        timer_id = f"hold:{generation}:{candle_id}:{condition_id}:{started_at}"
        remaining = duration
        if started_at is None:
            interrupted = not candle_changed and old_timer is not None and old_timer.state != "waiting"
            state = "stopped" if interrupted else "waiting"
            if interrupted:
                timer_id = old_timer.timer_id
                reason = "condition_broken"
        else:
            elapsed = Decimal(monotonic_time - started_at) / Decimal("1000000000")
            remaining = max(Decimal("0"), duration - elapsed)
            state = "completed" if flags[flag_name] else "running"
            if old_timer is not None and old_timer.timer_id == timer_id:
                reason = old_timer.reset_reason  # 동일 회차의 재전송에서 리셋 사유가 깜빡이지 않게 한다.
        timers.append(TradingTimerSnapshot(
            condition_id, timer_id, "hold", state, duration, remaining, sampled_at, reason,
        ))
    return tuple(timers)
