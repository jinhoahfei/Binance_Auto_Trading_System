import { fileURLToPath } from 'node:url';
import type { Plugin } from 'vite';

const stages = new Set([
    'dashboard-ready',
    'market-parity',
    'tooltip-status',
    'tooltip-hidden',
    'tooltip-reopened',
    'recovery-visible',
    'recovery-retry-passed',
    'renderer-reload-passed',
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
export function desktop_smoke_plugin(mode: 'connections' | 'recovery-shutdown' | 'recovery-reload'): Plugin {
    // 검증 시나리오는 명시한 개발 실행에서만 고정된 entry로 연결한다.
    const entry_points = {
        connections: '/src/test/desktopSmoke.ts',
        'recovery-shutdown': '/src/test/desktopRecoveryShutdownSmoke.ts',
        'recovery-reload': '/src/test/desktopRecoveryReloadSmoke.ts',
    } as const;

    return {
        name: 'desktop-smoke',
        apply: 'serve',
        enforce: 'pre',

        /**
         * 함수 이름: resolveId()
         * 기능: 복구 재진입 검증 모드의 main 모듈에만 Tauri fault seam을 연결한다.
         * 인자: source -> import 경로, importer -> 호출 모듈 경로
         * 반환값: 대체 모듈 경로 또는 기본 해석용 null
         * 작성 날짜: 2026/09/17
         */
        resolveId(source, importer) {
            // 실제 Tauri API는 유지하고 명시한 검증 실행의 main import 하나에만 fault seam을 연결한다.
            if (mode === 'recovery-reload' && source === '@tauri-apps/api/core'
                && importer?.split('?')[0]?.endsWith('/src/main.tsx')) {
                return fileURLToPath(new URL('../src/test/recoverySmokeInvoke.ts', import.meta.url));
            }

            return null;
        },

        /**
         * 함수 이름: transformIndexHtml()
         * 기능: 선택한 desktop smoke 모드의 진입 script를 HTML에 삽입한다.
         * 인자: 없음
         * 반환값: head 앞에 삽입할 module script 정의
         * 작성 날짜: 2026/09/17
         */
        transformIndexHtml() {
            return [{
                tag: 'script',
                attrs: {
                    type: 'module',
                    src: entry_points[mode],
                },
                injectTo: 'head-prepend',
            }];
        },

        /**
         * 함수 이름: configureServer()
         * 기능: 고정 stage와 연결 enum만 기록하는 로컬 smoke 결과 endpoint를 등록한다.
         * 인자: server -> Vite 개발 서버
         * 반환값: 없음
         * 작성 날짜: 2026/09/17
         */
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

                // 실제 시세 대조 결과는 공개 스윙 분류만 남기며 원본 snapshot을 기록하지 않는다.
                for (const key of ['swing_low', 'swing_high']) {
                    const value = url.searchParams.get(key);
                    if (value !== null && ['HL', 'LL', 'HH', 'LH', '-'].includes(value)) {
                        result[key] = value;
                    }
                }
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
