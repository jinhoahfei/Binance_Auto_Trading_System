import type { KlineIndicatorPoint, NormalizedKline } from './types';

const ema9_period = 9;
const bollinger_period = 20;
const bollinger_standard_deviations = 2;

/**
 * 함수 이름: create_kline_key()
 * 기능: symbol, interval, open time을 결합해 kline의 유일 key를 만든다.
 * 인자: kline -> key를 만들 정규화 kline
 * 반환값: symbol, interval, open time이 결합된 key
 * 작성 날짜: 2026/08/20
 */
function create_kline_key(kline: NormalizedKline): string {
    return `${kline.symbol}\u0000${kline.interval}\u0000${kline.open_time}`;
}

/**
 * 함수 이름: compare_klines()
 * 기능: kline을 open time 기준 오름차순으로, 동시점은 key 순으로 정렬한다.
 * 인자: left_kline -> 왼쪽 비교 kline
 *      right_kline -> 오른쪽 비교 kline
 * 반환값: Array.sort에 사용할 비교 결과
 * 작성 날짜: 2026/08/20
 */
function compare_klines(
    left_kline: NormalizedKline,
    right_kline: NormalizedKline,
): number {
    if (left_kline.open_time !== right_kline.open_time) {
        return left_kline.open_time - right_kline.open_time;
    }

    return create_kline_key(left_kline).localeCompare(create_kline_key(right_kline));
}

/**
 * 함수 이름: merge_klines()
 * 기능: REST 기준 kline과 신규 WebSocket kline을 key로 병합하고 최신 개수로 제한한다.
 * 인자: existing_klines -> 기존 REST 또는 병합 kline
 *      incoming_klines -> 기존 동일 key를 덮어쓸 WebSocket kline
 *      max_size -> 반환할 최대 최신 kline 수
 * 반환값: 중복 제거, 오름차순 정렬, 크기 제한된 kline 목록
 * 작성 날짜: 2026/08/20
 */
export function merge_klines(
    existing_klines: ReadonlyArray<NormalizedKline>,
    incoming_klines: ReadonlyArray<NormalizedKline>,
    max_size = 1000,
): ReadonlyArray<NormalizedKline> {
    if (!Number.isInteger(max_size) || max_size < 1) {
        throw new Error('Kline max_size must be a positive integer.');
    }

    const klines_by_key = new Map<string, NormalizedKline>();

    existing_klines.forEach((kline) => {
        klines_by_key.set(create_kline_key(kline), kline);
    });
    incoming_klines.forEach((kline) => {
        klines_by_key.set(create_kline_key(kline), kline);
    });

    const sorted_klines = Array.from(klines_by_key.values()).sort(compare_klines);

    return sorted_klines.slice(Math.max(0, sorted_klines.length - max_size));
}

/**
 * 함수 이름: calculate_average()
 * 기능: 주어진 유한 숫자 목록의 산술 평균을 계산한다.
 * 인자: values -> 평균을 계산할 숫자 목록
 * 반환값: 산술 평균
 * 작성 날짜: 2026/08/20
 */
function calculate_average(values: ReadonlyArray<number>): number {
    return values.reduce((total, value) => total + value, 0) / values.length;
}

/**
 * 함수 이름: validate_indicator_klines()
 * 기능: 지표 계산 입력이 단일 series의 시간순 유한 close 값인지 확인한다.
 * 인자: klines -> 지표를 계산할 kline 목록
 * 반환값: 없음
 * 작성 날짜: 2026/08/20
 */
function validate_indicator_klines(klines: ReadonlyArray<NormalizedKline>): void {
    klines.forEach((kline, index) => {
        if (!Number.isFinite(kline.close)) {
            throw new Error(`Kline at index ${index} must have a finite close value.`);
        }

        if (index === 0) {
            return;
        }

        const previous_kline = klines[index - 1];

        if (previous_kline === undefined) {
            return;
        }

        if (kline.symbol !== previous_kline.symbol || kline.interval !== previous_kline.interval) {
            throw new Error('Indicator klines must belong to one symbol and interval.');
        }

        if (kline.open_time <= previous_kline.open_time) {
            throw new Error('Indicator klines must be strictly chronological.');
        }
    });
}

/**
 * 함수 이름: calculate_kline_indicators()
 * 기능: kline 시간축에 맞춰 EMA9과 20-period 2σ 볼린저밴드를 계산한다.
 * 인자: klines -> 오름차순으로 정렬된 단일 symbol·주기의 kline 목록
 * 반환값: 각 kline 시간에 정렬된 지표, warmup 구간은 null
 * 작성 날짜: 2026/08/20
 */
export function calculate_kline_indicators(
    klines: ReadonlyArray<NormalizedKline>,
): ReadonlyArray<KlineIndicatorPoint> {
    validate_indicator_klines(klines);

    const close_values = klines.map((kline) => kline.close);
    const ema_multiplier = 2 / (ema9_period + 1);
    let previous_ema: number | null = null;

    return klines.map((kline, index) => {
        let ema9: number | null = null;

        if (index === ema9_period - 1) {
            previous_ema = calculate_average(close_values.slice(0, ema9_period));
            ema9 = previous_ema;
        } else if (index >= ema9_period && previous_ema !== null) {
            previous_ema = ((kline.close - previous_ema) * ema_multiplier) + previous_ema;
            ema9 = previous_ema;
        }

        let bollinger_middle: number | null = null;
        let bollinger_upper: number | null = null;
        let bollinger_lower: number | null = null;

        if (index >= bollinger_period - 1) {
            const window_start = index - bollinger_period + 1;
            const window_values = close_values.slice(window_start, index + 1);
            const window_average = calculate_average(window_values);
            const variance = calculate_average(window_values.map((value) => {
                return (value - window_average) ** 2;
            }));
            const standard_deviation = Math.sqrt(Math.max(variance, 0));

            bollinger_middle = window_average;
            bollinger_upper = window_average
                + (bollinger_standard_deviations * standard_deviation);
            bollinger_lower = window_average
                - (bollinger_standard_deviations * standard_deviation);
        }

        return {
            symbol: kline.symbol,
            interval: kline.interval,
            open_time: kline.open_time,
            close_time: kline.close_time,
            ema9,
            bollinger_middle,
            bollinger_upper,
            bollinger_lower,
        };
    });
}
