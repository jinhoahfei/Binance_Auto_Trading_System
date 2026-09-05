import type { RealtimeIndicatorTimerViewModel } from './types';

export type IndicatorTimerState = 'waiting' | 'running' | 'stopped' | 'completed' | 'expired' | 'pending' | 'unavailable';

export interface IndicatorTimerPresentation {
    readonly time: string;
    readonly state: IndicatorTimerState;
    readonly label: string;
    readonly reason: string | null;
}

const RESET_REASONS: Readonly<Record<string, string>> = {
    timeout: '시간 초과로 재시작', new_low: '저점 갱신으로 재시작',
    candle_changed: '새 30분봉에서 다시 계산', stream_reset: '시장 재동기화 후 다시 계산',
};

/**
 * 함수 이름: format_timer_seconds()
 * 기능: 남은 초를 올림하고 전체 지속시간 기준으로 고정된 시·분·초 폭을 사용한다.
 * 인자: remaining -> 표시할 남은 초, duration -> 타이머 전체 초
 * 반환값: HH:MM:SS 또는 MM:SS 문자열
 * 작성 날짜: 2026/09/05
 */
export function format_timer_seconds(remaining: number, duration: number): string {
    const seconds = Math.ceil(Math.max(0, remaining));
    const minutes = Math.floor(seconds / 60) % 60;
    const parts = [minutes, seconds % 60].map((value) => String(value).padStart(2, '0'));
    if (duration >= 3600) parts.unshift(String(Math.floor(seconds / 3600)).padStart(2, '0'));
    return parts.join(':');  // 짧은 잔여시간으로 바뀌어도 열 너비를 바꾸지 않는다.
}

/**
 * 함수 이름: present_indicator_timer()
 * 기능: 서버의 마지막 타이머와 로컬 단조 경과로 표시만 보정하고 실제 완료 판정은 보존한다.
 * 인자: model -> 서버 타이머·전송 시각·최초 수신 기준, now -> 현재 UI monotonic millisecond
 * 반환값: 숫자와 구별된 상태·리셋 사유 표시
 * 작성 날짜: 2026/09/05
 */
export function present_indicator_timer(model: RealtimeIndicatorTimerViewModel, now: number): IndicatorTimerPresentation {
    const timer = model.snapshot;
    if (timer === null || model.server_time === null || model.received_at === null) {
        return { time: '—', state: 'unavailable', label: '확인 대기', reason: null };
    }

    // 두 서버 시각의 차이와 로컬 monotonic 차이만 써서 PC의 시간대·시각 오차를 배제한다.
    const duration = Number(timer.duration_seconds);
    const server_elapsed = Math.max(0, Date.parse(model.server_time) - Date.parse(timer.sampled_at)) / 1000;
    const local_elapsed = Math.max(0, now - model.received_at) / 1000;
    const remaining = timer.state === 'running'
        ? Math.max(0, Number(timer.remaining_seconds) - server_elapsed - local_elapsed)
        : Number(timer.remaining_seconds);
    const state: IndicatorTimerState = timer.state === 'running' && remaining === 0 ? 'pending' : timer.state;
    const labels: Readonly<Record<IndicatorTimerState, string>> = {
        waiting: '시작 대기', running: '진행 중', stopped: '정지 · 조건 미충족',
        completed: timer.kind === 'hold' ? '유지 완료' : '시간 도달', expired: '기한 만료',
        pending: '판정 대기', unavailable: '확인 대기',
    };
    return {
        time: format_timer_seconds(remaining, duration), state, label: labels[state],
        reason: timer.reset_reason === null ? null : RESET_REASONS[timer.reset_reason] ?? null,
    };  // 숫자가 0이 되는 것만으로 satisfied나 전략 상태를 변경하지 않는다.
}
