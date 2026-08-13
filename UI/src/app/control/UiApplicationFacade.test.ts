import { describe, expect, it } from 'vitest';
import {
    CHART_DRAWING_FIXTURE,
    FakeUiCommandAdapter,
} from '../../shared/testing';
import { UiApplicationFacade } from './UiApplicationFacade';

describe('UiApplicationFacade', () => {
    it('VR-01: REGIME 미선택 시작 intent를 안내 modal과 highlight 흐름으로 조정한다', () => {
        const facade = new UiApplicationFacade(new FakeUiCommandAdapter(), {
            today: '2026-08-12',
        });

        facade.start();
        expect(facade.dispatch({ type: 'START_TRADING_CLICKED' })).toBe(true);
        expect(facade.get_view_model().active_modal).toBe('select_regime_notice');

        facade.dispatch({ type: 'SELECT_REGIME_NOTICE_CONFIRMED' });
        expect(facade.get_view_model().active_modal).toBeNull();
        expect(facade.get_view_model().regime.is_highlighted).toBe(true);
        facade.stop();
    });

    it('ES2-02/ES2-03: route 이동 후 dashboard feature snapshot을 보존한다', () => {
        const facade = new UiApplicationFacade(new FakeUiCommandAdapter(), {
            today: '2026-08-12',
        });

        facade.start();
        facade.dispatch({ type: 'CHART_INTERVAL_SELECTED', interval: '4h' });
        facade.dispatch({ type: 'SHOW_TRADE_HISTORY' });
        expect(facade.get_view_model().route).toBe('trade_history');

        facade.dispatch({ type: 'BACK_TO_DASHBOARD' });
        expect(facade.get_view_model().route).toBe('dashboard');
        expect(facade.get_view_model().chart.interval).toBe('4h');
        facade.stop();
    });

    it('DC3-02/DC6-02~06: chart 상태 갱신과 drawing 선택 event를 feature actor로 전달한다', () => {
        const facade = new UiApplicationFacade(new FakeUiCommandAdapter(), {
            today: '2026-08-12',
        });

        facade.start();
        facade.dispatch({ type: 'CHART_ACTIVE_STATE_UPDATED', state_label: 'ENTRY_WAIT' });
        facade.dispatch({ type: 'CHART_DRAWING_TOOL_CLICKED' });
        facade.dispatch({ type: 'CHART_DRAWING_STARTED' });
        facade.dispatch({ type: 'CHART_DRAWING_FINISHED', drawing: CHART_DRAWING_FIXTURE });
        facade.dispatch({ type: 'CHART_DRAWING_TOOL_CLICKED' });
        facade.dispatch({
            type: 'CHART_LINE_HOVER_ENTERED',
            line_id: CHART_DRAWING_FIXTURE.id,
        });
        facade.dispatch({ type: 'CHART_LINE_CONTEXT_MENU_REQUESTED' });

        expect(facade.get_view_model().chart.active_trading_logic_state).toBe('ENTRY_WAIT');
        expect(facade.get_view_model().chart.line_selection_state).toBe('context_menu');
        expect(facade.get_view_model().chart.selected_line_id).toBe(CHART_DRAWING_FIXTURE.id);

        facade.dispatch({ type: 'CHART_LINE_DELETE_REQUESTED' });
        expect(facade.get_view_model().chart.drawings).toEqual([]);
        expect(facade.get_view_model().chart.line_selection_state).toBe('awaiting_selection');
        facade.stop();
    });

    it('DI1-02/DI2-02/D1-02: backend 표시 snapshot을 요약 actor와 ViewModel에 투영한다', () => {
        const facade = new UiApplicationFacade(new FakeUiCommandAdapter(), {
            today: '2026-08-12',
        });

        facade.start();
        facade.dispatch({
            type: 'ACCOUNT_STRATEGY_UPDATED',
            strategy: {
                appliedState: 'EXIT_WAIT',
                profitAmount: '+₩ 8,200',
                profitRate: '+0.82%',
                status: '정상 작동',
                statusTone: 'positive',
            },
        });
        facade.dispatch({
            type: 'TRADE_HISTORY_PROFIT_RATE_UPDATED',
            daily_return: { value: '+0.82%', tone: 'positive' },
        });

        expect(facade.get_view_model().account_summary.strategy.appliedState).toBe('EXIT_WAIT');
        expect(facade.get_view_model().trade_history.summary.dailyReturn.value).toBe('+0.82%');
        facade.stop();
    });
});
