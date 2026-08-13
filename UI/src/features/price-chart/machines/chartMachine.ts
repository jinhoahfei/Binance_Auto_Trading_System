import { assign, setup } from 'xstate';
import type {
    ChartDrawing,
    ChartInterval,
} from '../../../shared/contracts';

export interface ChartMachineContext {
    readonly interval: ChartInterval;
    readonly indicators: {
        readonly bollinger_bands: boolean;
        readonly ema9: boolean;
        readonly volume: boolean;
    };
    readonly active_trading_logic_state: string;
    readonly is_fullscreen: boolean;
    readonly drawing_mode: 'deactivated' | 'waiting' | 'drawing';
    readonly selected_line_id: string | null;
    readonly context_menu_position: {
        readonly x: number;
        readonly y: number;
    } | null;
    readonly drawings: Readonly<Record<ChartInterval, ReadonlyArray<ChartDrawing>>>;
}

export interface ChartMachineOptions {
    readonly interval?: ChartInterval;
    readonly indicators?: Partial<ChartMachineContext['indicators']>;
    readonly active_trading_logic_state?: string;
    readonly drawings?: Partial<Record<ChartInterval, ReadonlyArray<ChartDrawing>>>;
}

export type ChartMachineEvent =
    | { readonly type: '1_M_BUTTON_CLICKED' }
    | { readonly type: '30_M_BUTTON_CLICKED' }
    | { readonly type: '4_H_BUTTON_CLICKED' }
    | { readonly type: '1_DAY_BUTTON_CLICKED' }
    | { readonly type: 'INDICATOR_SETTINGS_BUTTON_CLICKED' }
    | { readonly type: 'INDICATOR_POPUP_OUTSIDE_CLICKED' }
    | { readonly type: 'BB_DISPLAY_ON_CLICKED' }
    | { readonly type: 'BB_DISPLAY_OFF_CLICKED' }
    | { readonly type: 'EMA_DISPLAY_ON_CLICKED' }
    | { readonly type: 'EMA_DISPLAY_OFF_CLICKED' }
    | { readonly type: 'VOLUME_DISPLAY_ON_CLICKED' }
    | { readonly type: 'VOLUME_DISPLAY_OFF_CLICKED' }
    | { readonly type: 'TRADING_LOGIC_STATE_CHANGED'; readonly state_label: string }
    | { readonly type: 'FULL_SIZE_SELECTED' }
    | { readonly type: 'NORMAL_SIZE_SELECTED' }
    | { readonly type: 'DRAWING_TOOL_CLICKED' }
    | { readonly type: 'USER_START_DRAWING' }
    | { readonly type: 'USER_FINISH_DRAWING'; readonly drawing: ChartDrawing }
    | { readonly type: 'DRAWING_CANCELED' }
    | { readonly type: 'CURSOR_HOVER_ENTER'; readonly line_id: string }
    | { readonly type: 'CURSOR_HOVER_EXIT' }
    | {
        readonly type: 'HIGHLIGHTED_LINE_RIGHT_CLICKED';
        readonly x?: number;
        readonly y?: number;
    }
    | { readonly type: 'DELETE_LINE' }
    | { readonly type: 'CONTEXT_MENU_OUTSIDE_CLICKED' };

/**
 * 함수 이름: create_chart_machine()
 * 기능: 차트 주기, 지표 설정, 전체화면, drawing과 선 삭제를 병렬 region으로 관리한다.
 * 인자: options -> 차트의 초기 주기·지표·drawing 설정
 * 반환값: price-chart feature의 병렬 XState machine
 * 작성 날짜: 2026/08/12
 */
