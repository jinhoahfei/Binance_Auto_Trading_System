"""장애 종류·시계·증거 부재를 혼동하지 않는 진단 분석 회귀 테스트."""

from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT)) if str(ROOT) not in sys.path else None
from scripts.analyze_runtime_incidents import analyze, classify, duration, read_records


def sample(**fields):
    """함수 이름: sample()
    기능: 비밀 없는 정상 5초 측정값을 만들고 단일 장애만 주입한다.
    인자: fields -> 고장 조건
    반환값: native 관측
    작성 날짜: 2026/09/16
    """
    return dict(at_ms=100_000, native_pid=42, backend_pid=43, renderer_age_ms=1000,
                main_thread_age_ms=1000, probe_status="ok", environment={}, source={"file": "fixture", "line": 1}, **fields)


class RuntimeIncidentTests(unittest.TestCase):
    """클래스 이름: RuntimeIncidentTests
    기능: 과거 UI 공백과 새 계층별 장애 주입을 판정한다.
    작성 날짜: 2026/09/16
    """

    def test_original_99971_ms_is_not_a_binance_outage(self):
        """함수 이름: test_original_99971_ms_is_not_a_binance_outage()
        기능: 2026-09-16 원본의 선별된 두 기록으로 99.971초와 1.989초를 재계산한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/16
        """
        rows, problems = read_records(ROOT / "backend/tests/fixtures/liveness")
        report = analyze(rows, problems)["incidents"][0]
        self.assertEqual(report["ui_receive_gap"]["ms"], 99971)
        self.assertEqual(report["detection_delay"]["ms"], 97982)
        self.assertEqual(report["recovery_time"]["ms"], 1989)
        self.assertIsNone(report["exchange_outage_duration_ms"])
        self.assertEqual(report["root_confidence"], "unknown")
        self.assertIn("no_direct_os_execution_evidence", report["missing_evidence"])

    def test_layer_faults_remain_separate(self):
        """함수 이름: test_layer_faults_remain_separate()
        기능: 100초 화면 정지·소켓 수신 장애·백엔드 지연·거래소 장애를 별도로 분류한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/16
        """
        cases = [({"renderer_age_ms": 100_000, "renderer": {"timer_lag_ms": 95000}}, "renderer_execution_delayed"),
                 ({"renderer_age_ms": 100_000}, "renderer_signal_missing"),
                 ({}, "ui_receive_gap"), ({"probe_status": "timeout"}, "backend_probe_failed"),
                 ({"backend": {"monotonic_ms": 100_000, "last_runtime_cycle_monotonic_ms": 1000}}, "backend_processing_delayed"),
                 ({"backend": {"market_stream": "offline", "streams_checked_at_ms": 90_000}}, "exchange_stream_failure")]
        for fields, expected in cases:
            row = sample()
            row.update(fields)
            with self.subTest(expected=expected):
                self.assertEqual(classify([row], [], [], 0, 100_000)["cause"], expected)

    def test_os_proof_requires_target_and_incident_time_not_just_visibility(self):
        """함수 이름: test_os_proof_requires_target_and_incident_time_not_just_visibility()
        기능: 가림/정책/기록 거부를 OS 정지로 단정하지 않고 시각과 대상이 맞는 직접 증거만 수용한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/16
        """
        row = sample()
        row.update(renderer_age_ms=90_000, renderer={"timer_lag_ms": 85000}, environment={"occluded": True, "actual_policy": "suspend"})
        for status in ("access_denied", "unsupported", "no_records", "timeout", "query_failed"):
            result = classify([row], [{"event": "os_evidence", "result": {"status": status}}], [], 10_000, 100_000)
            self.assertEqual(result["cause"], "renderer_execution_delayed")
            self.assertEqual(result["root_confidence"], "inferred")
        proof = {"event": "os_evidence", "source": row["source"], "result": {"events": [{"event": "process_suspend_reported",
                 "target_pid": 42, "timestamp": datetime.fromtimestamp(20, timezone.utc).isoformat()}]}}
        self.assertEqual(classify([row], [proof], [], 10_000, 100_000)["cause"], "os_execution_restriction")
        self.assertNotEqual(classify([row], [proof], [], 50_000, 100_000)["cause"], "os_execution_restriction")
        proof["result"]["events"][0]["target_pid"] = 420
        self.assertNotEqual(classify([row], [proof], [], 10_000, 100_000)["cause"], "os_execution_restriction")

    def test_sleep_exit_and_storage_loss_have_explicit_evidence(self):
        """함수 이름: test_sleep_exit_and_storage_loss_have_explicit_evidence()
        기능: 잠자기/복귀·실제 종료·로그 유실을 직접 증거와 함께 보고한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/16
        """
        row = sample()
        row["write_failures"] = 1
        for event, expected in (("system_did_wake", "system_sleep"), ("backend_exited", "backend_exited")):
            result = classify([row], [{"event": event, "at_ms": 90_000, "source": row["source"]}], [], 0, 100_000)
            self.assertEqual(result["cause"], expected)
            self.assertIn("diagnostic_records_lost", result["missing_evidence"])

    def test_clock_change_and_restart_do_not_become_outage_duration(self):
        """함수 이름: test_clock_change_and_restart_do_not_become_outage_duration()
        기능: 벽시계 역행에서도 단조 시계를 사용하며 서로 다른 실행 사건은 합치지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/16
        """
        self.assertEqual(duration(100_000, 1, 10, 1010), {"ms": 1000, "basis": "monotonic", "wall_clock_jump": True})
        rows, _ = read_records(ROOT / "backend/tests/fixtures/liveness")
        rows[1]["renderer_id"] = "another-renderer"
        report = analyze(rows)["incidents"][0]
        self.assertIsNone(report["ui_receive_gap"]["ms"])
        self.assertEqual(duration(1, 2, 1000, 1)["basis"], "clock_invalid")
