"""운영 진단 queue의 지연 격리·순서·용량·종료 상한을 검증한다."""

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, RLock, get_ident
from time import monotonic
import unittest
from unittest.mock import patch

from binance_auto_trader.application.runtime_diagnostics import RuntimeDiagnostics
from binance_auto_trader.bootstrap.application import _TradingEventRuntimeWorker
from binance_auto_trader.bootstrap.diagnostics import create_runtime_diagnostics


class AsyncRuntimeDiagnosticsTests(unittest.TestCase):
    """
    클래스 이름: AsyncRuntimeDiagnosticsTests
    기능: sink 지연 중 거래·통신 관측과 bounded writer 계약을 확인한다.
    작성 날짜: 2026/09/22
    """

    def test_blocked_sink_keeps_worker_heartbeat_and_liveness_running(self) -> None:
        """
        함수 이름: test_blocked_sink_keeps_worker_heartbeat_and_liveness_running()
        기능: 디스크 대기를 흉내 내도 실제 worker cycle과 UI heartbeat 기록이 반환한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        sink_entered = Event()
        release_sink = Event()
        cycle_completed = Event()

        def blocked_sink(record):
            """
            함수 이름: blocked_sink()
            기능: 단일 writer의 파일 지연을 barrier로 재현한다.
            인자: record -> 수락한 진단
            반환값: 없음
            작성 날짜: 2026/09/22
            """
            sink_entered.set()
            release_sink.wait(2)

        diagnostics = RuntimeDiagnostics(blocked_sink, asynchronous=True)
        diagnostics.record("order_processing")
        self.assertTrue(sink_entered.wait(1))

        async def runtime_cycle():
            """
            함수 이름: runtime_cycle()
            기능: 정지한 sink와 같은 진단 경계를 사용하는 거래 cycle을 실행한다.
            인자: 없음
            반환값: 빈 처리 결과
            작성 날짜: 2026/09/22
            """
            diagnostics.record("strategy_evaluated")
            cycle_completed.set()
            return ()

        worker = _TradingEventRuntimeWorker(runtime_cycle, lambda: None, lambda: True,
            lambda: {}, RLock(), diagnostics=diagnostics, poll_interval_seconds=0.01)
        try:
            worker.start()
            self.assertTrue(cycle_completed.wait(0.5))
            began = monotonic()
            diagnostics.record("ui_stream_heartbeat")
            self.assertLess(monotonic() - began, 0.2)
            self.assertIsNotNone(diagnostics.liveness.snapshot()["last_strategy_evaluation_at_ms"])
            self.assertFalse(worker.failed)
        finally:
            worker.close()
            release_sink.set()
            self.assertTrue(diagnostics.close())

    def test_queue_preserves_input_copy_order_and_redaction_with_one_writer(self) -> None:
        """
        함수 이름: test_queue_preserves_input_copy_order_and_redaction_with_one_writer()
        기능: enqueue 뒤 원본 변경이 기록에 섞이지 않고 한 thread에서 FIFO로 저장된다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        sink_entered = Event()
        release_sink = Event()
        records = []
        writer_identities = set()

        def capture_sink(record):
            """
            함수 이름: capture_sink()
            기능: 첫 기록만 막고 수신 순서와 writer identity를 수집한다.
            인자: record -> 원본과 분리된 기록
            반환값: 없음
            작성 날짜: 2026/09/22
            """
            writer_identities.add(get_ident())
            sink_entered.set()
            release_sink.wait(2)
            records.append(record)

        diagnostics = RuntimeDiagnostics(capture_sink, asynchronous=True)
        try:
            diagnostics.record("first")
            self.assertTrue(sink_entered.wait(1))
            mutable_details = {"prices": [Decimal("1.234567890123456789")], "token": "SECRET_CANARY"}
            before_enqueue = datetime.now(timezone.utc)
            diagnostics.record("second", values=mutable_details)
            after_enqueue = datetime.now(timezone.utc)
            mutable_details["prices"].append(Decimal("9"))
            diagnostics.record("third")
            release_sink.set()
            self.assertTrue(diagnostics.flush())
            self.assertEqual([record["event"] for record in records], ["first", "second", "third"])
            self.assertEqual([record["diagnostic_sequence"] for record in records], [1, 2, 3])
            self.assertEqual(records[1]["details"]["values"]["prices"], ["1.234567890123456789"])
            self.assertNotIn("SECRET_CANARY", json.dumps(records[1]["details"]))
            self.assertLessEqual(before_enqueue, records[1]["observed_at"])
            self.assertLessEqual(records[1]["observed_at"], after_enqueue)
            self.assertEqual(len(writer_identities), 1)
        finally:
            release_sink.set()
            self.assertTrue(diagnostics.close())

    def test_overflow_and_bounded_close_report_unwritten_without_blocking(self) -> None:
        """
        함수 이름: test_overflow_and_bounded_close_report_unwritten_without_blocking()
        기능: 정지한 writer의 queue 상한·누락 보고·제한 종료를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        sink_entered = Event()
        release_sink = Event()
        records = []

        def blocked_sink(record):
            """
            함수 이름: blocked_sink()
            기능: 초과와 종료를 검사할 동안 sink를 정지한다.
            인자: record -> 수신 기록
            반환값: 없음
            작성 날짜: 2026/09/22
            """
            sink_entered.set()
            release_sink.wait(2)
            records.append(record)

        diagnostics = RuntimeDiagnostics(blocked_sink, asynchronous=True, queue_capacity=2)
        try:
            diagnostics.record("active")
            self.assertTrue(sink_entered.wait(1))
            diagnostics.record("queued_first")
            diagnostics.record("queued_second")
            for _ in range(10):
                diagnostics.record("overflow")
            snapshot = diagnostics.queue_snapshot()
            self.assertEqual(snapshot["queued_records"], 2)
            self.assertEqual(snapshot["overflow_count"], 10)
            self.assertEqual(snapshot["first_failure_event"], "overflow")
            began = monotonic()
            self.assertFalse(diagnostics.close(timeout_seconds=0.02))
            self.assertLess(monotonic() - began, 0.2)
            self.assertEqual(diagnostics.queue_snapshot()["close_unwritten_records"], 3)
            release_sink.set()
            self.assertTrue(diagnostics.close())
            self.assertEqual(records[1]["dropped_records_before"], 10)
            self.assertEqual(diagnostics.failure_count, 10)
            self.assertEqual(diagnostics.queue_snapshot()["dropped_records"], 0)
        finally:
            release_sink.set()
            diagnostics.close()

    def test_writer_failure_reports_gap_and_recovers_without_duplicate_worker(self) -> None:
        """
        함수 이름: test_writer_failure_reports_gap_and_recovers_without_duplicate_worker()
        기능: sink 예외와 logging handler 예외 뒤에도 같은 writer가 후속 기록을 저장한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        records = []

        def failing_sink(record):
            """
            함수 이름: failing_sink()
            기능: 첫 파일 쓰기만 실패시키고 다음 기록을 수집한다.
            인자: record -> 현재 기록
            반환값: 없음
            작성 날짜: 2026/09/22
            """
            if record["event"] == "lost":
                raise OSError("PRIVATE_CANARY")
            records.append(record)

        diagnostics = RuntimeDiagnostics(failing_sink, asynchronous=True)
        try:
            with patch("logging.Logger.error", side_effect=OSError("handler failed")):
                diagnostics.record("lost")
                self.assertTrue(diagnostics.flush())
            diagnostics.record("recovered")
            self.assertTrue(diagnostics.flush())
            self.assertEqual(records[0]["dropped_records_before"], 1)
            self.assertEqual(records[0]["first_failure_event"], "lost")
            self.assertEqual(diagnostics.failure_count, 1)
        finally:
            self.assertTrue(diagnostics.close())

    def test_production_factory_uses_async_writer_and_flushes_on_close(self) -> None:
        """
        함수 이름: test_production_factory_uses_async_writer_and_flushes_on_close()
        기능: 운영 조립이 비동기 모드를 켜고 종료 뒤 실제 JSONL을 읽을 수 있다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        with TemporaryDirectory() as directory:
            diagnostics = create_runtime_diagnostics("live", Path(directory))
            self.assertIsNotNone(diagnostics._writer_thread)
            diagnostics.record("last_record")
            self.assertTrue(diagnostics.close())
            paths = list(Path(directory).glob("*.log"))
            records = [json.loads(line) for line in paths[0].read_text().splitlines()]
            self.assertEqual([record["event"] for record in records], ["logging_started", "last_record"])
