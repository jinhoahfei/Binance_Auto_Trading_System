"""Testnet backend 조립의 환경 opt-in, endpoint 고정과 secret 비노출을 검증한다."""

from datetime import datetime, timezone
from decimal import Decimal
import importlib
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest.mock import Mock, patch, sentinel
import unittest
import urllib.request

from binance_auto_trader.adapters.binance import (
    APIGateway,
    BinanceSpotRESTClient,
    BinanceSpotWebSocketClient,
)
from binance_auto_trader.adapters.binance.mappers import (
    AccountAssetFilter,
    AccountRelevantFilters,
    NotionalFilter,
    OrderPreparationFilterEvidence,
    OrderSubmissionAttemptEvidence,
    QuantityFilter,
    ReferencePrice,
    SymbolTradingRules,
)
from binance_auto_trader.bootstrap import create_application_runtime
from binance_auto_trader.bootstrap import testnet as testnet_module
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.trading import RiskPolicyUnavailable
from binance_auto_trader.domain.trading.order import Order
from binance_auto_trader.domain.trading.states import (
    ExitReason,
    OrderSide,
    StrategyType,
)


API_KEY_CANARY = "testnet-api-key-canary"
API_SECRET_CANARY = "testnet-api-secret-canary"


def _empty_account_relevant_filters() -> AccountRelevantFilters:
    """
    함수 이름: _empty_account_relevant_filters()
    기능: Evidence 단위 테스트용 ETHUSDT signed-filter empty DTO를 만든다.
    인자: 없음
    반환값: 세 scope가 모두 비어 있는 AccountRelevantFilters
    작성 날짜: 2026/08/31
    """
    # Raw myFilters mapping 대신 exact frozen DTO를 evidence fixture에 직접 제공한다.
    return AccountRelevantFilters(
        symbol="ETHUSDT",
        exchange_order_count_filters=(),
        symbol_order_count_filters=(),
        symbol_quantity_filters=(),
        symbol_notional_filters=(),
        symbol_maximum_position=None,
        passive_symbol_filter_types=frozenset(),
        asset_filters=(),
    )


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


def _zero_commission_payload(symbol: str = "ETHUSDT") -> dict[str, object]:
    """
    함수 이름: _zero_commission_payload()
    기능: Testnet prepare cap 테스트용 지원 자산·0 수수료 응답을 만든다.
    인자: symbol -> 응답에 결속할 canonical Spot symbol
    반환값: 공식 account commission 형태의 독립 dictionary
    작성 날짜: 2026/08/31
    """
    # 모든 수수료가 0이고 할인 자산이 없게 해 cap/provenance 경계만 독립적으로 관찰한다.
    return {
        "symbol": symbol,
        "standardCommission": {
            "maker": "0.00000000",
            "taker": "0.00000000",
            "buyer": "0.00000000",
            "seller": "0.00000000",
        },
        "specialCommission": {
            "maker": "0.00000000",
            "taker": "0.00000000",
            "buyer": "0.00000000",
            "seller": "0.00000000",
        },
        "taxCommission": {
            "maker": "0.00000000",
            "taker": "0.00000000",
            "buyer": "0.00000000",
            "seller": "0.00000000",
        },
        "discount": {
            "enabledForAccount": True,
            "enabledForSymbol": True,
            "discountAsset": None,
            "discount": "0.00000000",
        },
    }


def _symbol_trading_rules() -> SymbolTradingRules:
    """
    함수 이름: _symbol_trading_rules()
    기능: Testnet permission proxy의 public filter 조회 계약에 사용할 엄격 fixture를 생성한다.
    인자: 없음
    반환값: ETHUSDT MARKET rule을 보존한 SymbolTradingRules
    작성 날짜: 2026/08/31
    """
    # Proxy는 raw exchangeInfo 대신 불변 domain DTO만 위임하므로 전체 필드를 분명히 채운다.
    return SymbolTradingRules(
        symbol="ETHUSDT",
        status="TRADING",
        base_asset="ETH",
        quote_asset="USDT",
        base_asset_precision=8,
        order_types=frozenset({"LIMIT", "MARKET"}),
        is_spot_trading_allowed=True,
        lot_size=QuantityFilter(
            filter_type="LOT_SIZE",
            minimum_quantity=Decimal("0.0001"),
            maximum_quantity=Decimal("1000"),
            step_size=Decimal("0.0001"),
        ),
        market_lot_size=QuantityFilter(
            filter_type="MARKET_LOT_SIZE",
            minimum_quantity=Decimal("0.001"),
            maximum_quantity=Decimal("100"),
            step_size=Decimal("0.001"),
        ),
        notional_filters=(
            NotionalFilter(
                filter_type="NOTIONAL",
                minimum_notional=Decimal("10"),
                maximum_notional=Decimal("100000"),
                apply_minimum_to_market=True,
                apply_maximum_to_market=True,
                average_price_minutes=5,
            ),
        ),
    )


