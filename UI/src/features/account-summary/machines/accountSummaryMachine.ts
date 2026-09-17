import { assign, setup } from 'xstate';
import type {
    AssetSummaryViewModel,
    StrategySummaryViewModel,
} from '../types';

export interface AccountSummaryMachineContext {
    readonly strategy: StrategySummaryViewModel;
    readonly asset: AssetSummaryViewModel;
}

export interface AccountSummaryMachineOptions {
    readonly strategy?: StrategySummaryViewModel;
    readonly asset?: AssetSummaryViewModel;
}

export type AccountSummaryMachineEvent =
    | {
        readonly type: 'ACCOUNT_SUMMARY_SYNCHRONIZED';
        readonly strategy: StrategySummaryViewModel;
        readonly asset: AssetSummaryViewModel;
    }
    | {
        readonly type: 'TRADING_STATUS_UPDATED';
        readonly strategy: StrategySummaryViewModel;
    }
    | {
        readonly type: 'ASSET_SUMMARY_UPDATED';
        readonly asset: AssetSummaryViewModel;
    };

const DEFAULT_STRATEGY_SUMMARY: StrategySummaryViewModel = {
    appliedState: 'WAITING',
    profitAmount: '+ ₩ 0',
    profitRate: '0.00%',
    status: '대기 중',
    statusTone: 'neutral',
};

const DEFAULT_ASSET_SUMMARY: AssetSummaryViewModel = {
    ethAmount: '0',
    ethValue: '₩ 0',
    krwValue: '₩ 0',
    profitLoss: '₩ 0',
    totalValue: '₩ 0',
};


/**
 * 함수 이름: create_account_summary_machine()
 * 기능: backend가 계산한 투자 로직 상태와 계좌 자산 snapshot을 두 독립 표시 region에 투영한다.
 * 인자: options -> 최초 전략 및 자산 표시 snapshot
 * 반환값: account-summary feature의 병렬 XState machine
 * 작성 날짜: 2026/08/12
 */
export function create_account_summary_machine(options: AccountSummaryMachineOptions = {}) {
    return setup({
        // 내부 context와 이벤트의 타입 계약을 연결한다.
        types: {
            context: {} as AccountSummaryMachineContext,
            events: {} as AccountSummaryMachineEvent,
        },

        // 상태 데이터 변경과 실행 요청을 Action 정의로 묶는다.
        actions: {
            synchronize_account_summary: assign({
                strategy: ({ context, event }) => {
                    return event.type === 'ACCOUNT_SUMMARY_SYNCHRONIZED'
                        ? event.strategy
                        : context.strategy;
                },
                asset: ({ context, event }) => {
                    return event.type === 'ACCOUNT_SUMMARY_SYNCHRONIZED'
                        ? event.asset
                        : context.asset;
                },
            }),
            update_strategy_summary: assign({
                strategy: ({ context, event }) => {
                    return event.type === 'TRADING_STATUS_UPDATED'
                        ? event.strategy
                        : context.strategy;
                },
            }),
            update_asset_summary: assign({
                asset: ({ context, event }) => {
                    return event.type === 'ASSET_SUMMARY_UPDATED'
                        ? event.asset
                        : context.asset;
                },
            }),
        },
    }).createMachine({
        id: 'accountSummaryMachine',
        type: 'parallel',

        // 외부 작업을 실행하지 않고 기능의 초기 데이터를 구성한다.
        context: {
            strategy: options.strategy ?? DEFAULT_STRATEGY_SUMMARY,
            asset: options.asset ?? DEFAULT_ASSET_SUMMARY,
        },
        on: {
            ACCOUNT_SUMMARY_SYNCHRONIZED: {
                actions: 'synchronize_account_summary',
            },
        },

        // 상태 계층과 이벤트별 전이·복귀 규칙을 정의한다.
        states: {
            trading_logic_status: {
                initial: 'displayed',
                states: {
                    displayed: {
                        meta: { spec_ids: ['DI1-01', 'DI1-02'] },
                        on: {
                            TRADING_STATUS_UPDATED: {
                                actions: 'update_strategy_summary',
                            },
                        },
                    },
                },
            },
            asset_summary: {
                initial: 'displayed',
                states: {
                    displayed: {
                        meta: { spec_ids: ['DI2-01', 'DI2-02'] },
                        on: {
                            ASSET_SUMMARY_UPDATED: {
                                actions: 'update_asset_summary',
                            },
                        },
                    },
                },
            },
        },
    });
}
