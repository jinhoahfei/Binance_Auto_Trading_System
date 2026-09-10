"""Classify repeated market input without rolling authoritative candles backwards."""
from dataclasses import replace
from datetime import timedelta
from .kline import Kline

_DURATIONS = {"1m": timedelta(minutes=1), "30m": timedelta(minutes=30),
              "4h": timedelta(hours=4), "1d": timedelta(days=1)}


def classify_kline(previous: Kline, current: Kline) -> str:
    """
    함수 이름: classify_kline()
    기능: Return accept, duplicate, stale, or conflict for one interval cursor.
    인자: 선언된 입력으로 현재 처리 상태를 확인한다.
    반환값: 처리 결과 또는 없음
    작성 날짜: 2026/09/10
    """
    if previous == current:
        return "duplicate"
    if previous.closed and current.closed and replace(previous, event_time=current.event_time) == current:
        return "duplicate"
    if previous.event_time is None or current.event_time is None:
        raise ValueError("stream cursors require event times")
    if current.open_time < previous.open_time:
        if (current.closed and not previous.closed
                and current.open_time + _DURATIONS[current.interval.value] == previous.open_time):
            return "late_close"
        return "stale"
    same_candle = current.open_time == previous.open_time
    if same_candle and previous.closed and not current.closed:
        return "stale"
    if current.event_time < previous.event_time:
        return "stale"
    if current.event_time == previous.event_time:
        if same_candle and not previous.closed and current.closed:
            if (current.open == previous.open and current.high >= previous.high
                    and current.low <= previous.low and current.volume >= previous.volume):
                return "accept"
        return "conflict"
    if same_candle and previous.closed:
        return "conflict"
    return "accept"
