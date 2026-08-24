"""Phase 12 signed artifact verifier의 signature, loader와 redaction 계약을 검증한다."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import plistlib
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from unittest.mock import patch

from scripts.verify_phase12_signed_artifacts import (
    COMMAND_TIMEOUT_SECONDS,
    EXPECTED_SIDECAR_NAME,
    LOADER_SMOKE_TIMEOUT_SECONDS,
    MAXIMUM_COMMAND_OUTPUT_BYTES,
    PYINSTALLER_COOKIE_FORMAT,
    PYINSTALLER_COOKIE_MAGIC,
    PYINSTALLER_COOKIE_LENGTH,
    PYINSTALLER_TOC_ENTRY_FORMAT,
    PYINSTALLER_TOC_ENTRY_LENGTH,
    SignedArtifactVerificationError,
    create_sanitized_environment,
    main,
    parse_arguments,
    run_command,
    verify_signed_artifacts,
)


TEST_TEAM_ID = "A1B2C3D4E5"
OTHER_TEAM_ID = "Z9Y8X7W6V5"
TEST_IDENTITY = f"Developer ID Application: Phase Twelve Release ({TEST_TEAM_ID})"
TEST_VERSION = "0.1.0"
TEST_BUILD_VERSION = "20260824.1"
TEST_COMMIT = "a" * 40
TEST_MAIN_EXECUTABLE_NAME = "binance-auto-trader"


class SignedArtifactFixture:
    """
    클래스 이름: SignedArtifactFixture
    기능: isolated signed app 구조와 executable·DMG placeholder를 test마다 생성한다.
    작성 날짜: 2026/08/24
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: temporary root 아래 verifier가 요구하는 최소 app bundle을 만든다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root_path = Path(self.temporary_directory.name).resolve(strict=True)
        self.app_path = self.root_path / "candidate-private-path.app"
        self.contents_path = self.app_path / "Contents"
        self.macos_path = self.contents_path / "MacOS"
        self.info_plist_path = self.contents_path / "Info.plist"
        self.main_executable_path = (
            self.macos_path / TEST_MAIN_EXECUTABLE_NAME
        )
        self.sidecar_path = self.macos_path / EXPECTED_SIDECAR_NAME
        self.dmg_path = self.root_path / "candidate-private-path.dmg"

        (self.contents_path / "_CodeSignature").mkdir(parents=True)
        self.macos_path.mkdir()
        (self.contents_path / "_CodeSignature" / "CodeResources").write_bytes(
            b"signed-resources-placeholder"
        )
        self.write_info_plist()
        self.write_executable(self.main_executable_path)
        self.write_sidecar(self.sidecar_path)
        self.dmg_path.write_bytes(b"signed-dmg-placeholder")

    def write_info_plist(
        self,
        *,
        minimum_system_version: str = "11.0",
        bundle_executable: str = TEST_MAIN_EXECUTABLE_NAME,
        release_version: str = TEST_VERSION,
        build_version: str = TEST_BUILD_VERSION,
    ) -> None:
        """
        함수 이름: write_info_plist()
        기능: case별 minimum version과 main name을 가진 valid XML plist를 쓴다.
        인자: minimum_system_version -> LSMinimumSystemVersion 값
            bundle_executable -> CFBundleExecutable 값
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        with self.info_plist_path.open("wb") as info_plist_file:
            plistlib.dump(
                {
                    "CFBundleExecutable": bundle_executable,
                    "CFBundleIdentifier": "com.binance-auto.trader",
                    "CFBundlePackageType": "APPL",
                    "CFBundleShortVersionString": release_version,
                    "CFBundleVersion": build_version,
                    "LSMinimumSystemVersion": minimum_system_version,
                },
                info_plist_file,
            )

    def write_executable(self, executable_path: Path) -> None:
        """
        함수 이름: write_executable()
        기능: 실제 실행하지 않는 regular executable fixture를 만든다.
        인자: executable_path -> 생성할 main 또는 sidecar path
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        executable_path.write_bytes(
            b"#!/bin/sh\nexit 1\nBINANCE_AUTO_RELEASE_COMMIT="
            + TEST_COMMIT.encode("ascii")
            + b"\n"
        )
        executable_path.chmod(0o755)

    def write_sidecar(
        self,
        executable_path: Path,
        *,
        include_native_extension: bool = True,
        provenance_commit: str = TEST_COMMIT,
        provenance_version: str = TEST_VERSION,
        provenance_build_version: str = TEST_BUILD_VERSION,
        provenance_bytes: bytes | None = None,
    ) -> None:
        """libpython과 native extension을 가진 최소 PyInstaller CArchive를 만든다."""

        payload_definitions = [
            ("libpython3.11.dylib", b"fake-mach-o-libpython-payload", b"b"),
            (
                "phase12-release-provenance.json",
                (
                    provenance_bytes
                    if provenance_bytes is not None
                    else json.dumps(
                        {
                            "schema_version": 1,
                            "commit": provenance_commit,
                            "version": provenance_version,
                            "build_version": provenance_build_version,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    + b"\n"
                ),
                # package_sidecar writes canonical JSON with one trailing LF.
                b"x",
            ),
        ]
        if include_native_extension:
            payload_definitions.append(
                (
                    "lib-dynload/_ssl.cpython-311-darwin.so",
                    b"fake-mach-o-extension",
                    b"b",
                )
            )
        compressed_payloads: list[bytes] = []
        toc_entries: list[bytes] = []
        payload_offset = 0
        for payload_name, payload_bytes, payload_typecode in payload_definitions:
            compressed_bytes = zlib.compress(payload_bytes)
            compressed_payloads.append(compressed_bytes)
            encoded_name = payload_name.encode("utf-8") + b"\0"
            entry_length = PYINSTALLER_TOC_ENTRY_LENGTH + len(encoded_name)
            entry_length += (-entry_length) % 16
            padded_name = encoded_name.ljust(
                entry_length - PYINSTALLER_TOC_ENTRY_LENGTH,
                b"\0",
            )
            toc_entries.append(
                struct.pack(
                    PYINSTALLER_TOC_ENTRY_FORMAT,
                    entry_length,
                    payload_offset,
                    len(compressed_bytes),
                    len(payload_bytes),
                    1,
                    payload_typecode,
                )
                + padded_name
            )
            payload_offset += len(compressed_bytes)

        payload_region = b"".join(compressed_payloads)
        toc_region = b"".join(toc_entries)
        python_library_field = b"libpython3.11.dylib".ljust(64, b"\0")
        archive_length = len(payload_region) + len(toc_region) + PYINSTALLER_COOKIE_LENGTH
        cookie = struct.pack(
            PYINSTALLER_COOKIE_FORMAT,
            PYINSTALLER_COOKIE_MAGIC,
            archive_length,
            len(payload_region),
            len(toc_region),
            311,
            python_library_field,
        )
        executable_path.write_bytes(
            b"#!/bin/sh\nexit 1\n" + payload_region + toc_region + cookie
        )
        executable_path.chmod(0o755)

    def cleanup(self) -> None:
        """
        함수 이름: cleanup()
        기능: test가 소유한 isolated temporary root를 정리한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        self.temporary_directory.cleanup()


class FakeCommandRunner:
    """
    클래스 이름: FakeCommandRunner
    기능: verifier argv를 분류해 deterministic Apple tool과 loader 결과를 반환한다.
    작성 날짜: 2026/08/24
    """

    def __init__(self, artifact_fixture: SignedArtifactFixture) -> None:
        """
        함수 이름: __init__()
        기능: artifact role mapping, override와 invocation capture를 초기화한다.
        인자: artifact_fixture -> path role을 제공하는 isolated fixture
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        self.artifact_fixture = artifact_fixture
        self.overrides: dict[
            tuple[str, str],
            subprocess.CompletedProcess[bytes] | BaseException,
        ] = {}
        self.invocations: list[
            tuple[list[str], Path, dict[str, str], float]
        ] = []
        self.last_mount_path: Path | None = None
        self.mounted_release_version = TEST_VERSION
        self.mounted_build_version = TEST_BUILD_VERSION
        self.mounted_main_bytes: bytes | None = None
        self.mounted_code_resources_bytes: bytes | None = None
        self.applications_target = "/Applications"
        self.add_extra_app = False
        self.mount_writable = False
        self.mutate_dmg_on_attach = False

    def set_result(
        self,
        operation: str,
        artifact_role: str,
        *,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> None:
        """
        함수 이름: set_result()
        기능: 한 logical operation의 fake completed result를 교체한다.
        인자: operation -> codesign, metadata, file, vtool, loader 또는 DMG operation
            artifact_role -> app, main, sidecar 또는 dmg
            returncode -> fake exit status
            stdout -> captured stdout
            stderr -> captured stderr
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        self.overrides[(operation, artifact_role)] = subprocess.CompletedProcess(
            [operation, artifact_role],
            returncode,
            stdout,
            stderr,
        )

    def set_exception(
        self,
        operation: str,
        artifact_role: str,
        exception: BaseException,
    ) -> None:
        """
        함수 이름: set_exception()
        기능: 한 logical invocation에서 timeout 등 fake exception을 발생시킨다.
        인자: operation -> logical command 이름
            artifact_role -> app, main, sidecar 또는 dmg
            exception -> 발생시킬 exception
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        self.overrides[(operation, artifact_role)] = exception

    def _artifact_role(self, artifact_path: str) -> str:
        """
        함수 이름: _artifact_role()
        기능: exact fixture path를 stable non-secret role로 분류한다.
        인자: artifact_path -> production command의 마지막 path argument
        반환값: app, main, sidecar 또는 dmg role
        작성 날짜: 2026/08/24
        """

        role_by_path = {
            str(self.artifact_fixture.app_path): "app",
            str(self.artifact_fixture.main_executable_path): "main",
            str(self.artifact_fixture.sidecar_path): "sidecar",
            str(self.artifact_fixture.dmg_path): "dmg",
        }
        if artifact_path in role_by_path:
            return role_by_path[artifact_path]
        artifact = Path(artifact_path)
        if artifact.name.startswith("native-") and artifact.suffix in {
            ".dylib",
            ".so",
        }:
            return "native"
        if self.last_mount_path is not None:
            mounted_app = self.last_mount_path / self.artifact_fixture.app_path.name
            mounted_roles = {
                str(mounted_app): "mounted-app",
                str(
                    mounted_app
                    / "Contents"
                    / "MacOS"
                    / TEST_MAIN_EXECUTABLE_NAME
                ): "mounted-main",
                str(
                    mounted_app
                    / "Contents"
                    / "MacOS"
                    / EXPECTED_SIDECAR_NAME
                ): "mounted-sidecar",
            }
            if artifact_path in mounted_roles:
                return mounted_roles[artifact_path]
        raise AssertionError("unexpected artifact path")

    def _classify_command(self, command: list[str]) -> tuple[str, str]:
        """
        함수 이름: _classify_command()
        기능: production argv shape를 logical operation과 artifact role로 변환한다.
        인자: command -> verifier가 전달한 exact argv list
        반환값: operation과 artifact role
        작성 날짜: 2026/08/24
        """

        if len(command) == 1:
            return "loader", self._artifact_role(command[0])

        if command[:2] == ["/usr/bin/hdiutil", "attach"]:
            return "attach", "dmg"
        if command[:2] == ["/usr/bin/hdiutil", "detach"]:
            return "detach", "mount"
        if command[:3] == ["/usr/sbin/diskutil", "info", "-plist"]:
            return "diskutil", "mount"

        artifact_role = self._artifact_role(command[-1])
        if command[0] == "/usr/bin/codesign":
            if command[1:5] == [
                "--verify",
                "--deep",
                "--strict",
                "--verbose=4",
            ]:
                return "strict-signature", artifact_role
            if command[1:4] == ["--verify", "--strict", "--verbose=4"]:
                return "dmg-signature", artifact_role
            if command[1] == "-dvvv":
                return "metadata", artifact_role
            if command[1:4] == ["-d", "--entitlements", "-"]:
                return "entitlements", artifact_role
        if command[:2] == ["/usr/bin/file", "-b"]:
            return "file", artifact_role
        if command[:3] == ["/usr/bin/xcrun", "vtool", "-show-build"]:
            return "vtool", artifact_role
        if command[:2] == ["/usr/bin/hdiutil", "verify"]:
            return "hdiutil", artifact_role
        if command[:3] == ["/usr/bin/xcrun", "stapler", "validate"]:
            return "stapler", artifact_role
        if command[:4] == [
            "/usr/sbin/spctl",
            "--assess",
            "--type",
            "execute",
        ]:
            return "app-spctl", artifact_role
        if command[:4] == [
            "/usr/sbin/spctl",
            "--assess",
            "--type",
            "open",
        ]:
            return "spctl", artifact_role
        raise AssertionError("unexpected verifier command shape")

    def _populate_mount(self, mount_path: Path) -> None:
        """fake attach가 fresh DMG builder layout을 exact 재현한다."""

        mounted_app = mount_path / self.artifact_fixture.app_path.name
        shutil.copytree(self.artifact_fixture.app_path, mounted_app)
        mounted_plist_path = mounted_app / "Contents" / "Info.plist"
        with mounted_plist_path.open("rb") as plist_file:
            mounted_plist = plistlib.load(plist_file)
        mounted_plist["CFBundleShortVersionString"] = self.mounted_release_version
        mounted_plist["CFBundleVersion"] = self.mounted_build_version
        with mounted_plist_path.open("wb") as plist_file:
            plistlib.dump(mounted_plist, plist_file)
        if self.mounted_main_bytes is not None:
            mounted_main = (
                mounted_app / "Contents" / "MacOS" / TEST_MAIN_EXECUTABLE_NAME
            )
            mounted_main.write_bytes(self.mounted_main_bytes)
            mounted_main.chmod(0o755)
        if self.mounted_code_resources_bytes is not None:
            (
                mounted_app / "Contents" / "_CodeSignature" / "CodeResources"
            ).write_bytes(self.mounted_code_resources_bytes)
        (mount_path / "Applications").symlink_to(self.applications_target)
        if self.add_extra_app:
            shutil.copytree(mounted_app, mount_path / "stale-copy.app")

    def _clear_mount(self) -> None:
        """fake detach가 test-owned mount contents만 제거한다."""

        if self.last_mount_path is None or not self.last_mount_path.exists():
            return
        for mounted_entry in list(self.last_mount_path.iterdir()):
            if mounted_entry.is_symlink() or mounted_entry.is_file():
                mounted_entry.unlink()
            else:
                shutil.rmtree(mounted_entry)

    def __call__(
        self,
        command: list[str] | tuple[str, ...],
        working_directory: Path,
        process_environment: dict[str, str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[bytes]:
        """
        함수 이름: __call__()
        기능: invocation을 기록하고 override 또는 happy-path 결과를 반환한다.
        인자: command -> production argv sequence
            working_directory -> production cwd
            process_environment -> sanitized environment
            timeout_seconds -> production timeout
        반환값: fake completed process
        작성 날짜: 2026/08/24
        """

        command_list = list(command)
        self.invocations.append(
            (
                command_list,
                working_directory,
                dict(process_environment),
                timeout_seconds,
            )
        )
        operation, artifact_role = self._classify_command(command_list)
        overridden_result = self.overrides.get((operation, artifact_role))
        if isinstance(overridden_result, BaseException):
            raise overridden_result
        if overridden_result is not None:
            return overridden_result

        if operation == "attach":
            mount_argument_index = command_list.index("-mountpoint") + 1
            self.last_mount_path = Path(command_list[mount_argument_index])
            if self.mutate_dmg_on_attach:
                self.artifact_fixture.dmg_path.write_bytes(
                    b"mutated-during-mounted-verification"
                )
            self._populate_mount(self.last_mount_path)
            return subprocess.CompletedProcess(
                command_list,
                0,
                plistlib.dumps(
                    {
                        "system-entities": [
                            {
                                "dev-entry": "/dev/disk99s1",
                                "mount-point": str(self.last_mount_path),
                            }
                        ]
                    }
                ),
                b"",
            )
        if operation == "diskutil":
            return subprocess.CompletedProcess(
                command_list,
                0,
                plistlib.dumps(
                    {
                        "DeviceNode": "/dev/disk99s1",
                        "MountPoint": str(self.last_mount_path),
                        "Writable": self.mount_writable,
                    }
                ),
                b"",
            )
        if operation == "detach":
            self._clear_mount()
            return subprocess.CompletedProcess(command_list, 0, b"", b"")

        if operation == "metadata":
            metadata_output = (
                b"CodeDirectory v=20500 size=512 "
                b"flags=0x10000(runtime) hashes=8+2 location=embedded\n"
                + f"Authority={TEST_IDENTITY}\n".encode("ascii")
                + b"Authority=Developer ID Certification Authority G2\n"
                + b"Authority=Apple Root CA\n"
                + f"TeamIdentifier={TEST_TEAM_ID}\n".encode("ascii")
            )
            return subprocess.CompletedProcess(
                command_list,
                0,
                b"",
                metadata_output,
            )
        if operation == "file":
            return subprocess.CompletedProcess(
                command_list,
                0,
                b"Mach-O 64-bit executable arm64\n",
                b"",
            )
        if operation == "vtool":
            return subprocess.CompletedProcess(
                command_list,
                0,
                b"platform MACOS\n    minos 11.0\n",
                b"",
            )
        if operation == "loader":
            return subprocess.CompletedProcess(
                command_list,
                1,
                b"",
                b"OSError: [Errno 9] Bad file descriptor\n",
            )
        return subprocess.CompletedProcess(command_list, 0, b"", b"")


class PhaseTwelveSignedArtifactTests(unittest.TestCase):
    """
    클래스 이름: PhaseTwelveSignedArtifactTests
    기능: Phase 12 signed app/DMG의 fail-closed gate와 secret-free CLI를 검증한다.
    작성 날짜: 2026/08/24
    """

    def setUp(self) -> None:
        """
        함수 이름: setUp()
        기능: test별 artifact fixture와 default happy-path runner를 만든다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        self.artifacts = SignedArtifactFixture()
        self.command_runner = FakeCommandRunner(self.artifacts)
        self.source_environment = {
            "PATH": "/usr/bin:/bin",
            "APPLE_CERTIFICATE": "certificate-secret",
            "APPLE_CERTIFICATE_PASSWORD": "password-secret",
            "APPLE_API_KEY_PATH": "/private/notary-key.p8",
            "APPLE_FUTURE_PRIVATE_KEY": "future-private-key-secret",
            "BINANCE_TESTNET_API_KEY": "testnet-api-key-secret",
            "BINANCE_TESTNET_API_SECRET": "testnet-api-secret",
            "BINANCE_FUTURE_CREDENTIAL": "future-binance-secret",
            "BINANCE_RUN_TESTNET": "0",
            "NOTARY_PROFILE": "phase12-notary",
            "APPLE_SIGNING_IDENTITY": TEST_IDENTITY,
            "DEVELOPER_DIR": "/private/fake-xcode",
            "TOOLCHAINS": "untrusted-toolchain",
            "DYLD_INSERT_LIBRARIES": "/private/injected.dylib",
            "PYTHONPATH": "/private/injected-python",
            "PYTHONINSPECT": "1",
            "GIT_CONFIG_GLOBAL": "/private/injected-gitconfig",
            "XCODE_XCCONFIG_FILE": "/private/injected.xcconfig",
            "TMPDIR": "/private/injected-temp",
        }

    def tearDown(self) -> None:
        """
        함수 이름: tearDown()
        기능: test가 소유한 temporary artifact tree를 정리한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        self.artifacts.cleanup()

    def _verify(
        self,
        *,
        dmg_path: Path | None = None,
        command_runner: FakeCommandRunner | None = None,
        app_path: Path | None = None,
    ):
        """
        함수 이름: _verify()
        기능: test fixture 기본값으로 production verify_signed_artifacts를 호출한다.
        인자: dmg_path -> optional DMG override
            command_runner -> fake runner override
            app_path -> app path override
        반환값: production SignedArtifactEvidence
        작성 날짜: 2026/08/24
        """

        return verify_signed_artifacts(
            self.artifacts.app_path if app_path is None else app_path,
            TEST_TEAM_ID,
            TEST_IDENTITY,
            TEST_VERSION,
            TEST_BUILD_VERSION,
            TEST_COMMIT,
            dmg_path=dmg_path,
            source_environment=self.source_environment,
            command_runner=(
                self.command_runner
                if command_runner is None
                else command_runner
            ),
        )

    def test_signed_app_happy_path_runs_all_exact_checks_with_sanitized_env(
        self,
    ) -> None:
        """
        함수 이름: test_signed_app_happy_path_runs_all_exact_checks_with_sanitized_env()
        기능: valid app이 strict signature, metadata, architecture, minOS와 loader gate를 통과하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        evidence = self._verify()

        self.assertEqual(evidence.team_id, TEST_TEAM_ID)
        self.assertFalse(evidence.dmg_verified)
        self.assertEqual(evidence.commit, TEST_COMMIT)
        self.assertEqual(len(evidence.provenance_sha256), 64)
        self.assertIsNone(evidence.dmg_sha256)
        self.assertIsNone(evidence.dmg_snapshot)
        self.assertEqual(len(evidence.extracted_native), 2)
        self.assertEqual(
            {native.kind for native in evidence.extracted_native},
            {"libpython", "native-extension"},
        )
        self.assertTrue(
            all(len(native.sha256) == 64 for native in evidence.extracted_native)
        )
        self.assertEqual(len(self.command_runner.invocations), 24)
        strict_signature_commands = [
            invocation[0]
            for invocation in self.command_runner.invocations
            if invocation[0][0:2] == ["/usr/bin/codesign", "--verify"]
            and "--deep" in invocation[0]
        ]
        self.assertEqual(len(strict_signature_commands), 5)
        for command in strict_signature_commands:
            self.assertEqual(
                command[1:5],
                ["--verify", "--deep", "--strict", "--verbose=4"],
            )

        loader_invocation = self.command_runner.invocations[-1]
        self.assertEqual(loader_invocation[0], [str(self.artifacts.sidecar_path)])
        self.assertEqual(loader_invocation[1], self.artifacts.macos_path)
        self.assertEqual(loader_invocation[3], LOADER_SMOKE_TIMEOUT_SECONDS)
        for command, _, process_environment, timeout_seconds in (
            self.command_runner.invocations
        ):
            self.assertIsInstance(command, list)
            self.assertNotIn("APPLE_CERTIFICATE", process_environment)
            self.assertNotIn("APPLE_CERTIFICATE_PASSWORD", process_environment)
            self.assertNotIn("APPLE_API_KEY_PATH", process_environment)
            self.assertNotIn("APPLE_FUTURE_PRIVATE_KEY", process_environment)
            self.assertNotIn("BINANCE_TESTNET_API_KEY", process_environment)
            self.assertNotIn("BINANCE_TESTNET_API_SECRET", process_environment)
            self.assertNotIn("BINANCE_FUTURE_CREDENTIAL", process_environment)
            self.assertEqual(process_environment["BINANCE_RUN_TESTNET"], "0")
            self.assertEqual(
                process_environment["PATH"],
                "/usr/bin:/bin:/usr/sbin:/sbin",
            )
            for removed_control_name in (
                "APPLE_SIGNING_IDENTITY",
                "DEVELOPER_DIR",
                "TOOLCHAINS",
                "DYLD_INSERT_LIBRARIES",
                "PYTHONPATH",
                "PYTHONINSPECT",
                "GIT_CONFIG_GLOBAL",
                "XCODE_XCCONFIG_FILE",
                "TMPDIR",
                "NOTARY_PROFILE",
            ):
                self.assertNotIn(removed_control_name, process_environment)
            self.assertIn(
                timeout_seconds,
                {COMMAND_TIMEOUT_SECONDS, LOADER_SMOKE_TIMEOUT_SECONDS},
            )

    def test_app_and_embedded_structure_fail_before_any_command(self) -> None:
        """
        함수 이름: test_app_and_embedded_structure_fail_before_any_command()
        기능: app symlink, missing executable과 non-executable sidecar를 tool 실행 전에 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        linked_app_path = self.artifacts.root_path / "linked.app"
        linked_app_path.symlink_to(self.artifacts.app_path, target_is_directory=True)
        with self.assertRaises(SignedArtifactVerificationError):
            self._verify(app_path=linked_app_path)
        self.assertEqual(self.command_runner.invocations, [])

        linked_parent = self.artifacts.root_path / "linked-parent"
        linked_parent.symlink_to(self.artifacts.root_path, target_is_directory=True)
        with self.assertRaises(SignedArtifactVerificationError):
            self._verify(
                app_path=linked_parent / self.artifacts.app_path.name,
            )
        self.assertEqual(self.command_runner.invocations, [])

        self.artifacts.main_executable_path.unlink()
        with self.assertRaises(SignedArtifactVerificationError):
            self._verify()
        self.assertEqual(self.command_runner.invocations, [])
        self.artifacts.write_executable(self.artifacts.main_executable_path)

        self.artifacts.sidecar_path.chmod(0o644)
        with self.assertRaises(SignedArtifactVerificationError):
            self._verify()
        self.assertEqual(self.command_runner.invocations, [])

    def test_info_plist_requires_exact_minimum_macos_version(self) -> None:
        """
        함수 이름: test_info_plist_requires_exact_minimum_macos_version()
        기능: plist LSMinimumSystemVersion drift가 signature command 전에 실패하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        self.artifacts.write_info_plist(minimum_system_version="10.15")

        with self.assertRaises(SignedArtifactVerificationError):
            self._verify()
        self.assertEqual(self.command_runner.invocations, [])

    def test_invalid_team_id_fails_before_any_command(self) -> None:
        """
        함수 이름: test_invalid_team_id_fails_before_any_command()
        기능: empty, pseudo와 malformed expected Team ID를 비교 기준으로 사용하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        for invalid_team_id in ("", "-", "short", "a1b2c3d4e5"):
            with self.subTest(invalid_team_id=invalid_team_id):
                with self.assertRaises(SignedArtifactVerificationError):
                    verify_signed_artifacts(
                        self.artifacts.app_path,
                        invalid_team_id,
                        TEST_IDENTITY,
                        TEST_VERSION,
                        TEST_BUILD_VERSION,
                        TEST_COMMIT,
                        command_runner=self.command_runner,
                    )
        self.assertEqual(self.command_runner.invocations, [])

    def test_strict_signature_failure_is_fail_closed(self) -> None:
        """
        함수 이름: test_strict_signature_failure_is_fail_closed()
        기능: outer app 또는 embedded signature 실패가 raw codesign detail 없이 중단되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        raw_secret_output = b"private-path certificate-secret"
        self.command_runner.set_result(
            "strict-signature",
            "sidecar",
            returncode=1,
            stderr=raw_secret_output,
        )

        with self.assertRaises(SignedArtifactVerificationError) as error_context:
            self._verify()
        self.assertNotIn("private-path", str(error_context.exception))
        self.assertNotIn("certificate-secret", str(error_context.exception))

    def test_team_identifier_must_be_single_exact_value(self) -> None:
        """
        함수 이름: test_team_identifier_must_be_single_exact_value()
        기능: missing, mismatched 또는 duplicate TeamIdentifier metadata를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        invalid_metadata_outputs = (
            b"flags=0x10000(runtime)\n",
            (
                b"flags=0x10000(runtime)\n"
                + f"TeamIdentifier={OTHER_TEAM_ID}\n".encode("ascii")
            ),
            (
                b"flags=0x10000(runtime)\n"
                + f"TeamIdentifier={TEST_TEAM_ID}\n".encode("ascii") * 2
            ),
        )
        for invalid_metadata_output in invalid_metadata_outputs:
            with self.subTest(metadata=invalid_metadata_output):
                command_runner = FakeCommandRunner(self.artifacts)
                command_runner.set_result(
                    "metadata",
                    "main",
                    stderr=invalid_metadata_output,
                )
                with self.assertRaises(SignedArtifactVerificationError):
                    self._verify(command_runner=command_runner)

    def test_expected_identity_and_exact_authority_chain_are_required(self) -> None:
        """R-01 identity suffix와 codesign leaf/intermediate/root chain을 exact 결합한다."""

        with self.assertRaises(SignedArtifactVerificationError):
            verify_signed_artifacts(
                self.artifacts.app_path,
                TEST_TEAM_ID,
                f"Developer ID Application: Wrong Team ({OTHER_TEAM_ID})",
                TEST_VERSION,
                TEST_BUILD_VERSION,
                TEST_COMMIT,
                command_runner=self.command_runner,
            )
        self.assertEqual(self.command_runner.invocations, [])

        for invalid_authority_output in (
            (
                b"flags=0x10000(runtime)\n"
                + f"TeamIdentifier={TEST_TEAM_ID}\n".encode("ascii")
            ),
            (
                b"flags=0x10000(runtime)\n"
                + f"Authority=Developer ID Application: Other ({TEST_TEAM_ID})\n".encode(
                    "ascii"
                )
                + b"Authority=Developer ID Certification Authority G2\n"
                + b"Authority=Apple Root CA\n"
                + f"TeamIdentifier={TEST_TEAM_ID}\n".encode("ascii")
            ),
            (
                b"flags=0x10000(runtime)\n"
                + f"Authority={TEST_IDENTITY}\n".encode("ascii")
                + b"Authority=Untrusted Intermediate\n"
                + b"Authority=Apple Root CA\n"
                + f"TeamIdentifier={TEST_TEAM_ID}\n".encode("ascii")
            ),
        ):
            with self.subTest(authority_output=invalid_authority_output):
                command_runner = FakeCommandRunner(self.artifacts)
                command_runner.set_result(
                    "metadata",
                    "app",
                    stderr=invalid_authority_output,
                )
                with self.assertRaises(SignedArtifactVerificationError):
                    self._verify(command_runner=command_runner)

    def test_release_version_build_and_absolute_paths_are_required(self) -> None:
        """manifest version/build drift와 cwd-dependent artifact path를 command 전에 거부한다."""

        for release_version, build_version in (
            ("0.1.1", TEST_BUILD_VERSION),
            (TEST_VERSION, "20260824.2"),
            ("01.0.0", TEST_BUILD_VERSION),
            (TEST_VERSION, "01.2"),
        ):
            with self.subTest(
                release_version=release_version,
                build_version=build_version,
            ):
                with self.assertRaises(SignedArtifactVerificationError):
                    verify_signed_artifacts(
                        self.artifacts.app_path,
                        TEST_TEAM_ID,
                        TEST_IDENTITY,
                        release_version,
                        build_version,
                        TEST_COMMIT,
                        command_runner=self.command_runner,
                    )
        with self.assertRaises(SignedArtifactVerificationError):
            verify_signed_artifacts(
                Path("relative-candidate.app"),
                TEST_TEAM_ID,
                TEST_IDENTITY,
                TEST_VERSION,
                TEST_BUILD_VERSION,
                TEST_COMMIT,
                command_runner=self.command_runner,
            )
        ambiguous_app_path = (
            self.artifacts.root_path
            / "unused-component"
            / ".."
            / self.artifacts.app_path.name
        )
        with self.assertRaises(SignedArtifactVerificationError):
            verify_signed_artifacts(
                ambiguous_app_path,
                TEST_TEAM_ID,
                TEST_IDENTITY,
                TEST_VERSION,
                TEST_BUILD_VERSION,
                TEST_COMMIT,
                command_runner=self.command_runner,
            )
        self.assertEqual(self.command_runner.invocations, [])

    def test_release_commit_marker_is_exact_single_and_streaming(self) -> None:
        """expected lowercase commit 하나만 허용하고 UNVERIFIED/duplicate/drift를 거부한다."""

        with self.assertRaises(SignedArtifactVerificationError):
            verify_signed_artifacts(
                self.artifacts.app_path,
                TEST_TEAM_ID,
                TEST_IDENTITY,
                TEST_VERSION,
                TEST_BUILD_VERSION,
                TEST_COMMIT.upper(),
                command_runner=self.command_runner,
            )
        self.assertEqual(self.command_runner.invocations, [])

        invalid_main_contents = (
            b"BINANCE_AUTO_RELEASE_COMMIT=UNVERIFIED",
            b"BINANCE_AUTO_RELEASE_COMMIT=" + ("b" * 40).encode("ascii"),
            (
                b"BINANCE_AUTO_RELEASE_COMMIT="
                + TEST_COMMIT.encode("ascii")
                + b"\nBINANCE_AUTO_RELEASE_COMMIT="
                + TEST_COMMIT.encode("ascii")
            ),
        )
        for invalid_main_content in invalid_main_contents:
            with self.subTest(invalid_main_content=invalid_main_content[:32]):
                self.artifacts.main_executable_path.write_bytes(invalid_main_content)
                self.artifacts.main_executable_path.chmod(0o755)
                command_runner = FakeCommandRunner(self.artifacts)
                with self.assertRaises(SignedArtifactVerificationError):
                    self._verify(command_runner=command_runner)
                self.assertEqual(command_runner.invocations, [])

        boundary_prefix = b"x" * (1024 * 1024 - 13)
        self.artifacts.main_executable_path.write_bytes(
            boundary_prefix
            + b"BINANCE_AUTO_RELEASE_COMMIT="
            + TEST_COMMIT.encode("ascii")
        )
        self.artifacts.main_executable_path.chmod(0o755)
        evidence = self._verify(command_runner=FakeCommandRunner(self.artifacts))
        self.assertEqual(evidence.team_id, TEST_TEAM_ID)

    def test_hardened_runtime_flag_is_required_on_main_and_sidecar(self) -> None:
        """
        함수 이름: test_hardened_runtime_flag_is_required_on_main_and_sidecar()
        기능: Team ID가 맞아도 runtime flag 없는 executable을 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        self.command_runner.set_result(
            "metadata",
            "sidecar",
            stderr=(
                b"flags=0x0(none)\n"
                + f"TeamIdentifier={TEST_TEAM_ID}\n".encode("ascii")
            ),
        )

        with self.assertRaises(SignedArtifactVerificationError):
            self._verify()

    def test_library_validation_disable_entitlement_presence_is_rejected(
        self,
    ) -> None:
        """
        함수 이름: test_library_validation_disable_entitlement_presence_is_rejected()
        기능: boolean 값과 무관하게 forbidden entitlement key가 있으면 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        self.command_runner.set_result(
            "entitlements",
            "main",
            stdout=(
                b"<key>com.apple.security.cs.disable-library-validation</key>"
                b"<false/>"
            ),
        )

        with self.assertRaises(SignedArtifactVerificationError):
            self._verify()

    def test_x86_only_or_non_macho_executable_is_rejected(self) -> None:
        """
        함수 이름: test_x86_only_or_non_macho_executable_is_rejected()
        기능: file output에 arm64 Mach-O evidence가 없으면 executable을 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        for invalid_file_output in (
            b"Mach-O 64-bit executable x86_64\n",
            b"ASCII text containing arm64\n",
        ):
            with self.subTest(file_output=invalid_file_output):
                command_runner = FakeCommandRunner(self.artifacts)
                command_runner.set_result(
                    "file",
                    "main",
                    stdout=invalid_file_output,
                )
                with self.assertRaises(SignedArtifactVerificationError):
                    self._verify(command_runner=command_runner)

    def test_vtool_requires_exact_minos_on_every_build_record(self) -> None:
        """
        함수 이름: test_vtool_requires_exact_minos_on_every_build_record()
        기능: missing, older 또는 mixed universal minos record를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        for invalid_vtool_output in (
            b"platform MACOS\n",
            b"platform MACOS\n minos 10.15\n",
            b"minos 11.0\nminos 12.0\n",
        ):
            with self.subTest(vtool_output=invalid_vtool_output):
                command_runner = FakeCommandRunner(self.artifacts)
                command_runner.set_result(
                    "vtool",
                    "sidecar",
                    stdout=invalid_vtool_output,
                )
                with self.assertRaises(SignedArtifactVerificationError):
                    self._verify(command_runner=command_runner)

    def test_loader_accepts_fd_contract_failure_but_rejects_security_errors(
        self,
    ) -> None:
        """
        함수 이름: test_loader_accepts_fd_contract_failure_but_rejects_security_errors()
        기능: expected EBADF는 통과하고 libpython/library-validation/signature 오류는 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        accepted_evidence = self._verify()
        self.assertEqual(accepted_evidence.team_id, TEST_TEAM_ID)

        loader_errors = (
            b"Library not loaded: libpython3.11.dylib",
            b"mapped file failed library validation",
            b"code signature invalid",
            b"unexpected bootstrap failure",
        )
        for loader_error in loader_errors:
            with self.subTest(loader_error=loader_error):
                command_runner = FakeCommandRunner(self.artifacts)
                command_runner.set_result(
                    "loader",
                    "sidecar",
                    returncode=1,
                    stderr=loader_error,
                )
                with self.assertRaises(SignedArtifactVerificationError):
                    self._verify(command_runner=command_runner)

    def test_loader_timeout_and_unexpected_success_are_rejected(self) -> None:
        """
        함수 이름: test_loader_timeout_and_unexpected_success_are_rejected()
        기능: loader hang과 fixed-FD 없이 exit 0인 sidecar를 fail-closed 처리한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        timeout_runner = FakeCommandRunner(self.artifacts)
        timeout_runner.set_exception(
            "loader",
            "sidecar",
            subprocess.TimeoutExpired(
                [str(self.artifacts.sidecar_path)],
                LOADER_SMOKE_TIMEOUT_SECONDS,
                output=b"private-partial-output",
            ),
        )
        with self.assertRaises(SignedArtifactVerificationError) as timeout_error:
            self._verify(command_runner=timeout_runner)
        self.assertNotIn("private-partial-output", str(timeout_error.exception))

        success_runner = FakeCommandRunner(self.artifacts)
        success_runner.set_result(
            "loader",
            "sidecar",
            returncode=0,
            stderr=b"",
        )
        with self.assertRaises(SignedArtifactVerificationError):
            self._verify(command_runner=success_runner)

    def test_pyinstaller_native_archive_is_required_and_verified(self) -> None:
        """CArchive provenance/libpython을 강제하고 존재하는 native 전부를 검증한다."""

        self.artifacts.sidecar_path.write_bytes(b"not-a-pyinstaller-carchive")
        self.artifacts.sidecar_path.chmod(0o755)
        with self.assertRaises(SignedArtifactVerificationError):
            self._verify()
        self.assertNotIn(
            "loader",
            [
                self.command_runner._classify_command(invocation[0])[0]
                for invocation in self.command_runner.invocations
            ],
        )
        self.artifacts.write_sidecar(self.artifacts.sidecar_path)

        self.artifacts.write_sidecar(
            self.artifacts.sidecar_path,
            provenance_commit="b" * 40,
        )
        with self.assertRaises(SignedArtifactVerificationError):
            self._verify(command_runner=FakeCommandRunner(self.artifacts))
        self.artifacts.write_sidecar(self.artifacts.sidecar_path)

        duplicate_key_provenance = (
            b'{"build_version":"20260824.1","commit":"'
            + TEST_COMMIT.encode("ascii")
            + b'","schema_version":1,"schema_version":1,"version":"0.1.0"}\n'
        )
        self.artifacts.write_sidecar(
            self.artifacts.sidecar_path,
            provenance_bytes=duplicate_key_provenance,
        )
        with self.assertRaises(SignedArtifactVerificationError):
            self._verify(command_runner=FakeCommandRunner(self.artifacts))

        noncanonical_provenance = json.dumps(
            {
                "schema_version": 1,
                "commit": TEST_COMMIT,
                "version": TEST_VERSION,
                "build_version": TEST_BUILD_VERSION,
            },
            indent=2,
        ).encode("utf-8")
        self.artifacts.write_sidecar(
            self.artifacts.sidecar_path,
            provenance_bytes=noncanonical_provenance,
        )
        with self.assertRaises(SignedArtifactVerificationError):
            self._verify(command_runner=FakeCommandRunner(self.artifacts))
        self.artifacts.write_sidecar(self.artifacts.sidecar_path)

        self.artifacts.write_sidecar(
            self.artifacts.sidecar_path,
            include_native_extension=False,
        )
        libpython_only_evidence = self._verify(
            command_runner=FakeCommandRunner(self.artifacts)
        )
        self.assertEqual(
            [
                native.kind
                for native in libpython_only_evidence.extracted_native
            ],
            ["libpython"],
        )
        self.artifacts.write_sidecar(self.artifacts.sidecar_path)

        mismatched_native_runner = FakeCommandRunner(self.artifacts)
        mismatched_native_runner.set_result(
            "metadata",
            "native",
            stderr=(
                b"flags=0x0(none)\n"
                + f"Authority={TEST_IDENTITY}\n".encode("ascii")
                + b"Authority=Developer ID Certification Authority G2\n"
                + b"Authority=Apple Root CA\n"
                + f"TeamIdentifier={OTHER_TEAM_ID}\n".encode("ascii")
            ),
        )
        with self.assertRaises(SignedArtifactVerificationError):
            self._verify(command_runner=mismatched_native_runner)

        newer_native_runner = FakeCommandRunner(self.artifacts)
        newer_native_runner.set_result(
            "vtool",
            "native",
            stdout=b"platform MACOS\n    minos 12.0\n",
        )
        with self.assertRaises(SignedArtifactVerificationError):
            self._verify(command_runner=newer_native_runner)

        compatible_native_runner = FakeCommandRunner(self.artifacts)
        compatible_native_runner.set_result(
            "vtool",
            "native",
            stdout=b"platform MACOS\n    minos 10.15\n",
        )
        evidence = self._verify(command_runner=compatible_native_runner)
        self.assertEqual(len(evidence.extracted_native), 2)

    def test_optional_dmg_runs_signature_image_stapler_and_gatekeeper_checks(
        self,
    ) -> None:
        """
        함수 이름: test_optional_dmg_runs_signature_image_stapler_and_gatekeeper_checks()
        기능: valid DMG가 app ticket/Gatekeeper와 final DMG identity·assessment를 모두 실행하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        evidence = self._verify(dmg_path=self.artifacts.dmg_path)

        self.assertTrue(evidence.dmg_verified)
        self.assertEqual(
            evidence.dmg_sha256,
            hashlib.sha256(b"signed-dmg-placeholder").hexdigest(),
        )
        self.assertIsNotNone(evidence.dmg_snapshot)
        self.assertEqual(
            evidence.dmg_snapshot.size,
            len(b"signed-dmg-placeholder"),
        )
        classified_operations = [
            self.command_runner._classify_command(invocation[0])[0]
            for invocation in self.command_runner.invocations
        ]
        self.assertIn("attach", classified_operations)
        self.assertIn("diskutil", classified_operations)
        self.assertEqual(classified_operations[-1], "detach")
        attach_command = next(
            invocation[0]
            for invocation in self.command_runner.invocations
            if self.command_runner._classify_command(invocation[0])[0] == "attach"
        )
        self.assertIn("-readonly", attach_command)
        self.assertIn("-nobrowse", attach_command)
        self.assertIn("-noautoopen", attach_command)
        self.assertFalse(self.command_runner.last_mount_path.exists())

    def test_stale_or_different_dmg_payload_is_rejected_and_detached(self) -> None:
        """stale version/build 또는 다른 executable을 담은 DMG가 외부 app을 빌려 PASS하지 못한다."""

        for mutation_name in ("version", "main-bytes", "sealed-resources"):
            with self.subTest(mutation_name=mutation_name):
                command_runner = FakeCommandRunner(self.artifacts)
                if mutation_name == "version":
                    command_runner.mounted_release_version = "0.1.1"
                elif mutation_name == "main-bytes":
                    command_runner.mounted_main_bytes = b"different-release-candidate"
                else:
                    command_runner.mounted_code_resources_bytes = (
                        b"different-sealed-resource-manifest"
                    )
                with self.assertRaises(SignedArtifactVerificationError):
                    self._verify(
                        dmg_path=self.artifacts.dmg_path,
                        command_runner=command_runner,
                    )
                operations = [
                    command_runner._classify_command(invocation[0])[0]
                    for invocation in command_runner.invocations
                ]
                self.assertIn("attach", operations)
                self.assertEqual(operations[-1], "detach")
                self.assertIsNotNone(command_runner.last_mount_path)
                self.assertFalse(command_runner.last_mount_path.exists())

    def test_mounted_layout_and_internal_signature_are_fail_closed_with_cleanup(
        self,
    ) -> None:
        """extra app, 잘못된 Applications link와 mounted signature failure에서도 detach한다."""

        for failure_mode in (
            "extra-app",
            "applications-link",
            "writable-mount",
            "signature",
        ):
            with self.subTest(failure_mode=failure_mode):
                command_runner = FakeCommandRunner(self.artifacts)
                if failure_mode == "extra-app":
                    command_runner.add_extra_app = True
                elif failure_mode == "applications-link":
                    command_runner.applications_target = "/private/Applications"
                elif failure_mode == "writable-mount":
                    command_runner.mount_writable = True
                else:
                    command_runner.set_result(
                        "strict-signature",
                        "mounted-sidecar",
                        returncode=1,
                        stderr=b"private mounted signature detail",
                    )
                with self.assertRaises(SignedArtifactVerificationError):
                    self._verify(
                        dmg_path=self.artifacts.dmg_path,
                        command_runner=command_runner,
                    )
                operations = [
                    command_runner._classify_command(invocation[0])[0]
                    for invocation in command_runner.invocations
                ]
                self.assertEqual(operations[-1], "detach")
                self.assertFalse(command_runner.last_mount_path.exists())

    def test_same_open_dmg_descriptor_detects_mount_time_mutation(self) -> None:
        """mount 동안 DMG path/content drift를 same open FD digest/snapshot으로 거부한다."""

        command_runner = FakeCommandRunner(self.artifacts)
        command_runner.mutate_dmg_on_attach = True
        with self.assertRaises(SignedArtifactVerificationError):
            self._verify(
                dmg_path=self.artifacts.dmg_path,
                command_runner=command_runner,
            )
        operations = [
            command_runner._classify_command(invocation[0])[0]
            for invocation in command_runner.invocations
        ]
        self.assertEqual(operations[-1], "detach")
        self.assertFalse(command_runner.last_mount_path.exists())

    def test_dmg_symlink_and_each_external_assessment_failure_are_rejected(
        self,
    ) -> None:
        """
        함수 이름: test_dmg_symlink_and_each_external_assessment_failure_are_rejected()
        기능: DMG symlink와 signature/image/ticket/Gatekeeper 실패를 각각 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        linked_dmg_path = self.artifacts.root_path / "linked.dmg"
        linked_dmg_path.symlink_to(self.artifacts.dmg_path)
        with self.assertRaises(SignedArtifactVerificationError):
            self._verify(dmg_path=linked_dmg_path)
        self.assertEqual(self.command_runner.invocations, [])

        for failing_operation, artifact_role in (
            ("stapler", "app"),
            ("app-spctl", "app"),
            ("dmg-signature", "dmg"),
            ("metadata", "dmg"),
            ("hdiutil", "dmg"),
            ("stapler", "dmg"),
            ("spctl", "dmg"),
            ("attach", "dmg"),
        ):
            with self.subTest(
                failing_operation=failing_operation,
                artifact_role=artifact_role,
            ):
                command_runner = FakeCommandRunner(self.artifacts)
                if failing_operation == "metadata":
                    command_runner.set_result(
                        failing_operation,
                        artifact_role,
                        stderr=(
                            f"TeamIdentifier={OTHER_TEAM_ID}\n".encode(
                                "ascii"
                            )
                        ),
                    )
                else:
                    command_runner.set_result(
                        failing_operation,
                        artifact_role,
                        returncode=1,
                        stderr=b"private DMG tool detail",
                    )
                with self.assertRaises(SignedArtifactVerificationError):
                    self._verify(
                        dmg_path=self.artifacts.dmg_path,
                        command_runner=command_runner,
                    )

    def test_environment_sanitizer_removes_current_and_future_apple_secrets(
        self,
    ) -> None:
        """
        함수 이름: test_environment_sanitizer_removes_current_and_future_apple_secrets()
        기능: known secret과 Apple-prefixed future secret marker를 모두 제거하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        sanitized_environment = create_sanitized_environment(
            self.source_environment
        )

        self.assertEqual(
            sanitized_environment["PATH"],
            "/usr/bin:/bin:/usr/sbin:/sbin",
        )
        self.assertNotIn("APPLE_CERTIFICATE", sanitized_environment)
        self.assertNotIn("APPLE_API_KEY_PATH", sanitized_environment)
        self.assertNotIn("APPLE_FUTURE_PRIVATE_KEY", sanitized_environment)
        self.assertNotIn("BINANCE_TESTNET_API_KEY", sanitized_environment)
        self.assertNotIn("BINANCE_TESTNET_API_SECRET", sanitized_environment)
        self.assertNotIn("BINANCE_FUTURE_CREDENTIAL", sanitized_environment)
        self.assertEqual(sanitized_environment["BINANCE_RUN_TESTNET"], "0")
        for removed_control_name in (
            "APPLE_SIGNING_IDENTITY",
            "DEVELOPER_DIR",
            "TOOLCHAINS",
            "DYLD_INSERT_LIBRARIES",
            "PYTHONPATH",
            "PYTHONINSPECT",
            "GIT_CONFIG_GLOBAL",
            "XCODE_XCCONFIG_FILE",
            "TMPDIR",
            "NOTARY_PROFILE",
        ):
            self.assertNotIn(removed_control_name, sanitized_environment)

    def test_command_output_is_bounded_for_injected_and_real_runner(self) -> None:
        """loader output가 memory limit을 넘으면 fake/real child 모두 fail-closed 종료한다."""

        oversized_runner = FakeCommandRunner(self.artifacts)
        oversized_runner.set_result(
            "loader",
            "sidecar",
            returncode=1,
            stderr=b"x" * (MAXIMUM_COMMAND_OUTPUT_BYTES + 1),
        )
        with self.assertRaises(SignedArtifactVerificationError):
            self._verify(command_runner=oversized_runner)

        with self.assertRaises(SignedArtifactVerificationError):
            run_command(
                [
                    sys.executable,
                    "-c",
                    (
                        "import os; os.write(1, b'x' * "
                        f"{MAXIMUM_COMMAND_OUTPUT_BYTES + 1})"
                    ),
                ],
                self.artifacts.root_path,
                {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                5,
            )

    def test_loader_timeout_kills_the_entire_child_process_group(self) -> None:
        """timeout child가 만든 descendant도 새 session process group과 함께 종료한다."""

        orphan_marker = self.artifacts.root_path / "orphan-marker"
        child_program = (
            "import subprocess,sys,time; "
            "subprocess.Popen([sys.executable,'-c',"
            "'import pathlib,sys,time; time.sleep(0.5); "
            "pathlib.Path(sys.argv[1]).write_text(\"orphan\")',sys.argv[1]]); "
            "time.sleep(30)"
        )
        with self.assertRaises(SignedArtifactVerificationError):
            run_command(
                [sys.executable, "-c", child_program, str(orphan_marker)],
                self.artifacts.root_path,
                {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                0.1,
            )
        import time as test_time

        deadline = test_time.monotonic() + 0.8
        while test_time.monotonic() < deadline and not orphan_marker.exists():
            test_time.sleep(0.05)
        self.assertFalse(orphan_marker.exists())

    def test_successful_leader_cannot_leave_a_closed_pipe_descendant(self) -> None:
        """leader exit 0 뒤 pipe를 닫은 같은-group descendant도 reap 전 제거한다."""

        orphan_marker = self.artifacts.root_path / "closed-pipe-orphan-marker"
        leader_program = (
            "import subprocess,sys; "
            "subprocess.Popen([sys.executable,'-c',"
            "'import pathlib,sys,time; time.sleep(0.5); "
            "pathlib.Path(sys.argv[1]).write_text(\"orphan\")',sys.argv[1]],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
            "stderr=subprocess.DEVNULL)"
        )
        process_result = run_command(
            [sys.executable, "-c", leader_program, str(orphan_marker)],
            self.artifacts.root_path,
            {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            5,
        )
        self.assertEqual(process_result.returncode, 0)
        import time as test_time

        deadline = test_time.monotonic() + 0.8
        while test_time.monotonic() < deadline and not orphan_marker.exists():
            test_time.sleep(0.05)
        self.assertFalse(orphan_marker.exists())

    @patch("scripts.verify_phase12_signed_artifacts.verify_signed_artifacts")
    def test_cli_failure_never_echoes_paths_team_id_or_tool_output(
        self,
        verify_signed_artifacts_mock,
    ) -> None:
        """
        함수 이름: test_cli_failure_never_echoes_paths_team_id_or_tool_output()
        기능: top-level verification 실패가 caller path, Team ID와 raw helper detail을 반사하지 않는지 검증한다.
        인자: verify_signed_artifacts_mock -> production verifier test double
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        raw_failure_detail = (
            f"{self.artifacts.app_path} {TEST_TEAM_ID} raw-tool-secret"
        )
        verify_signed_artifacts_mock.side_effect = SignedArtifactVerificationError(
            raw_failure_detail
        )
        captured_stdout = StringIO()
        captured_stderr = StringIO()

        with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
            exit_status = main(
                [
                    "--app",
                    str(self.artifacts.app_path),
                    "--dmg",
                    str(self.artifacts.dmg_path),
                    "--expected-team-id",
                    TEST_TEAM_ID,
                    "--expected-identity",
                    TEST_IDENTITY,
                    "--expected-version",
                    TEST_VERSION,
                    "--expected-build-version",
                    TEST_BUILD_VERSION,
                    "--expected-commit",
                    TEST_COMMIT,
                ]
            )

        combined_output = captured_stdout.getvalue() + captured_stderr.getvalue()
        self.assertEqual(exit_status, 1)
        self.assertEqual(
            captured_stderr.getvalue(),
            "phase12-signed-artifacts: ERROR: signed artifact verification failed.\n",
        )
        self.assertNotIn(str(self.artifacts.app_path), combined_output)
        self.assertNotIn(TEST_TEAM_ID, combined_output)
        self.assertNotIn("raw-tool-secret", combined_output)

    def test_cli_argument_error_does_not_echo_unknown_raw_value(self) -> None:
        """
        함수 이름: test_cli_argument_error_does_not_echo_unknown_raw_value()
        기능: argparse 실패도 unknown argument에 든 raw path를 출력하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        raw_argument = "--private-path=/Users/release/private-artifact"
        captured_stderr = StringIO()

        with redirect_stderr(captured_stderr):
            with self.assertRaises(SystemExit) as exit_context:
                parse_arguments([raw_argument])

        self.assertEqual(exit_context.exception.code, 2)
        self.assertEqual(
            captured_stderr.getvalue(),
            "phase12-signed-artifacts: ERROR: invalid arguments.\n",
        )
        self.assertNotIn("private-artifact", captured_stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
