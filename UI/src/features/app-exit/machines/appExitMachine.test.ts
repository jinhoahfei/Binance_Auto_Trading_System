import { createActor } from 'xstate';
import { describe, expect, it } from 'vitest';

import { BackendAdapterError } from '../../../shared/api';
import type { TradingCommandReceipt } from '../../../shared/ports';
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
        const command_adapter = new FakeUiCommandAdapter();
        const actor = createActor(create_app_exit_machine(command_adapter));

        actor.start();
        actor.send({ type: 'EXIT_CLICKED', has_open_position: false });
        actor.send({ type: 'EXIT_CONFIRMED' });
        await wait_for_exit_actor_settlement();

        expect(actor.getSnapshot().matches('ui_final_state')).toBe(true);
        expect(command_adapter.command_records).toEqual([
            { name: 'shutdown_application', payload: null },
        ]);
        actor.stop();
    });

    it('native와 renderer의 중복 close intent는 확인과 shutdown command를 한 번만 만든다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        const actor = createActor(create_app_exit_machine(command_adapter));

        actor.start();
        actor.send({ type: 'EXIT_CLICKED', has_open_position: false });
        actor.send({ type: 'EXIT_CLICKED', has_open_position: false });
        expect(actor.getSnapshot().matches('exit_confirmation')).toBe(true);

        actor.send({ type: 'EXIT_CONFIRMED' });
        await wait_for_exit_actor_settlement();
        expect(command_adapter.command_records).toEqual([
            { name: 'shutdown_application', payload: null },
        ]);
        actor.stop();
    });

    it('포지션 보유 확인은 force sell 결과 뒤 shutdown과 final 순서를 지킨다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        const actor = createActor(create_app_exit_machine(command_adapter));

        actor.start();
        actor.send({ type: 'EXIT_CLICKED', has_open_position: true });
        actor.send({ type: 'FORCE_SELL_EXIT_CONFIRMED' });
        await wait_for_exit_actor_settlement();
        await wait_for_exit_actor_settlement();

        expect(actor.getSnapshot().matches('ui_final_state')).toBe(true);
        expect(command_adapter.command_records).toEqual([
            { name: 'force_sell_and_stop', payload: null },
            { name: 'shutdown_application', payload: null },
        ]);
        actor.stop();
    });

    it('재시작 복구 포지션 종료는 일반 stop 대신 recovery liquidation을 사용한다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        const actor = createActor(create_app_exit_machine(command_adapter));

        // NOT_STARTED와 열린 Position 조합을 명시해 정상 실행 세션의 stop과 구분한다.
        actor.start();
        actor.send({
            type: 'EXIT_CLICKED',
            has_open_position: true,
            is_trading: false,
        });
        actor.send({ type: 'FORCE_SELL_EXIT_CONFIRMED' });
        await wait_for_exit_actor_settlement();
        await wait_for_exit_actor_settlement();

        expect(actor.getSnapshot().matches('ui_final_state')).toBe(true);
        expect(command_adapter.command_records).toEqual([
            { name: 'liquidate_recovered_position', payload: null },
            { name: 'shutdown_application', payload: null },
        ]);
        actor.stop();
    });

    it('복구 청산 STOPPING은 terminal event 전 shutdown하지 않고 확인 뒤 정확히 한 번 종료한다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        command_adapter.recovered_position_liquidation_receipt = {
            status: 'stopping',
            session_id: '62c511b2-ea5c-43ac-bc36-e96eb39c85aa',
            version: 3,
        };
        const actor = createActor(create_app_exit_machine(command_adapter));

        actor.start();
        actor.send({
            type: 'EXIT_CLICKED',
            has_open_position: true,
            is_trading: false,
        });
        actor.send({ type: 'FORCE_SELL_EXIT_CONFIRMED' });
        await wait_for_exit_actor_settlement();

        // STOPPING receipt만으로 Position을 지우거나 backend shutdown을 시작하지 않는다.
        expect(actor.getSnapshot().matches('awaiting_liquidation_terminal')).toBe(true);
        expect(actor.getSnapshot().context).toMatchObject({
            had_open_position: true,
            had_recovered_position: true,
        });
        expect(command_adapter.command_records).toEqual([
            { name: 'liquidate_recovered_position', payload: null },
        ]);

        // TERMINATED라도 authoritative Position이 남아 있으면 shutdown barrier를 열지 않는다.
        actor.send({
            type: 'TRADING_SESSION_UPDATED',
            version: 4,
            status: 'terminated',
            has_open_position: true,
        });
        expect(actor.getSnapshot().matches('awaiting_liquidation_terminal')).toBe(true);
        expect(command_adapter.command_records).toHaveLength(1);

        actor.send({
            type: 'TRADING_SESSION_UPDATED',
            version: 5,
            status: 'terminated',
            has_open_position: false,
        });
        await wait_for_exit_actor_settlement();

        expect(actor.getSnapshot().matches('ui_final_state')).toBe(true);
        expect(command_adapter.command_records).toEqual([
            { name: 'liquidate_recovered_position', payload: null },
            { name: 'shutdown_application', payload: null },
        ]);
        actor.stop();
    });

    it('복구 terminal event가 STOPPING receipt보다 먼저 와도 latch 뒤 shutdown을 한 번 수행한다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        let resolve_liquidation!: (receipt: TradingCommandReceipt) => void;
        command_adapter.recovered_position_liquidation_promise = new Promise((resolve) => {
            resolve_liquidation = resolve;
        });
        const actor = createActor(create_app_exit_machine(command_adapter));

        actor.start();
        actor.send({
            type: 'EXIT_CLICKED',
            has_open_position: true,
            is_trading: false,
        });
        actor.send({ type: 'FORCE_SELL_EXIT_CONFIRMED' });
        await wait_for_exit_actor_settlement();

        // HTTP가 대기 중이어도 더 최신 terminal lifecycle을 force_selling context에 보존한다.
        actor.send({
            type: 'TRADING_SESSION_UPDATED',
            version: 4,
            status: 'terminated',
            has_open_position: false,
        });
        resolve_liquidation({
            status: 'stopping',
            session_id: '62c511b2-ea5c-43ac-bc36-e96eb39c85aa',
            version: 3,
        });
        await wait_for_exit_actor_settlement();
        await wait_for_exit_actor_settlement();

        expect(actor.getSnapshot().matches('ui_final_state')).toBe(true);
        expect(command_adapter.command_records).toEqual([
            { name: 'liquidate_recovered_position', payload: null },
            { name: 'shutdown_application', payload: null },
        ]);
        actor.stop();
    });

    it('복구 reconciliation event가 STOPPING receipt보다 먼저 오면 상태를 보존하고 종료를 차단한다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        let resolve_liquidation!: (receipt: TradingCommandReceipt) => void;
        command_adapter.recovered_position_liquidation_promise = new Promise((resolve) => {
            resolve_liquidation = resolve;
        });
        const actor = createActor(create_app_exit_machine(command_adapter));

        actor.start();
        actor.send({
            type: 'EXIT_CLICKED',
            has_open_position: true,
            is_trading: false,
        });
        actor.send({ type: 'FORCE_SELL_EXIT_CONFIRMED' });
        await wait_for_exit_actor_settlement();

        // 더 최신 reconciliation event가 도착하면 뒤늦은 STOPPING receipt가 이를 덮지 못한다.
        actor.send({
            type: 'TRADING_SESSION_UPDATED',
            version: 4,
            status: 'reconciliation_required',
            has_open_position: true,
        });
        resolve_liquidation({
            status: 'stopping',
            session_id: '62c511b2-ea5c-43ac-bc36-e96eb39c85aa',
            version: 3,
        });
        await wait_for_exit_actor_settlement();

        expect(actor.getSnapshot().matches('force_sell_exit_confirmation')).toBe(true);
        expect(actor.getSnapshot().context).toMatchObject({
            had_open_position: true,
            had_recovered_position: true,
            error: {
                code: 'EXIT_LIQUIDATION_RECONCILIATION_REQUIRED',
            },
        });
        expect(command_adapter.command_records).toEqual([
            { name: 'liquidate_recovered_position', payload: null },
        ]);
        actor.stop();
    });

    it('복구 청산 RECONCILIATION_REQUIRED는 Position 상태와 창을 유지하고 shutdown을 차단한다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        command_adapter.recovered_position_liquidation_receipt = {
            status: 'reconciliation_required',
            session_id: '62c511b2-ea5c-43ac-bc36-e96eb39c85aa',
            version: 3,
        };
        const actor = createActor(create_app_exit_machine(command_adapter));

        actor.start();
        actor.send({
            type: 'EXIT_CLICKED',
            has_open_position: true,
            is_trading: false,
        });
        actor.send({ type: 'FORCE_SELL_EXIT_CONFIRMED' });
        await wait_for_exit_actor_settlement();

        // 조정 필요 receipt는 오류 확인 상태로 돌아가고 recovered Position 표식을 보존한다.
        expect(actor.getSnapshot().matches('force_sell_exit_confirmation')).toBe(true);
        expect(actor.getSnapshot().context).toMatchObject({
            had_open_position: true,
            had_recovered_position: true,
            error: {
                code: 'EXIT_LIQUIDATION_RECONCILIATION_REQUIRED',
            },
        });
        expect(command_adapter.command_records).toEqual([
            { name: 'liquidate_recovered_position', payload: null },
        ]);
        actor.stop();
    });

    it.each([
        { has_open_position: false },
        { has_open_position: true },
    ] as const)(
        'shutdown 실패 시 일반 종료 확인으로 돌아가 operator 재시도·취소를 유지한다',
        async ({ has_open_position }) => {
            const command_adapter = new FakeUiCommandAdapter();
            command_adapter.queue_failure(
                'shutdown_application',
                new Error('열린 포지션과 주문 상태를 확인해 주세요.'),
            );
            const actor = createActor(create_app_exit_machine(command_adapter));

            actor.start();
            actor.send({ type: 'EXIT_CLICKED', has_open_position });
            actor.send(has_open_position
                ? { type: 'FORCE_SELL_EXIT_CONFIRMED' }
                : { type: 'EXIT_CONFIRMED' });
            await wait_for_exit_actor_settlement();
            await wait_for_exit_actor_settlement();

            expect(actor.getSnapshot().matches('exit_confirmation')).toBe(true);
            expect(actor.getSnapshot().context.error?.message).toContain('열린 포지션');
            expect(actor.getSnapshot().status).not.toBe('done');
            actor.stop();
        },
    );

    it('강제 매도 뒤 202 이전 shutdown 오류 재시도는 force sell을 반복하지 않는다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        command_adapter.queue_failure(
            'shutdown_application',
            new Error('백엔드 안전 종료가 아직 완료되지 않았습니다.'),
        );
        const actor = createActor(create_app_exit_machine(command_adapter));

        actor.start();
        actor.send({ type: 'EXIT_CLICKED', has_open_position: true });
        actor.send({ type: 'FORCE_SELL_EXIT_CONFIRMED' });
        await wait_for_exit_actor_settlement();
        await wait_for_exit_actor_settlement();

        // 첫 종료 실패 뒤에는 일반 확인에서 native exit 대기를 다시 시도한다.
        expect(actor.getSnapshot().matches('exit_confirmation')).toBe(true);
        actor.send({ type: 'EXIT_CONFIRMED' });
        await wait_for_exit_actor_settlement();

        expect(actor.getSnapshot().matches('ui_final_state')).toBe(true);
        expect(command_adapter.command_records).toEqual([
            { name: 'force_sell_and_stop', payload: null },
            { name: 'shutdown_application', payload: null },
            { name: 'shutdown_application', payload: null },
        ]);
        actor.stop();
    });

    it('202 뒤 native timeout은 취소로 정상 화면에 복귀하지 않고 wait만 재시도한다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        command_adapter.queue_failure(
            'shutdown_application',
            new BackendAdapterError(
                'SIDECAR_EXIT_TIMEOUT',
                '열린 포지션: 없음, 미체결 주문: 없음. 종료 상태 확인이 필요합니다.',
                true,
            ),
        );
        const actor = createActor(create_app_exit_machine(command_adapter));

        actor.start();
        actor.send({ type: 'EXIT_CLICKED', has_open_position: false });
        actor.send({ type: 'EXIT_CONFIRMED' });
        await wait_for_exit_actor_settlement();

        expect(actor.getSnapshot().matches('shutdown_exit_recovery')).toBe(true);
        expect(actor.getSnapshot().context.error?.message).toContain('미체결 주문: 없음');
        actor.send({ type: 'EXIT_CANCELED' });
        expect(actor.getSnapshot().matches('shutdown_exit_recovery')).toBe(true);

        // Operator 재시도는 force sell이나 HTTP shutdown을 machine 수준에서 새로 분기하지 않는다.
        actor.send({ type: 'EXIT_CONFIRMED' });
        await wait_for_exit_actor_settlement();
        expect(actor.getSnapshot().matches('ui_final_state')).toBe(true);
        expect(command_adapter.command_records).toEqual([
            { name: 'shutdown_application', payload: null },
            { name: 'shutdown_application', payload: null },
        ]);
        actor.stop();
    });

    it('shutdown 응답 유실은 취소로 정상 화면에 복귀하지 않고 same-key 확인만 재시도한다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        command_adapter.queue_failure(
            'shutdown_application',
            new BackendAdapterError(
                'SHUTDOWN_OUTCOME_AMBIGUOUS',
                '열린 포지션: 없음, 미체결 주문·조정 상태: 확인 필요.',
                true,
            ),
        );
        const actor = createActor(create_app_exit_machine(command_adapter));

        actor.start();
        actor.send({ type: 'EXIT_CLICKED', has_open_position: false });
        actor.send({ type: 'EXIT_CONFIRMED' });
        await wait_for_exit_actor_settlement();

        expect(actor.getSnapshot().matches('shutdown_outcome_recovery')).toBe(true);
        actor.send({ type: 'EXIT_CANCELED' });
        expect(actor.getSnapshot().matches('shutdown_outcome_recovery')).toBe(true);

        // Adapter가 보존한 같은 idempotency key replay만 command port에서 다시 수행한다.
        actor.send({ type: 'EXIT_CONFIRMED' });
        await wait_for_exit_actor_settlement();
        expect(actor.getSnapshot().matches('ui_final_state')).toBe(true);
        expect(command_adapter.command_records).toEqual([
            { name: 'shutdown_application', payload: null },
            { name: 'shutdown_application', payload: null },
        ]);
        actor.stop();
    });

    it('native timeout 뒤 늦은 clean exit event가 stale recovery 창을 final로 닫는다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        command_adapter.queue_failure(
            'shutdown_application',
            new BackendAdapterError(
                'SIDECAR_EXIT_TIMEOUT',
                '종료 상태 확인이 필요합니다.',
                true,
            ),
        );
        const actor = createActor(create_app_exit_machine(command_adapter));

        actor.start();
        actor.send({ type: 'EXIT_CLICKED', has_open_position: false });
        actor.send({ type: 'EXIT_CONFIRMED' });
        await wait_for_exit_actor_settlement();
        expect(actor.getSnapshot().matches('shutdown_exit_recovery')).toBe(true);

        actor.send({ type: 'SIDECAR_EXITED' });
        expect(actor.getSnapshot().matches('ui_final_state')).toBe(true);
        expect(command_adapter.command_records).toEqual([
            { name: 'shutdown_application', payload: null },
        ]);
        actor.stop();
    });

    it('sidecar crash는 backend command 없이 offline recovery 확인 뒤 final로 간다', () => {
        const command_adapter = new FakeUiCommandAdapter();
        const actor = createActor(create_app_exit_machine(command_adapter));

        actor.start();
        actor.send({ type: 'SIDECAR_EXITED_ABNORMALLY' });
        expect(actor.getSnapshot().matches('sidecar_exit_failure')).toBe(true);
        expect(actor.getSnapshot().context.error?.code).toBe('SIDECAR_EXITED_ABNORMALLY');

        // Dead child에서 다시 들어온 close intent는 shutdown command를 생성하지 않는다.
        actor.send({ type: 'EXIT_CLICKED', has_open_position: true });
        expect(actor.getSnapshot().matches('sidecar_exit_failure')).toBe(true);
        actor.send({ type: 'EXIT_CONFIRMED' });
        expect(actor.getSnapshot().matches('ui_final_state')).toBe(true);
        expect(command_adapter.command_records).toEqual([]);
        actor.stop();
    });
});
