import type {
    ChartIndicator,
    IndicatorSettingsViewModel,
    PriceChartIntent,
} from '../types';
import styles from './IndicatorSettingsPopover.module.css';

const INDICATOR_OPTIONS: ReadonlyArray<{
    readonly description: string;
    readonly indicator: ChartIndicator;
    readonly label: string;
}> = [
    { indicator: 'ema9', label: 'EMA9', description: '지수 이동 평균' },
    { indicator: 'bollingerBand', label: '볼린저밴드', description: '상단/하단 밴드' },
    { indicator: 'volume', label: '거래량(Volume)', description: '하단 거래량 막대' },
];

export interface IndicatorSettingsPopoverProps {
    readonly onIntent?: ((intent: PriceChartIntent) => void) | undefined;
    readonly settings: IndicatorSettingsViewModel;
}

/**
 * 함수 이름: IndicatorSettingsPopover()
 * 기능: EMA9, 볼린저밴드와 거래량 표시 여부를 controlled toggle로 변경한다.
 * 인자: props -> 지표 표시 설정과 차트 intent 처리 함수
 * 반환값: 지표 설정 팝오버 React 요소
 * 작성 날짜: 2026/08/12
 */
export function IndicatorSettingsPopover({
    onIntent,
    settings,
}: IndicatorSettingsPopoverProps) {
    return (
        <section
            aria-label="지표 설정"
            className={styles.popover}
            onKeyDown={(event) => {
                if (event.key === 'Escape') {
                    onIntent?.({ type: 'INDICATOR_SETTINGS_CLOSED' });
                }
            }}
        >
            <h3>지표 설정</h3>
            <p>차트에 표시할 보조 지표를 선택하세요.</p>
            <div className={styles.rows}>
                {INDICATOR_OPTIONS.map((option) => {
                    const indicator_is_visible = settings[option.indicator];

                    return (
                        <div className={styles.row} key={option.indicator}>
                            <span
                                aria-hidden="true"
                                className={`${styles.swatch} ${styles[option.indicator]!}`}
                            />
                            <div className={styles.copy}>
                                <strong>{option.label}</strong>
                                <span>{option.description}</span>
                            </div>
                            <button
                                aria-label={`${option.label} ${indicator_is_visible ? '숨기기' : '표시하기'}`}
                                aria-pressed={indicator_is_visible}
                                className={indicator_is_visible ? styles.toggleOn : styles.toggleOff}
                                onClick={() => onIntent?.({
                                    type: 'INDICATOR_VISIBILITY_REQUESTED',
                                    indicator: option.indicator,
                                    visible: !indicator_is_visible,
                                })}
                                type="button"
                            >
                                <span />
                            </button>
                        </div>
                    );
                })}
            </div>
        </section>
    );
}
