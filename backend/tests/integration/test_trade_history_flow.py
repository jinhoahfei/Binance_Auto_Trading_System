"""Communication 메시지 3~3.3의 History/Performance 초기 로드를 종단 간 검증한다."""

from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from binance_auto_trader.adapters.persistence import TradeHistoryRepository
import binance_auto_trader.application.trade_history_controller as controller_module
from binance_auto_trader.application.trade_history_controller import (
    TradeHistoryController,
)
from binance_auto_trader.domain.history import Performance, Trade, TradeHistory
from binance_auto_trader.domain.trading.states import ExitReason, OrderSide

from tests.integration.trace_helpers import (
    TraceEntry,
    assert_trace_contract,
    finish_trace_entry,
    start_trace_entry,
)
from tests.unit.history.factories import make_trade, make_trade_record


CURRENT_TIME = datetime(2026, 8, 21, 3, 0, tzinfo=timezone.utc)
HISTORY_LOAD_COMMAND_ID = "history-load-20260821-001"


def fixed_clock() -> datetime:
    """
    함수 이름: fixed_clock()
    기능: 통합 흐름의 Performance account day를 2026-08-21 KST로 고정한다.
    인자: 없음
    반환값: 고정 UTC datetime
    작성 날짜: 2026/08/21
    """
    return CURRENT_TIME


def encode_history(trades: tuple[Trade, ...]) -> bytes:
    """
    함수 이름: encode_history()
    기능: 통합 test Trade tuple을 LF로 끝나는 ADR-004 JSONL bytes로 변환한다.
    인자: trades -> JSONL record로 만들 Trade tuple
    반환값: UTF-8 JSONL bytes
    작성 날짜: 2026/08/21
    """
    encoded_lines = (
        json.dumps(
            make_trade_record(trade),
            separators=(",", ":"),
        ).encode("utf-8")
        for trade in trades
    )
    return b"\n".join(encoded_lines) + b"\n"


def make_second_golden_sell() -> Trade:
    """
    함수 이름: make_second_golden_sell()
    기능: D-11 통합 test에 사용할 두 번째 손실 SELL Trade를 생성한다.
    인자: 없음
    반환값: realized PnL -10.19인 SELL Trade
    작성 날짜: 2026/08/21
    """
    return make_trade(
        trade_id="sell-2",
        order_id="3",
        side=OrderSide.SELL,
        executed_amount=Decimal("90"),
        average_fill_price=Decimal("90"),
        fee_amount=Decimal("0.09"),
        fee_asset="USDT",
        fee_quote_amount=Decimal("0.09"),
        allocated_cost_basis=Decimal("100.10"),
        realized_pnl=Decimal("-10.19"),
        realized_return_rate=Decimal("-10.17982018"),
        exit_reason=ExitReason.STOP,
    )


class TracingRepository:
    """
    클래스 이름: TracingRepository
    기능: Controller 단계 순서와 반복 load 결과를 제어하는 in-memory repository이다.
    작성 날짜: 2026/08/21
    """

    def __init__(self, trades: tuple[Trade, ...], trace: list[str]) -> None:
        """
        함수 이름: __init__()
        기능: 반환할 Trade tuple과 외부 단계 trace를 보존한다.
        인자: trades -> get_trade_history에서 반환할 Trade tuple
            trace -> 호출 순서를 기록할 list
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.trades = trades
        self.trace = trace

    def get_trade_history(self) -> tuple[Trade, ...]:
        """
        함수 이름: get_trade_history()
        기능: repository 단계를 기록하고 현재 설정된 Trade tuple을 반환한다.
        인자: 없음
        반환값: 현재 startup Trade tuple
        작성 날짜: 2026/08/21
        """
        self.trace.append("repository")
        return self.trades


class TracingTradeHistoryRepository(TradeHistoryRepository):
    """
    클래스 이름: TracingTradeHistoryRepository
    기능: 실제 JSONL repository 호출에 메시지 3.1과 3.1.1 trace를 추가한다.
    작성 날짜: 2026/08/21
    """

    def __init__(
        self,
        storage_path: Path,
        trace: list[TraceEntry],
        state_version: Callable[[], int],
    ) -> None:
        """
        함수 이름: __init__()
        기능: 실제 storage 경로와 구조화 trace 및 publication version 공급자를 보존한다.
        인자: storage_path -> 읽을 JSONL 파일 경로
            trace -> 구조화 Communication trace 목록
            state_version -> 현재 history publication version 공급자
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        super().__init__(storage_path)
        self.integration_trace = trace
        self.state_version = state_version

    def get_trade_history(self) -> tuple[Trade, ...]:
        """
        함수 이름: get_trade_history()
        기능: 실제 streaming restore 주위에 repository와 local file 메시지를 기록한다.
        인자: 없음
        반환값: 실제 repository가 복원한 Trade tuple
        작성 날짜: 2026/08/21
        """
        repository_entry = start_trace_entry(
            self.integration_trace,
            message_id="3.1",
            caller="TradeHistoryController",
            receiver="TradeHistoryRepository",
            command_event_id=HISTORY_LOAD_COMMAND_ID,
            state_version_before=self.state_version(),
            related_id=(),
        )
        file_entry = start_trace_entry(
            self.integration_trace,
            message_id="3.1.1",
            caller="TradeHistoryRepository",
            receiver="LocalFileSystem",
            command_event_id=HISTORY_LOAD_COMMAND_ID,
            state_version_before=self.state_version(),
            related_id=(),
        )
        try:
            trades = super().get_trade_history()
        except Exception as error:
            finish_trace_entry(
                file_entry,
                state_version_after=self.state_version(),
                error=error,
            )
            finish_trace_entry(
                repository_entry,
                state_version_after=self.state_version(),
                error=error,
            )
            raise

        restored_order_ids = tuple(trade.order_id for trade in trades)
        file_entry["related_id"] = restored_order_ids
        repository_entry["related_id"] = restored_order_ids
        finish_trace_entry(
            file_entry,
            state_version_after=self.state_version(),
        )
        finish_trace_entry(
            repository_entry,
            state_version_after=self.state_version(),
        )
        return trades


