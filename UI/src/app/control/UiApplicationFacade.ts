import { createActor, type ActorRefFrom, type Subscription } from 'xstate';

import type { UiCommandPort } from '../../shared/ports';
import { create_ui_application_machine } from '../machines/uiApplicationMachine';
import type { UiApplicationSnapshot } from '../machines/uiApplicationTypes';
import type {
    AppViewModel,
    UiApplicationFacadeOptions,
    UiApplicationIntent,
} from './uiApplicationContracts';
import { UiIntentRouter } from './uiApplicationIntents';
import { select_app_view_model } from './selectAppViewModel';

export { select_app_view_model } from './selectAppViewModel';
export type * from './uiApplicationContracts';
export type { UiApplicationSnapshot } from '../machines/uiApplicationTypes';


/**
 * 클래스 이름: UiApplicationFacade
 * 기능: 루트 UI actor의 수명·입력·snapshot 구독과 화면 모델 경계를 제공한다.
 * 작성 날짜: 2026/09/16
 */
export class UiApplicationFacade {
    private readonly actor: ActorRefFrom<ReturnType<typeof create_ui_application_machine>>;
    private readonly listeners = new Set<(snapshot: UiApplicationSnapshot) => void>();
    private subscription: Subscription | undefined;
    private is_started = false;

    /**
     * 함수 이름: UiApplicationFacade.constructor()
     * 기능: 명령 port와 초기 옵션으로 전체 UI를 실행할 루트 actor를 준비한다.
     * 인자: command_port -> 비동기 명령 계약, options -> UI 초기 데이터와 설정
     * 반환값: 생성된 UiApplicationFacade 인스턴스
     * 작성 날짜: 2026/09/16
     */
    constructor(command_port: UiCommandPort, options: UiApplicationFacadeOptions) {
        this.actor = createActor(create_ui_application_machine(command_port, options));
    }

    /**
     * 함수 이름: start()
     * 기능: 루트 snapshot 구독을 등록하고 UI actor를 한 번만 시작한다.
     * 인자: 없음
     * 반환값: 없음
     * 작성 날짜: 2026/09/16
     */
    start(): void {
        if (this.is_started) {
            return;
        }

        this.is_started = true;
        this.subscription = this.actor.subscribe(snapshot => {
            this.listeners.forEach(listener => listener(snapshot));
        });
        this.actor.start();
    }

    /**
     * 함수 이름: stop()
     * 기능: 루트 actor와 snapshot 구독을 종료하고 화면 listener를 정리한다.
     * 인자: 없음
     * 반환값: 없음
     * 작성 날짜: 2026/09/16
     */
    stop(): void {
        if (!this.is_started) {
            return;
        }

        this.is_started = false;
        this.subscription?.unsubscribe();
        this.actor.stop();
        this.listeners.clear();
    }

    /**
     * 함수 이름: dispatch()
     * 기능: 외부 intent를 내부 이벤트 묶음으로 변환하여 루트 actor에 전달한다.
     * 인자: intent -> 사용자 입력 또는 backend 통지
     * 반환값: 입력 변환 단계에서 수락했으면 true
     * 작성 날짜: 2026/09/16
     */
    dispatch(intent: UiApplicationIntent): boolean {
        const router = new UiIntentRouter(this.get_snapshot());
        const accepted = router.dispatch(intent);

        if (accepted && router.events.length) {
            this.actor.send({
                type: 'ui.batch',
                events: router.events,
                ...(intent.type === 'BACKEND_SNAPSHOT_SYNCHRONIZED' ? {
                    server_snapshot: intent.snapshot,
                } : {}),
            });
        }

        return accepted;
    }

    /**
     * 함수 이름: subscribe()
     * 기능: 루트 snapshot listener를 등록하고 현재 snapshot을 즉시 전달한다.
     * 인자: listener -> 루트 snapshot 변경을 받을 callback
     * 반환값: 등록한 listener를 제거하는 함수
     * 작성 날짜: 2026/09/16
     */
    subscribe(listener: (snapshot: UiApplicationSnapshot) => void): () => void {
        this.listeners.add(listener);
        listener(this.get_snapshot());

        return () => {
            this.listeners.delete(listener);
        };
    }

    /**
     * 함수 이름: get_snapshot()
     * 기능: 현재 UI 루트의 활성 상태와 공통 데이터를 읽는다.
     * 인자: 없음
     * 반환값: UI 루트 snapshot
     * 작성 날짜: 2026/09/16
     */
    get_snapshot(): UiApplicationSnapshot {
        return this.actor.getSnapshot();
    }

    /**
     * 함수 이름: get_view_model()
     * 기능: 현재 루트 snapshot에서 React 표시용 화면 모델을 계산한다.
     * 인자: 없음
     * 반환값: 현재 AppViewModel
     * 작성 날짜: 2026/09/16
     */
    get_view_model(): AppViewModel {
        return select_app_view_model(this.get_snapshot());
    }
}
