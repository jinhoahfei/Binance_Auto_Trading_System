import { createActor } from 'xstate';
import { describe, expect, it } from 'vitest';
import { create_trade_history_summary_machine } from './tradeHistorySummaryMachine';

describe('tradeHistorySummaryMachine', () => {
    it('D1-02/D2-02/D3-02/D4-02: 상세 요약 region을 서로 독립적으로 갱신한다', () => {
        const actor = createActor(create_trade_history_summary_machine());

        actor.start();
        actor.send({
            type: 'PROFIT_RATE_UPDATED',
            daily_return: { value: '+2.10%', tone: 'positive' },
        });
        actor.send({
            type: 'SELL_ORDER_EXECUTED',
            sell_performance: {
                winRate: '60%',
                completedCount: '3 / 5',
                averageRealizedReturn: '+0.42%',
                totalRealizedPnl: '+₩ 23,000',
                tone: 'positive',
            },
            position: { quantity: '0.20 ETH' },
        });
        actor.send({
            type: 'BUY_ORDER_EXECUTED',
            position: { quantity: '0.35 ETH' },
        });
        actor.send({
            type: 'DAILY_TRADING_FEE_CHANGED',
            fees: {
                amount: '₩ 1,500',
                totalExecutedAmount: '₩ 3,000,000',
                averageSlippage: '0.03%',
            },
        });

        expect(actor.getSnapshot().context.summary.dailyReturn.value).toBe('+2.10%');
        expect(actor.getSnapshot().context.summary.sellPerformance.winRate).toBe('60%');
        expect(actor.getSnapshot().context.summary.position.quantity).toBe('0.35 ETH');
        expect(actor.getSnapshot().context.summary.fees.amount).toBe('₩ 1,500');
        actor.stop();
    });
});
