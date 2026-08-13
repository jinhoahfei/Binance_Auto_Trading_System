import { AssetCard } from './AssetCard';
import { StrategyCard } from './StrategyCard';
import type { AccountSectionProps } from '../types';
import styles from './AccountSection.module.css';

/**
 * 함수 이름: AccountSection()
 * 기능: 전략·자산 요약 카드와 주문 비율 컨트롤을 거래·계좌 영역에 배치한다.
 * 인자: props -> 전략·자산 ViewModel과 우측에 배치할 제어 요소
 * 반환값: 거래 및 계좌 요약 React 요소
 * 작성 날짜: 2026/08/12
 */
export function AccountSection({ asset, children, strategy }: AccountSectionProps) {
    return (
        <section aria-labelledby="account-section-title" className={styles.section}>
            <h2 id="account-section-title">거래 / 계좌</h2>
            <div className={styles.content}>
                <StrategyCard {...strategy} />
                <AssetCard {...asset} />
                <div className={styles.controls}>{children}</div>
            </div>
        </section>
    );
}
