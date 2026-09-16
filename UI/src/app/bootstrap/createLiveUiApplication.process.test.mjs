import { spawn } from 'node:child_process';
import { randomBytes, randomUUID } from 'node:crypto';
import path from 'node:path';

import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createElement, StrictMode } from 'react';
import { describe, expect, it } from 'vitest';

import { App } from '../App';
import {
    create_live_ui_application,
    create_live_ui_application_factory,
} from './createLiveUiApplication';

const TEST_TIMEOUT_MILLISECONDS = 15_000;
const TEST_UI_ORIGIN = 'http://127.0.0.1:5173';
const REPOSITORY_ROOT = path.resolve(process.cwd(), '..');
const PROCESS_FIXTURE_PATH = path.join(
    REPOSITORY_ROOT,
    'backend',
    'tests',
    'integration',
    'phase5_process_fixture.py',
);

/**
 * 클래스 이름: ProcessFixtureWebSocket
 * 기능: 실제 HTTP snapshot 뒤 UI adapter의 첫 인증 frame과 URL 비노출을 관찰한다.
 * 작성 날짜: 2026/08/21
 */
class ProcessFixtureWebSocket {
    onopen = null;
    onmessage = null;
    onclose = null;
    onerror = null;
    authentication_received = false;

    /**
     * 함수 이름: ProcessFixtureWebSocket.constructor()
     * 기능: handler가 연결된 다음 browser open event와 같은 microtask를 예약한다.
     * 인자: 없음
     * 반환값: ProcessFixtureWebSocket 인스턴스
     * 작성 날짜: 2026/08/21
     */
    constructor() {
        queueMicrotask(() => this.onopen?.(new Event('open')));
    }

    /**
     * 함수 이름: send()
     * 기능: token 값을 보존하지 않고 첫 frame이 AUTHENTICATE인지 여부만 기록한다.
     * 인자: data -> adapter가 보낸 text frame
     * 반환값: 없음
     * 작성 날짜: 2026/08/21
     */
    send(data) {
        const frame = JSON.parse(data);

        this.authentication_received = frame.type === 'AUTHENTICATE'
            && Number.isSafeInteger(frame.after_sequence);
    }

    /**
     * 함수 이름: close()
     * 기능: React unmount의 socket 정리 요청을 side effect 없이 수용한다.
     * 인자: code -> optional close code, reason -> optional safe reason
     * 반환값: 없음
     * 작성 날짜: 2026/08/21
     */
    close(code, reason) {
        void code;
        void reason;
    }
}

/**
 * 함수 이름: read_json_line()
 * 기능: child pipe의 첫 newline JSON object를 bounded timeout 안에 읽는다.
 * 인자: readable -> child stdout 또는 trace pipe, timeout_milliseconds -> 제한 시간
 * 반환값: parsing한 JSON 값 Promise
 * 작성 날짜: 2026/08/21
 */
function read_json_line(readable, timeout_milliseconds) {
    return new Promise((resolve, reject) => {
        let buffered_text = '';
        const timeout_handle = setTimeout(() => {
            cleanup();
            reject(new Error('Phase 5 process fixture timed out'));
        }, timeout_milliseconds);

        /** 첫 JSON line을 얻거나 stream이 닫히면 모든 listener와 timer를 정리한다. */
        function cleanup() {
            clearTimeout(timeout_handle);
            readable.off('data', on_data);
            readable.off('end', on_end);
            readable.off('error', on_error);
        }

        /** newline 이전 bytes만 JSON으로 해석하고 나머지 출력은 소비하지 않는다. */
        function on_data(chunk) {
            buffered_text += chunk.toString('utf8');
            const newline_index = buffered_text.indexOf('\n');
            if (newline_index < 0) {
                return;
            }

            cleanup();
            try {
                resolve(JSON.parse(buffered_text.slice(0, newline_index)));
            } catch {
                reject(new Error('Phase 5 process fixture returned invalid JSON'));
            }
        }

        /** Ready/trace 없이 종료한 process를 raw stderr 없이 안전하게 보고한다. */
        function on_end() {
            cleanup();
            reject(new Error('Phase 5 process fixture closed before readiness'));
        }

        /** Pipe 내부 오류도 secret-bearing payload 없이 정적 오류로 변환한다. */
        function on_error() {
            cleanup();
            reject(new Error('Phase 5 process fixture pipe failed'));
        }

        readable.on('data', on_data);
        readable.on('end', on_end);
        readable.on('error', on_error);
    });
}

/**
 * 함수 이름: create_process_fetch()
 * 기능: Node fetch에 browser가 자동으로 넣는 exact Origin만 보완한다.
 * 인자: 없음
 * 반환값: 실제 loopback HTTP에 연결하는 fetch 함수
 * 작성 날짜: 2026/08/21
 */
function create_process_fetch() {
    return (input, init = {}) => {
        const headers = new Headers(init.headers);
        headers.set('Origin', TEST_UI_ORIGIN);

        return fetch(input, { ...init, headers });
    };
}

/**
 * 함수 이름: stop_child_process()
 * 기능: test 종료 시 stop pipe를 우선 사용하고 bounded 시간 뒤 fixture만 강제 종료한다.
 * 인자: child_process -> 이번 test가 생성한 Python process
 * 반환값: child 종료를 기다리는 Promise
 * 작성 날짜: 2026/08/21
 */
