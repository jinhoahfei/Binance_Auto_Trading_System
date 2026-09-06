import { spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

/**
 * 함수 이름: get_tauri_arguments()
 * 기능: CLI의 마지막 config merge가 Windows platform override를 되돌리지 않게 선택한다.
 * 인자: command -> dev 또는 build, platform -> Node OS 이름
 * 반환값: 고정 native CLI argument 배열
 * 작성 날짜: 2026/09/06
 */
export function get_tauri_arguments(command, platform) {
    if (!['dev', 'build'].includes(command) || !['darwin', 'win32'].includes(platform)) {
        throw new Error('Supported desktop command and platform required.');
    }

    // Tauri는 기본·OS 설정 다음 --config를 병합하므로 Windows에서는 OS 설정을 마지막에 유지한다.
    const configuration_name = platform === 'win32' ? 'tauri.windows.conf.json' : 'tauri.conf.json';
    return [command, '--config', `apps/desktop/src-tauri/${configuration_name}`];
}

/**
 * 함수 이름: main()
 * 기능: platform 설정을 선택해 설치된 Tauri CLI를 shell 없이 동일 Node에서 실행한다.
 * 인자: 없음
 * 반환값: CLI의 process exit code
 * 작성 날짜: 2026/09/06
 */
function main() {
    // .cmd/.sh shim과 PATH상의 전역 CLI 대신 lockfile에 설치된 entrypoint를 직접 호출한다.
    const require = createRequire(import.meta.url);
    const tauri_cli_path = require.resolve('@tauri-apps/cli/tauri.js');
    const arguments_list = get_tauri_arguments(process.argv[2], process.platform);
    const result = spawnSync(process.execPath, [tauri_cli_path, ...arguments_list, ...process.argv.slice(3)], {
        cwd: fileURLToPath(new URL('../', import.meta.url)),
        stdio: 'inherit',
    });
    if (result.error) throw new Error('Tauri CLI could not start.');
    process.exitCode = result.status ?? 1;  // Native failure를 launcher 성공으로 바꾸지 않는다.
}

// Test는 순수 argument 계약만 import하며 실제 native app은 명령 실행에서만 시작한다.
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
    try {
        main();
    } catch {
        console.error('desktop: Supported macOS or Windows development environment is required.');
        process.exitCode = 1;
    }
}
