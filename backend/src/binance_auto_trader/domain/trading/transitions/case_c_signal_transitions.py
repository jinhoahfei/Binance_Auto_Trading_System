"""Case C 신호 Region의 C-01~C-17 transition을 구현한다."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from ..action_requests import (
    CancelPendingOrder,
    PatchRuntimeContext,
    ResetCaseCContext,
    ScheduleReevaluation,
    patch,
)
# 표시와 주문 판단이 같은 순수 조건 평가를 공유한다.
from ..conditions import condition_met, condition_unmet
from ..context import TradingContextView
from ..events import TradingEvent, TradingEventType
from ..states import (
    CaseCSignalState,
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


def handle_case_c_signal_transition(
    state: TradingStateConfiguration,
    event: TradingEvent,
    context: TradingContextView,
) -> TransitionOutcome | None:
    """
    함수 이름: handle_case_c_signal_transition()
    기능: Case C setup 우선순위와 주문 feedback transition을 선택한다.
    인자: state -> 현재 전체 상태 구성
        event -> Case C Region에 전달된 TradingEvent
        context -> setup·flush·시장 값이 포함된 TradingContextView
    반환값: 선택된 TransitionOutcome 또는 적용할 transition이 없으면 None
    작성 날짜: 2026/08/14
    """
    signal_state = state.case_c_signal_state
    runtime = context.runtime
    market = context.market
    event_type = event.event_type

    # C_WAIT_SETUP은 한 30분봉에서 setup을 한 번만 만들 수 있는지 먼저 확인한다.
    if signal_state is CaseCSignalState.C_WAIT_SETUP:
        if event_type in (
            TradingEventType.ACTIVATE_TRADE_MANAGEMENT,
            TradingEventType.RETRY_C_WAIT_SETUP,
        ):
            setup_allowed = _is_setup_guard_satisfied(context)
            is_activation = event_type is TradingEventType.ACTIVATE_TRADE_MANAGEMENT
            # setup 조건이 아직 아니면 시장 변경을 기다리며 즉시 busy loop를 만들지 않는다.
            if not setup_allowed:
                return create_transition_outcome(
                    "C-02" if is_activation else "C-04",
                    state,
                    schedule_market_reevaluation(
                        TradingEventType.RETRY_C_WAIT_SETUP,
                        context,
                        "Wait for a new Case C setup in this candle",
                    ),
                )
            return create_transition_outcome(
                "C-03" if is_activation else "C-05",
                replace(state, case_c_signal_state=CaseCSignalState.C_SETUP),
                patch(last_case_c_setup_candle_id=market.current_30m_candle_id),
                create_queue_event_action(
                    TradingEventType.START_CASE_C_SETUP_CONDITION_CHECK,
                    context,
                ),
            )
        # Case B가 먼저 포지션을 열면 같은 lower event의 Case C 신규 진입을 종료한다.
        if (
            event_type is TradingEventType.CASE_B_POSITION_OPENED
            and runtime.position_owner is StrategyType.CASE_B
        ):
            return create_transition_outcome(
                "C-06",
                replace(
                    state,
                    case_c_signal_state=CaseCSignalState.CASE_C_FINAL_STATE,
                ),
                patch(allow_new_case_c_setup=False),
            )
        return None

    # C_SETUP 진입 시 이전 flush와 3분 회복 기준을 명시적으로 초기화한다.
    if signal_state is CaseCSignalState.C_SETUP:
        if event_type is TradingEventType.START_CASE_C_SETUP_CONDITION_CHECK:
            return create_transition_outcome(
                "C-07",
                state,
                patch(
                    flush_low=None,
                    flush_low_pct_b=None,
                    flush_low_time=None,
                    timer_base_pct_b=None,
                    timer_base_time=None,
                    entry_pct_b=None,
                ),
                create_queue_event_action(
                    TradingEventType.RETRY_CASE_C_SETUP_CONDITION_CHECK,
                    context,
                ),
            )

        if event_type is TradingEventType.RETRY_CASE_C_SETUP_CONDITION_CHECK:
            # 1순위: 매수 전에 %B가 0.25 이상 회복되면 setup을 소비하고 종료한다.
            if (
                runtime.position_owner is None
                and condition_met("c_recovery", context)
            ):
                return create_transition_outcome(
                    "C-08",
                    replace(
                        state,
                        case_c_signal_state=CaseCSignalState.CASE_C_FINAL_STATE,
                    ),
                    patch(
                        case_c_consumed_for_event=True,
                        case_c_recovery_confirmed=True,
                        allow_new_case_c_setup=False,
                    ),
                )

            # 2순위: 최초 flush 또는 기존보다 낮은 가격에서 timer 기준을 갱신한다.
            if (
                runtime.flush_low is None
                and condition_met("c_flush", context)
            ):
                return create_transition_outcome("C-09", state, *_create_flush_actions(context))

            if runtime.flush_low is not None and condition_met("c_new_low", context):
                return create_transition_outcome("C-10", state, *_create_flush_actions(context))

            # 3순위: 3분을 초과하면 현재 %B를 새 기준으로 회복 구간을 다시 시작한다.
            if (
                runtime.flush_low is not None
                and condition_unmet("c_new_low", context)
                and condition_unmet("c_recovery_window", context)
                and condition_unmet("c_recovery", context)
            ):
                entry_pct_b = market.realtime_pct_b + Decimal("0.06")
                return create_transition_outcome(
                    "C-11",
                    state,
                    patch(
                        current_open_pct_b=market.realtime_pct_b,
                        timer_base_pct_b=market.realtime_pct_b,
                        timer_base_time=context.evaluated_at,
                        entry_pct_b=entry_pct_b,
                    ),
                    schedule_market_reevaluation(
                        TradingEventType.RETRY_CASE_C_SETUP_CONDITION_CHECK,
                        context,
                        "Case C three-minute recovery window restarted",
                    ),
                )

            # 4순위: 3분 이내 회복 폭과 추격매수 금지선을 함께 계산한다.
            entry_pct_b = runtime.entry_pct_b
            in_recovery_window = (
                runtime.flush_low is not None
                and entry_pct_b is not None
                and condition_met("c_recovery_window", context)
                and condition_met("c_rebound", context)
                and condition_unmet("c_recovery", context)
            )
            # owner와 pending 주문이 모두 없을 때만 Case C 최초 매수를 요청한다.
            if (
                runtime.position_owner is None
                and runtime.pending_order_id is None
                and in_recovery_window
                and condition_met("c_entry_limit", context)
            ):
                actions = (
                    patch(current_open_pct_b=market.realtime_pct_b),
                    *create_entry_order_actions(
                        StrategyType.CASE_C,
                        OrderAttemptKind.INITIAL,
                        event,
                        context,
                    ),
                )
                return create_transition_outcome(
                    "C-12",
                    replace(
                        state,
                        case_c_signal_state=CaseCSignalState.C_POSITION_OPEN_SIGNALLED,
                    ),
                    *actions,
                )

            if in_recovery_window and condition_unmet("c_entry_limit", context):
                return create_transition_outcome(
                    "C-13",
                    state,
                    schedule_market_reevaluation(
                        TradingEventType.RETRY_CASE_C_SETUP_CONDITION_CHECK,
                        context,
                        "Recovery was too strong for a Case C entry",
                    ),
                )

            # 어느 전이도 선택되지 않으면 다음 실제 시장 변화까지 setup 감시를 유지한다.
            should_watch = condition_unmet("c_recovery", context) and (
                (
                    runtime.flush_low is None
                    and condition_unmet("c_flush", context)
                )
                or (
                    runtime.flush_low is not None
                    and entry_pct_b is not None
                    and condition_unmet("c_new_low", context)
                    and condition_met("c_recovery_window", context)
                    and condition_unmet("c_rebound", context)
                )
            )
            if should_watch:
                return create_transition_outcome(
                    "C-14",
                    state,
                    schedule_market_reevaluation(
                        TradingEventType.RETRY_CASE_C_SETUP_CONDITION_CHECK,
                        context,
                        "Continue Case C flush/recovery observation",
                    ),
                )

        if (
            event_type is TradingEventType.CASE_B_POSITION_OPENED
            and runtime.position_owner is StrategyType.CASE_B
        ):
            return create_transition_outcome(
                "C-15",
                replace(
                    state,
                    case_c_signal_state=CaseCSignalState.CASE_C_FINAL_STATE,
                ),
                patch(allow_new_case_c_setup=False),
            )
        return None

    # 주문 요청 상태는 terminal feedback이 도착한 경우에만 Final로 전이한다.
    if signal_state is CaseCSignalState.C_POSITION_OPEN_SIGNALLED:
        if (
            event_type is TradingEventType.CASE_C_POSITION_OPENED
            and runtime.position_owner is StrategyType.CASE_C
        ):
            return create_transition_outcome(
                "C-16",
                replace(state, case_c_signal_state=CaseCSignalState.CASE_C_FINAL_STATE),
            )
        if (
            event_type is TradingEventType.CASE_B_POSITION_OPENED
            and runtime.position_owner is StrategyType.CASE_B
        ):
            return create_transition_outcome(
                "C-17",
                replace(state, case_c_signal_state=CaseCSignalState.CASE_C_FINAL_STATE),
                CancelPendingOrder(
                    order_id=runtime.pending_order_id,
                    reason="CASE_B_WON_ENTRY_ARBITRATION",
                ),
                patch(allow_new_case_c_setup=False),
            )

    return None


def _is_setup_guard_satisfied(context: TradingContextView) -> bool:
    """
    함수 이름: _is_setup_guard_satisfied()
    기능: setup 권한, candle 중복, 실시간 %B 및 CCI 조건을 함께 판정한다.
    인자: context -> Case C runtime과 실시간 시장 snapshot
    반환값: Case C setup 생성 가능 여부
    작성 날짜: 2026/08/14
    """
    runtime = context.runtime
    market = context.market
    return (
        runtime.allow_new_case_c_setup
        and not runtime.case_c_consumed_for_event
        and runtime.last_case_c_setup_candle_id != market.current_30m_candle_id
        and condition_met("c_setup_pct_b", context)
        and condition_met("c_setup_cci", context)
    )


def _create_flush_actions(
    context: TradingContextView,
) -> tuple[PatchRuntimeContext, ScheduleReevaluation]:
    """
    함수 이름: _create_flush_actions()
    기능: 최초 또는 더 낮은 flush 가격에서 기준값과 3분 timer를 갱신한다.
    인자: context -> 최신 가격과 %B를 가진 TradingContextView
    반환값: flush runtime patch와 재평가 요청 tuple
    작성 날짜: 2026/08/14
    """
    market = context.market
    entry_pct_b = market.realtime_pct_b + Decimal("0.06")
    return (
        patch(
            flush_low=market.realtime_price,
            flush_low_pct_b=market.realtime_pct_b,
            flush_low_time=context.evaluated_at,
            timer_base_pct_b=market.realtime_pct_b,
            timer_base_time=context.evaluated_at,
            entry_pct_b=entry_pct_b,
        ),
        schedule_market_reevaluation(
            TradingEventType.RETRY_CASE_C_SETUP_CONDITION_CHECK,
            context,
            "Track recovery from the latest Case C flush low",
        ),
    )
