import { render, screen } from '@testing-library/react';
import { vi } from 'vitest';

import { TradingConfirmationDialog } from './TradingConfirmationDialog';

describe('TradingConfirmationDialog', () => {
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
});
