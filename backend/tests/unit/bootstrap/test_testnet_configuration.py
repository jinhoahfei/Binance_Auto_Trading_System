"""Testnet backend 조립의 환경 opt-in, endpoint 고정과 secret 비노출을 검증한다."""

from decimal import Decimal
import importlib
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from unittest.mock import patch, sentinel
import unittest
import urllib.request

from binance_auto_trader.adapters.binance import (
    BinanceSpotRESTClient,
    BinanceSpotWebSocketClient,
)
from binance_auto_trader.bootstrap import create_application_runtime
from binance_auto_trader.bootstrap import testnet as testnet_module
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.trading.order import Order
from binance_auto_trader.domain.trading.states import OrderSide, StrategyType


API_KEY_CANARY = "testnet-api-key-canary"
API_SECRET_CANARY = "testnet-api-secret-canary"


def _read_only_environment() -> dict[str, str]:
    """
    함수 이름: _read_only_environment()
    기능: read-only testnet opt-in과 testnet credential을 가진 기본 환경을 만든다.
    인자: 없음
    반환값: 독립 mutable 환경 dictionary
    작성 날짜: 2026/08/22
    """
    return {
        testnet_module.BINANCE_RUN_TESTNET_ENV: "1",
        testnet_module.BINANCE_TESTNET_API_KEY_ENV: API_KEY_CANARY,
        testnet_module.BINANCE_TESTNET_API_SECRET_ENV: API_SECRET_CANARY,
    }  # 주문 flag와 notional이 없어 command gate는 read-only로 남는다.


class _FakeRESTClient:
    """
    클래스 이름: _FakeRESTClient
    기능: runtime 조립 전에 전달된 REST credential keyword만 기록한다.
    작성 날짜: 2026/08/22
    """

    calls: list[dict[str, object]] = []
    instances: list["_FakeRESTClient"] = []

    def __init__(self, **arguments: object) -> None:
        """
        함수 이름: __init__()
        기능: testnet factory가 전달한 생성자 인자를 class trace에 보존한다.
        인자: arguments -> REST client keyword 인자
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        type(self).calls.append(arguments)  # network operation 없이 조립 인자만 관찰한다.
        type(self).instances.append(self)

    def get_server_timestamp_milliseconds(self) -> int:
        """
        함수 이름: get_server_timestamp_milliseconds()
        기능: WS timestamp provider identity 검증에 사용할 고정 server millisecond를 반환한다.
        인자: 없음
        반환값: 고정 millisecond 정수
        작성 날짜: 2026/08/22
        """
        return 1_777_000_000_000  # unit 조립 test에서는 외부 server를 조회하지 않는다.

    def get_account(self) -> object:
        """
        함수 이름: get_account()
        기능: order permission proxy 조립 검증용 빈 account payload를 반환한다.
        인자: 없음
        반환값: 빈 mapping
        작성 날짜: 2026/08/22
        """
        return {}  # factory unit test는 실제 Gateway account parsing을 호출하지 않는다.


class _FakeWebSocketClient:
    """
    클래스 이름: _FakeWebSocketClient
    기능: runtime 조립 전에 전달된 WebSocket credential keyword만 기록한다.
    작성 날짜: 2026/08/22
    """

    calls: list[dict[str, object]] = []

    def __init__(self, **arguments: object) -> None:
        """
        함수 이름: __init__()
        기능: testnet factory가 전달한 생성자 인자를 class trace에 보존한다.
        인자: arguments -> WebSocket client keyword 인자
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        type(self).calls.append(arguments)  # 실제 socket 생성 없이 credential 전달만 기록한다.


