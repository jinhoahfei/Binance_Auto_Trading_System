import type {
    BackendAccountSnapshot,
    BackendEventEnvelope,
    BackendHttpEnvelope,
    BackendIndicatorSnapshot,
    BackendPerformanceSnapshot,
    BackendRegimeType,
    BackendResyncRequired,
    BackendSnapshot,
    BackendTradeSnapshot,
} from '../contracts';
import { BACKEND_SCHEMA_VERSION } from '../contracts';
import type {
    RegimeMetric,
    RegimeType,
    TradeRecord,
} from '../contracts';
import {
    format_decimal_text,
    format_quote_amount,
} from '../formatting';
import type {
    UiApplicationFacadeOptions,
    UiApplicationIntent,
    UiServerOwnedSnapshot,
} from '../../app/control';

const DECIMAL_PATTERN = /^-?(0|[1-9][0-9]*)(\.[0-9]+)?$/u;
const UTC_TIMESTAMP_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/u;
const CANONICAL_UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;

// ADR-003의 첫 release 상품을 transport runtime validation의 단일 상수 묶음으로 고정한다.
const SUPPORTED_MARKET_SYMBOL = 'ETHUSDT';
const SUPPORTED_BASE_ASSET = 'ETH';
const SUPPORTED_QUOTE_ASSET = 'USDT';
const BACKEND_REGIME_TYPES: ReadonlySet<string> = new Set([
    'type0',
    'type1',
    'type2',
    'type3',
    'type4',
]);
const BACKEND_EXECUTION_MODES: ReadonlySet<string> = new Set([
    'disabled',
    'fake',
    'testnet',
    'live',
]);

/**
 * 클래스 이름: BackendContractError
 * 기능: raw payload나 secret을 포함하지 않는 backend runtime contract 실패를 표현한다.
 * 작성 날짜: 2026/08/21
 */
export class BackendContractError extends Error {
    readonly code: string;

    /**
     * 함수 이름: BackendContractError.constructor()
     * 기능: 안전한 오류 code와 고정된 사용자 비노출 설명만 보존한다.
     * 인자: code -> contract 실패 code
     *      message -> secret이 없는 정적 오류 설명
     * 반환값: BackendContractError 인스턴스
     * 작성 날짜: 2026/08/21
     */
    constructor(code: string, message: string) {
        super(message);
        this.name = 'BackendContractError';
        this.code = code;
    }
}

/**
 * coherent backend snapshot과 facade 생성 옵션을 함께 제공하는 mapper 결과이다.
 */
export interface MappedBackendSnapshot {
    readonly facade_options: UiApplicationFacadeOptions;
    readonly server_snapshot: UiServerOwnedSnapshot;
}

/**
 * WebSocket JSON frame의 정상 event 또는 resync control frame이다.
 */
export type ParsedBackendWebSocketMessage =
    | { readonly kind: 'event'; readonly event: BackendEventEnvelope }
    | { readonly kind: 'resync_required'; readonly control: BackendResyncRequired };

/**
 * 함수 이름: assert_record()
 * 기능: JSON 값이 null이나 배열이 아닌 object인지 검증한다.
 * 인자: value -> 검증할 JSON 값, field_name -> 오류 field 이름
 * 반환값: 문자열 key를 가진 JSON object
 * 작성 날짜: 2026/08/21
 */
function assert_record(value: unknown, field_name: string): Record<string, unknown> {
    if (typeof value !== 'object' || value === null || Array.isArray(value)) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            `${field_name} must be an object`,
        );
    }

    return value as Record<string, unknown>;
}

/**
 * 함수 이름: assert_string()
 * 기능: JSON field가 비어 있지 않은 문자열인지 검증한다.
 * 인자: value -> 검증할 JSON 값, field_name -> 오류 field 이름
 * 반환값: 검증된 문자열
 * 작성 날짜: 2026/08/21
 */
function assert_string(value: unknown, field_name: string): string {
    if (typeof value !== 'string' || value.length === 0) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            `${field_name} must be a non-empty string`,
        );
    }

    return value;
}

/**
 * 함수 이름: assert_uuid()
 * 기능: session, request, event ID가 lowercase canonical UUID 형식인지 검증한다.
 * 인자: value -> 검증할 JSON 값, field_name -> 오류 field 이름
 * 반환값: 검증된 canonical UUID 문자열
 * 작성 날짜: 2026/08/21
 */
function assert_uuid(value: unknown, field_name: string): string {
    const uuid = assert_string(value, field_name);

    if (!CANONICAL_UUID_PATTERN.test(uuid)) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            `${field_name} must be a canonical UUID`,
        );
    }

    return uuid;
}

