import { create_feature_test_actor } from '../../../shared/testing/createFeatureTestController';
import { describe, expect, it } from 'vitest';
import { FakeUiCommandAdapter } from '../../../shared/testing';
import { create_split_order_machine } from './splitOrderMachine';


/**
 * 함수 이름: wait_for_actor_settlement()
 * 기능: 분할 주문 저장 Promise의 최신 결과가 actor snapshot에 반영될 때까지 기다린다.
 * 인자: 없음
 * 반환값: 다음 event loop turn에서 완료되는 Promise
 * 작성 날짜: 2026/08/12
 */
async function wait_for_actor_settlement(): Promise<void> {
    await new Promise<void>((resolve) => {
        setTimeout(resolve, 0);
    });
}

describe('splitOrderMachine', () => {
    it('SI-02/SO-02: 저장 중에도 연속 slider 입력의 최신 비율을 즉시 표시하고 저장한다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const command_adapter = new FakeUiCommandAdapter();
        const actor = create_feature_test_actor(create_split_order_machine, command_adapter, {
            scale_in_percentage: 40,
            scale_out_percentage: 40,
        });

        // SCALE_IN_LEVEL_CHANGED → SCALE_IN_LEVEL_CHANGED → SCALE_IN_LEVEL_CHANGED 입력을 전달해 해당 전이를 실행한다.
        actor.start();
        actor.send({ type: 'SCALE_IN_LEVEL_CHANGED', percentage: 50 });
        actor.send({ type: 'SCALE_IN_LEVEL_CHANGED', percentage: 60 });
        actor.send({ type: 'SCALE_IN_LEVEL_CHANGED', percentage: 70 });

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().context.scale_in_percentage).toBe(70);
        expect(actor.getSnapshot().matches('saving')).toBe(true);

        // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
        await wait_for_actor_settlement();

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().matches('ready')).toBe(true);
        expect(actor.getSnapshot().context.scale_in_percentage).toBe(70);
        expect(command_adapter.command_records.at(-1)).toEqual({
            name: 'update_split_order',
            payload: { order_side: 'scale_in', percentage: 70 },
        });

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });
});
