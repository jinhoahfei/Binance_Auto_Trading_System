import { readFileSync } from 'node:fs';
import { createActor } from 'xstate';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { create_ui_application_machine } from './uiApplicationMachine';
import {
    select_app_view_model,
    type UiApplicationFacadeOptions,
} from '../control/UiApplicationFacade';
import {
    FakeUiCommandAdapter,
    CHART_DRAWING_FIXTURE,
    TRADE_RECORD_FIXTURES,
} from '../../shared/testing';
import type { FeatureKey } from './uiApplicationTypes';
import type {
    ChartInterval,
    CsvPeriod,
    HistoryPeriod,
    TradeSideFilter,
} from '../../shared/contracts';
import { details_screen_path, main_screen_path, value_at } from './uiApplicationViews';

// 각 설계 ID를 실제 루트의 상태·데이터·명령 결과를 검증하는 시나리오에 연결한다.
// meta.spec_ids의 존재 여부와 별도로 실제 동작의 검증 여부를 기록한다.
const covered_spec_ids = new Set<string>();
const actors: Array<{
    stop(): void;
}> = [];


/**
 * 함수 이름: create_spec_ids()
 * 기능: 표 접두사와 행 개수로 연속된 설계 ID 목록을 만든다.
 * 인자: prefix -> 표 접두사, count -> 행 개수
 * 반환값: 두 자리 행 번호를 포함한 설계 ID 배열
 * 작성 날짜: 2026/09/16
 */
const create_spec_ids = (prefix: string, count: number) => Array.from({
    length: count,
}, (_, index) => `${prefix}-${String(index + 1).padStart(2, '0')}`);


/**
 * 함수 이름: register_behavior_scenario()
 * 기능: 설계 ID의 추적 목록과 실제 행동 검증 시나리오를 함께 등록한다.
 * 인자: spec_ids -> 검증할 설계 ID, label -> 시나리오 설명, run_scenario -> 검증 callback
 * 반환값: 없음
 * 작성 날짜: 2026/09/16
 */
function register_behavior_scenario(spec_ids: string[], label: string, run_scenario: () => void | Promise<void>) {
    spec_ids.forEach(id => covered_spec_ids.add(id));
    it(`${spec_ids.join(', ')}: ${label}`, run_scenario);
}


/**
 * 함수 이름: create_root_test_application()
 * 기능: 표의 동작을 검증할 실제 UI 루트와 입력 도구를 준비한다.
 * 인자: options -> 초기 UI 설정, command_adapter -> 테스트용 명령 adapter
 * 반환값: 루트 actor·adapter·화면 이동·이벤트·화면 조회 도구
 * 작성 날짜: 2026/09/16
 */
function create_root_test_application(options: Partial<UiApplicationFacadeOptions> = {}, command_adapter = new FakeUiCommandAdapter()) {
    const actor = createActor(create_ui_application_machine(command_adapter, {
        today: '2026-09-16',
        ...options,
    }));

    actors.push(actor);
    actor.start();

    /**
     * 함수 이름: send_feature_event()
     * 기능: 기능 이름과 원래 event를 루트 내부 이벤트로 묶어 전달한다.
     * 인자: feature -> 이벤트 대상 기능, source -> 원래 이벤트와 데이터
     * 반환값: 없음
     * 작성 날짜: 2026/09/16
     */
    const send_feature_event = (feature: FeatureKey | 'shell', source: {
        type: string;
        [key: string]: unknown;
    }) => actor.send({
        type: `${feature}.${source.type}`,
        source,
    });

    /**
     * 함수 이름: show_trade_history()
     * 기능: 상세 화면으로 이동하고 상세 조회 진입 이벤트를 전달한다.
     * 인자: 없음
     * 반환값: 없음
     * 작성 날짜: 2026/09/16
     */
    const show_trade_history = () => {
        send_feature_event('shell', {
            type: 'SHOW_ALL_TRADING_DETAILS',
        });
        send_feature_event('trade_history', {
            type: 'ENTER_TRADE_HISTORY',
        });
    };

    /**
     * 함수 이름: open_csv_dialog()
     * 기능: 상세 화면을 준비하고 CSV 설정 팝업을 연다.
     * 인자: 없음
     * 반환값: 없음
     * 작성 날짜: 2026/09/16
     */
    const open_csv_dialog = () => {
        show_trade_history();
        send_feature_event('csv_export', {
            type: 'CSV_EXPORT_CLICKED',
        });
    };

    return {
        actor,
        port: command_adapter,
        send: send_feature_event,
        details: show_trade_history,
        csv: open_csv_dialog,
        view: () => select_app_view_model(actor.getSnapshot()),
    };
}


/**
 * 함수 이름: wait_for_event_settlement()
 * 기능: 명령 Promise와 결과 이벤트가 처리될 microtask를 진행한다.
 * 인자: 없음
 * 반환값: 이벤트 처리 대기가 끝나는 Promise
 * 작성 날짜: 2026/09/16
 */
async function wait_for_event_settlement() {
    for (let microtask_index = 0; microtask_index < 16; microtask_index++) {
        await Promise.resolve();
    }
}

afterEach(() => {
    actors.splice(0).forEach(actor => actor.stop());
    vi.useRealTimers();
});

