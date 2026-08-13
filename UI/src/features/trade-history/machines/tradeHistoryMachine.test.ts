import { createActor } from 'xstate';
import { FakeUiCommandAdapter } from '../../../shared/testing';
import { create_trade_history_machine } from './tradeHistoryMachine';

/**
 * 함수 이름: wait_for_history_settlement()
 * 기능: 거래 내역 조회 Promise 결과가 actor snapshot에 반영될 때까지 기다린다.
 * 인자: 없음
 * 반환값: 다음 event loop turn에서 완료되는 Promise
 * 작성 날짜: 2026/08/12
 */
async function wait_for_history_settlement(): Promise<void> {
    await new Promise<void>((resolve) => {
        setTimeout(resolve, 0);
    });
}

describe('tradeHistoryMachine', () => {
    it('TD2/TD3: 기간과 방향 필터를 결합해 조회 결과를 교체한다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        const actor = createActor(create_trade_history_machine(command_adapter));

        actor.start();
        actor.send({ type: 'ENTER_TRADE_HISTORY' });
        await wait_for_history_settlement();

        expect(actor.getSnapshot().matches('ready')).toBe(true);
        expect(actor.getSnapshot().context.records).toHaveLength(2);

        actor.send({ type: 'SELL_TRADE_HISTORY_SELECTED' });
        await wait_for_history_settlement();
        expect(actor.getSnapshot().context.records).toHaveLength(1);
        expect(actor.getSnapshot().context.records[0]?.side).toBe('sell');

        actor.send({ type: 'SELECT_DISPLAY_ALL_HISTORY' });
        await wait_for_history_settlement();
        expect(actor.getSnapshot().context.records).toHaveLength(2);
        actor.stop();
    });

    it('VR-10: 조회 결과가 없으면 empty 상태를 표시한다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        command_adapter.trade_history = [];
        const actor = createActor(create_trade_history_machine(command_adapter));

        actor.start();
        actor.send({ type: 'ENTER_TRADE_HISTORY' });
        await wait_for_history_settlement();

        expect(actor.getSnapshot().matches('empty')).toBe(true);
        expect(actor.getSnapshot().context.records).toEqual([]);
        actor.stop();
    });

    it('TD2/TD3 오류 분기: adapter 실패를 failed 상태와 표시 오류로 보존한다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        command_adapter.queue_failure('load_trade_history', new Error('history unavailable'));
        const actor = createActor(create_trade_history_machine(command_adapter));

        actor.start();
        actor.send({ type: 'ENTER_TRADE_HISTORY' });
        await wait_for_history_settlement();

        expect(actor.getSnapshot().matches('failed')).toBe(true);
        expect(actor.getSnapshot().context.error?.message).toBe('history unavailable');
        actor.stop();
    });
});
