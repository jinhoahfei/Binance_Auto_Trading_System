import { assign, setup } from 'xstate';
import type { RegimeMetric, TradeRecord } from '../../../shared/contracts';

export interface RecentOrdersMachineContext {
    readonly trades: ReadonlyArray<TradeRecord>;
    readonly realtime_indicators: ReadonlyArray<RegimeMetric>;
}

export interface RecentOrdersMachineOptions {
    readonly trades?: ReadonlyArray<TradeRecord>;
    readonly realtime_indicators?: ReadonlyArray<RegimeMetric>;
}

export type RecentOrdersMachineEvent =
    | { readonly type: 'BUY_ORDER_EXECUTED'; readonly trade: TradeRecord }
    | { readonly type: 'SELL_ORDER_EXECUTED'; readonly trade: TradeRecord }
    | { readonly type: 'REALTIME_INDICATOR_CLICKED' }
    | { readonly type: 'TRADING_HISTORY_CLICKED' }
    | {
        readonly type: 'REALTIME_INDICATOR_UPDATED';
        readonly indicators: ReadonlyArray<RegimeMetric>;
    }
    | {
        readonly type: 'TRADING_STATUS_UPDATED';
        readonly indicators: ReadonlyArray<RegimeMetric>;
    };

/**
 * 함수 이름: create_recent_orders_machine()
 * 기능: 최근 체결과 실시간 지표 탭 전환 및 실시간 목록 갱신 상태를 생성한다.
 * 인자: options -> 초기 체결 내역과 실시간 지표 fixture
 * 반환값: recent-orders feature의 XState machine
 * 작성 날짜: 2026/08/12
 */
export function create_recent_orders_machine(options: RecentOrdersMachineOptions = {}) {
    return setup({
        types: {
            context: {} as RecentOrdersMachineContext,
            events: {} as RecentOrdersMachineEvent,
        },
        actions: {
            prepend_trade: assign({
                trades: ({ context, event }) => {
                    return event.type === 'BUY_ORDER_EXECUTED'
                        || event.type === 'SELL_ORDER_EXECUTED'
                        ? [event.trade, ...context.trades]
                        : context.trades;
                },
            }),
            update_realtime_indicators: assign({
                realtime_indicators: ({ context, event }) => {
                    return event.type === 'REALTIME_INDICATOR_UPDATED'
                        || event.type === 'TRADING_STATUS_UPDATED'
                        ? event.indicators
                        : context.realtime_indicators;
                },
            }),
        },
    }).createMachine({
        id: 'recentOrdersMachine',
        initial: 'trade_history_displayed',
        context: {
            trades: options.trades ?? [],
            realtime_indicators: options.realtime_indicators ?? [],
        },
        states: {
            trade_history_displayed: {
                meta: {
                    spec_ids: ['M4-01', 'M4-02', 'M4-03', 'M4-04', 'CR-07'],
                },
                on: {
                    BUY_ORDER_EXECUTED: {
                        actions: 'prepend_trade',
                    },
                    SELL_ORDER_EXECUTED: {
                        actions: 'prepend_trade',
                    },
                    REALTIME_INDICATOR_CLICKED: {
                        target: 'realtime_indicator_displayed',
                    },
                },
            },
            realtime_indicator_displayed: {
                meta: {
                    spec_ids: ['M4-04', 'M4-05', 'M4-06', 'M4-07', 'M4-08', 'M4-09', 'CR-07'],
                },
                on: {
                    BUY_ORDER_EXECUTED: {
                        actions: 'prepend_trade',
                    },
                    SELL_ORDER_EXECUTED: {
                        actions: 'prepend_trade',
                    },
                    REALTIME_INDICATOR_UPDATED: {
                        actions: 'update_realtime_indicators',
                    },
                    TRADING_STATUS_UPDATED: {
                        actions: 'update_realtime_indicators',
                    },
                    TRADING_HISTORY_CLICKED: {
                        target: 'trade_history_displayed',
                    },
                },
            },
        },
    });
}

