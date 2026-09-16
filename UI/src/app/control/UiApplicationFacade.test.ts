import { describe, expect, it, vi } from 'vitest';
import type { TradeHistoryDetails } from '../../shared/contracts';
import { DEFAULT_TRADING_LOGIC_COVERAGE } from '../../shared/contracts';
import {
    CHART_DRAWING_FIXTURE,
    FakeUiCommandAdapter,
    TRADE_RECORD_FIXTURES,
} from '../../shared/testing';
import { map_backend_snapshot } from '../../shared/api';
import { create_backend_snapshot_fixture } from '../../shared/api/backendTestFixtures';
import { UiApplicationFacade } from './UiApplicationFacade';

/**
 * 함수 이름: wait_for_trade_history_settlement()
 * 기능: facade가 시작한 거래 상세 Promise와 actor 전이를 다음 event loop까지 기다린다.
 * 인자: 없음
 * 반환값: 조회 정착 후 완료되는 Promise
 * 작성 날짜: 2026/08/23
 */
async function wait_for_trade_history_settlement(): Promise<void> {
    await new Promise<void>((resolve) => {
        setTimeout(resolve, 0);
    });
}

// 직접 구성한 lifecycle fixture도 schema v3의 authoritative risk 상태를 빠짐없이 제공한다.
const AUTHORITATIVE_RISK_STATE = {
    risk_policy_availability: 'CONFIGURED',
    configured_risk_policy_version: 1,
    max_order_notional: null,
    max_position_notional: null,
    max_daily_loss: null,
    daily_loss_scope: 'REALIZED_ONLY',
    manual_kill_behavior: 'CANCEL_AND_LIQUIDATE',
    session_risk_policy_version: 1,
    risk_control_version: 0,
    manual_kill_active: false,
    manual_kill_cleanup_complete: true,
    manual_kill_activation_behavior: null,
    manual_kill_activation_policy_version: null,
    last_risk_decision_allowed: null,
    last_risk_budget: null,
    risk_block_reason: null,
    process_ownership_ambiguous: false,
} as const;

