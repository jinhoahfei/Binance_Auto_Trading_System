"""비할인 현물 체결과 기존 BNB 장부의 조회 전용 호환을 검증한다."""

from dataclasses import replace
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN, localcontext
import unittest
from urllib.parse import urlsplit

from binance_auto_trader.adapters.binance.live_clients import BinanceLiveRESTClient
from binance_auto_trader.adapters.binance.mappers import map_fill_payloads
from binance_auto_trader.adapters.binance.websocket_gateway import _parse_execution_fill
from binance_auto_trader.domain.trading.fee_valuation import BnbFeeValuation
from binance_auto_trader.domain.trading.order import FeeAssetReconciliationRequiredError, Fill, OrderStatus
from tests.unit.binance.test_spot_rest_client import (
    FIXED_TIME, FIXED_TIME_MILLISECONDS, QueueHTTPTransport, _client,
    _exchange_info_payload, _filled_order_payload, _json_response, _order,
)


def historical_fill() -> Fill:
    """
    함수 이름: historical_fill()
    기능: v3/v4 장부에서 이미 검증된 BNB 수수료 체결 fixture를 만든다.
    인자: 없음
    반환값: 원 수수료와 당시 평가 근거를 가진 Fill
    작성 날짜: 2026/09/22
    """
    return Fill(
        exchange_order_id="42", trade_id="7001", quantity=Decimal("0.5"),
        price=Decimal("100"), fee_amount=Decimal("0.00001"), fee_asset="BNB",
        fee_quote_amount=Decimal("0.006"), executed_at=FIXED_TIME,
        fee_valuation=BnbFeeValuation(
            FIXED_TIME_MILLISECONDS - 1000, FIXED_TIME_MILLISECONDS - 1, Decimal("600"),
        ),
    )


def trade_payload() -> dict[str, object]:
    """
    함수 이름: trade_payload()
    기능: 저장 BNB 체결과 원본 필드가 일치하는 signed myTrades 행을 만든다.
    인자: 없음
    반환값: 공식 myTrades 형식의 체결 object
    작성 날짜: 2026/09/22
    """
    return {
        "symbol": "ETHUSDT", "id": 7001, "orderId": 42, "price": "100", "qty": "0.5",
        "commission": "0.00001", "commissionAsset": "BNB", "time": FIXED_TIME_MILLISECONDS,
        "isBuyer": True,
    }


def restore_history(client: object, *, fills: tuple[Fill, ...] | None = None) -> None:
    """
    함수 이름: restore_history()
    기능: 해당 주문에서만 재사용할 과거 체결 fixture를 등록한다.
    인자: client -> 테스트 REST client, fills -> 기본 과거 체결을 대체할 검증 입력
    반환값: 없음
    작성 날짜: 2026/09/22
    """
    client.restore_historical_fee_fills(
        symbol="ETHUSDT", client_order_id="bat-client-0", exchange_order_id="42",
        fills=(historical_fill(),) if fills is None else fills,
    )


