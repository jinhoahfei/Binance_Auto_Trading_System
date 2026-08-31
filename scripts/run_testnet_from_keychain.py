#!/usr/bin/env python3
"""고정 macOS Keychain credential로 승인된 Testnet unittest 하나만 실행한다."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import resource
import stat
import subprocess
import sys


# Keychain source와 Testnet permission 환경 이름은 CLI로 바꿀 수 없는 상수로 고정한다.
KEYCHAIN_SECURITY_COMMAND = "/usr/bin/security"
KEYCHAIN_SERVICE = "com.binance-auto.trader.testnet"
KEYCHAIN_API_KEY_ACCOUNT = "api-key"
KEYCHAIN_API_SECRET_ACCOUNT = "api-secret"
KEYCHAIN_READ_TIMEOUT_SECONDS = 30
MINIMUM_CREDENTIAL_BYTES = 8
MAXIMUM_CREDENTIAL_BYTES = 512

BINANCE_RUN_TESTNET_ENV = "BINANCE_RUN_TESTNET"
BINANCE_TESTNET_API_KEY_ENV = "BINANCE_TESTNET_API_KEY"
BINANCE_TESTNET_API_SECRET_ENV = "BINANCE_TESTNET_API_SECRET"
BINANCE_RUN_TESTNET_ORDERS_ENV = "BINANCE_RUN_TESTNET_ORDERS"
BINANCE_RUN_PHASE13_PUBLIC_CASE2_ENV = "BINANCE_RUN_PHASE13_PUBLIC_CASE2"
BINANCE_TESTNET_MAX_NOTIONAL_ENV = "BINANCE_TESTNET_MAX_NOTIONAL"
BINANCE_TESTNET_BASELINE_HISTORY_FD_ENV = (
    "BINANCE_TESTNET_BASELINE_HISTORY_FD"
)
BINANCE_TESTNET_BASELINE_HISTORY_SHA256_ENV = (
    "BINANCE_TESTNET_BASELINE_HISTORY_SHA256"
)
BINANCE_TESTNET_BASELINE_PENDING_FD_ENV = (
    "BINANCE_TESTNET_BASELINE_PENDING_FD"
)
BINANCE_TESTNET_BASELINE_PENDING_SHA256_ENV = (
    "BINANCE_TESTNET_BASELINE_PENDING_SHA256"
)

# Script 위치에서 repository와 backend source를 계산해 호출자의 working directory를 신뢰하지 않는다.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
BACKEND_SOURCE_ROOT = BACKEND_ROOT / "src"
BASELINE_ARTIFACT_ROOT = BACKEND_ROOT / ".testnet-artifacts"
BASELINE_HISTORY_FILE_NAME = "history.jsonl"
BASELINE_HISTORY_READ_CHUNK_BYTES = 65_536

# 각 mode는 unittest module 하나와 permission mapping 하나만 선택할 수 있다.
MODE_TEST_MODULES: dict[str, str] = {
    "read-only": "tests.testnet.test_binance_testnet_read_only",
    "phase13-public-case2": "tests.testnet.test_phase13_public_market_case2",
}


class TestnetKeychainRunnerError(RuntimeError):
    """
    클래스 이름: TestnetKeychainRunnerError
    기능: Credential 원문 없이 Keychain 조회 또는 고정 unittest exec 실패를 나타낸다.
    작성 날짜: 2026/08/31
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 모든 실패를 credential과 무관한 고정 오류 문장으로 만든다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        super().__init__("secure Testnet runner is unavailable")


@dataclass(frozen=True, slots=True)
class TestnetRunnerArguments:
    """
    클래스 이름: TestnetRunnerArguments
    기능: Fixed Testnet mode와 optional verified baseline 경로를 함께 보존한다.
    작성 날짜: 2026/08/31
    """

    mode: str
    baseline_history_path: Path | None


@dataclass(frozen=True, slots=True)
class VerifiedBaselineHistory:
    """
    클래스 이름: VerifiedBaselineHistory
    기능: Path 교체와 무관한 history·pending descriptor와 exact digest를 보존한다.
    작성 날짜: 2026/08/31
    """

    descriptor: int
    sha256: str
    pending_descriptor: int | None
    pending_sha256: str | None


