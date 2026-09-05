"""Guard 판정과 action request 생성을 위한 공통 순수 함수를 정의한다."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from ..action_requests import (
    OpenLowerEvent,
    PatchRuntimeContext,
    QueueEvent,
    ReevaluationTrigger,
    ScheduleReevaluation,
    SubmitOrder,
    patch,
)
# 표시와 주문 판단이 같은 순수 조건 평가를 공유한다.
from ..conditions import condition_met
from ..context import TradingContextView
from ..events import TradingEvent, TradingEventType
from ..states import (
    ExitReason,
    OrderAttemptKind,
    OrderSide,
    PositionReturnState,
    StrategyType,
    TradingPhase,
)


# Event-Action Table의 시간 경곗값을 의미 있는 이름으로 한 곳에서 관리한다.
THREE_MINUTES = timedelta(minutes=3)
SIXTY_MINUTES = timedelta(minutes=60)
THREE_HOURS = timedelta(hours=3)
SIX_HOURS = timedelta(hours=6)


def is_lower_touch_condition_met(context: TradingContextView) -> bool:
    """
    함수 이름: is_lower_touch_condition_met()
    기능: 실시간 가격 또는 확정 30분봉 저가의 하단 Bollinger Band 접촉을 판정한다.
    인자: context -> 동일 평가 시점의 TradingContextView
    반환값: 하단 Band 접촉 여부
    작성 날짜: 2026/08/14
    """
    market = context.market
    return condition_met("lower_price", context) or (
        market.confirmed_30m_close and condition_met("lower_close", context)
    )


def resolve_lower_event_id(event: TradingEvent, context: TradingContextView) -> str:
    """
    함수 이름: resolve_lower_event_id()
    기능: adapter가 새 ID를 주지 않은 경우에도 결정적인 lower event ID를 생성한다.
    인자: event -> 현재 처리 중인 TradingEvent
        context -> 활성 lower event 정보를 가진 TradingContextView
    반환값: 새 lower event ID
    작성 날짜: 2026/08/14
    """
    # 현재 활성 ID와 다른 명시적 ID는 새 event 식별자로 그대로 사용한다.
    if (
        event.lower_event_id
        and event.lower_event_id != context.runtime.lower_event_id
    ):
        return event.lower_event_id

    # 명시적 새 ID가 없으면 candle과 queue sequence를 결합해 재현 가능한 ID를 만든다.
    candle_id = event.candle_id or context.market.current_30m_candle_id or "realtime"
    return f"lower:{candle_id}:{event.sequence_number}"


def create_open_lower_event_action(
    event: TradingEvent,
    context: TradingContextView,
) -> OpenLowerEvent:
    """
    함수 이름: create_open_lower_event_action()
    기능: 현재 touch candle 값을 고정한 OpenLowerEvent action을 생성한다.
    인자: event -> 하단 접촉을 알린 TradingEvent
        context -> touch 시점 시장 snapshot
    반환값: Controller가 실행할 OpenLowerEvent
    작성 날짜: 2026/08/14
    """
    market = context.market
    return OpenLowerEvent(
        lower_event_id=resolve_lower_event_id(event, context),
        touch_time=event.occurred_at,
        candle_id=event.candle_id or market.current_30m_candle_id,
        touch_candle_low=market.current_30m_low,
        lower_band_at_touch=market.lower_band,
        touch_candle_bbw=market.touch_candle_bbw,
    )


def create_lower_event_initialization_patch(context: TradingContextView) -> PatchRuntimeContext:
    """
    함수 이름: create_lower_event_initialization_patch()
    기능: G-02와 G-03의 lower event-local flag 초기화 patch를 생성한다.
    인자: context -> touch candle BBW를 포함한 TradingContextView
    반환값: event-local 초기값을 담은 PatchRuntimeContext
    작성 날짜: 2026/08/14
    """
    case_b_enabled = context.market.touch_candle_bbw < Decimal("0.02")
    return patch(
        case_b_enabled=case_b_enabled,
        case_c_enabled=True,
        allow_new_case_c_setup=True,
        case_c_consumed_for_event=False,
        case_c_recovery_confirmed=False,
        case_c_exit_reason=None,
        case_c_exit_pct_b=None,
        case_b_entry_paused=False,
        case_b_only_until_next_lower_touch=False,
    )


def create_queue_event_action(
    event_type: TradingEventType,
    context: TradingContextView,
) -> QueueEvent:
    """
    함수 이름: create_queue_event_action()
    기능: 현재 lower event 범위에 속하는 내부 후속 event action을 생성한다.
    인자: event_type -> 후속 처리할 event 유형
        context -> 활성 lower event ID를 가진 TradingContextView
    반환값: 생성된 QueueEvent
    작성 날짜: 2026/08/14
    """
    return QueueEvent(
        event_type=event_type,
        lower_event_id=context.runtime.lower_event_id,
    )


def schedule_market_reevaluation(
    event_type: TradingEventType,
    context: TradingContextView,
    reason: str,
    *,
    trigger: ReevaluationTrigger = ReevaluationTrigger.DEADLINE_OR_MARKET_CHANGE,
    earliest_delay: timedelta | None = None,
) -> ScheduleReevaluation:
    """
    함수 이름: schedule_market_reevaluation()
    기능: 즉시 반복 없이 시장 변경 또는 deadline에서 실행할 재평가를 생성한다.
    인자: event_type -> 미래에 발생시킬 재평가 event 유형
        context -> 활성 lower event 범위를 가진 TradingContextView
        reason -> scheduler trace에 기록할 재평가 사유
        trigger -> 재평가를 허용할 조건
        earliest_delay -> 재평가 전 최소 지연 시간
    반환값: 생성된 ScheduleReevaluation
    작성 날짜: 2026/08/14
    """
    return ScheduleReevaluation(
        event_type=event_type,
        trigger=trigger,
        earliest_delay=earliest_delay,
        lower_event_id=context.runtime.lower_event_id,
        reason=reason,
    )


def create_entry_order_actions(
    strategy: StrategyType,
    attempt_kind: OrderAttemptKind,
    event: TradingEvent,
    context: TradingContextView,
) -> tuple[PatchRuntimeContext, SubmitOrder]:
    """
    함수 이름: create_entry_order_actions()
    기능: position_owner를 선반영하지 않고 매수 자리를 예약한 뒤 주문 action을 생성한다.
    인자: strategy -> 진입을 요청한 Case 전략
        attempt_kind -> 최초 주문 또는 재시도 구분
        event -> 주문 결정을 만든 TradingEvent
        context -> 기존 주문 의도와 lower event 정보를 가진 Context
    반환값: runtime 예약 patch와 SubmitOrder의 순서가 있는 tuple
    작성 날짜: 2026/08/14
    """
    # 재시도에서는 기존 의도 ID를 재사용해 client order key의 멱등성을 지킨다.
    intent_id = (
        context.runtime.pending_intent_id
        or f"{strategy}:BUY:{context.runtime.lower_event_id}:{event.sequence_number}"
    )
    return (
        patch(
            pending_strategy=strategy,
            pending_order_side=OrderSide.BUY,
            pending_order_attempt_kind=attempt_kind,
            pending_intent_id=intent_id,
            trading_phase=TradingPhase.ENTRY_ORDER_PENDING,
        ),
        SubmitOrder(
            strategy=strategy,
            side=OrderSide.BUY,
            attempt_kind=attempt_kind,
            idempotency_key=intent_id,
        ),
    )


def create_exit_order_actions(
    strategy: StrategyType,
    reason: ExitReason,
    return_state: PositionReturnState,
    event: TradingEvent,
    context: TradingContextView,
    *,
    attempt_kind: OrderAttemptKind,
) -> tuple[PatchRuntimeContext, SubmitOrder]:
    """
    함수 이름: create_exit_order_actions()
    기능: 매도 체결 feedback까지 청산 사유와 복귀 상태를 보존하는 action을 생성한다.
    인자: strategy -> 포지션을 소유한 Case 전략
        reason -> 선택된 청산 사유
        return_state -> 매도 실패 시 유지할 포지션 상태
        event -> 청산 결정을 만든 TradingEvent
        context -> 기존 청산 의도와 lower event 정보를 가진 Context
        attempt_kind -> 최초 주문 또는 재시도 구분
    반환값: runtime 예약 patch와 SubmitOrder의 순서가 있는 tuple
    작성 날짜: 2026/08/14
    """
    # 모든 재시도는 최초 청산 의도의 ID와 Case C 매도 판단 %B를 재사용한다.
    intent_id = context.runtime.pending_intent_id or (
        f"{strategy}:SELL:{reason}:{context.runtime.lower_event_id}:{event.sequence_number}"
    )
    exit_pct_b_at_intent = None
    if strategy is StrategyType.CASE_C:
        exit_pct_b_at_intent = (
            context.market.realtime_pct_b
            if attempt_kind is OrderAttemptKind.INITIAL
            else context.runtime.pending_exit_pct_b
        )
    changes: dict[str, object] = {
        "pending_strategy": strategy,
        "pending_order_side": OrderSide.SELL,
        "pending_order_attempt_kind": attempt_kind,
        "pending_intent_id": intent_id,
        "trading_phase": TradingPhase.EXIT_ORDER_PENDING,
    }

    # 최초 시도에서만 청산 사유와 복귀 상태를 새로 고정한다.
    if attempt_kind is OrderAttemptKind.INITIAL:
        changes["pending_exit_reason"] = reason
        changes["pending_exit_pct_b"] = exit_pct_b_at_intent
        changes["pending_return_state"] = return_state

    return (
        patch(**changes),
        SubmitOrder(
            strategy=strategy,
            side=OrderSide.SELL,
            attempt_kind=attempt_kind,
            idempotency_key=intent_id,
            exit_reason=reason,
            exit_pct_b_at_intent=exit_pct_b_at_intent,
        ),
    )
