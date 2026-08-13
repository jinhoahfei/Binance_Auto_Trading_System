import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import {
  TRADE_HISTORY_ROWS_FIXTURE,
  TRADE_HISTORY_SUMMARY_FIXTURE,
} from '../../features/trade-history';
import { TradeHistoryPage } from './index';

describe('TradeHistoryPage', () => {
  it('라우트 본문을 조합하고 돌아가기 의도를 전달한다', async () => {
    const user = userEvent.setup();
    const handle_back = vi.fn();

    render(
      <TradeHistoryPage
        description="2026.06.22 · ETH/KRW · Basic Iterative · 전체 체결 6건"
        onBack={handle_back}
        onExportCsv={vi.fn()}
        onPeriodChange={vi.fn()}
        onSideChange={vi.fn()}
        period="TODAY"
        rows={TRADE_HISTORY_ROWS_FIXTURE}
        side="ALL"
        summary={TRADE_HISTORY_SUMMARY_FIXTURE}
      />,
    );

    expect(screen.getByRole('heading', { level: 1, name: '거래 내역 상세' })).toBeInTheDocument();
    expect(screen.getByText('26/06/22 - 10:42:18')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /돌아가기/ }));
    expect(handle_back).toHaveBeenCalledTimes(1);
  });
});
