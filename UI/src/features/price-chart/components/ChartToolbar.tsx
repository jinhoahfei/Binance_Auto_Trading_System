import type { ChartInterval, PriceChartIntent } from '../types';
import styles from './ChartToolbar.module.css';

const INTERVAL_OPTIONS: ReadonlyArray<{ readonly interval: ChartInterval; readonly label: string }> = [
    { interval: '1m', label: '1분' },
    { interval: '30m', label: '30분' },
    { interval: '4h', label: '4시간' },
    { interval: '1d', label: '1일' },
];

export interface ChartToolbarProps {
    readonly activeState: string;
    readonly interval: ChartInterval;
    readonly indicatorSettingsOpen?: boolean;
    readonly onIntent?: ((intent: PriceChartIntent) => void) | undefined;
}

/**
 * 함수 이름: ChartToolbar()
 * 기능: 가격 차트 주기, 적용 전략 상태와 지표 설정 intent 컨트롤을 표시한다.
 * 인자: props -> 선택 주기, 적용 상태와 차트 intent 처리 함수
 * 반환값: 가격 차트 도구 모음 React 요소
 * 작성 날짜: 2026/08/12
 */
export function ChartToolbar({
    activeState,
    interval,
    indicatorSettingsOpen = false,
    onIntent,
}: ChartToolbarProps) {
    return (
        <div className={styles.toolbar}>
            <div aria-label="차트 주기" className={styles.intervals} role="group">
                {INTERVAL_OPTIONS.map((option) => (
                    <button
                        aria-pressed={option.interval === interval}
                        className={option.interval === interval ? styles.activeInterval : styles.interval}
                        key={option.interval}
                        onClick={() => onIntent?.({
                            type: 'CHART_INTERVAL_REQUESTED',
                            interval: option.interval,
                        })}
                        type="button"
                    >
                        {option.label}
                    </button>
                ))}
            </div>
            <div className={styles.activeState}>
                <span aria-hidden="true" className={styles.activeDot} />
                <span className={styles.activeLabel}>ACTIVE STATE</span>
                <strong>{activeState}</strong>
            </div>
            <button
                aria-expanded={indicatorSettingsOpen}
                aria-controls="chart-indicator-settings"
                className={styles.settings}
                data-indicator-settings-trigger
                onClick={() => onIntent?.({ type: 'INDICATOR_SETTINGS_REQUESTED' })}
                type="button"
            >
                지표 설정
            </button>
        </div>
    );
}
