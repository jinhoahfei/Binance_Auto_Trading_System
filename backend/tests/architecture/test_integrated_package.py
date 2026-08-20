"""통합 backend package의 공용 계약과 의존 경계를 검증한다."""

import ast
import unittest
from pathlib import Path

from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.regime import (
    RegimeSTM,
    RegimeType as RegimePublicType,
)
from binance_auto_trader.domain.regime.transitions import (
    TRANSITION_IDS as REGIME_TRANSITION_IDS,
)
from binance_auto_trader.domain.trading import (
    RegimeType as TradingPublicType,
    TradingSTM,
)
from binance_auto_trader.domain.trading.transitions.catalog import (
    TRANSITION_IDS as TRADING_TRANSITION_IDS,
)


BACKEND_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = BACKEND_ROOT / "src"
DOMAIN_ROOT = SOURCE_ROOT / "binance_auto_trader" / "domain"


class IntegratedPackageArchitectureTests(unittest.TestCase):
    """
    클래스 이름: IntegratedPackageArchitectureTests
    기능: 두 STM의 통합 package와 canonical REGIME 계약을 검증한다.
    작성 날짜: 2026/08/20
    """

    def test_canonical_regime_type_has_only_domain_members(self) -> None:
        """
        함수 이름: test_canonical_regime_type_has_only_domain_members()
        기능: canonical REGIME이 다섯 domain 값만 보유하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        expected_names = tuple(f"TYPE_{index}" for index in range(5))

        self.assertEqual(tuple(RegimeType.__members__), expected_names)
        self.assertEqual(
            tuple(regime_type.value for regime_type in RegimeType),
            expected_names,
        )
        self.assertFalse(hasattr(RegimeType, "LOWER_BB"))

    def test_both_public_surfaces_share_canonical_regime_type(self) -> None:
        """
        함수 이름: test_both_public_surfaces_share_canonical_regime_type()
        기능: RegimeSTM과 TradingSTM 공개 API가 같은 enum class를 노출하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self.assertIs(RegimePublicType, RegimeType)
        self.assertIs(TradingPublicType, RegimeType)
        self.assertTrue(callable(RegimeSTM))
        self.assertTrue(callable(TradingSTM))

    def test_type_zero_selects_existing_trading_registry(self) -> None:
        """
        함수 이름: test_type_zero_selects_existing_trading_registry()
        기능: TYPE_0이 기존 109개 거래 전이 STM 인스턴스를 선택하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        state_machine = TradingSTM.get_stm_instance(RegimeType.TYPE_0)

        self.assertIs(state_machine.regime_type, RegimeType.TYPE_0)

    def test_unsupported_regime_types_do_not_fallback(self) -> None:
        """
        함수 이름: test_unsupported_regime_types_do_not_fallback()
        기능: 미지원 REGIME이 TYPE_0 거래 registry로 대체되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        for regime_type in tuple(RegimeType)[1:]:
            with self.subTest(regime_type=regime_type):
                with self.assertRaises(ValueError):
                    TradingSTM.get_stm_instance(regime_type)
                with self.assertRaises(ValueError):
                    TradingSTM(regime_type)

        for invalid_regime_type in ("TYPE_0", "type0", 0, None):
            with self.subTest(invalid_regime_type=invalid_regime_type):
                with self.assertRaises(TypeError):
                    TradingSTM(invalid_regime_type)

    def test_domain_does_not_define_ui_wire_regime_values(self) -> None:
        """
        함수 이름: test_domain_does_not_define_ui_wire_regime_values()
        기능: UI wire REGIME 문자열과 변환이 domain에 들어오지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        forbidden_wire_values = {f"type{index}" for index in range(5)}

        for source_path in DOMAIN_ROOT.rglob("*.py"):
            syntax_tree = ast.parse(source_path.read_text(encoding="utf-8"))
            string_values = {
                syntax_node.value
                for syntax_node in ast.walk(syntax_tree)
                if isinstance(syntax_node, ast.Constant)
                and isinstance(syntax_node.value, str)
            }

            with self.subTest(source_path=source_path):
                self.assertFalse(string_values & forbidden_wire_values)

    def test_domain_does_not_import_outward_or_io_layers(self) -> None:
        """
        함수 이름: test_domain_does_not_import_outward_or_io_layers()
        기능: domain이 application, adapter, transport 또는 외부 I/O module을 import하지 않는지 검증한다.
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
            "urllib",
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

    def test_only_common_module_defines_regime_type(self) -> None:
        """
        함수 이름: test_only_common_module_defines_regime_type()
        기능: production source의 RegimeType class 정의가 common module 하나인지 검증한다.
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
                    and syntax_node.name == "RegimeType"
                ):
                    definition_paths.append(source_path.relative_to(SOURCE_ROOT))

        self.assertEqual(
            definition_paths,
            [Path("binance_auto_trader/domain/common/enums.py")],
        )

    def test_transition_id_contracts_are_preserved(self) -> None:
        """
        함수 이름: test_transition_id_contracts_are_preserved()
        기능: 통합 후 Regime과 Trading 전이 ID가 각각 13개와 109개인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self.assertEqual(len(REGIME_TRANSITION_IDS), 13)
        self.assertEqual(len(TRADING_TRANSITION_IDS), 109)

    def test_repository_has_no_legacy_python_source_trees(self) -> None:
        """
        함수 이름: test_repository_has_no_legacy_python_source_trees()
        기능: root에 RegimeSTM과 TradingSTM 중복 source tree가 남지 않았는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        repository_root = BACKEND_ROOT.parent

        self.assertFalse((repository_root / "RegimeSTM").exists())
        self.assertFalse((repository_root / "TradingSTM").exists())


if __name__ == "__main__":
    unittest.main()
