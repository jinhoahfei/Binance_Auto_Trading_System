import { invoke } from '@tauri-apps/api/core';

import type {
    BackendAuthenticateMessage,
    BackendBinanceConnectionStatus,
    BackendCsvExportRequest,
    BackendEventEnvelope,
    BackendSnapshot,
    BackendShutdownState,
    BackendTradeDetails,
    BackendTradeDetailsQuery,
    BackendTradeDetailsSummary,
    BackendTradingLogicSupportStatus,
    BackendTradingStatus,
} from '../contracts';
import { BACKEND_MAX_TRADE_PAGE_SIZE, BACKEND_SCHEMA_VERSION } from '../contracts';
import type {
    CsvExportOptions,
    CsvExportReceipt,
    RegimeType,
    TradeHistoryDetails,
    TradeHistoryQuery,
} from '../contracts';
import type { UiApplicationIntent } from '../../app/control';
import type { TradingCommandReceipt, UiCommandPort } from '../ports';
import {
    BackendCommandError,
    BackendContractError,
    decode_backend_http_envelope,
    map_backend_event_to_intents,
    map_trade_history_summary,
    map_trade_record,
    parse_backend_web_socket_message,
    validate_backend_snapshot,
    validate_performance_snapshot,
    validate_trade_snapshot,
} from './backendEventMapper';
import { validate_binance_connection_status } from './binanceConnectionStatus';

const CANONICAL_UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
const MAX_TRACKED_EVENT_IDS = 10_000;
const MAX_PENDING_IDEMPOTENCY_KEYS = 128;
const NORMAL_CLIENT_CLOSE_CODE = 1000;
const DEFAULT_REQUEST_TIMEOUT_MS = 5_000;
const DEFAULT_SHUTDOWN_WAIT_TIMEOUT_MS = 30_000;
const DEFAULT_SHUTDOWN_POLL_INTERVAL_MS = 250;
const ADAPTER_STOP_ABORT_REASON = Symbol('ADAPTER_STOP_ABORT_REASON');
const CALLER_REQUEST_ABORT_REASON = Symbol('CALLER_REQUEST_ABORT_REASON');
const REQUEST_TIMEOUT_ABORT_REASON = Symbol('REQUEST_TIMEOUT_ABORT_REASON');
const UNIT_INTERVAL_RATIO_PATTERN = /^(?:0(?:\.[0-9]+)?|1(?:\.0+)?)$/u;
const NON_NEGATIVE_DECIMAL_PATTERN = /^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/u;
const LOCAL_DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/u;
const BACKEND_TRADING_STATUSES: ReadonlySet<BackendTradingStatus> = new Set([
    'not_started',
    'running',
    'stopping',
    'reconciliation_required',
    'terminated',
]);
const BACKEND_REGIME_TYPES: ReadonlySet<RegimeType> = new Set([
    'type0',
    'type1',
    'type2',
    'type3',
    'type4',
]);
const NATIVE_SIDECAR_EXIT_FAILURE_CODES: ReadonlySet<string> = new Set([
    'BACKEND_SIDECAR_EXITED_UNEXPECTEDLY',
    'BACKEND_SIDECAR_EXIT_FAILED',
    'BACKEND_SIDECAR_STATE_UNAVAILABLE',
    'BACKEND_SIDECAR_UNAVAILABLE',
    'INVALID_BACKEND_SIDECAR_EXIT_TIMEOUT',
]);

/**
 * Phase 7 command가 성공할 때 adapter의 optimistic version을 전진시키는 최소 응답이다.
 */
interface BackendVersionedCommandResult {
    readonly version: number;
    readonly [key: string]: unknown;
}

/**
 * 분할 비율 command가 두 방향의 authoritative 값을 함께 돌려주는 응답이다.
 */
interface BackendSplitRatioCommandResult extends BackendVersionedCommandResult {
    readonly scale_in: string;
    readonly scale_out: string;
}

/**
 * start/stop 성공 응답에서 UI actor가 기다릴 lifecycle 상태를 결정하는 결과이다.
 */
interface BackendTradingCommandResult extends BackendVersionedCommandResult {
    readonly status: BackendTradingStatus;
    readonly session_id: string | null;
}

/**
 * Phase 12 shutdown endpoint가 안전 종료 수락 시 반환하는 strict receipt이다.
 */
interface BackendShutdownReceipt extends BackendVersionedCommandResult {
    readonly accepted: true;
    readonly status: 'accepted';
}

/**
 * REGIME selection 응답에서 적용값과 coverage 상태를 version과 함께 보존한다.
 */
interface BackendRegimeCommandResult extends BackendVersionedCommandResult {
    readonly selected: RegimeType;
    readonly support_status: BackendTradingLogicSupportStatus;
}

/**
 * Tauri launch boundary가 live renderer에 한 번 전달하는 secret-bearing descriptor이다.
 */
export interface BackendConnectionDescriptor {
    readonly port: number;
    readonly session_id: string;
    readonly schema_version: number;
    readonly token: string;
}

/**
 * browser WebSocket과 test double이 공유하는 최소 event-stream 계약이다.
 */
export interface BackendWebSocket {
    onopen: ((event: Event) => void) | null;
    onmessage: ((event: MessageEvent<unknown>) => void) | null;
    onclose: ((event: CloseEvent) => void) | null;
    onerror: ((event: Event) => void) | null;
    send(data: string): void;
    close(code?: number, reason?: string): void;
}

/**
 * transport와 native picker 의존성을 browser global 대신 test에서 결정적으로 주입하는 옵션이다.
 */
export interface BackendUiAdapterDependencies {
    readonly fetch?: typeof fetch;
    readonly create_web_socket?: (url: string) => BackendWebSocket;
    readonly create_uuid?: () => string;
    readonly pick_csv_directory?: () => Promise<unknown>;
    readonly request_timeout_ms?: number;
    readonly shutdown_wait_timeout_ms?: number;
    readonly shutdown_poll_interval_ms?: number;
    readonly wait_for_sidecar_exit?: () => Promise<unknown>;
}

/**
 * event stream lifecycle에서 facade bootstrap이 받아야 하는 안전한 callback 묶음이다.
 */
export interface BackendUiAdapterCallbacks {
    on_event(intents: ReadonlyArray<UiApplicationIntent>, event: BackendEventEnvelope): void;
    on_full_resync(snapshot: BackendSnapshot): void;
    on_reconnecting(reason: string): void;
    on_failure(error: BackendAdapterError): void;
}

/**
 * 클래스 이름: BackendAdapterError
 * 기능: raw response, URL query와 token을 포함하지 않는 live adapter 실패를 표현한다.
 * 작성 날짜: 2026/08/21
 */
export class BackendAdapterError extends Error {
    readonly code: string;
    readonly retryable: boolean;

    /**
     * 함수 이름: BackendAdapterError.constructor()
     * 기능: 사용자 경계에 전달할 정적 code, 안전한 message와 retry 여부를 보존한다.
     * 인자: code -> adapter failure code
     *      message -> secret이 없는 정적 설명
     *      retryable -> 새 snapshot 연결을 다시 시도할 수 있는지 여부
     * 반환값: BackendAdapterError 인스턴스
     * 작성 날짜: 2026/08/21
     */
    constructor(code: string, message: string, retryable: boolean) {
        super(message);
        this.name = 'BackendAdapterError';
        this.code = code;
        this.retryable = retryable;
    }
}

/**
 * 함수 이름: create_default_web_socket()
 * 기능: 인증 정보 없는 exact loopback URL로 browser WebSocket을 생성한다.
 * 인자: url -> query와 subprotocol이 없는 event endpoint URL
 * 반환값: 생성된 browser WebSocket
 * 작성 날짜: 2026/08/21
 */
function create_default_web_socket(url: string): BackendWebSocket {
    return new WebSocket(url);
}

/**
 * 함수 이름: invoke_default_csv_directory_picker()
 * 기능: Tauri native command를 호출해 OS 폴더 선택 결과를 가져온다.
 * 인자: 없음
 * 반환값: native command가 반환한 미검증 값 Promise
 * 작성 날짜: 2026/08/23
 */
async function invoke_default_csv_directory_picker(): Promise<unknown> {
    return invoke<unknown>('choose_csv_export_directory');
}

/**
 * 함수 이름: invoke_default_sidecar_exit_waiter()
 * 기능: shutdown 202 수락 뒤 native owner가 관찰한 sidecar의 정상 프로세스 종료를 기다린다.
 * 인자: 없음
 * 반환값: secret을 포함하지 않는 native exit receipt Promise
 * 작성 날짜: 2026/08/24
 */
async function invoke_default_sidecar_exit_waiter(): Promise<unknown> {
    return invoke<unknown>('await_backend_sidecar_exit');
}

/**
 * 함수 이름: create_default_uuid()
 * 기능: request와 idempotency header에 사용할 CSPRNG UUID를 생성한다.
 * 인자: 없음
 * 반환값: canonical UUID 문자열
 * 작성 날짜: 2026/08/21
 */
function create_default_uuid(): string {
    return globalThis.crypto.randomUUID();
}

/**
 * 함수 이름: validate_connection_descriptor()
 * 기능: native injection 값을 사용하기 전에 loopback port, schema, session과 token shape를 검증한다.
 * 인자: descriptor -> Tauri launch boundary에서 받은 unknown 값
 * 반환값: 검증된 backend connection descriptor
 * 작성 날짜: 2026/08/21
 */
