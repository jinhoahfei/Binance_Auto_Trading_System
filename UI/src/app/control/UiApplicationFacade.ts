import {
    createActor,
    type ActorRefFrom,
} from 'xstate';
import {
    create_ui_shell_machine,
    type UiModalKind,
} from '../machines';
import {
    create_account_summary_machine,
    type AssetSummaryViewModel,
    type StrategySummaryViewModel,
} from '../../features/account-summary';
import { create_app_exit_machine } from '../../features/app-exit';
import { create_connection_machine } from '../../features/connection-status';
import { create_csv_export_machine } from '../../features/csv-export';
import { create_chart_machine } from '../../features/price-chart';
import { create_recent_orders_machine } from '../../features/recent-orders';
import { create_regime_machine } from '../../features/regime-selection';
import { create_split_order_machine } from '../../features/split-order';
import {
    create_trade_history_machine,
    create_trade_history_summary_machine,
    type TradeHistorySummaryViewModel,
} from '../../features/trade-history';
import { create_trading_command_machine } from '../../features/trading-control';
import type {
    ChartDrawing,
    ChartInterval,
    CsvPeriod,
    HistoryPeriod,
    LocalDateString,
    RegimeMetric,
    RegimeType,
    TradeRecord,
    TradeSideFilter,
    UiCommandFailure,
} from '../../shared/contracts';
import type { UiCommandPort } from '../../shared/ports';

type UiShellActor = ActorRefFrom<ReturnType<typeof create_ui_shell_machine>>;
type AccountSummaryActor = ActorRefFrom<ReturnType<typeof create_account_summary_machine>>;
type AppExitActor = ActorRefFrom<ReturnType<typeof create_app_exit_machine>>;
type ConnectionActor = ActorRefFrom<ReturnType<typeof create_connection_machine>>;
type CsvExportActor = ActorRefFrom<ReturnType<typeof create_csv_export_machine>>;
type ChartActor = ActorRefFrom<ReturnType<typeof create_chart_machine>>;
type RecentOrdersActor = ActorRefFrom<ReturnType<typeof create_recent_orders_machine>>;
type RegimeActor = ActorRefFrom<ReturnType<typeof create_regime_machine>>;
type SplitOrderActor = ActorRefFrom<ReturnType<typeof create_split_order_machine>>;
type TradeHistoryActor = ActorRefFrom<ReturnType<typeof create_trade_history_machine>>;
type TradeHistorySummaryActor = ActorRefFrom<ReturnType<typeof create_trade_history_summary_machine>>;
type TradingCommandActor = ActorRefFrom<ReturnType<typeof create_trading_command_machine>>;

interface ActorSubscription {
    unsubscribe(): void;
}

/**
 * facade가 생성하고 수명주기를 함께 관리하는 feature actor registry이다.
 */
export interface UiApplicationActors {
    readonly shell: UiShellActor;
    readonly account_summary: AccountSummaryActor;
    readonly app_exit: AppExitActor;
    readonly connection: ConnectionActor;
    readonly csv_export: CsvExportActor;
    readonly chart: ChartActor;
    readonly recent_orders: RecentOrdersActor;
    readonly regime: RegimeActor;
    readonly split_order: SplitOrderActor;
    readonly trade_history: TradeHistoryActor;
    readonly trade_history_summary: TradeHistorySummaryActor;
    readonly trading: TradingCommandActor;
}

/**
 * 모든 feature actor의 같은 시점 화면 projection 원본이다.
 */
export interface UiApplicationSnapshot {
    readonly shell: ReturnType<UiShellActor['getSnapshot']>;
    readonly account_summary: ReturnType<AccountSummaryActor['getSnapshot']>;
    readonly app_exit: ReturnType<AppExitActor['getSnapshot']>;
    readonly connection: ReturnType<ConnectionActor['getSnapshot']>;
    readonly csv_export: ReturnType<CsvExportActor['getSnapshot']>;
    readonly chart: ReturnType<ChartActor['getSnapshot']>;
    readonly recent_orders: ReturnType<RecentOrdersActor['getSnapshot']>;
    readonly regime: ReturnType<RegimeActor['getSnapshot']>;
    readonly split_order: ReturnType<SplitOrderActor['getSnapshot']>;
    readonly trade_history: ReturnType<TradeHistoryActor['getSnapshot']>;
    readonly trade_history_summary: ReturnType<TradeHistorySummaryActor['getSnapshot']>;
    readonly trading: ReturnType<TradingCommandActor['getSnapshot']>;
}

/**
 * UiApplicationFacade 초기 actor context를 결정적으로 구성하는 옵션이다.
 */
export interface UiApplicationFacadeOptions {
    readonly today: LocalDateString;
    readonly trading_symbol?: string;
    readonly csv_default_file_name?: string;
    readonly chart_interval?: ChartInterval;
    readonly chart_indicators?: {
        readonly bollinger_bands?: boolean;
        readonly ema9?: boolean;
        readonly volume?: boolean;
    };
    readonly recommended_regime?: RegimeType | null;
    readonly applied_regime?: RegimeType | null;
    readonly regime_metrics?: ReadonlyArray<RegimeMetric>;
    readonly recent_trades?: ReadonlyArray<TradeRecord>;
    readonly history_records?: ReadonlyArray<TradeRecord>;
    readonly scale_in_percentage?: number;
    readonly scale_out_percentage?: number;
    readonly account_strategy?: StrategySummaryViewModel;
    readonly account_asset?: AssetSummaryViewModel;
    readonly trade_history_summary?: TradeHistorySummaryViewModel;
    readonly is_trading?: boolean;
    readonly has_open_position?: boolean;
}

/**
 * backend의 한 coherent snapshot에서 UI actor가 소유할 server state만 추출한 계약이다.
 */
export interface UiServerOwnedSnapshot {
    readonly last_sequence: number;
    readonly trading_symbol: string;
    readonly recommended_regime: RegimeType | null;
    readonly applied_regime: RegimeType | null;
    readonly regime_metrics: ReadonlyArray<RegimeMetric>;
    readonly recent_trades: ReadonlyArray<TradeRecord>;
    readonly history_records: ReadonlyArray<TradeRecord>;
    readonly scale_in_percentage?: number;
    readonly scale_out_percentage?: number;
    readonly account_strategy: StrategySummaryViewModel;
    readonly account_asset: AssetSummaryViewModel;
    readonly trade_history_summary: TradeHistorySummaryViewModel;
    readonly is_trading: boolean;
    readonly has_open_position?: boolean;
    readonly trading_state_label: string;
}

/**
 * React Boundary가 facade에 전달할 수 있는 사용자 의도와 backend event의 통합 계약이다.
 */
