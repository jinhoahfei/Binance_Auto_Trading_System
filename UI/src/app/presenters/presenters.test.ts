import type {
    RealtimeChartDataRuntime,
    RealtimeChartDataSnapshot,
} from '../../features/price-chart';
import type { NormalizedKline } from '../../features/price-chart/data';
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

/**
 * 함수 이름: create_market_kline()
 * 기능: 실시간 차트 presenter 테스트에 사용할 주기별 정규화 봉을 만든다.
 * 인자: interval -> 봉 주기, index -> 봉 순번, base_price -> 주기별 기준 가격
 * 반환값: 시간순 정규화 kline
 * 작성 날짜: 2026/08/20
 */
function create_market_kline(
    interval: NormalizedKline['interval'],
    index: number,
    base_price: number,
): NormalizedKline {
    const open_time = 1_700_000_000_000 + (index * 60_000);
    const close = base_price + index;

    return {
        symbol: 'ETHUSDT',
        interval,
        open_time,
        close_time: open_time + 59_999,
        open: close - 1,
        high: close + 2,
        low: close - 2,
        close,
        volume: 100 + index,
        is_closed: index < 19,
    };
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

    it('실시간 MarketSnapshot에서 현재 봉과 EMA9·볼린저밴드·거래량을 투영한다', () => {
        const view_model = create_demo_view_model();
        const { controller } = create_recording_controller(view_model);
        const market_snapshot: RealtimeChartDataSnapshot = {
            data_status: 'live',
            klines_by_interval: {
                '1m': Array.from({ length: 20 }, (_, index) => create_market_kline('1m', index, 1_000)),
                '30m': Array.from({ length: 20 }, (_, index) => create_market_kline('30m', index, 2_000)),
                '4h': Array.from({ length: 20 }, (_, index) => create_market_kline('4h', index, 3_000)),
                '1d': Array.from({ length: 20 }, (_, index) => create_market_kline('1d', index, 4_000)),
            },
            status_message: null,
            symbol: 'ETHUSDT',
            updated_at: Date.parse('2026-08-20T03:34:56Z'),
        };
        const chart_props = present_dashboard_props(view_model, controller, market_snapshot).chart;

        expect(view_model.chart.interval).toBe('30m');
        expect(chart_props.dataStatus).toBe('live');
        expect(chart_props.symbol).toBe('ETHUSDT');
        expect(chart_props.timestampLabel).toBe('2026.08.20 · 12:34:56 KST');
        expect(chart_props.candles).toHaveLength(20);
        expect(chart_props.candles[0]).toMatchObject({ close: 2_000, volume: 100 });
        expect(chart_props.ema).toHaveLength(12);
        expect(chart_props.bollingerUpper).toHaveLength(1);
        expect(chart_props.bollingerLower).toHaveLength(1);
    });

    it('현재 주기의 과거 봉 상태와 요청 callback을 차트 Boundary에 연결한다', () => {
        const view_model = create_demo_view_model();
        const { controller } = create_recording_controller(view_model);
        const load_earlier_klines = vi.fn();
        const market_runtime: RealtimeChartDataRuntime = {
            data_status: 'live',
            history_load_state_by_interval: {
                '1m': { error_message: null, is_exhausted: false, is_loading: false },
                '30m': { error_message: null, is_exhausted: false, is_loading: true },
                '4h': { error_message: null, is_exhausted: false, is_loading: false },
                '1d': { error_message: null, is_exhausted: false, is_loading: false },
            },
            klines_by_interval: {
                '1m': [],
                '30m': [create_market_kline('30m', 0, 2_000)],
                '4h': [],
                '1d': [],
            },
            load_earlier_klines,
            status_message: null,
            symbol: 'ETHUSDT',
            updated_at: Date.parse('2026-08-20T03:34:56Z'),
        };
        const chart_props = present_dashboard_props(view_model, controller, market_runtime).chart;

        expect(chart_props.historyLoading).toBe(true);
        expect(chart_props.historyExhausted).toBe(false);

        chart_props.onLoadEarlier?.();

        expect(load_earlier_klines).toHaveBeenCalledOnce();
        expect(load_earlier_klines).toHaveBeenCalledWith('30m');
    });
});
