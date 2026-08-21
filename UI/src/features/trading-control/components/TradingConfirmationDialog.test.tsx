import { render, screen } from '@testing-library/react';
import { vi } from 'vitest';

import { TradingConfirmationDialog } from './TradingConfirmationDialog';

describe('TradingConfirmationDialog', () => {
  it('Phase 6: 시작 확인 문구 누락을 TYPE_0 표시로 대체하지 않는다', () => {
    render(
      <TradingConfirmationDialog
        kind="start"
        onCancel={vi.fn()}
        onConfirm={vi.fn()}
        open
      />,
    );

    expect(screen.getByText('미선택 REGIME으로 거래를 시작하시겠습니까?'))
      .toBeInTheDocument();
    expect(screen.queryByText(/Type 0/u)).not.toBeInTheDocument();
  });

  it('거래 명령 실패 사유를 재시도 확인창에 표시한다', () => {
    render(
      <TradingConfirmationDialog
        error="order command failed"
        kind="forceStop"
        onCancel={vi.fn()}
        onConfirm={vi.fn()}
        open
      />,
    );

    expect(screen.getByRole('alert')).toHaveTextContent('order command failed');
  });

  it.each([
    {
      reason: 'unsupported_logic' as const,
      description: '선택한 REGIME의 TradingSTM은 현재 지원되지 않습니다.',
      detail: 'UNSUPPORTED_TRADING_LOGIC · 지원 상태를 확인해주세요.',
    },
    {
      reason: 'command_disabled' as const,
      description: '거래 시작 명령이 아직 활성화되지 않았습니다.',
      detail: '시작 기능이 준비된 뒤 다시 시도해주세요.',
    },
  ])('$reason 시작 차단 사유를 하나의 unavailable 안내창에 표시한다', ({
    reason,
    description,
    detail,
  }) => {
    render(
      <TradingConfirmationDialog
        kind="tradingUnavailable"
        onCancel={vi.fn()}
        onConfirm={vi.fn()}
        open
        unavailableReason={reason}
      />,
    );

    expect(screen.getByText(description)).toBeInTheDocument();
    expect(screen.getByText(detail)).toBeInTheDocument();
  });
});
