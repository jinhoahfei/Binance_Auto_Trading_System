import type {
    BackendAuthenticateMessage,
    BackendEventEnvelope,
    BackendSnapshot,
    BackendTradingLogicSupportStatus,
    BackendTradingStatus,
} from '../contracts';
import { BACKEND_SCHEMA_VERSION } from '../contracts';
import type {
    CsvExportOptions,
    CsvExportReceipt,
    RegimeType,
    TradeHistoryQuery,
    TradeRecord,
} from '../contracts';
import type { UiApplicationIntent } from '../../app/control';
import type { TradingCommandReceipt, UiCommandPort } from '../ports';
import {
    BackendCommandError,
    BackendContractError,
    decode_backend_http_envelope,
    map_backend_event_to_intents,
    map_trade_record,
    parse_backend_web_socket_message,
    validate_backend_snapshot,
    validate_trade_snapshot,
} from './backendEventMapper';

const CANONICAL_UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
const MAX_TRACKED_EVENT_IDS = 10_000;
const MAX_PENDING_IDEMPOTENCY_KEYS = 128;
const NORMAL_CLIENT_CLOSE_CODE = 1000;
const DEFAULT_REQUEST_TIMEOUT_MS = 5_000;
const UNIT_INTERVAL_RATIO_PATTERN = /^(?:0(?:\.[0-9]+)?|1(?:\.0+)?)$/u;
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
 * transport 의존성을 browser global 대신 test에서 결정적으로 주입하는 옵션이다.
 */
export interface BackendUiAdapterDependencies {
    readonly fetch?: typeof fetch;
    readonly create_web_socket?: (url: string) => BackendWebSocket;
    readonly create_uuid?: () => string;
    readonly request_timeout_ms?: number;
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

/**
 * 함수 이름: validate_trade_page()
 * 기능: 미래 trade query 성공값의 rows와 row_count가 서로 일치하는지 검증한다.
 * 인자: value -> `/v1/trades` 성공 data
 * 반환값: 계산 없이 mapping한 UI trade 목록
 * 작성 날짜: 2026/08/21
 */
function validate_trade_page(value: unknown): ReadonlyArray<TradeRecord> {
    if (typeof value !== 'object' || value === null || Array.isArray(value)) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'Backend trade page must be an object',
        );
    }

    const page = value as Record<string, unknown>;
    if (!Array.isArray(page.rows)
        || !Number.isSafeInteger(page.row_count)
        || page.row_count !== page.rows.length) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'Backend trade page is invalid',
        );
    }

    return page.rows.map((row) => map_trade_record(validate_trade_snapshot(row)));
}

/**
 * 함수 이름: validate_csv_receipt()
 * 기능: 미래 CSV command 성공값을 기존 UiCommandPort receipt로 검증한다.
 * 인자: value -> `/v1/csv-exports` 성공 data
 * 반환값: 검증된 CSV receipt
 * 작성 날짜: 2026/08/21
 */
