import { CSVExportDialog } from '../../features/csv-export';
import { RegimeChangeDialog } from '../../features/regime-selection';
import { TradingConfirmationDialog } from '../../features/trading-control';
import type { AppViewModel } from '../control';
import { use_csv_calendar_navigation } from '../hooks';
import { present_csv_export_dialog_props } from '../presenters';
import type { UiApplicationController } from '../runtime';
import { ExitConfirmationDialog } from './modals/ExitConfirmationDialog';
import { OperationStatusDialog } from './modals/OperationStatusDialog';

const REGIME_LABELS: Readonly<Record<NonNullable<AppViewModel['regime']['candidate']>, string>> = {
    type0: '횡보',
    type1: '약상승',
    type2: '강상승',
    type3: '약하락',
    type4: '강하락',
};

export interface AppModalHostProps {
    readonly controller: UiApplicationController;
    readonly viewModel: AppViewModel;
}

/**
 * 함수 이름: AppModalHost()
 * 기능: shell actor의 단일 modal slot을 구체적인 거래·REGIME·CSV·종료 Dialog Boundary로 연결한다.
 * 인자: props -> 최신 AppViewModel과 typed intent controller
 * 반환값: 현재 전역 modal 하나 또는 null
 * 작성 날짜: 2026/08/12
 */