class TestnetConfigurationTests(unittest.TestCase):
    """
    클래스 이름: TestnetConfigurationTests
    기능: read-only 기본값, 주문 이중 gate와 고정 testnet 조립을 검증한다.
    작성 날짜: 2026/08/22
    """

    def test_actual_testnet_clients_cannot_be_smuggled_through_fake_mode(
        self,
    ) -> None:
        """
        함수 이름: test_actual_testnet_clients_cannot_be_smuggled_through_fake_mode()
        기능: 실제 network client가 generic fake mode로 opt-in·cap·journal gate를 우회하지 못하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        # Client 생성자는 network를 열지 않으므로 canary credential로 조립 경계만 재현한다.
        rest_client = BinanceSpotRESTClient(
            api_key=API_KEY_CANARY,
            secret_key=API_SECRET_CANARY,
        )
        web_socket_client = BinanceSpotWebSocketClient(
            api_key=API_KEY_CANARY,
            api_secret=API_SECRET_CANARY,
        )
        with TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "history.jsonl"

            with self.assertRaisesRegex(
                ValueError,
                "dedicated in-process fake",
            ):
                create_application_runtime(
                    rest_client,
                    web_socket_client,
                    history_path=history_path,
                    execution_mode="fake",
                )

    def setUp(self) -> None:
        """
        함수 이름: setUp()
        기능: 각 test 전에 fake client 생성 trace를 비운다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        _FakeRESTClient.calls = []
        _FakeRESTClient.instances = []
        _FakeWebSocketClient.calls = []  # 이전 test credential trace를 다음 assertion과 분리한다.

    def test_import_does_not_open_network_connections(self) -> None:
        """
        함수 이름: test_import_does_not_open_network_connections()
        기능: testnet module reload가 HTTP 또는 socket connection을 만들지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # network primitive를 차단한 상태에서 module body의 import-time side effect를 관찰한다.
        with patch.object(socket, "create_connection") as socket_connect, patch.object(
            urllib.request,
            "urlopen",
        ) as http_open:
            importlib.reload(testnet_module)

        socket_connect.assert_not_called()
        http_open.assert_not_called()  # 환경 load와 client 생성도 explicit factory 호출까지 지연된다.

    def test_actual_client_runtime_construction_stays_offline(self) -> None:
        """
        함수 이름: test_actual_client_runtime_construction_stays_offline()
        기능: dedicated REST·WS client의 실제 생성과 runtime 조립이 network를 열지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        environment = _read_only_environment()

        # 실제 class import와 constructor까지 허용하되 모든 connection primitive 호출은 감시한다.
        with TemporaryDirectory() as temporary_directory, patch.object(
            socket,
            "create_connection",
        ) as socket_connect, patch.object(
            urllib.request,
            "urlopen",
        ) as http_open:
            runtime = testnet_module.create_testnet_application_runtime(
                history_path=Path(temporary_directory) / "history.jsonl",
                environment=environment,
            )
            blocked_order = Order(
                intent_id="offline-test-intent",
                client_order_id="bat-offline-read-only",
                submission_attempt=0,
                symbol="ETHUSDT",
                side=OrderSide.BUY,
                strategy=StrategyType.CASE_B,
                regime_type=RegimeType.TYPE_0,
                requested_quantity=Decimal("0.01"),
                submitted_quantity=Decimal("0.01"),
                market_price_at_decision=Decimal("100"),
            )
            with self.assertRaises(testnet_module.TestnetConfigurationError):
                runtime.api_gateway.prepare_order(blocked_order)

            # Proxy는 명시한 read/query port 밖의 raw request와 credential attribute를 전달하지 않는다.
            permission_client = runtime.api_gateway._rest_client
            for forbidden_attribute in (
                "_perform_request",
                "_api_key",
                "_secret_key_bytes",
            ):
                with self.subTest(
                    forbidden_attribute=forbidden_attribute,
                ):
                    with self.assertRaises(AttributeError):
                        getattr(permission_client, forbidden_attribute)

        socket_connect.assert_not_called()
        http_open.assert_not_called()
        runtime_representation = repr(runtime)
        self.assertNotIn(API_KEY_CANARY, runtime_representation)
        self.assertNotIn(API_SECRET_CANARY, runtime_representation)

    def test_read_only_requires_exact_opt_in_and_both_credentials(self) -> None:
        """
        함수 이름: test_read_only_requires_exact_opt_in_and_both_credentials()
        기능: testnet flag와 key pair 중 하나라도 없으면 값 노출 없이 fail closed하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        invalid_environments = (
            {},
            {
                testnet_module.BINANCE_RUN_TESTNET_ENV: "true",
                testnet_module.BINANCE_TESTNET_API_KEY_ENV: API_KEY_CANARY,
                testnet_module.BINANCE_TESTNET_API_SECRET_ENV: API_SECRET_CANARY,
            },
            {
                testnet_module.BINANCE_RUN_TESTNET_ENV: "1",
                testnet_module.BINANCE_TESTNET_API_SECRET_ENV: API_SECRET_CANARY,
            },
            {
                testnet_module.BINANCE_RUN_TESTNET_ENV: "1",
                testnet_module.BINANCE_TESTNET_API_KEY_ENV: API_KEY_CANARY,
            },
        )

        # 각 오류는 안전한 환경변수 이름만 설명하고 다른 credential canary를 복사하지 않는다.
        for environment in invalid_environments:
            with self.subTest(environment_keys=frozenset(environment)):
                with self.assertRaises(
                    testnet_module.TestnetConfigurationError
                ) as raised:
                    testnet_module.load_testnet_configuration(environment)
                self.assertNotIn(API_KEY_CANARY, str(raised.exception))
                self.assertNotIn(API_SECRET_CANARY, str(raised.exception))

    def test_order_permission_requires_second_flag_and_positive_cap(self) -> None:
        """
        함수 이름: test_order_permission_requires_second_flag_and_positive_cap()
        기능: 주문 권한이 별도 exact flag와 finite positive notional을 모두 요구하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        read_only_configuration = testnet_module.load_testnet_configuration(
            _read_only_environment()
        )
        self.assertFalse(read_only_configuration.allow_testnet_orders)
        self.assertIsNone(read_only_configuration.max_notional)
        with self.assertRaises(testnet_module.TestnetConfigurationError):
            testnet_module.require_testnet_order_permission(
                read_only_configuration
            )

        # order flag가 exact 1이어도 cap 누락·0·비유한 값이면 runtime 권한을 만들지 않는다.
        for invalid_cap in (None, "0", "-1", "NaN", "Infinity", "not-a-number"):
            environment = _read_only_environment()
            environment[testnet_module.BINANCE_RUN_TESTNET_ORDERS_ENV] = "1"
            if invalid_cap is not None:
                environment[
                    testnet_module.BINANCE_TESTNET_MAX_NOTIONAL_ENV
                ] = invalid_cap
            with self.subTest(invalid_cap=invalid_cap):
                with self.assertRaises(testnet_module.TestnetConfigurationError):
                    testnet_module.load_testnet_configuration(environment)

        enabled_environment = _read_only_environment()
        enabled_environment[
            testnet_module.BINANCE_RUN_TESTNET_ORDERS_ENV
        ] = "1"
        enabled_environment[
            testnet_module.BINANCE_TESTNET_MAX_NOTIONAL_ENV
        ] = "12.50"
        enabled_configuration = testnet_module.load_testnet_configuration(
            enabled_environment
        )
        self.assertTrue(enabled_configuration.allow_testnet_orders)
        self.assertEqual(
            testnet_module.require_testnet_order_permission(
                enabled_configuration
            ),
            Decimal("12.50"),
        )

    def test_configuration_repr_and_errors_never_expose_credentials(self) -> None:
        """
        함수 이름: test_configuration_repr_and_errors_never_expose_credentials()
        기능: 정상 repr와 malformed credential 오류가 key·secret 원문을 노출하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        configuration = testnet_module.load_testnet_configuration(
            _read_only_environment()
        )
        configuration_representation = repr(configuration)
        self.assertIn("<redacted>", configuration_representation)
        self.assertNotIn(API_KEY_CANARY, configuration_representation)
        self.assertNotIn(API_SECRET_CANARY, configuration_representation)

        malformed_environment = _read_only_environment()
        malformed_environment[
            testnet_module.BINANCE_TESTNET_API_SECRET_ENV
        ] = f" {API_SECRET_CANARY}"
        with self.assertRaises(
            testnet_module.TestnetConfigurationError
        ) as raised:
            testnet_module.load_testnet_configuration(malformed_environment)
        self.assertNotIn(API_SECRET_CANARY, str(raised.exception))

    def test_runtime_uses_testnet_mode_and_ignores_all_base_url_environment(
        self,
    ) -> None:
        """
        함수 이름: test_runtime_uses_testnet_mode_and_ignores_all_base_url_environment()
        기능: hostile live URL 환경이 client 또는 runtime 조립 인자로 전달되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        environment = _read_only_environment()
        environment.update(
            {
                "BINANCE_BASE_URL": "https://api.binance.com",
                "BINANCE_REST_BASE_URL": "https://api.binance.com",
                "BINANCE_WEBSOCKET_URL": "wss://stream.binance.com/ws",
                testnet_module.BINANCE_RUN_TESTNET_ORDERS_ENV: "1",
                testnet_module.BINANCE_TESTNET_MAX_NOTIONAL_ENV: "12.50",
            }
        )

        # dedicated client class에는 credential만 주고 endpoint override surface를 만들지 않는다.
        with patch.object(
            testnet_module,
            "_load_testnet_client_types",
            return_value=(_FakeRESTClient, _FakeWebSocketClient),
        ), patch.object(
            testnet_module,
            "create_application_runtime",
            return_value=sentinel.runtime,
        ) as runtime_factory:
            runtime = testnet_module.create_testnet_application_runtime(
                history_path=Path("test-history.jsonl"),
                environment=environment,
                kline_limit=17,
            )

        self.assertIs(runtime, sentinel.runtime)
        self.assertEqual(
            _FakeRESTClient.calls,
            [
                {
                    "api_key": API_KEY_CANARY,
                    "secret_key": API_SECRET_CANARY,
                    "maximum_order_notional": Decimal("12.50"),
                }
            ],
        )
        self.assertEqual(len(_FakeWebSocketClient.calls), 1)
        web_socket_arguments = dict(_FakeWebSocketClient.calls[0])
        timestamp_provider = web_socket_arguments.pop("timestamp_provider")
        self.assertEqual(
            web_socket_arguments,
            {
                "api_key": API_KEY_CANARY,
                "api_secret": API_SECRET_CANARY,
            },
        )
        self.assertIs(timestamp_provider.__self__, _FakeRESTClient.instances[0])
        self.assertEqual(timestamp_provider(), 1_777_000_000_000)
        runtime_keywords = runtime_factory.call_args.kwargs
        self.assertEqual(runtime_keywords["execution_mode"], "testnet")
        self.assertTrue(runtime_keywords["allow_testnet_orders"])
        self.assertEqual(
            runtime_keywords["testnet_maximum_order_notional"],
            Decimal("12.50"),
        )
        self.assertIsNotNone(
            runtime_keywords["_testnet_order_capability"]
        )  # 실제 opaque identity는 production module 밖의 값 비교로 노출하지 않는다.
        self.assertEqual(runtime_keywords["kline_limit"], 17)

        fixed_endpoints = (
            testnet_module.BINANCE_SPOT_TESTNET_REST_ORIGIN,
            testnet_module.BINANCE_SPOT_TESTNET_STREAM_ORIGIN,
            testnet_module.BINANCE_SPOT_TESTNET_WEBSOCKET_API_URL,
        )
        self.assertTrue(
            all("testnet.binance.vision" in endpoint for endpoint in fixed_endpoints)
        )  # production hostname은 조립 module의 endpoint 상수에 존재하지 않는다.


if __name__ == "__main__":
    unittest.main()
