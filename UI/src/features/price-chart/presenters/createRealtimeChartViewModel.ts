import {
    calculate_kline_indicators,
    type NormalizedKline,
} from '../data';
import type { RealtimeChartDataSnapshot } from '../hooks';
import type {
    CandleViewModel,
    ChartInterval,
    LinePointViewModel,
    PriceChartDataStatus,
} from '../types';

export interface RealtimePriceChartViewModel {
    readonly bollingerLower: ReadonlyArray<LinePointViewModel>;
    readonly bollingerUpper: ReadonlyArray<LinePointViewModel>;
    readonly candles: ReadonlyArray<CandleViewModel>;
    readonly dataStatus: PriceChartDataStatus;
    readonly dataRevision: number;
    readonly ema: ReadonlyArray<LinePointViewModel>;
    readonly statusMessage: string | null;
    readonly symbol: string;
    readonly timestampLabel: string;
}

/**
 * 함수 이름: read_date_part()
 * 기능: Intl 날짜 part 목록에서 요청한 값을 안전하게 읽는다.
 * 인자: date_parts -> Intl이 분리한 날짜 part, part_type -> 찾을 part 종류
 * 반환값: 날짜 part 값 또는 대체 문자열
 * 작성 날짜: 2026/08/20
 */
function read_date_part(
    date_parts: ReadonlyArray<Intl.DateTimeFormatPart>,
    part_type: Intl.DateTimeFormatPartTypes,
): string {
    return date_parts.find((part) => part.type === part_type)?.value ?? '--';
}

/**
 * 함수 이름: format_timestamp_label()
 * 기능: 시장 snapshot 갱신 시각을 KST 날짜·시각 라벨로 변환한다.
 * 인자: updated_at -> 밀리초 단위 UNIX 갱신 시각
 * 반환값: YYYY.MM.DD · HH:mm:ss KST 또는 연결 대기 라벨
 * 작성 날짜: 2026/08/20
 */
function format_timestamp_label(updated_at: number | null): string {
    if (updated_at === null) {
        return '실시간 연결 대기 중';
    }

    const date_parts = new Intl.DateTimeFormat('en-CA', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hourCycle: 'h23',
        timeZone: 'Asia/Seoul',
    }).formatToParts(new Date(updated_at));
    return `${read_date_part(date_parts, 'year')}.${read_date_part(date_parts, 'month')}`
        + `.${read_date_part(date_parts, 'day')} · ${read_date_part(date_parts, 'hour')}`
        + `:${read_date_part(date_parts, 'minute')}:${read_date_part(date_parts, 'second')} KST`;
}

/**
 * 함수 이름: create_candle_view_model()
 * 기능: 정규화된 Binance kline을 가격 차트 candle ViewModel로 변환한다.
 * 인자: kline -> 변환할 정규화 kline
 * 반환값: 가격·시각·거래량을 포함한 candle ViewModel
 * 작성 날짜: 2026/08/20
 */
function create_candle_view_model(kline: NormalizedKline): CandleViewModel {
    return {
        close: kline.close,
        high: kline.high,
        is_closed: kline.is_closed,
        low: kline.low,
        open: kline.open,
        open_time: kline.open_time,
        volume: kline.volume,
    };
}

/**
 * 함수 이름: create_realtime_chart_view_model()
 * 기능: 선택 봉의 MarketSnapshot과 계산 지표를 PriceChart Boundary ViewModel로 투영한다.
 * 인자: snapshot -> 네 주기의 실시간 시장 snapshot, interval -> 현재 차트 주기
 * 반환값: 캔들·EMA9·볼린저밴드·연결 상태 ViewModel
 * 작성 날짜: 2026/08/20
 */
export function create_realtime_chart_view_model(
    snapshot: RealtimeChartDataSnapshot,
    interval: ChartInterval,
): RealtimePriceChartViewModel {
    const klines = snapshot.klines_by_interval[interval];
    const indicator_points = calculate_kline_indicators(klines);
    const ema = indicator_points.flatMap((point) => {
        return point.ema9 === null ? [] : [{ open_time: point.open_time, value: point.ema9 }];
    });
    const bollinger_upper = indicator_points.flatMap((point) => {
        return point.bollinger_upper === null
            ? []
            : [{ open_time: point.open_time, value: point.bollinger_upper }];
    });
    const bollinger_lower = indicator_points.flatMap((point) => {
        return point.bollinger_lower === null
            ? []
            : [{ open_time: point.open_time, value: point.bollinger_lower }];
    });

    return {
        bollingerLower: bollinger_lower,
        bollingerUpper: bollinger_upper,
        candles: klines.map(create_candle_view_model),
        dataStatus: snapshot.data_status,
        dataRevision: snapshot.data_revision ?? 0,
        ema,
        statusMessage: snapshot.status_message,
        symbol: snapshot.symbol,
        timestampLabel: format_timestamp_label(snapshot.updated_at_by_interval === undefined
            ? snapshot.updated_at : snapshot.updated_at_by_interval[interval] ?? null),
    };
}
