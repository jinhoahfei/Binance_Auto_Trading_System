import {
    and,
    assign,
    enqueueActions,
    fromPromise,
    not,
    stateIn,
    type AnyStateMachine,
} from 'xstate';

import type { FeatureKey, UiApplicationContext, UiDomainEvent } from './uiApplicationTypes';

// XState 설정을 조립할 때 사용하는 구조 타입이다.
// 기능별 정의는 각자의 context·event 타입 검사를 유지한다.
export type RegionNode = AnyStateMachine['config'];
type RootActionArguments = {
    context: UiApplicationContext;
    event: UiDomainEvent;
    self?: any;
};
type FeatureActionArguments = {
    context: any;
    event: any;
    self: any;
};
type Transition = any;


/**
 * 함수 이름: as_array()
 * 기능: 단일 값·배열·미지정 값을 순회 가능한 배열로 정규화한다.
 * 인자: value -> 단일 값, 읽기 전용 배열 또는 undefined
 * 반환값: 미지정이면 빈 배열, 그 외에는 값 배열
 * 작성 날짜: 2026/09/16
 */
const as_array = <T,>(value: T | readonly T[] | undefined): T[] => {
    if (value === undefined) {
        return [];
    }

    return Array.isArray(value) ? [...value] : [value as T];
};


/**
 * 함수 이름: is_screen_navigation()
 * 기능: 현재 이벤트가 메인·상세 화면 사이의 이동인지 검사한다.
 * 인자: event -> 루트 내부 이벤트
 * 반환값: 화면 이동 이벤트이면 true
 * 작성 날짜: 2026/09/16
 */
const is_screen_navigation = (event: UiDomainEvent) => (
    event.type === 'shell.SHOW_ALL_TRADING_DETAILS'
    || event.type === 'shell.BACK_TO_MAIN_SCREEN'
);


/**
 * 클래스 이름: UiRegionComposition
 * 기능: 기능 정의를 공통 context·이벤트·명령에 연결하여 실제 루트 상태 노드로 조립한다.
 * 작성 날짜: 2026/09/16
 */
export class UiRegionComposition {
    readonly fallback: Record<string, Transition[]> = {};
    readonly initial: Partial<Record<FeatureKey, unknown>> = {};
    private readonly registered = new Set<string>();
    private readonly command_starts = new Map<string, any[]>();

    /**
     * 함수 이름: UiRegionComposition.constructor()
     * 기능: 루트에 조립할 기능별 machine 정의를 보관한다.
     * 인자: definitions -> 기능별 상태·가드·Action·명령 정의
     * 반환값: 생성된 UiRegionComposition 인스턴스
     * 작성 날짜: 2026/09/16
     */
    constructor(private readonly definitions: Record<FeatureKey, AnyStateMachine>) {}

    /**
     * 함수 이름: scope_args()
     * 기능: 루트 Action 인자를 해당 기능의 context와 원래 이벤트로 투영한다.
     * 인자: feature -> 대상 기능, action_arguments -> 루트 context·event·actor 인자
     * 반환값: 기능 context와 공통 요청을 포함한 실행 인자
     * 작성 날짜: 2026/09/16
     */
    scope_args(feature: FeatureKey, action_arguments: RootActionArguments): FeatureActionArguments {
        return {
            ...action_arguments,
            requests: action_arguments.context.requests,
            context: action_arguments.context.features[feature],
            event: action_arguments.event.source ?? action_arguments.event,
        } as FeatureActionArguments;
    }

