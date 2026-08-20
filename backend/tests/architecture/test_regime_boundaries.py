"""RegimeSTM의 책임 경계, 추적성 및 코딩 컨벤션을 정적으로 검증한다."""

import ast
import inspect
import re
import unittest
from pathlib import Path
from typing import get_type_hints

from binance_auto_trader.domain.regime import (
    RegimeSTM,
    RegimeSTMResult,
    SPECIFICATION_TRANSITION_IDS,
    TRANSITION_IDS,
    TRANSITION_REGISTRY,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = (
    PROJECT_ROOT
    / "src"
    / "binance_auto_trader"
    / "domain"
    / "regime"
)
COMMON_PACKAGE_ROOT = (
    PROJECT_ROOT
    / "src"
    / "binance_auto_trader"
    / "domain"
    / "common"
)
PYTHON_PATHS = (
    tuple(PACKAGE_ROOT.rglob("*.py"))
    + tuple(COMMON_PACKAGE_ROOT.rglob("*.py"))
    + tuple((PROJECT_ROOT / "tests" / "unit" / "regime").rglob("*.py"))
    + (Path(__file__),)
)


class RegimeSTMArchitectureTests(unittest.TestCase):
    """
    클래스 이름: RegimeSTMArchitectureTests
    기능: STM package의 설계 책임과 Event-Action 추적성을 테스트한다.
    작성 날짜: 2026/08/14
    """

    def test_registry_contains_exactly_thirteen_specification_ids(self) -> None:
        """
        함수 이름: test_registry_contains_exactly_thirteen_specification_ids()
        기능: transition registry가 명세의 13개 ID를 누락과 중복 없이 포함하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        registry_ids = tuple(
            transition.transition_id
            for transition in TRANSITION_REGISTRY
        )

        self.assertEqual(len(registry_ids), 13)
        self.assertEqual(len(registry_ids), len(set(registry_ids)))
        self.assertEqual(TRANSITION_IDS, SPECIFICATION_TRANSITION_IDS)

    def test_pure_stm_modules_do_not_import_external_layers(self) -> None:
        """
        함수 이름: test_pure_stm_modules_do_not_import_external_layers()
        기능: STM, guard 및 transition 모듈이 외부 계층이나 I/O 모듈을 import하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        pure_module_paths = (
            PACKAGE_ROOT / "stm.py",
            PACKAGE_ROOT / "guards.py",
            PACKAGE_ROOT / "transitions.py",
        )
        forbidden_roots = {
            "asyncio",
            "controller",
            "gateway",
            "http",
            "pathlib",
            "requests",
            "socket",
            "time",
            "ui",
            "urllib",
        }

        for module_path in pure_module_paths:
            syntax_tree = ast.parse(module_path.read_text(encoding="utf-8"))
            imported_roots = set()

            for syntax_node in ast.walk(syntax_tree):
                if isinstance(syntax_node, ast.Import):
                    imported_roots.update(
                        alias.name.split(".")[0]
                        for alias in syntax_node.names
                    )
                elif isinstance(syntax_node, ast.ImportFrom):
                    if syntax_node.module is not None:
                        imported_roots.add(
                            syntax_node.module.split(".")[0]
                        )

            with self.subTest(module_path=module_path.name):
                self.assertFalse(imported_roots & forbidden_roots)

    def test_stm_modules_do_not_write_controller_owned_values(self) -> None:
        """
        함수 이름: test_stm_modules_do_not_write_controller_owned_values()
        기능: 순수 STM package가 추천값이나 사용자 선택값을 대입하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        forbidden_names = {"recommended_regime", "selected_regime"}

        for module_path in PACKAGE_ROOT.glob("*.py"):
            syntax_tree = ast.parse(module_path.read_text(encoding="utf-8"))

            for syntax_node in ast.walk(syntax_tree):
                if not isinstance(syntax_node, (ast.Assign, ast.AnnAssign)):
                    continue

                targets = (
                    syntax_node.targets
                    if isinstance(syntax_node, ast.Assign)
                    else (syntax_node.target,)
                )
                for target in targets:
                    if isinstance(target, ast.Name):
                        self.assertNotIn(target.id, forbidden_names)
                    elif isinstance(target, ast.Attribute):
                        self.assertNotIn(target.attr, forbidden_names)

    def test_handle_returns_regime_stm_result(self) -> None:
        """
        함수 이름: test_handle_returns_regime_stm_result()
        기능: canonical handle operation의 반환형이 RegimeSTMResult인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        type_hints = get_type_hints(RegimeSTM.handle)

        self.assertIs(type_hints["return"], RegimeSTMResult)
        self.assertFalse(inspect.iscoroutinefunction(RegimeSTM.handle))


class CodingConventionTests(unittest.TestCase):
    """
    클래스 이름: CodingConventionTests
    기능: 신규 Python 코드가 프로젝트 Coding Conventions의 형식을 따르는지 테스트한다.
    작성 날짜: 2026/08/14
    """

    def test_python_files_do_not_use_tabs(self) -> None:
        """
        함수 이름: test_python_files_do_not_use_tabs()
        기능: 모든 신규 Python 파일이 탭 없이 공백 들여쓰기만 사용하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        for python_path in PYTHON_PATHS:
            source_text = python_path.read_text(encoding="utf-8")

            with self.subTest(python_path=python_path):
                self.assertNotIn("\t", source_text)

    def test_classes_and_functions_follow_naming_convention(self) -> None:
        """
        함수 이름: test_classes_and_functions_follow_naming_convention()
        기능: 클래스는 PascalCase, 함수는 snake_case로 작성됐는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        class_name_pattern = re.compile(r"^[A-Z][A-Za-z0-9]*$")
        function_name_pattern = re.compile(r"^[a-z_][a-z0-9_]*$")

        for python_path in PYTHON_PATHS:
            syntax_tree = ast.parse(python_path.read_text(encoding="utf-8"))

            for syntax_node in ast.walk(syntax_tree):
                if isinstance(syntax_node, ast.ClassDef):
                    with self.subTest(
                        python_path=python_path,
                        class_name=syntax_node.name,
                    ):
                        self.assertRegex(
                            syntax_node.name,
                            class_name_pattern,
                        )
                elif isinstance(
                    syntax_node,
                    (ast.FunctionDef, ast.AsyncFunctionDef),
                ):
                    with self.subTest(
                        python_path=python_path,
                        function_name=syntax_node.name,
                    ):
                        self.assertRegex(
                            syntax_node.name,
                            function_name_pattern,
                        )

    def test_classes_and_functions_have_required_docstrings(self) -> None:
        """
        함수 이름: test_classes_and_functions_have_required_docstrings()
        기능: 클래스와 함수 docstring에 코딩 컨벤션의 필수 설명 항목이 있는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        class_markers = ("클래스 이름:", "기능:", "작성 날짜:")
        function_markers = (
            "함수 이름:",
            "기능:",
            "인자:",
            "반환값:",
            "작성 날짜:",
        )

        for python_path in PYTHON_PATHS:
            syntax_tree = ast.parse(python_path.read_text(encoding="utf-8"))

            for syntax_node in ast.walk(syntax_tree):
                if isinstance(syntax_node, ast.ClassDef):
                    docstring = ast.get_docstring(syntax_node) or ""

                    with self.subTest(
                        python_path=python_path,
                        class_name=syntax_node.name,
                    ):
                        for marker in class_markers:
                            self.assertIn(marker, docstring)
                elif isinstance(
                    syntax_node,
                    (ast.FunctionDef, ast.AsyncFunctionDef),
                ):
                    docstring = ast.get_docstring(syntax_node) or ""

                    with self.subTest(
                        python_path=python_path,
                        function_name=syntax_node.name,
                    ):
                        for marker in function_markers:
                            self.assertIn(marker, docstring)


if __name__ == "__main__":
    unittest.main()
