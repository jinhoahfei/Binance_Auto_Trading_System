import { getCurrentWindow } from '@tauri-apps/api/window';
import { flush_backend_connection_diagnostics } from '../../shared/api/backendConnectionDiagnostics';

/**
 * OS 창 닫기 요청을 UI 상태 머신과 연결할 때 사용하는 최소 event 계약이다.
 */
export interface DesktopCloseRequest {
    preventDefault(): void;
}

/**
 * React 런타임이 Tauri 창 구현 세부사항 없이 사용하는 desktop window 계약이다.
 */
export interface DesktopWindowLifecycle {
    destroy(): Promise<void>;
    on_close_requested(
        listener: (request: DesktopCloseRequest) => void,
    ): Promise<() => void>;
}


/**
 * 클래스 이름: TauriDesktopWindowLifecycle
 * 기능: Tauri 현재 창의 닫기 요청 구독과 최종 창 제거 명령을 좁은 UI port로 변환한다.
 * 작성 날짜: 2026/08/12
 */
class TauriDesktopWindowLifecycle implements DesktopWindowLifecycle {
    /**
     * 함수 이름: destroy()
     * 기능: appExitMachine이 final 상태에 도달한 뒤 현재 Tauri 창을 제거한다.
     * 인자: 없음
     * 반환값: 창 제거가 완료될 때 resolve되는 Promise
     * 작성 날짜: 2026/08/12
     */
    async destroy(): Promise<void> {
        await flush_backend_connection_diagnostics();
        await getCurrentWindow().destroy();
    }

    /**
     * 함수 이름: on_close_requested()
     * 기능: 현재 Tauri 창의 OS 닫기 요청을 UI lifecycle listener로 전달한다.
     * 인자: listener -> 닫기 기본 동작을 막고 상태 머신을 시작할 callback
     * 반환값: Tauri event 구독 해제 함수 Promise
     * 작성 날짜: 2026/08/12
     */
    async on_close_requested(
        listener: (request: DesktopCloseRequest) => void,
    ): Promise<() => void> {
        return getCurrentWindow().onCloseRequested((request) => listener(request));
    }
}


/**
 * 함수 이름: is_tauri_runtime()
 * 기능: 현재 문서가 Tauri IPC runtime 안에서 실행되는지 전역 marker로 판별한다.
 * 인자: 없음
 * 반환값: Tauri desktop runtime이면 true
 * 작성 날짜: 2026/08/12
 */
function is_tauri_runtime(): boolean {
    return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
}


/**
 * 함수 이름: create_desktop_window_lifecycle()
 * 기능: desktop에서는 Tauri 창 port를 만들고 브라우저·테스트에서는 연결을 생략한다.
 * 인자: 없음
 * 반환값: Tauri 창 lifecycle port 또는 null
 * 작성 날짜: 2026/08/12
 */
export function create_desktop_window_lifecycle(): DesktopWindowLifecycle | null {
    return is_tauri_runtime() ? new TauriDesktopWindowLifecycle() : null;
}
