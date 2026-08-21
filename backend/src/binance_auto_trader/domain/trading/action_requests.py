"""TradingController가 실행할 수 있도록 직렬화 가능한 action request를 정의한다."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import TypeAlias

from .events import TradingEventPayload, TradingEventType
from .states import (
    ExitReason,
    OrderAttemptKind,
    OrderSide,
    PositionReturnState,
    StrategyType,
    TradingPhase,
)


class RuntimeField(StrEnum):
    """
    클래스 이름: RuntimeField
    기능: PatchRuntimeContext가 변경할 수 있는 runtime 필드를 허용 목록으로 제한한다.
    작성 날짜: 2026/08/14
    """

    POSITION_OWNER = "position_owner"
    PENDING_STRATEGY = "pending_strategy"
    PENDING_ORDER_SIDE = "pending_order_side"
    PENDING_ORDER_ID = "pending_order_id"
    PENDING_ORDER_ATTEMPT_KIND = "pending_order_attempt_kind"
    PENDING_INTENT_ID = "pending_intent_id"
    TRADING_PHASE = "trading_phase"
    CASE_B_ENABLED = "case_b_enabled"
    CASE_C_ENABLED = "case_c_enabled"
    CASE_B_ENTRY_PAUSED = "case_b_entry_paused"
    CASE_B_ONLY_UNTIL_NEXT_LOWER_TOUCH = "case_b_only_until_next_lower_touch"
    ALLOW_NEW_CASE_C_SETUP = "allow_new_case_c_setup"
    CASE_C_CONSUMED_FOR_EVENT = "case_c_consumed_for_event"
    CASE_C_RECOVERY_CONFIRMED = "case_c_recovery_confirmed"
    SIGNAL_CREATED = "signal_created"
    SIGNAL_CANDLE_ID = "signal_candle_id"
    SIGNAL_TIME = "signal_time"
    LAST_CASE_C_SETUP_CANDLE_ID = "last_case_c_setup_candle_id"
    FLUSH_LOW = "flush_low"
    FLUSH_LOW_PCT_B = "flush_low_pct_b"
    FLUSH_LOW_TIME = "flush_low_time"
    CURRENT_OPEN_PCT_B = "current_open_pct_b"
    TIMER_BASE_PCT_B = "timer_base_pct_b"
    TIMER_BASE_TIME = "timer_base_time"
    ENTRY_PCT_B = "entry_pct_b"
    TP_PRICE = "tp_price"
    PREVIOUS_TRAIL_EMA_SLOPE = "previous_trail_ema_slope"
    PENDING_EXIT_REASON = "pending_exit_reason"
    PENDING_RETURN_STATE = "pending_return_state"
    CASE_B_EXIT_REASON = "case_b_exit_reason"
    CASE_C_EXIT_REASON = "case_c_exit_reason"
    CASE_C_EXIT_PCT_B = "case_c_exit_pct_b"


RuntimeValue: TypeAlias = (
    str
    | bool
    | Decimal
    | datetime
    | StrategyType
    | OrderSide
    | OrderAttemptKind
    | TradingPhase
    | ExitReason
    | PositionReturnState
    | None
)


@dataclass(frozen=True, slots=True)
class RuntimeFieldChange:
    """
    클래스 이름: RuntimeFieldChange
    기능: 허용된 runtime 필드 하나와 적용할 typed 값을 묶는다.
    작성 날짜: 2026/08/14
    """

    field: RuntimeField
    value: RuntimeValue


@dataclass(frozen=True, slots=True)
class PatchRuntimeContext:
    """
    클래스 이름: PatchRuntimeContext
    기능: TradingContext domain method로 순서대로 적용할 runtime 변경 목록을 보존한다.
    작성 날짜: 2026/08/14
    """

    changes: tuple[RuntimeFieldChange, ...]


@dataclass(frozen=True, slots=True)
class OpenLowerEvent:
    """
    클래스 이름: OpenLowerEvent
    기능: 새 하단 터치 event와 고정된 touch candle snapshot 생성을 요청한다.
    작성 날짜: 2026/08/14
    """

    lower_event_id: str
    touch_time: datetime
    candle_id: str | None
    touch_candle_low: Decimal
    lower_band_at_touch: Decimal
    touch_candle_bbw: Decimal


@dataclass(frozen=True, slots=True)
class CloseLowerEvent:
    """
    클래스 이름: CloseLowerEvent
    기능: 현재 하단 터치 event 범위의 종료와 정리를 요청한다.
    작성 날짜: 2026/08/14
    """

    reason: str


@dataclass(frozen=True, slots=True)
class ResetCaseBContext:
    """
    클래스 이름: ResetCaseBContext
    기능: STM 상태 변경 없이 Case B signal runtime 값의 초기화를 요청한다.
    작성 날짜: 2026/08/14
    """

    preserve_signal: bool = False


@dataclass(frozen=True, slots=True)
class ResetCaseCContext:
    """
    클래스 이름: ResetCaseCContext
    기능: STM 상태 변경 없이 Case C setup과 trailing runtime 값의 초기화를 요청한다.
    작성 날짜: 2026/08/14
    """

    preserve_exit_result: bool = False


@dataclass(frozen=True, slots=True)
class QueueEvent:
    """
    클래스 이름: QueueEvent
    기능: 현재 action batch가 끝난 뒤 처리할 내부 후속 event 등록을 요청한다.
    작성 날짜: 2026/08/14
    """

    event_type: TradingEventType
    payload: TradingEventPayload | None = None
    lower_event_id: str | None = None
    candle_id: str | None = None
    order_id: str | None = None


class ReevaluationTrigger(StrEnum):
    """
    클래스 이름: ReevaluationTrigger
    기능: scheduler가 재평가 event를 발생시킬 수 있는 조건을 정의한다.
    작성 날짜: 2026/08/14
    """

    MARKET_CHANGE = "MARKET_CHANGE"
    NEXT_1M_CLOSE = "NEXT_1M_CLOSE"
    NEXT_30M_CLOSE = "NEXT_30M_CLOSE"
    DEADLINE_OR_MARKET_CHANGE = "DEADLINE_OR_MARKET_CHANGE"
    RETRY_BACKOFF = "RETRY_BACKOFF"


@dataclass(frozen=True, slots=True)
class ScheduleReevaluation:
    """
    클래스 이름: ScheduleReevaluation
    기능: 즉시 반복 없이 시장 변경·candle 마감·deadline 기반의 미래 재평가를 요청한다.
    작성 날짜: 2026/08/14
    """

    event_type: TradingEventType
    trigger: ReevaluationTrigger
    earliest_delay: timedelta | None = None
    lower_event_id: str | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class CancelScheduledEvaluation:
    """
    클래스 이름: CancelScheduledEvaluation
    기능: 지정된 상태 또는 event 범위에 속한 scheduler 항목 취소를 요청한다.
    작성 날짜: 2026/08/14
    """

    scope: str


@dataclass(frozen=True, slots=True)
class SubmitOrder:
    """
    클래스 이름: SubmitOrder
    기능: TradingController에 하나의 멱등한 매수·매도 주문 의도 실행을 요청한다.
    작성 날짜: 2026/08/14
    """

    strategy: StrategyType
    side: OrderSide
    attempt_kind: OrderAttemptKind
    idempotency_key: str
    exit_reason: ExitReason | None = None


@dataclass(frozen=True, slots=True)
class CancelPendingOrder:
    """
    클래스 이름: CancelPendingOrder
    기능: 중지 또는 전략 우선순위로 무효가 된 pending 주문의 취소를 요청한다.
    작성 날짜: 2026/08/14
    """

    order_id: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class ForceSellAll:
    """
    클래스 이름: ForceSellAll
    기능: STOP 또는 상단 BB 안전 종료 절차에서 남은 포지션 전량 매도를 요청한다.
    작성 날짜: 2026/08/14
    """

    retry: bool = False
    use_retry_policy: bool = False

@dataclass(frozen=True, slots=True)
class StopTradingRuntime:
    """
    클래스 이름: StopTradingRuntime
    기능: 신규 event 수신·구독·timer·runtime consumer의 안전한 종료를 요청한다.
    작성 날짜: 2026/08/14
    """

    reason: str


@dataclass(frozen=True, slots=True)
class ReconcileOrder:
    """
    클래스 이름: ReconcileOrder
    기능: 신규 제출 없이 기존 주문 ID의 상태와 실제 fill을 재조회·조정하도록 요청한다.
    작성 날짜: 2026/08/14
    """

    order_id: str
    stop_after_reconciliation: bool = False


TradingActionRequest: TypeAlias = (
    PatchRuntimeContext
    | OpenLowerEvent
    | CloseLowerEvent
    | ResetCaseBContext
    | ResetCaseCContext
    | QueueEvent
    | ScheduleReevaluation
    | CancelScheduledEvaluation
    | SubmitOrder
    | CancelPendingOrder
    | ForceSellAll
    | StopTradingRuntime
    | ReconcileOrder
)


def patch(**changes: RuntimeValue) -> PatchRuntimeContext:
    """
    함수 이름: patch()
    기능: 허용 목록 밖의 필드를 거부하면서 typed runtime patch를 생성한다.
    인자: changes -> runtime 필드 이름과 적용할 값의 keyword 목록
    반환값: 순서가 보존된 PatchRuntimeContext
    작성 날짜: 2026/08/14
    """
    # 문자열 필드 이름을 RuntimeField로 변환해 임의의 Context 쓰기를 차단한다.
    mutations = tuple(
        RuntimeFieldChange(RuntimeField(field_name), value)
        for field_name, value in changes.items()
    )

    return PatchRuntimeContext(mutations)
