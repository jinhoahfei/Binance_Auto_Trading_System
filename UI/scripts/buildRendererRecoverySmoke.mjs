// 운영 UI 자산과 별도로 실제 bootstrap·차트의 주문 없는 WebView2 검증 자산만 만든다.
import { build } from 'vite';
import react from '@vitejs/plugin-react';
import { writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';


/**
 * 함수 이름: read_choice()
 * 기능: 명시한 검증 build 옵션을 허용 목록으로 제한한다.
 * 인자: name -> CLI 옵션 이름, choices -> 허용 값, fallback -> 기본 값
 * 반환값: 검증된 옵션 문자열
 * 작성 날짜: 2026/10/04
 */
function read_choice(name, choices, fallback) {
    const index = process.argv.indexOf(name);
    const value = index < 0 ? fallback : process.argv[index + 1];
    if (!choices.includes(value)) throw new Error(`Invalid ${name} option`);

    return value;
}

const react_mode = read_choice('--react-mode', ['development', 'production'], 'production');
const cleanup_enabled = read_choice('--performance-cleanup', ['enabled', 'disabled'], 'enabled') === 'enabled';
const root = fileURLToPath(new URL('./renderer-recovery-smoke', import.meta.url));
const output = fileURLToPath(new URL('../apps/desktop/renderer-recovery-dist', import.meta.url));
await build({
    configFile: false, root, base: './', mode: react_mode, plugins: [react()], logLevel: 'warn',
    define: {
        'process.env.NODE_ENV': JSON.stringify(react_mode),
        __SOAK_DEVELOPMENT_BUILD__: JSON.stringify(react_mode === 'development'),
        __SOAK_CLEANUP_ENABLED__: JSON.stringify(cleanup_enabled),
    },
    build: {
        outDir: output, emptyOutDir: true, minify: false, reportCompressedSize: false,
        rollupOptions: {
            input: {
                recovery: fileURLToPath(new URL('./renderer-recovery-smoke/index.html', import.meta.url)),
                chart: fileURLToPath(new URL('./renderer-recovery-smoke/renderer-soak.html', import.meta.url)),
            },
        },
    },
});
await writeFile(new URL('../apps/desktop/renderer-recovery-dist/build-profile.json', import.meta.url),
    JSON.stringify({ react_mode, cleanup_enabled, candle_count: 1000, tick_interval_ms: 250 }, null, 2) + '\n');
console.log(JSON.stringify({ status: 'built', react_mode, cleanup_enabled, output }));
