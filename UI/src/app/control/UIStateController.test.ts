import { afterEach, expect, it, vi } from 'vitest';
import { UIStateController } from './UIStateController';
import { UiApplicationFacade } from './UiApplicationFacade';
import { FakeUiCommandAdapter } from '../../shared/testing';

const controllers: UIStateController[] = [];
afterEach(() => { controllers.splice(0).forEach(controller => controller.stop()); vi.useRealTimers(); });
async function settle() { for (let index = 0; index < 20; index++) await Promise.resolve(); }
function create(port = new FakeUiCommandAdapter(), get_current_kst_date?: () => string) {
    const controller = new UIStateController(port, { today: '2026-09-17',
        ...(get_current_kst_date ? { get_current_kst_date } : {}) });
    controllers.push(controller);
    return controller;
}

it('keeps the existing facade as the exact same implementation and queues cold inputs until start', async () => {
    expect(UiApplicationFacade).toBe(UIStateController);
    const port = new FakeUiCommandAdapter();
    const controller = create(port);
    controller.dispatch({ type: 'SHOW_TRADE_HISTORY' });
    expect(port.command_records).toEqual([]);
    controller.start();
    controller.start();
    await settle();
    expect(port.command_records.map(record => record.name)).toEqual(['load_trade_history']);
});

it('commits pending state before executing work and serializes reentrant inputs after publication', async () => {
    const port = new FakeUiCommandAdapter();
    const controller = create(port);
    controller.start();
    const order: string[] = [];
    vi.spyOn(port, 'update_split_order').mockImplementation(async (side, percentage) => {
        expect(controller.get_view_model().split_order.is_pending).toBe(true);
        order.push(`${side}:${percentage}`);
        if (side === 'scale_in') controller.dispatch({ type: 'SCALE_OUT_CHANGED', percentage: 20 });
    });
    const unsubscribe = controller.subscribe(snapshot => {
        if (snapshot.context.features.split_order.pending_percentage) {
            order.push(`publish:${snapshot.context.features.split_order.pending_percentage}`);
        }
    });
    controller.dispatch({ type: 'SCALE_IN_CHANGED', percentage: 60 });
    expect(order.slice(0, 4)).toEqual(['scale_in:60', 'publish:60', 'scale_out:20', 'publish:20']);
    await settle();
    unsubscribe();
});

it('reads the date once for an actual new CSV draft and never for an ignored duplicate open', async () => {
    const date = vi.fn(() => '2026-09-18');
    const controller = create(new FakeUiCommandAdapter(), date);
    controller.start();
    controller.dispatch({ type: 'SHOW_TRADE_HISTORY' });
    await settle();
    controller.dispatch({ type: 'OPEN_CSV_EXPORT' });
    controller.dispatch({ type: 'OPEN_CSV_EXPORT' });
    expect(date).toHaveBeenCalledTimes(1);
    expect(controller.get_view_model().csv_export.start_date).toBe('2026-09-18');
    controller.dispatch({ type: 'CLOSE_CSV_EXPORT' });
    controller.dispatch({ type: 'OPEN_CSV_EXPORT' });
    expect(date).toHaveBeenCalledTimes(2);
});

it('aborts active reads on stop and ignores every subsequent completion and input', async () => {
    const port = new FakeUiCommandAdapter();
    let signal: AbortSignal | undefined;
    let resolve!: (value: { records: []; summary: typeof port.trade_history_summary }) => void;
    vi.spyOn(port, 'load_trade_history').mockImplementation((_query, input_signal) => {
        signal = input_signal;
        return new Promise(complete => { resolve = complete; });
    });
    const controller = create(port);
    controller.start();
    const listener = vi.fn();
    controller.subscribe(listener);
    controller.dispatch({ type: 'SHOW_TRADE_HISTORY' });
    controller.stop();
    const count = listener.mock.calls.length;
    expect(signal?.aborted).toBe(true);
    resolve({ records: [], summary: port.trade_history_summary });
    await settle();
    expect(listener).toHaveBeenCalledTimes(count);
    expect(controller.get_snapshot().status).toBe('stopped');
    expect(controller.dispatch({ type: 'BACK_TO_DASHBOARD' })).toBe(false);
});

it('supplies monotonic indicator receipt times and preserves the first timestamp on duplicate payloads', () => {
    vi.useFakeTimers();
    const controller = create();
    controller.start();
    const indicators = { phase_key: 'ENTRY_WAIT', notice: null, server_time: '2026-09-17T00:00:00Z', conditions: [] };
    const received_at = () => controller.get_snapshot().context.features.recent_orders.strategy_indicators_received_at;
    vi.advanceTimersByTime(1000);
    controller.handle_event({ type: 'recent_orders.STRATEGY_INDICATORS_SYNCHRONIZED',
        source: { type: 'STRATEGY_INDICATORS_SYNCHRONIZED', indicators } });
    expect(received_at()).toBe(1000);
    vi.advanceTimersByTime(1000);
    controller.handle_event({ type: 'recent_orders.STRATEGY_INDICATORS_SYNCHRONIZED',
        source: { type: 'STRATEGY_INDICATORS_SYNCHRONIZED', indicators: { ...indicators } } });
    expect(received_at()).toBe(1000);
    controller.handle_event({ type: 'recent_orders.STRATEGY_INDICATORS_SYNCHRONIZED',
        source: { type: 'STRATEGY_INDICATORS_SYNCHRONIZED', indicators: { ...indicators, server_time: '2026-09-17T00:00:02Z' } } });
    expect(received_at()).toBe(2000);
});
