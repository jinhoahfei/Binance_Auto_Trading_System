"""Public Kline부터 production Spot REST 주문 경계까지 Case 2를 검증한다."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import parse_qs, urlsplit

from binance_auto_trader.adapters.binance.api_gateway import APIGateway
from binance_auto_trader.adapters.binance.spot_rest_client import (
    BinanceSpotRESTClient,
    HTTPTransportResponse,
)
from binance_auto_trader.adapters.binance.websocket_gateway import (
    WebSocketGateway,
)
from binance_auto_trader.adapters.persistence import TradeHistoryRepository
from binance_auto_trader.application.market_data_controller import (
    MarketDataController,
)
from binance_auto_trader.application.market_evaluation_builder import (
    ThirtyMinuteMarketEvaluationBuilder,
)
from binance_auto_trader.application.regime_controller import RegimeController
from binance_auto_trader.application.trade_history_controller import (
    TradeHistoryController,
)
from binance_auto_trader.application.trading_controller import (
    TradingController,
    TradingSessionStatus,
)
from binance_auto_trader.domain.common import Interval, RegimeType
from binance_auto_trader.domain.market import Kline, MarketSnapshot
from binance_auto_trader.domain.regime import RegimeSTM
from binance_auto_trader.domain.trading import (
    Account,
    OrderSide,
    Position,
    StrategyType,
    TradingContext,
    TradingEventType,
)

from tests.integration.phase13_risk_fixture import create_test_risk_policy
from tests.integration.test_account_stream_flow import (
    SynchronousAccountWebSocketClient,
)
from tests.integration.test_buy_sell_flow import MutableUtcClock
from tests.integration.test_market_initialization_flow import (
    FakeWebSocketClient,
    NoOpRegimeController,
)


# 모든 local HTTP 응답과 public Kline은 같은 UTC 진행봉을 공유한다.
SYMBOL = "ETHUSDT"
API_KEY = "local-testnet-api-key"
SECRET_KEY = "local-testnet-secret"
CURRENT_THIRTY_MINUTE_OPEN = datetime(
    2026,
    8,
    29,
    12,
    0,
    tzinfo=timezone.utc,
)
INITIAL_TIME = CURRENT_THIRTY_MINUTE_OPEN + timedelta(
    minutes=10,
    seconds=5,
)
INITIAL_TIME_MILLISECONDS = int(INITIAL_TIME.timestamp() * 1_000)
EXCHANGE_ORDER_ID = 42_001
INTERVAL_DURATION_MILLISECONDS = {
    Interval.ONE_MINUTE: 60_000,
    Interval.THIRTY_MINUTES: 1_800_000,
    Interval.FOUR_HOURS: 14_400_000,
    Interval.ONE_DAY: 86_400_000,
}
BASE_CLOSED_PRICES = tuple(
    Decimal(100 + index)
    for index in range(20)
)


def _json_response(payload: object) -> HTTPTransportResponse:
    """
    함수 이름: _json_response()
    기능: Local JSON fixture를 production transport와 같은 bytes 응답으로 만든다.
    인자: payload -> JSON으로 직렬화할 응답 값
    반환값: HTTPTransportResponse
    작성 날짜: 2026/08/29
    """
    return HTTPTransportResponse(
        status_code=200,
        headers={},
        body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
    )  # REST client의 실제 JSON decode 경계를 그대로 통과한다.


def _create_rest_kline_row(
    interval: Interval,
    open_time: datetime,
    close_price: Decimal,
) -> list[object]:
    """
    함수 이름: _create_rest_kline_row()
    기능: APIGateway가 정규화할 12-field Spot REST Kline 행을 만든다.
    인자: interval -> Kline 주기
        open_time -> UTC 봉 시작 시각
        close_price -> OHLC에 사용할 종가
    반환값: Binance REST schema와 같은 list
    작성 날짜: 2026/08/29
    """
    open_time_milliseconds = int(open_time.timestamp() * 1_000)
    close_time_milliseconds = (
        open_time_milliseconds
        + INTERVAL_DURATION_MILLISECONDS[interval]
        - 1
    )

    # High·low를 종가에서 한 단위씩 벌려 지표 입력을 유효하게 두어야 한다.
    return [
        open_time_milliseconds,
        format(close_price, "f"),
        format(close_price + Decimal("1"), "f"),
        format(close_price - Decimal("1"), "f"),
        format(close_price, "f"),
        "10",
        close_time_milliseconds,
        "1000",
        10,
        "5",
        "500",
        "0",
    ]  # Raw 행은 mapper 외부로 공개되지 않는다.


def _create_market_rest_responses() -> dict[str, list[list[object]]]:
    """
    함수 이름: _create_market_rest_responses()
    기능: 20개 30분 확정봉과 네 주기 진행봉을 local REST 기준선으로 만든다.
    인자: 없음
    반환값: interval 문자열별 Kline 행 mapping
    작성 날짜: 2026/08/29
    """
    # 마지막 확정 30분봉이 현재 진행봉 직전에 끝나도록 역순 시각을 정렬한다.
    closed_thirty_minute_rows = [
        _create_rest_kline_row(
            Interval.THIRTY_MINUTES,
            CURRENT_THIRTY_MINUTE_OPEN
            - timedelta(
                minutes=30 * (len(BASE_CLOSED_PRICES) - index),
            ),
            close_price,
        )
        for index, close_price in enumerate(BASE_CLOSED_PRICES)
    ]
    current_rows = {
        Interval.ONE_MINUTE: _create_rest_kline_row(
            Interval.ONE_MINUTE,
            INITIAL_TIME.replace(second=0, microsecond=0),
            Decimal("100"),
        ),
        Interval.THIRTY_MINUTES: _create_rest_kline_row(
            Interval.THIRTY_MINUTES,
            CURRENT_THIRTY_MINUTE_OPEN,
            Decimal("100"),
        ),
        Interval.FOUR_HOURS: _create_rest_kline_row(
            Interval.FOUR_HOURS,
            CURRENT_THIRTY_MINUTE_OPEN,
            Decimal("100"),
        ),
        Interval.ONE_DAY: _create_rest_kline_row(
            Interval.ONE_DAY,
            CURRENT_THIRTY_MINUTE_OPEN.replace(hour=0),
            Decimal("100"),
        ),
    }

    # 30분 요청만 지표 seed용 확정 history를 진행봉 앞에 붙인다.
    return {
        Interval.ONE_MINUTE.value: [current_rows[Interval.ONE_MINUTE]],
        Interval.THIRTY_MINUTES.value: [
            *closed_thirty_minute_rows,
            current_rows[Interval.THIRTY_MINUTES],
        ],
        Interval.FOUR_HOURS.value: [current_rows[Interval.FOUR_HOURS]],
        Interval.ONE_DAY.value: [current_rows[Interval.ONE_DAY]],
    }


def _account_payload() -> dict[str, object]:
    """
    함수 이름: _account_payload()
    기능: Case C BUY에 사용할 ETH·USDT 전체 계좌 응답을 만든다.
    인자: 없음
    반환값: Binance account REST payload
    작성 날짜: 2026/08/29
    """
    return {
        "makerCommission": 15,
        "takerCommission": 15,
        "buyerCommission": 0,
        "sellerCommission": 0,
        "canTrade": True,
        "canWithdraw": True,
        "canDeposit": True,
        "updateTime": INITIAL_TIME_MILLISECONDS,
        "accountType": "SPOT",
        "balances": [
            {"asset": "ETH", "free": "0", "locked": "0"},
            {"asset": "USDT", "free": "100", "locked": "0"},
        ],
        "permissions": ["SPOT"],
    }  # BUY 전 Position과 잔고 소유권을 모두 0에서 시작한다.


def _exchange_info_payload() -> dict[str, object]:
    """
    함수 이름: _exchange_info_payload()
    기능: ETHUSDT MARKET BUY filter와 fill 자산 mapping에 필요한 응답을 만든다.
    인자: 없음
    반환값: 한 symbol의 exchangeInfo payload
    작성 날짜: 2026/08/29
    """
    return {
        "timezone": "UTC",
        "serverTime": INITIAL_TIME_MILLISECONDS,
        "rateLimits": [],
        "exchangeFilters": [],
        "symbols": [
            {
                "symbol": SYMBOL,
                "status": "TRADING",
                "baseAsset": "ETH",
                "baseAssetPrecision": 8,
                "quoteAsset": "USDT",
                "quoteAssetPrecision": 8,
                "orderTypes": ["LIMIT", "MARKET"],
                "isSpotTradingAllowed": True,
                "filters": [
                    {
                        "filterType": "LOT_SIZE",
                        "minQty": "0.001",
                        "maxQty": "1000",
                        "stepSize": "0.001",
                    },
                    {
                        "filterType": "MARKET_LOT_SIZE",
                        "minQty": "0.001",
                        "maxQty": "1000",
                        "stepSize": "0.001",
                    },
                    {
                        "filterType": "NOTIONAL",
                        "minNotional": "10",
                        "applyMinToMarket": True,
                        "maxNotional": "100000",
                        "applyMaxToMarket": True,
                        "avgPriceMins": 5,
                    },
                ],
            }
        ],
    }


class LocalCase2HTTPTransport:
    """
    클래스 이름: LocalCase2HTTPTransport
    기능: Production Spot REST request를 외부 전송 없이 endpoint별 응답으로 재생한다.
    작성 날짜: 2026/08/29
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: Kline fixture, request 기록과 주문 진행 상태를 초기화한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # Transport 외부로는 credential이 아닌 request metadata만 검증용으로 남긴다.
        self.market_responses = _create_market_rest_responses()
        self.requests: list[dict[str, object]] = []
        self.client_order_id: str | None = None
        self.submitted_quantity: Decimal | None = None
        self.order_query_count = 0
        self.my_trades_query_count = 0

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
        기능: URL을 local route로 해석해 Kline·account·order 응답을 반환한다.
        인자: method -> HTTP method
            url -> production client가 생성한 요청 URL
            headers -> request header mapping
            body -> form body 또는 None
            timeout_seconds -> 검증된 request timeout
            before_send -> local route 처리 직전에 실행할 선택 fail-closed guard
        반환값: 외부 I/O 없이 만든 HTTPTransportResponse
        작성 날짜: 2026/08/29
        """
        # 실제 transport와 같은 경계에서 주문 freshness guard를 먼저 통과시킨다.
        if before_send is not None:
            before_send()
        split_url = urlsplit(url)
        path = split_url.path
        query = parse_qs(split_url.query)

        # Mutable header와 body를 복사해 후속 request가 이전 증거를 바꾸지 못하게 한다.
        self.requests.append(
            {
                "method": method,
                "path": path,
                "query": query,
                "headers": dict(headers),
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        if method == "GET" and path.endswith("/v3/klines"):
            return self._handle_klines(query)
        if method == "GET" and path.endswith("/v3/time"):
            return _json_response({"serverTime": INITIAL_TIME_MILLISECONDS})
        if method == "GET" and path.endswith("/v3/account"):
            return _json_response(_account_payload())

        # Submit-time account·symbol·account-wide empty·reference read는 모두 production route를 재현한다.
        if method == "GET" and path.endswith("/v3/myFilters"):
            return _json_response(
                {
                    "exchangeFilters": [],
                    "symbolFilters": [],
                    "assetFilters": [],
                }
            )
        if method == "GET" and path.endswith("/v3/exchangeInfo"):
            return _json_response(_exchange_info_payload())
        if method == "GET" and path.endswith("/v3/openOrders"):
            return _json_response([])
        if method == "GET" and path.endswith("/v3/openOrderList"):
            return _json_response([])
        if method == "GET" and path.endswith("/v3/referencePrice"):
            return _json_response(
                {
                    "symbol": SYMBOL,
                    "referencePrice": "88.6",
                    "timestamp": INITIAL_TIME_MILLISECONDS,
                }
            )
        if method == "POST" and path.endswith("/v3/order"):
            return self._handle_submit(body)
        if method == "GET" and path.endswith("/v3/order"):
            return self._handle_order_query(query)
        if method == "GET" and path.endswith("/v3/myTrades"):
            return self._handle_my_trades(query)

        raise AssertionError(f"unexpected local HTTP route: {method} {path}")

    def _handle_klines(
        self,
        query: Mapping[str, list[str]],
    ) -> HTTPTransportResponse:
        """
        함수 이름: _handle_klines()
        기능: Public market 요청 주기의 local Kline history를 반환한다.
        인자: query -> URL에서 parse한 query mapping
        반환값: Kline JSON HTTP 응답
        작성 날짜: 2026/08/29
        """
        interval = self._single_query_value(query, "interval")
        limit = int(self._single_query_value(query, "limit"))
        if self._single_query_value(query, "symbol") != SYMBOL:
            raise AssertionError("local transport supports only ETHUSDT")
        if interval not in self.market_responses:
            raise AssertionError("unexpected Kline interval")

        # Limit 적용도 실제 REST와 같이 가장 최근 행에서 수행한다.
        return _json_response(self.market_responses[interval][-limit:])

    def _handle_submit(self, body: bytes | None) -> HTTPTransportResponse:
        """
        함수 이름: _handle_submit()
        기능: Signed MARKET POST의 실제 ID·수량을 고정하고 1/4 partial을 반환한다.
        인자: body -> percent-encoded signed form body
        반환값: PARTIALLY_FILLED FULL order 응답
        작성 날짜: 2026/08/29
        """
        if body is None:
            raise AssertionError("signed order submission requires a body")
        if self.client_order_id is not None:
            raise AssertionError("Case 2 must submit exactly one order")
        form = parse_qs(body.decode("ascii"))
        if self._single_query_value(form, "symbol") != SYMBOL:
            raise AssertionError("submitted symbol must be ETHUSDT")
        if self._single_query_value(form, "side") != "BUY":
            raise AssertionError("public Case C must submit BUY")

        # Production client가 filter 후 서명한 수량과 ID를 후속 same-order 조회에 그대로 재사용한다.
        self.client_order_id = self._single_query_value(
            form,
            "newClientOrderId",
        )
        self.submitted_quantity = Decimal(
            self._single_query_value(form, "quantity")
        )
        first_fill = self._fill_quantities()[0]
        payload = self._order_payload(
            status="PARTIALLY_FILLED",
            executed_quantity=first_fill,
        )
        payload["transactTime"] = INITIAL_TIME_MILLISECONDS
        payload["fills"] = [
            self._direct_fill_payload(0, first_fill)
        ]
        return _json_response(payload)

    def _handle_order_query(
        self,
        query: Mapping[str, list[str]],
    ) -> HTTPTransportResponse:
        """
        함수 이름: _handle_order_query()
        기능: 동일 orderId의 누적 1/2 partial과 FILLED를 순서대로 반환한다.
        인자: query -> signed GET order query mapping
        반환값: 누적 order status HTTP 응답
        작성 날짜: 2026/08/29
        """
        self._assert_same_order_query(query)
        self.order_query_count += 1
        fill_quantities = self._fill_quantities()
        if self.order_query_count == 1:
            executed_quantity = fill_quantities[0] + fill_quantities[1]
            return _json_response(
                self._order_payload(
                    status="PARTIALLY_FILLED",
                    executed_quantity=executed_quantity,
                )
            )
        if self.order_query_count == 2:
            return _json_response(
                self._order_payload(
                    status="FILLED",
                    executed_quantity=sum(
                        fill_quantities,
                        start=Decimal("0"),
                    ),
                )
            )

        raise AssertionError("Case 2 permits exactly two order queries")

    def _handle_my_trades(
        self,
        query: Mapping[str, list[str]],
    ) -> HTTPTransportResponse:
        """
        함수 이름: _handle_my_trades()
        기능: 각 order query의 executedQty와 일치하는 누적 myTrades를 반환한다.
        인자: query -> signed GET myTrades query mapping
        반환값: 동일 orderId의 누적 trade HTTP 응답
        작성 날짜: 2026/08/29
        """
        self._assert_same_order_query(query)
        self.my_trades_query_count += 1
        if self.my_trades_query_count not in (1, 2):
            raise AssertionError("Case 2 permits exactly two myTrades queries")

        # 첫 조회는 두 fill, terminal 조회는 기존 fill을 포함한 세 fill을 반환한다.
        fill_count = 2 if self.my_trades_query_count == 1 else 3
        fills = self._fill_quantities()[:fill_count]
        return _json_response(
            [
                self._trade_payload(fill_index, fill_quantity)
                for fill_index, fill_quantity in enumerate(fills)
            ]
        )

    def _order_payload(
        self,
        *,
        status: str,
        executed_quantity: Decimal,
    ) -> dict[str, object]:
        """
        함수 이름: _order_payload()
        기능: Production mapper가 검증할 공식 order 필드로 누적 상태를 만든다.
        인자: status -> Binance order status
            executed_quantity -> 현재까지 누적 체결 수량
        반환값: order response payload
        작성 날짜: 2026/08/29
        """
        client_order_id = self._require_client_order_id()
        submitted_quantity = self._require_submitted_quantity()
        with localcontext() as decimal_context:
            decimal_context.prec = 34
            decimal_context.rounding = ROUND_HALF_EVEN
            cumulative_quote_quantity = (
                executed_quantity * Decimal("88.6")
            )

        return {
            "symbol": SYMBOL,
            "orderId": EXCHANGE_ORDER_ID,
            "clientOrderId": client_order_id,
            "price": "0.00000000",
            "origQty": format(submitted_quantity, "f"),
            "executedQty": format(executed_quantity, "f"),
            "cummulativeQuoteQty": format(cumulative_quote_quantity, "f"),
            "status": status,
            "timeInForce": "GTC",
            "type": "MARKET",
            "side": "BUY",
            "time": INITIAL_TIME_MILLISECONDS,
            "updateTime": INITIAL_TIME_MILLISECONDS,
            "origQuoteOrderQty": "0.00000000",
        }

    def _direct_fill_payload(
        self,
        fill_index: int,
        fill_quantity: Decimal,
    ) -> dict[str, object]:
        """
        함수 이름: _direct_fill_payload()
        기능: Submit FULL 응답에 포함할 direct fill을 만든다.
        인자: fill_index -> 0부터 시작하는 fill 순서
            fill_quantity -> 해당 fill의 체결 수량
        반환값: FULL fills 항목
        작성 날짜: 2026/08/29
        """
        return {
            "price": "88.6",
            "qty": format(fill_quantity, "f"),
            "commission": "0",
            "commissionAsset": "USDT",
            "tradeId": 7_001 + fill_index,
        }  # 후속 myTrades와 같은 trade ID와 시각으로 멱등 병합된다.

    def _trade_payload(
        self,
        fill_index: int,
        fill_quantity: Decimal,
    ) -> dict[str, object]:
        """
        함수 이름: _trade_payload()
        기능: GET myTrades의 동일 orderId 체결 항목을 만든다.
        인자: fill_index -> 0부터 시작하는 fill 순서
            fill_quantity -> 해당 fill의 체결 수량
        반환값: myTrades 항목
        작성 날짜: 2026/08/29
        """
        with localcontext() as decimal_context:
            decimal_context.prec = 34
            quote_quantity = fill_quantity * Decimal("88.6")

        return {
            "symbol": SYMBOL,
            "id": 7_001 + fill_index,
            "orderId": EXCHANGE_ORDER_ID,
            "price": "88.6",
            "qty": format(fill_quantity, "f"),
            "quoteQty": format(quote_quantity, "f"),
            "commission": "0",
            "commissionAsset": "USDT",
            "time": INITIAL_TIME_MILLISECONDS + fill_index,
            "isBuyer": True,
            "isMaker": False,
            "isBestMatch": True,
        }

    def _fill_quantities(self) -> tuple[Decimal, Decimal, Decimal]:
        """
        함수 이름: _fill_quantities()
        기능: Filter 후 제출 수량을 1/4·1/4·1/2 누적 fill로 나눈다.
        인자: 없음
        반환값: 전체 수량과 정확히 같은 세 Decimal fill
        작성 날짜: 2026/08/29
        """
        submitted_quantity = self._require_submitted_quantity()
        with localcontext() as decimal_context:
            decimal_context.prec = 34
            decimal_context.rounding = ROUND_HALF_EVEN
            quarter_quantity = submitted_quantity / Decimal("4")
            final_quantity = submitted_quantity - (
                quarter_quantity * Decimal("2")
            )

        return (
            quarter_quantity,
            quarter_quantity,
            final_quantity,
        )  # 마지막 fill이 나눗셈 잔여를 흡수해 정확한 전체를 복원한다.

    def _assert_same_order_query(
        self,
        query: Mapping[str, list[str]],
    ) -> None:
        """
        함수 이름: _assert_same_order_query()
        기능: Order·myTrades 조회가 제출 시 확정한 동일 orderId인지 검증한다.
        인자: query -> signed request query mapping
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        if self._single_query_value(query, "symbol") != SYMBOL:
            raise AssertionError("same-order query symbol changed")
        if self._single_query_value(query, "orderId") != str(
            EXCHANGE_ORDER_ID
        ):
            raise AssertionError("same-order query orderId changed")

    def _require_client_order_id(self) -> str:
        """
        함수 이름: _require_client_order_id()
        기능: Submit에서 확정한 client order ID를 반환한다.
        인자: 없음
        반환값: non-empty client order ID
        작성 날짜: 2026/08/29
        """
        if self.client_order_id is None:
            raise AssertionError("order must be submitted before response mapping")

        return self.client_order_id  # 후속 응답은 최초 POST의 식별자만 사용한다.

    def _require_submitted_quantity(self) -> Decimal:
        """
        함수 이름: _require_submitted_quantity()
        기능: Submit에서 filter·서명을 끝낸 수량을 반환한다.
        인자: 없음
        반환값: 양수 Decimal 제출 수량
        작성 날짜: 2026/08/29
        """
        if self.submitted_quantity is None:
            raise AssertionError("order must be submitted before fill mapping")

        return self.submitted_quantity  # Query fixture가 수량을 다시 추측하지 않는다.

    @staticmethod
    def _single_query_value(
        query: Mapping[str, list[str]],
        field_name: str,
    ) -> str:
        """
        함수 이름: _single_query_value()
        기능: Parse된 query에서 정확히 하나의 문자열 값을 읽는다.
        인자: query -> parse_qs mapping
            field_name -> 읽을 field 이름
        반환값: 하나의 query field 값
        작성 날짜: 2026/08/29
        """
        values = query.get(field_name)
        if not isinstance(values, list) or len(values) != 1:
            raise AssertionError(f"{field_name} must occur exactly once")

        return values[0]  # 중복 parameter는 signing provenance를 불명하게 하므로 거부한다.