export function validate_connection_descriptor(
    descriptor: unknown,
): BackendConnectionDescriptor {
    if (typeof descriptor !== 'object' || descriptor === null || Array.isArray(descriptor)) {
        throw new BackendAdapterError(
            'INVALID_CONNECTION_DESCRIPTOR',
            'Backend connection descriptor is invalid',
            false,
        );
    }

    const candidate = descriptor as Record<string, unknown>;
    const is_valid_port = Number.isSafeInteger(candidate.port)
        && (candidate.port as number) >= 1
        && (candidate.port as number) <= 65_535;
    const is_valid_session = typeof candidate.session_id === 'string'
        && CANONICAL_UUID_PATTERN.test(candidate.session_id);
    const is_valid_token = typeof candidate.token === 'string'
        && /^[A-Za-z0-9_-]{43}$/u.test(candidate.token);

    if (!is_valid_port
        || !is_valid_session
        || candidate.schema_version !== BACKEND_SCHEMA_VERSION
        || !is_valid_token) {
        throw new BackendAdapterError(
            candidate.schema_version === BACKEND_SCHEMA_VERSION
                ? 'INVALID_CONNECTION_DESCRIPTOR'
                : 'UNSUPPORTED_SCHEMA_VERSION',
            'Backend connection descriptor is invalid',
            false,
        );
    }

    return descriptor as BackendConnectionDescriptor;
}

/**
 * 함수 이름: validate_success_object()
 * 기능: 현재 Phase에서 구체 success DTO가 없는 command 응답을 최소 JSON object로 제한한다.
 * 인자: value -> command envelope의 data
 * 반환값: 검증된 읽기 전용 object
 * 작성 날짜: 2026/08/21
 */
function validate_success_object(value: unknown): Readonly<Record<string, unknown>> {
    if (typeof value !== 'object' || value === null || Array.isArray(value)) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'Backend command data must be an object',
        );
    }

    return value as Readonly<Record<string, unknown>>;
}

/**
 * 함수 이름: validate_versioned_command_result()
 * 기능: Phase 7 command 성공값이 다음 optimistic concurrency version을 포함하는지 검증한다.
 * 인자: value -> command envelope의 data
 * 반환값: 안전한 version을 가진 command 결과
 * 작성 날짜: 2026/08/21
 */
function validate_versioned_command_result(
    value: unknown,
): BackendVersionedCommandResult {
    const result = validate_success_object(value);

    if (!Number.isSafeInteger(result.version) || (result.version as number) < 0) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'Backend command version must be a non-negative safe integer',
        );
    }

    return result as BackendVersionedCommandResult;
}

/**
 * 함수 이름: validate_split_ratio_command_result()
 * 기능: split-ratio 성공값의 version과 두 authoritative Decimal 비율을 함께 검증한다.
 * 인자: value -> split-ratio command envelope의 data
 * 반환값: 검증된 version과 scale-in/out 비율
 * 작성 날짜: 2026/08/21
 */
function validate_split_ratio_command_result(
    value: unknown,
): BackendSplitRatioCommandResult {
    const result = validate_versioned_command_result(value);

    if (typeof result.scale_in !== 'string'
        || !UNIT_INTERVAL_RATIO_PATTERN.test(result.scale_in)
        || typeof result.scale_out !== 'string'
        || !UNIT_INTERVAL_RATIO_PATTERN.test(result.scale_out)) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'Backend split ratios must be Decimal strings between zero and one',
        );
    }

    return result as BackendSplitRatioCommandResult;
}

/**
 * 함수 이름: validate_trading_command_result()
 * 기능: lifecycle command 성공값의 status, session ID와 version을 함께 검증한다.
 * 인자: value -> start/stop command envelope의 data
 * 반환값: 검증된 authoritative lifecycle 결과
 * 작성 날짜: 2026/08/21
 */
function validate_trading_command_result(value: unknown): BackendTradingCommandResult {
    const result = validate_versioned_command_result(value);

    if (typeof result.status !== 'string'
        || !BACKEND_TRADING_STATUSES.has(result.status as BackendTradingStatus)) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'Backend trading command status is invalid',
        );
    }
    if (result.session_id !== null
        && (typeof result.session_id !== 'string'
            || !CANONICAL_UUID_PATTERN.test(result.session_id))) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'Backend trading command session ID is invalid',
        );
    }
    if ((result.status === 'running'
            || result.status === 'stopping'
            || result.status === 'reconciliation_required')
        && result.session_id === null) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'An active trading command result requires a session ID',
        );
    }

    return result as BackendTradingCommandResult;
}

/**
 * 함수 이름: validate_shutdown_receipt()
 * 기능: shutdown 202 data가 accepted/status/version exact 계약을 따르는지 검증한다.
 * 인자: value -> shutdown success envelope의 data
 * 반환값: 검증된 안전 종료 수락 receipt
 * 작성 날짜: 2026/08/24
 */
function validate_shutdown_receipt(value: unknown): BackendShutdownReceipt {
    const receipt = require_exact_record(
        value,
        ['accepted', 'status', 'version'],
        'shutdown receipt',
    );

    if (receipt.accepted !== true
        || receipt.status !== 'accepted'
        || !Number.isSafeInteger(receipt.version)
        || (receipt.version as number) < 0) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'Backend shutdown receipt is invalid',
        );
    }

    return receipt as unknown as BackendShutdownReceipt;
}

/**
 * 함수 이름: validate_sidecar_exit_receipt()
 * 기능: native owner가 보고한 exact exit receipt와 정상 exit code를 검증한다.
 * 인자: value -> Tauri await command의 미검증 반환값
 * 반환값: 정상 종료이면 void, 비정상 또는 malformed 결과이면 typed 오류 발생
 * 작성 날짜: 2026/08/24
 */
function validate_sidecar_exit_receipt(value: unknown): void {
    const receipt = require_exact_record(
        value,
        ['exited', 'code'],
        'native sidecar exit receipt',
    );

    if (receipt.exited !== true || receipt.code !== 0) {
        throw new BackendAdapterError(
            'SIDECAR_ABNORMAL_EXIT',
            '백엔드 프로세스가 정상 종료되지 않았습니다. 창을 유지한 채 상태를 확인해 주세요.',
            true,
        );
    }
}

/**
 * 함수 이름: validate_shutdown_state()
 * 기능: 복구 종료에 필요한 최소 상태와 현재 native launch session을 검증한다.
 * 인자: value -> 미검증 종료 상태, session_id -> 현재 연결 descriptor의 session
 * 반환값: 검증된 종료 명령 기준값
 * 작성 날짜: 2026/09/05
 */
function validate_shutdown_state(value: unknown, session_id: string): BackendShutdownState {
    const state = require_exact_record(value, ['session_id', 'version', 'status'], 'shutdown state');
    if (state.session_id !== session_id) {
        throw new BackendAdapterError('SESSION_MISMATCH', 'Shutdown state belongs to another backend session', false);
    }
    if (!Number.isSafeInteger(state.version) || (state.version as number) < 0
        || typeof state.status !== 'string'
        || !BACKEND_TRADING_STATUSES.has(state.status as BackendTradingStatus)) {
        throw new BackendContractError('MALFORMED_BACKEND_PAYLOAD', 'Backend shutdown state is invalid');
    }
    return state as unknown as BackendShutdownState;
}

/**
 * 함수 이름: classify_native_sidecar_wait_failure()
 * 기능: Tauri invoke Err의 allowlisted code만 timeout 또는 abnormal exit로 분류한다.
 * 인자: error -> native await command가 reject한 unknown 값
 * 반환값: operator wait 재시도가 가능한 timeout 또는 offline 복구가 필요한 abnormal
 * 작성 날짜: 2026/08/24
 */
function classify_native_sidecar_wait_failure(
    error: unknown,
): 'timeout' | 'abnormal' {
    if (typeof error !== 'object' || error === null || !('code' in error)) {
        return 'abnormal';
    }

    const native_code = error.code;
    if (native_code === 'BACKEND_SIDECAR_EXIT_TIMEOUT') {
        return 'timeout';
    }
    if (typeof native_code === 'string'
        && NATIVE_SIDECAR_EXIT_FAILURE_CODES.has(native_code)) {
        return 'abnormal';
    }

    return 'abnormal';
}

/**
 * 함수 이름: format_shutdown_blocked_error()
 * 기능: shutdown 409의 exposure detail을 strict 검증해 운영자가 판단할 정적 문구로 바꾼다.
 * 인자: error -> backend typed shutdown failure
 * 반환값: position/order/reconciliation 상태가 포함된 안전한 typed 오류
 * 작성 날짜: 2026/08/24
 */
function format_shutdown_blocked_error(error: BackendCommandError): BackendCommandError {
    const details = require_exact_record(
        error.details,
        [
            'accepted',
            'status',
            'version',
            'position_open',
            'pending_order',
            'reconciliation_required',
        ],
        'shutdown blocked details',
    );
    const booleans_are_valid = details.accepted === false
        && details.status === 'blocked'
        && typeof details.position_open === 'boolean'
        && typeof details.pending_order === 'boolean'
        && typeof details.reconciliation_required === 'boolean';

    if (!booleans_are_valid
        || !Number.isSafeInteger(details.version)
        || (details.version as number) < 0) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'Backend shutdown blocked details are invalid',
        );
    }

    const exposure_summary = [
        `열린 포지션: ${details.position_open === true ? '있음' : '없음'}`,
        `미체결 주문: ${details.pending_order === true ? '있음' : '없음'}`,
        `조정 필요: ${details.reconciliation_required === true ? '있음' : '없음'}`,
    ].join(', ');

    // Backend의 임의 detail을 그대로 반사하지 않고 검증한 세 가지 안전 상태만 사용자에게 노출한다.
    return new BackendCommandError(
        error.code,
        `안전 종료가 보류되었습니다. ${exposure_summary}. 상태를 확인한 뒤 다시 시도하거나 취소해 주세요.`,
        true,
        details,
    );
}