export type UiApplicationIntent =
    | {
        readonly type: 'BACKEND_SNAPSHOT_SYNCHRONIZED';
        readonly snapshot: UiServerOwnedSnapshot;
    }
    | { readonly type: 'CONNECT_REQUESTED' }
    | { readonly type: 'RECONNECT_REQUESTED' }
    | { readonly type: 'API_CONNECTED'; readonly sequence?: number }
    | { readonly type: 'API_DISCONNECTED'; readonly reason?: string }
    | { readonly type: 'RECONNECT_FAILED'; readonly reason: string }
    | { readonly type: 'START_TRADING_CLICKED' }
    | { readonly type: 'START_TRADING_CONFIRMED' }
    | { readonly type: 'START_TRADING_CANCELED' }
    | { readonly type: 'SELECT_REGIME_NOTICE_CONFIRMED' }
    | { readonly type: 'SELECT_REGIME_NOTICE_CLOSED' }
    | { readonly type: 'API_CONNECTION_NOTICE_CONFIRMED' }
    | { readonly type: 'STOP_TRADING_CLICKED'; readonly has_open_position: boolean }
    | { readonly type: 'STOP_TRADING_CONFIRMED' }
    | { readonly type: 'STOP_TRADING_CANCELED' }
    | { readonly type: 'FORCE_SELL_AND_STOP_CONFIRMED' }
    | { readonly type: 'FORCE_SELL_AND_STOP_CANCELED' }
    | { readonly type: 'BACKEND_TRADING_STARTED' }
    | { readonly type: 'BACKEND_TRADING_STOPPED' }
    | { readonly type: 'POSITION_UPDATED'; readonly has_open_position: boolean }
    | {
        readonly type: 'ACCOUNT_STRATEGY_UPDATED';
        readonly strategy: StrategySummaryViewModel;
    }
    | {
        readonly type: 'ACCOUNT_ASSETS_UPDATED';
        readonly asset: AssetSummaryViewModel;
    }
    | { readonly type: 'REGIME_TYPE_CLICKED'; readonly regime: RegimeType }
    | { readonly type: 'REGIME_CHANGE_CONFIRMED' }
    | { readonly type: 'REGIME_CHANGE_CANCELED' }
    | { readonly type: 'REGIME_RECOMMENDED'; readonly regime: RegimeType }
    | { readonly type: 'REGIME_APPLIED'; readonly regime: RegimeType }
    | { readonly type: 'REGIME_INDICATORS_UPDATED'; readonly metrics: ReadonlyArray<RegimeMetric> }
    | { readonly type: 'REGIME_HIGHLIGHT_COMPLETED' }
    | { readonly type: 'CHART_INTERVAL_SELECTED'; readonly interval: ChartInterval }
    | { readonly type: 'CHART_INDICATOR_SETTINGS_TOGGLED' }
    | { readonly type: 'CHART_INDICATOR_SETTINGS_OUTSIDE_CLICKED' }
    | {
        readonly type: 'CHART_INDICATOR_CHANGED';
        readonly indicator: 'bollinger_bands' | 'ema9' | 'volume';
        readonly is_visible: boolean;
    }
    | { readonly type: 'CHART_FULLSCREEN_CHANGED'; readonly is_fullscreen: boolean }
    | { readonly type: 'CHART_DRAWING_TOOL_CLICKED' }
    | { readonly type: 'CHART_DRAWING_STARTED' }
    | { readonly type: 'CHART_DRAWING_FINISHED'; readonly drawing: ChartDrawing }
    | { readonly type: 'CHART_DRAWING_CANCELED' }
    | { readonly type: 'CHART_ACTIVE_STATE_UPDATED'; readonly state_label: string }
    | { readonly type: 'CHART_LINE_HOVER_ENTERED'; readonly line_id: string }
    | { readonly type: 'CHART_LINE_HOVER_EXITED' }
    | {
        readonly type: 'CHART_LINE_CONTEXT_MENU_REQUESTED';
        readonly x?: number;
        readonly y?: number;
    }
    | { readonly type: 'CHART_LINE_DELETE_REQUESTED' }
    | { readonly type: 'CHART_LINE_CONTEXT_MENU_OUTSIDE_CLICKED' }
    | { readonly type: 'RECENT_ORDERS_TAB_SELECTED' }
    | { readonly type: 'REALTIME_INDICATORS_TAB_SELECTED' }
    | { readonly type: 'REALTIME_INDICATORS_UPDATED'; readonly indicators: ReadonlyArray<RegimeMetric> }
    | { readonly type: 'BUY_ORDER_EXECUTED'; readonly trade: TradeRecord }
    | { readonly type: 'SELL_ORDER_EXECUTED'; readonly trade: TradeRecord }
    | { readonly type: 'SCALE_IN_CHANGED'; readonly percentage: number }
    | { readonly type: 'SCALE_OUT_CHANGED'; readonly percentage: number }
    | { readonly type: 'SHOW_TRADE_HISTORY' }
    | { readonly type: 'BACK_TO_DASHBOARD' }
    | { readonly type: 'REFRESH_TRADE_HISTORY' }
    | { readonly type: 'HISTORY_PERIOD_SELECTED'; readonly period: HistoryPeriod }
    | { readonly type: 'HISTORY_SIDE_SELECTED'; readonly side: TradeSideFilter }
    | {
        readonly type: 'TRADE_HISTORY_PROFIT_RATE_UPDATED';
        readonly daily_return: TradeHistorySummaryViewModel['dailyReturn'];
    }
    | {
        readonly type: 'TRADE_HISTORY_SELL_SUMMARY_UPDATED';
        readonly sell_performance: TradeHistorySummaryViewModel['sellPerformance'];
        readonly position: TradeHistorySummaryViewModel['position'];
    }
    | {
        readonly type: 'TRADE_HISTORY_BUY_HOLDINGS_UPDATED';
        readonly position: TradeHistorySummaryViewModel['position'];
    }
    | {
        readonly type: 'TRADE_HISTORY_DAILY_FEE_UPDATED';
        readonly fees: TradeHistorySummaryViewModel['fees'];
    }
    | {
        readonly type: 'TRADE_HISTORY_PERFORMANCE_UPDATED';
        readonly daily_return: TradeHistorySummaryViewModel['dailyReturn'];
        readonly sell_performance: TradeHistorySummaryViewModel['sellPerformance'];
        readonly fees: TradeHistorySummaryViewModel['fees'];
    }
    | { readonly type: 'OPEN_CSV_EXPORT' }
    | { readonly type: 'CLOSE_CSV_EXPORT' }
    | { readonly type: 'CSV_DIALOG_OUTSIDE_CLICKED' }
    | { readonly type: 'CSV_DIRECTORY_SELECT_CLICKED' }
    | { readonly type: 'CSV_PERIOD_SELECTED'; readonly period: CsvPeriod }
    | { readonly type: 'CSV_START_CALENDAR_OPENED' }
    | { readonly type: 'CSV_END_CALENDAR_OPENED' }
    | { readonly type: 'CSV_CALENDAR_OUTSIDE_CLICKED' }
    | { readonly type: 'CSV_START_DATE_SELECTED'; readonly date: LocalDateString }
    | { readonly type: 'CSV_END_DATE_SELECTED'; readonly date: LocalDateString }
    | { readonly type: 'CSV_FILE_NAME_EDIT_STARTED' }
    | { readonly type: 'CSV_FILE_NAME_CHANGED'; readonly file_name: string }
    | { readonly type: 'CSV_FILE_NAME_COMMITTED' }
    | { readonly type: 'CSV_EXPORT_SUBMITTED' }
    | { readonly type: 'CSV_EXPORT_ERROR_CONFIRMED' }
    | { readonly type: 'CSV_EXPORT_COMPLETE_CONFIRMED' }
    | { readonly type: 'APP_EXIT_CLICKED' }
    | { readonly type: 'APP_EXIT_CONFIRMED' }
    | { readonly type: 'APP_EXIT_CANCELED' }
    | { readonly type: 'FORCE_SELL_EXIT_CONFIRMED' }
    | { readonly type: 'FORCE_SELL_EXIT_CANCELED' };

/**
 * React 컴포넌트가 업무 분기 없이 바로 렌더링할 수 있는 애플리케이션 화면 모델이다.
 */
