import {
    CandlestickSeries,
    ColorType,
    CrosshairMode,
    HistogramSeries,
    LineSeries,
    LineStyle,
    TickMarkType,
    createChart,
    type CandlestickData,
    type Coordinate,
    type HistogramData,
    type IChartApi,
    type ISeriesApi,
    type LineData,
    type MouseEventParams,
    type Time,
    type UTCTimestamp,
} from 'lightweight-charts';
import { useEffect, useMemo, useRef, useState } from 'react';

import type {
    CandleViewModel,
    ChartInterval,
    IndicatorSettingsViewModel,
    LinePointViewModel,
    PriceChartPresentationMode,
} from '../types';

import styles from './LightweightChartSurface.module.css';
import { record_chart_diagnostic } from '../data/chartDiagnostics';
import { describe_chart_data_error } from '../data/chartDataError';

const FALLBACK_CHART_END_TIME = Date.UTC(2026, 5, 22, 2, 0, 0);
const INITIAL_VISIBLE_BAR_COUNT = 180;
const DEFAULT_MINIMUM_BAR_SPACING = 0.5;
const INITIAL_MINIMUM_BAR_SPACING = 0.00001;
const OVERVIEW_RANGE_MULTIPLIER = 2;
const RIGHT_OFFSET_BAR_COUNT = 2.35;
const VISIBLE_RANGE_BAR_BUFFER = 2;
const FIXTURE_BASE_CHART_TIME = Date.UTC(2026, 5, 22, 9, 0, 0) / 1_000;
const FIXTURE_CHART_TIME_STEP_SECONDS = 5 * 60;
const INTERVAL_DURATION_MILLISECONDS: Readonly<Record<ChartInterval, number>> = {
    '1m': 60_000,
    '30m': 30 * 60_000,
    '4h': 4 * 60 * 60_000,
    '1d': 24 * 60 * 60_000,
};

interface LightweightChartHandles {
    readonly chart: IChartApi;
    readonly bollinger_lower_series: ISeriesApi<'Line'>;
    readonly bollinger_upper_series: ISeriesApi<'Line'>;
    readonly candle_series: ISeriesApi<'Candlestick'>;
    readonly ema_series: ISeriesApi<'Line'>;
    readonly volume_series: ISeriesApi<'Histogram'>;
}

interface HoveredCandleViewModel {
    readonly close: number;
    readonly high: number;
    readonly low: number;
    readonly open: number;
    readonly open_time: number;
    readonly volume: number;
}

interface SeriesRenderState {
    readonly bollinger_is_visible: boolean;
    readonly candle_count: number;
    readonly data_revision: number;
    readonly ema_is_visible: boolean;
    readonly first_open_time: number | null;
    readonly interval: ChartInterval;
}

export interface ChartPixelPoint {
    readonly x: number;
    readonly y: number;
}

export interface ChartDataPoint {
    readonly open_time: number;
    readonly price: number;
}

export interface ChartCoordinateSpace {
    readonly bars_before: number | null;
    readonly pane_height: number;
    readonly pane_width: number;
    readonly visible_from: number | null;
    readonly visible_to: number | null;
    readonly convert_coordinates_to_point: (
        x_coordinate: number,
        y_coordinate: number,
    ) => ChartDataPoint | null;
    readonly convert_point_to_coordinates: (
        open_time: number,
        price: number,
    ) => ChartPixelPoint | null;
}

export interface LightweightChartSurfaceProps {
    readonly dataRevision?: number;
    readonly position_average_entry_price?: string | null;
    readonly bollingerLower: ReadonlyArray<LinePointViewModel>;
    readonly bollingerUpper: ReadonlyArray<LinePointViewModel>;
    readonly candles: ReadonlyArray<CandleViewModel>;
    readonly ema: ReadonlyArray<LinePointViewModel>;
    readonly indicatorSettings: IndicatorSettingsViewModel;
    readonly interval: ChartInterval;
    readonly onCoordinateSpaceChange?: ((coordinate_space: ChartCoordinateSpace | null) => void) | undefined;
    readonly presentationMode?: PriceChartPresentationMode;
    readonly symbol?: string;
}

/**
 * 함수 이름: get_candle_open_time()
 * 기능: 실시간 봉의 시작 시각을 사용하고 fixture에는 주기 기반 결정적 시각을 보완한다.
 * 인자: candle -> 표시할 봉, index -> 봉 순번, candle_count -> 전체 봉 수, interval -> 봉 주기
 * 반환값: 밀리초 단위 UNIX 시작 시각
 * 작성 날짜: 2026/08/20
 */
function get_candle_open_time(
    candle: CandleViewModel,
    index: number,
    candle_count: number,
    interval: ChartInterval,
): number {
    if (candle.open_time !== undefined) {
        return candle.open_time;
    }

    const interval_duration = INTERVAL_DURATION_MILLISECONDS[interval];

    return FALLBACK_CHART_END_TIME - ((candle_count - index) * interval_duration);
}

/**
 * 함수 이름: create_chart_time()
 * 기능: 밀리초 단위 봉 시작 시각을 Lightweight Charts의 UNIX 초 시각으로 변환한다.
 * 인자: open_time -> 밀리초 단위 UNIX 시작 시각
 * 반환값: Lightweight Charts UNIX timestamp
 * 작성 날짜: 2026/08/20
 */
function create_chart_time(open_time: number): UTCTimestamp {
    return Math.floor(open_time / 1_000) as UTCTimestamp;
}

/**
 * 함수 이름: read_time_milliseconds()
 * 기능: Lightweight Charts의 UNIX timestamp 또는 BusinessDay를 밀리초 시각으로 변환한다.
 * 인자: chart_time -> 차트가 전달한 시각 값
 * 반환값: 밀리초 단위 UNIX 시각 또는 변환할 수 없으면 null
 * 작성 날짜: 2026/08/20
 */
