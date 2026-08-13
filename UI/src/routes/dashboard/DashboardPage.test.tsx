import { render, screen } from '@testing-library/react';
import { DashboardPage } from './DashboardPage';
import { DEFAULT_DASHBOARD_PROPS } from './dashboardFixture';

describe('DashboardPage', () => {
    it('MainDashboardUI: 기능 모듈을 하나의 헤더 없는 대시보드에 배치한다', () => {
        render(<DashboardPage {...DEFAULT_DASHBOARD_PROPS} />);

        expect(screen.getByRole('main', { name: 'Binance 자동매매 대시보드' })).toBeInTheDocument();
        expect(screen.getByRole('heading', { name: 'REGIME 판단 패널' })).toBeInTheDocument();
        expect(screen.getByRole('heading', { name: 'ETH 가격 차트' })).toBeInTheDocument();
        expect(screen.getByRole('heading', { name: '거래 / 계좌' })).toBeInTheDocument();
        expect(screen.getByRole('heading', { name: '트레이딩 패널' })).toBeInTheDocument();
    });
});
