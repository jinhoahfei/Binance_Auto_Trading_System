"""화면·native·backend 진단 v1/v2의 관측 공백과 원인 증거를 구분한다."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path


def read_records(root):
    """함수 이름: read_records()
    기능: 기존/신규 JSONL을 읽고 파일·행을 보존하며 재저장된 ring 측정값을 합친다.
    인자: root -> 원본 로그 디렉터리
    반환값: 정규화된 기록, 읽기 문제 목록
    작성 날짜: 2026/09/16
    """
    rows, problems, seen = [], [], {}
    for path in sorted(Path(root).rglob("*")):
        if path.suffix not in {".log", ".jsonl"} or not path.is_file():
            continue
        try:
            with path.open(errors="replace") as stream:
                for line, raw in enumerate(stream, 1):
                    source = {"file": str(path), "line": line}
                    try:
                        row = json.loads(raw)
                        if not isinstance(row, dict):
                            continue
                        item = dict(row.get("connection", row.get("chart", row)))
                        layer = "ui" if "connection" in row else "chart" if "chart" in row else "backend" if "timestamp_utc" in row else "native"
                        at = item.get("sampled_at_ms", item.get("at_ms"))
                        if at is None and row.get("timestamp_utc"):
                            at = int(datetime.fromisoformat(row["timestamp_utc"]).timestamp() * 1000)
                        if at is None:
                            continue
                        item.update(at_ms=at, layer=layer, source=source)
                        for key in ("backend_pid", "backend_process_start_id", "native_run_id", "native_pid"):
                            if key in row:
                                item[key] = row[key]
                        if item.get("sample_monotonic_ms") is not None:
                            item["monotonic_ms"] = item["sample_monotonic_ms"]
                        identity = (layer, item.get("renderer_id", row.get("run_id", row.get("native_run_id"))),
                                    item.get("sample_sequence", item.get("sequence")))
                        if identity[1] is not None and identity[2] is not None:
                            if identity in seen:
                                if item.get("incident_id"):
                                    seen[identity]["incident_id"] = item["incident_id"]
                                continue
                            seen[identity] = item
                        rows.append(item)
                    except (ValueError, TypeError, OverflowError):
                        problems.append({**source, "reason": "invalid_record"})
        except OSError:
            problems.append({"file": str(path), "reason": "read_failed"})
    return rows, problems


def duration(start, end, start_mono=None, end_mono=None):
    """함수 이름: duration()
    기능: 같은 실행의 단조 시계를 우선하며 벽시계 변경/역행은 명시한다.
    인자: start/end -> 벽시계 ms, start_mono/end_mono -> 같은 시계의 ms
    반환값: 시간과 계산 근거
    작성 날짜: 2026/09/16
    """
    if start is None or end is None:
        return {"ms": None, "basis": "missing"}
    wall = end - start
    if start_mono is not None and end_mono is not None:
        elapsed = end_mono - start_mono
        return {"ms": elapsed if elapsed >= 0 else None,
                "basis": "monotonic" if elapsed >= 0 else "clock_invalid",
                "wall_clock_jump": abs(wall - elapsed) > 1000}
    return {"ms": wall if wall >= 0 else None,
            "basis": "wall_clock_unverified" if wall >= 0 else "clock_invalid"}


def classify(samples, evidence, related, start, end):
    """함수 이름: classify()
    기능: 독립 관측과 사건 구간에 속하는 직접 OS 증거만 판정 근거로 쓴다.
    인자: samples/evidence/related -> 해당 실행 기록, start/end -> 사건 벽시계 구간
    반환값: 판정·확실성·누락된 증거·근거 위치
    작성 날짜: 2026/09/16
    """
    collections = [row for row in evidence if row.get("event") == "os_evidence"]
    direct = []
    for row in collections:
        for event in row.get("result", {}).get("events", []):
            try:
                at = int(datetime.fromisoformat(event["timestamp"]).timestamp() * 1000)
            except (ValueError, KeyError, TypeError):
                continue
            # 수집 범위 5분 안의 다른 장애/다른 PID는 현재 사건의 직접 증거가 아니다.
            pids = {sample.get("native_pid") for sample in samples} | {sample.get("backend_pid") for sample in samples}
            if event.get("target_pid") in pids - {None} and start - 5000 <= at <= end:
                if event.get("event") in {"process_suspend_reported", "process_throttle_reported", "app_nap_reported"}:
                    direct.append(row)
    sleep = [row for row in evidence if row.get("event") in {"system_will_sleep", "system_did_wake"}
             and start - 5000 <= row["at_ms"] <= end + 5000]
    missing_renderer_signal = [row for row in samples if (row.get("renderer_age_ms") or 0) >= 15_000
                and row.get("main_thread_age_ms", 99999) < 15_000 and row.get("probe_status") == "ok"]
    delayed_ticks = [row for row in samples if (row.get("renderer") or {}).get("timer_lag_ms", 0) >= 15_000]
    delayed_ticks += [row for row in evidence if row.get("event") == "renderer_delivery_resumed" and row.get("timer_lag_ms", 0) >= 15_000]
    renderer = missing_renderer_signal if delayed_ticks else []
    backend = [row for row in samples if row.get("probe_status") not in {"ok", "not_ready"}]
    engine = [row for row in samples if (row.get("backend") or {}).get("last_runtime_cycle_monotonic_ms") is not None
              and row["backend"]["monotonic_ms"] - row["backend"]["last_runtime_cycle_monotonic_ms"] >= 15_000]
    exchange = [row for row in related if row.get("event") == "stream_unavailable"]
    exchange += [row for row in samples if any((row.get("backend") or {}).get(key) == "offline"
                 and start <= ((row.get("backend") or {}).get("streams_checked_at_ms") or 0) <= end
                 for key in ("market_stream", "account_stream"))]
    exited = [row for row in evidence if row.get("event") == "backend_exited" and start - 10_000 <= row["at_ms"] <= end]
    issues = []
    for condition, label in ((renderer, "renderer_execution_delayed"), (backend, "backend_probe_failed"),
                             (engine, "backend_processing_delayed"), (exchange, "exchange_stream_failure"),
                             (exited, "backend_exited"), (sleep, "system_sleep"), (direct, "os_execution_restriction")):
        if condition:
            issues.append(label)
    main = [row for row in samples if row.get("main_thread_age_ms", 0) >= 15_000]
    native = [row for row in samples if row.get("native_timer_lag_ms", 0) >= 10_000]
    cause, root, root_confidence = "ui_receive_gap", "unknown", "unknown"
    if missing_renderer_signal and not renderer:
        cause = "renderer_signal_missing"
        issues.append(cause)
    if main:
        cause = "native_main_thread_delayed"
        issues.append(cause)
    if native:
        cause = "native_execution_delayed"
        issues.append(cause)
    if renderer:
        cause = "renderer_execution_delayed"
        if any((row.get("environment") or {}).get("occluded") for row in renderer):
            root, root_confidence = "background_execution_delay", "inferred"
    elif backend:
        cause = "backend_probe_failed"  # 실패한 HTTP probe만으로 process 정지를 단정하지 않는다.
    elif engine:
        cause = "backend_processing_delayed"
    elif exchange:
        cause = "exchange_stream_failure"
    if exited:
        cause, root, root_confidence = "backend_exited", "native_observed_process_exit", "confirmed"
    if direct:
        cause, root, root_confidence = "os_execution_restriction", "os_reported_execution_restriction", "confirmed"
    if sleep:
        cause, root, root_confidence = "system_sleep", "os_sleep_notification", "confirmed"
    missing = []
    if not samples:
        missing.append("native_observer_unavailable_in_this_log_version")
    if not direct and not sleep:
        missing.append("no_direct_os_execution_evidence")
    if missing_renderer_signal and not delayed_ticks:
        missing.append("renderer_timer_evidence_missing_ipc_delay_not_excluded")
    if backend and not exited:
        missing.append("probe_failure_does_not_distinguish_loopback_from_backend_hang")
    if any(row.get("dropped_before", row.get("dropped_records_before", 0)) or row.get("write_failures", 0) for row in related + samples + evidence):
        missing.append("diagnostic_records_lost")
    references = direct + sleep + renderer[:2] + delayed_ticks[:2] + missing_renderer_signal[:1] + backend[:2] + engine[:2] + exchange[:2] + exited
    return {"cause": cause, "confidence": "confirmed", "observed_issues": issues,
            "root_cause": root, "root_confidence": root_confidence, "missing_evidence": missing,
            "os_collection_statuses": [row.get("result", {}).get("status", row.get("status")) for row in collections],
            "evidence": [row["source"] for row in references if "source" in row]}


def analyze(rows, problems=()):
    """함수 이름: analyze()
    기능: UI 사건과 화면 없이 감지된 native 사건을 실행 식별자별로 분석한다.
    인자: rows -> 정규화 기록, problems -> 읽기 실패
    반환값: 비밀·계좌 정보를 포함하지 않는 보고서
    작성 날짜: 2026/09/16
    """
    groups, native_groups = defaultdict(list), defaultdict(list)
    for row in rows:
        if row.get("incident_id") and row["layer"] == "ui":
            groups[(row.get("session_id"), row.get("renderer_id"), row["incident_id"])].append(row)
        if row.get("incident_id") and row["layer"] == "native":
            native_groups[(row.get("native_run_id"), row["incident_id"])].append(row)
    incidents = []
    for (session, renderer, incident_id), chain in groups.items():
        chain.sort(key=lambda row: row.get("sequence", row["at_ms"]))
        first = next((row for row in chain if row.get("event") == "first_failure"), None)
        if first is None:
            continue
        ready = next((row for row in chain[chain.index(first) + 1:] if row.get("event") == "connection_ready"), None)
        start = first.get("last_received_at_ms") or first["at_ms"]
        end = ready["at_ms"] if ready else chain[-1]["at_ms"]
        low, high = min(start, end), max(start, end)
        # 기존 로그도 session과 연결된 backend run_id로 묶는다. PID 단독 join은 금지한다.
        backend_runs = {row.get("run_id") for row in rows if row["layer"] == "backend"
                        and row.get("details", {}).get("transport_session_id") == session} - {None}
        related = [row for row in rows if low <= row["at_ms"] <= high and row["layer"] == "backend"
                   and row.get("run_id") in backend_runs]
        samples = [row for row in rows if row.get("event") == "runtime_sample" and low <= row["at_ms"] <= high
                   and ((first.get("backend_process_start_id") is not None and row.get("backend_process_start_id") == first["backend_process_start_id"])
                        or (row.get("renderer") or {}).get("renderer_id") == renderer)]
        runs = ({row.get("native_run_id") for row in samples} | {first.get("native_run_id")}) - {None}
        evidence = [row for row in rows if row.get("native_run_id") in runs and low - 5000 <= row["at_ms"] <= high + 60_000]
        result = classify(samples, evidence, related, low, high)
        result["evidence"] = [first["source"]] + ([ready["source"]] if ready else []) + result["evidence"]
        counts = Counter(row.get("event") for row in related)
        closed = [row for row in related if row.get("event") == "ui_stream_closed"]
        opened = [row for row in related if row.get("event") == "ui_stream_opened"]
        socket_gap = duration(None, None)
        if len(closed) == len(opened) == 1:
            socket_gap = duration(closed[0]["at_ms"], opened[0]["at_ms"], closed[0].get("monotonic_ms"), opened[0].get("monotonic_ms"))
            result["evidence"] += [closed[0]["source"], opened[0]["source"]]
        else:
            result["missing_evidence"].append("unambiguous_socket_close_open_pair_missing")
        ages = [sample["backend"]["monotonic_ms"] - sample["backend"]["last_runtime_cycle_monotonic_ms"]
                for sample in samples if (sample.get("backend") or {}).get("last_runtime_cycle_monotonic_ms") is not None]
        result.update(incident_id=incident_id, renderer_id=renderer, session_id=session,
                      last_received_at_ms=first.get("last_received_at_ms"), detected_at_ms=first["at_ms"],
                      recovered_at_ms=ready["at_ms"] if ready else None,
                      ui_receive_gap=duration(first.get("last_received_at_ms"), ready["at_ms"] if ready else None,
                          first.get("last_received_monotonic_ms"), ready.get("monotonic_ms") if ready else None),
                      detection_delay=duration(first.get("last_received_at_ms"), first["at_ms"], first.get("last_received_monotonic_ms"), first.get("monotonic_ms")),
                      recovery_time=duration(first["at_ms"], ready["at_ms"] if ready else None, first.get("monotonic_ms"), ready.get("monotonic_ms") if ready else None),
                      observed_socket_gap=socket_gap, engine_observed_max_cycle_age_ms=max(ages, default=None),
                      engine_gap_basis="sampled_age_lower_bound" if ages else "missing",
                      backend_market_inputs=counts["market_input_observed"], backend_evaluations=counts["strategy_evaluated"],
                      exchange_outage_duration_ms=None)
        if result["ui_receive_gap"].get("wall_clock_jump"):
            result["missing_evidence"].append("cross_layer_wall_clock_alignment_uncertain")
        incidents.append(result)
    native_incidents = []
    for (run, incident_id), chain in native_groups.items():
        chain.sort(key=lambda row: row.get("monotonic_ms", row["at_ms"]))
        first = next((row for row in chain if row.get("event") == "incident_started"), None)
        if first is None:
            continue
        last = next((row for row in chain if row.get("event") == "incident_recovered"), None)
        samples = [row for row in chain if row.get("event") == "runtime_sample"]
        start = min((row["at_ms"] - (row.get("renderer_age_ms") or 0) for row in samples if (row.get("renderer_age_ms") or 0) >= 15_000), default=first["at_ms"])
        end = last["at_ms"] if last else max(row["at_ms"] for row in chain)
        evidence = [row for row in rows if row.get("native_run_id") == run and start - 5000 <= row["at_ms"] <= end + 60_000]
        result = classify(samples, evidence, [], min(start, end), max(start, end))
        result.update(incident_id=incident_id, native_run_id=run,
                      detected_at_ms=first["at_ms"], recovered_at_ms=last["at_ms"] if last else None,
                      recovery_time=duration(first["at_ms"], last["at_ms"] if last else None, first.get("monotonic_ms"), last.get("monotonic_ms") if last else None),
                      heartbeat_gap_lower_bound_ms=max((row.get("renderer_age_ms") or 0 for row in samples), default=None))
        result["evidence"].append(first["source"])
        native_incidents.append(result)
    return {"schema_version": 2, "incidents": incidents, "native_incidents": native_incidents, "read_problems": list(problems)}


def main():
    """함수 이름: main()
    기능: 원본을 보존하고 JSON/한국어 요약을 새 출력 디렉터리에 생성한다.
    인자: CLI --log-root, --output
    반환값: 없음
    작성 날짜: 2026/09/16
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    rows, problems = read_records(args.log_root)
    report = analyze(rows, problems)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    lines = ["# 연결 장애 분석", "", "화면 수신 공백은 바이낸스 API 단절 시간이 아닙니다.", ""]
    for incident in report["incidents"] + report["native_incidents"]:
        lines.extend([f"- {incident['incident_id']}: {incident['cause']} ({incident['confidence']})",
                      f"  근본 원인: {incident['root_cause']} ({incident['root_confidence']})",
                      f"  화면 공백: {incident.get('ui_receive_gap', {}).get('ms')}ms; 복구: {incident['recovery_time']['ms']}ms",
                      f"  미확보 증거: {', '.join(incident['missing_evidence']) or '없음'}"])
    (output / "report.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({"incidents": len(report["incidents"]), "native_incidents": len(report["native_incidents"]), "read_problems": len(problems)}))


if __name__ == "__main__":
    main()
