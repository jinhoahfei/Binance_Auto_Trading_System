// UI 제어 계층과 외부 호출자가 주고받는 데이터의 형태를 정의한다.
/**
 * 파일 역할: UI 제어 계층과 외부 호출자가 주고받는 데이터의 형태를 정의한다.
 * Facade 생성 옵션, 서버 snapshot, 사용자 입력·서버 통지 intent, 화면 모델을 모은다.
 * 실제 이벤트 처리와 화면 모델 계산은 Controller·입력 router·selector에서 수행한다.
 */

import type { BackendBalanceReconciliation } from '../../shared/contracts';
import type { BackendConnectionStatus } from '../../shared/api/BackendUiAdapter';
import type { UiModalKind } from './uiModalTypes';
import type {
    AssetSummaryViewModel,
    StrategySummaryViewModel,
} from '../../features/account-summary';
import type { TradeHistorySummaryViewModel } from '../../features/trade-history';
import type { TradingUnavailableReason } from '../../features/trading-control';
import type {
    BackendDailyLossScope,
    BackendDecimalString,
    BackendManualKillBehavior,
    BackendTradingStatus,
    BackendTradingIndicatorSnapshot,
    BackendRiskBudgetSnapshot,
    BackendRiskBlockReason,
    BackendRiskPolicyAvailability,
    ChartDrawing,
    ChartInterval,
    CsvPeriod,
    HistoryPeriod,
    LocalDateString,
    RegimeMetric,
    RegimeType,
    TradingLogicCoverage,
    TradingLogicSupportStatus,
    TradeRecord,
    TradeSideFilter,
    UiCommandFailure,
} from '../../shared/contracts';

/** UI 루트를 생성할 때 전달하는 기준 날짜, 초기 표시 데이터와 매매·화면 설정이다. */
export interface UiApplicationFacadeOptions {
    readonly today: LocalDateString;
    readonly trading_symbol?: string;
    readonly csv_default_file_name?: string;
    // 생략하면 CSV 상태 정의는 today를 고정 날짜 source로 사용한다.
    readonly get_current_kst_date?: () => LocalDateString;
    readonly chart_interval?: ChartInterval;
    readonly chart_indicators?: {
        readonly bollinger_bands?: boolean;
        readonly ema9?: boolean;
        readonly volume?: boolean;
    };
    readonly recommended_regime?: RegimeType | null;
    readonly applied_regime?: RegimeType | null;
    readonly regime_metrics?: ReadonlyArray<RegimeMetric>;
    readonly strategy_indicators?: BackendTradingIndicatorSnapshot | null;
    readonly logic_coverage?: ReadonlyArray<TradingLogicCoverage>;
    readonly command_enabled?: boolean;
    readonly risk_policy_availability?: BackendRiskPolicyAvailability;
    readonly configured_risk_policy_version?: number | null;
    readonly max_order_notional?: BackendDecimalString | null;
    readonly max_position_notional?: BackendDecimalString | null;
    readonly max_daily_loss?: BackendDecimalString | null;
    readonly daily_loss_scope?: BackendDailyLossScope | null;
    readonly manual_kill_behavior?: BackendManualKillBehavior | null;
    readonly session_risk_policy_version?: number | null;
    readonly risk_control_version?: number;
    readonly manual_kill_active?: boolean;
    readonly manual_kill_cleanup_complete?: boolean;
    readonly manual_kill_activation_behavior?: BackendManualKillBehavior | null;
    readonly manual_kill_activation_policy_version?: number | null;
    readonly last_risk_decision_allowed?: boolean | null;
    readonly last_risk_budget?: BackendRiskBudgetSnapshot | null;
    readonly risk_block_reason?: BackendRiskBlockReason | null;
    readonly process_ownership_ambiguous?: boolean;
    readonly recent_trades?: ReadonlyArray<TradeRecord>;
    // Demo/Storybook/test fixture만 상세 행을 seed하며 live mapper는 이 값을 전달하지 않는다.
    readonly history_records?: ReadonlyArray<TradeRecord>;
    readonly scale_in_percentage?: number;
    readonly scale_out_percentage?: number;
    readonly account_strategy?: StrategySummaryViewModel;
    readonly account_asset?: AssetSummaryViewModel;
    readonly trade_history_summary?: TradeHistorySummaryViewModel;
    readonly lifecycle_status?: BackendTradingStatus;
    readonly is_trading?: boolean;
    readonly has_open_position?: boolean;
    readonly position_average_entry_price?: BackendDecimalString | null;
    readonly residual_quantity?: BackendDecimalString;
    readonly residual_cost_basis?: BackendDecimalString;
    readonly balance_reconciliation?: BackendBalanceReconciliation | null;
}

