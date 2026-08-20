"""TradingSTM source가 프로젝트 coding convention을 계속 지키는지 검증한다."""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path


SOURCE_ROOT = (
    Path(__file__).parents[2]
    / "src"
    / "binance_auto_trader"
    / "domain"
    / "trading"
)
CLASS_NAME_PATTERN = re.compile(r"^_?[A-Z][A-Za-z0-9]*$")
FUNCTION_NAME_PATTERN = re.compile(r"^(?:__[a-z0-9_]+__|_*[a-z][a-z0-9_]*)$")
VARIABLE_NAME_PATTERN = re.compile(r"^(?:_*[a-z][a-z0-9_]*|_*[A-Z][A-Z0-9_]*)$")


class CodingConventionTests(unittest.TestCase):
    """
    클래스 이름: CodingConventionTests
    기능: TradingSTM의 이름과 docstring 형식이 coding convention을 따르는지 검증한다.
    작성 날짜: 2026/08/14
    """

    def test_source_definitions_have_required_docstring_sections(self) -> None:
        """
        함수 이름: test_source_definitions_have_required_docstring_sections()
        기능: 모든 source 클래스와 함수에 명세된 docstring 항목이 있는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        class_sections = ("클래스 이름:", "기능:", "작성 날짜:")
        function_sections = (
            "함수 이름:",
            "기능:",
            "인자:",
            "반환값:",
            "작성 날짜:",
        )

        # package의 모든 Python source를 AST로 읽어 정의별 docstring을 검사한다.
        for source_path in SOURCE_ROOT.rglob("*.py"):
            syntax_tree = ast.parse(source_path.read_text(encoding="utf-8"))
            for definition in ast.walk(syntax_tree):
                if isinstance(definition, ast.ClassDef):
                    docstring = ast.get_docstring(definition) or ""
                    with self.subTest(path=source_path, class_name=definition.name):
                        self.assertTrue(
                            all(section in docstring for section in class_sections)
                        )
                        self.assertIn(
                            f"클래스 이름: {definition.name}",
                            docstring,
                        )
                elif isinstance(
                    definition,
                    (ast.FunctionDef, ast.AsyncFunctionDef),
                ):
                    docstring = ast.get_docstring(definition) or ""
                    with self.subTest(path=source_path, function=definition.name):
                        self.assertTrue(
                            all(section in docstring for section in function_sections)
                        )
                        self.assertIn(
                            f"함수 이름: {definition.name}()",
                            docstring,
                        )

    def test_source_symbols_follow_required_name_styles(self) -> None:
        """
        함수 이름: test_source_symbols_follow_required_name_styles()
        기능: 클래스·함수·인자·변수 이름이 명세된 표기법을 따르는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        for source_path in SOURCE_ROOT.rglob("*.py"):
            syntax_tree = ast.parse(source_path.read_text(encoding="utf-8"))

            # 클래스는 PascalCase, 함수와 모든 인자는 snake_case인지 확인한다.
            for definition in ast.walk(syntax_tree):
                if isinstance(definition, ast.ClassDef):
                    with self.subTest(path=source_path, class_name=definition.name):
                        self.assertRegex(definition.name, CLASS_NAME_PATTERN)
                elif isinstance(
                    definition,
                    (ast.FunctionDef, ast.AsyncFunctionDef),
                ):
                    with self.subTest(path=source_path, function=definition.name):
                        self.assertRegex(definition.name, FUNCTION_NAME_PATTERN)

                    function_arguments = (
                        *definition.args.posonlyargs,
                        *definition.args.args,
                        *definition.args.kwonlyargs,
                    )
                    for argument in function_arguments:
                        with self.subTest(path=source_path, argument=argument.arg):
                            self.assertRegex(argument.arg, VARIABLE_NAME_PATTERN)

            # 대입 대상은 snake_case 또는 상수용 UPPER_SNAKE_CASE인지 확인한다.
            for assignment in ast.walk(syntax_tree):
                if isinstance(assignment, ast.Assign):
                    assignment_targets = assignment.targets
                elif isinstance(assignment, ast.AnnAssign):
                    assignment_targets = [assignment.target]
                    if (
                        isinstance(assignment.target, ast.Name)
                        and isinstance(assignment.annotation, ast.Name)
                        and assignment.annotation.id == "TypeAlias"
                    ):
                        continue
                elif isinstance(
                    assignment,
                    (ast.For, ast.AsyncFor, ast.comprehension),
                ):
                    assignment_targets = [assignment.target]
                else:
                    continue

                for assignment_target in assignment_targets:
                    for target_name in (
                        target
                        for target in ast.walk(assignment_target)
                        if isinstance(target, ast.Name)
                    ):
                        with self.subTest(path=source_path, variable=target_name.id):
                            self.assertTrue(
                                target_name.id == "_"
                                or VARIABLE_NAME_PATTERN.fullmatch(target_name.id)
                            )


if __name__ == "__main__":
    unittest.main()
