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

    it('병렬 감시에는 제목 하나와 B·C 구분을 사용하고 연결이 끊기면 색상 판정을 지운다', () => {
        const snapshot: BackendTradingIndicatorSnapshot = { phase_key: 'PARALLEL', notice: null, conditions: [
            create_condition(), create_condition({ condition_id: 'c_stop', strategy: 'CASE_C', phase: 'CASE_C_HOLDING' }),
        ] };
        const groups = present_trading_indicators(snapshot, 'Case_B / Case_C', false, true);
        expect(groups).toHaveLength(1);
        expect(groups[0]?.title).toBe('Case_B / Case_C 실시간 지표');
        expect(groups[0]?.indicators.map((row) => row.label.slice(0, 3))).toEqual(['B ·', 'C ·']);
        expect(groups[0]?.indicators.every((row) => row.tone === 'neutral' && row.value === '—')).toBe(true);
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