export function AppModalHost({ controller, viewModel }: AppModalHostProps) {
    const calendar_navigation = use_csv_calendar_navigation();
    const active_modal = viewModel.active_modal;

    // 시작 확인 문구는 적용 REGIME이 없을 때 type0을 만들어내지 않고 미선택 의미를 보존한다.
    const applied_regime = viewModel.regime.applied;
    const applied_regime_label = applied_regime === null
        ? '미선택 REGIME'
        : `${applied_regime} · ${REGIME_LABELS[applied_regime]}`;

    switch (active_modal) {
        case 'start_confirmation':
            return (
                <TradingConfirmationDialog
                    error={viewModel.trading.error?.message}
                    kind={viewModel.trading.is_pending ? 'starting' : 'start'}
                    onCancel={() => controller.dispatch({ type: 'START_TRADING_CANCELED' })}
                    onConfirm={() => controller.dispatch({ type: 'START_TRADING_CONFIRMED' })}
                    open
                    pending={viewModel.trading.is_pending}
                    regimeLabel={applied_regime_label}
                />
            );
        case 'select_regime_notice':
            return (
                <TradingConfirmationDialog
                    error={viewModel.trading.error?.message}
                    kind="regimeRequired"
                    onCancel={() => controller.dispatch({ type: 'SELECT_REGIME_NOTICE_CLOSED' })}
                    onConfirm={() => controller.dispatch({ type: 'SELECT_REGIME_NOTICE_CONFIRMED' })}
                    open
                />
            );
        case 'api_connection_required':
            return (
                <TradingConfirmationDialog
                    error={viewModel.trading.error?.message}
                    kind="connectionRequired"
                    onCancel={() => controller.dispatch({ type: 'API_CONNECTION_NOTICE_CONFIRMED' })}
                    onConfirm={() => controller.dispatch({ type: 'API_CONNECTION_NOTICE_CONFIRMED' })}
                    open
                    pending={viewModel.trading.is_pending}
                />
            );
        case 'trading_unavailable_notice':
            return (
                <TradingConfirmationDialog
                    kind="tradingUnavailable"
                    onCancel={() => controller.dispatch({
                        type: 'TRADING_UNAVAILABLE_NOTICE_CONFIRMED',
                    })}
                    onConfirm={() => controller.dispatch({
                        type: 'TRADING_UNAVAILABLE_NOTICE_CONFIRMED',
                    })}
                    open
                    unavailableReason={viewModel.trading.unavailable_reason}
                />
            );
        case 'stop_confirmation':
            return (
                <TradingConfirmationDialog
                    error={viewModel.trading.error?.message}
                    kind="stop"
                    onCancel={() => controller.dispatch({ type: 'STOP_TRADING_CANCELED' })}
                    onConfirm={() => controller.dispatch({ type: 'STOP_TRADING_CONFIRMED' })}
                    open
                    pending={viewModel.trading.is_pending}
                />
            );
        case 'force_sell_stop_confirmation':
            return (
                <TradingConfirmationDialog
                    error={viewModel.trading.error?.message}
                    kind="forceStop"
                    onCancel={() => controller.dispatch({ type: 'FORCE_SELL_AND_STOP_CANCELED' })}
                    onConfirm={() => controller.dispatch({ type: 'FORCE_SELL_AND_STOP_CONFIRMED' })}
                    open
                    pending={viewModel.trading.is_pending}
                />
            );
        case 'regime_change_confirmation': {
            const candidate_regime = viewModel.regime.candidate;

            if (candidate_regime === null) {
                return null;  // 후보가 없는 잘못된 확인 상태에서는 임의 REGIME을 표시하지 않는다.
            }

            return (
                <RegimeChangeDialog
                    error={viewModel.regime.error?.message}
                    onCancel={() => controller.dispatch({ type: 'REGIME_CHANGE_CANCELED' })}
                    onConfirm={() => controller.dispatch({ type: 'REGIME_CHANGE_CONFIRMED' })}
                    open
                    pending={viewModel.regime.is_pending}
                    regimeKey={candidate_regime}
                    regimeLabel={REGIME_LABELS[candidate_regime]}
                />
            );
        }
        case 'csv_export':
            return (
                <CSVExportDialog
                    {...present_csv_export_dialog_props(
                        viewModel,
                        controller,
                        calendar_navigation,
                    )}
                />
            );
        case 'csv_export_progress':
            return (
                <OperationStatusDialog
                    description="선택한 기간의 체결 내역을 CSV 파일로 생성하고 있습니다."
                    detail={viewModel.csv_export.file_name}
                    open
                    status="progress"
                    title="CSV 내보내기"
                />
            );
        case 'csv_export_complete':
            return (
                <OperationStatusDialog
                    actionLabel="확인"
                    actionTone="positive"
                    description="거래 내역 CSV 파일을 저장했습니다."
                    detail={viewModel.csv_export.receipt_path}
                    onConfirm={() => controller.dispatch({ type: 'CSV_EXPORT_COMPLETE_CONFIRMED' })}
                    open
                    status="success"
                    title="CSV 내보내기 완료"
                />
            );
        case 'csv_export_error':
            return (
                <OperationStatusDialog
                    actionLabel="설정으로 돌아가기"
                    actionTone="negative"
                    description="CSV 파일을 생성하지 못했습니다. 설정을 확인한 뒤 다시 시도해 주세요."
                    detail={viewModel.csv_export.command_error?.message}
                    onConfirm={() => controller.dispatch({ type: 'CSV_EXPORT_ERROR_CONFIRMED' })}
                    open
                    status="error"
                    title="CSV 내보내기 실패"
                />
            );
        case 'exit_confirmation':
            return (
                <ExitConfirmationDialog
                    error={viewModel.app_exit.error?.message}
                    forceSell={false}
                    onCancel={() => controller.dispatch({ type: 'APP_EXIT_CANCELED' })}
                    onConfirm={() => controller.dispatch({ type: 'APP_EXIT_CONFIRMED' })}
                    open
                />
            );
        case 'force_sell_exit_confirmation':
            return (
                <ExitConfirmationDialog
                    error={viewModel.app_exit.error?.message}
                    forceSell
                    onCancel={() => controller.dispatch({ type: 'FORCE_SELL_EXIT_CANCELED' })}
                    onConfirm={() => controller.dispatch({ type: 'FORCE_SELL_EXIT_CONFIRMED' })}
                    open
                />
            );
        case 'exit_processing':
            return (
                <OperationStatusDialog
                    description="주문과 연결을 안전하게 정리하고 저장 내용을 반영하고 있습니다."
                    detail={viewModel.app_exit.error?.message}
                    open
                    status="progress"
                    title="프로그램 종료 준비"
                />
            );
        case null:
            return null;
    }
}
