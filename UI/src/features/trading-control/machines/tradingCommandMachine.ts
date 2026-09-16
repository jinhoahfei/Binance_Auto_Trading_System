import type { BackendBalanceReconciliation } from '../../../shared/contracts';
import { assign, fromPromise, setup } from 'xstate';
import {
    DEFAULT_TRADING_LOGIC_COVERAGE,
    type BackendDailyLossScope,
    type BackendDecimalString,
    type BackendManualKillBehavior,
    type BackendRiskBudgetSnapshot,
    type BackendRiskBlockReason,
    type BackendRiskPolicyAvailability,
    type BackendTradingStatus,
    type RegimeType,
    type TradingLogicCoverage,
    type UiCommandFailure,
} from '../../../shared/contracts';
import { to_ui_command_failure } from '../../../shared/errors';
import type { TradingCommandReceipt, UiCommandPort } from '../../../shared/ports';

export interface TradingCommandContext {
    readonly selected_regime: RegimeType | null;
    readonly logic_coverage: ReadonlyArray<TradingLogicCoverage>;
    readonly command_enabled: boolean;
    readonly risk_policy_availability: BackendRiskPolicyAvailability | undefined;
    readonly configured_risk_policy_version: number | null | undefined;
    readonly max_order_notional: BackendDecimalString | null | undefined;
    readonly max_position_notional: BackendDecimalString | null | undefined;
    readonly max_daily_loss: BackendDecimalString | null | undefined;
    readonly daily_loss_scope: BackendDailyLossScope | null | undefined;
    readonly manual_kill_behavior: BackendManualKillBehavior | null | undefined;
    readonly session_risk_policy_version: number | null | undefined;
    readonly risk_control_version: number | undefined;
    readonly manual_kill_active: boolean | undefined;
    readonly manual_kill_cleanup_complete: boolean | undefined;
    readonly manual_kill_activation_behavior:
        BackendManualKillBehavior | null | undefined;
    readonly manual_kill_activation_policy_version: number | null | undefined;
    readonly last_risk_decision_allowed: boolean | null | undefined;
    readonly last_risk_budget: BackendRiskBudgetSnapshot | null | undefined;
    readonly risk_block_reason: BackendRiskBlockReason | null | undefined;
    readonly process_ownership_ambiguous: boolean | undefined;
    readonly lifecycle_status: BackendTradingStatus;
    readonly is_trading: boolean;
    readonly is_recovery_liquidation: boolean;
    readonly has_open_position: boolean;
    readonly position_average_entry_price: BackendDecimalString | null;
    readonly residual_quantity?: BackendDecimalString;
    readonly residual_cost_basis?: BackendDecimalString;
    readonly balance_reconciliation?: BackendBalanceReconciliation | null;
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
    readonly risk_policy_availability?: BackendRiskPolicyAvailability;
    readonly configured_risk_policy_version?: number | null;
    readonly max_order_notional?: BackendDecimalString | null;
    readonly max_position_notional?: BackendDecimalString | null;
    readonly max_daily_loss?: BackendDecimalString | null;
    readonly daily_loss_scope?: BackendDailyLossScope | null;
    readonly manual_kill_behavior?: BackendManualKillBehavior | null;
    readonly session_risk_policy_version?: number | null;
    readonly risk_control_version?: number;
    readonly manual_kill_active?: boolean;
    readonly manual_kill_cleanup_complete?: boolean;
    readonly manual_kill_activation_behavior?: BackendManualKillBehavior | null;
    readonly manual_kill_activation_policy_version?: number | null;
    readonly last_risk_decision_allowed?: boolean | null;
    readonly last_risk_budget?: BackendRiskBudgetSnapshot | null;
    readonly risk_block_reason?: BackendRiskBlockReason | null;
    readonly process_ownership_ambiguous?: boolean;
    readonly lifecycle_status?: BackendTradingStatus;
    readonly is_trading?: boolean;
    readonly has_open_position?: boolean;
    readonly position_average_entry_price?: BackendDecimalString | null;
    readonly residual_quantity?: BackendDecimalString;
    readonly residual_cost_basis?: BackendDecimalString;
    readonly balance_reconciliation?: BackendBalanceReconciliation | null;
}

