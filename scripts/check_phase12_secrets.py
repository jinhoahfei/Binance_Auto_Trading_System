#!/usr/bin/env python3
"""
Phase 12 산출물에 .env credential 값이 복제되지 않았는지 값 자체를 출력하지 않고 검사한다.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import Iterator


SECRET_KEY_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL")
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


def parse_arguments() -> argparse.Namespace:
    """
    함수 이름: parse_arguments()
    기능: credential source와 검사할 file·directory 목록을 command line에서 읽는다.
    인자: 없음
    반환값: 검증 전 argparse namespace
    작성 날짜: 2026/08/24
    """

    # Secret 원문을 help나 기본 인자에 넣지 않고 source path와 scan target만 정의한다.
    parser = argparse.ArgumentParser(
        description="Scan artifacts for .env credential canaries without printing values.",
    )
    parser.add_argument(
        "--env",
        type=Path,
        default=Path(".env"),
        help="credential canary를 읽을 .env path",
    )
    parser.add_argument(
        "targets",
        nargs="*",
        type=Path,
        default=[Path(".")],
        help="검사할 file 또는 directory; 생략하면 repository root",
    )
    return parser.parse_args()  # 실제 path 존재 여부와 읽기 권한은 fail-closed scan 단계에서 검사한다.


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


def load_credential_canaries(env_path: Path) -> dict[str, bytes]:
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
    except (OSError, UnicodeError) as error:
        raise RuntimeError("credential source를 읽을 수 없습니다.") from error

    credential_canaries: dict[str, bytes] = {}

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
        if len(normalized_value) < 8:
            continue
        credential_canaries[normalized_key] = normalized_value.encode("utf-8")  # 출력 없이 bytes 비교에만 사용한다.

    # Canary가 하나도 없으면 무검사 PASS가 되므로 configuration 오류로 중단한다.
    if not credential_canaries:
        raise RuntimeError("검사할 non-empty credential canary가 없습니다.")
    return credential_canaries


def iter_regular_files(target: Path, env_path: Path) -> Iterator[Path]:
    """
    함수 이름: iter_regular_files()
    기능: symlink와 dependency cache를 건너뛰고 명시된 산출물의 regular file만 순회한다.
    인자: target -> file 또는 directory, env_path -> scan에서 제외할 credential source
    반환값: deterministic absolute regular file iterator
    작성 날짜: 2026/08/24
    """

    resolved_env_path = env_path.resolve(strict=False)

    # 명시 target 자체가 없거나 symlink이면 검사 성공으로 오인하지 않고 즉시 실패한다.
    if target.is_symlink():
        raise RuntimeError(f"검사 target은 symlink일 수 없습니다: {target}")
    if target.is_file():
        if target.resolve(strict=False) != resolved_env_path:
            yield target
        return
    if not target.is_dir():
        raise RuntimeError(f"검사 target이 존재하지 않거나 읽을 수 없습니다: {target}")

    def raise_walk_error(error: OSError) -> None:
        """
        함수 이름: raise_walk_error()
        기능: os.walk가 만난 unreadable directory를 silent skip 대신 검사 실패로 변환한다.
        인자: error -> directory 순회 중 발생한 filesystem 오류
        반환값: 정상 반환 없이 RuntimeError 발생
        작성 날짜: 2026/08/24
        """
        raise RuntimeError(f"검사 target을 순회할 수 없습니다: {target}") from error

    # 명시한 root 자체는 허용하되 그 아래 dependency/cache directory 진입은 차단한다.
    for current_root, directory_names, file_names in os.walk(
        target,
        followlinks=False,
        onerror=raise_walk_error,
    ):
        directory_names[:] = sorted(
            directory_name
            for directory_name in directory_names
            if directory_name not in DEFAULT_EXCLUDED_DIRECTORY_NAMES
            and not (Path(current_root) / directory_name).is_symlink()
        )
        for file_name in sorted(file_names):
            file_path = Path(current_root) / file_name
            if file_path.is_symlink() or not file_path.is_file():
                continue
            if file_path.resolve(strict=False) == resolved_env_path:
                continue
            yield file_path


def file_contains_canary(file_path: Path, canary: bytes) -> bool:
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
    except OSError as error:
        raise RuntimeError(f"산출물을 읽을 수 없습니다: {file_path}") from error


def main() -> int:
    """
    함수 이름: main()
    기능: 모든 canary와 target file 조합을 검사하고 secret 없는 PASS/FAIL만 출력한다.
    인자: 없음
    반환값: 누출 없음 0, 설정·읽기 오류 2, canary 발견 1
    작성 날짜: 2026/08/24
    """

    arguments = parse_arguments()
    env_path = arguments.env.resolve(strict=False)
    try:
        credential_canaries = load_credential_canaries(env_path)
        target_files = sorted({
            file_path.resolve(strict=False)
            for target in arguments.targets
            for file_path in iter_regular_files(target, env_path)
        })
        if not target_files:
            raise RuntimeError("검사할 regular file이 없습니다.")
    except RuntimeError as error:
        print(f"phase12-secret-scan: ERROR: {error}", file=sys.stderr)
        return 2

    leaked_locations: list[tuple[str, Path]] = []
    try:
        for credential_key, credential_canary in credential_canaries.items():
            for target_file in target_files:
                if file_contains_canary(target_file, credential_canary):
                    leaked_locations.append((credential_key, target_file))
    except RuntimeError as error:
        print(f"phase12-secret-scan: ERROR: {error}", file=sys.stderr)
        return 2

    if leaked_locations:
        # Credential 값은 절대 출력하지 않고 key와 발견 위치만 운영자에게 전달한다.
        for credential_key, leaked_path in leaked_locations:
            print(
                f"phase12-secret-scan: FAIL: {credential_key} matched in {leaked_path}",
                file=sys.stderr,
            )
        return 1

    print(
        "phase12-secret-scan: PASS: "
        f"{len(credential_canaries)} credential canaries absent from "
        f"{len(target_files)} files."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
