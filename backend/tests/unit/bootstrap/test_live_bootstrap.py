"""Memory HTTP/WS에서 live fixed endpoint·격리·cap·read-only 전송 방벽을 검증한다."""

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from binance_auto_trader.adapters.binance.live_clients import (
    BinanceLiveRESTClient, BinanceLiveWebSocketClient, _LiveHTTPTransport, _LIVE_REST_ORDER_CAPABILITY,
)
from binance_auto_trader.adapters.binance.live_endpoints import LIVE_REST_BASE_URL, LIVE_WEBSOCKET_API_URL
from binance_auto_trader.adapters.binance.spot_rest_client import BinanceSpotRESTClient, BinanceAPIError
from binance_auto_trader.bootstrap.application import (
    create_application_runtime, _LIVE_ORDER_CAPABILITY, _TESTNET_ORDER_CAPABILITY,
)
from binance_auto_trader.bootstrap.live import create_live_application_runtime
from binance_auto_trader.bootstrap.live_configuration import (
    LiveConfiguration, LiveConfigurationError, LIVE_CREDENTIAL_NAMESPACE,
    create_live_risk_policy, validate_live_history_path,
)
from binance_auto_trader.bootstrap.live_permission import LiveOrderPermissionRESTClient
from binance_auto_trader.bootstrap.sidecar import parse_sidecar_configuration_payload
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.trading.order import Order
from binance_auto_trader.domain.trading.states import OrderSide, StrategyType
from binance_auto_trader.transport import SCHEMA_VERSION
from tests.unit.binance.test_spot_rest_client import _json_response, QueueHTTPTransport
from tests.unit.binance.test_spot_websocket_client import _ScriptedSocketFactory
from tests.unit.bootstrap.test_testnet_configuration import _zero_commission_payload


def live_configuration(*, orders: bool = False) -> LiveConfiguration:
    """
    함수 이름: live_configuration()
    기능: 실제 credential 없는 검증된 live 설정 fixture를 만든다.
    인자: orders -> 별도 주문 gate 테스트 여부
    반환값: live canary 설정
    작성 날짜: 2026/09/08
    """
    # Canary는 memory transport에만 전달하며 production credential 저장소를 읽지 않는다.
    return LiveConfiguration(
        "live-key-canary", "live-secret-canary", enabled=True, confirmation="LIVE",
        allow_live_orders=orders, max_notional=Decimal("10") if orders else None,
    )


def spot_commission_payload() -> dict[str, object]:
    """
    함수 이름: spot_commission_payload()
    기능: BNB 납부가 꺼진 편도 0.1% 현물 계정 응답을 만든다.
    인자: 없음
    반환값: 공식 account commission 형식의 독립 dictionary
    작성 날짜: 2026/09/22
    """
    payload = _zero_commission_payload()
    payload["standardCommission"].update(maker="0.001", taker="0.001")
    payload["discount"] = {
        "enabledForAccount": False,
        "enabledForSymbol": True,
        "discountAsset": "BNB",
        "discount": "0.25",
    }
    return payload  # 비활성 discount metadata에 BNB가 남아 있어도 납부하지 않는다.


def live_order(*, version: int = 1, quantity: str = "0.004") -> Order:
    """
    함수 이름: live_order()
    기능: 10 USDT 이하의 canonical live policy 주문 fixture를 만든다.
    인자: version -> 정책 version, quantity -> 제출 수량
    반환값: 테스트용 BUY Order
    작성 날짜: 2026/09/08
    """
    return Order(
        intent_id="live-fixture", client_order_id="bat-live-fixture", submission_attempt=0,
        symbol="ETHUSDT", side=OrderSide.BUY, strategy=StrategyType.CASE_B,
        regime_type=RegimeType.TYPE_0, requested_quantity=Decimal("1"),
        submitted_quantity=Decimal(quantity), market_price_at_decision=Decimal("2000"),
        risk_policy_version=version,
    )  # Signal injection은 test module 밖에 존재하지 않는다.