function validate_csv_receipt(value: unknown): CsvExportReceipt {
    if (typeof value !== 'object' || value === null || Array.isArray(value)) {
        throw new BackendContractError(
            'MALFORMED_BACKEND_PAYLOAD',
            'Backend CSV receipt must be an object',
        );
    }

    const receipt = value as Record<string, unknown>;
    if (typeof receipt.file_path !== 'string'
        || receipt.file_path.length === 0
        || !Number.isSafeInteger(receipt.exported_row_count)
        || (receipt.exported_row_count as number) < 0) {
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
    readonly #request_timeout_ms: number;
    readonly #http_origin: string;
    readonly #web_socket_url: string;

    #session_token: string | null;
    #active_session_id: string;
    #last_sequence = 0;
    #web_socket: BackendWebSocket | null = null;
    #callbacks: BackendUiAdapterCallbacks | null = null;
    #is_stopped = true;
    #is_resynchronizing = false;
    #connection_generation = 0;
    #trading_version: number | null = null;
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

        if (!Number.isSafeInteger(request_timeout_ms) || request_timeout_ms <= 0) {
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
        this.#request_timeout_ms = request_timeout_ms;
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
     * 기능: period/side query를 URLSearchParams로 직렬화하고 typed unavailable 또는 검증된 rows를 반환한다.
     * 인자: query -> UI trade history filter
     * 반환값: 검증된 UI trade 목록 Promise
     * 작성 날짜: 2026/08/21
     */
    async load_trade_history(
        query: TradeHistoryQuery,
    ): Promise<ReadonlyArray<TradeRecord>> {
        const query_string = new URLSearchParams({
            period: query.period,
            side: query.side,
        }).toString();

        return this.request_json(
            'GET',
            `/v1/trades?${query_string}`,
            validate_trade_page,
        );
    }

    /**
     * 함수 이름: pick_csv_directory()
     * 기능: native picker가 Phase 12 전에는 fake path를 반환하지 않고 typed unavailable을 전파한다.
     * 인자: 없음
     * 반환값: 항상 rejected Promise
     * 작성 날짜: 2026/08/21
     */
    async pick_csv_directory(): Promise<string | null> {
        throw new BackendCommandError(
            'FEATURE_NOT_AVAILABLE',
            'Directory selection is not available in the current application phase.',
            false,
        );
    }

    /**
     * 함수 이름: export_csv()
     * 기능: Phase 5 fail-closed CSV endpoint에 authenticated command를 전달하고 미래 success는 검증한다.
     * 인자: options -> UI가 검증한 CSV export option
     * 반환값: backend typed receipt Promise
     * 작성 날짜: 2026/08/21
     */
    async export_csv(options: CsvExportOptions): Promise<CsvExportReceipt> {
        void options;
        return this.request_json(
            'POST',
            '/v1/csv-exports',
            validate_csv_receipt,
            { schema_version: BACKEND_SCHEMA_VERSION },
            true,
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
        await this.request_command('/v1/shutdown', 'POST');

        // Backend가 종료를 수락한 뒤에만 renderer의 launch token 참조를 폐기한다.
        this.stop();
    }

    /**
     * 함수 이름: request_command()
     * 기능: 구체 success DTO가 없는 Phase command를 공통 authenticated envelope로 실행한다.
     * 인자: path -> exact `/v1/*` command path, method -> POST 또는 PATCH
     * 반환값: 성공 시 void Promise
     * 작성 날짜: 2026/08/21
     */
    private async request_command(path: string, method: 'POST' | 'PATCH'): Promise<void> {
        await this.request_json(
            method,
            path,
            validate_success_object,
            { schema_version: BACKEND_SCHEMA_VERSION },
            true,
        );
    }

    /**
     * 함수 이름: request_json()
     * 기능: Bearer/X-Request-Id와 command Idempotency-Key를 붙이고 공통 envelope를 decode한다.
     * 인자: method -> HTTP method, path -> exact endpoint/query
     *      validate_data -> success data validator, body -> optional JSON body
     *      is_command -> idempotency header 필요 여부
     * 반환값: 검증된 success data Promise
     * 작성 날짜: 2026/08/21
     */
    private async request_json<Data>(
        method: 'GET' | 'POST' | 'PATCH',
        path: string,
        validate_data: (value: unknown) => Data,
        body?: Readonly<Record<string, unknown>>,
        is_command = false,
    ): Promise<Data> {
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
        this.#active_abort_controllers.add(abort_controller);
        const timeout_handle = globalThis.setTimeout(() => {
            abort_controller.abort();
        }, this.#request_timeout_ms);
        let response_status: number | null = null;

        try {
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
            if (command_fingerprint !== null) {
                this.#pending_idempotency_keys.delete(command_fingerprint);
            }
            return decoded_data;
        } catch (error) {
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
                abort_controller.signal.aborted
                    ? 'BACKEND_REQUEST_TIMEOUT'
                    : 'BACKEND_UNREACHABLE',
                abort_controller.signal.aborted
                    ? 'Backend request timed out'
                    : 'Backend is unavailable',
                true,
            );
        } finally {
            globalThis.clearTimeout(timeout_handle);
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
            abort_controller.abort();
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
