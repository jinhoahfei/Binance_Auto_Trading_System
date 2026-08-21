/* 이 파일은 Python transport schema에서 생성됩니다. 직접 수정하지 마세요. */

export const BACKEND_SCHEMA_VERSION = 2 as const;

export type BackendDecimalString = string;
export type BackendRegimeType = 'type0' | 'type1' | 'type2' | 'type3' | 'type4';
export type BackendExecutionMode = 'disabled' | 'fake' | 'testnet' | 'live';
export type BackendTradingStatus = 'not_started';
export type BackendTradingLogicSupportStatus = 'supported' | 'unsupported';
export type BackendTradingLogicStartGuard =
    | 'READY'
    | 'UNSUPPORTED_TRADING_LOGIC';
export type BackendTradeSide = 'BUY' | 'SELL';
export type BackendStrategyType = 'CASE_B' | 'CASE_C';

export interface BackendConnectionSnapshot {
    readonly status: 'online';
    readonly ready: boolean;
    readonly schema_version: typeof BACKEND_SCHEMA_VERSION;
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

export interface BackendTradingSnapshot {
    readonly mode: BackendExecutionMode;
    readonly status: BackendTradingStatus;
    readonly version: number;
    readonly command_enabled: false;
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

export interface BackendSnapshot {
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

export type BackendResyncReason = 'REPLAY_GAP' | 'SEQUENCE_AHEAD';

export interface BackendResyncRequired {
    readonly schema_version: typeof BACKEND_SCHEMA_VERSION;
    readonly session_id: string;
    readonly type: 'RESYNC_REQUIRED';
    readonly reason: BackendResyncReason;
    readonly last_sequence: number;
}
