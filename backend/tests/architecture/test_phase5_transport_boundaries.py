"""Phase 5 transport dependency, documentation과 보안 source boundary를 검증한다."""

import ast
from io import StringIO
from inspect import signature
from pathlib import Path
import tokenize
import unittest

from binance_auto_trader.transport import run_transport_process


TRANSPORT_ROOT = (  # 검증 대상을 production transport package 하나로 제한한다.
    Path(__file__).resolve().parents[2]
    / "src"
    / "binance_auto_trader"
    / "transport"
)


def _transport_source_paths() -> tuple[Path, ...]:
    """
    함수 이름: _transport_source_paths()
    기능: generated cache를 제외한 모든 Phase 5 transport Python source를 정렬한다.
    인자: 없음
    반환값: transport source path tuple
    작성 날짜: 2026/08/21
    """
    return tuple(sorted(TRANSPORT_ROOT.rglob("*.py")))


class Phase5TransportBoundaryTests(unittest.TestCase):
    """
    클래스 이름: Phase5TransportBoundaryTests
    기능: transport가 STM business를 침범하지 않고 coding convention을 지키는지 검증한다.
    작성 날짜: 2026/08/21
    """

    def test_transport_never_imports_or_calls_trading_stm(self) -> None:
        """
        함수 이름: test_transport_never_imports_or_calls_trading_stm()
        기능: HTTP/WS adapter가 Phase 7 TradingSTM owner operation을 직접 참조하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # Transport는 DTO mapping과 기존 Controller route만 소유해야 한다.
        for source_path in _transport_source_paths():
            source_text = source_path.read_text(encoding="utf-8")
            syntax_tree = ast.parse(source_text)
            imported_modules = []
            referenced_names = []
            for syntax_node in ast.walk(syntax_tree):
                if isinstance(syntax_node, ast.Import):
                    imported_modules.extend(
                        alias.name
                        for alias in syntax_node.names
                    )
                elif isinstance(syntax_node, ast.ImportFrom):
                    imported_modules.append(syntax_node.module or "")
                    referenced_names.extend(
                        alias.name
                        for alias in syntax_node.names
                    )
                elif isinstance(syntax_node, ast.Name):
                    referenced_names.append(syntax_node.id)

            with self.subTest(source_path=source_path.name):
                self.assertNotIn("TradingSTM", referenced_names)
                self.assertTrue(
                    all(
                        "domain.trading.stm" not in module_name
                        for module_name in imported_modules
                    )
                )

    def test_process_runner_owns_runtime_factory_and_shared_event_stream(
        self,
    ) -> None:
        """
        함수 이름: test_process_runner_owns_runtime_factory_and_shared_event_stream()
        기능: actual process API가 preassembled runtime/stream을 받아 identity를 우회하지 못하게 한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        parameter_names = tuple(signature(run_transport_process).parameters)

        # Factory만 공개해 runner 내부의 observer와 server stream이 한 조립 경로를 거친다.
        self.assertEqual(parameter_names[0], "runtime_factory")
        self.assertNotIn("runtime", parameter_names)
        self.assertNotIn("event_stream", parameter_names)

    def test_route_modules_do_not_import_domain_business_types(self) -> None:
        """
        함수 이름: test_route_modules_do_not_import_domain_business_types()
        기능: shape 검증 route가 domain guard나 state machine 판단을 재구현하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        route_root = TRANSPORT_ROOT / "routes"

        # 모든 owner 호출은 application/bootstrap protocol 뒤에 남기고 route는 DTO만 다룬다.
        for source_path in sorted(route_root.glob("*.py")):
            source_text = source_path.read_text(encoding="utf-8")
            with self.subTest(source_path=source_path.name):
                self.assertNotIn("binance_auto_trader.domain", source_text)

    def test_every_class_and_function_has_full_korean_convention_docstring(
        self,
    ) -> None:
        """
        함수 이름: test_every_class_and_function_has_full_korean_convention_docstring()
        기능: 신규 transport 클래스와 함수가 필수 이름·기능·인자·반환·날짜 설명을 갖는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        for source_path in _transport_source_paths():
            syntax_tree = ast.parse(source_path.read_text(encoding="utf-8"))
            self.assertIsNotNone(
                ast.get_docstring(syntax_tree),
                f"{source_path} requires a module docstring",
            )

            # Class와 nested function까지 AST 전체를 순회해 docstring 누락을 막는다.
            for syntax_node in ast.walk(syntax_tree):
                if isinstance(syntax_node, ast.ClassDef):
                    class_docstring = ast.get_docstring(syntax_node) or ""
                    with self.subTest(
                        source_path=source_path.name,
                        class_name=syntax_node.name,
                    ):
                        self.assertIn("클래스 이름:", class_docstring)
                        self.assertIn("기능:", class_docstring)
                        self.assertIn("작성 날짜:", class_docstring)
                elif isinstance(
                    syntax_node,
                    (ast.FunctionDef, ast.AsyncFunctionDef),
                ):
                    function_docstring = ast.get_docstring(syntax_node) or ""
                    with self.subTest(
                        source_path=source_path.name,
                        function_name=syntax_node.name,
                    ):
                        self.assertIn("함수 이름:", function_docstring)
                        self.assertIn("기능:", function_docstring)
                        self.assertIn("인자:", function_docstring)
                        self.assertIn("반환값:", function_docstring)
                        self.assertIn("작성 날짜:", function_docstring)

    def test_every_source_includes_meaningful_block_and_sentence_comments(
        self,
    ) -> None:
        """
        함수 이름: test_every_source_includes_meaningful_block_and_sentence_comments()
        기능: 모든 transport source가 docstring 외 블록·문장 주석 형식을 함께 지키는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        for source_path in _transport_source_paths():
            source_text = source_path.read_text(encoding="utf-8")
            source_lines = source_text.splitlines()
            source_tokens = tokenize.generate_tokens(StringIO(source_text).readline)
            comment_tokens = tuple(
                source_token
                for source_token in source_tokens
                if source_token.type == tokenize.COMMENT
            )
            block_comments = []
            sentence_comments = []

            # 주석 앞에 코드가 있는지로 coding convention의 두 주석 형식을 구분한다.
            for comment_token in comment_tokens:
                row_number, column_number = comment_token.start
                source_prefix = source_lines[row_number - 1][:column_number]
                if source_prefix.strip():
                    sentence_comments.append(comment_token)
                    self.assertTrue(
                        source_prefix.endswith("  "),
                        f"{source_path}:{row_number} requires two spaces before #",
                    )
                else:
                    block_comments.append(comment_token)

                # 빈 표식이나 자동검사 회피용 한 단어 주석은 의미 있는 설명으로 보지 않는다.
                comment_text = comment_token.string.lstrip("# ").strip()
                self.assertGreaterEqual(
                    len(comment_text),
                    10,
                    f"{source_path}:{row_number} requires a meaningful comment",
                )

            with self.subTest(source_path=source_path.name):
                self.assertGreaterEqual(
                    len(block_comments),
                    1,
                    "transport source requires a block comment",
                )
                self.assertGreaterEqual(
                    len(sentence_comments),
                    1,
                    "transport source requires a sentence comment",
                )

    def test_transport_source_contains_no_public_bind_or_secret_log_pattern(
        self,
    ) -> None:
        """
        함수 이름: test_transport_source_contains_no_public_bind_or_secret_log_pattern()
        기능: public interface bind와 token/header/body access logging pattern을 source에서 차단한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        combined_source = "\n".join(
            source_path.read_text(encoding="utf-8")
            for source_path in _transport_source_paths()
        )

        self.assertNotIn('"0.0.0.0"', combined_source)
        self.assertNotIn("logging.basicConfig", combined_source)
        self.assertNotIn("print(session_token", combined_source)


if __name__ == "__main__":
    unittest.main()
