import { create_feature_test_actor } from '../../../shared/testing/createFeatureTestController';
import type {
    HistoryPeriod,
    TradeHistoryDetails,
    TradeHistoryQuery,
    TradeSideFilter,
} from '../../../shared/contracts';
import { FakeUiCommandAdapter, TRADE_RECORD_FIXTURES } from '../../../shared/testing';
import {
    create_trade_history_machine,
    milliseconds_until_next_kst_midnight,
    type TradeHistoryMachineEvent,
} from './tradeHistoryMachine';


/**
 * 함수 이름: wait_for_history_settlement()
 * 기능: 거래 내역 조회 Promise 결과가 테스트 Controller의 snapshot에 반영될 때까지 기다린다.
 * 인자: 없음
 * 반환값: 다음 event loop turn에서 완료되는 Promise
 * 작성 날짜: 2026/08/23
 */
async function wait_for_history_settlement(): Promise<void> {
    await new Promise<void>((resolve) => {
        setTimeout(resolve, 0);
    });
}


/**
 * 클래스 이름: DeferredTradeHistoryAdapter
 * 기능: 진행 중 query를 test가 직접 완료해 체결·resync 경쟁 순서를 결정한다.
 * 작성 날짜: 2026/08/23
 */
class DeferredTradeHistoryAdapter extends FakeUiCommandAdapter {
    readonly queries: Array<TradeHistoryQuery> = [];
    readonly pending_requests: Array<{
        readonly signal: AbortSignal | undefined;
        readonly resolve: (details: TradeHistoryDetails) => void;
        readonly reject: (error: Error) => void;
    }> = [];

    /**
     * 함수 이름: load_trade_history()
     * 기능: query를 기록하고 test가 resolve할 때까지 composite 결과를 대기시킨다.
     * 인자: query -> 결합된 period/side filter, signal -> Controller 실행부의 조회 취소 신호
     * 반환값: test가 제어하는 상세 조회 Promise
     * 작성 날짜: 2026/08/23
     */
    override async load_trade_history(
        query: TradeHistoryQuery,
        signal?: AbortSignal,
    ): Promise<TradeHistoryDetails> {
        this.queries.push(query);

        return new Promise<TradeHistoryDetails>((resolve, reject) => {
            let is_settled = false;

            /**
             * 함수 이름: settle_request()
             * 기능: 상세 조회 Promise를 한 번만 완료시키고 취소 listener를 제거한다.
             * 인자: settle -> 성공 또는 실패를 확정할 callback
             * 반환값: 없음
             * 작성 날짜: 2026/09/17
             */
            const settle_request = (settle: () => void) => {
                if (is_settled) {
                    return;
                }

                is_settled = true;
                signal?.removeEventListener('abort', abort_request);
                settle();
            };

            /**
             * 함수 이름: abort_request()
             * 기능: 취소된 상세 조회를 AbortError로 한 번만 완료시킨다.
             * 인자: 없음
             * 반환값: 없음
             * 작성 날짜: 2026/09/17
             */
            const abort_request = () => {
                settle_request(() => {
                    reject(new DOMException('Trade history request aborted', 'AbortError'));
                });
            };

            // 실제 fetch처럼 조회 signal이 중단되면 pending test Promise도 즉시 종료한다.
            signal?.addEventListener('abort', abort_request, { once: true });
            this.pending_requests.push({
                signal,
                resolve: (details) => settle_request(() => resolve(details)),
                reject: (error) => settle_request(() => reject(error)),
            });
            if (signal?.aborted === true) {
                abort_request();
            }
        });
    }
}

const PERIOD_CASES: ReadonlyArray<{
    readonly period: HistoryPeriod;
    readonly event: TradeHistoryMachineEvent;
}> = [
    { period: 'today', event: { type: 'SELECT_DISPLAY_TODAY_HISTORY' } },
    { period: 'last7days', event: { type: 'SELECT_DISPLAY_WEEKLY_HISTORY' } },
    { period: 'last30days', event: { type: 'SELECT_DISPLAY_MONTHLY_HISTORY' } },
    { period: 'all', event: { type: 'SELECT_DISPLAY_ALL_HISTORY' } },
];

