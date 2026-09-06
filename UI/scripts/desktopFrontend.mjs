import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

/**
 * 함수 이름: main()
 * 기능: Windows 개발 모드의 Python 사전 검사 뒤 Vite를 동일 Node runtime에서 실행한다.
 * 인자: 없음
 * 반환값: 종료 시 process exit code
 * 작성 날짜: 2026/09/06
 */
async function main() {
    // Release origin은 Windows native 검증 전이므로 package 명령을 개발 실행으로 우회하지 않는다.
    if (process.argv[2] !== 'dev' || process.platform !== 'win32' || process.arch !== 'x64') {
        throw new Error('Windows 11 x64 source development is required.');
    }

    // Shell quoting과 PATH의 다른 Python을 피하고 repository의 설치된 venv만 검사한다.
    const ui_directory = fileURLToPath(new URL('../', import.meta.url));
    const python_path = path.resolve(ui_directory, '../backend/.venv/Scripts/python.exe');
    const environment = Object.fromEntries(Object.entries(process.env).filter(([name]) =>
        /^(SystemRoot|WINDIR|TEMP|TMP|LOCALAPPDATA|APPDATA|USERPROFILE|PATH)$/i.test(name),
    ));
    const result = spawnSync(python_path, ['-I', '-c',
        'import struct, sys, websocket; from zoneinfo import ZoneInfo; import binance_auto_trader.sidecar; assert sys.platform == "win32" and struct.calcsize("P") == 8; ZoneInfo("Asia/Seoul")',
    ], { env: environment, stdio: 'ignore', timeout: 15_000 });
    if (result.error || result.status !== 0) {
        throw new Error('Prepare backend/.venv with x64 Python and uv sync --locked first.');
    }

    // Vite API를 사용해 PowerShell/cmd와 .bin shell shim 차이를 제거한다.
    process.chdir(ui_directory);
    const { createServer } = await import('vite');
    const server = await createServer({ server: { host: '127.0.0.1', port: 5173, strictPort: true } });
    await server.listen();  // Native exact Origin 계약의 고정 port에서만 개발 서버를 연다.
    server.printUrls();
}

main().catch((error) => {
    // 사전 검사의 child stderr와 환경 값은 출력하지 않고 고정된 운영 안내만 남긴다.
    console.error(`desktop:dev: ${error.message}`);
    process.exitCode = 1;
});
