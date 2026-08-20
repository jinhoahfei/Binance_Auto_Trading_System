import { invoke } from '@tauri-apps/api/core';
import { StrictMode, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';

import { App } from './app/App';
import {
    create_live_ui_application,
    create_live_ui_application_factory,
} from './app/bootstrap';
import { AppProviders } from './app/providers/AppProviders';
import {
    BackendAdapterError,
    BackendCommandError,
    BackendContractError,
} from './shared/api';
import './shared/styles/global.css';

/**
 * live bootstrap loading 또는 typed failure만 표시하는 최소 startup boundary이다.
 */
interface BootstrapStatusProps {
    readonly code?: string;
    readonly children: ReactNode;
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
    children,
    code,
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

    try {
        if (!is_tauri_runtime()) {
            throw new BackendAdapterError(
                'LIVE_DESCRIPTOR_UNAVAILABLE',
                'A live backend descriptor is required',
                false,
            );
        }

        // Native one-shot command는 Phase 12 launcher가 stage한 descriptor만 반환한다.
        const descriptor = await invoke<unknown>('take_backend_connection_descriptor');
        const application = await create_live_ui_application(descriptor);
        const application_factory = create_live_ui_application_factory(application);

        root.render(
            <StrictMode>
                <AppProviders>
                    <App applicationFactory={application_factory} />
                </AppProviders>
            </StrictMode>,
        );
    } catch (error) {
        root.render(
            <StrictMode>
                <BootstrapStatus
                    code={safe_bootstrap_failure_code(error)}
                    is_failure
                >
                    백엔드 연결을 준비하지 못했습니다.
                </BootstrapStatus>
            </StrictMode>,
        );
    }
}

const root_element = document.getElementById('root');

if (root_element === null) {
    throw new Error('React 애플리케이션을 연결할 root 요소가 없습니다.');
}

const react_root = createRoot(root_element);
void bootstrap_live_renderer(react_root);
