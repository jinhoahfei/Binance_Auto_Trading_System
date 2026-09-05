import { act, render, screen } from '@testing-library/react';
import {
    CrosshairMode,
    LineStyle,
    createChart,
    type IChartApi,
    type MouseEventParams,
    type Time,
    type UTCTimestamp,
} from 'lightweight-charts';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { LightweightChartSurface } from './LightweightChartSurface';

vi.mock('lightweight-charts', () => ({
    CandlestickSeries: Symbol('CandlestickSeries'),
    ColorType: { Solid: 'Solid' },
    CrosshairMode: { Normal: 'Normal' },
    HistogramSeries: Symbol('HistogramSeries'),
    LineSeries: Symbol('LineSeries'),
    LineStyle: { Dashed: 'Dashed', Solid: 'Solid' },
    TickMarkType: {
        Year: 'Year',
        Month: 'Month',
        DayOfMonth: 'DayOfMonth',
        Time: 'Time',
        TimeWithSeconds: 'TimeWithSeconds',
    },
    createChart: vi.fn(),
}));

const INDICATOR_SETTINGS = {
    bollingerBand: true,
    ema9: true,
    volume: true,
};
const INITIAL_OPEN_TIME = Date.UTC(2026, 7, 20, 9, 0, 0);

/**
 * 함수 이름: create_candles()
 * 기능: 초기 표시 범위와 과거 prepend를 검증할 연속 1분봉을 만든다.
 * 인자: count -> 생성할 봉 수, first_open_time -> 첫 봉 시작 시각
 * 반환값: 시간순 candle ViewModel 목록
 * 작성 날짜: 2026/08/20
 */
function create_candles(count: number, first_open_time: number) {
    return Array.from({ length: count }, (_, index) => ({
        close: 101 + index,
        high: 102 + index,
        low: 99 + index,
        open: 100 + index,
        open_time: first_open_time + (index * 60_000),
        volume: 20 + index,
    }));
}

/**
 * 함수 이름: create_series_mock()
 * 기능: Lightweight Charts series가 사용하는 데이터·좌표 API mock을 만든다.
 * 인자: 없음
 * 반환값: component 검증용 series mock
 * 작성 날짜: 2026/08/20
 */
function create_series_mock() {
    return {
        applyOptions: vi.fn(),
        barsInLogicalRange: vi.fn(() => ({ barsBefore: 200, barsAfter: 0 })),
        coordinateToPrice: vi.fn(() => 100),
        priceToCoordinate: vi.fn(() => 120),
        setData: vi.fn(),
        update: vi.fn(),
    };
}

/**
 * 함수 이름: create_chart_harness()
 * 기능: 차트 생성, crosshair 발행, time scale 구독을 관찰할 test harness를 만든다.
 * 인자: 없음
 * 반환값: chart와 series 및 event 발행 helper
 * 작성 날짜: 2026/08/20
 */
