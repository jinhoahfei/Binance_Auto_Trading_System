import { afterEach, expect, it, vi } from 'vitest';
import { UiCommandExecutor } from './UiCommandExecutor';
import { FakeUiCommandAdapter } from '../../shared/testing';
import type { UiDomainEvent } from '../machines/uiApplicationTypes';

afterEach(() => vi.useRealTimers());


/**
 * 함수 이름: settle()
 * 기능: 명령 Promise와 완료 이벤트가 처리될 microtask를 진행한다.
 * 인자: 없음
 * 반환값: 완료 대기 Promise
 * 작성 날짜: 2026/09/17
 */
async function settle() { for (let index = 0; index < 20; index++) await Promise.resolve(); }

it('executes requests in order and converts synchronous throws and rejected promises to failure events', async () => {
    // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
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

    // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
    expect(order).toEqual(['apply', 'stop']);
    expect(events).toEqual([]);

    // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
    await settle();

    // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
    expect(events).toEqual([
        { type: 'command.regime.apply.error', token: 1, source: { type: 'command.error', error: synchronous } },
        { type: 'command.trading.stop.error', token: 2, source: { type: 'command.error', error: asynchronous } },
    ]);
});

it('replaces same-key work and suppresses late write results without claiming to undo the write', async () => {
    // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
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

    // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
    await settle();

    // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
    expect(events).toEqual([]);
    completions[1]!();

    // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
    await settle();

    // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
    expect(events).toEqual([{ type: 'command.regime.apply.done', token: 2, source: { type: 'command.done', output: undefined } }]);
    expect(port.apply_regime).toHaveBeenCalledTimes(2);
});

it('cancels timers and actual reads on cleanup and never delivers their stale results', async () => {
    // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
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

    // 가짜 시간을 진행해 예약된 작업과 후속 상태 반영을 실행한다.
    await vi.advanceTimersByTimeAsync(10_000);

    // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
    expect(signal?.aborted).toBe(true);
    expect(events).toEqual([]);
    expect(vi.getTimerCount()).toBe(0);
});

it('expires a retained timer at its original deadline and preserves query revision metadata', async () => {
    // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
    vi.useFakeTimers();
    vi.setSystemTime(1_000);
    const port = new FakeUiCommandAdapter();
    const events: UiDomainEvent[] = [];
    const executor = new UiCommandExecutor(port, event => events.push(event));
    executor.execute([{ type: 'start_timer', key: 'highlight', token: 1, due_at_ms: 5_000 }]);

    // 가짜 시간을 진행해 예약된 작업과 후속 상태 반영을 실행한다.
    await vi.advanceTimersByTimeAsync(3_999);
    expect(events).toEqual([]);  // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.

    // 가짜 시간을 진행해 예약된 작업과 후속 상태 반영을 실행한다.
    await vi.advanceTimersByTimeAsync(1);

    // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
    expect(events[0]).toMatchObject({ type: 'command.highlight.done', token: 1 });
    executor.execute([{ type: 'run_command', key: 'history', token: 2, operation: 'load_trade_history',
        input: { query: { period: 'today', side: 'all' }, summary_revision: 7, publish_summary: false } }]);

    // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
    await settle();

    // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
    expect(events[1]?.source?.output).toMatchObject({ request_summary_revision: 7, publish_summary: false });
});

it('does not start remaining work if the owner stops reentrantly during a command call', async () => {
    // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
    const port = new FakeUiCommandAdapter();
    const deliver = vi.fn();
    const executor = new UiCommandExecutor(port, deliver);
    vi.spyOn(port, 'apply_regime').mockImplementation(async () => executor.execute([{ type: 'stop_all' }]));
    const stop = vi.spyOn(port, 'stop_trading');
    executor.execute([
        { type: 'run_command', key: 'apply', token: 1, operation: 'apply_regime', input: 'type0' },
        { type: 'run_command', key: 'stop', token: 2, operation: 'stop_trading', input: undefined },
    ]);

    // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
    await settle();

    // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
    expect(stop).not.toHaveBeenCalled();
    expect(deliver).not.toHaveBeenCalled();
});
