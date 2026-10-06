import { execFileSync } from 'node:child_process';
import { writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import base_config from '../../UI/vitest.config';

const repository_root = fileURLToPath(new URL('../../', import.meta.url));
const ui_root = path.join(repository_root, 'UI');
const changed_paths = new Set(execFileSync('git', ['diff', '--name-only', '--', 'UI/src'],
    { cwd: repository_root, encoding: 'utf8' }).trim().split(/\r?\n/u));
const loaded_baseline_files = new Set<string>();

export default {
    ...base_config,
    root: ui_root,
    plugins: [...base_config.plugins, {
        name: 'renderer-parity-baseline',
        enforce: 'pre',
        /**
         * 함수 이름: load()
         * 기능: baseline 시험에서만 변경된 추적 소스를 수정 전 Git 내용으로 읽는다.
         * 인자: id -> Vite가 요청한 모듈 경로
         * 반환값: 기준 소스 또는 기본 loader를 사용하면 null
         * 작성 날짜: 2026/10/04
         */
        load(id) {
            if (process.env.RENDERER_PARITY_BASELINE !== '1') return null;
            const source_path = id.split('?')[0];
            const relative_path = path.relative(repository_root, source_path).replaceAll('\\', '/');
            if (!changed_paths.has(relative_path)) return null;
            loaded_baseline_files.add(relative_path);
            writeFileSync(path.join(repository_root,
                'artifacts/renderer-oom-implementation-20261004/renderer-parity-baseline-sources.json'),
            JSON.stringify([...loaded_baseline_files].sort(), null, 4));

            return execFileSync('git', ['show', `HEAD:${relative_path}`],
                { cwd: repository_root, encoding: 'utf8' });
        },
    }],
    test: {
        ...base_config.test,
        include: ['../artifacts/renderer-oom-implementation-20261004/renderer-parity.test.tsx'],
        maxWorkers: 1,
    },
};
