import { createActor } from 'xstate';
import { describe, expect, it } from 'vitest';
import { DEFAULT_TRADING_LOGIC_COVERAGE } from '../../../shared/contracts';
import { FakeUiCommandAdapter } from '../../../shared/testing';
import { create_trading_command_machine } from './tradingCommandMachine';

/**
 * 함수 이름: wait_for_actor_settlement()
 * 기능: invoked Promise actor의 microtask 완료가 snapshot에 반영될 때까지 기다린다.
 * 인자: 없음
 * 반환값: 다음 event loop turn에서 완료되는 Promise
 * 작성 날짜: 2026/08/12
 */
async function wait_for_actor_settlement(): Promise<void> {
    await new Promise<void>((resolve) => {
        setTimeout(resolve, 0);
    });
}

describe('tradingCommandMachine', () => {
    it('U3-03/VR-01: REGIME 미선택 시작은 명령 없이 안내 상태로 전이한다', () => {
        const command_adapter = new FakeUiCommandAdapter();
        const actor = createActor(create_trading_command_machine(command_adapter));

        actor.start();
        actor.send({ type: 'START_BUTTON_CLICKED', regime: null, is_online: true });

        expect(actor.getSnapshot().matches('select_regime_notice')).toBe(true);
        expect(command_adapter.command_records).toHaveLength(0);
        actor.stop();
    });

    it('U3-05: 확인된 온라인 시작은 한 번만 호출하고 실행 상태가 된다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        const actor = createActor(create_trading_command_machine(command_adapter, {
            command_enabled: true,
        }));

        actor.start();
        actor.send({ type: 'START_BUTTON_CLICKED', regime: 'type0', is_online: true });
        actor.send({ type: 'START_CONFIRMED', is_online: true });
        actor.send({ type: 'START_CONFIRMED', is_online: true });
        await wait_for_actor_settlement();

        expect(actor.getSnapshot().matches('running')).toBe(true);
        expect(command_adapter.command_records).toEqual([
            { name: 'start_trading', payload: { regime_type: 'type0' } },
        ]);
        actor.stop();
    });

    it('Phase 6: 미지원 REGIME은 선택을 보존하고 시작 명령을 보내지 않는다', () => {
        const command_adapter = new FakeUiCommandAdapter();
        const actor = createActor(create_trading_command_machine(command_adapter, {
            command_enabled: true,
        }));

        actor.start();
        actor.send({ type: 'START_BUTTON_CLICKED', regime: 'type3', is_online: true });

        expect(actor.getSnapshot().matches('trading_unavailable_notice')).toBe(true);
        expect(actor.getSnapshot().context.selected_regime).toBe('type3');
        expect(actor.getSnapshot().context.unavailable_reason).toBe('unsupported_logic');
        expect(command_adapter.command_records).toHaveLength(0);
        actor.stop();
    });

    it('Phase 6: 지원 REGIME도 command가 비활성화되면 시작 명령을 보내지 않는다', () => {
        const command_adapter = new FakeUiCommandAdapter();
        const actor = createActor(create_trading_command_machine(command_adapter));

        actor.start();
        actor.send({ type: 'START_BUTTON_CLICKED', regime: 'type0', is_online: true });

        expect(actor.getSnapshot().matches('trading_unavailable_notice')).toBe(true);
        expect(actor.getSnapshot().context.unavailable_reason).toBe('command_disabled');
        expect(command_adapter.command_records).toHaveLength(0);
        actor.stop();
    });

    it('Phase 6: 시작 확인 중 resync로 command가 닫히면 stale 확인을 차단한다', () => {
        const command_adapter = new FakeUiCommandAdapter();
        const actor = createActor(create_trading_command_machine(command_adapter, {
            command_enabled: true,
        }));

        actor.start();
        actor.send({ type: 'START_BUTTON_CLICKED', regime: 'type0', is_online: true });
        expect(actor.getSnapshot().matches('start_confirmation')).toBe(true);

        // 확인 modal을 유지한 채 최신 backend gate를 반영해 오래된 확인을 재검증한다.
        actor.send({
            type: 'TRADING_SNAPSHOT_CONTEXT_SYNCHRONIZED',
            selected_regime: 'type0',
            logic_coverage: DEFAULT_TRADING_LOGIC_COVERAGE,
            command_enabled: false,
            is_trading: false,
            has_open_position: false,
            lifecycle_status: 'not_started',
        });
        actor.send({ type: 'START_CONFIRMED', is_online: true });

        expect(actor.getSnapshot().matches('trading_unavailable_notice')).toBe(true);
        expect(actor.getSnapshot().context.unavailable_reason).toBe('command_disabled');
        expect(command_adapter.command_records).toHaveLength(0);
        actor.stop();
    });

    it('U2-03/U2-09: 강제 매도 실패는 확인 상태로 돌아가고 실행 상태를 유지한다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        command_adapter.queue_failure('force_sell_and_stop', new Error('test sell failure'));
        const actor = createActor(create_trading_command_machine(command_adapter));

        actor.start();
        actor.send({ type: 'BACKEND_TRADING_STARTED' });
        actor.send({ type: 'STOP_BUTTON_CLICKED', has_open_position: true });
        actor.send({ type: 'FORCE_SELL_AND_STOP_CONFIRMED' });
        await wait_for_actor_settlement();

        expect(actor.getSnapshot().matches('force_sell_confirmation')).toBe(true);
        expect(actor.getSnapshot().context.is_trading).toBe(true);
        expect(actor.getSnapshot().context.has_open_position).toBe(true);
        expect(actor.getSnapshot().context.error?.message).toBe('test sell failure');
        actor.stop();
    });

    it('U2-03/U2-10: 강제 매도 중지 취소 후에도 backend 포지션 snapshot을 보존한다', () => {
        const command_adapter = new FakeUiCommandAdapter();
        const actor = createActor(create_trading_command_machine(command_adapter));

        actor.start();
        actor.send({ type: 'BACKEND_TRADING_STARTED' });
        actor.send({ type: 'POSITION_UPDATED', has_open_position: true });
        actor.send({ type: 'STOP_BUTTON_CLICKED', has_open_position: true });
        actor.send({ type: 'FORCE_SELL_AND_STOP_CANCELED' });

        expect(actor.getSnapshot().matches('running')).toBe(true);
        expect(actor.getSnapshot().context.has_open_position).toBe(true);
        actor.stop();
    });

    it('U2-08: 강제 매도 후 중지가 성공한 경우에만 포지션 snapshot을 비운다', async () => {
        const command_adapter = new FakeUiCommandAdapter();
        const actor = createActor(create_trading_command_machine(command_adapter));

        actor.start();
        actor.send({ type: 'BACKEND_TRADING_STARTED' });
        actor.send({ type: 'POSITION_UPDATED', has_open_position: true });
        actor.send({ type: 'STOP_BUTTON_CLICKED', has_open_position: true });
        actor.send({ type: 'FORCE_SELL_AND_STOP_CONFIRMED' });
        await wait_for_actor_settlement();

        expect(actor.getSnapshot().matches('stopped')).toBe(true);
        expect(actor.getSnapshot().context.has_open_position).toBe(false);
        actor.stop();
    });

    it.each(['stopping', 'reconciliation_required'] as const)(
        'Phase 7: stop receipt가 %s이면 완료로 오표시하지 않고 authoritative 종료를 기다린다',
        async (stop_status) => {
            const command_adapter = new FakeUiCommandAdapter();
            command_adapter.stop_trading_receipt = {
                status: stop_status,
                session_id: '62c511b2-ea5c-43ac-bc36-e96eb39c85aa',
                version: 3,
            };
            const actor = createActor(create_trading_command_machine(command_adapter));

            actor.start();
            actor.send({ type: 'BACKEND_TRADING_STARTED' });
            actor.send({ type: 'POSITION_UPDATED', has_open_position: true });
            actor.send({ type: 'STOP_BUTTON_CLICKED', has_open_position: true });
            actor.send({ type: 'FORCE_SELL_AND_STOP_CONFIRMED' });
            await wait_for_actor_settlement();

            expect(actor.getSnapshot().matches('awaiting_stop_completion')).toBe(true);
            expect(actor.getSnapshot().context.is_trading).toBe(true);
            expect(actor.getSnapshot().context.has_open_position).toBe(true);

            // 실제 청산 완료 snapshot이 도착한 뒤에만 stopped와 position zero를 적용한다.
            actor.send({
                type: 'TRADING_SNAPSHOT_SYNCHRONIZED',
                selected_regime: 'type0',
                logic_coverage: DEFAULT_TRADING_LOGIC_COVERAGE,
                command_enabled: true,
                is_trading: false,
                has_open_position: false,
                lifecycle_status: 'terminated',
            });

            expect(actor.getSnapshot().matches('stopped')).toBe(true);
            expect(actor.getSnapshot().context.is_trading).toBe(false);
            expect(actor.getSnapshot().context.has_open_position).toBe(false);
            actor.stop();
        },
    );
});
