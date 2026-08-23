"""Phase 9 재시작 주문 복구가 신규 제출과 중복 history를 만들지 않는지 검증한다."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
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
    TradingSessionStatus,
)
from binance_auto_trader.bootstrap.application import (
    _AccountStreamRecoveryWorker,
)
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.trading.account import Account
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
)
from binance_auto_trader.domain.trading.position import (
    LegacyFeeAccountingMigrationRequiredError,
    Position,
)
from binance_auto_trader.domain.trading.states import (
    ExitReason,
    OrderAttemptKind,
    OrderSide,
    PositionReturnState,
    StrategyType,
)
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
from tests.unit.history.factories import make_trade


FIXED_TIME = datetime(2026, 8, 22, 5, 0, tzinfo=timezone.utc)


def _make_pending_order(
    *,
    intent_id: str = "restart-buy-intent",
    client_order_id: str = "bat-restart-buy-1",
) -> Order:
    """
    함수 이름: _make_pending_order()
    기능: crash 직전 sidecar에 남은 testnet BUY 주문 metadata를 만든다.
    인자: intent_id -> 재시작 뒤에도 보존할 application intent ID
        client_order_id -> session과 제출 시도를 포함한 Binance client order ID
    반환값: 제출 전 canonical Order
    작성 날짜: 2026/08/22
    """
    # 앱 prefix와 원 intent를 보존해 startup same-order query의 상관관계를 고정한다.
    return Order(
        intent_id=intent_id,
        client_order_id=client_order_id,
        submission_attempt=1,
        symbol="ETHUSDT",
        side=OrderSide.BUY,
        strategy=StrategyType.CASE_B,
        regime_type=RegimeType.TYPE_0,
        requested_quantity=Decimal("0.25000000"),
        submitted_quantity=Decimal("0.25000000"),
        market_price_at_decision=Decimal("2500.00"),
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

    def submit_order(self, *, order: Order) -> OrderResult:
        """
        함수 이름: submit_order()
        기능: runtime 주문을 한 번 FILLED 처리하고 이후 same-ID 조회의 authoritative 결과로 보존한다.
        인자: order -> Controller가 생성해 durable journal에 먼저 기록한 BUY
        반환값: 동일 client ID와 고정 exchange ID의 terminal FILLED 결과
        작성 날짜: 2026/08/23
        """
        self.submit_count += 1
        self.result = _make_filled_result(
            order,
            exchange_order_id=self.submission_exchange_order_id,
            trade_id=self.submission_trade_id,
        )
        self.include_recent_result = True

        return self.result  # 재접속 recent와 query가 동일 누적 fill을 재현한다.


def _create_recovery_controller(
    history_path: Path,
    client: RestartReconciliationRESTClient,
    *,
    command_gate: bool = False,
    order_retry_waiter: Callable[[timedelta], object] | None = None,
    web_socket_client: SynchronousAccountWebSocketClient | None = None,
) -> tuple[TradingController, TradeHistoryController, Position]:
    """
    함수 이름: _create_recovery_controller()
    기능: concrete history sidecar와 ready REST/stream을 가진 testnet형 Controller를 만든다.
    인자: history_path -> 재실행 사이 공유할 durable JSONL 경로
        client -> startup exchange 사실을 제공할 REST fake
        command_gate -> 복구 전후 주문 gate를 관찰할지 결정하는 실행 mode 값
        order_retry_waiter -> same-ID 조회 전 deterministic 대기 대체 함수 또는 None
        web_socket_client -> ACK 시점 주입이 필요한 account WebSocket fake 또는 None
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
        order_retry_waiter=order_retry_waiter,
        clock=lambda: FIXED_TIME,
    )
    controller.load_account()

    return controller, history_controller, position


class TestnetRestartReconciliationFlowTests(unittest.TestCase):
    """
    클래스 이름: TestnetRestartReconciliationFlowTests
    기능: crash recovery와 다음 process restart의 주문·Position·history 멱등성을 검증한다.
    작성 날짜: 2026/08/22
    """

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

    def test_prepared_journal_absence_remains_locked_after_four_observations(
        self,
    ) -> None:
        """
        함수 이름: test_prepared_journal_absence_remains_locked_after_four_observations()
        기능: PREPARED 주문은 네 번의 typed 부재 뒤에도 미제출로 추측하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
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
            client = RestartReconciliationRESTClient(absent_result)
            client.include_recent_result = False
            observed_delays: list[timedelta] = []
            controller, history_controller, position = (
                _create_recovery_controller(
                    history_path,
                    client,
                    order_retry_waiter=observed_delays.append,
                )
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
            self.assertEqual(repository.get_pending_orders(), (order,))
            self.assertEqual(history_controller.trade_history.trades, ())
            self.assertEqual(position.quantity, Decimal("0"))
            self.assertFalse(controller.startup_reconciliation_complete)

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
            first_controller, _, _ = _create_recovery_controller(
                history_path,
                RestartReconciliationRESTClient(result),
                order_retry_waiter=lambda _delay: None,
            )
            first_controller.reconcile_startup_state()

            # 직전 process가 history fsync 뒤 sidecar REMOVE에서 멈춘 상태만 다시 만든다.
            crash_repository = TradeHistoryRepository(history_path)
            crash_repository.save_pending_order(_make_pending_order())
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
            first_controller, _, _ = _create_recovery_controller(
                history_path,
                RestartReconciliationRESTClient(original_result),
                order_retry_waiter=lambda _delay: None,
            )
            first_controller.reconcile_startup_state()

            # 같은 client ID를 재사용했지만 exchange identity가 다른 충돌을 명시적으로 주입한다.
            crash_repository = TradeHistoryRepository(history_path)
            crash_repository.save_pending_order(_make_pending_order())
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
