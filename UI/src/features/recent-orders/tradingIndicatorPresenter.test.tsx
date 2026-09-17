import { render, screen, within } from '@testing-library/react';
import type { BackendTradingCondition, BackendTradingIndicatorSnapshot } from '../../shared/contracts';
import { is_trading_indicator_snapshot } from '../../shared/api/tradingIndicatorValidation';
import { RealtimeIndicators } from './components/RealtimeIndicators';
import { present_trading_indicators } from './tradingIndicatorPresenter';

/**
 * 함수 이름: create_condition()
 * 기능: 화면 판정 경계 테스트에서 수신 여부를 구분할 조건을 생성한다.
 * 인자: changes -> 테스트할 wire 필드 변경
 * 반환값: 유효한 불변 지표 계약
 * 작성 날짜: 2026/09/05
 */
function create_condition(changes: Partial<BackendTradingCondition> = {}): BackendTradingCondition {
    return {
        condition_id: 'b_stop', strategy: 'CASE_B', phase: 'CASE_B_HOLDING',
        value: '-0.08000001', threshold: '-0.08', comparison: '<', satisfied: true,
        source: 'close_30m', hold_seconds: null, evaluated_at: '2026-09-05T01:00:00Z',
        market_version: 1, context_version: 1, ...changes,
    };  // 원본 정밀도의 backend 판정을 제공한다.
}

