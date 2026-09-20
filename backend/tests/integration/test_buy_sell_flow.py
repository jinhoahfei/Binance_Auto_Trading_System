"""Phase 8 BUY 주문의 체결·포지션·이력·성과 저장 흐름을 통합 검증한다."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch as mock_patch

from binance_auto_trader.adapters.binance.api_gateway import APIGateway
from binance_auto_trader.adapters.binance.websocket_gateway import (
    WebSocketGateway,
)
from binance_auto_trader.adapters.persistence import (
    ManualKillControlJournalCorruptedError,
    TradeHistoryRepository,
)
from binance_auto_trader.application.regime_controller import RegimeController
from binance_auto_trader.application.trade_history_controller import (
    TradeHistoryController,
)
from binance_auto_trader.application.trading_controller import (
    ReconciliationCauseCategory,
    ReconciliationCauseStatus,
    TradingController,
    TradingSessionError,
    TradingSessionFailureCode,
    TradingSessionStatus,
)
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.regime import RegimeSTM
from binance_auto_trader.domain.trading import (
    Account,
    DailyLossScope,
    Fill,
    ManualKillBehavior,
    ManualKillControlState,
    Order,
    OrderAttemptKind,
    OrderResult,
    OrderSide,
    OrderStatus,
    Position,
    RiskBlockReason,
    RiskPolicy,
    RiskPolicyUnavailable,
    StrategyType,
    SubmitOrder,
    TradingContext,
    TradingEvent,
    TradingEventType,
    TradingPhase,
)
from binance_auto_trader.domain.trading.action_requests import patch
from binance_auto_trader.domain.trading.results import TradingSTMResult

from tests.integration.test_account_stream_flow import (
    CURRENT_ETH_PRICE,
    FakeAccountRESTClient,
    MARKET_UPDATED_AT,
    SynchronousAccountWebSocketClient,
    _ready_market_snapshot,
)
from tests.integration.phase13_risk_fixture import create_test_risk_policy


class FakeOrderScenario(str, Enum):
    """
    클래스 이름: FakeOrderScenario
    기능: 통합 시험에서 fake REST가 재생할 주문 상태 진행을 구분한다.
    작성 날짜: 2026/08/22
    """

    IMMEDIATE_FILLED = "IMMEDIATE_FILLED"
    NEW_THEN_FILLED = "NEW_THEN_FILLED"
    PARTIALS_THEN_FILLED = "PARTIALS_THEN_FILLED"


class MutableUtcClock:
    """
    클래스 이름: MutableUtcClock
    기능: 주문 조회 backoff를 실제 대기 없이 전진시키는
        결정론적 UTC clock이다.
    작성 날짜: 2026/08/22
    """

    def __init__(self, current_time: datetime) -> None:
        """
        함수 이름: __init__()
        기능: 최초 UTC 시각을 mutable clock 상태로 보존한다.
        인자: current_time -> 최초 현재 시각
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # 주문·이력 계층이 같은 aware UTC 시각을 공유하도록
        # 최초 값을 검증한다.
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            raise ValueError("current_time must be timezone-aware")
        self._current_time = current_time

    def __call__(self) -> datetime:
        """
        함수 이름: __call__()
        기능: 현재 결정론적 UTC 시각을 반환한다.
        인자: 없음
        반환값: 현재 datetime
        작성 날짜: 2026/08/22
        """
        return self._current_time  # 모든 collaborator가 같은 test 시각을 관측한다.

    def advance(self, delay: timedelta) -> datetime:
        """
        함수 이름: advance()
        기능: 양의 backoff만큼 현재 시각을 전진시키고 새 시각을 반환한다.
        인자: delay -> 전진할 non-negative 기간
        반환값: 전진한 현재 datetime
        작성 날짜: 2026/08/22
        """
        if not isinstance(delay, timedelta):
            raise TypeError("delay must be a timedelta")
        if delay < timedelta(0):
            raise ValueError("delay must not be negative")

        # 실제 sleep 없이 Controller의 due-at 비교에 사용할 시각만 전진시킨다.
        self._current_time += delay
        return self._current_time


