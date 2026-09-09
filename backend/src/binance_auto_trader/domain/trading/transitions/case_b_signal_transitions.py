"""Case B 신호 Region의 B-01~B-19 transition을 구현한다."""

from __future__ import annotations

from dataclasses import replace

from ..action_requests import (
    CancelPendingOrder,
    ReevaluationTrigger,
    ResetCaseBContext,
    patch,
)
# 표시와 주문 판단이 같은 순수 조건 평가를 공유한다.
from ..conditions import condition_met, condition_unmet
from ..context import TradingContextView
from ..events import TradingEvent, TradingEventType
from ..states import (
    CaseBSignalState,
    ExitReason,
    OrderAttemptKind,
    StrategyType,
    TradingStateConfiguration,
)
from .base import TransitionOutcome, create_transition_outcome
from .helpers import (
    create_entry_order_actions,
    create_queue_event_action,
    schedule_market_reevaluation,
)


def handle_case_b_signal_transition(
    state: TradingStateConfiguration,
    event: TradingEvent,
    context: TradingContextView,
) -> TransitionOutcome | None:
    """
    함수 이름: handle_case_b_signal_transition()
    기능: broadcast event와 현재 Case B 신호 상태에서 하나의 transition을 선택한다.
    인자: state -> 현재 전체 상태 구성
        event -> Case B Region에 전달된 TradingEvent
        context -> signal·시장 값이 포함된 TradingContextView
    반환값: 선택된 TransitionOutcome 또는 적용할 transition이 없으면 None
    작성 날짜: 2026/08/14
    """
    signal_state = state.case_b_signal_state
    runtime = context.runtime
    market = context.market
    event_type = event.event_type

    # 공통 하단 접촉은 이미 통과했다. 그 순간의 30분봉 BBW만으로 Case B를 활성화한다.
    if signal_state is CaseBSignalState.B_WAIT_TOUCH:
        if event_type is not TradingEventType.ACTIVATE_TRADE_MANAGEMENT:
            return None
        valid_touch = (
            runtime.touch_candle_bbw is not None
            and condition_met("b_touch_bbw", context)
        )
        # touch 조건이 맞지 않으면 이 lower event의 Case B Region을 종료한다.
        if not valid_touch:
            return create_transition_outcome(
                "B-02",
                replace(state, case_b_signal_state=CaseBSignalState.CASE_B_FINAL_STATE),
                patch(case_b_enabled=False),
                ResetCaseBContext(),
            )
        return create_transition_outcome(
            "B-03",
            replace(state, case_b_signal_state=CaseBSignalState.B_WAIT_SIGNAL),
            patch(case_b_enabled=True, signal_created=False),
            schedule_market_reevaluation(
                TradingEventType.THIRTY_MINUTE_CANDLE_CLOSED,
                context,
                "Wait for the first valid Case B signal candle",
                trigger=ReevaluationTrigger.NEXT_30M_CLOSE,
            ),
        )

    # B_WAIT_SIGNAL은 확정 30분봉에서만 최초 signal candle을 생성한다.
    if signal_state is CaseBSignalState.B_WAIT_SIGNAL:
        if event_type is TradingEventType.THIRTY_MINUTE_CANDLE_CLOSED:
            signal_guard = _is_signal_candle_guard_satisfied(context)
            if not signal_guard:
                return create_transition_outcome(
                    "B-04",
                    state,
                    schedule_market_reevaluation(
                        TradingEventType.THIRTY_MINUTE_CANDLE_CLOSED,
                        context,
                        "Case B signal candle guard not yet met",
                        trigger=ReevaluationTrigger.NEXT_30M_CLOSE,
                    ),
                )
            # 첫 유효 signal만 저장하고 즉시 pullback 검사 event를 다음 microstep에 넣는다.
            if not runtime.signal_created:
                return create_transition_outcome(
                    "B-05",
                    replace(state, case_b_signal_state=CaseBSignalState.B_WAIT_PULLBACK),
                    patch(
                        signal_created=True,
                        signal_candle_id=event.candle_id or market.current_30m_candle_id,
                        # Production은 원본 마감 경계를 사용한다. source 없는 수동 fixture만 event 시각을 쓴다.
                        signal_time=market.confirmed_30m_close_time or event.occurred_at,
                    ),
                    create_queue_event_action(
                        TradingEventType.START_CASE_B_WAIT_PULLBACK_CONDITION_CHECK,
                        context,
                    ),
                )
        # Case C가 포지션을 획득해도 signal 감시는 유지하되 신규 Case B 진입은 멈춘다.
        if (
            event_type is TradingEventType.CASE_C_POSITION_OPENED
            and runtime.position_owner is StrategyType.CASE_C
        ):
            return create_transition_outcome("B-12", state, patch(case_b_entry_paused=True))
        if (
            event_type is TradingEventType.CASE_B_ACTIVE_RESUME
            and _is_active_resume_guard_satisfied(context)
        ):
            return create_transition_outcome("B-14", state, patch(case_b_entry_paused=False))
        if (
            event_type is TradingEventType.CASE_B_WAIT_ONLY
            and not _is_handoff_active(context)
        ):
            return create_transition_outcome("B-16", state, patch(case_b_entry_paused=True))
        return None

    # B_WAIT_PULLBACK은 3시간 경계, 주문 예약, pause 상태를 같은 snapshot으로 판정한다.
    if signal_state is CaseBSignalState.B_WAIT_PULLBACK:
        if event_type in (
            TradingEventType.START_CASE_B_WAIT_PULLBACK_CONDITION_CHECK,
            TradingEventType.RETRY_CASE_B_WAIT_PULLBACK_CONDITION_CHECK,
        ):
            is_start = (
                event_type
                is TradingEventType.START_CASE_B_WAIT_PULLBACK_CONDITION_CHECK
            )
            # 정확히 3시간은 허용하고 이를 초과한 signal만 폐기한다.
            if condition_unmet("b_signal_age", context):
                return create_transition_outcome(
                    "B-08" if is_start else "B-11",
                    replace(
                        state,
                        case_b_signal_state=CaseBSignalState.CASE_B_FINAL_STATE,
                    ),
                    ResetCaseBContext(),
                )
            can_buy = (
                runtime.position_owner is None
                and runtime.pending_order_id is None
                and not runtime.case_b_entry_paused
                and condition_met("b_signal_age", context)
                and condition_met("b_pullback", context)
            )
            # owner와 pending 주문이 없을 때만 최초 Case B 매수 action을 만든다.
            if can_buy:
                actions = create_entry_order_actions(
                    StrategyType.CASE_B,
                    OrderAttemptKind.INITIAL,
                    event,
                    context,
                )
                return create_transition_outcome(
                    "B-06" if is_start else "B-09",
                    replace(
                        state,
                        case_b_signal_state=CaseBSignalState.B_POSITION_OPEN_SIGNALLED,
                    ),
                    *actions,
                )
            should_wait = (
                condition_met("b_signal_age", context)
                and (
                    condition_unmet("b_pullback", context)
                    or runtime.position_owner is not None
                    or runtime.case_b_entry_paused
                )
            )
            # 시장 값 또는 진입 권한이 바뀔 때까지 scheduler에 재평가를 맡긴다.
            if should_wait:
                return create_transition_outcome(
                    "B-07" if is_start else "B-10",
                    state,
                    schedule_market_reevaluation(
                        TradingEventType.RETRY_CASE_B_WAIT_PULLBACK_CONDITION_CHECK,
                        context,
                        "Case B pullback or entry permission not yet available",
                    ),
                )
        if (
            event_type is TradingEventType.CASE_C_POSITION_OPENED
            and runtime.position_owner is StrategyType.CASE_C
        ):
            return create_transition_outcome("B-13", state, patch(case_b_entry_paused=True))
        if (
            event_type is TradingEventType.CASE_B_ACTIVE_RESUME
            and _is_active_resume_guard_satisfied(context)
        ):
            return create_transition_outcome("B-15", state, patch(case_b_entry_paused=False))
        if (
            event_type is TradingEventType.CASE_B_WAIT_ONLY
            and not _is_handoff_active(context)
        ):
            return create_transition_outcome("B-17", state, patch(case_b_entry_paused=True))
        return None

    # 주문 요청 상태는 terminal 포지션 feedback이 오기 전까지 성공 상태로 선전이하지 않는다.
    if signal_state is CaseBSignalState.B_POSITION_OPEN_SIGNALLED:
        if (
            event_type is TradingEventType.CASE_B_POSITION_OPENED
            and runtime.position_owner is StrategyType.CASE_B
        ):
            return create_transition_outcome(
                "B-18",
                replace(state, case_b_signal_state=CaseBSignalState.CASE_B_FINAL_STATE),
            )
        if (
            event_type is TradingEventType.CASE_C_POSITION_OPENED
            and runtime.position_owner is StrategyType.CASE_C
        ):
            return create_transition_outcome(
                "B-19",
                replace(state, case_b_signal_state=CaseBSignalState.B_WAIT_PULLBACK),
                CancelPendingOrder(
                    order_id=runtime.pending_order_id,
                    reason="CASE_C_WON_ENTRY_ARBITRATION",
                ),
                patch(case_b_entry_paused=True),
            )

    return None