/**
 * 함수 이름: validate_regime_command_result()
 * 기능: selection 성공값의 canonical REGIME, support 상태와 version을 검증한다.
 * 인자: value -> REGIME selection command envelope의 data
 * 반환값: 검증된 authoritative selection 결과
 * 작성 날짜: 2026/08/21
 */
function validate_regime_command_result(value: unknown): BackendRegimeCommandResult {
    const result = validate_versioned_command_result(value);

    if (typeof result.selected !== 'string'
        || !BACKEND_REGIME_TYPES.has(result.selected as RegimeType)) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'Backend regime selection is invalid',
        );
    }
    if (result.support_status !== 'supported'
        && result.support_status !== 'unsupported') {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'Backend regime support status is invalid',
        );
    }
    // Phase 6 registry의 TYPE_0-only coverage와 응답 support 상태가 어긋나면 fail closed한다.
    const expected_support_status = result.selected === 'type0'
        ? 'supported'
        : 'unsupported';
    if (result.support_status !== expected_support_status) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'Backend regime support status is inconsistent',
        );
    }

    return result as BackendRegimeCommandResult;
}

/**
 * 함수 이름: percentage_to_ratio_text()
 * 기능: integer percentage를 부동소수점 연산 없이 0~1 Decimal wire 문자열로 변환한다.
 * 인자: percentage -> 검증이 끝난 0~100 정수
 * 반환값: canonical ratio Decimal 문자열
 * 작성 날짜: 2026/08/21
 */
function percentage_to_ratio_text(percentage: number): string {
    if (percentage === 0) {
        return '0';
    }
    if (percentage === 100) {
        return '1';
    }

    // 정수 percentage의 두 자릿수를 소수부로 옮긴 뒤 의미 없는 후행 0만 제거한다.
    return `0.${percentage.toString().padStart(2, '0')}`.replace(/0$/u, '');
}

// Generated composite, primitive와 command receipt의 exact wire key를 runtime 검증 목록으로 고정한다.
const TRADE_DETAILS_KEYS = [
    'query',
    'rows',
    'row_count',
    'summary',
] as const satisfies ReadonlyArray<keyof BackendTradeDetails>;
const TRADE_DETAILS_QUERY_KEYS = [
    'period',
    'side',
    'start_date',
    'end_date',
] as const satisfies ReadonlyArray<keyof BackendTradeDetailsQuery>;
const TRADE_DETAILS_SUMMARY_KEYS = [
    'holdings_asset',
    'holdings',
    'account_version',
    'performance',
] as const satisfies ReadonlyArray<keyof BackendTradeDetailsSummary>;
const PERFORMANCE_KEYS = [
    'daily_return_rate',
    'cumulative_return_rate',
    'realized_pnl',
    'daily_fee',
    'total_fee',
    'average_sell_return_rate',
    'total_profit',
    'winning_sell_count',
    'losing_sell_count',
    'breakeven_sell_count',
    'completed_sell_count',
    'win_rate',
] as const satisfies ReadonlyArray<keyof BackendTradeDetailsSummary['performance']>;
const TRADE_KEYS = [
    'trade_id',
    'order_id',
    'client_order_id',
    'symbol',
    'executed_at',
    'side',
    'regime_type',
    'strategy',
    'requested_quantity',
    'executed_quantity',
    'executed_amount',
    'average_fill_price',
    'market_price_at_decision',
    'fee_amount',
    'fee_asset',
    'fee_quote_amount',
    'allocated_cost_basis',
    'realized_pnl',
    'realized_return_rate',
    'exit_reason',
] as const satisfies ReadonlyArray<keyof BackendTradeDetails['rows'][number]>;
const CSV_EXPORT_RECEIPT_KEYS = [
    'file_path',
    'exported_row_count',
] as const satisfies ReadonlyArray<keyof CsvExportReceipt>;

/**
 * 함수 이름: require_exact_record()
 * 기능: composite 상세 응답의 object 여부와 허용된 exact key 집합을 함께 검증한다.
 * 인자: value -> 검증할 JSON 값, keys -> 허용 key 목록, field_name -> 오류 경계 이름
 * 반환값: exact key만 가진 record
 * 작성 날짜: 2026/08/23
 */
function require_exact_record(
    value: unknown,
    keys: ReadonlyArray<string>,
    field_name: string,
): Record<string, unknown> {
    if (typeof value !== 'object' || value === null || Array.isArray(value)) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            `Backend ${field_name} must be an object`,
        );
    }

    const record = value as Record<string, unknown>;
    const actual_keys = Object.keys(record);
    if (actual_keys.length !== keys.length
        || !keys.every((key) => Object.hasOwn(record, key))) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            `Backend ${field_name} keys are invalid`,
        );
    }

    return record;
}

/**
 * 함수 이름: parse_local_date_epoch()
 * 기능: YYYY-MM-DD가 실제 Gregorian 날짜인지 확인하고 UTC day 비교값으로 변환한다.
 * 인자: value -> backend가 반환한 inclusive KST LocalDate
 * 반환값: 검증된 날짜의 UTC epoch millisecond
 * 작성 날짜: 2026/08/23
 */
function parse_local_date_epoch(value: unknown): number {
    if (typeof value !== 'string' || !LOCAL_DATE_PATTERN.test(value)) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'Backend trade query date is invalid',
        );
    }

    const epoch = Date.parse(`${value}T00:00:00.000Z`);
    if (!Number.isFinite(epoch)
        || new Date(epoch).toISOString().slice(0, 10) !== value) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'Backend trade query date is invalid',
        );
    }

    return epoch;
}

/**
 * 함수 이름: validate_trade_details_query()
 * 기능: backend가 echo한 period/side와 inclusive KST 날짜 범위가 요청과 일치하는지 검증한다.
 * 인자: value -> 상세 응답 query, requested_query -> UI가 전송한 결합 filter
 * 반환값: 검증된 wire query
 * 작성 날짜: 2026/08/23
 */
function validate_trade_details_query(
    value: unknown,
    requested_query: TradeHistoryQuery,
): BackendTradeDetailsQuery {
    const query = require_exact_record(value, TRADE_DETAILS_QUERY_KEYS, 'trade query');
    if (query.period !== requested_query.period || query.side !== requested_query.side) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'Backend trade query does not match the requested filters',
        );
    }

    const start_epoch = parse_local_date_epoch(query.start_date);
    const end_epoch = parse_local_date_epoch(query.end_date);
    const inclusive_span_days = ((end_epoch - start_epoch) / 86_400_000) + 1;
    const expected_span_days = requested_query.period === 'today'
        ? 1
        : requested_query.period === 'last7days'
            ? 7
            : requested_query.period === 'last30days'
                ? 30
                : null;
    const is_invalid_all_range = requested_query.period === 'all'
        && query.start_date !== '0001-01-01';

    // Preset은 inclusive day 수를, 전체 기간은 명세된 최소 LocalDate를 정확히 사용해야 한다.
    if (end_epoch < start_epoch
        || is_invalid_all_range
        || (expected_span_days !== null && inclusive_span_days !== expected_span_days)) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'Backend trade query date range is inconsistent',
        );
    }

    return query as unknown as BackendTradeDetailsQuery;
}

/**
 * 함수 이름: validate_trade_details_summary()
 * 기능: current ETH holdings와 account version, 전체 Performance의 exact composite 계약을 검증한다.
 * 인자: value -> 상세 응답 summary
 * 반환값: 검증된 wire summary
 * 작성 날짜: 2026/08/23
 */
function validate_trade_details_summary(value: unknown): BackendTradeDetailsSummary {
    const summary = require_exact_record(value, TRADE_DETAILS_SUMMARY_KEYS, 'trade summary');
    const performance = require_exact_record(
        summary.performance,
        PERFORMANCE_KEYS,
        'trade performance',
    );
    if (summary.holdings_asset !== 'ETH'
        || typeof summary.holdings !== 'string'
        || !NON_NEGATIVE_DECIMAL_PATTERN.test(summary.holdings)
        || !Number.isSafeInteger(summary.account_version)
        || (summary.account_version as number) < 0) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'Backend trade summary is invalid',
        );
    }

    validate_performance_snapshot(performance);

    return summary as unknown as BackendTradeDetailsSummary;
}

/**
 * 함수 이름: validate_trade_details()
 * 기능: 상세 조회의 query, filtered rows와 D-12 summary를 하나의 strict 결과로 검증한다.
 * 인자: value -> `/v1/trades` 성공 data, requested_query -> 전송한 period/side filter
 * 반환값: UI actor가 소비할 정규화된 composite 상세 결과
 * 작성 날짜: 2026/08/23
 */
function validate_trade_details(
    value: unknown,
    requested_query: TradeHistoryQuery,
): TradeHistoryDetails {
    const details = require_exact_record(value, TRADE_DETAILS_KEYS, 'trade details');
    validate_trade_details_query(details.query, requested_query);
    const summary = validate_trade_details_summary(details.summary);
    if (!Array.isArray(details.rows)
        || !Number.isSafeInteger(details.row_count)
        || (details.row_count as number) < 0
        || (details.row_count as number) > BACKEND_MAX_TRADE_PAGE_SIZE
        || details.row_count !== details.rows.length) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'Backend trade rows are invalid',
        );
    }

    const wire_details = details as unknown as BackendTradeDetails;
    const records = wire_details.rows.map((row) => {
        require_exact_record(row, TRADE_KEYS, 'trade row');
        return map_trade_record(validate_trade_snapshot(row));
    });

    return {
        records,
        summary: map_trade_history_summary(summary.performance, summary.holdings),
    };
}

/**
 * 함수 이름: validate_csv_receipt()
 * 기능: CSV command 성공값의 exact key, non-empty path와 양수 row count를 검증한다.
 * 인자: value -> `/v1/csv-exports` 성공 data
 * 반환값: 검증된 CSV receipt
 * 작성 날짜: 2026/08/23
 */
