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
  plugins: [react(), ...(smoke_mode === '1' || smoke_mode === 'recovery-shutdown' || smoke_mode === 'recovery-reload'
    ? [desktop_smoke_plugin(smoke_mode === '1' ? 'connections' : smoke_mode)]
    : [])],
  server: {
    strictPort: true,
    // Tauri가 관리하는 Rust source/build 경로는 Vite에서 감시하지 않는다.
    // Windows linker가 점유한 target의 exe를 watch하면 EBUSY로 개발 서버가 종료된다.
    watch: { ignored: ['**/apps/desktop/src-tauri/**'] },
  },
});
