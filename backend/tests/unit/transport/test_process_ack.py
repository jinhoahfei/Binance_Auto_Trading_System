"""Production process의 post-CLOSED FD5 acknowledgement latch를 검증한다."""

import os
import select
from threading import Event, RLock, Thread
from time import monotonic, sleep
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from binance_auto_trader.transport.app import (
    _enforce_parent_stop_pipe_eof_safety,
    _wait_for_safe_process_ack,
)


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


class _ParentLossTradingController:
    """
    클래스 이름: _ParentLossTradingController
    기능: parent EOF가 유일하게 ownership ambiguity operation을 호출하는지 기록한다.
    작성 날짜: 2026/08/25
    """

    def __init__(self, calls: list[str], *, failures: int = 0) -> None:
        """
        함수 이름: __init__()
        기능: 공유 호출 기록과 성공 전 failure 횟수를 보존한다.
        인자: calls -> ordered safety operation 기록
            failures -> 성공 전에 발생시킬 예외 횟수
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        self._calls = calls
        self._failures = failures

    def mark_process_ownership_ambiguous(self, reason: str) -> None:
        """
        함수 이름: mark_process_ownership_ambiguous()
        기능: exact parent EOF reason을 기록하고 설정된 횟수만큼 실패한다.
        인자: reason -> transport가 전달한 stable ownership loss reason
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        # 호출 순서를 먼저 남기고 아직 남은 주입 failure가 있으면 해당 시도만 실패시킨다.
        self._calls.append(f"mark:{reason}")
        if self._failures > 0:
            self._failures -= 1
            raise RuntimeError("injected ownership gate failure")


class _ParentLossHistoryController:
    """
    클래스 이름: _ParentLossHistoryController
    기능: parent EOF durability barrier의 호출과 retry를 기록한다.
    작성 날짜: 2026/08/25
    """

    def __init__(self, calls: list[str], *, failures: int = 0) -> None:
        """
        함수 이름: __init__()
        기능: 공유 호출 기록과 성공 전 flush failure 횟수를 보존한다.
        인자: calls -> ordered safety operation 기록
            failures -> 성공 전에 발생시킬 예외 횟수
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        self._calls = calls
        self._failures = failures

    def flush_durable_state(self) -> None:
        """
        함수 이름: flush_durable_state()
        기능: fsync 장벽 시도를 기록하고 설정된 횟수만큼 실패한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        # Flush 시도를 기록한 뒤 설정된 횟수 동안만 내구성 오류를 주입한다.
        self._calls.append("flush")
        if self._failures > 0:
            self._failures -= 1
            raise RuntimeError("injected durable flush failure")


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

    def test_parent_eof_marks_ownership_then_flushes_without_other_effects(
        self,
    ) -> None:
        """
        함수 이름: test_parent_eof_marks_ownership_then_flushes_without_other_effects()
        기능: parent EOF safety가 ownership gate와 durable flush만 exact 순서로 실행하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        calls: list[str] = []
        runtime = SimpleNamespace(
            application_lock=RLock(),
            trading_controller=_ParentLossTradingController(calls),
            trade_history_controller=_ParentLossHistoryController(calls),
        )

        result = _enforce_parent_stop_pipe_eof_safety(
            runtime,
            ownership_ambiguity_marked=False,
            durable_flush_completed=False,
        )

        self.assertEqual(result, (True, True))
        self.assertEqual(
            calls,
            ["mark:parent_stop_pipe_eof", "flush"],
        )  # Cancel, kill, reorder, liquidation operation은 호출 목록에 들어갈 경로가 없다.

    def test_waiter_enforces_parent_eof_before_entering_retention_loop(
        self,
    ) -> None:
        """
        함수 이름: test_waiter_enforces_parent_eof_before_entering_retention_loop()
        기능: FD5 EOF를 읽은 같은 iteration에서 안전 경계를 적용한 뒤 process 유지 loop로 가는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        calls: list[str] = []
        runtime = SimpleNamespace(
            application_lock=RLock(),
            state=SimpleNamespace(closed=False),
            trading_controller=_ParentLossTradingController(calls),
            trade_history_controller=_ParentLossHistoryController(calls),
        )
        server = SimpleNamespace(shutdown_response_flushed=False)
        stop_read_fd, stop_write_fd = os.pipe()
        os.close(stop_write_fd)

        # Retention sleep을 test sentinel로 바꿔 무한 listener 정책의 첫 iteration만 결정론적으로 관찰한다.
        with patch(
            "binance_auto_trader.transport.app.sleep",
            side_effect=RuntimeError("retention loop reached"),
        ):
            with self.assertRaisesRegex(RuntimeError, "retention loop reached"):
                _wait_for_safe_process_ack(runtime, server, stop_read_fd)

        os.close(stop_read_fd)
        self.assertEqual(
            calls,
            ["mark:parent_stop_pipe_eof", "flush"],
        )  # Waiter는 EOF를 정상 exit acknowledgement로 반환하지 않는다.

    def test_parent_eof_retries_failed_fail_closed_boundaries_only(self) -> None:
        """
        함수 이름: test_parent_eof_retries_failed_fail_closed_boundaries_only()
        기능: gate와 flush 실패를 process exit로 바꾸지 않고 성공 후에는 반복하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        calls: list[str] = []
        runtime = SimpleNamespace(
            application_lock=RLock(),
            trading_controller=_ParentLossTradingController(calls, failures=1),
            trade_history_controller=_ParentLossHistoryController(calls, failures=1),
        )

        first_result = _enforce_parent_stop_pipe_eof_safety(
            runtime,
            ownership_ambiguity_marked=False,
            durable_flush_completed=False,
        )
        second_result = _enforce_parent_stop_pipe_eof_safety(
            runtime,
            ownership_ambiguity_marked=first_result[0],
            durable_flush_completed=first_result[1],
        )
        third_result = _enforce_parent_stop_pipe_eof_safety(
            runtime,
            ownership_ambiguity_marked=second_result[0],
            durable_flush_completed=second_result[1],
        )

        self.assertEqual(first_result, (False, False))
        self.assertEqual(second_result, (True, True))
        self.assertEqual(third_result, (True, True))
        self.assertEqual(
            calls,
            [
                "mark:parent_stop_pipe_eof",
                "flush",
                "mark:parent_stop_pipe_eof",
                "flush",
            ],
        )  # 성공한 operation은 세 번째 iteration에서 중복 실행하지 않는다.

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
