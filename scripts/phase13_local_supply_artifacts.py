#!/usr/bin/env python3
"""Phase 13 local lockfile 공급망 산출물을 외부 통신 없이 생성·검증한다."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tomllib
from typing import Any, Mapping, Protocol, Sequence
import uuid


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
OBSERVED_DATE = "2026-08-29"
LOCKFILE_SPECS = (
    (Path("backend/uv.lock"), "PyPI"),
    (Path("UI/pnpm-lock.yaml"), "npm"),
    (Path("UI/apps/desktop/src-tauri/Cargo.lock"), "crates.io"),
)
SBOM_PATH = Path("Design/Architecture/phase13_exact_lockfile_sbom.cdx.json")
LICENSE_INVENTORY_PATH = Path(
    "Design/Architecture/phase13_dependency_license_inventory.json"
)
NOTICE_REVIEW_PATH = Path(
    "Design/Architecture/phase13_third_party_notice_review.md"
)
HISTORICAL_RELEASE_BINDING_PATH = Path(
    "Design/Architecture/phase13_historical_release_artifact_binding.json"
)
HISTORICAL_APP_PATH = Path(
    "UI/apps/desktop/src-tauri/target/release/bundle/macos/"
    "Binance Auto Trader Phase12 Local Fixed.app"
)
HISTORICAL_DMG_PATH = Path(
    "UI/apps/desktop/src-tauri/target/release/bundle/dmg/"
    "Binance Auto Trader_0.1.0_aarch64_phase12-local-fixed.dmg"
)
EXPECTED_HISTORICAL_DMG_SHA256 = (
    "563136398d4d6544c0e1e585e67df66cf6f0d3ce61d6a7050eabe7c640964d05"
)
MAXIMUM_INPUT_BYTES = 64 * 1024 * 1024
MAXIMUM_JSON_BYTES = 4 * 1024 * 1024
MAXIMUM_APP_TREE_ENTRIES = 10_000
MAXIMUM_APP_FILE_BYTES = 64 * 1024 * 1024
MAXIMUM_APP_TOTAL_BYTES = 512 * 1024 * 1024
MAXIMUM_PACKAGE_METADATA_BYTES = 2 * 1024 * 1024
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
ALLOWED_LICENSE_SOURCE_KINDS = frozenset(
    {
        "python_dist_info_metadata",
        "npm_installed_package_json",
        "cargo_registry_manifest",
    }
)


class LocalSupplyArtifactError(RuntimeError):
    """
    클래스 이름: LocalSupplyArtifactError
    기능: Local-only SBOM, license inventory 또는 릴리스 결속 위반을 나타낸다.
    작성 날짜: 2026/08/29
    """


@dataclass(frozen=True)
class LockedComponent:
    """
    클래스 이름: LockedComponent
    기능: 한 exact lockfile package의 ecosystem, identity와 source를 보존한다.
    작성 날짜: 2026/08/29
    """

    ecosystem: str
    name: str
    version: str
    source: str
    lockfile_path: str
    project_component: bool

    @property
    def coordinate(self) -> str:
        """
        함수 이름: coordinate()
        기능: Ecosystem·name·version·source가 결합된 canonical coordinate를 반환한다.
        인자: 없음
        반환값: canonical package coordinate
        작성 날짜: 2026/08/29
        """
        return f"{self.ecosystem}:{self.name}@{self.version}|{self.source}"  # Package 출처까지 identity에 포함한다.

    @property
    def bom_ref(self) -> str:
        """
        함수 이름: bom_ref()
        기능: Coordinate에서 안정적이고 충돌 없는 CycloneDX reference를 만든다.
        인자: 없음
        반환값: SHA-256 기반 bom-ref
        작성 날짜: 2026/08/29
        """
        # Canonical coordinate의 digest로 SBOM 안의 충돌 없는 reference를 만든다.
        coordinate_hash = hashlib.sha256(self.coordinate.encode("utf-8")).hexdigest()
        return f"urn:binance-auto:locked-component:{coordinate_hash}"


@dataclass(frozen=True)
class LocalSupplyArtifactSummary:
    """
    클래스 이름: LocalSupplyArtifactSummary
    기능: 검증된 local SBOM·license·notice·historical release 집계를 보존한다.
    작성 날짜: 2026/08/29
    """

    component_count: int
    third_party_component_count: int
    license_declaration_count: int
    noassertion_count: int
    notice_complete: bool
    current_phase13_release_candidate_bound: bool


@dataclass(frozen=True)
class LicenseObservation:
    """
    클래스 이름: LicenseObservation
    기능: Exact package version의 local installed metadata에서 관찰한 license 선언을 보존한다.
    작성 날짜: 2026/08/29
    """

    license_expression: str
    source_kind: str
    source_identity: str
    source_sha256: str
    metadata_field: str


class _DigestAccumulator(Protocol):
    """
    클래스 이름: _DigestAccumulator
    기능: App tree helper가 사용하는 SHA-256 update 경계를 타입으로 제한한다.
    작성 날짜: 2026/08/31
    """

    def update(self, data: bytes) -> None:
        """
        함수 이름: update()
        기능: 지정 bytes를 누적 digest에 반영한다.
        인자: data -> hash에 추가할 bytes
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        ...


@dataclass
class _AppTreeCounters:
    """
    클래스 이름: _AppTreeCounters
    기능: Historical app traversal의 entry·file·total byte 상한 상태를 누적한다.
    작성 날짜: 2026/08/31
    """

    entry_count: int = 0
    file_count: int = 0
    total_file_bytes: int = 0


def _sha256(file_bytes: bytes) -> str:
    """
    함수 이름: _sha256()
    기능: 입력 bytes의 lowercase SHA-256을 계산한다.
    인자: file_bytes -> hash할 bytes
    반환값: 64자리 lowercase SHA-256
    작성 날짜: 2026/08/29
    """
    return hashlib.sha256(file_bytes).hexdigest()  # 산출물 원문의 byte identity를 고정한다.


def _require_relative_path_parts(relative_path: Path) -> tuple[str, ...]:
    """
    함수 이름: _require_relative_path_parts()
    기능: Local artifact 상대 경로를 escape 없는 component tuple로 검증한다.
    인자: relative_path -> trusted root 기준 경로
    반환값: 순서가 보존된 non-empty path component tuple
    작성 날짜: 2026/08/31
    """
    if not isinstance(relative_path, Path) or relative_path.is_absolute():
        raise LocalSupplyArtifactError("local supply artifact path is invalid")
    path_parts = relative_path.parts
    if not path_parts or any(path_part in {"", ".", ".."} for path_part in path_parts):
        raise LocalSupplyArtifactError("local supply artifact path is invalid")
    return path_parts  # Dir-fd traversal에는 검증된 lexical component만 전달한다.


