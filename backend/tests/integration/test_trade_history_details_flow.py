"""Communication Case 3 상세 조회의 backend operation과 UI 소유 message 추적성을 검증한다."""

from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from typing import cast
import unittest
from unittest.mock import patch

from binance_auto_trader.application import (
    TradeDetailsResult,
    TradeHistoryController,
)
from binance_auto_trader.domain.history import (
    HistoryPeriod,
    Performance,
    Trade,
    TradeHistory,
    TradeHistoryQuery,
    TradeSide,
)
from binance_auto_trader.domain.trading import Account

from tests.integration.trace_helpers import (
    TraceEntry,
    assert_trace_contract,
    finish_trace_entry,
    start_trace_entry,
)
from tests.unit.history.factories import make_trade


CURRENT_TIME = datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc)
DETAILS_QUERY_COMMAND_ID = "trade-history-details-20260823-001"
CASE_3_MESSAGE_IDS = frozenset(
    {
        "1",
        "1.1",
        "1.1.1",
        "1.1.2",
        "1.1.2.1",
        "1.1.2.2",
        "1.1.2.3",
        "1.1.2.4",
        "1.1.3",
        "2",
        "2.1",
        "2.1.1",
        "2.1.2",
        "2.1.2.1",
        "2.1.2.2",
        "2.1.3",
    }
)
UI_OWNED_CASE_3_MESSAGE_IDS = frozenset(
    {
        "1",
        "1.1",
        "1.1.1",
        "1.1.2",
        "1.1.3",
        "2",
        "2.1",
        "2.1.1",
        "2.1.2",
        "2.1.3",
    }
)  # Facade trace test가 화면 intent·state·display 메시지를 검증한다.


def fixed_clock() -> datetime:
    """
    함수 이름: fixed_clock()
    기능: Case 3 query의 KST 날짜를 2026-08-23으로 고정한다.
    인자: 없음
    반환값: 고정 timezone-aware UTC datetime
    작성 날짜: 2026/08/23
    """
    return CURRENT_TIME  # Query와 Performance가 같은 account day를 사용한다.


class _HistoryRepository:
    """
    클래스 이름: _HistoryRepository
    기능: Case 3 조회에 하나의 durable Trade를 제공하는 in-memory port이다.
    작성 날짜: 2026/08/23
    """

    def __init__(self, trades: tuple[Trade, ...]) -> None:
        """
        함수 이름: __init__()
        기능: startup load에 반환할 immutable Trade tuple을 보존한다.
        인자: trades -> 복원할 canonical Trade tuple
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        self._trades = trades  # Query test가 repository 재계산 없이 published history를 읽게 한다.

    def get_trade_history(self) -> tuple[Trade, ...]:
        """
        함수 이름: get_trade_history()
        기능: Controller startup에 고정 durable Trade tuple을 반환한다.
        인자: 없음
        반환값: canonical Trade tuple
        작성 날짜: 2026/08/23
        """
        return self._trades  # 상세 조회 중에는 다시 호출되지 않아야 한다.

    def save_this_trade_by_order_id(
        self,
        order_id: int | str,
        trade: Trade,
    ) -> None:
        """
        함수 이름: save_this_trade_by_order_id()
        기능: 조회 전용 test port의 record Protocol shape를 충족한다.
        인자: order_id -> 저장할 주문 ID
            trade -> 저장할 canonical Trade
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        raise AssertionError(
            "detail query must not save trades"
        )  # Read-only Case 3에서 persistence write를 즉시 검출한다.


