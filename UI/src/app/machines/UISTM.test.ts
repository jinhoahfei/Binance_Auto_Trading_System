import { afterEach, describe, expect, it, vi } from 'vitest';
import { UISTM } from './UISTM';
import { create_ui_application_machine } from './uiApplicationMachine';
import type { UiDomainEvent } from './uiApplicationTypes';
import type { UIActionRequest } from './uiActions';
import { select_app_view_model } from '../control/selectAppViewModel';

const time = { now_epoch_ms: Date.parse('2026-09-17T14:59:59Z'), today: '2026-09-17' };


/**
 * 함수 이름: event()
 * 기능: 기능 이름이 포함된 이벤트를 원래 source 데이터와 함께 구성한다.
 * 인자: type -> 루트 이벤트 이름, source -> 추가 이벤트 데이터
 * 반환값: STM에 전달할 내부 이벤트
 * 작성 날짜: 2026/09/17
 */
const event = (type: string, source: Record<string, unknown> = {}): UiDomainEvent => ({
    type, source: { type: type.slice(type.indexOf('.') + 1), ...source },
});
afterEach(() => vi.restoreAllMocks());


/**
 * 함수 이름: assert_data()
 * 기능: 실행 요청 안에 함수나 특수 실행 객체가 없이 평범한 데이터만 있는지 재귀 검사한다.
 * 인자: value -> 검증할 요청 또는 하위 값
 * 반환값: 없음; 계약 위반이면 assertion 실패
 * 작성 날짜: 2026/09/17
 */
function assert_data(value: unknown): void {
    expect(typeof value).not.toBe('function');
    if (value !== null && typeof value === 'object') {
        expect(Object.getPrototypeOf(value)).toBe(Array.isArray(value) ? Array.prototype : Object.prototype);
        Object.values(value).forEach(assert_data);
    }
}

describe('UISTM decision boundary', () => {
    it('produces the same states and data-only requests without clocks, timers, fetch or a live actor', () => {
        /**
         * 함수 이름: forbidden()
         * 기능: 순수 평가 중 외부 시계·타이머·fetch 접근이 발생하면 즉시 실패시킨다.
         * 인자: 없음
         * 반환값: 반환하지 않고 예외 발생
         * 작성 날짜: 2026/09/17
         */
        const forbidden = () => { throw new Error('Unexpected external operation'); };
        vi.spyOn(Date, 'now').mockImplementation(forbidden);
        vi.spyOn(performance, 'now').mockImplementation(forbidden);
        vi.spyOn(globalThis, 'setTimeout').mockImplementation(forbidden);
        vi.spyOn(globalThis, 'fetch').mockImplementation(forbidden);

        /**
         * 함수 이름: replay()
         * 기능: 동일한 초기 상태·입력·시간으로 순수 전이와 요청 결과를 기록한다.
         * 인자: 없음
         * 반환값: 결정성을 비교할 상태·화면 모델·요청 배열
         * 작성 날짜: 2026/09/17
         */
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
        expect(replay()).toEqual(replay());  // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
    });

    it('accepts only the current command token and emits no work when replaying a stale completion', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const stm = new UISTM(create_ui_application_machine({ today: time.today }));
        stm.run(time);
        stm.handle(event('regime.TYPE_CLICKED', { regime: 'type0' }), time);

        // 준비한 입력으로 결과를 계산하거나 현재 상태를 읽는다.
        const result = stm.handle(event('regime.CONFIRM_TYPE_CHANGE'), time);
        const request = result.actions.find(action => action.type === 'run_command')!;
        const pending = select_app_view_model(stm.get_snapshot());
        const stale = stm.handle({ type: `command.${request.key}.done`, token: request.token + 1,
            source: { type: 'command.done', output: undefined } }, time);

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(stale.actions).toEqual([]);
        expect(select_app_view_model(stale.snapshot)).toEqual(pending);
        stm.handle({ type: `command.${request.key}.done`, token: request.token,
            source: { type: 'command.done', output: undefined } }, time);
        expect(select_app_view_model(stm.get_snapshot()).regime.applied).toBe('type0');
    });

    it('returns an absolute timer deadline and preserves the pre-transition screen for deep history', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const stm = new UISTM(create_ui_application_machine({ today: time.today }));
        stm.run(time);
        stm.handle(event('chart.4_H_BUTTON_CLICKED'), time);
        const timer = stm.handle(event('regime.HIGHLIGHT_REQUESTED'), time).actions;

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(timer).toContainEqual(expect.objectContaining({ type: 'start_timer', due_at_ms: time.now_epoch_ms + 4000 }));
        const previous = stm.get_snapshot().value;
        stm.handle(event('shell.SHOW_ALL_TRADING_DETAILS'), time);
        expect(stm.get_snapshot().context.retained).toEqual(previous);
        const returning = stm.handle(event('shell.BACK_TO_MAIN_SCREEN'), time);
        expect(returning.snapshot.value).toEqual(previous);
        expect(returning.actions.filter((action: UIActionRequest) => action.type === 'run_command')).toEqual([]);
    });
});
