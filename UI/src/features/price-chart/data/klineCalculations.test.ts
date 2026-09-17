import { describe, expect, it } from 'vitest';
import {
    calculate_kline_indicators,
    merge_klines,
} from './klineCalculations';
import type { NormalizedKline } from './types';


/**
 * 함수 이름: create_test_kline()
 * 기능: 병합과 지표 단위 테스트에 사용할 결정적 kline을 만든다.
 * 인자: open_time -> kline 시작 시각
 *      close -> OHLC에 사용할 가격
 *      interval -> kline 주기
 *      is_closed -> kline 종료 여부
 * 반환값: 테스트용 정규화 kline
 * 작성 날짜: 2026/08/20
 */
function create_test_kline(
    open_time: number,
    close: number,
    interval: NormalizedKline['interval'] = '1m',
    is_closed = true,
): NormalizedKline {
    return {
        symbol: 'ETHUSDT',
        interval,
        open_time,
        close_time: open_time + 59,
        open: close,
        high: close + 1,
        low: close - 1,
        close,
        volume: close * 2,
        is_closed,
    };
}

describe('klineCalculations', () => {
    it('deduplicates by series and time with incoming WebSocket values winning', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const existing_klines = [
            create_test_kline(100, 10),
            create_test_kline(200, 20),
            create_test_kline(200, 40, '4h'),
        ];
        const incoming_klines = [
            create_test_kline(200, 21, '1m', false),
            create_test_kline(300, 30),
        ];

        // 준비한 입력으로 결과를 계산하거나 현재 상태를 읽는다.
        const result = merge_klines(existing_klines, incoming_klines, 3);

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(result.map((kline) => [
            kline.open_time,
            kline.interval,
            kline.close,
            kline.is_closed,
        ])).toEqual([
            [200, '1m', 21, false],
            [200, '4h', 40, true],
            [300, '1m', 30, true],
        ]);
    });

    it('aligns EMA9 and 20-period 2σ Bollinger bands with each kline', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const klines = Array.from({ length: 20 }, (_, index) => {
            return create_test_kline(index * 60, index + 1);
        });

        // 준비한 입력으로 결과를 계산하거나 현재 상태를 읽는다.
        const result = calculate_kline_indicators(klines);

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(result).toHaveLength(20);
        expect(result[7]?.ema9).toBeNull();
        expect(result[8]?.ema9).toBe(5);
        expect(result[9]?.ema9).toBe(6);
        expect(result[18]?.bollinger_middle).toBeNull();
        expect(result[19]).toMatchObject({
            open_time: 1_140,
            close_time: 1_199,
            ema9: 16,
            bollinger_middle: 10.5,
        });
        expect(result[19]?.bollinger_upper).toBeCloseTo(22.0325625947, 9);
        expect(result[19]?.bollinger_lower).toBeCloseTo(-1.0325625947, 9);
    });

    it('returns aligned null warmup values for short series', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const klines = [create_test_kline(0, 100), create_test_kline(60, 101)];

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(calculate_kline_indicators(klines)).toEqual([
            {
                symbol: 'ETHUSDT',
                interval: '1m',
                open_time: 0,
                close_time: 59,
                ema9: null,
                bollinger_middle: null,
                bollinger_upper: null,
                bollinger_lower: null,
            },
            {
                symbol: 'ETHUSDT',
                interval: '1m',
                open_time: 60,
                close_time: 119,
                ema9: null,
                bollinger_middle: null,
                bollinger_upper: null,
                bollinger_lower: null,
            },
        ]);
    });
});
