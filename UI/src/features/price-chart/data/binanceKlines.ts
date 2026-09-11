import type { ChartInterval } from '../types';
import { ChartHttpError, describe_chart_data_error } from './chartDataError';
import type {
    KlinesByInterval,
    LoadAllKlinesOptions,
    LoadOlderKlinesOptions,
    NormalizedKline,
} from './types';

const binance_klines_rest_url = 'https://data-api.binance.vision/api/v3/klines';
const binance_combined_stream_url = 'wss://data-stream.binance.vision/stream';

export const supported_chart_intervals = [
    '1m',
    '30m',
    '4h',
    '1d',
] as const satisfies ReadonlyArray<ChartInterval>;

/**
 * 함수 이름: is_record()
 * 기능: runtime 값이 문자열 key로 조회할 수 있는 객체인지 확인한다.
 * 인자: value -> 확인할 runtime 값
 * 반환값: null이 아닌 객체이면 true
 * 작성 날짜: 2026/08/20
 */
function is_record(value: unknown): value is Record<string, unknown> {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * 함수 이름: normalize_symbol()
 * 기능: Binance symbol을 대문자로 정규화하고 안전한 형식인지 확인한다.
 * 인자: symbol -> REST 조회와 WebSocket 구독에 사용할 symbol
 * 반환값: 대문자로 정규화된 symbol
 * 작성 날짜: 2026/08/20
 */
function normalize_symbol(symbol: string): string {
    const normalized_symbol = symbol.trim().toUpperCase();

    if (!/^[A-Z0-9]{2,20}$/.test(normalized_symbol)) {
        throw new Error('Binance symbol must contain 2 to 20 letters or digits.');
    }

    return normalized_symbol;
}

/**
 * 함수 이름: is_chart_interval()
 * 기능: runtime 값이 UI가 지원하는 Binance kline 주기인지 확인한다.
 * 인자: value -> 확인할 runtime 값
 * 반환값: 1m, 30m, 4h, 1d 중 하나이면 true
 * 작성 날짜: 2026/08/20
 */
function is_chart_interval(value: unknown): value is ChartInterval {
    return typeof value === 'string'
        && supported_chart_intervals.some((interval) => interval === value);
}

/**
 * 함수 이름: read_integer()
 * 기능: Binance payload의 필드가 유한한 정수인지 검증한다.
 * 인자: value -> 검증할 값
 *      field_name -> 오류에 표시할 필드 이름
 * 반환값: 검증된 정수
 * 작성 날짜: 2026/08/20
 */
function read_integer(value: unknown, field_name: string): number {
    if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) {
        throw new Error(`Binance kline field "${field_name}" must be a non-negative safe integer.`);
    }

    return value;
}

/**
 * 함수 이름: read_decimal_string()
 * 기능: Binance payload의 decimal 문자열을 유한한 number로 변환한다.
 * 인자: value -> 변환할 문자열
 *      field_name -> 오류에 표시할 필드 이름
 * 반환값: 유한한 number로 변환된 decimal
 * 작성 날짜: 2026/08/20
 */
function read_decimal_string(value: unknown, field_name: string): number {
    if (typeof value !== 'string' || value.trim() === '') {
        throw new Error(`Binance kline field "${field_name}" must be a decimal string.`);
    }

    const decimal_value = Number(value);

    if (!Number.isFinite(decimal_value)) {
        throw new Error(`Binance kline field "${field_name}" must contain a finite decimal.`);
    }

    return decimal_value;
}

/**
 * 함수 이름: validate_rest_metadata()
 * 기능: REST kline tuple에서 사용하지 않는 공식 metadata 필드도 계약과 일치하는지 검증한다.
 * 인자: row -> Binance REST kline tuple
 *      row_index -> 오류에 표시할 tuple 위치
 * 반환값: 없음
 * 작성 날짜: 2026/08/20
 */
function validate_rest_metadata(row: ReadonlyArray<unknown>, row_index: number): void {
    read_decimal_string(row[7], `rows[${row_index}][7]`);
    read_integer(row[8], `rows[${row_index}][8]`);
    read_decimal_string(row[9], `rows[${row_index}][9]`);
    read_decimal_string(row[10], `rows[${row_index}][10]`);

    if (typeof row[11] !== 'string') {
        throw new Error(`Binance kline field "rows[${row_index}][11]" must be a string.`);
    }
}

