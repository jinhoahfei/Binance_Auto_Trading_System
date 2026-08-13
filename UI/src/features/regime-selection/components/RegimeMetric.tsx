import type { RegimeMetricViewModel } from '../types';
import styles from './RegimeMetric.module.css';

export type RegimeMetricProps = RegimeMetricViewModel;

/**
 * 함수 이름: RegimeMetric()
 * 기능: REGIME 판단에 사용되는 실시간 지표의 이름과 값을 표시한다.
 * 인자: props -> 지표 식별자, 라벨, 값 및 의미 톤
 * 반환값: 실시간 REGIME 지표 React 요소
 * 작성 날짜: 2026/08/12
 */
export function RegimeMetric({ id, label, tone, value }: RegimeMetricProps) {
    return (
        <div className={styles.metric} data-metric-id={id}>
            <span className={styles.label}>{label}</span>
            <strong className={`${styles.value} ${styles[tone]}`}>{value}</strong>
        </div>
    );
}
