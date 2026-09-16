import { useState } from 'react';
import { Button, ModalSurface } from '../../../shared/ui';
import { balance_reconciliation_text } from '../../../shared/api/balanceReconciliation';
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
    balanceReconciliation,
    ethAmount,
    ethValue,
    krwValue,
    quoteAsset = 'KRW',
    quoteValue = krwValue,
    profitLoss,
    totalValue,
}: AssetCardProps) {
    const [showDetails, setShowDetails] = useState(false);
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
                    <dt>{balanceReconciliation ? (
                        <button className={styles.balanceButton} type="button" onClick={() => setShowDetails(true)}>ETH · 대조 내역</button>
                    ) : 'ETH'}</dt>
                    <dd>{ethAmount}({ethValue})</dd>
                </div>
                <div>
                    <dt>평가 손익</dt>
                    <dd className={styles.profit}>{profitLoss}</dd>
                </div>
            </dl>
            {balanceReconciliation ? (
                <ModalSurface open={showDetails} onOpenChange={setShowDetails} closeOnOutside title="ETH 잔고 대조"
                    description="포지션과 잔여 원금에 확인된 Earn 보상·보관 내역을 반영한 결과입니다.">
                    <p role="note" className={styles.balanceDetails}>{balance_reconciliation_text(balanceReconciliation)}</p>
                    <Button onClick={() => setShowDetails(false)}>닫기</Button>
                </ModalSurface>
            ) : null}
        </article>
    );
}
