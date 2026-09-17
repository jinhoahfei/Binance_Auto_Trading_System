import { assign, setup } from 'xstate';
import type {
    RegimeMetric,
    RegimeType,
    UiCommandFailure,
} from '../../../shared/contracts';
import { to_ui_command_failure } from '../../../shared/errors';

export interface RegimeMachineContext {
    readonly recommended_regime: RegimeType | null;
    readonly applied_regime: RegimeType | null;
    readonly candidate_regime: RegimeType | null;
    readonly metrics: ReadonlyArray<RegimeMetric>;
    readonly is_highlighted: boolean;
    readonly error: UiCommandFailure | null;
}

export interface RegimeMachineOptions {
    readonly recommended_regime?: RegimeType | null;
    readonly applied_regime?: RegimeType | null;
    readonly metrics?: ReadonlyArray<RegimeMetric>;
}

export type RegimeMachineEvent =
    | {
        readonly type: 'REGIME_SNAPSHOT_SYNCHRONIZED';
        readonly recommended_regime: RegimeType | null;
        readonly applied_regime: RegimeType | null;
        readonly metrics: ReadonlyArray<RegimeMetric>;
    }
    | {
        readonly type: 'REGIME_SNAPSHOT_CONTEXT_SYNCHRONIZED';
        readonly recommended_regime: RegimeType | null;
        readonly applied_regime: RegimeType | null;
        readonly metrics: ReadonlyArray<RegimeMetric>;
    }
    | { readonly type: 'TYPE_RECOMMENDED'; readonly regime: RegimeType }
    | { readonly type: 'TYPE_CLICKED'; readonly regime: RegimeType }
    | { readonly type: 'CONFIRM_TYPE_CHANGE' }
    | { readonly type: 'CANCEL_TYPE_CHANGE' }
    | { readonly type: 'REGIME_INDICATOR_UPDATED'; readonly metrics: ReadonlyArray<RegimeMetric> }
    | { readonly type: 'HIGHLIGHT_REQUESTED' }
    | { readonly type: 'HIGHLIGHT_COMPLETED' }
    | { readonly type: 'REGIME_APPLIED'; readonly regime: RegimeType };

/**
 * 함수 이름: to_regime_failure()
 * 기능: REGIME adapter 오류를 화면 표시용 실패 객체로 변환한다.
 * 인자: error -> adapter 또는 actor가 반환한 오류
 * 반환값: UI 명령 실패 객체
 * 작성 날짜: 2026/08/12
 */
function to_regime_failure(error: unknown): UiCommandFailure {
    return to_ui_command_failure(
        error,
        'REGIME_APPLY_FAILED',
        'REGIME을 적용하지 못했습니다.',
    );
}

/**
 * 함수 이름: create_regime_machine()
 * 기능: 추천 REGIME, 확인 전 후보, 적용 REGIME, 패널 점멸과 비동기 적용 상태를 관리한다.
 * 인자: options -> 초기 추천·적용 REGIME과 지표 값
 * 반환값: regime-selection feature의 XState machine
 * 작성 날짜: 2026/08/12
 */
