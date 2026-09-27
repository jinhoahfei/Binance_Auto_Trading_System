"""Windows packaging의 IPC 보존, 실패 전파와 credential 격리를 검증한다."""

from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import package_sidecar_windows as packaging
from scripts.check_platform import verification_environment


class WindowsPackagingTests(unittest.TestCase):
    """클래스 이름: WindowsPackagingTests
    기능: native builder 호출 경계와 완성 artifact 게시를 실제 임시 파일로 검증한다.
    작성 날짜: 2026/09/27
    """

    def test_build_preserves_pipes_timezone_data_and_sanitizes_environment(self):
        """공백·한글 경로와 Windows 실행 suffix를 보존하고 credential을 전달하지 않는다."""
        with tempfile.TemporaryDirectory(prefix="windows package 한글 ") as directory, ExitStack() as stack:
            root = Path(directory)
            python = root / "backend/.venv/Scripts/python.exe"
            python.parent.mkdir(parents=True)
            python.touch()
            calls = []

            def run(command, **options):
                calls.append((command, options))
                if command[0] == "rustc":
                    return subprocess.CompletedProcess(command, 0, packaging.TARGET_TRIPLE + "\n")
                if "PyInstaller" in command:
                    dist = Path(command[command.index("--distpath") + 1])
                    dist.mkdir()
                    (dist / "binance-auto-sidecar.exe").write_bytes(b"MZfixture-executable")
                return subprocess.CompletedProcess(command, 0)

            stack.enter_context(patch.object(packaging.sys, "platform", "win32"))
            stack.enter_context(patch.object(packaging.platform, "machine", return_value="AMD64"))
            stack.enter_context(patch.object(packaging.struct, "calcsize", return_value=8))
            stack.enter_context(patch.object(packaging.subprocess, "run", side_effect=run))
            stack.enter_context(patch.dict(os.environ, {
                "BINANCE_API_SECRET": "fixture-secret", "APPLE_CERTIFICATE": "fixture-certificate",
                "PYTHONPATH": "fixture-injection", "TAURI_CONFIG": "fixture-config",
            }))
            result = packaging.package_sidecar(root)
            self.assertEqual(result.name, f"binance-auto-sidecar-{packaging.TARGET_TRIPLE}.exe")
            self.assertEqual(result.read_bytes(), b"MZfixture-executable")
            self.assertEqual(len(calls), 3)
            command = calls[-1][0]
            self.assertEqual(command[0], str(python))
            self.assertIn("--console", command)
            self.assertNotIn("--windowed", command)
            self.assertEqual(command[command.index("--collect-data") + 1], "tzdata")
            for _, options in calls:
                self.assertTrue(options["check"])
                for name in ("BINANCE_API_SECRET", "APPLE_CERTIFICATE", "PYTHONPATH", "TAURI_CONFIG"):
                    self.assertNotIn(name, options["env"])
            self.assertFalse(list(result.parent.glob(".windows-package-*")))

    def test_failed_build_keeps_existing_executable(self):
        """PyInstaller 실패가 이전 정상 exe를 지우거나 덮어쓰지 않는다."""
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            python = root / "backend/.venv/Scripts/python.exe"
            python.parent.mkdir(parents=True)
            python.touch()
            target = root / "UI/apps/desktop/src-tauri/binaries" / f"binance-auto-sidecar-{packaging.TARGET_TRIPLE}.exe"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"MZexisting")
            stack.enter_context(patch.object(packaging.sys, "platform", "win32"))
            stack.enter_context(patch.object(packaging.platform, "machine", return_value="AMD64"))
            stack.enter_context(patch.object(packaging.struct, "calcsize", return_value=8))
            stack.enter_context(patch.object(packaging.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0, packaging.TARGET_TRIPLE),
                subprocess.CompletedProcess([], 0),
                subprocess.CalledProcessError(1, ["build"]),
            ]))
            with self.assertRaises(subprocess.CalledProcessError):
                packaging.package_sidecar(root)
            self.assertEqual(target.read_bytes(), b"MZexisting")
            self.assertFalse(list(target.parent.glob(".windows-package-*")))

    def test_non_windows_host_fails_before_starting_builder(self):
        """macOS에서 Windows artifact를 만든 것으로 오인하지 않는다."""
        with patch.object(packaging.sys, "platform", "darwin"), patch.object(packaging.subprocess, "run") as run:
            with self.assertRaises(RuntimeError):
                packaging.package_sidecar()
            run.assert_not_called()

    def test_verification_closes_order_flags_and_uses_native_path_separator(self):
        """검증 child가 부모의 주문 opt-in, credential, Python 주입을 상속하지 않는다."""
        environment = verification_environment({
            "PATH": "tools", "BINANCE_RUN_TESTNET": "1", "BINANCE_API_KEY": "fixture",
            "BINANCE_RUN_TESTNET_ORDERS": "1", "BINANCE_RUN_PHASE13_PUBLIC_CASE2": "1",
            "PYTHONSTARTUP": "fixture", "TAURI_CONFIG": "fixture",
        })
        self.assertEqual(environment["BINANCE_RUN_TESTNET"], "0")
        self.assertEqual(environment["BINANCE_RUN_TESTNET_ORDERS"], "0")
        self.assertEqual(environment["BINANCE_RUN_PHASE13_PUBLIC_CASE2"], "0")
        self.assertNotIn("BINANCE_API_KEY", environment)
        self.assertNotIn("PYTHONSTARTUP", environment)
        self.assertEqual(len(environment["PYTHONPATH"].split(os.pathsep)), 2)


if __name__ == "__main__":
    unittest.main()
