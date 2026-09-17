// 대시보드와 거래 내역에서 공통으로 사용하는 패널 표면을 표시한다.

import type { HTMLAttributes, ReactNode } from 'react';

import styles from './Surface.module.css';

export interface SurfaceProps extends HTMLAttributes<HTMLElement> {
  children: ReactNode;
  as?: 'section' | 'article' | 'div';
}


/**
 * 함수 이름: Surface()
 * 기능: 대시보드와 거래 내역에서 공통으로 사용하는 패널 표면을 표시한다.
 * 인자: props -> 패널 요소 종류, 내용과 표준 HTML 속성
 * 반환값: 공통 표면 스타일이 적용된 요소
 * 작성 날짜: 2026/08/12
 */
export function Surface({ as: Component = 'section', children, className = '', ...surface_props }: SurfaceProps) {
  return (
    <Component className={`${styles.surface} ${className}`} {...surface_props}>
      {children}
    </Component>
  );
}
