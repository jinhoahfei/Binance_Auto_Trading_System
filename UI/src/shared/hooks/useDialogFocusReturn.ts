import { useEffect, useRef } from 'react';

/**
 * 함수 이름: use_dialog_focus_return()
 * 기능: 조건부로 마운트된 모달이 닫힐 때 모달을 열었던 요소로 키보드 초점을 복원한다.
 * 인자: 없음
 * 반환값: 없음
 * 작성 날짜: 2026/08/12
 */
export function use_dialog_focus_return() {
  const return_focus_element_ref = useRef<HTMLElement | null>(
    typeof document !== 'undefined' && document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null,
  );

  useEffect(() => () => {
    const return_focus_element = return_focus_element_ref.current;

    queueMicrotask(() => {
      const has_replacement_dialog = document.querySelector('[role="dialog"]') !== null;

      if (!has_replacement_dialog && return_focus_element?.isConnected) {
        return_focus_element.focus();
      }
    });
  }, []);
}
