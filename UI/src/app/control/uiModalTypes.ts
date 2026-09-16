/**
 * 파일 역할: UI에서 사용하는 화면 경로와 모달 종류의 공통 이름을 정의한다.
 * 입력 router·모달 정책·화면 모델이 같은 이름으로 화면과 팝업을 구분하도록 한다.
 * 어떤 모달을 표시할지는 uiModalPolicy.ts에서 현재 루트 상태를 읽어 결정한다.
 */

/** 메인 화면과 거래 상세 화면을 구분하는 화면 모델의 경로 값이다. */
export type UiRoute = 'dashboard' | 'trade_history';

/** 확인·진행·완료·실패·복구 팝업의 종류이며, 모달이 없는 경우는 호출 측에서 null로 표현한다. */
export type UiModalKind =
    | 'start_confirmation'
    | 'select_regime_notice'
    | 'api_connection_required'
    | 'trading_unavailable_notice'
    | 'stop_confirmation'
    | 'force_sell_stop_confirmation'
    | 'regime_change_confirmation'
    | 'csv_export'
    | 'csv_export_progress'
    | 'csv_export_complete'
    | 'csv_export_error'
    | 'exit_confirmation'
    | 'force_sell_exit_confirmation'
    | 'shutdown_exit_recovery'
    | 'shutdown_outcome_recovery'
    | 'sidecar_exit_failure'
    | 'exit_processing';