function validate_csv_receipt(value: unknown): CsvExportReceipt {
    const receipt = require_exact_record(
        value,
        CSV_EXPORT_RECEIPT_KEYS,
        'CSV receipt',
    );
    if (typeof receipt.file_path !== 'string'
        || receipt.file_path.trim().length === 0
        || !Number.isSafeInteger(receipt.exported_row_count)
        || (receipt.exported_row_count as number) <= 0) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'Backend CSV receipt is invalid',
        );
    }

    return receipt as unknown as CsvExportReceipt;
}

/**
 * 클래스 이름: BackendUiAdapter
 * 기능: UiCommandPort와 snapshot-first loopback HTTP/WebSocket lifecycle을 wire 수준에서 구현한다.
 * 작성 날짜: 2026/08/21
 */
export class BackendUiAdapter implements UiCommandPort {
    readonly session_id: string;

    readonly #fetch: typeof fetch;
    readonly #create_web_socket: (url: string) => BackendWebSocket;
    readonly #create_uuid: () => string;
    readonly #invoke_csv_directory_picker: () => Promise<unknown>;
    readonly #request_timeout_ms: number;
    readonly #shutdown_wait_timeout_ms: number;
    readonly #shutdown_poll_interval_ms: number;
    readonly #wait_for_sidecar_exit: () => Promise<unknown>;
    readonly #http_origin: string;
    readonly #web_socket_url: string;

    #session_token: string | null;
    #active_session_id: string;
    #last_sequence = 0;
    #web_socket: BackendWebSocket | null = null;
    #callbacks: BackendUiAdapterCallbacks | null = null;
    #is_stopped = true;
    #is_resynchronizing = false;
    #shutdown_is_in_flight = false;
    #shutdown_was_accepted = false;
    #shutdown_outcome_is_ambiguous = false;
    #shutdown_stream_closed_during_request = false;
    #connection_generation = 0;
    #trading_version: number | null = null;
    #trading_status: BackendTradingStatus | null = null;
    #has_open_position = false;
    #selected_regime: RegimeType | null = null;
    #scale_in_ratio: string | null = null;
    #scale_out_ratio: string | null = null;
    readonly #seen_event_ids = new Set<string>();
    readonly #event_id_order: Array<string> = [];
    readonly #active_abort_controllers = new Set<AbortController>();
    readonly #pending_idempotency_keys = new Map<string, string>();

    /**
     * 함수 이름: BackendUiAdapter.constructor()
     * 기능: 검증된 launch descriptor를 secret 비열거 private field와 exact loopback origin으로 옮긴다.
     * 인자: descriptor -> native launch descriptor
     *      dependencies -> fetch/WebSocket/UUID/timeout test seam
     * 반환값: 수명주기를 아직 시작하지 않은 adapter
     * 작성 날짜: 2026/08/21
     */
    constructor(
        descriptor: BackendConnectionDescriptor,
        dependencies: BackendUiAdapterDependencies = {},
    ) {
        const validated_descriptor = validate_connection_descriptor(descriptor);
        const request_timeout_ms = dependencies.request_timeout_ms
            ?? DEFAULT_REQUEST_TIMEOUT_MS;
        const shutdown_wait_timeout_ms = dependencies.shutdown_wait_timeout_ms
            ?? DEFAULT_SHUTDOWN_WAIT_TIMEOUT_MS;
        const shutdown_poll_interval_ms = dependencies.shutdown_poll_interval_ms
            ?? DEFAULT_SHUTDOWN_POLL_INTERVAL_MS;

        if (!Number.isSafeInteger(request_timeout_ms) || request_timeout_ms <= 0
            || !Number.isSafeInteger(shutdown_wait_timeout_ms)
            || shutdown_wait_timeout_ms <= 0
            || !Number.isSafeInteger(shutdown_poll_interval_ms)
            || shutdown_poll_interval_ms <= 0) {
            throw new BackendAdapterError(
                'INVALID_ADAPTER_CONFIGURATION',
                'Backend request timeout is invalid',
                false,
            );
        }

        this.session_id = validated_descriptor.session_id;
        this.#active_session_id = validated_descriptor.session_id;
        this.#session_token = validated_descriptor.token;
        this.#fetch = dependencies.fetch ?? globalThis.fetch.bind(globalThis);
        this.#create_web_socket = dependencies.create_web_socket
            ?? create_default_web_socket;
        this.#create_uuid = dependencies.create_uuid ?? create_default_uuid;
        this.#invoke_csv_directory_picker = dependencies.pick_csv_directory
            ?? invoke_default_csv_directory_picker;
        this.#request_timeout_ms = request_timeout_ms;
        this.#shutdown_wait_timeout_ms = shutdown_wait_timeout_ms;
        this.#shutdown_poll_interval_ms = shutdown_poll_interval_ms;
        this.#wait_for_sidecar_exit = dependencies.wait_for_sidecar_exit
            ?? invoke_default_sidecar_exit_waiter;
        this.#http_origin = `http://127.0.0.1:${validated_descriptor.port}`;
        this.#web_socket_url = `ws://127.0.0.1:${validated_descriptor.port}/v1/events`;
    }

    /**
     * 함수 이름: load_snapshot()
     * 기능: authenticated snapshot envelope를 요청하고 전체 runtime schema와 ready 상태를 검증한다.
     * 인자: 없음
     * 반환값: coherent backend snapshot Promise
     * 작성 날짜: 2026/08/21
     */
    async load_snapshot(): Promise<BackendSnapshot> {
        const snapshot = await this.request_json(
            'GET',
            '/v1/snapshot',
            validate_backend_snapshot,
        );

        // Snapshot은 이후 모든 command의 expected_version과 split-ratio 기준값을 원자적으로 갱신한다.
        this.synchronize_command_state(snapshot);
        return snapshot;
    }

    /**
     * 함수 이름: load_binance_connection_status()
     * 기능: 백엔드에서 Binance API·WebSocket 연결을 진단하고 검증된 상태만 반환한다.
     * 인자: signal -> 툴팁이 닫힐 때 요청을 취소할 신호
     * 반환값: Binance 연결 상태 Promise
     * 작성 날짜: 2026/09/05
     */
    async load_binance_connection_status(signal?: AbortSignal): Promise<BackendBinanceConnectionStatus> {
        return this.request_json(
            'GET',
            '/v1/binance/connection-status',
            validate_binance_connection_status,
            undefined,
            false,
            signal,
            60_000,  // Binance의 bounded 인증 조회·시간 재동기화가 끝난 뒤 상태를 받을 수 있게 한다.
        );
    }

    /**
     * 함수 이름: start_live_events()
     * 기능: 성공한 startup snapshot의 session/sequence 뒤에서 첫-frame 인증 WebSocket을 시작한다.
     * 인자: snapshot -> facade cold hydration에 사용한 같은 backend snapshot
     *      callbacks -> event, full resync와 failure 전달 callback
     * 반환값: 없음
     * 작성 날짜: 2026/08/21
     */
    start_live_events(
        snapshot: BackendSnapshot,
        callbacks: BackendUiAdapterCallbacks,
    ): void {
        if (snapshot.session_id !== this.session_id) {
            throw new BackendAdapterError(
                'SESSION_MISMATCH',
                'Backend snapshot session does not match the launch descriptor',
                false,
            );
        }
        if (this.#session_token === null) {
            throw new BackendAdapterError(
                'ADAPTER_STOPPED',
                'Backend adapter has already stopped',
                false,
            );
        }

        this.#callbacks = callbacks;
        this.#active_session_id = snapshot.session_id;
        this.#last_sequence = snapshot.last_sequence;
        this.synchronize_command_state(snapshot);
        this.#is_stopped = false;
        this.clear_seen_events();
        this.open_event_stream();
    }

    /**
     * 함수 이름: stop()
     * 기능: stale callback generation을 폐기하고 socket, callback과 token 참조를 제거한다.
     * 인자: 없음
     * 반환값: 없음
     * 작성 날짜: 2026/08/21
     */
    stop(): void {
        if (this.#is_stopped && this.#session_token === null) {
            return;
        }

        this.#is_stopped = true;
        this.#is_resynchronizing = false;
        this.#callbacks = null;
        this.#connection_generation += 1;
        this.close_current_socket('client stop');
        this.abort_active_requests();
        this.clear_seen_events();
        this.#pending_idempotency_keys.clear();
        this.#session_token = null;
    }

    /**
     * 함수 이름: start_trading()
     * 기능: snapshot과 같은 선택 REGIME인지 확인하고 versioned trading start command를 전달한다.
     * 인자: regime_type -> UI가 시작하려는 선택 REGIME
     * 반환값: backend typed 결과 Promise
     * 작성 날짜: 2026/08/21
     */
    async start_trading(regime_type: RegimeType): Promise<TradingCommandReceipt> {
        if (this.#selected_regime !== regime_type) {
            throw new BackendCommandError(
                'REGIME_SELECTION_REQUIRED',
                'The selected regime must be synchronized before trading starts.',
                false,
            );
        }

        const expected_version = this.require_trading_version();
        const result = await this.request_json(
            'POST',
            '/v1/trading/start',
            validate_trading_command_result,
            {
                schema_version: BACKEND_SCHEMA_VERSION,
                expected_version,
            },
            true,
        );

        if (result.status !== 'running' || result.session_id === null) {
            throw new BackendContractError(
                'MALFORMED_BACKEND_PAYLOAD',
                'Backend start command did not return a running session',
            );
        }

        this.require_current_command_response(result.version, expected_version);
        this.#trading_version = result.version;  // 다음 command는 start가 확정한 새 version을 사용한다.
        this.#trading_status = result.status;
        return result;
    }

