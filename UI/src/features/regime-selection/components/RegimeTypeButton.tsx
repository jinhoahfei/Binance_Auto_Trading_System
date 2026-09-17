import type { TradingLogicSupportStatus } from '../../../shared/contracts';
import type { RegimeOption, RegimeType } from '../types';
import styles from './RegimeTypeButton.module.css';

export interface RegimeTypeButtonProps extends RegimeOption {
    readonly candidate?: boolean;
    readonly disabled?: boolean;
    readonly onSelect?: (regime: RegimeType) => void;
    readonly selected?: boolean;
    readonly supportStatus: TradingLogicSupportStatus;
}


/**
 * 함수 이름: get_button_tone()
 * 기능: REGIME 타입 번호를 상승·하락 시각 톤으로 변환한다.
 * 인자: regime_type -> 표시할 REGIME 타입
 * 반환값: CSS 톤 클래스 이름
 * 작성 날짜: 2026/08/12
 */
function get_button_tone(regime_type: RegimeType): string {
    return regime_type === 'type3' || regime_type === 'type4'
        ? styles.negative!
        : styles.positive!;
}


/**
 * 함수 이름: RegimeTypeButton()
 * 기능: 하나의 REGIME 타입을 선택하는 접근 가능한 버튼을 표시한다.
 * 인자: props -> REGIME 타입, 라벨, 선택 상태 및 선택 intent 처리 함수
 * 반환값: REGIME 타입 선택 버튼 React 요소
 * 작성 날짜: 2026/08/12
 */
export function RegimeTypeButton({
    candidate = false,
    disabled = false,
    label,
    onSelect,
    selected = false,
    supportStatus,
    type,
}: RegimeTypeButtonProps) {
    // 후보와 확정 선택을 서로 다른 CSS 상태로 표현한다.
    const button_class_name = [
        styles.button,
        get_button_tone(type),
        selected ? styles.selected : '',
        candidate ? styles.candidate : '',
    ].filter(Boolean).join(' ');

    // 거래 지원 여부의 설명을 버튼의 접근성 참조와 연결한다.
    const support_label = supportStatus === 'supported' ? '지원' : '미지원';
    const support_description_id = `regime-${type}-trading-support`;

    // 표시 상태와 사용자의 선택 callback을 버튼에 연결한다.
    return (
        <button
            aria-label={`${type} ${label} 적용 요청`}
            aria-describedby={support_description_id}
            aria-pressed={selected}
            className={button_class_name}
            data-candidate={candidate || undefined}
            disabled={disabled}
            onClick={() => onSelect?.(type)}
            type="button"
        >
            <span className={styles.type}>{type}</span>
            <span className={styles.label}>{label}</span>
            <span
                className={`${styles.supportBadge} ${styles[supportStatus]}`}
                id={support_description_id}
            >
                {support_label}
            </span>
        </button>
    );
}
