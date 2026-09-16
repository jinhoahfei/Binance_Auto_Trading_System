import { resolve_trading_start_unavailable_reason } from '../../features/trading-control';
import type {
    ChartInterval,
    HistoryPeriod,
    TradeSideFilter,
    CsvPeriod,
} from '../../shared/contracts';
import type { UiModalKind } from './uiModalTypes';
import type { UiApplicationIntent, UiServerOwnedSnapshot } from './uiApplicationContracts';
import type {
    UiApplicationSnapshot,
    UiDomainEvent,
    FeatureKey,
} from '../machines/uiApplicationTypes';
import { feature_view } from '../machines/uiApplicationViews';
import { derive_active_modal } from './uiModalPolicy';


/**
 * 함수 이름: is_intent_active()
 * 기능: 루트 수명과 현재 화면을 기준으로 사용자 intent의 수신 가능 여부를 검사한다.
 * 인자: snapshot -> UI 루트 snapshot, intent -> 검사할 외부 입력
 * 반환값: 현재 화면에서 처리 가능한 입력이면 true
 * 작성 날짜: 2026/09/16
 */
function is_intent_active(snapshot: UiApplicationSnapshot, intent: UiApplicationIntent): boolean {
    if (snapshot.status === 'done' || snapshot.status === 'stopped') {
        return false;
    }

    const route = feature_view(snapshot, 'shell').context.route;
    const is_main_screen_intent = /^(CHART_|SCALE_|RECENT_ORDERS_TAB_|REALTIME_INDICATORS_TAB_SELECTED|REGIME_TYPE_CLICKED|REGIME_CHANGE_|SHOW_TRADE_HISTORY)/.test(intent.type)
        && intent.type !== 'CHART_ACTIVE_STATE_UPDATED';
    const is_details_screen_intent = /^(CSV_|OPEN_CSV_EXPORT|CLOSE_CSV_EXPORT|HISTORY_|REFRESH_TRADE_HISTORY|BACK_TO_DASHBOARD)/.test(intent.type);

    if (is_main_screen_intent && route !== 'dashboard') {
        return false;
    }

    if (is_details_screen_intent && route !== 'trade_history') {
        return false;
    }

    return true;
}


/**
 * 클래스 이름: UiIntentRouter
 * 기능: 한 외부 intent를 현재 루트 snapshot에 맞는 내부 이벤트 묶음으로 변환한다.
 * 작성 날짜: 2026/09/16
 */
export class UiIntentRouter {
    readonly events: UiDomainEvent[] = [];

    /**
     * 함수 이름: UiIntentRouter.constructor()
     * 기능: 한 외부 입력을 변환할 기준 루트 snapshot을 보관한다.
     * 인자: snapshot -> 입력 수신 시점의 UI 루트 snapshot
     * 반환값: 생성된 UiIntentRouter 인스턴스
     * 작성 날짜: 2026/09/16
     */
    constructor(private readonly snapshot: UiApplicationSnapshot) {}

    /**
     * 함수 이름: read()
     * 기능: 루트 snapshot에서 지정한 기능의 읽기 전용 view를 추출한다.
     * 인자: key -> 기능 이름 또는 shell
     * 반환값: 해당 기능의 상태·context view
     * 작성 날짜: 2026/09/16
     */
    private read<K extends FeatureKey | 'shell'>(key: K) {
        return feature_view(this.snapshot, key);
    }

    /**
     * 함수 이름: send()
     * 기능: 기능 이름을 붙인 내부 이벤트를 현재 입력의 처리 목록에 추가한다.
     * 인자: feature -> 이벤트를 받을 기능, source -> 원래 이벤트와 입력 데이터
     * 반환값: 없음
     * 작성 날짜: 2026/09/16
     */
    private send(feature: FeatureKey | 'shell', source: {
        type: string;
        [key: string]: unknown;
    }): void {
        this.events.push({
            type: `${feature}.${source.type}`,
            source,
        });
    }