/**
 * 함수 이름: assert_boolean()
 * 기능: JSON field가 boolean인지 검증한다.
 * 인자: value -> 검증할 JSON 값, field_name -> 오류 field 이름
 * 반환값: 검증된 boolean
 * 작성 날짜: 2026/08/21
 */
function assert_boolean(value: unknown, field_name: string): boolean {
    if (typeof value !== 'boolean') {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            `${field_name} must be a boolean`,
        );
    }

    return value;
}

/**
 * 함수 이름: assert_safe_integer()
 * 기능: browser에서 순서와 version을 손실 없이 비교할 안전한 정수인지 검증한다.
 * 인자: value -> 검증할 JSON 값, field_name -> 오류 field 이름, minimum -> 허용 최소값
 * 반환값: 검증된 안전한 정수
 * 작성 날짜: 2026/08/21
 */
function assert_safe_integer(
    value: unknown,
    field_name: string,
    minimum = 0,
): number {
    if (!Number.isSafeInteger(value) || (value as number) < minimum) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            `${field_name} must be a safe integer`,
        );
    }

    return value as number;
}

/**
 * 함수 이름: assert_decimal_string()
 * 기능: 금융 wire 값이 exponent와 locale separator가 없는 plain Decimal 문자열인지 검증한다.
 * 인자: value -> 검증할 JSON 값, field_name -> 오류 field 이름
 * 반환값: 검증된 Decimal 문자열
 * 작성 날짜: 2026/08/21
 */
function assert_decimal_string(value: unknown, field_name: string): string {
    const decimal_text = assert_string(value, field_name);

    if (!DECIMAL_PATTERN.test(decimal_text)) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            `${field_name} must be a plain decimal string`,
        );
    }

    return decimal_text;
}

/**
 * 함수 이름: assert_nullable_decimal_string()
 * 기능: nullable 금융 wire 값을 plain Decimal 문자열 또는 null로 제한한다.
 * 인자: value -> 검증할 JSON 값, field_name -> 오류 field 이름
 * 반환값: 검증된 Decimal 문자열 또는 null
 * 작성 날짜: 2026/08/21
 */
function assert_nullable_decimal_string(
    value: unknown,
    field_name: string,
): string | null {
    return value === null
        ? null
        : assert_decimal_string(value, field_name);
}

/**
 * 함수 이름: assert_nullable_string()
 * 기능: JSON field를 non-empty 문자열 또는 null로 제한한다.
 * 인자: value -> 검증할 JSON 값, field_name -> 오류 field 이름
 * 반환값: 검증된 문자열 또는 null
 * 작성 날짜: 2026/08/21
 */
function assert_nullable_string(value: unknown, field_name: string): string | null {
    return value === null
        ? null
        : assert_string(value, field_name);
}

/**
 * 함수 이름: assert_utc_timestamp()
 * 기능: backend 시각이 UTC Z suffix의 RFC 3339 문자열인지 검증한다.
 * 인자: value -> 검증할 JSON 값, field_name -> 오류 field 이름
 * 반환값: 검증된 UTC 시각 문자열
 * 작성 날짜: 2026/08/21
 */
function assert_utc_timestamp(value: unknown, field_name: string): string {
    const timestamp = assert_string(value, field_name);

    if (!UTC_TIMESTAMP_PATTERN.test(timestamp) || Number.isNaN(Date.parse(timestamp))) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            `${field_name} must be an RFC 3339 UTC timestamp`,
        );
    }

    return timestamp;
}

/**
 * 함수 이름: assert_schema_version()
 * 기능: backend major schema가 renderer와 같은 version인지 fail closed로 검증한다.
 * 인자: value -> 검증할 schema version
 * 반환값: 없음
 * 작성 날짜: 2026/08/21
 */
function assert_schema_version(value: unknown): void {
    if (value !== BACKEND_SCHEMA_VERSION) {
        throw new BackendContractError(
            'UNSUPPORTED_SCHEMA_VERSION',
            'Backend schema version is not supported',
        );
    }
}

/**
 * 함수 이름: validate_indicator_snapshot()
 * 기능: REGIME 표시와 provenance에 필요한 indicator snapshot field를 검증한다.
 * 인자: value -> indicator JSON 값
 * 반환값: generated indicator contract
 * 작성 날짜: 2026/08/21
 */