export interface AppViewModel {
    readonly route: 'dashboard' | 'trade_history';
    readonly active_modal: UiModalKind | null;
    readonly connection: {
        readonly status: 'offline' | 'connecting' | 'online' | 'reconnecting';
        readonly is_online: boolean;
        readonly is_pending: boolean;
        readonly reconnect_attempt: number;
        readonly error: string | null;
    };
    readonly trading: {
        readonly is_trading: boolean;
        readonly is_pending: boolean;
        readonly has_open_position: boolean;
        readonly error: UiCommandFailure | null;
    };
    readonly regime: {
        readonly recommended: RegimeType | null;
        readonly applied: RegimeType | null;
        readonly candidate: RegimeType | null;
        readonly is_highlighted: boolean;
        readonly is_pending: boolean;
        readonly metrics: ReadonlyArray<RegimeMetric>;
        readonly error: UiCommandFailure | null;
    };
    readonly chart: {
        readonly interval: ChartInterval;
        readonly indicators: {
            readonly bollinger_bands: boolean;
            readonly ema9: boolean;
            readonly volume: boolean;
        };
        readonly is_indicator_settings_open: boolean;
        readonly is_fullscreen: boolean;
        readonly drawing_mode: 'deactivated' | 'waiting' | 'drawing';
        readonly active_trading_logic_state: string;
        readonly selected_line_id: string | null;
        readonly line_selection_state: 'awaiting_selection' | 'highlighted' | 'context_menu';
        readonly context_menu_position: { readonly x: number; readonly y: number } | null;
        readonly drawings: ReadonlyArray<ChartDrawing>;
    };
    readonly trader_panel: {
        readonly active_tab: 'recent_orders' | 'realtime_indicators';
        readonly trades: ReadonlyArray<TradeRecord>;
        readonly realtime_indicators: ReadonlyArray<RegimeMetric>;
    };
    readonly split_order: {
        readonly scale_in_percentage: number;
        readonly scale_out_percentage: number;
        readonly is_pending: boolean;
        readonly error: UiCommandFailure | null;
    };
    readonly account_summary: {
        readonly strategy: StrategySummaryViewModel;
        readonly asset: AssetSummaryViewModel;
    };
    readonly trade_history: {
        readonly symbol: string;
        readonly period: HistoryPeriod;
        readonly side: TradeSideFilter;
        readonly status: 'idle' | 'loading' | 'ready' | 'empty' | 'failed';
        readonly is_loading: boolean;
        readonly records: ReadonlyArray<TradeRecord>;
        readonly error: UiCommandFailure | null;
        readonly summary: TradeHistorySummaryViewModel;
    };
    readonly csv_export: {
        readonly status: 'closed' | 'editing' | 'picking_directory' | 'exporting' | 'complete' | 'error';
        readonly is_open: boolean;
        readonly is_pending: boolean;
        readonly directory: string | null;
        readonly period: CsvPeriod;
        readonly start_date: LocalDateString | null;
        readonly end_date: LocalDateString | null;
        readonly file_name: string;
        readonly file_name_draft: string;
        readonly calendar_target: 'start_date' | 'end_date' | null;
        readonly validation_errors: {
            readonly directory: string | null;
            readonly file_name: string | null;
            readonly date_range: string | null;
        };
        readonly command_error: UiCommandFailure | null;
        readonly receipt_path: string | null;
    };
    readonly app_exit: {
        readonly status: 'awaiting_exit' | 'force_sell_exit_confirmation' | 'force_selling' | 'exit_confirmation' | 'shutting_down' | 'ui_final_state';
        readonly is_pending: boolean;
        readonly is_final: boolean;
        readonly error: UiCommandFailure | null;
    };
}

/**
 * 함수 이름: select_app_view_model()
 * 기능: feature actor snapshot 묶음을 React가 직접 렌더링할 단일 읽기 전용 ViewModel로 변환한다.
 * 인자: snapshot -> UiApplicationFacade가 제공한 전체 actor snapshot
 * 반환값: 애플리케이션 화면 모델
 * 작성 날짜: 2026/08/12
 */
export function select_app_view_model(snapshot: UiApplicationSnapshot): AppViewModel {
    const history_status = snapshot.trade_history.value as AppViewModel['trade_history']['status'];
    const csv_status = snapshot.csv_export.matches({
        editing: { file_browser: 'opened' },
    })
        ? 'picking_directory'
        : typeof snapshot.csv_export.value === 'string'
            ? snapshot.csv_export.value
            : 'editing';
    const exit_status = snapshot.app_exit.value as AppViewModel['app_exit']['status'];

    return {
        route: snapshot.shell.context.route,
        active_modal: snapshot.shell.context.active_modal,
        connection: {
            status: snapshot.connection.context.status,
            is_online: snapshot.connection.matches('api_online'),
            is_pending: snapshot.connection.matches('connecting')
                || snapshot.connection.matches('reconnecting'),
            reconnect_attempt: snapshot.connection.context.reconnect_attempt,
            error: snapshot.connection.context.last_error,
        },
        trading: {
            is_trading: snapshot.trading.context.is_trading,
            is_pending: snapshot.trading.matches('starting')
                || snapshot.trading.matches('stopping')
                || snapshot.trading.matches('force_selling')
                || snapshot.trading.matches('disconnect_stopping'),
            has_open_position: snapshot.trading.context.has_open_position,
            error: snapshot.trading.context.error,
        },
        regime: {
            recommended: snapshot.regime.context.recommended_regime,
            applied: snapshot.regime.context.applied_regime,
            candidate: snapshot.regime.context.candidate_regime,
            is_highlighted: snapshot.regime.context.is_highlighted,
            is_pending: snapshot.regime.matches('applying'),
            metrics: snapshot.regime.context.metrics,
            error: snapshot.regime.context.error,
        },
        chart: {
            interval: snapshot.chart.context.interval,
            indicators: snapshot.chart.context.indicators,
            is_indicator_settings_open: snapshot.chart.matches({ indicator_settings: 'opened' }),
            is_fullscreen: snapshot.chart.context.is_fullscreen,
            drawing_mode: snapshot.chart.context.drawing_mode,
            active_trading_logic_state: snapshot.chart.context.active_trading_logic_state,
            selected_line_id: snapshot.chart.context.selected_line_id,
            context_menu_position: snapshot.chart.context.context_menu_position,
            line_selection_state: snapshot.chart.matches({ line_selection: 'context_menu' })
                ? 'context_menu'
                : snapshot.chart.matches({ line_selection: 'highlighted' })
                    ? 'highlighted'
                    : 'awaiting_selection',
            drawings: snapshot.chart.context.drawings[snapshot.chart.context.interval],
        },
        trader_panel: {
            active_tab: snapshot.recent_orders.matches('trade_history_displayed')
                ? 'recent_orders'
                : 'realtime_indicators',
            trades: snapshot.recent_orders.context.trades,
            realtime_indicators: snapshot.recent_orders.context.realtime_indicators,
        },
        split_order: {
            scale_in_percentage: snapshot.split_order.context.scale_in_percentage,
            scale_out_percentage: snapshot.split_order.context.scale_out_percentage,
            is_pending: snapshot.split_order.matches('saving'),
            error: snapshot.split_order.context.error,
        },
        account_summary: {
            strategy: snapshot.account_summary.context.strategy,
            asset: snapshot.account_summary.context.asset,
        },
        trade_history: {
            symbol: snapshot.trade_history.context.symbol,
            period: snapshot.trade_history.context.period,
            side: snapshot.trade_history.context.side,
            status: history_status,
            is_loading: snapshot.trade_history.matches('loading'),
            records: snapshot.trade_history.context.records,
            error: snapshot.trade_history.context.error,
            summary: snapshot.trade_history_summary.context.summary,
        },
        csv_export: {
            status: csv_status as AppViewModel['csv_export']['status'],
            is_open: !snapshot.csv_export.matches('closed'),
            is_pending: snapshot.csv_export.matches({
                editing: { file_browser: 'opened' },
            })
                || snapshot.csv_export.matches('exporting'),
            directory: snapshot.csv_export.context.directory,
            period: snapshot.csv_export.context.period,
            start_date: snapshot.csv_export.context.start_date,
            end_date: snapshot.csv_export.context.end_date,
            file_name: snapshot.csv_export.context.file_name,
            file_name_draft: snapshot.csv_export.context.file_name_draft,
            calendar_target: snapshot.csv_export.context.calendar_target,
            validation_errors: snapshot.csv_export.context.validation_errors,
            command_error: snapshot.csv_export.context.command_error,
            receipt_path: snapshot.csv_export.context.receipt?.file_path ?? null,
        },
        app_exit: {
            status: exit_status,
            is_pending: snapshot.app_exit.matches('force_selling')
                || snapshot.app_exit.matches('shutting_down'),
            is_final: snapshot.app_exit.matches('ui_final_state'),
            error: snapshot.app_exit.context.error,
        },
    };
}

