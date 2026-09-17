import type { BackendTradingCondition, BackendTradingIndicatorSnapshot, BackendTradingStatus } from '../../shared/contracts';
import { format_decimal_text } from '../../shared/formatting';
import type { RealtimeIndicatorGroupViewModel } from './types';

// 표시 문구만 UI가 소유하며 임계값과 조건 판정은 backend 결과를 그대로 사용한다.
const CONDITION_LABELS: Readonly<Record<string, string>> = {
    lower_price: '현재가 하단 밴드 접촉',
    upper_safe_exit: '상단 밴드 접촉', b_touch_bbw: '터치 순간 30분봉 BBW',
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
    other_order_pending: '다른 Case의 주문 처리가 끝날 때까지 지표 평가를 기다립니다.',
    entry_paused: '신규 진입이 일시정지되어 감시만 진행 중입니다.',
    stopping: '자동매매 종료를 처리하고 있습니다.',
    inactive: '실행 중인 전략이 없습니다.',
    bbw_rejected: '터치 순간 BBW가 진입 기준을 충족하지 않아 이번 하단 이벤트의 Case_B 감시가 종료되었습니다.',
    case_finished: '이번 하단 이벤트의 신규 진입 감시가 종료되었습니다.',
};


/**
 * 함수 이름: format_indicator_value()
 * 기능: 지표와 기준값을 소수점 둘째 자리로 표시하고 시간 값에 초 단위를 붙인다.
 * 인자: value -> backend Decimal 문자열, source -> 값의 출처
 * 반환값: 두 자리로 반올림한 표시 문자열
 * 작성 날짜: 2026/09/05
 */
