import type { AssetSummaryViewModel } from '../types';
import styles from './AssetCard.module.css';

export type AssetCardProps = AssetSummaryViewModel;

/**
 * 함수 이름: AssetCard()
 * 기능: 계좌가 제공한 quote 자산·ETH 보유값과 unavailable 평가 필드를 표시한다.
 * 인자: props -> 자산 요약 ViewModel
 * 반환값: 보유 자산 카드 React 요소
 * 작성 날짜: 2026/08/12
 */
export function AssetCard({
    ethAmount,
    ethValue,
    krwValue,
    quoteAsset = 'KRW',
    quoteValue = krwValue,
    profitLoss,
    totalValue,
}: AssetCardProps) {
    return (
        <article aria-label="보유 자산" className={styles.card}>
            <span className={styles.title}>보유 자산</span>
            <strong className={styles.total}>{totalValue}</strong>
            <dl>
                <div>
                    <dt>{quoteAsset}</dt>
                    <dd>{quoteValue}</dd>
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
