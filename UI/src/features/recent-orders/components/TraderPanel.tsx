import { RealtimeIndicators } from './RealtimeIndicators';
import { RecentOrdersList } from './RecentOrdersList';
import type { TraderPanelProps, TraderPanelTab } from '../types';
import styles from './TraderPanel.module.css';

const TAB_OPTIONS: ReadonlyArray<{ readonly tab: TraderPanelTab; readonly label: string }> = [
    { tab: 'recent', label: '체결 내역' },
    { tab: 'realtime', label: '실시간 지표' },
];

/**
 * 함수 이름: TraderPanel()
 * 기능: 최근 체결과 실시간 지표 탭을 전환하여 트레이딩 결과를 표시한다.
 * 인자: props -> 선택 탭, 체결·지표 ViewModel과 사용자 intent 처리 함수
 * 반환값: 트레이딩 패널 React 요소
 * 작성 날짜: 2026/08/12
 */
export function TraderPanel({
    activeTab,
    indicatorGroups,
    onIntent,
    orders,
    residenceTime,
}: TraderPanelProps) {
    return (
        <aside aria-labelledby="trader-panel-title" className={styles.panel}>
            <header>
                <h2 id="trader-panel-title">트레이딩 패널</h2>
                <p>거래 결과와 실시간 지표</p>
            </header>
            <div aria-label="트레이딩 패널 보기" className={styles.tabs} role="tablist">
                {TAB_OPTIONS.map((option) => (
                    <button
                        aria-controls={`trader-tabpanel-${option.tab}`}
                        aria-selected={activeTab === option.tab}
                        className={activeTab === option.tab ? styles.activeTab : styles.tab}
                        id={`trader-tab-${option.tab}`}
                        key={option.tab}
                        onClick={() => onIntent?.({
                            type: 'TRADER_TAB_REQUESTED',
                            tab: option.tab,
                        })}
                        role="tab"
                        type="button"
                    >
                        {option.label}
                    </button>
                ))}
            </div>

            <div
                aria-labelledby={`trader-tab-${activeTab}`}
                className={styles.tabPanel}
                id={`trader-tabpanel-${activeTab}`}
                role="tabpanel"
            >
                {activeTab === 'recent' ? (
                    <>
                        <div className={styles.sectionHeader}>
                            <h3>최근 체결</h3>
                            <button
                                onClick={() => onIntent?.({ type: 'ALL_ORDERS_REQUESTED' })}
                                type="button"
                            >
                                전체 보기
                            </button>
                        </div>
                        <RecentOrdersList orders={orders} />
                    </>
                ) : (
                    <RealtimeIndicators groups={indicatorGroups} residenceTime={residenceTime} />
                )}
            </div>
        </aside>
    );
}
