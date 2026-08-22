"""Phase 8 execution 기록의 순서, durable 경계와 dirty 저장 재시도를 검증한다."""

from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
import unittest
from unittest.mock import patch

import binance_auto_trader.application.trade_history_controller as controller_module
from binance_auto_trader.application.trade_history_controller import (
    TradeHistoryController,
    TradeHistoryPersistencePendingError,
)
from binance_auto_trader.domain.history import (
    OrderHistoryConflictError,
    Performance,
    Trade,
    TradeHistory,
)
from binance_auto_trader.domain.trading.states import OrderSide

from tests.unit.history.factories import make_order_execution


CURRENT_TIME = datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc)


def fixed_clock() -> datetime:
    """
    함수 이름: fixed_clock()
    기능: controller Performance의 현재 KST account day를 고정한다.
    인자: 없음
    반환값: 고정 UTC datetime
    작성 날짜: 2026/08/22
    """
    return CURRENT_TIME


class RecordingRepository:
    """
    클래스 이름: RecordingRepository
    기능: in-memory durable index와 저장 실패 및 호출 순서를 제어하는 repository fake이다.
    작성 날짜: 2026/08/22
    """

    def __init__(
        self,
        *,
        trace: list[str] | None = None,
        save_failures: int = 0,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 비어 있는 durable index와 선택적 trace 및 남은 저장 실패 횟수를 준비한다.
        인자: trace -> save 단계 이름을 기록할 optional list
            save_failures -> 성공 전에 발생시킬 저장 오류 횟수
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self.trace = [] if trace is None else trace
        self.save_failures = save_failures
        self.save_call_count = 0
        self.trades_by_order_id: dict[str, Trade] = {}
        self.after_durable_write: Callable[[], None] | None = None

    def get_trade_history(self) -> tuple[Trade, ...]:
        """
        함수 이름: get_trade_history()
        기능: in-memory durable order 순서의 Trade tuple을 반환한다.
        인자: 없음
        반환값: durable Trade tuple
        작성 날짜: 2026/08/22
        """
        return tuple(self.trades_by_order_id.values())

    def save_this_trade_by_order_id(
        self,
        order_id: int | str,
        trade: Trade,
    ) -> None:
        """
        함수 이름: save_this_trade_by_order_id()
        기능: 제어된 실패 뒤 order ID idempotency로 Trade를 in-memory durable 저장한다.
        인자: order_id -> 저장 key
            trade -> 저장할 terminal Trade
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        normalized_order_id = str(order_id)
        self.trace.append("13.5")
        self.save_call_count += 1
        if self.save_failures > 0:
            self.save_failures -= 1
            raise OSError("controlled persistence failure")

        # 실제 repository와 같이 동일 내용은 no-op, 다른 내용은 typed conflict로 처리한다.
        existing_trade = self.trades_by_order_id.get(normalized_order_id)
        if existing_trade is not None and existing_trade != trade:
            raise OrderHistoryConflictError("controlled order conflict")
        self.trades_by_order_id[normalized_order_id] = trade
        if self.after_durable_write is not None:
            self.after_durable_write()  # repository 반환 전 publication 상태를 관찰한다.


class TradeHistoryControllerPhase8Tests(unittest.TestCase):
    """
    클래스 이름: TradeHistoryControllerPhase8Tests
    기능: 메시지 13.1~13.5 조정과 저장 전후 atomic state 경계를 테스트한다.
    작성 날짜: 2026/08/22
    """

    def test_sell_records_exact_submessage_order_and_publishes_after_save(
        self,
    ) -> None:
        """
        함수 이름: test_sell_records_exact_submessage_order_and_publishes_after_save()
        기능: SELL의 13.1~13.5 순서와 durable write 반환 뒤 state publication을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        trace: list[str] = []
        repository = RecordingRepository(trace=trace)
        controller = TradeHistoryController(repository, clock=fixed_clock)
        order, summary = make_order_execution(
            exchange_order_id="401",
            side=OrderSide.SELL,
        )
        observed_during_save: list[tuple[int, Decimal]] = []
        repository.after_durable_write = lambda: observed_during_save.append(
            (
                len(controller.trade_history.trades),
                controller.performance.realized_pnl,
            )
        )

        # 실제 domain operation을 감싸 메시지 하위 호출 순서만 관찰한다.
        original_calculate = Performance.calculate_realized_result
        original_factory = Trade.from_order_execution
        original_add_trade = TradeHistory.add_trade
        original_apply_trade = Performance.apply_new_trade

        def calculate_realized_result(
            performance: Performance,
            execution_summary: object,
            allocated_cost_basis: Decimal,
        ) -> object:
            """
            함수 이름: calculate_realized_result()
            기능: 메시지 13.1을 기록하고 실제 realized 계산을 호출한다.
            인자: performance -> 계산을 수행할 Performance
                execution_summary -> 실제 ExecutionSummary
                allocated_cost_basis -> 사전 고정 원가
            반환값: 실제 RealizedResult
            작성 날짜: 2026/08/22
            """
            trace.append("13.1")
            return original_calculate(
                performance,
                execution_summary,
                allocated_cost_basis,
            )

        def build_trade(*factory_arguments: object) -> Trade:
            """
            함수 이름: build_trade()
            기능: 메시지 13.2를 기록하고 실제 Trade factory를 호출한다.
            인자: factory_arguments -> factory에 전달된 order, summary, realized result
            반환값: 실제 Trade
            작성 날짜: 2026/08/22
            """
            trace.append("13.2")
            return original_factory(*factory_arguments)

        def add_trade(history: TradeHistory, trade: Trade) -> None:
            """
            함수 이름: add_trade()
            기능: 메시지 13.3을 기록하고 candidate TradeHistory에 실제 Trade를 추가한다.
            인자: history -> 공개 전 candidate TradeHistory
                trade -> 추가할 실제 Trade
            반환값: 없음
            작성 날짜: 2026/08/22
            """
            trace.append("13.3")
            original_add_trade(history, trade)

        def apply_new_trade(performance: Performance, trade: Trade) -> None:
            """
            함수 이름: apply_new_trade()
            기능: 메시지 13.4를 기록하고 candidate Performance에 실제 Trade를 반영한다.
            인자: performance -> 공개 전 candidate Performance
                trade -> 반영할 실제 Trade
            반환값: 없음
            작성 날짜: 2026/08/22
            """
            trace.append("13.4")
            original_apply_trade(performance, trade)

        with patch.object(
            Performance,
            "calculate_realized_result",
            autospec=True,
            side_effect=calculate_realized_result,
        ), patch.object(
            Trade,
            "from_order_execution",
            side_effect=build_trade,
        ), patch.object(
            TradeHistory,
            "add_trade",
            autospec=True,
            side_effect=add_trade,
        ), patch.object(
            Performance,
            "apply_new_trade",
            autospec=True,
            side_effect=apply_new_trade,
        ):
            trade = controller.record_order_execution(
                order,
                summary,
                Decimal("100.10"),
            )

        self.assertEqual(trace, ["13.1", "13.2", "13.3", "13.4", "13.5"])
        self.assertEqual(observed_during_save, [(0, Decimal("0"))])
        self.assertEqual(controller.trade_history.trades, (trade,))
        self.assertEqual(controller.performance.realized_pnl, Decimal("9.79"))
        self.assertEqual(repository.trades_by_order_id, {"401": trade})

    def test_storage_failure_keeps_published_state_clean_until_same_order_retry(
        self,
    ) -> None:
        """
        함수 이름: test_storage_failure_keeps_published_state_clean_until_same_order_retry()
        기능: 저장 실패가 공개 snapshot을 오염시키지 않고 save-only retry 뒤 게시하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        repository = RecordingRepository(save_failures=1)
        controller = TradeHistoryController(repository, clock=fixed_clock)
        order, summary = make_order_execution(exchange_order_id="501")
        previous_history = controller.trade_history
        previous_performance = controller.performance

        # 첫 호출은 candidate만 dirty로 보존하고 authoritative state identity를 유지한다.
        with self.assertRaisesRegex(OSError, "controlled persistence failure"):
            controller.record_order_execution(order, summary)

        self.assertIs(controller.trade_history, previous_history)
        self.assertIs(controller.performance, previous_performance)
        self.assertEqual(controller.trade_history.trades, ())
        self.assertEqual(controller.performance.total_fee, Decimal("0"))
        self.assertEqual(controller.dirty_order_ids, frozenset({"501"}))

        with self.assertRaises(TradeHistoryPersistencePendingError):
            next_order, next_summary = make_order_execution(
                exchange_order_id="502"
            )
            controller.record_order_execution(next_order, next_summary)

        retried_trade = controller.retry_pending_persistence("501")

        self.assertEqual(repository.save_call_count, 2)
        self.assertEqual(controller.trade_history.trades, (retried_trade,))
        self.assertEqual(controller.performance.total_fee, Decimal("0.10"))
        self.assertEqual(controller.dirty_order_ids, frozenset())

    def test_buy_rejects_allocated_cost_and_skips_realized_fields(self) -> None:
        """
        함수 이름: test_buy_rejects_allocated_cost_and_skips_realized_fields()
        기능: BUY는 원가 인자를 거부하고 정상 기록에서는 realized 필드를 null로 유지한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        repository = RecordingRepository()
        controller = TradeHistoryController(repository, clock=fixed_clock)
        order, summary = make_order_execution(exchange_order_id="601")

        with self.assertRaisesRegex(ValueError, "must be None for BUY"):
            controller.record_order_execution(
                order,
                summary,
                Decimal("100"),
            )

        trade = controller.record_order_execution(order, summary)

        self.assertIsNone(trade.allocated_cost_basis)
        self.assertIsNone(trade.realized_pnl)
        self.assertIsNone(trade.realized_return_rate)
        self.assertEqual(controller.performance.total_fee, Decimal("0.10"))


if __name__ == "__main__":
    unittest.main()
