import { createElement } from '../../UI/node_modules/react/index.js';
import { act, render, waitFor } from '../../UI/node_modules/@testing-library/react/dist/index.js';
import { expect, it, vi } from '../../UI/node_modules/vitest/dist/index.js';
import { writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { resolve } from 'node:path';
import { App } from '../../UI/src/app/App';
import { create_demo_ui_application } from '../../UI/src/app/bootstrap/createDemoUiApplication';
import { CHART_DRAWING_FIXTURE } from '../../UI/src/shared/testing';

it('captures deterministic App DOM parity for the pre-change source and working tree', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-10-04T12:00:00Z'));
    const application = create_demo_ui_application();
    const rendered = render(createElement(App, { applicationFactory: () => application }));
    const captures = [];
    /**
     * 함수 이름: capture()
     * 기능: 같은 fixture 입력에서 실제 App DOM과 대응 상태를 기록한다.
     * 인자: state -> 검증 시나리오 이름
     * 반환값: 없음
     * 작성 날짜: 2026/10/04
     */
    const capture = (state) => {
        const html = document.body.innerHTML;
        const view = application.facade.get_view_model();
        captures.push({ state, sha256: createHash('sha256').update(html).digest('hex'), html,
            view: { route: view.route, interval: view.chart.interval, fullscreen: view.chart.is_fullscreen,
                active_modal: view.active_modal, drawing_count: view.chart.drawings.length } });
    };
    /**
     * 함수 이름: dispatch()
     * 기능: 입력 처리 뒤 Controller store의 프레임 단위 화면 발행까지 기다린다.
     * 인자: intent -> fixture의 사용자 입력 또는 연결 통지
     * 반환값: 화면 발행 완료 Promise
     * 작성 날짜: 2026/10/04
     */
    const dispatch = async (intent) => {
        await act(async () => {
            application.facade.dispatch(intent);
            await new Promise((resolve_frame) => setTimeout(resolve_frame, 50));
        });
    };
    try {
        await waitFor(() => expect(document.body.textContent).toContain('ETH 가격 차트'));
        capture('dashboard-30m');
        for (const interval of ['1m', '4h', '1d', '30m']) {
            await dispatch({ type: 'CHART_INTERVAL_SELECTED', interval });
            capture(`chart-${interval}`);
        }
        await dispatch({ type: 'CHART_INDICATOR_SETTINGS_TOGGLED' });
        capture('indicator-settings-open');
        await dispatch({ type: 'CHART_INDICATOR_CHANGED', indicator: 'ema9', is_visible: false });
        await dispatch({ type: 'CHART_INDICATOR_SETTINGS_OUTSIDE_CLICKED' });
        await dispatch({ type: 'CHART_FULLSCREEN_CHANGED', is_fullscreen: true });
        capture('chart-fullscreen');
        await dispatch({ type: 'CHART_FULLSCREEN_CHANGED', is_fullscreen: false });
        await dispatch({ type: 'CHART_DRAWING_TOOL_CLICKED' });
        await dispatch({ type: 'CHART_DRAWING_STARTED' });
        await dispatch({ type: 'CHART_DRAWING_FINISHED', drawing: CHART_DRAWING_FIXTURE });
        capture('chart-committed-drawing');
        await dispatch({ type: 'REALTIME_INDICATORS_TAB_SELECTED' });
        capture('realtime-indicators');
        await dispatch({ type: 'SHOW_TRADE_HISTORY' });
        await waitFor(() => expect(application.facade.get_view_model().trade_history.is_loading).toBe(false));
        capture('trade-history');
        await dispatch({ type: 'HISTORY_PERIOD_SELECTED', period: 'last7days' });
        await waitFor(() => expect(application.facade.get_view_model().trade_history.is_loading).toBe(false));
        await dispatch({ type: 'HISTORY_SIDE_SELECTED', side: 'sell' });
        await waitFor(() => expect(application.facade.get_view_model().trade_history.is_loading).toBe(false));
        capture('trade-history-filtered');
        await dispatch({ type: 'OPEN_CSV_EXPORT' });
        capture('csv-modal');
        await dispatch({ type: 'CLOSE_CSV_EXPORT' });
        await dispatch({ type: 'BACK_TO_DASHBOARD' });
        await dispatch({ type: 'APP_EXIT_CLICKED' });
        capture('exit-confirmation');
        await dispatch({ type: 'APP_EXIT_CANCELED' });
        await dispatch({ type: 'UI_CONNECTION_DISCONNECTED', reason: 'fixture' });
        await dispatch({ type: 'BACKEND_CONNECTION_STATUS', status: { phase: 'recovering', attempt: 2,
            error_code: 'STREAM_STALE', last_received_at_ms: Date.now() - 1000, next_retry_at_ms: Date.now() + 1000 } });
        capture('connection-recovery');
        expect(application.command_adapter.command_records.every((record) => record.name === 'load_trade_history')).toBe(true);
        const mode = process.env.RENDERER_PARITY_BASELINE === '1' ? 'baseline' : 'modified';
        await writeFile(resolve('artifacts/renderer-oom-implementation-20261004', `renderer-parity-${mode}.json`),
            JSON.stringify({ mode, timestamp: '2026-10-04T12:00:00Z', kind: 'jsdom-dom-parity-not-pixel-screenshot', captures }, null, 4));
    } finally {
        rendered.unmount();
        application.deactivate();
        vi.useRealTimers();
    }
});
