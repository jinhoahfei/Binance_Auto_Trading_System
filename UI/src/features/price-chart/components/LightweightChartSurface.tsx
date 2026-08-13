import {
    CandlestickSeries,
    ColorType,
    HistogramSeries,
    LineSeries,
    LineStyle,
    createChart,
    type IChartApi,
    type ISeriesApi,
    type UTCTimestamp,
} from 'lightweight-charts';
import { useEffect, useRef, useState } from 'react';

import type {
    CandleViewModel,
    IndicatorSettingsViewModel,
    LinePointViewModel,
} from '../types';

import styles from './LightweightChartSurface.module.css';

const BASE_CHART_TIME = Date.UTC(2026, 5, 22, 9, 0, 0) / 1_000;
const CHART_TIME_STEP_SECONDS = 5 * 60;

interface LightweightChartHandles {
    readonly chart: IChartApi;
    readonly bollinger_lower_series: ISeriesApi<'Line'>;
    readonly bollinger_upper_series: ISeriesApi<'Line'>;
    readonly candle_series: ISeriesApi<'Candlestick'>;
    readonly ema_series: ISeriesApi<'Line'>;
    readonly volume_series: ISeriesApi<'Histogram'>;
}

export interface LightweightChartSurfaceProps {
    readonly bollingerLower: ReadonlyArray<LinePointViewModel>;
    readonly bollingerUpper: ReadonlyArray<LinePointViewModel>;
    readonly candles: ReadonlyArray<CandleViewModel>;
    readonly ema: ReadonlyArray<LinePointViewModel>;
    readonly indicatorSettings: IndicatorSettingsViewModel;
}

/**
 * 함수 이름: create_chart_time()
 * 기능: 순서 기반 차트 데이터를 Lightweight Charts가 요구하는 결정적 UNIX 시각으로 변환한다.
 * 인자: index -> 캔들 또는 보조지표 배열의 순번
 * 반환값: 5분 간격의 Lightweight Charts UNIX timestamp
 * 작성 날짜: 2026/08/12
 */
function create_chart_time(index: number): UTCTimestamp {
    return (BASE_CHART_TIME + index * CHART_TIME_STEP_SECONDS) as UTCTimestamp;
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
 * 함수 이름: create_lightweight_chart()
 * 기능: Figma 차트 표면에 맞춘 Lightweight Charts 인스턴스와 캔들·지표 series를 생성한다.
 * 인자: container -> chart canvas를 소유할 HTML 요소
 * 반환값: 갱신과 정리에 필요한 chart 및 series handle
 * 작성 날짜: 2026/08/12
 */
function create_lightweight_chart(container: HTMLDivElement): LightweightChartHandles {
    const positive_color = read_color_token('--color-status-positive', '#0eb77b');
    const negative_color = read_color_token('--color-status-negative', '#f03858');
    const information_color = read_color_token('--color-status-info', '#1768d4');
    const bollinger_color = read_color_token('--color-text-secondary', '#a7afbb');
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
            rightOffset: 2.35,
        },
    });
    const candle_series = chart.addSeries(CandlestickSeries, {
        borderVisible: false,
        downColor: negative_color,
        lastValueVisible: false,
        priceLineVisible: false,
        upColor: positive_color,
        wickDownColor: negative_color,
        wickUpColor: positive_color,
    });
    const bollinger_upper_series = chart.addSeries(LineSeries, {
        color: bollinger_color,
        crosshairMarkerVisible: false,
        lastValueVisible: false,
        lineStyle: LineStyle.Dashed,
        lineWidth: 1,
        priceLineVisible: false,
    });
    const bollinger_lower_series = chart.addSeries(LineSeries, {
        color: bollinger_color,
        crosshairMarkerVisible: false,
        lastValueVisible: false,
        lineStyle: LineStyle.Dashed,
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
 * 함수 이름: LightweightChartSurface()
 * 기능: 캔들·EMA·볼린저밴드·거래량을 Lightweight Charts canvas에 갱신해 표시한다.
 * 인자: props -> 시장 데이터와 지표 표시 설정
 * 반환값: SVG drawing overlay 아래에 배치되는 금융 차트 surface
 * 작성 날짜: 2026/08/12
 */
export function LightweightChartSurface({
    bollingerLower,
    bollingerUpper,
    candles,
    ema,
    indicatorSettings,
}: LightweightChartSurfaceProps) {
    const container_ref = useRef<HTMLDivElement | null>(null);
    const handles_ref = useRef<LightweightChartHandles | null>(null);
    const [is_ready, set_is_ready] = useState(false);

    useEffect(() => {
        const container = container_ref.current;

        if (container === null || typeof ResizeObserver === 'undefined') {
            return undefined;
        }

        const handles = create_lightweight_chart(container);

        handles_ref.current = handles;

        return () => {
            handles.chart.remove();
            handles_ref.current = null;
        };
    }, []);

    useEffect(() => {
        const handles = handles_ref.current;

        if (handles === null) {
            return;
        }

        handles.candle_series.setData(candles.flatMap((candle, index) => {
            const logical_index = index * 3;
            const candle_point = {
                ...candle,
                time: create_chart_time(logical_index),
            };

            if (index === candles.length - 1) {
                return [candle_point];
            }

            return [
                candle_point,
                { time: create_chart_time(logical_index + 1) },
                { time: create_chart_time(logical_index + 2) },
            ];
        }));
        handles.ema_series.setData(indicatorSettings.ema9
            ? ema.map((point, index) => ({ time: create_chart_time(index * 3), value: point.value }))
            : []);
        handles.bollinger_upper_series.setData(indicatorSettings.bollingerBand
            ? bollingerUpper.map((point, index) => ({ time: create_chart_time(index * 3), value: point.value }))
            : []);
        handles.bollinger_lower_series.setData(indicatorSettings.bollingerBand
            ? bollingerLower.map((point, index) => ({ time: create_chart_time(index * 3), value: point.value }))
            : []);
        handles.volume_series.setData(indicatorSettings.volume
            ? candles.map((candle, index) => ({
                color: candle.close >= candle.open ? '#0eb77b66' : '#f0385866',
                time: create_chart_time(index * 3),
                value: Math.max(1, candle.high - candle.low),
            }))
            : []);
        set_is_ready(true);
    }, [bollingerLower, bollingerUpper, candles, ema, indicatorSettings]);

    return (
        <div
            aria-hidden="true"
            className={styles.surface}
            data-chart-engine="lightweight-charts"
            data-chart-engine-ready={is_ready}
            ref={container_ref}
        />
    );
}
