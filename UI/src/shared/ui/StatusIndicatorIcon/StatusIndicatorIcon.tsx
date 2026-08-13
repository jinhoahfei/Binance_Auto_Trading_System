import negative_icon_bg from '../../../assets/figma/modal-status-negative-bg.svg';
import negative_icon_dot from '../../../assets/figma/modal-status-negative-dot.svg';
import negative_icon_ring from '../../../assets/figma/modal-status-negative-ring.svg';
import positive_icon_bg from '../../../assets/figma/modal-status-positive-bg.svg';
import positive_icon_dot from '../../../assets/figma/modal-status-positive-dot.svg';
import positive_icon_ring from '../../../assets/figma/modal-status-positive-ring.svg';

import styles from './StatusIndicatorIcon.module.css';

export interface StatusIndicatorIconProps {
    readonly tone: 'negative' | 'positive';
}

/**
 * 함수 이름: StatusIndicatorIcon()
 * 기능: Figma 확인·경고 모달의 세 겹 상태 아이콘을 공통 규격으로 표시한다.
 * 인자: props -> 긍정 또는 부정 상태 색상
 * 반환값: 장식용 상태 아이콘 React 요소
 * 작성 날짜: 2026/08/12
 */
export function StatusIndicatorIcon({ tone }: StatusIndicatorIconProps) {
    const icon_assets = tone === 'negative'
        ? { background: negative_icon_bg, ring: negative_icon_ring, dot: negative_icon_dot }
        : { background: positive_icon_bg, ring: positive_icon_ring, dot: positive_icon_dot };

    return (
        <span aria-hidden="true" className={styles.icon}>
            <img alt="" className={styles.background} src={icon_assets.background} />
            <img alt="" className={styles.ring} src={icon_assets.ring} />
            <img alt="" className={styles.dot} src={icon_assets.dot} />
        </span>
    );
}