const SIDE_CASES: ReadonlyArray<{
    readonly side: TradeSideFilter;
    readonly event: TradeHistoryMachineEvent;
}> = [
    { side: 'all', event: { type: 'ALL_TRADE_HISTORY_SELECTED' } },
    { side: 'buy', event: { type: 'BUY_TRADE_HISTORY_SELECTED' } },
    { side: 'sell', event: { type: 'SELL_TRADE_HISTORY_SELECTED' } },
];

const FILTER_CASES = PERIOD_CASES.flatMap((period_case) => {
    return SIDE_CASES.map((side_case) => ({
        period: period_case.period,
        period_event: period_case.event,
        side: side_case.side,
        side_event: side_case.event,
        label: `${period_case.period}/${side_case.side}`,
    }));
});

describe('tradeHistoryMachine', () => {
    it('다음 KST 자정까지의 delay를 UTC 경계에서 정확히 계산한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const one_second_before_midnight = Date.parse('2026-08-21T14:59:59.000Z');
        const exact_midnight = Date.parse('2026-08-21T15:00:00.000Z');

        // 잘못된 입력이나 실행 실패가 정해진 오류로 전달되는지 검증한다.
        expect(milliseconds_until_next_kst_midnight(one_second_before_midnight)).toBe(1_000);
        expect(milliseconds_until_next_kst_midnight(exact_midnight)).toBe(86_400_000);
        expect(() => milliseconds_until_next_kst_midnight(Number.POSITIVE_INFINITY)).toThrow(
            'now_epoch_ms must be finite',
        );
    });

    it('test_show_trade_details_initial_query: fixture와 기존 filter가 있어도 첫 SHOW는 today/all live query를 실행한다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const command_adapter = new FakeUiCommandAdapter();
        const summaries: Array<TradeHistoryDetails['summary']> = [];
        const actor = create_feature_test_actor(create_trade_history_machine, command_adapter, {
            period: 'all',
            side: 'sell',
            records: TRADE_RECORD_FIXTURES,
            on_details_loaded: (summary) => summaries.push(summary),
        });

        // ENTER_TRADE_HISTORY 입력을 전달해 해당 전이를 실행한다.
        actor.start();
        actor.send({ type: 'ENTER_TRADE_HISTORY' });

        expect(actor.getSnapshot().matches('loading')).toBe(true);  // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.

        // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
        await wait_for_history_settlement();

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(command_adapter.command_records.at(-1)).toEqual({
            name: 'load_trade_history',
            payload: { period: 'today', side: 'all' },
        });
        expect(actor.getSnapshot().matches('ready')).toBe(true);
        expect(summaries).toEqual([command_adapter.trade_history_summary]);

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });

    it.each(FILTER_CASES)(
        'test_trade_history_filter_combination: $label을 항상 한 query payload로 보낸다',
        async ({ period, period_event, side, side_event }) => {
            // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
            const command_adapter = new FakeUiCommandAdapter();
            const actor = create_feature_test_actor(create_trade_history_machine, command_adapter);

            // ENTER_TRADE_HISTORY 입력을 전달해 해당 전이를 실행한다.
            actor.start();
            actor.send({ type: 'ENTER_TRADE_HISTORY' });
            await wait_for_history_settlement();
            actor.send(period_event);
            await wait_for_history_settlement();
            actor.send(side_event);
            await wait_for_history_settlement();

            // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
            expect(command_adapter.command_records.at(-1)).toEqual({
                name: 'load_trade_history',
                payload: { period, side },
            });

            // 화면 또는 실행 수명의 종료를 요청한다.
            actor.stop();
        },
    );

    it('Case 3 2.1.2: filter 응답은 행만 교체하고 기존 summary를 다시 적용하지 않는다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const command_adapter = new FakeUiCommandAdapter();
        const summaries: Array<TradeHistoryDetails['summary']> = [];
        const actor = create_feature_test_actor(create_trade_history_machine, command_adapter, {
            on_details_loaded: (summary) => summaries.push(summary),
        });

        // ENTER_TRADE_HISTORY → SELECT_DISPLAY_MONTHLY_HISTORY 입력을 전달해 해당 전이를 실행한다.
        actor.start();
        actor.send({ type: 'ENTER_TRADE_HISTORY' });
        await wait_for_history_settlement();
        const initial_summary = summaries[0]!;
        command_adapter.trade_history_summary = {
            ...initial_summary,
            position: { quantity: '999 ETH' },
        };

        // 2.1.2 filter 조회의 composite summary는 transport 검증만 거치고 UI summary에는 재적용하지 않는다.
        actor.send({ type: 'SELECT_DISPLAY_MONTHLY_HISTORY' });
        await wait_for_history_settlement();

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().matches('ready')).toBe(true);
        expect(summaries).toEqual([initial_summary]);

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });

    it('KST 자정 뒤 일반 refresh는 revision-safe 최신 daily summary를 다시 적용한다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const command_adapter = new FakeUiCommandAdapter();
        const summaries: Array<TradeHistoryDetails['summary']> = [];
        const actor = create_feature_test_actor(create_trade_history_machine, command_adapter, {
            on_details_loaded: (summary) => summaries.push(summary),
        });

        // ENTER_TRADE_HISTORY → REFRESH_TRADE_HISTORY 입력을 전달해 해당 전이를 실행한다.
        actor.start();
        actor.send({ type: 'ENTER_TRADE_HISTORY' });
        await wait_for_history_settlement();
        const previous_day_summary = summaries[0]!;
        const next_day_summary: TradeHistoryDetails['summary'] = {
            ...previous_day_summary,
            dailyReturn: { value: '0.00%', tone: 'neutral' },
            fees: {
                ...previous_day_summary.fees,
                amount: '0 USDT',
            },
        };
        command_adapter.trade_history_summary = next_day_summary;

        // Filter 변경과 달리 사용자가 요청한 전체 refresh는 backend의 새 account-day summary를 채택한다.
        actor.send({ type: 'REFRESH_TRADE_HISTORY' });
        await wait_for_history_settlement();

        expect(summaries).toEqual([previous_day_summary, next_day_summary]);  // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });

    it('활성 상세 화면은 KST 자정에 summary를 자동 refresh하고 화면 이탈 시 timer를 취소한다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        vi.useFakeTimers();
        try {
            const command_adapter = new FakeUiCommandAdapter();
            const summaries: Array<TradeHistoryDetails['summary']> = [];
            vi.setSystemTime(new Date('2026-09-17T14:59:59.000Z'));
            const actor = create_feature_test_actor(create_trade_history_machine, command_adapter, {
                on_details_loaded: (summary) => summaries.push(summary),
            });

            actor.start();
            actor.send({ type: 'ENTER_TRADE_HISTORY' });
            await vi.advanceTimersByTimeAsync(0);
            const previous_day_summary = summaries[0]!;
            const next_day_summary: TradeHistoryDetails['summary'] = {
                ...previous_day_summary,
                fees: {
                    ...previous_day_summary.fees,
                    amount: '0 USDT',
                },
            };
            command_adapter.trade_history_summary = next_day_summary;

            // ready state의 one-shot 자정 timer가 전체 summary refresh를 시작한다.
            await vi.advanceTimersByTimeAsync(1_000);
            expect(summaries).toEqual([previous_day_summary, next_day_summary]);
            expect(command_adapter.command_records.filter((record) => {
                return record.name === 'load_trade_history';
            })).toHaveLength(2);

            // Dashboard로 이탈한 actor는 다음 자정 delay가 지나도 background query를 만들지 않는다.
            actor.send({ type: 'LEAVE_TRADE_HISTORY' });
            await vi.advanceTimersByTimeAsync(1_000);
            expect(actor.getSnapshot().matches('idle')).toBe(true);
            expect(command_adapter.command_records.filter((record) => {
                return record.name === 'load_trade_history';
            })).toHaveLength(2);
            actor.stop();
        } finally {
            vi.useRealTimers();
        }
    });

    it('VR-10: 조회 결과가 없으면 loading 이후 empty 상태를 표시한다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const command_adapter = new FakeUiCommandAdapter();
        command_adapter.trade_history = [];
        const actor = create_feature_test_actor(create_trade_history_machine, command_adapter);

        // ENTER_TRADE_HISTORY 입력을 전달해 해당 전이를 실행한다.
        actor.start();
        actor.send({ type: 'ENTER_TRADE_HISTORY' });

        expect(actor.getSnapshot().matches('loading')).toBe(true);  // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.

        // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
        await wait_for_history_settlement();

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().matches('empty')).toBe(true);
        expect(actor.getSnapshot().context.records).toEqual([]);

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });

    it('TD2/TD3 오류 분기: repository 실패를 failed로 표시하고 refresh retry로 복구한다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const command_adapter = new FakeUiCommandAdapter();
        command_adapter.queue_failure('load_trade_history', new Error('history unavailable'));
        const actor = create_feature_test_actor(create_trade_history_machine, command_adapter);

        // ENTER_TRADE_HISTORY 입력을 전달해 해당 전이를 실행한다.
        actor.start();
        actor.send({ type: 'ENTER_TRADE_HISTORY' });
        await wait_for_history_settlement();

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().matches('failed')).toBe(true);
        expect(actor.getSnapshot().context.error?.message).toBe('history unavailable');

        // REFRESH_TRADE_HISTORY 입력을 전달해 해당 전이를 실행한다.
        actor.send({ type: 'REFRESH_TRADE_HISTORY' });

        expect(actor.getSnapshot().matches('loading')).toBe(true);  // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.

        // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
        await wait_for_history_settlement();

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().matches('ready')).toBe(true);
        expect(actor.getSnapshot().context.error).toBeNull();

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });

    it('page-size 또는 query 실패 뒤 filter를 좁혀 새로운 결합 query로 복구한다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const command_adapter = new FakeUiCommandAdapter();
        command_adapter.queue_failure('load_trade_history', new Error('too many rows'));
        const actor = create_feature_test_actor(create_trade_history_machine, command_adapter);

        // ENTER_TRADE_HISTORY 입력을 전달해 해당 전이를 실행한다.
        actor.start();
        actor.send({ type: 'ENTER_TRADE_HISTORY' });
        await wait_for_history_settlement();
        expect(actor.getSnapshot().matches('failed')).toBe(true);  // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.

        // 실패한 ALL 계열을 그대로 반복하지 않고 사용자가 기간을 좁히는 복구 경로를 허용한다.
        actor.send({ type: 'SELECT_DISPLAY_WEEKLY_HISTORY' });
        await wait_for_history_settlement();

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(command_adapter.command_records.at(-1)).toEqual({
            name: 'load_trade_history',
            payload: { period: 'last7days', side: 'all' },
        });
        expect(actor.getSnapshot().matches('ready')).toBe(true);

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });

    it('조회 중 ORDER_EXECUTED가 오면 첫 결과를 버리고 현재 query를 후속 refresh한다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const command_adapter = new DeferredTradeHistoryAdapter();
        const summaries: Array<TradeHistoryDetails['summary']> = [];
        const actor = create_feature_test_actor(create_trade_history_machine, command_adapter, {
            on_details_loaded: (summary) => summaries.push(summary),
        });

        // ENTER_TRADE_HISTORY → ORDER_EXECUTION_RECEIVED 입력을 전달해 해당 전이를 실행한다.
        actor.start();
        actor.send({ type: 'ENTER_TRADE_HISTORY' });
        await wait_for_history_settlement();
        actor.send({ type: 'ORDER_EXECUTION_RECEIVED' });
        command_adapter.pending_requests[0]!.resolve({
            records: [TRADE_RECORD_FIXTURES[0]!],
            summary: command_adapter.trade_history_summary,
        });
        await wait_for_history_settlement();

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().matches('loading')).toBe(true);
        expect(command_adapter.queries).toEqual([
            { period: 'today', side: 'all' },
            { period: 'today', side: 'all' },
        ]);
        expect(actor.getSnapshot().context.records).toEqual([]);
        expect(summaries).toEqual([]);

        // 대기 중인 작업의 성공·실패를 제어해 완료 순서를 재현한다.
        command_adapter.pending_requests[1]!.resolve({
            records: TRADE_RECORD_FIXTURES,
            summary: command_adapter.trade_history_summary,
        });
        await wait_for_history_settlement();

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().matches('ready')).toBe(true);
        expect(actor.getSnapshot().context.records).toEqual(TRADE_RECORD_FIXTURES);
        expect(summaries).toEqual([command_adapter.trade_history_summary]);

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });

    it('조회 중 full resync는 오래된 invoke를 취소하고 같은 current query를 즉시 다시 읽는다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const command_adapter = new DeferredTradeHistoryAdapter();
        const actor = create_feature_test_actor(create_trade_history_machine, command_adapter);

        // ENTER_TRADE_HISTORY → TRADE_HISTORY_RESYNCHRONIZED 입력을 전달해 해당 전이를 실행한다.
        actor.start();
        actor.send({ type: 'ENTER_TRADE_HISTORY' });
        await wait_for_history_settlement();
        actor.send({
            type: 'TRADE_HISTORY_RESYNCHRONIZED',
            symbol: 'ETH/USDT',
        });
        await wait_for_history_settlement();

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(command_adapter.queries).toEqual([
            { period: 'today', side: 'all' },
            { period: 'today', side: 'all' },
        ]);

        // 대기 중인 작업의 성공·실패를 제어해 완료 순서를 재현한다.
        command_adapter.pending_requests[0]!.resolve({
            records: [TRADE_RECORD_FIXTURES[0]!],
            summary: command_adapter.trade_history_summary,
        });
        command_adapter.pending_requests[1]!.resolve({
            records: [TRADE_RECORD_FIXTURES[1]!],
            summary: command_adapter.trade_history_summary,
        });
        await wait_for_history_settlement();

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(actor.getSnapshot().context.symbol).toBe('ETH/USDT');
        expect(actor.getSnapshot().context.records).toEqual([TRADE_RECORD_FIXTURES[1]]);

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });

    it('loading 중 LEAVE는 invoke를 abort하고 재진입 결과와 오래된 요청이 경쟁하지 않게 한다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const command_adapter = new DeferredTradeHistoryAdapter();
        const actor = create_feature_test_actor(create_trade_history_machine, command_adapter);

        // ENTER_TRADE_HISTORY 입력을 전달해 해당 전이를 실행한다.
        actor.start();
        actor.send({ type: 'ENTER_TRADE_HISTORY' });
        await wait_for_history_settlement();
        const abandoned_request = command_adapter.pending_requests[0]!;

        expect(abandoned_request.signal?.aborted).toBe(false);  // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.

        // LEAVE_TRADE_HISTORY 입력을 전달해 해당 전이를 실행한다.
        actor.send({ type: 'LEAVE_TRADE_HISTORY' });

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().matches('idle')).toBe(true);
        expect(abandoned_request.signal?.aborted).toBe(true);

        // 재진입은 새 signal과 새 request를 만들고 이전 요청의 늦은 완료를 적용하지 않는다.
        actor.send({ type: 'ENTER_TRADE_HISTORY' });
        await wait_for_history_settlement();
        const current_request = command_adapter.pending_requests[1]!;

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(current_request.signal).not.toBe(abandoned_request.signal);
        expect(current_request.signal?.aborted).toBe(false);

        // 대기 중인 작업의 성공·실패를 제어해 완료 순서를 재현한다.
        abandoned_request.resolve({
            records: [TRADE_RECORD_FIXTURES[0]!],
            summary: command_adapter.trade_history_summary,
        });
        await wait_for_history_settlement();

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().matches('loading')).toBe(true);
        expect(actor.getSnapshot().context.records).toEqual([]);

        // 대기 중인 작업의 성공·실패를 제어해 완료 순서를 재현한다.
        current_request.resolve({
            records: [TRADE_RECORD_FIXTURES[1]!],
            summary: command_adapter.trade_history_summary,
        });
        await wait_for_history_settlement();

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().matches('ready')).toBe(true);
        expect(actor.getSnapshot().context.records).toEqual([TRADE_RECORD_FIXTURES[1]]);

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });
});