def _open_directory_chain(
    root_directory: Path,
    directory_parts: Sequence[str],
) -> tuple[list[int], list[tuple[int, str, os.stat_result]]]:
    """
    함수 이름: _open_directory_chain()
    기능: Trusted root부터 각 directory를 no-follow descriptor로 순차 고정한다.
    인자: root_directory -> repository 또는 filesystem root,
        directory_parts -> leaf parent까지의 상대 component sequence
    반환값: 열린 directory descriptor들과 parent-entry identity tuple
    작성 날짜: 2026/08/31
    """
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory_descriptors: list[int] = []
    directory_links: list[tuple[int, str, os.stat_result]] = []

    # Root와 모든 child를 descriptor-relative로 열어 parent symlink race를 차단한다.
    try:
        root_descriptor = os.open(root_directory, directory_flags)
        directory_descriptors.append(root_descriptor)
        if not stat.S_ISDIR(os.fstat(root_descriptor).st_mode):
            raise LocalSupplyArtifactError("local supply root is invalid")
        current_descriptor = root_descriptor
        for directory_name in directory_parts:
            path_status = os.stat(
                directory_name,
                dir_fd=current_descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(path_status.st_mode):
                raise LocalSupplyArtifactError("local supply artifact path is invalid")
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
                raise LocalSupplyArtifactError("local supply artifact path changed")
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
    기능: 열린 parent chain의 directory entry가 같은 inode인지 다시 검증한다.
    인자: directory_links -> parent fd, component 이름, 최초 child 상태 sequence
    반환값: 없음
    작성 날짜: 2026/08/31
    """
    # 모든 parent fd를 닫기 전에 entry를 다시 stat해 rename·symlink 교체를 거부한다.
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
            raise LocalSupplyArtifactError("local supply artifact path changed")


def _read_bounded_regular_bytes_at(
    root_directory: Path,
    relative_path: Path,
    maximum_bytes: int,
    artifact_label: str,
) -> bytes:
    """
    함수 이름: _read_bounded_regular_bytes_at()
    기능: Trusted root 아래 regular leaf를 nonblocking bounded snapshot으로 읽는다.
    인자: root_directory -> repository 또는 filesystem root,
        relative_path -> root 기준 leaf 경로, maximum_bytes -> 최대 byte,
        artifact_label -> 고정 오류 범주
    반환값: identity·metadata 검증을 통과한 bytes
    작성 날짜: 2026/08/31
    """
    if type(maximum_bytes) is not int or maximum_bytes <= 0:
        raise ValueError("maximum_bytes must be a positive integer")
    if artifact_label not in {"local supply artifact", "local package metadata"}:
        raise ValueError("artifact_label is invalid")
    path_parts = _require_relative_path_parts(relative_path)
    directory_descriptors: list[int] = []

    # Parent fd chain을 유지한 상태에서 O_NONBLOCK leaf를 열어 FIFO도 기다리지 않고 거부한다.
    try:
        directory_descriptors, directory_links = _open_directory_chain(
            root_directory,
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
            raise LocalSupplyArtifactError(f"{artifact_label} path is invalid")
        file_descriptor = os.open(
            leaf_name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent_descriptor,
        )
        try:
            initial_status = os.fstat(file_descriptor)
            if not stat.S_ISREG(initial_status.st_mode):
                raise LocalSupplyArtifactError(
                    f"{artifact_label} must be a regular file"
                )
            if initial_status.st_size <= 0 or initial_status.st_size > maximum_bytes:
                raise LocalSupplyArtifactError(f"{artifact_label} size is invalid")
            if (
                path_status_before.st_dev != initial_status.st_dev
                or path_status_before.st_ino != initial_status.st_ino
            ):
                raise LocalSupplyArtifactError(f"{artifact_label} changed during read")

            # Initial size를 신뢰하지 않고 max+1 byte까지만 chunk로 읽는다.
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
        raise LocalSupplyArtifactError(f"{artifact_label} read failed") from error
    finally:
        for directory_descriptor in reversed(directory_descriptors):
            os.close(directory_descriptor)

    # Fd와 path의 identity·size·mtime·ctime 및 실제 길이가 모두 고정됐을 때만 bytes를 신뢰한다.
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
        raise LocalSupplyArtifactError(f"{artifact_label} changed during read")
    return file_bytes  # Caller는 검증된 immutable snapshot만 parser나 digest에 사용한다.


def _read_regular_bytes(
    repository_root: Path,
    relative_path: Path,
    maximum_bytes: int = MAXIMUM_INPUT_BYTES,
) -> bytes:
    """
    함수 이름: _read_regular_bytes()
    기능: Repository 안 symlink가 아닌 bounded regular file을 bytes로 읽는다.
    인자: repository_root -> repository root, relative_path -> root 기준 경로,
        maximum_bytes -> 허용할 최대 byte 수
    반환값: 파일 원문 bytes
    작성 날짜: 2026/08/29
    """
    return _read_bounded_regular_bytes_at(
        repository_root,
        relative_path,
        maximum_bytes,
        "local supply artifact",
    )  # Repository reader도 공통 descriptor contract만 사용한다.


def _read_local_metadata_bytes(
    metadata_path: Path,
    maximum_bytes: int = MAXIMUM_PACKAGE_METADATA_BYTES,
) -> bytes:
    """
    함수 이름: _read_local_metadata_bytes()
    기능: Repository 또는 local package cache의 symlink가 아닌 bounded metadata를 안정적으로 읽는다.
    인자: metadata_path -> installed package metadata path,
        maximum_bytes -> 허용할 최대 byte 수
    반환값: 관찰한 metadata bytes
    작성 날짜: 2026/08/29
    """
    if not isinstance(metadata_path, Path) or not metadata_path.is_absolute():
        raise LocalSupplyArtifactError("local package metadata path is invalid")

    # Filesystem root에서 모든 parent component를 no-follow로 열어 cache parent race도 차단한다.
    filesystem_root = Path(metadata_path.anchor)
    relative_metadata_path = Path(*metadata_path.parts[1:])
    return _read_bounded_regular_bytes_at(
        filesystem_root,
        relative_metadata_path,
        maximum_bytes,
        "local package metadata",
    )


def _normalize_pypi_name(package_name: str) -> str:
    """
    함수 이름: _normalize_pypi_name()
    기능: PyPI project name의 hyphen/underscore/dot 차이를 canonical match key로 변환한다.
    인자: package_name -> package metadata의 project name
    반환값: PEP 503 형태의 lowercase name
    작성 날짜: 2026/08/29
    """
    return re.sub(r"[-_.]+", "-", package_name).lower()  # PEP 503 동치 표기를 하나로 통합한다.


def _normalize_license_expression(raw_license: object) -> str | None:
    """
    함수 이름: _normalize_license_expression()
    기능: Local metadata의 단일 license 선언을 one-line text로 정규화한다.
    인자: raw_license -> string 또는 npm legacy license object
    반환값: 빈 값이 아닌 normalized 선언 또는 None
    작성 날짜: 2026/08/29
    """
    # Legacy npm object는 type field만 표준 문자열 후보로 선택한다.
    selected_license = raw_license
    if isinstance(raw_license, Mapping):
        selected_license = raw_license.get("type")
    if not isinstance(selected_license, str):
        return None
    normalized_license = re.sub(r"\s+", " ", selected_license).strip()
    if not normalized_license or len(normalized_license) > 512:
        return None
    return normalized_license


def _append_license_observation(
    observations: dict[tuple[str, str, str], list[LicenseObservation]],
    *,
    ecosystem: str,
    package_name: str,
    package_version: str,
    license_expression: str,
    source_kind: str,
    source_identity: str,
    source_sha256: str,
    metadata_field: str,
) -> None:
    """
    함수 이름: _append_license_observation()
    기능: Exact ecosystem/name/version key에 검증 가능한 local license 관찰을 추가한다.
    인자: observations -> 누적 mapping, ecosystem/name/version -> package identity,
        license_expression -> 관찰 선언, source_kind/identity/sha256/field -> 출처 증거
    반환값: 없음
    작성 날짜: 2026/08/29
    """
    # 승인된 local metadata 종류만 provenance로 누적한다.
    if source_kind not in ALLOWED_LICENSE_SOURCE_KINDS:
        raise LocalSupplyArtifactError("local license source kind is invalid")
    normalized_name = (
        _normalize_pypi_name(package_name)
        if ecosystem == "PyPI"
        else package_name
    )
    observation_key = (ecosystem, normalized_name, package_version)
    observation = LicenseObservation(
        license_expression=license_expression,
        source_kind=source_kind,
        source_identity=source_identity,
        source_sha256=source_sha256,
        metadata_field=metadata_field,
    )
    observations.setdefault(observation_key, []).append(observation)


def _collect_python_license_observations(
    repository_root: Path,
    observations: dict[tuple[str, str, str], list[LicenseObservation]],
) -> None:
    """
    함수 이름: _collect_python_license_observations()
    기능: Local backend venv의 exact dist-info license field를 network 없이 수집한다.
    인자: repository_root -> repository root, observations -> 누적 mapping
    반환값: 없음
    작성 날짜: 2026/08/29
    """
    virtual_environment_root = repository_root.resolve() / "backend" / ".venv"
    if not virtual_environment_root.is_dir() or virtual_environment_root.is_symlink():
        return

    # Direct dist-info METADATA만 읽어 setuptools 내부 vendored metadata를 lock package로 오인하지 않는다.
    metadata_paths = sorted(
        virtual_environment_root.glob("lib/python*/site-packages/*.dist-info/METADATA"),
        key=lambda path: path.as_posix(),
    )
    for metadata_path in metadata_paths:
        try:
            metadata_bytes = _read_local_metadata_bytes(metadata_path)
            metadata = BytesParser().parsebytes(metadata_bytes)
        except (LocalSupplyArtifactError, TypeError, ValueError):
            continue
        package_name = metadata.get("Name")
        package_version = metadata.get("Version")
        expression_field = "License-Expression"
        license_expression = _normalize_license_expression(
            metadata.get(expression_field)
        )
        if license_expression is None:
            expression_field = "License"
            license_expression = _normalize_license_expression(
                metadata.get(expression_field)
            )
        if (
            not isinstance(package_name, str)
            or not package_name
            or not isinstance(package_version, str)
            or not package_version
            or license_expression is None
        ):
            continue
        source_identity = metadata_path.relative_to(repository_root.resolve()).as_posix()
        _append_license_observation(
            observations,
            ecosystem="PyPI",
            package_name=package_name,
            package_version=package_version,
            license_expression=license_expression,
            source_kind="python_dist_info_metadata",
            source_identity=source_identity,
            source_sha256=_sha256(metadata_bytes),
            metadata_field=expression_field,
        )


def _collect_npm_license_observations(
    repository_root: Path,
    observations: dict[tuple[str, str, str], list[LicenseObservation]],
) -> None:
    """
    함수 이름: _collect_npm_license_observations()
    기능: Local node_modules의 installed package.json license 선언을 network 없이 수집한다.
    인자: repository_root -> repository root, observations -> 누적 mapping
    반환값: 없음
    작성 날짜: 2026/08/29
    """
    node_modules_root = repository_root.resolve() / "UI" / "node_modules"
    if not node_modules_root.is_dir() or node_modules_root.is_symlink():
        return

    # os.walk은 directory symlink를 따라가지 않고 installed tree의 physical manifest만 방문한다.
    metadata_paths: list[Path] = []
    for current_root, directory_names, file_names in os.walk(
        node_modules_root,
        topdown=True,
        followlinks=False,
    ):
        directory_names[:] = sorted(
            [directory_name for directory_name in directory_names if directory_name != ".pnpm"],
            key=os.fsencode,
        )
        if "package.json" in file_names:
            metadata_paths.append(Path(current_root) / "package.json")
    for metadata_path in sorted(metadata_paths, key=lambda path: path.as_posix()):
        try:
            metadata_bytes = _read_local_metadata_bytes(metadata_path)
            package_metadata = json.loads(
                metadata_bytes.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonstandard_constant,
            )
        except (
            LocalSupplyArtifactError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ):
            continue
        if not isinstance(package_metadata, Mapping):
            continue
        package_name = package_metadata.get("name")
        package_version = package_metadata.get("version")
        license_expression = _normalize_license_expression(
            package_metadata.get("license")
        )
        if (
            not isinstance(package_name, str)
            or not package_name
            or not isinstance(package_version, str)
            or not package_version
            or license_expression is None
        ):
            continue
        source_identity = metadata_path.relative_to(repository_root.resolve()).as_posix()
        _append_license_observation(
            observations,
            ecosystem="npm",
            package_name=package_name,
            package_version=package_version,
            license_expression=license_expression,
            source_kind="npm_installed_package_json",
            source_identity=source_identity,
            source_sha256=_sha256(metadata_bytes),
            metadata_field="license",
        )


def _collect_cargo_license_observations(
    observations: dict[tuple[str, str, str], list[LicenseObservation]],
) -> None:
    """
    함수 이름: _collect_cargo_license_observations()
    기능: Fixed local Cargo registry cache의 Cargo.toml.orig license 선언을 수집한다.
    인자: observations -> 누적 mapping
    반환값: 없음
    작성 날짜: 2026/08/29
    """
    cargo_registry_root = Path.home() / ".cargo" / "registry" / "src"
    if not cargo_registry_root.is_dir() or cargo_registry_root.is_symlink():
        return

    # User-specific absolute path는 evidence에 남기지 않고 registry/crate identity와 hash만 보존한다.
    metadata_paths = sorted(
        cargo_registry_root.glob("*/*/Cargo.toml.orig"),
        key=lambda path: path.as_posix(),
    )
    for metadata_path in metadata_paths:
        try:
            metadata_bytes = _read_local_metadata_bytes(metadata_path)
            package_metadata = tomllib.loads(metadata_bytes.decode("utf-8"))
        except (
            LocalSupplyArtifactError,
            UnicodeDecodeError,
            tomllib.TOMLDecodeError,
            TypeError,
            ValueError,
        ):
            continue
        package = package_metadata.get("package")
        if not isinstance(package, Mapping):
            continue
        package_name = package.get("name")
        package_version = package.get("version")
        license_expression = _normalize_license_expression(package.get("license"))
        if (
            not isinstance(package_name, str)
            or not package_name
            or not isinstance(package_version, str)
            or not package_version
            or license_expression is None
        ):
            continue
        source_identity = (
            "cargo-registry:"
            f"{metadata_path.parent.name}/Cargo.toml.orig"
        )
        _append_license_observation(
            observations,
            ecosystem="crates.io",
            package_name=package_name,
            package_version=package_version,
            license_expression=license_expression,
            source_kind="cargo_registry_manifest",
            source_identity=source_identity,
            source_sha256=_sha256(metadata_bytes),
            metadata_field="package.license",
        )


def collect_local_license_observations(
    repository_root: Path = REPOSITORY_ROOT,
) -> dict[tuple[str, str, str], tuple[LicenseObservation, ...]]:
    """
    함수 이름: collect_local_license_observations()
    기능: Python/npm/Cargo local installed metadata의 license 관찰을 외부 통신 없이 집계한다.
    인자: repository_root -> repository root
    반환값: Ecosystem/name/version별 정렬된 observation tuple mapping
    작성 날짜: 2026/08/29
    """
    observations: dict[tuple[str, str, str], list[LicenseObservation]] = {}

    # 세 ecosystem은 서로 다른 local metadata 형식이므로 독립 parser로 수집한다.
    _collect_python_license_observations(repository_root, observations)
    _collect_npm_license_observations(repository_root, observations)
    _collect_cargo_license_observations(observations)
    return {
        package_key: tuple(
            sorted(
                package_observations,
                key=lambda item: (
                    item.license_expression,
                    item.source_kind,
                    item.source_identity,
                    item.source_sha256,
                ),
            )
        )
        for package_key, package_observations in observations.items()
    }


def _reject_duplicate_keys(object_pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """
    함수 이름: _reject_duplicate_keys()
    기능: JSON duplicate key를 silent overwrite 대신 거부한다.
    인자: object_pairs -> decoder가 전달한 key/value pair
    반환값: duplicate key가 없는 dictionary
    작성 날짜: 2026/08/29
    """
    loaded_object: dict[str, Any] = {}

    # 첫 key를 보존하고 같은 key가 반복되면 즉시 산출물을 거부한다.
    for object_key, object_value in object_pairs:
        if object_key in loaded_object:
            raise LocalSupplyArtifactError("local supply JSON contains duplicate keys")
        loaded_object[object_key] = object_value
    return loaded_object


def _reject_nonstandard_constant(constant_name: str) -> None:
    """
    함수 이름: _reject_nonstandard_constant()
    기능: NaN과 Infinity 같은 JSON 표준 밖 숫자를 거부한다.
    인자: constant_name -> 반사하지 않을 raw token
    반환값: 정상 반환하지 않음
    작성 날짜: 2026/08/29
    """
    del constant_name  # Untrusted raw token은 오류 문구에 남기지 않는다.
    raise LocalSupplyArtifactError("local supply JSON contains non-standard numbers")


def _load_json_object(repository_root: Path, relative_path: Path) -> dict[str, object]:
    """
    함수 이름: _load_json_object()
    기능: Bounded UTF-8 JSON을 duplicate/non-standard 값 없이 object로 읽는다.
    인자: repository_root -> repository root, relative_path -> JSON 상대 경로
    반환값: JSON root dictionary
    작성 날짜: 2026/08/29
    """
    # Bounded reader를 먼저 통과한 bytes만 strict JSON decoder에 전달한다.
    json_bytes = _read_regular_bytes(
        repository_root,
        relative_path,
        MAXIMUM_JSON_BYTES,
    )
    try:
        loaded_value = json.loads(
            json_bytes.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LocalSupplyArtifactError("local supply JSON could not be loaded") from error
    if not isinstance(loaded_value, dict):
        raise LocalSupplyArtifactError("local supply JSON root must be an object")
    return loaded_value


def _decode_pnpm_key(encoded_key: str) -> str:
    """
    함수 이름: _decode_pnpm_key()
    기능: pnpm YAML의 plain/single/double quoted package key를 복원한다.
    인자: encoded_key -> colon을 제외한 YAML key text
    반환값: package name@version key
    작성 날짜: 2026/08/29
    """
    # Quoted key는 YAML quote 규칙을 적용하고 plain key는 원문을 그대로 보존한다.
    if len(encoded_key) >= 2 and encoded_key[0] == encoded_key[-1] == "'":
        return encoded_key[1:-1].replace("''", "'")
    if len(encoded_key) >= 2 and encoded_key[0] == encoded_key[-1] == '"':
        try:
            decoded_key = json.loads(encoded_key)
        except json.JSONDecodeError as error:
            raise LocalSupplyArtifactError("pnpm package key is invalid") from error
        if isinstance(decoded_key, str):
            return decoded_key
    if encoded_key.startswith(("'", '"')) or encoded_key.endswith(("'", '"')):
        raise LocalSupplyArtifactError("pnpm package key quote is invalid")
    return encoded_key


def _parse_toml_components(
    lockfile_bytes: bytes,
    *,
    ecosystem: str,
    lockfile_path: str,
) -> tuple[LockedComponent, ...]:
    """
    함수 이름: _parse_toml_components()
    기능: uv/Cargo TOML의 모든 package identity와 source를 순서대로 읽는다.
    인자: lockfile_bytes -> TOML bytes, ecosystem -> ecosystem 이름,
        lockfile_path -> source lockfile 경로
    반환값: exact LockedComponent tuple
    작성 날짜: 2026/08/29
    """
    try:
        lockfile = tomllib.loads(lockfile_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise LocalSupplyArtifactError("TOML lockfile could not be parsed") from error
    packages = lockfile.get("package")
    if not isinstance(packages, list) or not packages:
        raise LocalSupplyArtifactError("TOML lockfile package list is invalid")

    # Source object는 key 순서에 의존하지 않는 compact JSON으로 canonicalize한다.
    components: list[LockedComponent] = []
    for package in packages:
        if not isinstance(package, dict):
            raise LocalSupplyArtifactError("TOML lockfile package entry is invalid")
        package_name = package.get("name")
        package_version = package.get("version")
        package_source = package.get("source")
        if not all(
            isinstance(value, str) and value
            for value in (package_name, package_version)
        ):
            raise LocalSupplyArtifactError("TOML package identity is invalid")
        if package_source is None:
            source_text = "workspace"
            project_component = True
        elif isinstance(package_source, str) and package_source:
            source_text = package_source
            project_component = False
        elif isinstance(package_source, dict) and package_source:
            source_text = json.dumps(
                package_source,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            project_component = "editable" in package_source
        else:
            raise LocalSupplyArtifactError("TOML package source is invalid")
        components.append(
            LockedComponent(
                ecosystem=ecosystem,
                name=package_name,
                version=package_version,
                source=source_text,
                lockfile_path=lockfile_path,
                project_component=project_component,
            )
        )
    return tuple(components)


def _parse_pnpm_components(
    lockfile_bytes: bytes,
    *,
    lockfile_path: str,
) -> tuple[LockedComponent, ...]:
    """
    함수 이름: _parse_pnpm_components()
    기능: pnpm v9 packages section의 exact package name/version을 network 없이 읽는다.
    인자: lockfile_bytes -> pnpm-lock YAML bytes, lockfile_path -> source lockfile 경로
    반환값: exact LockedComponent tuple
    작성 날짜: 2026/08/29
    """
    try:
        lockfile_lines = lockfile_bytes.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise LocalSupplyArtifactError("pnpm lockfile is not UTF-8") from error
    section_indexes = [
        line_index
        for line_index, line in enumerate(lockfile_lines)
        if line == "packages:"
    ]
    if len(section_indexes) != 1:
        raise LocalSupplyArtifactError("pnpm packages section is missing or duplicated")

    # 두 칸 들여쓴 direct package key만 읽고 metadata와 snapshots section은 제외한다.
    package_key_pattern = re.compile(r"^  (?P<key>\S.*):\s*\Z")
    components: list[LockedComponent] = []
    for line in lockfile_lines[section_indexes[0] + 1 :]:
        if line and not line.startswith(" "):
            break
        package_match = package_key_pattern.fullmatch(line)
        if package_match is None:
            continue
        package_key = _decode_pnpm_key(package_match.group("key"))
        package_name, separator, package_version = package_key.rpartition("@")
        if not separator or not package_name or not package_version:
            raise LocalSupplyArtifactError("pnpm package identity is invalid")
        components.append(
            LockedComponent(
                ecosystem="npm",
                name=package_name,
                version=package_version,
                source="pnpm-lock-packages",
                lockfile_path=lockfile_path,
                project_component=False,
            )
        )
    return tuple(components)


def read_exact_lockfile_components(
    repository_root: Path = REPOSITORY_ROOT,
) -> tuple[LockedComponent, ...]:
    """
    함수 이름: read_exact_lockfile_components()
    기능: Canonical 세 lockfile의 모든 package를 deterministic 순서로 반환한다.
    인자: repository_root -> repository root
    반환값: coordinate로 정렬된 LockedComponent tuple
    작성 날짜: 2026/08/29
    """
    components: list[LockedComponent] = []

    # Ecosystem별 parser를 fixed lockfile 경로에만 적용해 caller 입력을 받지 않는다.
    for lockfile_path, ecosystem in LOCKFILE_SPECS:
        lockfile_bytes = _read_regular_bytes(repository_root, lockfile_path)
        if ecosystem == "npm":
            parsed_components = _parse_pnpm_components(
                lockfile_bytes,
                lockfile_path=lockfile_path.as_posix(),
            )
        else:
            parsed_components = _parse_toml_components(
                lockfile_bytes,
                ecosystem=ecosystem,
                lockfile_path=lockfile_path.as_posix(),
            )
        components.extend(parsed_components)
    sorted_components = tuple(sorted(components, key=lambda item: item.coordinate))
    coordinates = [component.coordinate for component in sorted_components]
    if not sorted_components or len(coordinates) != len(set(coordinates)):
        raise LocalSupplyArtifactError("exact lockfile coordinate is empty or duplicated")
    return sorted_components


def _build_lockfile_inputs(
    repository_root: Path,
    components: Sequence[LockedComponent],
) -> list[dict[str, object]]:
    """
    함수 이름: _build_lockfile_inputs()
    기능: Lockfile byte hash와 package coordinate hash를 ecosystem별로 결합한다.
    인자: repository_root -> repository root, components -> exact component 목록
    반환값: canonical input reference list
    작성 날짜: 2026/08/29
    """
    lockfile_inputs: list[dict[str, object]] = []

    # Package 수와 coordinate digest를 lockfile byte hash와 함께 보존한다.
    for lockfile_path, ecosystem in LOCKFILE_SPECS:
        lockfile_bytes = _read_regular_bytes(repository_root, lockfile_path)
        lockfile_coordinates = [
            component.coordinate
            for component in components
            if component.lockfile_path == lockfile_path.as_posix()
        ]
        coordinate_bytes = ("\n".join(lockfile_coordinates) + "\n").encode("utf-8")
        lockfile_inputs.append(
            {
                "path": lockfile_path.as_posix(),
                "ecosystem": ecosystem,
                "sha256": _sha256(lockfile_bytes),
                "package_count": len(lockfile_coordinates),
                "coordinates_sha256": _sha256(coordinate_bytes),
            }
        )
    return lockfile_inputs


def build_exact_lockfile_sbom(
    repository_root: Path = REPOSITORY_ROOT,
) -> dict[str, object]:
    """
    함수 이름: build_exact_lockfile_sbom()
    기능: 세 lockfile의 1:1 component를 담은 deterministic CycloneDX 1.6 SBOM을 만든다.
    인자: repository_root -> repository root
    반환값: canonical CycloneDX JSON object
    작성 날짜: 2026/08/29
    """
    components = read_exact_lockfile_components(repository_root)
    lockfile_inputs = _build_lockfile_inputs(repository_root, components)
    serial_seed = "|".join(
        str(lockfile_input["sha256"]) for lockfile_input in lockfile_inputs
    )
    serial_number = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, serial_seed)}"

    # License·advisory를 추측하지 않고 lockfile에서 관찰한 identity/source만 보존한다.
    sbom_components = [
        {
            "type": "application" if component.project_component else "library",
            "bom-ref": component.bom_ref,
            "name": component.name,
            "version": component.version,
            "properties": [
                {"name": "binance-auto:ecosystem", "value": component.ecosystem},
                {
                    "name": "binance-auto:lockfile-path",
                    "value": component.lockfile_path,
                },
                {"name": "binance-auto:source", "value": component.source},
                {
                    "name": "binance-auto:exact-coordinate",
                    "value": component.coordinate,
                },
            ],
        }
        for component in components
    ]
    metadata_properties = [
        {
            "name": f"binance-auto:input:{lockfile_input['path']}:sha256",
            "value": lockfile_input["sha256"],
        }
        for lockfile_input in lockfile_inputs
    ]
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": serial_number,
        "version": 1,
        "metadata": {
            "timestamp": f"{OBSERVED_DATE}T00:00:00Z",
            "tools": {
                "components": [
                    {
                        "type": "application",
                        "name": "binance-auto-local-lockfile-sbom-generator",
                        "version": "1",
                    }
                ]
            },
            "properties": metadata_properties,
        },
        "components": sbom_components,
    }


def build_dependency_license_inventory(
    repository_root: Path = REPOSITORY_ROOT,
) -> dict[str, object]:
    """
    함수 이름: build_dependency_license_inventory()
    기능: Exact lockfile coordinate 전체의 license 확인 상태를 추측 없이 집계한다.
    인자: repository_root -> repository root
    반환값: Coordinate-complete, metadata-incomplete license inventory
    작성 날짜: 2026/08/29
    """
    components = read_exact_lockfile_components(repository_root)
    lockfile_inputs = _build_lockfile_inputs(repository_root, components)
    local_observations = collect_local_license_observations(repository_root)
    third_party_components = [
        component for component in components if not component.project_component
    ]

    # Name/version이 exact하게 일치한 local metadata만 선언으로 사용하고 충돌은 NOASSERTION으로 남긴다.
    inventory_entries: list[dict[str, object]] = []
    declared_count = 0
    noassertion_count = 0
    conflicting_count = 0
    matched_source_identities: set[tuple[str, str, str]] = set()
    for component in components:
        normalized_component_name = (
            _normalize_pypi_name(component.name)
            if component.ecosystem == "PyPI"
            else component.name
        )
        observation_key = (
            component.ecosystem,
            normalized_component_name,
            component.version,
        )
        component_observations = local_observations.get(observation_key, ())
        expressions = sorted(
            {observation.license_expression for observation in component_observations}
        )
        evidence_sources = [
            {
                "kind": observation.source_kind,
                "source_identity": observation.source_identity,
                "sha256": observation.source_sha256,
                "metadata_field": observation.metadata_field,
            }
            for observation in component_observations
        ]
        for observation in component_observations:
            if not component.project_component:
                matched_source_identities.add(
                    (
                        observation.source_kind,
                        observation.source_identity,
                        observation.source_sha256,
                    )
                )

        if component.project_component:
            license_expression = None
            license_status = "PROJECT_POLICY_SEPARATE"
            conflicting_expressions: list[str] = []
            evidence_sources = []
        elif len(expressions) == 1:
            license_expression = expressions[0]
            license_status = "DECLARED_LOCAL_METADATA"
            conflicting_expressions = []
            declared_count += 1
        elif len(expressions) > 1:
            license_expression = None
            license_status = "NOASSERTION_CONFLICTING_LOCAL_METADATA"
            conflicting_expressions = expressions
            noassertion_count += 1
            conflicting_count += 1
        else:
            license_expression = None
            license_status = "NOASSERTION_NO_LOCAL_METADATA"
            conflicting_expressions = []
            noassertion_count += 1
        inventory_entries.append(
            {
                "bom_ref": component.bom_ref,
                "coordinate": component.coordinate,
                "ecosystem": component.ecosystem,
                "name": component.name,
                "version": component.version,
                "scope": (
                    "project_component"
                    if component.project_component
                    else "third_party"
                ),
                "license_expression": license_expression,
                "license_status": license_status,
                "evidence_sources": evidence_sources,
                "conflicting_license_expressions": conflicting_expressions,
            }
        )
    return {
        "schema_version": 1,
        "record_type": "phase13_dependency_license_inventory",
        "observed_date": OBSERVED_DATE,
        "scope": "exact_lockfile_coordinates",
        "inputs": lockfile_inputs,
        "summary": {
            "package_count": len(components),
            "project_component_count": len(components) - len(third_party_components),
            "third_party_component_count": len(third_party_components),
            "third_party_license_declaration_observed_count": declared_count,
            "third_party_noassertion_count": noassertion_count,
            "third_party_conflicting_metadata_count": conflicting_count,
            "matched_local_metadata_source_count": len(matched_source_identities),
            "coordinate_inventory_complete": True,
            "license_metadata_complete": False,
            "license_texts_complete": False,
            "manual_review_required": True,
        },
        "packages": inventory_entries,
        "limitations": [
            "The exact lockfiles do not contain complete third-party license metadata.",
            "Observed local metadata declarations are not legal approval or a complete license-text review.",
            "NOASSERTION is an explicit unresolved or conflicting state and is not a license approval.",
            "Historical OSV license summaries are not promoted to current raw evidence.",
        ],
    }


def build_third_party_notice_review_from_inventory(
    inventory: Mapping[str, object],
) -> str:
    """
    함수 이름: build_third_party_notice_review_from_inventory()
    기능: 검증된 license inventory에서 release-blocking notice 검토본을 재생성한다.
    인자: inventory -> exact coordinate license inventory object
    반환값: Deterministic Markdown notice review text
    작성 날짜: 2026/08/29
    """
    packages = inventory.get("packages")
    summary = inventory.get("summary")
    if not isinstance(packages, list) or not isinstance(summary, Mapping):
        raise LocalSupplyArtifactError("license inventory could not build notice review")

    # 완성된 NOTICE로 오인하지 않도록 NO_GO와 잔여 NOASSERTION을 문서 첫 부분에 고정한다.
    lines = [
        "# Phase 13 Third-Party Notice Review (NOT RELEASE-READY)",
        "",
        f"Observed date: `{OBSERVED_DATE}`",
        "",
        "Status: **NO_GO — manual license and notice review is required.**",
        "",
        "This review artifact enumerates the exact lockfile coordinate scope. It is not the",
        "required final third-party notice and must not be packaged or presented as license",
        "approval. A local metadata declaration is version-matched evidence, not legal review;",
        "`NOASSERTION` means that no single declaration could be retained for that coordinate.",
        "",
        "## Scope summary",
        "",
        f"- Exact lockfile components: {summary['package_count']}",
        f"- Project components handled by repository policy: {summary['project_component_count']}",
        f"- Third-party components: {summary['third_party_component_count']}",
        f"- Third-party local license declarations observed: {summary['third_party_license_declaration_observed_count']}",
        f"- Third-party `NOASSERTION`: {summary['third_party_noassertion_count']}",
        f"- Conflicting local metadata coordinates: {summary['third_party_conflicting_metadata_count']}",
        "",
        "## Exact coordinate review index",
        "",
        "| Ecosystem | Package | Version | Observed declaration/status |",
        "| --- | --- | --- | --- |",
    ]
    for package in packages:
        if not isinstance(package, Mapping):
            raise LocalSupplyArtifactError("license package entry is invalid")
        observed_declaration = package.get("license_expression")
        display_value = (
            observed_declaration
            if isinstance(observed_declaration, str) and observed_declaration
            else package.get("license_status")
        )
        if not isinstance(display_value, str):
            raise LocalSupplyArtifactError("license package display value is invalid")
        escaped_display_value = display_value.replace("|", "\\|")
        lines.append(
            "| "
            f"{package['ecosystem']} | `{package['name']}` | `{package['version']}` | "
            f"{escaped_display_value} |"
        )
    lines.extend(
        [
            "",
            "## Release blocker",
            "",
            "A final notice requires verified license expressions, required attribution and",
            "license texts for every shipped third-party component. Until that evidence is",
            "complete and byte-bound to a current Phase 13 release artifact, live readiness",
            "remains `NO_GO`.",
            "",
        ]
    )
    return "\n".join(lines)


def build_third_party_notice_review(
    repository_root: Path = REPOSITORY_ROOT,
) -> str:
    """
    함수 이름: build_third_party_notice_review()
    기능: Exact dependency scope와 미확인 license를 표시한 release-blocking notice 검토본을 만든다.
    인자: repository_root -> repository root
    반환값: Deterministic Markdown notice review text
    작성 날짜: 2026/08/29
    """
    # 같은 exact-lockfile inventory를 notice renderer에 전달해 scope drift를 막는다.
    inventory = build_dependency_license_inventory(repository_root)
    return build_third_party_notice_review_from_inventory(inventory)


def _require_stable_status(
    expected_status: os.stat_result,
    observed_statuses: Sequence[os.stat_result],
    *,
    require_directory: bool,
) -> None:
    """
    함수 이름: _require_stable_status()
    기능: App entry의 type·identity·size·mtime·ctime이 관찰 내내 고정됐는지 검증한다.
    인자: expected_status -> 최초 no-follow 상태,
        observed_statuses -> fd/path 후속 상태 sequence,
        require_directory -> directory이면 True, regular file이면 False
    반환값: 없음
    작성 날짜: 2026/08/31
    """
    required_mode = stat.S_ISDIR if require_directory else stat.S_ISREG
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")

    # 모든 후속 관찰이 같은 type과 stable metadata를 유지해야 traversal 결과를 신뢰한다.
    for observed_status in observed_statuses:
        if not required_mode(observed_status.st_mode) or any(
            getattr(expected_status, field_name)
            != getattr(observed_status, field_name)
            for field_name in stable_fields
        ):
            raise LocalSupplyArtifactError("historical release app changed")


def _stream_app_file_digest(
    parent_descriptor: int,
    file_name: str,
    expected_status: os.stat_result,
    remaining_total_bytes: int,
) -> tuple[bytes, int, os.stat_result]:
    """
    함수 이름: _stream_app_file_digest()
    기능: App regular file을 nonblocking bounded stream으로 hash하고 identity를 재검증한다.
    인자: parent_descriptor -> file parent directory fd, file_name -> leaf 이름,
        expected_status -> directory listing 시점 상태,
        remaining_total_bytes -> app 전체 상한에서 남은 byte
    반환값: SHA-256 raw digest, file byte 길이, 검증된 최초 fd 상태
    작성 날짜: 2026/08/31
    """
    maximum_file_bytes = min(MAXIMUM_APP_FILE_BYTES, remaining_total_bytes)
    file_descriptor = -1

    # O_NONBLOCK과 no-follow open으로 FIFO·device·symlink를 기다리거나 따라가지 않는다.
    try:
        path_status_before = os.stat(
            file_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        file_descriptor = os.open(
            file_name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent_descriptor,
        )
        initial_status = os.fstat(file_descriptor)
        _require_stable_status(
            expected_status,
            (path_status_before, initial_status),
            require_directory=False,
        )
        if initial_status.st_size > maximum_file_bytes:
            raise LocalSupplyArtifactError("historical release app is too large")

        # max+1 byte까지만 streaming hash해 sparse/growing file도 bounded memory로 거부한다.
        file_digest = hashlib.sha256()
        observed_length = 0
        remaining_read_bytes = maximum_file_bytes + 1
        while remaining_read_bytes > 0:
            file_chunk = os.read(
                file_descriptor,
                min(64 * 1024, remaining_read_bytes),
            )
            if not file_chunk:
                break
            file_digest.update(file_chunk)
            observed_length += len(file_chunk)
            remaining_read_bytes -= len(file_chunk)
        final_status = os.fstat(file_descriptor)
        path_status_after = os.stat(
            file_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as error:
        raise LocalSupplyArtifactError("historical release app read failed") from error
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)

    # Stream 길이와 fd/path metadata를 함께 결속해 same-size replacement도 거부한다.
    _require_stable_status(
        initial_status,
        (expected_status, path_status_before, final_status, path_status_after),
        require_directory=False,
    )
    if observed_length != initial_status.st_size or observed_length > maximum_file_bytes:
        raise LocalSupplyArtifactError("historical release app changed")
    return file_digest.digest(), observed_length, initial_status


def _update_app_tree_entry(
    tree_digest: _DigestAccumulator,
    relative_parts: Sequence[str],
    entry_kind: str,
    entry_status: os.stat_result,
) -> None:
    """
    함수 이름: _update_app_tree_entry()
    기능: 기존 v1 app tree의 path·type·execute-bit framing을 그대로 hash한다.
    인자: tree_digest -> 누적 SHA-256, relative_parts -> app 기준 path component,
        entry_kind -> D 또는 F, entry_status -> 검증된 entry 상태
    반환값: 없음
    작성 날짜: 2026/08/31
    """
    relative_bytes = Path(*relative_parts).as_posix().encode("utf-8")
    executable_bits = stat.S_IMODE(entry_status.st_mode) & 0o111

    # Host inode·timestamp은 digest에서 제외해 복사 후에도 v1 content identity를 유지한다.
    tree_digest.update(entry_kind.encode("ascii"))
    tree_digest.update(len(relative_bytes).to_bytes(4, "big"))
    tree_digest.update(relative_bytes)
    tree_digest.update(executable_bits.to_bytes(2, "big"))


def _hash_app_directory(
    directory_descriptor: int,
    relative_parts: tuple[str, ...],
    tree_digest: _DigestAccumulator,
    counters: _AppTreeCounters,
) -> None:
    """
    함수 이름: _hash_app_directory()
    기능: 열린 app directory를 정렬된 fd-relative DFS로 순회해 v1 tree hash를 누적한다.
    인자: directory_descriptor -> 현재 directory fd,
        relative_parts -> app root 기준 현재 path,
        tree_digest -> 누적 SHA-256, counters -> bounded traversal 집계
    반환값: 없음
    작성 날짜: 2026/08/31
    """
    initial_directory_status = os.fstat(directory_descriptor)
    if not stat.S_ISDIR(initial_directory_status.st_mode):
        raise LocalSupplyArtifactError("historical release app entry is invalid")

    # 한 directory의 이름과 no-follow 상태를 먼저 고정해 기존 os.walk 정렬 순서를 재현한다.
    try:
        entry_names: list[str] = []
        with os.scandir(directory_descriptor) as directory_iterator:
            for directory_entry in directory_iterator:
                if (
                    counters.entry_count + len(entry_names) + 1
                    > MAXIMUM_APP_TREE_ENTRIES
                ):
                    raise LocalSupplyArtifactError(
                        "historical release app is too large"
                    )
                entry_names.append(directory_entry.name)
        entry_names.sort(key=os.fsencode)  # 상한 안의 이름만 모아 v1 byte order로 정렬한다.
        directory_entries: list[tuple[str, os.stat_result]] = []
        file_entries: list[tuple[str, os.stat_result]] = []
        for entry_name in entry_names:
            entry_status = os.stat(
                entry_name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(entry_status.st_mode):
                raise LocalSupplyArtifactError(
                    "historical release app contains symlink"
                )
            if stat.S_ISDIR(entry_status.st_mode):
                directory_entries.append((entry_name, entry_status))
            elif stat.S_ISREG(entry_status.st_mode):
                file_entries.append((entry_name, entry_status))
            else:
                raise LocalSupplyArtifactError(
                    "historical release app entry is invalid"
                )

        # 현재 directory의 D entries를 먼저 기록해 기존 top-down os.walk digest 순서를 유지한다.
        for entry_name, entry_status in directory_entries:
            counters.entry_count += 1
            if counters.entry_count > MAXIMUM_APP_TREE_ENTRIES:
                raise LocalSupplyArtifactError("historical release app is too large")
            _update_app_tree_entry(
                tree_digest,
                (*relative_parts, entry_name),
                "D",
                entry_status,
            )

        # F entries는 per-file·total cap 안에서 streaming hash한 결과만 v1 digest에 넣는다.
        for entry_name, entry_status in file_entries:
            counters.entry_count += 1
            if counters.entry_count > MAXIMUM_APP_TREE_ENTRIES:
                raise LocalSupplyArtifactError("historical release app is too large")
            remaining_total_bytes = (
                MAXIMUM_APP_TOTAL_BYTES - counters.total_file_bytes
            )
            file_digest, file_length, verified_status = _stream_app_file_digest(
                directory_descriptor,
                entry_name,
                entry_status,
                remaining_total_bytes,
            )
            _update_app_tree_entry(
                tree_digest,
                (*relative_parts, entry_name),
                "F",
                verified_status,
            )
            tree_digest.update(file_length.to_bytes(8, "big"))
            tree_digest.update(file_digest)
            counters.file_count += 1
            counters.total_file_bytes += file_length

        # D entries를 정렬 순서대로 열어 재귀하고 parent entry identity를 반환 직전 재검증한다.
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        for entry_name, entry_status in directory_entries:
            child_descriptor = os.open(
                entry_name,
                directory_flags,
                dir_fd=directory_descriptor,
            )
            try:
                child_initial_status = os.fstat(child_descriptor)
                _require_stable_status(
                    entry_status,
                    (child_initial_status,),
                    require_directory=True,
                )
                _hash_app_directory(
                    child_descriptor,
                    (*relative_parts, entry_name),
                    tree_digest,
                    counters,
                )
                child_final_status = os.fstat(child_descriptor)
                child_path_status = os.stat(
                    entry_name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                _require_stable_status(
                    child_initial_status,
                    (child_final_status, child_path_status),
                    require_directory=True,
                )
            finally:
                os.close(child_descriptor)
        final_directory_status = os.fstat(directory_descriptor)
    except OSError as error:
        raise LocalSupplyArtifactError("historical release app traversal failed") from error

    _require_stable_status(
        initial_directory_status,
        (final_directory_status,),
        require_directory=True,
    )  # Entry 추가·삭제나 directory 교체도 전체 tree를 fail closed한다.


def _calculate_app_content_tree(
    repository_root: Path,
    relative_app_path: Path,
) -> tuple[str, int, int]:
    """
    함수 이름: _calculate_app_content_tree()
    기능: Symlink 없는 app을 fd-relative bounded traversal로 deterministic hash한다.
    인자: repository_root -> repository root, relative_app_path -> app bundle 상대 경로
    반환값: content tree SHA-256, regular file count, total file bytes
    작성 날짜: 2026/08/29
    """
    path_parts = _require_relative_path_parts(relative_app_path)
    directory_descriptors: list[int] = []

    # App root까지 no-follow directory chain을 유지해 bundle parent 교체도 post-check한다.
    try:
        directory_descriptors, directory_links = _open_directory_chain(
            repository_root,
            path_parts,
        )
        app_descriptor = directory_descriptors[-1]
        tree_digest = hashlib.sha256()
        counters = _AppTreeCounters()
        _hash_app_directory(
            app_descriptor,
            (),
            tree_digest,
            counters,
        )
        _require_directory_chain_stable(directory_links)
    except OSError as error:
        raise LocalSupplyArtifactError("historical release app is unavailable") from error
    finally:
        for directory_descriptor in reversed(directory_descriptors):
            os.close(directory_descriptor)
    return (
        tree_digest.hexdigest(),
        counters.file_count,
        counters.total_file_bytes,
    )  # 기존 binding schema와 v1 digest 결과 shape를 보존한다.


def build_historical_release_artifact_binding(
    repository_root: Path = REPOSITORY_ROOT,
) -> dict[str, object]:
    """
    함수 이름: build_historical_release_artifact_binding()
    기능: 기존 Phase 12 local-fixed app/DMG의 digest를 Phase 13 current 산출물과 구분해 기록한다.
    인자: repository_root -> repository root
    반환값: Historical-only release artifact binding object
    작성 날짜: 2026/08/29
    """
    app_tree_sha256, app_file_count, app_total_bytes = _calculate_app_content_tree(
        repository_root,
        HISTORICAL_APP_PATH,
    )
    dmg_bytes = _read_regular_bytes(repository_root, HISTORICAL_DMG_PATH)
    dmg_sha256 = _sha256(dmg_bytes)
    if dmg_sha256 != EXPECTED_HISTORICAL_DMG_SHA256:
        raise LocalSupplyArtifactError("historical Phase 12 DMG digest drifted")

    # 현재 dirty Phase 13 source와 build provenance가 없으므로 historical digest를 current candidate로 승격하지 않는다.
    return {
        "schema_version": 1,
        "record_type": "phase13_historical_release_artifact_binding",
        "observed_date": OBSERVED_DATE,
        "classification": "HISTORICAL_PHASE12_LOCAL_FIXED",
        "current_phase13_release_candidate": False,
        "current_phase13_source_provenance_bound": False,
        "exact_lockfile_build_provenance_bound": False,
        "artifact_retention": "LOCAL_IGNORED_NOT_RETAINED_IN_REPOSITORY",
        "app": {
            "path": HISTORICAL_APP_PATH.as_posix(),
            "digest_algorithm": "binance-auto-content-tree-sha256-v1",
            "sha256": app_tree_sha256,
            "regular_file_count": app_file_count,
            "total_regular_file_bytes": app_total_bytes,
        },
        "dmg": {
            "path": HISTORICAL_DMG_PATH.as_posix(),
            "sha256": dmg_sha256,
            "size": len(dmg_bytes),
        },
        "gap_reason": (
            "This retained pair is the historical Phase 12 local-fixed artifact, not a "
            "current Phase 13 build bound to the present source and lockfiles."
        ),
    }


def _canonical_json_bytes(json_object: Mapping[str, object]) -> bytes:
    """
    함수 이름: _canonical_json_bytes()
    기능: Evidence JSON을 UTF-8, 2-space indent, trailing newline로 canonicalize한다.
    인자: json_object -> serialize할 mapping
    반환값: canonical JSON bytes
    작성 날짜: 2026/08/29
    """
    # Key 정렬과 trailing newline을 고정해 재생성 가능한 artifact bytes를 만든다.
    return (
        json.dumps(json_object, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")


def build_local_supply_artifact_bytes(
    repository_root: Path = REPOSITORY_ROOT,
) -> dict[Path, bytes]:
    """
    함수 이름: build_local_supply_artifact_bytes()
    기능: 네 개 local supply artifact의 canonical bytes를 한 번에 생성한다.
    인자: repository_root -> repository root
    반환값: Repository 상대 경로와 산출물 bytes mapping
    작성 날짜: 2026/08/29
    """
    # SBOM·license·notice는 lockfile에서 재생성하고 release binding은 local historical bytes에서 만든다.
    return {
        SBOM_PATH: _canonical_json_bytes(build_exact_lockfile_sbom(repository_root)),
        LICENSE_INVENTORY_PATH: _canonical_json_bytes(
            build_dependency_license_inventory(repository_root)
        ),
        NOTICE_REVIEW_PATH: build_third_party_notice_review(repository_root).encode(
            "utf-8"
        ),
        HISTORICAL_RELEASE_BINDING_PATH: _canonical_json_bytes(
            build_historical_release_artifact_binding(repository_root)
        ),
    }


def write_local_supply_artifacts(
    repository_root: Path = REPOSITORY_ROOT,
) -> dict[str, str]:
    """
    함수 이름: write_local_supply_artifacts()
    기능: Canonical local supply artifact bytes를 fixed repository 경로에 기록한다.
    인자: repository_root -> repository root
    반환값: Artifact 경로별 SHA-256 mapping
    작성 날짜: 2026/08/29
    """
    artifact_bytes = build_local_supply_artifact_bytes(repository_root)
    artifact_hashes: dict[str, str] = {}

    # Fixed 경로 밖의 caller-selected output을 허용하지 않고 canonical bytes만 기록한다.
    for relative_path, generated_bytes in artifact_bytes.items():
        output_path = repository_root.resolve() / relative_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(generated_bytes)
        artifact_hashes[relative_path.as_posix()] = _sha256(generated_bytes)
    return artifact_hashes


def _validate_historical_binding_shape(binding: Mapping[str, object]) -> None:
    """
    함수 이름: _validate_historical_binding_shape()
    기능: Historical release record가 current Phase 13 candidate를 주장하지 않는지 검증한다.
    인자: binding -> release binding JSON object
    반환값: 없음
    작성 날짜: 2026/08/29
    """
    # Historical record에 current release 의미를 추가하는 schema 드리프트를 거부한다.
    app = binding.get("app")
    dmg = binding.get("dmg")
    if (
        binding.get("record_type")
        != "phase13_historical_release_artifact_binding"
        or binding.get("classification") != "HISTORICAL_PHASE12_LOCAL_FIXED"
        or binding.get("current_phase13_release_candidate") is not False
        or binding.get("current_phase13_source_provenance_bound") is not False
        or binding.get("exact_lockfile_build_provenance_bound") is not False
        or not isinstance(app, Mapping)
        or not isinstance(dmg, Mapping)
        or app.get("path") != HISTORICAL_APP_PATH.as_posix()
        or dmg.get("path") != HISTORICAL_DMG_PATH.as_posix()
        or dmg.get("sha256") != EXPECTED_HISTORICAL_DMG_SHA256
        or not isinstance(app.get("sha256"), str)
        or SHA256_PATTERN.fullmatch(str(app.get("sha256"))) is None
    ):
        raise LocalSupplyArtifactError("historical release binding is invalid")


def _validate_dependency_license_inventory(
    repository_root: Path,
    inventory: Mapping[str, object],
) -> Mapping[str, object]:
    """
    함수 이름: _validate_dependency_license_inventory()
    기능: Retained local metadata 선언이 exact lock coordinate와 정직한 요약에 결합됐는지 검증한다.
    인자: repository_root -> repository root, inventory -> parsed license inventory
    반환값: 검증된 summary mapping
    작성 날짜: 2026/08/29
    """
    expected_root_keys = {
        "schema_version",
        "record_type",
        "observed_date",
        "scope",
        "inputs",
        "summary",
        "packages",
        "limitations",
    }
    if set(inventory) != expected_root_keys:
        raise LocalSupplyArtifactError("license inventory schema is invalid")
    if (
        inventory.get("schema_version") != 1
        or inventory.get("record_type")
        != "phase13_dependency_license_inventory"
        or inventory.get("observed_date") != OBSERVED_DATE
        or inventory.get("scope") != "exact_lockfile_coordinates"
    ):
        raise LocalSupplyArtifactError("license inventory metadata is invalid")

    components = read_exact_lockfile_components(repository_root)
    expected_inputs = _build_lockfile_inputs(repository_root, components)
    packages = inventory.get("packages")
    summary = inventory.get("summary")
    if (
        inventory.get("inputs") != expected_inputs
        or not isinstance(packages, list)
        or len(packages) != len(components)
        or not isinstance(summary, Mapping)
    ):
        raise LocalSupplyArtifactError("license inventory scope is invalid")

    declared_count = 0
    noassertion_count = 0
    conflicting_count = 0
    matched_source_identities: set[tuple[str, str, str]] = set()
    expected_package_keys = {
        "bom_ref",
        "coordinate",
        "ecosystem",
        "name",
        "version",
        "scope",
        "license_expression",
        "license_status",
        "evidence_sources",
        "conflicting_license_expressions",
    }

    # Inventory 순서와 identity를 SBOM과 같은 sorted component tuple에 1:1 대조한다.
    for component, package in zip(components, packages, strict=True):
        if not isinstance(package, Mapping) or set(package) != expected_package_keys:
            raise LocalSupplyArtifactError("license package schema is invalid")
        if (
            package.get("bom_ref") != component.bom_ref
            or package.get("coordinate") != component.coordinate
            or package.get("ecosystem") != component.ecosystem
            or package.get("name") != component.name
            or package.get("version") != component.version
        ):
            raise LocalSupplyArtifactError("license package identity is invalid")
        evidence_sources = package.get("evidence_sources")
        conflicting_expressions = package.get("conflicting_license_expressions")
        license_expression = package.get("license_expression")
        license_status = package.get("license_status")
        if not isinstance(evidence_sources, list) or not isinstance(
            conflicting_expressions,
            list,
        ):
            raise LocalSupplyArtifactError("license package evidence is invalid")

        source_keys: set[tuple[str, str, str, str]] = set()
        for evidence_source in evidence_sources:
            if not isinstance(evidence_source, Mapping) or set(evidence_source) != {
                "kind",
                "source_identity",
                "sha256",
                "metadata_field",
            }:
                raise LocalSupplyArtifactError("license evidence source is invalid")
            source_kind = evidence_source.get("kind")
            source_identity = evidence_source.get("source_identity")
            source_sha256 = evidence_source.get("sha256")
            metadata_field = evidence_source.get("metadata_field")
            if (
                source_kind not in ALLOWED_LICENSE_SOURCE_KINDS
                or not isinstance(source_identity, str)
                or not source_identity
                or "\n" in source_identity
                or "\r" in source_identity
                or Path(source_identity).is_absolute()
                or ".." in Path(source_identity).parts
                or not isinstance(source_sha256, str)
                or SHA256_PATTERN.fullmatch(source_sha256) is None
                or not isinstance(metadata_field, str)
                or not metadata_field
                or len(metadata_field) > 100
            ):
                raise LocalSupplyArtifactError("license evidence source is invalid")
            source_key = (
                str(source_kind),
                source_identity,
                source_sha256,
                metadata_field,
            )
            source_keys.add(source_key)
            matched_source_identities.add(source_key[:3])
        if len(source_keys) != len(evidence_sources):
            raise LocalSupplyArtifactError("license evidence source is duplicated")

        if component.project_component:
            if (
                package.get("scope") != "project_component"
                or license_status != "PROJECT_POLICY_SEPARATE"
                or license_expression is not None
                or evidence_sources
                or conflicting_expressions
            ):
                raise LocalSupplyArtifactError("project license boundary is invalid")
            continue
        if package.get("scope") != "third_party":
            raise LocalSupplyArtifactError("third-party license scope is invalid")
        if license_status == "DECLARED_LOCAL_METADATA":
            if (
                not isinstance(license_expression, str)
                or not license_expression
                or len(license_expression) > 512
                or "\n" in license_expression
                or "\r" in license_expression
                or not evidence_sources
                or conflicting_expressions
            ):
                raise LocalSupplyArtifactError("declared license evidence is invalid")
            declared_count += 1
        elif license_status == "NOASSERTION_NO_LOCAL_METADATA":
            if license_expression is not None or evidence_sources or conflicting_expressions:
                raise LocalSupplyArtifactError("NOASSERTION evidence is invalid")
            noassertion_count += 1
        elif license_status == "NOASSERTION_CONFLICTING_LOCAL_METADATA":
            if (
                license_expression is not None
                or len(evidence_sources) < 2
                or len(conflicting_expressions) < 2
                or conflicting_expressions != sorted(set(conflicting_expressions))
                or any(
                    not isinstance(expression, str)
                    or not expression
                    or len(expression) > 512
                    for expression in conflicting_expressions
                )
            ):
                raise LocalSupplyArtifactError("conflicting license evidence is invalid")
            noassertion_count += 1
            conflicting_count += 1
        else:
            raise LocalSupplyArtifactError("license package status is invalid")

    expected_summary = {
        "package_count": len(components),
        "project_component_count": sum(
            component.project_component for component in components
        ),
        "third_party_component_count": sum(
            not component.project_component for component in components
        ),
        "third_party_license_declaration_observed_count": declared_count,
        "third_party_noassertion_count": noassertion_count,
        "third_party_conflicting_metadata_count": conflicting_count,
        "matched_local_metadata_source_count": len(matched_source_identities),
        "coordinate_inventory_complete": True,
        "license_metadata_complete": False,
        "license_texts_complete": False,
        "manual_review_required": True,
    }
    expected_limitations = [
        "The exact lockfiles do not contain complete third-party license metadata.",
        "Observed local metadata declarations are not legal approval or a complete license-text review.",
        "NOASSERTION is an explicit unresolved or conflicting state and is not a license approval.",
        "Historical OSV license summaries are not promoted to current raw evidence.",
    ]
    if summary != expected_summary or inventory.get("limitations") != expected_limitations:
        raise LocalSupplyArtifactError("license inventory summary is invalid")
    return summary


def validate_local_supply_artifacts(
    repository_root: Path = REPOSITORY_ROOT,
) -> LocalSupplyArtifactSummary:
    """
    함수 이름: validate_local_supply_artifacts()
    기능: Checked-in local artifacts를 current lockfiles에서 exact 재생성해 검증한다.
    인자: repository_root -> repository root
    반환값: 검증된 artifact 집계
    작성 날짜: 2026/08/29
    """
    expected_sbom_bytes = _canonical_json_bytes(build_exact_lockfile_sbom(repository_root))
    observed_sbom_bytes = _read_regular_bytes(
        repository_root,
        SBOM_PATH,
        MAXIMUM_JSON_BYTES,
    )
    if observed_sbom_bytes != expected_sbom_bytes:
        raise LocalSupplyArtifactError("local supply artifact drifted from lockfiles")

    # License 산출물은 exact coordinate를 재생성하되 ignored local metadata 관찰은 retained hash로 검증한다.
    license_inventory = _load_json_object(repository_root, LICENSE_INVENTORY_PATH)
    summary = _validate_dependency_license_inventory(
        repository_root,
        license_inventory,
    )
    observed_license_bytes = _read_regular_bytes(
        repository_root,
        LICENSE_INVENTORY_PATH,
        MAXIMUM_JSON_BYTES,
    )
    if observed_license_bytes != _canonical_json_bytes(license_inventory):
        raise LocalSupplyArtifactError("license inventory JSON is not canonical")
    expected_notice_bytes = build_third_party_notice_review_from_inventory(
        license_inventory
    ).encode("utf-8")
    observed_notice_bytes = _read_regular_bytes(
        repository_root,
        NOTICE_REVIEW_PATH,
        MAXIMUM_JSON_BYTES,
    )
    if observed_notice_bytes != expected_notice_bytes:
        raise LocalSupplyArtifactError("local supply artifact drifted from lockfiles")

    # 현재 workspace에 venv·node_modules가 모두 있으면 retained metadata hash까지 재생성한다.
    if (
        (repository_root.resolve() / "backend" / ".venv").is_dir()
        and (repository_root.resolve() / "UI" / "node_modules").is_dir()
    ):
        current_license_inventory = build_dependency_license_inventory(repository_root)
        if license_inventory != current_license_inventory:
            raise LocalSupplyArtifactError("local license metadata drifted")

    # Historical binding JSON은 main evidence에 보존된 digest record로 검증하되 binary가 있으면 현재 bytes도 재확인한다.
    historical_binding = _load_json_object(
        repository_root,
        HISTORICAL_RELEASE_BINDING_PATH,
    )
    _validate_historical_binding_shape(historical_binding)
    absolute_app_path = repository_root.resolve() / HISTORICAL_APP_PATH
    absolute_dmg_path = repository_root.resolve() / HISTORICAL_DMG_PATH
    if absolute_app_path.exists() or absolute_dmg_path.exists():
        if not absolute_app_path.exists() or not absolute_dmg_path.exists():
            raise LocalSupplyArtifactError("historical release pair is incomplete")
        current_binding = build_historical_release_artifact_binding(repository_root)
        if historical_binding != current_binding:
            raise LocalSupplyArtifactError("historical release bytes drifted from binding")

    return LocalSupplyArtifactSummary(
        component_count=int(summary["package_count"]),
        third_party_component_count=int(summary["third_party_component_count"]),
        license_declaration_count=int(
            summary["third_party_license_declaration_observed_count"]
        ),
        noassertion_count=int(summary["third_party_noassertion_count"]),
        notice_complete=False,
        current_phase13_release_candidate_bound=False,
    )


def parse_arguments(argument_values: Sequence[str] | None = None) -> argparse.Namespace:
    """
    함수 이름: parse_arguments()
    기능: Fixed artifact write 또는 read-only check 작업만 허용한다.
    인자: argument_values -> 명시 argument 또는 실제 argv를 뜻하는 None
    반환값: argparse namespace
    작성 날짜: 2026/08/29
    """
    # CLI는 산출물 생성과 검증 두 명령만 허용한다.
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("write", "check"))
    return parser.parse_args(argument_values)


def main(argument_values: Sequence[str] | None = None) -> int:
    """
    함수 이름: main()
    기능: Network 없이 fixed local supply artifact를 기록하거나 검증한다.
    인자: argument_values -> 명시 argument 또는 실제 argv를 뜻하는 None
    반환값: 성공 0, local input/artifact 오류 1
    작성 날짜: 2026/08/29
    """
    arguments = parse_arguments(argument_values)
    try:
        if arguments.operation == "write":
            artifact_hashes = write_local_supply_artifacts()
            print(
                "phase13 local supply artifacts written: "
                f"count={len(artifact_hashes)}"
            )
        else:
            summary = validate_local_supply_artifacts()
            print(
                "phase13 local supply artifacts validated: "
                f"components={summary.component_count} "
                f"third_party={summary.third_party_component_count} "
                f"license_declarations={summary.license_declaration_count} "
                f"noassertion={summary.noassertion_count} "
                "status=NO_GO"
            )
    except (LocalSupplyArtifactError, OSError, TypeError, ValueError):
        # Host path와 raw package metadata를 반사하지 않고 고정 local 오류만 출력한다.
        print("phase13-local-supply: ERROR: local artifact unavailable.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
