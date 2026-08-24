"""Production process의 post-CLOSED FD5 acknowledgement latch를 검증한다."""

import os
import select
from threading import Event, RLock, Thread
from time import monotonic, sleep
from types import SimpleNamespace
import unittest

from binance_auto_trader.transport.app import _wait_for_safe_process_ack


class _ControlledShutdownServer:
    """
    클래스 이름: _ControlledShutdownServer
    기능: ack latch 이후 handler 완료 property 조회를 deterministic Event로 관찰한다.
    작성 날짜: 2026/08/24
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 아직 handler가 끝나지 않은 상태와 property 조회 Event를 준비한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # Handler 완료 플래그와 ack 관찰 barrier를 하나의 fixture state로 준비한다.
        self.handler_complete = False
        self.ack_latched = Event()

    @property
    def shutdown_response_flushed(self) -> bool:
        """
        함수 이름: shutdown_response_flushed()
        기능: ack latch 뒤 process waiter의 완료조건 조회를 기록하고 제어 값을 반환한다.
        인자: 없음
        반환값: test가 설정한 handler 완료 여부
        작성 날짜: 2026/08/24
        """
        self.ack_latched.set()
        return self.handler_complete  # Event set을 먼저 해 barrier 관찰 순서를 고정한다.


class ProcessAcknowledgementTests(unittest.TestCase):
    """
    클래스 이름: ProcessAcknowledgementTests
    기능: early byte 폐기와 response handler 완료 전 post-CLOSED ack 보존을 검증한다.
    작성 날짜: 2026/08/24
    """

    def test_post_closed_ack_is_latched_until_handler_finishes(self) -> None:
        """
        함수 이름: test_post_closed_ack_is_latched_until_handler_finishes()
        기능: CLOSED 뒤 ack가 active handler 완료 전에 와도 EOF에 빠지지 않고 나중에 반환하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        runtime = SimpleNamespace(
            application_lock=RLock(),
            state=SimpleNamespace(closed=True),
        )
        server = _ControlledShutdownServer()
        stop_read_fd, stop_write_fd = os.pipe()
        waiter_thread = Thread(
            target=_wait_for_safe_process_ack,
            args=(runtime, server, stop_read_fd),
            daemon=True,
        )
        waiter_thread.start()

        os.write(stop_write_fd, b"A")
        os.close(stop_write_fd)  # Handler active-count가 남은 동안 pipe는 EOF에 도달한다.
        self.assertTrue(server.ack_latched.wait(timeout=1.0))
        self.assertTrue(waiter_thread.is_alive())

        server.handler_complete = True
        waiter_thread.join(timeout=1.0)
        self.assertFalse(waiter_thread.is_alive())
        os.close(stop_read_fd)

    def test_ready_byte_is_discarded_and_requires_new_closed_ack(self) -> None:
        """
        함수 이름: test_ready_byte_is_discarded_and_requires_new_closed_ack()
        기능: READY에서 읽은 byte를 저장하지 않고 CLOSED 뒤 새 byte를 요구하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        runtime = SimpleNamespace(
            application_lock=RLock(),
            state=SimpleNamespace(closed=False),
        )
        server = SimpleNamespace(shutdown_response_flushed=True)
        stop_read_fd, stop_write_fd = os.pipe()
        waiter_thread = Thread(
            target=_wait_for_safe_process_ack,
            args=(runtime, server, stop_read_fd),
            daemon=True,
        )
        waiter_thread.start()

        os.write(stop_write_fd, b"E")
        drain_deadline = monotonic() + 1.0
        while monotonic() < drain_deadline:
            readable_descriptors, _, _ = select.select(
                (stop_read_fd,),
                (),
                (),
                0.01,
            )
            if not readable_descriptors:
                break
        self.assertEqual(readable_descriptors, [])  # READY byte가 실제로 소비된 뒤 CLOSED를 게시한다.
        with runtime.application_lock:
            runtime.state = SimpleNamespace(closed=True)
        sleep(0.1)
        self.assertTrue(waiter_thread.is_alive())

        os.write(stop_write_fd, b"A")
        os.close(stop_write_fd)
        waiter_thread.join(timeout=1.0)
        self.assertFalse(waiter_thread.is_alive())
        os.close(stop_read_fd)


if __name__ == "__main__":
    unittest.main()
