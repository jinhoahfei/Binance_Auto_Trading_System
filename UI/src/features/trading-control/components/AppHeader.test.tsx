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
  it('Session 7: live read-only는 실계좌와 주문 비활성을 함께 표시한다', () => {
    // Backend의 account와 orders_enabled를 별도로 표시해 LIVE 시세와 주문 권한을 혼동하지 않는다.
    render(<AppHeader
      environment={{ market_data: 'mainnet', account: 'mainnet', orders_enabled: false }}
      isConnected isTrading={false} hasOpenPosition={false} isCommandPending={false}
      onStartRequested={vi.fn()} onStopRequested={vi.fn()}
    />);
    const environment = screen.getByLabelText('시세 및 거래 환경');
    expect(environment).toHaveTextContent('계좌 실계좌 · 주문 비활성');
    expect(environment).toHaveAttribute('data-account-environment', 'mainnet');
    expect(environment).toHaveAttribute('data-orders-enabled', 'false');
  });

});
