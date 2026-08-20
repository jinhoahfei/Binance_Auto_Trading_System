import { describe, expect, it } from 'vitest';

import {
    BACKEND_SCHEMA_VERSION,
    type BackendSnapshot,
} from '../contracts';
import {
    BackendContractError,
    decode_backend_http_envelope,
    map_backend_event_to_intents,
    map_backend_snapshot,
    parse_backend_web_socket_message,
    validate_backend_snapshot,
} from './backendEventMapper';
import {
    create_backend_event_fixture,
    create_backend_snapshot_fixture,
    TEST_BACKEND_SESSION_ID,
} from './backendTestFixtures';

const TEST_REQUEST_ID = 'f5a4f621-25f8-4dd2-bfb7-1b80e9561423';

/**
 * 함수 이름: create_unsupported_btc_product_snapshot()
 * 기능: wire shape는 유효하지만 ADR-003의 ETHUSDT 상품을 BTCUSDT로 바꾼 snapshot을 만든다.
 * 인자: 없음
 * 반환값: runtime product 검증에서 거부되어야 하는 BTC snapshot
 * 작성 날짜: 2026/08/21
 */
function create_unsupported_btc_product_snapshot(): BackendSnapshot {
    const snapshot = create_backend_snapshot_fixture();

    // 모든 product field를 함께 BTC로 바꿔 단순 cross-field mismatch가 아닌 unsupported product를 만든다.
    return {
        ...snapshot,
        market: {
            ...snapshot.market,
            symbol: 'BTCUSDT',
        },
        regime: {
            ...snapshot.regime,
            indicator: {
                ...snapshot.regime.indicator!,
                symbol: 'BTCUSDT',
            },
        },
        account: {
            ...snapshot.account,
            valuation_asset: 'BTC',
            balances: snapshot.account.balances.map((balance) => {
                return balance.asset === 'ETH'
                    ? { ...balance, asset: 'BTC' }
                    : balance;
            }),
        },
        recent_trades: snapshot.recent_trades.map((trade) => ({
            ...trade,
            symbol: 'BTCUSDT',
        })),
    };
}

describe('backend runtime contract validation', () => {
    it('ready coherent snapshot의 UUID, Decimal, UTC와 required aggregate를 검증한다', () => {
        const snapshot = create_backend_snapshot_fixture();

        expect(validate_backend_snapshot(snapshot)).toBe(snapshot);
    });

    it.each([
        {
            name: 'non-canonical session UUID',
            mutate: () => ({
                ...create_backend_snapshot_fixture(),
                session_id: 'session-1',
            }),
            code: 'MALFORMED_BACKEND_PAYLOAD',
        },
        {
            name: 'not-ready connection',
            mutate: () => {
                const snapshot = create_backend_snapshot_fixture();
                return {
                    ...snapshot,
                    connection: { ...snapshot.connection, ready: false },
                };
            },
            code: 'BACKEND_NOT_READY',
        },
        {
            name: 'partial market',
            mutate: () => {
                const snapshot = create_backend_snapshot_fixture();
                return {
                    ...snapshot,
                    market: { ...snapshot.market, current_price: null },
                };
            },
            code: 'MALFORMED_BACKEND_PAYLOAD',
        },
        {
            name: 'missing recommendation',
            mutate: () => {
                const snapshot = create_backend_snapshot_fixture();
                return {
                    ...snapshot,
                    regime: { ...snapshot.regime, recommended: null },
                };
            },
            code: 'BACKEND_NOT_READY',
        },
        {
            name: 'partial account',
            mutate: () => {
                const snapshot = create_backend_snapshot_fixture();
                return {
                    ...snapshot,
                    account: { ...snapshot.account, valuation: null },
                };
            },
            code: 'BACKEND_NOT_READY',
        },
    ])('$name snapshot을 fail closed한다', ({ mutate, code }) => {
        expect(() => validate_backend_snapshot(mutate())).toThrowError(
            expect.objectContaining({ code }),
        );
    });

    it.each([
        {
            name: 'shape-valid BTC/BTCUSDT product',
            mutate: create_unsupported_btc_product_snapshot,
        },
        {
            name: 'indicator symbol cross mismatch',
            mutate: () => {
                const snapshot = create_backend_snapshot_fixture();
                return {
                    ...snapshot,
                    regime: {
                        ...snapshot.regime,
                        indicator: {
                            ...snapshot.regime.indicator!,
                            symbol: 'BTCUSDT',
                        },
                    },
                };
            },
        },
        {
            name: 'account base asset mismatch',
            mutate: () => {
                const snapshot = create_backend_snapshot_fixture();
                return {
                    ...snapshot,
                    account: { ...snapshot.account, valuation_asset: 'BTC' },
                };
            },
        },
        {
            name: 'recent trade product mismatch',
            mutate: () => {
                const snapshot = create_backend_snapshot_fixture();
                return {
                    ...snapshot,
                    recent_trades: snapshot.recent_trades.map((trade) => ({
                        ...trade,
                        symbol: 'BTCUSDT',
                    })),
                };
            },
        },
        {
            name: 'indicator source version mismatch',
            mutate: () => {
                const snapshot = create_backend_snapshot_fixture();
                return {
                    ...snapshot,
                    regime: {
                        ...snapshot.regime,
                        indicator: {
                            ...snapshot.regime.indicator!,
                            source_market_version: snapshot.market.version + 1,
                        },
                    },
                };
            },
        },
        {
            name: 'indicator current price mismatch',
            mutate: () => {
                const snapshot = create_backend_snapshot_fixture();
                return {
                    ...snapshot,
                    regime: {
                        ...snapshot.regime,
                        indicator: {
                            ...snapshot.regime.indicator!,
                            current_price: '4321.5001',
                        },
                    },
                };
            },
        },
    ])('$name snapshot을 product contract 위반으로 거부한다', ({ mutate }) => {
        expect(() => validate_backend_snapshot(mutate())).toThrowError(
            expect.objectContaining({ code: 'MALFORMED_BACKEND_PAYLOAD' }),
        );
    });

    it('ETH balance row가 없으면 unavailable holdings 의미를 위해 snapshot을 허용한다', () => {
        const snapshot = create_backend_snapshot_fixture();
        const snapshot_without_eth_balance = {
            ...snapshot,
            account: {
                ...snapshot.account,
                balances: snapshot.account.balances.filter((balance) => {
                    return balance.asset !== 'ETH';
                }),
            },
        };

        expect(validate_backend_snapshot(snapshot_without_eth_balance)).toBe(
            snapshot_without_eth_balance,
        );
    });

    it('HTTP envelope의 unknown schema와 request UUID mismatch를 fail closed한다', () => {
        const snapshot = create_backend_snapshot_fixture();

        expect(() => decode_backend_http_envelope(
            {
                schema_version: 2,
                request_id: TEST_REQUEST_ID,
                ok: true,
                data: snapshot,
            },
            TEST_REQUEST_ID,
            validate_backend_snapshot,
        )).toThrowError(expect.objectContaining({ code: 'UNSUPPORTED_SCHEMA_VERSION' }));
        expect(() => decode_backend_http_envelope(
            {
                schema_version: BACKEND_SCHEMA_VERSION,
                request_id: TEST_REQUEST_ID,
                ok: true,
                data: snapshot,
            },
            '2522ef0c-d88d-42b3-a22f-fc7bdd09a662',
            validate_backend_snapshot,
        )).toThrowError(expect.objectContaining({ code: 'REQUEST_ID_MISMATCH' }));
    });
});

