import { invoke } from '@tauri-apps/api/core';

export type ConnectionDiagnosticStage = 'connect' | 'authenticate' | 'receive' | 'decode'
    | 'map' | 'publish' | 'resync' | 'request' | 'shutdown' | 'lifecycle';
export interface ConnectionDiagnostic {
    readonly event: 'connection_started' | 'socket_opened' | 'authentication_sent' | 'heartbeat'
        | 'socket_closed' | 'socket_error' | 'request_started' | 'request_succeeded'
        | 'request_failed' | 'first_failure' | 'failure' | 'retry_scheduled'
        | 'connection_ready' | 'terminal_failure' | 'shutdown_started' | 'shutdown_completed'
        | 'stopped' | 'environment_changed';
    readonly session_id: string;
    readonly adapter_id: string;
    readonly generation: number;
    readonly last_sequence: number;
    readonly last_received_at_ms: number | null;
    readonly incident_id?: string;
    readonly error_code?: string;
    readonly error_type?: 'TypeError' | 'RangeError' | 'SyntaxError' | 'AbortError' | 'ContractError' | 'AdapterError' | 'CommandError' | 'Unknown' | undefined;
    readonly stage?: ConnectionDiagnosticStage;
    readonly origin?: string | undefined;
    readonly validation_field?: string | undefined;
    readonly retryable?: boolean;
    readonly request_id?: string;
    readonly operation?: 'snapshot' | 'shutdown_state' | 'shutdown' | 'trading' | 'regime' | 'history' | 'csv' | 'connection_status' | 'other';
    readonly http_status?: number;
    readonly elapsed_ms?: number;
    readonly close_code?: number;
    readonly clean?: boolean;
    readonly attempt?: number;
    readonly delay_ms?: number;
    readonly visible?: boolean;
    readonly online?: boolean;
}

export interface StoredConnectionDiagnostic extends ConnectionDiagnostic {
    readonly renderer_id: string;
    readonly at_ms: number;
    readonly sequence: number;
    readonly dropped_before: number;
}

/** Native IPC 실패 중에도 최초 오류를 보존하는 bounded queue. 인증 값과 원본 응답은 받지 않는다. */
export class BackendConnectionDiagnosticWriter {
    private readonly renderer_id = globalThis.crypto.randomUUID();
    private pending: StoredConnectionDiagnostic[] = [];
    private sequence = 0;
    private dropped = 0;
    private in_flight: Promise<void> | null = null;
    private retry_timer: ReturnType<typeof setTimeout> | null = null;

    constructor(private readonly persist: (records: StoredConnectionDiagnostic[]) => Promise<unknown>) {}

    record(diagnostic: ConnectionDiagnostic): void {
        this.pending.push({ ...diagnostic, renderer_id: this.renderer_id, at_ms: Date.now(),
            sequence: ++this.sequence, dropped_before: 0 });
        // 최초 오류는 일반 heartbeat/후속 오류보다 오래 보존한다. 전부 최초 오류일 때만 가장 오래된 것을 버린다.
        if (this.pending.length > 256) {
            const ordinary = this.pending.findIndex((record) => record.event !== 'first_failure');
            const [removed] = this.pending.splice(ordinary < 0 ? 0 : ordinary, 1);
            this.dropped += 1 + (removed?.dropped_before ?? 0);
        }
        if (this.retry_timer === null) void this.flush();
    }

    async flush(): Promise<void> {
        if (this.in_flight !== null) return this.in_flight;
        if (this.retry_timer !== null) {
            clearTimeout(this.retry_timer);
            this.retry_timer = null;
        }
        const drain = async () => {
            while (this.pending.length > 0) {
                // 원래 순서를 보존한다. 실패 시 in-flight batch도 queue와 합쳐 상한을 적용한다.
                const batch = this.pending.splice(0, 32);
                batch[0] = { ...batch[0]!, dropped_before: batch[0]!.dropped_before + this.dropped };
                this.dropped = 0;
                let timeout: ReturnType<typeof setTimeout> | undefined;
                try {
                    await Promise.race([this.persist(batch), new Promise<never>((_, reject) => {
                        timeout = setTimeout(() => reject(new Error('DIAGNOSTIC_ACK_TIMEOUT')), 5_000);
                    })]);
                }
                catch {
                    this.pending.unshift(...batch);
                    while (this.pending.length > 256) {
                        const ordinary = this.pending.findIndex((record) => record.event !== 'first_failure');
                        const [removed] = this.pending.splice(ordinary < 0 ? 0 : ordinary, 1);
                        this.dropped += 1 + (removed?.dropped_before ?? 0);
                    }
                    console.error('BACKEND_CONNECTION_DIAGNOSTIC_WRITE_FAILED');
                    this.retry_timer = setTimeout(() => { this.retry_timer = null; void this.flush(); }, 5_000);
                    return;
                } finally { if (timeout !== undefined) clearTimeout(timeout); }
            }
        };
        // Promise를 먼저 등록해 동기 record 재진입도 한 writer로 직렬화한다.
        this.in_flight = Promise.resolve().then(drain);
        try { await this.in_flight; } finally {
            this.in_flight = null;
            // drain 종료와 finally 사이에 들어온 record도 다음 사건을 기다리지 않고 저장한다.
            if (this.pending.length > 0 && this.retry_timer === null) void this.flush();
        }
    }
}

let native_writer: BackendConnectionDiagnosticWriter | null = null;
export function record_backend_connection_diagnostic(diagnostic: ConnectionDiagnostic): void {
    if (typeof window === 'undefined' || !('__TAURI_INTERNALS__' in window)) return;
    native_writer ??= new BackendConnectionDiagnosticWriter((records) => invoke('record_backend_connection_diagnostics', { records }));
    native_writer.record(diagnostic);
}

export async function flush_backend_connection_diagnostics(): Promise<void> {
    await native_writer?.flush();
}

/** 오류 원문 대신 소스에 있는 함수명·행 번호만 추출한다. URL/query/오류 message는 버린다. */
export function connection_error_origin(error: unknown): string | undefined {
    if (!(error instanceof Error)) return undefined;
    const location = error.stack?.split('\n').slice(1).map((line) =>
        line.match(/(?:backendEventMapper|tradingIndicatorValidation|BackendUiAdapter)\.(?:ts|js):(\d+):(\d+)/u)?.[0],
    ).find(Boolean);
    return location;
}
