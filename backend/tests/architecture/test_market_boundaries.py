"""Phase 2 시장 데이터 구현의 canonical 타입, 책임 경계와 코딩 규약을 검증한다."""

from __future__ import annotations

import ast
from pathlib import Path
import re
import unittest
from typing import get_args, get_type_hints

from binance_auto_trader.adapters.binance.api_gateway import APIGateway
from binance_auto_trader.adapters.binance.websocket_gateway import (
    WebSocketGateway,
)
from binance_auto_trader.application import market_data_controller
from binance_auto_trader.domain.common import Interval, SUPPORTED_INTERVALS
from binance_auto_trader.domain.market import (
    Interval as MarketInterval,
    Kline,
)
from binance_auto_trader.domain.regime import Interval as RegimeInterval


BACKEND_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = BACKEND_ROOT / "src" / "binance_auto_trader"
DOMAIN_ROOT = SOURCE_ROOT / "domain"
MARKET_SOURCE_PATHS = (
    *tuple((DOMAIN_ROOT / "market").rglob("*.py")),
    DOMAIN_ROOT / "common" / "enums.py",
    *tuple((SOURCE_ROOT / "adapters" / "binance").rglob("*.py")),
    SOURCE_ROOT / "application" / "market_data_controller.py",
)
CLASS_NAME_PATTERN = re.compile(r"^_?[A-Z][A-Za-z0-9]*$")
FUNCTION_NAME_PATTERN = re.compile(
    r"^(?:__[a-z0-9_]+__|_*[a-z][a-z0-9_]*)$"
)
VARIABLE_NAME_PATTERN = re.compile(
    r"^(?:_*[a-z][a-z0-9_]*|_*[A-Z][A-Z0-9_]*)$"
)


