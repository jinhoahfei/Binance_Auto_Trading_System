import { act, cleanup, render, screen } from '@testing-library/react';
import { createActor } from 'xstate';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { BackendTradingCondition, BackendTradingIndicatorSnapshot, BackendTradingTimer } from '../../shared/contracts';
import { is_trading_indicator_snapshot } from '../../shared/api/tradingIndicatorValidation';
import { RealtimeIndicators } from './components/RealtimeIndicators';
import { create_recent_orders_machine } from './machines/recentOrdersMachine';
import { format_timer_seconds, present_indicator_timer } from './indicatorTimerPresenter';
import { present_trading_indicators } from './tradingIndicatorPresenter';

const SAMPLE_TIME = '2026-09-05T01:00:00Z';


/**
 * 함수 이름: create_timer()
 * 기능: 서버가 측정한 3분 회복 타이머의 기본 wire 값을 생성한다.
 * 인자: changes -> 현재 테스트의 상태·잔여시간·회차 변경
 * 반환값: 유효한 타이머 DTO
 * 작성 날짜: 2026/09/05
 */
function create_timer(changes: Partial<BackendTradingTimer> = {}): BackendTradingTimer {
    return { timer_id: 'recovery:1', kind: 'window', state: 'running', duration_seconds: '180',
        remaining_seconds: '180', sampled_at: SAMPLE_TIME, reset_reason: null, ...changes };
}


/**
 * 함수 이름: create_snapshot()
 * 기능: 같은 C 회복 단계의 지표와 선택적 타이머를 함께 생성한다.
 * 인자: timer -> 타이머 또는 구버전 누락, changes -> 지표 판정 변경, server_time -> 전송 기준 시각
 * 반환값: 하나의 지표가 있는 snapshot
 * 작성 날짜: 2026/09/05
 */
function create_snapshot(timer: BackendTradingTimer | null = create_timer(), changes: Partial<BackendTradingCondition> = {}, server_time = SAMPLE_TIME): BackendTradingIndicatorSnapshot {
    return { phase_key: 'C_SETUP_RECOVERY', notice: null, server_time, conditions: [{
        condition_id: 'c_recovery_window', strategy: 'CASE_C', phase: 'C_SETUP_RECOVERY', value: '0',
        threshold: '180', comparison: '<=', satisfied: true, source: 'elapsed', hold_seconds: null,
        evaluated_at: SAMPLE_TIME, market_version: 1, context_version: 1, timer, ...changes,
    }] };  // 실제 조건 색상은 timer.state와 별도로 입력한다.
}


/**
 * 함수 이름: groups_for()
 * 기능: wire timer를 production presenter를 거쳐 화면 모델로 변환한다.
 * 인자: snapshot -> 테스트 snapshot, online -> 연결 상태, received_at -> 최초 수신 시각
 * 반환값: 현재 단계의 단일 지표 묶음
 * 작성 날짜: 2026/09/05
 */
function groups_for(snapshot: BackendTradingIndicatorSnapshot, online = true, received_at = 0) {
    return present_trading_indicators(snapshot, 'Case_C', online, true, received_at);
}