class TradeHistoryDetailsFlowTests(unittest.TestCase):
    """
    클래스 이름: TradeHistoryDetailsFlowTests
    기능: Case 3 backend nested operation 순서와 UI trace 추적성 매트릭스를 검증한다.
    작성 날짜: 2026/08/23
    """

    def test_show_trade_details_and_filter_backend_message_trace(self) -> None:
        """
        함수 이름: test_show_trade_details_and_filter_backend_message_trace()
        기능: 초기 상세 조회와 filter 변경의 Controller·Query·domain 호출 trace를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        trade = make_trade(executed_at=CURRENT_TIME)
        repository = _HistoryRepository((trade,))
        controller = TradeHistoryController(
            repository,
            clock=fixed_clock,
            account=Account(),
        )
        controller.load_trade_history()
        integration_trace: list[TraceEntry] = []
        current_parent_message_id = [""]
        state_version = len(controller.trade_history.trades)

        def trace_operation(
            message_id: str,
            receiver: str,
            operation: Callable[[], object],
        ) -> object:
            """
            함수 이름: trace_operation()
            기능: nested backend operation의 전후 version과 성공·실패를 구조화해 기록한다.
            인자: message_id -> Communication nested 메시지 ID
                receiver -> operation을 소유한 domain 객체
                operation -> 실제 실행할 backend operation
            반환값: 실제 operation 반환값
            작성 날짜: 2026/08/23
            """
            trace_entry = start_trace_entry(
                integration_trace,
                message_id=message_id,
                caller="TradeHistoryController",
                receiver=receiver,
                command_event_id=DETAILS_QUERY_COMMAND_ID,
                state_version_before=state_version,
                related_id=current_parent_message_id[0],
            )
            try:
                result = operation()
            except Exception as error:
                finish_trace_entry(
                    trace_entry,
                    state_version_after=state_version,
                    error=error,
                )
                raise

            finish_trace_entry(
                trace_entry,
                state_version_after=state_version,
            )
            return result  # Query는 authoritative history version을 변경하지 않는다.

        original_build_query = TradeHistoryController._build_trade_history_query
        original_find = TradeHistory.find
        original_get_holdings = Account.get_holdings
        original_get_performance = Performance.get_performance

        def build_query(
            current_controller: TradeHistoryController,
            period: HistoryPeriod,
            side: TradeSide,
        ) -> TradeHistoryQuery:
            """
            함수 이름: build_query()
            기능: period와 side를 하나의 TradeHistoryQuery로 만드는 메시지를 기록한다.
            인자: current_controller -> 실제 조회 Controller
                period -> canonical HistoryPeriod
                side -> canonical TradeSide
            반환값: 실제 inclusive TradeHistoryQuery
            작성 날짜: 2026/08/23
            """
            return cast(
                TradeHistoryQuery,
                trace_operation(
                    f"{current_parent_message_id[0]}.1",
                    "TradeHistoryQuery",
                    lambda: original_build_query(
                        current_controller,
                        period,
                        side,
                    ),
                ),
            )

        def find_trades(
            history: TradeHistory,
            query: TradeHistoryQuery,
        ) -> tuple[Trade, ...]:
            """
            함수 이름: find_trades()
            기능: published TradeHistory의 inclusive filter 메시지를 기록한다.
            인자: history -> 조회할 authoritative TradeHistory
                query -> 결합된 기간·방향 query
            반환값: 실제 filtered Trade tuple
            작성 날짜: 2026/08/23
            """
            return cast(
                tuple[Trade, ...],
                trace_operation(
                    f"{current_parent_message_id[0]}.2",
                    "TradeHistory",
                    lambda: original_find(history, query),
                ),
            )

        def get_holdings(account: Account, asset: str = "ETH") -> Decimal:
            """
            함수 이름: get_holdings()
            기능: 최초 상세 조회의 authoritative ETH holdings 메시지를 기록한다.
            인자: account -> shared authoritative Account
                asset -> 조회할 asset symbol
            반환값: 실제 Account holdings Decimal
            작성 날짜: 2026/08/23
            """
            # Filter parent에서 호출되면 명세에 없는 summary 재조회이므로 즉시 실패시킨다.
            if current_parent_message_id[0] != "1.1.2":
                raise AssertionError(
                    "filter query must preserve the existing Account summary"
                )
            return cast(
                Decimal,
                trace_operation(
                    "1.1.2.3",
                    "Account",
                    lambda: original_get_holdings(account, asset),
                ),
            )

        def get_performance(performance: Performance) -> Performance:
            """
            함수 이름: get_performance()
            기능: 최초 상세 조회의 unfiltered Performance 메시지를 기록한다.
            인자: performance -> authoritative Performance
            반환값: account day가 갱신된 실제 Performance
            작성 날짜: 2026/08/23
            """
            # Filter parent에서 호출되면 명세에 없는 summary 재조회이므로 즉시 실패시킨다.
            if current_parent_message_id[0] != "1.1.2":
                raise AssertionError(
                    "filter query must preserve the existing Performance summary"
                )
            return cast(
                Performance,
                trace_operation(
                    "1.1.2.4",
                    "Performance",
                    lambda: original_get_performance(performance),
                ),
            )

        def get_details(
            parent_message_id: str,
            period: HistoryPeriod,
            side: TradeSide,
        ) -> TradeDetailsResult:
            """
            함수 이름: get_details()
            기능: UIStateController의 초기 또는 filter 상세 조회 호출을 구조화해 기록한다.
            인자: parent_message_id -> 1.1.2 또는 2.1.2
                period -> UI가 전달한 canonical period
                side -> UI가 전달한 canonical side
            반환값: 실제 TradeDetailsResult
            작성 날짜: 2026/08/23
            """
            current_parent_message_id[0] = parent_message_id
            trace_entry = start_trace_entry(
                integration_trace,
                message_id=parent_message_id,
                caller="UIStateController",
                receiver="TradeHistoryController",
                command_event_id=DETAILS_QUERY_COMMAND_ID,
                state_version_before=state_version,
                related_id=f"{period.value}:{side.value}",
            )
            try:
                result = controller.get_trade_details(period, side)
            except Exception as error:
                finish_trace_entry(
                    trace_entry,
                    state_version_after=state_version,
                    error=error,
                )
                raise

            finish_trace_entry(
                trace_entry,
                state_version_after=state_version,
            )
            return result  # Read-only query의 publication version은 전후가 같다.

        # 실제 Controller 내부 operation을 감싸 Case 3 nested 순서를 관측한다.
        with patch.object(
            TradeHistoryController,
            "_build_trade_history_query",
            autospec=True,
            side_effect=build_query,
        ), patch.object(
            TradeHistory,
            "find",
            autospec=True,
            side_effect=find_trades,
        ), patch.object(
            Account,
            "get_holdings",
            autospec=True,
            side_effect=get_holdings,
        ), patch.object(
            Performance,
            "get_performance",
            autospec=True,
            side_effect=get_performance,
        ):
            initial_result = get_details(
                "1.1.2",
                HistoryPeriod.TODAY,
                TradeSide.ALL,
            )
            filtered_result = get_details(
                "2.1.2",
                HistoryPeriod.LAST_30_DAYS,
                TradeSide.BUY,
            )

        expected_backend_message_ids = (
            "1.1.2",
            "1.1.2.1",
            "1.1.2.2",
            "1.1.2.3",
            "1.1.2.4",
            "2.1.2",
            "2.1.2.1",
            "2.1.2.2",
        )
        assert_trace_contract(
            self,
            integration_trace,
            expected_backend_message_ids,
        )
        self.assertEqual(
            UI_OWNED_CASE_3_MESSAGE_IDS
            | frozenset(expected_backend_message_ids),
            CASE_3_MESSAGE_IDS,
        )  # UI facade trace와 backend nested trace의 합집합이 Case 3 전체다.
        self.assertEqual(initial_result.rows, (trade,))
        self.assertEqual(filtered_result.rows, (trade,))
        self.assertTrue(
            all(
                entry["state_version_before"]
                == entry["state_version_after"]
                == state_version
                for entry in integration_trace
            )
        )


if __name__ == "__main__":
    unittest.main()