/**
 * 함수 이름: parse_rest_kline_row()
 * 기능: Binance REST kline tuple을 공통 NormalizedKline으로 변환한다.
 * 인자: value -> 변환할 REST tuple
 *      row_index -> payload 내 tuple 위치
 *      symbol -> 조회한 symbol
 *      interval -> 조회한 chart 주기
 *      current_time -> 현재 시각의 Unix millisecond
 * 반환값: 정규화된 kline
 * 작성 날짜: 2026/08/20
 */
function parse_rest_kline_row(
    value: unknown,
    row_index: number,
    symbol: string,
    interval: ChartInterval,
    current_time: number,
): NormalizedKline {
    if (!Array.isArray(value) || value.length !== 12) {
        throw new Error(`Binance kline row ${row_index} must be a 12-field tuple.`);
    }

    const open_time = read_integer(value[0], `rows[${row_index}][0]`);
    const close_time = read_integer(value[6], `rows[${row_index}][6]`);

    if (close_time < open_time) {
        throw new Error(`Binance kline row ${row_index} closes before it opens.`);
    }

    validate_rest_metadata(value, row_index);

    return {
        symbol,
        interval,
        open_time,
        close_time,
        open: read_decimal_string(value[1], `rows[${row_index}][1]`),
        high: read_decimal_string(value[2], `rows[${row_index}][2]`),
        low: read_decimal_string(value[3], `rows[${row_index}][3]`),
        close: read_decimal_string(value[4], `rows[${row_index}][4]`),
        volume: read_decimal_string(value[5], `rows[${row_index}][5]`),
        is_closed: close_time <= current_time,
    };
}

/**
 * 함수 이름: parse_rest_klines()
 * 기능: Binance REST response payload 전체를 검증하고 kline 목록으로 변환한다.
 * 인자: value -> response JSON payload
 *      symbol -> 조회한 symbol
 *      interval -> 조회한 chart 주기
 * 반환값: 정규화된 kline 목록
 * 작성 날짜: 2026/08/20
 */
function parse_rest_klines(
    value: unknown,
    symbol: string,
    interval: ChartInterval,
): ReadonlyArray<NormalizedKline> {
    if (!Array.isArray(value)) {
        throw new Error(`Binance ${interval} kline payload must be an array.`);
    }

    const current_time = Date.now();

    return value.map((row, row_index) => parse_rest_kline_row(
        row,
        row_index,
        symbol,
        interval,
        current_time,
    ));
}

/**
 * 함수 이름: with_chart_request_timeout()
 * 기능: 응답 body를 포함한 REST 작업을 15초로 제한하고 종료 시 timer·listener를 정리한다.
 * 인자: operation -> 취소 가능한 요청, signal -> 상위 연결의 취소 signal
 * 반환값: 요청 결과 또는 시간 제한·취소 오류
 * 작성 날짜: 2026/09/11
 */
async function with_chart_request_timeout<T>(
    operation: (signal: AbortSignal) => Promise<T>, signal?: AbortSignal,
): Promise<T> {
    const controller = new AbortController();
    let reject_cancellation: (reason: unknown) => void = () => undefined;
    const cancelled = new Promise<never>((resolve, reject) => { reject_cancellation = reject; });
    const abort = () => {
        reject_cancellation(new DOMException('Chart request aborted', 'AbortError'));
        controller.abort();
    };
    signal?.addEventListener('abort', abort, { once: true });
    const timer = setTimeout(() => {
        reject_cancellation(new DOMException('Chart request deadline exceeded', 'TimeoutError'));
        controller.abort();
    }, 15_000);
    try {
        if (signal?.aborted) abort();
        return await Promise.race([cancelled, operation(controller.signal)]);
    } finally {
        clearTimeout(timer);
        signal?.removeEventListener('abort', abort);
    }
}

/**
 * 함수 이름: fetch_interval_klines()
 * 기능: 공식 Binance public REST endpoint에서 단일 주기의 kline을 조회한다.
 * 인자: symbol -> 조회할 symbol
 *      interval -> 조회할 chart 주기
 *      limit -> 요청할 kline 수
 *      signal -> 요청 취소 signal
 *      fetch_implementation -> 사용할 fetch 구현
 *      end_time -> 이 시각까지의 kline만 요청하는 선택적 Unix millisecond
 * 반환값: 정규화된 kline 목록 Promise
 * 작성 날짜: 2026/08/20
 */
