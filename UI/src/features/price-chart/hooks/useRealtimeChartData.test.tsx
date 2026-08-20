import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ChartInterval } from '../types';
import {
    use_realtime_chart_data,
    type WebSocketFactory,
} from './useRealtimeChartData';

const supported_intervals = ['1m', '30m', '4h', '1d'] as const;

interface PendingFetchRequest {
    readonly interval: ChartInterval;
    readonly request_url: URL;
    readonly resolve_response: (response: Response) => void;
    readonly signal: AbortSignal | null | undefined;
}

interface WebSocketCloseCall {
    readonly code: number | undefined;
    readonly reason: string | undefined;
}

/**
 * 클래스 이름: FakeWebSocket
 * 기능: hook 테스트에서 WebSocket lifecycle event와 close 호출을 수동 제어한다.
 * 작성 날짜: 2026/08/20
 */
class FakeWebSocket {
    readonly close_calls: Array<WebSocketCloseCall> = [];
    onclose: WebSocket['onclose'] = null;
    onerror: WebSocket['onerror'] = null;
    onmessage: WebSocket['onmessage'] = null;
    onopen: WebSocket['onopen'] = null;

    constructor(readonly url: string) {}

    /**
     * 함수 이름: close()
     * 기능: hook이 요청한 WebSocket 종료 인자를 기록한다.
     * 인자: code -> WebSocket 종료 코드
     *      reason -> WebSocket 종료 사유
     * 반환값: 없음
     * 작성 날짜: 2026/08/20
     */
    close(code?: number, reason?: string): void {
        this.close_calls.push({ code, reason });
    }

    /**
     * 함수 이름: emit_open()
     * 기능: 등록된 WebSocket open handler를 실행한다.
     * 인자: 없음
     * 반환값: 없음
     * 작성 날짜: 2026/08/20
     */
    emit_open(): void {
        this.onopen?.call(this as unknown as WebSocket, new Event('open'));
    }

    /**
     * 함수 이름: emit_message()
     * 기능: 등록된 WebSocket message handler에 Binance payload를 전달한다.
     * 인자: payload -> 전달할 combined stream JSON 문자열
     * 반환값: 없음
     * 작성 날짜: 2026/08/20
     */
    emit_message(payload: string): void {
        this.onmessage?.call(
            this as unknown as WebSocket,
            new MessageEvent('message', { data: payload }),
        );
    }

    /**
     * 함수 이름: emit_close()
     * 기능: 등록된 WebSocket close handler를 실행한다.
     * 인자: 없음
     * 반환값: 없음
     * 작성 날짜: 2026/08/20
     */
    emit_close(): void {
        this.onclose?.call(this as unknown as WebSocket, new CloseEvent('close'));
    }
}

/**
 * 함수 이름: create_pending_fetch()
 * 기능: 각 REST 요청을 기록하고 테스트가 명시적으로 응답할 때까지 보류한다.
 * 인자: pending_requests -> 보류 중 요청을 저장할 목록
 *      event_order -> 선택적으로 lifecycle 호출 순서를 저장할 목록
 * 반환값: 제어 가능한 fetch 구현
 * 작성 날짜: 2026/08/20
 */
function create_pending_fetch(
    pending_requests: Array<PendingFetchRequest>,
    event_order?: Array<string>,
): typeof fetch {
    return ((input: RequestInfo | URL, options?: RequestInit) => {
        const request_url = new URL(String(input));
        const interval_value = request_url.searchParams.get('interval');
        const interval_is_supported = supported_intervals.some((interval) => {
            return interval === interval_value;
        });

        if (!interval_is_supported) {
            throw new Error(`Unexpected chart interval: ${interval_value ?? 'null'}`);
        }

        const interval = interval_value as ChartInterval;

        event_order?.push(`fetch:${interval}`);

        return new Promise<Response>((resolve_response) => {
            pending_requests.push({
                interval,
                request_url,
                resolve_response,
                signal: options?.signal,
            });
        });
    }) as typeof fetch;
}

/**
 * 함수 이름: create_fake_web_socket_factory()
 * 기능: 생성된 fake socket과 선택적 lifecycle 호출 순서를 기록한다.
 * 인자: sockets -> 생성된 fake socket을 저장할 목록
 *      event_order -> 선택적으로 lifecycle 호출 순서를 저장할 목록
 * 반환값: hook에 주입할 WebSocket factory
 * 작성 날짜: 2026/08/20
 */