/**
 * 클래스 이름: UiApplicationFacade
 * 기능: Boundary intent를 기능별 XState actor event로 변환하고 전체 snapshot과 modal 단일성을 조정한다.
 * 작성 날짜: 2026/08/12
 */
export class UiApplicationFacade {
    readonly actors: UiApplicationActors;

    private readonly listeners = new Set<(snapshot: UiApplicationSnapshot) => void>();
    private readonly actor_subscriptions: Array<ActorSubscription> = [];
    private is_started = false;
    private notification_batch_depth = 0;
    private notification_is_pending = false;

    /**
     * 함수 이름: UiApplicationFacade.constructor()
     * 기능: 공통 command port와 초기 fixture를 주입해 모든 feature actor를 생성한다.
     * 인자: command_port -> backend·desktop 명령 port, options -> 결정적 초기 UI 데이터
     * 반환값: 생성된 UiApplicationFacade 인스턴스
     * 작성 날짜: 2026/08/12
     */
    constructor(command_port: UiCommandPort, options: UiApplicationFacadeOptions) {
        this.actors = {
            shell: createActor(create_ui_shell_machine()),
            account_summary: createActor(create_account_summary_machine({
                ...(options.account_strategy === undefined
                    ? {}
                    : { strategy: options.account_strategy }),
                ...(options.account_asset === undefined
                    ? {}
                    : { asset: options.account_asset }),
            })),
            app_exit: createActor(create_app_exit_machine(command_port)),
            connection: createActor(create_connection_machine()),
            csv_export: createActor(create_csv_export_machine(command_port, {
                today: options.today,
                ...(options.csv_default_file_name === undefined
                    ? {}
                    : { default_file_name: options.csv_default_file_name }),
            })),
            chart: createActor(create_chart_machine({
                ...(options.chart_interval === undefined
                    ? {}
                    : { interval: options.chart_interval }),
                ...(options.chart_indicators === undefined
                    ? {}
                    : { indicators: options.chart_indicators }),
            })),
            recent_orders: createActor(create_recent_orders_machine({
                ...(options.recent_trades === undefined
                    ? {}
                    : { trades: options.recent_trades }),
                ...(options.regime_metrics === undefined
                    ? {}
                    : { realtime_indicators: options.regime_metrics }),
            })),
            regime: createActor(create_regime_machine(command_port, {
                ...(options.recommended_regime === undefined
                    ? {}
                    : { recommended_regime: options.recommended_regime }),
                ...(options.applied_regime === undefined
                    ? {}
                    : { applied_regime: options.applied_regime }),
                ...(options.regime_metrics === undefined
                    ? {}
                    : { metrics: options.regime_metrics }),
            })),
            split_order: createActor(create_split_order_machine(command_port, {
                ...(options.scale_in_percentage === undefined
                    ? {}
                    : { scale_in_percentage: options.scale_in_percentage }),
                ...(options.scale_out_percentage === undefined
                    ? {}
                    : { scale_out_percentage: options.scale_out_percentage }),
            })),
            trade_history: createActor(create_trade_history_machine(command_port, {
                ...(options.trading_symbol === undefined
                    ? {}
                    : { symbol: options.trading_symbol }),
                ...(options.history_records === undefined
                    ? {}
                    : { records: options.history_records }),
            })),
            trade_history_summary: createActor(create_trade_history_summary_machine({
                ...(options.trade_history_summary === undefined
                    ? {}
                    : { summary: options.trade_history_summary }),
            })),
            trading: createActor(create_trading_command_machine(command_port, {
                ...(options.is_trading === undefined
                    ? {}
                    : { is_trading: options.is_trading }),
                ...(options.has_open_position === undefined
                    ? {}
                    : { has_open_position: options.has_open_position }),
            })),
        };

        Object.values(this.actors).forEach((actor) => {
            this.actor_subscriptions.push(actor.subscribe(() => this.handle_actor_update()));
        });
    }

    /**
     * 함수 이름: start()
     * 기능: shell과 모든 feature actor를 시작하고 최초 ViewModel snapshot을 발행한다.
     * 인자: 없음
     * 반환값: 없음
     * 작성 날짜: 2026/08/12
     */
    start(): void {
        if (this.is_started) {
            return;
        }

        Object.values(this.actors).forEach((actor) => actor.start());
        this.is_started = true;
        this.reconcile_modal_slot();
        this.notify_listeners();
    }

    /**
     * 함수 이름: stop()
     * 기능: 모든 feature actor 실행과 내부 subscription을 안전하게 종료한다.
     * 인자: 없음
     * 반환값: 없음
     * 작성 날짜: 2026/08/12
     */
    stop(): void {
        if (!this.is_started) {
            return;
        }

        this.is_started = false;
        Object.values(this.actors).forEach((actor) => actor.stop());
        this.actor_subscriptions.splice(0).forEach((subscription) => {
            subscription.unsubscribe();
        });
        this.listeners.clear();
    }

