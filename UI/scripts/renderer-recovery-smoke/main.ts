import { invoke } from '@tauri-apps/api/core';
import { create_live_ui_application } from '../../src/app/bootstrap/createLiveUiApplication';
import { start_renderer_liveness } from '../../src/shared/api/rendererLiveness';

/**
 * 함수 이름: start_recovery_probe()
 * 기능: 실제 descriptor·snapshot 검증·단일 stream bootstrap을 주문 없는 fixture에서 실행한다.
 * 인자: 없음
 * 반환값: bootstrap 완료 Promise
 * 작성 날짜: 2026/10/04
 */
async function start_recovery_probe(): Promise<void> {
    start_renderer_liveness();
    const descriptor = await invoke('get_backend_connection_descriptor');
    const application = await create_live_ui_application(descriptor);
    application.activate();
    window.addEventListener('pagehide', () => application.deactivate(), { once: true });
}

void start_recovery_probe().catch(() => {
    // 원본 오류나 인증 정보를 출력하지 않고 native bounded timeout이 실패를 판정한다.
    document.documentElement.dataset.bootstrap = 'failed';
});
