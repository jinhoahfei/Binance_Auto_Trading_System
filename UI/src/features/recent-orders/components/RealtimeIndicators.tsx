import type { RealtimeIndicatorGroupViewModel } from '../types';
import styles from './RealtimeIndicators.module.css';
import { IndicatorCountdown } from './IndicatorCountdown';

export interface RealtimeIndicatorsProps {
    readonly groups: ReadonlyArray<RealtimeIndicatorGroupViewModel>;
    readonly visible?: boolean;
}

/**
 * 함수 이름: RealtimeIndicators()
 * 기능: 자동매매 전략의 실시간 지표를 의미 톤과 그룹별로 표시한다.
 * 인자: props -> 현재 전략의 실시간 지표 그룹과 탭 표시 여부
 * 반환값: 실시간 지표 목록 React 요소
 * 작성 날짜: 2026/08/12
 */
export function RealtimeIndicators({ groups, visible = true }: RealtimeIndicatorsProps) {
    // 단계별 단일 목록과 안내를 표시하며 고정 체류시간이나 예시 지표는 만들지 않는다.
    return (
        <div className={styles.groups}>
            {groups.map((group) => (
                <section className={styles.group} key={group.id}>
                    <h3>{group.title}</h3>
                    {group.notice === undefined ? null : <p className={styles.notice}>{group.notice}</p>}
                    <ul>
                        {group.indicators.map((indicator) => (
                            <li key={indicator.id} data-condition-id={indicator.id} data-tone={indicator.tone}>
                                <span aria-hidden="true" className={`${styles.dot} ${styles[indicator.tone]!}`} />
                                <span className={styles.label}>
                                    {indicator.label}
                                    <small className={styles.criterion}>{indicator.criterion}</small>
                                </span>
                                <strong className={styles[indicator.tone]} aria-label={`${indicator.value} · ${indicator.tone === 'positive' ? '충족' : indicator.tone === 'negative' ? '미충족' : '확인 대기'}`}>
                                    {indicator.value}
                                </strong>
                                {indicator.timer === undefined ? null : <IndicatorCountdown timer={indicator.timer} visible={visible} />}
                            </li>
                        ))}
                    </ul>
                </section>
            ))}
        </div>
    );
}
