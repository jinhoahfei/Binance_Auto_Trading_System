import { useEffect, useState } from 'react';
import type { ChartDrawing } from '../../../shared/contracts';
import type {
    CandleViewModel,
    IndicatorSettingsViewModel,
    LinePointViewModel,
    PriceChartIntent,
} from '../types';
import { LightweightChartSurface } from './LightweightChartSurface';
import styles from './ChartCanvas.module.css';

import chart_maximize_icon from '../../../assets/figma/chart-maximize.svg';
import chart_pen_icon from '../../../assets/figma/chart-pen.svg';

const CHART_WIDTH = 862;
const CHART_HEIGHT = 328;
const CHART_PADDING_TOP = -22;
const CHART_PADDING_BOTTOM = 46;

export interface ChartCanvasProps {
    readonly bollingerLower: ReadonlyArray<LinePointViewModel>;
    readonly bollingerUpper: ReadonlyArray<LinePointViewModel>;
    readonly candles: ReadonlyArray<CandleViewModel>;
    readonly drawingActive?: boolean;
    readonly drawings?: ReadonlyArray<ChartDrawing>;
    readonly ema: ReadonlyArray<LinePointViewModel>;
    readonly indicatorSettings?: IndicatorSettingsViewModel | undefined;
    readonly isFullscreen?: boolean;
    readonly selectedLineId?: string | null;
    readonly lineContextMenuOpen?: boolean;
    readonly contextMenuPosition?: { readonly x: number; readonly y: number } | null;
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

interface DrawingInteractionBounds {
    readonly height_percent: number;
    readonly left_percent: number;
    readonly top_percent: number;
    readonly width_percent: number;
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
    const values = [
        ...candles.flatMap((candle) => [candle.low, candle.high]),
        ...lines.flatMap((line) => line.map((point) => point.value)),
    ];

    if (values.length === 0) {
        return { minimum: 0, maximum: 1 };
    }

