#!/usr/bin/env python3
"""Phase 13 진입 전에 non-secret Phase 12 release evidence manifest를 검증한다."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import plistlib
import re
import stat
import sys
from typing import Any, Callable, Mapping, Sequence

if __package__:
    from scripts.check_phase12_release_identity import (
        ReleaseIdentityEvidence,
        create_sanitized_environment,
        read_successful_stdout,
        run_command,
        validate_notary_profile_name,
        verify_release_identity_environment,
    )
    from scripts.verify_phase12_signed_artifacts import (
        SignedArtifactEvidence,
        verify_signed_artifacts,
    )
else:  # pragma: no cover - direct CLI import path
    from check_phase12_release_identity import (
        ReleaseIdentityEvidence,
        create_sanitized_environment,
        read_successful_stdout,
        run_command,
        validate_notary_profile_name,
        verify_release_identity_environment,
    )
    from verify_phase12_signed_artifacts import (
        SignedArtifactEvidence,
        verify_signed_artifacts,
    )


SCHEMA_VERSION = 2
MAXIMUM_MANIFEST_BYTES = 1024 * 1024
MINIMUM_SECRET_CANARIES = 2
MINIMUM_MACOS_VERSION = "11.0"
REQUIRED_REGRESSION_SUITES = ("backend", "ui", "rust", "scripts")
MINIMUM_REGRESSION_TOTALS = {
    "backend": 643,
    "ui": 280,
    "rust": 31,
    "scripts": 114,
}
REQUIRED_CHECKS = (
    "typescript",
    "vite_build",
    "cargo_fmt",
    "cargo_check",
    "cargo_clippy",
    "shell_syntax",
    "git_diff_check",
    "mounted_dmg_verification",
)
SECRET_SCAN_COVERAGE_TARGETS = (
    "repository",
    "signed_app",
    "dmg",
    "mounted_dmg",
    "application_support",
    "diagnostic_reports",
)

GENERIC_PASS_MESSAGE = "phase12-release-gate: PASS."
GENERIC_ERROR_MESSAGE = "phase12-release-gate: ERROR."

COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
DEVELOPER_IDENTITY_PATTERN = re.compile(
    r'Developer ID Application: [^"\r\n]+ \(([A-Z0-9]{10})\)\Z'
)
UUID_PATTERN = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\Z"
)
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
TEAM_ID_PATTERN = re.compile(r"[A-Z0-9]{10}\Z")
SEMANTIC_VERSION_PATTERN = re.compile(
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
BUNDLE_VERSION_PATTERN = re.compile(
    r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){0,2}\Z"
)
NATIVE_ARTIFACT_KINDS = {"libpython", "native-extension"}
MAXIMUM_INFO_PLIST_BYTES = 1024 * 1024
EXPECTED_BUNDLE_IDENTIFIER = "com.binance-auto.trader"
FILE_READ_CHUNK_BYTES = 1024 * 1024
MAXIMUM_APP_TREE_ENTRIES = 10_000
MAXIMUM_APP_RELATIVE_PATH_BYTES = 4096


class EvidenceLoadError(RuntimeError):
    """
    클래스 이름: EvidenceLoadError
    기능: evidence manifest를 안전하게 읽거나 JSON으로 해석할 수 없음을 나타낸다.
    작성 날짜: 2026/08/24
    """


class EvidenceValidationError(RuntimeError):
    """
    클래스 이름: EvidenceValidationError
    기능: 읽은 evidence가 Phase 12 release gate 계약을 충족하지 않음을 나타낸다.
    작성 날짜: 2026/08/24
    """


def _load_unique_json_object(
    object_pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    """
    함수 이름: _load_unique_json_object()
    기능: JSON object의 key 중복을 덮어쓰지 않고 load 단계에서 거부한다.
    인자: object_pairs -> JSON decoder가 순서대로 전달한 key-value pair
    반환값: 중복 key가 없는 JSON object
    작성 날짜: 2026/08/24
    """
    loaded_object: dict[str, Any] = {}
    for object_key, object_value in object_pairs:
        if object_key in loaded_object:
            raise EvidenceLoadError("Evidence JSON object key is duplicated.")
        loaded_object[object_key] = object_value
    return loaded_object


def _reject_nonstandard_json_constant(constant_name: str) -> None:
    """
    함수 이름: _reject_nonstandard_json_constant()
    기능: NaN과 Infinity 같은 JSON 표준 밖 숫자 token을 fail-closed로 거부한다.
    인자: constant_name -> JSON decoder가 발견한 비표준 constant 이름
    반환값: 없음
    작성 날짜: 2026/08/24
    """
    del constant_name  # Raw manifest token을 exception이나 CLI output에 반사하지 않는다.
    raise EvidenceLoadError("Evidence JSON contains a non-standard constant.")


def _stable_file_snapshot(file_status: os.stat_result) -> tuple[int, ...]:
    """
    함수 이름: _stable_file_snapshot()
    기능: read atime을 제외한 file identity와 mutation-sensitive metadata를 고정한다.
    인자: file_status -> lstat 또는 fstat 결과
    반환값: dev, inode, size, mtime와 ctime tuple
    작성 날짜: 2026/08/24
    """
    return (
        file_status.st_dev,
        file_status.st_ino,
        file_status.st_size,
        file_status.st_mtime_ns,
        file_status.st_ctime_ns,
    )


def _inspect_physical_absolute_path(file_path: Path) -> os.stat_result:
    """
    함수 이름: _inspect_physical_absolute_path()
    기능: absolute path의 모든 component가 실제 directory/file이고 symlink가 아님을 검증한다.
    인자: file_path -> existing manifest 또는 release artifact path
    반환값: symlink를 따라가지 않고 읽은 final component lstat
    작성 날짜: 2026/08/24
    """
    if (
        not isinstance(file_path, Path)
        or not file_path.is_absolute()
        or file_path == Path("/")
        or ".." in file_path.parts
    ):
        raise ValueError("path is not a physical absolute path")

    resolved_path = file_path.resolve(strict=True)
    if resolved_path != file_path:
        raise ValueError("path is not canonical")

    current_path = Path(file_path.anchor)
    current_status = os.lstat(current_path)
    if stat.S_ISLNK(current_status.st_mode):
        raise ValueError("path contains a symlink")

    remaining_components = file_path.parts[1:]
    for component_index, path_component in enumerate(remaining_components):
        current_path = current_path / path_component
        current_status = os.lstat(current_path)
        if stat.S_ISLNK(current_status.st_mode):
            raise ValueError("path contains a symlink")
        if component_index < len(remaining_components) - 1 and not stat.S_ISDIR(
            current_status.st_mode
        ):
            raise ValueError("path ancestor is not a directory")
    return current_status


def load_evidence_manifest(manifest_path: Path) -> Any:
    """
    함수 이름: load_evidence_manifest()
    기능: 크기가 제한된 regular UTF-8 JSON evidence file을 중복 key 없이 읽는다.
    인자: manifest_path -> 읽을 non-secret release evidence JSON file
    반환값: JSON decoder가 만든 검증 전 root value
    작성 날짜: 2026/08/24
    """
    manifest_descriptor = -1
    try:
        initial_path_status = _inspect_physical_absolute_path(manifest_path)
        if not stat.S_ISREG(initial_path_status.st_mode):
            raise EvidenceLoadError("Evidence manifest is not a regular file.")

        # O_NOFOLLOW로 path 검사와 open 사이 symlink 교체 없이 exact file descriptor를 고정한다.
        open_flags = os.O_RDONLY
        open_flags |= getattr(os, "O_CLOEXEC", 0)
        open_flags |= getattr(os, "O_NONBLOCK", 0)
        no_follow_flag = getattr(os, "O_NOFOLLOW", 0)
        if no_follow_flag == 0 and manifest_path.is_symlink():
            raise EvidenceLoadError("Evidence manifest symlink is not allowed.")
        open_flags |= no_follow_flag
        manifest_descriptor = os.open(manifest_path, open_flags)

        # 한 FD의 fstat와 bounded read만 사용해 regular-file type과 최종 byte를 함께 검증한다.
        manifest_stat = os.fstat(manifest_descriptor)
        if not stat.S_ISREG(manifest_stat.st_mode):
            raise EvidenceLoadError("Evidence manifest is not a regular file.")
        if _stable_file_snapshot(initial_path_status) != _stable_file_snapshot(
            manifest_stat
        ):
            raise EvidenceLoadError("Evidence manifest changed before reading.")
        if (
            manifest_stat.st_size <= 0
            or manifest_stat.st_size > MAXIMUM_MANIFEST_BYTES
        ):
            raise EvidenceLoadError("Evidence manifest size is invalid.")
        with os.fdopen(manifest_descriptor, "rb", closefd=True) as manifest_file:
            manifest_descriptor = -1
            manifest_bytes = manifest_file.read(MAXIMUM_MANIFEST_BYTES + 1)
            final_descriptor_status = os.fstat(manifest_file.fileno())
        final_path_status = _inspect_physical_absolute_path(manifest_path)
        if (
            not manifest_bytes
            or len(manifest_bytes) > MAXIMUM_MANIFEST_BYTES
            or len(manifest_bytes) != manifest_stat.st_size
        ):
            raise EvidenceLoadError("Evidence manifest size is invalid.")
        if (
            not stat.S_ISREG(final_path_status.st_mode)
            or _stable_file_snapshot(manifest_stat)
            != _stable_file_snapshot(final_descriptor_status)
            or _stable_file_snapshot(manifest_stat)
            != _stable_file_snapshot(final_path_status)
        ):
            raise EvidenceLoadError("Evidence manifest changed while reading.")
        manifest_text = manifest_bytes.decode("utf-8")
        return json.loads(
            manifest_text,
            object_pairs_hook=_load_unique_json_object,
            parse_constant=_reject_nonstandard_json_constant,
        )
    except EvidenceLoadError:
        raise
    except (
        OSError,
        RuntimeError,
        TypeError,
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ):
        raise EvidenceLoadError("Evidence manifest could not be loaded.") from None
    finally:
        if manifest_descriptor >= 0:
            try:
                os.close(manifest_descriptor)
            except OSError:
                pass  # CLI는 close 실패 세부사항도 generic load error 경계 밖에 출력하지 않는다.


def _require_object(value: Any, field_name: str) -> dict[str, Any]:
    """
    함수 이름: _require_object()
    기능: schema field가 JSON object인지 확인한다.
    인자: value -> 검사할 JSON value
        field_name -> value를 식별하는 non-secret schema field 이름
    반환값: 검증된 JSON object
    작성 날짜: 2026/08/24
    """
    if not isinstance(value, dict):
        raise EvidenceValidationError(f"{field_name} must be an object.")
    return value


def _require_exact_keys(
    evidence_object: dict[str, Any],
    expected_keys: set[str],
    field_name: str,
) -> None:
    """
    함수 이름: _require_exact_keys()
    기능: object의 key가 versioned schema의 필수 key 집합과 정확히 같은지 확인한다.
    인자: evidence_object -> key를 검사할 JSON object
        expected_keys -> 허용하고 요구하는 exact key 집합
        field_name -> object를 식별하는 non-secret schema field 이름
    반환값: 없음
    작성 날짜: 2026/08/24
    """
    if set(evidence_object) != expected_keys:
        raise EvidenceValidationError(f"{field_name} keys do not match the schema.")


def _require_non_empty_string(value: Any, field_name: str) -> str:
    """
    함수 이름: _require_non_empty_string()
    기능: schema field가 앞뒤 공백 없는 non-empty string인지 확인한다.
    인자: value -> 검사할 JSON value
        field_name -> value를 식별하는 non-secret schema field 이름
    반환값: 검증된 string
    작성 날짜: 2026/08/24
    """
    if not isinstance(value, str) or not value or value.strip() != value:
        raise EvidenceValidationError(f"{field_name} must be a non-empty string.")
    return value


def _require_boolean(value: Any, expected_value: bool, field_name: str) -> None:
    """
    함수 이름: _require_boolean()
    기능: schema field가 integer 대용값이 아닌 exact JSON boolean과 기대값인지 확인한다.
    인자: value -> 검사할 JSON value
        expected_value -> release gate가 요구하는 boolean 값
        field_name -> value를 식별하는 non-secret schema field 이름
    반환값: 없음
    작성 날짜: 2026/08/24
    """
    if type(value) is not bool or value is not expected_value:
        raise EvidenceValidationError(f"{field_name} has an invalid boolean value.")


def _require_non_negative_integer(value: Any, field_name: str) -> int:
    """
    함수 이름: _require_non_negative_integer()
    기능: schema count가 boolean이나 float가 아닌 0 이상의 integer인지 확인한다.
    인자: value -> 검사할 JSON value
        field_name -> value를 식별하는 non-secret schema field 이름
    반환값: 검증된 integer
    작성 날짜: 2026/08/24
    """
    if type(value) is not int or value < 0:
        raise EvidenceValidationError(f"{field_name} must be a non-negative integer.")
    return value


def _validate_release_candidate(release_candidate_value: Any) -> str:
    """
    함수 이름: _validate_release_candidate()
    기능: 고정 commit, clean worktree, explicit release version과 build host evidence를 검증한다.
    인자: release_candidate_value -> release_candidate JSON value
    반환값: clean Mac host와 비교할 build host identifier
    작성 날짜: 2026/08/24
    """
    release_candidate = _require_object(
        release_candidate_value,
        "release_candidate",
    )
    _require_exact_keys(
        release_candidate,
        {"commit", "dirty", "build_host_id", "version", "build_version"},
        "release_candidate",
    )

    commit_identifier = _require_non_empty_string(
        release_candidate["commit"],
        "release_candidate.commit",
    )
    if COMMIT_PATTERN.fullmatch(commit_identifier) is None:
        raise EvidenceValidationError("release_candidate.commit is invalid.")
    _require_boolean(
        release_candidate["dirty"],
        False,
        "release_candidate.dirty",
    )
    release_version = _require_non_empty_string(
        release_candidate["version"],
        "release_candidate.version",
    )
    if SEMANTIC_VERSION_PATTERN.fullmatch(release_version) is None:
        raise EvidenceValidationError("release_candidate.version is invalid.")
    build_version = _require_non_empty_string(
        release_candidate["build_version"],
        "release_candidate.build_version",
    )
    if (
        BUNDLE_VERSION_PATTERN.fullmatch(build_version) is None
        or not any(component != "0" for component in build_version.split("."))
    ):
        raise EvidenceValidationError("release_candidate.build_version is invalid.")
    return _require_non_empty_string(
        release_candidate["build_host_id"],
        "release_candidate.build_host_id",
    )


def _validate_signed_artifact(
    artifact_value: Any,
    expected_team_id: str,
    field_name: str,
    *,
    native_artifact: bool = False,
) -> None:
    """
    함수 이름: _validate_signed_artifact()
    기능: signed artifact의 Team ID, hardened runtime과 minimum macOS 계약을 검증한다.
    인자: artifact_value -> app, sidecar 또는 extracted native JSON value
        expected_team_id -> 모든 signed artifact가 공유해야 하는 Team ID
        field_name -> artifact를 식별하는 non-secret schema field 이름
    반환값: 없음
    작성 날짜: 2026/08/24
    """
    signed_artifact = _require_object(artifact_value, field_name)
    expected_keys = {"team_id", "hardened_runtime", "min_os"}
    if native_artifact:
        expected_keys.update({"kind", "sha256"})
    _require_exact_keys(signed_artifact, expected_keys, field_name)
    artifact_team_id = _require_non_empty_string(
        signed_artifact["team_id"],
        f"{field_name}.team_id",
    )
    if artifact_team_id != expected_team_id:
        raise EvidenceValidationError(f"{field_name}.team_id does not match.")
    _require_boolean(
        signed_artifact["hardened_runtime"],
        True,
        f"{field_name}.hardened_runtime",
    )
    if signed_artifact["min_os"] != MINIMUM_MACOS_VERSION:
        raise EvidenceValidationError(f"{field_name}.min_os is invalid.")
    if native_artifact:
        artifact_kind = _require_non_empty_string(
            signed_artifact["kind"],
            f"{field_name}.kind",
        )
        if artifact_kind not in NATIVE_ARTIFACT_KINDS:
            raise EvidenceValidationError(f"{field_name}.kind is invalid.")
        artifact_digest = _require_non_empty_string(
            signed_artifact["sha256"],
            f"{field_name}.sha256",
        )
        if (
            SHA256_PATTERN.fullmatch(artifact_digest) is None
            or len(set(artifact_digest)) <= 1
        ):
            raise EvidenceValidationError(f"{field_name}.sha256 is invalid.")


def _validate_signing(signing_value: Any) -> None:
    """
    함수 이름: _validate_signing()
    기능: Developer ID identity와 모든 executable/native signing invariant를 검증한다.
    인자: signing_value -> signing JSON value
    반환값: 없음
    작성 날짜: 2026/08/24
    """
    signing = _require_object(signing_value, "signing")
    _require_exact_keys(
        signing,
        {
            "identity",
            "team_id",
            "library_validation_disabled",
            "provenance_sha256",
            "app",
            "sidecar",
            "extracted_native",
        },
        "signing",
    )

    signing_identity = _require_non_empty_string(
        signing["identity"],
        "signing.identity",
    )
    identity_match = DEVELOPER_IDENTITY_PATTERN.fullmatch(signing_identity)
    if identity_match is None:
        raise EvidenceValidationError("signing.identity is invalid.")
    team_id = _require_non_empty_string(signing["team_id"], "signing.team_id")
    if TEAM_ID_PATTERN.fullmatch(team_id) is None:
        raise EvidenceValidationError("signing.team_id is invalid.")
    if identity_match.group(1) != team_id:
        raise EvidenceValidationError("signing.identity Team ID does not match.")
    _require_boolean(
        signing["library_validation_disabled"],
        False,
        "signing.library_validation_disabled",
    )
    provenance_digest = _require_non_empty_string(
        signing["provenance_sha256"],
        "signing.provenance_sha256",
    )
    if (
        SHA256_PATTERN.fullmatch(provenance_digest) is None
        or len(set(provenance_digest)) <= 1
    ):
        raise EvidenceValidationError("signing.provenance_sha256 is invalid.")

    _validate_signed_artifact(signing["app"], team_id, "signing.app")
    _validate_signed_artifact(signing["sidecar"], team_id, "signing.sidecar")

    extracted_native_values = signing["extracted_native"]
    if not isinstance(extracted_native_values, list) or not extracted_native_values:
        raise EvidenceValidationError(
            "signing.extracted_native must contain libpython evidence."
        )
    native_kinds: set[str] = set()
    libpython_count = 0
    native_digests: set[str] = set()
    for native_index, native_value in enumerate(extracted_native_values, start=1):
        _validate_signed_artifact(
            native_value,
            team_id,
            f"signing.extracted_native[{native_index}]",
            native_artifact=True,
        )
        native_kinds.add(native_value["kind"])
        libpython_count += native_value["kind"] == "libpython"
        native_digests.add(native_value["sha256"])
    if libpython_count != 1 or not native_kinds.issubset(NATIVE_ARTIFACT_KINDS):
        raise EvidenceValidationError(
            "signing.extracted_native libpython evidence is invalid."
        )
    if len(native_digests) != len(extracted_native_values):
        raise EvidenceValidationError(
            "signing.extracted_native artifacts are duplicated."
        )


def _validate_notarized_artifact(
    artifact_value: Any,
    field_name: str,
    require_hdiutil: bool,
) -> str:
    """
    함수 이름: _validate_notarized_artifact()
    기능: notarization status, submission UUID, stapler, Gatekeeper와 DMG verify 결과를 검증한다.
    인자: artifact_value -> app 또는 DMG notarization JSON value
        field_name -> artifact를 식별하는 non-secret schema field 이름
        require_hdiutil -> DMG 전용 hdiutil evidence를 요구할지 여부
    반환값: artifact 사이 중복을 비교할 lowercase submission UUID
    작성 날짜: 2026/08/24
    """
    notarized_artifact = _require_object(artifact_value, field_name)
    expected_keys = {
        "status",
        "submission_id",
        "stapler_valid",
        "gatekeeper_accepted",
    }
    if require_hdiutil:
        expected_keys.add("hdiutil_verified")
    _require_exact_keys(notarized_artifact, expected_keys, field_name)

    if notarized_artifact["status"] != "Accepted":
        raise EvidenceValidationError(f"{field_name}.status is invalid.")
    submission_id = _require_non_empty_string(
        notarized_artifact["submission_id"],
        f"{field_name}.submission_id",
    )
    if UUID_PATTERN.fullmatch(submission_id) is None:
        raise EvidenceValidationError(f"{field_name}.submission_id is invalid.")
    _require_boolean(
        notarized_artifact["stapler_valid"],
        True,
        f"{field_name}.stapler_valid",
    )
    _require_boolean(
        notarized_artifact["gatekeeper_accepted"],
        True,
        f"{field_name}.gatekeeper_accepted",
    )
    if require_hdiutil:
        _require_boolean(
            notarized_artifact["hdiutil_verified"],
            True,
            f"{field_name}.hdiutil_verified",
        )
    return submission_id.lower()


def _validate_notarization(notarization_value: Any) -> None:
    """
    함수 이름: _validate_notarization()
    기능: app과 final DMG의 독립 notarization·distribution verification을 검증한다.
    인자: notarization_value -> notarization JSON value
    반환값: 없음
    작성 날짜: 2026/08/24
    """
    notarization = _require_object(notarization_value, "notarization")
    _require_exact_keys(notarization, {"app", "dmg"}, "notarization")
    app_submission_id = _validate_notarized_artifact(
        notarization["app"],
        "notarization.app",
        require_hdiutil=False,
    )
    dmg_submission_id = _validate_notarized_artifact(
        notarization["dmg"],
        "notarization.dmg",
        require_hdiutil=True,
    )
    if app_submission_id == dmg_submission_id:
        raise EvidenceValidationError(
            "app and DMG notarization submissions must differ."
        )


def _validate_clean_mac(clean_mac_value: Any, build_host_id: str) -> None:
    """
    함수 이름: _validate_clean_mac()
    기능: distinct clean Mac의 quarantine, Keychain, read-only, shutdown과 relaunch smoke를 검증한다.
    인자: clean_mac_value -> clean_mac JSON value
        build_host_id -> clean Mac과 달라야 하는 release build host identifier
    반환값: 없음
    작성 날짜: 2026/08/24
    """
    clean_mac = _require_object(clean_mac_value, "clean_mac")
    _require_exact_keys(
        clean_mac,
        {
            "host_id",
            "quarantine_bypassed",
            "missing_keychain_sidecar_started",
            "read_only_state",
            "allow_testnet_orders",
            "max_notional",
            "orders_created",
            "pending_orders",
            "position_quantity",
            "safe_shutdown",
            "shutdown_http_status",
            "shutdown_receipt",
            "native_exit_code",
            "orphan_processes",
            "relaunch_state",
        },
        "clean_mac",
    )

    clean_host_id = _require_non_empty_string(clean_mac["host_id"], "clean_mac.host_id")
    if clean_host_id == build_host_id:
        raise EvidenceValidationError("clean_mac.host_id must differ from build host.")
    _require_boolean(
        clean_mac["quarantine_bypassed"],
        False,
        "clean_mac.quarantine_bypassed",
    )
    _require_boolean(
        clean_mac["missing_keychain_sidecar_started"],
        False,
        "clean_mac.missing_keychain_sidecar_started",
    )
    if clean_mac["read_only_state"] != "READY":
        raise EvidenceValidationError("clean_mac.read_only_state is invalid.")
    _require_boolean(
        clean_mac["allow_testnet_orders"],
        False,
        "clean_mac.allow_testnet_orders",
    )
    if clean_mac["max_notional"] is not None:
        raise EvidenceValidationError("clean_mac.max_notional must be null.")
    if _require_non_negative_integer(
        clean_mac["orders_created"],
        "clean_mac.orders_created",
    ) != 0:
        raise EvidenceValidationError("clean_mac.orders_created must be zero.")
    if _require_non_negative_integer(
        clean_mac["pending_orders"],
        "clean_mac.pending_orders",
    ) != 0:
        raise EvidenceValidationError("clean_mac.pending_orders must be zero.")
    if clean_mac["position_quantity"] != "0":
        raise EvidenceValidationError("clean_mac.position_quantity must be zero.")
    _require_boolean(
        clean_mac["safe_shutdown"],
        True,
        "clean_mac.safe_shutdown",
    )
    if _require_non_negative_integer(
        clean_mac["shutdown_http_status"],
        "clean_mac.shutdown_http_status",
    ) != 202:
        raise EvidenceValidationError(
            "clean_mac.shutdown_http_status must be accepted."
        )
    if clean_mac["shutdown_receipt"] != "CLOSED":
        raise EvidenceValidationError("clean_mac.shutdown_receipt is invalid.")
    if _require_non_negative_integer(
        clean_mac["native_exit_code"],
        "clean_mac.native_exit_code",
    ) != 0:
        raise EvidenceValidationError("clean_mac.native_exit_code must be zero.")
    if _require_non_negative_integer(
        clean_mac["orphan_processes"],
        "clean_mac.orphan_processes",
    ) != 0:
        raise EvidenceValidationError("clean_mac.orphan_processes must be zero.")
    if clean_mac["relaunch_state"] != "READY":
        raise EvidenceValidationError("clean_mac.relaunch_state is invalid.")


def _validate_regression_count(count_value: Any, field_name: str) -> None:
    """
    함수 이름: _validate_regression_count()
    기능: regression suite의 total과 pass/skip/fail/error count가 완전하고 무결한지 확인한다.
    인자: count_value -> suite count JSON value
        field_name -> suite를 식별하는 non-secret schema field 이름
    반환값: 없음
    작성 날짜: 2026/08/24
    """
    regression_count = _require_object(count_value, field_name)
    _require_exact_keys(
        regression_count,
        {"total", "passed", "skipped", "failed", "errors"},
        field_name,
    )
    total_count = _require_non_negative_integer(
        regression_count["total"],
        f"{field_name}.total",
    )
    passed_count = _require_non_negative_integer(
        regression_count["passed"],
        f"{field_name}.passed",
    )
    skipped_count = _require_non_negative_integer(
        regression_count["skipped"],
        f"{field_name}.skipped",
    )
    failed_count = _require_non_negative_integer(
        regression_count["failed"],
        f"{field_name}.failed",
    )
    error_count = _require_non_negative_integer(
        regression_count["errors"],
        f"{field_name}.errors",
    )

    if total_count <= 0 or passed_count <= 0:
        raise EvidenceValidationError(f"{field_name} did not execute passing tests.")
    if failed_count != 0 or error_count != 0:
        raise EvidenceValidationError(f"{field_name} contains failures.")
    if total_count != passed_count + skipped_count + failed_count + error_count:
        raise EvidenceValidationError(f"{field_name} counts are incomplete.")
    suite_name = field_name.removeprefix("regressions.")
    minimum_total = MINIMUM_REGRESSION_TOTALS.get(suite_name)
    if minimum_total is None or total_count < minimum_total:
        raise EvidenceValidationError(f"{field_name} count regressed below baseline.")


def _validate_regressions(regressions_value: Any) -> None:
    """
    함수 이름: _validate_regressions()
    기능: backend, UI, Rust와 script regression suite의 필수 count를 모두 검증한다.
    인자: regressions_value -> regressions JSON value
    반환값: 없음
    작성 날짜: 2026/08/24
    """
    regressions = _require_object(regressions_value, "regressions")
    required_suite_keys = set(REQUIRED_REGRESSION_SUITES)
    _require_exact_keys(regressions, required_suite_keys, "regressions")
    for suite_name in REQUIRED_REGRESSION_SUITES:
        _validate_regression_count(
            regressions[suite_name],
            f"regressions.{suite_name}",
        )


def _validate_checks(checks_value: Any) -> None:
    """
    함수 이름: _validate_checks()
    기능: count가 없는 build, static, shell과 mounted-DMG release check를 모두 검증한다.
    인자: checks_value -> checks JSON value
    반환값: 없음
    작성 날짜: 2026/08/24
    """
    checks = _require_object(checks_value, "checks")
    _require_exact_keys(checks, set(REQUIRED_CHECKS), "checks")
    for check_name in REQUIRED_CHECKS:
        _require_boolean(checks[check_name], True, f"checks.{check_name}")


def _validate_secret_scan(secret_scan_value: Any) -> None:
    """
    함수 이름: _validate_secret_scan()
    기능: final secret scan이 충분한 canary와 실제 file을 성공적으로 검사했는지 확인한다.
    인자: secret_scan_value -> secret_scan JSON value
    반환값: 없음
    작성 날짜: 2026/08/24
    """
    secret_scan = _require_object(secret_scan_value, "secret_scan")
    secret_scan_keys = {
        "passed",
        "canaries",
        "files",
        *SECRET_SCAN_COVERAGE_TARGETS,
    }
    _require_exact_keys(
        secret_scan,
        secret_scan_keys,
        "secret_scan",
    )
    _require_boolean(secret_scan["passed"], True, "secret_scan.passed")
    for coverage_target in SECRET_SCAN_COVERAGE_TARGETS:
        _require_boolean(
            secret_scan[coverage_target],
            True,
            f"secret_scan.{coverage_target}",
        )
    canary_count = _require_non_negative_integer(
        secret_scan["canaries"],
        "secret_scan.canaries",
    )
    file_count = _require_non_negative_integer(
        secret_scan["files"],
        "secret_scan.files",
    )
    if canary_count < MINIMUM_SECRET_CANARIES or file_count <= 0:
        raise EvidenceValidationError("secret_scan coverage is incomplete.")


def _validate_sha256(sha256_value: Any) -> None:
    """
    함수 이름: _validate_sha256()
    기능: build, final과 clean Mac evidence의 canonical lowercase DMG SHA-256 일치를 검증한다.
    인자: sha256_value -> sha256 JSON value
    반환값: 없음
    작성 날짜: 2026/08/24
    """
    sha256_evidence = _require_object(sha256_value, "sha256")
    digest_locations = ("build", "final", "clean_mac")
    _require_exact_keys(sha256_evidence, set(digest_locations), "sha256")

    digests: list[str] = []
    for digest_location in digest_locations:
        digest_value = _require_non_empty_string(
            sha256_evidence[digest_location],
            f"sha256.{digest_location}",
        )
        if SHA256_PATTERN.fullmatch(digest_value) is None:
            raise EvidenceValidationError(
                f"sha256.{digest_location} is not canonical lowercase SHA-256."
            )
        if len(set(digest_value)) <= 1:
            raise EvidenceValidationError(
                f"sha256.{digest_location} is a placeholder digest."
            )
        digests.append(digest_value)
    if len(set(digests)) != 1:
        raise EvidenceValidationError("sha256 evidence does not match.")


def validate_release_evidence(manifest_value: Any) -> None:
    """
    함수 이름: validate_release_evidence()
    기능: versioned evidence manifest의 모든 Phase 12 go/no-go invariant를 fail-closed로 검증한다.
    인자: manifest_value -> JSON decoder가 만든 evidence root value
    반환값: 없음
    작성 날짜: 2026/08/24
    """
    manifest = _require_object(manifest_value, "manifest")
    _require_exact_keys(
        manifest,
        {
            "schema_version",
            "release_candidate",
            "signing",
            "notarization",
            "clean_mac",
            "regressions",
            "checks",
            "secret_scan",
            "sha256",
        },
        "manifest",
    )
    if type(manifest["schema_version"]) is not int or (
        manifest["schema_version"] != SCHEMA_VERSION
    ):
        raise EvidenceValidationError("schema_version is not supported.")

    build_host_id = _validate_release_candidate(manifest["release_candidate"])
    _validate_signing(manifest["signing"])
    _validate_notarization(manifest["notarization"])
    _validate_clean_mac(manifest["clean_mac"], build_host_id)
    _validate_regressions(manifest["regressions"])
    _validate_checks(manifest["checks"])
    _validate_secret_scan(manifest["secret_scan"])
    _validate_sha256(manifest["sha256"])


IdentityVerifier = Callable[[Mapping[str, str]], ReleaseIdentityEvidence]
ArtifactVerifier = Callable[..., SignedArtifactEvidence]
NotarizationVerifier = Callable[[dict[str, Any], Mapping[str, str]], None]


def verify_notarization_submissions(
    manifest: dict[str, Any],
    source_environment: Mapping[str, str],
    *,
    command_runner: Callable[..., Any] = run_command,
) -> None:
    """
    함수 이름: verify_notarization_submissions()
    기능: manifest UUID를 실제 notarytool info Accepted 결과에 각각 결합한다.
    인자: manifest -> schema 검증 완료 evidence
        source_environment -> NOTARY_PROFILE을 가진 release environment
    반환값: 없음
    작성 날짜: 2026/08/24
    """
    try:
        notary_profile = validate_notary_profile_name(
            source_environment.get("NOTARY_PROFILE")
        )
        process_environment = create_sanitized_environment(source_environment)
        repository_root = Path(__file__).resolve().parents[1]
        for artifact_name in ("app", "dmg"):
            expected_submission_id = manifest["notarization"][artifact_name][
                "submission_id"
            ]
            notary_result = command_runner(
                [
                    "/usr/bin/xcrun",
                    "notarytool",
                    "info",
                    expected_submission_id,
                    "--keychain-profile",
                    notary_profile,
                    "--output-format",
                    "json",
                ],
                repository_root,
                process_environment,
            )
            notary_output = read_successful_stdout(notary_result)
            notary_evidence = json.loads(
                notary_output,
                object_pairs_hook=_load_unique_json_object,
                parse_constant=_reject_nonstandard_json_constant,
            )
            if not isinstance(notary_evidence, dict):
                raise EvidenceValidationError("notarization evidence is invalid")
            actual_submission_id = notary_evidence.get("id")
            if (
                not isinstance(actual_submission_id, str)
                or actual_submission_id.lower() != expected_submission_id.lower()
                or notary_evidence.get("status") != "Accepted"
            ):
                raise EvidenceValidationError("notarization evidence mismatched")
    except EvidenceValidationError:
        raise
    except Exception:
        raise EvidenceValidationError("live notarization evidence failed") from None


def _open_regular_file_no_follow(file_path: Path) -> tuple[int, os.stat_result]:
    """
    함수 이름: _open_regular_file_no_follow()
    기능: symlink를 따라가지 않고 exact regular file descriptor와 최초 metadata를 고정한다.
    인자: file_path -> release gate가 읽을 file
    반환값: open descriptor와 fstat 결과
    작성 날짜: 2026/08/24
    """
    file_descriptor = -1
    try:
        open_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        no_follow_flag = getattr(os, "O_NOFOLLOW", 0)
        if no_follow_flag == 0 and file_path.is_symlink():
            raise EvidenceValidationError("release artifact symlink is not allowed")
        file_descriptor = os.open(file_path, open_flags | no_follow_flag)
        file_status = os.fstat(file_descriptor)
        if not stat.S_ISREG(file_status.st_mode) or file_status.st_size <= 0:
            raise EvidenceValidationError("release artifact file is invalid")
        return file_descriptor, file_status
    except EvidenceValidationError:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        raise
    except (OSError, ValueError):
        if file_descriptor >= 0:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        raise EvidenceValidationError("release artifact file is invalid") from None


def _same_file_snapshot(
    first_status: os.stat_result,
    second_status: os.stat_result,
) -> bool:
    """
    함수 이름: _same_file_snapshot()
    기능: release file identity와 mutation-sensitive metadata가 한 검증 동안 유지됐는지 비교한다.
    인자: first_status -> 최초 fstat
        second_status -> 후속 fstat 또는 lstat
    반환값: 동일 snapshot이면 True
    작성 날짜: 2026/08/24
    """
    return _stable_file_snapshot(first_status) == _stable_file_snapshot(second_status)


def calculate_regular_file_sha256(file_path: Path) -> str:
    """
    함수 이름: calculate_regular_file_sha256()
    기능: 한 non-symlink regular file의 stable byte에 대한 lowercase SHA-256을 계산한다.
    인자: file_path -> final DMG path
    반환값: canonical lowercase SHA-256
    작성 날짜: 2026/08/24
    """
    file_descriptor, initial_status = _open_regular_file_no_follow(file_path)
    digest = hashlib.sha256()
    try:
        while True:
            file_chunk = os.read(file_descriptor, FILE_READ_CHUNK_BYTES)
            if not file_chunk:
                break
            digest.update(file_chunk)
        final_descriptor_status = os.fstat(file_descriptor)
    except OSError:
        raise EvidenceValidationError("release artifact digest failed") from None
    finally:
        try:
            os.close(file_descriptor)
        except OSError:
            pass

    try:
        final_path_status = os.lstat(file_path)
    except OSError:
        raise EvidenceValidationError("release artifact changed during digest") from None
    if (
        stat.S_ISLNK(final_path_status.st_mode)
        or not _same_file_snapshot(initial_status, final_descriptor_status)
        or not _same_file_snapshot(initial_status, final_path_status)
    ):
        raise EvidenceValidationError("release artifact changed during digest")
    return digest.hexdigest()


def calculate_app_tree_sha256(app_path: Path) -> str:
    """
    함수 이름: calculate_app_tree_sha256()
    기능: symlink 없는 app tree의 relative path와 모든 regular-file byte를 stable digest로 묶는다.
    인자: app_path -> physical absolute release app bundle
    반환값: canonical lowercase SHA-256
    작성 날짜: 2026/08/24
    """
    try:
        initial_root_status = _inspect_physical_absolute_path(app_path)
    except (OSError, RuntimeError, TypeError, ValueError):
        raise EvidenceValidationError("release app tree is invalid") from None
    if not stat.S_ISDIR(initial_root_status.st_mode):
        raise EvidenceValidationError("release app tree is invalid")

    tree_digest = hashlib.sha256()
    entry_count = 0

    def hash_directory(directory_path: Path, relative_directory: Path) -> None:
        nonlocal entry_count
        try:
            initial_directory_status = _inspect_physical_absolute_path(
                directory_path
            )
            if not stat.S_ISDIR(initial_directory_status.st_mode):
                raise EvidenceValidationError("release app tree is invalid")
            with os.scandir(directory_path) as directory_iterator:
                directory_entries = sorted(
                    directory_iterator,
                    key=lambda directory_entry: os.fsencode(directory_entry.name),
                )
        except EvidenceValidationError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            raise EvidenceValidationError("release app tree is invalid") from None

        for directory_entry in directory_entries:
            entry_count += 1
            if entry_count > MAXIMUM_APP_TREE_ENTRIES:
                raise EvidenceValidationError("release app tree is too large")
            relative_path = relative_directory / directory_entry.name
            relative_path_bytes = os.fsencode(str(relative_path))
            if (
                not relative_path_bytes
                or len(relative_path_bytes) > MAXIMUM_APP_RELATIVE_PATH_BYTES
            ):
                raise EvidenceValidationError("release app tree path is invalid")
            entry_path = directory_path / directory_entry.name
            try:
                entry_status = _inspect_physical_absolute_path(entry_path)
            except (OSError, RuntimeError, TypeError, ValueError):
                raise EvidenceValidationError("release app tree is invalid") from None

            tree_digest.update(len(relative_path_bytes).to_bytes(4, "big"))
            tree_digest.update(relative_path_bytes)
            entry_metadata = ":".join(
                str(metadata_value)
                for metadata_value in (
                    entry_status.st_mode,
                    *_stable_file_snapshot(entry_status),
                )
            ).encode("ascii")
            tree_digest.update(len(entry_metadata).to_bytes(4, "big"))
            tree_digest.update(entry_metadata)
            if stat.S_ISDIR(entry_status.st_mode):
                tree_digest.update(b"D")
                hash_directory(entry_path, relative_path)
            elif stat.S_ISREG(entry_status.st_mode):
                tree_digest.update(b"F")
                tree_digest.update(
                    bytes.fromhex(calculate_regular_file_sha256(entry_path))
                )
            else:
                # Signed release app에는 symlink, device, FIFO와 socket을 허용하지 않는다.
                raise EvidenceValidationError("release app tree entry is invalid")

            try:
                final_entry_status = _inspect_physical_absolute_path(entry_path)
            except (OSError, RuntimeError, TypeError, ValueError):
                raise EvidenceValidationError("release app tree changed") from None
            if (
                entry_status.st_mode != final_entry_status.st_mode
                or not _same_file_snapshot(entry_status, final_entry_status)
            ):
                raise EvidenceValidationError("release app tree changed")

        try:
            final_directory_status = _inspect_physical_absolute_path(directory_path)
        except (OSError, RuntimeError, TypeError, ValueError):
            raise EvidenceValidationError("release app tree changed") from None
        if not _same_file_snapshot(
            initial_directory_status,
            final_directory_status,
        ):
            raise EvidenceValidationError("release app tree changed")

    hash_directory(app_path, Path())
    try:
        final_root_status = _inspect_physical_absolute_path(app_path)
    except (OSError, RuntimeError, TypeError, ValueError):
        raise EvidenceValidationError("release app tree changed") from None
    if not _same_file_snapshot(initial_root_status, final_root_status):
        raise EvidenceValidationError("release app tree changed")
    return tree_digest.hexdigest()


def read_app_release_metadata(app_path: Path) -> tuple[str, str]:
    """
    함수 이름: read_app_release_metadata()
    기능: bounded Info.plist에서 fixed bundle ID와 release version/build를 읽는다.
    인자: app_path -> verified release .app path
    반환값: CFBundleShortVersionString과 CFBundleVersion
    작성 날짜: 2026/08/24
    """
    info_plist_path = app_path / "Contents" / "Info.plist"
    file_descriptor, initial_status = _open_regular_file_no_follow(info_plist_path)
    try:
        if initial_status.st_size > MAXIMUM_INFO_PLIST_BYTES:
            raise EvidenceValidationError("release app metadata is invalid")
        plist_chunks: list[bytes] = []
        remaining_bytes = MAXIMUM_INFO_PLIST_BYTES + 1
        while remaining_bytes > 0:
            plist_chunk = os.read(file_descriptor, remaining_bytes)
            if not plist_chunk:
                break
            plist_chunks.append(plist_chunk)
            remaining_bytes -= len(plist_chunk)
        plist_bytes = b"".join(plist_chunks)
        final_status = os.fstat(file_descriptor)
    except OSError:
        raise EvidenceValidationError("release app metadata is invalid") from None
    finally:
        try:
            os.close(file_descriptor)
        except OSError:
            pass
    if (
        len(plist_bytes) != initial_status.st_size
        or not _same_file_snapshot(initial_status, final_status)
    ):
        raise EvidenceValidationError("release app metadata changed while reading")
    try:
        app_metadata = plistlib.loads(plist_bytes)
    except (ValueError, TypeError, plistlib.InvalidFileException):
        raise EvidenceValidationError("release app metadata is invalid") from None
    if not isinstance(app_metadata, dict) or (
        app_metadata.get("CFBundleIdentifier") != EXPECTED_BUNDLE_IDENTIFIER
    ):
        raise EvidenceValidationError("release app bundle identifier is invalid")
    release_version = app_metadata.get("CFBundleShortVersionString")
    build_version = app_metadata.get("CFBundleVersion")
    if not isinstance(release_version, str) or not isinstance(build_version, str):
        raise EvidenceValidationError("release app version metadata is invalid")
    return release_version, build_version


def verify_release_evidence_against_artifacts(
    manifest: dict[str, Any],
    app_path: Path,
    dmg_path: Path,
    *,
    source_environment: Mapping[str, str] | None = None,
    identity_verifier: IdentityVerifier = verify_release_identity_environment,
    artifact_verifier: ArtifactVerifier = verify_signed_artifacts,
    notarization_verifier: NotarizationVerifier = verify_notarization_submissions,
) -> None:
    """
    함수 이름: verify_release_evidence_against_artifacts()
    기능: 자기신고 JSON만으로 PASS하지 않도록 live identity, clean git, app/DMG와 digest에 결합한다.
    인자: manifest -> schema 검증 완료 evidence
        app_path -> exact signed/stapled app
        dmg_path -> exact signed/stapled final DMG
        source_environment -> identity/notary preflight environment
        identity_verifier -> 주입 가능한 live release identity verifier
        artifact_verifier -> 주입 가능한 signed artifact verifier
    반환값: 없음
    작성 날짜: 2026/08/24
    """
    process_environment = os.environ if source_environment is None else source_environment
    try:
        initial_app_path_status = _inspect_physical_absolute_path(app_path)
        initial_dmg_path_status = _inspect_physical_absolute_path(dmg_path)
    except (OSError, RuntimeError, TypeError, ValueError):
        raise EvidenceValidationError("release artifact path is invalid") from None
    if not stat.S_ISDIR(initial_app_path_status.st_mode) or not stat.S_ISREG(
        initial_dmg_path_status.st_mode
    ):
        raise EvidenceValidationError("release artifact path is invalid")
    initial_app_tree_digest = calculate_app_tree_sha256(app_path)
    release_candidate = manifest["release_candidate"]
    signing = manifest["signing"]
    expected_identity = signing["identity"]
    expected_team_id = signing["team_id"]
    expected_version = release_candidate["version"]
    expected_build_version = release_candidate["build_version"]
    expected_commit = release_candidate["commit"]

    try:
        identity_evidence = identity_verifier(process_environment)
    except Exception:
        raise EvidenceValidationError("live release identity evidence failed") from None
    if (
        identity_evidence.commit_id != release_candidate["commit"]
        or identity_evidence.identity != expected_identity
        or identity_evidence.team_id != expected_team_id
    ):
        raise EvidenceValidationError("live release identity evidence mismatched")

    try:
        notarization_verifier(manifest, process_environment)
    except EvidenceValidationError:
        raise
    except Exception:
        raise EvidenceValidationError("live notarization evidence failed") from None

    actual_version, actual_build_version = read_app_release_metadata(app_path)
    if (
        actual_version != expected_version
        or actual_build_version != expected_build_version
    ):
        raise EvidenceValidationError("release app version evidence mismatched")

    pre_verification_dmg_digest = calculate_regular_file_sha256(dmg_path)
    if any(
        digest_value != pre_verification_dmg_digest
        for digest_value in manifest["sha256"].values()
    ):
        raise EvidenceValidationError("live release artifact digest mismatched")

    try:
        artifact_evidence = artifact_verifier(
            app_path,
            expected_team_id,
            dmg_path=dmg_path,
            source_environment=process_environment,
            expected_identity=expected_identity,
            expected_version=expected_version,
            expected_build_version=expected_build_version,
            expected_commit=expected_commit,
        )
    except Exception:
        raise EvidenceValidationError("live signed artifact evidence failed") from None
    if (
        artifact_evidence.team_id != expected_team_id
        or artifact_evidence.dmg_verified is not True
        or artifact_evidence.commit != expected_commit
        or artifact_evidence.provenance_sha256 != signing["provenance_sha256"]
        or artifact_evidence.dmg_sha256 != pre_verification_dmg_digest
    ):
        raise EvidenceValidationError("live signed artifact evidence mismatched")
    try:
        verified_dmg_snapshot = artifact_evidence.dmg_snapshot
        snapshot_values = (
            verified_dmg_snapshot.device,
            verified_dmg_snapshot.inode,
            verified_dmg_snapshot.size,
            verified_dmg_snapshot.mtime_ns,
            verified_dmg_snapshot.ctime_ns,
        )
    except (AttributeError, TypeError):
        raise EvidenceValidationError(
            "live signed artifact snapshot is invalid"
        ) from None
    if snapshot_values != _stable_file_snapshot(initial_dmg_path_status):
        raise EvidenceValidationError("live signed artifact snapshot mismatched")
    try:
        actual_native_evidence_items = tuple(
            (native_evidence.kind, native_evidence.sha256)
            for native_evidence in artifact_evidence.extracted_native
        )
        actual_native_evidence = set(actual_native_evidence_items)
    except (AttributeError, TypeError):
        raise EvidenceValidationError(
            "live extracted native evidence is invalid"
        ) from None
    manifest_native_evidence = {
        (native_evidence["kind"], native_evidence["sha256"])
        for native_evidence in signing["extracted_native"]
    }
    if (
        actual_native_evidence != manifest_native_evidence
        or len(actual_native_evidence_items) != len(actual_native_evidence)
        or len(actual_native_evidence_items) != len(signing["extracted_native"])
        or len(actual_native_evidence) != len(signing["extracted_native"])
    ):
        raise EvidenceValidationError("live extracted native evidence mismatched")

    post_verification_dmg_digest = calculate_regular_file_sha256(dmg_path)
    if post_verification_dmg_digest != pre_verification_dmg_digest:
        raise EvidenceValidationError("release artifact changed during verification")
    if calculate_app_tree_sha256(app_path) != initial_app_tree_digest:
        raise EvidenceValidationError("release app changed during verification")
    try:
        final_app_path_status = _inspect_physical_absolute_path(app_path)
        final_dmg_path_status = _inspect_physical_absolute_path(dmg_path)
    except (OSError, RuntimeError, TypeError, ValueError):
        raise EvidenceValidationError("release artifact path changed") from None
    if not _same_file_snapshot(
        initial_app_path_status,
        final_app_path_status,
    ) or not _same_file_snapshot(
        initial_dmg_path_status,
        final_dmg_path_status,
    ):
        raise EvidenceValidationError("release artifact path changed")


def _print_generic_error() -> None:
    """
    함수 이름: _print_generic_error()
    기능: manifest value, field와 path가 없는 고정 release gate 오류를 stderr에 출력한다.
    인자: 없음
    반환값: 없음
    작성 날짜: 2026/08/24
    """
    print(GENERIC_ERROR_MESSAGE, file=sys.stderr)


def main(
    argument_values: Sequence[str] | None = None,
    *,
    runtime_verifier: Callable[[dict[str, Any], Path, Path], None] | None = None,
) -> int:
    """
    함수 이름: main()
    기능: evidence JSON, exact app과 DMG를 live release state에 결합해 generic gate status를 반환한다.
    인자: argument_values -> 명시 command line 인자 또는 실제 argv를 뜻하는 None
    반환값: 합격 0, evidence 불합격 1, 사용법·file·JSON 오류 2
    작성 날짜: 2026/08/24
    """
    try:
        resolved_arguments = list(
            sys.argv[1:] if argument_values is None else argument_values
        )
        if len(resolved_arguments) != 3 or any(
            not isinstance(argument_value, str)
            for argument_value in resolved_arguments
        ):
            _print_generic_error()
            return 2

        manifest_value = load_evidence_manifest(Path(resolved_arguments[0]))
        validate_release_evidence(manifest_value)
        selected_runtime_verifier = (
            verify_release_evidence_against_artifacts
            if runtime_verifier is None
            else runtime_verifier
        )
        selected_runtime_verifier(
            manifest_value,
            Path(resolved_arguments[1]),
            Path(resolved_arguments[2]),
        )
    except EvidenceValidationError:
        _print_generic_error()
        return 1
    except Exception:
        # Unexpected I/O·decode·usage failure도 raw exception 없이 같은 fixed message로 닫는다.
        _print_generic_error()
        return 2

    print(GENERIC_PASS_MESSAGE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
