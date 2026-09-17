import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import {
  EMPTY_TRADE_HISTORY_SUMMARY_FIXTURE,
  TRADE_HISTORY_ROWS_FIXTURE,
  TRADE_HISTORY_SUMMARY_FIXTURE,
} from '../fixtures';
import { HistoryFilters, SummaryCards, TradeTable } from './index';

describe('거래 내역 표시 컴포넌트', () => {
  it('BNB 원 수수료와 USDT 평가 정책을 금액 옆에 표시한다', () => {
    // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
    const feeNote = '원 수수료 0.000001 BNB · BNB는 체결 직전 1초봉 종가로 USDT 평가';

    // 준비한 의존성을 주입해 화면 또는 hook을 실행한다.
    render(<TradeTable rows={[{ ...TRADE_HISTORY_ROWS_FIXTURE[0]!, feeNote }]} />);
    expect(screen.getByText(feeNote)).toBeInTheDocument();  // 화면의 표시 내용과 입력 가능 상태를 검증한다.
  });

  it('요약 카드와 체결 행을 Figma 열 구조로 표시한다', () => {
    // 준비한 의존성을 주입해 화면 또는 hook을 실행한다.
    render(
      <>
        <SummaryCards summary={TRADE_HISTORY_SUMMARY_FIXTURE} />
        <TradeTable rows={TRADE_HISTORY_ROWS_FIXTURE} />
      </>,
    );

    // 화면의 표시 내용과 입력 가능 상태를 검증한다.
    expect(screen.getByText('+1.62%')).toBeInTheDocument();
    expect(screen.getByText('0.8421 ETH')).toBeInTheDocument();
    expect(screen.getByText('오늘 계좌 수익률')).toBeInTheDocument();
    expect(screen.getByText('전체 매도 성과')).toBeInTheDocument();
    expect(screen.getByText('현재 ETH 보유량')).toBeInTheDocument();
    expect(screen.getByText('오늘 계좌 수수료')).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: '진입당시 ETH가격' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: '직전 매수 수익률' })).toBeInTheDocument();
    expect(screen.getByText('Momentum Exit')).toBeInTheDocument();
    expect(screen.getByText('+₩7,688')).toBeInTheDocument();
  });

  it('기간과 거래 구분 필터를 독립된 제어 의도로 전달한다', async () => {
    // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
    const user = userEvent.setup();
    const handle_period_change = vi.fn();
    const handle_side_change = vi.fn();
    const handle_export = vi.fn();

    // 준비한 의존성을 주입해 화면 또는 hook을 실행한다.
    render(
      <HistoryFilters
        onExportCsv={handle_export}
        onPeriodChange={handle_period_change}
        onSideChange={handle_side_change}
        period="TODAY"
        side="ALL"
      />,
    );

    await user.click(screen.getByRole('button', { name: '최근 7일' }));
    await user.click(screen.getByRole('button', { name: '매도' }));
    await user.click(screen.getByRole('button', { name: 'CSV 내보내기' }));

    // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
    expect(handle_period_change).toHaveBeenCalledWith('WEEKLY');
    expect(handle_side_change).toHaveBeenCalledWith('SELL');
    expect(handle_export).toHaveBeenCalledTimes(1);
  });

  it('거래가 없으면 안내와 자동매매 시작 의도를 표시한다', async () => {
    // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
    const user = userEvent.setup();
    const handle_start_trading = vi.fn();

    // 준비한 의존성을 주입해 화면 또는 hook을 실행한다.
    render(
      <>
        <SummaryCards summary={EMPTY_TRADE_HISTORY_SUMMARY_FIXTURE} />
        <TradeTable onStartTrading={handle_start_trading} rows={[]} />
      </>,
    );

    expect(screen.getByText('거래 내역이 없습니다')).toBeInTheDocument();  // 화면의 표시 내용과 입력 가능 상태를 검증한다.

    // 사용자 조작을 수행하고 그에 따른 비동기 반영을 기다린다.
    await user.click(screen.getByRole('button', { name: '자동매매 시작하기' }));
    expect(handle_start_trading).toHaveBeenCalledTimes(1);  // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
  });

  it('loading 중에는 empty CTA를 노출하지 않고 busy 상태만 표시한다', () => {
    // 준비한 의존성을 주입해 화면 또는 hook을 실행한다.
    render(
      <TradeTable
        emptyState={{
          title: '거래 내역을 불러오는 중입니다',
          description: '최신 체결 기록을 조회하고 있습니다.',
          suggestion: '잠시만 기다려 주세요.',
          actionLabel: '조회 중',
        }}
        isLoading
        rows={[]}
      />,
    );

    // 화면의 표시 내용과 입력 가능 상태를 검증한다.
    expect(screen.getByLabelText('거래 내역 표')).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByText('거래 내역을 불러오는 중입니다')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '조회 중' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '자동매매 시작하기' })).not.toBeInTheDocument();
  });
});
