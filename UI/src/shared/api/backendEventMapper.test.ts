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
    map_trade_history_summary,
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

/**
 * 함수 이름: create_snapshot_with_trading_patch()
 * 기능: schema v3 trading field 하나씩의 fail-closed 검증에 사용할 snapshot을 만든다.
 * 인자: patch -> authoritative trading fixture에 덮어쓸 임의 JSON field
 * 반환값: trading patch 외 aggregate가 같은 snapshot 모양
 * 작성 날짜: 2026/08/25
 */
function create_snapshot_with_trading_patch(
    patch: Readonly<Record<string, unknown>>,
): unknown {
    const snapshot = create_backend_snapshot_fixture();

    return {
        ...snapshot,
        trading: {
            ...snapshot.trading,
            ...patch,
        },
    };
}

/**
 * 함수 이름: create_configured_unbounded_snapshot()
 * 기능: 세 위험 상한을 명시적 null로 둔 configured 정책의 coherent snapshot을 만든다.
 * 인자: 없음
 * 반환값: REALIZED_ONLY와 CANCEL_AND_LIQUIDATE provenance를 가진 snapshot
 * 작성 날짜: 2026/08/29
 */
function create_configured_unbounded_snapshot(): BackendSnapshot {
    const snapshot = create_backend_snapshot_fixture();

    // Unavailable fixture의 정책 field를 한 번에 교체해 configured-unbounded 상태를 만든다.
    return {
        ...snapshot,
        trading: {
            ...snapshot.trading,
            risk_policy_availability: 'CONFIGURED',
            configured_risk_policy_version: 4,
            max_order_notional: null,
            max_position_notional: null,
            max_daily_loss: null,
            daily_loss_scope: 'REALIZED_ONLY',
            manual_kill_behavior: 'CANCEL_AND_LIQUIDATE',
            session_risk_policy_version: 4,
            last_risk_decision_allowed: true,
            last_risk_budget: {
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
            },
        },
    };
}

