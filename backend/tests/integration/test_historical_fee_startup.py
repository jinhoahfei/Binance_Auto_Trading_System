"""과거 BNB 수수료 v3·v4 장부의 실제 live lifecycle 재시작을 검증한다."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from binance_auto_trader.adapters.binance.live_clients import (
    BinanceLiveRESTClient,
    BinanceLiveWebSocketClient,
)
from binance_auto_trader.adapters.persistence import TradeHistoryRepository
from binance_auto_trader.application.external_exit_recovery import build_external_exit_trades
from binance_auto_trader.bootstrap import (
    ApplicationStartupError,
    StartupFailureCode,
    close_application,
    start_application,
)
from binance_auto_trader.bootstrap.live import create_live_application_runtime
from binance_auto_trader.bootstrap.live_configuration import LIVE_CREDENTIAL_NAMESPACE
from binance_auto_trader.domain.history.performance import Performance
from binance_auto_trader.domain.history.trade import Trade
from binance_auto_trader.domain.trading.account_execution import AccountExecution
from binance_auto_trader.domain.trading.order import OrderResult, OrderStatus
from binance_auto_trader.domain.trading.position import Position
from binance_auto_trader.domain.trading.states import OrderSide
from tests.integration.test_live_readiness_flow import MemoryLiveHTTP
from tests.unit.binance.test_spot_rest_client import _exchange_info_payload, _json_response
from tests.unit.binance.test_spot_websocket_client import _ScriptedSocketFactory
from tests.unit.bootstrap.test_live_bootstrap import live_configuration
from tests.unit.trading.test_bnb_fee_accounting import bnb_fill, mixed_execution, valuation_for
from tests.unit.trading.test_order import make_order


def historical_bnb_trades(sell_fee_asset: str = "BNB") -> tuple[Trade, Trade]:
    """
    함수 이름: historical_bnb_trades()
    기능: 밀리초 시각의 BNB 매수 v3와 선택한 수수료의 부분 수동 매도 v4를 만든다.
    인자: sell_fee_asset -> BNB, MIXED, USDT 또는 수수료 0인 ETH
    반환값: 실제 fill 근거를 보존하는 두 과거 Trade
    작성 날짜: 2026/09/22
    """
    original_buy_fill = bnb_fill()
    buy_fill = replace(
        original_buy_fill,
        executed_at=original_buy_fill.executed_at + timedelta(milliseconds=123),
    )
    order = make_order(
        requested_quantity=buy_fill.quantity,
        submitted_quantity=buy_fill.quantity,
    )
    order.client_order_id = "bat-historical-bnb-buy"
    order.apply_order_result(OrderResult(
        symbol="ETHUSDT",
        client_order_id=order.client_order_id,
        status=OrderStatus.FILLED,
        processed_at=buy_fill.executed_at,
        exchange_order_id=buy_fill.exchange_order_id,
        fills=(buy_fill,),
    ))
    summary = order.build_execution_summary()
    buy = Trade.from_order_execution(order, summary)
    position = Position()
    position.apply_execution(summary)

    sell_time = buy_fill.executed_at + timedelta(minutes=1)
    sell_fill = replace(
        buy_fill,
        exchange_order_id="1002",
        trade_id="12",
        quantity=Decimal("0.2"),
        price=Decimal("110"),
        executed_at=sell_time,
        fee_valuation=valuation_for(sell_time),
    )
    if sell_fee_asset == "MIXED":
        sell_fills = (
            replace(sell_fill, quantity=Decimal("0.1")),
            replace(
                sell_fill, trade_id="13", quantity=Decimal("0.1"),
                fee_amount=Decimal("0.011"), fee_asset="USDT",
                fee_quote_amount=Decimal("0.011"), fee_valuation=None,
                executed_at=sell_time + timedelta(milliseconds=1),
            ),
        )
    elif sell_fee_asset in ("USDT", "ETH"):
        # 외부 매도 복구는 양수 ETH 수수료를 허용하지 않으므로 0 비용 호환만 검사한다.
        fee_amount = Decimal("0.022") if sell_fee_asset == "USDT" else Decimal("0")
        sell_fills = (replace(
            sell_fill, fee_asset=sell_fee_asset, fee_amount=fee_amount,
            fee_quote_amount=fee_amount, fee_valuation=None,
        ),)
    elif sell_fee_asset == "BNB":
        sell_fills = (sell_fill,)
    else:
        raise ValueError("unsupported historical sell fee fixture")
    sell_result = OrderResult(
        symbol="ETHUSDT",
        client_order_id="web-historical-bnb-sell",
        status=OrderStatus.FILLED,
        processed_at=sell_fills[-1].executed_at,
        exchange_order_id="1002",
        fills=sell_fills,
    )
    execution = AccountExecution(OrderSide.SELL, sell_fill.quantity, sell_time, sell_result)
    sell = build_external_exit_trades(
        (buy,), position, (execution,),
        eth_balance=Decimal("0.3"), residual_quantity=Decimal("0"),
    )[0]
    return buy, sell


def timestamp_milliseconds(instant: datetime) -> int:
    """
    함수 이름: timestamp_milliseconds()
    기능: 체결 시각의 밀리초를 버리지 않고 공식 응답의 epoch 정수로 변환한다.
    인자: instant -> timezone-aware 체결 시각
    반환값: UTC epoch 밀리초
    작성 날짜: 2026/09/22
    """
    elapsed = instant - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (elapsed.days * 86400 + elapsed.seconds) * 1000 + elapsed.microseconds // 1000


class HistoricalFeeHTTP(MemoryLiveHTTP):
    """
    클래스 이름: HistoricalFeeHTTP
    기능: 현재 수수료 설정과 구분되는 과거 signed 주문·체결 원문을 memory로 제공한다.
    작성 날짜: 2026/09/22
    """

    def __init__(self, trades: tuple[Trade, ...], balance: Decimal) -> None:
        """
        함수 이름: __init__()
        기능: 독립 REST 인스턴스가 대조할 과거 이력과 현재 ETH 잔고를 고정한다.
        인자: trades -> 응답에 사용할 과거 거래, balance -> 남은 실제 ETH
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        super().__init__()
        self.trades = trades
        self.balance = balance
        self.fill_read_order_ids: list[str] = []

    def request(self, *, method, url, headers, body, timeout_seconds, before_send=None):
        """
        함수 이름: request()
        기능: 서명·정규화 production 경계를 통과할 account·주문·fill 응답만 제공한다.
        인자: method/url/headers/body -> 요청, timeout_seconds -> 제한, before_send -> guard
        반환값: 실제 네트워크를 사용하지 않는 HTTP 응답
        작성 날짜: 2026/09/22
        """
        parsed = urlsplit(url)
        if method != "GET" or parsed.netloc != "api.binance.com":
            raise AssertionError("historical startup must remain read-only")
        query = parse_qs(parsed.query)
        if "symbol" in query and query["symbol"] != ["ETHUSDT"]:
            raise AssertionError("historical fees must not query a new BNB price")
        path = parsed.path
        if not path.endswith(("/account", "/exchangeInfo", "/allOrders", "/myTrades")):
            return super().request(
                method=method, url=url, headers=headers, body=body,
                timeout_seconds=timeout_seconds, before_send=before_send,
            )
        if before_send is not None:
            before_send()
        self.requests.append((method, path))
        if path.endswith("/account"):
            return _json_response({
                "updateTime": self.timestamp, "canTrade": True, "accountType": "SPOT",
                "balances": [
                    {"asset": "ETH", "free": str(self.balance), "locked": "0"},
                    {"asset": "USDT", "free": "100", "locked": "0"},
                ],
            })
        if path.endswith("/exchangeInfo"):
            return _json_response(_exchange_info_payload())
        if path.endswith("/allOrders"):
            cursor = int(query.get("orderId", ["0"])[0])
            return _json_response([
                {
                    "symbol": trade.symbol, "orderId": int(trade.order_id),
                    "clientOrderId": trade.client_order_id, "status": "FILLED",
                    "side": trade.side.value, "type": "MARKET",
                    "origQty": str(trade.executed_quantity),
                    "executedQty": str(trade.executed_quantity),
                    "cummulativeQuoteQty": str(trade.executed_amount),
                    "time": timestamp_milliseconds(min(fill.executed_at for fill in trade.fee_fills)),
                    "updateTime": timestamp_milliseconds(trade.executed_at),
                }
                for trade in self.trades if int(trade.order_id) >= cursor
            ])
        order_id = query["orderId"][0]
        self.fill_read_order_ids.append(order_id)
        trade = next(trade for trade in self.trades if trade.order_id == order_id)
        return _json_response([
            {
                "symbol": trade.symbol, "id": int(fill.trade_id),
                "orderId": int(fill.exchange_order_id), "qty": str(fill.quantity),
                "price": str(fill.price), "commission": str(fill.fee_amount),
                "commissionAsset": fill.fee_asset,
                "time": timestamp_milliseconds(fill.executed_at),
                "isBuyer": trade.side is OrderSide.BUY,
            }
            for fill in trade.fee_fills
        ])


