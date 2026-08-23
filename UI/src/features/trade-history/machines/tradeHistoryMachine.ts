import { assign, fromPromise, setup } from 'xstate';
import type {
    HistoryPeriod,
    TradeHistoryDetails,
    TradeHistoryQuery,
    TradeHistorySummary,
    TradeRecord,
    TradeSideFilter,
    UiCommandFailure,
} from '../../../shared/contracts';
import type { UiCommandPort } from '../../../shared/ports';
import { to_ui_command_failure } from '../../../shared/errors';

const KST_UTC_OFFSET_MILLISECONDS = 9 * 60 * 60 * 1_000;

export interface TradeHistoryMachineContext {
    readonly symbol: string;
    readonly period: HistoryPeriod;
    readonly side: TradeSideFilter;
    readonly records: ReadonlyArray<TradeRecord>;
    readonly error: UiCommandFailure | null;
    readonly request_version: number;
    readonly has_entered: boolean;
    readonly refresh_pending: boolean;
    readonly should_publish_summary: boolean;
}

export interface TradeHistoryMachineOptions {
    readonly symbol?: string;
    readonly period?: HistoryPeriod;
    readonly side?: TradeSideFilter;
    readonly records?: ReadonlyArray<TradeRecord>;
    readonly get_summary_revision?: () => number;
    readonly get_kst_midnight_delay_ms?: () => number;
    readonly on_details_loaded?: (
        summary: TradeHistorySummary,
        request_summary_revision: number,
    ) => void;
}

/**
 * 상세 query와 시작 시점의 summary revision을 한 invoke input으로 고정한다.
 */
interface TradeHistoryRequest {
    readonly query: TradeHistoryQuery;
    readonly summary_revision: number;
    readonly publish_summary: boolean;
}

/**
 * HTTP 결과가 어느 summary revision에서 시작됐는지 actor action까지 보존한다.
 */
interface VersionedTradeHistoryDetails extends TradeHistoryDetails {
    readonly request_summary_revision: number;
    readonly publish_summary: boolean;
}

export type TradeHistoryMachineEvent =
    | { readonly type: 'ENTER_TRADE_HISTORY' }
    | { readonly type: 'LEAVE_TRADE_HISTORY' }
    | { readonly type: 'REFRESH_TRADE_HISTORY' }
    | { readonly type: 'TRADE_HISTORY_RESYNCHRONIZED'; readonly symbol: string }
    | { readonly type: 'INVALIDATE_TRADE_HISTORY_CACHE'; readonly symbol: string }
    | { readonly type: 'ORDER_EXECUTION_RECEIVED' }
    | { readonly type: 'SELECT_DISPLAY_TODAY_HISTORY' }
    | { readonly type: 'SELECT_DISPLAY_WEEKLY_HISTORY' }
    | { readonly type: 'SELECT_DISPLAY_MONTHLY_HISTORY' }
    | { readonly type: 'SELECT_DISPLAY_ALL_HISTORY' }
    | { readonly type: 'ALL_TRADE_HISTORY_SELECTED' }
    | { readonly type: 'BUY_TRADE_HISTORY_SELECTED' }
    | { readonly type: 'SELL_TRADE_HISTORY_SELECTED' }
    | { readonly type: 'DISMISS_TRADE_HISTORY_ERROR' };

/**
 * 함수 이름: milliseconds_until_next_kst_midnight()
 * 기능: timezone database나 local browser timezone에 의존하지 않고 다음 Asia/Seoul 자정까지 계산한다.
 * 인자: now_epoch_ms -> 현재 UTC epoch millisecond
 * 반환값: 다음 KST 00:00까지 남은 양의 millisecond
 * 작성 날짜: 2026/08/23
 */
export function milliseconds_until_next_kst_midnight(
    now_epoch_ms: number = Date.now(),
): number {
    if (!Number.isFinite(now_epoch_ms)) {
        throw new TypeError('now_epoch_ms must be finite');
    }

    // UTC epoch를 KST wall-clock으로 이동한 뒤 다음 UTC calendar day의 시작과 차이를 구한다.
    const kst_now = new Date(now_epoch_ms + KST_UTC_OFFSET_MILLISECONDS);
    const next_kst_midnight_wall_clock = Date.UTC(
        kst_now.getUTCFullYear(),
        kst_now.getUTCMonth(),
        kst_now.getUTCDate() + 1,
    );

    return next_kst_midnight_wall_clock
        - (now_epoch_ms + KST_UTC_OFFSET_MILLISECONDS);
}

