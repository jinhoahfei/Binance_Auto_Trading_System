import { fireEvent, render, screen } from '@testing-library/react';
import { vi } from 'vitest';
import { TraderPanel } from './TraderPanel';

describe('TraderPanel', () => {
    it('CR-06: 실시간 지표 탭 요청을 controlled intent로 전달한다', () => {
        const handle_intent = vi.fn();

        render(
            <TraderPanel
                activeTab="recent"
                indicatorGroups={[]}
                onIntent={handle_intent}
                orders={[]}
            />,
        );

        fireEvent.click(screen.getByRole('tab', { name: '실시간 지표' }));
        expect(handle_intent).toHaveBeenCalledWith({
            type: 'TRADER_TAB_REQUESTED',
            tab: 'realtime',
        });
    });

    it('Case 3.1: 전체 보기 intent를 전달한다', () => {
        const handle_intent = vi.fn();

        render(
            <TraderPanel
                activeTab="recent"
                indicatorGroups={[]}
                onIntent={handle_intent}
                orders={[]}
            />,
        );

        fireEvent.click(screen.getByRole('button', { name: '전체 보기' }));
        expect(handle_intent).toHaveBeenCalledWith({ type: 'ALL_ORDERS_REQUESTED' });
    });
});
