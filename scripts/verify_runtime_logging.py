"""주문 전송 없는 로그 검증을 실행하고 Log_History에 실제 파일과 결과를 보존한다."""

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


def verify_runtime_logging(output_directory: Path) -> bool:
    """
    함수 이름: verify_runtime_logging()
    기능: memory 거래소 기반 검증을 실행하고 JSONL 파일 내용과 시험 결과를 영구 보존한다.
    인자: output_directory -> 이번 검증의 독립 artifact 디렉터리
    반환값: 모든 검증이 통과하면 True
    작성 날짜: 2026/09/09
    """
    project_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(project_root / "backend"))
    sys.path.insert(0, str(project_root / "backend" / "src"))
    from tests.integration.test_runtime_logging_flow import RuntimeLoggingFlowTests
    from tests.unit.filesystem.test_diagnostic_log_writer import DiagnosticLogWriterTests

    # 이미 사용한 경로를 덮어쓰지 않고 실제 LIVE와 구분되는 fake 검증 파일을 남긴다.
    output_directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    integration_suite = unittest.defaultTestLoader.loadTestsFromTestCase(RuntimeLoggingFlowTests)
    for test_case in integration_suite:
        test_case.artifact_directory = output_directory
    suite = unittest.TestSuite((integration_suite, unittest.defaultTestLoader.loadTestsFromTestCase(DiagnosticLogWriterTests)))

    # 잘못된 fixture 변경이 있어도 네트워크 접속을 통한 실주문 검증으로 바뀌지 않게 한다.
    with patch("socket.socket.connect", side_effect=AssertionError("verification network is disabled")):
        result = unittest.TextTestRunner(verbosity=2).run(suite)

    # 로그를 다시 parsing한 집계는 메모리 trace 대신 실제 디스크 기록을 검증 증거로 삼는다.
    log_paths = sorted(output_directory.glob("*.log"))
    records = [json.loads(line) for log_path in log_paths for line in log_path.read_text(encoding="utf-8").splitlines()]
    event_counts: dict[str, int] = {}
    transitions = set()
    for record in records:
        event = record["event"]
        event_counts[event] = event_counts.get(event, 0) + 1
        transitions.update(record["details"].get("transition_ids", ()))
    report = {
        "successful": result.wasSuccessful(),
        "tests_run": result.testsRun,
        "failure_count": len(result.failures),
        "error_count": len(result.errors),
        "live_orders_sent": 0,
        "network_access": "disabled",
        "log_files": [str(log_path) for log_path in log_paths],
        "record_count": len(records),
        "event_counts": event_counts,
        "transition_ids": sorted(transitions),
        "fault_note": "ERROR records in fake logs are intentional timeout, persistence and event failure injections.",
    }
    report_path = output_directory / "verification_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"검증 결과: {report_path}")  # 이 별도 검증 script만 결과 위치를 콘솔에 출력한다.
    return result.wasSuccessful()


def main() -> int:
    """
    함수 이름: main()
    기능: KST 시각별 기본 경로 또는 지정 경로에서 offline 로그 검증을 실행한다.
    인자: 없음, 선택적인 --output-directory 명령행 인자 사용
    반환값: 성공 0 또는 검증 실패 1
    작성 날짜: 2026/09/09
    """
    local_time = datetime.now(timezone(timedelta(hours=9)))
    default_directory = Path(__file__).resolve().parents[1] / "Log_History" / f"verification_{local_time:%Y-%m-%d_%H-%M-%S-%f}_KST"
    parser = argparse.ArgumentParser(description="실제 주문 없는 backend 로그 검증")
    parser.add_argument("--output-directory", type=Path, default=default_directory)
    arguments = parser.parse_args()
    return 0 if verify_runtime_logging(arguments.output_directory) else 1


if __name__ == "__main__":
    raise SystemExit(main())  # 실패를 성공 종료 코드로 숨기지 않는다.
