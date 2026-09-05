/**
 * 함수 이름: report()
 * 기능: 개발 서버에 고정 검사 단계와 연결 상태만 전달한다.
 * 인자: stage -> 검사 단계, details -> 비밀 없는 상태 enum
 * 반환값: 결과 전달 완료 Promise
 * 작성 날짜: 2026/09/05
 */
async function report(stage: string, details: Record<string, string> = {}): Promise<void> {
    await fetch(`/__desktop_smoke?${new URLSearchParams({ stage, ...details })}`, {
        method: 'POST',
    });
}

/**
 * 함수 이름: wait_for()
 * 기능: 실제 WebView에서 준비된 DOM을 기다리며 bootstrap 실패와 timeout을 구분한다.
 * 인자: read -> DOM 조회, code -> timeout 식별자, timeout_ms -> 최대 대기 시간
 * 반환값: 준비된 조회 결과 Promise
 * 작성 날짜: 2026/09/05
 */
async function wait_for<T>(
    read: () => T | null | false,
    code: string,
    timeout_ms = 90_000,
): Promise<T> {
    const deadline = Date.now() + timeout_ms;
    while (Date.now() < deadline) {
        const failure = document.querySelector('[data-bootstrap-status="failure"] small');
        if (failure !== null) {
            throw new Error(failure.textContent ?? 'BOOTSTRAP_FAILED');
        }
        const result = read();
        if (result !== null && result !== false) {
            return result;
        }
        await new Promise((resolve) => setTimeout(resolve, 100));
    }
    throw new Error(code);
}

/**
 * 함수 이름: run_desktop_smoke()
 * 기능: 실제 Tauri·백엔드에서 화면/hover/연결 상태를 검증하고 앱을 열린 상태로 유지한다.
 * 인자: 없음
 * 반환값: 검사 완료 Promise
 * 작성 날짜: 2026/09/05
 */
async function run_desktop_smoke(): Promise<void> {
    if (!('__TAURI_INTERNALS__' in window)) {
        throw new Error('TAURI_RUNTIME_REQUIRED');
    }
    const badge = await wait_for(
        () => document.querySelector<HTMLButtonElement>('button[aria-label="Binance 연결 상태: LIVE"]'),
        'DASHBOARD_TIMEOUT',
    );
    await report('dashboard-ready');

    // Synthetic pointer 이동도 실제 React handler와 backend 조회 경로를 통과한다.
    badge.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, relatedTarget: document.body }));
    const tooltip = await wait_for(() => {
        const element = document.querySelector<HTMLElement>('[role="tooltip"]');
        return element?.querySelectorAll('dd[data-state]').length === 3 ? element : null;
    }, 'TOOLTIP_STATUS_TIMEOUT');
    const states = [...tooltip.querySelectorAll('dd')].map((row) => row.dataset.state);
    if (states.some((state) => state !== 'online' && state !== 'offline')) {
        throw new Error('INVALID_CONNECTION_STATUS');
    }
    const labels = [...tooltip.querySelectorAll('dt')].map((row) => row.textContent);
    if (labels.join('|') !== 'API|시세 WebSocket|계좌 WebSocket'
        || getComputedStyle(tooltip).pointerEvents !== 'none'
        || tooltip.getBoundingClientRect().width <= 0) {
        throw new Error('INVALID_TOOLTIP_LAYOUT');
    }
    await report('tooltip-status', {
        api: states[0]!, market_stream: states[1]!, account_stream: states[2]!,
    });

    badge.dispatchEvent(new MouseEvent('mouseout', { bubbles: true, relatedTarget: document.body }));
    await wait_for(() => document.querySelector('[role="tooltip"]') === null, 'TOOLTIP_DID_NOT_CLOSE', 1_000);
    await report('tooltip-hidden');
    badge.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, relatedTarget: document.body }));
    await wait_for(() => document.querySelector('[role="tooltip"]'), 'TOOLTIP_DID_NOT_REOPEN', 1_000);
    await report('tooltip-reopened');
    badge.dispatchEvent(new MouseEvent('mouseout', { bubbles: true, relatedTarget: document.body }));

    await wait_for(() => document.querySelector('[role="tooltip"]') === null, 'TOOLTIP_DID_NOT_CLOSE', 1_000);
    // 초기 chart frame과 비동기 상태 갱신까지 관찰한 뒤 고정 성공 결과만 남긴다.
    await new Promise((resolve) => setTimeout(resolve, 5_000));
    if (runtime_error_observed) {
        throw new Error('UNHANDLED_RENDERER_ERROR');
    }
    await report('passed');
}

let runtime_error_observed = false;
const remember_runtime_error = (): void => { runtime_error_observed = true; };
window.addEventListener('error', remember_runtime_error);
window.addEventListener('unhandledrejection', remember_runtime_error);
void run_desktop_smoke().catch(async (error: unknown) => {
    const code = error instanceof Error && /^[A-Z][A-Z0-9_]{1,63}$/u.test(error.message)
        ? error.message : 'DESKTOP_SMOKE_FAILED';
    await report('failed', { code });
}).finally(() => {
    window.removeEventListener('error', remember_runtime_error);
    window.removeEventListener('unhandledrejection', remember_runtime_error);
});
