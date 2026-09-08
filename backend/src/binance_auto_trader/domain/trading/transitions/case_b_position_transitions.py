"""Case B 포지션 관리의 PB-01~PB-24 및 PB-23F transition을 구현한다."""

from __future__ import annotations

from dataclasses import replace

from ..action_requests import (
    CancelScheduledEvaluation,
    CloseLowerEvent,
    PatchRuntimeContext,
    QueueEvent,
    ReevaluationTrigger,
    ResetCaseBContext,
    ResetCaseCContext,
    patch,
)
# 표시와 주문 판단이 같은 순수 조건 평가를 공유한다.
from ..conditions import condition_met
from ..context import TradingContextView
from ..events import SellAttemptPayload, TradingEvent, TradingEventType
from ..states import (
    CaseBPositionState,
    ExitReason,
    OrderAttemptKind,
    PositionReturnState,
    RootState,
    StrategyType,
    TradingPhase,
    TradingStateConfiguration,
)
from .base import TransitionOutcome, create_transition_outcome
from .helpers import (
    create_exit_order_actions,
    create_lower_event_initialization_patch,
    is_lower_touch_condition_met,
    create_open_lower_event_action,
    create_queue_event_action,
    schedule_market_reevaluation,
)


# 동일한 청산 흐름이 사용하는 event·transition·reason 대응 관계를 보존한다.
CASE_B_HOLDING_EXIT_BY_EVENT: dict[
    TradingEventType,
    tuple[str, ExitReason],
] = {
    TradingEventType.CASE_B_EMERGENCY_STOP: (
        "PB-06",
        ExitReason.EMERGENCY_STOP,
    ),
    TradingEventType.CASE_B_STOP: ("PB-08", ExitReason.STOP),
    TradingEventType.CASE_B_TIME_EXIT: ("PB-10", ExitReason.TIME),
    TradingEventType.CASE_B_TAKE_PROFIT: ("PB-12", ExitReason.TAKE_PROFIT),
}

CASE_B_INITIAL_FAILURE_TRANSITION_ID_BY_REASON: dict[ExitReason, str] = {
    ExitReason.EMERGENCY_STOP: "PB-07",
    ExitReason.STOP: "PB-09",
    ExitReason.TIME: "PB-11",
    ExitReason.TAKE_PROFIT: "PB-13",
}


def handle_case_b_position_transition(
    state: TradingStateConfiguration,
    event: TradingEvent,
    context: TradingContextView,
) -> TransitionOutcome | None:
    """
    함수 이름: handle_case_b_position_transition()
    기능: Case B 청산 우선순위와 주문 요청·결과의 2단계 transition을 처리한다.
    인자: state -> 현재 전체 상태 구성
        event -> Case B 포지션 submachine에 전달된 TradingEvent
        context -> 포지션·청산 runtime·시장 snapshot
    반환값: 선택된 TransitionOutcome 또는 적용할 transition이 없으면 None
    작성 날짜: 2026/08/14
    """
    position_state = state.case_b_position_state
    if position_state is None:
        return None

    runtime = context.runtime
    event_type = event.event_type

    # 실제 매도 체결과 Entity 반영이 끝난 feedback에서만 CLOSED로 전이한다.
    if event_type is TradingEventType.CASE_B_SELL_FILLED:
        if (
            position_state in (
                CaseBPositionState.CASE_B_HOLDING,
                CaseBPositionState.CASE_B_TREND_HOLD,
            )
            and runtime.position_owner is None
            and runtime.pending_exit_reason is not None
            and runtime.case_b_exit_reason is runtime.pending_exit_reason
        ):
            return create_transition_outcome(
                "PB-23F",
                replace(state, case_b_position_state=CaseBPositionState.CASE_B_CLOSED),
                _create_clear_exit_patch(),
                create_queue_event_action(TradingEventType.CASE_B_SELL_FINISHED, context),
            )
        return None

    # CLOSED 상태에서는 손절 후 즉시 새 lower event를 열지 감시 상태로 돌아갈지 결정한다.
    if position_state is CaseBPositionState.CASE_B_CLOSED:
        if event_type is not TradingEventType.CASE_B_SELL_FINISHED:
            return None
        restart_lower_event = (
            runtime.case_b_exit_reason
            in (ExitReason.STOP, ExitReason.EMERGENCY_STOP)
            and is_lower_touch_condition_met(context)
        )
        if restart_lower_event:
            # 새 scope의 ID와 세 Region initial transition을 한 microstep에 준비한다.
            open_action = create_open_lower_event_action(event, context)
            return create_transition_outcome(
                "PB-23",
                TradingStateConfiguration.create_trade_management_initial_state(),
                CloseLowerEvent(reason="CASE_B_STOP_REENTRY"),
                ResetCaseBContext(),
                ResetCaseCContext(preserve_setup_candle=True),
                open_action,
                create_lower_event_initialization_patch(context),
                patch(position_owner=None),
                QueueEvent(
                    event_type=TradingEventType.ACTIVATE_TRADE_MANAGEMENT,
                    lower_event_id=open_action.lower_event_id,
                ),
                extra_transition_ids=("O-01", "C-01", "B-01"),
                exclusive=True,
            )
        return create_transition_outcome(
            "PB-24",
            TradingStateConfiguration(root_state=RootState.LOWER_TOUCH_WATCH),
            ResetCaseBContext(),
            CloseLowerEvent(reason="CASE_B_CLOSED"),
            CancelScheduledEvaluation(scope="lower-event"),
            exclusive=True,
        )

    # 청산 의도가 생긴 뒤에는 일반 시장 조건을 무시하고 결과·retry event만 허용한다.
    allowed_while_exit_pending = {
        TradingEventType.CASE_B_SELL_FAILED,
        TradingEventType.CASE_B_SELL_RETRY,
    }
    if (
        runtime.pending_exit_reason is not None
        and event_type not in allowed_while_exit_pending
    ):
        return None

    if position_state is CaseBPositionState.CASE_B_HOLDING:
        return _handle_holding(state, event, context)
    if position_state is CaseBPositionState.CASE_B_TREND_HOLD:
        return _handle_trend_hold(state, event, context)
    return None


