"""Phase 4 계좌·거래 이력 구현의 책임 경계와 코딩 규약을 검증한다."""

from __future__ import annotations

import ast
from pathlib import Path
import re
import unittest

from binance_auto_trader.domain.history import (
    Performance,
    Trade,
    TradeHistory,
)
from binance_auto_trader.domain.trading import (
    Account,
    AccountSnapshot,
    AssetBalance,
)


BACKEND_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = BACKEND_ROOT / "src" / "binance_auto_trader"
PHASE_FOUR_SOURCE_PATHS = (
    SOURCE_ROOT / "domain" / "trading" / "account.py",
    *tuple((SOURCE_ROOT / "domain" / "history").rglob("*.py")),
    *tuple((SOURCE_ROOT / "adapters" / "persistence").rglob("*.py")),
    SOURCE_ROOT / "application" / "trading_controller.py",
    SOURCE_ROOT / "application" / "trade_history_controller.py",
)
CLASS_NAME_PATTERN = re.compile(r"^_?[A-Z][A-Za-z0-9]*$")
FUNCTION_NAME_PATTERN = re.compile(
    r"^(?:__[a-z0-9_]+__|_*[a-z][a-z0-9_]*)$"
)
VARIABLE_NAME_PATTERN = re.compile(
    r"^(?:_*[a-z][a-z0-9_]*|_*[A-Z][A-Z0-9_]*)$"
)
DOCSTRING_DATE_PATTERN = re.compile(
    r"작성 날짜: [0-9]{4}/[0-9]{2}/[0-9]{2}"
)


class PhaseFourBoundaryArchitectureTests(unittest.TestCase):
    """
    클래스 이름: PhaseFourBoundaryArchitectureTests
    기능: Phase 4 공개 타입, 금융 수치와 계층 책임이 명세 경계를 지키는지 검증한다.
    작성 날짜: 2026/08/21
    """

    def test_phase_four_public_types_are_single_canonical_classes(self) -> None:
        """
        함수 이름: test_phase_four_public_types_are_single_canonical_classes()
        기능: 계좌와 이력 package 공개면이 구현 class를 복제 없이 노출하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        expected_public_types = (
            Account,
            AccountSnapshot,
            AssetBalance,
            Performance,
            Trade,
            TradeHistory,
        )

        for public_type in expected_public_types:
            with self.subTest(public_type=public_type.__name__):
                self.assertTrue(callable(public_type))
                self.assertTrue(
                    public_type.__module__.startswith(
                        "binance_auto_trader.domain"
                    )
                )

    def test_phase_four_financial_source_does_not_use_float(self) -> None:
        """
        함수 이름: test_phase_four_financial_source_does_not_use_float()
        기능: 계좌·이력·성과·저장소와 초기 load가 금융값에 float를 쓰지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        for source_path in PHASE_FOUR_SOURCE_PATHS:
            syntax_tree = ast.parse(source_path.read_text(encoding="utf-8"))
            float_nodes = tuple(
                syntax_node
                for syntax_node in ast.walk(syntax_tree)
                if (
                    isinstance(syntax_node, ast.Constant)
                    and isinstance(syntax_node.value, float)
                )
                or (
                    isinstance(syntax_node, ast.Name)
                    and syntax_node.id == "float"
                )
                or (
                    isinstance(syntax_node, ast.Attribute)
                    and syntax_node.attr == "float"
                )
            )

            with self.subTest(source_path=source_path):
                self.assertEqual(float_nodes, ())

    def test_filesystem_dependency_stays_in_persistence_adapter(self) -> None:
        """
        함수 이름: test_filesystem_dependency_stays_in_persistence_adapter()
        기능: Phase 4 domain과 application이 filesystem module을 import하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        persistence_root = SOURCE_ROOT / "adapters" / "persistence"
        forbidden_modules = {"os", "pathlib"}

        for source_path in PHASE_FOUR_SOURCE_PATHS:
            if source_path.is_relative_to(persistence_root):
                continue

            syntax_tree = ast.parse(source_path.read_text(encoding="utf-8"))
            imported_modules = set()
            for syntax_node in ast.walk(syntax_tree):
                if isinstance(syntax_node, ast.Import):
                    for imported_name in syntax_node.names:
                        imported_modules.add(imported_name.name.split(".")[0])
                elif (
                    isinstance(syntax_node, ast.ImportFrom)
                    and syntax_node.module is not None
                ):
                    imported_modules.add(syntax_node.module.split(".")[0])

            with self.subTest(source_path=source_path):
                self.assertFalse(imported_modules & forbidden_modules)

    def test_controllers_do_not_implement_order_csv_or_transport(self) -> None:
        """
        함수 이름: test_controllers_do_not_implement_order_csv_or_transport()
        기능: Phase 7 session Controller에 실제 주문·CSV·transport 책임이 섞이지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        controller_paths = (
            SOURCE_ROOT / "application" / "trading_controller.py",
            SOURCE_ROOT / "application" / "trade_history_controller.py",
        )
        forbidden_names = {
            "CSVFileGateway",
            "Order",
            "Position",
            "UIStateController",
        }
        forbidden_module_parts = {"bootstrap", "transport", "ui"}

        for source_path in controller_paths:
            syntax_tree = ast.parse(source_path.read_text(encoding="utf-8"))
            referenced_names = {
                syntax_node.id
                for syntax_node in ast.walk(syntax_tree)
                if isinstance(syntax_node, ast.Name)
            }
            imported_parts = set()
            for syntax_node in ast.walk(syntax_tree):
                if isinstance(syntax_node, ast.Import):
                    for imported_name in syntax_node.names:
                        imported_parts.update(imported_name.name.split("."))
                elif (
                    isinstance(syntax_node, ast.ImportFrom)
                    and syntax_node.module is not None
                ):
                    imported_parts.update(syntax_node.module.split("."))

            with self.subTest(source_path=source_path):
                self.assertFalse(referenced_names & forbidden_names)
                self.assertFalse(imported_parts & forbidden_module_parts)