describe('UiApplicationFacade', () => {
    it('test_show_trade_details_message_trace: SHOW intent를 Case 3 1계열 live query와 render까지 연결한다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        const facade = new UiApplicationFacade(command_adapter, {
            today: '2026-08-23',
            history_records: [],
        });
        const observed_message_ids: Array<string> = ['1'];

        facade.start();
        expect(facade.dispatch({ type: 'SHOW_TRADE_HISTORY' })).toBe(true);
        observed_message_ids.push('1.1', '1.1.1');
        expect(facade.get_view_model().route).toBe('trade_history');
        expect(facade.get_view_model().trade_history.status).toBe('loading');
        await wait_for_trade_history_settlement();

        expect(command_adapter.command_records).toContainEqual({
            name: 'load_trade_history',
            payload: { period: 'today', side: 'all' },
        });
        observed_message_ids.push('1.1.2');
        expect(facade.get_view_model().trade_history.status).toBe('ready');
        observed_message_ids.push('1.1.3');
        expect(observed_message_ids).toEqual([
            '1',
            '1.1',
            '1.1.1',
            '1.1.2',
            '1.1.3',
        ]);
        facade.stop();
    });

    it('test_trade_history_filter_message_trace: filter intent를 Case 3 2계열 combined query와 render까지 연결한다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        const facade = new UiApplicationFacade(command_adapter, {
            today: '2026-08-23',
        });

        facade.start();
        facade.dispatch({ type: 'SHOW_TRADE_HISTORY' });
        await wait_for_trade_history_settlement();
        facade.dispatch({ type: 'HISTORY_SIDE_SELECTED', side: 'sell' });
        await wait_for_trade_history_settlement();
        command_adapter.command_records.splice(0);

        const observed_message_ids: Array<string> = ['2'];
        expect(facade.dispatch({
            type: 'HISTORY_PERIOD_SELECTED',
            period: 'last30days',
        })).toBe(true);
        observed_message_ids.push('2.1', '2.1.1');
        expect(facade.get_view_model().trade_history.status).toBe('loading');
        await wait_for_trade_history_settlement();

        expect(command_adapter.command_records).toEqual([{
            name: 'load_trade_history',
            payload: { period: 'last30days', side: 'sell' },
        }]);
        observed_message_ids.push('2.1.2');
        expect(facade.get_view_model().trade_history.status).toBe('ready');
        observed_message_ids.push('2.1.3');
        expect(observed_message_ids).toEqual([
            '2',
            '2.1',
            '2.1.1',
            '2.1.2',
            '2.1.3',
        ]);
        facade.stop();
    });

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

    it('ACCOUNT/PERFORMANCE intent가 D-12 summary의 holdings와 성과 범위를 독립 갱신한다', () => {
        const facade = new UiApplicationFacade(new FakeUiCommandAdapter(), {
            today: '2026-08-23',
        });

        facade.start();
        facade.dispatch({
            type: 'TRADE_HISTORY_HOLDINGS_UPDATED',
            position: { quantity: '2.5 ETH' },
        });
        facade.dispatch({
            type: 'TRADE_HISTORY_PERFORMANCE_UPDATED',
            daily_return: { value: '+1.25%', tone: 'positive' },
            sell_performance: {
                winRate: '60.00%',
                completedCount: '3 / 5',
                averageRealizedReturn: '+0.50%',
                totalRealizedPnl: '12 USDT',
                tone: 'positive',
            },
            fees: {
                amount: '0.25 USDT',
                totalExecutedAmount: '-',
                averageSlippage: '-',
            },
        });

        expect(facade.get_view_model().trade_history.summary).toMatchObject({
            position: { quantity: '2.5 ETH' },
            dailyReturn: { value: '+1.25%' },
            sellPerformance: { completedCount: '3 / 5' },
            fees: { amount: '0.25 USDT' },
        });
        facade.stop();
    });

    it('조회 중 event보다 오래된 초기 summary는 버리고 이후 명시적 refresh summary는 적용한다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        let resolve_stale_query: ((details: TradeHistoryDetails) => void) | undefined;
        let query_count = 0;
        vi.spyOn(command_adapter, 'load_trade_history').mockImplementation(async () => {
            query_count += 1;
            if (query_count === 1) {
                return new Promise<TradeHistoryDetails>((resolve) => {
                    resolve_stale_query = resolve;
                });
            }

            return {
                records: TRADE_RECORD_FIXTURES,
                summary: {
                    ...command_adapter.trade_history_summary,
                    dailyReturn: { value: '+2.00%', tone: 'positive' },
                    position: { quantity: '3 ETH' },
                },
            };
        });
        const facade = new UiApplicationFacade(command_adapter, {
            today: '2026-08-23',
        });

        facade.start();
        facade.dispatch({ type: 'SHOW_TRADE_HISTORY' });
        await wait_for_trade_history_settlement();
        expect(facade.get_view_model().trade_history.status).toBe('loading');

        facade.dispatch({
            type: 'TRADE_HISTORY_HOLDINGS_UPDATED',
            position: { quantity: '2.5 ETH' },
        });
        facade.dispatch({
            type: 'TRADE_HISTORY_PERFORMANCE_UPDATED',
            daily_return: { value: '+1.00%', tone: 'positive' },
            sell_performance: {
                winRate: '50.00%',
                completedCount: '1 / 2',
                averageRealizedReturn: '+0.50%',
                totalRealizedPnl: '5 USDT',
                tone: 'positive',
            },
            fees: {
                amount: '0.10 USDT',
                totalExecutedAmount: '-',
                averageSlippage: '-',
            },
        });
        resolve_stale_query?.({
            records: [TRADE_RECORD_FIXTURES[0]!],
            summary: {
                ...command_adapter.trade_history_summary,
                dailyReturn: { value: '-9.00%', tone: 'negative' },
                position: { quantity: '99 ETH' },
            },
        });
        await wait_for_trade_history_settlement();

        expect(facade.get_view_model().trade_history.records).toEqual([
            TRADE_RECORD_FIXTURES[0],
        ]);
        expect(facade.get_view_model().trade_history.summary).toMatchObject({
            dailyReturn: { value: '+1.00%' },
            position: { quantity: '2.5 ETH' },
        });

        // 새 revision에서 시작한 명시적 refresh는 KST rollover를 포함한 최신 composite를 적용한다.
        facade.dispatch({ type: 'REFRESH_TRADE_HISTORY' });
        await wait_for_trade_history_settlement();
        expect(facade.get_view_model().trade_history.summary).toMatchObject({
            dailyReturn: { value: '+2.00%' },
            position: { quantity: '3 ETH' },
        });
        facade.stop();
    });

    it('ORDER_EXECUTED는 recent orders를 항상 갱신하고 active history에서만 현재 query를 refresh한다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        const facade = new UiApplicationFacade(command_adapter, {
            today: '2026-08-23',
            recent_trades: [],
        });

        facade.start();
        facade.dispatch({ type: 'SHOW_TRADE_HISTORY' });
        await wait_for_trade_history_settlement();
        const query_count_before_order = command_adapter.command_records.filter((record) => {
            return record.name === 'load_trade_history';
        }).length;

        facade.dispatch({
            type: 'BUY_ORDER_EXECUTED',
            trade: TRADE_RECORD_FIXTURES[0]!,
        });
        await wait_for_trade_history_settlement();
        expect(facade.get_view_model().trader_panel.trades[0]).toEqual(TRADE_RECORD_FIXTURES[0]);
        expect(command_adapter.command_records.filter((record) => {
            return record.name === 'load_trade_history';
        })).toHaveLength(query_count_before_order + 1);

        facade.dispatch({ type: 'BACK_TO_DASHBOARD' });
        facade.dispatch({
            type: 'SELL_ORDER_EXECUTED',
            trade: TRADE_RECORD_FIXTURES[1]!,
        });
        await wait_for_trade_history_settlement();
        expect(facade.get_view_model().trader_panel.trades[0]).toEqual(TRADE_RECORD_FIXTURES[1]);
        expect(command_adapter.command_records.filter((record) => {
            return record.name === 'load_trade_history';
        })).toHaveLength(query_count_before_order + 1);
        facade.stop();
    });

    it('full resync의 recent_trades로 filtered table을 덮지 않고 active current query를 다시 읽는다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        const facade = new UiApplicationFacade(command_adapter, {
            today: '2026-08-23',
        });

        facade.start();
        facade.dispatch({ type: 'SHOW_TRADE_HISTORY' });
        await wait_for_trade_history_settlement();
        facade.dispatch({ type: 'HISTORY_SIDE_SELECTED', side: 'sell' });
        await wait_for_trade_history_settlement();
        expect(facade.get_view_model().trade_history.records).toEqual([
            TRADE_RECORD_FIXTURES[0],
        ]);

        const synchronized_snapshot = map_backend_snapshot(
            create_backend_snapshot_fixture(),
            '2026-08-23',
        ).server_snapshot;
        facade.dispatch({
            type: 'BACKEND_SNAPSHOT_SYNCHRONIZED',
            snapshot: synchronized_snapshot,
        });

        expect(facade.get_view_model().trade_history.status).toBe('loading');
        expect(facade.get_view_model().trade_history.records).toEqual([
            TRADE_RECORD_FIXTURES[0],
        ]);
        await wait_for_trade_history_settlement();
        expect(command_adapter.command_records.at(-1)).toEqual({
            name: 'load_trade_history',
            payload: { period: 'today', side: 'sell' },
        });
        expect(facade.get_view_model().trade_history.records).toEqual([
            TRADE_RECORD_FIXTURES[0],
        ]);
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

    it('Phase 9: 복구 Position은 별도 확인 modal과 liquidation command로 조정한다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        const facade = new UiApplicationFacade(command_adapter, {
            today: '2026-08-24',
            applied_regime: 'type0',
            command_enabled: true,
            is_trading: false,
            has_open_position: true,
        });

        facade.start();

        // 열린 복구 Position이 있는 동안 facade도 새 자동매매 시작 intent를 fail closed한다.
        expect(facade.dispatch({ type: 'START_TRADING_CLICKED' })).toBe(false);
        expect(facade.get_view_model().active_modal).toBeNull();
        expect(facade.dispatch({
            type: 'STOP_TRADING_CLICKED',
            has_open_position: true,
        })).toBe(true);
        expect(facade.get_view_model().active_modal).toBe('force_sell_stop_confirmation');
        expect(facade.get_view_model().trading.is_recovery_liquidation).toBe(true);

        facade.dispatch({ type: 'RECOVERED_POSITION_LIQUIDATION_CONFIRMED' });
        await wait_for_trade_history_settlement();

        expect(command_adapter.command_records).toEqual([{
            name: 'liquidate_recovered_position',
            payload: null,
        }]);
        expect(facade.get_view_model().trading).toMatchObject({
            is_trading: false,
            is_pending: false,
            is_recovery_liquidation: false,
            has_open_position: false,
        });
        expect(facade.get_view_model().active_modal).toBeNull();
        facade.stop();
    });

    it('Phase 9: STOPPING 복구 청산은 실행 상태를 되살리지 않고 start를 계속 차단한다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        command_adapter.recovered_position_liquidation_receipt = {
            status: 'stopping',
            session_id: '62c511b2-ea5c-43ac-bc36-e96eb39c85aa',
            version: 3,
        };
        const facade = new UiApplicationFacade(command_adapter, {
            today: '2026-08-24',
            command_enabled: true,
            is_trading: false,
            has_open_position: true,
        });

        facade.start();
        facade.dispatch({
            type: 'STOP_TRADING_CLICKED',
            has_open_position: true,
        });
        facade.dispatch({ type: 'RECOVERED_POSITION_LIQUIDATION_CONFIRMED' });
        await wait_for_trade_history_settlement();

        expect(facade.get_view_model().trading).toMatchObject({
            is_trading: false,
            is_pending: true,
            is_recovery_liquidation: true,
            has_open_position: true,
        });
        expect(facade.dispatch({ type: 'START_TRADING_CLICKED' })).toBe(false);
        expect(command_adapter.command_records).toEqual([{
            name: 'liquidate_recovered_position',
            payload: null,
        }]);
        facade.stop();
    });

    it('exit progress stays pending until the backend operation and native exit complete', async () => {
        const commands = new FakeUiCommandAdapter();
        let finish!: () => void;
        const shutdown = vi.spyOn(commands, 'shutdown_application').mockImplementation(() => new Promise<void>((resolve) => { finish = resolve; }));
        const facade = new UiApplicationFacade(commands, { today: '2026-09-16', is_trading: false, has_open_position: true });
        facade.start(); facade.dispatch({ type: 'APP_EXIT_CLICKED' }); facade.dispatch({ type: 'FORCE_SELL_EXIT_CONFIRMED' });
        await wait_for_trade_history_settlement();
        expect(facade.get_view_model().app_exit.status).toBe('shutting_down');
        expect(facade.get_view_model().active_modal).toBe('exit_processing');
        expect(shutdown).toHaveBeenCalledExactlyOnceWith(true);
        finish(); await wait_for_trade_history_settlement();
        expect(facade.get_view_model().app_exit.is_final).toBe(true); facade.stop();
    });

    it('메시지 4~5: coherent backend snapshot을 한 번만 발행하고 UI-local 상태를 보존한다', () => {
        const facade = new UiApplicationFacade(new FakeUiCommandAdapter(), {
            today: '2026-08-21',
        });

        facade.start();
        facade.dispatch({ type: 'CHART_INTERVAL_SELECTED', interval: '4h' });
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
                trading_version: 4,
                trading_session_id: null,
                trading_symbol: 'ETH/USDT',
                recommended_regime: 'type1',
                applied_regime: 'type0',
                regime_metrics: [],
                logic_coverage: DEFAULT_TRADING_LOGIC_COVERAGE,
                command_enabled: false,
                ...AUTHORITATIVE_RISK_STATE,
                risk_control_version: 3,
                manual_kill_active: true,
                manual_kill_cleanup_complete: false,
                manual_kill_activation_behavior: 'CANCEL_AND_LIQUIDATE',
                manual_kill_activation_policy_version: 1,
                last_risk_decision_allowed: false,
                last_risk_budget: {
                    policy_version: 1,
                    market_version: 7,
                    account_version: 3,
                    context_version: 4,
                    current_position_notional: '125.50',
                    reserved_buy_notional: '24.25',
                    candidate_order_notional: '50.25',
                    projected_position_notional: '200.00',
                    daily_realized_pnl: '-12.75',
                    unrealized_pnl: '-3.50',
                    daily_loss: '12.75',
                    manual_kill_active: true,
                },
                risk_block_reason: 'MANUAL_KILL_SWITCH_ACTIVE',
                process_ownership_ambiguous: true,
                recent_trades: [],
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
                trading_state_label: 'not_started',
            },
        });

        const view_model = facade.get_view_model();

        expect(notification_count).toBe(1);
        expect(view_model.route).toBe('dashboard');
        expect(view_model.chart.interval).toBe('4h');
        expect(view_model.active_modal).toBe('regime_change_confirmation');
        expect(view_model.regime.candidate).toBe('type4');
        expect(view_model.regime.recommended).toBe('type1');
        expect(view_model.regime.applied).toBe('type0');
        expect(view_model.account_summary.asset.quoteAsset).toBe('USDT');
        expect(view_model.trade_history.symbol).toBe('ETH/USDT');
        expect(view_model.split_order.scale_in_percentage).toBe(40);
        expect(view_model.trading.has_open_position).toBe(true);
        expect(view_model.trading).toMatchObject({
            risk_policy_availability: 'CONFIGURED',
            configured_risk_policy_version: 1,
            max_order_notional: null,
            max_position_notional: null,
            max_daily_loss: null,
            daily_loss_scope: 'REALIZED_ONLY',
            manual_kill_behavior: 'CANCEL_AND_LIQUIDATE',
            session_risk_policy_version: 1,
            risk_control_version: 3,
            manual_kill_active: true,
            manual_kill_cleanup_complete: false,
            manual_kill_activation_behavior: 'CANCEL_AND_LIQUIDATE',
            manual_kill_activation_policy_version: 1,
            last_risk_decision_allowed: false,
            last_risk_budget: {
                policy_version: 1,
                market_version: 7,
                account_version: 3,
                context_version: 4,
                current_position_notional: '125.50',
                reserved_buy_notional: '24.25',
                candidate_order_notional: '50.25',
                projected_position_notional: '200.00',
                daily_realized_pnl: '-12.75',
                unrealized_pnl: '-3.50',
                daily_loss: '12.75',
                manual_kill_active: true,
            },
            risk_block_reason: 'MANUAL_KILL_SWITCH_ACTIVE',
            process_ownership_ambiguous: true,
        });

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
                trading_version: 5,
                trading_session_id: null,
                trading_symbol: 'ETH/USDT',
                recommended_regime: 'type0',
                applied_regime: 'type0',
                regime_metrics: [],
                logic_coverage: DEFAULT_TRADING_LOGIC_COVERAGE,
                command_enabled: false,
                ...AUTHORITATIVE_RISK_STATE,
                recent_trades: [],
                scale_in_percentage: 50,
                scale_out_percentage: 50,
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

    it('화면 통신 장애는 실행 중 매매·포지션을 유지하며 중지 명령이나 API 팝업을 만들지 않는다', () => {
        const adapter = new FakeUiCommandAdapter();
        const facade = new UiApplicationFacade(adapter, { today: '2026-09-16', applied_regime: 'type0', command_enabled: true });
        facade.start();
        facade.dispatch({ type: 'API_CONNECTED', sequence: 1 });
        facade.dispatch({ type: 'BACKEND_TRADING_STARTED' });
        facade.dispatch({ type: 'POSITION_UPDATED', has_open_position: true });
        const before = facade.get_view_model().trading;
        facade.dispatch({ type: 'UI_CONNECTION_DISCONNECTED', reason: 'EVENT_STREAM_STALE' });
        expect(facade.get_view_model().trading).toEqual(before);
        expect(facade.get_view_model().connection.is_online).toBe(false);
        expect(facade.get_view_model().active_modal).toBeNull();
        expect(adapter.command_records).toHaveLength(0);
        facade.stop();
    });

    it('Phase 7 lifecycle event는 stopping을 pending으로 유지하고 terminated에서만 완료한다', () => {
        const facade = new UiApplicationFacade(new FakeUiCommandAdapter(), {
            today: '2026-08-21',
            applied_regime: 'type0',
            command_enabled: true,
        });

        facade.start();
        facade.dispatch({ type: 'BACKEND_TRADING_STARTED' });
        facade.dispatch({ type: 'POSITION_UPDATED', has_open_position: true });
        facade.dispatch({
            type: 'TRADING_SESSION_SYNCHRONIZED',
            status: 'stopping',
            version: 5,
            session_id: '62c511b2-ea5c-43ac-bc36-e96eb39c85aa',
            command_enabled: false,
            ...AUTHORITATIVE_RISK_STATE,
            scale_in: '0.4',
            scale_out: '0.6',
            scale_in_percentage: 40,
            scale_out_percentage: 60,
            has_open_position: true,
            logic_coverage: DEFAULT_TRADING_LOGIC_COVERAGE,
            strategy_status: '자동매매 중지 처리 중',
            strategy_status_tone: 'neutral',
        });

        expect(facade.get_view_model().trading).toMatchObject({
            is_trading: true,
            is_pending: true,
            has_open_position: true,
        });
        expect(facade.get_view_model().split_order).toMatchObject({
            scale_in_percentage: 40,
            scale_out_percentage: 60,
        });
        expect(facade.get_view_model().chart.active_trading_logic_state).toBe('자동매매 중지 처리 중');
        expect(facade.get_view_model().account_summary.strategy).toMatchObject({
            appliedState: '자동매매 중지 처리 중',  // 실행 Case가 없는 legacy intent는 상태 문구를 공유한다.
            status: '자동매매 중지 처리 중',
            statusTone: 'neutral',
        });

        facade.dispatch({
            type: 'TRADING_SESSION_SYNCHRONIZED',
            status: 'terminated',
            version: 6,
            session_id: '62c511b2-ea5c-43ac-bc36-e96eb39c85aa',
            command_enabled: true,
            ...AUTHORITATIVE_RISK_STATE,
            scale_in: '0.4',
            scale_out: '0.6',
            scale_in_percentage: 40,
            scale_out_percentage: 60,
            has_open_position: false,
            logic_coverage: DEFAULT_TRADING_LOGIC_COVERAGE,
            strategy_status: '자동매매 종료',
            strategy_status_tone: 'neutral',
        });

        expect(facade.get_view_model().trading).toMatchObject({
            is_trading: false,
            is_pending: false,
            has_open_position: false,
        });
        expect(facade.get_view_model().account_summary.strategy.status).toBe('자동매매 종료');
        facade.stop();
    });

    it('Phase 12 abnormal sidecar event는 command 없이 restart recovery modal을 우선한다', () => {
        const command_adapter = new FakeUiCommandAdapter();
        const facade = new UiApplicationFacade(command_adapter, {
            today: '2026-08-24',
        });

        facade.start();
        facade.dispatch({ type: 'BACKEND_SIDECAR_EXITED_ABNORMALLY' });
        expect(facade.get_view_model().app_exit.status).toBe('sidecar_exit_failure');
        expect(facade.get_view_model().active_modal).toBe('sidecar_exit_failure');

        // Native·renderer close intent가 다시 와도 dead child에 shutdown command를 보내지 않는다.
        facade.dispatch({ type: 'APP_EXIT_CLICKED' });
        expect(facade.get_view_model().active_modal).toBe('sidecar_exit_failure');
        facade.dispatch({ type: 'APP_EXIT_CONFIRMED' });
        expect(facade.get_view_model().app_exit.is_final).toBe(true);
        expect(command_adapter.command_records).toEqual([]);
        facade.stop();
    });
});
