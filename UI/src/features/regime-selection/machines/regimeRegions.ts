import type { AnyStateMachine } from 'xstate';

import type { RegionNode } from '../../../app/machines/uiRegionComposition';


/**
 * 함수 이름: regime_regions()
 * 기능: REGIME 추천·선택·지표를 세 개의 병렬 Region으로 구성한다.
 * 인자: definition -> REGIME 상태·Action·작업 machine 정의
 * 반환값: 루트에 포함할 REGIME 병렬 상태 정의
 * 작성 날짜: 2026/09/16
 */
export function regime_regions(definition: AnyStateMachine): RegionNode {
    const { context: _context, initial, states, on } = definition.config as any;
    const {
        TYPE_RECOMMENDED: recommendation_transition,
        REGIME_INDICATOR_UPDATED: indicator_transition,
        ...selection_events
    } = on;
    const selection_states = {
        ...states,
        type_selection: {
            ...states.type_selection,
            meta: {
                spec_ids: ['R2-01', 'R2-03'],
            },
        },
    };

    return {
        type: 'parallel',
        states: {
            recommendation: {
                initial: 'RECOMMENDED_TYPE_DISPLAYED',
                states: {
                    RECOMMENDED_TYPE_DISPLAYED: {
                        meta: {
                            spec_ids: ['R1-01', 'R1-02'],
                        },
                        on: {
                            TYPE_RECOMMENDED: recommendation_transition,
                        },
                    },
                },
            },
            selection: {
                initial,
                states: selection_states,
                on: selection_events,
            },
            indicators: {
                initial: 'DISPLAY_TYPE_INDICATOR',
                states: {
                    DISPLAY_TYPE_INDICATOR: {
                        meta: {
                            spec_ids: ['R3-01', 'R3-02'],
                        },
                        on: {
                            REGIME_INDICATOR_UPDATED: indicator_transition,
                        },
                    },
                },
            },
        },
    };
}
