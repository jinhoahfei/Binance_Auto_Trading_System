"""Phase 8 same-order 조회와 force-sell bounded retry 시나리오를 통합 검증한다."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import unittest

from binance_auto_trader.adapters.binance.api_gateway import APIGateway
from binance_auto_trader.adapters.binance.websocket_gateway import (
    WebSocketGateway,
)
from binance_auto_trader.application.trade_history_controller import (
    TradeHistoryController,
)
from binance_auto_trader.application.trading_controller import (
    TradingController,
    TradingSessionStatus,
)
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.history import Trade
from binance_auto_trader.domain.trading.account import Account
from binance_auto_trader.domain.trading.action_requests import (
    SubmitOrder,
    patch,
)
from binance_auto_trader.domain.trading.context import PositionSnapshot
from binance_auto_trader.domain.trading.events import TradingEventType
from binance_auto_trader.domain.trading.order import (
    ExecutionSummary,
    Fill,
    Order,
    OrderResult,
    OrderResultFailureKind,
    OrderStatus,
)
from binance_auto_trader.domain.trading.position import Position
from binance_auto_trader.domain.trading.states import (
    ExitReason,
    OrderAttemptKind,
    OrderSide,
    StrategyType,
    TradingPhase,
)

from tests.integration.test_account_stream_flow import (
    SynchronousAccountWebSocketClient,
    _account_rest_payload,
    _ready_market_snapshot,
)
from tests.integration.phase13_risk_fixture import create_test_risk_policy


STARTED_AT = datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc)
BUY_INTENT_ID = "case-b-buy-reconciliation-intent"


class MutableUtcClock:
    """
    클래스 이름: MutableUtcClock
    기능: Controller와 fake Gateway가 공유하는 deterministic UTC 시각을 명시적으로 이동한다.
    작성 날짜: 2026/08/22
    """

    def __init__(self, current_time: datetime = STARTED_AT) -> None:
        """
        함수 이름: __init__()
        기능: 최초 deterministic UTC 시각을 검증해 보존한다.
        인자: current_time -> 시작할 timezone-aware UTC 시각
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self.set(current_time)  # 생성과 후속 이동에 같은 UTC validation을 적용한다.

    def __call__(self) -> datetime:
        """
        함수 이름: __call__()
        기능: 현재 deterministic UTC 시각을 반환한다.
        인자: 없음
        반환값: Controller scheduler가 관측할 datetime
        작성 날짜: 2026/08/22
        """
        return self.current_time  # sleep이나 wall clock 없이 같은 값을 재사용한다.

    def set(self, current_time: datetime) -> None:
        """
        함수 이름: set()
        기능: scheduler trigger 전에 현재 시각을 timezone-aware UTC 값으로 이동한다.
        인자: current_time -> 새로 관측할 UTC 시각
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        if not isinstance(current_time, datetime):
            raise TypeError("current_time must be a datetime")
        if current_time.tzinfo is None or current_time.utcoffset() != timedelta(0):
            raise ValueError("current_time must be timezone-aware UTC")

        self.current_time = current_time.astimezone(timezone.utc)


class _OrderResponseKind(str, Enum):
    """
    클래스 이름: _OrderResponseKind
    기능: scripted fake client가 만들 normalized 주문 결과 종류를 정의한다.
    작성 날짜: 2026/08/22
    """

    UNKNOWN = "UNKNOWN"
    SUBMISSION_REJECTED = "SUBMISSION_REJECTED"
    ORDER_NOT_VISIBLE = "ORDER_NOT_VISIBLE"
    NEW = "NEW"
    FILLED = "FILLED"
    TERMINAL_ZERO_FILL = "TERMINAL_ZERO_FILL"


class ScriptedOrderRESTClient:
    """
    클래스 이름: ScriptedOrderRESTClient
    기능: account payload와 submit/query별 scripted OrderResult 및 호출 identity를 제공한다.
    작성 날짜: 2026/08/22
    """

    def __init__(
        self,
        clock: MutableUtcClock,
        *,
        submit_steps: tuple[_OrderResponseKind | BaseException, ...],
        query_steps: tuple[_OrderResponseKind | BaseException, ...] = (),
        cancel_steps: tuple[_OrderResponseKind | BaseException, ...] = (),
    ) -> None:
        """
        함수 이름: __init__()
        기능: 공유 clock, operation별 response script와 빈 호출 기록을 초기화한다.
        인자: clock -> response processed_at에 사용할 deterministic clock
            submit_steps -> submit 호출 순서대로 소비할 결과 종류 또는 예외
            query_steps -> query 호출 순서대로 소비할 결과 종류 또는 예외
            cancel_steps -> cancel 호출 순서대로 소비할 결과 종류 또는 예외
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        if not isinstance(clock, MutableUtcClock):
            raise TypeError("clock must be a MutableUtcClock")

        # Script와 호출 기록을 분리해 조회가 신규 제출을 만들었는지 독립 검증한다.
        self.clock = clock
        self.submit_steps = list(submit_steps)
        self.query_steps = list(query_steps)
        self.cancel_steps = list(cancel_steps)
        self.submitted_orders: list[Order] = []
        self.queried_orders: list[Order] = []
        self.canceled_orders: list[Order] = []
        self._exchange_order_ids: dict[str, str] = {}
        self._response_sequence = 0

    def get_account(self) -> object:
        """
        함수 이름: get_account()
        기능: started Controller fixture에 공식 형식 Spot account payload를 반환한다.
        인자: 없음
        반환값: ETH와 USDT 전체 잔액을 포함한 account payload
        작성 날짜: 2026/08/22
        """
        return _account_rest_payload()  # 기존 account integration fixture와 같은 schema를 쓴다.

    def submit_order(self, *, order: Order) -> OrderResult:
        """
        함수 이름: submit_order()
        기능: 새 제출 Order identity를 기록하고 다음 scripted response를 반환한다.
        인자: order -> Gateway가 전달한 새 Order
        반환값: 같은 client ID의 normalized OrderResult
        작성 날짜: 2026/08/22
        """
        self.submitted_orders.append(order)  # 예외가 나도 외부 제출 시도 자체는 기록한다.
        return self._consume_response(self.submit_steps, order, "submit")

    def query_order_result(self, *, order: Order) -> OrderResult:
        """
        함수 이름: query_order_result()
        기능: 기존 Order identity를 기록하고 다음 scripted 조회 결과를 반환한다.
        인자: order -> 같은-order reconciliation 대상
        반환값: 같은 client ID의 normalized OrderResult
        작성 날짜: 2026/08/22
        """
        self.queried_orders.append(order)  # submit 기록과 별도 list로 operation 분리를 증명한다.
        return self._consume_response(self.query_steps, order, "query")

    def cancel_order(self, *, order: Order) -> OrderResult:
        """
        함수 이름: cancel_order()
        기능: cancel 대상 identity를 기록하고 설정된 scripted 결과를 반환한다.
        인자: order -> 취소할 기존 Order
        반환값: 같은 client ID의 normalized cancel 결과
        작성 날짜: 2026/08/22
        """
        self.canceled_orders.append(order)  # 결함 보고에 대상 identity를 남긴다.
        return self._consume_response(self.cancel_steps, order, "cancel")

    def _consume_response(
        self,
        steps: list[_OrderResponseKind | BaseException],
        order: Order,
        operation_name: str,
    ) -> OrderResult:
        """
        함수 이름: _consume_response()
        기능: operation script의 다음 항목을 한 번 소비해 예외 또는 domain 결과로 변환한다.
        인자: steps -> 해당 operation의 남은 response script
            order -> result identity와 fill 수량을 제공할 Order
            operation_name -> script 소진 오류에 표시할 안전한 operation 이름
        반환값: 정규화된 OrderResult
        작성 날짜: 2026/08/22
        """
        if not steps:
            raise AssertionError(f"unexpected {operation_name} call after script end")
        selected_step = steps.pop(0)
        if isinstance(selected_step, BaseException):
            raise selected_step

        return self._build_order_result(order, selected_step)

    def _build_order_result(
        self,
        order: Order,
        response_kind: _OrderResponseKind,
    ) -> OrderResult:
        """
        함수 이름: _build_order_result()
        기능: response 종류를 원 Order와 상관된 UNKNOWN, NEW, FILLED 또는 terminal 결과로 만든다.
        인자: order -> client ID와 실제 제출 수량을 제공할 Order
            response_kind -> 생성할 normalized 결과 종류
        반환값: immutable OrderResult
        작성 날짜: 2026/08/22
        """
        if not isinstance(response_kind, _OrderResponseKind):
            raise TypeError("response_kind must be an _OrderResponseKind")
        self._response_sequence += 1

        # UNKNOWN은 exchange ID를 추측하지 않고 같은 client ID만 조회 근거로 남긴다.
        if response_kind is _OrderResponseKind.UNKNOWN:
            return OrderResult(
                symbol=order.symbol,
                client_order_id=order.client_order_id,
                status=OrderStatus.UNKNOWN,
                processed_at=self.clock(),
                failure_reason="SCRIPTED_UNKNOWN",
            )
        if response_kind is _OrderResponseKind.ORDER_NOT_VISIBLE:
            return OrderResult(
                symbol=order.symbol,
                client_order_id=order.client_order_id,
                status=OrderStatus.UNKNOWN,
                processed_at=self.clock(),
                failure_reason="SCRIPTED_ORDER_NOT_VISIBLE",
                failure_kind=OrderResultFailureKind.ORDER_NOT_VISIBLE,
            )
        if response_kind is _OrderResponseKind.SUBMISSION_REJECTED:
            return OrderResult(
                symbol=order.symbol,
                client_order_id=order.client_order_id,
                status=OrderStatus.REJECTED,
                processed_at=self.clock(),
                failure_reason="SCRIPTED_SUBMISSION_REJECTED",
                failure_kind=OrderResultFailureKind.SUBMISSION_REJECTED,
            )

        # 같은 client ID의 submit/query/cancel은 하나의 exchange order ID를 계속 사용한다.
        exchange_order_id = self._exchange_order_ids.setdefault(
            order.client_order_id,
            str(8_000 + self._response_sequence),
        )
        if response_kind is _OrderResponseKind.NEW:
            return OrderResult(
                symbol=order.symbol,
                client_order_id=order.client_order_id,
                exchange_order_id=exchange_order_id,
                status=OrderStatus.NEW,
                processed_at=self.clock(),
            )
        if response_kind is _OrderResponseKind.TERMINAL_ZERO_FILL:
            return OrderResult(
                symbol=order.symbol,
                client_order_id=order.client_order_id,
                exchange_order_id=exchange_order_id,
                status=OrderStatus.CANCELED,
                processed_at=self.clock(),
                failure_reason="TERMINAL_ZERO_FILL",
            )

        # FILLED recovery는 제출 수량 전체와 결정 가격을 실제 fill 근거로 사용한다.
        fill = Fill(
            exchange_order_id=exchange_order_id,
            trade_id=f"trade-{exchange_order_id}",
            quantity=order.submitted_quantity,
            price=order.market_price_at_decision,
            fee_amount=Decimal("0"),
            fee_asset="USDT",
            fee_quote_amount=Decimal("0"),
            executed_at=self.clock(),
        )
        return OrderResult(
            symbol=order.symbol,
            client_order_id=order.client_order_id,
            exchange_order_id=exchange_order_id,
            status=OrderStatus.FILLED,
            processed_at=self.clock(),
            fills=(fill,),
        )