async function fetch_interval_klines(
    symbol: string,
    interval: ChartInterval,
    limit: number,
    signal: AbortSignal | undefined,
    fetch_implementation: typeof fetch,
    end_time?: number,
): Promise<ReadonlyArray<NormalizedKline>> {
    const request_url = new URL(binance_klines_rest_url);
    request_url.searchParams.set('symbol', symbol);
    request_url.searchParams.set('interval', interval);
    request_url.searchParams.set('limit', String(limit));

    if (end_time !== undefined) {
        request_url.searchParams.set('endTime', String(end_time));
    }

    return with_chart_request_timeout(async (request_signal) => {
        const response = await fetch_implementation(request_url, { signal: request_signal });

        if (!response.ok) {
            throw new ChartHttpError(response.status, interval);
        }

        let response_payload: unknown;

        try {
            response_payload = await response.json();
        } catch (error: unknown) {
            throw new Error(`Binance ${interval} kline response is not valid JSON.`, { cause: error });
        }

        return parse_rest_klines(response_payload, symbol, interval);
    }, signal);
}

/**
 * 함수 이름: load_all_klines()
 * 기능: 1m, 30m, 4h, 1d REST kline을 병렬로 조회해 주기별로 반환한다.
 * 인자: symbol -> 조회할 Binance symbol
 *      options -> limit, AbortSignal, fetch 구현 옵션
 * 반환값: 네 주기의 정규화된 kline 목록 Promise
 * 작성 날짜: 2026/08/20
 */
export async function load_all_klines(
    symbol = 'ETHUSDT',
    options: LoadAllKlinesOptions = {},
): Promise<KlinesByInterval> {
    const normalized_symbol = normalize_symbol(symbol);
    const limit = options.limit ?? 500;

    if (!Number.isInteger(limit) || limit < 1 || limit > 1000) {
        throw new Error('Binance kline limit must be an integer from 1 to 1000.');
    }

    const fetch_implementation = options.fetch_implementation ?? globalThis.fetch;

    if (typeof fetch_implementation !== 'function') {
        throw new Error('A fetch implementation is required to load Binance klines.');
    }

    const interval_results = await Promise.all(supported_chart_intervals.map(async (interval) => {
        const started_at = performance.now();
        options.on_request_event?.({ event: 'rest_started', interval, elapsed_ms: 0 });
        try {
            const klines = await fetch_interval_klines(
                normalized_symbol, interval, limit, options.signal, fetch_implementation,
            );
            options.on_request_event?.({ event: 'rest_completed', interval, elapsed_ms: Math.round(performance.now() - started_at) });
            return [interval, klines] as const;
        } catch (error: unknown) {
            if (!((error instanceof Error || error instanceof DOMException) && error.name === 'AbortError')) {
                options.on_request_event?.({
                    event: (error instanceof Error || error instanceof DOMException) && error.name === 'TimeoutError' ? 'rest_timeout' : 'rest_failed',
                    interval, elapsed_ms: Math.round(performance.now() - started_at),
                    ...describe_chart_data_error(error),
                });
            }
            throw error;
        }
    }));

    return Object.fromEntries(interval_results) as unknown as KlinesByInterval;
}

/**
 * 함수 이름: load_older_klines()
 * 기능: 기준 봉보다 이전인 단일 주기의 Binance kline을 최대 1000개 조회한다.
 * 인자: symbol -> 조회할 Binance symbol
 *      interval -> 조회할 chart 주기
 *      before_open_time -> 결과에서 제외할 기준 봉의 open time
 *      options -> limit, AbortSignal, fetch 구현 옵션
 * 반환값: 기준 봉보다 이전인 정규화된 kline 목록 Promise
 * 작성 날짜: 2026/08/20
 */
export async function load_older_klines(
    symbol: string,
    interval: ChartInterval,
    before_open_time: number,
    options: LoadOlderKlinesOptions = {},
): Promise<ReadonlyArray<NormalizedKline>> {
    const normalized_symbol = normalize_symbol(symbol);
    const limit = options.limit ?? 1000;

    if (!is_chart_interval(interval)) {
        throw new Error('Binance kline interval is not supported.');
    }

    if (!Number.isSafeInteger(before_open_time) || before_open_time < 1) {
        throw new Error('Binance kline before open time must be a positive safe integer.');
    }

    if (!Number.isInteger(limit) || limit < 1 || limit > 1000) {
        throw new Error('Binance kline limit must be an integer from 1 to 1000.');
    }

    const fetch_implementation = options.fetch_implementation ?? globalThis.fetch;

    if (typeof fetch_implementation !== 'function') {
        throw new Error('A fetch implementation is required to load Binance klines.');
    }

    return fetch_interval_klines(
        normalized_symbol,
        interval,
        limit,
        options.signal,
        fetch_implementation,
        before_open_time - 1,
    );
}

