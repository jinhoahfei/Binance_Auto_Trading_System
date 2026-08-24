#!/usr/bin/env python3
"""Phase 12 signed app과 선택적 DMG의 release 계약을 fail-closed 검증한다."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import selectors
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
from typing import Callable, Iterator, Mapping, Sequence
import zlib


COMMAND_TIMEOUT_SECONDS = 60
LOADER_SMOKE_TIMEOUT_SECONDS = 10
MAXIMUM_INFO_PLIST_BYTES = 1024 * 1024
MAXIMUM_COMMAND_OUTPUT_BYTES = 1024 * 1024
MAXIMUM_ARCHIVE_TOC_BYTES = 16 * 1024 * 1024
MAXIMUM_NATIVE_PAYLOAD_BYTES = 256 * 1024 * 1024
MAXIMUM_NATIVE_PAYLOAD_COUNT = 4096
MAXIMUM_MAIN_EXECUTABLE_BYTES = 512 * 1024 * 1024
MAXIMUM_PROVENANCE_BYTES = 64 * 1024
REQUIRED_MINIMUM_MACOS_VERSION = "11.0"
EXPECTED_SIDECAR_NAME = "binance-auto-sidecar"
TEAM_IDENTIFIER_PATTERN = re.compile(r"[A-Z0-9]{10}\Z")
RELEASE_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
RELEASE_COMMIT_MARKER_PREFIX = b"BINANCE_AUTO_RELEASE_COMMIT="
EXPECTED_IDENTITY_PATTERN = re.compile(
    r'Developer ID Application: ([^"\r\n]{1,200}) \(([A-Z0-9]{10})\)\Z'
)
SEMANTIC_VERSION_PATTERN = re.compile(
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z"
)
BUNDLE_VERSION_PATTERN = re.compile(
    r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){0,2}\Z"
)
BUNDLE_EXECUTABLE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}\Z")
RUNTIME_FLAGS_PATTERN = re.compile(
    rb"\bflags=0x[0-9A-Fa-f]+\(([^)\r\n]*)\)"
)
ARM64_PATTERN = re.compile(rb"(?<![A-Za-z0-9_])arm64(?![A-Za-z0-9_])")
MINIMUM_OS_PATTERN = re.compile(rb"^\s*minos\s+(\S+)\s*$", re.MULTILINE)
PLATFORM_PATTERN = re.compile(rb"^\s*platform\s+(\S+)\s*$", re.MULTILINE)
FORBIDDEN_LIBRARY_VALIDATION_ENTITLEMENT = (
    b"com.apple.security.cs.disable-library-validation"
)
EXPECTED_AUTHORITY_INTERMEDIATES = {
    b"Developer ID Certification Authority",
    b"Developer ID Certification Authority G2",
}
EXPECTED_AUTHORITY_ROOT = b"Apple Root CA"
PYINSTALLER_COOKIE_MAGIC = b"MEI\014\013\012\013\016"
PYINSTALLER_COOKIE_FORMAT = "!8sIIII64s"
PYINSTALLER_COOKIE_LENGTH = struct.calcsize(PYINSTALLER_COOKIE_FORMAT)
PYINSTALLER_TOC_ENTRY_FORMAT = "!IIIIBc"
PYINSTALLER_TOC_ENTRY_LENGTH = struct.calcsize(PYINSTALLER_TOC_ENTRY_FORMAT)
PYINSTALLER_PROVENANCE_NAME = "phase12-release-provenance.json"
DMG_DEVICE_PATTERN = re.compile(r"/dev/disk[0-9]+(?:s[0-9]+)*\Z")
TRUSTED_TEMPORARY_ROOT = Path("/private/tmp")

# Release verifier child에는 Apple certificate와 notarization account 원문을 상속하지 않는다.
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
CONTROL_ENVIRONMENT_NAMES = {
    "__PYVENV_LAUNCHER__",
    "APPLE_SIGNING_IDENTITY",
    "COMMAND_MODE",
    "DEVELOPER_DIR",
    "LD_PRELOAD",
    "NOTARY_PROFILE",
    "SDKROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
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
CANONICAL_SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

# FD smoke는 application contract에 도달한 명시적 EBADF만 정상 loader 도달 증거로 인정한다.
FD_CONTRACT_FAILURE_MARKERS = (
    b"bad file descriptor",
    b"[errno 9]",
    b"errno 9",
    b"ebadf",
)
FORBIDDEN_LOADER_ERROR_MARKERS = (
    b"dyld:",
    b"libpython",
    b"library not loaded",
    b"library validation",
    b"code signature",
    b"code-signature",
    b"code signing",
    b"different team id",
    b"different team identifier",
    b"mapped file has no cdhash",
    b"not valid for use in process",
    b"terminated due to code signing error",
)


class SignedArtifactVerificationError(RuntimeError):
    """
    이름: SignedArtifactVerificationError
    기능: raw path나 command output을 포함하지 않는 signed artifact 검증 실패를 나타낸다.
    작성 날짜: 2026/08/24
    """


class GenericArgumentParser(argparse.ArgumentParser):
    """
    클래스 이름: GenericArgumentParser
    기능: 잘못된 command line 값을 반사하지 않고 고정 오류만 출력한다.
    작성 날짜: 2026/08/24
    """

    def error(self, message: str) -> None:
        """
        함수 이름: error()
        기능: argparse 상세 대신 path·Team ID 비반사 오류로 종료한다.
        인자: message -> 출력하지 않을 argparse 상세
        반환값: 정상 반환 없이 SystemExit 발생
        작성 날짜: 2026/08/24
        """

        del message
        self.exit(
            2,
            "phase12-signed-artifacts: ERROR: invalid arguments.\n",
        )


@dataclass(frozen=True)
class AppBundleArtifacts:
    """
    클래스 이름: AppBundleArtifacts
    기능: 구조와 plist 검증을 통과한 app, main executable과 sidecar path를 묶는다.
    작성 날짜: 2026/08/24
    """

    app_path: Path
    main_executable_path: Path
    sidecar_path: Path
    code_resources_path: Path
    bundle_identifier: str
    release_version: str
    build_version: str


@dataclass(frozen=True)
class NativePayloadArtifact:
    """격리 추출한 PyInstaller native payload와 종류를 묶는다."""

    payload_path: Path
    is_python_library: bool
    sha256: str


@dataclass(frozen=True)
class ExtractedNativeEvidence:
    """manifest와 exact 대조 가능한 non-secret native payload evidence다."""

    kind: str
    sha256: str


@dataclass(frozen=True)
class PyInstallerArchiveArtifacts:
    """격리 추출 native와 검증된 archive provenance digest를 묶는다."""

    native_payloads: tuple[NativePayloadArtifact, ...]
    provenance_sha256: str


@dataclass(frozen=True)
class ArtifactFileSnapshot:
    """path-free file identity를 gate pre/post evidence와 결합한다."""

    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class SignedArtifactEvidence:
    """
    클래스 이름: SignedArtifactEvidence
    기능: path 없이 검증된 Team ID와 선택적 DMG 검증 여부만 보존한다.
    작성 날짜: 2026/08/24
    """

    team_id: str
    dmg_verified: bool
    extracted_native: tuple[ExtractedNativeEvidence, ...]
    commit: str
    provenance_sha256: str
    dmg_sha256: str | None
    dmg_snapshot: ArtifactFileSnapshot | None


CommandRunner = Callable[
    [Sequence[str], Path, Mapping[str, str], float],
    subprocess.CompletedProcess[bytes],
]


def run_command(
    command: Sequence[str],
    working_directory: Path,
    process_environment: Mapping[str, str],
    timeout_seconds: float,
) -> subprocess.CompletedProcess[bytes]:
    """
    함수 이름: run_command()
    기능: shell과 inherited FD 없이 bounded verifier command를 실행해 출력을 포착한다.
    인자: command -> executable과 인자의 고정 목록
        working_directory -> child process working directory
        process_environment -> Apple secret이 제거된 environment
        timeout_seconds -> command별 최대 실행 시간
    반환값: captured subprocess 결과
    작성 날짜: 2026/08/24
    """

    process: subprocess.Popen[bytes] | None = None
    output_selector = selectors.DefaultSelector()
    captured_streams = {"stdout": bytearray(), "stderr": bytearray()}
    total_output_bytes = 0
    deadline = time.monotonic() + timeout_seconds

    def terminate_process_group() -> None:
        """새 session 전체를 종료해 loader descendant orphan을 남기지 않는다."""

        if process is None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            process.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass

    try:
        process = subprocess.Popen(
            list(command),
            cwd=working_directory,
            env=dict(process_environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            start_new_session=True,
        )
        if process.stdout is None or process.stderr is None:
            raise SignedArtifactVerificationError(
                "signed artifact verification command failed"
            )
        output_selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        output_selector.register(process.stderr, selectors.EVENT_READ, "stderr")

        while output_selector.get_map():
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                terminate_process_group()
                raise SignedArtifactVerificationError(
                    "signed artifact verification command timed out"
                )
            ready_streams = output_selector.select(remaining_seconds)
            if not ready_streams:
                continue
            for selector_key, _ in ready_streams:
                try:
                    output_chunk = os.read(selector_key.fileobj.fileno(), 65536)
                except OSError:
                    terminate_process_group()
                    raise SignedArtifactVerificationError(
                        "signed artifact verification command failed"
                    ) from None
                if not output_chunk:
                    output_selector.unregister(selector_key.fileobj)
                    selector_key.fileobj.close()
                    continue
                total_output_bytes += len(output_chunk)
                if total_output_bytes > MAXIMUM_COMMAND_OUTPUT_BYTES:
                    terminate_process_group()
                    raise SignedArtifactVerificationError(
                        "signed artifact verification output exceeded limit"
                    )
                captured_streams[selector_key.data].extend(output_chunk)

        # Pipe를 닫고 먼저 종료한 leader 뒤에서 descendant만 살아 있는 경우도
        # leader를 reap하기 전에 exact PGID를 kill한다. Zombie PID는 이 시점에
        # 재사용되지 않으므로 unrelated process-group PID reuse race를 만들지 않는다.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            terminate_process_group()
            raise SignedArtifactVerificationError(
                "signed artifact verification command timed out"
            )
        try:
            return_code = process.wait(timeout=remaining_seconds)
        except subprocess.TimeoutExpired:
            terminate_process_group()
            raise SignedArtifactVerificationError(
                "signed artifact verification command timed out"
            ) from None
        return subprocess.CompletedProcess(
            list(command),
            return_code,
            bytes(captured_streams["stdout"]),
            bytes(captured_streams["stderr"]),
        )
    except SignedArtifactVerificationError:
        terminate_process_group()
        raise
    except (OSError, subprocess.SubprocessError, ValueError):
        terminate_process_group()
        raise SignedArtifactVerificationError(
            "signed artifact verification command failed"
        ) from None
    finally:
        output_selector.close()
        if process is not None:
            for output_stream in (process.stdout, process.stderr):
                if output_stream is not None and not output_stream.closed:
                    output_stream.close()


def create_sanitized_environment(
    source_environment: Mapping[str, str],
) -> dict[str, str]:
    """
    함수 이름: create_sanitized_environment()
    기능: Apple release secret과 Binance credential을 verifier child에 상속하지 않는다.
    인자: source_environment -> verifier caller environment
    반환값: 알려진 Apple secret 변수가 제거된 environment 사본
    작성 날짜: 2026/08/24
    """

    process_environment = dict(source_environment)
    for environment_name in tuple(process_environment):
        normalized_name = environment_name.upper()
        is_named_secret = normalized_name in SENSITIVE_APPLE_ENVIRONMENT_NAMES
        is_future_apple_secret = (
            normalized_name.startswith(("APPLE_", "ASC_"))
            and any(
                marker in normalized_name
                for marker in SENSITIVE_APPLE_ENVIRONMENT_MARKERS
            )
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
            is_named_secret
            or is_future_apple_secret
            or is_named_binance_secret
            or is_future_binance_secret
        ):
            process_environment.pop(environment_name, None)
            continue
        if normalized_name in CONTROL_ENVIRONMENT_NAMES or normalized_name.startswith(
            CONTROL_ENVIRONMENT_PREFIXES
        ):
            process_environment.pop(environment_name, None)
    process_environment["PATH"] = CANONICAL_SYSTEM_PATH
    process_environment["LANG"] = "C"
    process_environment["LC_ALL"] = "C"
    return process_environment


def validate_expected_team_id(expected_team_id: str) -> str:
    """
    함수 이름: validate_expected_team_id()
    기능: 비교 기준 Team ID가 exact 10-byte Apple identifier인지 검증한다.
    인자: expected_team_id -> release identity preflight가 확인한 non-secret Team ID
    반환값: 검증된 Team ID
    작성 날짜: 2026/08/24
    """

    if not isinstance(expected_team_id, str) or (
        TEAM_IDENTIFIER_PATTERN.fullmatch(expected_team_id) is None
    ):
        raise SignedArtifactVerificationError(
            "expected release Team ID is invalid"
        )
    return expected_team_id


def validate_expected_identity(
    expected_identity: str,
    expected_team_id: str,
) -> str:
    """Developer ID Application identity와 Team ID 결합을 exact 검증한다."""

    if not isinstance(expected_identity, str):
        raise SignedArtifactVerificationError(
            "expected release signing identity is invalid"
        )
    identity_match = EXPECTED_IDENTITY_PATTERN.fullmatch(expected_identity)
    if (
        identity_match is None
        or identity_match.group(1).strip() != identity_match.group(1)
        or identity_match.group(2) != expected_team_id
    ):
        raise SignedArtifactVerificationError(
            "expected release signing identity is invalid"
        )
    return expected_identity


def validate_expected_versions(
    expected_version: str,
    expected_build_version: str,
) -> tuple[str, str]:
    """명시 release SemVer와 canonical nonzero CFBundleVersion을 검증한다."""

    if (
        not isinstance(expected_version, str)
        or SEMANTIC_VERSION_PATTERN.fullmatch(expected_version) is None
        or not isinstance(expected_build_version, str)
        or BUNDLE_VERSION_PATTERN.fullmatch(expected_build_version) is None
        or not any(
            component != "0" for component in expected_build_version.split(".")
        )
    ):
        raise SignedArtifactVerificationError(
            "expected release version configuration is invalid"
        )
    return expected_version, expected_build_version


def validate_expected_commit(expected_commit: str) -> str:
    """manifest와 build marker를 묶는 exact lowercase Git commit을 검증한다."""

    if (
        not isinstance(expected_commit, str)
        or RELEASE_COMMIT_PATTERN.fullmatch(expected_commit) is None
    ):
        raise SignedArtifactVerificationError(
            "expected release commit is invalid"
        )
    return expected_commit


def _read_entry_status(entry_path: Path) -> os.stat_result:
    """
    함수 이름: _read_entry_status()
    기능: symlink를 따라가지 않고 exact bundle entry metadata를 읽는다.
    인자: entry_path -> 검사할 app 또는 embedded entry
    반환값: lstat 결과
    작성 날짜: 2026/08/24
    """

    try:
        entry_status = os.lstat(entry_path)
    except (OSError, ValueError):
        raise SignedArtifactVerificationError(
            "signed artifact structure is invalid"
        ) from None
    if stat.S_ISLNK(entry_status.st_mode):
        raise SignedArtifactVerificationError(
            "signed artifact structure is invalid"
        )
    return entry_status


def _require_directory(directory_path: Path) -> None:
    """
    함수 이름: _require_directory()
    기능: exact path가 존재하는 non-symlink directory인지 검증한다.
    인자: directory_path -> 검사할 directory
    반환값: 없음
    작성 날짜: 2026/08/24
    """

    directory_status = _read_entry_status(directory_path)
    if not stat.S_ISDIR(directory_status.st_mode):
        raise SignedArtifactVerificationError(
            "signed artifact structure is invalid"
        )


def _require_physical_absolute_path(artifact_path: Path) -> None:
    """artifact leaf까지 모든 existing component가 non-symlink physical path인지 확인한다."""

    if not artifact_path.is_absolute():
        raise SignedArtifactVerificationError(
            "signed artifact path is invalid"
        )
    current_path = Path(artifact_path.anchor)
    try:
        for path_component in artifact_path.parts[1:]:
            current_path /= path_component
            if stat.S_ISLNK(os.lstat(current_path).st_mode):
                raise SignedArtifactVerificationError(
                    "signed artifact path contains a symlink"
                )
    except SignedArtifactVerificationError:
        raise
    except (OSError, ValueError):
        raise SignedArtifactVerificationError(
            "signed artifact path is invalid"
        ) from None


def _require_regular_file(file_path: Path, *, executable: bool = False) -> None:
    """
    함수 이름: _require_regular_file()
    기능: exact path가 non-symlink regular file이고 필요하면 executable mode인지 검증한다.
    인자: file_path -> 검사할 file
        executable -> 하나 이상의 execute bit를 요구할지 여부
    반환값: 없음
    작성 날짜: 2026/08/24
    """

    file_status = _read_entry_status(file_path)
    if not stat.S_ISREG(file_status.st_mode):
        raise SignedArtifactVerificationError(
            "signed artifact structure is invalid"
        )
    execute_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    if executable and not file_status.st_mode & execute_bits:
        raise SignedArtifactVerificationError(
            "signed artifact executable structure is invalid"
        )


def _create_isolated_temporary_directory(prefix: str) -> Path:
    """caller TMPDIR를 무시하고 sticky system temp 아래 mode 0700 directory를 만든다."""

    temporary_path: Path | None = None
    try:
        root_status = os.lstat(TRUSTED_TEMPORARY_ROOT)
        if (
            not stat.S_ISDIR(root_status.st_mode)
            or stat.S_ISLNK(root_status.st_mode)
            or root_status.st_uid != 0
            or not root_status.st_mode & stat.S_ISVTX
        ):
            raise SignedArtifactVerificationError(
                "trusted temporary root is invalid"
            )
        temporary_path = Path(
            tempfile.mkdtemp(
                prefix=prefix,
                dir=str(TRUSTED_TEMPORARY_ROOT),
            )
        )
        os.chmod(temporary_path, 0o700)
        temporary_status = os.lstat(temporary_path)
        if (
            not stat.S_ISDIR(temporary_status.st_mode)
            or stat.S_ISLNK(temporary_status.st_mode)
            or temporary_status.st_uid != os.geteuid()
            or stat.S_IMODE(temporary_status.st_mode) != 0o700
        ):
            raise SignedArtifactVerificationError(
                "isolated temporary directory is invalid"
            )
        return temporary_path
    except SignedArtifactVerificationError:
        if temporary_path is not None:
            try:
                os.rmdir(temporary_path)
            except OSError:
                pass
        raise
    except (OSError, ValueError):
        if temporary_path is not None:
            try:
                os.rmdir(temporary_path)
            except OSError:
                pass
        raise SignedArtifactVerificationError(
            "isolated temporary directory could not be created"
        ) from None


def _cleanup_native_extraction_directory(
    extraction_directory: Path,
    initial_status: os.stat_result,
) -> None:
    """owned native extraction files만 unlink하고 directory identity swap을 거부한다."""

    try:
        if (
            extraction_directory.parent != TRUSTED_TEMPORARY_ROOT
            or _stable_directory_identity(os.lstat(extraction_directory))
            != _stable_directory_identity(initial_status)
        ):
            raise SignedArtifactVerificationError(
                "native extraction cleanup identity is invalid"
            )
        with os.scandir(extraction_directory) as extraction_entries:
            entry_names = sorted(entry.name for entry in extraction_entries)
        for entry_name in entry_names:
            if re.fullmatch(r"native-[0-9]{4}\.(?:dylib|so)", entry_name) is None:
                raise SignedArtifactVerificationError(
                    "native extraction cleanup contents are invalid"
                )
            entry_path = extraction_directory / entry_name
            entry_status = os.lstat(entry_path)
            if not stat.S_ISREG(entry_status.st_mode):
                raise SignedArtifactVerificationError(
                    "native extraction cleanup contents are invalid"
                )
            os.unlink(entry_path)
        if (
            _stable_directory_identity(os.lstat(extraction_directory))
            != _stable_directory_identity(initial_status)
        ):
            raise SignedArtifactVerificationError(
                "native extraction cleanup identity is invalid"
            )
        os.rmdir(extraction_directory)
    except SignedArtifactVerificationError:
        raise
    except (OSError, ValueError):
        raise SignedArtifactVerificationError(
            "native extraction cleanup failed"
        ) from None


@contextmanager
def _isolated_native_extraction_directory() -> Iterator[Path]:
    """trusted temp 아래 native extraction directory를 identity-safe 관리한다."""

    extraction_directory = _create_isolated_temporary_directory(
        "phase12-native-payloads-"
    )
    initial_status = os.lstat(extraction_directory)
    try:
        yield extraction_directory
    finally:
        _cleanup_native_extraction_directory(
            extraction_directory,
            initial_status,
        )


def _stable_stat_identity(file_status: os.stat_result) -> tuple[int, ...]:
    """read 자체로 변할 수 있는 atime을 제외한 TOCTOU identity를 반환한다."""

    return (
        file_status.st_dev,
        file_status.st_ino,
        file_status.st_mode,
        file_status.st_nlink,
        file_status.st_uid,
        file_status.st_gid,
        file_status.st_size,
        file_status.st_mtime_ns,
        file_status.st_ctime_ns,
    )


def _stable_directory_identity(
    directory_status: os.stat_result,
) -> tuple[int, ...]:
    """owned temp directory content 변화와 무관한 inode identity를 반환한다."""

    return (
        directory_status.st_dev,
        directory_status.st_ino,
        directory_status.st_mode,
        directory_status.st_uid,
        directory_status.st_gid,
    )


def _load_info_plist(info_plist_path: Path) -> dict[str, object]:
    """
    함수 이름: _load_info_plist()
    기능: bounded non-symlink Info.plist를 strict top-level dictionary로 읽는다.
    인자: info_plist_path -> app Contents/Info.plist path
    반환값: parsed plist dictionary
    작성 날짜: 2026/08/24
    """

    info_plist_descriptor: int | None = None
    try:
        info_plist_descriptor = os.open(
            info_plist_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        info_plist_status = os.fstat(info_plist_descriptor)
        if (
            not stat.S_ISREG(info_plist_status.st_mode)
            or info_plist_status.st_size <= 0
            or info_plist_status.st_size > MAXIMUM_INFO_PLIST_BYTES
        ):
            raise SignedArtifactVerificationError(
                "signed app Info.plist is invalid"
            )
        plist_bytes = bytearray()
        while len(plist_bytes) <= MAXIMUM_INFO_PLIST_BYTES:
            plist_chunk = os.read(info_plist_descriptor, 65536)
            if not plist_chunk:
                break
            plist_bytes.extend(plist_chunk)
        if len(plist_bytes) != info_plist_status.st_size:
            raise SignedArtifactVerificationError(
                "signed app Info.plist is invalid"
            )
        if _stable_stat_identity(os.fstat(info_plist_descriptor)) != (
            _stable_stat_identity(info_plist_status)
        ):
            raise SignedArtifactVerificationError(
                "signed app Info.plist changed during verification"
            )
        parsed_plist = plistlib.loads(bytes(plist_bytes))
    except SignedArtifactVerificationError:
        raise
    except (OSError, ValueError, TypeError, plistlib.InvalidFileException):
        raise SignedArtifactVerificationError(
            "signed app Info.plist is invalid"
        ) from None
    finally:
        if info_plist_descriptor is not None:
            os.close(info_plist_descriptor)
    if not isinstance(parsed_plist, dict):
        raise SignedArtifactVerificationError(
            "signed app Info.plist is invalid"
        )
    return parsed_plist


def inspect_app_bundle(
    app_path: Path,
    expected_version: str,
    expected_build_version: str,
) -> AppBundleArtifacts:
    """
    함수 이름: inspect_app_bundle()
    기능: signed app의 suffix, directory, plist, main과 fixed-name sidecar 구조를 검증한다.
    인자: app_path -> exact release candidate .app path
    반환값: 검증된 embedded artifact path 묶음
    작성 날짜: 2026/08/24
    """

    if app_path.name == ".app" or not app_path.name.endswith(".app"):
        raise SignedArtifactVerificationError("signed app path is invalid")
    _require_directory(app_path)

    contents_path = app_path / "Contents"
    macos_path = contents_path / "MacOS"
    info_plist_path = contents_path / "Info.plist"
    code_resources_path = contents_path / "_CodeSignature" / "CodeResources"
    _require_directory(contents_path)
    _require_directory(macos_path)
    _require_directory(contents_path / "_CodeSignature")
    _require_regular_file(code_resources_path)

    parsed_plist = _load_info_plist(info_plist_path)
    bundle_executable = parsed_plist.get("CFBundleExecutable")
    if (
        not isinstance(bundle_executable, str)
        or BUNDLE_EXECUTABLE_PATTERN.fullmatch(bundle_executable) is None
        or bundle_executable in {".", ".."}
        or parsed_plist.get("CFBundlePackageType") != "APPL"
    ):
        raise SignedArtifactVerificationError(
            "signed app bundle metadata is invalid"
        )
    if parsed_plist.get("LSMinimumSystemVersion") != REQUIRED_MINIMUM_MACOS_VERSION:
        raise SignedArtifactVerificationError(
            "signed app minimum macOS version is invalid"
        )
    bundle_identifier = parsed_plist.get("CFBundleIdentifier")
    release_version = parsed_plist.get("CFBundleShortVersionString")
    build_version = parsed_plist.get("CFBundleVersion")
    if (
        not isinstance(bundle_identifier, str)
        or not bundle_identifier
        or release_version != expected_version
        or build_version != expected_build_version
    ):
        raise SignedArtifactVerificationError(
            "signed app release metadata is invalid"
        )

    main_executable_path = macos_path / bundle_executable
    sidecar_path = macos_path / EXPECTED_SIDECAR_NAME
    if main_executable_path == sidecar_path:
        raise SignedArtifactVerificationError(
            "signed app executable layout is invalid"
        )
    _require_regular_file(main_executable_path, executable=True)
    _require_regular_file(sidecar_path, executable=True)

    return AppBundleArtifacts(
        app_path=app_path,
        main_executable_path=main_executable_path,
        sidecar_path=sidecar_path,
        code_resources_path=code_resources_path,
        bundle_identifier=bundle_identifier,
        release_version=release_version,
        build_version=build_version,
    )


def inspect_dmg_path(dmg_path: Path) -> None:
    """
    함수 이름: inspect_dmg_path()
    기능: 선택적 release DMG가 .dmg suffix의 non-symlink regular file인지 검증한다.
    인자: dmg_path -> exact final DMG path
    반환값: 없음
    작성 날짜: 2026/08/24
    """

    if dmg_path.name == ".dmg" or not dmg_path.name.endswith(".dmg"):
        raise SignedArtifactVerificationError("signed DMG path is invalid")
    _require_regular_file(dmg_path)


def calculate_regular_file_sha256(file_path: Path) -> str:
    """non-symlink regular file을 single-FD로 읽어 stable SHA-256을 계산한다."""

    file_descriptor: int | None = None
    try:
        file_descriptor = os.open(
            file_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        initial_status = os.fstat(file_descriptor)
        if not stat.S_ISREG(initial_status.st_mode):
            raise SignedArtifactVerificationError(
                "signed artifact file is invalid"
            )
        digest = hashlib.sha256()
        bytes_read = 0
        while True:
            file_chunk = os.read(file_descriptor, 1024 * 1024)
            if not file_chunk:
                break
            bytes_read += len(file_chunk)
            digest.update(file_chunk)
        final_status = os.fstat(file_descriptor)
        if (
            bytes_read != initial_status.st_size
            or _stable_stat_identity(initial_status)
            != _stable_stat_identity(final_status)
        ):
            raise SignedArtifactVerificationError(
                "signed artifact changed during verification"
            )
        return digest.hexdigest()
    except SignedArtifactVerificationError:
        raise
    except (OSError, ValueError):
        raise SignedArtifactVerificationError(
            "signed artifact file could not be read"
        ) from None
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)


def _calculate_open_file_sha256(
    file_descriptor: int,
    expected_status: os.stat_result,
) -> str:
    """caller가 계속 소유하는 descriptor를 pread해 digest와 stable stat을 확인한다."""

    digest = hashlib.sha256()
    bytes_read = 0
    while bytes_read < expected_status.st_size:
        try:
            file_chunk = os.pread(
                file_descriptor,
                min(1024 * 1024, expected_status.st_size - bytes_read),
                bytes_read,
            )
        except OSError:
            raise SignedArtifactVerificationError(
                "open release artifact could not be read"
            ) from None
        if not file_chunk:
            break
        bytes_read += len(file_chunk)
        digest.update(file_chunk)
    if (
        bytes_read != expected_status.st_size
        or _stable_stat_identity(os.fstat(file_descriptor))
        != _stable_stat_identity(expected_status)
    ):
        raise SignedArtifactVerificationError(
            "open release artifact changed during verification"
        )
    return digest.hexdigest()


def _open_release_artifact(
    artifact_path: Path,
) -> tuple[int, os.stat_result, ArtifactFileSnapshot, str]:
    """DMG를 O_NOFOLLOW single FD로 열어 snapshot과 initial digest를 고정한다."""

    file_descriptor: int | None = None
    try:
        file_descriptor = os.open(
            artifact_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        file_status = os.fstat(file_descriptor)
        path_status = os.lstat(artifact_path)
        if (
            not stat.S_ISREG(file_status.st_mode)
            or _stable_stat_identity(file_status)
            != _stable_stat_identity(path_status)
        ):
            raise SignedArtifactVerificationError(
                "release artifact identity is invalid"
            )
        snapshot = ArtifactFileSnapshot(
            device=file_status.st_dev,
            inode=file_status.st_ino,
            size=file_status.st_size,
            mtime_ns=file_status.st_mtime_ns,
            ctime_ns=file_status.st_ctime_ns,
        )
        digest = _calculate_open_file_sha256(file_descriptor, file_status)
        returned_descriptor = file_descriptor
        file_descriptor = None
        return returned_descriptor, file_status, snapshot, digest
    except SignedArtifactVerificationError:
        raise
    except (OSError, ValueError):
        raise SignedArtifactVerificationError(
            "release artifact could not be opened"
        ) from None
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)


def _verify_open_release_artifact_unchanged(
    artifact_path: Path,
    file_descriptor: int,
    initial_status: os.stat_result,
    initial_digest: str,
) -> None:
    """same open FD와 current path lstat/digest가 initial DMG에 계속 결합됐는지 확인한다."""

    try:
        path_status = os.lstat(artifact_path)
    except OSError:
        raise SignedArtifactVerificationError(
            "release artifact path changed during verification"
        ) from None
    if (
        _stable_stat_identity(path_status) != _stable_stat_identity(initial_status)
        or _calculate_open_file_sha256(file_descriptor, initial_status)
        != initial_digest
    ):
        raise SignedArtifactVerificationError(
            "release artifact path changed during verification"
        )


def verify_release_commit_marker(
    main_executable_path: Path,
    expected_commit: str,
) -> None:
    """main binary를 streaming scan해 expected commit marker 하나만 허용한다."""

    file_descriptor: int | None = None
    try:
        file_descriptor = os.open(
            main_executable_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        initial_status = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(initial_status.st_mode)
            or initial_status.st_size <= 0
            or initial_status.st_size > MAXIMUM_MAIN_EXECUTABLE_BYTES
        ):
            raise SignedArtifactVerificationError(
                "release main executable size is invalid"
            )
        expected_marker = RELEASE_COMMIT_MARKER_PREFIX + expected_commit.encode(
            "ascii"
        )
        unverified_marker = RELEASE_COMMIT_MARKER_PREFIX + b"UNVERIFIED"
        needles = (
            RELEASE_COMMIT_MARKER_PREFIX,
            expected_marker,
            unverified_marker,
        )
        marker_counts = {needle: 0 for needle in needles}
        maximum_needle_length = max(len(needle) for needle in needles)
        carry = b""
        bytes_read = 0

        while True:
            file_chunk = os.read(file_descriptor, 1024 * 1024)
            bytes_read += len(file_chunk)
            combined_bytes = carry + file_chunk
            if file_chunk:
                safe_start_limit = max(
                    0,
                    len(combined_bytes) - maximum_needle_length + 1,
                )
            else:
                safe_start_limit = len(combined_bytes)
            for needle in needles:
                search_position = 0
                while True:
                    match_position = combined_bytes.find(
                        needle,
                        search_position,
                    )
                    if match_position < 0 or match_position >= safe_start_limit:
                        break
                    marker_counts[needle] += 1
                    search_position = match_position + 1
            carry = combined_bytes[safe_start_limit:]
            if not file_chunk:
                break

        if (
            bytes_read != initial_status.st_size
            or marker_counts[RELEASE_COMMIT_MARKER_PREFIX] != 1
            or marker_counts[expected_marker] != 1
            or marker_counts[unverified_marker] != 0
            or _stable_stat_identity(os.fstat(file_descriptor))
            != _stable_stat_identity(initial_status)
        ):
            raise SignedArtifactVerificationError(
                "release commit marker is invalid"
            )
    except SignedArtifactVerificationError:
        raise
    except (OSError, ValueError):
        raise SignedArtifactVerificationError(
            "release commit marker could not be verified"
        ) from None
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)


def _invoke_command(
    command: Sequence[str],
    working_directory: Path,
    process_environment: Mapping[str, str],
    command_runner: CommandRunner,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[bytes]:
    """
    함수 이름: _invoke_command()
    기능: injected runner도 timeout·OS failure를 raw detail 없이 동일하게 fail-closed 처리한다.
    인자: command -> 실행할 argv 목록
        working_directory -> child working directory
        process_environment -> sanitized environment
        command_runner -> production 또는 test subprocess 경계
        timeout_seconds -> 최대 실행 시간
    반환값: captured command 결과
    작성 날짜: 2026/08/24
    """

    try:
        process_result = command_runner(
            list(command),
            working_directory,
            process_environment,
            timeout_seconds,
        )
    except SignedArtifactVerificationError:
        raise
    except subprocess.TimeoutExpired as error:
        error.output = None
        error.stderr = None
        raise SignedArtifactVerificationError(
            "signed artifact verification command timed out"
        ) from None
    except (OSError, subprocess.SubprocessError):
        raise SignedArtifactVerificationError(
            "signed artifact verification command failed"
        ) from None

    if not isinstance(process_result, subprocess.CompletedProcess) or not isinstance(
        process_result.returncode,
        int,
    ):
        raise SignedArtifactVerificationError(
            "signed artifact verification command result is invalid"
        )
    return process_result


def _run_checked_command(
    command: Sequence[str],
    working_directory: Path,
    process_environment: Mapping[str, str],
    command_runner: CommandRunner,
) -> subprocess.CompletedProcess[bytes]:
    """
    함수 이름: _run_checked_command()
    기능: verifier command가 timeout 안에 exit 0을 반환했는지 확인한다.
    인자: command -> 실행할 argv 목록
        working_directory -> child working directory
        process_environment -> sanitized environment
        command_runner -> subprocess 실행 경계
    반환값: 성공 captured command 결과
    작성 날짜: 2026/08/24
    """

    process_result = _invoke_command(
        command,
        working_directory,
        process_environment,
        command_runner,
        COMMAND_TIMEOUT_SECONDS,
    )
    if process_result.returncode != 0:
        raise SignedArtifactVerificationError(
            "signed artifact verification command was rejected"
        )
    return process_result


def _read_captured_output(
    process_result: subprocess.CompletedProcess[bytes],
) -> bytes:
    """
    함수 이름: _read_captured_output()
    기능: stdout과 stderr를 log에 출력하지 않고 metadata parser용 bytes로 결합한다.
    인자: process_result -> 성공하거나 expected failure인 captured command 결과
    반환값: 구분 newline이 있는 combined output
    작성 날짜: 2026/08/24
    """

    if not isinstance(process_result.stdout, (bytes, bytearray)) or not isinstance(
        process_result.stderr,
        (bytes, bytearray),
    ):
        raise SignedArtifactVerificationError(
            "signed artifact verification output is invalid"
        )
    if (
        len(process_result.stdout) + len(process_result.stderr)
        > MAXIMUM_COMMAND_OUTPUT_BYTES
    ):
        raise SignedArtifactVerificationError(
            "signed artifact verification output exceeded limit"
        )
    return bytes(process_result.stdout) + b"\n" + bytes(process_result.stderr)


def verify_strict_code_signature(
    artifact_path: Path,
    working_directory: Path,
    process_environment: Mapping[str, str],
    command_runner: CommandRunner,
) -> None:
    """
    함수 이름: verify_strict_code_signature()
    기능: app 또는 Mach-O의 deep·strict code signature를 검증한다.
    인자: artifact_path -> 검증할 app, main 또는 sidecar
        working_directory -> verifier child working directory
        process_environment -> sanitized environment
        command_runner -> subprocess 실행 경계
    반환값: 없음
    작성 날짜: 2026/08/24
    """

    _run_checked_command(
        [
            "/usr/bin/codesign",
            "--verify",
            "--deep",
            "--strict",
            "--verbose=4",
            str(artifact_path),
        ],
        working_directory,
        process_environment,
        command_runner,
    )


def verify_codesign_metadata(
    executable_path: Path,
    expected_team_id: str,
    expected_identity: str,
    working_directory: Path,
    process_environment: Mapping[str, str],
    command_runner: CommandRunner,
    *,
    require_runtime_flag: bool = True,
) -> None:
    """
    함수 이름: verify_codesign_metadata()
    기능: artifact가 expected Team ID 하나와 필요시 hardened runtime flag를 갖는지 검증한다.
    인자: executable_path -> main, sidecar 또는 DMG path
        expected_team_id -> R-01에서 확인한 exact Team ID
        working_directory -> verifier child working directory
        process_environment -> sanitized environment
        command_runner -> subprocess 실행 경계
        require_runtime_flag -> executable hardened runtime을 요구할지 여부
    반환값: 없음
    작성 날짜: 2026/08/24
    """

    metadata_result = _run_checked_command(
        ["/usr/bin/codesign", "-dvvv", str(executable_path)],
        working_directory,
        process_environment,
        command_runner,
    )
    metadata_output = _read_captured_output(metadata_result)

    team_identifier_values = [
        metadata_line.removeprefix(b"TeamIdentifier=")
        for metadata_line in metadata_output.splitlines()
        if metadata_line.startswith(b"TeamIdentifier=")
    ]
    if team_identifier_values != [expected_team_id.encode("ascii")]:
        raise SignedArtifactVerificationError(
            "signed executable Team ID is invalid"
        )

    authority_values = [
        metadata_line.removeprefix(b"Authority=")
        for metadata_line in metadata_output.splitlines()
        if metadata_line.startswith(b"Authority=")
    ]
    expected_identity_bytes = expected_identity.encode("utf-8")
    if (
        len(authority_values) != 3
        or authority_values[0] != expected_identity_bytes
        or authority_values[1] not in EXPECTED_AUTHORITY_INTERMEDIATES
        or authority_values[2] != EXPECTED_AUTHORITY_ROOT
    ):
        raise SignedArtifactVerificationError(
            "signed executable authority chain is invalid"
        )

    if require_runtime_flag:
        runtime_flag_groups = RUNTIME_FLAGS_PATTERN.findall(metadata_output)
        if not runtime_flag_groups or any(
            b"runtime"
            not in {
                flag_name.strip()
                for flag_name in runtime_flag_group.split(b",")
            }
            for runtime_flag_group in runtime_flag_groups
        ):
            raise SignedArtifactVerificationError(
                "signed executable hardened runtime flag is missing"
            )


def verify_forbidden_entitlement_absent(
    artifact_path: Path,
    working_directory: Path,
    process_environment: Mapping[str, str],
    command_runner: CommandRunner,
) -> None:
    """
    함수 이름: verify_forbidden_entitlement_absent()
    기능: app 또는 executable에 library validation disable entitlement key가 없는지 확인한다.
    인자: artifact_path -> 검사할 app, main 또는 sidecar
        working_directory -> verifier child working directory
        process_environment -> sanitized environment
        command_runner -> subprocess 실행 경계
    반환값: 없음
    작성 날짜: 2026/08/24
    """

    entitlement_result = _run_checked_command(
        [
            "/usr/bin/codesign",
            "-d",
            "--entitlements",
            "-",
            str(artifact_path),
        ],
        working_directory,
        process_environment,
        command_runner,
    )
    if FORBIDDEN_LIBRARY_VALIDATION_ENTITLEMENT in _read_captured_output(
        entitlement_result
    ):
        raise SignedArtifactVerificationError(
            "signed artifact contains a forbidden entitlement"
        )


def verify_arm64_architecture(
    executable_path: Path,
    working_directory: Path,
    process_environment: Mapping[str, str],
    command_runner: CommandRunner,
) -> None:
    """
    함수 이름: verify_arm64_architecture()
    기능: executable이 arm64 slice를 가진 Mach-O이고 x86-only가 아닌지 확인한다.
    인자: executable_path -> main 또는 sidecar path
        working_directory -> verifier child working directory
        process_environment -> sanitized environment
        command_runner -> subprocess 실행 경계
    반환값: 없음
    작성 날짜: 2026/08/24
    """

    file_result = _run_checked_command(
        ["/usr/bin/file", "-b", str(executable_path)],
        working_directory,
        process_environment,
        command_runner,
    )
    file_output = _read_captured_output(file_result)
    if b"Mach-O" not in file_output or ARM64_PATTERN.search(file_output) is None:
        raise SignedArtifactVerificationError(
            "signed executable architecture is invalid"
        )


def verify_minimum_os_load_command(
    executable_path: Path,
    working_directory: Path,
    process_environment: Mapping[str, str],
    command_runner: CommandRunner,
) -> None:
    """
    함수 이름: verify_minimum_os_load_command()
    기능: 모든 vtool build record가 exact macOS minos 11.0을 선언하는지 검증한다.
    인자: executable_path -> main 또는 sidecar path
        working_directory -> verifier child working directory
        process_environment -> sanitized environment
        command_runner -> subprocess 실행 경계
    반환값: 없음
    작성 날짜: 2026/08/24
    """

    vtool_result = _run_checked_command(
        ["/usr/bin/xcrun", "vtool", "-show-build", str(executable_path)],
        working_directory,
        process_environment,
        command_runner,
    )
    vtool_output = _read_captured_output(vtool_result)
    minimum_os_values = MINIMUM_OS_PATTERN.findall(vtool_output)
    platform_values = PLATFORM_PATTERN.findall(vtool_output)
    required_version_bytes = REQUIRED_MINIMUM_MACOS_VERSION.encode("ascii")
    if (
        not minimum_os_values
        or len(platform_values) != len(minimum_os_values)
        or any(platform_value != b"MACOS" for platform_value in platform_values)
        or any(
        minimum_os_value != required_version_bytes
        for minimum_os_value in minimum_os_values
        )
    ):
        raise SignedArtifactVerificationError(
            "signed executable minimum macOS version is invalid"
        )


def verify_native_minimum_os_compatibility(
    native_path: Path,
    working_directory: Path,
    process_environment: Mapping[str, str],
    command_runner: CommandRunner,
) -> None:
    """embedded native payload의 모든 minOS가 deployment target 11.0 이하인지 검증한다."""

    vtool_result = _run_checked_command(
        ["/usr/bin/xcrun", "vtool", "-show-build", str(native_path)],
        working_directory,
        process_environment,
        command_runner,
    )
    vtool_output = _read_captured_output(vtool_result)
    minimum_os_values = MINIMUM_OS_PATTERN.findall(vtool_output)
    platform_values = PLATFORM_PATTERN.findall(vtool_output)
    if (
        not minimum_os_values
        or len(platform_values) != len(minimum_os_values)
        or any(platform_value != b"MACOS" for platform_value in platform_values)
    ):
        raise SignedArtifactVerificationError(
            "signed native payload minimum macOS version is invalid"
        )

    def parse_version(version_bytes: bytes) -> tuple[int, int, int]:
        try:
            version_text = version_bytes.decode("ascii")
        except UnicodeDecodeError:
            raise SignedArtifactVerificationError(
                "signed native payload minimum macOS version is invalid"
            ) from None
        if re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){1,2}", version_text) is None:
            raise SignedArtifactVerificationError(
                "signed native payload minimum macOS version is invalid"
            )
        components = [int(component) for component in version_text.split(".")]
        return tuple((components + [0, 0])[:3])  # type: ignore[return-value]

    required_version = parse_version(
        REQUIRED_MINIMUM_MACOS_VERSION.encode("ascii")
    )
    if any(
        parse_version(minimum_os_value) > required_version
        for minimum_os_value in minimum_os_values
    ):
        raise SignedArtifactVerificationError(
            "signed native payload requires a newer macOS version"
        )


def verify_sidecar_direct_loader(
    sidecar_path: Path,
    working_directory: Path,
    process_environment: Mapping[str, str],
    command_runner: CommandRunner,
) -> None:
    """
    함수 이름: verify_sidecar_direct_loader()
    기능: fixed FD를 상속하지 않은 sidecar가 loader를 지나 EBADF contract에서만 종료되는지 확인한다.
    인자: sidecar_path -> packaged one-file sidecar executable
        working_directory -> sidecar direct launch working directory
        process_environment -> sanitized environment
        command_runner -> subprocess 실행 경계
    반환값: 없음
    작성 날짜: 2026/08/24
    """

    loader_result = _invoke_command(
        [str(sidecar_path)],
        working_directory,
        process_environment,
        command_runner,
        LOADER_SMOKE_TIMEOUT_SECONDS,
    )
    loader_output = _read_captured_output(loader_result).lower()
    if loader_result.returncode == 0:
        raise SignedArtifactVerificationError(
            "sidecar loader bypassed the fixed FD contract"
        )
    if any(
        loader_error_marker in loader_output
        for loader_error_marker in FORBIDDEN_LOADER_ERROR_MARKERS
    ):
        raise SignedArtifactVerificationError(
            "sidecar loader security validation failed"
        )
    if not any(
        fd_failure_marker in loader_output
        for fd_failure_marker in FD_CONTRACT_FAILURE_MARKERS
    ):
        raise SignedArtifactVerificationError(
            "sidecar did not reach the fixed FD contract"
        )


def _read_exact_at(
    file_descriptor: int,
    byte_count: int,
    offset: int,
) -> bytes:
    """single descriptor에서 exact byte range를 bounded하게 읽는다."""

    if byte_count < 0 or offset < 0:
        raise SignedArtifactVerificationError(
            "PyInstaller native archive is invalid"
        )
    result = bytearray()
    while len(result) < byte_count:
        try:
            chunk = os.pread(
                file_descriptor,
                min(1024 * 1024, byte_count - len(result)),
                offset + len(result),
            )
        except OSError:
            raise SignedArtifactVerificationError(
                "PyInstaller native archive could not be read"
            ) from None
        if not chunk:
            break
        result.extend(chunk)
    if len(result) != byte_count:
        raise SignedArtifactVerificationError(
            "PyInstaller native archive is truncated"
        )
    return bytes(result)


def _find_pyinstaller_cookie(
    file_descriptor: int,
    file_size: int,
) -> int:
    """sidecar 끝에서 PyInstaller cookie magic의 마지막 위치를 찾는다."""

    search_end = file_size
    while search_end >= len(PYINSTALLER_COOKIE_MAGIC):
        search_start = max(search_end - 8192, 0)
        search_bytes = _read_exact_at(
            file_descriptor,
            search_end - search_start,
            search_start,
        )
        relative_position = search_bytes.rfind(PYINSTALLER_COOKIE_MAGIC)
        if relative_position >= 0:
            return search_start + relative_position
        if search_start == 0:
            break
        search_end = search_start + len(PYINSTALLER_COOKIE_MAGIC) - 1
    raise SignedArtifactVerificationError(
        "PyInstaller native archive cookie is missing"
    )


def _decompress_native_payload(
    compressed_payload: bytes,
    expected_size: int,
) -> bytes:
    """declared 크기보다 큰 zlib expansion을 허용하지 않는다."""

    if expected_size <= 0 or expected_size > MAXIMUM_NATIVE_PAYLOAD_BYTES:
        raise SignedArtifactVerificationError(
            "PyInstaller native payload size is invalid"
        )
    try:
        decompressor = zlib.decompressobj()
        payload = decompressor.decompress(
            compressed_payload,
            expected_size + 1,
        )
        if (
            len(payload) != expected_size
            or not decompressor.eof
            or decompressor.unconsumed_tail
            or decompressor.unused_data
            or decompressor.flush()
        ):
            raise SignedArtifactVerificationError(
                "PyInstaller native payload compression is invalid"
            )
        return payload
    except SignedArtifactVerificationError:
        raise
    except zlib.error:
        raise SignedArtifactVerificationError(
            "PyInstaller native payload compression is invalid"
        ) from None


def extract_pyinstaller_native_payloads(
    sidecar_path: Path,
    extraction_directory: Path,
    expected_commit: str,
    expected_version: str,
    expected_build_version: str,
) -> PyInstallerArchiveArtifacts:
    """one-file CArchive에서 libpython과 모든 dylib/so를 격리 추출한다."""

    _require_directory(extraction_directory)
    sidecar_descriptor: int | None = None
    try:
        sidecar_descriptor = os.open(
            sidecar_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        initial_status = os.fstat(sidecar_descriptor)
        if not stat.S_ISREG(initial_status.st_mode):
            raise SignedArtifactVerificationError(
                "PyInstaller sidecar archive is invalid"
            )
        cookie_offset = _find_pyinstaller_cookie(
            sidecar_descriptor,
            initial_status.st_size,
        )
        cookie_bytes = _read_exact_at(
            sidecar_descriptor,
            PYINSTALLER_COOKIE_LENGTH,
            cookie_offset,
        )
        (
            cookie_magic,
            archive_length,
            toc_offset,
            toc_length,
            _python_version,
            python_library_raw,
        ) = struct.unpack(PYINSTALLER_COOKIE_FORMAT, cookie_bytes)
        archive_start = cookie_offset + PYINSTALLER_COOKIE_LENGTH - archive_length
        if (
            cookie_magic != PYINSTALLER_COOKIE_MAGIC
            or archive_length < PYINSTALLER_COOKIE_LENGTH
            or archive_start < 0
            or toc_length <= 0
            or toc_length > MAXIMUM_ARCHIVE_TOC_BYTES
            or toc_offset < 0
            or toc_offset + toc_length > cookie_offset - archive_start
        ):
            raise SignedArtifactVerificationError(
                "PyInstaller native archive bounds are invalid"
            )
        try:
            python_library_name = python_library_raw.split(b"\0", 1)[0].decode(
                "ascii"
            )
        except UnicodeDecodeError:
            raise SignedArtifactVerificationError(
                "PyInstaller Python library name is invalid"
            ) from None
        if (
            not re.fullmatch(r"libpython[0-9]+(?:\.[0-9]+)*\.dylib", python_library_name)
            or PurePosixPath(python_library_name).name != python_library_name
        ):
            raise SignedArtifactVerificationError(
                "PyInstaller Python library name is invalid"
            )

        toc_bytes = _read_exact_at(
            sidecar_descriptor,
            toc_length,
            archive_start + toc_offset,
        )
        toc_position = 0
        archive_entries: dict[str, tuple[int, int, int, int, str]] = {}
        while toc_position < len(toc_bytes):
            if (
                len(archive_entries) >= MAXIMUM_NATIVE_PAYLOAD_COUNT
                or len(toc_bytes) - toc_position < PYINSTALLER_TOC_ENTRY_LENGTH
            ):
                raise SignedArtifactVerificationError(
                    "PyInstaller native archive TOC is invalid"
                )
            (
                entry_length,
                entry_offset,
                compressed_length,
                uncompressed_length,
                compression_flag,
                typecode_raw,
            ) = struct.unpack_from(
                PYINSTALLER_TOC_ENTRY_FORMAT,
                toc_bytes,
                toc_position,
            )
            if (
                entry_length < PYINSTALLER_TOC_ENTRY_LENGTH + 1
                or entry_length > len(toc_bytes) - toc_position
                or compression_flag not in {0, 1}
                or entry_offset < 0
                or compressed_length <= 0
                or compressed_length > MAXIMUM_NATIVE_PAYLOAD_BYTES
                or uncompressed_length <= 0
                or uncompressed_length > MAXIMUM_NATIVE_PAYLOAD_BYTES
                or entry_offset + compressed_length > toc_offset
            ):
                raise SignedArtifactVerificationError(
                    "PyInstaller native archive TOC is invalid"
                )
            name_bytes = toc_bytes[
                toc_position + PYINSTALLER_TOC_ENTRY_LENGTH : toc_position
                + entry_length
            ].rstrip(b"\0")
            try:
                entry_name = name_bytes.decode("utf-8")
                typecode = typecode_raw.decode("ascii")
            except UnicodeDecodeError:
                raise SignedArtifactVerificationError(
                    "PyInstaller native archive TOC is invalid"
                ) from None
            normalized_entry_path = PurePosixPath(entry_name)
            if (
                not entry_name
                or entry_name in archive_entries
                or normalized_entry_path.is_absolute()
                or ".." in normalized_entry_path.parts
                or typecode == "o"
            ):
                raise SignedArtifactVerificationError(
                    "PyInstaller native archive TOC is invalid"
                )
            archive_entries[entry_name] = (
                entry_offset,
                compressed_length,
                uncompressed_length,
                compression_flag,
                typecode,
            )
            toc_position += entry_length
        if toc_position != len(toc_bytes) or python_library_name not in archive_entries:
            raise SignedArtifactVerificationError(
                "PyInstaller Python library payload is missing"
            )

        provenance_aliases = [
            entry_name
            for entry_name in archive_entries
            if PurePosixPath(entry_name).name == PYINSTALLER_PROVENANCE_NAME
        ]
        if provenance_aliases != [PYINSTALLER_PROVENANCE_NAME]:
            raise SignedArtifactVerificationError(
                "PyInstaller release provenance is missing or ambiguous"
            )
        (
            provenance_offset,
            provenance_compressed_length,
            provenance_uncompressed_length,
            provenance_compression_flag,
            provenance_typecode,
        ) = archive_entries[PYINSTALLER_PROVENANCE_NAME]
        if (
            provenance_typecode != "x"
            or provenance_compressed_length > MAXIMUM_PROVENANCE_BYTES + 1024
            or provenance_uncompressed_length <= 0
            or provenance_uncompressed_length > MAXIMUM_PROVENANCE_BYTES
        ):
            raise SignedArtifactVerificationError(
                "PyInstaller release provenance member is invalid"
            )
        provenance_bytes = _read_exact_at(
            sidecar_descriptor,
            provenance_compressed_length,
            archive_start + provenance_offset,
        )
        if provenance_compression_flag:
            provenance_bytes = _decompress_native_payload(
                provenance_bytes,
                provenance_uncompressed_length,
            )
        elif len(provenance_bytes) != provenance_uncompressed_length:
            raise SignedArtifactVerificationError(
                "PyInstaller release provenance size is invalid"
            )

        def reject_duplicate_json_keys(
            object_pairs: list[tuple[str, object]],
        ) -> dict[str, object]:
            parsed_object: dict[str, object] = {}
            for object_key, object_value in object_pairs:
                if object_key in parsed_object:
                    raise ValueError("duplicate provenance key")
                parsed_object[object_key] = object_value
            return parsed_object

        try:
            provenance = json.loads(
                provenance_bytes.decode("utf-8"),
                object_pairs_hook=reject_duplicate_json_keys,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    ValueError("non-standard JSON constant")
                ),
            )
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            raise SignedArtifactVerificationError(
                "PyInstaller release provenance JSON is invalid"
            ) from None
        if (
            not isinstance(provenance, dict)
            or set(provenance) != {
                "schema_version",
                "commit",
                "version",
                "build_version",
            }
            or type(provenance["schema_version"]) is not int
            or provenance["schema_version"] != 1
            or provenance["commit"] != expected_commit
            or provenance["version"] != expected_version
            or provenance["build_version"] != expected_build_version
        ):
            raise SignedArtifactVerificationError(
                "PyInstaller release provenance does not match"
            )
        canonical_provenance_bytes = (
            json.dumps(
                provenance,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
            + b"\n"
        )
        if provenance_bytes != canonical_provenance_bytes:
            raise SignedArtifactVerificationError(
                "PyInstaller release provenance is not canonical"
            )
        provenance_sha256 = hashlib.sha256(provenance_bytes).hexdigest()

        native_entry_names = sorted(
            entry_name
            for entry_name in archive_entries
            if entry_name.endswith((".dylib", ".so"))
        )
        if python_library_name not in native_entry_names:
            raise SignedArtifactVerificationError(
                "PyInstaller Python library payload is missing"
            )
        extracted_payloads: list[NativePayloadArtifact] = []
        for payload_index, entry_name in enumerate(native_entry_names):
            (
                entry_offset,
                compressed_length,
                uncompressed_length,
                compression_flag,
                _typecode,
            ) = archive_entries[entry_name]
            payload_bytes = _read_exact_at(
                sidecar_descriptor,
                compressed_length,
                archive_start + entry_offset,
            )
            if compression_flag:
                payload_bytes = _decompress_native_payload(
                    payload_bytes,
                    uncompressed_length,
                )
            elif len(payload_bytes) != uncompressed_length:
                raise SignedArtifactVerificationError(
                    "PyInstaller native payload size is invalid"
                )

            payload_suffix = ".dylib" if entry_name.endswith(".dylib") else ".so"
            extracted_path = extraction_directory / (
                f"native-{payload_index:04d}{payload_suffix}"
            )
            payload_descriptor: int | None = None
            try:
                payload_descriptor = os.open(
                    extracted_path,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o700,
                )
                bytes_written = 0
                while bytes_written < len(payload_bytes):
                    written_count = os.write(
                        payload_descriptor,
                        payload_bytes[bytes_written:],
                    )
                    if written_count <= 0:
                        raise OSError("short native payload write")
                    bytes_written += written_count
                if os.fstat(payload_descriptor).st_size != len(payload_bytes):
                    raise OSError("invalid native payload size")
            except OSError:
                raise SignedArtifactVerificationError(
                    "PyInstaller native payload extraction failed"
                ) from None
            finally:
                if payload_descriptor is not None:
                    os.close(payload_descriptor)
            extracted_payloads.append(
                NativePayloadArtifact(
                    payload_path=extracted_path,
                    is_python_library=entry_name == python_library_name,
                    sha256=hashlib.sha256(payload_bytes).hexdigest(),
                )
            )

        if sum(payload.is_python_library for payload in extracted_payloads) != 1:
            raise SignedArtifactVerificationError(
                "PyInstaller Python library evidence is ambiguous"
            )
        if _stable_stat_identity(os.fstat(sidecar_descriptor)) != (
            _stable_stat_identity(initial_status)
        ):
            raise SignedArtifactVerificationError(
                "PyInstaller sidecar changed during extraction"
            )
        return PyInstallerArchiveArtifacts(
            native_payloads=tuple(extracted_payloads),
            provenance_sha256=provenance_sha256,
        )
    except SignedArtifactVerificationError:
        raise
    except (OSError, ValueError, struct.error):
        raise SignedArtifactVerificationError(
            "PyInstaller native archive verification failed"
        ) from None
    finally:
        if sidecar_descriptor is not None:
            os.close(sidecar_descriptor)


def verify_pyinstaller_native_payloads(
    sidecar_path: Path,
    expected_team_id: str,
    expected_identity: str,
    expected_commit: str,
    expected_version: str,
    expected_build_version: str,
    working_directory: Path,
    process_environment: Mapping[str, str],
    command_runner: CommandRunner,
) -> tuple[tuple[ExtractedNativeEvidence, ...], str]:
    """실제 CArchive libpython과 모든 native extension signature/minOS를 검증한다."""

    with _isolated_native_extraction_directory() as extraction_directory:
        _require_directory(extraction_directory)
        archive_artifacts = extract_pyinstaller_native_payloads(
            sidecar_path,
            extraction_directory,
            expected_commit,
            expected_version,
            expected_build_version,
        )
        extracted_evidence: list[ExtractedNativeEvidence] = []
        for native_payload in archive_artifacts.native_payloads:
            verify_strict_code_signature(
                native_payload.payload_path,
                working_directory,
                process_environment,
                command_runner,
            )
            verify_codesign_metadata(
                native_payload.payload_path,
                expected_team_id,
                expected_identity,
                working_directory,
                process_environment,
                command_runner,
                require_runtime_flag=False,
            )
            verify_forbidden_entitlement_absent(
                native_payload.payload_path,
                working_directory,
                process_environment,
                command_runner,
            )
            verify_arm64_architecture(
                native_payload.payload_path,
                working_directory,
                process_environment,
                command_runner,
            )
            verify_native_minimum_os_compatibility(
                native_payload.payload_path,
                working_directory,
                process_environment,
                command_runner,
            )
            extracted_evidence.append(
                ExtractedNativeEvidence(
                    kind=(
                        "libpython"
                        if native_payload.is_python_library
                        else "native-extension"
                    ),
                    sha256=native_payload.sha256,
                )
            )
        if (
            sum(evidence.kind == "libpython" for evidence in extracted_evidence)
            != 1
            or any(
                evidence.kind not in {"libpython", "native-extension"}
                for evidence in extracted_evidence
            )
            or len({evidence.sha256 for evidence in extracted_evidence})
            != len(extracted_evidence)
        ):
            raise SignedArtifactVerificationError(
                "PyInstaller native evidence is incomplete or ambiguous"
            )
        return (
            tuple(extracted_evidence),
            archive_artifacts.provenance_sha256,
        )


def verify_app_signature_contract(
    app_artifacts: AppBundleArtifacts,
    expected_team_id: str,
    expected_identity: str,
    expected_commit: str,
    expected_version: str,
    expected_build_version: str,
    process_environment: Mapping[str, str],
    command_runner: CommandRunner,
) -> tuple[tuple[ExtractedNativeEvidence, ...], str]:
    """app/main/sidecar와 실제 PyInstaller native payload 전체 계약을 검증한다."""

    working_directory = app_artifacts.app_path.parent
    initial_hashes = (
        calculate_regular_file_sha256(app_artifacts.main_executable_path),
        calculate_regular_file_sha256(app_artifacts.sidecar_path),
        calculate_regular_file_sha256(app_artifacts.code_resources_path),
    )
    verify_release_commit_marker(
        app_artifacts.main_executable_path,
        expected_commit,
    )
    for signed_path in (
        app_artifacts.app_path,
        app_artifacts.main_executable_path,
        app_artifacts.sidecar_path,
    ):
        verify_strict_code_signature(
            signed_path,
            working_directory,
            process_environment,
            command_runner,
        )
        verify_codesign_metadata(
            signed_path,
            expected_team_id,
            expected_identity,
            working_directory,
            process_environment,
            command_runner,
        )
        verify_forbidden_entitlement_absent(
            signed_path,
            working_directory,
            process_environment,
            command_runner,
        )

    for executable_path in (
        app_artifacts.main_executable_path,
        app_artifacts.sidecar_path,
    ):
        verify_arm64_architecture(
            executable_path,
            working_directory,
            process_environment,
            command_runner,
        )
        verify_minimum_os_load_command(
            executable_path,
            working_directory,
            process_environment,
            command_runner,
        )

    extracted_native, provenance_sha256 = verify_pyinstaller_native_payloads(
        app_artifacts.sidecar_path,
        expected_team_id,
        expected_identity,
        expected_commit,
        expected_version,
        expected_build_version,
        working_directory,
        process_environment,
        command_runner,
    )
    verify_sidecar_direct_loader(
        app_artifacts.sidecar_path,
        app_artifacts.main_executable_path.parent,
        process_environment,
        command_runner,
    )
    final_hashes = (
        calculate_regular_file_sha256(app_artifacts.main_executable_path),
        calculate_regular_file_sha256(app_artifacts.sidecar_path),
        calculate_regular_file_sha256(app_artifacts.code_resources_path),
    )
    if final_hashes != initial_hashes:
        raise SignedArtifactVerificationError(
            "signed app changed during verification"
        )
    return extracted_native, provenance_sha256


def _parse_plist_command_output(
    process_result: subprocess.CompletedProcess[bytes],
) -> dict[str, object]:
    """bounded Apple tool plist output을 strict top-level dictionary로 읽는다."""

    try:
        parsed_output = plistlib.loads(_read_captured_output(process_result))
    except (ValueError, TypeError, plistlib.InvalidFileException):
        raise SignedArtifactVerificationError(
            "mounted DMG metadata is invalid"
        ) from None
    if not isinstance(parsed_output, dict):
        raise SignedArtifactVerificationError(
            "mounted DMG metadata is invalid"
        )
    return parsed_output


def _parse_attached_device(
    process_result: subprocess.CompletedProcess[bytes],
    mount_directory: Path,
) -> str:
    """hdiutil attach plist에서 exact mount와 단일 device를 추출한다."""

    parsed_output = _parse_plist_command_output(process_result)
    system_entities = parsed_output.get("system-entities")
    if not isinstance(system_entities, list):
        raise SignedArtifactVerificationError(
            "mounted DMG metadata is invalid"
        )
    mounted_entities = [
        entity
        for entity in system_entities
        if isinstance(entity, dict) and "mount-point" in entity
    ]
    if len(mounted_entities) != 1:
        raise SignedArtifactVerificationError(
            "mounted DMG metadata is ambiguous"
        )
    mounted_entity = mounted_entities[0]
    device_path = mounted_entity.get("dev-entry")
    if (
        mounted_entity.get("mount-point") != str(mount_directory)
        or not isinstance(device_path, str)
        or DMG_DEVICE_PATTERN.fullmatch(device_path) is None
    ):
        raise SignedArtifactVerificationError(
            "mounted DMG metadata is invalid"
        )
    return device_path


def _verify_read_only_mount(
    device_path: str,
    mount_directory: Path,
    working_directory: Path,
    process_environment: Mapping[str, str],
    command_runner: CommandRunner,
) -> None:
    """diskutil evidence로 attached filesystem이 exact mount이고 writable=false인지 확인한다."""

    disk_info_result = _run_checked_command(
        ["/usr/sbin/diskutil", "info", "-plist", device_path],
        working_directory,
        process_environment,
        command_runner,
    )
    disk_info = _parse_plist_command_output(disk_info_result)
    if (
        disk_info.get("DeviceNode") != device_path
        or disk_info.get("MountPoint") != str(mount_directory)
        or disk_info.get("Writable") is not False
    ):
        raise SignedArtifactVerificationError(
            "mounted DMG is not read-only"
        )


def _inspect_mounted_dmg_layout(
    mount_directory: Path,
    expected_app_name: str,
    expected_version: str,
    expected_build_version: str,
) -> AppBundleArtifacts:
    """top-level app 하나와 exact /Applications symlink만 허용한다."""

    _require_directory(mount_directory)
    try:
        mounted_entries = list(os.scandir(mount_directory))
    except OSError:
        raise SignedArtifactVerificationError(
            "mounted DMG layout could not be read"
        ) from None
    app_entries = [entry for entry in mounted_entries if entry.name.endswith(".app")]
    applications_entries = [
        entry for entry in mounted_entries if entry.name == "Applications"
    ]
    if len(app_entries) != 1 or len(applications_entries) != 1:
        raise SignedArtifactVerificationError(
            "mounted DMG layout is invalid"
        )
    app_entry = app_entries[0]
    applications_entry = applications_entries[0]
    if (
        app_entry.name != expected_app_name
        or not app_entry.is_dir(follow_symlinks=False)
        or app_entry.is_symlink()
        or not applications_entry.is_symlink()
    ):
        raise SignedArtifactVerificationError(
            "mounted DMG layout is invalid"
        )
    applications_path = mount_directory / "Applications"
    try:
        if os.readlink(applications_path) != "/Applications":
            raise SignedArtifactVerificationError(
                "mounted DMG Applications link is invalid"
            )
    except OSError:
        raise SignedArtifactVerificationError(
            "mounted DMG Applications link is invalid"
        ) from None
    return inspect_app_bundle(
        mount_directory / app_entry.name,
        expected_version,
        expected_build_version,
    )


def _verify_app_assessments(
    app_path: Path,
    working_directory: Path,
    process_environment: Mapping[str, str],
    command_runner: CommandRunner,
) -> None:
    """stapled app ticket과 Gatekeeper execute assessment를 검증한다."""

    for app_verification_command in (
        ["/usr/bin/xcrun", "stapler", "validate", str(app_path)],
        [
            "/usr/sbin/spctl",
            "--assess",
            "--type",
            "execute",
            "--verbose=4",
            str(app_path),
        ],
    ):
        _run_checked_command(
            app_verification_command,
            working_directory,
            process_environment,
            command_runner,
        )


def verify_dmg_artifact(
    external_app: AppBundleArtifacts,
    dmg_path: Path,
    expected_team_id: str,
    expected_identity: str,
    expected_version: str,
    expected_build_version: str,
    expected_commit: str,
    expected_provenance_sha256: str,
    dmg_file_descriptor: int,
    initial_dmg_status: os.stat_result,
    initial_dmg_digest: str,
    process_environment: Mapping[str, str],
    command_runner: CommandRunner,
) -> None:
    """final DMG, isolated mounted payload identity와 mounted signature 계약을 검증한다."""

    working_directory = external_app.app_path.parent
    _verify_app_assessments(
        external_app.app_path,
        working_directory,
        process_environment,
        command_runner,
    )
    _run_checked_command(
        [
            "/usr/bin/codesign",
            "--verify",
            "--strict",
            "--verbose=4",
            str(dmg_path),
        ],
        working_directory,
        process_environment,
        command_runner,
    )
    verify_codesign_metadata(
        dmg_path,
        expected_team_id,
        expected_identity,
        working_directory,
        process_environment,
        command_runner,
        require_runtime_flag=False,
    )
    for verification_command in (
        ["/usr/bin/hdiutil", "verify", str(dmg_path)],
        ["/usr/bin/xcrun", "stapler", "validate", str(dmg_path)],
        [
            "/usr/sbin/spctl",
            "--assess",
            "--type",
            "open",
            "--context",
            "context:primary-signature",
            "--verbose=4",
            str(dmg_path),
        ],
    ):
        _run_checked_command(
            verification_command,
            working_directory,
            process_environment,
            command_runner,
        )

    _verify_open_release_artifact_unchanged(
        dmg_path,
        dmg_file_descriptor,
        initial_dmg_status,
        initial_dmg_digest,
    )

    mount_directory = _create_isolated_temporary_directory(
        "phase12-dmg-mount-"
    )
    original_mount_directory_status = os.lstat(mount_directory)
    attach_attempted = False
    attached = False
    attached_device: str | None = None
    try:
        _require_directory(mount_directory)
        attach_attempted = True
        attach_result = _run_checked_command(
            [
                "/usr/bin/hdiutil",
                "attach",
                "-readonly",
                "-nobrowse",
                "-noautoopen",
                "-mountpoint",
                str(mount_directory),
                "-plist",
                str(dmg_path),
            ],
            working_directory,
            process_environment,
            command_runner,
        )
        attached = True
        attached_device = _parse_attached_device(attach_result, mount_directory)
        _verify_read_only_mount(
            attached_device,
            mount_directory,
            working_directory,
            process_environment,
            command_runner,
        )
        mounted_root_identity = _stable_stat_identity(os.lstat(mount_directory))
        mounted_app = _inspect_mounted_dmg_layout(
            mount_directory,
            external_app.app_path.name,
            expected_version,
            expected_build_version,
        )
        if (
            mounted_app.bundle_identifier != external_app.bundle_identifier
            or mounted_app.release_version != external_app.release_version
            or mounted_app.build_version != external_app.build_version
            or mounted_app.main_executable_path.name
            != external_app.main_executable_path.name
        ):
            raise SignedArtifactVerificationError(
                "mounted app release candidate does not match"
            )
        external_hashes = (
            calculate_regular_file_sha256(external_app.main_executable_path),
            calculate_regular_file_sha256(external_app.sidecar_path),
            calculate_regular_file_sha256(external_app.code_resources_path),
        )
        mounted_hashes = (
            calculate_regular_file_sha256(mounted_app.main_executable_path),
            calculate_regular_file_sha256(mounted_app.sidecar_path),
            calculate_regular_file_sha256(mounted_app.code_resources_path),
        )
        if mounted_hashes != external_hashes:
            raise SignedArtifactVerificationError(
                "mounted app release candidate does not match"
            )
        mounted_native, mounted_provenance_sha256 = verify_app_signature_contract(
            mounted_app,
            expected_team_id,
            expected_identity,
            expected_commit,
            expected_version,
            expected_build_version,
            process_environment,
            command_runner,
        )
        _verify_app_assessments(
            mounted_app.app_path,
            working_directory,
            process_environment,
            command_runner,
        )
        final_mounted_app = _inspect_mounted_dmg_layout(
            mount_directory,
            external_app.app_path.name,
            expected_version,
            expected_build_version,
        )
        if (
            _stable_stat_identity(os.lstat(mount_directory))
            != mounted_root_identity
            or calculate_regular_file_sha256(final_mounted_app.main_executable_path)
            != mounted_hashes[0]
            or calculate_regular_file_sha256(final_mounted_app.sidecar_path)
            != mounted_hashes[1]
            or calculate_regular_file_sha256(final_mounted_app.code_resources_path)
            != mounted_hashes[2]
            or not mounted_native
            or mounted_provenance_sha256 != expected_provenance_sha256
        ):
            raise SignedArtifactVerificationError(
                "mounted DMG changed during verification"
            )
    except SignedArtifactVerificationError:
        raise
    except (OSError, ValueError):
        raise SignedArtifactVerificationError(
            "mounted DMG verification failed"
        ) from None
    finally:
        detach_failed = False
        if attach_attempted:
            detach_target = attached_device or str(mount_directory)
            try:
                detach_result = _invoke_command(
                    ["/usr/bin/hdiutil", "detach", detach_target],
                    working_directory,
                    process_environment,
                    command_runner,
                    COMMAND_TIMEOUT_SECONDS,
                )
                if attached and detach_result.returncode != 0:
                    detach_failed = True
            except SignedArtifactVerificationError:
                if attached:
                    detach_failed = True
        cleanup_failed = False
        try:
            _require_directory(mount_directory)
            if (
                _stable_directory_identity(os.lstat(mount_directory))
                != _stable_directory_identity(original_mount_directory_status)
            ):
                raise OSError("mount directory identity changed")
            with os.scandir(mount_directory) as mounted_entries:
                if next(mounted_entries, None) is not None:
                    cleanup_failed = True
            if cleanup_failed:
                raise OSError("mount directory is not empty")
            os.rmdir(mount_directory)
        except (OSError, SignedArtifactVerificationError):
            cleanup_failed = True
        _verify_open_release_artifact_unchanged(
            dmg_path,
            dmg_file_descriptor,
            initial_dmg_status,
            initial_dmg_digest,
        )
        if detach_failed or cleanup_failed:
            raise SignedArtifactVerificationError(
                "mounted DMG cleanup failed"
            ) from None
    if (
        calculate_regular_file_sha256(external_app.main_executable_path)
        != external_hashes[0]
        or calculate_regular_file_sha256(external_app.sidecar_path)
        != external_hashes[1]
        or calculate_regular_file_sha256(external_app.code_resources_path)
        != external_hashes[2]
    ):
        raise SignedArtifactVerificationError(
            "release candidate changed during DMG verification"
        )


def verify_signed_artifacts(
    app_path: Path,
    expected_team_id: str,
    expected_identity: str,
    expected_version: str,
    expected_build_version: str,
    expected_commit: str,
    *,
    dmg_path: Path | None = None,
    source_environment: Mapping[str, str] | None = None,
    command_runner: CommandRunner = run_command,
) -> SignedArtifactEvidence:
    """
    함수 이름: verify_signed_artifacts()
    기능: app/main/sidecar와 선택적 DMG의 Phase 12 signed release gate를 순서대로 검증한다.
    인자: app_path -> exact release candidate .app path
        expected_team_id -> R-01에서 확인한 non-secret Team ID
        dmg_path -> 선택적 final signed·notarized DMG path
        source_environment -> caller environment 또는 실제 os.environ을 뜻하는 None
        command_runner -> production 또는 isolated test subprocess 경계
    반환값: path를 포함하지 않는 signed artifact evidence
    작성 날짜: 2026/08/24
    """

    validated_team_id = validate_expected_team_id(expected_team_id)
    validated_identity = validate_expected_identity(
        expected_identity,
        validated_team_id,
    )
    validated_version, validated_build_version = validate_expected_versions(
        expected_version,
        expected_build_version,
    )
    validated_commit = validate_expected_commit(expected_commit)
    try:
        normalized_app_path = Path(app_path)
        normalized_dmg_path = Path(dmg_path) if dmg_path is not None else None
    except (TypeError, ValueError):
        raise SignedArtifactVerificationError(
            "signed artifact path configuration is invalid"
        ) from None

    configured_paths = tuple(
        artifact_path
        for artifact_path in (normalized_app_path, normalized_dmg_path)
        if artifact_path is not None
    )
    if any(
        not artifact_path.is_absolute()
        or ".." in artifact_path.parts
        or str(artifact_path).startswith("//")
        for artifact_path in configured_paths
    ):
        raise SignedArtifactVerificationError(
            "signed artifact paths must be absolute"
        )
    for configured_path in configured_paths:
        _require_physical_absolute_path(configured_path)

    app_artifacts = inspect_app_bundle(
        normalized_app_path,
        validated_version,
        validated_build_version,
    )
    if normalized_dmg_path is not None:
        inspect_dmg_path(normalized_dmg_path)

    selected_environment = os.environ if source_environment is None else source_environment
    process_environment = create_sanitized_environment(selected_environment)
    dmg_file_descriptor: int | None = None
    dmg_status: os.stat_result | None = None
    dmg_snapshot: ArtifactFileSnapshot | None = None
    dmg_digest: str | None = None
    if normalized_dmg_path is not None:
        (
            dmg_file_descriptor,
            dmg_status,
            dmg_snapshot,
            dmg_digest,
        ) = _open_release_artifact(normalized_dmg_path)
    try:
        extracted_native, provenance_sha256 = verify_app_signature_contract(
            app_artifacts,
            validated_team_id,
            validated_identity,
            validated_commit,
            validated_version,
            validated_build_version,
            process_environment,
            command_runner,
        )
        if normalized_dmg_path is not None:
            if (
                dmg_file_descriptor is None
                or dmg_status is None
                or dmg_snapshot is None
                or dmg_digest is None
            ):
                raise SignedArtifactVerificationError(
                    "release artifact descriptor evidence is missing"
                )
            verify_dmg_artifact(
                app_artifacts,
                normalized_dmg_path,
                validated_team_id,
                validated_identity,
                validated_version,
                validated_build_version,
                validated_commit,
                provenance_sha256,
                dmg_file_descriptor,
                dmg_status,
                dmg_digest,
                process_environment,
                command_runner,
            )
    finally:
        if dmg_file_descriptor is not None:
            os.close(dmg_file_descriptor)

    return SignedArtifactEvidence(
        team_id=validated_team_id,
        dmg_verified=normalized_dmg_path is not None,
        extracted_native=extracted_native,
        commit=validated_commit,
        provenance_sha256=provenance_sha256,
        dmg_sha256=dmg_digest,
        dmg_snapshot=dmg_snapshot,
    )


def parse_arguments(
    argument_values: Sequence[str] | None = None,
) -> argparse.Namespace:
    """
    함수 이름: parse_arguments()
    기능: exact app, optional DMG와 expected Team ID CLI option을 읽는다.
    인자: argument_values -> 명시 인자 또는 실제 command line을 뜻하는 None
    반환값: parsed argparse namespace
    작성 날짜: 2026/08/24
    """

    parser = GenericArgumentParser(
        description="Verify a Phase 12 signed release candidate.",
    )
    parser.add_argument("--app", required=True, help="signed .app path")
    parser.add_argument("--dmg", help="optional final signed .dmg path")
    parser.add_argument(
        "--expected-team-id",
        required=True,
        help="non-secret Team ID confirmed by release identity preflight",
    )
    parser.add_argument(
        "--expected-identity",
        required=True,
        help="Developer ID Application identity confirmed by release preflight",
    )
    parser.add_argument(
        "--expected-version",
        required=True,
        help="exact release semantic version",
    )
    parser.add_argument(
        "--expected-build-version",
        required=True,
        help="exact CFBundleVersion",
    )
    parser.add_argument(
        "--expected-commit",
        required=True,
        help="exact lowercase release Git commit",
    )
    return parser.parse_args(argument_values)


def main(argument_values: Sequence[str] | None = None) -> int:
    """
    함수 이름: main()
    기능: CLI signed release gate를 path·Team ID·tool output 비반사 결과로 실행한다.
    인자: argument_values -> 명시 인자 또는 실제 command line을 뜻하는 None
    반환값: 검증 성공 0, artifact·command 실패 1
    작성 날짜: 2026/08/24
    """

    arguments = parse_arguments(argument_values)
    try:
        verify_signed_artifacts(
            Path(arguments.app),
            arguments.expected_team_id,
            arguments.expected_identity,
            arguments.expected_version,
            arguments.expected_build_version,
            arguments.expected_commit,
            dmg_path=Path(arguments.dmg) if arguments.dmg is not None else None,
        )
    except Exception:
        # 예상하지 못한 local exception도 traceback에 artifact path를 노출하지 않도록 닫는다.
        print(
            "phase12-signed-artifacts: ERROR: signed artifact verification failed.",
            file=sys.stderr,
        )
        return 1

    print("phase12-signed-artifacts: PASS: signed release artifacts verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
