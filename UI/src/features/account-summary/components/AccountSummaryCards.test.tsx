import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { AssetCard } from './AssetCard';
import { StrategyCard } from './StrategyCard';

describe('Account summary live currency rendering', () => {
    it('negative USDT 성과에 임의 plus 부호를 붙이지 않는다', () => {
        render(
            <StrategyCard
                appliedState="not_started"
                profitAmount="-0.40 USDT"
                profitRate="-0.20%"
                status="매매 시작 전"
                statusTone="neutral"
            />,
        );

        expect(screen.getByText('(-0.40 USDT)')).toBeInTheDocument();
        expect(screen.queryByText(/\+\s*-0\.40 USDT/u)).not.toBeInTheDocument();
    });

    it('live account는 KRW로 가장하지 않고 quote asset과 unavailable total/PnL을 표시한다', () => {
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

        expect(card).toHaveTextContent('USDT');
        expect(card).toHaveTextContent('120.00 USDT');
        expect(card).not.toHaveTextContent('KRW');
        expect(card).not.toHaveTextContent('₩');
    });
});
