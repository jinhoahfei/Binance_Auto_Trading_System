import { earned_balance_fixture } from './balanceReconciliationFixture';
import { shutdown_preparation_fixture } from './shutdownTestFixtures';
import { waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { BACKEND_SCHEMA_VERSION } from '../contracts';
import type { CsvExportOptions } from '../contracts';
import type { BackendUiAdapterCallbacks, BackendWebSocket } from './BackendUiAdapter';
import {
    BackendAdapterError,
    BackendUiAdapter,
} from './BackendUiAdapter';
import { BackendCommandError } from './backendEventMapper';
import {
    create_backend_account_fixture,
    create_backend_event_fixture,
    create_backend_snapshot_fixture,
    TEST_BACKEND_EVENT_ID,
    TEST_BACKEND_SESSION_ID,
    TEST_BACKEND_TOKEN,
} from './backendTestFixtures';

const TEST_REQUEST_ID = 'f5a4f621-25f8-4dd2-bfb7-1b80e9561423';
const TEST_IDEMPOTENCY_ID = '2522ef0c-d88d-42b3-a22f-fc7bdd09a662';
const SECOND_EVENT_ID = '06a59589-0aed-44fa-8983-7246aeb4c619';
const THIRD_EVENT_ID = 'c9ca6590-9d50-436d-8648-e8cc9ef7957f';
const CSV_EXPORT_OPTIONS: CsvExportOptions = {
    directory: '/Users/oscar/Exports',
    file_name: 'binance_trades_2026-08-23.csv',
    period: 'last7days',
    start_date: '2026-08-17',
    end_date: '2026-08-23',
    timezone: 'Asia/Seoul',
};

/**
 * 클래스 이름: FakeBackendWebSocket
 * 기능: adapter test에서 first frame, server event와 close lifecycle을 수동 제어한다.
 * 작성 날짜: 2026/08/21
 */
class FakeBackendWebSocket implements BackendWebSocket {
    readonly sent_frames: Array<string> = [];
    readonly close_calls: Array<{ readonly code?: number; readonly reason?: string }> = [];
    onopen: ((event: Event) => void) | null = null;
    onmessage: ((event: MessageEvent<unknown>) => void) | null = null;
    onclose: ((event: CloseEvent) => void) | null = null;
    onerror: ((event: Event) => void) | null = null;

    constructor(readonly url: string) {}

    /**
     * 함수 이름: send()
     * 기능: adapter가 보낸 client text frame을 순서대로 기록한다.
     * 인자: data -> WebSocket text frame
     * 반환값: 없음
     * 작성 날짜: 2026/08/21
     */
    send(data: string): void {
        this.sent_frames.push(data);
    }

    /**
     * 함수 이름: close()
     * 기능: adapter의 secret 없는 close code와 reason을 기록한다.
     * 인자: code -> close code, reason -> close reason
     * 반환값: 없음
     * 작성 날짜: 2026/08/21
     */
    close(code?: number, reason?: string): void {
        this.close_calls.push({
            ...(code === undefined ? {} : { code }),
            ...(reason === undefined ? {} : { reason }),
        });
    }

    /**
     * 함수 이름: emit_open()
     * 기능: browser open event를 adapter handler에 전달한다.
     * 인자: 없음
     * 반환값: 없음
     * 작성 날짜: 2026/08/21
     */
    emit_open(): void {
        this.onopen?.(new Event('open'));
    }

    /**
     * 함수 이름: emit_message()
     * 기능: JSON object를 server text frame으로 adapter handler에 전달한다.
     * 인자: payload -> JSON 직렬화할 event/control object
     * 반환값: 없음
     * 작성 날짜: 2026/08/21
     */
    emit_message(payload: object): void {
        this.onmessage?.(new MessageEvent('message', {
            data: JSON.stringify(payload),
        }));
    }

    /**
     * 함수 이름: emit_close()
     * 기능: unexpected browser close event를 adapter handler에 전달한다.
     * 인자: 없음
     * 반환값: 없음
     * 작성 날짜: 2026/08/21
     */
    emit_close(): void {
        this.onclose?.(new CloseEvent('close'));
    }
}

/**
 * 함수 이름: create_descriptor()
 * 기능: 모든 adapter test가 공유할 valid loopback launch descriptor를 만든다.
 * 인자: 없음
 * 반환값: valid descriptor object
 * 작성 날짜: 2026/08/21
 */
function create_descriptor() {
    return {
        port: 42_123,
        session_id: TEST_BACKEND_SESSION_ID,
        schema_version: BACKEND_SCHEMA_VERSION,
        token: TEST_BACKEND_TOKEN,
    } as const;
}

/**
 * 함수 이름: create_uuid_factory()
 * 기능: HTTP request와 idempotency header에 쓸 결정적 UUID sequence를 반환한다.
 * 인자: 없음
 * 반환값: UUID factory
 * 작성 날짜: 2026/08/21
 */
function create_uuid_factory(): () => string {
    const ids = [TEST_REQUEST_ID, TEST_IDEMPOTENCY_ID];
    let index = 0;

    return () => {
        const next_id = ids[index % ids.length];
        index += 1;
        return next_id!;
    };
}

/**
 * 함수 이름: create_success_response()
 * 기능: 요청 header UUID와 동일한 backend success envelope Response를 만든다.
 * 인자: request_id -> X-Request-Id 값, data -> endpoint success data
 * 반환값: JSON Response
 * 작성 날짜: 2026/08/21
 */
function create_success_response(
    request_id: string,
    data: unknown,
    status = 200,
): Response {
    return new Response(JSON.stringify({
        schema_version: BACKEND_SCHEMA_VERSION,
        request_id,
        ok: true,
        data,
    }), { status });
}

/**
 * 함수 이름: create_failure_response()
 * 기능: Phase 5 unavailable command와 같은 typed failure envelope Response를 만든다.
 * 인자: request_id -> X-Request-Id 값, code -> typed failure code
 *      retryable -> 같은 command 재시도 가능 여부, status -> HTTP status
 * 반환값: JSON Response
 * 작성 날짜: 2026/08/21
 */
function create_failure_response(
    request_id: string,
    code = 'FEATURE_NOT_AVAILABLE',
    retryable = false,
    status = 503,
    details: Readonly<Record<string, unknown>> = {},
): Response {
    return new Response(JSON.stringify({
        schema_version: BACKEND_SCHEMA_VERSION,
        request_id,
        ok: false,
        error: {
            code,
            message: 'This feature is not available in the current application phase.',
            retryable,
            details,
        },
    }), { status });
}

/**
 * 함수 이름: request_headers()
 * 기능: fake fetch RequestInit에서 string record header를 안전하게 추출한다.
 * 인자: init -> fetch가 받은 RequestInit
 * 반환값: header record
 * 작성 날짜: 2026/08/21
 */
function request_headers(init: RequestInit | undefined): Record<string, string> {
    return init?.headers as Record<string, string>;
}

/**
 * 함수 이름: create_abortable_fetch_mock()
 * 기능: 전달된 composite signal을 기록하고 그 신호가 중단될 때 AbortError로 종료한다.
 * 인자: received_signals -> fetch별 signal 기록 배열
 * 반환값: 실제 네트워크 응답 없이 abort를 관찰하는 fetch test double
 * 작성 날짜: 2026/08/23
 */
function create_abortable_fetch_mock(
    received_signals: Array<AbortSignal>,
): typeof fetch {
    return vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        const signal = init?.signal;
        if (signal === undefined || signal === null) {
            throw new TypeError('Backend request signal is required');
        }

        received_signals.push(signal);
        return new Promise<Response>((_resolve, reject) => {
            const reject_aborted_request = () => {
                reject(new DOMException('Backend request aborted', 'AbortError'));
            };

            // 이미 중단된 signal과 이후 중단되는 signal을 같은 fetch 실패 경로로 수렴시킨다.
            if (signal.aborted) {
                reject_aborted_request();
                return;
            }
            signal.addEventListener('abort', reject_aborted_request, { once: true });
        });
    }) as unknown as typeof fetch;
}