    /**
     * 함수 이름: dispatch()
     * 기능: React Boundary의 typed intent를 소유 feature actor event로 전달하고 global modal 충돌을 차단한다.
     * 인자: intent -> 사용자 의도 또는 backend 상태 event
     * 반환값: intent가 수락되면 true, modal 정책 또는 현재 상태로 거절되면 false
     * 작성 날짜: 2026/08/12
     */
    dispatch(intent: UiApplicationIntent): boolean {
        switch (intent.type) {
            case 'BACKEND_SNAPSHOT_SYNCHRONIZED': {
                this.synchronize_server_owned_snapshot(intent.snapshot);
                break;
            }
            case 'CONNECT_REQUESTED':
                this.actors.connection.send({ type: 'CONNECT_REQUESTED' });
                break;
            case 'RECONNECT_REQUESTED':
                this.actors.connection.send({ type: 'RECONNECT_REQUESTED' });
                break;
            case 'API_CONNECTED':
                this.actors.connection.send(intent.sequence === undefined
                    ? { type: 'API_CONNECTED' }
                    : { type: 'API_CONNECTED', sequence: intent.sequence });
                break;
            case 'API_DISCONNECTED':
                this.actors.connection.send(intent.reason === undefined
                    ? { type: 'API_DISCONNECTED' }
                    : { type: 'API_DISCONNECTED', reason: intent.reason });
                this.actors.trading.send({ type: 'API_DISCONNECTED' });
                break;
            case 'RECONNECT_FAILED':
                this.actors.connection.send(intent);
                break;
            case 'START_TRADING_CLICKED': {
                const regime = this.actors.regime.getSnapshot().context.applied_regime;
                const is_online = this.actors.connection.getSnapshot().matches('api_online');
                const modal = regime === null
                    ? 'select_regime_notice'
                    : is_online
                        ? 'start_confirmation'
                        : 'api_connection_required';

                if (!this.can_open_modal(modal)) {
                    return false;
                }

                this.actors.trading.send({
                    type: 'START_BUTTON_CLICKED',
                    regime,
                    is_online,
                });
                break;
            }
            case 'START_TRADING_CONFIRMED':
                this.actors.trading.send({
                    type: 'START_CONFIRMED',
                    is_online: this.actors.connection.getSnapshot().matches('api_online'),
                });
                break;
            case 'START_TRADING_CANCELED':
                this.actors.trading.send({ type: 'START_CANCELED' });
                break;
            case 'SELECT_REGIME_NOTICE_CONFIRMED':
                this.actors.trading.send({ type: 'SELECT_REGIME_NOTICE_CONFIRMED' });
                this.actors.regime.send({ type: 'HIGHLIGHT_REQUESTED' });
                break;
            case 'SELECT_REGIME_NOTICE_CLOSED':
                this.actors.trading.send({ type: 'SELECT_REGIME_NOTICE_CLOSED' });
                break;
            case 'API_CONNECTION_NOTICE_CONFIRMED':
                this.actors.trading.send({ type: 'API_CONNECTION_NOTICE_CONFIRMED' });
                break;
            case 'STOP_TRADING_CLICKED': {
                const modal = intent.has_open_position
                    ? 'force_sell_stop_confirmation'
                    : 'stop_confirmation';

                if (!this.can_open_modal(modal)) {
                    return false;
                }

                this.actors.trading.send({
                    type: 'STOP_BUTTON_CLICKED',
                    has_open_position: intent.has_open_position,
                });
                break;
            }
            case 'STOP_TRADING_CONFIRMED':
                this.actors.trading.send({ type: 'STOP_CONFIRMED' });
                break;
            case 'STOP_TRADING_CANCELED':
                this.actors.trading.send({ type: 'STOP_CANCELED' });
                break;
            case 'FORCE_SELL_AND_STOP_CONFIRMED':
                this.actors.trading.send({ type: 'FORCE_SELL_AND_STOP_CONFIRMED' });
                break;
            case 'FORCE_SELL_AND_STOP_CANCELED':
                this.actors.trading.send({ type: 'FORCE_SELL_AND_STOP_CANCELED' });
                break;
            case 'BACKEND_TRADING_STARTED':
            case 'BACKEND_TRADING_STOPPED':
            case 'POSITION_UPDATED':
                this.actors.trading.send(intent);
                break;
            case 'ACCOUNT_STRATEGY_UPDATED':
                this.actors.account_summary.send({
                    type: 'TRADING_STATUS_UPDATED',
                    strategy: intent.strategy,
                });
                break;
            case 'ACCOUNT_ASSETS_UPDATED':
                this.actors.account_summary.send({
                    type: 'ASSET_SUMMARY_UPDATED',
                    asset: intent.asset,
                });
                break;
            case 'REGIME_TYPE_CLICKED':
                if (!this.can_open_modal('regime_change_confirmation')) {
                    return false;
                }
                this.actors.regime.send({ type: 'TYPE_CLICKED', regime: intent.regime });
                break;
            case 'REGIME_CHANGE_CONFIRMED':
                this.actors.regime.send({ type: 'CONFIRM_TYPE_CHANGE' });
                break;
            case 'REGIME_CHANGE_CANCELED':
                this.actors.regime.send({ type: 'CANCEL_TYPE_CHANGE' });
                break;
            case 'REGIME_RECOMMENDED':
                this.actors.regime.send({ type: 'TYPE_RECOMMENDED', regime: intent.regime });
                break;
            case 'REGIME_APPLIED':
                this.actors.regime.send({ type: 'REGIME_APPLIED', regime: intent.regime });
                break;
            case 'REGIME_INDICATORS_UPDATED':
                this.actors.regime.send({
                    type: 'REGIME_INDICATOR_UPDATED',
                    metrics: intent.metrics,
                });
                break;
            case 'REGIME_HIGHLIGHT_COMPLETED':
                this.actors.regime.send({ type: 'HIGHLIGHT_COMPLETED' });
                break;
            case 'CHART_INTERVAL_SELECTED':
                this.dispatch_chart_interval(intent.interval);
                break;
            case 'CHART_INDICATOR_SETTINGS_TOGGLED':
                this.actors.chart.send({ type: 'INDICATOR_SETTINGS_BUTTON_CLICKED' });
                break;
            case 'CHART_INDICATOR_SETTINGS_OUTSIDE_CLICKED':
                this.actors.chart.send({ type: 'INDICATOR_POPUP_OUTSIDE_CLICKED' });
                break;
            case 'CHART_INDICATOR_CHANGED':
                this.dispatch_chart_indicator(intent.indicator, intent.is_visible);
                break;
            case 'CHART_FULLSCREEN_CHANGED':
                this.actors.chart.send({
                    type: intent.is_fullscreen ? 'FULL_SIZE_SELECTED' : 'NORMAL_SIZE_SELECTED',
                });
                break;
            case 'CHART_DRAWING_TOOL_CLICKED':
                this.actors.chart.send({ type: 'DRAWING_TOOL_CLICKED' });
                break;
            case 'CHART_DRAWING_STARTED':
                this.actors.chart.send({ type: 'USER_START_DRAWING' });
                break;
            case 'CHART_DRAWING_FINISHED':
                this.actors.chart.send({ type: 'USER_FINISH_DRAWING', drawing: intent.drawing });
                break;
            case 'CHART_DRAWING_CANCELED':
                this.actors.chart.send({ type: 'DRAWING_CANCELED' });
                break;
            case 'CHART_ACTIVE_STATE_UPDATED':
                this.actors.chart.send({
                    type: 'TRADING_LOGIC_STATE_CHANGED',
                    state_label: intent.state_label,
                });
                break;
            case 'CHART_LINE_HOVER_ENTERED':
                this.actors.chart.send({
                    type: 'CURSOR_HOVER_ENTER',
                    line_id: intent.line_id,
                });
                break;
            case 'CHART_LINE_HOVER_EXITED':
                this.actors.chart.send({ type: 'CURSOR_HOVER_EXIT' });
                break;
            case 'CHART_LINE_CONTEXT_MENU_REQUESTED':
                this.actors.chart.send({
                    type: 'HIGHLIGHTED_LINE_RIGHT_CLICKED',
                    ...(intent.x === undefined ? {} : { x: intent.x }),
                    ...(intent.y === undefined ? {} : { y: intent.y }),
                });
                break;
            case 'CHART_LINE_DELETE_REQUESTED':
                this.actors.chart.send({ type: 'DELETE_LINE' });
                break;
            case 'CHART_LINE_CONTEXT_MENU_OUTSIDE_CLICKED':
                this.actors.chart.send({ type: 'CONTEXT_MENU_OUTSIDE_CLICKED' });
                break;
            case 'RECENT_ORDERS_TAB_SELECTED':
                this.actors.recent_orders.send({ type: 'TRADING_HISTORY_CLICKED' });
                break;
            case 'REALTIME_INDICATORS_TAB_SELECTED':
                this.actors.recent_orders.send({ type: 'REALTIME_INDICATOR_CLICKED' });
                break;
            case 'REALTIME_INDICATORS_UPDATED':
                this.actors.recent_orders.send({
                    type: 'REALTIME_INDICATOR_UPDATED',
                    indicators: intent.indicators,
                });
                break;
            case 'BUY_ORDER_EXECUTED':
            case 'SELL_ORDER_EXECUTED':
                this.actors.recent_orders.send(intent);
                break;
            case 'SCALE_IN_CHANGED':
                this.actors.split_order.send({
                    type: 'SCALE_IN_LEVEL_CHANGED',
                    percentage: intent.percentage,
                });
                break;
            case 'SCALE_OUT_CHANGED':
                this.actors.split_order.send({
                    type: 'SCALE_OUT_LEVEL_CHANGED',
                    percentage: intent.percentage,
                });
                break;
            case 'SHOW_TRADE_HISTORY':
                this.actors.shell.send({ type: 'SHOW_ALL_TRADING_DETAILS' });
                this.actors.trade_history.send({ type: 'ENTER_TRADE_HISTORY' });
                break;
            case 'BACK_TO_DASHBOARD':
                this.actors.shell.send({ type: 'BACK_TO_MAIN_SCREEN' });
                break;
            case 'REFRESH_TRADE_HISTORY':
                this.actors.trade_history.send({ type: 'REFRESH_TRADE_HISTORY' });
                break;
            case 'HISTORY_PERIOD_SELECTED':
                this.dispatch_history_period(intent.period);
                break;
            case 'HISTORY_SIDE_SELECTED':
                this.dispatch_history_side(intent.side);
                break;
            case 'TRADE_HISTORY_PROFIT_RATE_UPDATED':
                this.actors.trade_history_summary.send({
                    type: 'PROFIT_RATE_UPDATED',
                    daily_return: intent.daily_return,
                });
                break;
            case 'TRADE_HISTORY_SELL_SUMMARY_UPDATED':
                this.actors.trade_history_summary.send({
                    type: 'SELL_ORDER_EXECUTED',
                    sell_performance: intent.sell_performance,
                    position: intent.position,
                });
                break;
            case 'TRADE_HISTORY_BUY_HOLDINGS_UPDATED':
                this.actors.trade_history_summary.send({
                    type: 'BUY_ORDER_EXECUTED',
                    position: intent.position,
                });
                break;
            case 'TRADE_HISTORY_DAILY_FEE_UPDATED':
                this.actors.trade_history_summary.send({
                    type: 'DAILY_TRADING_FEE_CHANGED',
                    fees: intent.fees,
                });
                break;
            case 'TRADE_HISTORY_PERFORMANCE_UPDATED':
                this.actors.trade_history_summary.send({
                    type: 'PERFORMANCE_SNAPSHOT_UPDATED',
                    daily_return: intent.daily_return,
                    sell_performance: intent.sell_performance,
                    fees: intent.fees,
                });
                break;
            case 'OPEN_CSV_EXPORT':
                if (!this.can_open_modal('csv_export')) {
                    return false;
                }
                this.actors.csv_export.send({ type: 'CSV_EXPORT_CLICKED' });
                break;
            case 'CLOSE_CSV_EXPORT':
                this.actors.csv_export.send({ type: 'CLOSE_CSV_EXPORT_POPUP' });
                break;
            case 'CSV_DIALOG_OUTSIDE_CLICKED':
                this.actors.csv_export.send({ type: 'CSV_DIALOG_OUTSIDE_CLICKED' });
                break;
            case 'CSV_DIRECTORY_SELECT_CLICKED':
                this.actors.csv_export.send({ type: 'SAVE_LOCATION_SELECT_CLICKED' });
                break;
            case 'CSV_PERIOD_SELECTED':
                this.dispatch_csv_period(intent.period);
                break;
            case 'CSV_START_CALENDAR_OPENED':
                this.actors.csv_export.send({ type: 'START_CSV_START_DATE_SELECTION' });
                break;
            case 'CSV_END_CALENDAR_OPENED':
                this.actors.csv_export.send({ type: 'START_CSV_FINISH_DATE_SELECTION' });
                break;
            case 'CSV_CALENDAR_OUTSIDE_CLICKED': {
                const calendar_target = this.actors.csv_export.getSnapshot().context.calendar_target;
                this.actors.csv_export.send({
                    type: calendar_target === 'start_date'
                        ? 'START_DATE_CALENDAR_OUTSIDE_CLICKED'
                        : 'FINISH_DATE_CALENDAR_OUTSIDE_CLICKED',
                });
                break;
            }
            case 'CSV_START_DATE_SELECTED':
                this.actors.csv_export.send({ type: 'START_DATE_SELECTED', date: intent.date });
                break;
            case 'CSV_END_DATE_SELECTED':
                this.actors.csv_export.send({ type: 'FINISH_DATE_SELECTED', date: intent.date });
                break;
            case 'CSV_FILE_NAME_EDIT_STARTED':
                this.actors.csv_export.send({ type: 'FILE_NAME_CLICKED' });
                break;
            case 'CSV_FILE_NAME_CHANGED':
                this.actors.csv_export.send({
                    type: 'FILE_NAME_CHANGED',
                    file_name: intent.file_name,
                });
                break;
            case 'CSV_FILE_NAME_COMMITTED':
                this.actors.csv_export.send({ type: 'FILE_NAME_INPUT_FOCUS_LOST' });
                break;
            case 'CSV_EXPORT_SUBMITTED':
                this.actors.csv_export.send({ type: 'EXPORT_CSV' });
                break;
            case 'CSV_EXPORT_ERROR_CONFIRMED':
                this.actors.csv_export.send({ type: 'CSV_EXPORT_ERROR_CONFIRMED' });
                break;
            case 'CSV_EXPORT_COMPLETE_CONFIRMED':
                this.actors.csv_export.send({ type: 'ACCEPT_CLOSE_ALL_POPUP' });
                break;
            case 'APP_EXIT_CLICKED':
                this.actors.app_exit.send({
                    type: 'EXIT_CLICKED',
                    has_open_position: this.actors.trading.getSnapshot().context.has_open_position,
                });
                break;
            case 'APP_EXIT_CONFIRMED':
                this.actors.app_exit.send({ type: 'EXIT_CONFIRMED' });
                break;
            case 'APP_EXIT_CANCELED':
                this.actors.app_exit.send({ type: 'EXIT_CANCELED' });
                break;
            case 'FORCE_SELL_EXIT_CONFIRMED':
                this.actors.app_exit.send({ type: 'FORCE_SELL_EXIT_CONFIRMED' });
                break;
            case 'FORCE_SELL_EXIT_CANCELED':
                this.actors.app_exit.send({ type: 'FORCE_SELL_EXIT_CANCELED' });
                break;
        }

        return true;
    }