    /**
     * 함수 이름: dispatch()
     * 기능: 사용자 입력과 서버 통지를 기능별 내부 이벤트 목록으로 변환한다.
     * 인자: intent -> 변환할 외부 입력
     * 반환값: 화면·모달 정책에 따라 입력을 수락했으면 true
     * 작성 날짜: 2026/09/16
     */
    dispatch(intent: UiApplicationIntent): boolean {
        if (!is_intent_active(this.snapshot, intent)) {
            return false;
        }

        switch (intent.type) {
            case 'BACKEND_SNAPSHOT_SYNCHRONIZED':
                {
                    this.synchronize_server_owned_snapshot(intent.snapshot);
                    break;
                }
            case 'CONNECT_REQUESTED':
                this.send('connection', {
                    type: 'CONNECT_REQUESTED',
                });
                break;
            case 'RECONNECT_REQUESTED':
                this.send('connection', {
                    type: 'RECONNECT_REQUESTED',
                });
                break;
            case 'API_CONNECTED':
                this.send('connection', intent.sequence === undefined ? {
                    type: 'API_CONNECTED',
                } : {
                    type: 'API_CONNECTED',
                    sequence: intent.sequence,
                });
                break;
            case 'API_DISCONNECTED':
                this.send('connection', intent.reason === undefined ? {
                    type: 'API_DISCONNECTED',
                } : {
                    type: 'API_DISCONNECTED',
                    reason: intent.reason,
                });
                this.send('trading', {
                    type: 'API_DISCONNECTED',
                });
                break;
            case 'UI_CONNECTION_DISCONNECTED':
                // 화면 수신 복구는 authoritative 매매 lifecycle이나 실행 중 명령을 변경하지 않는다.
                this.send('connection', { type: 'API_DISCONNECTED', ...(intent.reason === undefined ? {} : { reason: intent.reason }) });
                break;
            case 'BACKEND_CONNECTION_STATUS':
                this.send('connection', intent);
                break;
            case 'RECONNECT_FAILED':
                this.send('connection', intent);
                break;
            case 'START_TRADING_CLICKED':
                {
                    const regime = this.read('regime').context.applied_regime;
                    const is_online = this.read('connection').matches('api_online');
                    const trading_context = this.read('trading').context;

                    // 정지 상태의 열린 Position은 복구 청산 전까지 새 전략 session 시작을 허용하지 않는다.
                    if (!trading_context.is_trading && trading_context.has_open_position) {
                        return false;
                    }

                    const unavailable_reason = resolve_trading_start_unavailable_reason(trading_context.logic_coverage, trading_context.command_enabled, regime);
                    const modal = regime === null ? 'select_regime_notice' : unavailable_reason !== null ? 'trading_unavailable_notice' : is_online ? 'start_confirmation' : 'api_connection_required';

                    if (!this.can_open_modal(modal)) {
                        return false;
                    }

                    this.send('trading', {
                        type: 'START_BUTTON_CLICKED',
                        regime,
                        is_online,
                    });
                    break;
                }
            case 'START_TRADING_CONFIRMED':
                this.send('trading', {
                    type: 'START_CONFIRMED',
                    is_online: this.read('connection').matches('api_online'),
                });
                break;
            case 'START_TRADING_CANCELED':
                this.send('trading', {
                    type: 'START_CANCELED',
                });
                break;
            case 'SELECT_REGIME_NOTICE_CONFIRMED':
                this.send('trading', {
                    type: 'SELECT_REGIME_NOTICE_CONFIRMED',
                });
                this.send('regime', {
                    type: 'HIGHLIGHT_REQUESTED',
                });
                break;
            case 'SELECT_REGIME_NOTICE_CLOSED':
                this.send('trading', {
                    type: 'SELECT_REGIME_NOTICE_CLOSED',
                });
                break;
            case 'API_CONNECTION_NOTICE_CONFIRMED':
                this.send('trading', {
                    type: 'API_CONNECTION_NOTICE_CONFIRMED',
                });
                break;
            case 'TRADING_UNAVAILABLE_NOTICE_CONFIRMED':
                this.send('trading', {
                    type: 'TRADING_UNAVAILABLE_NOTICE_CONFIRMED',
                });
                break;
            case 'STOP_TRADING_CLICKED':
                {
                    const modal = intent.has_open_position ? 'force_sell_stop_confirmation' : 'stop_confirmation';

                    if (!this.can_open_modal(modal)) {
                        return false;
                    }

                    this.send('trading', {
                        type: 'STOP_BUTTON_CLICKED',
                        has_open_position: intent.has_open_position,
                    });
                    break;
                }
            case 'STOP_TRADING_CONFIRMED':
                this.send('trading', {
                    type: 'STOP_CONFIRMED',
                });
                break;
            case 'STOP_TRADING_CANCELED':
                this.send('trading', {
                    type: 'STOP_CANCELED',
                });
                break;
            case 'FORCE_SELL_AND_STOP_CONFIRMED':
                this.send('trading', {
                    type: 'FORCE_SELL_AND_STOP_CONFIRMED',
                });
                break;
            case 'FORCE_SELL_AND_STOP_CANCELED':
                this.send('trading', {
                    type: 'FORCE_SELL_AND_STOP_CANCELED',
                });
                break;
            case 'RECOVERED_POSITION_LIQUIDATION_CONFIRMED':
                this.send('trading', {
                    type: 'RECOVERED_POSITION_LIQUIDATION_CONFIRMED',
                });
                break;
            case 'RECOVERED_POSITION_LIQUIDATION_CANCELED':
                this.send('trading', {
                    type: 'RECOVERED_POSITION_LIQUIDATION_CANCELED',
                });
                break;
            case 'BACKEND_TRADING_STARTED':
            case 'BACKEND_TRADING_STOPPED':
            case 'POSITION_UPDATED':
                this.send('trading', intent);
                break;
            case 'TRADING_SESSION_SYNCHRONIZED':
                {
                    const is_trading = intent.status !== 'not_started' && intent.status !== 'terminated';
                    const strategy_state_label = intent.strategy_state_label ?? intent.strategy_status;  // 이전 호출자는 실행 Case를 추측하지 않고 상태 문구를 쓴다.

                    // Case·단계·목록을 하나의 루트 이벤트 처리 안에서 함께 반영한다.
                    {
                        this.send('recent_orders', {
                            type: 'STRATEGY_INDICATORS_SYNCHRONIZED',
                            indicators: is_trading ? intent.strategy_indicators ?? null : null,
                        });

                        // 한 backend lifecycle event의 ratio, position과 state를 동일한 source 값으로 적용한다.
                        this.send('split_order', {
                            type: 'SPLIT_ORDER_SNAPSHOT_SYNCHRONIZED',
                            scale_in_percentage: intent.scale_in_percentage,
                            scale_out_percentage: intent.scale_out_percentage,
                        });
                        this.send('trading', {
                            type: 'TRADING_SNAPSHOT_SYNCHRONIZED',
                            selected_regime: this.read('regime').context.applied_regime,
                            logic_coverage: intent.logic_coverage,
                            command_enabled: intent.command_enabled,
                            risk_policy_availability: intent.risk_policy_availability,
                            configured_risk_policy_version: intent.configured_risk_policy_version,
                            max_order_notional: intent.max_order_notional,
                            max_position_notional: intent.max_position_notional,
                            max_daily_loss: intent.max_daily_loss,
                            daily_loss_scope: intent.daily_loss_scope,
                            manual_kill_behavior: intent.manual_kill_behavior,
                            session_risk_policy_version: intent.session_risk_policy_version,
                            risk_control_version: intent.risk_control_version,
                            manual_kill_active: intent.manual_kill_active,
                            manual_kill_cleanup_complete: intent.manual_kill_cleanup_complete,
                            manual_kill_activation_behavior: intent.manual_kill_activation_behavior,
                            manual_kill_activation_policy_version: intent.manual_kill_activation_policy_version,
                            last_risk_decision_allowed: intent.last_risk_decision_allowed,
                            last_risk_budget: intent.last_risk_budget,
                            risk_block_reason: intent.risk_block_reason,
                            process_ownership_ambiguous: intent.process_ownership_ambiguous,
                            is_trading,
                            has_open_position: intent.has_open_position,
                            lifecycle_status: intent.status,
                            position_average_entry_price: intent.position_average_entry_price ?? null,
                            residual_quantity: intent.residual_quantity ?? '0',
                            residual_cost_basis: intent.residual_cost_basis ?? '0',
                            balance_reconciliation: intent.balance_reconciliation ?? null,
                        });
                        // 종료 Region에도 같은 authoritative lifecycle과 Position 조합을 전달한다.
                        this.send('app_exit', {
                            type: 'TRADING_SESSION_UPDATED',
                            version: intent.version,
                            status: intent.status,
                            has_open_position: intent.has_open_position,
                        });
                        this.send('chart', {
                            type: 'TRADING_LOGIC_STATE_CHANGED',
                            state_label: strategy_state_label,
                        });

                        const current_strategy = this.read('account_summary').context.strategy;

                        this.send('account_summary', {
                            type: 'TRADING_STATUS_UPDATED',
                            strategy: {
                                ...current_strategy,
                                appliedState: strategy_state_label,
                                status: intent.strategy_status,
                                statusTone: intent.strategy_status_tone,
                            },
                        });
                    }
                    break;
                }
            case 'ACCOUNT_STRATEGY_UPDATED':
                // 계좌 전략을 직접 갱신하는 demo와 기존 intent 경로도 차트에 같은 상태를 전달한다.
                this.send('chart', {
                    type: 'TRADING_LOGIC_STATE_CHANGED',
                    state_label: intent.strategy.appliedState,
                });
                this.send('account_summary', {
                    type: 'TRADING_STATUS_UPDATED',
                    strategy: intent.strategy,
                });
                break;
            case 'ACCOUNT_ASSETS_UPDATED':
                this.send('account_summary', {
                    type: 'ASSET_SUMMARY_UPDATED',
                    asset: intent.asset,
                });
                break;
            case 'REGIME_TYPE_CLICKED':
                if (!this.can_open_modal('regime_change_confirmation')) {
                    return false;
                }
                this.send('regime', {
                    type: 'TYPE_CLICKED',
                    regime: intent.regime,
                });
                break;
            case 'REGIME_CHANGE_CONFIRMED':
                this.send('regime', {
                    type: 'CONFIRM_TYPE_CHANGE',
                });
                break;
            case 'REGIME_CHANGE_CANCELED':
                this.send('regime', {
                    type: 'CANCEL_TYPE_CHANGE',
                });
                break;
            case 'REGIME_RECOMMENDED':
                this.send('regime', {
                    type: 'TYPE_RECOMMENDED',
                    regime: intent.regime,
                });
                break;
            case 'REGIME_APPLIED':
                this.send('regime', {
                    type: 'REGIME_APPLIED',
                    regime: intent.regime,
                });
                break;
            case 'REGIME_SELECTION_SYNCHRONIZED':
                this.send('regime', {
                    type: 'REGIME_APPLIED',
                    regime: intent.selected,
                });
                break;
            case 'REGIME_INDICATORS_UPDATED':
                this.send('regime', {
                    type: 'REGIME_INDICATOR_UPDATED',
                    metrics: intent.metrics,
                });
                break;
            case 'REGIME_HIGHLIGHT_COMPLETED':
                this.send('regime', {
                    type: 'HIGHLIGHT_COMPLETED',
                });
                break;
            case 'CHART_INTERVAL_SELECTED':
                this.dispatch_chart_interval(intent.interval);
                break;
            case 'CHART_INDICATOR_SETTINGS_TOGGLED':
                this.send('chart', {
                    type: 'INDICATOR_SETTINGS_BUTTON_CLICKED',
                });
                break;
            case 'CHART_INDICATOR_SETTINGS_OUTSIDE_CLICKED':
                this.send('chart', {
                    type: 'INDICATOR_POPUP_OUTSIDE_CLICKED',
                });
                break;
            case 'CHART_INDICATOR_CHANGED':
                this.dispatch_chart_indicator(intent.indicator, intent.is_visible);
                break;
            case 'CHART_FULLSCREEN_CHANGED':
                this.send('chart', {
                    type: intent.is_fullscreen ? 'FULL_SIZE_SELECTED' : 'NORMAL_SIZE_SELECTED',
                });
                break;
            case 'CHART_DRAWING_TOOL_CLICKED':
                this.send('chart', {
                    type: 'DRAWING_TOOL_CLICKED',
                });
                break;
            case 'CHART_DRAWING_STARTED':
                this.send('chart', {
                    type: 'USER_START_DRAWING',
                });
                break;
            case 'CHART_DRAWING_FINISHED':
                this.send('chart', {
                    type: 'USER_FINISH_DRAWING',
                    drawing: intent.drawing,
                });
                break;
            case 'CHART_DRAWING_CANCELED':
                this.send('chart', {
                    type: 'DRAWING_CANCELED',
                });
                break;
            case 'CHART_ACTIVE_STATE_UPDATED':
                this.send('chart', {
                    type: 'TRADING_LOGIC_STATE_CHANGED',
                    state_label: intent.state_label,
                });
                // 차트 전용 legacy intent도 계좌의 수익률·작동 상태를 보존하며 전략 문구만 맞춘다.
                this.send('account_summary', {
                    type: 'TRADING_STATUS_UPDATED',
                    strategy: {
                        ...this.read('account_summary').context.strategy,
                        appliedState: intent.state_label,
                    },
                });
                break;
            case 'CHART_LINE_HOVER_ENTERED':
                this.send('chart', {
                    type: 'CURSOR_HOVER_ENTER',
                    line_id: intent.line_id,
                });
                break;
            case 'CHART_LINE_HOVER_EXITED':
                this.send('chart', {
                    type: 'CURSOR_HOVER_EXIT',
                });
                break;
            case 'CHART_LINE_CONTEXT_MENU_REQUESTED':
                this.send('chart', {
                    type: 'HIGHLIGHTED_LINE_RIGHT_CLICKED',
                    ...(intent.x === undefined ? {} : {
                        x: intent.x,
                    }),
                    ...(intent.y === undefined ? {} : {
                        y: intent.y,
                    }),
                });
                break;
            case 'CHART_LINE_DELETE_REQUESTED':
                this.send('chart', {
                    type: 'DELETE_LINE',
                });
                break;
            case 'CHART_LINE_CONTEXT_MENU_OUTSIDE_CLICKED':
                this.send('chart', {
                    type: 'CONTEXT_MENU_OUTSIDE_CLICKED',
                });
                break;
            case 'RECENT_ORDERS_TAB_SELECTED':
                this.send('recent_orders', {
                    type: 'TRADING_HISTORY_CLICKED',
                });
                break;
            case 'REALTIME_INDICATORS_TAB_SELECTED':
                this.send('recent_orders', {
                    type: 'REALTIME_INDICATOR_CLICKED',
                });
                break;
            case 'REALTIME_INDICATORS_UPDATED':
                this.send('recent_orders', {
                    type: 'REALTIME_INDICATOR_UPDATED',
                    indicators: intent.indicators,
                });
                break;
            case 'BUY_ORDER_EXECUTED':
            case 'SELL_ORDER_EXECUTED':
                {
                    this.send('recent_orders', intent);

                    if (this.read('shell').context.route === 'trade_history') {
                        // Active 상세 화면은 같은 현재 filter query를 다시 읽어 recent/table 일관성을 맞춘다.
                        this.send('trade_history', {
                            type: 'ORDER_EXECUTION_RECEIVED',
                        });
                    }

                    break;
                }
            case 'SCALE_IN_CHANGED':
                this.send('split_order', {
                    type: 'SCALE_IN_LEVEL_CHANGED',
                    percentage: intent.percentage,
                });
                break;
            case 'SCALE_OUT_CHANGED':
                this.send('split_order', {
                    type: 'SCALE_OUT_LEVEL_CHANGED',
                    percentage: intent.percentage,
                });
                break;
            case 'SHOW_TRADE_HISTORY':
                this.send('shell', {
                    type: 'SHOW_ALL_TRADING_DETAILS',
                });
                this.send('trade_history', {
                    type: 'ENTER_TRADE_HISTORY',
                });
                break;
            case 'BACK_TO_DASHBOARD':
                this.send('shell', {
                    type: 'BACK_TO_MAIN_SCREEN',
                });
                this.send('trade_history', {
                    type: 'LEAVE_TRADE_HISTORY',
                });
                break;
            case 'REFRESH_TRADE_HISTORY':
                this.send('trade_history', {
                    type: 'REFRESH_TRADE_HISTORY',
                });
                break;
            case 'HISTORY_PERIOD_SELECTED':
                this.dispatch_history_period(intent.period);
                break;
            case 'HISTORY_SIDE_SELECTED':
                this.dispatch_history_side(intent.side);
                break;
            case 'TRADE_HISTORY_PROFIT_RATE_UPDATED':
                this.advance_trade_history_summary_revision();
                this.send('trade_history_summary', {
                    type: 'PROFIT_RATE_UPDATED',
                    daily_return: intent.daily_return,
                });
                break;
            case 'TRADE_HISTORY_SELL_SUMMARY_UPDATED':
                this.advance_trade_history_summary_revision();
                this.send('trade_history_summary', {
                    type: 'SELL_ORDER_EXECUTED',
                    sell_performance: intent.sell_performance,
                    position: intent.position,
                });
                break;
            case 'TRADE_HISTORY_BUY_HOLDINGS_UPDATED':
                this.advance_trade_history_summary_revision();
                this.send('trade_history_summary', {
                    type: 'BUY_ORDER_EXECUTED',
                    position: intent.position,
                });
                break;
            case 'TRADE_HISTORY_HOLDINGS_UPDATED':
                this.advance_trade_history_summary_revision();
                this.send('trade_history_summary', {
                    type: 'HOLDINGS_SNAPSHOT_UPDATED',
                    position: intent.position,
                });
                break;
            case 'TRADE_HISTORY_DAILY_FEE_UPDATED':
                this.advance_trade_history_summary_revision();
                this.send('trade_history_summary', {
                    type: 'DAILY_TRADING_FEE_CHANGED',
                    fees: intent.fees,
                });
                break;
            case 'TRADE_HISTORY_PERFORMANCE_UPDATED':
                this.advance_trade_history_summary_revision();
                this.send('trade_history_summary', {
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
                this.send('csv_export', {
                    type: 'CSV_EXPORT_CLICKED',
                });
                break;
            case 'CLOSE_CSV_EXPORT':
                this.send('csv_export', {
                    type: 'CLOSE_CSV_EXPORT_POPUP',
                });
                break;
            case 'CSV_DIALOG_OUTSIDE_CLICKED':
                this.send('csv_export', {
                    type: 'CSV_DIALOG_OUTSIDE_CLICKED',
                });
                break;
            case 'CSV_DIRECTORY_SELECT_CLICKED':
                this.send('csv_export', {
                    type: 'SAVE_LOCATION_SELECT_CLICKED',
                });
                break;
            case 'CSV_PERIOD_SELECTED':
                this.dispatch_csv_period(intent.period);
                break;
            case 'CSV_START_CALENDAR_OPENED':
                this.send('csv_export', {
                    type: 'START_CSV_START_DATE_SELECTION',
                });
                break;
            case 'CSV_END_CALENDAR_OPENED':
                this.send('csv_export', {
                    type: 'START_CSV_FINISH_DATE_SELECTION',
                });
                break;
            case 'CSV_CALENDAR_OUTSIDE_CLICKED':
                {
                    const calendar_target = this.read('csv_export').context.calendar_target;

                    this.send('csv_export', {
                        type: calendar_target === 'start_date' ? 'START_DATE_CALENDAR_OUTSIDE_CLICKED' : 'FINISH_DATE_CALENDAR_OUTSIDE_CLICKED',
                    });
                    break;
                }
            case 'CSV_START_DATE_SELECTED':
                this.send('csv_export', {
                    type: 'START_DATE_SELECTED',
                    date: intent.date,
                });
                break;
            case 'CSV_END_DATE_SELECTED':
                this.send('csv_export', {
                    type: 'FINISH_DATE_SELECTED',
                    date: intent.date,
                });
                break;
            case 'CSV_FILE_NAME_EDIT_STARTED':
                this.send('csv_export', {
                    type: 'FILE_NAME_CLICKED',
                });
                break;
            case 'CSV_FILE_NAME_CHANGED':
                this.send('csv_export', {
                    type: 'FILE_NAME_CHANGED',
                    file_name: intent.file_name,
                });
                break;
            case 'CSV_FILE_NAME_COMMITTED':
                this.send('csv_export', {
                    type: 'FILE_NAME_INPUT_FOCUS_LOST',
                });
                break;
            case 'CSV_EXPORT_SUBMITTED':
                this.send('csv_export', {
                    type: 'EXPORT_CSV',
                });
                break;
            case 'CSV_EXPORT_ERROR_CONFIRMED':
                this.send('csv_export', {
                    type: 'CSV_EXPORT_ERROR_CONFIRMED',
                });
                break;
            case 'CSV_EXPORT_COMPLETE_CONFIRMED':
                this.send('csv_export', {
                    type: 'ACCEPT_CLOSE_ALL_POPUP',
                });
                break;
            case 'APP_EXIT_CLICKED':
                {
                    // 종료 시점의 lifecycle로 정상 stop과 recovered-position liquidation을 구분한다.
                    const trading_snapshot = this.read('trading').context;

                    this.send('app_exit', {
                        type: 'EXIT_CLICKED',
                        has_open_position: trading_snapshot.has_open_position,
                        is_trading: trading_snapshot.is_trading,
                    });
                    break;
                }
            case 'APP_EXIT_CONFIRMED':
                this.send('app_exit', {
                    type: 'EXIT_CONFIRMED',
                });
                break;
            case 'APP_EXIT_CANCELED':
                this.send('app_exit', {
                    type: 'EXIT_CANCELED',
                });
                break;
            case 'FORCE_SELL_EXIT_CONFIRMED':
                this.send('app_exit', {
                    type: 'FORCE_SELL_EXIT_CONFIRMED',
                });
                break;
            case 'FORCE_SELL_EXIT_CANCELED':
                this.send('app_exit', {
                    type: 'FORCE_SELL_EXIT_CANCELED',
                });
                break;
            case 'BACKEND_SIDECAR_EXITED_NORMALLY':
                this.send('app_exit', {
                    type: 'SIDECAR_EXITED',
                });
                break;
            case 'BACKEND_SIDECAR_EXITED_ABNORMALLY':
                this.send('app_exit', {
                    type: 'SIDECAR_EXITED_ABNORMALLY',
                });
                break;
        }

        return true;
    }

    /**
     * 함수 이름: advance_trade_history_summary_revision()
     * 기능: account/performance authoritative update가 이전 HTTP summary보다 최신임을 표시한다.
     * 인자: 없음
     * 반환값: 없음
     * 작성 날짜: 2026/08/23
     */
    private advance_trade_history_summary_revision(): void {
        this.events.push({
            type: 'summary_revision.advance',
        });
    }

    /**
     * 함수 이름: synchronize_server_owned_snapshot()
     * 기능: 한 backend snapshot을 루트의 내부 동기화 이벤트 묶음으로 변환한다.
     * 인자: synchronized_snapshot -> mapper가 검증하고 UI 표시 계약으로 변환한 전체 snapshot
     * 반환값: 없음
     * 작성 날짜: 2026/08/21
     */
    private synchronize_server_owned_snapshot(synchronized_snapshot: UiServerOwnedSnapshot): void {
        // 열린 사용자 확인 modal은 유지하되 그 안에서 표시할 authoritative context는 갱신한다.
        const active_modal = derive_active_modal(this.snapshot);
        const preserve_regime_interaction = active_modal === 'regime_change_confirmation';
        const preserve_trading_interaction = active_modal === 'select_regime_notice'
            || active_modal === 'api_connection_required'
            || active_modal === 'trading_unavailable_notice'
            || active_modal === 'start_confirmation'
            || active_modal === 'stop_confirmation'
            || active_modal === 'force_sell_stop_confirmation';

        {
            // Full resync summary를 진행 중 상세 HTTP 결과보다 먼저 authoritative revision으로 만든다.
            this.advance_trade_history_summary_revision();

            // UI-local route와 chart 설정은 건드리지 않고 server-owned context만 교체한다.
            this.send('account_summary', {
                type: 'ACCOUNT_SUMMARY_SYNCHRONIZED',
                strategy: synchronized_snapshot.account_strategy,
                asset: synchronized_snapshot.account_asset,
            });
            this.send('regime', {
                type: preserve_regime_interaction ? 'REGIME_SNAPSHOT_CONTEXT_SYNCHRONIZED' : 'REGIME_SNAPSHOT_SYNCHRONIZED',
                recommended_regime: synchronized_snapshot.recommended_regime,
                applied_regime: synchronized_snapshot.applied_regime,
                metrics: synchronized_snapshot.regime_metrics,
            });
            this.send('recent_orders', {
                type: 'RECENT_ORDERS_SNAPSHOT_SYNCHRONIZED',
                trades: synchronized_snapshot.recent_trades,
                indicators: [],  // REGIME의 4시간봉 값을 전략 지표에 복사하지 않는다.
            });
            this.send('recent_orders', {
                type: 'STRATEGY_INDICATORS_SYNCHRONIZED',
                indicators: synchronized_snapshot.strategy_indicators ?? null,
            });

            const history_is_active = this.read('shell').context.route === 'trade_history';

            // recent_trades는 현재 period/side filter 결과가 아니므로 table cache에 복사하지 않는다.
            this.send('trade_history', {
                type: history_is_active ? 'TRADE_HISTORY_RESYNCHRONIZED' : 'INVALIDATE_TRADE_HISTORY_CACHE',
                symbol: synchronized_snapshot.trading_symbol,
            });
            this.send('trade_history_summary', {
                type: 'TRADE_HISTORY_SUMMARY_SYNCHRONIZED',
                summary: synchronized_snapshot.trade_history_summary,
            });
            // Phase 7의 두 authoritative ratio를 같은 snapshot batch에서 함께 교체한다.
            this.send('split_order', {
                type: 'SPLIT_ORDER_SNAPSHOT_SYNCHRONIZED',
                scale_in_percentage: synchronized_snapshot.scale_in_percentage,
                scale_out_percentage: synchronized_snapshot.scale_out_percentage,
            });

            // Trading status와 position은 같은 backend snapshot에서 받은 authoritative 값으로 교체한다.
            this.send('trading', {
                type: preserve_trading_interaction ? 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED' : 'TRADING_SNAPSHOT_SYNCHRONIZED',
                selected_regime: synchronized_snapshot.applied_regime,
                logic_coverage: synchronized_snapshot.logic_coverage,
                command_enabled: synchronized_snapshot.command_enabled,
                risk_policy_availability: synchronized_snapshot.risk_policy_availability,
                configured_risk_policy_version: synchronized_snapshot.configured_risk_policy_version,
                max_order_notional: synchronized_snapshot.max_order_notional,
                max_position_notional: synchronized_snapshot.max_position_notional,
                max_daily_loss: synchronized_snapshot.max_daily_loss,
                daily_loss_scope: synchronized_snapshot.daily_loss_scope,
                manual_kill_behavior: synchronized_snapshot.manual_kill_behavior,
                session_risk_policy_version: synchronized_snapshot.session_risk_policy_version,
                risk_control_version: synchronized_snapshot.risk_control_version,
                manual_kill_active: synchronized_snapshot.manual_kill_active,
                manual_kill_cleanup_complete: synchronized_snapshot.manual_kill_cleanup_complete,
                manual_kill_activation_behavior: synchronized_snapshot.manual_kill_activation_behavior,
                manual_kill_activation_policy_version: synchronized_snapshot.manual_kill_activation_policy_version,
                last_risk_decision_allowed: synchronized_snapshot.last_risk_decision_allowed,
                last_risk_budget: synchronized_snapshot.last_risk_budget,
                risk_block_reason: synchronized_snapshot.risk_block_reason,
                process_ownership_ambiguous: synchronized_snapshot.process_ownership_ambiguous,
                is_trading: synchronized_snapshot.is_trading,
                has_open_position: synchronized_snapshot.has_open_position,
                lifecycle_status: synchronized_snapshot.trading_state_label,
                position_average_entry_price: synchronized_snapshot.position_average_entry_price ?? null,
                residual_quantity: synchronized_snapshot.residual_quantity ?? '0',
                residual_cost_basis: synchronized_snapshot.residual_cost_basis ?? '0',
                balance_reconciliation: synchronized_snapshot.balance_reconciliation ?? null,
            });
            // Event 유실 뒤 full resync도 app-exit의 동일 terminal·Position barrier를 열 수 있어야 한다.
            this.send('app_exit', {
                type: 'TRADING_SESSION_UPDATED',
                version: synchronized_snapshot.trading_version,
                status: synchronized_snapshot.trading_state_label,
                has_open_position: synchronized_snapshot.has_open_position,
            });
            this.send('chart', {
                type: 'TRADING_LOGIC_STATE_CHANGED',
                state_label: synchronized_snapshot.account_strategy.appliedState,  // 재연결도 동일한 전략을 복원한다.
            });
            this.send('connection', {
                type: 'API_CONNECTED',
                sequence: synchronized_snapshot.last_sequence,
            });
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
        const active_modal = derive_active_modal(this.snapshot);

        return active_modal === null || active_modal === modal;
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
            '1m': {
                type: '1_M_BUTTON_CLICKED',
            } as const,
            '30m': {
                type: '30_M_BUTTON_CLICKED',
            } as const,
            '4h': {
                type: '4_H_BUTTON_CLICKED',
            } as const,
            '1d': {
                type: '1_DAY_BUTTON_CLICKED',
            } as const,
        };

        this.send('chart', event_by_interval[interval]);
    }

    /**
     * 함수 이름: dispatch_chart_indicator()
     * 기능: 지표 이름과 ON/OFF 값을 차트 machine의 명세 event로 변환한다.
     * 인자: indicator -> 변경할 지표, is_visible -> 표시 여부
     * 반환값: 없음
     * 작성 날짜: 2026/08/12
     */
    private dispatch_chart_indicator(indicator: 'bollinger_bands' | 'ema9' | 'volume', is_visible: boolean): void {
        if (indicator === 'bollinger_bands') {
            this.send('chart', {
                type: is_visible ? 'BB_DISPLAY_ON_CLICKED' : 'BB_DISPLAY_OFF_CLICKED',
            });
        } else if (indicator === 'ema9') {
            this.send('chart', {
                type: is_visible ? 'EMA_DISPLAY_ON_CLICKED' : 'EMA_DISPLAY_OFF_CLICKED',
            });
        } else {
            this.send('chart', {
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
            today: {
                type: 'SELECT_DISPLAY_TODAY_HISTORY',
            } as const,
            last7days: {
                type: 'SELECT_DISPLAY_WEEKLY_HISTORY',
            } as const,
            last30days: {
                type: 'SELECT_DISPLAY_MONTHLY_HISTORY',
            } as const,
            all: {
                type: 'SELECT_DISPLAY_ALL_HISTORY',
            } as const,
        };

        this.send('trade_history', event_by_period[period]);
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
            all: {
                type: 'ALL_TRADE_HISTORY_SELECTED',
            } as const,
            buy: {
                type: 'BUY_TRADE_HISTORY_SELECTED',
            } as const,
            sell: {
                type: 'SELL_TRADE_HISTORY_SELECTED',
            } as const,
        };

        this.send('trade_history', event_by_side[side]);
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
            today: {
                type: 'SELECT_CSV_TODAY_HISTORY',
            } as const,
            last7days: {
                type: 'SELECT_CSV_WEEKLY_HISTORY',
            } as const,
            last30days: {
                type: 'SELECT_CSV_MONTHLY_HISTORY',
            } as const,
            custom: {
                type: 'SELECT_CSV_DATE',
            } as const,
        };

        this.send('csv_export', event_by_period[period]);
    }
}
