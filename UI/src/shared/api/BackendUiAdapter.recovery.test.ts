import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { BackendUiAdapter, type BackendWebSocket, type BackendUiAdapterCallbacks } from './BackendUiAdapter';
import type { ConnectionDiagnostic } from './backendConnectionDiagnostics';
import { BACKEND_SCHEMA_VERSION } from '../contracts';
import { create_backend_account_fixture, create_backend_event_fixture, create_backend_snapshot_fixture,
    TEST_BACKEND_SESSION_ID, TEST_BACKEND_TOKEN } from './backendTestFixtures';

class Socket implements BackendWebSocket {
    onopen: BackendWebSocket['onopen'] = null;
    onmessage: BackendWebSocket['onmessage'] = null;
    onclose: BackendWebSocket['onclose'] = null;
    onerror: BackendWebSocket['onerror'] = null;
    close = vi.fn();
    send = vi.fn();
    receive(sequence = 10) { this.onmessage?.({ data: JSON.stringify(create_backend_event_fixture(sequence, 'ACCOUNT_UPDATED', { account: create_backend_account_fixture() }, crypto.randomUUID())) } as MessageEvent); }
    disconnect() { this.onclose?.({ code: 1006, wasClean: false } as CloseEvent); }
    open() { this.onopen?.(new Event('open')); }
}

const descriptor = { port: 42123, schema_version: BACKEND_SCHEMA_VERSION, session_id: TEST_BACKEND_SESSION_ID, token: TEST_BACKEND_TOKEN };
function response(init: RequestInit | undefined, data: unknown, status = 200) {
    return new Response(JSON.stringify({ schema_version: BACKEND_SCHEMA_VERSION,
        request_id: new Headers(init?.headers).get('X-Request-Id'), ok: true, data }), { status });
}
const adapters: BackendUiAdapter[] = [];
function setup(fetcher: typeof fetch = async (_input, init) => response(init, create_backend_snapshot_fixture())) {
    const sockets: Socket[] = [];
    const logs: ConnectionDiagnostic[] = [];
    const callbacks: BackendUiAdapterCallbacks = { on_event: vi.fn(), on_full_resync: vi.fn(),
        on_reconnecting: vi.fn(), on_failure: vi.fn(), on_ready: vi.fn(), on_connection_status: vi.fn() };
    const adapter = new BackendUiAdapter(descriptor, { fetch: fetcher, diagnostic: (record) => logs.push(record),
        create_web_socket: () => { const socket = new Socket(); sockets.push(socket); return socket; },
        wait_for_sidecar_exit: async () => ({ exited: true, code: 0 }) });
    adapters.push(adapter);
    const snapshot = create_backend_snapshot_fixture();
    adapter.start_live_events(snapshot, callbacks);
    return { adapter, sockets, logs, callbacks };
}

