// 이 개발 전용 entry는 main보다 먼저 실행되어 실제 응답의 표시용 지표 한 필드만 손상시킨다.
const original_fetch = globalThis.fetch.bind(globalThis);
let shutdown_accepted = false;

/**
 * 함수 이름: report_recovery_stage()
 * 기능: token·계좌·응답 본문 없이 고정된 실제 종료 검증 단계를 보고한다.
 * 인자: stage -> 고정 단계, code -> 고정 실패 식별자
 * 반환값: 보고 완료 Promise
 * 작성 날짜: 2026/09/05
 */
async function report_recovery_stage(stage: string, code?: string): Promise<void> {
    await original_fetch(`/__desktop_smoke?${new URLSearchParams({
        stage, ...(code === undefined ? {} : { code }),
    })}`, { method: 'POST' });
}

/**
 * 함수 이름: inject_malformed_dashboard_response()
 * 기능: 실제 backend snapshot 응답의 지표만 변경하고 종료 요청·응답은 원본 그대로 통과시킨다.
 * 인자: input -> 원본 요청 URL, init -> 원본 fetch option
 * 반환값: 표시용 fault를 주입한 snapshot 또는 원본 Response
 * 작성 날짜: 2026/09/05
 */
async function inject_malformed_dashboard_response(
    input: RequestInfo | URL,
    init?: RequestInit,
): Promise<Response> {
    const response = await original_fetch(input, init);
    const url = new URL(input instanceof Request ? input.url : input.toString(), location.href);
    if (url.pathname === '/v1/snapshot' && response.ok) {
        const payload = await response.clone().json();
        const indicator = payload?.data?.regime?.indicator;
        if (payload?.ok !== true || indicator === null || typeof indicator !== 'object') {
            throw new Error('SMOKE_SNAPSHOT_UNAVAILABLE');
        }
        // Backend의 거래 상태는 변경하지 않으며 UI가 받는 복사본만 계속 malformed로 만든다.
        indicator.current_price = 'invalid';
        return new Response(JSON.stringify(payload), {
            status: response.status,
            headers: response.headers,
        });
    }
    if (url.pathname === '/v1/shutdown/state' && response.ok) {
        await report_recovery_stage('recovery-shutdown-state');
    }
    if (url.pathname === '/v1/shutdown' && response.status === 202) {
        shutdown_accepted = true;
        await report_recovery_stage('recovery-shutdown-accepted');
    }
    return response;
}

/**
 * 함수 이름: run_recovery_shutdown_smoke()
 * 기능: 실제 Tauri 복구 화면의 안전 종료를 누르고 HTTP 202 이후 native 종료가 이어지게 한다.
 * 인자: 없음
 * 반환값: 202 관찰 또는 고정 실패 Promise
 * 작성 날짜: 2026/09/05
 */
async function run_recovery_shutdown_smoke(): Promise<void> {
    if (!('__TAURI_INTERNALS__' in window)) {
        throw new Error('TAURI_RUNTIME_REQUIRED');
    }
    const deadline = Date.now() + 90_000;
    while (Date.now() < deadline) {
        const failure = document.querySelector('[data-bootstrap-status="failure"] small');
        if (failure !== null) {
            if (failure.textContent !== 'MALFORMED_BACKEND_PAYLOAD') {
                throw new Error('UNEXPECTED_BOOTSTRAP_FAILURE');
            }
            const button = [...document.querySelectorAll<HTMLButtonElement>('button')]
                .find((candidate) => candidate.textContent === '안전 종료' && !candidate.disabled);
            if (button !== undefined && button.getBoundingClientRect().width > 0) {
                await report_recovery_stage('recovery-visible');
                button.click();
                break;
            }
        }
        await new Promise((resolve) => setTimeout(resolve, 100));
    }
    if (Date.now() >= deadline) {
        throw new Error('RECOVERY_SCREEN_TIMEOUT');
    }
    while (Date.now() < deadline && !shutdown_accepted) {
        await new Promise((resolve) => setTimeout(resolve, 100));
    }
    if (!shutdown_accepted) {
        throw new Error('RECOVERY_SHUTDOWN_TIMEOUT');
    }
    // Code 0과 RELEASED는 실행 도구가 확인한다. 창이 계속 남으면 성공으로 처리하지 않는다.
    setTimeout(() => {
        void report_recovery_stage('failed', 'NATIVE_EXIT_TIMEOUT');
    }, 30_000);
}

globalThis.fetch = inject_malformed_dashboard_response;
void run_recovery_shutdown_smoke().catch(async (error: unknown) => {
    const code = error instanceof Error && /^[A-Z][A-Z0-9_]{1,63}$/u.test(error.message)
        ? error.message : 'RECOVERY_SHUTDOWN_FAILED';
    await report_recovery_stage('failed', code);
});

export {};
