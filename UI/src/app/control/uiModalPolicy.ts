import type { UiApplicationSnapshot } from '../machines/uiApplicationTypes';
import type { UiModalKind } from './uiModalTypes';
import { feature_view } from '../machines/uiApplicationViews';


/**
 * 함수 이름: derive_active_modal()
 * 기능: 루트의 종료·매매·REGIME·CSV 상태에서 우선순위가 가장 높은 모달을 선택한다.
 * 인자: snapshot -> 현재 UI 루트 snapshot
 * 반환값: 표시할 모달 종류 또는 null
 * 작성 날짜: 2026/09/16
 */
export function derive_active_modal(snapshot: UiApplicationSnapshot): UiModalKind | null {
    const exit_snapshot = feature_view(snapshot, 'app_exit');
    const trading_snapshot = feature_view(snapshot, 'trading');
    const regime_snapshot = feature_view(snapshot, 'regime');
    const csv_snapshot = feature_view(snapshot, 'csv_export');

    if (exit_snapshot.matches('force_sell_exit_confirmation')) {
        return 'force_sell_exit_confirmation';
    }

    if (exit_snapshot.matches('exit_confirmation')) {
        return 'exit_confirmation';
    }

    if (exit_snapshot.matches('shutdown_exit_recovery')) {
        return 'shutdown_exit_recovery';
    }

    if (exit_snapshot.matches('shutdown_outcome_recovery')) {
        return 'shutdown_outcome_recovery';
    }

    if (exit_snapshot.matches('sidecar_exit_failure')) {
        return 'sidecar_exit_failure';
    }

    if (exit_snapshot.matches('shutting_down')) {
        return 'exit_processing';
    }

    if (trading_snapshot.matches('select_regime_notice')) {
        return 'select_regime_notice';
    }

    if (trading_snapshot.matches('api_connection_required')
        || trading_snapshot.matches('disconnect_stopping')) {
        return 'api_connection_required';
    }

    if (trading_snapshot.matches('trading_unavailable_notice')) {
        return 'trading_unavailable_notice';
    }

    if (trading_snapshot.matches('start_confirmation') || trading_snapshot.matches('starting')) {
        return 'start_confirmation';
    }

    if (trading_snapshot.matches('stop_confirmation') || trading_snapshot.matches('stopping')) {
        return 'stop_confirmation';
    }

    if (trading_snapshot.matches('force_sell_confirmation') || trading_snapshot.matches('force_selling')) {
        return 'force_sell_stop_confirmation';
    }

    if (trading_snapshot.matches('recovered_position_liquidation_confirmation')
        || trading_snapshot.matches('liquidating_recovered_position')) {
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
