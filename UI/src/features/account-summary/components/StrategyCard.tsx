import type { StrategySummaryViewModel } from '../types';
import styles from './StrategyCard.module.css';

export type StrategyCardProps = StrategySummaryViewModel;

/**
 * 함수 이름: StrategyCard()
 * 기능: 자동매매 전략의 작동 상태, 적용 상태와 상태 수익률을 표시한다.
 * 인자: props -> 전략 요약 ViewModel
 * 반환값: 전략 상태 카드 React 요소
 * 작성 날짜: 2026/08/12
 */
export function StrategyCard({
    appliedState,
    profitAmount,
    profitRate,
    status,
    statusTone,
}: StrategyCardProps) {
    return (
        <article aria-label="전략 상태" className={styles.card}>
            <div className={styles.header}>
                <span>전략 상태</span>
                <strong className={styles[statusTone]}>{status}</strong>
                <span aria-hidden="true" className={`${styles.statusDot} ${styles[statusTone]!}`} />
            </div>
            <dl>
                <div>
                    <dt>현재 상태</dt>
                    {/* 차트와 공유하는 실행 전략 문구를 축약 없이 접근 가능한 원문으로 보존한다. */}
                    <dd title={appliedState}>{appliedState}</dd>
                </div>
                <div>
                    <dt>해당 상태 수익률</dt>
                    <dd className={styles.profit}>
                        <strong>{profitRate}</strong>
                        <small>({profitAmount})</small>
                    </dd>
                </div>
            </dl>
        </article>
    );
}
