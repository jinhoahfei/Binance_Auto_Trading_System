import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { BACKEND_SCHEMA_VERSION } from './shared/contracts';
import {
    create_backend_snapshot_fixture,
    TEST_BACKEND_SESSION_ID,
    TEST_BACKEND_TOKEN,
} from './shared/api/backendTestFixtures';

const TEST_REQUEST_ID = 'f5a4f621-25f8-4dd2-bfb7-1b80e9561423';

/**
 * native event mock이 보존하는 renderer callback의 최소 계약이다.
 */
type TestNativeListener = (event: { readonly payload: unknown }) => void;

const {
    destroy_window_mock,
    invoke_mock,
    listen_mock,
    native_listeners,
    native_unlisteners,
} = vi.hoisted(() => ({
    destroy_window_mock: vi.fn<() => Promise<void>>(),
    invoke_mock: vi.fn<(command: string) => Promise<unknown>>(),
    listen_mock: vi.fn<(
        event_name: string,
        listener: TestNativeListener,
    ) => Promise<() => void>>(),
    native_listeners: new Map<string, TestNativeListener>(),
    native_unlisteners: new Map<string, ReturnType<typeof vi.fn>>(),
}));

vi.mock('@tauri-apps/api/core', () => ({
    invoke: invoke_mock,
}));

vi.mock('@tauri-apps/api/event', () => ({
    listen: listen_mock,
}));

vi.mock('@tauri-apps/api/window', () => ({
    getCurrentWindow: () => ({ destroy: destroy_window_mock }),
}));

/**
 * 함수 이름: create_live_descriptor()
 * 기능: production main bootstrap test에 전달할 valid one-shot descriptor를 만든다.
 * 인자: 없음
 * 반환값: token을 포함한 loopback descriptor
 * 작성 날짜: 2026/08/24
 */
function create_live_descriptor() {
    return {
        port: 42_123,
        schema_version: BACKEND_SCHEMA_VERSION,
        session_id: TEST_BACKEND_SESSION_ID,
        token: TEST_BACKEND_TOKEN,
    } as const;
}

/**
 * 함수 이름: create_success_response()
 * 기능: adapter가 보낸 request ID를 반사하는 strict success envelope를 만든다.
 * 인자: request_id -> X-Request-Id, data -> endpoint별 response data, status -> HTTP status
 * 반환값: JSON Response
 * 작성 날짜: 2026/08/24
 */
function create_success_response(
    request_id: string,
    data: unknown,
    status = 200,
): Response {
    return new Response(JSON.stringify({
        schema_version: BACKEND_SCHEMA_VERSION,
        request_id,
        ok: true,
        data,
    }), { status });
}

/**
 * 함수 이름: request_headers()
 * 기능: mocked fetch 요청의 인증 header를 browser-independent record로 읽는다.
 * 인자: init -> fetch RequestInit
 * 반환값: 검사할 header record
 * 작성 날짜: 2026/08/24
 */
function request_headers(init?: RequestInit): Readonly<{
    authorization: string | null;
    idempotency_key: string | null;
    request_id: string | null;
}> {
    const headers = new Headers(init?.headers);

    return {
        authorization: headers.get('Authorization'),
        idempotency_key: headers.get('Idempotency-Key'),
        request_id: headers.get('X-Request-Id'),
    };
}

