import type { CSSProperties } from 'react';
import type { PercentSliderProps } from '../types';
import styles from './PercentSlider.module.css';

const PERCENTAGE_STEP = 10;

/**
 * 함수 이름: clamp_percentage()
 * 기능: 분할 주문 비율을 0부터 100 사이의 10 단위 값으로 제한한다.
 * 인자: percentage -> 제한할 원본 비율
 * 반환값: 허용 범위와 단위에 맞춘 비율
 * 작성 날짜: 2026/08/12
 */
function clamp_percentage(percentage: number): number {
    const rounded_percentage = Math.round(percentage / PERCENTAGE_STEP) * PERCENTAGE_STEP;

    return Math.max(0, Math.min(100, rounded_percentage));
}

/**
 * 함수 이름: PercentSlider()
 * 기능: 분할 매수 또는 매도 비율을 버튼과 접근 가능한 슬라이더로 조정한다.
 * 인자: props -> 주문 방향, 표시 라벨, 현재 비율과 변경 처리 함수
 * 반환값: 분할 주문 비율 슬라이더 React 요소
 * 작성 날짜: 2026/08/12
 */
export function PercentSlider({
    disabled = false,
    label,
    onChange,
    side,
    value,
}: PercentSliderProps) {
    const safe_value = clamp_percentage(value);

    return (
        <div className={`${styles.slider} ${styles[side]!}`}>
            <div className={styles.heading}>
                <label htmlFor={`split-order-${side}`}>{label}</label>
                <output htmlFor={`split-order-${side}`}>{safe_value}%</output>
            </div>
            <div className={styles.controls}>
                <button
                    aria-label={`${label} 10% 감소`}
                    disabled={disabled || safe_value === 0}
                    onClick={() => onChange?.(clamp_percentage(safe_value - PERCENTAGE_STEP))}
                    type="button"
                >
                    −
                </button>
                <input
                    aria-valuetext={`${safe_value}%`}
                    disabled={disabled}
                    id={`split-order-${side}`}
                    max="100"
                    min="0"
                    onInput={(event) => onChange?.(clamp_percentage(Number(event.currentTarget.value)))}
                    step={PERCENTAGE_STEP}
                    style={{ '--slider-value': `${safe_value}%` } as CSSProperties}
                    type="range"
                    value={safe_value}
                />
                <button
                    aria-label={`${label} 10% 증가`}
                    disabled={disabled || safe_value === 100}
                    onClick={() => onChange?.(clamp_percentage(safe_value + PERCENTAGE_STEP))}
                    type="button"
                >
                    +
                </button>
            </div>
        </div>
    );
}