/**
 * 함수 이름: create_callbacks()
 * 기능: event/resync/reconnect/failure 호출을 관찰할 기본 mock callbacks를 만든다.
 * 인자: 없음
 * 반환값: typed callback mock 묶음
 * 작성 날짜: 2026/08/21
 */
function create_callbacks(): BackendUiAdapterCallbacks {
    return {
        on_event: vi.fn(),
        on_full_resync: vi.fn(),
        on_reconnecting: vi.fn(),
        on_failure: vi.fn(),
    };
}

/**
 * 함수 이름: create_trade_details_fixture()
 * 기능: generated primitive를 결합한 `/v1/trades` 정상 data fixture를 만든다.
 * 인자: 없음
 * 반환값: today/all query의 strict composite 상세 응답
 * 작성 날짜: 2026/08/23
 */
function create_trade_details_fixture() {
    const snapshot = create_backend_snapshot_fixture();

    return {
        query: {
            period: 'today',
            side: 'all',
            start_date: '2026-08-23',
            end_date: '2026-08-23',
        },
        rows: snapshot.recent_trades,
        row_count: snapshot.recent_trades.length,
        summary: {
            holdings_asset: 'ETH',
            holdings: '1.75',
            account_version: snapshot.account.version,
            performance: snapshot.performance,
        },
    };
}

