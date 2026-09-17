import { assign, setup } from 'xstate';
import type { BackendTradingIndicatorSnapshot, RegimeMetric, TradeRecord } from '../../../shared/contracts';

export interface RecentOrdersMachineContext {
    readonly trades: ReadonlyArray<TradeRecord>;
    readonly strategy_indicators: BackendTradingIndicatorSnapshot | null;
    readonly strategy_indicators_received_at: number | null;
    readonly realtime_indicators: ReadonlyArray<RegimeMetric>;
}

export interface RecentOrdersMachineOptions {
    readonly initial_monotonic_ms?: number;
    readonly trades?: ReadonlyArray<TradeRecord>;
    readonly strategy_indicators?: BackendTradingIndicatorSnapshot | null;
    readonly realtime_indicators?: ReadonlyArray<RegimeMetric>;
}

export type RecentOrdersMachineEvent =
    | { readonly type: 'STRATEGY_INDICATORS_SYNCHRONIZED'; readonly received_at?: number; readonly indicators: BackendTradingIndicatorSnapshot | null }
    | {
        readonly type: 'RECENT_ORDERS_SNAPSHOT_SYNCHRONIZED';
        readonly trades: ReadonlyArray<TradeRecord>;
        readonly indicators: ReadonlyArray<RegimeMetric>;
    }
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
        // 내부 context와 이벤트의 타입 계약을 연결한다.
        types: {
            context: {} as RecentOrdersMachineContext,
            events: {} as RecentOrdersMachineEvent,
        },

        // 상태 데이터 변경과 실행 요청을 Action 정의로 묶는다.
        actions: {
            // REGIME 수치와 독립된 상태를 유지해 거래 단계와 지표를 함께 교체한다.
            synchronize_strategy_indicators: assign(({ context, event }) => {
                if (event.type !== 'STRATEGY_INDICATORS_SYNCHRONIZED') return {};

                // 동일 payload의 재전송은 최초 수신 기준점을 보존해 남은 시간을 늘리지 않는다.
                const duplicate = JSON.stringify(context.strategy_indicators) === JSON.stringify(event.indicators);

                return {
                    strategy_indicators: event.indicators,
                    strategy_indicators_received_at: duplicate ? context.strategy_indicators_received_at : event.received_at ?? 0,
                };  // 이 시각은 화면 전용이며 서버의 전략 상태나 경과 시간에 전달하지 않는다.
            }),
            synchronize_recent_orders: assign({
                trades: ({ context, event }) => {
                    return event.type === 'RECENT_ORDERS_SNAPSHOT_SYNCHRONIZED'
                        ? event.trades
                        : context.trades;
                },
                realtime_indicators: ({ context, event }) => {
                    return event.type === 'RECENT_ORDERS_SNAPSHOT_SYNCHRONIZED'
                        ? event.indicators
                        : context.realtime_indicators;
                },
            }),
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

        // 외부 작업을 실행하지 않고 기능의 초기 데이터를 구성한다.
        context: {
            trades: options.trades ?? [],
            strategy_indicators: options.strategy_indicators ?? null,  // 구버전 연결은 예시값 대신 대기한다.
            strategy_indicators_received_at: options.strategy_indicators ? options.initial_monotonic_ms ?? 0 : null,
            realtime_indicators: options.realtime_indicators ?? [],
        },
        on: {
            STRATEGY_INDICATORS_SYNCHRONIZED: { actions: 'synchronize_strategy_indicators' },
            RECENT_ORDERS_SNAPSHOT_SYNCHRONIZED: {
                actions: 'synchronize_recent_orders',
            },
            REALTIME_INDICATOR_UPDATED: {
                actions: 'update_realtime_indicators',
            },
            TRADING_STATUS_UPDATED: {
                actions: 'update_realtime_indicators',
            },
        },

        // 상태 계층과 이벤트별 전이·복귀 규칙을 정의한다.
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
