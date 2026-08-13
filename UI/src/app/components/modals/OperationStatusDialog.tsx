import { Button, ModalSurface, type ButtonTone } from '../../../shared/ui';

import styles from './OperationStatusDialog.module.css';

export interface OperationStatusDialogProps {
    readonly open: boolean;
    readonly title: string;
    readonly description: string;
    readonly detail?: string | null | undefined;
    readonly status: 'progress' | 'success' | 'error';
    readonly actionLabel?: string;
    readonly actionTone?: ButtonTone;
    readonly onConfirm?: () => void;
}

/**
 * 함수 이름: OperationStatusDialog()
 * 기능: CSV와 앱 종료의 처리 중·완료·실패 결과를 공통 modal 표면에 표시한다.
 * 인자: props -> 상태 문구, 상세 결과와 선택적 확인 action
 * 반환값: 비동기 작업 상태 Dialog 요소
 * 작성 날짜: 2026/08/12
 */
export function OperationStatusDialog({
    open,
    title,
    description,
    detail,
    status,
    actionLabel = '확인',
    actionTone = 'info',
    onConfirm,
}: OperationStatusDialogProps) {
    return (
        <ModalSurface open={open} title={title} description={description}>
            <div aria-live="polite" className={`${styles.status} ${styles[status]}`}>
                <span aria-hidden="true" className={styles.statusIcon}>
                    {status === 'progress' ? '…' : status === 'success' ? '✓' : '!'}
                </span>
                <div>
                    <strong>{status === 'progress' ? '처리 중' : status === 'success' ? '완료' : '오류'}</strong>
                    {detail ? <p>{detail}</p> : null}
                </div>
                {status === 'progress' ? <span aria-hidden="true" className={styles.progressBar} /> : null}
            </div>
            {onConfirm ? (
                <div className={styles.actions}>
                    <Button onClick={onConfirm} tone={actionTone}>{actionLabel}</Button>
                </div>
            ) : null}
        </ModalSurface>
    );
}