describe('BackendUiAdapter HTTP contract', () => {
    it('Binance 진단은 loopback backend에서 API와 두 WebSocket 상태를 독립적으로 읽는다', async () => {
        const connection_status = { api: 'online', market_stream: 'offline', account_stream: 'online' };
        const fetch_mock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
            return create_success_response(request_headers(init)['X-Request-Id']!, connection_status);
        });
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: fetch_mock as typeof fetch,
            create_uuid: create_uuid_factory(),
        });

        await expect(adapter.load_binance_connection_status()).resolves.toEqual(connection_status);
        expect(fetch_mock.mock.calls[0]?.[0]).toBe('http://127.0.0.1:42123/v1/binance/connection-status');
        expect(fetch_mock.mock.calls[0]?.[1]?.method).toBe('GET');
    });

    it.each([
        null,
        { api: true, market_stream: 'online', account_stream: 'online' },
        { api: 'online', market_stream: 'online' },
        { api: 'online', market_stream: 'online', account_stream: 'online', extra: true },
    ])('잘못된 Binance 연결 진단을 정상 연결로 표시하지 않는다: %j', async (payload) => {
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
                return create_success_response(request_headers(init)['X-Request-Id']!, payload);
            }) as typeof fetch,
            create_uuid: create_uuid_factory(),
        });

        await expect(adapter.load_binance_connection_status()).rejects.toMatchObject({
            code: 'MALFORMED_BACKEND_PAYLOAD',
        });
    });

    it('test_show_trade_details_adapter_contract: combined query와 generated composite 응답을 UI details로 변환한다', async () => {
        const fetch_mock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
            return create_success_response(
                request_headers(init)['X-Request-Id']!,
                create_trade_details_fixture(),
            );
        });
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: fetch_mock as typeof fetch,
            create_uuid: create_uuid_factory(),
        });

        await expect(adapter.load_trade_history({
            period: 'today',
            side: 'all',
        })).resolves.toMatchObject({
            records: [expect.objectContaining({
                id: create_backend_snapshot_fixture().recent_trades[0]?.trade_id,
                quote_asset: 'USDT',
            })],
            summary: {
                position: { quantity: '1.7500 ETH' },
                dailyReturn: expect.any(Object),
                sellPerformance: expect.any(Object),
                fees: expect.any(Object),
            },
        });
        expect(fetch_mock.mock.calls[0]?.[0]).toBe(
            'http://127.0.0.1:42123/v1/trades?period=today&side=all',
        );
        expect(fetch_mock.mock.calls[0]?.[1]?.method).toBe('GET');
    });

    it('test_trade_history_filter_adapter_contract: all query의 concrete LocalDate range를 허용한다', async () => {
        const trade_details = create_trade_details_fixture();
        const fetch_mock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
            return create_success_response(request_headers(init)['X-Request-Id']!, {
                ...trade_details,
                query: {
                    period: 'all',
                    side: 'sell',
                    start_date: '0001-01-01',
                    end_date: '2026-08-23',
                },
            });
        });
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: fetch_mock as typeof fetch,
            create_uuid: create_uuid_factory(),
        });

        await expect(adapter.load_trade_history({
            period: 'all',
            side: 'sell',
        })).resolves.toHaveProperty('records');
    });

    it('trade-history caller signal을 내부 timeout/stop signal과 합성하고 HTTP read를 중단한다', async () => {
        const received_signals: Array<AbortSignal> = [];
        const caller_abort_controller = new AbortController();
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: create_abortable_fetch_mock(received_signals),
            create_uuid: create_uuid_factory(),
        });

        const request_promise = adapter.load_trade_history({
            period: 'today',
            side: 'all',
        }, caller_abort_controller.signal);
        expect(received_signals).toHaveLength(1);
        expect(received_signals[0]).not.toBe(caller_abort_controller.signal);

        caller_abort_controller.abort();

        await expect(request_promise).rejects.toMatchObject({
            code: 'BACKEND_REQUEST_CANCELLED',
            retryable: false,
        });
        expect(received_signals[0]?.aborted).toBe(true);
    });

    it('adapter stop이 caller signal과 독립적으로 진행 중 trade-history read를 중단한다', async () => {
        const received_signals: Array<AbortSignal> = [];
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: create_abortable_fetch_mock(received_signals),
            create_uuid: create_uuid_factory(),
        });

        const request_promise = adapter.load_trade_history({
            period: 'today',
            side: 'all',
        });
        expect(received_signals).toHaveLength(1);

        adapter.stop();

        await expect(request_promise).rejects.toMatchObject({
            code: 'ADAPTER_STOPPED',
            retryable: false,
        });
        expect(received_signals[0]?.aborted).toBe(true);
    });

    it('caller 취소가 없으면 기존 timeout이 진행 중 trade-history read를 중단한다', async () => {
        vi.useFakeTimers();
        try {
            const received_signals: Array<AbortSignal> = [];
            const adapter = new BackendUiAdapter(create_descriptor(), {
                fetch: create_abortable_fetch_mock(received_signals),
                create_uuid: create_uuid_factory(),
                request_timeout_ms: 50,
            });
            const request_promise = adapter.load_trade_history({
                period: 'today',
                side: 'all',
            });
            const rejection_assertion = expect(request_promise).rejects.toMatchObject({
                code: 'BACKEND_REQUEST_TIMEOUT',
                retryable: true,
            });

            await vi.advanceTimersByTimeAsync(50);

            await rejection_assertion;
            expect(received_signals[0]?.aborted).toBe(true);
        } finally {
            vi.useRealTimers();
        }
    });

    it.each([
        {
            label: 'echoed period 불일치',
            mutate: (details: ReturnType<typeof create_trade_details_fixture>) => {
                details.query.period = 'last7days';
            },
        },
        {
            label: 'preset inclusive date 범위 불일치',
            mutate: (details: ReturnType<typeof create_trade_details_fixture>) => {
                details.query.start_date = '2026-08-22';
            },
        },
        {
            label: 'row_count 불일치',
            mutate: (details: ReturnType<typeof create_trade_details_fixture>) => {
                details.row_count = 0;
            },
        },
        {
            label: '1000행 page 상한 초과',
            mutate: (details: ReturnType<typeof create_trade_details_fixture>) => {
                details.rows = Array.from(
                    { length: 1_001 },
                    () => details.rows[0]!,
                );
                details.row_count = details.rows.length;
            },
        },
        {
            label: 'ETH 외 holdings asset',
            mutate: (details: ReturnType<typeof create_trade_details_fixture>) => {
                details.summary.holdings_asset = 'BTC';
            },
        },
        {
            label: '음수 holdings',
            mutate: (details: ReturnType<typeof create_trade_details_fixture>) => {
                details.summary.holdings = '-0.1';
            },
        },
        {
            label: '음수 account version',
            mutate: (details: ReturnType<typeof create_trade_details_fixture>) => {
                details.summary.account_version = -1;
            },
        },
        {
            label: 'summary unknown key',
            mutate: (details: ReturnType<typeof create_trade_details_fixture>) => {
                Object.assign(details.summary, { future_value: 'unexpected' });
            },
        },
        {
            label: 'performance unknown key',
            mutate: (details: ReturnType<typeof create_trade_details_fixture>) => {
                Object.assign(details.summary.performance, {
                    future_value: 'unexpected',
                });
            },
        },
        {
            label: 'trade row unknown key',
            mutate: (details: ReturnType<typeof create_trade_details_fixture>) => {
                Object.assign(details.rows[0]!, { future_value: 'unexpected' });
            },
        },
    ])('trade details strict validator가 $label payload를 fail closed한다', async ({ mutate }) => {
        const trade_details = create_trade_details_fixture();
        mutate(trade_details);
        const fetch_mock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
            return create_success_response(
                request_headers(init)['X-Request-Id']!,
                trade_details,
            );
        });
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: fetch_mock as typeof fetch,
            create_uuid: create_uuid_factory(),
        });

        await expect(adapter.load_trade_history({
            period: 'today',
            side: 'all',
        })).rejects.toMatchObject({ code: 'MALFORMED_BACKEND_PAYLOAD' });
    });

    it.each([
        {
            label: '폴더 선택',
            native_result: '/Users/oscar/Exports',
            expected_result: '/Users/oscar/Exports',
        },
        {
            label: '사용자 취소',
            native_result: null,
            expected_result: null,
        },
    ])('native picker의 $label 결과를 손실 없이 반환한다', async ({
        native_result,
        expected_result,
    }) => {
        const picker_mock = vi.fn(async (): Promise<unknown> => native_result);
        const adapter = new BackendUiAdapter(create_descriptor(), {
            pick_csv_directory: picker_mock,
        });

        await expect(adapter.pick_csv_directory()).resolves.toBe(expected_result);
        expect(picker_mock).toHaveBeenCalledTimes(1);
    });

    it.each([
        undefined,
        42,
        { directory: '/Users/oscar/Exports' },
        ['/Users/oscar/Exports'],
    ])('native picker의 string/null 외 결과 %j를 fail closed한다', async (native_result) => {
        const adapter = new BackendUiAdapter(create_descriptor(), {
            pick_csv_directory: vi.fn(async (): Promise<unknown> => native_result),
        });

        await expect(adapter.pick_csv_directory()).rejects.toMatchObject({
            code: 'MALFORMED_NATIVE_PICKER_RESULT',
        });
    });

    it('native picker typed failure를 변형하거나 경로를 추가하지 않고 전파한다', async () => {
        const native_failure = {
            code: 'CSV_EXPORT_DIRECTORY_INVALID',
            message: 'The selected CSV export directory is invalid.',
        };
        const adapter = new BackendUiAdapter(create_descriptor(), {
            pick_csv_directory: vi.fn(async () => Promise.reject(native_failure)),
        });

        await expect(adapter.pick_csv_directory()).rejects.toBe(native_failure);
    });

    it('CSV option 전체와 schema version을 POST하고 strict success receipt를 반환한다', async () => {
        const receipt = {
            file_path: '/Users/oscar/Exports/binance_trades_2026-08-23.csv',
            exported_row_count: 17,
        };
        const fetch_mock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
            return create_success_response(request_headers(init)['X-Request-Id']!, receipt);
        });
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: fetch_mock as typeof fetch,
            create_uuid: create_uuid_factory(),
        });

        await expect(adapter.export_csv(CSV_EXPORT_OPTIONS)).resolves.toEqual(receipt);
        expect(fetch_mock.mock.calls[0]?.[0]).toBe(
            'http://127.0.0.1:42123/v1/csv-exports',
        );
        expect(fetch_mock.mock.calls[0]?.[1]?.method).toBe('POST');
        expect(JSON.parse(fetch_mock.mock.calls[0]?.[1]?.body as string) as unknown).toEqual({
            schema_version: BACKEND_SCHEMA_VERSION,
            ...CSV_EXPORT_OPTIONS,
        });
    });

    it.each([
        {
            label: '누락 key',
            receipt: { file_path: '/Users/oscar/Exports/trades.csv' },
        },
        {
            label: 'unknown key',
            receipt: {
                file_path: '/Users/oscar/Exports/trades.csv',
                exported_row_count: 1,
                future_value: 'unexpected',
            },
        },
        {
            label: '빈 path',
            receipt: { file_path: '   ', exported_row_count: 1 },
        },
        {
            label: '0 row',
            receipt: { file_path: '/Users/oscar/Exports/trades.csv', exported_row_count: 0 },
        },
        {
            label: '음수 row',
            receipt: { file_path: '/Users/oscar/Exports/trades.csv', exported_row_count: -1 },
        },
        {
            label: '비정수 row',
            receipt: { file_path: '/Users/oscar/Exports/trades.csv', exported_row_count: 1.5 },
        },
        {
            label: 'unsafe row',
            receipt: {
                file_path: '/Users/oscar/Exports/trades.csv',
                exported_row_count: Number.MAX_SAFE_INTEGER + 1,
            },
        },
    ])('CSV receipt의 $label를 fail closed한다', async ({ receipt }) => {
        const fetch_mock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
            return create_success_response(request_headers(init)['X-Request-Id']!, receipt);
        });
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: fetch_mock as typeof fetch,
            create_uuid: create_uuid_factory(),
        });

        await expect(adapter.export_csv(CSV_EXPORT_OPTIONS)).rejects.toMatchObject({
            code: 'MALFORMED_BACKEND_PAYLOAD',
        });
    });

    it('CSV backend typed failure를 receipt로 오인하지 않고 보존한다', async () => {
        const fetch_mock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
            return create_failure_response(
                request_headers(init)['X-Request-Id']!,
                'DESTINATION_EXISTS',
                false,
                409,
            );
        });
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: fetch_mock as typeof fetch,
            create_uuid: create_uuid_factory(),
        });

        await expect(adapter.export_csv(CSV_EXPORT_OPTIONS)).rejects.toMatchObject({
            code: 'DESTINATION_EXISTS',
            retryable: false,
        });
    });

    it('CSV export는 일반 timeout을 적용하지 않고 adapter stop에서만 중단한다', async () => {
        vi.useFakeTimers();
        try {
            const received_signals: Array<AbortSignal> = [];
            const adapter = new BackendUiAdapter(create_descriptor(), {
                fetch: create_abortable_fetch_mock(received_signals),
                create_uuid: create_uuid_factory(),
                request_timeout_ms: 50,
            });
            const request_promise = adapter.export_csv(CSV_EXPORT_OPTIONS);
            const rejection_assertion = expect(request_promise).rejects.toMatchObject({
                code: 'ADAPTER_STOPPED',
                retryable: false,
            });

            // 기존 CSV 300초 상한을 넘겨도 streaming command의 signal은 살아 있어야 한다.
            await vi.advanceTimersByTimeAsync(600_000);
            expect(received_signals).toHaveLength(1);
            expect(received_signals[0]?.aborted).toBe(false);

            adapter.stop();

            await rejection_assertion;
            expect(received_signals[0]?.aborted).toBe(true);
        } finally {
            vi.useRealTimers();
        }
    });

    it('Bearer/X-Request-Id와 command Idempotency-Key를 보내고 typed failure를 보존한다', async () => {
        const snapshot = create_backend_snapshot_fixture();
        const fetch_mock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
            const headers = request_headers(init);

            return init?.method === 'GET'
                ? create_success_response(headers['X-Request-Id']!, snapshot)
                : create_failure_response(headers['X-Request-Id']!);
        });
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: fetch_mock as typeof fetch,
            create_uuid: create_uuid_factory(),
        });

        await expect(adapter.load_snapshot()).resolves.toEqual(snapshot);
        await expect(adapter.apply_regime('type2')).rejects.toBeInstanceOf(
            BackendCommandError,
        );

        const snapshot_call = fetch_mock.mock.calls[0];
        const command_call = fetch_mock.mock.calls[1];
        const snapshot_headers = request_headers(snapshot_call?.[1]);
        const command_headers = request_headers(command_call?.[1]);

        expect(snapshot_call?.[0]).toBe('http://127.0.0.1:42123/v1/snapshot');
        expect(snapshot_headers.Authorization).toBe(`Bearer ${TEST_BACKEND_TOKEN}`);
        expect(snapshot_headers['X-Request-Id']).toBe(TEST_REQUEST_ID);
        expect(snapshot_headers['Idempotency-Key']).toBeUndefined();
        expect(command_call?.[0]).toBe('http://127.0.0.1:42123/v1/regime/selection');
        expect(command_headers.Authorization).toBe(`Bearer ${TEST_BACKEND_TOKEN}`);
        expect(command_headers['Idempotency-Key']).toBe(TEST_REQUEST_ID);
        expect(command_call?.[1]?.body).toBe(JSON.stringify({
            schema_version: BACKEND_SCHEMA_VERSION,
            regime_type: 'type2',
            expected_version: 0,
        }));
    });

    it('응답이 불명인 동일 command retry에는 Idempotency-Key를 재사용한다', async () => {
        const snapshot = create_backend_snapshot_fixture();
        let command_attempt = 0;
        const fetch_mock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
            const request_id = request_headers(init)['X-Request-Id']!;
            if (init?.method === 'GET') {
                return create_success_response(request_id, snapshot);
            }

            command_attempt += 1;
            if (command_attempt === 1) {
                throw new TypeError('simulated connection loss after send');
            }
            return create_success_response(request_id, {
                selected: 'type0',
                support_status: 'supported',
                version: 1,
            });
        });
        const uuid_values = [
            TEST_REQUEST_ID,
            TEST_IDEMPOTENCY_ID,
            SECOND_EVENT_ID,
            THIRD_EVENT_ID,
        ];
        let uuid_index = 0;
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: fetch_mock as typeof fetch,
            create_uuid: () => uuid_values[uuid_index++]!,
        });

        await adapter.load_snapshot();
        await expect(adapter.apply_regime('type0')).rejects.toMatchObject({
            code: 'BACKEND_UNREACHABLE',
        });
        await expect(adapter.apply_regime('type0')).resolves.toBeUndefined();

        const first_command_headers = request_headers(fetch_mock.mock.calls[1]?.[1]);
        const retry_command_headers = request_headers(fetch_mock.mock.calls[2]?.[1]);
        expect(first_command_headers['X-Request-Id']).not.toBe(
            retry_command_headers['X-Request-Id'],
        );
        expect(first_command_headers['Idempotency-Key']).toBe(
            retry_command_headers['Idempotency-Key'],
        );
    });

    it('retryable backend 실패 뒤 동일 command가 같은 Idempotency-Key로 복구한다', async () => {
        const snapshot = create_backend_snapshot_fixture();
        let command_attempt = 0;
        const fetch_mock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
            const request_id = request_headers(init)['X-Request-Id']!;
            if (init?.method === 'GET') {
                return create_success_response(request_id, snapshot);
            }

            command_attempt += 1;
            if (command_attempt === 1) {
                return create_failure_response(
                    request_id,
                    'CONNECTION_NOT_READY',
                    true,
                    503,
                );
            }
            return create_success_response(request_id, {
                selected: 'type0',
                support_status: 'supported',
                version: 1,
            });
        });
        const retryable_uuid_values = [
            TEST_REQUEST_ID,
            TEST_IDEMPOTENCY_ID,
            SECOND_EVENT_ID,
            THIRD_EVENT_ID,
        ];
        let retryable_uuid_index = 0;
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: fetch_mock as typeof fetch,
            create_uuid: () => retryable_uuid_values[retryable_uuid_index++]!,
        });

        await adapter.load_snapshot();
        await expect(adapter.apply_regime('type0')).rejects.toMatchObject({
            code: 'CONNECTION_NOT_READY',
            retryable: true,
        });
        await expect(adapter.apply_regime('type0')).resolves.toBeUndefined();

        const first_headers = request_headers(fetch_mock.mock.calls[1]?.[1]);
        const retry_headers = request_headers(fetch_mock.mock.calls[2]?.[1]);
        expect(first_headers['Idempotency-Key']).toBe(
            retry_headers['Idempotency-Key'],
        );
        expect(first_headers['X-Request-Id']).not.toBe(
            retry_headers['X-Request-Id'],
        );
    });

    it('snapshot version을 이어 regime/start/split/authoritative stop DTO를 손실 없이 보낸다', async () => {
        const snapshot = create_backend_snapshot_fixture();
        const fetch_mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
            const request_id = request_headers(init)['X-Request-Id']!;
            const path = new URL(input.toString()).pathname;

            if (path === '/v1/snapshot') {
                return create_success_response(request_id, snapshot);
            }
            if (path === '/v1/regime/selection') {
                return create_success_response(request_id, {
                    selected: 'type0',
                    support_status: 'supported',
                    version: 1,
                });
            }
            if (path === '/v1/trading/start') {
                return create_success_response(request_id, {
                    version: 2,
                    status: 'running',
                    session_id: '62c511b2-ea5c-43ac-bc36-e96eb39c85aa',
                });
            }
            if (path === '/v1/trading/split-ratios') {
                return create_success_response(request_id, {
                    version: 3,
                    scale_in: '0.5',
                    scale_out: '0.25',
                });
            }

            return create_success_response(request_id, {
                version: 4,
                status: 'terminated',
                session_id: '62c511b2-ea5c-43ac-bc36-e96eb39c85aa',
            });
        });
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: fetch_mock as typeof fetch,
            create_uuid: create_uuid_factory(),
        });

        // Snapshot-first version을 각 성공 결과로 전진시켜 stale command를 renderer에서 만들지 않는다.
        await adapter.load_snapshot();
        await adapter.apply_regime('type0');
        await adapter.start_trading('type0');
        await adapter.update_split_order('scale_out', 25);
        await adapter.force_sell_and_stop();

        expect(fetch_mock.mock.calls.slice(1).map((call) => ({
            path: new URL(call[0].toString()).pathname,
            method: call[1]?.method,
            body: JSON.parse(call[1]?.body as string) as unknown,
            idempotency_key: request_headers(call[1])['Idempotency-Key'],
        }))).toEqual([
            {
                path: '/v1/regime/selection',
                method: 'POST',
                body: {
                    schema_version: BACKEND_SCHEMA_VERSION,
                    regime_type: 'type0',
                    expected_version: 0,
                },
                idempotency_key: TEST_REQUEST_ID,
            },
            {
                path: '/v1/trading/start',
                method: 'POST',
                body: {
                    schema_version: BACKEND_SCHEMA_VERSION,
                    expected_version: 1,
                },
                idempotency_key: TEST_REQUEST_ID,
            },
            {
                path: '/v1/trading/split-ratios',
                method: 'PATCH',
                body: {
                    schema_version: BACKEND_SCHEMA_VERSION,
                    scale_in: '0.5',
                    scale_out: '0.25',
                    expected_version: 2,
                },
                idempotency_key: TEST_REQUEST_ID,
            },
            {
                path: '/v1/trading/stop',
                method: 'POST',
                body: {
                    schema_version: BACKEND_SCHEMA_VERSION,
                    expected_version: 3,
                },
                idempotency_key: TEST_REQUEST_ID,
            },
        ]);
    });

    it('Phase 9: 복구 Position 청산을 전용 endpoint와 현재 version으로 요청한다', async () => {
        const base_snapshot = create_backend_snapshot_fixture();
        const recovered_snapshot = {
            ...base_snapshot,
            trading: {
                ...base_snapshot.trading,
                has_open_position: true,
            },
        };
        const fetch_mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
            const request_id = request_headers(init)['X-Request-Id']!;
            const path = new URL(input.toString()).pathname;

            if (path === '/v1/snapshot') {
                return create_success_response(request_id, recovered_snapshot);
            }

            return create_success_response(request_id, {
                version: 1,
                status: 'stopping',
                session_id: '62c511b2-ea5c-43ac-bc36-e96eb39c85aa',
            }, 202);
        });
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: fetch_mock as typeof fetch,
            create_uuid: create_uuid_factory(),
        });

        await adapter.load_snapshot();
        const receipt = await adapter.liquidate_recovered_position();

        // 정상 stop endpoint 대신 recovery 전용 경계에 optimistic version을 그대로 전달한다.
        expect(receipt).toEqual({
            version: 1,
            status: 'stopping',
            session_id: '62c511b2-ea5c-43ac-bc36-e96eb39c85aa',
        });
        expect(fetch_mock).toHaveBeenCalledTimes(2);
        const command_call = fetch_mock.mock.calls[1]!;
        expect(new URL(command_call[0].toString()).pathname).toBe(
            '/v1/trading/recovered-position/liquidate',
        );
        expect(command_call[1]?.method).toBe('POST');
        expect(JSON.parse(command_call[1]?.body as string)).toEqual({
            schema_version: BACKEND_SCHEMA_VERSION,
            expected_version: 0,
        });
        expect(request_headers(command_call[1])['Idempotency-Key']).toBeDefined();
    });

    it('selection 응답이 요청 REGIME과 다르면 local selection/version을 전진시키지 않는다', async () => {
        const snapshot = create_backend_snapshot_fixture();
        const fetch_mock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
            const request_id = request_headers(init)['X-Request-Id']!;

            return init?.method === 'GET'
                ? create_success_response(request_id, snapshot)
                : create_success_response(request_id, {
                    selected: 'type1',
                    support_status: 'unsupported',
                    version: 1,
                });
        });
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: fetch_mock as typeof fetch,
            create_uuid: create_uuid_factory(),
        });

        await adapter.load_snapshot();
        await expect(adapter.apply_regime('type0')).rejects.toMatchObject({
            code: 'MALFORMED_BACKEND_PAYLOAD',
        });
        await expect(adapter.start_trading('type1')).rejects.toMatchObject({
            code: 'REGIME_SELECTION_REQUIRED',
        });
        expect(fetch_mock).toHaveBeenCalledTimes(2);
    });

    it('selection 응답의 support 상태가 canonical coverage와 다르면 fail closed한다', async () => {
        const snapshot = create_backend_snapshot_fixture();
        const fetch_mock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
            const request_id = request_headers(init)['X-Request-Id']!;

            return init?.method === 'GET'
                ? create_success_response(request_id, snapshot)
                : create_success_response(request_id, {
                    selected: 'type0',
                    support_status: 'unsupported',
                    version: 1,
                });
        });
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: fetch_mock as typeof fetch,
            create_uuid: create_uuid_factory(),
        });

        await adapter.load_snapshot();
        await expect(adapter.apply_regime('type0')).rejects.toMatchObject({
            code: 'MALFORMED_BACKEND_PAYLOAD',
        });
    });

});