class MarketBoundaryArchitectureTests(unittest.TestCase):
    """
    클래스 이름: MarketBoundaryArchitectureTests
    기능: Phase 2 시장 계층의 타입 identity와 의존 방향을 정적으로 검증한다.
    작성 날짜: 2026/08/20
    """

    def test_all_market_layers_share_one_canonical_interval(self) -> None:
        """
        함수 이름: test_all_market_layers_share_one_canonical_interval()
        기능: 도메인, Gateway와 Controller가 같은 canonical Interval만 사용하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        expected_intervals = (
            Interval.ONE_MINUTE,
            Interval.THIRTY_MINUTES,
            Interval.FOUR_HOURS,
            Interval.ONE_DAY,
        )
        kline_annotations = get_type_hints(Kline)
        api_annotations = get_type_hints(APIGateway.load_all_klines)
        web_socket_annotations = get_type_hints(
            WebSocketGateway.start_all_kline_buffering
        )
        api_mapping_type = api_annotations["return"]
        api_interval_type = get_args(api_mapping_type)[0]
        web_socket_interval_collection = web_socket_annotations["intervals"]
        web_socket_interval_type = get_args(
            web_socket_interval_collection
        )[0]

        self.assertIs(MarketInterval, Interval)
        self.assertIs(RegimeInterval, Interval)
        self.assertIs(kline_annotations["interval"], Interval)
        self.assertIs(api_interval_type, Interval)
        self.assertIs(web_socket_interval_type, Interval)
        self.assertEqual(SUPPORTED_INTERVALS, expected_intervals)
        self.assertIs(
            market_data_controller.MARKET_INTERVALS,
            SUPPORTED_INTERVALS,
        )

    def test_only_common_enums_defines_interval(self) -> None:
        """
        함수 이름: test_only_common_enums_defines_interval()
        기능: production source에 Interval class가 중복 정의되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        definition_paths = []

        for source_path in SOURCE_ROOT.rglob("*.py"):
            syntax_tree = ast.parse(source_path.read_text(encoding="utf-8"))
            for syntax_node in ast.walk(syntax_tree):
                if (
                    isinstance(syntax_node, ast.ClassDef)
                    and syntax_node.name == "Interval"
                ):
                    definition_paths.append(
                        source_path.relative_to(SOURCE_ROOT)
                    )

        self.assertEqual(
            definition_paths,
            [Path("domain/common/enums.py")],
        )

    def test_market_domain_does_not_import_outward_or_io_layers(self) -> None:
        """
        함수 이름: test_market_domain_does_not_import_outward_or_io_layers()
        기능: domain이 application, adapter, transport 또는 외부 I/O 계층을 참조하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        forbidden_parts = {
            "adapters",
            "aiohttp",
            "application",
            "http",
            "httpx",
            "pathlib",
            "requests",
            "socket",
            "transport",
            "ui",
            "urllib",
            "websocket",
            "websockets",
        }

        for source_path in DOMAIN_ROOT.rglob("*.py"):
            syntax_tree = ast.parse(source_path.read_text(encoding="utf-8"))
            imported_parts = set()

            for syntax_node in ast.walk(syntax_tree):
                if isinstance(syntax_node, ast.Import):
                    for alias in syntax_node.names:
                        imported_parts.update(alias.name.split("."))
                elif (
                    isinstance(syntax_node, ast.ImportFrom)
                    and syntax_node.module is not None
                ):
                    imported_parts.update(syntax_node.module.split("."))

            with self.subTest(source_path=source_path):
                self.assertFalse(imported_parts & forbidden_parts)

    def test_controller_does_not_import_future_phase_collaborators(self) -> None:
        """
        함수 이름: test_controller_does_not_import_future_phase_collaborators()
        기능: MarketDataController가 Regime, Trading, UI 또는 주문 책임을 참조하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        controller_path = (
            SOURCE_ROOT / "application" / "market_data_controller.py"
        )
        syntax_tree = ast.parse(controller_path.read_text(encoding="utf-8"))
        forbidden_parts = {"order", "regime", "trading", "ui"}
        imported_parts = set()

        for syntax_node in ast.walk(syntax_tree):
            if isinstance(syntax_node, ast.Import):
                for alias in syntax_node.names:
                    imported_parts.update(alias.name.lower().split("."))
            elif (
                isinstance(syntax_node, ast.ImportFrom)
                and syntax_node.module is not None
            ):
                imported_parts.update(
                    syntax_node.module.lower().split(".")
                )

        self.assertFalse(imported_parts & forbidden_parts)

    def test_financial_market_source_does_not_use_float(self) -> None:
        """
        함수 이름: test_financial_market_source_does_not_use_float()
        기능: 시장 domain, Binance adapter와 Controller가 금융 수치에 float를 사용하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        for source_path in MARKET_SOURCE_PATHS:
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


class MarketCodingConventionTests(unittest.TestCase):
    """
    클래스 이름: MarketCodingConventionTests
    기능: Phase 2 production Python source의 이름, 들여쓰기와 docstring을 검증한다.
    작성 날짜: 2026/08/20
    """

    def test_market_source_does_not_use_tabs(self) -> None:
        """
        함수 이름: test_market_source_does_not_use_tabs()
        기능: Phase 2 production source가 탭 없이 공백 들여쓰기를 사용하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        for source_path in MARKET_SOURCE_PATHS:
            with self.subTest(source_path=source_path):
                self.assertNotIn(
                    "\t",
                    source_path.read_text(encoding="utf-8"),
                )

    def test_market_definitions_follow_required_name_styles(self) -> None:
        """
        함수 이름: test_market_definitions_follow_required_name_styles()
        기능: Phase 2 클래스, 함수, 인자와 변수 이름이 표기 규약을 따르는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        for source_path in MARKET_SOURCE_PATHS:
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

    def test_market_definitions_have_korean_dated_docstrings(self) -> None:
        """
        함수 이름: test_market_definitions_have_korean_dated_docstrings()
        기능: Phase 2의 모든 class와 함수가 한국어 필수 항목과 기준 작성일을 갖는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        class_sections = (
            "클래스 이름:",
            "기능:",
            "작성 날짜: 2026/08/20",
        )
        function_sections = (
            "함수 이름:",
            "기능:",
            "인자:",
            "반환값:",
            "작성 날짜: 2026/08/20",
        )

        for source_path in MARKET_SOURCE_PATHS:
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


if __name__ == "__main__":
    unittest.main()
