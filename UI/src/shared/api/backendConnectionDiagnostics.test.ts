import { readFileSync } from 'node:fs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { BackendConnectionDiagnosticWriter, connection_error_origin, type ConnectionDiagnostic, type StoredConnectionDiagnostic } from './backendConnectionDiagnostics';
import { CONNECTION_ERROR_CODES, safe_connection_error_code } from './connectionErrorCodes';
import { CONNECTION_VALIDATION_FIELDS, identify_connection_validation_field } from './connectionValidationFields';
import { connection_recovery_message } from './connectionRecoveryMessage';

const record: ConnectionDiagnostic = { event: 'heartbeat', session_id: '00000000-0000-4000-8000-000000000001',
    adapter_id: '00000000-0000-4000-8000-000000000002', generation: 1, last_sequence: 42, last_received_at_ms: 1000 };

describe('independent backend connection log persistence', () => {
    beforeEach(() => { vi.useFakeTimers(); vi.spyOn(console, 'error').mockImplementation(() => undefined); });
    afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

    it('preserves first failure, correlations and lost-record count through native write failures', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const accepted: StoredConnectionDiagnostic[] = [];
        let fail = true;
        const writer = new BackendConnectionDiagnosticWriter(async (batch) => {
            if (fail) throw new Error('SECRET_NATIVE_ERROR');
            accepted.push(...batch);
        });
        writer.record({ ...record, event: 'first_failure', incident_id: crypto.randomUUID(), error_code: 'EVENT_STREAM_CLOSED' });
        await writer.flush();
        for (let index = 0; index < 300; index++) writer.record(record);
        fail = false;

        // 가짜 시간을 진행해 예약된 작업과 후속 상태 반영을 실행한다.
        await vi.advanceTimersByTimeAsync(5_000);

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(accepted).toHaveLength(256);
        expect(accepted[0]).toMatchObject({ event: 'first_failure', sequence: 1, last_sequence: 42 });
        expect(accepted.at(-1)?.sequence).toBe(301);
        expect(accepted.reduce((total, item) => total + item.dropped_before, 0)).toBe(45);
        expect(JSON.stringify(accepted)).not.toContain('SECRET_NATIVE_ERROR');
        expect(vi.getTimerCount()).toBe(0);
    });

    it('bounds an unresponsive native call and retries the same record identities', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        let release!: () => void;
        const batches: StoredConnectionDiagnostic[][] = [];
        const persist = vi.fn(async (batch: StoredConnectionDiagnostic[]) => {
            batches.push(batch);
            if (batches.length === 1) await new Promise<void>((resolve) => { release = resolve; });
        });
        const writer = new BackendConnectionDiagnosticWriter(persist);
        writer.record({ ...record, event: 'first_failure' });

        // 가짜 시간을 진행해 예약된 작업과 후속 상태 반영을 실행한다.
        await vi.advanceTimersByTimeAsync(10_000);

        // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
        expect(persist).toHaveBeenCalledTimes(2);
        expect(batches[0]).toEqual(batches[1]);
        release(); await vi.advanceTimersByTimeAsync(0);
        expect(vi.getTimerCount()).toBe(0);  // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
    });

    it('keeps the native and UI allowlists equal and rejects free-form error text', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const source = readFileSync('apps/desktop/src-tauri/src/backend_connection_diagnostics.rs', 'utf8');
        for (const [name, values] of [['ALLOWED_ERROR_CODES', CONNECTION_ERROR_CODES], ['ALLOWED_VALIDATION_FIELDS', CONNECTION_VALIDATION_FIELDS]] as const) {
            const body = source.split(`const ${name}: &[&str] = &[`)[1]!.split('];')[0]!;
            const native = [...body.matchAll(/"([^"]+)"/gu)].map((match) => match[1]);
            expect(native).toEqual([...values]);
        }

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(safe_connection_error_code('SECRET_CANARY')).toBe('UNCLASSIFIED_CONNECTION_ERROR');
        expect(identify_connection_validation_field('market.current_price must be a decimal')).toBe('market.current_price');
        expect(identify_connection_validation_field('SECRET_CANARY')).toBeUndefined();
    });

    it('keeps the originating UI frame without exposing URLs or error messages', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const error = new Error('SECRET_CANARY');
        error.stack = 'Error: SECRET_CANARY\n at notify (http://127.0.0.1:5173/src/UiApplicationStore.ts:145:12?token=SECRET)\n at BackendUiAdapter.ts:2000:5';

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(connection_error_origin(error)).toBe('UiApplicationStore.ts:145:12');
        expect(connection_recovery_message('UI_STATE_PUBLICATION_FAILED')).toContain('화면에 반영');
        expect(connection_recovery_message('EVENT_STREAM_SOCKET_ERROR')).toContain('통신');
        expect(connection_recovery_message('MALFORMED_BACKEND_PAYLOAD')).toContain('형식');
        expect(connection_recovery_message('SECRET_CANARY')).not.toContain('SECRET');
    });
});