class InMemoryTradeRepository:
    """
    클래스 이름: InMemoryTradeRepository
    기능: terminal recovery가 durable history 단계를 완료하도록 Trade를 메모리에 저장한다.
    작성 날짜: 2026/08/22
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 비어 있는 durable Trade 목록과 save 호출 기록을 초기화한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self.saved_trades: list[Trade] = []

    def get_trade_history(self) -> tuple[Trade, ...]:
        """
        함수 이름: get_trade_history()
        기능: 현재까지 저장된 canonical Trade를 immutable tuple로 반환한다.
        인자: 없음
        반환값: 저장 순서가 보존된 Trade tuple
        작성 날짜: 2026/08/22
        """
        return tuple(self.saved_trades)  # Controller startup에는 빈 tuple을 제공한다.

    def save_this_trade_by_order_id(
        self,
        order_id: int | str,
        trade: Trade,
    ) -> None:
        """
        함수 이름: save_this_trade_by_order_id()
        기능: order ID와 Trade identity가 일치하는 terminal 기록을 메모리에 append한다.
        인자: order_id -> 저장할 exchange order ID
            trade -> Controller가 완성한 canonical Trade
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        if str(order_id) != trade.order_id:
            raise ValueError("order_id must match Trade.order_id")

        self.saved_trades.append(trade)  # 성공 append가 durable save 완료를 모의한다.


