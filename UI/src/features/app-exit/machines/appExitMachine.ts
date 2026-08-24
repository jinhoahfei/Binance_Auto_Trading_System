import { assign, fromPromise, setup } from 'xstate';
import type { BackendTradingStatus, UiCommandFailure } from '../../../shared/contracts';
import { to_ui_command_failure } from '../../../shared/errors';
import type { TradingCommandReceipt, UiCommandPort } from '../../../shared/ports';

export interface AppExitMachineContext {
    readonly had_open_position: boolean;
    readonly had_recovered_position: boolean;
    readonly observed_trading_version: number | null;
    readonly observed_trading_status: BackendTradingStatus | null;
    readonly observed_has_open_position: boolean | null;
    readonly error: UiCommandFailure | null;
}

export type AppExitMachineEvent =
    | {
        readonly type: 'EXIT_CLICKED';
        readonly has_open_position: boolean;
        readonly is_trading?: boolean;
    }
    | { readonly type: 'FORCE_SELL_EXIT_CANCELED' }
    | { readonly type: 'FORCE_SELL_EXIT_CONFIRMED' }
    | { readonly type: 'EXIT_CANCELED' }
    | { readonly type: 'EXIT_CONFIRMED' }
    | { readonly type: 'SIDECAR_EXITED' }
    | { readonly type: 'SIDECAR_EXITED_ABNORMALLY' }
    | {
        readonly type: 'TRADING_SESSION_UPDATED';
        readonly version: number;
        readonly status: BackendTradingStatus;
        readonly has_open_position: boolean;
    };

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
 * 기능: OS 종료 요청을 Position 청산, authoritative terminal 확인과 안전 종료로 조정한다.
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
            force_sell: fromPromise<TradingCommandReceipt, boolean>(async ({ input }) => {
                // NOT_STARTED 복구 Position은 정상 session stop이 아닌 별도 takeover Operation을 사용한다.
                if (input) {
                    return command_port.liquidate_recovered_position();
                }
                return command_port.force_sell_and_stop();  // 실행 중 Position은 기존 D-05 stop을 유지한다.
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
            // Receipt 이상 version의 lifecycle 관찰값을 우선해 비동기 완료와 reconciliation을 판정한다.
            force_sell_completion_is_stopping: ({ context, event }) => {
                const receipt = 'output' in event
                    ? event.output as TradingCommandReceipt
                    : null;
                const observation_is_newer = receipt !== null
                    && context.observed_trading_version !== null
                    && context.observed_trading_version >= receipt.version;

                return observation_is_newer
                    ? context.observed_trading_status === 'stopping'
                    : receipt?.status === 'stopping';
            },
            force_sell_completion_requires_reconciliation: ({ context, event }) => {
                const receipt = 'output' in event
                    ? event.output as TradingCommandReceipt
                    : null;
                const observation_is_newer = receipt !== null
                    && context.observed_trading_version !== null
                    && context.observed_trading_version >= receipt.version;

                return observation_is_newer
                    ? context.observed_trading_status === 'reconciliation_required'
                    : receipt?.status === 'reconciliation_required';
            },
            force_sell_completion_is_terminal: ({ context, event }) => {
                const receipt = 'output' in event
                    ? event.output as TradingCommandReceipt
                    : null;
                const observation_is_newer = receipt !== null
                    && context.observed_trading_version !== null
                    && context.observed_trading_version >= receipt.version;

                return observation_is_newer
                    ? context.observed_trading_status === 'terminated'
                        && context.observed_has_open_position === false
                    : receipt?.status === 'terminated';
            },
            trading_session_is_terminal_without_position: ({ event }) => {
                return event.type === 'TRADING_SESSION_UPDATED'
                    && event.status === 'terminated'
                    && !event.has_open_position;
            },
            trading_session_requires_reconciliation: ({ event }) => {
                return event.type === 'TRADING_SESSION_UPDATED'
                    && event.status === 'reconciliation_required';
            },
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
                had_recovered_position: ({ event }) => {
                    return event.type === 'EXIT_CLICKED'
                        && event.has_open_position
                        && event.is_trading === false;
                },
                error: null,
            }),
            clear_liquidation_observation: assign({
                observed_trading_version: null,
                observed_trading_status: null,
                observed_has_open_position: null,
            }),
            remember_trading_session_update: assign({
                observed_trading_version: ({ context, event }) => {
                    const should_update = event.type === 'TRADING_SESSION_UPDATED'
                        && (context.observed_trading_version === null
                            || event.version >= context.observed_trading_version);

                    return should_update ? event.version : context.observed_trading_version;
                },
                observed_trading_status: ({ context, event }) => {
                    const should_update = event.type === 'TRADING_SESSION_UPDATED'
                        && (context.observed_trading_version === null
                            || event.version >= context.observed_trading_version);

                    return should_update ? event.status : context.observed_trading_status;
                },
                observed_has_open_position: ({ context, event }) => {
                    const should_update = event.type === 'TRADING_SESSION_UPDATED'
                        && (context.observed_trading_version === null
                            || event.version >= context.observed_trading_version);

                    return should_update
                        ? event.has_open_position
                        : context.observed_has_open_position;
                },
            }),
            remember_force_sell_failure: assign({
                error: ({ event }) => to_ui_command_failure(
                    'error' in event ? event.error : null,
                    'EXIT_FORCE_SELL_FAILED',
                    '포지션 강제 매도에 실패해 프로그램 종료를 취소했습니다.',
                ),
            }),
            remember_force_sell_success: assign({
                // Terminal receipt 또는 event가 Position 종료를 확정한 뒤에만 청산 상태를 지운다.
                had_open_position: false,
                had_recovered_position: false,
                observed_trading_version: null,
                observed_trading_status: null,
                observed_has_open_position: null,
                error: null,
            }),
            remember_liquidation_reconciliation_required: assign({
                // 조정 필요 상태에서는 Position 표식을 유지해 shutdown과 중복 신규 명령을 차단한다.
                error: {
                    code: 'EXIT_LIQUIDATION_RECONCILIATION_REQUIRED',
                    message: '포지션 청산 결과를 확정할 수 없어 프로그램을 종료하지 않았습니다. 주문과 포지션 조정 상태를 확인해 주세요.',
                },
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
            had_recovered_position: false,
            observed_trading_version: null,
            observed_trading_status: null,
            observed_has_open_position: null,
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
                entry: 'clear_liquidation_observation',
                invoke: {
                    id: 'exit_force_sell_command',
                    src: 'force_sell',
                    input: ({ context }) => context.had_recovered_position,
                    onDone: [
                        {
                            guard: 'force_sell_completion_requires_reconciliation',
                            target: 'force_sell_exit_confirmation',
                            actions: 'remember_liquidation_reconciliation_required',
                        },
                        {
                            guard: 'force_sell_completion_is_stopping',
                            target: 'awaiting_liquidation_terminal',
                        },
                        {
                            guard: 'force_sell_completion_is_terminal',
                            target: 'shutting_down',
                            actions: 'remember_force_sell_success',
                        },
                        {
                            target: 'force_sell_exit_confirmation',
                            actions: 'remember_force_sell_failure',
                        },
                    ],
                    onError: {
                        target: 'force_sell_exit_confirmation',
                        actions: 'remember_force_sell_failure',
                    },
                },
                on: {
                    TRADING_SESSION_UPDATED: {
                        actions: 'remember_trading_session_update',
                    },
                },
            },
            // STOPPING receipt는 lifecycle event가 Position 종료를 증명할 때까지 shutdown을 보류한다.
            awaiting_liquidation_terminal: {
                meta: {
                    spec_ids: ['PHASE9-RECOVERED-POSITION-LIQUIDATION'],
                    pending: true,
                },
                on: {
                    TRADING_SESSION_UPDATED: [
                        {
                            guard: 'trading_session_requires_reconciliation',
                            target: 'force_sell_exit_confirmation',
                            actions: 'remember_liquidation_reconciliation_required',
                        },
                        {
                            guard: 'trading_session_is_terminal_without_position',
                            target: 'shutting_down',
                            actions: 'remember_force_sell_success',
                        },
                    ],
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
