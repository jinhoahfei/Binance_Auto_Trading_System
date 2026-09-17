import { createActor } from 'xstate';
import { describe, expect, it } from 'vitest';
import { CHART_DRAWING_FIXTURE } from '../../../shared/testing';
import { create_chart_machine } from './chartMachine';

describe('chartMachine', () => {
    it('DC1-*/ER-17: 주기 변경과 전체화면에서도 interval별 drawing을 보존한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const actor = createActor(create_chart_machine({
            drawings: {
                '30m': [CHART_DRAWING_FIXTURE],
            },
        }));

        // 1_M_BUTTON_CLICKED → FULL_SIZE_SELECTED 입력을 전달해 해당 전이를 실행한다.
        actor.start();
        actor.send({ type: '1_M_BUTTON_CLICKED' });
        actor.send({ type: 'FULL_SIZE_SELECTED' });

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(actor.getSnapshot().context.interval).toBe('1m');
        expect(actor.getSnapshot().context.is_fullscreen).toBe(true);
        expect(actor.getSnapshot().context.drawings['30m']).toEqual([CHART_DRAWING_FIXTURE]);

        // 30_M_BUTTON_CLICKED 입력을 전달해 해당 전이를 실행한다.
        actor.send({ type: '30_M_BUTTON_CLICKED' });
        expect(actor.getSnapshot().context.drawings['30m']).toEqual([CHART_DRAWING_FIXTURE]);  // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });

    it('IP1-*/IP2-*: 지표 토글은 서로 독립적으로 저장된다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const actor = createActor(create_chart_machine());

        // INDICATOR_SETTINGS_BUTTON_CLICKED → EMA_DISPLAY_OFF_CLICKED 입력을 전달해 해당 전이를 실행한다.
        actor.start();
        actor.send({ type: 'INDICATOR_SETTINGS_BUTTON_CLICKED' });
        actor.send({ type: 'EMA_DISPLAY_OFF_CLICKED' });

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(actor.getSnapshot().context.indicators).toEqual({
            bollinger_bands: true,
            ema9: false,
            volume: true,
        });

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });

    it.each([
        {
            expected_viewport: 'normal',
            screen_mode: '축소화면',
            viewport_event: null,
        },
        {
            expected_viewport: 'fullscreen',
            screen_mode: '전체화면',
            viewport_event: { type: 'FULL_SIZE_SELECTED' } as const,
        },
    ])('DC6-02~06: $screen_mode에서 선 선택, 우클릭 메뉴와 삭제를 처리한다', ({
        expected_viewport,
        viewport_event,
    }) => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const actor = createActor(create_chart_machine({
            drawings: {
                '30m': [CHART_DRAWING_FIXTURE],
            },
        }));

        // CURSOR_HOVER_ENTER 입력을 전달해 해당 전이를 실행한다.
        actor.start();
        if (viewport_event !== null) {
            actor.send(viewport_event);
        }

        actor.send({
            type: 'CURSOR_HOVER_ENTER',
            line_id: CHART_DRAWING_FIXTURE.id,
        });
        expect(actor.getSnapshot().context.selected_line_id).toBe(CHART_DRAWING_FIXTURE.id);  // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.

        // HIGHLIGHTED_LINE_RIGHT_CLICKED 입력을 전달해 해당 전이를 실행한다.
        actor.send({
            type: 'HIGHLIGHTED_LINE_RIGHT_CLICKED',
            x: 320,
            y: 180,
        });

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(actor.getSnapshot().value).toMatchObject({
            line_selection: 'context_menu',
            viewport: expected_viewport,
        });
        expect(actor.getSnapshot().context.context_menu_position).toEqual({ x: 320, y: 180 });

        // DELETE_LINE 입력을 전달해 해당 전이를 실행한다.
        actor.send({ type: 'DELETE_LINE' });

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(actor.getSnapshot().value).toMatchObject({
            line_selection: 'awaiting_selection',
            viewport: expected_viewport,
        });
        expect(actor.getSnapshot().context.drawings['30m']).toEqual([]);
        expect(actor.getSnapshot().context.selected_line_id).toBeNull();
        expect(actor.getSnapshot().context.context_menu_position).toBeNull();

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });
});
