import { DEFAULT_DASHBOARD_PROPS } from '../../routes/dashboard';
import type { AppViewModel, UiApplicationIntent } from '../control';
import { create_demo_ui_application, initialize_demo_ui_application } from '../bootstrap';
import type { UiApplicationController } from '../runtime';
import { present_dashboard_props } from './dashboardPresenter';
import { present_trade_history_props } from './tradeHistoryPresenter';

/**
 * 함수 이름: create_recording_controller()
 * 기능: presenter가 보낸 intent를 기록하면서 최신 ViewModel을 제공하는 테스트 controller를 생성한다.
 * 인자: view_model -> presenter 입력으로 사용할 화면 모델
 * 반환값: 기록 배열과 controller
 * 작성 날짜: 2026/08/12
 */
function create_recording_controller(view_model: AppViewModel): {
    readonly controller: UiApplicationController;
    readonly intents: Array<UiApplicationIntent>;
} {
    const intents: Array<UiApplicationIntent> = [];

    return {
        intents,
        controller: {
            dispatch: (intent) => {
                intents.push(intent);
                return true;
            },
            get_view_model: () => view_model,
        },
    };
}

/**
 * 함수 이름: create_demo_view_model()
 * 기능: presenter 테스트가 사용할 online·자동매매 정지 상태의 전체 AppViewModel을 생성한다.
 * 인자: 없음
 * 반환값: 시작된 데모 facade의 최신 ViewModel
 * 작성 날짜: 2026/08/12
 */
function create_demo_view_model(): AppViewModel {
    const application = create_demo_ui_application();
    application.facade.start();
    initialize_demo_ui_application(application.facade);

    return application.facade.get_view_model();
}

describe('application presenters', () => {
    it('account와 history summary actor snapshot을 route props에 투영한다', () => {
        const view_model = create_demo_view_model();
        const { controller } = create_recording_controller(view_model);
        const dashboard_props = present_dashboard_props(view_model, controller);
        const history_props = present_trade_history_props(view_model, controller);

        expect(dashboard_props.account.asset).toEqual(view_model.account_summary.asset);
        expect(dashboard_props.account.strategy.profitAmount).toBe(
            DEFAULT_DASHBOARD_PROPS.account.strategy.profitAmount,
        );
        expect(history_props.summary).toBe(view_model.trade_history.summary);
        expect(history_props.description).toBe(
            `오늘 · ETH/KRW · 전체 · 체결 ${view_model.trade_history.records.length}건`,
        );
    });

    it('거래 내역 조회 실패를 오래된 행 대신 오류와 재시도 intent로 투영한다', () => {
        const view_model = create_demo_view_model();
        const failed_view_model: AppViewModel = {
            ...view_model,
            trade_history: {
                ...view_model.trade_history,
                status: 'failed',
                is_loading: false,
                error: {
                    code: 'TRADE_HISTORY_LOAD_FAILED',
                    message: 'history unavailable',
                },
            },
        };
        const { controller, intents } = create_recording_controller(failed_view_model);
        const history_props = present_trade_history_props(failed_view_model, controller);

        expect(history_props.description).toBe('오늘 · ETH/KRW · 전체 · 조회 실패');
        expect(history_props.rows).toEqual([]);
        expect(history_props.emptyState).toMatchObject({
            description: 'history unavailable',
            actionLabel: '다시 시도',
        });
        expect(history_props.filtersDisabled).toBe(true);

        history_props.onStartTrading?.();
        expect(intents).toEqual([{ type: 'REFRESH_TRADE_HISTORY' }]);
    });

    it('drawing hover·context-menu·delete intent를 facade 계약으로 모두 변환한다', () => {
        const view_model = create_demo_view_model();
        const { controller, intents } = create_recording_controller(view_model);
        const chart_props = present_dashboard_props(view_model, controller).chart;

        chart_props.onIntent?.({ type: 'DRAWING_LINE_HOVER_ENTERED', lineId: 'line-1' });
        chart_props.onIntent?.({ type: 'DRAWING_LINE_HOVER_EXITED' });
        chart_props.onIntent?.({
            type: 'DRAWING_LINE_CONTEXT_MENU_REQUESTED',
            x: 120,
            y: 84,
        });
        chart_props.onIntent?.({ type: 'DRAWING_LINE_DELETE_REQUESTED' });
        chart_props.onIntent?.({ type: 'DRAWING_LINE_CONTEXT_MENU_CLOSED' });

        expect(intents).toEqual([
            { type: 'CHART_LINE_HOVER_ENTERED', line_id: 'line-1' },
            { type: 'CHART_LINE_HOVER_EXITED' },
            { type: 'CHART_LINE_CONTEXT_MENU_REQUESTED', x: 120, y: 84 },
            { type: 'CHART_LINE_DELETE_REQUESTED' },
            { type: 'CHART_LINE_CONTEXT_MENU_OUTSIDE_CLICKED' },
        ]);
    });
});