    /**
     * 함수 이름: stop_trading()
     * 기능: authoritative position 분기를 backend에 맡기는 versioned stop command를 전달한다.
     * 인자: 없음
     * 반환값: backend typed 결과 Promise
     * 작성 날짜: 2026/08/21
     */
    async stop_trading(): Promise<TradingCommandReceipt> {
        const expected_version = this.require_trading_version();
        const result = await this.request_json(
            'POST',
            '/v1/trading/stop',
            validate_trading_command_result,
            {
                schema_version: BACKEND_SCHEMA_VERSION,
                expected_version,
            },
            true,
        );

        if (result.status === 'running') {
            throw new BackendContractError(
                'MALFORMED_BACKEND_PAYLOAD',
                'Backend stop command returned a running state',
            );
        }

        this.require_current_command_response(result.version, expected_version);
        this.#trading_version = result.version;  // 중지 결과가 stopping이어도 반환 version이 다음 기준이다.
        this.#trading_status = result.status;
        return result;
    }

    /**
     * 함수 이름: force_sell_and_stop()
     * 기능: UI가 추측한 position 분기와 무관하게 일반 stop endpoint의 authoritative D-05 분기를 사용한다.
     * 인자: 없음
     * 반환값: backend stop 결과 Promise
     * 작성 날짜: 2026/08/21
     */
    async force_sell_and_stop(): Promise<TradingCommandReceipt> {
        return this.stop_trading();
    }

    /**
     * 함수 이름: liquidate_recovered_position()
     * 기능: startup에서 복구한 Position을 자동 재개 없이 청산하는 versioned command를 전달한다.
     * 인자: 없음
     * 반환값: backend recovery liquidation lifecycle 결과 Promise
     * 작성 날짜: 2026/08/24
     */
    async liquidate_recovered_position(): Promise<TradingCommandReceipt> {
        const expected_version = this.require_trading_version();
        const result = await this.request_json(
            'POST',
            '/v1/trading/recovered-position/liquidate',
            validate_trading_command_result,
            {
                schema_version: BACKEND_SCHEMA_VERSION,
                expected_version,
            },
            true,
        );

        // 복구 청산은 전략 session을 RUNNING으로 재개하지 않고 종료 계열 상태만 반환해야 한다.
        if (result.status === 'running') {
            throw new BackendContractError(
                'MALFORMED_BACKEND_PAYLOAD',
                'Backend recovered-position liquidation returned a running state',
            );
        }

        this.require_current_command_response(result.version, expected_version);
        this.#trading_version = result.version;  // 다음 명령은 청산 수락 뒤 authoritative version을 사용한다.
        this.#trading_status = result.status;
        return result;
    }

    /**
     * 함수 이름: apply_regime()
     * 기능: canonical REGIME과 expected context version을 selection endpoint에 전달한다.
     * 인자: regime_type -> 사용자가 선택한 canonical UI REGIME
     * 반환값: backend typed 결과 Promise
     * 작성 날짜: 2026/08/21
     */
    async apply_regime(regime_type: RegimeType): Promise<void> {
        const expected_version = this.require_trading_version();
        const result = await this.request_json(
            'POST',
            '/v1/regime/selection',
            validate_regime_command_result,
            {
                schema_version: BACKEND_SCHEMA_VERSION,
                regime_type,
                expected_version,
            },
            true,
        );

        if (result.selected !== regime_type) {
            throw new BackendContractError(
                'MALFORMED_BACKEND_PAYLOAD',
                'Backend selected a different regime than requested',
            );
        }

        // 성공 응답 뒤에만 adapter의 선택값과 optimistic concurrency version을 함께 전진시킨다.
        this.require_current_command_response(result.version, expected_version);
        this.#selected_regime = result.selected;
        this.#trading_version = result.version;
    }

    /**
     * 함수 이름: update_split_order()
     * 기능: slider 값을 Decimal ratio로 바꾸고 반대 방향의 authoritative 비율과 함께 PATCH한다.
     * 인자: order_side -> scale-in 또는 scale-out
     *      percentage -> UI slider의 정수 percentage
     * 반환값: backend typed 결과 Promise
     * 작성 날짜: 2026/08/21
     */
    async update_split_order(
        order_side: 'scale_in' | 'scale_out',
        percentage: number,
    ): Promise<void> {
        if ((order_side !== 'scale_in' && order_side !== 'scale_out')
            || !Number.isSafeInteger(percentage)
            || percentage < 0
            || percentage > 100) {
            throw new BackendCommandError(
                'INVALID_SPLIT_RATIO',
                'Split ratio must be an integer between 0 and 100.',
                false,
            );
        }

        if (this.#scale_in_ratio === null || this.#scale_out_ratio === null) {
            throw new BackendCommandError(
                'TRADING_SNAPSHOT_REQUIRED',
                'A trading snapshot is required before split ratios can change.',
                true,
            );
        }

        const requested_ratio = percentage_to_ratio_text(percentage);
        const expected_version = this.require_trading_version();
        const result = await this.request_json(
            'PATCH',
            '/v1/trading/split-ratios',
            validate_split_ratio_command_result,
            {
                schema_version: BACKEND_SCHEMA_VERSION,
                scale_in: order_side === 'scale_in'
                    ? requested_ratio
                    : this.#scale_in_ratio,
                scale_out: order_side === 'scale_out'
                    ? requested_ratio
                    : this.#scale_out_ratio,
                expected_version,
            },
            true,
        );

        // Backend 정규화 결과를 다음 한쪽 변경 command의 반대편 기준값으로 보존한다.
        this.require_current_command_response(result.version, expected_version);
        this.#scale_in_ratio = result.scale_in;
        this.#scale_out_ratio = result.scale_out;
        this.#trading_version = result.version;
    }

    /**
     * 함수 이름: synchronize_command_state()
     * 기능: coherent snapshot의 version, selection과 split ratios를 command-local 기준으로 교체한다.
     * 인자: snapshot -> runtime validation이 끝난 backend snapshot
     * 반환값: 없음
     * 작성 날짜: 2026/08/21
     */
    private synchronize_command_state(snapshot: BackendSnapshot): void {
        const trading = snapshot.trading;

        this.#trading_version = trading.version;
        this.#trading_status = trading.status;
        this.#has_open_position = trading.has_open_position;
        this.#selected_regime = snapshot.regime.selected;
        this.#scale_in_ratio = trading.scale_in;
        this.#scale_out_ratio = trading.scale_out;
    }

    /**
     * 함수 이름: require_current_command_response()
     * 기능: 요청 중 WS/resync가 더 높은 Context version을 적용했으면 늦은 HTTP 결과를 거부한다.
     * 인자: response_version -> command success DTO의 version
     *      expected_version -> request body가 보낸 version
     * 반환값: 응답이 현재이면 void, 역행이면 BackendContractError 발생
     * 작성 날짜: 2026/08/21
     */
    private require_current_command_response(
        response_version: number,
        expected_version: number,
    ): void {
        const current_version = this.require_trading_version();

        // 서버가 요청 전보다 과거이거나 비동기 authoritative state보다 뒤면 local 값을 덮지 않는다.
        if (response_version < expected_version || response_version < current_version) {
            throw new BackendContractError(
                'STALE_BACKEND_RESPONSE',
                'Backend command response is older than the synchronized trading state',
            );
        }
    }

    /**
     * 함수 이름: require_trading_version()
     * 기능: snapshot-first command에 필요한 expected version이 없으면 fail closed한다.
     * 인자: 없음
     * 반환값: 마지막 authoritative trading version
     * 작성 날짜: 2026/08/21
     */
    private require_trading_version(): number {
        if (this.#trading_version === null) {
            throw new BackendCommandError(
                'TRADING_SNAPSHOT_REQUIRED',
                'A trading snapshot is required before this command.',
                true,
            );
        }

        return this.#trading_version;
    }

    /**
     * 함수 이름: load_trade_history()
     * 기능: period/side query를 URLSearchParams로 직렬화하고 strict composite 상세 결과를 반환한다.
     * 인자: query -> UI trade history filter, signal -> route invoke 수명주기 취소 신호
     * 반환값: 검증된 필터 행과 D-12 summary Promise
     * 작성 날짜: 2026/08/23
     */
    async load_trade_history(
        query: TradeHistoryQuery,
        signal?: AbortSignal,
    ): Promise<TradeHistoryDetails> {
        const query_string = new URLSearchParams({
            period: query.period,
            side: query.side,
        }).toString();

        return this.request_json(
            'GET',
            `/v1/trades?${query_string}`,
            (value) => validate_trade_details(value, query),
            undefined,
            false,
            signal,
        );
    }

    /**
     * 함수 이름: pick_csv_directory()
     * 기능: native picker 결과를 string 또는 취소 null로만 제한해 UI actor에 전달한다.
     * 인자: 없음
     * 반환값: 선택한 directory 문자열 또는 취소를 나타내는 null Promise
     * 작성 날짜: 2026/08/23
     */
    async pick_csv_directory(): Promise<string | null> {
        const selected_directory = await this.#invoke_csv_directory_picker();

        // Native IPC 경계에서 primitive string과 명시적 취소 null 외의 값을 차단한다.
        if (selected_directory === null || typeof selected_directory === 'string') {
            return selected_directory;
        }

        throw new BackendContractError(
            'MALFORMED_NATIVE_PICKER_RESULT',
            'Native CSV directory picker result is invalid',
        );
    }

    /**
     * 함수 이름: export_csv()
     * 기능: 검증된 CSV option 전체를 authenticated backend command로 전달하고 strict receipt를 반환한다.
     * 인자: options -> UI가 검증한 CSV export option
     * 반환값: backend typed receipt Promise
     * 작성 날짜: 2026/08/23
     */
    async export_csv(options: CsvExportOptions): Promise<CsvExportReceipt> {
        // Generated request type가 schema version과 option 전체의 Python-owned 계약 drift를 막는다.
        const request_body = {
            schema_version: BACKEND_SCHEMA_VERSION,
            ...options,
        } satisfies BackendCsvExportRequest;

        return this.request_json(
            'POST',
            '/v1/csv-exports',
            validate_csv_receipt,
            request_body,
            true,
            undefined,
            null,
        );
    }

