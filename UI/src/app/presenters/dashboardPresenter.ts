import {
    create_realtime_chart_view_model,
    type PriceChartIntent,
    type RealtimeChartDataRuntime,
    type RealtimeChartDataSnapshot,
} from '../../features/price-chart';
import type { RecentOrderViewModel, TraderPanelIntent } from '../../features/recent-orders';
import type { RegimePanelIntent } from '../../features/regime-selection';
import type { SplitOrderIntent } from '../../features/split-order';
import type { DashboardPageProps } from '../../routes/dashboard/DashboardPage';
import { DEFAULT_DASHBOARD_PROPS } from '../../routes/dashboard/dashboardFixture';
import type { TradeRecord } from '../../shared/contracts';
import {
    format_decimal_text,
    format_eth_quantity,
    format_quote_amount,
} from '../../shared/formatting';
import type { AppViewModel } from '../control';
import type { UiApplicationController } from '../runtime';

const DASHBOARD_ORDER_BY_ID = new Map(
    DEFAULT_DASHBOARD_PROPS.trader.orders.map((order) => [order.id, order]),
);

const STRATEGY_LABEL_BY_REGIME: Readonly<Record<NonNullable<AppViewModel['regime']['applied']>, string>> = {
    type0: 'Basic Iterative',
    type1: 'First Buy',
    type2: 'Momentum',
    type3: 'Risk Off',
    type4: 'Defensive',
};

/**
 * 함수 이름: format_recent_trade_time()
 * 기능: actor의 ISO 체결 시각을 최근 체결 카드의 KST 시각으로 표시한다.
 * 인자: occurred_at -> ISO 체결 시각
 * 반환값: HH:mm:ss 형식의 KST 시각
 * 작성 날짜: 2026/08/12
 */
function format_recent_trade_time(occurred_at: string): string {
    return new Intl.DateTimeFormat('ko-KR', {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
        timeZone: 'Asia/Seoul',
    }).format(new Date(occurred_at));
}

/**
 * 함수 이름: create_recent_order_view_model()
 * 기능: 최근 체결 actor record를 Figma 행 fixture 또는 안전한 기본 표시 모델로 변환한다.
 * 인자: trade_record -> actor가 보관한 정규화 체결 기록
 * 반환값: TraderPanel 최근 체결 행 ViewModel
 * 작성 날짜: 2026/08/12
 */
function create_recent_order_view_model(trade_record: TradeRecord): RecentOrderViewModel {
    const fixture_order = trade_record.quote_asset === undefined
        ? DASHBOARD_ORDER_BY_ID.get(trade_record.id)
        : undefined;

    if (fixture_order !== undefined) {
        return fixture_order;
    }

    // Demo fixture의 기존 KRW 간격은 보존하고 live record는 authoritative quote asset을 표시한다.
    return {
        id: trade_record.id,
        side: trade_record.side,
        strategy: trade_record.strategy,
        time: format_recent_trade_time(trade_record.occurred_at),
        price: trade_record.quote_asset === undefined
            ? `₩ ${format_decimal_text(trade_record.price)}`
            : format_quote_amount(trade_record.price, trade_record.quote_asset),
        secondaryValue: trade_record.side === 'buy'
            ? `${format_eth_quantity(trade_record.quantity)} ETH`
            : trade_record.profit_rate === null
                ? '-'
                : `${format_decimal_text(trade_record.profit_rate)}%`,
    };
}

/**
 * 함수 이름: handle_chart_intent()
 * 기능: PriceChart Boundary intent를 facade의 Event-Action intent로 변환한다.
 * 인자: intent -> 차트 컴포넌트 intent, view_model -> 현재 차트 상태, controller -> UI actor controller
 * 반환값: 없음
 * 작성 날짜: 2026/08/12
 */