@dataclass(slots=True)
class PublicSpotRESTCase2Fixture:
    """
    클래스 이름: PublicSpotRESTCase2Fixture
    기능: Public market path와 production REST·Position·History owner를 한 fixture로 묶는다.
    작성 날짜: 2026/08/29
    """

    clock: MutableUtcClock
    transport: LocalCase2HTTPTransport
    rest_client: BinanceSpotRESTClient
    api_gateway: APIGateway
    market_controller: MarketDataController
    trading_controller: TradingController
    position: Position
    history_controller: TradeHistoryController
    repository: TradeHistoryRepository
    history_path: Path


def _create_fixture(temporary_directory: str) -> PublicSpotRESTCase2Fixture:
    """
    함수 이름: _create_fixture()
    기능: Production SpotRESTClient·Gateway·Controller로 RUNNING TYPE_0 세션을 만든다.
    인자: temporary_directory -> durable JSONL history를 둘 임시 경로
    반환값: PublicSpotRESTCase2Fixture
    작성 날짜: 2026/08/29
    """
    # REST signing, domain result와 Context가 모두 같은 결정론적 UTC clock을 읽는다.
    clock = MutableUtcClock(INITIAL_TIME)
    transport = LocalCase2HTTPTransport()
    rest_client = BinanceSpotRESTClient(
        API_KEY,
        SECRET_KEY,
        transport=transport,
        clock=clock,
        result_clock=clock,
    )
    api_gateway = APIGateway(rest_client, clock=clock)
    account = Account()
    position = Position()
    market_snapshot = MarketSnapshot(symbol=SYMBOL, clock=clock)
    context = TradingContext(clock=clock)

    # Terminal fill은 실제 repository의 JSONL commit을 통과해야 history에 공개된다.
    history_path = Path(temporary_directory) / "public-spot-rest-case2.jsonl"
    repository = TradeHistoryRepository(history_path, clock=clock)
    history_controller = TradeHistoryController(
        repository,
        clock=clock,
        account=account,
    )
    history_controller.load_trade_history()

    # Account stream만 memory fake로 열고 REST와 APIGateway는 production 객체를 그대로 사용한다.
    account_web_socket_gateway = WebSocketGateway(
        SynchronousAccountWebSocketClient([]),
        account_snapshot_callback=account.apply_stream_snapshot,
    )
    trading_controller = TradingController(
        api_gateway,
        account_web_socket_gateway,
        account,
        market_snapshot,
        command_gate=True,
        context=context,
        position=position,
        trade_history_controller=history_controller,
        risk_policy_state=create_test_risk_policy(),
        clock=clock,
    )
    market_controller = MarketDataController(
        api_gateway,
        WebSocketGateway(FakeWebSocketClient([])),
        market_snapshot,
        NoOpRegimeController(),
        kline_limit=21,
        market_evaluation_builder=ThirtyMinuteMarketEvaluationBuilder(
            monotonic_clock=lambda: 0,
        ),
        trading_market_observer=trading_controller,
    )
    market_controller.initialize_market_data()

    # Public operation으로 Account·TYPE_0·BUY/SELL split을 설정한 뒤 세션을 시작한다.
    trading_controller.load_account()
    selection = RegimeController(
        RegimeSTM(),
        market_snapshot,
        trading_controller,
    ).set_regime_type(
        RegimeType.TYPE_0,
        command_id="select-public-spot-rest-case2",
        expected_version=trading_controller.context.version,
    )
    split_result = trading_controller.update_split_ratios(
        command_id="split-public-spot-rest-case2",
        expected_version=selection.version,
        scale_in=Decimal("0.5"),
        scale_out=Decimal("1"),
    )
    trading_controller.start_trading(
        command_id="start-public-spot-rest-case2",
        expected_version=split_result.version,
    )

    return PublicSpotRESTCase2Fixture(
        clock=clock,
        transport=transport,
        rest_client=rest_client,
        api_gateway=api_gateway,
        market_controller=market_controller,
        trading_controller=trading_controller,
        position=position,
        history_controller=history_controller,
        repository=repository,
        history_path=history_path,
    )


