import { initialTransition, transition, type AnyStateMachine, type AnyMachineSnapshot } from 'xstate';
import type { UIActionRequest, UIEvaluationInput, UITransitionResult } from './uiActions';
import type { UiApplicationSnapshot, UiDomainEvent } from './uiApplicationTypes';

/** 상태와 실행 요청만 계산한다. 실행 중인 actor나 외부 자원을 소유하지 않는다. */
export class UISTM {
    private snapshot: AnyMachineSnapshot;
    private readonly initial_actions: readonly UIActionRequest[];
    private is_started = false;

    constructor(private readonly machine: AnyStateMachine) {
        const [snapshot, actions] = initialTransition(machine);
        this.snapshot = snapshot;
        this.initial_actions = this.read_actions(actions);
    }

    get_snapshot(): UiApplicationSnapshot { return this.snapshot as UiApplicationSnapshot; }

    /** 실제 선택된 reset Action을 순수하게 미리 계산한다. Controller가 가드를 복제하지 않는다. */
    requires_current_date(event: UiDomainEvent): boolean {
        const includes_open = (item: UiDomainEvent): boolean => item.type === 'csv_export.CSV_EXPORT_CLICKED'
            || (item.events?.some(includes_open) ?? false);
        if (this.snapshot.status !== 'active' || !includes_open(event)) return false;
        const [, actions] = transition(this.machine, this.snapshot, {
            type: 'ui.evaluate', events: [event],
            evaluation: { ...this.get_snapshot().context.evaluation, previous_value: this.snapshot.value },
        });
        return actions.some((action: { type: string }) => action.type === 'ui.read_current_date');
    }

    run(input: UIEvaluationInput): UITransitionResult {
        if (this.is_started || this.snapshot.status !== 'active') return this.result([]);
        this.is_started = true;
        const result = this.handle({ type: 'ui.batch', events: [] }, input);
        return this.result([...this.initial_actions, ...result.actions]);
    }

    handle(event: UiDomainEvent, input: UIEvaluationInput): UITransitionResult {
        if (this.snapshot.status !== 'active') return this.result([]);
        const [snapshot, actions] = transition(this.machine, this.snapshot, {
            type: 'ui.evaluate',
            evaluation: { ...input, previous_value: this.snapshot.value },
            events: [event],
        });
        this.snapshot = snapshot;
        return this.result(this.read_actions(actions));
    }

    /** 화면 런타임 폐기는 업무상의 정상 종료(final)와 구분한다. */
    stop(): UITransitionResult {
        if (this.snapshot.status === 'active') this.snapshot = { ...this.snapshot, status: 'stopped' };
        return this.result([{ type: 'stop_all' }]);
    }

    private result(actions: readonly UIActionRequest[]): UITransitionResult {
        return { snapshot: this.get_snapshot(), actions };
    }

    private read_actions(actions: readonly { type: string; params?: unknown }[]): UIActionRequest[] {
        return actions.flatMap(action => {
            if (action.type === 'ui.read_current_date') return [];
            // 즉시 raise는 XState가 같은 전이 계산 안에서 이미 처리했다.
            // 지연 raise는 실행 자원이 필요하므로 허용하지 않는다.
            if (action.type === 'xstate.raise' && (action.params as { delay?: number }).delay === undefined) return [];
            if (action.type !== 'ui.request') throw new Error(`Unexpected executable UI action: ${action.type}`);
            return [action.params as UIActionRequest];
        });
    }
}
