// 실제 Python HTTP/WebSocket ↔ production UI adapter의 주문 없는 시간 기반 통합 검증.
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { createInterface } from 'node:readline';
import { mkdir, open, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { createServer } from 'vite';

const root = fileURLToPath(new URL('../../', import.meta.url));
const args = process.argv.slice(2);


/**
 * 함수 이름: option()
 * 기능: 명령줄 옵션의 다음 값을 읽고 없으면 기본값을 사용한다.
 * 인자: key -> 옵션 이름, fallback -> 기본값
 * 반환값: 지정된 옵션값 또는 기본값
 * 작성 날짜: 2026/09/17
 */
const option = (key, fallback) => args.includes(key) ? args[args.indexOf(key) + 1] : fallback;
const hours = Number(option('--hours', '24'));
const faultSeconds = Number(option('--fault-seconds', '900'));
if (!(hours > 0 && hours <= 168 && faultSeconds >= 10) || !args.includes('--output')) throw new Error('Invalid soak arguments');
const output = path.resolve(root, option('--output'));
await mkdir(output, { recursive: false });  // 기존 구동 기록을 재사용하거나 덮어쓰지 않는다.
const require = createRequire(import.meta.url);
const { WebSocket } = require('ws');  // lockfile에 설치된 jsdom의 WebSocket 구현을 공유한다.
const vite = await createServer({ configFile: false, root: path.join(root, 'UI'), server: { middlewareMode: true, watch: null, hmr: false }, appType: 'custom' });
const { BackendUiAdapter } = await vite.ssrLoadModule('/src/shared/api/BackendUiAdapter.ts');
const { BackendConnectionDiagnosticWriter } = await vite.ssrLoadModule('/src/shared/api/backendConnectionDiagnostics.ts');
const stderr = await open(path.join(output, 'backend-stderr.log'), 'wx', 0o600);
const child = spawn(path.join(root, 'backend/.venv/bin/python'), [path.join(root, 'scripts/backend_connection_soak_fixture.py'), path.join(output, 'backend')],
    { cwd: root, env: { ...process.env, PYTHONPATH: path.join(root, 'backend/src'), BINANCE_RUN_TESTNET: '0', BINANCE_RUN_TESTNET_ORDERS: '0' }, stdio: ['pipe', 'pipe', stderr.fd] });
const lines = createInterface({ input: child.stdout });
const iterator = lines[Symbol.asyncIterator]();


/**
 * 함수 이름: nextLine()
 * 기능: 로컬 fixture의 다음 JSON 응답을 제한 시간 안에 읽는다.
 * 인자: 없음
 * 반환값: 해석한 응답 Promise; 시간 초과·프로세스 종료 시 예외
 * 작성 날짜: 2026/09/17
 */
async function nextLine() {
    let timer;
    try {
        const value = await Promise.race([iterator.next(), new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('SOAK_PIPE_TIMEOUT')), 10_000); })]);
        if (value.done) throw new Error('SOAK_CHILD_EXITED');

        return JSON.parse(value.value);
    } finally { clearTimeout(timer); }
}


/**
 * 함수 이름: control()
 * 기능: 로컬 fixture에 제어 메시지를 보내고 다음 응답을 기다린다.
 * 인자: operation -> 제어 종류, fields -> 추가 입력값
 * 반환값: fixture 응답 Promise
 * 작성 날짜: 2026/09/17
 */
async function control(operation, fields = {}) { child.stdin.write(`${JSON.stringify({ operation, ...fields })}\n`); return nextLine(); }
const descriptor = await nextLine();
const metrics = await open(path.join(output, 'metrics.jsonl'), 'wx', 0o600);
const startedAt = new Date().toISOString();
const started = performance.now();
const deadline = started + hours * 3_600_000;
let events = 0, recoveries = 0, faults = 0, lastEvent = started, terminal = null, status = 'running';
let socket = null, liveSockets = 0, maxSockets = 0, writeFailureUntil = 0, logBytes = 0, logPart = 1;
let backendMetrics = {}, nextFault = started + faultSeconds * 1000;
let idleUntil = 0, nextIdle = started + Math.min(60_000, faultSeconds * 500);
const counts = {};
const writer = new BackendConnectionDiagnosticWriter(async (batch) => {
    if (performance.now() < writeFailureUntil) throw new Error('INJECTED_LOG_STORAGE_FAILURE');

    // Node 파일 sink를 사용한다. Tauri IPC·Rust writer는 별도 native 테스트로 검증한다.
    const bytes = Buffer.from(batch.map((record) => JSON.stringify(record)).join('\n') + '\n');
    if (logBytes + bytes.length > 5 * 1024 * 1024) { logPart++; logBytes = 0; }

    const file = await open(path.join(output, `ui-part${String(logPart).padStart(4, '0')}.jsonl`), 'a', 0o600);
    try { await file.writeFile(bytes); await file.sync(); logBytes += bytes.length; } finally { await file.close(); }
});
const adapter = new BackendUiAdapter(descriptor, {
    fetch: (input, init) => { const headers = new Headers(init.headers); headers.set('Origin', 'http://127.0.0.1:5173'); return fetch(input, { ...init, headers }); },
    create_web_socket: (url) => {
        socket = new WebSocket(url, { origin: 'http://127.0.0.1:5173' });
        liveSockets++; maxSockets = Math.max(maxSockets, liveSockets);
        socket.once('close', () => { liveSockets--; });
        socket.on('error', () => {});  // 원본 오류 문자열은 stdout/stderr에 전파하지 않는다.
        return socket;
    },
    diagnostic: (record) => { counts[record.event] = (counts[record.event] ?? 0) + 1; writer.record(record); },
});


