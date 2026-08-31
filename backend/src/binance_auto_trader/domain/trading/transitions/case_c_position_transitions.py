"""Case C 포지션 관리의 PC-01~PC-28 및 PC-23F transition을 구현한다."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from ..action_requests import PatchRuntimeContext, ReevaluationTrigger, patch
from ..context import TradingContextView
from ..events import SellAttemptPayload, TradingEvent, TradingEventType
from ..states import (
    CaseCPositionState,
    ExitReason,
    OrderAttemptKind,
    OwnershipState,
    PositionReturnState,
    StrategyType,
    TradingPhase,
    TradingStateConfiguration,
)
from .base import TransitionOutcome, create_transition_outcome
from .helpers import (
    SIXTY_MINUTES,
    create_exit_order_actions,
    create_queue_event_action,
    schedule_market_reevaluation,
)


def handle_case_c_position_transition(
    state: TradingStateConfiguration,
    event: TradingEvent,
    context: TradingContextView,
) -> TransitionOutcome | None:
    """
    함수 이름: handle_case_c_position_transition()
    기능: Case C holding, TP trailing, 매도 feedback 및 청산 후 회복을 처리한다.
    인자: state -> 현재 전체 상태 구성
        event -> Case C 포지션 submachine에 전달된 TradingEvent
        context -> 포지션·청산·시장 snapshot
    반환값: 선택된 TransitionOutcome 또는 적용할 transition이 없으면 None
    작성 날짜: 2026/08/14
    """
    position_state = state.case_c_position_state
    if position_state is None:
        return None

    runtime = context.runtime
    event_type = event.event_type

    # Position과 이력 반영이 끝난 terminal feedback에서만 CLOSED로 전이한다.
    if event_type is TradingEventType.CASE_C_SELL_FILLED:
        if (
            position_state in (
                CaseCPositionState.CASE_C_HOLDING,
                CaseCPositionState.CASE_C_TP_TRAILING,
            )
            and runtime.position_owner is None
            and runtime.pending_exit_reason is not None
            and runtime.case_c_exit_reason is runtime.pending_exit_reason
        ):
            return create_transition_outcome(
                "PC-23F",
                replace(state, case_c_position_state=CaseCPositionState.CASE_C_CLOSED),
                _create_clear_exit_patch(),
                create_queue_event_action(TradingEventType.CASE_C_SELL_FINISHED, context),
            )
        return None

    # CLOSED에서는 같은 lower event의 Case C 재진입을 막고 %B 회복을 기다린다.
    if position_state is CaseCPositionState.CASE_C_CLOSED:
        if event_type is TradingEventType.CASE_C_SELL_FINISHED:
            return create_transition_outcome(
                "PC-24",
                state,
                patch(
                    case_c_consumed_for_event=True,
                    allow_new_case_c_setup=False,
                    case_b_entry_paused=True,
                ),
                create_queue_event_action(TradingEventType.CHECK_CASE_C_RECOVERY, context),
            )
        if event_type is TradingEventType.CHECK_CASE_C_RECOVERY:
            # 회복 전에는 scheduler를 사용해 다음 시장 변화에서만 다시 검사한다.
            if context.market.realtime_pct_b < Decimal("0.25"):
                return create_transition_outcome(
                    "PC-25",
                    state,
                    schedule_market_reevaluation(
                        TradingEventType.CHECK_CASE_C_RECOVERY,
                        context,
                        "Wait for Case C post-exit %B recovery",
                    ),
                )
            return create_transition_outcome(
                "PC-26",
                replace(
                    state,
                    case_c_position_state=CaseCPositionState.CASE_C_RECOVERY_SUCCEEDED,
                ),
                patch(case_c_recovery_confirmed=True),
                create_queue_event_action(TradingEventType.CHECK_CASE_B_HANDOFF, context),
            )
        return None

    # 회복 성공 후 청산 형태에 따라 Case B를 active 또는 wait-only로 인계한다.
    if position_state is CaseCPositionState.CASE_C_RECOVERY_SUCCEEDED:
        if event_type is not TradingEventType.CHECK_CASE_B_HANDOFF:
            return None
        handoff_active = _is_case_b_handoff_active(context)
        state_after = replace(
            state,
            ownership_state=OwnershipState.NO_POSITION,
            case_c_position_state=None,
        )
        if handoff_active:
            return create_transition_outcome(
                "PC-27",
                state_after,
                patch(
                    case_b_entry_paused=False,
                    case_b_only_until_next_lower_touch=True,
                    allow_new_case_c_setup=False,
                ),
                create_queue_event_action(TradingEventType.CASE_B_ACTIVE_RESUME, context),
            )
        return create_transition_outcome(
            "PC-28",
            state_after,
            patch(
                case_b_entry_paused=True,
                case_b_only_until_next_lower_touch=True,
                allow_new_case_c_setup=False,
            ),
            create_queue_event_action(TradingEventType.CASE_B_WAIT_ONLY, context),
        )

    # 청산 의도가 유지되는 동안에는 일반 조건 대신 매도 결과와 retry만 처리한다.
    allowed_while_exit_pending = {
        TradingEventType.CASE_C_SELL_FAILED,
        TradingEventType.CASE_C_SELL_RETRY,
    }
    if (
        runtime.pending_exit_reason is not None
        and event_type not in allowed_while_exit_pending
    ):
        return None

    if position_state is CaseCPositionState.CASE_C_HOLDING:
        return _handle_holding(state, event, context)
    if position_state is CaseCPositionState.CASE_C_TP_TRAILING:
        return _handle_tp_trailing(state, event, context)
    return None


def _handle_holding(
    state: TradingStateConfiguration,
    event: TradingEvent,
    context: TradingContextView,
) -> TransitionOutcome | None:
    """
    함수 이름: _handle_holding()
    기능: 익절권 진입 전 Case C 조건 검사, 손절·시간 청산 및 retry를 처리한다.
    인자: state -> 현재 전체 상태 구성
        event -> CASE_C_HOLDING에 전달된 TradingEvent
        context -> holding Guard와 pending 청산 정보를 가진 Context
    반환값: 선택된 TransitionOutcome 또는 None
    작성 날짜: 2026/08/14
    """
    runtime = context.runtime
    event_type = event.event_type

    # 조건 검사에서는 익절권→slope 손절→시간 청산 순서로 하나의 event를 고른다.
    if event_type in (
        TradingEventType.START_CASE_C_CONDITION_CHECK,
        TradingEventType.RETRY_CASE_C_CONDITION_CHECK,
    ):
        is_start = event_type is TradingEventType.START_CASE_C_CONDITION_CHECK
        selected_event = _select_holding_event(context)
        if selected_event is None:
            return create_transition_outcome(
                "PC-02" if is_start else "PC-04",
                state,
                schedule_market_reevaluation(
                    TradingEventType.RETRY_CASE_C_CONDITION_CHECK,
                    context,
                    "No Case C holding transition is currently enabled",
                ),
            )
        return create_transition_outcome(
            "PC-03" if is_start else "PC-05",
            state,
            create_queue_event_action(selected_event, context),
        )

    # slope 손절 event는 최초 청산 의도가 없을 때만 실제 매도 action을 만든다.
    if (
        event_type is TradingEventType.CASE_C_STOP
        and context.market.realtime_slope_at_most_minus_055_for_3m
        and _can_start_exit(context)
    ):
        actions = create_exit_order_actions(
            StrategyType.CASE_C,
            ExitReason.STOP,
            PositionReturnState.CASE_C_HOLDING,
            event,
            context,
            attempt_kind=OrderAttemptKind.INITIAL,
        )
        return create_transition_outcome("PC-06", state, *actions)

    # 익절권 진입 시 고정 tp_price와 비교 기준 slope를 저장하고 trailing을 시작한다.
    if (
        event_type is TradingEventType.CASE_C_ENTER_PROFIT_ZONE
        and context.market.realtime_pct_b >= Decimal("0.10")
        and _can_start_exit(context)
    ):
        market = context.market
        tp_price = market.lower_band + Decimal("0.10") * (
            market.upper_band - market.lower_band
        )
        return create_transition_outcome(
            "PC-10",
            replace(
                state,
                case_c_position_state=CaseCPositionState.CASE_C_TP_TRAILING,
            ),
            patch(
                tp_price=tp_price,
                previous_trail_ema_slope=market.tp_reference_ema_slope,
            ),
            create_queue_event_action(TradingEventType.START_TP_TRAILING_CONDITION_CHECK, context),
        )

    # holding과 trailing이 공유하는 시간 청산·실패 경로를 공통 함수로 처리한다.
    shared = _handle_time_exit_or_failure(
        state,
        event,
        context,
        PositionReturnState.CASE_C_HOLDING,
        repeated_failure_id="PC-21",
    )
    if shared is not None:
        return shared

    if (
        event_type is TradingEventType.CASE_C_SELL_FAILED
        and runtime.pending_exit_reason is ExitReason.STOP
        and runtime.pending_return_state is PositionReturnState.CASE_C_HOLDING
    ):
        transition_id = (
            "PC-07"
            if _get_sell_attempt_kind(event, context) is OrderAttemptKind.INITIAL
            else "PC-21"
        )
        return _schedule_sell_retry(transition_id, state, context)

    # retry는 같은 pending 청산 의도와 idempotency key를 재사용한다.
    if (
        event_type is TradingEventType.CASE_C_SELL_RETRY
        and runtime.pending_return_state is PositionReturnState.CASE_C_HOLDING
        and runtime.pending_order_id is None
        and runtime.pending_exit_reason is not None
    ):
        actions = create_exit_order_actions(
            StrategyType.CASE_C,
            runtime.pending_exit_reason,
            PositionReturnState.CASE_C_HOLDING,
            event,
            context,
            attempt_kind=OrderAttemptKind.RETRY,
        )
        return create_transition_outcome("PC-20", state, *actions)

    return None


def _handle_tp_trailing(
    state: TradingStateConfiguration,
    event: TradingEvent,
    context: TradingContextView,
) -> TransitionOutcome | None:
    """
    함수 이름: _handle_tp_trailing()
    기능: 익절권 fallback, 확정 1분봉 EMA slope 비교, 시간 청산 및 retry를 처리한다.
    인자: state -> 현재 전체 상태 구성
        event -> CASE_C_TP_TRAILING에 전달된 TradingEvent
        context -> TP 기준값·1분봉·pending 청산 정보를 가진 Context
    반환값: 선택된 TransitionOutcome 또는 None
    작성 날짜: 2026/08/14
    """
    runtime = context.runtime
    event_type = event.event_type

    # trailing 조건 검사는 fallback→확정 1분봉 slope→시간 청산 순서를 사용한다.
    if event_type in (
        TradingEventType.START_TP_TRAILING_CONDITION_CHECK,
        TradingEventType.RETRY_TP_TRAILING_CONDITION_CHECK,
    ):
        is_start = event_type is TradingEventType.START_TP_TRAILING_CONDITION_CHECK
        selected_event = _select_trailing_event(context)
        if selected_event is None:
            return create_transition_outcome(
                "PC-11" if is_start else "PC-13",
                state,
                schedule_market_reevaluation(
                    TradingEventType.RETRY_TP_TRAILING_CONDITION_CHECK,
                    context,
                    "Case C TP trailing remains active",
                    trigger=ReevaluationTrigger.DEADLINE_OR_MARKET_CHANGE,
                ),
            )
        return create_transition_outcome(
            "PC-12" if is_start else "PC-14",
            state,
            create_queue_event_action(selected_event, context),
        )

    # slope가 증가하면 기준값만 갱신하고 다음 1분봉 또는 시장 변화를 기다린다.
    if (
        event_type is TradingEventType.CASE_C_EMA_INCREASEMENT
        and context.market.confirmed_1m_close
        and runtime.previous_trail_ema_slope is not None
        and context.market.current_close_ema_slope
        > runtime.previous_trail_ema_slope
    ):
        return create_transition_outcome(
            "PC-15",
            state,
            patch(previous_trail_ema_slope=context.market.current_close_ema_slope),
            schedule_market_reevaluation(
                TradingEventType.RETRY_TP_TRAILING_CONDITION_CHECK,
                context,
                "Continue TP trailing after EMA slope increased",
            ),
        )

    # %B가 익절권 아래로 내려오면 TP_FALLBACK 청산을 요청한다.
    if (
        event_type is TradingEventType.CASE_C_SELL_AT_TP_PRICE
        and context.market.realtime_pct_b < Decimal("0.10")
        and _can_start_exit(context)
    ):
        actions = create_exit_order_actions(
            StrategyType.CASE_C,
            ExitReason.TP_FALLBACK,
            PositionReturnState.CASE_C_TP_TRAILING,
            event,
            context,
            attempt_kind=OrderAttemptKind.INITIAL,
        )
        return create_transition_outcome("PC-16", state, *actions)

    # 확정 1분봉 기준 slope가 비증가하면 TP_TRAIL 청산을 요청한다.
    if (
        event_type is TradingEventType.CASE_C_EMA_DECREASEMENT
        and context.market.confirmed_1m_close
        and runtime.previous_trail_ema_slope is not None
        and context.market.current_close_ema_slope
        <= runtime.previous_trail_ema_slope
        and _can_start_exit(context)
    ):
        actions = create_exit_order_actions(
            StrategyType.CASE_C,
            ExitReason.TP_TRAIL,
            PositionReturnState.CASE_C_TP_TRAILING,
            event,
            context,
            attempt_kind=OrderAttemptKind.INITIAL,
        )
        return create_transition_outcome("PC-18", state, *actions)

    shared = _handle_time_exit_or_failure(
        state,
        event,
        context,
        PositionReturnState.CASE_C_TP_TRAILING,
        repeated_failure_id="PC-23",
    )
    if shared is not None:
        return shared

    if (
        event_type is TradingEventType.CASE_C_SELL_FAILED
        and runtime.pending_return_state is PositionReturnState.CASE_C_TP_TRAILING
        and runtime.pending_exit_reason in (ExitReason.TP_FALLBACK, ExitReason.TP_TRAIL)
    ):
        if _get_sell_attempt_kind(event, context) is OrderAttemptKind.RETRY:
            transition_id = "PC-23"
        elif runtime.pending_exit_reason is ExitReason.TP_FALLBACK:
            transition_id = "PC-17"
        else:
            transition_id = "PC-19"
        return _schedule_sell_retry(transition_id, state, context)

    if (
        event_type is TradingEventType.CASE_C_SELL_RETRY
        and runtime.pending_return_state is PositionReturnState.CASE_C_TP_TRAILING
        and runtime.pending_order_id is None
        and runtime.pending_exit_reason is not None
    ):
        actions = create_exit_order_actions(
            StrategyType.CASE_C,
            runtime.pending_exit_reason,
            PositionReturnState.CASE_C_TP_TRAILING,
            event,
            context,
            attempt_kind=OrderAttemptKind.RETRY,
        )
        return create_transition_outcome("PC-22", state, *actions)

    return None


def _handle_time_exit_or_failure(
    state: TradingStateConfiguration,
    event: TradingEvent,
    context: TradingContextView,
    return_state: PositionReturnState,
    *,
    repeated_failure_id: str,
) -> TransitionOutcome | None:
    """
    함수 이름: _handle_time_exit_or_failure()
    기능: 두 Case C 보유 상태가 공유하는 시간 청산과 반복 실패 경로를 처리한다.
    인자: state -> 현재 전체 상태 구성
        event -> 시간 청산 또는 매도 실패 event
        context -> 보유 시간과 pending 청산 정보를 가진 Context
        return_state -> 실패 시 유지할 Case C 포지션 상태
        repeated_failure_id -> 재시도 실패에 기록할 transition ID
    반환값: 선택된 TransitionOutcome 또는 None
    작성 날짜: 2026/08/14
    """
    runtime = context.runtime
    if (
        event.event_type is TradingEventType.CASE_C_TIME_EXIT
        and context.market.holding_elapsed >= SIXTY_MINUTES
        and _can_start_exit(context)
    ):
        actions = create_exit_order_actions(
            StrategyType.CASE_C,
            ExitReason.TIME,
            return_state,
            event,
            context,
            attempt_kind=OrderAttemptKind.INITIAL,
        )
        return create_transition_outcome("PC-08", state, *actions)
    if (
        event.event_type is TradingEventType.CASE_C_SELL_FAILED
        and runtime.pending_exit_reason is ExitReason.TIME
        and runtime.pending_return_state is return_state
    ):
        transition_id = (
            "PC-09"
            if _get_sell_attempt_kind(event, context) is OrderAttemptKind.INITIAL
            else repeated_failure_id
        )
        return _schedule_sell_retry(transition_id, state, context)
    return None


def _select_holding_event(context: TradingContextView) -> TradingEventType | None:
    """
    함수 이름: _select_holding_event()
    기능: 익절권 진입, slope 손절, 시간 청산 순으로 Case C holding event를 선택한다.
    인자: context -> Case C holding Guard 입력을 가진 Context
    반환값: 선택된 TradingEventType 또는 조건이 없으면 None
    작성 날짜: 2026/08/14
    """
    market = context.market
    if market.realtime_pct_b >= Decimal("0.10"):
        return TradingEventType.CASE_C_ENTER_PROFIT_ZONE
    if market.realtime_slope_at_most_minus_055_for_3m:
        return TradingEventType.CASE_C_STOP
    if market.holding_elapsed >= SIXTY_MINUTES:
        return TradingEventType.CASE_C_TIME_EXIT
    return None


def _select_trailing_event(context: TradingContextView) -> TradingEventType | None:
    """
    함수 이름: _select_trailing_event()
    기능: fallback, 확정 1분봉 slope 비교, 시간 청산 순으로 event를 선택한다.
    인자: context -> Case C TP trailing Guard 입력을 가진 Context
    반환값: 선택된 TradingEventType 또는 조건이 없으면 None
    작성 날짜: 2026/08/14
    """
    market = context.market
    previous_slope = context.runtime.previous_trail_ema_slope
    if market.realtime_pct_b < Decimal("0.10"):
        return TradingEventType.CASE_C_SELL_AT_TP_PRICE
    if market.confirmed_1m_close and previous_slope is not None:
        if market.current_close_ema_slope > previous_slope:
            return TradingEventType.CASE_C_EMA_INCREASEMENT
        return TradingEventType.CASE_C_EMA_DECREASEMENT
    if market.holding_elapsed >= SIXTY_MINUTES:
        return TradingEventType.CASE_C_TIME_EXIT
    return None


def _is_case_b_handoff_active(context: TradingContextView) -> bool:
    """
    함수 이름: _is_case_b_handoff_active()
    기능: 낮은 %B에서의 TP_TRAIL 종료가 Case B active 인계를 허용하는지 판정한다.
    인자: context -> Case C 종료 사유와 종료 %B를 가진 Context
    반환값: Case B active 인계 여부
    작성 날짜: 2026/08/14
    """
    runtime = context.runtime
    return (
        runtime.case_c_exit_reason is ExitReason.TP_TRAIL
        and runtime.case_c_exit_pct_b is not None
        and runtime.case_c_exit_pct_b < Decimal("0.40")
    )


def _can_start_exit(context: TradingContextView) -> bool:
    """
    함수 이름: _can_start_exit()
    기능: 기존 주문 ID와 청산 의도가 없어 새 청산을 시작할 수 있는지 판정한다.
    인자: context -> pending 주문과 청산 정보를 가진 Context
    반환값: 신규 청산 의도 생성 가능 여부
    작성 날짜: 2026/08/14
    """
    runtime = context.runtime
    return runtime.pending_order_id is None and runtime.pending_exit_reason is None


def _get_sell_attempt_kind(
    event: TradingEvent,
    context: TradingContextView,
) -> OrderAttemptKind:
    """
    함수 이름: _get_sell_attempt_kind()
    기능: 매도 결과 payload 또는 runtime에서 최초·재시도 구분을 읽는다.
    인자: event -> 매도 결과 TradingEvent
        context -> pending 주문 시도 정보를 가진 Context
    반환값: 확인된 OrderAttemptKind
    작성 날짜: 2026/08/14
    """
    if isinstance(event.payload, SellAttemptPayload):
        return event.payload.attempt_kind
    return context.runtime.pending_order_attempt_kind or OrderAttemptKind.INITIAL


def _schedule_sell_retry(
    transition_id: str,
    state: TradingStateConfiguration,
    context: TradingContextView,
) -> TransitionOutcome:
    """
    함수 이름: _schedule_sell_retry()
    기능: busy loop 없이 같은 Case C 청산 의도의 retry/backoff를 예약한다.
    인자: transition_id -> 실패 경로의 Event-Action Table ID
        state -> 유지할 현재 전체 상태 구성
        context -> lower event와 pending 청산 정보를 가진 Context
    반환값: ScheduleReevaluation을 담은 TransitionOutcome
    작성 날짜: 2026/08/14
    """
    return create_transition_outcome(
        transition_id,
        state,
        schedule_market_reevaluation(
            TradingEventType.CASE_C_SELL_RETRY,
            context,
            "Retry the same Case C exit intent",
            trigger=ReevaluationTrigger.RETRY_BACKOFF,
        ),
    )


def _create_clear_exit_patch() -> PatchRuntimeContext:
    """
    함수 이름: _create_clear_exit_patch()
    기능: 매도 체결 feedback 뒤에만 적용할 pending 청산 필드 초기화 patch를 만든다.
    인자: 없음
    반환값: 청산 필드를 해제하는 PatchRuntimeContext
    작성 날짜: 2026/08/14
    """
    return patch(
        pending_strategy=None,
        pending_order_side=None,
        pending_order_id=None,
        pending_order_attempt_kind=None,
        pending_intent_id=None,
        pending_exit_reason=None,
        pending_exit_pct_b=None,
        pending_return_state=None,
        trading_phase=TradingPhase.IDLE,
    )
