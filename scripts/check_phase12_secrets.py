#!/usr/bin/env python3
"""
Phase 12 산출물의 file content와 path component에 credential canary가 있는지 검사한다.

.env와 Keychain 값은 출력하지 않고 scanner 자체의 최소 canary 길이 정책을 적용한다.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterator, Sequence


SECRET_KEY_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL")

# 기본 하위 순회에서만 cache 이름을 제외하고 그 아래 명시 root는 검사한다.
DEFAULT_EXCLUDED_DIRECTORY_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "node_modules",
    "target",
}
SCAN_CHUNK_SIZE = 1024 * 1024

# Keychain source는 검증된 identifier와 bounded security command만 사용한다.
KEYCHAIN_SECURITY_COMMAND = "/usr/bin/security"
KEYCHAIN_READ_TIMEOUT_SECONDS = 30
KEYCHAIN_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}\Z")
MINIMUM_CREDENTIAL_CANARY_BYTES = 8
MAXIMUM_KEYCHAIN_SECRET_BYTES = 512


def parse_arguments(
    argument_values: Sequence[str] | None = None,
) -> argparse.Namespace:
    """
    함수 이름: parse_arguments()
    기능: credential source와 검사할 file·directory 목록을 command line에서 읽는다.
    인자: argument_values -> 검사할 명시 인자 또는 실제 command line을 뜻하는 None
    반환값: 검증 전 argparse namespace
    작성 날짜: 2026/08/24
    """

    # Secret 원문을 help나 기본 인자에 넣지 않고 source path와 scan target만 정의한다.
    parser = argparse.ArgumentParser(
        description="Scan artifacts for credential canaries without printing values.",
    )
    credential_source_group = parser.add_mutually_exclusive_group()
    credential_source_group.add_argument(
        "--env",
        type=Path,
        default=Path(".env"),
        help="credential canary를 읽을 .env path",
    )
    credential_source_group.add_argument(
        "--keychain-service",
        help="credential canary를 읽을 macOS Keychain generic-password service",
    )
    parser.add_argument(
        "--keychain-account",
        action="append",
        dest="keychain_accounts",
        help="Keychain에서 읽을 account identifier; account마다 한 번씩 지정",
    )
    parser.add_argument(
        "targets",
        nargs="*",
        type=Path,
        default=[Path(".")],
        help="검사할 file 또는 directory; 생략하면 repository root",
    )
    arguments = parser.parse_args(argument_values)

    # Keychain account 단독 사용과 service/account의 잘못된 형식을 scan 전에 거부한다.
    if arguments.keychain_service is None:
        if arguments.keychain_accounts is not None:
            parser.error("--keychain-account에는 --keychain-service가 필요합니다.")
    else:
        try:
            validate_keychain_source(
                arguments.keychain_service,
                arguments.keychain_accounts,
            )
        except RuntimeError as error:
            parser.error(str(error))
    return arguments  # 실제 path 존재 여부와 읽기 권한은 fail-closed scan 단계에서 검사한다.


def validate_keychain_source(
    keychain_service: str,
    keychain_accounts: Sequence[str] | None,
) -> None:
    """
    함수 이름: validate_keychain_source()
    기능: Keychain service/account identifier의 문자, 개수와 중복을 엄격히 검증한다.
    인자: keychain_service -> generic-password service identifier
        keychain_accounts -> 반복 지정된 account identifier 목록
    반환값: 없음
    작성 날짜: 2026/08/24
    """

    # argv option이나 제어문자로 해석될 수 없는 제한된 ASCII identifier만 허용한다.
    if not isinstance(keychain_service, str) or (
        KEYCHAIN_IDENTIFIER_PATTERN.fullmatch(keychain_service) is None
    ):
        raise RuntimeError("Keychain service 형식이 올바르지 않습니다.")
    if not keychain_accounts or isinstance(keychain_accounts, str):
        raise RuntimeError("Keychain account를 한 개 이상 지정해야 합니다.")

    seen_accounts: set[str] = set()
    for keychain_account in keychain_accounts:
        if not isinstance(keychain_account, str) or (
            KEYCHAIN_IDENTIFIER_PATTERN.fullmatch(keychain_account) is None
        ):
            raise RuntimeError("Keychain account 형식이 올바르지 않습니다.")
        if keychain_account in seen_accounts:
            raise RuntimeError("Keychain account를 중복 지정할 수 없습니다.")
        seen_accounts.add(keychain_account)  # 다음 account의 중복 여부만 확인하는 non-secret identifier다.


def zeroize_secret_buffer(secret_buffer: bytearray) -> None:
    """
    함수 이름: zeroize_secret_buffer()
    기능: mutable secret buffer의 모든 byte를 덮어쓴 뒤 논리 길이를 비운다.
    인자: secret_buffer -> 더 이상 사용하지 않을 credential buffer
    반환값: 없음
    작성 날짜: 2026/08/24
    """

    # 기존 할당 영역을 먼저 0으로 덮어써 clear만으로 원문이 남는 상황을 줄인다.
    for byte_index in range(len(secret_buffer)):
        secret_buffer[byte_index] = 0
    secret_buffer.clear()  # 이후 코드가 이미 폐기한 credential을 실수로 재사용하지 못하게 한다.


def clear_captured_process_output(
    process_result: subprocess.CompletedProcess[bytes],
) -> None:
    """
    함수 이름: clear_captured_process_output()
    기능: subprocess 결과의 captured output 참조를 제거하고 mutable 대체값만 zeroize한다.
    인자: process_result -> security command의 captured 결과
    반환값: 없음
    작성 날짜: 2026/08/24
    """

    # Production bytes는 immutable이므로 참조만 버리고 mutable 대체 구현은 backing memory도 덮어쓴다.
    if isinstance(process_result.stdout, bytearray):
        zeroize_secret_buffer(process_result.stdout)
    if isinstance(process_result.stderr, bytearray):
        zeroize_secret_buffer(process_result.stderr)
    process_result.stdout = b""  # immutable stdout의 결과 객체 참조도 가능한 즉시 제거한다.
    process_result.stderr = b""  # 오류 세부사항은 진단 출력에 반사하지 않고 폐기한다.


def read_keychain_credential(
    keychain_service: str,
    keychain_account: str,
) -> bytearray:
    """
    함수 이름: read_keychain_credential()
    기능: macOS Keychain generic password의 captured stdout으로 mutable canary 사본을 만든다.
    인자: keychain_service -> 검증된 generic-password service identifier
        keychain_account -> 검증된 account identifier
    반환값: 출력 금지 credential bytearray
    작성 날짜: 2026/08/24
    """

    # Command argv에는 non-secret service/account만 넣고 password는 -w stdout으로만 받는다.
    security_command = [
        KEYCHAIN_SECURITY_COMMAND,
        "find-generic-password",
        "-s",
        keychain_service,
        "-a",
        keychain_account,
        "-w",
    ]
    try:
        process_result = subprocess.run(
            security_command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=KEYCHAIN_READ_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        # Timeout exception의 partial output도 진단 문자열에 연결하지 않고 즉시 참조를 제거한다.
        if isinstance(error.output, bytearray):
            zeroize_secret_buffer(error.output)
        if isinstance(error.stderr, bytearray):
            zeroize_secret_buffer(error.stderr)
        error.output = None  # immutable partial stdout도 exception에서 분리한다.
        error.stderr = None  # security의 raw 오류를 상위 RuntimeError에 연결하지 않는다.
        raise RuntimeError("Keychain credential을 읽을 수 없습니다.") from None
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("Keychain credential을 읽을 수 없습니다.") from None

    credential_buffer = bytearray()
    try:
        # Return code와 output type을 확인해 missing item을 검사 가능한 canary로 만들지 않는다.
        if process_result.returncode != 0 or not isinstance(
            process_result.stdout,
            (bytes, bytearray),
        ):
            raise RuntimeError("Keychain credential을 읽을 수 없습니다.")
        credential_buffer.extend(process_result.stdout)
        if credential_buffer.endswith(b"\n"):
            credential_buffer.pop()
            if credential_buffer.endswith(b"\r"):
                credential_buffer.pop()
        # Packaged app의 non-empty 경계와 달리 scanner canary는 오탐을 줄이기 위해 8 byte 이상을 요구한다.
        if (
            len(credential_buffer) < MINIMUM_CREDENTIAL_CANARY_BYTES
            or len(credential_buffer) > MAXIMUM_KEYCHAIN_SECRET_BYTES
            or not all(0x21 <= byte_value <= 0x7E for byte_value in credential_buffer)
        ):
            raise RuntimeError("Keychain credential이 scanner canary 계약에 맞지 않습니다.")
        return credential_buffer
    except RuntimeError:
        zeroize_secret_buffer(credential_buffer)  # 실패한 짧은 credential도 반환 전에 폐기한다.
        raise
    finally:
        clear_captured_process_output(process_result)  # 성공과 실패 모두 captured 원문 참조를 제거한다.


def load_keychain_credential_canaries(
    keychain_service: str,
    keychain_accounts: Sequence[str] | None,
) -> dict[str, bytearray]:
    """
    함수 이름: load_keychain_credential_canaries()
    기능: 검증된 Keychain account들을 각각 읽어 memory-only canary mapping을 만든다.
    인자: keychain_service -> generic-password service identifier
        keychain_accounts -> 읽을 account identifier 목록
    반환값: account 이름과 출력 금지 mutable canary mapping
    작성 날짜: 2026/08/24
    """

    validate_keychain_source(keychain_service, keychain_accounts)
    validated_accounts = tuple(keychain_accounts or ())  # validation 뒤 immutable non-secret 순서를 고정한다.
    credential_canaries: dict[str, bytearray] = {}

    # 어느 한 item이라도 실패하면 먼저 읽은 값까지 zeroize해 부분 검사로 진행하지 않는다.
    try:
        for keychain_account in validated_accounts:
            credential_canaries[keychain_account] = read_keychain_credential(
                keychain_service,
                keychain_account,
            )
    except RuntimeError:
        zeroize_credential_canaries(credential_canaries)
        raise
    return credential_canaries


def zeroize_credential_canaries(
    credential_canaries: dict[str, bytearray],
) -> None:
    """
    함수 이름: zeroize_credential_canaries()
    기능: 모든 mutable credential canary를 덮어쓰고 mapping을 비운다.
    인자: credential_canaries -> 폐기할 credential mapping
    반환값: 없음
    작성 날짜: 2026/08/24
    """

    # 각 value backing memory를 먼저 zeroize한 뒤 non-secret key mapping도 폐기한다.
    for credential_canary in credential_canaries.values():
        zeroize_secret_buffer(credential_canary)
    credential_canaries.clear()  # caller가 폐기된 buffer를 다시 순회하지 못하게 한다.


def normalize_env_value(raw_value: str) -> str:
    """
    함수 이름: normalize_env_value()
    기능: dotenv 한 줄의 양끝 공백과 matching quote만 제거한다.
    인자: raw_value -> 등호 오른쪽 원문
    반환값: canary 비교에 사용할 문자열
    작성 날짜: 2026/08/24
    """

    normalized_value = raw_value.strip()  # dotenv 구분자 오른쪽의 비의미 공백만 제거한다.

    # 양끝 문자가 같은 quote일 때만 한 겹을 제거해 서로 다른 quote나 내부 문자는 보존한다.
    if len(normalized_value) >= 2 and normalized_value[0] == normalized_value[-1]:
        if normalized_value[0] in {"'", '"'}:
            return normalized_value[1:-1]
    return normalized_value


def load_credential_canaries(env_path: Path) -> dict[str, bytearray]:
    """
    함수 이름: load_credential_canaries()
    기능: secret 계열 key의 non-empty .env 값을 binary canary로 memory에만 읽는다.
    인자: env_path -> 검사 기준 credential file
    반환값: key 이름과 출력 금지 value bytes mapping
    작성 날짜: 2026/08/24
    """

    # Credential source는 UTF-8 text 전체를 한 번 읽되 오류에 원문 내용은 반사하지 않는다.
    try:
        env_lines = env_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        raise RuntimeError("credential source를 읽을 수 없습니다.") from None

    credential_canaries: dict[str, bytearray] = {}

    # 주석·빈 줄을 건너뛰고 secret 계열 key의 충분히 긴 값만 binary canary로 만든다.
    for raw_line in env_lines:
        stripped_line = raw_line.strip()
        if not stripped_line or stripped_line.startswith("#") or "=" not in stripped_line:
            continue

        key, raw_value = stripped_line.split("=", maxsplit=1)
        normalized_key = key.removeprefix("export ").strip()
        if not normalized_key or not any(
            marker in normalized_key.upper() for marker in SECRET_KEY_MARKERS
        ):
            continue

        normalized_value = normalize_env_value(raw_value)
        if len(normalized_value) < MINIMUM_CREDENTIAL_CANARY_BYTES:
            continue
        credential_canaries[normalized_key] = bytearray(
            normalized_value,
            "utf-8",
        )  # 출력 없이 mutable bytes 비교에만 사용한다.

    # Canary가 하나도 없으면 무검사 PASS가 되므로 configuration 오류로 중단한다.
    if not credential_canaries:
        raise RuntimeError("검사할 non-empty credential canary가 없습니다.")
    return credential_canaries


def iter_artifact_entries(target: Path, env_path: Path | None) -> Iterator[Path]:
    """
    함수 이름: iter_artifact_entries()
    기능: symlink target은 따라가지 않고 entry 이름과 scannable directory·file을 순회한다.
    인자: target -> file 또는 directory
        env_path -> content·name scan에서 제외할 .env source 또는 Keychain source를 뜻하는 None
    반환값: deterministic artifact entry iterator
    작성 날짜: 2026/08/24
    """

    resolved_env_path = (
        env_path.resolve(strict=False) if env_path is not None else None
    )  # Keychain source에는 filesystem 제외 path가 없다.

    # 명시 target 자체가 없거나 symlink이면 검사 성공으로 오인하지 않고 즉시 실패한다.
    if target.is_symlink():
        raise RuntimeError("검사 target은 symlink일 수 없습니다.")
    if target.is_file():
        if target.resolve(strict=False) != resolved_env_path:
            yield target
        return
    if not target.is_dir():
        raise RuntimeError("검사 target이 존재하지 않거나 읽을 수 없습니다.")

    yield target  # 빈 directory와 명시 root 자체의 name도 path component 검사 범위에 포함한다.

    def raise_walk_error(error: OSError) -> None:
        """
        함수 이름: raise_walk_error()
        기능: os.walk가 만난 unreadable directory를 silent skip 대신 검사 실패로 변환한다.
        인자: error -> directory 순회 중 발생한 filesystem 오류
        반환값: 정상 반환 없이 RuntimeError 발생
        작성 날짜: 2026/08/24
        """
        raise RuntimeError("검사 target을 순회할 수 없습니다.") from None

    # 명시한 root 자체는 허용하되 그 아래 dependency/cache와 symlink target 진입은 차단한다.
    for current_root, directory_names, file_names in os.walk(
        target,
        followlinks=False,
        onerror=raise_walk_error,
    ):
        sorted_directory_names = sorted(directory_names)
        directory_names[:] = sorted(
            directory_name
            for directory_name in sorted_directory_names
            if directory_name not in DEFAULT_EXCLUDED_DIRECTORY_NAMES
            and not (Path(current_root) / directory_name).is_symlink()
        )

        # Directory symlink는 진입하지 않지만 그 entry 이름은 path component 검사에 포함한다.
        traversed_directory_names = set(directory_names)
        for directory_name in sorted_directory_names:
            directory_path = Path(current_root) / directory_name
            if directory_path.is_symlink() or (
                directory_name in traversed_directory_names
            ):
                yield directory_path

        # File symlink도 entry 이름은 반환하되 target content는 읽지 않는다.
        for file_name in sorted(file_names):
            file_path = Path(current_root) / file_name
            if file_path.is_symlink():
                yield file_path
                continue
            if not file_path.is_file():
                continue
            if file_path.resolve(strict=False) == resolved_env_path:
                continue
            yield file_path


def iter_regular_files(target: Path, env_path: Path | None) -> Iterator[Path]:
    """
    함수 이름: iter_regular_files()
    기능: 명시 target의 scannable entry 중 content를 읽을 regular file만 순회한다.
    인자: target -> file 또는 directory
        env_path -> scan에서 제외할 .env source 또는 Keychain source를 뜻하는 None
    반환값: deterministic regular file iterator
    작성 날짜: 2026/08/24
    """

    # is_file()이 symlink target을 따라가므로 symlink 여부를 먼저 검사한다.
    for artifact_entry in iter_artifact_entries(target, env_path):
        if not artifact_entry.is_symlink() and artifact_entry.is_file():
            yield artifact_entry


def path_contains_canary_component(
    artifact_path: Path,
    canary: bytes | bytearray,
) -> bool:
    """
    함수 이름: path_contains_canary_component()
    기능: artifact path의 각 file·directory component에 포함된 credential canary를 찾는다.
    인자: artifact_path -> 검사할 artifact entry path
        canary -> 출력하지 않을 credential bytes
    반환값: path component 내 canary byte substring 포함 여부
    작성 날짜: 2026/08/24
    """

    # 빈 canary는 의미 없는 component match를 만들 수 있으므로 즉시 거부한다.
    if not canary:
        return False

    # 각 component를 filesystem bytes로 비교해 separator를 넘는 오탐 없이 substring을 찾는다.
    for path_component in artifact_path.parts:
        try:
            component_buffer = bytearray(os.fsencode(path_component))
        except (OSError, UnicodeError):
            raise RuntimeError("산출물 이름을 검사할 수 없습니다.") from None
        component_matches = component_buffer.find(canary) >= 0
        zeroize_secret_buffer(component_buffer)  # Path component의 mutable bytes 사본을 비교 직후 폐기한다.
        if component_matches:
            return True
    return False


def file_contains_canary(file_path: Path, canary: bytes | bytearray) -> bool:
    """
    함수 이름: file_contains_canary()
    기능: 큰 binary도 bounded memory chunk와 경계 overlap으로 exact canary를 찾는다.
    인자: file_path -> 검사할 regular file, canary -> 출력하지 않을 credential bytes
    반환값: exact bytes 포함 여부
    작성 날짜: 2026/08/24
    """

    overlap_size = max(0, len(canary) - 1)  # 두 chunk 경계의 최대 부분 일치를 보존한다.
    previous_tail = b""

    # 한 번에 고정 크기만 읽고 직전 tail을 붙여 큰 binary에서도 bounded memory를 유지한다.
    try:
        with file_path.open("rb") as artifact_file:
            while True:
                next_chunk = artifact_file.read(SCAN_CHUNK_SIZE)
                if not next_chunk:
                    return False
                scan_buffer = previous_tail + next_chunk
                if canary in scan_buffer:
                    return True
                previous_tail = scan_buffer[-overlap_size:] if overlap_size > 0 else b""  # 다음 chunk 경계만 남긴다.
    except OSError:
        raise RuntimeError("산출물을 읽을 수 없습니다.") from None


def main() -> int:
    """
    함수 이름: main()
    기능: 모든 canary와 target path·file 조합을 검사하고 ordinal PASS/FAIL만 출력한다.
    인자: 없음
    반환값: 누출 없음 0, 설정·읽기 오류 2, canary 발견 1
    작성 날짜: 2026/08/24
    """

    arguments = parse_arguments()
    credential_canaries: dict[str, bytearray] = {}
    try:
        excluded_env_path: Path | None
        try:
            # 명시 source에 따라 .env 또는 Keychain을 하나만 읽고 다른 source로 fallback하지 않는다.
            if arguments.keychain_service is None:
                excluded_env_path = arguments.env.resolve(strict=False)
                credential_canaries = load_credential_canaries(excluded_env_path)
            else:
                excluded_env_path = None
                credential_canaries = load_keychain_credential_canaries(
                    arguments.keychain_service,
                    arguments.keychain_accounts,
                )
            artifact_entries = sorted({
                Path(os.path.abspath(os.fspath(artifact_entry)))
                for target in arguments.targets
                for artifact_entry in iter_artifact_entries(target, excluded_env_path)
            })  # abspath는 symlink target을 해석하지 않고 lexical entry 이름을 보존한다.
            target_files = [
                artifact_entry
                for artifact_entry in artifact_entries
                if not artifact_entry.is_symlink() and artifact_entry.is_file()
            ]
        except (OSError, RuntimeError):
            print("phase12-secret-scan: ERROR: scan setup failed.", file=sys.stderr)
            return 2

        artifact_entry_ordinals = {
            artifact_entry: entry_index
            for entry_index, artifact_entry in enumerate(artifact_entries, start=1)
        }  # Raw path를 출력하지 않고 stable ordinal로만 지점을 식별한다.
        path_leaked_matches: list[tuple[int, int]] = []
        try:
            # File content를 열기 전에 path component 누출을 먼저 검사하고 ordinal로만 기록한다.
            for canary_index, credential_canary in enumerate(
                credential_canaries.values(),
                start=1,
            ):
                for artifact_entry in artifact_entries:
                    if path_contains_canary_component(
                        artifact_entry,
                        credential_canary,
                    ):
                        path_leaked_matches.append((
                            canary_index,
                            artifact_entry_ordinals[artifact_entry],
                        ))
        except RuntimeError:
            print("phase12-secret-scan: ERROR: artifact scan failed.", file=sys.stderr)
            return 2

        if path_leaked_matches:
            # Path에 credential이 있으면 추가 file read 오류로 FAIL이 덮이지 않도록 즉시 보고한다.
            for canary_index, entry_index in path_leaked_matches:
                print(
                    "phase12-secret-scan: FAIL: "
                    f"credential canary #{canary_index} matched "
                    f"artifact entry #{entry_index} path component.",
                    file=sys.stderr,
                )
            return 1

        # Path leak이 없더라도 content를 하나도 검사하지 못한 빈 target은 PASS로 처리하지 않는다.
        if not target_files:
            print("phase12-secret-scan: ERROR: scan setup failed.", file=sys.stderr)
            return 2

        content_leaked_matches: list[tuple[int, int]] = []
        try:
            # Path leak이 없을 때만 regular file content를 exact canary와 비교한다.
            for canary_index, credential_canary in enumerate(
                credential_canaries.values(),
                start=1,
            ):
                for target_file in target_files:
                    if file_contains_canary(target_file, credential_canary):
                        content_leaked_matches.append((
                            canary_index,
                            artifact_entry_ordinals[target_file],
                        ))
        except RuntimeError:
            print("phase12-secret-scan: ERROR: artifact scan failed.", file=sys.stderr)
            return 2

        if content_leaked_matches:
            # Content 누출도 credential key·value·raw path 대신 secret-free ordinal로만 보고한다.
            for canary_index, entry_index in content_leaked_matches:
                print(
                    "phase12-secret-scan: FAIL: "
                    f"credential canary #{canary_index} matched "
                    f"artifact entry #{entry_index} file content.",
                    file=sys.stderr,
                )
            return 1

        print(
            "phase12-secret-scan: PASS: "
            f"{len(credential_canaries)} credential canaries absent from "
            f"{len(target_files)} files."
        )
        return 0
    finally:
        # 모든 return/error 경로에서 mutable canary를 덮어써 process lifetime보다 일찍 폐기한다.
        zeroize_credential_canaries(credential_canaries)


if __name__ == "__main__":
    raise SystemExit(main())
