import type { ChartInterval } from '../types';

export interface NormalizedKline {
    readonly symbol: string;
    readonly interval: ChartInterval;
    readonly open_time: number;
    readonly close_time: number;
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
    readonly ema9: number | null;
    readonly bollinger_middle: number | null;
    readonly bollinger_upper: number | null;
    readonly bollinger_lower: number | null;
}
