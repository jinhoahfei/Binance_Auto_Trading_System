import { map_trade_record, validate_trade_snapshot } from '../../shared/api/backendEventMapper';
import { create_backend_snapshot_fixture } from '../../shared/api/backendTestFixtures';
import type {
    RealtimeChartDataRuntime,
    RealtimeChartDataSnapshot,
} from '../../features/price-chart';
import type { NormalizedKline } from '../../features/price-chart/data';
import { DEFAULT_DASHBOARD_PROPS } from '../../routes/dashboard';
import type { TradeRecord } from '../../shared/contracts';
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
    it('실제 매수 평균 체결가와 ETH·USDT 원 수수료를 반올림 없이 표시한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const source = create_backend_snapshot_fixture().recent_trades[0]!;
        const buy_source = {
            ...source, average_fill_price: '2444.04000000', entry_price: '2444.04000000',
            market_price_at_decision: '2444.98000000', fee_amount: '0.00000400', fee_asset: 'ETH',
            fee_quote_amount: '0.0097761600000000',
        };
        const sell_source = {
            ...buy_source, trade_id: 'external-sell', side: 'SELL', client_order_id: 'web-manual-sell',
            average_fill_price: '2421.86000000', market_price_at_decision: null, exit_reason: 'EXTERNAL_MANUAL',
            fee_amount: '0.00944525', fee_asset: 'USDT', fee_quote_amount: '0.00944525',
            fee_note: '원 수수료 0.00944525 USDT',
        };
        const buy = map_trade_record(validate_trade_snapshot(buy_source));
        const sell = map_trade_record(validate_trade_snapshot(sell_source));

        const view_model = create_demo_view_model();
        const { controller } = create_recording_controller(view_model);
        const rows = present_trade_history_props({ ...view_model, trade_history: { ...view_model.trade_history, records: [buy, sell] } }, controller).rows;

        // 잘못된 입력이나 실행 실패가 정해진 오류로 전달되는지 검증한다.
        expect(rows).toMatchObject([
            { entryPrice: '2,444.04 USDT', executionPrice: '2,444.04 USDT', fee: '0.000004 ETH', feeNote: '≈ 0.00977616 USDT' },
            { entryPrice: '2,444.04 USDT', executionPrice: '2,421.86 USDT', fee: '0.00944525 USDT' },
        ]);
        expect(rows[1]?.feeNote).toBeUndefined();
        expect(buy.market_price_at_decision).toBe('2444.98000000');
        expect(buy.fee_amount).toBe('0.00000400');
        expect(sell.market_price_at_decision).toBeNull();
        const filtered = present_trade_history_props({ ...view_model, trade_history: { ...view_model.trade_history, side: 'sell', records: [sell] } }, controller);
        expect(filtered.rows[0]?.entryPrice).toBe('2,444.04 USDT');
        for (const entry_price of ['0', '-1', 'NaN', 2444.04]) {
            expect(() => validate_trade_snapshot({ ...buy_source, entry_price })).toThrow();
        }
    });

    it('아주 작은 BNB·혼합 수수료·실제 0 수수료의 단위를 보존한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const view_model = create_demo_view_model();
        const { controller } = create_recording_controller(view_model);
        const source = create_backend_snapshot_fixture().recent_trades[0]!;
        const cases = [
            { fee_asset: 'BNB', fee_amount: '0.0000000009', fee_quote_amount: '0.0000005400', expected: '0.0000000009 BNB', note: '≈ 0.00000054 USDT' },
            { fee_asset: 'MIXED', fee_amount: '0.01000054', fee_quote_amount: '0.01000054', fee_note: '원 수수료 0.0000000009 BNB + 0.01 USDT', expected: '0.01000054 USDT 환산', note: '원 수수료 0.0000000009 BNB + 0.01 USDT' },
            { fee_asset: 'USDT', fee_amount: '0.00000000', fee_quote_amount: '0.00000000', expected: '0 USDT', note: undefined },
        ];
        for (const example of cases) {
            const record = map_trade_record(validate_trade_snapshot({ ...source, ...example }));
            const rows = present_trade_history_props({ ...view_model, trade_history: { ...view_model.trade_history, records: [record] } }, controller).rows;
            expect(rows[0]?.fee).toBe(example.expected);
            expect(rows[0]?.feeNote).toBe(example.note);
        }
    });

    it('외부 수동 매도는 두 거래 화면에서 구분하고 모르는 판단 시세는 null로 유지한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const source = {
            ...create_backend_snapshot_fixture().recent_trades[0]!,
            side: 'SELL', client_order_id: 'web-manual-sell', exit_reason: 'EXTERNAL_MANUAL',
            market_price_at_decision: null, realized_pnl: '-0.10', realized_return_rate: '-1.10', allocated_cost_basis: '9.54',
        };
        const record = map_trade_record(validate_trade_snapshot(source));

        const view_model = create_demo_view_model();
        const updated: AppViewModel = {
            ...view_model,
            trader_panel: { ...view_model.trader_panel, trades: [record] },
            trade_history: { ...view_model.trade_history, records: [record] },
        };
        const { controller } = create_recording_controller(updated);

        // 잘못된 입력이나 실행 실패가 정해진 오류로 전달되는지 검증한다.
        expect(present_dashboard_props(updated, controller).trader.orders[0]?.strategy).toBe('외부 수동 매도');
        expect(present_trade_history_props(updated, controller).rows[0]?.strategy).toBe('외부 수동 매도');
        expect(record.market_price_at_decision).toBeNull();
        expect(record.strategy).toBe(source.strategy);
        expect(() => validate_trade_snapshot({ ...source, exit_reason: 'STOP' })).toThrow();
        expect(() => validate_trade_snapshot({ ...source, side: 'BUY' })).toThrow();
        expect(() => validate_trade_snapshot({ ...source, client_order_id: 'bat-app-order' })).toThrow();
    });

    it('최근 체결과 상세 거래의 ETH 수량은 4자리, 금액과 수익률은 2자리로 표시한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const view_model = create_demo_view_model();
        const buy_trade: TradeRecord = Object.freeze({
            id: 'live-buy',
            quote_asset: 'USDT',
            occurred_at: '2026-09-05T00:00:00Z',
            side: 'buy',
            regime: 'type0',
            strategy: 'CASE_C',
            price: '2451.42500000',
            entry_price: null,
            market_price_at_decision: '2451.42000000',
            quantity: '0.00415000',
            total: '10.1734137500',
            fee: '0.000000000000000000',
            profit_rate: null,
            realized_pnl: null,
            exit_reason: null,
        });
        const sell_trade: TradeRecord = Object.freeze({
            ...buy_trade,
            id: 'live-sell',
            side: 'sell',
            entry_price: '2450.00500000',
            profit_rate: '-0.38233420',
            realized_pnl: '-0.03837600',
        });
        const records = [buy_trade, sell_trade];
        const live_view_model: AppViewModel = {
            ...view_model,
            trader_panel: { ...view_model.trader_panel, trades: records },
            trade_history: { ...view_model.trade_history, records },
        };
        const { controller } = create_recording_controller(live_view_model);
        const dashboard_props = present_dashboard_props(live_view_model, controller);
        const history_props = present_trade_history_props(live_view_model, controller);

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(dashboard_props.trader.orders).toMatchObject([
            { price: '2,451.43 USDT', secondaryValue: '0.0042 ETH' },
            { price: '2,451.43 USDT', secondaryValue: '-0.38%' },
        ]);
        expect(history_props.rows).toMatchObject([
            {
                entryPrice: '-',
                executionPrice: '2,451.43 USDT',
                quantity: '0.0042',
                orderAmount: '10.17 USDT',
                fee: '0.00 USDT',
                previousBuyReturn: '-',
                realizedPnl: '-',
            },
            {
                entryPrice: '2,450.01 USDT',
                executionPrice: '2,451.43 USDT',
                quantity: '0.0042',
                orderAmount: '10.17 USDT',
                fee: '0.00 USDT',
                previousBuyReturn: '-0.38%',
                realizedPnl: '-0.04 USDT',
            },
        ]);
        expect(buy_trade.quantity).toBe('0.00415000');
        expect(buy_trade.price).toBe('2451.42500000');
        expect(sell_trade.realized_pnl).toBe('-0.03837600');
    });

    it('authoritative risk와 process ownership 상태를 계산 없이 TraderPanel 경계에 전달한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const view_model = create_demo_view_model();
        const last_risk_budget = {
            policy_version: 4,
            market_version: 7,
            account_version: 3,
            context_version: 9,
            current_position_notional: '125.50',
            reserved_buy_notional: '24.25',
            candidate_order_notional: '50.25',
            projected_position_notional: '200.00',
            daily_realized_pnl: '-12.75',
            unrealized_pnl: '-3.50',
            daily_loss: '12.75',
            manual_kill_active: false,
        } as const;
        const risk_view_model: AppViewModel = {
            ...view_model,
            trading: {
                ...view_model.trading,
                configured_risk_policy_version: 4,
                max_order_notional: null,
                max_position_notional: null,
                max_daily_loss: null,
                daily_loss_scope: 'REALIZED_ONLY',
                manual_kill_behavior: 'BLOCK_NEW_ORDERS',
                last_risk_decision_allowed: false,
                last_risk_budget,
                manual_kill_active: true,
                manual_kill_cleanup_complete: false,
                manual_kill_activation_behavior: 'CANCEL_AND_LIQUIDATE',
                manual_kill_activation_policy_version: 3,
                process_ownership_ambiguous: true,
                risk_block_reason: 'MANUAL_KILL_SWITCH_ACTIVE',
                risk_policy_availability: 'CONFIGURED',
                session_risk_policy_version: 3,
            },
        };
        const { controller } = create_recording_controller(risk_view_model);
        const trader_props = present_dashboard_props(risk_view_model, controller).trader;

        // Presenter는 backend 값을 재해석하거나 raw detail을 추가하지 않고 feature Boundary로 전달한다.
        expect(trader_props).toMatchObject({
            configured_risk_policy_version: 4,
            max_order_notional: null,
            max_position_notional: null,
            max_daily_loss: null,
            daily_loss_scope: 'REALIZED_ONLY',
            manual_kill_behavior: 'BLOCK_NEW_ORDERS',
            last_risk_decision_allowed: false,
            last_risk_budget,
            manual_kill_active: true,
            manual_kill_cleanup_complete: false,
            manual_kill_activation_behavior: 'CANCEL_AND_LIQUIDATE',
            manual_kill_activation_policy_version: 3,
            process_ownership_ambiguous: true,
            risk_block_reason: 'MANUAL_KILL_SWITCH_ACTIVE',
            risk_policy_availability: 'CONFIGURED',
            session_risk_policy_version: 3,
        });
        expect(trader_props.last_risk_budget).toBe(last_risk_budget);
    });

    it('account와 history summary actor snapshot을 route props에 투영한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const view_model = create_demo_view_model();
        const { controller } = create_recording_controller(view_model);
        const dashboard_props = present_dashboard_props(view_model, controller);
        const history_props = present_trade_history_props(view_model, controller);

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
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
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
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

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(history_props.description).toBe('오늘 · ETH/KRW · 전체 · 조회 실패');
        expect(history_props.rows).toEqual([]);
        expect(history_props.emptyState).toMatchObject({
            description: 'history unavailable',
            actionLabel: '다시 시도',
        });
        expect(history_props.filtersDisabled).toBe(false);

        history_props.onStartTrading?.();
        expect(intents).toEqual([{ type: 'REFRESH_TRADE_HISTORY' }]);
    });

    it('거래 내역 loading을 오래된 행이나 empty CTA 없이 실제 busy props로 투영한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const view_model = create_demo_view_model();
        const loading_view_model: AppViewModel = {
            ...view_model,
            trade_history: {
                ...view_model.trade_history,
                status: 'loading',
                is_loading: true,
            },
        };
        const { controller } = create_recording_controller(loading_view_model);
        const history_props = present_trade_history_props(loading_view_model, controller);

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(history_props.description).toBe('오늘 · ETH/KRW · 전체 · 조회 중');
        expect(history_props.rows).toEqual([]);
        expect(history_props.isLoading).toBe(true);
        expect(history_props.filtersDisabled).toBe(true);
        expect(history_props.emptyState).toMatchObject({
            title: '거래 내역을 불러오는 중입니다',
        });
        expect(history_props.onStartTrading).toBeUndefined();
    });

    it('drawing hover·context-menu·delete intent를 facade 계약으로 모두 변환한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
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

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(intents).toEqual([
            { type: 'CHART_LINE_HOVER_ENTERED', line_id: 'line-1' },
            { type: 'CHART_LINE_HOVER_EXITED' },
            { type: 'CHART_LINE_CONTEXT_MENU_REQUESTED', x: 120, y: 84 },
            { type: 'CHART_LINE_DELETE_REQUESTED' },
            { type: 'CHART_LINE_CONTEXT_MENU_OUTSIDE_CLICKED' },
        ]);
    });

    it('실시간 MarketSnapshot에서 현재 봉과 EMA9·볼린저밴드·거래량을 투영한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
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

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
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
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
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

        // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
        expect(chart_props.historyLoading).toBe(true);
        expect(chart_props.historyExhausted).toBe(false);

        chart_props.onLoadEarlier?.();

        expect(load_earlier_klines).toHaveBeenCalledOnce();
        expect(load_earlier_klines).toHaveBeenCalledWith('30m');
    });
});
