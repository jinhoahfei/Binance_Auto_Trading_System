import { invoke } from '@tauri-apps/api/core';

// 한 document의 backend·차트·독립 생존 신호를 같은 식별자로 연결한다.
export const renderer_instance_id = globalThis.crypto.randomUUID();
const observations = {
    session_id: null as string | null,
    last_received_at_ms: null as number | null,
    last_applied_at_ms: null as number | null,
    last_sequence: 0,
    last_chart_received_at_ms: null as number | null,
};


/**
 * 함수 이름: observe_renderer_connection()
 * 기능: 독립 생존 신호에 실을 최신 backend·차트 수신 정보를 합친다.
 * 인자: values -> 갱신할 관찰 필드
 * 반환값: 없음
 * 작성 날짜: 2026/09/17
 */
export function observe_renderer_connection(values: Partial<typeof observations>): void {
    Object.assign(observations, values);
}


/**
 * 함수 이름: start_renderer_liveness()
 * 기능: 데스크톱에서 독립 heartbeat를 시작하고 한 번에 하나의 네이티브 ACK만 기다린다.
 * 인자: 없음
 * 반환값: 생존 신호 타이머를 종료하는 함수
 * 작성 날짜: 2026/09/17
 */
export function start_renderer_liveness(): () => void {
    // 데스크톱 환경에서만 생존 신호의 순번과 실행 상태를 관리한다.
    if (typeof window === 'undefined' || !('__TAURI_INTERNALS__' in window)) return () => {};

    let previous = performance.now();
    let sequence = 0;
    let pending = false;
    let stopped = false;
    let failed = 0;

    /**
     * 함수 이름: tick()
     * 기능: 이벤트 루프 지연과 최신 관찰값을 기록하고 IPC 실패를 누적한다.
     * 인자: 없음
     * 반환값: 이번 heartbeat 전송 시도의 완료 Promise
     * 작성 날짜: 2026/09/17
     */
    const tick = async () => {
        // 단조 시각의 간격으로 renderer 실행 지연을 측정한다.
        const now = performance.now();
        const lag = Math.max(0, now - previous - 5_000);
        previous = now;

        // 이전 IPC가 끝나지 않았거나 종료된 경우 전송을 겹치지 않는다.
        if (pending || stopped) return;
        pending = true;
        try {
            await invoke('record_renderer_heartbeat', { record: {
                renderer_id: renderer_instance_id, sequence: ++sequence, at_ms: Date.now(),
                monotonic_ms: Math.round(now), timer_lag_ms: Math.round(lag),
                visible: document.visibilityState === 'visible', online: navigator.onLine,
                ipc_failures: failed, ...observations,
            } });
        } catch { failed++; }
        finally { pending = false; }
    };

    // 첫 신호를 즉시 보내고 이후 주기적으로 전송한다.
    const timer = setInterval(() => void tick(), 5_000);
    void tick();

    // 화면 수명이 끝나면 예약된 주기 전송을 해제한다.
    return () => { stopped = true; clearInterval(timer); };
}
