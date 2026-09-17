import type {
    AppViewModel,
    UiApplicationIntent,
} from '../control';
import type {
    UiApplicationFactory,
    UiApplicationRuntime,
} from '../bootstrap';
import type { BackendBinanceConnectionStatus, BackendRuntimeEnvironment } from '../../shared/contracts';

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
    private cancel_pending_notification: (() => void) | null = null;

    /**
     * 함수 이름: UiApplicationStore.constructor()
     * 기능: actor를 아직 시작하지 않은 애플리케이션과 안정적인 최초 snapshot을 준비한다.
     * 인자: application_factory -> facade와 adapter를 새로 구성하는 factory
     * 반환값: React 외부 store 인스턴스
     * 작성 날짜: 2026/08/12
     */
    constructor(application_factory: UiApplicationFactory) {
        // function Object() { [native code] }
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
        // 새 구독은 예약된 비활성화를 무효화하고 필요한 실행 수명을 활성화한다.
        this.deactivation_version += 1;
        this.listeners.add(listener);

        if (!this.is_active) {
            this.activate_application();
        } else {
            listener();
        }

        // 구독 해제는 listener를 제거하고 남은 구독 수에 따라 폐기를 예약한다.
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
     * 함수 이름: get_environment()
     * 기능: 현재 session의 검증된 시세·계좌·주문 환경을 화면에 제공한다.
     * 인자: 없음
     * 반환값: runtime 환경 또는 미확인 null
     * 작성 날짜: 2026/09/05
     */
    get_environment(): BackendRuntimeEnvironment | null {
        return this.application?.environment ?? null;  // 정보가 없으면 실거래 환경으로 추측하지 않는다.
    }

    /**
     * 함수 이름: dispatch()
     * 기능: 활성 facade에 typed UI intent를 전달한다.
     * 인자: intent -> 사용자 의도 또는 backend 상태 event
     * 반환값: facade가 intent를 수락했는지 여부
     * 작성 날짜: 2026/08/12
     */
    dispatch(intent: UiApplicationIntent): boolean {
        const accepted = this.application?.facade.dispatch(intent) ?? false;

        // 사용자 클릭과 native 종료 의도는 즉시 보인다. 수신 event는 facade 구독에서 프레임별로 합친다.
        if (this.cancel_pending_notification !== null) this.publish_notification();

        return accepted;
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
        this.cancel_pending_notification?.();
        this.cancel_pending_notification = null;
        this.facade_unsubscribe?.();
        this.facade_unsubscribe = null;
        this.application?.deactivate();
        this.application = null;
        this.is_active = false;
    }

    /**
     * 함수 이름: notify_listeners()
     * 기능: 상태는 즉시 보존하면서 React 갱신은 한 프레임에 한 번으로 합쳐 누적 event의 중첩 렌더링을 막는다.
     * 인자: 없음
     * 반환값: 없음
     * 작성 날짜: 2026/08/12
     */
    private notify_listeners(): void {
        // 이미 발행이 예약되었거나 구독자가 없으면 중복 예약하지 않는다.
        if (this.cancel_pending_notification !== null || this.listeners.size === 0) return;

        /**
         * 함수 이름: publish()
         * 기능: 예약한 프레임 또는 timer에서 최신 화면 모델의 변경을 발행한다.
         * 인자: 없음
         * 반환값: 없음
         * 작성 날짜: 2026/09/17
         */
        const publish = () => this.publish_notification();

        // 브라우저 프레임에 발행을 모으고 사용할 수 없으면 타이머 경계를 이용한다.
        if (typeof requestAnimationFrame === 'function') {
            const frame = requestAnimationFrame(publish);
            this.cancel_pending_notification = () => cancelAnimationFrame(frame);
        } else {
            const timer = setTimeout(publish, 0);
            this.cancel_pending_notification = () => clearTimeout(timer);
        }
    }

    /**
     * 함수 이름: publish_notification()
     * 기능: 대기 알림을 정리하고 활성 runtime의 최신 캐시 변경을 listener에 한 번 알린다.
     * 인자: 없음
     * 반환값: 없음; 발행 오류는 runtime 실패 처리 경계로 전달
     * 작성 날짜: 2026/09/17
     */
    private publish_notification(): void {
        this.cancel_pending_notification?.();
        this.cancel_pending_notification = null;
        if (!this.is_active) return;
        try {
            this.listeners.forEach((listener) => listener());
        } catch (error) {
            if (this.application?.on_publication_failure !== undefined) this.application.on_publication_failure(error);
            else throw error;
        }
    }
}
