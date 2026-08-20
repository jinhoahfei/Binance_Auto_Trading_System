"""Phase 5 bootstrap의 계층 경계, 공개 타입과 코딩 규약을 검증한다."""

from __future__ import annotations

import ast
from pathlib import Path
import re
import unittest

from binance_auto_trader.bootstrap import ExecutionMode


BACKEND_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = BACKEND_ROOT / "src" / "binance_auto_trader"
BOOTSTRAP_ROOT = PACKAGE_ROOT / "bootstrap"
BOOTSTRAP_SOURCE_PATHS = tuple(BOOTSTRAP_ROOT.glob("*.py"))  # package 공개 경계도 포함한다.
CLASS_NAME_PATTERN = re.compile(r"^_?[A-Z][A-Za-z0-9]*$")
FUNCTION_NAME_PATTERN = re.compile(
    r"^(?:__[a-z0-9_]+__|_*[a-z][a-z0-9_]*)$"
)
DOCSTRING_DATE_PATTERN = re.compile(
    r"작성 날짜: [0-9]{4}/[0-9]{2}/[0-9]{2}"
)


def _imported_module_names(syntax_tree: ast.AST) -> set[str]:
    """
    함수 이름: _imported_module_names()
    기능: Python AST에서 import와 from import의 전체 module 이름을 수집한다.
    인자: syntax_tree -> 분석할 source AST
    반환값: source가 직접 import한 module 이름 set
    작성 날짜: 2026/08/21
    """
    imported_modules: set[str] = set()
    for syntax_node in ast.walk(syntax_tree):
        if isinstance(syntax_node, ast.Import):
            imported_modules.update(
                imported_name.name
                for imported_name in syntax_node.names
            )
        elif (
            isinstance(syntax_node, ast.ImportFrom)
            and syntax_node.module is not None
        ):
            imported_modules.add(syntax_node.module)

    return imported_modules