class FakeOrderRESTClient(FakeAccountRESTClient):
    """
    클래스 이름: FakeOrderRESTClient
    기능: account bootstrap과 계획된 normalized 주문 응답을 함께 제공한다.
    작성 날짜: 2026/08/22
    """

    def __init__(
        self,
        scenario: FakeOrderScenario,
        clock: MutableUtcClock,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 주문 시나리오, 안정된 fill 시각과 REST 호출 기록을 초기화한다.
        인자: scenario -> 재생할 주문 상태 진행
            clock -> OrderResult 처리 시각 공급자
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        if not isinstance(scenario, FakeOrderScenario):
            raise TypeError("scenario must be a FakeOrderScenario")
        if not isinstance(clock, MutableUtcClock):
            raise TypeError("clock must be a MutableUtcClock")

        # 계좌 bootstrap 계약은 기존 통합 fake를 재사용하고
        # 주문 상태만 추가한다.
        super().__init__([])
        self.scenario = scenario
        self.clock = clock
        self.fill_time_origin = clock()
        self.submitted_orders: list[Order] = []
        self.queried_orders: list[Order] = []

    def submit_order(self, *, order: Order) -> OrderResult:
        """
        함수 이름: submit_order()
        기능: 신규 제출을 한 번 기록하고 시나리오의 최초
            normalized 결과를 반환한다.
        인자: order -> Controller가 생성한 주문 aggregate
        반환값: 최초 FILLED, NEW 또는 PARTIALLY_FILLED 결과
        작성 날짜: 2026/08/22
        """
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")

        # 제출 횟수 검증을 위해 실제 aggregate identity를
        # 호출 순서대로 보존한다.
        self.submitted_orders.append(order)
        if self.scenario is FakeOrderScenario.IMMEDIATE_FILLED:
            return self._build_result(order, OrderStatus.FILLED, fill_count=1)
        if self.scenario is FakeOrderScenario.NEW_THEN_FILLED:
            return self._build_result(order, OrderStatus.NEW, fill_count=0)

        return self._build_result(
            order,
            OrderStatus.PARTIALLY_FILLED,
            fill_count=1,
        )

    def query_order_result(self, *, order: Order) -> OrderResult:
        """
        함수 이름: query_order_result()
        기능: 제출된 동일 aggregate의 상태를 시나리오 순서대로 재조회한다.
        인자: order -> 이전 submit에서 받은 동일 주문 aggregate
        반환값: 누적 fill을 포함한 후속 normalized 결과
        작성 날짜: 2026/08/22
        """
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")
        if not self.submitted_orders or order is not self.submitted_orders[0]:
            raise AssertionError("query must reuse the submitted Order aggregate")

        # 조회 index는 동일 client ID에서 NEW 또는 partial 진행을 결정한다.
        self.queried_orders.append(order)
        query_index = len(self.queried_orders) - 1
        if self.scenario is FakeOrderScenario.NEW_THEN_FILLED:
            if query_index != 0:
                raise AssertionError("NEW scenario allows exactly one query")
            return self._build_result(order, OrderStatus.FILLED, fill_count=1)
        if self.scenario is FakeOrderScenario.PARTIALS_THEN_FILLED:
            if query_index == 0:
                return self._build_result(
                    order,
                    OrderStatus.PARTIALLY_FILLED,
                    fill_count=2,
                )
            if query_index == 1:
                return self._build_result(
                    order,
                    OrderStatus.FILLED,
                    fill_count=3,
                )
            raise AssertionError("partial scenario allows exactly two queries")

        raise AssertionError("immediate FILLED scenario must not query")

    def _build_result(
        self,
        order: Order,
        status: OrderStatus,
        *,
        fill_count: int,
    ) -> OrderResult:
        """
        함수 이름: _build_result()
        기능: 시나리오별 고정 exchange ID와 누적 fill로 OrderResult를 만든다.
        인자: order -> 결과가 속한 주문
            status -> 이번 관찰 상태
            fill_count -> 결과에 포함할 처음 fill 개수
        반환값: 동일 client order ID의 normalized OrderResult
        작성 날짜: 2026/08/22
        """
        exchange_order_ids = {
            FakeOrderScenario.IMMEDIATE_FILLED: "1001",
            FakeOrderScenario.NEW_THEN_FILLED: "1002",
            FakeOrderScenario.PARTIALS_THEN_FILLED: "1003",
        }
        exchange_order_id = exchange_order_ids[self.scenario]

        # 재조회 응답은 앞선 fill을 다시 포함해
        # 실제 Binance 누적 관찰을 모의한다.
        fills = self._build_fills(order, exchange_order_id)[:fill_count]
        return OrderResult(
            symbol=order.symbol,
            client_order_id=order.client_order_id,
            exchange_order_id=exchange_order_id,
            status=status,
            processed_at=self.clock(),
            fills=fills,
        )

    def _build_fills(
        self,
        order: Order,
        exchange_order_id: str,
    ) -> tuple[Fill, ...]:
        """
        함수 이름: _build_fills()
        기능: 전체 제출 수량을 한 번 또는 1/4·1/4·1/2의 안정된 fill로 나눈다.
        인자: order -> 수량과 결정 가격을 제공할 주문
            exchange_order_id -> 모든 fill이 공유할 거래소 주문 ID
        반환값: 재조회에도 동일한 immutable Fill tuple
        작성 날짜: 2026/08/22
        """
        # Production 주문 수량과 같은 Decimal128 정책으로 누적 fill을 분할한다.
        with localcontext() as decimal_context:
            decimal_context.prec = 34
            decimal_context.rounding = ROUND_HALF_EVEN
            if self.scenario is not FakeOrderScenario.PARTIALS_THEN_FILLED:
                fill_quantities = (order.submitted_quantity,)
            else:
                quarter_quantity = order.submitted_quantity / Decimal("4")
                final_quantity = order.submitted_quantity - (
                    quarter_quantity * Decimal("2")
                )
                fill_quantities = (
                    quarter_quantity,
                    quarter_quantity,
                    final_quantity,
                )

        # 동일 fill key와 체결 사실을 매 응답에서 재생해
        # aggregate dedup을 검증한다.
        return tuple(
            Fill(
                exchange_order_id=exchange_order_id,
                trade_id=f"fill-{fill_index + 1}",
                quantity=fill_quantity,
                price=order.market_price_at_decision,
                fee_amount=Decimal("0"),
                fee_asset="USDT",
                fee_quote_amount=Decimal("0"),
                executed_at=(
                    self.fill_time_origin
                    + timedelta(milliseconds=fill_index)
                ),
            )
            for fill_index, fill_quantity in enumerate(fill_quantities)
        )


@dataclass(slots=True)
class BuyFlowFixture:
    """
    클래스 이름: BuyFlowFixture
    기능: 한 BUY 통합 흐름의 실제 Controller와
        저장·도메인 collaborator를 묶는다.
    작성 날짜: 2026/08/22
    """

    controller: TradingController
    rest_client: FakeOrderRESTClient
    position: Position
    history_controller: TradeHistoryController
    repository: TradeHistoryRepository
    history_path: Path
    clock: MutableUtcClock


def _create_buy_flow_fixture(
    temporary_directory: str,
    scenario: FakeOrderScenario,
) -> BuyFlowFixture:
    """
    함수 이름: _create_buy_flow_fixture()
    기능: ready startup부터 실제 session event queue까지 Phase 8 의존성을 조립한다.
    인자: temporary_directory -> JSONL repository를 둘 임시 디렉터리
        scenario -> fake 주문 상태 진행
    반환값: 실행 준비된 BuyFlowFixture
    작성 날짜: 2026/08/22
    """
    return _create_buy_flow_fixture_with_risk_state(
        temporary_directory,
        scenario,
        create_test_risk_policy(),
    )  # 기존 fake 성공 scenario는 production 기본값과 분리된 명시적 테스트 정책만 사용한다.


def _create_buy_flow_fixture_with_risk_state(
    temporary_directory: str,
    scenario: FakeOrderScenario,
    risk_policy_state: RiskPolicy | RiskPolicyUnavailable,
) -> BuyFlowFixture:
    """
    함수 이름: _create_buy_flow_fixture_with_risk_state()
    기능: 지정한 configured 또는 unavailable 위험 정책으로 BUY 통합 fixture를 조립한다.
    인자: temporary_directory -> JSONL repository를 둘 임시 디렉터리
        scenario -> fake 주문 상태 진행
        risk_policy_state -> start 시점에 session이 고정할 위험 정책 상태
    반환값: 실행 준비된 BuyFlowFixture
    작성 날짜: 2026/08/25
    """
    if not isinstance(risk_policy_state, (RiskPolicy, RiskPolicyUnavailable)):
        raise TypeError(
            "risk_policy_state must be a RiskPolicy or RiskPolicyUnavailable"
        )

    # 계좌와 주문 양쪽 fake REST가 공유할 결정론적 clock을 먼저 준비한다.
    clock = MutableUtcClock(MARKET_UPDATED_AT)
    rest_client = FakeOrderRESTClient(scenario, clock)
    account = Account()
    market_snapshot = _ready_market_snapshot()
    api_gateway = APIGateway(rest_client, clock=clock)

    # 실제 WebSocketGateway를 계좌 snapshot callback과 연결해 start readiness를 만든다.
    web_socket_client = SynchronousAccountWebSocketClient([])
    web_socket_gateway = WebSocketGateway(
        web_socket_client,
        account_snapshot_callback=account.apply_stream_snapshot,
    )

    # 실제 JSONL repository를 빈 파일 상태에서 load하고
    # Phase 8 domain owner를 주입한다.
    history_path = Path(temporary_directory) / "trades.jsonl"
    repository = TradeHistoryRepository(history_path, clock=clock)
    history_controller = TradeHistoryController(repository, clock=clock)
    history_controller.load_trade_history()
    position = Position()
    context = TradingContext(clock=clock)
    controller = TradingController(
        api_gateway,
        web_socket_gateway,
        account,
        market_snapshot,
        command_gate=True,
        context=context,
        position=position,
        trade_history_controller=history_controller,
        risk_policy_state=risk_policy_state,
        clock=clock,
    )

    # account bootstrap과 TYPE_0 선택 후 start를 거쳐
    # 실제 serial event queue를 생성한다.
    controller.load_account()
    regime_controller = RegimeController(
        RegimeSTM(),
        market_snapshot,
        controller,
    )
    selection = regime_controller.set_regime_type(
        RegimeType.TYPE_0,
        command_id=f"select-{scenario.value.lower()}",
        expected_version=controller.context.version,
    )
    controller.start_trading(
        command_id=f"start-{scenario.value.lower()}",
        expected_version=selection.version,
    )

    return BuyFlowFixture(
        controller=controller,
        rest_client=rest_client,
        position=position,
        history_controller=history_controller,
        repository=repository,
        history_path=history_path,
        clock=clock,
    )


def _execute_case_b_buy(
    fixture: BuyFlowFixture,
    intent_id: str,
) -> tuple[TradingEvent, ...]:
    """
    함수 이름: _execute_case_b_buy()
    기능: 실제 Context 예약 patch 뒤 Case B 최초 BUY action을 Controller에 실행한다.
    인자: fixture -> 준비된 통합 fixture
        intent_id -> 주문 intent와 멱등성에 사용할 ID
    반환값: 동기 terminal이면 concrete outcome, active이면 빈 tuple
    작성 날짜: 2026/08/22
    """
    # STM이 SubmitOrder 바로 앞에 만드는 runtime 예약을
    # 실제 Context method로 적용한다.
    patch_outcomes = fixture.controller._execute_action(
        patch(
            pending_strategy=StrategyType.CASE_B,
            pending_order_side=OrderSide.BUY,
            pending_order_attempt_kind=OrderAttemptKind.INITIAL,
            pending_intent_id=intent_id,
            trading_phase=TradingPhase.ENTRY_ORDER_PENDING,
        )
    )
    if patch_outcomes:
        raise AssertionError("runtime patch must not create order outcomes")

    # 준비된 Context에서 production SubmitOrder adapter를 한 번 실행한다.
    return fixture.controller._execute_action(
        SubmitOrder(
            strategy=StrategyType.CASE_B,
            side=OrderSide.BUY,
            attempt_kind=OrderAttemptKind.INITIAL,
            idempotency_key=intent_id,
        )
    )


def _drain_controller(controller: TradingController) -> tuple[TradingSTMResult, ...]:
    """
    함수 이름: _drain_controller()
    기능: 실제 serial event queue의 현재 outcome 연쇄를 모두 처리한다.
    인자: controller -> RUNNING session의 TradingController
    반환값: order_finished를 포함해 처리된 STM 결과 tuple
    작성 날짜: 2026/08/22
    """
    # unittest의 동기 test 경계에서 production async drain을 한 번 완료한다.
    return asyncio.run(controller.drain_events())


class BuySellFlowIntegrationTests(unittest.TestCase):
    """
    클래스 이름: BuySellFlowIntegrationTests
    기능: Phase 8 BUY의 즉시·조회·누적 partial 체결 파이프라인을 검증한다.
    작성 날짜: 2026/08/22
    """

    def test_legacy_direct_order_without_evaluation_uses_four_hour_fallback(
        self,
    ) -> None:
        """
        함수 이름: test_legacy_direct_order_without_evaluation_uses_four_hour_fallback()
        기능: evaluation이 없는 legacy direct 주문만 4시간 MarketSnapshot 가격을 쓰는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with TemporaryDirectory() as temporary_directory:
            fixture = _create_buy_flow_fixture(
                temporary_directory,
                FakeOrderScenario.IMMEDIATE_FILLED,
            )

            # Legacy fixture는 public market evaluation을 claim하지 않아 Context 가격이 초기값 0이다.
            self.assertEqual(
                Decimal("0"),
                fixture.controller.context.market.realtime_price,
            )
            _execute_case_b_buy(fixture, "intent-legacy-price-fallback")

            # 이 경로에서만 ready 4시간봉 종가가 Order의 decision price를 보완한다.
            self.assertEqual(1, len(fixture.rest_client.submitted_orders))
            self.assertEqual(
                CURRENT_ETH_PRICE,
                fixture.rest_client.submitted_orders[0].market_price_at_decision,
            )

    def test_immediate_buy_filled_updates_all_phase8_outputs_and_trace(
        self,
    ) -> None:
        """
        함수 이름: test_immediate_buy_filled_updates_all_phase8_outputs_and_trace()
        기능: 즉시 FILLED BUY의 Position·history·performance·저장·outcome과
            Case 2 trace를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        with TemporaryDirectory() as temporary_directory:
            fixture = _create_buy_flow_fixture(
                temporary_directory,
                FakeOrderScenario.IMMEDIATE_FILLED,
            )

            # 동기 terminal outcome을 실제 session queue에 넣고
            # order_finished까지 처리한다.
            outcomes = _execute_case_b_buy(fixture, "intent-immediate-buy")
            self.assertEqual(1, len(outcomes))
            self.assertIs(
                outcomes[0].event_type,
                TradingEventType.CASE_B_POSITION_OPENED,
            )
            self.assertNotIn(
                "14",
                tuple(
                    trace_entry.message_id
                    for trace_entry in fixture.controller.order_execution_trace
                ),
            )  # queue 수락 전에는 order_finished 성공 trace가 존재할 수 없다.
            enqueued_outcomes = fixture.controller._enqueue_order_outcomes(
                outcomes
            )
            self.assertEqual(1, len(enqueued_outcomes))
            self.assertEqual(
                outcomes[0].event_id,
                enqueued_outcomes[0].event_id,
            )
            self.assertEqual(
                outcomes[0].order_id,
                enqueued_outcomes[0].order_id,
            )
            processed_results = _drain_controller(fixture.controller)
            self.assertGreaterEqual(len(processed_results), 1)

            # 실제 fill 수량과 quote 원가가 Position과 Context에
            # 같은 값으로 게시된다.
            self.assertEqual(1, len(fixture.rest_client.submitted_orders))
            order = fixture.rest_client.submitted_orders[0]
            expected_quantity = order.submitted_quantity
            expected_cost_basis = order.filled_amount
            self.assertEqual(expected_quantity, fixture.position.quantity)
            self.assertEqual(expected_cost_basis, fixture.position.cost_basis)
            self.assertEqual(CURRENT_ETH_PRICE, fixture.position.average_entry_price)
            self.assertIs(fixture.position.owner, StrategyType.CASE_B)
            self.assertTrue(fixture.controller.context.position.is_open)
            self.assertIsNone(fixture.controller.context.pending_order)

            # 하나의 terminal BUY만 history와 Performance에 원자적으로 반영된다.
            trades = fixture.history_controller.trade_history.trades
            self.assertEqual(1, len(trades))
            self.assertEqual(order.exchange_order_id, trades[0].order_id)
            self.assertEqual(expected_quantity, trades[0].executed_quantity)
            performance = fixture.history_controller.performance
            self.assertEqual(Decimal("0"), performance.total_fee)
            self.assertEqual(Decimal("0"), performance.total_profit)
            self.assertEqual(0, performance.completed_sell_count)

            # 같은 Trade가 실제 JSONL repository에서 다시 복원되는지 확인한다.
            persisted_trades = fixture.repository.get_trade_history()
            self.assertEqual(trades, persisted_trades)
            self.assertTrue(fixture.history_path.is_file())
            self.assertEqual(1, len(fixture.history_path.read_text().splitlines()))

            # BUY 성공 Case 2는 명세의 메시지를 생략·추가 없이
            # 정확한 순서로 남긴다.
            expected_trace = (
                "1",
                "2",
                "3",
                "4",
                "5.1",
                "5",
                "6",
                "6.1",
                "7",
                "10",
                "12",
                "13",
                "13.2",
                "13.3",
                "13.4",
                "13.5",
                "13.5.1",
                "14",
            )
            actual_trace = tuple(
                trace_entry.message_id
                for trace_entry in fixture.controller.order_execution_trace
            )
            self.assertEqual(expected_trace, actual_trace)
            trace_entries = fixture.controller.order_execution_trace
            self.assertEqual(
                trace_entries[0].context_version_before,
                trace_entries[0].context_version_after,
            )
            self.assertEqual(
                trace_entries[0].context_version_before,
                trace_entries[1].context_version_before,
            )
            self.assertGreater(
                trace_entries[1].context_version_after,
                trace_entries[1].context_version_before,
            )  # 메시지 2만 실제 주문 예약 Context patch의 version 증가를 보존한다.
            self.assertIs(
                fixture.controller.status,
                TradingSessionStatus.RUNNING,
            )

    def test_new_then_filled_queries_same_client_id_once(self) -> None:
        """
        함수 이름: test_new_then_filled_queries_same_client_id_once()
        기능: NEW 주문을 재제출하지 않고 같은 client ID로 한 번 조회해
            FILLED 처리하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        with TemporaryDirectory() as temporary_directory:
            fixture = _create_buy_flow_fixture(
                temporary_directory,
                FakeOrderScenario.NEW_THEN_FILLED,
            )

            # 최초 NEW는 outcome 없이 동일 주문 조회 한 건만 예약한다.
            initial_outcomes = _execute_case_b_buy(
                fixture,
                "intent-new-then-filled",
            )
            self.assertEqual((), initial_outcomes)
            self.assertEqual(1, fixture.controller.pending_order_query_count)
            self.assertEqual(1, len(fixture.rest_client.submitted_orders))
            self.assertEqual(0, len(fixture.rest_client.queried_orders))

            # 1초 backoff 후 due query가 terminal outcome을 실제 queue에 등록한다.
            due_at = fixture.clock.advance(timedelta(seconds=1))
            outcomes = fixture.controller.trigger_order_reconciliation(
                occurred_at=due_at,
            )
            self.assertEqual(1, len(outcomes))
            self.assertIs(
                outcomes[0].event_type,
                TradingEventType.CASE_B_POSITION_OPENED,
            )
            _drain_controller(fixture.controller)

            # submit과 query는 같은 Order identity와 결정론적 client ID를 공유한다.
            self.assertEqual(1, len(fixture.rest_client.submitted_orders))
            self.assertEqual(1, len(fixture.rest_client.queried_orders))
            submitted_order = fixture.rest_client.submitted_orders[0]
            queried_order = fixture.rest_client.queried_orders[0]
            self.assertIs(submitted_order, queried_order)
            self.assertEqual(
                submitted_order.client_order_id,
                queried_order.client_order_id,
            )
            self.assertEqual(0, fixture.controller.pending_order_query_count)
            self.assertEqual(
                submitted_order.submitted_quantity,
                fixture.position.quantity,
            )
            self.assertEqual(
                1,
                len(fixture.history_controller.trade_history.trades),
            )

            # query adapter 메시지와 reapply 경계가
            # 신규 메시지 6 없이 한 번씩 나타난다.
            trace_ids = tuple(
                trace_entry.message_id
                for trace_entry in fixture.controller.order_execution_trace
            )
            self.assertEqual(1, trace_ids.count("6"))
            self.assertEqual(1, trace_ids.count("8"))
            self.assertEqual(1, trace_ids.count("8.1"))
            self.assertEqual(1, trace_ids.count("8.2"))
            self.assertEqual(1, trace_ids.count("9"))

    def test_cumulative_partial_fills_apply_only_deltas_and_record_one_trade(
        self,
    ) -> None:
        """
        함수 이름: test_cumulative_partial_fills_apply_only_deltas_and_record_one_trade()
        기능: 여러 누적 partial 응답의 중복 fill을 제거하고
            terminal Trade 하나만 저장하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        with TemporaryDirectory() as temporary_directory:
            fixture = _create_buy_flow_fixture(
                temporary_directory,
                FakeOrderScenario.PARTIALS_THEN_FILLED,
            )

            # 최초 partial은 첫 1/4 fill만 Position에 반영하고 history는 비워 둔다.
            initial_outcomes = _execute_case_b_buy(
                fixture,
                "intent-cumulative-partials",
            )
            self.assertEqual((), initial_outcomes)
            order = fixture.rest_client.submitted_orders[0]
            # Fake fill과 같은 Decimal128 정책으로 단계별 기대 수량을 계산한다.
            with localcontext() as decimal_context:
                decimal_context.prec = 34
                decimal_context.rounding = ROUND_HALF_EVEN
                quarter_quantity = order.submitted_quantity / Decimal("4")
                half_quantity = quarter_quantity * Decimal("2")
            self.assertEqual(quarter_quantity, fixture.position.quantity)
            self.assertEqual((), fixture.history_controller.trade_history.trades)

            # 첫 query의 누적 두 fill 중 새 두 번째 1/4만 추가 적용한다.
            first_due_at = fixture.clock.advance(timedelta(seconds=1))
            first_query_outcomes = (
                fixture.controller.trigger_order_reconciliation(
                    occurred_at=first_due_at,
                )
            )
            self.assertEqual((), first_query_outcomes)
            self.assertEqual(
                half_quantity,
                fixture.position.quantity,
            )
            self.assertEqual((), fixture.history_controller.trade_history.trades)

            # 두 번째 2초 backoff query의 final half만 더해
            # 정확히 전체 수량을 만든다.
            second_due_at = fixture.clock.advance(timedelta(seconds=2))
            terminal_outcomes = fixture.controller.trigger_order_reconciliation(
                occurred_at=second_due_at,
            )
            self.assertEqual(1, len(terminal_outcomes))
            self.assertIs(
                terminal_outcomes[0].event_type,
                TradingEventType.CASE_B_POSITION_OPENED,
            )
            _drain_controller(fixture.controller)

            # 세 누적 응답의 반복 fill은 Position과 Order aggregate에서
            # 한 번씩만 남는다.
            self.assertEqual(1, len(fixture.rest_client.submitted_orders))
            self.assertEqual(2, len(fixture.rest_client.queried_orders))
            self.assertTrue(
                all(
                    queried_order is order
                    for queried_order in fixture.rest_client.queried_orders
                )
            )
            self.assertEqual(3, len(order.fills))
            self.assertEqual(order.submitted_quantity, order.filled_quantity)
            self.assertEqual(order.submitted_quantity, fixture.position.quantity)

            # terminal 누적 summary 하나만 TradeHistory, Performance와 JSONL에 게시된다.
            trades = fixture.history_controller.trade_history.trades
            self.assertEqual(1, len(trades))
            self.assertEqual(order.submitted_quantity, trades[0].executed_quantity)
            self.assertEqual(trades, fixture.repository.get_trade_history())
            self.assertEqual(
                Decimal("0"),
                fixture.history_controller.performance.total_fee,
            )
            self.assertEqual(1, len(fixture.history_path.read_text().splitlines()))

            # Position delta 메시지는 fill batch마다 세 번,
            # terminal 기록은 정확히 한 번이다.
            trace_ids = tuple(
                trace_entry.message_id
                for trace_entry in fixture.controller.order_execution_trace
            )
            self.assertEqual(3, trace_ids.count("12"))
            self.assertEqual(1, trace_ids.count("13"))
            self.assertEqual(1, trace_ids.count("13.5.1"))

    def test_unavailable_risk_policy_blocks_before_journal_and_rest(
        self,
    ) -> None:
        """
        함수 이름: test_unavailable_risk_policy_blocks_before_journal_and_rest()
        기능: 미설정 정책이 BUY를 typed 사유로 차단하고 제출 예산·REST·history를 소비하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        with TemporaryDirectory() as temporary_directory:
            fixture = _create_buy_flow_fixture_with_risk_state(
                temporary_directory,
                FakeOrderScenario.IMMEDIATE_FILLED,
                RiskPolicyUnavailable(),
            )

            # 첫 BUY는 filter 뒤 5.1 gate에서 멈추고 typed STM feedback만 생성한다.
            outcomes = _execute_case_b_buy(
                fixture,
                "intent-unavailable-policy",
            )
            self.assertEqual(1, len(outcomes))
            self.assertIs(
                outcomes[0].event_type,
                TradingEventType.BUY_RISK_BLOCKED,
            )
            self.assertEqual(
                RiskBlockReason.RISK_POLICY_UNAVAILABLE,
                outcomes[0].payload.reason,
            )

            # PREPARED journal, REST mutation, 조회와 durable Trade는 모두 위험 판정 뒤 경계다.
            self.assertEqual([], fixture.rest_client.submitted_orders)
            self.assertEqual([], fixture.rest_client.queried_orders)
            self.assertEqual((), fixture.history_controller.trade_history.trades)
            self.assertEqual(0, fixture.controller.pending_order_query_count)
            self.assertIsNotNone(fixture.controller.last_risk_decision)
            self.assertEqual(
                RiskBlockReason.RISK_POLICY_UNAVAILABLE,
                fixture.controller.last_risk_decision.block_reason,
            )
            trace_ids = tuple(
                entry.message_id
                for entry in fixture.controller.order_execution_trace
            )
            self.assertEqual(("1", "2", "3", "4", "5.1"), trace_ids)

    def test_manual_kill_blocks_buy_without_consuming_submission_attempt(
        self,
    ) -> None:
        """
        함수 이름: test_manual_kill_blocks_buy_without_consuming_submission_attempt()
        기능: manual kill 동안 같은 BUY identity를 REST 전 차단하고 해제 뒤 attempt 0으로 제출하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        with TemporaryDirectory() as temporary_directory:
            fixture = _create_buy_flow_fixture(
                temporary_directory,
                FakeOrderScenario.IMMEDIATE_FILLED,
            )
            activated = fixture.controller.set_manual_kill(
                True,
                command_id="activate-manual-kill",
                expected_version=0,
            )
            self.assertEqual(1, activated.risk_control_version)
            self.assertEqual(
                ManualKillControlState(
                    active=True,
                    version=1,
                    command_id="activate-manual-kill",
                    expected_version=0,
                    behavior=ManualKillBehavior.BLOCK_NEW_ORDERS,
                    policy_version=1,
                ),
                fixture.repository.get_manual_kill_control_state(),
            )  # HTTP 성공으로 게시할 activation은 먼저 durable journal에 존재해야 한다.

            # 동일 command는 최초 결과를 재생하고 다른 command의 stale version은 상태를 바꾸지 못한다.
            replayed_activation = fixture.controller.set_manual_kill(
                True,
                command_id="activate-manual-kill",
                expected_version=0,
            )
            self.assertIs(activated, replayed_activation)
            with self.assertRaises(TradingSessionError) as stale_context:
                fixture.controller.set_manual_kill(
                    False,
                    command_id="stale-manual-kill",
                    expected_version=0,
                )
            self.assertIs(
                stale_context.exception.code,
                TradingSessionFailureCode.STALE_RISK_CONTROL_VERSION,
            )

            # 차단 결과의 client ID는 제출되지 않은 attempt 0 identity를 보존한다.
            blocked_outcomes = _execute_case_b_buy(
                fixture,
                "intent-manual-kill",
            )
            self.assertEqual(1, len(blocked_outcomes))
            self.assertEqual(
                RiskBlockReason.MANUAL_KILL_SWITCH_ACTIVE,
                blocked_outcomes[0].payload.reason,
            )
            self.assertEqual([], fixture.rest_client.submitted_orders)

            # BLOCK_NEW_ORDERS 정책의 kill 해제 뒤 같은 intent는 새 attempt가 아니라 원 identity로 제출된다.
            deactivated = fixture.controller.set_manual_kill(
                False,
                command_id="deactivate-manual-kill",
                expected_version=activated.risk_control_version,
            )
            self.assertEqual(2, deactivated.risk_control_version)
            self.assertEqual(
                ManualKillControlState(
                    active=False,
                    version=2,
                    command_id="deactivate-manual-kill",
                    expected_version=1,
                    behavior=ManualKillBehavior.BLOCK_NEW_ORDERS,
                    policy_version=1,
                ),
                fixture.repository.get_manual_kill_control_state(),
            )  # 해제도 같은 optimistic version과 함께 fsync된 뒤에만 BUY를 허용한다.
            allowed_outcomes = _execute_case_b_buy(
                fixture,
                "intent-manual-kill",
            )
            self.assertEqual(1, len(allowed_outcomes))
            self.assertEqual(1, len(fixture.rest_client.submitted_orders))
            submitted_order = fixture.rest_client.submitted_orders[0]
            self.assertEqual(0, submitted_order.submission_attempt)
            self.assertEqual(
                blocked_outcomes[0].order_id,
                submitted_order.client_order_id,
            )

    def test_manual_kill_activation_and_release_survive_process_restart(
        self,
    ) -> None:
        """
        함수 이름: test_manual_kill_activation_and_release_survive_process_restart()
        기능: manual kill 활성·해제와 control version이 새 Controller에서도 fail-closed로 복원되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with TemporaryDirectory() as temporary_directory:
            first_fixture = _create_buy_flow_fixture(
                temporary_directory,
                FakeOrderScenario.IMMEDIATE_FILLED,
            )
            first_fixture.controller.set_manual_kill(
                True,
                command_id="activate-before-restart",
                expected_version=0,
            )

            # 같은 storage를 쓰는 fresh Controller는 operator command 없이 active/version 1부터 시작한다.
            restarted_fixture = _create_buy_flow_fixture(
                temporary_directory,
                FakeOrderScenario.IMMEDIATE_FILLED,
            )
            restarted_snapshot = restarted_fixture.controller.snapshot_session()
            self.assertTrue(restarted_snapshot.manual_kill_active)
            self.assertEqual(1, restarted_snapshot.risk_control_version)
            replayed_activation = restarted_fixture.controller.set_manual_kill(
                True,
                command_id="activate-before-restart",
                expected_version=0,
            )
            self.assertTrue(replayed_activation.active)
            self.assertEqual(1, replayed_activation.risk_control_version)
            self.assertEqual(
                1,
                len(
                    restarted_fixture.repository.manual_kill_control_storage_path
                    .read_text(encoding="utf-8")
                    .splitlines()
                ),
            )  # Response-loss retry는 새 toggle이나 journal line을 만들지 않는다.
            blocked_outcomes = _execute_case_b_buy(
                restarted_fixture,
                "intent-restarted-manual-kill",
            )
            self.assertEqual(
                RiskBlockReason.MANUAL_KILL_SWITCH_ACTIVE,
                blocked_outcomes[0].payload.reason,
            )
            self.assertEqual([], restarted_fixture.rest_client.submitted_orders)

            # Version 1을 관측한 명시적 해제만 version 2를 fsync하고 다음 process에도 유지된다.
            restarted_fixture.controller.set_manual_kill(
                False,
                command_id="release-after-restart",
                expected_version=1,
            )
            released_fixture = _create_buy_flow_fixture(
                temporary_directory,
                FakeOrderScenario.IMMEDIATE_FILLED,
            )
            released_snapshot = released_fixture.controller.snapshot_session()
            self.assertFalse(released_snapshot.manual_kill_active)
            self.assertEqual(2, released_snapshot.risk_control_version)

    def test_manual_kill_restart_preserves_noop_and_previous_command_ids(
        self,
    ) -> None:
        """
        함수 이름: test_manual_kill_restart_preserves_noop_and_previous_command_ids()
        기능: restart가 최근 toggle/no-op receipt를 모두 복원해 기존 ID의 다른 payload를 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with TemporaryDirectory() as temporary_directory:
            fixture = _create_buy_flow_fixture(
                temporary_directory,
                FakeOrderScenario.IMMEDIATE_FILLED,
            )
            activation = fixture.controller.set_manual_kill(
                True,
                command_id="activation-a",
                expected_version=0,
            )
            confirmed_active = fixture.controller.set_manual_kill(
                True,
                command_id="noop-x",
                expected_version=1,
            )
            fixture.controller.set_manual_kill(
                False,
                command_id="release-b",
                expected_version=1,
            )
            fixture.controller.set_manual_kill(
                True,
                command_id="activation-c",
                expected_version=2,
            )
            journal_path = fixture.repository.manual_kill_control_storage_path
            journal_before_restart_replays = journal_path.read_bytes()

            # Fresh Controller는 마지막 상태뿐 아니라 이전 no-op/toggle command receipt도 복원한다.
            restarted_fixture = _create_buy_flow_fixture(
                temporary_directory,
                FakeOrderScenario.IMMEDIATE_FILLED,
            )
            replayed_activation = restarted_fixture.controller.set_manual_kill(
                True,
                command_id="activation-a",
                expected_version=0,
            )
            replayed_noop = restarted_fixture.controller.set_manual_kill(
                True,
                command_id="noop-x",
                expected_version=1,
            )
            self.assertEqual(activation, replayed_activation)
            self.assertEqual(confirmed_active, replayed_noop)

            # 과거 ID를 현재 version의 해제 payload로 바꿔도 active kill과 journal은 그대로 유지된다.
            for reused_command_id in ("activation-a", "noop-x"):
                with self.subTest(reused_command_id=reused_command_id):
                    with self.assertRaises(TradingSessionError) as context:
                        restarted_fixture.controller.set_manual_kill(
                            False,
                            command_id=reused_command_id,
                            expected_version=3,
                        )
                    self.assertIs(
                        TradingSessionFailureCode.COMMAND_ID_REUSED,
                        context.exception.code,
                    )
            restarted_snapshot = restarted_fixture.controller.snapshot_session()
            self.assertTrue(restarted_snapshot.manual_kill_active)
            self.assertEqual(3, restarted_snapshot.risk_control_version)
            self.assertEqual(
                journal_before_restart_replays,
                journal_path.read_bytes(),
            )
            self.assertEqual([], restarted_fixture.rest_client.submitted_orders)

    def test_general_commands_cannot_evict_restored_manual_kill_receipts(
        self,
    ) -> None:
        """
        함수 이름: test_general_commands_cannot_evict_restored_manual_kill_receipts()
        기능: restart 뒤 selection/start cache 압력이 durable manual-kill ID 충돌을 지우지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            bootstrap_repository = TradeHistoryRepository(history_path)
            for command_index in range(2):
                bootstrap_repository.save_manual_kill_control_state(
                    ManualKillControlState(
                        active=False,
                        version=0,
                        command_id=f"durable-noop-{command_index}",
                        expected_version=0,
                        behavior=ManualKillBehavior.BLOCK_NEW_ORDERS,
                        policy_version=1,
                    )
                )
            journal_path = (
                bootstrap_repository.manual_kill_control_storage_path
            )
            journal_before_restart = journal_path.read_bytes()

            # 두 칸짜리 일반 cache를 selection/start가 채워도 manual receipt는 전용 cache에 남아야 한다.
            with mock_patch(
                "binance_auto_trader.application.trading_controller."
                "_MAX_COMMAND_RECORDS",
                2,
            ):
                restarted_fixture = _create_buy_flow_fixture(
                    temporary_directory,
                    FakeOrderScenario.IMMEDIATE_FILLED,
                )
                with self.assertRaises(TradingSessionError) as context:
                    restarted_fixture.controller.set_manual_kill(
                        True,
                        command_id="durable-noop-0",
                        expected_version=0,
                    )

            # Deterministic payload 충돌은 저장 불명확성이나 process-wide command lock으로 오분류하지 않는다.
            self.assertIs(
                TradingSessionFailureCode.COMMAND_ID_REUSED,
                context.exception.code,
            )
            restarted_snapshot = restarted_fixture.controller.snapshot_session()
            self.assertFalse(restarted_snapshot.manual_kill_active)
            self.assertEqual(0, restarted_snapshot.risk_control_version)
            self.assertFalse(restarted_snapshot.process_ownership_ambiguous)
            self.assertTrue(restarted_snapshot.command_enabled)
            self.assertEqual(journal_before_restart, journal_path.read_bytes())
            self.assertEqual([], restarted_fixture.rest_client.submitted_orders)

    def test_manual_kill_persistence_failure_keeps_current_process_closed(
        self,
    ) -> None:
        """
        함수 이름: test_manual_kill_persistence_failure_keeps_current_process_closed()
        기능: activation fsync 실패가 volatile inactive 상태나 신규 BUY 허용으로 완화되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with TemporaryDirectory() as temporary_directory:
            fixture = _create_buy_flow_fixture(
                temporary_directory,
                FakeOrderScenario.IMMEDIATE_FILLED,
            )

            # Durable owner 실패를 주입해 command 오류 뒤에도 kill과 reconciliation blocker를 확인한다.
            with mock_patch.object(
                TradeHistoryController,
                "save_manual_kill_control_state",
                side_effect=OSError("injected-control-fsync-failure"),
            ):
                with self.assertRaises(OSError):
                    fixture.controller.set_manual_kill(
                        True,
                        command_id="failed-manual-kill-persistence",
                        expected_version=0,
                    )
            snapshot = fixture.controller.snapshot_session()
            self.assertTrue(snapshot.manual_kill_active)
            self.assertTrue(snapshot.process_ownership_ambiguous)
            self.assertFalse(snapshot.command_enabled)
            self.assertEqual([], fixture.rest_client.submitted_orders)

            # Control fsync의 반환 전후가 불명하면 process ownership 원인을 같은 lock에서 고정한다.
            cause_snapshot = fixture.controller.reconciliation_cause_snapshot
            self.assertTrue(cause_snapshot.reconciliation_required)
            self.assertIs(
                cause_snapshot.status,
                ReconciliationCauseStatus.EXACT,
            )
            self.assertIs(
                cause_snapshot.category,
                ReconciliationCauseCategory.PROCESS_OWNERSHIP_AMBIGUOUS,
            )

            # 같은 process는 journal이 기록됐는지 추측해 해제하거나 version을 재사용하지 못한다.
            with self.assertRaises(RuntimeError):
                fixture.controller.set_manual_kill(
                    False,
                    command_id="release-after-ambiguous-persistence",
                    expected_version=0,
                )

    def test_runtime_journal_truncation_cannot_release_manual_kill(
        self,
    ) -> None:
        """
        함수 이름: test_runtime_journal_truncation_cannot_release_manual_kill()
        기능: active journal의 실행 중 truncate가 release나 BUY gate 재개로 완화되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with TemporaryDirectory() as temporary_directory:
            fixture = _create_buy_flow_fixture(
                temporary_directory,
                FakeOrderScenario.IMMEDIATE_FILLED,
            )
            activation = fixture.controller.set_manual_kill(
                True,
                command_id="activation-before-runtime-truncate",
                expected_version=0,
            )
            journal_path = fixture.repository.manual_kill_control_storage_path
            journal_path.write_bytes(b"")

            # Cached active 상태보다 disk가 후퇴하면 release append 전에 typed corruption으로 닫는다.
            with self.assertRaises(
                ManualKillControlJournalCorruptedError
            ):
                fixture.controller.set_manual_kill(
                    False,
                    command_id="release-after-runtime-truncate",
                    expected_version=activation.risk_control_version,
                )

            # 현재 process는 active/version을 유지하고 ownership ambiguity로 모든 command를 차단한다.
            snapshot = fixture.controller.snapshot_session()
            self.assertTrue(snapshot.manual_kill_active)
            self.assertEqual(1, snapshot.risk_control_version)
            self.assertTrue(snapshot.process_ownership_ambiguous)
            self.assertFalse(snapshot.command_enabled)
            self.assertEqual(b"", journal_path.read_bytes())
            self.assertEqual([], fixture.rest_client.submitted_orders)

    def test_pending_unknown_buy_reserves_cumulative_position_budget(
        self,
    ) -> None:
        """
        함수 이름: test_pending_unknown_buy_reserves_cumulative_position_budget()
        기능: terminal 전 BUY 미체결 금액이 다음 BUY의 누적 position 예산을 점유하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        policy = RiskPolicy(
            version=1,
            max_order_notional=Decimal("60"),
            max_position_notional=Decimal("75"),
            max_daily_loss=Decimal("100"),
            daily_loss_scope=DailyLossScope.REALIZED_AND_UNREALIZED,
            manual_kill_behavior=ManualKillBehavior.BLOCK_NEW_ORDERS,
        )
        with TemporaryDirectory() as temporary_directory:
            fixture = _create_buy_flow_fixture_with_risk_state(
                temporary_directory,
                FakeOrderScenario.NEW_THEN_FILLED,
                policy,
            )

            # 첫 BUY는 NEW 상태로 남아 같은 client identity의 미체결 금액 전체를 예약한다.
            first_outcomes = _execute_case_b_buy(
                fixture,
                "intent-reserved-first",
            )
            self.assertEqual((), first_outcomes)
            self.assertEqual(1, len(fixture.rest_client.submitted_orders))

            # 첫 예약을 뺀 남은 25만 사용하고, 최종 누적 금액은 75를 넘지 않는다.
            second_outcomes = _execute_case_b_buy(
                fixture,
                "intent-reserved-second",
            )
            self.assertEqual((), second_outcomes)
            self.assertEqual(2, len(fixture.rest_client.submitted_orders))
            decision = fixture.controller.last_risk_decision
            self.assertIsNotNone(decision)
            self.assertGreater(
                decision.budget.reserved_buy_notional,
                Decimal("0"),
            )
            self.assertEqual(
                decision.budget.projected_position_notional,
                policy.max_position_notional,
            )
            self.assertTrue(decision.allowed)
            self.assertEqual(Decimal("25"), decision.budget.candidate_order_notional)


if __name__ == "__main__":
    unittest.main()
