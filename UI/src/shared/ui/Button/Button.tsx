import type { ButtonHTMLAttributes, ReactNode } from 'react';

import styles from './Button.module.css';

export type ButtonTone = 'neutral' | 'positive' | 'negative' | 'info';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  children: ReactNode;
  tone?: ButtonTone;
  compact?: boolean;
}


/**
 * 함수 이름: Button()
 * 기능: Figma 디자인 토큰을 공유하는 범용 버튼을 표시한다.
 * 인자: props -> 버튼의 내용, 색상 역할, 크기와 표준 HTML 속성
 * 반환값: 스타일이 적용된 버튼 요소
 * 작성 날짜: 2026/08/12
 */
export function Button({
  children,
  className = '',
  tone = 'neutral',
  compact = false,
  type = 'button',
  ...button_props
}: ButtonProps) {
  // 크기·상태색·추가 class를 합쳐 공통 버튼의 표시와 전달받은 입력 속성을 연결한다.
  const class_names = [styles.button, styles[tone], compact ? styles.compact : '', className]
    .filter(Boolean)
    .join(' ');

  return (
    <button className={class_names} type={type} {...button_props}>
      {children}
    </button>
  );
}
