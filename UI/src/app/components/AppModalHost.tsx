import { balance_reconciliation_text } from '../../shared/api/balanceReconciliation';
import { CSVExportDialog } from '../../features/csv-export';
import { RegimeChangeDialog } from '../../features/regime-selection';
import { TradingConfirmationDialog } from '../../features/trading-control';
import type { AppViewModel } from '../control';
import { use_csv_calendar_navigation } from '../hooks';
import { present_csv_export_dialog_props } from '../presenters';
import type { UiApplicationController } from '../runtime';
import { ExitConfirmationDialog } from './modals/ExitConfirmationDialog';
import { OperationStatusDialog } from './modals/OperationStatusDialog';
import { shutdown_step_message } from '../../shared/api/shutdownMessages';

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
        case 'force_sell_stop_confirmation': {
            const is_recovery_liquidation = viewModel.trading.is_recovery_liquidation;

            return (
                <TradingConfirmationDialog
                    error={viewModel.trading.error?.message}
                    kind={is_recovery_liquidation ? 'recoveryLiquidation' : 'forceStop'}
                    onCancel={() => controller.dispatch(is_recovery_liquidation
                        ? { type: 'RECOVERED_POSITION_LIQUIDATION_CANCELED' }
                        : { type: 'FORCE_SELL_AND_STOP_CANCELED' })}
                    onConfirm={() => controller.dispatch(is_recovery_liquidation
                        ? { type: 'RECOVERED_POSITION_LIQUIDATION_CONFIRMED' }
                        : { type: 'FORCE_SELL_AND_STOP_CONFIRMED' })}
                    open
                    pending={viewModel.trading.is_pending}
                />
            );
        }
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
        case 'shutdown_exit_recovery':
            return (
                <OperationStatusDialog
                    actionLabel="종료 상태 다시 확인"
                    actionTone="negative"
                    description="백엔드가 종료 요청을 수락했지만 프로세스 종료 확인이 지연되고 있습니다. 강제 종료하지 않고 운영자가 다시 확인할 때까지 창을 유지합니다."
                    detail={viewModel.app_exit.error?.message}
                    onConfirm={() => controller.dispatch({ type: 'APP_EXIT_CONFIRMED' })}
                    open
                    status="error"
                    title="백엔드 종료 확인 필요"
                />
            );
        case 'shutdown_outcome_recovery':
            return (
                <OperationStatusDialog
                    actionLabel="동일 종료 요청 다시 확인"
                    actionTone="negative"
                    description="백엔드의 종료 응답을 받지 못했습니다. 종료 요청을 다시 확인하면 같은 작업의 결과를 확인합니다."
                    detail={viewModel.app_exit.error?.message}
                    onConfirm={() => controller.dispatch({ type: 'APP_EXIT_CONFIRMED' })}
                    open
                    status="error"
                    title="종료 결과 확인 필요"
                />
            );
        case 'sidecar_exit_failure':
            return (
                <OperationStatusDialog
                    actionLabel="창 닫기"
                    actionTone="negative"
                    description="백엔드 프로세스가 중단되어 새 주문을 차단했습니다. 현재 창에서는 거래를 계속할 수 없습니다."
                    detail={viewModel.app_exit.error?.message}
                    onConfirm={() => controller.dispatch({ type: 'APP_EXIT_CONFIRMED' })}
                    open
                    status="error"
                    title="백엔드 복구 필요"
                />
            );
        case 'exit_processing':
            return (
                <OperationStatusDialog
                    description={shutdown_step_message(viewModel.connection.recovery?.shutdown_step)}
                    detail={viewModel.connection.recovery?.balance_reconciliation
                        ? balance_reconciliation_text(viewModel.connection.recovery.balance_reconciliation)
                        : viewModel.app_exit.error?.message}
                    open
                    status="progress"
                    title="프로그램 종료 준비"
                />
            );
        case null:
            return null;
    }
}
