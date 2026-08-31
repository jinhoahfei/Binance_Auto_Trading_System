#!/usr/bin/env python3
"""Phase 13 actual browser JPEG와 고정 Figma PNG를 read-only SSIM으로 비교한다."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VISUAL_DIRECTORY = Path("UI/visual-regression")
BASELINE_MANIFEST_PATH = VISUAL_DIRECTORY / "baseline_manifest.json"
COMPARISON_POLICY_PATH = VISUAL_DIRECTORY / "comparison_policy.json"
CURRENT_MANIFEST_PATH = VISUAL_DIRECTORY / "current_capture_manifest.json"
BASELINE_DIRECTORY = VISUAL_DIRECTORY / "figma"
DEFAULT_CURRENT_DIRECTORY = VISUAL_DIRECTORY / "current"
MAXIMUM_JSON_BYTES = 256 * 1024
MAXIMUM_IMAGE_BYTES = 20 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
SSIM_PATTERN = re.compile(r"\bAll:(?P<score>[0-9]+(?:\.[0-9]+)?)")
EXPECTED_FRAME_KEYS = (
    "realtime-indicator",
    "recent-orders",
    "indicator-settings",
    "trade-history",
    "start-confirmation",
    "stop-with-position",
    "stop-without-position",
    "csv-end-calendar",
    "csv-start-calendar",
    "csv-dialog",
    "regime-confirmation",
    "regime-required",
    "regime-highlight",
    "history-empty",
    "start-loading",
    "csv-validation-error",
)
EXPECTED_BASELINE_SOURCE = {
    "kind": "figma_export",
    "file_key": "kzt9vOXj0QmPeYxO8SVPax",
    "captured_on": "2026-08-12",
}


class VisualRegressionError(RuntimeError):
    """
    클래스 이름: VisualRegressionError
    기능: Visual evidence의 schema, artifact 또는 comparison 계약 위반을 나타낸다.
    작성 날짜: 2026/08/29
    """


@dataclass(frozen=True)
class VisualComparisonResult:
    """
    클래스 이름: VisualComparisonResult
    기능: 한 frame의 SSIM 점수와 threshold 통과 여부를 보존한다.
    작성 날짜: 2026/08/29
    """

    frame_key: str
    score: Decimal
    passed: bool


def _reject_duplicate_keys(
    object_pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    """
    함수 이름: _reject_duplicate_keys()
    기능: JSON duplicate key를 silent overwrite하지 않고 거부한다.
    인자: object_pairs -> decoder가 원문 순서로 전달한 key/value pair
    반환값: duplicate key가 없는 dictionary
    작성 날짜: 2026/08/29
    """
    loaded_object: dict[str, Any] = {}

    # 첫 key를 보존하고 같은 key가 다시 나타나는 순간 evidence 전체를 거부한다.
    for object_key, object_value in object_pairs:
        if object_key in loaded_object:
            raise VisualRegressionError("visual JSON contains duplicate keys")
        loaded_object[object_key] = object_value
    return loaded_object


def _reject_nonstandard_constant(constant_name: str) -> None:
    """
    함수 이름: _reject_nonstandard_constant()
    기능: NaN과 Infinity 같은 JSON 표준 밖 숫자를 거부한다.
    인자: constant_name -> decoder가 발견한 비표준 constant 이름
    반환값: 정상 반환하지 않음
    작성 날짜: 2026/08/29
    """
    del constant_name  # Raw token은 CLI 오류에 반사하지 않는다.
    raise VisualRegressionError("visual JSON contains non-standard numbers")


def _require_relative_path_parts(relative_path: Path) -> tuple[str, ...]:
    """
    함수 이름: _require_relative_path_parts()
    기능: Visual artifact 상대 경로를 escape 없는 component tuple로 검증한다.
    인자: relative_path -> trusted root 기준 artifact 경로
    반환값: 순서가 보존된 non-empty path component tuple
    작성 날짜: 2026/08/31
    """
    if not isinstance(relative_path, Path) or relative_path.is_absolute():
        raise VisualRegressionError("visual artifact path is invalid")
    path_parts = relative_path.parts
    if not path_parts or any(path_part in {"", ".", ".."} for path_part in path_parts):
        raise VisualRegressionError("visual artifact path is invalid")
    return path_parts  # Dir-fd traversal에는 검증된 lexical component만 전달한다.


def _open_directory_chain(
    root_directory: Path,
    directory_parts: Sequence[str],
) -> tuple[list[int], list[tuple[int, str, os.stat_result]]]:
    """
    함수 이름: _open_directory_chain()
    기능: Trusted visual root부터 각 directory를 no-follow descriptor로 순차 고정한다.
    인자: root_directory -> repository 또는 current capture root,
        directory_parts -> leaf parent까지의 상대 component sequence
    반환값: 열린 directory descriptor들과 parent-entry identity tuple
    작성 날짜: 2026/08/31
    """
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory_descriptors: list[int] = []
    directory_links: list[tuple[int, str, os.stat_result]] = []

    # Root부터 child까지 descriptor-relative open을 사용해 component 검사 경합을 제거한다.
    try:
        root_descriptor = os.open(root_directory, directory_flags)
        directory_descriptors.append(root_descriptor)
        if not stat.S_ISDIR(os.fstat(root_descriptor).st_mode):
            raise VisualRegressionError("visual artifact root is invalid")
        current_descriptor = root_descriptor
        for directory_name in directory_parts:
            path_status = os.stat(
                directory_name,
                dir_fd=current_descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(path_status.st_mode):
                raise VisualRegressionError("visual artifact path is invalid")
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
                raise VisualRegressionError("visual artifact path changed")
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
    기능: 열린 visual parent chain의 각 entry가 같은 inode인지 다시 검증한다.
    인자: directory_links -> parent fd, component 이름, 최초 child 상태 sequence
    반환값: 없음
    작성 날짜: 2026/08/31
    """
    # Parent fd가 살아 있는 동안 각 component를 post-stat해 rename·symlink 교체를 차단한다.
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
            raise VisualRegressionError("visual artifact path changed")