def _handle_holding(
    state: TradingStateConfiguration,
    event: TradingEvent,
    context: TradingContextView,
) -> TransitionOutcome | None:
    """
    함수 이름: _handle_holding()
    기능: CASE_B_HOLDING의 조건 검사, 최초 청산 주문 및 retry transition을 처리한다.
    인자: state -> 현재 전체 상태 구성
        event -> Case B holding에 전달된 TradingEvent
        context -> 보유 시간·가격·pending 청산 정보가 담긴 Context
    반환값: 선택된 TransitionOutcome 또는 None
    작성 날짜: 2026/08/14
    """
    runtime = context.runtime
    event_type = event.event_type
    return_state = (
        PositionReturnState.CASE_B_TREND_HOLD
        if state.case_b_position_state is CaseBPositionState.CASE_B_TREND_HOLD
        else PositionReturnState.CASE_B_HOLDING
    )

    # 조건 검사 event에서는 우선순위 함수로 정확히 하나의 후속 event만 선택한다.
    if event_type in (
        TradingEventType.START_CASE_B_CONDITION_CHECK,
        TradingEventType.RETRY_CASE_B_CONDITION_CHECK,
    ):
        is_start = event_type is TradingEventType.START_CASE_B_CONDITION_CHECK
        selected_event = _select_holding_exit_event(context)
        if selected_event is None:
            return create_transition_outcome(
                "PB-02" if is_start else "PB-04",
                state,
                schedule_market_reevaluation(
                    TradingEventType.RETRY_CASE_B_CONDITION_CHECK,
                    context,
                    "No Case B exit guard is currently true",
                ),
            )
        return create_transition_outcome(
            "PB-03" if is_start else "PB-05",
            state,
            create_queue_event_action(selected_event, context),
        )

    # 청산 event를 다시 Guard하고 pending 의도가 없을 때만 최초 매도를 요청한다.
    if event_type in CASE_B_HOLDING_EXIT_BY_EVENT:
        transition_id, reason = CASE_B_HOLDING_EXIT_BY_EVENT[event_type]
        if _is_holding_exit_guard_satisfied(event_type, context) and _can_start_exit(context):
            actions = create_exit_order_actions(
                StrategyType.CASE_B,
                reason,
                return_state,
                event,
                context,
                attempt_kind=OrderAttemptKind.INITIAL,
            )
            return create_transition_outcome(transition_id, state, *actions)

    # 강한 반등 조건은 매도가 아니라 Trend Hold 상태 진입을 요청한다.
    if (
        event_type is TradingEventType.CASE_B_UPPER_TREND
        and _is_upper_trend_guard_satisfied(context)
    ):
        if _can_start_exit(context):
            return create_transition_outcome(
                "PB-14",
                replace(
                    state,
                    case_b_position_state=CaseBPositionState.CASE_B_TREND_HOLD,
                ),
                create_queue_event_action(
                    TradingEventType.CASE_B_TREND_HOLD_CONDITION_CHECK,
                    context,
                ),
            )

    # terminal 미체결은 최초·재시도를 구분해 같은 청산 의도의 backoff를 예약한다.
    if event_type is TradingEventType.CASE_B_SELL_FAILED:
        reason = runtime.pending_exit_reason
        if (
            reason in CASE_B_INITIAL_FAILURE_TRANSITION_ID_BY_REASON
            and runtime.pending_return_state is return_state
        ):
            attempt_kind = _get_sell_attempt_kind(event, context)
            transition_id = (
                CASE_B_INITIAL_FAILURE_TRANSITION_ID_BY_REASON[reason]
                if attempt_kind is OrderAttemptKind.INITIAL
                else "PB-20"
            )
            return create_transition_outcome(
                transition_id,
                state,
                schedule_market_reevaluation(
                    TradingEventType.CASE_B_SELL_RETRY,
                    context,
                    "Retry the same Case B holding exit intent",
                    trigger=ReevaluationTrigger.RETRY_BACKOFF,
                ),
            )

    # retry event에서는 기존 pending_intent_id를 재사용해 중복 주문을 방지한다.
    if (
        event_type is TradingEventType.CASE_B_SELL_RETRY
        and runtime.pending_exit_reason
        in (
            ExitReason.EMERGENCY_STOP,
            ExitReason.STOP,
            ExitReason.TIME,
            ExitReason.TAKE_PROFIT,
        )
        and runtime.pending_return_state is return_state
        and runtime.pending_order_id is None
    ):
        actions = create_exit_order_actions(
            StrategyType.CASE_B,
            runtime.pending_exit_reason,
            return_state,
            event,
            context,
            attempt_kind=OrderAttemptKind.RETRY,
        )
        return create_transition_outcome("PB-19", state, *actions)

    return None


