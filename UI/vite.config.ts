import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';
import { desktop_smoke_plugin } from './scripts/desktopSmokePlugin.ts';

const smoke_mode = process.env.BINANCE_DESKTOP_SMOKE;

/**
 * 함수 이름: defineConfig()
 * 기능: React 기반 데스크톱 렌더러의 Vite 개발 및 빌드 설정을 정의한다.
 * 인자: 없음
 * 반환값: Vite 설정 객체
 * 작성 날짜: 2026/08/12
 */
export default defineConfig({
  plugins: [react(), ...(smoke_mode === '1' || smoke_mode === 'recovery-shutdown'
    ? [desktop_smoke_plugin(smoke_mode === '1' ? 'connections' : 'recovery-shutdown')]
    : [])],
  server: {
    strictPort: true,
  },
});
