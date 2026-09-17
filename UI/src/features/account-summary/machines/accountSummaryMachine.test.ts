import { createActor } from 'xstate';
import { describe, expect, it } from 'vitest';
import { create_account_summary_machine } from './accountSummaryMachine';

describe('accountSummaryMachine', () => {
    it('DI1-02/DI2-02: 전략과 자산 snapshot을 서로 독립적으로 갱신한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const actor = createActor(create_account_summary_machine());

        // TRADING_STATUS_UPDATED 입력을 전달해 해당 전이를 실행한다.
        actor.start();
        actor.send({
            type: 'TRADING_STATUS_UPDATED',
            strategy: {
                appliedState: 'ENTRY_WAIT',
                profitAmount: '₩ 15,000',
                profitRate: '+1.25%',
                status: '정상 작동',
                statusTone: 'positive',
            },
        });

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(actor.getSnapshot().context.strategy.appliedState).toBe('ENTRY_WAIT');
        expect(actor.getSnapshot().context.asset.totalValue).toBe('₩ 0');

        // ASSET_SUMMARY_UPDATED 입력을 전달해 해당 전이를 실행한다.
        actor.send({
            type: 'ASSET_SUMMARY_UPDATED',
            asset: {
                ethAmount: '0.42',
                ethValue: '₩ 2,100,000',
                krwValue: '₩ 900,000',
                profitLoss: '+₩ 15,000',
                totalValue: '₩ 3,000,000',
            },
        });

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(actor.getSnapshot().context.asset.ethAmount).toBe('0.42');
        expect(actor.getSnapshot().context.strategy.appliedState).toBe('ENTRY_WAIT');

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();
    });
});