def _create_live_kline(
    *,
    close_price: Decimal,
    candle_low: Decimal,
    event_time: datetime,
) -> Kline:
    """
    함수 이름: _create_live_kline()
    기능: Public observe_kline에 전달할 진행 30분봉 OHLC를 만든다.
    인자: close_price -> 현재 realtime 종가
        candle_low -> 진행봉 누적 저가
        event_time -> source WebSocket event UTC 시각
    반환값: 불변 Kline
    작성 날짜: 2026/08/29
    """
    return Kline(
        symbol=SYMBOL,
        interval=Interval.THIRTY_MINUTES,
        open_time=CURRENT_THIRTY_MINUTE_OPEN,
        open=Decimal("100"),
        high=Decimal("101"),
        low=candle_low,
        close=close_price,
        volume=Decimal("10"),
        closed=False,
        event_time=event_time,
    )  # Public observer는 production Kline 검증과 지표 계산을 그대로 수행한다.


def _trigger_public_case_c_buy(
    fixture: PublicSpotRESTCase2Fixture,
) -> None:
    """
    함수 이름: _trigger_public_case_c_buy()
    기능: Public 30분 Kline 세 개로 Case C setup·flush·BUY submit을 발생시킨다.
    인자: fixture -> production REST 경계가 연결된 통합 fixture
    반환값: 없음
    작성 날짜: 2026/08/29
    """
    # 하단 과이탈 setup과 깊은 flush를 서로 다른 public event cycle로 반영한다.
    for close_price, candle_low in (
        (Decimal("90"), Decimal("89")),
        (Decimal("85"), Decimal("84")),
    ):
        fixture.clock.advance(timedelta(seconds=5))
        fixture.market_controller.observe_kline(
            _create_live_kline(
                close_price=close_price,
                candle_low=candle_low,
                event_time=fixture.clock(),
            )
        )
        asyncio.run(fixture.trading_controller.drain_events())

    # +0.06 %B 회복 event가 production SubmitOrder·APIGateway·SpotRESTClient를 순서대로 실행한다.
    fixture.clock.advance(timedelta(seconds=5))
    fixture.market_controller.observe_kline(
        _create_live_kline(
            close_price=Decimal("88.6"),
            candle_low=Decimal("84"),
            event_time=fixture.clock(),
        )
    )
    asyncio.run(fixture.trading_controller.drain_events())


