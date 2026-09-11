import { describe, expect, it } from 'vitest';
import {
    build_combined_kline_stream_url,
    load_all_klines,
    load_older_klines,
    parse_combined_kline_message,
} from './binanceKlines';

describe('binanceKlines', () => {
    it('four REST intervals are requested and mapped to normalized klines', async () => {
        const requested_urls: Array<string> = [];
        const requested_signals: Array<AbortSignal | null | undefined> = [];
        const abort_controller = new AbortController();
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

        const result = await load_all_klines('ethusdt', {
            limit: 42,
            signal: abort_controller.signal,
            fetch_implementation,
        });

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
        expect(build_combined_kline_stream_url('ETHUSDT')).toBe(
            'wss://data-stream.binance.vision/stream?streams='
            + 'ethusdt@kline_1m/ethusdt@kline_30m/ethusdt@kline_4h/ethusdt@kline_1d',
        );
    });

    it('requests one normalized page strictly before the supplied open time', async () => {
        let requested_url = '';
        const abort_controller = new AbortController();
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

        const result = await load_older_klines('ethusdt', '30m', 3_000, {
            signal: abort_controller.signal,
            fetch_implementation,
        });
        const parsed_url = new URL(requested_url);

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
        const fetch_implementation: typeof fetch = async () => {
            throw new Error('fetch must not be called');
        };

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
        const malformed_fetch: typeof fetch = async () => {
            return new Response(JSON.stringify([['not-a-kline']]), {
                headers: { 'Content-Type': 'application/json' },
                status: 200,
            });
        };

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