def zeroize_secret_buffer(secret_buffer: bytearray) -> None:
    """
    함수 이름: zeroize_secret_buffer()
    기능: 더 이상 필요하지 않은 mutable credential bytes를 덮어쓰고 비운다.
    인자: secret_buffer -> 폐기할 credential bytearray
    반환값: 없음
    작성 날짜: 2026/08/31
    """
    if not isinstance(secret_buffer, bytearray):
        raise TypeError("secret_buffer must be a bytearray")

    # 논리 길이를 비우기 전에 기존 할당 영역의 모든 byte를 0으로 덮어쓴다.
    for byte_index in range(len(secret_buffer)):
        secret_buffer[byte_index] = 0
    secret_buffer.clear()  # 폐기된 buffer를 이후 코드가 credential로 재사용하지 못하게 한다.


def _clear_captured_output(
    process_result: subprocess.CompletedProcess[bytes],
) -> None:
    """
    함수 이름: _clear_captured_output()
    기능: security subprocess가 보존한 stdout과 stderr 참조를 성공·실패 모두에서 제거한다.
    인자: process_result -> Keychain 조회의 completed process 결과
    반환값: 없음
    작성 날짜: 2026/08/31
    """
    # Test double이 mutable output을 반환하면 참조 제거 전에 backing memory도 덮어쓴다.
    if isinstance(process_result.stdout, bytearray):
        zeroize_secret_buffer(process_result.stdout)
    if isinstance(process_result.stderr, bytearray):
        zeroize_secret_buffer(process_result.stderr)
    process_result.stdout = b""  # Immutable stdout의 결과 객체 참조를 가능한 즉시 끊는다.
    process_result.stderr = b""  # Raw security 진단은 runner stderr로 전달하지 않는다.


def _clear_timeout_output(error: subprocess.TimeoutExpired) -> None:
    """
    함수 이름: _clear_timeout_output()
    기능: Timeout exception에 붙은 partial Keychain output을 원인 연결 전에 제거한다.
    인자: error -> security subprocess timeout
    반환값: 없음
    작성 날짜: 2026/08/31
    """
    # Mutable partial output은 덮어쓰고 immutable output은 exception 참조에서 분리한다.
    if isinstance(error.output, bytearray):
        zeroize_secret_buffer(error.output)
    if isinstance(error.stderr, bytearray):
        zeroize_secret_buffer(error.stderr)
    error.output = None  # Traceback이 partial credential stdout을 표현하지 못하게 한다.
    error.stderr = None  # Security raw stderr도 generic runner 오류 밖으로 내보내지 않는다.


