// UI 단위·컴포넌트 테스트의 DOM 환경과 공통 준비 파일을 설정한다.

import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

/**
 * 함수 이름: defineConfig()
 * 기능: UI 상태 머신과 React 컴포넌트 테스트 환경을 정의한다.
 * 인자: 없음
 * 반환값: Vitest 설정 객체
 * 작성 날짜: 2026/08/12
 */
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: true,
  },
});
