import type { BackendTradingIndicatorSnapshot } from '../contracts';
import { is_timer_timestamp, is_trading_timer } from './tradingTimerValidation';

const DECIMAL_PATTERN = /^-?(0|[1-9][0-9]*)(\.[0-9]+)?$/u;


/**
 * 함수 이름: is_nullable_version()
 * 기능: 미평가 version 또는 음수가 아닌 정확한 정수만 수락한다.
 * 인자: value -> wire version
 * 반환값: 유효 여부
 * 작성 날짜: 2026/09/05
 */
function is_nullable_version(value: unknown): boolean {
    return value === null || (Number.isSafeInteger(value) && Number(value) >= 0);
}


/**
 * 함수 이름: is_trading_indicator_snapshot()
 * 기능: 초기 snapshot과 event의 선택적 전략 지표 계약을 동일하게 검증한다.
 * 인자: value -> 검증 전 wire 지표 객체
 * 반환값: 지표 스냅샷 타입의 유효 여부
 * 작성 날짜: 2026/09/05
 */
export function is_trading_indicator_snapshot(value: unknown): value is BackendTradingIndicatorSnapshot {
    if (value === null || typeof value !== 'object') return false;

    const snapshot = value as Record<string, unknown>;
    if (snapshot.server_time !== undefined && snapshot.server_time !== null && !is_timer_timestamp(snapshot.server_time)) return false;
    if (typeof snapshot.phase_key !== 'string' || snapshot.phase_key.length === 0
        || (snapshot.notice !== null && !['order_pending', 'entry_paused', 'stopping', 'inactive'].includes(String(snapshot.notice)))
        || !Array.isArray(snapshot.conditions)) return false;

    // 단계는 지표 행이 없는 주문·종료 상태도 전달하며 각 Case를 한 번만 허용한다.
    if (snapshot.phases !== undefined) {
        if (!Array.isArray(snapshot.phases)) return false;

        const strategies = new Set<string>();
        for (const candidate of snapshot.phases) {
            if (candidate === null || typeof candidate !== 'object') return false;

            const phase = candidate as Record<string, unknown>;
            if ((phase.strategy !== 'CASE_B' && phase.strategy !== 'CASE_C')
                || typeof phase.phase !== 'string' || !phase.phase
                || (phase.notice !== null && !['order_pending', 'other_order_pending', 'entry_paused', 'bbw_rejected', 'case_finished'].includes(String(phase.notice)))
                || strategies.has(phase.strategy)) return false;
            strategies.add(phase.strategy);
        }
    }

    // 중복 행과 불완전한 참·거짓 판정은 정상 전략 수치로 표시하지 않는다.
    const identities = new Set<string>();

    return snapshot.conditions.every((candidate: unknown) => {
        if (candidate === null || typeof candidate !== 'object') return false;

        const row = candidate as Record<string, unknown>;

        // 구버전은 timer 생략을 허용하되 새 타이머에는 서버 기준 시각이 반드시 동반되어야 한다.
        if (row.timer !== undefined && row.timer !== null
            && (!is_trading_timer(row.timer) || !is_timer_timestamp(snapshot.server_time))) return false;
        if (typeof row.condition_id !== 'string' || !row.condition_id
            || typeof row.phase !== 'string' || !row.phase
            || (row.strategy !== null && row.strategy !== 'CASE_B' && row.strategy !== 'CASE_C')
            || !['<', '<=', '>', '>='].includes(String(row.comparison))
            || !['realtime', 'close_30m', 'close_1m', 'touch', 'elapsed', 'runtime'].includes(String(row.source))
            || (row.satisfied !== null && typeof row.satisfied !== 'boolean')
            || !is_nullable_version(row.market_version) || !is_nullable_version(row.context_version)
            || !is_nullable_version(row.hold_seconds)) return false;
        for (const operand of [row.value, row.threshold]) {
            if (operand !== null && (typeof operand !== 'string' || !DECIMAL_PATTERN.test(operand))) return false;
        }
        if (row.evaluated_at !== null && (typeof row.evaluated_at !== 'string'
            || !Number.isFinite(Date.parse(row.evaluated_at)))) return false;
        if (row.satisfied !== null && (row.value === null || row.threshold === null
            || row.evaluated_at === null || row.market_version === null || row.context_version === null)) return false;

        const identity = `${row.strategy}:${row.phase}:${row.condition_id}`;
        if (identities.has(identity)) return false;
        identities.add(identity);  // 동일 ID라도 다른 Case·단계의 조건은 별도 행이다.
        return true;
    });
}
