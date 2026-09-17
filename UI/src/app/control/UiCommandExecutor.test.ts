import { afterEach, expect, it, vi } from 'vitest';
import { UiCommandExecutor } from './UiCommandExecutor';
import { FakeUiCommandAdapter } from '../../shared/testing';
import type { UiDomainEvent } from '../machines/uiApplicationTypes';

afterEach(() => vi.useRealTimers());
async function settle() { for (let index = 0; index < 20; index++) await Promise.resolve(); }

it('executes requests in order and converts synchronous throws and rejected promises to failure events', async () => {
    const port = new FakeUiCommandAdapter();
    const events: UiDomainEvent[] = [];
    const executor = new UiCommandExecutor(port, event => events.push(event));
    const synchronous = new Error('synchronous');
    const asynchronous = new Error('asynchronous');
    const order: string[] = [];
    vi.spyOn(port, 'apply_regime').mockImplementation(() => { order.push('apply'); throw synchronous; });
    vi.spyOn(port, 'stop_trading').mockImplementation(async () => { order.push('stop'); throw asynchronous; });
    executor.execute([
        { type: 'run_command', key: 'regime.apply', token: 1, operation: 'apply_regime', input: 'type0' },
        { type: 'run_command', key: 'trading.stop', token: 2, operation: 'stop_trading', input: undefined },
    ]);
    expect(order).toEqual(['apply', 'stop']);
    expect(events).toEqual([]);
    await settle();
    expect(events).toEqual([
        { type: 'command.regime.apply.error', token: 1, source: { type: 'command.error', error: synchronous } },
        { type: 'command.trading.stop.error', token: 2, source: { type: 'command.error', error: asynchronous } },
    ]);
});

it('replaces same-key work and suppresses late write results without claiming to undo the write', async () => {
    const port = new FakeUiCommandAdapter();
    const events: UiDomainEvent[] = [];
    const executor = new UiCommandExecutor(port, event => events.push(event));
    const completions: Array<() => void> = [];
    vi.spyOn(port, 'apply_regime').mockImplementation(() => new Promise(resolve => completions.push(resolve)));
    executor.execute([
        { type: 'run_command', key: 'regime.apply', token: 1, operation: 'apply_regime', input: 'type0' },
        { type: 'run_command', key: 'regime.apply', token: 2, operation: 'apply_regime', input: 'type1' },
    ]);
    completions[0]!();
    await settle();
    expect(events).toEqual([]);
    completions[1]!();
    await settle();
    expect(events).toEqual([{ type: 'command.regime.apply.done', token: 2, source: { type: 'command.done', output: undefined } }]);
    expect(port.apply_regime).toHaveBeenCalledTimes(2);
});

it('cancels timers and actual reads on cleanup and never delivers their stale results', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_000);
    const port = new FakeUiCommandAdapter();
    const events: UiDomainEvent[] = [];
    const executor = new UiCommandExecutor(port, event => events.push(event));
    let signal: AbortSignal | undefined;
    vi.spyOn(port, 'load_trade_history').mockImplementation((_query, input_signal) => {
        signal = input_signal;
        return new Promise((_resolve, reject) => signal?.addEventListener('abort', () => reject(new Error('aborted'))));
    });
    executor.execute([
        { type: 'start_timer', key: 'highlight', token: 1, due_at_ms: 5_000 },
        { type: 'run_command', key: 'history', token: 2, operation: 'load_trade_history',
            input: { query: { period: 'today', side: 'all' }, summary_revision: 1, publish_summary: true } },
    ]);
    executor.execute([{ type: 'stop_all' }]);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(signal?.aborted).toBe(true);
    expect(events).toEqual([]);
    expect(vi.getTimerCount()).toBe(0);
});

it('expires a retained timer at its original deadline and preserves query revision metadata', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_000);
    const port = new FakeUiCommandAdapter();
    const events: UiDomainEvent[] = [];
    const executor = new UiCommandExecutor(port, event => events.push(event));
    executor.execute([{ type: 'start_timer', key: 'highlight', token: 1, due_at_ms: 5_000 }]);
    await vi.advanceTimersByTimeAsync(3_999);
    expect(events).toEqual([]);
    await vi.advanceTimersByTimeAsync(1);
    expect(events[0]).toMatchObject({ type: 'command.highlight.done', token: 1 });
    executor.execute([{ type: 'run_command', key: 'history', token: 2, operation: 'load_trade_history',
        input: { query: { period: 'today', side: 'all' }, summary_revision: 7, publish_summary: false } }]);
    await settle();
    expect(events[1]?.source?.output).toMatchObject({ request_summary_revision: 7, publish_summary: false });
});

it('does not start remaining work if the owner stops reentrantly during a command call', async () => {
    const port = new FakeUiCommandAdapter();
    const deliver = vi.fn();
    const executor = new UiCommandExecutor(port, deliver);
    vi.spyOn(port, 'apply_regime').mockImplementation(async () => executor.execute([{ type: 'stop_all' }]));
    const stop = vi.spyOn(port, 'stop_trading');
    executor.execute([
        { type: 'run_command', key: 'apply', token: 1, operation: 'apply_regime', input: 'type0' },
        { type: 'run_command', key: 'stop', token: 2, operation: 'stop_trading', input: undefined },
    ]);
    await settle();
    expect(stop).not.toHaveBeenCalled();
    expect(deliver).not.toHaveBeenCalled();
});
