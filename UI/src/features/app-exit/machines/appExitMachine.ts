import { assign, fromPromise, setup } from 'xstate';
import type { UiCommandFailure } from '../../../shared/contracts';
import { to_ui_command_failure } from '../../../shared/errors';
import type { UiCommandPort } from '../../../shared/ports';

export interface AppExitMachineContext {
    readonly had_open_position: boolean;
    readonly error: UiCommandFailure | null;
}

export type AppExitMachineEvent =
    | { readonly type: 'EXIT_CLICKED'; readonly has_open_position: boolean }
    | { readonly type: 'FORCE_SELL_EXIT_CANCELED' }
    | { readonly type: 'FORCE_SELL_EXIT_CONFIRMED' }
    | { readonly type: 'EXIT_CANCELED' }
    | { readonly type: 'EXIT_CONFIRMED' };

/**
 * 함수 이름: create_app_exit_machine()
 * 기능: OS 종료 요청을 포지션 유무별 확인, 강제 매도, 저장·연결 종료와 최종 상태로 조정한다.
 * 인자: command_port -> 강제 매도와 애플리케이션 종료 명령 port
 * 반환값: app-exit feature의 XState machine
 * 작성 날짜: 2026/08/12
 */
export function create_app_exit_machine(command_port: UiCommandPort) {
    return setup({
        types: {
            context: {} as AppExitMachineContext,
            events: {} as AppExitMachineEvent,
        },
        actors: {
            force_sell: fromPromise<void>(async () => {
                await command_port.force_sell_and_stop();
            }),
            shutdown_application: fromPromise<void>(async () => {
                await command_port.shutdown_application();
            }),
        },
        guards: {
            has_open_position: ({ event }) => {
                return event.type === 'EXIT_CLICKED' && event.has_open_position;
            },
        },
        actions: {
            remember_position: assign({
                had_open_position: ({ event }) => {
                    return event.type === 'EXIT_CLICKED'
                        ? event.has_open_position
                        : false;
                },
                error: null,
            }),
            remember_force_sell_failure: assign({
                error: ({ event }) => to_ui_command_failure(
                    'error' in event ? event.error : null,
                    'EXIT_FORCE_SELL_FAILED',
                    '포지션 강제 매도에 실패해 프로그램 종료를 취소했습니다.',
                ),
            }),
            remember_shutdown_failure: assign({
                error: ({ event }) => to_ui_command_failure(
                    'error' in event ? event.error : null,
                    'APPLICATION_SHUTDOWN_FAILED',
                    '프로그램 종료 준비를 완료하지 못했습니다.',
                ),
            }),
        },
    }).createMachine({
        id: 'appExitMachine',
        initial: 'awaiting_exit',
        context: {
            had_open_position: false,
            error: null,
        },
        states: {
            awaiting_exit: {
                meta: { spec_ids: ['ES3-01', 'ES3-02', 'ES3-03'] },
                on: {
                    EXIT_CLICKED: [
                        {
                            guard: 'has_open_position',
                            target: 'force_sell_exit_confirmation',
                            actions: 'remember_position',
                        },
                        {
                            target: 'exit_confirmation',
                            actions: 'remember_position',
                        },
                    ],
                },
            },
            force_sell_exit_confirmation: {
                meta: { spec_ids: ['ES3-02', 'ES3-04', 'ES3-05', 'ES3-07'] },
                on: {
                    FORCE_SELL_EXIT_CANCELED: {
                        target: 'awaiting_exit',
                    },
                    FORCE_SELL_EXIT_CONFIRMED: {
                        target: 'force_selling',
                    },
                },
            },
            force_selling: {
                meta: {
                    spec_ids: ['ES3-05', 'ES3-06', 'ES3-07'],
                    pending: true,
                },
                invoke: {
                    id: 'exit_force_sell_command',
                    src: 'force_sell',
                    onDone: {
                        target: 'shutting_down',
                    },
                    onError: {
                        target: 'force_sell_exit_confirmation',
                        actions: 'remember_force_sell_failure',
                    },
                },
            },
            exit_confirmation: {
                meta: { spec_ids: ['ES3-03', 'ES3-08', 'ES3-09'] },
                on: {
                    EXIT_CANCELED: {
                        target: 'awaiting_exit',
                    },
                    EXIT_CONFIRMED: {
                        target: 'shutting_down',
                    },
                },
            },
            shutting_down: {
                meta: {
                    spec_ids: ['ES3-06', 'ES3-09'],
                    pending: true,
                },
                invoke: {
                    id: 'shutdown_application_command',
                    src: 'shutdown_application',
                    onDone: {
                        target: 'ui_final_state',
                    },
                    onError: {
                        target: 'awaiting_exit',
                        actions: 'remember_shutdown_failure',
                    },
                },
            },
            ui_final_state: {
                type: 'final',
                meta: { spec_ids: ['UI_FINAL_STATE'] },
            },
        },
    });
}
