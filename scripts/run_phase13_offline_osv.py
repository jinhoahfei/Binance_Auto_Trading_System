#!/usr/bin/env python3
"""OSV scan을 OS network sandbox와 scanner offline mode 안에서만 실행한다."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import os
from pathlib import Path
import shutil
import subprocess
import sys


# Repository-relative fixed inputs와 Apple sandbox profile은 CLI에서 바꿀 수 없게 고정한다.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LOCKFILE_PATHS = (
    Path("backend/uv.lock"),
    Path("UI/pnpm-lock.yaml"),
    Path("UI/apps/desktop/src-tauri/Cargo.lock"),
)
SANDBOX_EXECUTABLE = Path("/usr/bin/sandbox-exec")
NETWORK_DENY_PROFILE = "(version 1) (allow default) (deny network*)"
EXTERNAL_OSV_TRANSMISSION_ALLOWED = False  # 원격 전송을 허용하는 runtime mode는 없다.

# Scanner child에는 proxy와 OSV credential을 전달하지 않아 local-only 경계를 좁힌다.
PROXY_ENVIRONMENT_KEYS = frozenset(
    {
        "ALL_PROXY",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "OSV_API_KEY",
        "OSV_SCANNER_API_KEY",
    }
)

# 두 operation은 caller가 raw option을 추가할 수 없는 exact argument tuple이다.
OPERATION_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "vulnerability": (
        "--offline",
        "--offline-vulnerabilities",
        "--verbosity=error",
    ),
    "license": (
        # 명시적 빈 값은 summary를 요청하면서 다음 --offline이 allowlist 값으로 소비되는 일을 막는다.
        "--licenses=",
        "--offline",
        "--verbosity=error",
    ),
}


class OfflineOsvPolicyError(RuntimeError):
    """
    클래스 이름: OfflineOsvPolicyError
    기능: OSV executable, lockfile 또는 network-deny 정책 위반을 나타낸다.
    작성 날짜: 2026/08/29
    """


def build_offline_osv_command(
    operation: str,
    *,
    scanner_path: Path,
) -> tuple[str, ...]:
    """
    함수 이름: build_offline_osv_command()
    기능: 선택 가능한 remote flag 없이 고정 offline OSV command를 만든다.
    인자: operation -> vulnerability 또는 license
        scanner_path -> local osv-scanner executable
    반환값: shell을 사용하지 않는 exact argv tuple
    작성 날짜: 2026/08/29
    """
    if EXTERNAL_OSV_TRANSMISSION_ALLOWED:
        raise OfflineOsvPolicyError("external OSV transmission policy drifted")
    operation_arguments = OPERATION_ARGUMENTS.get(operation)
    if operation_arguments is None:
        raise OfflineOsvPolicyError("unsupported OSV operation")

    # 세 lockfile과 offline flags는 caller 입력을 받지 않는 canonical argv로만 조립한다.
    lockfile_arguments = tuple(
        argument
        for lockfile_path in LOCKFILE_PATHS
        for argument in ("--lockfile", lockfile_path.as_posix())
    )
    command = (
        os.fspath(SANDBOX_EXECUTABLE),
        "-p",
        NETWORK_DENY_PROFILE,
        os.fspath(scanner_path),
        "scan",
        "source",
        *lockfile_arguments,
        *operation_arguments,
    )

    # OS sandbox와 scanner offline flag를 둘 다 요구하고 database download flag는 금지한다.
    if (
        command.count("--offline") != 1
        or NETWORK_DENY_PROFILE not in command
        or "--download-offline-databases" in command
    ):
        raise OfflineOsvPolicyError("offline OSV command policy is invalid")
    return command


def build_offline_environment(
    source_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """
    함수 이름: build_offline_environment()
    기능: Proxy와 OSV credential을 제거한 child environment를 만든다.
    인자: source_environment -> 복제할 환경 또는 None
    반환값: network credential이 제거된 environment dictionary
    작성 날짜: 2026/08/29
    """
    selected_environment = os.environ if source_environment is None else source_environment

    # 대소문자 변형까지 제거해 inherited proxy나 API credential을 scanner에 전달하지 않는다.
    sanitized_environment = {
        key: value
        for key, value in selected_environment.items()
        if key.upper() not in PROXY_ENVIRONMENT_KEYS
    }
    return sanitized_environment


def run_offline_osv_scan(
    operation: str,
    *,
    repository_root: Path = REPOSITORY_ROOT,
    tool_finder: Callable[[str], str | None] = shutil.which,
    command_runner: Callable[..., subprocess.CompletedProcess[object]] = subprocess.run,
    source_environment: Mapping[str, str] | None = None,
) -> int:
    """
    함수 이름: run_offline_osv_scan()
    기능: Fixed lockfiles를 OS network deny와 OSV offline mode 아래에서 검사한다.
    인자: operation -> vulnerability 또는 license
        repository_root -> fixed repository root
        tool_finder -> osv-scanner executable finder
        command_runner -> subprocess-compatible injected runner
        source_environment -> sanitized child 환경의 원본 또는 None
    반환값: scanner exit status
    작성 날짜: 2026/08/29
    """
    if not callable(tool_finder) or not callable(command_runner):
        raise TypeError("tool_finder and command_runner must be callable")
    if SANDBOX_EXECUTABLE.is_symlink() or not SANDBOX_EXECUTABLE.is_file():
        raise OfflineOsvPolicyError("network sandbox is unavailable")
    if (SANDBOX_EXECUTABLE.stat().st_mode & 0o111) == 0:
        raise OfflineOsvPolicyError("network sandbox is not executable")

    # Scanner 실행 전에 모든 lockfile을 symlink가 아닌 local regular file로 제한한다.
    for lockfile_path in LOCKFILE_PATHS:
        absolute_lockfile_path = repository_root / lockfile_path
        if absolute_lockfile_path.is_symlink() or not absolute_lockfile_path.is_file():
            raise OfflineOsvPolicyError("OSV lockfile is unavailable")
    discovered_scanner = tool_finder("osv-scanner")
    if discovered_scanner is None:
        raise OfflineOsvPolicyError("osv-scanner is unavailable")
    scanner_path = Path(discovered_scanner)
    if not scanner_path.is_file() or (scanner_path.stat().st_mode & 0o111) == 0:
        raise OfflineOsvPolicyError("osv-scanner is not executable")

    command = build_offline_osv_command(
        operation,
        scanner_path=scanner_path,
    )
    environment = build_offline_environment(source_environment)

    # Shell과 inherited FD 없이 exact argv를 실행해 우회 option이나 기존 network channel을 닫는다.
    try:
        result = command_runner(
            command,
            cwd=repository_root,
            env=environment,
            check=False,
            shell=False,
            close_fds=True,
        )
    except OSError as error:
        raise OfflineOsvPolicyError("offline OSV execution failed") from error
    return int(result.returncode)


def parse_arguments(argument_values: Sequence[str] | None = None) -> argparse.Namespace:
    """
    함수 이름: parse_arguments()
    기능: 두 fixed offline operation 외의 인자를 허용하지 않는 CLI를 만든다.
    인자: argument_values -> 명시 argument 또는 실제 argv를 뜻하는 None
    반환값: argparse namespace
    작성 날짜: 2026/08/29
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=tuple(OPERATION_ARGUMENTS))
    return parser.parse_args(argument_values)


def main(argument_values: Sequence[str] | None = None) -> int:
    """
    함수 이름: main()
    기능: 선택한 offline OSV operation을 실행하고 원래 scan status를 반환한다.
    인자: argument_values -> 명시 argument 또는 실제 argv를 뜻하는 None
    반환값: scanner status, policy/tool 오류이면 2
    작성 날짜: 2026/08/29
    """
    arguments = parse_arguments(argument_values)
    try:
        return run_offline_osv_scan(arguments.operation)
    except (OfflineOsvPolicyError, OSError, TypeError, ValueError):
        # Host path, proxy, credential과 scanner raw error는 고정 오류 밖으로 반사하지 않는다.
        print("offline-osv: ERROR: local-only scan unavailable.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
