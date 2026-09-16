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
export function observe_renderer_connection(values: Partial<typeof observations>): void {
    Object.assign(observations, values);
}

/** Native ACK 대기는 한 개만 유지한다. 저장이나 IPC 지연은 매매 흐름에 전파하지 않는다. */
export function start_renderer_liveness(): () => void {
    if (typeof window === 'undefined' || !('__TAURI_INTERNALS__' in window)) return () => {};
    let previous = performance.now();
    let sequence = 0;
    let pending = false;
    let stopped = false;
    let failed = 0;
    const tick = async () => {
        const now = performance.now();
        const lag = Math.max(0, now - previous - 5_000);
        previous = now;
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
    const timer = setInterval(() => void tick(), 5_000);
    void tick();
    return () => { stopped = true; clearInterval(timer); };
}
