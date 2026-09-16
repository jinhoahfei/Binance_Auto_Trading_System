import { afterEach, describe, expect, it, vi } from 'vitest';

import {
    UiApplicationFacade,
    type UiApplicationFacadeOptions,
} from '../control/UiApplicationFacade';
import {
    FakeUiCommandAdapter,
    CHART_DRAWING_FIXTURE,
    TRADE_RECORD_FIXTURES,
} from '../../shared/testing';
import {
    main_screen_path,
    details_screen_path,
    upper_status_bar_path,
    value_at,
} from './uiApplicationViews';
import type { TradeHistoryDetails } from '../../shared/contracts';
import { BackendAdapterError } from '../../shared/api';

const applications: UiApplicationFacade[] = [];


/**
 * 함수 이름: create_test_application()
 * 기능: 루트 통합 테스트용 Facade를 생성·시작하고 종료 대상에 등록한다.
 * 인자: options -> 초기 UI 설정, command_adapter -> 테스트용 명령 adapter
 * 반환값: Facade·adapter·입력 전달·화면 조회 도구
 * 작성 날짜: 2026/09/16
 */
function create_test_application(options: Partial<UiApplicationFacadeOptions> = {}, command_adapter = new FakeUiCommandAdapter()) {
    const facade = new UiApplicationFacade(command_adapter, {
        today: '2026-09-16',
        ...options,
    });

    applications.push(facade);
    facade.start();

    return {
        facade,
        port: command_adapter,
        send: facade.dispatch.bind(facade),
        view: () => facade.get_view_model(),
    };
}


/**
 * 함수 이름: wait_for_event_settlement()
 * 기능: Promise 작업과 내부 결과 이벤트가 처리될 microtask를 진행한다.
 * 인자: 없음
 * 반환값: 이벤트 처리 대기가 끝나는 Promise
 * 작성 날짜: 2026/09/16
 */
async function wait_for_event_settlement() {
    for (let count = 0; count < 16; count++) {
        await Promise.resolve();
    }
}


/**
 * 함수 이름: create_deferred_result()
 * 기능: 테스트에서 완료와 실패 시점을 직접 결정할 Promise를 만든다.
 * 인자: 없음
 * 반환값: Promise와 resolve·reject callback
 * 작성 날짜: 2026/09/16
 */
function create_deferred_result<T>() {
    let resolve!: (value: T) => void;
    let reject!: (error: Error) => void;
    const promise = new Promise<T>((resolve_promise, reject_promise) => {
        resolve = resolve_promise;
        reject = reject_promise;
    });

    return {
        promise,
        resolve,
        reject,
    };
}

afterEach(() => {
    applications.splice(0).forEach(facade => facade.stop());
    vi.useRealTimers();
});