function validate_indicator_snapshot(value: unknown): BackendIndicatorSnapshot {
    const indicator = assert_record(value, 'indicator');
    const ema9_series = indicator.ema9_series;
    const swing = assert_record(indicator.swing, 'indicator.swing');

    if (indicator.symbol !== SUPPORTED_MARKET_SYMBOL) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'indicator.symbol must use the supported ETHUSDT product',
        );
    }
    if (indicator.timeframe !== '4h') {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'indicator.timeframe must be 4h',
        );
    }
    if (!Array.isArray(ema9_series)) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'indicator.ema9_series must be an array',
        );
    }
    ema9_series.forEach((item, index) => {
        assert_decimal_string(item, `indicator.ema9_series[${index}]`);
    });
    assert_decimal_string(indicator.ema9_slope, 'indicator.ema9_slope');
    assert_decimal_string(indicator.live_ema9, 'indicator.live_ema9');
    assert_decimal_string(indicator.current_price, 'indicator.current_price');
    assert_safe_integer(indicator.source_market_version, 'indicator.source_market_version', 1);
    assert_string(indicator.source_candle_id, 'indicator.source_candle_id');
    assert_utc_timestamp(indicator.calculated_at, 'indicator.calculated_at');

    for (const point_name of ['highs', 'lows'] as const) {
        const points = swing[point_name];

        if (!Array.isArray(points)) {
            throw new BackendContractError(
                'MALFORMED_BACKEND_PAYLOAD',
                `indicator.swing.${point_name} must be an array`,
            );
        }
        points.forEach((item, index) => {
            assert_decimal_string(item, `indicator.swing.${point_name}[${index}]`);
        });
    }
    assert_boolean(swing.has_higher_high, 'indicator.swing.has_higher_high');
    assert_boolean(swing.has_higher_low, 'indicator.swing.has_higher_low');
    assert_boolean(swing.has_lower_high, 'indicator.swing.has_lower_high');
    assert_boolean(swing.has_lower_low, 'indicator.swing.has_lower_low');

    return value as BackendIndicatorSnapshot;
}

/**
 * 함수 이름: validate_account_snapshot()
 * 기능: account와 balance의 Decimal·asset·UTC contract를 검증한다.
 * 인자: value -> account JSON 값
 * 반환값: generated account contract
 * 작성 날짜: 2026/08/21
 */
export function validate_account_snapshot(value: unknown): BackendAccountSnapshot {
    const account = assert_record(value, 'account');
    const balances = account.balances;

    if (account.valuation_asset !== SUPPORTED_BASE_ASSET) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'account.valuation_asset must use the supported ETH base asset',
        );
    }
    if (account.quote_asset !== SUPPORTED_QUOTE_ASSET) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'account.quote_asset must be USDT',
        );
    }
    assert_nullable_decimal_string(account.current_price, 'account.current_price');
    assert_nullable_decimal_string(account.valuation, 'account.valuation');
    assert_safe_integer(account.version, 'account.version');
    if (account.updated_at !== null) {
        assert_utc_timestamp(account.updated_at, 'account.updated_at');
    }
    if (!Array.isArray(balances)) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'account.balances must be an array',
        );
    }
    balances.forEach((balance_value, index) => {
        const balance = assert_record(balance_value, `account.balances[${index}]`);

        assert_string(balance.asset, `account.balances[${index}].asset`);
        assert_decimal_string(balance.free, `account.balances[${index}].free`);
        assert_decimal_string(balance.locked, `account.balances[${index}].locked`);
        assert_decimal_string(balance.total, `account.balances[${index}].total`);
    });

    return value as BackendAccountSnapshot;
}

/**
 * 함수 이름: validate_trade_snapshot()
 * 기능: durable Trade의 wire enum, Decimal, UTC와 nullable realized field를 검증한다.
 * 인자: value -> Trade JSON 값
 * 반환값: generated Trade contract
 * 작성 날짜: 2026/08/21
 */
export function validate_trade_snapshot(value: unknown): BackendTradeSnapshot {
    const trade = assert_record(value, 'trade');

    assert_string(trade.trade_id, 'trade.trade_id');
    assert_string(trade.order_id, 'trade.order_id');
    assert_string(trade.client_order_id, 'trade.client_order_id');
    if (trade.symbol !== SUPPORTED_MARKET_SYMBOL) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'trade.symbol must use the supported ETHUSDT product',
        );
    }
    assert_utc_timestamp(trade.executed_at, 'trade.executed_at');
    if (trade.side !== 'BUY' && trade.side !== 'SELL') {
        throw new BackendContractError('MALFORMED_BACKEND_PAYLOAD', 'trade.side is invalid');
    }
    if (typeof trade.regime_type !== 'string' || !BACKEND_REGIME_TYPES.has(trade.regime_type)) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'trade.regime_type is invalid',
        );
    }
    if (trade.strategy !== 'CASE_B' && trade.strategy !== 'CASE_C') {
        throw new BackendContractError('MALFORMED_BACKEND_PAYLOAD', 'trade.strategy is invalid');
    }

    const required_decimal_fields = [
        'requested_quantity',
        'executed_quantity',
        'executed_amount',
        'average_fill_price',
        'market_price_at_decision',
        'fee_amount',
        'fee_quote_amount',
    ] as const;
    required_decimal_fields.forEach((field_name) => {
        assert_decimal_string(trade[field_name], `trade.${field_name}`);
    });
    assert_string(trade.fee_asset, 'trade.fee_asset');
    assert_nullable_decimal_string(trade.allocated_cost_basis, 'trade.allocated_cost_basis');
    assert_nullable_decimal_string(trade.realized_pnl, 'trade.realized_pnl');
    assert_nullable_decimal_string(trade.realized_return_rate, 'trade.realized_return_rate');
    assert_nullable_string(trade.exit_reason, 'trade.exit_reason');

    return value as BackendTradeSnapshot;
}