class TradeHistoryFlowTests(unittest.TestCase):
    """
    클래스 이름: TradeHistoryFlowTests
    기능: JSONL repository부터 domain snapshot publication까지의 초기 로드를 테스트한다.
    작성 날짜: 2026/08/21
    """

    def test_history_startup_trace_reproduces_golden_snapshot(self) -> None:
        """
        함수 이름: test_history_startup_trace_reproduces_golden_snapshot()
        기능: 메시지 3~3.3 trace와 JSONL의 D-11 snapshot 복원을 함께 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        trades = (
            make_trade(trade_id="buy", order_id="1"),
            make_trade(trade_id="sell-1", order_id="2", side=OrderSide.SELL),
            make_second_golden_sell(),
        )

        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "trades.jsonl"
            history_path.write_bytes(encode_history(trades))
            integration_trace: list[TraceEntry] = []
            publication_version = [0]

            def state_version() -> int:
                """
                함수 이름: state_version()
                기능: startup publish 전후의 test context version을 반환한다.
                인자: 없음
                반환값: 현재 publication version
                작성 날짜: 2026/08/21
                """
                return publication_version[0]

            repository = TracingTradeHistoryRepository(
                history_path,
                integration_trace,
                state_version,
            )
            controller = TradeHistoryController(repository, clock=fixed_clock)

            def build_trade_history(
                restored_trades: tuple[Trade, ...],
            ) -> TradeHistory:
                """
                함수 이름: build_trade_history()
                기능: 실제 TradeHistory 생성의 메시지 3.2와 order ID를 기록한다.
                인자: restored_trades -> repository가 복원한 Trade tuple
                반환값: 실제 TradeHistory instance
                작성 날짜: 2026/08/21
                """
                restored_order_ids = tuple(
                    trade.order_id for trade in restored_trades
                )
                trace_entry = start_trace_entry(
                    integration_trace,
                    message_id="3.2",
                    caller="TradeHistoryController",
                    receiver="TradeHistory",
                    command_event_id=HISTORY_LOAD_COMMAND_ID,
                    state_version_before=state_version(),
                    related_id=restored_order_ids,
                )
                try:
                    history = TradeHistory(restored_trades)
                except Exception as error:
                    finish_trace_entry(
                        trace_entry,
                        state_version_after=state_version(),
                        error=error,
                    )
                    raise

                finish_trace_entry(
                    trace_entry,
                    state_version_after=state_version(),
                )
                return history

            def build_performance(
                restored_trades: tuple[Trade, ...],
                clock: Callable[[], datetime] | None,
            ) -> Performance:
                """
                함수 이름: build_performance()
                기능: 실제 Performance 생성의 메시지 3.3과 order ID를 기록한다.
                인자: restored_trades -> TradeHistory에 적용된 Trade tuple
                    clock -> KST account day를 결정할 주입 clock
                반환값: 실제 Performance instance
                작성 날짜: 2026/08/21
                """
                restored_order_ids = tuple(
                    trade.order_id for trade in restored_trades
                )
                trace_entry = start_trace_entry(
                    integration_trace,
                    message_id="3.3",
                    caller="TradeHistoryController",
                    receiver="Performance",
                    command_event_id=HISTORY_LOAD_COMMAND_ID,
                    state_version_before=state_version(),
                    related_id=restored_order_ids,
                )
                try:
                    performance = Performance(
                        restored_trades,
                        clock=clock,
                    )
                except Exception as error:
                    finish_trace_entry(
                        trace_entry,
                        state_version_after=state_version(),
                        error=error,
                    )
                    raise

                finish_trace_entry(
                    trace_entry,
                    state_version_after=state_version(),
                )
                return performance

            load_entry = start_trace_entry(
                integration_trace,
                message_id="3",
                caller="UIStateController",
                receiver="TradeHistoryController",
                command_event_id=HISTORY_LOAD_COMMAND_ID,
                state_version_before=state_version(),
                related_id=tuple(trade.order_id for trade in trades),
            )
            try:
                with patch.object(
                    controller_module,
                    "TradeHistory",
                    side_effect=build_trade_history,
                ), patch.object(
                    controller_module,
                    "Performance",
                    side_effect=build_performance,
                ):
                    loaded_history = controller.load_trade_history()
            except Exception as error:
                finish_trace_entry(
                    load_entry,
                    state_version_after=state_version(),
                    error=error,
                )
                raise

            publication_version[0] += 1
            finish_trace_entry(
                load_entry,
                state_version_after=state_version(),
            )

        assert_trace_contract(
            self,
            integration_trace,
            ("3", "3.1", "3.1.1", "3.2", "3.3"),
        )
        self.assertEqual(
            tuple(
                (
                    entry["state_version_before"],
                    entry["state_version_after"],
                )
                for entry in integration_trace
            ),
            ((0, 1), (0, 0), (0, 0), (0, 0), (0, 0)),
        )
        self.assertTrue(
            all(
                entry["command_event_id"] == HISTORY_LOAD_COMMAND_ID
                and entry["related_id"] == ("1", "2", "3")
                for entry in integration_trace
            )
        )

        self.assertIs(controller.trade_history, loaded_history)
        self.assertEqual(loaded_history.trades, trades)
        self.assertEqual(
            controller.performance.cumulative_return_rate,
            Decimal("-0.19980020"),
        )
        self.assertEqual(controller.performance.realized_pnl, Decimal("-0.40"))
        self.assertEqual(controller.performance.total_fee, Decimal("0.40"))
        self.assertEqual(
            repository.loaded_order_ids,
            frozenset({"1", "2", "3"}),
        )

    def test_controller_calls_repository_history_performance_in_order(self) -> None:
        """
        함수 이름: test_controller_calls_repository_history_performance_in_order()
        기능: 메시지 3.1~3.3이 repository, TradeHistory, Performance 순서인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        trace: list[str] = []
        trades = (make_trade(),)
        repository = TracingRepository(trades, trace)
        controller = TradeHistoryController(repository, clock=fixed_clock)
        trace.clear()

        def build_trade_history(restored_trades: tuple[Trade, ...]) -> TradeHistory:
            """
            함수 이름: build_trade_history()
            기능: domain history 생성 단계를 기록하고 실제 TradeHistory를 생성한다.
            인자: restored_trades -> repository가 반환한 Trade tuple
            반환값: 검증된 TradeHistory
            작성 날짜: 2026/08/21
            """
            trace.append("trade_history")
            return TradeHistory(restored_trades)

        def build_performance(
            restored_trades: tuple[Trade, ...],
            clock: Callable[[], datetime] | None,
        ) -> Performance:
            """
            함수 이름: build_performance()
            기능: performance 생성 단계를 기록하고 실제 Performance를 생성한다.
            인자: restored_trades -> TradeHistory가 보존한 Trade tuple
                clock -> account day를 결정할 주입 clock
            반환값: 복원된 Performance
            작성 날짜: 2026/08/21
            """
            trace.append("performance")
            return Performance(restored_trades, clock=clock)

        with patch.object(
            controller_module,
            "TradeHistory",
            side_effect=build_trade_history,
        ), patch.object(
            controller_module,
            "Performance",
            side_effect=build_performance,
        ):
            loaded_history = controller.load_trade_history()

        self.assertEqual(trace, ["repository", "trade_history", "performance"])
        self.assertEqual(loaded_history.trades, trades)

    def test_controller_keeps_previous_state_when_local_restore_fails(self) -> None:
        """
        함수 이름: test_controller_keeps_previous_state_when_local_restore_fails()
        기능: 새 Performance 완성 전 오류가 기존 history/performance를 교체하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        trace: list[str] = []
        repository = TracingRepository((make_trade(),), trace)
        controller = TradeHistoryController(repository, clock=fixed_clock)
        previous_history = controller.load_trade_history()
        previous_performance = controller.performance
        repository.trades = (make_trade(trade_id="next", order_id="2"),)

        with patch.object(
            controller_module,
            "Performance",
            side_effect=RuntimeError("controlled restore failure"),
        ):
            with self.assertRaises(RuntimeError):
                controller.load_trade_history()

        self.assertIs(controller.trade_history, previous_history)
        self.assertIs(controller.performance, previous_performance)


if __name__ == "__main__":
    unittest.main()
