import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { AppHeader } from './AppHeader';

describe('AppHeader', () => {
  it('Phase 9: 정지 상태의 복구 Position은 시작을 차단하고 명시적 청산만 노출한다', async () => {
    const user = userEvent.setup();
    const on_start_requested = vi.fn();
    const on_stop_requested = vi.fn();

    render(
      <AppHeader
        hasOpenPosition
        isCommandPending={false}
        isConnected
        isTrading={false}
        onStartRequested={on_start_requested}
        onStopRequested={on_stop_requested}
      />,
    );

    // 복구 Position을 정상 자동매매 시작으로 인수하지 않고 청산 버튼만 활성화한다.
    expect(screen.getByRole('button', { name: '자동매매 실행' })).toBeDisabled();
    const liquidation_button = screen.getByRole('button', { name: '복구 포지션 청산' });
    expect(liquidation_button).toBeEnabled();

    await user.click(liquidation_button);

    expect(on_stop_requested).toHaveBeenCalledTimes(1);
    expect(on_start_requested).not.toHaveBeenCalled();
  });
});