describe('BackendUiAdapter backend-owned shutdown', () => {
    function fixture(options: {
        state?: unknown;
        prepare?: (init: RequestInit) => unknown | Promise<unknown>;
        finish?: (init: RequestInit) => Response | Promise<Response>;
        wait?: () => Promise<unknown>;
    } = {}) {
        const paths: string[] = [];
        const fetch_mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
            const path = new URL(String(input)).pathname;
            paths.push(path);
            const id = request_headers(init)['X-Request-Id']!;
            if (path === '/v1/shutdown/state') return create_success_response(id,
                options.state ?? { session_id: TEST_BACKEND_SESSION_ID, version: 0, status: 'running' });
            if (path === '/v1/shutdown/prepare') return create_success_response(id,
                options.prepare ? await options.prepare(init!) : shutdown_preparation_fixture(), init?.method === 'POST' ? 202 : 200);
            if (path === '/v1/shutdown') return options.finish ? await options.finish(init!)
                : create_success_response(id, { accepted: true, status: 'accepted', version: 0 }, 202);
            throw new Error(`Unexpected endpoint ${path}`);
        });
        const wait = vi.fn(options.wait ?? (async () => ({ exited: true, code: 0 })));
        const adapter = new BackendUiAdapter(create_descriptor(), { fetch: fetch_mock as typeof fetch,
            create_uuid: () => crypto.randomUUID(), wait_for_sidecar_exit: wait,
            shutdown_poll_interval_ms: 1, shutdown_wait_timeout_ms: 100 });
        return { adapter, paths, fetch_mock, wait };
    }

    it.each(['normal', 'recovery'])('%s exit uses the same narrow APIs without dashboard or stream', async (mode) => {
        const f = fixture();
        await (mode === 'normal' ? f.adapter.shutdown_application() : f.adapter.shutdown_recovery_application());
        expect(f.paths).toEqual(['/v1/shutdown/state', '/v1/shutdown/prepare', '/v1/shutdown']);
        expect(JSON.parse(String(f.fetch_mock.mock.calls[1]![1]!.body))).toEqual({
            schema_version: BACKEND_SCHEMA_VERSION, expected_version: 0, liquidation_confirmed: false });
        expect(f.wait).toHaveBeenCalledOnce();
        expect(f.adapter.is_disposed).toBe(true);
        expect(JSON.stringify(f.adapter)).not.toContain(TEST_BACKEND_TOKEN);
    });

    it('coalesces clicks and publishes order/account/history progress before final exit', async () => {
        const stages = [shutdown_preparation_fixture({ phase: 'settling_orders', step: 'orders' }),
            shutdown_preparation_fixture({ phase: 'checking', step: 'account' }),
            shutdown_preparation_fixture({ phase: 'checking', step: 'history' }), shutdown_preparation_fixture()];
        const f = fixture({ prepare: () => stages.shift()! });
        const progress = vi.fn(); f.adapter.set_shutdown_progress_listener(progress);
        await Promise.all([f.adapter.shutdown_application(), f.adapter.shutdown_application()]);
        expect(progress.mock.calls.map(([step]) => step)).toEqual(['orders', 'account', 'history', 'complete']);
        expect(f.fetch_mock.mock.calls.filter(([input, init]) => String(input).endsWith('/prepare') && init?.method === 'POST')).toHaveLength(1);
        expect(f.wait).toHaveBeenCalledOnce();
    });

    it('a publication error does not prevent safe backend completion', async () => {
        const f = fixture(); f.adapter.set_shutdown_progress_listener(() => { throw Error('UI fault'); });
        await f.adapter.shutdown_application(); expect(f.wait).toHaveBeenCalledOnce();
    });

    it('lost prepare response reuses the exact body and key before polling its operation', async () => {
        let count = 0;
        const f = fixture({ prepare: () => { if (++count === 1) throw new TypeError('lost response'); return shutdown_preparation_fixture(); } });
        await f.adapter.shutdown_application();
        const calls = f.fetch_mock.mock.calls.filter(([input]) => String(input).endsWith('/prepare'));
        expect(calls).toHaveLength(2);
        expect(calls[0]![1]!.body).toBe(calls[1]![1]!.body);
        expect(request_headers(calls[0]![1])['Idempotency-Key']).toBe(request_headers(calls[1]![1])['Idempotency-Key']);
        expect(f.wait).toHaveBeenCalledOnce();
    });

    it.each(['SHUTDOWN_BALANCE_MISMATCH', 'SHUTDOWN_ORDER_UNRESOLVED', 'SHUTDOWN_ACCOUNT_UNREACHABLE', 'SHUTDOWN_HISTORY_SAVE_FAILED', 'SHUTDOWN_PREPARATION_TIMEOUT'])('%s keeps the process and gives a concrete reason', async (code) => {
        const f = fixture({ prepare: () => shutdown_preparation_fixture({ phase: 'blocked', step: 'account', reason_code: code, retryable: true }) });
        await expect(f.adapter.shutdown_application()).rejects.toMatchObject({ code, retryable: true });
        expect(f.wait).not.toHaveBeenCalled(); expect(f.adapter.is_disposed).toBe(false);
        expect(f.paths).not.toContain('/v1/shutdown'); f.adapter.stop();
    });

    it('automatically finishes with a verified Earn reward receipt on repeated clicks', async () => {
        const f = fixture({ prepare: () => shutdown_preparation_fixture({ balance_reconciliation: earned_balance_fixture }) });
        await Promise.all([f.adapter.shutdown_application(), f.adapter.shutdown_application()]);
        expect(f.paths.filter((path) => path === '/v1/shutdown')).toHaveLength(1);
        expect(f.wait).toHaveBeenCalledOnce();
    });

    it('a real difference displays its exact quantities and reason', async () => {
        const f = fixture({ prepare: () => shutdown_preparation_fixture({ phase: 'blocked', step: 'account',
            reason_code: 'SHUTDOWN_BALANCE_MISMATCH', balance_reconciliation: { ...earned_balance_fixture,
                status: 'mismatch', reason_code: 'BALANCE_UNEXPLAINED', difference_quantity: '0.00000001', exchange_spot_quantity: '0.00009602' } }) });
        await expect(f.adapter.shutdown_application()).rejects.toMatchObject({
            code: 'SHUTDOWN_BALANCE_MISMATCH', message: expect.stringContaining('차이 0.00000001 ETH'),
        });
        expect(f.paths).not.toContain('/v1/shutdown');
        f.adapter.stop();
    });

    it('rejects malformed reward details before final shutdown', async () => {
        const f = fixture({ prepare: () => ({ ...shutdown_preparation_fixture(),
            balance_reconciliation: { ...earned_balance_fixture, earn_rewards_quantity: 0.00000001 } }) });
        await expect(f.adapter.shutdown_application()).rejects.toMatchObject({ code: 'MALFORMED_BACKEND_PAYLOAD' });
        expect(f.paths).not.toContain('/v1/shutdown'); f.adapter.stop();
    });

    it('requires explicit liquidation consent and starts a fresh job after confirmation', async () => {
        const f = fixture({ prepare: (init) => JSON.parse(String(init.body)).liquidation_confirmed
            ? shutdown_preparation_fixture()
            : shutdown_preparation_fixture({ phase: 'blocked', step: 'account', reason_code: 'SHUTDOWN_LIQUIDATION_CONFIRMATION_REQUIRED' }) });
        await expect(f.adapter.shutdown_application()).rejects.toMatchObject({ code: 'SHUTDOWN_LIQUIDATION_CONFIRMATION_REQUIRED' });
        expect(f.wait).not.toHaveBeenCalled();
        await f.adapter.shutdown_application(true);
        expect(f.paths.filter((path) => path === '/v1/shutdown/state')).toHaveLength(2);
        expect(f.wait).toHaveBeenCalledOnce();
    });

    it.each([
        { session_id: SECOND_EVENT_ID, version: 0, status: 'not_started' },
        { session_id: TEST_BACKEND_SESSION_ID, version: -1, status: 'not_started' },
        { session_id: TEST_BACKEND_SESSION_ID, version: true, status: 'not_started' },
        { session_id: TEST_BACKEND_SESSION_ID, version: 0, status: 'unknown' },
        { session_id: TEST_BACKEND_SESSION_ID, version: 0, status: 'not_started', force: true },
    ])('rejects malformed shutdown state %j', async (state) => {
        const f = fixture({ state }); await expect(f.adapter.shutdown_recovery_application()).rejects.toBeDefined();
        expect(f.paths).toEqual(['/v1/shutdown/state']); expect(f.wait).not.toHaveBeenCalled(); f.adapter.stop();
    });

    it('rejects a changed operation ID and cannot finalize', async () => {
        let count = 0;
        const f = fixture({ prepare: () => ++count === 1
            ? shutdown_preparation_fixture({ phase: 'checking', step: 'account' })
            : shutdown_preparation_fixture({ operation_id: SECOND_EVENT_ID }) });
        await expect(f.adapter.shutdown_application()).rejects.toMatchObject({ code: 'MALFORMED_BACKEND_PAYLOAD' });
        expect(f.wait).not.toHaveBeenCalled(); f.adapter.stop();
    });

    it('refreshes a stale prepare version without resending any order', async () => {
        let count = 0;
        const f = fixture({ prepare: () => ++count === 1
            ? shutdown_preparation_fixture({ phase: 'blocked', reason_code: 'STALE_CONTEXT_VERSION', step: 'workers', retryable: true })
            : shutdown_preparation_fixture() });
        await f.adapter.shutdown_application();
        expect(f.paths.filter((path) => path === '/v1/shutdown/state')).toHaveLength(2);
        expect(f.wait).toHaveBeenCalledOnce();
    });

    it.each([-1, 1])('does not trust a final receipt with wrong version %s', async (version) => {
        const f = fixture({ finish: (init) => create_success_response(request_headers(init)['X-Request-Id']!, { accepted: true, status: 'accepted', version }, 202) });
        await expect(f.adapter.shutdown_application()).rejects.toMatchObject({ code: 'SHUTDOWN_OUTCOME_AMBIGUOUS' });
        expect(f.wait).not.toHaveBeenCalled(); expect(f.adapter.is_disposed).toBe(false); f.adapter.stop();
    });

    it('lost final response preserves the exact final request and bypasses preparation on retry', async () => {
        let attempts = 0;
        const f = fixture({ finish: (init) => { if (++attempts === 1) throw new TypeError('lost');
            return create_success_response(request_headers(init)['X-Request-Id']!, { accepted: true, status: 'accepted', version: 0 }, 202); } });
        await expect(f.adapter.shutdown_application()).rejects.toMatchObject({ code: 'SHUTDOWN_OUTCOME_AMBIGUOUS' });
        await expect(f.adapter.apply_regime('type0')).rejects.toBeDefined();
        await f.adapter.shutdown_application();
        const calls = f.fetch_mock.mock.calls.filter(([input]) => String(input).endsWith('/v1/shutdown'));
        expect(calls).toHaveLength(2);
        expect(calls[0]![1]!.body).toBe(calls[1]![1]!.body);
        expect(request_headers(calls[0]![1])['Idempotency-Key']).toBe(request_headers(calls[1]![1])['Idempotency-Key']);
        expect(f.paths.filter((path) => path.endsWith('/prepare'))).toHaveLength(1);
    });

    it('after accepted shutdown only retries native wait, retaining the token until actual exit', async () => {
        let attempts = 0;
        const f = fixture({ wait: async () => { if (++attempts === 1) throw { code: 'BACKEND_SIDECAR_EXIT_TIMEOUT' }; return { exited: true, code: 0 }; } });
        await expect(f.adapter.shutdown_application()).rejects.toMatchObject({ code: 'SIDECAR_EXIT_TIMEOUT' });
        expect(f.adapter.is_disposed).toBe(false); await f.adapter.shutdown_application();
        expect(f.paths.filter((path) => path === '/v1/shutdown')).toHaveLength(1);
        expect(f.wait).toHaveBeenCalledTimes(2); expect(f.adapter.is_disposed).toBe(true);
    });

    it('nonzero native exit is not treated as another wait timeout', async () => {
        const f = fixture({ wait: async () => { throw { code: 'BACKEND_SIDECAR_EXIT_FAILED' }; } });
        await expect(f.adapter.shutdown_application()).rejects.toMatchObject({ code: 'SIDECAR_ABNORMAL_EXIT', retryable: false });
        expect(f.adapter.is_disposed).toBe(false); f.adapter.stop();
    });
});

