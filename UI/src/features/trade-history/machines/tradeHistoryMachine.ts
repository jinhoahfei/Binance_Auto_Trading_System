import { assign, fromPromise, setup } from 'xstate';
import type {
    HistoryPeriod,
    TradeHistoryQuery,
    TradeRecord,
    TradeSideFilter,
    UiCommandFailure,
} from '../../../shared/contracts';
import type { UiCommandPort } from '../../../shared/ports';

export interface TradeHistoryMachineContext {
    readonly period: HistoryPeriod;
    readonly side: TradeSideFilter;
    readonly records: ReadonlyArray<TradeRecord>;
    readonly error: UiCommandFailure | null;
    readonly request_version: number;
}

export interface TradeHistoryMachineOptions {
    readonly period?: HistoryPeriod;
    readonly side?: TradeSideFilter;
    readonly records?: ReadonlyArray<TradeRecord>;
}

export type TradeHistoryMachineEvent =
    | { readonly type: 'ENTER_TRADE_HISTORY' }
    | { readonly type: 'REFRESH_TRADE_HISTORY' }
    | { readonly type: 'SELECT_DISPLAY_TODAY_HISTORY' }
    | { readonly type: 'SELECT_DISPLAY_WEEKLY_HISTORY' }
    | { readonly type: 'SELECT_DISPLAY_MONTHLY_HISTORY' }
    | { readonly type: 'SELECT_DISPLAY_ALL_HISTORY' }
    | { readonly type: 'ALL_TRADE_HISTORY_SELECTED' }
    | { readonly type: 'BUY_TRADE_HISTORY_SELECTED' }
    | { readonly type: 'SELL_TRADE_HISTORY_SELECTED' }
    | { readonly type: 'DISMISS_TRADE_HISTORY_ERROR' };

/**
 * 함수 이름: create_trade_history_machine()
 * 기능: 거래 내역의 기간·방향 필터와 조회 중·결과·빈 결과·오류 상태를 관리한다.
 * 인자: command_port -> 거래 내역 조회 port, options -> 초기 필터와 화면 fixture
 * 반환값: trade-history feature의 XState machine
 * 작성 날짜: 2026/08/12
 */
