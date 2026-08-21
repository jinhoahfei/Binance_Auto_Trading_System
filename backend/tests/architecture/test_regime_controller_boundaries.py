"""Phase 3 RegimeController의 의존 방향, 상태 소유와 public 계약을 검증한다."""

import ast
from pathlib import Path
import unittest
from typing import get_type_hints

from binance_auto_trader.application.market_data_controller import (
    MarketDataController,
)
from binance_auto_trader.application.regime_controller import RegimeController
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.market import IndicatorSnapshot
from binance_auto_trader.domain.regime import RegimeResult


BACKEND_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = BACKEND_ROOT / "src" / "binance_auto_trader"
REGIME_CONTROLLER_PATH = SOURCE_ROOT / "application" / "regime_controller.py"
INDICATOR_SNAPSHOT_PATH = (
    SOURCE_ROOT / "domain" / "market" / "indicator_snapshot.py"
)


class RegimeControllerBoundaryTests(unittest.TestCase):
    """
    클래스 이름: RegimeControllerBoundaryTests
    기능: 추천 vertical slice가 Phase 3 책임과 의존 방향만 갖는지 검증한다.
    작성 날짜: 2026/08/20
    """

    def test_market_controller_requires_regime_controller_dependency(
        self,
    ) -> None:
        """
        함수 이름: test_market_controller_requires_regime_controller_dependency()
        기능: MarketDataController 조립 계약에 RegimeController가 명시됐는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        constructor_hints = get_type_hints(MarketDataController.__init__)

        self.assertIs(
            constructor_hints["regime_controller"],
            RegimeController,
        )

    def test_phase_three_public_operations_use_canonical_result_types(
        self,
    ) -> None:
        """
        함수 이름: test_phase_three_public_operations_use_canonical_result_types()
        기능: 지표 계산과 추천 façade가 Communication의 canonical 반환형을 사용하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        calculate_hints = get_type_hints(
            RegimeController.calculate_4h_indicators
        )
        recommend_hints = get_type_hints(RegimeController.recommend_regime)

        self.assertIs(calculate_hints["return"], IndicatorSnapshot)
        self.assertIs(recommend_hints["return"], RegimeType)
        self.assertIn(
            "source_market_version",
            get_type_hints(RegimeResult),
        )

    def test_regime_controller_does_not_import_future_or_io_layers(
        self,
    ) -> None:
        """
        함수 이름: test_regime_controller_does_not_import_future_or_io_layers()
        기능: 추천 Controller가 Trading, UI, adapter, transport, network와 file 책임을 참조하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        syntax_tree = ast.parse(
            REGIME_CONTROLLER_PATH.read_text(encoding="utf-8")
        )
        forbidden_parts = {
            "adapters",
            "asyncio",
            "http",
            "pathlib",
            "requests",
            "socket",
            "trading",
            "transport",
            "ui",
            "urllib",
        }
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

    def test_indicator_snapshot_does_not_import_application_layer(self) -> None:
        """
        함수 이름: test_indicator_snapshot_does_not_import_application_layer()
        기능: IndicatorSnapshot domain entity가 Controller 계층을 역참조하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        source_text = INDICATOR_SNAPSHOT_PATH.read_text(encoding="utf-8")
        syntax_tree = ast.parse(source_text)
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

        self.assertNotIn("application", imported_parts)

    def test_controller_owned_values_have_separate_write_paths(self) -> None:
        """
        함수 이름: test_controller_owned_values_have_separate_write_paths()
        기능: 추천값은 Action handler만, 선택값은 명시적 Phase 7 선택 경로만 쓰는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        syntax_tree = ast.parse(
            REGIME_CONTROLLER_PATH.read_text(encoding="utf-8")
        )
        parent_by_node = {}
        for parent_node in ast.walk(syntax_tree):
            for child_node in ast.iter_child_nodes(parent_node):
                parent_by_node[child_node] = parent_node

        assignment_methods: dict[str, set[str]] = {
            "_recommended_regime": set(),
            "_selected_regime": set(),
        }
        for syntax_node in ast.walk(syntax_tree):
            if not isinstance(syntax_node, ast.Attribute):
                continue
            if not isinstance(syntax_node.ctx, ast.Store):
                continue
            if syntax_node.attr not in assignment_methods:
                continue

            parent_node = parent_by_node.get(syntax_node)
            while parent_node is not None and not isinstance(
                parent_node,
                ast.FunctionDef,
            ):
                parent_node = parent_by_node.get(parent_node)
            if isinstance(parent_node, ast.FunctionDef):
                assignment_methods[syntax_node.attr].add(parent_node.name)

        self.assertEqual(
            assignment_methods["_recommended_regime"],
            {"__init__", "_apply_recommended_regime"},
        )
        self.assertEqual(
            assignment_methods["_selected_regime"],
            {"__init__", "set_regime_type"},
        )


if __name__ == "__main__":
    unittest.main()