/**
 * backend의 한 coherent snapshot에서 UI 루트가 소유할 server state만 추출한 계약이다.
 */
export interface UiServerOwnedSnapshot {
    readonly last_sequence: number;
    readonly trading_version: number;
    readonly trading_session_id: string | null;
    readonly trading_symbol: string;
    readonly recommended_regime: RegimeType | null;
    readonly applied_regime: RegimeType | null;
    readonly regime_metrics: ReadonlyArray<RegimeMetric>;
    readonly strategy_indicators?: BackendTradingIndicatorSnapshot | null;
    readonly logic_coverage: ReadonlyArray<TradingLogicCoverage>;
    readonly command_enabled: boolean;
    readonly risk_policy_availability: BackendRiskPolicyAvailability;
    readonly configured_risk_policy_version: number | null;
    readonly max_order_notional: BackendDecimalString | null;
    readonly max_position_notional: BackendDecimalString | null;
    readonly max_daily_loss: BackendDecimalString | null;
    readonly daily_loss_scope: BackendDailyLossScope | null;
    readonly manual_kill_behavior: BackendManualKillBehavior | null;
    readonly session_risk_policy_version: number | null;
    readonly risk_control_version: number;
    readonly manual_kill_active: boolean;
    readonly manual_kill_cleanup_complete: boolean;
    readonly manual_kill_activation_behavior: BackendManualKillBehavior | null;
    readonly manual_kill_activation_policy_version: number | null;
    readonly last_risk_decision_allowed: boolean | null;
    readonly last_risk_budget: BackendRiskBudgetSnapshot | null;
    readonly risk_block_reason: BackendRiskBlockReason | null;
    readonly process_ownership_ambiguous: boolean;
    readonly recent_trades: ReadonlyArray<TradeRecord>;
    readonly scale_in_percentage: number;
    readonly scale_out_percentage: number;
    readonly account_strategy: StrategySummaryViewModel;
    readonly account_asset: AssetSummaryViewModel;
    readonly trade_history_summary: TradeHistorySummaryViewModel;
    readonly is_trading: boolean;
    readonly has_open_position: boolean;
    readonly position_average_entry_price?: BackendDecimalString | null;
    readonly residual_quantity?: BackendDecimalString;
    readonly residual_cost_basis?: BackendDecimalString;
    readonly balance_reconciliation?: BackendBalanceReconciliation | null;
    readonly trading_state_label: BackendTradingStatus;
}

/**
 * React Boundary가 facade에 전달할 수 있는 사용자 의도와 backend event의 통합 계약이다.
 */
