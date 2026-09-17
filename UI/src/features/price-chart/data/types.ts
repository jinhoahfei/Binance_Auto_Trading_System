// 가격 차트에서 사용하는 데이터 구조와 입력·표시 타입을 정의한다.

import type { ChartInterval } from '../types';
import type { ChartErrorDiagnostic } from './chartDataError';

export interface NormalizedKline {
    readonly symbol: string;
    readonly interval: ChartInterval;
    readonly open_time: number;
    readonly close_time: number;
    readonly event_time?: number;
    readonly open: number;
    readonly high: number;
    readonly low: number;
    readonly close: number;
    readonly volume: number;
    readonly is_closed: boolean;
}

export interface KlinesByInterval {
    readonly '1m': ReadonlyArray<NormalizedKline>;
    readonly '30m': ReadonlyArray<NormalizedKline>;
    readonly '4h': ReadonlyArray<NormalizedKline>;
    readonly '1d': ReadonlyArray<NormalizedKline>;
}

export interface LoadAllKlinesOptions {
    readonly on_request_event?: (event: ChartErrorDiagnostic & {
        readonly event: 'rest_started' | 'rest_completed' | 'rest_failed' | 'rest_timeout';
        readonly interval: ChartInterval;
        readonly elapsed_ms: number;
    }) => void;
    readonly limit?: number;
    readonly signal?: AbortSignal;
    readonly fetch_implementation?: typeof fetch;
}

export interface LoadOlderKlinesOptions {
    readonly limit?: number;
    readonly signal?: AbortSignal;
    readonly fetch_implementation?: typeof fetch;
}

export interface KlineIndicatorPoint {
    readonly symbol: string;
    readonly interval: ChartInterval;
    readonly open_time: number;
    readonly close_time: number;
    readonly event_time?: number;
    readonly ema9: number | null;
    readonly bollinger_middle: number | null;
    readonly bollinger_upper: number | null;
    readonly bollinger_lower: number | null;
}
