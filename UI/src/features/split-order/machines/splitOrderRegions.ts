import { assign } from 'xstate';

import type { UiRegionComposition } from '../../../app/machines/uiRegionComposition';
import type { UiApplicationContext } from '../../../app/machines/uiApplicationTypes';


/**
 * 함수 이름: split_order_regions()
 * 기능: 공통 저장 요청을 참조하는 매수·매도 입력 Region을 구성한다.
 * 인자: composition -> 루트 상태와 명령을 연결할 조립 객체
 * 반환값: 분할 매수·매도 입력의 병렬 상태 노드
 * 작성 날짜: 2026/09/16
 */
export function split_order_regions(composition: UiRegionComposition) {
    const command = composition.command('split_order', 'split_order.operation', {
        id: 'update_split_order_command',
        src: 'update_split_order',
        input: ({ context }: any) => ({
            order_side: context.pending_side,
            percentage: context.pending_percentage,
        }),
        onDone: {
            actions: 'apply_pending_percentage',
        },
        onError: {
            actions: 'remember_failure',
        },
    });

    /**
     * 함수 이름: create_input_region()
     * 기능: 지정 주문 방향의 ready·saving·failed 입력 상태를 구성한다.
     * 인자: side -> 매수 또는 매도 방향, id -> 입력 상태 ID, spec_ids -> 설계 표 ID
     * 반환값: 주문 방향별 입력 Region 정의
     * 작성 날짜: 2026/09/16
     */
    const create_input_region = (side: string, id: string, spec_ids: string[]) => {
        /**
         * 함수 이름: is_save_pending()
         * 기능: 해당 주문 방향의 최신 저장 요청이 처리 중인지 검사한다.
         * 인자: context -> 분할 설정 데이터, requests -> 루트 공통 요청 목록
         * 반환값: 해당 방향의 요청이 pending이면 true
         * 작성 날짜: 2026/09/16
         */
        const is_save_pending = ({ context, requests }: any) => context.pending_side === side && requests[command.key]?.status === 'pending';

        /**
         * 함수 이름: has_save_failure()
         * 기능: 해당 주문 방향의 저장 오류가 남아 있는지 검사한다.
         * 인자: context -> 분할 설정 데이터
         * 반환값: 해당 방향의 오류가 있으면 true
         * 작성 날짜: 2026/09/16
         */
        const has_save_failure = ({ context }: any) => context.pending_side === side && context.error !== null;

        return {
            initial: id,
            states: {
                [id]: {
                    initial: 'ready',
                    meta: {
                        spec_ids: spec_ids,
                    },
                    states: {
                        ready: {
                            always: [
                                {
                                    guard: is_save_pending,
                                    target: 'saving',
                                },
                                {
                                    guard: has_save_failure,
                                    target: 'failed',
                                },
                            ],
                        },
                        saving: {
                            always: [
                                {
                                    guard: (guard_arguments: any) => !is_save_pending(guard_arguments) && has_save_failure(guard_arguments),
                                    target: 'failed',
                                },
                                {
                                    guard: (guard_arguments: any) => !is_save_pending(guard_arguments),
                                    target: 'ready',
                                },
                            ],
                        },
                        failed: {
                            always: [
                                {
                                    guard: is_save_pending,
                                    target: 'saving',
                                },
                                {
                                    guard: (guard_arguments: any) => !has_save_failure(guard_arguments),
                                    target: 'ready',
                                },
                            ],
                        },
                    },
                },
            },
        };
    };

    const node = composition.compile('split_order', {
        type: 'parallel',
        on: {
            SCALE_IN_LEVEL_CHANGED: {
                actions: 'prepare_scale_in',
            },
            SCALE_OUT_LEVEL_CHANGED: {
                actions: 'prepare_scale_out',
            },
            SPLIT_ORDER_SNAPSHOT_SYNCHRONIZED: {
                actions: 'synchronize_split_order',
            },
            DISMISS_SPLIT_ORDER_ERROR: {
                actions: 'clear_error',
            },
        },
        states: {
            scale_in: create_input_region('scale_in', 'SCALE_IN_ORDER', ['SI-01', 'SI-02']),
            scale_out: create_input_region('scale_out', 'SCALE_OUT_ORDER', ['SO-01', 'SO-02']),
        },
    });

    for (const event of ['SCALE_IN_LEVEL_CHANGED', 'SCALE_OUT_LEVEL_CHANGED']) {
        node.on[`split_order.${event}`][0].actions.push(command.cancel, command.start);
    }

    const settle_request = assign(({ context }: {
        context: UiApplicationContext;
    }) => {
        const requests = {
            ...context.requests,
        };

        delete requests[command.key];

        return {
            requests,
        };
    });

    for (const [event, transitions] of Object.entries(command.on)) {
        node.on[event] = event.includes('.resume.') ? transitions : transitions.map((transition: any) => ({
            ...transition,
            actions: [...transition.actions, settle_request],
        }));
    }

    node.on['split_order.RETRY_SPLIT_ORDER_CHANGE'] = {
        guard: ({ context }: {
            context: UiApplicationContext;
        }) => context.features.split_order.error !== null,
        actions: [...composition.actions('split_order', 'clear_error'), command.cancel, command.start],
    };

    const snapshot_transition = node.on['split_order.SPLIT_ORDER_SNAPSHOT_SYNCHRONIZED'][0];

    snapshot_transition.actions.unshift(command.cancel);

    // 계좌 화면이 비활성 상태일 때에도 같은 무효화 규칙을 적용한다.
    composition.fallback['split_order.SPLIT_ORDER_SNAPSHOT_SYNCHRONIZED']![0].actions.unshift(command.cancel);

    return node;
}
