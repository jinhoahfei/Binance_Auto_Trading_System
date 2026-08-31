"""TradingSTM과 직렬 event queue가 사용하는 불변 event 타입을 정의한다."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import IntEnum, StrEnum
from typing import TypeAlias

from .risk import RiskBlockReason
from .states import OrderAttemptKind, StrategyType


class EventPriority(IntEnum):
    """
    클래스 이름: EventPriority
    기능: 직렬 event queue의 처리 우선순위를 정의하며 작은 값이 먼저 처리된다.
    작성 날짜: 2026/08/14
    """

    INTERNAL = 0
    USER_COMMAND = 10
    ORDER_OUTCOME = 20
    MARKET = 30


class TradingEventType(StrEnum):
    """
    클래스 이름: TradingEventType
    기능: Event-Action Table과 시장 broadcast에서 사용하는 event 유형을 정의한다.
    작성 날짜: 2026/08/14
    """

    MARKET_DATA_UPDATED = "MARKET_DATA_UPDATED"
    LOGIC_STARTED = "LOGIC_STARTED"
    LOWER_BAND_TOUCHED = "LOWER_BAND_TOUCHED"
    NEW_30M_LOWER_BAND_TOUCHED = "NEW_30M_LOWER_BAND_TOUCHED"
    TRADE_MANAGEMENT_COMPLETED = "TRADE_MANAGEMENT_COMPLETED"
    STOP_CONFIRMED = "STOP_CONFIRMED"
    FORCE_SELL_FINISHED = "FORCE_SELL_FINISHED"
    FORCE_SELL_FAILED = "FORCE_SELL_FAILED"
    UPPER_BAND_TOUCHED = "UPPER_BAND_TOUCHED"
    ACTIVATE_TRADE_MANAGEMENT = "ACTIVATE_TRADE_MANAGEMENT"

    CASE_B_POSITION_OPENED = "CASE_B_POSITION_OPENED"
    CASE_B_BUY_FAILED = "CASE_B_BUY_FAILED"
    CASE_B_BUY_RETRY = "CASE_B_BUY_RETRY"
    CASE_C_POSITION_OPENED = "CASE_C_POSITION_OPENED"
    CASE_C_BUY_FAILED = "CASE_C_BUY_FAILED"
    CASE_C_BUY_RETRY = "CASE_C_BUY_RETRY"

    START_CASE_B_CONDITION_CHECK = "START_CASE_B_CONDITION_CHECK"
    RETRY_CASE_B_CONDITION_CHECK = "RETRY_CASE_B_CONDITION_CHECK"
    CASE_B_EMERGENCY_STOP = "CASE_B_EMERGENCY_STOP"
    CASE_B_STOP = "CASE_B_STOP"
    CASE_B_TIME_EXIT = "CASE_B_TIME_EXIT"
    CASE_B_TAKE_PROFIT = "CASE_B_TAKE_PROFIT"
    CASE_B_UPPER_TREND = "CASE_B_UPPER_TREND"
    CASE_B_TREND_HOLD_CONDITION_CHECK = "CASE_B_TREND_HOLD_CONDITION_CHECK"
    CASE_B_TREND_HOLD_SELL = "CASE_B_TREND_HOLD_SELL"
    CASE_B_SELL_FAILED = "CASE_B_SELL_FAILED"
    CASE_B_SELL_RETRY = "CASE_B_SELL_RETRY"
    CASE_B_SELL_FILLED = "CASE_B_SELL_FILLED"
    CASE_B_SELL_FINISHED = "CASE_B_SELL_FINISHED"

    START_CASE_C_CONDITION_CHECK = "START_CASE_C_CONDITION_CHECK"
    RETRY_CASE_C_CONDITION_CHECK = "RETRY_CASE_C_CONDITION_CHECK"
    CASE_C_STOP = "CASE_C_STOP"
    CASE_C_TIME_EXIT = "CASE_C_TIME_EXIT"
    CASE_C_ENTER_PROFIT_ZONE = "CASE_C_ENTER_PROFIT_ZONE"
    START_TP_TRAILING_CONDITION_CHECK = "START_TP_TRAILING_CONDITION_CHECK"
    RETRY_TP_TRAILING_CONDITION_CHECK = "RETRY_TP_TRAILING_CONDITION_CHECK"
    CASE_C_EMA_INCREASEMENT = "CASE_C_EMA_INCREASEMENT"
    CASE_C_SELL_AT_TP_PRICE = "CASE_C_SELL_AT_TP_PRICE"
    CASE_C_EMA_DECREASEMENT = "CASE_C_EMA_DECREASEMENT"
    CASE_C_SELL_FAILED = "CASE_C_SELL_FAILED"
    CASE_C_SELL_RETRY = "CASE_C_SELL_RETRY"
    CASE_C_SELL_FILLED = "CASE_C_SELL_FILLED"
    CASE_C_SELL_FINISHED = "CASE_C_SELL_FINISHED"
    CHECK_CASE_C_RECOVERY = "CHECK_CASE_C_RECOVERY"
    CHECK_CASE_B_HANDOFF = "CHECK_CASE_B_HANDOFF"
    CASE_B_ACTIVE_RESUME = "CASE_B_ACTIVE_RESUME"
    CASE_B_WAIT_ONLY = "CASE_B_WAIT_ONLY"
    BUY_RISK_BLOCKED = "BUY_RISK_BLOCKED"

    THIRTY_MINUTE_CANDLE_CLOSED = "THIRTY_MINUTE_CANDLE_CLOSED"
    START_CASE_B_WAIT_PULLBACK_CONDITION_CHECK = (
        "START_CASE_B_WAIT_PULLBACK_CONDITION_CHECK"
    )
    RETRY_CASE_B_WAIT_PULLBACK_CONDITION_CHECK = (
        "RETRY_CASE_B_WAIT_PULLBACK_CONDITION_CHECK"
    )

    RETRY_C_WAIT_SETUP = "RETRY_C_WAIT_SETUP"
    START_CASE_C_SETUP_CONDITION_CHECK = "START_CASE_C_SETUP_CONDITION_CHECK"
    RETRY_CASE_C_SETUP_CONDITION_CHECK = "RETRY_CASE_C_SETUP_CONDITION_CHECK"


@dataclass(frozen=True, slots=True)
class BuyAttemptPayload:
    """
    클래스 이름: BuyAttemptPayload
    기능: 매수 주문 결과 event에 최초·재시도 구분 정보를 담는다.
    작성 날짜: 2026/08/14
    """

    attempt_kind: OrderAttemptKind


@dataclass(frozen=True, slots=True)
class SellAttemptPayload:
    """
    클래스 이름: SellAttemptPayload
    기능: 매도 실패 event가 최초 시도인지 재시도인지 구분한다.
    작성 날짜: 2026/08/14
    """

    attempt_kind: OrderAttemptKind


@dataclass(frozen=True, slots=True)
class BuyRiskBlockedPayload:
    """
    클래스 이름: BuyRiskBlockedPayload
    기능: 제출 전 BUY 위험 차단의 전략과 typed 첫 사유를 STM feedback에 보존한다.
    작성 날짜: 2026/08/24
    """

    strategy: StrategyType
    reason: RiskBlockReason

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 위험 차단 feedback이 canonical 전략과 RiskBlockReason만 담는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # 문자열이나 유사 enum이 주문 상태 복구 분기를 위장하지 못하게 identity를 고정한다.
        if not isinstance(self.strategy, StrategyType):
            raise TypeError("strategy must be a StrategyType")
        if not isinstance(self.reason, RiskBlockReason):
            raise TypeError("reason must be a RiskBlockReason")


@dataclass(frozen=True, slots=True)
class ForceSellOutcomePayload:
    """
    클래스 이름: ForceSellOutcomePayload
    기능: 전량 매도 결과의 Position 반영·이력 저장·미체결 확정 여부를 보존한다.
    작성 날짜: 2026/08/14
    """

    execution_applied: bool = True
    history_persisted: bool = True
    terminal_unfilled: bool = False

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 강제 매도 결과 증거가 truthy 대체값이 아닌 exact bool인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 세 종료 증거를 한 tuple로 묶어 모든 값에 exact-bool 규칙을 동일 적용한다.
        outcome_flags = (
            self.execution_applied,
            self.history_persisted,
            self.terminal_unfilled,
        )

        # 문자열과 정수 truthiness가 종료·재시도 증거로 오인되지 않게 차단한다.
        if any(type(outcome_flag) is not bool for outcome_flag in outcome_flags):
            raise TypeError("force-sell outcome flags must be bool values")


TradingEventPayload: TypeAlias = (
    BuyAttemptPayload
    | BuyRiskBlockedPayload
    | SellAttemptPayload
    | ForceSellOutcomePayload
)


@dataclass(frozen=True, slots=True)
class TradingEvent:
    """
    클래스 이름: TradingEvent
    기능: TradingSTM의 한 microstep에 입력할 순서 정보와 payload를 불변 값으로 보존한다.
    작성 날짜: 2026/08/14
    """

    event_type: TradingEventType
    occurred_at: datetime
    sequence_number: int = 0
    priority: EventPriority = EventPriority.MARKET
    event_id: str | None = None
    lower_event_id: str | None = None
    candle_id: str | None = None
    order_id: str | None = None
    payload: TradingEventPayload | None = None
    market_evaluation: object | None = None
    market_version: int | None = None

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: event 필드의 타입, timezone 및 sequence 범위를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        # enum과 기본 값 타입을 먼저 확인해 잘못된 event가 queue에 들어가지 않게 한다.
        if not isinstance(self.event_type, TradingEventType):
            raise TypeError("event_type must be TradingEventType")
        if not isinstance(self.occurred_at, datetime):
            raise TypeError("occurred_at must be datetime")
        if not isinstance(self.priority, EventPriority):
            raise TypeError("priority must be EventPriority")
        if not isinstance(self.sequence_number, int):
            raise TypeError("sequence_number must be int")

        # 기록·비교 시 지역 시간 혼동이 없도록 timezone-aware 시각만 허용한다.
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        if self.sequence_number < 0:
            raise ValueError("sequence_number cannot be negative")

        # Public market event는 immutable 평가와 그 source version을 항상 함께 보존한다.
        has_market_evaluation = self.market_evaluation is not None
        has_market_version = self.market_version is not None
        if has_market_evaluation != has_market_version:
            raise ValueError(
                "market_evaluation and market_version must be provided together"
            )
        if self.market_version is not None:
            if type(self.market_version) is not int:
                raise TypeError("market_version must be an exact int")
            if self.market_version <= 0:
                raise ValueError("market_version must be positive")
            if self.priority is not EventPriority.MARKET:
                raise ValueError(
                    "market_evaluation is allowed only on MARKET priority events"
                )

    @classmethod
    def create(
        cls,
        event_type: TradingEventType,
        *,
        occurred_at: datetime | None = None,
        priority: EventPriority = EventPriority.MARKET,
        **kwargs: object,
    ) -> "TradingEvent":
        """
        함수 이름: create()
        기능: 외부 adapter가 UTC 기본 시각을 가진 TradingEvent를 생성하도록 돕는다.
        인자: event_type -> 생성할 event 유형
            occurred_at -> event 발생 시각, 생략하면 현재 UTC 시각
            priority -> event queue 우선순위
            kwargs -> event 식별자와 typed payload 등 선택 필드
        반환값: 생성된 불변 TradingEvent
        작성 날짜: 2026/08/14
        """
        timestamp = occurred_at or datetime.now(timezone.utc)
        return cls(
            event_type=event_type,
            occurred_at=timestamp,
            priority=priority,
            **kwargs,
        )

    def with_queue_identity(self, sequence_number: int) -> "TradingEvent":
        """
        함수 이름: with_queue_identity()
        기능: 원본 event를 변경하지 않고 queue 순번과 안정적인 event ID를 부여한다.
        인자: sequence_number -> queue가 할당한 전역 FIFO 순번
        반환값: queue 식별자가 반영된 새 TradingEvent
        작성 날짜: 2026/08/14
        """
        event_id = self.event_id or f"{self.event_type}:{sequence_number}"
        return replace(self, sequence_number=sequence_number, event_id=event_id)
