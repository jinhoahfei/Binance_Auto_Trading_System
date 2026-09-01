"""Phase 8 Order pipeline의 partial, persistence와 중복 장애 행렬을 통합 검증한다."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch as mock_patch

from binance_auto_trader.adapters.binance.api_gateway import APIGateway
from binance_auto_trader.adapters.binance.websocket_gateway import (
    WebSocketGateway,
)
from binance_auto_trader.adapters.persistence.trade_history_repository import (
    TradeHistoryRepository,
)
from binance_auto_trader.application.regime_controller import RegimeController
from binance_auto_trader.application.trade_history_controller import (
    TradeHistoryController,
)
from binance_auto_trader.application.trading_controller import (
    OrderExecutionFailureCode,
    ReconciliationCauseCategory,
    ReconciliationCauseStatus,
    TradingController,
    TradingSessionStatus,
)
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.regime import RegimeSTM
from binance_auto_trader.domain.trading.account import Account
from binance_auto_trader.domain.trading.action_requests import (
    SubmitOrder,
    TradingActionRequest,
)
from binance_auto_trader.domain.trading.events import (
    TradingEvent,
    TradingEventType,
)
from binance_auto_trader.domain.trading.order import (
    Fill,
    Order,
    OrderResult,
    OrderStatus,
)
from binance_auto_trader.domain.trading.position import (
    Position,
    PositionStatus,
)
from binance_auto_trader.domain.trading.states import (
    ExitReason,
    OrderAttemptKind,
    OrderSide,
    PositionReturnState,
    StrategyType,
)
from binance_auto_trader.domain.trading.transitions.helpers import (
    create_entry_order_actions,
    create_exit_order_actions,
)

from tests.integration.test_account_stream_flow import (
    FakeAccountRESTClient,
    MARKET_UPDATED_AT,
    SynchronousAccountWebSocketClient,
    _ready_market_snapshot,
)
from tests.integration.phase13_risk_fixture import create_test_risk_policy


@dataclass(slots=True)
class MutableClock:
    """
    클래스 이름: MutableClock
    기능: 주문 재조회 delay와 체결 시각을 결정론적으로 전진시킨다.
    작성 날짜: 2026/08/22
    """

    current_time: datetime

    def __call__(self) -> datetime:
        """
        함수 이름: __call__()
        기능: 현재 테스트 UTC 시각을 반환한다.
        인자: 없음
        반환값: 현재 timezone-aware datetime
        작성 날짜: 2026/08/22
        """
        return self.current_time

    def advance(self, *, seconds: int) -> None:
        """
        함수 이름: advance()
        기능: scheduler due 시각까지 clock을 지정한 초만큼 전진시킨다.
        인자: seconds -> 전진할 초
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self.current_time += timedelta(seconds=seconds)  # 일정한 query backoff만 경과시킨다.


@dataclass(frozen=True, slots=True)
class FillSpecification:
    """
    클래스 이름: FillSpecification
    기능: 제출 수량 비율 또는 잔량으로 fake Fill을 재현한다.
    작성 날짜: 2026/08/22
    """

    trade_id: str
    quantity_fraction: Decimal | None
    price: Decimal
    fee_amount: Decimal
    executed_at: datetime


@dataclass(frozen=True, slots=True)
class OrderResultSpecification:
    """
    클래스 이름: OrderResultSpecification
    기능: 실제 Order 제출 수량에 맞춘 정규화 OrderResult를 생성한다.
    작성 날짜: 2026/08/22
    """

    exchange_order_id: str
    status: OrderStatus
    processed_at: datetime
    fill_specifications: tuple[FillSpecification, ...] = ()

    def build(self, order: Order) -> OrderResult:
        """
        함수 이름: build()
        기능: 주문의 client ID와 제출 수량을 사용해 fake 결과를 만든다.
        인자: order -> Gateway가 실제로 제출 또는 재조회한 Order
        반환값: Order aggregate가 검증할 수 있는 OrderResult
        작성 날짜: 2026/08/22
        """
        # Production 주문 수량과 같은 Decimal128 정밀도로 비율 fill을 계산한다.
        with localcontext() as decimal_context:
            decimal_context.prec = 34
            decimal_context.rounding = ROUND_HALF_EVEN
            built_fills: list[Fill] = []
            assigned_quantity = Decimal("0")
            for fill_specification in self.fill_specifications:
                quantity_fraction = fill_specification.quantity_fraction
                if quantity_fraction is None:
                    fill_quantity = order.submitted_quantity - assigned_quantity
                else:
                    fill_quantity = order.submitted_quantity * quantity_fraction
                assigned_quantity += fill_quantity

                # 누적 fill의 마지막 수량은 정밀도 정책 안에서 정확한 잔량을 사용한다.
                built_fills.append(
                    Fill(
                        exchange_order_id=self.exchange_order_id,
                        trade_id=fill_specification.trade_id,
                        quantity=fill_quantity,
                        price=fill_specification.price,
                        fee_amount=fill_specification.fee_amount,
                        fee_asset="USDT",
                        fee_quote_amount=fill_specification.fee_amount,
                        executed_at=fill_specification.executed_at,
                    )
                )

        return OrderResult(
            symbol=order.symbol,
            client_order_id=order.client_order_id,
            exchange_order_id=self.exchange_order_id,
            status=self.status,
            processed_at=self.processed_at,
            fills=tuple(built_fills),
        )  # raw Binance payload 없이 domain-normalized fake 결과만 넘긴다.


