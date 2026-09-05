import type {
    AppViewModel,
    UiApplicationIntent,
} from '../control';
import type {
    UiApplicationFactory,
    UiApplicationRuntime,
} from '../bootstrap';
import type { BackendBinanceConnectionStatus } from '../../shared/contracts';

type StoreListener = () => void;

/**
 * presenter가 UI actor에 intent를 보내고 즉시 최신 ViewModel을 확인하는 좁은 계약이다.
 */
export interface UiApplicationController {
    dispatch(intent: UiApplicationIntent): boolean;
    get_view_model(): AppViewModel;
}

/**
 * 클래스 이름: UiApplicationStore
 * 기능: UiApplicationFacade를 React 외부 store로 감싸고 StrictMode 구독 수명주기를 안전하게 관리한다.
 * 작성 날짜: 2026/08/12
 */
export class UiApplicationStore implements UiApplicationController {
    private readonly application_factory: UiApplicationFactory;
    private readonly listeners = new Set<StoreListener>();
    private application: UiApplicationRuntime | null;
    private facade_unsubscribe: (() => void) | null = null;
    private current_view_model: AppViewModel;
    private is_active = false;
    private deactivation_version = 0;

    /**
     * 함수 이름: UiApplicationStore.constructor()
     * 기능: actor를 아직 시작하지 않은 애플리케이션과 안정적인 최초 snapshot을 준비한다.
     * 인자: application_factory -> facade와 adapter를 새로 구성하는 factory
     * 반환값: React 외부 store 인스턴스
     * 작성 날짜: 2026/08/12
     */
    constructor(application_factory: UiApplicationFactory) {
        this.application_factory = application_factory;
        this.application = this.application_factory();
        this.current_view_model = this.application.facade.get_view_model();
    }

    /**
     * 함수 이름: subscribe()
     * 기능: React listener를 등록하고 첫 구독에서 주입된 runtime lifecycle을 시작한다.
     * 인자: listener -> snapshot 변경을 React에 알릴 callback
     * 반환값: 해당 listener를 제거하는 구독 해제 함수
     * 작성 날짜: 2026/08/12
     */
    subscribe(listener: StoreListener): () => void {
        this.deactivation_version += 1;
        this.listeners.add(listener);

        if (!this.is_active) {
            this.activate_application();
        } else {
            listener();
        }

        return () => {
            this.listeners.delete(listener);
            this.schedule_deactivation();
        };
    }

    /**
     * 함수 이름: get_snapshot()
     * 기능: useSyncExternalStore가 참조 동일성으로 비교할 캐시된 ViewModel을 반환한다.
     * 인자: 없음
     * 반환값: 마지막 actor 갱신에서 생성된 애플리케이션 ViewModel
     * 작성 날짜: 2026/08/12
     */
    get_snapshot(): AppViewModel {
        return this.current_view_model;
    }

    /**
     * 함수 이름: get_view_model()
     * 기능: event 연속 처리 중 presenter가 확인할 최신 캐시 ViewModel을 반환한다.
     * 인자: 없음
     * 반환값: 최신 애플리케이션 ViewModel
     * 작성 날짜: 2026/08/12
     */
    get_view_model(): AppViewModel {
        return this.current_view_model;
    }

    /**
     * 함수 이름: dispatch()
     * 기능: 활성 facade에 typed UI intent를 전달한다.
     * 인자: intent -> 사용자 의도 또는 backend 상태 event
     * 반환값: facade가 intent를 수락했는지 여부
     * 작성 날짜: 2026/08/12
     */
    dispatch(intent: UiApplicationIntent): boolean {
        return this.application?.facade.dispatch(intent) ?? false;
    }

    /**
     * 함수 이름: load_binance_connection_status()
     * 기능: 활성 runtime의 조회 경계로 Binance 연결 진단을 전달한다.
     * 인자: signal -> 툴팁 수명주기 취소 신호
     * 반환값: 연결 상태 또는 조회 불가 실패 Promise
     * 작성 날짜: 2026/09/05
     */
    async load_binance_connection_status(signal?: AbortSignal): Promise<BackendBinanceConnectionStatus> {
        if (!this.is_active || this.application?.load_binance_connection_status === undefined) {
            throw new Error('Binance connection status is unavailable');
        }

        return this.application.load_binance_connection_status(signal);
    }

    /**
     * 함수 이름: activate_application()
     * 기능: 필요하면 runtime을 재생성하고 snapshot 구독과 주입된 lifecycle 시작을 순서대로 수행한다.
     * 인자: 없음
     * 반환값: 없음
     * 작성 날짜: 2026/08/12
     */
    private activate_application(): void {
        if (this.application === null) {
            this.application = this.application_factory();
            this.current_view_model = this.application.facade.get_view_model();
        }

        const application = this.application;
        this.is_active = true;
        this.facade_unsubscribe = application.facade.subscribe(() => {
            this.current_view_model = application.facade.get_view_model();
            this.notify_listeners();
        });
        application.activate();
    }

    /**
     * 함수 이름: schedule_deactivation()
     * 기능: StrictMode의 즉시 재구독 기회를 보존한 뒤 실제 마지막 구독에서만 facade를 종료한다.
     * 인자: 없음
     * 반환값: 없음
     * 작성 날짜: 2026/08/12
     */
    private schedule_deactivation(): void {
        const scheduled_version = ++this.deactivation_version;

        queueMicrotask(() => {
            if (scheduled_version !== this.deactivation_version || this.listeners.size > 0) {
                return;
            }

            this.deactivate_application();
        });
    }

    /**
     * 함수 이름: deactivate_application()
     * 기능: 마지막 React 구독이 사라졌을 때 facade 구독과 모든 feature actor를 정리한다.
     * 인자: 없음
     * 반환값: 없음
     * 작성 날짜: 2026/08/12
     */
    private deactivate_application(): void {
        this.facade_unsubscribe?.();
        this.facade_unsubscribe = null;
        this.application?.deactivate();
        this.application = null;
        this.is_active = false;
    }

    /**
     * 함수 이름: notify_listeners()
     * 기능: 등록된 모든 React listener에 캐시 ViewModel 변경을 알린다.
     * 인자: 없음
     * 반환값: 없음
     * 작성 날짜: 2026/08/12
     */
    private notify_listeners(): void {
        this.listeners.forEach((listener) => listener());
    }
}
