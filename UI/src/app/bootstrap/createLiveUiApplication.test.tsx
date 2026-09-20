import { StrictMode } from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { App } from '../App';
import { BACKEND_SCHEMA_VERSION } from '../../shared/contracts';
import type { BackendWebSocket } from '../../shared/api';
import { shutdown_preparation_fixture } from '../../shared/api/shutdownTestFixtures';
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
    it('종료 준비 실패 후 idle 연결만 복구하고 포지션과 전략 중단 상태를 유지한다', async () => {
        const base = create_backend_snapshot_fixture();
        const snapshot = { ...base, trading: { ...base.trading, status: 'reconciliation_required',
            session_id: TEST_BACKEND_SESSION_ID, version: 42, command_enabled: false, has_open_position: true } };
        const paths: string[] = [];
        const sockets: BootstrapFakeWebSocket[] = [];
        const application = await create_live_ui_application(create_descriptor(), {
            adapter_dependencies: {
                fetch: async (input, init) => {
                    const path = new URL(String(input)).pathname; paths.push(path);
                    let data: unknown = snapshot;
                    let status = 200;
                    if (path === '/v1/shutdown/state') data = { session_id: TEST_BACKEND_SESSION_ID,
                        status: 'reconciliation_required', version: 42 };
                    else if (path === '/v1/shutdown/prepare') {
                        data = shutdown_preparation_fixture({ phase: 'blocked', step: 'account',
                            reason_code: 'SHUTDOWN_ORDER_UNRESOLVED', retryable: true });
                        status = 202;
                    } else expect(path).toBe('/v1/snapshot');

                    return new Response(JSON.stringify({ schema_version: BACKEND_SCHEMA_VERSION,
                        request_id: new Headers(init?.headers).get('X-Request-Id'), ok: true, data }), { status });
                },
                create_web_socket: () => {
                    const socket = new BootstrapFakeWebSocket(); sockets.push(socket);

                    return socket;
                },
            },
        });
        application.activate();
        try {
            application.facade.dispatch({ type: 'APP_EXIT_CLICKED' });
            application.facade.dispatch({ type: 'FORCE_SELL_EXIT_CONFIRMED' });
            await waitFor(() => expect(sockets).toHaveLength(2));
            sockets[1]!.onopen?.(new Event('open'));
            sockets[1]!.onmessage?.(new MessageEvent('message', { data: JSON.stringify({
                schema_version: BACKEND_SCHEMA_VERSION, session_id: TEST_BACKEND_SESSION_ID,
                type: 'STREAM_HEARTBEAT', last_sequence: snapshot.last_sequence,
            }) }));
            const view = application.facade.get_view_model();
            expect(view.connection.is_online).toBe(true);
            expect(view.connection.recovery?.phase).toBe('live');
            expect(view.trading.command_enabled).toBe(false);
            expect(view.trading.lifecycle_status).toBe('reconciliation_required');
            expect(view.trading.has_open_position).toBe(true);
            expect(view.app_exit.error?.code).toBe('SHUTDOWN_ORDER_UNRESOLVED');
            expect(paths).toEqual(['/v1/snapshot', '/v1/shutdown/state', '/v1/shutdown/prepare', '/v1/snapshot']);
        } finally {
            application.deactivate();
        }
    });

    it('snapshot을 먼저 받은 뒤 StrictMode App에 실제 USDT 상태를 render한다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const lifecycle_order: Array<string> = [];

        // 실제 시세와 Testnet 계좌를 구분한 backend 환경 정보가 화면까지 전달되어야 한다.
        const base_snapshot = create_backend_snapshot_fixture();

        // 준비한 입력으로 결과를 계산하거나 현재 상태를 읽는다.
        const snapshot = {
            ...base_snapshot,
            environment: { market_data: 'mainnet', account: 'testnet', orders_enabled: false },
            trading: { ...base_snapshot.trading, mode: 'testnet' },
        };

        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
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

        expect(lifecycle_order).toEqual(['snapshot']);  // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.

        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const application_factory = create_live_ui_application_factory(application);
        const rendered = render(
            <StrictMode>
                <App applicationFactory={application_factory} />
            </StrictMode>,
        );

        // 화면의 표시 내용과 입력 가능 상태를 검증한다.
        expect(await screen.findByRole('button', { name: '화면 연결 상태: 연결됨' })).toBeInTheDocument();
        expect(screen.getByLabelText('시세 및 거래 환경')).toHaveTextContent('시세·REGIME 실제 시장');
        expect(screen.getByLabelText('시세 및 거래 환경')).toHaveTextContent('계좌 Testnet · 주문 비활성');
        expect(screen.getByRole('button', { name: '자동매매 실행' })).toBeEnabled();
        await screen.findByRole('region', { name: 'REGIME 판단 패널' });  // 지연 로딩된 대시보드가 표시된 뒤 검사한다.
        expect(document.querySelector('[data-metric-id="swingLow"] strong')).toHaveTextContent('HL');
        expect(document.querySelector('[data-metric-id="swingHigh"] strong')).toHaveTextContent('HH');
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

        // 화면 또는 실행 수명의 종료를 요청한다.
        rendered.unmount();
    });

    it('주문 비활성 상태에서도 시작 클릭으로 REGIME 안내·점멸과 시작 차단 팝업을 표시한다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const user = userEvent.setup();
        const base_snapshot = create_backend_snapshot_fixture();

        // 준비한 입력으로 결과를 계산하거나 현재 상태를 읽는다.
        const snapshot = {
            ...base_snapshot,
            environment: { market_data: 'mainnet', account: 'testnet', orders_enabled: false },
            trading: { ...base_snapshot.trading, mode: 'testnet', command_enabled: false },
        };

        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const snapshot_fetch = create_snapshot_fetch(snapshot);
        const request_paths: string[] = [];
        const application = await create_live_ui_application(create_descriptor(), {
            adapter_dependencies: {
                fetch: async (input, init) => {
                    const path = new URL(input.toString()).pathname;
                    request_paths.push(path);
                    if (path === '/v1/snapshot') {
                        return snapshot_fetch(input, init);
                    }
                    if (path === '/v1/regime/selection') {
                        expect(JSON.parse(init?.body as string)).toMatchObject({ regime_type: 'type0' });

                        return create_snapshot_fetch({
                            selected: 'type0', support_status: 'supported', version: 1,
                        })(input, init);
                    }
                    throw new Error(`Unexpected request: ${path}`);
                },
                create_uuid: () => TEST_REQUEST_ID,
                create_web_socket: () => new BootstrapFakeWebSocket(),
            },
        });
        const start_trading = vi.spyOn(application.command_adapter, 'start_trading');
        const rendered = render(<App applicationFactory={create_live_ui_application_factory(application)} />);

        const regime_panel = await screen.findByRole('region', { name: 'REGIME 판단 패널' });
        const start_button = screen.getByRole('button', { name: '자동매매 실행' });
        expect(start_button).toBeEnabled();  // 화면의 표시 내용과 입력 가능 상태를 검증한다.

        // 사용자 조작을 수행하고 그에 따른 비동기 반영을 기다린다.
        await user.click(start_button);
        const regime_notice = await screen.findByRole('dialog', { name: 'REGIME type을 먼저 선택해주세요' });
        await user.click(within(regime_notice).getByRole('button', { name: 'REGIME 선택' }));

        // 화면의 표시 내용과 입력 가능 상태를 검증한다.
        expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
        expect(regime_panel).toHaveAttribute('data-highlighted', 'true');
        expect(request_paths).toEqual(['/v1/snapshot']);

        // 선택 확인 문구가 바뀌어도 기존 REGIME 선택 요청을 통해 후보를 적용한다.
        const regime_button = screen.getByRole('button', { name: 'type0 횡보 적용 요청' });

        // 사용자 조작을 수행하고 그에 따른 비동기 반영을 기다린다.
        await user.click(regime_button);
        const regime_confirmation = await screen.findByRole('dialog', { name: 'REGIME type을 선택할까요?' });
        await user.click(within(regime_confirmation).getByRole('button', { name: '확인' }));  // 선택 확정 뒤 적용 상태를 기다린다.
        await waitFor(() => expect(regime_button).toHaveAttribute('aria-pressed', 'true'));

        // 사용자 조작을 수행하고 그에 따른 비동기 반영을 기다린다.
        await user.click(start_button);
        const unavailable_notice = await screen.findByRole('dialog', { name: '자동매매를 시작할 수 없습니다' });

        // 화면의 표시 내용과 입력 가능 상태를 검증한다.
        expect(unavailable_notice).toHaveTextContent('거래 시작 명령이 아직 활성화되지 않았습니다.');
        expect(within(unavailable_notice).queryByRole('button', { name: '거래 시작' })).not.toBeInTheDocument();

        // 사용자 조작을 수행하고 그에 따른 비동기 반영을 기다린다.
        await user.click(within(unavailable_notice).getByRole('button', { name: '확인' }));

        // 화면의 표시 내용과 입력 가능 상태를 검증한다.
        expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

        // 안내를 닫은 뒤 키보드로 다시 눌러도 실행 명령 없이 같은 차단 안내가 열린다.
        start_button.focus();

        // 사용자 조작을 수행하고 그에 따른 비동기 반영을 기다린다.
        await user.keyboard('{Enter}');

        // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
        expect(await screen.findByRole('dialog', { name: '자동매매를 시작할 수 없습니다' })).toBeInTheDocument();
        expect(start_trading).not.toHaveBeenCalled();
        expect(request_paths).toEqual(['/v1/snapshot', '/v1/regime/selection']);
        expect(application.facade.get_view_model().trading.is_trading).toBe(false);
        expect(screen.getByLabelText('시세 및 거래 환경')).toHaveTextContent('계좌 Testnet · 주문 비활성');

        // 화면 또는 실행 수명의 종료를 요청한다.
        rendered.unmount();
    });

    it('startup snapshot이 ready가 아니면 facade나 demo state를 만들지 않고 typed 실패한다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const snapshot = create_backend_snapshot_fixture();
        const not_ready_snapshot = {
            ...snapshot,
            connection: { ...snapshot.connection, ready: false },
        };
        const create_web_socket = vi.fn(() => new BootstrapFakeWebSocket());

        // 잘못된 입력이나 실행 실패가 정해진 오류로 전달되는지 검증한다.
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
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
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

        // 화면 또는 실행 수명의 종료를 요청한다.
        rendered.unmount();
    });
});
