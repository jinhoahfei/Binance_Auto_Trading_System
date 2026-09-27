"""공개 시세 FIFO와 application 장애 통지의 socket 지연 격리를 검증한다."""

from threading import Event, Thread
from time import monotonic
import json
import unittest
from unittest.mock import patch

from binance_auto_trader.adapters.binance.spot_websocket_client import (
    BinanceSpotWebSocketClient,
    _DisconnectNotifier,
)
from binance_auto_trader.adapters.binance.websocket_gateway import WebSocketGateway
from binance_auto_trader.domain.market import Interval
from tests.unit.binance.test_spot_websocket_client import _ScriptedSocketFactory
from tests.unit.market.test_kline_live_promotion import _create_kline_event


class StreamDispatchIsolationTests(unittest.TestCase):
    """
    클래스 이름: StreamDispatchIsolationTests
    기능: 느린 consumer·복구 통지·overflow·세대 교체가 socket 처리와 readiness를 지키는지 검사한다.
    작성 날짜: 2026/09/22
    """

    def _create_client(self, *, market_capacity: int = 8):
        """
        함수 이름: _create_client()
        기능: 네트워크 없이 실제 client callback을 실행하는 socket factory를 조립한다.
        인자: market_capacity -> 공개 시세 queue 상한
        반환값: 실제 client와 fake socket factory
        작성 날짜: 2026/09/22
        """
        factory = _ScriptedSocketFactory()
        client = BinanceSpotWebSocketClient("fake-key", "fake-secret", socket_factory=factory,
            startup_timeout_seconds=1, market_event_queue_capacity=market_capacity)
        return client, factory

    def test_public_fifo_returns_during_consumer_wait_and_preserves_close_open(self) -> None:
        """
        함수 이름: test_public_fifo_returns_during_consumer_wait_and_preserves_close_open()
        기능: 봉 종료·다음 OPEN을 합치거나 버리지 않고 수신 callback이 즉시 반환한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        client, factory = self._create_client()
        consumer_entered = Event()
        release_consumer = Event()
        received = []

        def consume(payload):
            """
            함수 이름: consume()
            기능: 첫 시세 처리를 지연시키며 frame FIFO를 관측한다.
            인자: payload -> 검증용 공개 frame
            반환값: 없음
            작성 날짜: 2026/09/22
            """
            received.append(json.loads(payload))
            consumer_entered.set()
            release_consumer.wait(2)

        subscription = client.subscribe_all_kline_streams(symbol="ETHUSDT", intervals=("1m",),
            on_message=consume, on_disconnect=lambda: None)
        frames = [{"sequence": 1, "closed": False}, {"sequence": 2, "closed": True},
                  {"sequence": 3, "closed": False}]
        try:
            factory.sockets[0].emit(frames[0])
            self.assertTrue(consumer_entered.wait(1))
            began = monotonic()
            for frame in frames[1:]:
                factory.sockets[0].emit(frame)
            self.assertLess(monotonic() - began, 0.2)
            self.assertFalse(subscription.caught_up)
            release_consumer.set()
            self.assertTrue(subscription.wait_until_caught_up(1))
            self.assertEqual(received, frames)
        finally:
            release_consumer.set()
            subscription.close()

    def test_overflow_closes_socket_before_blocked_notification_finishes(self) -> None:
        """
        함수 이름: test_overflow_closes_socket_before_blocked_notification_finishes()
        기능: consumer와 오류 통지가 모두 막혀도 overflow frame 반환·close·readiness 차단이 끝난다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        client, factory = self._create_client(market_capacity=1)
        consumer_entered = Event()
        release_consumer = Event()
        notification_entered = Event()
        release_notification = Event()
        notification_finished = Event()
        received = []
        notifications = []

        def consume(payload):
            """
            함수 이름: consume()
            기능: queue 한도를 검사할 동안 첫 시세 consumer를 멈춘다.
            인자: payload -> 공개 시세 frame
            반환값: 없음
            작성 날짜: 2026/09/22
            """
            received.append(json.loads(payload))
            consumer_entered.set()
            release_consumer.wait(2)

        def notify_disconnect():
            """
            함수 이름: notify_disconnect()
            기능: application 잠금 대기와 같은 장애 통지 지연을 재현한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/09/22
            """
            notifications.append(True)
            notification_entered.set()
            release_notification.wait(2)
            notification_finished.set()

        subscription = client.subscribe_all_kline_streams(symbol="ETHUSDT", intervals=("1m",),
            on_message=consume, on_disconnect=notify_disconnect)
        socket = factory.sockets[0]
        try:
            socket.emit({"sequence": 1})
            self.assertTrue(consumer_entered.wait(1))
            socket.emit({"sequence": 2})
            began = monotonic()
            socket.emit({"sequence": 3})
            self.assertLess(monotonic() - began, 0.2)
            self.assertTrue(socket._closed)
            self.assertFalse(subscription.connected)
            self.assertFalse(subscription.caught_up)
            self.assertTrue(notification_entered.wait(1))
            socket.fail_and_close()
            release_consumer.set()
            release_notification.set()
            self.assertTrue(notification_finished.wait(1))
            subscription._message_dispatcher._worker_thread.join(1)
            self.assertEqual(received, [{"sequence": 1}])
            self.assertEqual(notifications, [True])
        finally:
            release_consumer.set()
            release_notification.set()
            subscription.close()

    def test_gateway_market_readiness_closes_before_delivery_lock_is_released(self) -> None:
        """
        함수 이름: test_gateway_market_readiness_closes_before_delivery_lock_is_released()
        기능: 기존 delivery 잠금의 느린 consumer 때문에 오류 callback이 기다려도 시장 gate가 닫힌다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        client, factory = self._create_client()
        gateway = WebSocketGateway(client)
        consumer_entered = Event()
        release_consumer = Event()
        recovery_requested = Event()

        def consume(kline):
            """
            함수 이름: consume()
            기능: 실제 Gateway delivery 잠금을 보유한 시장 consumer를 지연시킨다.
            인자: kline -> 정규화 시세
            반환값: 없음
            작성 날짜: 2026/09/22
            """
            consumer_entered.set()
            release_consumer.wait(2)

        subscription = gateway.start_all_kline_buffering("ETHUSDT", (Interval.ONE_MINUTE,),
            reconciliation_required_callback=lambda reason: recovery_requested.set())
        gateway.promote_kline_buffer_to_live(consume, subscription)
        try:
            factory.sockets[0].emit(_create_kline_event(close="100", event_offset_milliseconds=1000))
            self.assertTrue(consumer_entered.wait(1))
            began = monotonic()
            factory.sockets[0].fail_and_close()
            self.assertLess(monotonic() - began, 0.2)
            self.assertFalse(gateway.kline_connected)
            self.assertFalse(gateway.kline_live_ready)
            self.assertFalse(recovery_requested.is_set())
            release_consumer.set()
            self.assertTrue(recovery_requested.wait(1))
        finally:
            release_consumer.set()
            subscription.close()

    def test_account_ready_closes_before_application_notification_finishes(self) -> None:
        """
        함수 이름: test_account_ready_closes_before_application_notification_finishes()
        기능: 계좌 복구 통지가 application 잠금을 기다려도 receive와 readiness를 지연하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        client, factory = self._create_client()
        notification_entered = Event()
        release_notification = Event()

        def request_recovery(reason):
            """
            함수 이름: request_recovery()
            기능: production application 잠금 대기와 같은 복구 통지 지연을 만든다.
            인자: reason -> 고정 장애 사유
            반환값: 없음
            작성 날짜: 2026/09/22
            """
            notification_entered.set()
            release_notification.wait(2)

        gateway = WebSocketGateway(client, account_snapshot_callback=lambda snapshot: None,
            reconciliation_required_callback=request_recovery)
        subscription = gateway.start_account_info_stream()
        try:
            self.assertTrue(gateway.account_ready)
            began = monotonic()
            factory.sockets[0].fail_and_close()
            self.assertLess(monotonic() - began, 0.2)
            self.assertFalse(gateway.account_connected)
            self.assertFalse(gateway.account_ready)
            self.assertTrue(notification_entered.wait(1))
            dispatcher_thread = subscription._transport_subscription._message_dispatcher._worker_thread
            dispatcher_thread.join(1)
            self.assertFalse(dispatcher_thread.is_alive())
        finally:
            release_notification.set()
            subscription.close()

    def test_delayed_old_generation_notification_does_not_close_new_market(self) -> None:
        """
        함수 이름: test_delayed_old_generation_notification_does_not_close_new_market()
        기능: 이전 세대의 늦은 장애 통지와 frame이 새 공개 시세 구독을 오염시키지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        client, factory = self._create_client()
        notification_entered = Event()
        release_notification = Event()
        notification_drained = Event()
        reconciliations = []

        def block_notification():
            """
            함수 이름: block_notification()
            기능: old-generation 통지를 client notifier queue에 대기시킨다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/09/22
            """
            notification_entered.set()
            release_notification.wait(2)

        client._disconnect_notifier.request_notification("account", block_notification)
        self.assertTrue(notification_entered.wait(1))
        gateway = WebSocketGateway(client)
        first = gateway.start_all_kline_buffering("ETHUSDT", (Interval.ONE_MINUTE,),
            reconciliation_required_callback=reconciliations.append)
        second = None
        try:
            factory.sockets[0].fail_and_close()
            second = gateway.start_all_kline_buffering("ETHUSDT", (Interval.ONE_MINUTE,),
                reconciliation_required_callback=reconciliations.append)
            observed = []
            gateway.promote_kline_buffer_to_live(observed.append, second)
            factory.sockets[0].emit("malformed stale payload")
            factory.sockets[1].emit(_create_kline_event(close="200", event_offset_milliseconds=1000))
            self.assertTrue(second._transport_subscription.wait_until_caught_up(1))
            client._disconnect_notifier.request_notification("account", notification_drained.set)
            release_notification.set()
            self.assertTrue(notification_drained.wait(1))
            self.assertTrue(gateway.kline_live_ready)
            self.assertEqual(len(observed), 1)
            self.assertEqual(reconciliations, [])
        finally:
            release_notification.set()
            first.close()
            if second is not None:
                second.close()

    def test_notification_storm_coalesces_to_two_streams_and_one_worker(self) -> None:
        """
        함수 이름: test_notification_storm_coalesces_to_two_streams_and_one_worker()
        기능: 통지 지연 중 반복 실패가 thread와 callback queue를 무한 증가시키지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        notifier = _DisconnectNotifier()
        notification_entered = Event()
        release_notification = Event()
        drained = Event()
        received = []

        def block_notification():
            """
            함수 이름: block_notification()
            기능: 하나의 active 통지를 유지해 후속 알림 병합을 검사한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/09/22
            """
            notification_entered.set()
            release_notification.wait(2)

        notifier.request_notification("market", block_notification)
        self.assertTrue(notification_entered.wait(1))
        worker = notifier._active_thread
        try:
            for index in range(100):
                notifier.request_notification("market", lambda value=index: received.append(value))
                notifier.request_notification("account", drained.set)
            self.assertIs(notifier._active_thread, worker)
            self.assertEqual(len(notifier._pending), 2)
            release_notification.set()
            self.assertTrue(drained.wait(1))
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(received, [99])
        finally:
            release_notification.set()

    def test_late_old_enqueue_preserves_new_market_and_account_recovery(self) -> None:
        """
        함수 이름: test_late_old_enqueue_preserves_new_market_and_account_recovery()
        기능: 과거 세대의 늦은 enqueue가 새 세대 장애의 복구 통지를 덮지 않게 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        for stream_name in ("market", "account"):
            with self.subTest(stream_name=stream_name):
                client, factory = self._create_client()
                notifier = client._disconnect_notifier
                blocker_entered = Event()
                release_blocker = Event()
                old_at_enqueue = Event()
                release_old_enqueue = Event()
                reconciliations = []

                def block_notification():
                    """
                    함수 이름: block_notification()
                    기능: 단일 notifier를 점유해 두 세대 통지의 enqueue 순서를 제어한다.
                    인자: 없음
                    반환값: 없음
                    작성 날짜: 2026/09/27
                    """
                    blocker_entered.set()
                    release_blocker.wait(2)

                blocking_stream = "account" if stream_name == "market" else "market"
                notifier.request_notification(blocking_stream, block_notification)
                self.assertTrue(blocker_entered.wait(1))
                original_request = notifier.request_notification
                stream_request_count = 0

                def delay_first_notification(name, callback, **arguments):
                    """
                    함수 이름: delay_first_notification()
                    기능: 과거 실패의 생성과 enqueue 사이에 새 세대의 실패를 삽입한다.
                    인자: name -> stream 이름, callback -> 복구 통지, arguments -> 구독 세대
                    반환값: 없음
                    작성 날짜: 2026/09/27
                    """
                    nonlocal stream_request_count
                    if name == stream_name:
                        stream_request_count += 1
                        if stream_request_count == 1:
                            old_at_enqueue.set()
                            release_old_enqueue.wait(2)
                    original_request(name, callback, **arguments)

                gateway = WebSocketGateway(
                    client,
                    account_snapshot_callback=lambda snapshot: None,
                    reconciliation_required_callback=reconciliations.append,
                )

                def start_subscription():
                    """
                    함수 이름: start_subscription()
                    기능: 검사 중인 stream의 실제 Gateway 구독 세대를 생성한다.
                    인자: 없음
                    반환값: 관리되는 구독
                    작성 날짜: 2026/09/27
                    """
                    if stream_name == "account":
                        return gateway.start_account_info_stream()
                    return gateway.start_all_kline_buffering(
                        "ETHUSDT", (Interval.ONE_MINUTE,),
                        reconciliation_required_callback=reconciliations.append,
                    )

                first = start_subscription()
                second = None
                old_failure = Thread(target=factory.sockets[0].fail_and_close)
                try:
                    with patch.object(notifier, "request_notification", side_effect=delay_first_notification):
                        old_failure.start()
                        self.assertTrue(old_at_enqueue.wait(1))
                        second = start_subscription()
                        factory.sockets[1].fail_and_close()
                        self.assertFalse(second.connected)
                        release_old_enqueue.set()
                        old_failure.join(1)
                        self.assertFalse(old_failure.is_alive())
                    worker = notifier._active_thread
                    release_blocker.set()
                    worker.join(1)
                    self.assertFalse(worker.is_alive())
                    expected_reason = (
                        "kline_stream_disconnected" if stream_name == "market"
                        else "account_stream_disconnected"
                    )
                    self.assertEqual(reconciliations, [expected_reason])
                finally:
                    release_old_enqueue.set()
                    release_blocker.set()
                    if old_failure.ident is not None:
                        old_failure.join(1)
                    first.close()
                    if second is not None:
                        second.close()