export function create_regime_machine(
    options: RegimeMachineOptions = {},
) {
    return setup({
        types: {
            context: {} as RegimeMachineContext,
            events: {} as RegimeMachineEvent,
        },
        actions: {
            synchronize_regime_snapshot: assign({
                recommended_regime: ({ context, event }) => {
                    return event.type === 'REGIME_SNAPSHOT_SYNCHRONIZED'
                        || event.type === 'REGIME_SNAPSHOT_CONTEXT_SYNCHRONIZED'
                        ? event.recommended_regime
                        : context.recommended_regime;
                },
                applied_regime: ({ context, event }) => {
                    return event.type === 'REGIME_SNAPSHOT_SYNCHRONIZED'
                        || event.type === 'REGIME_SNAPSHOT_CONTEXT_SYNCHRONIZED'
                        ? event.applied_regime
                        : context.applied_regime;
                },
                metrics: ({ context, event }) => {
                    return event.type === 'REGIME_SNAPSHOT_SYNCHRONIZED'
                        || event.type === 'REGIME_SNAPSHOT_CONTEXT_SYNCHRONIZED'
                        ? event.metrics
                        : context.metrics;
                },
                candidate_regime: ({ context, event }) => {
                    return event.type === 'REGIME_SNAPSHOT_SYNCHRONIZED'
                        ? null
                        : context.candidate_regime;
                },
                is_highlighted: ({ context, event }) => {
                    return event.type === 'REGIME_SNAPSHOT_SYNCHRONIZED'
                        ? false
                        : context.is_highlighted;
                },
                error: ({ context, event }) => {
                    return event.type === 'REGIME_SNAPSHOT_SYNCHRONIZED'
                        ? null
                        : context.error;
                },
            }),
            remember_recommendation: assign({
                recommended_regime: ({ event, context }) => {
                    return event.type === 'TYPE_RECOMMENDED'
                        ? event.regime
                        : context.recommended_regime;
                },
            }),
            remember_candidate: assign({
                candidate_regime: ({ event, context }) => {
                    return event.type === 'TYPE_CLICKED'
                        ? event.regime
                        : context.candidate_regime;
                },
                is_highlighted: false,
                error: null,
            }),
            discard_candidate: assign({
                candidate_regime: null,
                error: null,
            }),
            apply_candidate: assign({
                applied_regime: ({ context }) => context.candidate_regime,
                candidate_regime: null,
                error: null,
            }),
            synchronize_applied_regime: assign({
                applied_regime: ({ event, context }) => {
                    return event.type === 'REGIME_APPLIED'
                        ? event.regime
                        : context.applied_regime;
                },
                candidate_regime: null,
                error: null,
            }),
            update_metrics: assign({
                metrics: ({ event, context }) => {
                    return event.type === 'REGIME_INDICATOR_UPDATED'
                        ? event.metrics
                        : context.metrics;
                },
            }),
            enable_highlight: assign({
                is_highlighted: true,
            }),
            disable_highlight: assign({
                is_highlighted: false,
            }),
            remember_failure: assign({
                error: ({ event }) => {
                    return 'error' in event
                        ? to_regime_failure(event.error)
                        : null;
                },
            }),
        },
    }).createMachine({
        id: 'regimeMachine',
        initial: 'type_selection',
        context: {
            recommended_regime: options.recommended_regime ?? null,
            applied_regime: options.applied_regime ?? null,
            candidate_regime: null,
            metrics: options.metrics ?? [],
            is_highlighted: false,
            error: null,
        },
        on: {
            REGIME_SNAPSHOT_SYNCHRONIZED: {
                target: '.type_selection',
                actions: 'synchronize_regime_snapshot',
            },
            REGIME_SNAPSHOT_CONTEXT_SYNCHRONIZED: {
                actions: 'synchronize_regime_snapshot',
            },
            TYPE_RECOMMENDED: {
                actions: 'remember_recommendation',
            },
            REGIME_INDICATOR_UPDATED: {
                actions: 'update_metrics',
            },
            REGIME_APPLIED: {
                target: '.type_selection',
                actions: 'synchronize_applied_regime',
            },
        },
        states: {
            type_selection: {
                meta: {
                    spec_ids: ['R1-01', 'R1-02', 'R2-01', 'R2-03', 'R3-01', 'R3-02'],
                },
                on: {
                    TYPE_CLICKED: {
                        target: 'type_change_confirmation',
                        actions: 'remember_candidate',
                    },
                    HIGHLIGHT_REQUESTED: {
                        target: 'highlighting',
                        actions: 'enable_highlight',
                    },
                },
            },
            highlighting: {
                meta: {
                    spec_ids: ['CR-19', 'ER-06C'],
                    timers: {
                        4000: {
                            target: 'type_selection',
                            actions: 'disable_highlight',
                        },
                    },
                },

                on: {
                    TYPE_CLICKED: {
                        target: 'type_change_confirmation',
                        actions: 'remember_candidate',
                    },
                    HIGHLIGHT_COMPLETED: {
                        target: 'type_selection',
                        actions: 'disable_highlight',
                    },
                },
            },
            type_change_confirmation: {
                meta: {
                    spec_ids: ['R2-02', 'R2-04', 'R2-05', 'CR-03', 'VR-05', 'VR-14', 'ER-10A'],
                },
                on: {
                    CONFIRM_TYPE_CHANGE: {
                        target: 'applying',
                    },
                    CANCEL_TYPE_CHANGE: {
                        target: 'type_selection',
                        actions: 'discard_candidate',
                    },
                },
            },
            applying: {
                meta: {
                    spec_ids: ['R2-04', 'ER-10B'],
                    pending: true,
                    command: {
                        id: 'apply_regime_command',
                        src: 'apply_regime',
                        input: ({ context }: { context: RegimeMachineContext }) => context.candidate_regime as RegimeType,
                        onDone: {
                            target: 'type_selection',
                            actions: 'apply_candidate',
                        },
                        onError: {
                            target: 'type_change_confirmation',
                            actions: 'remember_failure',
                        },
                    },
                },
            },
        },
    });
}
