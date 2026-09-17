import type { UiCommandPort } from '../../shared/ports';
import type { UIActionRequest, UIRunCommand } from '../machines/uiActions';
import type { UiDomainEvent } from '../machines/uiApplicationTypes';

interface PendingWork {
    readonly token: number;
    readonly abort: AbortController;
    timer?: ReturnType<typeof setTimeout>;
}

/** Controller 소유 실행부. 전이 규칙 없이 요청한 I/O와 타이머만 실행한다. */
export class UiCommandExecutor {
    private readonly pending = new Map<string, PendingWork>();
    private generation = 0;

    constructor(private readonly port: UiCommandPort, private readonly deliver: (event: UiDomainEvent) => void) {}

    execute(actions: readonly UIActionRequest[]): void {
        const generation = this.generation;
        for (const action of actions) {
            if (generation !== this.generation) return;
            switch (action.type) {
                case 'stop_all':
                    this.generation += 1;
                    for (const key of this.pending.keys()) this.cancel(key);
                    break;
                case 'cancel_command':
                case 'cancel_timer':
                    this.cancel(action.key);
                    break;
                case 'run_command':
                case 'start_timer': {
                    this.cancel(action.key);
                    const work: PendingWork = { token: action.token, abort: new AbortController() };
                    this.pending.set(action.key, work);
                    if (action.type === 'start_timer') {
                        work.timer = setTimeout(() => this.finish(action.key, work, 'done', undefined),
                            Math.max(0, action.due_at_ms - Date.now()));
                    } else {
                        // 동기 throw도 기존 Promise actor와 같은 비동기 실패 이벤트로 바꾼다.
                        void this.run(action, work.abort.signal).then(
                            output => this.finish(action.key, work, 'done', output),
                            error => this.finish(action.key, work, 'error', error),
                        );
                    }
                    break;
                }
            }
        }
    }

    private cancel(key: string): void {
        const work = this.pending.get(key);
        if (!work) return;
        this.pending.delete(key);
        if (work.timer !== undefined) clearTimeout(work.timer);
        // 읽기만 실제 AbortSignal을 사용한다. 제출된 쓰기를 취소했다고 간주하지 않는다.
        work.abort.abort();
    }

    private finish(key: string, work: PendingWork, kind: 'done' | 'error', value: unknown): void {
        if (this.pending.get(key) !== work) return;
        this.pending.delete(key);
        this.deliver({ type: `command.${key}.${kind}`, token: work.token,
            source: kind === 'done' ? { type: 'command.done', output: value } : { type: 'command.error', error: value } });
    }

    private async run(request: UIRunCommand, signal: AbortSignal): Promise<unknown> {
        switch (request.operation) {
            case 'start_trading': return this.port.start_trading(request.input);
            case 'stop_trading': return this.port.stop_trading();
            case 'force_sell_and_stop': return this.port.force_sell_and_stop();
            case 'liquidate_recovered_position': return this.port.liquidate_recovered_position();
            case 'apply_regime': return this.port.apply_regime(request.input);
            case 'update_split_order': return this.port.update_split_order(request.input.order_side, request.input.percentage);
            case 'pick_directory': return this.port.pick_csv_directory();
            case 'export_csv': return this.port.export_csv(request.input);
            case 'shutdown_application': return this.port.shutdown_application(request.input);
            case 'load_trade_history': {
                const details = await this.port.load_trade_history(request.input.query, signal);
                return { ...details, request_summary_revision: request.input.summary_revision, publish_summary: request.input.publish_summary };
            }
        }
    }
}