class LiveBootstrapTests(unittest.TestCase):
    """
    클래스 이름: LiveBootstrapTests
    기능: live root의 positive/negative 구성과 실제 protocol 전송 경계를 검사한다.
    작성 날짜: 2026/09/08
    """

    def test_disabled_default_and_bad_configuration_never_create_clients(self) -> None:
        """
        함수 이름: test_disabled_default_and_bad_configuration_never_create_clients()
        기능: disabled·namespace·truthy·확인·cap·version 위반을 network 조립 전에 막는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        with patch("binance_auto_trader.bootstrap.live.BinanceLiveRESTClient") as client:
            with self.assertRaises(LiveConfigurationError):
                create_live_application_runtime(configuration=LiveConfiguration("key", "secret"), history_path="/tmp/wrong")
            client.assert_not_called()
        # 잘못된 필드 하나씩 독립적으로 바꿔 암묵적 보정을 허용하지 않는다.
        for changes in (
            {"credential_namespace": "com.binance-auto.trader.testnet"}, {"enabled": 1},
            {"confirmation": "live"}, {"allow_live_orders": "1"}, {"policy_version": True},
            {"policy_version": 2}, {"allow_live_orders": True}, {"max_notional": Decimal("10")},
            {"allow_live_orders": True, "max_notional": Decimal("11")},
        ):
            with self.subTest(changes=changes), self.assertRaises(LiveConfigurationError):
                replace(live_configuration(), **changes)

    def test_history_namespace_rejects_testnet_symlink_and_hardlink(self) -> None:
        """
        함수 이름: test_history_namespace_rejects_testnet_symlink_and_hardlink()
        기능: live 저장소가 Testnet 파일·symlink·hardlink를 사용할 수 없는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        import os

        # 별도 namespace라도 실제 inode를 공유하면 저장 전 거부해야 한다.
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            history = root / LIVE_CREDENTIAL_NAMESPACE / "trade-history.jsonl"
            history.parent.mkdir()
            self.assertEqual(validate_live_history_path(history), history)
            original = root / "history.jsonl"
            original.write_text("")
            for link in (os.symlink, os.link):
                link(original, history)
                with self.assertRaises(LiveConfigurationError):
                    validate_live_history_path(history)
                history.unlink()
            with self.assertRaises(LiveConfigurationError):
                validate_live_history_path(original)

    def test_exact_signed_live_http_and_no_redirect_fallback(self) -> None:
        """
        함수 이름: test_exact_signed_live_http_and_no_redirect_fallback()
        기능: signed GET이 live origin만 사용하고 302를 다른 endpoint로 재전송하지 않게 한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        transport = QueueHTTPTransport([
            _json_response({"serverTime": 1788825600000}), _json_response({"canTrade": True}),
            _json_response({}, status_code=302, headers={"Location": "https://testnet.binance.vision/api/v3/account"}),
        ])
        client = BinanceLiveRESTClient("key", "secret", transport=transport)
        self.assertTrue(client.get_account()["canTrade"])
        with self.assertRaises(BinanceAPIError):
            client.get_account()
        self.assertEqual(len(transport.requests), 3)
        for request in transport.requests:
            self.assertTrue(request["url"].startswith(LIVE_REST_BASE_URL + "/v3/"))
        self.assertIn("signature=", transport.requests[1]["url"])
        with self.assertRaises(ValueError):
            BinanceSpotRESTClient("key", "secret", base_url=LIVE_REST_BASE_URL)
        with self.assertRaises(TypeError):
            BinanceLiveRESTClient("key", "secret", base_url="https://testnet.binance.vision/api")

    def test_read_only_transport_denies_all_mutation_and_wrong_hosts(self) -> None:
        """
        함수 이름: test_read_only_transport_denies_all_mutation_and_wrong_hosts()
        기능: permission proxy를 우회한 요청도 HTTP seam 진입 전에 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        delegate = Mock()
        transport = _LiveHTTPTransport(delegate, order_capability=None)
        # 임의 method·URL·유사 host·SAPI는 모두 전송 0회여야 한다.
        for method, url in (
            ("POST", LIVE_REST_BASE_URL + "/v3/order"), ("DELETE", LIVE_REST_BASE_URL + "/v3/order"),
            ("GET", "https://testnet.binance.vision/api/v3/account"),
            ("GET", "https://api.binance.com.evil/api/v3/account"),
            ("GET", "https://api.binance.com/sapi/v1/account/apiRestrictions"),
        ):
            with self.subTest(method=method, url=url), self.assertRaises(ValueError):
                transport.request(method=method, url=url, headers={}, body=None, timeout_seconds=1)
        delegate.request.assert_not_called()

    def test_memory_websockets_use_exact_live_pair(self) -> None:
        """
        함수 이름: test_memory_websockets_use_exact_live_pair()
        기능: market·signed account WS가 fixed live 주소만 사용하고 주문 method를 보내지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        factory = _ScriptedSocketFactory()
        client = BinanceLiveWebSocketClient("key", "secret", socket_factory=factory)
        market = client.subscribe_all_kline_streams(symbol="ETHUSDT", intervals=("1m",), on_message=Mock(), on_disconnect=Mock())
        account = client.subscribe_account_info(on_message=Mock(), on_disconnect=Mock())
        try:
            self.assertEqual(factory.sockets[0].url, "wss://stream.binance.com:443/stream?streams=ethusdt@kline_1m")
            self.assertEqual(factory.sockets[1].url, LIVE_WEBSOCKET_API_URL)
            self.assertEqual(json.loads(factory.sockets[1].sent_payloads[0])["method"], "userDataStream.subscribe.signature")
        finally:
            market.close()
            account.close()  # Memory worker도 test 이후 남기지 않는다.

    def test_rest_cap_requires_separate_live_capability(self) -> None:
        """
        함수 이름: test_rest_cap_requires_separate_live_capability()
        기능: Testnet 표식·cap 누락·cap 초과를 live HTTP 조립 전에 막는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        for capability, cap in ((_TESTNET_ORDER_CAPABILITY, None), (_LIVE_REST_ORDER_CAPABILITY, None), (_LIVE_REST_ORDER_CAPABILITY, Decimal("11")), (None, Decimal("10"))):
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                BinanceLiveRESTClient("key", "secret", order_capability=capability, maximum_order_notional=cap)
        BinanceLiveRESTClient("key", "secret", order_capability=_LIVE_REST_ORDER_CAPABILITY, maximum_order_notional=Decimal("10"))

    def test_permission_version_cap_and_read_only_block_before_delegate(self) -> None:
        """
        함수 이름: test_permission_version_cap_and_read_only_block_before_delegate()
        기능: prepare·submit·cancel의 read-only와 version·cap 위반을 모두 delegate 전에 차단한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        delegate = Mock()
        for configuration, order in ((live_configuration(), live_order()), (live_configuration(orders=True), live_order(version=2)), (live_configuration(orders=True), live_order(quantity="0.006"))):
            proxy = LiveOrderPermissionRESTClient(delegate, configuration)
            for method in (proxy.prepare_order, proxy.submit_order, proxy.cancel_order):
                with self.assertRaises(LiveConfigurationError):
                    method(order=order)
        # Mutable Order가 생성 후 bool version으로 바뀌어도 int 1로 취급하지 않는다.
        mutated_order = live_order()
        mutated_order.risk_policy_version = True
        with self.assertRaises(LiveConfigurationError):
            LiveOrderPermissionRESTClient(delegate, live_configuration(orders=True)).submit_order(order=mutated_order)
        self.assertEqual(delegate.mock_calls, [])  # 잘못된 gate는 public filter 조회도 하지 않는다.

    def test_permission_rechecks_prepared_cap_and_commission(self) -> None:
        """
        함수 이름: test_permission_rechecks_prepared_cap_and_commission()
        기능: filter 뒤 수량 상승과 base-asset 수수료 위험을 live 제출 전에 차단한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        delegate = Mock()
        delegate.get_account_commission.return_value = spot_commission_payload()
        proxy = LiveOrderPermissionRESTClient(
            delegate, live_configuration(orders=True), base_fee_residual_enabled=True
        )
        order = live_order()
        delegate.prepare_order.return_value = order
        self.assertIs(proxy.prepare_order(order=order), order)
        # Filter가 final quantity를 cap 밖으로 바꾸는 결함을 별도로 주입한다.
        def exceed_cap(*, order: Order) -> Order:
            """
            함수 이름: exceed_cap()
            기능: 반환 직전의 잘못된 filter 수량을 주입한다.
            인자: order -> test 주문
            반환값: 변조된 test 주문
            작성 날짜: 2026/09/08
            """
            order.submitted_quantity = Decimal("0.006")  # Test-only fault injection이다.
            return order
        delegate.prepare_order.side_effect = exceed_cap
        with self.assertRaises(LiveConfigurationError):
            proxy.prepare_order(order=live_order())
        delegate.submit_order.assert_not_called()

    def test_root_binds_controller_cap_policy_and_recovery(self) -> None:
        """
        함수 이름: test_root_binds_controller_cap_policy_and_recovery()
        기능: live root가 read-only와 order 설정을 Controller·REST에 일관되게 결속한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        with TemporaryDirectory() as directory:
            history = Path(directory).resolve() / LIVE_CREDENTIAL_NAMESPACE / "trade-history.jsonl"
            for orders in (False, True):
                runtime = create_live_application_runtime(configuration=live_configuration(orders=orders), history_path=history)
                self.assertEqual(runtime.order_execution_enabled, orders)
                self.assertEqual(runtime.execution_mode.value, "live")
                self.assertTrue(runtime.trading_controller._pending_order_recovery_enabled)
                self.assertEqual(runtime.trading_controller._risk_policy_state, create_live_risk_policy())
                self.assertEqual(runtime.trading_controller._maximum_order_notional, Decimal("10") if orders else None)

    def test_generic_live_rejects_capability_mismatch_and_unbound_policy(self) -> None:
        """
        함수 이름: test_generic_live_rejects_capability_mismatch_and_unbound_policy()
        기능: generic root가 Testnet 표식·미설정 cap·policy drift로 live 주문을 열지 못한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        for fields in (
            {"_testnet_order_capability": _TESTNET_ORDER_CAPABILITY},
            {"_live_order_capability": object()}, {"_live_order_capability": _LIVE_ORDER_CAPABILITY},
            {"_live_order_capability": _LIVE_ORDER_CAPABILITY, "live_maximum_order_notional": Decimal("10"), "risk_policy_state": replace(create_live_risk_policy(), version=2)},
        ):
            with self.subTest(fields=tuple(fields)), self.assertRaises(ValueError):
                create_application_runtime(Mock(), Mock(), execution_mode="live", **fields)

    def test_native_live_wire_is_complete_secret_safe_and_has_no_testnet_fallback(self) -> None:
        """
        함수 이름: test_native_live_wire_is_complete_secret_safe_and_has_no_testnet_fallback()
        기능: native live wire를 strict parse하고 누락·혼용·unknown URL 입력을 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        wire = {
            "schema_version": SCHEMA_VERSION, "allowed_origin": "tauri://localhost",
            "history_path": "/private/tmp/" + LIVE_CREDENTIAL_NAMESPACE + "/trade-history.jsonl",
            "api_key": "live-key-canary", "api_secret": "live-secret-canary",
            "allow_testnet_orders": False, "max_notional": None,
            "execution_mode": "live", "credential_namespace": LIVE_CREDENTIAL_NAMESPACE,
            "allow_live_orders": False, "live_confirmation": "LIVE", "policy_version": 1,
        }
        configuration = parse_sidecar_configuration_payload(json.dumps(wire).encode(), expected_schema_version=SCHEMA_VERSION)
        self.assertNotIn("live-secret-canary", repr(configuration))
        with self.assertRaises(ValueError):
            configuration.to_testnet_environment()
        for changes in ({"base_url": LIVE_REST_BASE_URL}, {"allow_testnet_orders": True}, {"credential_namespace": "com.binance-auto.trader.testnet"}, {"max_notional": "10"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                parse_sidecar_configuration_payload(json.dumps(wire | changes).encode(), expected_schema_version=SCHEMA_VERSION)
