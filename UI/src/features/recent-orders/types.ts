export type TraderPanelTab = 'recent' | 'realtime';

export type OrderSide = 'buy' | 'sell';

export type MetricTone = 'positive' | 'negative' | 'neutral';

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
}

export interface RealtimeIndicatorGroupViewModel {
    readonly id: string;
    readonly indicators: ReadonlyArray<RealtimeIndicatorViewModel>;
    readonly title: string;
}

export interface TraderPanelProps {
    readonly activeTab: TraderPanelTab;
    readonly indicatorGroups: ReadonlyArray<RealtimeIndicatorGroupViewModel>;
    readonly onIntent?: ((intent: TraderPanelIntent) => void) | undefined;
    readonly orders: ReadonlyArray<RecentOrderViewModel>;
    readonly residenceTime?: string | undefined;
}
