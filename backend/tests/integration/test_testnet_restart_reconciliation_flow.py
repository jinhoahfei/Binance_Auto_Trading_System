"""Phase 9 재시작 주문 복구가 신규 제출과 중복 history를 만들지 않는지 검증한다."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, RLock
import unittest
from unittest.mock import patch as mock_patch

from binance_auto_trader.adapters.binance.api_gateway import APIGateway
from binance_auto_trader.adapters.binance.websocket_gateway import (
    WebSocketGateway,
)
from binance_auto_trader.adapters.persistence.trade_history_repository import (
    TradeHistoryRepository,
)
from binance_auto_trader.application.trade_history_controller import (
    TradeHistoryController,
)
from binance_auto_trader.application.trading_controller import (
    AccountStreamRecoveryBlockedError,
    StartupOrderReconciliationError,
    TradingController,
    TradingSessionError,
    TradingSessionFailureCode,
    TradingSessionStatus,
)
from binance_auto_trader.bootstrap.application import (
    _AccountStreamRecoveryWorker,
)
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.trading.account import (
    Account,
    AccountSnapshot,
    AssetBalance,
)
from binance_auto_trader.domain.trading.events import (
    TradingEvent,
    TradingEventType,
)
from binance_auto_trader.domain.trading.order import (
    Fill,
    Order,
    OrderResult,
    OrderResultFailureKind,
    OrderStatus,
    PendingOrderRecoveryLifecycle,
    PendingOrderSubmissionProvenance,
)
from binance_auto_trader.domain.trading.position import (
    LegacyFeeAccountingMigrationRequiredError,
    Position,
)
from binance_auto_trader.domain.trading.risk import (
    DailyLossScope,
    ManualKillBehavior,
    ManualKillControlState,
    RiskPolicy,
    RiskPolicyUnavailable,
)
from binance_auto_trader.domain.trading.states import (
    ExitReason,
    OrderAttemptKind,
    OrderSide,
    PositionReturnState,
    StrategyType,
)
from binance_auto_trader.domain.trading.stm import TradingSTM
from binance_auto_trader.domain.trading.transitions.helpers import (
    create_exit_order_actions,
)

from tests.integration.test_account_stream_flow import (
    FakeSubscription,
    SynchronousAccountWebSocketClient,
    _account_rest_payload,
    _ready_market_snapshot,
)
from tests.integration.test_order_reconciliation_flow import (
    MutableUtcClock,
    ScriptedOrderRESTClient,
    _OrderResponseKind,
    _create_started_controller,
    _submit_case_b_buy,
)
from tests.integration.phase13_risk_fixture import create_test_risk_policy
from tests.unit.history.factories import make_order_execution, make_trade


FIXED_TIME = datetime(2026, 8, 22, 5, 0, tzinfo=timezone.utc)


def _make_pending_order(
    *,
    intent_id: str = "restart-buy-intent",
    client_order_id: str = "bat-restart-buy-1",
    submission_attempt: int = 1,
    risk_policy_version: int | None = None,
) -> Order:
    """
    함수 이름: _make_pending_order()
    기능: crash 직전 sidecar에 남은 testnet BUY 주문 metadata를 만든다.
    인자: intent_id -> 재시작 뒤에도 보존할 application intent ID
        client_order_id -> session과 제출 시도를 포함한 Binance client order ID
        submission_attempt -> journal에 보존할 0 이상 제출 시도
        risk_policy_version -> 제출 판단에 사용한 policy version 또는 legacy None
    반환값: 제출 전 canonical Order
    작성 날짜: 2026/08/22
    """
    # 앱 prefix와 원 intent를 보존해 startup same-order query의 상관관계를 고정한다.
    return Order(
        intent_id=intent_id,
        client_order_id=client_order_id,
        submission_attempt=submission_attempt,
        symbol="ETHUSDT",
        side=OrderSide.BUY,
        strategy=StrategyType.CASE_B,
        regime_type=RegimeType.TYPE_0,
        requested_quantity=Decimal("0.25000000"),
        submitted_quantity=Decimal("0.25000000"),
        market_price_at_decision=Decimal("2500.00"),
        risk_policy_version=risk_policy_version,
    )


def _make_filled_result(
    order: Order,
    *,
    exchange_order_id: str = "91001",
    trade_id: str = "31001",
) -> OrderResult:
    """
    함수 이름: _make_filled_result()
    기능: pending 주문과 같은 identity의 authoritative FILLED 조회 결과를 만든다.
    인자: order -> client ID와 제출 수량을 제공할 복구 주문
        exchange_order_id -> authoritative Binance order ID
        trade_id -> authoritative Binance fill ID
    반환값: 한 fill을 가진 terminal OrderResult
    작성 날짜: 2026/08/22
    """
    # Fill key는 exchange order ID와 trade ID 조합으로 재실행에서도 동일하게 유지한다.
    fill = Fill(
        exchange_order_id=exchange_order_id,
        trade_id=trade_id,
        quantity=order.submitted_quantity,
        price=Decimal("2500.00"),
        fee_amount=Decimal("0"),
        fee_asset="USDT",
        fee_quote_amount=Decimal("0"),
        executed_at=FIXED_TIME,
    )
    return OrderResult(
        symbol=order.symbol,
        client_order_id=order.client_order_id,
        exchange_order_id=fill.exchange_order_id,
        status=OrderStatus.FILLED,
        processed_at=FIXED_TIME,
        fills=(fill,),
    )


class RestartReconciliationRESTClient:
    """
    클래스 이름: RestartReconciliationRESTClient
    기능: startup open/recent/same-order 조회와 신규 제출 부재를 기록한다.
    작성 날짜: 2026/08/22
    """

    def __init__(self, result: OrderResult) -> None:
        """
        함수 이름: __init__()
        기능: authoritative 결과와 비어 있는 operation 호출 기록을 초기화한다.
        인자: result -> recent 및 same-order query에 반환할 체결 결과
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        if not isinstance(result, OrderResult):
            raise TypeError("result must be an OrderResult")

        self.result = result
        self.account_payloads: list[object] = []
        self.query_client_order_ids: list[str] = []
        self.submit_count = 0
        self.include_recent_result = True
        self.additional_recent_results: tuple[OrderResult, ...] = ()
        self.recent_request_count = 0
        self.recent_requested = Event()

    def get_account(self) -> object:
        """
        함수 이름: get_account()
        기능: 복구 Position보다 충분한 ETH를 가진 전체 Spot account payload를 반환한다.
        인자: 없음
        반환값: 공식 account response 형식의 mapping
        작성 날짜: 2026/08/22
        """
        if self.account_payloads:
            return self.account_payloads.pop(0)  # 재접속 전후 잔액 차이를 순서대로 주입한다.

        return _account_rest_payload()  # 기존 공식 schema fixture를 그대로 공유한다.

    def submit_order(self, *, order: Order) -> OrderResult:
        """
        함수 이름: submit_order()
        기능: startup reconciliation 중 금지된 신규 주문 시도를 즉시 실패시킨다.
        인자: order -> 호출되면 안 되는 신규 주문
        반환값: 반환하지 않음
        작성 날짜: 2026/08/22
        """
        self.submit_count += 1
        raise AssertionError("startup reconciliation must not submit an order")

    def query_order_result(self, *, order: Order) -> OrderResult:
        """
        함수 이름: query_order_result()
        기능: sidecar의 동일 client ID 조회를 기록하고 authoritative 결과를 반환한다.
        인자: order -> durable journal에서 복원한 Order
        반환값: 같은 client ID의 terminal OrderResult
        작성 날짜: 2026/08/22
        """
        self.query_client_order_ids.append(order.client_order_id)
        if order.client_order_id != self.result.client_order_id:
            raise AssertionError("startup query changed the client order ID")

        return self.result  # 신규 client ID 생성 없이 같은 결과 identity를 재사용한다.

    def list_open_order_results(
        self,
        *,
        symbol: str,
    ) -> tuple[OrderResult, ...]:
        """
        함수 이름: list_open_order_results()
        기능: terminal 복구 fixture에 현재 미결 주문이 없음을 반환한다.
        인자: symbol -> 조회한 Spot symbol
        반환값: 빈 OrderResult tuple
        작성 날짜: 2026/08/22
        """
        if symbol != "ETHUSDT":
            raise AssertionError("unexpected startup symbol")

        return ()  # FILLED 주문은 open-order snapshot에 포함되지 않는다.

    def list_recent_order_results(
        self,
        *,
        symbol: str,
        limit: int = 100,
    ) -> tuple[OrderResult, ...]:
        """
        함수 이름: list_recent_order_results()
        기능: 거래소 recent-order 사실에 동일 FILLED 결과 한 건을 반환한다.
        인자: symbol -> 조회한 Spot symbol
            limit -> 요청한 최대 recent 결과 개수
        반환값: authoritative OrderResult 한 건의 tuple
        작성 날짜: 2026/08/22
        """
        if symbol != "ETHUSDT" or limit != 100:
            raise AssertionError("unexpected recent-order request")

        self.recent_request_count += 1
        self.recent_requested.set()
        if not self.include_recent_result:
            return ()  # Testnet reset은 과거 app 주문이 사라진 recent history로 재현한다.

        return (
            self.result,
            *self.additional_recent_results,
        )  # 첫 결과는 durable provenance이고 추가 결과는 disconnect race를 주입한다.


class _SubmissionRejectingTestnetRESTClient(ScriptedOrderRESTClient):
    """
    클래스 이름: _SubmissionRejectingTestnetRESTClient
    기능: startup은 빈 주문 목록을 주고 실제 submit은 typed pre-matching 거부를 반환한다.
    작성 날짜: 2026/08/23
    """

    def __init__(self, clock: MutableUtcClock) -> None:
        """
        함수 이름: __init__()
        기능: 제출 거부 한 건과 빈 startup query 상태를 준비한다.
        인자: clock -> 결과 processed_at에 사용할 결정적 UTC clock
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        super().__init__(
            clock,
            submit_steps=(_OrderResponseKind.SUBMISSION_REJECTED,),
        )

    def list_open_order_results(
        self,
        *,
        symbol: str,
    ) -> tuple[OrderResult, ...]:
        """
        함수 이름: list_open_order_results()
        기능: 최초 process startup에 app-owned open order가 없음을 반환한다.
        인자: symbol -> 조회한 Spot symbol
        반환값: 빈 OrderResult tuple
        작성 날짜: 2026/08/23
        """
        if symbol != "ETHUSDT":
            raise AssertionError("unexpected startup symbol")

        return ()

    def list_recent_order_results(
        self,
        *,
        symbol: str,
        limit: int = 100,
    ) -> tuple[OrderResult, ...]:
        """
        함수 이름: list_recent_order_results()
        기능: 최초 process startup에 recent app order가 없음을 반환한다.
        인자: symbol -> 조회한 Spot symbol
            limit -> 요청한 최대 recent 결과 개수
        반환값: 빈 OrderResult tuple
        작성 날짜: 2026/08/23
        """
        if symbol != "ETHUSDT" or limit != 100:
            raise AssertionError("unexpected recent-order request")

        return ()


class _FilledSubmissionTestnetRESTClient(
    RestartReconciliationRESTClient
):
    """
    클래스 이름: _FilledSubmissionTestnetRESTClient
    기능: 빈 startup 뒤 실제 runtime BUY를 FILLED로 만들고 재접속 query/recent에 같은 결과를 제공한다.
    작성 날짜: 2026/08/23
    """

    def __init__(
        self,
        *,
        startup_result: OrderResult | None = None,
        submission_exchange_order_id: str = "93001",
        submission_trade_id: str = "43001",
    ) -> None:
        """
        함수 이름: __init__()
        기능: optional startup recent 결과와 runtime submit identity를 초기화한다.
        인자: startup_result -> startup recent에 공개할 durable 결과 또는 None
            submission_exchange_order_id -> runtime fill에 재사용할 exchange order ID
            submission_trade_id -> runtime fill의 Binance trade ID
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        placeholder_order = _make_pending_order(
            intent_id="filled-runtime-placeholder",
            client_order_id="bat-filled-runtime-placeholder-1",
        )
        super().__init__(
            _make_filled_result(placeholder_order)
            if startup_result is None
            else startup_result
        )
        self.include_recent_result = startup_result is not None
        self.submission_exchange_order_id = submission_exchange_order_id
        self.submission_trade_id = submission_trade_id
        self.reject_prepare_order = False
        self.prepared_quantity_override: Decimal | None = None
        self.terminal_partial_quantity: Decimal | None = None

    def prepare_order(self, *, order: Order) -> Order:
        """
        함수 이름: prepare_order()
        기능: 복구 청산 사전검증 실패를 선택적으로 주입하거나 원 Order를 보존한다.
        인자: order -> filter 적용 전 주문
        반환값: 실패가 비활성화되어 있으면 원 Order
        작성 날짜: 2026/08/24
        """
        # 사전검증 예외는 journal 저장과 submit 호출보다 먼저 발생한다.
        if self.reject_prepare_order:
            raise RuntimeError("injected order preflight failure")

        # Exchange filter의 LOT_SIZE 내림을 독립 Order로 재현해 원 requested 수량을 보존한다.
        if self.prepared_quantity_override is not None:
            return replace(
                order,
                submitted_quantity=self.prepared_quantity_override,
            )

        return order  # 정상 경로에서는 fake가 수량과 identity를 변경하지 않는다.

    def submit_order(self, *, order: Order) -> OrderResult:
        """
        함수 이름: submit_order()
        기능: runtime 주문을 FILLED 또는 선택한 terminal partial로 만들고 same-ID 결과로 보존한다.
        인자: order -> Controller가 생성해 durable journal에 먼저 기록한 BUY
        반환값: 동일 client ID와 고정 exchange ID의 terminal FILLED 결과
        작성 날짜: 2026/08/23
        """
        self.submit_count += 1
        if self.terminal_partial_quantity is not None:
            # CANCELED terminal의 실제 fill만 반영해 recovery residual retry를 결정론적으로 만든다.
            partial_fill = Fill(
                exchange_order_id=self.submission_exchange_order_id,
                trade_id=self.submission_trade_id,
                quantity=self.terminal_partial_quantity,
                price=Decimal("2500.00"),
                fee_amount=Decimal("0"),
                fee_asset="USDT",
                fee_quote_amount=Decimal("0"),
                executed_at=FIXED_TIME,
            )
            self.result = OrderResult(
                symbol=order.symbol,
                client_order_id=order.client_order_id,
                exchange_order_id=partial_fill.exchange_order_id,
                status=OrderStatus.CANCELED,
                processed_at=FIXED_TIME,
                fills=(partial_fill,),
            )
            self.include_recent_result = True
            return self.result

        # 기본 fake는 제출 수량 전체를 한 terminal FILLED 결과로 정규화한다.
        self.result = _make_filled_result(
            order,
            exchange_order_id=self.submission_exchange_order_id,
            trade_id=self.submission_trade_id,
        )
        self.include_recent_result = True

        return self.result  # 재접속 recent와 query가 동일 누적 fill을 재현한다.