class PublicMarketSpotRESTCase2FlowTests(unittest.TestCase):
    """
    클래스 이름: PublicMarketSpotRESTCase2FlowTests
    기능: Public market event가 production Spot REST submit·query·myTrades를 통과하는지 검증한다.
    작성 날짜: 2026/08/29
    """

    def test_public_case_c_partials_use_real_spot_rest_boundary_once(
        self,
    ) -> None:
        """
        함수 이름: test_public_case_c_partials_use_real_spot_rest_boundary_once()
        기능: Public Kline이 production REST의 submit·getOrder·myTrades를 통해 Trade 하나로 수렴하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with TemporaryDirectory() as temporary_directory:
            fixture = _create_fixture(temporary_directory)

            # Private action seam 없이 public market observer에서 첫 partial POST까지 진행한다.
            _trigger_public_case_c_buy(fixture)
            submitted_quantity = fixture.transport.submitted_quantity
            self.assertIsNotNone(submitted_quantity)
            if submitted_quantity is None:
                raise AssertionError("local POST must capture submitted quantity")
            self.assertEqual(0, fixture.transport.order_query_count)
            self.assertGreater(fixture.position.quantity, Decimal("0"))
            self.assertLess(fixture.position.quantity, submitted_quantity)
            self.assertEqual((), fixture.history_controller.trade_history.trades)

            # 1초 재조회는 동일 orderId의 누적 두 fill을 myTrades에서 병합한다.
            first_due_at = fixture.clock.advance(timedelta(seconds=1))
            first_outcomes = (
                fixture.trading_controller.trigger_order_reconciliation(
                    occurred_at=first_due_at,
                )
            )
            self.assertEqual((), first_outcomes)
            self.assertEqual(1, fixture.transport.order_query_count)
            self.assertEqual(1, fixture.transport.my_trades_query_count)
            first_partial_quantity = fixture.position.quantity
            self.assertGreater(first_partial_quantity, Decimal("0"))
            self.assertLess(first_partial_quantity, submitted_quantity)
            self.assertEqual((), fixture.history_controller.trade_history.trades)

            # 2초 backoff 재조회는 세 fill의 FILLED를 적용하고 terminal outcome을 queue에 넣는다.
            second_due_at = fixture.clock.advance(timedelta(seconds=2))
            terminal_outcomes = (
                fixture.trading_controller.trigger_order_reconciliation(
                    occurred_at=second_due_at,
                )
            )
            self.assertEqual(1, len(terminal_outcomes))
            self.assertIs(
                terminal_outcomes[0].event_type,
                TradingEventType.CASE_C_POSITION_OPENED,
            )
            asyncio.run(fixture.trading_controller.drain_events())

            # Production mapper·aggregate가 누적 fill을 delta로만 반영해 Position과 Trade를 한 번만 완성한다.
            self.assertEqual(2, fixture.transport.order_query_count)
            self.assertEqual(2, fixture.transport.my_trades_query_count)
            self.assertEqual(submitted_quantity, fixture.position.quantity)
            self.assertIs(fixture.position.owner, StrategyType.CASE_C)
            trades = fixture.history_controller.trade_history.trades
            self.assertEqual(1, len(trades))
            self.assertEqual(submitted_quantity, trades[0].executed_quantity)
            self.assertEqual(trades, fixture.repository.get_trade_history())
            self.assertEqual(
                1,
                len(
                    fixture.history_path.read_text(
                        encoding="utf-8"
                    ).splitlines()
                ),
            )
            self.assertIs(
                fixture.trading_controller.status,
                TradingSessionStatus.RUNNING,
            )

            # Terminal 후 재호출은 schedule가 없어 REST request·Trade를 추가하지 않는다.
            request_count_after_fill = len(fixture.transport.requests)
            duplicate_outcomes = (
                fixture.trading_controller.trigger_order_reconciliation(
                    occurred_at=fixture.clock.advance(timedelta(seconds=8)),
                )
            )
            self.assertEqual((), duplicate_outcomes)
            self.assertEqual(
                request_count_after_fill,
                len(fixture.transport.requests),
            )
            self.assertEqual(1, len(fixture.history_controller.trade_history.trades))

            # Communication 6.1·8.1·8.2와 실제 HTTP endpoint 횟수가 동일 provenance에서 일치한다.
            trace_ids = tuple(
                trace_entry.message_id
                for trace_entry in (
                    fixture.trading_controller.order_execution_trace
                )
            )
            self.assertEqual(1, trace_ids.count("6.1"))
            self.assertEqual(2, trace_ids.count("8.1"))
            self.assertEqual(2, trace_ids.count("8.2"))
            order_posts = [
                request
                for request in fixture.transport.requests
                if request["method"] == "POST"
                and str(request["path"]).endswith("/v3/order")
            ]
            order_queries = [
                request
                for request in fixture.transport.requests
                if request["method"] == "GET"
                and str(request["path"]).endswith("/v3/order")
            ]
            trade_queries = [
                request
                for request in fixture.transport.requests
                if str(request["path"]).endswith("/v3/myTrades")
            ]
            self.assertEqual(1, len(order_posts))
            self.assertEqual(2, len(order_queries))
            self.assertEqual(2, len(trade_queries))
            self.assertTrue(
                all(
                    request["query"]["orderId"]
                    == [str(EXCHANGE_ORDER_ID)]
                    for request in (*order_queries, *trade_queries)
                )
            )

            # Signed private request는 API key와 signature를 갖지만 secret 평문을 어떤 기록에도 남기지 않는다.
            private_requests = [
                request
                for request in fixture.transport.requests
                if str(request["path"]).endswith(
                    ("/v3/account", "/v3/order", "/v3/myTrades")
                )
            ]
            self.assertTrue(private_requests)
            self.assertTrue(
                all(
                    request["headers"].get("X-MBX-APIKEY") == API_KEY
                    for request in private_requests
                )
            )
            self.assertTrue(
                all(
                    "signature" in request["query"]
                    or (
                        request["body"] is not None
                        and b"signature=" in request["body"]
                    )
                    for request in private_requests
                )
            )
            self.assertNotIn(SECRET_KEY, repr(fixture.transport.requests))
            self.assertIsInstance(fixture.rest_client, BinanceSpotRESTClient)
            self.assertIsInstance(fixture.api_gateway, APIGateway)
            self.assertIs(
                fixture.trading_controller.context.pending_order,
                None,
            )
            self.assertIs(
                fixture.trading_controller.context.runtime.pending_order_id,
                None,
            )
            self.assertIsNotNone(fixture.transport.client_order_id)
            self.assertEqual(
                Decimal("88.6"),
                fixture.trading_controller.context.market.realtime_price,
            )
            self.assertIs(
                fixture.history_controller.trade_history.trades[0].side,
                OrderSide.BUY,
            )
            self.assertIs(
                fixture.history_controller.trade_history.trades[0].strategy,
                StrategyType.CASE_C,
            )
            self.assertEqual(
                str(EXCHANGE_ORDER_ID),
                fixture.history_controller.trade_history.trades[0].order_id,
            )
            self.assertEqual(
                fixture.transport.client_order_id,
                fixture.history_controller.trade_history.trades[
                    0
                ].client_order_id,
            )
