import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

/**
 * 함수 이름: defineConfig()
 * 기능: React 기반 데스크톱 렌더러의 Vite 개발 및 빌드 설정을 정의한다.
 * 인자: 없음
 * 반환값: Vite 설정 객체
 * 작성 날짜: 2026/08/12
 */
export default defineConfig({
  plugins: [react()],
  server: {
    strictPort: true,
  },
});