class _ManualKillRestartRESTClient(_FilledSubmissionTestnetRESTClient):
    """
    클래스 이름: _ManualKillRestartRESTClient
    기능: durable kill 재시작의 active BUY 취소·partial 복원·잔여 SELL 순서를 재현한다.
    작성 날짜: 2026/08/29
    """

    def __init__(
        self,
        pending_order: Order,
        *,
        cancel_stays_active: bool = False,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 같은 pending identity의 NEW와 cancel-race CANCELED partial 결과를 준비한다.
        인자: pending_order -> crash 전에 SUBMITTED로 fsync된 app-owned BUY
            cancel_stays_active -> cancel 뒤 bounded query도 NEW를 유지할지 여부
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        if not isinstance(pending_order, Order):
            raise TypeError("pending_order must be an Order")
        if type(cancel_stays_active) is not bool:
            raise TypeError("cancel_stays_active must be a bool")

        self.pending_order = pending_order
        self.cancel_stays_active = cancel_stays_active
        self.active_result = OrderResult(
            symbol=pending_order.symbol,
            client_order_id=pending_order.client_order_id,
            exchange_order_id="95001",
            status=OrderStatus.NEW,
            processed_at=FIXED_TIME,
        )
        partial_fill = Fill(
            exchange_order_id="95001",
            trade_id="55001",
            quantity=Decimal("0.12500000"),
            price=Decimal("2500.00"),
            fee_amount=Decimal("0"),
            fee_asset="USDT",
            fee_quote_amount=Decimal("0"),
            executed_at=FIXED_TIME,
        )
        self.canceled_result = OrderResult(
            symbol=pending_order.symbol,
            client_order_id=pending_order.client_order_id,
            exchange_order_id=partial_fill.exchange_order_id,
            status=OrderStatus.CANCELED,
            processed_at=FIXED_TIME,
            fills=(partial_fill,),
        )
        super().__init__(
            startup_result=self.active_result,
            submission_exchange_order_id="95002",
            submission_trade_id="55002",
        )
        self.cancel_completed = False
        self.operation_trace: list[str] = []
        self.submitted_orders: list[Order] = []

    def list_open_order_results(
        self,
        *,
        symbol: str,
    ) -> tuple[OrderResult, ...]:
        """
        함수 이름: list_open_order_results()
        기능: 개별 cancel 전에는 원 BUY만, 이후에는 빈 app open-order 집합을 반환한다.
        인자: symbol -> 조회한 Spot symbol
        반환값: 현재 authoritative open OrderResult tuple
        작성 날짜: 2026/08/29
        """
        if symbol != self.pending_order.symbol:
            raise AssertionError("unexpected manual-kill startup symbol")

        if self.cancel_completed and not self.cancel_stays_active:
            return ()
        return (self.active_result,)

    def query_order_result(self, *, order: Order) -> OrderResult:
        """
        함수 이름: query_order_result()
        기능: cancel 전 NEW와 cancel 뒤 terminal partial을 같은 client ID 조회로 반환한다.
        인자: order -> durable journal에서 복원한 원 BUY
        반환값: 현재 authoritative same-ID OrderResult
        작성 날짜: 2026/08/29
        """
        if order.client_order_id != self.pending_order.client_order_id:
            raise AssertionError("manual-kill query changed the pending identity")

        self.query_client_order_ids.append(order.client_order_id)
        self.operation_trace.append("query:BUY")
        return (
            self.canceled_result
            if self.cancel_completed and not self.cancel_stays_active
            else self.active_result
        )

    def cancel_order(self, *, order: Order) -> OrderResult:
        """
        함수 이름: cancel_order()
        기능: journal로 설명되는 원 BUY 하나만 취소하고 terminal 응답을 후속 조회와 분리한다.
        인자: order -> 취소할 durable app-owned BUY
        반환값: 같은 identity의 CANCELED partial 응답
        작성 날짜: 2026/08/29
        """
        if order.client_order_id != self.pending_order.client_order_id:
            raise AssertionError("manual-kill canceled a foreign order")
        if self.cancel_completed:
            raise AssertionError("manual-kill cancel must be idempotently queried")

        self.operation_trace.append("cancel:BUY")
        self.cancel_completed = True
        return self.canceled_result  # Controller는 이 응답만 믿지 않고 같은 ID를 다시 조회해야 한다.

    def submit_order(self, *, order: Order) -> OrderResult:
        """
        함수 이름: submit_order()
        기능: startup은 신규 BUY 없이 복원된 partial 수량과 같은 recovery SELL 하나만 체결한다.
        인자: order -> Controller가 pending fsync 뒤 만든 recovery SELL
        반환값: submitted 수량 전체의 terminal FILLED 결과
        작성 날짜: 2026/08/29
        """
        if order.side is not OrderSide.SELL:
            raise AssertionError("manual-kill restart must not submit a new BUY")

        self.operation_trace.append("submit:SELL")
        self.submitted_orders.append(order)
        return super().submit_order(order=order)


def _create_recovery_controller(
    history_path: Path,
    client: RestartReconciliationRESTClient,
    *,
    command_gate: bool = False,
    order_retry_waiter: Callable[[timedelta], object] | None = None,
    web_socket_client: SynchronousAccountWebSocketClient | None = None,
    event_runtime_notifier: Callable[[], object] | None = None,
    application_lock: RLock | None = None,
    maximum_order_notional: Decimal | None = None,
    risk_policy_state: RiskPolicy | RiskPolicyUnavailable | None = None,
) -> tuple[TradingController, TradeHistoryController, Position]:
    """
    함수 이름: _create_recovery_controller()
    기능: concrete history sidecar와 ready REST/stream을 가진 testnet형 Controller를 만든다.
    인자: history_path -> 재실행 사이 공유할 durable JSONL 경로
        client -> startup exchange 사실을 제공할 REST fake
        command_gate -> 복구 전후 주문 gate를 관찰할지 결정하는 실행 mode 값
        order_retry_waiter -> same-ID 조회 전 deterministic 대기 대체 함수 또는 None
        web_socket_client -> ACK 시점 주입이 필요한 account WebSocket fake 또는 None
        event_runtime_notifier -> queue와 retry schedule wake를 받을 optional callback
        application_lock -> production worker와 공유할 optional application RLock
        maximum_order_notional -> 신규 BUY에만 적용할 optional quote 진입 상한
        risk_policy_state -> 복구와 신규 BUY에 사용할 explicit 위험 정책 또는 None
    반환값: Controller, history Controller와 mutable Position tuple
    작성 날짜: 2026/08/22
    """
    repository = TradeHistoryRepository(history_path)
    history_controller = TradeHistoryController(
        repository,
        clock=lambda: FIXED_TIME,
    )
    history_controller.load_trade_history()
    account = Account()
    position = Position()

    # Startup 구독은 event를 동기 삽입하지 않아 full REST account를 authoritative하게 유지한다.
    if web_socket_client is None:
        web_socket_client = SynchronousAccountWebSocketClient([])
    web_socket_client.synchronous_payload = None
    controller = TradingController(
        APIGateway(client),
        WebSocketGateway(
            web_socket_client,
            account_snapshot_callback=account.apply_stream_snapshot,
        ),
        account,
        _ready_market_snapshot(),
        command_gate=command_gate,
        position=position,
        trade_history_controller=history_controller,
        pending_order_recovery_enabled=True,
        risk_policy_state=(
            create_test_risk_policy()
            if risk_policy_state is None
            else risk_policy_state
        ),
        order_retry_waiter=order_retry_waiter,
        event_runtime_notifier=event_runtime_notifier,
        clock=lambda: FIXED_TIME,
        application_lock=application_lock,
        maximum_order_notional=maximum_order_notional,
    )
    controller.load_account()

    return controller, history_controller, position


class TestnetRestartReconciliationFlowTests(unittest.TestCase):
    """
    클래스 이름: TestnetRestartReconciliationFlowTests
    기능: crash recovery와 다음 process restart의 주문·Position·history 멱등성을 검증한다.
    작성 날짜: 2026/08/22
    """

    def test_runtime_fill_fsyncs_full_lifecycle_and_policy_version(self) -> None:
        """
        함수 이름: test_runtime_fill_fsyncs_full_lifecycle_and_policy_version()
        기능: 정상 BUY가 policy provenance와 PREPARED→SUBMITTED→TERMINAL→HISTORY_COMMITTED를 남기는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            client = _FilledSubmissionTestnetRESTClient()
            controller, history_controller, _ = _create_recovery_controller(
                history_path,
                client,
                command_gate=True,
                order_retry_waiter=lambda _delay: None,
            )
            controller.reconcile_startup_state()
            selected_stm = controller.fetch_selected_trading_logic(
                RegimeType.TYPE_0
            )
            selection = controller.commit_regime_selection(
                RegimeType.TYPE_0,
                selected_stm,
                command_id="select-lifecycle",
                expected_version=0,
            )
            controller.start_trading(
                command_id="start-lifecycle",
                expected_version=selection.version,
            )
            intent_id = "phase13-full-lifecycle-intent"

            # Production executor는 하나의 client ID로 주문·Position·history를 완료한다.
            outcomes = _submit_case_b_buy(
                controller,
                intent_id=intent_id,
            )
            self.assertEqual(len(outcomes), 1)
            repository = history_controller._repository
            journal_events = tuple(
                json.loads(line)
                for line in repository.pending_order_storage_path.read_text(
                    encoding="utf-8"
                ).splitlines()
            )
            self.assertEqual(
                tuple(event["operation"] for event in journal_events),
                (
                    "UPSERT",
                    "TRANSITION",
                    "TRANSITION",
                    "TRANSITION",
                    "REMOVE",
                ),
            )
            self.assertEqual(
                tuple(
                    event.get("lifecycle")
                    for event in journal_events[:-1]
                ),
                (
                    "PREPARED",
                    "SUBMITTED",
                    "TERMINAL",
                    "HISTORY_COMMITTED",
                ),
            )
            self.assertEqual(
                journal_events[0]["order"]["risk_policy_version"],
                create_test_risk_policy().version,
            )
            self.assertEqual(
                history_controller.get_pending_order_submission_counts(),
                ((intent_id, 1),),
            )

    def test_restart_preserves_exhausted_intent_budget(self) -> None:
        """
        함수 이름: test_restart_preserves_exhausted_intent_budget()
        기능: REMOVE된 다섯 attempt 후 fresh Controller가 같은 intent를 제출하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        intent_id = "phase13-exhausted-intent"
        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            repository = TradeHistoryRepository(history_path)

            # 확정 zero-fill 거부로 active lock을 해제하되 다섯 제출 예산은 event log에 남긴다.
            for submission_attempt in range(5):
                order = _make_pending_order(
                    intent_id=intent_id,
                    client_order_id=(
                        f"bat-phase13-budget-{submission_attempt}"
                    ),
                    submission_attempt=submission_attempt,
                    risk_policy_version=(
                        create_test_risk_policy().version
                    ),
                )
                repository.save_pending_order(order)
                repository.mark_pending_order_submission_rejected(
                    order.client_order_id
                )
                repository.delete_pending_order(order.client_order_id)

            clock = MutableUtcClock()
            client = _SubmissionRejectingTestnetRESTClient(clock)
            controller, history_controller, _ = _create_recovery_controller(
                history_path,
                client,
                command_gate=True,
                order_retry_waiter=lambda _delay: None,
            )
            controller.reconcile_startup_state()
            selected_stm = controller.fetch_selected_trading_logic(
                RegimeType.TYPE_0
            )
            selection = controller.commit_regime_selection(
                RegimeType.TYPE_0,
                selected_stm,
                command_id="select-exhausted-budget",
                expected_version=0,
            )
            controller.start_trading(
                command_id="start-exhausted-budget",
                expected_version=selection.version,
            )

            # Start도 durable count를 지우지 않으므로 여섯 번째 REST POST는 없다.
            self.assertEqual(
                _submit_case_b_buy(controller, intent_id=intent_id),
                (),
            )
            self.assertEqual(len(client.submitted_orders), 0)
            self.assertEqual(
                history_controller.get_pending_order_submission_counts(),
                ((intent_id, 5),),
            )

    def test_submitted_lifecycle_fsync_failure_blocks_rest_post(self) -> None:
        """
        함수 이름: test_submitted_lifecycle_fsync_failure_blocks_rest_post()
        기능: PREPARED 후 SUBMITTED fsync 실패가 REST mutation 전 gate를 잠그는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            client = _FilledSubmissionTestnetRESTClient()
            controller, history_controller, _ = _create_recovery_controller(
                history_path,
                client,
                command_gate=True,
                order_retry_waiter=lambda _delay: None,
            )
            controller.reconcile_startup_state()
            selected_stm = controller.fetch_selected_trading_logic(
                RegimeType.TYPE_0
            )
            selection = controller.commit_regime_selection(
                RegimeType.TYPE_0,
                selected_stm,
                command_id="select-submitted-fsync-fault",
                expected_version=0,
            )
            controller.start_trading(
                command_id="start-submitted-fsync-fault",
                expected_version=selection.version,
            )
            repository = history_controller._repository
            original_transition = (
                TradeHistoryRepository.transition_pending_order_lifecycle
            )

            def fail_submitted_transition(
                selected_repository: TradeHistoryRepository,
                client_order_id: str,
                lifecycle: PendingOrderRecoveryLifecycle,
            ) -> None:
                """
                함수 이름: fail_submitted_transition()
                기능: SUBMITTED fsync 경계만 실패시키고 나머지 transition은 concrete repository에 위임한다.
                인자: selected_repository -> transition 호출을 받은 concrete repository
                    client_order_id -> pending application order ID
                    lifecycle -> Controller가 기록하려는 lifecycle
                반환값: 없음
                작성 날짜: 2026/08/25
                """
                # 목표 SUBMITTED 전이만 실패시키고 이후 lifecycle은 원래 구현으로 전달한다.
                if lifecycle is PendingOrderRecoveryLifecycle.SUBMITTED:
                    raise OSError("controlled SUBMITTED fsync failure")
                original_transition(
                    selected_repository,
                    client_order_id,
                    lifecycle,
                )

            # SUBMITTED durable 증거 없이 Gateway POST를 호출하지 않는다.
            with mock_patch.object(
                TradeHistoryRepository,
                "transition_pending_order_lifecycle",
                autospec=True,
                side_effect=fail_submitted_transition,
            ):
                self.assertEqual(
                    _submit_case_b_buy(
                        controller,
                        intent_id="submitted-fsync-fault-intent",
                    ),
                    (),
                )
            self.assertEqual(client.submit_count, 0)
            self.assertFalse(controller.command_enabled)
            recovery_records = (
                history_controller.get_pending_order_recovery_records()
            )
            self.assertEqual(len(recovery_records), 1)
            self.assertIs(
                recovery_records[0].lifecycle,
                PendingOrderRecoveryLifecycle.PREPARED,
            )

    def test_unknown_and_partial_keep_same_identity_and_apply_one_delta(self) -> None:
        """
        함수 이름: test_unknown_and_partial_keep_same_identity_and_apply_one_delta()
        기능: UNKNOWN은 재제출 없이 같은 ID를 조회하고 partial 중복은 Position delta를 한 번만 반영하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        with TemporaryDirectory() as temporary_directory:
            unknown_history_path = (
                Path(temporary_directory) / "unknown-trades.jsonl"
            )
            unknown_clock = MutableUtcClock()
            unknown_client = _SubmissionRejectingTestnetRESTClient(
                unknown_clock
            )
            unknown_client.submit_steps = [_OrderResponseKind.UNKNOWN]
            unknown_client.query_steps = [_OrderResponseKind.NEW]
            unknown_controller, unknown_history, _ = (
                _create_recovery_controller(
                    unknown_history_path,
                    unknown_client,
                    command_gate=True,
                    order_retry_waiter=lambda _delay: None,
                )
            )
            unknown_controller.reconcile_startup_state()
            selected_stm = unknown_controller.fetch_selected_trading_logic(
                RegimeType.TYPE_0
            )
            selection = unknown_controller.commit_regime_selection(
                RegimeType.TYPE_0,
                selected_stm,
                command_id="select-unknown-lifecycle",
                expected_version=0,
            )
            unknown_controller.start_trading(
                command_id="start-unknown-lifecycle",
                expected_version=selection.version,
            )
            unknown_intent_id = "phase13-unknown-intent"

            # Timeout을 실패로 단정하지 않고 UNKNOWN journal과 같은 client identity를 유지한다.
            self.assertEqual(
                _submit_case_b_buy(
                    unknown_controller,
                    intent_id=unknown_intent_id,
                ),
                (),
            )
            self.assertEqual(
                _submit_case_b_buy(
                    unknown_controller,
                    intent_id=unknown_intent_id,
                ),
                (),
            )
            self.assertEqual(len(unknown_client.submitted_orders), 1)
            self.assertIs(
                unknown_history.get_pending_order_recovery_records()[0].lifecycle,
                PendingOrderRecoveryLifecycle.UNKNOWN,
            )

            # Same-ID query가 NEW를 확인해도 새 제출 없이 memory만 전진하고 durable UNKNOWN은 보수적으로 유지한다.
            unknown_query_time = FIXED_TIME + timedelta(seconds=1)
            unknown_clock.set(unknown_query_time)
            self.assertEqual(
                unknown_controller.trigger_order_reconciliation(
                    occurred_at=unknown_query_time,
                ),
                (),
            )
            self.assertEqual(len(unknown_client.submitted_orders), 1)
            self.assertEqual(len(unknown_client.queried_orders), 1)
            self.assertIs(
                unknown_client.submitted_orders[0].status,
                OrderStatus.NEW,
            )
            self.assertIs(
                unknown_history.get_pending_order_recovery_records()[0].lifecycle,
                PendingOrderRecoveryLifecycle.UNKNOWN,
            )
            self.assertIs(
                unknown_controller.status,
                TradingSessionStatus.RUNNING,
            )

            partial_history_path = (
                Path(temporary_directory) / "partial-trades.jsonl"
            )
            partial_clock = MutableUtcClock()
            partial_client = _SubmissionRejectingTestnetRESTClient(
                partial_clock
            )
            partial_client.submit_steps = [_OrderResponseKind.NEW]
            partial_controller, partial_history, partial_position = (
                _create_recovery_controller(
                    partial_history_path,
                    partial_client,
                    command_gate=True,
                    order_retry_waiter=lambda _delay: None,
                )
            )
            partial_controller.reconcile_startup_state()
            partial_stm = partial_controller.fetch_selected_trading_logic(
                RegimeType.TYPE_0
            )
            partial_selection = partial_controller.commit_regime_selection(
                RegimeType.TYPE_0,
                partial_stm,
                command_id="select-partial-lifecycle",
                expected_version=0,
            )
            partial_controller.start_trading(
                command_id="start-partial-lifecycle",
                expected_version=partial_selection.version,
            )
            _submit_case_b_buy(
                partial_controller,
                intent_id="phase13-partial-intent",
            )
            submitted_order = partial_client.submitted_orders[0]
            exchange_order_id = submitted_order.exchange_order_id
            if exchange_order_id is None:
                raise AssertionError("NEW response must publish exchange ID")
            partial_fill = Fill(
                exchange_order_id=exchange_order_id,
                trade_id="phase13-partial-fill",
                quantity=submitted_order.submitted_quantity / Decimal("2"),
                price=submitted_order.market_price_at_decision,
                fee_amount=Decimal("0"),
                fee_asset="USDT",
                fee_quote_amount=Decimal("0"),
                executed_at=FIXED_TIME,
            )
            partial_result = OrderResult(
                symbol=submitted_order.symbol,
                client_order_id=submitted_order.client_order_id,
                exchange_order_id=exchange_order_id,
                status=OrderStatus.PARTIALLY_FILLED,
                processed_at=FIXED_TIME,
                fills=(partial_fill,),
            )

            # 동일 executionReport를 두 번 적용해도 fill key가 중복 Position delta를 차단한다.
            self.assertTrue(
                partial_controller.observe_order_result(partial_result)
            )
            position_after_first_partial = partial_position.quantity
            self.assertTrue(
                partial_controller.observe_order_result(partial_result)
            )
            self.assertEqual(
                partial_position.quantity,
                position_after_first_partial,
            )
            self.assertEqual(
                position_after_first_partial,
                partial_fill.quantity,
            )
            self.assertIs(
                partial_history.get_pending_order_recovery_records()[0].lifecycle,
                PendingOrderRecoveryLifecycle.PARTIAL,
            )
            self.assertEqual(partial_history.trade_history.trades, ())

    def test_history_lifecycle_failure_keeps_gate_until_same_id_reconnect(
        self,
    ) -> None:
        """
        함수 이름: test_history_lifecycle_failure_keeps_gate_until_same_id_reconnect()
        기능: history fsync 후 lifecycle 실패가 exact same-ID reconnect 전까지 중복 Trade·주문을 차단하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            client = _FilledSubmissionTestnetRESTClient()
            controller, history_controller, position = (
                _create_recovery_controller(
                    history_path,
                    client,
                    command_gate=True,
                    order_retry_waiter=lambda _delay: None,
                )
            )
            controller.reconcile_startup_state()
            selected_stm = controller.fetch_selected_trading_logic(
                RegimeType.TYPE_0
            )
            selection = controller.commit_regime_selection(
                RegimeType.TYPE_0,
                selected_stm,
                command_id="select-history-lifecycle-fault",
                expected_version=0,
            )
            controller.start_trading(
                command_id="start-history-lifecycle-fault",
                expected_version=selection.version,
            )
            repository = history_controller._repository
            original_transition = (
                TradeHistoryRepository.transition_pending_order_lifecycle
            )
            history_fault_pending = True

            def fail_history_transition_once(
                selected_repository: TradeHistoryRepository,
                client_order_id: str,
                lifecycle: PendingOrderRecoveryLifecycle,
            ) -> None:
                """
                함수 이름: fail_history_transition_once()
                기능: 첫 HISTORY_COMMITTED transition만 실패시켜 append·journal 사이 crash를 재현한다.
                인자: selected_repository -> transition 호출을 받은 concrete repository
                    client_order_id -> pending application order ID
                    lifecycle -> Controller가 기록하려는 lifecycle
                반환값: 없음
                작성 날짜: 2026/08/25
                """
                nonlocal history_fault_pending

                # 최초 HISTORY_COMMITTED만 실패시켜 재시도의 내구 경계를 결정적으로 관찰한다.
                if (
                    history_fault_pending
                    and lifecycle
                    is PendingOrderRecoveryLifecycle.HISTORY_COMMITTED
                ):
                    history_fault_pending = False
                    raise OSError("controlled history lifecycle failure")
                original_transition(
                    selected_repository,
                    client_order_id,
                    lifecycle,
                )

            # Position·Trade는 한 번 반영되지만 outcome과 REMOVE는 lifecycle commit 전에 중단된다.
            with mock_patch.object(
                TradeHistoryRepository,
                "transition_pending_order_lifecycle",
                autospec=True,
                side_effect=fail_history_transition_once,
            ):
                self.assertEqual(
                    _submit_case_b_buy(
                        controller,
                        intent_id="history-lifecycle-fault-intent",
                    ),
                    (),
                )
            filled_quantity = position.quantity
            self.assertGreater(filled_quantity, Decimal("0"))
            self.assertEqual(len(history_controller.trade_history.trades), 1)
            self.assertFalse(controller.command_enabled)
            self.assertIs(
                history_controller.get_pending_order_recovery_records()[0].lifecycle,
                PendingOrderRecoveryLifecycle.TERMINAL,
            )

            # 재연결은 새 submit 없이 같은 client ID를 조회한 뒤 history transition·REMOVE만 완료한다.
            controller.reconnect_account_stream_after_reconciliation()
            self.assertEqual(client.submit_count, 1)
            self.assertEqual(len(client.query_client_order_ids), 1)
            self.assertEqual(position.quantity, filled_quantity)
            self.assertEqual(len(history_controller.trade_history.trades), 1)
            self.assertEqual(
                history_controller.get_pending_order_recovery_records(),
                (),
            )
            self.assertTrue(controller.command_enabled)

    def test_active_kill_restart_cancels_partial_then_liquidates_without_run(
        self,
    ) -> None:
        """
        함수 이름: test_active_kill_restart_cancels_partial_then_liquidates_without_run()
        기능: kill receipt 직후 crash가 same-ID BUY 취소·partial 복원·정확한 recovery SELL로 재개되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            pending_order = _make_pending_order(
                intent_id="manual-kill-restart-buy",
                client_order_id="bat-manual-kill-restart-buy-1",
                risk_policy_version=1,
            )
            repository = TradeHistoryRepository(history_path)
            repository.save_pending_order(pending_order)
            repository.transition_pending_order_lifecycle(
                pending_order.client_order_id,
                PendingOrderRecoveryLifecycle.SUBMITTED,
            )
            repository.save_manual_kill_control_state(
                ManualKillControlState(
                    active=True,
                    version=1,
                    command_id="activate-before-cleanup-crash",
                    expected_version=0,
                    behavior=ManualKillBehavior.CANCEL_AND_LIQUIDATE,
                    policy_version=1,
                )
            )
            policy = RiskPolicy(
                version=1,
                max_order_notional=None,
                max_position_notional=None,
                max_daily_loss=None,
                daily_loss_scope=DailyLossScope.REALIZED_ONLY,
                manual_kill_behavior=(
                    ManualKillBehavior.CANCEL_AND_LIQUIDATE
                ),
            )
            client = _ManualKillRestartRESTClient(pending_order)
            controller, history_controller, position = (
                _create_recovery_controller(
                    history_path,
                    client,
                    command_gate=True,
                    order_retry_waiter=lambda _delay: None,
                    risk_policy_state=policy,
                )
            )

            # Startup은 active BUY를 같은 ID로 조회·개별 취소·재조회하고 partial만 Position에 복원한다.
            controller.reconcile_startup_state()
            recovered_quantity = client.canceled_result.fills[0].quantity
            self.assertEqual(recovered_quantity, position.quantity)
            self.assertEqual(0, client.submit_count)
            self.assertEqual(
                (OrderSide.BUY,),
                tuple(
                    trade.side
                    for trade in history_controller.trade_history.trades
                ),
            )
            self.assertEqual(
                ["query:BUY", "cancel:BUY", "query:BUY"],
                client.operation_trace,
            )

            # Lifecycle resume은 전략 run 없이 stable recovery identity로 복원 수량만 한 번 SELL한다.
            self.assertTrue(controller.resume_manual_kill_cleanup())
            self.assertEqual(1, client.submit_count)
            self.assertEqual(1, len(client.submitted_orders))
            recovery_sell = client.submitted_orders[0]
            self.assertIs(recovery_sell.side, OrderSide.SELL)
            self.assertEqual(
                recovered_quantity,
                recovery_sell.submitted_quantity,
            )
            self.assertEqual(Decimal("0"), position.quantity)
            self.assertTrue(controller.manual_kill_cleanup_complete)
            self.assertFalse(controller.command_enabled)
            self.assertEqual(
                [
                    "query:BUY",
                    "cancel:BUY",
                    "query:BUY",
                    "submit:SELL",
                ],
                client.operation_trace,
            )

            # Terminal force outcome만 소비하면 복구 전용 STM이 끝나며 exact resume은 POST를 늘리지 않는다.
            asyncio.run(controller.drain_events())
            self.assertIs(controller.status, TradingSessionStatus.TERMINATED)
            self.assertTrue(controller.resume_manual_kill_cleanup())
            self.assertEqual(1, client.submit_count)
            self.assertEqual(
                (OrderSide.BUY, OrderSide.SELL),
                tuple(
                    trade.side
                    for trade in history_controller.trade_history.trades
                ),
            )
            self.assertEqual(
                (),
                history_controller.get_pending_order_recovery_records(),
            )

    def test_active_kill_restart_never_liquidates_while_cancel_is_active(
        self,
    ) -> None:
        """
        함수 이름: test_active_kill_restart_never_liquidates_while_cancel_is_active()
        기능: cancel 뒤 same-ID가 계속 active이면 startup과 recovery SELL을 모두 차단하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            pending_order = _make_pending_order(
                intent_id="manual-kill-active-after-cancel",
                client_order_id="bat-manual-kill-active-after-cancel-1",
                risk_policy_version=1,
            )
            repository = TradeHistoryRepository(history_path)
            repository.save_pending_order(pending_order)
            repository.transition_pending_order_lifecycle(
                pending_order.client_order_id,
                PendingOrderRecoveryLifecycle.SUBMITTED,
            )
            repository.save_manual_kill_control_state(
                ManualKillControlState(
                    active=True,
                    version=1,
                    command_id="activate-before-nonterminal-cancel",
                    expected_version=0,
                    behavior=ManualKillBehavior.CANCEL_AND_LIQUIDATE,
                    policy_version=1,
                )
            )
            client = _ManualKillRestartRESTClient(
                pending_order,
                cancel_stays_active=True,
            )
            controller, history_controller, position = (
                _create_recovery_controller(
                    history_path,
                    client,
                    command_gate=True,
                    order_retry_waiter=lambda _delay: None,
                )
            )

            # Binance cancel 응답과 네 번의 후속 조회가 terminal을 증명하지 못하면 startup은 READY가 아니다.
            with self.assertRaises(StartupOrderReconciliationError):
                controller.reconcile_startup_state()

            self.assertEqual(0, client.submit_count)
            self.assertEqual(Decimal("0"), position.quantity)
            self.assertFalse(controller.startup_reconciliation_complete)
            self.assertFalse(controller.manual_kill_cleanup_complete)
            self.assertFalse(controller.command_enabled)
            self.assertEqual(
                1,
                len(history_controller.get_pending_order_recovery_records()),
            )
            self.assertEqual(
                [
                    "query:BUY",
                    "cancel:BUY",
                    "query:BUY",
                    "query:BUY",
                    "query:BUY",
                    "query:BUY",
                ],
                client.operation_trace,
            )  # Cancel 호출은 한 번뿐이고 나머지는 모두 동일 원 주문의 권위 조회다.

    def test_recovered_position_is_liquidated_without_strategy_resume_or_duplicate_submit(
        self,
    ) -> None:
        """
        함수 이름: test_recovered_position_is_liquidated_without_strategy_resume_or_duplicate_submit()
        기능: cold restart Position을 public 복구 청산으로 한 번만 정리하고 다음 process가 0을 복원하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"

            # 첫 process는 정상 startup 뒤 terminal BUY와 durable history만 남기고 crash로 간주한다.
            first_client = _FilledSubmissionTestnetRESTClient(
                submission_exchange_order_id="94001",
                submission_trade_id="54001",
            )
            first_controller, first_history, first_position = (
                _create_recovery_controller(
                    history_path,
                    first_client,
                    command_gate=True,
                    order_retry_waiter=lambda _delay: None,
                )
            )
            first_controller.reconcile_startup_state()
            selected_stm = first_controller.fetch_selected_trading_logic(
                RegimeType.TYPE_0
            )
            selection = first_controller.commit_regime_selection(
                RegimeType.TYPE_0,
                selected_stm,
                command_id="select-before-recovery-crash",
                expected_version=0,
            )
            first_controller.start_trading(
                command_id="start-before-recovery-crash",
                expected_version=selection.version,
            )
            buy_outcomes = _submit_case_b_buy(
                first_controller,
                intent_id="buy-before-recovery-crash",
            )
            buy_result = first_client.result  # 둘째 process가 대조할 exact exchange execution이다.

            self.assertEqual(len(buy_outcomes), 1)
            self.assertGreater(first_position.quantity, Decimal("0"))
            self.assertEqual(len(first_history.trade_history.trades), 1)
            self.assertEqual(
                first_history.get_pending_order_recovery_records(),
                (),
            )

            # 둘째 process는 BUY를 재제출하지 않고 durable lot과 exchange provenance를 복원한다.
            liquidation_client = _FilledSubmissionTestnetRESTClient(
                startup_result=buy_result,
                submission_exchange_order_id="94002",
                submission_trade_id="54002",
            )
            recovered_controller, recovered_history, recovered_position = (
                _create_recovery_controller(
                    history_path,
                    liquidation_client,
                    command_gate=True,
                    order_retry_waiter=lambda _delay: None,
                )
            )
            recovered_controller.reconcile_startup_state()
            recovered_snapshot = recovered_controller.snapshot_session()
            expected_version = recovered_snapshot.version

            self.assertTrue(recovered_snapshot.has_open_position)
            self.assertFalse(recovered_controller.context.initialized)
            self.assertEqual(liquidation_client.submit_count, 0)

            # Public Operation은 run 없이 G-06 SELL 하나만 만들고 terminal outcome을 queue에 둔다.
            with mock_patch.object(
                TradingSTM,
                "run",
                autospec=True,
                side_effect=AssertionError("recovery liquidation must not run"),
            ) as forbidden_run:
                liquidation = (
                    recovered_controller.liquidate_recovered_position(
                        command_id="liquidate-recovered-position",
                        expected_version=expected_version,
                    )
                )

            forbidden_run.assert_not_called()
            self.assertIs(
                liquidation.status,
                TradingSessionStatus.STOPPING,
            )
            self.assertEqual(liquidation.transition_ids, ("G-06",))
            self.assertEqual(liquidation_client.submit_count, 1)
            sell_result = liquidation_client.result  # 셋째 process가 대조할 durable SELL 사실이다.

            # STOPPING 진행 조회와 stale·command-ID 충돌도 두 번째 force-sell을 만들지 않는다.
            stopping_progress = (
                recovered_controller.liquidate_recovered_position(
                    command_id="observe-recovered-liquidation-progress",
                    expected_version=liquidation.version,
                )
            )
            with self.assertRaises(TradingSessionError) as stale_progress:
                recovered_controller.liquidate_recovered_position(
                    command_id="stale-recovered-liquidation-progress",
                    expected_version=expected_version,
                )
            with self.assertRaises(TradingSessionError) as reused_command:
                recovered_controller.liquidate_recovered_position(
                    command_id="liquidate-recovered-position",
                    expected_version=liquidation.version,
                )
            self.assertIs(
                stopping_progress.status,
                TradingSessionStatus.STOPPING,
            )
            self.assertIs(
                stale_progress.exception.code,
                TradingSessionFailureCode.STALE_CONTEXT_VERSION,
            )
            self.assertIs(
                reused_command.exception.code,
                TradingSessionFailureCode.COMMAND_ID_REUSED,
            )
            self.assertEqual(liquidation_client.submit_count, 1)

            # FORCE_SELL_FINISHED는 Position과 history 저장 뒤에만 G-06F로 종료된다.
            drained_results = asyncio.run(recovered_controller.drain_events())
            self.assertEqual(
                tuple(
                    transition_id
                    for result in drained_results
                    for transition_id in result.transition_ids
                ),
                ("G-06F",),
            )
            self.assertIs(
                recovered_controller.status,
                TradingSessionStatus.TERMINATED,
            )
            self.assertEqual(recovered_position.quantity, Decimal("0"))
            self.assertEqual(
                tuple(
                    trade.side
                    for trade in recovered_history.trade_history.trades
                ),
                (OrderSide.BUY, OrderSide.SELL),
            )
            self.assertEqual(
                recovered_history.get_pending_order_recovery_records(),
                (),
            )

            # Exact duplicate는 최초 receipt를 replay하고 최신-version 새 command는 종료 no-op이다.
            duplicate = recovered_controller.liquidate_recovered_position(
                command_id="liquidate-recovered-position",
                expected_version=expected_version,
            )
            progress = recovered_controller.liquidate_recovered_position(
                command_id="observe-recovered-liquidation-terminal",
                expected_version=recovered_controller.context.version,
            )
            self.assertEqual(duplicate, liquidation)
            self.assertIs(progress.status, TradingSessionStatus.TERMINATED)
            self.assertEqual(liquidation_client.submit_count, 1)

            # 셋째 process는 BUY와 SELL을 모두 재생해 Position 0을 만들고 어떤 주문도 제출하지 않는다.
            final_client = RestartReconciliationRESTClient(buy_result)
            final_client.additional_recent_results = (sell_result,)
            final_controller, final_history, final_position = (
                _create_recovery_controller(
                    history_path,
                    final_client,
                    command_gate=True,
                    order_retry_waiter=lambda _delay: None,
                )
            )
            final_controller.reconcile_startup_state()

            self.assertFalse(final_controller.snapshot_session().has_open_position)
            self.assertEqual(final_position.quantity, Decimal("0"))
            self.assertEqual(len(final_history.trade_history.trades), 2)
            self.assertEqual(final_client.submit_count, 0)

    def test_recovered_liquidation_gate_race_rolls_back_before_submit(
        self,
    ) -> None:
        """
        함수 이름: test_recovered_liquidation_gate_race_rolls_back_before_submit()
        기능: 청산 Guard 직후 effect gate가 닫히면 주문 없이 NOT_STARTED로 복원되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        seed_order = _make_pending_order(
            intent_id="recovery-gate-race-buy",
            client_order_id="bat-recovery-gate-race-buy-1",
        )
        seed_result = _make_filled_result(
            seed_order,
            exchange_order_id="94501",
            trade_id="54501",
        )

        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            TradeHistoryRepository(history_path).save_pending_order(seed_order)
            client = _FilledSubmissionTestnetRESTClient(
                startup_result=seed_result,
                submission_exchange_order_id="94502",
                submission_trade_id="54502",
            )
            controller, history_controller, position = (
                _create_recovery_controller(
                    history_path,
                    client,
                    command_gate=True,
                    order_retry_waiter=lambda _delay: None,
                )
            )
            controller.reconcile_startup_state()

            # Prerequisite 통과 뒤 ForceSellAll 직전 gate close를 직접 주입한다.
            with mock_patch.object(
                TradingController,
                "_order_pipeline_enabled",
                False,
            ):
                with self.assertRaises(TradingSessionError) as gate_closed:
                    controller.liquidate_recovered_position(
                        command_id="liquidate-after-gate-race",
                        expected_version=0,
                    )

            self.assertIs(
                gate_closed.exception.code,
                TradingSessionFailureCode.CONNECTION_NOT_READY,
            )
            self.assertIs(controller.status, TradingSessionStatus.NOT_STARTED)
            self.assertIsNone(controller.session_id)
            self.assertFalse(controller.context.initialized)
            self.assertTrue(controller.snapshot_session().has_open_position)
            self.assertGreater(position.quantity, Decimal("0"))
            self.assertEqual(client.submit_count, 0)
            self.assertEqual(
                history_controller.get_pending_order_recovery_records(),
                (),
            )

            # 실패 command는 cache되지 않아 동일 ID 재시도가 정확히 한 SELL만 만든다.
            retry_result = controller.liquidate_recovered_position(
                command_id="liquidate-after-gate-race",
                expected_version=0,
            )
            self.assertIs(retry_result.status, TradingSessionStatus.STOPPING)
            self.assertEqual(client.submit_count, 1)
            asyncio.run(controller.drain_events())
            self.assertIs(controller.status, TradingSessionStatus.TERMINATED)
            self.assertEqual(position.quantity, Decimal("0"))

    def test_recovered_liquidation_preflight_failure_is_retryable(
        self,
    ) -> None:
        """
        함수 이름: test_recovered_liquidation_preflight_failure_is_retryable()
        기능: filter·commission 사전검증 실패를 원자 복원하고 동일 명령 ID로 한 SELL만 재시도하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        seed_order = _make_pending_order(
            intent_id="recovery-preflight-buy",
            client_order_id="bat-recovery-preflight-buy-1",
        )
        seed_result = _make_filled_result(
            seed_order,
            exchange_order_id="94601",
            trade_id="54601",
        )

        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            TradeHistoryRepository(history_path).save_pending_order(seed_order)
            client = _FilledSubmissionTestnetRESTClient(
                startup_result=seed_result,
                submission_exchange_order_id="94602",
                submission_trade_id="54602",
            )
            controller, history_controller, position = (
                _create_recovery_controller(
                    history_path,
                    client,
                    command_gate=True,
                    order_retry_waiter=lambda _delay: None,
                )
            )
            controller.reconcile_startup_state()
            client.reject_prepare_order = True

            # Preflight 예외은 journal·REST effect 전이므로 세션·Context를 명령 전으로 복원한다.
            with self.assertRaises(TradingSessionError) as preflight_failed:
                controller.liquidate_recovered_position(
                    command_id="liquidate-after-preflight-failure",
                    expected_version=0,
                )

            self.assertIs(
                preflight_failed.exception.code,
                TradingSessionFailureCode.COMMAND_DISABLED,
            )
            self.assertIs(controller.status, TradingSessionStatus.NOT_STARTED)
            self.assertIsNone(controller.session_id)
            self.assertFalse(controller.context.initialized)
            self.assertTrue(controller.snapshot_session().has_open_position)
            self.assertGreater(position.quantity, Decimal("0"))
            self.assertEqual(client.submit_count, 0)
            self.assertEqual(
                history_controller.get_pending_order_recovery_records(),
                (),
            )

            # 실패 command는 cache되지 않아 사전조건 해소 후 동일 ID가 SELL 하나만 생성한다.
            client.reject_prepare_order = False
            retry_result = controller.liquidate_recovered_position(
                command_id="liquidate-after-preflight-failure",
                expected_version=0,
            )
            self.assertIs(retry_result.status, TradingSessionStatus.STOPPING)
            self.assertEqual(client.submit_count, 1)
            asyncio.run(controller.drain_events())
            self.assertIs(controller.status, TradingSessionStatus.TERMINATED)
            self.assertEqual(position.quantity, Decimal("0"))

    def test_recovered_liquidation_ignores_entry_cap_and_closes_full_position(
        self,
    ) -> None:
        """
        함수 이름: test_recovered_liquidation_ignores_entry_cap_and_closes_full_position()
        기능: 가격 상승으로 평가액이 entry cap을 넘어도 복구 Position 정확한 전량만 청산하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        seed_order = _make_pending_order(
            intent_id="recovery-cap-buy",
            client_order_id="bat-recovery-cap-buy-1",
        )
        seed_result = _make_filled_result(
            seed_order,
            exchange_order_id="94611",
            trade_id="54611",
        )

        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            TradeHistoryRepository(history_path).save_pending_order(seed_order)
            client = _FilledSubmissionTestnetRESTClient(
                startup_result=seed_result,
                submission_exchange_order_id="94612",
                submission_trade_id="54612",
            )
            controller, history_controller, position = (
                _create_recovery_controller(
                    history_path,
                    client,
                    command_gate=True,
                    order_retry_waiter=lambda _delay: None,
                    maximum_order_notional=Decimal("100"),
                )
            )
            controller.reconcile_startup_state()

            # 0.25 ETH × 2,500 USDT는 100 USDT entry cap보다 크지만 SELL은 노출을 늘리지 않는다.
            recovered_quantity = position.quantity
            self.assertGreater(
                recovered_quantity * Decimal("2500"),
                Decimal("100"),
            )
            liquidation_result = controller.liquidate_recovered_position(
                command_id="liquidate-after-cap-increase",
                expected_version=0,
            )
            self.assertIs(
                liquidation_result.status,
                TradingSessionStatus.STOPPING,
            )
            self.assertEqual(client.submit_count, 1)
            asyncio.run(controller.drain_events())
            self.assertIs(controller.status, TradingSessionStatus.TERMINATED)
            self.assertEqual(position.quantity, Decimal("0"))
            liquidation_trade = history_controller.trade_history.trades[-1]
            self.assertIs(liquidation_trade.side, OrderSide.SELL)
            self.assertEqual(
                liquidation_trade.requested_quantity,
                recovered_quantity,
            )
            self.assertEqual(
                history_controller.get_pending_order_recovery_records(),
                (),
            )

    def test_recovered_liquidation_rejects_filter_rounded_dust(
        self,
    ) -> None:
        """
        함수 이름: test_recovered_liquidation_rejects_filter_rounded_dust()
        기능: exchange filter가 전량 수량을 내리면 PREPARED 저장과 SELL POST 전에 원자 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        seed_order = _make_pending_order(
            intent_id="recovery-filter-dust-buy",
            client_order_id="bat-recovery-filter-dust-buy-1",
        )
        seed_result = _make_filled_result(
            seed_order,
            exchange_order_id="94621",
            trade_id="54621",
        )

        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            TradeHistoryRepository(history_path).save_pending_order(seed_order)
            client = _FilledSubmissionTestnetRESTClient(
                startup_result=seed_result,
                submission_exchange_order_id="94622",
                submission_trade_id="54622",
            )
            controller, history_controller, position = (
                _create_recovery_controller(
                    history_path,
                    client,
                    command_gate=True,
                    order_retry_waiter=lambda _delay: None,
                )
            )
            controller.reconcile_startup_state()
            client.prepared_quantity_override = (
                position.quantity - Decimal("0.001")
            )

            # LOT_SIZE 내림 수량은 전량과 다르므로 journal이나 submit 횟수를 만들지 않는다.
            with self.assertRaises(TradingSessionError) as rounded_quantity:
                controller.liquidate_recovered_position(
                    command_id="liquidate-after-filter-resync",
                    expected_version=0,
                )

            self.assertIs(
                rounded_quantity.exception.code,
                TradingSessionFailureCode.COMMAND_DISABLED,
            )
            self.assertIs(controller.status, TradingSessionStatus.NOT_STARTED)
            self.assertIsNone(controller.session_id)
            self.assertFalse(controller.context.initialized)
            self.assertGreater(position.quantity, Decimal("0"))
            self.assertEqual(client.submit_count, 0)
            self.assertEqual(
                history_controller.get_pending_order_recovery_records(),
                (),
            )

            # Filter 재동기화를 모사한 뒤 같은 command ID가 정확한 전량 SELL 하나만 제출한다.
            client.prepared_quantity_override = None
            retry_result = controller.liquidate_recovered_position(
                command_id="liquidate-after-filter-resync",
                expected_version=0,
            )
            self.assertIs(retry_result.status, TradingSessionStatus.STOPPING)
            self.assertEqual(client.submit_count, 1)
            asyncio.run(controller.drain_events())
            self.assertIs(controller.status, TradingSessionStatus.TERMINATED)
            self.assertEqual(position.quantity, Decimal("0"))

    def test_recovered_residual_filter_failure_enters_reconciliation(
        self,
    ) -> None:
        """
        함수 이름: test_recovered_residual_filter_failure_enters_reconciliation()
        기능: terminal partial 뒤 잔량 filter 실패가 private 예외 대신 operator lock으로 닫히는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        seed_order = _make_pending_order(
            intent_id="recovery-residual-buy",
            client_order_id="bat-recovery-residual-buy-1",
        )
        seed_result = _make_filled_result(
            seed_order,
            exchange_order_id="94631",
            trade_id="54631",
        )

        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            TradeHistoryRepository(history_path).save_pending_order(seed_order)
            client = _FilledSubmissionTestnetRESTClient(
                startup_result=seed_result,
                submission_exchange_order_id="94632",
                submission_trade_id="54632",
            )
            controller, history_controller, position = (
                _create_recovery_controller(
                    history_path,
                    client,
                    command_gate=True,
                    order_retry_waiter=lambda _delay: None,
                )
            )
            controller.reconcile_startup_state()
            client.terminal_partial_quantity = Decimal("0.249")

            # 첫 전량 SELL은 terminal partial로 끝나고 G-06R이 3초 residual retry를 예약한다.
            liquidation_result = controller.liquidate_recovered_position(
                command_id="liquidate-before-residual-filter-failure",
                expected_version=0,
            )
            self.assertIs(
                liquidation_result.status,
                TradingSessionStatus.STOPPING,
            )
            self.assertEqual(client.submit_count, 1)
            asyncio.run(controller.drain_events())
            self.assertEqual(position.quantity, Decimal("0.001"))
            self.assertIs(controller.status, TradingSessionStatus.STOPPING)

            # 잔량이 exchange step에 맞지 않는 상황을 주입해 due worker 경계가 fail closed되는지 본다.
            client.prepared_quantity_override = Decimal("0.0005")
            controller.trigger_order_reconciliation(
                occurred_at=FIXED_TIME + timedelta(seconds=3),
            )

            self.assertIs(
                controller.status,
                TradingSessionStatus.RECONCILIATION_REQUIRED,
            )
            self.assertFalse(controller.command_enabled)
            self.assertEqual(client.submit_count, 1)
            self.assertEqual(position.quantity, Decimal("0.001"))
            self.assertEqual(
                history_controller.get_pending_order_recovery_records(),
                (),
            )  # Filter 실패 attempt는 PREPARED journal을 만들지 않는다.

    def test_recovered_liquidation_zero_free_balance_is_retryable(
        self,
    ) -> None:
        """
        함수 이름: test_recovered_liquidation_zero_free_balance_is_retryable()
        기능: 복원 Position과 free ETH 불일치를 effect 없이 복원하고 재동기화 후 동일 ID로 재시도한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        seed_order = _make_pending_order(
            intent_id="recovery-zero-free-buy",
            client_order_id="bat-recovery-zero-free-buy-1",
        )
        seed_result = _make_filled_result(
            seed_order,
            exchange_order_id="94701",
            trade_id="54701",
        )

        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            TradeHistoryRepository(history_path).save_pending_order(seed_order)
            client = _FilledSubmissionTestnetRESTClient(
                startup_result=seed_result,
                submission_exchange_order_id="94702",
                submission_trade_id="54702",
            )
            controller, history_controller, position = (
                _create_recovery_controller(
                    history_path,
                    client,
                    command_gate=True,
                    order_retry_waiter=lambda _delay: None,
                )
            )
            controller.reconcile_startup_state()

            # 최신 account stream 사실을 모사해 durable Position은 열려 있지만 free ETH는 0으로 만든다.
            controller._account.apply_stream_snapshot(
                AccountSnapshot(
                    balances=(
                        AssetBalance(
                            asset="ETH",
                            free=Decimal("0"),
                            locked=Decimal("0"),
                        ),
                    ),
                    updated_at=FIXED_TIME + timedelta(seconds=1),
                    is_full_snapshot=False,
                )
            )
            with self.assertRaises(TradingSessionError) as zero_quantity:
                controller.liquidate_recovered_position(
                    command_id="liquidate-after-zero-free-balance",
                    expected_version=0,
                )

            self.assertIs(
                zero_quantity.exception.code,
                TradingSessionFailureCode.POSITION_RECONCILIATION_REQUIRED,
            )
            self.assertIs(controller.status, TradingSessionStatus.NOT_STARTED)
            self.assertIsNone(controller.session_id)
            self.assertFalse(controller.context.initialized)
            self.assertTrue(controller.snapshot_session().has_open_position)
            self.assertGreater(position.quantity, Decimal("0"))
            self.assertEqual(client.submit_count, 0)
            self.assertEqual(
                history_controller.get_pending_order_recovery_records(),
                (),
            )

            # Account 재동기화 후 실패 command ID를 재사용해 SELL 하나만 제출한다.
            controller._account.apply_stream_snapshot(
                AccountSnapshot(
                    balances=(
                        AssetBalance(
                            asset="ETH",
                            free=position.quantity,
                            locked=Decimal("0"),
                        ),
                    ),
                    updated_at=FIXED_TIME + timedelta(seconds=2),
                    is_full_snapshot=False,
                )
            )
            retry_result = controller.liquidate_recovered_position(
                command_id="liquidate-after-zero-free-balance",
                expected_version=0,
            )
            self.assertIs(retry_result.status, TradingSessionStatus.STOPPING)
            self.assertEqual(client.submit_count, 1)
            asyncio.run(controller.drain_events())
            self.assertIs(controller.status, TradingSessionStatus.TERMINATED)
            self.assertEqual(position.quantity, Decimal("0"))

    def test_recovered_liquidation_save_then_raise_keeps_operator_lock(
        self,
    ) -> None:
        """
        함수 이름: test_recovered_liquidation_save_then_raise_keeps_operator_lock()
        기능: PREPARED fsync 뒤 예외의 모호한 cut-point를 rollback하지 않고 제출 없이 잠그는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        seed_order = _make_pending_order(
            intent_id="recovery-ambiguous-journal-buy",
            client_order_id="bat-recovery-ambiguous-journal-buy-1",
        )
        seed_result = _make_filled_result(
            seed_order,
            exchange_order_id="94801",
            trade_id="54801",
        )

        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            TradeHistoryRepository(history_path).save_pending_order(seed_order)
            client = _FilledSubmissionTestnetRESTClient(
                startup_result=seed_result,
                submission_exchange_order_id="94802",
                submission_trade_id="54802",
            )
            controller, history_controller, position = (
                _create_recovery_controller(
                    history_path,
                    client,
                    command_gate=True,
                    order_retry_waiter=lambda _delay: None,
                )
            )
            controller.reconcile_startup_state()
            repository = history_controller._repository
            original_save_pending = repository.save_pending_order

            def save_pending_then_raise(
                selected_repository: TradeHistoryRepository,
                order: Order,
            ) -> None:
                """
                함수 이름: save_pending_then_raise()
                기능: 실제 PREPARED file·directory fsync를 끝낸 직후 호출자에게 I/O 예외를 주입한다.
                인자: selected_repository -> class patch가 전달한 실제 repository
                    order -> 복구 청산의 제출 전 canonical SELL Order
                반환값: 반환하지 않음
                작성 날짜: 2026/08/24
                """
                if selected_repository is not repository:
                    raise AssertionError("unexpected pending repository")

                # Durable 성공 뒤 예외는 호출자가 저장 성공 여부를 판별할 수 없는 모호한 경계다.
                original_save_pending(order)
                raise OSError("injected post-fsync pending journal failure")

            # 저장 전 상태로 되돌리면 durable PREPARED를 숨길 수 있으므로 session lock을 유지해야 한다.
            with mock_patch.object(
                TradeHistoryRepository,
                "save_pending_order",
                autospec=True,
                side_effect=save_pending_then_raise,
            ):
                liquidation = controller.liquidate_recovered_position(
                    command_id="liquidate-after-ambiguous-journal",
                    expected_version=0,
                )

            self.assertIs(
                liquidation.status,
                TradingSessionStatus.RECONCILIATION_REQUIRED,
            )
            self.assertIs(
                controller.status,
                TradingSessionStatus.RECONCILIATION_REQUIRED,
            )
            self.assertIsNotNone(controller.session_id)
            self.assertTrue(controller.context.initialized)
            self.assertFalse(controller.command_enabled)
            self.assertTrue(controller.snapshot_session().has_open_position)
            self.assertGreater(position.quantity, Decimal("0"))
            self.assertEqual(client.submit_count, 0)
            pending_records = (
                history_controller.get_pending_order_recovery_records()
            )
            self.assertEqual(len(pending_records), 1)
            self.assertIs(
                pending_records[0].lifecycle,
                PendingOrderRecoveryLifecycle.PREPARED,
            )
            self.assertIs(pending_records[0].order.side, OrderSide.SELL)

            # Exact replay와 새 command 모두 현재 operator lock만 반환하고 두 번째 POST를 만들지 않는다.
            duplicate = controller.liquidate_recovered_position(
                command_id="liquidate-after-ambiguous-journal",
                expected_version=0,
            )
            progress = controller.liquidate_recovered_position(
                command_id="observe-ambiguous-journal-lock",
                expected_version=controller.context.version,
            )
            self.assertEqual(duplicate, liquidation)
            self.assertIs(
                progress.status,
                TradingSessionStatus.RECONCILIATION_REQUIRED,
            )
            self.assertEqual(client.submit_count, 0)

    def test_recovered_position_liquidation_guards_submit_zero_times(
        self,
    ) -> None:
        """
        함수 이름: test_recovered_position_liquidation_guards_submit_zero_times()
        기능: startup 미완료, Position 없음과 staged REGIME에서 복구 청산을 fail closed로 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        seed_order = _make_pending_order()
        client = RestartReconciliationRESTClient(
            _make_filled_result(seed_order)
        )
        client.include_recent_result = False

        with TemporaryDirectory() as temporary_directory:
            empty_history_path = Path(temporary_directory) / "empty.jsonl"
            controller, _, _ = _create_recovery_controller(
                empty_history_path,
                client,
                command_gate=True,
                order_retry_waiter=lambda _delay: None,
            )

            # Account와 stream만 준비된 상태는 startup reconciliation을 대신할 수 없다.
            with self.assertRaises(TradingSessionError) as before_startup:
                controller.liquidate_recovered_position(
                    command_id="liquidate-before-startup",
                    expected_version=0,
                )
            self.assertIs(
                before_startup.exception.code,
                TradingSessionFailureCode.POSITION_RECONCILIATION_REQUIRED,
            )

            # 정상 empty startup 뒤에는 청산할 Position이 없다는 typed failure를 반환한다.
            controller.reconcile_startup_state()
            with self.assertRaises(TradingSessionError) as without_position:
                controller.liquidate_recovered_position(
                    command_id="liquidate-without-position",
                    expected_version=0,
                )
            self.assertIs(
                without_position.exception.code,
                TradingSessionFailureCode.TRADING_NOT_STARTED,
            )
            self.assertEqual(client.submit_count, 0)

            # 실행 중인 정상 session은 recovery takeover보다 우선해 별도 active 오류로 거부한다.
            active_stm = controller.fetch_selected_trading_logic(
                RegimeType.TYPE_0
            )
            active_selection = controller.commit_regime_selection(
                RegimeType.TYPE_0,
                active_stm,
                command_id="select-before-active-recovery-request",
                expected_version=0,
            )
            active_session = controller.start_trading(
                command_id="start-before-active-recovery-request",
                expected_version=active_selection.version,
            )
            with self.assertRaises(TradingSessionError) as active_request:
                controller.liquidate_recovered_position(
                    command_id="liquidate-active-normal-session",
                    expected_version=active_session.version,
                )
            self.assertIs(
                active_request.exception.code,
                TradingSessionFailureCode.TRADING_ALREADY_ACTIVE,
            )
            controller.stop_trading(
                command_id="stop-after-active-recovery-request",
                expected_version=active_session.version,
            )
            self.assertEqual(client.submit_count, 0)

            # 복구 Position이 있어도 staged UI REGIME을 durable provenance로 덮어쓰지 않는다.
            recovered_history_path = (
                Path(temporary_directory) / "recovered.jsonl"
            )
            repository = TradeHistoryRepository(recovered_history_path)
            repository.save_pending_order(seed_order)
            recovered_client = RestartReconciliationRESTClient(
                _make_filled_result(seed_order)
            )
            recovered_controller, _, _ = _create_recovery_controller(
                recovered_history_path,
                recovered_client,
                command_gate=True,
                order_retry_waiter=lambda _delay: None,
            )
            recovered_controller.reconcile_startup_state()
            staged_stm = recovered_controller.fetch_selected_trading_logic(
                RegimeType.TYPE_0
            )
            staged_selection = recovered_controller.commit_regime_selection(
                RegimeType.TYPE_0,
                staged_stm,
                command_id="stage-regime-before-recovery-liquidation",
                expected_version=0,
            )

            with self.assertRaises(TradingSessionError) as staged_regime:
                recovered_controller.liquidate_recovered_position(
                    command_id="liquidate-with-staged-regime",
                    expected_version=staged_selection.version,
                )
            self.assertIs(
                staged_regime.exception.code,
                TradingSessionFailureCode.INVALID_SESSION_STATE,
            )
            self.assertEqual(recovered_client.submit_count, 0)

    def test_recovered_position_liquidation_rejects_mixed_or_unsupported_regime(
        self,
    ) -> None:
        """
        함수 이름: test_recovered_position_liquidation_rejects_mixed_or_unsupported_regime()
        기능: open lot의 durable REGIME이 혼합되거나 미지원이면 SELL 없이 청산 인수를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)

            # 같은 owner의 TYPE_0과 TYPE_1 BUY를 한 open lot에 저장해 기존 Position replay는 통과시킨다.
            mixed_history_path = temporary_path / "mixed-regime.jsonl"
            mixed_repository = TradeHistoryRepository(mixed_history_path)
            mixed_history = TradeHistoryController(
                mixed_repository,
                clock=lambda: FIXED_TIME,
            )
            mixed_history.load_trade_history()
            type_zero_order, type_zero_summary = make_order_execution(
                exchange_order_id="95001",
                side=OrderSide.BUY,
                quantity=Decimal("0.10"),
                fee_quote_amount=Decimal("0"),
                regime_type=RegimeType.TYPE_0,
            )
            type_one_order, type_one_summary = make_order_execution(
                exchange_order_id="95002",
                side=OrderSide.BUY,
                quantity=Decimal("0.10"),
                fee_quote_amount=Decimal("0"),
                regime_type=RegimeType.TYPE_1,
            )
            mixed_history.record_order_execution(
                type_zero_order,
                type_zero_summary,
            )
            mixed_history.record_order_execution(
                type_one_order,
                type_one_summary,
            )
            latest_mixed_result = OrderResult(
                symbol=type_one_order.symbol,
                client_order_id=type_one_order.client_order_id,
                exchange_order_id=type_one_order.exchange_order_id,
                status=OrderStatus.FILLED,
                processed_at=FIXED_TIME,
                fills=type_one_order.fills,
            )
            mixed_client = RestartReconciliationRESTClient(
                latest_mixed_result
            )
            mixed_controller, _, _ = _create_recovery_controller(
                mixed_history_path,
                mixed_client,
                command_gate=True,
                order_retry_waiter=lambda _delay: None,
            )
            mixed_controller.reconcile_startup_state()

            with self.assertRaises(TradingSessionError) as mixed_regime:
                mixed_controller.liquidate_recovered_position(
                    command_id="liquidate-mixed-regime-position",
                    expected_version=0,
                )
            self.assertIs(
                mixed_regime.exception.code,
                TradingSessionFailureCode.POSITION_RECONCILIATION_REQUIRED,
            )
            self.assertEqual(mixed_client.submit_count, 0)

            # 단일 TYPE_1 lot도 provenance는 명확하지만 registry가 미지원이므로 별도 typed 거부다.
            unsupported_history_path = temporary_path / "unsupported-regime.jsonl"
            unsupported_repository = TradeHistoryRepository(
                unsupported_history_path
            )
            unsupported_history = TradeHistoryController(
                unsupported_repository,
                clock=lambda: FIXED_TIME,
            )
            unsupported_history.load_trade_history()
            unsupported_order, unsupported_summary = make_order_execution(
                exchange_order_id="95003",
                side=OrderSide.BUY,
                quantity=Decimal("0.10"),
                fee_quote_amount=Decimal("0"),
                regime_type=RegimeType.TYPE_1,
            )
            unsupported_history.record_order_execution(
                unsupported_order,
                unsupported_summary,
            )
            unsupported_result = OrderResult(
                symbol=unsupported_order.symbol,
                client_order_id=unsupported_order.client_order_id,
                exchange_order_id=unsupported_order.exchange_order_id,
                status=OrderStatus.FILLED,
                processed_at=FIXED_TIME,
                fills=unsupported_order.fills,
            )
            unsupported_client = RestartReconciliationRESTClient(
                unsupported_result
            )
            unsupported_controller, _, _ = _create_recovery_controller(
                unsupported_history_path,
                unsupported_client,
                command_gate=True,
                order_retry_waiter=lambda _delay: None,
            )
            unsupported_controller.reconcile_startup_state()

            with self.assertRaises(TradingSessionError) as unsupported_regime:
                unsupported_controller.liquidate_recovered_position(
                    command_id="liquidate-unsupported-regime-position",
                    expected_version=0,
                )
            self.assertIs(
                unsupported_regime.exception.code,
                TradingSessionFailureCode.UNSUPPORTED_TRADING_LOGIC,
            )
            self.assertEqual(unsupported_client.submit_count, 0)

    def test_start_stays_disabled_until_startup_reconciliation_completes(
        self,
    ) -> None:
        """
        함수 이름: test_start_stays_disabled_until_startup_reconciliation_completes()
        기능: Testnet startup 복구 전 공개 start와 내부 주문 effect gate가 모두 닫히는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # Exchange recent 목록이 비어 있는 정상 fresh-start fixture를 준비한다.
        seed_order = _make_pending_order()  # Client/result identity 생성에만 쓰는 제출 전 주문이다.
        client = RestartReconciliationRESTClient(
            _make_filled_result(seed_order)
        )
        client.include_recent_result = False  # Startup exchange order 목록은 비워 둔다.

        # Account와 stream이 준비돼도 startup order·Position 복구 전에는 command를 열지 않는다.
        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            controller, _, _ = _create_recovery_controller(
                history_path,
                client,
                command_gate=True,
                order_retry_waiter=lambda _delay: None,
            )
            self.assertFalse(controller.startup_reconciliation_complete)
            self.assertFalse(controller.command_enabled)
            self.assertFalse(
                controller._order_pipeline_enabled
            )  # Direct effect seam도 startup 복구 전에는 닫혀 있어야 한다.

            # 지원 REGIME 선택 뒤 직접 start를 호출해도 Context와 외부 주문은 변경되지 않아야 한다.
            selected_stm = controller.fetch_selected_trading_logic(
                RegimeType.TYPE_0
            )
            selection = controller.commit_regime_selection(
                RegimeType.TYPE_0,
                selected_stm,
                command_id="select-before-startup-reconciliation",
                expected_version=0,
            )
            with self.assertRaises(TradingSessionError) as blocked_start:
                controller.start_trading(
                    command_id="start-before-startup-reconciliation",
                    expected_version=selection.version,
                )

            self.assertIs(
                blocked_start.exception.code,
                TradingSessionFailureCode.POSITION_RECONCILIATION_REQUIRED,
            )
            self.assertFalse(controller.context.initialized)
            self.assertEqual(client.submit_count, 0)

            # 동일 runtime에서 startup reconciliation을 완료하면 새 command만 정상 실행한다.
            controller.reconcile_startup_state()
            self.assertTrue(controller.startup_reconciliation_complete)
            self.assertTrue(controller.command_enabled)
            self.assertTrue(
                controller._order_pipeline_enabled
            )  # 복구 완료 뒤에만 정상 session의 주문 effect가 활성화된다.
            started = controller.start_trading(
                command_id="start-after-startup-reconciliation",
                expected_version=selection.version,
            )
            stopped = controller.stop_trading(
                command_id="stop-after-startup-reconciliation",
                expected_version=started.version,
            )

            self.assertIs(started.status, TradingSessionStatus.RUNNING)
            self.assertIs(stopped.status, TradingSessionStatus.TERMINATED)
            self.assertEqual(client.submit_count, 0)

    def test_disconnect_after_journal_blocks_post_and_keeps_recovery_record(
        self,
    ) -> None:
        """
        함수 이름: test_disconnect_after_journal_blocks_post_and_keeps_recovery_record()
        기능: stream flag가 callback보다 먼저 내려가면 durable journal 뒤에도 신규 POST를 차단한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        seed_order = _make_pending_order()
        client = RestartReconciliationRESTClient(
            _make_filled_result(seed_order)
        )
        client.include_recent_result = False

        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            controller, history_controller, _ = _create_recovery_controller(
                history_path,
                client,
                command_gate=True,
                order_retry_waiter=lambda _delay: None,
            )
            controller.reconcile_startup_state()
            selected_stm = controller.fetch_selected_trading_logic(
                RegimeType.TYPE_0
            )
            selection = controller.commit_regime_selection(
                RegimeType.TYPE_0,
                selected_stm,
                command_id="select-before-disconnect-race",
                expected_version=0,
            )
            controller.start_trading(
                command_id="start-before-disconnect-race",
                expected_version=selection.version,
            )
            repository = history_controller._repository
            original_save_pending = repository.save_pending_order

            def save_then_mark_transport_disconnected(
                selected_repository: TradeHistoryRepository,
                order: Order,
            ) -> None:
                """
                함수 이름: save_then_mark_transport_disconnected()
                기능: PREPARED fsync 직후 Gateway flag만 먼저 내려 callback lock 대기 창을 재현한다.
                인자: selected_repository -> class patch가 전달한 실제 repository
                    order -> 제출 직전 durable Order
                반환값: 없음
                작성 날짜: 2026/08/23
                """
                if selected_repository is not repository:
                    raise AssertionError("unexpected pending repository")
                original_save_pending(order)
                gateway = controller._web_socket_gateway
                with gateway._lock:
                    generation = gateway._account_generation
                gateway._mark_account_disconnected(
                    generation,
                    require_reconciliation=False,
                )  # application callback 전에도 account_connected는 이미 false여야 한다.

            # Submit action의 최초 gate는 통과시키고 filter·journal 이후 마지막 transport check를 겨냥한다.
            with mock_patch.object(
                TradeHistoryRepository,
                "save_pending_order",
                autospec=True,
                side_effect=save_then_mark_transport_disconnected,
            ):
                outcomes = _submit_case_b_buy(
                    controller,
                    intent_id="disconnect-after-journal-intent",
                )

            pending_orders = repository.get_pending_orders()
            self.assertEqual(outcomes, ())
            self.assertEqual(client.submit_count, 0)
            self.assertEqual(len(pending_orders), 1)
            self.assertEqual(
                pending_orders[0].intent_id,
                "disconnect-after-journal-intent",
            )
            self.assertIs(
                controller.status,
                TradingSessionStatus.RECONCILIATION_REQUIRED,
            )
            self.assertFalse(controller.command_enabled)

    def test_restart_recovers_same_order_once_without_duplicate_submit(
        self,
    ) -> None:
        """
        함수 이름: test_restart_recovers_same_order_once_without_duplicate_submit()
        기능: pending FILLED 복구 후 다시 실행해도 submit/query/history가 중복되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        order = _make_pending_order()
        result = _make_filled_result(order)

        # 첫 process가 제출 직전 journal만 남긴 crash 상태를 concrete sidecar로 준비한다.
        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            initial_repository = TradeHistoryRepository(history_path)
            initial_repository.save_pending_order(order)

            first_client = RestartReconciliationRESTClient(result)
            first_controller, first_history, first_position = (
                _create_recovery_controller(history_path, first_client)
            )
            first_controller.reconcile_startup_state()

            self.assertTrue(first_controller.startup_reconciliation_complete)
            self.assertEqual(first_client.submit_count, 0)
            self.assertEqual(
                first_client.query_client_order_ids,
                [order.client_order_id],
            )
            self.assertEqual(first_position.quantity, Decimal("0.25000000"))
            self.assertEqual(len(first_history.trade_history.trades), 1)
            self.assertEqual(initial_repository.get_pending_orders(), ())

            # 둘째 process는 durable history로 Position을 재생하고 같은 recent fill을 설명한다.
            second_client = RestartReconciliationRESTClient(result)
            second_controller, second_history, second_position = (
                _create_recovery_controller(history_path, second_client)
            )
            second_controller.reconcile_startup_state()

            self.assertTrue(second_controller.startup_reconciliation_complete)
            self.assertEqual(second_client.submit_count, 0)
            self.assertEqual(second_client.query_client_order_ids, [])
            self.assertEqual(second_position.quantity, Decimal("0.25000000"))
            self.assertEqual(len(second_history.trade_history.trades), 1)

    def test_v3_prepared_absence_clears_lock_and_preserves_attempt_audit(
        self,
    ) -> None:
        """
        함수 이름: test_v3_prepared_absence_clears_lock_and_preserves_attempt_audit()
        기능: v3 PREPARED를 bounded 부재 뒤 정리하고 attempt·audit과 신규 주문 gate를 복구한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        order = _make_pending_order()
        absent_result = OrderResult(
            symbol=order.symbol,
            client_order_id=order.client_order_id,
            status=OrderStatus.UNKNOWN,
            processed_at=FIXED_TIME,
            failure_reason="SCRIPTED_ORDER_NOT_VISIBLE",
            failure_kind=OrderResultFailureKind.ORDER_NOT_VISIBLE,
        )

        # Sidecar가 있지만 exchange open/recent에는 없는 journal-before-POST 상태를 준비한다.
        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            repository = TradeHistoryRepository(history_path)
            repository.save_pending_order(order)
            prepared_record = (
                repository.get_pending_order_recovery_records()[0]
            )
            self.assertIs(
                prepared_record.submission_provenance,
                PendingOrderSubmissionProvenance.SUBMITTED_FSYNC_PRECEDES_REST_POST,
            )
            client = RestartReconciliationRESTClient(absent_result)
            client.include_recent_result = False
            observed_delays: list[timedelta] = []
            controller, history_controller, position = (
                _create_recovery_controller(
                    history_path,
                    client,
                    command_gate=True,
                    order_retry_waiter=observed_delays.append,
                )
            )
            self.assertFalse(controller.command_enabled)

            # V4 writer는 SUBMITTED fsync 전에 POST를 시작할 수 없으므로 네 번의 exact 부재로 정리한다.
            controller.reconcile_startup_state()

            self.assertEqual(
                observed_delays,
                [
                    timedelta(seconds=1),
                    timedelta(seconds=2),
                    timedelta(seconds=4),
                    timedelta(seconds=8),
                ],
            )
            self.assertEqual(
                client.query_client_order_ids,
                [order.client_order_id] * 4,
            )
            self.assertEqual(client.submit_count, 0)
            self.assertEqual(repository.get_pending_orders(), ())
            self.assertEqual(history_controller.trade_history.trades, ())
            self.assertEqual(position.quantity, Decimal("0"))
            self.assertTrue(controller.startup_reconciliation_complete)
            self.assertTrue(controller.command_enabled)

            # REMOVE는 active lock만 풀고 최초 UPSERT의 schema·attempt audit를 append-only로 남긴다.
            journal_events = tuple(
                json.loads(line)
                for line in repository.pending_order_storage_path.read_text(
                    encoding="utf-8"
                ).splitlines()
            )
            self.assertEqual(
                tuple(event["operation"] for event in journal_events),
                ("UPSERT", "REMOVE"),
            )
            self.assertEqual(journal_events[0]["schema_version"], 4)
            self.assertEqual(journal_events[0]["lifecycle"], "PREPARED")
            self.assertEqual(
                journal_events[0]["order"]["submission_attempt"],
                order.submission_attempt,
            )
            self.assertEqual(
                TradeHistoryRepository(
                    history_path
                ).get_pending_order_submission_counts(),
                ((order.intent_id, order.submission_attempt + 1),),
            )

    def test_legacy_prepared_absence_remains_fail_closed(self) -> None:
        """
        함수 이름: test_legacy_prepared_absence_remains_fail_closed()
        기능: v1·v2 PREPARED는 bounded 부재만으로 POST 미시작을 추측하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # 두 legacy schema를 각각 독립 journal과 Controller process로 재시작한다.
        for schema_version in (1, 2):
            with self.subTest(schema_version=schema_version):
                order = _make_pending_order()
                absent_result = OrderResult(
                    symbol=order.symbol,
                    client_order_id=order.client_order_id,
                    status=OrderStatus.UNKNOWN,
                    processed_at=FIXED_TIME,
                    failure_reason="SCRIPTED_ORDER_NOT_VISIBLE",
                    failure_kind=OrderResultFailureKind.ORDER_NOT_VISIBLE,
                )

                # 과거 schema에는 SUBMITTED fsync-before-POST 계약이 없어 PREPARED가 모호하다.
                with TemporaryDirectory() as temporary_directory:
                    history_path = Path(temporary_directory) / "trades.jsonl"
                    repository = TradeHistoryRepository(history_path)
                    repository.save_pending_order(order)
                    sidecar_path = repository.pending_order_storage_path
                    legacy_event = json.loads(
                        sidecar_path.read_text(encoding="utf-8")
                    )
                    legacy_event["schema_version"] = schema_version
                    del legacy_event["order"]["risk_policy_version"]
                    del legacy_event["order"]["exit_pct_b_at_intent"]
                    if schema_version == 1:
                        del legacy_event[
                            "lifecycle"
                        ]  # V1 envelope은 lifecycle field를 지원하지 않는다.
                    sidecar_path.write_text(
                        json.dumps(
                            legacy_event,
                            separators=(",", ":"),
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    legacy_journal = sidecar_path.read_bytes()
                    replayed_record = TradeHistoryRepository(
                        history_path
                    ).get_pending_order_recovery_records()[0]
                    self.assertIs(
                        replayed_record.submission_provenance,
                        PendingOrderSubmissionProvenance.LEGACY_PREPARED_AMBIGUOUS,
                    )
                    client = RestartReconciliationRESTClient(absent_result)
                    client.include_recent_result = False
                    observed_delays: list[timedelta] = []
                    controller, _, _ = _create_recovery_controller(
                        history_path,
                        client,
                        command_gate=True,
                        order_retry_waiter=observed_delays.append,
                    )

                    with self.assertRaisesRegex(
                        StartupOrderReconciliationError,
                        "same-order startup query remained not visible",
                    ):
                        controller.reconcile_startup_state()

                    self.assertEqual(
                        observed_delays,
                        [
                            timedelta(seconds=1),
                            timedelta(seconds=2),
                            timedelta(seconds=4),
                            timedelta(seconds=8),
                        ],
                    )
                    self.assertEqual(
                        client.query_client_order_ids,
                        [order.client_order_id] * 4,
                    )
                    self.assertEqual(client.submit_count, 0)
                    self.assertEqual(sidecar_path.read_bytes(), legacy_journal)
                    self.assertEqual(repository.get_pending_orders(), (order,))
                    self.assertFalse(controller.startup_reconciliation_complete)
                    self.assertFalse(controller.command_enabled)

    def test_durable_submission_rejection_survives_crash_and_clears_after_absence(
        self,
    ) -> None:
        """
        함수 이름: test_durable_submission_rejection_survives_crash_and_clears_after_absence()
        기능: typed submit 거부 직후 crash를 재생해 네 번의 부재 뒤 journal만 안전하게 제거한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        clock = MutableUtcClock()
        rejecting_client = _SubmissionRejectingTestnetRESTClient(clock)

        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            first_controller, first_history, _ = _create_recovery_controller(
                history_path,
                rejecting_client,
                command_gate=True,
                order_retry_waiter=lambda _delay: None,
            )
            first_controller.reconcile_startup_state()
            selected_stm = first_controller.fetch_selected_trading_logic(
                RegimeType.TYPE_0
            )
            selection = first_controller.commit_regime_selection(
                RegimeType.TYPE_0,
                selected_stm,
                command_id="select-before-rejection-crash",
                expected_version=0,
            )
            first_controller.start_trading(
                command_id="start-before-rejection-crash",
                expected_version=selection.version,
            )

            # 최초 submit 거부 처리까지만 수행하고 예정된 same-ID query 전에 process crash로 간주한다.
            self.assertEqual(
                _submit_case_b_buy(
                    first_controller,
                    intent_id="durable-rejection-crash-intent",
                ),
                (),
            )
            submitted_order = rejecting_client.submitted_orders[0]
            first_repository = first_history._repository
            recovery_records = (
                first_repository.get_pending_order_recovery_records()
            )
            self.assertEqual(len(recovery_records), 1)
            self.assertIs(
                recovery_records[0].lifecycle,
                PendingOrderRecoveryLifecycle.SUBMISSION_REJECTED_CONFIRMED,
            )

            # 새 process는 신규 submit 없이 동일 ID의 정확한 부재 네 번만 확인한다.
            absent_result = OrderResult(
                symbol=submitted_order.symbol,
                client_order_id=submitted_order.client_order_id,
                status=OrderStatus.UNKNOWN,
                processed_at=FIXED_TIME,
                failure_reason="SCRIPTED_ORDER_NOT_VISIBLE",
                failure_kind=OrderResultFailureKind.ORDER_NOT_VISIBLE,
            )
            restarted_client = RestartReconciliationRESTClient(absent_result)
            restarted_client.include_recent_result = False
            observed_delays: list[timedelta] = []
            restarted_controller, restarted_history, restarted_position = (
                _create_recovery_controller(
                    history_path,
                    restarted_client,
                    order_retry_waiter=observed_delays.append,
                )
            )
            restarted_controller.reconcile_startup_state()

            self.assertEqual(restarted_client.submit_count, 0)
            self.assertEqual(
                restarted_client.query_client_order_ids,
                [submitted_order.client_order_id] * 4,
            )
            self.assertEqual(
                observed_delays,
                [
                    timedelta(seconds=1),
                    timedelta(seconds=2),
                    timedelta(seconds=4),
                    timedelta(seconds=8),
                ],
            )
            self.assertEqual(
                first_repository.get_pending_order_recovery_records(),
                (),
            )
            self.assertEqual(restarted_history.trade_history.trades, ())
            self.assertEqual(restarted_position.quantity, Decimal("0"))
            self.assertTrue(
                restarted_controller.startup_reconciliation_complete
            )

    def test_reconnect_retries_durable_pending_remove_before_unlock(
        self,
    ) -> None:
        """
        함수 이름: test_reconnect_retries_durable_pending_remove_before_unlock()
        기능: history 성공 뒤 sidecar REMOVE 실패가 재접속의 same-ID 재확인 전 gate를 열지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        client = _FilledSubmissionTestnetRESTClient()

        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            controller, history_controller, position = (
                _create_recovery_controller(
                    history_path,
                    client,
                    command_gate=True,
                    order_retry_waiter=lambda _delay: None,
                )
            )
            controller.reconcile_startup_state()
            selected_stm = controller.fetch_selected_trading_logic(
                RegimeType.TYPE_0
            )
            selection = controller.commit_regime_selection(
                RegimeType.TYPE_0,
                selected_stm,
                command_id="select-before-remove-failure",
                expected_version=0,
            )
            controller.start_trading(
                command_id="start-before-remove-failure",
                expected_version=selection.version,
            )
            repository = history_controller._repository
            original_delete_pending_order = (
                repository.delete_pending_order
            )
            delete_attempts = 0

            def fail_first_pending_remove(
                selected_repository: TradeHistoryRepository,
                client_order_id: str,
            ) -> None:
                """
                함수 이름: fail_first_pending_remove()
                기능: 최초 terminal history 저장 직후 sidecar REMOVE 한 번만 실패시킨다.
                인자: selected_repository -> patch가 전달한 concrete repository
                    client_order_id -> 제거하려던 runtime client order ID
                반환값: 두 번째 호출부터 원래 durable REMOVE 결과
                작성 날짜: 2026/08/23
                """
                nonlocal delete_attempts
                if selected_repository is not repository:
                    raise AssertionError("unexpected pending repository")
                delete_attempts += 1
                if delete_attempts == 1:
                    raise OSError("controlled pending REMOVE failure")
                original_delete_pending_order(client_order_id)

            # 첫 terminal fill은 Trade까지 저장하되 sidecar만 남겨 두 durable 경계를 분리한다.
            with mock_patch.object(
                TradeHistoryRepository,
                "delete_pending_order",
                autospec=True,
                side_effect=fail_first_pending_remove,
            ):
                outcomes = _submit_case_b_buy(
                    controller,
                    intent_id="remove-failure-buy-intent",
                )

            self.assertEqual(outcomes, ())
            self.assertEqual(len(history_controller.trade_history.trades), 1)
            self.assertGreater(position.quantity, Decimal("0"))
            self.assertEqual(
                len(repository.get_pending_order_recovery_records()),
                1,
            )
            self.assertFalse(controller.command_enabled)

            # Reconnect는 exact durable execution을 확인하고 REMOVE를 재시도한 뒤에만 gate를 연다.
            controller.mark_account_stream_reconciliation_required(
                "injected_after_pending_remove_failure"
            )
            with mock_patch.object(
                TradeHistoryRepository,
                "delete_pending_order",
                autospec=True,
                side_effect=fail_first_pending_remove,
            ):
                subscription = (
                    controller.reconnect_account_stream_after_reconciliation()
                )

            self.assertIsNotNone(subscription)
            self.assertEqual(delete_attempts, 2)
            self.assertEqual(
                repository.get_pending_order_recovery_records(),
                (),
            )
            self.assertEqual(len(history_controller.trade_history.trades), 1)
            self.assertTrue(controller.command_enabled)

    def test_reconnect_keeps_gate_closed_when_remove_wrote_then_raised(
        self,
    ) -> None:
        """
        함수 이름: test_reconnect_keeps_gate_closed_when_remove_wrote_then_raised()
        기능: durable REMOVE 후 호출 실패가 pending identity를 남겨 둔 채 command gate를 열지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        client = _FilledSubmissionTestnetRESTClient()

        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            controller, history_controller, _ = _create_recovery_controller(
                history_path,
                client,
                command_gate=True,
                order_retry_waiter=lambda _delay: None,
            )
            controller.reconcile_startup_state()
            selected_stm = controller.fetch_selected_trading_logic(
                RegimeType.TYPE_0
            )
            selection = controller.commit_regime_selection(
                RegimeType.TYPE_0,
                selected_stm,
                command_id="select-before-uncertain-remove",
                expected_version=0,
            )
            controller.start_trading(
                command_id="start-before-uncertain-remove",
                expected_version=selection.version,
            )
            repository = history_controller._repository
            original_delete_pending_order = repository.delete_pending_order

            def remove_then_raise(
                selected_repository: TradeHistoryRepository,
                client_order_id: str,
            ) -> None:
                """
                함수 이름: remove_then_raise()
                기능: REMOVE를 내구 기록한 뒤 호출자에게는 결과 불명 예외를 반환한다.
                인자: selected_repository -> patch가 전달한 concrete repository
                    client_order_id -> 제거할 runtime client order ID
                반환값: 없음
                작성 날짜: 2026/08/25
                """
                if selected_repository is not repository:
                    raise AssertionError("unexpected pending repository")

                # File에 REMOVE가 남은 직후 process가 성공 반환을 관찰하지 못한 경계를 만든다.
                original_delete_pending_order(client_order_id)
                raise OSError("controlled exception after durable REMOVE")

            # Trade·HISTORY_COMMITTED·REMOVE는 durable하지만 terminal outcome publication은 보류된다.
            with mock_patch.object(
                TradeHistoryRepository,
                "delete_pending_order",
                autospec=True,
                side_effect=remove_then_raise,
            ):
                outcomes = _submit_case_b_buy(
                    controller,
                    intent_id="uncertain-remove-buy-intent",
                )
            self.assertEqual(outcomes, ())
            self.assertFalse(controller.command_enabled)
            self.assertEqual(
                repository.get_pending_order_recovery_records(),
                (),
            )
            self.assertIsNotNone(controller.context.runtime.pending_order_id)

            # Explicit terminal completion replay가 없으면 reconnect는 불명 marker를 보존하고 gate를 유지한다.
            controller.mark_account_stream_reconciliation_required(
                "injected_after_uncertain_pending_remove"
            )
            subscription = (
                controller.reconnect_account_stream_after_reconciliation()
            )

            self.assertIsNotNone(subscription)
            self.assertFalse(controller.command_enabled)
            self.assertIsNotNone(controller.context.runtime.pending_order_id)
            self.assertEqual(asyncio.run(controller.drain_events()), ())

    def test_reconnect_terminal_fill_order_id_collision_keeps_gate_closed(
        self,
    ) -> None:
        """
        함수 이름: test_reconnect_terminal_fill_order_id_collision_keeps_gate_closed()
        기능: terminal 결과가 과거 숫자 order ID와 충돌하면 Position 적용 전부터 재접속까지 gate를 잠근다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            client = _FilledSubmissionTestnetRESTClient(
                submission_exchange_order_id="91001",
                submission_trade_id="41001",
            )
            controller, history_controller, position = (
                _create_recovery_controller(
                    history_path,
                    client,
                    command_gate=True,
                    order_retry_waiter=lambda _delay: None,
                )
            )
            controller.reconcile_startup_state()
            selected_stm = controller.fetch_selected_trading_logic(
                RegimeType.TYPE_0
            )
            selection = controller.commit_regime_selection(
                RegimeType.TYPE_0,
                selected_stm,
                command_id="select-before-reset-sell",
                expected_version=0,
            )
            split_result = controller.update_split_ratios(
                command_id="full-split-before-reset-sell",
                expected_version=selection.version,
                scale_in=Decimal("1"),
                scale_out=Decimal("1"),
            )
            controller.start_trading(
                command_id="start-before-reset-sell",
                expected_version=split_result.version,
            )

            # 실행 중 첫 BUY를 정상 저장한 뒤 Testnet reset의 숫자 ID 재사용을 다음 SELL에 주입한다.
            buy_outcomes = _submit_case_b_buy(
                controller,
                intent_id="buy-before-reset-collision",
            )
            self.assertEqual(len(buy_outcomes), 1)
            opened_quantity = position.quantity
            self.assertGreater(opened_quantity, Decimal("0"))
            self.assertEqual(len(history_controller.trade_history.trades), 1)
            client.submission_trade_id = "44001"
            exit_event = TradingEvent(
                TradingEventType.MARKET_DATA_UPDATED,
                FIXED_TIME,
                sequence_number=91_001,
            )
            exit_actions = create_exit_order_actions(
                StrategyType.CASE_B,
                ExitReason.TAKE_PROFIT,
                PositionReturnState.CASE_B_HOLDING,
                exit_event,
                controller.context,
                attempt_kind=OrderAttemptKind.INITIAL,
            )

            # Pair 충돌은 exchange fill을 Position에 적용하기 전에 sidecar와 lock을 유지한다.
            sell_outcomes: list[TradingEvent] = []
            for action in exit_actions:
                sell_outcomes.extend(controller._execute_action(action))
            self.assertEqual(sell_outcomes, [])
            self.assertEqual(position.quantity, opened_quantity)
            self.assertEqual(len(history_controller.trade_history.trades), 1)
            self.assertEqual(
                len(
                    history_controller.get_pending_order_recovery_records()
                ),
                1,
            )
            self.assertFalse(controller.command_enabled)

            controller.mark_account_stream_reconciliation_required(
                "injected_reset_id_collision_disconnect"
            )
            with self.assertRaisesRegex(
                AccountStreamRecoveryBlockedError,
                "reused exchange order ID",
            ):
                controller.reconnect_account_stream_after_reconciliation()

            self.assertEqual(position.quantity, opened_quantity)
            self.assertEqual(len(history_controller.trade_history.trades), 1)
            self.assertEqual(
                len(
                    history_controller.get_pending_order_recovery_records()
                ),
                1,
            )
            self.assertFalse(controller.command_enabled)

    def test_live_terminal_summary_conflict_blocks_before_position_delta(
        self,
    ) -> None:
        """
        함수 이름: test_live_terminal_summary_conflict_blocks_before_position_delta()
        기능: durable terminal partial과 다른 후속 누적 fill을 Position 반영 전에 차단하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        client = _FilledSubmissionTestnetRESTClient()

        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            controller, history_controller, position = (
                _create_recovery_controller(
                    history_path,
                    client,
                    command_gate=True,
                    order_retry_waiter=lambda _delay: None,
                )
            )
            controller.reconcile_startup_state()
            selected_stm = controller.fetch_selected_trading_logic(
                RegimeType.TYPE_0
            )
            selection = controller.commit_regime_selection(
                RegimeType.TYPE_0,
                selected_stm,
                command_id="select-before-terminal-summary-conflict",
                expected_version=0,
            )
            split_result = controller.update_split_ratios(
                command_id="full-split-before-terminal-summary-conflict",
                expected_version=selection.version,
                scale_in=Decimal("1"),
                scale_out=Decimal("1"),
            )
            controller.start_trading(
                command_id="start-before-terminal-summary-conflict",
                expected_version=split_result.version,
            )

            def submit_terminal_partial(*, order: Order) -> OrderResult:
                """
                함수 이름: submit_terminal_partial()
                기능: 요청량 절반만 체결된 terminal CANCELED BUY를 durable history 대상으로 만든다.
                인자: order -> journal fsync를 마친 runtime BUY
                반환값: 한 partial fill을 가진 terminal OrderResult
                작성 날짜: 2026/08/23
                """
                partial_fill = Fill(
                    exchange_order_id="94001",
                    trade_id="44001",
                    quantity=order.submitted_quantity / Decimal("2"),
                    price=Decimal("2500.00"),
                    fee_amount=Decimal("0"),
                    fee_asset="USDT",
                    fee_quote_amount=Decimal("0"),
                    executed_at=FIXED_TIME,
                )
                client.submit_count += 1
                client.result = OrderResult(
                    symbol=order.symbol,
                    client_order_id=order.client_order_id,
                    exchange_order_id=partial_fill.exchange_order_id,
                    status=OrderStatus.CANCELED,
                    processed_at=FIXED_TIME,
                    fills=(partial_fill,),
                )
                client.include_recent_result = True

                return client.result  # Reconnect/query fixture도 최초 durable 누적값을 공유한다.

            # 최초 terminal partial은 정상적으로 Position과 history에 한 번 반영한다.
            with mock_patch.object(
                client,
                "submit_order",
                side_effect=submit_terminal_partial,
            ):
                buy_outcomes = _submit_case_b_buy(
                    controller,
                    intent_id="terminal-summary-conflict-buy",
                )
            self.assertEqual(len(buy_outcomes), 1)
            original_quantity = position.quantity
            self.assertGreater(original_quantity, Decimal("0"))
            durable_trade = history_controller.trade_history.trades[0]
            original_result = client.result

            # 같은 pair에 새 partial fill을 더한 terminal 결과는 과거 Trade와 다른 summary다.
            additional_fill = Fill(
                exchange_order_id="94001",
                trade_id="44002",
                quantity=original_quantity / Decimal("2"),
                price=Decimal("2500.00"),
                fee_amount=Decimal("0"),
                fee_asset="USDT",
                fee_quote_amount=Decimal("0"),
                executed_at=FIXED_TIME + timedelta(seconds=1),
            )
            conflicting_result = OrderResult(
                symbol=original_result.symbol,
                client_order_id=original_result.client_order_id,
                exchange_order_id=original_result.exchange_order_id,
                status=OrderStatus.CANCELED,
                processed_at=additional_fill.executed_at,
                fills=(*original_result.fills, additional_fill),
            )
            self.assertTrue(controller.observe_order_result(conflicting_result))

            self.assertEqual(position.quantity, original_quantity)
            self.assertEqual(
                history_controller.trade_history.trades,
                (durable_trade,),
            )
            self.assertFalse(controller.command_enabled)

    def test_history_commit_with_stale_journal_is_confirmed_then_removed(
        self,
    ) -> None:
        """
        함수 이름: test_history_commit_with_stale_journal_is_confirmed_then_removed()
        기능: history commit 뒤 sidecar REMOVE만 실패한 상태를 같은 exchange 체결 확인 후 정리한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        order = _make_pending_order()
        result = _make_filled_result(order)

        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            repository = TradeHistoryRepository(history_path)
            repository.save_pending_order(order)
            pending_journal_snapshot = (
                repository.pending_order_storage_path.read_bytes()
            )  # REMOVE 전 durable bytes를 보존해 process crash disk 관점을 재현한다.
            first_controller, _, _ = _create_recovery_controller(
                history_path,
                RestartReconciliationRESTClient(result),
                order_retry_waiter=lambda _delay: None,
            )
            first_controller.reconcile_startup_state()

            # 직전 process가 history fsync 뒤 sidecar REMOVE에서 멈춘 disk snapshot을 다시 만든다.
            crash_repository = TradeHistoryRepository(history_path)
            crash_repository.pending_order_storage_path.write_bytes(
                pending_journal_snapshot
            )
            second_client = RestartReconciliationRESTClient(result)
            second_controller, second_history, second_position = (
                _create_recovery_controller(
                    history_path,
                    second_client,
                    order_retry_waiter=lambda _delay: None,
                )
            )
            second_controller.reconcile_startup_state()

            self.assertEqual(
                second_client.query_client_order_ids,
                [order.client_order_id],
            )
            self.assertEqual(crash_repository.get_pending_orders(), ())
            self.assertEqual(len(second_history.trade_history.trades), 1)
            self.assertEqual(second_position.quantity, Decimal("0.25000000"))
            self.assertTrue(second_controller.startup_reconciliation_complete)

    def test_history_client_id_reuse_with_different_exchange_order_blocks(
        self,
    ) -> None:
        """
        함수 이름: test_history_client_id_reuse_with_different_exchange_order_blocks()
        기능: stale journal과 같은 client ID의 다른 Binance 주문을 완료된 history로 오인하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        order = _make_pending_order()
        original_result = _make_filled_result(order)

        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            repository = TradeHistoryRepository(history_path)
            repository.save_pending_order(order)
            pending_journal_snapshot = (
                repository.pending_order_storage_path.read_bytes()
            )  # 동일 client identity의 중복 UPSERT 없이 stale REMOVE crash만 재현한다.
            first_controller, _, _ = _create_recovery_controller(
                history_path,
                RestartReconciliationRESTClient(original_result),
                order_retry_waiter=lambda _delay: None,
            )
            first_controller.reconcile_startup_state()

            # 직전 REMOVE가 남지 않은 disk처럼 stale journal을 복원한 뒤 다른 exchange identity를 주입한다.
            crash_repository = TradeHistoryRepository(history_path)
            crash_repository.pending_order_storage_path.write_bytes(
                pending_journal_snapshot
            )
            conflicting_result = _make_filled_result(
                _make_pending_order(),
                exchange_order_id="91002",
                trade_id="31002",
            )
            second_client = RestartReconciliationRESTClient(
                conflicting_result
            )
            second_controller, second_history, second_position = (
                _create_recovery_controller(
                    history_path,
                    second_client,
                    order_retry_waiter=lambda _delay: None,
                )
            )

            with self.assertRaisesRegex(
                StartupOrderReconciliationError,
                "durable exchange identity",
            ):
                second_controller.reconcile_startup_state()

            self.assertEqual(crash_repository.get_pending_orders(), (order,))
            self.assertEqual(len(second_history.trade_history.trades), 1)
            self.assertEqual(second_position.quantity, Decimal("0.25000000"))
            self.assertFalse(second_controller.startup_reconciliation_complete)

    def test_reset_reused_exchange_id_with_new_client_blocks_startup(
        self,
    ) -> None:
        """
        함수 이름: test_reset_reused_exchange_id_with_new_client_blocks_startup()
        기능: Testnet reset이 과거 숫자 order ID를 새 client ID에 재사용해도 READY로 열리지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        old_order = _make_pending_order()
        old_result = _make_filled_result(old_order)

        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            repository = TradeHistoryRepository(history_path)
            repository.save_pending_order(old_order)
            first_controller, _, _ = _create_recovery_controller(
                history_path,
                RestartReconciliationRESTClient(old_result),
                order_retry_waiter=lambda _delay: None,
            )
            first_controller.reconcile_startup_state()

            # Reset 뒤 새 client ID 주문이 과거 숫자 ID를 받은 recent 결과만 남긴다.
            reset_order = _make_pending_order(
                intent_id="reset-reused-id-intent",
                client_order_id="bat-reset-reused-id-1",
            )
            reset_result = _make_filled_result(
                reset_order,
                exchange_order_id="91001",
                trade_id="41001",
            )
            second_controller, second_history, second_position = (
                _create_recovery_controller(
                    history_path,
                    RestartReconciliationRESTClient(reset_result),
                    order_retry_waiter=lambda _delay: None,
                )
            )

            with self.assertRaisesRegex(
                StartupOrderReconciliationError,
                "reused a durable exchange order ID",
            ):
                second_controller.reconcile_startup_state()

            self.assertEqual(len(second_history.trade_history.trades), 1)
            self.assertEqual(second_position.quantity, Decimal("0.25000000"))
            self.assertFalse(second_controller.startup_reconciliation_complete)

    def test_pending_reset_id_collision_blocks_before_position_or_delete(
        self,
    ) -> None:
        """
        함수 이름: test_pending_reset_id_collision_blocks_before_position_or_delete()
        기능: pending query의 재사용 order ID를 Position 적용과 journal 삭제 전에 차단하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        old_order = _make_pending_order()
        old_result = _make_filled_result(old_order)

        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            repository = TradeHistoryRepository(history_path)
            repository.save_pending_order(old_order)
            first_controller, _, _ = _create_recovery_controller(
                history_path,
                RestartReconciliationRESTClient(old_result),
                order_retry_waiter=lambda _delay: None,
            )
            first_controller.reconcile_startup_state()

            # Recent 목록에는 숨기고 same-client query에서만 숫자 ID 재사용 fill을 공개한다.
            reset_order = _make_pending_order(
                intent_id="pending-reset-collision-intent",
                client_order_id="bat-pending-reset-collision-1",
            )
            repository.save_pending_order(reset_order)
            reset_result = _make_filled_result(
                reset_order,
                exchange_order_id="91001",
                trade_id="41002",
            )
            reset_client = RestartReconciliationRESTClient(reset_result)
            reset_client.include_recent_result = False
            second_controller, second_history, second_position = (
                _create_recovery_controller(
                    history_path,
                    reset_client,
                    order_retry_waiter=lambda _delay: None,
                )
            )

            with self.assertRaisesRegex(
                StartupOrderReconciliationError,
                "reused a durable exchange order ID",
            ):
                second_controller.reconcile_startup_state()

            self.assertEqual(repository.get_pending_orders(), (reset_order,))
            self.assertEqual(len(second_history.trade_history.trades), 1)
            self.assertEqual(second_position.quantity, Decimal("0.25000000"))
            self.assertFalse(second_controller.startup_reconciliation_complete)

    def test_reconnect_rejects_changed_execution_for_durable_pair(
        self,
    ) -> None:
        """
        함수 이름: test_reconnect_rejects_changed_execution_for_durable_pair()
        기능: 같은 client/exchange ID라도 recent 누적 fill이 durable Trade와 다르면 재접속을 차단한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        order = _make_pending_order()
        result = _make_filled_result(order)

        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            TradeHistoryRepository(history_path).save_pending_order(order)
            client = RestartReconciliationRESTClient(result)
            controller, history_controller, position = (
                _create_recovery_controller(
                    history_path,
                    client,
                    command_gate=True,
                    order_retry_waiter=lambda _delay: None,
                )
            )
            controller.reconcile_startup_state()

            # Pair는 유지하되 수량과 fill ID를 바꿔 reset/손상된 execution summary를 재현한다.
            conflicting_fill = Fill(
                exchange_order_id="91001",
                trade_id="41003",
                quantity=Decimal("0.20000000"),
                price=Decimal("2500.00"),
                fee_amount=Decimal("0"),
                fee_asset="USDT",
                fee_quote_amount=Decimal("0"),
                executed_at=FIXED_TIME,
            )
            client.result = OrderResult(
                symbol=order.symbol,
                client_order_id=order.client_order_id,
                exchange_order_id=conflicting_fill.exchange_order_id,
                status=OrderStatus.FILLED,
                processed_at=FIXED_TIME,
                fills=(conflicting_fill,),
            )
            controller.mark_account_stream_reconciliation_required(
                "injected_changed_durable_execution"
            )

            with self.assertRaisesRegex(
                AccountStreamRecoveryBlockedError,
                "unexplained recent execution",
            ):
                controller.reconnect_account_stream_after_reconciliation()

            self.assertEqual(len(history_controller.trade_history.trades), 1)
            self.assertEqual(position.quantity, Decimal("0.25000000"))
            self.assertFalse(controller.command_enabled)

    def test_open_legacy_base_fee_history_requires_explicit_migration(
        self,
    ) -> None:
        """
        함수 이름: test_open_legacy_base_fee_history_requires_explicit_migration()
        기능: 열린 v1 base-fee lot을 신규 주문보다 먼저 typed migration 오류로 차단하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        order = _make_pending_order()
        result = _make_filled_result(order)

        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            repository = TradeHistoryRepository(history_path)
            legacy_buy = make_trade(
                schema_version=1,
                order_id="70001",
                trade_id="legacy-base-fee-buy",
                fee_asset="ETH",
                fee_amount=Decimal("0.002"),
                fee_quote_amount=Decimal("0.20"),
            )
            repository.save_this_trade_by_order_id(
                legacy_buy.order_id,
                legacy_buy,
            )
            client = RestartReconciliationRESTClient(result)
            client.include_recent_result = False
            controller, _, position = _create_recovery_controller(
                history_path,
                client,
            )

            # Legacy 의미를 추측 변환하지 않고 stable code를 가진 예외를 그대로 공개한다.
            with self.assertRaises(
                LegacyFeeAccountingMigrationRequiredError
            ) as raised:
                controller.reconcile_startup_state()

            self.assertEqual(
                raised.exception.code,
                "HISTORY_ACCOUNTING_MIGRATION_REQUIRED",
            )
            self.assertTrue(position.requires_legacy_fee_accounting_migration)
            self.assertFalse(controller.startup_reconciliation_complete)
            self.assertEqual(client.submit_count, 0)

    def test_disconnect_blocks_commands_until_full_rest_reconciliation(
        self,
    ) -> None:
        """
        함수 이름: test_disconnect_blocks_commands_until_full_rest_reconciliation()
        기능: account stream 단절 뒤 REST account/open-order 확인과 새 stream 전까지 주문을 잠그는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        clock = MutableUtcClock()
        rest_client = ScriptedOrderRESTClient(
            clock,
            submit_steps=(),
        )
        controller, _, _, _ = _create_started_controller(
            rest_client,
            clock,
        )
        self.assertTrue(controller.command_enabled)

        # 비정상 종료 callback은 session을 즉시 잠그고 이전 subscription identity를 폐기한다.
        controller.mark_account_stream_reconciliation_required(
            "injected_account_stream_disconnect"
        )
        self.assertFalse(controller.command_enabled)

        # Full account와 app-owned open-order 검증 성공 뒤에만 새 stream을 열고 RUNNING으로 복귀한다.
        subscription = (
            controller.reconnect_account_stream_after_reconciliation()
        )
        self.assertIsNotNone(subscription)
        self.assertTrue(controller.command_enabled)

    def test_reconnect_recent_durable_buy_preserves_position_provenance(
        self,
    ) -> None:
        """
        함수 이름: test_reconnect_recent_durable_buy_preserves_position_provenance()
        기능: open Position의 durable BUY가 recent app order에 있으면 재접속 provenance가 성공하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        order = _make_pending_order()
        result = _make_filled_result(order)

        # Startup에서 같은 BUY를 history와 Position에 복원해 재접속 provenance 기준을 만든다.
        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            TradeHistoryRepository(history_path).save_pending_order(order)
            client = RestartReconciliationRESTClient(result)
            controller, _, position = _create_recovery_controller(
                history_path,
                client,
                command_gate=True,
            )
            controller.reconcile_startup_state()
            client.recent_request_count = 0
            client.recent_requested.clear()

            # Disconnect 뒤 open에는 없는 완료 BUY를 recent app order에서 다시 읽어 복구한다.
            controller.mark_account_stream_reconciliation_required(
                "injected_account_stream_disconnect"
            )
            subscription = (
                controller.reconnect_account_stream_after_reconciliation()
            )

            self.assertIsNotNone(subscription)
            self.assertEqual(client.recent_request_count, 1)
            self.assertEqual(position.quantity, Decimal("0.25000000"))
            self.assertTrue(controller.command_enabled)

    def test_reconnect_gap_balance_drop_keeps_command_gate_closed(
        self,
    ) -> None:
        """
        함수 이름: test_reconnect_gap_balance_drop_keeps_command_gate_closed()
        기능: 새 stream ACK 뒤 두 번째 REST에서 ETH가 줄면 local Position과 command를 차단하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        order = _make_pending_order()
        result = _make_filled_result(order)

        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            TradeHistoryRepository(history_path).save_pending_order(order)
            client = RestartReconciliationRESTClient(result)
            controller, _, position = _create_recovery_controller(
                history_path,
                client,
                command_gate=True,
            )
            controller.reconcile_startup_state()
            self.assertEqual(position.quantity, Decimal("0.25000000"))

            # 첫 재접속 REST는 정상이고 signed ACK 뒤 snapshot만 Position 아래로 감소시킨다.
            first_reconnect_account = _account_rest_payload()
            post_subscribe_account = _account_rest_payload()
            post_subscribe_account["updateTime"] = (
                int(post_subscribe_account["updateTime"]) + 1
            )
            post_subscribe_account["balances"] = [
                {"asset": "ETH", "free": "0.10000000", "locked": "0"},
                {"asset": "USDT", "free": "100.00", "locked": "10.00"},
            ]
            client.account_payloads.extend(
                (first_reconnect_account, post_subscribe_account)
            )
            controller.mark_account_stream_reconciliation_required(
                "injected_gap_balance_drop"
            )

            with self.assertRaises(AccountStreamRecoveryBlockedError):
                controller.reconnect_account_stream_after_reconciliation()

            self.assertFalse(controller.command_enabled)
            self.assertEqual(
                controller.account.get_holdings("ETH"),
                Decimal("0.10000000"),
            )

    def test_reconnect_unexplained_recent_fill_keeps_command_gate_closed(
        self,
    ) -> None:
        """
        함수 이름: test_reconnect_unexplained_recent_fill_keeps_command_gate_closed()
        기능: 단절 중 다른 process의 app-prefix 체결을 local Position 없이 수용하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        order = _make_pending_order()
        result = _make_filled_result(order)

        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            TradeHistoryRepository(history_path).save_pending_order(order)
            client = RestartReconciliationRESTClient(result)
            controller, _, _ = _create_recovery_controller(
                history_path,
                client,
                command_gate=True,
            )
            controller.reconcile_startup_state()

            # 같은 prefix지만 durable state에 없는 체결을 원래 BUY provenance와 함께 반환한다.
            foreign_order = Order(
                intent_id="foreign-disconnect-intent",
                client_order_id="bat-foreign-disconnect-1",
                submission_attempt=1,
                symbol="ETHUSDT",
                side=OrderSide.BUY,
                strategy=StrategyType.CASE_B,
                regime_type=RegimeType.TYPE_0,
                requested_quantity=Decimal("0.10000000"),
                submitted_quantity=Decimal("0.10000000"),
                market_price_at_decision=Decimal("2500.00"),
            )
            foreign_fill = Fill(
                exchange_order_id="92002",
                trade_id="32002",
                quantity=foreign_order.submitted_quantity,
                price=Decimal("2500.00"),
                fee_amount=Decimal("0"),
                fee_asset="USDT",
                fee_quote_amount=Decimal("0"),
                executed_at=FIXED_TIME + timedelta(seconds=1),
            )
            client.additional_recent_results = (
                OrderResult(
                    symbol=foreign_order.symbol,
                    client_order_id=foreign_order.client_order_id,
                    exchange_order_id=foreign_fill.exchange_order_id,
                    status=OrderStatus.FILLED,
                    processed_at=foreign_fill.executed_at,
                    fills=(foreign_fill,),
                ),
            )
            controller.mark_account_stream_reconciliation_required(
                "injected_foreign_recent_execution"
            )

            with self.assertRaisesRegex(
                AccountStreamRecoveryBlockedError,
                "unexplained recent execution",
            ):
                controller.reconnect_account_stream_after_reconciliation()

            self.assertFalse(controller.command_enabled)

    def test_reconnect_ack_then_unknown_recent_fill_closes_new_stream(
        self,
    ) -> None:
        """
        함수 이름: test_reconnect_ack_then_unknown_recent_fill_closes_new_stream()
        기능: signed ACK 직후 생긴 미확인 app-owned FILLED BUY를 recent 재조회로 차단한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        order = _make_pending_order()
        result = _make_filled_result(order)
        web_socket_client = SynchronousAccountWebSocketClient([])

        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            TradeHistoryRepository(history_path).save_pending_order(order)
            client = RestartReconciliationRESTClient(result)
            controller, _, _ = _create_recovery_controller(
                history_path,
                client,
                command_gate=True,
                web_socket_client=web_socket_client,
            )
            controller.reconcile_startup_state()
            client.recent_request_count = 0
            client.recent_requested.clear()

            # ACK 전 recent에는 없고 ACK가 끝난 직후에만 app-owned BUY 체결을 공개한다.
            unknown_order = Order(
                intent_id="ack-gap-unknown-intent",
                client_order_id="bat-ack-gap-unknown-1",
                submission_attempt=1,
                symbol="ETHUSDT",
                side=OrderSide.BUY,
                strategy=StrategyType.CASE_B,
                regime_type=RegimeType.TYPE_0,
                requested_quantity=Decimal("0.10000000"),
                submitted_quantity=Decimal("0.10000000"),
                market_price_at_decision=Decimal("2500.00"),
            )
            unknown_fill = Fill(
                exchange_order_id="92003",
                trade_id="32003",
                quantity=unknown_order.submitted_quantity,
                price=Decimal("2500.00"),
                fee_amount=Decimal("0"),
                fee_asset="USDT",
                fee_quote_amount=Decimal("0"),
                executed_at=FIXED_TIME + timedelta(seconds=2),
            )
            unknown_result = OrderResult(
                symbol=unknown_order.symbol,
                client_order_id=unknown_order.client_order_id,
                exchange_order_id=unknown_fill.exchange_order_id,
                status=OrderStatus.FILLED,
                processed_at=unknown_fill.executed_at,
                fills=(unknown_fill,),
            )
            original_subscribe = web_socket_client.subscribe_account_info
            opened_subscriptions: list[FakeSubscription] = []

            def subscribe_then_publish_unknown_fill(
                on_message: Callable[[object], None],
                on_disconnect: Callable[[], None],
            ) -> FakeSubscription:
                """
                함수 이름: subscribe_then_publish_unknown_fill()
                기능: transport ACK 반환 직후 recent endpoint에 새 FILLED BUY를 노출한다.
                인자: on_message -> account event callback
                    on_disconnect -> account disconnect callback
                반환값: ACK를 완료한 fake subscription
                작성 날짜: 2026/08/23
                """
                subscription = original_subscribe(
                    on_message=on_message,
                    on_disconnect=on_disconnect,
                )
                opened_subscriptions.append(subscription)
                client.additional_recent_results = (
                    unknown_result,
                )  # ACK 이전 order REST라면 이 체결을 볼 수 없게 순서를 고정한다.

                return subscription

            controller.mark_account_stream_reconciliation_required(
                "injected_ack_gap_unknown_execution"
            )
            with mock_patch.object(
                web_socket_client,
                "subscribe_account_info",
                side_effect=subscribe_then_publish_unknown_fill,
            ):
                with self.assertRaisesRegex(
                    AccountStreamRecoveryBlockedError,
                    "unexplained recent execution",
                ):
                    controller.reconnect_account_stream_after_reconciliation()

            self.assertEqual(client.recent_request_count, 1)
            self.assertEqual(len(opened_subscriptions), 1)
            self.assertTrue(opened_subscriptions[0].closed)
            self.assertFalse(controller.command_enabled)

    def test_testnet_reset_blocks_worker_once_and_keeps_command_gate_closed(
        self,
    ) -> None:
        """
        함수 이름: test_testnet_reset_blocks_worker_once_and_keeps_command_gate_closed()
        기능: recent BUY가 사라진 reset provenance가 결정적 오류 한 번으로 worker를 멈추는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        order = _make_pending_order()
        result = _make_filled_result(order)

        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            TradeHistoryRepository(history_path).save_pending_order(order)
            client = RestartReconciliationRESTClient(result)
            controller, _, _ = _create_recovery_controller(
                history_path,
                client,
                command_gate=True,
            )
            controller.reconcile_startup_state()

            # Testnet reset처럼 durable BUY의 거래소 recent provenance만 제거한다.
            client.include_recent_result = False
            client.recent_request_count = 0
            client.recent_requested.clear()
            controller.mark_account_stream_reconciliation_required(
                "injected_testnet_reset_disconnect"
            )
            observed_errors: list[AccountStreamRecoveryBlockedError] = []
            retry_delays: list[float] = []

            def recover_account_stream() -> object:
                """
                함수 이름: recover_account_stream()
                기능: 실제 Controller 재접속의 결정적 오류 type을 보존한 뒤 worker에 다시 전달한다.
                인자: 없음
                반환값: 정상 반환 없이 AccountStreamRecoveryBlockedError 발생
                작성 날짜: 2026/08/23
                """
                try:
                    return controller.reconnect_account_stream_after_reconciliation()
                except AccountStreamRecoveryBlockedError as error:
                    observed_errors.append(error)
                    raise

            def record_retry_delay(delay_seconds: float) -> bool:
                """
                함수 이름: record_retry_delay()
                기능: 결정적 reset 오류가 transient backoff로 잘못 분류되면 간격을 기록한다.
                인자: delay_seconds -> worker가 선택한 잘못된 재시도 간격
                반환값: 추가 반복을 막기 위해 True
                작성 날짜: 2026/08/23
                """
                retry_delays.append(delay_seconds)
                return True

            worker = _AccountStreamRecoveryWorker(
                recover_account_stream,
                lambda: True,
                retry_waiter=record_retry_delay,
            )
            try:
                self.assertTrue(worker.request_recovery())
                self.assertTrue(client.recent_requested.wait(timeout=1.0))
            finally:
                worker.close()  # 결정적 failure 반환까지 join해 정확한 호출 수를 읽는다.

            self.assertEqual(client.recent_request_count, 1)
            self.assertEqual(len(observed_errors), 1)
            self.assertEqual(
                observed_errors[0].code,
                "ACCOUNT_STREAM_RECOVERY_BLOCKED",
            )
            self.assertEqual(retry_delays, [])
            self.assertFalse(controller.command_enabled)


if __name__ == "__main__":
    unittest.main()