    /**
     * 함수 이름: guard()
     * 기능: 기능의 가드 이름 또는 함수를 루트에서 실행할 가드로 연결한다.
     * 인자: feature -> 대상 기능, original -> 가드 이름·함수·XState 가드 정의
     * 반환값: 루트 context에 연결한 가드 또는 undefined
     * 작성 날짜: 2026/09/16
     */
    guard(feature: FeatureKey, original: any): any {
        if (!original) {
            return undefined;
        }

        // XState의 stateIn·and·or 가드는 이미 루트 상태 ID를 참조한다.
        if (typeof original === 'object' || original.check) {
            return original;
        }

        const guard = typeof original === 'string' ? this.definitions[feature].implementations.guards[original] : original;

        if (!guard) {
            throw new Error(`Unknown ${feature} guard: ${original}`);
        }

        return (action_arguments: RootActionArguments) => guard(this.scope_args(feature, action_arguments), undefined);
    }

    /**
     * 함수 이름: actions()
     * 기능: 기능 Action들을 루트의 context 갱신과 실행 인자에 연결한다.
     * 인자: feature -> 대상 기능, original -> 단일 Action 또는 Action 목록
     * 반환값: 루트에서 순서대로 실행할 Action 목록
     * 작성 날짜: 2026/09/16
     */
    actions(feature: FeatureKey, original: any): any[] {
        return as_array(original).map(action => {
            const implementation = typeof action === 'string' ? this.definitions[feature].implementations.actions[action] : action;

            if (!implementation) {
                throw new Error(`Unknown ${feature} action: ${action}`);
            }

            // 기능의 assign 정의를 루트 context 갱신으로 연결한다.
            if (implementation.type === 'xstate.assign') {
                return assign((action_arguments: RootActionArguments) => {
                    const feature_arguments = this.scope_args(feature, action_arguments);
                    const assignment = implementation.assignment;
                    const patch = typeof assignment === 'function'
                        ? assignment(feature_arguments, undefined)
                        : Object.fromEntries(
                            Object.entries(assignment).map(([key, value]) => [
                                key,
                                typeof value === 'function'
                                    ? value(feature_arguments, undefined)
                                    : value,
                            ]),
                        );

                    return {
                        features: {
                            ...action_arguments.context.features,
                            [feature]: {
                                ...action_arguments.context.features[feature],
                                ...patch,
                            },
                        },
                    };
                });
            }

            if (feature === 'trade_history' && action === 'publish_summary') {
                return assign(({ context, event }: RootActionArguments) => {
                    const output = event.source?.output as any;

                    if (!output?.publish_summary || output.request_summary_revision !== context.summary_revision) {
                        return {};
                    }

                    return {
                        features: {
                            ...context.features,
                            trade_history_summary: {
                                ...context.features.trade_history_summary,
                                summary: output.summary,
                            },
                        },
                    };
                });
            }

            if (implementation.type?.startsWith('xstate.')) {
                return implementation;
            }

            return (action_arguments: RootActionArguments) => implementation(this.scope_args(feature, action_arguments), undefined);
        });
    }

    /**
     * 함수 이름: target()
     * 기능: 상대 전이 경로를 루트에서 참조할 절대 상태 ID로 변환한다.
     * 인자: path -> 현재 상태 ID, target -> 원래 전이 대상 경로
     * 반환값: #으로 시작하는 절대 전이 대상
     * 작성 날짜: 2026/09/16
     */
    private target(path: string, target: string): string {
        if (target.startsWith('#')) {
            return target;
        }

        return `#${target.startsWith('.') ? path + target : path.slice(0, path.lastIndexOf('.')) + '.' + target}`;
    }

    /**
     * 함수 이름: transitions()
     * 기능: 전이의 target·guard·actions를 기능별 루트 경계에 연결한다.
     * 인자: feature -> 대상 기능, path -> 현재 상태 ID, original -> 원래 전이 정의
     * 반환값: 루트에서 사용할 전이 목록
     * 작성 날짜: 2026/09/16
     */
    transitions(feature: FeatureKey, path: string, original: any): any[] {
        return as_array(original).map(item => {
            const transition = typeof item === 'string' ? {
                target: item,
            } : item;

            return {
                ...transition,
                ...(transition.target === undefined ? {} : {
                    target: as_array<string>(transition.target).map(target => (
                        feature === 'app_exit' && target.replace(/^\./, '') === 'ui_final_state'
                            ? '#UI_FINAL_STATE'
                            : this.target(path, target)
                    )),
                }),
                ...(transition.guard === undefined ? {} : {
                    guard: this.guard(feature, transition.guard),
                }),
                actions: this.actions(feature, transition.actions),
            };
        });
    }