describe('backend snapshot and event mapping', () => {
    it('USDT와 nullable/unavailable 의미를 보존하고 금융 값을 JS Number로 계산하지 않는다', () => {
        const mapped = map_backend_snapshot(
            create_backend_snapshot_fixture(),
            '2026-08-21',
        );

        expect(mapped.facade_options.recommended_regime).toBe('type2');
        expect(mapped.facade_options.applied_regime).toBeNull();
        expect(mapped.facade_options.trading_symbol).toBe('ETH/USDT');
        expect(mapped.facade_options.scale_in_percentage).toBeUndefined();
        expect(mapped.server_snapshot.account_asset).toMatchObject({
            quoteAsset: 'USDT',
            quoteValue: '120.00 USDT',
            ethAmount: '1.75',
            ethValue: '7,562.625000 USDT',
            totalValue: '-',
            profitLoss: '-',
        });
        expect(mapped.server_snapshot.account_strategy).toMatchObject({
            profitAmount: '-',
            profitRate: '-',
        });
        expect(mapped.server_snapshot.recent_trades[0]).toMatchObject({
            quote_asset: 'USDT',
            entry_price: null,
            total: '432.150',
        });
        expect(mapped.server_snapshot.trade_history_summary.position.quantity).toBe('-');
        expect(mapped.server_snapshot.trade_history_summary.sellPerformance).toMatchObject({
            averageRealizedReturn: '-0.10%',
            totalRealizedPnl: '-0.40 USDT',
        });
        expect(mapped.server_snapshot.trade_history_summary.fees).toMatchObject({
            amount: '0.43215 USDT',
            totalExecutedAmount: '-',
            averageSlippage: '-',
        });
    });

    it('known ACCOUNT_UPDATED를 facade intent로 mapping하고 unknown event와 readiness signal은 ignore한다', () => {
        const account_event = create_backend_event_fixture(10, 'ACCOUNT_UPDATED', {
            account: create_backend_snapshot_fixture().account,
        });
        const unknown_event = create_backend_event_fixture(11, 'FUTURE_EVENT', {});
        const ready_event = create_backend_event_fixture(12, 'APPLICATION_READY', {
            application_version: 1,
        });

        expect(map_backend_event_to_intents(account_event)).toEqual([
            expect.objectContaining({ type: 'ACCOUNT_ASSETS_UPDATED' }),
        ]);
        expect(map_backend_event_to_intents(unknown_event)).toEqual([]);
        expect(map_backend_event_to_intents(ready_event)).toEqual([]);
    });

    it('unknown event type은 연결 가능한 event로 parse하지만 unknown schema와 malformed UUID는 거부한다', () => {
        const event = create_backend_event_fixture(10, 'FUTURE_EVENT', {});

        expect(parse_backend_web_socket_message(JSON.stringify(event))).toEqual({
            kind: 'event',
            event,
        });
        expect(() => parse_backend_web_socket_message(JSON.stringify({
            ...event,
            schema_version: 2,
        }))).toThrowError(expect.objectContaining({ code: 'UNSUPPORTED_SCHEMA_VERSION' }));
        expect(() => parse_backend_web_socket_message(JSON.stringify({
            ...event,
            event_id: 'not-a-uuid',
        }))).toThrowError(expect.objectContaining({ code: 'MALFORMED_BACKEND_PAYLOAD' }));
        expect(TEST_BACKEND_SESSION_ID).toMatch(/^[0-9a-f-]{36}$/u);
    });

    it('contract error message에는 raw payload가 포함되지 않는다', () => {
        const secret_marker = 'secret-marker-that-must-not-leak';

        try {
            parse_backend_web_socket_message(secret_marker);
            throw new Error('Expected parser failure');
        } catch (error) {
            expect(error).toBeInstanceOf(BackendContractError);
            expect(String(error)).not.toContain(secret_marker);
        }
    });
});
