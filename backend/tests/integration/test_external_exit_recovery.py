"""수동 매도 뒤 실제 startup·장부·Position·UI DTO 복구와 실패 경계를 검증한다."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from binance_auto_trader.adapters.persistence import TradeHistoryRepository
from binance_auto_trader.adapters.persistence.residual_repository import ResidualRepository
from binance_auto_trader.adapters.filesystem.csv_file_gateway import CSV_HEADER, _trade_to_csv_row
from binance_auto_trader.application.residual_settlement import ResidualSettlement
from binance_auto_trader.application.trading_controller import StartupOrderReconciliationError
from binance_auto_trader.domain.history.trade import Trade, trade_from_json_object, trade_to_json_object
from binance_auto_trader.domain.trading.account_execution import AccountExecution
from binance_auto_trader.domain.trading.fee_valuation import BnbFeeValuation
from binance_auto_trader.domain.trading.order import Fill, OrderResult, OrderStatus
from binance_auto_trader.domain.trading.states import ExitReason, OrderSide
from binance_auto_trader.transport.contracts import map_trade
from tests.integration.test_account_stream_flow import _account_rest_payload
from tests.integration.test_testnet_restart_reconciliation_flow import FIXED_TIME, RestartReconciliationRESTClient, _create_recovery_controller
from tests.unit.history.factories import make_trade


def recovery_facts(quantity: str = "0.0039", balance: str = "0.000096"):
    """
    함수 이름: recovery_facts()
    기능: 실제 장애와 같은 매수·ETH fee·수동 SELL을 익명 ID의 외부 거래소 fixture로 만든다.
    인자: quantity -> 외부 매도량, balance -> 최종 ETH 잔고
    반환값: durable BUY와 읽기 전용 fake 거래소
    작성 날짜: 2026/09/10
    """
    buy_time = FIXED_TIME - timedelta(hours=2)
    sell_time = FIXED_TIME - timedelta(hours=1)
    buy = replace(make_trade(order_id="1001", executed_at=buy_time, requested_quantity=Decimal("0.00409"), executed_quantity=Decimal("0.004"), executed_amount=Decimal("9.77616"), average_fill_price=Decimal("2444.04"), fee_amount=Decimal("0.000004"), fee_asset="ETH", fee_quote_amount=Decimal("0.00977616")), client_order_id="bat-external-recovery-buy")
    buy_fill = Fill("1001", "10001", buy.executed_quantity, buy.average_fill_price, buy.fee_amount, "ETH", buy.fee_quote_amount, buy_time)
    buy_result = OrderResult("ETHUSDT", buy.client_order_id, OrderStatus.FILLED, buy_time, "1001", (buy_fill,))
    sell_fill = Fill("1002", "10002", Decimal(quantity), Decimal("2421.86"), Decimal("0.00944525"), "USDT", Decimal("0.00944525"), sell_time)
    sell_result = OrderResult("ETHUSDT", "web-manual-sell", OrderStatus.FILLED, sell_time, "1002", (sell_fill,))
    executions = (AccountExecution(OrderSide.BUY, buy.executed_quantity, buy_time, buy_result), AccountExecution(OrderSide.SELL, Decimal(quantity), sell_time, sell_result))
    return buy, ExternalExitRESTClient(buy_result, executions, balance)


class ExternalExitRESTClient(RestartReconciliationRESTClient):
    """
    클래스 이름: ExternalExitRESTClient
    기능: 신규 주문을 거부하며 외부 체결과 ETH 잔고만 제공한다.
    작성 날짜: 2026/09/10
    """

    def __init__(self, result, executions, balance):
        """
        함수 이름: __init__()
        기능: 불변 체결과 잔고 및 race 주입 상태를 준비한다.
        인자: result -> 앱 매수, executions -> 전체 체결, balance -> ETH 잔고
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        super().__init__(result)
        self.executions = executions
        self.balance = balance
        self.open_orders = False
        self.execution_reads = 0
        self.change_on_repeat = False

    def get_account(self):
        """
        함수 이름: get_account()
        기능: 외부 매도 이후의 실제 잔액을 반환한다.
        인자: 없음
        반환값: 공식 account payload
        작성 날짜: 2026/09/10
        """
        payload = _account_rest_payload()
        for row in payload["balances"]:
            if row["asset"] == "ETH":
                row.update(free=self.balance, locked="0")
        return payload

    def list_account_executions_since(self, *, symbol, order_id):
        """
        함수 이름: list_account_executions_since()
        기능: cursor 이후 모든 client 체결과 선택적 조회 race를 반환한다.
        인자: symbol -> ETHUSDT, order_id -> durable cursor
        반환값: 체결 tuple
        작성 날짜: 2026/09/10
        """
        self.execution_reads += 1
        if self.change_on_repeat and self.execution_reads > 1:
            return ()
        return tuple(item for item in self.executions if int(item.result.exchange_order_id) >= int(order_id))

    def has_any_exchange_open_orders(self):
        """
        함수 이름: has_any_exchange_open_orders()
        기능: 전체 계좌 미체결 주문 유무를 제공한다.
        인자: 없음
        반환값: bool
        작성 날짜: 2026/09/10
        """
        return self.open_orders

    def has_any_exchange_open_order_lists(self):
        """
        함수 이름: has_any_exchange_open_order_lists()
        기능: 전체 계좌 조건부 주문 목록의 부재를 제공한다.
        인자: 없음
        반환값: False
        작성 날짜: 2026/09/10
        """
        return False