function create_chart_harness() {
    const candle_series = create_series_mock();
    const bollinger_upper_series = create_series_mock();
    const bollinger_lower_series = create_series_mock();
    const ema_series = create_series_mock();
    const volume_series = create_series_mock();
    const volume_price_scale = { applyOptions: vi.fn() };
    let size_change_handler: (() => void) | null = null;
    let time_scale_width = 800;
    const time_scale = {
        applyOptions: vi.fn(),
        coordinateToTime: vi.fn(() => (INITIAL_OPEN_TIME / 1_000) as UTCTimestamp),
        fitContent: vi.fn(),
        getVisibleLogicalRange: vi.fn(() => ({ from: 0, to: 120 })),
        getVisibleRange: vi.fn(() => ({
            from: (INITIAL_OPEN_TIME / 1_000) as UTCTimestamp,
            to: ((INITIAL_OPEN_TIME + 60_000) / 1_000) as UTCTimestamp,
        })),
        subscribeSizeChange: vi.fn((handler: () => void) => {
            size_change_handler = handler;
        }),
        subscribeVisibleLogicalRangeChange: vi.fn(),
        setVisibleLogicalRange: vi.fn(),
        setVisibleRange: vi.fn(),
        timeToCoordinate: vi.fn(() => 240),
        unsubscribeSizeChange: vi.fn(),
        unsubscribeVisibleLogicalRangeChange: vi.fn(),
        width: vi.fn(() => time_scale_width),
    };
    let crosshair_move_handler: ((event: MouseEventParams<Time>) => void) | null = null;
    const chart = {
        addSeries: vi.fn()
            .mockReturnValueOnce(candle_series)
            .mockReturnValueOnce(bollinger_upper_series)
            .mockReturnValueOnce(bollinger_lower_series)
            .mockReturnValueOnce(ema_series)
            .mockReturnValueOnce(volume_series),
        paneSize: vi.fn(() => ({ height: 320, width: 800 })),
        priceScale: vi.fn(() => volume_price_scale),
        remove: vi.fn(),
        subscribeCrosshairMove: vi.fn((handler: (event: MouseEventParams<Time>) => void) => {
            crosshair_move_handler = handler;
        }),
        timeScale: vi.fn(() => time_scale),
        unsubscribeCrosshairMove: vi.fn(),
    };

    /**
     * 함수 이름: emit_crosshair_move()
     * 기능: component가 등록한 crosshair callback에 봉 event를 전달한다.
     * 인자: event -> 발행할 Lightweight Charts mouse event
     * 반환값: 없음
     * 작성 날짜: 2026/08/20
     */
    function emit_crosshair_move(event: MouseEventParams<Time>): void {
        if (crosshair_move_handler === null) {
            throw new Error('crosshair move handler가 등록되지 않았습니다.');
        }

        crosshair_move_handler(event);
    }

    /**
     * 함수 이름: emit_time_scale_size_change()
     * 기능: 시간축 폭을 변경하고 component가 등록한 resize callback을 실행한다.
     * 인자: next_width -> 변경된 시간축 폭
     * 반환값: 없음
     * 작성 날짜: 2026/08/20
     */
    function emit_time_scale_size_change(next_width: number): void {
        if (size_change_handler === null) {
            throw new Error('time scale size change handler가 등록되지 않았습니다.');
        }

        time_scale_width = next_width;
        size_change_handler();
    }

    return {
        bollinger_lower_series,
        bollinger_upper_series,
        candle_series,
        chart,
        ema_series,
        emit_crosshair_move,
        emit_time_scale_size_change,
        time_scale,
        volume_series,
    };
}

let chart_harness: ReturnType<typeof create_chart_harness>;

beforeEach(() => {
    chart_harness = create_chart_harness();
    vi.mocked(createChart).mockReset();
    vi.mocked(createChart).mockReturnValue(chart_harness.chart as unknown as IChartApi);
    vi.stubGlobal('ResizeObserver', vi.fn());
    vi.stubGlobal('requestAnimationFrame', vi.fn(() => 17));
    vi.stubGlobal('cancelAnimationFrame', vi.fn());
});

afterEach(() => {
    vi.unstubAllGlobals();
});

