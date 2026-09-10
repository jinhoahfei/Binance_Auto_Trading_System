import { render, screen, within } from '@testing-library/react';
import { DashboardPage } from '../../routes/dashboard/DashboardPage';
import type { BackendSnapshot, BackendTradingSnapshot, RegimeType } from '../../shared/contracts';
import {
    map_backend_event_to_intents,
    map_backend_snapshot,
    validate_backend_snapshot,
} from '../../shared/api/backendEventMapper';
import {
    create_backend_event_fixture,
    create_backend_snapshot_fixture,
    TEST_BACKEND_SESSION_ID,
} from '../../shared/api/backendTestFixtures';
import { FakeUiCommandAdapter } from '../../shared/testing';
import { UiApplicationFacade } from '../control';
import { present_dashboard_props } from './dashboardPresenter';

/**
 * 함수 이름: create_strategy_snapshot()
 * 기능: 실제 transport 검증을 거칠 실행 전략과 lifecycle의 backend snapshot을 생성한다.
 * 인자: active_logic -> backend 실행 STM 표시 정보
 *      status -> 세션 lifecycle, version -> backend Context version
 * 반환값: coherent backend snapshot fixture
 * 작성 날짜: 2026/09/05
 */
function create_strategy_snapshot(
    active_logic: BackendTradingSnapshot['active_logic'],
    status: BackendTradingSnapshot['status'] = 'running',
    version = 1,
): BackendSnapshot {
    // 최근 체결과 선택 REGIME는 실행 전략의 원천이 아니므로 고정하고 STM 정보만 바꾼다.
    const snapshot = create_backend_snapshot_fixture();
    return {
        ...snapshot,
        regime: { ...snapshot.regime, selected: 'type0' },
        trading: {
            ...snapshot.trading,
            status,
            version,
            session_id: TEST_BACKEND_SESSION_ID,
            ...(active_logic === undefined ? {} : { active_logic }),
        },
    };
}

/**
 * 함수 이름: expect_shared_strategy()
 * 기능: 화면의 ACTIVE STATE와 계좌 현재 상태가 같은 전략 원문을 표시하는지 확인한다.
 * 인자: strategy_label -> 기대하는 실행 전략 문구
 * 반환값: 없음
 * 작성 날짜: 2026/09/05
 */
function expect_shared_strategy(strategy_label: string): void {
    // 두 독립 Boundary의 실제 DOM을 조회해 presenter 값만 일치하는 오류를 놓치지 않는다.
    const chart_state = screen.getByLabelText('현재 실행 전략');
    const account_state = screen.getByRole('article', { name: '전략 상태' });
    expect(within(chart_state).getByText(strategy_label)).toBeInTheDocument();
    expect(within(account_state).getByText(strategy_label)).toBeInTheDocument();
    expect(chart_state).toHaveAttribute('title', strategy_label);  // 좁은 폭에서도 전체 전략을 확인한다.
}

