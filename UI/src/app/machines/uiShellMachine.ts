import { assign, setup } from 'xstate';

export type UiRoute = 'dashboard' | 'trade_history';

export type UiModalKind =
    | 'start_confirmation'
    | 'select_regime_notice'
    | 'api_connection_required'
    | 'stop_confirmation'
    | 'force_sell_stop_confirmation'
    | 'regime_change_confirmation'
    | 'csv_export'
    | 'csv_export_progress'
    | 'csv_export_complete'
    | 'csv_export_error'
    | 'exit_confirmation'
    | 'force_sell_exit_confirmation'
    | 'exit_processing';

export interface UiShellMachineContext {
    readonly route: UiRoute;
    readonly active_modal: UiModalKind | null;
}

export type UiShellMachineEvent =
    | { readonly type: 'SHOW_ALL_TRADING_DETAILS' }
    | { readonly type: 'BACK_TO_MAIN_SCREEN' }
    | { readonly type: 'OPEN_MODAL'; readonly modal: UiModalKind }
    | { readonly type: 'CLOSE_MODAL'; readonly modal?: UiModalKind }
    | { readonly type: 'REPLACE_MODAL'; readonly modal: UiModalKind };

/**
 * 함수 이름: create_ui_shell_machine()
 * 기능: dashboard와 거래 상세 화면 이동 및 전역 확인 modal 단일 slot을 관리한다.
 * 인자: 없음
 * 반환값: 애플리케이션 shell의 병렬 XState machine
 * 작성 날짜: 2026/08/12
 */
export function create_ui_shell_machine() {
    return setup({
        types: {
            context: {} as UiShellMachineContext,
            events: {} as UiShellMachineEvent,
        },
        guards: {
            is_modal_slot_available: ({ context, event }) => {
                return event.type === 'OPEN_MODAL'
                    && (context.active_modal === null || context.active_modal === event.modal);
            },
            is_requested_modal_active: ({ context, event }) => {
                return event.type === 'CLOSE_MODAL'
                    && (event.modal === undefined || event.modal === context.active_modal);
            },
        },
        actions: {
            show_dashboard: assign({ route: 'dashboard' }),
            show_trade_history: assign({ route: 'trade_history' }),
            open_modal: assign({
                active_modal: ({ context, event }) => {
                    return event.type === 'OPEN_MODAL' || event.type === 'REPLACE_MODAL'
                        ? event.modal
                        : context.active_modal;
                },
            }),
            close_modal: assign({ active_modal: null }),
        },
    }).createMachine({
        id: 'uiShellMachine',
        type: 'parallel',
        context: {
            route: 'dashboard',
            active_modal: null,
        },
        states: {
            navigation: {
                initial: 'dashboard',
                states: {
                    dashboard: {
                        meta: { spec_ids: ['ES2-01', 'ES2-02', 'ER-01', 'ER-02'] },
                        entry: 'show_dashboard',
                        on: {
                            SHOW_ALL_TRADING_DETAILS: {
                                target: 'trade_history',
                            },
                        },
                    },
                    trade_history: {
                        meta: { spec_ids: ['ES2-02', 'ES2-03', 'CR-08', 'CR-17', 'ER-11', 'ER-12'] },
                        entry: 'show_trade_history',
                        on: {
                            BACK_TO_MAIN_SCREEN: {
                                target: 'dashboard_history',
                            },
                        },
                    },
                    dashboard_history: {
                        type: 'history',
                        history: 'deep',
                        target: 'dashboard',
                        meta: { spec_ids: ['ES2-03', 'H*'] },
                    },
                },
            },
            modal_slot: {
                initial: 'empty',
                states: {
                    empty: {
                        on: {
                            OPEN_MODAL: {
                                guard: 'is_modal_slot_available',
                                target: 'occupied',
                                actions: 'open_modal',
                            },
                        },
                    },
                    occupied: {
                        on: {
                            OPEN_MODAL: {
                                guard: 'is_modal_slot_available',
                                actions: 'open_modal',
                            },
                            REPLACE_MODAL: {
                                actions: 'open_modal',
                            },
                            CLOSE_MODAL: {
                                guard: 'is_requested_modal_active',
                                target: 'empty',
                                actions: 'close_modal',
                            },
                        },
                    },
                },
            },
        },
    });
}