/**
 * 함수 이름: validate_performance_snapshot()
 * 기능: Performance Decimal과 non-negative count field를 검증한다.
 * 인자: value -> Performance JSON 값
 * 반환값: generated Performance contract
 * 작성 날짜: 2026/08/21
 */
export function validate_performance_snapshot(value: unknown): BackendPerformanceSnapshot {
    const performance = assert_record(value, 'performance');
    const decimal_fields = [
        'daily_return_rate',
        'cumulative_return_rate',
        'realized_pnl',
        'daily_fee',
        'total_fee',
        'average_sell_return_rate',
        'total_profit',
    ] as const;

    decimal_fields.forEach((field_name) => {
        assert_decimal_string(performance[field_name], `performance.${field_name}`);
    });
    assert_nullable_decimal_string(performance.win_rate, 'performance.win_rate');
    for (const count_name of [
        'winning_sell_count',
        'losing_sell_count',
        'breakeven_sell_count',
        'completed_sell_count',
    ] as const) {
        assert_safe_integer(performance[count_name], `performance.${count_name}`);
    }

    return value as BackendPerformanceSnapshot;
}

/**
 * 함수 이름: validate_backend_snapshot()
 * 기능: `/v1/snapshot` data 전체를 generated schema의 runtime contract로 검증한다.
 * 인자: value -> HTTP envelope의 data JSON 값
 * 반환값: coherent backend snapshot
 * 작성 날짜: 2026/08/21
 */
export function validate_backend_snapshot(value: unknown): BackendSnapshot {
    const snapshot = assert_record(value, 'snapshot');
    const connection = assert_record(snapshot.connection, 'snapshot.connection');
    const market = assert_record(snapshot.market, 'snapshot.market');
    const regime = assert_record(snapshot.regime, 'snapshot.regime');
    const trading = assert_record(snapshot.trading, 'snapshot.trading');
    const recent_trades = snapshot.recent_trades;

    assert_uuid(snapshot.session_id, 'snapshot.session_id');
    assert_safe_integer(snapshot.last_sequence, 'snapshot.last_sequence');
    if (connection.status !== 'online') {
        throw new BackendContractError('MALFORMED_BACKEND_PAYLOAD', 'connection.status is invalid');
    }
    assert_boolean(connection.ready, 'connection.ready');
    assert_schema_version(connection.schema_version);
    if (connection.ready !== true) {
        throw new BackendContractError(
            'BACKEND_NOT_READY',
            'Backend snapshot is not ready',
        );
    }

    if (market.symbol !== SUPPORTED_MARKET_SYMBOL) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'market.symbol must use the supported ETHUSDT product',
        );
    }
    const market_current_price = assert_decimal_string(
        market.current_price,
        'market.current_price',
    );
    const market_version = assert_safe_integer(market.version, 'market.version', 1);
    assert_utc_timestamp(market.updated_at, 'market.updated_at');

    if (regime.indicator === null) {
        throw new BackendContractError(
            'BACKEND_NOT_READY',
            'Backend snapshot does not contain a ready indicator',
        );
    }
    const indicator = validate_indicator_snapshot(regime.indicator);

    // Ready REGIME provenance는 같은 authoritative MarketSnapshot version과 가격을 가리켜야 한다.
    if (indicator.source_market_version !== market_version
        || indicator.current_price !== market_current_price) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'indicator provenance does not match the market snapshot',
        );
    }
    for (const regime_field of ['recommended', 'selected'] as const) {
        const regime_value = regime[regime_field];

        if (regime_value !== null
            && (typeof regime_value !== 'string' || !BACKEND_REGIME_TYPES.has(regime_value))) {
            throw new BackendContractError(
                'MALFORMED_BACKEND_PAYLOAD',
                `regime.${regime_field} is invalid`,
            );
        }
    }
    if (regime.recommended === null) {
        throw new BackendContractError(
            'BACKEND_NOT_READY',
            'Backend snapshot does not contain a recommendation',
        );
    }

    if (typeof trading.mode !== 'string' || !BACKEND_EXECUTION_MODES.has(trading.mode)) {
        throw new BackendContractError('MALFORMED_BACKEND_PAYLOAD', 'trading.mode is invalid');
    }
    if (trading.status !== 'not_started' || trading.command_enabled !== false) {
        throw new BackendContractError('MALFORMED_BACKEND_PAYLOAD', 'trading state is invalid');
    }
    assert_safe_integer(trading.version, 'trading.version');

    const account = validate_account_snapshot(snapshot.account);
    if (account.current_price === null
        || account.valuation === null
        || account.updated_at === null
        || account.version < 1) {
        throw new BackendContractError(
            'BACKEND_NOT_READY',
            'Backend snapshot does not contain a ready account',
        );
    }
    if (!Array.isArray(recent_trades)) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'recent_trades must be an array',
        );
    }
    recent_trades.forEach(validate_trade_snapshot);
    validate_performance_snapshot(snapshot.performance);

    return value as BackendSnapshot;
}

