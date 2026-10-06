"""별도 WebView2 프로필과 읽기 전용 fixture에서 실제 renderer 충돌 복구를 검사한다."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys


def read_native_records(output: Path) -> list[dict]:
    """
    함수 이름: read_native_records()
    기능: 검증 디렉터리의 native JSONL 진단만 읽는다.
    인자: output -> 시나리오 결과 디렉터리
    반환값: 완전한 JSON 진단 행 목록
    작성 날짜: 2026/10/04
    """
    records = []
    for path in sorted((output / "native").glob("*.log")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def validate_scenario(output: Path, scenario: str, exit_code: int) -> dict:
    """
    함수 이름: validate_scenario()
    기능: native 정책 값과 실제 ProcessFailed·reload·새 renderer 증거를 함께 확인한다.
    인자: output -> 시나리오 결과 경로, scenario -> 시나리오 이름, exit_code -> native 종료 코드
    반환값: payload 없는 검증 결과
    작성 날짜: 2026/10/04
    """
    result_path = output / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
    records = read_native_records(output)
    failed = [record for record in records if record.get("event") == "webview_process_failed"]
    reloaded = [record for record in records if record.get("event") == "renderer_recovery_reload"
                and record.get("outcome") == "reload_dispatched"]
    ready = [record for record in records if record.get("event") == "renderer_recovery_ready"]
    expected_failures = 4 if scenario == "limit" else 1
    expected_reloads = {"recover": 1, "limit": 3, "shutdown": 0, "stopped": 0}[scenario]
    passed = (exit_code == 0 and result.get("passed") is True
              and len(failed) == expected_failures and len(reloaded) == expected_reloads
              and len(ready) == expected_reloads)
    return {"scenario": scenario, "passed": passed, "exit_code": exit_code,
            "process_failed_events": len(failed), "reloads": len(reloaded), "ready_events": len(ready),
            "native_result": result}


def main() -> int:
    """
    함수 이름: main()
    기능: 네 개의 격리된 native 앱 실행을 한 번씩 검사하고 결과를 저장한다.
    인자: --output -> 새 결과 디렉터리, --scenario -> all 또는 단일 시나리오
    반환값: 모든 시나리오 통과 시 0, 실패·미완료 시 1
    작성 날짜: 2026/10/04
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scenario", choices=("all", "recover", "limit", "shutdown", "stopped"), default="all")
    arguments = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Windows WebView2 is required")
    output = arguments.output.resolve()
    if output.exists():
        parser.error("output must not exist")
    root = Path(__file__).resolve().parents[1]
    binary = root / "UI/apps/desktop/src-tauri/target/debug/renderer-recovery-smoke.exe"
    if not binary.is_file():
        parser.error("Build the renderer-recovery-smoke feature first")
    output.mkdir(parents=True)
    scenarios = ("recover", "limit", "shutdown", "stopped") if arguments.scenario == "all" else (arguments.scenario,)
    results = []
    for scenario in scenarios:
        with (output / f"{scenario}.runner.log").open("x", encoding="utf-8") as stream:
            try:
                result = subprocess.run([str(binary), "--output", str(output / scenario), "--scenario", scenario],
                                        cwd=root, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                                        timeout=150, creationflags=subprocess.CREATE_NO_WINDOW, check=False)
                validation = validate_scenario(output / scenario, scenario, result.returncode)
            except subprocess.TimeoutExpired:
                validation = {"scenario": scenario, "passed": False, "error": "native_timeout"}
        results.append(validation)
        print(json.dumps(validation, ensure_ascii=False), flush=True)
    summary = {"passed": all(result["passed"] for result in results), "orders_enabled": False, "results": results}
    (output / "validation.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