    /**
     * 함수 이름: subscribe()
     * 기능: feature actor 중 하나가 바뀔 때마다 전체 snapshot을 구독자에게 전달한다.
     * 인자: listener -> 전체 snapshot 변경 callback
     * 반환값: 구독 해제 함수
     * 작성 날짜: 2026/08/12
     */
    subscribe(listener: (snapshot: UiApplicationSnapshot) => void): () => void {
        this.listeners.add(listener);
        listener(this.get_snapshot());

        return () => {
            this.listeners.delete(listener);
        };
    }

    /**
     * 함수 이름: get_snapshot()
     * 기능: 모든 feature actor의 최신 snapshot을 하나의 읽기 전용 객체로 반환한다.
     * 인자: 없음
     * 반환값: 전체 애플리케이션 snapshot
     * 작성 날짜: 2026/08/12
     */
    get_snapshot(): UiApplicationSnapshot {
        return {
            shell: this.actors.shell.getSnapshot(),
            account_summary: this.actors.account_summary.getSnapshot(),
            app_exit: this.actors.app_exit.getSnapshot(),
            connection: this.actors.connection.getSnapshot(),
            csv_export: this.actors.csv_export.getSnapshot(),
            chart: this.actors.chart.getSnapshot(),
            recent_orders: this.actors.recent_orders.getSnapshot(),
            regime: this.actors.regime.getSnapshot(),
            split_order: this.actors.split_order.getSnapshot(),
            trade_history: this.actors.trade_history.getSnapshot(),
            trade_history_summary: this.actors.trade_history_summary.getSnapshot(),
            trading: this.actors.trading.getSnapshot(),
        };
    }

