import { waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { BACKEND_SCHEMA_VERSION } from '../contracts';
import type { BackendUiAdapterCallbacks, BackendWebSocket } from './BackendUiAdapter';
import {
    BackendAdapterError,
    BackendUiAdapter,
} from './BackendUiAdapter';
import { BackendCommandError } from './backendEventMapper';
import {
    create_backend_account_fixture,
    create_backend_event_fixture,
    create_backend_snapshot_fixture,
    TEST_BACKEND_EVENT_ID,
    TEST_BACKEND_SESSION_ID,
    TEST_BACKEND_TOKEN,
} from './backendTestFixtures';

const TEST_REQUEST_ID = 'f5a4f621-25f8-4dd2-bfb7-1b80e9561423';
const TEST_IDEMPOTENCY_ID = '2522ef0c-d88d-42b3-a22f-fc7bdd09a662';
const SECOND_EVENT_ID = '06a59589-0aed-44fa-8983-7246aeb4c619';
const THIRD_EVENT_ID = 'c9ca6590-9d50-436d-8648-e8cc9ef7957f';

/**
 * 클래스 이름: FakeBackendWebSocket
 * 기능: adapter test에서 first frame, server event와 close lifecycle을 수동 제어한다.
 * 작성 날짜: 2026/08/21
 */
class FakeBackendWebSocket implements BackendWebSocket {
    readonly sent_frames: Array<string> = [];
    readonly close_calls: Array<{ readonly code?: number; readonly reason?: string }> = [];
    onopen: ((event: Event) => void) | null = null;
    onmessage: ((event: MessageEvent<unknown>) => void) | null = null;
    onclose: ((event: CloseEvent) => void) | null = null;
    onerror: ((event: Event) => void) | null = null;

    constructor(readonly url: string) {}

    /**
     * 함수 이름: send()
     * 기능: adapter가 보낸 client text frame을 순서대로 기록한다.
     * 인자: data -> WebSocket text frame
     * 반환값: 없음
     * 작성 날짜: 2026/08/21
     */
    send(data: string): void {
        this.sent_frames.push(data);
    }

    /**
     * 함수 이름: close()
     * 기능: adapter의 secret 없는 close code와 reason을 기록한다.
     * 인자: code -> close code, reason -> close reason
     * 반환값: 없음
     * 작성 날짜: 2026/08/21
     */
    close(code?: number, reason?: string): void {
        this.close_calls.push({
            ...(code === undefined ? {} : { code }),
            ...(reason === undefined ? {} : { reason }),
        });
    }

    /**
     * 함수 이름: emit_open()
     * 기능: browser open event를 adapter handler에 전달한다.
     * 인자: 없음
     * 반환값: 없음
     * 작성 날짜: 2026/08/21
     */
    emit_open(): void {
        this.onopen?.(new Event('open'));
    }

    /**
     * 함수 이름: emit_message()
     * 기능: JSON object를 server text frame으로 adapter handler에 전달한다.
     * 인자: payload -> JSON 직렬화할 event/control object
     * 반환값: 없음
     * 작성 날짜: 2026/08/21
     */
    emit_message(payload: object): void {
        this.onmessage?.(new MessageEvent('message', {
            data: JSON.stringify(payload),
        }));
    }

    /**
     * 함수 이름: emit_close()
     * 기능: unexpected browser close event를 adapter handler에 전달한다.
     * 인자: 없음
     * 반환값: 없음
     * 작성 날짜: 2026/08/21
     */
    emit_close(): void {
        this.onclose?.(new CloseEvent('close'));
    }
}

/**
 * 함수 이름: create_descriptor()
 * 기능: 모든 adapter test가 공유할 valid loopback launch descriptor를 만든다.
 * 인자: 없음
 * 반환값: valid descriptor object
 * 작성 날짜: 2026/08/21
 */
function create_descriptor() {
    return {
        port: 42_123,
        session_id: TEST_BACKEND_SESSION_ID,
        schema_version: BACKEND_SCHEMA_VERSION,
        token: TEST_BACKEND_TOKEN,
    } as const;
}

/**
 * 함수 이름: create_uuid_factory()
 * 기능: HTTP request와 idempotency header에 쓸 결정적 UUID sequence를 반환한다.
 * 인자: 없음
 * 반환값: UUID factory
 * 작성 날짜: 2026/08/21
 */
function create_uuid_factory(): () => string {
    const ids = [TEST_REQUEST_ID, TEST_IDEMPOTENCY_ID];
    let index = 0;

    return () => {
        const next_id = ids[index % ids.length];
        index += 1;
        return next_id!;
    };
}

/**
 * 함수 이름: create_success_response()
 * 기능: 요청 header UUID와 동일한 backend success envelope Response를 만든다.
 * 인자: request_id -> X-Request-Id 값, data -> endpoint success data
 * 반환값: JSON Response
 * 작성 날짜: 2026/08/21
 */
function create_success_response(request_id: string, data: unknown): Response {
    return new Response(JSON.stringify({
        schema_version: BACKEND_SCHEMA_VERSION,
        request_id,
        ok: true,
        data,
    }), { status: 200 });
}

/**
 * 함수 이름: create_failure_response()
 * 기능: Phase 5 unavailable command와 같은 typed failure envelope Response를 만든다.
 * 인자: request_id -> X-Request-Id 값
 * 반환값: JSON Response
 * 작성 날짜: 2026/08/21
 */
function create_failure_response(request_id: string): Response {
    return new Response(JSON.stringify({
        schema_version: BACKEND_SCHEMA_VERSION,
        request_id,
        ok: false,
        error: {
            code: 'FEATURE_NOT_AVAILABLE',
            message: 'This feature is not available in the current application phase.',
            retryable: false,
            details: {},
        },
    }), { status: 503 });
}

/**
 * 함수 이름: request_headers()
 * 기능: fake fetch RequestInit에서 string record header를 안전하게 추출한다.
 * 인자: init -> fetch가 받은 RequestInit
 * 반환값: header record
 * 작성 날짜: 2026/08/21
 */
function request_headers(init: RequestInit | undefined): Record<string, string> {
    return init?.headers as Record<string, string>;
}

/**
 * 함수 이름: create_callbacks()
 * 기능: event/resync/reconnect/failure 호출을 관찰할 기본 mock callbacks를 만든다.
 * 인자: 없음
 * 반환값: typed callback mock 묶음
 * 작성 날짜: 2026/08/21
 */
function create_callbacks(): BackendUiAdapterCallbacks {
    return {
        on_event: vi.fn(),
        on_full_resync: vi.fn(),
        on_reconnecting: vi.fn(),
        on_failure: vi.fn(),
    };
}

describe('BackendUiAdapter HTTP contract', () => {
    it('Bearer/X-Request-Id와 command Idempotency-Key를 보내고 typed failure를 보존한다', async () => {
        const snapshot = create_backend_snapshot_fixture();
        const fetch_mock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
            const headers = request_headers(init);

            return init?.method === 'GET'
                ? create_success_response(headers['X-Request-Id']!, snapshot)
                : create_failure_response(headers['X-Request-Id']!);
        });
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: fetch_mock as typeof fetch,
            create_uuid: create_uuid_factory(),
        });

        await expect(adapter.load_snapshot()).resolves.toEqual(snapshot);
        await expect(adapter.apply_regime('type2')).rejects.toBeInstanceOf(
            BackendCommandError,
        );

        const snapshot_call = fetch_mock.mock.calls[0];
        const command_call = fetch_mock.mock.calls[1];
        const snapshot_headers = request_headers(snapshot_call?.[1]);
        const command_headers = request_headers(command_call?.[1]);

        expect(snapshot_call?.[0]).toBe('http://127.0.0.1:42123/v1/snapshot');
        expect(snapshot_headers.Authorization).toBe(`Bearer ${TEST_BACKEND_TOKEN}`);
        expect(snapshot_headers['X-Request-Id']).toBe(TEST_REQUEST_ID);
        expect(snapshot_headers['Idempotency-Key']).toBeUndefined();
        expect(command_call?.[0]).toBe('http://127.0.0.1:42123/v1/regime/selection');
        expect(command_headers.Authorization).toBe(`Bearer ${TEST_BACKEND_TOKEN}`);
        expect(command_headers['Idempotency-Key']).toBe(TEST_REQUEST_ID);
        expect(command_call?.[1]?.body).toBe(JSON.stringify({ schema_version: 1 }));
    });

    it('shutdown typed failure 뒤에는 token을 유지하고 명시 stop에서만 제거한다', async () => {
        const fetch_mock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
            return create_failure_response(request_headers(init)['X-Request-Id']!);
        }) as typeof fetch;
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: fetch_mock,
            create_uuid: create_uuid_factory(),
        });

        await expect(adapter.shutdown_application()).rejects.toMatchObject({
            code: 'FEATURE_NOT_AVAILABLE',
        });
        await expect(adapter.shutdown_application()).rejects.toMatchObject({
            code: 'FEATURE_NOT_AVAILABLE',
        });

        adapter.stop();
        await expect(adapter.load_snapshot()).rejects.toBeInstanceOf(BackendAdapterError);
        expect(JSON.stringify(adapter)).not.toContain(TEST_BACKEND_TOKEN);
    });
});

