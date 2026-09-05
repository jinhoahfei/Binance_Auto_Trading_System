import type { Plugin } from 'vite';

const stages = new Set([
    'dashboard-ready',
    'tooltip-status',
    'tooltip-hidden',
    'tooltip-reopened',
    'recovery-visible',
    'recovery-shutdown-state',
    'recovery-shutdown-accepted',
    'passed',
    'failed',
]);

/**
 * 함수 이름: desktop_smoke_plugin()
 * 기능: 명시한 개발 실행에서만 실제 Tauri 화면 검사와 비밀 없는 결과 수집을 설치한다.
 * 인자: mode -> 연결 팝업 또는 malformed 화면 안전 종료 검증
 * 반환값: Vite serve 전용 검증 plugin
 * 작성 날짜: 2026/09/05
 */
export function desktop_smoke_plugin(mode: 'connections' | 'recovery-shutdown'): Plugin {
    return {
        name: 'desktop-smoke',
        apply: 'serve',
        transformIndexHtml() {
            return [{
                tag: 'script',
                attrs: {
                    type: 'module',
                    src: mode === 'connections'
                        ? '/src/test/desktopSmoke.ts'
                        : '/src/test/desktopRecoveryShutdownSmoke.ts',
                },
                injectTo: 'head-prepend',
            }];
        },
        configureServer(server) {
            server.middlewares.use('/__desktop_smoke', (request, response) => {
                const url = new URL(request.url ?? '/', 'http://127.0.0.1');
                const stage = url.searchParams.get('stage');
                if (request.method !== 'POST' || stage === null || !stages.has(stage)) {
                    response.statusCode = 400;
                    response.end();
                    return;
                }

                // Credential, 계좌 금액, 원본 오류 대신 고정 stage와 연결 enum만 기록한다.
                const result: Record<string, string> = { stage };
                for (const key of ['api', 'market_stream', 'account_stream']) {
                    const value = url.searchParams.get(key);
                    if (value === 'online' || value === 'offline') {
                        result[key] = value;
                    }
                }
                const code = url.searchParams.get('code');
                if (code !== null && /^[A-Z][A-Z0-9_]{1,63}$/u.test(code)) {
                    result.code = code;
                }
                server.config.logger.info(`desktop-smoke: ${JSON.stringify(result)}`);
                response.statusCode = 204;
                response.end();
            });
        },
    };
}