    /**
     * 함수 이름: get_view_model()
     * 기능: 현재 actor snapshot을 즉시 React 렌더링용 ViewModel로 변환한다.
     * 인자: 없음
     * 반환값: 최신 애플리케이션 화면 모델
     * 작성 날짜: 2026/08/12
     */
    get_view_model(): AppViewModel {
        return select_app_view_model(this.get_snapshot());
    }

    /**
     * 함수 이름: handle_actor_update()
     * 기능: actor 상태 변경 시 modal slot을 동기화한 뒤 외부 구독자에게 알린다.
     * 인자: 없음
     * 반환값: 없음
     * 작성 날짜: 2026/08/12
     */
    private handle_actor_update(): void {
        if (!this.is_started) {
            return;
        }

        if (this.notification_batch_depth > 0) {
            this.notification_is_pending = true;
            return;
        }

        if (!this.reconcile_modal_slot()) {
            this.notify_listeners();
        }
    }

    /**
     * 함수 이름: notify_listeners()
     * 기능: 현재 전체 snapshot을 등록된 모든 facade 구독자에게 발행한다.
     * 인자: 없음
     * 반환값: 없음
     * 작성 날짜: 2026/08/12
     */
    private notify_listeners(): void {
        const snapshot = this.get_snapshot();

        this.listeners.forEach((listener) => listener(snapshot));
    }

    /**
     * 함수 이름: synchronize_server_owned_snapshot()
     * 기능: 한 coherent backend snapshot의 actor별 값을 중간 publish 없이 원자적으로 교체한다.
     * 인자: synchronized_snapshot -> mapper가 검증하고 UI 표시 계약으로 변환한 전체 snapshot
     * 반환값: 없음
     * 작성 날짜: 2026/08/21
     */
    private synchronize_server_owned_snapshot(
        synchronized_snapshot: UiServerOwnedSnapshot,
    ): void {
        // 열린 사용자 확인 modal은 유지하되 그 안에서 표시할 authoritative context는 갱신한다.
        const active_modal = this.actors.shell.getSnapshot().context.active_modal;
        const preserve_regime_interaction = active_modal === 'regime_change_confirmation';
        const preserve_trading_interaction = active_modal === 'select_regime_notice'
            || active_modal === 'api_connection_required'
            || active_modal === 'start_confirmation'
            || active_modal === 'stop_confirmation'
            || active_modal === 'force_sell_stop_confirmation';

        this.notification_batch_depth += 1;

        try {
            // UI-local route와 chart 설정은 건드리지 않고 server-owned context만 교체한다.
            this.actors.account_summary.send({
                type: 'ACCOUNT_SUMMARY_SYNCHRONIZED',
                strategy: synchronized_snapshot.account_strategy,
                asset: synchronized_snapshot.account_asset,
            });
            this.actors.regime.send({
                type: preserve_regime_interaction
                    ? 'REGIME_SNAPSHOT_CONTEXT_SYNCHRONIZED'
                    : 'REGIME_SNAPSHOT_SYNCHRONIZED',
                recommended_regime: synchronized_snapshot.recommended_regime,
                applied_regime: synchronized_snapshot.applied_regime,
                metrics: synchronized_snapshot.regime_metrics,
            });
            this.actors.recent_orders.send({
                type: 'RECENT_ORDERS_SNAPSHOT_SYNCHRONIZED',
                trades: synchronized_snapshot.recent_trades,
                indicators: synchronized_snapshot.regime_metrics,
            });
            this.actors.trade_history.send({
                type: 'TRADE_HISTORY_SNAPSHOT_SYNCHRONIZED',
                symbol: synchronized_snapshot.trading_symbol,
                records: synchronized_snapshot.history_records,
            });
            this.actors.trade_history_summary.send({
                type: 'TRADE_HISTORY_SUMMARY_SYNCHRONIZED',
                summary: synchronized_snapshot.trade_history_summary,
            });
            // Phase 5 snapshot에 없는 split ratio는 UI-local 현재값을 임의 기본값으로 덮지 않는다.
            if (synchronized_snapshot.scale_in_percentage !== undefined
                && synchronized_snapshot.scale_out_percentage !== undefined) {
                this.actors.split_order.send({
                    type: 'SPLIT_ORDER_SNAPSHOT_SYNCHRONIZED',
                    scale_in_percentage: synchronized_snapshot.scale_in_percentage,
                    scale_out_percentage: synchronized_snapshot.scale_out_percentage,
                });
            }

            // Position 소유 backend field가 없는 동안 trading status만 authoritative하게 교체한다.
            this.actors.trading.send({
                type: preserve_trading_interaction
                    ? 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED'
                    : 'TRADING_SNAPSHOT_SYNCHRONIZED',
                is_trading: synchronized_snapshot.is_trading,
                has_open_position: synchronized_snapshot.has_open_position
                    ?? this.actors.trading.getSnapshot().context.has_open_position,
            });
            this.actors.chart.send({
                type: 'TRADING_LOGIC_STATE_CHANGED',
                state_label: synchronized_snapshot.trading_state_label,
            });
            this.actors.connection.send({
                type: 'API_CONNECTED',
                sequence: synchronized_snapshot.last_sequence,
            });
            this.reconcile_modal_slot();
            this.notification_is_pending = true;
        } finally {
            this.notification_batch_depth -= 1;
        }

        if (this.notification_batch_depth === 0 && this.notification_is_pending) {
            this.notification_is_pending = false;
            this.notify_listeners();
        }
    }

    /**
     * 함수 이름: can_open_modal()
     * 기능: 전역 modal slot이 비어 있거나 같은 modal이 이미 활성인지 검사한다.
     * 인자: modal -> 열려고 하는 modal 종류
     * 반환값: modal intent 허용 여부
     * 작성 날짜: 2026/08/12
     */
    private can_open_modal(modal: UiModalKind): boolean {
        const active_modal = this.actors.shell.getSnapshot().context.active_modal;

        return active_modal === null || active_modal === modal;
    }