    /**
     * 함수 이름: remember_fallback()
     * 기능: 비활성 화면의 통지·작업 결과 처리와 복귀 target 이벤트를 등록한다.
     * 인자: event -> 내부 이벤트 이름, transitions -> 결과 전이, feature -> 대상 기능, handlers -> 복귀 handler를 추가할 객체
     * 반환값: 없음
     * 작성 날짜: 2026/09/16
     */
    private remember_fallback(
        event: string,
        transitions: Transition[],
        feature: FeatureKey,
        handlers: Record<string, any>,
    ): void {
        // 상단 표시와 종료 영역은 ETIRE_UI_SYSTEM 안에서 계속 활성 상태다.
        // 해당 영역이 처리하지 않은 이벤트는 비활성 화면의 결과로 저장하지 않는다.
        if (['connection', 'trading', 'app_exit'].includes(feature)) {
            return;
        }

        if (this.registered.has(event)) {
            // 매도 체결 통지는 독립된 두 요약 Region을 함께 갱신한다.
            if (event === 'trade_history_summary.SELL_ORDER_EXECUTED') {
                this.fallback[event]![0].actions.push(...transitions.flatMap(transition => as_array(transition.actions)));
            }

            return;
        }

        this.registered.add(event);
        this.fallback[event] = transitions.map((transition, index) => {
            const resume = `${event}.resume.${index}`;

            // 비활성 화면의 데이터 갱신과 Action은 즉시 처리한다.
            // 화면 복귀 시에는 저장한 상태 전이만 수행하여 결과를 중복 반영하지 않는다.
            handlers[resume] = {
                target: transition.target,
            };

            return {
                ...transition,
                target: undefined,
                reenter: undefined,
                guard: and([not(stateIn(`#${feature}`)), ...(transition.guard ? [transition.guard] : [])]),
                actions: [
                    ...(
                        transition.target !== undefined
                        && !event.startsWith('command.')
                        && event !== 'regime.HIGHLIGHT_REQUESTED'
                            ? [this.invalidate_feature_requests(feature)]
                            : []
                    ),
                    ...as_array(transition.actions),
                    ...(transition.target === undefined || feature === 'trade_history'
                        ? []
                        : [
                            assign(({ context, event }: RootActionArguments) => ({
                                deferred: [
                                    ...context.deferred.filter(item => item.resume_key !== event.type),
                                    {
                                        ...event,
                                        type: resume,
                                        owner: feature,
                                        resume_key: event.type,
                                    },
                                ],
                            })),
                        ]),
                ],
            };
        });
    }

    /**
     * 함수 이름: invalidate_feature_requests()
     * 기능: 지정 기능의 진행 요청과 복귀 대기 이벤트를 무효화하는 Action을 만든다.
     * 인자: feature -> 요청을 취소할 기능
     * 반환값: 요청 삭제·명령 취소·지연 이벤트 정리를 수행할 Action
     * 작성 날짜: 2026/09/16
     */
    private invalidate_feature_requests(feature: FeatureKey) {
        return enqueueActions(({ context, enqueue }: RootActionArguments & {
            enqueue: any;
        }) => {
            const requests = {
                ...context.requests,
            };

            for (const key of Object.keys(requests).filter(key => key.startsWith(`${feature}.`))) {
                delete requests[key];
                enqueue.sendTo('ui_commands', {
                    type: 'cancel',
                    key,
                });
            }

            enqueue.assign({
                requests,
                deferred: context.deferred.filter(item => item.owner !== feature),
            });
        });
    }

