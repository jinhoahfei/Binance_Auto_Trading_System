import { create_account_summary_machine } from '../../features/account-summary/machines/accountSummaryMachine';
import { create_app_exit_machine } from '../../features/app-exit/machines/appExitMachine';
import { create_connection_machine } from '../../features/connection-status/machines/connectionMachine';
import { create_csv_export_machine } from '../../features/csv-export/machines/csvExportMachine';
import { create_chart_machine } from '../../features/price-chart/machines/chartMachine';
import { create_recent_orders_machine } from '../../features/recent-orders/machines/recentOrdersMachine';
import { create_regime_machine } from '../../features/regime-selection/machines/regimeMachine';
import { create_split_order_machine } from '../../features/split-order/machines/splitOrderMachine';
import { create_trade_history_machine } from '../../features/trade-history/machines/tradeHistoryMachine';
import { create_trade_history_summary_machine } from '../../features/trade-history/machines/tradeHistorySummaryMachine';
import { create_trading_command_machine } from '../../features/trading-control/machines/tradingCommandMachine';
import type { UiApplicationFacadeOptions } from '../control/uiApplicationContracts';


/**
 * 함수 이름: create_feature_definitions()
 * 기능: 순수 초기 데이터로 기능별 상태·가드·Action 요청 정의를 준비한다.
 * 인자: options -> 외부 함수가 없는 초기 UI 데이터
 * 반환값: 루트 조립에 사용할 기능별 machine 정의
 * 작성 날짜: 2026/09/16
 */
export function create_feature_definitions(options: Omit<UiApplicationFacadeOptions, 'get_current_kst_date'> & { initial_monotonic_ms?: number }) {
    // 명시한 초기값만 각 기능에 주입하고 생략된 옵션은 기능별 기본값을 유지한다.
    const definitions = {
        account_summary: create_account_summary_machine({
            ...(options.account_strategy === undefined ? {} : {
                strategy: options.account_strategy,
            }),
            ...(options.account_asset === undefined ? {} : {
                asset: options.account_asset,
            }),
        }),
        app_exit: create_app_exit_machine(),
        connection: create_connection_machine(),
        csv_export: create_csv_export_machine({
            today: options.today,
            ...(options.csv_default_file_name === undefined ? {} : {
                default_file_name: options.csv_default_file_name,
            }),
        }),
        chart: create_chart_machine({
            // 최초 snapshot에서도 계좌 카드와 차트가 같은 실행 전략으로 시작하도록 seed한다.
            ...(options.account_strategy === undefined ? {} : {
                active_trading_logic_state: options.account_strategy.appliedState,
            }),
            ...(options.chart_interval === undefined ? {} : {
                interval: options.chart_interval,
            }),
            ...(options.chart_indicators === undefined ? {} : {
                indicators: options.chart_indicators,
            }),
        }),
        recent_orders: create_recent_orders_machine({
            initial_monotonic_ms: options.initial_monotonic_ms ?? 0,
            ...(options.recent_trades === undefined ? {} : {
                trades: options.recent_trades,
            }),
            strategy_indicators: options.strategy_indicators ?? null,
        }),
        regime: create_regime_machine({
            ...(options.recommended_regime === undefined ? {} : {
                recommended_regime: options.recommended_regime,
            }),
            ...(options.applied_regime === undefined ? {} : {
                applied_regime: options.applied_regime,
            }),
            ...(options.regime_metrics === undefined ? {} : {
                metrics: options.regime_metrics,
            }),
        }),
        split_order: create_split_order_machine({
            ...(options.scale_in_percentage === undefined ? {} : {
                scale_in_percentage: options.scale_in_percentage,
            }),
            ...(options.scale_out_percentage === undefined ? {} : {
                scale_out_percentage: options.scale_out_percentage,
            }),
        }),
        trade_history: create_trade_history_machine({
            symbol: options.trading_symbol ?? 'ETH/KRW',
            ...(options.history_records === undefined ? {} : {
                records: options.history_records,
            }),
        }),
        trade_history_summary: create_trade_history_summary_machine(options.trade_history_summary === undefined ? {} : {
            summary: options.trade_history_summary,
        }),
        trading: create_trading_command_machine({
            ...(options.logic_coverage === undefined ? {} : {
                logic_coverage: options.logic_coverage,
            }),
            ...(options.command_enabled === undefined ? {} : {
                command_enabled: options.command_enabled,
            }),
            ...(options.risk_policy_availability === undefined ? {} : {
                risk_policy_availability: options.risk_policy_availability,
            }),
            ...(options.configured_risk_policy_version === undefined ? {} : {
                configured_risk_policy_version: options.configured_risk_policy_version,
            }),
            ...(options.max_order_notional === undefined ? {} : {
                max_order_notional: options.max_order_notional,
            }),
            ...(options.max_position_notional === undefined ? {} : {
                max_position_notional: options.max_position_notional,
            }),
            ...(options.max_daily_loss === undefined ? {} : {
                max_daily_loss: options.max_daily_loss,
            }),
            ...(options.daily_loss_scope === undefined ? {} : {
                daily_loss_scope: options.daily_loss_scope,
            }),
            ...(options.manual_kill_behavior === undefined ? {} : {
                manual_kill_behavior: options.manual_kill_behavior,
            }),
            ...(options.session_risk_policy_version === undefined ? {} : {
                session_risk_policy_version: options.session_risk_policy_version,
            }),
            ...(options.risk_control_version === undefined ? {} : {
                risk_control_version: options.risk_control_version,
            }),
            ...(options.manual_kill_active === undefined ? {} : {
                manual_kill_active: options.manual_kill_active,
            }),
            ...(options.manual_kill_cleanup_complete === undefined ? {} : {
                manual_kill_cleanup_complete: options.manual_kill_cleanup_complete,
            }),
            ...(options.manual_kill_activation_behavior === undefined ? {} : {
                manual_kill_activation_behavior: options.manual_kill_activation_behavior,
            }),
            ...(options.manual_kill_activation_policy_version === undefined ? {} : {
                manual_kill_activation_policy_version: options.manual_kill_activation_policy_version,
            }),
            ...(options.last_risk_decision_allowed === undefined ? {} : {
                last_risk_decision_allowed: options.last_risk_decision_allowed,
            }),
            ...(options.last_risk_budget === undefined ? {} : {
                last_risk_budget: options.last_risk_budget,
            }),
            ...(options.risk_block_reason === undefined ? {} : {
                risk_block_reason: options.risk_block_reason,
            }),
            ...(options.process_ownership_ambiguous === undefined ? {} : {
                process_ownership_ambiguous: options.process_ownership_ambiguous,
            }),
            ...(options.lifecycle_status === undefined ? {} : {
                lifecycle_status: options.lifecycle_status,
            }),
            ...(options.is_trading === undefined ? {} : {
                is_trading: options.is_trading,
            }),
            ...(options.has_open_position === undefined ? {} : {
                has_open_position: options.has_open_position,
            }),
            // 초기 backend 평단가를 반올림 없이 매매 상태 정의의 표시 데이터로 전달한다.
            position_average_entry_price: options.position_average_entry_price ?? null,
            residual_quantity: options.residual_quantity ?? '0',
            residual_cost_basis: options.residual_cost_basis ?? '0',
            balance_reconciliation: options.balance_reconciliation ?? null,
        }),
    };

    return definitions;
}