/**
 * 함수 이름: decode_backend_http_envelope()
 * 기능: 공통 HTTP envelope, request ID와 data runtime contract를 함께 검증한다.
 * 인자: value -> JSON response
 *      expected_request_id -> 요청 header에 보낸 UUID
 *      validate_data -> endpoint data validator
 * 반환값: 성공 envelope의 검증된 data
 * 작성 날짜: 2026/08/21
 */
export function decode_backend_http_envelope<Data>(
    value: unknown,
    expected_request_id: string,
    validate_data: (data: unknown) => Data,
): Data {
    const envelope = assert_record(value, 'response');

    assert_schema_version(envelope.schema_version);
    if (assert_uuid(envelope.request_id, 'response.request_id') !== expected_request_id) {
        throw new BackendContractError(
            'REQUEST_ID_MISMATCH',
            'Backend response request ID does not match',
        );
    }
    if (envelope.ok === true) {
        return validate_data(envelope.data);
    }
    if (envelope.ok !== false) {
        throw new BackendContractError('MALFORMED_BACKEND_PAYLOAD', 'response.ok is invalid');
    }

    const error = assert_record(envelope.error, 'response.error');
    const details = assert_record(error.details, 'response.error.details');
    const failure_envelope = envelope as unknown as BackendHttpEnvelope<never>;

    assert_string(error.code, 'response.error.code');
    assert_string(error.message, 'response.error.message');
    assert_boolean(error.retryable, 'response.error.retryable');
    void details;

    if (failure_envelope.ok) {
        throw new BackendContractError('MALFORMED_BACKEND_PAYLOAD', 'response error is invalid');
    }

    throw new BackendCommandError(
        failure_envelope.error.code,
        failure_envelope.error.message,
        failure_envelope.error.retryable,
    );
}

/**
 * 클래스 이름: BackendCommandError
 * 기능: backend가 반환한 user-safe typed failure만 UI command actor에 전달한다.
 * 작성 날짜: 2026/08/21
 */
export class BackendCommandError extends Error {
    readonly code: string;
    readonly retryable: boolean;

    /**
     * 함수 이름: BackendCommandError.constructor()
     * 기능: typed backend failure의 안전한 code, message와 retry 가능 여부를 보존한다.
     * 인자: code -> backend failure code
     *      message -> backend가 보장한 user-safe message
     *      retryable -> 동일 command를 재시도할 수 있는지 여부
     * 반환값: BackendCommandError 인스턴스
     * 작성 날짜: 2026/08/21
     */
    constructor(code: string, message: string, retryable: boolean) {
        super(message);
        this.name = 'BackendCommandError';
        this.code = code;
        this.retryable = retryable;
    }
}

/**
 * 함수 이름: parse_backend_web_socket_message()
 * 기능: WebSocket JSON frame을 event 또는 RESYNC_REQUIRED control로 검증한다.
 * 인자: frame_data -> WebSocket text frame
 * 반환값: 검증된 event/control 구분값
 * 작성 날짜: 2026/08/21
 */
