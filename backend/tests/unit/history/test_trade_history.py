"""TradeHistory의 order idempotency와 KST inclusive query를 검증한다."""

import unittest
from dataclasses import replace
from datetime import date, datetime, timezone

from binance_auto_trader.domain.history import (
    OrderHistoryConflictError,
    TradeHistory,
    TradeHistoryQuery,
    TradeSide,
)
from binance_auto_trader.domain.trading.states import OrderSide

from tests.unit.history.factories import make_trade


class TradeHistoryTests(unittest.TestCase):
    """
    클래스 이름: TradeHistoryTests
    기능: TradeHistory가 canonical 순서, 중복 규칙과 KST 조회를 지키는지 테스트한다.
    작성 날짜: 2026/08/21
    """

    def test_same_order_and_content_is_idempotent_no_op(self) -> None:
        """
        함수 이름: test_same_order_and_content_is_idempotent_no_op()
        기능: 같은 order ID와 동일 Trade의 생성·추가 중복이 한 건만 남는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        trade = make_trade()
        history = TradeHistory((trade, trade))

        history.add_trade(trade)

        self.assertEqual(history.trades, (trade,))

    def test_same_order_with_different_content_raises_conflict(self) -> None:
        """
        함수 이름: test_same_order_with_different_content_raises_conflict()
        기능: 동일 order ID의 다른 canonical 내용이 typed conflict로 실패하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        original_trade = make_trade()
        conflicting_trade = replace(original_trade, trade_id="trade-conflict")
        history = TradeHistory((original_trade,))

        with self.assertRaises(OrderHistoryConflictError) as captured_error:
            history.add_trade(conflicting_trade)

        self.assertEqual(captured_error.exception.code, "ORDER_HISTORY_CONFLICT")
        self.assertEqual(history.trades, (original_trade,))

    def test_find_uses_kst_midnight_and_inclusive_date_bounds(self) -> None:
        """
        함수 이름: test_find_uses_kst_midnight_and_inclusive_date_bounds()
        기능: 15:00 UTC KST 자정 경계와 시작·종료 날짜의 양끝 포함을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        before_midnight = make_trade(
            trade_id="trade-before",
            order_id="1",
            executed_at=datetime(2026, 8, 20, 14, 59, 59, tzinfo=timezone.utc),
        )
        at_midnight = make_trade(
            trade_id="trade-at",
            order_id="2",
            executed_at=datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc),
            side=OrderSide.SELL,
        )
        next_day = make_trade(
            trade_id="trade-next",
            order_id="3",
            executed_at=datetime(2026, 8, 21, 15, 0, tzinfo=timezone.utc),
        )
        history = TradeHistory((before_midnight, at_midnight, next_day))

        august_twentieth = history.find(
            TradeHistoryQuery(date(2026, 8, 20), date(2026, 8, 20))
        )
        through_august_twenty_first = history.find(
            TradeHistoryQuery(date(2026, 8, 20), date(2026, 8, 21))
        )

        self.assertEqual(august_twentieth, (before_midnight,))
        self.assertEqual(
            through_august_twenty_first,
            (before_midnight, at_midnight),
        )

    def test_find_filters_side_without_reordering(self) -> None:
        """
        함수 이름: test_find_filters_side_without_reordering()
        기능: side 조건이 날짜 조건에 맞는 거래의 저장 순서를 유지하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        first_buy = make_trade(trade_id="buy-1", order_id="1")
        sell = make_trade(
            trade_id="sell-1",
            order_id="2",
            side=OrderSide.SELL,
        )
        second_buy = make_trade(trade_id="buy-2", order_id="3")
        history = TradeHistory((first_buy, sell, second_buy))
        query_date = date(2026, 8, 21)

        buy_rows = history.find(
            TradeHistoryQuery(query_date, query_date, TradeSide.BUY)
        )
        sell_rows = history.find(
            TradeHistoryQuery(query_date, query_date, TradeSide.SELL)
        )

        self.assertEqual(buy_rows, (first_buy, second_buy))
        self.assertEqual(sell_rows, (sell,))

    def test_query_rejects_datetime_string_side_and_reverse_range(self) -> None:
        """
        함수 이름: test_query_rejects_datetime_string_side_and_reverse_range()
        기능: query가 순수 date, canonical side와 정방향 범위만 허용하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        with self.assertRaises(TypeError):
            TradeHistoryQuery(
                datetime(2026, 8, 21, tzinfo=timezone.utc),
                date(2026, 8, 21),
            )
        with self.assertRaises(TypeError):
            TradeHistoryQuery(
                date(2026, 8, 21),
                date(2026, 8, 21),
                "ALL",
            )
        with self.assertRaises(ValueError):
            TradeHistoryQuery(date(2026, 8, 22), date(2026, 8, 21))


if __name__ == "__main__":
    unittest.main()
