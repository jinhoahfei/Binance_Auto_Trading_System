import { or, stateIn } from 'xstate';

import type { RegionNode } from '../../../app/machines/uiRegionComposition';


/**
 * 함수 이름: history_filter_region()
 * 기능: 기간 또는 거래 종류 선택을 조회 상태와 연동되는 실제 상태로 구성한다.
 * 인자: kind -> period 또는 side
 * 반환값: 선택값 복원과 조회 가능 조건을 가진 필터 Region 정의
 * 작성 날짜: 2026/09/16
 */
export function history_filter_region(kind: 'period' | 'side'): RegionNode {
    const values = kind === 'period' ? {
        today: 'SELECT_DISPLAY_TODAY_HISTORY',
        last7days: 'SELECT_DISPLAY_WEEKLY_HISTORY',
        last30days: 'SELECT_DISPLAY_MONTHLY_HISTORY',
        all: 'SELECT_DISPLAY_ALL_HISTORY',
    } : {
        all: 'ALL_TRADE_HISTORY_SELECTED',
        buy: 'BUY_TRADE_HISTORY_SELECTED',
        sell: 'SELL_TRADE_HISTORY_SELECTED',
    };
    const can_select_filter = or(['ready', 'empty', 'failed'].map(state => stateIn(`#trade_history.${state}`)));

    return {
        initial: 'restore',
        on: Object.fromEntries(Object.entries(values).map(([value, type]) => [
            type,
            {
                guard: can_select_filter,
                target: `.${value}`,
            },
        ])),
        states: {
            restore: {
                always: Object.keys(values).map(value => ({
                    guard: ({ context }: any) => context[kind] === value,
                    target: value,
                })),
            },
            ...Object.fromEntries(Object.keys(values).map(value => [
                value,
                {
                    meta: {
                        filter: kind,
                        selection: value,
                    },
                },
            ])),
        },
    };
}