export function create_trade_history_machine(
    command_port: UiCommandPort,
    options: TradeHistoryMachineOptions = {},
) {
    return setup({
        types: {
            context: {} as TradeHistoryMachineContext,
            events: {} as TradeHistoryMachineEvent,
        },
        actors: {
            load_trade_history: fromPromise<ReadonlyArray<TradeRecord>, TradeHistoryQuery>(
                async ({ input }) => command_port.load_trade_history(input),
            ),
        },
        guards: {
            has_records: ({ event }) => {
                return 'output' in event
                    && Array.isArray(event.output)
                    && event.output.length > 0;
            },
        },
        actions: {
            select_today: assign({
                period: 'today',
                request_version: ({ context }) => context.request_version + 1,
            }),
            select_weekly: assign({
                period: 'last7days',
                request_version: ({ context }) => context.request_version + 1,
            }),
            select_monthly: assign({
                period: 'last30days',
                request_version: ({ context }) => context.request_version + 1,
            }),
            select_all_periods: assign({
                period: 'all',
                request_version: ({ context }) => context.request_version + 1,
            }),
            select_all_sides: assign({
                side: 'all',
                request_version: ({ context }) => context.request_version + 1,
            }),
            select_buy_side: assign({
                side: 'buy',
                request_version: ({ context }) => context.request_version + 1,
            }),
            select_sell_side: assign({
                side: 'sell',
                request_version: ({ context }) => context.request_version + 1,
            }),
            mark_refresh: assign({
                request_version: ({ context }) => context.request_version + 1,
                error: null,
            }),
            store_records: assign({
                records: ({ event }) => {
                    return 'output' in event && Array.isArray(event.output)
                        ? event.output as ReadonlyArray<TradeRecord>
                        : [];
                },
                error: null,
            }),
            remember_failure: assign({
                error: ({ event }) => ({
                    code: 'TRADE_HISTORY_LOAD_FAILED',
                    message: 'error' in event && event.error instanceof Error
                        ? event.error.message
                        : '거래 내역을 불러오지 못했습니다.',
                }),
            }),
            clear_error: assign({ error: null }),
        },
    }).createMachine({
        id: 'tradeHistoryMachine',
        initial: options.records === undefined
            ? 'idle'
            : options.records.length > 0
                ? 'ready'
                : 'empty',
        context: {
            period: options.period ?? 'today',
            side: options.side ?? 'all',
            records: options.records ?? [],
            error: null,
            request_version: 0,
        },
        states: {
            idle: {
                on: {
                    ENTER_TRADE_HISTORY: {
                        target: 'loading',
                        actions: 'mark_refresh',
                    },
                },
            },
            loading: {
                meta: {
                    spec_ids: ['ER-11', 'TD2-01', 'TD3-01'],
                    pending: true,
                },
                invoke: {
                    id: 'load_trade_history_command',
                    src: 'load_trade_history',
                    input: ({ context }) => ({
                        period: context.period,
                        side: context.side,
                    }),
                    onDone: [
                        {
                            guard: 'has_records',
                            target: 'ready',
                            actions: 'store_records',
                        },
                        {
                            target: 'empty',
                            actions: 'store_records',
                        },
                    ],
                    onError: {
                        target: 'failed',
                        actions: 'remember_failure',
                    },
                },
            },
            ready: {
                meta: {
                    spec_ids: ['TD2-01', 'TD2-02', 'TD2-03', 'TD2-04', 'TD2-05', 'TD2-06', 'TD2-07', 'TD2-08', 'TD2-09', 'TD2-10', 'TD2-11', 'TD2-12', 'TD2-13', 'TD3-01', 'TD3-02', 'TD3-03', 'TD3-04', 'TD3-05', 'TD3-06', 'TD3-07', 'CR-10'],
                },
                on: {
                    REFRESH_TRADE_HISTORY: {
                        target: 'loading',
                        actions: 'mark_refresh',
                    },
                    SELECT_DISPLAY_TODAY_HISTORY: {
                        target: 'loading',
                        actions: 'select_today',
                    },
                    SELECT_DISPLAY_WEEKLY_HISTORY: {
                        target: 'loading',
                        actions: 'select_weekly',
                    },
                    SELECT_DISPLAY_MONTHLY_HISTORY: {
                        target: 'loading',
                        actions: 'select_monthly',
                    },
                    SELECT_DISPLAY_ALL_HISTORY: {
                        target: 'loading',
                        actions: 'select_all_periods',
                    },
                    ALL_TRADE_HISTORY_SELECTED: {
                        target: 'loading',
                        actions: 'select_all_sides',
                    },
                    BUY_TRADE_HISTORY_SELECTED: {
                        target: 'loading',
                        actions: 'select_buy_side',
                    },
                    SELL_TRADE_HISTORY_SELECTED: {
                        target: 'loading',
                        actions: 'select_sell_side',
                    },
                },
            },
            empty: {
                meta: {
                    spec_ids: ['VR-10'],
                },
                on: {
                    REFRESH_TRADE_HISTORY: {
                        target: 'loading',
                        actions: 'mark_refresh',
                    },
                    SELECT_DISPLAY_TODAY_HISTORY: {
                        target: 'loading',
                        actions: 'select_today',
                    },
                    SELECT_DISPLAY_WEEKLY_HISTORY: {
                        target: 'loading',
                        actions: 'select_weekly',
                    },
                    SELECT_DISPLAY_MONTHLY_HISTORY: {
                        target: 'loading',
                        actions: 'select_monthly',
                    },
                    SELECT_DISPLAY_ALL_HISTORY: {
                        target: 'loading',
                        actions: 'select_all_periods',
                    },
                    ALL_TRADE_HISTORY_SELECTED: {
                        target: 'loading',
                        actions: 'select_all_sides',
                    },
                    BUY_TRADE_HISTORY_SELECTED: {
                        target: 'loading',
                        actions: 'select_buy_side',
                    },
                    SELL_TRADE_HISTORY_SELECTED: {
                        target: 'loading',
                        actions: 'select_sell_side',
                    },
                },
            },
            failed: {
                on: {
                    REFRESH_TRADE_HISTORY: {
                        target: 'loading',
                        actions: 'mark_refresh',
                    },
                    DISMISS_TRADE_HISTORY_ERROR: {
                        target: 'idle',
                        actions: 'clear_error',
                    },
                },
            },
        },
    });
}

