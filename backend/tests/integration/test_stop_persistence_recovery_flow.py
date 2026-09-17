"""STOP pending 주문의 저장 복구와 잔여 force-sell 연결을 통합 검증한다."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch as mock_patch

from binance_auto_trader.adapters.binance.api_gateway import APIGateway
from binance_auto_trader.adapters.binance.websocket_gateway import WebSocketGateway
from binance_auto_trader.adapters.persistence.trade_history_repository import (
    TradeHistoryRepository,
)
from binance_auto_trader.application.regime_controller import RegimeController
from binance_auto_trader.application.trade_history_controller import (
    TradeHistoryController,
)
from binance_auto_trader.application.trading_controller import (
    OrderExecutionFailureCode,
    TradingController,
    TradingSessionError,
    TradingSessionFailureCode,
    TradingSessionStatus,
)
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.regime import RegimeSTM
from binance_auto_trader.domain.trading.account import Account
from binance_auto_trader.domain.trading.action_requests import TradingActionRequest
from binance_auto_trader.domain.trading.events import TradingEvent, TradingEventType
from binance_auto_trader.domain.trading.order import Fill, Order, OrderResult, OrderStatus
from binance_auto_trader.domain.trading.position import Position
from binance_auto_trader.domain.trading.risk import (
    DailyLossScope,
    ManualKillBehavior,
    ManualKillControlState,
    RiskPolicy,
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
class MutableUtcClock:
    """
    클래스 이름: MutableUtcClock
    기능: 주문 응답과 STOP 저장 복구가 공유할 결정론적 UTC 시각을 제공한다.
    작성 날짜: 2026/08/22
    """

    current_time: datetime

    def __call__(self) -> datetime:
        """
        함수 이름: __call__()
        기능: 현재 timezone-aware UTC 시각을 반환한다.
        인자: 없음
        반환값: 현재 UTC datetime
        작성 날짜: 2026/08/22
        """
        return self.current_time  # 모든 collaborator가 같은 테스트 시각을 관찰한다.

    def advance(self, seconds: int) -> None:
        """
        함수 이름: advance()
        기능: 다음 주문 단계가 이전 단계보다 늦은 시각을 갖도록 clock을 전진시킨다.
        인자: seconds -> 전진할 양의 초
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        if isinstance(seconds, bool) or not isinstance(seconds, int):
            raise TypeError("seconds must be an integer")
        if seconds <= 0:
            raise ValueError("seconds must be positive")

        self.current_time += timedelta(seconds=seconds)  # wall clock sleep 없이 순서만 전진시킨다.


