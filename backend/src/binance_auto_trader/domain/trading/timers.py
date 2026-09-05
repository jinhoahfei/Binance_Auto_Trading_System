"""전략 타이머의 실제 측정 결과를 전송 계층과 독립적인 불변 값으로 정의한다."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class TradingTimerSnapshot:
    """
    클래스 이름: TradingTimerSnapshot
    기능: 한 타이머 회차의 남은 시간과 실제 계산 상태를 불변으로 보존한다.
    작성 날짜: 2026/09/05
    """

    condition_id: str
    timer_id: str
    kind: str
    state: str
    duration_seconds: Decimal
    remaining_seconds: Decimal
    sampled_at: datetime
    reset_reason: str | None = None

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 시간 정밀도·상태·회차·측정 시각의 모순을 생성 경계에서 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/05
        """
        # 표시용 시간도 음수·비유한 수치나 float를 받아 정상 카운트다운으로 만들지 않는다.
        for value in (self.duration_seconds, self.remaining_seconds):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError("Timer seconds must be finite Decimal values")
        if self.duration_seconds <= 0 or not 0 <= self.remaining_seconds <= self.duration_seconds:
            raise ValueError("Timer remaining seconds must be within its positive duration")
        if not self.condition_id or not self.timer_id:
            raise ValueError("Timer condition and instance IDs are required")
        if self.kind not in ("window", "hold", "time_exit"):
            raise ValueError("Unknown timer kind")
        if self.state not in ("waiting", "running", "stopped", "completed", "expired"):
            raise ValueError("Unknown timer state")
        if self.reset_reason not in (None, "timeout", "new_low", "condition_broken", "candle_changed", "stream_reset"):
            raise ValueError("Unknown timer reset reason")

        # 완료와 정지는 실제 판정에서만 만들며 숫자와 상태를 한 객체에서 검증한다.
        if self.state in ("completed", "expired") and self.remaining_seconds != 0:
            raise ValueError("Terminal timers must have zero remaining seconds")
        if self.state in ("waiting", "stopped") and self.remaining_seconds != self.duration_seconds:
            raise ValueError("Inactive timers must retain their full duration")
        if not isinstance(self.sampled_at, datetime) or self.sampled_at.utcoffset() is None:
            raise ValueError("Timer sample time must be timezone-aware")  # 서로 다른 시계의 시각을 추측해 보정하지 않는다.