describe('BackendUiAdapter WebSocket lifecycle', () => {
    it('WebSocket constructor 동기 실패를 밖으로 던지지 않고 token과 online lifecycle을 닫는다', async () => {
        const callbacks = create_callbacks();
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: vi.fn() as unknown as typeof fetch,
            create_uuid: create_uuid_factory(),
            create_web_socket: () => {
                throw new Error('constructor failure must not escape');
            },
        });

        // React activation 경계에는 예외를 던지지 않고 facade용 typed failure만 전달한다.
        expect(() => {
            adapter.start_live_events(create_backend_snapshot_fixture(), callbacks);
        }).not.toThrow();
        expect(callbacks.on_failure).toHaveBeenCalledWith(expect.objectContaining({
            code: 'EVENT_STREAM_CONNECTION_FAILED',
            retryable: true,
        }));
        await expect(adapter.load_snapshot()).rejects.toMatchObject({
            code: 'ADAPTER_STOPPED',
        });
        expect(JSON.stringify(adapter)).not.toContain(TEST_BACKEND_TOKEN);
    });

    it('URL/subprotocol 없이 연결하고 onopen의 첫 AUTHENTICATE frame에만 token을 넣는다', () => {
        const sockets: Array<FakeBackendWebSocket> = [];
        const snapshot = create_backend_snapshot_fixture();
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: vi.fn() as unknown as typeof fetch,
            create_uuid: create_uuid_factory(),
            create_web_socket: (url) => {
                const socket = new FakeBackendWebSocket(url);
                sockets.push(socket);
                return socket;
            },
        });

        adapter.start_live_events(snapshot, create_callbacks());
        const socket = sockets[0]!;
        expect(socket.url).toBe('ws://127.0.0.1:42123/v1/events');
        expect(socket.url).not.toContain(TEST_BACKEND_TOKEN);
        expect(socket.sent_frames).toEqual([]);

        socket.emit_open();
        expect(socket.sent_frames).toHaveLength(1);
        expect(JSON.parse(socket.sent_frames[0]!) as unknown).toEqual({
            schema_version: 1,
            type: 'AUTHENTICATE',
            token: TEST_BACKEND_TOKEN,
            after_sequence: 9,
        });
        expect(JSON.stringify(adapter)).not.toContain(TEST_BACKEND_TOKEN);

        adapter.stop();
        expect(socket.close_calls).toEqual([{ code: 1000, reason: 'client stop' }]);
    });

    it('duplicate/older sequence를 적용하지 않고 unknown type sequence 뒤 exact-next를 적용한다', () => {
        const sockets: Array<FakeBackendWebSocket> = [];
        const callbacks = create_callbacks();
        const snapshot = create_backend_snapshot_fixture();
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: vi.fn() as unknown as typeof fetch,
            create_uuid: create_uuid_factory(),
            create_web_socket: (url) => {
                const socket = new FakeBackendWebSocket(url);
                sockets.push(socket);
                return socket;
            },
        });

        adapter.start_live_events(snapshot, callbacks);
        const socket = sockets[0]!;
        const account_event = create_backend_event_fixture(10, 'ACCOUNT_UPDATED', {
            account: create_backend_account_fixture(),
        });
        socket.emit_message(account_event);
        socket.emit_message(account_event);
        socket.emit_message({ ...account_event, sequence: 8 });
        socket.emit_message({ ...account_event, sequence: 11 });
        socket.emit_message(create_backend_event_fixture(
            12,
            'FUTURE_EVENT',
            {},
            SECOND_EVENT_ID,
        ));
        socket.emit_message(create_backend_event_fixture(
            13,
            'ACCOUNT_UPDATED',
            { account: create_backend_account_fixture() },
            THIRD_EVENT_ID,
        ));

        expect(callbacks.on_event).toHaveBeenCalledTimes(3);
        expect(callbacks.on_event).toHaveBeenNthCalledWith(
            2,
            [],
            expect.objectContaining({ type: 'FUTURE_EVENT', sequence: 12 }),
        );
        expect(callbacks.on_reconnecting).not.toHaveBeenCalled();
        adapter.stop();
    });

    it('sequence gap에서 새 full snapshot을 먼저 적용한 뒤 그 last_sequence로 재연결한다', async () => {
        const sockets: Array<FakeBackendWebSocket> = [];
        const callbacks = create_callbacks();
        const resync_snapshot = {
            ...create_backend_snapshot_fixture(),
            last_sequence: 20,
        };
        const fetch_mock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
            return create_success_response(
                request_headers(init)['X-Request-Id']!,
                resync_snapshot,
            );
        }) as typeof fetch;
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: fetch_mock,
            create_uuid: create_uuid_factory(),
            create_web_socket: (url) => {
                const socket = new FakeBackendWebSocket(url);
                sockets.push(socket);
                return socket;
            },
        });

        adapter.start_live_events(create_backend_snapshot_fixture(), callbacks);
        sockets[0]!.emit_message(create_backend_event_fixture(
            11,
            'ACCOUNT_UPDATED',
            { account: create_backend_account_fixture() },
        ));

        await waitFor(() => {
            expect(callbacks.on_full_resync).toHaveBeenCalledWith(resync_snapshot);
            expect(sockets).toHaveLength(2);
        });
        sockets[1]!.emit_open();
        const authentication = JSON.parse(sockets[1]!.sent_frames[0]!) as {
            readonly after_sequence: number;
        };
        expect(authentication.after_sequence).toBe(20);
        expect(callbacks.on_reconnecting).toHaveBeenCalledWith('SEQUENCE_GAP');
        adapter.stop();
    });

    it('RESYNC_REQUIRED와 session change에서 cache를 잇지 않고 descriptor session만 다시 받는다', async () => {
        const sockets: Array<FakeBackendWebSocket> = [];
        const callbacks = create_callbacks();
        const snapshot = create_backend_snapshot_fixture();
        const fetch_mock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
            return create_success_response(request_headers(init)['X-Request-Id']!, snapshot);
        }) as typeof fetch;
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: fetch_mock,
            create_uuid: create_uuid_factory(),
            create_web_socket: (url) => {
                const socket = new FakeBackendWebSocket(url);
                sockets.push(socket);
                return socket;
            },
        });

        adapter.start_live_events(snapshot, callbacks);
        sockets[0]!.emit_message({
            schema_version: 1,
            session_id: TEST_BACKEND_SESSION_ID,
            type: 'RESYNC_REQUIRED',
            reason: 'REPLAY_GAP',
            last_sequence: 12,
        });

        await waitFor(() => expect(sockets).toHaveLength(2));
        sockets[1]!.emit_message(create_backend_event_fixture(
            10,
            'ACCOUNT_UPDATED',
            { account: create_backend_account_fixture() },
            SECOND_EVENT_ID,
            '3fcb584c-0d34-4839-908d-4f3ef8f83442',
        ));

        await waitFor(() => expect(sockets).toHaveLength(3));
        expect(callbacks.on_full_resync).toHaveBeenCalledTimes(2);
        expect(callbacks.on_reconnecting).toHaveBeenNthCalledWith(1, 'REPLAY_GAP');
        expect(callbacks.on_reconnecting).toHaveBeenNthCalledWith(2, 'SESSION_CHANGED');
        adapter.stop();
    });

    it('full resync snapshot이 launch descriptor session과 다르면 적용 없이 fail closed한다', async () => {
        const sockets: Array<FakeBackendWebSocket> = [];
        const callbacks = create_callbacks();
        const changed_session_snapshot = {
            ...create_backend_snapshot_fixture(),
            session_id: '3fcb584c-0d34-4839-908d-4f3ef8f83442',
        };
        const fetch_mock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
            return create_success_response(
                request_headers(init)['X-Request-Id']!,
                changed_session_snapshot,
            );
        }) as typeof fetch;
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: fetch_mock,
            create_uuid: create_uuid_factory(),
            create_web_socket: (url) => {
                const socket = new FakeBackendWebSocket(url);
                sockets.push(socket);
                return socket;
            },
        });

        adapter.start_live_events(create_backend_snapshot_fixture(), callbacks);
        sockets[0]!.emit_message(create_backend_event_fixture(
            11,
            'ACCOUNT_UPDATED',
            { account: create_backend_account_fixture() },
        ));

        await waitFor(() => {
            expect(callbacks.on_failure).toHaveBeenCalledWith(expect.objectContaining({
                code: 'SESSION_MISMATCH',
            }));
        });
        expect(callbacks.on_full_resync).not.toHaveBeenCalled();
        expect(sockets).toHaveLength(1);
        await expect(adapter.load_snapshot()).rejects.toMatchObject({ code: 'ADAPTER_STOPPED' });
    });

    it('unknown schema frame은 fail closed하고 token 참조와 socket을 제거한다', async () => {
        const sockets: Array<FakeBackendWebSocket> = [];
        const callbacks = create_callbacks();
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: vi.fn() as unknown as typeof fetch,
            create_uuid: create_uuid_factory(),
            create_web_socket: (url) => {
                const socket = new FakeBackendWebSocket(url);
                sockets.push(socket);
                return socket;
            },
        });

        adapter.start_live_events(create_backend_snapshot_fixture(), callbacks);
        sockets[0]!.emit_message({
            ...create_backend_event_fixture(10, 'FUTURE_EVENT', {}),
            schema_version: 2,
        });

        expect(callbacks.on_failure).toHaveBeenCalledWith(expect.objectContaining({
            code: 'UNSUPPORTED_SCHEMA_VERSION',
        }));
        expect(sockets[0]!.close_calls).toEqual([{ code: 1000, reason: 'fail closed' }]);
        await expect(adapter.load_snapshot()).rejects.toMatchObject({ code: 'ADAPTER_STOPPED' });
        expect(JSON.stringify(adapter)).not.toContain(TEST_BACKEND_TOKEN);
    });
});