describe('182 event-action rows executed by the UI root', () => {
    register_behavior_scenario(create_spec_ids('U1', 3), 'API initial, connected and disconnected display', () => {
        const { send: send_feature_event, view: read_view_model } = create_root_test_application();

        expect(read_view_model().connection.is_online).toBe(false);

        send_feature_event('connection', {
            type: 'API_CONNECTED',
        });

        expect(read_view_model().connection.is_online).toBe(true);

        send_feature_event('connection', {
            type: 'API_DISCONNECTED',
        });

        expect(read_view_model().connection.is_online).toBe(false);
    });

    register_behavior_scenario(['U2-01', 'U2-04', 'U3-01', 'U3-03', 'U3-08'], 'idle buttons, no-regime notice and highlight', () => {
        const { send: send_feature_event, view: read_view_model, actor, port: command_adapter } = create_root_test_application();

        expect(read_view_model().trading.is_trading).toBe(false);
        expect(read_view_model().active_modal).toBeNull();

        send_feature_event('trading', {
            type: 'STOP_BUTTON_CLICKED',
            has_open_position: false,
        });

        expect(actor.getSnapshot().context.features.trading.notice).toBe('not_running');

        send_feature_event('trading', {
            type: 'START_BUTTON_CLICKED',
            regime: null,
            is_online: true,
        });

        expect(read_view_model().active_modal).toBe('select_regime_notice');

        send_feature_event('trading', {
            type: 'SELECT_REGIME_NOTICE_CONFIRMED',
        });
        send_feature_event('regime', {
            type: 'HIGHLIGHT_REQUESTED',
        });

        expect(read_view_model().active_modal).toBeNull();
        expect(read_view_model().regime.is_highlighted).toBe(true);
        expect(command_adapter.command_records).toEqual([]);
    });

    register_behavior_scenario(['U3-02', 'U3-05', 'U3-07'], 'start cancel, confirm and duplicate confirm', async () => {
        const { send: send_feature_event, view: read_view_model, port: command_adapter } = create_root_test_application({
            command_enabled: true,
        });

        /**
         * 함수 이름: click_scenario_button()
         * 기능: 현재 시나리오가 검증하는 버튼 입력을 동일한 조건으로 다시 전달한다.
         * 인자: 없음
         * 반환값: 없음
         * 작성 날짜: 2026/09/16
         */
        const click_scenario_button = () => send_feature_event('trading', {
            type: 'START_BUTTON_CLICKED',
            regime: 'type0',
            is_online: true,
        });

        click_scenario_button();

        expect(read_view_model().active_modal).toBe('start_confirmation');

        send_feature_event('trading', {
            type: 'START_CANCELED',
        });

        expect(read_view_model().active_modal).toBeNull();
        expect(command_adapter.command_records).toEqual([]);

        click_scenario_button();
        send_feature_event('trading', {
            type: 'START_CONFIRMED',
            is_online: true,
        });
        send_feature_event('trading', {
            type: 'START_CONFIRMED',
            is_online: true,
        });

        expect(read_view_model().trading.is_pending).toBe(true);

        await wait_for_event_settlement();

        expect(read_view_model().trading.is_trading).toBe(true);
        expect(read_view_model().active_modal).toBeNull();
        expect(command_adapter.command_records).toEqual([
            {
                name: 'start_trading',
                payload: {
                    regime_type: 'type0',
                },
            },
        ]);
    });

    register_behavior_scenario(['U3-04', 'U3-06', 'U3-09', 'U3-12'], 'offline request, connection lost before confirm, disconnect stop', async () => {
        const { send: send_feature_event, view: read_view_model, port: command_adapter } = create_root_test_application({
            command_enabled: true,
        });

        send_feature_event('trading', {
            type: 'START_BUTTON_CLICKED',
            regime: 'type0',
            is_online: false,
        });

        expect(read_view_model().active_modal).toBe('api_connection_required');

        send_feature_event('trading', {
            type: 'API_CONNECTION_NOTICE_CONFIRMED',
        });

        expect(read_view_model().active_modal).toBeNull();

        send_feature_event('trading', {
            type: 'START_BUTTON_CLICKED',
            regime: 'type0',
            is_online: true,
        });
        send_feature_event('trading', {
            type: 'START_CONFIRMED',
            is_online: false,
        });

        expect(read_view_model().active_modal).toBe('api_connection_required');
        expect(command_adapter.command_records).toEqual([]);

        send_feature_event('trading', {
            type: 'BACKEND_TRADING_STARTED',
        });
        send_feature_event('trading', {
            type: 'API_DISCONNECTED',
        });
        await wait_for_event_settlement();

        expect(command_adapter.command_records).toEqual([
            {
                name: 'stop_trading',
                payload: null,
            },
        ]);
        expect(read_view_model().active_modal).toBe('api_connection_required');
        expect(read_view_model().trading.lifecycle_status).toBe('stopping');

        send_feature_event('trading', {
            type: 'API_CONNECTION_NOTICE_CONFIRMED',
        });

        expect(read_view_model().trading.is_pending).toBe(true);
    });

    register_behavior_scenario(['U2-02', 'U2-05', 'U2-06', 'U3-11'], 'stop cancel then successful stop updates both buttons', async () => {
        const { send: send_feature_event, view: read_view_model, port: command_adapter } = create_root_test_application({
            is_trading: true,
        });

        send_feature_event('trading', {
            type: 'STOP_BUTTON_CLICKED',
            has_open_position: false,
        });

        expect(read_view_model().active_modal).toBe('stop_confirmation');

        send_feature_event('trading', {
            type: 'STOP_CANCELED',
        });

        expect(read_view_model().active_modal).toBeNull();
        expect(read_view_model().trading.is_trading).toBe(true);

        send_feature_event('trading', {
            type: 'STOP_BUTTON_CLICKED',
            has_open_position: false,
        });
        send_feature_event('trading', {
            type: 'STOP_CONFIRMED',
        });
        send_feature_event('trading', {
            type: 'STOP_CONFIRMED',
        });
        await wait_for_event_settlement();

        expect(read_view_model().trading.is_trading).toBe(false);
        expect(command_adapter.command_records).toEqual([
            {
                name: 'stop_trading',
                payload: null,
            },
        ]);
    });

    register_behavior_scenario(['U2-03', 'U2-07', 'U2-08', 'U2-09', 'U2-10', 'U3-10'], 'force sell cancel, failure, retry and terminal completion', async () => {
        const { send: send_feature_event, view: read_view_model, port: command_adapter } = create_root_test_application({
            is_trading: true,
            has_open_position: true,
        });

        /**
         * 함수 이름: click_scenario_button()
         * 기능: 현재 시나리오가 검증하는 버튼 입력을 동일한 조건으로 다시 전달한다.
         * 인자: 없음
         * 반환값: 없음
         * 작성 날짜: 2026/09/16
         */
        const click_scenario_button = () => send_feature_event('trading', {
            type: 'STOP_BUTTON_CLICKED',
            has_open_position: true,
        });

        click_scenario_button();

        expect(read_view_model().active_modal).toBe('force_sell_stop_confirmation');

        send_feature_event('trading', {
            type: 'FORCE_SELL_AND_STOP_CANCELED',
        });

        expect(read_view_model().active_modal).toBeNull();
        expect(command_adapter.command_records).toEqual([]);

        command_adapter.queue_failure('force_sell_and_stop', new Error('sale failed'));
        click_scenario_button();
        send_feature_event('trading', {
            type: 'FORCE_SELL_AND_STOP_CONFIRMED',
        });

        expect(read_view_model().trading.is_pending).toBe(true);

        await wait_for_event_settlement();

        expect(read_view_model().trading.error?.message).toBe('sale failed');
        expect(read_view_model().active_modal).toBe('force_sell_stop_confirmation');
        expect(read_view_model().trading.has_open_position).toBe(true);

        send_feature_event('trading', {
            type: 'FORCE_SELL_AND_STOP_CONFIRMED',
        });
        await wait_for_event_settlement();

        expect(read_view_model().trading).toMatchObject({
            is_trading: false,
            has_open_position: false,
            is_pending: false,
        });
        expect(command_adapter.command_records.filter(command_record => command_record.name === 'force_sell_and_stop')).toHaveLength(2);
    });

    register_behavior_scenario(create_spec_ids('ES2', 3), 'main, details and real deep-history return', () => {
        const { actor, send: send_feature_event, details: show_trade_history, view: read_view_model } = create_root_test_application();

        expect(read_view_model().route).toBe('dashboard');

        send_feature_event('chart', {
            type: '4_H_BUTTON_CLICKED',
        });

        const previous = value_at(actor.getSnapshot().value, main_screen_path);

        show_trade_history();

        expect(read_view_model().route).toBe('trade_history');
        expect(value_at(actor.getSnapshot().value, main_screen_path)).toBeUndefined();

        send_feature_event('shell', {
            type: 'BACK_TO_MAIN_SCREEN',
        });

        expect(read_view_model().route).toBe('dashboard');
        expect(value_at(actor.getSnapshot().value, main_screen_path)).toEqual(previous);
    });

    register_behavior_scenario([...create_spec_ids('R1', 2), ...create_spec_ids('R3', 2)], 'recommendation and indicator display are independent', () => {
        const { send: send_feature_event, view: read_view_model } = create_root_test_application();

        expect(read_view_model().regime).toMatchObject({
            recommended: null,
            metrics: [],
        });

        send_feature_event('regime', {
            type: 'TYPE_RECOMMENDED',
            regime: 'type3',
        });

        const metrics = [
            {
                label: 'return',
                value: '+1.2%',
                tone: 'positive',
            },
        ];

        send_feature_event('regime', {
            type: 'REGIME_INDICATOR_UPDATED',
            metrics,
        });

        expect(read_view_model().regime.recommended).toBe('type3');
        expect(read_view_model().regime.metrics).toEqual(metrics);
        expect(read_view_model().regime.applied).toBeNull();
    });

    register_behavior_scenario(create_spec_ids('R2', 5), 'CR-03 confirmation in both stopped and running states; cancel and failed apply preserve selection', async () => {
        for (const running of [false, true]) {
            const { send: send_feature_event, view: read_view_model, port: command_adapter } = create_root_test_application({
                is_trading: running,
            });

            expect(read_view_model().regime.applied).toBeNull();

            send_feature_event('regime', {
                type: 'TYPE_CLICKED',
                regime: 'type0',
            });

            expect(read_view_model().regime.candidate).toBe('type0');
            expect(read_view_model().active_modal).toBe('regime_change_confirmation');
            expect(command_adapter.command_records).toEqual([]);

            send_feature_event('regime', {
                type: 'CANCEL_TYPE_CHANGE',
            });

            expect(read_view_model().regime.candidate).toBeNull();
            expect(read_view_model().regime.applied).toBeNull();

            send_feature_event('regime', {
                type: 'TYPE_CLICKED',
                regime: 'type0',
            });
            send_feature_event('regime', {
                type: 'CONFIRM_TYPE_CHANGE',
            });
            await wait_for_event_settlement();

            expect(read_view_model().regime.applied).toBe('type0');
            expect(read_view_model().active_modal).toBeNull();

            command_adapter.queue_failure('apply_regime', new Error('apply failed'));
            send_feature_event('regime', {
                type: 'TYPE_CLICKED',
                regime: 'type1',
            });
            send_feature_event('regime', {
                type: 'CONFIRM_TYPE_CHANGE',
            });
            await wait_for_event_settlement();

            expect(read_view_model().regime.applied).toBe('type0');
            expect(read_view_model().regime.error?.message).toBe('apply failed');

            send_feature_event('regime', {
                type: 'CANCEL_TYPE_CHANGE',
            });

            expect(read_view_model().regime.applied).toBe('type0');
        }
    });

    const intervals: ChartInterval[] = ['30m', '1m', '4h', '1d'];
    const interval_event = {
        '30m': '30_M_BUTTON_CLICKED',
        '1m': '1_M_BUTTON_CLICKED',
        '4h': '4_H_BUTTON_CLICKED',
        '1d': '1_DAY_BUTTON_CLICKED',
    };

    register_behavior_scenario(['DC1-01'], 'default interval and empty drawings', () => {
        const { view: read_view_model } = create_root_test_application();

        expect(read_view_model().chart.interval).toBe('30m');
        expect(read_view_model().chart.drawings).toEqual([]);
    });

    let interval_id = 2;

    for (const initial_selection of intervals) {
        for (const next_selection of (['1m', '30m', '4h', '1d'] as const).filter(value => value !== initial_selection)) {
            register_behavior_scenario([`DC1-${String(interval_id++).padStart(2, '0')}`], `${initial_selection} → ${next_selection}, interval drawings restored`, () => {
                const { send: send_feature_event, view: read_view_model } = create_root_test_application({
                    chart_interval: initial_selection,
                });

                send_feature_event('chart', {
                    type: 'DRAWING_TOOL_CLICKED',
                });
                send_feature_event('chart', {
                    type: 'USER_START_DRAWING',
                });
                send_feature_event('chart', {
                    type: 'USER_FINISH_DRAWING',
                    drawing: CHART_DRAWING_FIXTURE,
                });
                send_feature_event('chart', {
                    type: interval_event[next_selection],
                });

                expect(read_view_model().chart.interval).toBe(next_selection);
                expect(read_view_model().chart.drawings).toEqual([]);

                send_feature_event('chart', {
                    type: interval_event[initial_selection],
                });

                expect(read_view_model().chart.drawings).toEqual([CHART_DRAWING_FIXTURE]);
            });
        }
    }

    register_behavior_scenario(create_spec_ids('DC2', 3), 'indicator popup open and outside close', () => {
        const { send: send_feature_event, view: read_view_model } = create_root_test_application();

        expect(read_view_model().chart.is_indicator_settings_open).toBe(false);

        send_feature_event('chart', {
            type: 'INDICATOR_SETTINGS_BUTTON_CLICKED',
        });

        expect(read_view_model().chart.is_indicator_settings_open).toBe(true);

        send_feature_event('chart', {
            type: 'INDICATOR_POPUP_OUTSIDE_CLICKED',
        });

        expect(read_view_model().chart.is_indicator_settings_open).toBe(false);
    });

    for (const [index, name, event] of [[1, 'bollinger_bands', 'BB'], [2, 'ema9', 'EMA'], [3, 'volume', 'VOLUME']] as const) {
        register_behavior_scenario(create_spec_ids(`IP${index}`, 5), `${name} restores both initial choices and changes without touching other indicators`, () => {
            for (const initial of [false, true]) {
                const { send: send_feature_event, view: read_view_model } = create_root_test_application({
                    chart_indicators: {
                        [name]: initial,
                    },
                });

                send_feature_event('chart', {
                    type: 'INDICATOR_SETTINGS_BUTTON_CLICKED',
                });

                expect(read_view_model().chart.indicators[name]).toBe(initial);

                const others = {
                    ...read_view_model().chart.indicators,
                };

                delete (others as Partial<typeof others>)[name];

                for (const visible of [true, false]) {
                    send_feature_event('chart', {
                        type: `${event}_DISPLAY_${visible ? 'ON' : 'OFF'}_CLICKED`,
                    });

                    expect(read_view_model().chart.indicators[name]).toBe(visible);
                    expect(read_view_model().chart.indicators).toMatchObject(others);
                }
            }
        });
    }

    register_behavior_scenario(create_spec_ids('DC3', 2), 'active strategy label initialized and updated', () => {
        const { send: send_feature_event, view: read_view_model } = create_root_test_application();

        expect(typeof read_view_model().chart.active_trading_logic_state).toBe('string');

        send_feature_event('chart', {
            type: 'TRADING_LOGIC_STATE_CHANGED',
            state_label: 'C_WAIT',
        });

        expect(read_view_model().chart.active_trading_logic_state).toBe('C_WAIT');
    });

    register_behavior_scenario(create_spec_ids('DC4', 3), 'full-screen preserves current interval and restores normal size', () => {
        const { send: send_feature_event, view: read_view_model } = create_root_test_application({
            chart_interval: '4h',
        });

        expect(read_view_model().chart.is_fullscreen).toBe(false);

        send_feature_event('chart', {
            type: 'FULL_SIZE_SELECTED',
        });

        expect(read_view_model().chart.is_fullscreen).toBe(true);
        expect(read_view_model().chart.interval).toBe('4h');

        send_feature_event('chart', {
            type: 'NORMAL_SIZE_SELECTED',
        });

        expect(read_view_model().chart.is_fullscreen).toBe(false);
        expect(read_view_model().chart.interval).toBe('4h');
    });

    register_behavior_scenario(create_spec_ids('DC5', 7), 'drawing start, finish, cancel and disable', () => {
        const { send: send_feature_event, view: read_view_model } = create_root_test_application();

        expect(read_view_model().chart.drawing_mode).toBe('deactivated');

        send_feature_event('chart', {
            type: 'DRAWING_TOOL_CLICKED',
        });

        expect(read_view_model().chart.drawing_mode).toBe('waiting');

        send_feature_event('chart', {
            type: 'DRAWING_TOOL_CLICKED',
        });

        expect(read_view_model().chart.drawing_mode).toBe('deactivated');

        send_feature_event('chart', {
            type: 'DRAWING_TOOL_CLICKED',
        });
        send_feature_event('chart', {
            type: 'USER_START_DRAWING',
        });

        expect(read_view_model().chart.drawing_mode).toBe('drawing');

        send_feature_event('chart', {
            type: 'USER_FINISH_DRAWING',
            drawing: CHART_DRAWING_FIXTURE,
        });

        expect(read_view_model().chart.drawings).toHaveLength(1);
        expect(read_view_model().chart.drawing_mode).toBe('waiting');

        send_feature_event('chart', {
            type: 'USER_START_DRAWING',
        });
        send_feature_event('chart', {
            type: 'DRAWING_CANCELED',
        });

        expect(read_view_model().chart.drawing_mode).toBe('waiting');
        expect(read_view_model().chart.drawings).toHaveLength(1);

        send_feature_event('chart', {
            type: 'USER_START_DRAWING',
        });
        send_feature_event('chart', {
            type: 'DRAWING_TOOL_CLICKED',
        });

        expect(read_view_model().chart.drawing_mode).toBe('deactivated');
    });

    register_behavior_scenario(create_spec_ids('DC6', 6), 'line hover, context menu outside close and delete with drawing guards', () => {
        const { send: send_feature_event, view: read_view_model } = create_root_test_application();

        expect(read_view_model().chart.line_selection_state).toBe('awaiting_selection');

        send_feature_event('chart', {
            type: 'DRAWING_TOOL_CLICKED',
        });
        send_feature_event('chart', {
            type: 'USER_START_DRAWING',
        });
        send_feature_event('chart', {
            type: 'CURSOR_HOVER_ENTER',
            line_id: CHART_DRAWING_FIXTURE.id,
        });

        expect(read_view_model().chart.selected_line_id).toBeNull();

        send_feature_event('chart', {
            type: 'USER_FINISH_DRAWING',
            drawing: CHART_DRAWING_FIXTURE,
        });
        send_feature_event('chart', {
            type: 'DRAWING_TOOL_CLICKED',
        });

        /**
         * 함수 이름: hover_drawing_line()
         * 기능: 저장한 테스트 선 위로 커서가 들어온 이벤트를 전달한다.
         * 인자: 없음
         * 반환값: 없음
         * 작성 날짜: 2026/09/16
         */
        const hover_drawing_line = () => send_feature_event('chart', {
            type: 'CURSOR_HOVER_ENTER',
            line_id: CHART_DRAWING_FIXTURE.id,
        });

        hover_drawing_line();

        expect(read_view_model().chart.line_selection_state).toBe('highlighted');

        send_feature_event('chart', {
            type: 'CURSOR_HOVER_EXIT',
        });

        expect(read_view_model().chart.selected_line_id).toBeNull();

        hover_drawing_line();
        send_feature_event('chart', {
            type: 'DRAWING_TOOL_CLICKED',
        });

        expect(read_view_model().chart.drawing_mode).toBe('deactivated');

        send_feature_event('chart', {
            type: 'HIGHLIGHTED_LINE_RIGHT_CLICKED',
            x: 12,
            y: 20,
        });

        expect(read_view_model().chart.context_menu_position).toEqual({
            x: 12,
            y: 20,
        });

        send_feature_event('chart', {
            type: 'CONTEXT_MENU_OUTSIDE_CLICKED',
        });

        expect(read_view_model().chart.line_selection_state).toBe('awaiting_selection');

        hover_drawing_line();
        send_feature_event('chart', {
            type: 'HIGHLIGHTED_LINE_RIGHT_CLICKED',
        });
        send_feature_event('chart', {
            type: 'DELETE_LINE',
        });

        expect(read_view_model().chart.drawings).toEqual([]);
        expect(read_view_model().chart.selected_line_id).toBeNull();
    });

    register_behavior_scenario([...create_spec_ids('DI1', 2), ...create_spec_ids('DI2', 2)], 'account strategy and asset projections', () => {
        const { send: send_feature_event, view: read_view_model } = create_root_test_application();
        const old = read_view_model().account_summary;

        expect(old.strategy).toBeDefined();
        expect(old.asset).toBeDefined();

        const strategy = {
            ...old.strategy,
            appliedState: 'C_WAIT',
            profitRate: '+2%',
        };
        const asset = {
            ...old.asset,
            ethAmount: '3',
            quoteValue: '500 USDT',
        };

        send_feature_event('account_summary', {
            type: 'TRADING_STATUS_UPDATED',
            strategy,
        });

        expect(read_view_model().account_summary).toEqual({
            strategy,
            asset: old.asset,
        });

        send_feature_event('account_summary', {
            type: 'ASSET_SUMMARY_UPDATED',
            asset,
        });

        expect(read_view_model().account_summary).toEqual({
            strategy,
            asset,
        });
    });

    register_behavior_scenario([...create_spec_ids('SI', 2), ...create_spec_ids('SO', 2)], 'split initialization, immediate values and unchanged command payloads', async () => {
        const { send: send_feature_event, view: read_view_model, port: command_adapter } = create_root_test_application({
            scale_in_percentage: 30,
            scale_out_percentage: 70,
        });

        expect(read_view_model().split_order).toMatchObject({
            scale_in_percentage: 30,
            scale_out_percentage: 70,
        });

        send_feature_event('split_order', {
            type: 'SCALE_IN_LEVEL_CHANGED',
            percentage: 45,
        });

        expect(read_view_model().split_order.scale_in_percentage).toBe(45);

        send_feature_event('split_order', {
            type: 'SCALE_OUT_LEVEL_CHANGED',
            percentage: 25,
        });

        expect(read_view_model().split_order.scale_out_percentage).toBe(25);

        await wait_for_event_settlement();

        expect(command_adapter.command_records).toEqual([
            {
                name: 'update_split_order',
                payload: {
                    order_side: 'scale_in',
                    percentage: 45,
                },
            },
            {
                name: 'update_split_order',
                payload: {
                    order_side: 'scale_out',
                    percentage: 25,
                },
            },
        ]);
        expect(read_view_model().split_order.is_pending).toBe(false);
    });

    register_behavior_scenario(create_spec_ids('M4', 9), 'recent fills and live tab retain and update their data', () => {
        const { send: send_feature_event, view: read_view_model } = create_root_test_application({
            recent_trades: [TRADE_RECORD_FIXTURES[0]!],
        });

        expect(read_view_model().trader_panel.active_tab).toBe('recent_orders');
        expect(read_view_model().trader_panel.trades).toHaveLength(1);

        for (const tab of ['recent_orders', 'realtime_indicators']) {
            if (tab === 'realtime_indicators') {
                send_feature_event('recent_orders', {
                    type: 'REALTIME_INDICATOR_CLICKED',
                });
            }

            send_feature_event('recent_orders', {
                type: 'BUY_ORDER_EXECUTED',
                trade: TRADE_RECORD_FIXTURES[1],
            });

            expect(read_view_model().trader_panel.trades[0]).toEqual(TRADE_RECORD_FIXTURES[1]);

            send_feature_event('recent_orders', {
                type: 'SELL_ORDER_EXECUTED',
                trade: TRADE_RECORD_FIXTURES[0],
            });

            expect(read_view_model().trader_panel.trades[0]).toEqual(TRADE_RECORD_FIXTURES[0]);
            expect(read_view_model().trader_panel.active_tab).toBe(tab);
        }

        for (const type of ['REALTIME_INDICATOR_UPDATED', 'TRADING_STATUS_UPDATED']) {
            const indicators = [
                {
                    label: type,
                    value: '1',
                    tone: 'neutral',
                },
            ];

            send_feature_event('recent_orders', {
                type,
                indicators,
            });

            expect(read_view_model().trader_panel.realtime_indicators).toEqual(indicators);
        }

        send_feature_event('recent_orders', {
            type: 'TRADING_HISTORY_CLICKED',
        });

        expect(read_view_model().trader_panel.active_tab).toBe('recent_orders');
        expect(read_view_model().trader_panel.trades).toHaveLength(5);
    });

    register_behavior_scenario([
        ...create_spec_ids('D1', 2),
        ...create_spec_ids('D2', 2),
        ...create_spec_ids('D3', 3),
        ...create_spec_ids('D4', 2),
    ], 'four summary regions update while visible or hidden', () => {
        for (const visible of [false, true]) {
            const { send: send_feature_event, details: show_trade_history, view: read_view_model } = create_root_test_application();

            if (visible) {
                show_trade_history();
            }

            const initial = read_view_model().trade_history.summary;

            expect(initial.dailyReturn).toBeDefined();
            expect(initial.sellPerformance).toBeDefined();
            expect(initial.position).toBeDefined();
            expect(initial.fees).toBeDefined();

            send_feature_event('trade_history_summary', {
                type: 'PROFIT_RATE_UPDATED',
                daily_return: {
                    value: '+3%',
                    tone: 'positive',
                },
            });

            expect(read_view_model().trade_history.summary.dailyReturn.value).toBe('+3%');

            send_feature_event('trade_history_summary', {
                type: 'BUY_ORDER_EXECUTED',
                position: {
                    quantity: '2 ETH',
                },
            });

            expect(read_view_model().trade_history.summary.position.quantity).toBe('2 ETH');

            const sell = {
                ...initial.sellPerformance,
                completedCount: '3 / 4',
            };

            send_feature_event('trade_history_summary', {
                type: 'SELL_ORDER_EXECUTED',
                sell_performance: sell,
                position: {
                    quantity: '1 ETH',
                },
            });

            expect(read_view_model().trade_history.summary.sellPerformance).toEqual(sell);
            expect(read_view_model().trade_history.summary.position.quantity).toBe('1 ETH');

            const fees = {
                ...initial.fees,
                amount: '2 USDT',
            };

            send_feature_event('trade_history_summary', {
                type: 'DAILY_TRADING_FEE_CHANGED',
                fees,
            });

            expect(read_view_model().trade_history.summary.fees).toEqual(fees);
        }
    });

    const periods: HistoryPeriod[] = ['today', 'last7days', 'last30days', 'all'];
    const period_event = {
        today: 'SELECT_DISPLAY_TODAY_HISTORY',
        last7days: 'SELECT_DISPLAY_WEEKLY_HISTORY',
        last30days: 'SELECT_DISPLAY_MONTHLY_HISTORY',
        all: 'SELECT_DISPLAY_ALL_HISTORY',
    };

    register_behavior_scenario(['TD2-01', 'TD3-01'], 'first details entry queries today/all exactly once', async () => {
        const { details: show_trade_history, port: command_adapter, view: read_view_model } = create_root_test_application();

        show_trade_history();
        await wait_for_event_settlement();

        expect(read_view_model().trade_history).toMatchObject({
            period: 'today',
            side: 'all',
            status: 'ready',
        });
        expect(command_adapter.command_records).toEqual([
            {
                name: 'load_trade_history',
                payload: {
                    period: 'today',
                    side: 'all',
                },
            },
        ]);
    });

    let period_id = 2;

    for (const initial_selection of periods) {
        for (const next_selection of periods.filter(value => value !== initial_selection)) {
            register_behavior_scenario([`TD2-${String(period_id++).padStart(2, '0')}`], `history ${initial_selection} → ${next_selection} preserves side`, async () => {
                const { send: send_feature_event, details: show_trade_history, view: read_view_model, port: command_adapter } = create_root_test_application();

                show_trade_history();
                await wait_for_event_settlement();
                send_feature_event('trade_history', {
                    type: 'SELL_TRADE_HISTORY_SELECTED',
                });
                await wait_for_event_settlement();
                send_feature_event('trade_history', {
                    type: period_event[initial_selection],
                });
                await wait_for_event_settlement();
                command_adapter.command_records.splice(0);
                send_feature_event('trade_history', {
                    type: period_event[next_selection],
                });
                await wait_for_event_settlement();

                expect(read_view_model().trade_history).toMatchObject({
                    period: next_selection,
                    side: 'sell',
                });
                expect(command_adapter.command_records).toEqual([
                    {
                        name: 'load_trade_history',
                        payload: {
                            period: next_selection,
                            side: 'sell',
                        },
                    },
                ]);
            });
        }
    }

    const sides: TradeSideFilter[] = ['all', 'buy', 'sell'];
    const side_event = {
        all: 'ALL_TRADE_HISTORY_SELECTED',
        buy: 'BUY_TRADE_HISTORY_SELECTED',
        sell: 'SELL_TRADE_HISTORY_SELECTED',
    };
    let side_id = 2;

    for (const initial_selection of sides) {
        for (const next_selection of sides.filter(value => value !== initial_selection)) {
            register_behavior_scenario([`TD3-${String(side_id++).padStart(2, '0')}`], `history ${initial_selection} → ${next_selection} preserves period`, async () => {
                const { send: send_feature_event, details: show_trade_history, view: read_view_model, port: command_adapter } = create_root_test_application();

                show_trade_history();
                await wait_for_event_settlement();
                send_feature_event('trade_history', {
                    type: period_event.last7days,
                });
                await wait_for_event_settlement();
                send_feature_event('trade_history', {
                    type: side_event[initial_selection],
                });
                await wait_for_event_settlement();
                command_adapter.command_records.splice(0);
                send_feature_event('trade_history', {
                    type: side_event[next_selection],
                });
                await wait_for_event_settlement();

                expect(read_view_model().trade_history).toMatchObject({
                    period: 'last7days',
                    side: next_selection,
                });
                expect(command_adapter.command_records).toEqual([
                    {
                        name: 'load_trade_history',
                        payload: {
                            period: 'last7days',
                            side: next_selection,
                        },
                    },
                ]);
            });
        }
    }

    register_behavior_scenario(create_spec_ids('TD4', 9), 'CSV validation, close, submit, failure return, success and dismiss', async () => {
        const { send: send_feature_event, details: show_trade_history, view: read_view_model, port: command_adapter } = create_root_test_application();

        show_trade_history();

        expect(read_view_model().csv_export.status).toBe('closed');

        send_feature_event('csv_export', {
            type: 'CSV_EXPORT_CLICKED',
        });

        expect(read_view_model().csv_export.status).toBe('editing');

        send_feature_event('csv_export', {
            type: 'CLOSE_CSV_EXPORT_POPUP',
        });

        expect(read_view_model().csv_export.status).toBe('closed');

        send_feature_event('csv_export', {
            type: 'CSV_EXPORT_CLICKED',
        });
        send_feature_event('csv_export', {
            type: 'EXPORT_CSV',
        });

        expect(read_view_model().csv_export.validation_errors.directory).not.toBeNull();
        expect(command_adapter.command_records.filter(command_record => command_record.name === 'export_csv')).toHaveLength(0);

        send_feature_event('csv_export', {
            type: 'SAVE_LOCATION_SELECT_CLICKED',
        });
        await wait_for_event_settlement();
        command_adapter.queue_failure('export_csv', new Error('disk full'));
        send_feature_event('csv_export', {
            type: 'EXPORT_CSV',
        });

        expect(read_view_model().csv_export.status).toBe('exporting');

        await wait_for_event_settlement();

        expect(read_view_model().csv_export.status).toBe('error');
        expect(read_view_model().csv_export.command_error?.message).toBe('disk full');

        const directory = read_view_model().csv_export.directory;

        send_feature_event('csv_export', {
            type: 'CSV_EXPORT_ERROR_CONFIRMED',
        });

        expect(read_view_model().csv_export.status).toBe('editing');
        expect(read_view_model().csv_export.directory).toBe(directory);

        send_feature_event('csv_export', {
            type: 'EXPORT_CSV',
        });
        await wait_for_event_settlement();

        expect(read_view_model().csv_export.status).toBe('complete');
        expect(read_view_model().csv_export.receipt_path).toBe(command_adapter.exported_receipt.file_path);

        send_feature_event('csv_export', {
            type: 'ACCEPT_CLOSE_ALL_POPUP',
        });

        expect(read_view_model().csv_export.status).toBe('closed');
    });

    register_behavior_scenario(create_spec_ids('CR1', 4), 'directory picker success and cancellation preserve path', async () => {
        const { send: send_feature_event, csv: open_csv_dialog, port: command_adapter, view: read_view_model, actor } = create_root_test_application();

        open_csv_dialog();

        expect(value_at(actor.getSnapshot().value, [...details_screen_path, 'CSV_EXPORT', 'editing', 'file_browser'])).toBe('closed');

        send_feature_event('csv_export', {
            type: 'SAVE_LOCATION_SELECT_CLICKED',
        });

        expect(read_view_model().csv_export.status).toBe('picking_directory');

        await wait_for_event_settlement();

        expect(read_view_model().csv_export.directory).toBe(command_adapter.selected_directory);

        const selected = read_view_model().csv_export.directory;

        command_adapter.selected_directory = null;
        send_feature_event('csv_export', {
            type: 'SAVE_LOCATION_SELECT_CLICKED',
        });
        await wait_for_event_settlement();

        expect(read_view_model().csv_export.directory).toBe(selected);
        expect(read_view_model().csv_export.status).toBe('editing');
    });

    const csv_periods: CsvPeriod[] = ['today', 'last7days', 'last30days', 'custom'];
    const csv_period_event = {
        today: 'SELECT_CSV_TODAY_HISTORY',
        last7days: 'SELECT_CSV_WEEKLY_HISTORY',
        last30days: 'SELECT_CSV_MONTHLY_HISTORY',
        custom: 'SELECT_CSV_DATE',
    };

    register_behavior_scenario(['CR2-01'], 'CSV opens on today', () => {
        const { csv: open_csv_dialog, view: read_view_model } = create_root_test_application();

        open_csv_dialog();

        expect(read_view_model().csv_export.period).toBe('today');
    });

    let csv_period_id = 2;

    for (const initial_selection of csv_periods) {
        for (const next_selection of csv_periods.filter(value => value !== initial_selection)) {
            register_behavior_scenario([`CR2-${String(csv_period_id++).padStart(2, '0')}`], `CSV ${initial_selection} → ${next_selection}, filename stays unchanged`, () => {
                const { send: send_feature_event, csv: open_csv_dialog, view: read_view_model } = create_root_test_application();

                open_csv_dialog();
                send_feature_event('csv_export', {
                    type: csv_period_event[initial_selection],
                });

                const filename = read_view_model().csv_export.file_name;

                send_feature_event('csv_export', {
                    type: csv_period_event[next_selection],
                });

                expect(read_view_model().csv_export.period).toBe(next_selection);
                expect(read_view_model().csv_export.file_name).toBe(filename);
                expect(read_view_model().csv_export.calendar_target).toBeNull();
            });
        }
    }

    register_behavior_scenario(create_spec_ids('CR2', 21).slice(13), 'calendar validation retains valid selection until outside click', () => {
        const { send: send_feature_event, csv: open_csv_dialog, view: read_view_model } = create_root_test_application();

        open_csv_dialog();
        send_feature_event('csv_export', {
            type: 'SELECT_CSV_DATE',
        });
        send_feature_event('csv_export', {
            type: 'START_CSV_START_DATE_SELECTION',
        });

        expect(read_view_model().csv_export.calendar_target).toBe('start_date');

        send_feature_event('csv_export', {
            type: 'START_DATE_SELECTED',
            date: '2026-09-10',
        });

        expect(read_view_model().csv_export.start_date).toBe('2026-09-10');
        expect(read_view_model().csv_export.calendar_target).toBe('start_date');

        send_feature_event('csv_export', {
            type: 'START_DATE_CALENDAR_OUTSIDE_CLICKED',
        });

        expect(read_view_model().csv_export.calendar_target).toBeNull();

        send_feature_event('csv_export', {
            type: 'START_CSV_FINISH_DATE_SELECTION',
        });

        expect(read_view_model().csv_export.calendar_target).toBe('end_date');

        send_feature_event('csv_export', {
            type: 'FINISH_DATE_SELECTED',
            date: '2026-09-09',
        });

        expect(read_view_model().csv_export.validation_errors.date_range).not.toBeNull();

        send_feature_event('csv_export', {
            type: 'FINISH_DATE_SELECTED',
            date: '2026-09-12',
        });

        expect(read_view_model().csv_export.end_date).toBe('2026-09-12');
        expect(read_view_model().csv_export.calendar_target).toBe('end_date');

        send_feature_event('csv_export', {
            type: 'FINISH_DATE_CALENDAR_OUTSIDE_CLICKED',
        });

        expect(read_view_model().csv_export.calendar_target).toBeNull();

        send_feature_event('csv_export', {
            type: 'START_CSV_START_DATE_SELECTION',
        });
        send_feature_event('csv_export', {
            type: 'START_DATE_SELECTED',
            date: '2026-09-13',
        });

        expect(read_view_model().csv_export.start_date).toBe('2026-09-10');
        expect(read_view_model().csv_export.validation_errors.date_range).not.toBeNull();
    });

    register_behavior_scenario(create_spec_ids('CR3', 7), 'default, typing, invalid enter/blur, committed filename and reediting are distinct', () => {
        const { send: send_feature_event, csv: open_csv_dialog, view: read_view_model, actor } = create_root_test_application();

        open_csv_dialog();

        /**
         * 함수 이름: read_file_name_state()
         * 기능: 루트 snapshot에서 CSV 파일명의 실제 활성 상태를 읽는다.
         * 인자: 없음
         * 반환값: 기본값·편집·확정 중 현재 파일명 상태
         * 작성 날짜: 2026/09/16
         */
        const read_file_name_state = () => value_at(actor.getSnapshot().value, [...details_screen_path, 'CSV_EXPORT', 'editing', 'file_name']);

        expect(read_file_name_state()).toBe('DEFAULT_FILE_NAME');
        expect(read_view_model().csv_export.file_name.length).toBeGreaterThan(0);

        for (const confirm of ['ENTER_KEY_TYPED', 'FILE_NAME_INPUT_FOCUS_LOST']) {
            send_feature_event('csv_export', {
                type: 'FILE_NAME_CLICKED',
            });

            expect(read_file_name_state()).toBe('editing');

            send_feature_event('csv_export', {
                type: 'FILE_NAME_CHANGED',
                file_name: '',
            });
            send_feature_event('csv_export', {
                type: confirm,
            });

            expect(read_file_name_state()).toBe('editing');
            expect(read_view_model().csv_export.validation_errors.file_name).not.toBeNull();

            send_feature_event('csv_export', {
                type: 'FILE_NAME_CHANGED',
                file_name: 'my_trades',
            });
            send_feature_event('csv_export', {
                type: confirm,
            });

            expect(read_file_name_state()).toBe('FILE_NAME_WRITTEN');
            expect(read_view_model().csv_export.file_name).toBe('my_trades.csv');
        }
    });

    register_behavior_scenario(create_spec_ids('ES3', 9), 'exit cancel, failure retry and final only after shutdown resolves', async () => {
        for (const position of [false, true]) {
            const { actor, send: send_feature_event, view: read_view_model, port: command_adapter } = create_root_test_application();

            expect(read_view_model().app_exit.status).toBe('awaiting_exit');

            /**
             * 함수 이름: click_scenario_button()
             * 기능: 현재 시나리오가 검증하는 버튼 입력을 동일한 조건으로 다시 전달한다.
             * 인자: 없음
             * 반환값: 없음
             * 작성 날짜: 2026/09/16
             */
            const click_scenario_button = () => send_feature_event('app_exit', {
                type: 'EXIT_CLICKED',
                has_open_position: position,
            });

            click_scenario_button();

            expect(read_view_model().active_modal).toBe(position ? 'force_sell_exit_confirmation' : 'exit_confirmation');

            send_feature_event('app_exit', {
                type: position ? 'FORCE_SELL_EXIT_CANCELED' : 'EXIT_CANCELED',
            });

            expect(read_view_model().app_exit.status).toBe('awaiting_exit');
            expect(command_adapter.command_records).toEqual([]);

            click_scenario_button();

            if (position) {
                command_adapter.queue_failure('shutdown_application', new Error('liquidation failed'));
            }

            send_feature_event('app_exit', {
                type: position ? 'FORCE_SELL_EXIT_CONFIRMED' : 'EXIT_CONFIRMED',
            });

            expect(read_view_model().app_exit.status).toBe('shutting_down');

            await wait_for_event_settlement();

            if (position) {
                expect(read_view_model().app_exit.is_final).toBe(false);
                expect(read_view_model().app_exit.error?.message).toBe('liquidation failed');
                expect(read_view_model().active_modal).toBe('exit_confirmation');

                send_feature_event('app_exit', {
                    type: 'EXIT_CONFIRMED',
                });
                await wait_for_event_settlement();
            }

            expect(actor.getSnapshot().value).toBe('UI_FINAL_STATE');
            expect(actor.getSnapshot().status).toBe('done');
            expect(read_view_model().app_exit.is_final).toBe(true);
            expect(command_adapter.command_records.every(command_record => command_record.name === 'shutdown_application')).toBe(true);
        }
    });

    it('accounts for every one of the 182 original table IDs with executable behavior scenarios', () => {
        const table = readFileSync('../Design/UI/UI_Event_Action_Table.md', 'utf8');
        const expected = [...table.matchAll(/^\| ([A-Z]+\d*-\d+) \|/gm)].map(match => match[1]!);

        expect(expected).toHaveLength(182);
        expect([...covered_spec_ids].sort()).toEqual(expected.sort());
    });
});