class SpotFeePolicyTests(unittest.TestCase):
    """
    클래스 이름: SpotFeePolicyTests
    기능: 신규 BNB 차단과 과거 체결 근거의 제한된 재사용을 검증한다.
    작성 날짜: 2026/09/22
    """

    def test_rest_and_stream_preserve_base_and_quote_commissions(self) -> None:
        """
        함수 이름: test_rest_and_stream_preserve_base_and_quote_commissions()
        기능: 매수 ETH와 매도 USDT의 0.1% 비용을 두 수신 경로에서 동일하게 보존한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        for fee_asset, fee_amount in (("ETH", "0.0005"), ("USDT", "0.05")):
            with self.subTest(fee_asset=fee_asset):
                rest_fill = map_fill_payloads(
                    [{**trade_payload(), "commissionAsset": fee_asset, "commission": fee_amount}],
                    symbol="ETHUSDT", exchange_order_id="42", base_asset="ETH",
                    quote_asset="USDT", fallback_executed_at=FIXED_TIME,
                )[0]
                stream_fill = _parse_execution_fill(
                    {"l": "0.5", "L": "100", "n": fee_amount, "t": 7001,
                     "N": fee_asset, "T": FIXED_TIME_MILLISECONDS},
                    exchange_order_id="42", execution_type="TRADE",
                )
                self.assertEqual(rest_fill, stream_fill)
                self.assertEqual(rest_fill.fee_quote_amount, Decimal("0.05"))
                self.assertIsNone(rest_fill.fee_valuation)

    def test_new_bnb_is_rejected_even_when_reported_commission_is_zero(self) -> None:
        """
        함수 이름: test_new_bnb_is_rejected_even_when_reported_commission_is_zero()
        기능: BNB 잔액이나 비용 크기에 관계없이 새 BNB 수수료를 두 경로에서 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        for amount in ("0", "0.00001"):
            with self.subTest(amount=amount):
                with self.assertRaises(FeeAssetReconciliationRequiredError):
                    map_fill_payloads(
                        [{**trade_payload(), "commission": amount}], symbol="ETHUSDT",
                        exchange_order_id="42", base_asset="ETH", quote_asset="USDT",
                        fallback_executed_at=FIXED_TIME,
                    )
                with self.assertRaises(FeeAssetReconciliationRequiredError):
                    _parse_execution_fill(
                        {"l": "0.5", "L": "100", "n": amount, "t": 7001,
                         "N": "BNB", "T": FIXED_TIME_MILLISECONDS},
                        exchange_order_id="42", execution_type="TRADE",
                    )
        self.assertFalse(hasattr(BinanceLiveRESTClient, "resolve_bnb_fee"))

    def test_base_fee_conversion_ignores_ambient_decimal_precision(self) -> None:
        """
        함수 이름: test_base_fee_conversion_ignores_ambient_decimal_precision()
        기능: 외부 Decimal 정밀도가 낮아져도 REST와 WS의 ETH 0.1% 비용을 보존한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        fee_amount = "0.00000123456789123456789"
        price = "2345.67890123456789123"
        quantity = "0.00123456789123456789"
        with localcontext() as decimal_context:
            decimal_context.prec = 34
            decimal_context.rounding = ROUND_HALF_EVEN
            expected_cost = Decimal(fee_amount) * Decimal(price)
            decimal_context.prec = 6
            decimal_context.rounding = ROUND_DOWN
            rest_fill = map_fill_payloads(
                [{**trade_payload(), "qty": quantity, "price": price,
                  "commissionAsset": "ETH", "commission": fee_amount}],
                symbol="ETHUSDT", exchange_order_id="42", base_asset="ETH",
                quote_asset="USDT", fallback_executed_at=FIXED_TIME,
            )[0]
            stream_fill = _parse_execution_fill(
                {"l": quantity, "L": price, "n": fee_amount, "t": 7001,
                 "N": "ETH", "T": FIXED_TIME_MILLISECONDS},
                exchange_order_id="42", execution_type="TRADE",
            )
        self.assertEqual(rest_fill, stream_fill)
        self.assertEqual(rest_fill.fee_quote_amount, expected_cost)

    def test_recent_history_reuses_only_matching_saved_evidence_without_prices(self) -> None:
        """
        함수 이름: test_recent_history_reuses_only_matching_saved_evidence_without_prices()
        기능: 일치한 과거 체결만 복원하고 주문·수수료·시각 변조와 새 BNB를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        for mutation in (None, "client", "trade", "fee", "time", "missing_history"):
            with self.subTest(mutation=mutation):
                payload = _filled_order_payload(quantity="0.5")
                payload.pop("fills")
                payload["cummulativeQuoteQty"] = "50"
                fill_payload = trade_payload()
                if mutation == "client":
                    payload["clientOrderId"] = "bat-new-order"
                elif mutation == "trade":
                    fill_payload["id"] = 7002
                elif mutation == "fee":
                    fill_payload["commission"] = "0.00002"
                elif mutation == "time":
                    fill_payload["time"] += 1
                transport = QueueHTTPTransport([
                    _json_response({"serverTime": FIXED_TIME_MILLISECONDS}),
                    _json_response([payload]), _json_response([fill_payload]),
                    _json_response(_exchange_info_payload()),
                ])
                client = _client(transport)
                if mutation != "missing_history":
                    restore_history(client)
                if mutation is None:
                    self.assertEqual(client.list_recent_order_results()[0].fills, (historical_fill(),))
                else:
                    with self.assertRaises(ValueError):
                        client.list_recent_order_results()
                self.assertEqual(
                    [urlsplit(request["url"]).path for request in transport.requests],
                    ["/api/v3/time", "/api/v3/allOrders", "/api/v3/myTrades", "/api/v3/exchangeInfo"],
                )

    def test_full_response_cannot_reuse_historical_fee_evidence(self) -> None:
        """
        함수 이름: test_full_response_cannot_reuse_historical_fee_evidence()
        기능: FULL 체결은 저장 근거가 있어도 BNB 재평가나 추가 myTrades 조회를 하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        payload = _filled_order_payload(quantity="0.5")
        payload["cummulativeQuoteQty"] = "50"
        payload["fills"][0].update(commission="0.00001", commissionAsset="BNB")
        transport = QueueHTTPTransport([
            _json_response({"serverTime": FIXED_TIME_MILLISECONDS}),
            _json_response(payload), _json_response(_exchange_info_payload()),
        ])
        client = _client(transport)
        restore_history(client)
        result = client.query_order_result(order=_order(quantity="0.5"))
        self.assertIs(result.status, OrderStatus.UNKNOWN)
        self.assertEqual(result.fills, ())
        self.assertEqual(len(transport.requests), 3)

    def test_external_manual_history_retains_saved_bnb_cost(self) -> None:
        """
        함수 이름: test_external_manual_history_retains_saved_bnb_cost()
        기능: v4 외부 매도 장부의 원 client ID로 조회해 저장 BNB 비용을 재사용한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        client_order_id = "web:manual/sell.1"
        payload = _filled_order_payload(client_order_id=client_order_id, quantity="0.5")
        payload.pop("fills")
        payload.update(side="SELL", time=FIXED_TIME_MILLISECONDS, cummulativeQuoteQty="50")
        transport = QueueHTTPTransport([
            _json_response({"serverTime": FIXED_TIME_MILLISECONDS}),
            _json_response([payload]),
            _json_response([{**trade_payload(), "isBuyer": False}]),
            _json_response(_exchange_info_payload()),
        ])
        client = _client(transport)
        client.restore_historical_fee_fills(
            symbol="ETHUSDT", client_order_id=client_order_id, exchange_order_id="42",
            fills=(historical_fill(),),
        )
        executions = client.list_account_executions_since(symbol="ETHUSDT", order_id="42")
        self.assertEqual(executions[0].result.client_order_id, client_order_id)
        self.assertEqual(executions[0].result.fills, (historical_fill(),))
        self.assertEqual(len(transport.requests), 4)

    def test_historical_seed_rejects_duplicates_conflicts_and_wrong_orders(self) -> None:
        """
        함수 이름: test_historical_seed_rejects_duplicates_conflicts_and_wrong_orders()
        기능: 중복·다른 주문·변조된 과거 근거를 등록 단계에서 차단한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        transport = QueueHTTPTransport([])
        client = _client(transport)
        original = historical_fill()
        for fills in ((), (original, original), (replace(original, exchange_order_id="43"),)):
            with self.subTest(fills=fills), self.assertRaises(ValueError):
                restore_history(client, fills=fills)
        restore_history(client)
        with self.assertRaises(ValueError):
            restore_history(client, fills=(replace(original, quantity=Decimal("0.6")),))
        self.assertEqual(transport.requests, [])
