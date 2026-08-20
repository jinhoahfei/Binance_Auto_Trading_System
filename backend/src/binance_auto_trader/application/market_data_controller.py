"""REST와 WebSocket 시장 데이터를 원자적으로 초기화하는 controller를 정의한다."""

from threading import RLock

from binance_auto_trader.adapters.binance.api_gateway import APIGateway
from binance_auto_trader.adapters.binance.websocket_gateway import (
    WebSocketGateway,
)
from binance_auto_trader.domain.common import SUPPORTED_INTERVALS
from binance_auto_trader.domain.market import MarketSnapshot


MARKET_INTERVALS = SUPPORTED_INTERVALS
DEFAULT_KLINE_LIMIT = 500
MINIMUM_KLINE_LIMIT = 1
MAXIMUM_KLINE_LIMIT = 1000


class MarketDataController:
    """
    클래스 이름: MarketDataController
    기능: REST 조회 중 수신한 WebSocket 봉을 병합해 시장 snapshot을 초기화한다.
    작성 날짜: 2026/08/20
    """

    def __init__(
        self,
        api_gateway: APIGateway,
        web_socket_gateway: WebSocketGateway,
        market_snapshot: MarketSnapshot,
        kline_limit: int = DEFAULT_KLINE_LIMIT,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 주입된 Gateway와 동일 수명의 MarketSnapshot을 보존한다.
        인자: api_gateway -> 과거 Kline을 조회할 REST Gateway
            web_socket_gateway -> 초기 Kline buffer를 관리할 WebSocket Gateway
            market_snapshot -> 성공한 전체 초기화 결과를 반영할 시장 snapshot
            kline_limit -> 각 주기에서 조회할 Kline 개수
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        if isinstance(kline_limit, bool) or not isinstance(kline_limit, int):
            raise TypeError("kline_limit must be an integer")
        if not MINIMUM_KLINE_LIMIT <= kline_limit <= MAXIMUM_KLINE_LIMIT:
            raise ValueError("kline_limit must be between 1 and 1000")

        self._api_gateway = api_gateway
        self._web_socket_gateway = web_socket_gateway
        self._market_snapshot = market_snapshot
        self._kline_limit = kline_limit
        self._initialization_lock = RLock()

    def initialize_market_data(
        self,
        symbol: str = "ETHUSDT",
    ) -> MarketSnapshot:
        """
        함수 이름: initialize_market_data()
        기능: WebSocket 시작, REST 조회, buffer 배출, snapshot 갱신 순으로 초기화한다.
        인자: symbol -> 초기화할 Binance Spot 거래 symbol
        반환값: 주입 시 받은 것과 동일한 최신 MarketSnapshot
        작성 날짜: 2026/08/20
        """
        normalized_symbol = self._normalize_symbol(symbol)
        if normalized_symbol != self._market_snapshot.symbol:
            raise ValueError("symbol must match the MarketSnapshot symbol")

        with self._initialization_lock:
            return self._initialize_market_data(normalized_symbol)

    def _initialize_market_data(
        self,
        normalized_symbol: str,
    ) -> MarketSnapshot:
        """
        함수 이름: _initialize_market_data()
        기능: 직렬화된 한 초기화 시도의 네 단계를 수행한다.
        인자: normalized_symbol -> 검증과 정규화를 마친 Binance symbol
        반환값: 주입 시 받은 것과 동일한 최신 MarketSnapshot
        작성 날짜: 2026/08/20
        """
        subscription = self._web_socket_gateway.start_all_kline_buffering(
            symbol=normalized_symbol,
            intervals=MARKET_INTERVALS,
        )
        subscription_closed = False
        try:
            rest_klines = self._api_gateway.load_all_klines(
                symbol=normalized_symbol,
                limit=self._kline_limit,
            )
            buffered_klines = self._web_socket_gateway.drain_kline_buffer(
                subscription,
            )
            subscription.close()
            subscription_closed = True
            merged_klines = {
                interval: (
                    *rest_klines[interval],
                    *buffered_klines[interval],
                )
                for interval in MARKET_INTERVALS
            }

            # WebSocket 봉을 REST 봉 뒤에 배치해 같은 key에서 실시간 값을 우선한다.
            self._market_snapshot.update(merged_klines)
        except Exception as initialization_error:
            if not subscription_closed:
                try:
                    subscription.close()
                except Exception as close_error:
                    initialization_error.add_note(
                        f"subscription cleanup failed: {close_error!r}"
                    )
            raise

        return self._market_snapshot

    def _normalize_symbol(self, symbol: str) -> str:
        """
        함수 이름: _normalize_symbol()
        기능: 외부 입력 symbol을 Binance에서 사용하는 대문자 형식으로 검증한다.
        인자: symbol -> 호출자가 전달한 거래 symbol
        반환값: 공백을 제거한 대문자 거래 symbol
        작성 날짜: 2026/08/20
        """
        if not isinstance(symbol, str):
            raise TypeError("symbol must be a string")

        normalized_symbol = symbol.strip().upper()
        if (
            not normalized_symbol
            or not normalized_symbol.isascii()
            or not normalized_symbol.isalnum()
        ):
            raise ValueError("symbol must be a non-empty alphanumeric string")

        return normalized_symbol
