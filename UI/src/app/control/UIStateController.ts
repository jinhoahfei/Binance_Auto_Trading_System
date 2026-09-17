import type { UiCommandPort } from '../../shared/ports';
import { UISTM } from '../machines/UISTM';
import { create_ui_application_machine } from '../machines/uiApplicationMachine';
import type { UiApplicationSnapshot, UiDomainEvent } from '../machines/uiApplicationTypes';
import type { UIEvaluationInput, UITransitionResult } from '../machines/uiActions';
import type { AppViewModel, UiApplicationFacadeOptions, UiApplicationIntent } from './uiApplicationContracts';
import { UiIntentRouter } from './uiApplicationIntents';
import { UiCommandExecutor } from './UiCommandExecutor';
import { select_app_view_model } from './selectAppViewModel';


/**
 * 클래스 이름: UIStateController
 * 기능: 입력과 완료 이벤트를 직렬화하고 순수 STM의 요청 실행·구독·화면 런타임 수명을 조정한다.
 * 작성 날짜: 2026/09/17
 */
export class UIStateController {
    private readonly stm: UISTM;
    private readonly executor: UiCommandExecutor;
    private readonly listeners = new Set<(snapshot: UiApplicationSnapshot) => void>();
    // 사용자 입력·서버 통지·명령 결과를 하나의 순서로 처리하는 큐다.
    private readonly events: UiDomainEvent[] = [];
    private is_started = false;
    private is_processing = false;

    /**
     * 함수 이름: UIStateController.constructor()
     * 기능: 초기 데이터로 STM을 준비하고 Port 실행 결과를 같은 이벤트 큐에 연결한다.
     * 인자: command_port -> 외부 명령 계약, options -> 초기 데이터·날짜 제공자, stm -> 선택적 STM 주입
     * 반환값: 생성된 UIStateController 인스턴스
     * 작성 날짜: 2026/09/17
     */
    constructor(command_port: UiCommandPort, private readonly options: UiApplicationFacadeOptions, stm?: UISTM) {
        // 외부 날짜 callback을 STM 정의에 전달하지 않고 Controller에만 보관한다.
        const { get_current_kst_date: _date_source, ...initial_options } = options;
        this.stm = stm ?? new UISTM(create_ui_application_machine({ ...initial_options, initial_monotonic_ms: performance.now() }));

        // 실행 중 들어온 완료 이벤트도 별도 경로 없이 같은 큐로 되돌린다.
        this.executor = new UiCommandExecutor(command_port, event => this.handle_event(event));
    }

    /**
     * 함수 이름: start()
     * 기능: 초기 Action을 한 번 실행·발행하고 시작 전에 받은 입력을 처리한다.
     * 인자: 없음
     * 반환값: 없음
     * 작성 날짜: 2026/09/17
     */
    start(): void {
        if (this.is_started || this.get_snapshot().status !== 'active') return;

        // 초기 요청 실행 중의 재진입 입력은 초기 snapshot 발행이 끝난 뒤 처리한다.
        this.is_started = true;
        this.is_processing = true;
        try { this.apply(this.stm.run(this.read_time())); }
        finally { this.is_processing = false; }
        this.drain();
    }

    /**
     * 함수 이름: stop()
     * 기능: 진행 작업·타이머·큐·구독을 정리하여 화면 런타임을 폐기한다.
     * 인자: 없음
     * 반환값: 없음
     * 작성 날짜: 2026/09/17
     */
    stop(): void {
        if (!this.is_started) return;

        // 큐를 먼저 닫아 취소 callback이 새 작업을 시작하지 못하게 한다.
        this.is_started = false;
        this.events.length = 0;
        this.executor.execute(this.stm.stop().actions);
        this.listeners.clear();
    }

    /**
     * 함수 이름: dispatch()
     * 기능: 외부 intent를 내부 이벤트 묶음으로 변환하여 직렬 큐에 전달한다.
     * 인자: intent -> 사용자 조작 또는 backend 통지
     * 반환값: 입력 변환 단계의 수락 여부; 명령 성공 여부가 아님
     * 작성 날짜: 2026/09/17
     */
    dispatch(intent: UiApplicationIntent): boolean {
        // 기존 입력 수락 계약을 유지하며 한 intent의 이벤트들은 한 묶음으로 전달한다.
        const router = new UiIntentRouter(this.get_snapshot());
        const accepted = router.dispatch(intent);
        if (accepted && router.events.length) this.handle_event({ type: 'ui.batch', events: router.events,
            ...(intent.type === 'BACKEND_SNAPSHOT_SYNCHRONIZED' ? { server_snapshot: intent.snapshot } : {}) });

        return accepted;
    }

