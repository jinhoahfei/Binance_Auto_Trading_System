import { PercentSlider } from './PercentSlider';
import type { SplitOrderControlsProps } from '../types';
import styles from './SplitOrderControls.module.css';


/**
 * 함수 이름: SplitOrderControls()
 * 기능: 분할 매수·매도 비율 슬라이더를 하나의 계좌 카드에 표시하고 intent를 전달한다.
 * 인자: props -> 매수·매도 비율, 비활성 상태와 변경 intent 처리 함수
 * 반환값: 분할 주문 제어 카드 React 요소
 * 작성 날짜: 2026/08/12
 */
export function SplitOrderControls({
    buyPercentage,
    disabled = false,
    onIntent,
    sellPercentage,
}: SplitOrderControlsProps) {
    // 매수·매도 입력을 분리하고 각 변경을 해당 방향의 intent로 전달한다.
    return (
        <section aria-labelledby="split-order-title" className={styles.card}>
            <h3 id="split-order-title">분할 매수/매도</h3>
            <div className={styles.sliders}>
                <PercentSlider
                    disabled={disabled}
                    label="분할 매수"
                    onChange={(percentage) => onIntent?.({
                        type: 'SPLIT_PERCENTAGE_REQUESTED',
                        side: 'buy',
                        percentage,
                    })}
                    side="buy"
                    value={buyPercentage}
                />
                <PercentSlider
                    disabled={disabled}
                    label="분할 매도"
                    onChange={(percentage) => onIntent?.({
                        type: 'SPLIT_PERCENTAGE_REQUESTED',
                        side: 'sell',
                        percentage,
                    })}
                    side="sell"
                    value={sellPercentage}
                />
            </div>
        </section>
    );
}
