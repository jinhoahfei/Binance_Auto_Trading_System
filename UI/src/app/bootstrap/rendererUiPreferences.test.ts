import { afterEach, describe, expect, it, vi } from 'vitest';

import { UiApplicationFacade } from '../control';
import { CHART_DRAWING_FIXTURE, FakeUiCommandAdapter } from '../../shared/testing';
import { BACKEND_SCHEMA_VERSION } from '../../shared/contracts';
import { create_backend_snapshot_fixture, TEST_BACKEND_SESSION_ID, TEST_BACKEND_TOKEN } from '../../shared/api/backendTestFixtures';
import { create_live_ui_application } from './createLiveUiApplication';
import * as renderer_preferences from './rendererUiPreferences';
import {
    get_renderer_preference_storage,
    load_renderer_ui_preferences,
    start_renderer_ui_preference_persistence,
} from './rendererUiPreferences';


/**
 * 함수 이름: create_storage_fixture()
 * 기능: 같은 창에서 재사용되는 한 저장값과 저장 횟수를 관찰한다.
 * 인자: 없음
 * 반환값: 메모리 저장소와 저장값 접근자
 * 작성 날짜: 2026/10/04
 */
function create_storage_fixture() {
    let serialized: string | null = null;
    const storage = {
        getItem: vi.fn(() => serialized),
        setItem: vi.fn((_key: string, value: string) => { serialized = value; }),
        removeItem: vi.fn(() => { serialized = null; }),
    };

    return { storage, read: () => serialized, replace: (value: string | null) => { serialized = value; } };
}

afterEach(() => vi.restoreAllMocks());