def _handle_trend_hold(
    state: TradingStateConfiguration,
    event: TradingEvent,
    context: TradingContextView,
) -> TransitionOutcome | None:
    """
    함수 이름: _handle_trend_hold()
    기능: Trend Hold 약화 감시와 멱등한 매도·재시도 transition을 처리한다.
    인자: state -> 현재 전체 상태 구성
        event -> Trend Hold에 전달된 TradingEvent
        context -> 5초 유지 조건과 pending 청산 정보를 가진 Context
    반환값: 선택된 TransitionOutcome 또는 None
    작성 날짜: 2026/08/14
    """
    runtime = context.runtime
    event_type = event.event_type

    # Lower BB 공통 방어는 Trend Hold에서도 유지하고, 같은 청산 의도의 retry까지 연결한다.
    defensive_events = (
        TradingEventType.CASE_B_EMERGENCY_STOP,
        TradingEventType.CASE_B_STOP,
        TradingEventType.CASE_B_TIME_EXIT,
    )
    if event_type in defensive_events or (
        event_type in (TradingEventType.CASE_B_SELL_FAILED, TradingEventType.CASE_B_SELL_RETRY)
        and runtime.pending_exit_reason in (ExitReason.EMERGENCY_STOP, ExitReason.STOP, ExitReason.TIME)
    ):
        return _handle_holding(state, event, context)

    # 비상손절·확정봉 손절·시간 제한을 먼저 확인한 뒤 5초 약화 조건을 평가한다.
    if event_type is TradingEventType.CASE_B_TREND_HOLD_CONDITION_CHECK:
        for defensive_event in defensive_events:
            if _is_holding_exit_guard_satisfied(defensive_event, context):
                return create_transition_outcome(
                    "PB-16", state, create_queue_event_action(defensive_event, context),
                )
        if not _is_trend_hold_exit_guard_satisfied(context):
            return create_transition_outcome(
                "PB-15",
                state,
                schedule_market_reevaluation(
                    TradingEventType.CASE_B_TREND_HOLD_CONDITION_CHECK,
                    context,
                    "Case B Trend Hold remains valid",
                ),
            )
        return create_transition_outcome(
            "PB-16",
            state,
            create_queue_event_action(TradingEventType.CASE_B_TREND_HOLD_SELL, context),
        )

    if (
        event_type is TradingEventType.CASE_B_TREND_HOLD_SELL
        and _is_trend_hold_exit_guard_satisfied(context)
        and _can_start_exit(context)
    ):
        actions = create_exit_order_actions(
            StrategyType.CASE_B,
            ExitReason.TREND_HOLD,
            PositionReturnState.CASE_B_TREND_HOLD,
            event,
            context,
            attempt_kind=OrderAttemptKind.INITIAL,
        )
        return create_transition_outcome("PB-17", state, *actions)

    if (
        event_type is TradingEventType.CASE_B_SELL_FAILED
        and runtime.pending_exit_reason is ExitReason.TREND_HOLD
        and runtime.pending_return_state is PositionReturnState.CASE_B_TREND_HOLD
    ):
        transition_id = (
            "PB-18"
            if _get_sell_attempt_kind(event, context) is OrderAttemptKind.INITIAL
            else "PB-22"
        )
        return create_transition_outcome(
            transition_id,
            state,
            schedule_market_reevaluation(
                TradingEventType.CASE_B_SELL_RETRY,
                context,
                "Retry the same Case B Trend Hold exit intent",
                trigger=ReevaluationTrigger.RETRY_BACKOFF,
            ),
        )

    if (
        event_type is TradingEventType.CASE_B_SELL_RETRY
        and runtime.pending_exit_reason is ExitReason.TREND_HOLD
        and runtime.pending_return_state is PositionReturnState.CASE_B_TREND_HOLD
        and runtime.pending_order_id is None
    ):
        actions = create_exit_order_actions(
            StrategyType.CASE_B,
            ExitReason.TREND_HOLD,
            PositionReturnState.CASE_B_TREND_HOLD,
            event,
            context,
            attempt_kind=OrderAttemptKind.RETRY,
        )
        return create_transition_outcome("PB-21", state, *actions)

    return None


