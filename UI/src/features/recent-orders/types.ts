// 최근 주문과 실시간 지표에서 사용하는 데이터 구조와 입력·표시 타입을 정의한다.

import type {
    BackendDailyLossScope,
    BackendDecimalString,
    BackendManualKillBehavior,
    BackendRiskBudgetSnapshot,
    BackendRiskPolicyAvailability,
    BackendTradingTimer,
} from '../../shared/contracts';

export type TraderPanelTab = 'recent' | 'realtime';

export type OrderSide = 'buy' | 'sell';

export type MetricTone = 'positive' | 'negative' | 'neutral';

export type TraderPanelRiskPolicyAvailability = BackendRiskPolicyAvailability;

export type TraderPanelIntent =
    | { readonly type: 'TRADER_TAB_REQUESTED'; readonly tab: TraderPanelTab }
    | { readonly type: 'ALL_ORDERS_REQUESTED' };

export interface RecentOrderViewModel {
    readonly id: string;
    readonly price: string;
    readonly secondaryValue: string;
    readonly side: OrderSide;
    readonly strategy: string;
    readonly time: string;
}

export interface RealtimeIndicatorViewModel {
    readonly id: string;
    readonly label: string;
    readonly tone: MetricTone;
    readonly value: string;
    readonly criterion?: string;
    readonly timer?: RealtimeIndicatorTimerViewModel;
}

export interface RealtimeIndicatorTimerViewModel {
    readonly snapshot: BackendTradingTimer | null;
    readonly server_time: string | null;
    readonly received_at: number | null;
}

export interface RealtimeIndicatorGroupViewModel {
    readonly id: string;
    readonly indicators: ReadonlyArray<RealtimeIndicatorViewModel>;
    readonly title: string;
    readonly phase?: string;
    readonly phase_description?: string;
    readonly notice?: string | undefined;
}

export interface TraderPanelProps {
    readonly activeTab: TraderPanelTab;
    readonly configured_risk_policy_version?: number | null | undefined;
    readonly max_order_notional?: BackendDecimalString | null | undefined;
    readonly max_position_notional?: BackendDecimalString | null | undefined;
    readonly max_daily_loss?: BackendDecimalString | null | undefined;
    readonly daily_loss_scope?: BackendDailyLossScope | null | undefined;
    readonly manual_kill_behavior?: BackendManualKillBehavior | null | undefined;
    readonly indicatorGroups: ReadonlyArray<RealtimeIndicatorGroupViewModel>;
    readonly last_risk_decision_allowed?: boolean | null | undefined;
    readonly last_risk_budget?: BackendRiskBudgetSnapshot | null | undefined;
    readonly manual_kill_active?: boolean | undefined;
    readonly manual_kill_cleanup_complete?: boolean | undefined;
    readonly manual_kill_activation_behavior?:
        BackendManualKillBehavior | null | undefined;
    readonly manual_kill_activation_policy_version?: number | null | undefined;
    readonly onIntent?: ((intent: TraderPanelIntent) => void) | undefined;
    readonly orders: ReadonlyArray<RecentOrderViewModel>;
    readonly process_ownership_ambiguous?: boolean | undefined;
    readonly risk_block_reason?: string | null | undefined;
    readonly risk_policy_availability?: TraderPanelRiskPolicyAvailability | undefined;
    readonly session_risk_policy_version?: number | null | undefined;
}