/**
 * 함수 이름: report()
 * 기능: 누적 수신·복구·자원 통계를 JSONL과 현재 상태 파일에 기록한다.
 * 인자: 없음
 * 반환값: 기록 완료 Promise
 * 작성 날짜: 2026/09/17
 */
async function report() {
    const record = { status, started_at: startedAt, at: new Date().toISOString(), elapsed_seconds: (performance.now() - started) / 1000,
        target_hours: hours, events, recoveries, faults, live_sockets: liveSockets, max_sockets: maxSockets,
        since_last_event_seconds: (performance.now() - lastEvent) / 1000, terminal, node_memory: process.memoryUsage(),
        active_resources: process.getActiveResourcesInfo(), backend: backendMetrics, counts, live_orders_sent: 0,
        scope: 'real_loopback_transport_and_ui_adapter; simulated_read_only_runtime; node_diagnostic_sink' };
    await metrics.writeFile(`${JSON.stringify(record)}\n`);
    await writeFile(path.join(output, 'status.json'), `${JSON.stringify(record, null, 2)}\n`);
}
let interrupted = false;
process.on('SIGTERM', () => { interrupted = true; });
process.on('SIGINT', () => { interrupted = true; });
try {
    const snapshot = await adapter.load_snapshot();
    adapter.start_live_events(snapshot, { on_event: () => { events++; lastEvent = performance.now(); },
        on_full_resync: () => {}, on_reconnecting: () => {}, on_ready: () => { recoveries++; },
        on_failure: (error) => { terminal = error.code; } });
    while (performance.now() < deadline && !interrupted) {
        const now = performance.now();
        if (terminal || child.exitCode !== null || now - lastEvent > 120_000) throw new Error('SOAK_LIVENESS_FAILURE');
        if (idleUntil && now >= idleUntil) { await control('idle', { enabled: false }); idleUntil = 0; }
        if (hours >= 0.03 && !idleUntil && now >= nextIdle) {
            await control('idle', { enabled: true }); idleUntil = now + 80_000; nextIdle = now + 3_600_000;
        }
        if (deadline - now > 15_000 && now >= nextFault && !idleUntil && liveSockets > 0 && now - lastEvent < 10_000) {
            await control('fault'); faults++; nextFault = now + faultSeconds * 1000;

            // 첫 장애 때 로그 저장도 잠시 실패시켜 최초 오류가 복구 후 남는지 관찰한다.
            if (faults === 1) writeFailureUntil = now + 6_000;
            socket?.terminate();
        }
        backendMetrics = await control('metrics');
        if (backendMetrics.worker_failed) throw new Error('SOAK_WORKER_FAILED');
        await report();
        await delay(Math.min(5_000, Math.max(0, deadline - performance.now())));
    }
    if (!interrupted && (events === 0 || recoveries !== faults || maxSockets > 2)) throw new Error('SOAK_INCOMPLETE_RECOVERY');
    status = interrupted ? 'interrupted' : 'completed_needs_review';
} catch (error) {
    terminal ??= error instanceof Error && /^[A-Z_]+$/u.test(error.message) ? error.message : 'SOAK_FAILED';
    status = 'failed'; process.exitCode = 1;
} finally {
    adapter.stop(); await writer.flush();
    child.stdin.end();
    await new Promise((resolve) => { if (child.exitCode !== null) resolve(); else child.once('exit', resolve); });
    await report(); await metrics.close(); await stderr.close(); await vite.close();
    console.log(JSON.stringify({ status, output, events, recoveries, faults }));
}
