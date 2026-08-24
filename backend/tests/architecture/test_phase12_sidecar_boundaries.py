"""Phase 12 production sidecar의 secret channel과 safe-exit architecture를 고정한다."""

from __future__ import annotations

import ast
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


class PhaseTwelveSidecarBoundaryTests(unittest.TestCase):
    """
    클래스 이름: PhaseTwelveSidecarBoundaryTests
    기능: credential의 argv/env/log 금지와 fixed FD 및 safe process 옵션을 AST로 검증한다.
    작성 날짜: 2026/08/24
    """

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