describe('LightweightChartSurface', () => {
    it('차트 가격은 2자리, ETH 거래량은 4자리로 반올림하고 원본 봉 값은 유지한다', () => {
        const candle = {
            close: 2451.425,
            high: 2452.5678,
            low: 2449.1234,
            open: 2450.9876,
            open_time: INITIAL_OPEN_TIME,
            volume: 330.86914999,
        };
        render(
            <LightweightChartSurface
                bollingerLower={[]}
                bollingerUpper={[]}
                candles={[candle]}
                ema={[]}
                indicatorSettings={INDICATOR_SETTINGS}
                interval="1m"
                symbol="ETHUSDT"
            />,
        );
        const information = screen.getByLabelText('선택한 봉 정보');
        const price_formatter = vi.mocked(createChart).mock.calls[0]?.[1]
            ?.localization?.priceFormatter as ((price: number) => string) | undefined;

        expect(information).toHaveTextContent('시가 2,450.99');
        expect(information).toHaveTextContent('고가 2,452.57');
        expect(information).toHaveTextContent('저가 2,449.12');
        expect(information).toHaveTextContent('종가 2,451.43');
        expect(information).toHaveTextContent('거래량(ETH) 330.8691');
        expect(price_formatter?.(0.125)).toBe('0.13');
        expect(chart_harness.candle_series.setData).toHaveBeenCalledWith([
            expect.objectContaining({ close: 2451.425, open: 2450.9876 }),
        ]);
        expect(chart_harness.volume_series.setData).toHaveBeenCalledWith([
            expect.objectContaining({ value: 330.86914999 }),
        ]);
    });

    it('Binance형 zoom, pan, 동적 축과 crosshair option을 활성화한다', () => {
        const { unmount } = render(
            <LightweightChartSurface
                bollingerLower={[]}
                bollingerUpper={[]}
                candles={[{
                    close: 110,
                    high: 120,
                    low: 90,
                    open: 100,
                    open_time: INITIAL_OPEN_TIME,
                    volume: 20,
                }]}
                ema={[]}
                indicatorSettings={INDICATOR_SETTINGS}
                interval="1m"
            />,
        );
        const chart_options = vi.mocked(createChart).mock.calls[0]?.[1];

        expect(chart_options).toMatchObject({
            crosshair: {
                mode: CrosshairMode.Normal,
                horzLine: {
                    labelVisible: true,
                    style: LineStyle.Dashed,
                    visible: true,
                },
                vertLine: {
                    labelVisible: true,
                    style: LineStyle.Dashed,
                    visible: true,
                },
            },
            handleScale: {
                axisDoubleClickReset: { price: true, time: true },
                axisPressedMouseMove: { price: true, time: true },
                mouseWheel: true,
                pinch: true,
            },
            handleScroll: {
                horzTouchDrag: true,
                mouseWheel: true,
                pressedMouseMove: true,
                vertTouchDrag: true,
            },
            kineticScroll: { mouse: true, touch: true },
            rightPriceScale: {
                autoScale: true,
                visible: true,
            },
            timeScale: {
                visible: true,
            },
        });
        expect(chart_harness.chart.subscribeCrosshairMove).toHaveBeenCalledOnce();
        expect(chart_harness.time_scale.subscribeVisibleLogicalRangeChange).toHaveBeenCalledOnce();
        expect(chart_harness.time_scale.subscribeSizeChange).toHaveBeenCalledOnce();

        unmount();
    });

    it('적재 봉 수에 맞춰 최대 x축 축소 간격을 다시 계산하고 conflation을 적용한다', () => {
        const loaded_candles = create_candles(4_000, INITIAL_OPEN_TIME);
        const { rerender } = render(
            <LightweightChartSurface
                bollingerLower={[]}
                bollingerUpper={[]}
                candles={loaded_candles}
                ema={[]}
                indicatorSettings={INDICATOR_SETTINGS}
                interval="1m"
            />,
        );

        const chart_options = vi.mocked(createChart).mock.calls[0]?.[1];
        const safe_minimum_bar_spacing = chart_options?.timeScale?.minBarSpacing;
        const initial_scale_options = chart_harness.time_scale.applyOptions.mock.calls.at(-1)?.[0];
        const initial_minimum_bar_spacing = initial_scale_options?.minBarSpacing;

        expect(chart_options).toMatchObject({
            timeScale: {
                conflationThresholdFactor: 1,
                enableConflation: true,
            },
        });
        expect(safe_minimum_bar_spacing).toBeTypeOf('number');
        expect(safe_minimum_bar_spacing ?? 0).toBeGreaterThan(0);
        expect(safe_minimum_bar_spacing ?? Number.POSITIVE_INFINITY).toBeLessThan(0.5);
        expect(initial_minimum_bar_spacing).toBeTypeOf('number');
        expect((initial_minimum_bar_spacing ?? Number.POSITIVE_INFINITY)
            * loaded_candles.length
            * 2)
            .toBeLessThanOrEqual(chart_harness.time_scale.width());

        const prepended_candles = [
            ...create_candles(4_000, INITIAL_OPEN_TIME - (4_000 * 60_000)),
            ...loaded_candles,
        ];

        rerender(
            <LightweightChartSurface
                bollingerLower={[]}
                bollingerUpper={[]}
                candles={prepended_candles}
                ema={[]}
                indicatorSettings={INDICATOR_SETTINGS}
                interval="1m"
            />,
        );

        const prepended_scale_options = chart_harness.time_scale.applyOptions.mock.calls.at(-1)?.[0];
        const prepended_minimum_bar_spacing = prepended_scale_options?.minBarSpacing;

        expect(prepended_minimum_bar_spacing).toBeTypeOf('number');
        expect((prepended_minimum_bar_spacing ?? Number.POSITIVE_INFINITY)
            * prepended_candles.length
            * 2)
            .toBeLessThanOrEqual(chart_harness.time_scale.width());
        expect(prepended_minimum_bar_spacing ?? Number.POSITIVE_INFINITY)
            .toBeLessThan(initial_minimum_bar_spacing ?? 0);

        act(() => {
            chart_harness.emit_time_scale_size_change(400);
        });

        const resized_scale_options = chart_harness.time_scale.applyOptions.mock.calls.at(-1)?.[0];
        const resized_minimum_bar_spacing = resized_scale_options?.minBarSpacing;

        expect(resized_minimum_bar_spacing).toBeTypeOf('number');
        expect((resized_minimum_bar_spacing ?? Number.POSITIVE_INFINITY)
            * prepended_candles.length
            * 2)
            .toBeLessThanOrEqual(chart_harness.time_scale.width());
        expect(resized_minimum_bar_spacing ?? Number.POSITIVE_INFINITY)
            .toBeLessThan(prepended_minimum_bar_spacing ?? 0);
    });

    it('crosshair OHLCV를 표시하고 같은 interval의 live update에서는 fitContent를 반복하지 않는다', () => {
        const handle_coordinate_space_change = vi.fn();
        const { rerender, unmount } = render(
            <LightweightChartSurface
                bollingerLower={[]}
                bollingerUpper={[]}
                candles={[{
                    close: 110,
                    high: 120,
                    low: 90,
                    open: 100,
                    open_time: INITIAL_OPEN_TIME,
                    volume: 20,
                }]}
                ema={[]}
                indicatorSettings={INDICATOR_SETTINGS}
                interval="1m"
                onCoordinateSpaceChange={handle_coordinate_space_change}
                symbol="BTCUSDT"
            />,
        );

        rerender(
            <LightweightChartSurface
                bollingerLower={[]}
                bollingerUpper={[]}
                candles={[{
                    close: 215,
                    high: 220,
                    low: 195,
                    open: 200,
                    open_time: INITIAL_OPEN_TIME,
                    volume: 30,
                }]}
                ema={[]}
                indicatorSettings={INDICATOR_SETTINGS}
                interval="1m"
                onCoordinateSpaceChange={handle_coordinate_space_change}
                symbol="BTCUSDT"
            />,
        );

        expect(chart_harness.time_scale.fitContent).toHaveBeenCalledOnce();
        expect(chart_harness.candle_series.setData).toHaveBeenCalledOnce();
        expect(chart_harness.candle_series.update).toHaveBeenCalledOnce();
        expect(chart_harness.volume_series.setData).toHaveBeenCalledOnce();
        expect(chart_harness.volume_series.update).toHaveBeenCalledOnce();

        act(() => {
            chart_harness.emit_crosshair_move({
                seriesData: new Map([
                    [chart_harness.candle_series, {
                        close: 110,
                        high: 120,
                        low: 90,
                        open: 100,
                        time: (INITIAL_OPEN_TIME / 1_000) as UTCTimestamp,
                    }],
                    [chart_harness.volume_series, {
                        time: (INITIAL_OPEN_TIME / 1_000) as UTCTimestamp,
                        value: 4_567.891234,
                    }],
                ]),
                time: (INITIAL_OPEN_TIME / 1_000) as UTCTimestamp,
            } as unknown as MouseEventParams<Time>);
        });

        const candle_information = screen.getByLabelText('선택한 봉 정보');

        expect(candle_information).toHaveTextContent('2026.08.20 18:00 KST');
        expect(candle_information).toHaveTextContent('시가 100.00');
        expect(candle_information).toHaveTextContent('고가 120.00');
        expect(candle_information).toHaveTextContent('저가 90.00');
        expect(candle_information).toHaveTextContent('종가 110.00');
        expect(candle_information).toHaveTextContent('등락 10.00%');
        expect(candle_information).toHaveTextContent('변동폭 30.00%');
        expect(candle_information).toHaveTextContent('거래량(BTC) 4,567.8912');

        act(() => {
            chart_harness.emit_crosshair_move({
                seriesData: new Map(),
            } as unknown as MouseEventParams<Time>);
        });
        expect(candle_information).toHaveTextContent('시가 200.00');

        const crosshair_handler = chart_harness.chart.subscribeCrosshairMove.mock.calls[0]?.[0];
        const visible_range_handler = chart_harness.time_scale
            .subscribeVisibleLogicalRangeChange.mock.calls[0]?.[0];
        const size_handler = chart_harness.time_scale.subscribeSizeChange.mock.calls[0]?.[0];

        unmount();

        expect(chart_harness.chart.unsubscribeCrosshairMove).toHaveBeenCalledWith(crosshair_handler);
        expect(chart_harness.time_scale.unsubscribeVisibleLogicalRangeChange)
            .toHaveBeenCalledWith(visible_range_handler);
        expect(chart_harness.time_scale.unsubscribeSizeChange).toHaveBeenCalledWith(size_handler);
        expect(chart_harness.chart.remove).toHaveBeenCalledOnce();
        expect(handle_coordinate_space_change).toHaveBeenLastCalledWith(null);
    });

    it('초기 1000개는 최신 180개를 표시하고 과거 prepend 뒤에는 기존 시간 범위를 보존한다', () => {
        const handle_coordinate_space_change = vi.fn();
        const initial_candles = create_candles(1_000, INITIAL_OPEN_TIME);
        const { rerender } = render(
            <LightweightChartSurface
                bollingerLower={[]}
                bollingerUpper={[]}
                candles={initial_candles}
                ema={[]}
                indicatorSettings={INDICATOR_SETTINGS}
                interval="1m"
                onCoordinateSpaceChange={handle_coordinate_space_change}
            />,
        );

        expect(chart_harness.time_scale.fitContent).not.toHaveBeenCalled();
        expect(chart_harness.time_scale.setVisibleLogicalRange).toHaveBeenCalledWith({
            from: 820,
            to: 1_002.35,
        });
        expect(handle_coordinate_space_change).toHaveBeenCalledWith(expect.objectContaining({
            bars_before: 200,
        }));

        rerender(
            <LightweightChartSurface
                bollingerLower={[]}
                bollingerUpper={[]}
                candles={[
                    ...create_candles(1, INITIAL_OPEN_TIME - 60_000),
                    ...initial_candles,
                ]}
                ema={[]}
                indicatorSettings={INDICATOR_SETTINGS}
                interval="1m"
                onCoordinateSpaceChange={handle_coordinate_space_change}
            />,
        );

        expect(chart_harness.time_scale.setVisibleRange).toHaveBeenCalledWith({
            from: (INITIAL_OPEN_TIME / 1_000) as UTCTimestamp,
            to: ((INITIAL_OPEN_TIME + 60_000) / 1_000) as UTCTimestamp,
        });
        expect(chart_harness.candle_series.setData).toHaveBeenCalledTimes(2);
        expect(chart_harness.candle_series.update).not.toHaveBeenCalled();
        expect(chart_harness.time_scale.setVisibleLogicalRange).toHaveBeenCalledTimes(1);
    });
});
