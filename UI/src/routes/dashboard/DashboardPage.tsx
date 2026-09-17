import { AccountSection } from '../../features/account-summary';
import type { AccountSectionProps } from '../../features/account-summary';
import { PriceChartPanel } from '../../features/price-chart';
import type { PriceChartPanelProps } from '../../features/price-chart';
import { RegimePanel } from '../../features/regime-selection';
import type { RegimePanelProps } from '../../features/regime-selection';
import { TraderPanel } from '../../features/recent-orders';
import type { TraderPanelProps } from '../../features/recent-orders';
import { SplitOrderControls } from '../../features/split-order';
import type { SplitOrderControlsProps } from '../../features/split-order';
import styles from './DashboardPage.module.css';

export interface DashboardPageProps {
    readonly account: Omit<AccountSectionProps, 'children'>;
    readonly chart: PriceChartPanelProps;
    readonly className?: string | undefined;
    readonly regime: RegimePanelProps;
    readonly splitOrder: SplitOrderControlsProps;
    readonly trader: TraderPanelProps;
}


/**
 * 함수 이름: DashboardPage()
 * 기능: REGIME, 가격 차트, 계좌와 트레이딩 기능 모듈을 메인 대시보드 격자에 배치한다.
 * 인자: props -> 각 기능 Boundary에 전달할 controlled ViewModel과 intent 함수
 * 반환값: 헤더를 제외한 메인 대시보드 React 요소
 * 작성 날짜: 2026/08/12
 */
export function DashboardPage({
    account,
    chart,
    className,
    regime,
    splitOrder,
    trader,
}: DashboardPageProps) {
    // 차트·REGIME·계좌·트레이더 패널에 전달받은 표시값과 조작을 연결한다.
    const page_class_name = [styles.page, className].filter(Boolean).join(' ');

    return (
        <main aria-label="Binance 자동매매 대시보드" className={page_class_name}>
            <div className={styles.leftColumn}>
                <RegimePanel {...regime} />
                <PriceChartPanel {...chart} />
                <div className={styles.accountRow}>
                    <AccountSection {...account}>
                        <SplitOrderControls {...splitOrder} />
                    </AccountSection>
                </div>
            </div>
            <TraderPanel {...trader} />
        </main>
    );
}
