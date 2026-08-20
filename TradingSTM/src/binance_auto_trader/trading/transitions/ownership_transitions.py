"""병렬 포지션 소유권 Region의 O-01~O-09 transition을 구현한다."""

from __future__ import annotations

from dataclasses import replace

from ..action_requests import ReevaluationTrigger, ScheduleReevaluation, patch
from ..context import TradingContextView
from ..events import BuyAttemptPayload, TradingEvent, TradingEventType
from ..states import (
    CaseBPositionState,
    CaseCPositionState,
    OrderAttemptKind,
    OwnershipState,
    StrategyType,
    TradingStateConfiguration,
)
from .base import TransitionOutcome, create_transition_outcome
from .helpers import THREE_HOURS, create_entry_order_actions, create_queue_event_action


def handle_ownership_transition(
    state: TradingStateConfiguration,
    event: TradingEvent,
    context: TradingContextView,
) -> TransitionOutcome | None:
    """
    함수 이름: handle_ownership_transition()
    기능: 무포지션 상태에서 주문 결과와 재시도 event에 맞는 소유권 transition을 선택한다.
    인자: state -> 현재 전체 상태 구성
        event -> Region 1에 broadcast된 TradingEvent
        context -> 주문·포지션이 반영된 TradingContextView
    반환값: 선택된 TransitionOutcome 또는 적용할 transition이 없으면 None
    작성 날짜: 2026/08/14
    """
    # 소유권을 이미 획득한 경우에는 활성 Case별 포지션 submachine이 처리한다.
    if state.ownership_state is not OwnershipState.NO_POSITION:
        return None

    runtime = context.runtime
    event_type = event.event_type

    # Case B 실제 체결 feedback에서만 소유권과 PB initial 상태를 함께 활성화한다.
    if (
        event_type is TradingEventType.CASE_B_POSITION_OPENED
        and runtime.position_owner is StrategyType.CASE_B
        and runtime.pending_order_id is None
        and context.position.is_open
    ):
        state_after = replace(
            state,
            ownership_state=OwnershipState.CASE_B_POSITION_MANAGEMENT,
            case_b_position_state=CaseBPositionState.CASE_B_HOLDING,
        )
        return create_transition_outcome(
            "O-02",
            state_after,
            create_queue_event_action(TradingEventType.START_CASE_B_CONDITION_CHECK, context),
            extra_transition_ids=("PB-01",),
        )

    # 최초 Case B 매수 실패는 Controller의 backoff 정책을 통해 재시도한다.
    if (
        event_type is TradingEventType.CASE_B_BUY_FAILED
        and _get_buy_attempt_kind(event) is OrderAttemptKind.INITIAL
    ):
        if runtime.position_owner is None:
            return create_transition_outcome(
                "O-03",
                state,
                _create_entry_retry_action(
                    TradingEventType.CASE_B_BUY_RETRY,
                    context,
                    "Case B initial buy failed",
                ),
            )

    # Case B retry event에서도 owner·pending·signal 유효성을 다시 확인한다.
    if event_type is TradingEventType.CASE_B_BUY_RETRY:
        if (
            runtime.position_owner is None
            and runtime.pending_order_id is None
            and not runtime.case_b_entry_paused
            and runtime.signal_created
            and context.market.signal_elapsed <= THREE_HOURS
        ):
            actions = create_entry_order_actions(
                StrategyType.CASE_B,
                OrderAttemptKind.RETRY,
                event,
                context,
            )
            return create_transition_outcome("O-04", state, *actions)

    if (
        event_type is TradingEventType.CASE_B_BUY_FAILED
        and _get_buy_attempt_kind(event) is OrderAttemptKind.RETRY
    ):
        if runtime.position_owner is None:
            return create_transition_outcome(
                "O-05",
                state,
                _create_entry_retry_action(
                    TradingEventType.CASE_B_BUY_RETRY,
                    context,
                    "Case B retry buy failed",
                ),
            )

    # Case C 실제 체결 feedback에서 소유권을 바꾸고 Case B 진입을 일시 정지한다.
    if (
        event_type is TradingEventType.CASE_C_POSITION_OPENED
        and runtime.position_owner is StrategyType.CASE_C
        and runtime.pending_order_id is None
        and context.position.is_open
    ):
        state_after = replace(
            state,
            ownership_state=OwnershipState.CASE_C_POSITION_MANAGEMENT,
            case_c_position_state=CaseCPositionState.CASE_C_HOLDING,
        )
        return create_transition_outcome(
            "O-06",
            state_after,
            patch(case_b_entry_paused=True),
            create_queue_event_action(TradingEventType.START_CASE_C_CONDITION_CHECK, context),
            extra_transition_ids=("PC-01",),
        )

    # 최초 Case C 매수 실패도 즉시 반복하지 않고 backoff scheduler를 사용한다.
    if (
        event_type is TradingEventType.CASE_C_BUY_FAILED
        and _get_buy_attempt_kind(event) is OrderAttemptKind.INITIAL
    ):
        if runtime.position_owner is None:
            return create_transition_outcome(
                "O-07",
                state,
                _create_entry_retry_action(
                    TradingEventType.CASE_C_BUY_RETRY,
                    context,
                    "Case C initial buy failed",
                ),
            )

    # Case C retry는 같은 lower event에서 setup 권한이 남은 경우에만 주문한다.
    if event_type is TradingEventType.CASE_C_BUY_RETRY:
        if (
            runtime.position_owner is None
            and runtime.pending_order_id is None
            and runtime.allow_new_case_c_setup
            and not runtime.case_c_consumed_for_event
        ):
            actions = create_entry_order_actions(
                StrategyType.CASE_C,
                OrderAttemptKind.RETRY,
                event,
                context,
            )
            return create_transition_outcome("O-08", state, *actions)

    if (
        event_type is TradingEventType.CASE_C_BUY_FAILED
        and _get_buy_attempt_kind(event) is OrderAttemptKind.RETRY
    ):
        if runtime.position_owner is None:
            return create_transition_outcome(
                "O-09",
                state,
                _create_entry_retry_action(
                    TradingEventType.CASE_C_BUY_RETRY,
                    context,
                    "Case C retry buy failed",
                ),
            )

    return None


def _get_buy_attempt_kind(event: TradingEvent) -> OrderAttemptKind | None:
    """
    함수 이름: _get_buy_attempt_kind()
    기능: 정규화된 매수 결과 event에서 최초·재시도 구분을 읽는다.
    인자: event -> 매수 결과 TradingEvent
    반환값: OrderAttemptKind 또는 payload가 다르면 None
    작성 날짜: 2026/08/14
    """
    if isinstance(event.payload, BuyAttemptPayload):
        return event.payload.attempt_kind
    return None


def _create_entry_retry_action(
    event_type: TradingEventType,
    context: TradingContextView,
    reason: str,
) -> ScheduleReevaluation:
    """
    함수 이름: _create_entry_retry_action()
    기능: 매수 재시도를 Controller의 retry/backoff scheduler로 전달한다.
    인자: event_type -> backoff 뒤 발생시킬 매수 재시도 event
        context -> 활성 lower event ID를 가진 Context
        reason -> retry trace에 기록할 사유
    반환값: 생성된 ScheduleReevaluation
    작성 날짜: 2026/08/14
    """
    return ScheduleReevaluation(
        event_type=event_type,
        trigger=ReevaluationTrigger.RETRY_BACKOFF,
        lower_event_id=context.runtime.lower_event_id,
        reason=reason,
    )
