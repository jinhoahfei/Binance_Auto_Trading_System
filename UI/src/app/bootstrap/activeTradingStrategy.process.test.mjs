import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';

import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createElement } from 'react';
import { describe, expect, it } from 'vitest';

import { App } from '../App';
import { BACKEND_SCHEMA_VERSION } from '../../shared/contracts';
import {
    create_backend_snapshot_fixture,
    TEST_BACKEND_TOKEN,
} from '../../shared/api/backendTestFixtures';
import {
    create_live_ui_application,
    create_live_ui_application_factory,
} from './createLiveUiApplication';


/**
 * 함수 이름: load_backend_replay()
 * 기능: 별도 Python process의 실제 STM 시나리오에서 생성한 transport event를 읽는다.
 * 인자: 없음
 * 반환값: 외부 통신 없이 실행한 Case별 전이와 기대 문구
 * 작성 날짜: 2026/09/05
 */
function load_backend_replay() {
    // 개발 서버를 띄우지 않고 기존 backend 테스트 환경에서 시장·가짜 체결 조건을 실행한다.
    const backend_directory = path.resolve(process.cwd(), '..', 'backend');
    const virtual_environment_python = process.platform === 'win32'
        ? path.join(backend_directory, '.venv', 'Scripts', 'python.exe')
        : path.join(backend_directory, '.venv', 'bin', 'python');
    const python_executable = existsSync(virtual_environment_python)
        ? virtual_environment_python : (process.platform === 'win32' ? 'python' : 'python3');
    const output = execFileSync(python_executable, [
        '-m', 'tests.integration.active_trading_logic_replay',
    ], {
        cwd: backend_directory,
        env: { ...process.env, PYTHONPATH: path.join(backend_directory, 'src') },
        encoding: 'utf8',
        timeout: 10_000,
        maxBuffer: 1_048_576,
    });

    return JSON.parse(output);  // UI의 기대 Case로 backend의 실제 event payload를 덮어쓰지 않는다.
}


/**
 * 클래스 이름: ReplayWebSocket
 * 기능: 실제 adapter의 인증·sequence·event 처리에 Python wire frame을 메모리로 전달한다.
 * 작성 날짜: 2026/09/05
 */
class ReplayWebSocket {
    onopen = null;
    onmessage = null;
    onclose = null;
    onerror = null;
    after_sequence = null;
    is_closed = false;

    /**
     * 함수 이름: send()
     * 기능: adapter가 보낸 인증 frame에서 재생 시작 sequence만 확인한다.
     * 인자: data -> adapter의 인증 JSON
     * 반환값: 없음
     * 작성 날짜: 2026/09/05
     */
    send(data) {
        const frame = JSON.parse(data);
        expect(frame.type).toBe('AUTHENTICATE');
        this.after_sequence = frame.after_sequence;  // 테스트 token을 별도로 저장하거나 출력하지 않는다.
    }

    /**
     * 함수 이름: receive()
     * 기능: 실제 backend event를 adapter의 WebSocket 수신 handler에 전달한다.
     * 인자: event -> Python observer가 발행한 DTO
     * 반환값: 없음
     * 작성 날짜: 2026/09/05
     */
    receive(event) {
        this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(event) }));
    }

    /**
     * 함수 이름: close()
     * 기능: adapter의 연결 교체와 React unmount에서 socket 정리 여부를 기록한다.
     * 인자: 없음
     * 반환값: 없음
     * 작성 날짜: 2026/09/05
     */
    close() {
        this.is_closed = true;
    }
}


/**
 * 함수 이름: create_replay_snapshot()
 * 기능: UI의 비거래 fixture와 실제 backend 거래 상태를 하나의 초기·재연결 snapshot으로 구성한다.
 * 인자: event -> snapshot에 보존할 실제 거래 event
 * 반환값: event와 session·sequence·거래 상태가 같은 전체 snapshot
 * 작성 날짜: 2026/09/05
 */