function handle_chart_intent(
    intent: PriceChartIntent,
    view_model: AppViewModel,
    controller: UiApplicationController,
): void {
    switch (intent.type) {
        case 'CHART_INTERVAL_REQUESTED':
            controller.dispatch({
                type: 'CHART_INTERVAL_SELECTED',
                interval: intent.interval,
            });
            break;
        case 'INDICATOR_SETTINGS_REQUESTED':
            controller.dispatch({ type: 'CHART_INDICATOR_SETTINGS_TOGGLED' });
            break;
        case 'INDICATOR_SETTINGS_CLOSED':
            controller.dispatch({ type: 'CHART_INDICATOR_SETTINGS_OUTSIDE_CLICKED' });
            break;
        case 'INDICATOR_VISIBILITY_REQUESTED':
            controller.dispatch({
                type: 'CHART_INDICATOR_CHANGED',
                indicator: intent.indicator === 'bollingerBand'
                    ? 'bollinger_bands'
                    : intent.indicator,
                is_visible: intent.visible,
            });
            break;
        case 'DRAWING_MODE_REQUESTED':
            controller.dispatch({ type: 'CHART_DRAWING_TOOL_CLICKED' });
            break;
        case 'DRAWING_STARTED':
            controller.dispatch({ type: 'CHART_DRAWING_STARTED' });
            break;
        case 'DRAWING_FINISHED':
            controller.dispatch({
                type: 'CHART_DRAWING_FINISHED',
                drawing: intent.drawing,
            });
            break;
        case 'DRAWING_CANCELED':
            controller.dispatch({ type: 'CHART_DRAWING_CANCELED' });
            break;
        case 'CHART_FULLSCREEN_REQUESTED':
            controller.dispatch({
                type: 'CHART_FULLSCREEN_CHANGED',
                is_fullscreen: !view_model.chart.is_fullscreen,
            });
            break;
        case 'DRAWING_LINE_HOVER_ENTERED':
            controller.dispatch({
                type: 'CHART_LINE_HOVER_ENTERED',
                line_id: intent.lineId,
            });
            break;
        case 'DRAWING_LINE_HOVER_EXITED':
            controller.dispatch({ type: 'CHART_LINE_HOVER_EXITED' });
            break;
        case 'DRAWING_LINE_CONTEXT_MENU_REQUESTED':
            controller.dispatch({
                type: 'CHART_LINE_CONTEXT_MENU_REQUESTED',
                x: intent.x,
                y: intent.y,
            });
            break;
        case 'DRAWING_LINE_DELETE_REQUESTED':
            controller.dispatch({ type: 'CHART_LINE_DELETE_REQUESTED' });
            break;
        case 'DRAWING_LINE_CONTEXT_MENU_CLOSED':
            controller.dispatch({ type: 'CHART_LINE_CONTEXT_MENU_OUTSIDE_CLICKED' });
            break;
    }
}

/**
 * 함수 이름: handle_trader_panel_intent()
 * 기능: 트레이딩 패널 탭과 전체 보기 intent를 소유 actor 또는 route actor로 전달한다.
 * 인자: intent -> TraderPanel Boundary intent, controller -> UI actor controller
 * 반환값: 없음
 * 작성 날짜: 2026/08/12
 */
function handle_trader_panel_intent(
    intent: TraderPanelIntent,
    controller: UiApplicationController,
): void {
    if (intent.type === 'ALL_ORDERS_REQUESTED') {
        controller.dispatch({ type: 'SHOW_TRADE_HISTORY' });
        return;
    }

    controller.dispatch({
        type: intent.tab === 'recent'
            ? 'RECENT_ORDERS_TAB_SELECTED'
            : 'REALTIME_INDICATORS_TAB_SELECTED',
    });
}

/**
 * 함수 이름: handle_regime_intent()
 * 기능: REGIME Boundary의 후보 선택 intent를 regime actor로 전달한다.
 * 인자: intent -> REGIME 패널 intent, controller -> UI actor controller
 * 반환값: 없음
 * 작성 날짜: 2026/08/12
 */
function handle_regime_intent(
    intent: RegimePanelIntent,
    controller: UiApplicationController,
): void {
    controller.dispatch({
        type: 'REGIME_TYPE_CLICKED',
        regime: intent.regime,
    });
}

/**
 * 함수 이름: handle_split_order_intent()
 * 기능: 분할 매수·매도 Boundary intent를 해당 비율 actor event로 전달한다.
 * 인자: intent -> 분할 주문 intent, controller -> UI actor controller
 * 반환값: 없음
 * 작성 날짜: 2026/08/12
 */
function handle_split_order_intent(
    intent: SplitOrderIntent,
    controller: UiApplicationController,
): void {
    controller.dispatch({
        type: intent.side === 'buy' ? 'SCALE_IN_CHANGED' : 'SCALE_OUT_CHANGED',
        percentage: intent.percentage,
    });
}

/**
 * 함수 이름: present_dashboard_props()
 * 기능: 단일 AppViewModel을 정적 Figma fixture와 결합해 제어형 DashboardPage props로 투영한다.
 * 인자: view_model -> facade의 최신 화면 모델
 *      controller -> 사용자 intent 전달 controller
 *      market_snapshot -> 실시간 Binance 시장 snapshot
 * 반환값: 모든 대시보드 feature Boundary에 전달할 props
 * 작성 날짜: 2026/08/20
 */
