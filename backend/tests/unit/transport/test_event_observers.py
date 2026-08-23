"""Trade history application publication이 loopback event로 변환되는 계약을 검증한다."""

import unittest

from binance_auto_trader.transport import (
    BackendEventStream,
    create_trade_history_update_observer,
)

from tests.unit.transport.test_contracts import _create_ready_runtime


class TradeHistoryEventObserverTests(unittest.TestCase):
    """
    클래스 이름: TradeHistoryEventObserverTests
    기능: durable Trade와 authoritative Performance의 순서·wire 불변식을 검증한다.
    작성 날짜: 2026/08/23
    """

    def test_trade_history_observer_publishes_order_then_performance(
        self,
    ) -> None:
        """
        함수 이름: test_trade_history_observer_publishes_order_then_performance()
        기능: 신규 체결이 주문 event 뒤 전체 성과 event를 연속 발행하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        # 스냅샷 fixture의 실제 wire mapping 필드를 그대로 observer에 주입한다.
        runtime = _create_ready_runtime()
        trade = runtime.trade_history_controller.trade_history.trades[0]
        performance = runtime.trade_history_controller.performance
        event_stream = BackendEventStream()
        observer = create_trade_history_update_observer(event_stream)

        published_events = observer(trade, performance)

        self.assertEqual(
            tuple(event.event_type for event in published_events),
            ("ORDER_EXECUTED", "PERFORMANCE_UPDATED"),
        )
        self.assertEqual(
            tuple(event.sequence for event in published_events),
            (1, 2),
        )  # 두 event 사이에 sequence gap이 없어야 한다.
        self.assertEqual(
            published_events[0].payload["trade"]["executed_amount"],
            "432.150",
        )
        self.assertEqual(
            published_events[1].payload["performance"]["daily_fee"],
            "0.43215",
        )

    def test_invalid_performance_does_not_partially_publish_order_event(
        self,
    ) -> None:
        """
        함수 이름: test_invalid_performance_does_not_partially_publish_order_event()
        기능: 두 번째 DTO mapping 실패 전에 ORDER event도 공개되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        runtime = _create_ready_runtime()
        trade = runtime.trade_history_controller.trade_history.trades[0]
        event_stream = BackendEventStream()
        observer = create_trade_history_update_observer(event_stream)

        # Performance 계약 위반을 두 번째 payload에 주입해 observer의 preflight mapping을 검사한다.
        with self.assertRaises((AttributeError, TypeError)):
            observer(trade, object())

        self.assertEqual(event_stream.last_sequence, 0)
        self.assertEqual(event_stream.replay_after(0).events, ())


if __name__ == "__main__":
    unittest.main()
