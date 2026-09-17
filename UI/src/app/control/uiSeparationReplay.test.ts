import { afterEach, expect, it, vi } from 'vitest';

import { UiApplicationFacade, type UiApplicationIntent } from './UiApplicationFacade';
import { FakeUiCommandAdapter } from '../../shared/testing';

// Captured against 58727e2901a04a2a0faeb6dd594b94dc51b232d8 before the separation.
// This records observable states and commands, not actor/runtime implementation details.
const applications: UiApplicationFacade[] = [];
afterEach(() => {
    // 화면 또는 실행 수명의 종료를 요청한다.
    applications.splice(0).forEach(application => application.stop());

    // 테스트가 바꾼 전역 환경과 실행 자원을 정리한다.
    vi.useRealTimers();
});


/**
 * 함수 이름: settle()
 * 기능: 명령 완료와 후속 상태 전이가 처리될 microtask를 진행한다.
 * 인자: 없음
 * 반환값: 완료 대기 Promise
 * 작성 날짜: 2026/09/17
 */
async function settle() {
    for (let index = 0; index < 20; index++) await Promise.resolve();
}


/**
 * 함수 이름: deferred()
 * 기능: 테스트가 성공·실패 시점을 직접 결정할 Promise를 만든다.
 * 인자: 없음
 * 반환값: promise와 외부 resolve·reject 함수
 * 작성 날짜: 2026/09/17
 */
function deferred<T>() {
    let resolve!: (value: T) => void;
    let reject!: (error: Error) => void;
    const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });

    return { promise, resolve, reject };
}


/**
 * 함수 이름: replay_fixture()
 * 기능: 고정 시간·가짜 adapter로 기준 동작의 입력·snapshot·명령 기록 환경을 준비한다.
 * 인자: 없음
 * 반환값: 앱·adapter·기록 목록과 입력/날짜 제어 도구
 * 작성 날짜: 2026/09/17
 */
function replay_fixture() {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-17T14:59:58.000Z'));
    const port = new FakeUiCommandAdapter();
    let today = '2026-09-17';
    const application = new UiApplicationFacade(port, {
        today,
        get_current_kst_date: () => today,
    });
    applications.push(application);
    const trace: unknown[] = [];

    /**
     * 함수 이름: capture()
     * 기능: 현재 상태 경로·화면 모델·명령 목록을 변경되지 않는 기록으로 복사한다.
     * 인자: label -> 관찰 시점을 구분할 이름
     * 반환값: 없음
     * 작성 날짜: 2026/09/17
     */
    const capture = (label: string) => {
        const snapshot = application.get_snapshot();
        trace.push(JSON.parse(JSON.stringify({
            label,
            value: snapshot.value,
            status: snapshot.status,
            view: application.get_view_model(),
            commands: port.command_records,
        })));
    };
    application.subscribe(() => capture('publication'));
    application.start();

    /**
     * 함수 이름: send()
     * 기능: intent 수락 여부와 처리 직후의 관찰값을 기준 기록에 추가한다.
     * 인자: intent -> 재생할 외부 UI 입력
     * 반환값: 없음
     * 작성 날짜: 2026/09/17
     */
    const send = (intent: UiApplicationIntent) => {
        const accepted = application.dispatch(intent);
        trace.push({ intent, accepted });
        capture('after input');
    };

    return { application, port, trace, capture, send, set_today: (value: string) => { today = value; } };
}

