"""order ID idempotency와 KST 조회를 소유하는 TradeHistory를 정의한다."""

from collections.abc import Iterable
from decimal import Decimal
from threading import RLock
from zoneinfo import ZoneInfo

from ..trading.states import OrderSide
from .query import TradeHistoryQuery, TradeSide
from .trade import Trade


_KOREA_TIME_ZONE = ZoneInfo("Asia/Seoul")


class OrderHistoryConflictError(ValueError):
    """
    클래스 이름: OrderHistoryConflictError
    기능: 동일 order ID에 서로 다른 Trade 내용이 연결된 durable history 충돌을 나타낸다.
    작성 날짜: 2026/08/21
    """

    code = "ORDER_HISTORY_CONFLICT"


class TradeHistory:
    """
    클래스 이름: TradeHistory
    기능: canonical Trade 목록과 order ID index를 보존하고 KST 조건 조회를 제공한다.
    작성 날짜: 2026/08/21
    """

    __slots__ = (
        "_lock",
        "_trades",
        "_trades_by_order_id",
    )

    def __init__(self, trades: Iterable[Trade] = ()) -> None:
        """
        함수 이름: __init__()
        기능: 입력 순서를 보존하며 order ID index를 재구성한 TradeHistory를 생성한다.
        인자: trades -> startup에서 복원한 Trade iterable
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        try:
            incoming_trades = tuple(trades)
        except TypeError as error:
            raise TypeError("trades must be an iterable of Trade") from error

        self._lock = RLock()
        self._trades: list[Trade] = []
        self._trades_by_order_id: dict[str, Trade] = {}
        for trade in incoming_trades:
            self._add_trade_without_lock(trade)

    @property
    def trades(self) -> tuple[Trade, ...]:
        """
        함수 이름: trades()
        기능: 현재 거래 목록의 불변 tuple snapshot을 반환한다.
        인자: 없음
        반환값: 입력 순서를 보존한 Trade tuple
        작성 날짜: 2026/08/21
        """
        with self._lock:
            return tuple(self._trades)

    def add_trade(self, trade: Trade) -> None:
        """
        함수 이름: add_trade()
        기능: 새 order ID Trade를 추가하고 동일 내용 중복은 idempotent no-op 처리한다.
        인자: trade -> 추가할 불변 Trade
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        with self._lock:
            self._add_trade_without_lock(trade)

    def find(self, query: TradeHistoryQuery) -> tuple[Trade, ...]:
        """
        함수 이름: find()
        기능: UTC 체결 시각을 KST 날짜로 변환해 양끝 포함 날짜와 side로 조회한다.
        인자: query -> KST 날짜 범위와 거래 방향 조건
        반환값: 입력 순서를 보존한 matching Trade tuple
        작성 날짜: 2026/08/21
        """
        if not isinstance(query, TradeHistoryQuery):
            raise TypeError("query must be a TradeHistoryQuery")

        with self._lock:
            current_trades = tuple(self._trades)

        matching_trades = []
        for trade in current_trades:
            executed_date = trade.executed_at.astimezone(_KOREA_TIME_ZONE).date()
            if not query.start_date <= executed_date <= query.end_date:
                continue
            if query.side is TradeSide.BUY and trade.side is not OrderSide.BUY:
                continue
            if query.side is TradeSide.SELL and trade.side is not OrderSide.SELL:
                continue
            matching_trades.append(trade)

        return tuple(matching_trades)

    def get_entry_prices(self) -> dict[str, Decimal | None]:
        """
        함수 이름: get_entry_prices()
        기능: 필터 전 전체 이력에서 각 매수와 그 뒤 매도의 직전 매수 평균 체결가를 연결한다.
        인자: 없음
        반환값: order ID별 수수료를 포함하지 않은 매수 평균 체결가 또는 근거 없음의 None
        작성 날짜: 2026/09/10
        """
        entry_prices = {}
        latest_buy = None
        for trade in self.trades:
            if trade.side is OrderSide.BUY:
                latest_buy = trade
                entry_prices[trade.order_id] = trade.average_fill_price
            else:
                entry_prices[trade.order_id] = (
                    latest_buy.average_fill_price
                    if latest_buy is not None
                    and latest_buy.executed_at <= trade.executed_at
                    and (latest_buy.symbol, latest_buy.strategy, latest_buy.regime_type)
                    == (trade.symbol, trade.strategy, trade.regime_type)
                    else None
                )
        return entry_prices

    def _add_trade_without_lock(self, trade: Trade) -> None:
        """
        함수 이름: _add_trade_without_lock()
        기능: 호출자가 lock을 소유한 상태에서 Trade 타입과 order ID idempotency를 적용한다.
        인자: trade -> index에 반영할 불변 Trade
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        if not isinstance(trade, Trade):
            raise TypeError("trade must be a Trade")

        existing_trade = self._trades_by_order_id.get(trade.order_id)
        if existing_trade is not None:
            if existing_trade == trade:
                return
            raise OrderHistoryConflictError(
                f"order_id {trade.order_id} has conflicting trade content"
            )

        self._trades.append(trade)
        self._trades_by_order_id[trade.order_id] = trade
