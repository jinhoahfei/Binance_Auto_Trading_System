#!/usr/bin/env python3
"""Phase 12 정식 release identity와 notarization profile을 fail-closed 검증한다."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable, Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
COMMAND_TIMEOUT_SECONDS = 60
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
DEVELOPER_IDENTITY_PATTERN = re.compile(
    r'Developer ID Application: [^"\r\n]+ \(([A-Z0-9]{10})\)\Z'
)
SECURITY_IDENTITY_LINE_PATTERN = re.compile(
    r'\s*\d+\)\s+[0-9A-Fa-f]{40}\s+"([^"\r\n]+)"\s*\Z'
)

# Release child process에 certificate·account secret 원문을 상속하지 않는다.
SENSITIVE_APPLE_ENVIRONMENT_NAMES = {
    "AC_PASSWORD",
    "APPLE_API_ISSUER",
    "APPLE_API_KEY",
    "APPLE_API_KEY_ID",
    "APPLE_API_KEY_PATH",
    "APPLE_CERTIFICATE",
    "APPLE_CERTIFICATE_PASSWORD",
    "APPLE_ID",
    "APPLE_PASSWORD",
    "ASC_API_KEY",
    "ASC_API_KEY_PATH",
    "ASC_ISSUER_ID",
}
SENSITIVE_APPLE_ENVIRONMENT_MARKERS = (
    "API_KEY",
    "CERTIFICATE",
    "CREDENTIAL",
    "PASSWORD",
    "PRIVATE_KEY",
    "SECRET",
    "TOKEN",
)
SENSITIVE_BINANCE_ENVIRONMENT_NAMES = {
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "BINANCE_TESTNET_API_KEY",
    "BINANCE_TESTNET_API_SECRET",
}
SENSITIVE_BINANCE_ENVIRONMENT_MARKERS = (
    "API_KEY",
    "API_SECRET",
    "CREDENTIAL",
    "PASSWORD",
    "PRIVATE_KEY",
    "SECRET",
    "TOKEN",
)

# Git repository 선택과 xcrun toolchain 탐색을 caller가 environment로
# 바꿔치기하지 못하게 한다. Python launcher 제어도 native helper에
# 전파할 이유가 없으므로 같이 제거한다.
CONTROL_ENVIRONMENT_NAMES = {
    "__PYVENV_LAUNCHER__",
    "COMMAND_MODE",
    "DEVELOPER_DIR",
    "LD_PRELOAD",
    "SDKROOT",
    "TOOLCHAINS",
    "VIRTUAL_ENV",
    "VIRTUAL_ENV_PROMPT",
}
CONTROL_ENVIRONMENT_PREFIXES = (
    "_PYTHON",
    "DYLD_",
    "GIT_",
    "PYTHON",
    "XCODE_",
)

# 검증 입력은 child environment에 중복 상속하지 않고, 필요한
# profile 이름만 notarytool의 explicit argument로 전달한다.
PREFLIGHT_INPUT_ENVIRONMENT_NAMES = {
    "APPLE_SIGNING_IDENTITY",
    "NOTARY_PROFILE",
}


class ReleaseIdentityPreflightError(RuntimeError):
    """
    클래스 이름: ReleaseIdentityPreflightError
    기능: 원본 command output을 포함하지 않는 release identity 사전 검사 실패를 나타낸다.
    작성 날짜: 2026/08/24
    """


@dataclass(frozen=True)
class ReleaseIdentityEvidence:
    """
    클래스 이름: ReleaseIdentityEvidence
    기능: 검증된 non-secret commit, Developer ID identity와 Team ID를 보존한다.
    작성 날짜: 2026/08/24
    """

    commit_id: str
    identity: str
    team_id: str


CommandRunner = Callable[
    [Sequence[str], Path, Mapping[str, str]],
    subprocess.CompletedProcess[bytes],
]


def run_command(
    command: Sequence[str],
    working_directory: Path,
    process_environment: Mapping[str, str],
) -> subprocess.CompletedProcess[bytes]:
    """
    함수 이름: run_command()
    기능: shell 없이 bounded release preflight command를 실행하고 출력을 memory에만 포착한다.
    인자: command -> 고정 executable과 검증된 인자 목록
        working_directory -> repository root
        process_environment -> secret 제거 environment
    반환값: captured subprocess 결과
    작성 날짜: 2026/08/24
    """

    try:
        return subprocess.run(
            list(command),
            cwd=working_directory,
            env=dict(process_environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        raise ReleaseIdentityPreflightError(
            "release identity preflight command failed"
        ) from None


def create_sanitized_environment(
    source_environment: Mapping[str, str],
) -> dict[str, str]:
    """
    함수 이름: create_sanitized_environment()
    기능: release tool에 secret·repository·toolchain·Python 제어값을 상속하지 않는 environment를 만든다.
    인자: source_environment -> caller environment
    반환값: known secret 변수가 제거된 environment 사본
    작성 날짜: 2026/08/24
    """

    process_environment = dict(source_environment)
    for environment_name in tuple(process_environment):
        normalized_name = environment_name.upper()
        is_control_value = (
            normalized_name in CONTROL_ENVIRONMENT_NAMES
            or normalized_name.startswith(CONTROL_ENVIRONMENT_PREFIXES)
        )
        is_preflight_input = (
            normalized_name in PREFLIGHT_INPUT_ENVIRONMENT_NAMES
        )
        is_named_apple_secret = (
            normalized_name in SENSITIVE_APPLE_ENVIRONMENT_NAMES
        )
        is_future_apple_secret = normalized_name.startswith(
            ("AC_", "APPLE_", "ASC_")
        ) and any(
            marker in normalized_name
            for marker in SENSITIVE_APPLE_ENVIRONMENT_MARKERS
        )
        is_named_binance_secret = (
            normalized_name in SENSITIVE_BINANCE_ENVIRONMENT_NAMES
        )
        is_future_binance_secret = normalized_name.startswith(
            "BINANCE_"
        ) and any(
            marker in normalized_name
            for marker in SENSITIVE_BINANCE_ENVIRONMENT_MARKERS
        )
        if (
            is_control_value
            or is_preflight_input
            or is_named_apple_secret
            or is_future_apple_secret
            or is_named_binance_secret
            or is_future_binance_secret
        ):
            process_environment.pop(environment_name, None)
    return process_environment


def read_successful_stdout(
    process_result: subprocess.CompletedProcess[bytes],
) -> str:
    """
    함수 이름: read_successful_stdout()
    기능: 성공 command의 bounded stdout을 UTF-8 text로 decode하고 실패 상세는 폐기한다.
    인자: process_result -> captured command 결과
    반환값: decode된 stdout
    작성 날짜: 2026/08/24
    """

    if process_result.returncode != 0 or not isinstance(
        process_result.stdout,
        (bytes, bytearray),
    ):
        raise ReleaseIdentityPreflightError("release preflight command was rejected")
    try:
        return bytes(process_result.stdout).decode("utf-8")
    except UnicodeError:
        raise ReleaseIdentityPreflightError(
            "release preflight output was invalid"
        ) from None


def validate_notary_profile_name(notary_profile: str | None) -> str:
    """
    함수 이름: validate_notary_profile_name()
    기능: Keychain profile 이름의 non-empty·bounded·제어문자 금지 계약을 검증한다.
    인자: notary_profile -> NOTARY_PROFILE environment 값
    반환값: 검증된 profile 이름
    작성 날짜: 2026/08/24
    """

    if (
        not isinstance(notary_profile, str)
        or not notary_profile
        or notary_profile.strip() != notary_profile
        or len(notary_profile) > 255
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in notary_profile)
    ):
        raise ReleaseIdentityPreflightError("notary profile configuration is invalid")
    return notary_profile


def parse_developer_identity(signing_identity: str | None) -> tuple[str, str]:
    """
    함수 이름: parse_developer_identity()
    기능: exact Developer ID Application identity와 마지막 Team ID를 분리한다.
    인자: signing_identity -> APPLE_SIGNING_IDENTITY environment 값
    반환값: 검증된 identity와 Team ID
    작성 날짜: 2026/08/24
    """

    if not isinstance(signing_identity, str):
        raise ReleaseIdentityPreflightError("release signing identity is missing")
    identity_match = DEVELOPER_IDENTITY_PATTERN.fullmatch(signing_identity)
    if identity_match is None:
        raise ReleaseIdentityPreflightError("release signing identity is invalid")
    return signing_identity, identity_match.group(1)


def verify_release_identity_environment(
    source_environment: Mapping[str, str],
    command_runner: CommandRunner = run_command,
    repository_root: Path = REPOSITORY_ROOT,
) -> ReleaseIdentityEvidence:
    """
    함수 이름: verify_release_identity_environment()
    기능: 고정 repository의 clean commit, exact Keychain identity와 독립적으로 usable한 notary profile을 검증한다.
    인자: source_environment -> release caller environment
        command_runner -> subprocess 실행 경계
        repository_root -> 검증할 repository root
    반환값: non-secret release identity evidence
    작성 날짜: 2026/08/24
    """

    signing_identity, team_id = parse_developer_identity(
        source_environment.get("APPLE_SIGNING_IDENTITY")
    )
    notary_profile = validate_notary_profile_name(
        source_environment.get("NOTARY_PROFILE")
    )
    process_environment = create_sanitized_environment(source_environment)
    resolved_repository_root = repository_root.resolve()
    git_directory = resolved_repository_root / ".git"
    fixed_git_command = [
        "/usr/bin/git",
        f"--git-dir={git_directory}",
        f"--work-tree={resolved_repository_root}",
    ]

    # Release candidate는 environment가 가리킨 다른 Git repository가 아니라
    # script와 같은 고정 root의 tracked·untracked 변경이 없는 commit이어야 한다.
    status_result = command_runner(
        fixed_git_command
        + ["status", "--porcelain=v1", "--untracked-files=all"],
        resolved_repository_root,
        process_environment,
    )
    if read_successful_stdout(status_result):
        raise ReleaseIdentityPreflightError("release candidate worktree is dirty")

    commit_result = command_runner(
        fixed_git_command + ["rev-parse", "--verify", "HEAD^{commit}"],
        resolved_repository_root,
        process_environment,
    )
    commit_id = read_successful_stdout(commit_result).strip()
    if COMMIT_PATTERN.fullmatch(commit_id) is None:
        raise ReleaseIdentityPreflightError("release candidate commit is invalid")

    # Keychain의 유효 identity 목록에서 exact certificate 이름 하나만 선택한다.
    identity_result = command_runner(
        ["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"],
        resolved_repository_root,
        process_environment,
    )
    identity_output = read_successful_stdout(identity_result)
    matching_identities = [
        identity_match.group(1)
        for identity_line in identity_output.splitlines()
        if (identity_match := SECURITY_IDENTITY_LINE_PATTERN.fullmatch(identity_line))
        is not None
        and identity_match.group(1) == signing_identity
    ]
    if matching_identities != [signing_identity]:
        raise ReleaseIdentityPreflightError("release signing identity is unavailable")

    # Profile secret은 Keychain에서만 읽게 하고 history 성공 여부로 실제
    # 사용 가능성만 확인한다. history는 profile account와 signing
    # certificate Team이 같음을 증명하지 않으며, 그 결합은 후속 artifact gate의 범위다.
    notary_result = command_runner(
        [
            "/usr/bin/xcrun",
            "notarytool",
            "history",
            "--keychain-profile",
            notary_profile,
            "--output-format",
            "json",
        ],
        resolved_repository_root,
        process_environment,
    )
    read_successful_stdout(notary_result)

    return ReleaseIdentityEvidence(
        commit_id=commit_id,
        identity=signing_identity,
        team_id=team_id,
    )


def main() -> int:
    """
    함수 이름: main()
    기능: 실제 environment의 Phase 12 release identity gate를 secret-free 결과로 실행한다.
    인자: 없음
    반환값: 검증 성공 0, configuration·command 실패 1
    작성 날짜: 2026/08/24
    """

    try:
        verify_release_identity_environment(os.environ)
    except ReleaseIdentityPreflightError:
        print(
            "phase12-release-identity: ERROR: release preflight failed.",
            file=sys.stderr,
        )
        return 1

    print("phase12-release-identity: PASS: release identity is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
