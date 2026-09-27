"""구독 정리의 실패 보존·완료 확인·재시도를 외부 연결 없이 검증한다."""

import unittest
from unittest.mock import Mock, patch

from binance_auto_trader.adapters.binance.request_deadline import (
    RequestDeadlineExceeded, deadline_lock, request_deadline_scope,
)
from binance_auto_trader.adapters.binance.spot_websocket_client import _SocketSubscription
from binance_auto_trader.adapters.binance.websocket_gateway import _ManagedSubscription


class SubscriptionCleanupTests(unittest.TestCase):
    """
    클래스 이름: SubscriptionCleanupTests
    기능: 정리 요청만으로 완료를 추측하지 않고 실패한 자원만 재확인하는지 검증한다.
    작성 날짜: 2026/09/27
    """

    def test_managed_subscription_retries_failed_transport_without_duplicate_notifications(self):
        """
        함수 이름: test_managed_subscription_retries_failed_transport_without_duplicate_notifications()
        기능: 닫기 요청 후 readiness를 막으면서 실제 transport 실패는 재시도한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        transport, closing, closed = Mock(), Mock(), Mock()
        transport.close.side_effect = [OSError("synthetic close"), None]
        subscription = _ManagedSubscription(transport, closed, on_closing=closing)
        with self.assertRaises(OSError):
            subscription.close()
        self.assertFalse(subscription.connected)
        subscription.close()
        subscription.close()
        self.assertEqual(transport.close.call_count, 2)
        closing.assert_called_once_with()
        closed.assert_called_once_with()

    def test_dispatcher_failure_still_joins_socket_and_retains_failed_cleanup(self):
        """
        함수 이름: test_dispatcher_failure_still_joins_socket_and_retains_failed_cleanup()
        기능: 한 자원 실패에도 나머지를 회수하고 성공한 socket close를 반복하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        socket, worker, dispatcher = Mock(), Mock(), Mock()
        worker.is_alive.return_value = False
        dispatcher.close.side_effect = [TimeoutError("busy consumer"), None]
        subscription = _SocketSubscription(socket, Mock(), worker, dispatcher)
        with self.assertRaises(TimeoutError):
            subscription.close()
        worker.join.assert_called_once()
        subscription.close()
        subscription.close()
        socket.close.assert_called_once_with()
        self.assertEqual(dispatcher.close.call_count, 2)
        self.assertTrue(subscription._close_completed)

    def test_notification_failure_retries_only_unfinished_step(self):
        """
        함수 이름: test_notification_failure_retries_only_unfinished_step()
        기능: 종료 통지 실패를 재시도해도 이미 닫힌 transport는 다시 닫지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        transport, closing, closed = Mock(), Mock(), Mock()
        closing.side_effect = [RuntimeError("closing notification"), None]
        closed.side_effect = [RuntimeError("closed notification"), None]
        subscription = _ManagedSubscription(transport, closed, on_closing=closing)
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                subscription.close()
        subscription.close()
        transport.close.assert_called_once_with()
        self.assertEqual(closing.call_count, 2)
        self.assertEqual(closed.call_count, 2)

    def test_socket_close_false_or_exception_is_retried_until_worker_exits(self):
        """
        함수 이름: test_socket_close_false_or_exception_is_retried_until_worker_exits()
        기능: 명시적 False나 예외 뒤 handle을 유지하고 worker 종료까지 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        socket, worker = Mock(), Mock()
        socket.close.side_effect = [False, OSError("socket busy"), None]
        worker.is_alive.side_effect = [True, True, False]
        subscription = _SocketSubscription(socket, Mock(), worker)
        for error_type in (TimeoutError, OSError):
            with self.assertRaises(error_type):
                subscription.close()
            self.assertFalse(subscription._close_completed)
        subscription.close()
        subscription.close()
        self.assertEqual(socket.close.call_count, 3)

    def test_lock_wait_uses_remaining_budget_and_never_releases_unowned_lock(self):
        """
        함수 이름: test_lock_wait_uses_remaining_budget_and_never_releases_unowned_lock()
        기능: 잠금 획득 실패와 획득 후 예외의 소유권 정리가 정확한지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        lock = Mock()
        with patch("binance_auto_trader.adapters.binance.request_deadline.monotonic", return_value=100) as clock:
            with request_deadline_scope(120):
                clock.return_value = 215
                lock.acquire.return_value = False
                with self.assertRaises(RequestDeadlineExceeded):
                    with deadline_lock(lock):
                        self.fail("unowned lock admitted")
                lock.acquire.assert_called_once_with(timeout=5)
                lock.release.assert_not_called()
                lock.acquire.return_value = True
                with self.assertRaises(ValueError):
                    with deadline_lock(lock):
                        raise ValueError("synthetic operation failure")
                lock.release.assert_called_once_with()
