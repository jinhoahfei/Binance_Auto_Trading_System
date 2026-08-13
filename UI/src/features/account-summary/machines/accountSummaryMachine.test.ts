import { createActor } from 'xstate';
import { describe, expect, it } from 'vitest';
import { create_account_summary_machine } from './accountSummaryMachine';

describe('accountSummaryMachine', () => {
    it('DI1-02/DI2-02: 전략과 자산 snapshot을 서로 독립적으로 갱신한다', () => {
        const actor = createActor(create_account_summary_machine());

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

        expect(actor.getSnapshot().context.strategy.appliedState).toBe('ENTRY_WAIT');
        expect(actor.getSnapshot().context.asset.totalValue).toBe('₩ 0');

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

        expect(actor.getSnapshot().context.asset.ethAmount).toBe('0.42');
        expect(actor.getSnapshot().context.strategy.appliedState).toBe('ENTRY_WAIT');
        actor.stop();
    });
});

