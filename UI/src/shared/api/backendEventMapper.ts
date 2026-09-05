import type {
    BackendAccountSnapshot,
    BackendDailyLossScope,
    BackendDecimalString,
    BackendEventEnvelope,
    BackendHttpEnvelope,
    BackendIndicatorSnapshot,
    BackendManualKillBehavior,
    BackendPerformanceSnapshot,
    BackendRegimeType,
    BackendResyncRequired,
    BackendRiskBudgetSnapshot,
    BackendRiskBlockReason,
    BackendRiskPolicyAvailability,
    BackendSnapshot,
    BackendTradeSnapshot,
    BackendTradingStatus,
    BackendTradingSnapshot,
} from '../contracts';
import { BACKEND_SCHEMA_VERSION } from '../contracts';
import type {
    RegimeMetric,
    RegimeType,
    TradingLogicCoverage,
    TradeRecord,
} from '../contracts';
import {
    format_decimal_text,
    format_eth_quantity,
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
const CANONICAL_REGIME_TYPES: ReadonlyArray<RegimeType> = [
    'type0',
    'type1',
    'type2',
    'type3',
    'type4',
];
const BACKEND_EXECUTION_MODES: ReadonlySet<string> = new Set([
    'disabled',
    'fake',
    'testnet',
    'live',
]);
const BACKEND_TRADING_STATUSES: ReadonlySet<BackendTradingStatus> = new Set([
    'not_started',
    'running',
    'stopping',
    'reconciliation_required',
    'terminated',
]);
const BACKEND_RISK_POLICY_AVAILABILITIES: ReadonlySet<BackendRiskPolicyAvailability> = new Set([
    'CONFIGURED',
    'UNAVAILABLE',
]);
const BACKEND_DAILY_LOSS_SCOPES: ReadonlySet<BackendDailyLossScope> = new Set([
    'REALIZED_ONLY',
    'REALIZED_AND_UNREALIZED',
]);
const BACKEND_MANUAL_KILL_BEHAVIORS: ReadonlySet<BackendManualKillBehavior> = new Set([
    'BLOCK_NEW_ORDERS',
    'CANCEL_AND_LIQUIDATE',
]);
const BACKEND_RISK_BLOCK_REASONS: ReadonlySet<BackendRiskBlockReason> = new Set([
    'RISK_POLICY_UNAVAILABLE',
    'RISK_POLICY_VERSION_MISMATCH',
    'MANUAL_KILL_SWITCH_ACTIVE',
    'RISK_ORDER_NOTIONAL_EXCEEDED',
    'RISK_DAILY_LOSS_EXCEEDED',
    'RISK_POSITION_NOTIONAL_EXCEEDED',
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
 * 함수 이름: assert_nullable_safe_integer()
 * 기능: nullable policy version을 null 또는 지정 최솟값 이상의 안전한 정수로 제한한다.
 * 인자: value -> 검증할 JSON 값, field_name -> 오류 field 이름, minimum -> 허용 최소값
 * 반환값: 검증된 안전한 정수 또는 null
 * 작성 날짜: 2026/08/25
 */
function assert_nullable_safe_integer(
    value: unknown,
    field_name: string,
    minimum = 0,
): number | null {
    return value === null
        ? null
        : assert_safe_integer(value, field_name, minimum);
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
 * 함수 이름: assert_nullable_positive_decimal_string()
 * 기능: configured 위험 상한을 양의 plain Decimal 문자열 또는 명시적 무제한 null로 제한한다.
 * 인자: value -> 검증할 JSON 값, field_name -> 오류 field 이름
 * 반환값: 정밀도를 보존한 양의 Decimal 문자열 또는 null
 * 작성 날짜: 2026/08/29
 */
function assert_nullable_positive_decimal_string(
    value: unknown,
    field_name: string,
): BackendDecimalString | null {
    const decimal_text = assert_nullable_decimal_string(value, field_name);

    // JS number 변환 없이 문자열 문법만으로 음수와 모든 zero 표현을 거부한다.
    if (decimal_text !== null
        && !/^(?:0\.[0-9]*[1-9][0-9]*|[1-9][0-9]*(?:\.[0-9]+)?)$/u.test(decimal_text)) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            `${field_name} must be a positive decimal string or null`,
        );
    }

    return decimal_text;
}

/**
 * 함수 이름: assert_non_negative_decimal_string()
 * 기능: 노출·손실 wire 값을 음수가 아닌 plain Decimal 문자열로 제한한다.
 * 인자: value -> 검증할 JSON 값, field_name -> 오류 field 이름
 * 반환값: 정밀도가 보존된 0 이상 Decimal 문자열
 * 작성 날짜: 2026/08/29
 */
function assert_non_negative_decimal_string(
    value: unknown,
    field_name: string,
): BackendDecimalString {
    const decimal_text = assert_decimal_string(value, field_name);

    // 금융 금액의 음수 표기를 배제하되 소수 정밀도와 0 표기는 유지한다.
    if (!/^(?:0(?:\.[0-9]+)?|[1-9][0-9]*(?:\.[0-9]+)?)$/u.test(decimal_text)) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            `${field_name} must be a non-negative decimal string`,
        );
    }

    return decimal_text;
}