function read_time_milliseconds(chart_time: Time | undefined): number | null {
    if (typeof chart_time === 'number') {
        return chart_time * 1_000;
    }
    if (chart_time === undefined) {
        return null;
    }
    if (typeof chart_time === 'string') {
        const parsed_time = Date.parse(chart_time);

        return Number.isFinite(parsed_time) ? parsed_time : null;
    }

    return Date.UTC(chart_time.year, chart_time.month - 1, chart_time.day);
}

/**
 * 함수 이름: get_line_open_time()
 * 기능: 지표의 명시 시각 또는 대응 봉 시각을 사용해 선 point를 봉과 정렬한다.
 * 인자: point -> 지표 point, index -> point 순번, point_count -> 전체 point 수, candles -> 봉 목록, interval -> 봉 주기
 * 반환값: 밀리초 단위 UNIX 시작 시각
 * 작성 날짜: 2026/08/20
 */
function get_line_open_time(
    point: LinePointViewModel,
    index: number,
    point_count: number,
    candles: ReadonlyArray<CandleViewModel>,
    interval: ChartInterval,
): number {
    if (point.open_time !== undefined) {
        return point.open_time;
    }

    const candle_index = candles.length - point_count + index;
    const candle = candles[candle_index];

    if (candle !== undefined) {
        return get_candle_open_time(candle, candle_index, candles.length, interval);
    }

    return FALLBACK_CHART_END_TIME + (index * INTERVAL_DURATION_MILLISECONDS[interval]);
}

/**
 * 함수 이름: create_candlestick_data()
 * 기능: 화면 봉 ViewModel을 Lightweight Charts candlestick data로 변환한다.
 * 인자: candle -> 변환할 봉, index -> 봉 순번, candle_count -> 전체 봉 수, interval -> 봉 주기
 * 반환값: 시각과 실제 OHLCV를 포함한 candlestick data
 * 작성 날짜: 2026/08/20
 */
function create_candlestick_data(
    candle: CandleViewModel,
    index: number,
    candle_count: number,
    interval: ChartInterval,
): CandlestickData<UTCTimestamp> {
    return {
        close: candle.close,
        customValues: { volume: candle.volume ?? 0 },
        high: candle.high,
        low: candle.low,
        open: candle.open,
        time: create_chart_time(get_candle_open_time(candle, index, candle_count, interval)),
    };
}

/**
 * 함수 이름: create_volume_data()
 * 기능: 화면 봉 ViewModel을 실제 거래량 histogram data로 변환한다.
 * 인자: candle -> 변환할 봉, index -> 봉 순번, candle_count -> 전체 봉 수, interval -> 봉 주기
 * 반환값: 상승·하락 색상과 base asset 거래량을 포함한 histogram data
 * 작성 날짜: 2026/08/20
 */
function create_volume_data(
    candle: CandleViewModel,
    index: number,
    candle_count: number,
    interval: ChartInterval,
): HistogramData<UTCTimestamp> {
    return {
        color: candle.close >= candle.open ? '#0eb77b66' : '#f0385866',
        time: create_chart_time(get_candle_open_time(candle, index, candle_count, interval)),
        value: candle.volume ?? 0,
    };
}

/**
 * 함수 이름: create_line_data()
 * 기능: 지표 ViewModel을 대응 봉 시각의 Lightweight Charts line data로 변환한다.
 * 인자: point -> 변환할 지표 point, index -> point 순번, point_count -> 전체 point 수,
 *      candles -> 봉 목록, interval -> 봉 주기
 * 반환값: 봉 시간축에 정렬된 line data
 * 작성 날짜: 2026/08/20
 */
function create_line_data(
    point: LinePointViewModel,
    index: number,
    point_count: number,
    candles: ReadonlyArray<CandleViewModel>,
    interval: ChartInterval,
): LineData<UTCTimestamp> {
    return {
        time: create_chart_time(get_line_open_time(
            point,
            index,
            point_count,
            candles,
            interval,
        )),
        value: point.value,
    };
}

/**
 * 함수 이름: calculate_minimum_bar_spacing()
 * 기능: 현재 적재된 모든 봉과 이동 여유 공간을 최대 축소 화면에 수용할 최소 x축 간격을 계산한다.
 * 인자: time_scale_width -> 가격축을 제외한 시간축 폭, candle_count -> 현재 적재 봉 수
 * 반환값: 0보다 크고 기본 제한 이하인 봉 간격
 * 작성 날짜: 2026/08/20
 */
function calculate_minimum_bar_spacing(
    time_scale_width: number,
    candle_count: number,
): number {
    if (!Number.isFinite(time_scale_width) || time_scale_width <= 0 || candle_count < 1) {
        return INITIAL_MINIMUM_BAR_SPACING;
    }

    const required_bar_count = (candle_count * OVERVIEW_RANGE_MULTIPLIER)
        + RIGHT_OFFSET_BAR_COUNT
        + VISIBLE_RANGE_BAR_BUFFER;

    return Math.min(DEFAULT_MINIMUM_BAR_SPACING, time_scale_width / required_bar_count);
}

/**
 * 함수 이름: apply_minimum_bar_spacing()
 * 기능: 적재 봉 수와 최신 시간축 폭에 맞는 축소 한계를 값이 달라졌을 때만 적용한다.
 * 인자: handles -> chart와 series handle, candle_count -> 현재 적재 봉 수,
 *      current_minimum -> 직전에 적용한 최소 간격
 * 반환값: 현재 적용된 최소 봉 간격
 * 작성 날짜: 2026/08/20
 */
function apply_minimum_bar_spacing(
    handles: LightweightChartHandles,
    candle_count: number,
    current_minimum: number | null,
): number {
    const minimum_bar_spacing = calculate_minimum_bar_spacing(
        handles.chart.timeScale().width(),
        candle_count,
    );

    if (minimum_bar_spacing !== current_minimum) {
        handles.chart.timeScale().applyOptions({ minBarSpacing: minimum_bar_spacing });
    }

    return minimum_bar_spacing;
}