    /**
     * 함수 이름: reconcile_modal_slot()
     * 기능: feature actor의 확인·진행 상태로부터 전역 modal 하나를 결정해 shell과 동기화한다.
     * 인자: 없음
     * 반환값: shell actor event를 전송했으면 true
     * 작성 날짜: 2026/08/12
     */
    private reconcile_modal_slot(): boolean {
        const desired_modal = this.derive_active_modal();
        const active_modal = this.actors.shell.getSnapshot().context.active_modal;

        if (desired_modal === active_modal) {
            return false;
        }

        if (desired_modal === null) {
            this.actors.shell.send({ type: 'CLOSE_MODAL' });
        } else if (active_modal === null) {
            this.actors.shell.send({ type: 'OPEN_MODAL', modal: desired_modal });
        } else {
            this.actors.shell.send({ type: 'REPLACE_MODAL', modal: desired_modal });
        }

        return true;
    }

    /**
     * 함수 이름: derive_active_modal()
     * 기능: exit, trading, REGIME, CSV 순서의 안전 우선순위로 활성 modal 종류를 선택한다.
     * 인자: 없음
     * 반환값: 표시할 modal 종류 또는 null
     * 작성 날짜: 2026/08/12
     */
    private derive_active_modal(): UiModalKind | null {
        const exit_snapshot = this.actors.app_exit.getSnapshot();
        const trading_snapshot = this.actors.trading.getSnapshot();
        const regime_snapshot = this.actors.regime.getSnapshot();
        const csv_snapshot = this.actors.csv_export.getSnapshot();

        if (exit_snapshot.matches('force_sell_exit_confirmation')) {
            return 'force_sell_exit_confirmation';
        }
        if (exit_snapshot.matches('exit_confirmation')) {
            return 'exit_confirmation';
        }
        if (exit_snapshot.matches('force_selling') || exit_snapshot.matches('shutting_down')) {
            return 'exit_processing';
        }
        if (trading_snapshot.matches('select_regime_notice')) {
            return 'select_regime_notice';
        }
        if (trading_snapshot.matches('api_connection_required')
            || trading_snapshot.matches('disconnect_stopping')) {
            return 'api_connection_required';
        }
        if (trading_snapshot.matches('start_confirmation') || trading_snapshot.matches('starting')) {
            return 'start_confirmation';
        }
        if (trading_snapshot.matches('stop_confirmation') || trading_snapshot.matches('stopping')) {
            return 'stop_confirmation';
        }
        if (trading_snapshot.matches('force_sell_confirmation')
            || trading_snapshot.matches('force_selling')) {
            return 'force_sell_stop_confirmation';
        }
        if (regime_snapshot.matches('type_change_confirmation') || regime_snapshot.matches('applying')) {
            return 'regime_change_confirmation';
        }
        if (csv_snapshot.matches('exporting')) {
            return 'csv_export_progress';
        }
        if (csv_snapshot.matches('complete')) {
            return 'csv_export_complete';
        }
        if (csv_snapshot.matches('error')) {
            return 'csv_export_error';
        }
        if (!csv_snapshot.matches('closed')) {
            return 'csv_export';
        }

        return null;
    }

    /**
     * 함수 이름: dispatch_chart_interval()
     * 기능: 공통 차트 주기 값을 Event-Action 표의 구체 event로 변환한다.
     * 인자: interval -> 선택한 차트 주기
     * 반환값: 없음
     * 작성 날짜: 2026/08/12
     */
    private dispatch_chart_interval(interval: ChartInterval): void {
        const event_by_interval = {
            '1m': { type: '1_M_BUTTON_CLICKED' } as const,
            '30m': { type: '30_M_BUTTON_CLICKED' } as const,
            '4h': { type: '4_H_BUTTON_CLICKED' } as const,
            '1d': { type: '1_DAY_BUTTON_CLICKED' } as const,
        };

        this.actors.chart.send(event_by_interval[interval]);
    }

    /**
     * 함수 이름: dispatch_chart_indicator()
     * 기능: 지표 이름과 ON/OFF 값을 차트 machine의 명세 event로 변환한다.
     * 인자: indicator -> 변경할 지표, is_visible -> 표시 여부
     * 반환값: 없음
     * 작성 날짜: 2026/08/12
     */
    private dispatch_chart_indicator(
        indicator: 'bollinger_bands' | 'ema9' | 'volume',
        is_visible: boolean,
    ): void {
        if (indicator === 'bollinger_bands') {
            this.actors.chart.send({
                type: is_visible ? 'BB_DISPLAY_ON_CLICKED' : 'BB_DISPLAY_OFF_CLICKED',
            });
        } else if (indicator === 'ema9') {
            this.actors.chart.send({
                type: is_visible ? 'EMA_DISPLAY_ON_CLICKED' : 'EMA_DISPLAY_OFF_CLICKED',
            });
        } else {
            this.actors.chart.send({
                type: is_visible ? 'VOLUME_DISPLAY_ON_CLICKED' : 'VOLUME_DISPLAY_OFF_CLICKED',
            });
        }
    }

    /**
     * 함수 이름: dispatch_history_period()
     * 기능: 공통 거래 기간을 Event-Action 표의 구체 filter event로 변환한다.
     * 인자: period -> 선택한 거래 내역 기간
     * 반환값: 없음
     * 작성 날짜: 2026/08/12
     */
    private dispatch_history_period(period: HistoryPeriod): void {
        const event_by_period = {
            today: { type: 'SELECT_DISPLAY_TODAY_HISTORY' } as const,
            last7days: { type: 'SELECT_DISPLAY_WEEKLY_HISTORY' } as const,
            last30days: { type: 'SELECT_DISPLAY_MONTHLY_HISTORY' } as const,
            all: { type: 'SELECT_DISPLAY_ALL_HISTORY' } as const,
        };

        this.actors.trade_history.send(event_by_period[period]);
    }

    /**
     * 함수 이름: dispatch_history_side()
     * 기능: 공통 거래 방향을 Event-Action 표의 구체 filter event로 변환한다.
     * 인자: side -> 선택한 거래 방향
     * 반환값: 없음
     * 작성 날짜: 2026/08/12
     */
    private dispatch_history_side(side: TradeSideFilter): void {
        const event_by_side = {
            all: { type: 'ALL_TRADE_HISTORY_SELECTED' } as const,
            buy: { type: 'BUY_TRADE_HISTORY_SELECTED' } as const,
            sell: { type: 'SELL_TRADE_HISTORY_SELECTED' } as const,
        };

        this.actors.trade_history.send(event_by_side[side]);
    }

    /**
     * 함수 이름: dispatch_csv_period()
     * 기능: 공통 CSV 기간을 Event-Action 표의 구체 preset event로 변환한다.
     * 인자: period -> 선택한 CSV 내보내기 기간
     * 반환값: 없음
     * 작성 날짜: 2026/08/12
     */
    private dispatch_csv_period(period: CsvPeriod): void {
        const event_by_period = {
            today: { type: 'SELECT_CSV_TODAY_HISTORY' } as const,
            last7days: { type: 'SELECT_CSV_WEEKLY_HISTORY' } as const,
            last30days: { type: 'SELECT_CSV_MONTHLY_HISTORY' } as const,
            custom: { type: 'SELECT_CSV_DATE' } as const,
        };

        this.actors.csv_export.send(event_by_period[period]);
    }
}
