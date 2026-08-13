import type { AssetSummaryViewModel } from '../types';
import styles from './AssetCard.module.css';

export type AssetCardProps = AssetSummaryViewModel;

/**
 * 함수 이름: AssetCard()
 * 기능: 계좌 총자산, KRW·ETH 보유액과 평가 손익을 표시한다.
 * 인자: props -> 자산 요약 ViewModel
 * 반환값: 보유 자산 카드 React 요소
 * 작성 날짜: 2026/08/12
 */
export function AssetCard({
    ethAmount,
    ethValue,
    krwValue,
    profitLoss,
    totalValue,
}: AssetCardProps) {
    return (
        <article aria-label="보유 자산" className={styles.card}>
            <span className={styles.title}>보유 자산</span>
            <strong className={styles.total}>{totalValue}</strong>
            <dl>
                <div>
                    <dt>KRW</dt>
                    <dd>{krwValue}</dd>
                </div>
                <div>
                    <dt>ETH</dt>
                    <dd>{ethAmount}({ethValue})</dd>
                </div>
                <div>
                    <dt>평가 손익</dt>
                    <dd className={styles.profit}>{profitLoss}</dd>
                </div>
            </dl>
        </article>
    );
}