/**
 * 함수 이름: decimal_text_to_scaled_integer()
 * 기능: 음수가 아닌 Decimal 문자열을 정확한 공통 scale의 BigInt로 변환한다.
 * 인자: decimal_text -> 이미 검증된 Decimal 문자열, scale -> 맞출 소수 자릿수
 * 반환값: 반올림 없이 scale만 맞춘 BigInt
 * 작성 날짜: 2026/08/29
 */
function decimal_text_to_scaled_integer(
    decimal_text: BackendDecimalString,
    scale: number,
): bigint {
    const [integer_digits, fractional_digits = ''] = decimal_text.split('.');
    const scaled_digits = `${integer_digits}${fractional_digits.padEnd(scale, '0')}`;

    return BigInt(scaled_digits);  // JS number로 변환하지 않고 exact Decimal 합계를 검증한다.
}

/**
 * 함수 이름: validate_risk_budget_snapshot()
 * 기능: 마지막 BUY 위험 예산의 전체 Decimal·version·노출 합계 계약을 검증한다.
 * 인자: value -> risk budget JSON 또는 BUY 평가 전 null
 * 반환값: generated RiskBudgetSnapshot 또는 null
 * 작성 날짜: 2026/08/29
 */
function validate_risk_budget_snapshot(
    value: unknown,
): BackendRiskBudgetSnapshot | null {
    if (value === null) {
        return null;  // 평가 전을 모든 값 0인 예산으로 축약하지 않는다.
    }

    const budget = assert_record(value, 'trading.last_risk_budget');
    const expected_budget_fields: ReadonlySet<string> = new Set([
        'policy_version',
        'market_version',
        'account_version',
        'context_version',
        'current_position_notional',
        'reserved_buy_notional',
        'candidate_order_notional',
        'projected_position_notional',
        'daily_realized_pnl',
        'unrealized_pnl',
        'daily_loss',
        'manual_kill_active',
    ]);
    const received_budget_fields = Object.keys(budget);

    // Unknown field나 누락 field가 typed facade state에 숨지 못하게 exact DTO 표면을 고정한다.
    if (received_budget_fields.length !== expected_budget_fields.size
        || received_budget_fields.some((field_name) => {
            return !expected_budget_fields.has(field_name);
        })) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'trading.last_risk_budget fields are invalid',
        );
    }

    const policy_version = assert_nullable_safe_integer(
        budget.policy_version,
        'trading.last_risk_budget.policy_version',
        1,
    );
    const market_version = assert_safe_integer(
        budget.market_version,
        'trading.last_risk_budget.market_version',
    );
    const account_version = assert_safe_integer(
        budget.account_version,
        'trading.last_risk_budget.account_version',
    );
    const context_version = assert_safe_integer(
        budget.context_version,
        'trading.last_risk_budget.context_version',
    );

    // 노출 값은 각각 0 이상이고 projected가 세 구성 금액의 exact 합이어야 한다.
    const current_position_notional = assert_non_negative_decimal_string(
        budget.current_position_notional,
        'trading.last_risk_budget.current_position_notional',
    );
    const reserved_buy_notional = assert_non_negative_decimal_string(
        budget.reserved_buy_notional,
        'trading.last_risk_budget.reserved_buy_notional',
    );
    const candidate_order_notional = assert_non_negative_decimal_string(
        budget.candidate_order_notional,
        'trading.last_risk_budget.candidate_order_notional',
    );
    const projected_position_notional = assert_non_negative_decimal_string(
        budget.projected_position_notional,
        'trading.last_risk_budget.projected_position_notional',
    );
    const exposure_values = [
        current_position_notional,
        reserved_buy_notional,
        candidate_order_notional,
        projected_position_notional,
    ];
    const exposure_scale = Math.max(
        ...exposure_values.map((decimal_text) => decimal_text.split('.')[1]?.length ?? 0),
    );
    const expected_projected_notional = [
        current_position_notional,
        reserved_buy_notional,
        candidate_order_notional,
    ].reduce((sum, decimal_text) => {
        return sum + decimal_text_to_scaled_integer(decimal_text, exposure_scale);
    }, BigInt(0));
    if (expected_projected_notional
        !== decimal_text_to_scaled_integer(projected_position_notional, exposure_scale)) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'trading.last_risk_budget exposure total is inconsistent',
        );
    }

    // PnL은 이익·손실 부호를 모두 보존하고 daily loss만 0 이상으로 제한한다.
    const daily_realized_pnl = assert_decimal_string(
        budget.daily_realized_pnl,
        'trading.last_risk_budget.daily_realized_pnl',
    );
    const unrealized_pnl = assert_decimal_string(
        budget.unrealized_pnl,
        'trading.last_risk_budget.unrealized_pnl',
    );
    const daily_loss = assert_non_negative_decimal_string(
        budget.daily_loss,
        'trading.last_risk_budget.daily_loss',
    );
    const manual_kill_active = assert_boolean(
        budget.manual_kill_active,
        'trading.last_risk_budget.manual_kill_active',
    );

    return {
        policy_version,
        market_version,
        account_version,
        context_version,
        current_position_notional,
        reserved_buy_notional,
        candidate_order_notional,
        projected_position_notional,
        daily_realized_pnl,
        unrealized_pnl,
        daily_loss,
        manual_kill_active,
    };
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
 * 함수 이름: assert_unit_interval_ratio()
 * 기능: split ratio Decimal 문자열을 닫힌 구간 0~1로 검증한다.
 * 인자: value -> 검증할 JSON 값, field_name -> 오류 field 이름
 * 반환값: 검증된 Decimal ratio 문자열
 * 작성 날짜: 2026/08/21
 */
