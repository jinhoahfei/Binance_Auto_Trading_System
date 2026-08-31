"""Phase 8 주문 trace와 pending identity의 핵심 회귀 계약을 통합 검증한다."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
import asyncio
import unittest
from unittest.mock import Mock, patch as mock_patch

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
    OrderExecutionTraceResult,
    TradingController,
    TradingSessionStatus,
)
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.regime import RegimeSTM
from binance_auto_trader.domain.trading.account import Account
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
    OrderAttemptKind,
    StrategyType,
)
from binance_auto_trader.domain.trading.transitions.helpers import (
    create_entry_order_actions,
)

from tests.integration.test_account_stream_flow import (
    FakeAccountRESTClient,
    MARKET_UPDATED_AT,
    SynchronousAccountWebSocketClient,
    _ready_market_snapshot,
)
from tests.integration.test_order_fault_matrix import MutableClock
from tests.integration.phase13_risk_fixture import create_test_risk_policy


OrderResultHandler = Callable[[Order], OrderResult]


def _build_order_result(
    order: Order,
    *,
    exchange_order_id: str,
    status: OrderStatus,
    processed_at: datetime,
    fill_fraction: Decimal | None = None,
    include_fill: bool = False,
) -> OrderResult:
    """
    함수 이름: _build_order_result()
    기능: 실제 제출 수량과 Decimal128 정밀도로 zero/full/partial fake 결과를 만든다.
    인자: order -> Controller가 생성한 실제 Order
        exchange_order_id -> fake exchange order ID
        status -> 반환할 normalized status
        processed_at -> 결과와 fill의 UTC 시각
        fill_fraction -> 제출 수량 대비 체결 비율 또는 전량이면 None
        include_fill -> 실제 Fill을 결과에 포함할지 여부
    반환값: 같은 client ID와 선택 fill을 가진 OrderResult
    작성 날짜: 2026/08/22
    """
    # Controller 수량 계산과 같은 Decimal128 context에서 정확한 fill 수량을 만든다.
    fills: tuple[Fill, ...] = ()
    if include_fill:
        with localcontext() as decimal_context:
            decimal_context.prec = 34
            decimal_context.rounding = ROUND_HALF_EVEN
            fill_quantity = (
                order.submitted_quantity
                if fill_fraction is None
                else order.submitted_quantity * fill_fraction
            )
        fills = (
            Fill(
                exchange_order_id=exchange_order_id,
                trade_id=f"{exchange_order_id}-fill",
                quantity=fill_quantity,
                price=Decimal("2500.50"),
                fee_amount=Decimal("0.10"),
                fee_asset="USDT",
                fee_quote_amount=Decimal("0.10"),
                executed_at=processed_at,
            ),
        )

    return OrderResult(
        symbol=order.symbol,
        client_order_id=order.client_order_id,
        exchange_order_id=exchange_order_id,
        status=status,
        processed_at=processed_at,
        fills=fills,
    )  # raw payload 없이 같은 Order에 상관된 normalized 결과만 반환한다.


class TraceOrderRESTClient(FakeAccountRESTClient):
    """
    클래스 이름: TraceOrderRESTClient
    기능: account bootstrap과 주입된 submit/query 결과 handler를 한 fake port로 제공한다.
    작성 날짜: 2026/08/22
    """

    def __init__(
        self,
        submit_handler: OrderResultHandler,
        query_handler: OrderResultHandler | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 주문 handler와 호출 identity 기록 목록을 초기화한다.
        인자: submit_handler -> submit Order를 결과 또는 예외로 바꿀 callable
            query_handler -> same-order query 결과 callable 또는 조회 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        super().__init__([])
        if not callable(submit_handler):
            raise TypeError("submit_handler must be callable")
        if query_handler is not None and not callable(query_handler):
            raise TypeError("query_handler must be callable or None")

        # handler identity와 실제 submit/query Order를 별도 보존해 재제출 여부를 검증한다.
        self._submit_handler = submit_handler
        self._query_handler = query_handler
        self.submitted_orders: list[Order] = []
        self.queried_orders: list[Order] = []

    def submit_order(self, *, order: Order) -> OrderResult:
        """
        함수 이름: submit_order()
        기능: 제출 Order를 기록하고 주입 handler의 결과 또는 예외를 그대로 전달한다.
        인자: order -> Controller가 신규 제출한 Order
        반환값: handler가 만든 normalized OrderResult
        작성 날짜: 2026/08/22
        """
        self.submitted_orders.append(order)  # 예외 제출도 같은 client ID 증거로 남긴다.
        return self._submit_handler(order)

    def query_order_result(self, *, order: Order) -> OrderResult:
        """
        함수 이름: query_order_result()
        기능: 기존 Order identity를 기록하고 주입된 same-order query 결과를 반환한다.
        인자: order -> 최초 submit에서 생성된 기존 Order
        반환값: query handler가 만든 normalized OrderResult
        작성 날짜: 2026/08/22
        """
        if self._query_handler is None:
            raise AssertionError("unexpected order query")

        self.queried_orders.append(order)  # object identity로 신규 제출이 아님을 증명한다.
        return self._query_handler(order)

    def cancel_order(self, *, order: Order) -> OrderResult:
        """
        함수 이름: cancel_order()
        기능: 본 회귀 시나리오에 없는 cancel 호출을 즉시 실패시킨다.
        인자: order -> 예상하지 않은 cancel 대상
        반환값: 정상 경로에서 반환하지 않음
        작성 날짜: 2026/08/22
        """
        raise AssertionError(
            f"unexpected order cancellation: {order.client_order_id}"
        )


@dataclass(frozen=True, slots=True)
class TracePipelineFixture:
    """
    클래스 이름: TracePipelineFixture
    기능: trace 회귀 테스트가 공유할 실제 Controller와 domain collaborator를 묶는다.
    작성 날짜: 2026/08/22
    """

    controller: TradingController
    rest_client: TraceOrderRESTClient
    position: Position
    history_controller: TradeHistoryController
    clock: MutableClock


class OrderTraceInvariantIntegrationTests(unittest.TestCase):
    """
    클래스 이름: OrderTraceInvariantIntegrationTests
    기능: pending identity와 Case 2 trace의 실패·partial·확정 시점 불변식을 검증한다.
    작성 날짜: 2026/08/22
    """

    def setUp(self) -> None:
        """
        함수 이름: setUp()
        기능: 각 테스트가 사용할 격리된 history 디렉토리를 생성한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self._temporary_directory = TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)

    def _create_pipeline(
        self,
        *,
        submit_handler: OrderResultHandler,
        query_handler: OrderResultHandler | None = None,
    ) -> TracePipelineFixture:
        """
        함수 이름: _create_pipeline()
        기능: ready account·market과 실제 Position/History/TradingController를 시작한다.
        인자: submit_handler -> submit 결과 또는 예외 handler
            query_handler -> same-order query 결과 handler 또는 None
        반환값: RUNNING 상태의 TracePipelineFixture
        작성 날짜: 2026/08/22
        """
        # fake REST port와 실제 domain/persistence collaborator를 같은 clock으로 조립한다.
        clock = MutableClock(MARKET_UPDATED_AT)
        rest_client = TraceOrderRESTClient(submit_handler, query_handler)
        account = Account()
        web_socket_gateway = WebSocketGateway(
            SynchronousAccountWebSocketClient([]),
            account_snapshot_callback=account.apply_stream_snapshot,
        )
        position = Position()
        history_repository = TradeHistoryRepository(
            Path(self._temporary_directory.name) / "trades.jsonl",
            clock=clock,
        )
        history_controller = TradeHistoryController(
            history_repository,
            clock=clock,
        )
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

        # Account, REGIME, split과 session을 production version 순서대로 준비한다.
        controller.load_account()
        regime_controller = RegimeController(
            RegimeSTM(),
            market_snapshot,
            controller,
        )
        selection = regime_controller.set_regime_type(
            RegimeType.TYPE_0,
            command_id="select-type0-trace-invariant",
            expected_version=0,
        )
        split_result = controller.update_split_ratios(
            command_id="split-trace-invariant",
            expected_version=selection.version,
            scale_in=Decimal("1"),
            scale_out=Decimal("1"),
        )
        controller.start_trading(
            command_id="start-trace-invariant",
            expected_version=split_result.version,
        )
        return TracePipelineFixture(
            controller=controller,
            rest_client=rest_client,
            position=position,
            history_controller=history_controller,
            clock=clock,
        )

    @staticmethod
    def _execute_entry(
        fixture: TracePipelineFixture,
        *,
        sequence_number: int,
    ) -> tuple[TradingEvent, ...]:
        """
        함수 이름: _execute_entry()
        기능: Case B entry patch와 SubmitOrder를 production action executor 순서로 실행한다.
        인자: fixture -> 실행할 trace pipeline
            sequence_number -> 결정론적 intent ID에 사용할 event sequence
        반환값: 동기 terminal 처리에서 생성된 outcome tuple
        작성 날짜: 2026/08/22
        """
        # 실제 helper가 생성한 Context patch를 submit보다 먼저 적용한다.
        event = TradingEvent(
            TradingEventType.MARKET_DATA_UPDATED,
            fixture.clock(),
            sequence_number=sequence_number,
        )
        actions = create_entry_order_actions(
            StrategyType.CASE_B,
            OrderAttemptKind.INITIAL,
            event,
            fixture.controller.context,
        )
        outcomes: list[TradingEvent] = []
        for action in actions:
            outcomes.extend(fixture.controller._execute_action(action))

        return tuple(outcomes)  # patch와 submit이 반환한 concrete outcome만 공개한다.

    def test_initial_terminal_zero_fill_waits_for_same_order_query(self) -> None:
        """
        함수 이름: test_initial_terminal_zero_fill_waits_for_same_order_query()
        기능: 최초 terminal zero-fill이 즉시 실패하지 않고 같은 Order 조회 뒤 확정되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # submit과 query 모두 같은 exchange ID의 terminal zero-fill을 반환하게 한다.
        fixture = self._create_pipeline(
            submit_handler=partial(
                _build_order_result,
                exchange_order_id="701",
                status=OrderStatus.CANCELED,
                processed_at=MARKET_UPDATED_AT,
            ),
            query_handler=partial(
                _build_order_result,
                exchange_order_id="701",
                status=OrderStatus.CANCELED,
                processed_at=MARKET_UPDATED_AT,
            ),
        )

        # 최초 응답만으로는 outcome/history를 만들지 않고 exchange ID pending을 유지한다.
        initial_outcomes = self._execute_entry(fixture, sequence_number=40)
        submitted_order = fixture.rest_client.submitted_orders[0]
        pending_order = fixture.controller.context.pending_order
        self.assertEqual((), initial_outcomes)
        self.assertEqual(1, fixture.controller.pending_order_query_count)
        self.assertEqual(0, len(fixture.rest_client.queried_orders))
        self.assertIsNotNone(pending_order)
        self.assertEqual("701", pending_order.order_id)
        self.assertEqual((), fixture.history_controller.trade_history.trades)

        # due query가 동일 object identity를 확인한 뒤에만 terminal BUY failure를 발행한다.
        fixture.clock.advance(seconds=1)
        confirmed_outcomes = fixture.controller.trigger_order_reconciliation(
            occurred_at=fixture.clock(),
        )
        self.assertEqual(
            (TradingEventType.CASE_B_BUY_FAILED,),
            tuple(event.event_type for event in confirmed_outcomes),
        )
        self.assertEqual(1, len(fixture.rest_client.submitted_orders))
        self.assertEqual(1, len(fixture.rest_client.queried_orders))
        self.assertIs(submitted_order, fixture.rest_client.queried_orders[0])
        self.assertIsNone(fixture.controller.context.pending_order)
        self.assertEqual((), fixture.history_controller.trade_history.trades)

    def test_position_failure_preserves_pending_client_and_exchange_ids(self) -> None:
        """
        함수 이름: test_position_failure_preserves_pending_client_and_exchange_ids()
        기능: Position 적용 실패 뒤 pending exchange ID와 client ID state index가 남는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # 즉시 FILLED BUY의 Position mutation만 실패시켜 exchange 사실 이후 경계를 관찰한다.
        fixture = self._create_pipeline(
            submit_handler=partial(
                _build_order_result,
                exchange_order_id="702",
                status=OrderStatus.FILLED,
                processed_at=MARKET_UPDATED_AT,
                include_fill=True,
            ),
        )
        with mock_patch.object(
            Position,
            "apply_execution",
            side_effect=RuntimeError("controlled Position failure"),
        ):
            outcomes = self._execute_entry(fixture, sequence_number=41)

        # Context는 exchange ID를, Controller index는 같은 state의 client/exchange ID를 유지한다.
        submitted_order = fixture.rest_client.submitted_orders[0]
        pending_order = fixture.controller.context.pending_order
        state_by_client_id = fixture.controller._find_state_by_order_identifier(
            submitted_order.client_order_id
        )
        state_by_exchange_id = fixture.controller._find_state_by_order_identifier(
            "702"
        )
        self.assertEqual((), outcomes)
        self.assertIsNotNone(pending_order)
        self.assertEqual("702", pending_order.order_id)
        self.assertIs(state_by_client_id, state_by_exchange_id)
        self.assertIs(state_by_client_id.order, submitted_order)
        self.assertIs(fixture.position.get_snapshot().status, PositionStatus.CLOSED)
        self.assertIs(
            fixture.controller.status,
            TradingSessionStatus.RECONCILIATION_REQUIRED,
        )

    def test_message_two_trace_is_controller_context_commit(self) -> None:
        """
        함수 이름: test_message_two_trace_is_controller_context_commit()
        기능: 메시지 2가 Controller의 Context patch 호출과 실제 version 증가를 기록하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # terminal zero-fill을 사용해 Position/history 단계와 무관한 patch trace만 관찰한다.
        fixture = self._create_pipeline(
            submit_handler=partial(
                _build_order_result,
                exchange_order_id="703",
                status=OrderStatus.CANCELED,
                processed_at=MARKET_UPDATED_AT,
            ),
        )
        self._execute_entry(fixture, sequence_number=42)

        # 메시지 2는 TradingSTM이 아닌 Controller가 TradingContext를 mutation한 성공 기록이다.
        message_two_entries = tuple(
            trace_entry
            for trace_entry in fixture.controller.order_execution_trace
            if trace_entry.message_id == "2"
        )
        self.assertEqual(1, len(message_two_entries))
        message_two = message_two_entries[0]
        self.assertEqual("TradingController", message_two.caller)
        self.assertEqual("TradingContext", message_two.receiver)
        self.assertIs(message_two.result, OrderExecutionTraceResult.SUCCESS)
        self.assertGreater(
            message_two.context_version_after,
            message_two.context_version_before,
        )

    def test_active_partial_fill_traces_summary_before_position(self) -> None:
        """
        함수 이름: test_active_partial_fill_traces_summary_before_position()
        기능: active partial fill도 메시지 10 summary와 메시지 12 Position 순서를 남기는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # 절반 체결 PARTIALLY_FILLED로 terminal history 없이 Position delta만 만든다.
        fixture = self._create_pipeline(
            submit_handler=partial(
                _build_order_result,
                exchange_order_id="704",
                status=OrderStatus.PARTIALLY_FILLED,
                processed_at=MARKET_UPDATED_AT,
                fill_fraction=Decimal("0.5"),
                include_fill=True,
            ),
        )
        outcomes = self._execute_entry(fixture, sequence_number=43)

        # active 상태에서도 10이 12보다 먼저 한 번 기록되고 Trade는 아직 생성되지 않는다.
        trace_ids = tuple(
            trace_entry.message_id
            for trace_entry in fixture.controller.order_execution_trace
            if trace_entry.order_id == "704"
        )
        self.assertEqual((), outcomes)
        self.assertIn("10", trace_ids)
        self.assertLess(trace_ids.index("10"), trace_ids.index("12"))
        self.assertGreater(fixture.position.quantity, Decimal("0"))
        self.assertEqual((), fixture.history_controller.trade_history.trades)

    def test_gateway_exception_trace_correlates_with_recovered_outcome(self) -> None:
        """
        함수 이름: test_gateway_exception_trace_correlates_with_recovered_outcome()
        기능: submit 예외 trace와 same-client query 복구 outcome이 동일 intent와 ID로 연결되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # submit은 timeout으로 실패시키고 query는 같은 client Order의 완전 체결을 반환한다.
        submit_timeout = Mock(
            side_effect=TimeoutError("controlled submit timeout")
        )
        fixture = self._create_pipeline(
            submit_handler=submit_timeout,
            query_handler=partial(
                _build_order_result,
                exchange_order_id="705",
                status=OrderStatus.FILLED,
                processed_at=MARKET_UPDATED_AT,
                include_fill=True,
            ),
        )
        initial_outcomes = self._execute_entry(fixture, sequence_number=44)

        # Gateway 실패 trace는 exchange ID가 없어도 pending client ID와 typed failure를 보존한다.
        submitted_order = fixture.rest_client.submitted_orders[0]
        pending_order = fixture.controller.context.pending_order
        failure_entries = tuple(
            trace_entry
            for trace_entry in fixture.controller.order_execution_trace
            if trace_entry.message_id in {"6", "6.1"}
        )
        self.assertEqual((), initial_outcomes)
        self.assertIsNotNone(pending_order)
        self.assertEqual(submitted_order.client_order_id, pending_order.order_id)
        self.assertEqual(2, len(failure_entries))
        self.assertTrue(
            all(
                trace_entry.result is OrderExecutionTraceResult.FAILURE
                and trace_entry.failure_code
                is OrderExecutionFailureCode.GATEWAY_REQUEST_FAILED
                for trace_entry in failure_entries
            )
        )

        # same-client query의 terminal event와 메시지 14가 실패 trace의 intent/client를 이어받는다.
        fixture.clock.advance(seconds=1)
        recovered_outcomes = fixture.controller.trigger_order_reconciliation(
            occurred_at=fixture.clock(),
        )
        self.assertEqual(1, len(recovered_outcomes))
        recovered_event = recovered_outcomes[0]

        # Queue에 들어간 concrete outcome을 다음 microstep에서 처리해야 메시지 14가 완결된다.
        processed_results = asyncio.run(fixture.controller.drain_events())
        self.assertEqual(1, len(processed_results))
        message_fourteen = next(
            trace_entry
            for trace_entry in fixture.controller.order_execution_trace
            if trace_entry.message_id == "14"
        )
        self.assertEqual("705", recovered_event.order_id)
        self.assertIn(submitted_order.client_order_id, recovered_event.event_id)
        self.assertEqual("705", message_fourteen.order_id)
        self.assertTrue(
            all(
                trace_entry.intent_id == message_fourteen.intent_id
                and trace_entry.client_order_id
                == message_fourteen.client_order_id
                for trace_entry in failure_entries
            )
        )


if __name__ == "__main__":
    unittest.main()
