"""실제 진단 파일의 분할·동시 기록·비밀 제거·저장 장애 복구를 검증한다."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from binance_auto_trader.adapters.filesystem.diagnostic_log_writer import DiagnosticLogWriter
from binance_auto_trader.application.runtime_diagnostics import RuntimeDiagnostics
from binance_auto_trader.application.trading_diagnostics import normalize_order_failure
from binance_auto_trader.bootstrap.diagnostics import create_runtime_diagnostics, resolve_log_directory


class DiagnosticLogWriterTests(unittest.TestCase):
    """
    클래스 이름: DiagnosticLogWriterTests
    기능: 실제 파일을 읽어 정밀도·순서·분할·정보 제외와 누락 보고를 검증한다.
    작성 날짜: 2026/09/09
    """

    def test_decimal_timestamps_and_secrets_are_encoded_without_stdout(self) -> None:
        """
        함수 이름: test_decimal_timestamps_and_secrets_are_encoded_without_stdout()
        기능: JSONL이 Decimal·KST 시각을 보존하고 credential·payload·예외 원문을 제외하는지 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        with TemporaryDirectory() as temporary_directory:
            writer = DiagnosticLogWriter(Path(temporary_directory), "fake")
            diagnostics = RuntimeDiagnostics(writer)
            secret_marker = "sensitive-value-must-never-appear"

            # 표준 출력은 sidecar protocol 전용이므로 정상 로그가 이를 건드리지 않는지도 확인한다.
            with patch("sys.stdout") as stdout:
                diagnostics.record("sample", price=Decimal("123.123456789012345678901234"),
                    duration=timedelta(microseconds=1234567), api_key=secret_marker,
                    nested={"signature": secret_marker, "payload": secret_marker})
                try:
                    raise ValueError(secret_marker)
                except ValueError as error:
                    diagnostics.record_exception("verification", error)
                stdout.write.assert_not_called()
            content = writer.path.read_text(encoding="utf-8")
            records = [json.loads(line) for line in content.splitlines()]
            self.assertNotIn(secret_marker, content)
            self.assertEqual(records[0]["details"]["price"], "123.123456789012345678901234")
            self.assertEqual(records[0]["details"]["duration"], "1.234567")
            self.assertTrue(records[0]["timestamp_kst"].endswith("+09:00"))
            self.assertEqual(records[1]["details"]["causes"][0]["exception_type"], "ValueError")
            self.assertEqual(diagnostics.failure_count, 0)

    def test_rotation_keeps_old_files_and_uses_kst_midnight(self) -> None:
        """
        함수 이름: test_rotation_keeps_old_files_and_uses_kst_midnight()
        기능: 크기와 한국 자정 분할 뒤 이전 record가 삭제되거나 순서가 초기화되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        with TemporaryDirectory() as temporary_directory:
            writer = DiagnosticLogWriter(Path(temporary_directory), "fake", maximum_bytes=1)
            before_midnight = datetime(2026, 9, 9, 14, 59, 59, tzinfo=timezone.utc)

            # 같은 KST 날짜의 크기 분할과 다음 KST 날짜의 분할을 별도로 유발한다.
            writer({"observed_at": before_midnight, "event": "first"})
            first_path = writer.path
            writer({"observed_at": before_midnight, "event": "size_rotated"})
            second_path = writer.path
            writer({"observed_at": before_midnight + timedelta(seconds=1), "event": "day_rotated"})
            third_path = writer.path
            self.assertEqual(len({first_path, second_path, third_path}), 3)
            self.assertTrue(json.loads(first_path.read_text())["timestamp_kst"].startswith("2026-09-09T23:59:59"))
            self.assertTrue(third_path.name.startswith("2026-09-10_00-00-00"))
            self.assertEqual([json.loads(path.read_text())["sequence"] for path in (first_path, second_path, third_path)], [1, 2, 3])

    def test_concurrent_writers_have_complete_lines_and_contiguous_sequence(self) -> None:
        """
        함수 이름: test_concurrent_writers_have_complete_lines_and_contiguous_sequence()
        기능: 복수 worker의 동시 사건도 한 실행 안에서 중복·잘린 줄 없이 저장되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        with TemporaryDirectory() as temporary_directory:
            writer = DiagnosticLogWriter(Path(temporary_directory), "fake")
            diagnostics = RuntimeDiagnostics(writer)

            # 실제 thread를 통해 파일 출력 lock의 순서 보존을 확인한다.
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(diagnostics.record, "concurrent", index=index) for index in range(80)]
                for future in futures:
                    future.result()
            records = [json.loads(line) for line in writer.path.read_text().splitlines()]
            self.assertEqual([record["sequence"] for record in records], list(range(1, 81)))
            self.assertEqual({record["details"]["index"] for record in records}, set(range(80)))

    def test_write_failure_preserves_trading_and_reports_gap_on_recovery(self) -> None:
        """
        함수 이름: test_write_failure_preserves_trading_and_reports_gap_on_recovery()
        기능: 저장 실패를 stderr로 알리고 복구 후 partial tail 제거와 누락 건수를 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        with TemporaryDirectory() as temporary_directory:
            writer = DiagnosticLogWriter(Path(temporary_directory), "fake")
            diagnostics = RuntimeDiagnostics(writer)
            diagnostics.record("before_failure")

            # 디스크 쓰기 실패가 거래 호출자에게 전파되지 않으면서 한 번만 경고하는지 검증한다.
            with patch.object(Path, "open", side_effect=OSError("private disk error")):
                with self.assertLogs("binance_auto_trader.application.runtime_diagnostics", level="ERROR") as captured:
                    diagnostics.record("lost_one")
                    diagnostics.record("lost_two")
            self.assertEqual(len(captured.output), 1)
            self.assertNotIn("private disk error", str(captured.output))
            with writer.path.open("ab") as output_stream:
                output_stream.write(b'{"partial":')  # 중단된 한 번의 append가 남긴 불완전한 끝부분을 재현한다.
            diagnostics.record("recovered")
            records = [json.loads(line) for line in writer.path.read_text().splitlines()]
            self.assertEqual([record["event"] for record in records], ["before_failure", "recovered"])
            self.assertEqual(records[-1]["dropped_records_before"], 2)
            self.assertEqual(diagnostics.failure_count, 2)

    def test_separate_runs_and_default_directory_do_not_depend_on_cwd(self) -> None:
        """
        함수 이름: test_separate_runs_and_default_directory_do_not_depend_on_cwd()
        기능: 동일 프로세스 재조립에도 파일이 분리되고 프로젝트 지정 경로가 선택되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        with TemporaryDirectory() as temporary_directory:
            first_writer = DiagnosticLogWriter(Path(temporary_directory), "fake")
            second_writer = DiagnosticLogWriter(Path(temporary_directory), "fake")
            self.assertNotEqual(first_writer.path, second_writer.path)
            self.assertNotEqual(first_writer.run_id, second_writer.run_id)

        # 테스트 실행 cwd가 backend여도 소스 프로젝트 표식으로 루트 경로를 찾아야 한다.
        project_root = Path(__file__).resolve().parents[4]
        self.assertEqual(resolve_log_directory(), project_root / "Log_History")
        self.assertEqual(normalize_order_failure("BINANCE_SUBMISSION_REJECTED_-1013"), {"code": "BINANCE_SUBMISSION_REJECTED", "api_code": -1013})
        self.assertEqual(normalize_order_failure("secret response")["code"], "UNCLASSIFIED_ORDER_FAILURE")
        self.assertEqual(normalize_order_failure("INSUFFICIENT_BALANCES")["code"], "INSUFFICIENT_BALANCES")

    def test_live_enables_default_file_and_unwritable_start_fails(self) -> None:
        """
        함수 이름: test_live_enables_default_file_and_unwritable_start_fails()
        기능: LIVE 기본 파일 생성과 시작 시 저장 불가 경로 거부를 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        with TemporaryDirectory() as temporary_directory:
            with patch("binance_auto_trader.bootstrap.diagnostics.resolve_log_directory", return_value=Path(temporary_directory)):
                diagnostics = create_runtime_diagnostics("live")
            self.assertTrue(diagnostics.enabled)
            self.assertEqual(len(list(Path(temporary_directory).glob("*_KST_live_*.log"))), 1)

            # 파일을 디렉터리로 가장한 경로는 logging 없는 runtime으로 완화되지 않아야 한다.
            blocked_path = Path(temporary_directory) / "blocked"
            blocked_path.write_text("occupied", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                create_runtime_diagnostics("live", blocked_path)

    def test_partial_write_is_repaired_before_next_day_rotation(self) -> None:
        """
        함수 이름: test_partial_write_is_repaired_before_next_day_rotation()
        기능: 자정을 넘겨 복구해도 이전 파일의 불완전한 JSON 끝부분이 남지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        with TemporaryDirectory() as temporary_directory:
            writer = DiagnosticLogWriter(Path(temporary_directory), "fake")
            timestamp = datetime(2026, 9, 9, 14, 59, 59, tzinfo=timezone.utc)
            writer({"observed_at": timestamp, "event": "last_good"})
            old_path = writer.path

            # 운영 중 append가 실패한 뒤 남은 partial tail을 만든다.
            with patch.object(Path, "open", side_effect=OSError("controlled failure")):
                with self.assertRaises(OSError):
                    writer({"observed_at": timestamp, "event": "lost"})
            with old_path.open("ab") as output_stream:
                output_stream.write(b'{"partial":')
            writer({"observed_at": timestamp + timedelta(seconds=1), "event": "next_day"})
            self.assertNotEqual(old_path, writer.path)
            self.assertEqual(json.loads(old_path.read_text())["event"], "last_good")
            self.assertEqual(json.loads(writer.path.read_text())["sequence"], 2)
