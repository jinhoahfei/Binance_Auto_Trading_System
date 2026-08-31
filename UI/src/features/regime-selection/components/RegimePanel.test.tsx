import { fireEvent, render, screen } from '@testing-library/react';
import { vi } from 'vitest';
import { RegimeChangeDialog } from './RegimeChangeDialog';
import { RegimePanel } from './RegimePanel';

describe('RegimePanel', () => {
    it('R2-01: 적용 타입이 없으면 어떤 REGIME 버튼도 선택하지 않는다', () => {
        render(
            <RegimePanel
                applied={null}
                metrics={[]}
                recommended="type0"
            />,
        );

        screen.getAllByRole('button').forEach((button) => {
            expect(button).toHaveAttribute('aria-pressed', 'false');
        });
        expect(screen.getAllByText('지원')).toHaveLength(1);
        expect(screen.getAllByText('미지원')).toHaveLength(4);
    });

    it('CR-02: 적용 타입을 표시하고 선택 intent를 전달한다', () => {
        const handle_intent = vi.fn();

        render(
            <RegimePanel
                applied="type0"
                metrics={[
                    { id: 'emaSlope', label: 'EMA 기울기', value: '+0.23', tone: 'positive' },
                    { id: 'ema', label: 'EMA', value: '위 +0.84%', tone: 'positive' },
                    { id: 'swingLow', label: '스윙 저점', value: 'HL', tone: 'negative' },
                    { id: 'swingHigh', label: '스윙 고점', value: 'HH', tone: 'neutral' },
                ]}
                onIntent={handle_intent}
                recommended="type0"
            />,
        );

        expect(screen.getByRole('button', { name: 'type0 횡보 적용 요청' })).toHaveAttribute('aria-pressed', 'true');
        fireEvent.click(screen.getByRole('button', { name: 'type3 약하락 적용 요청' }));
        expect(handle_intent).toHaveBeenCalledWith({
            type: 'REGIME_TYPE_REQUESTED',
            regime: 'type3',
        });
        expect(screen.getByRole('button', { name: 'type3 약하락 적용 요청' }))
            .toHaveAccessibleDescription('미지원');
    });

    /** Communication Case 1 메시지 6의 비활성 사용자 경계를 직접 검증한다. */
    it('test_regime_selection_disabled_does_not_emit_intent: 비활성 선택은 intent를 보내지 않는다', () => {
        const handle_intent = vi.fn();

        render(
            <RegimePanel
                applied="type0"
                disabled
                metrics={[]}
                onIntent={handle_intent}
                recommended="type0"
            />,
        );

        // Disabled native button은 click을 소비하지 않고 기존 적용 REGIME을 그대로 보존한다.
        const requested_button = screen.getByRole('button', { name: 'type3 약하락 적용 요청' });
        fireEvent.click(requested_button);
        expect(requested_button).toBeDisabled();
        expect(handle_intent).not.toHaveBeenCalled();
    });

    it('REGIME 적용 실패 사유를 재시도 확인창에 표시한다', () => {
        render(
            <RegimeChangeDialog
                error="regime unavailable"
                onCancel={vi.fn()}
                onConfirm={vi.fn()}
                open
                regimeKey="type2"
                regimeLabel="강상승"
            />,
        );

        expect(screen.getByRole('alert')).toHaveTextContent('regime unavailable');
    });
});
