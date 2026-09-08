import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ResidualNotice } from './ResidualNotice';

describe('ResidualNotice', () => {
  it('전략 종료 뒤 작은 잔여량과 원가를 반올림하지 않는다', () => {
    render(<ResidualNotice quantity="0.000096" costBasis="0.24024024024024024024024024024024" hasOpenPosition={false} isTrading={false} />);
    expect(screen.getByRole('status')).toHaveTextContent('전략 종료 · 잔여 ETH 있음');
    expect(screen.getByRole('status')).toHaveTextContent('0.000096 ETH');
    expect(screen.getByRole('status')).toHaveTextContent('0.24024024024024024024024024024024 USDT');
    expect(screen.getByRole('status')).toHaveTextContent('가격 변동 위험');
  });

  it('열린 Position이 있으면 전략 종료로 표시하지 않는다', () => {
    render(<ResidualNotice quantity="0.000096" costBasis="0.24" hasOpenPosition isTrading={false} />);
    expect(screen.getByRole('status')).not.toHaveTextContent('전략 종료');
  });

  it('잔여가 없으면 자산 안내를 만들지 않는다', () => {
    render(<ResidualNotice quantity="0.0000" hasOpenPosition={false} isTrading={false} />);
    expect(screen.queryByRole('status')).toBeNull();
  });
});