export function create_chart_machine(options: ChartMachineOptions = {}) {
    const initial_indicators = {
        bollinger_bands: options.indicators?.bollinger_bands ?? true,
        ema9: options.indicators?.ema9 ?? true,
        volume: options.indicators?.volume ?? true,
    };

    return setup({
        types: {
            context: {} as ChartMachineContext,
            events: {} as ChartMachineEvent,
        },
        guards: {
            is_bollinger_bands_visible: ({ context }) => context.indicators.bollinger_bands,
            is_ema9_visible: ({ context }) => context.indicators.ema9,
            is_volume_visible: ({ context }) => context.indicators.volume,
            is_line_selection_idle: ({ context }) => context.selected_line_id === null,
            is_not_drawing: ({ context }) => context.drawing_mode !== 'drawing',
        },
        actions: {
            select_1m_interval: assign({ interval: '1m' }),
            select_30m_interval: assign({ interval: '30m' }),
            select_4h_interval: assign({ interval: '4h' }),
            select_1d_interval: assign({ interval: '1d' }),
            show_bollinger_bands: assign({
                indicators: ({ context }) => ({
                    ...context.indicators,
                    bollinger_bands: true,
                }),
            }),
            hide_bollinger_bands: assign({
                indicators: ({ context }) => ({
                    ...context.indicators,
                    bollinger_bands: false,
                }),
            }),
            show_ema9: assign({
                indicators: ({ context }) => ({
                    ...context.indicators,
                    ema9: true,
                }),
            }),
            hide_ema9: assign({
                indicators: ({ context }) => ({
                    ...context.indicators,
                    ema9: false,
                }),
            }),
            show_volume: assign({
                indicators: ({ context }) => ({
                    ...context.indicators,
                    volume: true,
                }),
            }),
            hide_volume: assign({
                indicators: ({ context }) => ({
                    ...context.indicators,
                    volume: false,
                }),
            }),
            update_active_state: assign({
                active_trading_logic_state: ({ event, context }) => {
                    return event.type === 'TRADING_LOGIC_STATE_CHANGED'
                        ? event.state_label
                        : context.active_trading_logic_state;
                },
            }),
            enter_fullscreen: assign({ is_fullscreen: true }),
            exit_fullscreen: assign({ is_fullscreen: false }),
            deactivate_drawing: assign({ drawing_mode: 'deactivated' }),
            await_drawing: assign({ drawing_mode: 'waiting' }),
            begin_drawing: assign({ drawing_mode: 'drawing' }),
            save_drawing: assign({
                drawing_mode: 'waiting',
                drawings: ({ context, event }) => {
                    if (event.type !== 'USER_FINISH_DRAWING') {
                        return context.drawings;
                    }

                    return {
                        ...context.drawings,
                        [context.interval]: [
                            ...context.drawings[context.interval],
                            event.drawing,
                        ],
                    };
                },
            }),
            select_line: assign({
                selected_line_id: ({ event, context }) => {
                    return event.type === 'CURSOR_HOVER_ENTER'
                        ? event.line_id
                        : context.selected_line_id;
                },
            }),
            clear_line_selection: assign({
                selected_line_id: null,
                context_menu_position: null,
            }),
            remember_context_menu_position: assign({
                context_menu_position: ({ event }) => {
                    return event.type === 'HIGHLIGHTED_LINE_RIGHT_CLICKED'
                        ? { x: event.x ?? 0, y: event.y ?? 0 }
                        : null;
                },
            }),
            delete_selected_line: assign({
                drawings: ({ context }) => {
                    if (context.selected_line_id === null) {
                        return context.drawings;
                    }

                    return {
                        ...context.drawings,
                        [context.interval]: context.drawings[context.interval].filter(
                            (drawing) => drawing.id !== context.selected_line_id,
                        ),
                    };
                },
                selected_line_id: null,
                context_menu_position: null,
            }),
        },
    }).createMachine({
        id: 'chartMachine',
        type: 'parallel',
        context: {
            interval: options.interval ?? '30m',
            indicators: initial_indicators,
            active_trading_logic_state: options.active_trading_logic_state ?? 'WAITING',
            is_fullscreen: false,
            drawing_mode: 'deactivated',
            selected_line_id: null,
            context_menu_position: null,
            drawings: {
                '1m': options.drawings?.['1m'] ?? [],
                '30m': options.drawings?.['30m'] ?? [],
                '4h': options.drawings?.['4h'] ?? [],
                '1d': options.drawings?.['1d'] ?? [],
            },
        },
        states: {
            interval: {
                initial: options.interval ?? '30m',
                on: {
                    '1_M_BUTTON_CLICKED': {
                        target: '.1m',
                        actions: 'select_1m_interval',
                    },
                    '30_M_BUTTON_CLICKED': {
                        target: '.30m',
                        actions: 'select_30m_interval',
                    },
                    '4_H_BUTTON_CLICKED': {
                        target: '.4h',
                        actions: 'select_4h_interval',
                    },
                    '1_DAY_BUTTON_CLICKED': {
                        target: '.1d',
                        actions: 'select_1d_interval',
                    },
                },
                states: {
                    '1m': {
                        meta: { spec_ids: ['DC1-02', 'DC1-08', 'DC1-11', 'CR-04'] },
                    },
                    '30m': {
                        meta: { spec_ids: ['DC1-01', 'DC1-05', 'DC1-09', 'DC1-12', 'CR-04'] },
                    },
                    '4h': {
                        meta: { spec_ids: ['DC1-03', 'DC1-06', 'DC1-13', 'CR-04'] },
                    },
                    '1d': {
                        meta: { spec_ids: ['DC1-04', 'DC1-07', 'DC1-10', 'CR-04'] },
                    },
                },
            },
            indicator_settings: {
                initial: 'closed',
                states: {
                    closed: {
                        meta: { spec_ids: ['DC2-01', 'DC2-03', 'ER-04'] },
                        on: {
                            INDICATOR_SETTINGS_BUTTON_CLICKED: {
                                target: 'opened',
                            },
                        },
                    },
                    opened: {
                        type: 'parallel',
                        meta: { spec_ids: ['DC2-02', 'CR-05', 'ER-03'] },
                        on: {
                            INDICATOR_POPUP_OUTSIDE_CLICKED: {
                                target: 'closed',
                            },
                            INDICATOR_SETTINGS_BUTTON_CLICKED: {
                                target: 'closed',
                            },
                        },
                        states: {
                            bollinger_bands: {
                                initial: 'choice',
                                states: {
                                    choice: {
                                        meta: { spec_ids: ['IP1-01'] },
                                        always: [
                                            {
                                                guard: 'is_bollinger_bands_visible',
                                                target: 'on',
                                            },
                                            { target: 'off' },
                                        ],
                                    },
                                    off: {
                                        meta: { spec_ids: ['IP1-02', 'IP1-04'] },
                                        on: {
                                            BB_DISPLAY_ON_CLICKED: {
                                                target: 'on',
                                                actions: 'show_bollinger_bands',
                                            },
                                        },
                                    },
                                    on: {
                                        meta: { spec_ids: ['IP1-03', 'IP1-05'] },
                                        on: {
                                            BB_DISPLAY_OFF_CLICKED: {
                                                target: 'off',
                                                actions: 'hide_bollinger_bands',
                                            },
                                        },
                                    },
                                },
                            },
                            ema9: {
                                initial: 'choice',
                                states: {
                                    choice: {
                                        meta: { spec_ids: ['IP2-01'] },
                                        always: [
                                            { guard: 'is_ema9_visible', target: 'on' },
                                            { target: 'off' },
                                        ],
                                    },
                                    off: {
                                        meta: { spec_ids: ['IP2-02', 'IP2-04'] },
                                        on: {
                                            EMA_DISPLAY_ON_CLICKED: {
                                                target: 'on',
                                                actions: 'show_ema9',
                                            },
                                        },
                                    },
                                    on: {
                                        meta: { spec_ids: ['IP2-03', 'IP2-05'] },
                                        on: {
                                            EMA_DISPLAY_OFF_CLICKED: {
                                                target: 'off',
                                                actions: 'hide_ema9',
                                            },
                                        },
                                    },
                                },
                            },
                            volume: {
                                initial: 'choice',
                                states: {
                                    choice: {
                                        meta: { spec_ids: ['IP3-01'] },
                                        always: [
                                            { guard: 'is_volume_visible', target: 'on' },
                                            { target: 'off' },
                                        ],
                                    },
                                    off: {
                                        meta: { spec_ids: ['IP3-02', 'IP3-04'] },
                                        on: {
                                            VOLUME_DISPLAY_ON_CLICKED: {
                                                target: 'on',
                                                actions: 'show_volume',
                                            },
                                        },
                                    },
                                    on: {
                                        meta: { spec_ids: ['IP3-03', 'IP3-05'] },
                                        on: {
                                            VOLUME_DISPLAY_OFF_CLICKED: {
                                                target: 'off',
                                                actions: 'hide_volume',
                                            },
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
            active_state: {
                initial: 'displayed',
                states: {
                    displayed: {
                        meta: { spec_ids: ['DC3-01', 'DC3-02'] },
                        on: {
                            TRADING_LOGIC_STATE_CHANGED: {
                                actions: 'update_active_state',
                            },
                        },
                    },
                },
            },
            viewport: {
                initial: 'normal',
                states: {
                    normal: {
                        meta: { spec_ids: ['DC4-01', 'DC4-02', 'ER-17'] },
                        entry: 'exit_fullscreen',
                        on: {
                            FULL_SIZE_SELECTED: {
                                target: 'fullscreen',
                            },
                        },
                    },
                    fullscreen: {
                        meta: { spec_ids: ['DC4-02', 'DC4-03', 'ER-17'] },
                        entry: 'enter_fullscreen',
                        on: {
                            NORMAL_SIZE_SELECTED: {
                                target: 'normal',
                            },
                        },
                    },
                },
            },
            drawing: {
                initial: 'deactivated',
                states: {
                    deactivated: {
                        meta: { spec_ids: ['DC5-01', 'DC5-02'] },
                        entry: 'deactivate_drawing',
                        on: {
                            DRAWING_TOOL_CLICKED: {
                                guard: 'is_line_selection_idle',
                                target: 'waiting',
                            },
                        },
                    },
                    waiting: {
                        meta: { spec_ids: ['DC5-02', 'DC5-03', 'DC5-04'] },
                        entry: 'await_drawing',
                        on: {
                            USER_START_DRAWING: {
                                target: 'user_drawing',
                            },
                            DRAWING_TOOL_CLICKED: {
                                target: 'deactivated',
                            },
                        },
                    },
                    user_drawing: {
                        meta: { spec_ids: ['DC5-03', 'DC5-05', 'DC5-06', 'DC5-07'] },
                        entry: 'begin_drawing',
                        on: {
                            USER_FINISH_DRAWING: {
                                target: 'waiting',
                                actions: 'save_drawing',
                            },
                            DRAWING_CANCELED: {
                                target: 'waiting',
                            },
                            DRAWING_TOOL_CLICKED: {
                                target: 'deactivated',
                            },
                        },
                    },
                },
            },
            line_selection: {
                initial: 'awaiting_selection',
                states: {
                    awaiting_selection: {
                        meta: { spec_ids: ['DC6-01', 'DC6-02'] },
                        entry: 'clear_line_selection',
                        on: {
                            CURSOR_HOVER_ENTER: {
                                guard: 'is_not_drawing',
                                target: 'highlighted',
                                actions: 'select_line',
                            },
                        },
                    },
                    highlighted: {
                        meta: { spec_ids: ['DC6-02', 'DC6-03', 'DC6-04'] },
                        on: {
                            HIGHLIGHTED_LINE_RIGHT_CLICKED: {
                                guard: 'is_not_drawing',
                                target: 'context_menu',
                                actions: 'remember_context_menu_position',
                            },
                            CURSOR_HOVER_EXIT: {
                                target: 'awaiting_selection',
                            },
                        },
                    },
                    context_menu: {
                        meta: { spec_ids: ['DC6-03', 'DC6-05', 'DC6-06'] },
                        on: {
                            DELETE_LINE: {
                                target: 'awaiting_selection',
                                actions: 'delete_selected_line',
                            },
                            CONTEXT_MENU_OUTSIDE_CLICKED: {
                                target: 'awaiting_selection',
                            },
                        },
                    },
                },
            },
        },
    });
}
