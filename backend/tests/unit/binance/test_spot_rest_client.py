"""BinanceSpotRESTClient의 Testnet signing·시간·오류·fill reconciliation을 검증한다."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from binance_auto_trader.adapters.binance.spot_rest_client import (
    BinanceSpotRESTClient,
    HTTPTransportResponse,
    OrderPreparationRequiredError,
    UrllibHTTPTransport,
)
from binance_auto_trader.adapters.binance.mappers import SymbolFilterError
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.trading.order import (
    Order,
    OrderResultFailureKind,
    OrderStatus,
)
from binance_auto_trader.domain.trading.states import OrderSide, StrategyType


FIXED_TIME = datetime(2026, 8, 22, 5, 0, tzinfo=timezone.utc)
FIXED_TIME_MILLISECONDS = 1787374800000
API_KEY = "testnet-api-key"
SECRET_KEY = "testnet-secret-never-log"


class QueueHTTPTransport:
    """
    클래스 이름: QueueHTTPTransport
    기능: 응답·예외 queue를 순서대로 반환하고 실제 HTTP request 내용을 기록한다.
    작성 날짜: 2026/08/22
    """

    def __init__(self, responses: list[object]) -> None:
        """
        함수 이름: __init__()
        기능: 반환할 response queue와 빈 request 기록을 초기화한다.
        인자: responses -> HTTPTransportResponse 또는 발생시킬 Exception 목록
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # 테스트마다 독립 queue를 복사해 호출자가 제공한 목록을 변경하지 않는다.
        self.responses = list(responses)
        self.requests: list[dict[str, object]] = []

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: int,
    ) -> HTTPTransportResponse:
        """
        함수 이름: request()
        기능: 요청 값을 기록하고 queue 첫 응답을 반환하거나 예외를 발생시킨다.
        인자: method -> HTTP method
            url -> 전송 대상 URL
            headers -> request header mapping
            body -> form body 또는 None
            timeout_seconds -> 정수 timeout
        반환값: queue의 HTTPTransportResponse
        작성 날짜: 2026/08/22
        """
        # Mutable header를 복사해 client가 후속 요청에서 바꿔도 과거 기록을 보존한다.
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        selected_response = self.responses.pop(0)
        if isinstance(selected_response, BaseException):
            raise selected_response
        if not isinstance(selected_response, HTTPTransportResponse):
            raise AssertionError("fake response must be HTTPTransportResponse")

        return selected_response  # production transport와 같은 불변 response만 반환한다.


def _json_response(
    payload: object,
    *,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
) -> HTTPTransportResponse:
    """
    함수 이름: _json_response()
    기능: JSON fixture를 UTF-8 HTTPTransportResponse로 직렬화한다.
    인자: payload -> JSON으로 직렬화할 값
        status_code -> HTTP status
        headers -> 선택 response headers
    반환값: fake transport 응답
    작성 날짜: 2026/08/22
    """
    return HTTPTransportResponse(
        status_code=status_code,
        headers={} if headers is None else dict(headers),
        body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
    )  # 실제 transport와 같은 bytes body를 사용한다.