describe('동일 backend session의 renderer 화면 설정 복구', () => {
    it('live bootstrap이 최신 서버 snapshot으로 매매 상태를 결정하고 같은 session의 화면만 복원한다', async () => {
        const fixture = create_storage_fixture();
        const original = new UiApplicationFacade(new FakeUiCommandAdapter(), {
            today: '2026-10-04', chart_interval: '1d', chart_is_fullscreen: true,
            is_trading: true, has_open_position: true,
        });
        original.start();
        start_renderer_ui_preference_persistence(original, TEST_BACKEND_SESSION_ID, fixture.storage)();
        original.stop();
        vi.spyOn(renderer_preferences, 'get_renderer_preference_storage').mockReturnValue(fixture.storage);
        const paths: string[] = [];
        const snapshot = create_backend_snapshot_fixture();
        const socket_factory = vi.fn(() => ({ onopen: null, onmessage: null, onclose: null, onerror: null,
            send: vi.fn(), close: vi.fn() }));
        const application = await create_live_ui_application({
            port: 42_123, session_id: TEST_BACKEND_SESSION_ID, schema_version: BACKEND_SCHEMA_VERSION,
            token: TEST_BACKEND_TOKEN,
        }, { today: '2026-10-04', adapter_dependencies: {
            fetch: async (input, options) => {
                paths.push(new URL(String(input)).pathname);

                return new Response(JSON.stringify({ schema_version: BACKEND_SCHEMA_VERSION,
                    request_id: new Headers(options?.headers).get('X-Request-Id'), ok: true, data: snapshot }),
                { status: 200 });
            },
            create_web_socket: socket_factory,
        } });
        application.activate();

        try {
            expect(application.facade.get_view_model().chart.interval).toBe('1d');
            expect(application.facade.get_view_model().chart.is_fullscreen).toBe(true);
            expect(application.facade.get_view_model().trading.is_trading).toBe(false);
            expect(application.facade.get_view_model().trading.lifecycle_status).toBe(snapshot.trading.status);
            expect(application.facade.get_view_model().trading.has_open_position).toBe(snapshot.trading.has_open_position);
            expect(paths).toEqual(['/v1/snapshot']);
            expect(socket_factory).toHaveBeenCalledTimes(1);
            expect(fixture.read()).not.toContain(TEST_BACKEND_TOKEN);
        } finally {
            application.deactivate();
        }
    });

    it('봉 주기·지표·완성 drawing·전체화면·상세 필터를 복구하며 매매 명령은 재생하지 않는다', () => {
        const fixture = create_storage_fixture();
        const original_adapter = new FakeUiCommandAdapter();
        const original = new UiApplicationFacade(original_adapter, {
            today: '2026-10-04', chart_interval: '4h',
            chart_indicators: { bollinger_bands: false, ema9: true, volume: false },
            chart_drawings: { '1m': [], '30m': [], '4h': [CHART_DRAWING_FIXTURE], '1d': [] },
            chart_is_fullscreen: true, history_period: 'last30days', history_side: 'sell', history_has_entered: true,
            is_trading: true, has_open_position: true,
        });
        original.start();
        original.dispatch({ type: 'SHOW_TRADE_HISTORY' });
        const stop = start_renderer_ui_preference_persistence(original, 'same-session', fixture.storage);
        const saved = load_renderer_ui_preferences('same-session', fixture.storage)!;
        stop();
        original.stop();

        const restored_adapter = new FakeUiCommandAdapter();
        const { route, ...display_options } = saved;
        const restored = new UiApplicationFacade(restored_adapter, { today: '2026-10-04', ...display_options });
        restored.start();
        if (route === 'trade_history') restored.dispatch({ type: 'SHOW_TRADE_HISTORY' });
        const view = restored.get_view_model();

        expect(view.route).toBe('trade_history');
        expect(view.chart.interval).toBe('4h');
        expect(view.chart.indicators).toEqual({ bollinger_bands: false, ema9: true, volume: false });
        expect(view.chart.drawings).toEqual([CHART_DRAWING_FIXTURE]);
        expect(view.chart.is_fullscreen).toBe(true);
        expect(view.chart.drawing_mode).toBe('deactivated');
        expect(view.chart.selected_line_id).toBeNull();
        expect(view.active_modal).toBeNull();
        expect(view.trade_history.period).toBe('last30days');
        expect(view.trade_history.side).toBe('sell');
        expect(view.trading.is_trading).toBe(false);
        expect(view.trading.has_open_position).toBe(false);
        expect(restored_adapter.command_records).toEqual([
            { name: 'load_trade_history', payload: { period: 'last30days', side: 'sell' } },
        ]);
        expect(fixture.read()).not.toMatch(/token|is_trading|has_open_position|account|balance|modal|command/u);
        restored.stop();
    });

    it('시세·전략 표시 갱신마다 저장하지 않고 설정 변경과 구독 종료만 반영한다', () => {
        const fixture = create_storage_fixture();
        const facade = new UiApplicationFacade(new FakeUiCommandAdapter(), { today: '2026-10-04' });
        facade.start();
        const stop = start_renderer_ui_preference_persistence(facade, 'session', fixture.storage);

        for (let index = 0; index < 100; index += 1) {
            facade.dispatch({ type: 'CHART_ACTIVE_STATE_UPDATED', state_label: `state-${index}` });
        }
        expect(fixture.storage.setItem).toHaveBeenCalledTimes(1);
        facade.dispatch({ type: 'CHART_INTERVAL_SELECTED', interval: '1d' });
        expect(fixture.storage.setItem).toHaveBeenCalledTimes(2);
        stop();
        facade.dispatch({ type: 'CHART_INTERVAL_SELECTED', interval: '1m' });
        expect(fixture.storage.setItem).toHaveBeenCalledTimes(2);
        facade.stop();
    });

    it('다른 session·잘못된 JSON·과대 값은 무시하고 허용하지 않은 필드는 복원하지 않는다', () => {
        const fixture = create_storage_fixture();
        const facade = new UiApplicationFacade(new FakeUiCommandAdapter(), { today: '2026-10-04' });
        facade.start();
        const stop = start_renderer_ui_preference_persistence(facade, 'session', fixture.storage);
        stop();
        facade.stop();
        const serialized = fixture.read()!;

        expect(load_renderer_ui_preferences('other-session', fixture.storage)).toBeNull();
        const envelope = JSON.parse(serialized);
        envelope.preferences.is_trading = true;
        envelope.preferences.token = 'must-never-restore';
        fixture.replace(JSON.stringify(envelope));
        expect(load_renderer_ui_preferences('session', fixture.storage)).not.toHaveProperty('is_trading');
        expect(load_renderer_ui_preferences('session', fixture.storage)).not.toHaveProperty('token');
        envelope.preferences.history_period = ['today'];
        fixture.replace(JSON.stringify(envelope));
        expect(load_renderer_ui_preferences('session', fixture.storage)).toBeNull();
        envelope.preferences.history_period = 'today';
        envelope.preferences.history_side = ['buy'];
        fixture.replace(JSON.stringify(envelope));
        expect(load_renderer_ui_preferences('session', fixture.storage)).toBeNull();
        envelope.preferences.history_side = 'all';
        fixture.replace('{');
        expect(load_renderer_ui_preferences('session', fixture.storage)).toBeNull();
        fixture.replace('x'.repeat(262_145));
        expect(load_renderer_ui_preferences('session', fixture.storage)).toBeNull();
        envelope.preferences.chart_drawings['1m'] = [{ id: 'bad', points: [{ time: 'invalid', price: 'NaN' }] }];
        fixture.replace(JSON.stringify(envelope));
        expect(load_renderer_ui_preferences('session', fixture.storage)).toBeNull();
    });

    it('drawing 예산 초과 시 이전 설정을 제거하여 삭제된 선을 잘못 되살리지 않는다', () => {
        const fixture = create_storage_fixture();
        fixture.replace('older-preferences');
        const facade = new UiApplicationFacade(new FakeUiCommandAdapter(), {
            today: '2026-10-04', chart_drawings: {
                '1m': Array.from({ length: 1_001 }, () => CHART_DRAWING_FIXTURE), '30m': [], '4h': [], '1d': [],
            },
        });
        facade.start();
        const stop = start_renderer_ui_preference_persistence(facade, 'session', fixture.storage);

        expect(fixture.storage.setItem).not.toHaveBeenCalled();
        expect(fixture.storage.removeItem).toHaveBeenCalledTimes(1);
        expect(fixture.read()).toBeNull();
        stop();
        facade.stop();
    });

    it('저장소 접근·기록 거부가 bootstrap이나 Controller 구독을 중단하지 않는다', () => {
        const fixture = create_storage_fixture();
        fixture.replace('older-preferences');
        fixture.storage.getItem.mockImplementation(() => { throw new Error('storage denied'); });
        fixture.storage.setItem.mockImplementation(() => { throw new Error('quota'); });
        expect(load_renderer_ui_preferences('session', fixture.storage)).toBeNull();
        const facade = new UiApplicationFacade(new FakeUiCommandAdapter(), { today: '2026-10-04' });
        facade.start();
        const stop = start_renderer_ui_preference_persistence(facade, 'session', fixture.storage);
        expect(() => facade.dispatch({ type: 'CHART_INTERVAL_SELECTED', interval: '1m' })).not.toThrow();
        expect(facade.get_view_model().chart.interval).toBe('1m');
        expect(fixture.read()).toBeNull();
        stop();
        facade.stop();
        expect(get_renderer_preference_storage()).toBeNull();
    });
});
