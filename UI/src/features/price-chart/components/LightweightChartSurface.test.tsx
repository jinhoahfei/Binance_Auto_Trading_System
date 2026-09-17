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

import { UiApplicationFacade } from '../../../app/control';
import { present_dashboard_props } from '../../../app/presenters/dashboardPresenter';
import {
    map_backend_event_to_intents,
    map_backend_snapshot,
    validate_backend_snapshot,
} from '../../../shared/api/backendEventMapper';
import {
    create_backend_event_fixture,
    create_backend_snapshot_fixture,
} from '../../../shared/api/backendTestFixtures';
import { FakeUiCommandAdapter } from '../../../shared/testing';
import type { RealtimeChartDataSnapshot } from '..';
import { LightweightChartSurface } from './LightweightChartSurface';
import { PriceChartPanel } from './PriceChartPanel';

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
        createPriceLine: vi.fn(() => ({ applyOptions: vi.fn() })),
        priceToCoordinate: vi.fn(() => 120),
        removePriceLine: vi.fn(),
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
    // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
    chart_harness = create_chart_harness();
    vi.mocked(createChart).mockReset();
    vi.mocked(createChart).mockReturnValue(chart_harness.chart as unknown as IChartApi);
    vi.stubGlobal('ResizeObserver', vi.fn());
    vi.stubGlobal('requestAnimationFrame', vi.fn(() => 17));
    vi.stubGlobal('cancelAnimationFrame', vi.fn());
});

afterEach(() => {
    // 테스트가 바꾼 전역 환경과 실행 자원을 정리한다.
    vi.unstubAllGlobals();
});

