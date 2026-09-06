import { Button, ModalSurface, StatusIndicatorIcon } from '../../../shared/ui';

import styles from './RegimeChangeDialog.module.css';

export interface RegimeChangeDialogProps {
  open: boolean;
  regimeKey: string;
  regimeLabel: string;
  pending?: boolean;
  error?: string | null | undefined;
  onCancel: () => void;
  onConfirm: () => void;
}

/**
 * 함수 이름: RegimeChangeDialog()
 * 기능: REGIME 후보를 실제 투자 기준으로 적용하기 전에 확인을 요청한다.
 * 인자: props -> 후보 REGIME, 열림 상태와 확인·취소 처리 함수
 * 반환값: REGIME 적용 확인 Dialog 요소
 * 작성 날짜: 2026/08/12
 */
export function RegimeChangeDialog({
  open,
  regimeKey,
  regimeLabel,
  pending = false,
  error,
  onCancel,
  onConfirm,
}: RegimeChangeDialogProps) {
  // 제목, 안내와 확인 버튼 문구를 REGIME 선택을 확정하는 의미로 일치시킨다.
  const confirm_label = pending ? '적용 중…' : '확인';  // 처리 중에는 기존 진행 상태 문구를 표시한다.

  return (
    <ModalSurface
      open={open}
      fixedHeight={!error}
      title="REGIME type을 선택할까요?"
      description="확인을 누르면 선택한 REGIME type이 선택됩니다."
      leadingVisual={<StatusIndicatorIcon tone="positive" />}
    >
      <div className={styles.notice}>
        <span>선택 REGIME type</span>
        <strong>
          <i aria-hidden="true" />
          {regimeKey} · {regimeLabel}으로 선택하시겠습니까?
        </strong>
      </div>
      {error ? <p className={styles.error} role="alert">{error}</p> : null}
      <div className={styles.actions}>
        <Button disabled={pending} onClick={onCancel}>취소</Button>
        <Button disabled={pending} onClick={onConfirm} tone="positive">
          {confirm_label}
        </Button>
      </div>
    </ModalSurface>
  );
}