it('preserves the pre-separation navigation, selection, commands, CSV and date trace', async () => {
    // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
    const { port, trace, capture, send, set_today } = replay_fixture();
    send({ type: 'START_TRADING_CLICKED' });
    send({ type: 'SELECT_REGIME_NOTICE_CONFIRMED' });
    send({ type: 'REGIME_TYPE_CLICKED', regime: 'type0' });
    send({ type: 'REGIME_CHANGE_CONFIRMED' });

    // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
    await settle();
    send({ type: 'API_CONNECTED' });
    send({ type: 'START_TRADING_CLICKED' });
    send({ type: 'START_TRADING_CONFIRMED' });
    await settle();
    send({ type: 'SCALE_IN_CHANGED', percentage: 73 });
    await settle();
    send({ type: 'CHART_INTERVAL_SELECTED', interval: '4h' });
    send({ type: 'SHOW_TRADE_HISTORY' });
    await settle();
    send({ type: 'HISTORY_SIDE_SELECTED', side: 'sell' });
    await settle();
    send({ type: 'OPEN_CSV_EXPORT' });
    port.selected_directory = null;
    send({ type: 'CSV_DIRECTORY_SELECT_CLICKED' });
    await settle();
    port.selected_directory = '/Users/demo/Exports';
    send({ type: 'CSV_DIRECTORY_SELECT_CLICKED' });
    await settle();
    send({ type: 'CSV_EXPORT_SUBMITTED' });
    await settle();
    send({ type: 'CLOSE_CSV_EXPORT' });
    set_today('2026-09-18');
    send({ type: 'OPEN_CSV_EXPORT' });
    send({ type: 'CLOSE_CSV_EXPORT' });
    send({ type: 'BACK_TO_DASHBOARD' });
    capture('complete');

    // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
    await expect(JSON.stringify(trace, null, 2)).toMatchFileSnapshot('./__snapshots__/ui-separation-navigation.json');
});

it('preserves hidden write completion, latest failure and stale response traces', async () => {
    // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
    const { port, trace, capture, send } = replay_fixture();
    const first = deferred<void>();
    const latest = deferred<void>();
    const original = port.update_split_order.bind(port);
    vi.spyOn(port, 'update_split_order')
        .mockImplementationOnce((side, value) => { void original(side, value); return first.promise; })
        .mockImplementationOnce((side, value) => { void original(side, value); return latest.promise; });
    send({ type: 'SCALE_IN_CHANGED', percentage: 60 });
    send({ type: 'SCALE_OUT_CHANGED', percentage: 20 });
    send({ type: 'SHOW_TRADE_HISTORY' });

    // 대기 중인 작업의 성공·실패를 제어해 완료 순서를 재현한다.
    latest.reject(new Error('latest failed'));
    await settle();
    first.resolve();
    await settle();
    send({ type: 'BACK_TO_DASHBOARD' });
    const applied = deferred<void>();
    const original_apply = port.apply_regime.bind(port);

    // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
    vi.spyOn(port, 'apply_regime').mockImplementation(value => { void original_apply(value); return applied.promise; });
    send({ type: 'REGIME_TYPE_CLICKED', regime: 'type1' });
    send({ type: 'REGIME_CHANGE_CONFIRMED' });
    send({ type: 'SHOW_TRADE_HISTORY' });

    // 대기 중인 작업의 성공·실패를 제어해 완료 순서를 재현한다.
    applied.resolve();
    await settle();
    send({ type: 'BACK_TO_DASHBOARD' });
    capture('complete');

    // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
    await expect(JSON.stringify(trace, null, 2)).toMatchFileSnapshot('./__snapshots__/ui-separation-races.json');
});

it('preserves hidden highlight and KST midnight timer traces', async () => {
    // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
    const { trace, capture, send } = replay_fixture();
    send({ type: 'START_TRADING_CLICKED' });
    send({ type: 'SELECT_REGIME_NOTICE_CONFIRMED' });
    send({ type: 'SHOW_TRADE_HISTORY' });

    // 가짜 시간을 진행해 예약된 작업과 후속 상태 반영을 실행한다.
    await settle();

    await vi.advanceTimersByTimeAsync(2100);
    capture('after midnight');
    await vi.advanceTimersByTimeAsync(2000);
    capture('after highlight');
    send({ type: 'BACK_TO_DASHBOARD' });

    // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
    await expect(JSON.stringify(trace, null, 2)).toMatchFileSnapshot('./__snapshots__/ui-separation-timers.json');
});
