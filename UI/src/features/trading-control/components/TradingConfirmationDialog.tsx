import { Button, ModalSurface, StatusIndicatorIcon } from '../../../shared/ui';

import styles from './TradingConfirmationDialog.module.css';

export type TradingDialogKind =
  | 'start'
  | 'starting'
  | 'stop'
  | 'forceStop'
  | 'regimeRequired'
  | 'connectionRequired';

export interface TradingConfirmationDialogProps {
  open: boolean;
  kind: TradingDialogKind;
  regimeLabel?: string;
  pending?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}

interface DialogCopy {
  title: string;
  description: string;
  label: string;
  detail: string;
  cancelLabel: string;
  confirmLabel: string;
  tone: 'positive' | 'negative' | 'info';
}

/**
 * 함수 이름: get_dialog_copy()
 * 기능: 자동매매 상태별 Figma 문구와 버튼 역할을 반환한다.
 * 인자: kind -> 표시할 자동매매 모달 종류, regimeLabel -> 선택된 REGIME 문구
 * 반환값: 모달에 표시할 문구 및 색상 정보
 * 작성 날짜: 2026/08/12
 */
function get_dialog_copy(kind: TradingDialogKind, regimeLabel: string): DialogCopy {
  const copies: Record<TradingDialogKind, DialogCopy> = {
    start: {
      title: '자동매매를 시작할까요?',
      description: '확인을 누르면 선택한 타입 기준으로 실시간 주문 감시가 시작됩니다.',
      label: '거래 타입',
      detail: `${regimeLabel}으로 거래를 시작하시겠습니까?`,
      cancelLabel: '취소',
      confirmLabel: '거래 시작',
      tone: 'positive',
    },
    starting: {
      title: '자동매매를 시작하고 있습니다…',
      description: '실시간 주문 감시를 연결하는 중입니다. 잠시만 기다려 주세요.',
      label: '거래 타입',
      detail: `${regimeLabel}으로 거래를 시작하시겠습니까?`,
      cancelLabel: '취소',
      confirmLabel: '연결 중…',
      tone: 'positive',
    },
    stop: {
      title: '매매를 중지할까요?',
      description: '현재 보유 중인 포지션이 없습니다.',
      label: '포지션 상태',
      detail: '자동매매 감시와 신규 주문을 중지하시겠습니까?',
      cancelLabel: '취소',
      confirmLabel: '매매 중지',
      tone: 'negative',
    },
    forceStop: {
      title: '보유 포지션이 있습니다',
      description: '매매를 중지하면 현재 보유 포지션이 강제 매도됩니다.',
      label: '중지 방식',
      detail: '그래도 매매를 중지하시겠습니까?',
      cancelLabel: '취소',
      confirmLabel: '강제 매도 후 중지',
      tone: 'negative',
    },
    regimeRequired: {
      title: 'REGIME type을 먼저 선택해주세요',
      description: 'REGIME type이 선택되지 않아 자동매매 실행을 시작할 수 없습니다.',
      label: '허용되지 않은 동작',
      detail: '자동매매 실행 전 REGIME type을 우선 선택해야 합니다.',
      cancelLabel: '닫기',
      confirmLabel: 'REGIME 선택',
      tone: 'info',
    },
    connectionRequired: {
      title: 'API 연결이 필요합니다',
      description: 'Binance API 연결이 끊겨 자동매매를 시작할 수 없습니다.',
      label: '연결 상태',
      detail: '연결이 복구된 뒤 다시 시도해주세요.',
      cancelLabel: '닫기',
      confirmLabel: '확인',
      tone: 'info',
    },
  };

  return copies[kind];
}

/**
 * 함수 이름: TradingConfirmationDialog()
 * 기능: 시작, 중지, REGIME 및 연결 guard 결과를 공통 확인 모달로 표시한다.
 * 인자: props -> 모달 종류, REGIME 문구와 확인·취소 처리 함수
 * 반환값: 자동매매 확인 Dialog 요소
 * 작성 날짜: 2026/08/12
 */
export function TradingConfirmationDialog({
  open,
  kind,
  regimeLabel = 'Type 0(횡보)',
  pending = false,
  onCancel,
  onConfirm,
}: TradingConfirmationDialogProps) {
  const copy = get_dialog_copy(kind, regimeLabel);
  const is_pending = pending || kind === 'starting';
  const icon_tone = copy.tone === 'positive' ? 'positive' : 'negative';

  return (
    <ModalSurface
      description={copy.description}
      fixedHeight
      leadingVisual={<StatusIndicatorIcon tone={icon_tone} />}
      open={open}
      title={copy.title}
    >
      <div className={`${styles.notice} ${styles[copy.tone]}`}>
        <span>{copy.label}</span>
        <strong>
          <i aria-hidden="true" />
          {copy.detail}
        </strong>
        {is_pending ? <span className={styles.progress} aria-label="연결 진행 중" /> : null}
      </div>
      <div className={styles.actions}>
        <Button disabled={is_pending} onClick={onCancel}>
          {copy.cancelLabel}
        </Button>
        <Button disabled={is_pending} onClick={onConfirm} tone={copy.tone}>
          {is_pending ? '처리 중…' : copy.confirmLabel}
        </Button>
      </div>
    </ModalSurface>
  );
}