def read_keychain_credential(
    keychain_account: str,
    *,
    command_runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> bytearray:
    """
    함수 이름: read_keychain_credential()
    기능: 고정 service의 허용 account 하나를 출력 없이 mutable ASCII buffer로 읽는다.
    인자: keychain_account -> api-key 또는 api-secret 고정 account
        command_runner -> subprocess.run 호환 주입 callable
    반환값: 출력하거나 파일에 기록하면 안 되는 credential bytearray
    작성 날짜: 2026/08/31
    """
    if keychain_account not in {
        KEYCHAIN_API_KEY_ACCOUNT,
        KEYCHAIN_API_SECRET_ACCOUNT,
    }:
        raise TestnetKeychainRunnerError()
    if not callable(command_runner):
        raise TestnetKeychainRunnerError()

    # Password는 argv가 아닌 security의 captured stdout으로만 읽고 stdin과 inherited FD를 닫는다.
    security_command = (
        KEYCHAIN_SECURITY_COMMAND,
        "find-generic-password",
        "-s",
        KEYCHAIN_SERVICE,
        "-a",
        keychain_account,
        "-w",
    )
    try:
        process_result = command_runner(
            security_command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            shell=False,
            close_fds=True,
            timeout=KEYCHAIN_READ_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        _clear_timeout_output(error)  # Partial output을 고정 오류 생성 전에 폐기한다.
        raise TestnetKeychainRunnerError() from None
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        raise TestnetKeychainRunnerError() from None

    credential_buffer = bytearray()
    try:
        # Non-zero status와 bytes가 아닌 test double output은 credential로 해석하지 않는다.
        if process_result.returncode != 0 or not isinstance(
            process_result.stdout,
            (bytes, bytearray),
        ):
            raise TestnetKeychainRunnerError()
        credential_buffer.extend(process_result.stdout)

        # macOS security가 붙인 마지막 line ending만 제거하고 내부 개행은 검증에서 거부한다.
        if credential_buffer.endswith(b"\n"):
            credential_buffer.pop()
            if credential_buffer.endswith(b"\r"):
                credential_buffer.pop()
        if (
            len(credential_buffer) < MINIMUM_CREDENTIAL_BYTES
            or len(credential_buffer) > MAXIMUM_CREDENTIAL_BYTES
            or not all(
                0x21 <= byte_value <= 0x7E
                for byte_value in credential_buffer
            )
        ):
            raise TestnetKeychainRunnerError()
        return credential_buffer
    except TestnetKeychainRunnerError:
        zeroize_secret_buffer(credential_buffer)  # Invalid credential도 caller로 반환하지 않는다.
        raise
    finally:
        _clear_captured_output(process_result)  # Captured immutable bytes 참조도 즉시 제거한다.


def build_unittest_command(
    mode: str,
    *,
    python_executable: Path,
) -> tuple[str, ...]:
    """
    함수 이름: build_unittest_command()
    기능: 허용 mode를 정확히 하나의 Testnet unittest module argv로 변환한다.
    인자: mode -> read-only 또는 phase13-public-case2
        python_executable -> 현재 runner와 같은 검증된 Python executable
    반환값: shell을 사용하지 않는 exact execve argv tuple
    작성 날짜: 2026/08/31
    """
    test_module = MODE_TEST_MODULES.get(mode)
    if test_module is None or not isinstance(python_executable, Path):
        raise TestnetKeychainRunnerError()
    if not python_executable.is_absolute():
        raise TestnetKeychainRunnerError()

    # `-B`와 exact module 하나만 사용해 discovery나 caller 제공 unittest selector를 차단한다.
    return (
        os.fspath(python_executable),
        "-B",
        "-m",
        "unittest",
        "-q",
        "-f",
        test_module,
    )


def validate_baseline_history_path(
    raw_baseline_history_path: str | None,
) -> Path | None:
    """
    함수 이름: validate_baseline_history_path()
    기능: Optional baseline을 canonical workspace history.jsonl regular file로 제한한다.
    인자: raw_baseline_history_path -> CLI absolute path 원문 또는 option 생략을 뜻하는 None
    반환값: 검증된 resolved baseline Path 또는 None
    작성 날짜: 2026/08/31
    """
    if raw_baseline_history_path is None:
        return None  # Fresh account의 read-only와 actual run은 baseline key를 갖지 않는다.
    if (
        not isinstance(raw_baseline_history_path, str)
        or not raw_baseline_history_path
        or raw_baseline_history_path != raw_baseline_history_path.strip()
    ):
        raise TestnetKeychainRunnerError()

    # 파일 접근 전에 absolute path와 canonical history file name을 먼저 고정한다.
    candidate_path = Path(raw_baseline_history_path)
    if (
        not candidate_path.is_absolute()
        or candidate_path.name != BASELINE_HISTORY_FILE_NAME
        or candidate_path.is_symlink()
        or not candidate_path.is_file()
    ):
        raise TestnetKeychainRunnerError()

    # Artifact root 자체와 resolved target을 검사해 symlink escape나 외부 history를 거부한다.
    try:
        if BASELINE_ARTIFACT_ROOT.is_symlink() or not BASELINE_ARTIFACT_ROOT.is_dir():
            raise TestnetKeychainRunnerError()
        resolved_artifact_root = BASELINE_ARTIFACT_ROOT.resolve(strict=True)
        resolved_candidate_path = candidate_path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise TestnetKeychainRunnerError() from None
    if (
        resolved_candidate_path.name != BASELINE_HISTORY_FILE_NAME
        or resolved_artifact_root not in resolved_candidate_path.parents
    ):
        raise TestnetKeychainRunnerError()
    return resolved_candidate_path  # Child에는 검증 후 symlink가 제거된 canonical path만 전달한다.


def _open_verified_baseline_file(
    baseline_file_path: Path,
) -> tuple[int, str]:
    """
    함수 이름: _open_verified_baseline_file()
    기능: Baseline 구성 regular file 하나를 단일 inode로 고정하고 exact SHA-256을 계산한다.
    인자: baseline_file_path -> symlink가 아닌 owner-only source file
    반환값: exec 상속이 허용된 descriptor와 원본 byte digest tuple
    작성 날짜: 2026/08/31
    """
    if not isinstance(baseline_file_path, Path):
        raise TestnetKeychainRunnerError()
    if baseline_file_path.is_symlink() or not baseline_file_path.is_file():
        raise TestnetKeychainRunnerError()

    descriptor = -1
    descriptor_approved = False
    try:
        # O_NOFOLLOW와 fstat/path identity 대조로 validation 뒤 leaf 교체를 같은 open에서 차단한다.
        open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(baseline_file_path, open_flags)
        descriptor_stat_before = os.fstat(descriptor)
        path_stat_before = os.stat(
            baseline_file_path,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(descriptor_stat_before.st_mode)
            or not stat.S_ISREG(path_stat_before.st_mode)
            or descriptor_stat_before.st_nlink != 1
            or path_stat_before.st_nlink != 1
            or descriptor_stat_before.st_uid != os.getuid()
            or stat.S_IMODE(descriptor_stat_before.st_mode) & 0o077
            or descriptor_stat_before.st_size <= 0
            or (descriptor_stat_before.st_dev, descriptor_stat_before.st_ino)
            != (path_stat_before.st_dev, path_stat_before.st_ino)
        ):
            raise TestnetKeychainRunnerError()

        # Digest는 descriptor에서만 읽고 전후 metadata가 같을 때만 child snapshot으로 승인한다.
        digest_builder = hashlib.sha256()
        while True:
            baseline_chunk = os.read(
                descriptor,
                BASELINE_HISTORY_READ_CHUNK_BYTES,
            )
            if not baseline_chunk:
                break
            digest_builder.update(baseline_chunk)
        descriptor_stat_after = os.fstat(descriptor)
        path_stat_after = os.stat(
            baseline_file_path,
            follow_symlinks=False,
        )
        stable_stat_fields_before = (
            descriptor_stat_before.st_dev,
            descriptor_stat_before.st_ino,
            descriptor_stat_before.st_size,
            descriptor_stat_before.st_mtime_ns,
            descriptor_stat_before.st_ctime_ns,
        )
        stable_stat_fields_after = (
            descriptor_stat_after.st_dev,
            descriptor_stat_after.st_ino,
            descriptor_stat_after.st_size,
            descriptor_stat_after.st_mtime_ns,
            descriptor_stat_after.st_ctime_ns,
        )
        if (
            stable_stat_fields_before != stable_stat_fields_after
            or (descriptor_stat_after.st_dev, descriptor_stat_after.st_ino)
            != (path_stat_after.st_dev, path_stat_after.st_ino)
        ):
            raise TestnetKeychainRunnerError()

        os.lseek(descriptor, 0, os.SEEK_SET)
        os.set_inheritable(descriptor, True)  # Exact inode만 fixed unittest process image로 넘긴다.
        descriptor_approved = True
        return descriptor, digest_builder.hexdigest()
    except (OSError, RuntimeError, TypeError, ValueError):
        raise TestnetKeychainRunnerError() from None
    finally:
        if descriptor >= 0 and not descriptor_approved:
            os.close(descriptor)  # 승인 완료 전에 실패한 descriptor는 credential 조회 전 닫는다.


def open_verified_baseline_history(
    baseline_history_path: Path,
) -> VerifiedBaselineHistory:
    """
    함수 이름: open_verified_baseline_history()
    기능: Closed baseline history와 optional pending journal을 inode-pinned snapshot으로 연다.
    인자: baseline_history_path -> workspace artifact 아래 검증할 history.jsonl
    반환값: history·pending inherited descriptor와 exact digest
    작성 날짜: 2026/08/31
    """
    if not isinstance(baseline_history_path, Path):
        raise TestnetKeychainRunnerError()
    validated_path = validate_baseline_history_path(
        os.fspath(baseline_history_path)
    )
    if validated_path is None:
        raise TestnetKeychainRunnerError()
    pending_path = validated_path.with_name(
        f"{validated_path.name}.pending-orders.jsonl"
    )
    manual_control_path = validated_path.with_name(
        f"{validated_path.name}.manual-kill-control.jsonl"
    )
    if manual_control_path.exists() or manual_control_path.is_symlink():
        raise TestnetKeychainRunnerError()

    history_descriptor = -1
    pending_descriptor: int | None = None
    try:
        # History와 append-only pending journal을 각각 pin해 child semantic replay가 같은 bytes를 본다.
        history_descriptor, history_sha256 = _open_verified_baseline_file(
            validated_path
        )
        pending_sha256: str | None = None
        if pending_path.exists() or pending_path.is_symlink():
            pending_descriptor, pending_sha256 = _open_verified_baseline_file(
                pending_path
            )

        # 마지막 path identity 대조 뒤의 교체는 inherited descriptors가 가리키는 snapshot을 못 바꾼다.
        history_path_stat = os.stat(validated_path, follow_symlinks=False)
        history_descriptor_stat = os.fstat(history_descriptor)
        pending_presence_matches = (
            pending_descriptor is not None
        ) is pending_path.exists()
        if (
            (history_path_stat.st_dev, history_path_stat.st_ino)
            != (history_descriptor_stat.st_dev, history_descriptor_stat.st_ino)
            or not pending_presence_matches
            or manual_control_path.exists()
            or manual_control_path.is_symlink()
        ):
            raise TestnetKeychainRunnerError()
        if pending_descriptor is not None:
            pending_path_stat = os.stat(
                pending_path,
                follow_symlinks=False,
            )
            pending_descriptor_stat = os.fstat(pending_descriptor)
            if (
                pending_path_stat.st_dev,
                pending_path_stat.st_ino,
            ) != (
                pending_descriptor_stat.st_dev,
                pending_descriptor_stat.st_ino,
            ):
                raise TestnetKeychainRunnerError()

        return VerifiedBaselineHistory(
            descriptor=history_descriptor,
            sha256=history_sha256,
            pending_descriptor=pending_descriptor,
            pending_sha256=pending_sha256,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        if history_descriptor >= 0:
            close_verified_baseline_history(
                VerifiedBaselineHistory(
                    descriptor=history_descriptor,
                    sha256="0" * 64,
                    pending_descriptor=pending_descriptor,
                    pending_sha256=(
                        None if pending_descriptor is None else "0" * 64
                    ),
                )
            )
        raise TestnetKeychainRunnerError() from None


def close_verified_baseline_history(
    verified_baseline_history: VerifiedBaselineHistory | None,
) -> None:
    """
    함수 이름: close_verified_baseline_history()
    기능: Exec가 반환한 실패 경로에서 inherited baseline descriptor를 best-effort로 닫는다.
    인자: verified_baseline_history -> open 결과 또는 baseline 미사용을 뜻하는 None
    반환값: 없음
    작성 날짜: 2026/08/31
    """
    if verified_baseline_history is None:
        return  # Baseline을 열지 않은 fresh mode에는 정리할 descriptor가 없다.
    # History와 optional pending descriptor를 모두 내려 exec 실패가 FD leak로 남지 않게 한다.
    descriptors = (
        verified_baseline_history.descriptor,
        verified_baseline_history.pending_descriptor,
    )
    for descriptor in descriptors:
        if descriptor is None:
            continue
        try:
            os.set_inheritable(descriptor, False)
        except OSError:
            pass  # 이미 닫힌 injected descriptor도 secret cleanup을 방해하지 않는다.
        try:
            os.close(descriptor)
        except OSError:
            pass  # Cleanup 자체의 상세 OS 오류는 fixed runner error 밖으로 노출하지 않는다.


def _decode_credential(secret_buffer: bytearray) -> str:
    """
    함수 이름: _decode_credential()
    기능: 이미 검증한 mutable ASCII credential을 execve environment용 문자열로 변환한다.
    인자: secret_buffer -> Keychain에서 읽은 credential bytearray
    반환값: child environment에서만 사용할 credential 문자열
    작성 날짜: 2026/08/31
    """
    if not isinstance(secret_buffer, bytearray) or not (
        MINIMUM_CREDENTIAL_BYTES <= len(secret_buffer) <= MAXIMUM_CREDENTIAL_BYTES
    ):
        raise TestnetKeychainRunnerError()
    if not all(0x21 <= byte_value <= 0x7E for byte_value in secret_buffer):
        raise TestnetKeychainRunnerError()

    # Decode 오류 원문이 exception에 포함되지 않도록 검증 뒤 ASCII conversion만 수행한다.
    try:
        return secret_buffer.decode("ascii")
    except UnicodeError:
        raise TestnetKeychainRunnerError() from None


def build_child_environment(
    mode: str,
    *,
    api_key_buffer: bytearray,
    api_secret_buffer: bytearray,
    verified_baseline_history: VerifiedBaselineHistory | None = None,
) -> dict[str, str]:
    """
    함수 이름: build_child_environment()
    기능: 상위 process 환경을 상속하지 않는 mode별 최소 Testnet child 환경을 만든다.
    인자: mode -> read-only 또는 phase13-public-case2
        api_key_buffer -> 출력 금지 Testnet API key buffer
        api_secret_buffer -> 출력 금지 Testnet API secret buffer
        verified_baseline_history -> optional inode-pinned closed history descriptor
    반환값: execve에만 전달할 fixed-key environment dictionary
    작성 날짜: 2026/08/31
    """
    if mode not in MODE_TEST_MODULES:
        raise TestnetKeychainRunnerError()
    if verified_baseline_history is not None and not isinstance(
        verified_baseline_history,
        VerifiedBaselineHistory,
    ):
        raise TestnetKeychainRunnerError()

    # Python import와 안전 opt-in에 필요한 고정 key만 새 mapping에 넣어 hostile parent 값을 버린다.
    child_environment = {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.fspath(BACKEND_SOURCE_ROOT),
        "PYTHONUNBUFFERED": "1",
        "PYTHONWARNINGS": "error",
        BINANCE_RUN_TESTNET_ENV: "1",
        BINANCE_TESTNET_API_KEY_ENV: _decode_credential(api_key_buffer),
        BINANCE_TESTNET_API_SECRET_ENV: _decode_credential(api_secret_buffer),
        BINANCE_RUN_TESTNET_ORDERS_ENV: (
            "0" if mode == "read-only" else "1"
        ),
        BINANCE_RUN_PHASE13_PUBLIC_CASE2_ENV: (
            "0" if mode == "read-only" else "1"
        ),
    }
    if mode == "phase13-public-case2":
        child_environment[BINANCE_TESTNET_MAX_NOTIONAL_ENV] = "100"
    if verified_baseline_history is not None:
        descriptor_text = str(verified_baseline_history.descriptor)
        digest_text = verified_baseline_history.sha256
        pending_descriptor = verified_baseline_history.pending_descriptor
        pending_digest = verified_baseline_history.pending_sha256
        if (
            verified_baseline_history.descriptor < 0
            or len(digest_text) != 64
            or any(
                character not in "0123456789abcdef"
                for character in digest_text
            )
            or (pending_descriptor is None) is not (pending_digest is None)
            or (
                pending_descriptor is not None
                and (
                    pending_descriptor < 0
                    or len(pending_digest) != 64
                    or any(
                        character not in "0123456789abcdef"
                        for character in pending_digest
                    )
                )
            )
        ):
            raise TestnetKeychainRunnerError()
        child_environment[BINANCE_TESTNET_BASELINE_HISTORY_FD_ENV] = descriptor_text
        child_environment[BINANCE_TESTNET_BASELINE_HISTORY_SHA256_ENV] = digest_text
        if pending_descriptor is not None and pending_digest is not None:
            child_environment[BINANCE_TESTNET_BASELINE_PENDING_FD_ENV] = str(
                pending_descriptor
            )
            child_environment[BINANCE_TESTNET_BASELINE_PENDING_SHA256_ENV] = (
                pending_digest
            )
        # Path 대신 inherited descriptor·digest pair만 전달해 validation→exec 사이 교체를 제거한다.
    return child_environment  # Read-only mode에는 notional cap key 자체가 존재하지 않는다.


def harden_runner_process() -> None:
    """
    함수 이름: harden_runner_process()
    기능: Credential 조회 전에 core dump를 끄고 이후 artifact의 기본 권한을 owner-only로 제한한다.
    인자: 없음
    반환값: 없음
    작성 날짜: 2026/08/31
    """
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        os.umask(0o077)  # Exec 뒤 unittest가 만드는 임시 artifact에도 owner-only 기본 권한을 유지한다.
    except (OSError, ValueError):
        raise TestnetKeychainRunnerError() from None


def execute_testnet_mode(
    mode: str,
    *,
    baseline_history_path: Path | None = None,
    credential_reader: Callable[[str], bytearray] = read_keychain_credential,
    process_executor: Callable[
        [str, tuple[str, ...], Mapping[str, str]],
        object,
    ] = os.execve,
    change_directory: Callable[[Path], object] = os.chdir,
    process_hardener: Callable[[], object] = harden_runner_process,
    python_executable: Path | None = None,
) -> None:
    """
    함수 이름: execute_testnet_mode()
    기능: 두 Keychain item을 같은 process에서 읽고 fixed unittest process image로 교체한다.
    인자: mode -> 허용된 fixed Testnet 실행 mode
        baseline_history_path -> optional verified baseline history.jsonl
        credential_reader -> account별 memory-only credential reader
        process_executor -> os.execve 호환 주입 callable
        change_directory -> backend root로 이동할 주입 callable
        process_hardener -> core dump와 file mode를 제한할 주입 callable
        python_executable -> 실행할 Python 또는 현재 executable을 뜻하는 None
    반환값: 성공한 execve는 반환하지 않음
    작성 날짜: 2026/08/31
    """
    if mode not in MODE_TEST_MODULES:
        raise TestnetKeychainRunnerError()
    if baseline_history_path is not None and not isinstance(
        baseline_history_path,
        Path,
    ):
        raise TestnetKeychainRunnerError()
    if not all(
        callable(candidate)
        for candidate in (
            credential_reader,
            process_executor,
            change_directory,
            process_hardener,
        )
    ):
        raise TestnetKeychainRunnerError()

    # 현재 interpreter와 fixed backend tree가 실제 local 실행 경계인지 credential 조회 전에 확인한다.
    if python_executable is not None and not isinstance(python_executable, Path):
        raise TestnetKeychainRunnerError()
    selected_python = (
        Path(sys.executable)
        if python_executable is None
        else python_executable
    )
    test_module_path = BACKEND_ROOT / Path(
        *MODE_TEST_MODULES[mode].split(".")
    ).with_suffix(".py")
    if (
        not selected_python.is_absolute()
        or not selected_python.is_file()
        or not os.access(selected_python, os.X_OK)
        or not BACKEND_SOURCE_ROOT.is_dir()
        or not test_module_path.is_file()
    ):
        raise TestnetKeychainRunnerError()
    command = build_unittest_command(
        mode,
        python_executable=selected_python,
    )

    api_key_buffer = bytearray()
    api_secret_buffer = bytearray()
    child_environment: dict[str, str] = {}
    verified_baseline_history: VerifiedBaselineHistory | None = None
    try:
        # Core dump를 닫고 baseline inode를 고정한 뒤에만 두 fixed Keychain account를 읽는다.
        process_hardener()
        if baseline_history_path is not None:
            verified_baseline_history = open_verified_baseline_history(
                baseline_history_path
            )
        selected_api_key_buffer = credential_reader(KEYCHAIN_API_KEY_ACCOUNT)
        if not isinstance(selected_api_key_buffer, bytearray):
            raise TestnetKeychainRunnerError()
        api_key_buffer = selected_api_key_buffer  # 검증된 mutable buffer만 cleanup 변수에 보존한다.
        selected_api_secret_buffer = credential_reader(KEYCHAIN_API_SECRET_ACCOUNT)
        if not isinstance(selected_api_secret_buffer, bytearray):
            raise TestnetKeychainRunnerError()
        api_secret_buffer = selected_api_secret_buffer  # 두 번째 값도 같은 zeroize 계약에 넣는다.

        # Exec environment를 만든 직후 mutable 원본을 지워 process image에는 한 사본만 남긴다.
        child_environment = build_child_environment(
            mode,
            api_key_buffer=api_key_buffer,
            api_secret_buffer=api_secret_buffer,
            verified_baseline_history=verified_baseline_history,
        )
        zeroize_secret_buffer(api_key_buffer)
        zeroize_secret_buffer(api_secret_buffer)

        # Backend root와 exact argv/env만 사용하며 shell이나 새 parent process를 만들지 않는다.
        change_directory(BACKEND_ROOT)
        process_executor(
            os.fspath(selected_python),
            command,
            child_environment,
        )
        raise TestnetKeychainRunnerError()  # os.execve가 반환하는 비정상 test double도 성공 처리하지 않는다.
    except TestnetKeychainRunnerError:
        raise
    except Exception:
        # 주입 경계의 예상 밖 exception도 message나 chain을 노출하지 않고 고정 오류로 닫는다.
        raise TestnetKeychainRunnerError() from None
    finally:
        # Exec 실패에서 credential 문자열 참조를 먼저 교체한 뒤 mapping과 mutable buffer를 모두 폐기한다.
        if BINANCE_TESTNET_API_KEY_ENV in child_environment:
            child_environment[BINANCE_TESTNET_API_KEY_ENV] = ""
        if BINANCE_TESTNET_API_SECRET_ENV in child_environment:
            child_environment[BINANCE_TESTNET_API_SECRET_ENV] = ""
        child_environment.clear()
        zeroize_secret_buffer(api_key_buffer)
        zeroize_secret_buffer(api_secret_buffer)
        close_verified_baseline_history(verified_baseline_history)


def parse_arguments(
    argument_values: Sequence[str] | None = None,
) -> TestnetRunnerArguments:
    """
    함수 이름: parse_arguments()
    기능: Raw argument를 오류에 반사하지 않고 정확히 하나의 허용 mode만 선택한다.
    인자: argument_values -> 명시 인자 또는 실제 argv를 뜻하는 None
    반환값: 검증된 fixed mode와 optional baseline을 가진 immutable arguments
    작성 날짜: 2026/08/31
    """
    selected_arguments = tuple(
        sys.argv[1:] if argument_values is None else argument_values
    )
    if not selected_arguments or selected_arguments[0] not in MODE_TEST_MODULES:
        raise TestnetKeychainRunnerError()
    if len(selected_arguments) == 1:
        return TestnetRunnerArguments(selected_arguments[0], None)
    if len(selected_arguments) != 3 or selected_arguments[1] != "--baseline-history":
        raise TestnetKeychainRunnerError()

    # Optional 위치 하나에는 workspace 아래 verified history의 canonical path만 허용한다.
    baseline_history_path = validate_baseline_history_path(selected_arguments[2])
    return TestnetRunnerArguments(
        selected_arguments[0],
        baseline_history_path,
    )  # Credential이나 unittest selector를 받는 추가 CLI 위치는 없다.


def main(argument_values: Sequence[str] | None = None) -> int:
    """
    함수 이름: main()
    기능: 허용 mode를 secure runner로 exec하고 모든 사전 실패를 고정 stderr로 보고한다.
    인자: argument_values -> 명시 인자 또는 실제 argv를 뜻하는 None
    반환값: exec 전에 실패하면 exit status 2
    작성 날짜: 2026/08/31
    """
    try:
        selected_arguments = parse_arguments(argument_values)
        execute_testnet_mode(
            selected_arguments.mode,
            baseline_history_path=selected_arguments.baseline_history_path,
        )
    except TestnetKeychainRunnerError:
        # Account별 상태, command raw output, path와 credential은 한 고정 문장에도 포함하지 않는다.
        print(
            "testnet-keychain-runner: ERROR: secure execution unavailable.",
            file=sys.stderr,
        )
        return 2
    return 2  # 정상 os.execve는 반환하지 않으므로 도달 가능한 반환도 성공으로 해석하지 않는다.


if __name__ == "__main__":
    raise SystemExit(main())