export type UiApplicationIntent = {
    readonly type: 'BACKEND_SNAPSHOT_SYNCHRONIZED';
    readonly snapshot: UiServerOwnedSnapshot;
} | {
    readonly type: 'CONNECT_REQUESTED';
} | {
    readonly type: 'RECONNECT_REQUESTED';
} | {
    readonly type: 'API_CONNECTED';
    readonly sequence?: number;
} | {
    readonly type: 'API_DISCONNECTED';
    readonly reason?: string;
} | {
    readonly type: 'UI_CONNECTION_DISCONNECTED';
    readonly reason?: string;
} | {
    readonly type: 'RECONNECT_FAILED';
    readonly reason: string;
} | {
    readonly type: 'BACKEND_CONNECTION_STATUS';
    readonly status: BackendConnectionStatus;
} | {
    readonly type: 'START_TRADING_CLICKED';
} | {
    readonly type: 'START_TRADING_CONFIRMED';
} | {
    readonly type: 'START_TRADING_CANCELED';
} | {
    readonly type: 'SELECT_REGIME_NOTICE_CONFIRMED';
} | {
    readonly type: 'SELECT_REGIME_NOTICE_CLOSED';
} | {
    readonly type: 'API_CONNECTION_NOTICE_CONFIRMED';
} | {
    readonly type: 'TRADING_UNAVAILABLE_NOTICE_CONFIRMED';
} | {
    readonly type: 'STOP_TRADING_CLICKED';
    readonly has_open_position: boolean;
} | {
    readonly type: 'STOP_TRADING_CONFIRMED';
} | {
    readonly type: 'STOP_TRADING_CANCELED';
} | {
    readonly type: 'FORCE_SELL_AND_STOP_CONFIRMED';
} | {
    readonly type: 'FORCE_SELL_AND_STOP_CANCELED';
} | {
    readonly type: 'RECOVERED_POSITION_LIQUIDATION_CONFIRMED';
} | {
    readonly type: 'RECOVERED_POSITION_LIQUIDATION_CANCELED';
} | {
    readonly type: 'BACKEND_TRADING_STARTED';
} | {
    readonly type: 'BACKEND_TRADING_STOPPED';
} | {
    readonly type: 'POSITION_UPDATED';
    readonly has_open_position: boolean;
} | {
    readonly type: 'TRADING_SESSION_SYNCHRONIZED';
    readonly strategy_indicators?: BackendTradingIndicatorSnapshot | null;
    readonly status: BackendTradingStatus;
    readonly version: number;
    readonly session_id: string | null;
    readonly command_enabled: boolean;
    readonly risk_policy_availability: BackendRiskPolicyAvailability;
    readonly configured_risk_policy_version: number | null;
    readonly max_order_notional: BackendDecimalString | null;
    readonly max_position_notional: BackendDecimalString | null;
    readonly max_daily_loss: BackendDecimalString | null;
    readonly daily_loss_scope: BackendDailyLossScope | null;
    readonly manual_kill_behavior: BackendManualKillBehavior | null;
    readonly session_risk_policy_version: number | null;
    readonly risk_control_version: number;
    readonly manual_kill_active: boolean;
    readonly manual_kill_cleanup_complete: boolean;
    readonly manual_kill_activation_behavior: BackendManualKillBehavior | null;
    readonly manual_kill_activation_policy_version: number | null;
    readonly last_risk_decision_allowed: boolean | null;
    readonly last_risk_budget: BackendRiskBudgetSnapshot | null;
    readonly risk_block_reason: BackendRiskBlockReason | null;
    readonly process_ownership_ambiguous: boolean;
    readonly scale_in: string;
    readonly scale_out: string;
    readonly scale_in_percentage: number;
    readonly scale_out_percentage: number;
    readonly has_open_position: boolean;
    readonly position_average_entry_price?: BackendDecimalString | null;
    readonly residual_quantity?: BackendDecimalString;
    readonly residual_cost_basis?: BackendDecimalString;
    readonly balance_reconciliation?: BackendBalanceReconciliation | null;
    readonly logic_coverage: ReadonlyArray<TradingLogicCoverage>;
    readonly strategy_status: string;
    readonly strategy_status_tone: 'positive' | 'neutral';
    readonly strategy_state_label?: string;  // Backend mapper가 두 표시 영역에 전달할 단일 문구다.
} | {
    readonly type: 'ACCOUNT_STRATEGY_UPDATED';
    readonly strategy: StrategySummaryViewModel;
} | {
    readonly type: 'ACCOUNT_ASSETS_UPDATED';
    readonly asset: AssetSummaryViewModel;
} | {
    readonly type: 'REGIME_TYPE_CLICKED';
    readonly regime: RegimeType;
} | {
    readonly type: 'REGIME_CHANGE_CONFIRMED';
} | {
    readonly type: 'REGIME_CHANGE_CANCELED';
} | {
    readonly type: 'REGIME_RECOMMENDED';
    readonly regime: RegimeType;
} | {
    readonly type: 'REGIME_APPLIED';
    readonly regime: RegimeType;
} | {
    readonly type: 'REGIME_SELECTION_SYNCHRONIZED';
    readonly selected: RegimeType;
    readonly support_status: TradingLogicSupportStatus;
    readonly version: number;
} | {
    readonly type: 'REGIME_INDICATORS_UPDATED';
    readonly metrics: ReadonlyArray<RegimeMetric>;
} | {
    readonly type: 'REGIME_HIGHLIGHT_COMPLETED';
} | {
    readonly type: 'CHART_INTERVAL_SELECTED';
    readonly interval: ChartInterval;
} | {
    readonly type: 'CHART_INDICATOR_SETTINGS_TOGGLED';
} | {
    readonly type: 'CHART_INDICATOR_SETTINGS_OUTSIDE_CLICKED';
} | {
    readonly type: 'CHART_INDICATOR_CHANGED';
    readonly indicator: 'bollinger_bands' | 'ema9' | 'volume';
    readonly is_visible: boolean;
} | {
    readonly type: 'CHART_FULLSCREEN_CHANGED';
    readonly is_fullscreen: boolean;
} | {
    readonly type: 'CHART_DRAWING_TOOL_CLICKED';
} | {
    readonly type: 'CHART_DRAWING_STARTED';
} | {
    readonly type: 'CHART_DRAWING_FINISHED';
    readonly drawing: ChartDrawing;
} | {
    readonly type: 'CHART_DRAWING_CANCELED';
} | {
    readonly type: 'CHART_ACTIVE_STATE_UPDATED';
    readonly state_label: string;
} | {
    readonly type: 'CHART_LINE_HOVER_ENTERED';
    readonly line_id: string;
} | {
    readonly type: 'CHART_LINE_HOVER_EXITED';
} | {
    readonly type: 'CHART_LINE_CONTEXT_MENU_REQUESTED';
    readonly x?: number;
    readonly y?: number;
} | {
    readonly type: 'CHART_LINE_DELETE_REQUESTED';
} | {
    readonly type: 'CHART_LINE_CONTEXT_MENU_OUTSIDE_CLICKED';
} | {
    readonly type: 'RECENT_ORDERS_TAB_SELECTED';
} | {
    readonly type: 'REALTIME_INDICATORS_TAB_SELECTED';
} | {
    readonly type: 'REALTIME_INDICATORS_UPDATED';
    readonly indicators: ReadonlyArray<RegimeMetric>;
} | {
    readonly type: 'BUY_ORDER_EXECUTED';
    readonly trade: TradeRecord;
} | {
    readonly type: 'SELL_ORDER_EXECUTED';
    readonly trade: TradeRecord;
} | {
    readonly type: 'SCALE_IN_CHANGED';
    readonly percentage: number;
} | {
    readonly type: 'SCALE_OUT_CHANGED';
    readonly percentage: number;
} | {
    readonly type: 'SHOW_TRADE_HISTORY';
} | {
    readonly type: 'BACK_TO_DASHBOARD';
} | {
    readonly type: 'REFRESH_TRADE_HISTORY';
} | {
    readonly type: 'HISTORY_PERIOD_SELECTED';
    readonly period: HistoryPeriod;
} | {
    readonly type: 'HISTORY_SIDE_SELECTED';
    readonly side: TradeSideFilter;
} | {
    readonly type: 'TRADE_HISTORY_PROFIT_RATE_UPDATED';
    readonly daily_return: TradeHistorySummaryViewModel['dailyReturn'];
} | {
    readonly type: 'TRADE_HISTORY_SELL_SUMMARY_UPDATED';
    readonly sell_performance: TradeHistorySummaryViewModel['sellPerformance'];
    readonly position: TradeHistorySummaryViewModel['position'];
} | {
    readonly type: 'TRADE_HISTORY_BUY_HOLDINGS_UPDATED';
    readonly position: TradeHistorySummaryViewModel['position'];
} | {
    readonly type: 'TRADE_HISTORY_HOLDINGS_UPDATED';
    readonly position: TradeHistorySummaryViewModel['position'];
} | {
    readonly type: 'TRADE_HISTORY_DAILY_FEE_UPDATED';
    readonly fees: TradeHistorySummaryViewModel['fees'];
} | {
    readonly type: 'TRADE_HISTORY_PERFORMANCE_UPDATED';
    readonly daily_return: TradeHistorySummaryViewModel['dailyReturn'];
    readonly sell_performance: TradeHistorySummaryViewModel['sellPerformance'];
    readonly fees: TradeHistorySummaryViewModel['fees'];
} | {
    readonly type: 'OPEN_CSV_EXPORT';
} | {
    readonly type: 'CLOSE_CSV_EXPORT';
} | {
    readonly type: 'CSV_DIALOG_OUTSIDE_CLICKED';
} | {
    readonly type: 'CSV_DIRECTORY_SELECT_CLICKED';
} | {
    readonly type: 'CSV_PERIOD_SELECTED';
    readonly period: CsvPeriod;
} | {
    readonly type: 'CSV_START_CALENDAR_OPENED';
} | {
    readonly type: 'CSV_END_CALENDAR_OPENED';
} | {
    readonly type: 'CSV_CALENDAR_OUTSIDE_CLICKED';
} | {
    readonly type: 'CSV_START_DATE_SELECTED';
    readonly date: LocalDateString;
} | {
    readonly type: 'CSV_END_DATE_SELECTED';
    readonly date: LocalDateString;
} | {
    readonly type: 'CSV_FILE_NAME_EDIT_STARTED';
} | {
    readonly type: 'CSV_FILE_NAME_CHANGED';
    readonly file_name: string;
} | {
    readonly type: 'CSV_FILE_NAME_COMMITTED';
} | {
    readonly type: 'CSV_EXPORT_SUBMITTED';
} | {
    readonly type: 'CSV_EXPORT_ERROR_CONFIRMED';
} | {
    readonly type: 'CSV_EXPORT_COMPLETE_CONFIRMED';
} | {
    readonly type: 'APP_EXIT_CLICKED';
} | {
    readonly type: 'APP_EXIT_CONFIRMED';
} | {
    readonly type: 'APP_EXIT_CANCELED';
} | {
    readonly type: 'FORCE_SELL_EXIT_CONFIRMED';
} | {
    readonly type: 'FORCE_SELL_EXIT_CANCELED';
} | {
    readonly type: 'BACKEND_SIDECAR_EXITED_NORMALLY';
} | {
    readonly type: 'BACKEND_SIDECAR_EXITED_ABNORMALLY';
};

