/* 이 파일은 Python transport schema에서 생성됩니다. 직접 수정하지 마세요. */

export const BACKEND_SCHEMA_VERSION = 3 as const;
export const BACKEND_MAX_TRADE_PAGE_SIZE = 1000 as const;

export type BackendDecimalString = string;
export type BackendRegimeType = 'type0' | 'type1' | 'type2' | 'type3' | 'type4';
export type BackendExecutionMode = 'disabled' | 'fake' | 'testnet' | 'live';
export type BackendTradingStatus =
    | 'not_started'
    | 'running'
    | 'stopping'
    | 'reconciliation_required'
    | 'terminated';
export type BackendTradingLogicSupportStatus = 'supported' | 'unsupported';
export type BackendTradingLogicStartGuard =
    | 'READY'
    | 'UNSUPPORTED_TRADING_LOGIC';
export type BackendTradeSide = 'BUY' | 'SELL';
export type BackendHistoryPeriod = 'today' | 'last7days' | 'last30days' | 'all';
export type BackendTradeSideFilter = 'all' | 'buy' | 'sell';
export type BackendCsvPeriod = 'today' | 'last7days' | 'last30days' | 'custom';
export type BackendStrategyType = 'CASE_B' | 'CASE_C';
export type BackendRiskPolicyAvailability = 'CONFIGURED' | 'UNAVAILABLE';
export type BackendDailyLossScope = 'REALIZED_ONLY' | 'REALIZED_AND_UNREALIZED';
export type BackendManualKillBehavior = 'BLOCK_NEW_ORDERS' | 'CANCEL_AND_LIQUIDATE';
export type BackendRiskBlockReason =
    | 'RISK_POLICY_UNAVAILABLE'
    | 'RISK_POLICY_VERSION_MISMATCH'
    | 'MANUAL_KILL_SWITCH_ACTIVE'
    | 'RISK_ORDER_NOTIONAL_EXCEEDED'
    | 'RISK_DAILY_LOSS_EXCEEDED'
    | 'RISK_POSITION_NOTIONAL_EXCEEDED';

export interface BackendConnectionSnapshot {
    readonly status: 'online';
    readonly ready: boolean;
    readonly schema_version: typeof BACKEND_SCHEMA_VERSION;
}

export interface BackendBinanceConnectionStatus {
    readonly api: 'online' | 'offline';
    readonly market_stream: 'online' | 'offline';
    readonly account_stream: 'online' | 'offline';
}

export interface BackendShutdownState {
    readonly session_id: string;
    readonly version: number;
    readonly status: BackendTradingStatus;
}

export interface BackendMarketSnapshot {
    readonly symbol: string;
    readonly current_price: BackendDecimalString | null;
    readonly version: number;
    readonly updated_at: string | null;
}

export interface BackendSwingSnapshot {
    readonly highs: ReadonlyArray<BackendDecimalString>;
    readonly lows: ReadonlyArray<BackendDecimalString>;
    readonly has_higher_high: boolean;
    readonly has_higher_low: boolean;
    readonly has_lower_high: boolean;
    readonly has_lower_low: boolean;
}

export interface BackendIndicatorSnapshot {
    readonly symbol: string;
    readonly timeframe: '4h';
    readonly ema9_series: ReadonlyArray<BackendDecimalString>;
    readonly ema9_slope: BackendDecimalString;
    readonly live_ema9: BackendDecimalString;
    readonly current_price: BackendDecimalString;
    readonly source_market_version: number;
    readonly source_candle_id: string;
    readonly calculated_at: string;
    readonly swing: BackendSwingSnapshot;
}

export interface BackendRegimeSnapshot {
    readonly indicator: BackendIndicatorSnapshot | null;
    readonly recommended: BackendRegimeType | null;
    readonly selected: BackendRegimeType | null;
}

