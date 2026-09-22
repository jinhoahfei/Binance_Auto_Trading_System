"""Live read-only runner의 credential 격리·출력 안전성과 GET 전용 보고를 검증한다."""

from contextlib import redirect_stdout
from decimal import Decimal
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

# Root scripts와 backend unittest 양쪽에서 동일 production runner를 import한다.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend" / "src"))
from scripts import run_live_read_only_from_keychain as runner
from binance_auto_trader.bootstrap.live_configuration import LiveConfiguration


class LiveKeychainRunnerTests(unittest.TestCase):
    """
    클래스 이름: LiveKeychainRunnerTests
    기능: secret-safe reader와 mutation 없는 preflight command를 검사한다.
    작성 날짜: 2026/09/08
    """

    def test_keychain_reader_uses_only_live_namespace_and_scrubs_capture(self) -> None:
        """
        함수 이름: test_keychain_reader_uses_only_live_namespace_and_scrubs_capture()
        기능: live 고정 service 조회·canary buffer 반환·captured output 제거를 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        captured = subprocess.CompletedProcess([], 0, b"live-secret-canary\n", b"")
        command_runner = Mock(return_value=captured)
        secret = runner.read_keychain_credential("api-secret", command_runner=command_runner)
        self.assertEqual(secret, b"live-secret-canary")
        self.assertIn("com.binance-auto.trader.live", command_runner.call_args.args[0])
        self.assertNotIn("live-secret-canary", repr(command_runner.call_args))
        self.assertFalse(captured.stdout)
        runner.zeroize_secret_buffer(secret)  # Test canary도 운영 reader와 같은 정리 경로를 따른다.
        self.assertFalse(any(secret))

    def test_keychain_errors_never_reflect_captured_secret(self) -> None:
        """
        함수 이름: test_keychain_errors_never_reflect_captured_secret()
        기능: security 실패·timeout 출력의 credential이 예외와 로그에 남지 않게 한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        for result in (subprocess.CompletedProcess([], 44, b"secret-canary", b"secret-canary"), subprocess.CompletedProcess([], 0, b"invalid\nsecret", b"")):
            with self.assertRaises(runner.LiveKeychainRunnerError) as caught:
                runner.read_keychain_credential("api-key", command_runner=Mock(return_value=result))
            self.assertNotIn("secret", str(caught.exception))
            self.assertFalse(result.stdout)
        with self.assertRaises(runner.LiveKeychainRunnerError):
            runner.read_keychain_credential("testnet-api-key", command_runner=Mock())

    def test_preflight_report_separates_read_success_from_pilot_fee_blocker(self) -> None:
        """
        함수 이름: test_preflight_report_separates_read_success_from_pilot_fee_blocker()
        기능: signed 조회 성공이 base-fee dust·open order의 pilot 승인을 의미하지 않게 한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        rest = Mock()
        gateway = Mock()
        gateway.fetch_account_snapshot.return_value.balances = ()
        gateway.fetch_symbol_trading_rules.return_value.symbol = "ETHUSDT"
        gateway.fetch_symbol_trading_rules.return_value.notional_filters = ()
        gateway.has_any_exchange_open_orders.return_value = False
        gateway.has_any_exchange_open_order_lists.return_value = False
        commission = gateway.fetch_commission_discount_policy.return_value
        commission.can_charge_discount_asset = False
        commission.market_buy_received_asset_commission_rate = Decimal("0.001")
        commission.market_sell_received_asset_commission_rate = Decimal("0.001")
        configuration = LiveConfiguration("key", "secret", enabled=True, confirmation="LIVE")
        with patch.object(runner, "BinanceLiveRESTClient", return_value=rest), patch.object(runner, "APIGateway", return_value=gateway):
            report = runner.run_preflight(configuration)
        self.assertEqual(report["read_only_preflight"], "PASS")
        self.assertEqual(report["pilot_prerequisites"], "NO_GO")
        self.assertEqual(report["order_mutations"], 0)
        self.assertIn("quote_balance_meets_market_minimum", report["blockers"])
        rest.submit_order.assert_not_called()
        rest.cancel_order.assert_not_called()  # Read-only 검증은 주문 permission을 생성하지 않는다.

    def test_market_minimum_balance_boundary_uses_only_applicable_filters(self) -> None:
        """
        함수 이름: test_market_minimum_balance_boundary_uses_only_applicable_filters()
        기능: MARKET 최소 금액 경계와 비적용 filter 제외를 잔액 원문 출력 없이 검사한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        gateway = Mock()
        rules = gateway.fetch_symbol_trading_rules.return_value
        rules.symbol = "ETHUSDT"
        rules.notional_filters = (
            SimpleNamespace(minimum_notional=Decimal("5"), apply_minimum_to_market=True),
            SimpleNamespace(minimum_notional=Decimal("100"), apply_minimum_to_market=False),
        )
        gateway.has_any_exchange_open_orders.return_value = False
        gateway.has_any_exchange_open_order_lists.return_value = False
        commission = gateway.fetch_commission_discount_policy.return_value
        commission.can_charge_discount_asset = False
        commission.market_buy_received_asset_commission_rate = Decimal("0.001")
        commission.market_sell_received_asset_commission_rate = Decimal("0.001")
        configuration = LiveConfiguration("key", "secret", enabled=True, confirmation="LIVE")

        # 정확한 하한만 통과시키고 상한 주문 가능성이나 수수료 여유까지 증명했다고 주장하지 않는다.
        for available, expected in (("0", False), ("4.99", False), ("5", True)):
            with self.subTest(available=available):
                gateway.fetch_account_snapshot.return_value.balances = (
                    SimpleNamespace(asset="USDT", free=Decimal(available)),
                )
                with patch.object(runner, "BinanceLiveRESTClient"), patch.object(runner, "APIGateway", return_value=gateway):
                    report = runner.run_preflight(configuration)
                self.assertEqual(report["checks"]["quote_balance_meets_market_minimum"], expected)
                self.assertEqual(report["pilot_prerequisites"], "PASS" if expected else "NO_GO")
                self.assertNotIn("balances", report)  # 필요조건 boolean만 report에 게시한다.

    def test_main_failure_output_is_secret_free(self) -> None:
        """
        함수 이름: test_main_failure_output_is_secret_free()
        기능: credential 값을 포함한 예외도 타입과 정수 API code만 출력한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        output = StringIO()
        # 실제 Keychain·resource policy를 변경하지 않고 실패 직렬화 경계만 검증한다.
        with patch.object(sys, "argv", ["runner", "--confirm-live", "LIVE"]), patch.object(runner.resource, "setrlimit"), patch.object(runner.os, "umask"), patch.object(runner, "read_keychain_credential", side_effect=RuntimeError("live-secret-canary")), redirect_stdout(output):
            self.assertEqual(runner.main(), 1)
        self.assertNotIn("live-secret-canary", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["order_mutations"], 0)

    def test_preflight_rejects_discount_payment_without_bnb_valuation(self) -> None:
        """
        함수 이름: test_preflight_rejects_discount_payment_without_bnb_valuation()
        기능: BNB 할인 납부는 평가 조회 없이 차단하고 양방향 표준 수수료 조건을 독립 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        gateway = self._create_ready_gateway()
        gateway.fetch_commission_discount_policy.return_value.can_charge_discount_asset = True
        configuration = LiveConfiguration("key", "secret", enabled=True, confirmation="LIVE")
        rest = Mock()
        with patch.object(runner, "BinanceLiveRESTClient", return_value=rest), patch.object(runner, "APIGateway", return_value=gateway):
            report = runner.run_preflight(configuration)
        self.assertEqual(report["read_only_preflight"], "PASS")
        self.assertEqual(report["pilot_prerequisites"], "NO_GO")
        self.assertEqual(report["blockers"], ["discount_asset_payment_disabled"])
        self.assertNotIn("bnb_fee_valuation", report["checks"])
        rest.resolve_bnb_fee.assert_not_called()
        rest.submit_order.assert_not_called()
        rest.cancel_order.assert_not_called()

    def test_preflight_requires_exact_fee_rate_on_both_order_sides(self) -> None:
        """
        함수 이름: test_preflight_requires_exact_fee_rate_on_both_order_sides()
        기능: 매수와 매도의 0·할인·추가 수수료를 각각 0.1% 정책 불일치로 보고한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        configuration = LiveConfiguration("key", "secret", enabled=True, confirmation="LIVE")
        for order_side in ("buy", "sell"):
            for commission_rate in ("0", "0.00075", "0.002"):
                with self.subTest(order_side=order_side, commission_rate=commission_rate):
                    gateway = self._create_ready_gateway()
                    commission = gateway.fetch_commission_discount_policy.return_value
                    setattr(
                        commission,
                        f"market_{order_side}_received_asset_commission_rate",
                        Decimal(commission_rate),
                    )
                    with patch.object(runner, "BinanceLiveRESTClient"), patch.object(runner, "APIGateway", return_value=gateway):
                        report = runner.run_preflight(configuration)
                    self.assertEqual(report["read_only_preflight"], "PASS")
                    self.assertEqual(report["pilot_prerequisites"], "NO_GO")
                    self.assertEqual(report["blockers"], [f"market_{order_side}_fee_rate_matches_policy"])

    def test_preflight_fee_policy_does_not_depend_on_bnb_balance_or_price(self) -> None:
        """
        함수 이름: test_preflight_fee_policy_does_not_depend_on_bnb_balance_or_price()
        기능: BNB 잔액이 없거나 변해도 가격 조회 없이 현물 0.1% 정책이 통과함을 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        configuration = LiveConfiguration("key", "secret", enabled=True, confirmation="LIVE")
        for bnb_balance in (None, "0", "1"):
            with self.subTest(bnb_balance=bnb_balance):
                gateway = self._create_ready_gateway()
                if bnb_balance is not None:
                    gateway.fetch_account_snapshot.return_value.balances += (
                        SimpleNamespace(asset="BNB", free=Decimal(bnb_balance)),
                    )
                rest = Mock()
                rest.resolve_bnb_fee.side_effect = ValueError("secret-canary signed URL")
                with patch.object(runner, "BinanceLiveRESTClient", return_value=rest), patch.object(runner, "APIGateway", return_value=gateway):
                    report = runner.run_preflight(configuration)
                self.assertEqual(report["pilot_prerequisites"], "PASS")
                self.assertEqual(report["blockers"], [])
                self.assertNotIn("bnb_fee_valuation_failure", report)
                self.assertNotIn("balances", report)
                self.assertNotIn("secret-canary", json.dumps(report))
                rest.resolve_bnb_fee.assert_not_called()

    def _create_ready_gateway(self) -> Mock:
        """
        함수 이름: _create_ready_gateway()
        기능: 주문·외부 조회 없이 기본 현물 수수료 정책과 사전 검사 조건을 만족하는 대역을 만든다.
        인자: 없음
        반환값: USDT 잔액·양방향 0.1% 수수료·미체결 없음이 설정된 Gateway 대역
        작성 날짜: 2026/09/22
        """
        gateway = Mock()
        gateway.fetch_account_snapshot.return_value.balances = (
            SimpleNamespace(asset="USDT", free=Decimal("5")),
        )
        gateway.fetch_symbol_trading_rules.return_value.symbol = "ETHUSDT"
        gateway.fetch_symbol_trading_rules.return_value.notional_filters = ()
        gateway.has_any_exchange_open_orders.return_value = False
        gateway.has_any_exchange_open_order_lists.return_value = False
        commission = gateway.fetch_commission_discount_policy.return_value
        commission.discount_asset = "BNB"
        commission.can_charge_discount_asset = False
        commission.market_buy_received_asset_commission_rate = Decimal("0.001")
        commission.market_sell_received_asset_commission_rate = Decimal("0.001")
        return gateway
