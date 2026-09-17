import type { UiCommandPort } from '../../shared/ports';
import type { UIActionRequest, UIRunCommand } from '../machines/uiActions';
import type { UiDomainEvent } from '../machines/uiApplicationTypes';

interface PendingWork {
    readonly token: number;
    readonly abort: AbortController;
    timer?: ReturnType<typeof setTimeout>;
}


/**
 * 클래스 이름: UiCommandExecutor
 * 기능: Controller가 소유한 Port 호출·Promise·취소 신호·타이머를 관리하며 전이 조건은 판단하지 않는다.
 * 작성 날짜: 2026/09/17
 */
export class UiCommandExecutor {
    private readonly pending = new Map<string, PendingWork>();
    // Port 호출 중 stop_all이 재진입하면 이전 Action 묶음의 남은 실행을 차단한다.
    private generation = 0;

    /**
     * 함수 이름: UiCommandExecutor.constructor()
     * 기능: 실제 외부 명령 계약과 완료 이벤트 전달 경계를 보관한다.
     * 인자: port -> 실행할 UiCommandPort, deliver -> Controller의 이벤트 진입 callback
     * 반환값: 생성된 UiCommandExecutor 인스턴스
     * 작성 날짜: 2026/09/17
     */
    constructor(private readonly port: UiCommandPort, private readonly deliver: (event: UiDomainEvent) => void) {}

    /**
     * 함수 이름: execute()
     * 기능: 요청 순서대로 작업을 시작·교체·취소하고 재진입 종료 이후의 추가 실행을 막는다.
     * 인자: actions -> STM이 반환한 순서 있는 작업 요청
     * 반환값: 없음; 완료·실패는 후속 이벤트로 전달
     * 작성 날짜: 2026/09/17
     */
    execute(actions: readonly UIActionRequest[]): void {
        const generation = this.generation;
        for (const action of actions) {
            if (generation !== this.generation) return;
            switch (action.type) {
                // 전체 정리는 실행 세대를 바꾸므로 현재 반복도 다음 요청 전에 멈춘다.
                case 'stop_all':
                    this.generation += 1;
                    for (const key of this.pending.keys()) this.cancel(key);
                    break;
                case 'cancel_command':
                case 'cancel_timer':
                    this.cancel(action.key);
                    break;
                // 같은 key를 교체해도 이전 Promise 자체의 서버 쓰기를 취소했다고 보지 않는다.
                case 'run_command':
                case 'start_timer': {
                    this.cancel(action.key);
                    const work: PendingWork = { token: action.token, abort: new AbortController() };
                    this.pending.set(action.key, work);

                    // 화면 복귀 여부와 관계없이 STM이 지정한 원래 절대 만료 시각을 따른다.
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

    /**
     * 함수 이름: cancel()
     * 기능: 해당 key의 관리 항목과 타이머를 지우고 읽기 취소 신호를 발생시킨다.
     * 인자: key -> 취소할 작업 식별자
     * 반환값: 없음; 이미 제출한 쓰기를 되돌리지 않음
     * 작성 날짜: 2026/09/17
     */
    private cancel(key: string): void {
        const work = this.pending.get(key);
        if (!work) return;
        this.pending.delete(key);
        if (work.timer !== undefined) clearTimeout(work.timer);

        // 읽기만 실제 AbortSignal을 사용한다. 제출된 쓰기를 취소했다고 간주하지 않는다.
        work.abort.abort();
    }

    /**
     * 함수 이름: finish()
     * 기능: 여전히 같은 작업일 때만 관리 항목을 제거하고 token을 가진 결과 이벤트를 전달한다.
     * 인자: key -> 작업 식별자, work -> 제출 당시 작업, kind -> 성공/실패, value -> 결과 또는 오류
     * 반환값: 없음; 교체·취소된 작업의 늦은 callback은 무시
     * 작성 날짜: 2026/09/17
     */
    private finish(key: string, work: PendingWork, kind: 'done' | 'error', value: unknown): void {
        // token 판정은 STM의 책임이며 여기서는 실행 수명이 끝난 callback만 거른다.
        if (this.pending.get(key) !== work) return;
        this.pending.delete(key);
        this.deliver({ type: `command.${key}.${kind}`, token: work.token,
            source: kind === 'done' ? { type: 'command.done', output: value } : { type: 'command.error', error: value } });
    }

    /**
     * 함수 이름: run()
     * 기능: 명령 종류를 Port 메서드에 연결하고 상세 조회의 revision 정보를 결과에 보존한다.
     * 인자: request -> 명령 종류와 입력 데이터, signal -> 실제 읽기에 전달할 취소 신호
     * 반환값: 명령 결과 Promise; 동기 예외도 거부된 Promise로 전달
     * 작성 날짜: 2026/09/17
     */
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
            // 실제 취소 가능한 읽기에만 signal을 전달하고 제출 당시 revision을 결과에 붙인다.
            case 'load_trade_history': {
                const details = await this.port.load_trade_history(request.input.query, signal);

                return { ...details, request_summary_revision: request.input.summary_revision, publish_summary: request.input.publish_summary };
            }
        }
    }
}
