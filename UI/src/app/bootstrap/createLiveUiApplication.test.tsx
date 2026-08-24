import { StrictMode } from 'react';
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { App } from '../App';
import { BACKEND_SCHEMA_VERSION } from '../../shared/contracts';
import type { BackendWebSocket } from '../../shared/api';
import {
    create_backend_snapshot_fixture,
    TEST_BACKEND_SESSION_ID,
    TEST_BACKEND_TOKEN,
} from '../../shared/api/backendTestFixtures';
import {
    create_live_ui_application,
    create_live_ui_application_factory,
} from './createLiveUiApplication';

const TEST_REQUEST_ID = 'f5a4f621-25f8-4dd2-bfb7-1b80e9561423';

/**
 * 클래스 이름: BootstrapFakeWebSocket
 * 기능: live App render test가 실제 network 없이 event connection 생성을 관찰한다.
 * 작성 날짜: 2026/08/21
 */
class BootstrapFakeWebSocket implements BackendWebSocket {
    onopen: ((event: Event) => void) | null = null;
    onmessage: ((event: MessageEvent<unknown>) => void) | null = null;
    onclose: ((event: CloseEvent) => void) | null = null;
    onerror: ((event: Event) => void) | null = null;

    /**
     * 함수 이름: send()
     * 기능: 이 test에서는 open하지 않는 socket의 required send 계약만 제공한다.
     * 인자: data -> 전송할 text frame
     * 반환값: 없음
     * 작성 날짜: 2026/08/21
     */
    send(data: string): void {
        void data;
    }

    /**
     * 함수 이름: close()
     * 기능: component unmount의 socket close 계약을 side effect 없이 수용한다.
     * 인자: code -> close code, reason -> safe close reason
     * 반환값: 없음
     * 작성 날짜: 2026/08/21
     */
    close(code?: number, reason?: string): void {
        void code;
        void reason;
    }
}

/**
 * 함수 이름: create_descriptor()
 * 기능: live bootstrap component test용 valid descriptor를 만든다.
 * 인자: 없음
 * 반환값: launch descriptor
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
 * 함수 이름: create_snapshot_fetch()
 * 기능: X-Request-Id를 반사하는 valid snapshot success envelope fetch를 만든다.
 * 인자: snapshot -> response data
 * 반환값: fake fetch
 * 작성 날짜: 2026/08/21
 */
function create_snapshot_fetch(snapshot: unknown): typeof fetch {
    return (async (_input: RequestInfo | URL, init?: RequestInit) => {
        const headers = init?.headers as Record<string, string>;

        return new Response(JSON.stringify({
            schema_version: BACKEND_SCHEMA_VERSION,
            request_id: headers['X-Request-Id'],
            ok: true,
            data: snapshot,
        }), { status: 200 });
    }) as typeof fetch;
}

describe('create_live_ui_application', () => {
    it('snapshot을 먼저 받은 뒤 StrictMode App에 실제 USDT 상태를 render한다', async () => {
        const lifecycle_order: Array<string> = [];
        const snapshot = create_backend_snapshot_fixture();
        const snapshot_fetch = create_snapshot_fetch(snapshot);
        const application = await create_live_ui_application(create_descriptor(), {
            today: '2026-08-21',
            adapter_dependencies: {
                fetch: async (input, init) => {
                    lifecycle_order.push('snapshot');
                    return snapshot_fetch(input, init);
                },
                create_uuid: () => TEST_REQUEST_ID,
                create_web_socket: () => {
                    lifecycle_order.push('web_socket');
                    return new BootstrapFakeWebSocket();
                },
            },
        });

        expect(lifecycle_order).toEqual(['snapshot']);
        const application_factory = create_live_ui_application_factory(application);
        const rendered = render(
            <StrictMode>
                <App applicationFactory={application_factory} />
            </StrictMode>,
        );

        expect(await screen.findByText('LIVE')).toBeInTheDocument();
        expect(lifecycle_order).toEqual(['snapshot', 'web_socket']);
        expect(await screen.findByRole('button', { name: 'type2 강상승 적용 요청' })).toHaveAttribute(
            'aria-pressed',
            'false',
        );
        expect(await screen.findByRole('article', { name: '보유 자산' })).toHaveTextContent(
            '120.00 USDT',
        );
        const strategy_card = screen.getByRole('article', { name: '전략 상태' });

        // 누적 Performance는 현재 REGIME 상태별 수익으로 오인되지 않게 unavailable로 표시한다.
        expect(strategy_card).toHaveTextContent('(-)');
        expect(strategy_card).not.toHaveTextContent('-0.40 USDT');
        expect(document.body).not.toHaveTextContent(TEST_BACKEND_TOKEN);

        rendered.unmount();
    });

    it('startup snapshot이 ready가 아니면 facade나 demo state를 만들지 않고 typed 실패한다', async () => {
        const snapshot = create_backend_snapshot_fixture();
        const not_ready_snapshot = {
            ...snapshot,
            connection: { ...snapshot.connection, ready: false },
        };
        const create_web_socket = vi.fn(() => new BootstrapFakeWebSocket());

        await expect(create_live_ui_application(create_descriptor(), {
            adapter_dependencies: {
                fetch: create_snapshot_fetch(not_ready_snapshot),
                create_uuid: () => TEST_REQUEST_ID,
                create_web_socket,
            },
        })).rejects.toMatchObject({ code: 'BACKEND_NOT_READY' });
        expect(create_web_socket).not.toHaveBeenCalled();
        expect(document.body).not.toHaveTextContent('₩ 6,184,200');
    });

    it('snapshot 뒤 React activation 전 sidecar exit는 activate 완료 후 recovery로 replay한다', async () => {
        const lifecycle_order: Array<string> = [];
        const snapshot = create_backend_snapshot_fixture();
        const application = await create_live_ui_application(create_descriptor(), {
            today: '2026-08-24',
            adapter_dependencies: {
                fetch: create_snapshot_fetch(snapshot),
                create_uuid: () => TEST_REQUEST_ID,
                create_web_socket: () => {
                    lifecycle_order.push('web_socket');
                    return new BootstrapFakeWebSocket();
                },
            },
        });
        const application_factory = create_live_ui_application_factory(application, () => {
            lifecycle_order.push('native_exit_replay');
            application.command_adapter.stop();
            application.facade.dispatch({
                type: 'API_DISCONNECTED',
                reason: 'BACKEND_SIDECAR_EXITED',
            });
            application.facade.dispatch({
                type: 'RECONNECT_FAILED',
                reason: '백엔드 프로세스가 중단되었습니다.',
            });
            application.facade.dispatch({
                type: 'BACKEND_SIDECAR_EXITED_ABNORMALLY',
            });
        });

        const rendered = render(<App applicationFactory={application_factory} />);

        // Facade/snapshot과 WebSocket 시작 뒤에만 buffered native event가 적용된다.
        expect(await screen.findByText('백엔드 복구 필요')).toBeInTheDocument();
        expect(lifecycle_order).toEqual(['web_socket', 'native_exit_replay']);
        expect(application.facade.get_view_model().app_exit.status).toBe(
            'sidecar_exit_failure',
        );
        rendered.unmount();
    });
});