describe('현재 단계 실시간 지표 표시', () => {
    it('손절 충족은 초록이고 미충족·미수신은 빨강·회색이며 기준을 두 자리로 표시하면서 원본 판정을 유지한다', () => {
        // 같은 수치라도 backend 유지시간 판정이 false이면 UI는 녹색으로 바꾸지 않는다.
        const snapshot: BackendTradingIndicatorSnapshot = { phase_key: 'B_HOLDING', notice: null, conditions: [
            create_condition(),
            create_condition({ condition_id: 'b_profit_zone', value: '0.61', threshold: '0.60', comparison: '>=', satisfied: false, source: 'realtime', hold_seconds: 5 }),
            create_condition({ condition_id: 'b_signal_low', value: null, threshold: null, satisfied: null, evaluated_at: null, market_version: null, context_version: null }),
        ] };
        render(<RealtimeIndicators groups={present_trading_indicators(snapshot, 'Case_B', true, true)} />);
        expect(screen.getAllByRole('heading')).toHaveLength(1);
        const rows = screen.getAllByRole('listitem');
        expect(rows[0]).toHaveAttribute('data-tone', 'positive');
        expect(rows[0]).toHaveTextContent('손절');
        expect(within(rows[0]!).getByLabelText('-0.08 · 충족')).toBeInTheDocument();
        expect(rows[0]).toHaveTextContent('< -0.08');
        expect(rows[0]).not.toHaveTextContent('-0.08000001');
        expect(rows[1]).toHaveAttribute('data-tone', 'negative');
        expect(rows[1]).toHaveTextContent('5초 연속 유지');
        expect(rows[2]).toHaveAttribute('data-tone', 'neutral');
        expect(within(rows[2]!).getByLabelText('— · 확인 대기')).toBeInTheDocument();
    });

    it.each([
        ['2509.99', '2485.043400943892438313960509311914', '2,509.99', '2,485.04'],
        ['0.019999999999999999', '0.02', '0.02', '0.02'],
        ['-0.005', '0', '-0.01', '0.00'],
        ['99.999', '100', '100.00', '100.00'],
    ])('지표 %s와 기준 %s를 두 자리로 표시한다', (value, threshold, displayed, limit) => {
        const row = create_condition({ value, threshold });
        const snapshot: BackendTradingIndicatorSnapshot = { phase_key: 'B', notice: null, conditions: [row] };
        const indicator = present_trading_indicators(snapshot, 'Case_B', true, true)[0]!.indicators[0]!;
        expect(indicator.value).toBe(displayed);
        expect(indicator.criterion).toBe(`< ${limit}`);
        expect(indicator.tone).toBe('positive');
        expect(row.value).toBe(value);
        expect(row.threshold).toBe(threshold);
    });

    it('병렬 감시는 Case별 제목과 현재 단계를 표시하고 연결이 끊기면 판정을 지운다', () => {
        const snapshot: BackendTradingIndicatorSnapshot = { phase_key: 'PARALLEL', notice: null, conditions: [
            create_condition(), create_condition({ condition_id: 'c_stop', strategy: 'CASE_C', phase: 'CASE_C_HOLDING' }),
        ] };
        const groups = present_trading_indicators(snapshot, 'Case_B / Case_C', false, true);
        expect(groups.map((group) => group.title)).toEqual(['Case_B 실시간 지표', 'Case_C 실시간 지표']);
        expect(groups.every((group) => group.phase === 'POSITION_OPEN · 포지션 보유')).toBe(true);
        expect(groups.flatMap((group) => group.indicators).every((row) => row.tone === 'neutral' && row.value === '—')).toBe(true);
    });

    it('WAIT_SIGNAL·Case C 회복 단계를 분리하고 이전 BBW 행을 제거하며 공통 조건은 한 번 표시한다', () => {
        const snapshot: BackendTradingIndicatorSnapshot = {
            phase_key: 'parallel-recovery', notice: null,
            phases: [
                { strategy: 'CASE_B', phase: 'B_WAIT_SIGNAL', notice: 'entry_paused' },
                { strategy: 'CASE_C', phase: 'C_SETUP_RECOVERY', notice: null },
            ],
            conditions: [
                create_condition({ condition_id: 'b_signal_slope', phase: 'B_WAIT_SIGNAL' }),
                create_condition({ condition_id: 'b_touch_bbw', phase: 'B_WAIT_TOUCH' }),
                create_condition({ condition_id: 'c_rebound', strategy: 'CASE_C', phase: 'C_SETUP_RECOVERY' }),
                create_condition({ condition_id: 'upper_safe_exit', strategy: null, phase: 'UPPER_SAFE_EXIT' }),
            ],
        };
        render(<RealtimeIndicators groups={present_trading_indicators(snapshot, 'Case_B / Case_C', true, true)} />);
        const case_b = screen.getByRole('region', { name: 'Case_B 실시간 지표' });
        const case_c = screen.getByRole('region', { name: 'Case_C 실시간 지표' });
        expect(case_b).toHaveTextContent('WAIT_SIGNAL');
        expect(case_b).toHaveTextContent('일시정지');
        expect(case_b).not.toHaveTextContent('터치 순간 30분봉 BBW');
        expect(case_c).toHaveTextContent('SETUP · 반등 회복 대기');
        expect(case_c).not.toHaveTextContent('일시정지');
        expect(screen.getAllByText('상단 밴드 접촉')).toHaveLength(1);
    });

    it('지표가 없는 주문·종료 단계도 backend 상태 그대로 표시한다', () => {
        const snapshot: BackendTradingIndicatorSnapshot = {
            phase_key: 'pending', notice: 'order_pending', conditions: [],
            phases: [
                { strategy: 'CASE_B', phase: 'CASE_B_FINAL_STATE', notice: 'bbw_rejected' },
                { strategy: 'CASE_C', phase: 'C_POSITION_OPEN_SIGNALLED', notice: 'order_pending' },
            ],
        };
        const groups = present_trading_indicators(snapshot, 'Case_C', true, true);
        expect(groups).toHaveLength(2);
        expect(groups[0]?.notice).toContain('터치 순간 BBW');
        expect(groups[1]?.phase).toBe('매수 주문 · 체결 대기');
        expect(groups.every((group) => group.indicators.length === 0)).toBe(true);
        expect(is_trading_indicator_snapshot(snapshot)).toBe(true);
        expect(is_trading_indicator_snapshot({ ...snapshot, phases: [snapshot.phases![0], snapshot.phases![0]] })).toBe(false);
        expect(is_trading_indicator_snapshot({ ...snapshot, phases: [{ strategy: 'CASE_A', phase: 'SETUP', notice: null }] })).toBe(false);
    });

    it('구버전과 종료 후에는 예시값을 만들지 않고 대기·빈 상태 안내를 표시한다', () => {
        for (const active of [false, true]) {
            const group = present_trading_indicators(null, '전략 확인 대기', true, active)[0]!;
            expect(group.indicators).toEqual([]);
            expect(group.notice).toBe(active ? '현재 전략의 지표 수신을 기다리고 있습니다.' : '실행 중인 전략이 없습니다.');
        }
    });

    it('잘못된 Decimal·판정·출처와 중복 지표 계약을 거부한다', () => {
        const row = create_condition();
        expect(is_trading_indicator_snapshot({ phase_key: 'B', notice: null, conditions: [row] })).toBe(true);
        for (const invalid of [
            { value: 0.1 }, { value: 'NaN' }, { satisfied: 'true' }, { value: null },
            { evaluated_at: null }, { market_version: -1 }, { source: '4h_regime' },
        ]) {
            expect(is_trading_indicator_snapshot({ phase_key: 'B', notice: null, conditions: [{ ...row, ...invalid }] })).toBe(false);
        }
        expect(is_trading_indicator_snapshot({ phase_key: 'B', notice: null, conditions: [row, row] })).toBe(false);
    });
});
