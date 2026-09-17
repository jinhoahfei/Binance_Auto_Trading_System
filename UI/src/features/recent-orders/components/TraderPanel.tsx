import { useLayoutEffect, useRef } from 'react';
import type { KeyboardEvent } from 'react';

import { RealtimeIndicators } from './RealtimeIndicators';
import { RecentOrdersList } from './RecentOrdersList';
import type {
    TraderPanelProps,
    TraderPanelRiskPolicyAvailability,
    TraderPanelTab,
} from '../types';
import { format_quote_amount } from '../../../shared/formatting';
import styles from './TraderPanel.module.css';

const TAB_OPTIONS: ReadonlyArray<{ readonly tab: TraderPanelTab; readonly label: string }> = [
    { tab: 'recent', label: '체결 내역' },
    { tab: 'realtime', label: '실시간 지표' },
];

interface RiskOperatorNotice {
    readonly action: string;
    readonly id: string;
    readonly summary: string;
}

const RISK_OPERATOR_NOTICE_BY_REASON: Readonly<Record<string, RiskOperatorNotice>> = {
    RISK_POLICY_UNAVAILABLE: {
        id: 'risk-policy-unavailable',
        summary: '승인된 위험 정책을 확인할 수 없어 신규 매수가 차단되었습니다.',
        action: '위험 정책을 복구한 뒤 최신 백엔드 상태를 다시 동기화하세요.',
    },
    RISK_POLICY_VERSION_MISMATCH: {
        id: 'risk-policy-version-mismatch',
        summary: '현재 세션과 위험 정책 버전이 일치하지 않습니다.',
        action: '진행 중인 주문을 같은 ID로 조정한 뒤 정책 버전을 확인하세요.',
    },
    MANUAL_KILL_SWITCH_ACTIVE: {
        id: 'manual-kill-active',
        summary: '수동 안전 차단이 활성화되어 신규 매수가 중지되었습니다.',
        action: '현재 노출과 주문 상태를 확인한 뒤 안전 차단을 명시적으로 해제하세요.',
    },
    RISK_ORDER_NOTIONAL_EXCEEDED: {
        id: 'risk-order-notional-exceeded',
        summary: '요청한 단건 주문 금액이 승인된 위험 한도를 초과했습니다.',
        action: '같은 주문을 즉시 재시도하지 말고 승인된 단건 한도를 확인하세요.',
    },
    RISK_DAILY_LOSS_EXCEEDED: {
        id: 'risk-daily-loss-exceeded',
        summary: '오늘의 누적 손실이 승인된 위험 한도에 도달했습니다.',
        action: '당일 손익과 거래 내역을 조정한 뒤 다음 거래 가능 시점을 확인하세요.',
    },
    RISK_POSITION_NOTIONAL_EXCEEDED: {
        id: 'risk-position-notional-exceeded',
        summary: '예상 포지션 금액이 승인된 누적 위험 한도를 초과했습니다.',
        action: '현재 포지션과 미체결 매수 예약을 조정한 뒤 다시 확인하세요.',
    },
};

const UNKNOWN_RISK_OPERATOR_NOTICE: RiskOperatorNotice = {
    id: 'unknown-risk-block',
    summary: '백엔드 위험 보호 장치가 신규 매수를 차단했습니다.',
    action: '화면의 원문 대신 백엔드의 typed 상태와 승인된 운영 절차를 확인하세요.',
};

const PROCESS_OWNERSHIP_OPERATOR_NOTICE: RiskOperatorNotice = {
    id: 'process-ownership-ambiguous',
    summary: '실행 중인 프로세스의 소유권을 확인할 수 없습니다.',
    action: '새 sidecar를 실행하거나 기존 프로세스를 강제 종료하지 말고 프로세스 identity를 조정하세요.',
};

const CONFIGURED_UNBOUNDED_OPERATOR_NOTICE: RiskOperatorNotice = {
    id: 'configured-unbounded-risk-policy',
    summary: '위험 정책은 구성되어 있지만 세 금액 상한은 명시적으로 무제한입니다.',
    action: '전략 손절과 운영 위험 상한은 별개이므로 현재 승인 범위와 수동 안전 차단 절차를 확인하세요.',
};

const MANUAL_KILL_CLEANUP_PENDING_OPERATOR_NOTICE: RiskOperatorNotice = {
    id: 'manual-kill-cleanup-pending',
    summary: '수동 안전 차단은 활성화됐지만 주문 정리와 보유 수량 청산이 아직 완료되지 않았습니다.',
    action: '완료 상태가 확인될 때까지 새 주문이나 차단 해제를 시도하지 말고 authoritative 주문·포지션 상태를 확인하세요.',
};

