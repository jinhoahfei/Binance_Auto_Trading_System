import { create_feature_test_actor } from '../../../shared/testing/createFeatureTestController';
import { describe, expect, it, vi } from 'vitest';

import { BackendAdapterError } from '../../../shared/api';
import { FakeUiCommandAdapter } from '../../../shared/testing';
import { create_app_exit_machine } from './appExitMachine';


/**
 * 함수 이름: wait_for_exit_actor_settlement()
 * 기능: app exit의 invoked Promise와 후속 XState 전이가 반영될 때까지 기다린다.
 * 인자: 없음
 * 반환값: 다음 event loop turn에서 완료되는 Promise
 * 작성 날짜: 2026/08/24
 */
async function wait_for_exit_actor_settlement(): Promise<void> {
    await new Promise<void>((resolve) => {
        setTimeout(resolve, 0);
    });
}

describe('appExitMachine Phase 12 lifecycle', () => {
    it('포지션이 없어도 확인 뒤 adapter 안전 종료를 거쳐야 final이 된다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const command_adapter = new FakeUiCommandAdapter();
        const actor = create_feature_test_actor(create_app_exit_machine, command_adapter);

        // EXIT_CLICKED → EXIT_CONFIRMED 입력을 전달해 해당 전이를 실행한다.
        actor.start();
        actor.send({ type: 'EXIT_CLICKED', has_open_position: false });
        actor.send({ type: 'EXIT_CONFIRMED' });
        await wait_for_exit_actor_settlement();

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().matches('ui_final_state')).toBe(true);
        expect(command_adapter.command_records).toEqual([
            { name: 'shutdown_application', payload: null },
        ]);

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });

    it('native와 renderer의 중복 close intent는 확인과 shutdown command를 한 번만 만든다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const command_adapter = new FakeUiCommandAdapter();
        const actor = create_feature_test_actor(create_app_exit_machine, command_adapter);

        // EXIT_CLICKED → EXIT_CLICKED 입력을 전달해 해당 전이를 실행한다.
        actor.start();
        actor.send({ type: 'EXIT_CLICKED', has_open_position: false });
        actor.send({ type: 'EXIT_CLICKED', has_open_position: false });
        expect(actor.getSnapshot().matches('exit_confirmation')).toBe(true);  // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.

        // EXIT_CONFIRMED 입력을 전달해 해당 전이를 실행한다.
        actor.send({ type: 'EXIT_CONFIRMED' });
        await wait_for_exit_actor_settlement();

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(command_adapter.command_records).toEqual([
            { name: 'shutdown_application', payload: null },
        ]);

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });

    it.each([true, false])('active/recovered position (%s) passes consent to a single backend operation', async (is_trading) => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const commands = new FakeUiCommandAdapter();
        let finish!: () => void;
        const shutdown = vi.spyOn(commands, 'shutdown_application').mockImplementation(() => new Promise<void>((resolve) => { finish = resolve; }));
        const actor = create_feature_test_actor(create_app_exit_machine, commands); actor.start();
        actor.send({ type: 'EXIT_CLICKED', has_open_position: true, is_trading });
        expect(shutdown).not.toHaveBeenCalled();  // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.

        // FORCE_SELL_EXIT_CONFIRMED 입력을 전달해 해당 전이를 실행한다.
        actor.send({ type: 'FORCE_SELL_EXIT_CONFIRMED' });
        await wait_for_exit_actor_settlement();

        // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
        expect(shutdown).toHaveBeenCalledExactlyOnceWith(true);
        expect(actor.getSnapshot().matches('shutting_down')).toBe(true);

        // TRADING_SESSION_UPDATED → EXIT_CLICKED 입력을 전달해 해당 전이를 실행한다.
        actor.send({ type: 'TRADING_SESSION_UPDATED', version: 99, status: 'terminated', has_open_position: false });
        actor.send({ type: 'EXIT_CLICKED', has_open_position: true });

        // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
        expect(actor.getSnapshot().matches('shutting_down')).toBe(true);
        expect(shutdown).toHaveBeenCalledOnce();
        finish(); await wait_for_exit_actor_settlement();

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().matches('ui_final_state')).toBe(true); actor.stop();
    });

    it('a position discovered by the backend asks for consent before any liquidation', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const commands = new FakeUiCommandAdapter();
        commands.queue_failure('shutdown_application', new BackendAdapterError('SHUTDOWN_LIQUIDATION_CONFIRMATION_REQUIRED', '청산 동의 필요', false));
        const shutdown = vi.spyOn(commands, 'shutdown_application');
        const actor = create_feature_test_actor(create_app_exit_machine, commands); actor.start();
        actor.send({ type: 'EXIT_CLICKED', has_open_position: false });
        actor.send({ type: 'EXIT_CONFIRMED' }); await wait_for_exit_actor_settlement();

        // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
        expect(shutdown).toHaveBeenCalledExactlyOnceWith(false);
        expect(actor.getSnapshot().matches('force_sell_exit_confirmation')).toBe(true);

        // FORCE_SELL_EXIT_CONFIRMED 입력을 전달해 해당 전이를 실행한다.
        actor.send({ type: 'FORCE_SELL_EXIT_CONFIRMED' }); await wait_for_exit_actor_settlement();

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(shutdown).toHaveBeenLastCalledWith(true);
        expect(actor.getSnapshot().matches('ui_final_state')).toBe(true); actor.stop();
    });

    it.each([
        { has_open_position: false },
        { has_open_position: true },
    ] as const)(
        'shutdown 실패 시 일반 종료 확인으로 돌아가 operator 재시도·취소를 유지한다',
        async ({ has_open_position }) => {
            // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
            const command_adapter = new FakeUiCommandAdapter();
            command_adapter.queue_failure(
                'shutdown_application',
                new Error('열린 포지션과 주문 상태를 확인해 주세요.'),
            );
            const actor = create_feature_test_actor(create_app_exit_machine, command_adapter);

            // EXIT_CLICKED 입력을 전달해 해당 전이를 실행한다.
            actor.start();
            actor.send({ type: 'EXIT_CLICKED', has_open_position });
            actor.send(has_open_position
                ? { type: 'FORCE_SELL_EXIT_CONFIRMED' }
                : { type: 'EXIT_CONFIRMED' });
            await wait_for_exit_actor_settlement();
            await wait_for_exit_actor_settlement();

            // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
            expect(actor.getSnapshot().matches('exit_confirmation')).toBe(true);
            expect(actor.getSnapshot().context.error?.message).toContain('열린 포지션');
            expect(actor.getSnapshot().status).not.toBe('done');

            // 화면 또는 실행 수명의 종료를 요청한다.
            actor.stop();
        },
    );

    it('202 뒤 native timeout은 취소로 정상 화면에 복귀하지 않고 wait만 재시도한다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const command_adapter = new FakeUiCommandAdapter();
        command_adapter.queue_failure(
            'shutdown_application',
            new BackendAdapterError(
                'SIDECAR_EXIT_TIMEOUT',
                '열린 포지션: 없음, 미체결 주문: 없음. 종료 상태 확인이 필요합니다.',
                true,
            ),
        );
        const actor = create_feature_test_actor(create_app_exit_machine, command_adapter);

        // EXIT_CLICKED → EXIT_CONFIRMED 입력을 전달해 해당 전이를 실행한다.
        actor.start();
        actor.send({ type: 'EXIT_CLICKED', has_open_position: false });
        actor.send({ type: 'EXIT_CONFIRMED' });
        await wait_for_exit_actor_settlement();

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().matches('shutdown_exit_recovery')).toBe(true);
        expect(actor.getSnapshot().context.error?.message).toContain('미체결 주문: 없음');

        // EXIT_CANCELED 입력을 전달해 해당 전이를 실행한다.
        actor.send({ type: 'EXIT_CANCELED' });
        expect(actor.getSnapshot().matches('shutdown_exit_recovery')).toBe(true);  // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.

        // Operator 재시도는 force sell이나 HTTP shutdown을 machine 수준에서 새로 분기하지 않는다.
        actor.send({ type: 'EXIT_CONFIRMED' });
        await wait_for_exit_actor_settlement();

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().matches('ui_final_state')).toBe(true);
        expect(command_adapter.command_records).toEqual([
            { name: 'shutdown_application', payload: null },
            { name: 'shutdown_application', payload: null },
        ]);

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });

    it('shutdown 응답 유실은 취소로 정상 화면에 복귀하지 않고 same-key 확인만 재시도한다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const command_adapter = new FakeUiCommandAdapter();
        command_adapter.queue_failure(
            'shutdown_application',
            new BackendAdapterError(
                'SHUTDOWN_OUTCOME_AMBIGUOUS',
                '열린 포지션: 없음, 미체결 주문·조정 상태: 확인 필요.',
                true,
            ),
        );
        const actor = create_feature_test_actor(create_app_exit_machine, command_adapter);

        // EXIT_CLICKED → EXIT_CONFIRMED 입력을 전달해 해당 전이를 실행한다.
        actor.start();
        actor.send({ type: 'EXIT_CLICKED', has_open_position: false });
        actor.send({ type: 'EXIT_CONFIRMED' });
        await wait_for_exit_actor_settlement();

        expect(actor.getSnapshot().matches('shutdown_outcome_recovery')).toBe(true);  // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.

        // EXIT_CANCELED 입력을 전달해 해당 전이를 실행한다.
        actor.send({ type: 'EXIT_CANCELED' });
        expect(actor.getSnapshot().matches('shutdown_outcome_recovery')).toBe(true);  // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.

        // Adapter가 보존한 같은 idempotency key replay만 command port에서 다시 수행한다.
        actor.send({ type: 'EXIT_CONFIRMED' });
        await wait_for_exit_actor_settlement();

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().matches('ui_final_state')).toBe(true);
        expect(command_adapter.command_records).toEqual([
            { name: 'shutdown_application', payload: null },
            { name: 'shutdown_application', payload: null },
        ]);

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });

    it('native timeout 뒤 늦은 clean exit event가 stale recovery 창을 final로 닫는다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const command_adapter = new FakeUiCommandAdapter();
        command_adapter.queue_failure(
            'shutdown_application',
            new BackendAdapterError(
                'SIDECAR_EXIT_TIMEOUT',
                '종료 상태 확인이 필요합니다.',
                true,
            ),
        );
        const actor = create_feature_test_actor(create_app_exit_machine, command_adapter);

        // EXIT_CLICKED → EXIT_CONFIRMED 입력을 전달해 해당 전이를 실행한다.
        actor.start();
        actor.send({ type: 'EXIT_CLICKED', has_open_position: false });
        actor.send({ type: 'EXIT_CONFIRMED' });
        await wait_for_exit_actor_settlement();
        expect(actor.getSnapshot().matches('shutdown_exit_recovery')).toBe(true);  // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.

        // SIDECAR_EXITED 입력을 전달해 해당 전이를 실행한다.
        actor.send({ type: 'SIDECAR_EXITED' });

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().matches('ui_final_state')).toBe(true);
        expect(command_adapter.command_records).toEqual([
            { name: 'shutdown_application', payload: null },
        ]);

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });

    it('sidecar crash는 backend command 없이 offline recovery 확인 뒤 final로 간다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const command_adapter = new FakeUiCommandAdapter();
        const actor = create_feature_test_actor(create_app_exit_machine, command_adapter);

        // SIDECAR_EXITED_ABNORMALLY 입력을 전달해 해당 전이를 실행한다.
        actor.start();
        actor.send({ type: 'SIDECAR_EXITED_ABNORMALLY' });

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().matches('sidecar_exit_failure')).toBe(true);
        expect(actor.getSnapshot().context.error?.code).toBe('SIDECAR_EXITED_ABNORMALLY');

        // Dead child에서 다시 들어온 close intent는 shutdown command를 생성하지 않는다.
        actor.send({ type: 'EXIT_CLICKED', has_open_position: true });
        expect(actor.getSnapshot().matches('sidecar_exit_failure')).toBe(true);  // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.

        // EXIT_CONFIRMED 입력을 전달해 해당 전이를 실행한다.
        actor.send({ type: 'EXIT_CONFIRMED' });

        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(actor.getSnapshot().matches('ui_final_state')).toBe(true);
        expect(command_adapter.command_records).toEqual([]);

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });
});
