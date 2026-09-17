import { describe, expect, it } from 'vitest';
import {
    build_combined_kline_stream_url,
    load_all_klines,
    load_older_klines,
    parse_combined_kline_message,
} from './binanceKlines';

describe('binanceKlines', () => {
    it('four REST intervals are requested and mapped to normalized klines', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const requested_urls: Array<string> = [];
        const requested_signals: Array<AbortSignal | null | undefined> = [];
        const abort_controller = new AbortController();

        /**
         * 함수 이름: fetch_implementation()
         * 기능: 공개 캔들 요청의 URL·취소 신호를 확인하고 고정 캔들 응답을 제공한다.
         * 인자: input -> 요청 URL, options -> 요청 옵션
         * 반환값: 고정 캔들 Response의 Promise
         * 작성 날짜: 2026/09/17
         */
        const fetch_implementation: typeof fetch = async (input, options) => {
            const request_url = new URL(String(input));
            const interval = request_url.searchParams.get('interval');

            requested_urls.push(request_url.toString());
            requested_signals.push(options?.signal);

            return new Response(JSON.stringify([[
                1_000,
                '100.5',
                '112.25',
                '98.75',
                '108.5',
                '42.125',
                1_999,
                '4400.0',
                15,
                '20.0',
                '2100.0',
                '0',
            ]]), {
                headers: { 'Content-Type': 'application/json' },
                status: 200,
                statusText: interval ?? '',
            });
        };

        // 준비한 입력으로 결과를 계산하거나 현재 상태를 읽는다.
        const result = await load_all_klines('ethusdt', {
            limit: 42,
            signal: abort_controller.signal,
            fetch_implementation,
        });

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(Object.keys(result)).toEqual(['1m', '30m', '4h', '1d']);
        expect(result['1m']).toEqual([{
            symbol: 'ETHUSDT',
            interval: '1m',
            open_time: 1_000,
            close_time: 1_999,
            open: 100.5,
            high: 112.25,
            low: 98.75,
            close: 108.5,
            volume: 42.125,
            is_closed: true,
        }]);
        expect(requested_urls).toHaveLength(4);
        expect(requested_urls).toContain(
            'https://data-api.binance.vision/api/v3/klines?symbol=ETHUSDT&interval=30m&limit=42',
        );
        expect(requested_signals.every((signal) => signal instanceof AbortSignal && !signal.aborted)).toBe(true);
    });

    it('builds one Binance combined stream URL for all chart intervals', () => {
        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(build_combined_kline_stream_url('ETHUSDT')).toBe(
            'wss://data-stream.binance.vision/stream?streams='
            + 'ethusdt@kline_1m/ethusdt@kline_30m/ethusdt@kline_4h/ethusdt@kline_1d',
        );
    });

    it('requests one normalized page strictly before the supplied open time', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        let requested_url = '';
        const abort_controller = new AbortController();

        /**
         * 함수 이름: fetch_implementation()
         * 기능: 공개 캔들 요청의 URL·취소 신호를 확인하고 고정 캔들 응답을 제공한다.
         * 인자: input -> 요청 URL, options -> 요청 옵션
         * 반환값: 고정 캔들 Response의 Promise
         * 작성 날짜: 2026/09/17
         */
        const fetch_implementation: typeof fetch = async (input, options) => {
            requested_url = String(input);
            expect(options?.signal).toBeInstanceOf(AbortSignal);

            return new Response(JSON.stringify([[
                1_000,
                '100.5',
                '112.25',
                '98.75',
                '108.5',
                '42.125',
                1_999,
                '4400.0',
                15,
                '20.0',
                '2100.0',
                '0',
            ]]), {
                headers: { 'Content-Type': 'application/json' },
                status: 200,
            });
        };

        // 준비한 입력으로 결과를 계산하거나 현재 상태를 읽는다.
        const result = await load_older_klines('ethusdt', '30m', 3_000, {
            signal: abort_controller.signal,
            fetch_implementation,
        });

        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const parsed_url = new URL(requested_url);

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(parsed_url.origin + parsed_url.pathname).toBe(
            'https://data-api.binance.vision/api/v3/klines',
        );
        expect(Object.fromEntries(parsed_url.searchParams)).toEqual({
            symbol: 'ETHUSDT',
            interval: '30m',
            limit: '1000',
            endTime: '2999',
        });
        expect(result).toEqual([{
            symbol: 'ETHUSDT',
            interval: '30m',
            open_time: 1_000,
            close_time: 1_999,
            open: 100.5,
            high: 112.25,
            low: 98.75,
            close: 108.5,
            volume: 42.125,
            is_closed: true,
        }]);
    });

    it('validates older kline paging inputs before requesting Binance', async () => {
        /**
         * 함수 이름: fetch_implementation()
         * 기능: 유효하지 않은 입력에서 fetch가 호출되면 즉시 테스트를 실패시킨다.
         * 인자: 없음
         * 반환값: 반환하지 않고 예외를 발생시키는 Promise
         * 작성 날짜: 2026/09/17
         */
        const fetch_implementation: typeof fetch = async () => {
            throw new Error('fetch must not be called');
        };

        // 잘못된 입력이나 실행 실패가 정해진 오류로 전달되는지 검증한다.
        await expect(load_older_klines('ETHUSDT', '1m', 1_000, {
            limit: 1001,
            fetch_implementation,
        })).rejects.toThrow('limit must be an integer from 1 to 1000');
        await expect(load_older_klines('ETHUSDT', '1m', 0, {
            fetch_implementation,
        })).rejects.toThrow('before open time must be a positive safe integer');
        await expect(load_older_klines(
            'ETHUSDT',
            '5m' as '1m',
            1_000,
            { fetch_implementation },
        )).rejects.toThrow('interval is not supported');
    });

    it('parses a combined WebSocket kline payload', () => {
        // 준비한 입력으로 결과를 계산하거나 현재 상태를 읽는다.
        const result = parse_combined_kline_message(JSON.stringify({
            stream: 'ethusdt@kline_30m',
            data: {
                e: 'kline',
                E: 2_000,
                s: 'ETHUSDT',
                k: {
                    t: 1_000,
                    T: 1_999,
                    s: 'ETHUSDT',
                    i: '30m',
                    o: '100.5',
                    h: '112.25',
                    l: '98.75',
                    c: '108.5',
                    v: '42.125',
                    x: false,
                },
            },
        }));

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(result).toEqual({
            event_time: 2_000,
            symbol: 'ETHUSDT',
            interval: '30m',
            open_time: 1_000,
            close_time: 1_999,
            open: 100.5,
            high: 112.25,
            low: 98.75,
            close: 108.5,
            volume: 42.125,
            is_closed: false,
        });
    });

    it('rejects malformed REST and combined WebSocket payloads', async () => {
        /**
         * 함수 이름: malformed_fetch()
         * 기능: 캔들 schema 검증 실패를 재현할 잘못된 행을 응답한다.
         * 인자: 없음
         * 반환값: 잘못된 캔들 Response의 Promise
         * 작성 날짜: 2026/09/17
         */
        const malformed_fetch: typeof fetch = async () => {
            return new Response(JSON.stringify([['not-a-kline']]), {
                headers: { 'Content-Type': 'application/json' },
                status: 200,
            });
        };

        // 잘못된 입력이나 실행 실패가 정해진 오류로 전달되는지 검증한다.
        await expect(load_all_klines('ETHUSDT', {
            fetch_implementation: malformed_fetch,
        })).rejects.toThrow('must be a 12-field tuple');

        expect(() => parse_combined_kline_message({
            stream: 'ethusdt@kline_1m',
            data: {
                e: 'kline',
                s: 'ETHUSDT',
                k: {
                    t: 1_000,
                    T: 1_999,
                    s: 'ETHUSDT',
                    i: '5m',
                    o: '100',
                    h: '101',
                    l: '99',
                    c: '100',
                    v: '1',
                    x: false,
                },
            },
        })).toThrow('interval is not supported');
    });
});