const MANUAL_KILL_BEHAVIOR_LABEL_BY_VALUE: Readonly<Record<string, string>> = {
    BLOCK_NEW_ORDERS: '신규 주문 차단',
    CANCEL_AND_LIQUIDATE: '주문 취소 및 포지션 청산',
};


/**
 * 함수 이름: create_risk_operator_notices()
 * 기능: authoritative risk 상태를 raw payload가 없는 allowlist 경고와 운영자 조치로 변환한다.
 * 인자: risk_block_reason -> backend typed 차단 사유 또는 null/미수신
 *      risk_policy_availability -> 승인 policy의 authoritative availability 또는 미수신
 *      configured_risk_policy_version -> 현재 승인 policy version 또는 null/미수신
 *      max_order_notional -> 단건 BUY 상한 Decimal 문자열, 명시적 무제한 null 또는 미수신
 *      max_position_notional -> 누적 Position 상한 Decimal 문자열, 명시적 무제한 null 또는 미수신
 *      max_daily_loss -> KST 일일 손실 상한 Decimal 문자열, 명시적 무제한 null 또는 미수신
 *      daily_loss_scope -> configured policy의 일일 손실 계산 범위 또는 null/미수신
 *      manual_kill_behavior -> configured policy의 수동 안전 차단 동작 또는 null/미수신
 *      session_risk_policy_version -> session이 고정한 policy version 또는 null/미수신
 *      last_risk_decision_allowed -> 마지막 BUY risk 판정 또는 null/미수신
 *      manual_kill_active -> manual kill의 authoritative 활성 상태 또는 미수신
 *      manual_kill_cleanup_complete -> activation 뒤 주문 정리·청산 완료 여부 또는 미수신
 *      manual_kill_activation_behavior -> 현재 활성 epoch에 고정된 동작 또는 null/미수신
 *      manual_kill_activation_policy_version -> 현재 활성 epoch에 고정된 policy version 또는 null/미수신
 *      process_ownership_ambiguous -> process ownership의 authoritative 모호성 또는 미수신
 * 반환값: 중복이 제거된 안전 경고 목록
 * 작성 날짜: 2026/08/25
 */