function create_replay_snapshot(event) {
    // 전략 검증과 무관한 계좌·차트 fixture만 재사용하고 거래 DTO는 실제 값을 그대로 전달한다.
    const snapshot = create_backend_snapshot_fixture();

    return {
        ...snapshot,
        session_id: event.session_id,
        last_sequence: event.sequence,
        regime: { ...snapshot.regime, selected: 'type0' },
        trading: event.payload.trading,
    };
}


/**
 * 함수 이름: mount_replay_application()
 * 기능: 메모리 HTTP·WebSocket 경계로 production bootstrap·adapter·App 구독을 시작한다.
 * 인자: initial_event -> 최초 snapshot의 실제 거래 event
 * 반환값: 정리할 App render와 실제 runtime 및 조작할 메모리 transport
 * 작성 날짜: 2026/09/05
 */
async function mount_replay_application(initial_event) {
    const transport = {
        snapshot: create_replay_snapshot(initial_event),
        sockets: [],
        snapshot_request_count: 0,
        snapshot_gate: null,
    };

    // 포트 번호는 descriptor 형식 검증용이며 fetch와 socket 생성은 모두 메모리 경계로 주입한다.
    const application = await create_live_ui_application({
        port: 42_123,
        session_id: initial_event.session_id,
        schema_version: BACKEND_SCHEMA_VERSION,
        token: TEST_BACKEND_TOKEN,
    }, {
        today: '2026-09-05',
        adapter_dependencies: {
            fetch: async (input, init) => {
                expect(new URL(input).pathname).toBe('/v1/snapshot');
                expect(init.method).toBe('GET');
                transport.snapshot_request_count += 1;
                if (transport.snapshot_gate !== null) await transport.snapshot_gate;

                return new Response(JSON.stringify({
                    schema_version: BACKEND_SCHEMA_VERSION,
                    request_id: init.headers['X-Request-Id'],
                    ok: true,
                    data: transport.snapshot,
                }), { status: 200 });
            },
            create_uuid: () => 'f5a4f621-25f8-4dd2-bfb7-1b80e9561423',
            create_web_socket: () => {
                const socket = new ReplayWebSocket();
                transport.sockets.push(socket);
                queueMicrotask(() => socket.onopen?.(new Event('open')));

                return socket;
            },
        },
    });
    const rendered = render(createElement(App, {
        applicationFactory: create_live_ui_application_factory(application),
    }));
    await userEvent.click(await screen.findByRole('tab', { name: '실시간 지표' }));

    return { application, rendered, transport };
}


/**
 * 함수 이름: expect_shared_strategy()
 * 기능: 수동 rerender 없이 구독으로 갱신된 두 전략 표시 DOM과 내부 chart 상태를 확인한다.
 * 인자: application -> 실제 facade를 소유한 runtime, step -> 독립 기대 문구와 단계 이름
 * 반환값: 두 표시 영역의 자동 갱신이 확인되면 완료되는 Promise
 * 작성 날짜: 2026/09/05
 */
