import type { BackendTradingTimer } from '../contracts';


/**
 * 함수 이름: is_timer_timestamp()
 * 기능: 타이머의 서버 시각을 UTC 형식으로 제한해 클라이언트 시간대 추측을 막는다.
 * 인자: value -> 검증할 wire 시각
 * 반환값: 유효한 UTC 시각 여부
 * 작성 날짜: 2026/09/05
 */
export function is_timer_timestamp(value: unknown): value is string {
    return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/u.test(value)
        && Number.isFinite(Date.parse(value));  // 화면의 로컬 시각으로 누락된 시간대를 채우지 않는다.
}


/**
 * 함수 이름: is_trading_timer()
 * 기능: 선택적 타이머의 시간 범위·상태·회차·리셋 사유 계약을 검증한다.
 * 인자: value -> 수신한 타이머 객체
 * 반환값: 유효한 타이머 DTO 여부
 * 작성 날짜: 2026/09/05
 */
export function is_trading_timer(value: unknown): value is BackendTradingTimer {
    if (value === null || typeof value !== 'object') return false;

    const timer = value as Record<string, unknown>;
    if (typeof timer.timer_id !== 'string' || !timer.timer_id.trim()
        || !['window', 'hold', 'time_exit'].includes(String(timer.kind))
        || !['waiting', 'running', 'stopped', 'completed', 'expired'].includes(String(timer.state))
        || !is_timer_timestamp(timer.sampled_at)
        || (timer.reset_reason !== null && !['timeout', 'new_low', 'condition_broken', 'candle_changed', 'stream_reset'].includes(String(timer.reset_reason)))) return false;

    // 금융 값과 같은 plain Decimal 계약을 사용하며 표시 가능한 유한 시간만 받는다.
    for (const seconds of [timer.duration_seconds, timer.remaining_seconds]) {
        if (typeof seconds !== 'string' || !/^(0|[1-9][0-9]*)(\.[0-9]+)?$/u.test(seconds)
            || !Number.isFinite(Number(seconds)) || Number(seconds) > Number.MAX_SAFE_INTEGER) return false;
    }
    const duration = Number(timer.duration_seconds);
    const remaining = Number(timer.remaining_seconds);
    if (duration <= 0 || remaining > duration) return false;
    if (['waiting', 'stopped'].includes(String(timer.state)) && remaining !== duration) return false;
    if (['completed', 'expired'].includes(String(timer.state)) && remaining !== 0) return false;

    return true;  // 실행 여부를 지표값에서 다시 추론하지 않는다.
}
