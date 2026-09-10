"""TradingContext와 거래 세션의 선택, 시작, 중지 및 event 처리를 조정한다."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN, localcontext
from enum import Enum
import hashlib
import sys
from threading import RLock
from time import sleep
from uuid import uuid4
from zoneinfo import ZoneInfo

from binance_auto_trader.adapters.binance.api_gateway import (
    APP_CLIENT_ORDER_ID_PREFIX,
    APIGateway,
)
from binance_auto_trader.adapters.binance.websocket_gateway import (
    Subscription,
    WebSocketGateway,
)
from binance_auto_trader.application.trade_history_controller import (
    TradeHistoryController,
)
from binance_auto_trader.application.residual_settlement import ResidualSettlement
from binance_auto_trader.application.runtime_diagnostics import RuntimeDiagnostics
from binance_auto_trader.application.trading_diagnostics import describe_trading_evaluation, normalize_order_failure, normalize_stream_reason
from binance_auto_trader.application.trading_indicator_snapshot import TradingIndicatorStore
from binance_auto_trader.application.market_evaluation_builder import calculate_execution_pct_b
from binance_auto_trader.application.trading_logic_snapshot import (
    TradingLogicSnapshot,
    create_trading_logic_snapshot,
)
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.history import Trade
from binance_auto_trader.domain.market import MarketSnapshot
from binance_auto_trader.domain.trading.account import (
    Account,
    SUPPORTED_VALUATION_ASSET,
)
from binance_auto_trader.domain.trading.action_requests import (
    CancelPendingOrder,
    CancelScheduledEvaluation,
    CloseLowerEvent,
    ForceSellAll,
    OpenLowerEvent,
    PatchRuntimeContext,
    QueueEvent,
    ReevaluationTrigger,
    ResetCaseBContext,
    ResetCaseCContext,
    ReconcileOrder,
    RuntimeField,
    ScheduleReevaluation,
    StopTradingRuntime,
    SubmitOrder,
    TradingActionRequest,
    patch,
)
from binance_auto_trader.domain.trading.context import (
    ContextVersionConflictError,
    MarketEvaluationSnapshot,
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
    BuyAttemptPayload,
    BuyRiskBlockedPayload,
    EventPriority,
    ForceSellOutcomePayload,
    SellAttemptPayload,
    TradingEvent,
    TradingEventType,
)
from binance_auto_trader.domain.trading.logic_registry import (
    TradingLogicConfiguration,
    TradingLogicSupportStatus,
    get_trading_logic_configuration,
)
from binance_auto_trader.domain.trading.order import (
    ACTIVE_ORDER_STATUSES,
    ExecutionSummary,
    Fill,
    Order,
    OrderResult,
    OrderResultFailureKind,
    OrderStatus,
    PendingOrderRecoveryLifecycle,
    PendingOrderSubmissionProvenance,
    TERMINAL_ORDER_STATUSES,
)
from binance_auto_trader.domain.trading.position import (
    LegacyFeeAccountingMigrationRequiredError,
    Position,
    PositionStateSnapshot,
)
from binance_auto_trader.domain.trading.results import TradingSTMResult
from binance_auto_trader.domain.trading.risk import (
    DailyLossScope,
    ManualKillBehavior,
    ManualKillControlState,
    RiskBlockReason,
    RiskBudgetSnapshot,
    RiskDecision,
    RiskPolicy,
    RiskPolicyAvailability,
    RiskPolicyUnavailable,
    evaluate_buy_risk,
)
from binance_auto_trader.domain.trading.states import (
    ExitReason,
    OrderAttemptKind,
    OrderSide,
    RootState,
    StrategyType,
    TradingPhase,
)
from binance_auto_trader.domain.trading.stm import TradingSTM


# 멱등 기록 한도와 외부 source별 허용 event를 module 수준의 불변 정책으로 고정한다.
_MAX_COMMAND_RECORDS = 1_024
_MAX_MANUAL_KILL_COMMAND_RECORDS = 1_024
_MAX_PUBLIC_MARKET_BOUNDARY_TRACE_ENTRIES = 4_096
_BASE_ASSET = "ETH"
_QUOTE_ASSET = "USDT"
_TRADING_SYMBOL = "ETHUSDT"
_KOREA_TIME_ZONE = ZoneInfo("Asia/Seoul")
_MAX_SUBMISSIONS_PER_INTENT = 5
_ORDER_RECONCILIATION_DELAYS = (
    timedelta(seconds=1),
    timedelta(seconds=2),
    timedelta(seconds=4),
    timedelta(seconds=8),
)
_FORCE_SELL_RETRY_DELAY = timedelta(seconds=3)
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


def _order_result_exactly_confirms_trade(
    result: OrderResult,
    trade: Trade,
) -> bool:
    """
    함수 이름: _order_result_exactly_confirms_trade()
    기능: 거래소 결과가 durable Trade의 복합 identity와 terminal 체결 집계를 정확히 재현하는지 확인한다.
    인자: result -> open/recent/same-ID REST에서 정규화한 주문 결과
        trade -> 비교할 durable terminal Trade
    반환값: 복합 identity, terminal 상태와 전체 체결 집계가 모두 같으면 True
    작성 날짜: 2026/08/23
    """
    if not _order_result_accounting_confirms_trade(result, trade):
        return False

    executed_at = max(
        fill_value.executed_at for fill_value in result.fills
    )  # Startup/query exact 검증은 durable Trade가 선택한 마지막 체결 시각도 유지한다.

    return executed_at == trade.executed_at


def _order_result_accounting_confirms_trade(
    result: OrderResult,
    trade: Trade,
) -> bool:
    """
    함수 이름: _order_result_accounting_confirms_trade()
    기능: 서로 다른 Binance transport 시각 표현을 제외한 durable terminal 회계 사실을 대조한다.
    인자: result -> REST FULL 또는 누적 executionReport의 canonical OrderResult
        trade -> 이미 fsync된 terminal Trade
    반환값: 복합 identity, 수량·금액·평균가·수수료가 모두 같으면 True
    작성 날짜: 2026/09/05
    """
    if not isinstance(result, OrderResult) or not isinstance(trade, Trade):
        raise TypeError("result and trade must use canonical domain types")

    # Testnet reset의 숫자 ID 재사용과 terminal이 아닌 stream update를 먼저 배제한다.
    if (
        result.client_order_id != trade.client_order_id
        or result.exchange_order_id != trade.order_id
        or result.symbol != trade.symbol
        or result.status not in TERMINAL_ORDER_STATUSES
        or not result.fills
    ):
        return False

    # v3는 aggregate만 같아도 다른 원 BNB·가격 근거이면 동일 체결로 취급하지 않는다.
    if trade.schema_version == 3:
        return {fill.key: fill for fill in result.fills} == {fill.key: fill for fill in trade.fee_fills}

    # Durable schema의 단일 fee asset과 같은 Decimal128 정책으로 누적 fill을 다시 집계한다.
    fee_assets = frozenset(fill_value.fee_asset for fill_value in result.fills)
    if len(fee_assets) != 1:
        return False
    with localcontext() as decimal_context:
        decimal_context.prec = 34
        decimal_context.rounding = ROUND_HALF_EVEN
        executed_quantity = sum(
            (fill_value.quantity for fill_value in result.fills),
            start=Decimal("0"),
        )
        executed_amount = sum(
            (fill_value.executed_amount for fill_value in result.fills),
            start=Decimal("0"),
        )
        average_fill_price = executed_amount / executed_quantity
        fee_amount = sum(
            (fill_value.fee_amount for fill_value in result.fills),
            start=Decimal("0"),
        )
        fee_quote_amount = sum(
            (fill_value.fee_quote_amount for fill_value in result.fills),
            start=Decimal("0"),
        )
    return (
        executed_quantity == trade.executed_quantity
        and executed_amount == trade.executed_amount
        and average_fill_price == trade.average_fill_price
        and fee_amount == trade.fee_amount
        and next(iter(fee_assets)) == trade.fee_asset
        and fee_quote_amount == trade.fee_quote_amount
    )  # REST FULL fallback 시각과 stream T가 달라도 동일한 재무 effect만 멱등 후보가 된다.


def _wait_for_order_retry_delay(delay: timedelta) -> None:
    """
    함수 이름: _wait_for_order_retry_delay()
    기능: 재시작 same-order 조회 전에 양수 bounded 대기 시간을 blocking 방식으로 기다린다.
    인자: delay -> jitter와 상한 적용을 마친 대기 시간
    반환값: 없음
    작성 날짜: 2026/08/23
    """
    # Startup command gate가 닫힌 동안에만 사용하며 잘못된 지연을 즉시 거부한다.
    if not isinstance(delay, timedelta) or delay <= timedelta(0):
        raise ValueError("delay must be a positive timedelta")

    sleep(delay.total_seconds())  # ADR-002 최대 8초 단위라 장시간 무제한 대기는 만들지 않는다.

# Communication Case 2의 caller/receiver를 message ID별 불변 trace 계약으로 고정한다.
_ORDER_TRACE_PARTICIPANTS = {
    "1": ("TradingController", "TradingSTM"),
    "2": ("TradingController", "TradingContext"),
    "3": ("TradingController", "TradingContext"),
    "4": ("TradingController", "MarketSnapshot"),
    "5": ("TradingController", "Order"),
    "5.1": ("TradingController", "RiskPolicy"),
    "6": ("TradingController", "APIGateway"),
    "6.1": ("APIGateway", "BinanceRESTClient"),
    "7": ("TradingController", "Order"),
    "8": ("TradingController", "APIGateway"),
    "8.1": ("APIGateway", "BinanceRESTClient"),
    "8.2": ("APIGateway", "BinanceRESTClient"),
    "9": ("TradingController", "Order"),
    "10": ("TradingController", "Order"),
    "11": ("TradingController", "Position"),
    "12": ("TradingController", "Position"),
    "13": ("TradingController", "TradeHistoryController"),
    "13.1": ("TradeHistoryController", "Performance"),
    "13.2": ("TradeHistoryController", "Trade"),
    "13.3": ("TradeHistoryController", "TradeHistory"),
    "13.4": ("TradeHistoryController", "Performance"),
    "13.5": ("TradeHistoryController", "TradeHistoryRepository"),
    "13.5.1": ("TradeHistoryRepository", "FileSystem"),
    "14": ("TradingController", "TradingSTM"),
}


def _utc_now() -> datetime:
    """
    함수 이름: _utc_now()
    기능: 거래 세션 event와 scheduler에 사용할 현재 UTC 시각을 반환한다.
    인자: 없음
    반환값: timezone-aware UTC datetime
    작성 날짜: 2026/08/21
    """
    return datetime.now(timezone.utc)


def _unit_order_retry_jitter_factor() -> Decimal:
    """
    함수 이름: _unit_order_retry_jitter_factor()
    기능: Phase 8 fake와 결정론적 테스트에서 기본 대기를 그대로 유지한다.
    인자: 없음
    반환값: ADR-002가 허용하는 경계 내 Decimal jitter factor 1.0
    작성 날짜: 2026/08/22
    """
    return Decimal("1.0")  # 실거래 adapter는 Phase 13에서 별도의 난수 provider를 주입한다.


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


class ReconciliationCauseCategory(str, Enum):
    """
    클래스 이름: ReconciliationCauseCategory
    기능: Phase 13 실패 증거에 공개할 secret 없는 재조정 원인 범주를 정의한다.
    작성 날짜: 2026/08/31
    """

    ACCOUNT_STREAM_UNKNOWN_OR_EXTERNAL_EXECUTION = (
        "ACCOUNT_STREAM_UNKNOWN_OR_EXTERNAL_EXECUTION"
    )
    PREPARE_FILTER_OR_CAP_REJECTED = "PREPARE_FILTER_OR_CAP_REJECTED"
    EVENT_WORKER_OR_RUNTIME_FAILED = "EVENT_WORKER_OR_RUNTIME_FAILED"
    MARKET_STREAM_FAILED = "MARKET_STREAM_FAILED"
    ORDER_OR_PERSISTENCE_AMBIGUOUS = "ORDER_OR_PERSISTENCE_AMBIGUOUS"
    PROCESS_OWNERSHIP_AMBIGUOUS = "PROCESS_OWNERSHIP_AMBIGUOUS"


class ReconciliationCauseStatus(str, Enum):
    """
    클래스 이름: ReconciliationCauseStatus
    기능: 단일 원인 latch의 미기록, 정확한 기록과 다중 기록 실패 상태를 정의한다.
    작성 날짜: 2026/08/31
    """

    MISSING = "MISSING"
    EXACT = "EXACT"
    DUPLICATE = "DUPLICATE"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True, slots=True)
class ReconciliationCauseSnapshot:
    """
    클래스 이름: ReconciliationCauseSnapshot
    기능: 재조정 필요 여부와 단일 원인 latch 상태를 한 lock 시점의 불변 값으로 보존한다.
    작성 날짜: 2026/08/31
    """

    reconciliation_required: bool
    status: ReconciliationCauseStatus
    category: ReconciliationCauseCategory | None

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: EXACT만 원인 범주를 공개하는 fail-closed snapshot 불변식을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        # bool subclass나 임의 enum 유사값이 production 증거 schema에 섞이지 않게 한다.
        if type(self.reconciliation_required) is not bool:
            raise TypeError("reconciliation_required must be an exact bool")
        if not isinstance(self.status, ReconciliationCauseStatus):
            raise TypeError("status must be a ReconciliationCauseStatus")
        if self.category is not None and not isinstance(
            self.category,
            ReconciliationCauseCategory,
        ):
            raise TypeError(
                "category must be a ReconciliationCauseCategory or None"
            )

        # 정확히 한 번 기록된 경우에만 범주를 노출하고 나머지는 모호성으로 닫는다.
        if self.status is ReconciliationCauseStatus.EXACT:
            if self.category is None:
                raise ValueError("EXACT status requires a category")
        elif self.category is not None:
            raise ValueError("non-EXACT status must not expose a category")


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
    STALE_RISK_CONTROL_VERSION = "STALE_RISK_CONTROL_VERSION"
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


class StartupOrderReconciliationError(RuntimeError):
    """
    클래스 이름: StartupOrderReconciliationError
    기능: 재시작 시 local history·pending journal과 Binance 사실의 불일치를 안전하게 알린다.
    작성 날짜: 2026/08/22
    """

    code = "STARTUP_ORDER_RECONCILIATION_FAILED"


class AccountStreamRecoveryBlockedError(StartupOrderReconciliationError):
    """
    클래스 이름: AccountStreamRecoveryBlockedError
    기능: 재접속 반복으로 개선되지 않는 account stream 불변식·provenance 충돌을 알린다.
    작성 날짜: 2026/08/23
    """

    code = "ACCOUNT_STREAM_RECOVERY_BLOCKED"


class _RecoveredPositionLiquidationGateClosedError(RuntimeError):
    """
    클래스 이름: _RecoveredPositionLiquidationGateClosedError
    기능: 복구 청산의 사전 Guard 뒤 주문 effect gate가 닫힌 cut-point를 나타낸다.
    작성 날짜: 2026/08/24
    """


class _RecoveredPositionLiquidationPreflightError(RuntimeError):
    """
    클래스 이름: _RecoveredPositionLiquidationPreflightError
    기능: 복구 청산의 journal·REST POST 전 주문 사전검증 실패를 나타낸다.
    작성 날짜: 2026/08/24
    """


class OrderExecutionFailureCode(str, Enum):
    """
    클래스 이름: OrderExecutionFailureCode
    기능: Phase 8 주문 pipeline이 reconciliation으로 닫히는 typed failure 사유를 정의한다.
    작성 날짜: 2026/08/22
    """

    ZERO_ORDER_QUANTITY = "ZERO_ORDER_QUANTITY"
    SUBMISSION_BUDGET_EXHAUSTED = "SUBMISSION_BUDGET_EXHAUSTED"
    QUERY_BUDGET_EXHAUSTED = "QUERY_BUDGET_EXHAUSTED"
    GATEWAY_REQUEST_FAILED = "GATEWAY_REQUEST_FAILED"
    ORDER_RESULT_INVALID = "ORDER_RESULT_INVALID"
    POSITION_UPDATE_FAILED = "POSITION_UPDATE_FAILED"
    HISTORY_PERSISTENCE_FAILED = "HISTORY_PERSISTENCE_FAILED"
    PENDING_ORDER_NOT_FOUND = "PENDING_ORDER_NOT_FOUND"
    SYMBOL_FILTER_REJECTED = "SYMBOL_FILTER_REJECTED"
    STREAM_RECONCILIATION_REQUIRED = "STREAM_RECONCILIATION_REQUIRED"
    EVENT_RUNTIME_FAILED = "EVENT_RUNTIME_FAILED"
    RISK_POLICY_UNAVAILABLE = "RISK_POLICY_UNAVAILABLE"
    RISK_POLICY_VERSION_MISMATCH = "RISK_POLICY_VERSION_MISMATCH"
    MANUAL_KILL_SWITCH_ACTIVE = "MANUAL_KILL_SWITCH_ACTIVE"
    RISK_ORDER_NOTIONAL_EXCEEDED = "RISK_ORDER_NOTIONAL_EXCEEDED"
    RISK_DAILY_LOSS_EXCEEDED = "RISK_DAILY_LOSS_EXCEEDED"
    RISK_POSITION_NOTIONAL_EXCEEDED = "RISK_POSITION_NOTIONAL_EXCEEDED"


class OrderExecutionTraceResult(str, Enum):
    """
    클래스 이름: OrderExecutionTraceResult
    기능: Case 2 trace 단계의 성공과 실패 결과를 typed 값으로 구분한다.
    작성 날짜: 2026/08/22
    """

    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


@dataclass(frozen=True, slots=True)
class OrderExecutionTraceEntry:
    """
    클래스 이름: OrderExecutionTraceEntry
    기능: Case 2 message 상관관계와 Context version 및 안전한 결과를 불변 기록한다.
    작성 날짜: 2026/08/22
    """

    message_id: str
    caller: str
    receiver: str
    command_event_id: str
    context_version_before: int
    context_version_after: int
    intent_id: str
    client_order_id: str
    order_id: str | None
    result: OrderExecutionTraceResult
    failure_code: OrderExecutionFailureCode | None = None

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: trace 식별자·version·결과와 typed failure 조합을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # 공개 trace의 모든 필수 상관 문자열은 secret 없이 공백 없는 값이어야 한다.
        required_text_values = (
            self.message_id,
            self.caller,
            self.receiver,
            self.command_event_id,
            self.intent_id,
            self.client_order_id,
        )
        if any(not isinstance(value, str) for value in required_text_values):
            raise TypeError("order trace identifiers must be strings")
        if any(not value.strip() for value in required_text_values):
            raise ValueError("order trace identifiers must not be empty")
        if self.order_id is not None and (
            not isinstance(self.order_id, str) or not self.order_id.strip()
        ):
            raise ValueError("order_id must be a non-empty string or None")

        # 두 Context version은 bool이 아닌 단조 0 이상 정수여야 한다.
        versions = (
            self.context_version_before,
            self.context_version_after,
        )
        if any(
            isinstance(version, bool) or not isinstance(version, int)
            for version in versions
        ):
            raise TypeError("order trace versions must be integers")
        if any(version < 0 for version in versions):
            raise ValueError("order trace versions must not be negative")
        if self.context_version_after < self.context_version_before:
            raise ValueError("order trace Context version must not move backward")

        # 성공에는 failure code가 없고 실패에는 반드시 typed code가 있어야 한다.
        if not isinstance(self.result, OrderExecutionTraceResult):
            raise TypeError("result must be an OrderExecutionTraceResult")
        if self.failure_code is not None and not isinstance(
            self.failure_code,
            OrderExecutionFailureCode,
        ):
            raise TypeError(
                "failure_code must be an OrderExecutionFailureCode or None"
            )
        if self.result is OrderExecutionTraceResult.SUCCESS:
            if self.failure_code is not None:
                raise ValueError("successful trace cannot contain failure_code")
        elif self.failure_code is None:
            raise ValueError("failed trace requires failure_code")


@dataclass(frozen=True, slots=True)
class PublicMarketBoundaryTraceEntry:
    """
    클래스 이름: PublicMarketBoundaryTraceEntry
    기능: production Kline 관찰, 시장 평가와 STM Action emission의 불변 provenance를 보존한다.
    작성 날짜: 2026/08/31
    """

    message_id: str
    event_type: str
    source_event_id: str
    evaluation_id: str
    market_version: int
    context_version: int
    regime: RegimeType
    action_type: str | None = None
    side: OrderSide | None = None
    strategy: StrategyType | None = None

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 1L 단계, source/version과 Action optional field 조합을 exact production contract로 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        expected_event_type = {
            "1L.1": "KLINE_OBSERVED",
            "1L.2": "MARKET_EVALUATED",
            "1L.3": "ACTION_EMITTED",
        }.get(self.message_id)
        if expected_event_type is None or self.event_type != expected_event_type:
            raise ValueError("public market boundary message and event type do not match")
        for field_name in ("source_event_id", "evaluation_id"):
            field_value = getattr(self, field_name)
            if (
                not isinstance(field_value, str)
                or not field_value
                or field_value != field_value.strip()
            ):
                raise ValueError(f"{field_name} must be non-empty canonical text")
        if self.evaluation_id != f"market:{self.market_version}:{self.source_event_id}":
            raise ValueError("evaluation_id must bind the exact source and market version")
        for field_name in ("market_version", "context_version"):
            field_value = getattr(self, field_name)
            if type(field_value) is not int or field_value < 0:
                raise ValueError(f"{field_name} must be a non-negative exact integer")
        if self.market_version < 1:
            raise ValueError("market_version must be positive")
        if not isinstance(self.regime, RegimeType):
            raise TypeError("regime must be a RegimeType")

        # Kline/evaluation에는 Action field가 없고 emission만 typed SubmitOrder identity를 가진다.
        if self.message_id in {"1L.1", "1L.2"}:
            if any(
                value is not None
                for value in (self.action_type, self.side, self.strategy)
            ):
                raise ValueError("pre-action public evidence cannot contain action fields")
            return
        if (
            self.action_type != "SUBMIT_ORDER"
            or not isinstance(self.side, OrderSide)
            or not isinstance(self.strategy, StrategyType)
        ):
            raise ValueError("ACTION_EMITTED requires a typed SubmitOrder identity")


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
class ManualKillResult:
    """
    클래스 이름: ManualKillResult
    기능: versioned manual kill command의 활성 상태, 정책 provenance와 control version을 보존한다.
    작성 날짜: 2026/08/25
    """

    active: bool
    behavior: ManualKillBehavior | None
    policy_version: int | None
    risk_control_version: int


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
    risk_policy_availability: RiskPolicyAvailability
    configured_risk_policy_version: int | None
    session_risk_policy_version: int | None
    risk_control_version: int
    manual_kill_active: bool
    manual_kill_cleanup_complete: bool
    manual_kill_activation_behavior: ManualKillBehavior | None
    manual_kill_activation_policy_version: int | None
    last_risk_decision: RiskDecision | None
    process_ownership_ambiguous: bool
    max_order_notional: Decimal | None = None
    max_position_notional: Decimal | None = None
    max_daily_loss: Decimal | None = None
    daily_loss_scope: DailyLossScope | None = None
    manual_kill_behavior: ManualKillBehavior | None = None
    residual_quantity: Decimal = Decimal("0")
    residual_cost_basis: Decimal = Decimal("0")
    position_average_entry_price: Decimal | None = None  # 열린 Position의 표시용 평단가다.
    active_logic: TradingLogicSnapshot | None = None  # 실제 실행 STM의 전략만 공개한다.


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


@dataclass(slots=True)
class _OrderExecutionState:
    """
    클래스 이름: _OrderExecutionState
    기능: 한 client order ID의 reconciliation·회계·저장 진행 상태를 Controller 내부에 묶는다.
    작성 날짜: 2026/08/22
    """

    order: Order
    force_sell: bool
    reconciliation_attempts: int = 0
    stop_after_reconciliation: bool = False
    allocated_cost_basis: Decimal = Decimal("0")
    terminal_summary: ExecutionSummary | None = None
    pending_outcome: TradingEvent | None = None
    persistence_pending: bool = False
    # Trade history 저장과 별개인 pending sidecar REMOVE durability를 독립적으로 보존한다.
    pending_recovery_pending: bool = False
    recovery_lifecycle: PendingOrderRecoveryLifecycle = (
        PendingOrderRecoveryLifecycle.PREPARED
    )
    stop_followup_started: bool = False
    awaiting_terminal_zero_confirmation: bool = False
    submission_rejection_confirmable: bool = False
    order_not_visible_observations: int = 0


@dataclass(frozen=True, slots=True)
class _ScheduledOrderQuery:
    """
    클래스 이름: _ScheduledOrderQuery
    기능: 같은 Order를 다시 조회할 가장 빠른 시각을 재귀 호출 없이 보존한다.
    작성 날짜: 2026/08/22
    """

    client_order_id: str
    due_at: datetime


@dataclass(slots=True)
class _AccountFreeOverlay:
    """
    클래스 이름: _AccountFreeOverlay
    기능: account stream 반영 전의 실제 fill이 free 잔액에 준 변화를 자산별로 보수적 보정한다.
    작성 날짜: 2026/08/22
    """

    observed_free: Decimal
    adjustment: Decimal = Decimal("0")


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
        position: Position | None = None,
        trade_history_controller: TradeHistoryController | None = None,
        residual_settlement: ResidualSettlement | None = None,
        pending_order_recovery_enabled: bool = False,
        market_stream_recovery_enabled: bool = False,
        maximum_order_notional: Decimal | None = None,
        maximum_order_submissions_per_intent: int = _MAX_SUBMISSIONS_PER_INTENT,
        risk_policy_state: RiskPolicy | RiskPolicyUnavailable | None = None,
        order_retry_jitter: Callable[[], Decimal] | None = None,
        order_retry_waiter: Callable[[timedelta], object] | None = None,
        event_runtime_notifier: Callable[[], object] | None = None,
        clock: Callable[[], datetime] | None = None,
        application_lock: RLock | None = None,
        diagnostics: RuntimeDiagnostics | None = None,
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
            position -> 실제 fill과 average cost를 소유할 Phase 8 Position 또는 None
            trade_history_controller -> terminal execution을 durable 기록할 Controller 또는 None
            residual_settlement -> live 전용 잔여 회계 서비스 또는 None
            pending_order_recovery_enabled -> testnet 제출 전 sidecar journal 활성 여부
            market_stream_recovery_enabled -> Kline live 세대와 full-resync gate 강제 여부
            maximum_order_notional -> BUY와 일반 SELL 생성 전 적용하고 force SELL은 제외할 quote 상한
            maximum_order_submissions_per_intent -> journal·client ID 생성 전 적용할 intent별 제출 상한
            risk_policy_state -> 모든 신규 BUY에 적용할 versioned 정책 또는 명시적 미설정 상태
            order_retry_jitter -> 각 일반 주문 대기에 적용할 0.8~1.2 Decimal provider 또는 None
            order_retry_waiter -> startup same-order 조회 지연을 수행할 callable 또는 None
            event_runtime_notifier -> queue 또는 due 작업이 생겼음을 알릴 non-blocking callback
            clock -> event와 scheduler가 공유할 UTC clock 또는 None
            application_lock -> transport publication과 공유할 application RLock 또는 None
            diagnostics -> bootstrap이 연결한 진단 출력 경계 또는 기록 비활성의 None
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
        if position is not None and not isinstance(position, Position):
            raise TypeError("position must be a Position or None")
        if trade_history_controller is not None and not isinstance(
            trade_history_controller,
            TradeHistoryController,
        ):
            raise TypeError(
                "trade_history_controller must be a TradeHistoryController or None"
            )
        if type(pending_order_recovery_enabled) is not bool:
            raise TypeError("pending_order_recovery_enabled must be a bool")
        if type(market_stream_recovery_enabled) is not bool:
            raise TypeError("market_stream_recovery_enabled must be a bool")
        if maximum_order_notional is not None and (
            not isinstance(maximum_order_notional, Decimal)
            or not maximum_order_notional.is_finite()
            or maximum_order_notional <= Decimal("0")
        ):
            raise ValueError(
                "maximum_order_notional must be a positive finite Decimal or None"
            )
        if isinstance(
            maximum_order_submissions_per_intent,
            bool,
        ) or not isinstance(maximum_order_submissions_per_intent, int):
            raise TypeError(
                "maximum_order_submissions_per_intent must be an integer"
            )
        if not 1 <= maximum_order_submissions_per_intent <= (
            _MAX_SUBMISSIONS_PER_INTENT
        ):
            raise ValueError(
                "maximum_order_submissions_per_intent must be an integer from 1 to 5"
            )
        selected_risk_policy_state = (
            RiskPolicyUnavailable()
            if risk_policy_state is None
            else risk_policy_state
        )
        if not isinstance(
            selected_risk_policy_state,
            (RiskPolicy, RiskPolicyUnavailable),
        ):
            raise TypeError(
                "risk_policy_state must be a RiskPolicy, "
                "RiskPolicyUnavailable, or None"
            )
        if order_retry_jitter is not None and not callable(order_retry_jitter):
            raise TypeError("order_retry_jitter must be callable or None")
        if order_retry_waiter is not None and not callable(order_retry_waiter):
            raise TypeError("order_retry_waiter must be callable or None")
        if event_runtime_notifier is not None and not callable(
            event_runtime_notifier
        ):
            raise TypeError("event_runtime_notifier must be callable or None")
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
        self._position = position
        self._trade_history_controller = trade_history_controller
        self._residual_settlement = residual_settlement  # Live root만 durable 잔여 정책을 조립한다.
        self._pending_order_recovery_enabled = (
            pending_order_recovery_enabled
        )
        self._market_stream_recovery_enabled = (
            market_stream_recovery_enabled
        )
        self._maximum_order_notional = maximum_order_notional
        self._maximum_order_submissions_per_intent = (
            maximum_order_submissions_per_intent
        )  # Phase 13 actual target은 한 intent에 새 client ID를 정확히 하나만 허용한다.
        self._risk_policy_state = selected_risk_policy_state
        self._session_risk_policy_version: int | None = None
        self._manual_kill_control_recovery_enabled = (
            trade_history_controller is not None
            and trade_history_controller.supports_manual_kill_control_recovery
        )
        restored_manual_kill_replay: tuple[
            ManualKillControlState,
            ...,
        ] = ()
        restored_manual_kill_state = ManualKillControlState()
        if self._manual_kill_control_recovery_enabled:
            restored_manual_kill_replay = (
                trade_history_controller.get_manual_kill_control_replay()
            )
            if restored_manual_kill_replay:
                restored_manual_kill_state = restored_manual_kill_replay[-1]
            # 구성 단계의 strict replay가 실패하면 volatile inactive 상태로 시작하지 않는다.
        self._manual_kill_active = restored_manual_kill_state.active
        self._risk_control_version = restored_manual_kill_state.version
        self._manual_kill_behavior_at_activation = (
            restored_manual_kill_state.behavior
            if restored_manual_kill_state.active
            else None
        )
        self._manual_kill_policy_version_at_activation = (
            restored_manual_kill_state.policy_version
            if restored_manual_kill_state.active
            else None
        )
        self._manual_kill_cleanup_verified = not (
            restored_manual_kill_state.active
            and restored_manual_kill_state.behavior
            is ManualKillBehavior.CANCEL_AND_LIQUIDATE
        )
        self._manual_kill_control_persistence_ambiguous = False
        self._last_risk_decision: RiskDecision | None = None
        self._order_retry_jitter = (
            order_retry_jitter or _unit_order_retry_jitter_factor
        )  # Phase 8 fake는 factor 1.0, 실거래는 동일 경계에 난수 provider를 주입한다.
        self._order_retry_waiter = (
            order_retry_waiter or _wait_for_order_retry_delay
        )  # 실제 startup은 backoff를 기다리고 test는 no-op waiter로 시간만 검증한다.
        self._event_runtime_notifier = (
            event_runtime_notifier
        )  # Controller는 실행 thread가 아니라 wake 신호 경계만 보존한다.

        # Phase 8 Position이 주입되면 시작 Guard와 Context도 같은 authoritative 수량을 사용한다.
        if position is not None:
            position_state = position.get_snapshot()
            entity_snapshot = PositionSnapshot(
                quantity=position_state.quantity,
                entry_price=(
                    position_state.average_entry_price
                    if position_state.quantity > Decimal("0")
                    else None
                ),
            )
            if position_snapshot is not None and position_snapshot != entity_snapshot:
                raise ValueError(
                    "position_snapshot must match the injected Position"
                )
            self._position_snapshot = entity_snapshot
        else:
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
        self._manual_kill_command_records: dict[str, _CommandRecord] = {}
        self._manual_kill_command_order: deque[str] = deque()
        for restored_receipt in restored_manual_kill_replay:
            restored_command_id = restored_receipt.command_id
            restored_expected_version = restored_receipt.expected_version
            if (
                restored_command_id is None
                or restored_expected_version is None
            ):
                raise RuntimeError(
                    "manual-kill replay receipt lacks command provenance"
                )
            restored_manual_kill_result = ManualKillResult(
                active=restored_receipt.active,
                behavior=restored_receipt.behavior,
                policy_version=restored_receipt.policy_version,
                risk_control_version=restored_receipt.version,
            )
            self._store_manual_kill_command_record(
                restored_command_id,
                (
                    restored_receipt.active,
                    restored_expected_version,
                ),
                restored_manual_kill_result,
            )  # 최근 command receipt 전부를 복원해 restart 뒤 ID payload 변경도 거부한다.

        # 주문별 조회·저장 상태와 retry 예산은 session Controller 한 곳에서만 변경한다.
        self._order_states_by_client_id: dict[str, _OrderExecutionState] = {}
        self._order_states_by_order_id: dict[str, _OrderExecutionState] = {}
        self._scheduled_order_queries: dict[str, _ScheduledOrderQuery] = {}
        self._submission_attempts_by_intent: dict[str, int] = {}
        self._quantity_overrides_by_intent: dict[str, Decimal] = {}
        self._persistence_states_by_order_id: dict[str, _OrderExecutionState] = {}
        self._force_sell_intent_id: str | None = None
        self._force_sell_retry_due_at: datetime | None = None
        self._order_trace: list[OrderExecutionTraceEntry] = []
        self._public_market_boundary_trace: list[
            PublicMarketBoundaryTraceEntry
        ] = []
        self._active_trace_event_id: str | None = None
        self._account_free_overlays: dict[str, _AccountFreeOverlay] = {}
        self._stream_reconciliation_required = False
        self._market_stream_monitoring_started = False
        self._market_stream_reconciliation_required = False
        self._market_stream_interrupted_running_session = False
        self._market_stop_requested: str | None = None
        self._indicator_store = TradingIndicatorStore()  # 지표는 세션 처리 lock 아래에서만 변경한다.
        self._latest_market_evaluation_version = 0
        self._event_runtime_failed = False  # 이 process에서는 worker failure 뒤 command gate를 다시 열지 않는다.
        self._process_ownership_ambiguous = False  # Parent/runtime identity 손실은 fresh process 전까지 해제하지 않는다.
        # 비소유 주문 execution은 fresh process의 전계정 검증 전까지 자동 복구하지 않는다.
        self._external_execution_reconciliation_required = False
        self._reconciliation_cause_status = ReconciliationCauseStatus.MISSING
        self._reconciliation_cause_category: (
            ReconciliationCauseCategory | None
        ) = None  # Fresh Controller만 새 Phase 13 원인 latch를 시작하며 session 안에서는 reset하지 않는다.
        self._startup_reconciliation_complete = False
        self._startup_reconciliation_blocked = False
        self._recovered_position_liquidation_session = False  # 일반 stop과 복구 청산의 멱등 namespace를 구분한다.
        self._diagnostics = diagnostics or RuntimeDiagnostics()  # 파일 책임은 주입된 sink에만 둔다.

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
    def maximum_order_submissions_per_intent(self) -> int:
        """
        함수 이름: maximum_order_submissions_per_intent()
        기능: journal과 새 client ID 생성 전에 적용하는 intent별 제출 상한을 반환한다.
        인자: 없음
        반환값: 1~5 범위의 immutable runtime 제출 상한
        작성 날짜: 2026/08/31
        """
        # Read-only 공개 값은 현재 제출 횟수나 주문 identity를 노출하지 않는다.
        return self._maximum_order_submissions_per_intent

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
    def startup_reconciliation_complete(self) -> bool:
        """
        함수 이름: startup_reconciliation_complete()
        기능: history·pending order·거래소 상태의 startup 재조정 완료 여부를 반환한다.
        인자: 없음
        반환값: startup full reconciliation을 성공했으면 True
        작성 날짜: 2026/08/22
        """
        with self._session_lock:
            return self._startup_reconciliation_complete  # READY 판정과 같은 lock의 값을 공개한다.

    @property
    def position(self) -> Position | None:
        """
        함수 이름: position()
        기능: Phase 8 pipeline이 실제 fill을 반영하는 mutable Position을 반환한다.
        인자: 없음
        반환값: 주입된 Position 또는 legacy Controller이면 None
        작성 날짜: 2026/08/22
        """
        with self._session_lock:
            return self._position  # mutation은 Controller pipeline에만 두고 identity만 공개한다.

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
        기능: bootstrap mode, account stream과 event runtime gate를 fail closed로 평가한다.
        인자: 없음
        반환값: 거래 command가 허용되면 True
        작성 날짜: 2026/08/21
        """
        # Testnet startup·연결 복구와 중단 session의 operator 재조정이 끝난 경우에만 공개 gate를 연다.
        return (
            self._mode_command_enabled
            and self._web_socket_gateway.account_ready
            and not self._requires_manual_kill_cleanup_locked()
            and not self._stream_reconciliation_required
            and self._market_stream_ready
            and not self._market_stream_interrupted_running_session
            and not self._event_runtime_failed
            and not self._process_ownership_ambiguous
            and not self._external_execution_reconciliation_required
            and (
                not self._pending_order_recovery_enabled
                or self._startup_reconciliation_complete
            )
            and not self._startup_reconciliation_blocked
            and self._status
            is not TradingSessionStatus.RECONCILIATION_REQUIRED
        )  # Testnet startup과 연결 backlog의 order/history reconciliation lifecycle을 모두 잠근다.

    @property
    def reconciliation_required(self) -> bool:
        """
        함수 이름: reconciliation_required()
        기능: 종료와 신규 command가 신뢰할 모든 주문·stream 재조정 blocker를 원자적으로 반환한다.
        인자: 없음
        반환값: 미해결 재조정 상태가 하나라도 있으면 True
        작성 날짜: 2026/08/24
        """
        # 공개 lifecycle과 중단 provenance, startup, stream 및 worker gate를 같은 session lock에서 판정한다.
        with self._session_lock:
            return self._reconciliation_required_locked()

    @property
    def reconciliation_cause_snapshot(self) -> ReconciliationCauseSnapshot:
        """
        함수 이름: reconciliation_cause_snapshot()
        기능: 재조정 필요 여부와 원인 latch를 같은 session lock에서 불변 snapshot으로 반환한다.
        인자: 없음
        반환값: EXACT 외에는 원인 범주를 숨기는 ReconciliationCauseSnapshot
        작성 날짜: 2026/08/31
        """
        with self._session_lock:
            # bool과 원인 상태 사이에 다른 callback이 끼어들 수 없도록 한 임계구역에서 복사한다.
            return ReconciliationCauseSnapshot(
                reconciliation_required=self._reconciliation_required_locked(),
                status=self._reconciliation_cause_status,
                category=(
                    self._reconciliation_cause_category
                    if self._reconciliation_cause_status
                    is ReconciliationCauseStatus.EXACT
                    else None
                ),
            )

    def _reconciliation_required_locked(self) -> bool:
        """
        함수 이름: _reconciliation_required_locked()
        기능: 호출자가 보유한 session lock 아래 모든 재조정 blocker를 한 번에 판정한다.
        인자: 없음
        반환값: 미해결 재조정 상태가 하나라도 있으면 True
        작성 날짜: 2026/08/31
        """
        # Fresh open order만 남아도 cleanup bool을 포함해 shutdown owner를 안전하게 차단한다.
        return (
            self._stream_reconciliation_required
            or self._market_stream_reconciliation_required
            or self._market_stream_interrupted_running_session
            or self._event_runtime_failed
            or self._process_ownership_ambiguous
            or self._external_execution_reconciliation_required
            or self._startup_reconciliation_blocked
            or not self._startup_reconciliation_complete
            or (
                self._requires_manual_kill_cleanup_locked()
                and not self._manual_kill_cleanup_verified
            )
            or self._status
            is TradingSessionStatus.RECONCILIATION_REQUIRED
        )

    def _process_lifetime_reconciliation_required_locked(self) -> bool:
        """
        함수 이름: _process_lifetime_reconciliation_required_locked()
        기능: fresh process 전에 해제할 수 없는 event·ownership·외부 실행 blocker를 판정한다.
        인자: 없음
        반환값: 현재 process에서 startup이나 신규 session을 열 수 없으면 True
        작성 날짜: 2026/09/04
        """
        # 세 flag는 일반 stream REST 재조정으로 해제하지 않고 Controller process 수명 전체에서 단조 증가한다.
        return (
            self._event_runtime_failed
            or self._process_ownership_ambiguous
            or self._external_execution_reconciliation_required
        )

    def _record_reconciliation_cause_locked(
        self,
        category: ReconciliationCauseCategory,
    ) -> None:
        """
        함수 이름: _record_reconciliation_cause_locked()
        기능: 반복 시세 장애는 같은 원인으로 유지하고 주문·계좌 등의 중복·충돌은 모호성으로 잠근다.
        인자: category -> secret 없는 안정적 재조정 원인 범주
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        if not isinstance(category, ReconciliationCauseCategory):
            raise TypeError("category must be a ReconciliationCauseCategory")

        # 이미 모호해진 latch는 후속 callback 순서와 무관하게 같은 fail-closed 상태를 유지한다.
        if self._reconciliation_cause_status in (
            ReconciliationCauseStatus.DUPLICATE,
            ReconciliationCauseStatus.CONFLICT,
        ):
            return
        if self._reconciliation_cause_status is ReconciliationCauseStatus.MISSING:
            self._reconciliation_cause_status = ReconciliationCauseStatus.EXACT
            self._reconciliation_cause_category = category
            return

        # 시세 장애→초기화→재연결 재시도는 주문 조정 원인이 추가된 것이 아니다.
        # market-only 사실이 유지될 때만 반복을 멱등 처리해 복구 뒤 명시적 STOP을 허용한다.
        if (
            self._reconciliation_cause_category is category
            and category is ReconciliationCauseCategory.MARKET_STREAM_FAILED
        ):
            return

        # 주문·계좌 등의 두 번째 기록은 여전히 단일 원인 증거가 아니므로 범주를 폐기한다.
        self._reconciliation_cause_status = (
            ReconciliationCauseStatus.DUPLICATE
            if self._reconciliation_cause_category is category
            else ReconciliationCauseStatus.CONFLICT
        )
        self._reconciliation_cause_category = None

    @property
    def market_stream_reconciliation_required(self) -> bool:
        """
        함수 이름: market_stream_reconciliation_required()
        기능: 새 Kline 세대와 REST same-version 평가가 필요한지 반환한다.
        인자: 없음
        반환값: 시장 full-resync blocker가 활성화됐으면 True
        작성 날짜: 2026/08/25
        """
        with self._session_lock:
            return self._market_stream_reconciliation_required

    @property
    def external_execution_reconciliation_required(self) -> bool:
        """
        함수 이름: external_execution_reconciliation_required()
        기능: 현재 process에서 관찰한 비소유 주문 execution의 영구 재조정 blocker를 반환한다.
        인자: 없음
        반환값: fresh process의 전계정 검증이 필요하면 True
        작성 날짜: 2026/09/01
        """
        # 비소유 execution은 app 주문 전용 reconnect가 증명할 수 없으므로 process 안에서 해제하지 않는다.
        with self._session_lock:
            return self._external_execution_reconciliation_required

    @property
    def _market_stream_ready(self) -> bool:
        """
        함수 이름: _market_stream_ready()
        기능: opt-in runtime에서 Controller blocker와 Gateway live readiness를 함께 판정한다.
        인자: 없음
        반환값: 시장 stream이 신규 effect에 안전하면 True
        작성 날짜: 2026/08/25
        """
        # Legacy 단위 조립은 기존 계약을 유지하고 production bootstrap만 live 세대를 강제한다.
        return (
            not self._market_stream_recovery_enabled
            or not self._market_stream_monitoring_started
            or (
                not self._market_stream_reconciliation_required
                and self._web_socket_gateway.kline_live_ready
            )
        )

    @property
    def risk_policy_state(self) -> RiskPolicy | RiskPolicyUnavailable:
        """
        함수 이름: risk_policy_state()
        기능: 현재 process에 주입된 configured 또는 explicit unavailable 위험 정책을 반환한다.
        인자: 없음
        반환값: 현재 RiskPolicy 또는 RiskPolicyUnavailable
        작성 날짜: 2026/08/24
        """
        with self._session_lock:
            return self._risk_policy_state  # 불변 정책 값만 외부에 공개한다.

    @property
    def last_risk_decision(self) -> RiskDecision | None:
        """
        함수 이름: last_risk_decision()
        기능: 가장 최근 신규 BUY의 허용·차단 결정과 authoritative budget을 반환한다.
        인자: 없음
        반환값: 최근 RiskDecision 또는 아직 BUY 평가가 없으면 None
        작성 날짜: 2026/08/24
        """
        with self._session_lock:
            return self._last_risk_decision  # frozen decision이므로 caller가 내부 budget을 바꿀 수 없다.

    @property
    def manual_kill_active(self) -> bool:
        """
        함수 이름: manual_kill_active()
        기능: 신규 BUY 위험 gate가 사용할 현재 manual kill 상태를 반환한다.
        인자: 없음
        반환값: manual kill이 활성화됐으면 True
        작성 날짜: 2026/08/24
        """
        with self._session_lock:
            return self._manual_kill_active  # 신규 BUY gate와 wire snapshot이 같은 잠금 상태를 읽는다.

    @property
    def manual_kill_cleanup_complete(self) -> bool:
        """
        함수 이름: manual_kill_cleanup_complete()
        기능: CANCEL_AND_LIQUIDATE의 주문·journal·Position 권위 검증이 끝났는지 반환한다.
        인자: 없음
        반환값: cleanup이 불필요하거나 권위 있게 완료됐으면 True
        작성 날짜: 2026/08/29
        """
        with self._session_lock:
            return self._manual_kill_cleanup_verified  # activation receipt와 cleanup 완료 publication을 분리한다.

    @property
    def process_ownership_ambiguous(self) -> bool:
        """
        함수 이름: process_ownership_ambiguous()
        기능: parent·runtime ownership 상실로 모든 자동 effect가 잠겼는지 반환한다.
        인자: 없음
        반환값: fresh process reconciliation이 필요하면 True
        작성 날짜: 2026/08/24
        """
        with self._session_lock:
            return self._process_ownership_ambiguous  # 외부 owner는 private flag를 추측하지 않는다.

    def replace_risk_policy(
        self,
        policy_state: RiskPolicy | RiskPolicyUnavailable,
    ) -> None:
        """
        함수 이름: replace_risk_policy()
        기능: immutable 위험 정책 state를 교체하되 active session의 고정 version은 보존한다.
        인자: policy_state -> 새 configured RiskPolicy 또는 explicit unavailable 상태
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # Mutable mapping이나 duck-typed policy를 active session에 주입하지 못하게 한다.
        if not isinstance(policy_state, (RiskPolicy, RiskPolicyUnavailable)):
            raise TypeError(
                "policy_state must be a RiskPolicy or RiskPolicyUnavailable"
            )

        with self._session_lock:
            self._risk_policy_state = policy_state  # active session은 다음 BUY에서 version mismatch로 fail closed한다.

    def set_manual_kill(
        self,
        active: bool,
        *,
        command_id: str,
        expected_version: int,
    ) -> ManualKillResult:
        """
        함수 이름: set_manual_kill()
        기능: operator manual kill을 별도 optimistic version과 command ID로 멱등 설정한다.
        인자: active -> kill switch 활성 여부인 exact bool
            command_id -> transport가 발급한 멱등 command 식별자
            expected_version -> 호출자가 관측한 risk control version
        반환값: 적용 상태와 정책 provenance를 가진 ManualKillResult
        작성 날짜: 2026/08/25
        """
        # Operator 입력을 exact bool/version으로 검증한 뒤 멱등 fingerprint를 구성한다.
        if type(active) is not bool:
            raise TypeError("active must be a bool")
        self._validate_expected_version_value(expected_version)
        fingerprint = (active, expected_version)

        with self._session_lock:
            # Fsync 결과가 불명인 process에서는 어느 control command도 상태를 다시 추측하지 않는다.
            if self._manual_kill_control_persistence_ambiguous:
                raise RuntimeError(
                    "manual-kill control persistence requires a fresh restart"
                )

            # 동일 command 재전송은 risk version이나 kill 상태를 다시 바꾸지 않고 최초 결과를 재생한다.
            cached = self._read_manual_kill_command_record(
                command_id,
                fingerprint,
            )
            if cached is not None:
                cached_result = self._require_manual_kill_result(cached)
                if self._requires_manual_kill_cleanup_locked():
                    self._begin_manual_kill_cleanup_locked(
                        self._manual_kill_cleanup_command_id_locked()
                    )
                return cached_result  # 응답 유실 재시도도 같은 activation epoch의 cleanup을 재개한다.
            if expected_version != self._risk_control_version:
                raise TradingSessionError(
                    TradingSessionFailureCode.STALE_RISK_CONTROL_VERSION,
                    "Expected risk control version does not match authoritative version",
                    current_version=self._risk_control_version,
                    expected_version=expected_version,
                )

            # 해제 직전마다 fresh REST·journal·Position을 다시 읽어 이전 완료 bool의 TOCTOU를 차단한다.
            if (
                not active
                and self._requires_manual_kill_cleanup_locked()
            ):
                self._verify_manual_kill_cleanup_locked()
                if not self._manual_kill_cleanup_verified:
                    raise TradingSessionError(
                        TradingSessionFailureCode.POSITION_RECONCILIATION_REQUIRED,
                        "Manual-kill cleanup must complete before release",
                        current_version=self._risk_control_version,
                        expected_version=expected_version,
                    )

            policy_state = self._risk_policy_state
            state_changes = self._manual_kill_active is not active

            # Active no-op은 hot policy가 아니라 최초 activation의 durable cleanup 의무를 보존한다.
            if active and not state_changes:
                receipt_behavior = self._manual_kill_behavior_at_activation
                receipt_policy_version = (
                    self._manual_kill_policy_version_at_activation
                )
            else:
                receipt_behavior = (
                    policy_state.manual_kill_behavior
                    if isinstance(policy_state, RiskPolicy)
                    else None
                )
                receipt_policy_version = (
                    policy_state.version
                    if isinstance(policy_state, RiskPolicy)
                    else None
                )

            # Toggle과 no-op 모두 성공 receipt를 fsync해 restart에서도 command ID 의미를 보존한다.
            next_control_state = ManualKillControlState(
                active=active,
                version=(
                    self._risk_control_version + 1
                    if state_changes
                    else self._risk_control_version
                ),
                command_id=command_id,
                expected_version=expected_version,
                behavior=receipt_behavior,
                policy_version=receipt_policy_version,
            )
            history_controller = self._trade_history_controller
            if self._manual_kill_control_recovery_enabled:
                if history_controller is None:
                    raise RuntimeError(
                        "manual-kill recovery capability lost its history owner"
                    )
                try:
                    history_controller.save_manual_kill_control_state(
                        next_control_state
                    )
                except Exception:
                    # 활성화 저장 실패는 즉시 메모리 kill을 켜고 모든 신규 effect를 reconciliation으로 잠근다.
                    if active:
                        self._manual_kill_active = True
                    self._manual_kill_control_persistence_ambiguous = True
                    self._record_reconciliation_cause_locked(
                        ReconciliationCauseCategory.PROCESS_OWNERSHIP_AMBIGUOUS
                    )
                    self._process_ownership_ambiguous = True
                    self._stream_reconciliation_required = True
                    if self._status in (
                        TradingSessionStatus.RUNNING,
                        TradingSessionStatus.STOPPING,
                    ):
                        self._status = (
                            TradingSessionStatus.RECONCILIATION_REQUIRED
                        )
                    if self._context.initialized:
                        try:
                            self._context.apply_runtime_patch(
                                patch(
                                    trading_phase=(
                                        TradingPhase.RECONCILIATION_REQUIRED
                                    )
                                )
                            )
                        except Exception:
                            pass  # 원 fsync 예외를 보존하되 이미 닫힌 공개 status는 되돌리지 않는다.
                    self._scheduler.clear()
                    if self._event_queue is not None:
                        self._event_queue.clear()
                    raise
            previous_cleanup_verified = self._manual_kill_cleanup_verified
            self._manual_kill_active = active
            self._risk_control_version = next_control_state.version
            if active:
                self._manual_kill_behavior_at_activation = (
                    next_control_state.behavior
                )
                self._manual_kill_policy_version_at_activation = (
                    next_control_state.policy_version
                )
                self._manual_kill_cleanup_verified = (
                    previous_cleanup_verified
                    if not state_changes
                    else next_control_state.behavior
                    is not ManualKillBehavior.CANCEL_AND_LIQUIDATE
                )
            else:
                self._manual_kill_behavior_at_activation = None
                self._manual_kill_policy_version_at_activation = None
                self._manual_kill_cleanup_verified = True
            result = ManualKillResult(
                active=self._manual_kill_active,
                behavior=next_control_state.behavior,
                policy_version=next_control_state.policy_version,
                risk_control_version=self._risk_control_version,
            )
            self._store_manual_kill_command_record(
                command_id,
                fingerprint,
                result,
            )  # 성공한 operator command receipt를 toggle 여부와 무관하게 bounded cache에 남긴다.

            # Durable activation 뒤에만 canonical STOP 또는 recovery SELL을 시작해 신규 entry를 선차단한다.
            if self._requires_manual_kill_cleanup_locked():
                self._begin_manual_kill_cleanup_locked(
                    self._manual_kill_cleanup_command_id_locked()
                )
            return result

    def resume_manual_kill_cleanup(self) -> bool:
        """
        함수 이름: resume_manual_kill_cleanup()
        기능: 재시작 reconciliation 뒤 durable CANCEL_AND_LIQUIDATE 정리를 같은 identity로 재개한다.
        인자: 없음
        반환값: app-owned open order, pending journal과 Position이 모두 0이면 True
        작성 날짜: 2026/08/29
        """
        with self._session_lock:
            if not self._requires_manual_kill_cleanup_locked():
                self._manual_kill_cleanup_verified = True
                return True  # inactive 또는 BLOCK_NEW_ORDERS에는 외부 cleanup effect가 없다.

            # Risk control version은 restart마다 같은 내부 cleanup command identity를 재구성한다.
            cleanup_command_id = self._manual_kill_cleanup_command_id_locked()
            self._begin_manual_kill_cleanup_locked(cleanup_command_id)
            return self._manual_kill_cleanup_verified

    def _manual_kill_cleanup_command_id_locked(self) -> str:
        """
        함수 이름: _manual_kill_cleanup_command_id_locked()
        기능: 활성 epoch의 control version에서 restart-safe cleanup command ID를 결정한다.
        인자: 없음
        반환값: 같은 activation 동안 변하지 않는 내부 cleanup command ID
        작성 날짜: 2026/08/29
        """
        if not self._requires_manual_kill_cleanup_locked():
            raise RuntimeError("manual-kill cleanup is not active")

        return (
            f"manual-kill-recovery-{self._risk_control_version}"
        )  # 사용자 no-op command ID와 분리해 response loss·restart에서도 외부 주문 identity를 재사용한다.

    def _requires_manual_kill_cleanup_locked(self) -> bool:
        """
        함수 이름: _requires_manual_kill_cleanup_locked()
        기능: durable activation 당시 행동이 CANCEL_AND_LIQUIDATE인지 lock 내부에서 판정한다.
        인자: 없음
        반환값: 활성 kill이 안전 정리를 요구하면 True
        작성 날짜: 2026/08/29
        """
        return (
            self._manual_kill_active
            and self._manual_kill_behavior_at_activation
            is ManualKillBehavior.CANCEL_AND_LIQUIDATE
        )  # 현재 policy hot-swap이 이미 fsync된 activation 행동을 바꾸지 못한다.

    def _begin_manual_kill_cleanup_locked(
        self,
        command_id: str,
        *,
        account_reconciliation_complete: bool = False,
    ) -> None:
        """
        함수 이름: _begin_manual_kill_cleanup_locked()
        기능: 실행 session은 STOP으로, 복구 Position은 recovery SELL로 넘기고 완료 사실을 재검증한다.
        인자: command_id -> activation 또는 restart에서 결정적으로 만든 cleanup identity
            account_reconciliation_complete -> 이번 호출이 fresh REST·signed-stream 재연결 뒤인지 여부
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        if not isinstance(command_id, str):
            raise TypeError("command_id must be a string")
        if not command_id or command_id != command_id.strip():
            raise ValueError("command_id must be a non-empty trimmed string")
        if type(account_reconciliation_complete) is not bool:
            raise TypeError("account_reconciliation_complete must be a bool")
        if not self._requires_manual_kill_cleanup_locked():
            return

        # 시작 전 startup reconciliation은 pending 취소와 Position 복원까지 마친 뒤 재개하도록 대기한다.
        if not self._startup_reconciliation_complete and self._status is (
            TradingSessionStatus.NOT_STARTED
        ):
            return

        try:
            # 실행 session에는 기존 STOP STM을 전달해 pending query→cancel→query와 force SELL을 재사용한다.
            if self._active_stm is not None and self._session_id is not None:
                if self._status is TradingSessionStatus.RUNNING:
                    self.stop_trading(
                        command_id=f"manual-kill-stop-{command_id}",
                        expected_version=self._context.version,
                    )
                elif self._status in (
                    TradingSessionStatus.RECONCILIATION_REQUIRED,
                    TradingSessionStatus.STOPPING,
                ):
                    self._resume_manual_kill_order_cleanup_locked(
                        command_id,
                        account_reconciliation_complete=(
                            account_reconciliation_complete
                        ),
                    )
            else:
                position = self._require_position()
                if position.quantity > Decimal("0"):
                    self.liquidate_recovered_position(
                        command_id=(
                            f"manual-kill-liquidation-{command_id}"
                        ),
                        expected_version=self._context.version,
                    )
        except Exception:
            # Receipt와 거래소 사실은 되돌리지 않고 operator-visible reconciliation 상태를 유지한다.
            self._record_reconciliation_cause_locked(
                ReconciliationCauseCategory.ORDER_OR_PERSISTENCE_AMBIGUOUS
            )
            self._manual_kill_cleanup_verified = False
            if self._context.initialized:
                self._status = TradingSessionStatus.RECONCILIATION_REQUIRED
                try:
                    self._context.apply_runtime_patch(
                        patch(
                            trading_phase=(
                                TradingPhase.RECONCILIATION_REQUIRED
                            )
                        )
                    )
                except Exception:
                    pass  # Cleanup 원 예외와 먼저 닫힌 공개 status를 모두 보존한다.
            else:
                self._startup_reconciliation_blocked = True
            return

        # 동기 cancel/terminal/force SELL이 끝난 경우에도 fresh open-order와 durable journal을 다시 본다.
        self._verify_manual_kill_cleanup_locked()

    def _resume_manual_kill_order_cleanup_locked(
        self,
        command_id: str,
        *,
        account_reconciliation_complete: bool,
    ) -> None:
        """
        함수 이름: _resume_manual_kill_order_cleanup_locked()
        기능: RECONCILIATION_REQUIRED 세션의 기존 주문만 query→개별 cancel→query 순서로 재개한다.
        인자: command_id -> 활성 kill epoch에서 고정된 cleanup identity
            account_reconciliation_complete -> fresh account REST와 signed stream gap을 닫았는지 여부
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        if not isinstance(command_id, str):
            raise TypeError("command_id must be a string")
        if not command_id or command_id != command_id.strip():
            raise ValueError("command_id must be a non-empty trimmed string")
        if type(account_reconciliation_complete) is not bool:
            raise TypeError("account_reconciliation_complete must be a bool")

        # Signed account 사실이나 process owner가 불명확하면 같은 ID 취소조차 다음 복구까지 보류한다.
        if not self._manual_kill_cancel_reentry_gate_open_locked(
            account_reconciliation_complete=account_reconciliation_complete,
        ):
            self._mark_manual_kill_cleanup_reconciliation_locked()
            return
        if account_reconciliation_complete:
            self._stream_reconciliation_required = False  # 방금 닫은 REST→stream ACK gap만 명시적으로 해제한다.

        # 이미 canonical STOPPING에 들어간 세션만 terminal 뒤 잔량 SELL을 같은 microstep에서 허용한다.
        active_stm = self._active_stm
        stop_effect_gate_open = (
            active_stm is not None
            and active_stm.current_state.root_state is RootState.STOPPING
            and self._manual_kill_stop_effect_gate_open_locked()
        )
        if stop_effect_gate_open:
            if self._context.runtime.trading_phase is not TradingPhase.STOPPING:
                self._context.apply_runtime_patch(
                    patch(trading_phase=TradingPhase.STOPPING)
                )
            self._status = TradingSessionStatus.STOPPING

        # Stable client ID 순서로 한 주문씩 처리해 다중 corruption에서도 effect 순서를 결정론적으로 만든다.
        unresolved_states = tuple(
            sorted(
                (
                    state
                    for state in self._order_states_by_client_id.values()
                    if (
                        not state.order.is_terminal
                        or state.persistence_pending
                        or state.pending_recovery_pending
                    )
                ),
                key=lambda state: state.order.client_order_id,
            )
        )
        outcomes: list[TradingEvent] = []
        for state in unresolved_states:
            if state.order.is_terminal:
                continue  # Terminal 저장·journal 실패는 cancel로 고치지 않고 전용 persistence 복구에 남긴다.

            # 새 주문·새 intent 없이 같은 aggregate에 bounded query budget을 한 recovery cycle만 부여한다.
            self._scheduled_order_queries.pop(
                state.order.client_order_id,
                None,
            )
            state.reconciliation_attempts = 0
            state.stop_after_reconciliation = True
            order_identifier = (
                state.order.exchange_order_id
                or state.order.client_order_id
            )
            outcomes.extend(
                self._cancel_pending_order_action(
                    CancelPendingOrder(
                        order_id=order_identifier,
                        reason="STOP_CONFIRMED",
                    )
                )
            )
            if not state.order.is_terminal:
                outcomes.extend(
                    self._reconcile_order_action(
                        ReconcileOrder(
                            order_id=order_identifier,
                            stop_after_reconciliation=True,
                        )
                    )
                )  # Cancel 응답은 완료로 쓰지 않고 즉시 같은 ID REST query로만 확인한다.

        if outcomes:
            self._enqueue_order_outcomes(tuple(outcomes))

        # Fresh open-order·journal·memory·Context가 모두 비어야 Position 청산 단계에 진입할 수 있다.
        if not self._manual_kill_order_cleanup_complete_locked():
            self._mark_manual_kill_cleanup_reconciliation_locked()
            return
        if not self._manual_kill_stop_effect_gate_open_locked():
            self._mark_manual_kill_cleanup_reconciliation_locked()
            return

        # Activation이 RECON 상태에서 시작됐다면 주문 ambiguity를 닫은 뒤 canonical STOP을 정확히 한 번 연다.
        if (
            active_stm is not None
            and active_stm.current_state.root_state is not RootState.STOPPING
        ):
            if self._context.runtime.trading_phase is not TradingPhase.IDLE:
                self._context.apply_runtime_patch(
                    patch(trading_phase=TradingPhase.IDLE)
                )
            self._status = TradingSessionStatus.RUNNING
            self.stop_trading(
                command_id=f"manual-kill-stop-{command_id}",
                expected_version=self._context.version,
            )

    def _manual_kill_cancel_reentry_gate_open_locked(
        self,
        *,
        account_reconciliation_complete: bool,
    ) -> bool:
        """
        함수 이름: _manual_kill_cancel_reentry_gate_open_locked()
        기능: RECON 상태에서 기존 ID의 query·cancel만 허용할 최소 account/process gate를 판정한다.
        인자: account_reconciliation_complete -> 현재 reconnect가 fresh REST와 stream gap을 닫았는지 여부
        반환값: 신규 주문 없이 same-ID query·cancel을 안전하게 실행할 수 있으면 True
        작성 날짜: 2026/08/29
        """
        return (
            self._mode_command_enabled
            and self._web_socket_gateway.account_ready
            and not self._event_runtime_failed
            and not self._process_ownership_ambiguous
            and not self._external_execution_reconciliation_required
            and (
                not self._pending_order_recovery_enabled
                or self._startup_reconciliation_complete
            )
            and not self._startup_reconciliation_blocked
            and (
                account_reconciliation_complete
                or not self._stream_reconciliation_required
            )
        )  # Market price는 cancel에 필요 없지만 account generation과 process owner는 반드시 확정돼야 한다.

    def _manual_kill_stop_effect_gate_open_locked(self) -> bool:
        """
        함수 이름: _manual_kill_stop_effect_gate_open_locked()
        기능: app-order ambiguity가 닫힌 뒤 canonical STOP과 잔량 SELL을 시작할 전체 effect gate를 판정한다.
        인자: 없음
        반환값: account·market·startup·process 사실이 모두 준비됐으면 True
        작성 날짜: 2026/08/29
        """
        return (
            self._mode_command_enabled
            and self._web_socket_gateway.account_ready
            and not self._stream_reconciliation_required
            and self._market_stream_ready
            and not self._market_stream_reconciliation_required
            and not self._market_stream_interrupted_running_session
            and not self._event_runtime_failed
            and not self._process_ownership_ambiguous
            and not self._external_execution_reconciliation_required
            and (
                not self._pending_order_recovery_enabled
                or self._startup_reconciliation_complete
            )
            and not self._startup_reconciliation_blocked
        )  # 노출을 줄이는 SELL도 stale price·filter 또는 소유권 모호성 위에서 제출하지 않는다.

    def _manual_kill_order_cleanup_complete_locked(self) -> bool:
        """
        함수 이름: _manual_kill_order_cleanup_complete_locked()
        기능: Position을 제외한 app open order, durable journal, memory와 Context pending이 모두 0인지 읽는다.
        인자: 없음
        반환값: fresh authoritative 주문 정리가 완료됐으면 True
        작성 날짜: 2026/08/29
        """
        if not self._web_socket_gateway.account_ready:
            return False
        try:
            app_open_orders = tuple(
                result
                for result in self._api_gateway.list_open_order_results(
                    _TRADING_SYMBOL
                )
                if result.client_order_id.startswith(
                    APP_CLIENT_ORDER_ID_PREFIX
                )
            )
            history_controller = self._require_trade_history_controller()
            pending_records = (
                history_controller.get_pending_order_recovery_records()
                if history_controller.supports_pending_order_recovery
                else ()
            )
        except Exception:
            return False  # REST·journal read 실패를 빈 주문 집합으로 완화하지 않는다.

        # Memory와 Context도 durable terminal/history가 끝나기 전에는 별도 unresolved 사실로 유지한다.
        unresolved_memory_states = tuple(
            state
            for state in self._order_states_by_client_id.values()
            if (
                not state.order.is_terminal
                or state.persistence_pending
                or state.pending_recovery_pending
            )
        )
        context_has_pending = (
            self._context.initialized
            and self._context.runtime.pending_order_id is not None
        )
        return (
            not app_open_orders
            and not pending_records
            and not unresolved_memory_states
            and not context_has_pending
        )  # Exchange, disk, memory와 Context 네 owner가 모두 비어야 True다.

    def _verify_manual_kill_cleanup_locked(self) -> bool:
        """
        함수 이름: _verify_manual_kill_cleanup_locked()
        기능: app-owned open order, pending·UNKNOWN journal과 Position이 모두 0인지 권위 있게 확인한다.
        인자: 없음
        반환값: 세 cleanup 조건과 account stream readiness가 모두 충족되면 True
        작성 날짜: 2026/08/29
        """
        if not self._requires_manual_kill_cleanup_locked():
            self._manual_kill_cleanup_verified = True
            return True

        # Signed stream과 fresh REST·journal·memory·Context 중 하나라도 남으면 완료로 게시하지 않는다.
        order_cleanup_complete = (
            self._manual_kill_order_cleanup_complete_locked()
        )
        position = self._require_position()
        cleanup_complete = (
            order_cleanup_complete
            and position.quantity == Decimal("0")
        )
        self._manual_kill_cleanup_verified = cleanup_complete
        if not cleanup_complete:
            self._mark_manual_kill_cleanup_reconciliation_locked()
        return cleanup_complete  # 단일 bool은 위 authoritative 근거가 모두 0인 경우에만 True다.

    def _mark_manual_kill_cleanup_reconciliation_locked(self) -> None:
        """
        함수 이름: _mark_manual_kill_cleanup_reconciliation_locked()
        기능: cleanup 권위 조회 실패를 완료로 완화하지 않고 재시작 또는 stream 재조정 상태로 잠근다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # Fresh REST·journal·memory·Context 중 미해결 owner를 주문·영속성 범주로 먼저 고정한다.
        self._record_reconciliation_cause_locked(
            ReconciliationCauseCategory.ORDER_OR_PERSISTENCE_AMBIGUOUS
        )
        self._manual_kill_cleanup_verified = False
        if not self._context.initialized:
            self._startup_reconciliation_blocked = True
            return  # 시작 전에는 Context 대신 startup gate가 READY publication을 차단한다.

        # 이미 같은 phase면 version을 불필요하게 올리지 않고 공개 status만 다시 고정한다.
        self._status = TradingSessionStatus.RECONCILIATION_REQUIRED
        if self._context.runtime.trading_phase is not (
            TradingPhase.RECONCILIATION_REQUIRED
        ):
            self._context.apply_runtime_patch(
                patch(trading_phase=TradingPhase.RECONCILIATION_REQUIRED)
            )

    def mark_process_ownership_ambiguous(self, reason: str) -> None:
        """
        함수 이름: mark_process_ownership_ambiguous()
        기능: parent/runtime identity 상실을 영구 fail-closed 상태로 표시하고 자동 effect를 중단한다.
        인자: reason -> credential 없는 안정적 ownership loss 분류
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # Ownership loss 분류는 공백 없는 안정 문자열만 받아 secret-bearing raw 오류를 배제한다.
        if not isinstance(reason, str):
            raise TypeError("reason must be a string")
        if not reason or reason != reason.strip():
            raise ValueError("reason must be non-empty without outer whitespace")

        with self._session_lock:
            self._record_reconciliation_cause_locked(
                ReconciliationCauseCategory.PROCESS_OWNERSHIP_AMBIGUOUS
            )
            self._process_ownership_ambiguous = True
            self._stream_reconciliation_required = True

            # Context publication 실패보다 먼저 공개 status를 닫아 active effect가 재개되지 않게 한다.
            if self._status in (
                TradingSessionStatus.RUNNING,
                TradingSessionStatus.STOPPING,
            ):
                self._status = TradingSessionStatus.RECONCILIATION_REQUIRED
            try:
                if self._context.initialized:
                    self._context.apply_runtime_patch(
                        patch(trading_phase=TradingPhase.RECONCILIATION_REQUIRED)
                    )
            finally:
                self._scheduler.clear()
                if self._event_queue is not None:
                    self._event_queue.clear()
                # Publication 예외도 queued effect cleanup과 status commit을 되돌리지 못한다.

    @property
    def _mode_command_enabled(self) -> bool:
        """
        함수 이름: _mode_command_enabled()
        기능: bootstrap이 주입한 execution mode gate만 strict bool 규칙으로 평가한다.
        인자: 없음
        반환값: 선택 mode 자체가 거래 command를 허용하면 True
        작성 날짜: 2026/08/23
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
        기능: 주문 실행 여부와 무관하게 Controller가 관찰한 외부 effect Action을 반환한다.
        인자: 없음
        반환값: 원본 순서의 외부 부수 효과 action tuple
        작성 날짜: 2026/08/21
        """
        with self._session_lock:
            return tuple(self._external_actions)

    @property
    def order_execution_trace(self) -> tuple[OrderExecutionTraceEntry, ...]:
        """
        함수 이름: order_execution_trace()
        기능: Phase 8 Case 2 message 실행 증거를 원본 순서의 immutable tuple로 반환한다.
        인자: 없음
        반환값: OrderExecutionTraceEntry tuple
        작성 날짜: 2026/08/22
        """
        with self._session_lock:
            return tuple(self._order_trace)  # 외부 호출자가 내부 trace를 변경하지 못하게 한다.

    @property
    def public_market_boundary_trace(
        self,
    ) -> tuple[PublicMarketBoundaryTraceEntry, ...]:
        """
        함수 이름: public_market_boundary_trace()
        기능: production 1L Kline→evaluation→Action observer evidence를 원본 순서의 immutable tuple로 반환한다.
        인자: 없음
        반환값: frozen PublicMarketBoundaryTraceEntry tuple
        작성 날짜: 2026/08/31
        """
        with self._session_lock:
            return tuple(self._public_market_boundary_trace)

    @property
    def pending_order_query_count(self) -> int:
        """
        함수 이름: pending_order_query_count()
        기능: 같은 ID reconciliation을 기다리는 Order 수를 반환한다.
        인자: 없음
        반환값: pending query schedule 개수
        작성 날짜: 2026/08/22
        """
        with self._session_lock:
            return len(self._scheduled_order_queries)

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

            # Startup 복구 전 Context가 초기화되지 않았어도 authoritative Position을 숨기지 않는다.
            position_state = (
                self._position.get_snapshot()
                if self._position is not None
                else None
            )
            has_open_position = (
                position_state.quantity > Decimal("0")
                if position_state is not None
                else self._position_snapshot.is_open
            )

            # 보유 여부와 같은 lock에서 기존 평단가를 읽고 종료된 포지션은 표시하지 않는다.
            position_average_entry_price = None
            if has_open_position:
                position_average_entry_price = (
                    position_state.average_entry_price
                    if position_state is not None
                    else self._position_snapshot.entry_price
                )  # UI 표시를 위해 수수료 포함 원가를 다시 계산하거나 반올림하지 않는다.

            # Configured policy의 nullable 상한과 운영 enum을 unavailable 상태와 섞지 않고 공개한다.
            configured_risk_policy = (
                self._risk_policy_state
                if isinstance(self._risk_policy_state, RiskPolicy)
                else None
            )

            # 상태 전이와 같은 lock에서 실행 Case를 읽어 polling과 실시간 event가 같은 값을 보게 한다.
            active_logic = create_trading_logic_snapshot(
                self._active_stm,
                self._context.runtime,
            )  # 선택된 REGIME의 이름으로 실행 전략을 추측하지 않는다.

            # 지표와 ACTIVE STATE를 같은 처리 단위의 상태로 publication에 묶는다.
            if active_logic is not None and self._active_stm is not None:
                active_logic = replace(
                    active_logic,
                    indicators=self._indicator_store.snapshot(self._active_stm, self._context.snapshot()),
                )

            # mutable Context의 각 값을 session lock 아래 같은 publication에 묶는다.
            return TradingSessionSnapshot(
                status=self._status,
                session_id=self._session_id,
                version=self._context.version,
                scale_in=self._context.scale_in_ratio,
                scale_out=self._context.scale_out_ratio,
                has_open_position=has_open_position,
                residual_quantity=self.residual_totals[0],
                residual_cost_basis=self.residual_totals[1],
                position_average_entry_price=position_average_entry_price,
                active_logic=active_logic,
                command_enabled=self.command_enabled,
                selected=self._selected_regime,
                support_status=(
                    None
                    if configuration is None
                    else configuration.support_status
                ),
                risk_policy_availability=(
                    self._risk_policy_state.availability
                ),
                configured_risk_policy_version=(
                    configured_risk_policy.version
                    if configured_risk_policy is not None
                    else None
                ),
                session_risk_policy_version=(
                    self._session_risk_policy_version
                ),
                risk_control_version=self._risk_control_version,
                manual_kill_active=self._manual_kill_active,
                manual_kill_cleanup_complete=(
                    self._manual_kill_cleanup_verified
                ),
                manual_kill_activation_behavior=(
                    self._manual_kill_behavior_at_activation
                ),
                manual_kill_activation_policy_version=(
                    self._manual_kill_policy_version_at_activation
                ),
                last_risk_decision=self._last_risk_decision,
                process_ownership_ambiguous=(
                    self._process_ownership_ambiguous
                ),
                max_order_notional=(
                    configured_risk_policy.max_order_notional
                    if configured_risk_policy is not None
                    else None
                ),
                max_position_notional=(
                    configured_risk_policy.max_position_notional
                    if configured_risk_policy is not None
                    else None
                ),
                max_daily_loss=(
                    configured_risk_policy.max_daily_loss
                    if configured_risk_policy is not None
                    else None
                ),
                daily_loss_scope=(
                    configured_risk_policy.daily_loss_scope
                    if configured_risk_policy is not None
                    else None
                ),
                manual_kill_behavior=(
                    configured_risk_policy.manual_kill_behavior
                    if configured_risk_policy is not None
                    else None
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
            if selected_regime is None:
                raise RuntimeError("validated selection must provide a REGIME")
            # REGIME 선택은 중지 후에도 유지하고, 이미 사용한 STM 대신 새 세션 객체를 만든다.
            # 모든 시작 Guard를 통과한 명시적 START에서만 생성하므로 자동 재개는 하지 않는다.
            selected_stm = self._selected_stm
            if selected_stm is None:
                selected_stm = self.fetch_selected_trading_logic(selected_regime)

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
            previous_order_states_by_client_id = dict(
                self._order_states_by_client_id
            )
            previous_order_states_by_order_id = dict(
                self._order_states_by_order_id
            )
            previous_scheduled_order_queries = dict(
                self._scheduled_order_queries
            )
            previous_submission_attempts = dict(
                self._submission_attempts_by_intent
            )
            previous_quantity_overrides = dict(
                self._quantity_overrides_by_intent
            )
            previous_force_sell_intent_id = self._force_sell_intent_id
            previous_force_sell_retry_due_at = self._force_sell_retry_due_at
            previous_order_trace = tuple(self._order_trace)
            previous_public_market_boundary_trace = tuple(
                self._public_market_boundary_trace
            )
            previous_recovery_liquidation_session = (
                self._recovered_position_liquidation_session
            )
            previous_session_risk_policy_version = (
                self._session_risk_policy_version
            )
            previous_last_risk_decision = self._last_risk_decision
            previous_market_evaluation_version = (
                self._latest_market_evaluation_version
            )

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
                    order_finished_observer=self._record_order_finished_trace,
                    event_processing_observer=self._observe_processing_event,
                    event_context_preparer=self._prepare_market_event_context,
                    result_observer=self._record_indicator_evaluation,
                )
                self._scheduler.clear()
                self._action_trace.clear()
                self._external_actions.clear()
                self._cleanup_failures.clear()
                self._cleanup_in_progress = False

                # 새 session은 이전 runtime state를 비우되 durable intent 예산은 process restart 후에도 유지한다.
                self._order_states_by_client_id.clear()
                self._order_states_by_order_id.clear()
                self._scheduled_order_queries.clear()
                if not self._pending_order_recovery_enabled:
                    self._submission_attempts_by_intent.clear()
                self._quantity_overrides_by_intent.clear()
                self._force_sell_intent_id = None
                self._force_sell_retry_due_at = None
                self._order_trace.clear()
                self._public_market_boundary_trace.clear()
                self._active_trace_event_id = None
                self._recovered_position_liquidation_session = False  # 일반 start는 복구 청산 세션 표식을 상속하지 않는다.
                self._session_risk_policy_version = (
                    self._risk_policy_state.version
                    if isinstance(self._risk_policy_state, RiskPolicy)
                    else None
                )  # session은 start 시점 policy version을 고정하고 hot change를 다음 BUY에서 거부한다.
                self._last_risk_decision = None
                self._latest_market_evaluation_version = 0

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
                self._order_states_by_client_id = (
                    previous_order_states_by_client_id
                )
                self._order_states_by_order_id = previous_order_states_by_order_id
                self._scheduled_order_queries = previous_scheduled_order_queries
                self._submission_attempts_by_intent = previous_submission_attempts
                self._quantity_overrides_by_intent = previous_quantity_overrides
                self._force_sell_intent_id = previous_force_sell_intent_id
                self._force_sell_retry_due_at = previous_force_sell_retry_due_at
                self._order_trace = list(previous_order_trace)
                self._public_market_boundary_trace = list(
                    previous_public_market_boundary_trace
                )
                self._recovered_position_liquidation_session = (
                    previous_recovery_liquidation_session
                )
                self._session_risk_policy_version = (
                    previous_session_risk_policy_version
                )
                self._last_risk_decision = previous_last_risk_decision
                self._latest_market_evaluation_version = (
                    previous_market_evaluation_version
                )
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

    def liquidate_recovered_position(
        self,
        *,
        command_id: str,
        expected_version: int,
    ) -> TradingSessionResult:
        """
        함수 이름: liquidate_recovered_position()
        기능: startup에서 복원한 Position을 자동 resume 없이 청산 전용 세션으로 인수한다.
        인자: command_id -> 일반 stop과 분리된 transport 멱등 식별자
            expected_version -> 호출자가 관측한 context version
        반환값: STOPPING, TERMINATED 또는 reconciliation 상태 결과
        작성 날짜: 2026/08/24
        """
        # 잘못된 version 형식은 command cache와 복구 provenance를 읽기 전에 거부한다.
        self._validate_expected_version_value(expected_version)
        fingerprint = (expected_version,)

        with self._session_lock:
            # Exact duplicate만 stale 검사보다 먼저 replay하고 다른 payload 재사용은 거부한다.
            cached = self._read_command_record(
                "recovered-position-liquidation",
                command_id,
                fingerprint,
            )
            if cached is not None:
                return self._require_session_result(cached)
            self._require_expected_version(expected_version)

            # 이미 시작된 같은 복구 청산은 새 force-sell intent 없이 진행 상태만 반환한다.
            if self._recovered_position_liquidation_session:
                if (
                    self._active_stm is None
                    or self._session_id is None
                    or self._status
                    not in (
                        TradingSessionStatus.STOPPING,
                        TradingSessionStatus.RECONCILIATION_REQUIRED,
                        TradingSessionStatus.TERMINATED,
                    )
                ):
                    raise TradingSessionError(
                        TradingSessionFailureCode.INVALID_SESSION_STATE,
                        "Recovered-position liquidation session is inconsistent",
                        current_version=self._context.version,
                        expected_version=expected_version,
                    )
                result = self._create_session_result(None)
                self._store_command_record(
                    "recovered-position-liquidation",
                    command_id,
                    fingerprint,
                    result,
                )  # 새 command ID도 현재 상태만 기록하고 liquidation intent를 재생성하지 않는다.
                return result

            # 정상 자동매매 세션은 별도 복구 Operation으로 중지하거나 인수하지 않는다.
            if self._is_active_locked():
                raise TradingSessionError(
                    TradingSessionFailureCode.TRADING_ALREADY_ACTIVE,
                    "An active trading session cannot be recovery-liquidated",
                    current_version=self._context.version,
                    expected_version=expected_version,
                )

            # 모든 startup·history·Position guard를 통과한 durable provenance만 session에 사용한다.
            recovered_regime, recovered_owner, recovered_position = (
                self._validate_recovered_position_liquidation_prerequisites()
            )
            recovered_stm = TradingSTM.get_stm_instance(
                recovered_regime
            )  # Registry가 지원하지 않는 durable REGIME은 session 생성 전에 차단한다.

            # 외부 주문 전 setup 실패를 원자 복원할 수 있도록 변경 대상 전체를 보존한다.
            context_checkpoint = self._context._create_checkpoint()
            previous_selected_regime = self._selected_regime
            previous_selected_stm = self._selected_stm
            previous_active_stm = self._active_stm
            previous_session_id = self._session_id
            previous_status = self._status
            previous_event_queue = self._event_queue
            previous_event_processor = self._event_processor
            previous_action_trace = tuple(self._action_trace)
            previous_external_actions = tuple(self._external_actions)
            previous_cleanup_failures = tuple(self._cleanup_failures)
            previous_cleanup_state = self._cleanup_in_progress
            previous_order_states_by_client_id = dict(
                self._order_states_by_client_id
            )
            previous_order_states_by_order_id = dict(
                self._order_states_by_order_id
            )
            previous_scheduled_order_queries = dict(
                self._scheduled_order_queries
            )
            previous_submission_attempts = dict(
                self._submission_attempts_by_intent
            )
            previous_quantity_overrides = dict(
                self._quantity_overrides_by_intent
            )
            previous_force_sell_intent_id = self._force_sell_intent_id
            previous_force_sell_retry_due_at = self._force_sell_retry_due_at
            previous_order_trace = tuple(self._order_trace)
            previous_public_market_boundary_trace = tuple(
                self._public_market_boundary_trace
            )
            previous_active_trace_event_id = self._active_trace_event_id
            previous_market_evaluation_version = (
                self._latest_market_evaluation_version
            )
            previous_recovery_liquidation_session = (
                self._recovered_position_liquidation_session
            )

            def restore_pre_liquidation_state() -> None:
                """
                함수 이름: restore_pre_liquidation_state()
                기능: 외부 effect 전 복구 청산 setup을 원래 NOT_STARTED snapshot으로 되돌린다.
                인자: 없음
                반환값: 없음
                작성 날짜: 2026/08/24
                """
                # Context, STM과 session identity를 명령 전 snapshot으로 함께 되돌린다.
                self._context._restore_checkpoint(context_checkpoint)
                self._selected_regime = previous_selected_regime
                self._selected_stm = previous_selected_stm
                self._active_stm = previous_active_stm
                self._session_id = previous_session_id
                self._status = previous_status
                self._event_queue = previous_event_queue
                self._event_processor = previous_event_processor
                self._scheduler.clear()

                # 외부 effect 전 임시 trace와 주문 pipeline 상태도 같은 checkpoint로 복원한다.
                self._action_trace = list(previous_action_trace)
                self._external_actions = list(previous_external_actions)
                self._cleanup_failures = list(previous_cleanup_failures)
                self._cleanup_in_progress = previous_cleanup_state
                self._order_states_by_client_id = (
                    previous_order_states_by_client_id
                )
                self._order_states_by_order_id = previous_order_states_by_order_id
                self._scheduled_order_queries = previous_scheduled_order_queries
                self._submission_attempts_by_intent = previous_submission_attempts
                self._quantity_overrides_by_intent = previous_quantity_overrides
                self._force_sell_intent_id = previous_force_sell_intent_id
                self._force_sell_retry_due_at = previous_force_sell_retry_due_at
                self._order_trace = list(previous_order_trace)
                self._public_market_boundary_trace = list(
                    previous_public_market_boundary_trace
                )
                self._active_trace_event_id = previous_active_trace_event_id
                self._latest_market_evaluation_version = (
                    previous_market_evaluation_version
                )
                self._recovered_position_liquidation_session = (
                    previous_recovery_liquidation_session
                )

            try:
                # Durable REGIME·owner와 Position을 새 Context에 묶되 strategy run은 호출하지 않는다.
                self._context.initialize(
                    self._account,
                    recovered_regime,
                    recovered_position,
                    self._context.scale_in_ratio,
                    self._context.scale_out_ratio,
                )
                self._context.update_position(
                    recovered_position,
                    position_owner=recovered_owner,
                )
                self._session_id = str(uuid4())  # 복구 청산도 transport가 검증하는 canonical UUID를 쓴다.
                self._selected_stm = None
                self._active_stm = recovered_stm
                self._event_queue = SerialEventQueue()
                self._event_processor = RunToCompletionEventProcessor(
                    stm=recovered_stm,
                    context_provider=self._context.snapshot,
                    action_executor=self._execute_action,
                    event_queue=self._event_queue,
                    clock=self._clock,
                    order_finished_observer=self._record_order_finished_trace,
                    event_processing_observer=self._observe_processing_event,
                    event_context_preparer=self._prepare_market_event_context,
                    result_observer=self._record_indicator_evaluation,
                )
                self._scheduler.clear()
                self._action_trace.clear()
                self._external_actions.clear()
                self._cleanup_failures.clear()
                self._cleanup_in_progress = False

                # 이전 runtime state는 제거하되 journal이 복원한 intent 예산은 초기화하지 않는다.
                self._order_states_by_client_id.clear()
                self._order_states_by_order_id.clear()
                self._scheduled_order_queries.clear()
                if not self._pending_order_recovery_enabled:
                    self._submission_attempts_by_intent.clear()
                self._quantity_overrides_by_intent.clear()
                self._force_sell_intent_id = None
                self._force_sell_retry_due_at = None
                self._order_trace.clear()
                self._public_market_boundary_trace.clear()
                self._active_trace_event_id = None
                self._recovered_position_liquidation_session = True
                self._latest_market_evaluation_version = 0

                # Fresh STM에는 LOGIC_STARTED 대신 global STOP_CONFIRMED만 정확히 한 번 전달한다.
                stop_event = TradingEvent(
                    event_type=TradingEventType.STOP_CONFIRMED,
                    occurred_at=self._clock(),
                    priority=EventPriority.USER_COMMAND,
                    event_id=f"recovery-stop-{self._session_id}-{command_id}",
                )
                stop_result = recovered_stm.handle(
                    stop_event,
                    self._context.snapshot(),
                )
                has_force_sell = any(
                    isinstance(action, ForceSellAll)
                    for action in stop_result.action_requests
                )
                if stop_result.transition_ids != ("G-06",) or not has_force_sell:
                    raise RuntimeError(
                        "Recovered open Position must enter canonical G-06"
                    )
            except Exception:
                # Journal이나 REST POST 전 setup 실패만 원래 NOT_STARTED 상태로 되돌린다.
                restore_pre_liquidation_state()
                raise

            # G-06를 공개한 뒤부터는 durable journal과 거래소 사실을 rollback하지 않는다.
            self._status = TradingSessionStatus.STOPPING
            previous_trace_event_id = self._active_trace_event_id
            self._active_trace_event_id = stop_event.event_id
            try:
                self._apply_stm_result(stop_result)
            except _RecoveredPositionLiquidationGateClosedError as error:
                # Gate가 effect 직전에 닫혔으면 journal·POST가 없으므로 명령 전 상태로 재시도 가능하게 복원한다.
                restore_pre_liquidation_state()
                raise TradingSessionError(
                    TradingSessionFailureCode.CONNECTION_NOT_READY,
                    "Account stream changed before recovered-position liquidation",
                    current_version=self._context.version,
                    expected_version=expected_version,
                ) from error
            except _RecoveredPositionLiquidationPreflightError as error:
                # Journal·POST가 없는 filter·commission 사전검증 실패는 명령 전 상태로 되돌린다.
                restore_pre_liquidation_state()
                raise TradingSessionError(
                    TradingSessionFailureCode.COMMAND_DISABLED,
                    "Recovered-position liquidation preflight failed",
                    current_version=self._context.version,
                    expected_version=expected_version,
                ) from error
            except Exception:
                self._record_reconciliation_cause_locked(
                    ReconciliationCauseCategory.ORDER_OR_PERSISTENCE_AMBIGUOUS
                )
                self._status = TradingSessionStatus.RECONCILIATION_REQUIRED
                raise  # 예상 밖 effect 실패도 NOT_STARTED로 위장하지 않고 operator 복구를 요구한다.
            finally:
                self._active_trace_event_id = previous_trace_event_id

            self._synchronize_status_from_context(recovered_stm)
            result = self._create_session_result(stop_result)
            self._store_command_record(
                "recovered-position-liquidation",
                command_id,
                fingerprint,
                result,
            )  # G-06 Action 적용 뒤에만 exact duplicate receipt를 고정한다.
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

            # 시세만 끊긴 세션의 명시적 STOP은 복구 후에도 버리지 않는다.
            # 주문/계좌/worker 원인이 섞인 상태는 기존 조정 절차를 우회하지 않는다.
            if (
                self._status is TradingSessionStatus.RECONCILIATION_REQUIRED
                and self._market_stream_interrupted_running_session
                and self._reconciliation_cause_status is ReconciliationCauseStatus.EXACT
                and self._reconciliation_cause_category is ReconciliationCauseCategory.MARKET_STREAM_FAILED
            ):
                if self._market_stop_requested is None:
                    self._market_stop_requested = command_id
                if (
                    not self._market_stream_reconciliation_required
                    and self._market_stream_ready
                    and not self._stream_reconciliation_required
                    and not self._process_lifetime_reconciliation_required_locked()
                    and not self._startup_reconciliation_blocked
                    and self._startup_reconciliation_complete
                    and self.pending_order_query_count == 0
                    and self._context.runtime.pending_order_id is None
                    and self._context.runtime.pending_intent_id is None
                    and self._context.runtime.pending_exit_reason is None
                    and active_stm.current_state.root_state is not RootState.STOPPING
                ):
                    self._market_stream_interrupted_running_session = False
                    self._market_stop_requested = None
                    # 같은 lock 안에서 곧바로 canonical STOP을 적용하므로 시장 평가를 재개하지 않는다.
                    self._status = TradingSessionStatus.RUNNING

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

            # STOP 이전에 대기하던 시장·timer만 폐기해 이후 생성될 주문 outcome은 보존한다.
            self._scheduler.clear()
            if self._event_queue is not None:
                self._event_queue.clear()

            # D05에 따라 cleanup이나 force-sell 기록 전에 STM.handle을 먼저 호출한다.
            stop_result = active_stm.handle(
                stop_event,
                self._context.snapshot(),
            )
            previous_trace_event_id = self._active_trace_event_id
            self._active_trace_event_id = stop_event.event_id
            try:
                self._apply_stm_result(stop_result)
            finally:
                self._active_trace_event_id = previous_trace_event_id
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

            enqueued_event = self._event_queue.enqueue(event)
            if enqueued_event is not None:
                self._request_event_runtime_processing()

            return enqueued_event

    def observe_public_market_boundary(
        self,
        *,
        message_id: str,
        event_type: str,
        source_event_id: str,
        market_version: int,
    ) -> PublicMarketBoundaryTraceEntry | None:
        """
        함수 이름: observe_public_market_boundary()
        기능: MarketDataController의 실제 builder 전·후 1L.1/1L.2 경계를 immutable production trace로 기록한다.
        인자: message_id -> 1L.1 또는 1L.2
            event_type -> KLINE_OBSERVED 또는 MARKET_EVALUATED
            source_event_id -> canonical public Kline source identity
            market_version -> source가 만든 authoritative MarketSnapshot version
        반환값: 기록·dedup한 frozen entry 또는 비활성 session이면 None
        작성 날짜: 2026/08/31
        """
        if message_id not in {"1L.1", "1L.2"}:
            raise ValueError("public market observer accepts only 1L.1 and 1L.2")
        if not isinstance(event_type, str):
            raise TypeError("event_type must be a string")
        if (
            not isinstance(source_event_id, str)
            or not source_event_id
            or source_event_id != source_event_id.strip()
        ):
            raise ValueError("source_event_id must be non-empty canonical text")
        if type(market_version) is not int or market_version < 1:
            raise ValueError("market_version must be a positive exact integer")

        with self._session_lock:
            # Start 전 REST merge와 terminal session Kline은 다음 run의 evidence로 이월하지 않는다.
            if (
                self._cleanup_in_progress
                or self._status is not TradingSessionStatus.RUNNING
            ):
                return None
            active_stm = self._active_stm
            if active_stm is None:
                raise RuntimeError("running public market observer requires an active STM")
            evaluation_id = f"market:{market_version}:{source_event_id}"
            candidate = PublicMarketBoundaryTraceEntry(
                message_id=message_id,
                event_type=event_type,
                source_event_id=source_event_id,
                evaluation_id=evaluation_id,
                market_version=market_version,
                context_version=self._context.version,
                regime=active_stm.regime_type,
            )
            existing_entries = tuple(
                entry
                for entry in self._public_market_boundary_trace
                if entry.evaluation_id == evaluation_id
            )
            if candidate in existing_entries:
                return candidate  # 같은 public callback replay는 새 evidence sequence를 만들지 않는다.
            if any(entry.message_id == message_id for entry in existing_entries):
                raise RuntimeError("public market boundary evidence conflicts with an existing step")
            if message_id == "1L.2" and (
                not existing_entries
                or existing_entries[-1].message_id != "1L.1"
            ):
                raise RuntimeError("market evaluation evidence requires its Kline observation")
            self._reserve_public_market_boundary_trace_capacity(
                required_entries=1,
                protected_evaluation_id=(
                    evaluation_id if existing_entries else None
                ),
            )
            self._public_market_boundary_trace.append(candidate)
            self._record_diagnostic("market_boundary", trace=candidate)  # Kline 수신·지표 계산·queue 처리 사이의 누락 위치를 구분한다.

            return candidate

    def _append_public_market_action_boundary(
        self,
        action: SubmitOrder,
    ) -> None:
        """
        함수 이름: _append_public_market_action_boundary()
        기능: 현재 public evaluation이 실제 STM SubmitOrder를 낸 순간 1L.3 evidence를 effect 전에 기록한다.
        인자: action -> STM이 방출한 immutable SubmitOrder
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        if not isinstance(action, SubmitOrder):
            raise TypeError("action must be a SubmitOrder")
        evaluation_id = self._active_trace_event_id
        if evaluation_id is None or not evaluation_id.startswith("market:"):
            return  # Direct command와 retry event는 public 1L evaluation chain으로 가장하지 않는다.
        matching_entries = tuple(
            entry
            for entry in self._public_market_boundary_trace
            if entry.evaluation_id == evaluation_id
        )
        if not matching_entries:
            raise RuntimeError(
                "public market Action requires retained Kline and evaluation evidence"
            )  # market:* event는 evicted·누락 1L evidence 상태에서 주문 effect로 진행하지 않는다.
        if tuple(entry.message_id for entry in matching_entries) != (
            "1L.1",
            "1L.2",
        ):
            raise RuntimeError("public Action evidence requires exact Kline and evaluation steps")
        evaluated_entry = matching_entries[-1]
        if self._context.version < evaluated_entry.context_version:
            raise RuntimeError("public Action Context version moved backward")
        self._reserve_public_market_boundary_trace_capacity(
            required_entries=1,
            protected_evaluation_id=evaluation_id,
        )

        # 이전 두 entry를 변경하지 않고 effect 직전 실제 Context와 typed action만 새 frozen entry로 추가한다.
        self._public_market_boundary_trace.append(
            PublicMarketBoundaryTraceEntry(
                message_id="1L.3",
                event_type="ACTION_EMITTED",
                source_event_id=evaluated_entry.source_event_id,
                evaluation_id=evaluation_id,
                market_version=evaluated_entry.market_version,
                context_version=self._context.version,
                regime=evaluated_entry.regime,
                action_type="SUBMIT_ORDER",
                side=action.side,
                strategy=action.strategy,
            )
        )

    def _reserve_public_market_boundary_trace_capacity(
        self,
        *,
        required_entries: int,
        protected_evaluation_id: str | None,
    ) -> None:
        """
        함수 이름: _reserve_public_market_boundary_trace_capacity()
        기능: 장기 session에서 오래된 evaluation 전체를 제거해 1L chain을 쪼개지 않는 bounded trace를 유지한다.
        인자: required_entries -> 이번 operation이 추가할 entry 개수
            protected_evaluation_id -> 현재 완성 중이라 제거하면 안 되는 evaluation ID 또는 None
        반환값: 요청한 공간을 확보하면 없음
        작성 날짜: 2026/08/31
        """
        if type(required_entries) is not int or required_entries < 1:
            raise ValueError("required_entries must be a positive exact integer")
        if protected_evaluation_id is not None and (
            not isinstance(protected_evaluation_id, str)
            or not protected_evaluation_id
            or protected_evaluation_id != protected_evaluation_id.strip()
        ):
            raise ValueError(
                "protected_evaluation_id must be canonical text or None"
            )
        if required_entries > _MAX_PUBLIC_MARKET_BOUNDARY_TRACE_ENTRIES:
            raise RuntimeError("public market boundary reservation exceeds capacity")

        # Capacity가 찰 때만 가장 오래된 evaluation의 1L.1~1L.3을 한 단위로 제거한다.
        while (
            len(self._public_market_boundary_trace) + required_entries
            > _MAX_PUBLIC_MARKET_BOUNDARY_TRACE_ENTRIES
        ):
            oldest_evaluation_id = next(
                (
                    entry.evaluation_id
                    for entry in self._public_market_boundary_trace
                    if entry.evaluation_id != protected_evaluation_id
                ),
                None,
            )
            if oldest_evaluation_id is None:
                raise RuntimeError(
                    "public market boundary capacity cannot preserve active evaluation"
                )
            self._public_market_boundary_trace = [
                entry
                for entry in self._public_market_boundary_trace
                if entry.evaluation_id != oldest_evaluation_id
            ]  # 한 evaluation 일부만 남겨 synthetic incomplete chain을 만들지 않는다.

    def observe_market_evaluation(
        self,
        market: MarketEvaluationSnapshot,
        *,
        source_event_id: str,
        market_version: int,
    ) -> TradingEvent | None:
        """
        함수 이름: observe_market_evaluation()
        기능: 외부 시장 평가와 source version을 불변 event로 묶어 production queue에 넣는다.
        인자: market -> Kline·지표 계산에서 만든 불변 MarketEvaluationSnapshot
            source_event_id -> 원본 market event의 안정적이고 비밀 없는 식별자
            market_version -> 평가가 사용한 authoritative MarketSnapshot version
        반환값: queue identity가 부여된 TradingEvent 또는 비활성·중복이면 None
        작성 날짜: 2026/08/24
        """
        # Adapter가 임의 mapping이나 공백 식별자로 시장 provenance를 가장하지 못하게 한다.
        if not isinstance(market, MarketEvaluationSnapshot):
            raise TypeError("market must be a MarketEvaluationSnapshot")
        if not isinstance(source_event_id, str):
            raise TypeError("source_event_id must be a string")
        if not source_event_id or source_event_id != source_event_id.strip():
            raise ValueError(
                "source_event_id must be non-empty without outer whitespace"
            )
        if type(market_version) is not int:
            raise TypeError("market_version must be an exact int")
        if market_version <= 0:
            raise ValueError("market_version must be positive")
        if (
            not market.realtime_price.is_finite()
            or market.realtime_price <= Decimal("0")
        ):
            raise ValueError(
                "market realtime_price must be a positive finite Decimal"
            )

        with self._session_lock:
            # 비활성·종료 session에는 다음 start로 새 시장 사실을 몰래 이월하지 않는다.
            if (
                self._cleanup_in_progress
                or self._status is not TradingSessionStatus.RUNNING
            ):
                return None

            # 계산 도중 MarketSnapshot이 전진했다면 서로 다른 source를 섞지 않고 fail closed한다.
            authoritative_market_version = self._market_snapshot.version
            if market_version != authoritative_market_version:
                raise ValueError(
                    "market_version must match the authoritative MarketSnapshot"
                )

            # Queue 대기 중 다른 Kline이 도착해도 source 평가를 잃지 않도록 event 자체에 보존한다.
            evaluation_time = self._clock()
            event_type = self._select_market_event_type(market)
            event = TradingEvent(
                event_type=event_type,
                occurred_at=evaluation_time,
                priority=EventPriority.MARKET,
                event_id=f"market:{market_version}:{source_event_id}",
                lower_event_id=self._context.runtime.lower_event_id,
                candle_id=market.current_30m_candle_id,
                market_evaluation=market,
                market_version=market_version,
            )
            return self.enqueue_event(event)  # Context mutation은 FIFO claim 직후 준비 단계에서만 수행한다.

    def _prepare_market_event_context(
        self,
        event: TradingEvent,
    ) -> TradingEvent:
        """
        함수 이름: _prepare_market_event_context()
        기능: claimed market event의 원본 평가를 Context에 적용하고 처리 시점 runtime으로 재분류한다.
        인자: event -> 직렬 queue가 꺼낸 immutable TradingEvent
        반환값: same-source Context와 일치하도록 분류·scope provenance를 갱신한 event
        작성 날짜: 2026/08/29
        """
        if event.market_evaluation is None:
            # 내부 후속·retry에서도 새 signal/체결/timer 기준의 경과시간을 다시 결합한다.
            if self._latest_market_evaluation_version > 0:
                self._context.update_market(self._enrich_market_evaluation_elapsed(
                    self._context.market, event.occurred_at,
                ))
            return event  # 사용자·주문·내부 event는 기존 Context와 event identity를 그대로 사용한다.
        if not isinstance(event.market_evaluation, MarketEvaluationSnapshot):
            raise TypeError(
                "market_evaluation must be a MarketEvaluationSnapshot"
            )
        if event.market_version is None:
            raise RuntimeError("market event requires a source version")

        # Queue backlog는 현재 MarketSnapshot보다 오래될 수 있지만 처리 순서는 반드시 전진해야 한다.
        if event.market_version > self._market_snapshot.version:
            raise ValueError(
                "market event version cannot exceed the authoritative snapshot"
            )
        if event.market_version <= self._latest_market_evaluation_version:
            raise ValueError(
                "market event versions must be processed in strictly increasing order"
            )

        # 앞선 microstep이 만든 signal·Position 시각을 반영한 뒤 이 event의 발생 시각까지 경과를 계산한다.
        enriched_market = self._enrich_market_evaluation_elapsed(
            event.market_evaluation,
            event.occurred_at,
        )
        event_type = self._select_market_event_type(enriched_market)
        prepared_event = replace(
            event,
            event_type=event_type,
            lower_event_id=self._context.runtime.lower_event_id,
            candle_id=enriched_market.current_30m_candle_id,
            market_evaluation=enriched_market,
        )

        # 모든 검증과 immutable event 생성이 끝난 뒤에만 Context와 processed version을 commit한다.
        self._context.update_market(enriched_market)
        self._latest_market_evaluation_version = event.market_version
        return prepared_event

    def _enrich_market_evaluation_elapsed(
        self,
        market: MarketEvaluationSnapshot,
        evaluation_time: datetime,
    ) -> MarketEvaluationSnapshot:
        """
        함수 이름: _enrich_market_evaluation_elapsed()
        기능: 시장 계산기가 소유하지 않는 Position·signal·Case C timer 경과 시간을 결합한다.
        인자: market -> 순수 시장 지표와 monotonic 연속 flag를 가진 평가
            evaluation_time -> 이번 Context와 event가 공유할 timezone-aware 시각
        반환값: 세 runtime 경과 시간이 채워진 새 MarketEvaluationSnapshot
        작성 날짜: 2026/08/29
        """
        if not isinstance(evaluation_time, datetime):
            raise TypeError("evaluation_time must be a datetime")
        if (
            evaluation_time.tzinfo is None
            or evaluation_time.utcoffset() is None
        ):
            raise ValueError("evaluation_time must be timezone-aware")

        # Context runtime과 authoritative Position owner를 같은 session lock 아래에서 한 번만 읽는다.
        runtime = self._context.runtime
        entered_at = (
            self._position.entered_at
            if self._position is not None
            else None
        )
        holding_elapsed = self._calculate_non_negative_elapsed(
            evaluation_time,
            entered_at,
            "Position entered_at",
        )
        signal_elapsed = self._calculate_non_negative_elapsed(
            evaluation_time,
            runtime.signal_time,
            "signal_time",
        )
        case_c_timer_elapsed = self._calculate_non_negative_elapsed(
            evaluation_time,
            runtime.timer_base_time,
            "timer_base_time",
        )
        return replace(
            market,
            holding_elapsed=holding_elapsed,
            signal_elapsed=signal_elapsed,
            case_c_timer_elapsed=case_c_timer_elapsed,
        )

    @staticmethod
    def _calculate_non_negative_elapsed(
        evaluation_time: datetime,
        started_at: datetime | None,
        field_name: str,
    ) -> timedelta:
        """
        함수 이름: _calculate_non_negative_elapsed()
        기능: optional 시작 시각부터 평가 시각까지 음수가 아닌 경과 시간을 계산한다.
        인자: evaluation_time -> 현재 평가 시각
            started_at -> runtime 또는 Position 시작 시각, 없으면 None
            field_name -> fail-closed 오류에 사용할 필드 이름
        반환값: 시작 시각이 없으면 0, 있으면 두 시각의 차이
        작성 날짜: 2026/08/29
        """
        if started_at is None:
            return timedelta(0)  # 활성 timer가 없으면 만료 조건을 충족시키지 않는다.

        elapsed = evaluation_time - started_at
        if elapsed < timedelta(0):
            raise ValueError(
                f"{field_name} must not be later than evaluation_time"
            )

        return elapsed

    def _select_market_event_type(
        self,
        market: MarketEvaluationSnapshot,
    ) -> TradingEventType:
        """
        함수 이름: _select_market_event_type()
        기능: 현재 session scope와 밴드 사실에서 우선순위가 가장 높은 공개 시장 event를 선택한다.
        인자: market -> 이미 Context에 적용한 same-version 시장 평가 snapshot
        반환값: upper, 최초·신규 lower 또는 일반 market update 유형
        작성 날짜: 2026/08/24
        """
        # 상단 접촉은 미구현 상단 전략 진입 대신 기존 안전 종료 전이를 가장 먼저 선택한다.
        if (
            market.upper_band > Decimal("0")
            and market.realtime_price >= market.upper_band
        ):
            return TradingEventType.UPPER_BAND_TOUCHED

        # 봉 확정을 기다리지 않고 현재가와 동일 평가의 실시간 30분봉 하단 Band를 비교한다.
        lower_touched = (
            market.lower_band > Decimal("0")
            and market.realtime_price <= market.lower_band
        )
        if not lower_touched:
            return TradingEventType.MARKET_DATA_UPDATED

        # 새 scope를 열 수 있을 때만 G-03으로 분류하고, 보유·잠금 중 확정봉 판단은 계속 전달한다.
        runtime = self._context.runtime
        if runtime.lower_event_id is None:
            return TradingEventType.LOWER_BAND_TOUCHED
        if (
            runtime.position_owner is None
            and runtime.pending_order_id is None
            and (not runtime.case_c_consumed_for_event or runtime.case_c_recovery_confirmed)
            and market.current_30m_candle_id is not None
            and market.current_30m_candle_id != runtime.touch_candle_id
        ):
            return TradingEventType.NEW_30M_LOWER_BAND_TOUCHED

        return TradingEventType.MARKET_DATA_UPDATED  # 같은 lower scope의 반복 tick은 새 event를 열지 않는다.

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
            try:
                result = await processor.process_next()
            except Exception as error:
                self._diagnostics.record_exception("process_next_event", error, session_id=self._session_id)
                raise  # 진단을 추가해도 원래 예외와 fail-close 경로는 그대로 유지한다.
            if result is not None:
                active_stm = self._active_stm
                if active_stm is not None:
                    self._synchronize_status_from_context(active_stm)

            # Reconciliation outcome이 RUNNING을 복원해도 active C&L은 lock 밖에 그 상태를 게시하지 않는다.
            if (
                self._requires_manual_kill_cleanup_locked()
                and self._status is TradingSessionStatus.RUNNING
            ):
                self._begin_manual_kill_cleanup_locked(
                    self._manual_kill_cleanup_command_id_locked()
                )

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
            try:
                results = await processor.drain(max_microsteps=max_microsteps)
            except Exception as error:
                self._diagnostics.record_exception("drain_events", error, session_id=self._session_id)
                raise  # 실패한 입력은 앞선 evaluation_started와 action_requested로 추적한다.
            active_stm = self._active_stm
            if active_stm is not None:
                self._synchronize_status_from_context(active_stm)

            # Reconciliation outcome 뒤 신규 strategy 상태가 잠깐 열려도 같은 lock에서 STOP을 재개한다.
            if self._requires_manual_kill_cleanup_locked():
                if self._status is TradingSessionStatus.RUNNING:
                    self._begin_manual_kill_cleanup_locked(
                        self._manual_kill_cleanup_command_id_locked()
                    )
                else:
                    self._verify_manual_kill_cleanup_locked()

            return tuple(results)

    async def run_event_runtime_cycle(
        self,
        *,
        max_microsteps: int = 10_000,
    ) -> tuple[TradingSTMResult, ...]:
        """
        함수 이름: run_event_runtime_cycle()
        기능: due 주문 retry를 한 번 release한 뒤 직렬 queue를 bounded microstep으로 처리한다.
        인자: max_microsteps -> 이번 production cycle에서 허용할 최대 event 수
        반환값: 실제 처리한 TradingSTMResult tuple
        작성 날짜: 2026/08/24
        """
        # bool과 무한·비양수 한도는 queue를 건드리기 전에 명시적으로 거부한다.
        if type(max_microsteps) is not int:
            raise TypeError("max_microsteps must be an integer")
        if max_microsteps <= 0:
            raise ValueError("max_microsteps must be positive")

        with self._session_lock:
            # 비활성 또는 terminal cleanup 중인 session에서는 scheduler와 queue를 모두 보존한다.
            if self._cleanup_in_progress or self._status not in (
                TradingSessionStatus.RUNNING,
                TradingSessionStatus.STOPPING,
                TradingSessionStatus.RECONCILIATION_REQUIRED,
            ):
                return ()

            # 같은 lock 안에서 due 작업을 먼저 enqueue하고 그 결과까지 이번 bounded cycle에 처리한다.
            self.trigger_scheduled_evaluations(
                ReevaluationTrigger.RETRY_BACKOFF,
                occurred_at=self._clock(),
            )
            return await self.drain_events(max_microsteps=max_microsteps)

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
            # cleanup 뒤에는 전략 timer와 주문 reconciliation 모두 새 event를 만들지 않는다.
            if self._cleanup_in_progress:
                return ()
            if self._event_queue is None:
                raise RuntimeError("running session requires an event queue")

            # RETRY_BACKOFF trigger는 STOPPING에서도 same-order query와 force retry를 깨운다.
            order_events: tuple[TradingEvent, ...] = ()
            if trigger is ReevaluationTrigger.RETRY_BACKOFF:
                order_events = self.trigger_order_reconciliation(
                    occurred_at=occurred_at,
                )
            if self._status is not TradingSessionStatus.RUNNING:
                return order_events

            # due schedule을 꺼낸 순서대로 dedup queue에 넣고 실제 수락 event만 반환한다.
            released_events = self._scheduler.release(trigger, occurred_at)
            enqueued_events: list[TradingEvent] = list(order_events)
            for released_event in released_events:
                enqueued_event = self._event_queue.enqueue(released_event)
                if enqueued_event is not None:
                    enqueued_events.append(enqueued_event)

            if enqueued_events:
                self._request_event_runtime_processing()

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
            self._account_free_overlays.clear()  # 새 full snapshot이 이전 session fill 보정을 대체한다.
            self._account_subscription = None
            account_subscription = (
                self._web_socket_gateway.start_account_info_stream()
            )
            self._account_subscription = account_subscription

        return self._account

    def observe_order_result(self, result: OrderResult) -> bool:
        """
        함수 이름: observe_order_result()
        기능: User Data Stream 주문 결과를 같은 Order state에 멱등 반영하고 outcome을 queue에 넣는다.
        인자: result -> WebSocketGateway가 정규화한 executionReport 결과
        반환값: 현재 Controller 주문에 결과를 적용했으면 True
        작성 날짜: 2026/08/22
        """
        # Raw payload나 다른 domain 객체는 session state 조회 전에 거부한다.
        if not isinstance(result, OrderResult):
            raise TypeError("result must be an OrderResult")

        with self._session_lock:
            # Prefixless execution은 exchange ID가 앱 주문과 충돌해도 app-owned 결과로 승격하지 않는다.
            if not result.client_order_id.startswith(
                APP_CLIENT_ORDER_ID_PREFIX
            ):
                self._stream_reconciliation_required = True
                self._external_execution_reconciliation_required = True
                if self._is_active_locked():
                    self._enter_order_reconciliation(
                        None,
                        OrderExecutionFailureCode.STREAM_RECONCILIATION_REQUIRED,
                        message_id=None,
                        cause_category=(
                            ReconciliationCauseCategory.ACCOUNT_STREAM_UNKNOWN_OR_EXTERNAL_EXECUTION
                        ),
                    )
                else:
                    self._record_reconciliation_cause_locked(
                        ReconciliationCauseCategory.ACCOUNT_STREAM_UNKNOWN_OR_EXTERNAL_EXECUTION
                    )
                return False  # Fresh process만 account-wide 외부 실행 사실을 다시 검증할 수 있다.

            # Client ID를 우선 사용하고 exchange ID는 cancel replace 상관관계 보조로만 사용한다.
            state = self._order_states_by_client_id.get(result.client_order_id)
            if state is None and result.exchange_order_id is not None:
                state = self._order_states_by_order_id.get(
                    result.exchange_order_id
                )
            if state is None:
                self._stream_reconciliation_required = True
                if self._is_active_locked():
                    self._enter_order_reconciliation(
                        None,
                        OrderExecutionFailureCode.STREAM_RECONCILIATION_REQUIRED,
                        message_id=None,
                        cause_category=(
                            ReconciliationCauseCategory.ACCOUNT_STREAM_UNKNOWN_OR_EXTERNAL_EXECUTION
                        ),
                    )
                else:
                    self._record_reconciliation_cause_locked(
                        ReconciliationCauseCategory.ACCOUNT_STREAM_UNKNOWN_OR_EXTERNAL_EXECUTION
                    )
                return False  # 알 수 없는 앱 주문은 새 aggregate를 추측하지 않고 REST 재조정을 요구한다.
            if not self._context.initialized:
                self._record_reconciliation_cause_locked(
                    ReconciliationCauseCategory.ACCOUNT_STREAM_UNKNOWN_OR_EXTERNAL_EXECUTION
                )
                self._stream_reconciliation_required = True
                return False  # startup 중 event는 history를 읽은 뒤 REST authoritative query로 복구한다.

            # 같은 application lock에서 Order, Position, history와 internal outcome 순서를 보존한다.
            initial_result = state.order.status is None
            outcomes = self._handle_order_result(
                state,
                result,
                initial=initial_result,
            )
            if outcomes:
                self._enqueue_order_outcomes(outcomes)

            return True  # 중복 fill은 Order key가 제거해도 관찰한 same-order 결과에는 True를 반환한다.

    def mark_account_stream_reconciliation_required(self, reason: str) -> None:
        """
        함수 이름: mark_account_stream_reconciliation_required()
        기능: account stream 종료 시 신규 주문을 잠그고 full REST 재조정을 요구한다.
        인자: reason -> Gateway가 만든 credential 없는 typed 종료 사유
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # Callback은 raw close frame이나 예외 문자열 대신 공백 없는 안전한 사유만 받는다.
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be a non-empty string")

        with self._session_lock:
            self._stream_reconciliation_required = True
            self._account_subscription = None
            self._record_diagnostic("stream_unavailable", level="WARNING", stream="account", reason=normalize_stream_reason(reason))
            if sys.exception() is not None:
                self._diagnostics.record_exception("account_stream", sys.exception(), session_id=self._session_id)
            if self._is_active_locked():
                self._enter_order_reconciliation(
                    None,
                    OrderExecutionFailureCode.STREAM_RECONCILIATION_REQUIRED,
                    message_id=None,
                    cause_category=(
                        ReconciliationCauseCategory.ACCOUNT_STREAM_UNKNOWN_OR_EXTERNAL_EXECUTION
                    ),
                )  # 실행 중 disconnect는 Context와 공개 status도 동시에 잠근다.
            else:
                self._record_reconciliation_cause_locked(
                    ReconciliationCauseCategory.ACCOUNT_STREAM_UNKNOWN_OR_EXTERNAL_EXECUTION
                )  # 시작 전 blocker도 원인 없는 MISSING으로 남기지 않는다.

    def mark_market_stream_reconciliation_required(self, reason: str) -> None:
        """
        함수 이름: mark_market_stream_reconciliation_required()
        기능: Kline 세대가 사용 불가한 사실을 기록하고 신규 시장·주문 effect를 full-resync까지 잠근다.
        인자: reason -> credential 없는 안정적 시장 stream 상태 분류
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        if not isinstance(reason, str):
            raise TypeError("reason must be a string")
        if not reason or reason != reason.strip():
            raise ValueError(
                "reason must be non-empty without outer whitespace"
            )

        with self._session_lock:
            # 최초 RUNNING 중단만 기억해 STOPPING 또는 다른 reconciliation을 자동 재개하지 않는다.
            self._market_stream_monitoring_started = True
            if self._status is TradingSessionStatus.RUNNING:
                self._market_stream_interrupted_running_session = True
            self._market_stream_reconciliation_required = True
            self._record_diagnostic("stream_unavailable", level="WARNING", stream="market", reason=normalize_stream_reason(reason))
            if sys.exception() is not None:
                self._diagnostics.record_exception("market_stream", sys.exception(), session_id=self._session_id)

            # 정상 startup의 initializing gate는 주문을 잠그지만 실패 origin이 아니므로 cause를 만들지 않는다.
            is_pre_session_initialization = (
                self._status is TradingSessionStatus.NOT_STARTED
                and reason == "market_stream_initializing"
            )
            if self._status in (
                TradingSessionStatus.RUNNING,
                TradingSessionStatus.STOPPING,
            ):
                self._enter_order_reconciliation(
                    None,
                    OrderExecutionFailureCode.STREAM_RECONCILIATION_REQUIRED,
                    message_id=None,
                    cause_category=ReconciliationCauseCategory.MARKET_STREAM_FAILED,
                )
            elif not is_pre_session_initialization:
                self._record_reconciliation_cause_locked(
                    ReconciliationCauseCategory.MARKET_STREAM_FAILED
                )  # 시작 전 실패나 이미 잠긴 session의 새 시장 원인도 단일 latch에 기록한다.

            # 전략 timer는 폐기하되 이미 수신한 주문 outcome은 position·history 복구를 위해 보존한다.
            self._scheduler.clear()

    def complete_market_stream_reconciliation(
        self,
        market_version: int,
    ) -> None:
        """
        함수 이름: complete_market_stream_reconciliation()
        기능: 새 live 세대와 REST 평가의 정확한 version을 확인한 뒤 시장 blocker만 해제한다.
        인자: market_version -> full-resync와 REGIME 평가가 공유한 MarketSnapshot version
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        if type(market_version) is not int:
            raise TypeError("market_version must be an exact int")
        if market_version <= 0:
            raise ValueError("market_version must be positive")

        with self._session_lock:
            # Snapshot identity, version과 실제 Gateway live 세대 중 하나라도 어긋나면 gate를 유지한다.
            self._market_stream_monitoring_started = True
            if not self._market_snapshot.ready:
                raise ValueError("MarketSnapshot must be ready after full resync")
            if market_version != self._market_snapshot.version:
                raise ValueError(
                    "market_version must match the authoritative MarketSnapshot"
                )
            if not self._web_socket_gateway.kline_live_ready:
                raise ValueError(
                    "Kline stream must be live before reconciliation completes"
                )

            # Market source 복구 사실만 기록하고 active session의 status·phase 중단 provenance는 보존한다.
            self._market_stream_reconciliation_required = False
            self._record_diagnostic("stream_reconciled", stream="market", restored_market_version=market_version)
            if self._market_stop_requested is not None:
                self.stop_trading(
                    command_id=f"market-recovered-stop-{self._market_stop_requested}",
                    expected_version=self._context.version,
                )  # 사용자가 요청한 STOP만 재개하며 매매를 자동 재시작하지 않는다.


    def reconcile_startup_state(self) -> None:
        """
        함수 이름: reconcile_startup_state()
        기능: history Position, 제출 전 journal, open/recent Binance 주문을 재시작 시 재조정한다.
        인자: 없음
        반환값: 설명 가능한 local·exchange 상태가 모두 복원되면 없음
        작성 날짜: 2026/08/22
        """
        with self._session_lock:
            # 이전 callback이 남긴 process-lifetime blocker는 startup I/O나 완료 flag로 완화하지 않는다.
            if self._process_lifetime_reconciliation_required_locked():
                raise StartupOrderReconciliationError(
                    "process-lifetime reconciliation requires a fresh process"
                )

            # Startup operation은 history와 full account가 준비된 뒤 정확히 한 번만 수행한다.
            if self._startup_reconciliation_complete:
                return  # 성공한 lifecycle의 중복 호출은 외부 조회나 Position 적용을 반복하지 않는다.
            if not self._account.ready:
                raise StartupOrderReconciliationError(
                    "full account snapshot is required before reconciliation"
                )
            if not self._web_socket_gateway.account_ready:
                raise StartupOrderReconciliationError(
                    "account stream must be connected and caught up during reconciliation"
                )

            # Signed stream ACK 뒤 REST 전체 계좌를 한 번 더 읽어 첫 snapshot과 구독 사이 공백을 닫는다.
            try:
                startup_account_snapshot = (
                    self._api_gateway.fetch_account_snapshot(
                        SUPPORTED_VALUATION_ASSET
                    )
                )
                current_price = self._market_snapshot.get_current_eth_price()
                self._account.apply_startup_reconciliation_snapshot(
                    startup_account_snapshot,
                    current_price,
                )
                self._account_free_overlays.clear()
            except Exception as error:
                raise StartupOrderReconciliationError(
                    "startup account stream gap reconciliation failed"
                ) from error
            if not self._web_socket_gateway.account_ready:
                raise StartupOrderReconciliationError(
                    "account stream was not caught up during account reconciliation"
                )

            history_controller = self._require_trade_history_controller()
            history_trades = history_controller.trade_history.trades

            # Durable Trade를 시간순으로 재생해 process memory의 Position을 먼저 복원한다.
            self._restore_position_from_history(history_trades)
            history_trades_by_order_id = {
                trade.order_id: trade for trade in history_trades
            }
            history_trades_by_identity = {
                (trade.client_order_id, trade.order_id): trade
                for trade in history_trades
            }
            history_trades_by_client_id: dict[str, list[Trade]] = {}
            for trade in history_trades:
                history_trades_by_client_id.setdefault(
                    trade.client_order_id,
                    [],
                ).append(trade)
            if (
                self._pending_order_recovery_enabled
                and not history_controller.supports_pending_order_recovery
            ):
                raise StartupOrderReconciliationError(
                    "testnet runtime requires pending-order recovery storage"
                )
            pending_records = (
                history_controller.get_pending_order_recovery_records()
                if self._pending_order_recovery_enabled
                else ()
            )
            if self._pending_order_recovery_enabled:
                # REMOVE된 attempt도 journal에서 복원해 fresh process가 같은 intent 예산을 초기화하지 않게 한다.
                durable_submission_counts = dict(
                    history_controller.get_pending_order_submission_counts()
                )
                self._submission_attempts_by_intent = {
                    intent_id: max(
                        durable_count,
                        self._submission_attempts_by_intent.get(intent_id, 0),
                    )
                    for intent_id, durable_count
                    in durable_submission_counts.items()
                }
            pending_orders = tuple(
                record.order for record in pending_records
            )
            pending_by_client_id = {
                order.client_order_id: order for order in pending_orders
            }
            if len(pending_by_client_id) != len(pending_orders):
                raise StartupOrderReconciliationError(
                    "pending order journal contains duplicate client IDs"
                )

            # Exchange 조회 결과는 앱 prefix 주문만 local durable identity와 대조한다.
            open_results = self._api_gateway.list_open_order_results(
                _TRADING_SYMBOL
            )
            recent_results = self._api_gateway.list_recent_order_results(
                _TRADING_SYMBOL,
                limit=100,
            )
            authoritative_order_results = [
                *open_results,
                *recent_results,
            ]  # REST가 확인한 fill을 새 stream 세대의 누적 기준으로 함께 보존한다.

            # Testnet reset이 재사용한 숫자 ID는 client ID가 다른 durable 주문과 결합하지 않는다.
            for result in authoritative_order_results:
                exchange_order_id = result.exchange_order_id
                if exchange_order_id is None:
                    continue
                durable_trade = history_trades_by_order_id.get(
                    exchange_order_id
                )
                if (
                    durable_trade is not None
                    and durable_trade.client_order_id
                    != result.client_order_id
                ):
                    raise StartupOrderReconciliationError(
                        "Binance reused a durable exchange order ID"
                    )
            unexplained_open_results = tuple(
                result
                for result in open_results
                if result.client_order_id.startswith(
                    APP_CLIENT_ORDER_ID_PREFIX
                )
                and result.client_order_id not in pending_by_client_id
            )
            if unexplained_open_results:
                raise StartupOrderReconciliationError(
                    "Binance has an app-owned open order without a durable journal"
                )

            # Fill이 있는 최근 앱 주문은 history 또는 pending journal 중 하나로 설명돼야 한다.
            for result in recent_results:
                if not result.client_order_id.startswith(
                    APP_CLIENT_ORDER_ID_PREFIX
                ):
                    continue
                if not result.fills:
                    continue
                durable_trade = (
                    history_trades_by_identity.get(
                        (
                            result.client_order_id,
                            result.exchange_order_id,
                        )
                    )
                    if result.exchange_order_id is not None
                    else None
                )
                if durable_trade is not None:
                    if not _order_result_exactly_confirms_trade(
                        result,
                        durable_trade,
                    ):
                        raise StartupOrderReconciliationError(
                            "Binance execution conflicts with durable history"
                        )
                    continue  # 같은 pair와 terminal execution이 모두 맞는 history만 설명 근거다.
                if result.client_order_id not in pending_by_client_id:
                    raise StartupOrderReconciliationError(
                        "Binance has an unexplained app-owned execution"
                    )

            # Journal 각 항목은 신규 submit 없이 같은 client ID의 authoritative 상태만 조회한다.
            self._startup_reconciliation_blocked = False
            for recovery_record in pending_records:
                order = recovery_record.order
                absence_confirms_no_submission = (
                    recovery_record.lifecycle
                    is PendingOrderRecoveryLifecycle.SUBMISSION_REJECTED_CONFIRMED
                    or (
                        recovery_record.lifecycle
                        is PendingOrderRecoveryLifecycle.PREPARED
                        and recovery_record.submission_provenance
                        is PendingOrderSubmissionProvenance.SUBMITTED_FSYNC_PRECEDES_REST_POST
                    )
                )  # V3 PREPARED와 명시적 거부만 bounded absence를 미제출 증거로 승격한다.
                result = self._query_pending_order_during_startup(
                    order,
                    absence_confirms_no_submission=(
                        absence_confirms_no_submission
                    ),
                )
                if (
                    result is not None
                    and result.status in ACTIVE_ORDER_STATUSES
                    and self._requires_manual_kill_cleanup_locked()
                ):
                    result = (
                        self._cancel_pending_order_during_manual_kill_startup(
                            order,
                            result,
                        )
                    )  # Durable kill은 active same-ID를 취소·terminal 조회한 뒤에만 Position을 복원한다.
                matching_history_trades = history_trades_by_client_id.get(
                    order.client_order_id,
                    [],
                )
                if result is None:
                    if matching_history_trades:
                        raise StartupOrderReconciliationError(
                            "durable no-submission provenance conflicts with history"
                        )
                    history_controller.delete_pending_order(
                        order.client_order_id
                    )
                    continue  # 네 번의 부재와 durable 미제출 근거를 결합해 신규 POST 없이 정리한다.
                authoritative_order_results.append(result)
                if matching_history_trades:
                    if (
                        recovery_record.lifecycle
                        is PendingOrderRecoveryLifecycle.SUBMISSION_REJECTED_CONFIRMED
                    ):
                        raise StartupOrderReconciliationError(
                            "durable submission rejection conflicts with history"
                        )
                    self._confirm_pending_order_already_in_history(
                        order,
                        result,
                        matching_history_trades,
                    )
                    try:
                        # Phase 9 PREPARED journal도 exact history 대조 후 terminal→commit 증거로 순차 승격한다.
                        if recovery_record.lifecycle is not (
                            PendingOrderRecoveryLifecycle.HISTORY_COMMITTED
                        ):
                            if recovery_record.lifecycle is not (
                                PendingOrderRecoveryLifecycle.TERMINAL
                            ):
                                history_controller.transition_pending_order_lifecycle(
                                    order.client_order_id,
                                    PendingOrderRecoveryLifecycle.TERMINAL,
                                )
                            history_controller.transition_pending_order_lifecycle(
                                order.client_order_id,
                                PendingOrderRecoveryLifecycle.HISTORY_COMMITTED,
                            )
                    except Exception as error:
                        raise StartupOrderReconciliationError(
                            "pending history lifecycle commit failed"
                        ) from error
                    history_controller.delete_pending_order(
                        order.client_order_id
                    )
                    continue  # 같은 exchange execution의 history commit 뒤 남은 REMOVE만 정리한다.
                self._recover_pending_order(
                    order,
                    result,
                    history_trades_by_order_id,
                    recovery_lifecycle=recovery_record.lifecycle,
                )

            # Active/partial 주문은 설명 가능해도 terminal history가 없으므로 이번 process를 READY로 열지 않는다.
            if self._startup_reconciliation_blocked:
                raise StartupOrderReconciliationError(
                    "an app-owned order is still active after startup query"
                )

            # Callback publication 전에 REST 누적 fill을 Gateway에 심어 첫 executionReport gap 오탐을 막는다.
            try:
                self._web_socket_gateway.rebase_order_results(
                    authoritative_order_results
                )
            except Exception as error:
                raise StartupOrderReconciliationError(
                    "startup order stream rebase failed"
                ) from error

            position = self._require_position()
            self._settle_residual_position()
            self._validate_residual_balance()
            self._validate_testnet_position_provenance(
                authoritative_order_results
            )

            # App Position은 account의 현재 ETH 총량보다 클 수 없으며 reset 차이도 자동 보정하지 않는다.
            if position.quantity > self._account.get_holdings(_BASE_ASSET):
                raise StartupOrderReconciliationError(
                    "restored Position exceeds the Binance ETH balance"
                )
            if not self._web_socket_gateway.account_ready:
                raise StartupOrderReconciliationError(
                    "account stream was not caught up during reconciliation"
                )
            self._publish_restored_position_snapshot(position)

            # Startup 내부의 동기 재진입 callback도 READY 직전에 다시 잡아 stream gate를 지우지 않는다.
            if self._process_lifetime_reconciliation_required_locked():
                raise StartupOrderReconciliationError(
                    "process-lifetime reconciliation requires a fresh process"
                )

            # Startup 중 관찰한 event는 위 REST open/recent/query 사실이 대체했으므로 gate를 해제한다.
            self._stream_reconciliation_required = False
            self._startup_reconciliation_complete = True

    def _query_pending_order_during_startup(
        self,
        order: Order,
        *,
        absence_confirms_no_submission: bool,
    ) -> OrderResult | None:
        """
        함수 이름: _query_pending_order_during_startup()
        기능: durable lifecycle 주문을 1·2·4·8초 뒤 같은 ID로 조회해 존재 또는 안전한 미제출을 확정한다.
        인자: order -> 신규 제출 없이 조회할 durable pending Order
            absence_confirms_no_submission -> durable 근거와 bounded absence로 미제출을 확정할지 여부
        반환값: 확인한 concrete OrderResult 또는 근거와 4회 부재가 결합되면 None
        작성 날짜: 2026/08/29
        """
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")
        if type(absence_confirms_no_submission) is not bool:
            raise TypeError("absence_confirms_no_submission must be a bool")

        # Binance의 비동기 Memory=>Database 지연을 고려해 첫 조회도 1초 backoff 뒤 수행한다.
        absent_observation_count = 0
        last_error: BaseException | None = None
        for base_delay in _ORDER_RECONCILIATION_DELAYS:
            selected_delay = self._jittered_order_retry_delay(base_delay)
            try:
                self._order_retry_waiter(selected_delay)
                result = self._api_gateway.query_order_result(order)
            except Exception as error:
                last_error = error
                continue  # 한 transport 실패를 주문 부재로 바꾸지 않고 남은 same-ID 예산을 사용한다.

            if result.status is not OrderStatus.UNKNOWN:
                return result  # concrete active 또는 terminal 사실은 기존 recovery pipeline에 넘긴다.
            if result.failure_kind is OrderResultFailureKind.ORDER_NOT_VISIBLE:
                absent_observation_count += 1

        # V3 PREPARED 경계 또는 durable 거부와 네 번의 정확한 부재를 결합한다.
        if (
            absence_confirms_no_submission
            and absent_observation_count == len(_ORDER_RECONCILIATION_DELAYS)
            and last_error is None
        ):
            return None

        # Legacy PREPARED와 SUBMITTED은 POST 수락 직후 crash를 포함하므로 부재만으로 정리하지 않는다.
        failure_message = (
            "same-order startup query remained not visible"
            if absent_observation_count == len(_ORDER_RECONCILIATION_DELAYS)
            else "same-order startup query remained unknown"
        )
        startup_error = StartupOrderReconciliationError(failure_message)
        if last_error is not None:
            raise startup_error from last_error

        raise startup_error

    def _cancel_pending_order_during_manual_kill_startup(
        self,
        order: Order,
        active_result: OrderResult,
    ) -> OrderResult:
        """
        함수 이름: _cancel_pending_order_during_manual_kill_startup()
        기능: durable kill 재시작에서 app-owned active 주문 하나를 취소하고 같은 ID terminal을 확인한다.
        인자: order -> pending journal이 보존한 app-owned Order
            active_result -> 첫 startup query가 확인한 active OrderResult
        반환값: cancel 뒤 같은 ID에서 확인한 terminal OrderResult
        작성 날짜: 2026/08/29
        """
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")
        if not isinstance(active_result, OrderResult):
            raise TypeError("active_result must be an OrderResult")
        if active_result.status not in ACTIVE_ORDER_STATUSES:
            raise ValueError("active_result must identify an active order")
        if active_result.client_order_id != order.client_order_id:
            raise ValueError("active_result must match the pending order")

        # 개별 app client identity만 취소하고 응답은 성공·실패 어느 쪽도 terminal 사실로 신뢰하지 않는다.
        try:
            self._api_gateway.cancel_order(order)
        except Exception:
            pass  # Binance 5xx·timeout의 UNKNOWN 의미 때문에 반드시 아래 same-ID query로 확정한다.

        # 취소·체결 race와 Memory=>Database 지연을 1·2·4·8초 bounded 조회로 terminal까지 관찰한다.
        last_error: BaseException | None = None
        last_result = active_result
        for base_delay in _ORDER_RECONCILIATION_DELAYS:
            selected_delay = self._jittered_order_retry_delay(base_delay)
            try:
                self._order_retry_waiter(selected_delay)
                queried_result = self._api_gateway.query_order_result(order)
            except Exception as error:
                last_error = error
                continue  # Transport 오류를 취소 완료로 바꾸지 않고 남은 same-ID 조회 예산을 사용한다.

            last_result = queried_result
            if queried_result.status in TERMINAL_ORDER_STATUSES:
                return queried_result

        # Active·UNKNOWN이 남으면 신규 submit이나 Position 청산을 시작하지 않고 startup을 닫는다.
        startup_error = StartupOrderReconciliationError(
            "manual-kill cancel remained non-terminal after same-order queries"
        )
        if last_error is not None:
            raise startup_error from last_error

        raise startup_error from RuntimeError(
            f"last order status was {last_result.status.value}"
        )

    def _confirm_pending_order_already_in_history(
        self,
        order: Order,
        result: OrderResult,
        matching_trades: list[Trade],
    ) -> None:
        """
        함수 이름: _confirm_pending_order_already_in_history()
        기능: history commit 뒤 남은 journal이 정확히 같은 exchange execution인지 확인한다.
        인자: order -> pending journal에서 복원한 원 주문 의도
            result -> 같은 client ID로 조회한 현재 거래소 결과
            matching_trades -> 같은 client ID를 가진 durable Trade 목록
        반환값: 동일 terminal execution이면 없음
        작성 날짜: 2026/08/23
        """
        # 과거 ID 재사용이나 손상으로 여러 durable row가 상관되면 임의 하나를 선택하지 않는다.
        if len(matching_trades) != 1:
            raise StartupOrderReconciliationError(
                "pending client order ID matches multiple durable trades"
            )
        durable_trade = matching_trades[0]
        if (
            result.exchange_order_id != durable_trade.order_id
            or result.status not in TERMINAL_ORDER_STATUSES
        ):
            raise StartupOrderReconciliationError(
                "pending journal conflicts with durable exchange identity"
            )

        # 같은 pair라도 reset 전후 누적 fill이 다르면 stale REMOVE 실패로 간주하지 않는다.
        if not _order_result_exactly_confirms_trade(result, durable_trade):
            raise StartupOrderReconciliationError(
                "pending journal execution conflicts with durable trade"
            )

        # Journal intent와 durable 전략 의미가 같아도 실제 누적 fill까지 일치해야 REMOVE 실패로 본다.
        if (
            order.symbol != durable_trade.symbol
            or order.side is not durable_trade.side
            or order.strategy is not durable_trade.strategy
            or order.regime_type is not durable_trade.regime_type
            or order.requested_quantity != durable_trade.requested_quantity
            or order.exit_reason is not durable_trade.exit_reason
        ):
            raise StartupOrderReconciliationError(
                "pending journal metadata conflicts with durable trade"
            )
        try:
            order.apply_order_result(result)
            summary = order.build_execution_summary()
        except Exception as error:
            raise StartupOrderReconciliationError(
                "pending journal result cannot confirm durable trade"
            ) from error
        if (
            summary.executed_quantity != durable_trade.executed_quantity
            or summary.executed_amount != durable_trade.executed_amount
            or summary.average_fill_price != durable_trade.average_fill_price
            or summary.fee_amount != durable_trade.fee_amount
            or summary.fee_asset != durable_trade.fee_asset
            or summary.fee_quote_amount != durable_trade.fee_quote_amount
            or summary.executed_at != durable_trade.executed_at
            or (durable_trade.schema_version == 3 and {fill.key: fill for fill in summary.fills} != {fill.key: fill for fill in durable_trade.fee_fills})
        ):
            raise StartupOrderReconciliationError(
                "pending journal execution conflicts with durable trade"
            )

    @property
    def residual_totals(self) -> tuple[Decimal, Decimal]:
        """
        함수 이름: residual_totals()
        기능: 전략 Position과 별개인 잔여 ETH·원가를 공개한다.
        인자: 없음
        반환값: 수량과 미실현 원가
        작성 날짜: 2026/09/08
        """
        return (Decimal("0"), Decimal("0")) if self._residual_settlement is None else self._residual_settlement.totals

    def _validate_residual_balance(self) -> None:
        """
        함수 이름: _validate_residual_balance()
        기능: 잔여가 있는 전용 계좌는 전략과 잔여의 합이 실제 ETH와 같은지 검사한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        residual_quantity = self.residual_totals[0]
        if residual_quantity > 0:
            with localcontext() as context:
                context.prec = 34
                total = self._require_position().quantity + residual_quantity
            if total != self._account.get_holdings(_BASE_ASSET):
                raise StartupOrderReconciliationError("residual ledger differs from exchange ETH balance")

    def _allows_residual_rounding(self, requested: Decimal, submitted: Decimal) -> bool:
        """
        함수 이름: _allows_residual_rounding()
        기능: live의 ETH 수수료 lot 전량 매도에만 sub-step 내림을 허용한다.
        인자: requested -> 전량, submitted -> filter 후 수량
        반환값: 승인된 잔여 정책의 준비 조건을 만족하면 True
        작성 날짜: 2026/09/08
        """
        if self._residual_settlement is None or not 0 < submitted < requested:
            return False
        rules = self._api_gateway.fetch_symbol_trading_rules(_TRADING_SYMBOL)
        # 전량 매도 전·후 양쪽에서 현재 lot의 durable ETH fee 출처를 확인한다.
        return self._residual_settlement.allows_rounding(
            self._require_position(), self._require_trade_history_controller().trade_history.trades,
            submitted, rules.lot_size.step_size,
        )

    def _settle_residual_position(self) -> None:
        """
        함수 이름: _settle_residual_position()
        기능: terminal durable history에 결속한 잔여만 저장하고 Context를 다시 게시한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        if self._residual_settlement is None or self._require_position().quantity <= 0:
            return
        rules = self._api_gateway.fetch_symbol_trading_rules(_TRADING_SYMBOL)
        history = self._require_trade_history_controller().trade_history.trades
        if self._residual_settlement.settle(self._require_position(), history, rules.lot_size.step_size):
            self._publish_restored_position_snapshot(self._require_position())

    def _restore_position_from_history(
        self,
        trades: tuple[Trade, ...],
    ) -> None:
        """
        함수 이름: _restore_position_from_history()
        기능: 검증된 durable Trade tuple을 새 Position에 순서대로 재생한다.
        인자: trades -> TradeHistoryController가 publish한 immutable 거래 tuple
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # Position 재생은 빈 process state에서만 허용해 같은 fill의 이중 적용을 차단한다.
        if not isinstance(trades, tuple) or any(
            not isinstance(trade, Trade) for trade in trades
        ):
            raise TypeError("trades must be a tuple of Trade values")
        position = self._require_position()
        if position.quantity != Decimal("0"):
            raise StartupOrderReconciliationError(
                "startup Position must be empty before history replay"
            )

        # Trade JSONL 원본 순서가 average-cost 적용 순서이므로 재정렬하지 않는다.
        try:
            if self._residual_settlement is not None:
                self._residual_settlement.restore(
                    position, trades,
                    self._api_gateway.fetch_symbol_trading_rules(_TRADING_SYMBOL).lot_size.step_size if trades else None,
                )
            else:
                for trade in trades:
                    position.apply_historical_trade(trade)
            position.require_history_accounting_compatibility()
        except LegacyFeeAccountingMigrationRequiredError:
            raise  # 열린 v1 base-fee lot은 일반 복원 오류로 지우지 않고 운영 migration code를 보존한다.
        except Exception as error:
            raise StartupOrderReconciliationError(
                "durable history cannot reconstruct Position"
            ) from error

    def _recover_pending_order(
        self,
        order: Order,
        result: OrderResult,
        history_trades_by_order_id: dict[str, Trade],
        *,
        recovery_lifecycle: PendingOrderRecoveryLifecycle,
    ) -> None:
        """
        함수 이름: _recover_pending_order()
        기능: 같은 client ID query 결과의 누락 fill을 Position·history에 멱등 복구한다.
        인자: order -> 제출 전 durable journal에서 복원한 Order
            result -> Binance REST가 반환한 같은 주문의 최신 결과
            history_trades_by_order_id -> exchange order ID별 durable Trade index
            recovery_lifecycle -> startup replay가 확인한 마지막 durable lifecycle
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        # Journal Order와 query 결과 상관관계를 aggregate 적용 전에 다시 검증한다.
        if not isinstance(order, Order) or not isinstance(result, OrderResult):
            raise TypeError("order and result must use canonical domain types")
        if not isinstance(
            recovery_lifecycle,
            PendingOrderRecoveryLifecycle,
        ):
            raise TypeError(
                "recovery_lifecycle must be a PendingOrderRecoveryLifecycle"
            )
        if result.client_order_id != order.client_order_id:
            raise StartupOrderReconciliationError(
                "startup query returned a different client order ID"
            )
        exchange_order_id = result.exchange_order_id
        if (
            exchange_order_id is not None
            and exchange_order_id in history_trades_by_order_id
        ):
            raise StartupOrderReconciliationError(
                "startup query reused a durable exchange order ID"
            )  # Position 적용과 journal 삭제 전에 Testnet reset의 숫자 ID 충돌을 차단한다.
        state = _OrderExecutionState(
            order=order,
            force_sell=False,
            pending_recovery_pending=True,
            recovery_lifecycle=recovery_lifecycle,
            submission_rejection_confirmable=(
                recovery_lifecycle
                is PendingOrderRecoveryLifecycle.SUBMISSION_REJECTED_CONFIRMED
            ),
        )  # 이 state는 durable pending record에서 왔으므로 REMOVE 성공 전까지 해제할 수 없다.
        try:
            order.apply_order_result(result)
        except Exception as error:
            raise StartupOrderReconciliationError(
                "startup order result conflicts with durable intent"
            ) from error

        # Query가 증명한 누적 상태를 Position mutation 전 sidecar에 먼저 fsync한다.
        observed_lifecycle = self._select_pending_order_lifecycle(
            state,
            result,
        )
        if not self._transition_pending_order_recovery(
            state,
            observed_lifecycle,
        ):
            raise StartupOrderReconciliationError(
                "startup pending lifecycle transition failed"
            )

        # Query가 확인한 실제 fill은 history 저장 여부와 무관하게 Position에 먼저 한 번 적용한다.
        if order.fills:
            try:
                summary = order.build_execution_summary(
                    require_terminal=order.is_terminal
                )
                position = self._require_position()
                if order.side is OrderSide.SELL:
                    state.allocated_cost_basis = position.get_cost_basis(
                        summary.executed_quantity
                    )
                position.apply_execution(summary)
                order.mark_fills_applied(order.fills)
                if order.is_terminal:
                    state.terminal_summary = summary
            except Exception as error:
                raise StartupOrderReconciliationError(
                    "startup fill cannot be applied to Position"
                ) from error

        # Terminal 주문은 누락 history를 저장한 뒤 journal을 제거하고 active index에 남기지 않는다.
        history_controller = self._require_trade_history_controller()
        if order.is_terminal:
            exchange_order_id = order.exchange_order_id
            if exchange_order_id is None:
                raise StartupOrderReconciliationError(
                    "terminal startup order is missing exchange order ID"
                )
            if order.fills:
                summary = state.terminal_summary
                if summary is None:
                    raise StartupOrderReconciliationError(
                        "terminal startup execution is missing its summary"
                    )
                durable_trade = history_controller.record_order_execution(
                    order,
                    summary,
                    (
                        state.allocated_cost_basis
                        if order.side is OrderSide.SELL
                        else None
                    ),
                )
                history_trades_by_order_id[
                    exchange_order_id
                ] = durable_trade  # 다음 pending record도 같은 숫자 ID를 재사용하지 못하게 한다.
                if not self._transition_pending_order_recovery(
                    state,
                    PendingOrderRecoveryLifecycle.HISTORY_COMMITTED,
                ):
                    raise StartupOrderReconciliationError(
                        "startup history lifecycle commit failed"
                    )
            history_controller.delete_pending_order(order.client_order_id)
            return

        # 설명 가능한 active/partial 주문은 복원하되 새 session과 신규 submit을 계속 차단한다.
        self._order_states_by_client_id[order.client_order_id] = state
        if order.exchange_order_id is not None:
            self._order_states_by_order_id[order.exchange_order_id] = state
        self._record_reconciliation_cause_locked(
            ReconciliationCauseCategory.ORDER_OR_PERSISTENCE_AMBIGUOUS
        )
        self._startup_reconciliation_blocked = True

    def _publish_restored_position_snapshot(self, position: Position) -> None:
        """
        함수 이름: _publish_restored_position_snapshot()
        기능: startup Position state를 아직 초기화 전인 Context의 start prerequisite로 복사한다.
        인자: position -> history와 pending fill 재생을 마친 authoritative Position
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # Context를 초기화하지 않고 이후 start Guard가 읽는 immutable snapshot만 교체한다.
        position_state = position.get_snapshot()
        self._position_snapshot = PositionSnapshot(
            quantity=position_state.quantity,
            entry_price=(
                position_state.average_entry_price
                if position_state.quantity > Decimal("0")
                else None
            ),
        )  # 선택 REGIME 없이 startup 중 owner를 추측해 Context에 쓰지 않는다.

    def _validate_testnet_position_provenance(
        self,
        authoritative_results: list[OrderResult],
    ) -> None:
        """
        함수 이름: _validate_testnet_position_provenance()
        기능: 열린 local Position의 최신 BUY가 현재 Testnet order history에 남아 있는지 검증한다.
        인자: authoritative_results -> open/recent/same-ID REST reconciliation 결과
        반환값: provenance가 설명되거나 testnet recovery가 아니면 없음
        작성 날짜: 2026/08/22
        """
        if not isinstance(authoritative_results, list) or any(
            not isinstance(result, OrderResult)
            for result in authoritative_results
        ):
            raise TypeError(
                "authoritative_results must be a list of OrderResult values"
            )

        # Pending-order 내구성을 강제하는 Testnet runtime의 열린 Position에만 reset 검사를 적용한다.
        position = self._require_position()
        if (
            not self._pending_order_recovery_enabled
            or position.quantity == Decimal("0")
        ):
            return
        durable_trades = self._require_trade_history_controller().trade_history.trades
        latest_buy_trade = next(
            (
                trade
                for trade in reversed(durable_trades)
                if trade.side is OrderSide.BUY
            ),
            None,
        )
        if (
            latest_buy_trade is None
            or not any(
                _order_result_exactly_confirms_trade(
                    result,
                    latest_buy_trade,
                )
                for result in authoritative_results
            )
        ):
            raise StartupOrderReconciliationError(
                "testnet reset or open-position order provenance is missing"
            )

    def reconnect_account_stream_after_reconciliation(
        self,
        *,
        recovery_commit_observer: Callable[[], object] | None = None,
    ) -> Subscription:
        """
        함수 이름: reconnect_account_stream_after_reconciliation()
        기능: disconnect 뒤 account·open order를 REST 재조정하고 새 user stream 세대를 연다.
        인자: recovery_commit_observer -> gate 재개와 같은 lock에서 호출할 optional application hook
        반환값: full reconciliation 뒤 시작한 새 account subscription
        작성 날짜: 2026/08/22
        """
        # Optional hook은 재조정 mutation을 시작하기 전에 검증해 잘못된 caller를 fail fast한다.
        if recovery_commit_observer is not None and not callable(
            recovery_commit_observer
        ):
            raise TypeError("recovery_commit_observer must be callable or None")

        with self._session_lock:
            # Parent/runtime ownership을 잃은 process는 network snapshot으로 스스로 gate를 다시 열지 않는다.
            if self._process_ownership_ambiguous:
                raise AccountStreamRecoveryBlockedError(
                    "process ownership is ambiguous"
                )
            if self._external_execution_reconciliation_required:
                raise AccountStreamRecoveryBlockedError(
                    "external account execution requires fresh-process reconciliation"
                )  # App-prefix 조회만 수행하는 자동 recovery로 외부 주문을 설명했다고 주장하지 않는다.

            # Reconnect operation 전체에서 command gate를 닫고 REST full snapshot부터 다시 적용한다.
            self._stream_reconciliation_required = True
            account_snapshot = self._api_gateway.fetch_account_snapshot(
                SUPPORTED_VALUATION_ASSET
            )
            try:
                current_price = self._market_snapshot.get_current_eth_price()
                self._account.apply_reconciliation_snapshot(
                    account_snapshot,
                    current_price,
                )
            except Exception as error:
                raise AccountStreamRecoveryBlockedError(
                    "account stream recovery account invariant failed"
                ) from error
            self._account_free_overlays.clear()

            # 첫 full account 직후 signed stream ACK를 열어 이후 order REST의 체결 공백을 닫는다.
            subscription = self._web_socket_gateway.start_account_info_stream()
            try:
                # Exchange open order가 현재 memory의 app-owned state로 모두 설명되는지 먼저 확인한다.
                open_results = self._api_gateway.list_open_order_results(
                    _TRADING_SYMBOL
                )
                open_app_order_results = tuple(
                    result
                    for result in open_results
                    if result.client_order_id.startswith(
                        APP_CLIENT_ORDER_ID_PREFIX
                    )
                )
                recent_app_order_results = tuple(
                    result
                    for result in self._api_gateway.list_recent_order_results(
                        _TRADING_SYMBOL,
                        limit=100,
                    )
                    if result.client_order_id.startswith(
                        APP_CLIENT_ORDER_ID_PREFIX
                    )
                )
                authoritative_order_results = [
                    *open_app_order_results,
                    *recent_app_order_results,
                ]  # 완료된 durable BUY도 recent app order에서 provenance 기준으로 유지한다.

                # Memory index보다 sidecar identity를 먼저 replay해 fsync 후 process gap에서도 새 ID를 만들지 않는다.
                history_controller = self._require_trade_history_controller()
                pending_records = (
                    history_controller.get_pending_order_recovery_records()
                    if self._pending_order_recovery_enabled
                    else ()
                )
                if self._pending_order_recovery_enabled:
                    for intent_id, durable_count in (
                        history_controller.get_pending_order_submission_counts()
                    ):
                        self._submission_attempts_by_intent[intent_id] = max(
                            durable_count,
                            self._submission_attempts_by_intent.get(
                                intent_id,
                                0,
                            ),
                        )
                    for recovery_record in pending_records:
                        recovered_order = recovery_record.order
                        if (
                            recovered_order.client_order_id
                            in self._order_states_by_client_id
                        ):
                            continue
                        recovered_state = _OrderExecutionState(
                            order=recovered_order,
                            force_sell=False,
                            pending_recovery_pending=True,
                            recovery_lifecycle=recovery_record.lifecycle,
                            submission_rejection_confirmable=(
                                recovery_record.lifecycle
                                is PendingOrderRecoveryLifecycle.SUBMISSION_REJECTED_CONFIRMED
                            ),
                        )
                        self._order_states_by_client_id[
                            recovered_order.client_order_id
                        ] = recovered_state  # 복원 state는 아래 same-ID query 외의 submit 경로를 갖지 않는다.
                known_client_order_ids = set(self._order_states_by_client_id)
                if any(
                    result.client_order_id not in known_client_order_ids
                    or (
                        self._order_states_by_client_id[
                            result.client_order_id
                        ].order.exchange_order_id
                        not in (None, result.exchange_order_id)
                    )
                    for result in open_app_order_results
                ):
                    raise AccountStreamRecoveryBlockedError(
                        "stream reconnect found an unexplained open order"
                    )

                # 단절 중 다른 process가 만든 app-prefix 체결도 local Position/history 없이 통과시키지 않는다.
                durable_trades = history_controller.trade_history.trades
                durable_trades_by_identity = {
                    (trade.client_order_id, trade.order_id): trade
                    for trade in durable_trades
                }
                durable_trades_by_order_id = {
                    trade.order_id: trade for trade in durable_trades
                }
                for result in authoritative_order_results:
                    exchange_order_id = result.exchange_order_id
                    if exchange_order_id is None:
                        continue
                    durable_trade = durable_trades_by_order_id.get(
                        exchange_order_id
                    )
                    if (
                        durable_trade is not None
                        and durable_trade.client_order_id
                        != result.client_order_id
                    ):
                        raise AccountStreamRecoveryBlockedError(
                            "stream reconnect found a reused exchange order ID"
                        )
                if self._pending_order_recovery_enabled:
                    pending_records_by_client_id = {
                        record.order.client_order_id: record
                        for record in pending_records
                    }
                    for state in self._order_states_by_client_id.values():
                        recovery_record = pending_records_by_client_id.get(
                            state.order.client_order_id
                        )
                        if recovery_record is not None:
                            state.pending_recovery_pending = True
                            state.recovery_lifecycle = (
                                recovery_record.lifecycle
                            )
                            state.submission_rejection_confirmable = (
                                recovery_record.lifecycle
                                is PendingOrderRecoveryLifecycle.SUBMISSION_REJECTED_CONFIRMED
                            )  # Sidecar replay를 in-memory marker보다 우선해 REMOVE 실패를 다시 복구한다.
                        # 기존 marker가 있는데 disk record가 없으면 REMOVE 성공·실패 경계를 추측하지 않는다.
                        elif state.pending_recovery_pending:
                            continue  # Terminal completion을 명시적으로 재실행하기 전까지 재연결 gate를 유지한다.
                for result in recent_app_order_results:
                    if not result.fills:
                        continue
                    current_state = self._order_states_by_client_id.get(
                        result.client_order_id
                    )
                    state_explains_result = (
                        current_state is not None
                        and (
                            current_state.order.status is None
                            or current_state.order.status
                            is OrderStatus.UNKNOWN
                            or current_state.order.status
                            in ACTIVE_ORDER_STATUSES
                            or current_state.persistence_pending
                            or current_state.pending_recovery_pending
                        )
                        and (
                            current_state.order.exchange_order_id is None
                            or current_state.order.exchange_order_id
                            == result.exchange_order_id
                        )
                    )
                    durable_trade = (
                        durable_trades_by_identity.get(
                            (
                                result.client_order_id,
                                result.exchange_order_id,
                            )
                        )
                        if result.exchange_order_id is not None
                        else None
                    )
                    history_explains_result = (
                        durable_trade is not None
                        and _order_result_exactly_confirms_trade(
                            result,
                            durable_trade,
                        )
                    )
                    if not state_explains_result and not history_explains_result:
                        raise AccountStreamRecoveryBlockedError(
                            "stream reconnect found an unexplained recent execution"
                        )

                # 현재 process가 소유한 unresolved 주문은 모두 같은 ID query로 최신 fill을 보강한다.
                outcomes: list[TradingEvent] = []
                for state in tuple(self._order_states_by_client_id.values()):
                    order = state.order
                    order_is_unresolved = (
                        order.status is None
                        or order.status is OrderStatus.UNKNOWN
                        or order.status in ACTIVE_ORDER_STATUSES
                        or state.persistence_pending
                        or state.pending_recovery_pending
                    )
                    if not order_is_unresolved:
                        continue
                    if not self._context.initialized:
                        raise AccountStreamRecoveryBlockedError(
                            "recovered startup order requires process restart"
                        )
                    if (
                        state.recovery_lifecycle
                        is PendingOrderRecoveryLifecycle.SUBMISSION_REJECTED_CONFIRMED
                    ):
                        result = self._query_pending_order_during_startup(
                            order,
                            absence_confirms_no_submission=True,
                        )
                        if result is None:
                            # Durable rejection와 네 번의 exact absence만 active lock을 해제하고 같은 intent 예산은 유지한다.
                            history_controller.delete_pending_order(
                                order.client_order_id
                            )
                            state.pending_recovery_pending = False
                            self._scheduled_order_queries.pop(
                                order.client_order_id,
                                None,
                            )
                            self._context.update_pending_order(
                                None,
                                preserve_intent_id=not state.force_sell,
                            )
                            failure_event = self._create_order_outcome_event(
                                state,
                                succeeded=False,
                            )
                            state.pending_outcome = failure_event
                            outcomes.append(failure_event)
                            continue
                    else:
                        result = self._api_gateway.query_order_result(order)
                    if result.status is OrderStatus.UNKNOWN:
                        raise StartupOrderReconciliationError(
                            "stream reconnect order query remained unknown"
                        )
                    if result.exchange_order_id is not None:
                        durable_trade = durable_trades_by_order_id.get(
                            result.exchange_order_id
                        )
                        if (
                            durable_trade is not None
                            and durable_trade.client_order_id
                            != result.client_order_id
                        ):
                            raise AccountStreamRecoveryBlockedError(
                                "stream query reused a durable exchange order ID"
                            )
                    authoritative_order_results.append(result)
                    try:
                        outcomes.extend(
                            self._handle_order_result(
                                state,
                                result,
                                initial=order.status is None,
                            )
                        )
                    except Exception as error:
                        raise AccountStreamRecoveryBlockedError(
                            "account stream recovery order invariant failed"
                        ) from error
                if outcomes:
                    try:
                        self._enqueue_order_outcomes(outcomes)
                    except Exception as error:
                        raise AccountStreamRecoveryBlockedError(
                            "account stream recovery outcome invariant failed"
                        ) from error

                # Terminal fill은 memory state만으로 설명하지 않고 새 durable snapshot과 정확히 대조한다.
                refreshed_durable_trades = (
                    history_controller.trade_history.trades
                )
                refreshed_trades_by_identity = {
                    (trade.client_order_id, trade.order_id): trade
                    for trade in refreshed_durable_trades
                }
                for result in authoritative_order_results:
                    if (
                        not result.fills
                        or result.status not in TERMINAL_ORDER_STATUSES
                    ):
                        continue
                    durable_trade = (
                        refreshed_trades_by_identity.get(
                            (
                                result.client_order_id,
                                result.exchange_order_id,
                            )
                        )
                        if result.exchange_order_id is not None
                        else None
                    )
                    if (
                        durable_trade is None
                        or not _order_result_exactly_confirms_trade(
                            result,
                            durable_trade,
                        )
                    ):
                        raise AccountStreamRecoveryBlockedError(
                            "terminal execution is not exact durable history"
                        )

                # Reset 뒤 새 allowance를 과거 Position으로 오인하지 않도록 ACK 아래에서 provenance를 확인한다.
                try:
                    self._validate_testnet_position_provenance(
                        authoritative_order_results
                    )
                except Exception as error:
                    raise AccountStreamRecoveryBlockedError(
                        "account stream recovery position provenance failed"
                    ) from error

                # REST 잔고보다 큰 local Position은 reset 또는 외부 거래 충돌이므로 재시도로 보정하지 않는다.
                try:
                    position = self._require_position()
                    position_exceeds_balance = (
                        position.quantity
                        > self._account.get_holdings(_BASE_ASSET)
                    )
                except Exception as error:
                    raise AccountStreamRecoveryBlockedError(
                        "account stream recovery Position invariant failed"
                    ) from error
                if position_exceeds_balance:
                    raise AccountStreamRecoveryBlockedError(
                        "account stream recovery Position exceeds Binance balance"
                    )

                # ACK 뒤 조회한 REST fill 기준을 stream 누적기에 병합해 gap 체결을 검증한다.
                try:
                    self._web_socket_gateway.rebase_order_results(
                        authoritative_order_results
                    )
                except Exception as error:
                    raise AccountStreamRecoveryBlockedError(
                        "account stream recovery order rebase conflict"
                    ) from error

                # 첫 REST와 signed stream ACK 사이의 balance gap은 두 번째 full snapshot으로 닫는다.
                post_subscribe_snapshot = (
                    self._api_gateway.fetch_account_snapshot(
                        SUPPORTED_VALUATION_ASSET
                    )
                )
                try:
                    self._account.apply_startup_reconciliation_snapshot(
                        post_subscribe_snapshot,
                        current_price,
                    )
                except Exception as error:
                    raise AccountStreamRecoveryBlockedError(
                        "account stream recovery gap snapshot invariant failed"
                    ) from error
                self._account_free_overlays.clear()  # 두 번째 full account가 재연결 중 fill 보정을 대체한다.
                self._validate_residual_balance()
                if position.quantity > self._account.get_holdings(_BASE_ASSET):
                    raise AccountStreamRecoveryBlockedError(
                        "account stream recovery gap Position exceeds Binance balance"
                    )  # ACK 공백에서 감소한 잔고도 command gate를 다시 열기 전에 확인한다.
                if not self._web_socket_gateway.account_ready:
                    raise StartupOrderReconciliationError(
                        "account stream was not caught up during reconnect gap reconciliation"
                    )

                pending_recovery_records_remain = False
                if self._pending_order_recovery_enabled:
                    pending_recovery_records_remain = bool(
                        history_controller.get_pending_order_recovery_records()
                    )  # 실제 sidecar가 비어야 memory marker 손상도 gate를 열 수 없다.
                unresolved_state_remains = pending_recovery_records_remain or any(
                    state.order.status is None
                    or state.order.status is OrderStatus.UNKNOWN
                    or state.order.status in ACTIVE_ORDER_STATUSES
                    or state.persistence_pending
                    or state.pending_recovery_pending
                    for state in self._order_states_by_client_id.values()
                )
                if (
                    self._status is TradingSessionStatus.RECONCILIATION_REQUIRED
                    and not unresolved_state_remains
                    and not self._market_stream_reconciliation_required
                    and self._context.initialized
                ):
                    try:
                        if (
                            self._requires_manual_kill_cleanup_locked()
                            and self._recovered_position_liquidation_session
                        ):
                            self._context.apply_runtime_patch(
                                patch(trading_phase=TradingPhase.STOPPING)
                            )
                            self._status = TradingSessionStatus.STOPPING
                        else:
                            self._context.apply_runtime_patch(
                                patch(trading_phase=TradingPhase.IDLE)
                            )
                            if self._requires_manual_kill_cleanup_locked():
                                if outcomes:
                                    self._status = (
                                        TradingSessionStatus.RECONCILIATION_REQUIRED
                                    )
                                else:
                                    self._status = TradingSessionStatus.RUNNING
                            else:
                                self._status = TradingSessionStatus.RUNNING
                    except Exception as error:
                        raise AccountStreamRecoveryBlockedError(
                            "account stream recovery Context rebase conflict"
                        ) from error
            except Exception:
                # ACK 이후 어느 REST·order·rebase 단계가 실패해도 새 handle을 닫고 gate를 유지한다.
                subscription.close()
                self._account_subscription = None
                raise

            # Context rebase까지 성공한 뒤에만 새 handle을 publish하고 command gate를 다시 연다.
            self._account_subscription = subscription
            self._stream_reconciliation_required = unresolved_state_remains
            if self._requires_manual_kill_cleanup_locked():
                self._begin_manual_kill_cleanup_locked(
                    self._manual_kill_cleanup_command_id_locked(),
                    account_reconciliation_complete=True,
                )  # Fresh reconnect는 active 주문의 same-ID cancel까지 재개하고 terminal 뒤에만 STOP한다.
            if recovery_commit_observer is not None:
                try:
                    recovery_commit_observer()
                except Exception:
                    # Application publication 실패도 열린 backend gate로 남지 않게 같은 lock에서 닫는다.
                    if not self._event_runtime_failed:
                        self.mark_event_runtime_failed()
                    raise

            return subscription  # Unresolved 주문은 새 stream을 유지하되 다음 reconciliation까지 gate를 잠근다.

    def _validate_recovered_position_liquidation_prerequisites(
        self,
    ) -> tuple[RegimeType, StrategyType, PositionSnapshot]:
        """
        함수 이름: _validate_recovered_position_liquidation_prerequisites()
        기능: 복구 Position 청산의 lifecycle, gate, pending, provenance와 전량 주문 가능성을 검증한다.
        인자: 없음
        반환값: 검증된 REGIME, Position owner와 authoritative PositionSnapshot tuple
        작성 날짜: 2026/08/24
        """
        # 복구 Operation은 pending journal을 강제하는 runtime에서 startup 완료 뒤에만 허용한다.
        if not self._pending_order_recovery_enabled:
            raise TradingSessionError(
                TradingSessionFailureCode.COMMAND_DISABLED,
                "Recovered-position liquidation is not enabled",
                current_version=self._context.version,
            )
        if not self._startup_reconciliation_complete:
            raise TradingSessionError(
                TradingSessionFailureCode.POSITION_RECONCILIATION_REQUIRED,
                "Startup position reconciliation is incomplete",
                current_version=self._context.version,
            )
        if (
            self._startup_reconciliation_blocked
            or self._stream_reconciliation_required
            or not self._market_stream_ready
            or self._event_runtime_failed
            or self._process_ownership_ambiguous
            or self._external_execution_reconciliation_required
        ):
            raise TradingSessionError(
                TradingSessionFailureCode.POSITION_RECONCILIATION_REQUIRED,
                "Order or account stream reconciliation blocks liquidation",
                current_version=self._context.version,
            )
        if not self._mode_command_enabled:
            raise TradingSessionError(
                TradingSessionFailureCode.COMMAND_DISABLED,
                "Trading commands are disabled by the selected execution mode",
                current_version=self._context.version,
            )

        # 정상 session 자원이나 staged UI REGIME이 있으면 복구 provenance로 덮지 않는다.
        if (
            self._status is not TradingSessionStatus.NOT_STARTED
            or self._session_id is not None
            or self._active_stm is not None
            or self._event_queue is not None
            or self._event_processor is not None
            or self._context.initialized
            or self._selected_regime is not None
            or self._selected_stm is not None
        ):
            raise TradingSessionError(
                TradingSessionFailureCode.INVALID_SESSION_STATE,
                "Recovery liquidation requires a pristine NOT_STARTED session",
                current_version=self._context.version,
            )
        if self._cleanup_in_progress or self._session_subscriptions:
            raise TradingSessionError(
                TradingSessionFailureCode.INVALID_SESSION_STATE,
                "Recovery liquidation session resources are not pristine",
                current_version=self._context.version,
            )

        # Account·시장·signed stream이 모두 같은 startup 세대로 준비돼야 외부 SELL을 허용한다.
        if not self._market_snapshot.ready:
            raise TradingSessionError(
                TradingSessionFailureCode.MARKET_NOT_READY,
                "MarketSnapshot must be ready before recovery liquidation",
                current_version=self._context.version,
            )
        if not self._account.ready:
            raise TradingSessionError(
                TradingSessionFailureCode.ACCOUNT_NOT_READY,
                "Account must be ready before recovery liquidation",
                current_version=self._context.version,
            )
        if not self._web_socket_gateway.account_ready:
            raise TradingSessionError(
                TradingSessionFailureCode.CONNECTION_NOT_READY,
                "Account stream must be caught up before recovery liquidation",
                current_version=self._context.version,
            )

        # Pending·query·저장 재시도가 하나라도 있으면 새 force-sell과 동시에 진행하지 않는다.
        retained_order_states = (
            *self._order_states_by_client_id.values(),
            *self._order_states_by_order_id.values(),
        )  # 어느 identity index에만 남은 state도 청산 준비에서 누락하지 않는다.
        unresolved_order_state = any(
            state.persistence_pending
            or state.pending_recovery_pending
            or state.awaiting_terminal_zero_confirmation
            or state.order.status is None
            or state.order.status is OrderStatus.UNKNOWN
            or state.order.status in ACTIVE_ORDER_STATUSES
            for state in retained_order_states
        )
        history_controller = self._require_trade_history_controller()
        if (
            unresolved_order_state
            or self._persistence_states_by_order_id
            or self._scheduled_order_queries
            or history_controller.dirty_order_ids
            or len(self._scheduler) > 0
            or self._context.pending_order is not None
            or history_controller.get_pending_order_recovery_records()
        ):
            raise TradingSessionError(
                TradingSessionFailureCode.POSITION_RECONCILIATION_REQUIRED,
                "Pending order recovery blocks recovered-position liquidation",
                current_version=self._context.version,
            )

        # Entity와 startup publication은 수량·평균가까지 정확히 같은 open Position이어야 한다.
        position = self._require_position()
        try:
            position.require_history_accounting_compatibility()
        except LegacyFeeAccountingMigrationRequiredError as error:
            raise TradingSessionError(
                TradingSessionFailureCode.POSITION_RECONCILIATION_REQUIRED,
                "Recovered Position requires fee-accounting migration",
                current_version=self._context.version,
            ) from error
        position_state = position.get_snapshot()
        recovered_position = PositionSnapshot(
            quantity=position_state.quantity,
            entry_price=(
                position_state.average_entry_price
                if position_state.quantity > Decimal("0")
                else None
            ),
        )
        if not recovered_position.is_open:
            raise TradingSessionError(
                TradingSessionFailureCode.TRADING_NOT_STARTED,
                "No recovered open Position is available for liquidation",
                current_version=self._context.version,
            )
        if position_state.owner is None or recovered_position != self._position_snapshot:
            raise TradingSessionError(
                TradingSessionFailureCode.POSITION_RECONCILIATION_REQUIRED,
                "Recovered Position owner or startup snapshot is inconsistent",
                current_version=self._context.version,
            )

        # Pending recovery까지 반영된 최종 durable history를 재생해 open lot의 단일 REGIME을 구한다.
        try:
            recovered_regime, recovered_owner = (
                self._resolve_recovered_position_provenance(
                    history_controller.trade_history.trades,
                    position_state,
                )
            )
        except (ValueError, LegacyFeeAccountingMigrationRequiredError) as error:
            raise TradingSessionError(
                TradingSessionFailureCode.POSITION_RECONCILIATION_REQUIRED,
                "Recovered Position durable provenance is inconsistent",
                current_version=self._context.version,
            ) from error
        if recovered_owner is not position_state.owner:
            raise TradingSessionError(
                TradingSessionFailureCode.POSITION_RECONCILIATION_REQUIRED,
                "Recovered Position owner does not match durable history",
                current_version=self._context.version,
            )
        configuration = get_trading_logic_configuration(recovered_regime)
        if (
            configuration.support_status
            is not TradingLogicSupportStatus.SUPPORTED
        ):
            raise TradingSessionError(
                TradingSessionFailureCode.UNSUPPORTED_TRADING_LOGIC,
                "Recovered Position REGIME is not supported",
                current_version=self._context.version,
            )

        # Account free ETH가 durable Position 전량보다 작으면 부분 SELL로 불일치를 숨기지 않는다.
        effective_free_base = self._get_effective_free_balance(_BASE_ASSET)
        if effective_free_base < recovered_position.quantity:
            raise TradingSessionError(
                TradingSessionFailureCode.POSITION_RECONCILIATION_REQUIRED,
                "Recovered Position exceeds the effective free ETH balance",
                current_version=self._context.version,
            )

        # Entry cap은 가격 상승 뒤 청산을 막지 않으며 SELL은 검증된 Position 전량만 줄인다.
        return recovered_regime, recovered_owner, recovered_position

    def _resolve_recovered_position_provenance(
        self,
        durable_trades: tuple[Trade, ...],
        authoritative_state: PositionStateSnapshot,
    ) -> tuple[RegimeType, StrategyType]:
        """
        함수 이름: _resolve_recovered_position_provenance()
        기능: 전체 durable execution을 재생해 현재 open lot의 단일 REGIME과 owner를 검증한다.
        인자: durable_trades -> startup pending recovery까지 반영된 canonical Trade tuple
            authoritative_state -> Controller Position의 최종 immutable state
        반환값: 현재 open lot의 canonical REGIME과 owner
        작성 날짜: 2026/08/24
        """
        if not isinstance(durable_trades, tuple) or any(
            not isinstance(trade, Trade) for trade in durable_trades
        ):
            raise TypeError("durable_trades must be a tuple of Trade values")
        if not isinstance(authoritative_state, PositionStateSnapshot):
            raise TypeError(
                "authoritative_state must be a PositionStateSnapshot"
            )

        # 별도 Position에 같은 회계를 재생하며 닫힌 과거 lot의 REGIME은 다음 lot로 넘기지 않는다.
        replayed_position = Position(_TRADING_SYMBOL)
        open_lot_regime: RegimeType | None = None
        open_lot_owner: StrategyType | None = None
        residual_entries = {} if self._residual_settlement is None else {entry.history_count: entry for entry in self._residual_settlement.transfers}
        for history_count, trade in enumerate(durable_trades, 1):
            if replayed_position.quantity == Decimal("0"):
                if trade.side is not OrderSide.BUY:
                    raise ValueError("A recovered open lot must begin with BUY")
                open_lot_regime = trade.regime_type
                open_lot_owner = trade.strategy
            elif (
                trade.regime_type is not open_lot_regime
                or trade.strategy is not open_lot_owner
            ):
                raise ValueError(
                    "A recovered open lot cannot mix REGIME or owner"
                )

            replayed_position.apply_historical_trade(trade)
            residual = residual_entries.get(history_count)
            if residual is not None:
                replayed_position.detach_residual(residual.quantity, residual.cost_basis, residual.step_size)
            if replayed_position.quantity == Decimal("0"):
                open_lot_regime = None
                open_lot_owner = None

        # Final Position 전체가 startup owner의 authoritative state와 같아야 history를 신뢰한다.
        if replayed_position.get_snapshot() != authoritative_state:
            raise ValueError("Durable history does not reproduce Position state")
        if open_lot_regime is None or open_lot_owner is None:
            raise ValueError("Durable history does not contain an open lot")

        return open_lot_regime, open_lot_owner

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
        if self._requires_manual_kill_cleanup_locked():
            raise TradingSessionError(
                TradingSessionFailureCode.COMMAND_DISABLED,
                "Manual kill blocks a new strategy session",
                current_version=self._context.version,
            )
        if self._process_lifetime_reconciliation_required_locked():
            raise TradingSessionError(
                TradingSessionFailureCode.POSITION_RECONCILIATION_REQUIRED,
                "Process-lifetime reconciliation requires a fresh process",
                current_version=self._context.version,
            )
        if self._stream_reconciliation_required:
            raise TradingSessionError(
                TradingSessionFailureCode.CONNECTION_NOT_READY,
                "Account stream requires full REST reconciliation",
                current_version=self._context.version,
            )
        if not self._market_stream_ready:
            raise TradingSessionError(
                TradingSessionFailureCode.CONNECTION_NOT_READY,
                "Market stream requires a full REST reconciliation",
                current_version=self._context.version,
            )
        if (
            self._pending_order_recovery_enabled
            and not self._startup_reconciliation_complete
        ):
            raise TradingSessionError(
                TradingSessionFailureCode.POSITION_RECONCILIATION_REQUIRED,
                "Startup order and position reconciliation is incomplete",
                current_version=self._context.version,
            )
        if self._startup_reconciliation_blocked:
            raise TradingSessionError(
                TradingSessionFailureCode.POSITION_RECONCILIATION_REQUIRED,
                "A recovered active order blocks a new trading session",
                current_version=self._context.version,
            )
        if not self._mode_command_enabled:
            raise TradingSessionError(
                TradingSessionFailureCode.COMMAND_DISABLED,
                "Trading commands are disabled by the selected execution mode",
                current_version=self._context.version,
            )
        if self._selected_regime is None:
            raise TradingSessionError(
                TradingSessionFailureCode.NO_SELECTED_REGIME,
                "A REGIME must be selected before trading starts",
                current_version=self._context.version,
            )

        # 지원 여부는 지속되는 REGIME 선택으로 판단하며 세션 STM은 START에서 준비한다.
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
        if not self._web_socket_gateway.account_ready:
            raise TradingSessionError(
                TradingSessionFailureCode.CONNECTION_NOT_READY,
                "Account stream must be connected and caught up before trading starts",
                current_version=self._context.version,
            )

        # 설명되지 않은 Position이나 pending 주문은 새 session으로 덮지 않고 reconciliation을 요구한다.
        if self._position_snapshot.is_open:
            raise TradingSessionError(
                TradingSessionFailureCode.POSITION_RECONCILIATION_REQUIRED,
                "A reconciled open position blocks a new trading session",
                current_version=self._context.version,
            )
        if self._position is not None and self._position.quantity > Decimal("0"):
            raise TradingSessionError(
                TradingSessionFailureCode.POSITION_RECONCILIATION_REQUIRED,
                "The authoritative Position blocks a new trading session",
                current_version=self._context.version,
            )
        if self._context.initialized and self._context.pending_order is not None:
            raise TradingSessionError(
                TradingSessionFailureCode.POSITION_RECONCILIATION_REQUIRED,
                "A pending order must be reconciled before trading starts",
                current_version=self._context.version,
            )
        if self._persistence_states_by_order_id:
            raise TradingSessionError(
                TradingSessionFailureCode.POSITION_RECONCILIATION_REQUIRED,
                "Pending trade persistence blocks a new trading session",
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

        evaluation_context = self._context.snapshot()  # action 이전의 비교 기준을 보존한다.
        if self._diagnostics.enabled:
            self._record_diagnostic(
                "decision_started", decision_id=result.decision_id,
                transition_ids=result.transition_ids,
                evaluation=describe_trading_evaluation(result.state_before, evaluation_context),
            )  # 직접 start/stop 처리도 첫 effect 이전의 원본 입력을 남긴다.

        # patch도 원래 위치에서 적용해 Context mutation과 외부 요청의 순서를 보존한다.
        returned_events: list[TradingEvent] = []
        for action in result.action_requests:
            if isinstance(action, PatchRuntimeContext):
                trace_identity = self._prepare_order_patch_trace(action)
                patch_result = replace(
                    result,
                    action_requests=(action,),
                    context_version=self._context.version,
                )
                self._context.apply_trading_stm_result(
                    patch_result
                )  # Context는 이 위치의 runtime patch만 실행하고 다른 Action은 모른다.
                self._complete_order_patch_trace(trace_identity)
                self._action_trace.append(action)
                continue
            if isinstance(action, QueueEvent):
                self._action_trace.append(action)
                self._enqueue_internal_action(action)
                continue

            returned_events.extend(self._execute_action(action))

        # direct start/stop의 동기 결과도 공통 queue+wake 경계에서 다음 microstep으로 넘긴다.
        if returned_events:
            self._enqueue_order_outcomes(
                returned_events
            )  # message 14는 재귀 호출 없이 worker의 다음 bounded cycle에서만 실행한다.

        self._record_indicator_evaluation(result, evaluation_context)

    def _record_indicator_evaluation(self, result: TradingSTMResult, context: TradingContextView) -> None:
        """
        함수 이름: _record_indicator_evaluation()
        기능: 성공한 STM 처리의 입력과 출력 상태를 같은 lock에서 지표 저장소에 반영한다.
        인자: result -> action 적용이 끝난 전이 결과
            context -> 실제 Guard가 판단한 원본 Context
        반환값: 없음
        작성 날짜: 2026/09/05
        """
        # 종료 cleanup 이후에도 입력·전이·최종 runtime을 남겨 마지막 청산까지 복원할 수 있게 한다.
        if self._diagnostics.enabled:
            self._record_diagnostic(
                "strategy_evaluated", decision_id=result.decision_id,
                consumed=result.consumed, transition_ids=result.transition_ids,
                state_before=result.state_before, state_after=result.state_after,
                evaluation=describe_trading_evaluation(result.state_before, context),
                action_requests=result.action_requests,
                runtime_after=self._context.runtime, position_after=self._context.snapshot().position,
            )

        # 시작·종료 직접 호출과 queue 처리 모두 같은 저장 경계를 사용한다.
        if self._active_stm is not None:
            self._indicator_store.observe(
                self._active_stm, result, context, self._context.snapshot(),
                self._latest_market_evaluation_version,
                entered_at=self._position.entered_at if self._position is not None else None,
            )  # 표시를 위해 주문 로직을 실행하거나 Context를 수정하지 않는다.

    def _execute_action(
        self,
        action: TradingActionRequest,
    ) -> tuple[TradingEvent, ...]:
        """
        함수 이름: _execute_action()
        기능: typed action 하나를 Context method, scheduler, cleanup 또는 Phase 8 요청으로 분배한다.
        인자: action -> STM이 생성한 하나의 TradingActionRequest
        반환값: 동기 완료된 concrete order outcome event tuple
        작성 날짜: 2026/08/21
        """
        self._action_trace.append(action)  # 부수 효과 전에 요청 순서를 먼저 고정한다.
        self._record_diagnostic("action_requested", action_type=type(action).__name__, action=action)
        if isinstance(action, SubmitOrder):
            self._append_public_market_action_boundary(
                action
            )  # Public STM action도 journal/client ID/REST effect보다 먼저 immutable하게 관찰한다.

        # Context mutation은 임의 setattr 대신 domain typed method로만 적용한다.
        if isinstance(action, PatchRuntimeContext):
            trace_identity = self._prepare_order_patch_trace(action)
            self._context.apply_runtime_patch(action)
            self._complete_order_patch_trace(trace_identity)
            return ()
        if isinstance(action, OpenLowerEvent):
            self._context.open_lower_event(action)
            return ()
        if isinstance(action, CloseLowerEvent):
            self._context.close_lower_event(action)
            return ()
        if isinstance(action, ResetCaseBContext):
            self._context.reset_case_b_context(action)
            return ()
        if isinstance(action, ResetCaseCContext):
            self._context.reset_case_c_context(action)
            return ()
        if isinstance(action, ScheduleReevaluation):
            scheduled_action = self._apply_order_retry_delay(action)
            if scheduled_action is not None:
                self._scheduler.schedule(scheduled_action)
                self._request_event_runtime_processing()
            return ()
        if isinstance(action, CancelScheduledEvaluation):
            self._scheduler.cancel(action.scope)
            return ()
        if isinstance(action, StopTradingRuntime):
            self._cleanup_session_resources()
            return ()
        if isinstance(action, QueueEvent):
            return ()  # processor가 action batch 뒤에 enqueue하고 Controller는 trace만 소유한다.

        # 외부 effect trace는 실제 fake Gateway 실행 여부와 무관하게 원본 Action을 보존한다.
        self._external_actions.append(action)
        if (
            self._market_stream_interrupted_running_session
            and isinstance(action, (SubmitOrder, ForceSellAll))
        ):
            self._record_diagnostic("action_blocked", level="WARNING", reason="MARKET_STREAM_INTERRUPTED", action_type=type(action).__name__)
            return ()  # Operator/session 재조정 전에는 신규 제출만 막고 same-order 취소·조회는 허용한다.
        if not self._order_pipeline_enabled:
            self._record_diagnostic("action_blocked", level="WARNING", reason="ORDER_PIPELINE_DISABLED", action_type=type(action).__name__)
            if (
                self._recovered_position_liquidation_session
                and isinstance(action, ForceSellAll)
            ):
                raise _RecoveredPositionLiquidationGateClosedError(
                    "Recovered-position liquidation effect gate is closed"
                )
            return ()  # legacy/disabled fixture는 Phase 7의 수동 outcome 경계를 유지한다.
        if isinstance(action, SubmitOrder):
            return self._submit_order_action(action)
        if isinstance(action, CancelPendingOrder):
            return self._cancel_pending_order_action(action)
        if isinstance(action, ReconcileOrder):
            return self._reconcile_order_action(action)
        if isinstance(action, ForceSellAll):
            return self._force_sell_action(action)

        return ()  # TypeAlias가 확장되더라도 알 수 없는 외부 effect를 임의 실행하지 않는다.

    @property
    def _order_pipeline_enabled(self) -> bool:
        """
        함수 이름: _order_pipeline_enabled()
        기능: 허용 mode와 Position·history owner가 모두 준비된 경우에만 주문 effect를 허용한다.
        인자: 없음
        반환값: Phase 8 pipeline 실행 가능 여부
        작성 날짜: 2026/08/22
        """
        # Mode·startup·stream·event worker·owner가 모두 준비된 경우에만 주문 effect를 허용한다.
        return (
            self._mode_command_enabled
            and self._web_socket_gateway.account_ready
            and not self._stream_reconciliation_required
            and self._market_stream_ready
            and not self._event_runtime_failed
            and not self._process_ownership_ambiguous
            and not self._external_execution_reconciliation_required
            and (
                not self._pending_order_recovery_enabled
                or self._startup_reconciliation_complete
            )
            and not self._startup_reconciliation_blocked
            and self._position is not None
            and self._trade_history_controller is not None
        )  # Startup 복구 전 외부 effect를 막고 완료 뒤 기존 주문의 내부 reconciliation을 허용한다.

    def _prepare_order_patch_trace(
        self,
        action: PatchRuntimeContext,
    ) -> tuple[str, str, int] | None:
        """
        함수 이름: _prepare_order_patch_trace()
        기능: 주문 예약 patch에서 intent를 찾아 Context mutation 전 메시지 1을 기록한다.
        인자: action -> 적용 직전의 typed runtime patch
        반환값: 메시지 2 완료에 쓸 intent, client ID, 이전 version 또는 주문 patch가 아니면 None
        작성 날짜: 2026/08/22
        """
        # pending intent를 문자열로 설정하는 patch만 Case 2 주문 결정 경계로 취급한다.
        intent_id = next(
            (
                change.value
                for change in action.changes
                if change.field is RuntimeField.PENDING_INTENT_ID
                and isinstance(change.value, str)
            ),
            None,
        )
        if intent_id is None:
            return None  # 일반 runtime patch는 주문 Communication trace를 만들지 않는다.

        # 아직 제출 전이므로 현재 intent attempt에서 사용할 결정론적 client ID를 계산한다.
        submission_attempt = self._submission_attempts_by_intent.get(intent_id, 0)
        if (
            submission_attempt
            >= self._maximum_order_submissions_per_intent
        ):
            return None  # 소진된 intent는 trace용 client ID조차 새로 계산하지 않는다.
        client_order_id = self._create_client_order_id(
            intent_id,
            submission_attempt,
        )
        version_before = self._context.version
        self._append_order_trace_values(
            "1",
            intent_id,
            client_order_id,
            None,
            version_before,
        )
        return (intent_id, client_order_id, version_before)

    def _complete_order_patch_trace(
        self,
        trace_identity: tuple[str, str, int] | None,
    ) -> None:
        """
        함수 이름: _complete_order_patch_trace()
        기능: 주문 예약 Context patch 성공 직후 메시지 2와 증가한 version을 기록한다.
        인자: trace_identity -> mutation 전에 준비한 intent·client ID·version 또는 None
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        if trace_identity is None:
            return  # 주문과 무관한 patch에는 메시지 2도 존재하지 않는다.

        # 메시지 2의 before는 patch 전, after는 실제 mutation 후 Context version이다.
        intent_id, client_order_id, version_before = trace_identity
        self._append_order_trace_values(
            "2",
            intent_id,
            client_order_id,
            None,
            version_before,
        )

    def _apply_order_retry_delay(
        self,
        action: ScheduleReevaluation,
    ) -> ScheduleReevaluation | None:
        """
        함수 이름: _apply_order_retry_delay()
        기능: STM의 retry 요청에 intent별 1·2·4·8초 지연과 주입된 제출 예산을 적용한다.
        인자: action -> STM이 만든 재평가 요청
        반환값: delay를 보강한 action 또는 예산 소진이면 None
        작성 날짜: 2026/08/22
        """
        # 시장·candle scheduler와 이미 명시된 delay는 주문 retry 정책으로 바꾸지 않는다.
        if (
            action.trigger is not ReevaluationTrigger.RETRY_BACKOFF
            or action.earliest_delay is not None
        ):
            return action

        # 주문 retry만 intent별 제출 횟수와 bounded schedule을 사용한다.
        intent_id = self._context.runtime.pending_intent_id
        if intent_id is None:
            return action  # 주문 의도와 무관한 backoff는 기존 즉시 trigger 계약을 유지한다.
        submission_count = self._submission_attempts_by_intent.get(intent_id, 0)
        if (
            submission_count
            >= self._maximum_order_submissions_per_intent
        ):
            self._handle_submission_budget_exhausted(intent_id)
            return None

        # 최초 실패 뒤 1초부터 네 번째 재제출 전 8초까지 deterministic 지연을 선택한다.
        delay_index = max(0, submission_count - 1)
        retry_delay = self._jittered_order_retry_delay(
            _ORDER_RECONCILIATION_DELAYS[delay_index]
        )
        return replace(
            action,
            earliest_delay=retry_delay,
        )  # 주입 factor 1.0을 쓰는 fake에서는 정확히 1·2·4·8초가 된다.

    def _jittered_order_retry_delay(self, base_delay: timedelta) -> timedelta:
        """
        함수 이름: _jittered_order_retry_delay()
        기능: ADR-002의 주입 jitter factor를 기본 대기에 정밀하게 적용한다.
        인자: base_delay -> 1·2·4·8초 중 하나인 기본 대기
        반환값: 0.8~1.2 factor가 적용된 timedelta
        작성 날짜: 2026/08/22
        """
        if not isinstance(base_delay, timedelta) or base_delay <= timedelta(0):
            raise ValueError("base_delay must be a positive timedelta")

        # provider 결과는 float를 혼용하지 않고 ADR 경계 안의 유한 Decimal로 제한한다.
        factor = self._order_retry_jitter()
        if not isinstance(factor, Decimal):
            raise TypeError("order retry jitter factor must be a Decimal")
        if (
            not factor.is_finite()
            or factor < Decimal("0.8")
            or factor > Decimal("1.2")
        ):
            raise ValueError("order retry jitter factor must be between 0.8 and 1.2")

        # microsecond 정수로 변환해 binary float 오차 없이 재현 가능한 scheduler 시각을 만든다.
        base_microseconds = (
            (base_delay.days * 86_400 + base_delay.seconds) * 1_000_000
            + base_delay.microseconds
        )
        jittered_microseconds = int(
            (Decimal(base_microseconds) * factor).to_integral_value(
                rounding=ROUND_HALF_EVEN
            )
        )
        return timedelta(microseconds=jittered_microseconds)

    def _handle_submission_budget_exhausted(self, intent_id: str) -> None:
        """
        함수 이름: _handle_submission_budget_exhausted()
        기능: 총 5회 제출 후 최종 실패를 보존하되 잔여 Position이 있을 때만 운영 lock을 유지한다.
        인자: intent_id -> 예산을 모두 소비한 원 주문 의도 ID
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        if not isinstance(intent_id, str) or not intent_id.strip():
            raise ValueError("intent_id must be a non-empty string")

        # ADR-002에 따라 실제 Position 잔량이 남은 실패만 운영 lock으로 보존한다.
        state = self._find_latest_state_for_intent(intent_id)
        position_quantity = (
            Decimal("0")
            if self._position is None
            else self._position.quantity
        )
        if position_quantity > Decimal("0"):
            self._enter_order_reconciliation(
                state,
                OrderExecutionFailureCode.SUBMISSION_BUDGET_EXHAUSTED,
                message_id=None,
            )
            return

        # Position 0 BUY는 이미 최종 실패 event를 STM에 전달했으므로 pending 의도를 정상 종료한다.
        self._quantity_overrides_by_intent.pop(intent_id, None)
        self._context.update_pending_order(None)
        if self._context.initialized:
            self._context.apply_runtime_patch(
                patch(trading_phase=TradingPhase.IDLE)
            )

    def _submit_order_action(
        self,
        action: SubmitOrder,
        *,
        force_sell: bool = False,
        quantity_override: Decimal | None = None,
    ) -> tuple[TradingEvent, ...]:
        """
        함수 이름: _submit_order_action()
        기능: intent 수량을 계산해 Order를 한 번 제출하고 결과를 같은 pipeline에 반영한다.
        인자: action -> STM 또는 force-sell adapter의 주문 의도
            force_sell -> stop 전량 매도인지 여부
            quantity_override -> residual 전량 등 비율 대신 사용할 검증된 수량
        반환값: terminal·durable 완료 시 concrete outcome tuple
        작성 날짜: 2026/08/22
        """
        # 외부 effect 입력과 optional residual 수량을 Gateway 호출 전에 검증한다.
        if not isinstance(action, SubmitOrder):
            raise TypeError("action must be a SubmitOrder")

        # 일반 Case C SELL은 trace·journal·REST 전에 최초 청산 의도와 exact 대조한다.
        if (
            not force_sell
            and action.strategy is StrategyType.CASE_C
            and action.side is OrderSide.SELL
        ):
            runtime = self._context.runtime
            exit_pct_b_at_intent = action.exit_pct_b_at_intent
            if (
                action.exit_reason is None
                or action.exit_reason is not runtime.pending_exit_reason
                or not isinstance(exit_pct_b_at_intent, Decimal)
                or not exit_pct_b_at_intent.is_finite()
                or runtime.pending_exit_pct_b != exit_pct_b_at_intent
            ):
                raise ValueError(
                    "non-force Case C SELL requires exact exit intent provenance"
                )
        if quantity_override is not None and (
            not isinstance(quantity_override, Decimal)
            or not quantity_override.is_finite()
            or quantity_override <= Decimal("0")
        ):
            raise ValueError("quantity_override must be a positive finite Decimal")

        # unresolved query나 저장 실패가 있으면 같은 intent를 포함한 모든 신규 제출을 차단한다.
        if self._status is TradingSessionStatus.RECONCILIATION_REQUIRED:
            return ()
        if self._persistence_states_by_order_id:
            self._enter_order_reconciliation(
                None,
                OrderExecutionFailureCode.HISTORY_PERSISTENCE_FAILED,
                message_id="13.5",
            )
            return ()
        existing_state = self._find_active_state_for_intent(action.idempotency_key)
        if existing_state is not None:
            self._schedule_order_query(existing_state, None)
            return ()  # active/UNKNOWN 주문은 새 client ID 대신 같은 Order를 조회한다.

        # terminal로 확정된 이전 attempt만 있는 경우에도 intent의 총 제출 예산을 지킨다.
        submission_count = self._submission_attempts_by_intent.get(
            action.idempotency_key,
            0,
        )
        if (
            submission_count
            >= self._maximum_order_submissions_per_intent
        ):
            self._handle_submission_budget_exhausted(action.idempotency_key)
            return ()

        # Context patch 때 예고한 동일 attempt client ID로 실제 Order identity를 만든다.
        provisional_client_id = self._create_client_order_id(
            action.idempotency_key,
            submission_count,
        )

        # 메시지 3·4의 split ratio와 decision price를 실제 Order 생성 전에 읽는다.
        context_version = self._context.version
        split_ratio = self._context.get_split_ratio()
        self._append_order_trace_values(
            "3",
            action.idempotency_key,
            provisional_client_id,
            None,
            context_version,
        )
        context_market_price = self._context.market.realtime_price
        if self._latest_market_evaluation_version > 0:
            # Public provenance가 있으면 claimed 30분 candidate만 허용하고 4H 값으로 조용히 대체하지 않는다.
            if (
                not context_market_price.is_finite()
                or context_market_price <= Decimal("0")
            ):
                raise RuntimeError(
                    "claimed market evaluation price must remain positive and finite"
                )
            market_price = context_market_price
        else:
            # Evaluation이 전혀 없는 legacy/direct 경계만 ready 4H 가격을 fallback으로 사용한다.
            market_price = self._market_snapshot.get_current_eth_price()
        self._append_order_trace_values(
            "4",
            action.idempotency_key,
            provisional_client_id,
            None,
            context_version,
        )

        # residual override가 없으면 Account/Position의 free 수량과 선택 split을 사용한다.
        selected_override = quantity_override
        if selected_override is None:
            selected_override = self._quantity_overrides_by_intent.pop(
                action.idempotency_key,
                None,
            )
        requested_quantity = self._calculate_order_quantity(
            action.side,
            split_ratio,
            market_price,
            selected_override,
            force_sell=force_sell,
        )
        if requested_quantity <= Decimal("0"):
            self._append_order_trace_values(
                "5",
                action.idempotency_key,
                provisional_client_id,
                None,
                self._context.version,
                failure_code=OrderExecutionFailureCode.ZERO_ORDER_QUANTITY,
            )
            # 복구 Position이 있지만 현재 free 수량이 0이면 effect 전 원상복구해 재동기화 후 재시도한다.
            if self._recovered_position_liquidation_session:
                raise _RecoveredPositionLiquidationPreflightError(
                    "recovered-position liquidation quantity is zero"
                )

            # 일반 세션의 0 수량은 기존 fail-closed 주문 재조정 정책을 유지한다.
            self._enter_order_reconciliation(
                None,
                OrderExecutionFailureCode.ZERO_ORDER_QUANTITY,
                message_id=None,
            )
            return ()  # 수량 0에서는 Gateway를 단 한 번도 호출하지 않는다.

        # 수량 확정 뒤 선택 REGIME과 filter 전 Order aggregate를 먼저 완성한다.
        active_stm = self._active_stm
        if active_stm is None:
            raise RuntimeError("an active order requires a TradingSTM")
        selected_regime = active_stm.regime_type  # 주문 REGIME은 UI 선택이 아니라 session owner에서 읽는다.
        order = Order(
            intent_id=action.idempotency_key,
            client_order_id=provisional_client_id,
            submission_attempt=submission_count,
            symbol=_TRADING_SYMBOL,
            side=action.side,
            strategy=action.strategy,
            regime_type=selected_regime,
            requested_quantity=requested_quantity,
            submitted_quantity=requested_quantity,
            market_price_at_decision=market_price,
            risk_policy_version=self._session_risk_policy_version,
            exit_reason=action.exit_reason,
            exit_pct_b_at_intent=action.exit_pct_b_at_intent,
        )
        # 최신 exchangeInfo filter는 요청 수량을 보존한 채 제출 수량만 내림 조정한다.
        try:
            order = self._api_gateway.prepare_order(order)
        except Exception as error:
            self._append_order_trace_values(
                "5",
                action.idempotency_key,
                provisional_client_id,
                None,
                context_version,
                failure_code=OrderExecutionFailureCode.SYMBOL_FILTER_REJECTED,
            )
            # 복구 청산은 durable journal·POST 전 실패이므로 상위 Operation이 원자적 복원한다.
            if self._recovered_position_liquidation_session:
                raise _RecoveredPositionLiquidationPreflightError(
                    "recovered-position liquidation order preflight failed"
                ) from error

            # 일반 자동매매 세션은 기존 fail-closed 정책대로 재조정 상태로 전환한다.
            self._enter_order_reconciliation(
                None,
                OrderExecutionFailureCode.SYMBOL_FILTER_REJECTED,
                message_id=None,
            )
            return ()  # filter 위반 수량은 거래소 REST 경계에 도달하지 않는다.

        # 복구 청산은 부분 cap·LOT_SIZE 내림을 허용하지 않고 한 주문의 정확한 전량만 journal에 넣는다.
        if self._recovered_position_liquidation_session:
            current_position_quantity = self._require_position().quantity
            if (
                requested_quantity != current_position_quantity
                or (order.submitted_quantity != current_position_quantity
                    and not self._allows_residual_rounding(current_position_quantity, order.submitted_quantity))
            ):
                self._append_order_trace_values(
                    "5",
                    action.idempotency_key,
                    provisional_client_id,
                    None,
                    context_version,
                    failure_code=(
                        OrderExecutionFailureCode.SYMBOL_FILTER_REJECTED
                    ),
                )
                raise _RecoveredPositionLiquidationPreflightError(
                    "recovered-position liquidation must submit the exact full Position"
                )  # PREPARED fsync 전이므로 상위 Operation이 동일 command 재시도를 허용한다.

        # 모든 production BUY는 filter 뒤 실제 제출 수량으로 같은 cumulative risk gate를 통과한다.
        if order.side is OrderSide.BUY:
            risk_decision = self._evaluate_buy_order_risk(order)
            if not risk_decision.allowed:
                risk_block_reason = risk_decision.block_reason
                if risk_block_reason is None:
                    raise RuntimeError("blocked risk decision requires a reason")
                self._append_order_trace(
                    order,
                    "5.1",
                    context_version,
                    failure_code=OrderExecutionFailureCode(
                        risk_block_reason.value
                    ),
                )
                return (
                    TradingEvent(
                        event_type=TradingEventType.BUY_RISK_BLOCKED,
                        occurred_at=self._clock(),
                        priority=EventPriority.ORDER_OUTCOME,
                        event_id=(
                            f"{order.client_order_id}:risk:"
                            f"{risk_block_reason.value}"
                        ),
                        lower_event_id=self._context.runtime.lower_event_id,
                        order_id=order.client_order_id,
                        payload=BuyRiskBlockedPayload(
                            strategy=order.strategy,
                            reason=risk_block_reason,
                        ),
                    ),
                )  # PREPARED journal과 REST POST 전에 STM에 typed feedback만 전달한다.
            self._append_order_trace(order, "5.1", context_version)

        # Filter 적용을 마친 정확한 Order를 한 submission state와 durable recovery 근거로 묶는다.
        state = _OrderExecutionState(
            order=order,
            force_sell=force_sell,
        )
        history_controller = self._require_trade_history_controller()
        if self._pending_order_recovery_enabled:
            try:
                history_controller.save_pending_order(order)
            except Exception:
                # Fsync 반환 전후가 불명하므로 메모리에도 같은 identity와 소비 attempt를 보수적으로 보존한다.
                state.pending_recovery_pending = True
                self._order_states_by_client_id[
                    order.client_order_id
                ] = state
                self._submission_attempts_by_intent[
                    action.idempotency_key
                ] = max(
                    submission_count + 1,
                    self._submission_attempts_by_intent.get(
                        action.idempotency_key,
                        0,
                    ),
                )
                self._append_order_trace(
                    order,
                    "5",
                    context_version,
                    failure_code=(
                        OrderExecutionFailureCode.HISTORY_PERSISTENCE_FAILED
                    ),
                )
                self._enter_order_reconciliation(
                    state,
                    OrderExecutionFailureCode.HISTORY_PERSISTENCE_FAILED,
                    message_id=None,
                )
                # 예외가 fsync 뒤일 수 있어 session/journal은 rollback하지 않되 신규 POST는 금지한다.
                return ()  # Operator가 sidecar와 same-ID 거래소 사실을 조정할 때까지 gate를 잠근다.
            state.pending_recovery_pending = True  # UPSERT fsync 성공 사실을 history 저장과 별도로 추적한다.

        self._order_states_by_client_id[order.client_order_id] = state
        self._submission_attempts_by_intent[action.idempotency_key] = (
            submission_count + 1
        )
        self._append_order_trace(order, "5", context_version)
        self._publish_pending_order(state)  # Gateway 호출 전부터 client ID를 in-flight 복구 근거로 게시한다.

        # Filter·journal 도중 stream이 끊기거나 callback backlog가 생기면 POST 직전에 중단한다.
        if not self._web_socket_gateway.account_ready:
            self._stream_reconciliation_required = True
            self._append_order_trace(
                order,
                "6",
                self._context.version,
                failure_code=(
                    OrderExecutionFailureCode.STREAM_RECONCILIATION_REQUIRED
                ),
            )
            self._append_order_trace(
                order,
                "6.1",
                self._context.version,
                failure_code=(
                    OrderExecutionFailureCode.STREAM_RECONCILIATION_REQUIRED
                ),
            )
            self._enter_order_reconciliation(
                state,
                OrderExecutionFailureCode.STREAM_RECONCILIATION_REQUIRED,
                message_id=None,
                cause_category=(
                    ReconciliationCauseCategory.ACCOUNT_STREAM_UNKNOWN_OR_EXTERNAL_EXECUTION
                ),
            )
            return ()  # Durable PREPARED journal을 유지해 재시작에도 미확정 주문을 숨기지 않는다.

        # REST mutation 직전 SUBMITTED를 fsync해 crash-before/after-call이 같은 ID 조회로 수렴하게 한다.
        if not self._transition_pending_order_recovery(
            state,
            PendingOrderRecoveryLifecycle.SUBMITTED,
        ):
            return ()  # Lifecycle fsync가 불명하면 REST POST는 시작하지 않는다.

        # 메시지 6/6.1은 실제 Gateway 결과를 관찰한 뒤 성공·실패를 같은 식별자로 기록한다.
        gateway_version = self._context.version
        self._record_diagnostic("order_submit_started", order=order, force_sell=force_sell)
        try:
            if force_sell:
                result = self._api_gateway.sell_all_position(order)
            else:
                result = self._api_gateway.submit_order(order)
        except Exception as error:
            self._append_order_trace(
                order,
                "6",
                gateway_version,
                failure_code=OrderExecutionFailureCode.GATEWAY_REQUEST_FAILED,
            )
            self._append_order_trace(
                order,
                "6.1",
                gateway_version,
                failure_code=OrderExecutionFailureCode.GATEWAY_REQUEST_FAILED,
            )
            result = OrderResult(
                symbol=order.symbol,
                client_order_id=order.client_order_id,
                status=OrderStatus.UNKNOWN,
                processed_at=self._clock(),
                failure_reason=type(error).__name__,
            )  # 제출 결과 불명은 실패 재제출이 아니라 동일 client ID 조회로 전환한다.
        else:
            self._append_order_trace(order, "6", gateway_version)
            self._append_order_trace(order, "6.1", gateway_version)

        return self._handle_order_result(state, result, initial=True)

    def _evaluate_buy_order_risk(self, order: Order) -> RiskDecision:
        """
        함수 이름: _evaluate_buy_order_risk()
        기능: filter 뒤 BUY와 current Position·예약·KST 손실을 한 RiskBudgetSnapshot으로 평가한다.
        인자: order -> 아직 journal이나 Gateway에 전달하지 않은 BUY Order
        반환값: 순수 RiskPolicy gate가 만든 RiskDecision
        작성 날짜: 2026/08/24
        """
        # Journal 이전 gate는 exact BUY Order만 받아 다른 side의 cleanup 경로를 방해하지 않는다.
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")
        if order.side is not OrderSide.BUY:
            raise ValueError("risk evaluation is available only for BUY orders")

        # Position·history·market 값은 session RLock을 보유한 caller의 동일 원자 snapshot에서 읽는다.
        position = self._require_position()
        history_controller = self._require_trade_history_controller()
        position_state = position.get_snapshot()
        decision_price = order.market_price_at_decision
        current_time = self._clock()
        if not isinstance(current_time, datetime):
            raise TypeError("clock result must be a datetime")
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            raise ValueError("clock result must be timezone-aware UTC")
        if current_time.utcoffset() != timedelta(0):
            raise ValueError("clock result must use UTC")
        current_kst_date = current_time.astimezone(_KOREA_TIME_ZONE).date()

        # Active·UNKNOWN·PREPARED BUY의 미체결 수량만 후보 외 예약 예산으로 합산한다.
        reserved_buy_notional = Decimal("0")
        for state in self._order_states_by_client_id.values():
            reserved_order = state.order
            if reserved_order.side is not OrderSide.BUY or reserved_order.is_terminal:
                continue
            remaining_quantity = (
                reserved_order.submitted_quantity
                - reserved_order.filled_quantity
            )
            if remaining_quantity > Decimal("0"):
                reserved_buy_notional += (
                    remaining_quantity
                    * reserved_order.market_price_at_decision
                )

        # Durable KST 당일 SELL만 실현손익에 포함하고 BUY/null field는 계산 대상에서 제외한다.
        daily_realized_pnl = Decimal("0")
        for trade in history_controller.trade_history.trades:
            if (
                trade.side is not OrderSide.SELL
                or trade.executed_at.astimezone(_KOREA_TIME_ZONE).date()
                != current_kst_date
            ):
                continue
            if trade.realized_pnl is None:
                raise RuntimeError("durable SELL Trade is missing realized PnL")
            daily_realized_pnl += trade.realized_pnl

        # Decimal128 계산 context로 ambient precision과 무관한 누적 exposure·PnL을 고정한다.
        with localcontext() as decimal_context:
            decimal_context.prec = 34
            current_position_notional = (
                (position_state.quantity + self.residual_totals[0]) * decision_price
            )
            candidate_order_notional = (
                order.submitted_quantity * decision_price
            )
            projected_position_notional = (
                current_position_notional
                + reserved_buy_notional
                + candidate_order_notional
            )
            unrealized_pnl = (
                current_position_notional - position_state.cost_basis - self.residual_totals[1]
            )

            # Unavailable policy는 먼저 차단되므로 진단 snapshot에는 realized-only 손실을 보수적으로 둔다.
            policy_state = self._risk_policy_state
            loss_scope = (
                policy_state.daily_loss_scope
                if isinstance(policy_state, RiskPolicy)
                else DailyLossScope.REALIZED_ONLY
            )
            scoped_pnl = daily_realized_pnl
            if loss_scope is DailyLossScope.REALIZED_AND_UNREALIZED:
                scoped_pnl += unrealized_pnl
            daily_loss = max(-scoped_pnl, Decimal("0"))

        budget = RiskBudgetSnapshot(
            policy_version=self._session_risk_policy_version,
            market_version=(
                self._latest_market_evaluation_version
                if self._latest_market_evaluation_version > 0
                else self._market_snapshot.version
            ),
            account_version=self._account.version,
            context_version=self._context.version,
            current_position_notional=current_position_notional,
            reserved_buy_notional=reserved_buy_notional,
            candidate_order_notional=candidate_order_notional,
            projected_position_notional=projected_position_notional,
            daily_realized_pnl=daily_realized_pnl,
            unrealized_pnl=unrealized_pnl,
            daily_loss=daily_loss,
            manual_kill_active=self._manual_kill_active,
        )
        decision = evaluate_buy_risk(policy_state, budget)
        self._last_risk_decision = decision
        self._record_diagnostic("risk_evaluated", order=order, decision=decision)
        return decision  # frozen decision은 UI publication과 fault trace가 같은 budget을 재사용한다.

    def _calculate_order_quantity(
        self,
        side: OrderSide,
        split_ratio: Decimal,
        market_price: Decimal,
        quantity_override: Decimal | None,
        *,
        force_sell: bool,
    ) -> Decimal:
        """
        함수 이름: _calculate_order_quantity()
        기능: BUY free quote 또는 SELL Position/free base에서 요청 수량을 Decimal 계산한다.
        인자: side -> 주문 방향
            split_ratio -> Context에서 읽은 매수·매도 비율
            market_price -> Order decision 시점 ETHUSDT 가격
            quantity_override -> terminal partial 뒤 잔량 또는 None
            force_sell -> split 없이 잔여 Position 전량인지 여부
        반환값: filter 적용 전 원래 요청 수량
        작성 날짜: 2026/08/22
        """
        # 수량 공식의 enum과 Decimal 입력을 계산 전에 fail closed한다.
        if not isinstance(side, OrderSide):
            raise TypeError("side must be an OrderSide")
        if not isinstance(split_ratio, Decimal) or not split_ratio.is_finite():
            raise TypeError("split_ratio must be a finite Decimal")
        if not isinstance(market_price, Decimal) or market_price <= Decimal("0"):
            raise ValueError("market_price must be a positive Decimal")

        # 전역 Decimal context와 무관하게 주문 수량도 ADR-004 Decimal128 정책으로 계산한다.
        with localcontext() as decimal_context:
            decimal_context.prec = 34
            decimal_context.rounding = ROUND_HALF_EVEN

            # BUY는 stream 반영 전 fill 보정을 포함한 free USDT를 결정 가격으로 환산한다.
            if side is OrderSide.BUY:
                free_quote = self._get_effective_free_balance(_QUOTE_ASSET)
                maximum_quantity = free_quote / market_price
                if quantity_override is not None:
                    natural_quantity = min(
                        quantity_override,
                        maximum_quantity,
                    )
                else:
                    natural_quantity = (
                        free_quote * split_ratio / market_price
                    )
            else:
                # SELL은 authoritative Position과 확인된 fill의 free ETH 중 작은 값만 사용한다.
                position = self._require_position()
                free_base = self._get_effective_free_balance(_BASE_ASSET)
                maximum_sell_quantity = min(position.quantity, free_base)
                if quantity_override is not None:
                    natural_quantity = min(
                        quantity_override,
                        maximum_sell_quantity,
                    )
                elif force_sell:
                    natural_quantity = maximum_sell_quantity
                else:
                    natural_quantity = maximum_sell_quantity * split_ratio

        # STOP/recovery force SELL은 Position/free balance가 상한이고 entry quote cap을 적용하지 않는다.
        if side is OrderSide.SELL and force_sell:
            return natural_quantity

        return self._apply_order_notional_ceiling(
            natural_quantity,
            market_price,
        )  # BUY와 일반 SELL cap은 Order/journal identity를 만들기 전에 승인 수량에 포함한다.

    def _apply_order_notional_ceiling(
        self,
        quantity: Decimal,
        market_price: Decimal,
    ) -> Decimal:
        """
        함수 이름: _apply_order_notional_ceiling()
        기능: 선택 quote 상한을 decision price 기준 BUY 또는 일반 SELL base 수량에 보수적으로 적용한다.
        인자: quantity -> Account·Position·split으로 계산한 자연 주문 수량
            market_price -> 수량 상한을 quote 금액으로 환산할 결정 시점 가격
        반환값: 설정된 quote 상한 이하로 내림 제한한 base 수량
        작성 날짜: 2026/08/23
        """
        # Factory가 Testnet order mode에만 주입하므로 None에서는 기존 fake 계산을 그대로 보존한다.
        maximum_notional = self._maximum_order_notional
        if maximum_notional is None:
            return quantity

        # 양수 값의 나눗셈을 ROUND_DOWN해 유한 Decimal 정밀도 때문에 estimate가 cap을 넘지 않게 한다.
        with localcontext() as decimal_context:
            decimal_context.prec = 34
            decimal_context.rounding = ROUND_DOWN
            maximum_quantity = maximum_notional / market_price

        return min(quantity, maximum_quantity)  # REST prepare의 동일 cap 검사는 방어 계층으로 남긴다.

    def _get_effective_free_balance(self, asset: str) -> Decimal:
        """
        함수 이름: _get_effective_free_balance()
        기능: Account stream이 아직 반영하지 않은 확정 fill 변화를 원 자산 free 잔액에 합성한다.
        인자: asset -> ETH 또는 USDT
        반환값: 중복 사용을 차단할 0 이상 Decimal free 잔액
        작성 날짜: 2026/08/22
        """
        if asset not in (_BASE_ASSET, _QUOTE_ASSET):
            raise ValueError("effective free balance supports only ETH and USDT")

        balance = self._account.balances.get(asset)
        observed_free = Decimal("0") if balance is None else balance.free
        overlay = self._account_free_overlays.get(asset)
        if overlay is None:
            return observed_free

        # 해당 자산의 raw free가 바뀌면 account stream이 이전 fill을 포함한 새 절대값을 준 것으로 본다.
        if observed_free != overlay.observed_free:
            self._account_free_overlays.pop(asset, None)
            return observed_free

        with localcontext() as decimal_context:
            decimal_context.prec = 34
            decimal_context.rounding = ROUND_HALF_EVEN
            return max(Decimal("0"), observed_free + overlay.adjustment)

    def _apply_account_fill_overlay(
        self,
        side: OrderSide,
        fills: tuple[Fill, ...],
    ) -> None:
        """
        함수 이름: _apply_account_fill_overlay()
        기능: 새로 Position에 반영한 fill의 base·quote·fee 변화를 account stream 대기 overlay에 더한다.
        인자: side -> 체결 주문 방향
            fills -> 이번에 처음 반영한 fill tuple
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # Fill domain 검증을 통과한 값만 받으므로 두 자산 변화를 Decimal128로 합산한다.
        with localcontext() as decimal_context:
            decimal_context.prec = 34
            decimal_context.rounding = ROUND_HALF_EVEN
            base_adjustment = sum(
                (fill.quantity for fill in fills),
                start=Decimal("0"),
            )
            quote_adjustment = sum(
                (fill.executed_amount for fill in fills),
                start=Decimal("0"),
            )
            if side is OrderSide.SELL:
                base_adjustment = -base_adjustment
            else:
                quote_adjustment = -quote_adjustment
            adjustments = {
                _BASE_ASSET: base_adjustment,
                _QUOTE_ASSET: quote_adjustment,
            }
            # 실제 수수료는 원자산에서만 차감하며 BNB 평가액을 USDT 잔액에서 빼지 않는다.
            for fill in fills:
                adjustments[fill.fee_asset] = adjustments.get(fill.fee_asset, Decimal("0")) - fill.fee_amount
            for asset, adjustment in adjustments.items():
                balance = self._account.balances.get(asset)
                observed_free = Decimal("0") if balance is None else balance.free
                overlay = self._account_free_overlays.get(asset)
                if overlay is None or overlay.observed_free != observed_free:
                    overlay = _AccountFreeOverlay(observed_free=observed_free)
                    self._account_free_overlays[asset] = overlay
                overlay.adjustment += adjustment

    def _create_client_order_id(
        self,
        intent_id: str,
        submission_attempt: int,
    ) -> str:
        """
        함수 이름: _create_client_order_id()
        기능: 같은 session·intent·attempt에서 결정론적이고 process 재시작 간 고유한 ID를 만든다.
        인자: intent_id -> STM이 보존하는 원 주문 의도 ID
            submission_attempt -> 0부터 증가하는 제출 시도 번호
        반환값: fake/Binance adapter가 사용할 client order ID
        작성 날짜: 2026/08/23
        """
        # Session UUID를 hash 입력에 넣어 완료된 Binance client ID를 새 session이 재사용하지 않게 한다.
        session_id = self._session_id
        if session_id is None:
            raise RuntimeError("client order ID requires an active session ID")
        digest_input = f"{session_id}\x00{intent_id}".encode("utf-8")
        digest = hashlib.sha256(digest_input).hexdigest()[:24]
        return f"bat-{digest}-{submission_attempt}"

    def _handle_order_result(
        self,
        state: _OrderExecutionState,
        result: OrderResult,
        *,
        initial: bool,
        schedule_from: datetime | None = None,
    ) -> tuple[TradingEvent, ...]:
        """
        함수 이름: _handle_order_result()
        기능: 최초·재조회 결과를 Order에 반영하고 새 fill delta와 terminal 저장을 조정한다.
        인자: state -> 같은 client ID의 Controller 실행 상태
            result -> Gateway의 normalized OrderResult
            initial -> apply 또는 reapply 경계 선택 여부
            schedule_from -> 후속 query 예약의 authoritative 관측 시각 또는 None
        반환값: durable terminal outcome tuple 또는 active이면 빈 tuple
        작성 날짜: 2026/08/22
        """
        # mutable aggregate와 Gateway 결과 및 최초/후속 경계를 적용 전에 검증한다.
        if not isinstance(state, _OrderExecutionState):
            raise TypeError("state must be an _OrderExecutionState")
        if not isinstance(result, OrderResult):
            raise TypeError("result must be an OrderResult")
        if type(initial) is not bool:
            raise TypeError("initial must be a bool")

        # Raw 응답 대신 정규화된 체결·수수료와 허용된 API 실패 코드만 기록한다.
        self._record_diagnostic(
            "order_result_received", level="WARNING" if result.failure_reason else "INFO",
            initial=initial, order=state.order, result=result,
            failure=normalize_order_failure(result.failure_reason),
        )

        # apply/reapply 전체를 예외 경계로 묶어 충돌 시 거래소 사실을 임의 보정하지 않는다.
        order = state.order
        message_id = "7" if initial else "9"
        version_before = self._context.version
        completed_durable_terminal_replay = False
        completed_durable_stale_no_fill_replay = False

        # Testnet reset의 숫자 order ID 재사용은 새 client execution을 Position에 적용하기 전에 막는다.
        if result.exchange_order_id is not None:
            durable_trades = (
                self._require_trade_history_controller().trade_history.trades
            )
            durable_trade = next(
                (
                    trade
                    for trade in durable_trades
                    if trade.order_id == result.exchange_order_id
                ),
                None,
            )
            if (
                durable_trade is not None
                and durable_trade.client_order_id
                != result.client_order_id
            ):
                self._enter_order_reconciliation(
                    state,
                    OrderExecutionFailureCode.ORDER_RESULT_INVALID,
                    message_id=message_id,
                )
                return ()  # Pair 충돌 상태와 durable sidecar를 남겨 신규 submit/retry를 차단한다.
            # Trade와 REMOVE 뒤에는 transport별 시각을 제외한 회계 사실이 같은 replay만 수용한다.
            completed_durable_state = (
                durable_trade is not None
                and state.recovery_lifecycle
                is PendingOrderRecoveryLifecycle.HISTORY_COMMITTED
                and not state.pending_recovery_pending
            )
            completed_durable_terminal_replay = (
                completed_durable_state
                and result.status in TERMINAL_ORDER_STATUSES
                and state.order.status is result.status
                and _order_result_accounting_confirms_trade(
                    result,
                    durable_trade,
                )
            )  # 동일 회계라도 FILLED·CANCELED 같은 terminal 상태가 바뀌면 멱등 replay가 아니다.
            completed_durable_stale_no_fill_replay = (
                completed_durable_state
                and result.status not in TERMINAL_ORDER_STATUSES
                and not result.fills
                and result.client_order_id == durable_trade.client_order_id
                and result.symbol == durable_trade.symbol
            )
            if (
                durable_trade is not None
                and result.status in TERMINAL_ORDER_STATUSES
                and not completed_durable_terminal_replay
                and not _order_result_exactly_confirms_trade(
                    result,
                    durable_trade,
                )
            ):
                self._enter_order_reconciliation(
                    state,
                    OrderExecutionFailureCode.ORDER_RESULT_INVALID,
                    message_id=message_id,
                )
                return ()  # 미완료 lifecycle이나 회계 불일치 terminal은 기존 strict 재조정으로 보낸다.
        if (
            completed_durable_terminal_replay
            or completed_durable_stale_no_fill_replay
        ):
            # REST FULL이 먼저 끝난 뒤 도착한 같은 주문의 과거 NEW와 terminal replay는 durable 사실을 되돌리지 않는다.
            return ()  # 회계 불일치 terminal과 fill이 있는 비terminal 결과는 위 보수적 재조정 경계를 유지한다.
        try:
            if initial:
                order.apply_order_result(result)
            else:
                order.reapply_order_result(result)
        except Exception:
            self._enter_order_reconciliation(
                state,
                OrderExecutionFailureCode.ORDER_RESULT_INVALID,
                message_id=message_id,
            )
            return ()

        # Typed pre-matching rejection은 후속 부재 조회와 결합할 별도 durable 증거로 보존한다.
        if (
            initial
            and result.status is OrderStatus.REJECTED
            and not result.fills
            and result.failure_kind
            is OrderResultFailureKind.SUBMISSION_REJECTED
        ):
            state.submission_rejection_confirmable = True
        observed_lifecycle = self._select_pending_order_lifecycle(
            state,
            result,
        )
        if not self._transition_pending_order_recovery(
            state,
            observed_lifecycle,
        ):
            return ()  # Journal 상태가 관찰 사실을 따라잡지 못하면 Position·history를 전진시키지 않는다.
        self._append_order_trace(order, message_id, version_before)

        # exchange ID를 처음 확인한 순간부터 client와 exchange 두 index가 같은 state를 가리킨다.
        if order.exchange_order_id is not None:
            self._order_states_by_order_id[order.exchange_order_id] = state
        self._publish_pending_order(state)  # terminal effect가 실패해도 복구할 exchange/client ID를 먼저 게시한다.

        # 최초 terminal zero-fill은 제출 응답만 믿지 않고 같은 ID query의 terminal·0-fill로 확정한다.
        if state.awaiting_terminal_zero_confirmation:
            if result.status not in TERMINAL_ORDER_STATUSES:
                if order.unapplied_fills and not self._apply_unapplied_fills(state):
                    return ()
                self._schedule_order_query(
                    state,
                    result.retry_after,
                    scheduled_from=schedule_from,
                )
                return ()
            state.awaiting_terminal_zero_confirmation = False

        if not order.is_terminal:
            # active partial은 final Trade 없이 새 fill delta만 Position에 조기 반영한다.
            if order.unapplied_fills and not self._apply_unapplied_fills(state):
                return ()
            self._schedule_order_query(
                state,
                result.retry_after,
                scheduled_from=schedule_from,
            )
            return ()

        # terminal zero-fill은 Trade 없이 같은 intent retry가 가능한 concrete 실패로 끝낸다.
        self._scheduled_order_queries.pop(order.client_order_id, None)
        if not order.fills:
            if initial:
                state.awaiting_terminal_zero_confirmation = True
                self._schedule_order_query(
                    state,
                    result.retry_after,
                    scheduled_from=schedule_from,
                )
                return ()  # 조회 확인 전에는 실패 event나 새 client ID를 만들지 않는다.

            # 조회로 terminal zero-fill을 확인한 뒤에만 제출 전 recovery journal을 제거한다.
            if not self._delete_pending_order_recovery(state):
                return ()

            # 일반 retry는 직전 요청 수량을 보존해 residual SELL에 split ratio를 다시 적용하지 않는다.
            if not state.force_sell and not state.stop_after_reconciliation:
                self._quantity_overrides_by_intent[order.intent_id] = (
                    order.requested_quantity
                )  # 다음 attempt에서도 같은 intent 수량을 현재 Account/Position 한도로만 제한한다.
            self._context.update_pending_order(
                None,
                preserve_intent_id=not state.force_sell,
            )
            if state.stop_after_reconciliation:
                if (
                    self._requires_manual_kill_cleanup_locked()
                    and self._status
                    is TradingSessionStatus.RECONCILIATION_REQUIRED
                ):
                    state.stop_followup_started = False
                    return ()  # RECON gate에서 terminal만 반영하고 canonical STOP 재개 전 SELL을 만들지 않는다.
                if self._require_position().quantity > Decimal("0"):
                    state.stop_followup_started = True
                    return self._force_sell_action(ForceSellAll())
                stop_event = self._create_order_outcome_event(
                    state,
                    succeeded=True,
                    force_outcome=True,
                )
                state.pending_outcome = stop_event
                return (stop_event,)
            failure_event = self._create_order_outcome_event(
                state,
                succeeded=False,
            )
            state.pending_outcome = failure_event
            return (failure_event,)

        # terminal 경로는 메시지 10 누적 summary를 먼저 고정한 뒤 새 delta를 Position에 반영한다.
        try:
            state.terminal_summary = order.build_execution_summary()
        except Exception:
            self._enter_order_reconciliation(
                state,
                OrderExecutionFailureCode.ORDER_RESULT_INVALID,
                message_id="10",
            )
            return ()
        self._append_order_trace(order, "10", self._context.version)
        if order.unapplied_fills and not self._apply_unapplied_fills(state):
            return ()

        return self._finalize_terminal_execution(state)

    def _select_pending_order_lifecycle(
        self,
        state: _OrderExecutionState,
        result: OrderResult,
    ) -> PendingOrderRecoveryLifecycle:
        """
        함수 이름: _select_pending_order_lifecycle()
        기능: 누적 Order 사실과 typed 거부 증거에 맞는 durable lifecycle을 선택한다.
        인자: state -> 현재 client order의 Controller 실행 상태
            result -> 방금 적용한 same-ID Gateway 결과
        반환값: 후퇴하지 않는 PendingOrderRecoveryLifecycle
        작성 날짜: 2026/08/25
        """
        # Lifecycle 선택에 필요한 실행 상태와 Gateway 결과를 canonical domain type으로 제한한다.
        if not isinstance(state, _OrderExecutionState):
            raise TypeError("state must be an _OrderExecutionState")
        if not isinstance(result, OrderResult):
            raise TypeError("result must be an OrderResult")

        # History fsync가 이미 증명된 state는 same-ID query 중에도 terminal 이전으로 후퇴하지 않는다.
        if (
            state.recovery_lifecycle
            is PendingOrderRecoveryLifecycle.HISTORY_COMMITTED
        ):
            return PendingOrderRecoveryLifecycle.HISTORY_COMMITTED

        # Confirmed rejection은 일반 terminal과 분리해 네 번의 exact absence만 REMOVE 권한을 얻게 한다.
        if state.submission_rejection_confirmable:
            return (
                PendingOrderRecoveryLifecycle.SUBMISSION_REJECTED_CONFIRMED
            )
        order = state.order
        if order.is_terminal:
            return PendingOrderRecoveryLifecycle.TERMINAL
        if order.fills:
            return PendingOrderRecoveryLifecycle.PARTIAL
        if order.status is OrderStatus.UNKNOWN:
            return PendingOrderRecoveryLifecycle.UNKNOWN

        # 이전 timeout을 journal에 남겨 둔 주문은 fill 없는 active 확인만으로 durable 불명 경계를 되돌리지 않는다.
        if (
            state.recovery_lifecycle
            is PendingOrderRecoveryLifecycle.UNKNOWN
        ):
            return PendingOrderRecoveryLifecycle.UNKNOWN  # Memory Order는 NEW로 전진하되 partial·terminal 전까지 same-ID query gate를 유지한다.

        return PendingOrderRecoveryLifecycle.SUBMITTED  # NEW·PENDING 주문은 제출 성공 identity를 계속 유지한다.

    def _transition_pending_order_recovery(
        self,
        state: _OrderExecutionState,
        lifecycle: PendingOrderRecoveryLifecycle,
    ) -> bool:
        """
        함수 이름: _transition_pending_order_recovery()
        기능: 주문 lifecycle을 fsync하고 실패 시 외부 mutation gate를 유지한다.
        인자: state -> durable pending Order를 소유한 실행 상태
            lifecycle -> 새로 관찰한 canonical lifecycle
        반환값: fsync 성공 또는 recovery 미사용이면 True, 실패하면 False
        작성 날짜: 2026/08/25
        """
        # 잘못된 state/lifecycle이 durable journal transition을 시작하기 전에 exact type을 확인한다.
        if not isinstance(state, _OrderExecutionState):
            raise TypeError("state must be an _OrderExecutionState")
        if not isinstance(lifecycle, PendingOrderRecoveryLifecycle):
            raise TypeError("lifecycle must be a PendingOrderRecoveryLifecycle")
        if not self._pending_order_recovery_enabled:
            state.recovery_lifecycle = lifecycle
            return True  # History-only fake는 기존 in-memory pipeline을 그대로 사용한다.

        history_controller = self._require_trade_history_controller()
        try:
            history_controller.transition_pending_order_lifecycle(
                state.order.client_order_id,
                lifecycle,
            )
        except Exception:
            state.pending_recovery_pending = True
            self._enter_order_reconciliation(
                state,
                OrderExecutionFailureCode.HISTORY_PERSISTENCE_FAILED,
                message_id=None,
            )
            return False  # 쓰기 완료 여부가 불명하므로 상태를 메모리에서 추측하지 않는다.

        state.recovery_lifecycle = lifecycle
        state.pending_recovery_pending = True
        return True  # REMOVE fsync 전이므로 lifecycle commit 후에도 pending marker는 유지한다.

    def _apply_unapplied_fills(self, state: _OrderExecutionState) -> bool:
        """
        함수 이름: _apply_unapplied_fills()
        기능: Order에서 아직 반영하지 않은 fill subset만 Position과 Context에 원자 적용한다.
        인자: state -> Order와 누적 SELL 배분 원가를 가진 실행 상태
        반환값: Position·Context 반영 성공 여부
        작성 날짜: 2026/08/22
        """
        # Order가 추적한 applied key를 기준으로 이번 Position delta만 분리한다.
        order = state.order
        unapplied_fills = order.unapplied_fills
        if not unapplied_fills:
            return True

        # active partial에도 terminal 요구를 끄고 실제 새 fill subset만 같은 summary로 만든다.
        try:
            delta_summary = order.build_execution_summary(
                fills=unapplied_fills,
                require_terminal=False,
            )
        except Exception:
            self._enter_order_reconciliation(
                state,
                OrderExecutionFailureCode.ORDER_RESULT_INVALID,
                message_id="10",
            )
            return False
        if state.terminal_summary is None:
            self._append_order_trace(
                order,
                "10",
                self._context.version,
            )  # active partial도 delta summary 생성 직후에 11/12보다 먼저 기록한다.

        position = self._require_position()
        version_before = self._context.version
        allocated_cost_basis = Decimal("0")
        try:
            if order.side is OrderSide.SELL:
                allocated_cost_basis = position.get_cost_basis(
                    delta_summary.executed_quantity
                )
                self._append_order_trace(order, "11", version_before)

            # 메시지 12는 원가 고정 뒤 실행하고 fill applied 표시는 성공 이후에만 기록한다.
            position.apply_execution(delta_summary)
            self._apply_account_fill_overlay(order.side, unapplied_fills)
            order.mark_fills_applied(unapplied_fills)
            if order.side is OrderSide.SELL:
                with localcontext() as decimal_context:
                    decimal_context.prec = 34
                    decimal_context.rounding = ROUND_HALF_EVEN
                    state.allocated_cost_basis = (
                        state.allocated_cost_basis + allocated_cost_basis
                    )  # partial별 사전 원가 합도 ADR-004 Decimal128 정밀도를 유지한다.
            self._publish_position_to_context(position)
            if not self._publish_case_b_exit_result(state):
                return False  # 청산 의도가 일치해야 Case B의 완료 feedback을 발행한다.
            if not self._publish_case_c_exit_result(state):
                # 종료 provenance 불일치에서는
                # terminal STM feedback을 발행하지 않는다.
                return False
            self._append_order_trace(order, "12", version_before)
        except Exception:
            self._enter_order_reconciliation(
                state,
                OrderExecutionFailureCode.POSITION_UPDATE_FAILED,
                message_id="12",
            )
            return False

        return True

    def _publish_position_to_context(self, position: Position) -> None:
        """
        함수 이름: _publish_position_to_context()
        기능: mutable Position의 한 snapshot을 TradingContext 수량·owner와 함께 publish한다.
        인자: position -> 체결 반영을 마친 authoritative Position
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # Position lock에서 읽은 한 snapshot을 기존 Context DTO 형식으로 변환한다.
        position_state = position.get_snapshot()
        context_snapshot = PositionSnapshot(
            quantity=position_state.quantity,
            entry_price=(
                position_state.average_entry_price
                if position_state.quantity > Decimal("0")
                else None
            ),
        )

        # start prerequisite와 STM Guard가 같은 Position 수량과 owner를 보도록 한 번에 갱신한다.
        self._position_snapshot = context_snapshot
        self._context.update_position(
            context_snapshot,
            position_owner=position_state.owner,
        )  # buy fill 전에는 호출되지 않으므로 owner도 실제 fill 이후에만 설정된다.

    def _publish_case_b_exit_result(
        self,
        state: _OrderExecutionState,
    ) -> bool:
        """
        함수 이름: _publish_case_b_exit_result()
        기능: Case B 전량 매도의 실제 종료 사유를 게시해 청산 완료 상태 전이를 연결한다.
        인자: state -> 방금 Position에 fill을 반영한 주문 실행 상태
        반환값: 종료 사유를 게시했거나 대상이 아니면 True, 의도가 불일치하면 False
        작성 날짜: 2026/09/09
        """
        # 전역 중지와 부분 청산은 각각 기존 중지 흐름과 잔여 포지션 관리를 계속한다.
        order = state.order
        if (
            state.force_sell
            or state.stop_after_reconciliation
            or order.side is not OrderSide.SELL
            or order.strategy is not StrategyType.CASE_B
            or self._require_position().quantity > Decimal("0")
        ):
            return True

        # 최초 청산 의도와 실제 체결 주문이 일치할 때만 PB-23F에서 사용할 결과를 고정한다.
        runtime = self._context.runtime
        exit_reason = runtime.pending_exit_reason
        if (
            exit_reason is None
            or order.exit_reason is not exit_reason
            or runtime.case_b_exit_reason not in (None, exit_reason)
        ):
            self._enter_order_reconciliation(
                state,
                OrderExecutionFailureCode.ORDER_RESULT_INVALID,
                message_id="12",
            )
            return False

        # 같은 체결의 재조회나 저장 재시도는 이미 확정한 종료 사유를 변경하지 않는다.
        if runtime.case_b_exit_reason is None:
            self._context.apply_runtime_patch(patch(case_b_exit_reason=exit_reason))
        return True  # 내역 저장 후 발생하는 CASE_B_SELL_FILLED가 이 결과를 소비한다.

    def _publish_case_c_exit_result(
        self,
        state: _OrderExecutionState,
    ) -> bool:
        """
        함수 이름: _publish_case_c_exit_result()
        기능: 일반 Case C 전량 SELL 체결 시 실제 마지막 fill의 %B와 종료 사유를 고정한다.
        인자: state -> 방금 Position에 fill을 반영한 주문 실행 상태
        반환값: 종료 provenance가 일관되게 게시됐거나 대상이 아니면 True,
            불일치면 False
        작성 날짜: 2026/08/29
        """
        if not isinstance(state, _OrderExecutionState):
            raise TypeError("state must be an _OrderExecutionState")

        # 전역 STOP과 STOP 중 기존 주문 reconciliation은
        # 전용 G-06F 결과를 사용하므로 건드리지 않는다.
        order = state.order
        if (
            state.force_sell
            or state.stop_after_reconciliation
            or order.side is not OrderSide.SELL
            or order.strategy is not StrategyType.CASE_C
            or self._require_position().quantity > Decimal("0")
        ):
            return True

        # 최초 청산 의도와 실제 제출 Order가 같은 사유·%B를 보존한 경우에만
        # 종료 결과를 확정한다.
        runtime = self._context.runtime
        exit_reason = runtime.pending_exit_reason
        intent_pct_b = order.exit_pct_b_at_intent
        if (
            exit_reason is None
            or order.exit_reason is not exit_reason
            or intent_pct_b is None
            or runtime.pending_exit_pct_b != intent_pct_b
        ):
            self._enter_order_reconciliation(
                state,
                OrderExecutionFailureCode.ORDER_RESULT_INVALID,
                message_id="12",
            )
            return False

        # 계산 불가(None)도 최초 종료 결과다. 재조회·저장 재시도에서 미래 시세로 덮어쓰지 않는다.
        if runtime.case_c_exit_reason is exit_reason:
            return True
        if (
            runtime.case_c_exit_reason is not None
            or runtime.case_c_exit_pct_b is not None
        ):
            self._enter_order_reconciliation(
                state,
                OrderExecutionFailureCode.ORDER_RESULT_INVALID,
                message_id="12",
            )
            return False

        # 실제 마지막 fill의 가격과 그 봉 이전 이력만 사용한다. 근거 부재는 wait-only 인계다.
        # 같은 밀리초의 Binance fill은 숫자 trade ID로 순서를 정한다.
        # 비숫자 ID를 쓰는 gateway에서는 누적 fill 수신 순서를 보조 기준으로 사용한다.
        _last_index, last_fill = max(enumerate(order.fills), key=lambda item: (
            item[1].executed_at,
            int(item[1].trade_id) if item[1].trade_id.isdecimal() else item[0],
        ))
        exit_pct_b = calculate_execution_pct_b(
            self._market_snapshot, last_fill.executed_at, last_fill.price,
        )
        self._context.apply_runtime_patch(
            patch(
                case_c_exit_reason=exit_reason,
                case_c_exit_pct_b=exit_pct_b,
            )
        )
        return True

    def _publish_pending_order(self, state: _OrderExecutionState) -> None:
        """
        함수 이름: _publish_pending_order()
        기능: active/UNKNOWN Order identity와 partial 증거를 Context pending snapshot에 반영한다.
        인자: state -> publish할 Order 실행 상태
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # exchange ID가 있으면 우선 사용하고 attempt 종류는 제출 번호에서 결정한다.
        order = state.order
        pending_identifier = order.exchange_order_id or order.client_order_id
        attempt_kind = (
            OrderAttemptKind.INITIAL
            if order.submission_attempt == 0
            else OrderAttemptKind.RETRY
        )
        self._context.update_pending_order(
            PendingOrderSnapshot(
                order_id=pending_identifier,
                strategy=order.strategy,
                side=order.side,
                attempt_kind=attempt_kind,
                has_partial_fill=bool(order.fills),
                status_unknown=order.status is OrderStatus.UNKNOWN,
            )
        )  # runtime intent ID는 앞선 STM patch 값을 그대로 유지한다.

    def _schedule_order_query(
        self,
        state: _OrderExecutionState,
        retry_after: timedelta | None,
        *,
        scheduled_from: datetime | None = None,
    ) -> None:
        """
        함수 이름: _schedule_order_query()
        기능: 같은 client/exchange Order의 다음 1·2·4·8초 reconciliation 조회를 예약한다.
        인자: state -> 조회할 기존 Order 상태
            retry_after -> Gateway가 제공한 축소하지 않은 rate-limit 대기 또는 None
            scheduled_from -> 직전 query를 관찰한 scheduler 시각 또는 None
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # 주입 시각도 scheduler와 같이 timezone-aware인지 예약 전에 검증한다.
        if scheduled_from is not None and (
            not isinstance(scheduled_from, datetime)
            or scheduled_from.tzinfo is None
            or scheduled_from.utcoffset() is None
        ):
            raise ValueError(
                "scheduled_from must be a timezone-aware datetime or None"
            )

        # 네 번의 실제 query를 모두 소비한 주문은 새 schedule 없이 운영 lock으로 전환한다.
        if state.reconciliation_attempts >= len(_ORDER_RECONCILIATION_DELAYS):
            self._enter_order_reconciliation(
                state,
                OrderExecutionFailureCode.QUERY_BUDGET_EXHAUSTED,
                message_id=None,
            )
            return

        # 공식 Retry-After가 기본 exponential delay보다 길면 그 제한을 우선한다.
        selected_delay = self._jittered_order_retry_delay(
            _ORDER_RECONCILIATION_DELAYS[state.reconciliation_attempts]
        )
        if retry_after is not None:
            selected_delay = max(selected_delay, retry_after)
        schedule_base = scheduled_from or self._clock()
        due_at = schedule_base + selected_delay
        self._scheduled_order_queries[state.order.client_order_id] = (
            _ScheduledOrderQuery(
                client_order_id=state.order.client_order_id,
                due_at=due_at,
            )
        )  # 같은 client ID schedule은 최신 시간 제약 하나로 대체한다.
        self._record_diagnostic(
            "order_query_scheduled", client_order_id=state.order.client_order_id,
            order_id=state.order.exchange_order_id, due_at=due_at,
            delay_seconds=selected_delay, reconciliation_attempts=state.reconciliation_attempts,
        )
        self._request_event_runtime_processing()  # 단일 worker가 due polling을 즉시 시작하게 한다.

    def _finalize_terminal_execution(
        self,
        state: _OrderExecutionState,
    ) -> tuple[TradingEvent, ...]:
        """
        함수 이름: _finalize_terminal_execution()
        기능: terminal 누적 summary를 한 Trade로 durable 기록한 뒤에만 concrete outcome을 만든다.
        인자: state -> Position delta 반영을 끝낸 terminal Order 상태
        반환값: 저장 성공 뒤 생성한 concrete outcome tuple
        작성 날짜: 2026/08/22
        """
        # terminal summary와 SELL 원가를 history operation 하나의 입력으로 준비한다.
        order = state.order
        version_before = self._context.version
        summary = state.terminal_summary
        if summary is None:
            try:
                summary = order.build_execution_summary()
            except Exception:
                self._enter_order_reconciliation(
                    state,
                    OrderExecutionFailureCode.ORDER_RESULT_INVALID,
                    message_id="10",
                )
                return ()
            state.terminal_summary = summary
            self._append_order_trace(order, "10", version_before)

        # SELL만 Position 적용 전에 누적한 배분 원가를 history 계산에 전달한다.
        history_controller = self._require_trade_history_controller()
        allocated_cost_basis = (
            state.allocated_cost_basis
            if order.side is OrderSide.SELL
            else None
        )
        try:
            committed_trade = history_controller.record_order_execution(
                order,
                summary,
                allocated_cost_basis,
            )
        except Exception:
            # Position과 exchange 사실은 되돌리지 않고 같은 order save-only retry 상태로 잠근다.
            storage_is_pending = (
                summary.order_id in history_controller.dirty_order_ids
            )
            if storage_is_pending:
                state.persistence_pending = True
                self._persistence_states_by_order_id[summary.order_id] = state
            selected_failure_code = (
                OrderExecutionFailureCode.HISTORY_PERSISTENCE_FAILED
                if storage_is_pending
                else OrderExecutionFailureCode.ORDER_RESULT_INVALID
            )
            self._append_order_trace(
                order,
                "13",
                self._context.version,
                failure_code=selected_failure_code,
            )
            if storage_is_pending:
                # dirty candidate는 13.1~13.4가 성공하고 filesystem durable 경계만 실패했음을 증명한다.
                if order.side is OrderSide.SELL:
                    self._append_order_trace(
                        order,
                        "13.1",
                        self._context.version,
                    )
                for message_id in ("13.2", "13.3", "13.4"):
                    self._append_order_trace(
                        order,
                        message_id,
                        self._context.version,
                    )
                for message_id in ("13.5", "13.5.1"):
                    self._append_order_trace(
                        order,
                        message_id,
                        self._context.version,
                        failure_code=selected_failure_code,
                    )
            self._enter_order_reconciliation(
                state,
                selected_failure_code,
                message_id=None,
            )
            return ()

        # 실제 durable Trade와 수수료 반영 성과를 남겨 하루 손익도 로그만으로 복원하게 한다.
        self._record_diagnostic("trade_committed", trade=committed_trade, performance=history_controller.performance)

        # Trade JSONL fsync 후 sidecar에 HISTORY_COMMITTED를 남겨 crash-before-REMOVE를 명시적으로 식별한다.
        if not self._transition_pending_order_recovery(
            state,
            PendingOrderRecoveryLifecycle.HISTORY_COMMITTED,
        ):
            return ()  # History는 되돌리지 않고 same-ID/history 정확 대조가 끝날 때까지 gate를 잠근다.

        # Controller operation 성공 뒤 내부 collaborator 순서를 동일 trace 순서로 공개한다.
        self._append_order_trace(order, "13", self._context.version)
        if order.side is OrderSide.SELL:
            self._append_order_trace(order, "13.1", self._context.version)
        self._append_order_trace(order, "13.2", self._context.version)
        self._append_order_trace(order, "13.3", self._context.version)
        self._append_order_trace(order, "13.4", self._context.version)
        self._append_order_trace(order, "13.5", self._context.version)
        self._append_order_trace(order, "13.5.1", self._context.version)

        return self._complete_terminal_after_history(state)

    def _complete_terminal_after_history(
        self,
        state: _OrderExecutionState,
    ) -> tuple[TradingEvent, ...]:
        """
        함수 이름: _complete_terminal_after_history()
        기능: durable history 성공 뒤 pending을 해제하고 일반·stop outcome을 결정한다.
        인자: state -> terminal summary와 저장 성공을 가진 실행 상태
        반환값: 다음 microstep에 전달할 concrete outcome tuple
        작성 날짜: 2026/08/22
        """
        # History durable 성공 뒤 recovery journal 삭제까지 끝나야 외부 성공을 게시할 수 있다.
        order = state.order
        try:
            if order.side is OrderSide.SELL:
                self._settle_residual_position()
        except Exception:
            self._enter_order_reconciliation(state, OrderExecutionFailureCode.HISTORY_PERSISTENCE_FAILED, message_id=None)
            return ()  # Residual fsync 불명은 pending을 보존하고 새 주문을 차단한다.
        if not self._delete_pending_order_recovery(state):
            return ()

        # 두 durable 경계가 끝난 주문의 save/query marker와 Context pending identity를 해제한다.
        state.persistence_pending = False
        if order.exchange_order_id is not None:
            self._persistence_states_by_order_id.pop(order.exchange_order_id, None)
        self._scheduled_order_queries.pop(order.client_order_id, None)
        self._context.update_pending_order(
            None,
            preserve_intent_id=(
                not state.force_sell
                and order.side is OrderSide.SELL
                and self._require_position().quantity > Decimal("0")
            ),
        )
        if (
            not state.force_sell
            and not state.stop_after_reconciliation
            and order.side is OrderSide.BUY
        ):
            self._context.apply_runtime_patch(
                patch(trading_phase=TradingPhase.IDLE)
            )  # 성공 BUY은 Position owner가 반영됐으므로 entry pending 단계를 종료한다.

        # STOP pending reconciliation은 일반 전략 event를 내지 않고 잔량 force-sell까지 이어간다.
        if state.stop_after_reconciliation:
            if (
                self._requires_manual_kill_cleanup_locked()
                and self._status
                is TradingSessionStatus.RECONCILIATION_REQUIRED
            ):
                state.stop_followup_started = False
                return ()  # Partial/history는 확정하되 fresh stream·market gate 전 청산 POST는 보류한다.
            if self._require_position().quantity > Decimal("0"):
                state.stop_followup_started = True
                return self._force_sell_action(ForceSellAll())
            stop_event = self._create_order_outcome_event(
                state,
                succeeded=True,
                force_outcome=True,
            )
            state.pending_outcome = stop_event
            return (stop_event,)

        if state.force_sell:
            force_succeeded = self._require_position().quantity == Decimal("0")
            force_event = self._create_order_outcome_event(
                state,
                succeeded=force_succeeded,
            )
            state.pending_outcome = force_event
            return (force_event,)

        # BUY terminal partial은 top-up하지 않고 성공하며 SELL은 잔량 전체를 같은 intent로 retry한다.
        if order.side is OrderSide.BUY:
            outcome = self._create_order_outcome_event(state, succeeded=True)
        elif self._require_position().quantity == Decimal("0"):
            outcome = self._create_order_outcome_event(state, succeeded=True)
        else:
            self._quantity_overrides_by_intent[order.intent_id] = (
                self._require_position().quantity
            )
            outcome = self._create_order_outcome_event(state, succeeded=False)
        state.pending_outcome = outcome

        return (outcome,)

    def _delete_pending_order_recovery(
        self,
        state: _OrderExecutionState,
    ) -> bool:
        """
        함수 이름: _delete_pending_order_recovery()
        기능: terminal 확인과 history 저장 뒤 제출 전 recovery journal 항목을 멱등 제거한다.
        인자: state -> 제거할 client order ID와 failure state를 가진 실행 상태
        반환값: journal 제거 또는 미지원 fake port이면 True, 실패하면 False
        작성 날짜: 2026/08/22
        """
        # Fake repository는 sidecar 계약이 없으므로 기존 결정론적 pipeline을 그대로 유지한다.
        history_controller = self._require_trade_history_controller()
        if not self._pending_order_recovery_enabled:
            return True

        # 삭제 실패 시 exchange와 history 사실을 되돌리지 않고 재시작 재조정 대상으로 잠근다.
        try:
            history_controller.delete_pending_order(
                state.order.client_order_id
            )
        except Exception:
            state.pending_recovery_pending = True
            self._enter_order_reconciliation(
                state,
                OrderExecutionFailureCode.HISTORY_PERSISTENCE_FAILED,
                message_id=None,
            )
            return False

        state.pending_recovery_pending = False
        return True  # 멱등 REMOVE event가 durable해진 뒤에만 terminal outcome을 허용한다.

    def _create_order_outcome_event(
        self,
        state: _OrderExecutionState,
        *,
        succeeded: bool,
        force_outcome: bool = False,
    ) -> TradingEvent:
        """
        함수 이름: _create_order_outcome_event()
        기능: strategy·side·force 구분에 맞는 concrete TradingEvent와 typed payload를 만든다.
        인자: state -> 완료된 Order 실행 상태
            succeeded -> Position/history까지 성공했는지 여부
            force_outcome -> pending stop 완료를 force 결과로 표시할지 여부
        반환값: stable ID를 가진 ORDER_OUTCOME TradingEvent
        작성 날짜: 2026/08/22
        """
        # outcome flag와 attempt 정보를 typed payload mapping 전에 확정한다.
        if type(succeeded) is not bool or type(force_outcome) is not bool:
            raise TypeError("outcome flags must be bool values")
        order = state.order
        attempt_kind = (
            OrderAttemptKind.INITIAL
            if order.submission_attempt == 0
            else OrderAttemptKind.RETRY
        )

        # stop force-sell은 전략 CASE와 무관한 전역 concrete outcome/payload를 사용한다.
        if state.force_sell or force_outcome:
            event_type = (
                TradingEventType.FORCE_SELL_FINISHED
                if succeeded
                else TradingEventType.FORCE_SELL_FAILED
            )
            payload: object = ForceSellOutcomePayload(
                execution_applied=succeeded or bool(order.fills),
                history_persisted=(
                    not order.fills or not state.persistence_pending
                ),
                terminal_unfilled=not succeeded,
            )
        elif order.side is OrderSide.BUY:
            event_type = self._buy_outcome_type(order.strategy, succeeded)
            payload = BuyAttemptPayload(attempt_kind=attempt_kind)
        else:
            event_type = self._sell_outcome_type(order.strategy, succeeded)
            payload = SellAttemptPayload(attempt_kind=attempt_kind)

        # event ID는 같은 order/outcome replay가 serial queue dedup에서 한 번만 처리되게 한다.
        event = TradingEvent(
            event_type=event_type,
            occurred_at=self._clock(),
            priority=EventPriority.ORDER_OUTCOME,
            event_id=f"order-outcome-{order.client_order_id}-{event_type.value}",
            lower_event_id=self._context.runtime.lower_event_id,
            order_id=order.exchange_order_id or order.client_order_id,
            payload=payload,
        )
        return event

    @staticmethod
    def _buy_outcome_type(
        strategy: StrategyType,
        succeeded: bool,
    ) -> TradingEventType:
        """
        함수 이름: _buy_outcome_type()
        기능: BUY 전략과 성공 여부를 concrete Case B/C event type으로 변환한다.
        인자: strategy -> 주문 소유 전략
            succeeded -> terminal execution 성공 여부
        반환값: CASE_B 또는 CASE_C BUY 결과 type
        작성 날짜: 2026/08/22
        """
        # 두 지원 전략의 BUY 성공·실패 event를 명시적으로 나눈다.
        if strategy is StrategyType.CASE_B:
            return (
                TradingEventType.CASE_B_POSITION_OPENED
                if succeeded
                else TradingEventType.CASE_B_BUY_FAILED
            )
        return (
            TradingEventType.CASE_C_POSITION_OPENED
            if succeeded
            else TradingEventType.CASE_C_BUY_FAILED
        )

    @staticmethod
    def _sell_outcome_type(
        strategy: StrategyType,
        succeeded: bool,
    ) -> TradingEventType:
        """
        함수 이름: _sell_outcome_type()
        기능: SELL 전략과 성공 여부를 concrete Case B/C event type으로 변환한다.
        인자: strategy -> Position 소유 전략
            succeeded -> 전량 청산 및 저장 성공 여부
        반환값: CASE_B 또는 CASE_C SELL 결과 type
        작성 날짜: 2026/08/22
        """
        # 두 지원 전략의 SELL 성공·실패 event를 명시적으로 나눈다.
        if strategy is StrategyType.CASE_B:
            return (
                TradingEventType.CASE_B_SELL_FILLED
                if succeeded
                else TradingEventType.CASE_B_SELL_FAILED
            )
        return (
            TradingEventType.CASE_C_SELL_FILLED
            if succeeded
            else TradingEventType.CASE_C_SELL_FAILED
        )

    def trigger_order_reconciliation(
        self,
        *,
        occurred_at: datetime | None = None,
    ) -> tuple[TradingEvent, ...]:
        """
        함수 이름: trigger_order_reconciliation()
        기능: due인 same-order query와 force-sell retry를 한 번씩 실행해 outcome을 queue에 넣는다.
        인자: occurred_at -> deterministic scheduler 관측 UTC 시각 또는 None
        반환값: 이번 호출에서 생성·enqueue한 concrete outcome tuple
        작성 날짜: 2026/08/22
        """
        # scheduler 관측 시각을 검증한 뒤 session lock 안에서 due 작업만 소비한다.
        selected_time = occurred_at or self._clock()
        if not isinstance(selected_time, datetime):
            raise TypeError("occurred_at must be a datetime or None")
        if selected_time.tzinfo is None or selected_time.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")

        with self._session_lock:
            # 주문 reconciliation은 실행·중지·운영 lock 세 상태에서만 진행할 수 있다.
            if self._status not in (
                TradingSessionStatus.RUNNING,
                TradingSessionStatus.STOPPING,
                TradingSessionStatus.RECONCILIATION_REQUIRED,
            ):
                return ()

            # client ID 정렬로 같은 due 시각의 fake test 실행 순서를 결정론적으로 고정한다.
            due_queries = tuple(
                sorted(
                    (
                        scheduled_query
                        for scheduled_query in self._scheduled_order_queries.values()
                        if scheduled_query.due_at <= selected_time
                    ),
                    key=lambda scheduled_query: (
                        scheduled_query.due_at,
                        scheduled_query.client_order_id,
                    ),
                )
            )
            outcomes: list[TradingEvent] = []
            for scheduled_query in due_queries:
                self._scheduled_order_queries.pop(
                    scheduled_query.client_order_id,
                    None,
                )
                state = self._order_states_by_client_id.get(
                    scheduled_query.client_order_id
                )
                if state is None or (
                    state.order.is_terminal
                    and not state.awaiting_terminal_zero_confirmation
                ):
                    continue
                outcomes.extend(
                    self._query_existing_order(
                        state,
                        observed_at=selected_time,
                    )
                )

            # G-06R은 고정 3초가 지난 trigger에서만 새 client ID의 residual SELL을 제출한다.
            if (
                self._force_sell_retry_due_at is not None
                and self._force_sell_retry_due_at <= selected_time
            ):
                self._force_sell_retry_due_at = None
                outcomes.extend(self._submit_force_sell_retry())

            return self._enqueue_order_outcomes(outcomes)

    def _query_existing_order(
        self,
        state: _OrderExecutionState,
        *,
        observed_at: datetime | None = None,
    ) -> tuple[TradingEvent, ...]:
        """
        함수 이름: _query_existing_order()
        기능: 신규 제출 없이 state의 같은 Order ID를 한 번 조회하고 reapply한다.
        인자: state -> 기존 client/exchange ID를 소유한 실행 상태
            observed_at -> trigger가 이 query를 관찰한 결정론적 시각 또는 None
        반환값: terminal 완료 시 concrete outcome tuple
        작성 날짜: 2026/08/22
        """
        # 새 client ID를 만들지 않고 같은 Order의 최대 4회 query 예산을 먼저 검사한다.
        if state.reconciliation_attempts >= len(_ORDER_RECONCILIATION_DELAYS):
            self._enter_order_reconciliation(
                state,
                OrderExecutionFailureCode.QUERY_BUDGET_EXHAUSTED,
                message_id=None,
            )
            return ()

        # 실제 REST query를 시도하는 순간에 예산과 Case 2 추적을 함께 소비한다.
        order = state.order
        state.reconciliation_attempts += 1  # Retry-After가 있어도 실제 query 한 번은 예산을 소비한다.
        gateway_version = self._context.version
        self._record_diagnostic("order_query_started", order=order, reconciliation_attempts=state.reconciliation_attempts)
        # transport 예외는 체결 0으로 간주하지 않고 UNKNOWN으로 정규화해 same-order 조회를 계속한다.
        try:
            result = self._api_gateway.query_order_result(order)
        except Exception as error:
            for message_id in ("8", "8.1", "8.2"):
                self._append_order_trace(
                    order,
                    message_id,
                    gateway_version,
                    failure_code=(
                        OrderExecutionFailureCode.GATEWAY_REQUEST_FAILED
                    ),
                )
            result = OrderResult(
                symbol=order.symbol,
                client_order_id=order.client_order_id,
                exchange_order_id=order.exchange_order_id,
                status=OrderStatus.UNKNOWN,
                processed_at=self._clock(),
                failure_reason=type(error).__name__,
            )
        else:
            for message_id in ("8", "8.1", "8.2"):
                self._append_order_trace(
                    order,
                    message_id,
                    gateway_version,
                )

        # 명시적 submit rejection 뒤 모든 조회가 NO_SUCH_ORDER였는지 별도 typed count로 보존한다.
        if (
            state.submission_rejection_confirmable
            and result.status is OrderStatus.UNKNOWN
            and result.failure_kind is OrderResultFailureKind.ORDER_NOT_VISIBLE
        ):
            state.order_not_visible_observations += 1

        # 네 번 모두 NO_SUCH_ORDER인 경우에만 terminal zero-fill 증거로 승격한다.
        if (
            state.submission_rejection_confirmable
            and state.reconciliation_attempts
            == len(_ORDER_RECONCILIATION_DELAYS)
            and state.order_not_visible_observations
            == len(_ORDER_RECONCILIATION_DELAYS)
            and result.status is OrderStatus.UNKNOWN
            and result.failure_kind is OrderResultFailureKind.ORDER_NOT_VISIBLE
        ):
            result = OrderResult(
                symbol=order.symbol,
                client_order_id=order.client_order_id,
                exchange_order_id=order.exchange_order_id,
                status=OrderStatus.REJECTED,
                processed_at=result.processed_at,
                failure_reason="SUBMISSION_REJECTION_CONFIRMED_ABSENT",
                failure_kind=OrderResultFailureKind.SUBMISSION_REJECTED,
            )  # 문자열 오류가 아니라 typed submit·query 사실 조합으로만 terminal을 만든다.

        return self._handle_order_result(
            state,
            result,
            initial=False,
            schedule_from=observed_at,
        )

    def _cancel_pending_order_action(
        self,
        action: CancelPendingOrder,
    ) -> tuple[TradingEvent, ...]:
        """
        함수 이름: _cancel_pending_order_action()
        기능: STOP pending Order를 먼저 조회하고 active일 때만 같은 ID를 취소한다.
        인자: action -> 취소할 pending order ID와 사유
        반환값: terminal 반영과 stop completion에서 생성된 outcome tuple
        작성 날짜: 2026/08/22
        """
        # Context가 보존한 exchange/client ID를 Controller state로 해석한 뒤에만 외부 effect를 호출한다.
        state = self._find_state_by_order_identifier(action.order_id)
        if state is None:
            self._enter_order_reconciliation(
                None,
                OrderExecutionFailureCode.PENDING_ORDER_NOT_FOUND,
                message_id="8",
            )
            return ()
        # 사용자 STOP과 상단 밴드 안전 종료는 모두 terminal 확인 뒤 잔량 force-sell로 이어진다.
        state.stop_after_reconciliation = action.reason in (
            "STOP_CONFIRMED",
            "UPPER_BAND_SAFE_TERMINATION",
        )

        # cancel 전에 같은 주문을 query해 이미 terminal인 주문에 불필요한 cancel을 보내지 않는다.
        outcomes = self._query_existing_order(state)
        if state.order.is_terminal or outcomes:
            return outcomes
        if state.order.status in (
            OrderStatus.UNKNOWN,
            OrderStatus.PENDING_CANCEL,
        ):
            return ()  # UNKNOWN/cancel timeout 중에는 force-sell을 동시에 만들지 않는다.

        self._record_diagnostic("order_cancel_started", order=state.order, reason=action.reason)
        try:
            cancel_result = self._api_gateway.cancel_order(state.order)
        except Exception as error:
            self._diagnostics.record_exception(
                "order_cancel", error, session_id=self._session_id, client_order_id=state.order.client_order_id,
            )  # 취소 결과 불명도 같은 ID를 후속 조회하는 기존 계약을 보존한다.
            self._schedule_order_query(state, None)
            return ()

        # cancel 응답의 상태·fill은 provisional로 두고 반드시 후속 same-order query로 확정한다.
        self._record_diagnostic("order_cancel_received", result=cancel_result, failure=normalize_order_failure(cancel_result.failure_reason))
        self._schedule_order_query(state, cancel_result.retry_after)
        return ()

    def _reconcile_order_action(
        self,
        action: ReconcileOrder,
    ) -> tuple[TradingEvent, ...]:
        """
        함수 이름: _reconcile_order_action()
        기능: cancel 뒤 동일 ID를 재조회하고 stop 잔량이 확정된 뒤에만 force-sell한다.
        인자: action -> 조회할 order ID와 stop 후속 여부
        반환값: terminal stop 또는 force-sell outcome tuple
        작성 날짜: 2026/08/22
        """
        # cancel 응답이 아닌 보존 ID의 실제 state를 기준으로 후속 경로를 결정한다.
        state = self._find_state_by_order_identifier(action.order_id)
        if state is None:
            self._enter_order_reconciliation(
                None,
                OrderExecutionFailureCode.PENDING_ORDER_NOT_FOUND,
                message_id="8",
            )
            return ()
        # 일반 STOP의 SELL은 조회만 하지만 durable CANCEL_AND_LIQUIDATE는 별도 취소 의무를 갖는다.
        if (
            action.stop_after_reconciliation
            and not state.stop_after_reconciliation
            and self._requires_manual_kill_cleanup_locked()
            and not state.order.is_terminal
        ):
            outcomes = self._cancel_pending_order_action(CancelPendingOrder(
                order_id=action.order_id, reason="STOP_CONFIRMED",
            ))
            if outcomes:
                return outcomes
        state.stop_after_reconciliation = action.stop_after_reconciliation

        # 앞선 cancel action에서 이미 terminal completion을 만들었으면 중복 force/order event를 만들지 않는다.
        if state.pending_outcome is not None or state.stop_followup_started:
            return ()
        if state.order.is_terminal:
            if self._require_position().quantity > Decimal("0"):
                return self._force_sell_action(ForceSellAll())
            stop_event = self._create_order_outcome_event(
                state,
                succeeded=True,
                force_outcome=True,
            )
            state.pending_outcome = stop_event
            return (stop_event,)

        return self._query_existing_order(state)  # cancel 이후에도 반드시 같은 ID만 조회한다.

    def _force_sell_action(
        self,
        action: ForceSellAll,
    ) -> tuple[TradingEvent, ...]:
        """
        함수 이름: _force_sell_action()
        기능: STOP Position 전량을 일반 Order pipeline으로 제출하거나 3초 retry를 예약한다.
        인자: action -> 최초 또는 retry force-sell 요청
        반환값: 동기 terminal이면 force outcome tuple
        작성 날짜: 2026/08/22
        """
        # force intent를 만들기 전에 authoritative Position의 실제 잔량으로 zero-order를 차단한다.
        position = self._require_position()
        if position.quantity <= Decimal("0"):
            return ()  # Position 0에서는 sell API를 호출하지 않는다.

        # STOP session 하나의 intent ID를 모든 3초 residual attempt가 공유한다.
        intent_id = self._force_sell_intent_id
        if intent_id is None:
            session_id = self._session_id or "unstarted"
            intent_id = f"force-sell:{session_id}"
            self._force_sell_intent_id = intent_id
        # retry action은 즉시 제출하지 않고 예산을 확인한 뒤 due 시각만 예약한다.
        submission_count = self._submission_attempts_by_intent.get(intent_id, 0)
        if action.retry:
            if (
                submission_count
                >= self._maximum_order_submissions_per_intent
            ):
                self._enter_order_reconciliation(
                    self._find_latest_state_for_intent(intent_id),
                    OrderExecutionFailureCode.SUBMISSION_BUDGET_EXHAUSTED,
                    message_id=None,
                )
                return ()
            self._force_sell_retry_due_at = self._clock() + _FORCE_SELL_RETRY_DELAY
            self._request_event_runtime_processing()
            return ()  # G-06R action stack에서 즉시 재귀 제출하지 않는다.

        return self._submit_force_sell(intent_id, position.quantity)

    def _submit_force_sell_retry(self) -> tuple[TradingEvent, ...]:
        """
        함수 이름: _submit_force_sell_retry()
        기능: due G-06R retry에서 현재 잔여 Position 전량만 새 client ID로 제출한다.
        인자: 없음
        반환값: 동기 terminal force outcome tuple
        작성 날짜: 2026/08/22
        """
        # due trigger는 최초 STOP에서 생성한 intent identity가 존재할 때만 잔량을 재평가한다.
        intent_id = self._force_sell_intent_id
        if intent_id is None:
            raise RuntimeError("force-sell retry requires an intent ID")
        # 재시도 시점의 Position이 0이면 이미 달성된 의도로 보고 REST 제출을 생략한다.
        position = self._require_position()
        if position.quantity <= Decimal("0"):
            return ()

        try:
            return self._submit_force_sell(intent_id, position.quantity)
        except _RecoveredPositionLiquidationPreflightError:
            # 최초 공개 명령과 달리 established recovery retry는 원복할 NOT_STARTED 경계가 없다.
            self._enter_order_reconciliation(
                self._find_latest_state_for_intent(intent_id),
                OrderExecutionFailureCode.SYMBOL_FILTER_REJECTED,
                message_id="5",
            )
            return ()  # Worker 밖으로 private 예외를 내보내지 않고 operator gate를 영구적으로 닫는다.

    def _submit_force_sell(
        self,
        intent_id: str,
        quantity: Decimal,
    ) -> tuple[TradingEvent, ...]:
        """
        함수 이름: _submit_force_sell()
        기능: force intent runtime을 예약하고 현재 Position owner의 SELL Order를 제출한다.
        인자: intent_id -> 모든 force retry가 공유할 의도 ID
            quantity -> 이번에 제출할 잔여 Position 수량
        반환값: 동기 terminal force outcome tuple
        작성 날짜: 2026/08/22
        """
        # Position owner를 반드시 보존해 force fill도 원 전략의 Trade와 같은 소유자로 기록한다.
        position = self._require_position()
        owner = position.owner
        if owner is None:
            self._enter_order_reconciliation(
                None,
                OrderExecutionFailureCode.POSITION_UPDATE_FAILED,
                message_id="11",
            )
            return ()
        # 제출 횟수는 client ID attempt와 STM outcome payload에 동일하게 사용한다.
        attempt = self._submission_attempts_by_intent.get(intent_id, 0)
        attempt_kind = (
            OrderAttemptKind.INITIAL if attempt == 0 else OrderAttemptKind.RETRY
        )

        # Force sell도 일반 주문과 같은 message 1→Context patch→message 2 경계를 보존한다.
        pending_patch = patch(
            pending_strategy=owner,
            pending_order_side=OrderSide.SELL,
            pending_order_attempt_kind=attempt_kind,
            pending_intent_id=intent_id,
            trading_phase=TradingPhase.STOPPING,
        )
        trace_identity = self._prepare_order_patch_trace(pending_patch)
        self._context.apply_runtime_patch(pending_patch)
        self._complete_order_patch_trace(trace_identity)
        submit_action = SubmitOrder(
            strategy=owner,
            side=OrderSide.SELL,
            attempt_kind=attempt_kind,
            idempotency_key=intent_id,
            exit_reason=(
                self._context.runtime.pending_exit_reason
                or ExitReason.STOP
            ),
        )
        return self._submit_order_action(
            submit_action,
            force_sell=True,
            quantity_override=quantity,
        )

    def retry_pending_order_persistence(
        self,
        order_id: str,
    ) -> tuple[TradingEvent, ...]:
        """
        함수 이름: retry_pending_order_persistence()
        기능: exchange 주문을 재제출하지 않고 같은 terminal Trade의 durable save만 재시도한다.
        인자: order_id -> 저장 실패한 exchange order ID
        반환값: 저장 성공 뒤 enqueue한 concrete outcome tuple
        작성 날짜: 2026/08/22
        """
        # durable retry key는 exchange order ID로만 받아 임의 client ID의 재제출 경로를 없앤다.
        if not isinstance(order_id, str):
            raise TypeError("order_id must be a string")
        if not order_id or not order_id.isascii() or not order_id.isdigit():
            raise ValueError("order_id must be a positive integer string")

        # Position을 rollback하지 않은 채 pending save state를 원자적으로 해제한다.
        with self._session_lock:
            state = self._persistence_states_by_order_id.get(order_id)
            if state is None:
                raise KeyError(f"order_id {order_id} has no pending persistence")
            history_controller = self._require_trade_history_controller()

            # Controller는 Gateway를 호출하지 않고 TradeHistoryController의 same-order save만 호출한다.
            try:
                committed_trade = history_controller.retry_pending_persistence(order_id)
            except Exception:
                for message_id in ("13.5", "13.5.1"):
                    self._append_order_trace(
                        state.order,
                        message_id,
                        self._context.version,
                        failure_code=(
                            OrderExecutionFailureCode.HISTORY_PERSISTENCE_FAILED
                        ),
                    )
                raise
            self._record_diagnostic("trade_committed", trade=committed_trade, performance=history_controller.performance, persistence_retry=True)
            if not self._transition_pending_order_recovery(
                state,
                PendingOrderRecoveryLifecycle.HISTORY_COMMITTED,
            ):
                return ()  # History retry 성공 후 lifecycle fsync 실패도 주문 gate를 계속 유지한다.
            for message_id in ("13.5", "13.5.1"):
                self._append_order_trace(
                    state.order,
                    message_id,
                    self._context.version,
                )  # recovery trace는 최초 실패 뒤 같은 order의 durable 확정을 별도로 남긴다.
            state.persistence_pending = False
            self._persistence_states_by_order_id.pop(order_id, None)

            # 저장 성공으로 reconciliation 원인이 해소되면 원래 주문 단계에서 outcome을 계속한다.
            phase = (
                TradingPhase.STOPPING
                if state.force_sell or state.stop_after_reconciliation
                else (
                    TradingPhase.ENTRY_ORDER_PENDING
                    if state.order.side is OrderSide.BUY
                    else TradingPhase.EXIT_ORDER_PENDING
                )
            )
            self._context.apply_runtime_patch(patch(trading_phase=phase))
            self._status = (
                TradingSessionStatus.STOPPING
                if state.force_sell or state.stop_after_reconciliation
                else TradingSessionStatus.RUNNING
            )  # 저장 lock 해소를 먼저 publish해 후속 residual submit이 자체 차단되지 않게 한다.
            outcomes = self._complete_terminal_after_history(state)
            return self._enqueue_order_outcomes(outcomes)

    def _enqueue_order_outcomes(
        self,
        outcomes: list[TradingEvent] | tuple[TradingEvent, ...],
    ) -> tuple[TradingEvent, ...]:
        """
        함수 이름: _enqueue_order_outcomes()
        기능: concrete order outcome을 serial queue에 INTERNAL 우선순위로 중복 없이 넣는다.
        인자: outcomes -> 저장과 Position 반영을 마친 event collection
        반환값: 실제 queue가 수락한 event tuple
        작성 날짜: 2026/08/22
        """
        # durable 저장까지 끝나지 않은 outcome이 queue를 통해 STM에 도달하지 못하게 한다.
        if self._event_queue is None:
            if outcomes:
                raise RuntimeError("order outcomes require an initialized event queue")
            return ()

        # serial queue dedup이 실제 수락한 concrete event만 호출자에게 반환한다.
        enqueued_events: list[TradingEvent] = []
        for outcome in outcomes:
            enqueued_event = self._event_queue.enqueue(
                outcome,
                internal=True,
            )
            if enqueued_event is not None:
                enqueued_events.append(enqueued_event)

        # 한 wake 신호로 여러 concrete outcome을 합치고 queue가 비었으면 불필요한 신호를 만들지 않는다.
        if enqueued_events:
            self._request_event_runtime_processing()

        return tuple(enqueued_events)

    def _find_state_by_order_identifier(
        self,
        order_identifier: str | None,
    ) -> _OrderExecutionState | None:
        """
        함수 이름: _find_state_by_order_identifier()
        기능: exchange 또는 client order ID로 같은 Controller execution state를 찾는다.
        인자: order_identifier -> Context/action이 보존한 order ID 또는 None
        반환값: 일치하는 state 또는 None
        작성 날짜: 2026/08/22
        """
        # exchange ID를 우선하되 제출 직후의 client-only state도 같은 aggregate로 찾는다.
        if order_identifier is None:
            return None
        state = self._order_states_by_order_id.get(order_identifier)
        if state is not None:
            return state

        return self._order_states_by_client_id.get(order_identifier)

    def _find_active_state_for_intent(
        self,
        intent_id: str,
    ) -> _OrderExecutionState | None:
        """
        함수 이름: _find_active_state_for_intent()
        기능: 신규 제출을 막아야 하는 active·UNKNOWN·persistence state를 같은 intent에서 찾는다.
        인자: intent_id -> STM idempotency key
        반환값: unresolved state 또는 None
        작성 날짜: 2026/08/22
        """
        # 가장 최근 attempt부터 검사해 이전 terminal attempt가 새 제출을 막지 않게 한다.
        for state in reversed(tuple(self._order_states_by_client_id.values())):
            if state.order.intent_id != intent_id:
                continue
            if (
                state.persistence_pending
                or state.pending_recovery_pending
                or state.awaiting_terminal_zero_confirmation
                or state.order.status is None
                or state.order.status is OrderStatus.UNKNOWN
                or state.order.status in ACTIVE_ORDER_STATUSES
            ):
                return state

        return None

    def _find_latest_state_for_intent(
        self,
        intent_id: str,
    ) -> _OrderExecutionState | None:
        """
        함수 이름: _find_latest_state_for_intent()
        기능: 진단 trace와 budget failure에 사용할 가장 최근 intent state를 찾는다.
        인자: intent_id -> 조회할 원 주문 의도 ID
        반환값: 최근 state 또는 None
        작성 날짜: 2026/08/22
        """
        # insertion order를 역순회해 budget failure와 trace를 마지막 attempt에 연결한다.
        for state in reversed(tuple(self._order_states_by_client_id.values())):
            if state.order.intent_id == intent_id:
                return state

        return None

    def _enter_order_reconciliation(
        self,
        state: _OrderExecutionState | None,
        failure_code: OrderExecutionFailureCode,
        *,
        message_id: str | None,
        cause_category: ReconciliationCauseCategory | None = None,
    ) -> None:
        """
        함수 이름: _enter_order_reconciliation()
        기능: unresolved exchange/Position/storage 사실을 rollback하지 않고 신규 action을 잠근다.
        인자: state -> 관련 Order state 또는 생성 전이면 None
            failure_code -> typed reconciliation 사유
            message_id -> 실제 실패가 관찰된 Communication message ID 또는 policy lock이면 None
            cause_category -> 호출 origin이 구분한 재조정 원인 범주 또는 자동 mapping이면 None
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        if not isinstance(failure_code, OrderExecutionFailureCode):
            raise TypeError("failure_code must be an OrderExecutionFailureCode")
        if message_id is not None and (
            not isinstance(message_id, str) or not message_id.strip()
        ):
            raise ValueError("message_id must be a non-empty string or None")
        if cause_category is not None and not isinstance(
            cause_category,
            ReconciliationCauseCategory,
        ):
            raise TypeError(
                "cause_category must be a ReconciliationCauseCategory or None"
            )

        with self._session_lock:
            # 명시 origin이 없으면 failure code를 안정적인 공개 범주로 보수적으로 축약한다.
            selected_cause_category = cause_category
            if selected_cause_category is None:
                if failure_code is OrderExecutionFailureCode.SYMBOL_FILTER_REJECTED:
                    selected_cause_category = (
                        ReconciliationCauseCategory.PREPARE_FILTER_OR_CAP_REJECTED
                    )
                elif failure_code is OrderExecutionFailureCode.EVENT_RUNTIME_FAILED:
                    selected_cause_category = (
                        ReconciliationCauseCategory.EVENT_WORKER_OR_RUNTIME_FAILED
                    )
                else:
                    selected_cause_category = (
                        ReconciliationCauseCategory.ORDER_OR_PERSISTENCE_AMBIGUOUS
                    )
            self._record_reconciliation_cause_locked(selected_cause_category)
            self._status = TradingSessionStatus.RECONCILIATION_REQUIRED
            self._record_diagnostic(
                "reconciliation_required", level="ERROR", failure_code=failure_code,
                cause_category=selected_cause_category, message_id=message_id,
                order=None if state is None else state.order,
            )  # message ID가 없는 policy·budget 실패도 파일에는 원인을 남긴다.

            # 공개 status를 먼저 잠가 Context patch 실패도 신규 effect 허용 상태로 완화하지 않는다.
            version_before = self._context.version
            if self._context.initialized:
                self._context.apply_runtime_patch(
                    patch(trading_phase=TradingPhase.RECONCILIATION_REQUIRED)
                )
            if state is not None:
                self._scheduled_order_queries.pop(
                    state.order.client_order_id,
                    None,
                )
            if state is not None and message_id is not None:
                self._append_order_trace(
                    state.order,
                    message_id,
                    version_before,
                    failure_code=failure_code,
                )

    def mark_event_runtime_failed(self) -> None:
        """
        함수 이름: mark_event_runtime_failed()
        기능: bounded event cycle 실패를 credential 없는 typed reconciliation 상태로 잠근다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with self._session_lock:
            self._event_runtime_failed = True

            # 이미 terminal인 session은 background 실패 때문에 새 active 상태로 되살리지 않는다.
            if self._cleanup_in_progress or self._status not in (
                TradingSessionStatus.RUNNING,
                TradingSessionStatus.STOPPING,
                TradingSessionStatus.RECONCILIATION_REQUIRED,
            ):
                self._record_reconciliation_cause_locked(
                    ReconciliationCauseCategory.EVENT_WORKER_OR_RUNTIME_FAILED
                )
                return

            self._enter_order_reconciliation(
                None,
                OrderExecutionFailureCode.EVENT_RUNTIME_FAILED,
                message_id=None,
            )  # Queue, pending journal과 same-ID state는 보존하고 신규 주문 gate만 닫는다.

    def seal_event_runtime_failure_gate(self) -> bool:
        """
        함수 이름: seal_event_runtime_failure_gate()
        기능: failure finalizer가 terminal truth를 보존하며 active/reconciliation scheduler gate를 원자적으로 봉인한다.
        인자: 없음
        반환값: RUNNING 또는 STOPPING을 새 event reconciliation으로 전이했으면 True
        작성 날짜: 2026/08/31
        """
        with self._session_lock:
            # Completed recovery와 시작 전 failure는 새 event blocker로 오염시키지 않는다.
            if self._status in (
                TradingSessionStatus.NOT_STARTED,
                TradingSessionStatus.TERMINATED,
            ):
                return False

            # 기존 reconciliation은 원인 latch를 다시 기록하지 않고 future reconnect만 계속 닫는다.
            self._event_runtime_failed = True
            if self._status is TradingSessionStatus.RECONCILIATION_REQUIRED:
                return False

            self._enter_order_reconciliation(
                None,
                OrderExecutionFailureCode.EVENT_RUNTIME_FAILED,
                message_id=None,
            )
            return True  # Status check와 blocker commit은 같은 reentrant session lock에서 완료된다.

    def _record_order_finished_trace(
        self,
        event: TradingEvent,
        context: TradingContextView,
    ) -> None:
        """
        함수 이름: _record_order_finished_trace()
        기능: concrete outcome이 실제 TradingSTM.order_finished에서 성공한 순간 메시지 14를 기록한다.
        인자: event -> serial queue가 전달한 concrete order outcome
            context -> order_finished가 사용한 최신 immutable Context view
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        if not isinstance(event, TradingEvent):
            raise TypeError("event must be a TradingEvent")
        if not isinstance(context, TradingContextView):
            raise TypeError("context must be a TradingContextView")

        # Phase 7 legacy 수동 outcome에는 Controller Order state가 없으므로 trace만 생략한다.
        state = self._find_state_by_order_identifier(event.order_id)
        if state is None:
            return

        self._append_order_trace(
            state.order,
            "14",
            context.version,
            command_event_id=event.event_id,
        )  # 실제 adapter 호출과 version 검증을 통과한 outcome만 SUCCESS로 남긴다.

    def _observe_processing_event(self, event: TradingEvent | None) -> None:
        """
        함수 이름: _observe_processing_event()
        기능: serial processor가 실행 중인 원 event ID를 Case 2 action trace 수명 동안만 보존한다.
        인자: event -> 현재 microstep event 또는 batch 종료를 뜻하는 None
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        if event is not None and not isinstance(event, TradingEvent):
            raise TypeError("event must be a TradingEvent or None")

        self._active_trace_event_id = (
            None if event is None else event.event_id
        )  # event processor의 finally callback이 다음 microstep 전에 반드시 비운다.
        if event is not None and self._diagnostics.enabled and self._active_stm is not None:
            self._record_diagnostic(
                "evaluation_started", event_type=event.event_type, occurred_at=event.occurred_at,
                event_lower_event_id=event.lower_event_id, candle_id=event.candle_id, order_id=event.order_id,
                evaluation=describe_trading_evaluation(self._active_stm.current_state, self._context.snapshot()),
            )  # action 실행 전에 입력을 확정해 중도 예외에도 판단 지표가 남는다.

    def _append_order_trace(
        self,
        order: Order,
        message_id: str,
        context_version_before: int,
        *,
        failure_code: OrderExecutionFailureCode | None = None,
        command_event_id: str | None = None,
    ) -> None:
        """
        함수 이름: _append_order_trace()
        기능: 현재 Order identity와 Context version으로 안전한 Case 2 trace 한 건을 추가한다.
        인자: order -> trace 상관관계 Order
            message_id -> Communication Case 2 message ID
            context_version_before -> operation 직전 Context version
            failure_code -> 실패 trace의 typed code 또는 성공이면 None
            command_event_id -> 명시적 원 event ID 또는 현재 processor event를 쓰면 None
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")
        self._append_order_trace_values(
            message_id,
            order.intent_id,
            order.client_order_id,
            order.exchange_order_id,
            context_version_before,
            failure_code=failure_code,
            command_event_id=command_event_id,
            order_snapshot=order,
        )

    def _append_order_trace_values(
        self,
        message_id: str,
        intent_id: str,
        client_order_id: str,
        order_id: str | None,
        context_version_before: int,
        *,
        failure_code: OrderExecutionFailureCode | None = None,
        command_event_id: str | None = None,
        order_snapshot: Order | None = None,
    ) -> None:
        """
        함수 이름: _append_order_trace_values()
        기능: Order 생성 전 단계도 동일 schema의 상관관계 trace로 기록한다.
        인자: message_id -> Communication message ID
            intent_id -> 원 주문 의도 ID
            client_order_id -> submission attempt별 client ID
            order_id -> exchange order ID 또는 미확정이면 None
            context_version_before -> operation 직전 Context version
            failure_code -> 실패 trace typed code 또는 None
            command_event_id -> 직접 주어진 원 event ID 또는 None
            order_snapshot -> 처리 직후 주문·체결 수치 또는 Order 생성 전의 None
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # 문서에 없는 message ID는 caller/receiver를 추측하지 않고 즉시 거부한다.
        participants = _ORDER_TRACE_PARTICIPANTS.get(message_id)
        if participants is None:
            raise ValueError(f"unsupported order trace message ID: {message_id}")
        result = (
            OrderExecutionTraceResult.SUCCESS
            if failure_code is None
            else OrderExecutionTraceResult.FAILURE
        )
        self._order_trace.append(
            OrderExecutionTraceEntry(
                message_id=message_id,
                caller=participants[0],
                receiver=participants[1],
                command_event_id=(
                    command_event_id
                    or self._active_trace_event_id
                    or intent_id
                ),
                context_version_before=context_version_before,
                context_version_after=self._context.version,
                intent_id=intent_id,
                client_order_id=client_order_id,
                order_id=order_id,
                result=result,
                failure_code=failure_code,
            )
        )  # credential와 raw payload는 trace schema에 필드 자체가 없다.
        self._record_diagnostic("order_step", level="ERROR" if failure_code else "INFO", trace=self._order_trace[-1], order=order_snapshot)
        if failure_code is not None and sys.exception() is not None:
            self._diagnostics.record_exception(
                f"order_step:{message_id}", sys.exception(), session_id=self._session_id,
                event_id=command_event_id or self._active_trace_event_id,
                intent_id=intent_id, client_order_id=client_order_id, order_id=order_id,
            )  # 포착된 원인은 타입·내부 위치만 보존하고 예외 메시지는 출력하지 않는다.

    def _record_diagnostic(self, event: str, *, level: str = "INFO", **details: object) -> None:
        """
        함수 이름: _record_diagnostic()
        기능: 세션·하단 터치·시장 version을 모든 거래 진단 사건에 연결한다.
        인자: event -> 사건 이름, level -> 심각도, details -> 명시적으로 선택한 진단 값
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        if not self._diagnostics.enabled:
            return

        # 호출자는 Controller lock 아래에서 입력·effect를 관찰하므로 같은 시점 식별자를 공유한다.
        self._diagnostics.record(
            event, level=level, session_id=self._session_id,
            event_id=self._active_trace_event_id, lower_event_id=self._context.runtime.lower_event_id,
            market_version=self._latest_market_evaluation_version, context_version=self._context.version,
            session_status=self._status, selected_regime=self._selected_regime, **details,
        )

    def _require_position(self) -> Position:
        """
        함수 이름: _require_position()
        기능: Phase 8 주문 effect에서 주입 Position을 검증해 반환한다.
        인자: 없음
        반환값: authoritative Position
        작성 날짜: 2026/08/22
        """
        # legacy Phase 7 조립과 실행 중 Phase 8 dependency 누락을 구분할 수 있는 fail-fast 경계다.
        position = self._position
        if position is None:
            raise RuntimeError("order pipeline requires a Position")

        return position

    def _require_trade_history_controller(self) -> TradeHistoryController:
        """
        함수 이름: _require_trade_history_controller()
        기능: terminal 기록에서 주입 TradeHistoryController를 검증해 반환한다.
        인자: 없음
        반환값: Phase 8 TradeHistoryController
        작성 날짜: 2026/08/22
        """
        # history dependency 누락을 REST 제출 후 silent skip으로 숨기지 않는다.
        controller = self._trade_history_controller
        if controller is None:
            raise RuntimeError(
                "order pipeline requires a TradeHistoryController"
            )

        return controller

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
        enqueued_event = self._event_queue.enqueue(
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
        if enqueued_event is not None:
            self._request_event_runtime_processing()

    def _request_event_runtime_processing(self) -> None:
        """
        함수 이름: _request_event_runtime_processing()
        기능: queue 또는 due 작업의 존재를 production runtime driver에 non-blocking으로 알린다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # Unit 조립에는 notifier가 없을 수 있으며 Controller가 직접 thread를 만들지는 않는다.
        notifier = self._event_runtime_notifier
        if notifier is not None:
            notifier()  # Bootstrap worker의 Event.set 경계만 호출하고 queue를 여기서 drain하지 않는다.

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
                self._diagnostics.record_exception("session_subscription_cleanup", error, session_id=self._session_id)

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
        status_before = self._status  # STM 전이와 공개 세션 상태 동기화의 시점을 구분해 기록한다.

        # 시장 중단 provenance를 포함한 어떤 blocker도 STM 문맥 변경만으로 RUNNING을 다시 열 수 없다.
        reconciliation_blocked = (
            self._stream_reconciliation_required
            or self._market_stream_reconciliation_required
            or self._market_stream_interrupted_running_session
            or self._event_runtime_failed
            or self._process_ownership_ambiguous
            or self._external_execution_reconciliation_required
            or self._startup_reconciliation_blocked
        )
        if (
            reconciliation_blocked
            or runtime.trading_phase
            is TradingPhase.RECONCILIATION_REQUIRED
        ):
            self._status = TradingSessionStatus.RECONCILIATION_REQUIRED
        elif root_state is RootState.LOGIC_TERMINATED:
            self._status = TradingSessionStatus.TERMINATED
            self._selected_stm = None  # 다음 start는 정상 stop 뒤 새 REGIME 선택을 반드시 요구한다.
        elif root_state is RootState.STOPPING:
            if runtime.pending_order_id is not None:
                self._record_reconciliation_cause_locked(
                    ReconciliationCauseCategory.ORDER_OR_PERSISTENCE_AMBIGUOUS
                )
                self._status = TradingSessionStatus.RECONCILIATION_REQUIRED
            else:
                self._status = TradingSessionStatus.STOPPING
        else:
            self._status = TradingSessionStatus.RUNNING

        # 같은 상태의 반복 동기화는 줄이고 실제 공개 상태 변경은 완료 시점에 기록한다.
        if self._status is not status_before:
            self._record_diagnostic("session_state_changed", status_before=status_before, status_after=self._status, state=stm.current_state)

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
        self._record_diagnostic("session_command_completed", operation=operation, command_id=normalized_command_id, result=result)

        # adversarial unique command가 process lifetime memory를 무제한 늘리지 못하게 한다.
        while len(self._command_order) > _MAX_COMMAND_RECORDS:
            expired_key = self._command_order.popleft()
            self._command_records.pop(expired_key, None)

    def _read_manual_kill_command_record(
        self,
        command_id: str,
        fingerprint: tuple[object, ...],
    ) -> object | None:
        """
        함수 이름: _read_manual_kill_command_record()
        기능: durable manual-kill command 결과를 찾고 다른 payload의 ID 재사용을 거부한다.
        인자: command_id -> manual-kill transport command 식별자
            fingerprint -> active와 expected risk-control version tuple
        반환값: 저장된 결과 또는 처음 본 command이면 None
        작성 날짜: 2026/08/29
        """
        # 일반 selection/start command eviction이 durable receipt 의미를 지우지 않게 전용 cache를 읽는다.
        normalized_command_id = self._normalize_command_id(command_id)
        record = self._manual_kill_command_records.get(normalized_command_id)
        if record is None:
            return None
        if record.fingerprint != fingerprint:
            raise TradingSessionError(
                TradingSessionFailureCode.COMMAND_ID_REUSED,
                "Command ID cannot be reused with a different payload",
                current_version=self._context.version,
            )

        return record.result  # duplicate에는 durable state나 control version을 다시 변경하지 않는다.

    def _store_manual_kill_command_record(
        self,
        command_id: str,
        fingerprint: tuple[object, ...],
        result: object,
    ) -> None:
        """
        함수 이름: _store_manual_kill_command_record()
        기능: manual-kill receipt의 fingerprint와 최초 성공 결과를 전용 bounded cache에 결합한다.
        인자: command_id -> manual-kill transport command 식별자
            fingerprint -> active와 expected risk-control version tuple
            result -> duplicate에 반환할 최초 성공 결과
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # Repository replay와 같은 최근 1,024개 범위를 일반 command와 독립적으로 보존한다.
        normalized_command_id = self._normalize_command_id(command_id)
        self._manual_kill_command_records[normalized_command_id] = (
            _CommandRecord(fingerprint, result)
        )
        self._manual_kill_command_order.append(normalized_command_id)

        # Manual-kill command만의 adversarial ID 흐름도 process memory를 무제한 늘리지 못하게 한다.
        while (
            len(self._manual_kill_command_order)
            > _MAX_MANUAL_KILL_COMMAND_RECORDS
        ):
            expired_command_id = self._manual_kill_command_order.popleft()
            self._manual_kill_command_records.pop(
                expired_command_id,
                None,
            )

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
    def _require_manual_kill_result(result: object) -> ManualKillResult:
        """
        함수 이름: _require_manual_kill_result()
        기능: command cache의 manual kill 결과 타입을 검증해 반환한다.
        인자: result -> command cache에서 읽은 값
        반환값: 검증된 ManualKillResult
        작성 날짜: 2026/08/25
        """
        # 다른 operation의 cached value가 manual kill 결과로 잘못 재생되지 않게 exact type을 확인한다.
        if not isinstance(result, ManualKillResult):
            raise RuntimeError("manual kill command cache type mismatch")

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