async function expect_shared_strategy(application, step, connected = true) {
    await waitFor(() => {
        // actor 값만 비교하지 않고 사용자에게 보이는 두 Boundary를 각각 검증한다.
        const chart_state = screen.getByLabelText('현재 실행 전략');
        const account_state = screen.getByRole('article', { name: '전략 상태' });
        expect(within(chart_state).getByText(step.expected_label), step.stage).toBeInTheDocument();
        expect(within(account_state).getAllByText(step.expected_label).length, step.stage).toBeGreaterThan(0);
        expect(chart_state).toHaveAttribute('title', step.expected_label);
        expect(application.facade.get_view_model().chart.active_trading_logic_state).toBe(step.expected_label);

        // 실제 DTO의 단계와 각 행이 Case별 그룹에서 이전 단계 행 없이 표시되는지 확인한다.
        const panel = screen.getByRole('tabpanel', { name: '실시간 지표' });
        const rows = step.event.payload.trading.active_logic?.indicators?.conditions ?? [];
        const phases = step.event.payload.trading.active_logic?.indicators?.phases ?? [];
        const expected_titles = phases.map((phase) => `${phase.strategy === 'CASE_B' ? 'Case_B' : 'Case_C'} 실시간 지표`);
        if (rows.some((row) => row.strategy === null) || !expected_titles.length) {
            expected_titles.push(phases.length ? '공통 실시간 지표' : `${step.expected_label} 실시간 지표`);
        }
        expect(within(panel).getAllByRole('heading').map((heading) => heading.textContent)).toEqual(expected_titles);
        for (const phase of phases) {
            const group = within(panel).getByRole('region', { name: `${phase.strategy === 'CASE_B' ? 'Case_B' : 'Case_C'} 실시간 지표` });
            expect(group).toHaveTextContent('현재 단계');
        }
        expect(panel.querySelectorAll('[data-condition-id]')).toHaveLength(rows.length);
        for (const row of rows) {
            const identity = `${row.strategy ?? 'common'}:${row.phase}:${row.condition_id}`;
            const displayed = panel.querySelector(`[data-condition-id="${identity}"]`);
            expect(displayed, identity).not.toBeNull();
            expect(displayed).toHaveAttribute('data-tone', !connected || row.satisfied === null ? 'neutral' : row.satisfied ? 'positive' : 'negative');
            if (!connected || row.satisfied === null) expect(displayed.querySelector('strong')).toHaveTextContent('—');
        }
        expect(panel).not.toHaveTextContent('27s');
    });
}

const replay_report = load_backend_replay();