    /**
     * 함수 이름: shutdown_application()
     * 기능: backend shutdown 결과를 그대로 전파하고 성공한 경우에만 live secret 참조를 제거한다.
     * 인자: 없음
     * 반환값: backend typed 결과 Promise
     * 작성 날짜: 2026/08/21
     */
    async shutdown_application(): Promise<void> {
        if (!this.#shutdown_was_accepted && !this.#shutdown_outcome_is_ambiguous) {
            await this.await_safe_trading_terminal_state();
        }
        await this.request_shutdown_and_await_exit(false);
    }

    /**
     * 함수 이름: shutdown_recovery_application()
     * 기능: 전체 snapshot 없이 종료 기준만 읽고 백엔드의 exposure 검사와 정상 종료를 요청한다.
     * 인자: 없음
     * 반환값: backend code 0 확인까지의 Promise
     * 작성 날짜: 2026/09/05
     */
    async shutdown_recovery_application(): Promise<void> {
        if (!this.#shutdown_was_accepted && !this.#shutdown_outcome_is_ambiguous) {
            const state = await this.request_json(
                'GET',
                '/v1/shutdown/state',
                (value) => validate_shutdown_state(value, this.session_id),
            );
            this.#trading_version = state.version;
            this.#trading_status = state.status;
        }

        // 복구 화면은 stop/청산을 제출하지 않는다. Backend shutdown owner가 열린 exposure를 거부한다.
        await this.request_shutdown_and_await_exit(true);
    }

    /**
     * 함수 이름: request_shutdown_and_await_exit()
     * 기능: 정상·복구 종료의 202, 멱등 재시도와 native process exit 확인을 공유한다.
     * 인자: from_recovery -> backend가 flat RUNNING session을 직접 종료하는 복구 경로 여부
     * 반환값: backend 정상 종료 완료 Promise
     * 작성 날짜: 2026/09/05
     */
    private async request_shutdown_and_await_exit(from_recovery: boolean): Promise<void> {
        if (!this.#shutdown_was_accepted) {
            const expected_version = this.require_trading_version();
            let receipt: BackendShutdownReceipt;

            this.#shutdown_is_in_flight = true;
            try {
                receipt = await this.request_json(
                    'POST',
                    '/v1/shutdown',
                    (value) => {
                        const validated_receipt = validate_shutdown_receipt(value);

                        // Version 검증도 idempotency 결과 확정 전에 끝내 잘못된 202를 수락하지 않는다.
                        this.require_current_command_response(
                            validated_receipt.version,
                            expected_version,
                        );
                        const backend_stopped_running_session = from_recovery
                            && this.#trading_status === 'running';
                        if (!backend_stopped_running_session && validated_receipt.version !== expected_version) {
                            throw new BackendContractError(
                                'MALFORMED_BACKEND_PAYLOAD',
                                'Backend shutdown receipt version does not match the request',
                            );
                        }
                        return validated_receipt;
                    },
                    {
                        schema_version: BACKEND_SCHEMA_VERSION,
                        expected_version,
                    },
                    true,
                    undefined,
                    this.#request_timeout_ms,
                    202,
                );
            } catch (error) {
                this.#shutdown_is_in_flight = false;
                const stream_was_closed = this.#shutdown_stream_closed_during_request;
                this.#shutdown_stream_closed_during_request = false;
                if (error instanceof BackendCommandError
                    && (error.code === 'SHUTDOWN_BLOCKED_BY_OPEN_EXPOSURE'
                        || error.code === 'STALE_CONTEXT_VERSION')) {
                    this.#shutdown_outcome_is_ambiguous = false;
                    if (stream_was_closed) {
                        // 명시적 409 뒤 닫힌 stream은 정상 live resync 경로로 복구한다.
                        void this.full_resynchronize('EVENT_STREAM_CLOSED');
                    }
                    throw error.code === 'SHUTDOWN_BLOCKED_BY_OPEN_EXPOSURE'
                        ? format_shutdown_blocked_error(error) : error;
                }

                // 응답이 불명확하면 동일 shutdown 재시도 외의 command를 차단한다.
                this.#shutdown_outcome_is_ambiguous = true;
                const position_label = this.#has_open_position ? '있음' : '없음';
                throw new BackendAdapterError(
                    'SHUTDOWN_OUTCOME_AMBIGUOUS',
                    from_recovery
                        ? '종료 응답을 확인하지 못했습니다. 포지션·미체결 주문·조정 상태: 확인 필요. 동일 종료 요청으로 결과를 다시 확인해 주세요.'
                        : `종료 응답을 확인하지 못했습니다. 마지막 확인 위치의 열린 포지션: ${position_label}, 미체결 주문·조정 상태: 확인 필요. 프로세스를 강제 종료하지 말고 동일 종료 요청으로 결과를 다시 확인해 주세요.`,
                    true,
                );
            }

            this.#shutdown_was_accepted = true;
            this.#shutdown_outcome_is_ambiguous = false;
            this.#shutdown_is_in_flight = false;
            this.#shutdown_stream_closed_during_request = false;
            this.#trading_version = receipt.version;
        }

        let native_exit_receipt: unknown;
        try {
            // HTTP 202는 접수일 뿐이므로 child process의 정상 종료까지 창과 token을 유지한다.
            native_exit_receipt = await this.#wait_for_sidecar_exit();
        } catch (error) {
            if (classify_native_sidecar_wait_failure(error) === 'timeout') {
                throw new BackendAdapterError(
                    'SIDECAR_EXIT_TIMEOUT',
                    '종료 접수 시 열린 포지션: 없음, 미체결 주문: 없음, 조정 필요: 없음으로 확인했습니다. 프로세스를 강제 종료하지 않았으며 창을 유지한 채 종료 상태를 다시 확인해야 합니다.',
                    true,
                );
            }

            throw new BackendAdapterError(
                'SIDECAR_ABNORMAL_EXIT',
                '백엔드 프로세스가 비정상 종료되었거나 종료 상태를 검증할 수 없습니다. 새 주문을 차단하고 애플리케이션을 다시 시작해야 합니다.',
                false,
            );
        }
        validate_sidecar_exit_receipt(native_exit_receipt);

        // Backend process가 정상 종료된 뒤에만 renderer의 launch token 참조를 폐기한다.
        this.stop();
    }

