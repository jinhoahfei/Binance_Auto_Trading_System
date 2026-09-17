import { afterEach, expect, it, vi } from 'vitest';

import { UiApplicationFacade, type UiApplicationIntent } from './UiApplicationFacade';
import { FakeUiCommandAdapter } from '../../shared/testing';

// Captured against 58727e2901a04a2a0faeb6dd594b94dc51b232d8 before the separation.
// This records observable states and commands, not actor/runtime implementation details.
const applications: UiApplicationFacade[] = [];
afterEach(() => {
    applications.splice(0).forEach(application => application.stop());
    vi.useRealTimers();
});

async function settle() {
    for (let index = 0; index < 20; index++) await Promise.resolve();
}

function deferred<T>() {
    let resolve!: (value: T) => void;
    let reject!: (error: Error) => void;
    const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
}

function replay_fixture() {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-17T14:59:58.000Z'));
    const port = new FakeUiCommandAdapter();
    let today = '2026-09-17';
    const application = new UiApplicationFacade(port, {
        today,
        get_current_kst_date: () => today,
    });
    applications.push(application);
    const trace: unknown[] = [];
    const capture = (label: string) => {
        const snapshot = application.get_snapshot();
        trace.push(JSON.parse(JSON.stringify({
            label,
            value: snapshot.value,
            status: snapshot.status,
            view: application.get_view_model(),
            commands: port.command_records,
        })));
    };
    application.subscribe(() => capture('publication'));
    application.start();
    const send = (intent: UiApplicationIntent) => {
        const accepted = application.dispatch(intent);
        trace.push({ intent, accepted });
        capture('after input');
    };
    return { application, port, trace, capture, send, set_today: (value: string) => { today = value; } };
}

it('preserves the pre-separation navigation, selection, commands, CSV and date trace', async () => {
    const { port, trace, capture, send, set_today } = replay_fixture();
    send({ type: 'START_TRADING_CLICKED' });
    send({ type: 'SELECT_REGIME_NOTICE_CONFIRMED' });
    send({ type: 'REGIME_TYPE_CLICKED', regime: 'type0' });
    send({ type: 'REGIME_CHANGE_CONFIRMED' });
    await settle();
    send({ type: 'API_CONNECTED' });
    send({ type: 'START_TRADING_CLICKED' });
    send({ type: 'START_TRADING_CONFIRMED' });
    await settle();
    send({ type: 'SCALE_IN_CHANGED', percentage: 73 });
    await settle();
    send({ type: 'CHART_INTERVAL_SELECTED', interval: '4h' });
    send({ type: 'SHOW_TRADE_HISTORY' });
    await settle();
    send({ type: 'HISTORY_SIDE_SELECTED', side: 'sell' });
    await settle();
    send({ type: 'OPEN_CSV_EXPORT' });
    port.selected_directory = null;
    send({ type: 'CSV_DIRECTORY_SELECT_CLICKED' });
    await settle();
    port.selected_directory = '/Users/demo/Exports';
    send({ type: 'CSV_DIRECTORY_SELECT_CLICKED' });
    await settle();
    send({ type: 'CSV_EXPORT_SUBMITTED' });
    await settle();
    send({ type: 'CLOSE_CSV_EXPORT' });
    set_today('2026-09-18');
    send({ type: 'OPEN_CSV_EXPORT' });
    send({ type: 'CLOSE_CSV_EXPORT' });
    send({ type: 'BACK_TO_DASHBOARD' });
    capture('complete');
    await expect(JSON.stringify(trace, null, 2)).toMatchFileSnapshot('./__snapshots__/ui-separation-navigation.json');
});

it('preserves hidden write completion, latest failure and stale response traces', async () => {
    const { port, trace, capture, send } = replay_fixture();
    const first = deferred<void>();
    const latest = deferred<void>();
    const original = port.update_split_order.bind(port);
    vi.spyOn(port, 'update_split_order')
        .mockImplementationOnce((side, value) => { void original(side, value); return first.promise; })
        .mockImplementationOnce((side, value) => { void original(side, value); return latest.promise; });
    send({ type: 'SCALE_IN_CHANGED', percentage: 60 });
    send({ type: 'SCALE_OUT_CHANGED', percentage: 20 });
    send({ type: 'SHOW_TRADE_HISTORY' });
    latest.reject(new Error('latest failed'));
    await settle();
    first.resolve();
    await settle();
    send({ type: 'BACK_TO_DASHBOARD' });
    const applied = deferred<void>();
    const original_apply = port.apply_regime.bind(port);
    vi.spyOn(port, 'apply_regime').mockImplementation(value => { void original_apply(value); return applied.promise; });
    send({ type: 'REGIME_TYPE_CLICKED', regime: 'type1' });
    send({ type: 'REGIME_CHANGE_CONFIRMED' });
    send({ type: 'SHOW_TRADE_HISTORY' });
    applied.resolve();
    await settle();
    send({ type: 'BACK_TO_DASHBOARD' });
    capture('complete');
    await expect(JSON.stringify(trace, null, 2)).toMatchFileSnapshot('./__snapshots__/ui-separation-races.json');
});

it('preserves hidden highlight and KST midnight timer traces', async () => {
    const { trace, capture, send } = replay_fixture();
    send({ type: 'START_TRADING_CLICKED' });
    send({ type: 'SELECT_REGIME_NOTICE_CONFIRMED' });
    send({ type: 'SHOW_TRADE_HISTORY' });
    await settle();
    await vi.advanceTimersByTimeAsync(2100);
    capture('after midnight');
    await vi.advanceTimersByTimeAsync(2000);
    capture('after highlight');
    send({ type: 'BACK_TO_DASHBOARD' });
    await expect(JSON.stringify(trace, null, 2)).toMatchFileSnapshot('./__snapshots__/ui-separation-timers.json');
});
