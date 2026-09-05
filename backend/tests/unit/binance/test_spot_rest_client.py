"""BinanceSpotRESTClient의 Testnet signing·시간·오류·fill reconciliation을 검증한다."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.message import Message
import hashlib
import hmac
from io import BytesIO
import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
from urllib.request import (
    HTTPSHandler,
    Request,
    build_opener as build_standard_url_opener,
)
from urllib.response import addinfourl

from binance_auto_trader.adapters.binance import spot_rest_client as spot_rest_client_module
from binance_auto_trader.adapters.binance.spot_rest_client import (
    BinanceAPIError,
    BinancePayloadError,
    BinanceSpotRESTClient,
    HTTPTransportResponse,
    OrderPreparationRequiredError,
    UrllibHTTPTransport,
)
from binance_auto_trader.adapters.binance.mappers import (
    AccountAssetFilter,
    AccountOrderCountFilter,
    OrderPreparationFilterEvidence,
    OrderSubmissionAttemptEvidence,
    ReferencePrice,
    SymbolFilterError,
    SymbolTradingRules,
)
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.trading.order import (
    Order,
    OrderResultFailureKind,
    OrderStatus,
)
from binance_auto_trader.domain.trading.states import (
    ExitReason,
    OrderSide,
    StrategyType,
)


FIXED_TIME = datetime(2026, 8, 22, 5, 0, tzinfo=timezone.utc)
FIXED_TIME_MILLISECONDS = 1787374800000
API_KEY = "testnet-api-key"
SECRET_KEY = "testnet-secret-never-log"


class CrossOriginRedirectHTTPSHandler(HTTPSHandler):
    """
    클래스 이름: CrossOriginRedirectHTTPSHandler
    기능: 실제 network 없이 교차 origin 302와 후속 요청 여부를 기록한다.
    작성 날짜: 2026/08/31
    """

    def __init__(self, *, source_url: str, redirect_url: str) -> None:
        """
        함수 이름: __init__()
        기능: 최초 URL과 서로 다른 origin의 Location fixture를 설정한다.
        인자: source_url -> 302를 반환할 최초 URL
            redirect_url -> 자동 추적 시 열릴 교차 origin URL
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        super().__init__()

        # Source와 redirect target 요청을 분리해 인증 재전송 횟수를 정확히 계산한다.
        self.source_url = source_url
        self.redirect_url = redirect_url
        self.source_requests: list[dict[str, object]] = []
        self.redirect_requests: list[dict[str, object]] = []

    def https_open(self, request: Request) -> object:
        """
        함수 이름: https_open()
        기능: Source에는 302를, redirect target에는 200을 반환하고 요청을 기록한다.
        인자: request -> urllib opener가 전송하려는 HTTPS Request
        반환값: memory body를 사용하는 fake HTTP response
        작성 날짜: 2026/08/31
        """
        recorded_request = {
            "url": request.full_url,
            "headers": {
                header_name.lower(): header_value
                for header_name, header_value in request.header_items()
            },
            "body": request.data,
        }

        # Fail-closed handler가 없다면 302 target이 두 번째 요청으로 기록된다.
        if request.full_url == self.redirect_url:
            self.redirect_requests.append(recorded_request)
            return self._response(
                url=self.redirect_url,
                status_code=200,
                body=b'{"unexpected":"redirect-followed"}',
            )
        if request.full_url != self.source_url:
            raise AssertionError("unexpected fake HTTPS request URL")

        self.source_requests.append(recorded_request)
        return self._response(
            url=self.source_url,
            status_code=302,
            body=b'{"redirect":"blocked"}',
            location=self.redirect_url,
        )

    @staticmethod
    def _response(
        *,
        url: str,
        status_code: int,
        body: bytes,
        location: str | None = None,
    ) -> object:
        """
        함수 이름: _response()
        기능: urllib handler chain이 해석할 memory HTTP response를 만든다.
        인자: url -> 응답 URL
            status_code -> HTTP status
            body -> response body bytes
            location -> 선택 Location header
        반환값: addinfourl fake response
        작성 날짜: 2026/08/31
        """
        response_headers = Message()
        response_headers["Content-Type"] = "application/json"
        if location is not None:
            response_headers["Location"] = location

        # BytesIO body는 HTTPError 전환 후에도 transport가 원 3xx body를 읽게 한다.
        response = addinfourl(
            BytesIO(body),
            response_headers,
            url,
            code=status_code,
        )
        response.msg = "Found" if status_code == 302 else "OK"  # urllib의 HTTP reason을 재현한다.
        return response


class QueueHTTPTransport:
    """
    클래스 이름: QueueHTTPTransport
    기능: 응답·예외 queue를 순서대로 반환하고 실제 HTTP request 내용을 기록한다.
    작성 날짜: 2026/08/22
    """

    def __init__(
        self,
        responses: list[object],
        *,
        request_entry_hook: Callable[[str, str], None] | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 반환할 response queue와 빈 request 기록을 초기화한다.
        인자: responses -> HTTPTransportResponse 또는 발생시킬 Exception 목록
            request_entry_hook -> production guard 직전 호출할 선택 test hook
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # 테스트마다 독립 queue를 복사해 호출자가 제공한 목록을 변경하지 않는다.
        self.responses = list(responses)
        self.requests: list[dict[str, object]] = []
        self.request_entry_hook = request_entry_hook  # Scheduler 지연 fixture만 선택 주입한다.

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: int,
        before_send: Callable[[], None] | None = None,
    ) -> HTTPTransportResponse:
        """
        함수 이름: request()
        기능: 요청 값을 기록하고 queue 첫 응답을 반환하거나 예외를 발생시킨다.
        인자: method -> HTTP method
            url -> 전송 대상 URL
            headers -> request header mapping
            body -> form body 또는 None
            timeout_seconds -> 정수 timeout
            before_send -> 실제 fake I/O 직전에 실행할 production guard
        반환값: queue의 HTTPTransportResponse
        작성 날짜: 2026/08/22
        """
        # Transport 진입 지연을 먼저 재현한 뒤 production guard를 actual I/O 경계에서 실행한다.
        if self.request_entry_hook is not None:
            self.request_entry_hook(method, url)
        if before_send is not None:
            before_send()

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


class MutableUTCClock:
    """
    클래스 이름: MutableUTCClock
    기능: 준비 증거 expiry와 clock regression을 실제 대기 없이 재현한다.
    작성 날짜: 2026/08/31
    """

    def __init__(self, current_time: datetime) -> None:
        """
        함수 이름: __init__()
        기능: timezone-aware UTC 시작 시각을 mutable test state로 보존한다.
        인자: current_time -> 최초 반환할 UTC datetime
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        # Production clock이 요구하는 timezone-aware UTC만 mutable fixture로 허용한다.
        if current_time.tzinfo is None or current_time.utcoffset() != timedelta(0):
            raise ValueError("current_time must be a timezone-aware UTC datetime")

        self.current_time = current_time  # Test가 지정한 UTC 시각만 production clock 경계에 제공한다.

    def __call__(self) -> datetime:
        """
        함수 이름: __call__()
        기능: 현재 설정된 UTC 시각을 production client clock 형식으로 반환한다.
        인자: 없음
        반환값: timezone-aware UTC datetime
        작성 날짜: 2026/08/31
        """
        return self.current_time  # Wall-clock sleep 없이 요청 간 시간 경과를 결정론적으로 만든다.

    def advance(self, elapsed_time: timedelta) -> None:
        """
        함수 이름: advance()
        기능: 현재 시각을 지정한 양의 기간만큼 전진시킨다.
        인자: elapsed_time -> 전진할 양의 timedelta
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        # 0 또는 음수 이동은 advance와 regression test의 의도를 섞으므로 거부한다.
        if elapsed_time <= timedelta(0):
            raise ValueError("elapsed_time must be positive")

        self.current_time += elapsed_time  # Expiry 경계 밖의 submit 시각을 정확히 재현한다.

    def regress(self, elapsed_time: timedelta) -> None:
        """
        함수 이름: regress()
        기능: 현재 시각을 지정한 양의 기간만큼 후퇴시킨다.
        인자: elapsed_time -> 후퇴할 양의 timedelta
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        # 0 또는 음수 이동은 causal clock regression fixture로 허용하지 않는다.
        if elapsed_time <= timedelta(0):
            raise ValueError("elapsed_time must be positive")

        self.current_time -= elapsed_time  # Causal observation 이전으로 간 wall clock을 재현한다.


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
        "exchangeFilters": [],
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


def _account_filters_payload(
    *asset_filters: dict[str, object],
    exchange_filters: tuple[dict[str, object], ...] = (),
    symbol_filters: tuple[dict[str, object], ...] = (),
) -> dict[str, object]:
    """
    함수 이름: _account_filters_payload()
    기능: Submit-time signed myFilters에 사용할 공식 세 collection fixture를 만든다.
    인자: asset_filters -> assetFilters에 넣을 MAX_ASSET object들
        exchange_filters -> exchangeFilters에 넣을 account count object들
        symbol_filters -> symbolFilters에 넣을 symbol relevant object들
    반환값: strict parser가 소비할 myFilters payload
    작성 날짜: 2026/08/31
    """
    # 세 scope의 입력을 서로 섞지 않고 공식 root collection에 그대로 배치한다.
    return {
        "exchangeFilters": list(exchange_filters),
        "symbolFilters": list(symbol_filters),
        "assetFilters": list(asset_filters),
    }  # 호출자가 생략한 scope만 정확히 empty array로 남긴다.


def _reference_price_payload(
    *,
    price: str | None = "100",
    symbol: str = "ETHUSDT",
) -> dict[str, object]:
    """
    함수 이름: _reference_price_payload()
    기능: Submit-time public referencePrice의 설정 또는 null fixture를 만든다.
    인자: price -> 공식 decimal 문자열 또는 null
        symbol -> 응답에 포함할 canonical symbol
    반환값: 공식 referencePrice payload
    작성 날짜: 2026/08/31
    """
    # Exchange timestamp는 고정 clock과 같게 두어 evidence 비교를 결정론적으로 만든다.
    return {
        "symbol": symbol,
        "referencePrice": price,
        "timestamp": FIXED_TIME_MILLISECONDS,
    }


def _preparation_responses(
    *,
    exchange_info_payload: object | None = None,
    account_filters_payload: object | None = None,
    reference_price_payload: object | None = None,
    server_time_milliseconds: int = FIXED_TIME_MILLISECONDS,
    include_server_time: bool = True,
) -> list[HTTPTransportResponse]:
    """
    함수 이름: _preparation_responses()
    기능: prepare_order의 filter·all-symbol empty state·referencePrice 응답 순서를 만든다.
    인자: exchange_info_payload -> 선택 exchangeInfo payload
        account_filters_payload -> 선택 myFilters payload
        reference_price_payload -> 선택 referencePrice payload
        server_time_milliseconds -> 최초 signed read의 serverTime
        include_server_time -> client가 아직 offset을 갖지 않아 /time이 필요한지 여부
    반환값: QueueHTTPTransport에 전달할 prepare response 목록
    작성 날짜: 2026/08/31
    """
    selected_exchange_info = (
        _exchange_info_payload()
        if exchange_info_payload is None
        else exchange_info_payload
    )
    selected_account_filters = (
        _account_filters_payload()
        if account_filters_payload is None
        else account_filters_payload
    )
    selected_reference_price = (
        _reference_price_payload()
        if reference_price_payload is None
        else reference_price_payload
    )

    # Production 순서와 같이 optional /time 뒤 filter, 두 empty account state와 가격을 배치한다.
    responses: list[HTTPTransportResponse] = []
    if include_server_time:
        responses.append(
            _json_response({"serverTime": server_time_milliseconds})
        )
    responses.extend(
        (
            _json_response(selected_account_filters),
            _json_response(selected_exchange_info),
            _json_response([]),
            _json_response([]),
            _json_response(selected_reference_price),
        )
    )  # 기본 prepare fixture도 account-wide isolation의 완전한 zero를 증명한다.

    return responses


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
    allow_order_timestamp_retry: bool = True,
) -> BinanceSpotRESTClient:
    """
    함수 이름: _client()
    기능: 고정 clock과 주입 transport를 사용하는 credential-safe REST client를 만든다.
    인자: transport -> response queue와 request 기록을 가진 fake transport
        maximum_order_notional -> 선택 local BUY 진입 금액 상한
        allow_order_timestamp_retry -> POST /v3/order -1021 재전송 허용 여부
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
        allow_order_timestamp_retry=allow_order_timestamp_retry,
    )  # 실제 network 없이 production signing pipeline 전체를 실행한다.