class ExternalExitRecoveryTests(unittest.TestCase):
    """
    클래스 이름: ExternalExitRecoveryTests
    기능: 실제 startup·내구 이력·잔여 회계의 연결과 차단 조건을 검사한다.
    작성 날짜: 2026/09/10
    """

    def reconcile(self, path, client):
        """
        함수 이름: reconcile()
        기능: 새 process와 같은 실제 Controller를 만들고 정상 종료까지 정리한다.
        인자: path -> 격리된 이력, client -> 읽기 전용 fake 거래소
        반환값: controller, history, position
        작성 날짜: 2026/09/10
        """
        controller, history, position = _create_recovery_controller(path, client)
        self.addCleanup(controller.close_session_resources)
        controller._residual_settlement = ResidualSettlement(ResidualRepository(path.parent / "residual-ledger.json"))
        with patch.object(controller._api_gateway, "fetch_symbol_trading_rules", return_value=SimpleNamespace(lot_size=SimpleNamespace(step_size=Decimal("0.0001")))):
            controller.reconcile_startup_state()
        return controller, history, position

    def test_full_manual_exit_is_durable_idempotent_and_preserves_dust(self):
        """
        함수 이름: test_full_manual_exit_is_durable_idempotent_and_preserves_dust()
        기능: 실제 장애 수치로 세 번 재시작해 중복 없는 SELL·잔량·성과와 DTO를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        with TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "history.jsonl"
            buy, client = recovery_facts()
            TradeHistoryRepository(path).save_this_trade_by_order_id(buy.order_id, buy)
            original = path.read_bytes()
            for _ in range(3):
                controller, history, position = self.reconcile(path, client)
                self.assertTrue(controller.startup_reconciliation_complete)
                self.assertEqual(position.quantity, 0)
                self.assertEqual(controller.residual_totals[0], Decimal("0.000096"))
                self.assertEqual(controller.snapshot_session().status.value, "not_started")
                trades = history.trade_history.trades
                self.assertEqual(len(trades), 2)
                self.assertEqual(trades[0], buy)
                external = trades[1]
                self.assertEqual(external.exit_reason, ExitReason.EXTERNAL_MANUAL)
                self.assertIsNone(external.market_price_at_decision)
                self.assertEqual(external.requested_quantity, Decimal("0.0039"))
                self.assertEqual(trade_from_json_object(trade_to_json_object(external)), external)
                self.assertEqual(map_trade(external)["exit_reason"], "EXTERNAL_MANUAL")
                self.assertNotIn("BNB", map_trade(external)["fee_note"])
                exported = dict(zip(CSV_HEADER, _trade_to_csv_row(external), strict=True))
                self.assertEqual(exported["market_price_at_decision"], "")
                self.assertEqual(exported["exit_reason"], "EXTERNAL_MANUAL")
                self.assertEqual(exported["trade_schema_version"], "4")
                with localcontext() as context:
                    context.prec = 34
                    self.assertEqual(external.allocated_cost_basis + controller.residual_totals[1], buy.executed_amount)
                controller.close_session_resources()
            self.assertTrue(path.read_bytes().startswith(original))
            self.assertEqual(client.submit_count, 0)

    def test_partial_manual_exit_preserves_open_position_across_restart(self):
        """
        함수 이름: test_partial_manual_exit_preserves_open_position_across_restart()
        기능: 부분 수동 매도가 잔여 포지션을 닫거나 자동매매를 시작하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        with TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "history.jsonl"
            buy, client = recovery_facts("0.002", "0.001996")
            TradeHistoryRepository(path).save_this_trade_by_order_id(buy.order_id, buy)
            for _ in range(2):
                controller, history, position = self.reconcile(path, client)
                self.assertEqual(position.quantity, Decimal("0.001996"))
                self.assertEqual(controller.residual_totals[0], 0)
                self.assertEqual(len(history.trade_history.trades), 2)
                controller.close_session_resources()
            self.assertEqual(client.submit_count, 0)

    def test_sequential_partial_exits_preserve_fees_cost_and_idempotency(self):
        """
        함수 이름: test_sequential_partial_exits_preserve_fees_cost_and_idempotency()
        기능: 취소된 부분 체결 뒤 BNB 수수료 매도가 이어져도 원 주문량·원가·증거를 보존한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        with TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "history.jsonl"
            buy, client = recovery_facts("0.002", "0.000096")
            first = client.executions[1]
            first = replace(first, requested_quantity=Decimal("0.0039"), result=replace(first.result, status=OrderStatus.CANCELED))
            second_time = first.result.processed_at + timedelta(minutes=1)
            milliseconds = int(second_time.timestamp()) * 1000
            valuation = BnbFeeValuation(milliseconds - 1000, milliseconds - 1, Decimal("600"))
            second_fill = replace(first.result.fills[0], exchange_order_id="1003", trade_id="10003", quantity=Decimal("0.0019"), fee_asset="BNB", fee_amount=Decimal("0.00001"), fee_quote_amount=Decimal("0.006"), fee_valuation=valuation, executed_at=second_time)
            second_result = replace(first.result, exchange_order_id="1003", client_order_id="web-second-manual-sell", status=OrderStatus.FILLED, fills=(second_fill,), processed_at=second_time)
            second = AccountExecution(OrderSide.SELL, Decimal("0.0019"), second_time, second_result)
            client.executions = (client.executions[0], first, second)
            TradeHistoryRepository(path).save_this_trade_by_order_id(buy.order_id, buy)
            for attempt in range(2):
                controller, history, position = self.reconcile(path, client)
                trades = history.trade_history.trades
                self.assertEqual(len(trades), 3)
                self.assertEqual(trades[1].requested_quantity, Decimal("0.0039"))
                self.assertEqual(trades[1].executed_quantity, Decimal("0.002"))
                self.assertEqual(trades[2].fee_fills, (second_fill,))
                self.assertEqual(trade_from_json_object(trade_to_json_object(trades[2])), trades[2])
                self.assertEqual(position.quantity, 0)
                self.assertEqual(controller.residual_totals[0], Decimal("0.000096"))
                with localcontext() as context:
                    context.prec = 34
                    self.assertEqual(sum(trade.allocated_cost_basis for trade in trades[1:]) + controller.residual_totals[1], buy.executed_amount)
                controller.close_session_resources()
            self.assertEqual(client.submit_count, 0)

    def test_save_failure_recovers_from_durable_record_without_duplicate_sale(self):
        """
        함수 이름: test_save_failure_recovers_from_durable_record_without_duplicate_sale()
        기능: 외부 SELL 저장 직후 publication 실패를 다음 startup이 원 주문 ID로 복구한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        with TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "history.jsonl"
            buy, client = recovery_facts()
            TradeHistoryRepository(path).save_this_trade_by_order_id(buy.order_id, buy)
            original_save = TradeHistoryRepository.save_this_trade_by_order_id

            def save_then_fail(repository, order_id, trade):
                """
                함수 이름: save_then_fail()
                기능: 실제 파일 저장과 fsync 뒤 메모리 publication 전에 중단을 주입한다.
                인자: repository -> 실제 저장소, order_id -> 주문 ID, trade -> 저장할 외부 매도
                반환값: 반환 전에 OSError
                작성 날짜: 2026/09/10
                """
                original_save(repository, order_id, trade)
                raise OSError("injected post-save failure")

            with patch.object(TradeHistoryRepository, "save_this_trade_by_order_id", save_then_fail), self.assertRaises(OSError):
                self.reconcile(path, client)
            controller, history, position = self.reconcile(path, client)
            self.assertEqual(len(history.trade_history.trades), 2)
            self.assertEqual(position.quantity, 0)
            self.assertEqual(controller.residual_totals[0], Decimal("0.000096"))
            self.assertEqual(client.submit_count, 0)

    def test_ambiguous_evidence_never_changes_durable_history(self):
        """
        함수 이름: test_ambiguous_evidence_never_changes_durable_history()
        기능: 이체·다른 잔고·외부 BUY·진행 중 주문·조회 race·중복을 복구로 숨기지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        for scenario in ("missing_sell", "withdrawal", "other_holdings", "external_buy", "active", "open_orders", "race", "duplicate", "oversell"):
            with self.subTest(scenario=scenario), TemporaryDirectory() as directory:
                path = Path(directory).resolve() / "history.jsonl"
                buy, client = recovery_facts()
                if scenario == "missing_sell":
                    client.executions = client.executions[:1]
                elif scenario == "withdrawal":
                    client.balance = "0"
                elif scenario == "other_holdings":
                    client.balance = "1"
                elif scenario == "external_buy":
                    client.executions = (client.executions[0], replace(client.executions[1], side=OrderSide.BUY))
                elif scenario == "active":
                    item = client.executions[1]
                    client.executions = (client.executions[0], replace(item, result=replace(item.result, status=OrderStatus.PARTIALLY_FILLED)))
                elif scenario == "open_orders":
                    client.open_orders = True
                elif scenario == "race":
                    client.change_on_repeat = True
                elif scenario == "duplicate":
                    client.executions += (client.executions[1],)
                elif scenario == "oversell":
                    client.executions = (client.executions[0], replace(client.executions[1], requested_quantity=Decimal("1"), result=replace(client.executions[1].result, status=OrderStatus.CANCELED)))
                TradeHistoryRepository(path).save_this_trade_by_order_id(buy.order_id, buy)
                original = path.read_bytes()
                with self.assertRaises((StartupOrderReconciliationError, ValueError)):
                    self.reconcile(path, client)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(client.submit_count, 0)


if __name__ == "__main__":
    unittest.main()