    /**
     * 함수 이름: handle_event()
     * 기능: 사용자 입력과 작업 결과를 같은 큐에 넣고 활성 수명 안에서 처리한다.
     * 인자: event -> 내부 입력·완료·실패·타이머 이벤트
     * 반환값: 없음
     * 작성 날짜: 2026/09/17
     */
    handle_event(event: UiDomainEvent): void {
        if (this.get_snapshot().status !== 'active') return;
        this.events.push(event);
        this.drain();
    }

    /**
     * 함수 이름: subscribe()
     * 기능: 현재 snapshot을 즉시 전달하고 이후 변경을 받을 listener를 등록한다.
     * 인자: listener -> 완성된 snapshot을 받을 callback
     * 반환값: 등록한 listener를 제거하는 함수
     * 작성 날짜: 2026/09/17
     */
    subscribe(listener: (snapshot: UiApplicationSnapshot) => void): () => void {
        this.listeners.add(listener);
        listener(this.get_snapshot());

        return () => { this.listeners.delete(listener); };
    }

    /**
     * 함수 이름: get_snapshot()
     * 기능: STM이 보관한 최신 루트 상태를 읽는다.
     * 인자: 없음
     * 반환값: 최신 UI 루트 snapshot
     * 작성 날짜: 2026/09/17
     */
    get_snapshot(): UiApplicationSnapshot {
        return this.stm.get_snapshot();
    }

    /**
     * 함수 이름: get_view_model()
     * 기능: 루트 snapshot으로 현재 화면의 표시값·모달·처리 상태를 계산한다.
     * 인자: 없음
     * 반환값: React 표시용 AppViewModel
     * 작성 날짜: 2026/09/17
     */
    get_view_model(): AppViewModel {
        return select_app_view_model(this.get_snapshot());
    }

    /**
     * 함수 이름: read_time()
     * 기능: 현재 시각을 읽고 실제 새 CSV draft에 필요한 경우에만 기준 날짜를 확보한다.
     * 인자: event -> 날짜 갱신 필요 여부를 판단할 선택적 이벤트
     * 반환값: STM 평가에 사용할 시각·단조 시각·날짜
     * 작성 날짜: 2026/09/17
     */
    private read_time(event?: UiDomainEvent): UIEvaluationInput {
        // CSV 가드를 복제하지 않고 STM이 실제 초기화 Action을 선택했을 때만 날짜를 읽는다.
        return { now_epoch_ms: Date.now(), monotonic_ms: performance.now(),
            today: event && this.stm.requires_current_date(event) ? this.options.get_current_kst_date?.() ?? this.options.today : this.options.today };
    }

    /**
     * 함수 이름: drain()
     * 기능: 한 입력의 평가·요청 실행·발행을 마친 뒤 다음 이벤트를 처리한다.
     * 인자: 없음
     * 반환값: 없음; 비동기 작업 완료를 기다리며 큐를 막지 않음
     * 작성 날짜: 2026/09/17
     */
    private drain(): void {
        if (!this.is_started || this.is_processing) return;
        this.is_processing = true;
        try {
            // Promise를 기다리지 않는다. 현재 입력의 발행까지 마쳐야 다음 입력을 평가한다.
            while (this.is_started && this.events.length && this.get_snapshot().status === 'active') {
                const event = this.events.shift()!;
                this.apply(this.stm.handle(event, this.read_time(event)));
            }
            if (this.get_snapshot().status !== 'active') this.events.length = 0;
        } finally { this.is_processing = false; }
    }

    /**
     * 함수 이름: apply()
     * 기능: 이미 반영된 최종 snapshot의 요청을 순서대로 실행한 뒤 구독자에게 발행한다.
     * 인자: result -> STM의 최종 snapshot과 Action 요청
     * 반환값: 없음
     * 작성 날짜: 2026/09/17
     */
    private apply(result: UITransitionResult): void {
        // STM에는 최종 상태가 이미 반영되어 있으므로 Port 호출 중 조회해도 같은 값을 읽는다.
        this.executor.execute(result.actions);
        if (!this.is_started) return;

        // 실행 중 폐기되지 않았을 때만 완성된 snapshot을 모든 현재 구독자에게 발행한다.
        this.listeners.forEach(listener => {
            try { listener(result.snapshot); }
            catch (error) { queueMicrotask(() => { throw error; }); }
        });
    }
}
