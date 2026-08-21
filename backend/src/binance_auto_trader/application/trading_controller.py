"""TradingContext와 거래 세션의 선택, 시작, 중지 및 event 처리를 조정한다."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from threading import RLock
from uuid import uuid4

from binance_auto_trader.adapters.binance.api_gateway import APIGateway
from binance_auto_trader.adapters.binance.websocket_gateway import (
    Subscription,
    WebSocketGateway,
)
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.market import MarketSnapshot
from binance_auto_trader.domain.trading.account import (
    Account,
    SUPPORTED_VALUATION_ASSET,
)
from binance_auto_trader.domain.trading.action_requests import (
    CancelScheduledEvaluation,
    CloseLowerEvent,
    ForceSellAll,
    OpenLowerEvent,
    PatchRuntimeContext,
    QueueEvent,
    ReevaluationTrigger,
    ResetCaseBContext,
    ResetCaseCContext,
    ScheduleReevaluation,
    StopTradingRuntime,
    TradingActionRequest,
)
from binance_auto_trader.domain.trading.context import (
    ContextVersionConflictError,
    PendingOrderSnapshot,
    PositionSnapshot,
    TradingContext,
    TradingContextView,
)
from binance_auto_trader.domain.trading.event_queue import (
    RunToCompletionEventProcessor,
    SerialEventQueue,
)
from binance_auto_trader.domain.trading.events import (
    EventPriority,
    ForceSellOutcomePayload,
    TradingEvent,
    TradingEventType,
)
from binance_auto_trader.domain.trading.logic_registry import (
    TradingLogicConfiguration,
    TradingLogicSupportStatus,
    get_trading_logic_configuration,
)
from binance_auto_trader.domain.trading.results import TradingSTMResult
from binance_auto_trader.domain.trading.states import (
    RootState,
    StrategyType,
    TradingPhase,
)
from binance_auto_trader.domain.trading.stm import TradingSTM


# 멱등 기록 한도와 외부 source별 허용 event를 module 수준의 불변 정책으로 고정한다.
_MAX_COMMAND_RECORDS = 1_024
_PUBLIC_MARKET_EVENT_TYPES = frozenset(
    {
        TradingEventType.MARKET_DATA_UPDATED,
        TradingEventType.LOWER_BAND_TOUCHED,
        TradingEventType.NEW_30M_LOWER_BAND_TOUCHED,
        TradingEventType.UPPER_BAND_TOUCHED,
        TradingEventType.THIRTY_MINUTE_CANDLE_CLOSED,
    }
)
_PUBLIC_ORDER_OUTCOME_TYPES = frozenset(
    {
        TradingEventType.CASE_B_POSITION_OPENED,
        TradingEventType.CASE_B_BUY_FAILED,
        TradingEventType.CASE_B_SELL_FILLED,
        TradingEventType.CASE_B_SELL_FAILED,
        TradingEventType.CASE_C_POSITION_OPENED,
        TradingEventType.CASE_C_BUY_FAILED,
        TradingEventType.CASE_C_SELL_FILLED,
        TradingEventType.CASE_C_SELL_FAILED,
    }
)


def _utc_now() -> datetime:
    """
    함수 이름: _utc_now()
    기능: 거래 세션 event와 scheduler에 사용할 현재 UTC 시각을 반환한다.
    인자: 없음
    반환값: timezone-aware UTC datetime
    작성 날짜: 2026/08/21
    """
    return datetime.now(timezone.utc)


class TradingSessionStatus(str, Enum):
    """
    클래스 이름: TradingSessionStatus
    기능: TradingController가 공개하는 거래 세션 lifecycle 상태를 정의한다.
    작성 날짜: 2026/08/21
    """

    NOT_STARTED = "not_started"
    RUNNING = "running"
    STOPPING = "stopping"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    TERMINATED = "terminated"


class TradingSessionFailureCode(str, Enum):
    """
    클래스 이름: TradingSessionFailureCode
    기능: 선택, 시작, 중지와 버전 경계의 fail-closed 사유를 정의한다.
    작성 날짜: 2026/08/21
    """

    ACCOUNT_NOT_READY = "ACCOUNT_NOT_READY"
    COMMAND_DISABLED = "COMMAND_DISABLED"
    COMMAND_ID_REUSED = "COMMAND_ID_REUSED"
    CONNECTION_NOT_READY = "CONNECTION_NOT_READY"
    MARKET_NOT_READY = "MARKET_NOT_READY"
    INVALID_SESSION_STATE = "INVALID_SESSION_STATE"
    NO_SELECTED_REGIME = "NO_SELECTED_REGIME"
    POSITION_RECONCILIATION_REQUIRED = "POSITION_RECONCILIATION_REQUIRED"
    STALE_CONTEXT_VERSION = "STALE_CONTEXT_VERSION"
    TRADING_ACTIVE = "TRADING_ACTIVE"
    TRADING_ALREADY_ACTIVE = "TRADING_ALREADY_ACTIVE"
    TRADING_NOT_STARTED = "TRADING_NOT_STARTED"
    UNSUPPORTED_TRADING_LOGIC = "UNSUPPORTED_TRADING_LOGIC"


class TradingSessionError(RuntimeError):
    """
    클래스 이름: TradingSessionError
    기능: transport가 안정적으로 mapping할 typed 세션 실패와 version을 전달한다.
    작성 날짜: 2026/08/21
    """

    def __init__(
        self,
        code: TradingSessionFailureCode,
        message: str,
        *,
        current_version: int | None = None,
        expected_version: int | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: typed failure code, 안전한 설명과 선택적인 version 정보를 보존한다.
        인자: code -> 정규화된 세션 실패 code
            message -> 호출자에게 공개할 안전한 설명
            current_version -> 실패 시 authoritative context version 또는 None
            expected_version -> 요청이 제시한 context version 또는 None
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 외부 route가 임의 code·빈 문구를 typed application 오류로 위장하지 못하게 한다.
        if not isinstance(code, TradingSessionFailureCode):
            raise TypeError("code must be a TradingSessionFailureCode")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")

        # 표준 예외 문구와 transport mapping metadata를 하나의 failure 객체에 보존한다.
        super().__init__(message)
        self.code = code
        self.failure_code = code  # application과 transport가 같은 enum identity를 공유한다.
        self.current_version = current_version
        self.expected_version = expected_version


@dataclass(frozen=True, slots=True)
class TradingLogicSelectionResult:
    """
    클래스 이름: TradingLogicSelectionResult
    기능: 선택된 REGIME, 구현 상태와 새 context version을 불변 보존한다.
    작성 날짜: 2026/08/21
    """

    selected: RegimeType
    support_status: TradingLogicSupportStatus
    version: int


@dataclass(frozen=True, slots=True)
class SplitRatioResult:
    """
    클래스 이름: SplitRatioResult
    기능: 적용된 분할 매수·매도 비율과 context version을 불변 보존한다.
    작성 날짜: 2026/08/21
    """

    scale_in: Decimal
    scale_out: Decimal
    version: int


@dataclass(frozen=True, slots=True)
class TradingSessionResult:
    """
    클래스 이름: TradingSessionResult
    기능: start·stop 결과의 상태, 식별자, version과 STM trace를 보존한다.
    작성 날짜: 2026/08/21
    """

    status: TradingSessionStatus
    session_id: str | None
    version: int
    transition_ids: tuple[str, ...] = ()
    action_requests: tuple[TradingActionRequest, ...] = ()


@dataclass(frozen=True, slots=True)
class TradingSessionSnapshot:
    """
    클래스 이름: TradingSessionSnapshot
    기능: transport publication에 필요한 세션과 Context 값을 한 lock에서 묶는다.
    작성 날짜: 2026/08/21
    """

    status: TradingSessionStatus
    session_id: str | None
    version: int
    scale_in: Decimal
    scale_out: Decimal
    has_open_position: bool
    command_enabled: bool
    selected: RegimeType | None
    support_status: TradingLogicSupportStatus | None


@dataclass(frozen=True, slots=True)
class _CommandRecord:
    """
    클래스 이름: _CommandRecord
    기능: command ID의 fingerprint와 최초 성공 결과를 불변으로 보존한다.
    작성 날짜: 2026/08/21
    """

    fingerprint: tuple[object, ...]
    result: object


@dataclass(frozen=True, slots=True)
class _ScheduledEvaluation:
    """
    클래스 이름: _ScheduledEvaluation
    기능: 재평가 action과 가장 빠른 허용 시각을 하나의 항목으로 묶는다.
    작성 날짜: 2026/08/21
    """

    action: ScheduleReevaluation
    due_at: datetime


class _EventDrivenScheduler:
    """
    클래스 이름: _EventDrivenScheduler
    기능: busy loop 없이 명시적 market·candle·deadline·backoff trigger만 event로 만든다.
    작성 날짜: 2026/08/21
    """

    def __init__(self, clock: Callable[[], datetime]) -> None:
        """
        함수 이름: __init__()
        기능: UTC clock과 빈 one-shot 재평가 목록을 초기화한다.
        인자: clock -> 현재 평가 시각을 제공할 callable
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # scheduler는 background task 없이 주입 clock과 pending one-shot 목록만 소유한다.
        self._clock = clock
        self._scheduled: list[_ScheduledEvaluation] = []

    def schedule(self, action: ScheduleReevaluation) -> None:
        """
        함수 이름: schedule()
        기능: action을 즉시 실행하지 않고 외부 trigger 대기 항목으로 등록한다.
        인자: action -> STM이 생성한 ScheduleReevaluation
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # STM 외 객체와 잘못된 시간 설정이 scheduler 목록을 오염시키기 전에 거부한다.
        if not isinstance(action, ScheduleReevaluation):
            raise TypeError("action must be a ScheduleReevaluation")

        # earliest delay가 있는 retry만 현재 UTC 시각에서 due 시각을 뒤로 미룬다.
        due_at = self._clock()
        if action.earliest_delay is not None:
            due_at += action.earliest_delay

        # 같은 event와 scope의 요청은 최신 시간 제약 하나로 대체한다.
        self._scheduled = [
            item
            for item in self._scheduled
            if not (
                item.action.event_type is action.event_type
                and item.action.trigger is action.trigger
                and item.action.lower_event_id == action.lower_event_id
            )
        ]
        self._scheduled.append(
            _ScheduledEvaluation(action, due_at)
        )  # 등록만 하며 callback이나 반복 loop를 시작하지 않는다.

    def cancel(self, scope: str) -> None:
        """
        함수 이름: cancel()
        기능: 지정한 lower event 또는 전체 거래 세션의 재평가 요청을 취소한다.
        인자: scope -> lower-event, trading-strategy 또는 trading-session
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 빈 scope가 전역 취소로 해석되지 않도록 scheduler mutation 전에 거부한다.
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError("scope must be a non-empty string")

        # lower event 취소는 session 전역 항목만 보존하고 session 취소는 모두 비운다.
        if scope == "lower-event":
            self._scheduled = [
                item
                for item in self._scheduled
                if item.action.lower_event_id is None
            ]
            return

        self._scheduled.clear()  # 중지 뒤 timer가 새 event를 만들지 못하게 한다.

    def release(
        self,
        trigger: ReevaluationTrigger,
        occurred_at: datetime | None = None,
    ) -> tuple[TradingEvent, ...]:
        """
        함수 이름: release()
        기능: 실제 trigger와 시간 제약을 만족한 one-shot 항목만 event로 변환한다.
        인자: trigger -> 관측된 market, candle, deadline 또는 backoff 유형
            occurred_at -> event 발생 시각 또는 None
        반환값: queue에 삽입할 TradingEvent tuple
        작성 날짜: 2026/08/21
        """
        # trigger enum과 timezone을 먼저 검증해 일부 schedule만 소비되는 실패를 막는다.
        if not isinstance(trigger, ReevaluationTrigger):
            raise TypeError("trigger must be a ReevaluationTrigger")
        selected_time = occurred_at or self._clock()
        if selected_time.tzinfo is None or selected_time.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")

        # due event와 아직 기다릴 항목을 분리해 release 뒤 목록을 원자 교체한다.
        events: list[TradingEvent] = []
        remaining: list[_ScheduledEvaluation] = []

        # 주입된 trigger 한 번에 due인 one-shot 항목만 소비한다.
        for item in self._scheduled:
            action = item.action
            trigger_matches = action.trigger is trigger
            if (
                action.trigger is ReevaluationTrigger.DEADLINE_OR_MARKET_CHANGE
                and trigger is ReevaluationTrigger.MARKET_CHANGE
            ):
                trigger_matches = True
            if not trigger_matches or selected_time < item.due_at:
                remaining.append(item)
                continue

            events.append(
                TradingEvent(
                    event_type=action.event_type,
                    occurred_at=selected_time,
                    priority=EventPriority.MARKET,
                    lower_event_id=action.lower_event_id,
                )
            )

        self._scheduled = remaining  # release한 one-shot 항목만 pending 목록에서 제거한다.
        return tuple(events)

    def clear(self) -> None:
        """
        함수 이름: clear()
        기능: 종료된 세션의 모든 재평가 요청을 멱등 정리한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self._scheduled.clear()  # 종료된 session을 후속 trigger가 깨우지 못한다.

    def __len__(self) -> int:
        """
        함수 이름: __len__()
        기능: 현재 대기 중인 재평가 요청 수를 반환한다.
        인자: 없음
        반환값: scheduler 대기 항목 수
        작성 날짜: 2026/08/21
        """
        return len(self._scheduled)


class TradingController:
    """
    클래스 이름: TradingController
    기능: 계좌 startup, TradingContext 단일 writer와 거래 세션 lifecycle을 조정한다.
    작성 날짜: 2026/08/21
    """

    def __init__(
        self,
        api_gateway: APIGateway,
        web_socket_gateway: WebSocketGateway,
        account: Account,
        market_snapshot: MarketSnapshot,
        *,
        command_gate: bool | Callable[[], bool] = False,
        context: TradingContext | None = None,
        position_snapshot: PositionSnapshot | None = None,
        clock: Callable[[], datetime] | None = None,
        application_lock: RLock | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: Gateway, authoritative entity, Context와 fail-closed command gate를 조립한다.
        인자: api_gateway -> Spot 계좌 REST snapshot을 조회할 Gateway
            web_socket_gateway -> account stream을 시작하고 readiness를 제공할 Gateway
            account -> REST와 stream 결과를 보존할 동일 수명의 Account
            market_snapshot -> ETH valuation 가격을 제공할 MarketSnapshot
            command_gate -> fake mode에서만 True인 bool 또는 callable
            context -> 주입할 mutable TradingContext 또는 None
            position_snapshot -> 시작 전에 reconciliation된 포지션
            clock -> event와 scheduler가 공유할 UTC clock 또는 None
            application_lock -> transport publication과 공유할 application RLock 또는 None
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 주입 dependency와 선택적 설정을 상태 작성 전에 모두 검증한다.
        if not isinstance(account, Account):
            raise TypeError("account must be an Account")
        if not isinstance(market_snapshot, MarketSnapshot):
            raise TypeError("market_snapshot must be a MarketSnapshot")
        if not isinstance(command_gate, bool) and not callable(command_gate):
            raise TypeError("command_gate must be a bool or callable")
        if context is not None and not isinstance(context, TradingContext):
            raise TypeError("context must be a TradingContext or None")
        if position_snapshot is not None and not isinstance(
            position_snapshot,
            PositionSnapshot,
        ):
            raise TypeError("position_snapshot must be a PositionSnapshot or None")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        if application_lock is not None and not hasattr(
            application_lock,
            "__enter__",
        ):
            raise TypeError("application_lock must be a context manager or None")

        # account 경계, single-writer Context와 command gate identity를 그대로 보존한다.
        self._api_gateway = api_gateway
        self._web_socket_gateway = web_socket_gateway
        self._account = account
        self._market_snapshot = market_snapshot
        self._command_gate = command_gate
        self._clock = clock or _utc_now
        self._context = context or TradingContext(clock=self._clock)
        self._position_snapshot = position_snapshot or PositionSnapshot()
        # account 수명 자원과 session 수명 자원은 서로 다른 cleanup 목록으로 관리한다.
        self._account_subscription: Subscription | None = None
        self._session_subscriptions: list[Subscription] = []

        # Account load와 session command는 transport publication과 같은 RLock을 공유한다.
        shared_application_lock = application_lock or RLock()
        self._account_load_lock = shared_application_lock
        self._session_lock = shared_application_lock
        # 선택 준비 STM과 실행 중 STM을 분리해 active hot-swap을 구조적으로 막는다.
        self._selected_regime: RegimeType | None = None
        self._selected_stm: TradingSTM | None = None
        self._active_stm: TradingSTM | None = None
        self._session_id: str | None = None
        self._status = TradingSessionStatus.NOT_STARTED
        # session event runtime과 scheduler는 start 성공 뒤에만 활성화된다.
        self._event_queue: SerialEventQueue | None = None
        self._event_processor: RunToCompletionEventProcessor | None = None
        self._scheduler = _EventDrivenScheduler(self._clock)
        # ordered Action 증거, 미실행 외부 요청과 bounded command replay를 별도 보존한다.
        self._action_trace: list[TradingActionRequest] = []
        self._external_actions: list[TradingActionRequest] = []
        self._cleanup_failures: list[Exception] = []
        self._cleanup_in_progress = False
        self._command_records: dict[tuple[str, str], _CommandRecord] = {}
        self._command_order: deque[tuple[str, str]] = deque()

    @property
    def account(self) -> Account:
        """
        함수 이름: account()
        기능: Controller가 초기화하고 stream callback이 갱신하는 Account를 반환한다.
        인자: 없음
        반환값: 동일 수명의 Account
        작성 날짜: 2026/08/21
        """
        return self._account

    @property
    def account_subscription(self) -> Subscription | None:
        """
        함수 이름: account_subscription()
        기능: 성공한 최신 account stream 구독 handle을 반환한다.
        인자: 없음
        반환값: 최신 구독 handle 또는 load 전 None
        작성 날짜: 2026/08/21
        """
        return self._account_subscription

    @property
    def context(self) -> TradingContextView:
        """
        함수 이름: context()
        기능: 세션의 authoritative TradingContext를 외부 mutation 없는 불변 view로 반환한다.
        인자: 없음
        반환값: Controller lock 안에서 만든 TradingContextView
        작성 날짜: 2026/08/21
        """
        with self._session_lock:
            return self._context.snapshot()  # mutable Context writer는 Controller 내부로 제한한다.

    @property
    def selected_regime(self) -> RegimeType | None:
        """
        함수 이름: selected_regime()
        기능: RegimeController가 commit한 현재 거래 REGIME을 반환한다.
        인자: 없음
        반환값: 선택된 REGIME 또는 None
        작성 날짜: 2026/08/21
        """
        with self._session_lock:
            return self._selected_regime

    @property
    def selected_configuration(self) -> TradingLogicConfiguration | None:
        """
        함수 이름: selected_configuration()
        기능: transport가 domain import 없이 읽을 수 있는 선택 REGIME 구성을 반환한다.
        인자: 없음
        반환값: 선택된 TradingLogicConfiguration 또는 None
        작성 날짜: 2026/08/21
        """
        with self._session_lock:
            if self._selected_regime is None:
                return None

            return get_trading_logic_configuration(self._selected_regime)

    @property
    def status(self) -> TradingSessionStatus:
        """
        함수 이름: status()
        기능: 현재 거래 세션 lifecycle 상태를 반환한다.
        인자: 없음
        반환값: 현재 TradingSessionStatus
        작성 날짜: 2026/08/21
        """
        with self._session_lock:
            return self._status

    @property
    def session_id(self) -> str | None:
        """
        함수 이름: session_id()
        기능: 현재 또는 마지막 거래 세션 식별자를 반환한다.
        인자: 없음
        반환값: 세션 ID 또는 시작 전 None
        작성 날짜: 2026/08/21
        """
        with self._session_lock:
            return self._session_id

    @property
    def command_enabled(self) -> bool:
        """
        함수 이름: command_enabled()
        기능: bootstrap이 주입한 fake mode gate를 fail closed로 평가한다.
        인자: 없음
        반환값: 거래 command가 허용되면 True
        작성 날짜: 2026/08/21
        """
        # 주입 callable의 예외와 truthy 비-bool 결과를 모두 명시적 비활성으로 처리한다.
        gate = self._command_gate
        try:
            enabled = gate() if callable(gate) else gate
        except Exception:
            return False  # gate 실패를 명령 허용으로 fallback하지 않는다.

        return enabled is True

    @property
    def action_trace(self) -> tuple[TradingActionRequest, ...]:
        """
        함수 이름: action_trace()
        기능: STM action을 Controller가 수행한 순서대로 반환한다.
        인자: 없음
        반환값: 순서가 보존된 action tuple
        작성 날짜: 2026/08/21
        """
        with self._session_lock:
            return tuple(self._action_trace)

    @property
    def external_action_requests(self) -> tuple[TradingActionRequest, ...]:
        """
        함수 이름: external_action_requests()
        기능: Phase 8 adapter가 아직 실행하지 않은 주문·reconciliation 요청을 반환한다.
        인자: 없음
        반환값: 외부 부수 효과 action tuple
        작성 날짜: 2026/08/21
        """
        with self._session_lock:
            return tuple(self._external_actions)

    @property
    def pending_schedule_count(self) -> int:
        """
        함수 이름: pending_schedule_count()
        기능: 세션 scheduler에 남은 재평가 요청 수를 반환한다.
        인자: 없음
        반환값: 대기 중인 scheduler 항목 수
        작성 날짜: 2026/08/21
        """
        with self._session_lock:
            return len(self._scheduler)

    @property
    def cleanup_failures(self) -> tuple[Exception, ...]:
        """
        함수 이름: cleanup_failures()
        기능: 종료 자원 close 중 기록된 진단용 실패를 불변 tuple로 반환한다.
        인자: 없음
        반환값: session cleanup에서 발생한 예외 tuple
        작성 날짜: 2026/08/21
        """
        with self._session_lock:
            return tuple(self._cleanup_failures)  # 호출자가 내부 진단 목록을 바꾸지 못하게 한다.

    def snapshot_session(self) -> TradingSessionSnapshot:
        """
        함수 이름: snapshot_session()
        기능: transport publication에 필요한 lifecycle과 Context 값을 원자 조회한다.
        인자: 없음
        반환값: 일관된 TradingSessionSnapshot
        작성 날짜: 2026/08/21
        """
        with self._session_lock:
            # 선택이 있을 때만 canonical registry에서 지원 상태를 같은 lock으로 읽는다.
            configuration = None
            if self._selected_regime is not None:
                configuration = get_trading_logic_configuration(
                    self._selected_regime
                )

            # mutable Context의 각 값을 session lock 아래 같은 publication에 묶는다.
            return TradingSessionSnapshot(
                status=self._status,
                session_id=self._session_id,
                version=self._context.version,
                scale_in=self._context.scale_in_ratio,
                scale_out=self._context.scale_out_ratio,
                has_open_position=self._context.position.is_open,
                command_enabled=self.command_enabled,
                selected=self._selected_regime,
                support_status=(
                    None
                    if configuration is None
                    else configuration.support_status
                ),
            )

    def fetch_selected_trading_logic(
        self,
        regime_type: RegimeType,
    ) -> TradingSTM:
        """
        함수 이름: fetch_selected_trading_logic()
        기능: fallback 없이 선택한 canonical REGIME의 새 TradingSTM을 생성한다.
        인자: regime_type -> UI가 명시적으로 선택한 canonical REGIME
        반환값: 지원된 거래 로직의 세션 전용 TradingSTM
        작성 날짜: 2026/08/21
        """
        # 선택 소유자는 RegimeController이므로 factory는 별도 mutation을 하지 않는다.
        return TradingSTM.get_stm_instance(regime_type)  # 미지원 오류를 그대로 전달한다.

    def commit_regime_selection(
        self,
        regime_type: RegimeType,
        selected_stm: TradingSTM | None,
        *,
        command_id: str,
        expected_version: int,
    ) -> TradingLogicSelectionResult:
        """
        함수 이름: commit_regime_selection()
        기능: RegimeController가 결정한 선택을 active·version·멱등 검증 후 commit한다.
        인자: regime_type -> 새로 선택할 canonical REGIME
            selected_stm -> 지원 REGIME의 factory 결과 또는 미지원이면 None
            command_id -> transport command의 멱등 식별자
            expected_version -> 호출자가 관측한 context version
        반환값: 선택, 지원 상태와 새 version을 담은 결과
        작성 날짜: 2026/08/21
        """
        # factory 결과가 요청 REGIME과 정확히 일치하는지 cache 조회 전에 검증한다.
        if not isinstance(regime_type, RegimeType):
            raise TypeError("regime_type must be a RegimeType")
        if selected_stm is not None and not isinstance(selected_stm, TradingSTM):
            raise TypeError("selected_stm must be a TradingSTM or None")
        if selected_stm is not None and selected_stm.regime_type is not regime_type:
            raise ValueError("selected_stm must match regime_type")
        self._validate_expected_version_value(expected_version)
        configuration = get_trading_logic_configuration(regime_type)
        fingerprint = (regime_type, expected_version)

        with self._session_lock:
            # exact duplicate만 먼저 replay하고 active·stale 새 mutation은 그 다음 거부한다.
            cached = self._read_command_record(
                "selection",
                command_id,
                fingerprint,
            )
            if cached is not None:
                return self._require_selection_result(cached)
            if self._is_active_locked():
                raise TradingSessionError(
                    TradingSessionFailureCode.TRADING_ACTIVE,
                    "REGIME selection cannot change during an active session",
                    current_version=self._context.version,
                    expected_version=expected_version,
                )
            self._require_expected_version(expected_version)

            # 미지원 선택도 보존하되 executable STM만 None으로 유지한다.
            self._context.select_regime(regime_type)
            self._selected_regime = regime_type
            self._selected_stm = selected_stm
            result = TradingLogicSelectionResult(
                selected=regime_type,
                support_status=configuration.support_status,
                version=self._context.version,
            )
            self._store_command_record(
                "selection",
                command_id,
                fingerprint,
                result,
            )  # 성공 mutation 뒤에만 멱등 결과를 저장한다.
            return result

    def update_split_ratios(
        self,
        *,
        command_id: str,
        expected_version: int,
        scale_in: Decimal,
        scale_out: Decimal,
    ) -> SplitRatioResult:
        """
        함수 이름: update_split_ratios()
        기능: expected version이 일치할 때 분할 매수·매도 비율을 원자 변경한다.
        인자: command_id -> transport command의 멱등 식별자
            expected_version -> 호출자가 관측한 context version
            scale_in -> BUY pending에 사용할 Decimal 비율
            scale_out -> SELL pending에 사용할 Decimal 비율
        반환값: 적용된 두 비율과 새 context version
        작성 날짜: 2026/08/21
        """
        # bool version과 float ratio가 command cache equality를 우회하지 못하게 선검증한다.
        self._validate_expected_version_value(expected_version)
        if not isinstance(scale_in, Decimal) or not isinstance(
            scale_out,
            Decimal,
        ):
            raise TypeError("split ratios must use Decimal")
        fingerprint = (
            expected_version,
            scale_in.as_tuple(),
            scale_out.as_tuple(),
        )  # Decimal 부호와 scale까지 보존해 signed zero 재사용도 같다고 보지 않는다.

        with self._session_lock:
            # duplicate replay와 optimistic version 검증을 실제 Context mutation보다 앞세운다.
            cached = self._read_command_record(
                "split",
                command_id,
                fingerprint,
            )
            if cached is not None:
                return self._require_split_result(cached)
            self._require_expected_version(expected_version)

            # Context domain method가 Decimal 형식과 범위를 단일 검증한다.
            self._context.set_split_ratios(scale_in, scale_out)
            result = SplitRatioResult(
                scale_in=self._context.scale_in_ratio,
                scale_out=self._context.scale_out_ratio,
                version=self._context.version,
            )
            self._store_command_record(
                "split",
                command_id,
                fingerprint,
                result,
            )  # duplicate command에는 동일 결과를 반환한다.
            return result

    def start_trading(
        self,
        *,
        command_id: str,
        expected_version: int,
    ) -> TradingSessionResult:
        """
        함수 이름: start_trading()
        기능: 선택·계좌·포지션·연결 조건을 검증하고 Context 초기화 뒤 STM을 한 번 시작한다.
        인자: command_id -> transport command의 멱등 식별자
            expected_version -> 호출자가 관측한 context version
        반환값: RUNNING 세션과 G-01 trace를 담은 결과
        작성 날짜: 2026/08/21
        """
        # request version 형식과 fingerprint를 lock·cache 진입 전에 고정한다.
        self._validate_expected_version_value(expected_version)
        fingerprint = (expected_version,)

        with self._session_lock:
            # duplicate start는 전제조건이나 STM을 다시 실행하지 않고 최초 결과를 반환한다.
            cached = self._read_command_record(
                "start",
                command_id,
                fingerprint,
            )
            if cached is not None:
                return self._require_session_result(cached)
            self._require_expected_version(expected_version)
            self._validate_start_prerequisites()

            selected_regime = self._selected_regime
            selected_stm = self._selected_stm
            if selected_regime is None or selected_stm is None:
                raise RuntimeError("supported selection must provide a TradingSTM")

            # start publication 전 예외가 Context와 이전 lifecycle을 반쪽 상태로 남기지 않게 보존한다.
            context_checkpoint = self._context._create_checkpoint()
            previous_session_id = self._session_id
            previous_active_stm = self._active_stm
            previous_status = self._status
            previous_event_queue = self._event_queue
            previous_event_processor = self._event_processor
            previous_action_trace = tuple(self._action_trace)
            previous_external_actions = tuple(self._external_actions)
            previous_cleanup_failures = tuple(self._cleanup_failures)
            previous_cleanup_state = self._cleanup_in_progress

            try:
                # 모든 Guard를 통과한 뒤 Context와 새 session-owned 자원을 초기화한다.
                self._context.initialize(
                    self._account,
                    selected_regime,
                    self._position_snapshot,
                    self._context.scale_in_ratio,
                    self._context.scale_out_ratio,
                )
                self._session_id = str(uuid4())  # transport와 UI가 검증하는 canonical UUID를 사용한다.
                self._active_stm = selected_stm
                self._selected_stm = None
                self._event_queue = SerialEventQueue()
                self._event_processor = RunToCompletionEventProcessor(
                    stm=selected_stm,
                    context_provider=self._context.snapshot,
                    action_executor=self._execute_action,
                    event_queue=self._event_queue,
                    clock=self._clock,
                )
                self._scheduler.clear()
                self._action_trace.clear()
                self._external_actions.clear()
                self._cleanup_failures.clear()
                self._cleanup_in_progress = False

                # initialize된 동일 Context snapshot으로 run을 정확히 한 번 호출한다.
                start_result = selected_stm.run(self._context.snapshot())
                self._apply_stm_result(start_result)
            except Exception:
                # G-01 publish 전 실패는 Context와 Controller의 이전 공개 상태를 모두 복원한다.
                self._context._restore_checkpoint(context_checkpoint)
                self._session_id = previous_session_id
                self._active_stm = previous_active_stm
                self._selected_stm = TradingSTM.get_stm_instance(
                    selected_regime
                )  # 실행 도중 변경됐을 수 있는 session STM은 새 instance로 폐기한다.
                self._event_queue = previous_event_queue
                self._event_processor = previous_event_processor
                self._scheduler.clear()
                self._action_trace = list(previous_action_trace)
                self._external_actions = list(previous_external_actions)
                self._cleanup_failures = list(previous_cleanup_failures)
                self._cleanup_in_progress = previous_cleanup_state
                self._status = previous_status
                raise

            # G-01 Action 반영이 끝난 뒤에만 RUNNING 결과와 멱등 receipt를 publish한다.
            self._status = TradingSessionStatus.RUNNING
            result = self._create_session_result(start_result)
            self._store_command_record(
                "start",
                command_id,
                fingerprint,
                result,
            )  # run 성공 뒤에만 duplicate start 결과를 고정한다.
            return result

    def stop_trading(
        self,
        *,
        command_id: str,
        expected_version: int,
    ) -> TradingSessionResult:
        """
        함수 이름: stop_trading()
        기능: STOP_CONFIRMED를 먼저 전달하고 위치·pending에 맞는 action을 순서대로 수행한다.
        인자: command_id -> transport command의 멱등 식별자
            expected_version -> 호출자가 관측한 context version
        반환값: TERMINATED, STOPPING 또는 reconciliation 상태 결과
        작성 날짜: 2026/08/21
        """
        # bool version을 포함한 잘못된 public 호출은 cached 성공을 읽기 전에 차단한다.
        self._validate_expected_version_value(expected_version)
        fingerprint = (expected_version,)

        with self._session_lock:
            # exact replay를 먼저 반환하고 새 stop만 version·session 상태를 검증한다.
            cached = self._read_command_record(
                "stop",
                command_id,
                fingerprint,
            )
            if cached is not None:
                return self._require_session_result(cached)
            self._require_expected_version(expected_version)
            active_stm = self._active_stm
            if active_stm is None or self._session_id is None:
                raise TradingSessionError(
                    TradingSessionFailureCode.TRADING_NOT_STARTED,
                    "Trading session has not been started",
                    current_version=self._context.version,
                    expected_version=expected_version,
                )

            # 종료된 세션의 재중지는 새 action 없는 성공 no-op으로 기록한다.
            if self._status is TradingSessionStatus.TERMINATED:
                result = self._create_session_result(None)
                self._store_command_record(
                    "stop",
                    command_id,
                    fingerprint,
                    result,
                )
                return result

            # 이미 중지 중이면 terminal outcome queue를 건드리지 않고 현재 진행만 반환한다.
            if self._status in (
                TradingSessionStatus.STOPPING,
                TradingSessionStatus.RECONCILIATION_REQUIRED,
            ):
                result = self._create_session_result(None)
                self._store_command_record(
                    "stop",
                    command_id,
                    fingerprint,
                    result,
                )  # 다른 command ID도 새 force-sell intent 없이 idempotent 진행 조회가 된다.
                return result

            # session과 command를 결합한 내부 STOP_CONFIRMED event를 정확히 한 번 만든다.
            stop_event = TradingEvent(
                event_type=TradingEventType.STOP_CONFIRMED,
                occurred_at=self._clock(),
                priority=EventPriority.USER_COMMAND,
                event_id=f"stop-{self._session_id}-{command_id}",
            )

            # D05에 따라 cleanup이나 force-sell 기록 전에 STM.handle을 먼저 호출한다.
            stop_result = active_stm.handle(
                stop_event,
                self._context.snapshot(),
            )
            self._apply_stm_result(stop_result)
            self._scheduler.clear()  # 모든 stop branch에서 신규 timer event를 즉시 차단한다.
            if self._event_queue is not None:
                self._event_queue.clear()  # STOP 이후 대기 중인 시장·timer event를 모두 폐기한다.
            self._synchronize_status_from_context(active_stm)
            result = self._create_session_result(stop_result)
            self._store_command_record(
                "stop",
                command_id,
                fingerprint,
                result,
            )  # 같은 command 재시도는 ForceSellAll을 중복 생성하지 않는다.
            return result

    def enqueue_event(self, event: TradingEvent) -> TradingEvent | None:
        """
        함수 이름: enqueue_event()
        기능: RUNNING event와 STOPPING의 exact force-sell outcome만 직렬 queue에 추가한다.
        인자: event -> adapter가 정규화한 TradingEvent
        반환값: queue identity가 부여된 event 또는 duplicate이면 None
        작성 날짜: 2026/08/21
        """
        # public adapter가 domain event 외 객체로 lifecycle Guard를 우회하지 못하게 한다.
        if not isinstance(event, TradingEvent):
            raise TypeError("event must be a TradingEvent")

        with self._session_lock:
            # terminal cleanup가 시작된 뒤에는 callback 재진입을 무조건 no-op으로 만든다.
            if self._cleanup_in_progress:
                return None  # terminal cleanup callback은 새 event intake를 다시 열 수 없다.

            # public intake는 lifecycle/internal command를 제외한 source별 exact 조합만 받는다.
            is_public_market_event = (
                event.priority is EventPriority.MARKET
                and event.event_type in _PUBLIC_MARKET_EVENT_TYPES
            )
            is_public_order_outcome = (
                event.priority is EventPriority.ORDER_OUTCOME
                and event.event_type in _PUBLIC_ORDER_OUTCOME_TYPES
            )
            if self._status is TradingSessionStatus.RUNNING and (
                not isinstance(event.event_id, str)
                or not event.event_id.strip()
                or not (is_public_market_event or is_public_order_outcome)
            ):
                return None

            # 종료 결과는 stable ID·typed payload·실제 force-sell intent를 모두 요구한다.
            is_force_sell_outcome = event.event_type in (
                TradingEventType.FORCE_SELL_FINISHED,
                TradingEventType.FORCE_SELL_FAILED,
            )
            has_force_sell_intent = any(
                isinstance(action, ForceSellAll)
                for action in self._external_actions
            )
            if is_force_sell_outcome and (
                not isinstance(event.payload, ForceSellOutcomePayload)
                or not isinstance(event.event_id, str)
                or not event.event_id.strip()
                or self._context.runtime.pending_order_id is not None
                or not has_force_sell_intent
                or (
                    event.event_type is TradingEventType.FORCE_SELL_FINISHED
                    and self._context.position.is_open
                )
                or (
                    event.event_type is TradingEventType.FORCE_SELL_FAILED
                    and not self._context.position.is_open
                )
            ):
                return None

            # 현재 lifecycle에 따라 일반 running source와 exact stop outcome을 구분한다.
            accepts_running_event = (
                self._status is TradingSessionStatus.RUNNING
            )
            accepts_stop_outcome = (
                self._status
                in (
                    TradingSessionStatus.STOPPING,
                    TradingSessionStatus.RECONCILIATION_REQUIRED,
                )
                and event.priority is EventPriority.ORDER_OUTCOME
                and event.event_type
                in (
                    TradingEventType.FORCE_SELL_FINISHED,
                    TradingEventType.FORCE_SELL_FAILED,
                )
            )
            if not accepts_running_event and not accepts_stop_outcome:
                return None  # stop 이후 시장 event는 막고 terminal 주문 결과만 허용한다.
            if self._event_queue is None:
                raise RuntimeError("running session requires an event queue")

            return self._event_queue.enqueue(event)

    async def process_next_event(self) -> TradingSTMResult | None:
        """
        함수 이름: process_next_event()
        기능: queue의 한 event와 action batch를 run-to-completion으로 처리한다.
        인자: 없음
        반환값: 처리한 TradingSTMResult 또는 queue가 비었으면 None
        작성 날짜: 2026/08/21
        """
        with self._session_lock:
            # cleanup·비활성 상태는 processor 참조를 사용하기 전에 빠르게 종료한다.
            processor = self._event_processor
            if self._cleanup_in_progress:
                return None
            if self._status not in (
                TradingSessionStatus.RUNNING,
                TradingSessionStatus.STOPPING,
                TradingSessionStatus.RECONCILIATION_REQUIRED,
            ):
                return None
            if processor is None:
                raise RuntimeError("running session requires an event processor")

            # direct stop과 event microstep이 같은 STM·Context를 동시에 commit하지 못하게 한다.
            result = await processor.process_next()
            if result is not None:
                active_stm = self._active_stm
                if active_stm is not None:
                    self._synchronize_status_from_context(active_stm)

            return result

    async def drain_events(
        self,
        *,
        max_microsteps: int = 10_000,
    ) -> tuple[TradingSTMResult, ...]:
        """
        함수 이름: drain_events()
        기능: 현재 queue의 event 연쇄를 유한 한도 내에서 모두 처리한다.
        인자: max_microsteps -> 한 drain에서 허용할 최대 event 수
        반환값: 처리 순서가 보존된 TradingSTMResult tuple
        작성 날짜: 2026/08/21
        """
        with self._session_lock:
            # drain도 cleanup·비활성 session에서는 queue를 건드리지 않는 no-op이다.
            processor = self._event_processor
            if self._cleanup_in_progress:
                return ()
            if self._status not in (
                TradingSessionStatus.RUNNING,
                TradingSessionStatus.STOPPING,
                TradingSessionStatus.RECONCILIATION_REQUIRED,
            ):
                return ()
            if processor is None:
                raise RuntimeError("running session requires an event processor")

            # drain 전체도 stop command와 하나의 serialized session operation으로 취급한다.
            results = await processor.drain(max_microsteps=max_microsteps)
            active_stm = self._active_stm
            if active_stm is not None:
                self._synchronize_status_from_context(active_stm)

            return tuple(results)

    def trigger_scheduled_evaluations(
        self,
        trigger: ReevaluationTrigger,
        *,
        occurred_at: datetime | None = None,
    ) -> tuple[TradingEvent, ...]:
        """
        함수 이름: trigger_scheduled_evaluations()
        기능: 관측된 candle·market·deadline·backoff trigger의 due event만 queue에 넣는다.
        인자: trigger -> 실제로 관측된 ReevaluationTrigger
            occurred_at -> 관측 시각 또는 None
        반환값: 이번 trigger가 queue에 추가한 event tuple
        작성 날짜: 2026/08/21
        """
        with self._session_lock:
            # scheduler release는 RUNNING이며 intake가 열린 session에서만 허용한다.
            if (
                self._cleanup_in_progress
                or self._status is not TradingSessionStatus.RUNNING
            ):
                return ()
            if self._event_queue is None:
                raise RuntimeError("running session requires an event queue")

            # due schedule을 꺼낸 순서대로 dedup queue에 넣고 실제 수락 event만 반환한다.
            released_events = self._scheduler.release(trigger, occurred_at)
            enqueued_events: list[TradingEvent] = []
            for released_event in released_events:
                enqueued_event = self._event_queue.enqueue(released_event)
                if enqueued_event is not None:
                    enqueued_events.append(enqueued_event)

            return tuple(enqueued_events)

    def register_session_subscription(
        self,
        subscription: Subscription,
    ) -> None:
        """
        함수 이름: register_session_subscription()
        기능: StopTradingRuntime에서 정리할 세션 전용 구독 handle을 등록한다.
        인자: subscription -> close() 계약을 갖는 세션 구독
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # opaque handle도 close callable이 없으면 session 자원으로 등록하지 않는다.
        if not callable(getattr(subscription, "close", None)):
            raise TypeError("subscription must provide close()")

        # 등록 순간에도 RUNNING인지 확인해 cleanup와 동시에 새 handle이 유출되지 않게 한다.
        with self._session_lock:
            if (
                self._cleanup_in_progress
                or self._status is not TradingSessionStatus.RUNNING
            ):
                raise TradingSessionError(
                    TradingSessionFailureCode.TRADING_NOT_STARTED,
                    "Session subscriptions require a running session",
                    current_version=self._context.version,
                )
            self._session_subscriptions.append(subscription)

    def close_session_resources(self) -> None:
        """
        함수 이름: close_session_resources()
        기능: application 종료 시 거래 상태를 추론하지 않고 event intake와 세션 구독만 정리한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        with self._session_lock:
            self._cleanup_session_resources()  # 포지션을 매도하거나 STM 종료로 위장하지 않는다.

    def update_position_snapshot(
        self,
        position_snapshot: PositionSnapshot,
        *,
        owner: StrategyType | None = None,
    ) -> None:
        """
        함수 이름: update_position_snapshot()
        기능: 체결 reconciliation의 authoritative 포지션을 세션 Context에 적용한다.
        인자: position_snapshot -> 체결 반영이 완료된 포지션 snapshot
            owner -> 포지션 소유 전략 enum 또는 None
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # Position snapshot과 owner 조합을 Context version mutation 전에 완전히 검증한다.
        if not isinstance(position_snapshot, PositionSnapshot):
            raise TypeError("position_snapshot must be a PositionSnapshot")
        if owner is not None and not isinstance(owner, StrategyType):
            raise TypeError("owner must be a StrategyType or None")
        if not position_snapshot.is_open and owner is not None:
            raise ValueError("A zero position cannot have a strategy owner")

        # Controller가 보존하는 start prerequisite와 실행 중 Context를 같은 lock에서 갱신한다.
        with self._session_lock:
            self._position_snapshot = position_snapshot
            if self._context.initialized:
                self._context.update_position(
                    position_snapshot,
                    position_owner=owner,
                )  # Context method가 owner 타입과 수량 조합을 검증한다.

    def update_pending_order_snapshot(
        self,
        pending_order: PendingOrderSnapshot | None,
    ) -> None:
        """
        함수 이름: update_pending_order_snapshot()
        기능: 주문 실행·reconciliation의 authoritative pending 상태를 Context에 적용한다.
        인자: pending_order -> 최신 PendingOrderSnapshot 또는 terminal이면 None
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # pending 입력 타입은 initialized session 검증보다 먼저 fail closed한다.
        if pending_order is not None and not isinstance(
            pending_order,
            PendingOrderSnapshot,
        ):
            raise TypeError(
                "pending_order must be a PendingOrderSnapshot or None"
            )

        # initialized Guard와 pending snapshot publication을 같은 session mutation으로 묶는다.
        with self._session_lock:
            if not self._context.initialized:
                raise TradingSessionError(
                    TradingSessionFailureCode.TRADING_NOT_STARTED,
                    "Pending orders require an initialized trading session",
                    current_version=self._context.version,
                )
            self._context.update_pending_order(
                pending_order
            )  # snapshot과 runtime pending 식별자를 한 version으로 맞춘다.

    def load_account(
        self,
        asset: str = SUPPORTED_VALUATION_ASSET,
    ) -> Account:
        """
        함수 이름: load_account()
        기능: REST 전체 계좌를 가격과 적용한 뒤 변경 stream을 정확한 순서로 시작한다.
        인자: asset -> ETHUSDT 상품에서 평가할 기준 asset
        반환값: 주입 시 받은 것과 동일한 초기화된 Account
        작성 날짜: 2026/08/21
        """
        # 상품 범위 밖 asset은 REST나 WebSocket 부수 효과 전에 canonical 검증한다.
        normalized_asset = self._normalize_asset(asset)

        # REST full snapshot 성공 뒤에만 stream을 열어 delta 순서를 보존한다.
        with self._account_load_lock:
            account_snapshot = self._api_gateway.fetch_account_snapshot(
                normalized_asset
            )
            current_price = self._market_snapshot.get_current_eth_price()
            self._account.apply_initial_snapshot(account_snapshot, current_price)
            self._account_subscription = None
            account_subscription = (
                self._web_socket_gateway.start_account_info_stream()
            )
            self._account_subscription = account_subscription

        return self._account

    def _validate_start_prerequisites(self) -> None:
        """
        함수 이름: _validate_start_prerequisites()
        기능: Context mutation 전에 mode, 선택, 계좌, 시장, 연결과 reconciliation을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # lifecycle·mode·registry 준비 조건을 Context mutation보다 먼저 확인한다.
        if self._is_active_locked():
            raise TradingSessionError(
                TradingSessionFailureCode.TRADING_ALREADY_ACTIVE,
                "Trading session is already active",
                current_version=self._context.version,
            )
        if not self.command_enabled:
            raise TradingSessionError(
                TradingSessionFailureCode.COMMAND_DISABLED,
                "Trading commands are disabled outside fake mode",
                current_version=self._context.version,
            )
        if self._selected_regime is None:
            raise TradingSessionError(
                TradingSessionFailureCode.NO_SELECTED_REGIME,
                "A REGIME must be selected before trading starts",
                current_version=self._context.version,
            )

        # 미지원 선택과 이전 session에서 이미 소비한 지원 선택을 서로 다른 Guard로 구분한다.
        selected_configuration = get_trading_logic_configuration(
            self._selected_regime
        )
        if (
            selected_configuration.support_status
            is TradingLogicSupportStatus.UNSUPPORTED
        ):
            raise TradingSessionError(
                TradingSessionFailureCode.UNSUPPORTED_TRADING_LOGIC,
                "The selected REGIME does not provide a trading logic",
                current_version=self._context.version,
            )
        if self._selected_stm is None:
            raise TradingSessionError(
                TradingSessionFailureCode.NO_SELECTED_REGIME,
                "A REGIME must be selected again before a new session starts",
                current_version=self._context.version,
            )

        # 시장·계좌·실제 account subscription이 모두 같은 startup 세대로 준비돼야 한다.
        if not self._market_snapshot.ready:
            raise TradingSessionError(
                TradingSessionFailureCode.MARKET_NOT_READY,
                "MarketSnapshot must be ready before trading starts",
                current_version=self._context.version,
            )
        if not self._account.ready:
            raise TradingSessionError(
                TradingSessionFailureCode.ACCOUNT_NOT_READY,
                "Account must be ready before trading starts",
                current_version=self._context.version,
            )
        if not self._web_socket_gateway.account_connected:
            raise TradingSessionError(
                TradingSessionFailureCode.CONNECTION_NOT_READY,
                "Account stream must be connected before trading starts",
                current_version=self._context.version,
            )

        # 설명되지 않은 Position이나 pending 주문은 새 session으로 덮지 않고 reconciliation을 요구한다.
        if self._position_snapshot.is_open:
            raise TradingSessionError(
                TradingSessionFailureCode.POSITION_RECONCILIATION_REQUIRED,
                "A reconciled open position blocks a new trading session",
                current_version=self._context.version,
            )
        if self._context.initialized and self._context.pending_order is not None:
            raise TradingSessionError(
                TradingSessionFailureCode.POSITION_RECONCILIATION_REQUIRED,
                "A pending order must be reconciled before trading starts",
                current_version=self._context.version,
            )

    def _apply_stm_result(self, result: TradingSTMResult) -> None:
        """
        함수 이름: _apply_stm_result()
        기능: STM action batch를 원본 순서대로 Context, queue, scheduler와 외부 요청에 분배한다.
        인자: result -> 한 STM microstep의 결정과 action batch
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # typed STM 결과만 받아 임의 action collection의 실행을 차단한다.
        if not isinstance(result, TradingSTMResult):
            raise TypeError("result must be a TradingSTMResult")

        # 어떤 Action도 실행하기 전에 STM이 판정한 원본 Context version을 검증한다.
        if result.context_version != self._context.version:
            raise ContextVersionConflictError(
                "TradingSTM result context version is stale"
            )

        # patch도 원래 위치에서 적용해 Context mutation과 외부 요청의 순서를 보존한다.
        for action in result.action_requests:
            if isinstance(action, PatchRuntimeContext):
                patch_result = replace(
                    result,
                    action_requests=(action,),
                    context_version=self._context.version,
                )
                self._context.apply_trading_stm_result(
                    patch_result
                )  # Context는 이 위치의 runtime patch만 실행하고 다른 Action은 모른다.
                self._action_trace.append(action)
                continue
            if isinstance(action, QueueEvent):
                self._action_trace.append(action)
                self._enqueue_internal_action(action)
                continue

            self._execute_action(action)

    def _execute_action(
        self,
        action: TradingActionRequest,
    ) -> None:
        """
        함수 이름: _execute_action()
        기능: typed action 하나를 Context method, scheduler, cleanup 또는 Phase 8 요청으로 분배한다.
        인자: action -> STM이 생성한 하나의 TradingActionRequest
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self._action_trace.append(action)  # 부수 효과 전에 요청 순서를 먼저 고정한다.

        # Context mutation은 임의 setattr 대신 domain typed method로만 적용한다.
        if isinstance(action, PatchRuntimeContext):
            self._context.apply_runtime_patch(action)
            return
        if isinstance(action, OpenLowerEvent):
            self._context.open_lower_event(action)
            return
        if isinstance(action, CloseLowerEvent):
            self._context.close_lower_event(action)
            return
        if isinstance(action, ResetCaseBContext):
            self._context.reset_case_b_context(action)
            return
        if isinstance(action, ResetCaseCContext):
            self._context.reset_case_c_context(action)
            return
        if isinstance(action, ScheduleReevaluation):
            self._scheduler.schedule(action)
            return
        if isinstance(action, CancelScheduledEvaluation):
            self._scheduler.cancel(action.scope)
            return
        if isinstance(action, StopTradingRuntime):
            self._cleanup_session_resources()
            return
        if isinstance(action, QueueEvent):
            return  # processor가 action batch 뒤에 enqueue하고 Controller는 trace만 소유한다.

        # Phase 8 전에는 주문·취소·reconcile·force-sell을 API로 실행하지 않는다.
        self._external_actions.append(action)

    def _enqueue_internal_action(self, action: QueueEvent) -> None:
        """
        함수 이름: _enqueue_internal_action()
        기능: direct microstep의 QueueEvent를 INTERNAL event로 등록한다.
        인자: action -> STM이 생성한 QueueEvent
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # start가 만든 session queue가 없으면 내부 microstep을 다른 세션에 넘기지 않는다.
        if self._event_queue is None:
            raise RuntimeError("QueueEvent requires an initialized event queue")

        # 현재 lower-event identity를 보충해 후속 event를 동일 serial queue에 등록한다.
        context_snapshot = self._context.snapshot()
        self._event_queue.enqueue(
            TradingEvent(
                event_type=action.event_type,
                occurred_at=self._clock(),
                priority=EventPriority.INTERNAL,
                lower_event_id=(
                    action.lower_event_id
                    or context_snapshot.runtime.lower_event_id
                ),
                candle_id=action.candle_id,
                order_id=action.order_id,
                payload=action.payload,
            ),
            internal=True,
        )  # 후속 microstep은 재귀 호출하지 않고 queue로만 이어간다.

    def _cleanup_session_resources(self) -> None:
        """
        함수 이름: _cleanup_session_resources()
        기능: 종료된 세션의 scheduler와 전용 구독을 멱등 정리한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # close callback이 Controller에 재진입해도 같은 자원을 두 번 정리하지 않는다.
        if self._cleanup_in_progress:
            return

        # untrusted close callback의 재진입보다 먼저 intake와 신규 등록 Guard를 닫는다.
        self._cleanup_in_progress = True
        self._scheduler.clear()
        event_queue = self._event_queue
        self._event_queue = None
        if event_queue is not None:
            event_queue.clear()
        self._event_processor = None  # terminal session processor 참조도 함께 해제한다.
        subscriptions = tuple(self._session_subscriptions)
        self._session_subscriptions.clear()

        # 한 close 실패가 나머지 session 자원 정리와 terminal 상태 publish를 막지 않게 한다.
        for subscription in subscriptions:
            try:
                subscription.close()
            except Exception as error:
                self._cleanup_failures.append(
                    error
                )  # 실패를 숨기지 않되 이미 commit된 STM 종료는 반쪽 상태로 되돌리지 않는다.

    def _synchronize_status_from_context(self, stm: TradingSTM) -> None:
        """
        함수 이름: _synchronize_status_from_context()
        기능: STM root와 Context phase에서 공개 session status를 결정한다.
        인자: stm -> 현재 세션을 소유한 TradingSTM
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        root_state = stm.current_state.root_state
        runtime = self._context.runtime

        # reconciliation은 일반 STOPPING보다 구체적인 운영 상태로 먼저 공개한다.
        if runtime.trading_phase is TradingPhase.RECONCILIATION_REQUIRED:
            self._status = TradingSessionStatus.RECONCILIATION_REQUIRED
        elif root_state is RootState.LOGIC_TERMINATED:
            self._status = TradingSessionStatus.TERMINATED
            self._selected_stm = None  # 다음 start는 정상 stop 뒤 새 REGIME 선택을 반드시 요구한다.
        elif root_state is RootState.STOPPING:
            if runtime.pending_order_id is not None:
                self._status = TradingSessionStatus.RECONCILIATION_REQUIRED
            else:
                self._status = TradingSessionStatus.STOPPING
        else:
            self._status = TradingSessionStatus.RUNNING

    def _create_session_result(
        self,
        stm_result: TradingSTMResult | None,
    ) -> TradingSessionResult:
        """
        함수 이름: _create_session_result()
        기능: 현재 lifecycle과 선택 STM trace를 transport-safe 결과로 변환한다.
        인자: stm_result -> 반영된 STM 결과 또는 no-op이면 None
        반환값: 현재 TradingSessionResult
        작성 날짜: 2026/08/21
        """
        # no-op과 실제 STM 결과를 같은 immutable receipt shape로 정규화한다.
        transition_ids = () if stm_result is None else stm_result.transition_ids
        action_requests = () if stm_result is None else stm_result.action_requests
        return TradingSessionResult(
            status=self._status,
            session_id=self._session_id,
            version=self._context.version,
            transition_ids=transition_ids,
            action_requests=action_requests,
        )

    def _is_active_locked(self) -> bool:
        """
        함수 이름: _is_active_locked()
        기능: 선택 변경을 차단할 running·stopping·reconciliation 상태인지 판정한다.
        인자: 없음
        반환값: active 세션이면 True
        작성 날짜: 2026/08/21
        """
        return self._status in (
            TradingSessionStatus.RUNNING,
            TradingSessionStatus.STOPPING,
            TradingSessionStatus.RECONCILIATION_REQUIRED,
        )

    def _require_expected_version(self, expected_version: int) -> None:
        """
        함수 이름: _require_expected_version()
        기능: command expected version이 authoritative context version과 일치하는지 검증한다.
        인자: expected_version -> 호출자가 관측한 context version
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 형식을 먼저 확정한 뒤 authoritative Context와 optimistic version을 비교한다.
        self._validate_expected_version_value(expected_version)
        if expected_version != self._context.version:
            raise TradingSessionError(
                TradingSessionFailureCode.STALE_CONTEXT_VERSION,
                "Expected context version does not match authoritative version",
                current_version=self._context.version,
                expected_version=expected_version,
            )

    @staticmethod
    def _validate_expected_version_value(expected_version: int) -> None:
        """
        함수 이름: _validate_expected_version_value()
        기능: cache 비교 전에 expected version의 정수 형식과 범위를 검증한다.
        인자: expected_version -> 호출자가 전달한 optimistic version
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # wire bool을 int로 인정하지 않고 0 이상의 exact 정수만 optimistic version으로 받는다.
        if isinstance(expected_version, bool) or not isinstance(
            expected_version,
            int,
        ):
            raise TypeError("expected_version must be an integer")
        if expected_version < 0:
            raise ValueError("expected_version must not be negative")

    def _read_command_record(
        self,
        operation: str,
        command_id: str,
        fingerprint: tuple[object, ...],
    ) -> object | None:
        """
        함수 이름: _read_command_record()
        기능: 멱등 command 결과를 찾고 다른 payload의 ID 재사용을 거부한다.
        인자: operation -> selection, split, start 또는 stop 이름
            command_id -> transport command 식별자
            fingerprint -> mutation의 불변 입력 tuple
        반환값: 저장된 결과 또는 처음 본 command이면 None
        작성 날짜: 2026/08/21
        """
        # operation namespace와 canonical command ID로 최초 성공 기록을 조회한다.
        normalized_command_id = self._normalize_command_id(command_id)
        record = self._command_records.get((operation, normalized_command_id))
        if record is None:
            return None
        if record.fingerprint != fingerprint:
            raise TradingSessionError(
                TradingSessionFailureCode.COMMAND_ID_REUSED,
                "Command ID cannot be reused with a different payload",
                current_version=self._context.version,
            )

        return record.result  # duplicate에는 Context나 STM을 다시 변경하지 않는다.

    def _store_command_record(
        self,
        operation: str,
        command_id: str,
        fingerprint: tuple[object, ...],
        result: object,
    ) -> None:
        """
        함수 이름: _store_command_record()
        기능: 성공 mutation의 fingerprint와 결과를 command ID에 결합한다.
        인자: operation -> mutation 종류 이름
            command_id -> transport command 식별자
            fingerprint -> mutation의 불변 입력 tuple
            result -> duplicate에 반환할 최초 성공 결과
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 성공 결과만 operation별 key에 저장하고 insertion order를 eviction 기준으로 보존한다.
        normalized_command_id = self._normalize_command_id(command_id)
        command_key = (operation, normalized_command_id)
        self._command_records[command_key] = _CommandRecord(
            fingerprint,
            result,
        )  # operation별 namespace로 start와 stop ID를 분리한다.
        self._command_order.append(command_key)

        # adversarial unique command가 process lifetime memory를 무제한 늘리지 못하게 한다.
        while len(self._command_order) > _MAX_COMMAND_RECORDS:
            expired_key = self._command_order.popleft()
            self._command_records.pop(expired_key, None)

    @staticmethod
    def _normalize_command_id(command_id: str) -> str:
        """
        함수 이름: _normalize_command_id()
        기능: command ID가 공백 없는 문자열인지 검증한다.
        인자: command_id -> transport command 식별자
        반환값: 검증된 command ID
        작성 날짜: 2026/08/21
        """
        # 공백 정규화로 서로 다른 요청 ID가 암묵적으로 합쳐지지 않게 exact 값을 요구한다.
        if not isinstance(command_id, str):
            raise TypeError("command_id must be a string")
        if not command_id or command_id != command_id.strip():
            raise ValueError("command_id must be a non-empty trimmed string")

        return command_id

    @staticmethod
    def _require_selection_result(
        result: object,
    ) -> TradingLogicSelectionResult:
        """
        함수 이름: _require_selection_result()
        기능: command cache의 선택 결과 타입을 검증해 반환한다.
        인자: result -> command cache에서 읽은 값
        반환값: 검증된 TradingLogicSelectionResult
        작성 날짜: 2026/08/21
        """
        # Cache 값의 exact result 타입을 확인한 뒤 검증된 객체만 호출자에게 반환한다.
        if not isinstance(result, TradingLogicSelectionResult):
            raise RuntimeError("selection command cache type mismatch")

        return result

    @staticmethod
    def _require_split_result(result: object) -> SplitRatioResult:
        """
        함수 이름: _require_split_result()
        기능: command cache의 분할 비율 결과 타입을 검증해 반환한다.
        인자: result -> command cache에서 읽은 값
        반환값: 검증된 SplitRatioResult
        작성 날짜: 2026/08/21
        """
        # Cache 값의 exact result 타입을 확인한 뒤 검증된 객체만 호출자에게 반환한다.
        if not isinstance(result, SplitRatioResult):
            raise RuntimeError("split command cache type mismatch")

        return result

    @staticmethod
    def _require_session_result(result: object) -> TradingSessionResult:
        """
        함수 이름: _require_session_result()
        기능: command cache의 세션 결과 타입을 검증해 반환한다.
        인자: result -> command cache에서 읽은 값
        반환값: 검증된 TradingSessionResult
        작성 날짜: 2026/08/21
        """
        # Cache 값의 exact result 타입을 확인한 뒤 검증된 객체만 호출자에게 반환한다.
        if not isinstance(result, TradingSessionResult):
            raise RuntimeError("session command cache type mismatch")

        return result

    def _normalize_asset(self, asset: object) -> str:
        """
        함수 이름: _normalize_asset()
        기능: Account valuation 입력을 canonical ETH로 정규화하고 제한한다.
        인자: asset -> 호출자가 전달한 기준 asset
        반환값: canonical ETH 문자열
        작성 날짜: 2026/08/21
        """
        # 공백·대소문자만 canonicalize하고 다른 상품으로 fallback하지 않는다.
        if not isinstance(asset, str):
            raise TypeError("asset must be a string")

        normalized_asset = asset.strip().upper()
        if normalized_asset != SUPPORTED_VALUATION_ASSET:
            raise ValueError("TradingController account load supports only ETH")

        return normalized_asset
