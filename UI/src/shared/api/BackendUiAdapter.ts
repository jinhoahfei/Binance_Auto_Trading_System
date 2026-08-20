import type {
    BackendAuthenticateMessage,
    BackendEventEnvelope,
    BackendSnapshot,
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
import type { UiCommandPort } from '../ports';
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
const NORMAL_CLIENT_CLOSE_CODE = 1000;
const DEFAULT_REQUEST_TIMEOUT_MS = 5_000;

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
    readonly #seen_event_ids = new Set<string>();
    readonly #event_id_order: Array<string> = [];
    readonly #active_abort_controllers = new Set<AbortController>();

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
        return this.request_json(
            'GET',
            '/v1/snapshot',
            validate_backend_snapshot,
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
        this.#session_token = null;
    }

    /**
     * 함수 이름: start_trading()
     * 기능: Phase 5 fail-closed trading start endpoint에 authenticated command를 전달한다.
     * 인자: regime_type -> 후속 Phase command DTO가 소유할 선택 REGIME
     * 반환값: backend typed 결과 Promise
     * 작성 날짜: 2026/08/21
     */
    async start_trading(regime_type: RegimeType): Promise<void> {
        void regime_type;
        await this.request_command('/v1/trading/start', 'POST');
    }

    /**
     * 함수 이름: stop_trading()
     * 기능: Phase 5 fail-closed trading stop endpoint에 authenticated command를 전달한다.
     * 인자: 없음
     * 반환값: backend typed 결과 Promise
     * 작성 날짜: 2026/08/21
     */
    async stop_trading(): Promise<void> {
        await this.request_command('/v1/trading/stop', 'POST');
    }

    /**
     * 함수 이름: force_sell_and_stop()
     * 기능: 별도 Phase owner가 없는 force-sell stop을 일반 stop 성공으로 가장하지 않고 typed 실패시킨다.
     * 인자: 없음
     * 반환값: 항상 rejected Promise
     * 작성 날짜: 2026/08/21
     */
    async force_sell_and_stop(): Promise<void> {
        throw new BackendCommandError(
            'FEATURE_NOT_AVAILABLE',
            'Force-sell stop is not available in the current application phase.',
            false,
        );
    }

    /**
     * 함수 이름: apply_regime()
     * 기능: Phase 5 fail-closed REGIME selection endpoint에 authenticated command를 전달한다.
     * 인자: regime_type -> 사용자가 선택한 canonical UI REGIME
     * 반환값: backend typed 결과 Promise
     * 작성 날짜: 2026/08/21
     */
    async apply_regime(regime_type: RegimeType): Promise<void> {
        void regime_type;
        await this.request_command('/v1/regime/selection', 'POST');
    }

    /**
     * 함수 이름: update_split_order()
     * 기능: slider number를 엄격히 검증한 뒤 future DTO를 추측하지 않고 fail-closed endpoint를 호출한다.
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

        // 실제 ratio wire DTO는 Phase 7 owner가 확정하므로 stub에는 schema만 전달한다.
        await this.request_command('/v1/trading/split-ratios', 'PATCH');
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
        const headers: Record<string, string> = {
            Authorization: `Bearer ${token}`,
            'X-Request-Id': request_id,
        };
        if (body !== undefined) {
            headers['Content-Type'] = 'application/json';
        }
        if (is_command) {
            headers['Idempotency-Key'] = this.create_header_uuid('idempotency');
        }

        const abort_controller = new AbortController();
        this.#active_abort_controllers.add(abort_controller);
        const timeout_handle = globalThis.setTimeout(() => {
            abort_controller.abort();
        }, this.#request_timeout_ms);

        try {
            const response = await this.#fetch(`${this.#http_origin}${path}`, {
                method,
                headers,
                signal: abort_controller.signal,
                ...(body === undefined ? {} : { body: JSON.stringify(body) }),
            });
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

            return decode_backend_http_envelope(
                response_json,
                request_id,
                validate_data,
            );
        } catch (error) {
            if (error instanceof BackendCommandError
                || error instanceof BackendContractError
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
            this.#callbacks?.on_event(intents, event);
            this.#last_sequence = event.sequence;
            this.remember_event_id(event.event_id);
        } catch (error) {
            this.fail_closed(this.normalize_adapter_failure(error));
        }
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
