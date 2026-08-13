import * as Dialog from '@radix-ui/react-dialog';
import type { ReactNode } from 'react';

import { use_dialog_focus_return } from '../../hooks';

import styles from './ModalSurface.module.css';

export interface ModalSurfaceProps {
  open: boolean;
  title: string;
  description?: string;
  children: ReactNode;
  leadingVisual?: ReactNode;
  onOpenChange?: (is_open: boolean) => void;
  closeOnOutside?: boolean;
  fixedHeight?: boolean;
  size?: 'compact' | 'wide';
}

/**
 * 함수 이름: ModalSurface()
 * 기능: 포커스 잠금과 배경 비활성화를 제공하는 공통 모달 표면을 표시한다.
 * 인자: props -> 열림 상태, 제목, 설명, 내용과 닫힘 정책
 * 반환값: 접근 가능한 Radix Dialog 요소
 * 작성 날짜: 2026/08/12
 */
export function ModalSurface({
  open,
  title,
  description,
  children,
  leadingVisual,
  onOpenChange,
  closeOnOutside = false,
  fixedHeight = false,
  size = 'compact',
}: ModalSurfaceProps) {
  use_dialog_focus_return();

  const root_props = onOpenChange ? { onOpenChange } : {};

  return (
    <Dialog.Root open={open} {...root_props}>
      <Dialog.Portal>
        <Dialog.Overlay className={styles.overlay} />
        <Dialog.Content
          className={`${styles.content} ${styles[size]} ${fixedHeight ? styles.fixedHeight : ''}`}
          onEscapeKeyDown={(event) => {
            if (!closeOnOutside) {
              event.preventDefault();
            }
          }}
          onInteractOutside={(event) => {
            if (!closeOnOutside) {
              event.preventDefault();
            }
          }}
        >
          <div className={leadingVisual ? styles.headingWithVisual : undefined}>
            {leadingVisual ? <div className={styles.leadingVisual}>{leadingVisual}</div> : null}
            <div>
              <Dialog.Title className={styles.title}>{title}</Dialog.Title>
              {description ? <Dialog.Description className={styles.description}>{description}</Dialog.Description> : null}
            </div>
          </div>
          {children}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
