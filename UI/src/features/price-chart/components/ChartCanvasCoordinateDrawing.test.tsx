import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { ChartDrawing } from '../../../shared/contracts';
import type { PriceChartIntent } from '../types';
import type { ChartCoordinateSpace } from './LightweightChartSurface';
import { PriceChartPanel } from './PriceChartPanel';

const chart_surface_controller = vi.hoisted(() => ({
    on_coordinate_space_change: null as ((coordinate_space: unknown) => void) | null,
}));

vi.mock('./LightweightChartSurface', () => {
    interface MockLightweightChartSurfaceProps {
        readonly onCoordinateSpaceChange?: ((coordinate_space: unknown) => void) | undefined;
    }

    /**
     * 함수 이름: MockLightweightChartSurface()
     * 기능: ChartCanvas가 등록한 좌표계 callback을 테스트에서 제어할 수 있게 보관한다.
     * 인자: props -> 좌표계 변경 callback
     * 반환값: 실제 chart pointer 입력을 대신할 canvas mock
     * 작성 날짜: 2026/08/20
     */
    function MockLightweightChartSurface({
        onCoordinateSpaceChange,
    }: MockLightweightChartSurfaceProps) {
        chart_surface_controller.on_coordinate_space_change = onCoordinateSpaceChange ?? null;

        return <canvas data-testid="mock-lightweight-chart" />;
    }

    return { LightweightChartSurface: MockLightweightChartSurface };
});

const BASE_OPEN_TIME = Date.UTC(2026, 7, 20, 9, 0, 0);
const CHART_DATA = {
    activeState: 'WAITING',
    bollingerLower: [],
    bollingerUpper: [],
    candles: [{
        close: 1_010,
        high: 1_020,
        low: 990,
        open: 1_000,
        open_time: BASE_OPEN_TIME,
        volume: 12,
    }],
    ema: [],
    interval: '1m' as const,
    timestampLabel: '2026.08.20 · 18:00 KST',
};


/**
 * 함수 이름: create_coordinate_space_harness()
 * 기능: data 좌표와 pane 좌표 사이 변환을 관찰할 제어 좌표계를 만든다.
 * 인자: pane_width/pane_height -> pane 크기,
 *      x_projection_scale/y_projection_scale -> 저장된 선을 화면에 투영할 배율,
 *      bars_before -> 현재 화면 왼쪽에 적재된 봉 수
 * 반환값: 좌표계와 개별 time·price 변환 mock
 * 작성 날짜: 2026/08/20
 */
function create_coordinate_space_harness(
    pane_width: number,
    pane_height: number,
    x_projection_scale: number,
    y_projection_scale: number,
    bars_before = 200,
) {
    const coordinate_to_time = vi.fn((x_coordinate: number) => {
        return BASE_OPEN_TIME + (x_coordinate * 60_000);
    });
    const coordinate_to_price = vi.fn((y_coordinate: number) => {
        return 1_000 + (y_coordinate / 10);
    });
    const time_to_coordinate = vi.fn((open_time: number) => {
        return ((open_time - BASE_OPEN_TIME) / 60_000) * x_projection_scale;
    });
    const price_to_coordinate = vi.fn((price: number) => {
        return ((price - 1_000) * 10) * y_projection_scale;
    });
    const coordinate_space: ChartCoordinateSpace = {
        bars_before,
        pane_height,
        pane_width,
        visible_from: BASE_OPEN_TIME,
        visible_to: BASE_OPEN_TIME + 400 * 60_000,
        convert_coordinates_to_point: (x_coordinate, y_coordinate) => ({
            open_time: coordinate_to_time(x_coordinate),
            price: coordinate_to_price(y_coordinate),
        }),
        convert_point_to_coordinates: (open_time, price) => ({
            x: time_to_coordinate(open_time),
            y: price_to_coordinate(price),
        }),
    };

    return {
        coordinate_space,
        coordinate_to_price,
        coordinate_to_time,
        price_to_coordinate,
        time_to_coordinate,
    };
}