class StopPersistenceRESTClient(FakeAccountRESTClient):
    """
    클래스 이름: StopPersistenceRESTClient
    기능: BUY, pending SELL, cancel reconciliation과 잔여 force-sell 응답을 순서대로 제공한다.
    작성 날짜: 2026/08/22
    """

    def __init__(
        self,
        clock: MutableUtcClock,
        *,
        pre_query_terminal: bool = False,
        terminal_query_index: int = 2,
    ) -> None:
        """
        함수 이름: __init__()
        기능: account fake와 STOP 조회 모드 및 빈 주문별 호출 기록을 초기화한다.
        인자: clock -> 응답 시각을 제공할 mutable UTC clock
            pre_query_terminal -> 첫 STOP query가 terminal partial을 반환할지 여부
            terminal_query_index -> 일반 모드에서 terminal partial을 반환할 query 순번
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        if not isinstance(clock, MutableUtcClock):
            raise TypeError("clock must be a MutableUtcClock")
        if type(pre_query_terminal) is not bool:
            raise TypeError("pre_query_terminal must be a bool")
        if type(terminal_query_index) is not int:
            raise TypeError("terminal_query_index must be an integer")
        if terminal_query_index < 2:
            raise ValueError("terminal_query_index must be at least two")

        super().__init__([])
        self.clock = clock
        self.pre_query_terminal = pre_query_terminal
        self.terminal_query_index = terminal_query_index
        self.operation_trace: list[str] = []
        self.submitted_orders: list[Order] = []
        self.queried_orders: list[Order] = []
        self.canceled_orders: list[Order] = []
        self.open_order_results: tuple[OrderResult, ...] = ()

    def list_open_order_results(
        self,
        *,
        symbol: str,
    ) -> tuple[OrderResult, ...]:
        """
        함수 이름: list_open_order_results()
        기능: manual-kill fresh verification에 주입한 현재 app-owned open order를 반환한다.
        인자: symbol -> 조회 대상 canonical Binance symbol
        반환값: 테스트가 주입한 현재 open OrderResult tuple
        작성 날짜: 2026/08/29
        """
        if symbol != "ETHUSDT":
            raise ValueError("symbol must be ETHUSDT")

        return self.open_order_results  # Cleanup 완료 뒤 생긴 외부 order도 local journal과 독립해 표현한다.

    def submit_order(self, *, order: Order) -> OrderResult:
        """
        함수 이름: submit_order()
        기능: BUY fill, active SELL 또는 잔여 force-sell fill을 제출 순서대로 반환한다.
        인자: order -> Controller가 만든 실제 Order aggregate
        반환값: 같은 client ID의 normalized OrderResult
        작성 날짜: 2026/08/22
        """
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")

        self.submitted_orders.append(order)
        self.operation_trace.append(f"submit:{order.side.value}")
        submission_index = len(self.submitted_orders)
        if submission_index == 1:
            return self._filled_result(order, "901", "buy-fill")
        if submission_index == 2:
            return OrderResult(
                symbol=order.symbol,
                client_order_id=order.client_order_id,
                exchange_order_id="902",
                status=OrderStatus.NEW,
                processed_at=self.clock(),
            )
        if submission_index == 3:
            return self._filled_result(order, "903", "force-sell-fill")

        raise AssertionError("STOP persistence scenario allows exactly three submissions")

    def query_order_result(self, *, order: Order) -> OrderResult:
        """
        함수 이름: query_order_result()
        기능: STOP 모드에 따라 첫 terminal 또는 취소 전·후 조회 결과를 반환한다.
        인자: order -> 최초 SELL 제출에서 보존된 동일 Order aggregate
        반환값: 첫 CANCELED partial 또는 NEW 후 CANCELED partial 결과
        작성 날짜: 2026/08/22
        """
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")

        self.queried_orders.append(order)
        self.operation_trace.append("query:SELL")
        if self.pre_query_terminal:
            if len(self.queried_orders) != 1:
                raise AssertionError("pre-query terminal scenario queries once")
            return self._terminal_partial_result(order)

        if len(self.queried_orders) < self.terminal_query_index:
            return OrderResult(
                symbol=order.symbol,
                client_order_id=order.client_order_id,
                exchange_order_id="902",
                status=OrderStatus.NEW,
                processed_at=self.clock(),
            )
        if len(self.queried_orders) == self.terminal_query_index:
            return self._terminal_partial_result(order)

        raise AssertionError("STOP persistence scenario exceeded its query fixture")

    def cancel_order(self, *, order: Order) -> OrderResult:
        """
        함수 이름: cancel_order()
        기능: active pending SELL의 취소 응답에 terminal partial fill을 포함한다.
        인자: order -> query로 active임을 확인한 동일 SELL Order
        반환값: 후속 query로 다시 확인해야 하는 CANCELED partial 결과
        작성 날짜: 2026/08/22
        """
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")
        if self.pre_query_terminal:
            raise AssertionError("terminal pre-query must bypass cancel")

        self.canceled_orders.append(order)
        self.operation_trace.append("cancel:SELL")
        allowed_cancel_count = (
            1 if self.terminal_query_index == 2 else 2
        )
        if len(self.canceled_orders) > allowed_cancel_count:
            raise AssertionError("STOP persistence scenario exceeded its cancel fixture")

        return self._terminal_partial_result(order)  # cancel 응답만으로 완료하지 않는 경계를 자극한다.

    def _filled_result(
        self,
        order: Order,
        exchange_order_id: str,
        trade_id: str,
    ) -> OrderResult:
        """
        함수 이름: _filled_result()
        기능: 주문 제출 수량 전체를 체결한 수수료 0 결과를 생성한다.
        인자: order -> 체결할 원 Order
            exchange_order_id -> 결과에 사용할 거래소 주문 ID
            trade_id -> fill dedup에 사용할 거래 ID
        반환값: FILLED OrderResult
        작성 날짜: 2026/08/22
        """
        fill = Fill(
            exchange_order_id=exchange_order_id,
            trade_id=trade_id,
            quantity=order.submitted_quantity,
            price=Decimal("2500.50"),
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

    def _terminal_partial_result(self, order: Order) -> OrderResult:
        """
        함수 이름: _terminal_partial_result()
        기능: pending SELL 제출 수량의 절반만 체결된 CANCELED 결과를 생성한다.
        인자: order -> STOP이 reconciliation하는 원 SELL Order
        반환값: 동일 fill identity를 가진 CANCELED OrderResult
        작성 날짜: 2026/08/22
        """
        fill = Fill(
            exchange_order_id="902",
            trade_id="partial-stop-sell",
            quantity=order.submitted_quantity / Decimal("2"),
            price=Decimal("2600"),
            fee_amount=Decimal("0"),
            fee_asset="USDT",
            fee_quote_amount=Decimal("0"),
            executed_at=self.clock(),
        )
        return OrderResult(
            symbol=order.symbol,
            client_order_id=order.client_order_id,
            exchange_order_id="902",
            status=OrderStatus.CANCELED,
            processed_at=self.clock(),
            fills=(fill,),
        )


def _execute_actions(
    controller: TradingController,
    actions: tuple[TradingActionRequest, ...],
) -> tuple[TradingEvent, ...]:
    """
    함수 이름: _execute_actions()
    기능: helper가 만든 runtime patch와 order action을 production 순서대로 실행한다.
    인자: controller -> action executor를 소유한 TradingController
        actions -> 순서가 보존된 typed action tuple
    반환값: 동기 terminal 처리에서 생성된 concrete outcome tuple
    작성 날짜: 2026/08/22
    """
    outcomes: list[TradingEvent] = []
    for action in actions:
        outcomes.extend(controller._execute_action(action))  # patch를 submit보다 먼저 적용한다.

    return tuple(outcomes)


class StopPersistenceRecoveryIntegrationTests(unittest.TestCase):
    """
    클래스 이름: StopPersistenceRecoveryIntegrationTests
    기능: pending STOP history 복구 뒤 잔여 force-sell이 중단 없이 완료되는지 검증한다.
    작성 날짜: 2026/08/22
    """

    def test_manual_kill_fsyncs_then_cancels_reconciles_and_liquidates(
        self,
    ) -> None:
        """
        함수 이름: test_manual_kill_fsyncs_then_cancels_reconciles_and_liquidates()
        기능: CANCEL_AND_LIQUIDATE가 receipt fsync 뒤 same-ID cancel·partial 반영·잔량 청산을 완료하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with TemporaryDirectory() as temporary_directory:
            clock = MutableUtcClock(MARKET_UPDATED_AT)
            rest_client = StopPersistenceRESTClient(clock)
            account = Account()
            web_socket_gateway = WebSocketGateway(
                SynchronousAccountWebSocketClient([]),
                account_snapshot_callback=account.apply_stream_snapshot,
            )
            position = Position()
            storage_path = Path(temporary_directory) / "trades.jsonl"
            repository = TradeHistoryRepository(storage_path, clock=clock)
            history_controller = TradeHistoryController(
                repository,
                clock=clock,
            )
            history_controller.load_trade_history()
            market_snapshot = _ready_market_snapshot()
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
            controller = TradingController(
                APIGateway(rest_client, clock=clock),
                web_socket_gateway,
                account,
                market_snapshot,
                command_gate=True,
                position=position,
                trade_history_controller=history_controller,
                risk_policy_state=policy,
                clock=clock,
            )

            # 실제 session에서 BUY Position을 연 뒤 일반 SELL을 NEW로 남겨 kill cancel 대상에 둔다.
            controller.load_account()
            selection = RegimeController(
                RegimeSTM(),
                market_snapshot,
                controller,
            ).set_regime_type(
                RegimeType.TYPE_0,
                command_id="select-manual-kill-cleanup",
                expected_version=0,
            )
            split_result = controller.update_split_ratios(
                command_id="split-manual-kill-cleanup",
                expected_version=selection.version,
                scale_in=Decimal("1"),
                scale_out=Decimal("1"),
            )
            controller.start_trading(
                command_id="start-manual-kill-cleanup",
                expected_version=split_result.version,
            )
            buy_event = TradingEvent(
                TradingEventType.MARKET_DATA_UPDATED,
                clock(),
                sequence_number=1,
            )
            buy_outcomes = _execute_actions(
                controller,
                create_entry_order_actions(
                    StrategyType.CASE_B,
                    OrderAttemptKind.INITIAL,
                    buy_event,
                    controller.context,
                ),
            )
            controller._enqueue_order_outcomes(buy_outcomes)
            asyncio.run(controller.drain_events())
            sell_event = TradingEvent(
                TradingEventType.MARKET_DATA_UPDATED,
                clock(),
                sequence_number=2,
            )
            self.assertEqual(
                (),
                _execute_actions(
                    controller,
                    create_exit_order_actions(
                        StrategyType.CASE_B,
                        ExitReason.TAKE_PROFIT,
                        PositionReturnState.CASE_B_HOLDING,
                        sell_event,
                        controller.context,
                        attempt_kind=OrderAttemptKind.INITIAL,
                    ),
                ),
            )

            # 실제 repository append가 끝난 직후 같은 trace에 marker를 남겨 외부 cancel보다 앞섰는지 본다.
            original_save = (
                TradeHistoryController.save_manual_kill_control_state
            )

            def save_and_trace(
                selected_controller: TradeHistoryController,
                state: ManualKillControlState,
            ) -> None:
                """
                함수 이름: save_and_trace()
                기능: concrete manual-kill journal을 저장한 뒤 operation trace에 durable 경계를 남긴다.
                인자: selected_controller -> 호출을 받은 concrete history Controller
                    state -> Controller가 fsync할 ManualKillControlState
                반환값: 없음
                작성 날짜: 2026/08/29
                """
                original_save(selected_controller, state)
                rest_client.operation_trace.append("fsync:MANUAL_KILL")

            with mock_patch.object(
                TradeHistoryController,
                "save_manual_kill_control_state",
                autospec=True,
                side_effect=save_and_trace,
            ):
                activation = controller.set_manual_kill(
                    True,
                    command_id="activate-cancel-and-liquidate",
                    expected_version=0,
                )

            # Kill receipt 뒤에만 query→개별 cancel→same-ID query→잔여 force SELL이 실행돼야 한다.
            self.assertTrue(activation.active)
            self.assertIs(
                ManualKillBehavior.CANCEL_AND_LIQUIDATE,
                activation.behavior,
            )
            self.assertEqual(
                [
                    "submit:BUY",
                    "submit:SELL",
                    "fsync:MANUAL_KILL",
                    "query:SELL",
                    "cancel:SELL",
                    "query:SELL",
                    "submit:SELL",
                ],
                rest_client.operation_trace,
            )
            self.assertEqual(Decimal("0"), position.quantity)
            self.assertTrue(controller.manual_kill_cleanup_complete)
            self.assertFalse(controller.command_enabled)
            self.assertEqual(
                ManualKillControlState(
                    active=True,
                    version=1,
                    command_id="activate-cancel-and-liquidate",
                    expected_version=0,
                    behavior=ManualKillBehavior.CANCEL_AND_LIQUIDATE,
                    policy_version=1,
                ),
                repository.get_manual_kill_control_state(),
            )

            # Durable force outcome을 STM이 소비한 뒤 session도 canonical TERMINATED 상태로 닫힌다.
            asyncio.run(controller.drain_events())
            self.assertIs(controller.status, TradingSessionStatus.TERMINATED)
            self.assertEqual(
                ("901", "902", "903"),
                tuple(
                    trade.order_id
                    for trade in history_controller.trade_history.trades
                ),
            )

            # Activation 응답 유실 재시도는 완료 증거만 다시 확인하고 cancel·SELL을 만들지 않는다.
            replayed_activation = controller.set_manual_kill(
                True,
                command_id="activate-cancel-and-liquidate",
                expected_version=0,
            )
            self.assertEqual(activation, replayed_activation)

            # Active epoch 중 policy가 바뀌어도 no-op은 최초 C&L provenance와 완료 증거를 보존한다.
            controller.replace_risk_policy(
                RiskPolicy(
                    version=2,
                    max_order_notional=None,
                    max_position_notional=None,
                    max_daily_loss=None,
                    daily_loss_scope=DailyLossScope.REALIZED_ONLY,
                    manual_kill_behavior=(
                        ManualKillBehavior.BLOCK_NEW_ORDERS
                    ),
                )
            )
            confirmed_active = controller.set_manual_kill(
                True,
                command_id="confirm-cleanup-after-policy-change",
                expected_version=1,
            )
            self.assertIs(
                ManualKillBehavior.CANCEL_AND_LIQUIDATE,
                confirmed_active.behavior,
            )
            self.assertEqual(1, confirmed_active.policy_version)
            self.assertTrue(controller.manual_kill_cleanup_complete)
            self.assertEqual(
                ManualKillControlState(
                    active=True,
                    version=1,
                    command_id="confirm-cleanup-after-policy-change",
                    expected_version=1,
                    behavior=ManualKillBehavior.CANCEL_AND_LIQUIDATE,
                    policy_version=1,
                ),
                repository.get_manual_kill_control_state(),
            )
            self.assertEqual(
                [
                    "submit:BUY",
                    "submit:SELL",
                    "fsync:MANUAL_KILL",
                    "query:SELL",
                    "cancel:SELL",
                    "query:SELL",
                    "submit:SELL",
                ],
                rest_client.operation_trace,
            )  # Policy 확인 command는 cancel 또는 SELL effect를 중복 생성하지 않는다.

            # 이전 완료 뒤 새 app-owned open order가 보이면 해제 직전 fresh REST 검증이 release를 거부한다.
            rest_client.open_order_results = (
                OrderResult(
                    symbol="ETHUSDT",
                    client_order_id="bat-orphan-after-cleanup",
                    exchange_order_id="904",
                    status=OrderStatus.NEW,
                    processed_at=clock(),
                ),
            )
            with self.assertRaises(TradingSessionError) as error_context:
                controller.set_manual_kill(
                    False,
                    command_id="release-after-new-open-order",
                    expected_version=1,
                )

            self.assertIs(
                error_context.exception.code,
                TradingSessionFailureCode.POSITION_RECONCILIATION_REQUIRED,
            )
            self.assertFalse(controller.manual_kill_cleanup_complete)
            self.assertTrue(controller.reconciliation_required)
            self.assertTrue(repository.get_manual_kill_control_state().active)

    def test_manual_kill_reenters_cancel_after_reconciliation_and_reconnect(
        self,
    ) -> None:
        """
        함수 이름: test_manual_kill_reenters_cancel_after_reconciliation_and_reconnect()
        기능: 이미 RECON인 C&L이 같은 주문을 재취소하고 terminal partial 뒤 잔량만 청산하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with TemporaryDirectory() as temporary_directory:
            clock = MutableUtcClock(MARKET_UPDATED_AT)
            rest_client = StopPersistenceRESTClient(
                clock,
                terminal_query_index=5,
            )
            account = Account()
            web_socket_client = SynchronousAccountWebSocketClient([])
            web_socket_gateway = WebSocketGateway(
                web_socket_client,
                account_snapshot_callback=account.apply_stream_snapshot,
            )
            position = Position()
            repository = TradeHistoryRepository(
                Path(temporary_directory) / "trades.jsonl",
                clock=clock,
            )
            history_controller = TradeHistoryController(
                repository,
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
                risk_policy_state=RiskPolicy(
                    version=1,
                    max_order_notional=None,
                    max_position_notional=None,
                    max_daily_loss=None,
                    daily_loss_scope=DailyLossScope.REALIZED_ONLY,
                    manual_kill_behavior=(
                        ManualKillBehavior.CANCEL_AND_LIQUIDATE
                    ),
                ),
                clock=clock,
            )

            # 정상 session에서 BUY를 채운 뒤 active SELL 하나를 durable pending으로 남긴다.
            controller.load_account()
            selection = RegimeController(
                RegimeSTM(),
                market_snapshot,
                controller,
            ).set_regime_type(
                RegimeType.TYPE_0,
                command_id="select-manual-kill-reentry",
                expected_version=0,
            )
            split_result = controller.update_split_ratios(
                command_id="split-manual-kill-reentry",
                expected_version=selection.version,
                scale_in=Decimal("1"),
                scale_out=Decimal("1"),
            )
            controller.start_trading(
                command_id="start-manual-kill-reentry",
                expected_version=split_result.version,
            )
            buy_event = TradingEvent(
                TradingEventType.MARKET_DATA_UPDATED,
                clock(),
                sequence_number=1,
            )
            buy_outcomes = _execute_actions(
                controller,
                create_entry_order_actions(
                    StrategyType.CASE_B,
                    OrderAttemptKind.INITIAL,
                    buy_event,
                    controller.context,
                ),
            )
            controller._enqueue_order_outcomes(buy_outcomes)
            asyncio.run(controller.drain_events())
            sell_event = TradingEvent(
                TradingEventType.MARKET_DATA_UPDATED,
                clock(),
                sequence_number=2,
            )
            self.assertEqual(
                (),
                _execute_actions(
                    controller,
                    create_exit_order_actions(
                        StrategyType.CASE_B,
                        ExitReason.TAKE_PROFIT,
                        PositionReturnState.CASE_B_HOLDING,
                        sell_event,
                        controller.context,
                        attempt_kind=OrderAttemptKind.INITIAL,
                    ),
                ),
            )

            # 기존 주문 query budget failure를 재현해 C&L activation 이전부터 session을 RECON으로 둔다.
            pending_state = next(
                state
                for state in controller._order_states_by_client_id.values()
                if state.order.side is OrderSide.SELL
                and not state.order.is_terminal
            )
            controller._enter_order_reconciliation(
                pending_state,
                OrderExecutionFailureCode.QUERY_BUDGET_EXHAUSTED,
                message_id=None,
            )
            self.assertIs(
                controller.status,
                TradingSessionStatus.RECONCILIATION_REQUIRED,
            )

            # 첫 activation은 query→cancel→query가 계속 active라서 Position SELL을 만들지 않는다.
            activation = controller.set_manual_kill(
                True,
                command_id="activate-manual-kill-reentry",
                expected_version=0,
            )
            self.assertTrue(activation.active)
            self.assertFalse(controller.manual_kill_cleanup_complete)
            self.assertEqual(2, len(rest_client.submitted_orders))
            self.assertEqual(
                [
                    "submit:BUY",
                    "submit:SELL",
                    "query:SELL",
                    "cancel:SELL",
                    "query:SELL",
                ],
                rest_client.operation_trace,
            )

            # Fresh account reconnect도 같은 ID를 재조회·개별 취소하고 다섯 번째 terminal만 반영한다.
            if web_socket_client.on_disconnect is None:
                raise AssertionError("account stream disconnect callback is required")
            web_socket_client.on_disconnect()
            controller.reconnect_account_stream_after_reconciliation()

            self.assertEqual(Decimal("0"), position.quantity)
            self.assertTrue(controller.manual_kill_cleanup_complete)
            self.assertFalse(controller.command_enabled)
            self.assertEqual(3, len(rest_client.submitted_orders))
            self.assertIs(rest_client.submitted_orders[-1].side, OrderSide.SELL)
            self.assertEqual(
                [
                    "submit:BUY",
                    "submit:SELL",
                    "query:SELL",
                    "cancel:SELL",
                    "query:SELL",
                    "query:SELL",
                    "query:SELL",
                    "cancel:SELL",
                    "query:SELL",
                    "submit:SELL",
                ],
                rest_client.operation_trace,
            )  # 두 cancel 모두 같은 client ID이고 신규 POST는 정확한 잔량 SELL 하나뿐이다.

            asyncio.run(controller.drain_events())
            self.assertIs(controller.status, TradingSessionStatus.TERMINATED)
            self.assertEqual(
                (),
                history_controller.get_pending_order_recovery_records(),
            )

    def test_pending_stop_partial_save_retry_continues_residual_force_sell(
        self,
    ) -> None:
        """
        함수 이름: test_pending_stop_partial_save_retry_continues_residual_force_sell()
        기능: query-cancel-query partial의 저장 재시도 뒤 잔량 전량 매도와 종료를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        with TemporaryDirectory() as temporary_directory:
            clock = MutableUtcClock(MARKET_UPDATED_AT)
            rest_client = StopPersistenceRESTClient(clock)
            account = Account()
            web_socket_gateway = WebSocketGateway(
                SynchronousAccountWebSocketClient([]),
                account_snapshot_callback=account.apply_stream_snapshot,
            )
            position = Position()
            storage_path = Path(temporary_directory) / "trades.jsonl"
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

            # Account와 TYPE_0 session을 준비한 뒤 실제 BUY fill로 Position owner를 연다.
            controller.load_account()
            selection = RegimeController(
                RegimeSTM(),
                market_snapshot,
                controller,
            ).set_regime_type(
                RegimeType.TYPE_0,
                command_id="select-stop-persistence",
                expected_version=0,
            )
            split_result = controller.update_split_ratios(
                command_id="split-stop-persistence",
                expected_version=selection.version,
                scale_in=Decimal("1"),
                scale_out=Decimal("1"),
            )
            controller.start_trading(
                command_id="start-stop-persistence",
                expected_version=split_result.version,
            )
            buy_event = TradingEvent(
                TradingEventType.MARKET_DATA_UPDATED,
                clock(),
                sequence_number=1,
            )
            buy_outcomes = _execute_actions(
                controller,
                create_entry_order_actions(
                    StrategyType.CASE_B,
                    OrderAttemptKind.INITIAL,
                    buy_event,
                    controller.context,
                ),
            )
            controller._enqueue_order_outcomes(buy_outcomes)
            asyncio.run(controller.drain_events())
            opened_quantity = position.quantity
            self.assertGreater(opened_quantity, Decimal("0"))

            # Case B SELL은 취소하지 않고 첫 NEW query 뒤 다음 same-ID query에서 terminal을 확정한다.
            clock.advance(1)
            sell_event = TradingEvent(
                TradingEventType.MARKET_DATA_UPDATED,
                clock(),
                sequence_number=2,
            )
            sell_outcomes = _execute_actions(
                controller,
                create_exit_order_actions(
                    StrategyType.CASE_B,
                    ExitReason.TAKE_PROFIT,
                    PositionReturnState.CASE_B_HOLDING,
                    sell_event,
                    controller.context,
                    attempt_kind=OrderAttemptKind.INITIAL,
                ),
            )
            self.assertEqual((), sell_outcomes)
            self.assertIsNotNone(controller.context.pending_order)

            # terminal partial Position은 유지하되 JSONL fsync만 실패시켜 save-only lock을 만든다.
            with mock_patch(
                "binance_auto_trader.adapters.persistence."
                "trade_history_repository.os.fsync",
                side_effect=OSError("controlled STOP history fsync failure"),
            ):
                stopped = controller.stop_trading(
                    command_id="stop-with-pending-partial",
                    expected_version=controller.context.version,
                )
                self.assertEqual([], rest_client.canceled_orders)
                clock.advance(5)
                controller.trigger_order_reconciliation(occurred_at=clock())
            self.assertIs(
                controller.status,
                TradingSessionStatus.RECONCILIATION_REQUIRED,
            )
            self.assertEqual(
                [
                    "submit:BUY",
                    "submit:SELL",
                    "query:SELL",
                    "query:SELL",
                ],
                rest_client.operation_trace,
            )
            self.assertEqual(frozenset({"902"}), history_controller.dirty_order_ids)
            with localcontext() as decimal_context:
                decimal_context.prec = 34
                decimal_context.rounding = ROUND_HALF_EVEN
                expected_remaining_quantity = (
                    opened_quantity
                    - rest_client.queried_orders[-1].fills[0].quantity
                )
            self.assertEqual(expected_remaining_quantity, position.quantity)

            # 저장 재시도는 902를 중복 append하지 않고 잔여 Position만 새 force SELL로 넘긴다.
            operation_count_before_retry = len(rest_client.operation_trace)
            recovered_outcomes = controller.retry_pending_order_persistence("902")
            self.assertEqual(
                (TradingEventType.FORCE_SELL_FINISHED,),
                tuple(event.event_type for event in recovered_outcomes),
            )
            self.assertEqual(
                ["submit:SELL"],
                rest_client.operation_trace[operation_count_before_retry:],
            )
            self.assertEqual(3, len(rest_client.submitted_orders))
            self.assertIs(rest_client.submitted_orders[-1].side, OrderSide.SELL)
            self.assertNotEqual(
                rest_client.submitted_orders[1].client_order_id,
                rest_client.submitted_orders[2].client_order_id,
            )
            self.assertEqual(Decimal("0"), position.quantity)
            self.assertEqual(frozenset(), history_controller.dirty_order_ids)

            # FORCE_SELL_FINISHED가 다음 microstep에서 G-06F를 닫고 세 terminal order를 보존한다.
            asyncio.run(controller.drain_events())
            self.assertIs(controller.status, TradingSessionStatus.TERMINATED)
            self.assertEqual(
                ("901", "902", "903"),
                tuple(
                    trade.order_id
                    for trade in history_controller.trade_history.trades
                ),
            )
            self.assertEqual(3, storage_path.read_bytes().count(b"\n"))

    def test_user_stop_pre_query_terminal_continues_force_sell(
        self,
    ) -> None:
        """
        함수 이름: test_user_stop_pre_query_terminal_continues_force_sell()
        기능: G-06P의 첫 pending query가 terminal일 때 일반 CASE 결과 없이 잔량 매도를 완료한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        with TemporaryDirectory() as temporary_directory:
            clock = MutableUtcClock(MARKET_UPDATED_AT)
            rest_client = StopPersistenceRESTClient(
                clock,
                pre_query_terminal=True,
            )
            account = Account()
            web_socket_gateway = WebSocketGateway(
                SynchronousAccountWebSocketClient([]),
                account_snapshot_callback=account.apply_stream_snapshot,
            )
            position = Position()
            storage_path = Path(temporary_directory) / "user-stop-trades.jsonl"
            history_controller = TradeHistoryController(
                TradeHistoryRepository(storage_path, clock=clock),
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

            # TYPE_0 session을 열고 BUY fill을 반영해 G-06P가 정리할 실제 노출을 만든다.
            controller.load_account()
            selection = RegimeController(
                RegimeSTM(),
                market_snapshot,
                controller,
            ).set_regime_type(
                RegimeType.TYPE_0,
                command_id="select-stop-pre-query-terminal",
                expected_version=0,
            )
            split_result = controller.update_split_ratios(
                command_id="split-stop-pre-query-terminal",
                expected_version=selection.version,
                scale_in=Decimal("1"),
                scale_out=Decimal("1"),
            )
            controller.start_trading(
                command_id="start-stop-pre-query-terminal",
                expected_version=split_result.version,
            )

            # Public lower-touch event로 STM을 실제 TRADE_MANAGEMENT root에 진입시킨다.
            accepted_lower_event = controller.enqueue_event(
                TradingEvent(
                    TradingEventType.LOWER_BAND_TOUCHED,
                    clock(),
                    event_id="lower-touch-before-user-stop",
                )
            )
            self.assertIsNotNone(accepted_lower_event)
            activation_results = asyncio.run(controller.drain_events())
            self.assertIn(
                "G-02",
                tuple(
                    transition_id
                    for result in activation_results
                    for transition_id in result.transition_ids
                ),
            )

            buy_event = TradingEvent(
                TradingEventType.MARKET_DATA_UPDATED,
                clock(),
                sequence_number=1,
            )
            buy_outcomes = _execute_actions(
                controller,
                create_entry_order_actions(
                    StrategyType.CASE_B,
                    OrderAttemptKind.INITIAL,
                    buy_event,
                    controller.context,
                ),
            )
            controller._enqueue_order_outcomes(buy_outcomes)
            asyncio.run(controller.drain_events())
            self.assertGreater(position.quantity, Decimal("0"))

            # 일반 SELL은 NEW로 남겨 사용자 STOP의 ReconcileOrder 조회 대상으로 고정한다.
            clock.advance(1)
            sell_event = TradingEvent(
                TradingEventType.MARKET_DATA_UPDATED,
                clock(),
                sequence_number=2,
            )
            sell_outcomes = _execute_actions(
                controller,
                create_exit_order_actions(
                    StrategyType.CASE_B,
                    ExitReason.TAKE_PROFIT,
                    PositionReturnState.CASE_B_HOLDING,
                    sell_event,
                    controller.context,
                    attempt_kind=OrderAttemptKind.INITIAL,
                ),
            )
            self.assertEqual((), sell_outcomes)
            self.assertIsNotNone(controller.context.pending_order)

            # 사용자 STOP event의 첫 query terminal partial이 취소 없이 잔량 force SELL로 직결되게 한다.
            clock.advance(1)
            stop_result = controller.stop_trading(
                command_id="stop-pre-query-terminal",
                expected_version=controller.context.version,
            )
            results = (stop_result, *asyncio.run(controller.drain_events()))

            # G-06P는 일반 CASE sell outcome으로 멈추지 않고 G-06F 종료까지 소비한다.
            self.assertIn(
                "G-06P",
                tuple(
                    transition_id
                    for result in results
                    for transition_id in result.transition_ids
                ),
            )
            self.assertIn(
                "G-06F",
                tuple(
                    transition_id
                    for result in results
                    for transition_id in result.transition_ids
                ),
            )
            self.assertIs(controller.status, TradingSessionStatus.TERMINATED)
            self.assertEqual(Decimal("0"), position.quantity)
            self.assertEqual([], rest_client.canceled_orders)
            self.assertEqual(
                [
                    "submit:BUY",
                    "submit:SELL",
                    "query:SELL",
                    "submit:SELL",
                ],
                rest_client.operation_trace,
            )
            self.assertEqual(
                ("901", "902", "903"),
                tuple(
                    trade.order_id
                    for trade in history_controller.trade_history.trades
                ),
            )
            self.assertEqual(3, storage_path.read_bytes().count(b"\n"))


if __name__ == "__main__":
    unittest.main()
