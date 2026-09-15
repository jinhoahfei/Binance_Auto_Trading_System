export const shutdown_step_message = (step?: string): string => ({
    workers: '진행 중인 작업을 정리하고 있습니다.',
    account: '잔고와 거래 기록을 확인하고 있습니다.',
    orders: '미체결 주문을 확인하고 정리하고 있습니다.',
    liquidation: '포지션 청산 결과를 확인하고 있습니다.',
    history: '거래 기록을 저장하고 있습니다.',
    complete: '백엔드 프로세스 종료를 확인하고 있습니다.',
}[step ?? ''] ?? '안전 종료를 준비하고 있습니다.');

export const shutdown_failure_message = (code: string, step?: string): string => ({
    SHUTDOWN_LIQUIDATION_CONFIRMATION_REQUIRED: '열린 포지션이 확인됐습니다. 청산 후 종료할지 확인해 주세요.',
    SHUTDOWN_ACCOUNT_UNREACHABLE: '거래소에서 잔고와 주문 상태를 확인하지 못했습니다. 연결 복구 후 다시 확인해 주세요.',
    SHUTDOWN_ACCOUNT_RECONCILIATION_FAILED: '거래소의 잔고·체결과 저장 기록을 대조하지 못했습니다. 앱을 유지하고 있습니다.',
    SHUTDOWN_BALANCE_MISMATCH: '거래소 잔고와 앱의 포지션·잔여 기록이 일치하지 않아 종료를 보류했습니다.',
    SHUTDOWN_UNEXPLAINED_ORDER: '앱의 기록으로 설명되지 않는 미체결 주문이 있어 종료를 보류했습니다.',
    SHUTDOWN_UNEXPLAINED_EXECUTION: '외부 체결 기록을 확인해야 하므로 종료를 보류했습니다.',
    SHUTDOWN_ORDER_UNRESOLVED: '주문 전송 또는 체결 결과가 아직 확정되지 않았습니다. 같은 주문을 다시 확인해 주세요.',
    SHUTDOWN_OWNERSHIP_UNVERIFIED: '실행 중인 백엔드의 소유권을 확인하지 못했습니다.',
    SHUTDOWN_POSITION_RECOVERY_REQUIRED: '저장된 포지션의 복구·청산 확인이 필요합니다.',
    SHUTDOWN_LIQUIDATION_PREPARATION_FAILED: '청산 주문을 전송하기 전 가격·수량 확인에 실패했습니다. 새 주문은 전송하지 않았으며 종료를 보류했습니다.',
    SHUTDOWN_HISTORY_SAVE_FAILED: '거래 기록 저장에 실패해 종료를 보류했습니다. 저장 공간과 파일 접근 상태를 확인해 주세요.',
    SHUTDOWN_HISTORY_MISMATCH: '저장된 거래 기록과 현재 기록이 일치하지 않아 종료를 보류했습니다.',
    SHUTDOWN_RESOURCE_CLEANUP_FAILED: '연결 정리를 완료하지 못했습니다. 종료 상태를 다시 확인해 주세요.',
    SHUTDOWN_PREPARATION_INTERNAL_ERROR: '종료 준비 중 내부 오류가 발생했습니다. 오류 기록을 남기고 앱을 유지했습니다.',
    SHUTDOWN_PREPARATION_TIMEOUT: `${shutdown_step_message(step)} 이 단계의 확인 시간이 초과되어 종료를 보류했습니다.`,
    STALE_CONTEXT_VERSION: '거래 상태가 변경되었습니다. 최신 상태로 종료를 다시 확인해 주세요.',
}[code] ?? `종료 준비를 완료하지 못했습니다. ${shutdown_step_message(step)}`);
