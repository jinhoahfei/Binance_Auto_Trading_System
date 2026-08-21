import { describe, expect, it } from 'vitest';
import { DEFAULT_TRADING_LOGIC_COVERAGE } from '../../shared/contracts';
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

    it('Phase 6: 미지원 REGIME 선택은 유지하되 시작 명령은 unavailable 안내로 차단한다', () => {
        const command_adapter = new FakeUiCommandAdapter();
        const facade = new UiApplicationFacade(command_adapter, {
            today: '2026-08-21',
            applied_regime: 'type2',
            command_enabled: true,
        });

        facade.start();
        facade.dispatch({ type: 'API_CONNECTED', sequence: 1 });
        facade.dispatch({ type: 'START_TRADING_CLICKED' });

        expect(facade.get_view_model().regime.applied).toBe('type2');
        expect(facade.get_view_model().active_modal).toBe('trading_unavailable_notice');
        expect(facade.get_view_model().trading.unavailable_reason).toBe('unsupported_logic');
        expect(command_adapter.command_records).toHaveLength(0);
        facade.stop();
    });

    it('Phase 6: live command 비활성 상태는 지원 REGIME도 명령 없이 차단한다', () => {
        const command_adapter = new FakeUiCommandAdapter();
        const facade = new UiApplicationFacade(command_adapter, {
            today: '2026-08-21',
            applied_regime: 'type0',
            command_enabled: false,
        });

        facade.start();
        facade.dispatch({ type: 'API_CONNECTED', sequence: 1 });
        facade.dispatch({ type: 'START_TRADING_CLICKED' });

        expect(facade.get_view_model().active_modal).toBe('trading_unavailable_notice');
        expect(facade.get_view_model().trading.unavailable_reason).toBe('command_disabled');
        expect(command_adapter.command_records).toHaveLength(0);
        facade.stop();
    });

    it('메시지 4~5: coherent backend snapshot을 한 번만 발행하고 UI-local 상태를 보존한다', () => {
        const facade = new UiApplicationFacade(new FakeUiCommandAdapter(), {
            today: '2026-08-21',
        });

        facade.start();
        facade.dispatch({ type: 'CHART_INTERVAL_SELECTED', interval: '4h' });
        facade.dispatch({ type: 'SHOW_TRADE_HISTORY' });
        facade.dispatch({ type: 'REGIME_TYPE_CLICKED', regime: 'type4' });
        expect(facade.get_view_model().active_modal).toBe('regime_change_confirmation');

        let notification_count = 0;
        const unsubscribe = facade.subscribe(() => {
            notification_count += 1;
        });
        notification_count = 0;

        facade.dispatch({
            type: 'BACKEND_SNAPSHOT_SYNCHRONIZED',
            snapshot: {
                last_sequence: 12,
                trading_symbol: 'ETH/USDT',
                recommended_regime: 'type1',
                applied_regime: 'type0',
                regime_metrics: [],
                logic_coverage: DEFAULT_TRADING_LOGIC_COVERAGE,
                command_enabled: false,
                recent_trades: [],
                history_records: [],
                scale_in_percentage: 40,
                scale_out_percentage: 60,
                account_strategy: {
                    appliedState: 'WAITING',
                    profitAmount: '10 USDT',
                    profitRate: '1.00%',
                    status: '매매 중지',
                    statusTone: 'neutral',
                },
                account_asset: {
                    ethAmount: '1',
                    ethValue: '3,000 USDT',
                    krwValue: '-',
                    quoteAsset: 'USDT',
                    quoteValue: '100 USDT',
                    profitLoss: '10 USDT',
                    totalValue: '-',
                },
                trade_history_summary: {
                    dailyReturn: { value: '1.00%', tone: 'positive' },
                    sellPerformance: {
                        winRate: '50.00%',
                        completedCount: '1 / 2',
                        averageRealizedReturn: '0.50%',
                        totalRealizedPnl: '10 USDT',
                        tone: 'positive',
                    },
                    position: { quantity: '1 ETH' },
                    fees: {
                        amount: '1 USDT',
                        totalExecutedAmount: '-',
                        averageSlippage: '-',
                    },
                },
                is_trading: false,
                has_open_position: true,
                trading_state_label: 'WAITING',
            },
        });

        const view_model = facade.get_view_model();

        expect(notification_count).toBe(1);
        expect(view_model.route).toBe('trade_history');
        expect(view_model.chart.interval).toBe('4h');
        expect(view_model.active_modal).toBe('regime_change_confirmation');
        expect(view_model.regime.candidate).toBe('type4');
        expect(view_model.regime.recommended).toBe('type1');
        expect(view_model.regime.applied).toBe('type0');
        expect(view_model.account_summary.asset.quoteAsset).toBe('USDT');
        expect(view_model.trade_history.symbol).toBe('ETH/USDT');
        expect(view_model.split_order.scale_in_percentage).toBe(40);
        expect(view_model.trading.has_open_position).toBe(true);

        unsubscribe();
        facade.stop();
    });

    it('Phase 6: 시작 확인 뒤 backend resync가 gate를 닫으면 stale 확인 명령을 차단한다', () => {
        const command_adapter = new FakeUiCommandAdapter();
        const facade = new UiApplicationFacade(command_adapter, {
            today: '2026-08-21',
            applied_regime: 'type0',
            command_enabled: true,
        });

        facade.start();
        facade.dispatch({ type: 'API_CONNECTED', sequence: 1 });
        facade.dispatch({ type: 'START_TRADING_CLICKED' });
        expect(facade.get_view_model().active_modal).toBe('start_confirmation');

        // 열린 확인창은 유지하되 authoritative command gate를 false로 최신화한다.
        facade.dispatch({
            type: 'BACKEND_SNAPSHOT_SYNCHRONIZED',
            snapshot: {
                last_sequence: 2,
                trading_symbol: 'ETH/USDT',
                recommended_regime: 'type0',
                applied_regime: 'type0',
                regime_metrics: [],
                logic_coverage: DEFAULT_TRADING_LOGIC_COVERAGE,
                command_enabled: false,
                recent_trades: [],
                history_records: [],
                account_strategy: {
                    appliedState: 'not_started',
                    profitAmount: '-',
                    profitRate: '-',
                    status: '매매 시작 전',
                    statusTone: 'neutral',
                },
                account_asset: {
                    ethAmount: '-',
                    ethValue: '-',
                    krwValue: '-',
                    profitLoss: '-',
                    totalValue: '-',
                },
                trade_history_summary: {
                    dailyReturn: { value: '-', tone: 'neutral' },
                    sellPerformance: {
                        winRate: '-',
                        completedCount: '0 / 0',
                        averageRealizedReturn: '-',
                        totalRealizedPnl: '-',
                        tone: 'neutral',
                    },
                    position: { quantity: '-' },
                    fees: {
                        amount: '-',
                        totalExecutedAmount: '-',
                        averageSlippage: '-',
                    },
                },
                is_trading: false,
                has_open_position: false,
                trading_state_label: 'not_started',
            },
        });
        expect(facade.get_view_model().active_modal).toBe('start_confirmation');

        facade.dispatch({ type: 'START_TRADING_CONFIRMED' });

        expect(facade.get_view_model().active_modal).toBe('trading_unavailable_notice');
        expect(facade.get_view_model().trading.unavailable_reason).toBe('command_disabled');
        expect(command_adapter.command_records).toHaveLength(0);
        facade.stop();
    });
});