    /**
     * 함수 이름: command()
     * 기능: 작업 시작·취소와 token을 검사하는 완료·실패 handler를 구성한다.
     * 인자: feature -> 명령 소유 기능, path -> 작업 상태 ID, invocation -> invoke 정의
     * 반환값: 시작·취소 Action, 결과 handler와 작업 key
     * 작성 날짜: 2026/09/16
     */
    command(feature: FeatureKey, path: string, invocation: any) {
        const key = `${feature}.${invocation.id ?? path}`;
        const logic = this.definitions[feature].implementations.actors[invocation.src];

        if (!logic) {
            throw new Error(`Unknown command ${feature}.${invocation.src}`);
        }

        const start = enqueueActions(({ context, event, enqueue }: RootActionArguments & {
            enqueue: any;
        }) => {
            // deep history로 복귀해도 이미 시작한 작업은 다시 제출하지 않는다.
            if (context.requests[key]) {
                return;
            }

            const token = context.request_sequence + 1;
            const feature_arguments = this.scope_args(feature, {
                context,
                event,
            } as RootActionArguments);
            let input = typeof invocation.input === 'function' ? invocation.input(feature_arguments) : invocation.input;

            if (feature === 'trade_history' && invocation.src === 'load_trade_history') {
                input = {
                    ...input,
                    summary_revision: context.summary_revision,
                };
            }

            enqueue.assign({
                request_sequence: token,
                requests: {
                    ...context.requests,
                    [key]: {
                        token,
                        status: 'pending',
                    },
                },
            });
            enqueue.sendTo('ui_commands', {
                type: 'run',
                key,
                token,
                logic,
                input,
            });
        });
        const cancel = enqueueActions(({ context, event, enqueue }: RootActionArguments & {
            enqueue: any;
        }) => {
            if (is_screen_navigation(event) && feature !== 'trade_history') {
                return;
            }

            const requests = {
                ...context.requests,
            };

            delete requests[key];
            enqueue.assign({
                requests,
            });
            enqueue.sendTo('ui_commands', {
                type: 'cancel',
                key,
            });
        });
        const on: Record<string, any> = {};

        for (const [kind, original] of [['done', invocation.onDone], ['error', invocation.onError]] as const) {
            const name = `command.${key}.${kind}`;
            const transitions = this.transitions(feature, path, original).map(transition => {
                const guard = transition.guard;

                return {
                    ...transition,
                    guard: (action_arguments: RootActionArguments) => (
                        action_arguments.context.requests[key]?.token === action_arguments.event.token
                        && (!guard || guard(action_arguments))
                    ),
                };
            });

            on[name] = transitions;
            this.remember_fallback(name, transitions.map(transition => ({
                ...transition,
                actions: [
                    ...as_array(transition.actions),
                    assign(({ context, event }: RootActionArguments) => ({
                        requests: {
                            ...context.requests,
                            [key]: {
                                token: event.token!,
                                status: kind,
                            },
                        },
                    })),
                ],
            })), feature, on);
        }

        return {
            start,
            cancel,
            on,
            key,
        };
    }

