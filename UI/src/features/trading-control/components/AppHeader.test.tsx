import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { AppHeader } from './AppHeader';

describe('AppHeader', () => {
  it('Phase 9: 정지 상태의 복구 Position은 시작을 차단하고 명시적 청산만 노출한다', async () => {
    // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
    const user = userEvent.setup();
    const on_start_requested = vi.fn();
    const on_stop_requested = vi.fn();

    // 준비한 의존성을 주입해 화면 또는 hook을 실행한다.
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

    // 사용자 조작을 수행하고 그에 따른 비동기 반영을 기다린다.
    await user.click(liquidation_button);

    // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
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

    // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
    expect(environment).toHaveTextContent('계좌 실계좌 · 주문 비활성');
    expect(environment).toHaveAttribute('data-account-environment', 'mainnet');
    expect(environment).toHaveAttribute('data-orders-enabled', 'false');
  });

});
