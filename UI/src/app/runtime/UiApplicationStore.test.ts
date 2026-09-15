import { DEFAULT_DASHBOARD_PROPS } from '../../routes/dashboard';
import { FakeUiCommandAdapter, FIXTURE_TODAY } from '../../shared/testing';
import { UiApplicationFacade } from '../control';
import type { DemoUiApplication } from '../bootstrap';
import { UiApplicationStore } from './UiApplicationStore';

/**
 * 함수 이름: create_test_application()
 * 기능: store 수명주기 테스트가 호출 횟수를 관찰할 facade와 fake adapter를 생성한다.
 * 인자: 없음
 * 반환값: actor 시작 전 테스트 애플리케이션 구성
 * 작성 날짜: 2026/08/12
 */
function create_test_application(): DemoUiApplication {
    const command_adapter = new FakeUiCommandAdapter();
    const facade = new UiApplicationFacade(command_adapter, {
        applied_regime: 'type0',
        recommended_regime: 'type0',
        regime_metrics: DEFAULT_DASHBOARD_PROPS.regime.metrics,
        today: FIXTURE_TODAY,
    });

    return {
        command_adapter,
        facade,
        activate: () => {
            facade.start();
            facade.dispatch({ type: 'API_CONNECTED', sequence: 1 });
        },
        deactivate: () => facade.stop(),
    };
}

describe('UiApplicationStore', () => {
    afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

    it('첫 구독에서 facade를 시작하고 dispatch snapshot을 React listener로 발행한다', async () => {
        vi.useFakeTimers();
        const store = new UiApplicationStore(create_test_application);
        let notification_count = 0;

        const unsubscribe = store.subscribe(() => {
            notification_count += 1;
        });

        expect(store.get_snapshot().connection.is_online).toBe(true);
        expect(store.get_snapshot().trading.is_trading).toBe(false);

        store.dispatch({ type: 'API_DISCONNECTED', reason: 'test' });

        expect(store.get_snapshot().connection.is_online).toBe(false);
        await vi.advanceTimersByTimeAsync(20);
        expect(notification_count).toBeGreaterThan(0);
        unsubscribe();
    });

    it('300개 연속 상태는 즉시 최신 값으로 보존하고 React에는 한 프레임만 발행한다', async () => {
        vi.useFakeTimers();
        const application = create_test_application();
        const store = new UiApplicationStore(() => application);
        const listener = vi.fn();
        const unsubscribe = store.subscribe(listener);
        for (let index = 0; index < 300; index++) {
            application.facade.dispatch({ type: index % 2 === 0 ? 'API_CONNECTED' : 'API_DISCONNECTED' });
        }
        expect(store.get_snapshot().connection.is_online).toBe(false);
        expect(listener).not.toHaveBeenCalled();
        await vi.advanceTimersByTimeAsync(20);
        expect(listener).toHaveBeenCalledOnce();
        unsubscribe();
        await Promise.resolve();
        expect(vi.getTimerCount()).toBe(0);
    });

    it('비동기 화면 알림 예외를 같은 runtime에 전달하고 구독 종료 후 알림은 취소한다', async () => {
        vi.useFakeTimers();
        const on_publication_failure = vi.fn();
        const application = { ...create_test_application(), on_publication_failure };
        const store = new UiApplicationStore(() => application);
        const error = new Error('fixture publication failure');
        const unsubscribe = store.subscribe(() => { throw error; });
        await vi.advanceTimersByTimeAsync(20);
        expect(on_publication_failure).toHaveBeenCalledExactlyOnceWith(error);
        application.facade.dispatch({ type: 'API_DISCONNECTED' });
        unsubscribe();
        await Promise.resolve();
        await vi.advanceTimersByTimeAsync(20);
        expect(on_publication_failure).toHaveBeenCalledOnce();
        expect(vi.getTimerCount()).toBe(0);
    });

    it('StrictMode식 즉시 구독 교체에서는 동일 애플리케이션 snapshot을 유지한다', async () => {
        let application_count = 0;
        const store = new UiApplicationStore(() => {
            application_count += 1;
            return create_test_application();
        });
        const first_unsubscribe = store.subscribe(() => undefined);

        first_unsubscribe();
        const second_unsubscribe = store.subscribe(() => undefined);
        await Promise.resolve();

        expect(application_count).toBe(1);
        expect(store.get_snapshot().connection.is_online).toBe(true);
        second_unsubscribe();
    });
});