/**
 * 함수 이름: read_color_token()
 * 기능: 차트 canvas에서 사용할 semantic CSS 색상 토큰의 계산값을 읽는다.
 * 인자: token_name -> CSS custom property 이름, fallback -> 토큰을 읽지 못했을 때의 색상
 * 반환값: canvas가 해석할 수 있는 색상 문자열
 * 작성 날짜: 2026/08/12
 */
function read_color_token(token_name: string, fallback: string): string {
    const token_value = getComputedStyle(document.documentElement).getPropertyValue(token_name).trim();

    return token_value || fallback;
}

/**
 * 함수 이름: format_chart_price()
 * 기능: 가격축과 봉 정보에 사용할 ETH/USDT 가격 문자열을 만든다.
 * 인자: price -> 표시할 가격
 * 반환값: 천 단위 구분과 소수점 2자리 반올림을 적용한 가격
 * 작성 날짜: 2026/08/20
 */
function format_chart_price(price: number): string {
    return new Intl.NumberFormat('en-US', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
    }).format(price);
}

/**
 * 함수 이름: format_chart_volume()
 * 기능: 선택 봉의 실제 base asset 거래량을 읽기 쉬운 문자열로 만든다.
 * 인자: volume -> 표시할 거래량
 * 반환값: 천 단위 구분과 소수점 4자리 반올림을 적용한 ETH 거래량
 * 작성 날짜: 2026/08/20
 */
function format_chart_volume(volume: number): string {
    return new Intl.NumberFormat('en-US', {
        minimumFractionDigits: 4,
        maximumFractionDigits: 4,
    }).format(volume);
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
 * 함수 이름: format_hover_time()
 * 기능: 선택 봉 시작 시각을 Binance 형식과 가까운 KST 라벨로 변환한다.
 * 인자: open_time -> 밀리초 단위 UNIX 시작 시각
 * 반환값: YYYY.MM.DD HH:mm KST 문자열
 * 작성 날짜: 2026/08/20
 */
function format_hover_time(open_time: number): string {
    const date_parts = new Intl.DateTimeFormat('en-CA', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        hourCycle: 'h23',
        timeZone: 'Asia/Seoul',
    }).formatToParts(new Date(open_time));

    return `${read_date_part(date_parts, 'year')}.${read_date_part(date_parts, 'month')}`
        + `.${read_date_part(date_parts, 'day')} ${read_date_part(date_parts, 'hour')}`
        + `:${read_date_part(date_parts, 'minute')} KST`;
}

/**
 * 함수 이름: format_tick_mark()
 * 기능: 실제 차트 시간축 tick을 KST 날짜 또는 시각으로 표시한다.
 * 인자: chart_time -> 차트 tick 시각, tick_mark_type -> tick 표시 단계
 * 반환값: KST 기준 시간축 라벨
 * 작성 날짜: 2026/08/20
 */
function format_tick_mark(chart_time: Time, tick_mark_type: TickMarkType): string {
    const open_time = read_time_milliseconds(chart_time);

    if (open_time === null) {
        return '';
    }
    if (tick_mark_type === TickMarkType.Year) {
        return new Intl.DateTimeFormat('ko-KR', {
            year: 'numeric',
            timeZone: 'Asia/Seoul',
        }).format(new Date(open_time));
    }
    if (tick_mark_type === TickMarkType.Month || tick_mark_type === TickMarkType.DayOfMonth) {
        return new Intl.DateTimeFormat('ko-KR', {
            month: '2-digit',
            day: '2-digit',
            timeZone: 'Asia/Seoul',
        }).format(new Date(open_time));
    }

    return new Intl.DateTimeFormat('ko-KR', {
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
        timeZone: 'Asia/Seoul',
    }).format(new Date(open_time));
}

/**
 * 함수 이름: read_base_asset()
 * 기능: Binance symbol에서 hover 거래량 단위로 사용할 base asset을 읽는다.
 * 인자: symbol -> Binance 거래 symbol
 * 반환값: USDT 앞의 base asset 또는 원본 symbol
 * 작성 날짜: 2026/08/20
 */
function read_base_asset(symbol: string): string {
    return symbol.endsWith('USDT') ? symbol.slice(0, -4) : symbol;
}

/**
 * 함수 이름: calculate_change_rate()
 * 기능: 선택 봉 시가 대비 종가 등락률을 계산한다.
 * 인자: candle -> 계산할 봉
 * 반환값: 백분율 단위 등락률
 * 작성 날짜: 2026/08/20
 */
function calculate_change_rate(candle: HoveredCandleViewModel): number {
    return candle.open === 0 ? 0 : ((candle.close - candle.open) / candle.open) * 100;
}

/**
 * 함수 이름: calculate_price_range_rate()
 * 기능: Binance Range와 같은 시가 대비 고가·저가 변동폭을 계산한다.
 * 인자: candle -> 계산할 봉
 * 반환값: 백분율 단위 변동폭
 * 작성 날짜: 2026/08/20
 */
function calculate_price_range_rate(candle: HoveredCandleViewModel): number {
    return candle.open === 0 ? 0 : ((candle.high - candle.low) / candle.open) * 100;
}

/**
 * 함수 이름: create_latest_candle_view_model()
 * 기능: pointer가 차트를 벗어났을 때 표시할 최신 봉 정보를 만든다.
 * 인자: candles -> 현재 주기의 봉 목록, interval -> 현재 봉 주기
 * 반환값: 최신 봉 정보 또는 봉이 없으면 null
 * 작성 날짜: 2026/08/20
 */
function create_latest_candle_view_model(
    candles: ReadonlyArray<CandleViewModel>,
    interval: ChartInterval,
): HoveredCandleViewModel | null {
    const candle = candles.at(-1);

    if (candle === undefined) {
        return null;
    }

    return {
        close: candle.close,
        high: candle.high,
        low: candle.low,
        open: candle.open,
        open_time: get_candle_open_time(candle, candles.length - 1, candles.length, interval),
        volume: candle.volume ?? 0,
    };
}

