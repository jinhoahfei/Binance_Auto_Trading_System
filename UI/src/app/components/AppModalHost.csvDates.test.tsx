import { fireEvent, render, screen, within } from '@testing-library/react';

import { FakeUiCommandAdapter } from '../../shared/testing';
import { UiApplicationFacade } from '../control';
import { AppModalHost } from './AppModalHost';

describe('CSV 종료일 선택', () => {
    it('10월 3일을 선택하면 종료일 필드와 달력 선택일을 오늘인 9월 29일로 표시한다', () => {
        const facade = new UiApplicationFacade(new FakeUiCommandAdapter(), { today: '2026-09-29' });
        facade.start();
        facade.dispatch({ type: 'SHOW_TRADE_HISTORY' });
        facade.dispatch({ type: 'OPEN_CSV_EXPORT' });

        try {
            const { rerender } = render(<AppModalHost controller={facade} viewModel={facade.get_view_model()} />);
            const refresh = () => rerender(<AppModalHost controller={facade} viewModel={facade.get_view_model()} />);

            fireEvent.click(screen.getByRole('button', { name: '날짜 선택' }));
            refresh();
            fireEvent.click(screen.getByRole('button', { name: /종료일 선택/ }));
            refresh();
            fireEvent.click(screen.getByRole('button', { name: '다음 달' }));
            fireEvent.click(screen.getByRole('gridcell', { name: '2026년 10월 3일 선택' }));
            refresh();

            expect(screen.getByRole('button', { name: /종료일 선택/ })).toHaveTextContent('2026.09.29');
            expect(within(screen.getByRole('dialog', { name: '종료일 선택 달력' }))
                .getByText('2026.09.29')).toBeInTheDocument();
            expect(screen.getByRole('gridcell', { name: '2026년 10월 3일 선택' }))
                .toHaveAttribute('aria-selected', 'false');

            fireEvent.click(screen.getByRole('button', { name: '이전 달' }));
            expect(screen.getByRole('gridcell', { name: '2026년 9월 29일 선택' }))
                .toHaveAttribute('aria-selected', 'true');
        } finally {
            facade.stop();
        }
    });
});
