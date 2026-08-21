import { DEFAULT_TRADING_LOGIC_COVERAGE } from '../../../shared/contracts';
import { RegimeMetric } from './RegimeMetric';
import { RegimeTypeButton } from './RegimeTypeButton';
import type { RegimeOption, RegimePanelProps, RegimeType } from '../types';
import styles from './RegimePanel.module.css';

const REGIME_OPTIONS: ReadonlyArray<RegimeOption> = [
    { type: 'type0', label: '횡보' },
    { type: 'type1', label: '약상승' },
    { type: 'type2', label: '강상승' },
    { type: 'type3', label: '약하락' },
    { type: 'type4', label: '강하락' },
];

/**
 * 함수 이름: get_regime_label()
 * 기능: REGIME 타입을 화면에 표시할 한국어 시장 상태로 변환한다.
 * 인자: regime_type -> 변환할 REGIME 타입 또는 미선택 값
 * 반환값: 타입과 시장 상태를 조합한 표시 문자열
 * 작성 날짜: 2026/08/12
 */
function get_regime_label(regime_type: RegimeType | null): string {
    if (regime_type === null) {
        return '선택 필요';
    }

    const selected_option = REGIME_OPTIONS.find((option) => option.type === regime_type);

    return selected_option === undefined
        ? regime_type
        : `${selected_option.type} · ${selected_option.label}`;
}

/**
 * 함수 이름: RegimePanel()
 * 기능: 추천·적용 REGIME, 수동 선택 버튼과 실시간 판단 지표를 한 패널에 표시한다.
 * 인자: props -> REGIME 표시 ViewModel과 REGIME 선택 intent 처리 함수
 * 반환값: REGIME 판단 패널 React 요소
 * 작성 날짜: 2026/08/12
 */
export function RegimePanel({
    applied,
    candidate = null,
    disabled = false,
    highlight = false,
    logicCoverage = DEFAULT_TRADING_LOGIC_COVERAGE,
    metrics,
    onIntent,
    recommended,
}: RegimePanelProps) {
    return (
        <section
            aria-labelledby="regime-panel-title"
            className={`${styles.panel} ${highlight ? styles.highlight : ''}`}
            data-highlighted={highlight || undefined}
        >
            <div className={styles.content}>
                <div className={styles.summary}>
                    <h2 className={styles.title} id="regime-panel-title">REGIME 판단 패널</h2>
                    <div className={styles.currentState}>
                        <span className={styles.currentLabel}>추천 타입</span>
                        <strong className={styles.currentValue}>{get_regime_label(recommended)}</strong>
                        {applied !== recommended && applied !== null ? (
                            <span className={styles.appliedValue}>적용 {get_regime_label(applied)}</span>
                        ) : null}
                    </div>
                </div>

                <div aria-hidden="true" className={styles.divider} />

                <div className={styles.manualSelection}>
                    <div className={styles.notice}>
                        <span className={styles.manualBadge}>수동 적용</span>
                        <span className={styles.manualNote}>버튼 선택이 실제 적용입니다</span>
                    </div>
                    <div aria-label="REGIME 타입 선택" className={styles.options} role="group">
                        {REGIME_OPTIONS.map((option) => (
                            <RegimeTypeButton
                                candidate={candidate === option.type}
                                disabled={disabled}
                                key={option.type}
                                label={option.label}
                                onSelect={(regime) => onIntent?.({
                                    type: 'REGIME_TYPE_REQUESTED',
                                    regime,
                                })}
                                selected={applied === option.type}
                                supportStatus={logicCoverage.find((coverage) => {
                                    return coverage.regime_type === option.type;
                                })?.support_status ?? 'unsupported'}
                                type={option.type}
                            />
                        ))}
                    </div>
                </div>

                <div aria-hidden="true" className={styles.divider} />

                <div className={styles.indicators}>
                    <h3 className={styles.indicatorTitle}>실시간 판단 지표 4개</h3>
                    <div className={styles.metricGrid}>
                        {metrics.map((metric) => (
                            <RegimeMetric key={metric.id} {...metric} />
                        ))}
                    </div>
                </div>
            </div>
        </section>
    );
}
