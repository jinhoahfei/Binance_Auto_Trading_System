import { assign, fromPromise, setup } from 'xstate';
import {
    DEFAULT_TRADING_LOGIC_COVERAGE,
    type RegimeType,
    type TradingLogicCoverage,
    type UiCommandFailure,
} from '../../../shared/contracts';
import { to_ui_command_failure } from '../../../shared/errors';
import type { UiCommandPort } from '../../../shared/ports';

export interface TradingCommandContext {
    readonly selected_regime: RegimeType | null;
    readonly logic_coverage: ReadonlyArray<TradingLogicCoverage>;
    readonly command_enabled: boolean;
    readonly is_trading: boolean;
    readonly has_open_position: boolean;
    readonly regime_highlight_requested: boolean;
    readonly notice: 'not_running' | null;
    readonly unavailable_reason: TradingUnavailableReason | null;
    readonly error: UiCommandFailure | null;
}

/**
 * 시작을 차단한 Phase 6 coverage 또는 command 준비 상태 사유이다.
 */
export type TradingUnavailableReason = 'unsupported_logic' | 'command_disabled';

/**
 * backend startup snapshot으로 trading actor의 authoritative 상태를 초기화하는 옵션이다.
 */
export interface TradingCommandMachineOptions {
    readonly logic_coverage?: ReadonlyArray<TradingLogicCoverage>;
    readonly command_enabled?: boolean;
    readonly is_trading?: boolean;
    readonly has_open_position?: boolean;
}

export type TradingCommandEvent =
    | {
        readonly type: 'TRADING_SNAPSHOT_SYNCHRONIZED';
        readonly selected_regime: RegimeType | null;
        readonly logic_coverage: ReadonlyArray<TradingLogicCoverage>;
        readonly command_enabled: boolean;
        readonly is_trading: boolean;
        readonly has_open_position: boolean;
    }
    | {
        readonly type: 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED';
        readonly selected_regime: RegimeType | null;
        readonly logic_coverage: ReadonlyArray<TradingLogicCoverage>;
        readonly command_enabled: boolean;
        readonly is_trading: boolean;
        readonly has_open_position: boolean;
    }
    | {
        readonly type: 'START_BUTTON_CLICKED';
        readonly regime: RegimeType | null;
        readonly is_online: boolean;
    }
    | { readonly type: 'START_CONFIRMED'; readonly is_online: boolean }
    | { readonly type: 'START_CANCELED' }
    | { readonly type: 'SELECT_REGIME_NOTICE_CONFIRMED' }
    | { readonly type: 'SELECT_REGIME_NOTICE_CLOSED' }
    | { readonly type: 'API_CONNECTION_NOTICE_CONFIRMED' }
    | { readonly type: 'TRADING_UNAVAILABLE_NOTICE_CONFIRMED' }
    | { readonly type: 'STOP_BUTTON_CLICKED'; readonly has_open_position: boolean }
    | { readonly type: 'STOP_CONFIRMED' }
    | { readonly type: 'STOP_CANCELED' }
    | { readonly type: 'FORCE_SELL_AND_STOP_CONFIRMED' }
    | { readonly type: 'FORCE_SELL_AND_STOP_CANCELED' }
    | { readonly type: 'API_DISCONNECTED' }
    | { readonly type: 'POSITION_UPDATED'; readonly has_open_position: boolean }
    | { readonly type: 'BACKEND_TRADING_STARTED' }
    | { readonly type: 'BACKEND_TRADING_STOPPED' };

/**
 * 함수 이름: to_command_failure()
 * 기능: 알 수 없는 adapter 오류를 화면에 안전하게 표시할 공통 실패 형식으로 변환한다.
 * 인자: error -> adapter 또는 actor가 반환한 오류
 * 반환값: UI 명령 실패 객체
 * 작성 날짜: 2026/08/12
 */
function to_command_failure(error: unknown): UiCommandFailure {
    return to_ui_command_failure(
        error,
        'UI_COMMAND_FAILED',
        '요청을 완료하지 못했습니다.',
    );
}

/**
 * 함수 이름: resolve_trading_start_unavailable_reason()
 * 기능: 선택 REGIME coverage와 command 준비 상태를 backend 우선순위로 확인한다.
 * 인자: logic_coverage -> 최신 REGIME별 coverage, command_enabled -> command 준비 여부,
 *      regime -> 시작하려는 REGIME
 * 반환값: 시작 차단 사유 또는 시작 가능한 경우 null
 * 작성 날짜: 2026/08/21
 */