def _exchange_info_payload(
    *,
    minimum_notional: str = "10",
    maximum_notional: str = "100000",
) -> dict[str, object]:
    """
    함수 이름: _exchange_info_payload()
    기능: ETHUSDT MARKET 제출과 fee mapping에 필요한 공식 exchangeInfo fixture를 만든다.
    인자: minimum_notional -> NOTIONAL minNotional
        maximum_notional -> NOTIONAL maxNotional
    반환값: 한 symbol의 exchangeInfo payload
    작성 날짜: 2026/08/22
    """
    # MARKET_LOT_SIZE가 LOT_SIZE보다 큰 step을 사용해 실제 floor 적용 여부를 드러낸다.
    return {
        "timezone": "UTC",
        "serverTime": FIXED_TIME_MILLISECONDS,
        "rateLimits": [],
        "symbols": [
            {
                "symbol": "ETHUSDT",
                "status": "TRADING",
                "baseAsset": "ETH",
                "baseAssetPrecision": 5,
                "quoteAsset": "USDT",
                "orderTypes": ["LIMIT", "MARKET"],
                "isSpotTradingAllowed": True,
                "filters": [
                    {
                        "filterType": "LOT_SIZE",
                        "minQty": "0.0001",
                        "maxQty": "10000",
                        "stepSize": "0.0001",
                    },
                    {
                        "filterType": "MARKET_LOT_SIZE",
                        "minQty": "0.001",
                        "maxQty": "1000",
                        "stepSize": "0.001",
                    },
                    {
                        "filterType": "NOTIONAL",
                        "minNotional": minimum_notional,
                        "applyMinToMarket": True,
                        "maxNotional": maximum_notional,
                        "applyMaxToMarket": True,
                        "avgPriceMins": 5,
                    },
                ],
            }
        ],
    }


def _order(*, quantity: str = "1.23456", client_order_id: str = "bat-client-0") -> Order:
    """
    함수 이름: _order()
    기능: 실제 REST client operation에 사용할 유효한 ETHUSDT MARKET intent를 만든다.
    인자: quantity -> requested/submitted quantity decimal 문자열
        client_order_id -> 시도별 Binance client order ID
    반환값: 아직 거래소 결과를 적용하지 않은 BUY Order
    작성 날짜: 2026/08/22
    """
    selected_quantity = Decimal(quantity)

    return Order(
        intent_id="spot-rest-intent",
        client_order_id=client_order_id,
        submission_attempt=0,
        symbol="ETHUSDT",
        side=OrderSide.BUY,
        strategy=StrategyType.CASE_B,
        regime_type=RegimeType.TYPE_0,
        requested_quantity=selected_quantity,
        submitted_quantity=selected_quantity,
        market_price_at_decision=Decimal("100"),
    )  # filter 전 두 quantity는 같은 intent 값을 갖는다.


def _fixed_clock() -> datetime:
    """
    함수 이름: _fixed_clock()
    기능: server offset 테스트에 사용할 고정 timezone-aware 시각을 반환한다.
    인자: 없음
    반환값: FIXED_TIME
    작성 날짜: 2026/08/22
    """
    return FIXED_TIME  # 모든 request attempt를 결정론적 timestamp로 만든다.


def _filled_order_payload(
    *,
    client_order_id: str = "bat-client-0",
    quantity: str = "1.234",
) -> dict[str, object]:
    """
    함수 이름: _filled_order_payload()
    기능: FULL MARKET response와 직접 fill 한 건을 가진 공식 주문 fixture를 만든다.
    인자: client_order_id -> 응답 clientOrderId
        quantity -> executedQty와 fill qty
    반환값: FILLED order response payload
    작성 날짜: 2026/08/22
    """
    return {
        "symbol": "ETHUSDT",
        "orderId": 42,
        "orderListId": -1,
        "clientOrderId": client_order_id,
        "transactTime": FIXED_TIME_MILLISECONDS,
        "price": "0.00000000",
        "origQty": quantity,
        "executedQty": quantity,
        "origQuoteOrderQty": "0.00000000",
        "cummulativeQuoteQty": "123.40000000",
        "status": "FILLED",
        "timeInForce": "GTC",
        "type": "MARKET",
        "side": "BUY",
        "fills": [
            {
                "price": "100.00000000",
                "qty": quantity,
                "commission": "0.12340000",
                "commissionAsset": "USDT",
                "tradeId": 7001,
            }
        ],
    }