function assert_unit_interval_ratio(value: unknown, field_name: string): string {
    const ratio = assert_decimal_string(value, field_name);

    if (!/^(?:0(?:\.[0-9]+)?|1(?:\.0+)?)$/u.test(ratio)) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            `${field_name} must be between zero and one`,
        );
    }

    return ratio;
}

/**
 * 함수 이름: map_trading_status_presentation()
 * 기능: backend lifecycle status를 StrategyCard의 설명과 tone으로 변환한다.
 * 인자: status -> runtime validation이 끝난 trading status
 * 반환값: 상태 설명과 표시 tone
 * 작성 날짜: 2026/08/21
 */
function map_trading_status_presentation(status: BackendTradingStatus): {
    readonly label: string;
    readonly tone: 'positive' | 'neutral';
} {
    switch (status) {
        case 'running':
            return { label: '자동매매 실행 중', tone: 'positive' };
        case 'stopping':
            return { label: '자동매매 중지 처리 중', tone: 'neutral' };
        case 'reconciliation_required':
            return { label: '주문 상태 확인 필요', tone: 'neutral' };
        case 'terminated':
            return { label: '자동매매 종료', tone: 'neutral' };
        case 'not_started':
            return { label: '매매 시작 전', tone: 'neutral' };
    }
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
 * 함수 이름: validate_trading_logic_coverage()
 * 기능: REGIME별 TradingSTM coverage를 빠짐없는 canonical 5행과 일관된 guard로 검증한다.
 * 인자: value -> trading.logic_coverage JSON 값
 * 반환값: UI actor가 그대로 보존할 REGIME별 coverage 목록
 * 작성 날짜: 2026/08/21
 */
function validate_trading_logic_coverage(
    value: unknown,
): ReadonlyArray<TradingLogicCoverage> {
    if (!Array.isArray(value) || value.length !== CANONICAL_REGIME_TYPES.length) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'trading.logic_coverage must contain the canonical five regimes',
        );
    }

    // 배열 순서까지 canonical mapping으로 고정해 중복, 누락, 알 수 없는 REGIME을 함께 차단한다.
    return value.map((coverage_value, index) => {
        const coverage = assert_record(
            coverage_value,
            `trading.logic_coverage[${index}]`,
        );
        const expected_regime = CANONICAL_REGIME_TYPES[index]!;

        if (coverage.regime_type !== expected_regime) {
            throw new BackendContractError(
                'MALFORMED_BACKEND_PAYLOAD',
                'trading.logic_coverage regime order is invalid',
            );
        }
        if (coverage.support_status !== 'supported'
            && coverage.support_status !== 'unsupported') {
            throw new BackendContractError(
                'MALFORMED_BACKEND_PAYLOAD',
                'trading.logic_coverage support status is invalid',
            );
        }

        const expected_guard = coverage.support_status === 'supported'
            ? 'READY'
            : 'UNSUPPORTED_TRADING_LOGIC';

        if (coverage.start_guard !== expected_guard) {
            throw new BackendContractError(
                'MALFORMED_BACKEND_PAYLOAD',
                'trading.logic_coverage start guard is inconsistent',
            );
        }

        return {
            regime_type: expected_regime,
            support_status: coverage.support_status,
            start_guard: expected_guard,
        };
    });
}

