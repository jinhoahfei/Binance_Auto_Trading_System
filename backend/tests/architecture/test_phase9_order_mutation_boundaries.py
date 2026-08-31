"""Phase 9 Binance 주문 mutation이 Controller 소유 경계를 벗어나지 않게 고정한다."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


PACKAGE_ROOT = (
    Path(__file__).resolve().parents[2] / "src" / "binance_auto_trader"
)


class PhaseNineOrderMutationBoundaryTests(unittest.TestCase):
    """
    클래스 이름: PhaseNineOrderMutationBoundaryTests
    기능: route와 다른 application 코드가 Binance 주문 Gateway를 직접 호출하지 못하게 검증한다.
    작성 날짜: 2026/08/24
    """

    def test_order_mutation_calls_remain_in_controller_owned_files(
        self,
    ) -> None:
        """
        함수 이름: test_order_mutation_calls_remain_in_controller_owned_files()
        기능: prepare, submit, cancel과 force-sell 호출이 정확한 production owner method에만 있는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        allowed_call_sites = {
            (
                "adapters/binance/api_gateway.py",
                "APIGateway",
                "prepare_order",
                "prepare_order",
            ),
            (
                "adapters/binance/api_gateway.py",
                "APIGateway",
                "submit_order",
                "submit_order",
            ),
            (
                "adapters/binance/api_gateway.py",
                "APIGateway",
                "cancel_order",
                "cancel_order",
            ),
            (
                "adapters/binance/api_gateway.py",
                "APIGateway",
                "sell_all_position",
                "submit_order",
            ),
            (
                "application/trading_controller.py",
                "TradingController",
                "_submit_order_action",
                "prepare_order",
            ),
            (
                "application/trading_controller.py",
                "TradingController",
                "_submit_order_action",
                "sell_all_position",
            ),
            (
                "application/trading_controller.py",
                "TradingController",
                "_submit_order_action",
                "submit_order",
            ),
            (
                "application/trading_controller.py",
                "TradingController",
                "_cancel_pending_order_action",
                "cancel_order",
            ),
            (
                "application/trading_controller.py",
                "TradingController",
                "_cancel_pending_order_during_manual_kill_startup",
                "cancel_order",
            ),
            (
                "bootstrap/testnet.py",
                "_TestnetOrderPermissionRESTClient",
                "prepare_order",
                "prepare_order",
            ),
            (
                "bootstrap/testnet.py",
                "_TestnetOrderPermissionRESTClient",
                "submit_order",
                "submit_order",
            ),
            (
                "bootstrap/testnet.py",
                "_TestnetOrderPermissionRESTClient",
                "cancel_order",
                "cancel_order",
            ),
        }
        mutation_operation_names = {
            "cancel_order",
            "prepare_order",
            "sell_all_position",
            "submit_order",
        }
        unauthorized_calls: list[str] = []

        # 모든 production Python call target과 lexical owner를 AST로 읽어 문자열에는 결합하지 않는다.
        for source_path in sorted(PACKAGE_ROOT.rglob("*.py")):
            relative_path = source_path.relative_to(PACKAGE_ROOT).as_posix()
            syntax_tree = ast.parse(source_path.read_text(encoding="utf-8"))
            parent_by_node = {
                child_node: parent_node
                for parent_node in ast.walk(syntax_tree)
                for child_node in ast.iter_child_nodes(parent_node)
            }
            for syntax_node in ast.walk(syntax_tree):
                if not isinstance(syntax_node, ast.Call):
                    continue
                call_target = syntax_node.func
                if isinstance(call_target, ast.Attribute):
                    operation_name = call_target.attr
                elif isinstance(call_target, ast.Name):
                    operation_name = call_target.id
                else:
                    continue
                if operation_name not in mutation_operation_names:
                    continue

                # 같은 파일이라도 허용된 class/method 밖의 새 mutation call은 우회로 간주한다.
                owner_class = "<module>"
                owner_function = "<module>"
                ancestor_node = parent_by_node.get(syntax_node)
                while ancestor_node is not None:
                    if (
                        owner_function == "<module>"
                        and isinstance(
                            ancestor_node,
                            (ast.FunctionDef, ast.AsyncFunctionDef),
                        )
                    ):
                        owner_function = ancestor_node.name
                    if (
                        owner_class == "<module>"
                        and isinstance(ancestor_node, ast.ClassDef)
                    ):
                        owner_class = ancestor_node.name
                    ancestor_node = parent_by_node.get(ancestor_node)
                call_site = (
                    relative_path,
                    owner_class,
                    owner_function,
                    operation_name,
                )
                if call_site not in allowed_call_sites:
                    unauthorized_calls.append(
                        f"{relative_path}:{syntax_node.lineno}:"
                        f"{owner_class}.{owner_function}:{operation_name}"
                    )

        self.assertEqual(
            unauthorized_calls,
            [],
        )  # 새 route나 composition이 Gateway mutation을 직접 호출하면 정확한 source line을 남긴다.


if __name__ == "__main__":
    unittest.main()
