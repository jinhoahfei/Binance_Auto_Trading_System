"""양 OS에서 주문 없이 application 회귀 검사를 같은 순서로 실행한다."""

from collections.abc import Mapping
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def verification_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """
    함수 이름: verification_environment()
    기능: 주문 opt-in과 credential을 제거하고 platform별 경로로 검증 환경을 만든다.
    인자: environment -> 부모 환경
    반환값: 외부 주문을 차단한 child 환경
    작성 날짜: 2026/09/27
    """
    result = {
        name: value for name, value in environment.items()
        if not name.upper().startswith(("BINANCE_", "PYTHON", "TAURI_CONFIG"))
    }
    result.update({
        "BINANCE_RUN_TESTNET": "0", "BINANCE_RUN_TESTNET_ORDERS": "0",
        "BINANCE_RUN_PHASE13_PUBLIC_CASE2": "0", "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join((str(ROOT / "backend/src"), str(ROOT))),
    })
    if sys.platform == "win32":
        result["TAURI_CONFIG"] = json.dumps({"bundle": {"active": False, "externalBin": []}})
    return result


def verification_steps() -> list[tuple[str, Path, list[str]]]:
    """
    함수 이름: verification_steps()
    기능: shell shim 없이 실행하는 backend/UI/native 검사 목록을 만든다.
    인자: 없음
    반환값: 단계명, 작업 directory, argument 목록
    작성 날짜: 2026/09/27
    """
    ui = ROOT / "UI"
    rust = ui / "apps/desktop/src-tauri"
    return [
        ("backend", ROOT / "backend", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"]),
        ("windows-tooling", ROOT, [sys.executable, "-m", "unittest", "scripts.test_windows_development", "scripts.test_package_sidecar_windows", "scripts.test_windows_credentials", "scripts.test_windows_diagnostics", "-q"]),
        ("ui-contracts", ROOT, [sys.executable, "backend/scripts/generate_ui_contracts.py", "--check"]),
        ("deterministic-replay", ROOT, [sys.executable, "scripts/phase13_deterministic_replay.py", "backend/tests/fixtures/phase13/canonical_fault_trace.json", "--repeat", "5", "--quiet"]),
        ("ui-tests", ui, ["node", "node_modules/vitest/vitest.mjs", "run"]),
        ("ui-types", ui, ["node", "node_modules/typescript/bin/tsc", "-b", "--pretty", "false"]),
        ("ui-build", ui, ["node", "node_modules/vite/bin/vite.js", "build"]),
        ("rust-tests", rust, ["cargo", "test", "--locked"]),
        ("rust-clippy", rust, ["cargo", "clippy", "--locked", "--all-targets", "--", "-D", "warnings"]),
    ]


def main() -> int:
    """
    함수 이름: main()
    기능: 첫 실패를 전파하며 단계별 결과만 출력한다.
    인자: --list이면 실행 없이 단계명을 출력
    반환값: 모두 통과 0, 실패 1
    작성 날짜: 2026/09/27
    """
    if sys.argv[1:] not in ([], ["--list"]):
        print("Usage: check_platform.py [--list]", file=sys.stderr)
        return 1
    environment = verification_environment(os.environ)
    for name, directory, command in verification_steps():
        print(f"check_platform: {name}", flush=True)
        if sys.argv[1:] == ["--list"]:
            continue
        try:
            result = subprocess.run(command, cwd=directory, env=environment, check=False)
        except OSError:
            print(f"check_platform: Could not start {name}; install the required development tools.", file=sys.stderr)
            return 1
        if result.returncode != 0:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
