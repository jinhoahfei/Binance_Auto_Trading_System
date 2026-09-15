import type { BackendConnectionStatus } from '../../../shared/api/BackendUiAdapter';
import { assign, setup } from 'xstate';

export interface ConnectionMachineContext {
    readonly recovery: BackendConnectionStatus | null;
    readonly status: 'offline' | 'connecting' | 'online' | 'reconnecting';
    readonly reconnect_attempt: number;
    readonly last_sequence: number | null;
    readonly last_error: string | null;
}

export type ConnectionMachineEvent =
    | { readonly type: 'CONNECT_REQUESTED' }
    | { readonly type: 'API_CONNECTED'; readonly sequence?: number }
    | { readonly type: 'API_DISCONNECTED'; readonly reason?: string }
    | { readonly type: 'RECONNECT_REQUESTED' }
    | { readonly type: 'RECONNECT_FAILED'; readonly reason: string }
    | { readonly type: 'BACKEND_CONNECTION_STATUS'; readonly status: BackendConnectionStatus };

/**
 * 함수 이름: create_connection_machine()
 * 기능: API 연결, 재연결 진행, 온라인 및 오프라인 표시 상태를 관리하는 XState actor logic을 생성한다.
 * 인자: 없음
 * 반환값: connection-status feature의 XState machine
 * 작성 날짜: 2026/08/12
 */
export function create_connection_machine() {
    return setup({
        types: {
            context: {} as ConnectionMachineContext,
            events: {} as ConnectionMachineEvent,
        },
        actions: {
            mark_connecting: assign({
                status: 'connecting',
                last_error: null,
            }),
            mark_online: assign({
                status: 'online',
                reconnect_attempt: 0,
                last_sequence: ({ context, event }) => {
                    return event.type === 'API_CONNECTED' && event.sequence !== undefined
                        ? event.sequence
                        : context.last_sequence;
                },
                last_error: null,
            }),
            mark_reconnecting: assign({
                status: 'reconnecting',
                reconnect_attempt: ({ context }) => context.reconnect_attempt + 1,
                last_error: ({ event }) => {
                    return event.type === 'API_DISCONNECTED'
                        ? event.reason ?? 'API 연결이 끊어졌습니다.'
                        : null;
                },
            }),
            mark_offline: assign({
                status: 'offline',
                last_error: ({ event }) => {
                    return event.type === 'RECONNECT_FAILED'
                        ? event.reason
                        : 'API 연결이 오프라인 상태입니다.';
                },
            }),
        },
    }).createMachine({
        id: 'connectionMachine',
        initial: 'api_offline',
        on: { BACKEND_CONNECTION_STATUS: { actions: assign({ recovery: ({ event }) => event.status }) } },
        context: {
            recovery: null,
            status: 'offline',
            reconnect_attempt: 0,
            last_sequence: null,
            last_error: null,
        },
        states: {
            api_offline: {
                meta: {
                    spec_ids: ['U1-01', 'U1-03', 'CR-01'],
                },
                on: {
                    CONNECT_REQUESTED: {
                        target: 'connecting',
                        actions: 'mark_connecting',
                    },
                    RECONNECT_REQUESTED: {
                        target: 'reconnecting',
                        actions: 'mark_reconnecting',
                    },
                    API_CONNECTED: {
                        target: 'api_online',
                        actions: 'mark_online',
                    },
                },
            },
            connecting: {
                meta: {
                    spec_ids: ['U1-02'],
                    pending: true,
                },
                on: {
                    API_CONNECTED: {
                        target: 'api_online',
                        actions: 'mark_online',
                    },
                    API_DISCONNECTED: {
                        target: 'api_offline',
                        actions: 'mark_offline',
                    },
                },
            },
            api_online: {
                meta: {
                    spec_ids: ['U1-02', 'CR-01'],
                },
                on: {
                    API_DISCONNECTED: {
                        target: 'reconnecting',
                        actions: 'mark_reconnecting',
                    },
                },
            },
            reconnecting: {
                meta: {
                    spec_ids: ['U1-03', 'U3-12'],
                    pending: true,
                },
                on: {
                    API_CONNECTED: {
                        target: 'api_online',
                        actions: 'mark_online',
                    },
                    RECONNECT_FAILED: {
                        target: 'api_offline',
                        actions: 'mark_offline',
                    },
                },
            },
        },
    });
}

