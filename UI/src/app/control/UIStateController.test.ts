import { afterEach, expect, it, vi } from 'vitest';
import { UIStateController } from './UIStateController';
import { UiApplicationFacade } from './UiApplicationFacade';
import { FakeUiCommandAdapter } from '../../shared/testing';

const controllers: UIStateController[] = [];
afterEach(() => { controllers.splice(0).forEach(controller => controller.stop()); vi.useRealTimers(); });


/**
 * 함수 이름: settle()
 * 기능: 명령 Promise와 후속 큐 이벤트가 완료되도록 microtask를 진행한다.
 * 인자: 없음
 * 반환값: 완료 대기 Promise
 * 작성 날짜: 2026/09/17
 */
async function settle() { for (let index = 0; index < 20; index++) await Promise.resolve(); }


/**
 * 함수 이름: create()
 * 기능: Controller를 초기화하고 테스트 종료 시 정리할 목록에 등록한다.
 * 인자: port -> 가짜 명령 adapter, get_current_kst_date -> 선택적 날짜 제공자
 * 반환값: 검증할 Controller
 * 작성 날짜: 2026/09/17
 */
function create(port = new FakeUiCommandAdapter(), get_current_kst_date?: () => string) {
    const controller = new UIStateController(port, { today: '2026-09-17',
        ...(get_current_kst_date ? { get_current_kst_date } : {}) });
    controllers.push(controller);

    return controller;
}

it('keeps the existing facade as the exact same implementation and queues cold inputs until start', async () => {
    expect(UiApplicationFacade).toBe(UIStateController);  // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.

    // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
    const port = new FakeUiCommandAdapter();
    const controller = create(port);

    // SHOW_TRADE_HISTORY 입력을 전달해 해당 전이를 실행한다.
    controller.dispatch({ type: 'SHOW_TRADE_HISTORY' });
    expect(port.command_records).toEqual([]);  // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.

    // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
    controller.start();
    controller.start();
    await settle();
    expect(port.command_records.map(record => record.name)).toEqual(['load_trade_history']);  // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
});

it('commits pending state before executing work and serializes reentrant inputs after publication', async () => {
    // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
    const port = new FakeUiCommandAdapter();
    const controller = create(port);

    // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
    controller.start();
    const order: string[] = [];

    // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
    vi.spyOn(port, 'update_split_order').mockImplementation(async (side, percentage) => {
        expect(controller.get_view_model().split_order.is_pending).toBe(true);
        order.push(`${side}:${percentage}`);
        if (side === 'scale_in') controller.dispatch({ type: 'SCALE_OUT_CHANGED', percentage: 20 });
    });

    const unsubscribe = controller.subscribe(snapshot => {
        if (snapshot.context.features.split_order.pending_percentage) {
            order.push(`publish:${snapshot.context.features.split_order.pending_percentage}`);
        }
    });

    // SCALE_IN_CHANGED 입력을 전달해 해당 전이를 실행한다.
    controller.dispatch({ type: 'SCALE_IN_CHANGED', percentage: 60 });

    expect(order.slice(0, 4)).toEqual(['scale_in:60', 'publish:60', 'scale_out:20', 'publish:20']);  // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.

    // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
    await settle();
    unsubscribe();
});

it('reads the date once for an actual new CSV draft and never for an ignored duplicate open', async () => {
    // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
    const date = vi.fn(() => '2026-09-18');
    const controller = create(new FakeUiCommandAdapter(), date);

    // SHOW_TRADE_HISTORY → OPEN_CSV_EXPORT → OPEN_CSV_EXPORT 입력을 전달해 해당 전이를 실행한다.
    controller.start();
    controller.dispatch({ type: 'SHOW_TRADE_HISTORY' });
    await settle();
    controller.dispatch({ type: 'OPEN_CSV_EXPORT' });
    controller.dispatch({ type: 'OPEN_CSV_EXPORT' });

    // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
    expect(date).toHaveBeenCalledTimes(1);
    expect(controller.get_view_model().csv_export.start_date).toBe('2026-09-18');

    // CLOSE_CSV_EXPORT → OPEN_CSV_EXPORT 입력을 전달해 해당 전이를 실행한다.
    controller.dispatch({ type: 'CLOSE_CSV_EXPORT' });
    controller.dispatch({ type: 'OPEN_CSV_EXPORT' });
    expect(date).toHaveBeenCalledTimes(2);  // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
});

it('aborts active reads on stop and ignores every subsequent completion and input', async () => {
    // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
    const port = new FakeUiCommandAdapter();
    let signal: AbortSignal | undefined;
    let resolve!: (value: { records: []; summary: typeof port.trade_history_summary }) => void;
    vi.spyOn(port, 'load_trade_history').mockImplementation((_query, input_signal) => {
        signal = input_signal;

        return new Promise(complete => { resolve = complete; });
    });
    const controller = create(port);

    // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
    controller.start();

    // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
    const listener = vi.fn();
    controller.subscribe(listener);

    // SHOW_TRADE_HISTORY 입력을 전달해 해당 전이를 실행한다.
    controller.dispatch({ type: 'SHOW_TRADE_HISTORY' });
    controller.stop();
    const count = listener.mock.calls.length;

    expect(signal?.aborted).toBe(true);  // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.

    // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
    resolve({ records: [], summary: port.trade_history_summary });
    await settle();

    // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
    expect(listener).toHaveBeenCalledTimes(count);
    expect(controller.get_snapshot().status).toBe('stopped');
    expect(controller.dispatch({ type: 'BACK_TO_DASHBOARD' })).toBe(false);
});

it('supplies monotonic indicator receipt times and preserves the first timestamp on duplicate payloads', () => {
    // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
    vi.useFakeTimers();
    const controller = create();

    // 가짜 시간을 진행해 예약된 작업과 후속 상태 반영을 실행한다.
    controller.start();
    const indicators = { phase_key: 'ENTRY_WAIT', notice: null, server_time: '2026-09-17T00:00:00Z', conditions: [] };

    /**
     * 함수 이름: received_at()
     * 기능: 중복 지표 수신 전후의 최초 수신 시각을 읽는다.
     * 인자: 없음
     * 반환값: 현재 지표의 단조 수신 시각 또는 null
     * 작성 날짜: 2026/09/17
     */
    const received_at = () => controller.get_snapshot().context.features.recent_orders.strategy_indicators_received_at;
    vi.advanceTimersByTime(1000);
    controller.handle_event({ type: 'recent_orders.STRATEGY_INDICATORS_SYNCHRONIZED',
        source: { type: 'STRATEGY_INDICATORS_SYNCHRONIZED', indicators } });
    expect(received_at()).toBe(1000);  // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.

    // 가짜 시간을 진행해 예약된 작업과 후속 상태 반영을 실행한다.
    vi.advanceTimersByTime(1000);
    controller.handle_event({ type: 'recent_orders.STRATEGY_INDICATORS_SYNCHRONIZED',
        source: { type: 'STRATEGY_INDICATORS_SYNCHRONIZED', indicators: { ...indicators } } });
    expect(received_at()).toBe(1000);  // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.

    // recent_orders.STRATEGY_INDICATORS_SYNCHRONIZED 입력을 전달해 해당 전이를 실행한다.
    controller.handle_event({ type: 'recent_orders.STRATEGY_INDICATORS_SYNCHRONIZED',
        source: { type: 'STRATEGY_INDICATORS_SYNCHRONIZED', indicators: { ...indicators, server_time: '2026-09-17T00:00:02Z' } } });
    expect(received_at()).toBe(2000);  // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
});