/**
 * 함수 이름: create_chart_coordinate_space()
 * 기능: Lightweight Charts의 현재 visible range와 시각·가격 좌표 변환기를 만든다.
 * 인자: handles -> chart와 candle series handle
 * 반환값: 현재 pane 좌표계 또는 유효한 pane이 없으면 null
 * 작성 날짜: 2026/08/20
 */
function create_chart_coordinate_space(handles: LightweightChartHandles): ChartCoordinateSpace | null {
    const pane_size = handles.chart.paneSize();

    if (pane_size.width <= 0 || pane_size.height <= 0) {
        return null;
    }

    const visible_range = handles.chart.timeScale().getVisibleRange();
    const visible_logical_range = handles.chart.timeScale().getVisibleLogicalRange();
    const visible_bars = visible_logical_range === null
        ? null
        : handles.candle_series.barsInLogicalRange(visible_logical_range);

    return {
        bars_before: visible_bars?.barsBefore ?? null,
        pane_height: pane_size.height,
        pane_width: pane_size.width,
        visible_from: visible_range === null ? null : read_time_milliseconds(visible_range.from),
        visible_to: visible_range === null ? null : read_time_milliseconds(visible_range.to),
        convert_coordinates_to_point: (x_coordinate, y_coordinate) => {
            const chart_time = handles.chart.timeScale().coordinateToTime(x_coordinate as Coordinate);
            const price = handles.candle_series.coordinateToPrice(y_coordinate);
            const open_time = read_time_milliseconds(chart_time ?? undefined);

            if (open_time === null || price === null || !Number.isFinite(price)) {
                return null;
            }

            return { open_time, price };
        },
        convert_point_to_coordinates: (open_time, price) => {
            const x_coordinate = handles.chart.timeScale().timeToCoordinate(create_chart_time(open_time));
            const y_coordinate = handles.candle_series.priceToCoordinate(price);

            if (x_coordinate === null || y_coordinate === null) {
                return null;
            }

            return { x: x_coordinate, y: y_coordinate };
        },
    };
}

/**
 * 함수 이름: create_lightweight_chart()
 * 기능: Binance와 유사한 확대·이동·동적 축·crosshair를 가진 금융 차트를 생성한다.
 * 인자: container -> chart canvas를 소유할 HTML 요소
 * 반환값: 갱신과 정리에 필요한 chart 및 series handle
 * 작성 날짜: 2026/08/20
 */
function create_lightweight_chart(container: HTMLDivElement): LightweightChartHandles {
    // 캔들과 보조지표에 사용할 색상을 공통 CSS 토큰에서 읽는다.
    const positive_color = read_color_token('--color-status-positive', '#0eb77b');
    const negative_color = read_color_token('--color-status-negative', '#f03858');
    const information_color = read_color_token('--color-status-info', '#1768d4');
    const bollinger_color = read_color_token('--color-status-warning', '#f0b90b');  // 볼린저밴드 설정 항목의 주황색을 사용한다.
    const border_color = read_color_token('--color-border-default', '#33404b');
    const text_color = read_color_token('--color-text-secondary', '#a7afbb');
    const chart = createChart(container, {
        autoSize: true,
        layout: {
            attributionLogo: false,
            background: { type: ColorType.Solid, color: 'transparent' },
            fontFamily: 'var(--font-numeric)',
            fontSize: 12,
            textColor: text_color,
        },
        localization: {
            locale: 'ko-KR',
            priceFormatter: format_chart_price,
            timeFormatter: (chart_time: Time) => {
                const open_time = read_time_milliseconds(chart_time);

                return open_time === null ? '' : format_hover_time(open_time);
            },
        },
        grid: {
            horzLines: { color: border_color, style: LineStyle.Solid, visible: true },
            vertLines: { color: border_color, style: LineStyle.Solid, visible: true },
        },
        crosshair: {
            mode: CrosshairMode.Normal,
            horzLine: {
                color: text_color,
                labelBackgroundColor: border_color,
                labelVisible: true,
                style: LineStyle.Dashed,
                visible: true,
                width: 1,
            },
            vertLine: {
                color: text_color,
                labelBackgroundColor: border_color,
                labelVisible: true,
                style: LineStyle.Dashed,
                visible: true,
                width: 1,
            },
        },
        handleScale: {
            mouseWheel: true,
            pinch: true,
            axisPressedMouseMove: { time: true, price: true },
            axisDoubleClickReset: { time: true, price: true },
        },
        handleScroll: {
            mouseWheel: true,
            pressedMouseMove: true,
            horzTouchDrag: true,
            vertTouchDrag: true,
        },
        kineticScroll: { mouse: true, touch: true },
        leftPriceScale: { visible: false },
        rightPriceScale: {
            autoScale: true,
            visible: true,
            borderColor: border_color,
            borderVisible: true,
            minimumWidth: 58,
            scaleMargins: { top: 0.16, bottom: 0.22 },
        },
        timeScale: {
            barSpacing: 12,
            conflationThresholdFactor: 1,
            enableConflation: true,
            visible: true,
            borderColor: border_color,
            borderVisible: true,
            fixLeftEdge: false,
            fixRightEdge: false,
            minBarSpacing: INITIAL_MINIMUM_BAR_SPACING,
            precomputeConflationOnInit: false,
            rightOffset: RIGHT_OFFSET_BAR_COUNT,
            secondsVisible: false,
            tickMarkFormatter: (chart_time: Time, tick_mark_type: TickMarkType) => {
                return format_tick_mark(chart_time, tick_mark_type);
            },
            timeVisible: true,
        },
    });
    // 현재가는 오른쪽 가격표만 유지하고 차트 전체를 가로지르는 기본 기준선은 숨긴다.
    const candle_series = chart.addSeries(CandlestickSeries, {
        borderVisible: false,
        downColor: negative_color,
        lastValueVisible: true,
        priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
        priceLineVisible: false,  // 별도 기준선은 열린 포지션의 평단가 표시에만 사용한다.
        upColor: positive_color,
        wickDownColor: negative_color,
        wickUpColor: positive_color,
    });

    // 볼린저밴드의 상단과 하단을 동일한 주황색 실선으로 표시한다.
    const bollinger_upper_series = chart.addSeries(LineSeries, {
        color: bollinger_color,
        crosshairMarkerVisible: false,
        lastValueVisible: false,
        lineStyle: LineStyle.Solid,
        lineWidth: 1,
        priceLineVisible: false,
    });
    const bollinger_lower_series = chart.addSeries(LineSeries, {
        color: bollinger_color,
        crosshairMarkerVisible: false,
        lastValueVisible: false,
        lineStyle: LineStyle.Solid,
        lineWidth: 1,
        priceLineVisible: false,
    });
    const ema_series = chart.addSeries(LineSeries, {
        color: information_color,
        crosshairMarkerVisible: false,
        lastValueVisible: false,
        lineWidth: 2,
        priceLineVisible: false,
    });
    const volume_series = chart.addSeries(HistogramSeries, {
        lastValueVisible: false,
        priceFormat: { type: 'volume' },
        priceLineVisible: false,
        priceScaleId: 'volume',
    });

    chart.priceScale('volume').applyOptions({
        scaleMargins: { top: 0.82, bottom: 0 },
    });

    return {
        chart,
        bollinger_lower_series,
        bollinger_upper_series,
        candle_series,
        ema_series,
        volume_series,
    };
}

