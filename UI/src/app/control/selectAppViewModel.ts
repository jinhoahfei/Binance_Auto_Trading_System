import type { AppViewModel } from './uiApplicationContracts';
import type { UiApplicationSnapshot } from '../machines/uiApplicationTypes';
import { select_feature_views } from '../machines/uiApplicationViews';
import { derive_active_modal } from './uiModalPolicy';


/**
 * 함수 이름: select_app_view_model()
 * 기능: 루트 상태와 기능별 데이터를 화면 표시 계약으로 변환한다.
 * 인자: root_snapshot -> 현재 UI 루트 snapshot
 * 반환값: 화면·모달·pending·오류 정보를 포함한 AppViewModel
 * 작성 날짜: 2026/09/16
 */
export function select_app_view_model(root_snapshot: UiApplicationSnapshot): AppViewModel {
    // 병렬 Region의 상태를 기능별 표시 상태로 읽는다.
    const snapshot = select_feature_views(root_snapshot);
    const history_status = snapshot.trade_history.value as AppViewModel['trade_history']['status'];
    const csv_status = snapshot.csv_export.matches({
        editing: {
            file_browser: 'opened',
        },
    }) ? 'picking_directory' : typeof snapshot.csv_export.value === 'string' ? snapshot.csv_export.value : 'editing';
    const exit_status = snapshot.app_exit.value as AppViewModel['app_exit']['status'];

    // 동일한 최종 snapshot에서 모든 표시값을 만들어 화면 간 상태가 섞이지 않게 한다.
    return {
        route: snapshot.shell.context.route,
        active_modal: derive_active_modal(root_snapshot),

        // 연결 상태와 복구 진행을 상단 표시 계약으로 변환한다.
        connection: {
            status: snapshot.connection.context.status,
            is_online: snapshot.connection.matches('api_online'),
            is_pending: snapshot.connection.matches('connecting') || snapshot.connection.matches('reconnecting'),
            reconnect_attempt: snapshot.connection.context.reconnect_attempt,
            error: snapshot.connection.context.last_error,
            recovery: snapshot.connection.context.recovery,
        },

        // 서버 매매 상태·위험 제한과 명령 진행 여부를 함께 표시한다.
        trading: {
            command_enabled: snapshot.trading.context.command_enabled,
            risk_policy_availability: snapshot.trading.context.risk_policy_availability,
            configured_risk_policy_version: snapshot.trading.context.configured_risk_policy_version,
            max_order_notional: snapshot.trading.context.max_order_notional,
            max_position_notional: snapshot.trading.context.max_position_notional,
            max_daily_loss: snapshot.trading.context.max_daily_loss,
            daily_loss_scope: snapshot.trading.context.daily_loss_scope,
            manual_kill_behavior: snapshot.trading.context.manual_kill_behavior,
            session_risk_policy_version: snapshot.trading.context.session_risk_policy_version,
            risk_control_version: snapshot.trading.context.risk_control_version,
            manual_kill_active: snapshot.trading.context.manual_kill_active,
            manual_kill_cleanup_complete: snapshot.trading.context.manual_kill_cleanup_complete,
            manual_kill_activation_behavior: snapshot.trading.context.manual_kill_activation_behavior,
            manual_kill_activation_policy_version: snapshot.trading.context.manual_kill_activation_policy_version,
            last_risk_decision_allowed: snapshot.trading.context.last_risk_decision_allowed,
            last_risk_budget: snapshot.trading.context.last_risk_budget,
            risk_block_reason: snapshot.trading.context.risk_block_reason,
            process_ownership_ambiguous: snapshot.trading.context.process_ownership_ambiguous,
            lifecycle_status: snapshot.trading.context.lifecycle_status,
            is_trading: snapshot.trading.context.is_trading,
            is_pending: snapshot.trading.matches('starting')
                || snapshot.trading.matches('stopping')
                || snapshot.trading.matches('force_selling')
                || snapshot.trading.matches('liquidating_recovered_position')
                || snapshot.trading.matches('awaiting_recovered_position_liquidation_completion')
                || snapshot.trading.matches('disconnect_stopping')
                || snapshot.trading.matches('awaiting_stop_completion'),
            is_recovery_liquidation: snapshot.trading.context.is_recovery_liquidation,
            has_open_position: snapshot.trading.context.has_open_position,
            position_average_entry_price: snapshot.trading.context.position_average_entry_price,
            residual_quantity: snapshot.trading.context.residual_quantity ?? '0',
            residual_cost_basis: snapshot.trading.context.residual_cost_basis ?? '0',
            balance_reconciliation: snapshot.trading.context.balance_reconciliation ?? null,
            unavailable_reason: snapshot.trading.context.unavailable_reason,
            error: snapshot.trading.context.error,
        },

        // 추천·적용·후보 값과 선택 진행 상태를 분리해 표시한다.
        regime: {
            recommended: snapshot.regime.context.recommended_regime,
            applied: snapshot.regime.context.applied_regime,
            candidate: snapshot.regime.context.candidate_regime,
            is_highlighted: snapshot.regime.context.is_highlighted,
            is_pending: snapshot.regime.matches('applying'),
            metrics: snapshot.regime.context.metrics,
            logic_coverage: snapshot.trading.context.logic_coverage,
            error: snapshot.regime.context.error,
        },

        // 현재 주기와 표시 설정·드로잉 선택 상태를 같은 차트 모델로 묶는다.
        chart: {
            interval: snapshot.chart.context.interval,
            indicators: snapshot.chart.context.indicators,
            is_indicator_settings_open: snapshot.chart.matches({
                indicator_settings: 'opened',
            }),
            is_fullscreen: snapshot.chart.context.is_fullscreen,
            drawing_mode: snapshot.chart.context.drawing_mode,
            active_trading_logic_state: snapshot.chart.context.active_trading_logic_state,
            selected_line_id: snapshot.chart.context.selected_line_id,
            context_menu_position: snapshot.chart.context.context_menu_position,
            line_selection_state: snapshot.chart.matches({
                line_selection: 'context_menu',
            }) ? 'context_menu' : snapshot.chart.matches({
                line_selection: 'highlighted',
            }) ? 'highlighted' : 'awaiting_selection',
            drawings: snapshot.chart.context.drawings[snapshot.chart.context.interval],
        },

        // 선택한 탭에 필요한 최근 주문과 실시간 지표를 제공한다.
        trader_panel: {
            active_tab: snapshot.recent_orders.matches('trade_history_displayed') ? 'recent_orders' : 'realtime_indicators',
            trades: snapshot.recent_orders.context.trades,
            realtime_indicators: snapshot.recent_orders.context.realtime_indicators,
            strategy_indicators: snapshot.recent_orders.context.strategy_indicators,
            strategy_indicators_received_at: snapshot.recent_orders.context.strategy_indicators_received_at,
        },

        // 분할 주문 비율과 저장 상태를 계좌 영역에 전달한다.
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

        // 현재 필터의 상세 행과 독립적인 계좌 요약을 함께 제공한다.
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

        // 편집값·검증 오류·완료 경로를 CSV 창의 표시 계약으로 옮긴다.
        csv_export: {
            status: csv_status as AppViewModel['csv_export']['status'],
            is_open: !snapshot.csv_export.matches('closed'),
            is_pending: snapshot.csv_export.matches({
                editing: {
                    file_browser: 'opened',
                },
            }) || snapshot.csv_export.matches('exporting'),
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

        // 정상 종료의 완료 상태와 진행·오류 상태를 구분한다.
        app_exit: {
            status: exit_status,
            is_pending: snapshot.app_exit.matches('shutting_down'),
            is_final: snapshot.app_exit.matches('ui_final_state'),
            error: snapshot.app_exit.context.error,
        },
    };
}
