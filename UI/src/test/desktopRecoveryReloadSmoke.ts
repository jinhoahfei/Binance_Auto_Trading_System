const phase_key = 'binance-desktop-recovery-smoke-phase';
const current_phase = sessionStorage.getItem(phase_key) ?? 'retry';
const original_fetch = globalThis.fetch.bind(globalThis);
let shutdown_accepted = false;


/**
 * 함수 이름: report_recovery_stage()
 * 기능: 연결 정보와 계좌 값 없이 실제 복구·종료 검증의 고정 단계만 기록한다.
 * 인자: stage -> 검증 단계, code -> optional 실패 식별자
 * 반환값: 기록 완료 Promise
 * 작성 날짜: 2026/09/05
 */
async function report_recovery_stage(stage: string, code?: string): Promise<void> {
    await original_fetch(`/__desktop_smoke?${new URLSearchParams({
        stage, ...(code === undefined ? {} : { code }),
    })}`, { method: 'POST' });
}


/**
 * 함수 이름: observe_shutdown_response()
 * 기능: 실제 종료 요청과 응답을 그대로 통과시키며 정상 수락 여부만 관찰한다.
 * 인자: input -> 원본 요청, init -> 원본 fetch 옵션
 * 반환값: 변경하지 않은 원본 응답
 * 작성 날짜: 2026/09/05
 */
async function observe_shutdown_response(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
    const response = await original_fetch(input, init);
    const url = new URL(input instanceof Request ? input.url : input.toString(), location.href);
    if (url.pathname === '/v1/shutdown/state' && response.ok) {
        await report_recovery_stage('recovery-shutdown-state');
    }
    if (url.pathname === '/v1/shutdown' && response.status === 202) {
        shutdown_accepted = true;
        sessionStorage.removeItem(phase_key);  // 다음 실행에 검증 단계가 남지 않게 한다.
        await report_recovery_stage('recovery-shutdown-accepted');
    }

    return response;
}


/**
 * 함수 이름: wait_for_element()
 * 기능: 실제 WebView에서 복구 버튼 또는 대시보드가 준비되는지 제한 시간 안에 확인한다.
 * 인자: read -> 관찰 함수, code -> timeout 식별자
 * 반환값: 준비된 결과 Promise
 * 작성 날짜: 2026/09/05
 */
async function wait_for_element<T>(read: () => T | null | false, code: string): Promise<T> {
    const deadline = Date.now() + 90_000;
    while (Date.now() < deadline) {
        const result = read();
        if (result !== null && result !== false) {
            return result;
        }
        await new Promise((resolve) => setTimeout(resolve, 100));
    }
    throw new Error(code);
}


/**
 * 함수 이름: run_recovery_reload_smoke()
 * 기능: 연결 재시도 성공, 같은 backend로 전체 화면 재로딩, 연결 없는 화면의 안전 종료를 차례로 검증한다.
 * 인자: 없음
 * 반환값: 현재 document의 검증 완료 Promise
 * 작성 날짜: 2026/09/05
 */
async function run_recovery_reload_smoke(): Promise<void> {
    if (current_phase !== 'reload') {
        await wait_for_element(() => document.querySelector('[data-bootstrap-status="failure"]'), 'RECOVERY_SCREEN_TIMEOUT');
        const failure = document.querySelector('[data-bootstrap-status="failure"] small');
        if (failure?.textContent !== 'BACKEND_DESCRIPTOR_UNAVAILABLE') {
            throw new Error('UNEXPECTED_BOOTSTRAP_FAILURE');
        }

        const buttons = [...document.querySelectorAll<HTMLButtonElement>('button')];
        const retry = buttons.find((button) => button.textContent === '연결 다시 확인');
        const shutdown = buttons.find((button) => button.textContent === '안전 종료');
        if (retry === undefined || shutdown === undefined || retry.disabled || shutdown.disabled) {
            throw new Error('RECOVERY_ACTION_DISABLED');
        }
        await report_recovery_stage('recovery-visible');
        (current_phase === 'shutdown' ? shutdown : retry).click();
    }
    if (current_phase === 'shutdown') {
        await wait_for_element(() => shutdown_accepted, 'RECOVERY_SHUTDOWN_TIMEOUT');

        // HTTP 202는 중간 결과다. 실행 도구가 실제 process code 0과 RELEASED까지 확인한다.
        setTimeout(() => { void report_recovery_stage('failed', 'NATIVE_EXIT_TIMEOUT'); }, 30_000);

        return;
    }

    await wait_for_element(
        () => document.querySelector('[aria-label="시세 및 거래 환경"][data-market-environment="mainnet"]'),
        'RELOADED_DASHBOARD_TIMEOUT',
    );
    await report_recovery_stage(current_phase === 'retry' ? 'recovery-retry-passed' : 'renderer-reload-passed');

    // 저장소에는 공개된 시험 단계만 넣으며 token·descriptor·backend snapshot은 보관하지 않는다.
    sessionStorage.setItem(phase_key, current_phase === 'retry' ? 'reload' : 'shutdown');
    window.location.reload();
}

// IPC 오류 주입은 개발 plugin의 main 전용 import에서만 수행하며 native 객체를 변경하지 않는다.
globalThis.fetch = observe_shutdown_response;
void run_recovery_reload_smoke().catch(async (error: unknown) => {
    const code = error instanceof Error && /^[A-Z][A-Z0-9_]{1,63}$/u.test(error.message)
        ? error.message : 'RECOVERY_RELOAD_FAILED';
    await report_recovery_stage('failed', code);
});

export {};
