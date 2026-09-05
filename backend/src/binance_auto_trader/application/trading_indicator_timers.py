"""신호·회복·포지션 타이머와 시장 유지시간의 표시용 수명을 관리한다."""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

from ..domain.trading.conditions import evaluate_condition, unevaluated_condition
from ..domain.trading.context import TradingContextView
from ..domain.trading.timers import TradingTimerSnapshot


def seconds_between(sampled_at: datetime, started_at: datetime) -> Decimal:
    """
    함수 이름: seconds_between()
    기능: 두 서버 시각 사이의 음수가 아닌 경과 시간을 정확한 Decimal 초로 만든다.
    인자: sampled_at -> 측정 시각, started_at -> 실제 runtime 시작 시각
    반환값: 마이크로초 정밀도를 유지하는 경과 시간
    작성 날짜: 2026/09/05
    """
    microseconds = max(0, (sampled_at - started_at) // timedelta(microseconds=1))
    return Decimal(microseconds) / Decimal("1000000")  # float total_seconds의 오차를 피한다.


class TradingIndicatorTimers:
    """
    클래스 이름: TradingIndicatorTimers
    기능: 성공한 STM 처리에서 실제 타이머 회차·리셋 사유·남은 시간을 보존한다.
    작성 날짜: 2026/09/05
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 세션별 타이머 결과와 시작 기준을 저장할 빈 공간을 만든다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/05
        """
        self._timers: dict[str, TradingTimerSnapshot] = {}
        self._origins: dict[str, object] = {}
        self._sequence = 0  # 같은 서버 시각에 기준이 바뀌어도 회차를 구별한다.

    def observe(
        self,
        before: TradingContextView,
        after: TradingContextView,
        entered_at: datetime | None,
        market_origins: dict[str, object],
    ) -> None:
        """
        함수 이름: observe()
        기능: 시장의 실제 유지 결과와 action 적용 후 runtime의 새 타이머를 함께 보존한다.
        인자: before -> 실제 Guard 입력, after -> action 적용 후 상태
            entered_at -> authoritative Position 진입 시각
            market_origins -> 현재 시장 평가가 경과 시간을 계산할 때 사용한 시작점
        반환값: 없음
        작성 날짜: 2026/09/05
        """
        # 시장 유지시간은 상태 진입 시 다시 시작하지 않고 원본 측정 결과를 그대로 사용한다.
        self._timers = {key: timer for key, timer in self._timers.items() if timer.kind != "hold"}
        self._timers.update({timer.condition_id: timer for timer in after.market.condition_timers})
        runtime = after.runtime
        origins = {
            "b_signal_age": runtime.signal_time if runtime.signal_created else None,
            "c_recovery_window": runtime.timer_base_time if runtime.flush_low is not None else None,
            "b_time_exit": entered_at if after.position.is_open else None,
            "c_time_exit": entered_at if after.position.is_open else None,
        }

        # 회복 기준이 바뀐 처리 단위에서 이전 180초 초과 결과를 새 회차에 이월하지 않는다.
        for condition_id, started_at in origins.items():
            if started_at is None and (condition_id != "c_recovery_window" or runtime.flush_low is not None):
                self._timers.pop(condition_id, None)
                self._origins.pop(condition_id, None)
                continue  # 실제 시작 시각이 누락된 신호·Position은 가짜 전체 시간으로 표시하지 않는다.
            duration = unevaluated_condition(condition_id).threshold
            if duration is None:
                raise RuntimeError("Runtime timer requires a fixed strategy duration")
            origin = (started_at, runtime.timer_base_pct_b, runtime.flush_low) if condition_id == "c_recovery_window" else started_at
            changed = condition_id not in self._origins or self._origins[condition_id] != origin
            previous = self._timers.get(condition_id)
            if changed:
                self._sequence += 1
            timer_id = f"runtime:{condition_id}:{self._sequence}:{started_at.isoformat() if started_at else 'waiting'}" if changed else previous.timer_id
            kind = "time_exit" if condition_id.endswith("time_exit") else "window"
            sampled_at = after.evaluated_at
            remaining = duration
            state = "waiting"
            reason = previous.reset_reason if previous is not None and not changed else None
            if started_at is not None:
                # 새 구간은 180초 전체와 실제 기준 시각을 함께 보내 첫 publication부터 리셋을 보인다.
                sampled_at = started_at if changed else after.evaluated_at
                remaining = max(Decimal("0"), duration - seconds_between(sampled_at, started_at))
                state = "running"
                if changed and condition_id == "c_recovery_window" and before.runtime.timer_base_time is not None:
                    reason = "new_low" if before.runtime.flush_low != runtime.flush_low else "timeout"
                if not changed and market_origins.get(condition_id) == origin:
                    result = evaluate_condition(condition_id, before)
                    if kind == "time_exit" and result.satisfied is True:
                        state, remaining = "completed", Decimal("0")
                    elif kind == "window" and result.satisfied is False:
                        state, remaining = "expired", Decimal("0")
            self._origins[condition_id] = origin
            self._timers[condition_id] = TradingTimerSnapshot(
                condition_id, timer_id, kind, state, duration, remaining, sampled_at, reason,
            )

    def get(self, condition_id: str) -> TradingTimerSnapshot | None:
        """
        함수 이름: get()
        기능: 현재 행의 타이머를 읽고 저점 확인 전에는 회복 타이머의 시작 대기를 연결한다.
        인자: condition_id -> 현재 단계의 지표 ID
        반환값: 실제 측정 타이머 또는 미수신 None
        작성 날짜: 2026/09/05
        """
        timer = self._timers.get("c_recovery_window" if condition_id == "c_flush" else condition_id)
        return replace(timer, condition_id=condition_id) if timer is not None else None  # 읽기로 회차나 시간을 전진시키지 않는다.
