import { fireEvent, render, screen, within } from '@testing-library/react';
import { vi } from 'vitest';
import { TraderPanel } from './TraderPanel';

describe('TraderPanel', () => {
    it('authoritative risk 입력이 없거나 명시적으로 해제됐으면 운영자 경고를 표시하지 않는다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const { rerender } = render(
            <TraderPanel
                activeTab="recent"
                indicatorGroups={[]}
                onIntent={vi.fn()}
                orders={[]}
            />,
        );

        // 화면의 표시 내용과 입력 가능 상태를 검증한다.
        expect(screen.queryByRole('alert')).not.toBeInTheDocument();

        rerender(
            <TraderPanel
                activeTab="recent"
                configured_risk_policy_version={2}
                indicatorGroups={[]}
                last_risk_decision_allowed={null}
                manual_kill_active={false}
                onIntent={vi.fn()}
                orders={[]}
                process_ownership_ambiguous={false}
                risk_block_reason={null}
                risk_policy_availability="CONFIGURED"
                session_risk_policy_version={2}
            />,
        );

        // false/null은 백엔드가 안전 차단을 해제한 authoritative 상태이므로 경고를 만들지 않는다.
        expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    });

    it('risk policy unavailable과 session policy version 불일치를 차단 시도 전에도 표시한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const { rerender } = render(
            <TraderPanel
                activeTab="recent"
                configured_risk_policy_version={null}
                indicatorGroups={[]}
                orders={[]}
                risk_block_reason={null}
                risk_policy_availability="UNAVAILABLE"
                session_risk_policy_version={null}
            />,
        );

        // 화면의 표시 내용과 입력 가능 상태를 검증한다.
        expect(screen.getByText(/승인된 위험 정책을 확인할 수 없어/u)).toBeInTheDocument();

        rerender(
            <TraderPanel
                activeTab="recent"
                configured_risk_policy_version={4}
                indicatorGroups={[]}
                orders={[]}
                risk_block_reason={null}
                risk_policy_availability="CONFIGURED"
                session_risk_policy_version={3}
            />,
        );

        // Version 숫자를 DOM에 복제하지 않고 mismatch에 필요한 안전 조치만 표시한다.
        expect(screen.getByText('현재 세션과 위험 정책 버전이 일치하지 않습니다.'))
            .toBeInTheDocument();
    });

    it('configured-unbounded 정책을 unavailable·version mismatch와 다른 운영 상태로 표시한다', () => {
        // 준비한 의존성을 주입해 화면 또는 hook을 실행한다.
        render(
            <TraderPanel
                activeTab="recent"
                configured_risk_policy_version={4}
                daily_loss_scope="REALIZED_ONLY"
                indicatorGroups={[]}
                manual_kill_behavior="CANCEL_AND_LIQUIDATE"
                max_daily_loss={null}
                max_order_notional={null}
                max_position_notional={null}
                orders={[]}
                risk_policy_availability="CONFIGURED"
                session_risk_policy_version={4}
            />,
        );

        const notice = screen.getByRole('alert', { name: '운영자 확인 필요' });

        // 화면의 표시 내용과 입력 가능 상태를 검증한다.
        expect(within(notice).getByText(/세 금액 상한은 명시적으로 무제한/u))
            .toBeInTheDocument();
        expect(within(notice).queryByText(/위험 정책을 확인할 수 없어/u))
            .not.toBeInTheDocument();
        expect(within(notice).queryByText(/정책 버전이 일치하지 않습니다/u))
            .not.toBeInTheDocument();
    });

    it('configured-unbounded에서도 계산된 노출·PnL·source version을 전체 표시한다', () => {
        // 준비한 의존성을 주입해 화면 또는 hook을 실행한다.
        render(
            <TraderPanel
                activeTab="recent"
                indicatorGroups={[]}
                last_risk_budget={{
                    policy_version: 4,
                    market_version: 7,
                    account_version: 3,
                    context_version: 9,
                    current_position_notional: '125.50',
                    reserved_buy_notional: '24.25',
                    candidate_order_notional: '50.25',
                    projected_position_notional: '200.00',
                    daily_realized_pnl: '-12.75',
                    unrealized_pnl: '-3.50',
                    daily_loss: '12.75',
                    manual_kill_active: false,
                }}
                last_risk_decision_allowed
                orders={[]}
            />,
        );

        const budget_section = screen.getByRole('region', {
            name: '마지막 BUY 위험 예산',
        });

        // Decimal은 JS number 계산 없이 정밀도와 USDT 단위를 그대로 보존한다.
        expect(within(budget_section).getByText('125.50 USDT')).toBeInTheDocument();
        expect(within(budget_section).getByText('24.25 USDT')).toBeInTheDocument();
        expect(within(budget_section).getByText('50.25 USDT')).toBeInTheDocument();
        expect(within(budget_section).getByText('200.00 USDT')).toBeInTheDocument();
        expect(within(budget_section).getByText('-12.75 USDT')).toBeInTheDocument();
        expect(within(budget_section).getByText('-3.50 USDT')).toBeInTheDocument();
        expect(budget_section).toHaveTextContent(
            'policy v4 · market v7 · account v3 · context v9',
        );
        expect(budget_section).toHaveTextContent('판정 시 안전 차단 비활성');
    });

    it('차단 판정만 전달된 불완전 component 입력도 성공으로 오인하지 않는다', () => {
        // 준비한 의존성을 주입해 화면 또는 hook을 실행한다.
        render(
            <TraderPanel
                activeTab="recent"
                indicatorGroups={[]}
                last_risk_decision_allowed={false}
                orders={[]}
            />,
        );

        // 화면의 표시 내용과 입력 가능 상태를 검증한다.
        expect(screen.getByText('백엔드 위험 보호 장치가 신규 매수를 차단했습니다.'))
            .toBeInTheDocument();
    });

    it.each([
        ['RISK_POLICY_UNAVAILABLE', '승인된 위험 정책을 확인할 수 없어 신규 매수가 차단되었습니다.'],
        ['RISK_POLICY_VERSION_MISMATCH', '현재 세션과 위험 정책 버전이 일치하지 않습니다.'],
        ['MANUAL_KILL_SWITCH_ACTIVE', '수동 안전 차단이 활성화되어 신규 매수가 중지되었습니다.'],
        ['RISK_ORDER_NOTIONAL_EXCEEDED', '요청한 단건 주문 금액이 승인된 위험 한도를 초과했습니다.'],
        ['RISK_DAILY_LOSS_EXCEEDED', '오늘의 누적 손실이 승인된 위험 한도에 도달했습니다.'],
        ['RISK_POSITION_NOTIONAL_EXCEEDED', '예상 포지션 금액이 승인된 누적 위험 한도를 초과했습니다.'],
    ])('%s typed risk 차단을 안전한 운영자 문구로 표시한다', (risk_block_reason, summary) => {
        // 준비한 의존성을 주입해 화면 또는 hook을 실행한다.
        render(
            <TraderPanel
                activeTab="recent"
                indicatorGroups={[]}
                orders={[]}
                risk_block_reason={risk_block_reason}
            />,
        );

        const notice = screen.getByRole('alert', { name: '운영자 확인 필요' });

        // 화면의 표시 내용과 입력 가능 상태를 검증한다.
        expect(within(notice).getByText(summary)).toBeInTheDocument();
        expect(within(notice).getByText(/^운영자 조치:/u)).toBeInTheDocument();
    });

    it('manual kill과 process ownership 모호성을 하나의 접근 가능한 운영자 surface에 표시한다', () => {
        // 준비한 의존성을 주입해 화면 또는 hook을 실행한다.
        render(
            <TraderPanel
                activeTab="recent"
                indicatorGroups={[]}
                manual_kill_active
                orders={[]}
                process_ownership_ambiguous
                risk_block_reason={null}
            />,
        );

        const notice = screen.getByRole('alert', { name: '운영자 확인 필요' });
        const action_list = within(notice).getByRole('list', {
            name: '안전 차단 상태와 운영자 조치',
        });

        // 비대화식 운영 지침은 keyboard trap이나 동작하지 않는 가짜 action button을 만들지 않는다.
        expect(within(action_list).getAllByRole('listitem')).toHaveLength(2);
        expect(within(notice).getByText(/수동 안전 차단이 활성화/u)).toBeInTheDocument();
        expect(within(notice).getByText(/프로세스의 소유권을 확인할 수 없습니다/u))
            .toBeInTheDocument();
        expect(within(notice).queryByRole('button')).not.toBeInTheDocument();
    });

    it('manual kill activation과 cleanup 완료를 분리해 미완료 상태를 추가 경고한다', () => {
        // 준비한 의존성을 주입해 화면 또는 hook을 실행한다.
        render(
            <TraderPanel
                activeTab="recent"
                indicatorGroups={[]}
                manual_kill_active
                manual_kill_cleanup_complete={false}
                orders={[]}
                risk_block_reason={null}
            />,
        );

        const notice = screen.getByRole('alert', { name: '운영자 확인 필요' });

        // 화면의 표시 내용과 입력 가능 상태를 검증한다.
        expect(within(notice).getByText(/수동 안전 차단이 활성화/u)).toBeInTheDocument();
        expect(within(notice).getByText(/주문 정리와 보유 수량 청산이 아직 완료되지 않았/u))
            .toBeInTheDocument();
    });

    it('policy hot-swap 뒤 활성 epoch에 고정된 cleanup provenance를 별도 경고한다', () => {
        // 준비한 의존성을 주입해 화면 또는 hook을 실행한다.
        render(
            <TraderPanel
                activeTab="recent"
                configured_risk_policy_version={5}
                indicatorGroups={[]}
                manual_kill_active
                manual_kill_activation_behavior="CANCEL_AND_LIQUIDATE"
                manual_kill_activation_policy_version={4}
                manual_kill_behavior="BLOCK_NEW_ORDERS"
                manual_kill_cleanup_complete={false}
                orders={[]}
                risk_block_reason={null}
            />,
        );

        const notice = screen.getByRole('alert', { name: '운영자 확인 필요' });

        // 화면의 표시 내용과 입력 가능 상태를 검증한다.
        expect(within(notice).getByText(/활성화 당시 정책 v4/u)).toHaveTextContent(
            '주문 취소 및 포지션 청산',
        );
        expect(within(notice).getByText(/현재 구성 정책이 바뀌었더라도/u))
            .toBeInTheDocument();
    });

    it('allowlist 밖 risk reason에 credential이나 raw payload가 섞여도 원문을 표시하지 않는다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const raw_secret = 'RISK_POLICY_UNAVAILABLE apiKey=raw-secret-marker';

        // 준비한 의존성을 주입해 화면 또는 hook을 실행한다.
        render(
            <TraderPanel
                activeTab="recent"
                indicatorGroups={[]}
                orders={[]}
                risk_block_reason={raw_secret}
            />,
        );

        const notice = screen.getByRole('alert', { name: '운영자 확인 필요' });

        // 화면의 표시 내용과 입력 가능 상태를 검증한다.
        expect(within(notice).getByText('백엔드 위험 보호 장치가 신규 매수를 차단했습니다.'))
            .toBeInTheDocument();
        expect(screen.queryByText(raw_secret)).not.toBeInTheDocument();
        expect(document.body).not.toHaveTextContent('raw-secret-marker');
    });

    it('선택 탭만 tab 순서에 두고 tab과 tabpanel을 양방향으로 연결한다', () => {
        // 준비한 의존성을 주입해 화면 또는 hook을 실행한다.
        render(
            <TraderPanel
                activeTab="recent"
                indicatorGroups={[]}
                onIntent={vi.fn()}
                orders={[]}
            />,
        );

        const recent_tab = screen.getByRole('tab', { name: '체결 내역' });
        const realtime_tab = screen.getByRole('tab', { name: '실시간 지표' });
        const tab_panels = screen.getAllByRole('tabpanel', { hidden: true });
        const recent_panel = tab_panels.find((panel) => panel.id === 'trader-tabpanel-recent');
        const realtime_panel = tab_panels.find((panel) => panel.id === 'trader-tabpanel-realtime');

        // 선택 탭 하나만 순차 focus 대상이며 두 ARIA ID 모두 실제 상대 panel을 가리켜야 한다.
        expect(recent_tab).toHaveAttribute('aria-selected', 'true');
        expect(recent_tab).toHaveAttribute('aria-controls', 'trader-tabpanel-recent');
        expect(recent_tab).toHaveAttribute('tabindex', '0');
        expect(realtime_tab).toHaveAttribute('aria-selected', 'false');
        expect(realtime_tab).toHaveAttribute('aria-controls', 'trader-tabpanel-realtime');
        expect(realtime_tab).toHaveAttribute('tabindex', '-1');
        expect(recent_panel).toHaveAttribute('aria-labelledby', 'trader-tab-recent');
        expect(recent_panel).not.toHaveAttribute('hidden');
        expect(realtime_panel).toHaveAttribute('aria-labelledby', 'trader-tab-realtime');
        expect(realtime_panel).toHaveAttribute('hidden');
    });

    it.each([
        {
            active_tab: 'recent' as const,
            key: 'ArrowRight',
            source_label: '체결 내역',
            target_label: '실시간 지표',
            target_tab: 'realtime' as const,
        },
        {
            active_tab: 'recent' as const,
            key: 'ArrowLeft',
            source_label: '체결 내역',
            target_label: '실시간 지표',
            target_tab: 'realtime' as const,
        },
        {
            active_tab: 'realtime' as const,
            key: 'ArrowRight',
            source_label: '실시간 지표',
            target_label: '체결 내역',
            target_tab: 'recent' as const,
        },
        {
            active_tab: 'realtime' as const,
            key: 'Home',
            source_label: '실시간 지표',
            target_label: '체결 내역',
            target_tab: 'recent' as const,
        },
        {
            active_tab: 'recent' as const,
            key: 'End',
            source_label: '체결 내역',
            target_label: '실시간 지표',
            target_tab: 'realtime' as const,
        },
    ])('$key가 $source_label에서 $target_label 탭으로 focus와 intent를 이동한다', ({
        active_tab,
        key,
        source_label,
        target_label,
        target_tab,
    }) => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const handle_intent = vi.fn();

        // 준비한 의존성을 주입해 화면 또는 hook을 실행한다.
        render(
            <TraderPanel
                activeTab={active_tab}
                indicatorGroups={[]}
                onIntent={handle_intent}
                orders={[]}
            />,
        );

        const source_tab = screen.getByRole('tab', { name: source_label });
        const target_tab_element = screen.getByRole('tab', { name: target_label });

        source_tab.focus();
        fireEvent.keyDown(source_tab, { key });

        // Controlled rerender 전에도 focus를 이동하고 선택 변경은 단 하나의 intent로 위임한다.
        expect(target_tab_element).toHaveFocus();
        expect(handle_intent).toHaveBeenCalledTimes(1);
        expect(handle_intent).toHaveBeenCalledWith({
            type: 'TRADER_TAB_REQUESTED',
            tab: target_tab,
        });
    });

    it('CR-06: 실시간 지표 탭 요청을 controlled intent로 전달한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const handle_intent = vi.fn();

        // 준비한 의존성을 주입해 화면 또는 hook을 실행한다.
        render(
            <TraderPanel
                activeTab="recent"
                indicatorGroups={[]}
                onIntent={handle_intent}
                orders={[]}
            />,
        );

        fireEvent.click(screen.getByRole('tab', { name: '실시간 지표' }));

        // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
        expect(handle_intent).toHaveBeenCalledWith({
            type: 'TRADER_TAB_REQUESTED',
            tab: 'realtime',
        });
    });

    it('Case 3.1: 전체 보기 intent를 전달한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const handle_intent = vi.fn();

        // 준비한 의존성을 주입해 화면 또는 hook을 실행한다.
        render(
            <TraderPanel
                activeTab="recent"
                indicatorGroups={[]}
                onIntent={handle_intent}
                orders={[]}
            />,
        );

        fireEvent.click(screen.getByRole('button', { name: '전체 보기' }));
        expect(handle_intent).toHaveBeenCalledWith({ type: 'ALL_ORDERS_REQUESTED' });  // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
    });

    /** Communication Case 3 메시지 1의 optional boundary handler 부재 경계를 검증한다. */
    it('test_all_orders_without_handler_is_safe_no_op: handler가 없으면 전체 보기는 no-op이다', () => {
        // 준비한 의존성을 주입해 화면 또는 hook을 실행한다.
        render(
            <TraderPanel
                activeTab="recent"
                indicatorGroups={[]}
                orders={[]}
            />,
        );

        // 읽기 전용 렌더에서도 사용자 click이 예외나 숨은 navigation을 만들지 않아야 한다.
        expect(() => fireEvent.click(screen.getByRole('button', { name: '전체 보기' })))
            .not.toThrow();
    });
});
