import {
    useCallback,
    useEffect,
    useRef,
    useState,
} from 'react';
import type { ChartDrawing } from '../../../shared/contracts';
import type {
    CandleViewModel,
    ChartInterval,
    IndicatorSettingsViewModel,
    LinePointViewModel,
    PriceChartDataStatus,
    PriceChartIntent,
} from '../types';
import {
    LightweightChartSurface,
    type ChartCoordinateSpace,
    type ChartPixelPoint,
} from './LightweightChartSurface';
import styles from './ChartCanvas.module.css';

import chart_maximize_icon from '../../../assets/figma/chart-maximize.svg';
import chart_pen_icon from '../../../assets/figma/chart-pen.svg';

const CHART_WIDTH = 862;
const CHART_HEIGHT = 328;
const CHART_PADDING_TOP = -22;
const CHART_PADDING_BOTTOM = 46;
const FALLBACK_CHART_RANGE: ChartRange = { minimum: 0, maximum: 1 };
const FALLBACK_CHART_END_TIME = Date.UTC(2026, 5, 22, 2, 0, 0);
const INTERVAL_DURATION_MILLISECONDS: Readonly<Record<ChartInterval, number>> = {
    '1m': 60_000,
    '30m': 30 * 60_000,
    '4h': 4 * 60 * 60_000,
    '1d': 24 * 60 * 60_000,
};

export interface ChartCanvasProps {
    readonly bollingerLower: ReadonlyArray<LinePointViewModel>;
    readonly bollingerUpper: ReadonlyArray<LinePointViewModel>;
    readonly candles: ReadonlyArray<CandleViewModel>;
    readonly dataStatus?: PriceChartDataStatus;
    readonly drawingActive?: boolean;
    readonly drawings?: ReadonlyArray<ChartDrawing>;
    readonly ema: ReadonlyArray<LinePointViewModel>;
    readonly historyErrorMessage?: string | null;
    readonly historyExhausted?: boolean;
    readonly historyLoading?: boolean;
    readonly indicatorSettings?: IndicatorSettingsViewModel | undefined;
    readonly interval: ChartInterval;
    readonly isFullscreen?: boolean;
    readonly selectedLineId?: string | null;
    readonly lineContextMenuOpen?: boolean;
    readonly contextMenuPosition?: { readonly x: number; readonly y: number } | null;
    readonly statusMessage?: string | null;
    readonly symbol?: string;
    readonly onLoadEarlier?: (() => void) | undefined;
    readonly onIntent?: ((intent: PriceChartIntent) => void) | undefined;
}

interface ChartRange {
    readonly maximum: number;
    readonly minimum: number;
}

interface DraftDrawingPoint {
    readonly chart_x: number;
    readonly chart_y: number;
    readonly price: string;
    readonly time: string;
    readonly x_ratio: number;
}

/**
 * 함수 이름: get_chart_range()
 * 기능: 캔들 및 보조지표 전체를 포함하는 차트 표시 가격 범위를 계산한다.
 * 인자: candles -> 캔들 데이터 배열, lines -> 보조지표 선 데이터 배열
 * 반환값: 최소값과 최대값을 가진 차트 범위
 * 작성 날짜: 2026/08/12
 */
function get_chart_range(
    candles: ReadonlyArray<CandleViewModel>,
    lines: ReadonlyArray<ReadonlyArray<LinePointViewModel>>,
): ChartRange {
    let minimum_value = Number.POSITIVE_INFINITY;
    let maximum_value = Number.NEGATIVE_INFINITY;

    candles.forEach((candle) => {
        minimum_value = Math.min(minimum_value, candle.low);
        maximum_value = Math.max(maximum_value, candle.high);
    });
    lines.forEach((line) => {
        line.forEach((point) => {
            minimum_value = Math.min(minimum_value, point.value);
            maximum_value = Math.max(maximum_value, point.value);
        });
    });

    if (!Number.isFinite(minimum_value) || !Number.isFinite(maximum_value)) {
        return FALLBACK_CHART_RANGE;
    }

    return {
        minimum: minimum_value,
        maximum: maximum_value === minimum_value ? minimum_value + 1 : maximum_value,
    };
}

