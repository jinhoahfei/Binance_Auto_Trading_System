#!/usr/bin/env python3
"""Phase 13 공급망 NO_GO 증거를 현재 lockfile과 로컬 manifest byte에 결합한다."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tomllib
from typing import Any, Mapping, Sequence

from scripts.phase13_local_supply_artifacts import (
    HISTORICAL_RELEASE_BINDING_PATH,
    LICENSE_INVENTORY_PATH as DEPENDENCY_LICENSE_INVENTORY_PATH,
    NOTICE_REVIEW_PATH,
    SBOM_PATH,
    LocalSupplyArtifactError,
    validate_local_supply_artifacts,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_PATH = Path("Design/Architecture/phase13_supply_chain_evidence.json")
LOCAL_INVENTORY_PATH = Path(
    "Design/Architecture/phase13_local_supply_chain_inventory.json"
)
MAXIMUM_JSON_BYTES = 512 * 1024
MAXIMUM_INPUT_BYTES = 8 * 1024 * 1024
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
LOCKFILE_SPECS = (
    (Path("backend/uv.lock"), "PyPI"),
    (Path("UI/pnpm-lock.yaml"), "npm"),
    (Path("UI/apps/desktop/src-tauri/Cargo.lock"), "crates.io"),
)
PROJECT_MANIFEST_SPECS = (
    (Path("backend/pyproject.toml"), "python"),
    (Path("UI/package.json"), "node"),
    (Path("UI/apps/desktop/src-tauri/Cargo.toml"), "rust"),
)
EXPECTED_EVIDENCE_DATE = "2026-08-25"
EXPECTED_EVIDENCE_UPDATED_DATE = "2026-08-29"
EXPECTED_LOCAL_INVENTORY_DATE = "2026-08-29"
EXPECTED_RETAINED_SCAN_INPUTS = (
    (
        "backend/uv.lock",
        "cfecb5e02854f90c2c8fabbc67d2692c370dc81df767127329a40489e708084f",
    ),
    (
        "UI/pnpm-lock.yaml",
        "dd1834c32ee46752104f3dde32558e5f661f605d88bfa9c1ac1062678bc0e24f",
    ),
    (
        "UI/apps/desktop/src-tauri/Cargo.lock",
        "759f9787c88a67c4d7b356adfe1842b14d087bf48c7825db3f186d5bedef7783",
    ),
)
EXPECTED_RETAINED_SCAN_PACKAGE_COUNT = 868
EXPECTED_FINDING_IDS = frozenset(
    {
        "GHSA-wrw7-89jp-8q8g",
        "RUSTSEC-2024-0370",
        "RUSTSEC-2024-0411",
        "RUSTSEC-2024-0412",
        "RUSTSEC-2024-0413",
        "RUSTSEC-2024-0414",
        "RUSTSEC-2024-0415",
        "RUSTSEC-2024-0416",
        "RUSTSEC-2024-0417",
        "RUSTSEC-2024-0418",
        "RUSTSEC-2024-0419",
        "RUSTSEC-2024-0420",
        "RUSTSEC-2024-0429",
        "RUSTSEC-2025-0075",
        "RUSTSEC-2025-0080",
        "RUSTSEC-2025-0081",
        "RUSTSEC-2025-0098",
        "RUSTSEC-2025-0100",
    }
)
EXPECTED_REACHABLE_FINDING_IDS = frozenset(
    {
        "RUSTSEC-2025-0075",
        "RUSTSEC-2025-0080",
        "RUSTSEC-2025-0081",
        "RUSTSEC-2025-0098",
        "RUSTSEC-2025-0100",
    }
)
EXPECTED_UNRESOLVED_LICENSE_GROUPS = (
    ("non-standard", ("pyinstaller", "pyinstaller-hooks-contrib")),
    ("GPL-2.0", ("pyinstaller-hooks-contrib",)),
)
EXPECTED_PROJECT_LICENSE_POLICY = {
    "backend/pyproject.toml": ("LicenseRef-Proprietary", True),
    "UI/package.json": ("UNLICENSED", True),
    "UI/apps/desktop/src-tauri/Cargo.toml": (None, True),
}
HISTORICAL_COMMAND_PREFIX = "HISTORICAL_NON_EXECUTABLE: "
CURRENT_LOCAL_COMMAND_PREFIX = "CURRENT_LOCAL_ONLY: "
EXPECTED_HISTORICAL_COMMANDS = frozenset(
    {
        "osv-scanner scan source --lockfile backend/uv.lock --lockfile UI/pnpm-lock.yaml --lockfile UI/apps/desktop/src-tauri/Cargo.lock --format=json --all-packages --verbosity=error",
        "osv-scanner scan source --lockfile backend/uv.lock --lockfile UI/pnpm-lock.yaml --lockfile UI/apps/desktop/src-tauri/Cargo.lock --licenses --format=json --all-packages --verbosity=error",
        "osv-scanner scan source --lockfile backend/uv.lock --lockfile UI/pnpm-lock.yaml --lockfile UI/apps/desktop/src-tauri/Cargo.lock --format=cyclonedx-1-6 --all-packages --verbosity=error",
    }
)
EXPECTED_CURRENT_LOCAL_COMMANDS = frozenset(
    {
        "python3 scripts/run_phase13_offline_osv.py vulnerability",
        "python3 scripts/run_phase13_offline_osv.py license",
        "python3 scripts/phase13_local_supply_artifacts.py check",
        "cargo tree --locked --offline --target aarch64-apple-darwin -i glib@0.18.5",
        "cargo tree --locked --offline --target aarch64-apple-darwin -i unic-char-property@0.9.0",
    }
)


class SupplyChainEvidenceError(RuntimeError):
    """
    클래스 이름: SupplyChainEvidenceError
    기능: 공급망 evidence, lockfile 또는 로컬 inventory의 fail-closed 위반을 나타낸다.
    작성 날짜: 2026/08/29
    """


@dataclass(frozen=True)
class LockfileInventory:
    """
    클래스 이름: LockfileInventory
    기능: 한 lockfile의 byte hash와 정렬된 package coordinate 집계를 보존한다.
    작성 날짜: 2026/08/29
    """

    path: str
    ecosystem: str
    sha256: str
    package_count: int
    coordinates_sha256: str


@dataclass(frozen=True)
class SupplyChainEvidenceSummary:
    """
    클래스 이름: SupplyChainEvidenceSummary
    기능: 현재 inventory와 보존 scan을 분리한 NO_GO evidence 집계를 보존한다.
    작성 날짜: 2026/08/29
    """

    package_count: int
    retained_scan_package_count: int
    current_vs_scanned_match: bool
    security_finding_count: int
    unresolved_license_group_count: int
    exact_sbom_component_count: int
    third_party_license_declaration_count: int
    third_party_noassertion_count: int
    current_phase13_release_candidate_bound: bool
    overall_status: str


def _reject_duplicate_keys(object_pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """
    함수 이름: _reject_duplicate_keys()
    기능: JSON object의 duplicate key를 silent overwrite하지 않고 거부한다.
    인자: object_pairs -> decoder가 원문 순서로 전달한 key/value pair
    반환값: duplicate key가 없는 dictionary
    작성 날짜: 2026/08/29
    """
    loaded_object: dict[str, Any] = {}

    # 첫 key를 보존하고 같은 key가 다시 나타나는 즉시 evidence 전체를 거부한다.
    for object_key, object_value in object_pairs:
        if object_key in loaded_object:
            raise SupplyChainEvidenceError("supply-chain JSON contains duplicate keys")
        loaded_object[object_key] = object_value
    return loaded_object


def _reject_nonstandard_constant(constant_name: str) -> None:
    """
    함수 이름: _reject_nonstandard_constant()
    기능: NaN과 Infinity 같은 JSON 표준 밖 숫자를 evidence에서 거부한다.
    인자: constant_name -> decoder가 발견한 비표준 constant 이름
    반환값: 정상 반환하지 않음
    작성 날짜: 2026/08/29
    """
    del constant_name  # 원문 token은 오류 출력에 반사하지 않는다.
    raise SupplyChainEvidenceError("supply-chain JSON contains non-standard numbers")


def _require_exact_keys(
    value: object,
    expected_keys: frozenset[str],
    label: str,
) -> Mapping[str, object]:
    """
    함수 이름: _require_exact_keys()
    기능: 값이 누락·추가 없는 exact-key JSON object인지 확인한다.
    인자: value -> 검사할 값, expected_keys -> 요구 key 집합, label -> 오류 위치
    반환값: 검증된 mapping
    작성 날짜: 2026/08/29
    """
    # Mapping 타입과 key 집합을 함께 검사해 추가 field의 silent 수용을 막는다.
    if not isinstance(value, Mapping) or frozenset(value) != expected_keys:
        raise SupplyChainEvidenceError(f"{label} schema is invalid")
    return value


def _require_relative_path_parts(relative_path: Path) -> tuple[str, ...]:
    """
    함수 이름: _require_relative_path_parts()
    기능: Repository 상대 경로를 escape 없는 component tuple로 검증한다.
    인자: relative_path -> repository root 기준 artifact 경로
    반환값: 순서가 보존된 non-empty path component tuple
    작성 날짜: 2026/08/31
    """
    if not isinstance(relative_path, Path) or relative_path.is_absolute():
        raise SupplyChainEvidenceError("supply-chain artifact path is invalid")
    path_parts = relative_path.parts
    if not path_parts or any(path_part in {"", ".", ".."} for path_part in path_parts):
        raise SupplyChainEvidenceError("supply-chain artifact path is invalid")
    return path_parts  # 이후 open은 이 lexical component만 dir_fd에 한 단계씩 전달한다.


def _open_directory_chain(
    repository_root: Path,
    directory_parts: Sequence[str],
) -> tuple[list[int], list[tuple[int, str, os.stat_result]]]:
    """
    함수 이름: _open_directory_chain()
    기능: Repository root부터 각 directory를 no-follow descriptor로 순차 고정한다.
    인자: repository_root -> 신뢰할 repository root,
        directory_parts -> leaf parent까지의 상대 component sequence
    반환값: 열린 directory descriptor들과 parent-entry identity tuple
    작성 날짜: 2026/08/31
    """
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory_descriptors: list[int] = []
    directory_links: list[tuple[int, str, os.stat_result]] = []

    # Root와 각 child를 descriptor-relative로 열어 검사와 open 사이 parent symlink 경합을 제거한다.
    try:
        root_descriptor = os.open(repository_root, directory_flags)
        directory_descriptors.append(root_descriptor)
        if not stat.S_ISDIR(os.fstat(root_descriptor).st_mode):
            raise SupplyChainEvidenceError("supply-chain repository root is invalid")
        current_descriptor = root_descriptor
        for directory_name in directory_parts:
            path_status = os.stat(
                directory_name,
                dir_fd=current_descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(path_status.st_mode):
                raise SupplyChainEvidenceError("supply-chain artifact path is invalid")
            child_descriptor = os.open(
                directory_name,
                directory_flags,
                dir_fd=current_descriptor,
            )
            child_status = os.fstat(child_descriptor)
            if (
                not stat.S_ISDIR(child_status.st_mode)
                or path_status.st_dev != child_status.st_dev
                or path_status.st_ino != child_status.st_ino
            ):
                os.close(child_descriptor)
                raise SupplyChainEvidenceError("supply-chain artifact path changed")
            directory_links.append(
                (current_descriptor, directory_name, child_status)
            )
            directory_descriptors.append(child_descriptor)
            current_descriptor = child_descriptor
    except Exception:
        for directory_descriptor in reversed(directory_descriptors):
            os.close(directory_descriptor)
        raise
    return directory_descriptors, directory_links


def _require_directory_chain_stable(
    directory_links: Sequence[tuple[int, str, os.stat_result]],
) -> None:
    """
    함수 이름: _require_directory_chain_stable()
    기능: 열린 parent chain의 각 directory entry가 같은 inode인지 다시 검증한다.
    인자: directory_links -> parent fd, component 이름, 최초 child 상태 sequence
    반환값: 없음
    작성 날짜: 2026/08/31
    """
    # 모든 parent fd를 닫기 전에 entry를 다시 stat해 rename·symlink 교체를 fail closed한다.
    for parent_descriptor, directory_name, initial_status in directory_links:
        final_status = os.stat(
            directory_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(final_status.st_mode)
            or initial_status.st_dev != final_status.st_dev
            or initial_status.st_ino != final_status.st_ino
        ):
            raise SupplyChainEvidenceError("supply-chain artifact path changed")


def _read_regular_bytes(
    repository_root: Path,
    relative_path: Path,
    maximum_bytes: int = MAXIMUM_INPUT_BYTES,
) -> bytes:
    """
    함수 이름: _read_regular_bytes()
    기능: 검증된 repository regular file을 bounded bytes로 읽는다.
    인자: repository_root -> repository root, relative_path -> root 기준 상대 경로,
        maximum_bytes -> 허용할 최대 byte 수
    반환값: 파일 원문 bytes
    작성 날짜: 2026/08/29
    """
    if type(maximum_bytes) is not int or maximum_bytes <= 0:
        raise ValueError("maximum_bytes must be a positive integer")
    path_parts = _require_relative_path_parts(relative_path)
    directory_descriptors: list[int] = []

    # Root부터 leaf parent까지 descriptor chain을 유지한 채 nonblocking leaf를 읽는다.
    try:
        directory_descriptors, directory_links = _open_directory_chain(
            repository_root,
            path_parts[:-1],
        )
        parent_descriptor = directory_descriptors[-1]
        leaf_name = path_parts[-1]
        path_status_before = os.stat(
            leaf_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if stat.S_ISLNK(path_status_before.st_mode):
            raise SupplyChainEvidenceError("supply-chain artifact path is invalid")
        file_descriptor = os.open(
            leaf_name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent_descriptor,
        )
        try:
            initial_status = os.fstat(file_descriptor)
            if not stat.S_ISREG(initial_status.st_mode):
                raise SupplyChainEvidenceError(
                    "supply-chain artifact must be a regular file"
                )
            if initial_status.st_size <= 0 or initial_status.st_size > maximum_bytes:
                raise SupplyChainEvidenceError(
                    "supply-chain artifact size is invalid"
                )
            if (
                path_status_before.st_dev != initial_status.st_dev
                or path_status_before.st_ino != initial_status.st_ino
            ):
                raise SupplyChainEvidenceError(
                    "supply-chain artifact changed during read"
                )

            # 사전 size를 신뢰하지 않고 max+1 byte까지만 읽어 증가한 파일도 bounded 메모리에서 거부한다.
            remaining_bytes = maximum_bytes + 1
            file_chunks: list[bytes] = []
            while remaining_bytes > 0:
                file_chunk = os.read(
                    file_descriptor,
                    min(64 * 1024, remaining_bytes),
                )
                if not file_chunk:
                    break
                file_chunks.append(file_chunk)
                remaining_bytes -= len(file_chunk)
            file_bytes = b"".join(file_chunks)
            final_status = os.fstat(file_descriptor)
            path_status_after = os.stat(
                leaf_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            _require_directory_chain_stable(directory_links)
        finally:
            os.close(file_descriptor)
    except OSError as error:
        raise SupplyChainEvidenceError("supply-chain artifact could not be read") from error
    finally:
        for directory_descriptor in reversed(directory_descriptors):
            os.close(directory_descriptor)

    # Fd와 path가 동일한 regular file을 가리키고 read 전후 metadata가 고정됐을 때만 bytes를 신뢰한다.
    stable_status_fields = (
        "st_dev",
        "st_ino",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if (
        len(file_bytes) <= 0
        or len(file_bytes) > maximum_bytes
        or len(file_bytes) != initial_status.st_size
        or not stat.S_ISREG(final_status.st_mode)
        or not stat.S_ISREG(path_status_after.st_mode)
        or any(
            getattr(initial_status, field_name) != getattr(observed_status, field_name)
            for observed_status in (
                path_status_before,
                final_status,
                path_status_after,
            )
            for field_name in stable_status_fields
        )
    ):
        raise SupplyChainEvidenceError("supply-chain artifact changed during read")
    return file_bytes  # Caller는 고정 descriptor에서 검증된 bounded snapshot만 사용한다.


def _load_json_object(repository_root: Path, relative_path: Path) -> dict[str, object]:
    """
    함수 이름: _load_json_object()
    기능: Bounded UTF-8 JSON을 duplicate/non-standard 값 없이 object로 읽는다.
    인자: repository_root -> repository root, relative_path -> root 기준 JSON 경로
    반환값: 검증 전 JSON dictionary
    작성 날짜: 2026/08/29
    """
    json_bytes = _read_regular_bytes(
        repository_root,
        relative_path,
        MAXIMUM_JSON_BYTES,
    )

    # UTF-8 decode와 JSON parse 오류를 raw content 없는 고정 evidence 오류로 변환한다.
    try:
        loaded_value = json.loads(
            json_bytes.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SupplyChainEvidenceError("supply-chain JSON could not be loaded") from error
    if not isinstance(loaded_value, dict):
        raise SupplyChainEvidenceError("supply-chain JSON root must be an object")
    return loaded_value


def _validate_bound_artifact_reference(
    repository_root: Path,
    reference_value: object,
    *,
    expected_path: Path,
    label: str,
) -> None:
    """
    함수 이름: _validate_bound_artifact_reference()
    기능: Main evidence의 artifact reference가 fixed path의 현재 bytes와 일치하는지 검증한다.
    인자: repository_root -> repository root, reference_value -> path/hash reference,
        expected_path -> 허용한 canonical 경로, label -> 오류 위치
    반환값: 없음
    작성 날짜: 2026/08/29
    """
    artifact_reference = _require_exact_keys(
        reference_value,
        frozenset({"path", "sha256"}),
        label,
    )
    artifact_sha256 = artifact_reference["sha256"]
    if (
        artifact_reference["path"] != expected_path.as_posix()
        or not isinstance(artifact_sha256, str)
        or SHA256_PATTERN.fullmatch(artifact_sha256) is None
    ):
        raise SupplyChainEvidenceError(f"{label} is invalid")

    # Reference hash는 JSON·Markdown 원문 bytes와 직접 대조해 semantic 재직렬을 허용하지 않는다.
    artifact_bytes = _read_regular_bytes(
        repository_root,
        expected_path,
        MAXIMUM_INPUT_BYTES,
    )
    if _sha256(artifact_bytes) != artifact_sha256:
        raise SupplyChainEvidenceError(f"{label} hash mismatch")


def _sha256(file_bytes: bytes) -> str:
    """
    함수 이름: _sha256()
    기능: 파일 또는 canonical coordinate bytes의 lowercase SHA-256을 계산한다.
    인자: file_bytes -> hash할 bytes
    반환값: 64자리 lowercase SHA-256
    작성 날짜: 2026/08/29
    """
    return hashlib.sha256(file_bytes).hexdigest()  # Evidence byte identity를 lowercase digest로 고정한다.


def _coordinate_digest(coordinates: Sequence[str]) -> str:
    """
    함수 이름: _coordinate_digest()
    기능: 정렬된 package coordinate 전체를 newline framing으로 결합해 SHA-256을 계산한다.
    인자: coordinates -> lockfile에서 읽은 package identity 목록
    반환값: package coordinate 집합의 canonical SHA-256
    작성 날짜: 2026/08/29
    """
    # 입력 순서가 달라도 같은 package 집합은 같은 digest를 내도록 정렬한다.
    if not coordinates or any(not coordinate for coordinate in coordinates):
        raise SupplyChainEvidenceError("lockfile package coordinates are empty")
    canonical_bytes = ("\n".join(sorted(coordinates)) + "\n").encode("utf-8")
    return _sha256(canonical_bytes)


def _parse_toml_packages(
    lockfile_bytes: bytes,
    ecosystem: str,
) -> tuple[str, ...]:
    """
    함수 이름: _parse_toml_packages()
    기능: uv/Cargo TOML lockfile에서 name, version과 source가 결합된 package 좌표를 읽는다.
    인자: lockfile_bytes -> TOML lockfile bytes, ecosystem -> PyPI 또는 crates.io
    반환값: lockfile 순서의 package coordinate tuple
    작성 날짜: 2026/08/29
    """
    try:
        lockfile = tomllib.loads(lockfile_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise SupplyChainEvidenceError("TOML lockfile could not be parsed") from error
    packages = lockfile.get("package")
    if not isinstance(packages, list) or not packages:
        raise SupplyChainEvidenceError("TOML lockfile package list is invalid")

    # 같은 name/version의 다른 source를 숨기지 않도록 source 문자열도 coordinate에 포함한다.
    coordinates: list[str] = []
    for package in packages:
        if not isinstance(package, dict):
            raise SupplyChainEvidenceError("TOML lockfile package entry is invalid")
        package_name = package.get("name")
        package_version = package.get("version")
        package_source = package.get("source", "workspace")
        if not all(
            isinstance(value, str) and value
            for value in (package_name, package_version)
        ) or not isinstance(package_source, (str, dict)):
            raise SupplyChainEvidenceError("TOML package identity is invalid")
        source_text = (
            package_source
            if isinstance(package_source, str)
            else json.dumps(package_source, sort_keys=True, separators=(",", ":"))
        )
        coordinates.append(
            f"{ecosystem}:{package_name}@{package_version}|{source_text}"
        )
    return tuple(coordinates)


def _decode_pnpm_package_key(encoded_key: str) -> str:
    """
    함수 이름: _decode_pnpm_package_key()
    기능: pnpm packages section의 plain 또는 quoted YAML key를 원래 coordinate로 복원한다.
    인자: encoded_key -> colon을 제외한 YAML key text
    반환값: package name@version coordinate
    작성 날짜: 2026/08/29
    """
    # Quoted key는 YAML quote 규칙을 적용하고 plain key는 원문을 그대로 보존한다.
    if len(encoded_key) >= 2 and encoded_key[0] == encoded_key[-1] == "'":
        return encoded_key[1:-1].replace("''", "'")
    if len(encoded_key) >= 2 and encoded_key[0] == encoded_key[-1] == '"':
        try:
            decoded_key = json.loads(encoded_key)
        except json.JSONDecodeError as error:
            raise SupplyChainEvidenceError("pnpm package key is invalid") from error
        if isinstance(decoded_key, str):
            return decoded_key
    if encoded_key.startswith(("'", '"')) or encoded_key.endswith(("'", '"')):
        raise SupplyChainEvidenceError("pnpm package key quote is invalid")
    return encoded_key


def _parse_pnpm_packages(lockfile_bytes: bytes) -> tuple[str, ...]:
    """
    함수 이름: _parse_pnpm_packages()
    기능: pnpm v9 lockfile packages section의 exact package coordinate를 network 없이 읽는다.
    인자: lockfile_bytes -> pnpm-lock.yaml bytes
    반환값: packages section 순서의 npm coordinate tuple
    작성 날짜: 2026/08/29
    """
    try:
        lockfile_lines = lockfile_bytes.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise SupplyChainEvidenceError("pnpm lockfile is not UTF-8") from error
    package_section_indexes = [
        line_index
        for line_index, line in enumerate(lockfile_lines)
        if line == "packages:"
    ]
    if len(package_section_indexes) != 1:
        raise SupplyChainEvidenceError("pnpm packages section is missing or duplicated")

    # 두 칸 들여쓴 packages key만 읽고 네 칸 이상의 package metadata는 건너뛴다.
    coordinates: list[str] = []
    package_key_pattern = re.compile(r"^  (?P<key>\S.*):\s*\Z")
    for line in lockfile_lines[package_section_indexes[0] + 1 :]:
        if line and not line.startswith(" "):
            break
        package_match = package_key_pattern.fullmatch(line)
        if package_match is None:
            continue
        package_key = _decode_pnpm_package_key(package_match.group("key"))
        if "@" not in package_key or package_key.endswith("@"):
            raise SupplyChainEvidenceError("pnpm package identity is invalid")
        coordinates.append(f"npm:{package_key}")
    if len(coordinates) != len(set(coordinates)):
        raise SupplyChainEvidenceError("pnpm package coordinate is duplicated")
    return tuple(coordinates)


def _read_lockfile_inventory(
    repository_root: Path,
    relative_path: Path,
    ecosystem: str,
) -> LockfileInventory:
    """
    함수 이름: _read_lockfile_inventory()
    기능: 한 lockfile의 bytes, package count와 canonical coordinate digest를 계산한다.
    인자: repository_root -> repository root, relative_path -> lockfile 상대 경로,
        ecosystem -> package ecosystem 이름
    반환값: 계산된 LockfileInventory
    작성 날짜: 2026/08/29
    """
    # Raw lockfile bytes와 parsing한 coordinate 집합을 하나의 inventory로 결속한다.
    lockfile_bytes = _read_regular_bytes(repository_root, relative_path)
    coordinates = (
        _parse_pnpm_packages(lockfile_bytes)
        if ecosystem == "npm"
        else _parse_toml_packages(lockfile_bytes, ecosystem)
    )
    return LockfileInventory(
        path=relative_path.as_posix(),
        ecosystem=ecosystem,
        sha256=_sha256(lockfile_bytes),
        package_count=len(coordinates),
        coordinates_sha256=_coordinate_digest(coordinates),
    )


def _read_project_manifest(
    repository_root: Path,
    relative_path: Path,
    manifest_kind: str,
) -> dict[str, object]:
    """
    함수 이름: _read_project_manifest()
    기능: Repository project manifest의 name/version/license 선언과 byte hash를 읽는다.
    인자: repository_root -> repository root, relative_path -> manifest 상대 경로,
        manifest_kind -> python, node 또는 rust
    반환값: local license inventory의 project manifest object
    작성 날짜: 2026/08/29
    """
    manifest_bytes = _read_regular_bytes(repository_root, relative_path)
    try:
        if manifest_kind == "node":
            manifest = json.loads(
                manifest_bytes.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonstandard_constant,
            )
            package = manifest
        else:
            manifest = tomllib.loads(manifest_bytes.decode("utf-8"))
            package = manifest["project" if manifest_kind == "python" else "package"]
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError, tomllib.TOMLDecodeError) as error:
        raise SupplyChainEvidenceError("project manifest could not be parsed") from error
    if not isinstance(package, dict):
        raise SupplyChainEvidenceError("project manifest package metadata is invalid")

    # License key의 존재와 문자열 expression을 분리해 빈 값도 미선언으로 오인하지 않는다.
    package_name = package.get("name")
    package_version = package.get("version")
    license_value = package.get("license")
    if not all(
        isinstance(value, str) and value
        for value in (package_name, package_version)
    ):
        raise SupplyChainEvidenceError("project manifest identity is invalid")
    license_expression = (
        license_value.strip()
        if isinstance(license_value, str) and license_value.strip()
        else None
    )
    if manifest_kind == "python":
        classifiers = package.get("classifiers")
        publish_disabled = (
            isinstance(classifiers, list)
            and "Private :: Do Not Upload" in classifiers
        )
        license_declared = "license" in package
    elif manifest_kind == "node":
        publish_disabled = package.get("private") is True
        license_declared = "license" in package
    else:
        publish_disabled = package.get("publish") is False
        license_declared = "license" in package or "license-file" in package
    return {
        "path": relative_path.as_posix(),
        "sha256": _sha256(manifest_bytes),
        "package_name": package_name,
        "package_version": package_version,
        "license_declared": license_declared,
        "license_expression": license_expression,
        "publish_disabled": publish_disabled,
    }


def build_local_supply_chain_inventory(
    repository_root: Path = REPOSITORY_ROOT,
    *,
    observed_date: str,
) -> dict[str, object]:
    """
    함수 이름: build_local_supply_chain_inventory()
    기능: 세 lockfile 좌표와 repository project license 선언을 network 없이 재생성한다.
    인자: repository_root -> repository root, observed_date -> evidence 관찰 날짜
    반환값: canonical local supply-chain inventory object
    작성 날짜: 2026/08/29
    """
    if DATE_PATTERN.fullmatch(observed_date) is None:
        raise SupplyChainEvidenceError("local inventory observed date is invalid")

    # 각 lockfile에서 byte hash와 package coordinate digest를 독립적으로 계산한다.
    lockfile_inventories = [
        _read_lockfile_inventory(repository_root, relative_path, ecosystem)
        for relative_path, ecosystem in LOCKFILE_SPECS
    ]
    lockfile_inputs = [
        {
            "path": inventory.path,
            "ecosystem": inventory.ecosystem,
            "sha256": inventory.sha256,
            "package_count": inventory.package_count,
            "coordinates_sha256": inventory.coordinates_sha256,
        }
        for inventory in lockfile_inventories
    ]

    # Local project 세 manifest는 license key와 repository byte를 함께 기록한다.
    project_manifests = [
        _read_project_manifest(repository_root, relative_path, manifest_kind)
        for relative_path, manifest_kind in PROJECT_MANIFEST_SPECS
    ]
    declared_count = sum(
        manifest["license_declared"] is True for manifest in project_manifests
    )
    repository_license_files = []
    for candidate_name in (
        "LICENSE",
        "LICENSE.md",
        "LICENSE.txt",
        "LICENCE",
        "LICENCE.md",
        "LICENCE.txt",
        "COPYING",
        "COPYING.md",
        "COPYING.txt",
        "NOTICE",
        "NOTICE.md",
        "NOTICE.txt",
    ):
        candidate_path = Path(candidate_name)
        absolute_candidate = repository_root.resolve() / candidate_path
        if absolute_candidate.exists():
            license_bytes = _read_regular_bytes(repository_root, candidate_path)
            repository_license_files.append(
                {"path": candidate_name, "sha256": _sha256(license_bytes)}
            )

    return {
        "schema_version": 1,
        "record_type": "phase13_local_supply_chain_inventory",
        "observed_date": observed_date,
        "inputs": lockfile_inputs,
        "inventory": {
            "package_count": sum(
                inventory.package_count for inventory in lockfile_inventories
            ),
            "package_count_by_ecosystem": {
                inventory.ecosystem: inventory.package_count
                for inventory in lockfile_inventories
            },
        },
        "project_manifests": project_manifests,
        "license_inventory": {
            "scope": "repository_project_manifests_only",
            "project_count": len(project_manifests),
            "declared_count": declared_count,
            "undeclared_count": len(project_manifests) - declared_count,
            "repository_license_files": repository_license_files,
            "third_party_metadata_complete": False,
            "policy_approved": False,
        },
        "limitations": [
            "Lockfiles do not contain complete third-party license metadata.",
            "Exact lockfile SBOM and license scope are bound separately; raw current OSV/license output and a current Phase 13 release remain unavailable.",
        ],
    }


def _validate_local_inventory(
    repository_root: Path,
    inventory_path: Path,
) -> dict[str, object]:
    """
    함수 이름: _validate_local_inventory()
    기능: Checked-in local inventory가 현재 lockfile·manifest에서 byte 단위로 재생성되는지 확인한다.
    인자: repository_root -> repository root, inventory_path -> local inventory 상대 경로
    반환값: exact regeneration과 일치한 inventory object
    작성 날짜: 2026/08/29
    """
    inventory = _load_json_object(repository_root, inventory_path)
    inventory_root = _require_exact_keys(
        inventory,
        frozenset(
            {
                "schema_version",
                "record_type",
                "observed_date",
                "inputs",
                "inventory",
                "project_manifests",
                "license_inventory",
                "limitations",
            }
        ),
        "local inventory",
    )
    observed_date = inventory_root["observed_date"]
    if observed_date != EXPECTED_LOCAL_INVENTORY_DATE:
        raise SupplyChainEvidenceError("local inventory observed date is invalid")

    # 현재 repository에서 다시 만든 전체 object와 semantic equality를 요구한다.
    expected_inventory = build_local_supply_chain_inventory(
        repository_root,
        observed_date=observed_date,
    )
    if inventory != expected_inventory:
        raise SupplyChainEvidenceError("local supply-chain inventory drifted from repository")
    return inventory


def build_current_state_comparison(
    local_inventory: Mapping[str, object],
) -> dict[str, object]:
    """
    함수 이름: build_current_state_comparison()
    기능: 현재 local inventory와 2026-08-25 보존 scan 기준의 적용 여부를 계산한다.
    인자: local_inventory -> 현재 repository bytes에서 재생성·검증된 inventory
    반환값: Historical scan 입력과 현재 상태의 비교 object
    작성 날짜: 2026/08/29
    """
    inventory_inputs = local_inventory.get("inputs")
    inventory_summary = local_inventory.get("inventory")
    observed_date = local_inventory.get("observed_date")
    if (
        not isinstance(inventory_inputs, list)
        or not isinstance(inventory_summary, Mapping)
        or observed_date != EXPECTED_LOCAL_INVENTORY_DATE
    ):
        raise SupplyChainEvidenceError("local inventory comparison input is invalid")

    # 현재 lock hash는 보존 scan hash와 path별로 비교하되 historical 값을 덮어쓰지 않는다.
    retained_input_map = dict(EXPECTED_RETAINED_SCAN_INPUTS)
    current_input_map = {
        item.get("path"): item.get("sha256")
        for item in inventory_inputs
        if isinstance(item, Mapping)
    }
    lockfile_inputs_match = (
        len(current_input_map) == len(EXPECTED_RETAINED_SCAN_INPUTS)
        and current_input_map == retained_input_map
    )
    current_package_count = inventory_summary.get("package_count")
    if type(current_package_count) is not int:
        raise SupplyChainEvidenceError("local inventory package count is invalid")
    package_count_matches = (
        current_package_count == EXPECTED_RETAINED_SCAN_PACKAGE_COUNT
    )

    # Retained scan에는 현재 project manifest byte binding이 없으므로 lock이 같아도 적용 PASS로 승격하지 않는다.
    mismatch_reasons: list[str] = []
    if not lockfile_inputs_match:
        mismatch_reasons.append("current_lockfile_inputs_differ_from_retained_scan")
    if not package_count_matches:
        mismatch_reasons.append("current_package_count_differs_from_retained_scan")
    mismatch_reasons.append("current_project_manifests_are_not_bound_by_retained_scan")
    return {
        "retained_scan_observed_date": EXPECTED_EVIDENCE_DATE,
        "current_inventory_observed_date": observed_date,
        "lockfile_inputs_match": lockfile_inputs_match,
        "package_count_matches": package_count_matches,
        "project_manifests_bound": False,
        "current_vs_scanned_match": False,
        "mismatch_reasons": mismatch_reasons,
    }


def validate_supply_chain_evidence(
    repository_root: Path = REPOSITORY_ROOT,
    evidence_path: Path = EVIDENCE_PATH,
) -> SupplyChainEvidenceSummary:
    """
    함수 이름: validate_supply_chain_evidence()
    기능: Historical OSV NO_GO 요약과 현재 local inventory를 분리해 적용 GAP을 검증한다.
    인자: repository_root -> repository root, evidence_path -> evidence 상대 경로
    반환값: 검증된 NO_GO evidence 집계
    작성 날짜: 2026/08/29
    """
    evidence = _load_json_object(repository_root, evidence_path)
    root = _require_exact_keys(
        evidence,
        frozenset(
            {
                "schema_version",
                "record_type",
                "retained_scan_observed_date",
                "evidence_updated_date",
                "overall_status",
                "tool",
                "inputs",
                "inventory",
                "current_state_comparison",
                "security_findings",
                "license_findings",
                "artifact_binding",
                "commands",
                "decision",
            }
        ),
        "supply-chain evidence",
    )
    if root["schema_version"] != 4:
        raise SupplyChainEvidenceError("supply-chain evidence schema is unsupported")
    if root["record_type"] != "phase13_supply_chain_evidence":
        raise SupplyChainEvidenceError("supply-chain evidence record type is invalid")
    if (
        root["retained_scan_observed_date"] != EXPECTED_EVIDENCE_DATE
        or root["evidence_updated_date"] != EXPECTED_EVIDENCE_UPDATED_DATE
    ):
        raise SupplyChainEvidenceError("supply-chain evidence dates are invalid")
    if root["overall_status"] != "NO_GO":
        raise SupplyChainEvidenceError("supply-chain evidence cannot claim GO")

    # Local inventory artifact path와 hash를 먼저 검증해 나머지 요약의 lockfile 기준을 고정한다.
    artifact_binding = _require_exact_keys(
        root["artifact_binding"],
        frozenset(
            {
                "local_inventory",
                "exact_lockfile_sbom",
                "dependency_license_inventory",
                "third_party_notice_review",
                "historical_release_artifact",
                "raw_osv_scan",
                "raw_license_scan",
                "current_phase13_release_artifact",
                "complete",
                "gap_reason",
            }
        ),
        "artifact binding",
    )
    local_inventory_reference = _require_exact_keys(
        artifact_binding["local_inventory"],
        frozenset({"path", "sha256"}),
        "local inventory reference",
    )
    if local_inventory_reference["path"] != LOCAL_INVENTORY_PATH.as_posix():
        raise SupplyChainEvidenceError("local inventory path is not canonical")
    expected_inventory_hash = local_inventory_reference["sha256"]
    if not isinstance(expected_inventory_hash, str) or SHA256_PATTERN.fullmatch(
        expected_inventory_hash
    ) is None:
        raise SupplyChainEvidenceError("local inventory hash is invalid")
    inventory_bytes = _read_regular_bytes(
        repository_root,
        LOCAL_INVENTORY_PATH,
        MAXIMUM_JSON_BYTES,
    )
    if _sha256(inventory_bytes) != expected_inventory_hash:
        raise SupplyChainEvidenceError("local inventory artifact hash mismatch")
    local_inventory = _validate_local_inventory(repository_root, LOCAL_INVENTORY_PATH)

    # Exact SBOM, coordinate license inventory, notice review와 historical release record를 byte-bind한다.
    bound_artifact_paths = {
        "exact_lockfile_sbom": SBOM_PATH,
        "dependency_license_inventory": DEPENDENCY_LICENSE_INVENTORY_PATH,
        "third_party_notice_review": NOTICE_REVIEW_PATH,
        "historical_release_artifact": HISTORICAL_RELEASE_BINDING_PATH,
    }
    for artifact_key, expected_path in bound_artifact_paths.items():
        _validate_bound_artifact_reference(
            repository_root,
            artifact_binding[artifact_key],
            expected_path=expected_path,
            label=artifact_key.replace("_", " "),
        )

    # Current raw OSV/license와 current Phase 13 release가 없다는 사실은 null로 보존해 GO 오인을 막는다.
    for missing_artifact_key in (
        "raw_osv_scan",
        "raw_license_scan",
        "current_phase13_release_artifact",
    ):
        if artifact_binding[missing_artifact_key] is not None:
            raise SupplyChainEvidenceError("unvalidated supply-chain artifact was added")
    try:
        local_artifact_summary = validate_local_supply_artifacts(repository_root)
    except LocalSupplyArtifactError as error:
        raise SupplyChainEvidenceError(
            "local supply artifact validation failed"
        ) from error
    if (
        local_artifact_summary.component_count != EXPECTED_RETAINED_SCAN_PACKAGE_COUNT
        or local_artifact_summary.third_party_component_count != 866
        or local_artifact_summary.license_declaration_count
        + local_artifact_summary.noassertion_count
        != local_artifact_summary.third_party_component_count
        or local_artifact_summary.noassertion_count <= 0
        or local_artifact_summary.notice_complete is not False
        or local_artifact_summary.current_phase13_release_candidate_bound is not False
    ):
        raise SupplyChainEvidenceError("local supply artifact status is invalid")
    if artifact_binding["complete"] is not False:
        raise SupplyChainEvidenceError("supply-chain artifact binding cannot be complete")
    if not isinstance(artifact_binding["gap_reason"], str) or not artifact_binding[
        "gap_reason"
    ].strip():
        raise SupplyChainEvidenceError("artifact binding gap reason is invalid")

    # Retained scan input은 검사 당시 historical hash로 고정해 현재 lock으로 재바인딩하지 않는다.
    evidence_inputs = root["inputs"]
    expected_evidence_inputs = [
        {"path": path, "sha256": sha256}
        for path, sha256 in EXPECTED_RETAINED_SCAN_INPUTS
    ]
    if not isinstance(evidence_inputs, list):
        raise SupplyChainEvidenceError("supply-chain inputs are invalid")
    if evidence_inputs != expected_evidence_inputs:
        raise SupplyChainEvidenceError("retained scan inputs drifted")

    # OSV package count는 historical scan 요약으로 검증하고 현재 count와는 별도로 비교한다.
    inventory_summary = _require_exact_keys(
        root["inventory"],
        frozenset(
            {"package_count", "cyclonedx_spec_version", "cyclonedx_component_count"}
        ),
        "inventory summary",
    )
    scanned_package_count = inventory_summary["package_count"]
    if (
        scanned_package_count != EXPECTED_RETAINED_SCAN_PACKAGE_COUNT
        or inventory_summary["cyclonedx_component_count"]
        != EXPECTED_RETAINED_SCAN_PACKAGE_COUNT
        or inventory_summary["cyclonedx_spec_version"] != "1.6"
    ):
        raise SupplyChainEvidenceError("retained scan package inventory is invalid")

    # Checked-in comparison은 현재 inventory에서 계산한 결과와 정확히 같아야 한다.
    current_state_comparison = _require_exact_keys(
        root["current_state_comparison"],
        frozenset(
            {
                "retained_scan_observed_date",
                "current_inventory_observed_date",
                "lockfile_inputs_match",
                "package_count_matches",
                "project_manifests_bound",
                "current_vs_scanned_match",
                "mismatch_reasons",
            }
        ),
        "current state comparison",
    )
    expected_comparison = build_current_state_comparison(local_inventory)
    if current_state_comparison != expected_comparison:
        raise SupplyChainEvidenceError("current-versus-scanned comparison drifted")
    if (
        current_state_comparison["current_vs_scanned_match"] is not False
        or current_state_comparison["project_manifests_bound"] is not False
        or not current_state_comparison["mismatch_reasons"]
    ):
        raise SupplyChainEvidenceError("retained scan cannot apply to current state")

    local_inventory_summary = local_inventory["inventory"]
    if not isinstance(local_inventory_summary, Mapping):
        raise SupplyChainEvidenceError("local inventory summary is invalid")
    package_count = local_inventory_summary.get("package_count")
    if type(package_count) is not int:
        raise SupplyChainEvidenceError("current package inventory is invalid")

    # Advisory ID 전체와 target triage partition을 대조하고 exception 없는 NO_GO를 강제한다.
    security_findings = _require_exact_keys(
        root["security_findings"],
        frozenset(
            {"finding_record_count", "affected_package_count", "ids", "triage", "macos_target_triage"}
        ),
        "security findings",
    )
    finding_ids = security_findings["ids"]
    finding_count = security_findings["finding_record_count"]
    affected_package_count = security_findings["affected_package_count"]
    if (
        not isinstance(finding_ids, list)
        or not finding_ids
        or any(not isinstance(finding_id, str) or not finding_id for finding_id in finding_ids)
        or len(finding_ids) != len(set(finding_ids))
        or type(finding_count) is not int
        or finding_count != len(finding_ids)
        or set(finding_ids) != EXPECTED_FINDING_IDS
        or type(affected_package_count) is not int
        or affected_package_count != 17
    ):
        raise SupplyChainEvidenceError("security finding summary is invalid")
    triage = _require_exact_keys(
        security_findings["triage"],
        frozenset(
            {
                "unmaintained_records",
                "unsound_records",
                "moderate_alias_records",
                "glib_0_18_5_in_aarch64_apple_darwin_tree",
                "unmaintained_unic_0_9_0_in_aarch64_apple_darwin_tree",
                "release_exception_approved",
            }
        ),
        "security triage",
    )
    triage_counts = (
        triage["unmaintained_records"],
        triage["unsound_records"],
        triage["moderate_alias_records"],
    )
    if (
        any(type(record_count) is not int or record_count < 0 for record_count in triage_counts)
        or sum(triage_counts) != finding_count
        or triage_counts != (16, 1, 1)
        or triage["release_exception_approved"] is not False
        or triage["glib_0_18_5_in_aarch64_apple_darwin_tree"] is not False
        or triage["unmaintained_unic_0_9_0_in_aarch64_apple_darwin_tree"] is not True
    ):
        raise SupplyChainEvidenceError("security triage cannot approve release")
    target_triage = _require_exact_keys(
        security_findings["macos_target_triage"],
        frozenset(
            {
                "target",
                "reachable_unmaintained_ids",
                "non_reachable_ids",
                "immediate_lockfile_only_remediation_available",
                "remediation_note",
            }
        ),
        "macOS target triage",
    )
    reachable_ids = target_triage["reachable_unmaintained_ids"]
    non_reachable_ids = target_triage["non_reachable_ids"]
    if (
        target_triage["target"] != "aarch64-apple-darwin"
        or not isinstance(reachable_ids, list)
        or not reachable_ids
        or not isinstance(non_reachable_ids, list)
        or any(
            not isinstance(finding_id, str) or not finding_id
            for finding_id in (*reachable_ids, *non_reachable_ids)
        )
        or len(reachable_ids) != len(set(reachable_ids))
        or len(non_reachable_ids) != len(set(non_reachable_ids))
        or set(reachable_ids) & set(non_reachable_ids)
        or set(reachable_ids) | set(non_reachable_ids) != set(finding_ids)
        or set(reachable_ids) != EXPECTED_REACHABLE_FINDING_IDS
        or target_triage["immediate_lockfile_only_remediation_available"] is not False
        or not isinstance(target_triage["remediation_note"], str)
        or not target_triage["remediation_note"].strip()
    ):
        raise SupplyChainEvidenceError("macOS target triage is invalid")

    # Repository 자체의 proprietary 결정과 제3자 dependency의 미완료 검토를 분리해 확인한다.
    license_findings = _require_exact_keys(
        root["license_findings"],
        frozenset(
            {
                "policy_approved",
                "manual_review_required",
                "repository_license_declared",
                "packaged_contrib_runtime_hook_observed",
                "unresolved",
            }
        ),
        "license findings",
    )
    unresolved_license_groups = license_findings["unresolved"]
    local_license_inventory = local_inventory["license_inventory"]
    local_project_manifests = local_inventory["project_manifests"]
    if not isinstance(local_license_inventory, Mapping):
        raise SupplyChainEvidenceError("local license inventory is invalid")
    if (
        license_findings["policy_approved"] is not False
        or license_findings["manual_review_required"] is not True
        or license_findings["repository_license_declared"] is not True
        or local_license_inventory.get("declared_count") != 3
        or local_license_inventory.get("policy_approved") is not False
        or not isinstance(unresolved_license_groups, list)
        or not unresolved_license_groups
    ):
        raise SupplyChainEvidenceError("license findings cannot approve release")
    if not isinstance(local_project_manifests, list):
        raise SupplyChainEvidenceError("local project license inventory is invalid")
    observed_project_policy = {
        manifest.get("path"): (
            manifest.get("license_expression"),
            manifest.get("publish_disabled"),
        )
        for manifest in local_project_manifests
        if isinstance(manifest, Mapping)
    }
    if observed_project_policy != EXPECTED_PROJECT_LICENSE_POLICY:
        raise SupplyChainEvidenceError("project private license policy drifted")
    repository_license_files = local_license_inventory.get(
        "repository_license_files"
    )
    if (
        not isinstance(repository_license_files, list)
        or len(repository_license_files) != 1
        or not isinstance(repository_license_files[0], Mapping)
        or repository_license_files[0].get("path") != "LICENSE"
    ):
        raise SupplyChainEvidenceError("repository proprietary notice is missing")
    for unresolved_group in unresolved_license_groups:
        group = _require_exact_keys(
            unresolved_group,
            frozenset({"license", "count", "packages"}),
            "unresolved license group",
        )
        if (
            not isinstance(group["license"], str)
            or type(group["count"]) is not int
            or group["count"] <= 0
            or not isinstance(group["packages"], list)
            or group["count"] != len(group["packages"])
            or any(not isinstance(package, str) or not package for package in group["packages"])
        ):
            raise SupplyChainEvidenceError("unresolved license group is invalid")
    observed_license_groups = tuple(
        (
            group["license"],
            tuple(group["packages"]),
        )
        for group in unresolved_license_groups
        if isinstance(group, Mapping)
    )
    if observed_license_groups != EXPECTED_UNRESOLVED_LICENSE_GROUPS:
        raise SupplyChainEvidenceError("unresolved license findings drifted")

    # Historical raw command와 현재 local-only wrapper를 표시상·검증상 분리해 재실행 오인을 막는다.
    tool = _require_exact_keys(root["tool"], frozenset({"name", "version"}), "tool")
    commands = root["commands"]
    decision = root["decision"]
    if (
        tool["name"] != "osv-scanner"
        or tool["version"] != "2.5.1"
        or not isinstance(commands, list)
        or not commands
        or any(not isinstance(command, str) or not command for command in commands)
        or not isinstance(decision, str)
        or "NO_GO" not in decision
    ):
        raise SupplyChainEvidenceError("supply-chain audit metadata is invalid")

    # 현재 OSV operation은 network-deny wrapper 두 개만 허용하고 raw scanner text는 historical로 고정한다.
    current_local_commands = {
        command.removeprefix(CURRENT_LOCAL_COMMAND_PREFIX)
        for command in commands
        if command.startswith(CURRENT_LOCAL_COMMAND_PREFIX)
    }
    historical_commands = {
        command.removeprefix(HISTORICAL_COMMAND_PREFIX)
        for command in commands
        if command.startswith(HISTORICAL_COMMAND_PREFIX)
    }
    if not all(
        command.startswith(
            (HISTORICAL_COMMAND_PREFIX, CURRENT_LOCAL_COMMAND_PREFIX)
        )
        for command in commands
    ):
        raise SupplyChainEvidenceError("supply-chain command provenance is invalid")
    if not all(
        not command.removeprefix(CURRENT_LOCAL_COMMAND_PREFIX).startswith(
            "osv-scanner "
        )
        for command in commands
        if command.startswith(CURRENT_LOCAL_COMMAND_PREFIX)
    ):
        raise SupplyChainEvidenceError("raw current OSV command is prohibited")
    if (
        historical_commands != EXPECTED_HISTORICAL_COMMANDS
        or current_local_commands != EXPECTED_CURRENT_LOCAL_COMMANDS
        or len(commands)
        != len(EXPECTED_HISTORICAL_COMMANDS) + len(EXPECTED_CURRENT_LOCAL_COMMANDS)
    ):
        raise SupplyChainEvidenceError("supply-chain command allowlist drifted")
    return SupplyChainEvidenceSummary(
        package_count=package_count,
        retained_scan_package_count=scanned_package_count,
        current_vs_scanned_match=False,
        security_finding_count=finding_count,
        unresolved_license_group_count=len(unresolved_license_groups),
        exact_sbom_component_count=local_artifact_summary.component_count,
        third_party_license_declaration_count=(
            local_artifact_summary.license_declaration_count
        ),
        third_party_noassertion_count=local_artifact_summary.noassertion_count,
        current_phase13_release_candidate_bound=(
            local_artifact_summary.current_phase13_release_candidate_bound
        ),
        overall_status="NO_GO",
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> int:
    """
    함수 이름: main()
    기능: 인자 없는 local evidence binding 검사를 실행하고 NO_GO 상태를 명시적으로 출력한다.
    인자: argv -> 허용하지 않는 CLI 인자, repository_root -> 검증할 repository root
    반환값: 검증된 NO_GO 또는 malformed/stale evidence 모두 readiness 차단을 뜻하는 1
    작성 날짜: 2026/08/29
    """
    # 외부 경로나 relaxed policy를 받지 않도록 CLI를 인자 없는 고정 repository 검사로 제한한다.
    command_arguments = tuple(sys.argv[1:] if argv is None else argv)
    if command_arguments:
        print("supply-chain evidence checker accepts no arguments", file=sys.stderr)
        return 1
    # Current inventory와 retained NO_GO evidence를 strict schema·byte binding으로 함께 검증한다.
    try:
        summary = validate_supply_chain_evidence(repository_root=repository_root)
    except SupplyChainEvidenceError as error:
        print(f"supply-chain evidence invalid: {error}", file=sys.stderr)
        return 1

    # 구조 PASS와 release GO를 혼동하지 않도록 검증된 status를 출력에도 그대로 남긴다.
    print(
        "supply-chain evidence binding: "
        f"current_packages={summary.package_count} "
        f"retained_scan_packages={summary.retained_scan_package_count} "
        f"current_vs_scanned_match={str(summary.current_vs_scanned_match).lower()} "
        f"security_findings={summary.security_finding_count} "
        f"license_groups={summary.unresolved_license_group_count} "
        f"sbom_components={summary.exact_sbom_component_count} "
        "third_party_license_declarations="
        f"{summary.third_party_license_declaration_count} "
        f"third_party_noassertion={summary.third_party_noassertion_count} "
        "current_phase13_release_bound="
        f"{str(summary.current_phase13_release_candidate_bound).lower()} "
        f"validated_status={summary.overall_status}"
    )
    print(
        "supply-chain evidence gate is blocked by retained NO_GO findings",
        file=sys.stderr,
    )
    return 1  # Evidence가 정확해도 미해결 NO_GO를 readiness PASS로 승격하지 않는다.


if __name__ == "__main__":
    raise SystemExit(main())
