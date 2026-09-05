import type { BackendIndicatorSnapshot } from '../shared/contracts';

// 실제 snapshot 중 공개 시장 지표만 관찰하며 계좌·credential·요청 header는 보관하지 않는다.
const original_fetch = globalThis.fetch.bind(globalThis);
let observed_indicator: BackendIndicatorSnapshot | null = null;

/**
 * 함수 이름: observe_market_snapshot()
 * 기능: 실제 backend 응답을 변경하지 않고 최초 REGIME 지표만 검증용으로 보존한다.
 * 인자: input -> 요청 URL, init -> 원본 fetch 옵션
 * 반환값: 변경하지 않은 원본 응답
 * 작성 날짜: 2026/09/05
 */
async function observe_market_snapshot(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
    const response = await original_fetch(input, init);
    const url = new URL(input instanceof Request ? input.url : input.toString(), location.href);
    if (url.pathname === '/v1/snapshot' && response.ok) {
        const payload = await response.clone().json();
        observed_indicator = payload?.data?.regime?.indicator ?? null;  // 계좌와 token은 관찰 대상에서 제외한다.
    }
    return response;
}

globalThis.fetch = observe_market_snapshot;

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
 * 함수 이름: verify_mainnet_swing_metrics()
 * 기능: backend 평가 시점의 실제 시장 봉을 독립 조회해 스윙 판정과 실제 DOM 표시를 대조한다.
 * 인자: 없음
 * 반환값: 시세·계좌 환경과 스윙 일치 검증 완료 Promise
 * 작성 날짜: 2026/09/05
 */
async function verify_mainnet_swing_metrics(): Promise<void> {
    const environment = document.querySelector<HTMLElement>('[aria-label="시세 및 거래 환경"]');
    if (environment?.dataset.marketEnvironment !== 'mainnet'
        || environment.dataset.accountEnvironment !== 'testnet'
        || environment.dataset.ordersEnabled !== 'false'
        || environment.getBoundingClientRect().width <= 0) {
        throw new Error('INVALID_RUNTIME_ENVIRONMENT');
    }
    const indicator = await wait_for(() => observed_indicator, 'INDICATOR_SNAPSHOT_TIMEOUT');
    const evaluation_time = Date.parse(indicator.calculated_at);
    const query = new URLSearchParams({
        symbol: indicator.symbol, interval: '4h', limit: '1000', endTime: String(evaluation_time),
    });
    const response = await original_fetch(`https://data-api.binance.vision/api/v3/klines?${query}`, {
        credentials: 'omit', signal: AbortSignal.timeout(15_000),
    });
    if (!response.ok) {
        throw new Error('PUBLIC_MARKET_VERIFICATION_FAILED');
    }
    const rows: Array<Array<string | number>> = await response.json();
    const closed_rows = rows.filter((row) => Number(row[6]) < evaluation_time);
    const labels: Array<string> = [];

    // 마감된 봉의 좌우 두 지점을 비교하며 소수 가격은 정수로 바꿔 0.30% 경계 오차를 피한다.
    for (const field_index of [3, 2]) {
        const prices = closed_rows.map((row) => {
            const [whole = '0', fraction = ''] = String(row[field_index]).split('.');
            return BigInt(`${whole}${fraction.padEnd(8, '0')}`);
        });
        const pivots = prices.filter((price, index) => index >= 2 && index < prices.length - 2
            && [index - 2, index - 1, index + 1, index + 2].every((neighbor) => (
                field_index === 3 ? price < prices[neighbor]! : price > prices[neighbor]!
            )));
        const previous = pivots.at(-2);
        const latest = pivots.at(-1);
        if (previous === undefined || latest === undefined) {
            throw new Error('PUBLIC_SWING_POINTS_UNAVAILABLE');
        }
        const difference = (latest - previous) * 10_000n;
        const higher = difference >= previous * 30n;
        const lower = difference <= -previous * 30n;
        const label = higher ? (field_index === 3 ? 'HL' : 'HH')
            : lower ? (field_index === 3 ? 'LL' : 'LH') : '-';
        const expected_higher = field_index === 3 ? indicator.swing.has_higher_low : indicator.swing.has_higher_high;
        const expected_lower = field_index === 3 ? indicator.swing.has_lower_low : indicator.swing.has_lower_high;
        if (higher !== expected_higher || lower !== expected_lower) {
            throw new Error('BACKEND_MAINNET_SWING_MISMATCH');
        }
        const metric_id = field_index === 3 ? 'swingLow' : 'swingHigh';
        const metric = await wait_for(
            () => document.querySelector(`[data-metric-id="${metric_id}"] strong`),
            'REGIME_PANEL_TIMEOUT',
        );
        const value = metric.textContent;
        if (value !== label) {
            throw new Error('DISPLAYED_SWING_MISMATCH');
        }
        labels.push(label);
    }
    await report('market-parity', { swing_low: labels[0]!, swing_high: labels[1]! });
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
        () => document.querySelector<HTMLButtonElement>('button[aria-label="Binance 연결 상태: 연결됨"]'),
        'DASHBOARD_TIMEOUT',
    );
    await report('dashboard-ready');
    await verify_mainnet_swing_metrics();

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
    globalThis.fetch = original_fetch;  // 검증 종료 뒤에는 임시 관찰 wrapper도 제거한다.
    window.removeEventListener('error', remember_runtime_error);
    window.removeEventListener('unhandledrejection', remember_runtime_error);
});