function create_risk_operator_notices(
    risk_block_reason: string | null | undefined,
    risk_policy_availability: TraderPanelRiskPolicyAvailability | undefined,
    configured_risk_policy_version: number | null | undefined,
    max_order_notional: string | null | undefined,
    max_position_notional: string | null | undefined,
    max_daily_loss: string | null | undefined,
    daily_loss_scope: string | null | undefined,
    manual_kill_behavior: string | null | undefined,
    session_risk_policy_version: number | null | undefined,
    last_risk_decision_allowed: boolean | null | undefined,
    manual_kill_active: boolean | undefined,
    manual_kill_cleanup_complete: boolean | undefined,
    manual_kill_activation_behavior: string | null | undefined,
    manual_kill_activation_policy_version: number | null | undefined,
    process_ownership_ambiguous: boolean | undefined,
): ReadonlyArray<RiskOperatorNotice> {
    const notices: Array<RiskOperatorNotice> = [];

    // Backend reason은 exact allowlist로만 번역하며 알 수 없는 원문은 절대 DOM에 보간하지 않는다.
    if (risk_block_reason !== null && risk_block_reason !== undefined) {
        notices.push(
            RISK_OPERATOR_NOTICE_BY_REASON[risk_block_reason]
                ?? UNKNOWN_RISK_OPERATOR_NOTICE,
        );
    }

    // Policy availability와 session version 불일치는 마지막 BUY 판정이 없어도 독립 경고한다.
    if (risk_policy_availability === 'UNAVAILABLE'
        && !notices.some((notice) => notice.id === 'risk-policy-unavailable')) {
        notices.push(RISK_OPERATOR_NOTICE_BY_REASON.RISK_POLICY_UNAVAILABLE!);
    }
    if (configured_risk_policy_version !== null
        && configured_risk_policy_version !== undefined
        && session_risk_policy_version !== null
        && session_risk_policy_version !== undefined
        && configured_risk_policy_version !== session_risk_policy_version
        && !notices.some((notice) => notice.id === 'risk-policy-version-mismatch')) {
        notices.push(RISK_OPERATOR_NOTICE_BY_REASON.RISK_POLICY_VERSION_MISMATCH!);
    }

    // 세 null은 정책 부재가 아니라 configured policy가 명시한 무제한 상태로 별도 경고한다.
    if (risk_policy_availability === 'CONFIGURED'
        && max_order_notional === null
        && max_position_notional === null
        && max_daily_loss === null
        && daily_loss_scope !== null
        && daily_loss_scope !== undefined
        && manual_kill_behavior !== null
        && manual_kill_behavior !== undefined) {
        notices.push(CONFIGURED_UNBOUNDED_OPERATOR_NOTICE);
    }

    // 불완전 component 입력의 차단 판정도 성공 상태로 축소하지 않고 일반 안전 경고로 표시한다.
    if (last_risk_decision_allowed === false
        && (risk_block_reason === null || risk_block_reason === undefined)
        && !notices.some((notice) => notice.id === 'unknown-risk-block')) {
        notices.push(UNKNOWN_RISK_OPERATOR_NOTICE);
    }

    // Manual kill과 process ownership 모호성은 risk reason과 별개인 운영 상태로 각각 보존한다.
    if (manual_kill_active === true
        && !notices.some((notice) => notice.id === 'manual-kill-active')) {
        notices.push(RISK_OPERATOR_NOTICE_BY_REASON.MANUAL_KILL_SWITCH_ACTIVE!);
    }
    if (manual_kill_active === true && manual_kill_cleanup_complete === false) {
        notices.push(MANUAL_KILL_CLEANUP_PENDING_OPERATOR_NOTICE);
    }

    // Hot-swap 뒤 configured policy와 활성 epoch provenance가 다르면 기존 cleanup 의무를 명시한다.
    const activation_provenance_received = (
        manual_kill_activation_behavior !== undefined
        && manual_kill_activation_policy_version !== undefined
    );
    const activation_provenance_changed = (
        manual_kill_activation_behavior !== manual_kill_behavior
        || manual_kill_activation_policy_version !== configured_risk_policy_version
    );
    if (manual_kill_active === true
        && activation_provenance_received
        && activation_provenance_changed) {
        const activation_behavior_label = manual_kill_activation_behavior === null
            ? '정책 미설정 상태의 차단'
            : MANUAL_KILL_BEHAVIOR_LABEL_BY_VALUE[manual_kill_activation_behavior]
                ?? '확인되지 않은 안전 동작';
        const activation_version_label = manual_kill_activation_policy_version === null
            ? '정책 버전 없음'
            : `정책 v${manual_kill_activation_policy_version}`;
        notices.push({
            id: 'manual-kill-activation-provenance',
            summary: `현재 안전 차단은 활성화 당시 ${activation_version_label}의 ${activation_behavior_label} 동작으로 고정되어 있습니다.`,
            action: '현재 구성 정책이 바뀌었더라도 기존 차단을 해제하기 전까지 활성 epoch의 정리 의무를 따르세요.',
        });
    }
    if (process_ownership_ambiguous === true) {
        notices.push(PROCESS_OWNERSHIP_OPERATOR_NOTICE);
    }

    return notices;
}


/**
 * 함수 이름: resolve_keyboard_target_tab()
 * 기능: ARIA tablist의 방향키·Home·End 입력을 다음 focus 대상 탭으로 변환한다.
 * 인자: key -> 눌린 키, current_tab -> 현재 키보드 focus를 가진 탭
 * 반환값: 이동할 탭 또는 tab 이동 키가 아닐 때 null
 * 작성 날짜: 2026/08/24
 */
function resolve_keyboard_target_tab(
    key: string,
    current_tab: TraderPanelTab,
): TraderPanelTab | null {
    const current_index = TAB_OPTIONS.findIndex((option) => option.tab === current_tab);

    if (key === 'Home') {
        return TAB_OPTIONS[0]!.tab;
    }
    if (key === 'End') {
        return TAB_OPTIONS[TAB_OPTIONS.length - 1]!.tab;
    }
    if (key !== 'ArrowLeft' && key !== 'ArrowRight') {
        return null;
    }

    // 좌우 방향키는 양 끝에서 순환해 두 탭을 하나의 roving focus group으로 유지한다.
    const movement = key === 'ArrowRight' ? 1 : -1;
    const target_index = (
        current_index + movement + TAB_OPTIONS.length
    ) % TAB_OPTIONS.length;

    return TAB_OPTIONS[target_index]!.tab;
}


