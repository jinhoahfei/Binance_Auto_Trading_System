#!/usr/bin/env python3
"""Phase 13 in-scope GAP과 사용자가 영구 제외한 soak를 정직한 manifest로 기록한다."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Any


SCHEMA_VERSION = 2
MAXIMUM_MANIFEST_BYTES = 256 * 1024
UTC_TIMESTAMP_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z"
)
READINESS_CHECK_TOOL_CANDIDATES: dict[str, tuple[str, ...]] = {
    "python_dependency_audit": ("pip-audit", "osv-scanner"),
    "ui_dependency_audit": ("pnpm", "osv-scanner"),
    "rust_dependency_audit": ("cargo-audit", "cargo-deny"),
    "license_inventory": ("pip-licenses", "cargo-license", "pnpm"),
    "sbom": ("cyclonedx-py", "syft"),
}
MANUAL_GAP_CHECKS = (
    "third_party_notice",
    "third_party_license_review",
)
EXCLUDED_CHECKS: dict[str, str] = {
    "phase13_soak_report": "USER_SCOPE_EXCLUSION",
}
ALLOWED_GAP_REASONS = frozenset(
    {
        "TOOL_UNAVAILABLE",
        "NOT_RUN",
        "MANUAL_REVIEW_REQUIRED",
    }
)


class ReadinessManifestError(RuntimeError):
    """
    클래스 이름: ReadinessManifestError
    기능: Phase 13 readiness manifest의 file framing 또는 exact schema 위반을 나타낸다.
    작성 날짜: 2026/08/24
    """


def _utc_now() -> datetime:
    """
    함수 이름: _utc_now()
    기능: readiness manifest 생성 시각으로 사용할 timezone-aware UTC 현재 시각을 반환한다.
    인자: 없음
    반환값: UTC datetime
    작성 날짜: 2026/08/24
    """
    return datetime.now(timezone.utc)


def _format_utc(value: datetime) -> str:
    """
    함수 이름: _format_utc()
    기능: UTC datetime을 microsecond 없는 canonical Z 문자열로 변환한다.
    인자: value -> timezone-aware UTC datetime
    반환값: canonical timestamp 문자열
    작성 날짜: 2026/08/24
    """
    # naive 또는 UTC가 아닌 시각을 manifest의 canonical 생성 시각으로 기록하지 않는다.
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TypeError("value must be a timezone-aware datetime")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("value must use UTC")
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _select_available_tool(
    candidates: tuple[str, ...],
    tool_finder: Callable[[str], str | None],
) -> str | None:
    """
    함수 이름: _select_available_tool()
    기능: 후보 중 첫 executable 이름만 반환하고 host absolute path는 evidence에 넣지 않는다.
    인자: candidates -> 고정 tool 이름 tuple
        tool_finder -> executable path 또는 None을 반환할 주입 finder
    반환값: 발견된 tool 이름 또는 None
    작성 날짜: 2026/08/24
    """
    if not isinstance(candidates, tuple) or not candidates:
        raise ValueError("candidates must be a non-empty tuple")
    if not callable(tool_finder):
        raise TypeError("tool_finder must be callable")
    for tool_name in candidates:
        if tool_finder(tool_name) is not None:
            return tool_name  # 사용자별 install path 대신 공개 tool 이름만 기록한다.
    return None


def build_gap_readiness_manifest(
    *,
    generated_at: datetime | None = None,
    tool_finder: Callable[[str], str | None] = shutil.which,
) -> dict[str, object]:
    """
    함수 이름: build_gap_readiness_manifest()
    기능: 도구 발견만으로 PASS를 만들지 않고 in-scope GAP과 영구 제외를 구분해 기록한다.
    인자: generated_at -> 주입 UTC 생성 시각 또는 None
        tool_finder -> PATH tool lookup callable
    반환값: secret-free readiness manifest object
    작성 날짜: 2026/08/24
    """
    selected_time = _utc_now() if generated_at is None else generated_at
    checks: list[dict[str, object]] = []

    # Tool이 있어도 실제 scan log가 없으므로 NOT_RUN이며, 없을 때만 TOOL_UNAVAILABLE로 구분한다.
    for check_id, tool_candidates in READINESS_CHECK_TOOL_CANDIDATES.items():
        selected_tool = _select_available_tool(tool_candidates, tool_finder)
        checks.append(
            {
                "check_id": check_id,
                "status": "GAP",
                "reason": (
                    "NOT_RUN" if selected_tool is not None else "TOOL_UNAVAILABLE"
                ),
                "tool": selected_tool,
                "evidence_sha256": None,
            }
        )

    # 프로젝트 자체 정책은 별도 byte-bound gate가 검증하므로 남은 제3자 notice/review만 요구한다.
    for check_id in MANUAL_GAP_CHECKS:
        checks.append(
            {
                "check_id": check_id,
                "status": "GAP",
                "reason": "MANUAL_REVIEW_REQUIRED",
                "tool": None,
                "evidence_sha256": None,
            }
        )

    # 사용자가 Phase 13에서 영구 제외한 soak는 GAP이나 PASS가 아닌 독립 범위 결정으로 보존한다.
    for check_id, exclusion_reason in EXCLUDED_CHECKS.items():
        checks.append(
            {
                "check_id": check_id,
                "status": "EXCLUDED",
                "reason": exclusion_reason,
                "tool": None,
                "evidence_sha256": None,
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "phase13_readiness_manifest",
        "generated_at": _format_utc(selected_time),
        "overall_status": "GAP",
        "checks": checks,
    }  # 누락 artifact를 빈 THIRD_PARTY/SBOM 파일이나 성공 boolean으로 위장하지 않는다.


def _load_unique_json_object(
    object_pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    """
    함수 이름: _load_unique_json_object()
    기능: JSON object의 duplicate key를 마지막 값으로 덮지 않고 즉시 거부한다.
    인자: object_pairs -> decoder가 전달한 순서 보존 key-value pair
    반환값: duplicate key가 없는 dictionary
    작성 날짜: 2026/08/24
    """
    # Decoder pair 순서를 유지하되 같은 key가 다시 나타나는 순간 ambiguous 입력을 거부한다.
    loaded_object: dict[str, Any] = {}
    for object_key, object_value in object_pairs:
        if object_key in loaded_object:
            raise ReadinessManifestError("readiness manifest contains duplicate keys")
        loaded_object[object_key] = object_value
    return loaded_object


def _reject_nonstandard_constant(constant_name: str) -> None:
    """
    함수 이름: _reject_nonstandard_constant()
    기능: NaN과 Infinity 같은 JSON 표준 밖 constant를 fail-closed로 거부한다.
    인자: constant_name -> decoder가 발견한 raw constant 이름
    반환값: 정상 반환하지 않음
    작성 날짜: 2026/08/24
    """
    del constant_name  # Raw manifest token을 CLI 오류에 반사하지 않는다.
    raise ReadinessManifestError("readiness manifest contains non-standard JSON")


def validate_readiness_manifest(manifest: object) -> tuple[str, ...]:
    """
    함수 이름: validate_readiness_manifest()
    기능: exact readiness schema를 검증하고 아직 해결되지 않은 GAP check ID를 반환한다.
    인자: manifest -> JSON decoder가 만든 root object
    반환값: manifest 순서의 GAP check ID tuple
    작성 날짜: 2026/08/24
    """
    # Root object와 고정 metadata를 먼저 검증해 다른 record가 readiness로 해석되지 않게 한다.
    if not isinstance(manifest, Mapping):
        raise ReadinessManifestError("readiness manifest root must be an object")
    if set(manifest) != {
        "schema_version",
        "record_type",
        "generated_at",
        "overall_status",
        "checks",
    }:
        raise ReadinessManifestError("readiness manifest root keys are invalid")
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise ReadinessManifestError("readiness manifest schema is unsupported")
    if manifest["record_type"] != "phase13_readiness_manifest":
        raise ReadinessManifestError("readiness manifest record type is invalid")
    if (
        not isinstance(manifest["generated_at"], str)
        or UTC_TIMESTAMP_PATTERN.fullmatch(manifest["generated_at"]) is None
    ):
        raise ReadinessManifestError("readiness manifest timestamp is invalid")
    if manifest["overall_status"] != "GAP":
        raise ReadinessManifestError(
            "readiness PASS is unavailable without bound evidence artifacts"
        )

    # Check 목록은 누락·중복 여부를 마지막에 대조할 canonical ID 집합과 함께 순회한다.
    checks = manifest["checks"]
    if not isinstance(checks, list):
        raise ReadinessManifestError("readiness checks must be a list")

    expected_check_ids = (
        set(READINESS_CHECK_TOOL_CANDIDATES)
        | set(MANUAL_GAP_CHECKS)
        | set(EXCLUDED_CHECKS)
    )
    observed_check_ids: set[str] = set()
    gap_check_ids: list[str] = []
    for check in checks:
        if not isinstance(check, Mapping) or set(check) != {
            "check_id",
            "status",
            "reason",
            "tool",
            "evidence_sha256",
        }:
            raise ReadinessManifestError("readiness check keys are invalid")
        check_id = check["check_id"]
        if not isinstance(check_id, str) or check_id not in expected_check_ids:
            raise ReadinessManifestError("readiness check ID is invalid")
        if check_id in observed_check_ids:
            raise ReadinessManifestError("readiness check ID is duplicated")
        observed_check_ids.add(check_id)

        # 현재 schema는 in-scope GAP과 고정 제외만 허용하고 digest-only PASS를 거부한다.
        check_status = check["status"]
        reason = check["reason"]
        tool = check["tool"]
        evidence_sha256 = check["evidence_sha256"]
        allowed_tools = READINESS_CHECK_TOOL_CANDIDATES.get(check_id, ())
        if tool is not None and tool not in allowed_tools:
            raise ReadinessManifestError("readiness tool name is invalid")
        if (
            check_id in MANUAL_GAP_CHECKS or check_id in EXCLUDED_CHECKS
        ) and tool is not None:
            raise ReadinessManifestError(
                "manual or excluded readiness check cannot name a tool"
            )
        if check_status == "GAP":
            if reason not in ALLOWED_GAP_REASONS or evidence_sha256 is not None:
                raise ReadinessManifestError("readiness GAP evidence is invalid")
            gap_check_ids.append(check_id)
        elif check_status == "EXCLUDED":
            if (
                check_id not in EXCLUDED_CHECKS
                or reason != EXCLUDED_CHECKS[check_id]
                or evidence_sha256 is not None
            ):
                raise ReadinessManifestError(
                    "readiness exclusion evidence is invalid"
                )
        elif check_status == "PASS":
            raise ReadinessManifestError(
                "readiness PASS is unavailable without bound evidence artifacts"
            )
        else:
            raise ReadinessManifestError("readiness check status is invalid")

    if observed_check_ids != expected_check_ids:
        raise ReadinessManifestError("readiness checks are incomplete")

    # 모든 in-scope check는 GAP으로, 영구 제외 check는 EXCLUDED로 보존되어야 한다.
    expected_gap_count = len(READINESS_CHECK_TOOL_CANDIDATES) + len(
        MANUAL_GAP_CHECKS
    )
    if len(gap_check_ids) != expected_gap_count:
        raise ReadinessManifestError("readiness manifest must preserve every GAP")
    return tuple(gap_check_ids)


def load_readiness_manifest(manifest_path: Path) -> dict[str, object]:
    """
    함수 이름: load_readiness_manifest()
    기능: bounded regular JSON file을 duplicate/nonstandard token 없이 읽고 exact schema를 검증한다.
    인자: manifest_path -> 읽을 readiness JSON 경로
    반환값: 검증된 manifest dictionary
    작성 날짜: 2026/08/24
    """
    # Caller 경로는 symlink가 아닌 bounded regular file로 먼저 제한한다.
    if not isinstance(manifest_path, Path):
        raise TypeError("manifest_path must be a Path")
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ReadinessManifestError("readiness manifest must be a regular file")
    manifest_size = manifest_path.stat().st_size
    if manifest_size <= 0 or manifest_size > MAXIMUM_MANIFEST_BYTES:
        raise ReadinessManifestError("readiness manifest size is invalid")

    # UTF-8, duplicate key와 비표준 상수를 한 parse 경계에서 검증한다.
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(
            manifest_bytes.decode("utf-8"),
            object_pairs_hook=_load_unique_json_object,
            parse_constant=_reject_nonstandard_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReadinessManifestError("readiness manifest could not be loaded") from error

    # Exact semantic schema까지 통과한 실제 dictionary만 caller에게 반환한다.
    validate_readiness_manifest(manifest)
    if not isinstance(manifest, dict):
        raise AssertionError("validated readiness manifest must be a dictionary")
    return manifest


def write_readiness_manifest(
    output_path: Path,
    manifest: Mapping[str, object],
) -> None:
    """
    함수 이름: write_readiness_manifest()
    기능: existing file이나 symlink를 덮지 않고 canonical readiness JSON을 fsync한다.
    인자: output_path -> 새 manifest file 경로
        manifest -> exact schema validation을 통과할 object
    반환값: 없음
    작성 날짜: 2026/08/24
    """
    # 새 파일만 허용하며 parent symlink나 기존 evidence를 덮어쓰지 않는다.
    if not isinstance(output_path, Path):
        raise TypeError("output_path must be a Path")
    validate_readiness_manifest(manifest)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.parent.is_symlink() or output_path.exists() or output_path.is_symlink():
        raise ReadinessManifestError("existing readiness manifest cannot be overwritten")

    # 정렬 key와 compact separator로 동일 의미가 항상 같은 JSON byte를 만들게 한다.
    encoded_manifest = (
        json.dumps(
            manifest,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")

    # Exclusive create 뒤 file content를 fsync해 반환 시점의 evidence 내구성을 보장한다.
    with output_path.open("xb") as manifest_file:
        manifest_file.write(encoded_manifest)
        manifest_file.flush()
        os.fsync(manifest_file.fileno())


def parse_arguments(argument_values: Sequence[str] | None = None) -> argparse.Namespace:
    """
    함수 이름: parse_arguments()
    기능: GAP manifest write와 existing manifest check subcommand를 읽는다.
    인자: argument_values -> 명시 CLI argument 또는 실제 argv를 뜻하는 None
    반환값: argparse Namespace
    작성 날짜: 2026/08/24
    """
    # Write, check와 현재 in-memory GAP gate가 서로 다른 인자를 갖도록 subcommand를 분리한다.
    parser = argparse.ArgumentParser(
        description="Write or check a secret-free Phase 13 readiness manifest.",
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)
    write_parser = subparsers.add_parser("write")
    write_parser.add_argument("output_path", type=Path)
    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("manifest_path", type=Path)
    subparsers.add_parser("gate")
    return parser.parse_args(argument_values)


def main(argument_values: Sequence[str] | None = None) -> int:
    """
    함수 이름: main()
    기능: GAP manifest를 생성하거나 schema와 unresolved GAP을 검사해 exit status로 반환한다.
    인자: argument_values -> 명시 CLI argument 또는 실제 argv를 뜻하는 None
    반환값: valid GAP 1, invalid/error 2
    작성 날짜: 2026/08/24
    """
    # Write/check/gate 모두 같은 validator를 통과시키고 raw path나 JSON 내용은 반사하지 않는다.
    arguments = parse_arguments(argument_values)
    try:
        if arguments.operation == "write":
            manifest = build_gap_readiness_manifest()
            write_readiness_manifest(arguments.output_path, manifest)
            print(
                "phase13-readiness: GAP: manifest written with unresolved "
                "checks and explicit exclusions."
            )
            return 1

        if arguments.operation == "gate":
            manifest = build_gap_readiness_manifest()
            gap_check_ids = validate_readiness_manifest(manifest)
        else:
            manifest = load_readiness_manifest(arguments.manifest_path)
            gap_check_ids = validate_readiness_manifest(manifest)

    except (OSError, ReadinessManifestError, TypeError, ValueError):
        # Tool path와 manifest raw path/contents를 generic 오류 밖으로 내보내지 않는다.
        print("phase13-readiness: ERROR: manifest rejected.", file=sys.stderr)
        return 2

    # CLI summary에도 영구 제외를 노출해 GAP 해소나 PASS로 오해하지 않게 한다.
    exclusion_summary = ",".join(
        f"{check_id}={reason}"
        for check_id, reason in EXCLUDED_CHECKS.items()
    )
    print(
        "phase13-readiness: GAP: "
        + ",".join(gap_check_ids)
        + "; EXCLUDED: "
        + exclusion_summary
        + "."
    )
    return 1  # Evidence path와 실제 byte hash를 결합하는 후속 schema 전에는 PASS를 만들지 않는다.


if __name__ == "__main__":
    raise SystemExit(main())