def _read_bounded_regular_file(
    root_directory: Path,
    relative_path: Path,
    maximum_bytes: int,
) -> bytes:
    """
    함수 이름: _read_bounded_regular_file()
    기능: Trusted root 아래 symlink가 아닌 bounded regular file의 bytes를 읽는다.
    인자: root_directory -> repository 또는 capture root,
        relative_path -> root 기준 artifact 경로, maximum_bytes -> 최대 허용 byte 수
    반환값: 검증된 file bytes
    작성 날짜: 2026/08/29
    """
    if type(maximum_bytes) is not int or maximum_bytes <= 0:
        raise ValueError("maximum_bytes must be a positive integer")
    path_parts = _require_relative_path_parts(relative_path)
    directory_descriptors: list[int] = []

    # Root부터 leaf parent까지 descriptor chain을 유지한 채 nonblocking leaf를 읽는다.
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
            raise VisualRegressionError("visual artifact path is invalid")
        file_descriptor = os.open(
            leaf_name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent_descriptor,
        )
        try:
            initial_status = os.fstat(file_descriptor)
            if not stat.S_ISREG(initial_status.st_mode):
                raise VisualRegressionError("visual artifact must be a regular file")
            if initial_status.st_size <= 0 or initial_status.st_size > maximum_bytes:
                raise VisualRegressionError("visual artifact size is invalid")
            if (
                path_status_before.st_dev != initial_status.st_dev
                or path_status_before.st_ino != initial_status.st_ino
            ):
                raise VisualRegressionError("visual artifact changed during read")

            # max+1까지만 읽어 stat 이후 증가도 bounded memory 안에서 거부한다.
            remaining_bytes = maximum_bytes + 1
            file_parts: list[bytes] = []
            while remaining_bytes > 0:
                file_part = os.read(
                    file_descriptor,
                    min(64 * 1024, remaining_bytes),
                )
                if not file_part:
                    break
                file_parts.append(file_part)
                remaining_bytes -= len(file_part)
            file_bytes = b"".join(file_parts)
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
        raise VisualRegressionError("visual artifact could not be read") from error
    finally:
        for directory_descriptor in reversed(directory_descriptors):
            os.close(directory_descriptor)

    # 같은 descriptor도 read 중 truncate/replace되면 identity·metadata·실제 길이가 달라져 fail closed한다.
    if len(file_bytes) <= 0 or len(file_bytes) > maximum_bytes:
        raise VisualRegressionError("visual artifact size changed during read")
    if (
        initial_status.st_dev != final_status.st_dev
        or initial_status.st_ino != final_status.st_ino
        or initial_status.st_size != final_status.st_size
        or initial_status.st_mtime_ns != final_status.st_mtime_ns
        or initial_status.st_ctime_ns != final_status.st_ctime_ns
        or path_status_before.st_dev != initial_status.st_dev
        or path_status_before.st_ino != initial_status.st_ino
        or path_status_before.st_size != initial_status.st_size
        or path_status_before.st_mtime_ns != initial_status.st_mtime_ns
        or path_status_before.st_ctime_ns != initial_status.st_ctime_ns
        or path_status_after.st_dev != initial_status.st_dev
        or path_status_after.st_ino != initial_status.st_ino
        or path_status_after.st_size != initial_status.st_size
        or path_status_after.st_mtime_ns != initial_status.st_mtime_ns
        or path_status_after.st_ctime_ns != initial_status.st_ctime_ns
        or len(file_bytes) != final_status.st_size
    ):
        raise VisualRegressionError("visual artifact changed during read")

    return file_bytes  # Caller는 이 bounded immutable snapshot만 digest·decode에 사용한다.