def _client(
    transport: QueueHTTPTransport,
    *,
    maximum_order_notional: Decimal | None = None,
) -> BinanceSpotRESTClient:
    """
    함수 이름: _client()
    기능: 고정 clock과 주입 transport를 사용하는 credential-safe REST client를 만든다.
    인자: transport -> response queue와 request 기록을 가진 fake transport
        maximum_order_notional -> 선택 local opt-in 금액 상한
    반환값: 테스트 대상 BinanceSpotRESTClient
    작성 날짜: 2026/08/22
    """
    return BinanceSpotRESTClient(
        API_KEY,
        SECRET_KEY,
        transport=transport,
        clock=_fixed_clock,
        result_clock=_fixed_clock,
        maximum_order_notional=maximum_order_notional,
    )  # 실제 network 없이 production signing pipeline 전체를 실행한다.


class BinanceSpotRESTClientTests(unittest.TestCase):
    """
    클래스 이름: BinanceSpotRESTClientTests
    기능: 현행 Spot Testnet REST 계약과 안전한 OrderResult 분류를 검증한다.
    작성 날짜: 2026/08/22
    """

    def test_production_origin_is_rejected_even_with_explicit_transport(
        self,
    ) -> None:
        """
        함수 이름: test_production_origin_is_rejected_even_with_explicit_transport()
        기능: built-in 또는 fake transport 주입이 production endpoint 권한으로 바뀌지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # Phase 13 승인 전 concrete client는 transport 종류와 무관하게 Testnet URL만 허용한다.
        injected_transports = (
            UrllibHTTPTransport(),
            QueueHTTPTransport([]),
        )
        for injected_transport in injected_transports:
            with self.subTest(transport=type(injected_transport).__name__):
                with self.assertRaisesRegex(ValueError, "only official Spot Testnet"):
                    BinanceSpotRESTClient(
                        API_KEY,
                        SECRET_KEY,
                        base_url="https://api.binance.com/api",
                        transport=injected_transport,
                    )

    def test_submit_percent_encodes_then_signs_and_never_exposes_secret(self) -> None:
        """
        함수 이름: test_submit_percent_encodes_then_signs_and_never_exposes_secret()
        기능: MARKET form body의 percent-encoded bytes를 HMAC sign하고 secret을 노출하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        transport = QueueHTTPTransport(
            [
                _json_response(_exchange_info_payload()),
                _json_response({"serverTime": FIXED_TIME_MILLISECONDS}),
                _json_response(_filled_order_payload()),
            ]
        )
        client = _client(transport)
        order = _order()

        # Journal 전 prepare 결과와 이후 signed POST가 같은 exact 수량을 사용해야 한다.
        prepared_order = client.prepare_order(order)
        result = client.submit_order(order=prepared_order)

        self.assertEqual(result.status, OrderStatus.FILLED)
        self.assertEqual(order.requested_quantity, Decimal("1.23456"))
        self.assertEqual(order.submitted_quantity, Decimal("1.234"))
        self.assertEqual(len(result.fills), 1)
        post_request = transport.requests[2]
        self.assertEqual(post_request["method"], "POST")
        self.assertNotIn("?", post_request["url"])
        request_body = post_request["body"].decode("ascii")
        signed_payload, signature = request_body.rsplit("&signature=", 1)
        expected_signature = hmac.new(
            SECRET_KEY.encode("utf-8"),
            signed_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(signature, expected_signature)
        self.assertIn("newClientOrderId=bat-client-0", signed_payload)
        self.assertIn("quantity=1.234", signed_payload)

        # Secret은 repr, URL, header와 body 어디에도 평문으로 나타나지 않아야 한다.
        serialized_requests = repr(transport.requests)
        self.assertNotIn(SECRET_KEY, repr(client))
        self.assertNotIn(API_KEY, repr(client))
        self.assertNotIn(SECRET_KEY, serialized_requests)
        self.assertFalse(transport.responses)

    def test_server_timestamp_provider_reuses_synchronized_offset(self) -> None:
        """
        함수 이름: test_server_timestamp_provider_reuses_synchronized_offset()
        기능: WS provider용 timestamp가 최초 /time offset을 이후 network 없이 재사용하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        server_time = FIXED_TIME_MILLISECONDS + 4321
        transport = QueueHTTPTransport(
            [_json_response({"serverTime": server_time})]
        )
        client = _client(transport)

        # 첫 호출은 /time을 사용하고 두 번째 호출은 같은 local clock과 저장 offset만 사용한다.
        first_timestamp = client.get_server_timestamp_milliseconds()
        second_timestamp = client.get_server_timestamp_milliseconds()

        self.assertEqual(first_timestamp, server_time)
        self.assertEqual(second_timestamp, server_time)
        self.assertEqual(len(transport.requests), 1)
        self.assertTrue(transport.requests[0]["url"].endswith("/v3/time"))

    def test_submit_requires_prepare_before_any_http_request(self) -> None:
        """
        함수 이름: test_submit_requires_prepare_before_any_http_request()
        기능: durable journal용 준비 표식이 없는 직접 제출을 network 전에 차단하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        transport = QueueHTTPTransport([])
        client = _client(transport)

        # 준비되지 않은 주문은 /time과 /order 어느 쪽에도 전송할 수 없다.
        with self.assertRaises(OrderPreparationRequiredError):
            client.submit_order(order=_order())

        self.assertEqual(transport.requests, [])

    def test_prepare_is_idempotent_for_the_same_exact_order(self) -> None:
        """
        함수 이름: test_prepare_is_idempotent_for_the_same_exact_order()
        기능: 같은 객체·client ID·수량의 중복 prepare가 exchangeInfo를 재조회하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        transport = QueueHTTPTransport(
            [_json_response(_exchange_info_payload())]
        )
        client = _client(transport)
        order = _order()

        # 첫 prepare가 수량을 고정한 뒤 두 번째 prepare는 동일 객체를 그대로 반환한다.
        first_prepared_order = client.prepare_order(order)
        second_prepared_order = client.prepare_order(order)

        self.assertIs(first_prepared_order, order)
        self.assertIs(second_prepared_order, order)
        self.assertEqual(order.submitted_quantity, Decimal("1.234"))
        self.assertEqual(len(transport.requests), 1)

    def test_submit_rejects_quantity_changed_after_prepare(self) -> None:
        """
        함수 이름: test_submit_rejects_quantity_changed_after_prepare()
        기능: prepare와 durable journal 이후 제출 수량 mutation을 HTTP 전송 전에 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        transport = QueueHTTPTransport(
            [_json_response(_exchange_info_payload())]
        )
        client = _client(transport)
        order = client.prepare_order(_order())

        # journal에 1.234가 기록된 뒤 더 작은 값으로 바뀌어도 새 수량을 전송하지 않는다.
        order.submitted_quantity = Decimal("1.233")
        with self.assertRaises(OrderPreparationRequiredError):
            client.submit_order(order=order)

        self.assertEqual(len(transport.requests), 1)
        self.assertIn("/v3/exchangeInfo", transport.requests[0]["url"])

    def test_submit_rejects_side_changed_after_prepare(self) -> None:
        """
        함수 이름: test_submit_rejects_side_changed_after_prepare()
        기능: durable journal 이후 주문 방향 metadata mutation을 HTTP 전에 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        transport = QueueHTTPTransport(
            [_json_response(_exchange_info_payload())]
        )
        client = _client(transport)
        order = client.prepare_order(_order())

        # BUY journal을 SELL wire 요청으로 바꾸려는 mutation은 exact fingerprint와 다르다.
        order.side = OrderSide.SELL
        with self.assertRaises(OrderPreparationRequiredError):
            client.submit_order(order=order)

        self.assertEqual(len(transport.requests), 1)
        self.assertIn("/v3/exchangeInfo", transport.requests[0]["url"])

    def test_invalid_timestamp_resynchronizes_once_then_retries(self) -> None:
        """
        함수 이름: test_invalid_timestamp_resynchronizes_once_then_retries()
        기능: signed 요청의 -1021에서 server time을 다시 읽고 같은 operation을 한 번만 재시도하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        account_payload = {
            "accountType": "SPOT",
            "updateTime": FIXED_TIME_MILLISECONDS,
            "balances": [],
        }
        transport = QueueHTTPTransport(
            [
                _json_response({"serverTime": FIXED_TIME_MILLISECONDS}),
                _json_response(
                    {"code": -1021, "msg": "outside recvWindow"},
                    status_code=400,
                ),
                _json_response({"serverTime": FIXED_TIME_MILLISECONDS + 5}),
                _json_response(account_payload),
            ]
        )
        client = _client(transport)

        # 첫 account rejection 뒤 /time 한 번과 account retry 한 번만 발생해야 한다.
        result = client.get_account()

        self.assertEqual(result, account_payload)
        request_paths = tuple(
            urlsplit(request_value["url"]).path
            for request_value in transport.requests
        )
        self.assertEqual(
            request_paths,
            ("/api/v3/time", "/api/v3/account", "/api/v3/time", "/api/v3/account"),
        )

    def test_submit_timeout_5xx_and_minus_1007_are_unknown(self) -> None:
        """
        함수 이름: test_submit_timeout_5xx_and_minus_1007_are_unknown()
        기능: transport timeout, HTTP 5xx와 -1007이 실패 단정 없이 UNKNOWN인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        failure_responses = (
            TimeoutError("simulated read timeout"),
            _json_response(
                {"code": -1000, "msg": "internal"},
                status_code=500,
            ),
            _json_response(
                {"code": -1007, "msg": "execution status unknown"},
                status_code=400,
            ),
        )

        # 각 ambiguous failure를 독립 client로 실행해 숨은 state 공유를 배제한다.
        for failure_response in failure_responses:
            with self.subTest(failure_type=type(failure_response).__name__):
                transport = QueueHTTPTransport(
                    [
                        _json_response(_exchange_info_payload()),
                        _json_response({"serverTime": FIXED_TIME_MILLISECONDS}),
                        failure_response,
                    ]
                )
                client = _client(transport)
                order = client.prepare_order(_order())
                result = client.submit_order(order=order)

                self.assertEqual(result.status, OrderStatus.UNKNOWN)
                self.assertEqual(result.client_order_id, "bat-client-0")
                self.assertIsNone(result.exchange_order_id)

    def test_rate_limit_retry_after_is_clamped_to_thirty_seconds(self) -> None:
        """
        함수 이름: test_rate_limit_retry_after_is_clamped_to_thirty_seconds()
        기능: REST 429 Retry-After seconds가 domain 상한 30초를 넘지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        transport = QueueHTTPTransport(
            [
                _json_response(_exchange_info_payload()),
                _json_response({"serverTime": FIXED_TIME_MILLISECONDS}),
                _json_response(
                    {"code": -1003, "msg": "rate limited"},
                    status_code=429,
                    headers={"Retry-After": "90"},
                ),
            ]
        )

        # Rate-limit 제출은 실행 여부를 UNKNOWN으로 두고 긴 header는 30초로 제한한다.
        client = _client(transport)
        order = client.prepare_order(_order())
        result = client.submit_order(order=order)

        self.assertEqual(result.status, OrderStatus.UNKNOWN)
        self.assertEqual(result.retry_after, timedelta(seconds=30))
        self.assertNotIn("rate limited", result.failure_reason)

    def test_known_filter_4xx_is_terminal_rejected_with_safe_reason(self) -> None:
        """
        함수 이름: test_known_filter_4xx_is_terminal_rejected_with_safe_reason()
        기능: Matching Engine 미도달 filter 4xx를 fill 없는 REJECTED와 안전한 code로 보존하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        reflected_message = "Filter failure for bat-client-0 and secret text"
        transport = QueueHTTPTransport(
            [
                _json_response(_exchange_info_payload()),
                _json_response({"serverTime": FIXED_TIME_MILLISECONDS}),
                _json_response(
                    {"code": -1013, "msg": reflected_message},
                    status_code=400,
                ),
            ]
        )

        # 서버 message를 failure_reason에 반사하지 않고 숫자 code만 남긴다.
        client = _client(transport)
        order = client.prepare_order(_order())
        result = client.submit_order(order=order)

        self.assertEqual(result.status, OrderStatus.REJECTED)
        self.assertEqual(result.failure_reason, "BINANCE_SUBMISSION_REJECTED_-1013")
        self.assertIs(
            result.failure_kind,
            OrderResultFailureKind.SUBMISSION_REJECTED,
        )
        self.assertNotIn(reflected_message, result.failure_reason)
        self.assertEqual(result.fills, ())

    def test_query_no_such_order_preserves_typed_not_visible_fact(self) -> None:
        """
        함수 이름: test_query_no_such_order_preserves_typed_not_visible_fact()
        기능: 공식 -2013 query 오류를 즉시 terminal로 바꾸지 않고 typed UNKNOWN으로 보존한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        transport = QueueHTTPTransport(
            [
                _json_response({"serverTime": FIXED_TIME_MILLISECONDS}),
                _json_response(
                    {"code": -2013, "msg": "Order does not exist."},
                    status_code=400,
                ),
            ]
        )

        # Adapter는 비동기 조회 지연 가능성을 남기고 Controller의 bounded 정책에 사실만 전달한다.
        result = _client(transport).query_order_result(order=_order())

        self.assertIs(result.status, OrderStatus.UNKNOWN)
        self.assertIs(
            result.failure_kind,
            OrderResultFailureKind.ORDER_NOT_VISIBLE,
        )
        self.assertEqual(
            result.failure_reason,
            "BINANCE_ORDER_NOT_VISIBLE_-2013",
        )

    def test_matching_engine_duplicate_rejection_remains_unknown(self) -> None:
        """
        함수 이름: test_matching_engine_duplicate_rejection_remains_unknown()
        기능: -2010의 duplicate 가능성을 fill-free terminal 거부로 오판하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        transport = QueueHTTPTransport(
            [
                _json_response(_exchange_info_payload()),
                _json_response({"serverTime": FIXED_TIME_MILLISECONDS}),
                _json_response(
                    {"code": -2010, "msg": "Duplicate order sent."},
                    status_code=400,
                ),
            ]
        )

        # 같은 client ID가 이미 존재할 수 있으므로 신규 주문 실패가 아니라 same-ID 조회 상태로 둔다.
        client = _client(transport)
        order = client.prepare_order(_order())
        result = client.submit_order(order=order)

        self.assertIs(result.status, OrderStatus.UNKNOWN)
        self.assertIsNone(result.failure_kind)
        self.assertEqual(result.failure_reason, "BINANCE_SUBMISSION_UNKNOWN_-2010")
        self.assertEqual(result.fills, ())

    def test_configured_maximum_notional_blocks_order_before_signing(self) -> None:
        """
        함수 이름: test_configured_maximum_notional_blocks_order_before_signing()
        기능: bootstrap opt-in 금액 cap이 exchange filter를 통과한 실제 수량에 추가 적용되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        transport = QueueHTTPTransport(
            [_json_response(_exchange_info_payload(maximum_notional="100000"))]
        )
        client = _client(
            transport,
            maximum_order_notional=Decimal("100"),
        )

        # 1.234 ETH×100 USDT는 local cap 100을 넘으므로 journal·/time·POST 전에 거부한다.
        with self.assertRaisesRegex(
            SymbolFilterError,
            "FILTER_CONFIGURED_MAXIMUM_NOTIONAL",
        ):
            client.prepare_order(_order())

        self.assertEqual(len(transport.requests), 1)
        self.assertIn("/v3/exchangeInfo", transport.requests[0]["url"])

    def test_query_hydrates_partial_fill_from_my_trades(self) -> None:
        """
        함수 이름: test_query_hydrates_partial_fill_from_my_trades()
        기능: fill 배열이 없는 order query를 orderId 조건 myTrades와 병합하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        query_payload = {
            "symbol": "ETHUSDT",
            "orderId": 42,
            "clientOrderId": "bat-client-0",
            "price": "0.00000000",
            "origQty": "1.000",
            "executedQty": "0.400",
            "cummulativeQuoteQty": "40.00000000",
            "status": "PARTIALLY_FILLED",
            "timeInForce": "GTC",
            "type": "MARKET",
            "side": "BUY",
            "time": FIXED_TIME_MILLISECONDS,
            "updateTime": FIXED_TIME_MILLISECONDS,
            "origQuoteOrderQty": "0.00000000",
        }
        trade_payload = [
            {
                "symbol": "ETHUSDT",
                "id": 7001,
                "orderId": 42,
                "price": "100.00000000",
                "qty": "0.400",
                "quoteQty": "40.00000000",
                "commission": "0.04000000",
                "commissionAsset": "USDT",
                "time": FIXED_TIME_MILLISECONDS,
                "isBuyer": True,
                "isMaker": False,
                "isBestMatch": True,
            }
        ]
        transport = QueueHTTPTransport(
            [
                _json_response({"serverTime": FIXED_TIME_MILLISECONDS}),
                _json_response(query_payload),
                _json_response(trade_payload),
                _json_response(_exchange_info_payload()),
            ]
        )
        order = _order(quantity="1.000")
        order.exchange_order_id = "42"

        # Query response의 executedQty를 myTrades 한 fill과 일치시켜 회계 가능한 결과를 만든다.
        result = _client(transport).query_order_result(order=order)

        self.assertEqual(result.status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(len(result.fills), 1)
        self.assertEqual(result.fills[0].key, ("42", "7001"))
        self.assertEqual(result.fills[0].quantity, Decimal("0.400"))
        my_trades_query = parse_qs(urlsplit(transport.requests[2]["url"]).query)
        self.assertEqual(my_trades_query["orderId"], ["42"])
        self.assertEqual(my_trades_query["symbol"], ["ETHUSDT"])

    def test_list_open_results_filters_application_client_ids(self) -> None:
        """
        함수 이름: test_list_open_results_filters_application_client_ids()
        기능: openOrders를 symbol로 제한하고 application prefix 주문만 반환하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        open_orders = [
            {
                "symbol": "ETHUSDT",
                "orderId": 51,
                "clientOrderId": "bat-open-0",
                "price": "0.00000000",
                "origQty": "1.000",
                "executedQty": "0.000",
                "cummulativeQuoteQty": "0.00000000",
                "status": "NEW",
                "timeInForce": "GTC",
                "type": "MARKET",
                "side": "BUY",
                "time": FIXED_TIME_MILLISECONDS,
                "updateTime": FIXED_TIME_MILLISECONDS,
                "origQuoteOrderQty": "0.00000000",
            },
            {
                "symbol": "ETHUSDT",
                "orderId": 52,
                "clientOrderId": "manual-order",
                "price": "0.00000000",
                "origQty": "1.000",
                "executedQty": "0.000",
                "cummulativeQuoteQty": "0.00000000",
                "status": "NEW",
                "timeInForce": "GTC",
                "type": "MARKET",
                "side": "BUY",
                "time": FIXED_TIME_MILLISECONDS,
                "updateTime": FIXED_TIME_MILLISECONDS,
                "origQuoteOrderQty": "0.00000000",
            },
        ]
        transport = QueueHTTPTransport(
            [
                _json_response({"serverTime": FIXED_TIME_MILLISECONDS}),
                _json_response(open_orders),
            ]
        )

        # Symbol parameter를 포함한 signed openOrders에서 bat- prefix 하나만 선택한다.
        results = _client(transport).list_open_order_results()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].client_order_id, "bat-open-0")
        open_query = parse_qs(urlsplit(transport.requests[1]["url"]).query)
        self.assertEqual(open_query["symbol"], ["ETHUSDT"])


if __name__ == "__main__":
    unittest.main()