export function present_dashboard_props(
    view_model: AppViewModel,
    controller: UiApplicationController,
    market_snapshot?: RealtimeChartDataSnapshot | RealtimeChartDataRuntime,
): DashboardPageProps {
    const applied_strategy = view_model.regime.applied === null
        ? '선택 필요'
        : STRATEGY_LABEL_BY_REGIME[view_model.regime.applied];
    const strategy_status = view_model.connection.is_online
        ? view_model.trading.is_trading
            ? '정상 작동'
            : '매매 중지'
        : 'API 연결 대기';
    const strategy_status_tone = view_model.connection.is_online && view_model.trading.is_trading
        ? 'positive' as const
        : 'negative' as const;
    const fixture_indicator_groups = DEFAULT_DASHBOARD_PROPS.trader.indicatorGroups;
    const primary_indicator_group = fixture_indicator_groups[0];
    const dynamic_indicator_groups = primary_indicator_group === undefined
        ? fixture_indicator_groups
        : [
            {
                ...primary_indicator_group,
                indicators: view_model.trader_panel.realtime_indicators.map((indicator, index) => ({
                    id: primary_indicator_group.indicators[index]?.id ?? indicator.id,
                    label: indicator.label,
                    tone: indicator.tone,
                    value: indicator.value,
                })),
            },
            ...fixture_indicator_groups.slice(1),
        ];
    const realtime_chart_view_model = market_snapshot === undefined
        || market_snapshot.data_status === 'idle'
        ? null
        : create_realtime_chart_view_model(market_snapshot, view_model.chart.interval);
    const realtime_chart_runtime = market_snapshot !== undefined
        && 'history_load_state_by_interval' in market_snapshot
        ? market_snapshot
        : null;
    const history_load_state = realtime_chart_runtime
        ?.history_load_state_by_interval[view_model.chart.interval];

    return {
        regime: {
            recommended: view_model.regime.recommended,
            applied: view_model.regime.applied,
            candidate: view_model.regime.candidate,
            logicCoverage: view_model.regime.logic_coverage,
            metrics: view_model.regime.metrics,
            disabled: view_model.regime.is_pending,
            highlight: view_model.regime.is_highlighted,
            onIntent: (intent) => handle_regime_intent(intent, controller),
        },
        chart: {
            ...DEFAULT_DASHBOARD_PROPS.chart,
            ...(realtime_chart_view_model ?? {}),
            ...(history_load_state === undefined || realtime_chart_runtime === null
                ? {}
                : {
                    historyErrorMessage: history_load_state.error_message,
                    historyExhausted: history_load_state.is_exhausted,
                    historyLoading: history_load_state.is_loading,
                    onLoadEarlier: () => {
                        realtime_chart_runtime.load_earlier_klines(view_model.chart.interval);
                    },
                }),
            activeState: applied_strategy,
            // ETHUSDT의 열린 포지션 평단가만 전달하고 캔들·최근 체결 가격으로 대체하지 않는다.
            position_average_entry_price: view_model.trading.has_open_position
                && realtime_chart_view_model?.symbol === 'ETHUSDT'
                ? view_model.trading.position_average_entry_price
                : null,
            interval: view_model.chart.interval,
            isFullscreen: view_model.chart.is_fullscreen,
            drawingActive: view_model.chart.drawing_mode !== 'deactivated',
            drawings: view_model.chart.drawings,
            selectedLineId: view_model.chart.selected_line_id,
            lineContextMenuOpen: view_model.chart.line_selection_state === 'context_menu',
            contextMenuPosition: view_model.chart.context_menu_position,
            indicatorSettingsOpen: view_model.chart.is_indicator_settings_open,
            indicatorSettings: {
                ema9: view_model.chart.indicators.ema9,
                bollingerBand: view_model.chart.indicators.bollinger_bands,
                volume: view_model.chart.indicators.volume,
            },
            onIntent: (intent) => handle_chart_intent(intent, view_model, controller),
        },
        trader: {
            ...DEFAULT_DASHBOARD_PROPS.trader,
            activeTab: view_model.trader_panel.active_tab === 'recent_orders'
                ? 'recent'
                : 'realtime',
            configured_risk_policy_version:
                view_model.trading.configured_risk_policy_version,
            max_order_notional: view_model.trading.max_order_notional,
            max_position_notional: view_model.trading.max_position_notional,
            max_daily_loss: view_model.trading.max_daily_loss,
            daily_loss_scope: view_model.trading.daily_loss_scope,
            manual_kill_behavior: view_model.trading.manual_kill_behavior,
            last_risk_decision_allowed: view_model.trading.last_risk_decision_allowed,
            last_risk_budget: view_model.trading.last_risk_budget,
            manual_kill_active: view_model.trading.manual_kill_active,
            manual_kill_cleanup_complete:
                view_model.trading.manual_kill_cleanup_complete,
            manual_kill_activation_behavior:
                view_model.trading.manual_kill_activation_behavior,
            manual_kill_activation_policy_version:
                view_model.trading.manual_kill_activation_policy_version,
            orders: view_model.trader_panel.trades.map(create_recent_order_view_model),
            process_ownership_ambiguous: view_model.trading.process_ownership_ambiguous,
            risk_block_reason: view_model.trading.risk_block_reason,
            risk_policy_availability: view_model.trading.risk_policy_availability,
            session_risk_policy_version: view_model.trading.session_risk_policy_version,
            indicatorGroups: dynamic_indicator_groups,
            onIntent: (intent) => handle_trader_panel_intent(intent, controller),
        },
        account: {
            asset: view_model.account_summary.asset,
            strategy: {
                ...view_model.account_summary.strategy,
                status: strategy_status,
                statusTone: strategy_status_tone,
                appliedState: applied_strategy,
            },
        },
        splitOrder: {
            buyPercentage: view_model.split_order.scale_in_percentage,
            sellPercentage: view_model.split_order.scale_out_percentage,
            onIntent: (intent) => handle_split_order_intent(intent, controller),
        },
    };
}