/**
 * 함수 이름: create_fixture_lightweight_chart()
 * 기능: 고정 Figma 기준 화면의 축 없는 결정적 차트 인스턴스를 생성한다.
 * 인자: container -> fixture chart canvas를 소유할 HTML 요소
 * 반환값: 고정 series 갱신과 정리에 필요한 chart handle
 * 작성 날짜: 2026/08/29
 */
function create_fixture_lightweight_chart(container: HTMLDivElement): LightweightChartHandles {
    // 고정 차트도 실제 차트와 같은 지표 색상 토큰을 사용한다.
    const positive_color = read_color_token('--color-status-positive', '#0eb77b');
    const negative_color = read_color_token('--color-status-negative', '#f03858');
    const information_color = read_color_token('--color-status-info', '#1768d4');
    const bollinger_color = read_color_token('--color-status-warning', '#f0b90b');  // 볼린저밴드 설정 항목의 주황색을 사용한다.

    // Figma 기준은 SVG 격자·축과 조합되므로 library 자체의 축·격자·상호작용은 숨긴다.
    const chart = createChart(container, {
        autoSize: true,
        height: 328,
        layout: {
            attributionLogo: false,
            background: { type: ColorType.Solid, color: 'transparent' },
            textColor: 'transparent',
        },
        grid: {
            horzLines: { visible: false },
            vertLines: { visible: false },
        },
        crosshair: {
            horzLine: { visible: false, labelVisible: false },
            vertLine: { visible: false, labelVisible: false },
        },
        handleScale: false,
        handleScroll: false,
        leftPriceScale: { visible: false },
        rightPriceScale: {
            visible: false,
            borderVisible: false,
            scaleMargins: { top: 0, bottom: 0.13 },
        },
        timeScale: {
            barSpacing: 20,
            visible: false,
            borderVisible: false,
            fixLeftEdge: false,
            fixRightEdge: false,
            rightOffset: RIGHT_OFFSET_BAR_COUNT,
        },
    });

    // 고정 series는 2026-08-12 Figma export와 같은 장식 없는 표현만 활성화한다.
    const candle_series = chart.addSeries(CandlestickSeries, {
        borderVisible: false,
        downColor: negative_color,
        lastValueVisible: false,
        priceLineVisible: false,
        upColor: positive_color,
        wickDownColor: negative_color,
        wickUpColor: positive_color,
    });

    // 고정 화면에서도 볼린저밴드 상단과 하단의 주황색 실선 표현을 유지한다.
    const bollinger_upper_series = chart.addSeries(LineSeries, {
        color: bollinger_color,
        crosshairMarkerVisible: false,
        lastValueVisible: false,
        lineStyle: LineStyle.Solid,
        lineWidth: 1,
        priceLineVisible: false,
    });
    const bollinger_lower_series = chart.addSeries(LineSeries, {
        color: bollinger_color,
        crosshairMarkerVisible: false,
        lastValueVisible: false,
        lineStyle: LineStyle.Solid,
        lineWidth: 1,
        priceLineVisible: false,
    });
    const ema_series = chart.addSeries(LineSeries, {
        color: information_color,
        crosshairMarkerVisible: false,
        lastValueVisible: false,
        lineWidth: 2,
        priceLineVisible: false,
    });
    const volume_series = chart.addSeries(HistogramSeries, {
        lastValueVisible: false,
        priceFormat: { type: 'volume' },
        priceLineVisible: false,
        priceScaleId: 'volume',
    });

    chart.priceScale('volume').applyOptions({
        scaleMargins: { top: 0.82, bottom: 0 },
    });

    return {
        chart,
        bollinger_lower_series,
        bollinger_upper_series,
        candle_series,
        ema_series,
        volume_series,
    };
}

/**
 * 함수 이름: create_fixture_chart_time()
 * 기능: fixture 순번을 Figma 기준의 결정적 5분 간격 chart 시각으로 변환한다.
 * 인자: index -> candle 또는 indicator의 논리 순번
 * 반환값: Lightweight Charts UNIX timestamp
 * 작성 날짜: 2026/08/29
 */
function create_fixture_chart_time(index: number): UTCTimestamp {
    return (FIXTURE_BASE_CHART_TIME + index * FIXTURE_CHART_TIME_STEP_SECONDS) as UTCTimestamp;
}