class PhaseFourCodingConventionTests(unittest.TestCase):
    """
    클래스 이름: PhaseFourCodingConventionTests
    기능: Phase 4 production Python source의 이름, 들여쓰기와 docstring을 검증한다.
    작성 날짜: 2026/08/21
    """

    def test_phase_four_source_does_not_use_tabs(self) -> None:
        """
        함수 이름: test_phase_four_source_does_not_use_tabs()
        기능: Phase 4 production source가 탭 없이 공백 들여쓰기를 사용하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        for source_path in PHASE_FOUR_SOURCE_PATHS:
            with self.subTest(source_path=source_path):
                self.assertNotIn(
                    "\t",
                    source_path.read_text(encoding="utf-8"),
                )

    def test_phase_four_definitions_follow_required_name_styles(self) -> None:
        """
        함수 이름: test_phase_four_definitions_follow_required_name_styles()
        기능: Phase 4 클래스, 함수, 인자와 변수 이름이 표기 규약을 따르는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        for source_path in PHASE_FOUR_SOURCE_PATHS:
            syntax_tree = ast.parse(source_path.read_text(encoding="utf-8"))

            for definition in ast.walk(syntax_tree):
                if isinstance(definition, ast.ClassDef):
                    with self.subTest(
                        source_path=source_path,
                        class_name=definition.name,
                    ):
                        self.assertRegex(
                            definition.name,
                            CLASS_NAME_PATTERN,
                        )
                elif isinstance(
                    definition,
                    (ast.FunctionDef, ast.AsyncFunctionDef),
                ):
                    with self.subTest(
                        source_path=source_path,
                        function_name=definition.name,
                    ):
                        self.assertRegex(
                            definition.name,
                            FUNCTION_NAME_PATTERN,
                        )

                    function_arguments = (
                        *definition.args.posonlyargs,
                        *definition.args.args,
                        *definition.args.kwonlyargs,
                    )
                    for argument in function_arguments:
                        with self.subTest(
                            source_path=source_path,
                            argument_name=argument.arg,
                        ):
                            self.assertRegex(
                                argument.arg,
                                VARIABLE_NAME_PATTERN,
                            )

            for assignment in ast.walk(syntax_tree):
                if isinstance(assignment, ast.Assign):
                    assignment_targets = assignment.targets
                elif isinstance(assignment, ast.AnnAssign):
                    assignment_targets = (assignment.target,)
                elif isinstance(
                    assignment,
                    (ast.For, ast.AsyncFor, ast.comprehension),
                ):
                    assignment_targets = (assignment.target,)
                else:
                    continue

                for assignment_target in assignment_targets:
                    target_names = (
                        target.id
                        for target in ast.walk(assignment_target)
                        if isinstance(target, ast.Name)
                    )
                    for target_name in target_names:
                        with self.subTest(
                            source_path=source_path,
                            variable_name=target_name,
                        ):
                            self.assertRegex(
                                target_name,
                                VARIABLE_NAME_PATTERN,
                            )

    def test_phase_four_definitions_have_required_docstrings(self) -> None:
        """
        함수 이름: test_phase_four_definitions_have_required_docstrings()
        기능: Phase 4 모든 class와 함수가 한국어 필수 항목과 작성일을 갖는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        class_sections = ("클래스 이름:", "기능:")
        function_sections = (
            "함수 이름:",
            "기능:",
            "인자:",
            "반환값:",
        )

        for source_path in PHASE_FOUR_SOURCE_PATHS:
            syntax_tree = ast.parse(source_path.read_text(encoding="utf-8"))

            for definition in ast.walk(syntax_tree):
                if isinstance(definition, ast.ClassDef):
                    docstring = ast.get_docstring(definition) or ""
                    with self.subTest(
                        source_path=source_path,
                        class_name=definition.name,
                    ):
                        self.assertIn(
                            f"클래스 이름: {definition.name}",
                            docstring,
                        )
                        self.assertTrue(
                            all(
                                section in docstring
                                for section in class_sections
                            )
                        )
                        self.assertRegex(
                            docstring,
                            DOCSTRING_DATE_PATTERN,
                        )
                elif isinstance(
                    definition,
                    (ast.FunctionDef, ast.AsyncFunctionDef),
                ):
                    docstring = ast.get_docstring(definition) or ""
                    with self.subTest(
                        source_path=source_path,
                        function_name=definition.name,
                    ):
                        self.assertIn(
                            f"함수 이름: {definition.name}()",
                            docstring,
                        )
                        self.assertTrue(
                            all(
                                section in docstring
                                for section in function_sections
                            )
                        )
                        self.assertRegex(
                            docstring,
                            DOCSTRING_DATE_PATTERN,
                        )


if __name__ == "__main__":
    unittest.main()