/**
 * 함수 이름: validate_trading_snapshot()
 * 기능: snapshot과 TRADING_SESSION_UPDATED가 공유하는 Phase 7 lifecycle DTO를 검증한다.
 * 인자: value -> trading snapshot JSON 값
 * 반환값: generated authoritative trading snapshot
 * 작성 날짜: 2026/08/21
 */
function validate_trading_snapshot(value: unknown): BackendTradingSnapshot {
    const trading = assert_record(value, 'trading');

    if (typeof trading.mode !== 'string' || !BACKEND_EXECUTION_MODES.has(trading.mode)) {
        throw new BackendContractError('MALFORMED_BACKEND_PAYLOAD', 'trading.mode is invalid');
    }
    if (typeof trading.status !== 'string'
        || !BACKEND_TRADING_STATUSES.has(trading.status as BackendTradingStatus)) {
        throw new BackendContractError('MALFORMED_BACKEND_PAYLOAD', 'trading state is invalid');
    }
    assert_boolean(trading.command_enabled, 'trading.command_enabled');
    assert_unit_interval_ratio(trading.scale_in, 'trading.scale_in');
    assert_unit_interval_ratio(trading.scale_out, 'trading.scale_out');
    assert_boolean(trading.has_open_position, 'trading.has_open_position');
    if (trading.session_id !== null) {
        assert_uuid(trading.session_id, 'trading.session_id');
    }
    if ((trading.status === 'running'
            || trading.status === 'stopping'
            || trading.status === 'reconciliation_required')
        && trading.session_id === null) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'An active trading state requires a trading session ID',
        );
    }

    // Phase 13 risk publication은 임의 문자열이나 truthy 값이 UI 운영 상태를 가장하지 못하게 한다.
    if (typeof trading.risk_policy_availability !== 'string'
        || !BACKEND_RISK_POLICY_AVAILABILITIES.has(
            trading.risk_policy_availability as BackendRiskPolicyAvailability,
        )) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'trading.risk_policy_availability is invalid',
        );
    }
    const configured_risk_policy_version = assert_nullable_safe_integer(
        trading.configured_risk_policy_version,
        'trading.configured_risk_policy_version',
        1,
    );
    const max_order_notional = assert_nullable_positive_decimal_string(
        trading.max_order_notional,
        'trading.max_order_notional',
    );
    const max_position_notional = assert_nullable_positive_decimal_string(
        trading.max_position_notional,
        'trading.max_position_notional',
    );
    const max_daily_loss = assert_nullable_positive_decimal_string(
        trading.max_daily_loss,
        'trading.max_daily_loss',
    );
    const daily_loss_scope = trading.daily_loss_scope === null
        ? null
        : assert_string(trading.daily_loss_scope, 'trading.daily_loss_scope');
    const manual_kill_behavior = trading.manual_kill_behavior === null
        ? null
        : assert_string(trading.manual_kill_behavior, 'trading.manual_kill_behavior');
    assert_nullable_safe_integer(
        trading.session_risk_policy_version,
        'trading.session_risk_policy_version',
        1,
    );
    assert_safe_integer(trading.risk_control_version, 'trading.risk_control_version');
    const manual_kill_active = assert_boolean(
        trading.manual_kill_active,
        'trading.manual_kill_active',
    );
    const manual_kill_cleanup_complete = assert_boolean(
        trading.manual_kill_cleanup_complete,
        'trading.manual_kill_cleanup_complete',
    );
    const manual_kill_activation_behavior = (
        trading.manual_kill_activation_behavior === null
            ? null
            : assert_string(
                trading.manual_kill_activation_behavior,
                'trading.manual_kill_activation_behavior',
            )
    );
    const manual_kill_activation_policy_version = assert_nullable_safe_integer(
        trading.manual_kill_activation_policy_version,
        'trading.manual_kill_activation_policy_version',
        1,
    );
    const last_risk_decision_allowed = trading.last_risk_decision_allowed === null
        ? null
        : assert_boolean(
            trading.last_risk_decision_allowed,
            'trading.last_risk_decision_allowed',
        );
    const last_risk_budget = validate_risk_budget_snapshot(
        trading.last_risk_budget,
    );
    const risk_block_reason = trading.risk_block_reason === null
        ? null
        : assert_string(trading.risk_block_reason, 'trading.risk_block_reason');
    assert_boolean(
        trading.process_ownership_ambiguous,
        'trading.process_ownership_ambiguous',
    );

    // Availability와 policy version은 하나의 authoritative 상태이므로 모순된 조합을 거부한다.
    const policy_is_configured = trading.risk_policy_availability === 'CONFIGURED';
    if (policy_is_configured !== (configured_risk_policy_version !== null)) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'trading risk policy availability and version are inconsistent',
        );
    }
    if (daily_loss_scope !== null
        && !BACKEND_DAILY_LOSS_SCOPES.has(daily_loss_scope as BackendDailyLossScope)) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'trading.daily_loss_scope is invalid',
        );
    }
    if (manual_kill_behavior !== null
        && !BACKEND_MANUAL_KILL_BEHAVIORS.has(
            manual_kill_behavior as BackendManualKillBehavior,
        )) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'trading.manual_kill_behavior is invalid',
        );
    }
    if (manual_kill_activation_behavior !== null
        && !BACKEND_MANUAL_KILL_BEHAVIORS.has(
            manual_kill_activation_behavior as BackendManualKillBehavior,
        )) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'trading.manual_kill_activation_behavior is invalid',
        );
    }

    // 활성 epoch provenance는 behavior/version을 한 쌍으로 보존하고 inactive 상태에는 남기지 않는다.
    const activation_provenance_is_complete = (
        (manual_kill_activation_behavior === null)
        === (manual_kill_activation_policy_version === null)
    );
    if (!activation_provenance_is_complete
        || (!manual_kill_active && manual_kill_activation_behavior !== null)
        || (!manual_kill_active && !manual_kill_cleanup_complete)
        || (!manual_kill_cleanup_complete
            && manual_kill_activation_behavior !== 'CANCEL_AND_LIQUIDATE')) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'trading manual-kill activation provenance is inconsistent',
        );
    }

    // Unavailable은 정책 자체의 부재이고 configured만 scope·behavior와 nullable 상한을 소유한다.
    if ((policy_is_configured && (daily_loss_scope === null || manual_kill_behavior === null))
        || (!policy_is_configured && (
            max_order_notional !== null
            || max_position_notional !== null
            || max_daily_loss !== null
            || daily_loss_scope !== null
            || manual_kill_behavior !== null
        ))) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'trading risk policy details are inconsistent with availability',
        );
    }
    if (risk_block_reason !== null
        && !BACKEND_RISK_BLOCK_REASONS.has(risk_block_reason as BackendRiskBlockReason)) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'trading.risk_block_reason is invalid',
        );
    }

    // 허용 판정은 사유가 없어야 하고 차단 판정은 반드시 allowlist 사유를 가져야 한다.
    if ((last_risk_decision_allowed === null && risk_block_reason !== null)
        || (last_risk_decision_allowed === true && risk_block_reason !== null)
        || (last_risk_decision_allowed === false && risk_block_reason === null)) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'trading risk decision and block reason are inconsistent',
        );
    }
    if ((last_risk_decision_allowed === null) !== (last_risk_budget === null)) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'trading risk decision and budget are inconsistent',
        );
    }
    validate_trading_logic_coverage(trading.logic_coverage);
    assert_safe_integer(trading.version, 'trading.version');

    return value as BackendTradingSnapshot;
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
    const trading = validate_trading_snapshot(snapshot.trading);
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

    // 시세 tick은 시장 version만 전진시키며 REGIME은 마지막 4H 평가의 provenance를 유지한다.
    // 같은 version의 가격 불일치와 아직 존재하지 않는 미래 시장 참조만 거부한다.
    if (indicator.source_market_version > market_version
        || (indicator.source_market_version === market_version
            && indicator.current_price !== market_current_price)) {
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
        details,
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
    readonly details: Readonly<Record<string, unknown>>;

    /**
     * 함수 이름: BackendCommandError.constructor()
     * 기능: typed backend failure의 안전한 code, message와 retry 가능 여부를 보존한다.
     * 인자: code -> backend failure code
     *      message -> backend가 보장한 user-safe message
     *      retryable -> 동일 command를 재시도할 수 있는지 여부
     *      details -> endpoint별로 검증하기 전인 secret 없는 구조화 세부 정보
     * 반환값: BackendCommandError 인스턴스
     * 작성 날짜: 2026/08/21
     */
    constructor(
        code: string,
        message: string,
        retryable: boolean,
        details: Readonly<Record<string, unknown>> = {},
    ) {
        super(message);
        this.name = 'BackendCommandError';
        this.code = code;
        this.retryable = retryable;
        this.details = details;
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
            : format_eth_quantity(eth_balance.total),
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
 * 함수 이름: map_trade_history_summary()
 * 기능: Performance와 authoritative ETH 보유량을 D-12 history summary에 투영한다.
 * 인자: performance -> validated backend Performance
 *      holdings -> Account 또는 상세 조회가 제공한 ETH Decimal 문자열, 없으면 unavailable
 * 반환값: 계산되지 않은 체결총액/slippage가 unavailable인 summary
 * 작성 날짜: 2026/08/23
 */
export function map_trade_history_summary(
    performance: BackendPerformanceSnapshot,
    holdings: string | null = null,
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
            quantity: holdings === null
                ? '-'
                : `${format_eth_quantity(holdings)} ETH`,
        },
        fees: {
            amount: format_quote_amount(performance.daily_fee, 'USDT'),
            totalExecutedAmount: '-',
            averageSlippage: '-',
        },
    };
}

