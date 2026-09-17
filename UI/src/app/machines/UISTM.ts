import { initialTransition, transition, type AnyStateMachine, type AnyMachineSnapshot } from 'xstate';
import type { UIActionRequest, UIEvaluationInput, UITransitionResult } from './uiActions';
import type { UiApplicationSnapshot, UiDomainEvent } from './uiApplicationTypes';


/**
 * 클래스 이름: UISTM
 * 기능: 하나의 UI 루트 snapshot을 보관하며 외부 작업 없이 상태 전이와 실행 요청만 계산한다.
 * 작성 날짜: 2026/09/17
 */
export class UISTM {
    private snapshot: AnyMachineSnapshot;
    // 초기 평가에서 나온 요청은 생성 시 실행하지 않고 run()이 최초 한 번 반환한다.
    private readonly initial_actions: readonly UIActionRequest[];
    private is_started = false;

    /**
     * 함수 이름: UISTM.constructor()
     * 기능: 순수 초기 전이로 snapshot과 시작 시 반환할 요청 목록을 준비한다.
     * 인자: machine -> 기능별 정의가 조립된 XState 루트
     * 반환값: 생성된 UISTM 인스턴스
     * 작성 날짜: 2026/09/17
     */
    constructor(private readonly machine: AnyStateMachine) {
        // 상태와 요청만 계산하며 actor·Port·타이머를 생성하지 않는다.
        const [snapshot, actions] = initialTransition(machine);
        this.snapshot = snapshot;
        this.initial_actions = this.read_actions(actions);
    }

    /**
     * 함수 이름: get_snapshot()
     * 기능: 현재까지 평가한 UI 루트 snapshot을 읽는다.
     * 인자: 없음
     * 반환값: 최신 UI 루트 snapshot
     * 작성 날짜: 2026/09/17
     */
    get_snapshot(): UiApplicationSnapshot {
        return this.snapshot as UiApplicationSnapshot;
    }

    /**
     * 함수 이름: requires_current_date()
     * 기능: CSV 열기 전이를 사전 평가해 실제 draft 초기화에 새 날짜가 필요한지 판단한다.
     * 인자: event -> 평가할 내부 이벤트 또는 이벤트 묶음
     * 반환값: 날짜 읽기 표식이 선택되면 true; 현재 snapshot은 변경하지 않음
     * 작성 날짜: 2026/09/17
     */
    requires_current_date(event: UiDomainEvent): boolean {
        /**
         * 함수 이름: includes_open()
         * 기능: 중첩 이벤트 묶음에 CSV 창 열기 입력이 포함되어 있는지 검사한다.
         * 인자: item -> 검사할 이벤트 또는 이벤트 묶음
         * 반환값: CSV 열기 이벤트가 포함되면 true
         * 작성 날짜: 2026/09/17
         */
        const includes_open = (item: UiDomainEvent): boolean => item.type === 'csv_export.CSV_EXPORT_CLICKED'
            || (item.events?.some(includes_open) ?? false);
        if (this.snapshot.status !== 'active' || !includes_open(event)) return false;

        // 사전 평가 결과는 저장하지 않아 날짜 확보 전의 draft가 실제 상태에 노출되지 않는다.
        const [, actions] = transition(this.machine, this.snapshot, {
            type: 'ui.evaluate', events: [event],
            evaluation: { ...this.get_snapshot().context.evaluation, previous_value: this.snapshot.value },
        });

        return actions.some((action: { type: string }) => action.type === 'ui.read_current_date');
    }

    /**
     * 함수 이름: run()
     * 기능: 평가 시간을 반영하고 보관된 초기 요청을 최초 시작에 한 번만 반환한다.
     * 인자: input -> Controller가 확보한 현재 시각·단조 시각·날짜
     * 반환값: 최종 snapshot과 순서 있는 초기 Action 요청
     * 작성 날짜: 2026/09/17
     */
    run(input: UIEvaluationInput): UITransitionResult {
        if (this.is_started || this.snapshot.status !== 'active') return this.result([]);
        this.is_started = true;

        // 빈 입력 묶음으로 평가 시간을 반영한 뒤 초기 요청의 원래 순서를 유지한다.
        const result = this.handle({ type: 'ui.batch', events: [] }, input);

        return this.result([...this.initial_actions, ...result.actions]);
    }

    /**
     * 함수 이름: handle()
     * 기능: 이전 상태와 명시적 시간 입력으로 내부 전이를 완료하고 새 snapshot과 요청을 반환한다.
     * 인자: event -> 처리할 내부 이벤트, input -> 외부에서 확보한 시간·날짜
     * 반환값: 전이 완료 snapshot과 실행 순서가 있는 Action 요청
     * 작성 날짜: 2026/09/17
     */
    handle(event: UiDomainEvent, input: UIEvaluationInput): UITransitionResult {
        if (this.snapshot.status !== 'active') return this.result([]);

        // 화면 exit Action이 실제 이전 상태를 보관하도록 직전 value를 명시적으로 전달한다.
        const [snapshot, actions] = transition(this.machine, this.snapshot, {
            type: 'ui.evaluate',
            evaluation: { ...input, previous_value: this.snapshot.value },
            events: [event],
        });

        // 외부 실행 전에 완성된 상태를 확정해 Controller가 중간 상태를 발행하지 않게 한다.
        this.snapshot = snapshot;

        return this.result(this.read_actions(actions));
    }

    /**
     * 함수 이름: stop()
     * 기능: 정상 종료 상태를 보존하면서 화면 런타임을 폐기하고 전체 실행 정리를 요청한다.
     * 인자: 없음
     * 반환값: stopped 또는 기존 done snapshot과 stop_all 요청
     * 작성 날짜: 2026/09/17
     */
    stop(): UITransitionResult {
        // 업무상 final(done)과 화면 런타임 폐기(stopped)를 구분한다.
        if (this.snapshot.status === 'active') this.snapshot = { ...this.snapshot, status: 'stopped' };

        return this.result([{ type: 'stop_all' }]);
    }

    /**
     * 함수 이름: result()
     * 기능: 현재 snapshot과 요청 목록을 동일한 전이 결과 계약으로 묶는다.
     * 인자: actions -> 실행 순서대로 정렬된 요청 목록
     * 반환값: UITransitionResult
     * 작성 날짜: 2026/09/17
     */
    private result(actions: readonly UIActionRequest[]): UITransitionResult {
        return { snapshot: this.get_snapshot(), actions };
    }

    /**
     * 함수 이름: read_actions()
     * 기능: 순수 평가에서 처리한 내부 Action을 제외하고 데이터 요청만 추출한다.
     * 인자: actions -> XState 평가가 반환한 Action 목록
     * 반환값: UIActionRequest 배열; 알 수 없는 실행 Action이면 예외
     * 작성 날짜: 2026/09/17
     */
    private read_actions(actions: readonly { type: string; params?: unknown }[]): UIActionRequest[] {
        return actions.flatMap(action => {
            // 날짜 확인용 표식은 Controller가 실행할 명령이 아니다.
            if (action.type === 'ui.read_current_date') return [];

            // 즉시 raise는 XState가 같은 전이 계산 안에서 이미 처리했다.
            // 지연 raise는 실행 자원이 필요하므로 허용하지 않는다.
            if (action.type === 'xstate.raise' && (action.params as { delay?: number }).delay === undefined) return [];
            if (action.type !== 'ui.request') throw new Error(`Unexpected executable UI action: ${action.type}`);

            return [action.params as UIActionRequest];
        });
    }
}
