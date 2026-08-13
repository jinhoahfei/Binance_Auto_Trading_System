import type { RecentOrderViewModel } from '../types';
import styles from './RecentOrdersList.module.css';

export interface RecentOrdersListProps {
    readonly orders: ReadonlyArray<RecentOrderViewModel>;
}

/**
 * 함수 이름: RecentOrdersList()
 * 기능: 최근 자동매매 체결을 매수·매도 의미와 함께 시간순 목록으로 표시한다.
 * 인자: props -> 최근 체결 ViewModel 배열
 * 반환값: 최근 체결 목록 React 요소
 * 작성 날짜: 2026/08/12
 */
export function RecentOrdersList({ orders }: RecentOrdersListProps) {
    if (orders.length === 0) {
        return <p className={styles.empty}>최근 체결 내역이 없습니다.</p>;
    }

    return (
        <ul aria-label="최근 체결 목록" className={styles.list}>
            {orders.map((order) => (
                <li className={styles.order} key={order.id}>
                    <span aria-hidden="true" className={`${styles.dot} ${styles[order.side]!}`} />
                    <div className={styles.description}>
                        <strong>
                            {order.side === 'buy' ? '매수' : '매도'} · ETH ({order.strategy})
                        </strong>
                        <time>{order.time}</time>
                    </div>
                    <div className={styles.values}>
                        <strong>{order.price}</strong>
                        <span className={order.side === 'sell' ? styles.positiveValue : undefined}>
                            {order.secondaryValue}
                        </span>
                    </div>
                </li>
            ))}
        </ul>
    );
}