/**
 * 함수 이름: create_axis_values()
 * 기능: 실제 가격 범위를 네 구간의 읽기 쉬운 축 가격으로 변환한다.
 * 인자: range -> 캔들 및 지표가 구성한 원시 가격 범위
 * 반환값: 위에서 아래 순서로 표시할 다섯 개 가격 값
 * 작성 날짜: 2026/08/12
 */
function create_axis_values(range: ChartRange): ReadonlyArray<number> {
    const rough_step = Math.max(1, (range.maximum - range.minimum) / 4);
    const magnitude = 10 ** Math.floor(Math.log10(rough_step));
    const normalized_step = rough_step / magnitude;
    const step_candidates = [1, 2, 2.5, 4, 5, 10] as const;
    const normalized_nice_step = step_candidates.find((candidate) => candidate >= normalized_step) ?? 10;
    const nice_step = normalized_nice_step * magnitude;
    const minimum_axis_value = Math.floor(range.minimum / nice_step) * nice_step;

    return Array.from({ length: 5 }, (_, index) => minimum_axis_value + nice_step * (4 - index));
}

/**
 * 함수 이름: map_price_to_y()
 * 기능: 가격을 SVG 차트 내부의 세로 좌표로 변환한다.
 * 인자: price -> 변환할 가격, range -> 차트 가격 범위
 * 반환값: SVG 세로 좌표
 * 작성 날짜: 2026/08/12
 */
function map_price_to_y(price: number, range: ChartRange): number {
    const drawable_height = CHART_HEIGHT - CHART_PADDING_TOP - CHART_PADDING_BOTTOM;
    const normalized_value = (price - range.minimum) / (range.maximum - range.minimum);

    return CHART_PADDING_TOP + drawable_height * (1 - normalized_value);
}

/**
 * 함수 이름: map_y_to_price()
 * 기능: SVG 세로 좌표를 현재 차트 범위의 가격으로 역변환한다.
 * 인자: chart_y -> SVG 세로 좌표, range -> 차트 가격 범위
 * 반환값: 정수 단위 가격
 * 작성 날짜: 2026/08/12
 */
function map_y_to_price(chart_y: number, range: ChartRange): number {
    const drawable_height = CHART_HEIGHT - CHART_PADDING_TOP - CHART_PADDING_BOTTOM;
    const bounded_y = Math.min(
        CHART_HEIGHT - CHART_PADDING_BOTTOM,
        Math.max(CHART_PADDING_TOP, chart_y),
    );
    const normalized_value = 1 - ((bounded_y - CHART_PADDING_TOP) / drawable_height);

    return range.minimum + normalized_value * (range.maximum - range.minimum);
}

/**
 * 함수 이름: build_line_points()
 * 기능: 보조지표 배열을 SVG polyline의 points 문자열로 변환한다.
 * 인자: points -> 보조지표 값 배열, range -> 차트 가격 범위
 * 반환값: SVG polyline points 문자열
 * 작성 날짜: 2026/08/12
 */
function build_line_points(points: ReadonlyArray<LinePointViewModel>, range: ChartRange): string {
    const horizontal_step = points.length > 1 ? CHART_WIDTH / (points.length - 1) : CHART_WIDTH;

    return points.map((point, index) => (
        `${(index * horizontal_step).toFixed(1)},${map_price_to_y(point.value, range).toFixed(1)}`
    )).join(' ');
}

/**
 * 함수 이름: get_drawing_pixel_points()
 * 기능: drawing의 실제 봉 시각·가격을 현재 Lightweight Charts pane 좌표로 투영한다.
 * 인자: drawing -> 표시할 사용자 drawing
 *      range -> chart engine이 없을 때 사용할 fallback 가격 범위
 *      coordinate_space -> 현재 zoom·pan·resize가 반영된 chart 좌표계
 * 반환값: 현재 pane에서 표시할 drawing point 목록
 * 작성 날짜: 2026/08/20
 */
function get_drawing_pixel_points(
    drawing: ChartDrawing,
    range: ChartRange,
    coordinate_space: ChartCoordinateSpace | null,
): ReadonlyArray<ChartPixelPoint> {
    if (coordinate_space !== null) {
        return drawing.points.flatMap((point) => {
            const open_time = Date.parse(point.time);
            const price = Number(point.price);

            if (!Number.isFinite(open_time) || !Number.isFinite(price)) {
                return [];
            }

            const chart_point = coordinate_space.convert_point_to_coordinates(open_time, price);

            return chart_point === null ? [] : [chart_point];
        });
    }

    const horizontal_step = drawing.points.length > 1
        ? CHART_WIDTH / (drawing.points.length - 1)
        : CHART_WIDTH;

    return drawing.points.map((point, index) => {
        const fallback_x = index * horizontal_step;
        const chart_x = point.x_ratio === undefined
            ? fallback_x
            : Math.min(1, Math.max(0, point.x_ratio)) * CHART_WIDTH;

        return { x: chart_x, y: map_price_to_y(Number(point.price), range) };
    });
}