export function parse_backend_web_socket_message(
    frame_data: unknown,
): ParsedBackendWebSocketMessage {
    if (typeof frame_data !== 'string') {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'Backend WebSocket frame must be text JSON',
        );
    }

    let parsed_value: unknown;
    try {
        parsed_value = JSON.parse(frame_data) as unknown;
    } catch {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'Backend WebSocket frame is not valid JSON',
        );
    }

    const message = assert_record(parsed_value, 'event');
    assert_schema_version(message.schema_version);
    const session_id = assert_uuid(message.session_id, 'event.session_id');

    if (message.type === 'RESYNC_REQUIRED') {
        if (message.reason !== 'REPLAY_GAP' && message.reason !== 'SEQUENCE_AHEAD') {
            throw new BackendContractError(
                'MALFORMED_BACKEND_PAYLOAD',
                'RESYNC_REQUIRED reason is invalid',
            );
        }
        assert_safe_integer(message.last_sequence, 'event.last_sequence');

        return {
            kind: 'resync_required',
            control: message as unknown as BackendResyncRequired,
        };
    }

    assert_uuid(message.event_id, 'event.event_id');
    assert_safe_integer(message.sequence, 'event.sequence', 1);
    assert_utc_timestamp(message.occurred_at, 'event.occurred_at');
    assert_string(message.type, 'event.type');
    if (message.aggregate_version !== null) {
        assert_safe_integer(message.aggregate_version, 'event.aggregate_version');
    }
    assert_nullable_string(message.correlation_id, 'event.correlation_id');
    assert_record(message.payload, 'event.payload');
    void session_id;

    return {
        kind: 'event',
        event: message as unknown as BackendEventEnvelope,
    };
}

/**
 * 함수 이름: decimal_tone()
 * 기능: Decimal 문자열의 부호만으로 표시 tone을 선택하고 어떤 금융 계산도 수행하지 않는다.
 * 인자: decimal_text -> validated Decimal string
 * 반환값: positive, negative 또는 neutral tone
 * 작성 날짜: 2026/08/21
 */
function decimal_tone(decimal_text: string): RegimeMetric['tone'] {
    if (/^-0(?:\.0+)?$/u.test(decimal_text) || /^0(?:\.0+)?$/u.test(decimal_text)) {
        return 'neutral';
    }

    return decimal_text.startsWith('-') ? 'negative' : 'positive';
}

/**
 * 함수 이름: map_indicator_metrics()
 * 기능: raw indicator Decimal과 swing point를 계산 없이 기존 REGIME metric 표시로 변환한다.
 * 인자: indicator -> validated backend indicator 또는 null
 * 반환값: 기존 facade가 수용하는 네 metric 목록
 * 작성 날짜: 2026/08/21
 */
function map_indicator_metrics(
    indicator: BackendIndicatorSnapshot | null,
): ReadonlyArray<RegimeMetric> {
    if (indicator === null) {
        return [];
    }

    const latest_swing_low = indicator.swing.lows.at(-1);
    const latest_swing_high = indicator.swing.highs.at(-1);

    return [
        {
            id: 'emaSlope',
            label: 'EMA9 기울기',
            value: `${format_decimal_text(indicator.ema9_slope)}% / 4H`,
            tone: decimal_tone(indicator.ema9_slope),
        },
        {
            id: 'ema',
            label: '실시간 EMA9',
            value: `${format_decimal_text(indicator.live_ema9)} USDT`,
            tone: 'neutral',
        },
        {
            id: 'swingLow',
            label: 'Swing Low',
            value: latest_swing_low === undefined
                ? '-'
                : `${format_decimal_text(latest_swing_low)} USDT`,
            tone: indicator.swing.has_higher_low
                ? 'positive'
                : indicator.swing.has_lower_low
                    ? 'negative'
                    : 'neutral',
        },
        {
            id: 'swingHigh',
            label: 'Swing High',
            value: latest_swing_high === undefined
                ? '-'
                : `${format_decimal_text(latest_swing_high)} USDT`,
            tone: indicator.swing.has_higher_high
                ? 'positive'
                : indicator.swing.has_lower_high
                    ? 'negative'
                    : 'neutral',
        },
    ];
}

/**
 * 함수 이름: map_trade_record()
 * 기능: durable backend Trade를 계산 없이 기존 UI 체결 record로 변환한다.
 * 인자: trade -> validated backend Trade
 * 반환값: nullable unavailable entry price와 USDT 단위를 보존한 UI record
 * 작성 날짜: 2026/08/21
 */
export function map_trade_record(trade: BackendTradeSnapshot): TradeRecord {
    return {
        id: trade.trade_id,
        symbol: trade.symbol,
        quote_asset: 'USDT',
        occurred_at: trade.executed_at,
        side: trade.side === 'BUY' ? 'buy' : 'sell',
        regime: trade.regime_type,
        strategy: trade.strategy,
        price: trade.average_fill_price,
        entry_price: null,
        market_price_at_decision: trade.market_price_at_decision,
        quantity: trade.executed_quantity,
        total: trade.executed_amount,
        fee: trade.fee_quote_amount,
        profit_rate: trade.realized_return_rate,
        realized_pnl: trade.realized_pnl,
        exit_reason: trade.exit_reason,
    };
}

