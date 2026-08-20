"""Repository, TradeHistory와 Performance startup 복원을 조정한다."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Protocol

from binance_auto_trader.domain.history import Performance, Trade, TradeHistory


class TradeHistoryRepositoryPort(Protocol):
    """
    클래스 이름: TradeHistoryRepositoryPort
    기능: TradeHistoryController가 startup 거래 목록을 읽기 위해 요구하는 최소 port를 정의한다.
    작성 날짜: 2026/08/21
    """

    def get_trade_history(self) -> tuple[Trade, ...]:
        """
        함수 이름: get_trade_history()
        기능: durable storage에서 복원한 canonical Trade tuple을 반환한다.
        인자: 없음
        반환값: startup Trade tuple
        작성 날짜: 2026/08/21
        """
        ...


@dataclass(frozen=True, slots=True)
class _TradeHistoryLoadState:
    """
    클래스 이름: _TradeHistoryLoadState
    기능: 같은 durable 거래 목록에서 만든 TradeHistory와 Performance를 원자적으로 묶는다.
    작성 날짜: 2026/08/21
    """

    trade_history: TradeHistory
    performance: Performance


class TradeHistoryController:
    """
    클래스 이름: TradeHistoryController
    기능: Repository 조회 후 TradeHistory와 Performance를 순서대로 완성해 함께 publish한다.
    작성 날짜: 2026/08/21
    """

    __slots__ = (
        "_clock",
        "_load_lock",
        "_repository",
        "_state",
        "_state_lock",
    )

    def __init__(
        self,
        repository: TradeHistoryRepositoryPort,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: repository와 Performance account-day clock 및 빈 초기 state를 준비한다.
        인자: repository -> startup Trade tuple을 제공할 persistence port
            clock -> Performance의 현재 KST 날짜를 결정할 optional UTC clock
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        if repository is None or not callable(
            getattr(repository, "get_trade_history", None)
        ):
            raise TypeError("repository must provide get_trade_history")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")

        self._repository = repository
        self._clock = clock
        self._load_lock = RLock()
        self._state_lock = RLock()
        self._state = _TradeHistoryLoadState(
            trade_history=TradeHistory(),
            performance=Performance((), clock=clock),
        )

    @property
    def trade_history(self) -> TradeHistory:
        """
        함수 이름: trade_history()
        기능: 마지막 성공 load에서 publish한 TradeHistory를 반환한다.
        인자: 없음
        반환값: 현재 TradeHistory
        작성 날짜: 2026/08/21
        """
        with self._state_lock:
            return self._state.trade_history

    @property
    def performance(self) -> Performance:
        """
        함수 이름: performance()
        기능: 마지막 성공 load에서 TradeHistory와 함께 publish한 Performance를 반환한다.
        인자: 없음
        반환값: 현재 Performance
        작성 날짜: 2026/08/21
        """
        with self._state_lock:
            return self._state.performance

    def load_trade_history(self) -> TradeHistory:
        """
        함수 이름: load_trade_history()
        기능: Repository, TradeHistory, Performance 순서로 local 복원 후 결과를 원자 교체한다.
        인자: 없음
        반환값: 새로 publish한 TradeHistory
        작성 날짜: 2026/08/21
        """
        with self._load_lock:
            restored_trades = self._repository.get_trade_history()
            next_trade_history = TradeHistory(restored_trades)
            next_performance = Performance(
                next_trade_history.trades,
                clock=self._clock,
            )
            next_state = _TradeHistoryLoadState(
                trade_history=next_trade_history,
                performance=next_performance,
            )

            with self._state_lock:
                self._state = next_state

            return next_trade_history
