"""실제 차트 WebView2 검증을 제한된 메모리 감시와 독립 결과 파일로 실행한다."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time


class MetricRange:
    """
    클래스 이름: MetricRange
    기능: 전체 시계열을 메모리에 모으지 않고 처음·마지막·최소·최대 수치만 보관한다.
    작성 날짜: 2026/10/04
    """

    def __init__(self):
        """
        함수 이름: MetricRange.__init__()
        기능: 빈 수치 요약을 초기화한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/10/04
        """
        self.count = 0
        self.first = None
        self.last = None
        self.minimum = None
        self.maximum = None

    def observe(self, value):
        """
        함수 이름: observe()
        기능: 유효한 수치만 상수 크기의 요약에 반영한다.
        인자: value -> byte 또는 count 단위 관찰값
        반환값: 없음
        작성 날짜: 2026/10/04
        """
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return
        self.count += 1
        if self.first is None:
            self.first = value
        self.last = value
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)

    def summary(self):
        """
        함수 이름: summary()
        기능: 관찰 범위를 직렬화할 수 있는 고정 JSON 값으로 반환한다.
        인자: 없음
        반환값: count·first·last·min·max 수치
        작성 날짜: 2026/10/04
        """
        return {"count": self.count, "first": self.first, "last": self.last,
                "minimum": self.minimum, "maximum": self.maximum}


class SoakObservations:
    """
    클래스 이름: SoakObservations
    기능: 렌더러 수치와 프로세스별 메모리를 스트리밍 요약하고 중단 조건을 판정한다.
    작성 날짜: 2026/10/04
    """

    def __init__(self, private_limit_bytes):
        """
        함수 이름: SoakObservations.__init__()
        기능: 예상 가능한 표본 필드와 메모리 상한을 초기화한다.
        인자: private_limit_bytes -> renderer 전용 commit 중단 한도
        반환값: 없음
        작성 날짜: 2026/10/04
        """
        self.private_limit_bytes = private_limit_bytes
        self.metrics = {name: MetricRange() for name in (
            "elapsed_ms", "ticks", "candle_count", "dom_nodes", "measure_count",
            "generated_measure_detail_characters", "js_heap_used_bytes", "js_heap_total_bytes", "errors",
        )}
        self.processes = {}
        self.development_build = None
        self.cleanup_enabled = None
        self.process_failed_events = 0
        self.stop_reason = None

    def observe(self, record):
        """
        함수 이름: observe()
        기능: 고정 event의 숫자만 수집하고 메모리 상한·렌더러 종료를 감시한다.
        인자: record -> native 진단 JSON 객체
        반환값: 없음
        작성 날짜: 2026/10/04
        """
        if record.get("event") == "renderer_soak_sample":
            sample = record["sample"]
            for name, metric in self.metrics.items():
                metric.observe(sample.get(name))
            self.development_build = sample.get("development_build")
            self.cleanup_enabled = sample.get("cleanup_enabled")
        if record.get("event") == "webview_process_failed":
            self.process_failed_events += 1
            self.stop_reason = "webview_process_failed"
        if record.get("event") != "runtime_sample":
            return
        resources = record.get("resources", {})
        process_samples = [resources.get("native"), resources.get("backend")]
        process_samples.extend(resources.get("webview2", {}).get("processes", []))
        for sample in process_samples:
            if not isinstance(sample, dict) or sample.get("status") != "ok":
                continue
            process_key = f'{sample["kind"]}:{sample["process_id"]}:{sample.get("creation_filetime_100ns")}'
            if process_key not in self.processes:
                if len(self.processes) >= 128:
                    self.stop_reason = "process_churn_limit"
                    continue
                self.processes[process_key] = {"kind": sample["kind"], "process_id": sample["process_id"],
                                               "private_commit_bytes": MetricRange(), "working_set_bytes": MetricRange()}
            for name in ("private_commit_bytes", "working_set_bytes"):
                self.processes[process_key][name].observe(sample.get(name))
            if sample["kind"] == "renderer" and sample.get("private_commit_bytes", 0) > self.private_limit_bytes:
                self.stop_reason = "renderer_private_commit_limit"

    def summary(self):
        """
        함수 이름: summary()
        기능: 과정 전체를 적재하지 않은 수치 요약과 제한 판정을 반환한다.
        인자: 없음
        반환값: 표본·프로세스·중단 조건 JSON
        작성 날짜: 2026/10/04
        """
        return {"metrics": {name: metric.summary() for name, metric in self.metrics.items()},
                "processes": [{key: value.summary() if isinstance(value, MetricRange) else value
                               for key, value in process.items()} for process in self.processes.values()],
                "development_build": self.development_build, "cleanup_enabled": self.cleanup_enabled,
                "process_failed_events": self.process_failed_events, "stop_reason": self.stop_reason}


def read_new_records(directory, offsets):
    """
    함수 이름: read_new_records()
    기능: 분할 로그에서 완전히 기록된 새 JSON 행만 제한된 행 크기로 읽는다.
    인자: directory -> native 로그 디렉터리, offsets -> 파일별 확정 읽기 위치
    반환값: 새 JSON 객체 generator
    작성 날짜: 2026/10/04
    """
    for path in sorted(directory.glob("*.log")):
        with path.open("rb") as stream:
            stream.seek(offsets.get(path.name, 0))
            while True:
                line = stream.readline(1024 * 1024)
                if not line.endswith(b"\n"):
                    break
                offsets[path.name] = stream.tell()
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(record, dict):
                    yield record


def main():
    """
    함수 이름: main()
    기능: 격리 native 검증 앱을 실행하고 완료·중단 여부와 수치 증거를 독립 파일에 저장한다.
    인자: --output 새 경로, --seconds 20~86400, 선택적 --devtools·--binary·메모리 상한
    반환값: 워크로드 완료 시 0, 실패·중단 시 1; 메모리 안정성을 자동 확정하지 않음
    작성 날짜: 2026/10/04
    """
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--devtools", action="store_true")
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--max-renderer-private-mib", type=int, default=768)
    parser.add_argument("--binary", type=Path,
                        default=root / "UI/apps/desktop/src-tauri/target/debug/renderer-recovery-smoke.exe")
    arguments = parser.parse_args()
    if sys.platform != "win32" or not 20 <= arguments.seconds <= 86_400:
        parser.error("Windows and seconds in [20, 86400] are required")
    if not 128 <= arguments.max_renderer_private_mib <= 2048:
        parser.error("renderer private memory limit must be in [128, 2048] MiB")
    output = arguments.output.resolve()
    if output.exists() or not output.parent.is_dir() or not arguments.binary.is_file():
        parser.error("a new output path, existing parent, and built smoke binary are required")
    observations = SoakObservations(arguments.max_renderer_private_mib * 1024 * 1024)
    offsets = {}
    command = [str(arguments.binary.resolve()), "--output", str(output), "--scenario", "soak",
               "--seconds", str(arguments.seconds), "--entry", "renderer-soak.html"]
    if arguments.devtools:
        command.append("--devtools")
    if arguments.capture:
        command.append("--capture")
    started = time.monotonic()
    stop_requested_at = None
    with output.with_suffix(".runner.log").open("x", encoding="utf-8") as stream:
        process = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL, stdout=stream,
                                   stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        while process.poll() is None:
            for record in read_new_records(output / "native", offsets):
                observations.observe(record)
            if time.monotonic() - started > arguments.seconds + 60:
                observations.stop_reason = "native_deadline_exceeded"
            if observations.stop_reason and stop_requested_at is None:
                if output.is_dir():
                    (output / "stop-request.json").write_text('{"stop":true}\n', encoding="utf-8")
                stop_requested_at = time.monotonic()
            if stop_requested_at is not None and time.monotonic() - stop_requested_at > 30:
                # 이 runner가 만든 검증 PID만 종료한다. private fixture pipe는 EOF로 정리된다.
                process.terminate()
                process.wait(timeout=10)
                break
            time.sleep(1)
    for record in read_new_records(output / "native", offsets):
        observations.observe(record)
    result_path = output / "result.json"
    native_result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else {}
    workload_completed = (process.returncode == 0 and native_result.get("passed") is True
                          and observations.stop_reason is None and observations.metrics["ticks"].count > 0
                          and observations.metrics["ticks"].last > 0
                          and observations.metrics["candle_count"].minimum == 1000
                          and observations.metrics["errors"].maximum == 0
                          and any(process["kind"] == "renderer" for process in observations.processes.values()))
    result = {"workload_completed": workload_completed, "exit_code": process.returncode,
              "requested_seconds": arguments.seconds, "wall_seconds": round(time.monotonic() - started, 2),
              "devtools": arguments.devtools, "capture": arguments.capture,
              "orders_enabled": False, "native_result": native_result,
              "memory_stability_verdict": "requires_time_series_review_not_inferred_from_no_crash",
              **observations.summary()}
    summary_path = output / "memory-summary.json" if output.is_dir() else output.with_suffix(".memory-summary.json")
    summary_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"workload_completed": workload_completed, "summary": str(summary_path),
                      "stop_reason": observations.stop_reason}, ensure_ascii=False), flush=True)
    return 0 if workload_completed else 1


if __name__ == "__main__":
    raise SystemExit(main())