    /**
     * 함수 이름: compile()
     * 기능: 기능 상태 정의를 루트의 실제 상태 노드로 재귀 조립한다.
     * 인자: feature -> 대상 기능, definition -> 원래 상태 정의, path -> 조립할 상태 ID
     * 반환값: 루트 이벤트·Action·명령에 연결된 상태 노드
     * 작성 날짜: 2026/09/16
     */
    compile(feature: FeatureKey, definition: RegionNode, path = feature as string): any {
        const {
            context: _context,
            invoke,
            after,
            id: _id,
            ...node
        } = definition as any;

        if (!(feature in this.initial)) {
            this.initial[feature] = this.definitions[feature].config.context;
        }

        const result: any = {
            ...node,
            id: path,
            entry: this.actions(feature, node.entry),
            exit: this.actions(feature, node.exit),
        };

        if (node.on) {
            result.on = {};

            for (const [name, transition] of Object.entries(node.on)) {
                const event = name.startsWith('command.') || name.startsWith('shell.') ? name : `${feature}.${name}`;
                const transitions = this.transitions(feature, path, transition);

                result.on[event] = transitions;

                // 비활성 화면에서는 부모의 handler를 먼저 등록하고 하위 표시 갱신을 연결한다.
                if (/SYNCHRONIZED|UPDATED|RECOMMENDED|REGIME_APPLIED|ORDER_EXECUTED|INVALIDATE|TRADING_LOGIC_STATE_CHANGED|DAILY_TRADING_FEE_CHANGED|HIGHLIGHT_REQUESTED/.test(name)) {
                    this.remember_fallback(event, transitions, feature, result.on);
                }
            }
        }

        if (node.always) {
            result.always = this.transitions(feature, path, node.always);
        }

        if (node.target) {
            result.target = this.target(path, node.target);
        }

        if (node.states) {
            result.states = Object.fromEntries(
                Object.entries(node.states).map(([name, child]) => [
                    name,
                    this.compile(feature, child as RegionNode, `${path}.${name}`),
                ]),
            );
        }

        for (const invocation of as_array<any>(invoke)) {
            const command = this.command(feature, path, invocation);

            this.command_starts.set(`#${path}`, [...(this.command_starts.get(`#${path}`) ?? []), command.start]);
            result.exit.push(command.cancel);
            result.on = {
                ...result.on,
                ...command.on,
            };
        }

        for (const [delay, transition] of Object.entries(after ?? {})) {
            const key = `timer_${path}_${delay}`;
            const delay_definition = this.definitions[feature].implementations.delays?.[delay];
            const logic = fromPromise(async ({ input, signal }: {
                input: number;
                signal: AbortSignal;
            }) => {
                await new Promise<void>((resolve, reject) => {
                    const timer = setTimeout(resolve, input);

                    signal.addEventListener('abort', () => {
                        clearTimeout(timer);
                        reject(new Error('Timer canceled'));
                    }, {
                        once: true,
                    });
                });
            });

            this.definitions[feature].implementations.actors[key] = logic;

            const command = this.command(feature, path, {
                id: key,
                src: key,
                input: (action_arguments: any) => (
                    typeof delay_definition === 'function'
                        ? delay_definition(action_arguments, undefined)
                        : delay_definition ?? Number(delay)
                ),
                onDone: transition,
            });

            result.entry.push(command.start);
            result.exit.push(command.cancel);
            result.on = {
                ...result.on,
                ...command.on,
            };

            if (feature === 'regime' && path.endsWith('.highlighting')) {
                const hidden_highlight_transition = this.fallback['regime.HIGHLIGHT_REQUESTED']![0];

                hidden_highlight_transition.guard = and([
                    not(stateIn('#regime')),
                    ({ context }: RootActionArguments) => !context.features.regime.is_highlighted,
                ]);
                hidden_highlight_transition.actions.push(command.cancel, command.start);
            }
        }

        return result;
    }

    /**
     * 함수 이름: connect_commands()
     * 기능: 작업 상태로 향하는 명시적 전이에 명령 시작 Action을 연결한다.
     * 인자: node -> 명령 시작을 연결할 상태 노드
     * 반환값: 명령 시작 Action이 연결된 동일 상태 노드
     * 작성 날짜: 2026/09/16
     */
    connect_commands(node: any): any {
        for (const [event, original] of Object.entries(node.on ?? {})) {
            if (event.includes('.resume.')) {
                continue;
            }

            for (const transition of as_array<any>(original)) {
                if (typeof transition !== 'object') {
                    continue;
                }

                for (const target of as_array<string>(transition.target)) {
                    transition.actions = [...as_array(transition.actions), ...(this.command_starts.get(target) ?? [])];
                }
            }
        }

        for (const child of Object.values(node.states ?? {})) {
            this.connect_commands(child);
        }

        return node;
    }
}
