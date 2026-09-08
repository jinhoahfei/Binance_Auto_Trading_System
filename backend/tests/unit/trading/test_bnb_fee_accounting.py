"""BNB 평가·혼합 fallback·durable replay와 malformed 증거를 검증한다."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
import copy
import unittest
from unittest.mock import Mock
from binance_auto_trader.adapters.binance.bnb_fee_valuator import BnbFeeValuator
from binance_auto_trader.adapters.binance.mappers import map_fill_payloads
from binance_auto_trader.domain.trading.fee_valuation import BnbFeeValuation
from binance_auto_trader.domain.trading.position import Position
from binance_auto_trader.domain.trading.order import FeeAssetReconciliationRequiredError
from binance_auto_trader.domain.history.trade import Trade, trade_from_json_object, trade_to_json_object
from binance_auto_trader.bootstrap.live_permission import LiveOrderPermissionRESTClient
from binance_auto_trader.bootstrap.live_configuration import LiveConfigurationError
from tests.unit.trading.test_order import TEST_TIME, make_fill, make_order, make_result, OrderStatus
from tests.unit.bootstrap.test_live_bootstrap import live_configuration, live_order
from tests.unit.bootstrap.test_testnet_configuration import _zero_commission_payload


def valuation_for(instant=TEST_TIME):
    """
    함수 이름: valuation_for()
    기능: 테스트 체결 직전 초의 고정 600 USDT 평가 근거를 만든다.
    인자: instant -> fixture UTC 체결 시각
    반환값: BnbFeeValuation
    작성 날짜: 2026/09/09
    """
    milliseconds = int(instant.timestamp()) * 1000
    return BnbFeeValuation(milliseconds - 1000, milliseconds - 1, Decimal("600"))  # 외부 가격 fixture다.


def bnb_fill():
    """
    함수 이름: bnb_fill()
    기능: 실제 BNB 수수료와 별도 USDT 평가액을 갖는 체결을 만든다.
    인자: 없음
    반환값: Fill
    작성 날짜: 2026/09/09
    """
    return replace(make_fill("11", Decimal("0.5"), Decimal("100")), fee_asset="BNB", fee_amount=Decimal("0.00001"), fee_quote_amount=Decimal("0.006"), fee_valuation=valuation_for())


def mixed_execution():
    """
    함수 이름: mixed_execution()
    기능: BNB 납부 후 ETH fallback한 terminal BUY를 생성한다.
    인자: 없음
    반환값: Order와 summary
    작성 날짜: 2026/09/09
    """
    order = make_order()
    fallback = make_fill("12", Decimal("1"), Decimal("100"), fee_asset="ETH", fee_amount=Decimal("0.001"))
    order.apply_order_result(make_result(OrderStatus.FILLED, (bnb_fill(), fallback)))
    return order, order.build_execution_summary()


class BnbFeeAccountingTests(unittest.TestCase):
    """
    클래스 이름: BnbFeeAccountingTests
    기능: 할인 자산 원장과 평가 손익의 복구 일관성을 검증한다.
    작성 날짜: 2026/09/09
    """

    def test_mixed_buy_quantity_cost_and_history_roundtrip(self):
        """
        함수 이름: test_mixed_buy_quantity_cost_and_history_roundtrip()
        기능: ETH 실수령량과 BNB 비용을 반영하고 저장값으로 동일 상태를 복원한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        order, summary = mixed_execution()
        position = Position()
        position.apply_execution(summary)
        self.assertEqual(position.quantity, Decimal("1.499"))
        self.assertEqual(position.cost_basis, Decimal("150.006"))
        self.assertEqual(summary.fee_asset, "MIXED")
        self.assertEqual(summary.fee_amount, 0)  # 서로 다른 명목 단위는 합산하지 않는다.
        self.assertEqual(summary.fee_quote_amount, Decimal("0.106"))
        trade = Trade.from_order_execution(order, summary)
        self.assertEqual(trade.schema_version, 3)
        restored_trade = trade_from_json_object(trade_to_json_object(trade))
        self.assertEqual(restored_trade, trade)
        restored = Position()
        restored.apply_historical_trade(restored_trade)
        self.assertEqual(restored.get_snapshot(), position.get_snapshot())

    def test_tampered_evidence_and_aggregate_are_rejected(self):
        """
        함수 이름: test_tampered_evidence_and_aggregate_are_rejected()
        기능: 환율·체결·시간·중복·합계 변조를 복원 시 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        order, summary = mixed_execution()
        original = trade_to_json_object(Trade.from_order_execution(order, summary))
        for mutation in ("rate", "time", "amount", "aggregate", "duplicate", "asset", "source"):
            record = copy.deepcopy(original)
            fill = record["fee_fills"][0]
            if mutation == "rate": fill["fee_valuation"]["rate"] = "601"
            elif mutation == "time": fill["fee_valuation"]["close_time_ms"] += 1000
            elif mutation == "amount": fill["fee_amount"] = "0.1"
            elif mutation == "aggregate": record["fee_quote_amount"] = "0"
            elif mutation == "duplicate": record["fee_fills"].append(copy.deepcopy(fill))
            elif mutation == "asset": fill["fee_asset"] = "ABC"
            else: fill["fee_valuation"]["source"] = "CURRENT_TICKER"
            with self.subTest(mutation=mutation), self.assertRaises((ValueError, TypeError)):
                trade_from_json_object(record)

    def test_missing_and_future_valuation_fail_closed(self):
        """
        함수 이름: test_missing_and_future_valuation_fail_closed()
        기능: BNB 근거 누락과 미래·오래된 환율을 0원 비용으로 대체하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        fill = bnb_fill()
        with self.assertRaises(FeeAssetReconciliationRequiredError):
            replace(fill, fee_valuation=None)
        for delta in (-1, 1):
            with self.assertRaises(ValueError):
                replace(fill, fee_valuation=valuation_for(TEST_TIME + timedelta(seconds=delta)))
        self.assertEqual(replace(fill, fee_amount=Decimal("0"), fee_quote_amount=Decimal("0")).fee_quote_amount, 0)

    def test_public_valuation_exact_window_and_unavailable_data(self):
        """
        함수 이름: test_public_valuation_exact_window_and_unavailable_data()
        기능: 정확한 과거 1초봉만 수용하고 빈·미래·NaN 데이터를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        evidence = valuation_for()
        row = [evidence.open_time_ms, "600", "600", "600", "600", "2", evidence.close_time_ms, "1200", 2, "0", "0", "0"]
        request = Mock(return_value=SimpleNamespace(payload=[row]))
        self.assertEqual(BnbFeeValuator(request).resolve(TEST_TIME), evidence)
        self.assertFalse(request.call_args.kwargs["signed"])
        self.assertEqual(request.call_args.kwargs["parameters"]["symbol"], "BNBUSDT")
        for payload in ([], [row, row], [row[:8] + [0] + row[9:]], [row[:4] + ["NaN"] + row[5:]], [[True] + row[1:]]):
            request.return_value = SimpleNamespace(payload=payload)
            with self.assertRaises((ValueError, TypeError)):
                BnbFeeValuator(request).resolve(TEST_TIME)

    def test_rest_mytrades_individual_time_and_dedup(self):
        """
        함수 이름: test_rest_mytrades_individual_time_and_dedup()
        기능: 개별 체결시각과 중복 제거를 검증하고 FULL 시간 대용을 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        payload = {"id": 11, "orderId": 1001, "price": "100", "qty": "0.5", "commission": "0.00001", "commissionAsset": "BNB", "time": int(TEST_TIME.timestamp()) * 1000}
        options = dict(symbol="ETHUSDT", exchange_order_id="1001", base_asset="ETH", quote_asset="USDT", fallback_executed_at=TEST_TIME, bnb_fee_resolver=valuation_for)
        self.assertEqual(map_fill_payloads([payload, payload], **options), (bnb_fill(),))
        del payload["time"]
        with self.assertRaises(ValueError):
            map_fill_payloads([payload], **options)

    def test_permission_requires_policy_and_rejects_other_assets(self):
        """
        함수 이름: test_permission_requires_policy_and_rejects_other_assets()
        기능: BNB 지원 표식·환율 조회가 있어야 할인 설정을 허용한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        delegate = Mock()
        payload = _zero_commission_payload()
        payload["standardCommission"]["taker"] = "0.001"
        payload["discount"] = {"enabledForAccount": True, "enabledForSymbol": True, "discountAsset": "BNB", "discount": "0.25"}
        delegate.get_account_commission.return_value = payload
        order = live_order()
        delegate.prepare_order.return_value = order
        with self.assertRaises(LiveConfigurationError):
            LiveOrderPermissionRESTClient(delegate, live_configuration(orders=True), base_fee_residual_enabled=True).prepare_order(order=order)
        permission = LiveOrderPermissionRESTClient(delegate, live_configuration(orders=True), base_fee_residual_enabled=True, bnb_fee_accounting_enabled=True)
        self.assertIs(permission.prepare_order(order=order), order)
        delegate.resolve_bnb_fee.assert_called_once()
        payload["discount"]["discountAsset"] = "ABC"
        with self.assertRaises(LiveConfigurationError):
            permission.prepare_order(order=order)
        delegate.submit_order.assert_not_called()  # 외부 fixture이며 실제 주문은 없다.


    def test_ws_rest_and_csv_preserve_same_fee_evidence(self):
        """
        함수 이름: test_ws_rest_and_csv_preserve_same_fee_evidence()
        기능: WS 개별 체결이 REST와 같고 CSV v2가 원 수수료와 평가 근거를 보존한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        import json
        from binance_auto_trader.adapters.binance.websocket_gateway import _parse_execution_fill
        from binance_auto_trader.adapters.filesystem.csv_file_gateway import _trade_to_csv_row
        from binance_auto_trader.domain.history.fill_record import fill_from_record
        event = {"l": "0.5", "L": "100", "n": "0.00001", "t": 11, "N": "BNB", "T": int(TEST_TIME.timestamp()) * 1000}
        fill = _parse_execution_fill(event, exchange_order_id="1001", execution_type="TRADE", bnb_fee_resolver=valuation_for)
        self.assertEqual(fill, bnb_fill())
        order, summary = mixed_execution()
        trade = Trade.from_order_execution(order, summary)
        row = _trade_to_csv_row(trade)
        self.assertEqual(row[0], "2")
        self.assertEqual(row[-2], "3")
        self.assertEqual(tuple(fill_from_record(item) for item in json.loads(row[-1])), summary.fills)

    def test_durable_replay_rejects_changed_fill_even_with_equal_totals(self):
        """
        함수 이름: test_durable_replay_rejects_changed_fill_even_with_equal_totals()
        기능: v3 재전송은 합계가 같아도 다른 체결 ID를 동일 이력으로 인정하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        from binance_auto_trader.application.trading_controller import _order_result_accounting_confirms_trade
        order, summary = mixed_execution()
        trade = Trade.from_order_execution(order, summary)
        result = make_result(OrderStatus.FILLED, summary.fills)
        self.assertTrue(_order_result_accounting_confirms_trade(result, trade))
        changed = replace(result, fills=(replace(summary.fills[0], trade_id="other"), summary.fills[1]))
        self.assertFalse(_order_result_accounting_confirms_trade(changed, trade))

    def test_bnb_buy_and_mixed_sell_preserve_realized_cost(self):
        """
        함수 이름: test_bnb_buy_and_mixed_sell_preserve_realized_cost()
        기능: BNB BUY 원가와 BNB/USDT SELL 비용을 정확히 차감하고 fresh zero 전략 상태를 복원한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        from binance_auto_trader.domain.trading.states import OrderSide, ExitReason
        from binance_auto_trader.domain.history.performance import Performance
        buy = make_order()
        buy.apply_order_result(make_result(OrderStatus.FILLED, (replace(bnb_fill(), quantity=Decimal("1.5")),)))
        buy_summary = buy.build_execution_summary()
        position = Position()
        position.apply_execution(buy_summary)
        self.assertEqual(position.quantity, Decimal("1.5"))
        self.assertEqual(position.cost_basis, Decimal("150.006"))
        buy_trade = Trade.from_order_execution(buy, buy_summary)

        # SELL fallback은 실제 USDT 비용만 현금에서 차감하고 BNB는 평가 비용으로 계산한다.
        sell = make_order(side=OrderSide.SELL)
        sell.client_order_id = "sell-client"
        sell.exit_reason = ExitReason.STOP
        first = replace(bnb_fill(), exchange_order_id="2001", price=Decimal("110"))
        second = make_fill("12", Decimal("1"), Decimal("110"), exchange_order_id="2001", fee_amount=Decimal("0.01"))
        result = replace(make_result(OrderStatus.FILLED, ()), exchange_order_id="2001", client_order_id=sell.client_order_id, fills=(first, second))
        sell.apply_order_result(result)
        summary = sell.build_execution_summary()
        realized = Performance().calculate_realized_result(summary, position.cost_basis)
        self.assertEqual(realized.realized_pnl, Decimal("14.978"))
        sell_trade = Trade.from_order_execution(sell, summary, realized)
        position.apply_execution(summary)
        restored = Position()
        for trade in (buy_trade, sell_trade):
            restored.apply_historical_trade(trade_from_json_object(trade_to_json_object(trade)))
        self.assertEqual(position.quantity, 0)
        self.assertEqual(restored.get_snapshot(), position.get_snapshot())
