"""24시간 통신 점검 원본을 변경하지 않고 사건·요청·순서·자원 추이를 교차 검증한다."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import statistics


ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024


def read_json_lines(paths):
    """
    함수 이름: read_json_lines()
    기능: 모든 행을 엄격하게 해석하고 손상·빈 행을 숨기지 않는다.
    인자: paths -> 순서대로 읽을 파일 경로
    반환값: JSON object 목록
    작성 날짜: 2026/09/14
    """
    return [json.loads(line) for path in paths for line in path.read_text().splitlines()]


def summarize(values):
    """
    함수 이름: summarize()
    기능: 실제 관측 분포의 범위·중앙값과 nearest-rank 95백분위를 계산한다.
    인자: values -> 비어 있지 않은 수치 목록
    반환값: 기술 통계
    작성 날짜: 2026/09/14
    """
    ordered = sorted(values)
    return {"count": len(values), "min": min(values), "median": statistics.median(values),
        "mean": statistics.mean(values), "p95": ordered[-(-95 * len(ordered) // 100) - 1], "max": max(values)}


def analyze(directory, launch_path):
    """
    함수 이름: analyze()
    기능: 종료 요약과 전체 UI·backend 로그 및 5초 측정치를 독립 대조한다.
    인자: directory -> 원본 점검 경로, launch_path -> 실행 코드 해시 기록
    반환값: 검증 결과·사건별 복구 시간·시간별 자원 요약
    작성 날짜: 2026/09/14
    """
    status = json.loads((directory / "status.json").read_text())
    launch = json.loads(launch_path.read_text())
    metric_paths = [directory / "metrics.jsonl"]
    ui_paths = sorted(directory.glob("ui-part*.jsonl"))
    backend_paths = sorted((directory / "backend").glob("*.log"))
    metrics = read_json_lines(metric_paths)
    raw_ui = read_json_lines(ui_paths)
    backend = read_json_lines(backend_paths)
    unique_ui = {(row["renderer_id"], row["sequence"]): row for row in raw_ui}
    ui = sorted(unique_ui.values(), key=lambda row: row["sequence"])
    checks = []

    def check(name, passed):
        """
        함수 이름: check()
        기능: 합격과 실패를 모두 결과에 보존한다.
        인자: name -> 검사 이름, passed -> 대조 결과
        반환값: 없음
        작성 날짜: 2026/09/14
        """
        checks.append({"name": name, "passed": bool(passed)})

    check("completed_24_hours", status["status"] == "completed_needs_review"
        and status["elapsed_seconds"] >= 86400 and status["terminal"] is None)
    check("final_summary_matches_last_metric", status == metrics[-1])
    check("metric_time_and_counters_monotonic", all(right["elapsed_seconds"] > left["elapsed_seconds"]
        and all(right[key] >= left[key] for key in ("events", "faults", "recoveries"))
        for left, right in zip(metrics, metrics[1:])))
    check("metric_sampling_no_long_gap", max(right["elapsed_seconds"] - left["elapsed_seconds"]
        for left, right in zip(metrics, metrics[1:])) < 10)
    check("all_faults_recovered", status["faults"] > 0 and status["faults"] == status["recoveries"])
    check("ui_counts_match_summary", dict(Counter(row["event"] for row in ui)) == status["counts"])
    check("ui_diagnostic_sequences_contiguous", len({row["renderer_id"] for row in ui}) == 1
        and [row["sequence"] for row in ui] == list(range(1, len(ui) + 1)))
    check("backend_diagnostic_sequences_contiguous", len({row["run_id"] for row in backend}) == 1
        and [row["sequence"] for row in backend] == list(range(1, len(backend) + 1)))
    check("diagnostic_drops_zero", all(row["dropped_before"] == 0 for row in ui)
        and all(row["dropped_records_before"] == 0 for row in backend))
    check("same_session_throughout", {row["session_id"] for row in ui}
        == {row["details"]["transport_session_id"] for row in backend})

    incidents = defaultdict(list)
    for row in ui:
        if row.get("incident_id"):
            incidents[row["incident_id"]].append(row)
    recovery_rows = []
    snapshot_skipped = 0
    complete_incidents = True
    for incident_id, rows in incidents.items():
        grouped = defaultdict(list)
        for row in rows:
            grouped[row["event"]].append(row)
        expected_counts = {"first_failure": 1, "request_started": 3, "request_failed": 2,
            "request_succeeded": 1, "retry_scheduled": 2, "connection_started": 1,
            "socket_opened": 1, "authentication_sent": 1, "connection_ready": 1}
        if any(len(grouped[event]) != count for event, count in expected_counts.items()):
            complete_incidents = False
            continue
        first = grouped["first_failure"][0]
        ready = grouped["connection_ready"][0]
        connect = grouped["connection_started"][0]
        complete_incidents &= first["error_code"] == "EVENT_STREAM_CLOSED" and first["close_code"] == 1006
        complete_incidents &= all(row["http_status"] == 503 and row["error_code"] == "BACKEND_UNREACHABLE"
            for row in grouped["request_failed"])
        complete_incidents &= grouped["request_succeeded"][0]["http_status"] == 200
        complete_incidents &= [row["delay_ms"] for row in grouped["retry_scheduled"]] == [1000, 2000]
        complete_incidents &= first["at_ms"] < connect["at_ms"] <= ready["at_ms"]
        snapshot_skipped += connect["last_sequence"] - first["last_sequence"]
        recovery_rows.append({"incident_id": incident_id, "started_at_ms": first["at_ms"],
            "elapsed_seconds": (ready["at_ms"] - first["at_ms"]) / 1000})
    check("every_incident_has_exact_expected_recovery_chain", complete_incidents
        and len(recovery_rows) == len(incidents) == status["faults"])

    ui_requests = {row["request_id"]: row["http_status"] for row in ui
        if row["event"] in ("request_failed", "request_succeeded")}
    backend_requests = {row["details"]["request_id"]: row["details"]["http_status"] for row in backend
        if row["event"] == "ui_http_request_completed"}
    check("every_http_response_correlates_with_backend", ui_requests == backend_requests
        and len(ui_requests) == 1 + 3 * status["faults"])
    ui_cursors = [row["last_sequence"] for row in ui if row["event"] == "connection_started"]
    backend_cursors = [row["details"]["after_sequence"] for row in backend if row["event"] == "ui_stream_authenticated"]
    check("every_new_socket_uses_snapshot_cursor", ui_cursors == backend_cursors)
    check("events_and_snapshot_skips_account_for_final_sequence", status["events"] + snapshot_skipped + ui_cursors[0]
        == ui[-1]["last_sequence"])
    check("ui_backend_error_codes_only_injected_faults", {row["error_code"] for row in ui if row.get("error_code")}
        == {"EVENT_STREAM_CLOSED", "BACKEND_UNREACHABLE"})
    backend_errors = [row for row in backend if row["level"] in ("ERROR", "WARNING")]
    check("backend_errors_only_expected_socket_termination", Counter(row["event"] for row in backend_errors)
        == Counter({"ui_stream_connection_lost": status["faults"], "operation_failed": status["faults"]})
        and all(row["details"]["stage"] == "ui_stream_connection_lost"
            and [cause["exception_type"] for cause in row["details"]["causes"]] in (["_WebSocketClosed"], ["ConnectionResetError"])
            for row in backend_errors if row["event"] == "operation_failed"))
    # TCP 종료는 EOF 또는 reset으로 관측될 수 있다. 예외 타입만 허용하지 않고
    # 해당 오류가 실제 주입으로 닫은 연결·시각·sequence와 대응하는지도 검증한다.
    first_failures = [row for row in ui if row["event"] == "first_failure"]
    backend_failures = [row for row in backend if row["event"] == "operation_failed"]
    opened_connections = [row["details"]["connection_id"] for row in backend if row["event"] == "ui_stream_opened"]
    check("backend_exceptions_correlate_with_injected_disconnects", len(first_failures) == len(backend_failures)
        and all(failure["details"]["connection_id"] == opened_connections[index]
            and 0 <= datetime.fromisoformat(failure["timestamp_utc"]).timestamp() - first["at_ms"] / 1000 <= 2
            and 0 <= failure["details"]["last_sequence"] - first["last_sequence"] <= 1
            for index, (first, failure) in enumerate(zip(first_failures, backend_failures))))
    check("all_backend_sockets_closed", Counter(row["event"] for row in backend)["ui_stream_closed"] == len(ui_cursors))
    check("no_stale_timeout_or_terminal_failure", not any(row["event"] == "terminal_failure"
        or row.get("error_code") in ("EVENT_STREAM_STALE", "EVENT_STREAM_CONNECT_TIMEOUT", "BACKEND_REQUEST_TIMEOUT") for row in ui))
    check("sockets_do_not_accumulate", max(row["live_sockets"] for row in metrics) <= 1
        and status["max_sockets"] == 1 and status["live_sockets"] == 0)
    check("worker_and_backend_diagnostics_healthy", all(not row["backend"]["worker_failed"]
        and row["backend"]["diagnostic_failures"] == 0 for row in metrics))
    source_changes = [name for name, digest in launch["source_sha256"].items()
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest]
    check("current_sources_match_launch_hashes", not source_changes)

    hourly = []
    for hour in range(24):
        rows = [row for row in metrics if hour * 3600 <= row["elapsed_seconds"] < (hour + 1) * 3600]
        hourly.append({"hour": hour, "samples": len(rows),
            "heap_mib": summarize([row["node_memory"]["heapUsed"] / MIB for row in rows]),
            "rss_mib": summarize([row["node_memory"]["rss"] / MIB for row in rows]),
            "backend_peak_rss_mib": max(row["backend"]["max_rss"] for row in rows) / MIB,
            "backend_threads": sorted({row["backend"]["threads"] for row in rows})})
    large_heap_drops = [{"hour": right["elapsed_seconds"] / 3600,
        "before_mib": left["node_memory"]["heapUsed"] / MIB, "after_mib": right["node_memory"]["heapUsed"] / MIB}
        for left, right in zip(metrics, metrics[1:])
        if left["node_memory"]["heapUsed"] - right["node_memory"]["heapUsed"] > 2 * MIB]
    recovery_times = [row["elapsed_seconds"] for row in recovery_rows]
    resource_names = {name for row in metrics for name in row["active_resources"]}
    return {"verdict": "PASS_WITHIN_TESTED_TRANSPORT_SCOPE" if all(row["passed"] for row in checks) else "REVIEW_REQUIRED",
        "checks": checks, "status": status, "metric_samples": len(metrics),
        "metric_gap_seconds": summarize([right["elapsed_seconds"] - left["elapsed_seconds"] for left, right in zip(metrics, metrics[1:])]),
        "observed_event_age_seconds": summarize([row["since_last_event_seconds"] for row in metrics]),
        "recovery_seconds": summarize(recovery_times),
        "recovery_first_24_mean": statistics.mean(recovery_times[:24]),
        "recovery_last_24_mean": statistics.mean(recovery_times[-24:]),
        "incidents": recovery_rows, "ui_log_rows": len(ui), "ui_duplicate_rows": len(raw_ui) - len(ui),
        "backend_log_rows": len(backend), "snapshot_skipped_events": snapshot_skipped,
        "backend_exception_types": dict(Counter(row["details"]["causes"][0]["exception_type"] for row in backend_failures)),
        "last_event_sequence": ui[-1]["last_sequence"], "http_statuses": dict(Counter(ui_requests.values())),
        "ui_log_dropped": sum(row["dropped_before"] for row in ui),
        "backend_log_dropped": sum(row["dropped_records_before"] for row in backend),
        "hourly": hourly, "large_heap_drops": large_heap_drops,
        "node_memory_mib": {key: summarize([row["node_memory"][key] / MIB for row in metrics]) for key in ("heapUsed", "rss")},
        "backend_peak_rss_mib": max(row["backend"]["max_rss"] for row in metrics) / MIB,
        "resource_maxima": {name: max(row["active_resources"].count(name) for row in metrics) for name in sorted(resource_names)},
        "source_changes": source_changes,
        "memory_assessment": "Observed heap recovery and bounded sockets/threads; no heap snapshots or GC trace, so absence of a memory leak is not proven.",
        "input_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [directory / "status.json", *metric_paths, *ui_paths, *backend_paths, launch_path]},
        "analyzed_at": datetime.now().astimezone().isoformat()}


def main():
    """
    함수 이름: main()
    기능: 원본을 보존하고 별도 경로에 재현 가능한 분석 결과를 기록한다.
    인자: --input -> 점검 경로, --launch -> 실행 기록, --output -> 새 분석 파일
    반환값: 통과 시 0, 검토 필요 시 비정상 종료 코드
    작성 날짜: 2026/09/14
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--launch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = analyze(args.input.resolve(), args.launch.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as file:
        json.dump(result, file, ensure_ascii=False, indent=2)
        file.write("\n")
    print(json.dumps({"verdict": result["verdict"], "checks": len(result["checks"]),
        "failed_checks": [row["name"] for row in result["checks"] if not row["passed"]],
        "recovery_seconds": result["recovery_seconds"]}, ensure_ascii=False))
    return 0 if all(row["passed"] for row in result["checks"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