async function stop_child_process(child_process) {
    if (child_process.exitCode !== null) {
        return;
    }

    const stop_pipe = child_process.stdio[4];
    if (stop_pipe !== null && !stop_pipe.destroyed) {
        // Child가 먼저 실패한 경우의 EPIPE는 test process 밖으로 전파하지 않는다.
        stop_pipe.once('error', () => {});
        stop_pipe.end('S');
    }
    await new Promise((resolve) => {
        const kill_timeout = setTimeout(() => {
            child_process.kill('SIGTERM');
        }, 2_000);

        child_process.once('exit', () => {
            clearTimeout(kill_timeout);
            resolve();
        });
    });
}

// 이 fixture는 POSIX FD 3/4/5 상속 전용이다. Windows stdio process seam은 backend 별도 fixture로 검증한다.
describe.skipIf(process.platform === 'win32')('Phase 5 actual process live read', () => {
    it.each([false, true])('Python startup 1~3 뒤 UISTM 4와 AppShell 5가 실제 snapshot을 표시한다: live tick %s', async (advance_market) => {
        const session_token = randomBytes(32).toString('base64url');
        const python_path = [
            path.join(REPOSITORY_ROOT, 'backend', 'src'),
            path.join(REPOSITORY_ROOT, 'backend'),
        ].join(path.delimiter);
        const child_process = spawn('python3', [PROCESS_FIXTURE_PATH], {
            cwd: REPOSITORY_ROOT,
            env: {
                LANG: 'C.UTF-8',
                PATH: process.env.PATH ?? '/usr/bin:/bin',
                PYTHONPATH: python_path,
                UI_PROCESS_FIXTURE_ADVANCE_MARKET: advance_market ? '1' : '0',
            },
            stdio: ['ignore', 'pipe', 'pipe', 'pipe', 'pipe', 'pipe'],
        });
        let rendered;

        // Session credential은 argv/environment가 아니라 inherited anonymous pipe에 한 번 기록한다.
        child_process.stdio[3].end(session_token);

        try {
            const [descriptor, backend_trace] = await Promise.all([
                read_json_line(child_process.stdout, TEST_TIMEOUT_MILLISECONDS),
                read_json_line(child_process.stdio[5], TEST_TIMEOUT_MILLISECONDS),
            ]);
            expect(backend_trace.market_version - backend_trace.indicator_market_version)
                .toBe(advance_market ? 1 : 0);
            let event_socket = null;
            const web_socket_urls = [];
            const application = await create_live_ui_application(
                { ...descriptor, token: session_token },
                {
                    today: '2026-08-21',
                    adapter_dependencies: {
                        fetch: create_process_fetch(),
                        create_uuid: randomUUID,
                        create_web_socket: (url) => {
                            web_socket_urls.push(url);
                            event_socket = new ProcessFixtureWebSocket();
                            return event_socket;
                        },
                    },
                },
            );
            const integrated_trace = [...backend_trace.message_ids];
            const traced_application = {
                ...application,
                activate: () => {
                    application.activate();
                    integrated_trace.push('4');
                },
            };

            // React commit가 facade actor를 시작한 뒤 실제 backend 값이 AppShell에 나타나야 한다.
            rendered = render(createElement(
                StrictMode,
                null,
                createElement(App, {
                    applicationFactory: create_live_ui_application_factory(
                        traced_application,
                    ),
                }),
            ));
            expect(await screen.findByRole('button', { name: '화면 연결 상태: 연결됨' })).toBeInTheDocument();
            expect(await screen.findByRole('article', { name: '보유 자산' })).toHaveTextContent(
                'USDT',
            );
            integrated_trace.push('5');

            await waitFor(() => {
                expect(event_socket?.authentication_received).toBe(true);
            });
            expect(web_socket_urls).toHaveLength(1);
            expect(web_socket_urls[0]).not.toContain(session_token);
            expect(document.body).not.toHaveTextContent('KRW');
            expect(document.body).not.toHaveTextContent(session_token);
            expect(integrated_trace).toEqual(['1', '2', '3', '4', '5']);

            // Case 3의 production adapter가 실제 Python `/v1/trades` empty 결과까지 렌더링해야 한다.
            const user = userEvent.setup();
            const connection_badge = screen.getByRole('button', { name: '화면 연결 상태: 연결됨' });
            await user.hover(connection_badge);
            const connection_tooltip = await screen.findByRole('tooltip');
            await waitFor(() => {
                expect(within(connection_tooltip).getAllByText('연결됨')).toHaveLength(2);
                expect(within(connection_tooltip).getByText('연결 안 됨')).toBeInTheDocument();
            });
            await user.unhover(connection_badge);
            expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();

            await user.click(screen.getByRole('button', { name: '전체 보기' }));
            expect(await screen.findByRole('heading', {
                level: 1,
                name: '거래 내역 상세',
            })).toBeInTheDocument();
            await waitFor(() => {
                expect(screen.getByText('거래 내역이 없습니다')).toBeInTheDocument();
            });
        } finally {
            rendered?.unmount();
            await stop_child_process(child_process);
        }
    }, TEST_TIMEOUT_MILLISECONDS);
});
