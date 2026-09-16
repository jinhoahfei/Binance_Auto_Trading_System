"""실제 WebView soak의 수행 시간·수신·진단 유실·복구를 확인한다."""

import argparse
import json
from pathlib import Path
from analyze_runtime_incidents import read_records, analyze


def validate(root):
    """함수 이름: validate()
    기능: 시간 경과와 실제 수신 증거를 검사하며 24시간/20분 미수행을 통과로 표시하지 않는다.
    인자: root -> 독립 검증 앱 산출물 경로
    반환값: 검증 결과와 자원·로그 요약
    작성 날짜: 2026/09/17
    """
    rows, problems = read_records(root)
    rows.sort(key=lambda row: row.get("monotonic_ms", 0) if row["layer"] == "native" else 0)
    start = next((row for row in rows if row.get("event") == "soak_started"), {})
    end = next((row for row in rows if row.get("event") == "soak_duration_completed"), {})
    summaries = [row for row in rows if row.get("event") == "soak_summary"]
    samples = [row for row in rows if row.get("event") == "runtime_sample"]
    injected = start.get("injected_freeze_seconds", 0)
    issues = []
    elapsed = (end.get("monotonic_ms", 0) - start.get("monotonic_ms", 0)) / 1000 if end else 0
    if not end or elapsed + 1 < start.get("target_seconds", 1):
        issues.append("duration_incomplete")
    if not summaries or summaries[-1].get("received_events", 0) == 0:
        issues.append("no_renderer_events")
    if not any(row.get("chart_live") for row in summaries):
        issues.append("public_chart_not_verified")
    if any(row.get("errors", 0) for row in summaries):
        issues.append("renderer_error")
    if any(row.get("dropped_before", 0) or row.get("write_failures", 0) for row in rows):
        issues.append("diagnostic_loss")
    if not any(row.get("event") == "execution_policy_readback" and row.get("status") == "verified" for row in rows):
        issues.append("background_policy_not_verified")
    if not samples or any(row.get("probe_status") != "ok" for row in samples):
        issues.append("backend_probe_failed")
    gaps = [row["renderer_age_ms"] for row in samples if row.get("renderer_age_ms") is not None]
    if not injected and max(gaps, default=15000) >= 15000:
        issues.append("heartbeat_gap_exceeded")
    report = analyze(rows, problems)
    if injected:
        native = report["native_incidents"]
        if not any(row["cause"] == "renderer_execution_delayed" and row["recovered_at_ms"] is not None for row in native):
            issues.append("injected_renderer_stall_not_independently_confirmed_and_recovered")
        if summaries and summaries[-1]["recoveries"] != 1:
            issues.append("expected_one_recovery")
    elif summaries and summaries[-1]["recoveries"] != 0:
        issues.append("unexpected_recovery")
    if problems:
        issues.append("unreadable_records")
    return {"status": "failed_or_incomplete" if issues else "passed", "issues": issues,
            "phase": start.get("phase"), "elapsed_seconds": elapsed,
            "continuous_24h_complete": elapsed >= 86400 and not issues and not injected,
            "formal_24h_complete": elapsed >= 86400 and not issues and not injected and any(row.get("event") == "soak_window_cycle" for row in rows),
            "formal_20m_complete": elapsed >= 1200 and not issues and not injected and start.get("phase") == "minimized"
                and sum(bool((row.get("environment") or {}).get("minimized")) for row in samples) >= len(samples) * 0.9,
            "occluded_samples": sum(bool((row.get("environment") or {}).get("occluded")) for row in samples),
            "minimized_samples": sum(bool((row.get("environment") or {}).get("minimized")) for row in samples),
            "sample_count": len(samples), "max_heartbeat_age_ms": max(gaps, default=None),
            "last_summary": {key: summaries[-1].get(key) for key in ("received_events", "recoveries", "errors", "chart_live")} if summaries else None,
            "native_resources": samples[-1].get("resources") if samples else None,
            "total_log_bytes": sum(path.stat().st_size for path in Path(root).rglob("*.log")),
            "classification": [{key: row.get(key) for key in ("cause", "root_confidence", "missing_evidence")} for row in report["native_incidents"]]}


def main():
    """함수 이름: main()
    기능: stdout에 비밀 없는 결과를 출력하고 실패/미완료는 비정상 종료한다.
    인자: log_root -> 분석 경로
    반환값: 없음
    작성 날짜: 2026/09/17
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("log_root")
    args = parser.parse_args()
    result = validate(args.log_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
