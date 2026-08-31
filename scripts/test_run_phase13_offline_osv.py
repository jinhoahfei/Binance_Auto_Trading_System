"""Phase 13 OSV scan의 외부 전송 불가와 local-only fail-closed 정책을 검증한다."""

from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.run_phase13_offline_osv import (
    EXTERNAL_OSV_TRANSMISSION_ALLOWED,
    LOCKFILE_PATHS,
    NETWORK_DENY_PROFILE,
    OfflineOsvPolicyError,
    build_offline_environment,
    build_offline_osv_command,
    parse_arguments,
    run_offline_osv_scan,
)


class Phase13OfflineOsvTests(unittest.TestCase):
    """
    클래스 이름: Phase13OfflineOsvTests
    기능: OS sandbox와 scanner offline mode가 없는 모든 OSV 실행을 거부하는지 검증한다.
    작성 날짜: 2026/08/29
    """

    def _write_executable(self, executable_path: Path) -> None:
        """
        함수 이름: _write_executable()
        기능: 경계 검증용 local executable fixture를 만든다.
        인자: executable_path -> 생성할 executable file
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        executable_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable_path.chmod(0o755)  # 실행 bit까지 production preflight와 동일하게 맞춘다.

    def _write_fixed_lockfiles(self, repository_root: Path) -> None:
        """
        함수 이름: _write_fixed_lockfiles()
        기능: Canonical 세 lockfile을 symlink가 아닌 regular file로 만든다.
        인자: repository_root -> 임시 repository root
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # 모든 fixture는 production runner가 허용한 relative path에만 생성한다.
        for lockfile_path in LOCKFILE_PATHS:
            absolute_lockfile_path = repository_root / lockfile_path
            absolute_lockfile_path.parent.mkdir(parents=True, exist_ok=True)
            absolute_lockfile_path.write_text("# local lockfile\n", encoding="utf-8")

    def test_vulnerability_command_has_two_independent_offline_boundaries(
        self,
    ) -> None:
        """
        함수 이름: test_vulnerability_command_has_two_independent_offline_boundaries()
        기능: 취약점 scan이 OS network deny와 cached database option을 동시에 강제하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        scanner_path = Path("/opt/local/bin/osv-scanner")
        sandbox_path = Path("/usr/bin/sandbox-exec")
        command = build_offline_osv_command(
            "vulnerability",
            scanner_path=scanner_path,
        )

        # Network-deny wrapper가 scanner보다 먼저 오고 세 input 뒤 fixed offline tail만 이어진다.
        self.assertEqual(
            command[:6],
            (
                os.fspath(sandbox_path),
                "-p",
                NETWORK_DENY_PROFILE,
                os.fspath(scanner_path),
                "scan",
                "source",
            ),
        )
        self.assertEqual(command.count("--lockfile"), len(LOCKFILE_PATHS))
        self.assertEqual(command.count("--offline"), 1)
        self.assertIn("--offline-vulnerabilities", command)
        self.assertFalse(
            any(argument.startswith("--licenses") for argument in command)
        )
        self.assertNotIn("--download-offline-databases", command)
        self.assertFalse(EXTERNAL_OSV_TRANSMISSION_ALLOWED)

    def test_license_command_cannot_select_remote_source_or_download(self) -> None:
        """
        함수 이름: test_license_command_cannot_select_remote_source_or_download()
        기능: License scan이 offline fixed argv만 가지며 remote source option을 노출하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        command = build_offline_osv_command(
            "license",
            scanner_path=Path("/opt/local/bin/osv-scanner"),
        )

        # License path도 동일한 OS sandbox 안에서 scanner offline mode를 정확히 한 번 사용한다.
        self.assertIn("--licenses=", command)
        self.assertEqual(command.count("--offline"), 1)
        self.assertEqual(
            command.index("--licenses=") + 1,
            command.index("--offline"),
        )
        self.assertNotIn("--offline-vulnerabilities", command)
        self.assertNotIn("--data-source", command)
        self.assertNotIn("--maven-registry", command)
        self.assertNotIn("--download-offline-databases", command)
        self.assertFalse(
            any(argument.startswith(("http://", "https://")) for argument in command)
        )

    def test_remote_operation_and_additional_cli_options_are_rejected(self) -> None:
        """
        함수 이름: test_remote_operation_and_additional_cli_options_are_rejected()
        기능: Remote endpoint와 download option을 operation 또는 추가 CLI 인자로 주입할 수 없는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with self.assertRaisesRegex(OfflineOsvPolicyError, "unsupported"):
            build_offline_osv_command(
                "https://api.osv.dev",
                scanner_path=Path("/opt/local/bin/osv-scanner"),
            )

        # Argparse choices와 단일 positional 계약은 raw scanner option을 모두 parse 단계에서 막는다.
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parse_arguments(["vulnerability", "--download-offline-databases"])
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parse_arguments(["https://api.osv.dev"])

    def test_child_environment_strips_proxy_and_osv_credentials(self) -> None:
        """
        함수 이름: test_child_environment_strips_proxy_and_osv_credentials()
        기능: 대소문자가 다른 proxy와 OSV credential이 scanner child로 상속되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        source_environment = {
            "HTTP_PROXY": "http://proxy.invalid",
            "https_proxy": "http://proxy.invalid",
            "All_Proxy": "socks5://proxy.invalid",
            "NO_PROXY": "localhost",
            "OSV_API_KEY": "secret-api-key",
            "osv_scanner_api_key": "secret-scanner-key",
            "SAFE_LOCAL_VALUE": "preserved",
        }

        # Network route와 credential은 제거하되 scan에 무관한 local environment는 보존한다.
        sanitized_environment = build_offline_environment(source_environment)
        self.assertEqual(sanitized_environment, {"SAFE_LOCAL_VALUE": "preserved"})

    def test_runner_uses_exact_argv_without_shell_and_returns_scanner_status(
        self,
    ) -> None:
        """
        함수 이름: test_runner_uses_exact_argv_without_shell_and_returns_scanner_status()
        기능: 실제 runner가 fixed root에서 sanitized env와 shell 없는 argv만 실행하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository_root = Path(temporary_directory) / "repository"
            repository_root.mkdir()
            self._write_fixed_lockfiles(repository_root)
            scanner_path = Path(temporary_directory) / "osv-scanner"
            sandbox_path = Path(temporary_directory) / "sandbox-exec"
            self._write_executable(scanner_path)
            self._write_executable(sandbox_path)
            captured_call: dict[str, object] = {}

            def find_local_scanner(tool_name: str) -> str | None:
                """
                함수 이름: find_local_scanner()
                기능: Test가 승인한 local scanner 한 개만 반환한다.
                인자: tool_name -> 요청 executable 이름
                반환값: local scanner path 또는 None
                작성 날짜: 2026/08/29
                """
                if tool_name != "osv-scanner":
                    return None
                return os.fspath(scanner_path)

            def capture_command(
                command: tuple[str, ...],
                **keyword_arguments: object,
            ) -> subprocess.CompletedProcess[object]:
                """
                함수 이름: capture_command()
                기능: Process를 시작하지 않고 exact command와 실행 경계를 기록한다.
                인자: command -> 실행 예정 argv, keyword_arguments -> subprocess options
                반환값: Finding을 뜻하는 status 7의 completed process
                작성 날짜: 2026/08/29
                """
                captured_call["command"] = command
                captured_call.update(keyword_arguments)
                return subprocess.CompletedProcess(command, 7)

            # Hostile proxy와 API key를 주입해도 captured scanner child에는 남지 않아야 한다.
            with patch(
                "scripts.run_phase13_offline_osv.SANDBOX_EXECUTABLE",
                sandbox_path,
            ):
                exit_status = run_offline_osv_scan(
                    "vulnerability",
                    repository_root=repository_root,
                    tool_finder=find_local_scanner,
                    command_runner=capture_command,
                    source_environment={
                        "PATH": "/local/bin",
                        "HTTPS_PROXY": "https://proxy.invalid",
                        "OSV_API_KEY": "secret",
                    },
                )
            captured_command = captured_call["command"]
            captured_environment = captured_call["env"]

            self.assertEqual(exit_status, 7)
            self.assertIsInstance(captured_command, tuple)
            self.assertEqual(captured_call["cwd"], repository_root)
            self.assertFalse(captured_call["check"])
            self.assertFalse(captured_call["shell"])
            self.assertTrue(captured_call["close_fds"])
            self.assertEqual(captured_environment, {"PATH": "/local/bin"})
            self.assertIn(NETWORK_DENY_PROFILE, captured_command)
            self.assertIn("--offline", captured_command)

    def test_missing_sandbox_lockfile_or_scanner_fails_closed(self) -> None:
        """
        함수 이름: test_missing_sandbox_lockfile_or_scanner_fails_closed()
        기능: Network boundary, fixed input 또는 scanner가 없을 때 process를 시작하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository_root = Path(temporary_directory) / "repository"
            repository_root.mkdir()
            sandbox_path = Path(temporary_directory) / "sandbox-exec"
            scanner_path = Path(temporary_directory) / "osv-scanner"

            # Sandbox 부재는 lockfile과 tool discovery보다 먼저 local-only 실행을 차단한다.
            with patch(
                "scripts.run_phase13_offline_osv.SANDBOX_EXECUTABLE",
                sandbox_path,
            ):
                with self.assertRaisesRegex(OfflineOsvPolicyError, "sandbox"):
                    run_offline_osv_scan(
                        "vulnerability",
                        repository_root=repository_root,
                    )

                self._write_executable(sandbox_path)
                with self.assertRaisesRegex(OfflineOsvPolicyError, "lockfile"):
                    run_offline_osv_scan(
                        "vulnerability",
                        repository_root=repository_root,
                    )

                self._write_fixed_lockfiles(repository_root)
                with self.assertRaisesRegex(OfflineOsvPolicyError, "unavailable"):
                    run_offline_osv_scan(
                        "vulnerability",
                        repository_root=repository_root,
                        tool_finder=lambda _tool_name: None,
                    )

                # 발견된 scanner도 실행 bit가 없으면 command runner에 도달하지 않는다.
                scanner_path.write_text("not executable\n", encoding="utf-8")
                with self.assertRaisesRegex(
                    OfflineOsvPolicyError,
                    "not executable",
                ):
                    run_offline_osv_scan(
                        "license",
                        repository_root=repository_root,
                        tool_finder=lambda _tool_name: os.fspath(scanner_path),
                    )


if __name__ == "__main__":
    unittest.main()