describe('실시간 지표 타이머', () => {
    beforeEach(() => {
        // 수면 없이 실제 React interval과 monotonic 시계를 함께 전진시킨다.
        vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'performance', 'Date'] });
    });
    afterEach(() => {
        // 테스트가 바꾼 전역 환경과 실행 자원을 정리한다.
        cleanup();
        vi.useRealTimers();
        vi.restoreAllMocks();
    });

    it('초 미만을 올리고 전체 시간에 맞는 고정 폭을 유지한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        for (const [remaining, duration, expected] of [
            [0.000000001, 5, '00:01'], [0, 5, '00:00'], [180, 180, '03:00'],
            [10800, 10800, '03:00:00'], [3600, 3600, '01:00:00'], [1, 21600, '00:00:01'],
        ] as const) expect(format_timer_seconds(remaining, duration)).toBe(expected);
    });

    it('서버 측 오래된 평가와 수신 후 시간을 보정하며 PC 시각 변경에 영향받지 않는다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const model = { snapshot: create_timer(), server_time: '2026-09-05T01:00:30Z', received_at: 1000 };

        expect(present_indicator_timer(model, 2000).time).toBe('02:29');  // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.

        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        vi.setSystemTime(new Date('2030-01-01T00:00:00Z'));
        expect(present_indicator_timer(model, 2000).time).toBe('02:29');  // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
    });

    it('매초 감소하고 0에서는 서버 완료를 기다리며 지표 색상을 바꾸지 않는다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const snapshot = create_snapshot(create_timer({ kind: 'hold', duration_seconds: '5', remaining_seconds: '5' }), {
            condition_id: 'b_profit_zone', source: 'realtime', hold_seconds: 5, satisfied: false, value: '0.61', threshold: '0.60',
        });
        const view = render(<RealtimeIndicators groups={groups_for(snapshot)} />);
        expect(screen.getByRole('timer')).toHaveTextContent('00:05');  // 화면의 표시 내용과 입력 가능 상태를 검증한다.

        // 가짜 시간을 진행해 예약된 작업과 후속 상태 반영을 실행한다.
        act(() => vi.advanceTimersByTime(1000));
        expect(screen.getByRole('timer')).toHaveTextContent('00:04');  // 화면의 표시 내용과 입력 가능 상태를 검증한다.

        // 가짜 시간을 진행해 예약된 작업과 후속 상태 반영을 실행한다.
        act(() => vi.advanceTimersByTime(4000));

        // 화면의 표시 내용과 입력 가능 상태를 검증한다.
        expect(screen.getByText('판정 대기')).toBeInTheDocument();
        expect(screen.getByRole('listitem')).toHaveAttribute('data-tone', 'negative');

        // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
        view.rerender(<RealtimeIndicators groups={groups_for(create_snapshot(create_timer({
            kind: 'hold', duration_seconds: '5', remaining_seconds: '0', state: 'completed',
        }), { satisfied: true }))} />);

        // 화면의 표시 내용과 입력 가능 상태를 검증한다.
        expect(screen.getByText('유지 완료')).toBeInTheDocument();
        expect(screen.getByRole('listitem')).toHaveAttribute('data-tone', 'positive');
    });

    it('연속 조건 중단 시 전체 시간에서 멈추고 새 회차는 처음부터 계산한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const view = render(<RealtimeIndicators groups={groups_for(create_snapshot(create_timer({ kind: 'hold', remaining_seconds: '150' })))} />);

        // 가짜 시간을 진행해 예약된 작업과 후속 상태 반영을 실행한다.
        act(() => vi.advanceTimersByTime(1000));
        expect(screen.getByRole('timer')).toHaveTextContent('02:29');  // 화면의 표시 내용과 입력 가능 상태를 검증한다.

        // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
        view.rerender(<RealtimeIndicators groups={groups_for(create_snapshot(create_timer({ kind: 'hold', state: 'stopped', reset_reason: 'condition_broken' })))} />);
        expect(screen.getByText('정지 · 조건 미충족')).toBeInTheDocument();  // 화면의 표시 내용과 입력 가능 상태를 검증한다.

        // 가짜 시간을 진행해 예약된 작업과 후속 상태 반영을 실행한다.
        act(() => vi.advanceTimersByTime(30_000));

        // 화면의 표시 내용과 입력 가능 상태를 검증한다.
        expect(screen.getByRole('timer')).toHaveTextContent('03:00');
        expect(vi.getTimerCount()).toBe(0);

        // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
        view.rerender(<RealtimeIndicators groups={groups_for(create_snapshot(create_timer({ timer_id: 'hold:2', kind: 'hold' })), true, performance.now())} />);
        expect(screen.getByText('진행 중')).toBeInTheDocument();  // 화면의 표시 내용과 입력 가능 상태를 검증한다.

        // 가짜 시간을 진행해 예약된 작업과 후속 상태 반영을 실행한다.
        act(() => vi.advanceTimersByTime(1000));
        expect(screen.getByRole('timer')).toHaveTextContent('02:59');  // 화면의 표시 내용과 입력 가능 상태를 검증한다.
    });

    it.each(['timeout', 'new_low'] as const)('C %s 리셋은 같은 행에서 새 3분과 사유를 표시한다', (reason) => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const view = render(<RealtimeIndicators groups={groups_for(create_snapshot(create_timer({ remaining_seconds: '1' })))} />);
        const original_row = screen.getByRole('listitem');

        // 가짜 시간을 진행해 예약된 작업과 후속 상태 반영을 실행한다.
        act(() => vi.advanceTimersByTime(1000));

        expect(screen.getByText('판정 대기')).toBeInTheDocument();  // 화면의 표시 내용과 입력 가능 상태를 검증한다.

        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const reset = create_snapshot(create_timer({ timer_id: 'recovery:2', reset_reason: reason }), {
            satisfied: null, value: null, threshold: null, evaluated_at: null, market_version: null, context_version: null,
        });

        // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
        view.rerender(<RealtimeIndicators groups={groups_for(reset, true, performance.now())} />);

        // 화면의 표시 내용과 입력 가능 상태를 검증한다.
        expect(screen.getByRole('listitem')).toBe(original_row);
        expect(original_row).toHaveAttribute('data-tone', 'neutral');
        expect(screen.getByRole('timer')).toHaveTextContent('03:00');
        expect(screen.getByText(reason === 'timeout' ? '시간 초과로 재시작' : '저점 갱신으로 재시작')).toBeInTheDocument();
        expect(screen.getByText('진행 중')).toBeInTheDocument();
    });

    it('숨긴 탭과 문서는 interval을 중지하고 다시 보일 때 경과를 복원한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const groups = groups_for(create_snapshot());
        const view = render(<RealtimeIndicators groups={groups} />);

        // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
        view.rerender(<RealtimeIndicators groups={groups} visible={false} />);
        expect(vi.getTimerCount()).toBe(0);  // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.

        // 가짜 시간을 진행해 예약된 작업과 후속 상태 반영을 실행한다.
        act(() => vi.advanceTimersByTime(60_000));
        view.rerender(<RealtimeIndicators groups={groups} visible />);

        expect(screen.getByRole('timer')).toHaveTextContent('02:00');  // 화면의 표시 내용과 입력 가능 상태를 검증한다.

        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');

        // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
        act(() => document.dispatchEvent(new Event('visibilitychange')));
        expect(vi.getTimerCount()).toBe(0);  // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.

        // 가짜 시간을 진행해 예약된 작업과 후속 상태 반영을 실행한다.
        act(() => vi.advanceTimersByTime(60_000));
        visibility.mockReturnValue('visible');
        act(() => document.dispatchEvent(new Event('visibilitychange')));
        expect(screen.getByRole('timer')).toHaveTextContent('01:00');  // 화면의 표시 내용과 입력 가능 상태를 검증한다.
    });

    it('연결 단절·구버전은 확인 대기이며 새 전체 snapshot으로 복원한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const view = render(<RealtimeIndicators groups={groups_for(create_snapshot(), false)} />);

        // 화면의 표시 내용과 입력 가능 상태를 검증한다.
        expect(screen.getByText('확인 대기')).toBeInTheDocument();
        expect(screen.getByRole('timer')).toHaveTextContent('—');
        expect(vi.getTimerCount()).toBe(0);

        // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
        view.rerender(<RealtimeIndicators groups={groups_for(create_snapshot(null))} />);
        expect(screen.getByRole('timer')).toHaveTextContent('—');  // 화면의 표시 내용과 입력 가능 상태를 검증한다.

        // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
        view.rerender(<RealtimeIndicators groups={groups_for(create_snapshot(create_timer(), {}, '2026-09-05T01:01:00Z'))} />);
        expect(screen.getByRole('timer')).toHaveTextContent('02:00');  // 화면의 표시 내용과 입력 가능 상태를 검증한다.
    });

    it('동일 데이터 재전송은 최초 수신 시각을 유지하고 새로운 서버 측정만 보정한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const actor = createActor(create_recent_orders_machine()).start();

        const snapshot = create_snapshot();

        // 가짜 시간을 진행해 예약된 작업과 후속 상태 반영을 실행한다.
        actor.send({ type: 'STRATEGY_INDICATORS_SYNCHRONIZED', indicators: snapshot });
        vi.advanceTimersByTime(10_000);
        actor.send({ type: 'STRATEGY_INDICATORS_SYNCHRONIZED', indicators: structuredClone(snapshot) });

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(actor.getSnapshot().context.strategy_indicators_received_at).toBe(0);
        const context = actor.getSnapshot().context;

        // 준비한 의존성을 주입해 화면 또는 hook을 실행한다.
        render(<RealtimeIndicators groups={groups_for(context.strategy_indicators!, true, context.strategy_indicators_received_at!)} />);
        expect(screen.getByRole('timer')).toHaveTextContent('02:50');  // 화면의 표시 내용과 입력 가능 상태를 검증한다.

        // 화면 또는 실행 수명의 종료를 요청한다.
        actor.stop();  // 화면을 열기 전 받은 타이머도 최초 수신 시각부터 계산한다.
    });

    it('시간 범위·UTC·상태가 잘못된 timer는 거부하고 구버전 생략은 허용한다', () => {
        // 전이 완료 상태와 화면 모델이 기대값을 유지하는지 검증한다.
        expect(is_trading_indicator_snapshot(create_snapshot())).toBe(true);
        expect(is_trading_indicator_snapshot(create_snapshot(null))).toBe(true);
        for (const changes of [
            { remaining_seconds: '-1' }, { remaining_seconds: '181' }, { remaining_seconds: 'NaN' },
            { duration_seconds: '0' }, { duration_seconds: 180 }, { state: 'completed' },
            { state: 'stopped', remaining_seconds: '100' }, { timer_id: '' },
            { sampled_at: '2026-09-05T01:00:00' }, { reset_reason: 'guess' },
        ]) {
            const invalid = create_snapshot({ ...create_timer(), ...changes } as BackendTradingTimer);
            expect(is_trading_indicator_snapshot(invalid)).toBe(false);
        }
        expect(is_trading_indicator_snapshot({ ...create_snapshot(), server_time: null })).toBe(false);
    });
});
