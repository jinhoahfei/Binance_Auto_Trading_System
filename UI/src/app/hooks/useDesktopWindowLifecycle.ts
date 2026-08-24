import { useEffect, useState } from 'react';

import {
    create_desktop_window_lifecycle,
    type DesktopWindowLifecycle,
} from '../runtime/DesktopWindowLifecycle';
import type { UiApplicationController } from '../runtime/UiApplicationStore';

/**
 * 함수 이름: create_window_lifecycle()
 * 기능: React App mount가 한 번만 소유할 desktop window lifecycle port를 생성한다.
 * 인자: 없음
 * 반환값: Tauri 창 port 또는 브라우저 실행을 나타내는 null
 * 작성 날짜: 2026/08/12
 */
function create_window_lifecycle(): DesktopWindowLifecycle | null {
    return create_desktop_window_lifecycle();
}

/**
 * 함수 이름: report_window_lifecycle_error()
 * 기능: desktop window IPC 실패를 개발자 console에 남겨 종료 lifecycle 진단 정보를 보존한다.
 * 인자: operation -> 실패한 창 작업, error -> Tauri가 반환한 오류
 * 반환값: 없음
 * 작성 날짜: 2026/08/12
 */
function report_window_lifecycle_error(operation: string, error: unknown): void {
    console.error(`[desktop-window] ${operation} 작업을 완료하지 못했습니다.`, error);
}

/**
 * 함수 이름: use_desktop_window_lifecycle()
 * 기능: OS 닫기 요청을 APP_EXIT_CLICKED intent로 막아 전달하고 final 상태에서 실제 창을 제거한다.
 * 인자: controller -> UI actor controller, is_final -> appExitMachine의 최종 상태 여부
 * 반환값: 없음
 * 작성 날짜: 2026/08/12
 */
export function use_desktop_window_lifecycle(
    controller: UiApplicationController,
    is_final: boolean,
): void {
    const [window_lifecycle] = useState(create_window_lifecycle);

    useEffect(() => {
        if (window_lifecycle === null) {
            return;
        }

        let is_disposed = false;
        let remove_close_listener: (() => void) | null = null;

        void window_lifecycle.on_close_requested((request) => {
            request.preventDefault();
            controller.dispatch({ type: 'APP_EXIT_CLICKED' });
        }).then((remove_listener) => {
            if (is_disposed) {
                remove_listener();
                return;
            }

            remove_close_listener = remove_listener;
        }).catch((error: unknown) => {
            report_window_lifecycle_error('닫기 요청 구독', error);
        });

        return () => {
            is_disposed = true;
            remove_close_listener?.();
        };
    }, [controller, window_lifecycle]);

    useEffect(() => {
        if (!is_final || window_lifecycle === null) {
            return;
        }

        // Window close, Command-Q와 sidecar crash recovery 모두 같은 final에서 native 창을 제거한다.
        void window_lifecycle.destroy().catch((error: unknown) => {
            report_window_lifecycle_error('창 제거', error);
        });
    }, [is_final, window_lifecycle]);
}