/**
 * 함수 이름: map_account_asset()
 * 기능: backend Account이 실제 제공하는 ETH/USDT 값만 표시하고 총자산·평가손익은 추측하지 않는다.
 * 인자: account -> validated backend account
 * 반환값: USDT quote label과 unavailable field가 명시된 asset ViewModel
 * 작성 날짜: 2026/08/21
 */
function map_account_asset(account: BackendAccountSnapshot): UiServerOwnedSnapshot['account_asset'] {
    const eth_balance = account.balances.find((balance) => {
        return balance.asset === account.valuation_asset;
    });
    const quote_balance = account.balances.find((balance) => {
        return balance.asset === account.quote_asset;
    });

    return {
        ethAmount: eth_balance === undefined
            ? '-'
            : format_decimal_text(eth_balance.total),
        ethValue: account.valuation === null
            ? '-'
            : format_quote_amount(account.valuation, account.quote_asset),
        krwValue: '-',
        quoteAsset: account.quote_asset,
        quoteValue: quote_balance === undefined
            ? '-'
            : format_quote_amount(quote_balance.total, account.quote_asset),
        profitLoss: '-',
        totalValue: '-',
    };
}

/**
 * 함수 이름: format_rate()
 * 기능: backend percentage Decimal 문자열을 계산 없이 percentage 표시로 변환한다.
 * 인자: decimal_text -> validated percentage Decimal 또는 null
 * 반환값: percentage 표시 또는 unavailable 표시
 * 작성 날짜: 2026/08/21
 */
function format_rate(decimal_text: string | null): string {
    return decimal_text === null ? '--%' : `${format_decimal_text(decimal_text)}%`;
}

/**
 * 함수 이름: map_performance_summary()
 * 기능: Performance가 실제 제공하는 값만 history summary에 투영한다.
 * 인자: performance -> validated backend Performance
 * 반환값: 계산되지 않은 체결총액/slippage가 unavailable인 summary
 * 작성 날짜: 2026/08/21
 */
function map_performance_summary(
    performance: BackendPerformanceSnapshot,
): UiServerOwnedSnapshot['trade_history_summary'] {
    const result_tone = decimal_tone(performance.realized_pnl);

    return {
        dailyReturn: {
            value: format_rate(performance.daily_return_rate),
            tone: decimal_tone(performance.daily_return_rate),
        },
        sellPerformance: {
            winRate: format_rate(performance.win_rate),
            completedCount: `${performance.winning_sell_count} / ${performance.completed_sell_count}`,
            averageRealizedReturn: format_rate(performance.average_sell_return_rate),
            totalRealizedPnl: format_quote_amount(performance.realized_pnl, 'USDT'),
            tone: result_tone,
        },
        position: {
            quantity: '-',
        },
        fees: {
            amount: format_quote_amount(performance.daily_fee, 'USDT'),
            totalExecutedAmount: '-',
            averageSlippage: '-',
        },
    };
}

/**
 * 함수 이름: map_backend_snapshot()
 * 기능: coherent backend snapshot을 cold facade options와 reconnect 전체 교체값으로 변환한다.
 * 인자: snapshot -> runtime 검증이 끝난 backend snapshot
 *      today -> CSV actor가 사용할 KST LocalDate
 * 반환값: 같은 source를 공유하는 facade options와 server-owned snapshot
 * 작성 날짜: 2026/08/21
 */