export interface BackendTradingLogicCoverage {
    readonly regime_type: BackendRegimeType;
    readonly support_status: BackendTradingLogicSupportStatus;
    readonly start_guard: BackendTradingLogicStartGuard;
}

export interface BackendRiskBudgetSnapshot {
    readonly policy_version: number | null;
    readonly market_version: number;
    readonly account_version: number;
    readonly context_version: number;
    readonly current_position_notional: BackendDecimalString;
    readonly reserved_buy_notional: BackendDecimalString;
    readonly candidate_order_notional: BackendDecimalString;
    readonly projected_position_notional: BackendDecimalString;
    readonly daily_realized_pnl: BackendDecimalString;
    readonly unrealized_pnl: BackendDecimalString;
    readonly daily_loss: BackendDecimalString;
    readonly manual_kill_active: boolean;
}

export interface BackendTradingSnapshot {
    readonly mode: BackendExecutionMode;
    readonly status: BackendTradingStatus;
    readonly version: number;
    readonly command_enabled: boolean;
    readonly scale_in: BackendDecimalString;
    readonly scale_out: BackendDecimalString;
    readonly has_open_position: boolean;
    /** 구버전 schema v3에서 생략될 수 있는 열린 포지션의 표시용 평단가다. */
    readonly position_average_entry_price?: BackendDecimalString | null;
    readonly session_id: string | null;
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
    readonly logic_coverage: ReadonlyArray<BackendTradingLogicCoverage>;
}

export interface BackendBalanceSnapshot {
    readonly asset: string;
    readonly free: BackendDecimalString;
    readonly locked: BackendDecimalString;
    readonly total: BackendDecimalString;
}

export interface BackendAccountSnapshot {
    readonly valuation_asset: string;
    readonly quote_asset: 'USDT';
    readonly current_price: BackendDecimalString | null;
    readonly valuation: BackendDecimalString | null;
    readonly version: number;
    readonly updated_at: string | null;
    readonly balances: ReadonlyArray<BackendBalanceSnapshot>;
}

export interface BackendTradeSnapshot {
    readonly trade_id: string;
    readonly order_id: string;
    readonly client_order_id: string;
    readonly symbol: string;
    readonly executed_at: string;
    readonly side: BackendTradeSide;
    readonly regime_type: BackendRegimeType;
    readonly strategy: BackendStrategyType;
    readonly requested_quantity: BackendDecimalString;
    readonly executed_quantity: BackendDecimalString;
    readonly executed_amount: BackendDecimalString;
    readonly average_fill_price: BackendDecimalString;
    readonly market_price_at_decision: BackendDecimalString;
    readonly fee_amount: BackendDecimalString;
    readonly fee_asset: string;
    readonly fee_quote_amount: BackendDecimalString;
    readonly allocated_cost_basis: BackendDecimalString | null;
    readonly realized_pnl: BackendDecimalString | null;
    readonly realized_return_rate: BackendDecimalString | null;
    readonly exit_reason: string | null;
}

export interface BackendPerformanceSnapshot {
    readonly daily_return_rate: BackendDecimalString;
    readonly cumulative_return_rate: BackendDecimalString;
    readonly realized_pnl: BackendDecimalString;
    readonly daily_fee: BackendDecimalString;
    readonly total_fee: BackendDecimalString;
    readonly average_sell_return_rate: BackendDecimalString;
    readonly total_profit: BackendDecimalString;
    readonly winning_sell_count: number;
    readonly losing_sell_count: number;
    readonly breakeven_sell_count: number;
    readonly completed_sell_count: number;
    readonly win_rate: BackendDecimalString | null;
}

export interface BackendTradeDetailsQuery {
    readonly period: BackendHistoryPeriod;
    readonly side: BackendTradeSideFilter;
    readonly start_date: string;
    readonly end_date: string;
}

export interface BackendTradeDetailsSummary {
    readonly holdings_asset: 'ETH';
    readonly holdings: BackendDecimalString;
    readonly account_version: number;
    readonly performance: BackendPerformanceSnapshot;
}

