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
    | { readonly type: 'EXIT_CONFIRMED' }
    | { readonly type: 'SIDECAR_EXITED' }
    | { readonly type: 'SIDECAR_EXITED_ABNORMALLY' };

/**
 * 함수 이름: has_failure_code()
 * 기능: invoked command의 unknown 오류에서 공개 가능한 typed failure code 하나만 비교한다.
 * 인자: error -> XState onError가 전달한 unknown 오류, expected_code -> 비교할 오류 code
 * 반환값: exact code가 일치하면 true
 * 작성 날짜: 2026/08/24
 */
function has_failure_code(error: unknown, expected_code: string): boolean {
    return typeof error === 'object'
        && error !== null
        && 'code' in error
        && error.code === expected_code;
}

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
            had_open_position: ({ context }) => context.had_open_position,
            sidecar_exit_wait_timed_out: ({ event }) => {
                return 'error' in event
                    && has_failure_code(event.error, 'SIDECAR_EXIT_TIMEOUT');
            },
            sidecar_exit_failed: ({ event }) => {
                return 'error' in event
                    && has_failure_code(event.error, 'SIDECAR_ABNORMAL_EXIT');
            },
            shutdown_outcome_is_ambiguous: ({ event }) => {
                return 'error' in event
                    && has_failure_code(event.error, 'SHUTDOWN_OUTCOME_AMBIGUOUS');
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
            remember_force_sell_success: assign({
                // 강제 매도 command가 성공하면 이후 종료 재시도에서 같은 매도를 반복하지 않는다.
                had_open_position: false,
                error: null,
            }),
            remember_shutdown_failure: assign({
                error: ({ event }) => to_ui_command_failure(
                    'error' in event ? event.error : null,
                    'APPLICATION_SHUTDOWN_FAILED',
                    '프로그램 종료 준비를 완료하지 못했습니다.',
                ),
            }),
            remember_sidecar_exit_failure: assign({
                // Child가 이미 끝났으므로 추가 backend command 대신 offline 재시작 안내만 보존한다.
                error: {
                    code: 'SIDECAR_EXITED_ABNORMALLY',
                    message: '백엔드 프로세스가 중단되어 새 주문을 차단했습니다. 창을 닫고 애플리케이션을 다시 시작해 복구해 주세요.',
                },
            }),
        },
    }).createMachine({
        id: 'appExitMachine',
        initial: 'awaiting_exit',
        context: {
            had_open_position: false,
            error: null,
        },
        on: {
            SIDECAR_EXITED_ABNORMALLY: {
                target: '.sidecar_exit_failure',
                actions: 'remember_sidecar_exit_failure',
            },
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
                        actions: 'remember_force_sell_success',
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
                    onError: [
                        {
                            guard: 'sidecar_exit_wait_timed_out',
                            target: 'shutdown_exit_recovery',
                            actions: 'remember_shutdown_failure',
                        },
                        {
                            guard: 'sidecar_exit_failed',
                            target: 'sidecar_exit_failure',
                            actions: 'remember_shutdown_failure',
                        },
                        {
                            guard: 'shutdown_outcome_is_ambiguous',
                            target: 'shutdown_outcome_recovery',
                            actions: 'remember_shutdown_failure',
                        },
                        {
                            // 202 이전 실패는 일반 종료 확인에서 안전 조건을 다시 평가한다.
                            target: 'exit_confirmation',
                            actions: 'remember_shutdown_failure',
                        },
                    ],
                },
                on: {
                    SIDECAR_EXITED: {
                        target: 'ui_final_state',
                    },
                },
            },
            shutdown_exit_recovery: {
                meta: {
                    spec_ids: ['ES3-06', 'ES3-09', 'SIDECAR_EXIT_TIMEOUT'],
                },
                on: {
                    EXIT_CONFIRMED: {
                        target: 'shutting_down',
                    },
                    EXIT_CANCELED: {
                        // Irreversible 202 뒤에는 취소가 정상 거래 화면으로 복귀시키지 않는다.
                        target: 'shutdown_exit_recovery',
                    },
                    SIDECAR_EXITED: {
                        target: 'ui_final_state',
                    },
                },
            },
            shutdown_outcome_recovery: {
                meta: {
                    spec_ids: ['SHUTDOWN_OUTCOME_AMBIGUOUS'],
                },
                on: {
                    EXIT_CONFIRMED: {
                        target: 'shutting_down',
                    },
                    EXIT_CANCELED: {
                        // Commit 여부가 불명인 동안에는 정상 거래 화면과 다른 command로 돌아가지 않는다.
                        target: 'shutdown_outcome_recovery',
                    },
                    SIDECAR_EXITED: {
                        target: 'ui_final_state',
                    },
                },
            },
            sidecar_exit_failure: {
                meta: {
                    spec_ids: ['SIDECAR_CRASH_RECOVERY'],
                },
                on: {
                    EXIT_CONFIRMED: {
                        target: 'ui_final_state',
                    },
                    EXIT_CANCELED: {
                        // Dead child에는 정상 거래 화면 복귀나 shutdown command 재전송을 허용하지 않는다.
                        target: 'sidecar_exit_failure',
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