/**
 * 함수 이름: publish_coordinate_space()
 * 기능: mock surface를 통해 ChartCanvas에 현재 좌표계를 전달한다.
 * 인자: coordinate_space -> 전달할 chart pane 좌표계
 * 반환값: 없음
 * 작성 날짜: 2026/08/20
 */
function publish_coordinate_space(coordinate_space: ChartCoordinateSpace): void {
    const handle_coordinate_space_change = chart_surface_controller.on_coordinate_space_change;

    if (handle_coordinate_space_change === null) {
        throw new Error('ChartCanvas 좌표계 callback이 등록되지 않았습니다.');
    }

    act(() => {
        handle_coordinate_space_change(coordinate_space);
    });
}

describe('ChartCanvas canonical drawing coordinates', () => {
    it('time·price로 저장한 선을 축소·전체화면 좌표계에 재투영하고 x_ratio를 무시한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const handle_intent = vi.fn<(intent: PriceChartIntent) => void>();
        const normal_harness = create_coordinate_space_harness(400, 200, 1, 1);
        const fullscreen_harness = create_coordinate_space_harness(1_200, 700, 3, 2);
        const { rerender } = render(
            <PriceChartPanel
                {...CHART_DATA}
                drawingActive
                onIntent={handle_intent}
            />,
        );

        publish_coordinate_space(normal_harness.coordinate_space);

        const chart = screen.getByRole('img', { name: 'ETH 캔들 가격 차트' });

        vi.spyOn(chart, 'getBoundingClientRect').mockReturnValue(new DOMRect(10, 20, 400, 200));

        // 사용자 조작을 수행하고 그에 따른 비동기 반영을 기다린다.
        fireEvent.pointerDown(chart, { button: 0, clientX: 110, clientY: 70 });
        fireEvent.pointerDown(chart, { button: 0, clientX: 310, clientY: 170 });

        // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
        expect(normal_harness.coordinate_to_time).toHaveBeenNthCalledWith(1, 100);
        expect(normal_harness.coordinate_to_time).toHaveBeenNthCalledWith(2, 300);
        expect(normal_harness.coordinate_to_price).toHaveBeenNthCalledWith(1, 50);
        expect(normal_harness.coordinate_to_price).toHaveBeenNthCalledWith(2, 150);

        const emitted_intents = handle_intent.mock.calls.map(([intent]) => intent);
        const drawing_finished_intent = emitted_intents.find((intent) => {
            return intent.type === 'DRAWING_FINISHED';
        });

        if (drawing_finished_intent?.type !== 'DRAWING_FINISHED') {
            throw new Error('DRAWING_FINISHED intent가 전달되지 않았습니다.');
        }

        expect(drawing_finished_intent.drawing.points).toEqual([
            {
                price: '1005',
                time: new Date(BASE_OPEN_TIME + 100 * 60_000).toISOString(),
                x_ratio: 0.25,
            },
            {
                price: '1015',
                time: new Date(BASE_OPEN_TIME + 300 * 60_000).toISOString(),
                x_ratio: 0.75,
            },
        ]);

        const canonical_drawing: ChartDrawing = {
            ...drawing_finished_intent.drawing,
            points: drawing_finished_intent.drawing.points.map((point, index) => ({
                ...point,
                x_ratio: index === 0 ? 0.01 : 0.99,
            })),
        };

        rerender(
            <PriceChartPanel
                {...CHART_DATA}
                drawings={[canonical_drawing]}
                onIntent={handle_intent}
            />,
        );
        publish_coordinate_space(normal_harness.coordinate_space);

        const drawing_line = screen.getByRole('button', {
            name: `저장된 차트 선 ${canonical_drawing.id}`,
        });

        expect(drawing_line).toHaveAttribute('points', '100.0,50.0 300.0,150.0');

        rerender(
            <PriceChartPanel
                {...CHART_DATA}
                drawings={[canonical_drawing]}
                isFullscreen
                onIntent={handle_intent}
            />,
        );
        publish_coordinate_space(fullscreen_harness.coordinate_space);

        expect(drawing_line).toHaveAttribute('points', '300.0,100.0 900.0,300.0');
        expect(fullscreen_harness.time_to_coordinate).toHaveBeenCalledWith(
            BASE_OPEN_TIME + 100 * 60_000,
        );
        expect(fullscreen_harness.time_to_coordinate).toHaveBeenCalledWith(
            BASE_OPEN_TIME + 300 * 60_000,
        );
        expect(fullscreen_harness.price_to_coordinate).toHaveBeenCalledWith(1_005);
        expect(fullscreen_harness.price_to_coordinate).toHaveBeenCalledWith(1_015);

        const reversed_ratio_drawing: ChartDrawing = {
            ...canonical_drawing,
            points: canonical_drawing.points.map((point, index) => ({
                ...point,
                x_ratio: index === 0 ? 0.99 : 0.01,
            })),
        };

        rerender(
            <PriceChartPanel
                {...CHART_DATA}
                drawings={[reversed_ratio_drawing]}
                isFullscreen
                onIntent={handle_intent}
            />,
        );

        expect(drawing_line).toHaveAttribute('points', '300.0,100.0 900.0,300.0');
    });

    it('왼쪽에 100개 미만이 남으면 현재 주기의 과거 페이지를 한 번만 요청한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const handle_load_earlier = vi.fn();
        const near_history_boundary = create_coordinate_space_harness(400, 200, 1, 1, 99);
        const { rerender } = render(
            <PriceChartPanel
                {...CHART_DATA}
                onLoadEarlier={handle_load_earlier}
            />,
        );

        publish_coordinate_space(near_history_boundary.coordinate_space);

        expect(handle_load_earlier).not.toHaveBeenCalled();  // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.

        // 사용자 조작을 수행하고 그에 따른 비동기 반영을 기다린다.
        fireEvent.wheel(screen.getByTestId('mock-lightweight-chart'), {
            deltaX: 0,
            deltaY: 120,
        });
        publish_coordinate_space({
            ...near_history_boundary.coordinate_space,
            visible_from: BASE_OPEN_TIME + 1,
        });

        expect(handle_load_earlier).not.toHaveBeenCalled();  // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.

        // 사용자 조작을 수행하고 그에 따른 비동기 반영을 기다린다.
        fireEvent.pointerDown(screen.getByTestId('mock-lightweight-chart'));
        publish_coordinate_space({
            ...near_history_boundary.coordinate_space,
            visible_from: BASE_OPEN_TIME + 2,
        });
        publish_coordinate_space({
            ...near_history_boundary.coordinate_space,
            visible_from: BASE_OPEN_TIME + 3,
        });

        // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
        expect(handle_load_earlier).toHaveBeenCalledOnce();

        rerender(
            <PriceChartPanel
                {...CHART_DATA}
                candles={[
                    {
                        close: 995,
                        high: 1_000,
                        low: 980,
                        open: 990,
                        open_time: BASE_OPEN_TIME - 60_000,
                        volume: 10,
                    },
                    ...CHART_DATA.candles,
                ]}
                onLoadEarlier={handle_load_earlier}
            />,
        );

        expect(handle_load_earlier).toHaveBeenCalledOnce();

        // 사용자 조작을 수행하고 그에 따른 비동기 반영을 기다린다.
        fireEvent.pointerDown(screen.getByTestId('mock-lightweight-chart'));
        publish_coordinate_space({
            ...near_history_boundary.coordinate_space,
            visible_from: BASE_OPEN_TIME + 4,
        });

        // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
        expect(handle_load_earlier).toHaveBeenCalledTimes(2);

        rerender(
            <PriceChartPanel
                {...CHART_DATA}
                historyExhausted
                onLoadEarlier={handle_load_earlier}
            />,
        );
        publish_coordinate_space(near_history_boundary.coordinate_space);

        expect(handle_load_earlier).toHaveBeenCalledTimes(2);
    });
});
