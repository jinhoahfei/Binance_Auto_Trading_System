"""Phase 12 production sidecar의 secret channel과 safe-exit architecture를 고정한다."""

from __future__ import annotations

import ast
import json
from inspect import signature
from pathlib import Path
import unittest

from binance_auto_trader.bootstrap.sidecar import (
    READY_DESCRIPTOR_FD,
    SESSION_TOKEN_FD,
    SIDECAR_CONFIGURATION_FD,
    STOP_SIGNAL_FD,
)
from binance_auto_trader.transport import run_transport_process


PACKAGE_ROOT = (
    Path(__file__).resolve().parents[2] / "src" / "binance_auto_trader"
)

# Bundle metadata test가 source tree의 authoritative Tauri 설정만 읽도록 path를 고정한다.
TAURI_CONFIGURATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "UI"
    / "apps"
    / "desktop"
    / "src-tauri"
    / "tauri.conf.json"
)

# Release signing regression test가 authoritative sidecar package script를 직접 읽는다.
PACKAGE_SIDECAR_SCRIPT_PATH = (
    Path(__file__).resolve().parents[3] / "scripts" / "package_sidecar.sh"
)


class PhaseTwelveSidecarBoundaryTests(unittest.TestCase):
    """
    클래스 이름: PhaseTwelveSidecarBoundaryTests
    기능: Phase 12 bundle metadata와 credential·fixed FD·safe process 경계를 검증한다.
    작성 날짜: 2026/08/24
    """

    def test_tauri_configuration_declares_macos_eleven_minimum_version(self) -> None:
        """
        함수 이름: test_tauri_configuration_declares_macos_eleven_minimum_version()
        기능: Phase 12 Tauri source 설정이 macOS 11.0 최소 버전을 명시하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # Tauri 설정을 직접 읽어 app bundle의 배포 대상 metadata 회귀를 차단한다.
        tauri_configuration = json.loads(
            TAURI_CONFIGURATION_PATH.read_text(encoding="utf-8")
        )
        minimum_system_version = tauri_configuration["bundle"]["macOS"][
            "minimumSystemVersion"
        ]  # 실제 app의 Info.plist와 Mach-O 일치는 package smoke에서 별도로 검증한다.

        self.assertEqual(minimum_system_version, "11.0")

    def test_package_script_contains_explicit_signing_identity_contract(self) -> None:
        """
        함수 이름: test_package_script_contains_explicit_signing_identity_contract()
        기능: package script source에 명시 signing identity option과 거부 경계가 있는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # 실제 shell 행동 테스트와 별개로 authoritative script의 signing option 경계를 정적으로 고정한다.
        package_script_source = PACKAGE_SIDECAR_SCRIPT_PATH.read_text(
            encoding="utf-8"
        )

        self.assertIn(
            '--codesign-identity "${APPLE_SIGNING_IDENTITY}"',
            package_script_source,
        )
        self.assertIn(
            '[ "${APPLE_SIGNING_IDENTITY:-}" = "-" ]',
            package_script_source,
        )
        self.assertIn(
            '[ -n "${APPLE_CERTIFICATE:-}" ] && [ -z "${APPLE_SIGNING_IDENTITY:-}" ]',
            package_script_source,
        )

    def test_production_entrypoint_uses_only_fixed_fd_contract(self) -> None:
        """
        함수 이름: test_production_entrypoint_uses_only_fixed_fd_contract()
        기능: launcher와 child가 합의한 token/ready/stop/config 번호가 3/4/5/6인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        self.assertEqual(
            (
                SESSION_TOKEN_FD,
                READY_DESCRIPTOR_FD,
                STOP_SIGNAL_FD,
                SIDECAR_CONFIGURATION_FD,
            ),
            (3, 4, 5, 6),
        )
        self.assertIn(
            "require_closed_before_stop",
            signature(run_transport_process).parameters,
        )  # Generic test runner와 production safe-exit owner를 explicit option으로 구분한다.

    def test_sidecar_source_does_not_read_argv_environment_or_log(self) -> None:
        """
        함수 이름: test_sidecar_source_does_not_read_argv_environment_or_log()
        기능: production sidecar 두 module이 credential fallback과 출력 API를 참조하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        source_paths = (
            PACKAGE_ROOT / "sidecar.py",
            PACKAGE_ROOT / "bootstrap" / "sidecar.py",
        )
        forbidden_names = {
            "argv",
            "environ",
            "getenv",
            "logging",
            "print",
        }

        # AST name과 attribute만 검사해 docstring의 보안 설명 문구에는 결합하지 않는다.
        for source_path in source_paths:
            syntax_tree = ast.parse(source_path.read_text(encoding="utf-8"))
            referenced_names = {
                syntax_node.id
                for syntax_node in ast.walk(syntax_tree)
                if isinstance(syntax_node, ast.Name)
            }
            referenced_names.update(
                syntax_node.attr
                for syntax_node in ast.walk(syntax_tree)
                if isinstance(syntax_node, ast.Attribute)
            )
            with self.subTest(source_path=source_path.name):
                self.assertTrue(forbidden_names.isdisjoint(referenced_names))

    def test_bootstrap_sidecar_does_not_depend_on_transport_layer(self) -> None:
        """
        함수 이름: test_bootstrap_sidecar_does_not_depend_on_transport_layer()
        기능: shared config parser가 transport를 역참조하지 않고 top-level entrypoint만 두 경계를 조립하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        bootstrap_source = (
            PACKAGE_ROOT / "bootstrap" / "sidecar.py"
        ).read_text(encoding="utf-8")
        entrypoint_source = (PACKAGE_ROOT / "sidecar.py").read_text(
            encoding="utf-8"
        )

        # Bootstrap은 inward dependency만 유지하고 executable composition root가 transport를 연결한다.
        self.assertNotIn("binance_auto_trader.transport", bootstrap_source)
        self.assertIn("binance_auto_trader.transport", entrypoint_source)
        self.assertIn("require_closed_before_stop=True", entrypoint_source)


if __name__ == "__main__":
    unittest.main()