describe('one UI root: structure, history and command ownership', () => {
    it('executes all designed region levels in the real root snapshot', async () => {
        const { facade, send: dispatch_intent } = create_test_application();
        const system = value_at(facade.get_snapshot().value, ['ETIRE_UI_SYSTEM'])!;

        expect(Object.keys(system)).toEqual(['UPPER_STATUS_BAR', 'SCREEN', 'EXIT']);
        expect(Object.keys(value_at(facade.get_snapshot().value, upper_status_bar_path)!)).toEqual(['API_DISPLAY', 'STOP_BUTTON', 'START_BUTTON']);

        const main = value_at(facade.get_snapshot().value, main_screen_path)!;

        expect(Object.keys(main)).toEqual(['REGIME_PANEL', 'DISPLAY_CHART', 'DISPLAY_ACCOUNT_INFO', 'TRADER_PANEL']);
        expect(Object.keys(value_at(main, ['REGIME_PANEL'])!)).toHaveLength(3);
        expect(Object.keys(value_at(main, ['DISPLAY_CHART'])!)).toHaveLength(6);
        expect(Object.keys(value_at(main, ['DISPLAY_ACCOUNT_INFO'])!)).toHaveLength(3);
        expect(Object.keys(value_at(main, ['DISPLAY_ACCOUNT_INFO', 'SPLIT_ORDER'])!)).toHaveLength(2);

        dispatch_intent({
            type: 'CHART_INDICATOR_SETTINGS_TOGGLED',
        });

        expect(Object.keys(value_at(facade.get_snapshot().value, [...main_screen_path, 'DISPLAY_CHART', 'indicator_settings', 'opened'])!)).toHaveLength(3);

        dispatch_intent({
            type: 'SHOW_TRADE_HISTORY',
        });
        await wait_for_event_settlement();

        expect(Object.keys(value_at(facade.get_snapshot().value, details_screen_path)!)).toEqual(['ACCOUNT_DETAILS', 'PERIOD', 'SIDE', 'CSV_EXPORT']);
        expect(Object.keys(value_at(facade.get_snapshot().value, [...details_screen_path, 'ACCOUNT_DETAILS'])!)).toHaveLength(4);

        dispatch_intent({
            type: 'HISTORY_SIDE_SELECTED',
            side: 'sell',
        });
        await wait_for_event_settlement();
        dispatch_intent({
            type: 'HISTORY_PERIOD_SELECTED',
            period: 'last7days',
        });
        await wait_for_event_settlement();

        expect(value_at(facade.get_snapshot().value, [...details_screen_path, 'SIDE'])).toBe('sell');
        expect(value_at(facade.get_snapshot().value, [...details_screen_path, 'PERIOD', 'selection'])).toBe('last7days');

        dispatch_intent({
            type: 'OPEN_CSV_EXPORT',
        });

        expect(Object.keys(value_at(facade.get_snapshot().value, [...details_screen_path, 'CSV_EXPORT', 'editing'])!)).toHaveLength(3);
    });

    it('deep history restores chart paths and tabs but retains newer hidden server data', async () => {
        const { facade, send: dispatch_intent, view: read_view_model } = create_test_application();

        dispatch_intent({
            type: 'CHART_INTERVAL_SELECTED',
            interval: '4h',
        });
        dispatch_intent({
            type: 'CHART_INDICATOR_SETTINGS_TOGGLED',
        });
        dispatch_intent({
            type: 'CHART_INDICATOR_CHANGED',
            indicator: 'ema9',
            is_visible: false,
        });
        dispatch_intent({
            type: 'CHART_DRAWING_TOOL_CLICKED',
        });
        dispatch_intent({
            type: 'CHART_DRAWING_STARTED',
        });
        dispatch_intent({
            type: 'CHART_DRAWING_FINISHED',
            drawing: CHART_DRAWING_FIXTURE,
        });
        dispatch_intent({
            type: 'REALTIME_INDICATORS_TAB_SELECTED',
        });

        const chart_before = value_at(facade.get_snapshot().value, [...main_screen_path, 'DISPLAY_CHART']);

        dispatch_intent({
            type: 'SHOW_TRADE_HISTORY',
        });

        expect(dispatch_intent({
            type: 'CHART_INTERVAL_SELECTED',
            interval: '1m',
        })).toBe(false);
        expect(dispatch_intent({
            type: 'REALTIME_INDICATORS_TAB_SELECTED',
        })).toBe(false);

        dispatch_intent({
            type: 'CHART_ACTIVE_STATE_UPDATED',
            state_label: 'NEW_SERVER_STATE',
        });
        dispatch_intent({
            type: 'REGIME_SELECTION_SYNCHRONIZED',
            selected: 'type2',
            support_status: 'unsupported',
            version: 2,
        });
        dispatch_intent({
            type: 'BUY_ORDER_EXECUTED',
            trade: TRADE_RECORD_FIXTURES[1]!,
        });
        dispatch_intent({
            type: 'BACK_TO_DASHBOARD',
        });

        expect(value_at(facade.get_snapshot().value, [...main_screen_path, 'DISPLAY_CHART'])).toEqual(chart_before);
        expect(read_view_model().chart).toMatchObject({
            interval: '4h',
            active_trading_logic_state: 'NEW_SERVER_STATE',
            indicators: {
                ema9: false,
            },
        });
        expect(read_view_model().chart.drawings).toHaveLength(1);
        expect(read_view_model().trader_panel.active_tab).toBe('realtime_indicators');
        expect(read_view_model().trader_panel.trades[0]?.id).toBe(TRADE_RECORD_FIXTURES[1]!.id);
        expect(read_view_model().regime.applied).toBe('type2');

        await wait_for_event_settlement();
    });

    it('keeps submitted writes alive, applies hidden completion once and never resubmits on history entry', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        const pending_result = create_deferred_result<void>();
        const write_command = vi.spyOn(command_adapter, 'update_split_order').mockReturnValue(pending_result.promise);
        const { facade, send: dispatch_intent, view: read_view_model } = create_test_application({}, command_adapter);

        dispatch_intent({
            type: 'SCALE_IN_CHANGED',
            percentage: 73,
        });

        expect(read_view_model().split_order).toMatchObject({
            scale_in_percentage: 73,
            is_pending: true,
        });
        expect(value_at(facade.get_snapshot().value, [...main_screen_path, 'DISPLAY_ACCOUNT_INFO', 'SPLIT_ORDER', 'scale_in'])).toEqual({
            SCALE_IN_ORDER: 'saving',
        });

        dispatch_intent({
            type: 'SHOW_TRADE_HISTORY',
        });
        pending_result.resolve();
        await wait_for_event_settlement();

        expect(read_view_model().split_order.is_pending).toBe(false);

        dispatch_intent({
            type: 'BACK_TO_DASHBOARD',
        });
        await wait_for_event_settlement();

        expect(write_command).toHaveBeenCalledExactlyOnceWith('scale_in', 73);
        expect(value_at(facade.get_snapshot().value, [...main_screen_path, 'DISPLAY_ACCOUNT_INFO', 'SPLIT_ORDER', 'scale_in'])).toEqual({
            SCALE_IN_ORDER: 'ready',
        });
        expect(read_view_model().split_order.scale_in_percentage).toBe(73);
    });

    it('preserves rapid split input and ignores an older successful response after the latest failure', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        const first_result = create_deferred_result<void>();
        const latest_result = create_deferred_result<void>();
        const write_command = vi.spyOn(command_adapter, 'update_split_order').mockReturnValueOnce(first_result.promise).mockReturnValueOnce(latest_result.promise);
        const { send: dispatch_intent, view: read_view_model } = create_test_application({}, command_adapter);

        dispatch_intent({
            type: 'SCALE_IN_CHANGED',
            percentage: 60,
        });
        dispatch_intent({
            type: 'SCALE_OUT_CHANGED',
            percentage: 20,
        });

        expect(write_command.mock.calls).toEqual([['scale_in', 60], ['scale_out', 20]]);
        expect(read_view_model().split_order).toMatchObject({
            scale_in_percentage: 60,
            scale_out_percentage: 20,
            is_pending: true,
        });

        latest_result.reject(new Error('latest failed'));
        await wait_for_event_settlement();
        first_result.resolve();
        await wait_for_event_settlement();

        expect(read_view_model().split_order.error?.message).toBe('latest failed');
        expect(read_view_model().split_order.is_pending).toBe(false);
    });

    it('restores completed REGIME state without applying its candidate twice', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        const pending_result = create_deferred_result<void>();
        const write_command = vi.spyOn(command_adapter, 'apply_regime').mockReturnValue(pending_result.promise);
        const { send: dispatch_intent, view: read_view_model } = create_test_application({
            applied_regime: 'type0',
        }, command_adapter);

        dispatch_intent({
            type: 'REGIME_TYPE_CLICKED',
            regime: 'type1',
        });
        dispatch_intent({
            type: 'REGIME_CHANGE_CONFIRMED',
        });
        dispatch_intent({
            type: 'SHOW_TRADE_HISTORY',
        });
        pending_result.resolve();
        await wait_for_event_settlement();

        expect(read_view_model().regime.applied).toBe('type1');

        dispatch_intent({
            type: 'BACK_TO_DASHBOARD',
        });

        expect(read_view_model().regime).toMatchObject({
            applied: 'type1',
            candidate: null,
            is_pending: false,
        });
        expect(write_command).toHaveBeenCalledExactlyOnceWith('type1');
    });

    it('a hidden authoritative REGIME update invalidates an older pending completion', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        const pending_result = create_deferred_result<void>();

        vi.spyOn(command_adapter, 'apply_regime').mockReturnValue(pending_result.promise);

        const { send: dispatch_intent, view: read_view_model } = create_test_application({
            applied_regime: 'type0',
        }, command_adapter);

        dispatch_intent({
            type: 'REGIME_TYPE_CLICKED',
            regime: 'type1',
        });
        dispatch_intent({
            type: 'REGIME_CHANGE_CONFIRMED',
        });
        dispatch_intent({
            type: 'SHOW_TRADE_HISTORY',
        });
        dispatch_intent({
            type: 'REGIME_SELECTION_SYNCHRONIZED',
            selected: 'type2',
            version: 5,
            support_status: 'unsupported',
        });
        pending_result.resolve();
        await wait_for_event_settlement();
        dispatch_intent({
            type: 'BACK_TO_DASHBOARD',
        });

        expect(read_view_model().regime).toMatchObject({
            applied: 'type2',
            candidate: null,
            is_pending: false,
        });
    });

    it('CSV completion survives screen departure without duplicate export', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        const pending_result = create_deferred_result<typeof command_adapter.exported_receipt>();
        const write_command = vi.spyOn(command_adapter, 'export_csv').mockReturnValue(pending_result.promise);
        const { send: dispatch_intent, view: read_view_model } = create_test_application({}, command_adapter);

        dispatch_intent({
            type: 'SHOW_TRADE_HISTORY',
        });
        dispatch_intent({
            type: 'OPEN_CSV_EXPORT',
        });
        dispatch_intent({
            type: 'CSV_DIRECTORY_SELECT_CLICKED',
        });
        await wait_for_event_settlement();
        dispatch_intent({
            type: 'CSV_EXPORT_SUBMITTED',
        });
        dispatch_intent({
            type: 'BACK_TO_DASHBOARD',
        });
        pending_result.resolve(command_adapter.exported_receipt);
        await wait_for_event_settlement();
        dispatch_intent({
            type: 'SHOW_TRADE_HISTORY',
        });
        await wait_for_event_settlement();

        expect(read_view_model().csv_export.status).toBe('complete');
        expect(write_command).toHaveBeenCalledOnce();
    });
    it.each([false, true])('highlight expiry continues while hidden (request started hidden=%s)', async is_initially_hidden => {
        vi.useFakeTimers();

        const { send: dispatch_intent, view: read_view_model } = create_test_application();

        if (is_initially_hidden) {
            dispatch_intent({
                type: 'SHOW_TRADE_HISTORY',
            });
        }

        dispatch_intent({
            type: 'START_TRADING_CLICKED',
        });
        dispatch_intent({
            type: 'SELECT_REGIME_NOTICE_CONFIRMED',
        });

        expect(read_view_model().regime.is_highlighted).toBe(true);

        if (!is_initially_hidden) {
            dispatch_intent({
                type: 'SHOW_TRADE_HISTORY',
            });
        }

        await vi.advanceTimersByTimeAsync(4100);

        expect(read_view_model().regime.is_highlighted).toBe(false);

        dispatch_intent({
            type: 'BACK_TO_DASHBOARD',
        });

        expect(read_view_model().regime.is_highlighted).toBe(false);
        expect(vi.getTimerCount()).toBe(0);
    });

    it('cancels details reads on exit and ignores late results; preserves filters on reentry', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        const previous_result = create_deferred_result<TradeHistoryDetails>();
        const current_result = create_deferred_result<TradeHistoryDetails>();
        const load_history = vi.spyOn(command_adapter, 'load_trade_history').mockReturnValueOnce(previous_result.promise).mockReturnValueOnce(current_result.promise);
        const { send: dispatch_intent, view: read_view_model } = create_test_application({}, command_adapter);

        dispatch_intent({
            type: 'SHOW_TRADE_HISTORY',
        });
        dispatch_intent({
            type: 'BACK_TO_DASHBOARD',
        });

        expect(load_history.mock.calls[0]![1]?.aborted).toBe(true);

        dispatch_intent({
            type: 'SHOW_TRADE_HISTORY',
        });
        current_result.resolve({
            records: [TRADE_RECORD_FIXTURES[0]!],
            summary: command_adapter.trade_history_summary,
        });
        await wait_for_event_settlement();
        previous_result.resolve({
            records: [TRADE_RECORD_FIXTURES[1]!],
            summary: command_adapter.trade_history_summary,
        });
        await wait_for_event_settlement();

        expect(read_view_model().trade_history.records).toEqual([TRADE_RECORD_FIXTURES[0]!]);

        dispatch_intent({
            type: 'HISTORY_SIDE_SELECTED',
            side: 'sell',
        });
        await wait_for_event_settlement();
        dispatch_intent({
            type: 'HISTORY_PERIOD_SELECTED',
            period: 'last30days',
        });
        await wait_for_event_settlement();
        dispatch_intent({
            type: 'BACK_TO_DASHBOARD',
        });
        dispatch_intent({
            type: 'SHOW_TRADE_HISTORY',
        });
        await wait_for_event_settlement();

        expect(read_view_model().trade_history).toMatchObject({
            period: 'last30days',
            side: 'sell',
        });
    });

    it('ignores filters during loading and reloads once when a fill arrives during that read', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        const first_result = create_deferred_result<TradeHistoryDetails>();
        const refreshed_result = create_deferred_result<TradeHistoryDetails>();
        const load_history = vi.spyOn(command_adapter, 'load_trade_history').mockReturnValueOnce(first_result.promise).mockReturnValueOnce(refreshed_result.promise);
        const { send: dispatch_intent, view: read_view_model, facade } = create_test_application({}, command_adapter);

        dispatch_intent({
            type: 'SHOW_TRADE_HISTORY',
        });
        dispatch_intent({
            type: 'HISTORY_SIDE_SELECTED',
            side: 'sell',
        });
        dispatch_intent({
            type: 'HISTORY_PERIOD_SELECTED',
            period: 'last30days',
        });

        expect(read_view_model().trade_history).toMatchObject({
            period: 'today',
            side: 'all',
        });
        expect(value_at(facade.get_snapshot().value, [...details_screen_path, 'SIDE'])).toBe('all');
        expect(value_at(facade.get_snapshot().value, [...details_screen_path, 'PERIOD', 'selection'])).toBe('today');

        dispatch_intent({
            type: 'BUY_ORDER_EXECUTED',
            trade: TRADE_RECORD_FIXTURES[1]!,
        });
        first_result.resolve({
            records: [],
            summary: command_adapter.trade_history_summary,
        });
        await wait_for_event_settlement();

        expect(load_history).toHaveBeenCalledTimes(2);
        expect(load_history.mock.calls.map(call => call[0])).toEqual([
            {
                period: 'today',
                side: 'all',
            },
            {
                period: 'today',
                side: 'all',
            },
        ]);

        refreshed_result.resolve({
            records: [TRADE_RECORD_FIXTURES[1]!],
            summary: command_adapter.trade_history_summary,
        });
        await wait_for_event_settlement();

        expect(read_view_model().trade_history.records).toEqual([TRADE_RECORD_FIXTURES[1]!]);
    });

    it('protects newer summary events from an older query and refreshes at KST midnight', async () => {
        vi.useFakeTimers();
        vi.setSystemTime(new Date('2026-09-16T14:59:59.000Z'));

        const command_adapter = new FakeUiCommandAdapter();
        const first_result = create_deferred_result<TradeHistoryDetails>();
        const load_history = vi.spyOn(command_adapter, 'load_trade_history').mockReturnValueOnce(first_result.promise);
        const { send: dispatch_intent, view: read_view_model } = create_test_application({}, command_adapter);

        dispatch_intent({
            type: 'SHOW_TRADE_HISTORY',
        });
        dispatch_intent({
            type: 'TRADE_HISTORY_HOLDINGS_UPDATED',
            position: {
                quantity: '99 ETH',
            },
        });
        first_result.resolve({
            records: command_adapter.trade_history,
            summary: command_adapter.trade_history_summary,
        });
        await wait_for_event_settlement();

        expect(read_view_model().trade_history.summary.position.quantity).toBe('99 ETH');

        await vi.advanceTimersByTimeAsync(1100);

        expect(load_history).toHaveBeenCalledTimes(2);
        expect(load_history.mock.calls[1]![0]).toEqual({
            period: 'today',
            side: 'all',
        });
    });
    it.each([
        ['SIDECAR_EXIT_TIMEOUT', 'shutdown_exit_recovery'],
        ['SHUTDOWN_OUTCOME_AMBIGUOUS', 'shutdown_outcome_recovery'],
        ['SIDECAR_ABNORMAL_EXIT', 'sidecar_exit_failure'],
    ] as const)('preserves %s exit recovery and its cancel barrier', async (failure_code, expected_status) => {
        const command_adapter = new FakeUiCommandAdapter();

        command_adapter.queue_failure('shutdown_application', new BackendAdapterError(failure_code, failure_code, false));

        const { send: dispatch_intent, view: read_view_model } = create_test_application({}, command_adapter);

        dispatch_intent({
            type: 'APP_EXIT_CLICKED',
        });
        dispatch_intent({
            type: 'APP_EXIT_CONFIRMED',
        });
        await wait_for_event_settlement();

        expect(read_view_model().app_exit.status).toBe(expected_status);

        dispatch_intent({
            type: 'APP_EXIT_CANCELED',
        });

        expect(read_view_model().app_exit.status).toBe(expected_status);
        expect(command_adapter.command_records).toHaveLength(1);
        expect(read_view_model().app_exit.is_final).toBe(false);
    });

    it('waits for native shutdown completion and enters root final with all owned jobs stopped', async () => {
        vi.useFakeTimers();

        const command_adapter = new FakeUiCommandAdapter();
        const shutdown = create_deferred_result<void>();

        vi.spyOn(command_adapter, 'shutdown_application').mockReturnValue(shutdown.promise);

        const { facade, send: dispatch_intent, view: read_view_model } = create_test_application({}, command_adapter);

        dispatch_intent({
            type: 'START_TRADING_CLICKED',
        });
        dispatch_intent({
            type: 'SELECT_REGIME_NOTICE_CONFIRMED',
        });
        dispatch_intent({
            type: 'APP_EXIT_CLICKED',
        });
        dispatch_intent({
            type: 'APP_EXIT_CONFIRMED',
        });
        await wait_for_event_settlement();

        expect(facade.get_snapshot().status).toBe('active');
        expect(read_view_model().app_exit.is_final).toBe(false);

        shutdown.resolve();
        await wait_for_event_settlement();

        expect(facade.get_snapshot().value).toBe('UI_FINAL_STATE');
        expect(facade.get_snapshot().status).toBe('done');
        expect(vi.getTimerCount()).toBe(0);
        expect(dispatch_intent({
            type: 'START_TRADING_CLICKED',
        })).toBe(false);
    });
});
