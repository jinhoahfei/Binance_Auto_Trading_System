import { Button, ModalSurface } from '../../../shared/ui';

import styles from './ExitConfirmationDialog.module.css';

export interface ExitConfirmationDialogProps {
    readonly open: boolean;
    readonly forceSell: boolean;
    readonly error?: string | null | undefined;
    readonly onCancel: () => void;
    readonly onConfirm: () => void;
}


/**
 * 함수 이름: ExitConfirmationDialog()
 * 기능: OS 종료 요청을 포지션 유무에 따른 일반 종료 또는 강제 매도 후 종료 확인으로 표시한다.
 * 인자: props -> 포지션 variant, 오류 문구와 확인·취소 action
 * 반환값: 애플리케이션 종료 확인 Dialog 요소
 * 작성 날짜: 2026/08/12
 */
export function ExitConfirmationDialog({
    open,
    forceSell,
    error,
    onCancel,
    onConfirm,
}: ExitConfirmationDialogProps) {
    // 포지션 상태에 맞는 종료 안내와 확인·취소 조작을 공통 모달에 배치한다.
    return (
        <ModalSurface
            open={open}
            title={forceSell ? '포지션 정리 후 종료할까요?' : '프로그램을 종료할까요?'}
            description={forceSell
                ? '현재 보유 포지션을 강제 매도한 뒤 연결과 저장 작업을 종료합니다.'
                : '연결을 닫고 저장 중인 설정을 반영한 뒤 프로그램을 종료합니다.'}
        >
            <div className={`${styles.notice} ${forceSell ? styles.warning : ''}`}>
                <span>{forceSell ? '포지션 보유 중' : '종료 준비'}</span>
                <strong>{forceSell ? '강제 매도 주문이 먼저 실행됩니다.' : '진행 중인 신규 주문 감시가 중단됩니다.'}</strong>
                {error ? <p role="alert">{error}</p> : null}
            </div>
            <div className={styles.actions}>
                <Button onClick={onCancel}>취소</Button>
                <Button onClick={onConfirm} tone="negative">
                    {forceSell ? '강제 매도 후 종료' : '종료'}
                </Button>
            </div>
        </ModalSurface>
    );
}