def _select_holding_exit_event(context: TradingContextView) -> TradingEventType | None:
    """
    함수 이름: _select_holding_exit_event()
    기능: 명세 우선순위에 따라 Case B holding 청산 event 하나만 선택한다.
    인자: context -> 모든 Case B 청산 Guard 입력을 가진 Context
    반환값: 선택된 TradingEventType 또는 조건이 없으면 None
    작성 날짜: 2026/08/14
    """
    # 등록 순서가 아니라 문서의 비상손절→손절→Trend→익절→시간 순서를 명시한다.
    for event_type in (
        TradingEventType.CASE_B_EMERGENCY_STOP,
        TradingEventType.CASE_B_STOP,
        TradingEventType.CASE_B_UPPER_TREND,
        TradingEventType.CASE_B_TAKE_PROFIT,
        TradingEventType.CASE_B_TIME_EXIT,
    ):
        if _is_holding_exit_guard_satisfied(event_type, context):
            return event_type
    return None


def _is_holding_exit_guard_satisfied(
    event_type: TradingEventType,
    context: TradingContextView,
) -> bool:
    """
    함수 이름: _is_holding_exit_guard_satisfied()
    기능: 지정된 Case B holding 청산 event의 Guard를 순수하게 판정한다.
    인자: event_type -> 판정할 청산 event 유형
        context -> 포지션과 시장 snapshot
    반환값: 해당 청산 Guard 만족 여부
    작성 날짜: 2026/08/14
    """
    market = context.market
    entry_price = context.position.entry_price
    if event_type is TradingEventType.CASE_B_EMERGENCY_STOP:
        return (
            entry_price is not None
            and condition_met("b_emergency_stop", context)
        )
    if event_type is TradingEventType.CASE_B_STOP:
        return (
            market.confirmed_30m_close
            and condition_met("b_stop", context)
        )
    if event_type is TradingEventType.CASE_B_UPPER_TREND:
        return _is_upper_trend_guard_satisfied(context)
    if event_type is TradingEventType.CASE_B_TAKE_PROFIT:
        return (
            condition_met("b_profit_zone", context)
            and condition_met("b_take_profit_slope", context)
        )
    if event_type is TradingEventType.CASE_B_TIME_EXIT:
        return condition_met("b_time_exit", context)
    return False


def _is_upper_trend_guard_satisfied(context: TradingContextView) -> bool:
    """
    함수 이름: _is_upper_trend_guard_satisfied()
    기능: %B와 실시간 EMA slope의 5초 연속 유지 조건을 판정한다.
    인자: context -> 5초 유지 결과와 실시간 slope를 가진 Context
    반환값: Trend Hold 진입 가능 여부
    작성 날짜: 2026/08/14
    """
    # 시장 평가기가 확정한 연속 유지 결과를 전략과 표시가 공유한다.
    return (
        condition_met("b_profit_zone", context)
        and condition_met("b_trend_slope", context)
    )


def _is_trend_hold_exit_guard_satisfied(context: TradingContextView) -> bool:
    """
    함수 이름: _is_trend_hold_exit_guard_satisfied()
    기능: Trend Hold의 slope 또는 %B 약화가 5초 지속되었는지 판정한다.
    인자: context -> Trend Hold 약화 유지 결과를 가진 Context
    반환값: Trend Hold 청산 조건 만족 여부
    작성 날짜: 2026/08/14
    """
    # 시장 평가기가 확정한 연속 유지 결과를 전략과 표시가 공유한다.
    return (
        condition_met("b_trend_exit_slope", context)
        or condition_met("b_trend_exit_pct_b", context)
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