export function map_backend_snapshot(
    snapshot: BackendSnapshot,
    today: string,
): MappedBackendSnapshot {
    const regime_metrics = map_indicator_metrics(snapshot.regime.indicator);
    const recent_trades = snapshot.recent_trades.map(map_trade_record);
    const account_asset = map_account_asset(snapshot.account);
    const trade_history_summary = map_performance_summary(snapshot.performance);
    const account_strategy = {
        appliedState: snapshot.trading.status,
        // Performance 누적값은 현재 REGIME 상태별 성과가 아니므로 StrategyCard에는 투영하지 않는다.
        profitAmount: '-',
        profitRate: '-',
        status: '매매 시작 전',
        statusTone: 'neutral' as const,
    };
    const server_snapshot: UiServerOwnedSnapshot = {
        last_sequence: snapshot.last_sequence,
        trading_symbol: snapshot.market.symbol === 'ETHUSDT'
            ? 'ETH/USDT'
            : snapshot.market.symbol,
        recommended_regime: snapshot.regime.recommended,
        applied_regime: snapshot.regime.selected,
        regime_metrics,
        recent_trades,
        history_records: recent_trades,
        account_strategy,
        account_asset,
        trade_history_summary,
        is_trading: false,
        trading_state_label: snapshot.trading.status,
    };

    return {
        facade_options: {
            today,
            trading_symbol: server_snapshot.trading_symbol,
            recommended_regime: server_snapshot.recommended_regime,
            applied_regime: server_snapshot.applied_regime,
            regime_metrics: server_snapshot.regime_metrics,
            recent_trades: server_snapshot.recent_trades,
            history_records: server_snapshot.history_records,
            account_strategy: server_snapshot.account_strategy,
            account_asset: server_snapshot.account_asset,
            trade_history_summary: server_snapshot.trade_history_summary,
            is_trading: server_snapshot.is_trading,
        },
        server_snapshot,
    };
}

/**
 * 함수 이름: map_backend_event_to_intents()
 * 기능: 알려진 backend event를 기존 facade intent로만 변환하고 알 수 없는 type은 무시한다.
 * 인자: event -> envelope runtime 검증이 끝난 정확히 다음 sequence event
 * 반환값: 순서대로 dispatch할 기존 facade intent 목록
 * 작성 날짜: 2026/08/21
 */
export function map_backend_event_to_intents(
    event: BackendEventEnvelope,
): ReadonlyArray<UiApplicationIntent> {
    const payload = assert_record(event.payload, 'event.payload');

    switch (event.type) {
        case 'APPLICATION_READY':
            // Snapshot-first startup가 이미 ready 상태를 적용하므로 readiness signal은 표시 state를 바꾸지 않는다.
            return [];
        case 'ACCOUNT_UPDATED': {
            const account = validate_account_snapshot(payload.account);
            return [{
                type: 'ACCOUNT_ASSETS_UPDATED',
                asset: map_account_asset(account),
            }];
        }
        case 'REGIME_RECOMMENDED': {
            const regime_value = payload.regime ?? payload.recommended;
            if (typeof regime_value !== 'string' || !BACKEND_REGIME_TYPES.has(regime_value)) {
                throw new BackendContractError(
                    'MALFORMED_BACKEND_PAYLOAD',
                    'REGIME_RECOMMENDED payload is invalid',
                );
            }
            const intents: Array<UiApplicationIntent> = [{
                type: 'REGIME_RECOMMENDED',
                regime: regime_value as RegimeType,
            }];
            if (payload.indicator !== undefined && payload.indicator !== null) {
                intents.push({
                    type: 'REGIME_INDICATORS_UPDATED',
                    metrics: map_indicator_metrics(validate_indicator_snapshot(payload.indicator)),
                });
            }
            return intents;
        }
        case 'REGIME_APPLIED':
        case 'REGIME_SELECTED': {
            const regime_value = payload.regime ?? payload.selected;
            if (typeof regime_value !== 'string' || !BACKEND_REGIME_TYPES.has(regime_value)) {
                throw new BackendContractError(
                    'MALFORMED_BACKEND_PAYLOAD',
                    'REGIME_APPLIED payload is invalid',
                );
            }
            return [{ type: 'REGIME_APPLIED', regime: regime_value as RegimeType }];
        }
        case 'PERFORMANCE_UPDATED': {
            const performance = validate_performance_snapshot(
                payload.performance ?? payload,
            );
            const summary = map_performance_summary(performance);
            return [{
                type: 'TRADE_HISTORY_PERFORMANCE_UPDATED',
                daily_return: summary.dailyReturn,
                sell_performance: summary.sellPerformance,
                fees: summary.fees,
            }];
        }
        case 'ORDER_EXECUTED': {
            const trade = validate_trade_snapshot(payload.trade ?? payload);
            const mapped_trade = map_trade_record(trade);
            return [{
                type: mapped_trade.side === 'buy'
                    ? 'BUY_ORDER_EXECUTED'
                    : 'SELL_ORDER_EXECUTED',
                trade: mapped_trade,
            }];
        }
        default:
            return [];
    }
}

/**
 * 함수 이름: is_backend_regime_type()
 * 기능: 외부 command 입력을 generated canonical REGIME union으로 좁힌다.
 * 인자: value -> 검증할 문자열
 * 반환값: generated REGIME type 여부
 * 작성 날짜: 2026/08/21
 */
export function is_backend_regime_type(value: string): value is BackendRegimeType {
    return BACKEND_REGIME_TYPES.has(value);
}
