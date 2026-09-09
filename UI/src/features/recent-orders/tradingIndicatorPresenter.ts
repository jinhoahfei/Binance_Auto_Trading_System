import type { BackendTradingCondition, BackendTradingIndicatorSnapshot } from '../../shared/contracts';
import type { RealtimeIndicatorGroupViewModel } from './types';

// 표시 문구만 UI가 소유하며 임계값과 조건 판정은 backend 결과를 그대로 사용한다.
const CONDITION_LABELS: Readonly<Record<string, string>> = {
    lower_price: '현재가 하단 밴드 접촉',
    upper_safe_exit: '상단 밴드 안전 종료', b_touch_bbw: '터치 순간 30분봉 BBW',
    b_signal_slope: '확정 30분봉 EMA9 기울기', b_signal_pct_b: '확정 30분봉 종가 %B',
    b_signal_low: '신호봉 저가 · 직전 3봉 최저가', b_pullback: '눌림 진입 %B', b_signal_age: '신호 유효시간',
    b_profit_zone: '익절권 %B', b_take_profit_slope: '일반 익절 EMA9 기울기', b_trend_slope: '추세 유지 EMA9 기울기',
    b_stop: '확정봉 손절 EMA9 기울기', b_emergency_stop: '매수가 대비 −1% 비상손절', b_time_exit: '시간청산',
    b_trend_exit_slope: '추세 종료 EMA9 기울기', b_trend_exit_pct_b: '추세 종료 %B',
    c_setup_pct_b: 'Setup %B', c_setup_cci: '30분봉 CCI(20)', c_flush: '저점 확인 %B',
    c_new_low: '저점 갱신', c_rebound: '회복 기준 +0.06 도달 %B', c_recovery_window: '회복 유효시간',
    c_entry_limit: '진입 기준 %B', c_recovery: '회복 종료 %B', c_profit_zone: '익절권 %B',
    c_stop: '손절 EMA9 기울기', c_time_exit: '시간청산', c_trail_fallback: '익절권 이탈 %B',
    c_trail_increase: '30분봉 EMA9 기울기 · 1분봉 종가 대입', c_handoff: 'B 인계 · TP_TRAIL 매도 시점 %B',
};
const NOTICES: Readonly<Record<string, string>> = {
    order_pending: '주문 처리·재시도를 기다리고 있습니다. 현재 재평가하는 조건만 표시합니다.',
    entry_paused: '신규 진입이 일시정지되어 감시만 진행 중입니다.',
    stopping: '자동매매 종료를 처리하고 있습니다.',
    inactive: '실행 중인 전략이 없습니다.',
};

/**
 * 함수 이름: format_indicator_value()
 * 기능: 비교 경계의 정밀도를 유지하고 시간 값에만 초 단위를 붙인다.
 * 인자: value -> backend Decimal 문자열, source -> 값의 출처
 * 반환값: 반올림하지 않은 표시 문자열
 * 작성 날짜: 2026/09/05
 */
function format_indicator_value(value: string | null, source: string): string {
    if (value === null) return '—';
    // 소수 끝 영점만 제거하며 임계값에 가까운 수치를 반올림하지 않는다.
    const display = value.includes('.') ? value.replace(/\.?0+$/u, '') : value;
    return source === 'elapsed' ? `${display}초` : display;
}

/**
 * 함수 이름: present_condition_criterion()
 * 기능: 실제 비교 당시 기준과 backend 유지시간 조건을 읽기 쉬운 문구로 표시한다.
 * 인자: row -> 불변 조건 평가
 * 반환값: 비교식과 필요한 연속 유지시간
 * 작성 날짜: 2026/09/05
 */
function present_condition_criterion(row: BackendTradingCondition): string {
    const operator = { '<': '<', '<=': '≤', '>': '>', '>=': '≥' }[row.comparison];
    const reference = row.condition_id === 'c_trail_increase' ? '이전 기준 ' : '';
    const duration = row.hold_seconds === null ? '' : ` · ${row.hold_seconds === 180 ? '3분' : `${row.hold_seconds}초`} 연속 유지`;
    const duration_labels: Readonly<Record<string, string>> = { '180': '3분', '3600': '60분', '10800': '3시간', '21600': '6시간' };
    const threshold = row.source === 'elapsed' && row.threshold !== null
        ? duration_labels[row.threshold] ?? format_indicator_value(row.threshold, row.source)
        : format_indicator_value(row.threshold, row.source);
    return `${operator} ${reference}${threshold}${duration}`;
}

/**
 * 함수 이름: present_trading_indicators()
 * 기능: 현재 ACTIVE STATE의 지표를 하나의 목록으로 만들고 미수신·오프라인 판정을 회색으로 표시한다.
 * 인자: snapshot -> 현재 단계 지표, active_state -> ACTIVE STATE 문구
 *      is_online -> 최신 서버 연결 여부, is_trading -> 실행 중 여부
 *      received_at -> 마지막 지표를 최초 수신한 UI monotonic 시각
 * 반환값: 한 개의 전략 지표 그룹
 * 작성 날짜: 2026/09/05
 */
export function present_trading_indicators(
    snapshot: BackendTradingIndicatorSnapshot | null,
    active_state: string,
    is_online: boolean,
    is_trading: boolean,
    received_at: number | null = null,
): ReadonlyArray<RealtimeIndicatorGroupViewModel> {
    const rows = snapshot?.conditions ?? [];  // 재연결 알림의 로컬 상태보다 authoritative 지표 수명을 따른다.
    const strategies = new Set(rows.map((row) => row.strategy).filter((strategy) => strategy !== null));
    const prefix_cases = strategies.size > 1 || active_state.includes(' / ')
        || rows.some((row) => row.strategy !== null && active_state !== (row.strategy === 'CASE_B' ? 'Case_B' : 'Case_C'));
    const notice = !is_trading && snapshot === null ? '실행 중인 전략이 없습니다.'
        : !is_online ? '연결이 끊겨 최신 지표 확인을 기다리고 있습니다.'
            : snapshot === null ? '현재 전략의 지표 수신을 기다리고 있습니다.'
                : snapshot.notice === null ? undefined : NOTICES[snapshot.notice];

    // 판정과 값은 항상 같은 행의 결과를 읽고 UI에서 수치 조건을 다시 비교하지 않는다.
    return [{
        id: snapshot?.phase_key ?? `waiting:${active_state}`,
        title: `${active_state} 실시간 지표`,
        notice,
        indicators: rows.map((row) => {
            const available = is_online && row.satisfied !== null;
            // 아직 시작하지 않은 회복 타이머와 구버전의 시간 정보 누락도 명시적으로 표시한다.
            const has_timer = row.timer != null || row.hold_seconds !== null || row.source === 'elapsed' || row.condition_id === 'c_flush';
            const prefix = prefix_cases && row.strategy !== null ? `${row.strategy === 'CASE_B' ? 'B' : 'C'} · ` : '';
            return {
                id: `${row.strategy ?? 'common'}:${row.phase}:${row.condition_id}`,
                label: `${prefix}${CONDITION_LABELS[row.condition_id] ?? row.condition_id}`,
                criterion: present_condition_criterion(row),
                tone: !available ? 'neutral' : row.satisfied ? 'positive' : 'negative',
                value: available ? format_indicator_value(row.value, row.source) : '—',
                ...(has_timer ? { timer: {
                    snapshot: is_online ? row.timer ?? null : null,
                    server_time: snapshot?.server_time ?? null,
                    received_at,
                } } : {}),
            };  // 손절도 조건 충족 자체를 초록색으로 표현한다.
        }),
    }];
}