/**
 * React 컴포넌트가 업무 분기 없이 바로 렌더링할 수 있는 애플리케이션 화면 모델이다.
 */
export interface AppViewModel {
    readonly route: 'dashboard' | 'trade_history';
    readonly active_modal: UiModalKind | null;
    readonly connection: {
        readonly status: 'offline' | 'connecting' | 'online' | 'reconnecting';
        readonly is_online: boolean;
        readonly is_pending: boolean;
        readonly reconnect_attempt: number;
        readonly recovery?: BackendConnectionStatus | null;
        readonly error: string | null;
    };
    readonly trading: {
        readonly command_enabled: boolean;
        readonly risk_policy_availability: BackendRiskPolicyAvailability | undefined;
        readonly configured_risk_policy_version: number | null | undefined;
        readonly max_order_notional: BackendDecimalString | null | undefined;
        readonly max_position_notional: BackendDecimalString | null | undefined;
        readonly max_daily_loss: BackendDecimalString | null | undefined;
        readonly daily_loss_scope: BackendDailyLossScope | null | undefined;
        readonly manual_kill_behavior: BackendManualKillBehavior | null | undefined;
        readonly session_risk_policy_version: number | null | undefined;
        readonly risk_control_version: number | undefined;
        readonly manual_kill_active: boolean | undefined;
        readonly manual_kill_cleanup_complete: boolean | undefined;
        readonly manual_kill_activation_behavior: BackendManualKillBehavior | null | undefined;
        readonly manual_kill_activation_policy_version: number | null | undefined;
        readonly last_risk_decision_allowed: boolean | null | undefined;
        readonly last_risk_budget: BackendRiskBudgetSnapshot | null | undefined;
        readonly risk_block_reason: BackendRiskBlockReason | null | undefined;
        readonly process_ownership_ambiguous: boolean | undefined;
        readonly lifecycle_status: BackendTradingStatus;
        readonly is_trading: boolean;
        readonly is_pending: boolean;
        readonly is_recovery_liquidation: boolean;
        readonly has_open_position: boolean;
        readonly position_average_entry_price: BackendDecimalString | null;
        readonly residual_quantity?: BackendDecimalString;
        readonly residual_cost_basis?: BackendDecimalString;
        readonly balance_reconciliation?: BackendBalanceReconciliation | null;
        readonly unavailable_reason: TradingUnavailableReason | null;
        readonly error: UiCommandFailure | null;
    };
    readonly regime: {
        readonly recommended: RegimeType | null;
        readonly applied: RegimeType | null;
        readonly candidate: RegimeType | null;
        readonly is_highlighted: boolean;
        readonly is_pending: boolean;
        readonly metrics: ReadonlyArray<RegimeMetric>;
        readonly logic_coverage: ReadonlyArray<TradingLogicCoverage>;
        readonly error: UiCommandFailure | null;
    };
    readonly chart: {
        readonly interval: ChartInterval;
        readonly indicators: {
            readonly bollinger_bands: boolean;
            readonly ema9: boolean;
            readonly volume: boolean;
        };
        readonly is_indicator_settings_open: boolean;
        readonly is_fullscreen: boolean;
        readonly drawing_mode: 'deactivated' | 'waiting' | 'drawing';
        readonly active_trading_logic_state: string;
        readonly selected_line_id: string | null;
        readonly line_selection_state: 'awaiting_selection' | 'highlighted' | 'context_menu';
        readonly context_menu_position: {
            readonly x: number;
            readonly y: number;
        } | null;
        readonly drawings: ReadonlyArray<ChartDrawing>;
    };
    readonly trader_panel: {
        readonly active_tab: 'recent_orders' | 'realtime_indicators';
        readonly trades: ReadonlyArray<TradeRecord>;
        readonly realtime_indicators: ReadonlyArray<RegimeMetric>;
        readonly strategy_indicators: BackendTradingIndicatorSnapshot | null;
        readonly strategy_indicators_received_at: number | null;
    };
    readonly split_order: {
        readonly scale_in_percentage: number;
        readonly scale_out_percentage: number;
        readonly is_pending: boolean;
        readonly error: UiCommandFailure | null;
    };
    readonly account_summary: {
        readonly strategy: StrategySummaryViewModel;
        readonly asset: AssetSummaryViewModel;
    };
    readonly trade_history: {
        readonly symbol: string;
        readonly period: HistoryPeriod;
        readonly side: TradeSideFilter;
        readonly status: 'idle' | 'loading' | 'ready' | 'empty' | 'failed';
        readonly is_loading: boolean;
        readonly records: ReadonlyArray<TradeRecord>;
        readonly error: UiCommandFailure | null;
        readonly summary: TradeHistorySummaryViewModel;
    };
    readonly csv_export: {
        readonly status: 'closed' | 'editing' | 'picking_directory' | 'exporting' | 'complete' | 'error';
        readonly is_open: boolean;
        readonly is_pending: boolean;
        readonly directory: string | null;
        readonly period: CsvPeriod;
        readonly start_date: LocalDateString | null;
        readonly end_date: LocalDateString | null;
        readonly file_name: string;
        readonly file_name_draft: string;
        readonly calendar_target: 'start_date' | 'end_date' | null;
        readonly validation_errors: {
            readonly directory: string | null;
            readonly file_name: string | null;
            readonly date_range: string | null;
        };
        readonly command_error: UiCommandFailure | null;
        readonly receipt_path: string | null;
    };
    readonly app_exit: {
        readonly status:
            | 'awaiting_exit'
            | 'force_sell_exit_confirmation'
            | 'exit_confirmation'
            | 'shutting_down'
            | 'shutdown_exit_recovery'
            | 'shutdown_outcome_recovery'
            | 'sidecar_exit_failure'
            | 'ui_final_state';
        readonly is_pending: boolean;
        readonly is_final: boolean;
        readonly error: UiCommandFailure | null;
    };
}
