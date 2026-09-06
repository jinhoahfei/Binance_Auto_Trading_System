"""Windows 개발 진입 설정과 credential tooling의 비밀 전달 경계를 검증한다."""

import json
from pathlib import Path
import subprocess
import struct
import sys
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TAURI_DIRECTORY = REPOSITORY_ROOT / "UI/apps/desktop/src-tauri"


class WindowsDevelopmentContractTests(unittest.TestCase):
    """
    클래스 이름: WindowsDevelopmentContractTests
    기능: Windows 개발 hook과 macOS 보존, credential target 계약의 drift를 막는다.
    작성 날짜: 2026/09/06
    """

    def test_windows_development_does_not_require_packaged_sidecar(self) -> None:
        """
        함수 이름: test_windows_development_does_not_require_packaged_sidecar()
        기능: Windows override가 기존 macOS hook을 바꾸지 않고 source dev로 진입하는지 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        # OS merge 뒤의 개발 hook과 외부 binary 요구사항을 동시에 검증한다.
        base = json.loads((TAURI_DIRECTORY / "tauri.conf.json").read_text())
        windows = json.loads((TAURI_DIRECTORY / "tauri.windows.conf.json").read_text())
        self.assertIn("package_sidecar.sh", base["build"]["beforeDevCommand"])
        self.assertEqual(windows["build"]["beforeDevCommand"], "node scripts/desktopFrontend.mjs dev")
        self.assertFalse(windows["bundle"]["active"])
        self.assertEqual(windows["bundle"]["externalBin"], [])
        self.assertEqual(windows["bundle"]["targets"], [])
        self.assertNotIn("app", windows)  # Origin/CSP/capability를 Windows에서 넓히지 않는다.

        # Tauri Windows debug resource도 ICO가 필요하며 기존 256px PNG의 픽셀을 그대로 포함한다.
        icon_path = TAURI_DIRECTORY / windows["bundle"]["icon"][0]
        icon_bytes = icon_path.read_bytes()
        self.assertEqual(struct.unpack("<HHH", icon_bytes[:6]), (0, 1, 1))
        payload_length, payload_offset = struct.unpack("<II", icon_bytes[14:22])
        self.assertEqual(len(icon_bytes), payload_offset + payload_length)
        self.assertEqual(icon_bytes[payload_offset:payload_offset + 8], b"\x89PNG\r\n\x1a\n")

    @unittest.skipIf(sys.platform == "win32", "현재 host의 non-Windows dev 거부 경로 전용")
    def test_wrong_host_fails_before_python_or_vite_start(self) -> None:
        """
        함수 이름: test_wrong_host_fails_before_python_or_vite_start()
        기능: 잘못된 host에서 dev hook이 sidecar나 frontend를 시작하지 않는지 검사한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        # Child 출력은 고정 안내만 허용하고 native 동작을 PASS로 흉내 내지 않는다.
        result = subprocess.run(
            ["node", str(REPOSITORY_ROOT / "UI/scripts/desktopFrontend.mjs"), "dev"],
            capture_output=True, text=True, check=False, timeout=10,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr.strip(), "desktop:dev: Windows 11 x64 source development is required.")

    def test_credential_tool_uses_native_hidden_input_and_fixed_targets(self) -> None:
        """
        함수 이름: test_credential_tool_uses_native_hidden_input_and_fixed_targets()
        기능: native canary 실행 전에도 설정 도구의 공개 입력과 secret 출력 금지를 검사한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        # 이 정적 검사는 native credential set/read/delete의 실행 증거와 구분한다.
        script = (REPOSITORY_ROOT / "scripts/configure_testnet_credentials.ps1").read_text()
        adapter = (REPOSITORY_ROOT / "scripts/windows/CredentialManager.cs").read_text()
        self.assertIn("-AsSecureString", script)
        self.assertIn('com.binance-auto.trader.testnet/', adapter)
        self.assertIn('"api-key"', adapter)
        self.assertIn('"api-secret"', adapter)
        self.assertIn('"session5-canary"', adapter)
        for unsafe_conversion in ("PtrToString", "GetNetworkCredential", "ConvertFrom-SecureString"):
            self.assertNotIn(unsafe_conversion, script + adapter)
        self.assertIn("Marshal.ZeroFreeBSTR", adapter)
        self.assertIn('EntryPoint = "CredReadW"', adapter)
        self.assertIn('EntryPoint = "CredWriteW"', adapter)
        self.assertIn('EntryPoint = "CredDeleteW"', adapter)

    def test_launcher_keeps_windows_override_as_the_last_config(self) -> None:
        """
        함수 이름: test_launcher_keeps_windows_override_as_the_last_config()
        기능: 실행되는 JS argument 선택을 검사해 full base config의 재병합 회귀를 막는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        # 모듈 import는 native app을 시작하지 않고 공개된 순수 CLI argument 함수만 평가한다.
        launcher_url = (REPOSITORY_ROOT / "UI/scripts/desktopLauncher.mjs").as_uri()
        program = (
            f"import {{ get_tauri_arguments }} from {json.dumps(launcher_url)};"
            "console.log(JSON.stringify(['darwin', 'win32'].map(platform => "
            "get_tauri_arguments('dev', platform))));"
        )
        result = subprocess.run(
            ["node", "--input-type=module", "-e", program],
            capture_output=True, text=True, check=True, timeout=10,
        )
        self.assertEqual(json.loads(result.stdout), [
            ["dev", "--config", "apps/desktop/src-tauri/tauri.conf.json"],
            ["dev", "--config", "apps/desktop/src-tauri/tauri.windows.conf.json"],
        ])


if __name__ == "__main__":
    unittest.main()  # Native OS 검증을 대신하는 이름이나 PASS 표기를 추가하지 않는다.