/**
 * 함수 이름: build_combined_kline_stream_url()
 * 기능: UI가 지원하는 네 주기의 Binance combined kline stream URL을 만든다.
 * 인자: symbol -> 구독할 Binance symbol
 * 반환값: 1m, 30m, 4h, 1d stream을 포함한 WebSocket URL
 * 작성 날짜: 2026/08/20
 */
export function build_combined_kline_stream_url(symbol = 'ETHUSDT'): string {
    const stream_symbol = normalize_symbol(symbol).toLowerCase();
    const streams = supported_chart_intervals
        .map((interval) => `${stream_symbol}@kline_${interval}`)
        .join('/');

    return `${binance_combined_stream_url}?streams=${streams}`;
}

/**
 * 함수 이름: parse_json_payload()
 * 기능: WebSocket text payload를 JSON으로 해석하고 객체 payload는 그대로 반환한다.
 * 인자: payload -> WebSocket에서 수신한 text 또는 이미 해석된 값
 * 반환값: JSON으로 해석된 runtime 값
 * 작성 날짜: 2026/08/20
 */
function parse_json_payload(payload: unknown): unknown {
    if (typeof payload !== 'string') {
        return payload;
    }

    try {
        return JSON.parse(payload) as unknown;
    } catch (error: unknown) {
        throw new Error('Binance combined WebSocket payload is not valid JSON.', { cause: error });
    }
}

/**
 * 함수 이름: parse_combined_kline_message()
 * 기능: Binance combined WebSocket kline payload를 검증하고 NormalizedKline으로 변환한다.
 * 인자: payload -> combined stream의 text 또는 JSON payload
 * 반환값: 정규화된 kline
 * 작성 날짜: 2026/08/20
 */
export function parse_combined_kline_message(payload: unknown): NormalizedKline {
    const parsed_payload = parse_json_payload(payload);

    if (!is_record(parsed_payload) || typeof parsed_payload.stream !== 'string') {
        throw new Error('Binance combined WebSocket payload must contain a stream name.');
    }

    if (!is_record(parsed_payload.data) || parsed_payload.data.e !== 'kline') {
        throw new Error('Binance combined WebSocket payload must contain a kline event.');
    }

    const event_symbol = typeof parsed_payload.data.s === 'string'
        ? normalize_symbol(parsed_payload.data.s)
        : null;
    const kline_payload = parsed_payload.data.k;

    if (event_symbol === null || !is_record(kline_payload)) {
        throw new Error('Binance combined WebSocket kline event is missing symbol or kline data.');
    }

    const kline_symbol = typeof kline_payload.s === 'string'
        ? normalize_symbol(kline_payload.s)
        : null;
    const interval = kline_payload.i;

    if (kline_symbol === null || kline_symbol !== event_symbol) {
        throw new Error('Binance combined WebSocket event and kline symbols must match.');
    }

    if (!is_chart_interval(interval)) {
        throw new Error('Binance combined WebSocket kline interval is not supported.');
    }

    const expected_stream = `${event_symbol.toLowerCase()}@kline_${interval}`;

    if (parsed_payload.stream.toLowerCase() !== expected_stream) {
        throw new Error('Binance combined WebSocket stream does not match its kline payload.');
    }

    if (typeof kline_payload.x !== 'boolean') {
        throw new Error('Binance combined WebSocket kline field "x" must be boolean.');
    }

    const open_time = read_integer(kline_payload.t, 'k.t');
    const close_time = read_integer(kline_payload.T, 'k.T');

    if (close_time < open_time) {
        throw new Error('Binance combined WebSocket kline closes before it opens.');
    }

    return {
        symbol: event_symbol,
        event_time: read_integer(parsed_payload.data.E, 'E'),
        interval,
        open_time,
        close_time,
        open: read_decimal_string(kline_payload.o, 'k.o'),
        high: read_decimal_string(kline_payload.h, 'k.h'),
        low: read_decimal_string(kline_payload.l, 'k.l'),
        close: read_decimal_string(kline_payload.c, 'k.c'),
        volume: read_decimal_string(kline_payload.v, 'k.v'),
        is_closed: kline_payload.x,
    };
}