export function resolve_trading_start_unavailable_reason(
    logic_coverage: ReadonlyArray<TradingLogicCoverage>,
    command_enabled: boolean,
    regime: RegimeType | null,
): TradingUnavailableReason | null {
    if (regime === null) {
        return null;
    }

    // 누락된 coverage도 지원으로 추측하지 않고 unsupported와 같은 fail-closed 결과로 처리한다.
    const coverage = logic_coverage.find((item) => item.regime_type === regime);

    if (coverage?.support_status !== 'supported' || coverage.start_guard !== 'READY') {
        return 'unsupported_logic';
    }
    if (!command_enabled) {
        return 'command_disabled';
    }

    return null;  // 두 backend 소유 gate가 모두 준비된 경우에만 확인 단계로 진행한다.
}

/**
 * 함수 이름: create_trading_command_machine()
 * 기능: 자동매매 시작, 일반 중지, 강제 매도 후 중지의 확인·대기·성공·실패 전이를 생성한다.
 * 인자: command_port -> backend 명령을 수행할 UI port
 * 반환값: trading-control feature의 XState machine
 * 작성 날짜: 2026/08/12
 */
export function create_trading_command_machine(
    command_port: UiCommandPort,
    options: TradingCommandMachineOptions = {},
) {
    return setup({
        types: {
            context: {} as TradingCommandContext,
            events: {} as TradingCommandEvent,
        },
        actors: {
            start_trading: fromPromise<void, RegimeType>(async ({ input }) => {
                await command_port.start_trading(input);
            }),
            stop_trading: fromPromise<void>(async () => {
                await command_port.stop_trading();
            }),
            force_sell_and_stop: fromPromise<void>(async () => {
                await command_port.force_sell_and_stop();
            }),
        },
        guards: {
            snapshot_is_running: ({ event }) => {
                return event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                    && event.is_trading;
            },
            is_regime_missing: ({ event }) => {
                return event.type === 'START_BUTTON_CLICKED' && event.regime === null;
            },
            is_regime_missing_at_confirmation: ({ context, event }) => {
                return event.type === 'START_CONFIRMED' && context.selected_regime === null;
            },
            is_start_unavailable_at_request: ({ context, event }) => {
                return event.type === 'START_BUTTON_CLICKED'
                    && resolve_trading_start_unavailable_reason(
                        context.logic_coverage,
                        context.command_enabled,
                        event.regime,
                    ) !== null;
            },
            is_start_unavailable_at_confirmation: ({ context, event }) => {
                return event.type === 'START_CONFIRMED'
                    && resolve_trading_start_unavailable_reason(
                        context.logic_coverage,
                        context.command_enabled,
                        context.selected_regime,
                    ) !== null;
            },
            is_api_offline_at_start_request: ({ event }) => {
                return event.type === 'START_BUTTON_CLICKED' && !event.is_online;
            },
            is_api_offline_at_confirmation: ({ event }) => {
                return event.type === 'START_CONFIRMED' && !event.is_online;
            },
            has_open_position: ({ event }) => {
                return event.type === 'STOP_BUTTON_CLICKED' && event.has_open_position;
            },
        },
        actions: {
            synchronize_trading_snapshot: assign({
                selected_regime: ({ context, event }) => {
                    return event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                        || event.type === 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED'
                        ? event.selected_regime
                        : context.selected_regime;
                },
                logic_coverage: ({ context, event }) => {
                    return event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                        || event.type === 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED'
                        ? event.logic_coverage
                        : context.logic_coverage;
                },
                command_enabled: ({ context, event }) => {
                    return event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                        || event.type === 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED'
                        ? event.command_enabled
                        : context.command_enabled;
                },
                is_trading: ({ context, event }) => {
                    return event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                        || event.type === 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED'
                        ? event.is_trading
                        : context.is_trading;
                },
                has_open_position: ({ context, event }) => {
                    return event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                        || event.type === 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED'
                        ? event.has_open_position
                        : context.has_open_position;
                },
                notice: ({ context, event }) => {
                    return event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                        ? null
                        : context.notice;
                },
                error: ({ context, event }) => {
                    return event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                        ? null
                        : context.error;
                },
                unavailable_reason: ({ context, event }) => {
                    return event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                        ? null
                        : context.unavailable_reason;
                },
            }),
            remember_start_request: assign({
                selected_regime: ({ event }) => {
                    return event.type === 'START_BUTTON_CLICKED' ? event.regime : null;
                },
                regime_highlight_requested: false,
                notice: null,
                unavailable_reason: null,
                error: null,
            }),
            remember_start_unavailability: assign({
                selected_regime: ({ context, event }) => {
                    return event.type === 'START_BUTTON_CLICKED'
                        ? event.regime
                        : context.selected_regime;
                },
                unavailable_reason: ({ context, event }) => {
                    const requested_regime = event.type === 'START_BUTTON_CLICKED'
                        ? event.regime
                        : context.selected_regime;

                    return resolve_trading_start_unavailable_reason(
                        context.logic_coverage,
                        context.command_enabled,
                        requested_regime,
                    );
                },
                regime_highlight_requested: false,
                notice: null,
                error: null,
            }),
            clear_start_unavailability: assign({
                unavailable_reason: null,
            }),
            remember_position: assign({
                has_open_position: ({ context, event }) => {
                    return event.type === 'STOP_BUTTON_CLICKED'
                        ? event.has_open_position
                        : context.has_open_position;
                },
                notice: null,
                error: null,
            }),
            synchronize_position: assign({
                has_open_position: ({ event, context }) => {
                    return event.type === 'POSITION_UPDATED'
                        ? event.has_open_position
                        : context.has_open_position;
                },
            }),
            request_regime_highlight: assign({
                regime_highlight_requested: true,
            }),
            clear_regime_highlight_request: assign({
                regime_highlight_requested: false,
            }),
            mark_not_running: assign({
                notice: 'not_running',
            }),
            mark_trading_started: assign({
                is_trading: true,
                error: null,
            }),
            mark_trading_stopped: assign({
                is_trading: false,
                error: null,
            }),
            clear_open_position: assign({
                has_open_position: false,
            }),
            remember_failure: assign({
                error: ({ event }) => {
                    return 'error' in event
                        ? to_command_failure(event.error)
                        : null;
                },
            }),
        },
    }).createMachine({
        id: 'tradingCommandMachine',
        initial: options.is_trading === true ? 'running' : 'stopped',
        context: {
            selected_regime: null,
            logic_coverage: options.logic_coverage ?? DEFAULT_TRADING_LOGIC_COVERAGE,
            command_enabled: options.command_enabled ?? false,
            is_trading: options.is_trading ?? false,
            has_open_position: options.has_open_position ?? false,
            regime_highlight_requested: false,
            notice: null,
            unavailable_reason: null,
            error: null,
        },
        on: {
            TRADING_SNAPSHOT_SYNCHRONIZED: [
                {
                    guard: 'snapshot_is_running',
                    target: '.running',
                    actions: 'synchronize_trading_snapshot',
                },
                {
                    target: '.stopped',
                    actions: 'synchronize_trading_snapshot',
                },
            ],
            TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED: {
                actions: 'synchronize_trading_snapshot',
            },
            POSITION_UPDATED: {
                actions: 'synchronize_position',
            },
            BACKEND_TRADING_STARTED: {
                target: '.running',
                actions: 'mark_trading_started',
            },
            BACKEND_TRADING_STOPPED: {
                target: '.stopped',
                actions: 'mark_trading_stopped',
            },
        },
        states: {
            stopped: {
                meta: {
                    spec_ids: ['U2-01', 'U2-04', 'U3-01', 'CR-16'],
                },
                entry: 'mark_trading_stopped',
                on: {
                    START_BUTTON_CLICKED: [
                        {
                            guard: 'is_regime_missing',
                            target: 'select_regime_notice',
                            actions: 'remember_start_request',
                        },
                        {
                            guard: 'is_start_unavailable_at_request',
                            target: 'trading_unavailable_notice',
                            actions: 'remember_start_unavailability',
                        },
                        {
                            guard: 'is_api_offline_at_start_request',
                            target: 'api_connection_required',
                            actions: 'remember_start_request',
                        },
                        {
                            target: 'start_confirmation',
                            actions: 'remember_start_request',
                        },
                    ],
                    STOP_BUTTON_CLICKED: {
                        actions: 'mark_not_running',
                    },
                },
            },
            select_regime_notice: {
                meta: {
                    spec_ids: ['U3-03', 'U3-08', 'VR-01', 'VR-13', 'ER-06A', 'ER-06B'],
                },
                on: {
                    SELECT_REGIME_NOTICE_CONFIRMED: {
                        target: 'stopped',
                        actions: 'request_regime_highlight',
                    },
                    SELECT_REGIME_NOTICE_CLOSED: {
                        target: 'stopped',
                        actions: 'clear_regime_highlight_request',
                    },
                },
            },
            api_connection_required: {
                meta: {
                    spec_ids: ['U3-04', 'U3-06', 'U3-09', 'U3-12'],
                },
                on: {
                    API_CONNECTION_NOTICE_CONFIRMED: {
                        target: 'stopped',
                    },
                },
            },
            trading_unavailable_notice: {
                meta: {
                    spec_ids: ['PHASE6-COVERAGE-GATE'],
                },
                on: {
                    TRADING_UNAVAILABLE_NOTICE_CONFIRMED: {
                        target: 'stopped',
                        actions: 'clear_start_unavailability',
                    },
                },
            },
            start_confirmation: {
                meta: {
                    spec_ids: ['U3-02', 'U3-05', 'U3-06', 'U3-07', 'VR-02', 'ER-05'],
                },
                on: {
                    START_CONFIRMED: [
                        {
                            guard: 'is_regime_missing_at_confirmation',
                            target: 'select_regime_notice',
                            actions: 'clear_start_unavailability',
                        },
                        {
                            guard: 'is_start_unavailable_at_confirmation',
                            target: 'trading_unavailable_notice',
                            actions: 'remember_start_unavailability',
                        },
                        {
                            guard: 'is_api_offline_at_confirmation',
                            target: 'api_connection_required',
                        },
                        {
                            target: 'starting',
                        },
                    ],
                    START_CANCELED: {
                        target: 'stopped',
                    },
                    API_DISCONNECTED: {
                        target: 'api_connection_required',
                    },
                },
            },
            starting: {
                meta: {
                    spec_ids: ['U3-05'],
                    pending: true,
                },
                invoke: {
                    id: 'start_command',
                    src: 'start_trading',
                    input: ({ context }) => context.selected_regime as RegimeType,
                    onDone: {
                        target: 'running',
                        actions: 'mark_trading_started',
                    },
                    onError: {
                        target: 'start_confirmation',
                        actions: 'remember_failure',
                    },
                },
            },
            running: {
                meta: {
                    spec_ids: ['U3-05', 'U3-10', 'U3-11', 'VR-01'],
                },
                entry: 'mark_trading_started',
                on: {
                    START_BUTTON_CLICKED: {},
                    STOP_BUTTON_CLICKED: [
                        {
                            guard: 'has_open_position',
                            target: 'force_sell_confirmation',
                            actions: 'remember_position',
                        },
                        {
                            target: 'stop_confirmation',
                            actions: 'remember_position',
                        },
                    ],
                    API_DISCONNECTED: {
                        target: 'disconnect_stopping',
                    },
                },
            },
            stop_confirmation: {
                meta: {
                    spec_ids: ['U2-02', 'U2-05', 'U2-06', 'VR-03', 'ER-07', 'ER-08'],
                },
                on: {
                    STOP_CONFIRMED: {
                        target: 'stopping',
                    },
                    STOP_CANCELED: {
                        target: 'running',
                    },
                    API_DISCONNECTED: {
                        target: 'disconnect_stopping',
                    },
                },
            },
            stopping: {
                meta: {
                    spec_ids: ['U2-05'],
                    pending: true,
                },
                invoke: {
                    id: 'stop_command',
                    src: 'stop_trading',
                    onDone: {
                        target: 'stopped',
                        actions: 'mark_trading_stopped',
                    },
                    onError: {
                        target: 'stop_confirmation',
                        actions: 'remember_failure',
                    },
                },
            },
            force_sell_confirmation: {
                meta: {
                    spec_ids: ['U2-03', 'U2-07', 'U2-09', 'U2-10', 'VR-03', 'ER-09', 'ER-10'],
                },
                on: {
                    FORCE_SELL_AND_STOP_CONFIRMED: {
                        target: 'force_selling',
                    },
                    FORCE_SELL_AND_STOP_CANCELED: {
                        target: 'running',
                    },
                    API_DISCONNECTED: {
                        target: 'disconnect_stopping',
                    },
                },
            },
            force_selling: {
                meta: {
                    spec_ids: ['U2-07', 'U2-08', 'U2-09'],
                    pending: true,
                },
                invoke: {
                    id: 'force_sell_command',
                    src: 'force_sell_and_stop',
                    onDone: {
                        target: 'stopped',
                        actions: ['clear_open_position', 'mark_trading_stopped'],
                    },
                    onError: {
                        target: 'force_sell_confirmation',
                        actions: 'remember_failure',
                    },
                },
            },
            disconnect_stopping: {
                meta: {
                    spec_ids: ['U3-12'],
                    pending: true,
                },
                invoke: {
                    id: 'disconnect_stop_command',
                    src: 'stop_trading',
                    onDone: {
                        target: 'api_connection_required',
                        actions: 'mark_trading_stopped',
                    },
                    onError: {
                        target: 'api_connection_required',
                        actions: ['remember_failure', 'mark_trading_stopped'],
                    },
                },
            },
        },
    });
}