export type TradingCommandEvent =
    | {
        readonly type: 'TRADING_SNAPSHOT_SYNCHRONIZED';
        readonly selected_regime: RegimeType | null;
        readonly logic_coverage: ReadonlyArray<TradingLogicCoverage>;
        readonly command_enabled: boolean;
        readonly risk_policy_availability?: BackendRiskPolicyAvailability;
        readonly configured_risk_policy_version?: number | null;
        readonly max_order_notional?: BackendDecimalString | null;
        readonly max_position_notional?: BackendDecimalString | null;
        readonly max_daily_loss?: BackendDecimalString | null;
        readonly daily_loss_scope?: BackendDailyLossScope | null;
        readonly manual_kill_behavior?: BackendManualKillBehavior | null;
        readonly session_risk_policy_version?: number | null;
        readonly risk_control_version?: number;
        readonly manual_kill_active?: boolean;
        readonly manual_kill_cleanup_complete?: boolean;
        readonly manual_kill_activation_behavior?: BackendManualKillBehavior | null;
        readonly manual_kill_activation_policy_version?: number | null;
        readonly last_risk_decision_allowed?: boolean | null;
        readonly last_risk_budget?: BackendRiskBudgetSnapshot | null;
        readonly risk_block_reason?: BackendRiskBlockReason | null;
        readonly process_ownership_ambiguous?: boolean;
        readonly is_trading: boolean;
        readonly has_open_position: boolean;
        readonly position_average_entry_price?: BackendDecimalString | null;
        readonly residual_quantity?: BackendDecimalString;
        readonly residual_cost_basis?: BackendDecimalString;
        readonly balance_reconciliation?: BackendBalanceReconciliation | null;
        readonly lifecycle_status: BackendTradingStatus;
    }
    | {
        readonly type: 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED';
        readonly selected_regime: RegimeType | null;
        readonly logic_coverage: ReadonlyArray<TradingLogicCoverage>;
        readonly command_enabled: boolean;
        readonly risk_policy_availability?: BackendRiskPolicyAvailability;
        readonly configured_risk_policy_version?: number | null;
        readonly max_order_notional?: BackendDecimalString | null;
        readonly max_position_notional?: BackendDecimalString | null;
        readonly max_daily_loss?: BackendDecimalString | null;
        readonly daily_loss_scope?: BackendDailyLossScope | null;
        readonly manual_kill_behavior?: BackendManualKillBehavior | null;
        readonly session_risk_policy_version?: number | null;
        readonly risk_control_version?: number;
        readonly manual_kill_active?: boolean;
        readonly manual_kill_cleanup_complete?: boolean;
        readonly manual_kill_activation_behavior?: BackendManualKillBehavior | null;
        readonly manual_kill_activation_policy_version?: number | null;
        readonly last_risk_decision_allowed?: boolean | null;
        readonly last_risk_budget?: BackendRiskBudgetSnapshot | null;
        readonly risk_block_reason?: BackendRiskBlockReason | null;
        readonly process_ownership_ambiguous?: boolean;
        readonly is_trading: boolean;
        readonly has_open_position: boolean;
        readonly position_average_entry_price?: BackendDecimalString | null;
        readonly residual_quantity?: BackendDecimalString;
        readonly residual_cost_basis?: BackendDecimalString;
        readonly balance_reconciliation?: BackendBalanceReconciliation | null;
        readonly lifecycle_status: BackendTradingStatus;
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
    | { readonly type: 'RECOVERED_POSITION_LIQUIDATION_CONFIRMED' }
    | { readonly type: 'RECOVERED_POSITION_LIQUIDATION_CANCELED' }
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
 * 기능: 자동매매 시작, 일반 중지, 강제 매도와 복구 Position 청산 전이를 생성한다.
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
            start_trading: fromPromise<TradingCommandReceipt, RegimeType>(async ({ input }) => {
                return command_port.start_trading(input);
            }),
            stop_trading: fromPromise<TradingCommandReceipt>(async () => {
                return command_port.stop_trading();
            }),
            force_sell_and_stop: fromPromise<TradingCommandReceipt>(async () => {
                return command_port.force_sell_and_stop();
            }),
            // startup 복구 Position은 정상 session stop과 분리된 공개 Operation으로만 청산한다.
            liquidate_recovered_position: fromPromise<TradingCommandReceipt>(async () => {
                return command_port.liquidate_recovered_position();
            }),
        },
        guards: {
            snapshot_preserves_local_command: ({ context, event }) => {
                return event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                    && event.lifecycle_status === context.lifecycle_status;
            },
            snapshot_is_running: ({ event }) => {
                return event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                    && event.lifecycle_status === 'running';
            },
            snapshot_is_awaiting_stop: ({ event }) => {
                return event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                    && (event.lifecycle_status === 'stopping'
                        || event.lifecycle_status === 'reconciliation_required');
            },
            snapshot_is_recovery_awaiting_stop: ({ context, event }) => {
                return context.is_recovery_liquidation
                    && event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                    && (event.lifecycle_status === 'stopping'
                        || event.lifecycle_status === 'reconciliation_required');
            },
            stop_result_is_terminal: ({ event }) => {
                const receipt = 'output' in event
                    ? event.output as TradingCommandReceipt
                    : null;

                return receipt?.status === 'terminated'
                    || receipt?.status === 'not_started';
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
            has_recovered_position: ({ context }) => {
                return !context.is_trading && context.has_open_position;
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
                // Optional risk fields는 수신한 snapshot 값만 갱신하고 생략 시 기존 authoritative 값을 보존한다.
                risk_policy_availability: ({ context, event }) => {
                    return (event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                        || event.type === 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED')
                        && event.risk_policy_availability !== undefined
                        ? event.risk_policy_availability
                        : context.risk_policy_availability;
                },
                configured_risk_policy_version: ({ context, event }) => {
                    return (event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                        || event.type === 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED')
                        && event.configured_risk_policy_version !== undefined
                        ? event.configured_risk_policy_version
                        : context.configured_risk_policy_version;
                },
                max_order_notional: ({ context, event }) => {
                    return (event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                        || event.type === 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED')
                        && event.max_order_notional !== undefined
                        ? event.max_order_notional
                        : context.max_order_notional;
                },
                max_position_notional: ({ context, event }) => {
                    return (event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                        || event.type === 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED')
                        && event.max_position_notional !== undefined
                        ? event.max_position_notional
                        : context.max_position_notional;
                },
                max_daily_loss: ({ context, event }) => {
                    return (event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                        || event.type === 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED')
                        && event.max_daily_loss !== undefined
                        ? event.max_daily_loss
                        : context.max_daily_loss;
                },
                daily_loss_scope: ({ context, event }) => {
                    return (event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                        || event.type === 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED')
                        && event.daily_loss_scope !== undefined
                        ? event.daily_loss_scope
                        : context.daily_loss_scope;
                },
                manual_kill_behavior: ({ context, event }) => {
                    return (event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                        || event.type === 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED')
                        && event.manual_kill_behavior !== undefined
                        ? event.manual_kill_behavior
                        : context.manual_kill_behavior;
                },
                session_risk_policy_version: ({ context, event }) => {
                    return (event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                        || event.type === 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED')
                        && event.session_risk_policy_version !== undefined
                        ? event.session_risk_policy_version
                        : context.session_risk_policy_version;
                },
                risk_control_version: ({ context, event }) => {
                    return (event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                        || event.type === 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED')
                        && event.risk_control_version !== undefined
                        ? event.risk_control_version
                        : context.risk_control_version;
                },
                manual_kill_active: ({ context, event }) => {
                    return (event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                        || event.type === 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED')
                        && event.manual_kill_active !== undefined
                        ? event.manual_kill_active
                        : context.manual_kill_active;
                },
                manual_kill_cleanup_complete: ({ context, event }) => {
                    return (event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                        || event.type === 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED')
                        && event.manual_kill_cleanup_complete !== undefined
                        ? event.manual_kill_cleanup_complete
                        : context.manual_kill_cleanup_complete;
                },
                manual_kill_activation_behavior: ({ context, event }) => {
                    return (event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                        || event.type === 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED')
                        && event.manual_kill_activation_behavior !== undefined
                        ? event.manual_kill_activation_behavior
                        : context.manual_kill_activation_behavior;
                },
                manual_kill_activation_policy_version: ({ context, event }) => {
                    return (event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                        || event.type === 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED')
                        && event.manual_kill_activation_policy_version !== undefined
                        ? event.manual_kill_activation_policy_version
                        : context.manual_kill_activation_policy_version;
                },
                last_risk_decision_allowed: ({ context, event }) => {
                    return (event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                        || event.type === 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED')
                        && event.last_risk_decision_allowed !== undefined
                        ? event.last_risk_decision_allowed
                        : context.last_risk_decision_allowed;
                },
                last_risk_budget: ({ context, event }) => {
                    return (event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                        || event.type === 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED')
                        && event.last_risk_budget !== undefined
                        ? event.last_risk_budget
                        : context.last_risk_budget;
                },
                risk_block_reason: ({ context, event }) => {
                    return (event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                        || event.type === 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED')
                        && event.risk_block_reason !== undefined
                        ? event.risk_block_reason
                        : context.risk_block_reason;
                },
                process_ownership_ambiguous: ({ context, event }) => {
                    return (event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                        || event.type === 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED')
                        && event.process_ownership_ambiguous !== undefined
                        ? event.process_ownership_ambiguous
                        : context.process_ownership_ambiguous;
                },
                lifecycle_status: ({ context, event }) => {
                    return event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                        || event.type === 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED'
                        ? event.lifecycle_status : context.lifecycle_status;
                },
                is_trading: ({ context, event }) => {
                    if (event.type !== 'TRADING_SNAPSHOT_SYNCHRONIZED'
                        && event.type !== 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED') {
                        return context.is_trading;
                    }

                    // Recovery STOPPING은 backend lifecycle이 active여도 자동매매 실행으로 표시하지 않는다.
                    return context.is_recovery_liquidation ? false : event.is_trading;
                },
                has_open_position: ({ context, event }) => {
                    return event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED'
                        || event.type === 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED'
                        ? event.has_open_position
                        : context.has_open_position;
                },
                // 시작·실시간 갱신·재연결에서 보유 여부와 평단가를 한 snapshot으로 교체한다.
                residual_quantity: ({ context, event }) => (event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED' || event.type === 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED') ? event.residual_quantity ?? '0' : context.residual_quantity,
                residual_cost_basis: ({ context, event }) => (event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED' || event.type === 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED') ? event.residual_cost_basis ?? '0' : context.residual_cost_basis,
                balance_reconciliation: ({ context, event }) => (event.type === 'TRADING_SNAPSHOT_SYNCHRONIZED' || event.type === 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED') ? event.balance_reconciliation ?? null : context.balance_reconciliation,
                position_average_entry_price: ({ context, event }) => {
                    if (event.type !== 'TRADING_SNAPSHOT_SYNCHRONIZED'
                        && event.type !== 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED') {
                        return context.position_average_entry_price;
                    }

                    return event.has_open_position
                        ? event.position_average_entry_price ?? null
                        : null;  // 종료되었거나 값이 미제공이면 이전 평단가를 남기지 않는다.
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
                is_recovery_liquidation: false,
                error: null,
            }),
            remember_recovery_liquidation_request: assign({
                has_open_position: ({ context, event }) => {
                    return event.type === 'STOP_BUTTON_CLICKED'
                        ? event.has_open_position
                        : context.has_open_position;
                },
                is_trading: false,
                is_recovery_liquidation: true,
                notice: null,
                error: null,
            }),
            synchronize_position: assign({
                // 보유 여부만 전달하는 기존 event로 포지션이 바뀌면 오래된 평단가를 제거한다.
                position_average_entry_price: ({ event, context }) => {
                    return event.type === 'POSITION_UPDATED'
                        && (!event.has_open_position || !context.has_open_position)
                        ? null
                        : context.position_average_entry_price;
                },
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
                lifecycle_status: ({ context }) => context.lifecycle_status === 'reconciliation_required'
                    || context.lifecycle_status === 'stopping' ? context.lifecycle_status : 'running',
                is_trading: true,
                is_recovery_liquidation: false,
                error: null,
            }),
            mark_trading_stopped: assign({
                lifecycle_status: ({ context }) => context.lifecycle_status === 'not_started' ? 'not_started' : 'terminated',
                is_trading: false,
                is_recovery_liquidation: false,
                error: null,
            }),
            mark_stop_accepted: assign({
                lifecycle_status: ({ context }) => context.lifecycle_status === 'reconciliation_required' ? 'reconciliation_required' : 'stopping',
                is_trading: true,
                is_recovery_liquidation: false,
                notice: null,
                error: null,
            }),
            mark_recovery_liquidation_pending: assign({
                lifecycle_status: ({ context }) => context.lifecycle_status === 'reconciliation_required' ? 'reconciliation_required' : 'stopping',
                is_trading: false,
                is_recovery_liquidation: true,
                notice: null,
                error: null,
            }),
            clear_open_position: assign({
                has_open_position: false,
                position_average_entry_price: null,  // 청산 완료 시 기준선 표시 값도 함께 정리한다.
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
            risk_policy_availability: options.risk_policy_availability,
            configured_risk_policy_version: options.configured_risk_policy_version,
            max_order_notional: options.max_order_notional,
            max_position_notional: options.max_position_notional,
            max_daily_loss: options.max_daily_loss,
            daily_loss_scope: options.daily_loss_scope,
            manual_kill_behavior: options.manual_kill_behavior,
            session_risk_policy_version: options.session_risk_policy_version,
            risk_control_version: options.risk_control_version,
            manual_kill_active: options.manual_kill_active,
            manual_kill_cleanup_complete: options.manual_kill_cleanup_complete,
            manual_kill_activation_behavior: options.manual_kill_activation_behavior,
            manual_kill_activation_policy_version:
                options.manual_kill_activation_policy_version,
            last_risk_decision_allowed: options.last_risk_decision_allowed,
            last_risk_budget: options.last_risk_budget,
            risk_block_reason: options.risk_block_reason,
            process_ownership_ambiguous: options.process_ownership_ambiguous,
            lifecycle_status: options.lifecycle_status ?? (options.is_trading ? 'running' : 'not_started'),
            is_trading: options.is_trading ?? false,
            is_recovery_liquidation: false,
            has_open_position: options.has_open_position ?? false,
            residual_quantity: options.residual_quantity ?? '0',
            residual_cost_basis: options.residual_cost_basis ?? '0',
            balance_reconciliation: options.balance_reconciliation ?? null,
            position_average_entry_price: options.has_open_position
                ? options.position_average_entry_price ?? null
                : null,
            regime_highlight_requested: false,
            notice: null,
            unavailable_reason: null,
            error: null,
        },
        on: {
            TRADING_SNAPSHOT_SYNCHRONIZED: [
                {
                    guard: 'snapshot_is_recovery_awaiting_stop',
                    target: '.awaiting_recovered_position_liquidation_completion',
                    actions: 'synchronize_trading_snapshot',
                },
                {
                    guard: 'snapshot_is_awaiting_stop',
                    target: '.awaiting_stop_completion',
                    actions: 'synchronize_trading_snapshot',
                },
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
                            guard: 'has_recovered_position',
                        },
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
                    STOP_BUTTON_CLICKED: [
                        {
                            guard: 'has_recovered_position',
                            target: 'recovered_position_liquidation_confirmation',
                            actions: 'remember_recovery_liquidation_request',
                        },
                        {
                            actions: 'mark_not_running',
                        },
                    ],
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
                    API_CONNECTION_NOTICE_CONFIRMED: [
                        { guard: ({ context }) => context.lifecycle_status === 'running', target: 'running' },
                        { guard: ({ context }) => context.lifecycle_status === 'reconciliation_required'
                            || context.lifecycle_status === 'stopping', target: 'awaiting_stop_completion' },
                        { target: 'stopped' },
                    ],
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
                    TRADING_SNAPSHOT_SYNCHRONIZED: {
                        guard: 'snapshot_preserves_local_command',
                        actions: 'synchronize_trading_snapshot',
                    },

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
                on: {
                    TRADING_SNAPSHOT_SYNCHRONIZED: {
                        guard: 'snapshot_preserves_local_command',
                        actions: 'synchronize_trading_snapshot',
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
                    TRADING_SNAPSHOT_SYNCHRONIZED: {
                        guard: 'snapshot_preserves_local_command',
                        actions: 'synchronize_trading_snapshot',
                    },

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
                    onDone: [
                        {
                            guard: 'stop_result_is_terminal',
                            target: 'stopped',
                            actions: 'mark_trading_stopped',
                        },
                        {
                            target: 'awaiting_stop_completion',
                            actions: 'mark_stop_accepted',
                        },
                    ],
                    onError: {
                        target: 'stop_confirmation',
                        actions: 'remember_failure',
                    },
                },
                on: {
                    TRADING_SNAPSHOT_SYNCHRONIZED: {
                        guard: 'snapshot_preserves_local_command',
                        actions: 'synchronize_trading_snapshot',
                    },
                },
            },
            force_sell_confirmation: {
                meta: {
                    spec_ids: ['U2-03', 'U2-07', 'U2-09', 'U2-10', 'VR-03', 'ER-09', 'ER-10'],
                },
                on: {
                    TRADING_SNAPSHOT_SYNCHRONIZED: {
                        guard: 'snapshot_preserves_local_command',
                        actions: 'synchronize_trading_snapshot',
                    },

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
                    onDone: [
                        {
                            guard: 'stop_result_is_terminal',
                            target: 'stopped',
                            actions: ['clear_open_position', 'mark_trading_stopped'],
                        },
                        {
                            target: 'awaiting_stop_completion',
                            actions: 'mark_stop_accepted',
                        },
                    ],
                    onError: {
                        target: 'force_sell_confirmation',
                        actions: 'remember_failure',
                    },
                },
                on: {
                    TRADING_SNAPSHOT_SYNCHRONIZED: {
                        guard: 'snapshot_preserves_local_command',
                        actions: 'synchronize_trading_snapshot',
                    },
                },
            },
            // stopped 상태의 복구 Position은 일반 force-sell 확인과 분리해 사용자 동의를 보존한다.
            recovered_position_liquidation_confirmation: {
                meta: {
                    spec_ids: ['PHASE9-RECOVERED-POSITION-LIQUIDATION'],
                },
                on: {
                    RECOVERED_POSITION_LIQUIDATION_CONFIRMED: [
                        {
                            guard: 'has_recovered_position',
                            target: 'liquidating_recovered_position',
                        },
                        {
                            target: 'stopped',
                        },
                    ],
                    RECOVERED_POSITION_LIQUIDATION_CANCELED: {
                        target: 'stopped',
                    },
                    API_DISCONNECTED: {
                        target: 'api_connection_required',
                    },
                },
            },
            // 명시적 확인 뒤에도 strategy run 없이 recovery liquidation actor만 실행한다.
            liquidating_recovered_position: {
                meta: {
                    spec_ids: ['PHASE9-RECOVERED-POSITION-LIQUIDATION'],
                    pending: true,
                },
                entry: 'mark_recovery_liquidation_pending',
                invoke: {
                    id: 'recovered_position_liquidation_command',
                    src: 'liquidate_recovered_position',
                    onDone: [
                        {
                            guard: 'stop_result_is_terminal',
                            target: 'stopped',
                            actions: ['clear_open_position', 'mark_trading_stopped'],
                        },
                        {
                            target: 'awaiting_recovered_position_liquidation_completion',
                            actions: 'mark_recovery_liquidation_pending',
                        },
                    ],
                    onError: {
                        target: 'recovered_position_liquidation_confirmation',
                        actions: 'remember_failure',
                    },
                },
            },
            // Recovery liquidation은 terminal snapshot 전까지 자동매매 실행 상태로 승격하지 않는다.
            awaiting_recovered_position_liquidation_completion: {
                meta: {
                    spec_ids: ['PHASE9-RECOVERED-POSITION-LIQUIDATION'],
                    pending: true,
                },
                entry: 'mark_recovery_liquidation_pending',
                on: {
                    START_BUTTON_CLICKED: {},
                    STOP_BUTTON_CLICKED: {},
                },
            },
            disconnect_stopping: {
                // 재연결 snapshot이 도착하기 전에는 알림을 닫아도 이전 RUNNING을 복원하지 않는다.
                entry: 'mark_stop_accepted',
                meta: {
                    spec_ids: ['U3-12'],
                    pending: true,
                },
                invoke: {
                    id: 'disconnect_stop_command',
                    src: 'stop_trading',
                    onDone: {
                        target: 'api_connection_required',
                    },
                    onError: {
                        target: 'api_connection_required',
                        actions: 'remember_failure',
                    },
                },
            },
            // Backend가 stop을 수락했지만 position/pending 정리가 끝나지 않은 동안 신규 명령을 차단한다.
            awaiting_stop_completion: {
                meta: {
                    spec_ids: ['PHASE7-AUTHORITATIVE-STOP'],
                    pending: true,
                },
                entry: 'mark_stop_accepted',
                on: {
                    START_BUTTON_CLICKED: {},
                    STOP_BUTTON_CLICKED: [
                        { guard: ({ context, event }) => context.lifecycle_status === 'reconciliation_required' && event.has_open_position,
                            target: 'force_sell_confirmation', actions: 'remember_position' },
                        { guard: ({ context }) => context.lifecycle_status === 'reconciliation_required',
                            target: 'stop_confirmation', actions: 'remember_position' },
                    ],
                },
            },
        },
    });
}