def _create_started_controller(
    client: ScriptedOrderRESTClient,
    clock: MutableUtcClock,
    *,
    maximum_order_notional: Decimal | None = None,
    maximum_order_submissions_per_intent: int = 5,
    order_retry_jitter: Callable[[], Decimal] | None = None,
) -> tuple[
    TradingController,
    Position,
    TradeHistoryController,
    InMemoryTradeRepository,
]:
    """
    함수 이름: _create_started_controller()
    기능: ready account/market/stream과 Phase 8 Position/history를 가진 RUNNING Controller를 만든다.
    인자: client -> order response script와 account payload를 제공할 fake REST client
        clock -> Controller와 fake 결과가 공유할 deterministic clock
        maximum_order_notional -> BUY Order 생성 전 적용할 선택 quote 진입 상한
        maximum_order_submissions_per_intent -> 새 client ID 전 intent별 제출 상한
        order_retry_jitter -> ADR-002 delay에 적용할 결정론적 factor provider 또는 None
    반환값: Controller, Position, history Controller와 in-memory repository tuple
    작성 날짜: 2026/08/22
    """
    account = Account()
    position = Position()
    repository = InMemoryTradeRepository()
    history_controller = TradeHistoryController(repository, clock=clock)
    history_controller.load_trade_history()

    # 실제 startup 경계와 동일하게 REST account commit 뒤 account stream을 연결한다.
    account_stream_client = SynchronousAccountWebSocketClient([])
    web_socket_gateway = WebSocketGateway(
        account_stream_client,
        account_snapshot_callback=account.apply_stream_snapshot,
    )
    controller = TradingController(
        APIGateway(client),
        web_socket_gateway,
        account,
        _ready_market_snapshot(),
        command_gate=True,
        position=position,
        trade_history_controller=history_controller,
        risk_policy_state=create_test_risk_policy(),
        maximum_order_notional=maximum_order_notional,
        maximum_order_submissions_per_intent=(
            maximum_order_submissions_per_intent
        ),
        order_retry_jitter=order_retry_jitter,
        clock=clock,
    )
    controller.load_account()

    # TYPE_0 선택을 application owner API로 commit하고 같은 version에서 session을 시작한다.
    selected_stm = controller.fetch_selected_trading_logic(RegimeType.TYPE_0)
    selection = controller.commit_regime_selection(
        RegimeType.TYPE_0,
        selected_stm,
        command_id="select-type-zero",
        expected_version=0,
    )
    controller.start_trading(
        command_id="start-trading",
        expected_version=selection.version,
    )

    return controller, position, history_controller, repository


def _submit_case_b_buy(
    controller: TradingController,
    *,
    intent_id: str = BUY_INTENT_ID,
) -> tuple[object, ...]:
    """
    함수 이름: _submit_case_b_buy()
    기능: STM의 직전 예약 patch와 SubmitOrder action을 production executor 순서로 실행한다.
    인자: controller -> RUNNING Phase 8 TradingController
        intent_id -> 같은-order correlation과 retry에 사용할 원 의도 ID
    반환값: 동기 terminal이면 생성된 outcome tuple, active/UNKNOWN이면 빈 tuple
    작성 날짜: 2026/08/22
    """
    # 주문 생성 전에 전략, side, attempt와 intent를 Context 한 version에 예약한다.
    controller._execute_action(
        patch(
            pending_strategy=StrategyType.CASE_B,
            pending_order_side=OrderSide.BUY,
            pending_order_attempt_kind=OrderAttemptKind.INITIAL,
            pending_intent_id=intent_id,
            trading_phase=TradingPhase.ENTRY_ORDER_PENDING,
        )
    )
    return controller._execute_action(
        SubmitOrder(
            strategy=StrategyType.CASE_B,
            side=OrderSide.BUY,
            attempt_kind=OrderAttemptKind.INITIAL,
            idempotency_key=intent_id,
        )
    )


def _open_case_b_position(
    controller: TradingController,
    position: Position,
) -> None:
    """
    함수 이름: _open_case_b_position()
    기능: stop force-sell 시나리오 전에 실제 BUY fill과 Context owner를 일관되게 적용한다.
    인자: controller -> started TradingController
        position -> Controller가 소유한 mutable Position
    반환값: 없음
    작성 날짜: 2026/08/22
    """
    fill = Fill(
        exchange_order_id="7001",
        trade_id="position-seed-fill",
        quantity=Decimal("0.8"),
        price=Decimal("2500"),
        fee_amount=Decimal("0"),
        fee_asset="USDT",
        fee_quote_amount=Decimal("0"),
        executed_at=STARTED_AT,
    )
    summary = ExecutionSummary(
        exchange_order_id="7001",
        client_order_id="position-seed-client-id",
        symbol="ETHUSDT",
        side=OrderSide.BUY,
        strategy=StrategyType.CASE_B,
        regime_type=RegimeType.TYPE_0,
        exit_reason=None,
        requested_quantity=Decimal("0.8"),
        submitted_quantity=Decimal("0.8"),
        executed_quantity=Decimal("0.8"),
        executed_amount=Decimal("2000"),
        average_fill_price=Decimal("2500"),
        fee_amount=Decimal("0"),
        fee_asset="USDT",
        fee_quote_amount=Decimal("0"),
        executed_at=STARTED_AT,
        fills=(fill,),
    )

    # 실제 Position을 먼저 바꾼 뒤 같은 quantity/owner를 Context snapshot에 게시한다.
    position.apply_execution(summary)
    controller.update_position_snapshot(
        PositionSnapshot(
            quantity=position.quantity,
            entry_price=position.average_entry_price,
        ),
        owner=position.owner,
    )


