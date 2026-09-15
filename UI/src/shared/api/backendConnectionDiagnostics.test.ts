import { readFileSync } from 'node:fs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { BackendConnectionDiagnosticWriter, type ConnectionDiagnostic, type StoredConnectionDiagnostic } from './backendConnectionDiagnostics';
import { CONNECTION_ERROR_CODES, safe_connection_error_code } from './connectionErrorCodes';
import { CONNECTION_VALIDATION_FIELDS, identify_connection_validation_field } from './connectionValidationFields';

const record: ConnectionDiagnostic = { event: 'heartbeat', session_id: '00000000-0000-4000-8000-000000000001',
    adapter_id: '00000000-0000-4000-8000-000000000002', generation: 1, last_sequence: 42, last_received_at_ms: 1000 };

describe('independent backend connection log persistence', () => {
    beforeEach(() => { vi.useFakeTimers(); vi.spyOn(console, 'error').mockImplementation(() => undefined); });
    afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

    it('preserves first failure, correlations and lost-record count through native write failures', async () => {
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
        await vi.advanceTimersByTimeAsync(5_000);
        expect(accepted).toHaveLength(256);
        expect(accepted[0]).toMatchObject({ event: 'first_failure', sequence: 1, last_sequence: 42 });
        expect(accepted.at(-1)?.sequence).toBe(301);
        expect(accepted.reduce((total, item) => total + item.dropped_before, 0)).toBe(45);
        expect(JSON.stringify(accepted)).not.toContain('SECRET_NATIVE_ERROR');
        expect(vi.getTimerCount()).toBe(0);
    });

    it('bounds an unresponsive native call and retries the same record identities', async () => {
        let release!: () => void;
        const batches: StoredConnectionDiagnostic[][] = [];
        const persist = vi.fn(async (batch: StoredConnectionDiagnostic[]) => {
            batches.push(batch);
            if (batches.length === 1) await new Promise<void>((resolve) => { release = resolve; });
        });
        const writer = new BackendConnectionDiagnosticWriter(persist);
        writer.record({ ...record, event: 'first_failure' });
        await vi.advanceTimersByTimeAsync(10_000);
        expect(persist).toHaveBeenCalledTimes(2);
        expect(batches[0]).toEqual(batches[1]);
        release(); await vi.advanceTimersByTimeAsync(0);
        expect(vi.getTimerCount()).toBe(0);
    });

    it('keeps the native and UI allowlists equal and rejects free-form error text', () => {
        const source = readFileSync('apps/desktop/src-tauri/src/backend_connection_diagnostics.rs', 'utf8');
        for (const [name, values] of [['ALLOWED_ERROR_CODES', CONNECTION_ERROR_CODES], ['ALLOWED_VALIDATION_FIELDS', CONNECTION_VALIDATION_FIELDS]] as const) {
            const body = source.split(`const ${name}: &[&str] = &[`)[1]!.split('];')[0]!;
            const native = [...body.matchAll(/"([^"]+)"/gu)].map((match) => match[1]);
            expect(native).toEqual([...values]);
        }
        expect(safe_connection_error_code('SECRET_CANARY')).toBe('UNCLASSIFIED_CONNECTION_ERROR');
        expect(identify_connection_validation_field('market.current_price must be a decimal')).toBe('market.current_price');
        expect(identify_connection_validation_field('SECRET_CANARY')).toBeUndefined();
    });
});