describe('BackendUiAdapter WebSocket lifecycle', () => {
    it('WebSocket constructor의 일시 실패는 token을 보존하고 재연결한다', async () => {
        const callbacks = create_callbacks();
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: vi.fn() as unknown as typeof fetch,
            create_uuid: create_uuid_factory(),
            create_web_socket: () => {
                throw new Error('constructor failure must not escape');
            },
        });

        // React activation 경계에는 예외를 던지지 않고 facade용 typed failure만 전달한다.
        expect(() => {
            adapter.start_live_events(create_backend_snapshot_fixture(), callbacks);
        }).not.toThrow();
        expect(callbacks.on_failure).not.toHaveBeenCalled();
        expect(callbacks.on_reconnecting).toHaveBeenCalledWith('EVENT_STREAM_CONNECTION_FAILED');
        expect(adapter.is_disposed).toBe(false);
        adapter.stop();
        expect(JSON.stringify(adapter)).not.toContain(TEST_BACKEND_TOKEN);
    });

    it('URL/subprotocol 없이 연결하고 onopen의 첫 AUTHENTICATE frame에만 token을 넣는다', () => {
        const sockets: Array<FakeBackendWebSocket> = [];
        const snapshot = create_backend_snapshot_fixture();
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: vi.fn() as unknown as typeof fetch,
            create_uuid: create_uuid_factory(),
            create_web_socket: (url) => {
                const socket = new FakeBackendWebSocket(url);
                sockets.push(socket);
                return socket;
            },
        });

        adapter.start_live_events(snapshot, create_callbacks());
        const socket = sockets[0]!;
        expect(socket.url).toBe('ws://127.0.0.1:42123/v1/events');
        expect(socket.url).not.toContain(TEST_BACKEND_TOKEN);
        expect(socket.sent_frames).toEqual([]);

        socket.emit_open();
        expect(socket.sent_frames).toHaveLength(1);
        expect(JSON.parse(socket.sent_frames[0]!) as unknown).toEqual({
            schema_version: BACKEND_SCHEMA_VERSION,
            type: 'AUTHENTICATE',
            token: TEST_BACKEND_TOKEN,
            after_sequence: 9,
        });
        expect(JSON.stringify(adapter)).not.toContain(TEST_BACKEND_TOKEN);

        adapter.stop();
        expect(socket.close_calls).toEqual([{ code: 1000, reason: 'client stop' }]);
    });

    it('duplicate/older sequence를 적용하지 않고 unknown type sequence 뒤 exact-next를 적용한다', () => {
        const sockets: Array<FakeBackendWebSocket> = [];
        const callbacks = create_callbacks();
        const snapshot = create_backend_snapshot_fixture();
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: vi.fn() as unknown as typeof fetch,
            create_uuid: create_uuid_factory(),
            create_web_socket: (url) => {
                const socket = new FakeBackendWebSocket(url);
                sockets.push(socket);
                return socket;
            },
        });

        adapter.start_live_events(snapshot, callbacks);
        const socket = sockets[0]!;
        const account_event = create_backend_event_fixture(10, 'ACCOUNT_UPDATED', {
            account: create_backend_account_fixture(),
        });
        socket.emit_message(account_event);
        socket.emit_message(account_event);
        socket.emit_message({ ...account_event, sequence: 8 });
        socket.emit_message({ ...account_event, sequence: 11 });
        socket.emit_message(create_backend_event_fixture(
            12,
            'FUTURE_EVENT',
            {},
            SECOND_EVENT_ID,
        ));
        socket.emit_message(create_backend_event_fixture(
            13,
            'ACCOUNT_UPDATED',
            { account: create_backend_account_fixture() },
            THIRD_EVENT_ID,
        ));

        expect(callbacks.on_event).toHaveBeenCalledTimes(3);
        expect(callbacks.on_event).toHaveBeenNthCalledWith(
            2,
            [],
            expect.objectContaining({ type: 'FUTURE_EVENT', sequence: 12 }),
        );
        expect(callbacks.on_reconnecting).not.toHaveBeenCalled();
        adapter.stop();
    });

    it('TRADING_SESSION_UPDATED의 version과 ratio를 다음 command DTO 기준으로 사용한다', async () => {
        const sockets: Array<FakeBackendWebSocket> = [];
        const callbacks = create_callbacks();
        const snapshot = create_backend_snapshot_fixture();
        const fetch_mock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
            return create_success_response(request_headers(init)['X-Request-Id']!, {
                version: 9,
                scale_in: '0.25',
                scale_out: '0.6',
            });
        });
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: fetch_mock as typeof fetch,
            create_uuid: create_uuid_factory(),
            create_web_socket: (url) => {
                const socket = new FakeBackendWebSocket(url);
                sockets.push(socket);
                return socket;
            },
        });

        adapter.start_live_events(snapshot, callbacks);
        sockets[0]!.emit_message({
            ...create_backend_event_fixture(
                10,
                'TRADING_SESSION_UPDATED',
                {
                    trading: {
                        ...snapshot.trading,
                        mode: 'fake',
                        status: 'stopping',
                        version: 8,
                        scale_in: '0.4',
                        scale_out: '0.6',
                        has_open_position: true,
                        session_id: '62c511b2-ea5c-43ac-bc36-e96eb39c85aa',
                    },
                },
            ),
            aggregate_version: 8,
        });
        await adapter.update_split_order('scale_in', 25);

        expect(callbacks.on_event).toHaveBeenCalledWith([
            expect.objectContaining({
                type: 'TRADING_SESSION_SYNCHRONIZED',
                status: 'stopping',
                version: 8,
            }),
        ], expect.objectContaining({ type: 'TRADING_SESSION_UPDATED' }));
        expect(JSON.parse(fetch_mock.mock.calls[0]?.[1]?.body as string) as unknown).toEqual({
            schema_version: BACKEND_SCHEMA_VERSION,
            scale_in: '0.25',
            scale_out: '0.6',
            expected_version: 8,
        });
        adapter.stop();
    });

    it('요청 중 더 높은 WS version이 오면 늦은 HTTP command 응답을 적용하지 않는다', async () => {
        const sockets: Array<FakeBackendWebSocket> = [];
        const callbacks = create_callbacks();
        const snapshot = create_backend_snapshot_fixture();
        let command_attempt = 0;
        const fetch_mock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
            const request_id = request_headers(init)['X-Request-Id']!;
            command_attempt += 1;
            if (command_attempt === 1) {
                sockets[0]!.emit_message({
                    ...create_backend_event_fixture(
                        10,
                        'TRADING_SESSION_UPDATED',
                        {
                            trading: {
                                ...snapshot.trading,
                                mode: 'fake',
                                status: 'running',
                                version: 8,
                                scale_in: '0.4',
                                scale_out: '0.6',
                                has_open_position: false,
                                session_id: '62c511b2-ea5c-43ac-bc36-e96eb39c85aa',
                            },
                        },
                    ),
                    aggregate_version: 8,
                });
                return create_success_response(request_id, {
                    version: 7,
                    scale_in: '0.25',
                    scale_out: '0.5',
                });
            }
            return create_success_response(request_id, {
                version: 9,
                scale_in: '0.4',
                scale_out: '0.3',
            });
        });
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: fetch_mock as typeof fetch,
            create_uuid: create_uuid_factory(),
            create_web_socket: (url) => {
                const socket = new FakeBackendWebSocket(url);
                sockets.push(socket);
                return socket;
            },
        });

        adapter.start_live_events(snapshot, callbacks);
        await expect(adapter.update_split_order('scale_in', 25)).rejects.toMatchObject({
            code: 'STALE_BACKEND_RESPONSE',
        });
        await expect(adapter.update_split_order('scale_out', 30)).resolves.toBeUndefined();

        expect(JSON.parse(fetch_mock.mock.calls[1]?.[1]?.body as string) as unknown).toEqual({
            schema_version: BACKEND_SCHEMA_VERSION,
            scale_in: '0.4',
            scale_out: '0.3',
            expected_version: 8,
        });
        adapter.stop();
    });

    it('REGIME_SELECTED의 selection/version을 다음 start command 기준으로 사용한다', async () => {
        const sockets: Array<FakeBackendWebSocket> = [];
        const callbacks = create_callbacks();
        const snapshot = create_backend_snapshot_fixture();
        const fetch_mock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
            return create_success_response(request_headers(init)['X-Request-Id']!, {
                status: 'running',
                session_id: '62c511b2-ea5c-43ac-bc36-e96eb39c85aa',
                version: 2,
            });
        });
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: fetch_mock as typeof fetch,
            create_uuid: create_uuid_factory(),
            create_web_socket: (url) => {
                const socket = new FakeBackendWebSocket(url);
                sockets.push(socket);
                return socket;
            },
        });

        adapter.start_live_events(snapshot, callbacks);
        sockets[0]!.emit_message({
            ...create_backend_event_fixture(10, 'REGIME_SELECTED', {
                selected: 'type0',
                support_status: 'supported',
                version: 1,
            }),
            aggregate_version: 1,
        });
        await adapter.start_trading('type0');

        expect(callbacks.on_event).toHaveBeenCalledWith([{
            type: 'REGIME_SELECTION_SYNCHRONIZED',
            selected: 'type0',
            support_status: 'supported',
            version: 1,
        }], expect.objectContaining({ type: 'REGIME_SELECTED' }));
        expect(JSON.parse(fetch_mock.mock.calls[0]?.[1]?.body as string) as unknown).toEqual({
            schema_version: BACKEND_SCHEMA_VERSION,
            expected_version: 1,
        });
        adapter.stop();
    });

    it('sequence gap에서 새 full snapshot을 먼저 적용한 뒤 그 last_sequence로 재연결한다', async () => {
        const sockets: Array<FakeBackendWebSocket> = [];
        const callbacks = create_callbacks();
        const resync_snapshot = {
            ...create_backend_snapshot_fixture(),
            last_sequence: 20,
        };
        const fetch_mock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
            return create_success_response(
                request_headers(init)['X-Request-Id']!,
                resync_snapshot,
            );
        }) as typeof fetch;
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: fetch_mock,
            create_uuid: create_uuid_factory(),
            create_web_socket: (url) => {
                const socket = new FakeBackendWebSocket(url);
                sockets.push(socket);
                return socket;
            },
        });

        adapter.start_live_events(create_backend_snapshot_fixture(), callbacks);
        sockets[0]!.emit_message(create_backend_event_fixture(
            11,
            'ACCOUNT_UPDATED',
            { account: create_backend_account_fixture() },
        ));

        await waitFor(() => {
            expect(callbacks.on_full_resync).toHaveBeenCalledWith(resync_snapshot);
            expect(sockets).toHaveLength(2);
        });
        sockets[1]!.emit_open();
        const authentication = JSON.parse(sockets[1]!.sent_frames[0]!) as {
            readonly after_sequence: number;
        };
        expect(authentication.after_sequence).toBe(20);
        expect(callbacks.on_reconnecting).toHaveBeenCalledWith('SEQUENCE_GAP');
        adapter.stop();
    });

    it('RESYNC_REQUIRED와 session change에서 cache를 잇지 않고 descriptor session만 다시 받는다', async () => {
        const sockets: Array<FakeBackendWebSocket> = [];
        const callbacks = create_callbacks();
        const snapshot = create_backend_snapshot_fixture();
        const fetch_mock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
            return create_success_response(request_headers(init)['X-Request-Id']!, snapshot);
        }) as typeof fetch;
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: fetch_mock,
            create_uuid: create_uuid_factory(),
            create_web_socket: (url) => {
                const socket = new FakeBackendWebSocket(url);
                sockets.push(socket);
                return socket;
            },
        });

        adapter.start_live_events(snapshot, callbacks);
        sockets[0]!.emit_message({
            schema_version: BACKEND_SCHEMA_VERSION,
            session_id: TEST_BACKEND_SESSION_ID,
            type: 'RESYNC_REQUIRED',
            reason: 'REPLAY_GAP',
            last_sequence: 12,
        });

        await waitFor(() => expect(sockets).toHaveLength(2));
        sockets[1]!.emit_message(create_backend_event_fixture(
            10,
            'ACCOUNT_UPDATED',
            { account: create_backend_account_fixture() },
            SECOND_EVENT_ID,
            '3fcb584c-0d34-4839-908d-4f3ef8f83442',
        ));

        await waitFor(() => expect(sockets).toHaveLength(3));
        expect(callbacks.on_full_resync).toHaveBeenCalledTimes(2);
        expect(callbacks.on_reconnecting).toHaveBeenNthCalledWith(1, 'REPLAY_GAP');
        expect(callbacks.on_reconnecting).toHaveBeenNthCalledWith(2, 'SESSION_CHANGED');
        adapter.stop();
    });

    it('full resync snapshot이 launch descriptor session과 다르면 적용 없이 fail closed한다', async () => {
        const sockets: Array<FakeBackendWebSocket> = [];
        const callbacks = create_callbacks();
        const changed_session_snapshot = {
            ...create_backend_snapshot_fixture(),
            session_id: '3fcb584c-0d34-4839-908d-4f3ef8f83442',
        };
        const fetch_mock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
            return create_success_response(
                request_headers(init)['X-Request-Id']!,
                changed_session_snapshot,
            );
        }) as typeof fetch;
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: fetch_mock,
            create_uuid: create_uuid_factory(),
            create_web_socket: (url) => {
                const socket = new FakeBackendWebSocket(url);
                sockets.push(socket);
                return socket;
            },
        });

        adapter.start_live_events(create_backend_snapshot_fixture(), callbacks);
        sockets[0]!.emit_message(create_backend_event_fixture(
            11,
            'ACCOUNT_UPDATED',
            { account: create_backend_account_fixture() },
        ));

        await waitFor(() => {
            expect(callbacks.on_failure).toHaveBeenCalledWith(expect.objectContaining({
                code: 'SESSION_MISMATCH',
            }));
        });
        expect(callbacks.on_full_resync).not.toHaveBeenCalled();
        expect(sockets).toHaveLength(1);
        await expect(adapter.load_snapshot()).rejects.toMatchObject({ code: 'ADAPTER_STOPPED' });
    });

    it('unknown schema frame은 fail closed하고 token 참조와 socket을 제거한다', async () => {
        const sockets: Array<FakeBackendWebSocket> = [];
        const callbacks = create_callbacks();
        const adapter = new BackendUiAdapter(create_descriptor(), {
            fetch: vi.fn() as unknown as typeof fetch,
            create_uuid: create_uuid_factory(),
            create_web_socket: (url) => {
                const socket = new FakeBackendWebSocket(url);
                sockets.push(socket);
                return socket;
            },
        });

        adapter.start_live_events(create_backend_snapshot_fixture(), callbacks);
        sockets[0]!.emit_message({
            ...create_backend_event_fixture(10, 'FUTURE_EVENT', {}),
            schema_version: 1,
        });

        expect(callbacks.on_failure).toHaveBeenCalledWith(expect.objectContaining({
            code: 'UNSUPPORTED_SCHEMA_VERSION',
        }));
        expect(sockets[0]!.close_calls).toEqual([{ code: 1000, reason: 'fail closed' }]);
        await expect(adapter.load_snapshot()).rejects.toMatchObject({ code: 'ADAPTER_STOPPED' });
        expect(JSON.stringify(adapter)).not.toContain(TEST_BACKEND_TOKEN);
    });
});