/**
 * 함수 이름: read_crosshair_candle()
 * 기능: crosshair event에서 실제 OHLCV와 봉 시작 시각을 안전하게 읽는다.
 * 인자: event -> Lightweight Charts crosshair event, handles -> series handle
 * 반환값: 선택 봉 정보 또는 data point가 아니면 null
 * 작성 날짜: 2026/08/20
 */
function read_crosshair_candle(
    event: MouseEventParams<Time>,
    handles: LightweightChartHandles,
): HoveredCandleViewModel | null {
    const candle_data = event.seriesData.get(handles.candle_series);
    const volume_data = event.seriesData.get(handles.volume_series);
    const open_time = read_time_milliseconds(event.time);

    if (open_time === null
        || candle_data === undefined
        || !('open' in candle_data)
        || !('high' in candle_data)
        || !('low' in candle_data)
        || !('close' in candle_data)) {
        return null;
    }

    const custom_volume = candle_data.customValues?.volume;
    const volume = volume_data !== undefined && 'value' in volume_data
        ? volume_data.value
        : typeof custom_volume === 'number'
            ? custom_volume
            : 0;

    return {
        close: candle_data.close,
        high: candle_data.high,
        low: candle_data.low,
        open: candle_data.open,
        open_time,
        volume,
    };
}

/**
 * 함수 이름: LightweightChartSurface()
 * 기능: 실시간 캔들·지표·거래량과 동적 축, pan/zoom, 선택 봉 정보를 금융 차트에 표시한다.
 * 인자: props -> 시장 데이터, 지표 설정, symbol과 drawing 좌표 callback
 * 반환값: 금융 차트와 Binance 형식의 선택 봉 정보 surface
 * 작성 날짜: 2026/08/20
 */