/**
 * 함수 이름: TraderPanel()
 * 기능: 최근 체결과 실시간 지표 탭을 전환하여 트레이딩 결과를 표시한다.
 * 인자: props -> 선택 탭, 체결·지표 ViewModel과 사용자 intent 처리 함수
 * 반환값: 트레이딩 패널 React 요소
 * 작성 날짜: 2026/08/12
 */
export function TraderPanel({
    activeTab,
    configured_risk_policy_version,
    max_order_notional,
    max_position_notional,
    max_daily_loss,
    daily_loss_scope,
    manual_kill_behavior,
    indicatorGroups,
    last_risk_decision_allowed,
    last_risk_budget,
    manual_kill_active,
    manual_kill_cleanup_complete,
    manual_kill_activation_behavior,
    manual_kill_activation_policy_version,
    onIntent,
    orders,
    process_ownership_ambiguous,
    risk_block_reason,
    risk_policy_availability,
    session_risk_policy_version,
}: TraderPanelProps) {
    // Roving focus element와 authoritative 안전 경고 projection을 render마다 같은 props에서 만든다.
    const tab_button_refs = useRef<Partial<Record<TraderPanelTab, HTMLButtonElement | null>>>({});
    const panel_ref = useRef<HTMLElement | null>(null);
    const indicator_panel_ref = useRef<HTMLDivElement | null>(null);
    const phase_key = indicatorGroups.map((group) => group.id).join('|');
    const previous_phase_key = useRef(phase_key);

    // 수치만 바뀌면 DOM과 스크롤을 유지하고, 보이는 단계가 바뀔 때만 목록 처음으로 이동한다.
    useLayoutEffect(() => {
        if (previous_phase_key.current === phase_key || activeTab !== 'realtime') return;
        previous_phase_key.current = phase_key;
        const panel = panel_ref.current;
        const indicators = indicator_panel_ref.current;
        if (panel === null || indicators === null) return;

        const target = panel.scrollTop + indicators.getBoundingClientRect().top
            - panel.getBoundingClientRect().top - 62;
        panel.scrollTop = Math.max(0, target);  // sticky 탭 바로 아래에 새 제목을 둔다.
    }, [activeTab, phase_key]);
    const risk_operator_notices = create_risk_operator_notices(
        risk_block_reason,
        risk_policy_availability,
        configured_risk_policy_version,
        max_order_notional,
        max_position_notional,
        max_daily_loss,
        daily_loss_scope,
        manual_kill_behavior,
        session_risk_policy_version,
        last_risk_decision_allowed,
        manual_kill_active,
        manual_kill_cleanup_complete,
        manual_kill_activation_behavior,
        manual_kill_activation_policy_version,
        process_ownership_ambiguous,
    );

    /**
     * 함수 이름: handle_tab_key_down()
     * 기능: roving focus 이동과 controlled 탭 선택 intent를 같은 키 입력으로 전달한다.
     * 인자: event -> 탭 버튼 keyboard event, current_tab -> event를 받은 탭
     * 반환값: 없음
     * 작성 날짜: 2026/08/24
     */
    function handle_tab_key_down(
        event: KeyboardEvent<HTMLButtonElement>,
        current_tab: TraderPanelTab,
    ): void {
        const target_tab = resolve_keyboard_target_tab(event.key, current_tab);

        if (target_tab === null) {
            return;
        }

        event.preventDefault();

        // Controlled state가 갱신되기 전에도 실제 keyboard focus는 즉시 목표 탭으로 이동한다.
        tab_button_refs.current[target_tab]?.focus();
        if (target_tab !== activeTab) {
            onIntent?.({
                type: 'TRADER_TAB_REQUESTED',
                tab: target_tab,
            });
        }
    }

    return (
        <aside aria-labelledby="trader-panel-title" className={styles.panel} ref={panel_ref} tabIndex={0}>
            <header>
                <h2 id="trader-panel-title">트레이딩 패널</h2>
                <p>거래 결과와 실시간 지표</p>
            </header>
            {risk_operator_notices.length > 0 ? (
                <section
                    aria-labelledby="trader-risk-notice-title"
                    className={styles.riskNotice}
                    role="alert"
                >
                    <h3 id="trader-risk-notice-title">운영자 확인 필요</h3>
                    <p className={styles.riskNoticeIntroduction}>
                        백엔드 위험 정책의 현재 운영 상태입니다.
                    </p>
                    <ul aria-label="안전 차단 상태와 운영자 조치">
                        {risk_operator_notices.map((notice) => (
                            <li key={notice.id}>
                                <strong>{notice.summary}</strong>
                                <span>운영자 조치: {notice.action}</span>
                            </li>
                        ))}
                    </ul>
                </section>
            ) : null}
            {/* 마지막 decision의 frozen budget을 별도 계산 없이 authoritative 문자열로 표시한다. */}
            {last_risk_budget === null || last_risk_budget === undefined ? null : (
                <section
                    aria-labelledby="trader-risk-budget-title"
                    className={styles.riskBudget}
                >
                    <div className={styles.riskBudgetHeader}>
                        <h3 id="trader-risk-budget-title">마지막 BUY 위험 예산</h3>
                        <span>
                            {last_risk_decision_allowed === true
                                ? '허용'
                                : last_risk_decision_allowed === false
                                    ? '차단'
                                    : '판정 확인 필요'}
                        </span>
                    </div>
                    <dl>
                        <div>
                            <dt>현재 포지션</dt>
                            <dd>{format_quote_amount(
                                last_risk_budget.current_position_notional,
                                'USDT',
                            )}</dd>
                        </div>
                        <div>
                            <dt>미체결 BUY 예약</dt>
                            <dd>{format_quote_amount(
                                last_risk_budget.reserved_buy_notional,
                                'USDT',
                            )}</dd>
                        </div>
                        <div>
                            <dt>후보 주문</dt>
                            <dd>{format_quote_amount(
                                last_risk_budget.candidate_order_notional,
                                'USDT',
                            )}</dd>
                        </div>
                        <div>
                            <dt>예상 포지션</dt>
                            <dd>{format_quote_amount(
                                last_risk_budget.projected_position_notional,
                                'USDT',
                            )}</dd>
                        </div>
                        <div>
                            <dt>KST 당일 실현 PnL</dt>
                            <dd>{format_quote_amount(
                                last_risk_budget.daily_realized_pnl,
                                'USDT',
                            )}</dd>
                        </div>
                        <div>
                            <dt>미실현 PnL</dt>
                            <dd>{format_quote_amount(
                                last_risk_budget.unrealized_pnl,
                                'USDT',
                            )}</dd>
                        </div>
                        <div>
                            <dt>적용 일일 손실</dt>
                            <dd>{format_quote_amount(last_risk_budget.daily_loss, 'USDT')}</dd>
                        </div>
                    </dl>
                    <p className={styles.riskBudgetProvenance}>
                        계산 기준: policy v
                        {last_risk_budget.policy_version ?? '미설정'} · market v
                        {last_risk_budget.market_version} · account v
                        {last_risk_budget.account_version} · context v
                        {last_risk_budget.context_version} · 판정 시 안전 차단
                        {' '}{last_risk_budget.manual_kill_active ? '활성' : '비활성'}
                    </p>
                </section>
            )}
            <div aria-label="트레이딩 패널 보기" className={styles.tabs} role="tablist">
                {TAB_OPTIONS.map((option) => (
                    <button
                        aria-controls={`trader-tabpanel-${option.tab}`}
                        aria-selected={activeTab === option.tab}
                        className={activeTab === option.tab ? styles.activeTab : styles.tab}
                        id={`trader-tab-${option.tab}`}
                        key={option.tab}
                        onClick={() => onIntent?.({
                            type: 'TRADER_TAB_REQUESTED',
                            tab: option.tab,
                        })}
                        onKeyDown={(event) => handle_tab_key_down(event, option.tab)}
                        ref={(element) => {
                            tab_button_refs.current[option.tab] = element;
                        }}
                        role="tab"
                        tabIndex={activeTab === option.tab ? 0 : -1}
                        type="button"
                    >
                        {option.label}
                    </button>
                ))}
            </div>

            <div
                aria-labelledby="trader-tab-recent"
                className={styles.tabPanel}
                hidden={activeTab !== 'recent'}
                id="trader-tabpanel-recent"
                role="tabpanel"
            >
                <div className={styles.sectionHeader}>
                    <h3>최근 체결</h3>
                    <button
                        onClick={() => onIntent?.({ type: 'ALL_ORDERS_REQUESTED' })}
                        type="button"
                    >
                        전체 보기
                    </button>
                </div>
                <RecentOrdersList orders={orders} />
            </div>
            <div
                aria-labelledby="trader-tab-realtime"
                className={styles.tabPanel}
                hidden={activeTab !== 'realtime'}
                id="trader-tabpanel-realtime"
                ref={indicator_panel_ref}
                role="tabpanel"
                tabIndex={0}
            >
                <RealtimeIndicators groups={indicatorGroups} visible={activeTab === 'realtime'} />
            </div>
        </aside>
    );
}