def _phase13_order(
    *,
    side: OrderSide,
    client_order_id: str,
    intent_id: str,
) -> Order:
    """
    함수 이름: _phase13_order()
    기능: Phase 13 logical submit guard용 CASE_C BUY 또는 STOP SELL fixture를 만든다.
    인자: side -> BUY 또는 SELL 방향
        client_order_id -> permit별 고유 application client ID
        intent_id -> logical order intent identity
    반환값: submission_attempt 0인 canonical ETHUSDT Order
    작성 날짜: 2026/08/31
    """
    if side not in (OrderSide.BUY, OrderSide.SELL):
        raise ValueError("side must be BUY or SELL")

    # 두 방향 모두 MARKET 의미를 공유하고 SELL만 exact STOP recovery provenance를 가진다.
    return Order(
        intent_id=intent_id,
        client_order_id=client_order_id,
        submission_attempt=0,
        symbol="ETHUSDT",
        side=side,
        strategy=StrategyType.CASE_C,
        regime_type=RegimeType.TYPE_0,
        requested_quantity=Decimal("0.01"),
        submitted_quantity=Decimal("0.01"),
        market_price_at_decision=Decimal("100"),
        exit_reason=(ExitReason.STOP if side is OrderSide.SELL else None),
    )


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

    def get_account_commission(self, *, symbol: str) -> object:
        """
        함수 이름: get_account_commission()
        기능: permission proxy 위임 검증용 비활성 할인 payload를 반환한다.
        인자: symbol -> proxy가 전달한 canonical Spot symbol
        반환값: 제3 자산 할인이 비활성인 account commission mapping
        작성 날짜: 2026/08/24
        """
        # 실제 network 없이 symbol identity와 read-only 위임 surface만 검증한다.
        return {
            "symbol": symbol,
            "standardCommission": {
                "maker": "0.00000000",
                "taker": "0.00000000",
                "buyer": "0.00000000",
                "seller": "0.00000000",
            },
            "specialCommission": {
                "maker": "0.00000000",
                "taker": "0.00000000",
                "buyer": "0.00000000",
                "seller": "0.00000000",
            },
            "taxCommission": {
                "maker": "0.00000000",
                "taker": "0.00000000",
                "buyer": "0.00000000",
                "seller": "0.00000000",
            },
            "discount": {
                "enabledForAccount": False,
                "enabledForSymbol": True,
                "discountAsset": "BNB",
                "discount": "0.75000000",
            },
        }

    def fetch_symbol_trading_rules(
        self,
        *,
        symbol: str,
    ) -> SymbolTradingRules:
        """
        함수 이름: fetch_symbol_trading_rules()
        기능: permission proxy의 public rule 위임 검증용 엄격 DTO를 반환한다.
        인자: symbol -> proxy가 전달한 canonical Spot symbol
        반환값: ETHUSDT SymbolTradingRules
        작성 날짜: 2026/08/31
        """
        if symbol != "ETHUSDT":
            raise ValueError("fake supports only ETHUSDT")

        return _symbol_trading_rules()  # 각 호출이 새 불변 DTO를 제공해 cache 공유를 배제한다.


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

    def test_permission_proxy_allows_read_only_commission_preflight(self) -> None:
        """
        함수 이름: test_permission_proxy_allows_read_only_commission_preflight()
        기능: 주문 권한이 없어도 normalized commission preflight를 위임하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        environment = _read_only_environment()

        # Generic runtime factory 호출을 가로채 실제 socket 없이 permission proxy만 조립한다.
        with patch.object(
            testnet_module,
            "_load_testnet_client_types",
            return_value=(_FakeRESTClient, _FakeWebSocketClient),
        ), patch.object(
            testnet_module,
            "create_application_runtime",
            return_value=sentinel.runtime,
        ) as runtime_factory, TemporaryDirectory() as temporary_directory:
            runtime = testnet_module.create_testnet_application_runtime(
                history_path=Path(temporary_directory) / "history.jsonl",
                environment=environment,
            )
        permission_client = runtime_factory.call_args.args[0]

        # Read-only runtime도 실제 주문 전에 BNB 등 제3 수수료 자산 가능성을 확인할 수 있어야 한다.
        self.assertIs(runtime, sentinel.runtime)
        policy = APIGateway(
            permission_client
        ).fetch_commission_discount_policy(
            "ethusdt"
        )
        self.assertEqual(policy.symbol, "ETHUSDT")
        self.assertEqual(policy.discount_asset, "BNB")
        self.assertFalse(policy.can_charge_discount_asset)

    def test_permission_proxy_allows_public_filter_reads_without_order_opt_in(
        self,
    ) -> None:
        """
        함수 이름: test_permission_proxy_allows_public_filter_reads_without_order_opt_in()
        기능: read-only proxy가 mutation 권한 없이 fresh rule과 prepare/submission provenance를 위임하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        rules = _symbol_trading_rules()
        filter_evidence = OrderPreparationFilterEvidence(
            intent_id="proxy-filter-evidence-intent",
            client_order_id="bat-proxy-filter-evidence",
            side=OrderSide.BUY,
            observed_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
            rules=rules,
            account_filters=_empty_account_relevant_filters(),
            account_filters_observed_at=datetime(
                2026,
                8,
                31,
                tzinfo=timezone.utc,
            ),
            account_open_orders_observed_at=datetime(
                2026,
                8,
                31,
                tzinfo=timezone.utc,
            ),
            account_open_order_lists_observed_at=datetime(
                2026,
                8,
                31,
                tzinfo=timezone.utc,
            ),
            account_open_state_verified_empty=True,
            reference_price=ReferencePrice(
                symbol="ETHUSDT",
                price=Decimal("100"),
                exchange_timestamp=1788134400000,
            ),
            reference_price_observed_at=datetime(
                2026,
                8,
                31,
                tzinfo=timezone.utc,
            ),
        )
        submission_evidence = OrderSubmissionAttemptEvidence(
            intent_id="proxy-filter-evidence-intent",
            client_order_id="bat-proxy-filter-evidence",
            side=OrderSide.BUY,
            attempted_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
        )
        delegate = Mock(name="read_only_symbol_rules_delegate")
        delegate.get_account.return_value = {}
        delegate.fetch_symbol_trading_rules.return_value = rules
        delegate.get_account_filters.return_value = {
            "exchangeFilters": [],
            "symbolFilters": [],
            "assetFilters": [
                {
                    "filterType": "MAX_ASSET",
                    "asset": "USDT",
                    "limit": "250.00000000",
                }
            ],
        }
        reference_price = ReferencePrice(
            symbol="ETHUSDT",
            price=Decimal("100"),
            exchange_timestamp=1788134400000,
        )
        delegate.fetch_reference_price.return_value = reference_price
        delegate.get_order_preparation_filter_evidence.return_value = (
            filter_evidence
        )
        delegate.get_order_submission_attempt_evidence.return_value = (
            submission_evidence
        )
        permission_client = testnet_module._TestnetOrderPermissionRESTClient(
            delegate,
            allow_orders=False,
            maximum_order_notional=None,
        )

        # Public GET과 immutable provenance 조회는 order gate를 열지 않고 delegate에만 위임된다.
        result = APIGateway(
            permission_client
        ).fetch_symbol_trading_rules("ethusdt")
        account_asset_filters = APIGateway(
            permission_client
        ).fetch_account_asset_filters("ethusdt")
        observed_reference_price = APIGateway(
            permission_client
        ).fetch_reference_price("ethusdt")
        observed_filter_evidence = APIGateway(
            permission_client
        ).get_order_preparation_filter_evidence(
            "bat-proxy-filter-evidence"
        )
        observed_submission_evidence = APIGateway(
            permission_client
        ).get_order_submission_attempt_evidence(
            "bat-proxy-filter-evidence"
        )

        self.assertIs(result, rules)
        self.assertEqual(
            account_asset_filters,
            (
                AccountAssetFilter(
                    filter_type="MAX_ASSET",
                    asset="USDT",
                    maximum_quantity=Decimal("250.00000000"),
                ),
            ),
        )
        self.assertIs(reference_price, observed_reference_price)
        self.assertIs(filter_evidence, observed_filter_evidence)
        self.assertIs(submission_evidence, observed_submission_evidence)
        delegate.fetch_symbol_trading_rules.assert_called_once_with(
            symbol="ETHUSDT"
        )
        delegate.get_account_filters.assert_called_once_with(
            symbol="ETHUSDT"
        )
        delegate.fetch_reference_price.assert_called_once_with(
            symbol="ETHUSDT"
        )
        delegate.get_order_preparation_filter_evidence.assert_called_once_with(
            client_order_id="bat-proxy-filter-evidence"
        )
        delegate.get_order_submission_attempt_evidence.assert_called_once_with(
            client_order_id="bat-proxy-filter-evidence"
        )
        delegate.prepare_order.assert_not_called()
        delegate.submit_order.assert_not_called()

    def test_permission_proxy_forwards_all_symbol_open_state_reads(
        self,
    ) -> None:
        """
        함수 이름: test_permission_proxy_forwards_all_symbol_open_state_reads()
        기능: Read-only Testnet proxy가 all-symbol open order/list bool 조회만 그대로 전달하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        delegate = Mock(name="read_only_open_state_delegate")
        delegate.has_any_exchange_open_orders.return_value = False
        delegate.has_any_exchange_open_order_lists.return_value = True
        permission_client = testnet_module._TestnetOrderPermissionRESTClient(
            delegate,
            allow_orders=False,
            maximum_order_notional=None,
        )

        # Order opt-in이 없어도 두 signed read는 허용하되 prepare·submit 권한으로 확장하지 않는다.
        has_open_orders = permission_client.has_any_exchange_open_orders()
        has_open_order_lists = (
            permission_client.has_any_exchange_open_order_lists()
        )  # Raw order/list object가 아닌 exact bool만 permission 경계를 통과한다.

        self.assertFalse(has_open_orders)
        self.assertTrue(has_open_order_lists)
        delegate.has_any_exchange_open_orders.assert_called_once_with()
        delegate.has_any_exchange_open_order_lists.assert_called_once_with()
        delegate.prepare_order.assert_not_called()
        delegate.submit_order.assert_not_called()

        # Delegate가 bool 계약을 벗어나면 truthiness 변환 없이 같은 read-only 경계에서 차단한다.
        invalid_delegate = Mock(name="invalid_open_state_delegate")
        invalid_delegate.has_any_exchange_open_orders.return_value = ()
        invalid_permission_client = (
            testnet_module._TestnetOrderPermissionRESTClient(
                invalid_delegate,
                allow_orders=False,
                maximum_order_notional=None,
            )
        )
        with self.assertRaisesRegex(
            TypeError,
            "exchange open order state must be a bool",
        ):
            invalid_permission_client.has_any_exchange_open_orders()
        invalid_delegate.prepare_order.assert_not_called()
        invalid_delegate.submit_order.assert_not_called()

    def test_permission_proxy_blocks_unsupported_commission_before_prepare(
        self,
    ) -> None:
        """
        함수 이름: test_permission_proxy_blocks_unsupported_commission_before_prepare()
        기능: BNB 할인 가능 계정의 Testnet 주문을 delegate prepare 전에 차단하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        delegate = Mock(name="testnet_rest_delegate")
        delegate.get_account_commission.return_value = {
            "symbol": "ETHUSDT",
            "standardCommission": {
                "maker": "0.00000000",
                "taker": "0.00000000",
                "buyer": "0.00000000",
                "seller": "0.00000000",
            },
            "specialCommission": {
                "maker": "0.00000000",
                "taker": "0.00000000",
                "buyer": "0.00000000",
                "seller": "0.00000000",
            },
            "taxCommission": {
                "maker": "0.00000000",
                "taker": "0.00000000",
                "buyer": "0.00000000",
                "seller": "0.00000000",
            },
            "discount": {
                "enabledForAccount": True,
                "enabledForSymbol": True,
                "discountAsset": "BNB",
                "discount": "0.75000000",
            },
        }
        permission_client = (
            testnet_module._TestnetOrderPermissionRESTClient(
                delegate,
                allow_orders=True,
                maximum_order_notional=Decimal("100"),
            )
        )
        blocked_order = Order(
            intent_id="commission-preflight-intent",
            client_order_id="bat-commission-preflight",
            submission_attempt=0,
            symbol="ETHUSDT",
            side=OrderSide.BUY,
            strategy=StrategyType.CASE_B,
            regime_type=RegimeType.TYPE_0,
            requested_quantity=Decimal("0.01"),
            submitted_quantity=Decimal("0.01"),
            market_price_at_decision=Decimal("100"),
        )

        # Unsupported policy 오류가 난 뒤 mutation 준비 호출은 단 한 번도 실행되지 않아야 한다.
        with self.assertRaisesRegex(
            testnet_module.TestnetConfigurationError,
            "unsupported fee asset",
        ):
            permission_client.prepare_order(order=blocked_order)
        delegate.prepare_order.assert_not_called()

    def test_permission_proxy_delegates_prepare_for_supported_commission(
        self,
    ) -> None:
        """
        함수 이름: test_permission_proxy_delegates_prepare_for_supported_commission()
        기능: 실제 Testnet all-zero null 정책은 signed preflight 뒤 prepare를 위임하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        delegate = Mock(name="supported_testnet_rest_delegate")
        delegate.get_account_commission.return_value = {
            "symbol": "ETHUSDT",
            "standardCommission": {
                "maker": "0.00000000",
                "taker": "0.00000000",
                "buyer": "0.00000000",
                "seller": "0.00000000",
            },
            "specialCommission": {
                "maker": "0.00000000",
                "taker": "0.00000000",
                "buyer": "0.00000000",
                "seller": "0.00000000",
            },
            "taxCommission": {
                "maker": "0.00000000",
                "taker": "0.00000000",
                "buyer": "0.00000000",
                "seller": "0.00000000",
            },
            "discount": {
                "enabledForAccount": True,
                "enabledForSymbol": True,
                "discountAsset": None,
                "discount": "0.00000000",
            },
        }
        permission_client = (
            testnet_module._TestnetOrderPermissionRESTClient(
                delegate,
                allow_orders=True,
                maximum_order_notional=Decimal("100"),
            )
        )
        candidate_order = Order(
            intent_id="supported-commission-preflight-intent",
            client_order_id="bat-supported-commission-preflight",
            submission_attempt=0,
            symbol="ETHUSDT",
            side=OrderSide.BUY,
            strategy=StrategyType.CASE_B,
            regime_type=RegimeType.TYPE_0,
            requested_quantity=Decimal("0.01"),
            submitted_quantity=Decimal("0.01"),
            market_price_at_decision=Decimal("100"),
        )
        delegate.prepare_order.return_value = candidate_order

        # 관찰된 exact all-zero 정책은 MARKET BUY에도 매 attempt 다시 읽은 뒤 위임한다.
        result = permission_client.prepare_order(order=candidate_order)
        self.assertIs(result, candidate_order)
        delegate.get_account_commission.assert_called_once_with(
            symbol="ETHUSDT"
        )
        delegate.prepare_order.assert_called_once_with(order=candidate_order)

    def test_permission_proxy_blocks_market_buy_received_asset_commission(
        self,
    ) -> None:
        """
        함수 이름: test_permission_proxy_blocks_market_buy_received_asset_commission()
        기능: MARKET BUY 수신 ETH 수수료 가능성을 delegate filter 준비 전에 차단하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        delegate = Mock(name="market_buy_commission_delegate")
        delegate.get_account_commission.return_value = {
            "symbol": "ETHUSDT",
            "standardCommission": {
                "maker": "0.00000000",
                "taker": "0.00100000",
                "buyer": "0.00000000",
                "seller": "0.00000000",
            },
            "specialCommission": {
                "maker": "0.00000000",
                "taker": "0.00000000",
                "buyer": "0.00000000",
                "seller": "0.00000000",
            },
            "taxCommission": {
                "maker": "0.00000000",
                "taker": "0.00000000",
                "buyer": "0.00000000",
                "seller": "0.00000000",
            },
            "discount": {
                "enabledForAccount": False,
                "enabledForSymbol": True,
                "discountAsset": "BNB",
                "discount": "0.75000000",
            },
        }
        permission_client = testnet_module._TestnetOrderPermissionRESTClient(
            delegate,
            allow_orders=True,
            maximum_order_notional=Decimal("100"),
        )
        blocked_order = Order(
            intent_id="market-buy-dust-preflight-intent",
            client_order_id="bat-market-buy-dust-preflight",
            submission_attempt=0,
            symbol="ETHUSDT",
            side=OrderSide.BUY,
            strategy=StrategyType.CASE_B,
            regime_type=RegimeType.TYPE_0,
            requested_quantity=Decimal("0.01"),
            submitted_quantity=Decimal("0.01"),
            market_price_at_decision=Decimal("100"),
        )

        # BUY fee가 수신 ETH를 줄일 수 있으면 filter 준비나 주문 POST에 도달하지 않는다.
        with self.assertRaisesRegex(
            testnet_module.TestnetConfigurationError,
            "base-asset dust",
        ):
            permission_client.prepare_order(order=blocked_order)
        delegate.prepare_order.assert_not_called()

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
        for invalid_cap in (
            None,
            "0",
            "-1",
            "NaN",
            "Infinity",
            "-Infinity",
            "100.0000000001",
            "101",
            " 100",
            "100 ",
            "not-a-number",
        ):
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
        ] = "100"
        enabled_configuration = testnet_module.load_testnet_configuration(
            enabled_environment
        )
        self.assertTrue(enabled_configuration.allow_testnet_orders)
        self.assertEqual(
            testnet_module.require_testnet_order_permission(
                enabled_configuration
            ),
            Decimal("100"),
        )

    def test_phase13_public_case2_requires_exact_third_opt_in(self) -> None:
        """
        함수 이름: test_phase13_public_case2_requires_exact_third_opt_in()
        기능: 전용 public Case 2 권한이 세 번째 exact flag와 기존 주문 gate를 모두 요구하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        # 세 번째 flag 단독 상태는 read-only 권한보다 강한 mode로 해석하지 않는다.
        read_only_with_third_flag = _read_only_environment()
        read_only_with_third_flag[
            testnet_module.BINANCE_RUN_PHASE13_PUBLIC_CASE2_ENV
        ] = "1"
        isolated_third_flag_configuration = (
            testnet_module.load_testnet_configuration(
                read_only_with_third_flag
            )
        )
        self.assertFalse(
            isolated_third_flag_configuration.allow_phase13_public_case2
        )  # 기존 order opt-in이 없으면 세 번째 flag 하나만으로 권한을 만들지 않는다.

        environment = _read_only_environment()
        environment[testnet_module.BINANCE_RUN_TESTNET_ORDERS_ENV] = "1"
        environment[testnet_module.BINANCE_TESTNET_MAX_NOTIONAL_ENV] = "100"

        # Flag 부재와 알 수 없는 값은 모두 false로 수렴하고 별도 require 경계에서 차단된다.
        for flag_value in (None, "0", "true", "yes", "2"):
            selected_environment = dict(environment)
            if flag_value is not None:
                selected_environment[
                    testnet_module.BINANCE_RUN_PHASE13_PUBLIC_CASE2_ENV
                ] = flag_value
            with self.subTest(flag_value=flag_value):
                configuration = testnet_module.load_testnet_configuration(
                    selected_environment
                )
                self.assertFalse(configuration.allow_phase13_public_case2)
                with self.assertRaises(testnet_module.TestnetConfigurationError):
                    testnet_module.require_phase13_public_case2_permission(
                        configuration
                    )

        enabled_environment = dict(environment)
        enabled_environment[
            testnet_module.BINANCE_RUN_PHASE13_PUBLIC_CASE2_ENV
        ] = "1"
        enabled_configuration = testnet_module.load_testnet_configuration(
            enabled_environment
        )
        self.assertTrue(enabled_configuration.allow_phase13_public_case2)
        self.assertEqual(
            Decimal("100"),
            testnet_module.require_phase13_public_case2_permission(
                enabled_configuration
            ),
        )  # 전용 gate도 기존 cap 객체를 다른 숫자로 대체하지 않는다.

    def test_phase13_broad_collection_keeps_legacy_order_suites_skipped(
        self,
    ) -> None:
        """
        함수 이름: test_phase13_broad_collection_keeps_legacy_order_suites_skipped()
        기능: 세 flag broad collection에서 Phase 9 lifecycle·cold-restart가 함께 활성화되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        backend_root = Path(__file__).resolve().parents[3]
        child_environment = dict(os.environ)
        child_environment.update(
            {
                "PYTHONPATH": os.pathsep.join(
                    (str(backend_root / "src"), str(backend_root))
                ),
                testnet_module.BINANCE_RUN_TESTNET_ENV: "1",
                testnet_module.BINANCE_RUN_TESTNET_ORDERS_ENV: "1",
                testnet_module.BINANCE_RUN_PHASE13_PUBLIC_CASE2_ENV: "1",
                testnet_module.BINANCE_TESTNET_API_KEY_ENV: API_KEY_CANARY,
                testnet_module.BINANCE_TESTNET_API_SECRET_ENV: API_SECRET_CANARY,
                testnet_module.BINANCE_TESTNET_MAX_NOTIONAL_ENV: "100",
            }
        )
        verification_source = "\n".join(
            (
                "from tests.testnet._support import READ_ONLY_TESTNET_REQUESTED, ORDER_TESTNET_REQUESTED, PHASE13_PUBLIC_CASE2_REQUESTED",
                "from tests.testnet.test_binance_testnet_read_only import BinanceTestnetReadOnlyTests",
                "from tests.testnet.test_binance_testnet_fault_injection import BinanceTestnetFaultInjectionTests",
                "from tests.testnet.test_binance_testnet_order_lifecycle import BinanceTestnetOrderLifecycleTests",
                "from tests.testnet.test_binance_testnet_cold_restart import BinanceTestnetColdRestartTests",
                "from tests.testnet.test_phase13_public_market_case2 import BinanceTestnetPhaseThirteenPublicMarketCase2Tests",
                "assert READ_ONLY_TESTNET_REQUESTED is False",
                "assert ORDER_TESTNET_REQUESTED is False",
                "assert PHASE13_PUBLIC_CASE2_REQUESTED is True",
                "assert BinanceTestnetReadOnlyTests.__unittest_skip__ is True",
                "assert BinanceTestnetFaultInjectionTests.__unittest_skip__ is True",
                "assert BinanceTestnetOrderLifecycleTests.__unittest_skip__ is True",
                "assert BinanceTestnetColdRestartTests.__unittest_skip__ is True",
                "assert getattr(BinanceTestnetPhaseThirteenPublicMarketCase2Tests, '__unittest_skip__', False) is False",
            )
        )

        # 별도 interpreter는 import-time decorator를 실제 broad-discovery 환경과 같은 새 module state로 평가한다.
        completed_process = subprocess.run(
            [sys.executable, "-c", verification_source],
            cwd=backend_root,
            env=child_environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )

        self.assertEqual(
            completed_process.returncode,
            0,
            msg=completed_process.stderr,
        )  # Class import만 수행하므로 Testnet network나 실제 test method는 실행되지 않는다.

    def test_actual_testnet_gate_truth_table_is_mutually_exclusive(self) -> None:
        """
        함수 이름: test_actual_testnet_gate_truth_table_is_mutually_exclusive()
        기능: base·order·Phase 13 flag 조합마다 legacy와 전용 actual target 선택을 새 interpreter에서 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        backend_root = Path(__file__).resolve().parents[3]
        gate_cases = (
            ("0", "0", "0", (False, False, False)),
            ("1", "0", "0", (True, False, False)),
            ("1", "1", "0", (True, True, False)),
            ("1", "0", "1", (False, False, False)),
            ("1", "1", "1", (False, False, True)),
        )

        # Import-time constant는 한 process에서 다시 계산하지 않고 각 truth-table row를 격리한다.
        for testnet_flag, order_flag, phase13_flag, expected_gates in gate_cases:
            with self.subTest(
                testnet_flag=testnet_flag,
                order_flag=order_flag,
                phase13_flag=phase13_flag,
            ):
                child_environment = dict(os.environ)
                child_environment.update(
                    {
                        "PYTHONPATH": os.pathsep.join(
                            (str(backend_root / "src"), str(backend_root))
                        ),
                        testnet_module.BINANCE_RUN_TESTNET_ENV: testnet_flag,
                        testnet_module.BINANCE_RUN_TESTNET_ORDERS_ENV: order_flag,
                        testnet_module.BINANCE_RUN_PHASE13_PUBLIC_CASE2_ENV: (
                            phase13_flag
                        ),
                        testnet_module.BINANCE_TESTNET_API_KEY_ENV: API_KEY_CANARY,
                        testnet_module.BINANCE_TESTNET_API_SECRET_ENV: (
                            API_SECRET_CANARY
                        ),
                    }
                )
                verification_source = "\n".join(
                    (
                        "from tests.testnet._support import READ_ONLY_TESTNET_REQUESTED, ORDER_TESTNET_REQUESTED, PHASE13_PUBLIC_CASE2_REQUESTED",
                        f"assert READ_ONLY_TESTNET_REQUESTED is {expected_gates[0]!r}",
                        f"assert ORDER_TESTNET_REQUESTED is {expected_gates[1]!r}",
                        f"assert PHASE13_PUBLIC_CASE2_REQUESTED is {expected_gates[2]!r}",
                    )
                )
                completed_process = subprocess.run(
                    [sys.executable, "-c", verification_source],
                    cwd=backend_root,
                    env=child_environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(
                    completed_process.returncode,
                    0,
                    msg=completed_process.stderr,
                )  # Credential은 canary뿐이고 suite method를 호출하지 않아 network effect가 없다.

    def test_legacy_broad_collection_keeps_phase13_target_skipped(
        self,
    ) -> None:
        """
        함수 이름: test_legacy_broad_collection_keeps_phase13_target_skipped()
        기능: 기존 두 flag broad collection에서는 Phase 9 suites만 활성화되고 Phase 13 target은 닫히는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        backend_root = Path(__file__).resolve().parents[3]
        child_environment = dict(os.environ)
        child_environment.pop(
            testnet_module.BINANCE_RUN_PHASE13_PUBLIC_CASE2_ENV,
            None,
        )
        child_environment.update(
            {
                "PYTHONPATH": os.pathsep.join(
                    (str(backend_root / "src"), str(backend_root))
                ),
                testnet_module.BINANCE_RUN_TESTNET_ENV: "1",
                testnet_module.BINANCE_RUN_TESTNET_ORDERS_ENV: "1",
                testnet_module.BINANCE_TESTNET_API_KEY_ENV: API_KEY_CANARY,
                testnet_module.BINANCE_TESTNET_API_SECRET_ENV: API_SECRET_CANARY,
                testnet_module.BINANCE_TESTNET_MAX_NOTIONAL_ENV: "100",
            }
        )
        verification_source = "\n".join(
            (
                "from tests.testnet._support import ORDER_TESTNET_REQUESTED, PHASE13_PUBLIC_CASE2_REQUESTED",
                "from tests.testnet.test_binance_testnet_order_lifecycle import BinanceTestnetOrderLifecycleTests",
                "from tests.testnet.test_binance_testnet_cold_restart import BinanceTestnetColdRestartTests",
                "from tests.testnet.test_phase13_public_market_case2 import BinanceTestnetPhaseThirteenPublicMarketCase2Tests",
                "assert ORDER_TESTNET_REQUESTED is True",
                "assert PHASE13_PUBLIC_CASE2_REQUESTED is False",
                "assert getattr(BinanceTestnetOrderLifecycleTests, '__unittest_skip__', False) is False",
                "assert getattr(BinanceTestnetColdRestartTests, '__unittest_skip__', False) is False",
                "assert BinanceTestnetPhaseThirteenPublicMarketCase2Tests.__unittest_skip__ is True",
            )
        )

        # 별도 interpreter의 import-time decorator만 확인해 legacy 주문 method 자체는 실행하지 않는다.
        completed_process = subprocess.run(
            [sys.executable, "-c", verification_source],
            cwd=backend_root,
            env=child_environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )

        self.assertEqual(
            completed_process.returncode,
            0,
            msg=completed_process.stderr,
        )  # 전용 flag 부재가 기존 Phase 9 collection 의미를 바꾸지 않아야 한다.

    def test_permission_proxy_rechecks_final_notional_and_provenance(self) -> None:
        """
        함수 이름: test_permission_proxy_rechecks_final_notional_and_provenance()
        기능: filter 반환 뒤 cap 초과와 decision provenance 변조를 submit 전에 각각 차단하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        delegate = Mock(name="post_filter_cap_delegate")
        delegate.get_account_commission.return_value = (
            _zero_commission_payload()
        )
        permission_client = testnet_module._TestnetOrderPermissionRESTClient(
            delegate,
            allow_orders=True,
            maximum_order_notional=Decimal("100"),
        )

        # 입력 submitted notional은 99지만 injected filter 결과가 101이면 반환 직후 absolute cap에서 막힌다.
        crossing_order = Order(
            intent_id="post-filter-crossing-intent",
            client_order_id="bat-post-filter-crossing",
            submission_attempt=0,
            symbol="ETHUSDT",
            side=OrderSide.BUY,
            strategy=StrategyType.CASE_B,
            regime_type=RegimeType.TYPE_0,
            requested_quantity=Decimal("200"),
            submitted_quantity=Decimal("99"),
            market_price_at_decision=Decimal("1"),
        )

        def cross_cap(*, order: Order) -> Order:
            """
            함수 이름: cross_cap()
            기능: filter 전 통과 수량을 반환 단계에서 absolute cap 초과로 바꾼다.
            인자: order -> proxy가 전달한 mutable Order
            반환값: submitted quantity만 101로 바꾼 동일 Order
            작성 날짜: 2026/08/31
            """
            order.submitted_quantity = Decimal("101")
            return order  # 실제 adapter가 아닌 negative fixture만 비정상 상향을 주입한다.

        delegate.prepare_order.side_effect = cross_cap
        with self.assertRaisesRegex(
            testnet_module.TestnetConfigurationError,
            "absolute cap",
        ):
            permission_client.prepare_order(order=crossing_order)
        delegate.submit_order.assert_not_called()

        # 두 번째 독립 Order는 filter가 decision price를 바꿔도 새 가격으로 cap을 재해석하지 못한다.
        provenance_order = Order(
            intent_id="decision-provenance-intent",
            client_order_id="bat-decision-provenance",
            submission_attempt=0,
            symbol="ETHUSDT",
            side=OrderSide.BUY,
            strategy=StrategyType.CASE_B,
            regime_type=RegimeType.TYPE_0,
            requested_quantity=Decimal("1"),
            submitted_quantity=Decimal("1"),
            market_price_at_decision=Decimal("100"),
        )

        def change_decision_price(*, order: Order) -> Order:
            """
            함수 이름: change_decision_price()
            기능: immutable evaluation 결정 가격 변조를 filter 반환에 주입한다.
            인자: order -> proxy가 전달한 mutable Order
            반환값: 결정 가격을 바꾼 동일 Order
            작성 날짜: 2026/08/31
            """
            order.market_price_at_decision = Decimal("99")
            return order  # provenance 비교가 cap 계산보다 먼저 실패해야 한다.

        delegate.prepare_order.side_effect = change_decision_price
        with self.assertRaisesRegex(
            testnet_module.TestnetConfigurationError,
            "immutable decision provenance",
        ):
            permission_client.prepare_order(order=provenance_order)
        delegate.submit_order.assert_not_called()

    def test_permission_proxy_allows_exact_cap_and_recovery_stop_sell(self) -> None:
        """
        함수 이름: test_permission_proxy_allows_exact_cap_and_recovery_stop_sell()
        기능: exact 100 BUY와 authoritative recovery STOP SELL의 cap 예외를 구분해 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        delegate = Mock(name="exact_cap_and_recovery_delegate")
        delegate.get_account_commission.return_value = (
            _zero_commission_payload()
        )

        def preserve_order(*, order: Order) -> Order:
            """
            함수 이름: preserve_order()
            기능: 정상 filter 준비 결과가 입력 Order identity와 provenance를 보존하게 한다.
            인자: order -> permission proxy가 전달한 주문
            반환값: 변경하지 않은 동일 Order
            작성 날짜: 2026/08/31
            """
            return order  # 경계값과 recovery 예외만 관찰하도록 filter 변형을 배제한다.

        delegate.prepare_order.side_effect = preserve_order
        permission_client = testnet_module._TestnetOrderPermissionRESTClient(
            delegate,
            allow_orders=True,
            maximum_order_notional=Decimal("100"),
        )
        exact_cap_buy = Order(
            intent_id="exact-cap-buy-intent",
            client_order_id="bat-exact-cap-buy",
            submission_attempt=0,
            symbol="ETHUSDT",
            side=OrderSide.BUY,
            strategy=StrategyType.CASE_B,
            regime_type=RegimeType.TYPE_0,
            requested_quantity=Decimal("1"),
            submitted_quantity=Decimal("1"),
            market_price_at_decision=Decimal("100"),
        )
        recovery_sell = Order(
            intent_id="recovery-stop-sell-intent",
            client_order_id="bat-recovery-stop-sell",
            submission_attempt=0,
            symbol="ETHUSDT",
            side=OrderSide.SELL,
            strategy=StrategyType.CASE_B,
            regime_type=RegimeType.TYPE_0,
            requested_quantity=Decimal("2"),
            submitted_quantity=Decimal("2"),
            market_price_at_decision=Decimal("100"),
            exit_reason=ExitReason.STOP,
        )

        # BUY는 경계값을 포함하고 STOP SELL은 정확한 Position 청산을 위해 entry quote ceiling을 받지 않는다.
        self.assertIs(
            exact_cap_buy,
            permission_client.prepare_order(order=exact_cap_buy),
        )
        self.assertIs(
            recovery_sell,
            permission_client.prepare_order(order=recovery_sell),
        )
        delegate.submit_order.assert_not_called()

    def test_phase13_submission_guard_allows_only_exact_buy_then_stop_sell(
        self,
    ) -> None:
        """
        함수 이름: test_phase13_submission_guard_allows_only_exact_buy_then_stop_sell()
        기능: Phase 13 proxy가 CASE_C initial BUY와 STOP SELL permit을 각각 한 번만 소비하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        delegate = Mock(name="phase13_submission_delegate")
        delegate.get_account.return_value = {}
        delegate.submit_order.return_value = sentinel.order_result
        permission_client = testnet_module._TestnetOrderPermissionRESTClient(
            delegate,
            allow_orders=True,
            maximum_order_notional=Decimal("100"),
            phase13_public_case2=True,
            clock=lambda: datetime(2026, 8, 31, tzinfo=timezone.utc),
        )
        gateway = APIGateway(permission_client)
        buy_order = _phase13_order(
            side=OrderSide.BUY,
            client_order_id="bat-phase13-buy-guard",
            intent_id="phase13-buy-intent",
        )
        sell_order = _phase13_order(
            side=OrderSide.SELL,
            client_order_id="bat-phase13-sell-guard",
            intent_id="phase13-stop-intent",
        )

        # CASE_B 등 target 밖 주문은 첫 permit이나 mutation_started 상태를 소비하지 않는다.
        wrong_strategy_order = _phase13_order(
            side=OrderSide.BUY,
            client_order_id="bat-phase13-case-b",
            intent_id="phase13-case-b-intent",
        )
        wrong_strategy_order.strategy = StrategyType.CASE_B
        with self.assertRaisesRegex(
            testnet_module.TestnetConfigurationError,
            "ETHUSDT CASE_C",
        ):
            permission_client.submit_order(order=wrong_strategy_order)
        self.assertFalse(
            gateway.get_phase13_order_submission_guard_snapshot().mutation_started
        )

        # 최초 BUY permit 뒤 같은 방향의 두 번째 logical submit은 delegate 전에 거부된다.
        self.assertIs(
            sentinel.order_result,
            permission_client.submit_order(order=buy_order),
        )
        with self.assertRaisesRegex(
            testnet_module.TestnetConfigurationError,
            "initial BUY then STOP SELL",
        ):
            permission_client.submit_order(
                order=_phase13_order(
                    side=OrderSide.BUY,
                    client_order_id="bat-phase13-buy-repeat",
                    intent_id="phase13-buy-repeat-intent",
                )
            )

        # 정확한 STOP SELL만 두 번째 permit을 소비하며 이후에는 영구 차단된다.
        self.assertIs(
            sentinel.order_result,
            permission_client.submit_order(order=sell_order),
        )
        with self.assertRaisesRegex(
            testnet_module.TestnetConfigurationError,
            "permanently blocked",
        ):
            permission_client.submit_order(
                order=_phase13_order(
                    side=OrderSide.SELL,
                    client_order_id="bat-phase13-sell-repeat",
                    intent_id="phase13-stop-repeat-intent",
                )
            )
        snapshot = gateway.get_phase13_order_submission_guard_snapshot()

        self.assertTrue(snapshot.mutation_started)
        self.assertTrue(snapshot.submissions_blocked)
        self.assertEqual(
            tuple(attempt.side for attempt in snapshot.attempts),
            (OrderSide.BUY, OrderSide.SELL),
        )
        self.assertEqual(
            tuple(attempt.order_type for attempt in snapshot.attempts),
            ("MARKET", "MARKET"),
        )
        self.assertEqual(delegate.submit_order.call_count, 2)

    def test_phase13_failure_block_prevents_delegate_and_reports_zero_mutation(
        self,
    ) -> None:
        """
        함수 이름: test_phase13_failure_block_prevents_delegate_and_reports_zero_mutation()
        기능: Harness failure block이 첫 mutation 전에도 모든 submit을 막고 안전한 snapshot을 반환하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        delegate = Mock(name="phase13_failure_block_delegate")
        delegate.get_account.return_value = {}
        permission_client = testnet_module._TestnetOrderPermissionRESTClient(
            delegate,
            allow_orders=True,
            maximum_order_notional=Decimal("100"),
            phase13_public_case2=True,
        )
        gateway = APIGateway(permission_client)

        # 공개 Gateway operation 하나로 permit을 닫은 뒤 실제 delegate 호출은 시작하지 않는다.
        gateway.block_phase13_order_submissions()
        with self.assertRaisesRegex(
            testnet_module.TestnetConfigurationError,
            "permanently blocked",
        ):
            permission_client.submit_order(
                order=_phase13_order(
                    side=OrderSide.BUY,
                    client_order_id="bat-phase13-blocked-buy",
                    intent_id="phase13-blocked-buy-intent",
                )
            )
        snapshot = gateway.get_phase13_order_submission_guard_snapshot()

        self.assertFalse(snapshot.mutation_started)
        self.assertTrue(snapshot.submissions_blocked)
        self.assertEqual(snapshot.attempts, ())
        delegate.submit_order.assert_not_called()

    def test_phase13_cancel_is_rejected_before_delegate_even_after_block(
        self,
    ) -> None:
        """
        함수 이름: test_phase13_cancel_is_rejected_before_delegate_even_after_block()
        기능: 전용 target의 임의 cancel을 최초 mutation 전과 failure block 뒤 모두 network 전에 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        delegate = Mock(name="phase13_cancel_delegate")
        permission_client = testnet_module._TestnetOrderPermissionRESTClient(
            delegate,
            allow_orders=True,
            maximum_order_notional=Decimal("100"),
            phase13_public_case2=True,
        )
        gateway = APIGateway(permission_client)
        buy_order = _phase13_order(
            side=OrderSide.BUY,
            client_order_id="bat-phase13-cancel-guard",
            intent_id="phase13-cancel-intent",
        )

        # Phase 13 shape가 맞는 Order도 cancel mutation에는 permit이 없고 submit budget도 소비하지 않는다.
        with self.assertRaisesRegex(
            testnet_module.TestnetConfigurationError,
            "does not permit order cancellation",
        ):
            permission_client.cancel_order(order=buy_order)
        self.assertFalse(
            gateway.get_phase13_order_submission_guard_snapshot().mutation_started
        )

        # Failure finalizer의 permanent block 뒤에도 cancel로 우회하는 별도 mutation surface는 열리지 않는다.
        gateway.block_phase13_order_submissions()
        with self.assertRaisesRegex(
            testnet_module.TestnetConfigurationError,
            "does not permit order cancellation",
        ):
            permission_client.cancel_order(order=buy_order)
        delegate.cancel_order.assert_not_called()

    def test_phase13_delegate_failure_does_not_restore_consumed_buy_permit(
        self,
    ) -> None:
        """
        함수 이름: test_phase13_delegate_failure_does_not_restore_consumed_buy_permit()
        기능: Delegate 예외로 결과가 불명이어도 최초 BUY permit과 mutation_started 증거가 유지되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        delegate = Mock(name="phase13_ambiguous_delegate")
        delegate.get_account.return_value = {}
        delegate.submit_order.side_effect = TimeoutError(
            "simulated credential-free timeout"
        )
        permission_client = testnet_module._TestnetOrderPermissionRESTClient(
            delegate,
            allow_orders=True,
            maximum_order_notional=Decimal("100"),
            phase13_public_case2=True,
        )
        buy_order = _phase13_order(
            side=OrderSide.BUY,
            client_order_id="bat-phase13-timeout-buy",
            intent_id="phase13-timeout-buy-intent",
        )

        # Delegate 진입 직전 permit이 소비되므로 timeout을 rollback해 같은 BUY를 재허용하지 않는다.
        with self.assertRaises(TimeoutError):
            permission_client.submit_order(order=buy_order)
        with self.assertRaisesRegex(
            testnet_module.TestnetConfigurationError,
            "initial BUY then STOP SELL",
        ):
            permission_client.submit_order(
                order=_phase13_order(
                    side=OrderSide.BUY,
                    client_order_id="bat-phase13-timeout-buy-repeat",
                    intent_id="phase13-timeout-buy-repeat-intent",
                )
            )
        snapshot = permission_client.get_phase13_order_submission_guard_snapshot()

        self.assertTrue(snapshot.mutation_started)
        self.assertFalse(snapshot.submissions_blocked)
        self.assertEqual(len(snapshot.attempts), 1)
        self.assertEqual(delegate.submit_order.call_count, 1)

    def test_phase13_submission_guard_rejects_concurrent_second_permit(
        self,
    ) -> None:
        """
        함수 이름: test_phase13_submission_guard_rejects_concurrent_second_permit()
        기능: 첫 delegate 호출 중인 thread와 경쟁하는 STOP SELL이 두 번째 permit을 조기 소비하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        delegate_entered = Event()
        release_delegate = Event()
        delegate = Mock(name="phase13_concurrent_delegate")
        delegate.get_account.return_value = {}

        def delayed_submit(*, order: Order) -> object:
            """
            함수 이름: delayed_submit()
            기능: 첫 permit이 in-progress인 동안 경쟁 호출을 관찰하도록 delegate 반환을 제한한다.
            인자: order -> proxy가 허용한 첫 Phase 13 BUY
            반환값: release 뒤 고정 sentinel result
            작성 날짜: 2026/08/31
            """
            self.assertIs(order.side, OrderSide.BUY)
            delegate_entered.set()
            if not release_delegate.wait(timeout=5):
                raise AssertionError("delegate release timed out")

            return sentinel.order_result  # 실제 network 없이 thread interleaving만 고정한다.

        delegate.submit_order.side_effect = delayed_submit
        permission_client = testnet_module._TestnetOrderPermissionRESTClient(
            delegate,
            allow_orders=True,
            maximum_order_notional=Decimal("100"),
            phase13_public_case2=True,
        )
        buy_order = _phase13_order(
            side=OrderSide.BUY,
            client_order_id="bat-phase13-concurrent-buy",
            intent_id="phase13-concurrent-buy-intent",
        )
        sell_order = _phase13_order(
            side=OrderSide.SELL,
            client_order_id="bat-phase13-concurrent-sell",
            intent_id="phase13-concurrent-stop-intent",
        )
        first_results: list[object] = []

        def submit_first_buy() -> None:
            """
            함수 이름: submit_first_buy()
            기능: 별도 thread에서 최초 BUY permit과 blocking delegate 호출을 시작한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/08/31
            """
            first_results.append(
                permission_client.submit_order(order=buy_order)
            )  # Thread 예외를 숨기지 않도록 성공 결과를 parent assertion에 넘긴다.

        buy_thread = Thread(target=submit_first_buy, daemon=True)
        buy_thread.start()
        try:
            self.assertTrue(delegate_entered.wait(timeout=5))

            # 첫 delegate가 끝나기 전에는 올바른 SELL shape여도 두 번째 permit을 소비할 수 없다.
            with self.assertRaisesRegex(
                testnet_module.TestnetConfigurationError,
                "one in-progress",
            ):
                permission_client.submit_order(order=sell_order)
        finally:
            # Assertion 실패에서도 background fixture를 즉시 깨워 다음 test로 thread를 넘기지 않는다.
            release_delegate.set()
            buy_thread.join(timeout=5)

        self.assertFalse(buy_thread.is_alive())
        self.assertEqual(first_results, [sentinel.order_result])
        self.assertEqual(
            len(
                permission_client.get_phase13_order_submission_guard_snapshot().attempts
            ),
            1,
        )
        self.assertEqual(delegate.submit_order.call_count, 1)

    def test_phase13_runtime_wires_single_controller_and_http_post_attempt(
        self,
    ) -> None:
        """
        함수 이름: test_phase13_runtime_wires_single_controller_and_http_post_attempt()
        기능: 세 번째 opt-in이 Controller intent 예산 1과 REST 주문 timestamp retry 금지를 함께 조립하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        environment = _read_only_environment()
        environment.update(
            {
                testnet_module.BINANCE_RUN_TESTNET_ORDERS_ENV: "1",
                testnet_module.BINANCE_RUN_PHASE13_PUBLIC_CASE2_ENV: "1",
                testnet_module.BINANCE_TESTNET_MAX_NOTIONAL_ENV: "100",
            }
        )

        # Client와 generic runtime factory를 가로채므로 실제 socket이나 주문 없이 exact 조립 인자만 본다.
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
                history_path=Path("phase13-history.jsonl"),
                environment=environment,
            )

        self.assertIs(runtime, sentinel.runtime)
        self.assertFalse(
            _FakeRESTClient.calls[0]["allow_order_timestamp_retry"]
        )
        self.assertEqual(
            runtime_factory.call_args.kwargs[
                "maximum_order_submissions_per_intent"
            ],
            1,
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
        account_update_observer = Mock(name="account_update_observer")
        trade_history_update_observer = Mock(
            name="trade_history_update_observer"
        )
        trading_session_update_observer = Mock(
            name="trading_session_update_observer"
        )
        monotonic_clock = Mock(
            name="monotonic_clock",
            return_value=0,
        )
        risk_policy_state = RiskPolicyUnavailable()

        # Transport runtime factory와 동일하게 세 observer를 위치 인자로 주입한다.
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
                account_update_observer,
                trade_history_update_observer,
                trading_session_update_observer,
                history_path=Path("test-history.jsonl"),
                environment=environment,
                risk_policy_state=risk_policy_state,
                monotonic_clock=monotonic_clock,
                kline_limit=21,
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

        # 두 observer는 Testnet 조립기에서 교체되지 않고 generic runtime에 그대로 전달된다.
        self.assertIs(
            runtime_keywords["account_update_observer"],
            account_update_observer,
        )
        self.assertIs(
            runtime_keywords["trade_history_update_observer"],
            trade_history_update_observer,
        )
        self.assertIs(
            runtime_keywords["trading_session_update_observer"],
            trading_session_update_observer,
        )  # Testnet 조립기가 세 observer identity를 generic runtime에 그대로 전달한다.
        self.assertIs(
            runtime_keywords["risk_policy_state"],
            risk_policy_state,
        )  # 승인값이 없는 explicit unavailable도 factory 경계에서 다른 값으로 바꾸지 않는다.
        self.assertIs(runtime_keywords["monotonic_clock"], monotonic_clock)
        self.assertEqual(runtime_keywords["kline_limit"], 21)

        fixed_endpoints = (
            testnet_module.BINANCE_SPOT_TESTNET_REST_ORIGIN,
            testnet_module.BINANCE_SPOT_TESTNET_STREAM_ORIGIN,
            testnet_module.BINANCE_SPOT_TESTNET_WEBSOCKET_API_URL,
        )
        self.assertTrue(
            all("testnet.binance.vision" in endpoint for endpoint in fixed_endpoints)
        )  # production hostname은 조립 module의 endpoint 상수에 존재하지 않는다.

    def test_runtime_rejects_non_callable_observers_before_client_creation(
        self,
    ) -> None:
        """
        함수 이름: test_runtime_rejects_non_callable_observers_before_client_creation()
        기능: Testnet 조립기가 잘못된 observer를 client 생성 전에 generic runtime과 같은 오류로 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        invalid_observer_cases = (
            (
                object(),
                None,
                "account_update_observer must be callable",
            ),
            (
                None,
                object(),
                "trade_history_update_observer must be callable",
            ),
        )

        # 각 observer 경계는 고정 endpoint client type을 로드하기 전에 독립적으로 검증한다.
        for (
            account_update_observer,
            trade_history_update_observer,
            expected_message,
        ) in invalid_observer_cases:
            with self.subTest(expected_message=expected_message), patch.object(
                testnet_module,
                "_load_testnet_client_types",
            ) as client_type_loader:
                with self.assertRaisesRegex(TypeError, expected_message):
                    testnet_module.create_testnet_application_runtime(
                        account_update_observer,
                        trade_history_update_observer,
                        history_path=Path("unused-history.jsonl"),
                        environment=_read_only_environment(),
                    )

                client_type_loader.assert_not_called()  # 잘못된 callback은 client 생성 경계에 도달하지 않는다.

    def test_runtime_rejects_non_callable_monotonic_clock_before_clients(
        self,
    ) -> None:
        """
        함수 이름: test_runtime_rejects_non_callable_monotonic_clock_before_clients()
        기능: Testnet 조립기가 잘못된 monotonic clock을 client 생성 전에 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # 30분 유지 시간 source는 network client를 만들기 전에 callable 계약을 통과해야 한다.
        with patch.object(
            testnet_module,
            "_load_testnet_client_types",
        ) as client_type_loader:
            with self.assertRaisesRegex(
                TypeError,
                "monotonic_clock must be callable or None",
            ):
                testnet_module.create_testnet_application_runtime(
                    history_path=Path("unused-history.jsonl"),
                    environment=_read_only_environment(),
                    monotonic_clock=object(),
                )

            client_type_loader.assert_not_called()  # 잘못된 시간 source에서는 credential client를 만들지 않는다.


if __name__ == "__main__":
    unittest.main()