/**
 * 함수 이름: build_drawing_points()
 * 기능: 현재 pane의 drawing point 목록을 SVG polyline points 문자열로 변환한다.
 * 인자: drawing_points -> 현재 pane에 투영된 drawing point 목록
 * 반환값: SVG polyline points 문자열
 * 작성 날짜: 2026/08/20
 */
function build_drawing_points(drawing_points: ReadonlyArray<ChartPixelPoint>): string {
    return drawing_points.map((point) => {
        return `${point.x.toFixed(1)},${point.y.toFixed(1)}`;
    }).join(' ');
}

/**
 * 함수 이름: format_drawing_price()
 * 기능: chart engine이 계산한 가격을 직렬화 가능한 최대 여덟 소수 자릿수 문자열로 만든다.
 * 인자: price -> 저장할 chart 가격
 * 반환값: 불필요한 뒤쪽 0이 제거된 가격 문자열
 * 작성 날짜: 2026/08/20
 */
function format_drawing_price(price: number): string {
    return price.toFixed(8).replace(/\.?0+$/, '');
}

/**
 * 함수 이름: create_draft_drawing_point()
 * 기능: 포인터 좌표를 실제 봉 시각·가격에 고정된 직렬화 drawing point로 변환한다.
 * 인자: client_x/client_y -> 화면 포인터 좌표
 *      chart_element -> drawing SVG
 *      range -> chart engine이 없을 때 사용할 fallback 가격 범위
 *      coordinate_space -> 현재 zoom·pan·resize가 반영된 chart 좌표계
 * 반환값: 화면 미리보기와 저장에 함께 사용하는 drawing point 또는 변환 실패 시 null
 * 작성 날짜: 2026/08/20
 */
function create_draft_drawing_point(
    client_x: number,
    client_y: number,
    chart_element: SVGSVGElement,
    range: ChartRange,
    coordinate_space: ChartCoordinateSpace | null,
): DraftDrawingPoint | null {
    const chart_rect = chart_element.getBoundingClientRect();
    const x_ratio = chart_rect.width === 0
        ? 0
        : Math.min(1, Math.max(0, (client_x - chart_rect.left) / chart_rect.width));
    const y_ratio = chart_rect.height === 0
        ? 0
        : Math.min(1, Math.max(0, (client_y - chart_rect.top) / chart_rect.height));
    const chart_width = coordinate_space?.pane_width ?? CHART_WIDTH;
    const chart_height = coordinate_space?.pane_height ?? CHART_HEIGHT;
    const chart_x = x_ratio * chart_width;
    const chart_y = y_ratio * chart_height;

    if (coordinate_space !== null) {
        const chart_point = coordinate_space.convert_coordinates_to_point(chart_x, chart_y);

        if (chart_point === null) {
            return null;
        }

        return {
            chart_x,
            chart_y,
            price: format_drawing_price(chart_point.price),
            time: new Date(chart_point.open_time).toISOString(),
            x_ratio,
        };
    }

    return {
        chart_x,
        chart_y,
        price: Math.round(map_y_to_price(chart_y, range)).toString(),
        time: new Date().toISOString(),
        x_ratio,
    };
}

/**
 * 함수 이름: format_axis_price()
 * 기능: 차트 축 가격을 값의 크기에 맞는 읽기 쉬운 표기로 변환한다.
 * 인자: price -> 화면에 표시할 가격
 * 반환값: 가격 축 문자열
 * 작성 날짜: 2026/08/20
 */
function format_axis_price(price: number): string {
    const absolute_price = Math.abs(price);

    if (absolute_price >= 1_000_000) {
        return `${(price / 1_000_000).toFixed(2)}M`;
    }
    if (absolute_price >= 1_000) {
        return new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 }).format(price);
    }

    return new Intl.NumberFormat('en-US', {
        maximumFractionDigits: absolute_price >= 1 ? 2 : 6,
    }).format(price);
}