def _is_signal_candle_guard_satisfied(context: TradingContextView) -> bool:
    """
    함수 이름: _is_signal_candle_guard_satisfied()
    기능: 확정 30분봉의 slope, %B 및 직전 3개 봉 저가 조건을 판정한다.
    인자: context -> 확정 candle 지표가 포함된 TradingContextView
    반환값: Case B signal candle 조건 만족 여부
    작성 날짜: 2026/08/14
    """
    market = context.market
    return (
        market.confirmed_30m_close
        and condition_met("b_signal_slope", context)
        and condition_met("b_signal_pct_b", context)
        and len(market.previous_3_closed_candle_lows) == 3
        and condition_met("b_signal_low", context)
    )


def _is_handoff_active(context: TradingContextView) -> bool:
    """
    함수 이름: _is_handoff_active()
    기능: Case C가 약한 TP_TRAIL로 종료되어 Case B active 인계가 가능한지 판정한다.
    인자: context -> Case C 종료 사유와 종료 %B를 가진 Context
    반환값: Case B active 인계 조건 만족 여부
    작성 날짜: 2026/08/14
    """
    runtime = context.runtime
    return (
        runtime.case_c_exit_reason is ExitReason.TP_TRAIL
        and runtime.case_c_exit_pct_b is not None
        and condition_met("c_handoff", context)
    )


def _is_active_resume_guard_satisfied(context: TradingContextView) -> bool:
    """
    함수 이름: _is_active_resume_guard_satisfied()
    기능: Case C 회복 확인과 active 인계 조건을 함께 판정한다.
    인자: context -> Case C 회복·종료 정보를 가진 Context
    반환값: Case B 진입 재개 가능 여부
    작성 날짜: 2026/08/14
    """
    return context.runtime.case_c_recovery_confirmed and _is_handoff_active(context)
