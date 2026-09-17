import { afterEach, describe, expect, it, vi } from 'vitest';
import { UISTM } from './UISTM';
import { create_ui_application_machine } from './uiApplicationMachine';
import type { UiDomainEvent } from './uiApplicationTypes';
import type { UIActionRequest } from './uiActions';
import { select_app_view_model } from '../control/selectAppViewModel';

const time = { now_epoch_ms: Date.parse('2026-09-17T14:59:59Z'), today: '2026-09-17' };
const event = (type: string, source: Record<string, unknown> = {}): UiDomainEvent => ({
    type, source: { type: type.slice(type.indexOf('.') + 1), ...source },
});
afterEach(() => vi.restoreAllMocks());

function assert_data(value: unknown): void {
    expect(typeof value).not.toBe('function');
    if (value !== null && typeof value === 'object') {
        expect(Object.getPrototypeOf(value)).toBe(Array.isArray(value) ? Array.prototype : Object.prototype);
        Object.values(value).forEach(assert_data);
    }
}

describe('UISTM decision boundary', () => {
    it('produces the same states and data-only requests without clocks, timers, fetch or a live actor', () => {
        const forbidden = () => { throw new Error('Unexpected external operation'); };
        vi.spyOn(Date, 'now').mockImplementation(forbidden);
        vi.spyOn(performance, 'now').mockImplementation(forbidden);
        vi.spyOn(globalThis, 'setTimeout').mockImplementation(forbidden);
        vi.spyOn(globalThis, 'fetch').mockImplementation(forbidden);
        const replay = () => {
            const stm = new UISTM(create_ui_application_machine({ today: time.today }));
            expect(stm.run(time).actions).toEqual([]);
            const outputs = [];
            for (const input of [event('regime.TYPE_CLICKED', { regime: 'type0' }), event('regime.CONFIRM_TYPE_CHANGE')]) {
                const result = stm.handle(input, time);
                result.actions.forEach(assert_data);
                outputs.push({ value: result.snapshot.value, view: select_app_view_model(result.snapshot), actions: result.actions });
            }
            expect(outputs[1]!.actions).toContainEqual(expect.objectContaining({
                type: 'run_command', operation: 'apply_regime', input: 'type0', token: 1,
            }));
            expect((stm.get_snapshot() as unknown as { children: object }).children).toEqual({});
            return outputs;
        };
        expect(replay()).toEqual(replay());
    });

    it('accepts only the current command token and emits no work when replaying a stale completion', () => {
        const stm = new UISTM(create_ui_application_machine({ today: time.today }));
        stm.run(time);
        stm.handle(event('regime.TYPE_CLICKED', { regime: 'type0' }), time);
        const result = stm.handle(event('regime.CONFIRM_TYPE_CHANGE'), time);
        const request = result.actions.find(action => action.type === 'run_command')!;
        const pending = select_app_view_model(stm.get_snapshot());
        const stale = stm.handle({ type: `command.${request.key}.done`, token: request.token + 1,
            source: { type: 'command.done', output: undefined } }, time);
        expect(stale.actions).toEqual([]);
        expect(select_app_view_model(stale.snapshot)).toEqual(pending);
        stm.handle({ type: `command.${request.key}.done`, token: request.token,
            source: { type: 'command.done', output: undefined } }, time);
        expect(select_app_view_model(stm.get_snapshot()).regime.applied).toBe('type0');
    });

    it('returns an absolute timer deadline and preserves the pre-transition screen for deep history', () => {
        const stm = new UISTM(create_ui_application_machine({ today: time.today }));
        stm.run(time);
        stm.handle(event('chart.4_H_BUTTON_CLICKED'), time);
        const timer = stm.handle(event('regime.HIGHLIGHT_REQUESTED'), time).actions;
        expect(timer).toContainEqual(expect.objectContaining({ type: 'start_timer', due_at_ms: time.now_epoch_ms + 4000 }));
        const previous = stm.get_snapshot().value;
        stm.handle(event('shell.SHOW_ALL_TRADING_DETAILS'), time);
        expect(stm.get_snapshot().context.retained).toEqual(previous);
        const returning = stm.handle(event('shell.BACK_TO_MAIN_SCREEN'), time);
        expect(returning.snapshot.value).toEqual(previous);
        expect(returning.actions.filter((action: UIActionRequest) => action.type === 'run_command')).toEqual([]);
    });
});
