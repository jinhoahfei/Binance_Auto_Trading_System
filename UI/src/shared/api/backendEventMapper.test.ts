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

/**
 * 함수 이름: create_snapshot_with_logic_coverage()
 * 기능: runtime validation 실패 경계를 검증하도록 trading coverage wire 값만 교체한다.
 * 인자: logic_coverage -> trading.logic_coverage에 넣을 임의 JSON 값
 * 반환값: coverage 외 field는 coherent fixture와 같은 snapshot 모양
 * 작성 날짜: 2026/08/21
 */
function create_snapshot_with_logic_coverage(logic_coverage: unknown): unknown {
    const snapshot = create_backend_snapshot_fixture();

    return {
        ...snapshot,
        trading: {
            ...snapshot.trading,
            logic_coverage,
        },
    };
}

describe('backend runtime contract validation', () => {
    it('ready coherent snapshot의 UUID, Decimal, UTC와 required aggregate를 검증한다', () => {
        const snapshot = create_backend_snapshot_fixture();

        expect(validate_backend_snapshot(snapshot)).toBe(snapshot);
    });

    it.each([
        {
            name: 'unknown status',
            patch: { status: 'paused' },
        },
        {
            name: 'out-of-range scale ratio',
            patch: { scale_in: '1.01' },
        },
        {
            name: 'active state without session',
            patch: { status: 'running', session_id: null },
        },
        {
            name: 'malformed session UUID',
            patch: { session_id: 'session-1' },
        },
    ])('Phase 7 trading snapshot의 $name을 fail closed한다', ({ patch }) => {
        const snapshot = create_backend_snapshot_fixture();
        const malformed_snapshot = {
            ...snapshot,
            trading: {
                ...snapshot.trading,
                ...patch,
            },
        };

        expect(() => validate_backend_snapshot(malformed_snapshot)).toThrowError(
            expect.objectContaining({ code: 'MALFORMED_BACKEND_PAYLOAD' }),
        );
    });

    it.each([
        {
            name: 'missing row',
            logic_coverage: [
                { regime_type: 'type0', support_status: 'supported', start_guard: 'READY' },
            ],
        },
        {
            name: 'duplicate regime',
            logic_coverage: [
                { regime_type: 'type0', support_status: 'supported', start_guard: 'READY' },
                { regime_type: 'type1', support_status: 'unsupported', start_guard: 'UNSUPPORTED_TRADING_LOGIC' },
                { regime_type: 'type1', support_status: 'unsupported', start_guard: 'UNSUPPORTED_TRADING_LOGIC' },
                { regime_type: 'type3', support_status: 'unsupported', start_guard: 'UNSUPPORTED_TRADING_LOGIC' },
                { regime_type: 'type4', support_status: 'unsupported', start_guard: 'UNSUPPORTED_TRADING_LOGIC' },
            ],
        },
        {
            name: 'unknown regime',
            logic_coverage: [
                { regime_type: 'type0', support_status: 'supported', start_guard: 'READY' },
                { regime_type: 'type1', support_status: 'unsupported', start_guard: 'UNSUPPORTED_TRADING_LOGIC' },
                { regime_type: 'type2', support_status: 'unsupported', start_guard: 'UNSUPPORTED_TRADING_LOGIC' },
                { regime_type: 'type3', support_status: 'unsupported', start_guard: 'UNSUPPORTED_TRADING_LOGIC' },
                { regime_type: 'type5', support_status: 'unsupported', start_guard: 'UNSUPPORTED_TRADING_LOGIC' },
            ],
        },
        {
            name: 'unknown support status',
            logic_coverage: [
                { regime_type: 'type0', support_status: 'ready', start_guard: 'READY' },
                { regime_type: 'type1', support_status: 'unsupported', start_guard: 'UNSUPPORTED_TRADING_LOGIC' },
                { regime_type: 'type2', support_status: 'unsupported', start_guard: 'UNSUPPORTED_TRADING_LOGIC' },
                { regime_type: 'type3', support_status: 'unsupported', start_guard: 'UNSUPPORTED_TRADING_LOGIC' },
                { regime_type: 'type4', support_status: 'unsupported', start_guard: 'UNSUPPORTED_TRADING_LOGIC' },
            ],
        },
        {
            name: 'inconsistent start guard',
            logic_coverage: [
                { regime_type: 'type0', support_status: 'supported', start_guard: 'UNSUPPORTED_TRADING_LOGIC' },
                { regime_type: 'type1', support_status: 'unsupported', start_guard: 'UNSUPPORTED_TRADING_LOGIC' },
                { regime_type: 'type2', support_status: 'unsupported', start_guard: 'UNSUPPORTED_TRADING_LOGIC' },
                { regime_type: 'type3', support_status: 'unsupported', start_guard: 'UNSUPPORTED_TRADING_LOGIC' },
                { regime_type: 'type4', support_status: 'unsupported', start_guard: 'UNSUPPORTED_TRADING_LOGIC' },
            ],
        },
    ])('$name TradingSTM coverage를 fail closed한다', ({ logic_coverage }) => {
        expect(() => validate_backend_snapshot(
            create_snapshot_with_logic_coverage(logic_coverage),
        )).toThrowError(expect.objectContaining({ code: 'MALFORMED_BACKEND_PAYLOAD' }));
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
                schema_version: 1,
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
    it('USDT와 nullable/unavailable 의미를 보존하고 금액 값을 JS Number로 계산하지 않는다', () => {
        const mapped = map_backend_snapshot(
            create_backend_snapshot_fixture(),
            '2026-08-21',
        );

        expect(mapped.facade_options.recommended_regime).toBe('type2');
        expect(mapped.facade_options.applied_regime).toBeNull();
        expect(mapped.facade_options.trading_symbol).toBe('ETH/USDT');
        expect(mapped.facade_options.scale_in_percentage).toBe(50);
        expect(mapped.facade_options.scale_out_percentage).toBe(50);
        expect(mapped.facade_options.logic_coverage).toEqual([
            { regime_type: 'type0', support_status: 'supported', start_guard: 'READY' },
            { regime_type: 'type1', support_status: 'unsupported', start_guard: 'UNSUPPORTED_TRADING_LOGIC' },
            { regime_type: 'type2', support_status: 'unsupported', start_guard: 'UNSUPPORTED_TRADING_LOGIC' },
            { regime_type: 'type3', support_status: 'unsupported', start_guard: 'UNSUPPORTED_TRADING_LOGIC' },
            { regime_type: 'type4', support_status: 'unsupported', start_guard: 'UNSUPPORTED_TRADING_LOGIC' },
        ]);
        expect(mapped.facade_options.command_enabled).toBe(false);
        expect(mapped.facade_options.is_trading).toBe(false);
        expect(mapped.facade_options).not.toHaveProperty('history_records');
        expect(mapped.server_snapshot).toMatchObject({
            trading_version: 0,
            trading_session_id: null,
            has_open_position: false,
            trading_state_label: 'not_started',
        });
        expect(JSON.stringify(mapped.facade_options.logic_coverage)).not.toContain('LOWER_BB');
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
        expect(mapped.server_snapshot.trade_history_summary.position.quantity).toBe('1.75 ETH');
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

    it('running session의 status/version/ratio/position/session을 authoritative UI 상태로 투영한다', () => {
        const base_snapshot = create_backend_snapshot_fixture();
        const running_snapshot = {
            ...base_snapshot,
            trading: {
                ...base_snapshot.trading,
                mode: 'fake',
                status: 'running',
                version: 7,
                command_enabled: true,
                scale_in: '0.25',
                scale_out: '0.75',
                has_open_position: true,
                session_id: '62c511b2-ea5c-43ac-bc36-e96eb39c85aa',
            },
        } as unknown as BackendSnapshot;

        const validated_snapshot = validate_backend_snapshot(running_snapshot);
        const mapped = map_backend_snapshot(validated_snapshot, '2026-08-21');

        expect(mapped.facade_options).toMatchObject({
            command_enabled: true,
            is_trading: true,
            has_open_position: true,
            scale_in_percentage: 25,
            scale_out_percentage: 75,
        });
        expect(mapped.server_snapshot).toMatchObject({
            trading_version: 7,
            trading_session_id: '62c511b2-ea5c-43ac-bc36-e96eb39c85aa',
            is_trading: true,
            has_open_position: true,
            trading_state_label: 'running',
        });
        expect(mapped.server_snapshot.account_strategy).toMatchObject({
            appliedState: 'running',
            status: '자동매매 실행 중',
            statusTone: 'positive',
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
            {
                type: 'TRADE_HISTORY_HOLDINGS_UPDATED',
                position: { quantity: '1.75 ETH' },
            },
        ]);
        expect(map_backend_event_to_intents(unknown_event)).toEqual([]);
        expect(map_backend_event_to_intents(ready_event)).toEqual([]);
    });

    it('ACCOUNT_UPDATED에 ETH balance가 없으면 Account.get_holdings 계약대로 summary를 0 ETH로 갱신한다', () => {
        const account = create_backend_snapshot_fixture().account;
        const account_event = create_backend_event_fixture(10, 'ACCOUNT_UPDATED', {
            account: {
                ...account,
                balances: account.balances.filter((balance) => balance.asset !== 'ETH'),
            },
        });

        expect(map_backend_event_to_intents(account_event)).toContainEqual({
            type: 'TRADE_HISTORY_HOLDINGS_UPDATED',
            position: { quantity: '0 ETH' },
        });
    });

    it('ACCOUNT_UPDATED envelope와 payload의 account version 불일치를 fail closed한다', () => {
        const account = create_backend_snapshot_fixture().account;
        const account_event = {
            ...create_backend_event_fixture(10, 'ACCOUNT_UPDATED', { account }),
            aggregate_version: account.version + 1,
        };

        expect(() => map_backend_event_to_intents(account_event)).toThrowError(
            expect.objectContaining({ code: 'MALFORMED_BACKEND_PAYLOAD' }),
        );
    });

    it('TRADING_SESSION_UPDATED를 version/ratio/position을 보존한 lifecycle intent로 mapping한다', () => {
        const snapshot = create_backend_snapshot_fixture();
        const trading_event = {
            ...create_backend_event_fixture(10, 'TRADING_SESSION_UPDATED', {
                trading: {
                    ...snapshot.trading,
                    mode: 'fake',
                    status: 'stopping',
                    version: 8,
                    command_enabled: false,
                    scale_in: '0.4',
                    scale_out: '0.6',
                    has_open_position: true,
                    session_id: '62c511b2-ea5c-43ac-bc36-e96eb39c85aa',
                },
            }),
            aggregate_version: 8,
        };

        expect(map_backend_event_to_intents(trading_event)).toEqual([{
            type: 'TRADING_SESSION_SYNCHRONIZED',
            status: 'stopping',
            version: 8,
            session_id: '62c511b2-ea5c-43ac-bc36-e96eb39c85aa',
            command_enabled: false,
            scale_in: '0.4',
            scale_out: '0.6',
            scale_in_percentage: 40,
            scale_out_percentage: 60,
            has_open_position: true,
            logic_coverage: snapshot.trading.logic_coverage,
            strategy_status: '자동매매 중지 처리 중',
            strategy_status_tone: 'neutral',
        }]);
    });

    it.each([
        {
            support_status: 'unsupported',
            aggregate_version: 1,
        },
        {
            support_status: 'supported',
            aggregate_version: 2,
        },
    ])('REGIME_SELECTED의 support/aggregate version 불일치를 거부한다', ({
        support_status,
        aggregate_version,
    }) => {
        const event = {
            ...create_backend_event_fixture(10, 'REGIME_SELECTED', {
                selected: 'type0',
                support_status,
                version: 1,
            }),
            aggregate_version,
        };

        expect(() => map_backend_event_to_intents(event)).toThrowError(
            expect.objectContaining({ code: 'MALFORMED_BACKEND_PAYLOAD' }),
        );
    });

    it('unknown event type은 연결 가능한 event로 parse하지만 unknown schema와 malformed UUID는 거부한다', () => {
        const event = create_backend_event_fixture(10, 'FUTURE_EVENT', {});

        expect(parse_backend_web_socket_message(JSON.stringify(event))).toEqual({
            kind: 'event',
            event,
        });
        expect(() => parse_backend_web_socket_message(JSON.stringify({
            ...event,
            schema_version: 1,
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