    const minimum_value = Math.min(...values);
    const maximum_value = Math.max(...values);
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
 * 함수 이름: build_drawing_points()
 * 기능: 저장된 사용자 drawing의 가격 point를 SVG polyline 좌표 문자열로 변환한다.
 * 인자: drawing -> 표시할 사용자 drawing, range -> 차트 가격 범위
 * 반환값: SVG polyline points 문자열
 * 작성 날짜: 2026/08/12
 */
function build_drawing_points(drawing: ChartDrawing, range: ChartRange): string {
    const horizontal_step = drawing.points.length > 1
        ? CHART_WIDTH / (drawing.points.length - 1)
        : CHART_WIDTH;

    return drawing.points.map((point, index) => {
        const fallback_x = index * horizontal_step;
        const chart_x = point.x_ratio === undefined
            ? fallback_x
            : Math.min(1, Math.max(0, point.x_ratio)) * CHART_WIDTH;

        return `${chart_x.toFixed(1)},${map_price_to_y(Number(point.price), range).toFixed(1)}`;
    }).join(' ');
}

/**
 * 함수 이름: get_drawing_interaction_bounds()
 * 기능: 가는 SVG 선을 마우스와 키보드로 안정적으로 선택할 수 있는 HTML hit 영역을 계산한다.
 * 인자: drawing -> 저장된 사용자 선, range -> 현재 차트 가격 범위
 * 반환값: 차트 interaction layer 기준 백분율 사각 영역
 * 작성 날짜: 2026/08/12
 */
function get_drawing_interaction_bounds(
    drawing: ChartDrawing,
    range: ChartRange,
): DrawingInteractionBounds {
    const horizontal_step = drawing.points.length > 1
        ? CHART_WIDTH / (drawing.points.length - 1)
        : CHART_WIDTH;
    const chart_points = drawing.points.map((point, index) => ({
        x: point.x_ratio === undefined
            ? index * horizontal_step
            : Math.min(1, Math.max(0, point.x_ratio)) * CHART_WIDTH,
        y: map_price_to_y(Number(point.price), range),
    }));
    const x_values = chart_points.map((point) => point.x);
    const y_values = chart_points.map((point) => point.y);
    const minimum_x = x_values.length === 0 ? 0 : Math.min(...x_values);
    const maximum_x = x_values.length === 0 ? CHART_WIDTH : Math.max(...x_values);
    const minimum_y = y_values.length === 0 ? 0 : Math.min(...y_values);
    const maximum_y = y_values.length === 0 ? CHART_HEIGHT : Math.max(...y_values);

    return {
        left_percent: minimum_x / CHART_WIDTH * 100,
        top_percent: minimum_y / CHART_HEIGHT * 100,
        width_percent: Math.max(1, (maximum_x - minimum_x) / CHART_WIDTH * 100),
        height_percent: Math.max(2, (maximum_y - minimum_y) / CHART_HEIGHT * 100),
    };
}

/**
 * 함수 이름: create_draft_drawing_point()
 * 기능: 포인터 좌표를 직렬화 가능한 차트 drawing point와 SVG 좌표로 변환한다.
 * 인자: client_x/client_y -> 화면 포인터 좌표, chart_element -> SVG 차트, range -> 가격 범위
 * 반환값: 화면 미리보기와 저장에 함께 사용하는 drawing point
 * 작성 날짜: 2026/08/12
 */
function create_draft_drawing_point(
    client_x: number,
    client_y: number,
    chart_element: SVGSVGElement,
    range: ChartRange,
): DraftDrawingPoint {
    const chart_rect = chart_element.getBoundingClientRect();
    const x_ratio = chart_rect.width === 0
        ? 0
        : Math.min(1, Math.max(0, (client_x - chart_rect.left) / chart_rect.width));
    const y_ratio = chart_rect.height === 0
        ? 0
        : Math.min(1, Math.max(0, (client_y - chart_rect.top) / chart_rect.height));
    const chart_x = x_ratio * CHART_WIDTH;
    const chart_y = y_ratio * CHART_HEIGHT;

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
 * 기능: 차트 축 가격을 Figma의 백만 단위 표기로 변환한다.
 * 인자: price -> 화면에 표시할 가격
 * 반환값: 소수 둘째 자리 백만 단위 문자열
 * 작성 날짜: 2026/08/12
 */
function format_axis_price(price: number): string {
    return `${(price / 1_000_000).toFixed(2)}M`;
}

/**
 * 함수 이름: ChartCanvas()
 * 기능: 결정적 SVG 캔들, EMA, 볼린저밴드, 격자와 차트 도구 버튼을 표시한다.
 * 인자: props -> 캔들·보조지표 데이터와 차트 intent 처리 함수
 * 반환값: 가격 차트 캔버스 React 요소
 * 작성 날짜: 2026/08/12
 */
export function ChartCanvas({
    bollingerLower,
    bollingerUpper,
    candles,
    drawingActive = false,
    drawings = [],
    ema,
    indicatorSettings,
    isFullscreen = false,
    selectedLineId = null,
    lineContextMenuOpen = false,
    contextMenuPosition = null,
    onIntent,
}: ChartCanvasProps) {
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
    const chart_range = get_chart_range(candles, [
        visible_indicators.bollingerBand ? bollingerLower : [],
        visible_indicators.bollingerBand ? bollingerUpper : [],
        visible_indicators.ema9 ? ema : [],
        drawing_values,
    ]);
    const horizontal_step = candles.length > 0 ? CHART_WIDTH / candles.length : CHART_WIDTH;
    const candle_width = Math.max(7, Math.min(14, horizontal_step * 0.24));
    const axis_values = create_axis_values(chart_range);

    useEffect(() => {
        if (!drawingActive) {
            set_draft_start(null);
            set_draft_current(null);
        }
    }, [drawingActive]);

    return (
        <div
            className={`${styles.canvas} ${isFullscreen ? styles.fullscreenCanvas : ''}`}
            onPointerDown={(event) => {
                const event_target = event.target;

                if (lineContextMenuOpen
                    && event_target instanceof Element
                    && event_target.closest('[data-chart-line-menu]') === null) {
                    onIntent?.({ type: 'DRAWING_LINE_CONTEXT_MENU_CLOSED' });
                }
            }}
            onKeyDown={(event) => {
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
            />
            <svg
                aria-label="ETH 캔들 가격 차트"
                className={styles.chart}
                preserveAspectRatio="none"
                role="img"
                viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
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
                    );

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

                    set_draft_current(create_draft_drawing_point(
                        event.clientX,
                        event.clientY,
                        event.currentTarget,
                        chart_range,
                    ));
                }}
            >
                <g aria-hidden="true" className={styles.grid}>
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
                            const range_ratio = Math.min(
                                1,
                                Math.max(0.18, (candle.high - candle.low) / 48_000),
                            );
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
                <g className={styles.drawings}>
                    {drawings.map((drawing) => {
                        const is_selected = drawing.id === selectedLineId;
                        const drawing_points = build_drawing_points(drawing, chart_range);

                        return (
                            <polyline
                                aria-hidden="true"
                                className={is_selected ? styles.selectedDrawing : styles.drawing}
                                fill="none"
                                key={drawing.id}
                                points={drawing_points}
                            />
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

            <div aria-label="저장된 차트 선" className={styles.drawingInteractionLayer}>
                {drawings.map((drawing) => {
                    const bounds = get_drawing_interaction_bounds(drawing, chart_range);

                    return (
                        <button
                            aria-label={`저장된 차트 선 ${drawing.id}`}
                            className={styles.drawingInteraction}
                            data-drawing-hit-area
                            key={drawing.id}
                            onBlur={() => onIntent?.({ type: 'DRAWING_LINE_HOVER_EXITED' })}
                            onContextMenu={(event) => {
                                event.preventDefault();
                                const layer_rect = event.currentTarget.parentElement?.getBoundingClientRect();

                                onIntent?.({
                                    type: 'DRAWING_LINE_CONTEXT_MENU_REQUESTED',
                                    x: layer_rect === undefined ? 0 : event.clientX - layer_rect.left,
                                    y: layer_rect === undefined ? 0 : event.clientY - layer_rect.top,
                                });
                            }}
                            onFocus={() => onIntent?.({
                                type: 'DRAWING_LINE_HOVER_ENTERED',
                                lineId: drawing.id,
                            })}
                            onKeyDown={(event) => {
                                if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) {
                                    event.preventDefault();
                                    onIntent?.({
                                        type: 'DRAWING_LINE_CONTEXT_MENU_REQUESTED',
                                        x: event.currentTarget.offsetLeft + event.currentTarget.offsetWidth / 2,
                                        y: event.currentTarget.offsetTop + event.currentTarget.offsetHeight / 2,
                                    });
                                }
                            }}
                            onMouseEnter={() => onIntent?.({
                                type: 'DRAWING_LINE_HOVER_ENTERED',
                                lineId: drawing.id,
                            })}
                            onMouseLeave={() => onIntent?.({ type: 'DRAWING_LINE_HOVER_EXITED' })}
                            style={{
                                height: `calc(${bounds.height_percent}% + 16px)`,
                                left: `calc(${bounds.left_percent}% - 8px)`,
                                top: `calc(${bounds.top_percent}% - 8px)`,
                                width: `calc(${bounds.width_percent}% + 16px)`,
                            }}
                            type="button"
                        />
                    );
                })}
            </div>

            {lineContextMenuOpen && contextMenuPosition !== null ? (
                <div
                    className={styles.lineContextMenu}
                    data-chart-line-menu
                    role="menu"
                    style={{ left: contextMenuPosition.x + 8, top: contextMenuPosition.y }}
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

            <div aria-hidden="true" className={styles.priceAxis}>
                {axis_values.map((value) => (
                    <span key={value}>{format_axis_price(value)}</span>
                ))}
            </div>

            <div aria-hidden="true" className={styles.timeAxis}>
                <span>09:00</span>
                <span>09:30</span>
                <span>10:00</span>
                <span>10:30</span>
                <span>11:00</span>
            </div>

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
