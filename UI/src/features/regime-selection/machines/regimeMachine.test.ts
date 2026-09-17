import { create_feature_test_actor } from '../../../shared/testing/createFeatureTestController';
import { describe, expect, it } from 'vitest';
import { FakeUiCommandAdapter } from '../../../shared/testing';
import { create_regime_machine } from './regimeMachine';

/**
 * 함수 이름: wait_for_actor_settlement()
 * 기능: REGIME 적용 Promise 결과가 actor snapshot에 반영될 때까지 기다린다.
 * 인자: 없음
 * 반환값: 다음 event loop turn에서 완료되는 Promise
 * 작성 날짜: 2026/08/12
 */
async function wait_for_actor_settlement(): Promise<void> {
    await new Promise<void>((resolve) => {
        setTimeout(resolve, 0);
    });
}

describe('regimeMachine', () => {
    it('CR-03/VR-14: type 클릭만으로 적용하지 않고 취소하면 기존 type을 보존한다', () => {
        const command_adapter = new FakeUiCommandAdapter();
        const actor = create_feature_test_actor(create_regime_machine, command_adapter, {
            applied_regime: 'type1',
        });

        actor.start();
        actor.send({ type: 'TYPE_CLICKED', regime: 'type4' });

        expect(actor.getSnapshot().matches('type_change_confirmation')).toBe(true);
        expect(actor.getSnapshot().context.applied_regime).toBe('type1');
        expect(actor.getSnapshot().context.candidate_regime).toBe('type4');

        actor.send({ type: 'CANCEL_TYPE_CHANGE' });
        expect(actor.getSnapshot().context.applied_regime).toBe('type1');
        expect(actor.getSnapshot().context.candidate_regime).toBeNull();
        expect(command_adapter.command_records).toHaveLength(0);
        actor.stop();
    });

    it('R2-04/ER-10B: 실행 확인 후 adapter 성공 시 후보 type을 적용한다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        const actor = create_feature_test_actor(create_regime_machine, command_adapter);

        actor.start();
        actor.send({ type: 'TYPE_CLICKED', regime: 'type3' });
        actor.send({ type: 'CONFIRM_TYPE_CHANGE' });
        await wait_for_actor_settlement();

        expect(actor.getSnapshot().matches('type_selection')).toBe(true);
        expect(actor.getSnapshot().context.applied_regime).toBe('type3');
        expect(command_adapter.command_records).toEqual([
            { name: 'apply_regime', payload: { regime_type: 'type3' } },
        ]);
        actor.stop();
    });

    it('Phase 5 unavailable adapter의 typed code를 generic code로 숨기지 않는다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        const unavailable_error = Object.assign(
            new Error('This feature is not available in the current application phase.'),
            { code: 'FEATURE_NOT_AVAILABLE' },
        );
        command_adapter.queue_failure('apply_regime', unavailable_error);
        const actor = create_feature_test_actor(create_regime_machine, command_adapter);

        actor.start();
        actor.send({ type: 'TYPE_CLICKED', regime: 'type2' });
        actor.send({ type: 'CONFIRM_TYPE_CHANGE' });
        await wait_for_actor_settlement();

        expect(actor.getSnapshot().context.error).toEqual({
            code: 'FEATURE_NOT_AVAILABLE',
            message: 'This feature is not available in the current application phase.',
        });
        actor.stop();
    });
});
