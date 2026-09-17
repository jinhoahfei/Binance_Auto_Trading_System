import { assign, setup } from 'xstate';
import type { UiCommandFailure } from '../../../shared/contracts';
import { to_ui_command_failure } from '../../../shared/errors';

export interface SplitOrderMachineContext {
    readonly scale_in_percentage: number;
    readonly scale_out_percentage: number;
    readonly pending_side: 'scale_in' | 'scale_out' | null;
    readonly pending_percentage: number | null;
    readonly error: UiCommandFailure | null;
}

export interface SplitOrderMachineOptions {
    readonly scale_in_percentage?: number;
    readonly scale_out_percentage?: number;
}

export type SplitOrderMachineEvent =
    | {
        readonly type: 'SPLIT_ORDER_SNAPSHOT_SYNCHRONIZED';
        readonly scale_in_percentage: number;
        readonly scale_out_percentage: number;
    }
    | { readonly type: 'SCALE_IN_LEVEL_CHANGED'; readonly percentage: number }
    | { readonly type: 'SCALE_OUT_LEVEL_CHANGED'; readonly percentage: number }
    | { readonly type: 'RETRY_SPLIT_ORDER_CHANGE' }
    | { readonly type: 'DISMISS_SPLIT_ORDER_ERROR' };

/**
 * 함수 이름: clamp_percentage()
 * 기능: slider에서 전달된 비율을 0부터 100 사이의 정수로 정규화한다.
 * 인자: percentage -> 사용자가 요청한 비율
 * 반환값: 정규화된 정수 비율
 * 작성 날짜: 2026/08/12
 */
function clamp_percentage(percentage: number): number {
    return Math.min(100, Math.max(0, Math.round(percentage)));
}

/**
 * 함수 이름: create_split_order_machine()
 * 기능: 분할 매수·매도 비율 변경을 adapter 승인 전후 상태로 분리하고 중복 변경을 방지한다.
 * 인자: options -> 초기 매수·매도 비율
 * 반환값: split-order feature의 XState machine
 * 작성 날짜: 2026/08/12
 */
export function create_split_order_machine(
    options: SplitOrderMachineOptions = {},
) {
    return setup({
        types: {
            context: {} as SplitOrderMachineContext,
            events: {} as SplitOrderMachineEvent,
        },
        actions: {
            synchronize_split_order: assign({
                scale_in_percentage: ({ context, event }) => {
                    return event.type === 'SPLIT_ORDER_SNAPSHOT_SYNCHRONIZED'
                        ? clamp_percentage(event.scale_in_percentage)
                        : context.scale_in_percentage;
                },
                scale_out_percentage: ({ context, event }) => {
                    return event.type === 'SPLIT_ORDER_SNAPSHOT_SYNCHRONIZED'
                        ? clamp_percentage(event.scale_out_percentage)
                        : context.scale_out_percentage;
                },
                pending_side: null,
                pending_percentage: null,
                error: null,
            }),
            prepare_scale_in: assign({
                scale_in_percentage: ({ event, context }) => {
                    return event.type === 'SCALE_IN_LEVEL_CHANGED'
                        ? clamp_percentage(event.percentage)
                        : context.scale_in_percentage;
                },
                pending_side: 'scale_in',
                pending_percentage: ({ event }) => {
                    return event.type === 'SCALE_IN_LEVEL_CHANGED'
                        ? clamp_percentage(event.percentage)
                        : null;
                },
                error: null,
            }),
            prepare_scale_out: assign({
                scale_out_percentage: ({ event, context }) => {
                    return event.type === 'SCALE_OUT_LEVEL_CHANGED'
                        ? clamp_percentage(event.percentage)
                        : context.scale_out_percentage;
                },
                pending_side: 'scale_out',
                pending_percentage: ({ event }) => {
                    return event.type === 'SCALE_OUT_LEVEL_CHANGED'
                        ? clamp_percentage(event.percentage)
                        : null;
                },
                error: null,
            }),
            apply_pending_percentage: assign({
                scale_in_percentage: ({ context }) => {
                    return context.pending_side === 'scale_in'
                        ? context.pending_percentage ?? context.scale_in_percentage
                        : context.scale_in_percentage;
                },
                scale_out_percentage: ({ context }) => {
                    return context.pending_side === 'scale_out'
                        ? context.pending_percentage ?? context.scale_out_percentage
                        : context.scale_out_percentage;
                },
                pending_side: null,
                pending_percentage: null,
                error: null,
            }),
            remember_failure: assign({
                error: ({ event }) => to_ui_command_failure(
                    'error' in event ? event.error : null,
                    'SPLIT_ORDER_UPDATE_FAILED',
                    '분할 주문 비율을 변경하지 못했습니다.',
                ),
            }),
            clear_error: assign({ error: null }),
        },
    }).createMachine({
        id: 'splitOrderMachine',
        initial: 'ready',
        context: {
            scale_in_percentage: options.scale_in_percentage ?? 50,
            scale_out_percentage: options.scale_out_percentage ?? 50,
            pending_side: null,
            pending_percentage: null,
            error: null,
        },
        on: {
            SPLIT_ORDER_SNAPSHOT_SYNCHRONIZED: {
                target: '.ready',
                actions: 'synchronize_split_order',
            },
        },
        states: {
            ready: {
                meta: {
                    spec_ids: ['SI-01', 'SI-02', 'SO-01', 'SO-02', 'CR-09'],
                },
                on: {
                    SCALE_IN_LEVEL_CHANGED: {
                        target: 'saving',
                        actions: 'prepare_scale_in',
                    },
                    SCALE_OUT_LEVEL_CHANGED: {
                        target: 'saving',
                        actions: 'prepare_scale_out',
                    },
                },
            },
            saving: {
                meta: {
                    spec_ids: ['SI-02', 'SO-02'],
                    pending: true,
                    command: {
                        id: 'update_split_order_command',
                        src: 'update_split_order',
                        input: ({ context }: { context: SplitOrderMachineContext }) => ({
                            order_side: context.pending_side as 'scale_in' | 'scale_out',
                            percentage: context.pending_percentage as number,
                        }),
                        onDone: {
                            target: 'ready',
                            actions: 'apply_pending_percentage',
                        },
                        onError: {
                            target: 'failed',
                            actions: 'remember_failure',
                        },
                    },
                },

                on: {
                    SCALE_IN_LEVEL_CHANGED: {
                        target: 'saving',
                        reenter: true,
                        actions: 'prepare_scale_in',
                    },
                    SCALE_OUT_LEVEL_CHANGED: {
                        target: 'saving',
                        reenter: true,
                        actions: 'prepare_scale_out',
                    },
                },
            },
            failed: {
                meta: {
                    spec_ids: ['SI-02', 'SO-02'],
                },
                on: {
                    SCALE_IN_LEVEL_CHANGED: {
                        target: 'saving',
                        actions: 'prepare_scale_in',
                    },
                    SCALE_OUT_LEVEL_CHANGED: {
                        target: 'saving',
                        actions: 'prepare_scale_out',
                    },
                    RETRY_SPLIT_ORDER_CHANGE: {
                        target: 'saving',
                    },
                    DISMISS_SPLIT_ORDER_ERROR: {
                        target: 'ready',
                        actions: 'clear_error',
                    },
                },
            },
        },
    });
}
