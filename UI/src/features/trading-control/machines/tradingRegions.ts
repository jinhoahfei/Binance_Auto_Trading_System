import { raise } from 'xstate';
import type { AnyStateMachine } from 'xstate';

import type { RegionNode } from '../../../app/machines/uiRegionComposition';


/**
 * 함수 이름: trading_regions()
 * 기능: 공통 매매 데이터를 참조하는 시작·중지 버튼 Region을 구성한다.
 * 인자: definition -> 매매 상태·가드·Action·작업 machine 정의
 * 반환값: 시작 Region과 중지 Region의 상태 정의
 * 작성 날짜: 2026/09/16
 */
export function trading_regions(definition: AnyStateMachine): {
    start: RegionNode;
    stop: RegionNode;
} {
    const config = definition.config as any;
    const start_state_names = [
        'stopped',
        'running',
        'select_regime_notice',
        'api_connection_required',
        'trading_unavailable_notice',
        'start_confirmation',
        'starting',
    ];
    const start_states: any = Object.fromEntries(start_state_names.map(name => [
        name,
        {
            ...config.states[name],
            on: {
                ...config.states[name].on,
            },
        },
    ]));

    delete start_states.stopped.on.STOP_BUTTON_CLICKED;
    delete start_states.running.on.STOP_BUTTON_CLICKED;
    delete start_states.running.on.API_DISCONNECTED;

    // 버튼 영역 진입만으로 공통 매매 lifecycle을 덮어쓰지 않는다.
    delete start_states.stopped.entry;
    delete start_states.running.entry;
    start_states.running.always = {
        guard: ({ context }: any) => !context.is_trading,
        target: 'stopped',
    };
    start_states.stopped.always = {
        guard: ({ context }: any) => context.is_trading,
        target: 'running',
    };

    const stop_states: any = Object.fromEntries(Object.entries(config.states).filter(([name]) => !start_state_names.includes(name)));

    stop_states.idle = {
        meta: {
            spec_ids: ['U2-01', 'U2-04'],
        },
        on: {
            STOP_BUTTON_CLICKED: [
                {
                    guard: 'has_recovered_position',
                    target: 'recovered_position_liquidation_confirmation',
                    actions: 'remember_recovery_liquidation_request',
                },
                {
                    guard: ({ context, event }: any) => context.is_trading && event.has_open_position,
                    target: 'force_sell_confirmation',
                    actions: 'remember_position',
                },
                {
                    guard: ({ context }: any) => context.is_trading,
                    target: 'stop_confirmation',
                    actions: 'remember_position',
                },
                {
                    actions: 'mark_not_running',
                },
            ],
            API_DISCONNECTED: {
                guard: ({ context }: any) => context.is_trading,
                target: 'disconnect_stopping',
            },
            API_CONNECTION_NOTICE_CONFIRMED: [
                {
                    guard: ({ context }: any) => context.is_recovery_liquidation,
                    target: 'awaiting_recovered_position_liquidation_completion',
                },
                {
                    guard: ({ context }: any) => ['stopping', 'reconciliation_required'].includes(context.lifecycle_status),
                    target: 'awaiting_stop_completion',
                },
            ],
        },
    };

    /**
     * 함수 이름: remap_targets()
     * 기능: 시작·중지 Region에서 유효한 상태로 전이 target을 재귀 연결한다.
     * 인자: value -> 원래 상태·전이 설정, scope -> start 또는 stop
     * 반환값: 해당 Region의 상태로 target을 연결한 설정
     * 작성 날짜: 2026/09/16
     */
    function remap_targets(value: any, scope: 'start' | 'stop'): any {
        if (Array.isArray(value)) {
            return value.map(item => remap_targets(item, scope));
        }

        if (!value || typeof value !== 'object') {
            return value;
        }

        return Object.fromEntries(Object.entries(value).map(([key, child]) => {
            if (key === 'target' && typeof child === 'string') {
                const leading = child.startsWith('.') ? '.' : '';
                const name = child.replace(/^\./, '');
                const mapped = scope === 'stop'
                    ? start_state_names.includes(name) ? 'idle' : name
                    : start_state_names.includes(name) ? name : 'running';

                return [key, leading + mapped];
            }

            return [key, remap_targets(child, scope)];
        }));
    }

    const stop = remap_targets({
        initial: 'idle',
        on: config.on,
        states: stop_states,
    }, 'stop');

    stop.states.recovered_position_liquidation_confirmation.on.API_DISCONNECTED.actions = [
        raise({
            type: 'trading.DISCONNECT_HANDLED',
        }),
    ];

    const disconnected = stop.states.disconnect_stopping.meta.command;
    const notify = raise({
        type: 'trading.DISCONNECT_HANDLED',
    });

    disconnected.onDone.actions = [notify];
    disconnected.onError.actions = ['remember_failure', notify];

    return {
        start: {
            initial: config.initial,
            on: {
                ...remap_targets(config.on, 'start'),
                DISCONNECT_HANDLED: {
                    target: '.api_connection_required',
                },
            },
            states: remap_targets(start_states, 'start'),
        },
        stop,
    };
}