def _write_verified_image_copy(
    temporary_directory: Path,
    file_name: str,
    image_bytes: bytes,
) -> Path:
    """
    함수 이름: _write_verified_image_copy()
    기능: 검증된 image bytes를 mode 0600 exclusive 임시 파일로 고정한다.
    인자: temporary_directory -> mode 0700 TemporaryDirectory 경로
        file_name -> caller가 고정한 basename
        image_bytes -> digest·dimension 검증을 통과한 bytes
    반환값: FFmpeg에만 전달할 owner-only 임시 image 경로
    작성 날짜: 2026/08/31
    """
    if not isinstance(temporary_directory, Path) or not temporary_directory.is_dir():
        raise ValueError("temporary_directory must be an existing directory")
    if (
        not isinstance(file_name, str)
        or not file_name
        or Path(file_name).name != file_name
    ):
        raise ValueError("file_name must be a basename")
    if not isinstance(image_bytes, bytes) or not image_bytes:
        raise ValueError("image_bytes must be non-empty bytes")

    # O_EXCL과 descriptor 기반 write로 경로 교체·partial write·완화된 permission을 거부한다.
    image_path = temporary_directory / file_name
    try:
        file_descriptor = os.open(
            image_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except OSError as error:
        raise VisualRegressionError("verified image copy could not be created") from error
    try:
        os.fchmod(file_descriptor, 0o600)
        remaining_view = memoryview(image_bytes)
        while remaining_view:
            written_length = os.write(file_descriptor, remaining_view)
            if written_length <= 0:
                raise OSError("verified image copy write was incomplete")
            remaining_view = remaining_view[written_length:]
        os.fsync(file_descriptor)
        written_status = os.fstat(file_descriptor)
    except OSError as error:
        raise VisualRegressionError("verified image copy could not be written") from error
    finally:
        os.close(file_descriptor)
    if (
        not stat.S_ISREG(written_status.st_mode)
        or stat.S_IMODE(written_status.st_mode) != 0o600
        or written_status.st_size != len(image_bytes)
    ):
        raise VisualRegressionError("verified image copy is invalid")

    return image_path  # Original source path가 바뀌어도 FFmpeg 입력 bytes는 불변이다.


def _load_json_object(repository_root: Path, relative_path: Path) -> dict[str, object]:
    """
    함수 이름: _load_json_object()
    기능: Repository 안의 bounded strict JSON object를 읽는다.
    인자: repository_root -> repository root, relative_path -> JSON 상대 경로
    반환값: duplicate/nonstandard 값이 없는 dictionary
    작성 날짜: 2026/08/29
    """
    # Strict parser에는 repository path가 아닌 bounded no-follow snapshot만 전달한다.
    json_bytes = _read_bounded_regular_file(
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
        raise VisualRegressionError("visual JSON is invalid") from error
    if not isinstance(loaded_value, dict):
        raise VisualRegressionError("visual JSON root must be an object")
    return loaded_value  # Exact object root만 schema validator로 전달한다.


def _require_exact_keys(
    value: object,
    expected_keys: frozenset[str],
    label: str,
) -> Mapping[str, object]:
    """
    함수 이름: _require_exact_keys()
    기능: Mapping이 지정 key를 빠짐없이 정확히 갖는지 검증한다.
    인자: value -> 검사할 값, expected_keys -> 허용 key 집합, label -> 오류 범주
    반환값: exact key를 가진 mapping
    작성 날짜: 2026/08/29
    """
    if not isinstance(value, Mapping) or set(value) != expected_keys:
        raise VisualRegressionError(f"{label} keys are invalid")
    return value


def _require_capture_geometry(value: object, label: str) -> None:
    """
    함수 이름: _require_capture_geometry()
    기능: Viewport 또는 clip이 고정 1440×1024 DPR1 계약과 일치하는지 검증한다.
    인자: value -> geometry mapping, label -> viewport 또는 clip
    반환값: 없음
    작성 날짜: 2026/08/29
    """
    # Viewport와 clip은 field 집합이 다르므로 label별 exact schema를 먼저 고른다.
    if label == "viewport":
        geometry = _require_exact_keys(
            value,
            frozenset({"width", "height", "device_scale_factor"}),
            label,
        )
        expected_geometry = {
            "width": 1440,
            "height": 1024,
            "device_scale_factor": 1,
        }
    else:
        geometry = _require_exact_keys(
            value,
            frozenset({"x", "y", "width", "height"}),
            label,
        )
        expected_geometry = {  # Clip은 viewport와 달리 좌상단 원점도 고정한다.
            "x": 0,
            "y": 0,
            "width": 1440,
            "height": 1024,
        }
    if dict(geometry) != expected_geometry:
        raise VisualRegressionError(f"{label} geometry is invalid")


def _validate_policy(policy: Mapping[str, object]) -> Decimal:
    """
    함수 이름: _validate_policy()
    기능: Capture, FFmpeg version, SSIM threshold와 anti-alias normalization을 고정한다.
    인자: policy -> comparison policy object
    반환값: SSIM minimum score
    작성 날짜: 2026/08/29
    """
    root = _require_exact_keys(
        policy,
        frozenset({"schema_version", "capture_contract", "comparison_contract"}),
        "comparison policy",
    )
    if root["schema_version"] != 1:
        raise VisualRegressionError("comparison policy schema is unsupported")
    capture = _require_exact_keys(
        root["capture_contract"],
        frozenset(
            {
                "viewport",
                "clip",
                "current_image_format",
                "current_file_extension",
                "automatic_baseline_update",
            }
        ),
        "capture contract",
    )
    _require_capture_geometry(capture["viewport"], "viewport")
    _require_capture_geometry(capture["clip"], "clip")
    if (
        capture["current_image_format"] != "jpeg_jfif"
        or capture["current_file_extension"] != ".jpg"
        or capture["automatic_baseline_update"] is not False
    ):
        raise VisualRegressionError("capture format or baseline update policy is invalid")

    # FFmpeg와 normalization 값을 exact하게 묶어 threshold 완화나 비대칭 filter를 막는다.
    comparison = _require_exact_keys(
        root["comparison_contract"],
        frozenset(
            {
                "tool",
                "required_version_prefix",
                "metric",
                "minimum_score",
                "anti_alias_normalization",
            }
        ),
        "comparison contract",
    )
    normalization = _require_exact_keys(
        comparison["anti_alias_normalization"],
        frozenset(
            {"filter", "sigma", "steps", "pixel_format", "apply_symmetrically"}
        ),
        "anti-alias normalization",
    )
    if (
        comparison["tool"] != "ffmpeg"
        or comparison["required_version_prefix"] != "ffmpeg version 8.0 "
        or comparison["metric"] != "ssim_all"
        or normalization
        != {
            "filter": "gblur",
            "sigma": "0.5",
            "steps": 1,
            "pixel_format": "yuv444p",
            "apply_symmetrically": True,
        }
    ):
        raise VisualRegressionError("comparison tool or normalization drifted")
    try:
        minimum_score = Decimal(str(comparison["minimum_score"]))
    except InvalidOperation as error:
        raise VisualRegressionError("SSIM threshold is invalid") from error
    if minimum_score != Decimal("0.980000"):
        raise VisualRegressionError("SSIM threshold drifted")
    return minimum_score


def _validate_manifests(
    baseline_manifest: Mapping[str, object],
    current_manifest: Mapping[str, object],
) -> tuple[tuple[Mapping[str, object], Mapping[str, object]], ...]:
    """
    함수 이름: _validate_manifests()
    기능: Baseline과 current의 exact 16-frame identity, geometry와 JPEG attestation을 결합한다.
    인자: baseline_manifest -> Figma reference manifest
        current_manifest -> actual browser capture manifest
    반환값: 같은 순서로 결합한 baseline/current frame tuple
    작성 날짜: 2026/08/29
    """
    # 두 manifest의 root schema와 고정 Figma provenance를 frame bytes보다 먼저 검증한다.
    baseline_root = _require_exact_keys(
        baseline_manifest,
        frozenset({"schema_version", "source", "viewport", "frames"}),
        "baseline manifest",
    )
    current_root = _require_exact_keys(
        current_manifest,
        frozenset({"schema_version", "source", "viewport", "clip", "image_format", "frames"}),
        "current manifest",
    )
    if baseline_root["schema_version"] != 1 or current_root["schema_version"] != 1:
        raise VisualRegressionError("visual manifest schema is unsupported")
    baseline_source = _require_exact_keys(
        baseline_root["source"],
        frozenset(EXPECTED_BASELINE_SOURCE),
        "baseline source",
    )
    if dict(baseline_source) != EXPECTED_BASELINE_SOURCE:
        raise VisualRegressionError("baseline source provenance is invalid")
    _require_capture_geometry(baseline_root["viewport"], "viewport")
    _require_capture_geometry(current_root["viewport"], "viewport")
    _require_capture_geometry(current_root["clip"], "clip")
    if current_root["image_format"] != "jpeg_jfif":
        raise VisualRegressionError("current capture format is invalid")
    source = _require_exact_keys(
        current_root["source"],
        frozenset({"kind", "capture_method", "captured_on"}),
        "current capture source",
    )
    if (
        source["kind"] != "actual_browser_capture"
        or source["capture_method"] != "in_app_browser_explicit_clip"
        or not isinstance(source["captured_on"], str)
        or DATE_PATTERN.fullmatch(source["captured_on"]) is None
    ):
        raise VisualRegressionError("current capture source is invalid")
    baseline_frames = baseline_root["frames"]
    current_frames = current_root["frames"]
    if not isinstance(baseline_frames, list) or not isinstance(current_frames, list):
        raise VisualRegressionError("visual frames must be lists")
    if len(baseline_frames) != 16 or len(current_frames) != 16:
        raise VisualRegressionError("visual manifests must contain sixteen frames")

    # Frame key 순서까지 고정해 다른 화면의 높은 score로 실패 화면을 대체하지 못하게 한다.
    paired_frames: list[tuple[Mapping[str, object], Mapping[str, object]]] = []
    for frame_index, expected_frame_key in enumerate(EXPECTED_FRAME_KEYS):
        baseline_frame = _require_exact_keys(
            baseline_frames[frame_index],
            frozenset({"frame_key", "reference_file", "sha256"}),
            "baseline frame",
        )
        current_frame = _require_exact_keys(
            current_frames[frame_index],
            frozenset({"frame_key", "current_file", "sha256"}),
            "current frame",
        )
        if (
            baseline_frame["frame_key"] != expected_frame_key
            or current_frame["frame_key"] != expected_frame_key
        ):
            raise VisualRegressionError("visual frame identity is invalid")
        if not all(
            isinstance(frame["sha256"], str)
            and SHA256_PATTERN.fullmatch(frame["sha256"]) is not None
            for frame in (baseline_frame, current_frame)
        ):
            raise VisualRegressionError("visual frame digest is invalid")
        if (
            not isinstance(baseline_frame["reference_file"], str)
            or not baseline_frame["reference_file"].endswith(".png")
            or not isinstance(current_frame["current_file"], str)
            or not current_frame["current_file"].endswith(".jpg")
            or Path(baseline_frame["reference_file"]).name
            != baseline_frame["reference_file"]
            or Path(current_frame["current_file"]).name != current_frame["current_file"]
        ):
            raise VisualRegressionError("visual frame filename is invalid")
        paired_frames.append((baseline_frame, current_frame))
    return tuple(paired_frames)


def _read_png_dimensions(image_bytes: bytes) -> tuple[int, int]:
    """
    함수 이름: _read_png_dimensions()
    기능: Figma reference가 canonical PNG IHDR와 고정 크기를 갖는지 읽는다.
    인자: image_bytes -> baseline PNG bytes
    반환값: width와 height
    작성 날짜: 2026/08/29
    """
    if (
        len(image_bytes) < 24
        or image_bytes[:8] != PNG_SIGNATURE
        or image_bytes[12:16] != b"IHDR"
    ):
        raise VisualRegressionError("baseline PNG framing is invalid")
    return (
        int.from_bytes(image_bytes[16:20], "big"),
        int.from_bytes(image_bytes[20:24], "big"),
    )


def _read_jpeg_dimensions(image_bytes: bytes) -> tuple[int, int]:
    """
    함수 이름: _read_jpeg_dimensions()
    기능: Current capture가 baseline 8-bit 3-component JPEG/JFIF인지 검증하고 크기를 읽는다.
    인자: image_bytes -> current JPEG bytes
    반환값: width와 height
    작성 날짜: 2026/08/29
    """
    # JPEG/JFIF signature를 먼저 고정해 PNG 또는 다른 container masquerade를 차단한다.
    if (
        len(image_bytes) < 20
        or image_bytes[:4] != b"\xff\xd8\xff\xe0"
        or image_bytes[6:11] != b"JFIF\x00"
    ):
        raise VisualRegressionError("current capture is not JPEG/JFIF")
    # Marker length를 bounded bytes 안에서만 따라가며 baseline SOF0의 precision·component·크기를 읽는다.
    byte_index = 2
    while byte_index + 4 <= len(image_bytes):
        if image_bytes[byte_index] != 0xFF:
            raise VisualRegressionError("current JPEG marker framing is invalid")
        while byte_index < len(image_bytes) and image_bytes[byte_index] == 0xFF:
            byte_index += 1
        if byte_index >= len(image_bytes):
            break
        marker = image_bytes[byte_index]
        byte_index += 1
        if marker in {0xD8, 0xD9}:
            continue
        if byte_index + 2 > len(image_bytes):
            break
        segment_length = int.from_bytes(image_bytes[byte_index : byte_index + 2], "big")
        if segment_length < 2 or byte_index + segment_length > len(image_bytes):
            raise VisualRegressionError("current JPEG segment length is invalid")
        if marker == 0xC0:
            segment = image_bytes[byte_index + 2 : byte_index + segment_length]
            if len(segment) < 6 or segment[0] != 8 or segment[5] != 3:
                raise VisualRegressionError("current JPEG encoding is unsupported")
            height = int.from_bytes(segment[1:3], "big")
            width = int.from_bytes(segment[3:5], "big")
            return width, height  # 최초 baseline SOF0만 canonical dimension 근거로 쓴다.
        if marker == 0xDA:
            break
        byte_index += segment_length
    raise VisualRegressionError("current JPEG baseline dimensions are missing")


def _preflight_ffmpeg(ffmpeg_path: Path, required_version_prefix: str) -> None:
    """
    함수 이름: _preflight_ffmpeg()
    기능: 지정 executable이 고정 FFmpeg version을 보고하는지 확인한다.
    인자: ffmpeg_path -> FFmpeg executable, required_version_prefix -> 고정 first-line prefix
    반환값: 없음
    작성 날짜: 2026/08/29
    """
    # Fixed executable path와 execute bit를 subprocess 실행 전에 검증한다.
    if not ffmpeg_path.is_file() or not ffmpeg_path.stat().st_mode & 0o111:
        raise VisualRegressionError("FFmpeg executable is unavailable")
    # Version command의 stdout 첫 줄만 고정 prefix와 대조하고 raw stderr는 반사하지 않는다.
    try:
        version_result = subprocess.run(
            [str(ffmpeg_path), "-version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise VisualRegressionError("FFmpeg preflight failed") from error
    first_line = (  # Version banner의 첫 줄 외 subprocess 출력은 판정에 사용하지 않는다.
        version_result.stdout.splitlines()[0] if version_result.stdout else ""
    )
    if version_result.returncode != 0 or not first_line.startswith(required_version_prefix):
        raise VisualRegressionError("FFmpeg version is unsupported")


def _measure_ssim(
    ffmpeg_path: Path,
    baseline_path: Path,
    current_path: Path,
) -> Decimal:
    """
    함수 이름: _measure_ssim()
    기능: 양쪽 image에 동일 anti-alias normalization을 적용하고 FFmpeg SSIM All을 읽는다.
    인자: ffmpeg_path -> 검증된 FFmpeg executable
        baseline_path -> Figma PNG, current_path -> browser JPEG
    반환값: 0~1 SSIM Decimal
    작성 날짜: 2026/08/29
    """
    filter_graph = (
        "[0:v]gblur=sigma=0.5:steps=1,format=yuv444p[reference];"
        "[1:v]gblur=sigma=0.5:steps=1,format=yuv444p[current];"
        "[reference][current]ssim"
    )

    # File path는 argv로만 넘기고 FFmpeg stderr는 parser 밖으로 반사하지 않는다.
    try:
        comparison_result = subprocess.run(
            [
                str(ffmpeg_path),
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "info",
                "-i",
                str(baseline_path),
                "-i",
                str(current_path),
                "-lavfi",
                filter_graph,
                "-f",
                "null",
                "-",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise VisualRegressionError("FFmpeg comparison failed") from error
    if comparison_result.returncode != 0:
        raise VisualRegressionError("FFmpeg comparison failed")
    score_matches = SSIM_PATTERN.findall(comparison_result.stderr)
    if len(score_matches) != 1:
        raise VisualRegressionError("FFmpeg SSIM output is invalid")
    try:
        score = Decimal(score_matches[0])
    except InvalidOperation as error:
        raise VisualRegressionError("FFmpeg SSIM score is invalid") from error
    if not Decimal("0") <= score <= Decimal("1"):
        raise VisualRegressionError("FFmpeg SSIM score is out of range")
    return score


def run_visual_regression_gate(
    *,
    repository_root: Path = REPOSITORY_ROOT,
    current_directory: Path | None = None,
    ffmpeg_path: Path | None = None,
) -> tuple[VisualComparisonResult, ...]:
    """
    함수 이름: run_visual_regression_gate()
    기능: 16개 artifact binding과 per-frame SSIM threshold를 read-only로 판정한다.
    인자: repository_root -> repository root
        current_directory -> current JPEG directory 또는 None
        ffmpeg_path -> FFmpeg executable 또는 None
    반환값: frame 순서의 comparison result tuple
    작성 날짜: 2026/08/29
    """
    selected_current_directory = (
        repository_root / DEFAULT_CURRENT_DIRECTORY
        if current_directory is None
        else (
            current_directory
            if current_directory.is_absolute()
            else repository_root / current_directory
        )
    )
    try:
        current_relative_directory = selected_current_directory.relative_to(
            repository_root
        )
    except ValueError as error:
        raise VisualRegressionError(
            "current capture directory must be inside repository"
        ) from error
    policy = _load_json_object(repository_root, COMPARISON_POLICY_PATH)
    baseline_manifest = _load_json_object(repository_root, BASELINE_MANIFEST_PATH)
    current_manifest = _load_json_object(repository_root, CURRENT_MANIFEST_PATH)
    minimum_score = _validate_policy(policy)
    paired_frames = _validate_manifests(baseline_manifest, current_manifest)
    comparison_contract = policy["comparison_contract"]
    assert isinstance(comparison_contract, Mapping)
    required_version_prefix = comparison_contract["required_version_prefix"]
    assert isinstance(required_version_prefix, str)
    selected_ffmpeg = ffmpeg_path
    if selected_ffmpeg is None:
        discovered_ffmpeg = shutil.which("ffmpeg")
        if discovered_ffmpeg is None:
            raise VisualRegressionError("FFmpeg executable is unavailable")
        selected_ffmpeg = Path(discovered_ffmpeg)
    _preflight_ffmpeg(selected_ffmpeg, required_version_prefix)

    # Directory의 누락·추가 파일을 exact set으로 대조해 부분 capture를 성공으로 집계하지 않는다.
    expected_current_files = {
        str(current_frame["current_file"])
        for _, current_frame in paired_frames
    }
    actual_current_files = {
        path.name for path in selected_current_directory.iterdir() if path.is_file()
    }
    if actual_current_files != expected_current_files:
        raise VisualRegressionError("current capture file set is incomplete")

    results: list[VisualComparisonResult] = []
    with TemporaryDirectory(prefix="phase13-visual-verified-") as temporary_name:
        temporary_directory = Path(temporary_name)
        for frame_index, (baseline_frame, current_frame) in enumerate(
            paired_frames,
            start=1,
        ):
            baseline_bytes = _read_bounded_regular_file(
                repository_root,
                BASELINE_DIRECTORY / str(baseline_frame["reference_file"]),
                MAXIMUM_IMAGE_BYTES,
            )
            current_bytes = _read_bounded_regular_file(
                repository_root,
                current_relative_directory
                / Path(str(current_frame["current_file"])),
                MAXIMUM_IMAGE_BYTES,
            )

            # Digest·encoded dimensions을 통과한 snapshot만 owner-only 임시 입력으로 고정한다.
            if hashlib.sha256(baseline_bytes).hexdigest() != baseline_frame["sha256"]:
                raise VisualRegressionError("baseline digest mismatch")
            if hashlib.sha256(current_bytes).hexdigest() != current_frame["sha256"]:
                raise VisualRegressionError("current capture digest mismatch")
            if _read_png_dimensions(baseline_bytes) != (1440, 1024):
                raise VisualRegressionError("baseline dimensions are invalid")
            if _read_jpeg_dimensions(current_bytes) != (1440, 1024):
                raise VisualRegressionError("current capture dimensions are invalid")
            verified_baseline_path = _write_verified_image_copy(
                temporary_directory,
                f"{frame_index:02d}-baseline.png",
                baseline_bytes,
            )
            verified_current_path = _write_verified_image_copy(
                temporary_directory,
                f"{frame_index:02d}-current.jpg",
                current_bytes,
            )
            score = _measure_ssim(
                selected_ffmpeg,
                verified_baseline_path,
                verified_current_path,
            )
            results.append(
                VisualComparisonResult(
                    frame_key=str(baseline_frame["frame_key"]),
                    score=score,
                    passed=score >= minimum_score,
                )
            )
    return tuple(results)


def parse_arguments(argument_values: Sequence[str] | None = None) -> argparse.Namespace:
    """
    함수 이름: parse_arguments()
    기능: Read-only current directory와 FFmpeg override만 받는 CLI를 구성한다.
    인자: argument_values -> 명시 argument 또는 실제 argv를 뜻하는 None
    반환값: argparse namespace
    작성 날짜: 2026/08/29
    """
    # Read-only current directory와 FFmpeg 선택만 노출하고 baseline mutation option은 만들지 않는다.
    parser = argparse.ArgumentParser()
    parser.add_argument("--current-directory", type=Path)
    parser.add_argument("--ffmpeg", type=Path)
    return parser.parse_args(argument_values)


def main(argument_values: Sequence[str] | None = None) -> int:
    """
    함수 이름: main()
    기능: 16개 SSIM을 출력하고 하나라도 threshold 미만이면 NO_GO를 반환한다.
    인자: argument_values -> 명시 argument 또는 실제 argv를 뜻하는 None
    반환값: PASS 0, visual mismatch 1, invalid/missing evidence 2
    작성 날짜: 2026/08/29
    """
    arguments = parse_arguments(argument_values)
    try:
        results = run_visual_regression_gate(
            current_directory=arguments.current_directory,
            ffmpeg_path=arguments.ffmpeg,
        )
    except (OSError, VisualRegressionError, ValueError):
        print("visual-regression: ERROR: evidence rejected.", file=sys.stderr)
        return 2

    # 각 frame 점수를 공개해 평균값이 낮은 화면을 숨기지 않도록 한다.
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"visual-regression: {status}: {result.frame_key}={result.score}")
    failed_count = sum(not result.passed for result in results)
    if failed_count:
        print(
            "visual-regression: NO_GO: "
            f"failed={failed_count}/16 threshold=0.980000.",
            file=sys.stderr,
        )
        return 1
    print("visual-regression: PASS: 16/16 threshold=0.980000.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