class PhaseFiveBootstrapBoundaryTests(unittest.TestCase):
    """
    클래스 이름: PhaseFiveBootstrapBoundaryTests
    기능: bootstrap이 조립 root에만 있고 domain·tests·transport와 순환하지 않는지 검증한다.
    작성 날짜: 2026/08/21
    """

    def test_execution_mode_is_owned_only_by_bootstrap_boundary(self) -> None:
        """
        함수 이름: test_execution_mode_is_owned_only_by_bootstrap_boundary()
        기능: ExecutionMode 구현이 domain이 아니라 bootstrap module에 속하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.assertEqual(
            ExecutionMode.__module__,
            "binance_auto_trader.bootstrap.application",
        )

        # Domain은 실행 환경 mode나 bootstrap 조립 책임을 참조하지 않는다.
        domain_root = PACKAGE_ROOT / "domain"
        for source_path in domain_root.rglob("*.py"):
            source_text = source_path.read_text(encoding="utf-8")
            with self.subTest(source_path=source_path):
                self.assertNotIn("ExecutionMode", source_text)
                self.assertNotIn("binance_auto_trader.bootstrap", source_text)

    def test_bootstrap_depends_inward_without_test_or_transport_imports(
        self,
    ) -> None:
        """
        함수 이름: test_bootstrap_depends_inward_without_test_or_transport_imports()
        기능: composition root가 기존 계층만 import하고 tests fake나 transport에 의존하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        forbidden_module_prefixes = (
            "tests",
            "binance_auto_trader.transport",
            "UI",
        )

        # Production bootstrap의 모든 import를 AST로 검사해 순환 방향을 차단한다.
        for source_path in BOOTSTRAP_SOURCE_PATHS:
            syntax_tree = ast.parse(source_path.read_text(encoding="utf-8"))
            imported_modules = _imported_module_names(syntax_tree)
            with self.subTest(source_path=source_path):
                self.assertFalse(
                    any(
                        imported_module.startswith(forbidden_prefix)
                        for imported_module in imported_modules
                        for forbidden_prefix in forbidden_module_prefixes
                    )
                )

    def test_inner_layers_do_not_import_bootstrap(self) -> None:
        """
        함수 이름: test_inner_layers_do_not_import_bootstrap()
        기능: domain, application과 adapters가 composition root를 역참조하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        inner_layer_names = ("domain", "application", "adapters")

        # 조립 방향은 bootstrap에서 안쪽 계층으로만 향해야 한다.
        for layer_name in inner_layer_names:
            layer_root = PACKAGE_ROOT / layer_name
            for source_path in layer_root.rglob("*.py"):
                syntax_tree = ast.parse(
                    source_path.read_text(encoding="utf-8")
                )
                with self.subTest(source_path=source_path):
                    self.assertNotIn(
                        "binance_auto_trader.bootstrap",
                        _imported_module_names(syntax_tree),
                    )


class PhaseFiveBootstrapCodingConventionTests(unittest.TestCase):
    """
    클래스 이름: PhaseFiveBootstrapCodingConventionTests
    기능: 신규 bootstrap source의 이름, docstring, 들여쓰기와 주석 형식을 검증한다.
    작성 날짜: 2026/08/21
    """

    def test_bootstrap_definitions_have_required_korean_docstrings(self) -> None:
        """
        함수 이름: test_bootstrap_definitions_have_required_korean_docstrings()
        기능: 모든 bootstrap 클래스와 함수가 명세된 한국어 docstring 항목을 갖는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        class_sections = ("클래스 이름:", "기능:", "작성 날짜:")
        function_sections = (
            "함수 이름:",
            "기능:",
            "인자:",
            "반환값:",
            "작성 날짜:",
        )

        # Package의 중첩 callback까지 AST로 순회해 누락된 함수 주석을 찾는다.
        for source_path in BOOTSTRAP_SOURCE_PATHS:
            syntax_tree = ast.parse(source_path.read_text(encoding="utf-8"))
            for definition in ast.walk(syntax_tree):
                if isinstance(definition, ast.ClassDef):
                    docstring = ast.get_docstring(definition) or ""
                    with self.subTest(
                        source_path=source_path,
                        class_name=definition.name,
                    ):
                        self.assertRegex(definition.name, CLASS_NAME_PATTERN)
                        self.assertTrue(
                            all(section in docstring for section in class_sections)
                        )
                        self.assertIn(
                            f"클래스 이름: {definition.name}",
                            docstring,
                        )
                        self.assertRegex(docstring, DOCSTRING_DATE_PATTERN)
                elif isinstance(
                    definition,
                    (ast.FunctionDef, ast.AsyncFunctionDef),
                ):
                    docstring = ast.get_docstring(definition) or ""
                    with self.subTest(
                        source_path=source_path,
                        function_name=definition.name,
                    ):
                        self.assertRegex(
                            definition.name,
                            FUNCTION_NAME_PATTERN,
                        )
                        self.assertTrue(
                            all(
                                section in docstring
                                for section in function_sections
                            )
                        )
                        self.assertIn(
                            f"함수 이름: {definition.name}()",
                            docstring,
                        )
                        self.assertRegex(docstring, DOCSTRING_DATE_PATTERN)

    def test_bootstrap_uses_spaces_block_comments_and_sentence_comments(
        self,
    ) -> None:
        """
        함수 이름: test_bootstrap_uses_spaces_block_comments_and_sentence_comments()
        기능: 실행 source가 탭 없이 블록 주석과 문장 뒤 inline 주석을 모두 포함하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 공개 __init__을 포함한 모든 bootstrap source가 두 주석 형식을 명시적으로 보존한다.
        for source_path in BOOTSTRAP_SOURCE_PATHS:
            source_text = source_path.read_text(encoding="utf-8")
            source_lines = source_text.splitlines()
            block_comment_lines = tuple(
                line
                for line in source_lines
                if line.lstrip().startswith("#")
            )
            sentence_comment_lines = tuple(
                line
                for line in source_lines
                if re.search(r"\S {2,}# \S", line)
            )

            with self.subTest(source_path=source_path):
                self.assertNotIn("\t", source_text)
                self.assertTrue(block_comment_lines)
                self.assertTrue(sentence_comment_lines)


if __name__ == "__main__":
    unittest.main()