function format_indicator_value(value: string | null, source: string): string {
    if (value === null) return '—';

    // 화면만 반올림하며 조건 충족 여부는 원본 정밀도로 평가한 backend 결과를 사용한다.
    const display = format_decimal_text(value);

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

// 단계 이름은 backend의 상태를 번역하며 수치로 단계를 추측하지 않는다.
const PHASE_LABELS: Readonly<Record<string, readonly [string, string]>> = {
    B_WAIT_TOUCH: ['터치 조건 판정', '터치 순간에 저장한 BBW로 후보 진입을 한 번 판정합니다.'],
    B_WAIT_SIGNAL: ['WAIT_SIGNAL · 회복 신호 대기', '30분봉 마감 때 세 조건을 처음으로 모두 만족하면 WAIT_PULLBACK으로 이동합니다.'],
    B_WAIT_PULLBACK: ['WAIT_PULLBACK · 눌림 매수 대기', '확정 신호의 유효시간 안에 실시간 눌림 조건을 만족하면 매수를 요청합니다.'],
    B_POSITION_OPEN_SIGNALLED: ['매수 주문 · 체결 대기', '매수 판정 후 주문 결과를 기다립니다.'],
    CASE_B_HOLDING: ['POSITION_OPEN · 포지션 보유', '비상손절 → 확정봉 손절 → 추세 유지·익절 → 시간청산 순서로 판정합니다.'],
    CASE_B_TREND_HOLD: ['TREND_HOLD · 추세 유지', 'EMA 기울기 또는 %B 약화가 연속 유지되면 청산합니다.'],
    CASE_B_CLOSED: ['청산 완료', '청산 사유와 현재 하단 접촉 여부로 다음 이벤트를 판정합니다.'],
    CASE_B_FINAL_STATE: ['신호 감시 종료', '새 하단 터치 이벤트에서 진입 조건을 다시 판정합니다.'],
    C_WAIT_SETUP: ['WAIT_SETUP · 과이탈 대기', '실시간 %B와 30분봉 CCI 조건을 모두 만족하면 SETUP으로 이동합니다.'],
    C_SETUP_FLUSH: ['SETUP · 저점 확인 대기', '더 깊은 과이탈을 확인하면 저점을 저장하고 회복 타이머를 시작합니다.'],
    C_SETUP_RECOVERY: ['SETUP · 반등 회복 대기', '새 저점 갱신을 먼저 처리합니다. 타이머 만료 시 기준을 다시 잡고, 회복 폭과 진입 한도를 함께 확인합니다.'],
    C_POSITION_OPEN_SIGNALLED: ['매수 주문 · 체결 대기', '회복 매수 판정 후 주문 결과를 기다립니다.'],
    CASE_C_HOLDING: ['POSITION_OPEN · 포지션 보유', '익절권 진입 → 손절 → 시간청산 순서로 판정합니다.'],
    CASE_C_TP_TRAILING: ['TP_TRAILING · 익절 추적', '익절권 이탈 → 1분봉 마감의 30분봉 EMA 기울기 비교 → 시간청산 순서로 판정합니다.'],
    CASE_C_CLOSED: ['청산 완료 · 회복 대기', '실시간 회복 조건을 만족한 뒤 Case_B 인계를 판정합니다.'],
    CASE_C_RECOVERY_SUCCEEDED: ['회복 확인 · Case_B 인계 판정', 'TP_TRAIL 청산 사유와 매도 시점 %B로 적극 인계 여부를 판정합니다.'],
    CASE_C_FINAL_STATE: ['신호 감시 종료', '같은 하단 이벤트에서는 Case_C 신규 진입을 다시 시도하지 않습니다.'],
    LOWER_TOUCH_WATCH: ['하단 터치 대기', '현재가와 실시간 30분봉 하단 밴드를 비교합니다.'],
};


/**
 * 함수 이름: present_trading_indicators()
 * 기능: Case별 단계와 해당 단계 지표를 분리하고 공통 안전 조건을 한 번 표시한다.
 * 인자: snapshot -> 현재 단계 지표, active_state -> ACTIVE STATE 문구
 *      is_online -> 최신 서버 연결 여부, is_trading -> 실행 중 여부
 *      received_at -> 마지막 지표를 최초 수신한 UI monotonic 시각
 *      lifecycle_status -> backend 세션의 실제 평가 실행 상태
 * 반환값: Case_B·Case_C 및 공통 조건의 지표 그룹
 * 작성 날짜: 2026/09/05
 */
export function present_trading_indicators(
    snapshot: BackendTradingIndicatorSnapshot | null,
    active_state: string,
    is_online: boolean,
    is_trading: boolean,
    received_at: number | null = null,
    lifecycle_status: BackendTradingStatus = is_trading ? 'running' : 'not_started',
): ReadonlyArray<RealtimeIndicatorGroupViewModel> {
    const rows = snapshot?.conditions ?? [];
    const evaluation_running = is_trading && lifecycle_status === 'running';
    const lifecycle_notice = lifecycle_status === 'reconciliation_required'
        ? '전략 평가가 중단되었습니다. 주문 상태 확인이 필요합니다.'
        : lifecycle_status === 'stopping' ? '자동매매 종료를 처리하고 있습니다. 전략 지표 평가가 중단되었습니다.'
        : !is_trading && snapshot === null ? '실행 중인 전략이 없습니다.'
        : !is_online ? '연결이 끊겨 최신 지표 확인을 기다리고 있습니다.'
            : snapshot === null ? '현재 전략의 지표 수신을 기다리고 있습니다.' : undefined;

    // 구버전은 실제 수신한 행의 소속과 단계만 사용한다. 지표 없는 Case를 임의 생성하지 않는다.
    const phases = snapshot?.phases?.length ? snapshot.phases : (['CASE_B', 'CASE_C'] as const).flatMap((strategy) => {
        const row = rows.find((candidate) => candidate.strategy === strategy);

        return row ? [{ strategy, phase: row.phase, notice: snapshot?.notice ?? null }] : [];
    });
    const definitions = phases.map((phase) => ({
        strategy: phase.strategy as BackendTradingCondition['strategy'],
        phase: phase.phase,
        title: `${phase.strategy === 'CASE_B' ? 'Case_B' : 'Case_C'} 실시간 지표`,
        notice: phase.notice,
    }));
    const common = rows.filter((row) => row.strategy === null);
    if (common.length || !definitions.length) {
        definitions.push({
            strategy: null,
            phase: common[0]?.phase ?? '',
            title: phases.length ? '공통 실시간 지표' : `${active_state} 실시간 지표`,
            notice: phases.length ? null : snapshot?.notice ?? null,
        });
    }

    return definitions.map((group) => {
        const phase_label = PHASE_LABELS[group.phase];

        return {
            id: `${snapshot?.phase_key ?? 'waiting'}:${group.strategy ?? 'common'}`,
            title: group.title,
            ...(phase_label ? { phase: phase_label[0], phase_description: phase_label[1] } : {}),
            notice: lifecycle_notice ?? (group.notice === null ? undefined : NOTICES[group.notice]),
            // backend가 제공한 단계와 행을 함께 읽고 조건의 수치나 충족 여부를 재계산하지 않는다.
            indicators: rows.filter((row) => row.strategy === group.strategy && (group.strategy === null || row.phase === group.phase)).map((row) => {
                const available = is_online && evaluation_running && row.satisfied !== null;
                const has_timer = row.timer != null || row.hold_seconds !== null || row.source === 'elapsed' || row.condition_id === 'c_flush';

                return {
                    id: `${row.strategy ?? 'common'}:${row.phase}:${row.condition_id}`,
                    label: CONDITION_LABELS[row.condition_id] ?? row.condition_id,
                    criterion: present_condition_criterion(row),
                    tone: !available ? 'neutral' : row.satisfied ? 'positive' : 'negative',
                    value: available ? format_indicator_value(row.value, row.source) : '—',
                    ...(has_timer ? { timer: {
                        snapshot: is_online && evaluation_running ? row.timer ?? null : null,
                        server_time: snapshot?.server_time ?? null,
                        received_at,
                    } } : {}),
                };
            }),
        };
    });
}