describe('production main bootstrap recovery', () => {
    beforeEach(() => {
        vi.resetModules();
        vi.clearAllMocks();
        native_listeners.clear();
        native_unlisteners.clear();
        document.body.innerHTML = '<div id="root"></div>';
        Object.defineProperty(window, '__TAURI_INTERNALS__', {
            configurable: true,
            value: {},
        });
        vi.spyOn(globalThis.crypto, 'randomUUID').mockReturnValue(TEST_REQUEST_ID);

        // Listener removal 여부를 event별로 독립 관찰한다.
        listen_mock.mockImplementation(async (event_name, listener) => {
            const unlisten = vi.fn();

            native_listeners.set(event_name, listener);
            native_unlisteners.set(event_name, unlisten);
            return unlisten;
        });
        destroy_window_mock.mockResolvedValue(undefined);
        invoke_mock.mockImplementation(async (command) => {
            if (command === 'arm_sidecar_exit_event_bridge'
                || command === 'arm_native_exit_intent_bridge') {
                return { armed: true };
            }
            if (command === 'get_backend_connection_descriptor') {
                return create_live_descriptor();
            }
            if (command === 'await_backend_sidecar_exit') {
                return { exited: true, code: 0 };
            }

            throw new Error(`Unexpected native command: ${command}`);
        });
    });

    afterEach(() => {
        vi.restoreAllMocks();
        vi.unstubAllGlobals();
        Reflect.deleteProperty(window, '__TAURI_INTERNALS__');
        document.body.innerHTML = '';
    });

    it.each(['연결 다시 확인', '안전 종료'] as const)('연결 정보가 없던 화면의 %s 버튼도 native 연결을 복구한다', async (action_name) => {
        const original_invoke = invoke_mock.getMockImplementation()!;
        let descriptor_attempts = 0;
        invoke_mock.mockImplementation(async (command) => {
            if (command === 'get_backend_connection_descriptor' && ++descriptor_attempts === 1) {
                throw { code: 'BACKEND_DESCRIPTOR_UNAVAILABLE' };
            }
            return original_invoke(command);
        });
        const snapshot = create_backend_snapshot_fixture();
        const requested_paths: string[] = [];
        vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
            const request_id = request_headers(init).request_id!;
            const path = new URL(input.toString()).pathname;
            requested_paths.push(path);
            if (path === '/v1/snapshot') {
                // 연결 복구 성공 이후의 snapshot 재시도를 actor 실행과 분리해 관찰한다.
                return create_success_response(request_id, {
                    ...snapshot, connection: { ...snapshot.connection, ready: false },
                });
            }
            if (path === '/v1/shutdown/state') {
                return create_success_response(request_id, {
                    session_id: TEST_BACKEND_SESSION_ID, version: 0, status: 'not_started',
                });
            }
            if (path === '/v1/shutdown') {
                return create_success_response(request_id, { accepted: true, status: 'accepted', version: 0 }, 202);
            }
            throw new Error('Unexpected recovery request');
        }));

        await act(async () => { await import('./main'); });
        expect(await screen.findByText('BACKEND_DESCRIPTOR_UNAVAILABLE')).toBeInTheDocument();
        expect(screen.getByText(/현재 화면에 백엔드 연결 정보가 없습니다/u)).toBeInTheDocument();
        expect(screen.getByRole('button', { name: '연결 다시 확인' })).toBeEnabled();
        expect(screen.getByRole('button', { name: '안전 종료' })).toBeEnabled();
        fireEvent.click(screen.getByRole('button', { name: action_name }));
        if (action_name === '연결 다시 확인') {
            expect(await screen.findByText('BACKEND_NOT_READY')).toBeInTheDocument();
            fireEvent.click(screen.getByRole('button', { name: '안전 종료' }));
        }

        // 두 action 모두 같은 adapter를 보존해 202와 native code 0 이후에만 창을 닫는다.
        await waitFor(() => expect(destroy_window_mock).toHaveBeenCalledOnce());
        expect(descriptor_attempts).toBe(2);
        expect(requested_paths).toEqual([
            ...(action_name === '연결 다시 확인' ? ['/v1/snapshot'] : []),
            '/v1/shutdown/state', '/v1/shutdown',
        ]);
        expect(document.body).not.toHaveTextContent(TEST_BACKEND_TOKEN);
    });

    it('새 화면에서 backend 종료를 확인하면 동작하지 않는 복구 버튼 대신 창 닫기를 제공한다', async () => {
        const original_invoke = invoke_mock.getMockImplementation()!;
        invoke_mock.mockImplementation(async (command) => {
            if (command === 'get_backend_connection_descriptor') {
                throw { code: 'BACKEND_SIDECAR_EXITED' };
            }
            return original_invoke(command);
        });
        await act(async () => { await import('./main'); });
        fireEvent.click(await screen.findByRole('button', { name: '창 닫기' }));
        await waitFor(() => expect(destroy_window_mock).toHaveBeenCalledOnce());
        expect(screen.queryByRole('button', { name: '연결 다시 확인' })).not.toBeInTheDocument();
    });

    it('전체 snapshot이 계속 malformed여도 안전 종료는 별도 상태를 읽고 실제 종료 확인까지 진행한다', async () => {
        const snapshot = create_backend_snapshot_fixture();
        const malformed_snapshot = {
            ...snapshot,
            regime: { ...snapshot.regime, indicator: { ...snapshot.regime.indicator, current_price: 'invalid' } },
        };
        const paths: string[] = [];
        vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
            const request_id = request_headers(init).request_id!;
            const path = new URL(input.toString()).pathname;
            paths.push(path);
            if (path === '/v1/snapshot') {
                return create_success_response(request_id, malformed_snapshot);
            }
            if (path === '/v1/shutdown/state') {
                return create_success_response(request_id, {
                    session_id: TEST_BACKEND_SESSION_ID, version: 0, status: 'not_started',
                });
            }
            if (path === '/v1/shutdown') {
                return create_success_response(request_id, {
                    accepted: true, status: 'accepted', version: 0,
                }, 202);
            }
            throw new Error('Unexpected recovery request');
        }));

        await act(async () => { await import('./main'); });
        expect(await screen.findByText('MALFORMED_BACKEND_PAYLOAD')).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: '안전 종료' }));

        await waitFor(() => expect(destroy_window_mock).toHaveBeenCalledOnce());
        expect(paths).toEqual(['/v1/snapshot', '/v1/shutdown/state', '/v1/shutdown']);
        expect(invoke_mock).toHaveBeenCalledWith('await_backend_sidecar_exit');
    });

    it.each([
        ['window', 'window close'],
        ['application', 'Command-Q'],
    ] as const)(
        'READY child snapshot failure 뒤 %s native intent도 같은 token으로 안전 종료한다',
        async (native_source, _native_label) => {
            const ready_snapshot = create_backend_snapshot_fixture();
            const not_ready_snapshot = {
                ...ready_snapshot,
                connection: { ...ready_snapshot.connection, ready: false },
            };
            let snapshot_attempt = 0;
            let shutdown_attempt = 0;
            const fetch_mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
                const request_id = request_headers(init).request_id;
                const path = new URL(input.toString()).pathname;

                if (request_id === null) {
                    throw new Error('Missing request ID');
                }
                if (path === '/v1/snapshot') {
                    snapshot_attempt += 1;
                    return create_success_response(
                        request_id,
                        snapshot_attempt === 1 ? not_ready_snapshot : ready_snapshot,
                    );
                }
                if (path === '/v1/shutdown') {
                    shutdown_attempt += 1;
                    if (shutdown_attempt === 1) {
                        throw new TypeError('Simulated response loss after shutdown send');
                    }
                    return create_success_response(request_id, {
                        accepted: true,
                        status: 'accepted',
                        version: 0,
                    }, 202);
                }
                if (path === '/v1/shutdown/state') {
                    return create_success_response(request_id, {
                        session_id: TEST_BACKEND_SESSION_ID, version: 0, status: 'not_started',
                    });
                }

                throw new Error(`Unexpected backend path: ${path}`);
            });
            vi.stubGlobal('fetch', fetch_mock);

            // main side effect는 첫 malformed-ready snapshot을 typed recovery 화면으로 전환한다.
            await import('./main');
            expect(await screen.findByText('백엔드 연결 복구가 필요합니다.')).toBeInTheDocument();
            expect(screen.getByText('BACKEND_NOT_READY')).toBeInTheDocument();
            expect(invoke_mock).toHaveBeenCalledWith('get_backend_connection_descriptor');
            expect(native_unlisteners.get('backend-sidecar-exited')).not.toHaveBeenCalled();
            expect(native_unlisteners.get('native-exit-requested')).not.toHaveBeenCalled();

            const native_exit_listener = native_listeners.get('native-exit-requested');
            if (native_exit_listener === undefined) {
                throw new Error('Native exit listener was not installed');
            }

            // Window close와 Command-Q 모두 native trap을 유지하며 operator 안전 종료를 요구한다.
            act(() => {
                native_exit_listener({ payload: { source: native_source } });
                if (native_source === 'application') {
                    // 연속 application-source native event도 즉시 destroy나 별도 workflow를 만들지 않는다.
                    native_exit_listener({ payload: { source: native_source } });
                }
            });
            expect(screen.getByText(
                '창 닫기 또는 Command-Q 요청을 감지했습니다. 아래 안전 종료를 확인해 주세요.',
            )).toBeInTheDocument();
            expect(screen.getAllByRole('button', { name: '안전 종료' })).toHaveLength(1);
            expect(destroy_window_mock).not.toHaveBeenCalled();
            fireEvent.click(screen.getByRole('button', { name: '안전 종료' }));

            // 첫 response loss는 child를 강제 종료하지 않고 같은 command 재시도만 허용한다.
            expect(await screen.findByText('SHUTDOWN_OUTCOME_AMBIGUOUS')).toBeInTheDocument();
            expect(destroy_window_mock).not.toHaveBeenCalled();
            fireEvent.click(screen.getByRole('button', { name: '안전 종료' }));

            // 같은 descriptor/token으로 snapshot을 복구하고 202 및 native code 0 뒤에만 창을 닫는다.
            await waitFor(() => expect(destroy_window_mock).toHaveBeenCalledOnce());
            expect(invoke_mock.mock.calls.filter(
                ([command]) => command === 'get_backend_connection_descriptor',
            )).toHaveLength(1);
            expect(invoke_mock).toHaveBeenCalledWith('await_backend_sidecar_exit');
            expect(fetch_mock).toHaveBeenCalledTimes(4);
            expect(fetch_mock.mock.calls.map(([input]) => new URL(input.toString()).pathname)).toEqual([
                '/v1/snapshot',
                '/v1/shutdown/state',
                '/v1/shutdown',
                '/v1/shutdown',
            ]);
            fetch_mock.mock.calls.forEach(([, init]) => {
                expect(request_headers(init).authorization).toBe(
                    `Bearer ${TEST_BACKEND_TOKEN}`,
                );
            });
            const first_shutdown_init = fetch_mock.mock.calls[2]?.[1];
            const retried_shutdown_init = fetch_mock.mock.calls[3]?.[1];

            expect(first_shutdown_init?.body).toBe(JSON.stringify({
                schema_version: BACKEND_SCHEMA_VERSION,
                expected_version: 0,
            }));
            expect(retried_shutdown_init?.body).toBe(first_shutdown_init?.body);
            expect(request_headers(first_shutdown_init).idempotency_key).toBe(TEST_REQUEST_ID);
            expect(request_headers(retried_shutdown_init).idempotency_key).toBe(TEST_REQUEST_ID);
            expect(listen_mock).toHaveBeenCalledTimes(2);
            expect(native_unlisteners.get('backend-sidecar-exited')).toHaveBeenCalledOnce();
            expect(native_unlisteners.get('native-exit-requested')).toHaveBeenCalledOnce();
            expect(document.body).not.toHaveTextContent(TEST_BACKEND_TOKEN);
        },
    );
});