describe('현재 실행 TradingSTM 전략 표시', () => {
    it('Case·단계·지표는 facade 구독자에게 하나의 원자적 상태로 발행된다', () => {
        const initial_mapping = map_backend_snapshot(create_strategy_snapshot({
            regime_type: 'type0', root_state: 'TRADE_MANAGEMENT', active_strategies: ['CASE_B'],
            indicators: { phase_key: 'B_WAIT_SIGNAL', notice: null, conditions: [] },
        }), '2026-09-05');
        const facade = new UiApplicationFacade(new FakeUiCommandAdapter(), initial_mapping.facade_options);
        facade.start();
        const observations: string[] = [];
        const unsubscribe = facade.subscribe(() => {
            const view = facade.get_view_model();
            observations.push(`${view.account_summary.strategy.appliedState}:${view.trader_panel.strategy_indicators?.phase_key}`);
        });
        observations.length = 0;  // subscribe가 즉시 알리는 초기 상태는 전환 검증에서 제외한다.
        try {
            const next = create_strategy_snapshot({
                regime_type: 'type0', root_state: 'TRADE_MANAGEMENT', active_strategies: ['CASE_C'],
                indicators: { phase_key: 'CASE_C_TP_TRAILING', notice: null, conditions: [] },
            }, 'running', 2);
            const event = { ...create_backend_event_fixture(2, 'TRADING_SESSION_UPDATED', { trading: next.trading }), aggregate_version: 2 };
            map_backend_event_to_intents(event).forEach((intent) => facade.dispatch(intent));
            expect(observations).toEqual(['Case_C:CASE_C_TP_TRAILING']);
        } finally {
            unsubscribe();
            facade.stop();
        }
    });

    it('초기 로딩·Case 전환 event·종료·재연결에서 두 영역의 실제 표시가 일치한다', () => {
        // 초기 snapshot을 mapper와 facade를 통해 주입해 live 초기화 경로를 그대로 검증한다.
        const initial_snapshot = create_strategy_snapshot({
            regime_type: 'type0', root_state: 'LOWER_TOUCH_WATCH', active_strategies: [],
        });
        const initial_mapping = map_backend_snapshot(
            validate_backend_snapshot(initial_snapshot), '2026-09-05',
        );
        const facade = new UiApplicationFacade(new FakeUiCommandAdapter(), initial_mapping.facade_options);
        facade.start();
        const rendered = render(<DashboardPage {...present_dashboard_props(facade.get_view_model(), facade)} />);

        try {
            expect_shared_strategy('하단 밴드 대기');
            // 실제 event mapping 경로에서 포지션 Case와 병렬 감시를 번갈아 전달한다.
            const active_strategy_cases = [
                { strategies: ['CASE_B', 'CASE_C'], label: 'Case_B / Case_C' },
                { strategies: ['CASE_B'], label: 'Case_B' },
                { strategies: ['CASE_C'], label: 'Case_C' },
            ] as const;
            for (const [index, strategy_case] of active_strategy_cases.entries()) {
                const snapshot = create_strategy_snapshot({
                    regime_type: 'type0',
                    root_state: 'TRADE_MANAGEMENT',
                    active_strategies: strategy_case.strategies,
                }, 'running', index + 2);
                const event = {
                    ...create_backend_event_fixture(index + 10, 'TRADING_SESSION_UPDATED', {
                        trading: snapshot.trading,
                    }),
                    aggregate_version: snapshot.trading.version,
                };
                map_backend_event_to_intents(event).forEach((intent) => facade.dispatch(intent));
                rendered.rerender(<DashboardPage {...present_dashboard_props(facade.get_view_model(), facade)} />);
                expect_shared_strategy(strategy_case.label);
                expect(facade.get_view_model().chart.active_trading_logic_state).toBe(strategy_case.label);
            }

            // 종료 event는 이전 Case를 제거하고 full resync는 서버의 최신 전략을 두 영역에 복원한다.
            const terminated = create_strategy_snapshot(null, 'terminated', 5);
            const terminated_event = {
                ...create_backend_event_fixture(13, 'TRADING_SESSION_UPDATED', { trading: terminated.trading }),
                aggregate_version: 5,
            };
            map_backend_event_to_intents(terminated_event).forEach((intent) => facade.dispatch(intent));
            rendered.rerender(<DashboardPage {...present_dashboard_props(facade.get_view_model(), facade)} />);
            expect_shared_strategy('자동매매 종료');
            const restored = create_strategy_snapshot({
                regime_type: 'type0', root_state: 'TRADE_MANAGEMENT', active_strategies: ['CASE_B'],
            }, 'running', 6);
            facade.dispatch({
                type: 'BACKEND_SNAPSHOT_SYNCHRONIZED',
                snapshot: map_backend_snapshot(validate_backend_snapshot(restored), '2026-09-05').server_snapshot,
            });
            rendered.rerender(<DashboardPage {...present_dashboard_props(facade.get_view_model(), facade)} />);
            expect_shared_strategy('Case_B');
            expect(facade.get_view_model().chart.active_trading_logic_state).toBe('Case_B');
        } finally {
            facade.stop();  // 실패한 검증에서도 actor 구독이 다음 테스트로 남지 않게 한다.
        }
    });

    it.each<RegimeType>(['type0', 'type1', 'type2', 'type3', 'type4'])(
        '선택 REGIME %s의 별칭으로 실제 실행 Case를 덮어쓰지 않는다', (selected_regime) => {
            // 적용·추천·후보 값과 실제 실행 Case를 의도적으로 다르게 만든다.
            const snapshot = create_strategy_snapshot({
                regime_type: 'type0', root_state: 'TRADE_MANAGEMENT', active_strategies: ['CASE_C'],
            });
            const mapped = map_backend_snapshot(validate_backend_snapshot(snapshot), '2026-09-05');
            const facade = new UiApplicationFacade(new FakeUiCommandAdapter(), mapped.facade_options);
            facade.start();
            try {
                const view_model = facade.get_view_model();
                const props = present_dashboard_props({
                    ...view_model,
                    regime: {
                        ...view_model.regime,
                        applied: selected_regime, recommended: 'type4', candidate: 'type1',
                    },
                }, facade);
                expect(props.chart.activeState).toBe('Case_C');
                expect(props.account.strategy.appliedState).toBe(props.chart.activeState);
            } finally {
                facade.stop();  // 실행 전략을 검증하는 테스트는 매매 명령을 호출하지 않는다.
            }
        },
    );

    it.each([
        ['not_started', null, '매매 시작 전'],
        ['running', undefined, '전략 확인 대기'],
        ['running', null, '전략 확인 대기'],
        ['stopping', null, '중지 처리 중'],
        ['reconciliation_required', null, '주문 상태 확인 필요'],
        ['terminated', null, '자동매매 종료'],
    ] as const)('전략 정보가 없는 %s 상태를 올바르게 표시한다', (status, active_logic, label) => {
        // 구버전 payload와 비활성 session의 전략 문구도 raw lifecycle 값 대신 명시적으로 표시한다.
        const snapshot = validate_backend_snapshot(create_strategy_snapshot(active_logic, status));
        expect(map_backend_snapshot(snapshot, '2026-09-05').server_snapshot.account_strategy.appliedState)
            .toBe(label);
    });

    it.each([
        ['running', 'TRADE_MANAGEMENT', [], '신호 대기'],
        ['stopping', 'STOPPING', [], '중지 처리 중'],
        ['stopping', 'STOPPING', ['CASE_B'], '중지 처리 중'],
        ['reconciliation_required', 'TRADE_MANAGEMENT', ['CASE_C'], '주문 상태 확인 필요'],
    ] as const)('활성 로직이 남아 있어도 %s lifecycle을 정확하게 표시한다', (status, root_state, active_strategies, label) => {
        // 중지·주문 확인 상태를 기존 Case 이름이 가리지 않아야 한다.
        const snapshot = validate_backend_snapshot(create_strategy_snapshot({
            regime_type: 'type0', root_state, active_strategies,
        }, status));
        expect(map_backend_snapshot(snapshot, '2026-09-05').server_snapshot.account_strategy.appliedState)
            .toBe(label);
    });

    it.each(['reconciliation_required', 'stopping'] as const)(
        '%s에서는 이전 지표를 숨기고 전체 재동기화와 이벤트 수신 후에도 정상 작동으로 표시하지 않는다',
        (status) => {
            const logic: NonNullable<BackendTradingSnapshot['active_logic']> = {
                regime_type: 'type0', root_state: 'LOWER_TOUCH_WATCH', active_strategies: [],
                indicators: { phase_key: 'LOWER_TOUCH_WATCH', notice: null, conditions: [{
                    condition_id: 'lower_price', strategy: null, phase: 'LOWER_TOUCH_WATCH',
                    value: '101', threshold: '100', comparison: '<=', satisfied: false,
                    source: 'realtime', hold_seconds: null, evaluated_at: '2026-09-10T00:00:00Z',
                    market_version: 1, context_version: 1,
                }] },
            };
            const cold_mapping = map_backend_snapshot(validate_backend_snapshot(create_strategy_snapshot(logic, status)), '2026-09-10');
            const cold = new UiApplicationFacade(new FakeUiCommandAdapter(), cold_mapping.facade_options);
            cold.start();
            try { expect(cold.get_view_model().trading.lifecycle_status).toBe(status); }
            finally { cold.stop(); }
            const initial = map_backend_snapshot(validate_backend_snapshot(create_strategy_snapshot(logic)), '2026-09-10');
            const facade = new UiApplicationFacade(new FakeUiCommandAdapter(), initial.facade_options);
            facade.start();
            try {
                facade.dispatch({ type: 'BACKEND_SNAPSHOT_SYNCHRONIZED', snapshot: initial.server_snapshot });
                expect(present_dashboard_props(facade.get_view_model(), facade).account.strategy.status).toBe('정상 작동');
                const stopped = validate_backend_snapshot(create_strategy_snapshot(logic, status, 2));
                const intents = map_backend_event_to_intents({
                    ...create_backend_event_fixture(11, 'TRADING_SESSION_UPDATED', { trading: stopped.trading }),
                    aggregate_version: 2,
                });
                for (const intent of intents) facade.dispatch(intent);
                for (const resync of [false, true]) {
                    if (resync) facade.dispatch({ type: 'BACKEND_SNAPSHOT_SYNCHRONIZED', snapshot: map_backend_snapshot(stopped, '2026-09-10').server_snapshot });
                    const props = present_dashboard_props(facade.get_view_model(), facade);
                    expect(props.account.strategy.status).toBe(status === 'stopping' ? '중지 처리 중' : '주문 상태 확인 필요');
                    expect(props.chart.activeState).toBe(props.account.strategy.status);
                    const group = props.trader.indicatorGroups![0]!;
                    expect(group.notice).toContain('중단');
                    expect(group.indicators[0]).toMatchObject({ value: '—', tone: 'neutral' });
                }
                // 복구 후 RUNNING snapshot을 받으면 현재 판정을 다시 표시한다.
                facade.dispatch({ type: 'BACKEND_SNAPSHOT_SYNCHRONIZED', snapshot: initial.server_snapshot });
                expect(present_dashboard_props(facade.get_view_model(), facade).trader.indicatorGroups![0]!.indicators[0])
                    .toMatchObject({ value: '101.00', tone: 'negative' });
            } finally { facade.stop(); }
        },
    );

    it.each([
        { regime_type: 'type5', root_state: 'TRADE_MANAGEMENT', active_strategies: ['CASE_B'] },
        { regime_type: 'type0', root_state: 'INVALID', active_strategies: [] },
        { regime_type: 'type0', root_state: 'TRADE_MANAGEMENT', active_strategies: ['CASE_D'] },
        { regime_type: 'type0', root_state: 'TRADE_MANAGEMENT', active_strategies: ['CASE_B', 'CASE_B'] },
        { regime_type: 'type0', root_state: 'TRADE_MANAGEMENT', active_strategies: 'CASE_C' },
        { regime_type: 'type0', root_state: 'LOWER_TOUCH_WATCH', active_strategies: ['CASE_C'] },
    ])('잘못된 실행 전략 DTO를 화면 상태로 수락하지 않는다: %j', (active_logic) => {
        // Snapshot과 실시간 event가 동일한 runtime 검증을 통과해야 한다.
        const snapshot = create_strategy_snapshot(null);
        const invalid_trading = { ...snapshot.trading, active_logic };
        expect(() => validate_backend_snapshot({ ...snapshot, trading: invalid_trading })).toThrow();
        expect(() => map_backend_event_to_intents({
            ...create_backend_event_fixture(10, 'TRADING_SESSION_UPDATED', { trading: invalid_trading }),
            aggregate_version: invalid_trading.version,
        })).toThrow();
    });
});
