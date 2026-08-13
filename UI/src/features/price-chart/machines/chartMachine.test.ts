import { createActor } from 'xstate';
import { describe, expect, it } from 'vitest';
import { CHART_DRAWING_FIXTURE } from '../../../shared/testing';
import { create_chart_machine } from './chartMachine';

describe('chartMachine', () => {
    it('DC1-*/ER-17: 주기 변경과 전체화면에서도 interval별 drawing을 보존한다', () => {
        const actor = createActor(create_chart_machine({
            drawings: {
                '30m': [CHART_DRAWING_FIXTURE],
            },
        }));

        actor.start();
        actor.send({ type: '1_M_BUTTON_CLICKED' });
        actor.send({ type: 'FULL_SIZE_SELECTED' });

        expect(actor.getSnapshot().context.interval).toBe('1m');
        expect(actor.getSnapshot().context.is_fullscreen).toBe(true);
        expect(actor.getSnapshot().context.drawings['30m']).toEqual([CHART_DRAWING_FIXTURE]);

        actor.send({ type: '30_M_BUTTON_CLICKED' });
        expect(actor.getSnapshot().context.drawings['30m']).toEqual([CHART_DRAWING_FIXTURE]);
        actor.stop();
    });

    it('IP1-*/IP2-*: 지표 토글은 서로 독립적으로 저장된다', () => {
        const actor = createActor(create_chart_machine());

        actor.start();
        actor.send({ type: 'INDICATOR_SETTINGS_BUTTON_CLICKED' });
        actor.send({ type: 'EMA_DISPLAY_OFF_CLICKED' });

        expect(actor.getSnapshot().context.indicators).toEqual({
            bollinger_bands: true,
            ema9: false,
            volume: true,
        });
        actor.stop();
    });
});

