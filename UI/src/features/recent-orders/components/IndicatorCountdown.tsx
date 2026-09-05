import { useEffect, useRef, useState } from 'react';
import type { RealtimeIndicatorTimerViewModel } from '../types';
import { present_indicator_timer } from '../indicatorTimerPresenter';
import styles from './IndicatorCountdown.module.css';

export interface IndicatorCountdownProps {
    readonly timer: RealtimeIndicatorTimerViewModel;
    readonly visible: boolean;
}

/**
 * 함수 이름: use_countdown_clock()
 * 기능: 보이는 실행 타이머만 매초 다시 그리며 탭·문서가 숨겨지면 화면 갱신을 중지한다.
 * 인자: active -> 현재 표시되는 진행 중 타이머 여부
 * 반환값: 마지막 화면 갱신의 monotonic 시각
 * 작성 날짜: 2026/09/05
 */
function use_countdown_clock(active: boolean): number {
    const [now, set_now] = useState(() => performance.now());
    useEffect(() => {
        let interval: ReturnType<typeof setInterval> | undefined;

        /**
         * 함수 이름: synchronize_visibility()
         * 기능: 숨김 해제 시 경과를 즉시 보정하고 필요한 화면 interval만 다시 연결한다.
         * 인자: 없음
         * 반환값: 없음
         * 작성 날짜: 2026/09/05
         */
        function synchronize_visibility() {
            // 타이머 기준 자체는 보존하고 브라우저 렌더링의 반복 작업만 해제한다.
            clearInterval(interval);
            set_now(performance.now());
            if (active && document.visibilityState !== 'hidden') {
                interval = setInterval(() => set_now(performance.now()), 1000);
            }
        }
        synchronize_visibility();
        document.addEventListener('visibilitychange', synchronize_visibility);
        return () => {
            clearInterval(interval);
            document.removeEventListener('visibilitychange', synchronize_visibility);
        };  // 단계 전환으로 사라진 행의 interval을 남기지 않는다.
    }, [active]);
    return now;
}

/**
 * 함수 이름: IndicatorCountdown()
 * 기능: 지표 판정값과 독립된 남은 시간·동작 상태·리셋 사유를 접근 가능한 텍스트로 표시한다.
 * 인자: timer -> 수신 시점이 고정된 타이머 모델, visible -> 실시간 지표 탭 표시 여부
 * 반환값: 조건 행 내부의 카운트다운 React 요소
 * 작성 날짜: 2026/09/05
 */
export function IndicatorCountdown({ timer, visible }: IndicatorCountdownProps) {
    const now = use_countdown_clock(visible && timer.snapshot?.state === 'running');
    const signature = JSON.stringify([timer.snapshot, timer.server_time]);
    const fallback = useRef({ signature, received_at: performance.now() });

    // 직접 표시 fixture에도 수신 기준을 한 번만 만들고 일반 App에서는 actor의 최초 수신 시각을 쓴다.
    if (fallback.current.signature !== signature) {
        fallback.current = { signature, received_at: performance.now() };
    }
    const presentation = present_indicator_timer({
        ...timer, received_at: timer.received_at ?? fallback.current.received_at,
    }, Math.max(now, performance.now()));

    return (
        <div className={styles.timer} data-timer-state={presentation.state} data-timer-id={timer.snapshot?.timer_id}>
            <span className={styles.line}>
                <span role="timer" aria-live="off" aria-label={`타이머 남은 시간 ${presentation.time}`}>
                    남은 <span className={styles.time}>{presentation.time}</span>
                </span>
                <span className={`${styles.status} ${styles[presentation.state]}`}>{presentation.label}</span>
            </span>
            <span className={styles.reason} aria-hidden={presentation.reason === null}>{presentation.reason}</span>
        </div>
    );
}
