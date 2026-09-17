import { earned_balance_fixture } from '../../../shared/api/balanceReconciliationFixture';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { AssetCard } from './AssetCard';
import { StrategyCard } from './StrategyCard';

describe('Account summary live currency rendering', () => {
    it('shows exact Earn principal, rewards and verified spot quantity', () => {
        // 준비한 의존성을 주입해 화면 또는 hook을 실행한다.
        render(<AssetCard ethAmount="0.0001" ethValue="0.23 USDT" krwValue="-" profitLoss="-" totalValue="-"
            balanceReconciliation={earned_balance_fixture} />);
        expect(screen.getByText('ETH · 대조 내역')).toBeInTheDocument();  // 화면의 표시 내용과 입력 가능 상태를 검증한다.

        // 사용자 조작을 수행하고 그에 따른 비동기 반영을 기다린다.
        fireEvent.click(screen.getByRole('button', { name: 'ETH · 대조 내역' }));
        const note = screen.getByRole('note');

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(note).toHaveTextContent('잔여 원금 0.00009600');
        expect(note).toHaveTextContent('Earn 보상 0.00000001');
        expect(note).toHaveTextContent('거래소 현물 0.00009601');
    });
    it('negative USDT 성과에 임의 plus 부호를 붙이지 않는다', () => {
        // 준비한 의존성을 주입해 화면 또는 hook을 실행한다.
        render(
            <StrategyCard
                appliedState="not_started"
                profitAmount="-0.40 USDT"
                profitRate="-0.20%"
                status="매매 시작 전"
                statusTone="neutral"
            />,
        );

        // 화면의 표시 내용과 입력 가능 상태를 검증한다.
        expect(screen.getByText('(-0.40 USDT)')).toBeInTheDocument();
        expect(screen.queryByText(/\+\s*-0\.40 USDT/u)).not.toBeInTheDocument();
    });

    it('live account는 KRW로 가장하지 않고 quote asset과 unavailable total/PnL을 표시한다', () => {
        // 준비한 의존성을 주입해 화면 또는 hook을 실행한다.
        render(
            <AssetCard
                ethAmount="1.75"
                ethValue="7,562.625000 USDT"
                krwValue="-"
                profitLoss="-"
                quoteAsset="USDT"
                quoteValue="120.00 USDT"
                totalValue="-"
            />,
        );

        const card = screen.getByRole('article', { name: '보유 자산' });

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(card).toHaveTextContent('USDT');
        expect(card).toHaveTextContent('120.00 USDT');
        expect(card).not.toHaveTextContent('KRW');
        expect(card).not.toHaveTextContent('₩');
    });
});