function create_fake_web_socket_factory(
    sockets: Array<FakeWebSocket>,
    event_order?: Array<string>,
): WebSocketFactory {
    return (url) => {
        event_order?.push('web_socket_factory');

        const socket = new FakeWebSocket(url);

        sockets.push(socket);
        return socket as unknown as WebSocket;
    };
}

/**
 * 함수 이름: create_rest_response()
 * 기능: 단일 주기의 정상 Binance REST kline response를 만든다.
 * 인자: interval -> 응답 대상 chart 주기
 *      close_value -> kline 종가
 *      open_time -> kline 시작 Unix millisecond
 * 반환값: 정상 REST Response
 * 작성 날짜: 2026/08/20
 */
function create_rest_response(
    interval: ChartInterval,
    close_value: number,
    open_time = 1_000,
): Response {
    return create_rest_page_response(interval, [{ close_value, open_time }]);
}

/**
 * 함수 이름: create_rest_page_response()
 * 기능: 과거 페이지 테스트에 사용할 여러 Binance REST kline tuple 응답을 만든다.
 * 인자: interval -> 응답 대상 chart 주기
 *      klines -> 각 봉의 종가와 시작 시각
 *      status -> 응답 HTTP status
 * 반환값: Binance kline 배열을 담은 Response
 * 작성 날짜: 2026/08/20
 */
function create_rest_page_response(
    interval: ChartInterval,
    klines: ReadonlyArray<{
        readonly close_value: number;
        readonly open_time: number;
    }>,
    status = 200,
): Response {
    return new Response(JSON.stringify(klines.map(({ close_value, open_time }) => [
        open_time,
        '100',
        '120',
        '90',
        String(close_value),
        '25',
        open_time + 999,
        '2500',
        10,
        '12',
        '1200',
        '0',
    ])), {
        headers: { 'Content-Type': 'application/json' },
        status,
        statusText: interval,
    });
}

/**
 * 함수 이름: resolve_rest_requests()
 * 기능: 보류 중인 네 주기 REST 요청을 지정한 종가로 모두 완료한다.
 * 인자: pending_requests -> 완료할 REST 요청 목록
 *      close_values -> 주기별 REST 종가
 * 반환값: 없음
 * 작성 날짜: 2026/08/20
 */
function resolve_rest_requests(
    pending_requests: ReadonlyArray<PendingFetchRequest>,
    close_values: Readonly<Record<ChartInterval, number>>,
): void {
    pending_requests.forEach((pending_request) => {
        pending_request.resolve_response(create_rest_response(
            pending_request.interval,
            close_values[pending_request.interval],
        ));
    });
}

/**
 * 함수 이름: create_web_socket_kline_message()
 * 기능: hook에 전달할 정상 Binance combined kline payload를 만든다.
 * 인자: interval -> kline chart 주기
 *      close_value -> kline 종가
 *      open_time -> kline 시작 Unix millisecond
 * 반환값: combined stream JSON 문자열
 * 작성 날짜: 2026/08/20
 */
function create_web_socket_kline_message(
    interval: ChartInterval,
    close_value: number,
    open_time = 1_000,
): string {
    return JSON.stringify({
        stream: `ethusdt@kline_${interval}`,
        data: {
            e: 'kline',
            E: open_time + 500,
            s: 'ETHUSDT',
            k: {
                t: open_time,
                T: open_time + 999,
                s: 'ETHUSDT',
                i: interval,
                o: '100',
                h: '120',
                l: '90',
                c: String(close_value),
                v: '25',
                x: false,
            },
        },
    });
}

afterEach(() => {
    vi.useRealTimers();
});