/**
 * 함수 이름: get_account_eth_holdings()
 * 기능: validated Account에서 D-12 현재 ETH 보유량을 찾아 Decimal 문자열로 반환한다.
 * 인자: account -> validated backend Account
 * 반환값: ETH total Decimal 또는 balance가 없을 때 Account 계약의 0
 * 작성 날짜: 2026/08/23
 */
function get_account_eth_holdings(account: BackendAccountSnapshot): string {
    const eth_balance = account.balances.find((balance) => {
        return balance.asset === SUPPORTED_BASE_ASSET;
    });

    return eth_balance?.total ?? '0';
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
    const trading = snapshot.trading;
    const regime_metrics = map_indicator_metrics(snapshot.regime.indicator);
    const logic_coverage = validate_trading_logic_coverage(snapshot.trading.logic_coverage);
    const recent_trades = snapshot.recent_trades.map(map_trade_record);
    const account_asset = map_account_asset(snapshot.account);
    const trade_history_summary = map_trade_history_summary(
        snapshot.performance,
        get_account_eth_holdings(snapshot.account),
    );
    const trading_presentation = map_trading_status_presentation(trading.status);
    const account_strategy = {
        appliedState: trading.status,
        // Performance 누적값은 현재 REGIME 상태별 성과가 아니므로 StrategyCard에는 투영하지 않는다.
        profitAmount: '-',
        profitRate: '-',
        status: trading_presentation.label,
        statusTone: trading_presentation.tone,
    };
    const is_trading = trading.status !== 'not_started' && trading.status !== 'terminated';
    const server_snapshot: UiServerOwnedSnapshot = {
        last_sequence: snapshot.last_sequence,
        trading_version: trading.version,
        trading_session_id: trading.session_id,
        trading_symbol: snapshot.market.symbol === 'ETHUSDT'
            ? 'ETH/USDT'
            : snapshot.market.symbol,
        recommended_regime: snapshot.regime.recommended,
        applied_regime: snapshot.regime.selected,
        regime_metrics,
        logic_coverage,
        command_enabled: trading.command_enabled,
        risk_policy_availability: trading.risk_policy_availability,
        configured_risk_policy_version: trading.configured_risk_policy_version,
        max_order_notional: trading.max_order_notional,
        max_position_notional: trading.max_position_notional,
        max_daily_loss: trading.max_daily_loss,
        daily_loss_scope: trading.daily_loss_scope,
        manual_kill_behavior: trading.manual_kill_behavior,
        session_risk_policy_version: trading.session_risk_policy_version,
        risk_control_version: trading.risk_control_version,
        manual_kill_active: trading.manual_kill_active,
        manual_kill_cleanup_complete: trading.manual_kill_cleanup_complete,
        manual_kill_activation_behavior: trading.manual_kill_activation_behavior,
        manual_kill_activation_policy_version:
            trading.manual_kill_activation_policy_version,
        last_risk_decision_allowed: trading.last_risk_decision_allowed,
        last_risk_budget: trading.last_risk_budget,
        risk_block_reason: trading.risk_block_reason,
        process_ownership_ambiguous: trading.process_ownership_ambiguous,
        recent_trades,
        account_strategy,
        account_asset,
        trade_history_summary,
        scale_in_percentage: Number(trading.scale_in) * 100,
        scale_out_percentage: Number(trading.scale_out) * 100,
        is_trading,
        has_open_position: trading.has_open_position,
        trading_state_label: trading.status,
    };

    return {
        facade_options: {
            today,
            trading_symbol: server_snapshot.trading_symbol,
            recommended_regime: server_snapshot.recommended_regime,
            applied_regime: server_snapshot.applied_regime,
            regime_metrics: server_snapshot.regime_metrics,
            logic_coverage: server_snapshot.logic_coverage,
            command_enabled: server_snapshot.command_enabled,
            risk_policy_availability: server_snapshot.risk_policy_availability,
            configured_risk_policy_version: server_snapshot.configured_risk_policy_version,
            max_order_notional: server_snapshot.max_order_notional,
            max_position_notional: server_snapshot.max_position_notional,
            max_daily_loss: server_snapshot.max_daily_loss,
            daily_loss_scope: server_snapshot.daily_loss_scope,
            manual_kill_behavior: server_snapshot.manual_kill_behavior,
            session_risk_policy_version: server_snapshot.session_risk_policy_version,
            risk_control_version: server_snapshot.risk_control_version,
            manual_kill_active: server_snapshot.manual_kill_active,
            manual_kill_cleanup_complete:
                server_snapshot.manual_kill_cleanup_complete,
            manual_kill_activation_behavior:
                server_snapshot.manual_kill_activation_behavior,
            manual_kill_activation_policy_version:
                server_snapshot.manual_kill_activation_policy_version,
            last_risk_decision_allowed: server_snapshot.last_risk_decision_allowed,
            last_risk_budget: server_snapshot.last_risk_budget,
            risk_block_reason: server_snapshot.risk_block_reason,
            process_ownership_ambiguous: server_snapshot.process_ownership_ambiguous,
            recent_trades: server_snapshot.recent_trades,
            account_strategy: server_snapshot.account_strategy,
            account_asset: server_snapshot.account_asset,
            trade_history_summary: server_snapshot.trade_history_summary,
            scale_in_percentage: server_snapshot.scale_in_percentage,
            scale_out_percentage: server_snapshot.scale_out_percentage,
            is_trading: server_snapshot.is_trading,
            has_open_position: server_snapshot.has_open_position,
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
            if (event.aggregate_version !== account.version) {
                throw new BackendContractError(
                    'MALFORMED_BACKEND_PAYLOAD',
                    'Account event aggregate version does not match its payload',
                );
            }
            const holdings = get_account_eth_holdings(account);

            // 같은 account version에서 dashboard 자산과 상세 ETH 보유량을 함께 갱신한다.
            return [
                {
                    type: 'ACCOUNT_ASSETS_UPDATED',
                    asset: map_account_asset(account),
                },
                {
                    type: 'TRADE_HISTORY_HOLDINGS_UPDATED',
                    position: {
                        quantity: `${format_eth_quantity(holdings)} ETH`,
                    },
                },
            ];
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
        case 'REGIME_APPLIED': {
            const regime_value = payload.regime ?? payload.selected;
            if (typeof regime_value !== 'string' || !BACKEND_REGIME_TYPES.has(regime_value)) {
                throw new BackendContractError(
                    'MALFORMED_BACKEND_PAYLOAD',
                    'REGIME_APPLIED payload is invalid',
                );
            }
            return [{ type: 'REGIME_APPLIED', regime: regime_value as RegimeType }];
        }
        case 'REGIME_SELECTED': {
            const selected = payload.selected;
            if (typeof selected !== 'string' || !BACKEND_REGIME_TYPES.has(selected)) {
                throw new BackendContractError(
                    'MALFORMED_BACKEND_PAYLOAD',
                    'REGIME_SELECTED selection is invalid',
                );
            }
            // Phase 6 registry coverage와 event support 상태를 교차 검증해 fallback을 차단한다.
            const expected_support_status = selected === 'type0'
                ? 'supported'
                : 'unsupported';
            if (payload.support_status !== expected_support_status) {
                throw new BackendContractError(
                    'MALFORMED_BACKEND_PAYLOAD',
                    'REGIME_SELECTED support status is inconsistent',
                );
            }
            const version = assert_safe_integer(payload.version, 'REGIME_SELECTED.version');
            if (event.aggregate_version !== version) {
                throw new BackendContractError(
                    'MALFORMED_BACKEND_PAYLOAD',
                    'REGIME_SELECTED aggregate version does not match its payload',
                );
            }

            return [{
                type: 'REGIME_SELECTION_SYNCHRONIZED',
                selected: selected as RegimeType,
                support_status: expected_support_status,
                version,
            }];
        }
        case 'TRADING_SESSION_UPDATED': {
            const trading = validate_trading_snapshot(payload.trading);
            const presentation = map_trading_status_presentation(trading.status);

            // Envelope와 payload version을 함께 확인해 다른 Context 상태를 같은 event로 섞지 못하게 한다.
            if (event.aggregate_version !== trading.version) {
                throw new BackendContractError(
                    'MALFORMED_BACKEND_PAYLOAD',
                    'Trading event aggregate version does not match its payload',
                );
            }

            return [{
                type: 'TRADING_SESSION_SYNCHRONIZED',
                status: trading.status,
                version: trading.version,
                session_id: trading.session_id,
                command_enabled: trading.command_enabled,
                risk_policy_availability: trading.risk_policy_availability,
                configured_risk_policy_version: trading.configured_risk_policy_version,
                max_order_notional: trading.max_order_notional,
                max_position_notional: trading.max_position_notional,
                max_daily_loss: trading.max_daily_loss,
                daily_loss_scope: trading.daily_loss_scope,
                manual_kill_behavior: trading.manual_kill_behavior,
                session_risk_policy_version: trading.session_risk_policy_version,
                risk_control_version: trading.risk_control_version,
                manual_kill_active: trading.manual_kill_active,
                manual_kill_cleanup_complete:
                    trading.manual_kill_cleanup_complete,
                manual_kill_activation_behavior:
                    trading.manual_kill_activation_behavior,
                manual_kill_activation_policy_version:
                    trading.manual_kill_activation_policy_version,
                last_risk_decision_allowed: trading.last_risk_decision_allowed,
                last_risk_budget: trading.last_risk_budget,
                risk_block_reason: trading.risk_block_reason,
                process_ownership_ambiguous: trading.process_ownership_ambiguous,
                scale_in: trading.scale_in,
                scale_out: trading.scale_out,
                scale_in_percentage: Number(trading.scale_in) * 100,
                scale_out_percentage: Number(trading.scale_out) * 100,
                has_open_position: trading.has_open_position,
                logic_coverage: validate_trading_logic_coverage(trading.logic_coverage),
                strategy_status: presentation.label,
                strategy_status_tone: presentation.tone,
            }];
        }
        case 'PERFORMANCE_UPDATED': {
            const performance = validate_performance_snapshot(
                payload.performance ?? payload,
            );
            const summary = map_trade_history_summary(performance);
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