export function LightweightChartSurface({
    dataRevision = 0,
    bollingerLower,
    bollingerUpper,
    candles,
    ema,
    indicatorSettings,
    interval,
    onCoordinateSpaceChange,
    presentationMode = 'interactive',
    position_average_entry_price = null,
    symbol = 'ETHUSDT',
}: LightweightChartSurfaceProps) {
    const container_ref = useRef<HTMLDivElement | null>(null);
    const coordinate_space_publish_ref = useRef<() => void>(() => undefined);
    const earliest_open_time_ref = useRef<number | null>(null);
    const fitted_interval_ref = useRef<ChartInterval | null>(null);
    const handles_ref = useRef<LightweightChartHandles | null>(null);
    const loaded_candle_count_ref = useRef(candles.length);
    const minimum_bar_spacing_ref = useRef<number | null>(null);
    const series_render_state_ref = useRef<SeriesRenderState | null>(null);
    const render_diagnostic_ref = useRef({ interval, next_at: 0 });
    const [hovered_candle, set_hovered_candle] = useState<HoveredCandleViewModel | null>(null);
    const [is_ready, set_is_ready] = useState(false);
    const latest_candle = useMemo(() => {
        return create_latest_candle_view_model(candles, interval);
    }, [candles, interval]);
    const displayed_candle = hovered_candle ?? latest_candle;

    loaded_candle_count_ref.current = candles.length;

    useEffect(() => {
        const container = container_ref.current;

        if (container === null || typeof ResizeObserver === 'undefined') {
            return undefined;
        }

        const handles = presentationMode === 'fixture'
            ? create_fixture_lightweight_chart(container)
            : create_lightweight_chart(container);
        let coordinate_animation_frame: number | null = null;

        handles_ref.current = handles;

        // Fixture는 정적 Figma 좌표를 사용하므로 live pan/zoom 구독을 만들지 않는다.
        if (presentationMode === 'fixture') {
            return () => {
                handles.chart.remove();
                handles_ref.current = null;
            };
        }

        /**
         * 함수 이름: publish_coordinate_space()
         * 기능: 현재 pane 좌표계를 ChartCanvas drawing overlay에 전달한다.
         * 인자: 없음
         * 반환값: 없음
         * 작성 날짜: 2026/08/20
         */
        function publish_coordinate_space(): void {
            // StrictMode 재생성이나 unmount 뒤 도착한 callback은 제거된 pane을 읽지 않는다.
            if (handles_ref.current !== handles) {
                return;
            }
            onCoordinateSpaceChange?.(create_chart_coordinate_space(handles));
        }

        /**
         * 함수 이름: schedule_coordinate_space_publish()
         * 기능: drag·zoom·resize 중 drawing 재투영을 animation frame당 한 번으로 제한한다.
         * 인자: 없음
         * 반환값: 없음
         * 작성 날짜: 2026/08/20
         */
        function schedule_coordinate_space_publish(): void {
            if (coordinate_animation_frame !== null) {
                return;
            }

            coordinate_animation_frame = requestAnimationFrame(() => {
                coordinate_animation_frame = null;
                publish_coordinate_space();
            });
        }

        /**
         * 함수 이름: handle_crosshair_move()
         * 기능: pointer가 가리키는 실제 봉의 OHLCV를 hover 정보로 반영한다.
         * 인자: event -> Lightweight Charts crosshair event
         * 반환값: 없음
         * 작성 날짜: 2026/08/20
         */
        function handle_crosshair_move(event: MouseEventParams<Time>): void {
            set_hovered_candle(read_crosshair_candle(event, handles));
            schedule_coordinate_space_publish();
        }

        /**
         * 함수 이름: handle_time_scale_size_change()
         * 기능: 일반·전체화면 크기 변경 뒤에도 적재된 전체 봉이 최대 축소 범위에 들어오게 한다.
         * 인자: 없음
         * 반환값: 없음
         * 작성 날짜: 2026/08/20
         */
        function handle_time_scale_size_change(): void {
            minimum_bar_spacing_ref.current = apply_minimum_bar_spacing(
                handles,
                loaded_candle_count_ref.current,
                minimum_bar_spacing_ref.current,
            );
            schedule_coordinate_space_publish();
        }

        handles.chart.subscribeCrosshairMove(handle_crosshair_move);
        handles.chart.timeScale().subscribeVisibleLogicalRangeChange(schedule_coordinate_space_publish);
        handles.chart.timeScale().subscribeSizeChange(handle_time_scale_size_change);
        container.addEventListener('dblclick', schedule_coordinate_space_publish);
        container.addEventListener('pointermove', schedule_coordinate_space_publish);
        container.addEventListener('wheel', schedule_coordinate_space_publish, { passive: true });
        coordinate_space_publish_ref.current = schedule_coordinate_space_publish;
        handle_time_scale_size_change();
        publish_coordinate_space();

        return () => {
            if (coordinate_animation_frame !== null) {
                cancelAnimationFrame(coordinate_animation_frame);
            }

            handles.chart.unsubscribeCrosshairMove(handle_crosshair_move);
            handles.chart.timeScale().unsubscribeVisibleLogicalRangeChange(schedule_coordinate_space_publish);
            handles.chart.timeScale().unsubscribeSizeChange(handle_time_scale_size_change);
            container.removeEventListener('dblclick', schedule_coordinate_space_publish);
            container.removeEventListener('pointermove', schedule_coordinate_space_publish);
            container.removeEventListener('wheel', schedule_coordinate_space_publish);
            coordinate_space_publish_ref.current = () => undefined;
            onCoordinateSpaceChange?.(null);
            handles.chart.remove();
            earliest_open_time_ref.current = null;
            fitted_interval_ref.current = null;
            handles_ref.current = null;
            minimum_bar_spacing_ref.current = null;
            series_render_state_ref.current = null;
        };
    }, [onCoordinateSpaceChange, presentationMode]);

    useEffect(() => {
        const handles = handles_ref.current;

        if (handles === null) {
            return;
        }

        // Fixture 자료는 각 candle 사이 두 칸을 비워 Figma의 고정 막대 간격을 재현한다.
        if (presentationMode === 'fixture') {
            handles.candle_series.setData(candles.flatMap((candle, index) => {
                const logical_index = index * 3;
                const candle_point = {
                    ...candle,
                    time: create_fixture_chart_time(logical_index),
                };

                if (index === candles.length - 1) {
                    return [candle_point];
                }

                return [
                    candle_point,
                    { time: create_fixture_chart_time(logical_index + 1) },
                    { time: create_fixture_chart_time(logical_index + 2) },
                ];
            }));
            // SVG fixture가 보조지표를 소유하므로 library 가격축에는 candle 범위만 남긴다.
            handles.ema_series.setData([]);
            handles.bollinger_upper_series.setData([]);
            handles.bollinger_lower_series.setData([]);
            handles.volume_series.setData(indicatorSettings.volume
                ? candles.map((candle, index) => ({
                    color: candle.close >= candle.open ? '#0eb77b66' : '#f0385866',
                    time: create_fixture_chart_time(index * 3),
                    value: Math.max(1, candle.high - candle.low),
                }))
                : []);
            set_is_ready(true);
            return;
        }

        minimum_bar_spacing_ref.current = apply_minimum_bar_spacing(
            handles,
            candles.length,
            minimum_bar_spacing_ref.current,
        );

        const first_candle = candles[0];
        const first_open_time = first_candle === undefined
            ? null
            : get_candle_open_time(first_candle, 0, candles.length, interval);
        const visible_range_before_update = handles.chart.timeScale().getVisibleRange();
        const history_was_prepended = fitted_interval_ref.current === interval
            && earliest_open_time_ref.current !== null
            && first_open_time !== null
            && first_open_time < earliest_open_time_ref.current;
        const previous_render_state = series_render_state_ref.current;
        const can_update_latest_only = previous_render_state !== null
            && previous_render_state.data_revision === dataRevision
            && previous_render_state.interval === interval
            && previous_render_state.first_open_time === first_open_time
            && candles.length >= previous_render_state.candle_count
            && candles.length <= previous_render_state.candle_count + 1
            && previous_render_state.ema_is_visible === indicatorSettings.ema9
            && previous_render_state.bollinger_is_visible === indicatorSettings.bollingerBand;
        const latest_candle = candles.at(-1);

        try {
            if (can_update_latest_only && latest_candle !== undefined) {
                const latest_candle_index = candles.length - 1;
                const latest_ema_point = ema.at(-1);
                const latest_bollinger_upper_point = bollingerUpper.at(-1);
                const latest_bollinger_lower_point = bollingerLower.at(-1);

                handles.candle_series.update(create_candlestick_data(
                    latest_candle,
                    latest_candle_index,
                    candles.length,
                    interval,
                ));
                handles.volume_series.update(create_volume_data(
                    latest_candle,
                    latest_candle_index,
                    candles.length,
                    interval,
                ));
                if (indicatorSettings.ema9 && latest_ema_point !== undefined) {
                    handles.ema_series.update(create_line_data(
                        latest_ema_point,
                        ema.length - 1,
                        ema.length,
                        candles,
                        interval,
                    ));
                }
                if (indicatorSettings.bollingerBand
                    && latest_bollinger_upper_point !== undefined
                    && latest_bollinger_lower_point !== undefined) {
                    handles.bollinger_upper_series.update(create_line_data(
                        latest_bollinger_upper_point,
                        bollingerUpper.length - 1,
                        bollingerUpper.length,
                        candles,
                        interval,
                    ));
                    handles.bollinger_lower_series.update(create_line_data(
                        latest_bollinger_lower_point,
                        bollingerLower.length - 1,
                        bollingerLower.length,
                        candles,
                        interval,
                    ));
                }
            } else {
                handles.candle_series.setData(candles.map((candle, index) => {
                    return create_candlestick_data(candle, index, candles.length, interval);
                }));
                handles.ema_series.setData(indicatorSettings.ema9
                    ? ema.map((point, index) => {
                        return create_line_data(point, index, ema.length, candles, interval);
                    })
                    : []);
                handles.bollinger_upper_series.setData(indicatorSettings.bollingerBand
                    ? bollingerUpper.map((point, index) => {
                        return create_line_data(
                            point,
                            index,
                            bollingerUpper.length,
                            candles,
                            interval,
                        );
                    })
                    : []);
                handles.bollinger_lower_series.setData(indicatorSettings.bollingerBand
                    ? bollingerLower.map((point, index) => {
                        return create_line_data(
                            point,
                            index,
                            bollingerLower.length,
                            candles,
                            interval,
                        );
                    })
                    : []);
                handles.volume_series.setData(candles.map((candle, index) => {
                    return create_volume_data(candle, index, candles.length, interval);
                }));
            }
            handles.volume_series.applyOptions({ visible: indicatorSettings.volume });
            if (candles.length > 0 && fitted_interval_ref.current !== interval) {
                if (candles.length <= INITIAL_VISIBLE_BAR_COUNT) {
                    handles.chart.timeScale().fitContent();
                } else {
                    handles.chart.timeScale().setVisibleLogicalRange({
                        from: candles.length - INITIAL_VISIBLE_BAR_COUNT,
                        to: candles.length + 2.35,
                    });
                }
                fitted_interval_ref.current = interval;
            } else if (history_was_prepended && visible_range_before_update !== null) {
                handles.chart.timeScale().setVisibleRange(visible_range_before_update);
            }

            earliest_open_time_ref.current = first_open_time;
            series_render_state_ref.current = {
                bollinger_is_visible: indicatorSettings.bollingerBand,
                candle_count: candles.length,
                data_revision: dataRevision,
                ema_is_visible: indicatorSettings.ema9,
                first_open_time,
                interval,
            };
            set_is_ready(true);
            coordinate_space_publish_ref.current();
            if (render_diagnostic_ref.current.interval !== interval || Date.now() >= render_diagnostic_ref.current.next_at) {
                render_diagnostic_ref.current = { interval, next_at: Date.now() + 30_000 };
                record_chart_diagnostic({ event: 'render_applied', interval, connection_id: dataRevision, intervals: [{
                    interval, received_at_ms: null, event_time_ms: null,
                    open_time_ms: latest_candle?.open_time ?? null, close: latest_candle?.close ?? null,
                    count: candles.length,
                }] });
            }
        } catch (error: unknown) {
            record_chart_diagnostic({ event: 'render_failed', interval, connection_id: dataRevision, ...describe_chart_data_error(error) });
            throw error;
        }
    }, [
        bollingerLower,
        bollingerUpper,
        candles,
        dataRevision,
        ema,
        indicatorSettings,
        interval,
        presentationMode,
    ]);

    useEffect(() => {
        set_hovered_candle(null);
    }, [interval]);

    useEffect(() => {
        const handles = handles_ref.current;
        const average_entry_price = Number(position_average_entry_price);

        // 표시할 열린 포지션의 유효 가격이 없거나 정적 fixture이면 기준선을 만들지 않는다.
        if (handles === null || presentationMode !== 'interactive'
            || position_average_entry_price === null
            || !Number.isFinite(average_entry_price) || average_entry_price <= 0) {
            return undefined;
        }

        // 기존 가격 formatter가 소수 둘째 자리까지 표시하며 선과 오른쪽 가격표는 같은 파랑을 쓴다.
        const position_color = read_color_token('--color-status-info', '#1768d4');
        const position_price_line = handles.candle_series.createPriceLine({
            price: average_entry_price,  // 숫자 변환은 chart 좌표 입력에만 사용한다.
            color: position_color,
            lineStyle: LineStyle.Dashed,
            lineWidth: 1,
            lineVisible: true,
            axisLabelVisible: true,
            axisLabelColor: position_color,
            axisLabelTextColor: read_color_token('--color-text-primary', '#f0f1f2'),
            title: '',
        });

        return () => {
            // 평단가 변경·포지션 종료 때 이전 선을 지우고 이미 제거된 차트에는 접근하지 않는다.
            if (handles_ref.current === handles) {
                handles.candle_series.removePriceLine(position_price_line);
            }
        };
    }, [onCoordinateSpaceChange, position_average_entry_price, presentationMode]);

    const change_rate = displayed_candle === null ? 0 : calculate_change_rate(displayed_candle);
    const range_rate = displayed_candle === null ? 0 : calculate_price_range_rate(displayed_candle);
    const change_class = change_rate >= 0 ? styles.positiveValue : styles.negativeValue;

    return (
        <div
            className={styles.surface}
            data-chart-presentation-mode={presentationMode}
        >
            <div
                aria-hidden="true"
                className={`${styles.chartHost} ${
                    presentationMode === 'fixture' ? styles.fixtureChartHost : ''
                }`}
                data-chart-engine="lightweight-charts"
                data-chart-engine-ready={is_ready}
                ref={container_ref}
            />
            {displayed_candle !== null && presentationMode === 'interactive' ? (
                <div aria-label="선택한 봉 정보" className={styles.candleInformation}>
                    <time dateTime={new Date(displayed_candle.open_time).toISOString()}>
                        {format_hover_time(displayed_candle.open_time)}
                    </time>
                    <span>시가 <strong>{format_chart_price(displayed_candle.open)}</strong></span>
                    <span>고가 <strong>{format_chart_price(displayed_candle.high)}</strong></span>
                    <span>저가 <strong>{format_chart_price(displayed_candle.low)}</strong></span>
                    <span>종가 <strong>{format_chart_price(displayed_candle.close)}</strong></span>
                    <span>등락 <strong className={change_class}>{change_rate.toFixed(2)}%</strong></span>
                    <span>변동폭 <strong>{range_rate.toFixed(2)}%</strong></span>
                    <span>
                        거래량({read_base_asset(symbol)})
                        {' '}
                        <strong>{format_chart_volume(displayed_candle.volume)}</strong>
                    </span>
                </div>
            ) : null}
        </div>
    );
}