describe('use_realtime_chart_data', () => {
    it('creates the WebSocket before starting any REST request', async () => {
        const event_order: Array<string> = [];
        const pending_requests: Array<PendingFetchRequest> = [];
        const sockets: Array<FakeWebSocket> = [];
        const fetch_implementation = create_pending_fetch(pending_requests, event_order);
        const web_socket_factory = create_fake_web_socket_factory(sockets, event_order);
        const { unmount } = renderHook(() => use_realtime_chart_data({
            fetch_implementation,
            web_socket_factory,
        }));

        await waitFor(() => {
            expect(pending_requests).toHaveLength(4);
        });

        expect(event_order).toEqual([
            'web_socket_factory',
            'fetch:1m',
            'fetch:30m',
            'fetch:4h',
            'fetch:1d',
        ]);
        expect(sockets[0]?.url).toContain(
            'streams=ethusdt@kline_1m/ethusdt@kline_30m/ethusdt@kline_4h/ethusdt@kline_1d',
        );

        unmount();
    });

    it('lets a buffered WebSocket candle override the same pending REST candle', async () => {
        const pending_requests: Array<PendingFetchRequest> = [];
        const sockets: Array<FakeWebSocket> = [];
        const fetch_implementation = create_pending_fetch(pending_requests);
        const web_socket_factory = create_fake_web_socket_factory(sockets);
        const { result } = renderHook(() => use_realtime_chart_data({
            fetch_implementation,
            web_socket_factory,
        }));

        await waitFor(() => {
            expect(pending_requests).toHaveLength(4);
        });

        act(() => {
            sockets[0]?.emit_open();
            sockets[0]?.emit_message(create_web_socket_kline_message('1m', 222));
        });

        await act(async () => {
            resolve_rest_requests(pending_requests, {
                '1m': 111,
                '30m': 130,
                '4h': 140,
                '1d': 150,
            });
            await Promise.resolve();
        });

        await waitFor(() => {
            expect(result.current.data_status).toBe('live');
        });

        expect(result.current.klines_by_interval['1m']).toEqual([
            expect.objectContaining({
                close: 222,
                interval: '1m',
                is_closed: false,
                open_time: 1_000,
            }),
        ]);
        expect(Object.keys(result.current.klines_by_interval)).toEqual([
            '1m',
            '30m',
            '4h',
            '1d',
        ]);
    });

    it('replaces the live candle while retaining every interval map', async () => {
        const pending_requests: Array<PendingFetchRequest> = [];
        const sockets: Array<FakeWebSocket> = [];
        const fetch_implementation = create_pending_fetch(pending_requests);
        const web_socket_factory = create_fake_web_socket_factory(sockets);
        const { result } = renderHook(() => use_realtime_chart_data({
            fetch_implementation,
            web_socket_factory,
        }));

        await waitFor(() => {
            expect(pending_requests).toHaveLength(4);
        });

        act(() => {
            sockets[0]?.emit_open();
        });
        await act(async () => {
            resolve_rest_requests(pending_requests, {
                '1m': 101,
                '30m': 130,
                '4h': 140,
                '1d': 150,
            });
            await Promise.resolve();
        });
        await waitFor(() => {
            expect(result.current.data_status).toBe('live');
        });

        const retained_30m_klines = result.current.klines_by_interval['30m'];
        const retained_4h_klines = result.current.klines_by_interval['4h'];
        const retained_1d_klines = result.current.klines_by_interval['1d'];

        act(() => {
            sockets[0]?.emit_message(create_web_socket_kline_message('1m', 303));
        });

        await waitFor(() => {
            expect(result.current.klines_by_interval['1m'][0]?.close).toBe(303);
        });

        expect(result.current.klines_by_interval['1m']).toHaveLength(1);
        expect(result.current.klines_by_interval['30m']).toBe(retained_30m_klines);
        expect(result.current.klines_by_interval['4h']).toBe(retained_4h_klines);
        expect(result.current.klines_by_interval['1d']).toBe(retained_1d_klines);
        expect(result.current.updated_at).not.toBeNull();
    });

    it('loads one older page per interval without truncating history on later WebSocket updates', async () => {
        const pending_requests: Array<PendingFetchRequest> = [];
        const sockets: Array<FakeWebSocket> = [];
        const fetch_implementation = create_pending_fetch(pending_requests);
        const web_socket_factory = create_fake_web_socket_factory(sockets);
        const { result } = renderHook(() => use_realtime_chart_data({
            fetch_implementation,
            history_page_limit: 2,
            limit: 1,
            web_socket_factory,
        }));

        await waitFor(() => {
            expect(pending_requests).toHaveLength(4);
        });
        act(() => {
            sockets[0]?.emit_open();
        });
        await act(async () => {
            resolve_rest_requests(pending_requests.slice(0, 4), {
                '1m': 101,
                '30m': 130,
                '4h': 140,
                '1d': 150,
            });
            await Promise.resolve();
        });
        await waitFor(() => {
            expect(result.current.data_status).toBe('live');
        });

        const stable_load_callback = result.current.load_earlier_klines;

        act(() => {
            result.current.load_earlier_klines('1m');
            result.current.load_earlier_klines('1m');
        });
        await waitFor(() => {
            expect(pending_requests).toHaveLength(5);
        });

        const first_history_request = pending_requests[4];

        expect(first_history_request?.interval).toBe('1m');
        expect(Object.fromEntries(first_history_request?.request_url.searchParams ?? [])).toEqual({
            symbol: 'ETHUSDT',
            interval: '1m',
            limit: '2',
            endTime: '999',
        });
        expect(result.current.history_load_state_by_interval['1m'].is_loading).toBe(true);
        expect(result.current.history_load_state_by_interval['30m'].is_loading).toBe(false);

        act(() => {
            sockets[0]?.emit_message(create_web_socket_kline_message('1m', 202, 2_000));
        });
        await act(async () => {
            first_history_request?.resolve_response(create_rest_page_response('1m', [
                { close_value: 11, open_time: 100 },
                { close_value: 55, open_time: 500 },
            ]));
            await Promise.resolve();
            await Promise.resolve();
        });
        await waitFor(() => {
            expect(result.current.klines_by_interval['1m']).toHaveLength(4);
        });

        expect(result.current.klines_by_interval['1m'].map((kline) => kline.open_time)).toEqual([
            100,
            500,
            1_000,
            2_000,
        ]);
        expect(result.current.history_load_state_by_interval['1m']).toEqual({
            error_message: null,
            is_exhausted: false,
            is_loading: false,
        });
        expect(result.current.load_earlier_klines).toBe(stable_load_callback);

        act(() => {
            sockets[0]?.emit_message(create_web_socket_kline_message('1m', 303, 2_000));
        });
        await waitFor(() => {
            expect(result.current.klines_by_interval['1m'].at(-1)?.close).toBe(303);
        });
        expect(result.current.klines_by_interval['1m']).toHaveLength(4);

        act(() => {
            result.current.load_earlier_klines('1m');
        });
        await waitFor(() => {
            expect(pending_requests).toHaveLength(6);
        });
        expect(pending_requests[5]?.request_url.searchParams.get('endTime')).toBe('99');

        await act(async () => {
            pending_requests[5]?.resolve_response(create_rest_page_response('1m', []));
            await Promise.resolve();
            await Promise.resolve();
        });
        await waitFor(() => {
            expect(result.current.history_load_state_by_interval['1m'].is_exhausted).toBe(true);
        });

        act(() => {
            result.current.load_earlier_klines('1m');
        });
        expect(pending_requests).toHaveLength(6);
    });

    it('tracks concurrent interval history requests independently and permits retry after an error', async () => {
        const pending_requests: Array<PendingFetchRequest> = [];
        const sockets: Array<FakeWebSocket> = [];
        const fetch_implementation = create_pending_fetch(pending_requests);
        const web_socket_factory = create_fake_web_socket_factory(sockets);
        const { result } = renderHook(() => use_realtime_chart_data({
            fetch_implementation,
            history_page_limit: 2,
            limit: 1,
            web_socket_factory,
        }));

        await waitFor(() => {
            expect(pending_requests).toHaveLength(4);
        });
        act(() => {
            sockets[0]?.emit_open();
        });
        await act(async () => {
            resolve_rest_requests(pending_requests.slice(0, 4), {
                '1m': 101,
                '30m': 130,
                '4h': 140,
                '1d': 150,
            });
            await Promise.resolve();
        });
        await waitFor(() => {
            expect(result.current.data_status).toBe('live');
        });

        act(() => {
            result.current.load_earlier_klines('1m');
            result.current.load_earlier_klines('30m');
        });
        await waitFor(() => {
            expect(pending_requests).toHaveLength(6);
        });
        expect(result.current.history_load_state_by_interval['1m'].is_loading).toBe(true);
        expect(result.current.history_load_state_by_interval['30m'].is_loading).toBe(true);

        await act(async () => {
            pending_requests[4]?.resolve_response(create_rest_page_response('1m', [], 503));
            pending_requests[5]?.resolve_response(create_rest_page_response('30m', [
                { close_value: 22, open_time: 500 },
            ]));
            await Promise.resolve();
            await Promise.resolve();
        });
        await waitFor(() => {
            expect(result.current.history_load_state_by_interval['1m'].error_message)
                .toContain('HTTP 503');
            expect(result.current.history_load_state_by_interval['30m'].is_exhausted).toBe(true);
        });

        expect(result.current.data_status).toBe('live');
        expect(result.current.history_load_state_by_interval['4h'])
            .toEqual({ error_message: null, is_exhausted: false, is_loading: false });

        act(() => {
            result.current.load_earlier_klines('1m');
        });
        await waitFor(() => {
            expect(pending_requests).toHaveLength(7);
        });
        expect(result.current.history_load_state_by_interval['1m'].error_message).toBeNull();
        expect(result.current.history_load_state_by_interval['1m'].is_loading).toBe(true);

        await act(async () => {
            pending_requests[6]?.resolve_response(create_rest_page_response('1m', []));
            await Promise.resolve();
            await Promise.resolve();
        });
        await waitFor(() => {
            expect(result.current.history_load_state_by_interval['1m']).toEqual({
                error_message: null,
                is_exhausted: true,
                is_loading: false,
            });
        });
    });

    it('retains previously prepended history when the WebSocket connection is initialized again', async () => {
        const pending_requests: Array<PendingFetchRequest> = [];
        const reconnect_delays = [0] as const;
        const sockets: Array<FakeWebSocket> = [];
        const fetch_implementation = create_pending_fetch(pending_requests);
        const web_socket_factory = create_fake_web_socket_factory(sockets);
        const { result } = renderHook(() => use_realtime_chart_data({
            fetch_implementation,
            history_page_limit: 2,
            limit: 1,
            reconnect_delays,
            web_socket_factory,
        }));

        await waitFor(() => {
            expect(pending_requests).toHaveLength(4);
        });
        act(() => {
            sockets[0]?.emit_open();
        });
        await act(async () => {
            resolve_rest_requests(pending_requests.slice(0, 4), {
                '1m': 101,
                '30m': 130,
                '4h': 140,
                '1d': 150,
            });
            await Promise.resolve();
        });
        await waitFor(() => {
            expect(result.current.data_status).toBe('live');
        });

        act(() => {
            result.current.load_earlier_klines('1m');
        });
        await waitFor(() => {
            expect(pending_requests).toHaveLength(5);
        });
        await act(async () => {
            pending_requests[4]?.resolve_response(create_rest_page_response('1m', [
                { close_value: 11, open_time: 100 },
                { close_value: 55, open_time: 500 },
            ]));
            await Promise.resolve();
            await Promise.resolve();
        });
        await waitFor(() => {
            expect(result.current.klines_by_interval['1m']).toHaveLength(3);
        });

        act(() => {
            sockets[0]?.emit_close();
        });
        await waitFor(() => {
            expect(sockets).toHaveLength(2);
            expect(pending_requests).toHaveLength(9);
        });
        act(() => {
            sockets[1]?.emit_open();
        });
        await act(async () => {
            resolve_rest_requests(pending_requests.slice(5, 9), {
                '1m': 202,
                '30m': 230,
                '4h': 240,
                '1d': 250,
            });
            await Promise.resolve();
        });
        await waitFor(() => {
            expect(result.current.data_status).toBe('live');
            expect(result.current.klines_by_interval['1m']).toHaveLength(3);
        });

        act(() => {
            sockets[1]?.emit_message(create_web_socket_kline_message('1m', 303));
        });
        await waitFor(() => {
            expect(result.current.klines_by_interval['1m'].at(-1)?.close).toBe(303);
        });

        expect(result.current.klines_by_interval['1m'].map((kline) => kline.open_time)).toEqual([
            100,
            500,
            1_000,
        ]);
    });

    it('aborts an in-flight older-page request and ignores its stale response on unmount', async () => {
        const pending_requests: Array<PendingFetchRequest> = [];
        const sockets: Array<FakeWebSocket> = [];
        const fetch_implementation = create_pending_fetch(pending_requests);
        const web_socket_factory = create_fake_web_socket_factory(sockets);
        const { result, unmount } = renderHook(() => use_realtime_chart_data({
            fetch_implementation,
            history_page_limit: 2,
            limit: 1,
            web_socket_factory,
        }));

        await waitFor(() => {
            expect(pending_requests).toHaveLength(4);
        });
        act(() => {
            sockets[0]?.emit_open();
        });
        await act(async () => {
            resolve_rest_requests(pending_requests.slice(0, 4), {
                '1m': 101,
                '30m': 130,
                '4h': 140,
                '1d': 150,
            });
            await Promise.resolve();
        });
        await waitFor(() => {
            expect(result.current.data_status).toBe('live');
        });

        act(() => {
            result.current.load_earlier_klines('1m');
        });
        await waitFor(() => {
            expect(pending_requests).toHaveLength(5);
        });

        const history_request = pending_requests[4];
        const snapshot_before_unmount = result.current;

        unmount();

        expect(history_request?.signal?.aborted).toBe(true);
        await act(async () => {
            history_request?.resolve_response(create_rest_page_response('1m', [
                { close_value: 11, open_time: 100 },
                { close_value: 55, open_time: 500 },
            ]));
            await Promise.resolve();
            await Promise.resolve();
        });
        expect(result.current).toBe(snapshot_before_unmount);
    });

    it('schedules a reconnect after close with an injected zero delay', async () => {
        const pending_requests: Array<PendingFetchRequest> = [];
        const reconnect_delays = [0] as const;
        const sockets: Array<FakeWebSocket> = [];
        const fetch_implementation = create_pending_fetch(pending_requests);
        const web_socket_factory = create_fake_web_socket_factory(sockets);
        const { unmount } = renderHook(() => use_realtime_chart_data({
            fetch_implementation,
            reconnect_delays,
            web_socket_factory,
        }));

        await waitFor(() => {
            expect(pending_requests).toHaveLength(4);
        });

        act(() => {
            sockets[0]?.emit_close();
        });

        await waitFor(() => {
            expect(sockets).toHaveLength(2);
        });

        expect(pending_requests).toHaveLength(8);
        expect(sockets[0]?.close_calls).toContainEqual({
            code: 1_000,
            reason: 'Binance 시장 데이터 연결을 갱신합니다.',
        });

        unmount();
    });

    it('aborts fetch, clears reconnect, closes the socket, and ignores stale work on unmount', async () => {
        vi.useFakeTimers();

        const pending_requests: Array<PendingFetchRequest> = [];
        const reconnect_delays = [25] as const;
        const sockets: Array<FakeWebSocket> = [];
        const fetch_implementation = create_pending_fetch(pending_requests);
        const web_socket_factory = create_fake_web_socket_factory(sockets);
        const { result, unmount } = renderHook(() => use_realtime_chart_data({
            fetch_implementation,
            reconnect_delays,
            web_socket_factory,
        }));

        expect(pending_requests).toHaveLength(4);

        const socket = sockets[0];

        expect(socket).toBeDefined();
        const stale_message_handler = socket?.onmessage ?? null;

        act(() => {
            socket?.emit_close();
        });
        expect(vi.getTimerCount()).toBe(1);
        const snapshot_before_unmount = result.current;

        unmount();

        expect(pending_requests.every((request) => request.signal?.aborted === true)).toBe(true);
        expect(socket?.close_calls).toEqual([{
            code: 1_000,
            reason: '가격 차트 구독을 종료합니다.',
        }]);
        expect(socket?.onclose).toBeNull();
        expect(socket?.onerror).toBeNull();
        expect(socket?.onmessage).toBeNull();
        expect(socket?.onopen).toBeNull();
        expect(vi.getTimerCount()).toBe(0);

        await act(async () => {
            resolve_rest_requests(pending_requests, {
                '1m': 101,
                '30m': 130,
                '4h': 140,
                '1d': 150,
            });
            stale_message_handler?.call(
                socket as unknown as WebSocket,
                new MessageEvent('message', {
                    data: create_web_socket_kline_message('1m', 999),
                }),
            );
            vi.advanceTimersByTime(25);
            await Promise.resolve();
            await Promise.resolve();
        });

        expect(sockets).toHaveLength(1);
        expect(pending_requests).toHaveLength(4);
        expect(result.current).toBe(snapshot_before_unmount);
    });
});
