// 동일 fixture의 수정 전·후 App DOM을 비교하며 canvas pixel 검증과 구분한다.
import { readFile, writeFile } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
const artifact_root = new URL('./', import.meta.url);
const baseline = JSON.parse(await readFile(new URL('renderer-parity-baseline.json', artifact_root), 'utf8'));
const modified = JSON.parse(await readFile(new URL('renderer-parity-modified.json', artifact_root), 'utf8'));
const results = baseline.captures.map((capture, index) => ({ state: capture.state,
    equal: capture.html === modified.captures[index].html, baseline_sha256: capture.sha256,
    modified_sha256: modified.captures[index].sha256 }));
const changed_style_assets = execFileSync('git', ['diff', '--name-only', '--', 'UI/src'],
    { encoding: 'utf8' }).trim().split(/\r?\n/u).filter((file_path) => /\.(css|svg|png)$/u.test(file_path));
const result = { all_equal: results.every((entry) => entry.equal),
    scope: 'identical fixture/date App DOM with baseline HEAD module source versus working tree; not canvas pixel comparison',
    scenario_count: results.length, results, changed_style_assets };
await writeFile(new URL('renderer-parity-comparison.json', artifact_root), JSON.stringify(result, null, 4));
console.log(JSON.stringify(result, null, 2));
