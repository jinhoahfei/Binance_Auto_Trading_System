"""Session 2 결정론적 fixture가 production order 경계를 우회하지 않게 고정한다."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


BACKEND_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = BACKEND_ROOT / "src" / "binance_auto_trader"
FIXTURE_PATH = (
    BACKEND_ROOT
    / "tests"
    / "testnet"
    / "_deterministic_public_case2_fixture.py"
)
FULL_FLOW_PATH = (
    BACKEND_ROOT
    / "tests"
    / "integration"
    / "test_deterministic_production_path_case2_flow.py"
)
ACTUAL_ONE_SHOT_PATH = (
    BACKEND_ROOT
    / "tests"
    / "testnet"
    / "test_phase13_public_market_case2.py"
)
_FIXTURE_IDENTIFIERS = {
    "DeterministicPublicCase2Klines",
    "create_deterministic_public_case2_klines",
    "_deterministic_public_case2_fixture",
}
_FORBIDDEN_ORDER_CALLS = {
    "SubmitOrder",
    "_execute_action",
    "_submit_order_action",
    "save_pending_order",
    "save_trade",
    "OrderResult",
    "Trade",
}


def _read_syntax_tree(source_path: Path) -> ast.Module:
    """
    함수 이름: _read_syntax_tree()
    기능: 지정한 Python source를 UTF-8로 읽어 AST module을 반환한다.
    인자: source_path -> 읽을 Python file 경로
    반환값: parse가 끝난 ast.Module
    작성 날짜: 2026/09/04
    """
    return ast.parse(source_path.read_text(encoding="utf-8"))  # 문자열 검색 대신 구조를 검사한다.


def _call_name(call_node: ast.Call) -> str | None:
    """
    함수 이름: _call_name()
    기능: Name 또는 Attribute call의 마지막 operation 이름을 반환한다.
    인자: call_node -> 이름을 읽을 AST Call
    반환값: operation 이름 또는 동적 call이면 None
    작성 날짜: 2026/09/04
    """
    if isinstance(call_node.func, ast.Name):
        return call_node.func.id
    if isinstance(call_node.func, ast.Attribute):
        return call_node.func.attr

    return None  # Subscription 같은 동적 callable은 order bypass 판정 대상이 아니다.


def _find_class(syntax_tree: ast.Module, class_name: str) -> ast.ClassDef:
    """
    함수 이름: _find_class()
    기능: module의 exact class 정의를 찾고 없으면 assertion을 발생시킨다.
    인자: syntax_tree -> 검색할 AST module
        class_name -> 찾을 exact class 이름
    반환값: 일치하는 ast.ClassDef
    작성 날짜: 2026/09/04
    """
    matching_classes = tuple(
        syntax_node
        for syntax_node in syntax_tree.body
        if isinstance(syntax_node, ast.ClassDef)
        and syntax_node.name == class_name
    )
    if len(matching_classes) != 1:
        raise AssertionError(f"expected exactly one class named {class_name}")

    return matching_classes[0]


def _find_method(class_node: ast.ClassDef, method_name: str) -> ast.FunctionDef:
    """
    함수 이름: _find_method()
    기능: class body의 exact 동기 method 정의를 찾고 없으면 assertion을 발생시킨다.
    인자: class_node -> 검색할 class AST
        method_name -> 찾을 exact method 이름
    반환값: 일치하는 ast.FunctionDef
    작성 날짜: 2026/09/04
    """
    matching_methods = tuple(
        syntax_node
        for syntax_node in class_node.body
        if isinstance(syntax_node, ast.FunctionDef)
        and syntax_node.name == method_name
    )
    if len(matching_methods) != 1:
        raise AssertionError(f"expected exactly one method named {method_name}")

    return matching_methods[0]


def _call_names(syntax_node: ast.AST) -> tuple[str, ...]:
    """
    함수 이름: _call_names()
    기능: AST subtree의 정적으로 식별 가능한 모든 call operation 이름을 반환한다.
    인자: syntax_node -> 순회할 AST subtree
    반환값: source 순서와 무관한 call 이름 tuple
    작성 날짜: 2026/09/04
    """
    return tuple(
        call_name
        for child_node in ast.walk(syntax_node)
        if isinstance(child_node, ast.Call)
        for call_name in (_call_name(child_node),)
        if call_name is not None
    )  # Forbidden call 존재 여부만 검사하므로 AST walk 순서를 업무 계약으로 쓰지 않는다.


class PhaseThirteenDeterministicPublicBoundaryTests(unittest.TestCase):
    """
    클래스 이름: PhaseThirteenDeterministicPublicBoundaryTests
    기능: test-only Kline fixture와 production strategy/order 소유 경계를 정적으로 검증한다.
    작성 날짜: 2026/09/04
    """

    def test_production_source_cannot_import_or_enable_fixture(
        self,
    ) -> None:
        """
        함수 이름: test_production_source_cannot_import_or_enable_fixture()
        기능: packaged backend가 tests module, fixture identifier 또는 enable switch를 참조하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/04
        """
        violations: list[str] = []

        # 모든 production source의 import와 identifier를 AST로 검사해 주석 문구에는 결합하지 않는다.
        for source_path in sorted(SOURCE_ROOT.rglob("*.py")):
            syntax_tree = _read_syntax_tree(source_path)
            relative_path = source_path.relative_to(SOURCE_ROOT).as_posix()
            for syntax_node in ast.walk(syntax_tree):
                if isinstance(syntax_node, ast.Import):
                    for import_alias in syntax_node.names:
                        if import_alias.name == "tests" or import_alias.name.startswith(
                            "tests."
                        ):
                            violations.append(
                                f"{relative_path}:{syntax_node.lineno}:tests-import"
                            )
                elif isinstance(syntax_node, ast.ImportFrom):
                    imported_module = syntax_node.module or ""
                    if imported_module == "tests" or imported_module.startswith(
                        "tests."
                    ):
                        violations.append(
                            f"{relative_path}:{syntax_node.lineno}:tests-import"
                        )
                elif isinstance(syntax_node, ast.Name):
                    if syntax_node.id in _FIXTURE_IDENTIFIERS:
                        violations.append(
                            f"{relative_path}:{syntax_node.lineno}:fixture-name"
                        )
                elif isinstance(syntax_node, ast.Constant) and isinstance(
                    syntax_node.value,
                    str,
                ):
                    normalized_value = syntax_node.value.upper()
                    if (
                        "DETERMINISTIC_PUBLIC_CASE2_FIXTURE"
                        in normalized_value
                        or "ENABLE_DETERMINISTIC_CASE2" in normalized_value
                    ):
                        violations.append(
                            f"{relative_path}:{syntax_node.lineno}:fixture-switch"
                        )

        self.assertEqual([], violations)

    def test_fixture_builds_only_market_klines_without_business_results(
        self,
    ) -> None:
        """
        함수 이름: test_fixture_builds_only_market_klines_without_business_results()
        기능: fixture가 trading, bootstrap, transport, environment 또는 order result를 조립하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/04
        """
        syntax_tree = _read_syntax_tree(FIXTURE_PATH)
        forbidden_import_parts = {
            "adapters",
            "bootstrap",
            "os",
            "requests",
            "socket",
            "subprocess",
            "trading",
            "transport",
            "urllib",
        }
        imported_parts: set[str] = set()
        for syntax_node in ast.walk(syntax_tree):
            if isinstance(syntax_node, ast.Import):
                for import_alias in syntax_node.names:
                    imported_parts.update(import_alias.name.split("."))
            elif isinstance(syntax_node, ast.ImportFrom):
                imported_parts.update((syntax_node.module or "").split("."))

        # Kline 외의 업무 aggregate 생성자와 private order mutation operation은 fixture 어디에도 없어야 한다.
        self.assertFalse(imported_parts & forbidden_import_parts)
        fixture_call_names = set(_call_names(syntax_tree))
        self.assertTrue(fixture_call_names.isdisjoint(_FORBIDDEN_ORDER_CALLS))
        self.assertIn("Kline", fixture_call_names)
        self.assertNotIn("getenv", fixture_call_names)

    def test_actual_one_shot_uses_fixture_only_through_public_market_input(
        self,
    ) -> None:
        """
        함수 이름: test_actual_one_shot_uses_fixture_only_through_public_market_input()
        기능: actual orchestration이 helper를 호출하고 injection method는 observe_kline 외 order seam을 쓰지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/04
        """
        syntax_tree = _read_syntax_tree(ACTUAL_ONE_SHOT_PATH)
        test_class = _find_class(
            syntax_tree,
            "BinanceTestnetPhaseThirteenPublicMarketCase2Tests",
        )
        execute_method = _find_method(
            test_class,
            "_execute_actual_public_market_case2",
        )
        injection_method = _find_method(
            test_class,
            "_inject_deterministic_public_case2_and_wait_for_buy",
        )
        execute_call_names = set(_call_names(execute_method))
        injection_call_names = set(_call_names(injection_method))

        # Current snapshot helper와 public observer가 모두 없으면 actual runner가 자연 signal 또는 private seam으로 회귀한 것이다.
        self.assertIn(
            "create_deterministic_public_case2_klines",
            execute_call_names,
        )
        self.assertIn(
            "_inject_deterministic_public_case2_and_wait_for_buy",
            execute_call_names,
        )
        self.assertIn("observe_kline", injection_call_names)
        self.assertTrue(
            execute_call_names.isdisjoint(_FORBIDDEN_ORDER_CALLS)
        )
        self.assertTrue(
            injection_call_names.isdisjoint(_FORBIDDEN_ORDER_CALLS)
        )

    def test_full_flow_does_not_construct_order_or_fill_results(
        self,
    ) -> None:
        """
        함수 이름: test_full_flow_does_not_construct_order_or_fill_results()
        기능: local E2E도 public Kline과 STOP만 호출하고 memory REST 응답 밖에서 성공을 합성하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/04
        """
        syntax_tree = _read_syntax_tree(FULL_FLOW_PATH)
        test_class = _find_class(
            syntax_tree,
            "DeterministicProductionPathCase2FlowTests",
        )
        test_method = _find_method(
            test_class,
            "test_public_fixture_runs_buy_stop_sell_and_zero_exposure",
        )
        rest_client_class = _find_class(
            syntax_tree,
            "DeterministicProductionPathRESTClient",
        )
        test_call_names = set(_call_names(test_method))
        rest_method_names = {
            syntax_node.name
            for syntax_node in rest_client_class.body
            if isinstance(syntax_node, ast.FunctionDef)
        }
        rest_base_names = {
            base_node.id
            for base_node in rest_client_class.bases
            if isinstance(base_node, ast.Name)
        }

        # External response는 기존 memory REST boundary가 소유하고 새 full-flow test는 result를 직접 만들지 않는다.
        self.assertIn("PublicCase2RESTClient", rest_base_names)
        self.assertNotIn("submit_order", rest_method_names)
        self.assertNotIn("query_order_result", rest_method_names)
        self.assertIn(
            "create_deterministic_public_case2_klines",
            test_call_names,
        )
        self.assertIn("observe_kline", test_call_names)
        self.assertIn("stop_trading", test_call_names)
        self.assertTrue(test_call_names.isdisjoint(_FORBIDDEN_ORDER_CALLS))

    def test_new_fixture_and_full_flow_follow_docstring_convention(
        self,
    ) -> None:
        """
        함수 이름: test_new_fixture_and_full_flow_follow_docstring_convention()
        기능: Session 2의 모든 새 class와 함수가 필수 한국어 docstring 항목과 공백 들여쓰기를 지키는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/04
        """
        for source_path in (FIXTURE_PATH, FULL_FLOW_PATH):
            source_text = source_path.read_text(encoding="utf-8")
            self.assertNotIn("\t", source_text)
            syntax_tree = ast.parse(source_text)
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
                        self.assertIn("기능:", docstring)
                        self.assertIn("작성 날짜:", docstring)
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
                        self.assertIn("기능:", docstring)
                        self.assertIn("인자:", docstring)
                        self.assertIn("반환값:", docstring)
                        self.assertIn("작성 날짜:", docstring)


if __name__ == "__main__":
    unittest.main()
