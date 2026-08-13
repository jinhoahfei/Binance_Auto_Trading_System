import type { RegimeOption, RegimeType } from '../types';
import styles from './RegimeTypeButton.module.css';

export interface RegimeTypeButtonProps extends RegimeOption {
    readonly candidate?: boolean;
    readonly disabled?: boolean;
    readonly onSelect?: (regime: RegimeType) => void;
    readonly selected?: boolean;
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
    type,
}: RegimeTypeButtonProps) {
    const button_class_name = [
        styles.button,
        get_button_tone(type),
        selected ? styles.selected : '',
        candidate ? styles.candidate : '',
    ].filter(Boolean).join(' ');

    return (
        <button
            aria-label={`${type} ${label} 적용 요청`}
            aria-pressed={selected}
            className={button_class_name}
            data-candidate={candidate || undefined}
            disabled={disabled}
            onClick={() => onSelect?.(type)}
            type="button"
        >
            <span className={styles.type}>{type}</span>
            <span className={styles.label}>{label}</span>
        </button>
    );
}
