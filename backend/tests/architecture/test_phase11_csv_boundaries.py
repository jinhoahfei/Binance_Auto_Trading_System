"""Phase 11 CSV export의 계층, schema와 streaming 경계를 검증한다."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest

from binance_auto_trader.adapters.filesystem.csv_file_gateway import CSV_HEADER
from binance_auto_trader.domain.history import (
    CSVExportOptions,
    CSVExportResult,
    CSVPeriod,
)


BACKEND_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = BACKEND_ROOT / "src" / "binance_auto_trader"
CSV_DOMAIN_PATH = SOURCE_ROOT / "domain" / "history" / "csv_export_options.py"
CSV_GATEWAY_PATH = SOURCE_ROOT / "adapters" / "filesystem" / "csv_file_gateway.py"
HISTORY_CONTROLLER_PATH = SOURCE_ROOT / "application" / "trade_history_controller.py"
HISTORY_REPOSITORY_PATH = (
    SOURCE_ROOT / "adapters" / "persistence" / "trade_history_repository.py"
)


def _imported_module_parts(source_path: Path) -> set[str]:
    """
    함수 이름: _imported_module_parts()
    기능: Python source가 import하는 module 경로의 모든 부분을 수집한다.
    인자: source_path -> 분석할 production Python 파일
    반환값: import 경로를 점으로 나눈 문자열 집합
    작성 날짜: 2026/08/23
    """
    syntax_tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_parts: set[str] = set()

    # import와 from import 두 AST 형태를 같은 module-part 집합으로 정규화한다.
    for syntax_node in ast.walk(syntax_tree):
        if isinstance(syntax_node, ast.Import):
            for imported_name in syntax_node.names:
                imported_parts.update(imported_name.name.split("."))
        elif (
            isinstance(syntax_node, ast.ImportFrom)
            and syntax_node.module is not None
        ):
            imported_parts.update(syntax_node.module.split("."))

    return imported_parts  # Alias 이름이 아니라 실제 dependency 방향만 비교한다.


def _method_syntax(source_path: Path, method_name: str) -> ast.FunctionDef:
    """
    함수 이름: _method_syntax()
    기능: production source에서 이름이 유일한 method AST를 찾아 반환한다.
    인자: source_path -> 분석할 Python source
        method_name -> 찾을 함수 또는 method 이름
    반환값: 유일한 ast.FunctionDef
    작성 날짜: 2026/08/23
    """
    syntax_tree = ast.parse(source_path.read_text(encoding="utf-8"))
    matching_methods = tuple(
        syntax_node
        for syntax_node in ast.walk(syntax_tree)
        if isinstance(syntax_node, ast.FunctionDef)
        and syntax_node.name == method_name
    )

    # 중복 이름이면 다른 class의 구현을 잘못 검사할 수 있으므로 architecture test를 닫는다.
    if len(matching_methods) != 1:
        raise AssertionError(
            f"{method_name} must identify exactly one method in {source_path}"
        )
    return matching_methods[0]  # Caller는 method body만 대상으로 materialization을 검사한다.


class PhaseElevenCSVBoundaryTests(unittest.TestCase):
    """
    클래스 이름: PhaseElevenCSVBoundaryTests
    기능: CSV domain 공개면, 계층 dependency와 iterator 기반 구현을 고정한다.
    작성 날짜: 2026/08/23
    """

    def test_csv_public_types_are_single_domain_classes(self) -> None:
        """
        함수 이름: test_csv_public_types_are_single_domain_classes()
        기능: option, period와 result 공개 계약이 domain의 canonical class인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        public_types = (CSVExportOptions, CSVExportResult, CSVPeriod)

        # Transport와 adapter가 복제 DTO class를 만들지 않고 같은 domain 공개면을 사용한다.
        for public_type in public_types:
            with self.subTest(public_type=public_type.__name__):
                self.assertTrue(
                    public_type.__module__.startswith(
                        "binance_auto_trader.domain.history"
                    )
                )  # Class identity의 owner가 history domain 아래에만 존재해야 한다.

    def test_csv_header_is_the_exact_adr_004_schema(self) -> None:
        """
        함수 이름: test_csv_header_is_the_exact_adr_004_schema()
        기능: CSV schema version 1의 21개 column 이름과 순서를 고정한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        expected_header = (
            "schema_version",
            "trade_id",
            "order_id",
            "client_order_id",
            "executed_at_utc",
            "executed_at_kst",
            "symbol",
            "side",
            "regime_type",
            "strategy",
            "average_fill_price",
            "market_price_at_decision",
            "executed_quantity",
            "executed_amount",
            "fee_amount",
            "fee_asset",
            "fee_quote_amount",
            "allocated_cost_basis",
            "realized_pnl",
            "realized_return_rate",
            "exit_reason",
        )

        self.assertEqual(CSV_HEADER, expected_header)  # 순서 변경도 schema drift로 처리한다.

    def test_domain_and_controller_keep_filesystem_dependencies_out(self) -> None:
        """
        함수 이름: test_domain_and_controller_keep_filesystem_dependencies_out()
        기능: path와 CSV I/O가 domain/application이 아니라 filesystem adapter에만 있는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        forbidden_domain_parts = {
            "adapters",
            "application",
            "csv",
            "os",
            "pathlib",
            "transport",
        }
        forbidden_controller_parts = {
            "adapters",
            "csv",
            "os",
            "pathlib",
            "transport",
        }

        # Pure option은 filesystem을 모르고 Controller는 concrete gateway를 import하지 않는다.
        self.assertFalse(
            _imported_module_parts(CSV_DOMAIN_PATH) & forbidden_domain_parts
        )
        self.assertFalse(
            _imported_module_parts(HISTORY_CONTROLLER_PATH)
            & forbidden_controller_parts
        )
        self.assertIn(
            "csv",
            _imported_module_parts(CSV_GATEWAY_PATH),
        )  # 표준 CSV serializer 사용 책임은 adapter에만 둔다.

    def test_export_path_does_not_materialize_the_history_collection(self) -> None:
        """
        함수 이름: test_export_path_does_not_materialize_the_history_collection()
        기능: Controller와 repository stream entry가 tuple/list 조회 API로 전체 history를 복제하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        export_method = _method_syntax(HISTORY_CONTROLLER_PATH, "export_csv")
        stream_method = _method_syntax(HISTORY_REPOSITORY_PATH, "stream_trades")

        # 두 owner method에서 eager collection constructor와 기존 tuple API 호출을 금지한다.
        for source_path, method_syntax in (
            (HISTORY_CONTROLLER_PATH, export_method),
            (HISTORY_REPOSITORY_PATH, stream_method),
        ):
            called_names = {
                syntax_node.func.id
                for syntax_node in ast.walk(method_syntax)
                if isinstance(syntax_node, ast.Call)
                and isinstance(syntax_node.func, ast.Name)
            }
            called_attributes = {
                syntax_node.func.attr
                for syntax_node in ast.walk(method_syntax)
                if isinstance(syntax_node, ast.Call)
                and isinstance(syntax_node.func, ast.Attribute)
            }
            with self.subTest(source_path=source_path):
                self.assertFalse(called_names & {"list", "sorted", "tuple"})
                self.assertNotIn("get_trade_history", called_attributes)


if __name__ == "__main__":
    unittest.main()
