"""Offline diagnostic portability checks without launching apps or exchange requests."""

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend/src"))
from scripts import diagnostic_process_metrics as metrics
from scripts import run_background_liveness_soak as background
from scripts import run_live_read_only_from_keychain as live_runner
from scripts import run_testnet_from_keychain as testnet_runner


class WindowsDiagnosticTests(unittest.TestCase):
    def test_background_runner_uses_native_executable_suffix(self):
        for platform, name in (("win32", "background-liveness-soak.exe"), ("darwin", "background-liveness-soak")):
            with self.subTest(platform=platform), patch.object(background.sys, "platform", platform):
                self.assertEqual(background.background_soak_binary(ROOT).name, name)

    def test_memory_metrics_keep_bytes_on_all_supported_platforms(self):
        resource = SimpleNamespace(RUSAGE_SELF=0, getrusage=Mock(return_value=SimpleNamespace(ru_maxrss=4096)))
        for platform, expected in (("darwin", 4096), ("linux", 4096 * 1024)):
            with self.subTest(platform=platform), patch.object(metrics.sys, "platform", platform), patch.object(metrics, "resource", resource):
                self.assertEqual(metrics.peak_rss_bytes(), expected)
        with patch.object(metrics.sys, "platform", "win32"), patch.object(metrics, "resource", None), patch.object(metrics, "_windows_peak_rss_bytes", return_value=16384):
            self.assertEqual(metrics.peak_rss_bytes(), 16384)

    def test_background_run_preserves_unicode_paths_and_status(self):
        with TemporaryDirectory(prefix="윈도우-") as directory:
            output = Path(directory) / "진단"
            def launch(command, **kwargs):
                self.assertTrue(command[0].endswith(".exe"))
                self.assertEqual(command[2], str(output.resolve()))
                output.mkdir()
                return SimpleNamespace(pid=123, wait=lambda: 0)
            with patch.object(background.sys, "platform", "win32"), \
                    patch.object(sys, "argv", ["runner", "--output", str(output), "--seconds", "20"]), \
                    patch.object(background.subprocess, "Popen", side_effect=launch), \
                    patch.object(background, "validate", return_value={"status": "passed", "message": "완료"}):
                self.assertEqual(background.main(), 0)
            status = json.loads(output.with_suffix(".status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["output"], str(output.resolve()))
            self.assertEqual(status["validation"]["message"], "완료")

    def test_native_memory_metric_is_available(self):
        self.assertGreater(metrics.peak_rss_bytes(), 0)

    def test_windows_keychain_commands_fail_before_credential_access(self):
        with patch.object(testnet_runner.sys, "platform", "win32"), patch.object(testnet_runner, "resource", None):
            with self.assertRaises(testnet_runner.TestnetKeychainRunnerError):
                testnet_runner.harden_runner_process()
        output = StringIO()
        with patch.object(live_runner.sys, "platform", "win32"), patch.object(live_runner, "resource", None), \
                patch.object(sys, "argv", ["runner", "--confirm-live", "LIVE"]), \
                patch.object(live_runner, "read_keychain_credential") as reader, redirect_stdout(output):
            self.assertEqual(live_runner.main(), 1)
            reader.assert_not_called()
        self.assertIn('"order_mutations": 0', output.getvalue())


if __name__ == "__main__":
    unittest.main()
