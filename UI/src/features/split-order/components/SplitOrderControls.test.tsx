import { fireEvent, render, screen } from '@testing-library/react';
import { vi } from 'vitest';
import { SplitOrderControls } from './SplitOrderControls';

describe('SplitOrderControls', () => {
    it('CR-09: 매수 증가 버튼으로 10% 증가 intent를 전달한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const handle_intent = vi.fn();

        // 준비한 의존성을 주입해 화면 또는 hook을 실행한다.
        render(
            <SplitOrderControls
                buyPercentage={40}
                onIntent={handle_intent}
                sellPercentage={40}
            />,
        );

        fireEvent.click(screen.getByRole('button', { name: '분할 매수 10% 증가' }));

        // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
        expect(handle_intent).toHaveBeenCalledWith({
            type: 'SPLIT_PERCENTAGE_REQUESTED',
            side: 'buy',
            percentage: 50,
        });
    });

    it('CR-09: 분할 매도 슬라이더를 연속 이동하면 각 10 단위 값을 전달한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const handle_intent = vi.fn();

        // 준비한 의존성을 주입해 화면 또는 hook을 실행한다.
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

        // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
        expect(handle_intent).toHaveBeenCalledTimes(3);
        expect(handle_intent).toHaveBeenLastCalledWith({
            type: 'SPLIT_PERCENTAGE_REQUESTED',
            side: 'sell',
            percentage: 80,
        });
    });
});
