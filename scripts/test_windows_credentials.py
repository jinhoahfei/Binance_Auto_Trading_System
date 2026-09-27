"""Credential provisioning contracts; native script flows use an in-memory fake store."""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
LIVE_SCRIPT = ROOT / "scripts/configure_live_credentials.ps1"
ADAPTER = ROOT / "scripts/windows/CredentialManager.cs"


class WindowsCredentialContractTests(unittest.TestCase):
    def test_secret_inputs_and_targets_are_fixed(self):
        script = LIVE_SCRIPT.read_text(encoding="utf-8")
        adapter = ADAPTER.read_text(encoding="utf-8")
        self.assertIn("[ValidateSet('set', 'check', 'read-only', 'orders', 'disable')]", script)
        self.assertEqual(script.count("-AsSecureString"), 2)
        for target in ("com.binance-auto.trader.testnet/", "com.binance-auto.trader.live/",
                       "com.binance-auto.trader.desktop-profile/execution-profile"):
            self.assertIn(target, adapter)
        for unsafe in ("PtrToString", "GetNetworkCredential", "ConvertFrom-SecureString", "$env:"):
            self.assertNotIn(unsafe, script + adapter)
        self.assertIn("Marshal.ZeroFreeBSTR", adapter)
        self.assertIn("$api_key.Dispose()", script)
        self.assertIn("$api_secret.Dispose()", script)


@unittest.skipUnless(sys.platform == "win32", "PowerShell native script execution requires Windows")
class WindowsLiveCredentialFlowTests(unittest.TestCase):
    def run_action(self, action, replies=(), *, fail_secret_write=False):
        """Run the real script while substituting only host input and the native credential store."""
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell, "Windows credential scripts require PowerShell")
        # No real store is accessed: the fake type is compiled first, then Add-Type is shadowed.
        fake_adapter = r'''
using System;
using System.Security;
public static class CredentialManager {
    public static bool failSecretWrite;
    public static void validate_secret(SecureString value) {
        if (value == null || value.Length == 0) throw new Exception("invalid");
    }
    public static void write_live_secret(string account, SecureString value) {
        Console.WriteLine("EVENT write:" + account);
        if (failSecretWrite && account == "api-secret") throw new Exception("private-error-canary");
    }
    public static bool verify_live_secret(string account, SecureString expected) {
        Console.WriteLine("EVENT verify:" + account);
        return true;
    }
    public static void write_execution_profile(string profile) {
        Console.WriteLine("EVENT profile:" + profile);
    }
}
'''
        def literal(value):
            return "'" + value.replace("'", "''") + "'"
        harness = "Add-Type -TypeDefinition @'\n" + fake_adapter + "\n'@\n"
        harness += "function Add-Type { param([string]$Path) }\n"
        harness += "$global:replies = [System.Collections.Generic.Queue[string]]::new()\n"
        for reply in replies:
            harness += f"$global:replies.Enqueue({literal(reply)})\n"
        harness += """
function Read-Host {
    param([string]$Prompt, [switch]$AsSecureString)
    if ($AsSecureString) {
        $value = New-Object Security.SecureString
        foreach ($character in 'public-test-canary'.ToCharArray()) { $value.AppendChar($character) }
        return $value
    }
    return $global:replies.Dequeue()
}
"""
        if fail_secret_write:
            harness += "[CredentialManager]::failSecretWrite = $true\n"
        harness += f"& {literal(str(LIVE_SCRIPT))} -Action {literal(action)}\n"
        with tempfile.TemporaryDirectory() as directory:
            harness_path = Path(directory) / "credential-flow.ps1"
            harness_path.write_text(harness, encoding="utf-8-sig")
            return subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness_path)],
                capture_output=True, text=True, check=False, timeout=30,
            )

    def test_set_disables_previous_live_profile_until_both_credentials_are_verified(self):
        result = self.run_action("set", ("LIVE",))
        self.assertEqual(result.returncode, 0, result.stderr)
        events = [line for line in result.stdout.splitlines() if line.startswith("EVENT ")]
        self.assertEqual(events, ["EVENT profile:TESTNET", "EVENT write:api-key", "EVENT write:api-secret",
                                  "EVENT verify:api-key", "EVENT verify:api-secret", "EVENT profile:LIVE_READ_ONLY"])
        self.assertNotIn("public-test-canary", result.stdout + result.stderr)

    def test_partial_replacement_never_reenables_live_profile_or_prints_error_contents(self):
        result = self.run_action("set", ("LIVE",), fail_secret_write=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("EVENT profile:TESTNET", result.stdout)
        self.assertNotIn("EVENT profile:LIVE_", result.stdout)
        self.assertNotIn("private-error-canary", result.stdout + result.stderr)

    def test_confirmation_is_exact_and_orders_need_separate_approval(self):
        for action, replies in (("set", ("live",)), ("orders", ("LIVE", "live orders 10 usdt"))):
            with self.subTest(action=action):
                result = self.run_action(action, replies)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("EVENT profile:", result.stdout)
        result = self.run_action("orders", ("LIVE", "LIVE ORDERS 10 USDT"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("EVENT profile:LIVE_ORDERS_V1", result.stdout)

    def test_check_only_verifies_and_disable_does_not_read_credentials(self):
        checked = self.run_action("check")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertNotIn("EVENT profile:", checked.stdout)
        disabled = self.run_action("disable", ("LIVE",))
        self.assertEqual(disabled.returncode, 0, disabled.stderr)
        self.assertIn("EVENT profile:TESTNET", disabled.stdout)
        self.assertNotIn("EVENT verify:", disabled.stdout)

    def test_native_adapter_compiles_without_accessing_credentials(self):
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell)
        quoted = str(ADAPTER).replace("'", "''")
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", f"$ErrorActionPreference = 'Stop'; Add-Type -Path '{quoted}'"],
            capture_output=True, text=True, check=False, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
