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
from binance_auto_trader.domain.trading.residual import EarnResidualEvidence
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

    def residual_exit_facts(self, path, *, reward="0.00000001"):
        """
        함수 이름: residual_exit_facts()
        기능: 이전 잔여와 새 매수 net 수량을 함께 수동 매도한 9월 20일 장애를 재현한다.
        인자: path -> 격리 이력, reward -> 검증된 Earn 보상
        반환값: 새 매수, 가짜 거래소, Earn 근거
        작성 날짜: 2026/09/20
        """
        old_buy, old_client = recovery_facts()
        repository = TradeHistoryRepository(path)
        repository.save_this_trade_by_order_id(old_buy.order_id, old_buy)
        controller, _, _ = self.reconcile(path, old_client)
        controller.close_session_resources()
        buy_time = FIXED_TIME - timedelta(minutes=20)
        sell_time = FIXED_TIME - timedelta(minutes=10)
        buy = replace(old_buy, trade_id="trade-1003", order_id="1003", client_order_id="bat-second-buy",
            executed_at=buy_time, requested_quantity=Decimal("0.003717591640607123789184654107238196"),
            executed_quantity=Decimal("0.0037"), executed_amount=Decimal("9.702177"),
            average_fill_price=Decimal("2622.21"), fee_amount=Decimal("0.0000037"), fee_quote_amount=Decimal("0.009702177"))
        repository.save_this_trade_by_order_id(buy.order_id, buy)
        buy_fill = Fill("1003", "10003", buy.executed_quantity, buy.average_fill_price, buy.fee_amount, "ETH", buy.fee_quote_amount, buy_time)
        buy_result = OrderResult("ETHUSDT", buy.client_order_id, OrderStatus.FILLED, buy_time, "1003", (buy_fill,))
        sell_fill = Fill("1004", "10004", Decimal("0.0037"), Decimal("2580.85"), Decimal("0.00954915"), "USDT", Decimal("0.00954915"), sell_time)
        sell_result = OrderResult("ETHUSDT", "web-combined-sell", OrderStatus.FILLED, sell_time, "1004", (sell_fill,))
        executions = (AccountExecution(OrderSide.BUY, buy.executed_quantity, buy_time, buy_result),
            AccountExecution(OrderSide.SELL, Decimal("0.0037"), sell_time, sell_result))
        client = ExternalExitRESTClient(buy_result, executions, str(Decimal("0.0000923") + Decimal(reward)))
        evidence = EarnResidualEvidence(
            (("123", int((FIXED_TIME - timedelta(minutes=50)).timestamp() * 1000), Decimal("0.000096")),),
            Decimal("0"), Decimal(reward),
            (("456", int((FIXED_TIME - timedelta(minutes=40)).timestamp() * 1000), Decimal("0.000096") + Decimal(reward)),))
        client.fetch_earn_residual_evidence = lambda **kwargs: evidence
        return buy, client, evidence

    def test_manual_exit_consumes_old_residual_and_preserves_earn_reward_across_restarts(self):
        """
        함수 이름: test_manual_exit_consumes_old_residual_and_preserves_earn_reward_across_restarts()
        기능: 실제 매도량·잔여 원가·보상을 보존하고 반복 시작 시 중복 차감을 막는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/20
        """
        for reward in ("0", "0.00000001"):
            with self.subTest(reward=reward), TemporaryDirectory() as directory:
                path = Path(directory).resolve() / "history.jsonl"
                buy, client, _ = self.residual_exit_facts(path, reward=reward)
                original_history = path.read_bytes()
                ledger = path.parent / "residual-ledger.json"
                original_ledger = ledger.read_bytes()
                with localcontext() as context:
                    context.prec = 34
                    original_cost = Decimal("0.234862702702702702702702702702703")
                    consumed_cost = original_cost * Decimal("0.0000037") / Decimal("0.000096")
                    remaining_cost = original_cost - consumed_cost
                for _ in range(3):
                    controller, history, position = self.reconcile(path, client)
                    self.assertTrue(controller.startup_reconciliation_complete)
                    self.assertEqual(position.quantity, 0)
                    self.assertEqual(controller.residual_totals, (Decimal("0.0000923"), remaining_cost))
                    self.assertEqual(controller.snapshot_session().status.value, "not_started")
                    trades = history.trade_history.trades
                    self.assertEqual(len(trades), 4)
                    sell = trades[-1]
                    self.assertEqual(sell.executed_quantity, Decimal("0.0037"))
                    self.assertEqual(sell.requested_quantity, Decimal("0.0037"))
                    self.assertEqual(sell.fee_fills, client.executions[-1].result.fills)
                    with localcontext() as context:
                        context.prec = 34
                        self.assertEqual(sell.allocated_cost_basis, buy.executed_amount + consumed_cost)
                        self.assertEqual(sell.realized_pnl, sell.executed_amount - sell.fee_quote_amount - sell.allocated_cost_basis)
                    receipt = controller.balance_reconciliation_snapshot()
                    self.assertEqual(receipt["status"], "verified")
                    self.assertEqual(Decimal(receipt["earn_rewards_quantity"]), Decimal(reward))
                    self.assertEqual(Decimal(receipt["difference_quantity"]), 0)
                    controller.close_session_resources()
                self.assertTrue(path.read_bytes().startswith(original_history))
                self.assertEqual(ledger.read_bytes(), original_ledger)
                self.assertEqual(client.submit_count, 0)

    def test_combined_exit_recovers_after_save_without_double_consuming_residual(self):
        """
        함수 이름: test_combined_exit_recovers_after_save_without_double_consuming_residual()
        기능: SELL 내구 저장 직후 중단돼도 잔여 차감을 다음 시작에서 한 번만 재생한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/20
        """
        with TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "history.jsonl"
            _, client, _ = self.residual_exit_facts(path)
            original_save = TradeHistoryRepository.save_this_trade_by_order_id

            def save_then_fail(repository, order_id, trade):
                """실제 저장이 완료된 직후 process 중단을 모사한다."""
                original_save(repository, order_id, trade)
                raise OSError("injected post-save failure")

            with patch.object(TradeHistoryRepository, "save_this_trade_by_order_id", save_then_fail), self.assertRaises(OSError):
                self.reconcile(path, client)
            for _ in range(2):
                controller, history, position = self.reconcile(path, client)
                self.assertEqual(len(history.trade_history.trades), 4)
                self.assertEqual(position.quantity, 0)
                self.assertEqual(controller.residual_totals[0], Decimal("0.0000923"))
                controller.close_session_resources()
            self.assertEqual(client.submit_count, 0)

    def test_combined_exit_still_rejects_unexplained_or_unstable_evidence_before_writing(self):
        """
        함수 이름: test_combined_exit_still_rejects_unexplained_or_unstable_evidence_before_writing()
        기능: 잔여 포함 복구도 미확인 보상·이체·Earn 변경·늦은 상환·장부 초과를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/20
        """
        for scenario in ("missing_earn", "earn_race", "late_redemption", "oversized_request", "extra_balance", "withdrawal", "base_fee"):
            with self.subTest(scenario=scenario), TemporaryDirectory() as directory:
                path = Path(directory).resolve() / "history.jsonl"
                _, client, evidence = self.residual_exit_facts(path)
                if scenario == "missing_earn":
                    client.fetch_earn_residual_evidence = lambda **kwargs: None
                elif scenario == "earn_race":
                    values = iter((evidence, None))
                    client.fetch_earn_residual_evidence = lambda **kwargs: next(values)
                elif scenario == "late_redemption":
                    late = replace(evidence, redemptions=(("456", int(FIXED_TIME.timestamp() * 1000), Decimal("0.00009601")),))
                    client.fetch_earn_residual_evidence = lambda **kwargs: late
                elif scenario == "oversized_request":
                    sell = client.executions[-1]
                    client.executions = (client.executions[0], replace(sell, requested_quantity=Decimal("0.004"), result=replace(sell.result, status=OrderStatus.CANCELED)))
                elif scenario == "extra_balance":
                    client.balance = "0.00009232"
                elif scenario == "withdrawal":
                    client.balance = "0.00009230"
                elif scenario == "base_fee":
                    sell = client.executions[-1]
                    fill = replace(sell.result.fills[0], fee_asset="ETH", fee_amount=Decimal("0.000001"), fee_quote_amount=Decimal("0.00258085"))
                    client.executions = (client.executions[0], replace(sell, result=replace(sell.result, fills=(fill,))))
                ledger = path.parent / "residual-ledger.json"
                original = path.read_bytes(), ledger.read_bytes()
                with self.assertRaises(StartupOrderReconciliationError):
                    self.reconcile(path, client)
                self.assertEqual((path.read_bytes(), ledger.read_bytes()), original)
                self.assertEqual(client.submit_count, 0)

    def test_new_open_lot_provenance_replays_prior_combined_exit(self):
        """
        함수 이름: test_new_open_lot_provenance_replays_prior_combined_exit()
        기능: 다음 매수 lot의 재시작·시작 조건도 이전 잔여 소비 이력을 정확히 재생한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/20
        """
        with TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "history.jsonl"
            buy, client, _ = self.residual_exit_facts(path)
            controller, _, _ = self.reconcile(path, client)
            controller.close_session_resources()
            buy = replace(buy, trade_id="trade-1005", order_id="1005", client_order_id="bat-third-buy", executed_at=FIXED_TIME)
            TradeHistoryRepository(path).save_this_trade_by_order_id(buy.order_id, buy)
            fill = replace(client.executions[0].result.fills[0], exchange_order_id="1005", trade_id="10005", executed_at=FIXED_TIME)
            client.result = replace(client.executions[0].result, exchange_order_id="1005", client_order_id=buy.client_order_id, processed_at=FIXED_TIME, fills=(fill,))
            client.executions = (AccountExecution(OrderSide.BUY, buy.executed_quantity, FIXED_TIME, client.result),)
            client.balance = "0.00378861"
            controller, history, position = self.reconcile(path, client)
            self.assertEqual(position.quantity, Decimal("0.0036963"))
            self.assertEqual(controller._resolve_recovered_position_provenance(history.trade_history.trades, position.get_snapshot()), (buy.regime_type, buy.strategy))
            self.assertEqual(controller.residual_totals[0], Decimal("0.0000923"))

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