describe('실제 Python STM → transport → App ACTIVE STATE', () => {
    it('연결 단절 중 이전 판정을 회색으로 해제하고 전체 snapshot으로 복원한다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const scenario = replay_report.scenarios[0];
        const filled = scenario.steps.find((step) => step.event.payload.trading.has_open_position);
        const { application, rendered, transport } = await mount_replay_application(filled.event);
        let release_snapshot;
        try {
            await expect_shared_strategy(application, filled);
            transport.snapshot_gate = new Promise((resolve) => { release_snapshot = resolve; });
            const next = scenario.steps[scenario.steps.indexOf(filled) + 2];
            transport.snapshot = create_replay_snapshot(next.event);
            await act(async () => transport.sockets[0].receive(next.event));
            await waitFor(() => {
                const panel = document.getElementById('trader-tabpanel-realtime');
                const rows = panel.querySelectorAll('[data-condition-id]');
                expect(rows.length).toBeGreaterThan(0);
                for (const row of rows) {
                    expect(row).toHaveAttribute('data-tone', 'neutral');
                    expect(row.querySelector('strong')).toHaveTextContent('—');
                }
                for (const timer of panel.querySelectorAll('[data-timer-state]')) {
                    expect(timer).toHaveAttribute('data-timer-state', 'unavailable');
                }
            });
            await act(async () => release_snapshot());
            expect(screen.queryByRole('dialog', { name: 'API 연결이 필요합니다' })).not.toBeInTheDocument();
            expect(await screen.findByRole('button', { name: '화면 연결 상태: 복구 중' })).toBeInTheDocument();
            await expect_shared_strategy(application, next, false);
            const resumed = scenario.steps[scenario.steps.indexOf(next) + 1];
            await act(async () => transport.sockets[1].receive(resumed.event));
            await expect_shared_strategy(application, resumed);
        } finally {
            release_snapshot?.();
            rendered.unmount();
            application.deactivate();
        }
    });

    it.each(replay_report.scenarios)('$strategy 시장·매수·체결·종료 전이가 두 DOM을 자동 갱신한다', async (scenario) => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const { application, rendered, transport } = await mount_replay_application(scenario.steps[0].event);
        try {
            // Python 단계는 실제 B-09/C-12와 Position owner를 검사하며 주문은 가짜 REST에만 제출한다.
            expect(replay_report.network_connections_allowed).toBe(false);
            expect(scenario.fake_order_count).toBe(2);
            await expect_shared_strategy(application, scenario.steps[0]);
            expect(transport.sockets[0].after_sequence).toBe(scenario.steps[0].event.sequence);
            for (const step of scenario.steps.slice(1)) {
                await act(async () => transport.sockets[0].receive(step.event));
                await expect_shared_strategy(application, step);
            }
            expect(transport.snapshot_request_count).toBe(1);  // 정상 event 연속 전달은 전체 재조회가 필요 없다.
        } finally {
            rendered.unmount();
            application.deactivate();
        }
    });

    it.each(replay_report.clock_skew_scenarios)('$strategy 체결 시각이 앞서도 보유 단계의 지표와 타이머가 계속 갱신된다', async (scenario) => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const { application, rendered, transport } = await mount_replay_application(scenario.steps[0].event);
        const prefix = scenario.strategy === 'CASE_B' ? 'b' : 'c';
        let first_timer_text;
        let observed_holding = false;
        let observed_update = false;
        try {
            for (const step of scenario.steps.slice(1)) {
                await act(async () => transport.sockets[0].receive(step.event));
                await expect_shared_strategy(application, step);
                const panel = screen.getByRole('tabpanel', { name: '실시간 지표' });
                if (step.stage.includes('체결·포지션 관리')) {
                    const group = within(panel).getByRole('region', { name: `${prefix === 'b' ? 'Case_B' : 'Case_C'} 실시간 지표` });
                    expect(group).toHaveTextContent('보유');
                    first_timer_text = within(panel.querySelector(`[data-condition-id$=":${prefix}_time_exit"]`)).getByRole('timer').textContent;
                }
                if (step.stage.includes('같은 보유 단계 지표 갱신')) {
                    const row = panel.querySelector(`[data-condition-id$=":${prefix}_profit_zone"]`);
                    await waitFor(() => expect(row.querySelector('strong')).toHaveTextContent(prefix === 'b' ? '0.61' : '0.09'));
                    observed_holding = true;
                }
                if (step.stage.includes('시각 오차 후 보유 지표 갱신')) {
                    const row = panel.querySelector(`[data-condition-id$=":${prefix}_profit_zone"]`);
                    await waitFor(() => expect(row.querySelector('strong')).toHaveTextContent(prefix === 'b' ? '0.62' : '0.07'));
                    const timer = within(panel.querySelector(`[data-condition-id$=":${prefix}_time_exit"]`)).getByRole('timer');
                    expect(timer.textContent).not.toBe(first_timer_text);
                    observed_update = true;
                }
            }
            expect(observed_holding).toBe(true);
            expect(observed_update).toBe(true);
            expect(transport.snapshot_request_count).toBe(1);
            expect(transport.sockets).toHaveLength(1);
        } finally {
            rendered.unmount();
            application.deactivate();
        }
    });

    it('실제 C 회복 타이머가 시작 대기·180초 경계·시간 초과·저점 갱신을 표시한다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const steps = replay_report.timer_scenario.steps;
        const { application, rendered, transport } = await mount_replay_application(steps[0].event);
        try {
            // 독립 기대 문구로 실제 STM이 만든 남은 시간과 새 회차를 DOM까지 검사한다.
            const times = ['03:00', '03:00', '00:01', '00:00', '03:00', '03:00', '02:59'];
            let previous_id = null;
            for (const [index, step] of steps.entries()) {
                if (index > 0) await act(async () => transport.sockets[0].receive(step.event));
                await expect_shared_strategy(application, step);
                if (index === steps.length - 1) {
                    expect(document.querySelectorAll('[data-timer-state]')).toHaveLength(0);
                    continue;
                }

                const row = document.querySelector(`[data-condition-id$=":${step.timer_condition_id}"]`);
                await waitFor(() => expect(within(row).getByRole('timer')).toHaveTextContent(times[index]));
                const timer = row.querySelector('[data-timer-state]');
                expect(timer).toHaveAttribute('data-timer-state', index === 0 ? 'waiting' : index === 3 ? 'pending' : 'running');
                if (index === 4 || index === 5) {
                    expect(timer.getAttribute('data-timer-id')).not.toBe(previous_id);
                    expect(timer).toHaveTextContent(index === 4 ? '시간 초과로 재시작' : '저점 갱신으로 재시작');
                    expect(row).toHaveAttribute('data-tone', 'neutral');
                    if (index === 5) {
                        const rebound = document.querySelector('[data-condition-id$=":c_rebound"]');
                        expect(rebound).toHaveTextContent('≥ -0.27');
                        expect(rebound).toHaveAttribute('data-tone', 'neutral');
                    }
                }
                previous_id = timer.getAttribute('data-timer-id');
            }
            expect(replay_report.timer_scenario.fake_order_count).toBe(0);
        } finally {
            rendered.unmount();
            application.deactivate();
        }
    });

    it('C 타이머 리셋 event 유실 뒤 전체 snapshot으로 새 회차를 복원한다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const steps = replay_report.timer_scenario.steps;
        const { application, rendered, transport } = await mount_replay_application(steps[0].event);
        try {
            const reset_step = steps[4];
            transport.snapshot = create_replay_snapshot(reset_step.event);
            await act(async () => transport.sockets[0].receive(reset_step.event));
            expect(screen.queryByRole('dialog', { name: 'API 연결이 필요합니다' })).not.toBeInTheDocument();
            expect(await screen.findByRole('button', { name: '화면 연결 상태: 복구 중' })).toBeInTheDocument();
            await expect_shared_strategy(application, reset_step, false);
            const row = document.querySelector('[data-condition-id$=":c_recovery_window"]');

            // 전체 snapshot만으로 연결 정상 판정을 하지 않는다. 다음 유효 event까지 타이머도 대기한다.
            expect(row.querySelector('[data-timer-state]')).toHaveAttribute('data-timer-state', 'unavailable');
            expect(transport.snapshot_request_count).toBe(2);

            // 새 socket의 다음 저점 갱신도 정상적으로 이어져 이전 회차가 살아나지 않아야 한다.
            await act(async () => transport.sockets[1].receive(steps[5].event));
            await expect_shared_strategy(application, steps[5]);
            expect(row).toHaveTextContent('저점 갱신으로 재시작');
        } finally {
            rendered.unmount();
            application.deactivate();
        }
    });

    it.each(replay_report.scenarios)('$strategy event 유실 후 전체 재동기화로 체결 전략을 복원한다', async (scenario) => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const { application, rendered, transport } = await mount_replay_application(scenario.steps[0].event);
        try {
            await expect_shared_strategy(application, scenario.steps[0]);
            const filled_step = scenario.steps.find((step) => step.event.payload.trading.has_open_position);
            expect(filled_step).toBeDefined();

            // 중간 sequence를 생략해 실제 adapter가 gap을 감지하고 최신 snapshot을 다시 받게 한다.
            transport.snapshot = create_replay_snapshot(filled_step.event);
            await act(async () => transport.sockets[0].receive(filled_step.event));

            // 화면 재동기화는 API 알림이나 매매 중지를 만들지 않는다.
            expect(screen.queryByRole('dialog', { name: 'API 연결이 필요합니다' })).not.toBeInTheDocument();
            expect(application.facade.get_view_model().chart.active_trading_logic_state).toBe(filled_step.expected_label);
            await expect_shared_strategy(application, filled_step, false);
            expect(transport.snapshot_request_count).toBe(2);
            expect(transport.sockets[0].is_closed).toBe(true);
            expect(transport.sockets[1].after_sequence).toBe(filled_step.event.sequence);

            // 복원 후 새 socket에서도 종료까지 이어져 이전 Case가 화면에 남지 않아야 한다.
            const filled_index = scenario.steps.indexOf(filled_step);
            for (const step of scenario.steps.slice(filled_index + 1)) {
                await act(async () => transport.sockets[1].receive(step.event));
                await expect_shared_strategy(application, step);
            }
        } finally {
            rendered.unmount();
            application.deactivate();
        }
    });
});
