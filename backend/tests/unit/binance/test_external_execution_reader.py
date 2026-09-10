"""외부 체결 복구 reader의 원 주문·fill 검증과 페이지 완전성을 검사한다."""

from copy import deepcopy
from urllib.parse import parse_qs, urlsplit
import unittest

from binance_auto_trader.domain.trading.states import OrderSide
from tests.unit.binance.test_spot_rest_client import (
    FIXED_TIME_MILLISECONDS, QueueHTTPTransport, _client, _exchange_info_payload,
    _filled_order_payload, _json_response,
)


def manual_payloads():
    """
    함수 이름: manual_payloads()
    기능: 동일 수량·금액·방향으로 대조할 allOrders와 myTrades 원문 fixture를 만든다.
    인자: 없음
    반환값: 외부 주문과 체결 배열
    작성 날짜: 2026/09/10
    """
    order = _filled_order_payload(client_order_id="web-external-sell", quantity="1.234")
    order.pop("fills")
    order.update(side="SELL", time=FIXED_TIME_MILLISECONDS, updateTime=FIXED_TIME_MILLISECONDS)
    fills = [{"symbol": "ETHUSDT", "id": 701, "orderId": 42, "price": "100", "qty": "1.234", "quoteQty": "123.4", "commission": "0.1234", "commissionAsset": "USDT", "time": FIXED_TIME_MILLISECONDS, "isBuyer": False}]
    return order, fills


class ExternalExecutionReaderTests(unittest.TestCase):
    """
    클래스 이름: ExternalExecutionReaderTests
    기능: 서명 GET만 사용하는 복구 reader가 불완전하거나 서로 다른 사실을 거부하는지 검증한다.
    작성 날짜: 2026/09/10
    """

    def test_reader_preserves_external_identity_and_only_uses_get(self):
        """
        함수 이름: test_reader_preserves_external_identity_and_only_uses_get()
        기능: 날짜 생략 없는 orderId cursor·외부 방향·원 수량·원 fill ID를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        order, fills = manual_payloads()
        transport = QueueHTTPTransport([_json_response({"serverTime": FIXED_TIME_MILLISECONDS}), _json_response([order]), _json_response(fills), _json_response(_exchange_info_payload())])
        results = _client(transport).list_account_executions_since(symbol="ETHUSDT", order_id="40")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].side, OrderSide.SELL)
        self.assertEqual(results[0].result.client_order_id, "web-external-sell")
        self.assertEqual(results[0].result.fills[0].key, ("42", "701"))
        self.assertTrue(all(request["method"] == "GET" for request in transport.requests))
        query = parse_qs(urlsplit(transport.requests[1]["url"]).query)
        self.assertEqual(query["orderId"], ["40"])
        self.assertEqual(query["limit"], ["1000"])

    def test_reader_rejects_inconsistent_or_truncated_fill_evidence(self):
        """
        함수 이름: test_reader_rejects_inconsistent_or_truncated_fill_evidence()
        기능: 방향·ID·시각·합계·중복·fill 상한 누락을 안전한 복구로 수용하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        for change in ({"isBuyer": True}, {"orderId": 43}, {"symbol": "BTCUSDT"}, {"time": None}, {"qty": "1"}, {"time": FIXED_TIME_MILLISECONDS - 1}, {"time": FIXED_TIME_MILLISECONDS + 1}, "truncated", "duplicate"):
            with self.subTest(change=change):
                order, fills = manual_payloads()
                if change == "truncated":
                    fills = [dict(fills[0], id=index) for index in range(1000)]
                elif change == "duplicate":
                    fills = [fills[0], deepcopy(fills[0])]
                else:
                    fills[0].update(change)
                transport = QueueHTTPTransport([_json_response({"serverTime": FIXED_TIME_MILLISECONDS}), _json_response([order]), _json_response(fills), _json_response(_exchange_info_payload())])
                with self.assertRaises(ValueError):
                    _client(transport).list_account_executions_since(symbol="ETHUSDT", order_id="40")

    def test_reader_continues_full_page_and_rejects_regressing_cursor(self):
        """
        함수 이름: test_reader_continues_full_page_and_rejects_regressing_cursor()
        기능: 1000번째 이후 외부 주문을 빠뜨리지 않고 중복 페이지를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        empty_orders = [{"symbol": "ETHUSDT", "orderId": identifier, "executedQty": "0"} for identifier in range(1, 1001)]
        order, fills = manual_payloads()
        order["orderId"] = 1001
        fills[0]["orderId"] = 1001
        transport = QueueHTTPTransport([_json_response({"serverTime": FIXED_TIME_MILLISECONDS}), _json_response(empty_orders), _json_response([order]), _json_response(fills), _json_response(_exchange_info_payload())])
        results = _client(transport).list_account_executions_since(symbol="ETHUSDT", order_id="1")
        self.assertEqual(results[0].result.exchange_order_id, "1001")
        self.assertEqual(parse_qs(urlsplit(transport.requests[2]["url"]).query)["orderId"], ["1001"])
        transport = QueueHTTPTransport([_json_response({"serverTime": FIXED_TIME_MILLISECONDS}), _json_response(empty_orders), _json_response(empty_orders)])
        with self.assertRaises(ValueError):
            _client(transport).list_account_executions_since(symbol="ETHUSDT", order_id="1")


if __name__ == "__main__":
    unittest.main()
