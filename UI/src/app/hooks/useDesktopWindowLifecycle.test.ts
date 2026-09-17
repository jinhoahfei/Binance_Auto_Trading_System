import { renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { DesktopCloseRequest } from '../runtime/DesktopWindowLifecycle';
import type { UiApplicationController } from '../runtime/UiApplicationStore';
import { use_desktop_window_lifecycle } from './useDesktopWindowLifecycle';


const lifecycle_fixture = vi.hoisted(() => ({
    destroy: vi.fn(async () => undefined),
    on_close_requested: vi.fn<
        (listener: (request: DesktopCloseRequest) => void) => Promise<() => void>
    >(async (_listener) => () => undefined),
}));

vi.mock('../runtime/DesktopWindowLifecycle', async (import_original) => {
    const original_module = await import_original<
        typeof import('../runtime/DesktopWindowLifecycle')
    >();

    return {
        ...original_module,
        create_desktop_window_lifecycle: () => lifecycle_fixture,
    };
});

describe('use_desktop_window_lifecycle Phase 12 bridge', () => {
    beforeEach(() => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        lifecycle_fixture.destroy.mockClear();
        lifecycle_fixture.on_close_requested.mockClear();
    });

    it('Command-Q처럼 renderer close callback이 없어도 final 상태에서 창을 제거한다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const controller = {
            dispatch: vi.fn(() => true),
        } as unknown as UiApplicationController;
        const hook = renderHook(
            ({ is_final }) => use_desktop_window_lifecycle(controller, is_final),
            { initialProps: { is_final: false } },
        );

        // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
        await waitFor(() => {
            expect(lifecycle_fixture.on_close_requested).toHaveBeenCalledOnce();
        });

        // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
        hook.rerender({ is_final: true });

        // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
        await waitFor(() => {
            expect(lifecycle_fixture.destroy).toHaveBeenCalledOnce();
        });
        expect(controller.dispatch).not.toHaveBeenCalled();

        // 화면 또는 실행 수명의 종료를 요청한다.
        hook.unmount();
    });

    it('renderer close callback은 기본 동작을 막고 기존 exit intent만 전달한다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const close_listener_holder: {
            current: ((request: DesktopCloseRequest) => void) | null;
        } = { current: null };
        lifecycle_fixture.on_close_requested.mockImplementationOnce(async (listener) => {
            close_listener_holder.current = listener;

            return () => undefined;
        });
        const controller = {
            dispatch: vi.fn(() => true),
        } as unknown as UiApplicationController;
        const prevent_default = vi.fn();
        const hook = renderHook(() => use_desktop_window_lifecycle(controller, false));

        // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
        await waitFor(() => {
            expect(close_listener_holder.current).not.toBeNull();
        });
        close_listener_holder.current?.({ preventDefault: prevent_default });

        // Native bridge와 중복돼도 machine이 멱등 처리할 동일 intent만 전달한다.
        expect(prevent_default).toHaveBeenCalledOnce();
        expect(controller.dispatch).toHaveBeenCalledWith({ type: 'APP_EXIT_CLICKED' });
        expect(lifecycle_fixture.destroy).not.toHaveBeenCalled();

        // 화면 또는 실행 수명의 종료를 요청한다.
        hook.unmount();
    });
});