describe('long-running backend connection recovery', () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => { adapters.splice(0).forEach((adapter) => adapter.stop()); vi.useRealTimers(); });

    it('transient snapshot failures preserve authentication and retry until a verified event resumes', async () => {
        let attempts = 0;
        const { adapter, sockets, callbacks, logs } = setup(async (_input, init) => {
            if (++attempts < 3) throw new TypeError('SECRET_CANARY');
            expect(new Headers(init?.headers).get('Authorization')).toBe(`Bearer ${TEST_BACKEND_TOKEN}`);
            return response(init, create_backend_snapshot_fixture());
        });
        sockets[0]!.disconnect();
        await vi.advanceTimersByTimeAsync(0);
        expect(adapter.is_disposed).toBe(false);
        await vi.advanceTimersByTimeAsync(1_000);
        await vi.advanceTimersByTimeAsync(2_000);
        expect(attempts).toBe(3);
        expect(sockets).toHaveLength(2);
        expect(callbacks.on_ready).not.toHaveBeenCalled();
        await expect(adapter.stop_trading()).rejects.toMatchObject({ code: 'BACKEND_CONNECTION_RECOVERING' });
        sockets[1]!.open(); sockets[1]!.receive();
        expect(callbacks.on_ready).toHaveBeenCalledOnce();
        expect(callbacks.on_failure).not.toHaveBeenCalled();
        expect(logs.filter((record) => record.event === 'first_failure')).toHaveLength(1);
        const incident = logs.find((record) => record.event === 'first_failure')!.incident_id;
        expect(logs.filter((record) => record.event === 'failure').every((record) => record.incident_id === incident)).toBe(true);
        expect(logs.some((record) => record.error_type === 'TypeError' && record.operation === 'snapshot')).toBe(true);
        expect(JSON.stringify(logs)).not.toContain('SECRET_CANARY');
        expect(JSON.stringify(logs)).not.toContain(TEST_BACKEND_TOKEN);
    });

    it('does not wait forever for open or for a silent event stream', async () => {
        const { sockets, logs } = setup();
        await vi.advanceTimersByTimeAsync(10_000);
        expect(logs.some((record) => record.error_code === 'EVENT_STREAM_CONNECT_TIMEOUT')).toBe(true);
        await vi.advanceTimersByTimeAsync(1_000);
        sockets[1]!.open(); sockets[1]!.receive();
        await vi.advanceTimersByTimeAsync(75_000);
        expect(logs.some((record) => record.error_code === 'EVENT_STREAM_STALE')).toBe(true);
        await vi.advanceTimersByTimeAsync(1_000);
        expect(sockets).toHaveLength(3);
    });

    it('normal idle heartbeat events prevent false recovery', async () => {
        const { sockets, callbacks } = setup(); sockets[0]!.open();
        for (let index = 0; index < 10; index++) {
            await vi.advanceTimersByTimeAsync(60_000);
            sockets[0]!.receive(10 + index);
        }
        expect(sockets).toHaveLength(1);
        expect(callbacks.on_reconnecting).not.toHaveBeenCalled();
    });

    it('invalidates the old socket callbacks and a late snapshot on explicit disposal', async () => {
        let finish!: (value: Response) => void;
        let request!: RequestInit;
        const { adapter, sockets, callbacks } = setup(async (_input, init) => { request = init!; return new Promise((resolve) => { finish = resolve; }); });
        const old_message = sockets[0]!.onmessage;
        sockets[0]!.disconnect();
        adapter.stop();
        finish(response(request, create_backend_snapshot_fixture()));
        old_message?.({ data: JSON.stringify(create_backend_event_fixture(10, 'ACCOUNT_UPDATED', { account: create_backend_account_fixture() }, crypto.randomUUID())) } as MessageEvent);
        await vi.advanceTimersByTimeAsync(120_000);
        expect(callbacks.on_full_resync).not.toHaveBeenCalled();
        expect(callbacks.on_event).not.toHaveBeenCalled();
        expect(sockets).toHaveLength(1);
        expect(vi.getTimerCount()).toBe(0);
    });

    it('recovers the exact screenshot failure and coalesces concurrent exit clicks', async () => {
        const paths: string[] = [];
        let unavailable = true;
        const { adapter, sockets } = setup(async (input, init) => {
            const path = new URL(String(input)).pathname; paths.push(path);
            if (path === '/v1/snapshot') {
                if (unavailable) throw new TypeError('temporary');
                const snapshot = create_backend_snapshot_fixture();
                return response(init, { ...snapshot, trading: { ...snapshot.trading, status: 'running', session_id: TEST_BACKEND_SESSION_ID, version: 42 } });
            }
            if (path === '/v1/trading/stop') {
                expect(JSON.parse(String(init?.body)).expected_version).toBe(42);
                return response(init, { status: 'terminated', session_id: TEST_BACKEND_SESSION_ID, version: 43 });
            }
            return response(init, { accepted: true, status: 'accepted', version: 43 }, 202);
        });
        sockets[0]!.disconnect(); await vi.advanceTimersByTimeAsync(0); unavailable = false;
        await Promise.all([adapter.shutdown_application(), adapter.shutdown_application()]);
        expect(paths).toEqual(['/v1/snapshot', '/v1/snapshot', '/v1/trading/stop', '/v1/shutdown']);
        expect(adapter.is_disposed).toBe(true);
        await vi.advanceTimersByTimeAsync(120_000);
        expect(sockets).toHaveLength(1);
        expect(vi.getTimerCount()).toBe(0);
    });

    it('request timeout records the original phase before retrying, including body read timeout', async () => {
        const { sockets, logs } = setup(async (_input, init) => {
            return { status: 200, json: () => new Promise((_, reject) => {
                init?.signal?.addEventListener('abort', () => reject(new DOMException('SECRET_CANARY', 'AbortError')), { once: true });
            }) } as Response;
        });
        sockets[0]!.disconnect(); await vi.advanceTimersByTimeAsync(5_000);
        expect(logs.some((record) => record.error_code === 'BACKEND_REQUEST_TIMEOUT' && record.stage === 'request')).toBe(true);
        expect(logs.some((record) => record.event === 'retry_scheduled')).toBe(true);
        expect(JSON.stringify(logs)).not.toContain('SECRET_CANARY');
    });

    it('malformed event preserves validation location and enters terminal recovery instead of retrying', () => {
        const { sockets, logs, callbacks, adapter } = setup();
        const event = create_backend_event_fixture(10, 'ACCOUNT_UPDATED', { account: { symbol: 'SECRET_CANARY' } });
        sockets[0]!.onmessage?.({ data: JSON.stringify(event) } as MessageEvent);
        expect(callbacks.on_failure).toHaveBeenCalledOnce();
        expect(adapter.is_disposed).toBe(true);
        expect(logs.find((record) => record.event === 'first_failure')).toMatchObject({ stage: 'map', error_type: 'ContractError' });
        expect(logs.some((record) => record.origin || record.validation_field)).toBe(true);
        expect(logs.some((record) => record.event === 'retry_scheduled')).toBe(false);
        expect(JSON.stringify(logs)).not.toContain('SECRET_CANARY');
    });
    it('caps retry delay at 30 seconds and retains only one retry timer', async () => {
        const { sockets, logs, adapter } = setup(async () => { throw new TypeError('offline'); });
        sockets[0]!.disconnect(); await vi.advanceTimersByTimeAsync(0);
        for (const delay of [1_000, 2_000, 5_000, 10_000, 30_000, 30_000]) {
            expect(vi.getTimerCount()).toBe(1);
            await vi.advanceTimersByTimeAsync(delay);
        }
        expect(logs.filter((record) => record.event === 'retry_scheduled').map((record) => record.delay_ms))
            .toEqual([1_000, 2_000, 5_000, 10_000, 30_000, 30_000, 30_000]);
        expect(adapter.is_disposed).toBe(false);
    });

    it('100 recovery cycles detach old sockets and do not multiply timers or replay commands', async () => {
        const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => response(init, create_backend_snapshot_fixture()));
        const { sockets, callbacks, adapter } = setup(fetcher);
        for (let index = 0; index < 100; index++) {
            const socket = sockets[index]!;
            socket.open(); socket.receive(); socket.disconnect();
            await vi.advanceTimersByTimeAsync(0);
            expect(socket.onmessage).toBeNull(); expect(socket.onclose).toBeNull();
            expect(vi.getTimerCount()).toBe(1);
        }
        sockets[100]!.open(); sockets[100]!.receive();
        expect(callbacks.on_failure).not.toHaveBeenCalled();
        expect(fetcher).toHaveBeenCalledTimes(100);
        expect(fetcher.mock.calls.every(([input]) => new URL(String(input)).pathname === '/v1/snapshot')).toBe(true);
        adapter.stop(); expect(vi.getTimerCount()).toBe(0);
    });

    it('recovers even when a browser emits error without a close event', async () => {
        const { sockets, logs } = setup();
        sockets[0]!.onerror?.(new Event('error'));
        await vi.advanceTimersByTimeAsync(1_000);
        expect(sockets).toHaveLength(2);
        expect(logs.find((record) => record.event === 'first_failure')).toMatchObject({ error_code: 'EVENT_STREAM_SOCKET_ERROR', stage: 'receive' });
    });

    it('a protocol or authentication rejection reaches terminal recovery with its close code', () => {
        const { sockets, callbacks, logs } = setup();
        sockets[0]!.onclose?.({ code: 1008, wasClean: true } as CloseEvent);
        expect(callbacks.on_failure).toHaveBeenCalledOnce();
        expect(logs.find((record) => record.event === 'first_failure')).toMatchObject({ error_code: 'EVENT_STREAM_REJECTED', close_code: 1008 });
        expect(vi.getTimerCount()).toBe(0);
    });

    it('distinguishes invalid JSON decoding from payload validation without recording the body', async () => {
        const { sockets, logs } = setup(async () => new Response('SECRET_CANARY', { status: 200 }));
        sockets[0]!.disconnect(); await vi.advanceTimersByTimeAsync(0);
        expect(logs.some((record) => record.stage === 'decode' && record.error_type === 'SyntaxError' && record.http_status === 200)).toBe(true);
        expect(JSON.stringify(logs)).not.toContain('SECRET_CANARY');
    });

    it('a UI publication bug is terminal and keeps its original exception type', async () => {
        const { sockets, callbacks, logs } = setup();
        callbacks.on_full_resync = () => { throw new TypeError('SECRET_CANARY'); };
        sockets[0]!.disconnect(); await vi.advanceTimersByTimeAsync(0);
        expect(callbacks.on_failure).toHaveBeenCalledWith(expect.objectContaining({ code: 'UI_STATE_PUBLICATION_FAILED', retryable: false }));
        expect(logs.some((record) => record.stage === 'publish' && record.error_type === 'TypeError' && record.retryable === false)).toBe(true);
        expect(vi.getTimerCount()).toBe(0);
        expect(JSON.stringify(logs)).not.toContain('SECRET_CANARY');
    });

});