describe('backend runtime contract validation', () => {
    it('실시간 시장 갱신 뒤에도 마지막 4시간봉 REGIME 평가 snapshot을 읽을 수 있다', () => {
        const snapshot = create_backend_snapshot_fixture();
        const live_snapshot = {
            ...snapshot,
            market: {
                ...snapshot.market,
                version: snapshot.market.version + 4,
                current_price: '4322.5000',
            },
        };

        expect(validate_backend_snapshot(live_snapshot)).toBe(live_snapshot);
        expect(live_snapshot.regime.indicator?.source_market_version).toBe(snapshot.market.version);
        expect(live_snapshot.regime.indicator?.current_price).toBe(snapshot.market.current_price);
    });

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
            name: 'unknown policy availability',
            patch: { risk_policy_availability: 'PENDING' },
        },
        {
            name: 'configured policy without version',
            patch: {
                risk_policy_availability: 'CONFIGURED',
                configured_risk_policy_version: null,
            },
        },
        {
            name: 'unavailable policy with version',
            patch: {
                risk_policy_availability: 'UNAVAILABLE',
                configured_risk_policy_version: 1,
            },
        },
        {
            name: 'zero session policy version',
            patch: { session_risk_policy_version: 0 },
        },
        {
            name: 'negative risk control version',
            patch: { risk_control_version: -1 },
        },
        {
            name: 'non-boolean manual kill',
            patch: { manual_kill_active: 1 },
        },
        {
            name: 'non-boolean manual kill cleanup completion',
            patch: { manual_kill_cleanup_complete: 'true' },
        },
        {
            name: 'missing manual kill cleanup completion',
            patch: { manual_kill_cleanup_complete: undefined },
        },
        {
            name: 'partial manual kill activation provenance',
            patch: {
                manual_kill_active: true,
                manual_kill_activation_behavior: 'CANCEL_AND_LIQUIDATE',
            },
        },
        {
            name: 'incomplete cleanup without liquidation activation',
            patch: {
                manual_kill_active: true,
                manual_kill_cleanup_complete: false,
            },
        },
        {
            name: 'non-boolean last decision',
            patch: { last_risk_decision_allowed: 'false' },
        },
        {
            name: 'unknown risk reason',
            patch: {
                last_risk_decision_allowed: false,
                risk_block_reason: 'raw-secret-marker',
            },
        },
        {
            name: 'blocked decision without reason',
            patch: { last_risk_decision_allowed: false },
        },
        {
            name: 'allowed decision with reason',
            patch: {
                last_risk_decision_allowed: true,
                risk_block_reason: 'RISK_POLICY_UNAVAILABLE',
            },
        },
        {
            name: 'non-boolean process ownership ambiguity',
            patch: { process_ownership_ambiguous: 'true' },
        },
    ])('Phase 13 trading risk snapshot의 $name을 fail closed한다', ({ patch }) => {
        expect(() => validate_backend_snapshot(
            create_snapshot_with_trading_patch(patch),
        )).toThrowError(expect.objectContaining({ code: 'MALFORMED_BACKEND_PAYLOAD' }));
    });

    it.each([
        {
            name: 'number order cap',
            patch: { max_order_notional: 10 },
        },
        {
            name: 'zero position cap',
            patch: { max_position_notional: '0.000' },
        },
        {
            name: 'negative daily cap',
            patch: { max_daily_loss: '-1' },
        },
        {
            name: 'unknown daily scope',
            patch: { daily_loss_scope: 'ALL_PNL' },
        },
        {
            name: 'unknown manual kill behavior',
            patch: { manual_kill_behavior: 'IGNORE' },
        },
    ])('configured risk 세부 field의 $name을 fail closed한다', ({ patch }) => {
        const snapshot = create_configured_unbounded_snapshot();

        expect(() => validate_backend_snapshot({
            ...snapshot,
            trading: {
                ...snapshot.trading,
                ...patch,
            },
        })).toThrowError(expect.objectContaining({ code: 'MALFORMED_BACKEND_PAYLOAD' }));
    });

    it.each([
        {
            name: 'negative current exposure',
            patch: { current_position_notional: '-1' },
        },
        {
            name: 'number realized PnL',
            patch: { daily_realized_pnl: -12.75 },
        },
        {
            name: 'unsafe source version',
            patch: { market_version: Number.MAX_SAFE_INTEGER + 1 },
        },
        {
            name: 'inconsistent projected exposure',
            patch: { projected_position_notional: '199.99' },
        },
        {
            name: 'unknown raw field',
            patch: { raw_exchange_detail: 'must-not-enter-facade' },
        },
    ])('risk budget의 $name을 fail closed한다', ({ patch }) => {
        const snapshot = create_configured_unbounded_snapshot();

        // 정상 budget의 한 field만 변조해 Decimal·version·합계 경계를 각각 검증한다.
        expect(() => validate_backend_snapshot({
            ...snapshot,
            trading: {
                ...snapshot.trading,
                last_risk_budget: {
                    ...snapshot.trading.last_risk_budget!,
                    ...patch,
                },
            },
        })).toThrowError(expect.objectContaining({ code: 'MALFORMED_BACKEND_PAYLOAD' }));
    });

    it('risk decision과 budget의 nullable lifecycle이 다르면 fail closed한다', () => {
        const snapshot = create_configured_unbounded_snapshot();

        // 허용·차단 결과만 있고 계산 근거가 없는 publication을 거부한다.
        expect(() => validate_backend_snapshot({
            ...snapshot,
            trading: {
                ...snapshot.trading,
                last_risk_budget: null,
            },
        })).toThrowError(expect.objectContaining({ code: 'MALFORMED_BACKEND_PAYLOAD' }));
    });

    it('configured-unbounded와 unavailable을 서로 다른 coherent wire 상태로 허용한다', () => {
        const configured_snapshot = create_configured_unbounded_snapshot();
        const unavailable_snapshot = create_backend_snapshot_fixture();

        expect(validate_backend_snapshot(configured_snapshot)).toBe(configured_snapshot);
        expect(validate_backend_snapshot(unavailable_snapshot)).toBe(unavailable_snapshot);
    });

    it('malformed risk reason을 거부할 때 credential/raw payload를 오류에 포함하지 않는다', () => {
        const secret_marker = 'credential-raw-secret-marker';

        try {
            validate_backend_snapshot(create_snapshot_with_trading_patch({
                last_risk_decision_allowed: false,
                risk_block_reason: secret_marker,
            }));
            throw new Error('Expected parser failure');
        } catch (error) {
            expect(error).toBeInstanceOf(BackendContractError);
            expect(String(error)).not.toContain(secret_marker);
        }
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
    it('거래 요약과 실시간 성과 갱신에 동일한 소수점 표시 규칙을 적용한다', () => {
        const performance = {
            ...create_backend_snapshot_fixture().performance,
            daily_return_rate: '-0.04275529',
            win_rate: '25.00000000',
            average_sell_return_rate: '-0.02645909',
            realized_pnl: '-0.0212870000000000',
            daily_fee: '0.000000000000000000',
        };
        const summary = map_trade_history_summary(performance, '1.00000000');

        expect(summary).toMatchObject({
            dailyReturn: { value: '-0.04%' },
            sellPerformance: {
                winRate: '25.00%',
                averageRealizedReturn: '-0.03%',
                totalRealizedPnl: '-0.02 USDT',
            },
            position: { quantity: '1.0000 ETH' },
            fees: { amount: '0.00 USDT' },
        });
        expect(map_backend_event_to_intents(create_backend_event_fixture(
            10, 'PERFORMANCE_UPDATED', { performance },
        ))).toEqual([{
            type: 'TRADE_HISTORY_PERFORMANCE_UPDATED',
            daily_return: summary.dailyReturn,
            sell_performance: summary.sellPerformance,
            fees: summary.fees,
        }]);
    });

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
        expect(mapped.facade_options).toMatchObject({
            risk_policy_availability: 'UNAVAILABLE',
            configured_risk_policy_version: null,
            max_order_notional: null,
            max_position_notional: null,
            max_daily_loss: null,
            daily_loss_scope: null,
            manual_kill_behavior: null,
            session_risk_policy_version: null,
            risk_control_version: 0,
            manual_kill_active: false,
            manual_kill_cleanup_complete: true,
            manual_kill_activation_behavior: null,
            manual_kill_activation_policy_version: null,
            last_risk_decision_allowed: null,
            last_risk_budget: null,
            risk_block_reason: null,
            process_ownership_ambiguous: false,
        });
        expect(mapped.facade_options.is_trading).toBe(false);
        expect(mapped.facade_options).not.toHaveProperty('history_records');
        expect(mapped.server_snapshot).toMatchObject({
            trading_version: 0,
            trading_session_id: null,
            has_open_position: false,
            trading_state_label: 'not_started',
            risk_policy_availability: 'UNAVAILABLE',
            max_order_notional: null,
            max_position_notional: null,
            max_daily_loss: null,
            daily_loss_scope: null,
            manual_kill_behavior: null,
            risk_control_version: 0,
            manual_kill_active: false,
            manual_kill_cleanup_complete: true,
            manual_kill_activation_behavior: null,
            manual_kill_activation_policy_version: null,
            risk_block_reason: null,
            process_ownership_ambiguous: false,
        });
        expect(JSON.stringify(mapped.facade_options.logic_coverage)).not.toContain('LOWER_BB');
        expect(mapped.server_snapshot.account_asset).toMatchObject({
            quoteAsset: 'USDT',
            quoteValue: '120.00 USDT',
            ethAmount: '1.7500',
            ethValue: '7,562.63 USDT',
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
        expect(mapped.server_snapshot.regime_metrics.map((metric) => metric.value)).toEqual([
            '0.12% / 4H', '4,242.42 USDT', '4,200.00 USDT', '4,500.00 USDT',
        ]);
        expect(mapped.server_snapshot.trade_history_summary.position.quantity).toBe('1.7500 ETH');
        expect(mapped.server_snapshot.trade_history_summary.sellPerformance).toMatchObject({
            averageRealizedReturn: '-0.10%',
            totalRealizedPnl: '-0.40 USDT',
        });
        expect(mapped.server_snapshot.trade_history_summary.fees).toMatchObject({
            amount: '0.43 USDT',
            totalExecutedAmount: '-',
            averageSlippage: '-',
        });
    });

    it('configured-unbounded 정책을 null 상한과 typed provenance 그대로 facade에 전달한다', () => {
        const mapped = map_backend_snapshot(
            create_configured_unbounded_snapshot(),
            '2026-08-29',
        );

        // 세 null은 unavailable로 축약하거나 거대한 JS number sentinel로 바꾸지 않는다.
        expect(mapped.facade_options).toMatchObject({
            risk_policy_availability: 'CONFIGURED',
            configured_risk_policy_version: 4,
            max_order_notional: null,
            max_position_notional: null,
            max_daily_loss: null,
            daily_loss_scope: 'REALIZED_ONLY',
            manual_kill_behavior: 'CANCEL_AND_LIQUIDATE',
            session_risk_policy_version: 4,
            last_risk_decision_allowed: true,
            last_risk_budget: {
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
            },
        });
        expect(mapped.server_snapshot.max_order_notional).toBeNull();
        expect(mapped.server_snapshot.risk_policy_availability).not.toBe('UNAVAILABLE');
    });

    it('hot-swap configured policy와 활성 manual-kill epoch provenance를 별도로 보존한다', () => {
        const configured_snapshot = create_configured_unbounded_snapshot();
        const hot_swapped_snapshot: BackendSnapshot = {
            ...configured_snapshot,
            trading: {
                ...configured_snapshot.trading,
                configured_risk_policy_version: 5,
                manual_kill_behavior: 'BLOCK_NEW_ORDERS',
                session_risk_policy_version: 4,
                manual_kill_active: true,
                manual_kill_cleanup_complete: false,
                manual_kill_activation_behavior: 'CANCEL_AND_LIQUIDATE',
                manual_kill_activation_policy_version: 4,
            },
        };

        // Configured policy 교체가 이미 활성화된 cleanup provenance를 덮어쓰지 않아야 한다.
        const mapped = map_backend_snapshot(hot_swapped_snapshot, '2026-08-29');

        expect(mapped.facade_options).toMatchObject({
            configured_risk_policy_version: 5,
            manual_kill_behavior: 'BLOCK_NEW_ORDERS',
            manual_kill_activation_behavior: 'CANCEL_AND_LIQUIDATE',
            manual_kill_activation_policy_version: 4,
            manual_kill_cleanup_complete: false,
        });
        expect(mapped.server_snapshot).toMatchObject({
            manual_kill_activation_behavior: 'CANCEL_AND_LIQUIDATE',
            manual_kill_activation_policy_version: 4,
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
                position: { quantity: '1.7500 ETH' },
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
            position: { quantity: '0.0000 ETH' },
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
            risk_policy_availability: 'UNAVAILABLE',
            configured_risk_policy_version: null,
            max_order_notional: null,
            max_position_notional: null,
            max_daily_loss: null,
            daily_loss_scope: null,
            manual_kill_behavior: null,
            session_risk_policy_version: null,
            risk_control_version: 0,
            manual_kill_active: false,
            manual_kill_cleanup_complete: true,
            manual_kill_activation_behavior: null,
            manual_kill_activation_policy_version: null,
            last_risk_decision_allowed: null,
            last_risk_budget: null,
            risk_block_reason: null,
            process_ownership_ambiguous: false,
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
