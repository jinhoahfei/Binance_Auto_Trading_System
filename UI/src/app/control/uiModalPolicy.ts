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

    // 종료 확인과 종료 복구 안내를 다른 기능의 모달보다 먼저 선택한다.
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

    // 매매 진입 조건과 명령 확인·진행 상태를 그 다음 우선순위로 선택한다.
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

    // 매매·종료 모달이 없을 때 REGIME 확인을 표시한다.
    if (regime_snapshot.matches('type_change_confirmation') || regime_snapshot.matches('applying')) {
        return 'regime_change_confirmation';
    }

    // 상위 모달이 없을 때 CSV의 진행·결과·편집 상태를 표시한다.
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

    // 표시할 모달 상태가 없으면 화면을 가리지 않는다.
    return null;
}