describe('LightweightChartSurface', () => {
    it('재연결로 보정된 과거 봉도 다시 반영하며 기존 확대 범위를 유지한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const candles = create_candles(3, INITIAL_OPEN_TIME);
        const props = { bollingerLower: [], bollingerUpper: [], ema: [],
            indicatorSettings: INDICATOR_SETTINGS, interval: '1m' as const };
        const { rerender } = render(<LightweightChartSurface {...props} candles={candles} dataRevision={1} />);
        const corrected = candles.map((candle, index) => index === 0 ? { ...candle, high: 150, close: 140 } : candle);
        rerender(<LightweightChartSurface {...props} candles={corrected} dataRevision={3} />);

        // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
        expect(chart_harness.candle_series.setData).toHaveBeenCalledTimes(2);
        expect(chart_harness.candle_series.setData.mock.calls.at(-1)?.[0][0].close).toBe(140);
        expect(chart_harness.candle_series.update).not.toHaveBeenCalled();
        expect(chart_harness.time_scale.fitContent).toHaveBeenCalledOnce();
    });

    it('backend 평단가를 초기화·갱신·재연결하고 포지션 종료 시 파란 선과 가격표를 제거한다', () => {
        // Backend 원본 문자열을 실제 mapper·actor·presenter·panel 경계를 거쳐 차트에 전달한다.
        const backend_snapshot = create_backend_snapshot_fixture();
        const open_snapshot = {
            ...backend_snapshot,
            trading: {
                ...backend_snapshot.trading,
                has_open_position: true,
                position_average_entry_price: '2451.42500000',
            },
        };
        const mapped_snapshot = map_backend_snapshot(
            validate_backend_snapshot(open_snapshot),
            '2026-09-05',
        );
        const facade = new UiApplicationFacade(new FakeUiCommandAdapter(), mapped_snapshot.facade_options);
        const market_snapshot: RealtimeChartDataSnapshot = {
            data_status: 'live',
            klines_by_interval: { '1m': [], '30m': [], '4h': [], '1d': [] },
            status_message: null,
            symbol: 'ETHUSDT',
            updated_at: INITIAL_OPEN_TIME,
        };

        // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
        facade.start();

        /**
         * 함수 이름: render_position_chart()
         * 기능: 최신 actor 상태와 고정 봉을 실제 panel에 주입해 평단가 전달 경로를 검증한다.
         * 인자: 없음
         * 반환값: 현재 포지션을 표시할 차트 panel
         * 작성 날짜: 2026/09/05
         */
        function render_position_chart() {
            const chart_props = present_dashboard_props(facade.get_view_model(), facade, market_snapshot).chart;

            return <PriceChartPanel {...chart_props} candles={create_candles(3, INITIAL_OPEN_TIME)} />;
        }

        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const { rerender, unmount } = render(render_position_chart());
        const initial_price_line = chart_harness.candle_series.createPriceLine.mock.results[0]?.value;
        const price_formatter = vi.mocked(createChart).mock.calls[0]?.[1]
            ?.localization?.priceFormatter as (price: number) => string;

        // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
        expect(chart_harness.candle_series.createPriceLine).toHaveBeenCalledWith({
            price: 2451.425,
            color: '#1768d4',
            lineStyle: LineStyle.Dashed,
            lineWidth: 1,
            lineVisible: true,
            axisLabelVisible: true,
            axisLabelColor: '#1768d4',
            axisLabelTextColor: '#f0f1f2',
            title: '',
        });
        expect(price_formatter(2451.425)).toBe('2,451.43');
        expect(facade.get_view_model().trading.position_average_entry_price).toBe('2451.42500000');

        // 추가 매수 등으로 바뀐 평단가는 기존 선을 대체하며 주기·전체화면 전환에도 하나만 유지한다.
        const updated_trading = { ...open_snapshot.trading, position_average_entry_price: '2460.00000000' };
        map_backend_event_to_intents({
            ...create_backend_event_fixture(1, 'TRADING_SESSION_UPDATED', { trading: updated_trading }),
            aggregate_version: updated_trading.version,
        }).forEach((intent) => facade.dispatch(intent));
        rerender(render_position_chart());
        expect(chart_harness.candle_series.removePriceLine).toHaveBeenCalledWith(initial_price_line);
        expect(chart_harness.candle_series.createPriceLine).toHaveBeenLastCalledWith(
            expect.objectContaining({ price: 2460 }),
        );

        // CHART_INTERVAL_SELECTED → CHART_FULLSCREEN_CHANGED 입력을 전달해 해당 전이를 실행한다.
        facade.dispatch({ type: 'CHART_INTERVAL_SELECTED', interval: '4h' });
        facade.dispatch({ type: 'CHART_FULLSCREEN_CHANGED', is_fullscreen: true });
        rerender(render_position_chart());

        // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
        expect(chart_harness.candle_series.createPriceLine).toHaveBeenCalledTimes(2);
        expect(createChart).toHaveBeenCalledOnce();  // 표시 값 갱신 때문에 chart 인스턴스를 초기화하지 않는다.

        // 재연결의 full snapshot에서 닫힌 포지션을 적용하면 선과 가격표를 함께 제거한다.
        facade.dispatch({
            type: 'BACKEND_SNAPSHOT_SYNCHRONIZED',
            snapshot: map_backend_snapshot(backend_snapshot, '2026-09-05').server_snapshot,
        });
        rerender(render_position_chart());

        // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
        expect(chart_harness.candle_series.removePriceLine).toHaveBeenCalledTimes(2);
        expect(chart_harness.candle_series.createPriceLine).toHaveBeenCalledTimes(2);

        // 열린 포지션을 다시 동기화해도 이전 가격이 아닌 snapshot의 평단가 하나만 복구한다.
        facade.dispatch({ type: 'BACKEND_SNAPSHOT_SYNCHRONIZED', snapshot: mapped_snapshot.server_snapshot });
        rerender(render_position_chart());

        // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
        expect(chart_harness.candle_series.createPriceLine).toHaveBeenCalledTimes(3);
        expect(chart_harness.candle_series.createPriceLine).toHaveBeenLastCalledWith(
            expect.objectContaining({ price: 2451.425 }),
        );
        unmount();

        // 화면 또는 실행 수명의 종료를 요청한다.
        facade.stop();
        expect(chart_harness.chart.remove).toHaveBeenCalledOnce();  // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
    });

    it('초기 좌표 발행 뒤 unmount하면 예약한 frame이 제거된 차트를 다시 조회하지 않는다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const pending_frames = new Map<number, FrameRequestCallback>();
        let next_frame_id = 0;
        vi.stubGlobal('requestAnimationFrame', vi.fn((callback: FrameRequestCallback) => {
            pending_frames.set(++next_frame_id, callback);

            return next_frame_id;
        }));
        vi.stubGlobal('cancelAnimationFrame', vi.fn((frame_id: number) => {
            pending_frames.delete(frame_id);
        }));
        chart_harness.chart.remove.mockImplementation(() => {
            chart_harness.chart.paneSize.mockImplementation(() => {
                throw new Error('removed chart has no pane');
            });
        });
        const { unmount } = render(
            <LightweightChartSurface
                bollingerLower={[]}
                bollingerUpper={[]}
                candles={create_candles(3, INITIAL_OPEN_TIME)}
                ema={[]}
                indicatorSettings={INDICATOR_SETTINGS}
                interval="1m"
                onCoordinateSpaceChange={vi.fn()}
                symbol="ETHUSDT"
            />,
        );
        const queued_callbacks = [...pending_frames.values()];

        // 잘못된 입력이나 실행 실패가 정해진 오류로 전달되는지 검증한다.
        expect(queued_callbacks.length).toBeGreaterThan(0);

        unmount();

        expect(pending_frames.size).toBe(0);

        // 이미 실행 queue에 넘어간 callback도 이전 StrictMode chart에 접근하지 않는다.
        expect(() => queued_callbacks.forEach((callback) => callback(0))).not.toThrow();
    });

    it('차트 가격은 2자리, ETH 거래량은 4자리로 반올림하고 원본 봉 값은 유지한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const candle = {
            close: 2451.425,
            high: 2452.5678,
            low: 2449.1234,
            open: 2450.9876,
            open_time: INITIAL_OPEN_TIME,
            volume: 330.86914999,
        };

        // 준비한 의존성을 주입해 화면 또는 hook을 실행한다.
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

        // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
        expect(information).toHaveTextContent('시가 2,450.99');
        expect(information).toHaveTextContent('고가 2,452.57');
        expect(information).toHaveTextContent('저가 2,449.12');
        expect(information).toHaveTextContent('종가 2,451.43');
        expect(information).toHaveTextContent('거래량(ETH) 330.8691');
        expect(price_formatter?.(0.125)).toBe('0.13');

        // 현재가의 오른쪽 가격표는 유지하고 기본 점선과 포지션 없는 평단가 선은 숨긴다.
        expect(chart_harness.chart.addSeries.mock.calls[0]?.[1]).toMatchObject({
            lastValueVisible: true,
            priceLineVisible: false,
        });
        expect(chart_harness.candle_series.createPriceLine).not.toHaveBeenCalled();
        expect(chart_harness.candle_series.setData).toHaveBeenCalledWith([
            expect.objectContaining({ close: 2451.425, open: 2450.9876 }),
        ]);
        expect(chart_harness.volume_series.setData).toHaveBeenCalledWith([
            expect.objectContaining({ value: 330.86914999 }),
        ]);
    });

    it('Binance형 zoom, pan, 동적 축과 crosshair option을 활성화한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
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

        // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
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
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
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

        // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
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

        // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
        act(() => {
            chart_harness.emit_time_scale_size_change(400);
        });

        const resized_scale_options = chart_harness.time_scale.applyOptions.mock.calls.at(-1)?.[0];
        const resized_minimum_bar_spacing = resized_scale_options?.minBarSpacing;

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(resized_minimum_bar_spacing).toBeTypeOf('number');
        expect((resized_minimum_bar_spacing ?? Number.POSITIVE_INFINITY)
            * prepended_candles.length
            * 2)
            .toBeLessThanOrEqual(chart_harness.time_scale.width());
        expect(resized_minimum_bar_spacing ?? Number.POSITIVE_INFINITY)
            .toBeLessThan(prepended_minimum_bar_spacing ?? 0);
    });

    it('crosshair OHLCV를 표시하고 같은 interval의 live update에서는 fitContent를 반복하지 않는다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
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

        // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
        expect(chart_harness.time_scale.fitContent).toHaveBeenCalledOnce();
        expect(chart_harness.candle_series.setData).toHaveBeenCalledOnce();
        expect(chart_harness.candle_series.update).toHaveBeenCalledOnce();
        expect(chart_harness.volume_series.setData).toHaveBeenCalledOnce();
        expect(chart_harness.volume_series.update).toHaveBeenCalledOnce();

        // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
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

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(candle_information).toHaveTextContent('2026.08.20 18:00 KST');
        expect(candle_information).toHaveTextContent('시가 100.00');
        expect(candle_information).toHaveTextContent('고가 120.00');
        expect(candle_information).toHaveTextContent('저가 90.00');
        expect(candle_information).toHaveTextContent('종가 110.00');
        expect(candle_information).toHaveTextContent('등락 10.00%');
        expect(candle_information).toHaveTextContent('변동폭 30.00%');
        expect(candle_information).toHaveTextContent('거래량(BTC) 4,567.8912');

        // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
        act(() => {
            chart_harness.emit_crosshair_move({
                seriesData: new Map(),
            } as unknown as MouseEventParams<Time>);
        });

        // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
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
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
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

        // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
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