class BinanceSpotRESTClientTests(unittest.TestCase):
    """
    클래스 이름: BinanceSpotRESTClientTests
    기능: 현행 Spot Testnet REST 계약과 안전한 OrderResult 분류를 검증한다.
    작성 날짜: 2026/08/22
    """

    def test_mainnet_klines_preserve_testnet_account_authentication(self) -> None:
        """
        함수 이름: test_mainnet_klines_preserve_testnet_account_authentication()
        기능: 실제 시세 GET에는 인증을 보내지 않고 계좌·시간 조회는 Testnet에 유지하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/05
        """
        # 공개 시세와 서명 계좌 조회를 연속 실행해 endpoint 및 credential 분리를 관찰한다.
        transport = QueueHTTPTransport([
            _json_response([]),
            _json_response({"serverTime": FIXED_TIME_MILLISECONDS}),
            _json_response({"balances": []}),
        ])
        client = BinanceSpotRESTClient(
            API_KEY, SECRET_KEY, transport=transport, clock=_fixed_clock,
            use_mainnet_market_data=True,
        )
        client.get_klines(symbol="ETHUSDT", interval="4h", limit=1000)
        client.get_account()

        market_request, time_request, account_request = transport.requests
        self.assertEqual(urlsplit(market_request["url"]).netloc, "data-api.binance.vision")
        self.assertNotIn("X-MBX-APIKEY", market_request["headers"])
        self.assertNotIn("signature", market_request["url"])
        self.assertEqual(urlsplit(time_request["url"]).netloc, "testnet.binance.vision")
        self.assertEqual(urlsplit(account_request["url"]).netloc, "testnet.binance.vision")
        self.assertEqual(account_request["headers"]["X-MBX-APIKEY"], API_KEY)  # 인증은 계좌 host에만 전달한다.

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

    def test_urllib_transport_does_not_follow_cross_origin_redirect(
        self,
    ) -> None:
        """
        함수 이름: test_urllib_transport_does_not_follow_cross_origin_redirect()
        기능: Signed POST의 302가 다른 origin으로 API key와 body를 재전송하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        source_url = "https://testnet.binance.vision/api/v3/order"
        redirect_url = "https://credential-sink.invalid/collect"
        unsigned_body = (
            b"symbol=ETHUSDT&side=BUY&type=MARKET&quantity=0.1"
            b"&timestamp=1787374800000"
        )
        signature = hmac.new(
            SECRET_KEY.encode("utf-8"),
            unsigned_body,
            hashlib.sha256,
        ).hexdigest()
        signed_body = unsigned_body + f"&signature={signature}".encode("ascii")
        redirect_handler = CrossOriginRedirectHTTPSHandler(
            source_url=source_url,
            redirect_url=redirect_url,
        )

        def build_fake_opener(*handlers: object) -> object:
            """
            함수 이름: build_fake_opener()
            기능: Production redirect handler와 memory HTTPS handler를 같은 opener에 연결한다.
            인자: handlers -> UrllibHTTPTransport가 등록하는 handler
            반환값: 실제 socket을 열지 않는 urllib opener
            작성 날짜: 2026/08/31
            """
            return build_standard_url_opener(
                *handlers,
                redirect_handler,
            )  # Fake HTTPSHandler가 표준 network handler를 대체한다.

        # Transport 생성 시점에만 opener factory를 교체해 production request 경로를 검증한다.
        with patch.object(
            spot_rest_client_module,
            "build_opener",
            side_effect=build_fake_opener,
        ):
            transport = UrllibHTTPTransport()
        response = transport.request(
            method="POST",
            url=source_url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "X-MBX-APIKEY": API_KEY,
            },
            body=signed_body,
            timeout_seconds=12,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers.get("Location"), redirect_url)
        self.assertEqual(len(redirect_handler.source_requests), 1)
        source_request = redirect_handler.source_requests[0]
        source_headers = source_request["headers"]
        if not isinstance(source_headers, dict):
            self.fail("source request headers must be recorded as a dict")
        self.assertEqual(source_headers.get("x-mbx-apikey"), API_KEY)
        self.assertEqual(source_request["body"], signed_body)

        # Redirect target request가 0건이면 API key header와 signed body 전달도 각각 0건이다.
        redirected_credentials = [
            request_value["headers"]
            for request_value in redirect_handler.redirect_requests
            if request_value["headers"]
        ]
        redirected_bodies = [
            request_value["body"]
            for request_value in redirect_handler.redirect_requests
            if request_value["body"] is not None
        ]
        self.assertEqual(redirect_handler.redirect_requests, [])
        self.assertEqual(redirected_credentials, [])
        self.assertEqual(redirected_bodies, [])

    def test_redirect_status_cannot_be_accepted_as_filled_order(self) -> None:
        """
        함수 이름: test_redirect_status_cannot_be_accepted_as_filled_order()
        기능: 302 body가 정상 FILLED 형태여도 signed 주문 성공으로 승격되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        transport = QueueHTTPTransport(
            [
                *_preparation_responses(),
                _json_response(
                    _filled_order_payload(),
                    status_code=302,
                    headers={"Location": "https://credential-sink.invalid/collect"},
                ),
            ]
        )
        client = _client(transport)
        prepared_order = client.prepare_order(_order())

        # 2xx이 아닌 원 응답은 payload 모양과 무관하게 조회 대상 UNKNOWN으로 유지한다.
        result = client.submit_order(order=prepared_order)

        self.assertIs(result.status, OrderStatus.UNKNOWN)
        self.assertIsNone(result.exchange_order_id)
        self.assertEqual(len(transport.requests), 7)

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
                *_preparation_responses(),
                _json_response(_filled_order_payload()),
            ]
        )
        client = _client(transport)
        order = _order()

        # Journal 전 prepare 결과와 이후 signed POST가 같은 exact 수량을 사용해야 한다.
        prepared_order = client.prepare_order(order)
        self.assertIsNone(
            client.get_order_submission_attempt_evidence(
                client_order_id=order.client_order_id
            )
        )
        result = client.submit_order(order=prepared_order)
        submission_evidence = client.get_order_submission_attempt_evidence(
            client_order_id=order.client_order_id
        )

        self.assertEqual(result.status, OrderStatus.FILLED)
        self.assertIsInstance(
            submission_evidence,
            OrderSubmissionAttemptEvidence,
        )
        if submission_evidence is None:
            self.fail("successful POST must expose immutable submission evidence")
        self.assertEqual(order.intent_id, submission_evidence.intent_id)
        self.assertEqual(order.client_order_id, submission_evidence.client_order_id)
        self.assertIs(OrderSide.BUY, submission_evidence.side)
        self.assertEqual(FIXED_TIME, submission_evidence.attempted_at)
        self.assertEqual(order.requested_quantity, Decimal("1.23456"))
        self.assertEqual(order.submitted_quantity, Decimal("1.234"))
        self.assertEqual(len(result.fills), 1)
        post_request = next(
            request_value
            for request_value in transport.requests
            if request_value["method"] == "POST"
        )
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

        # 공식 fill tradeId의 non-negative 범위를 벗어난 FULL 응답은 durable fill로 확정하지 않는다.
        negative_trade_payload = _filled_order_payload(
            client_order_id="bat-client-negative-trade-id"
        )
        negative_trade_payload["fills"][0]["tradeId"] = -1
        negative_transport = QueueHTTPTransport(
            [
                *_preparation_responses(),
                _json_response(negative_trade_payload),
            ]
        )
        negative_client = _client(negative_transport)
        negative_order = _order(
            client_order_id="bat-client-negative-trade-id"
        )
        prepared_negative_order = negative_client.prepare_order(negative_order)
        negative_result = negative_client.submit_order(
            order=prepared_negative_order
        )
        self.assertIs(negative_result.status, OrderStatus.UNKNOWN)
        self.assertEqual(
            "BINANCE_PAYLOAD_RECONCILIATION_REQUIRED",
            negative_result.failure_reason,
        )

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

    def test_get_klines_directly_supports_all_communication_intervals(self) -> None:
        """
        함수 이름: test_get_klines_directly_supports_all_communication_intervals()
        기능: Communication 1.2.1~1.2.4의 네 interval이 production REST owner에 그대로 전달되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        kline_row = [
            FIXED_TIME_MILLISECONDS,
            "100.00000000",
            "110.00000000",
            "90.00000000",
            "105.00000000",
            "1.50000000",
            FIXED_TIME_MILLISECONDS + 59_999,
            "157.50000000",
            12,
            "0.70000000",
            "73.50000000",
            "0",
        ]
        communication_intervals = ("1m", "30m", "4h", "1d")
        transport = QueueHTTPTransport(
            [_json_response([kline_row]) for _interval in communication_intervals]
        )
        client = _client(transport)

        # 네 메시지는 같은 production operation을 사용하므로 각 interval의 요청·반환을 직접 확인한다.
        for interval in communication_intervals:
            with self.subTest(interval=interval):
                payload = client.get_klines(
                    symbol="ethusdt",
                    interval=interval,
                    limit=2,
                )
                request = transport.requests[-1]
                query = parse_qs(urlsplit(str(request["url"])).query)

                self.assertEqual(payload, [kline_row])
                self.assertEqual(request["method"], "GET")
                self.assertEqual(query["symbol"], ["ETHUSDT"])
                self.assertEqual(query["interval"], [interval])
                self.assertEqual(query["limit"], ["2"])
                self.assertNotIn("X-MBX-APIKEY", request["headers"])

    def test_get_klines_directly_rejects_non_array_payload(self) -> None:
        """
        함수 이름: test_get_klines_directly_rejects_non_array_payload()
        기능: production REST owner가 Kline object 응답을 부분 결과로 반환하지 않고 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        transport = QueueHTTPTransport([_json_response("unexpected")])
        client = _client(transport)

        # Top-level array가 아닌 응답은 APIGateway mapper에 전달되기 전에 typed payload 오류가 된다.
        with self.assertRaisesRegex(BinancePayloadError, "must be an array"):
            client.get_klines(symbol="ETHUSDT", interval="1m", limit=1)

        self.assertEqual(len(transport.requests), 1)  # 거부된 응답을 보완하려 재요청하지 않는다.

    def test_get_account_directly_signs_and_returns_object_payload(self) -> None:
        """
        함수 이름: test_get_account_directly_signs_and_returns_object_payload()
        기능: Communication 2.1.1의 production REST owner가 signed account object를 반환하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        account_payload = {
            "makerCommission": 10,
            "takerCommission": 10,
            "canTrade": True,
            "balances": [],
        }
        transport = QueueHTTPTransport(
            [
                _json_response({"serverTime": FIXED_TIME_MILLISECONDS}),
                _json_response(account_payload),
            ]
        )
        client = _client(transport)

        # Signed account 조회는 server-time 동기화 뒤 API key header와 HMAC query를 사용한다.
        payload = client.get_account()
        account_request = transport.requests[1]
        query = parse_qs(urlsplit(str(account_request["url"])).query)

        self.assertEqual(payload, account_payload)
        self.assertEqual(account_request["method"], "GET")
        self.assertEqual(account_request["headers"]["X-MBX-APIKEY"], API_KEY)
        self.assertEqual(query["omitZeroBalances"], ["false"])
        self.assertIn("timestamp", query)
        self.assertIn("signature", query)

    def test_get_account_directly_rejects_non_object_payload(self) -> None:
        """
        함수 이름: test_get_account_directly_rejects_non_object_payload()
        기능: production REST owner가 account array 응답을 authoritative snapshot으로 허용하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        transport = QueueHTTPTransport(
            [
                _json_response({"serverTime": FIXED_TIME_MILLISECONDS}),
                _json_response([]),
            ]
        )
        client = _client(transport)

        # Signed transport가 성공해도 schema root가 틀리면 account payload를 즉시 폐기한다.
        with self.assertRaisesRegex(BinancePayloadError, "must be an object"):
            client.get_account()

        self.assertEqual(len(transport.requests), 2)  # time sync와 account 요청 외 mutation은 없다.

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
            _preparation_responses()
        )
        client = _client(transport)
        order = _order()

        # 첫 prepare가 수량을 고정한 뒤 두 번째 prepare는 동일 객체를 그대로 반환한다.
        first_prepared_order = client.prepare_order(order)
        second_prepared_order = client.prepare_order(order)

        self.assertIs(first_prepared_order, order)
        self.assertIs(second_prepared_order, order)
        self.assertEqual(order.submitted_quantity, Decimal("1.234"))
        self.assertEqual(len(transport.requests), 6)

    def test_duplicate_prepare_rejects_expired_cached_evidence(self) -> None:
        """
        함수 이름: test_duplicate_prepare_rejects_expired_cached_evidence()
        기능: 같은 주문의 cached prepare도 고정 30초를 넘으면 재사용하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        # 실제 I/O 없이 prepare 완료 시각과 이후 cached 호출 시각을 분리한다.
        transport = QueueHTTPTransport(_preparation_responses())
        mutable_clock = MutableUTCClock(FIXED_TIME)
        client = BinanceSpotRESTClient(
            API_KEY,
            SECRET_KEY,
            transport=transport,
            clock=mutable_clock,
            result_clock=_fixed_clock,
        )
        order = client.prepare_order(_order())
        mutable_clock.advance(timedelta(seconds=31))

        # Cached fingerprint가 같아도 stale account/filter state는 network 없이 차단한다.
        with self.assertRaisesRegex(
            OrderPreparationRequiredError,
            "filter evidence expired",
        ):
            client.prepare_order(order)

        self.assertEqual(len(transport.requests), 6)  # 추가 read나 POST가 전혀 발생하지 않는다.

    def test_submit_rejects_expired_preparation_before_post(self) -> None:
        """
        함수 이름: test_submit_rejects_expired_preparation_before_post()
        기능: 준비의 첫 composite 관찰 후 30초를 넘긴 제출을 POST 전에 차단하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        # Mutable clock으로 composite 관찰과 제출 사이의 30초+1ms 경과를 재현한다.
        transport = QueueHTTPTransport(_preparation_responses())
        mutable_clock = MutableUTCClock(FIXED_TIME)
        client = BinanceSpotRESTClient(
            API_KEY,
            SECRET_KEY,
            transport=transport,
            clock=mutable_clock,
            result_clock=_fixed_clock,
        )
        order = client.prepare_order(_order())
        mutable_clock.advance(
            timedelta(seconds=30, milliseconds=1)
        )

        # Expired 표식은 한 번 소모하고 submission evidence나 order POST를 만들지 않는다.
        with self.assertRaisesRegex(
            OrderPreparationRequiredError,
            "filter evidence expired",
        ):
            client.submit_order(order=order)

        self.assertEqual(len(transport.requests), 6)
        self.assertIsNone(
            client.get_order_submission_attempt_evidence(
                client_order_id=order.client_order_id
            )
        )  # Attempt evidence도 POST 직전에만 생성한다.
        with self.assertRaisesRegex(
            OrderPreparationRequiredError,
            "order must be prepared",
        ):
            client.submit_order(order=order)

        self.assertEqual(len(transport.requests), 6)  # Expired fingerprint는 다시 제출할 수 없다.

    def test_submit_rechecks_expiry_after_consuming_fingerprint(self) -> None:
        """
        함수 이름: test_submit_rechecks_expiry_after_consuming_fingerprint()
        기능: 표식 소비 직후 process가 지연돼도 실제 POST 직전 freshness를 다시 검사하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        mutable_clock = MutableUTCClock(FIXED_TIME)

        def delay_at_transport_entry(method: str, url: str) -> None:
            """
            함수 이름: delay_at_transport_entry()
            기능: Order transport 진입 뒤 production before-send guard 직전에 31초를 전진시킨다.
            인자: method -> fake transport가 받은 HTTP method
                url -> fake transport가 받은 absolute request URL
            반환값: 없음
            작성 날짜: 2026/08/31
            """
            if method == "POST" and urlsplit(url).path == "/api/v3/order":
                mutable_clock.advance(timedelta(seconds=31))

            return None  # GET 준비 요청은 움직이지 않고 실제 order I/O 경계만 지연시킨다.

        # Fingerprint 소비 뒤 transport가 실제 요청을 기록하기 직전의 scheduler 지연을 재현한다.
        transport = QueueHTTPTransport(
            _preparation_responses(),
            request_entry_hook=delay_at_transport_entry,
        )
        client = BinanceSpotRESTClient(
            API_KEY,
            SECRET_KEY,
            transport=transport,
            clock=mutable_clock,
            result_clock=_fixed_clock,
        )
        order = client.prepare_order(_order())

        # Transport before-send guard는 지연 뒤 stale evidence를 보고 fake I/O도 시작하지 않는다.
        with self.assertRaisesRegex(
            OrderPreparationRequiredError,
            "filter evidence expired",
        ):
            client.submit_order(order=order)

        self.assertEqual(len(transport.requests), 6)
        self.assertIsNone(
            client.get_order_submission_attempt_evidence(
                client_order_id=order.client_order_id
            )
        )  # 지연된 attempt는 evidence에도 기록하지 않는다.

    def test_submit_allows_exact_preparation_age_boundary(self) -> None:
        """
        함수 이름: test_submit_allows_exact_preparation_age_boundary()
        기능: Composite 첫 관찰 후 정확히 30초인 transport attempt는 허용하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        # 성공 POST 응답을 포함하고 clock을 고정 경계와 정확히 같게 전진시킨다.
        transport = QueueHTTPTransport(
            [*_preparation_responses(), _json_response(_filled_order_payload())]
        )
        mutable_clock = MutableUTCClock(FIXED_TIME)
        client = BinanceSpotRESTClient(
            API_KEY,
            SECRET_KEY,
            transport=transport,
            clock=mutable_clock,
            result_clock=_fixed_clock,
        )
        order = client.prepare_order(_order())
        mutable_clock.advance(timedelta(seconds=30))

        # `>` 경계만 만료이므로 정확한 30초 attempt는 한 번의 POST와 evidence를 만든다.
        result = client.submit_order(order=order)
        submission_evidence = client.get_order_submission_attempt_evidence(
            client_order_id=order.client_order_id
        )

        self.assertIs(result.status, OrderStatus.FILLED)
        self.assertIsNotNone(submission_evidence)
        if submission_evidence is None:
            self.fail("exact boundary submission must retain attempt evidence")
        self.assertEqual(
            FIXED_TIME + timedelta(seconds=30),
            submission_evidence.attempted_at,
        )  # Attempt timestamp는 before-send guard의 exact boundary 시각이다.

    def test_submit_rejects_preparation_clock_regression(self) -> None:
        """
        함수 이름: test_submit_rejects_preparation_clock_regression()
        기능: 제출 clock이 composite 관찰보다 뒤로 가면 age를 음수로 통과시키지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        # Composite 관찰 뒤 server-time clock만 1ms 후퇴시키는 fixture를 만든다.
        transport = QueueHTTPTransport(_preparation_responses())
        mutable_clock = MutableUTCClock(FIXED_TIME)
        client = BinanceSpotRESTClient(
            API_KEY,
            SECRET_KEY,
            transport=transport,
            clock=mutable_clock,
            result_clock=_fixed_clock,
        )
        order = client.prepare_order(_order())
        mutable_clock.regress(timedelta(milliseconds=1))

        # Server-time causal axis가 후퇴하면 stale 여부를 추측하지 않고 제출 표식을 폐기한다.
        with self.assertRaisesRegex(
            OrderPreparationRequiredError,
            "evidence clock regressed",
        ):
            client.submit_order(order=order)

        self.assertEqual(len(transport.requests), 6)
        self.assertIsNone(
            client.get_order_submission_attempt_evidence(
                client_order_id=order.client_order_id
            )
        )  # Clock 오류도 POST나 attempt evidence를 허용하지 않는다.

    def test_public_symbol_rules_fetch_is_fresh_unsigned_and_strict(self) -> None:
        """
        함수 이름: test_public_symbol_rules_fetch_is_fresh_unsigned_and_strict()
        기능: 동일 symbol의 연속 조회가 cache 대신 새 public exchangeInfo를 엄격 해석하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        transport = QueueHTTPTransport(
            [
                _json_response(
                    _exchange_info_payload(minimum_notional="10")
                ),
                _json_response(
                    _exchange_info_payload(minimum_notional="12")
                ),
            ]
        )
        client = _client(transport)

        # 두 호출은 독립 GET을 만들고 두 번째 응답의 변경된 filter를 즉시 반영한다.
        first_rules = client.fetch_symbol_trading_rules(symbol="ethusdt")
        second_rules = client.fetch_symbol_trading_rules(symbol="ETHUSDT")

        self.assertIsInstance(first_rules, SymbolTradingRules)
        self.assertIsInstance(second_rules, SymbolTradingRules)
        self.assertEqual(
            first_rules.notional_filters[0].minimum_notional,
            Decimal("10"),
        )
        self.assertEqual(
            second_rules.notional_filters[0].minimum_notional,
            Decimal("12"),
        )
        self.assertEqual(len(transport.requests), 2)
        for request_value in transport.requests:
            parsed_url = urlsplit(request_value["url"])
            self.assertEqual(request_value["method"], "GET")
            self.assertEqual(parsed_url.path, "/api/v3/exchangeInfo")
            self.assertEqual(
                parse_qs(parsed_url.query),
                {"symbol": ["ETHUSDT"]},
            )
            self.assertNotIn("X-MBX-APIKEY", request_value["headers"])
            self.assertIsNone(request_value["body"])

    def test_prepare_uses_reference_price_for_notional_validation(self) -> None:
        """
        함수 이름: test_prepare_uses_reference_price_for_notional_validation()
        기능: Decision price가 통과해도 fresh reference price notional이 낮으면 prepare를 차단하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        transport = QueueHTTPTransport(
            _preparation_responses(
                exchange_info_payload=_exchange_info_payload(
                    minimum_notional="10"
                ),
                reference_price_payload=_reference_price_payload(price="5"),
            )
        )
        client = _client(transport)
        order = _order(quantity="1.234")

        # Decision notional 123.4와 달리 공식 reference notional 6.17은 최소 10을 통과하지 못한다.
        with self.assertRaisesRegex(SymbolFilterError, "NOTIONAL_MINIMUM"):
            client.prepare_order(order)

        self.assertIsNone(
            client.get_order_preparation_filter_evidence(
                client_order_id=order.client_order_id
            )
        )
        self.assertEqual(len(transport.requests), 6)

    def test_prepare_quote_max_asset_requires_official_price_formula(
        self,
    ) -> None:
        """
        함수 이름: test_prepare_quote_max_asset_requires_official_price_formula()
        기능: Quantity MARKET의 공식 환산식이 없는 quote MAX_ASSET를 추정 없이 차단하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        transport = QueueHTTPTransport(
            _preparation_responses(
                account_filters_payload=_account_filters_payload(
                    {
                        "filterType": "MAX_ASSET",
                        "asset": "USDT",
                        "limit": "120",
                    }
                ),
                reference_price_payload=_reference_price_payload(price="100"),
            )
        )
        client = _client(transport)
        order = _order(quantity="1.23456")

        # Reference price가 있어도 MAX_ASSET 문서에 quantity MARKET quote 환산식이 없으므로 추정하지 않는다.
        with self.assertRaisesRegex(
            SymbolFilterError,
            "FILTER_MAX_ASSET_QUOTE_MARKET_PRICE_UNDEFINED",
        ):
            client.prepare_order(order)

        self.assertIsNone(
            client.get_order_preparation_filter_evidence(
                client_order_id=order.client_order_id
            )
        )
        self.assertEqual(len(transport.requests), 6)  # 실패 뒤 POST request는 생성되지 않는다.

    def test_prepare_count_filters_record_complete_empty_state_evidence(
        self,
    ) -> None:
        """
        함수 이름: test_prepare_count_filters_record_complete_empty_state_evidence()
        기능: Composite count filter가 두 all-symbol empty snapshot과 전체 provenance를 요구하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        exchange_count_filter = {
            "filterType": "EXCHANGE_MAX_NUM_ORDERS",
            "maxNumOrders": 1000,
        }
        symbol_count_filter = {
            "filterType": "MAX_NUM_ORDER_LISTS",
            "maxNumOrderLists": 20,
        }
        account_filters_payload = _account_filters_payload(
            exchange_filters=(exchange_count_filter,),
            symbol_filters=(symbol_count_filter,),
        )
        exchange_info_payload = _exchange_info_payload()
        exchange_info_payload["exchangeFilters"] = [
            dict(exchange_count_filter)
        ]
        exchange_info_payload["symbols"][0]["filters"].append(
            dict(symbol_count_filter)
        )
        transport = QueueHTTPTransport(
            [
                _json_response({"serverTime": FIXED_TIME_MILLISECONDS}),
                _json_response(account_filters_payload),
                _json_response(exchange_info_payload),
                _json_response([]),
                _json_response([]),
                _json_response(_reference_price_payload()),
            ]
        )
        client = _client(transport)

        # Count filter가 있으면 openOrders와 openOrderList가 모두 empty인 뒤에만 prepare가 완성된다.
        prepared_order = client.prepare_order(
            _order()
        )  # POST 없이 immutable preparation evidence까지만 생성한다.
        evidence = client.get_order_preparation_filter_evidence(
            client_order_id=prepared_order.client_order_id
        )
        if evidence is None:
            self.fail("successful count-filter prepare must expose evidence")

        self.assertEqual(prepared_order.intent_id, evidence.intent_id)
        self.assertEqual(
            prepared_order.client_order_id,
            evidence.client_order_id,
        )
        self.assertIs(OrderSide.BUY, evidence.side)
        self.assertEqual("ETHUSDT", evidence.rules.symbol)
        self.assertEqual(
            evidence.account_filters.exchange_order_count_filters,
            (
                AccountOrderCountFilter(
                    filter_type="EXCHANGE_MAX_NUM_ORDERS",
                    maximum_count=1000,
                ),
            ),
        )
        self.assertEqual(
            evidence.account_filters.symbol_order_count_filters,
            (
                AccountOrderCountFilter(
                    filter_type="MAX_NUM_ORDER_LISTS",
                    maximum_count=20,
                ),
            ),
        )
        self.assertTrue(evidence.account_open_state_verified_empty)
        self.assertEqual(
            FIXED_TIME,
            evidence.account_filters_observed_at,
        )
        self.assertEqual(
            FIXED_TIME,
            evidence.account_open_orders_observed_at,
        )
        self.assertEqual(
            FIXED_TIME,
            evidence.account_open_order_lists_observed_at,
        )
        self.assertEqual(FIXED_TIME, evidence.observed_at)
        self.assertEqual(
            FIXED_TIME,
            evidence.reference_price_observed_at,
        )
        self.assertEqual(
            ReferencePrice(
                symbol="ETHUSDT",
                price=Decimal("100"),
                exchange_timestamp=FIXED_TIME_MILLISECONDS,
            ),
            evidence.reference_price,
        )
        self.assertEqual((), evidence.account_asset_filters)
        self.assertEqual(
            tuple(urlsplit(value["url"]).path for value in transport.requests),
            (
                "/api/v3/time",
                "/api/v3/myFilters",
                "/api/v3/exchangeInfo",
                "/api/v3/openOrders",
                "/api/v3/openOrderList",
                "/api/v3/referencePrice",
            ),
        )
        self.assertFalse(
            any(
                request_value["method"] == "POST"
                for request_value in transport.requests
            )
        )

    def test_prepare_count_filters_reject_nonempty_state_before_post(
        self,
    ) -> None:
        """
        함수 이름: test_prepare_count_filters_reject_nonempty_state_before_post()
        기능: All-symbol order 또는 order-list 한 건이라도 있으면 evidence와 POST 전에 차단하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        exchange_count_filter = {
            "filterType": "EXCHANGE_MAX_NUM_ORDERS",
            "maxNumOrders": 1000,
        }
        symbol_count_filter = {
            "filterType": "MAX_NUM_ORDER_LISTS",
            "maxNumOrderLists": 20,
        }
        nonempty_cases = (
            (
                "open-order",
                [{"orderId": 81}],
                [],
                "FILTER_ACCOUNT_OPEN_ORDER_STATE_NOT_EMPTY",
            ),
            (
                "open-order-list",
                [],
                [{"orderListId": 91}],
                "FILTER_ACCOUNT_OPEN_ORDER_LIST_STATE_NOT_EMPTY",
            ),
        )

        # 각 account state를 새 client로 분리해 첫 실패가 다음 signed-read 순서에 영향을 주지 않게 한다.
        for (
            case_name,
            open_orders_payload,
            open_order_lists_payload,
            expected_error,
        ) in nonempty_cases:
            with self.subTest(case_name=case_name):
                account_filters_payload = _account_filters_payload(
                    exchange_filters=(exchange_count_filter,),
                    symbol_filters=(symbol_count_filter,),
                )
                exchange_info_payload = _exchange_info_payload()
                exchange_info_payload["exchangeFilters"] = [
                    dict(exchange_count_filter)
                ]
                exchange_info_payload["symbols"][0]["filters"].append(
                    dict(symbol_count_filter)
                )
                responses = [
                    _json_response(
                        {"serverTime": FIXED_TIME_MILLISECONDS}
                    ),
                    _json_response(account_filters_payload),
                    _json_response(exchange_info_payload),
                    _json_response(open_orders_payload),
                ]
                if not open_orders_payload:
                    responses.append(
                        _json_response(open_order_lists_payload)
                    )
                transport = QueueHTTPTransport(responses)
                client = _client(transport)
                order = _order()

                with self.assertRaisesRegex(
                    SymbolFilterError,
                    f"^{expected_error}$",
                ):
                    client.prepare_order(order)

                request_paths = tuple(
                    urlsplit(value["url"]).path
                    for value in transport.requests
                )  # Non-empty가 발견된 exact signed snapshot 뒤 즉시 중단한다.
                expected_paths = (
                    "/api/v3/time",
                    "/api/v3/myFilters",
                    "/api/v3/exchangeInfo",
                    "/api/v3/openOrders",
                )
                if not open_orders_payload:
                    expected_paths += ("/api/v3/openOrderList",)
                self.assertEqual(expected_paths, request_paths)
                self.assertIsNone(
                    client.get_order_preparation_filter_evidence(
                        client_order_id=order.client_order_id
                    )
                )
                self.assertFalse(
                    any(
                        request_value["method"] == "POST"
                        for request_value in transport.requests
                    )
                )

    def test_prepare_buy_fails_closed_when_max_position_is_present(
        self,
    ) -> None:
        """
        함수 이름: test_prepare_buy_fails_closed_when_max_position_is_present()
        기능: Submit-time exchangeInfo의 MAX_POSITION이 account exposure 추정 없이 BUY를 막는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        exchange_info_payload = _exchange_info_payload()
        exchange_info_payload["symbols"][0]["filters"].append(
            {
                "filterType": "MAX_POSITION",
                "maxPosition": "10.00000000",
            }
        )
        transport = QueueHTTPTransport(
            _preparation_responses(
                exchange_info_payload=exchange_info_payload
            )
        )
        client = _client(transport)

        # Base balance와 open BUY 총량을 확보하지 않은 narrow path는 MAX_POSITION presence를 차단한다.
        with self.assertRaisesRegex(
            SymbolFilterError,
            "FILTER_MAX_POSITION_REQUIRES_ACCOUNT_EXPOSURE",
        ):
            client.prepare_order(_order())

        self.assertEqual(len(transport.requests), 6)

    def test_public_symbol_rules_fetch_rejects_mismatched_payload(self) -> None:
        """
        함수 이름: test_public_symbol_rules_fetch_rejects_mismatched_payload()
        기능: exchangeInfo의 symbol이 요청 symbol과 다르면 cache가 아닌 엄격 parser 오류로 차단하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        mismatched_payload = _exchange_info_payload()
        mismatched_payload["symbols"][0]["symbol"] = "BTCUSDT"
        transport = QueueHTTPTransport(
            [_json_response(mismatched_payload)]
        )
        client = _client(transport)

        # Public DTO에 요청하지 않은 symbol을 대입하지 않도록 parser 실패를 그대로 보존한다.
        with self.assertRaises(BinancePayloadError):
            client.fetch_symbol_trading_rules(symbol="ETHUSDT")

        self.assertEqual(len(transport.requests), 1)

    def test_prepare_refetches_authoritative_rules_after_public_preflight(
        self,
    ) -> None:
        """
        함수 이름: test_prepare_refetches_authoritative_rules_after_public_preflight()
        기능: public preflight cache가 있어도 prepare가 submit-time exchangeInfo를 다시 읽어 최종 수량을 정하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        preflight_payload = _exchange_info_payload()
        submit_time_payload = _exchange_info_payload()
        submit_filters = submit_time_payload["symbols"][0]["filters"]
        submit_filters[1]["stepSize"] = "0.01"
        transport = QueueHTTPTransport(
            [
                _json_response(preflight_payload),
                *_preparation_responses(
                    exchange_info_payload=submit_time_payload,
                    account_filters_payload=_account_filters_payload(
                        {
                            "filterType": "MAX_ASSET",
                            "asset": "ETH",
                            "limit": "2.00000000",
                        }
                    ),
                ),
            ]
        )
        client = _client(transport)
        order = _order()

        # Preflight의 0.001 step을 재사용하지 않고 두 번째 GET의 0.01 step이 pending 대상 수량을 결정한다.
        client.fetch_symbol_trading_rules(symbol="ETHUSDT")
        self.assertIsNone(
            client.get_order_preparation_filter_evidence(
                client_order_id=order.client_order_id
            )
        )
        prepared_order = client.prepare_order(order)
        filter_evidence = client.get_order_preparation_filter_evidence(
            client_order_id=order.client_order_id
        )

        self.assertEqual(prepared_order.submitted_quantity, Decimal("1.23"))
        self.assertIsInstance(
            filter_evidence,
            OrderPreparationFilterEvidence,
        )
        if filter_evidence is None:
            self.fail("successful prepare must expose immutable filter evidence")
        self.assertEqual(order.intent_id, filter_evidence.intent_id)
        self.assertEqual(order.client_order_id, filter_evidence.client_order_id)
        self.assertIs(OrderSide.BUY, filter_evidence.side)
        self.assertEqual(FIXED_TIME, filter_evidence.observed_at)
        self.assertEqual(
            Decimal("0.01"),
            filter_evidence.rules.market_lot_size.step_size,
        )
        self.assertEqual(
            filter_evidence.account_asset_filters,
            (
                AccountAssetFilter(
                    filter_type="MAX_ASSET",
                    asset="ETH",
                    maximum_quantity=Decimal("2.00000000"),
                ),
            ),
        )
        self.assertEqual(
            FIXED_TIME,
            filter_evidence.account_filters_observed_at,
        )
        self.assertEqual(
            filter_evidence.reference_price,
            ReferencePrice(
                symbol="ETHUSDT",
                price=Decimal("100"),
                exchange_timestamp=FIXED_TIME_MILLISECONDS,
            ),
        )
        self.assertEqual(
            FIXED_TIME,
            filter_evidence.reference_price_observed_at,
        )
        self.assertEqual(len(transport.requests), 7)
        self.assertEqual(
            tuple(urlsplit(value["url"]).path for value in transport.requests),
            (
                "/api/v3/exchangeInfo",
                "/api/v3/time",
                "/api/v3/myFilters",
                "/api/v3/exchangeInfo",
                "/api/v3/openOrders",
                "/api/v3/openOrderList",
                "/api/v3/referencePrice",
            ),
        )

    def test_prepare_submission_and_missing_result_time_fail_closed_on_run_axis(self) -> None:
        """
        함수 이름: test_prepare_submission_and_missing_result_time_fail_closed_on_run_axis()
        기능: signed filter·POST 시각축과 timestamp 누락 성공 응답의 fail-closed 조정을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/01
        """
        server_offset = timedelta(seconds=60)
        server_time_milliseconds = FIXED_TIME_MILLISECONDS + 60_000
        transport = QueueHTTPTransport(
            [
                _json_response({"serverTime": server_time_milliseconds}),
                _json_response({}),
                *_preparation_responses(
                    server_time_milliseconds=server_time_milliseconds,
                    include_server_time=False,
                ),
                _json_response(_filled_order_payload()),
            ]
        )

        def skewed_result_clock() -> datetime:
            """
            함수 이름: skewed_result_clock()
            기능: provenance가 사용하면 causal ordering을 깨뜨릴 하루 느린 local clock을 반환한다.
            인자: 없음
            반환값: timezone-aware UTC datetime
            작성 날짜: 2026/08/31
            """
            return FIXED_TIME - timedelta(days=1)

        client = BinanceSpotRESTClient(
            API_KEY,
            SECRET_KEY,
            transport=transport,
            clock=_fixed_clock,
            result_clock=skewed_result_clock,
        )
        order = _order()

        # Signed commission이 server offset을 만든 뒤 prepare와 submit 모두 같은 server-aligned clock을 읽는다.
        client.get_account_commission(symbol="ETHUSDT")
        client.prepare_order(order)
        client.submit_order(order=order)
        filter_evidence = client.get_order_preparation_filter_evidence(
            client_order_id=order.client_order_id
        )
        submission_evidence = client.get_order_submission_attempt_evidence(
            client_order_id=order.client_order_id
        )

        self.assertIsNotNone(filter_evidence)
        self.assertIsNotNone(submission_evidence)
        if filter_evidence is None or submission_evidence is None:
            self.fail("successful prepare and POST must retain provenance")
        expected_server_time = FIXED_TIME + server_offset
        self.assertEqual(expected_server_time, filter_evidence.observed_at)
        self.assertEqual(expected_server_time, submission_evidence.attempted_at)
        self.assertNotEqual(skewed_result_clock(), filter_evidence.observed_at)

        # Actual harness clock을 REST의 primary와 result에 함께 주입해 offset 동기화 뒤 wall 후퇴를 재현한다.
        from tests.testnet.test_phase13_public_market_case2 import (
            _RunScopedEvidenceClock,
        )

        monotonic_value = 0

        def advancing_monotonic_clock() -> int:
            """
            함수 이름: advancing_monotonic_clock()
            기능: 각 evidence 관찰마다 1 millisecond씩 전진하는 monotonic nanosecond를 반환한다.
            인자: 없음
            반환값: 현재 monotonic nanosecond 정수
            작성 날짜: 2026/09/01
            """
            nonlocal monotonic_value
            current_value = monotonic_value
            monotonic_value += 1_000_000
            return current_value  # 단조 증가만 제공하며 wall clock 변경과 독립이다.

        wall_clock = Mock(return_value=FIXED_TIME)
        evidence_clock = _RunScopedEvidenceClock(
            wall_clock=wall_clock,
            monotonic_clock=advancing_monotonic_clock,
        )
        started_at = evidence_clock()
        missing_time_order_payload = _filled_order_payload(
            client_order_id="bat-client-wall-regression"
        )
        del missing_time_order_payload[
            "transactTime"
        ]  # Exchange 시각이 없는 FULL 응답을 성공으로 확정하지 않게 한다.
        regression_transport = QueueHTTPTransport(
            [
                _json_response({"serverTime": FIXED_TIME_MILLISECONDS}),
                _json_response({}),
                *_preparation_responses(include_server_time=False),
                _json_response(missing_time_order_payload),
            ]
        )
        regression_client = BinanceSpotRESTClient(
            API_KEY,
            SECRET_KEY,
            transport=regression_transport,
            clock=evidence_clock,
            result_clock=evidence_clock,
        )
        regression_order = _order(client_order_id="bat-client-wall-regression")

        # Server offset을 먼저 고정한 뒤 wall provider를 하루 후퇴시켜 UNKNOWN 인과 경계를 검증한다.
        regression_client.get_account_commission(symbol="ETHUSDT")
        wall_clock.return_value = FIXED_TIME - timedelta(days=1)
        regression_client.prepare_order(regression_order)
        regression_result = regression_client.submit_order(
            order=regression_order
        )
        regression_filter = (
            regression_client.get_order_preparation_filter_evidence(
                client_order_id=regression_order.client_order_id
            )
        )
        regression_submission = (
            regression_client.get_order_submission_attempt_evidence(
                client_order_id=regression_order.client_order_id
            )
        )
        completed_at = evidence_clock()

        self.assertIsNotNone(regression_filter)
        self.assertIsNotNone(regression_submission)
        if regression_filter is None or regression_submission is None:
            self.fail("wall-regression UNKNOWN must retain exact provenance")
        self.assertEqual(OrderStatus.UNKNOWN, regression_result.status)
        self.assertEqual((), regression_result.fills)
        self.assertEqual(
            "BINANCE_PAYLOAD_RECONCILIATION_REQUIRED",
            regression_result.failure_reason,
        )
        self.assertLessEqual(started_at, regression_filter.observed_at)
        self.assertLessEqual(
            regression_filter.observed_at,
            regression_submission.attempted_at,
        )
        self.assertLessEqual(
            regression_submission.attempted_at,
            regression_result.processed_at,
        )
        self.assertLessEqual(regression_result.processed_at, completed_at)
        wall_clock.assert_called_once_with()

    def test_submit_rejects_quantity_changed_after_prepare(self) -> None:
        """
        함수 이름: test_submit_rejects_quantity_changed_after_prepare()
        기능: prepare와 durable journal 이후 제출 수량 mutation을 HTTP 전송 전에 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        transport = QueueHTTPTransport(
            _preparation_responses()
        )
        client = _client(transport)
        order = client.prepare_order(_order())

        # journal에 1.234가 기록된 뒤 더 작은 값으로 바뀌어도 새 수량을 전송하지 않는다.
        order.submitted_quantity = Decimal("1.233")
        with self.assertRaises(OrderPreparationRequiredError):
            client.submit_order(order=order)

        self.assertEqual(len(transport.requests), 6)
        self.assertIn("/v3/referencePrice", transport.requests[-1]["url"])

    def test_submit_rejects_side_changed_after_prepare(self) -> None:
        """
        함수 이름: test_submit_rejects_side_changed_after_prepare()
        기능: durable journal 이후 주문 방향 metadata mutation을 HTTP 전에 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        transport = QueueHTTPTransport(
            _preparation_responses()
        )
        client = _client(transport)
        order = client.prepare_order(_order())

        # BUY journal을 SELL wire 요청으로 바꾸려는 mutation은 exact fingerprint와 다르다.
        order.side = OrderSide.SELL
        with self.assertRaises(OrderPreparationRequiredError):
            client.submit_order(order=order)

        self.assertEqual(len(transport.requests), 6)
        self.assertIn("/v3/referencePrice", transport.requests[-1]["url"])

    def test_submit_rejects_policy_version_changed_after_prepare(self) -> None:
        """
        함수 이름: test_submit_rejects_policy_version_changed_after_prepare()
        기능: prepare·journal 사이 risk policy provenance mutation을 HTTP 전송 전에 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        transport = QueueHTTPTransport(
            _preparation_responses()
        )
        client = _client(transport)
        order = client.prepare_order(_order())

        # Policy version도 수량·side와 같은 durable preparation fingerprint의 일부다.
        order.risk_policy_version = 1
        with self.assertRaises(OrderPreparationRequiredError):
            client.submit_order(order=order)

        self.assertEqual(len(transport.requests), 6)
        self.assertIn("/v3/referencePrice", transport.requests[-1]["url"])

    def test_account_commission_uses_signed_symbol_endpoint(self) -> None:
        """
        함수 이름: test_account_commission_uses_signed_symbol_endpoint()
        기능: commission preflight가 공식 signed path와 canonical symbol만 전송하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        commission_payload = {
            "symbol": "ETHUSDT",
            "discount": {
                "enabledForAccount": False,
                "enabledForSymbol": True,
                "discountAsset": "BNB",
                "discount": "0.75000000",
            },
        }
        transport = QueueHTTPTransport(
            [
                _json_response({"serverTime": FIXED_TIME_MILLISECONDS}),
                _json_response(commission_payload),
            ]
        )
        client = _client(transport)

        # Lowercase 입력은 canonical symbol로 정규화되고 raw response는 client 경계까지만 반환된다.
        result = client.get_account_commission(symbol="ethusdt")

        self.assertEqual(result, commission_payload)
        self.assertEqual(len(transport.requests), 2)
        commission_request = transport.requests[1]
        parsed_url = urlsplit(commission_request["url"])
        request_parameters = parse_qs(parsed_url.query)
        self.assertEqual(parsed_url.path, "/api/v3/account/commission")
        self.assertEqual(request_parameters["symbol"], ["ETHUSDT"])
        self.assertIn("timestamp", request_parameters)
        self.assertIn("recvWindow", request_parameters)
        self.assertIn("signature", request_parameters)
        self.assertEqual(
            commission_request["headers"]["X-MBX-APIKEY"],
            API_KEY,
        )
        self.assertNotIn(SECRET_KEY, commission_request["url"])
        self.assertIsNone(commission_request["body"])

    def test_account_filters_uses_signed_symbol_endpoint(self) -> None:
        """
        함수 이름: test_account_filters_uses_signed_symbol_endpoint()
        기능: myFilters가 공식 signed path와 canonical symbol을 사용해 raw schema를 반환하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        account_filters_payload = _account_filters_payload(
            {
                "filterType": "MAX_ASSET",
                "asset": "USDT",
                "limit": "250.00000000",
            }
        )
        transport = QueueHTTPTransport(
            [
                _json_response({"serverTime": FIXED_TIME_MILLISECONDS}),
                _json_response(account_filters_payload),
            ]
        )
        client = _client(transport)

        # USER_DATA 요청은 /time 뒤 API key와 HMAC parameter를 포함하되 secret 원문은 제외한다.
        result = client.get_account_filters(symbol="ethusdt")

        self.assertEqual(result, account_filters_payload)
        account_filters_request = transport.requests[1]
        parsed_url = urlsplit(account_filters_request["url"])
        request_parameters = parse_qs(parsed_url.query)
        self.assertEqual(parsed_url.path, "/api/v3/myFilters")
        self.assertEqual(request_parameters["symbol"], ["ETHUSDT"])
        self.assertIn("timestamp", request_parameters)
        self.assertIn("recvWindow", request_parameters)
        self.assertIn("signature", request_parameters)
        self.assertEqual(
            account_filters_request["headers"]["X-MBX-APIKEY"],
            API_KEY,
        )
        self.assertNotIn(SECRET_KEY, account_filters_request["url"])
        self.assertIsNone(account_filters_request["body"])

    def test_all_symbol_open_state_reads_are_signed_and_identity_free(
        self,
    ) -> None:
        """
        함수 이름: test_all_symbol_open_state_reads_are_signed_and_identity_free()
        기능: openOrders와 openOrderList 전체 계정 조회가 symbol 없이 bool만 반환하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        transport = QueueHTTPTransport(
            [
                _json_response({"serverTime": FIXED_TIME_MILLISECONDS}),
                _json_response([]),
                _json_response([{"orderListId": 71}]),
            ]
        )
        client = _client(transport)

        # 두 USER_DATA read는 raw order/list identity 대신 account 전체 non-empty bool만 남긴다.
        has_open_orders = client.has_any_exchange_open_orders()
        has_open_order_lists = (
            client.has_any_exchange_open_order_lists()
        )  # 같은 synchronized offset을 재사용하되 별도 signed snapshot을 만든다.

        self.assertFalse(has_open_orders)
        self.assertTrue(has_open_order_lists)
        self.assertEqual(
            tuple(urlsplit(value["url"]).path for value in transport.requests),
            (
                "/api/v3/time",
                "/api/v3/openOrders",
                "/api/v3/openOrderList",
            ),
        )
        for request_value in transport.requests[1:]:
            with self.subTest(path=urlsplit(request_value["url"]).path):
                request_parameters = parse_qs(
                    urlsplit(request_value["url"]).query
                )
                self.assertNotIn("symbol", request_parameters)
                self.assertIn("timestamp", request_parameters)
                self.assertIn("recvWindow", request_parameters)
                self.assertIn("signature", request_parameters)
                self.assertEqual(
                    API_KEY,
                    request_value["headers"]["X-MBX-APIKEY"],
                )
                self.assertIsNone(request_value["body"])
        self.assertFalse(
            any(
                request_value["method"] == "POST"
                for request_value in transport.requests
            )
        )

    def test_reference_price_fetch_is_fresh_unsigned_and_strict(self) -> None:
        """
        함수 이름: test_reference_price_fetch_is_fresh_unsigned_and_strict()
        기능: 연속 referencePrice 조회가 cache 없이 public exact DTO를 반환하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        transport = QueueHTTPTransport(
            [
                _json_response(_reference_price_payload(price="100")),
                _json_response(_reference_price_payload(price="101")),
            ]
        )
        client = _client(transport)

        # 두 public GET은 각각 새 Decimal을 반환하고 credential header를 전혀 보내지 않는다.
        first_reference_price = client.fetch_reference_price(
            symbol="ethusdt"
        )
        second_reference_price = client.fetch_reference_price(
            symbol="ETHUSDT"
        )

        self.assertEqual(first_reference_price.price, Decimal("100"))
        self.assertEqual(second_reference_price.price, Decimal("101"))
        self.assertEqual(first_reference_price.symbol, "ETHUSDT")
        for request_value in transport.requests:
            parsed_url = urlsplit(request_value["url"])
            self.assertEqual(parsed_url.path, "/api/v3/referencePrice")
            self.assertEqual(
                parse_qs(parsed_url.query),
                {"symbol": ["ETHUSDT"]},
            )
            self.assertNotIn("X-MBX-APIKEY", request_value["headers"])
            self.assertIsNone(request_value["body"])

    def test_reference_price_null_and_minus_2043_fail_closed(self) -> None:
        """
        함수 이름: test_reference_price_null_and_minus_2043_fail_closed()
        기능: Null과 공식 NO_REFERENCE_PRICE 오류가 VWAP 추정 없이 실패하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        null_transport = QueueHTTPTransport(
            [_json_response(_reference_price_payload(price=None))]
        )
        missing_transport = QueueHTTPTransport(
            [
                _json_response(
                    {
                        "code": -2043,
                        "msg": "This symbol doesn't have a reference price.",
                    },
                    status_code=400,
                )
            ]
        )

        # 두 공식 부재 표현 모두 값 생성이나 average-price request 없이 한 GET에서 종료한다.
        with self.assertRaisesRegex(BinancePayloadError, "non-null"):
            _client(null_transport).fetch_reference_price(
                symbol="ETHUSDT"
            )
        with self.assertRaises(BinanceAPIError) as captured_error:
            _client(missing_transport).fetch_reference_price(
                symbol="ETHUSDT"
            )

        self.assertEqual(captured_error.exception.api_code, -2043)
        self.assertEqual(len(null_transport.requests), 1)
        self.assertEqual(len(missing_transport.requests), 1)

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

    def test_phase13_order_timestamp_rejection_does_not_repeat_http_post(
        self,
    ) -> None:
        """
        함수 이름: test_phase13_order_timestamp_rejection_does_not_repeat_http_post()
        기능: Phase 13 주문의 -1021이 server 재동기화나 두 번째 POST permit으로 이어지지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        transport = QueueHTTPTransport(
            [
                *_preparation_responses(),
                _json_response(
                    {"code": -1021, "msg": "outside recvWindow"},
                    status_code=400,
                ),
            ]
        )
        client = _client(
            transport,
            allow_order_timestamp_retry=False,
        )
        prepared_order = client.prepare_order(_order())

        # Prepare와 최초 time sync 뒤 POST 하나만 보내고 확정 미도달 rejection으로 반환한다.
        result = client.submit_order(order=prepared_order)
        order_requests = tuple(
            request_value
            for request_value in transport.requests
            if request_value["method"] == "POST"
            and urlsplit(request_value["url"]).path == "/api/v3/order"
        )
        request_paths = tuple(
            urlsplit(request_value["url"]).path
            for request_value in transport.requests
        )

        self.assertIs(result.status, OrderStatus.REJECTED)
        self.assertIs(
            result.failure_kind,
            OrderResultFailureKind.SUBMISSION_REJECTED,
        )
        self.assertEqual(result.failure_reason, "BINANCE_SUBMISSION_REJECTED_-1021")
        self.assertEqual(len(order_requests), 1)
        self.assertEqual(
            request_paths,
            (
                "/api/v3/time",
                "/api/v3/myFilters",
                "/api/v3/exchangeInfo",
                "/api/v3/openOrders",
                "/api/v3/openOrderList",
                "/api/v3/referencePrice",
                "/api/v3/order",
            ),
        )

    def test_legacy_order_timestamp_rejection_retains_single_retry(
        self,
    ) -> None:
        """
        함수 이름: test_legacy_order_timestamp_rejection_retains_single_retry()
        기능: Phase 13 전용 제한을 주입하지 않은 기존 Testnet 주문은 -1021 재동기화 1회를 유지하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        transport = QueueHTTPTransport(
            [
                *_preparation_responses(),
                _json_response(
                    {"code": -1021, "msg": "outside recvWindow"},
                    status_code=400,
                ),
                _json_response({"serverTime": FIXED_TIME_MILLISECONDS + 5}),
                _json_response(_filled_order_payload()),
            ]
        )
        client = _client(transport)
        prepared_order = client.prepare_order(_order())

        # 기본 True 정책은 첫 미도달 rejection 뒤 time sync와 같은 client ID POST 한 번을 보존한다.
        result = client.submit_order(order=prepared_order)
        order_requests = tuple(
            request_value
            for request_value in transport.requests
            if request_value["method"] == "POST"
            and urlsplit(request_value["url"]).path == "/api/v3/order"
        )

        self.assertIs(result.status, OrderStatus.FILLED)
        self.assertEqual(len(order_requests), 2)
        self.assertEqual(
            tuple(
                urlsplit(request_value["url"]).path
                for request_value in transport.requests
            ),
            (
                "/api/v3/time",
                "/api/v3/myFilters",
                "/api/v3/exchangeInfo",
                "/api/v3/openOrders",
                "/api/v3/openOrderList",
                "/api/v3/referencePrice",
                "/api/v3/order",
                "/api/v3/time",
                "/api/v3/order",
            ),
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
                        *_preparation_responses(),
                        failure_response,
                    ]
                )
                client = _client(transport)
                order = client.prepare_order(_order())
                result = client.submit_order(order=order)

                self.assertEqual(result.status, OrderStatus.UNKNOWN)
                self.assertEqual(result.client_order_id, "bat-client-0")
                self.assertIsNone(result.exchange_order_id)

    def test_rate_limit_retry_after_is_never_shortened(self) -> None:
        """
        함수 이름: test_rate_limit_retry_after_is_never_shortened()
        기능: REST 429 Retry-After seconds가 임의 local 상한으로 축소되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        transport = QueueHTTPTransport(
            [
                *_preparation_responses(),
                _json_response(
                    {"code": -1003, "msg": "rate limited"},
                    status_code=429,
                    headers={"Retry-After": "90"},
                ),
            ]
        )

        # Rate-limit 제출은 실행 여부를 UNKNOWN으로 두고 공식 header 전체를 그대로 보존한다.
        client = _client(transport)
        order = client.prepare_order(_order())
        result = client.submit_order(order=order)

        self.assertEqual(result.status, OrderStatus.UNKNOWN)
        self.assertEqual(result.retry_after, timedelta(seconds=90))
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
                *_preparation_responses(),
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
                *_preparation_responses(),
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

    def test_absolute_maximum_notional_is_enforced_at_construction(self) -> None:
        """
        함수 이름: test_absolute_maximum_notional_is_enforced_at_construction()
        기능: 직접 adapter 조립도 100 USDT를 넘는 configured cap을 허용하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        transport = QueueHTTPTransport([])

        # Config bootstrap을 우회한 직접 생성에서도 절대 ceiling은 network 전에 같은 값으로 닫힌다.
        with self.assertRaisesRegex(ValueError, "between 0 and 100"):
            _client(
                transport,
                maximum_order_notional=Decimal("100.0000000001"),
            )

        self.assertEqual(transport.requests, [])  # 잘못된 상한은 exchangeInfo 조회조차 만들지 않는다.

    def test_configured_maximum_notional_allows_exact_ceiling(self) -> None:
        """
        함수 이름: test_configured_maximum_notional_allows_exact_ceiling()
        기능: filter 후 decision notional이 정확히 100 USDT면 preparation을 허용하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        transport = QueueHTTPTransport(
            _preparation_responses(
                exchange_info_payload=_exchange_info_payload(
                    maximum_notional="100000"
                )
            )
        )
        client = _client(
            transport,
            maximum_order_notional=Decimal("100"),
        )
        exact_ceiling_order = _order(quantity="1.000")

        # Decimal decision price 100과 filter 후 수량 1.000의 곱은 경계값 자체이므로 포함한다.
        prepared_order = client.prepare_order(exact_ceiling_order)

        self.assertIs(prepared_order, exact_ceiling_order)
        self.assertEqual(prepared_order.submitted_quantity, Decimal("1.000"))
        self.assertEqual(len(transport.requests), 6)  # 다섯 fresh read와 최초 time sync를 포함한다.

    def test_configured_maximum_notional_blocks_order_before_signing(self) -> None:
        """
        함수 이름: test_configured_maximum_notional_blocks_order_before_signing()
        기능: bootstrap opt-in 금액 cap이 exchange filter를 통과한 실제 수량에 추가 적용되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        transport = QueueHTTPTransport(
            _preparation_responses(
                exchange_info_payload=_exchange_info_payload(
                    maximum_notional="100000"
                )
            )
        )
        client = _client(
            transport,
            maximum_order_notional=Decimal("100"),
        )

        # 1.234 ETH×100 USDT는 fresh read 이후 journal·POST 전에 local cap으로 거부한다.
        with self.assertRaisesRegex(
            SymbolFilterError,
            "FILTER_CONFIGURED_MAXIMUM_NOTIONAL",
        ):
            client.prepare_order(_order())

        self.assertEqual(len(transport.requests), 6)
        self.assertIn("/v3/referencePrice", transport.requests[-1]["url"])

    def test_configured_entry_cap_allows_stop_sell_above_quote_cap(
        self,
    ) -> None:
        """
        함수 이름: test_configured_entry_cap_allows_stop_sell_above_quote_cap()
        기능: Position을 줄이는 STOP SELL은 BUY 진입 cap보다 평가액이 커도 prepare되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        transport = QueueHTTPTransport(
            _preparation_responses(
                exchange_info_payload=_exchange_info_payload(
                    maximum_notional="100000"
                )
            )
        )
        client = _client(
            transport,
            maximum_order_notional=Decimal("100"),
        )
        stop_sell = Order(
            intent_id="force-sell:entry-cap-regression",
            client_order_id="bat-entry-cap-stop-sell",
            submission_attempt=0,
            symbol="ETHUSDT",
            side=OrderSide.SELL,
            strategy=StrategyType.CASE_B,
            regime_type=RegimeType.TYPE_0,
            requested_quantity=Decimal("1.234"),
            submitted_quantity=Decimal("1.234"),
            market_price_at_decision=Decimal("100"),
            exit_reason=ExitReason.STOP,
        )

        # Exchange filter는 유지하되 local quote cap은 exposure-reducing SELL을 거부하지 않는다.
        prepared_sell = client.prepare_order(stop_sell)

        self.assertIs(prepared_sell.side, OrderSide.SELL)
        self.assertEqual(
            prepared_sell.submitted_quantity,
            Decimal("1.234"),
        )
        self.assertGreater(
            prepared_sell.submitted_quantity
            * prepared_sell.market_price_at_decision,
            Decimal("100"),
        )
        self.assertEqual(len(transport.requests), 6)

    def test_configured_entry_cap_still_blocks_non_stop_sell(
        self,
    ) -> None:
        """
        함수 이름: test_configured_entry_cap_still_blocks_non_stop_sell()
        기능: STOP cleanup이 아닌 일반 SELL은 기존 local quote cap을 우회하지 못하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        transport = QueueHTTPTransport(
            _preparation_responses(
                exchange_info_payload=_exchange_info_payload(
                    maximum_notional="100000"
                )
            )
        )
        client = _client(
            transport,
            maximum_order_notional=Decimal("100"),
        )
        take_profit_sell = Order(
            intent_id="case-b:entry-cap-regression",
            client_order_id="bat-entry-cap-take-profit",
            submission_attempt=0,
            symbol="ETHUSDT",
            side=OrderSide.SELL,
            strategy=StrategyType.CASE_B,
            regime_type=RegimeType.TYPE_0,
            requested_quantity=Decimal("1.234"),
            submitted_quantity=Decimal("1.234"),
            market_price_at_decision=Decimal("100"),
            exit_reason=ExitReason.TAKE_PROFIT,
        )

        # STOP provenance가 없는 SELL은 direct adapter 호출에서도 cap을 우회하지 못한다.
        with self.assertRaisesRegex(
            SymbolFilterError,
            "FILTER_CONFIGURED_MAXIMUM_NOTIONAL",
        ):
            client.prepare_order(take_profit_sell)

        self.assertEqual(len(transport.requests), 6)

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

    def test_query_rejects_fill_from_different_order_without_partial_result(
        self,
    ) -> None:
        """
        함수 이름: test_query_rejects_fill_from_different_order_without_partial_result()
        기능: myTrades가 다른 주문의 fill을 반환하면 부분 결과 대신 UNKNOWN으로 닫는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
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
        mismatched_trade_payload = [
            {
                "symbol": "ETHUSDT",
                "id": 7001,
                "orderId": 43,
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
                _json_response(mismatched_trade_payload),
                _json_response(_exchange_info_payload()),
            ]
        )
        order = _order(quantity="1.000")
        order.exchange_order_id = "42"

        # 다른 order ID의 fill은 공개 query 경계에서 체결 결과로 승격하지 않는다.
        result = _client(transport).query_order_result(order=order)

        self.assertEqual(result.status, OrderStatus.UNKNOWN)
        self.assertEqual(
            result.failure_reason,
            "BINANCE_QUERY_RECONCILIATION_REQUIRED",
        )
        self.assertEqual(result.fills, ())  # 검증 실패한 fill은 일부라도 노출하지 않는다.
        my_trades_query = parse_qs(urlsplit(transport.requests[2]["url"]).query)
        self.assertEqual(my_trades_query["orderId"], ["42"])

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

        # Safety preflight 전용 surface는 같은 signed endpoint에서 manual client ID도 숨기지 않는다.
        full_transport = QueueHTTPTransport(
            [
                _json_response({"serverTime": FIXED_TIME_MILLISECONDS}),
                _json_response(open_orders),
            ]
        )
        all_results = _client(full_transport).list_all_open_order_results()
        self.assertEqual(
            ("bat-open-0", "manual-order"),
            tuple(result.client_order_id for result in all_results),
        )

        # allOrders 전용 surface도 prefix 없이 manual ID를 유지하고 bounded limit를 그대로 서명한다.
        recent_transport = QueueHTTPTransport(
            [
                _json_response({"serverTime": FIXED_TIME_MILLISECONDS}),
                _json_response(open_orders),
            ]
        )
        all_recent_results = _client(
            recent_transport
        ).list_all_recent_order_results(limit=17)
        self.assertEqual(
            ("bat-open-0", "manual-order"),
            tuple(result.client_order_id for result in all_recent_results),
        )
        recent_query = parse_qs(
            urlsplit(recent_transport.requests[1]["url"]).query
        )
        self.assertEqual(recent_query["symbol"], ["ETHUSDT"])
        self.assertEqual(recent_query["limit"], ["17"])


if __name__ == "__main__":
    unittest.main()