/**
 * 함수 이름: create_trade_history_machine()
 * 기능: 거래 내역의 결합 filter query와 조회 중 체결·resync 경쟁을 포함한 실제 조회 상태를 관리한다.
 * 인자: command_port -> 거래 상세 조회 port, options -> 초기 표시값과 검증 완료 summary callback
 * 반환값: trade-history feature의 XState machine
 * 작성 날짜: 2026/08/23
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
            load_trade_history: fromPromise<VersionedTradeHistoryDetails, TradeHistoryRequest>(
                async ({ input, signal }) => {
                    // Invoke가 leave/reenter로 폐기되면 같은 신호로 실제 HTTP read까지 즉시 중단한다.
                    const details = await command_port.load_trade_history(input.query, signal);

                    return {
                        ...details,
                        request_summary_revision: input.summary_revision,
                        publish_summary: input.publish_summary,
                    };
                },
            ),
        },
        delays: {
            refresh_at_next_kst_midnight: () => {
                const delay = options.get_kst_midnight_delay_ms?.()
                    ?? milliseconds_until_next_kst_midnight();
                if (!Number.isFinite(delay) || delay <= 0) {
                    throw new RangeError('KST midnight delay must be positive');
                }

                return delay; // Actor가 ready/empty에 머무는 동안에만 이 timer가 유효하다.
            },
        },
        guards: {
            is_first_entry: ({ context }) => !context.has_entered,
            needs_follow_up_refresh: ({ context }) => context.refresh_pending,
            has_records: ({ event }) => {
                return 'output' in event
                    && event.output !== null
                    && typeof event.output === 'object'
                    && 'records' in event.output
                    && Array.isArray(event.output.records)
                    && event.output.records.length > 0;
            },
        },
        actions: {
            // 최초 상세 진입만 명세의 TODAY + ALL로 초기화하고 이후 재진입은 current query를 유지한다.
            prepare_initial_query: assign({
                period: 'today',
                side: 'all',
                has_entered: true,
                refresh_pending: false,
                should_publish_summary: true,
                error: null,
                request_version: ({ context }) => context.request_version + 1,
            }),
            select_today: assign({
                period: 'today',
                refresh_pending: false,
                should_publish_summary: false,
                request_version: ({ context }) => context.request_version + 1,
            }),
            select_weekly: assign({
                period: 'last7days',
                refresh_pending: false,
                should_publish_summary: false,
                request_version: ({ context }) => context.request_version + 1,
            }),
            select_monthly: assign({
                period: 'last30days',
                refresh_pending: false,
                should_publish_summary: false,
                request_version: ({ context }) => context.request_version + 1,
            }),
            select_all_periods: assign({
                period: 'all',
                refresh_pending: false,
                should_publish_summary: false,
                request_version: ({ context }) => context.request_version + 1,
            }),
            select_all_sides: assign({
                side: 'all',
                refresh_pending: false,
                should_publish_summary: false,
                request_version: ({ context }) => context.request_version + 1,
            }),
            select_buy_side: assign({
                side: 'buy',
                refresh_pending: false,
                should_publish_summary: false,
                request_version: ({ context }) => context.request_version + 1,
            }),
            select_sell_side: assign({
                side: 'sell',
                refresh_pending: false,
                should_publish_summary: false,
                request_version: ({ context }) => context.request_version + 1,
            }),
            mark_refresh: assign({
                request_version: ({ context }) => context.request_version + 1,
                refresh_pending: false,
                should_publish_summary: true,
                error: null,
            }),
            mark_rows_refresh: assign({
                request_version: ({ context }) => context.request_version + 1,
                refresh_pending: false,
                should_publish_summary: false,
                error: null,
            }),
            synchronize_symbol: assign({
                symbol: ({ context, event }) => event.type === 'TRADE_HISTORY_RESYNCHRONIZED'
                    ? event.symbol
                    : context.symbol,
            }),
            queue_follow_up_refresh: assign({
                refresh_pending: true,
            }),
            clear_follow_up_refresh: assign({
                refresh_pending: false,
                request_version: ({ context }) => context.request_version + 1,
            }),
            invalidate_cache: assign({
                symbol: ({ context, event }) => event.type === 'INVALIDATE_TRADE_HISTORY_CACHE'
                    ? event.symbol
                    : context.symbol,
                records: [],
                error: null,
                refresh_pending: false,
                should_publish_summary: false,
                request_version: ({ context }) => context.request_version + 1,
            }),
            store_records: assign({
                records: ({ event }) => {
                    return 'output' in event
                        && event.output !== null
                        && typeof event.output === 'object'
                        && 'records' in event.output
                        && Array.isArray(event.output.records)
                        ? event.output.records as ReadonlyArray<TradeRecord>
                        : [];
                },
                refresh_pending: false,
                should_publish_summary: false,
                error: null,
            }),
            publish_summary: ({ event }) => {
                if ('output' in event
                    && event.output !== null
                    && typeof event.output === 'object'
                    && 'summary' in event.output
                    && 'request_summary_revision' in event.output
                    && 'publish_summary' in event.output
                    && event.output.publish_summary === true
                    && typeof event.output.request_summary_revision === 'number') {
                    // Adapter validation을 통과한 D-12 summary만 별도 summary actor에 공개한다.
                    options.on_details_loaded?.(
                        event.output.summary as TradeHistorySummary,
                        event.output.request_summary_revision,
                    );
                }
            },
            remember_failure: assign({
                refresh_pending: false,
                should_publish_summary: false,
                error: ({ event }) => to_ui_command_failure(
                    'error' in event ? event.error : null,
                    'TRADE_HISTORY_LOAD_FAILED',
                    '거래 내역을 불러오지 못했습니다.',
                ),
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
            symbol: options.symbol ?? 'ETH/KRW',
            period: options.period ?? 'today',
            side: options.side ?? 'all',
            records: options.records ?? [],
            error: null,
            request_version: 0,
            has_entered: false,
            refresh_pending: false,
            should_publish_summary: false,
        },
        on: {
            LEAVE_TRADE_HISTORY: {
                target: '.idle',
            },
            ENTER_TRADE_HISTORY: [
                {
                    guard: 'is_first_entry',
                    target: '.loading',
                    actions: 'prepare_initial_query',
                },
                {
                    target: '.loading',
                    reenter: true,
                    actions: 'mark_refresh',
                },
            ],
            TRADE_HISTORY_RESYNCHRONIZED: {
                target: '.loading',
                reenter: true,
                actions: ['synchronize_symbol', 'mark_rows_refresh'],
            },
            INVALIDATE_TRADE_HISTORY_CACHE: {
                target: '.idle',
                actions: 'invalidate_cache',
            },
        },
        states: {
            idle: {},
            loading: {
                meta: {
                    spec_ids: ['1.1.2', '2.1.2', 'ER-11', 'TD2-01', 'TD3-01'],
                    pending: true,
                },
                on: {
                    ORDER_EXECUTION_RECEIVED: {
                        actions: 'queue_follow_up_refresh',
                    },
                },
                invoke: {
                    id: 'load_trade_history_command',
                    src: 'load_trade_history',
                    input: ({ context }) => ({
                        query: {
                            period: context.period,
                            side: context.side,
                        },
                        summary_revision: options.get_summary_revision?.() ?? 0,
                        publish_summary: context.should_publish_summary,
                    }),
                    onDone: [
                        {
                            guard: 'needs_follow_up_refresh',
                            target: 'loading',
                            reenter: true,
                            // 진행 중 체결 뒤 첫 응답은 적용하지 않고 같은 query를 한 번 더 읽는다.
                            actions: 'clear_follow_up_refresh',
                        },
                        {
                            guard: 'has_records',
                            target: 'ready',
                            actions: ['store_records', 'publish_summary'],
                        },
                        {
                            target: 'empty',
                            actions: ['store_records', 'publish_summary'],
                        },
                    ],
                    onError: [
                        {
                            guard: 'needs_follow_up_refresh',
                            target: 'loading',
                            reenter: true,
                            actions: 'clear_follow_up_refresh',
                        },
                        {
                            target: 'failed',
                            actions: 'remember_failure',
                        },
                    ],
                },
            },
            ready: {
                meta: {
                    spec_ids: ['1.1.3', '2.1.3', 'TD2-01', 'TD2-02', 'TD2-03', 'TD2-04', 'TD2-05', 'TD2-06', 'TD2-07', 'TD2-08', 'TD2-09', 'TD2-10', 'TD2-11', 'TD2-12', 'TD2-13', 'TD3-01', 'TD3-02', 'TD3-03', 'TD3-04', 'TD3-05', 'TD3-06', 'TD3-07', 'CR-10'],
                },
                after: {
                    refresh_at_next_kst_midnight: {
                        target: 'loading',
                        actions: 'mark_refresh',
                    },
                },
                on: {
                    ORDER_EXECUTION_RECEIVED: {
                        target: 'loading',
                        actions: 'mark_rows_refresh',
                    },
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
                    spec_ids: ['1.1.3', '2.1.3', 'VR-10'],
                },
                after: {
                    refresh_at_next_kst_midnight: {
                        target: 'loading',
                        actions: 'mark_refresh',
                    },
                },
                on: {
                    ORDER_EXECUTION_RECEIVED: {
                        target: 'loading',
                        actions: 'mark_rows_refresh',
                    },
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
                    ORDER_EXECUTION_RECEIVED: {
                        target: 'loading',
                        actions: 'mark_rows_refresh',
                    },
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
                    DISMISS_TRADE_HISTORY_ERROR: {
                        target: 'idle',
                        actions: 'clear_error',
                    },
                },
            },
        },
    });
}
