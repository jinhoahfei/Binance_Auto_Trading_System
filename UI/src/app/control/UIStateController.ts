import type { UiCommandPort } from '../../shared/ports';
import { UISTM } from '../machines/UISTM';
import { create_ui_application_machine } from '../machines/uiApplicationMachine';
import type { UiApplicationSnapshot, UiDomainEvent } from '../machines/uiApplicationTypes';
import type { UIEvaluationInput, UITransitionResult } from '../machines/uiActions';
import type { AppViewModel, UiApplicationFacadeOptions, UiApplicationIntent } from './uiApplicationContracts';
import { UiIntentRouter } from './uiApplicationIntents';
import { UiCommandExecutor } from './UiCommandExecutor';
import { select_app_view_model } from './selectAppViewModel';

/** STM을 호출하고 결정된 작업을 실행한다. 모든 입력과 결과는 같은 직렬 큐를 통과한다. */
export class UIStateController {
    private readonly stm: UISTM;
    private readonly executor: UiCommandExecutor;
    private readonly listeners = new Set<(snapshot: UiApplicationSnapshot) => void>();
    private readonly events: UiDomainEvent[] = [];
    private is_started = false;
    private is_processing = false;

    constructor(command_port: UiCommandPort, private readonly options: UiApplicationFacadeOptions, stm?: UISTM) {
        const { get_current_kst_date: _date_source, ...initial_options } = options;
        this.stm = stm ?? new UISTM(create_ui_application_machine({ ...initial_options, initial_monotonic_ms: performance.now() }));
        this.executor = new UiCommandExecutor(command_port, event => this.handle_event(event));
    }

    start(): void {
        if (this.is_started || this.get_snapshot().status !== 'active') return;
        this.is_started = true;
        this.is_processing = true;
        try { this.apply(this.stm.run(this.read_time())); }
        finally { this.is_processing = false; }
        this.drain();
    }

    stop(): void {
        if (!this.is_started) return;
        this.is_started = false;
        this.events.length = 0;
        this.executor.execute(this.stm.stop().actions);
        this.listeners.clear();
    }

    dispatch(intent: UiApplicationIntent): boolean {
        const router = new UiIntentRouter(this.get_snapshot());
        const accepted = router.dispatch(intent);
        if (accepted && router.events.length) this.handle_event({ type: 'ui.batch', events: router.events,
            ...(intent.type === 'BACKEND_SNAPSHOT_SYNCHRONIZED' ? { server_snapshot: intent.snapshot } : {}) });
        return accepted;
    }

    /** 내부 이벤트 진입점. 실행 결과도 사용자 입력과 같은 실행 수명을 따른다. */
    handle_event(event: UiDomainEvent): void {
        if (this.get_snapshot().status !== 'active') return;
        this.events.push(event);
        this.drain();
    }

    subscribe(listener: (snapshot: UiApplicationSnapshot) => void): () => void {
        this.listeners.add(listener);
        listener(this.get_snapshot());
        return () => { this.listeners.delete(listener); };
    }

    get_snapshot(): UiApplicationSnapshot { return this.stm.get_snapshot(); }
    get_view_model(): AppViewModel { return select_app_view_model(this.get_snapshot()); }

    private read_time(event?: UiDomainEvent): UIEvaluationInput {
        return { now_epoch_ms: Date.now(), monotonic_ms: performance.now(),
            today: event && this.stm.requires_current_date(event) ? this.options.get_current_kst_date?.() ?? this.options.today : this.options.today };
    }

    private drain(): void {
        if (!this.is_started || this.is_processing) return;
        this.is_processing = true;
        try {
            while (this.is_started && this.events.length && this.get_snapshot().status === 'active') {
                const event = this.events.shift()!;
                this.apply(this.stm.handle(event, this.read_time(event)));
            }
            if (this.get_snapshot().status !== 'active') this.events.length = 0;
        } finally { this.is_processing = false; }
    }

    private apply(result: UITransitionResult): void {
        this.executor.execute(result.actions);
        if (!this.is_started) return;
        this.listeners.forEach(listener => {
            try { listener(result.snapshot); }
            catch (error) { queueMicrotask(() => { throw error; }); }
        });
    }
}