/**
 * 함수 이름: get_candle_open_time()
 * 기능: 실시간 봉 시각 또는 fixture용 주기 기반 시각을 반환한다.
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

    return FALLBACK_CHART_END_TIME
        - ((candle_count - index) * INTERVAL_DURATION_MILLISECONDS[interval]);
}

/**
 * 함수 이름: format_time_axis_value()
 * 기능: 봉 주기에 맞춰 KST 차트 축 시각을 시간 또는 날짜 형식으로 표시한다.
 * 인자: open_time -> 밀리초 단위 UNIX 시각, interval -> 봉 주기
 * 반환값: 차트 하단 축 문자열
 * 작성 날짜: 2026/08/20
 */
function format_time_axis_value(open_time: number, interval: ChartInterval): string {
    if (interval === '1d') {
        return new Intl.DateTimeFormat('ko-KR', {
            month: '2-digit',
            day: '2-digit',
            timeZone: 'Asia/Seoul',
        }).format(new Date(open_time));
    }
    if (interval === '4h' || interval === '30m') {
        return new Intl.DateTimeFormat('ko-KR', {
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            ...(interval === '30m' ? { minute: '2-digit' as const } : {}),
            hour12: false,
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
 * 함수 이름: create_time_axis_labels()
 * 기능: 현재 표시 봉의 처음부터 끝까지 균등한 다섯 개 KST 축 라벨을 만든다.
 * 인자: candles -> 표시할 봉 목록, interval -> 봉 주기
 * 반환값: 왼쪽에서 오른쪽 순서의 다섯 시각 라벨
 * 작성 날짜: 2026/08/20
 */
function create_time_axis_labels(
    candles: ReadonlyArray<CandleViewModel>,
    interval: ChartInterval,
): ReadonlyArray<string> {
    const first_candle = candles[0];
    const last_candle = candles.at(-1);

    if (first_candle === undefined || last_candle === undefined) {
        return Array.from({ length: 5 }, () => '--:--');
    }

    const first_open_time = get_candle_open_time(first_candle, 0, candles.length, interval);
    const last_open_time = get_candle_open_time(
        last_candle,
        candles.length - 1,
        candles.length,
        interval,
    );

    return Array.from({ length: 5 }, (_, index) => {
        const open_time = first_open_time + ((last_open_time - first_open_time) * index / 4);

        return format_time_axis_value(open_time, interval);
    });
}

/**
 * 함수 이름: ChartCanvas()
 * 기능: 금융 차트 위에 시각·가격 기반 drawing, 상태와 차트 도구를 조합한다.
 * 인자: props -> 시장 데이터, drawing 상태와 차트 intent 처리 함수
 * 반환값: 확대·이동·전체화면에 동기화되는 가격 차트 캔버스
 * 작성 날짜: 2026/08/20
 */
export function ChartCanvas({
    bollingerLower,
    bollingerUpper,
    candles,
    dataStatus = 'idle',
    drawingActive = false,
    drawings = [],
    ema,
    historyErrorMessage = null,
    historyExhausted = false,
    historyLoading = false,
    indicatorSettings,
    interval,
    isFullscreen = false,
    selectedLineId = null,
    lineContextMenuOpen = false,
    contextMenuPosition = null,
    statusMessage = null,
    symbol = 'ETHUSDT',
    onLoadEarlier,
    onIntent,
}: ChartCanvasProps) {
    const history_boundary_request_ref = useRef<string | null>(null);
    const history_navigation_armed_ref = useRef(false);
    const [chart_coordinate_space, set_chart_coordinate_space] = useState<ChartCoordinateSpace | null>(null);
    const [draft_start, set_draft_start] = useState<DraftDrawingPoint | null>(null);
    const [draft_current, set_draft_current] = useState<DraftDrawingPoint | null>(null);
    const visible_indicators = indicatorSettings ?? {
        bollingerBand: true,
        ema9: true,
        volume: false,
    };
    const drawing_values = drawings.flatMap((drawing) => {
        return drawing.points.map((point) => ({ value: Number(point.price) }));
    });
    const should_render_fallback_market = chart_coordinate_space === null;
    const chart_range = should_render_fallback_market
        ? get_chart_range(candles, [
            visible_indicators.bollingerBand ? bollingerLower : [],
            visible_indicators.bollingerBand ? bollingerUpper : [],
            visible_indicators.ema9 ? ema : [],
            drawing_values,
        ])
        : FALLBACK_CHART_RANGE;
    const horizontal_step = should_render_fallback_market && candles.length > 0
        ? CHART_WIDTH / candles.length
        : CHART_WIDTH;
    const candle_width = Math.max(7, Math.min(14, horizontal_step * 0.24));
    const axis_values = should_render_fallback_market ? create_axis_values(chart_range) : [];
    const time_axis_labels = should_render_fallback_market
        ? create_time_axis_labels(candles, interval)
        : [];
    const chart_width = chart_coordinate_space?.pane_width ?? CHART_WIDTH;
    const chart_height = chart_coordinate_space?.pane_height ?? CHART_HEIGHT;
    const context_menu_left = contextMenuPosition === null
        ? 0
        : Math.min(Math.max(0, contextMenuPosition.x + 8), Math.max(0, chart_width - 116));
    const context_menu_top = contextMenuPosition === null
        ? 0
        : Math.min(Math.max(0, contextMenuPosition.y), Math.max(0, chart_height - 42));
    const maximum_volume = should_render_fallback_market
        ? candles.reduce((current_maximum, candle) => {
            const volume = candle.volume ?? Math.max(1, candle.high - candle.low);

            return Math.max(current_maximum, volume);
        }, 1)
        : 1;
    const earliest_open_time = candles[0]?.open_time;
    const history_boundary_is_visible = chart_coordinate_space?.bars_before !== null
        && chart_coordinate_space?.bars_before !== undefined
        && chart_coordinate_space.bars_before < 100;
    const handle_coordinate_space_change = useCallback((coordinate_space: ChartCoordinateSpace | null) => {
        set_chart_coordinate_space(coordinate_space);
    }, []);

    useEffect(() => {
        if (!history_boundary_is_visible) {
            history_boundary_request_ref.current = null;
            return;
        }

        if (!history_navigation_armed_ref.current
            || onLoadEarlier === undefined
            || historyLoading
            || historyExhausted
            || earliest_open_time === undefined) {
            return;
        }

        const boundary_request_key = `${interval}:${earliest_open_time}`;

        if (history_boundary_request_ref.current === boundary_request_key) {
            return;
        }

        history_boundary_request_ref.current = boundary_request_key;
        history_navigation_armed_ref.current = false;
        onLoadEarlier();
    }, [
        earliest_open_time,
        chart_coordinate_space?.visible_from,
        history_boundary_is_visible,
        historyExhausted,
        historyLoading,
        interval,
        onLoadEarlier,
    ]);

    useEffect(() => {
        history_boundary_request_ref.current = null;
        history_navigation_armed_ref.current = false;
    }, [interval]);

    useEffect(() => {
        if (!drawingActive) {
            set_draft_start(null);
            set_draft_current(null);
        }
    }, [drawingActive]);

    useEffect(() => {
        if (!lineContextMenuOpen) {
            return undefined;
        }

        const handle_outside_pointer_down = (event: PointerEvent) => {
            if (event.target instanceof Element
                && (event.target.closest('[data-chart-line-menu]') !== null
                    || event.target.closest('[data-drawing-hit-area]') !== null)) {
                return;
            }

            onIntent?.({ type: 'DRAWING_LINE_CONTEXT_MENU_CLOSED' });
        };

        document.addEventListener('pointerdown', handle_outside_pointer_down);

        return () => document.removeEventListener('pointerdown', handle_outside_pointer_down);
    }, [lineContextMenuOpen, onIntent]);

    return (
        <div
            className={`${styles.canvas} ${isFullscreen ? styles.fullscreenCanvas : ''}`}
            data-chart-bars-before={chart_coordinate_space?.bars_before ?? undefined}
            data-chart-visible-from={chart_coordinate_space?.visible_from ?? undefined}
            data-chart-visible-to={chart_coordinate_space?.visible_to ?? undefined}
            data-drawing-active={drawingActive}
            data-loaded-candle-count={candles.length}
            onPointerDownCapture={(event) => {
                if (!drawingActive && event.target instanceof HTMLCanvasElement) {
                    history_navigation_armed_ref.current = true;
                }
            }}
            onWheelCapture={(event) => {
                if (event.target instanceof HTMLCanvasElement
                    && Math.abs(event.deltaX) > Math.abs(event.deltaY)) {
                    history_navigation_armed_ref.current = true;
                }
            }}
            onKeyDown={(event) => {
                if (event.key === 'Escape' && lineContextMenuOpen) {
                    event.preventDefault();
                    onIntent?.({ type: 'DRAWING_LINE_CONTEXT_MENU_CLOSED' });
                    return;
                }

                if (event.key === 'Escape' && draft_start !== null) {
                    set_draft_start(null);
                    set_draft_current(null);
                    onIntent?.({ type: 'DRAWING_CANCELED' });
                }
            }}
            tabIndex={drawingActive ? 0 : -1}
        >
            <LightweightChartSurface
                bollingerLower={bollingerLower}
                bollingerUpper={bollingerUpper}
                candles={candles}
                ema={ema}
                indicatorSettings={visible_indicators}
                interval={interval}
                onCoordinateSpaceChange={handle_coordinate_space_change}
                symbol={symbol}
            />
            <svg
                aria-label="ETH 캔들 가격 차트"
                className={styles.chart}
                preserveAspectRatio="none"
                role="img"
                style={{ height: chart_height, width: chart_width }}
                viewBox={`0 0 ${chart_width} ${chart_height}`}
                onPointerDown={(event) => {
                    if (!drawingActive
                        || event.button !== 0
                        || (event.target instanceof Element
                            && event.target.closest('[data-drawing-hit-area]') !== null)) {
                        return;
                    }

                    const drawing_point = create_draft_drawing_point(
                        event.clientX,
                        event.clientY,
                        event.currentTarget,
                        chart_range,
                        chart_coordinate_space,
                    );

                    if (drawing_point === null) {
                        return;
                    }

                    if (draft_start === null) {
                        set_draft_start(drawing_point);
                        set_draft_current(drawing_point);
                        onIntent?.({ type: 'DRAWING_STARTED' });
                        return;
                    }

                    const drawing: ChartDrawing = {
                        id: `drawing-${Date.now().toString(36)}`,
                        points: [
                            {
                                time: draft_start.time,
                                price: draft_start.price,
                                x_ratio: draft_start.x_ratio,
                            },
                            {
                                time: drawing_point.time,
                                price: drawing_point.price,
                                x_ratio: drawing_point.x_ratio,
                            },
                        ],
                    };

                    set_draft_start(null);
                    set_draft_current(null);
                    onIntent?.({ type: 'DRAWING_FINISHED', drawing });
                }}
                onPointerMove={(event) => {
                    if (!drawingActive || draft_start === null) {
                        return;
                    }

                    const drawing_point = create_draft_drawing_point(
                        event.clientX,
                        event.clientY,
                        event.currentTarget,
                        chart_range,
                        chart_coordinate_space,
                    );

                    if (drawing_point !== null) {
                        set_draft_current(drawing_point);
                    }
                }}
            >
                {should_render_fallback_market ? (
                    <>
                        <g aria-hidden="true" className={`${styles.grid} ${styles.marketLayer}`}>
                    {Array.from({ length: 5 }, (_, index) => (
                        <line
                            key={`horizontal-${index}`}
                            x1="0"
                            x2={CHART_WIDTH}
                            y1={24 + index * 70}
                            y2={24 + index * 70}
                        />
                    ))}
                    {Array.from({ length: 6 }, (_, index) => (
                        <line
                            key={`vertical-${index}`}
                            x1={108 + index * 140}
                            x2={108 + index * 140}
                            y1="0"
                            y2={CHART_HEIGHT}
                        />
                    ))}
                        </g>
                        {visible_indicators.bollingerBand ? (
                    <g className={styles.marketLayer} data-indicator="bollinger-band">
                        <polyline
                            className={styles.bollinger}
                            fill="none"
                            points={build_line_points(bollingerUpper, chart_range)}
                        />
                        <polyline
                            className={styles.bollinger}
                            fill="none"
                            points={build_line_points(bollingerLower, chart_range)}
                        />
                    </g>
                        ) : null}
                        {visible_indicators.ema9 ? (
                    <polyline
                        className={`${styles.ema} ${styles.marketLayer}`}
                        data-indicator="ema9"
                        fill="none"
                        points={build_line_points(ema, chart_range)}
                    />
                        ) : null}
                        <g aria-hidden="true" className={styles.marketLayer}>
                    {candles.map((candle, index) => {
                        const candle_is_positive = candle.close >= candle.open;
                        const candle_color_class = candle_is_positive ? styles.positiveCandle : styles.negativeCandle;
                        const candle_center_x = horizontal_step * index + horizontal_step / 2;
                        const open_y = map_price_to_y(candle.open, chart_range);
                        const close_y = map_price_to_y(candle.close, chart_range);
                        const body_y = Math.min(open_y, close_y);
                        const body_height = Math.max(Math.abs(close_y - open_y), 3);

                        return (
                            <g className={candle_color_class} key={`candle-${index}`}>
                                <line
                                    className={styles.wick}
                                    x1={candle_center_x}
                                    x2={candle_center_x}
                                    y1={map_price_to_y(candle.high, chart_range)}
                                    y2={map_price_to_y(candle.low, chart_range)}
                                />
                                <rect
                                    height={body_height}
                                    rx="1"
                                    width={candle_width}
                                    x={candle_center_x - candle_width / 2}
                                    y={body_y}
                                />
                            </g>
                        );
                    })}
                        </g>
                        {visible_indicators.volume ? (
                    <g
                        aria-hidden="true"
                        className={`${styles.volumeBars} ${styles.marketLayer}`}
                        data-indicator="volume"
                    >
                        {candles.map((candle, index) => {
                            const candle_is_positive = candle.close >= candle.open;
                            const candle_center_x = horizontal_step * index + horizontal_step / 2;
                            const volume = candle.volume ?? Math.max(1, candle.high - candle.low);
                            const range_ratio = Math.min(1, Math.max(0.18, volume / maximum_volume));
                            const bar_height = 12 + range_ratio * 34;

                            return (
                                <rect
                                    className={candle_is_positive ? styles.positiveVolume : styles.negativeVolume}
                                    height={bar_height}
                                    key={`volume-${index}`}
                                    width={candle_width}
                                    x={candle_center_x - candle_width / 2}
                                    y={CHART_HEIGHT - CHART_PADDING_BOTTOM - bar_height}
                                />
                            );
                        })}
                    </g>
                        ) : null}
                    </>
                ) : null}
                <g className={styles.drawings}>
                    {drawings.map((drawing) => {
                        const is_selected = drawing.id === selectedLineId;
                        const drawing_pixel_points = get_drawing_pixel_points(
                            drawing,
                            chart_range,
                            chart_coordinate_space,
                        );
                        const drawing_points = build_drawing_points(drawing_pixel_points);
                        const first_point = drawing_pixel_points[0];
                        const last_point = drawing_pixel_points.at(-1);

                        if (drawing_pixel_points.length < 2
                            || first_point === undefined
                            || last_point === undefined) {
                            return null;
                        }

                        return (
                            <g key={drawing.id}>
                                <polyline
                                    aria-hidden="true"
                                    className={is_selected ? styles.selectedDrawing : styles.drawing}
                                    fill="none"
                                    points={drawing_points}
                                />
                                <polyline
                                    aria-label={`저장된 차트 선 ${drawing.id}`}
                                    className={styles.drawingHitArea}
                                    data-drawing-hit-area
                                    fill="none"
                                    onBlur={() => onIntent?.({ type: 'DRAWING_LINE_HOVER_EXITED' })}
                                    onContextMenu={(event) => {
                                        event.preventDefault();
                                        const chart_rect = event.currentTarget.ownerSVGElement
                                            ?.getBoundingClientRect();

                                        onIntent?.({
                                            type: 'DRAWING_LINE_CONTEXT_MENU_REQUESTED',
                                            x: chart_rect === undefined
                                                ? 0
                                                : event.clientX - chart_rect.left,
                                            y: chart_rect === undefined
                                                ? 0
                                                : event.clientY - chart_rect.top,
                                        });
                                    }}
                                    onFocus={() => onIntent?.({
                                        type: 'DRAWING_LINE_HOVER_ENTERED',
                                        lineId: drawing.id,
                                    })}
                                    onKeyDown={(event) => {
                                        if (event.key === 'ContextMenu'
                                            || (event.shiftKey && event.key === 'F10')) {
                                            event.preventDefault();
                                            onIntent?.({
                                                type: 'DRAWING_LINE_CONTEXT_MENU_REQUESTED',
                                                x: (first_point.x + last_point.x) / 2,
                                                y: (first_point.y + last_point.y) / 2,
                                            });
                                        }
                                    }}
                                    onMouseEnter={() => onIntent?.({
                                        type: 'DRAWING_LINE_HOVER_ENTERED',
                                        lineId: drawing.id,
                                    })}
                                    onMouseLeave={() => onIntent?.({ type: 'DRAWING_LINE_HOVER_EXITED' })}
                                    pointerEvents={drawingActive ? 'none' : 'stroke'}
                                    points={drawing_points}
                                    role="button"
                                    tabIndex={drawingActive ? -1 : 0}
                                />
                            </g>
                        );
                    })}
                </g>
                {draft_start !== null && draft_current !== null ? (
                    <line
                        aria-hidden="true"
                        className={styles.draftDrawing}
                        x1={draft_start.chart_x}
                        x2={draft_current.chart_x}
                        y1={draft_start.chart_y}
                        y2={draft_current.chart_y}
                    />
                ) : null}
            </svg>

            {candles.length === 0 ? (
                <div
                    aria-live={dataStatus === 'error' ? 'assertive' : 'polite'}
                    className={styles.dataStatus}
                    role={dataStatus === 'error' ? 'alert' : 'status'}
                >
                    <strong>{dataStatus === 'error' ? '차트 연결 실패' : '시장 데이터 연결 중'}</strong>
                    <span>{statusMessage ?? 'Binance 실시간 봉을 불러오고 있습니다.'}</span>
                </div>
            ) : null}

            {candles.length > 0
            && history_boundary_is_visible
            && (historyLoading || historyExhausted || historyErrorMessage !== null) ? (
                    <div
                        aria-live="polite"
                        className={styles.historyDataStatus}
                        role={historyErrorMessage === null ? 'status' : 'alert'}
                    >
                        {historyErrorMessage !== null ? (
                            <button
                                onClick={() => {
                                    history_boundary_request_ref.current = null;
                                    onLoadEarlier?.();
                                }}
                                type="button"
                            >
                                이전 봉 다시 불러오기
                            </button>
                        ) : (
                            <span>
                                {historyLoading
                                    ? '이전 봉을 불러오는 중입니다.'
                                    : 'Binance의 첫 거래 봉까지 모두 불러왔습니다.'}
                            </span>
                        )}
                    </div>
                ) : null}

            {lineContextMenuOpen && contextMenuPosition !== null ? (
                <div
                    className={styles.lineContextMenu}
                    data-chart-line-menu
                    role="menu"
                    style={{ left: context_menu_left, top: context_menu_top }}
                >
                    <button
                        onClick={() => onIntent?.({ type: 'DRAWING_LINE_DELETE_REQUESTED' })}
                        role="menuitem"
                        type="button"
                    >
                        선 삭제
                    </button>
                </div>
            ) : null}

            {should_render_fallback_market ? (
                <>
                    <div aria-hidden="true" className={styles.priceAxis}>
                        {axis_values.map((value) => (
                            <span key={value}>{format_axis_price(value)}</span>
                        ))}
                    </div>
                    <div aria-hidden="true" className={styles.timeAxis}>
                        {time_axis_labels.map((label, index) => (
                            <span key={`${label}-${index}`}>{label}</span>
                        ))}
                    </div>
                </>
            ) : null}

            <div className={styles.tools}>
                <button
                    aria-label="차트 드로잉 모드"
                    aria-pressed={drawingActive}
                    className={drawingActive ? styles.activeTool : styles.tool}
                    onClick={() => onIntent?.({ type: 'DRAWING_MODE_REQUESTED' })}
                    title="차트 드로잉 모드"
                    type="button"
                >
                    <img alt="" src={chart_pen_icon} />
                </button>
                <button
                    aria-label={isFullscreen ? '차트 전체화면 종료' : '차트 전체화면'}
                    className={styles.tool}
                    onClick={() => onIntent?.({ type: 'CHART_FULLSCREEN_REQUESTED' })}
                    title={isFullscreen ? '차트 전체화면 종료' : '차트 전체화면'}
                    type="button"
                >
                    <img alt="" src={chart_maximize_icon} />
                </button>
            </div>
        </div>
    );
}