    /**
     * 함수 이름: await_safe_trading_terminal_state()
     * 기능: 종료 전에 RUNNING을 authoritative stop하고 비동기 청산·조정의 terminal snapshot을 기다린다.
     * 인자: 없음
     * 반환값: not_started 또는 terminated에 도달하면 완료되는 Promise
     * 작성 날짜: 2026/08/24
     */
    private async await_safe_trading_terminal_state(): Promise<void> {
        if (this.#trading_status === null) {
            throw new BackendCommandError(
                'TRADING_SNAPSHOT_REQUIRED',
                '프로그램 종료 전 최신 거래 상태가 필요합니다.',
                true,
            );
        }
        if (this.#trading_status === 'running') {
            await this.stop_trading();
        }
        if (this.#trading_status === 'not_started'
            || this.#trading_status === 'terminated') {
            return;
        }

        const deadline = Date.now() + this.#shutdown_wait_timeout_ms;
        while (Date.now() < deadline) {
            await new Promise<void>((resolve) => {
                globalThis.setTimeout(resolve, this.#shutdown_poll_interval_ms);
            });
            const snapshot = await this.load_snapshot();

            if (snapshot.trading.status === 'not_started'
                || snapshot.trading.status === 'terminated') {
                return;
            }
        }

        const position_label = this.#has_open_position ? '있음' : '없음';
        throw new BackendAdapterError(
            'SHUTDOWN_SAFETY_TIMEOUT',
            `안전 종료 대기 시간이 초과되었습니다. 열린 포지션: ${position_label}, 미체결 주문 또는 조정 상태를 확인한 뒤 다시 시도해 주세요.`,
            true,
        );
    }

    /**
     * 함수 이름: request_json()
     * 기능: Bearer/X-Request-Id와 command Idempotency-Key를 붙이고 공통 envelope를 decode한다.
     * 인자: method -> HTTP method, path -> exact endpoint/query
     *      validate_data -> success data validator, body -> optional JSON body
     *      is_command -> idempotency header 필요 여부, caller_signal -> optional 호출자 취소 신호
     *      timeout_ms -> 양수 timeout milliseconds, null이면 wall-clock timeout 미적용
     *      expected_success_status -> 성공 envelope에서 요구할 HTTP status 또는 null
     * 반환값: 검증된 success data Promise
     * 작성 날짜: 2026/08/21
     */
    private async request_json<Data>(
        method: 'GET' | 'POST' | 'PATCH',
        path: string,
        validate_data: (value: unknown) => Data,
        body?: Readonly<Record<string, unknown>>,
        is_command = false,
        caller_signal?: AbortSignal,
        timeout_ms: number | null = this.#request_timeout_ms,
        expected_success_status: number | null = null,
    ): Promise<Data> {
        if (this.#shutdown_outcome_is_ambiguous && path !== '/v1/shutdown') {
            throw new BackendAdapterError(
                'SHUTDOWN_OUTCOME_AMBIGUOUS',
                '백엔드 종료 결과를 확인 중입니다. 동일한 종료 요청만 다시 시도해 주세요.',
                true,
            );
        }

        const token = this.#session_token;
        if (token === null) {
            throw new BackendAdapterError(
                'ADAPTER_STOPPED',
                'Backend adapter has already stopped',
                false,
            );
        }

        const request_id = this.create_header_uuid('request');
        const serialized_body = body === undefined
            ? undefined
            : JSON.stringify(body);
        const command_fingerprint = is_command
            ? `${method}\n${path}\n${serialized_body ?? ''}`
            : null;
        const headers: Record<string, string> = {
            Authorization: `Bearer ${token}`,
            'X-Request-Id': request_id,
        };
        if (body !== undefined) {
            headers['Content-Type'] = 'application/json';
        }
        if (command_fingerprint !== null) {
            headers['Idempotency-Key'] = this.acquire_idempotency_key(
                command_fingerprint,
            );
        }

        const abort_controller = new AbortController();
        const abort_from_caller = () => {
            abort_controller.abort(CALLER_REQUEST_ABORT_REASON);
        };

        // Caller가 이미 취소됐거나 이후 취소되면 adapter 소유 controller에 같은 수명 경계를 합성한다.
        if (caller_signal?.aborted === true) {
            abort_from_caller();
        } else {
            caller_signal?.addEventListener('abort', abort_from_caller, { once: true });
        }
        this.#active_abort_controllers.add(abort_controller);
        // Streaming CSV는 시간 상한 없이 기다리되 active controller로 adapter stop은 계속 수용한다.
        const timeout_handle = timeout_ms === null
            ? null
            : globalThis.setTimeout(() => {
                abort_controller.abort(REQUEST_TIMEOUT_ABORT_REASON);
            }, timeout_ms);
        let response_status: number | null = null;

        try {
            if (abort_controller.signal.aborted) {
                throw abort_controller.signal.reason;
            }

            const response = await this.#fetch(`${this.#http_origin}${path}`, {
                method,
                headers,
                signal: abort_controller.signal,
                ...(serialized_body === undefined ? {} : { body: serialized_body }),
            });
            response_status = response.status;
            let response_json: unknown;

            try {
                response_json = await response.json() as unknown;
            } catch {
                throw new BackendAdapterError(
                    'MALFORMED_BACKEND_RESPONSE',
                    'Backend returned a malformed JSON response',
                    response.status >= 500,
                );
            }

            const decoded_data = decode_backend_http_envelope(
                response_json,
                request_id,
                validate_data,
            );
            if (expected_success_status !== null
                && response.status !== expected_success_status) {
                throw new BackendContractError(
                    'MALFORMED_BACKEND_RESPONSE',
                    'Backend command returned an unexpected HTTP success status',
                );
            }
            if (command_fingerprint !== null) {
                this.#pending_idempotency_keys.delete(command_fingerprint);
            }
            return decoded_data;
        } catch (error) {
            if (abort_controller.signal.reason === CALLER_REQUEST_ABORT_REASON) {
                throw new BackendAdapterError(
                    'BACKEND_REQUEST_CANCELLED',
                    'Backend request was cancelled',
                    false,
                );
            }
            if (abort_controller.signal.reason === ADAPTER_STOP_ABORT_REASON) {
                throw new BackendAdapterError(
                    'ADAPTER_STOPPED',
                    'Backend adapter has already stopped',
                    false,
                );
            }
            if (abort_controller.signal.reason === REQUEST_TIMEOUT_ABORT_REASON) {
                throw new BackendAdapterError(
                    'BACKEND_REQUEST_TIMEOUT',
                    'Backend request timed out',
                    true,
                );
            }
            if (error instanceof BackendCommandError) {
                const is_definitive_client_failure = !error.retryable
                    && response_status !== null
                    && response_status < 500;

                // retryable·5xx 응답은 commit 여부가 불명확하므로 같은 key로 복구하게 보존한다.
                if (command_fingerprint !== null && is_definitive_client_failure) {
                    this.#pending_idempotency_keys.delete(command_fingerprint);
                }
                throw error;
            }
            if (error instanceof BackendContractError
                || error instanceof BackendAdapterError) {
                throw error;
            }

            throw new BackendAdapterError(
                'BACKEND_UNREACHABLE',
                'Backend is unavailable',
                true,
            );
        } finally {
            caller_signal?.removeEventListener('abort', abort_from_caller);
            if (timeout_handle !== null) {
                globalThis.clearTimeout(timeout_handle);
            }
            this.#active_abort_controllers.delete(abort_controller);
        }
    }

    /**
     * 함수 이름: acquire_idempotency_key()
     * 기능: 응답 결과가 불명인 동일 command fingerprint에 같은 UUID를 재사용한다.
     * 인자: command_fingerprint -> method, path와 exact JSON body 결합값
     * 반환값: 기존 ambiguous UUID 또는 새 canonical UUID
     * 작성 날짜: 2026/08/21
     */
    private acquire_idempotency_key(command_fingerprint: string): string {
        const existing_key = this.#pending_idempotency_keys.get(command_fingerprint);
        if (existing_key !== undefined) {
            return existing_key;
        }
        if (this.#pending_idempotency_keys.size >= MAX_PENDING_IDEMPOTENCY_KEYS) {
            throw new BackendAdapterError(
                'IDEMPOTENCY_STATE_EXHAUSTED',
                'Too many command outcomes are unresolved',
                false,
            );
        }

        // UUID를 먼저 검증한 뒤에만 ambiguous command table에 보존한다.
        const new_key = this.create_header_uuid('idempotency');
        this.#pending_idempotency_keys.set(command_fingerprint, new_key);
        return new_key;
    }

    /**
     * 함수 이름: create_header_uuid()
     * 기능: injected UUID factory 결과를 header에 넣기 전 canonical UUID로 검증한다.
     * 인자: purpose -> 오류 설명에만 쓰는 request 또는 idempotency 구분
     * 반환값: canonical UUID
     * 작성 날짜: 2026/08/21
     */
    private create_header_uuid(purpose: 'request' | 'idempotency'): string {
        const uuid = this.#create_uuid();

        if (!CANONICAL_UUID_PATTERN.test(uuid)) {
            throw new BackendAdapterError(
                'UUID_GENERATION_FAILED',
                `${purpose} UUID generation failed`,
                false,
            );
        }

        return uuid;
    }

    /**
     * 함수 이름: open_event_stream()
     * 기능: query/subprotocol 없는 socket을 만들고 onopen 첫 frame으로만 token을 전송한다.
     * 인자: 없음
     * 반환값: 없음
     * 작성 날짜: 2026/08/21
     */
    private open_event_stream(): void {
        if (this.#is_stopped || this.#session_token === null) {
            return;
        }

        const generation = ++this.#connection_generation;
        let web_socket: BackendWebSocket;
        try {
            // Browser constructor의 동기 보안·URL 실패도 callback lifecycle과 같은 typed 상태로 닫는다.
            web_socket = this.#create_web_socket(this.#web_socket_url);
        } catch {
            this.fail_closed(new BackendAdapterError(
                'EVENT_STREAM_CONNECTION_FAILED',
                'Backend event stream connection failed',
                true,
            ));
            return;
        }
        this.#web_socket = web_socket;

        web_socket.onopen = () => {
            if (!this.is_current_generation(generation, web_socket)) {
                return;
            }

            const token = this.#session_token;
            if (token === null) {
                return;
            }

            // AUTHENTICATE는 반드시 첫 client frame이며 token은 URL/subprotocol에 존재하지 않는다.
            const authentication_message: BackendAuthenticateMessage = {
                schema_version: BACKEND_SCHEMA_VERSION,
                type: 'AUTHENTICATE',
                token,
                after_sequence: this.#last_sequence,
            };
            try {
                web_socket.send(JSON.stringify(authentication_message));
            } catch {
                this.fail_closed(new BackendAdapterError(
                    'EVENT_STREAM_AUTHENTICATION_FAILED',
                    'Backend event stream authentication failed',
                    true,
                ));
            }
        };
        web_socket.onmessage = (message_event) => {
            if (!this.is_current_generation(generation, web_socket)
                || this.#is_resynchronizing) {
                return;
            }

            this.handle_event_frame(message_event.data);
        };
        web_socket.onerror = () => {
            // Browser WebSocket error에는 안전한 진단 정보가 없으므로 close callback이 resync를 시작한다.
        };
        web_socket.onclose = () => {
            if (!this.is_current_generation(generation, web_socket)) {
                return;
            }

            this.#web_socket = null;
            if (this.#shutdown_is_in_flight) {
                // 202와 stream close 순서를 판별할 수 있도록 request-local 사실을 보존한다.
                this.#shutdown_stream_closed_during_request = true;
                return;
            }
            if (this.#shutdown_was_accepted || this.#shutdown_outcome_is_ambiguous) {
                // Backend는 202 body보다 stream을 먼저 닫을 수 있으므로 accepted 판정 전 resync도 억제한다.
                return;
            }
            void this.full_resynchronize('EVENT_STREAM_CLOSED');
        };
    }

    /**
     * 함수 이름: handle_event_frame()
     * 기능: schema/session/ID/sequence를 검증해 dedup, exact-next 적용 또는 full resync를 결정한다.
     * 인자: frame_data -> browser WebSocket message data
     * 반환값: 없음
     * 작성 날짜: 2026/08/21
     */
    private handle_event_frame(frame_data: unknown): void {
        try {
            const parsed_message = parse_backend_web_socket_message(frame_data);

            const message_session_id = parsed_message.kind === 'event'
                ? parsed_message.event.session_id
                : parsed_message.control.session_id;
            if (message_session_id !== this.#active_session_id) {
                void this.full_resynchronize('SESSION_CHANGED');
                return;
            }
            if (parsed_message.kind === 'resync_required') {
                void this.full_resynchronize(parsed_message.control.reason);
                return;
            }

            const event = parsed_message.event;
            if (event.session_id !== this.#active_session_id) {
                void this.full_resynchronize('SESSION_CHANGED');
                return;
            }
            if (event.sequence <= this.#last_sequence) {
                return;
            }
            if (this.#seen_event_ids.has(event.event_id)) {
                if (event.sequence === this.#last_sequence + 1) {
                    // ADR-005 event-id duplicate no-op은 payload를 재적용하지 않고 cursor만 소비한다.
                    this.#last_sequence = event.sequence;
                } else {
                    void this.full_resynchronize('SEQUENCE_GAP');
                }
                return;
            }
            if (event.sequence !== this.#last_sequence + 1) {
                void this.full_resynchronize('SEQUENCE_GAP');
                return;
            }

            const intents = map_backend_event_to_intents(event);
            const applicable_intents = this.synchronize_command_state_from_intents(intents);
            this.#callbacks?.on_event(applicable_intents, event);
            this.#last_sequence = event.sequence;
            this.remember_event_id(event.event_id);
        } catch (error) {
            this.fail_closed(this.normalize_adapter_failure(error));
        }
    }

    /**
     * 함수 이름: synchronize_command_state_from_intents()
     * 기능: 비동기 trading completion event의 version과 ratio를 다음 command 기준으로 반영한다.
     * 인자: intents -> runtime validation을 통과한 backend event mapping 결과
     * 반환값: stale lifecycle update가 제거된 facade intent 목록
     * 작성 날짜: 2026/08/21
     */
    private synchronize_command_state_from_intents(
        intents: ReadonlyArray<UiApplicationIntent>,
    ): ReadonlyArray<UiApplicationIntent> {
        let applicable_intents = intents;
        const selection_intent = applicable_intents.find((intent) => {
            return intent.type === 'REGIME_SELECTION_SYNCHRONIZED';
        });

        if (selection_intent?.type === 'REGIME_SELECTION_SYNCHRONIZED') {
            if (this.#trading_version !== null
                && selection_intent.version < this.#trading_version) {
                // 늦게 온 과거 selection은 UI와 다음 start command 양쪽에서 함께 제외한다.
                applicable_intents = applicable_intents.filter((intent) => {
                    return intent.type !== 'REGIME_SELECTION_SYNCHRONIZED';
                });
            } else {
                if (selection_intent.version === this.#trading_version
                    && this.#selected_regime !== null
                    && selection_intent.selected !== this.#selected_regime) {
                    throw new BackendContractError(
                        'MALFORMED_BACKEND_PAYLOAD',
                        'A trading version cannot contain conflicting regime selections',
                    );
                }

                // Selection과 version을 같은 event에서 적용해 start가 서로 다른 시점을 섞지 않게 한다.
                this.#selected_regime = selection_intent.selected;
                this.#trading_version = selection_intent.version;
            }
        }

        const trading_intent = applicable_intents.find((intent) => {
            return intent.type === 'TRADING_SESSION_SYNCHRONIZED';
        });

        if (trading_intent?.type !== 'TRADING_SESSION_SYNCHRONIZED') {
            return applicable_intents;
        }
        if (this.#trading_version !== null
            && trading_intent.version < this.#trading_version) {
            // HTTP command result보다 늦게 온 과거 aggregate event는 sequence만 소비하고 UI에는 재적용하지 않는다.
            return applicable_intents.filter((intent) => {
                return intent.type !== 'TRADING_SESSION_SYNCHRONIZED';
            });
        }

        // Event의 같은 aggregate version에서 온 두 ratio를 함께 교체해 한쪽만 stale해지지 않게 한다.
        this.#trading_version = trading_intent.version;
        this.#trading_status = trading_intent.status;
        this.#has_open_position = trading_intent.has_open_position;
        this.#scale_in_ratio = trading_intent.scale_in;
        this.#scale_out_ratio = trading_intent.scale_out;
        return applicable_intents;
    }

    /**
     * 함수 이름: full_resynchronize()
     * 기능: 기존 cache/socket을 이어 붙이지 않고 새 전체 snapshot을 적용한 뒤 그 sequence부터 재연결한다.
     * 인자: reason -> secret이 없는 reconnect 원인 code
     * 반환값: resync 완료 Promise
     * 작성 날짜: 2026/08/21
     */
    private async full_resynchronize(reason: string): Promise<void> {
        if (this.#is_stopped || this.#is_resynchronizing) {
            return;
        }

        this.#is_resynchronizing = true;
        this.#connection_generation += 1;
        this.close_current_socket('full resync');
        this.clear_seen_events();
        this.#callbacks?.on_reconnecting(reason);

        try {
            const snapshot = await this.load_snapshot();
            if (this.#is_stopped) {
                return;
            }
            if (snapshot.session_id !== this.session_id) {
                throw new BackendAdapterError(
                    'SESSION_MISMATCH',
                    'Backend snapshot session does not match the launch descriptor',
                    false,
                );
            }

            // Snapshot callback이 facade의 server-owned state를 원자적으로 전체 교체한다.
            this.#callbacks?.on_full_resync(snapshot);
            this.#active_session_id = snapshot.session_id;
            this.#last_sequence = snapshot.last_sequence;
            this.#is_resynchronizing = false;
            this.open_event_stream();
        } catch (error) {
            this.#is_resynchronizing = false;
            this.fail_closed(this.normalize_adapter_failure(error));
        }
    }

    /**
     * 함수 이름: remember_event_id()
     * 기능: replay buffer와 같은 bounded 크기로 적용 event ID dedup set을 유지한다.
     * 인자: event_id -> 적용을 완료한 canonical event UUID
     * 반환값: 없음
     * 작성 날짜: 2026/08/21
     */
    private remember_event_id(event_id: string): void {
        this.#seen_event_ids.add(event_id);
        this.#event_id_order.push(event_id);

        if (this.#event_id_order.length > MAX_TRACKED_EVENT_IDS) {
            const expired_event_id = this.#event_id_order.shift();
            if (expired_event_id !== undefined) {
                this.#seen_event_ids.delete(expired_event_id);
            }
        }
    }

    /**
     * 함수 이름: clear_seen_events()
     * 기능: full snapshot 경계에서 이전 cache session의 event ID를 모두 버린다.
     * 인자: 없음
     * 반환값: 없음
     * 작성 날짜: 2026/08/21
     */
    private clear_seen_events(): void {
        this.#seen_event_ids.clear();
        this.#event_id_order.splice(0);
    }

    /**
     * 함수 이름: abort_active_requests()
     * 기능: stop/fail-closed 시 진행 중 HTTP closure가 token header 참조를 오래 보존하지 않게 모두 중단한다.
     * 인자: 없음
     * 반환값: 없음
     * 작성 날짜: 2026/08/21
     */
    private abort_active_requests(): void {
        this.#active_abort_controllers.forEach((abort_controller) => {
            abort_controller.abort(ADAPTER_STOP_ABORT_REASON);
        });
        this.#active_abort_controllers.clear();
    }

    /**
     * 함수 이름: close_current_socket()
     * 기능: callback을 먼저 분리한 뒤 secret 없는 normal close reason으로 socket을 닫는다.
     * 인자: reason -> client-local 안전한 close reason
     * 반환값: 없음
     * 작성 날짜: 2026/08/21
     */
    private close_current_socket(reason: string): void {
        const web_socket = this.#web_socket;
        this.#web_socket = null;

        if (web_socket === null) {
            return;
        }

        web_socket.onopen = null;
        web_socket.onmessage = null;
        web_socket.onclose = null;
        web_socket.onerror = null;
        web_socket.close(NORMAL_CLIENT_CLOSE_CODE, reason);
    }

    /**
     * 함수 이름: is_current_generation()
     * 기능: 이전 socket의 늦은 callback이 현재 sequence/cache를 변경하지 못하게 차단한다.
     * 인자: generation -> callback 생성 시점 generation
     *      web_socket -> callback을 보낸 socket identity
     * 반환값: 현재 live socket callback 여부
     * 작성 날짜: 2026/08/21
     */
    private is_current_generation(
        generation: number,
        web_socket: BackendWebSocket,
    ): boolean {
        return !this.#is_stopped
            && generation === this.#connection_generation
            && web_socket === this.#web_socket;
    }

    /**
     * 함수 이름: normalize_adapter_failure()
     * 기능: contract/command/network 오류를 raw payload 없이 lifecycle용 typed failure로 정규화한다.
     * 인자: error -> caught unknown 오류
     * 반환값: 안전한 BackendAdapterError
     * 작성 날짜: 2026/08/21
     */
    private normalize_adapter_failure(error: unknown): BackendAdapterError {
        if (error instanceof BackendAdapterError) {
            return error;
        }
        if (error instanceof BackendCommandError) {
            return new BackendAdapterError(error.code, error.message, error.retryable);
        }
        if (error instanceof BackendContractError) {
            return new BackendAdapterError(error.code, error.message, false);
        }

        return new BackendAdapterError(
            'BACKEND_EVENT_STREAM_FAILED',
            'Backend event stream failed',
            true,
        );
    }

    /**
     * 함수 이름: fail_closed()
     * 기능: malformed/unknown schema 또는 unrecoverable resync에서 socket을 닫고 callback에 안전한 failure만 전달한다.
     * 인자: error -> normalized adapter failure
     * 반환값: 없음
     * 작성 날짜: 2026/08/21
     */
    private fail_closed(error: BackendAdapterError): void {
        if (this.#is_stopped) {
            return;
        }

        this.#is_stopped = true;
        this.#connection_generation += 1;
        this.close_current_socket('fail closed');
        this.abort_active_requests();
        this.clear_seen_events();
        const callbacks = this.#callbacks;
        this.#callbacks = null;
        this.#session_token = null;
        callbacks?.on_failure(error);
    }

}
