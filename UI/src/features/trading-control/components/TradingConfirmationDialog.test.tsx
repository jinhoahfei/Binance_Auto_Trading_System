import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { vi } from 'vitest';

import { TradingConfirmationDialog } from './TradingConfirmationDialog';

describe('TradingConfirmationDialog', () => {
  it('Radix modal은 첫 action에 focus하고 Escape로 확인 절차를 우회하지 않는다', async () => {
    const handle_cancel = vi.fn();
    const handle_confirm = vi.fn();
    const user = userEvent.setup();

    render(
      <TradingConfirmationDialog
        kind="start"
        onCancel={handle_cancel}
        onConfirm={handle_confirm}
        open
      />,
    );

    const cancel_button = screen.getByRole('button', { name: '취소' });

    // Radix가 dialog를 연 직후 DOM 순서상 첫 action을 initial focus로 선택해야 한다.
    await waitFor(() => expect(cancel_button).toHaveFocus());
    await user.keyboard('{Escape}');

    // 거래 확인 modal은 명시적 버튼 intent 없이 Escape만으로 닫히거나 command를 보내지 않는다.
    expect(screen.getByRole('dialog', { name: '자동매매를 시작할까요?' }))
      .toBeInTheDocument();
    expect(handle_cancel).not.toHaveBeenCalled();
    expect(handle_confirm).not.toHaveBeenCalled();
  });

  it('Radix modal이 제거되면 modal을 열었던 trigger로 focus를 복원한다', async () => {
    const trigger = document.createElement('button');

    trigger.textContent = '자동매매 실행';
    document.body.append(trigger);
    trigger.focus();

    const { unmount } = render(
      <TradingConfirmationDialog
        kind="start"
        onCancel={vi.fn()}
        onConfirm={vi.fn()}
        open
      />,
    );

    await waitFor(() => expect(screen.getByRole('button', { name: '취소' })).toHaveFocus());
    unmount();

    // 공통 focus-return hook은 교체 dialog가 없을 때만 원래 trigger를 다시 활성화한다.
    await waitFor(() => expect(trigger).toHaveFocus());
    trigger.remove();
  });

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

  /** 시작 pending 표시가 유효한 live status 이름을 제공하는지 검증한다. */
  it('시작 pending progress를 접근 가능한 status로 표시한다', () => {
    render(
      <TradingConfirmationDialog
        kind="starting"
        onCancel={vi.fn()}
        onConfirm={vi.fn()}
        open
      />,
    );

    // 장식용 progress bar와 별도로 screen reader가 처리 상태를 읽을 수 있어야 한다.
    expect(screen.getByRole('status')).toHaveTextContent('연결 진행 중');
  });

  it('Phase 9: 복구 Position을 자동매매 재개와 구분한 청산 문구를 표시한다', () => {
    render(
      <TradingConfirmationDialog
        kind="recoveryLiquidation"
        onCancel={vi.fn()}
        onConfirm={vi.fn()}
        open
      />,
    );

    expect(screen.getByRole('dialog', { name: '복구 포지션을 청산할까요?' }))
      .toBeInTheDocument();
    expect(screen.getByText(/자동매매는 재개되지 않습니다/u)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '복구 포지션 청산' })).toBeEnabled();
  });

  /** Communication Case 1 메시지 8의 stop 취소와 Escape 음성 경계를 검증한다. */
  it('test_stop_confirmation_cancel_and_escape_do_not_confirm: stop 확인을 우회하지 않는다', async () => {
    const handle_cancel = vi.fn();
    const handle_confirm = vi.fn();
    const user = userEvent.setup();

    render(
      <TradingConfirmationDialog
        kind="stop"
        onCancel={handle_cancel}
        onConfirm={handle_confirm}
        open
      />,
    );

    // 명시적 취소는 취소 intent만 보내고 Escape는 별도 거래 명령을 만들지 않는다.
    await user.click(screen.getByRole('button', { name: '취소' }));
    await user.keyboard('{Escape}');
    expect(handle_cancel).toHaveBeenCalledTimes(1);
    expect(handle_confirm).not.toHaveBeenCalled();
  });

  /** Communication Case 1 메시지 8R의 복구 청산 confirm/cancel 분리를 검증한다. */
  it('test_recovery_liquidation_confirm_and_cancel_are_distinct: 복구 입력을 정확히 분기한다', async () => {
    const handle_cancel = vi.fn();
    const handle_confirm = vi.fn();
    const user = userEvent.setup();
    const { rerender } = render(
      <TradingConfirmationDialog
        kind="recoveryLiquidation"
        onCancel={handle_cancel}
        onConfirm={handle_confirm}
        open
      />,
    );

    // Confirm 경로가 recovery 전용 label에서 정확히 한 번만 호출되는지 확인한다.
    await user.click(screen.getByRole('button', { name: '복구 포지션 청산' }));
    expect(handle_confirm).toHaveBeenCalledTimes(1);
    expect(handle_cancel).not.toHaveBeenCalled();

    rerender(
      <TradingConfirmationDialog
        kind="recoveryLiquidation"
        onCancel={handle_cancel}
        onConfirm={handle_confirm}
        open
      />,
    );
    await user.click(screen.getByRole('button', { name: '취소' }));
    expect(handle_cancel).toHaveBeenCalledTimes(1);
    expect(handle_confirm).toHaveBeenCalledTimes(1);
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