export interface BackendTradeDetails {
    readonly query: BackendTradeDetailsQuery;
    readonly rows: ReadonlyArray<BackendTradeSnapshot>;
    readonly row_count: number;
    readonly summary: BackendTradeDetailsSummary;
}

export interface BackendCsvExportRequest {
    readonly schema_version: typeof BACKEND_SCHEMA_VERSION;
    readonly directory: string;
    readonly file_name: string;
    readonly period: BackendCsvPeriod;
    readonly start_date: string;
    readonly end_date: string;
    readonly timezone: 'Asia/Seoul';
}

export interface BackendCsvExportReceipt {
    readonly file_path: string;
    readonly exported_row_count: number;
}

export interface BackendRuntimeEnvironment {
    readonly market_data: 'mainnet' | 'testnet' | 'fake' | 'unavailable';
    readonly account: 'mainnet' | 'testnet' | 'fake' | 'unavailable';
    readonly orders_enabled: boolean;
}

export interface BackendSnapshot {
    readonly environment?: BackendRuntimeEnvironment;
    readonly session_id: string;
    readonly last_sequence: number;
    readonly connection: BackendConnectionSnapshot;
    readonly market: BackendMarketSnapshot;
    readonly regime: BackendRegimeSnapshot;
    readonly trading: BackendTradingSnapshot;
    readonly account: BackendAccountSnapshot;
    readonly recent_trades: ReadonlyArray<BackendTradeSnapshot>;
    readonly performance: BackendPerformanceSnapshot;
}

export interface BackendHealth {
    readonly process: 'running';
    readonly session_id: string;
    readonly schema_version: typeof BACKEND_SCHEMA_VERSION;
    readonly ready: boolean;
    readonly state: string;
}

export interface BackendSuccessEnvelope<TData> {
    readonly schema_version: typeof BACKEND_SCHEMA_VERSION;
    readonly request_id: string;
    readonly ok: true;
    readonly data: TData;
}

export interface BackendFailureDetails {
    readonly [key: string]: unknown;
}

export interface BackendFailureEnvelope {
    readonly schema_version: typeof BACKEND_SCHEMA_VERSION;
    readonly request_id: string;
    readonly ok: false;
    readonly error: {
        readonly code: string;
        readonly message: string;
        readonly retryable: boolean;
        readonly details: BackendFailureDetails;
    };
}

export type BackendHttpEnvelope<TData> =
    | BackendSuccessEnvelope<TData>
    | BackendFailureEnvelope;

export interface BackendAuthenticateMessage {
    readonly schema_version: typeof BACKEND_SCHEMA_VERSION;
    readonly type: 'AUTHENTICATE';
    readonly token: string;
    readonly after_sequence: number;
}

export interface BackendEventEnvelope<TPayload = Readonly<Record<string, unknown>>> {
    readonly schema_version: typeof BACKEND_SCHEMA_VERSION;
    readonly session_id: string;
    readonly event_id: string;
    readonly sequence: number;
    readonly occurred_at: string;
    readonly type: string;
    readonly aggregate_version: number | null;
    readonly correlation_id: string | null;
    readonly payload: TPayload;
}

export interface BackendAccountUpdatedPayload {
    readonly account: BackendAccountSnapshot;
}

export interface BackendOrderExecutedPayload {
    readonly trade: BackendTradeSnapshot;
}

export interface BackendPerformanceUpdatedPayload {
    readonly performance: BackendPerformanceSnapshot;
}

export type BackendResyncReason = 'REPLAY_GAP' | 'SEQUENCE_AHEAD';

export interface BackendResyncRequired {
    readonly schema_version: typeof BACKEND_SCHEMA_VERSION;
    readonly session_id: string;
    readonly type: 'RESYNC_REQUIRED';
    readonly reason: BackendResyncReason;
    readonly last_sequence: number;
}
