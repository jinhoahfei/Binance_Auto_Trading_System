import type {
    BackendAccountSnapshot,
    BackendEventEnvelope,
    BackendSnapshot,
} from '../contracts';
import { BACKEND_SCHEMA_VERSION } from '../contracts';

export const TEST_BACKEND_SESSION_ID = '3c73d583-c1c8-4830-8393-cc31639a40fd';
export const TEST_BACKEND_EVENT_ID = 'c1394f5d-f728-47c0-8e0b-e8ef44bd96e8';
export const TEST_BACKEND_TOKEN = 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA';

/**
 * 함수 이름: create_backend_account_fixture()
 * 기능: UI transport test에서 사용하는 ready ETH/USDT account DTO를 생성한다.
 * 인자: 없음
 * 반환값: Decimal string과 UTC field가 채워진 backend account
 * 작성 날짜: 2026/08/21
 */
export function create_backend_account_fixture(): BackendAccountSnapshot {
    return {
        valuation_asset: 'ETH',
        quote_asset: 'USDT',
        current_price: '4321.5000',
        valuation: '7562.625000',
        version: 3,
        updated_at: '2026-08-21T02:03:04.567890Z',
        balances: [
            {
                asset: 'ETH',
                free: '1.25',
                locked: '0.50',
                total: '1.75',
            },
            {
                asset: 'USDT',
                free: '120.00',
                locked: '0',
                total: '120.00',
            },
        ],
    };
}

/**
 * 함수 이름: create_backend_snapshot_fixture()
 * 기능: generated contract와 startup ready invariant를 모두 만족하는 coherent snapshot을 생성한다.
 * 인자: 없음
 * 반환값: UI adapter test용 backend snapshot
 * 작성 날짜: 2026/08/21
 */
export function create_backend_snapshot_fixture(): BackendSnapshot {
    return {
        session_id: TEST_BACKEND_SESSION_ID,
        last_sequence: 9,
        connection: {
            status: 'online',
            ready: true,
            schema_version: BACKEND_SCHEMA_VERSION,
        },
        market: {
            symbol: 'ETHUSDT',
            current_price: '4321.5000',
            version: 7,
            updated_at: '2026-08-21T02:03:04.567890Z',
        },
        regime: {
            indicator: {
                symbol: 'ETHUSDT',
                timeframe: '4h',
                ema9_series: ['4200.10', '4210.20'],
                ema9_slope: '0.12340000',
                live_ema9: '4242.42',
                current_price: '4321.5000',
                source_market_version: 7,
                source_candle_id: 'ETHUSDT:4h:2026-08-21T00:00:00Z',
                calculated_at: '2026-08-21T02:03:04.567890Z',
                swing: {
                    highs: ['4400', '4500'],
                    lows: ['4100', '4200'],
                    has_higher_high: true,
                    has_higher_low: true,
                    has_lower_high: false,
                    has_lower_low: false,
                },
            },
            recommended: 'type2',
            selected: null,
        },
        trading: {
            mode: 'disabled',
            status: 'not_started',
            version: 0,
            command_enabled: false,
            logic_coverage: [
                {
                    regime_type: 'type0',
                    support_status: 'supported',
                    start_guard: 'READY',
                },
                {
                    regime_type: 'type1',
                    support_status: 'unsupported',
                    start_guard: 'UNSUPPORTED_TRADING_LOGIC',
                },
                {
                    regime_type: 'type2',
                    support_status: 'unsupported',
                    start_guard: 'UNSUPPORTED_TRADING_LOGIC',
                },
                {
                    regime_type: 'type3',
                    support_status: 'unsupported',
                    start_guard: 'UNSUPPORTED_TRADING_LOGIC',
                },
                {
                    regime_type: 'type4',
                    support_status: 'unsupported',
                    start_guard: 'UNSUPPORTED_TRADING_LOGIC',
                },
            ],
        },
        account: create_backend_account_fixture(),
        recent_trades: [{
            trade_id: 'trade-1',
            order_id: '123',
            client_order_id: 'client-1',
            symbol: 'ETHUSDT',
            executed_at: '2026-08-21T02:03:04.567890Z',
            side: 'BUY',
            regime_type: 'type2',
            strategy: 'CASE_B',
            requested_quantity: '0.10',
            executed_quantity: '0.10',
            executed_amount: '432.150',
            average_fill_price: '4321.50',
            market_price_at_decision: '4320.00',
            fee_amount: '0.0001',
            fee_asset: 'ETH',
            fee_quote_amount: '0.43215',
            allocated_cost_basis: null,
            realized_pnl: null,
            realized_return_rate: null,
            exit_reason: null,
        }],
        performance: {
            daily_return_rate: '-0.10',
            cumulative_return_rate: '-0.20',
            realized_pnl: '-0.40',
            daily_fee: '0.43215',
            total_fee: '0.43215',
            average_sell_return_rate: '-0.10',
            total_profit: '-0.40',
            winning_sell_count: 0,
            losing_sell_count: 1,
            breakeven_sell_count: 0,
            completed_sell_count: 1,
            win_rate: '0',
        },
    };
}

/**
 * 함수 이름: create_backend_event_fixture()
 * 기능: sequence와 payload만 바꿔 runtime-valid WebSocket event envelope를 생성한다.
 * 인자: sequence -> backend monotonic sequence
 *      type -> backend event type
 *      payload -> event별 JSON payload
 *      event_id -> optional canonical event UUID
 *      session_id -> optional backend session UUID
 * 반환값: generated backend event envelope
 * 작성 날짜: 2026/08/21
 */
export function create_backend_event_fixture(
    sequence: number,
    type: string,
    payload: Readonly<Record<string, unknown>>,
    event_id = TEST_BACKEND_EVENT_ID,
    session_id = TEST_BACKEND_SESSION_ID,
): BackendEventEnvelope {
    return {
        schema_version: BACKEND_SCHEMA_VERSION,
        session_id,
        event_id,
        sequence,
        occurred_at: '2026-08-21T02:03:04.567890Z',
        type,
        aggregate_version: null,
        correlation_id: null,
        payload,
    };
}
