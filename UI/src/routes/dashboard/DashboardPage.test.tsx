import { render, screen } from '@testing-library/react';
import { DashboardPage } from './DashboardPage';
import { DEFAULT_DASHBOARD_PROPS } from './dashboardFixture';

describe('DashboardPage', () => {
    it('MainDashboardUI: 기능 모듈을 하나의 헤더 없는 대시보드에 배치한다', () => {
        // 준비한 의존성을 주입해 화면 또는 hook을 실행한다.
        render(<DashboardPage {...DEFAULT_DASHBOARD_PROPS} />);

        // 화면의 표시 내용과 입력 가능 상태를 검증한다.
        expect(screen.getByRole('main', { name: 'Binance 자동매매 대시보드' })).toBeInTheDocument();
        expect(screen.getByRole('heading', { name: 'REGIME 판단 패널' })).toBeInTheDocument();
        expect(screen.getByRole('heading', { name: 'ETH 가격 차트' })).toBeInTheDocument();
        expect(screen.getByRole('heading', { name: '거래 / 계좌' })).toBeInTheDocument();
        expect(screen.getByRole('heading', { name: '트레이딩 패널' })).toBeInTheDocument();
    });
});
