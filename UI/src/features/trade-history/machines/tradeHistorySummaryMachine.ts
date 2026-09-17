import { assign, setup } from 'xstate';
import type { TradeHistorySummaryViewModel } from '../components/types';

type DailyReturnSummary = TradeHistorySummaryViewModel['dailyReturn'];
type SellPerformanceSummary = TradeHistorySummaryViewModel['sellPerformance'];
type PositionSummary = TradeHistorySummaryViewModel['position'];
type FeeSummary = TradeHistorySummaryViewModel['fees'];

export interface TradeHistorySummaryMachineContext {
    readonly summary: TradeHistorySummaryViewModel;
}

export interface TradeHistorySummaryMachineOptions {
    readonly summary?: TradeHistorySummaryViewModel;
}

export type TradeHistorySummaryMachineEvent =
    | {
        readonly type: 'TRADE_HISTORY_SUMMARY_SYNCHRONIZED';
        readonly summary: TradeHistorySummaryViewModel;
    }
    | { readonly type: 'PROFIT_RATE_UPDATED'; readonly daily_return: DailyReturnSummary }
    | {
        readonly type: 'PERFORMANCE_SNAPSHOT_UPDATED';
        readonly daily_return: DailyReturnSummary;
        readonly sell_performance: SellPerformanceSummary;
        readonly fees: FeeSummary;
    }
    | {
        readonly type: 'SELL_ORDER_EXECUTED';
        readonly sell_performance: SellPerformanceSummary;
        readonly position: PositionSummary;
    }
    | {
        readonly type: 'BUY_ORDER_EXECUTED';
        readonly position: PositionSummary;
    }
    | {
        readonly type: 'HOLDINGS_SNAPSHOT_UPDATED';
        readonly position: PositionSummary;
    }
    | { readonly type: 'DAILY_TRADING_FEE_CHANGED'; readonly fees: FeeSummary };

const DEFAULT_TRADE_HISTORY_SUMMARY: TradeHistorySummaryViewModel = {
    dailyReturn: {
        value: '0.00%',
        tone: 'neutral',
    },
    sellPerformance: {
        winRate: '--%',
        completedCount: '0 / 0',
        averageRealizedReturn: '-',
        totalRealizedPnl: '-',
        tone: 'neutral',
    },
    position: {
        quantity: '0 ETH',
    },
    fees: {
        amount: '₩ 0',
        totalExecutedAmount: '₩ 0',
        averageSlippage: '0.00%',
    },
};


/**
 * 함수 이름: create_trade_history_summary_machine()
 * 기능: 상세 화면의 수익률, 매도 성과, ETH 보유량과 당일 수수료 snapshot을 독립 region으로 투영한다.
 * 인자: options -> 최초 거래 내역 요약 snapshot
 * 반환값: trade-history summary의 병렬 XState machine
 * 작성 날짜: 2026/08/12
 */
export function create_trade_history_summary_machine(
    options: TradeHistorySummaryMachineOptions = {},
) {
    return setup({
        // 내부 context와 이벤트의 타입 계약을 연결한다.
        types: {
            context: {} as TradeHistorySummaryMachineContext,
            events: {} as TradeHistorySummaryMachineEvent,
        },

        // 상태 데이터 변경과 실행 요청을 Action 정의로 묶는다.
        actions: {
            synchronize_summary: assign({
                summary: ({ context, event }) => {
                    return event.type === 'TRADE_HISTORY_SUMMARY_SYNCHRONIZED'
                        ? event.summary
                        : context.summary;
                },
            }),
            update_profit_rate: assign({
                summary: ({ context, event }) => ({
                    ...context.summary,
                    dailyReturn: event.type === 'PROFIT_RATE_UPDATED'
                        ? event.daily_return
                        : context.summary.dailyReturn,
                }),
            }),
            update_performance_snapshot: assign({
                summary: ({ context, event }) => {
                    if (event.type !== 'PERFORMANCE_SNAPSHOT_UPDATED') {
                        return context.summary;
                    }

                    // Performance event에는 Position이 없으므로 기존 position projection을 보존한다.
                    return {
                        ...context.summary,
                        dailyReturn: event.daily_return,
                        sellPerformance: event.sell_performance,
                        fees: event.fees,
                    };
                },
            }),
            update_sell_performance: assign({
                summary: ({ context, event }) => ({
                    ...context.summary,
                    sellPerformance: event.type === 'SELL_ORDER_EXECUTED'
                        ? event.sell_performance
                        : context.summary.sellPerformance,
                }),
            }),
            update_position: assign({
                summary: ({ context, event }) => ({
                    ...context.summary,
                    position: event.type === 'BUY_ORDER_EXECUTED'
                        || event.type === 'SELL_ORDER_EXECUTED'
                        || event.type === 'HOLDINGS_SNAPSHOT_UPDATED'
                        ? event.position
                        : context.summary.position,
                }),
            }),
            update_fees: assign({
                summary: ({ context, event }) => ({
                    ...context.summary,
                    fees: event.type === 'DAILY_TRADING_FEE_CHANGED'
                        ? event.fees
                        : context.summary.fees,
                }),
            }),
        },
    }).createMachine({
        id: 'tradeHistorySummaryMachine',
        type: 'parallel',

        // 외부 작업을 실행하지 않고 기능의 초기 데이터를 구성한다.
        context: {
            summary: options.summary ?? DEFAULT_TRADE_HISTORY_SUMMARY,
        },
        on: {
            TRADE_HISTORY_SUMMARY_SYNCHRONIZED: {
                actions: 'synchronize_summary',
            },
            PERFORMANCE_SNAPSHOT_UPDATED: {
                actions: 'update_performance_snapshot',
            },
            HOLDINGS_SNAPSHOT_UPDATED: {
                actions: 'update_position',
            },
        },

        // 상태 계층과 이벤트별 전이·복귀 규칙을 정의한다.
        states: {
            profit_rate: {
                initial: 'displayed',
                states: {
                    displayed: {
                        meta: { spec_ids: ['D1-01', 'D1-02'] },
                        on: {
                            PROFIT_RATE_UPDATED: { actions: 'update_profit_rate' },
                        },
                    },
                },
            },
            sell_performance: {
                initial: 'displayed',
                states: {
                    displayed: {
                        meta: { spec_ids: ['D2-01', 'D2-02'] },
                        on: {
                            SELL_ORDER_EXECUTED: { actions: 'update_sell_performance' },
                        },
                    },
                },
            },
            eth_holdings: {
                initial: 'displayed',
                states: {
                    displayed: {
                        meta: { spec_ids: ['D3-01', 'D3-02', 'D3-03'] },
                        on: {
                            BUY_ORDER_EXECUTED: { actions: 'update_position' },
                            SELL_ORDER_EXECUTED: { actions: 'update_position' },
                        },
                    },
                },
            },
            daily_trading_fee: {
                initial: 'displayed',
                states: {
                    displayed: {
                        meta: { spec_ids: ['D4-01', 'D4-02'] },
                        on: {
                            DAILY_TRADING_FEE_CHANGED: { actions: 'update_fees' },
                        },
                    },
                },
            },
        },
    });
}
