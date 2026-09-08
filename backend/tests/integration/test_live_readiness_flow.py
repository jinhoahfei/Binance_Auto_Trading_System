"""실제 live bootstrap/protocol과 memory HTTP·WS로 startup·restart·unknown 격리를 검증한다."""

from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from binance_auto_trader.adapters.binance.live_clients import BinanceLiveRESTClient, BinanceLiveWebSocketClient
from binance_auto_trader.bootstrap import close_application, start_application
from binance_auto_trader.bootstrap.live import create_live_application_runtime
from binance_auto_trader.bootstrap.live_configuration import LIVE_CREDENTIAL_NAMESPACE
from tests.unit.bootstrap.test_live_bootstrap import live_configuration
from tests.unit.bootstrap.test_testnet_configuration import _zero_commission_payload
from tests.unit.binance.test_spot_rest_client import _json_response
from tests.unit.binance.test_spot_websocket_client import _ScriptedSocketFactory, _execution_report


class MemoryLiveHTTP:
    """
    클래스 이름: MemoryLiveHTTP
    기능: 실제 URL·서명 경계를 통과한 read 요청에만 deterministic Spot payload를 반환한다.
    작성 날짜: 2026/09/08
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 현재 분의 중간 시각과 secret 없는 요청 목록을 초기화한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        self.timestamp = int(datetime.now(timezone.utc).timestamp() * 1000)
        self.requests = []  # Credential·signature는 이 fixture 기록에도 남기지 않는다.

    def request(self, *, method, url, headers, body, timeout_seconds, before_send=None):
        """
        함수 이름: request()
        기능: GET만 허용하고 startup이 필요한 네 주기·계좌·empty exchange 상태를 반환한다.
        인자: method/url/headers/body -> HTTP 요청, timeout_seconds -> 제한, before_send -> guard
        반환값: memory HTTP response
        작성 날짜: 2026/09/08
        """
        if before_send is not None:
            before_send()
        parsed = urlsplit(url)
        if method != "GET" or parsed.netloc != "api.binance.com":
            raise AssertionError("unexpected live mutation or endpoint")
        self.requests.append((method, parsed.path))
        # API protocol과 Controller를 mocking하지 않고 외부 응답만 결정론적으로 제공한다.
        if parsed.path.endswith("/time"):
            return _json_response({"serverTime": self.timestamp})
        if parsed.path.endswith("/klines"):
            query = parse_qs(parsed.query)
            duration = {"1m": 60000, "30m": 1800000, "4h": 14400000, "1d": 86400000}[query["interval"][0]]
            current_open = self.timestamp // duration * duration
            rows = []
            for index in range(int(query["limit"][0])):
                opened = current_open - (int(query["limit"][0]) - index - 1) * duration
                price = str(1900 + index % 7)
                rows.append([opened, price, str(1901 + index % 7), str(1899 + index % 7), price, "5", opened + duration - 1, "10000", 10, "2", "4000", "0"])
            return _json_response(rows)
        if parsed.path.endswith("/account"):
            return _json_response({"updateTime": self.timestamp, "canTrade": True, "canWithdraw": False, "canDeposit": True, "accountType": "SPOT", "balances": [{"asset": "ETH", "free": "0", "locked": "0"}, {"asset": "USDT", "free": "100", "locked": "0"}], "permissions": ["SPOT"]})
        if parsed.path.endswith("/commission"):
            return _json_response(_zero_commission_payload())
        if parsed.path.endswith(("/openOrders", "/allOrders", "/openOrderList")):
            return _json_response([])
        raise AssertionError("unexpected memory live GET path")


class LiveReadinessFlowTests(unittest.TestCase):
    """
    클래스 이름: LiveReadinessFlowTests
    기능: live runtime의 READY·fresh restart·unknown execution 차단을 production 경로로 검증한다.
    작성 날짜: 2026/09/08
    """

    def test_live_read_only_ready_restart_and_unknown_execution_fail_closed(self) -> None:
        """
        함수 이름: test_live_read_only_ready_restart_and_unknown_execution_fail_closed()
        기능: 실제 bootstrap이 두 번 READY가 되고 외부 execution을 받으면 주문 gate를 잠그는지 검사한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        transport = MemoryLiveHTTP()
        with TemporaryDirectory() as directory:
            history = Path(directory).resolve() / LIVE_CREDENTIAL_NAMESPACE / "trade-history.jsonl"
            history.parent.mkdir()
            # 각각 새 graph와 새 socket을 만들어 in-memory 상태 재사용 없는 restart를 재현한다.
            for iteration in range(2):
                socket_factory = _ScriptedSocketFactory()
                rest = BinanceLiveRESTClient("key", "secret", transport=transport)
                websocket = BinanceLiveWebSocketClient("key", "secret", socket_factory=socket_factory, timestamp_provider=rest.get_server_timestamp_milliseconds)
                with patch("binance_auto_trader.bootstrap.live.BinanceLiveRESTClient", return_value=rest), patch("binance_auto_trader.bootstrap.live.BinanceLiveWebSocketClient", return_value=websocket):
                    runtime = create_live_application_runtime(configuration=live_configuration(), history_path=history)
                try:
                    self.assertTrue(start_application(runtime).ready)
                    self.assertFalse(runtime.order_execution_enabled)
                    self.assertEqual(runtime.trading_controller._position.quantity, 0)
                    self.assertFalse(runtime.trading_controller.reconciliation_required)
                    if iteration == 1:
                        # 기존 application Order와 매칭되지 않는 executionReport는 조용히 버리면 안 된다.
                        observed = Event()
                        original = runtime.trading_controller.observe_order_result
                        def observe(result):
                            """
                            함수 이름: observe()
                            기능: 실제 Controller 처리 완료를 fixture Event로 동기화한다.
                            인자: result -> 정규화 주문 결과
                            반환값: production 처리 결과
                            작성 날짜: 2026/09/08
                            """
                            accepted = original(result)
                            observed.set()  # Worker sleep 추정 대신 실제 처리 완료를 기다린다.
                            return accepted
                        with patch.object(runtime.trading_controller, "observe_order_result", side_effect=observe):
                            socket_factory.sockets[1].emit(_execution_report(transaction_time=transport.timestamp + 1, execution_id=99, execution_type="NEW", status="NEW", cumulative_quantity="0", client_order_id="external-unknown"))
                            self.assertTrue(observed.wait(1))
                        self.assertTrue(runtime.trading_controller.external_execution_reconciliation_required)
                        self.assertTrue(runtime.trading_controller.reconciliation_required)
                finally:
                    close_application(runtime)  # Memory fixture에는 실제 exposure와 거래소 mutation이 없다.
        self.assertTrue(transport.requests)
        self.assertTrue(all(method == "GET" for method, _path in transport.requests))
