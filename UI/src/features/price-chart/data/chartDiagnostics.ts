import { invoke } from '@tauri-apps/api/core';
import { renderer_instance_id, observe_renderer_connection } from '../../../shared/api/rendererLiveness';
import type { ChartInterval } from '../types';
import type { ChartErrorDiagnostic } from './chartDataError';

export interface ChartIntervalDiagnostic {
    readonly interval: ChartInterval;
    readonly received_at_ms: number | null;
    readonly open_time_ms: number | null;
    readonly event_time_ms: number | null;
    readonly close: number | null;
    readonly count: number;
}

export interface ChartDiagnostic extends ChartErrorDiagnostic {
    readonly event: 'connection_started' | 'socket_opened' | 'rest_started' | 'rest_completed'
        | 'rest_failed' | 'rest_timeout' | 'first_kline' | 'heartbeat' | 'stream_stale'
        | 'socket_closed' | 'socket_error' | 'parse_error' | 'reconnect_scheduled'
        | 'connection_ready' | 'stopped' | 'browser_online' | 'visibility_changed'
        | 'connect_timeout' | 'render_applied' | 'render_failed';
    readonly connection_id?: number;
    readonly interval?: ChartInterval;
    readonly elapsed_ms?: number;
    readonly close_code?: number;
    readonly clean?: boolean;
    readonly attempt?: number;
    readonly delay_ms?: number;
    readonly visible?: boolean;
    readonly online?: boolean;
    readonly intervals?: ReadonlyArray<ChartIntervalDiagnostic>;
}

interface StoredChartDiagnostic extends ChartDiagnostic {
    readonly monotonic_ms: number;
    readonly renderer_id: string;
    readonly at_ms: number;
    readonly sequence: number;
    readonly dropped_before: number;
}

const renderer_id = renderer_instance_id;
const pending_records: Array<StoredChartDiagnostic> = [];
let sequence = 0;
let dropped_records = 0;
let flush_is_running = false;
let retry_timer: ReturnType<typeof setTimeout> | null = null;

/**
 * 함수 이름: flush_chart_diagnostics()
 * 기능: 제한된 진단 queue를 native 파일 writer에 전달하고 실패 시 다음 시도까지 보존한다.
 * 인자: 없음
 * 반환값: 없음
 * 작성 날짜: 2026/09/11
 */
async function flush_chart_diagnostics(): Promise<void> {
    if (flush_is_running || pending_records.length === 0) return;
    flush_is_running = true;
    const records = pending_records.splice(0, 32);
    if (dropped_records > 0 && records[0] !== undefined) {
        records[0] = { ...records[0], dropped_before: records[0].dropped_before + dropped_records };
        dropped_records = 0;
    }
    try {
        await invoke('record_chart_diagnostics', { records });
    } catch {
        pending_records.unshift(...records);
        if (pending_records.length > 256) {
            const removed = pending_records.splice(0, pending_records.length - 256);
            dropped_records += removed.reduce((count, record) => count + 1 + record.dropped_before, 0);
        }
        // 저장 장애가 차트 수신을 막지 않으며 임의 native 오류 원문은 출력하지 않는다.
        console.error('CHART_DIAGNOSTIC_WRITE_FAILED');
        retry_timer = setTimeout(() => {
            retry_timer = null;
            void flush_chart_diagnostics();
        }, 5_000);
    } finally {
        flush_is_running = false;
        if (retry_timer === null && pending_records.length > 0) void flush_chart_diagnostics();
    }
}

/**
 * 함수 이름: record_chart_diagnostic()
 * 기능: 원본 payload·주소·인증 정보 없이 chart 상태와 수신 사실을 native 로그에 저장한다.
 * 인자: diagnostic -> 고정 event와 공개 시장 수치
 * 반환값: 없음
 * 작성 날짜: 2026/09/11
 */
export function record_chart_diagnostic(diagnostic: ChartDiagnostic): void {
    const received = diagnostic.intervals?.map((entry) => entry.received_at_ms ?? 0) ?? [];
    if (received.some((value) => value > 0)) observe_renderer_connection({ last_chart_received_at_ms: Math.max(...received) });
    if (typeof window === 'undefined' || !('__TAURI_INTERNALS__' in window)) return;
    if (pending_records.length >= 256) {
        const removed = pending_records.shift();
        dropped_records += 1 + (removed?.dropped_before ?? 0);
    }
    pending_records.push({
        ...diagnostic, renderer_id, at_ms: Date.now(), monotonic_ms: Math.round(performance.now()), sequence: ++sequence,
        dropped_before: dropped_records,
    });
    dropped_records = 0;
    if (retry_timer === null) void flush_chart_diagnostics();
}
