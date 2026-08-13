import type { RealtimeIndicatorGroupViewModel } from '../types';
import styles from './RealtimeIndicators.module.css';

export interface RealtimeIndicatorsProps {
    readonly groups: ReadonlyArray<RealtimeIndicatorGroupViewModel>;
    readonly residenceTime?: string | undefined;
}

/**
 * 함수 이름: RealtimeIndicators()
 * 기능: 자동매매 전략의 실시간 지표를 의미 톤과 그룹별로 표시한다.
 * 인자: props -> 실시간 지표 그룹과 선택적 체류시간
 * 반환값: 실시간 지표 목록 React 요소
 * 작성 날짜: 2026/08/12
 */
export function RealtimeIndicators({ groups, residenceTime }: RealtimeIndicatorsProps) {
    return (
        <div className={styles.groups}>
            {groups.map((group, group_index) => (
                <section className={styles.group} key={group.id}>
                    <h3>{group.title}</h3>
                    <ul>
                        {group.indicators.map((indicator, indicator_index) => (
                            <li key={indicator.id}>
                                <span aria-hidden="true" className={`${styles.dot} ${styles[indicator.tone]!}`} />
                                <span className={styles.label}>{indicator.label}</span>
                                <strong className={styles[indicator.tone]}>{indicator.value}</strong>
                                {group_index === 0 && indicator_index === 1 && residenceTime !== undefined ? (
                                    <small>체류시간: <b>{residenceTime}</b></small>
                                ) : null}
                            </li>
                        ))}
                    </ul>
                </section>
            ))}
        </div>
    );
}