class OrderReconciliationFlowTests(unittest.TestCase):
    """
    클래스 이름: OrderReconciliationFlowTests
    기능: timeout same-order recovery, query budget과 force-sell retry budget을 검증한다.
    작성 날짜: 2026/08/22
    """

    def test_client_order_id_is_stable_within_session_and_unique_across_sessions(
        self,
    ) -> None:
        """
        함수 이름: test_client_order_id_is_stable_within_session_and_unique_across_sessions()
        기능: 같은 session retry ID는 결정론적이고 다음 session에서는 완료 ID를 재사용하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        first_clock = MutableUtcClock()
        second_clock = MutableUtcClock()
        first_controller, _, _, _ = _create_started_controller(
            ScriptedOrderRESTClient(first_clock, submit_steps=()),
            first_clock,
        )
        second_controller, _, _, _ = _create_started_controller(
            ScriptedOrderRESTClient(second_clock, submit_steps=()),
            second_clock,
        )

        # 같은 session·intent·attempt는 조회 상관관계를 위해 정확히 같은 ID를 재생성한다.
        first_id = first_controller._create_client_order_id(
            "stable-intent",
            0,
        )
        repeated_first_id = first_controller._create_client_order_id(
            "stable-intent",
            0,
        )
        second_id = second_controller._create_client_order_id(
            "stable-intent",
            0,
        )

        self.assertEqual(first_id, repeated_first_id)
        self.assertNotEqual(first_id, second_id)
        self.assertTrue(first_id.startswith("bat-"))
        self.assertLessEqual(len(first_id), 36)

    def test_entry_notional_ceiling_clamps_buy_but_force_sell_closes_position(
        self,
    ) -> None:
        """
        함수 이름: test_entry_notional_ceiling_clamps_buy_but_force_sell_closes_position()
        기능: Testnet 진입 상한은 BUY만 제한하고 force SELL은 Position 전량을 닫는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        maximum_notional = Decimal("12.50")

        # 큰 free USDT를 쓰는 정상 전략 BUY도 decision-price 금액이 cap을 넘지 않게 만든다.
        buy_clock = MutableUtcClock()
        buy_client = ScriptedOrderRESTClient(
            buy_clock,
            submit_steps=(_OrderResponseKind.FILLED,),
        )
        buy_controller, _, _, _ = _create_started_controller(
            buy_client,
            buy_clock,
            maximum_order_notional=maximum_notional,
        )
        _submit_case_b_buy(buy_controller, intent_id="capped-testnet-buy")
        capped_buy = buy_client.submitted_orders[0]
        self.assertEqual(
            capped_buy.requested_quantity,
            capped_buy.submitted_quantity,
        )
        self.assertLessEqual(
            capped_buy.requested_quantity
            * capped_buy.market_price_at_decision,
            maximum_notional,
        )

        # 큰 기존 Position의 force SELL은 가격 상승으로 평가액이 cap을 넘어도 전량을 줄인다.
        sell_clock = MutableUtcClock()
        sell_client = ScriptedOrderRESTClient(
            sell_clock,
            submit_steps=(_OrderResponseKind.FILLED,),
        )
        sell_controller, sell_position, _, _ = _create_started_controller(
            sell_client,
            sell_clock,
            maximum_order_notional=maximum_notional,
        )
        _open_case_b_position(sell_controller, sell_position)
        position_quantity_before_stop = sell_position.quantity

        # 일반 scale-out SELL은 기존 quote cap과 Position/free ETH 상한을 함께 유지한다.
        ordinary_sell_quantity = sell_controller._calculate_order_quantity(
            OrderSide.SELL,
            Decimal("1"),
            Decimal("2500"),
            None,
            force_sell=False,
        )
        self.assertLessEqual(
            ordinary_sell_quantity * Decimal("2500"),
            maximum_notional,
        )
        self.assertLessEqual(
            ordinary_sell_quantity,
            position_quantity_before_stop,
        )

        sell_controller.stop_trading(
            command_id="capped-testnet-stop",
            expected_version=sell_controller.context.version,
        )
        liquidation_sell = sell_client.submitted_orders[0]
        self.assertIs(liquidation_sell.side, OrderSide.SELL)
        self.assertEqual(
            liquidation_sell.requested_quantity,
            position_quantity_before_stop,
        )
        self.assertGreater(
            liquidation_sell.requested_quantity
            * liquidation_sell.market_price_at_decision,
            maximum_notional,
        )
        asyncio.run(sell_controller.drain_events())
        self.assertEqual(sell_position.quantity, Decimal("0"))
        self.assertIs(
            sell_controller.status,
            TradingSessionStatus.TERMINATED,
        )

    def test_timeout_recovers_by_querying_the_same_client_order_id(self) -> None:
        """
        함수 이름: test_timeout_recovers_by_querying_the_same_client_order_id()
        기능: submit timeout 뒤 새 주문 없이 동일 client ID 조회의 FILLED 결과를 반영하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        clock = MutableUtcClock()
        client = ScriptedOrderRESTClient(
            clock,
            submit_steps=(TimeoutError("scripted submit timeout"),),
            query_steps=(_OrderResponseKind.FILLED,),
        )
        controller, position, history_controller, repository = (
            _create_started_controller(client, clock)
        )

        # Timeout은 UNKNOWN pending과 1초 same-order query 하나만 만들고 outcome을 만들지 않는다.
        outcomes = _submit_case_b_buy(controller)
        self.assertEqual(outcomes, ())
        self.assertEqual(len(client.submitted_orders), 1)
        self.assertEqual(len(client.queried_orders), 0)
        self.assertEqual(controller.pending_order_query_count, 1)
        submitted_order = client.submitted_orders[0]
        self.assertIs(submitted_order.status, OrderStatus.UNKNOWN)

        # 1초 직전에는 조회하지 않고 정확한 due 시각에 같은 Order identity만 조회한다.
        clock.set(STARTED_AT + timedelta(microseconds=999_999))
        self.assertEqual(controller.trigger_order_reconciliation(), ())
        self.assertEqual(len(client.queried_orders), 0)
        clock.set(STARTED_AT + timedelta(seconds=1))
        recovered_events = controller.trigger_order_reconciliation()

        self.assertEqual(len(client.submitted_orders), 1)
        self.assertEqual(len(client.queried_orders), 1)
        self.assertIs(client.queried_orders[0], submitted_order)
        self.assertEqual(
            client.queried_orders[0].client_order_id,
            submitted_order.client_order_id,
        )
        self.assertIs(submitted_order.status, OrderStatus.FILLED)
        self.assertEqual(controller.pending_order_query_count, 0)
        self.assertEqual(len(recovered_events), 1)

        # Position, durable history와 공개 history가 같은 recovered fill을 한 번만 반영한다.
        self.assertGreater(position.quantity, Decimal("0"))
        self.assertIs(position.owner, StrategyType.CASE_B)
        self.assertEqual(len(repository.saved_trades), 1)
        self.assertEqual(len(history_controller.trade_history.trades), 1)
        self.assertIs(controller.status, TradingSessionStatus.RUNNING)

    def test_same_order_queries_use_one_two_four_eight_seconds_then_lock(
        self,
    ) -> None:
        """
        함수 이름: test_same_order_queries_use_one_two_four_eight_seconds_then_lock()
        기능: UNKNOWN 조회가 1·2·4·8초에 실행되고 네 번 뒤 reconciliation lock에 드는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        clock = MutableUtcClock()
        client = ScriptedOrderRESTClient(
            clock,
            submit_steps=(_OrderResponseKind.UNKNOWN,),
            query_steps=(
                _OrderResponseKind.UNKNOWN,
                _OrderResponseKind.UNKNOWN,
                _OrderResponseKind.UNKNOWN,
                _OrderResponseKind.UNKNOWN,
            ),
        )
        controller, _, _, _ = _create_started_controller(client, clock)
        self.assertEqual(_submit_case_b_buy(controller), ())
        submitted_order = client.submitted_orders[0]

        # 각 직전 시각에는 no-op이고 누적 1·3·7·15초에만 query count가 하나씩 증가한다.
        absolute_due_seconds = (1, 3, 7, 15)
        for expected_query_count, due_seconds in enumerate(
            absolute_due_seconds,
            start=1,
        ):
            with self.subTest(
                expected_query_count=expected_query_count,
                due_seconds=due_seconds,
            ):
                clock.set(
                    STARTED_AT
                    + timedelta(seconds=due_seconds, microseconds=-1)
                )
                self.assertEqual(controller.trigger_order_reconciliation(), ())
                self.assertEqual(
                    len(client.queried_orders),
                    expected_query_count - 1,
                )

                clock.set(STARTED_AT + timedelta(seconds=due_seconds))
                self.assertEqual(controller.trigger_order_reconciliation(), ())
                self.assertEqual(
                    len(client.queried_orders),
                    expected_query_count,
                )

        # 네 query는 모두 최초 Order identity를 재사용하고 다섯 번째 submit/query는 만들지 않는다.
        self.assertEqual(len(client.submitted_orders), 1)
        self.assertTrue(
            all(
                queried_order is submitted_order
                for queried_order in client.queried_orders
            )
        )
        self.assertEqual(
            {order.client_order_id for order in client.queried_orders},
            {submitted_order.client_order_id},
        )
        self.assertEqual(controller.pending_order_query_count, 0)
        self.assertIs(
            controller.status,
            TradingSessionStatus.RECONCILIATION_REQUIRED,
        )
        self.assertIs(
            controller.context.runtime.trading_phase,
            TradingPhase.RECONCILIATION_REQUIRED,
        )

        clock.set(STARTED_AT + timedelta(seconds=60))
        self.assertEqual(controller.trigger_order_reconciliation(), ())
        self.assertEqual(len(client.submitted_orders), 1)
        self.assertEqual(len(client.queried_orders), 4)

    def test_rejected_submit_and_four_absent_queries_confirm_zero_fill(
        self,
    ) -> None:
        """
        함수 이름: test_rejected_submit_and_four_absent_queries_confirm_zero_fill()
        기능: 명시적 제출 거부 뒤 네 번의 NO_SUCH_ORDER만 zero-fill 실패로 확정하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        clock = MutableUtcClock()
        client = ScriptedOrderRESTClient(
            clock,
            submit_steps=(_OrderResponseKind.SUBMISSION_REJECTED,),
            query_steps=(
                _OrderResponseKind.ORDER_NOT_VISIBLE,
                _OrderResponseKind.ORDER_NOT_VISIBLE,
                _OrderResponseKind.ORDER_NOT_VISIBLE,
                _OrderResponseKind.ORDER_NOT_VISIBLE,
            ),
        )
        controller, position, history_controller, repository = (
            _create_started_controller(client, clock)
        )
        self.assertEqual(_submit_case_b_buy(controller), ())
        submitted_order = client.submitted_orders[0]

        # 누적 1·3·7초의 부재는 아직 확정하지 않고 네 번째 15초 관측만 실패 event를 만든다.
        observed_outcomes: list[object] = []
        for due_seconds in (1, 3, 7, 15):
            clock.set(STARTED_AT + timedelta(seconds=due_seconds))
            observed_outcomes.extend(
                controller.trigger_order_reconciliation()
            )

        self.assertEqual(len(client.submitted_orders), 1)
        self.assertEqual(len(client.queried_orders), 4)
        self.assertTrue(
            all(order is submitted_order for order in client.queried_orders)
        )
        self.assertEqual(len(observed_outcomes), 1)
        self.assertIs(
            observed_outcomes[0].event_type,
            TradingEventType.CASE_B_BUY_FAILED,
        )
        self.assertEqual(position.quantity, Decimal("0"))
        self.assertEqual(history_controller.trade_history.trades, ())
        self.assertEqual(repository.saved_trades, [])
        self.assertEqual(controller.pending_order_query_count, 0)
        self.assertIs(controller.status, TradingSessionStatus.RUNNING)

    def test_rejected_submit_mixed_unknown_queries_remain_locked(
        self,
    ) -> None:
        """
        함수 이름: test_rejected_submit_mixed_unknown_queries_remain_locked()
        기능: 네 조회 중 일반 UNKNOWN이 하나라도 있으면 부재를 추측 확정하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        clock = MutableUtcClock()
        client = ScriptedOrderRESTClient(
            clock,
            submit_steps=(_OrderResponseKind.SUBMISSION_REJECTED,),
            query_steps=(
                _OrderResponseKind.UNKNOWN,
                _OrderResponseKind.ORDER_NOT_VISIBLE,
                _OrderResponseKind.ORDER_NOT_VISIBLE,
                _OrderResponseKind.ORDER_NOT_VISIBLE,
            ),
        )
        controller, _, _, _ = _create_started_controller(client, clock)
        self.assertEqual(_submit_case_b_buy(controller), ())

        # 마지막 응답이 부재여도 앞선 transport UNKNOWN 때문에 zero-fill로 승격할 수 없다.
        for due_seconds in (1, 3, 7, 15):
            clock.set(STARTED_AT + timedelta(seconds=due_seconds))
            self.assertEqual(controller.trigger_order_reconciliation(), ())

        self.assertEqual(len(client.submitted_orders), 1)
        self.assertEqual(len(client.queried_orders), 4)
        self.assertEqual(controller.pending_order_query_count, 0)
        self.assertIs(
            controller.status,
            TradingSessionStatus.RECONCILIATION_REQUIRED,
        )

    def test_unknown_submit_and_four_absent_queries_never_create_new_order(
        self,
    ) -> None:
        """
        함수 이름: test_unknown_submit_and_four_absent_queries_never_create_new_order()
        기능: Matching Engine 결과가 불명인 제출은 네 번의 부재 뒤에도 재제출하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        clock = MutableUtcClock()
        client = ScriptedOrderRESTClient(
            clock,
            submit_steps=(_OrderResponseKind.UNKNOWN,),
            query_steps=(
                _OrderResponseKind.ORDER_NOT_VISIBLE,
                _OrderResponseKind.ORDER_NOT_VISIBLE,
                _OrderResponseKind.ORDER_NOT_VISIBLE,
                _OrderResponseKind.ORDER_NOT_VISIBLE,
            ),
        )
        controller, position, history_controller, repository = (
            _create_started_controller(client, clock)
        )
        self.assertEqual(_submit_case_b_buy(controller), ())
        submitted_order = client.submitted_orders[0]

        # 최초 제출이 명시적 미도달 거부가 아니므로 연속 부재도 중복 주문 허가 근거가 아니다.
        for due_seconds in (1, 3, 7, 15):
            clock.set(STARTED_AT + timedelta(seconds=due_seconds))
            self.assertEqual(controller.trigger_order_reconciliation(), ())

        self.assertEqual(client.submitted_orders, [submitted_order])
        self.assertEqual(len(client.queried_orders), 4)
        self.assertTrue(
            all(order is submitted_order for order in client.queried_orders)
        )
        self.assertEqual(position.quantity, Decimal("0"))
        self.assertEqual(history_controller.trade_history.trades, ())
        self.assertEqual(repository.saved_trades, [])
        self.assertIs(
            controller.status,
            TradingSessionStatus.RECONCILIATION_REQUIRED,
        )

    def test_same_order_query_applies_injected_minus_twenty_percent_jitter(
        self,
    ) -> None:
        """
        함수 이름: test_same_order_query_applies_injected_minus_twenty_percent_jitter()
        기능: ADR-002 하한 factor 0.8이 최초 same-order query 대기에 적용되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        clock = MutableUtcClock()
        client = ScriptedOrderRESTClient(
            clock,
            submit_steps=(_OrderResponseKind.UNKNOWN,),
            query_steps=(_OrderResponseKind.FILLED,),
        )
        controller, _, _, _ = _create_started_controller(
            client,
            clock,
            order_retry_jitter=lambda: Decimal("0.8"),
        )
        self.assertEqual(_submit_case_b_buy(controller), ())

        # 1초 기본 delay의 80% 직전에는 no-op이고 800ms에서만 query한다.
        clock.set(STARTED_AT + timedelta(microseconds=799_999))
        self.assertEqual(controller.trigger_order_reconciliation(), ())
        self.assertEqual(client.queried_orders, [])

        clock.set(STARTED_AT + timedelta(milliseconds=800))
        recovered_events = controller.trigger_order_reconciliation()
        self.assertEqual(len(recovered_events), 1)
        self.assertEqual(len(client.queried_orders), 1)

    def test_zero_position_buy_budget_exhaustion_releases_session_lock(
        self,
    ) -> None:
        """
        함수 이름: test_zero_position_buy_budget_exhaustion_releases_session_lock()
        기능: 최종 BUY 실패 후 Position이 0이면 불필요한 reconciliation lock을 남기지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        clock = MutableUtcClock()
        client = ScriptedOrderRESTClient(
            clock,
            submit_steps=(),
        )
        controller, position, _, _ = _create_started_controller(
            client,
            clock,
        )

        # 다섯 번째 terminal-zero BUY_FAILED가 이미 생성된 직후의 pending intent를 재현한다.
        controller._execute_action(
            patch(
                pending_strategy=StrategyType.CASE_B,
                pending_order_side=OrderSide.BUY,
                pending_order_attempt_kind=OrderAttemptKind.RETRY,
                pending_intent_id=BUY_INTENT_ID,
                trading_phase=TradingPhase.ENTRY_ORDER_PENDING,
            )
        )
        controller._submission_attempts_by_intent[BUY_INTENT_ID] = 5
        controller._handle_submission_budget_exhausted(BUY_INTENT_ID)

        self.assertEqual(position.quantity, Decimal("0"))
        self.assertIs(controller.status, TradingSessionStatus.RUNNING)
        self.assertIs(
            controller.context.runtime.trading_phase,
            TradingPhase.IDLE,
        )
        self.assertIsNone(
            controller.context.runtime.pending_intent_id
        )  # ADR-002는 잔량 Position이 있는 최종 실패에만 운영 lock을 유지한다.

    def test_single_submission_budget_blocks_retry_before_client_id_trace(
        self,
    ) -> None:
        """
        함수 이름: test_single_submission_budget_blocks_retry_before_client_id_trace()
        기능: Phase 13 상한 1이 소비된 intent의 새 client ID·Gateway submit을 모두 사전에 차단하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        clock = MutableUtcClock()
        client = ScriptedOrderRESTClient(clock, submit_steps=())
        controller, position, _, repository = _create_started_controller(
            client,
            clock,
            maximum_order_submissions_per_intent=1,
        )
        controller._submission_attempts_by_intent[BUY_INTENT_ID] = 1
        trace_before = controller.order_execution_trace

        # RETRY 예약 patch와 submit이 들어와도 소진 검사는 client ID trace나 외부 호출보다 먼저 끝난다.
        result = _submit_case_b_buy(controller)

        self.assertEqual(result, ())
        self.assertEqual(controller.maximum_order_submissions_per_intent, 1)
        self.assertEqual(controller.order_execution_trace, trace_before)
        self.assertEqual(client.submitted_orders, [])
        self.assertEqual(position.quantity, Decimal("0"))
        self.assertEqual(repository.saved_trades, [])
        self.assertIs(controller.status, TradingSessionStatus.RUNNING)
        self.assertIs(controller.context.runtime.trading_phase, TradingPhase.IDLE)

    def test_pending_stop_queries_cancels_and_queries_same_order_again(
        self,
    ) -> None:
        """
        함수 이름: test_pending_stop_queries_cancels_and_queries_same_order_again()
        기능: STOP pending이 query → cancel → same-order query 뒤에만 terminal을 완료하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        clock = MutableUtcClock()
        client = ScriptedOrderRESTClient(
            clock,
            submit_steps=(_OrderResponseKind.NEW,),
            query_steps=(
                _OrderResponseKind.NEW,
                _OrderResponseKind.TERMINAL_ZERO_FILL,
            ),
            cancel_steps=(_OrderResponseKind.TERMINAL_ZERO_FILL,),
        )
        controller, position, history_controller, repository = (
            _create_started_controller(client, clock)
        )
        self.assertEqual(_submit_case_b_buy(controller), ())
        submitted_order = client.submitted_orders[0]

        # G-06P는 cancel terminal 응답도 직접 확정하지 않고 같은 Order를 즉시 재조회한다.
        stopped = controller.stop_trading(
            command_id="stop-pending-order",
            expected_version=controller.context.version,
        )
        self.assertIs(stopped.status, TradingSessionStatus.STOPPING)
        self.assertEqual(len(client.submitted_orders), 1)
        self.assertEqual(len(client.canceled_orders), 1)
        self.assertEqual(len(client.queried_orders), 2)
        self.assertTrue(
            all(
                observed_order is submitted_order
                for observed_order in (
                    *client.queried_orders,
                    *client.canceled_orders,
                )
            )
        )

        # zero-fill 확인은 Position/history를 만들지 않고 force completion만 다음 microstep에 둔다.
        self.assertEqual(position.quantity, Decimal("0"))
        self.assertEqual(history_controller.trade_history.trades, ())
        self.assertEqual(repository.saved_trades, [])
        asyncio.run(controller.drain_events())
        self.assertIs(controller.status, TradingSessionStatus.TERMINATED)

    def test_pending_stop_does_not_trust_terminal_cancel_when_query_is_active(
        self,
    ) -> None:
        """
        함수 이름: test_pending_stop_does_not_trust_terminal_cancel_when_query_is_active()
        기능: terminal cancel 응답 뒤 same-order query가 active이면 fill·force 완료를 만들지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        clock = MutableUtcClock()
        client = ScriptedOrderRESTClient(
            clock,
            submit_steps=(_OrderResponseKind.NEW,),
            query_steps=(
                _OrderResponseKind.NEW,
                _OrderResponseKind.NEW,
            ),
            cancel_steps=(_OrderResponseKind.TERMINAL_ZERO_FILL,),
        )
        controller, position, history_controller, repository = (
            _create_started_controller(client, clock)
        )
        self.assertEqual(_submit_case_b_buy(controller), ())
        submitted_order = client.submitted_orders[0]

        # cancel의 CANCELED 상태보다 뒤의 authoritative NEW query를 우선해 pending을 유지한다.
        stopped = controller.stop_trading(
            command_id="stop-pending-active-after-cancel",
            expected_version=controller.context.version,
        )
        self.assertIs(
            stopped.status,
            TradingSessionStatus.RECONCILIATION_REQUIRED,
        )
        self.assertIs(submitted_order.status, OrderStatus.NEW)
        self.assertEqual(len(client.canceled_orders), 1)
        self.assertEqual(len(client.queried_orders), 2)
        self.assertEqual(controller.pending_order_query_count, 1)

        # 미확정 BUY가 뒤늦게 체결될 수 있으므로 Position/history/force outcome은 모두 비어 있다.
        self.assertEqual(position.quantity, Decimal("0"))
        self.assertEqual(history_controller.trade_history.trades, ())
        self.assertEqual(repository.saved_trades, [])
        self.assertEqual(asyncio.run(controller.drain_events()), ())

    def test_force_sell_zero_fill_retries_every_three_seconds_then_locks(
        self,
    ) -> None:
        """
        함수 이름: test_force_sell_zero_fill_retries_every_three_seconds_then_locks()
        기능: terminal zero-fill force-sell이 3초 간격, 새 client ID와 총 5회 예산을 지키는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        clock = MutableUtcClock()
        client = ScriptedOrderRESTClient(
            clock,
            submit_steps=(
                _OrderResponseKind.TERMINAL_ZERO_FILL,
                _OrderResponseKind.TERMINAL_ZERO_FILL,
                _OrderResponseKind.TERMINAL_ZERO_FILL,
                _OrderResponseKind.TERMINAL_ZERO_FILL,
                _OrderResponseKind.TERMINAL_ZERO_FILL,
            ),
            query_steps=(
                _OrderResponseKind.TERMINAL_ZERO_FILL,
                _OrderResponseKind.TERMINAL_ZERO_FILL,
                _OrderResponseKind.TERMINAL_ZERO_FILL,
                _OrderResponseKind.TERMINAL_ZERO_FILL,
                _OrderResponseKind.TERMINAL_ZERO_FILL,
            ),
        )
        controller, position, _, repository = _create_started_controller(
            client,
            clock,
        )
        _open_case_b_position(controller, position)

        # STOP G-06은 최초 force-sell을 제출하되 terminal zero 확인 query만 예약한다.
        stopped = controller.stop_trading(
            command_id="stop-open-position",
            expected_version=controller.context.version,
        )
        self.assertIs(
            stopped.status,
            TradingSessionStatus.RECONCILIATION_REQUIRED,
        )
        self.assertEqual(len(client.submitted_orders), 1)
        self.assertEqual(len(client.queried_orders), 0)
        self.assertEqual(controller.pending_order_query_count, 1)
        self.assertEqual(position.quantity, Decimal("0.8"))
        self.assertEqual(repository.saved_trades, [])

        # 각 시도는 1초 뒤 같은 ID를 확인하고 그 실패 처리 시점부터 3초를 기다린다.
        confirmation_seconds = (1, 5, 9, 13, 17)
        for attempt_number, confirmation_second in enumerate(
            confirmation_seconds,
            start=1,
        ):
            with self.subTest(
                attempt_number=attempt_number,
                confirmation_second=confirmation_second,
            ):
                clock.set(
                    STARTED_AT
                    + timedelta(
                        seconds=confirmation_second,
                        microseconds=-1,
                    )
                )
                self.assertEqual(controller.trigger_order_reconciliation(), ())
                self.assertEqual(
                    len(client.queried_orders),
                    attempt_number - 1,
                )

                # Query는 해당 attempt의 기존 Order identity를 재사용해 실패를 확정한다.
                clock.set(
                    STARTED_AT + timedelta(seconds=confirmation_second)
                )
                confirmation_events = (
                    controller.trigger_order_reconciliation()
                )
                self.assertEqual(len(confirmation_events), 1)
                self.assertEqual(len(client.queried_orders), attempt_number)
                self.assertIs(
                    client.queried_orders[-1],
                    client.submitted_orders[attempt_number - 1],
                )
                self.assertEqual(position.quantity, Decimal("0.8"))
                self.assertEqual(repository.saved_trades, [])

                # Failure event를 처리해야 확인 시각 기준 retry 또는 최종 lock이 결정된다.
                asyncio.run(controller.drain_events())
                if attempt_number == len(confirmation_seconds):
                    continue

                retry_due_second = confirmation_second + 3
                clock.set(
                    STARTED_AT
                    + timedelta(
                        seconds=retry_due_second,
                        microseconds=-1,
                    )
                )
                self.assertEqual(controller.trigger_order_reconciliation(), ())
                self.assertEqual(
                    len(client.submitted_orders),
                    attempt_number,
                )

                # 정확히 3초가 지나면 새 client ID로 제출하고 다음 1초 확인을 예약한다.
                clock.set(STARTED_AT + timedelta(seconds=retry_due_second))
                self.assertEqual(controller.trigger_order_reconciliation(), ())
                self.assertEqual(
                    len(client.submitted_orders),
                    attempt_number + 1,
                )
                self.assertEqual(len(client.queried_orders), attempt_number)
                self.assertEqual(controller.pending_order_query_count, 1)
                self.assertEqual(position.quantity, Decimal("0.8"))

        # 모든 제출은 같은 intent와 잔량을 보존하되 attempt별 client ID만 새로 만든다.
        submitted_orders = tuple(client.submitted_orders)
        self.assertEqual(len(submitted_orders), 5)
        self.assertEqual(
            {order.intent_id for order in submitted_orders},
            {submitted_orders[0].intent_id},
        )
        self.assertEqual(
            tuple(order.submission_attempt for order in submitted_orders),
            (0, 1, 2, 3, 4),
        )
        self.assertEqual(
            len({order.client_order_id for order in submitted_orders}),
            5,
        )
        self.assertTrue(
            all(
                order.requested_quantity == Decimal("0.8")
                and order.submitted_quantity == Decimal("0.8")
                for order in submitted_orders
            )
        )

        # 다섯 번째 same-ID zero 확인을 처리하면 여섯 번째 예약 없이 잠긴다.
        self.assertIs(
            controller.status,
            TradingSessionStatus.RECONCILIATION_REQUIRED,
        )
        self.assertIs(
            controller.context.runtime.trading_phase,
            TradingPhase.RECONCILIATION_REQUIRED,
        )
        clock.set(STARTED_AT + timedelta(seconds=30))
        self.assertEqual(controller.trigger_order_reconciliation(), ())
        self.assertEqual(len(client.submitted_orders), 5)
        self.assertEqual(position.quantity, Decimal("0.8"))
        self.assertEqual(repository.saved_trades, [])


if __name__ == "__main__":
    unittest.main()
