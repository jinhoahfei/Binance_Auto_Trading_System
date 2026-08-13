import { fireEvent, render, screen } from '@testing-library/react';
import { vi } from 'vitest';
import { SplitOrderControls } from './SplitOrderControls';

describe('SplitOrderControls', () => {
    it('CR-09: 매수 증가 버튼으로 10% 증가 intent를 전달한다', () => {
        const handle_intent = vi.fn();

        render(
            <SplitOrderControls
                buyPercentage={40}
                onIntent={handle_intent}
                sellPercentage={40}
            />,
        );

        fireEvent.click(screen.getByRole('button', { name: '분할 매수 10% 증가' }));
        expect(handle_intent).toHaveBeenCalledWith({
            type: 'SPLIT_PERCENTAGE_REQUESTED',
            side: 'buy',
            percentage: 50,
        });
    });

    it('CR-09: 분할 매도 슬라이더를 연속 이동하면 각 10 단위 값을 전달한다', () => {
        const handle_intent = vi.fn();

        render(
            <SplitOrderControls
                buyPercentage={40}
                onIntent={handle_intent}
                sellPercentage={40}
            />,
        );

        const slider = screen.getByLabelText('분할 매도');

        fireEvent.input(slider, { target: { value: '53' } });
        fireEvent.input(slider, { target: { value: '64' } });
        fireEvent.input(slider, { target: { value: '77' } });

        expect(handle_intent).toHaveBeenCalledTimes(3);
        expect(handle_intent).toHaveBeenLastCalledWith({
            type: 'SPLIT_PERCENTAGE_REQUESTED',
            side: 'sell',
            percentage: 80,
        });
    });
});