class HistoricalFeeStartupTests(unittest.TestCase):
    """
    클래스 이름: HistoricalFeeStartupTests
    기능: lifecycle이 v3·v4 근거를 REST 조회보다 먼저 복원해 재시작이 완료되는지 검사한다.
    작성 날짜: 2026/09/22
    """

    def test_saved_bnb_v3_and_v4_restart_without_new_valuation(self) -> None:
        """
        함수 이름: test_saved_bnb_v3_and_v4_restart_without_new_valuation()
        기능: 새 live graph 두 개가 저장 원문을 유지하며 BNB 비용과 열린 잔고를 재현한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        buy, sell = historical_bnb_trades()
        _, mixed_sell = historical_bnb_trades("MIXED")
        _, quote_sell = historical_bnb_trades("USDT")
        _, zero_base_sell = historical_bnb_trades("ETH")
        mixed_order, mixed_summary = mixed_execution()
        mixed_buy = replace(
            Trade.from_order_execution(mixed_order, mixed_summary),
            client_order_id="bat-historical-mixed-buy",
        )
        for trades, balance, cost in (
            ((buy,), Decimal("0.5"), Decimal("50.006")),
            ((mixed_buy,), Decimal("1.499"), Decimal("150.006")),
            ((buy, sell), Decimal("0.3"), Decimal("30.0036")),
            ((buy, mixed_sell), Decimal("0.3"), Decimal("30.0036")),
            ((buy, quote_sell), Decimal("0.3"), Decimal("30.0036")),
            ((buy, zero_base_sell), Decimal("0.3"), Decimal("30.0036")),
        ):
            with self.subTest(fees=tuple(trade.fee_asset for trade in trades)):
                with TemporaryDirectory() as directory:
                    history_path = Path(directory).resolve() / LIVE_CREDENTIAL_NAMESPACE / "trade-history.jsonl"
                    history_path.parent.mkdir()
                    repository = TradeHistoryRepository(history_path)
                    for trade in trades:
                        repository.save_this_trade_by_order_id(trade.order_id, trade)
                    original_history = history_path.read_bytes()
                    for _ in range(2):
                        transport = HistoricalFeeHTTP(trades, balance)
                        rest = BinanceLiveRESTClient("key", "secret", transport=transport)
                        websocket = BinanceLiveWebSocketClient(
                            "key", "secret", socket_factory=_ScriptedSocketFactory(),
                            timestamp_provider=rest.get_server_timestamp_milliseconds,
                        )
                        with (
                            patch("binance_auto_trader.bootstrap.live.BinanceLiveRESTClient", return_value=rest),
                            patch("binance_auto_trader.bootstrap.live.BinanceLiveWebSocketClient", return_value=websocket),
                        ):
                            runtime = create_live_application_runtime(
                                configuration=live_configuration(), history_path=history_path,
                            )
                        try:
                            self.assertTrue(start_application(runtime).ready)
                            self.assertFalse(runtime.order_execution_enabled)
                            self.assertFalse(runtime.trading_controller.reconciliation_required)
                            self.assertEqual(runtime.trading_controller._position.quantity, balance)
                            self.assertEqual(runtime.trading_controller._position.cost_basis, cost)
                            self.assertEqual(runtime.trade_history.trades, trades)
                            self.assertEqual(
                                runtime.trade_history_controller.performance.total_fee,
                                Performance(trades).total_fee,
                            )
                            # Startup 중 앱 v3와 외부 v4의 raw myTrades를 실제로 읽었는지 확인한다.
                            self.assertEqual(set(transport.fill_read_order_ids), {trade.order_id for trade in trades})
                            recent = runtime.api_gateway.list_all_recent_order_results("ETHUSDT")
                            self.assertEqual(
                                tuple(result.fills for result in recent),
                                tuple(trade.fee_fills for trade in trades),
                            )
                            subscription = runtime.trading_controller.reconnect_account_stream_after_reconciliation()
                            self.assertIsNotNone(subscription)
                            self.assertFalse(runtime.trading_controller.reconciliation_required)
                            self.assertEqual(runtime.trading_controller._position.quantity, balance)
                            self.assertEqual(runtime.trading_controller._position.cost_basis, cost)
                            self.assertEqual(history_path.read_bytes(), original_history)
                            self.assertTrue(all(method == "GET" for method, _ in transport.requests))
                        finally:
                            close_application(runtime)

    def test_external_fee_evidence_must_match_even_when_totals_are_unchanged(self) -> None:
        """
        함수 이름: test_external_fee_evidence_must_match_even_when_totals_are_unchanged()
        기능: v4 수수료 합계가 같아도 다른 원 체결이면 시작을 거부하고 저장 원문을 보존한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        for fee_asset in ("BNB", "MIXED", "USDT", "ETH"):
            with self.subTest(fee_asset=fee_asset), TemporaryDirectory() as directory:
                buy, sell = historical_bnb_trades(fee_asset)
                changed_fill = replace(sell.fee_fills[0], trade_id="99")
                changed_sell = replace(sell, fee_fills=(changed_fill, *sell.fee_fills[1:]))
                history_path = Path(directory).resolve() / LIVE_CREDENTIAL_NAMESPACE / "trade-history.jsonl"
                history_path.parent.mkdir()
                repository = TradeHistoryRepository(history_path)
                for trade in (buy, sell):
                    repository.save_this_trade_by_order_id(trade.order_id, trade)
                original_history = history_path.read_bytes()
                transport = HistoricalFeeHTTP((buy, changed_sell), Decimal("0.3"))
                rest = BinanceLiveRESTClient("key", "secret", transport=transport)
                websocket = BinanceLiveWebSocketClient(
                    "key", "secret", socket_factory=_ScriptedSocketFactory(),
                    timestamp_provider=rest.get_server_timestamp_milliseconds,
                )
                with (
                    patch("binance_auto_trader.bootstrap.live.BinanceLiveRESTClient", return_value=rest),
                    patch("binance_auto_trader.bootstrap.live.BinanceLiveWebSocketClient", return_value=websocket),
                ):
                    runtime = create_live_application_runtime(
                        configuration=live_configuration(), history_path=history_path,
                    )
                try:
                    with self.assertRaises(ApplicationStartupError) as captured:
                        start_application(runtime)
                    self.assertIs(
                        captured.exception.failure.code,
                        StartupFailureCode.ORDER_RECONCILIATION_FAILED,
                    )
                    self.assertFalse(runtime.state.ready)
                    self.assertEqual(history_path.read_bytes(), original_history)
                    self.assertTrue(all(method == "GET" for method, _ in transport.requests))
                finally:
                    close_application(runtime)
