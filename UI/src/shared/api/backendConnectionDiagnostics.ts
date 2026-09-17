import { invoke } from '@tauri-apps/api/core';
import { renderer_instance_id } from './rendererLiveness';

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
    readonly error_type?: 'TypeError' | 'RangeError' | 'SyntaxError' | 'AbortError' | 'ContractError' | 'AdapterError' | 'CommandError' | 'Error' | 'Unknown' | undefined;
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
    readonly client_connection_id?: string;
    readonly monotonic_ms?: number;
    readonly last_received_monotonic_ms?: number | null;
}

export interface StoredConnectionDiagnostic extends ConnectionDiagnostic {
    readonly renderer_id: string;
    readonly at_ms: number;
    readonly sequence: number;
    readonly dropped_before: number;
}


/**
 * 클래스 이름: BackendConnectionDiagnosticWriter
 * 기능: 인증 값과 원본 응답 없이 제한된 진단 큐를 관리하고 IPC 실패 중에도 최초 오류를 우선 보존한다.
 * 작성 날짜: 2026/09/17
 */
export class BackendConnectionDiagnosticWriter {
    private readonly renderer_id = renderer_instance_id;
    private pending: StoredConnectionDiagnostic[] = [];
    private sequence = 0;
    private dropped = 0;
    private in_flight: Promise<void> | null = null;
    private retry_timer: ReturnType<typeof setTimeout> | null = null;

    /**
     * 함수 이름: BackendConnectionDiagnosticWriter.constructor()
     * 기능: 진단 batch를 저장할 비동기 함수를 보관한다.
     * 인자: persist -> 진단 기록 배열을 저장하는 함수
     * 반환값: 생성된 진단 writer
     * 작성 날짜: 2026/09/17
     */
    constructor(private readonly persist: (records: StoredConnectionDiagnostic[]) => Promise<unknown>) {}

    /**
     * 함수 이름: record()
     * 기능: 진단에 수신 시각·순번을 붙이고 큐 상한과 최초 오류 보존 정책을 적용한다.
     * 인자: diagnostic -> 허용된 고정 필드의 연결 진단
     * 반환값: 없음
     * 작성 날짜: 2026/09/17
     */
    record(diagnostic: ConnectionDiagnostic): void {
        // 수신 시각과 순번을 기록 시점에 고정해 저장 재시도에도 사건 순서를 유지한다.
        this.pending.push({ ...diagnostic, renderer_id: this.renderer_id, at_ms: Date.now(),
            monotonic_ms: Math.round(performance.now()),
            sequence: ++this.sequence, dropped_before: 0 });

        // 최초 오류는 일반 heartbeat/후속 오류보다 오래 보존한다. 전부 최초 오류일 때만 가장 오래된 것을 버린다.
        if (this.pending.length > 256) {
            const ordinary = this.pending.findIndex((record) => record.event !== 'first_failure');
            const [removed] = this.pending.splice(ordinary < 0 ? 0 : ordinary, 1);
            this.dropped += 1 + (removed?.dropped_before ?? 0);
        }
        if (this.retry_timer === null) void this.flush();
    }

    /**
     * 함수 이름: flush()
     * 기능: 동시에 한 저장 작업만 진행하고 실패한 batch를 복구해 재시도를 예약한다.
     * 인자: 없음
     * 반환값: 현재 저장 시도의 완료 Promise
     * 작성 날짜: 2026/09/17
     */
    async flush(): Promise<void> {
        // 동시 flush는 현재 Promise를 공유하여 같은 batch를 중복 저장하지 않는다.
        if (this.in_flight !== null) return this.in_flight;
        if (this.retry_timer !== null) {
            clearTimeout(this.retry_timer);
            this.retry_timer = null;
        }

        /**
         * 함수 이름: drain()
         * 기능: 진단을 원래 순서의 batch로 저장하고 실패 시 누락 개수와 함께 큐에 되돌린다.
         * 인자: 없음
         * 반환값: 큐 소진 또는 재시도 예약까지의 Promise
         * 작성 날짜: 2026/09/17
         */
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


/**
 * 함수 이름: record_backend_connection_diagnostic()
 * 기능: 데스크톱 환경에서만 공유 writer를 만들어 연결 진단을 기록한다.
 * 인자: diagnostic -> 저장할 연결 진단
 * 반환값: 없음
 * 작성 날짜: 2026/09/17
 */
export function record_backend_connection_diagnostic(diagnostic: ConnectionDiagnostic): void {
    if (typeof window === 'undefined' || !('__TAURI_INTERNALS__' in window)) return;
    native_writer ??= new BackendConnectionDiagnosticWriter((records) => invoke('record_backend_connection_diagnostics', { records }));
    native_writer.record(diagnostic);
}


/**
 * 함수 이름: flush_backend_connection_diagnostics()
 * 기능: 이미 생성된 네이티브 진단 writer의 남은 저장을 기다린다.
 * 인자: 없음
 * 반환값: 현재 저장 시도의 완료 Promise
 * 작성 날짜: 2026/09/17
 */
export async function flush_backend_connection_diagnostics(): Promise<void> {
    await native_writer?.flush();
}


/**
 * 함수 이름: connection_error_origin()
 * 기능: 오류 stack에서 허용된 소스 파일·행·열만 추출하고 URL·메시지는 제외한다.
 * 인자: error -> 발생한 원본 오류
 * 반환값: 허용된 소스 위치 문자열 또는 undefined
 * 작성 날짜: 2026/09/17
 */
export function connection_error_origin(error: unknown): string | undefined {
    if (!(error instanceof Error)) return undefined;

    const location = error.stack?.split('\n').slice(1).map((line) =>
        line.match(/(?:backendEventMapper|tradingIndicatorValidation|BackendUiAdapter|UiApplicationFacade|UiApplicationStore|createLiveUiApplication)\.(?:ts|js):(\d+):(\d+)/u)?.[0],
    ).find(Boolean);

    return location;
}
