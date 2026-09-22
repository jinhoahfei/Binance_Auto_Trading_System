"""Testnet protocol 구현을 재사용하되 fixed live endpoint와 전송 권한을 별도로 결속한다."""

from collections.abc import Callable, Mapping
from decimal import Decimal
from datetime import datetime
from .earn_residual import EARN_READ_ENDPOINTS, read_earn_residual_evidence

from binance_auto_trader.adapters.binance.live_endpoints import (
    LIVE_REST_BASE_URL, _LIVE_ENDPOINT_CAPABILITY,
)
from binance_auto_trader.adapters.binance.spot_rest_client import (
    BinanceSpotRESTClient, HTTPTransportResponse, UrllibHTTPTransport,
)
from binance_auto_trader.adapters.binance.spot_websocket_client import BinanceSpotWebSocketClient

_LIVE_REST_ORDER_CAPABILITY = object()  # 별도 live root만 mutation transport를 조립한다.


class _LiveHTTPTransport:
    """
    클래스 이름: _LiveHTTPTransport
    기능: 서명된 요청도 fixed live URL과 method allowlist 밖이면 전송 전에 차단한다.
    작성 날짜: 2026/09/08
    """

    def __init__(self, delegate: object, *, order_capability: object | None) -> None:
        """
        함수 이름: __init__()
        기능: 주입 HTTP seam과 live 전용 mutation 표식을 검증한다.
        인자: delegate -> request HTTP transport, order_capability -> 별도 live 표식 또는 None
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        # 잘못된 표식을 read-only로 완화하지 않고 client 생성 자체를 거부한다.
        if order_capability is not None and order_capability is not _LIVE_REST_ORDER_CAPABILITY:
            raise ValueError("invalid live REST capability")
        self._delegate = delegate
        self._allow_orders = order_capability is _LIVE_REST_ORDER_CAPABILITY

    def request(
        self, *, method: str, url: str, headers: Mapping[str, str],
        body: bytes | None, timeout_seconds: int,
        before_send: Callable[[], None] | None = None,
    ) -> HTTPTransportResponse:
        """
        함수 이름: request()
        기능: URL·method gate를 통과한 요청만 redirect-disabled transport에 전달한다.
        인자: method/url/headers/body -> protocol이 만든 요청, timeout_seconds -> 제한,
            before_send -> protocol의 최종 submit guard
        반환값: HTTP 응답
        작성 날짜: 2026/09/08
        """
        # Spot와 잔여 예치 조회의 exact path만 허용하며 예치·상환 API는 제공하지 않는다.
        request_path = url.split("?", 1)[0]
        read_paths = {
            "/v3/time", "/v3/klines", "/v3/account", "/v3/account/commission",
            "/v3/myFilters", "/v3/exchangeInfo", "/v3/referencePrice",
            "/v3/order", "/v3/openOrders", "/v3/openOrderList", "/v3/allOrders", "/v3/myTrades",
        }
        allowed_read = method == "GET" and request_path in {
            LIVE_REST_BASE_URL + path for path in read_paths
        }
        allowed_read = allowed_read or (method == "GET" and request_path in {
            LIVE_REST_BASE_URL.removesuffix("/api") + path for path in EARN_READ_ENDPOINTS
        })
        allowed_mutation = self._allow_orders and method in {"POST", "DELETE"} and request_path == LIVE_REST_BASE_URL + "/v3/order"
        if not (allowed_read or allowed_mutation):
            raise ValueError("live HTTP request denied")
        return self._delegate.request(
            method=method, url=url, headers=headers, body=body,
            timeout_seconds=timeout_seconds, before_send=before_send,
        )  # Redirect와 timestamp retry는 기존 transport/protocol의 fail-closed 계약을 유지한다.


class BinanceLiveRESTClient(BinanceSpotRESTClient):
    """
    클래스 이름: BinanceLiveRESTClient
    기능: URL override 없는 live REST adapter와 read-only 기본 전송 방벽을 제공한다.
    작성 날짜: 2026/09/08
    """

    def __init__(
        self, api_key: str, secret_key: str, *, transport: object | None = None,
        order_capability: object | None = None, maximum_order_notional: Decimal | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: fixed live endpoint·주문 cap·전송 표식을 원자적으로 결속한다.
        인자: api_key/secret_key -> memory credential, transport -> test seam,
            order_capability -> live 표식, maximum_order_notional -> enabled일 때 exact 10
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        # Cap이 없는 mutation 또는 read-only cap 혼합은 credential 전달 전에 거부한다.
        if order_capability is _LIVE_REST_ORDER_CAPABILITY:
            if type(maximum_order_notional) is not Decimal or maximum_order_notional != Decimal("10"):
                raise ValueError("live REST orders require exact cap 10")
        elif maximum_order_notional is not None:
            raise ValueError("read-only live REST cannot carry order cap")
        guarded_transport = _LiveHTTPTransport(
            transport if transport is not None else UrllibHTTPTransport(),
            order_capability=order_capability,
        )
        super().__init__(
            api_key, secret_key, base_url=LIVE_REST_BASE_URL, transport=guarded_transport,
            maximum_order_notional=maximum_order_notional, allow_order_timestamp_retry=False,
            _live_endpoint_capability=_LIVE_ENDPOINT_CAPABILITY,
        )  # Live는 -1021에서도 POST를 자동 재전송하지 않는다.


    def fetch_earn_residual_evidence(self, *, since: datetime):
        """
        함수 이름: fetch_earn_residual_evidence()
        기능: 고정 Simple Earn GET만 기존 서명·시간 동기화·redirect 차단 경계에서 실행한다.
        인자: since -> 잔여 발생 UTC 시각
        반환값: 검증된 자동 예치 근거 또는 None
        작성 날짜: 2026/09/15
        """
        def request(endpoint, parameters):
            """
            함수 이름: request()
            기능: 검증된 Earn 조회 경로를 signed GET으로만 호출한다.
            인자: endpoint -> 고정 Earn 경로, parameters -> 조회 조건
            반환값: 해석된 JSON
            작성 날짜: 2026/09/15
            """
            return self._request_json(method="GET", endpoint=endpoint, parameters=parameters, signed=True).payload
        return read_earn_residual_evidence(request, since, self.get_server_timestamp_milliseconds())


class BinanceLiveWebSocketClient(BinanceSpotWebSocketClient):
    """
    클래스 이름: BinanceLiveWebSocketClient
    기능: 공식 live market/user-data stream만 열며 주문 WebSocket operation은 제공하지 않는다.
    작성 날짜: 2026/09/08
    """

    def __init__(
        self, api_key: str, api_secret: str, *, socket_factory: object | None = None,
        timestamp_provider: Callable[[], int] | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: Testnet과 분리한 live endpoint pair를 기존 bounded stream protocol에 주입한다.
        인자: api_key/api_secret -> memory credential, socket_factory -> memory WS seam,
            timestamp_provider -> signed REST server time provider
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        # Public/private stream 선택을 함께 고정하고 환경 URL과 fallback 입력을 받지 않는다.
        super().__init__(
            api_key, api_secret, socket_factory=socket_factory,
            timestamp_provider=timestamp_provider, _live_endpoint_capability=_LIVE_ENDPOINT_CAPABILITY,
        )