class ScriptedOrderRESTClient(FakeAccountRESTClient):
    """
    클래스 이름: ScriptedOrderRESTClient
    기능: account bootstrap과 주문 submit·query 시나리오를 한 fake port에서 제공한다.
    작성 날짜: 2026/08/22
    """

    def __init__(
        self,
        submit_specifications: tuple[OrderResultSpecification, ...],
        query_specifications: tuple[OrderResultSpecification, ...] = (),
    ) -> None:
        """
        함수 이름: __init__()
        기능: account payload와 순서가 고정된 주문 응답 queue를 초기화한다.
        인자: submit_specifications -> submit 호출별 결과 명세
            query_specifications -> same-order query 호출별 결과 명세
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        super().__init__([])
        self._submit_specifications = deque(submit_specifications)
        self._query_specifications = deque(query_specifications)
        self.submitted_orders: list[Order] = []
        self.queried_orders: list[Order] = []

    def submit_order(self, *, order: Order) -> OrderResult:
        """
        함수 이름: submit_order()
        기능: 제출 Order identity를 기록하고 다음 scripted 결과를 반환한다.
        인자: order -> Controller가 생성한 실제 Order aggregate
        반환값: 해당 제출에 예약된 OrderResult
        작성 날짜: 2026/08/22
        """
        self.submitted_orders.append(order)  # 신규 제출 횟수와 intent 재사용을 함께 증명한다.
        try:
            specification = self._submit_specifications.popleft()
        except IndexError as error:
            raise AssertionError("unexpected order submission") from error

        return specification.build(order)

    def query_order_result(self, *, order: Order) -> OrderResult:
        """
        함수 이름: query_order_result()
        기능: 재제출 없이 기존 Order identity의 다음 query 결과를 반환한다.
        인자: order -> 최초 submit에서 보존된 동일 Order aggregate
        반환값: 해당 재조회에 예약된 OrderResult
        작성 날짜: 2026/08/22
        """
        self.queried_orders.append(order)  # object identity로 same-order query 계약을 확인한다.
        try:
            specification = self._query_specifications.popleft()
        except IndexError as error:
            raise AssertionError("unexpected order query") from error

        return specification.build(order)

    def cancel_order(self, *, order: Order) -> OrderResult:
        """
        함수 이름: cancel_order()
        기능: 본 fault matrix에 없는 cancel 호출을 즉시 실패시킨다.
        인자: order -> 예상하지 않은 cancel 대상
        반환값: 정상 경로에서 반환하지 않음
        작성 날짜: 2026/08/22
        """
        raise AssertionError(
            f"unexpected order cancellation: {order.client_order_id}"
        )


@dataclass(frozen=True, slots=True)
class PipelineFixture:
    """
    클래스 이름: PipelineFixture
    기능: 실제 Phase 8 Controller·Position·JSONL 조합을 테스트에 제공한다.
    작성 날짜: 2026/08/22
    """

    controller: TradingController
    rest_client: ScriptedOrderRESTClient
    position: Position
    history_controller: TradeHistoryController
    repository: TradeHistoryRepository
    storage_path: Path
    clock: MutableClock


class OrderFaultMatrixIntegrationTests(unittest.TestCase):
    """
    클래스 이름: OrderFaultMatrixIntegrationTests
    기능: partial SELL, durable save, Position 장애와 query 중복을 종단 검증한다.
    작성 날짜: 2026/08/22
    """

    def setUp(self) -> None:
        """
        함수 이름: setUp()
        기능: 각 테스트에 격리된 거래 이력 디렉토리를 생성한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self._temporary_directory = TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)

    def _create_pipeline(
        self,
        *,
        submit_specifications: tuple[OrderResultSpecification, ...],
        query_specifications: tuple[OrderResultSpecification, ...] = (),
        scale_out: Decimal = Decimal("1"),
    ) -> PipelineFixture:
        """
        함수 이름: _create_pipeline()
        기능: ready account·market과 실제 Phase 8 collaborator를 시작한다.
        인자: submit_specifications -> 신규 제출에 사용할 scripted 결과
            query_specifications -> existing-order query에 사용할 scripted 결과
            scale_out -> SELL 주문 수량에 적용할 분할 비율
        반환값: RUNNING 상태의 PipelineFixture
        작성 날짜: 2026/08/22
        """
        # 시각, Position과 durable JSONL owner를 한 Controller에 주입한다.
        clock = MutableClock(MARKET_UPDATED_AT)
        rest_client = ScriptedOrderRESTClient(
            submit_specifications,
            query_specifications,
        )
        account = Account()
        web_socket_client = SynchronousAccountWebSocketClient([])
        web_socket_gateway = WebSocketGateway(
            web_socket_client,
            account_snapshot_callback=account.apply_stream_snapshot,
        )
        position = Position()
        storage_path = Path(self._temporary_directory.name) / "trades.jsonl"
        repository = TradeHistoryRepository(storage_path, clock=clock)
        history_controller = TradeHistoryController(repository, clock=clock)
        history_controller.load_trade_history()
        market_snapshot = _ready_market_snapshot()
        controller = TradingController(
            APIGateway(rest_client, clock=clock),
            web_socket_gateway,
            account,
            market_snapshot,
            command_gate=True,
            position=position,
            trade_history_controller=history_controller,
            risk_policy_state=create_test_risk_policy(),
            clock=clock,
        )

        # Account bootstrap 후 TYPE_0, 100% split과 session을 version 순서대로 commit한다.
        controller.load_account()
        regime_controller = RegimeController(
            RegimeSTM(),
            market_snapshot,
            controller,
        )
        selection = regime_controller.set_regime_type(
            RegimeType.TYPE_0,
            command_id="select-type0-fault-matrix",
            expected_version=0,
        )
        split_result = controller.update_split_ratios(
            command_id="split-fault-matrix",
            expected_version=selection.version,
            scale_in=Decimal("1"),
            scale_out=scale_out,
        )
        controller.start_trading(
            command_id="start-fault-matrix",
            expected_version=split_result.version,
        )

        return PipelineFixture(
            controller=controller,
            rest_client=rest_client,
            position=position,
            history_controller=history_controller,
            repository=repository,
            storage_path=storage_path,
            clock=clock,
        )

    @staticmethod
    def _execute_actions(
        controller: TradingController,
        actions: tuple[TradingActionRequest, ...],
    ) -> tuple[TradingEvent, ...]:
        """
        함수 이름: _execute_actions()
        기능: STM helper가 만든 patch와 submit을 실제 action executor에 순서대로 전달한다.
        인자: controller -> Phase 8 action executor를 소유한 Controller
            actions -> 순서 보존할 typed action tuple
        반환값: 동기 terminal 처리에서 생성된 outcome event tuple
        작성 날짜: 2026/08/22
        """
        # 주문 전 runtime patch를 생략하지 않고 production action 순서를 그대로 실행한다.
        outcomes: list[TradingEvent] = []
        for action in actions:
            outcomes.extend(controller._execute_action(action))

        return tuple(outcomes)

    @staticmethod
    def _entry_actions(
        fixture: PipelineFixture,
        *,
        sequence_number: int,
    ) -> tuple[TradingActionRequest, ...]:
        """
        함수 이름: _entry_actions()
        기능: Case B 최초 BUY의 runtime patch와 submit action을 만든다.
        인자: fixture -> 현재 clock과 Context를 가진 pipeline
            sequence_number -> 결정론적 intent ID에 사용할 event sequence
        반환값: entry patch와 SubmitOrder tuple
        작성 날짜: 2026/08/22
        """
        event = TradingEvent(
            TradingEventType.MARKET_DATA_UPDATED,
            fixture.clock(),
            sequence_number=sequence_number,
        )
        return create_entry_order_actions(
            StrategyType.CASE_B,
            OrderAttemptKind.INITIAL,
            event,
            fixture.controller.context,
        )

    @staticmethod
    def _exit_actions(
        fixture: PipelineFixture,
        *,
        attempt_kind: OrderAttemptKind,
        sequence_number: int,
    ) -> tuple[TradingActionRequest, ...]:
        """
        함수 이름: _exit_actions()
        기능: Case B TAKE_PROFIT SELL의 최초 또는 residual retry action을 만든다.
        인자: fixture -> 보존된 pending intent와 Context를 가진 pipeline
            attempt_kind -> 최초 SELL 또는 잔량 retry 구분
            sequence_number -> 최초 intent fallback에 사용할 event sequence
        반환값: exit patch와 SubmitOrder tuple
        작성 날짜: 2026/08/22
        """
        event = TradingEvent(
            TradingEventType.MARKET_DATA_UPDATED,
            fixture.clock(),
            sequence_number=sequence_number,
        )
        return create_exit_order_actions(
            StrategyType.CASE_B,
            ExitReason.TAKE_PROFIT,
            PositionReturnState.CASE_B_HOLDING,
            event,
            fixture.controller.context,
            attempt_kind=attempt_kind,
        )

    def test_terminal_partial_sell_records_cost_before_residual_retry(
        self,
    ) -> None:
        """
        함수 이름: test_terminal_partial_sell_records_cost_before_residual_retry()
        기능: terminal partial SELL이 사전 원가 Trade를 남기고 잔량 전에 FILLED를 내지 않음을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # BUY 전량, SELL 반량 terminal, 잔량 SELL 전량을 서로 다른 order로 준비한다.
        fixture = self._create_pipeline(
            submit_specifications=(
                OrderResultSpecification(
                    "101",
                    OrderStatus.FILLED,
                    MARKET_UPDATED_AT,
                    (
                        FillSpecification(
                            "101-buy",
                            None,
                            Decimal("2500.50"),
                            Decimal("0.10"),
                            MARKET_UPDATED_AT,
                        ),
                    ),
                ),
                OrderResultSpecification(
                    "102",
                    OrderStatus.CANCELED,
                    MARKET_UPDATED_AT + timedelta(seconds=1),
                    (
                        FillSpecification(
                            "102-sell-half",
                            Decimal("0.5"),
                            Decimal("3000"),
                            Decimal("0.05"),
                            MARKET_UPDATED_AT + timedelta(seconds=1),
                        ),
                    ),
                ),
                OrderResultSpecification(
                    "103",
                    OrderStatus.FILLED,
                    MARKET_UPDATED_AT + timedelta(seconds=2),
                    (
                        FillSpecification(
                            "103-sell-rest",
                            None,
                            Decimal("3100"),
                            Decimal("0.05"),
                            MARKET_UPDATED_AT + timedelta(seconds=2),
                        ),
                    ),
                ),
            )
        )

        # 실제 BUY pipeline으로 Position을 열고 SELL 전 snapshot에서 배분 원가를 고정한다.
        buy_outcomes = self._execute_actions(
            fixture.controller,
            self._entry_actions(fixture, sequence_number=1),
        )
        self.assertEqual(
            (TradingEventType.CASE_B_POSITION_OPENED,),
            tuple(event.event_type for event in buy_outcomes),
        )
        before_sell = fixture.position.get_snapshot()
        # Fake fill과 같은 Decimal128 정책으로 사전 원가 대상 수량을 산출한다.
        with localcontext() as decimal_context:
            decimal_context.prec = 34
            decimal_context.rounding = ROUND_HALF_EVEN
            partial_quantity = (
                fixture.rest_client.submitted_orders[0].submitted_quantity
                * Decimal("0.5")
            )
        expected_allocated_cost = fixture.position.get_cost_basis(
            partial_quantity
        )  # SELL Position mutation 전 authoritative cost basis를 읽는다.

        fixture.clock.advance(seconds=1)
        partial_outcomes = self._execute_actions(
            fixture.controller,
            self._exit_actions(
                fixture,
                attempt_kind=OrderAttemptKind.INITIAL,
                sequence_number=2,
            ),
        )

        # terminal partial은 SELL_FAILED 하나와 잔량 Position을 남기며 SELL_FILLED를 발행하지 않는다.
        self.assertEqual(
            (TradingEventType.CASE_B_SELL_FAILED,),
            tuple(event.event_type for event in partial_outcomes),
        )
        self.assertNotIn(
            TradingEventType.CASE_B_SELL_FILLED,
            {event.event_type for event in partial_outcomes},
        )
        after_partial = fixture.position.get_snapshot()
        self.assertIs(after_partial.status, PositionStatus.OPEN)
        self.assertGreater(after_partial.quantity, Decimal("0"))
        self.assertLess(after_partial.quantity, before_sell.quantity)
        self.assertEqual(2, len(fixture.history_controller.trade_history.trades))

        # SELL Trade의 realized 값이 사전 원가와 fee 포함 공식을 정확히 보존하는지 확인한다.
        partial_trade = fixture.history_controller.trade_history.trades[-1]
        self.assertIs(partial_trade.side, OrderSide.SELL)
        self.assertEqual(
            expected_allocated_cost,
            partial_trade.allocated_cost_basis,
        )
        with localcontext() as decimal_context:
            decimal_context.prec = 34
            decimal_context.rounding = ROUND_HALF_EVEN
            expected_realized_pnl = (
                partial_trade.executed_amount
                - partial_trade.fee_quote_amount
                - expected_allocated_cost
            )
        self.assertEqual(expected_realized_pnl, partial_trade.realized_pnl)
        self.assertEqual(
            expected_realized_pnl,
            fixture.history_controller.performance.realized_pnl,
        )
        self.assertEqual(2, len(fixture.rest_client.submitted_orders))

        # 잔량 retry는 보존된 intent의 새 client ID로 현재 Position 전량만 제출한다.
        fixture.clock.advance(seconds=1)
        retry_outcomes = self._execute_actions(
            fixture.controller,
            self._exit_actions(
                fixture,
                attempt_kind=OrderAttemptKind.RETRY,
                sequence_number=3,
            ),
        )
        self.assertEqual(
            (TradingEventType.CASE_B_SELL_FILLED,),
            tuple(event.event_type for event in retry_outcomes),
        )
        self.assertEqual(Decimal("0"), fixture.position.quantity)
        self.assertEqual(3, len(fixture.rest_client.submitted_orders))
        self.assertEqual(3, len(fixture.history_controller.trade_history.trades))

    def test_zero_fill_residual_retry_preserves_full_remaining_quantity(
        self,
    ) -> None:
        """
        함수 이름: test_zero_fill_residual_retry_preserves_full_remaining_quantity()
        기능: 분할 SELL의 residual zero-fill 뒤 다음 retry도 잔여 전량 의도를 보존함을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # 50% 분할 SELL의 partial fill 뒤 residual 전량 zero-fill과 다음 full fill을 준비한다.
        fixture = self._create_pipeline(
            submit_specifications=(
                OrderResultSpecification(
                    "501",
                    OrderStatus.FILLED,
                    MARKET_UPDATED_AT,
                    (
                        FillSpecification(
                            "501-buy",
                            None,
                            Decimal("2500.50"),
                            Decimal("0.10"),
                            MARKET_UPDATED_AT,
                        ),
                    ),
                ),
                OrderResultSpecification(
                    "502",
                    OrderStatus.CANCELED,
                    MARKET_UPDATED_AT + timedelta(seconds=1),
                    (
                        FillSpecification(
                            "502-sell-partial",
                            Decimal("0.5"),
                            Decimal("3000"),
                            Decimal("0.05"),
                            MARKET_UPDATED_AT + timedelta(seconds=1),
                        ),
                    ),
                ),
                OrderResultSpecification(
                    "503",
                    OrderStatus.CANCELED,
                    MARKET_UPDATED_AT + timedelta(seconds=2),
                ),
                OrderResultSpecification(
                    "504",
                    OrderStatus.FILLED,
                    MARKET_UPDATED_AT + timedelta(seconds=4),
                    (
                        FillSpecification(
                            "504-sell-rest",
                            None,
                            Decimal("3100"),
                            Decimal("0.05"),
                            MARKET_UPDATED_AT + timedelta(seconds=4),
                        ),
                    ),
                ),
            ),
            query_specifications=(
                OrderResultSpecification(
                    "503",
                    OrderStatus.CANCELED,
                    MARKET_UPDATED_AT + timedelta(seconds=3),
                ),
            ),
            scale_out=Decimal("0.5"),
        )

        # 실제 BUY로 Position을 연 뒤 첫 분할 SELL이 terminal partial이 되게 한다.
        self._execute_actions(
            fixture.controller,
            self._entry_actions(fixture, sequence_number=4),
        )
        fixture.clock.advance(seconds=1)
        partial_outcomes = self._execute_actions(
            fixture.controller,
            self._exit_actions(
                fixture,
                attempt_kind=OrderAttemptKind.INITIAL,
                sequence_number=5,
            ),
        )
        self.assertEqual(
            (TradingEventType.CASE_B_SELL_FAILED,),
            tuple(event.event_type for event in partial_outcomes),
        )
        remaining_after_partial = fixture.position.quantity
        self.assertGreater(remaining_after_partial, Decimal("0"))
        self.assertLess(
            fixture.rest_client.submitted_orders[1].submitted_quantity,
            fixture.rest_client.submitted_orders[0].submitted_quantity,
        )

        # 첫 residual retry는 split을 무시한 잔여 전량을 제출하고 확인 query를 예약한다.
        fixture.clock.advance(seconds=1)
        zero_fill_outcomes = self._execute_actions(
            fixture.controller,
            self._exit_actions(
                fixture,
                attempt_kind=OrderAttemptKind.RETRY,
                sequence_number=6,
            ),
        )
        zero_fill_order = fixture.rest_client.submitted_orders[2]
        self.assertEqual(remaining_after_partial, zero_fill_order.submitted_quantity)
        self.assertEqual((), zero_fill_outcomes)
        self.assertEqual((), tuple(fixture.rest_client.queried_orders))
        self.assertEqual(remaining_after_partial, fixture.position.quantity)
        self.assertEqual(2, len(fixture.history_controller.trade_history.trades))

        # 동일 주문의 terminal zero를 재확인한 뒤에만 실패 outcome과 retry를 허용한다.
        fixture.clock.advance(seconds=1)
        confirmed_zero_outcomes = (
            fixture.controller.trigger_order_reconciliation(
                occurred_at=fixture.clock(),
            )
        )
        self.assertEqual(
            (TradingEventType.CASE_B_SELL_FAILED,),
            tuple(event.event_type for event in confirmed_zero_outcomes),
        )
        self.assertEqual((zero_fill_order,), tuple(fixture.rest_client.queried_orders))
        self.assertEqual(3, len(fixture.rest_client.submitted_orders))
        self.assertEqual(remaining_after_partial, fixture.position.quantity)

        # 확인된 zero-fill이 보존한 override로 다음 retry도 잔여 전량을 청산한다.
        fixture.clock.advance(seconds=1)
        final_outcomes = self._execute_actions(
            fixture.controller,
            self._exit_actions(
                fixture,
                attempt_kind=OrderAttemptKind.RETRY,
                sequence_number=7,
            ),
        )
        final_order = fixture.rest_client.submitted_orders[3]
        self.assertEqual(remaining_after_partial, final_order.submitted_quantity)
        self.assertEqual(zero_fill_order.intent_id, final_order.intent_id)
        self.assertNotEqual(zero_fill_order.client_order_id, final_order.client_order_id)
        self.assertEqual(
            (TradingEventType.CASE_B_SELL_FILLED,),
            tuple(event.event_type for event in final_outcomes),
        )
        self.assertEqual(Decimal("0"), fixture.position.quantity)
        self.assertEqual(
            ("501", "502", "504"),
            tuple(
                trade.order_id
                for trade in fixture.history_controller.trade_history.trades
            ),
        )

    def test_fsync_failure_keeps_position_and_retries_only_pending_trade(
        self,
    ) -> None:
        """
        함수 이름: test_fsync_failure_keeps_position_and_retries_only_pending_trade()
        기능: append fsync 실패가 Position을 rollback하지 않고 save-only retry 후 outcome을 냄을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # BUY로 Position을 연 뒤 SELL JSONL append의 fsync만 결정론적으로 실패시킨다.
        fixture = self._create_pipeline(
            submit_specifications=(
                OrderResultSpecification(
                    "201",
                    OrderStatus.FILLED,
                    MARKET_UPDATED_AT,
                    (
                        FillSpecification(
                            "201-buy",
                            None,
                            Decimal("2500.50"),
                            Decimal("0.10"),
                            MARKET_UPDATED_AT,
                        ),
                    ),
                ),
                OrderResultSpecification(
                    "202",
                    OrderStatus.FILLED,
                    MARKET_UPDATED_AT + timedelta(seconds=1),
                    (
                        FillSpecification(
                            "202-sell",
                            None,
                            Decimal("3000"),
                            Decimal("0.05"),
                            MARKET_UPDATED_AT + timedelta(seconds=1),
                        ),
                    ),
                ),
            )
        )
        self._execute_actions(
            fixture.controller,
            self._entry_actions(fixture, sequence_number=10),
        )
        self.assertGreater(fixture.position.quantity, Decimal("0"))

        fixture.clock.advance(seconds=1)
        with mock_patch(
            "binance_auto_trader.adapters.persistence."
            "trade_history_repository.os.fsync",
            side_effect=OSError("controlled history fsync failure"),
        ):
            sell_outcomes = self._execute_actions(
                fixture.controller,
                self._exit_actions(
                    fixture,
                    attempt_kind=OrderAttemptKind.INITIAL,
                    sequence_number=11,
                ),
            )

        # exchange·Position 사실은 되돌리지 않지만 history/performance와 성공 event는 게시하지 않는다.
        self.assertEqual((), sell_outcomes)
        failed_position = fixture.position.get_snapshot()
        self.assertIs(failed_position.status, PositionStatus.CLOSED)
        self.assertEqual(Decimal("0"), failed_position.quantity)
        self.assertEqual(
            ("201",),
            tuple(
                trade.order_id
                for trade in fixture.history_controller.trade_history.trades
            ),
        )
        self.assertEqual(
            Decimal("0"),
            fixture.history_controller.performance.realized_pnl,
        )
        self.assertEqual(frozenset({"202"}), fixture.history_controller.dirty_order_ids)
        self.assertIs(
            fixture.controller.status,
            TradingSessionStatus.RECONCILIATION_REQUIRED,
        )
        self.assertFalse(
            any(
                trace.message_id == "14" and trace.order_id == "202"
                for trace in fixture.controller.order_execution_trace
            )
        )

        # dirty save가 남은 동안 다른 intent 제출은 Gateway 호출 전에 차단된다.
        submit_count_before_blocked_action = len(
            fixture.rest_client.submitted_orders
        )
        blocked_outcomes = fixture.controller._execute_action(
            SubmitOrder(
                strategy=StrategyType.CASE_B,
                side=OrderSide.BUY,
                attempt_kind=OrderAttemptKind.INITIAL,
                idempotency_key="blocked-after-history-failure",
            )
        )
        self.assertEqual((), blocked_outcomes)
        self.assertEqual(
            submit_count_before_blocked_action,
            len(fixture.rest_client.submitted_orders),
        )

        # retry는 Gateway를 건드리지 않고 이미 append된 202 line의 fsync만 확정한다.
        query_count_before_retry = len(fixture.rest_client.queried_orders)
        retry_outcomes = fixture.controller.retry_pending_order_persistence(
            "202"
        )
        self.assertEqual(
            (TradingEventType.CASE_B_SELL_FILLED,),
            tuple(event.event_type for event in retry_outcomes),
        )
        self.assertEqual(
            submit_count_before_blocked_action,
            len(fixture.rest_client.submitted_orders),
        )
        self.assertEqual(
            query_count_before_retry,
            len(fixture.rest_client.queried_orders),
        )
        self.assertEqual(frozenset(), fixture.history_controller.dirty_order_ids)
        self.assertEqual(
            ("201", "202"),
            tuple(
                trade.order_id
                for trade in fixture.history_controller.trade_history.trades
            ),
        )
        history_bytes = fixture.storage_path.read_bytes()
        self.assertTrue(history_bytes.endswith(b"\n"))
        self.assertEqual(2, history_bytes.count(b"\n"))

    def test_duplicate_partial_query_result_does_not_double_apply_fill(
        self,
    ) -> None:
        """
        함수 이름: test_duplicate_partial_query_result_does_not_double_apply_fill()
        기능: 동일 partial query fill을 재관찰해도 Position·Trade·fee가 중복 반영되지 않음을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        first_fill = FillSpecification(
            "301-first",
            Decimal("0.5"),
            Decimal("2500.50"),
            Decimal("0.05"),
            MARKET_UPDATED_AT + timedelta(seconds=1),
        )
        fixture = self._create_pipeline(
            submit_specifications=(
                OrderResultSpecification(
                    "301",
                    OrderStatus.NEW,
                    MARKET_UPDATED_AT,
                ),
            ),
            query_specifications=(
                OrderResultSpecification(
                    "301",
                    OrderStatus.PARTIALLY_FILLED,
                    MARKET_UPDATED_AT + timedelta(seconds=1),
                    (first_fill,),
                ),
                OrderResultSpecification(
                    "301",
                    OrderStatus.PARTIALLY_FILLED,
                    MARKET_UPDATED_AT + timedelta(seconds=3),
                    (first_fill,),
                ),
                OrderResultSpecification(
                    "301",
                    OrderStatus.FILLED,
                    MARKET_UPDATED_AT + timedelta(seconds=7),
                    (
                        first_fill,
                        FillSpecification(
                            "301-second",
                            None,
                            Decimal("2500.50"),
                            Decimal("0.05"),
                            MARKET_UPDATED_AT + timedelta(seconds=7),
                        ),
                    ),
                ),
            ),
        )

        # NEW submit은 Position과 history를 변경하지 않고 1초 same-order query만 예약한다.
        submit_outcomes = self._execute_actions(
            fixture.controller,
            self._entry_actions(fixture, sequence_number=20),
        )
        self.assertEqual((), submit_outcomes)
        self.assertEqual(Decimal("0"), fixture.position.quantity)
        self.assertEqual(1, fixture.controller.pending_order_query_count)

        # 첫 partial query의 fill delta는 Position에 한 번만 반영하고 terminal Trade는 아직 만들지 않는다.
        fixture.clock.advance(seconds=1)
        first_query_outcomes = fixture.controller.trigger_order_reconciliation(
            occurred_at=fixture.clock(),
        )
        self.assertEqual((), first_query_outcomes)
        after_first_partial = fixture.position.get_snapshot()
        self.assertGreater(after_first_partial.quantity, Decimal("0"))
        self.assertEqual((), fixture.history_controller.trade_history.trades)

        # 동일 trade key와 내용을 다시 받아도 Position snapshot은 byte 의미상 동일하다.
        fixture.clock.advance(seconds=2)
        duplicate_outcomes = fixture.controller.trigger_order_reconciliation(
            occurred_at=fixture.clock(),
        )
        self.assertEqual((), duplicate_outcomes)
        self.assertEqual(after_first_partial, fixture.position.get_snapshot())
        self.assertEqual((), fixture.history_controller.trade_history.trades)

        # terminal 재조회에서 새 fill 하나만 추가해 전체 BUY Trade를 단 한 번 기록한다.
        fixture.clock.advance(seconds=4)
        terminal_outcomes = fixture.controller.trigger_order_reconciliation(
            occurred_at=fixture.clock(),
        )
        self.assertEqual(
            (TradingEventType.CASE_B_POSITION_OPENED,),
            tuple(event.event_type for event in terminal_outcomes),
        )
        submitted_order = fixture.rest_client.submitted_orders[0]
        self.assertEqual(
            submitted_order.submitted_quantity,
            fixture.position.quantity,
        )
        self.assertEqual(2, len(submitted_order.fills))
        self.assertEqual(3, len(fixture.rest_client.queried_orders))
        self.assertTrue(
            all(
                queried_order is submitted_order
                for queried_order in fixture.rest_client.queried_orders
            )
        )
        recorded_trades = fixture.history_controller.trade_history.trades
        self.assertEqual(1, len(recorded_trades))
        self.assertEqual(
            submitted_order.submitted_quantity,
            recorded_trades[0].executed_quantity,
        )
        self.assertEqual(
            Decimal("0.10"),
            fixture.history_controller.performance.total_fee,
        )

    def test_position_failure_prevents_history_and_success_publication(
        self,
    ) -> None:
        """
        함수 이름: test_position_failure_prevents_history_and_success_publication()
        기능: Position mutation 실패 시 history와 outcome을 게시하지 않고 reconciliation으로 전환함을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        fixture = self._create_pipeline(
            submit_specifications=(
                OrderResultSpecification(
                    "401",
                    OrderStatus.FILLED,
                    MARKET_UPDATED_AT,
                    (
                        FillSpecification(
                            "401-buy",
                            None,
                            Decimal("2500.50"),
                            Decimal("0.10"),
                            MARKET_UPDATED_AT,
                        ),
                    ),
                ),
            )
        )

        # Position collaborator 장애를 주입해 message 12 이후 history 호출이 없음을 확인한다.
        with mock_patch.object(
            Position,
            "apply_execution",
            side_effect=RuntimeError("controlled Position failure"),
        ):
            outcomes = self._execute_actions(
                fixture.controller,
                self._entry_actions(fixture, sequence_number=30),
            )

        self.assertEqual((), outcomes)
        self.assertIs(fixture.position.get_snapshot().status, PositionStatus.CLOSED)
        self.assertEqual((), fixture.history_controller.trade_history.trades)
        self.assertFalse(fixture.storage_path.exists())
        self.assertIs(
            fixture.controller.status,
            TradingSessionStatus.RECONCILIATION_REQUIRED,
        )

        # Position 반영 실패는 더 세부 사실을 추측하지 않고 주문·영속성 모호성 하나로 고정한다.
        cause_snapshot = fixture.controller.reconciliation_cause_snapshot
        self.assertTrue(cause_snapshot.reconciliation_required)
        self.assertIs(
            cause_snapshot.status,
            ReconciliationCauseStatus.EXACT,
        )
        self.assertIs(
            cause_snapshot.category,
            ReconciliationCauseCategory.ORDER_OR_PERSISTENCE_AMBIGUOUS,
        )

        # trace는 Position failure code를 남기고 history 13·outcome 14 성공 단계를 남기지 않는다.
        order_traces = tuple(
            trace
            for trace in fixture.controller.order_execution_trace
            if trace.order_id == "401"
        )
        self.assertTrue(
            any(
                trace.message_id == "12"
                and trace.failure_code
                is OrderExecutionFailureCode.POSITION_UPDATE_FAILED
                for trace in order_traces
            )
        )
        self.assertFalse(
            any(
                trace.message_id in {"13", "14"}
                for trace in order_traces
            )
        )


if __name__ == "__main__":
    unittest.main()
