import { invoke } from '@tauri-apps/api/core';
import { listen, type UnlistenFn } from '@tauri-apps/api/event';
import { getCurrentWindow } from '@tauri-apps/api/window';
import { StrictMode, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';

import { App } from './app/App';
import {
    create_live_ui_application_factory,
    hydrate_live_ui_application,
    type LiveUiApplication,
} from './app/bootstrap';
import { AppProviders } from './app/providers/AppProviders';
import {
    is_expected_normal_sidecar_exit,
    validate_native_exit_intent_bridge_receipt,
    validate_native_exit_request_payload,
    validate_native_sidecar_exit_payload,
    type NativeSidecarExitPayload,
} from './app/runtime/NativeSidecarLifecycle';
import {
    BackendAdapterError,
    BackendCommandError,
    BackendContractError,
    BackendUiAdapter,
    validate_connection_descriptor,
} from './shared/api';
import './shared/styles/global.css';

/**
 * live bootstrap loading 또는 typed failure만 표시하는 최소 startup boundary이다.
 */
interface BootstrapStatusProps {
    readonly actions?: ReactNode;
    readonly code?: string;
    readonly children: ReactNode;
    readonly detail?: ReactNode;
    readonly is_failure?: boolean;
}

/**
 * 함수 이름: BootstrapStatus()
 * 기능: snapshot이 준비되기 전에는 demo/부분 dashboard 대신 loading 또는 failure 상태만 표시한다.
 * 인자: props -> 안전한 상태 문구, optional failure code와 failure 여부
 * 반환값: startup 상태 React 요소
 * 작성 날짜: 2026/08/21
 */
function BootstrapStatus({
    actions,
    children,
    code,
    detail,
    is_failure = false,
}: BootstrapStatusProps) {
    return (
        <main
            aria-busy={!is_failure}
            aria-live="polite"
            data-bootstrap-status={is_failure ? 'failure' : 'loading'}
            style={{
                alignItems: 'center',
                background: '#0b0e11',
                color: '#eaecef',
                display: 'flex',
                flexDirection: 'column',
                gap: '12px',
                justifyContent: 'center',
                minHeight: '100vh',
                padding: '24px',
                textAlign: 'center',
            }}
        >
            <strong>{children}</strong>
            {code === undefined ? null : <small>{code}</small>}
            {detail === undefined ? null : <span>{detail}</span>}
            {actions === undefined ? null : (
                <div
                    style={{
                        display: 'flex',
                        flexWrap: 'wrap',
                        gap: '8px',
                        justifyContent: 'center',
                    }}
                >
                    {actions}
                </div>
            )}
        </main>
    );
}

/**
 * 함수 이름: is_tauri_runtime()
 * 기능: production descriptor IPC를 호출할 수 있는 Tauri renderer인지 확인한다.
 * 인자: 없음
 * 반환값: Tauri internal marker 존재 여부
 * 작성 날짜: 2026/08/21
 */
function is_tauri_runtime(): boolean {
    return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
}

/**
 * 함수 이름: safe_bootstrap_failure_code()
 * 기능: secret이 없는 typed adapter failure code만 startup UI에 노출한다.
 * 인자: error -> descriptor IPC 또는 snapshot bootstrap에서 발생한 unknown 오류
 * 반환값: 공개 가능한 failure code
 * 작성 날짜: 2026/08/21
 */
function safe_bootstrap_failure_code(error: unknown): string {
    if (error instanceof BackendAdapterError
        || error instanceof BackendCommandError
        || error instanceof BackendContractError) {
        return error.code;
    }
    if (typeof error === 'object'
        && error !== null
        && 'code' in error
        && typeof error.code === 'string'
        && /^[A-Z][A-Z0-9_]{1,63}$/u.test(error.code)) {
        // Native invoke failure에서는 allowlisted code shape만 읽고 message/details는 반사하지 않는다.
        return error.code;
    }

    return 'LIVE_BOOTSTRAP_FAILED';
}

/**
 * 함수 이름: bootstrap_live_renderer()
 * 기능: native descriptor와 ready snapshot을 받은 뒤에만 production App factory를 렌더링한다.
 * 인자: root -> 이미 생성한 React root
 * 반환값: startup 완료 Promise
 * 작성 날짜: 2026/08/21
 */
async function bootstrap_live_renderer(root: Root): Promise<void> {
    root.render(
        <StrictMode>
            <BootstrapStatus>백엔드 상태를 불러오는 중입니다.</BootstrapStatus>
        </StrictMode>,
    );

    let remove_sidecar_exit_listener: UnlistenFn | null = null;
    let remove_native_exit_request_listener: UnlistenFn | null = null;
    let pending_native_exit_request = false;
    let pending_invalid_native_exit_request = false;
    let handle_native_exit_request: ((is_invalid: boolean) => void) | null = null;
    let pending_sidecar_exit_payload: NativeSidecarExitPayload | null = null;
    let handle_sidecar_exit: ((payload: NativeSidecarExitPayload) => void) | null = null;
    let live_application: LiveUiApplication | null = null;
    let application_is_active = false;
    let recovery_adapter: BackendUiAdapter | null = null;
    let recovery_error: unknown = null;
    let recovery_is_pending = false;
    let recovery_is_visible = false;
    let recovery_snapshot_is_loaded = false;
    let recovery_sidecar_has_exited = false;
    let recovery_window_is_finalized = false;

    /**
     * 함수 이름: remove_native_listeners()
     * 기능: child exit 또는 window destroy가 확정된 뒤 native listener 두 개를 한 번만 제거한다.
     * 인자: 없음
     * 반환값: 없음
     * 작성 날짜: 2026/08/24
     */
    function remove_native_listeners(): void {
        remove_sidecar_exit_listener?.();
        remove_sidecar_exit_listener = null;
        remove_native_exit_request_listener?.();
        remove_native_exit_request_listener = null;
    }

    /**
     * 함수 이름: render_bootstrap_recovery()
     * 기능: READY child와 token을 보존한 bootstrap 재시도·안전 종료 operator surface를 표시한다.
     * 인자: 없음
     * 반환값: 없음
     * 작성 날짜: 2026/08/24
     */
    function render_bootstrap_recovery(): void {
        recovery_is_visible = true;
        const failure_code = safe_bootstrap_failure_code(recovery_error);
        const recovery_detail = recovery_sidecar_has_exited
            ? '백엔드 프로세스가 이미 종료되었습니다. 새 주문은 차단되었으며 창만 닫을 수 있습니다.'
            : pending_native_exit_request
                ? '창 닫기 또는 Command-Q 요청을 감지했습니다. 아래 안전 종료를 확인해 주세요.'
                : '실행 중인 백엔드와 인증 연결을 보존했습니다. 연결을 다시 확인하거나 안전 종료할 수 있습니다.';
        const action_style = {
            background: '#f0b90b',
            border: 0,
            borderRadius: '6px',
            color: '#0b0e11',
            cursor: recovery_is_pending ? 'wait' : 'pointer',
            fontWeight: 600,
            padding: '9px 14px',
        } as const;

        root.render(
            <StrictMode>
                <BootstrapStatus
                    actions={recovery_sidecar_has_exited ? (
                        <button
                            disabled={recovery_is_pending}
                            onClick={() => {
                                void finalize_recovery_window();
                            }}
                            style={action_style}
                            type="button"
                        >
                            창 닫기
                        </button>
                    ) : (
                        <>
                            <button
                                disabled={recovery_is_pending || recovery_adapter === null}
                                onClick={() => {
                                    void retry_live_bootstrap();
                                }}
                                style={action_style}
                                type="button"
                            >
                                연결 다시 확인
                            </button>
                            <button
                                disabled={recovery_is_pending || recovery_adapter === null}
                                onClick={() => {
                                    void safely_shutdown_recovery_child();
                                }}
                                style={action_style}
                                type="button"
                            >
                                안전 종료
                            </button>
                        </>
                    )}
                    code={failure_code}
                    detail={recovery_detail}
                    is_failure
                >
                    {recovery_is_pending
                        ? '백엔드 복구 상태를 확인하는 중입니다.'
                        : '백엔드 연결 복구가 필요합니다.'}
                </BootstrapStatus>
            </StrictMode>,
        );
    }

    /**
     * 함수 이름: finalize_recovery_window()
     * 기능: child가 끝난 recovery 경로에서 listener를 제거하고 native window를 실제로 파괴한다.
     * 인자: 없음
     * 반환값: window destroy 완료 Promise
     * 작성 날짜: 2026/08/24
     */
    async function finalize_recovery_window(): Promise<void> {
        if (recovery_window_is_finalized) {
            return;
        }

        recovery_window_is_finalized = true;
        remove_native_listeners();
        try {
            await getCurrentWindow().destroy();
        } catch {
            // Child가 끝난 뒤 destroy 실패는 secret 없는 typed recovery로만 다시 표시한다.
            recovery_window_is_finalized = false;
            recovery_error = new BackendAdapterError(
                'WINDOW_DESTROY_FAILED',
                '안전 종료 뒤 창을 닫지 못했습니다. 다시 시도해 주세요.',
                true,
            );
            render_bootstrap_recovery();
        }
    }

    /**
     * 함수 이름: safely_shutdown_recovery_child()
     * 기능: bootstrap failure에서도 같은 adapter로 snapshot을 복원하고 정상 sidecar exit 뒤에만 창을 닫는다.
     * 인자: 없음
     * 반환값: 안전 종료 시도 완료 Promise
     * 작성 날짜: 2026/08/24
     */
    async function safely_shutdown_recovery_child(): Promise<void> {
        if (recovery_adapter === null || recovery_is_pending || recovery_sidecar_has_exited) {
            return;
        }

        recovery_is_pending = true;
        render_bootstrap_recovery();
        try {
            if (!recovery_snapshot_is_loaded) {
                await recovery_adapter.load_snapshot();
                recovery_snapshot_is_loaded = true;
            }
            await recovery_adapter.shutdown_application();
            recovery_sidecar_has_exited = true;
            await finalize_recovery_window();
        } catch (error) {
            recovery_error = error;
            recovery_is_pending = false;
            if (error instanceof BackendCommandError
                && (error.code === 'STALE_CONTEXT_VERSION'
                    || error.code === 'SHUTDOWN_BLOCKED_BY_OPEN_EXPOSURE')) {
                // Definitive pre-202 state drift만 다음 operator 시도에서 snapshot을 새로 읽는다.
                recovery_snapshot_is_loaded = false;
            } else if (error instanceof BackendAdapterError
                && error.code === 'SHUTDOWN_SAFETY_TIMEOUT') {
                recovery_snapshot_is_loaded = false;
            }
            render_bootstrap_recovery();
        }
    }

    /**
     * 함수 이름: install_live_application()
     * 기능: recovery 가능한 cold application에 native exit handlers를 연결하고 React activation 뒤 replay한다.
     * 인자: application -> snapshot hydration을 마친 live runtime
     * 반환값: 없음
     * 작성 날짜: 2026/08/24
     */
    function install_live_application(application: LiveUiApplication): void {
        live_application = application;
        recovery_is_visible = false;
        recovery_is_pending = false;
        application_is_active = false;

        const application_factory = create_live_ui_application_factory(application, () => {
            application_is_active = true;

            // Last-known snapshot을 먼저 publish한 뒤 child exit를 우선 replay해 신규 주문을 차단한다.
            if (pending_sidecar_exit_payload !== null) {
                handle_sidecar_exit?.(pending_sidecar_exit_payload);
                pending_sidecar_exit_payload = null;
            }
            if (pending_invalid_native_exit_request) {
                handle_native_exit_request?.(true);
            } else if (pending_native_exit_request) {
                handle_native_exit_request?.(false);
            }
            pending_invalid_native_exit_request = false;
            pending_native_exit_request = false;
        });

        root.render(
            <StrictMode>
                <AppProviders>
                    <App applicationFactory={application_factory} />
                </AppProviders>
            </StrictMode>,
        );
    }

    /**
     * 함수 이름: retry_live_bootstrap()
     * 기능: descriptor를 다시 소비하지 않고 보존한 adapter로 snapshot hydration만 재시도한다.
     * 인자: 없음
     * 반환값: 재시도 완료 Promise
     * 작성 날짜: 2026/08/24
     */
    async function retry_live_bootstrap(): Promise<void> {
        if (recovery_adapter === null || recovery_is_pending || recovery_sidecar_has_exited) {
            return;
        }

        recovery_is_pending = true;
        render_bootstrap_recovery();
        try {
            const application = await hydrate_live_ui_application(recovery_adapter);
            if (recovery_sidecar_has_exited) {
                application.deactivate();
                render_bootstrap_recovery();
                return;
            }
            recovery_snapshot_is_loaded = true;
            install_live_application(application);
        } catch (error) {
            recovery_error = error;
            recovery_is_pending = false;
            render_bootstrap_recovery();
        }
    }

    handle_sidecar_exit = (exit_payload) => {
        if (live_application === null || !application_is_active) {
            if (live_application !== null) {
                pending_sidecar_exit_payload = exit_payload;
                return;
            }

            // Bootstrap recovery에서는 dead child를 즉시 차단하되 window close는 operator 확인에 맡긴다.
            recovery_sidecar_has_exited = true;
            recovery_is_pending = false;
            recovery_adapter?.stop();
            recovery_error = new BackendAdapterError(
                is_expected_normal_sidecar_exit(exit_payload)
                    ? 'BACKEND_SIDECAR_EXITED_NORMALLY'
                    : 'BACKEND_SIDECAR_EXITED_ABNORMALLY',
                'Bootstrap 중 backend sidecar가 종료되었습니다.',
                false,
            );
            remove_sidecar_exit_listener?.();
            remove_sidecar_exit_listener = null;
            render_bootstrap_recovery();
            return;
        }

        if (is_expected_normal_sidecar_exit(exit_payload)) {
            // 늦은 clean exit도 timeout recovery를 final로 보내 stale 화면을 남기지 않는다.
            live_application.command_adapter.stop();
            live_application.facade.dispatch({
                type: 'BACKEND_SIDECAR_EXITED_NORMALLY',
            });
        } else {
            // Native crash를 socket 재연결 지연과 무관하게 즉시 fail-closed하여 새 주문을 차단한다.
            live_application.command_adapter.stop();
            live_application.facade.dispatch({
                type: 'API_DISCONNECTED',
                reason: 'BACKEND_SIDECAR_EXITED',
            });
            live_application.facade.dispatch({
                type: 'RECONNECT_FAILED',
                reason: '백엔드 프로세스가 중단되었습니다. 애플리케이션을 다시 시작해 복구해 주세요.',
            });
            live_application.facade.dispatch({
                type: 'BACKEND_SIDECAR_EXITED_ABNORMALLY',
            });
        }

        remove_sidecar_exit_listener?.();
        remove_sidecar_exit_listener = null;
    };
    handle_native_exit_request = (is_invalid) => {
        if (live_application === null || !application_is_active) {
            if (is_invalid) {
                pending_invalid_native_exit_request = true;
                recovery_error = new BackendAdapterError(
                    'NATIVE_EXIT_REQUEST_INVALID',
                    'Native 종료 요청을 확인할 수 없습니다.',
                    false,
                );
            } else {
                pending_native_exit_request = true;
            }
            if (recovery_is_visible) {
                render_bootstrap_recovery();
            }
            return;
        }
        if (is_invalid) {
            // Trusted native boundary drift는 거래 command를 차단하는 recovery로 보낸다.
            live_application.command_adapter.stop();
            live_application.facade.dispatch({
                type: 'API_DISCONNECTED',
                reason: 'NATIVE_EXIT_REQUEST_INVALID',
            });
            live_application.facade.dispatch({
                type: 'RECONNECT_FAILED',
                reason: 'Native 종료 요청을 확인할 수 없습니다. 애플리케이션을 다시 시작해 주세요.',
            });
            live_application.facade.dispatch({
                type: 'BACKEND_SIDECAR_EXITED_ABNORMALLY',
            });
            return;
        }

        // Native가 막아 둔 window close와 Command-Q를 기존 안전 종료 machine으로 단일화한다.
        live_application.facade.dispatch({ type: 'APP_EXIT_CLICKED' });
    };

    try {
        if (!is_tauri_runtime()) {
            throw new BackendAdapterError(
                'LIVE_DESCRIPTOR_UNAVAILABLE',
                'A live backend descriptor is required',
                false,
            );
        }

        // Native exit record를 먼저 arm해 JS listen 이전과 snapshot bootstrap 중 crash를 모두 replay한다.
        remove_sidecar_exit_listener = await listen<unknown>(
            'backend-sidecar-exited',
            (event) => {
                let exit_payload: NativeSidecarExitPayload;
                try {
                    exit_payload = validate_native_sidecar_exit_payload(event.payload);
                } catch {
                    exit_payload = { expected: false, code: null };
                }

                if (handle_sidecar_exit !== null) {
                    handle_sidecar_exit(exit_payload);
                } else {
                    pending_sidecar_exit_payload = exit_payload;
                }
            },
        );
        const sidecar_bridge_receipt = await invoke<unknown>(
            'arm_sidecar_exit_event_bridge',
        );
        validate_native_exit_intent_bridge_receipt(sidecar_bridge_receipt);

        // Close listener도 먼저 등록·arm해 descriptor/snapshot bootstrap 중 intent를 잃지 않는다.
        remove_native_exit_request_listener = await listen<unknown>(
            'native-exit-requested',
            (event) => {
                let is_invalid = false;
                try {
                    validate_native_exit_request_payload(event.payload);
                } catch {
                    is_invalid = true;
                }

                if (handle_native_exit_request !== null) {
                    handle_native_exit_request(is_invalid);
                    return;
                }
                if (is_invalid) {
                    pending_invalid_native_exit_request = true;
                } else {
                    pending_native_exit_request = true;
                }
            },
        );
        const bridge_receipt = await invoke<unknown>('arm_native_exit_intent_bridge');
        validate_native_exit_intent_bridge_receipt(bridge_receipt);

        // Native one-shot command는 Phase 12 launcher가 stage한 descriptor만 반환한다.
        const descriptor = validate_connection_descriptor(
            await invoke<unknown>('take_backend_connection_descriptor'),
        );
        recovery_adapter = new BackendUiAdapter(descriptor);
        recovery_is_pending = true;
        const application = await hydrate_live_ui_application(recovery_adapter);
        recovery_snapshot_is_loaded = true;
        install_live_application(application);
    } catch (error) {
        // READY child가 살아 있을 수 있으므로 token/listener를 버리지 않고 operator recovery로 남긴다.
        recovery_error = error;
        recovery_is_pending = false;
        render_bootstrap_recovery();
    }
}

const root_element = document.getElementById('root');

if (root_element === null) {
    throw new Error('React 애플리케이션을 연결할 root 요소가 없습니다.');
}

const react_root = createRoot(root_element);
void bootstrap_live_renderer(react_root);
