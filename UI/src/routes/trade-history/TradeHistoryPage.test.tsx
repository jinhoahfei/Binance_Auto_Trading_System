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

  /** Communication Case 3 메시지 1.1.3·2.1.3의 empty/failure 교체를 검증한다. */
  it('test_trade_history_page_replaces_rows_with_empty_and_failure_states: 상태를 직접 렌더링한다', () => {
    const common_props = {
      description: '오늘 · ETHUSDT · 전체',
      onBack: vi.fn(),
      onExportCsv: vi.fn(),
      onPeriodChange: vi.fn(),
      onSideChange: vi.fn(),
      period: 'TODAY' as const,
      rows: [],
      side: 'ALL' as const,
      summary: TRADE_HISTORY_SUMMARY_FIXTURE,
    };
    const { rerender } = render(
      <TradeHistoryPage
        {...common_props}
        emptyState={{
          title: '거래 내역이 없습니다',
          description: '현재 조건과 일치하는 체결이 없습니다.',
          suggestion: '필터를 변경하세요.',
          actionLabel: '자동매매 시작하기',
        }}
      />,
    );

    expect(screen.getByText('거래 내역이 없습니다')).toBeInTheDocument();
    expect(screen.queryByText('26/06/22 - 10:42:18')).not.toBeInTheDocument();

    // 같은 page owner가 실패 문구로 교체하되 stale 거래 행을 다시 표시하지 않아야 한다.
    rerender(
      <TradeHistoryPage
        {...common_props}
        emptyState={{
          title: '거래 내역을 불러오지 못했습니다',
          description: '안전한 조회 실패',
          suggestion: '잠시 후 다시 시도해 주세요.',
          actionLabel: '다시 시도',
        }}
      />,
    );
    expect(screen.getByText('거래 내역을 불러오지 못했습니다')).toBeInTheDocument();
    expect(screen.getByText('안전한 조회 실패')).toBeInTheDocument();
    expect(screen.queryByText('거래 내역이 없습니다')).not.toBeInTheDocument();
  });

  /** Communication Case 3 메시지 2와 Case 4 메시지 1의 disabled 사용자 경계를 검증한다. */
  it('test_trade_history_disabled_filters_and_export_emit_nothing: 조회 중 입력을 차단한다', async () => {
    const handle_export = vi.fn();
    const handle_period = vi.fn();
    const handle_side = vi.fn();
    const user = userEvent.setup();

    render(
      <TradeHistoryPage
        description="조회 중"
        filtersDisabled
        onBack={vi.fn()}
        onExportCsv={handle_export}
        onPeriodChange={handle_period}
        onSideChange={handle_side}
        period="TODAY"
        rows={[]}
        side="ALL"
        summary={TRADE_HISTORY_SUMMARY_FIXTURE}
      />,
    );

    // Native disabled controls는 filter와 CSV intent를 모두 facade 밖에서 차단한다.
    const period_button = screen.getByRole('button', { name: '최근 30일' });
    const side_button = screen.getByRole('button', { name: '매도' });
    const export_button = screen.getByRole('button', { name: 'CSV 내보내기' });
    await user.click(period_button);
    await user.click(side_button);
    await user.click(export_button);
    expect(period_button).toBeDisabled();
    expect(side_button).toBeDisabled();
    expect(export_button).toBeDisabled();
    expect(handle_period).not.toHaveBeenCalled();
    expect(handle_side).not.toHaveBeenCalled();
    expect(handle_export).not.toHaveBeenCalled();
  });
});
