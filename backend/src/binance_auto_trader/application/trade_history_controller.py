"""Repository, TradeHistory와 Performance startup 복원을 조정한다."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from threading import RLock
from typing import Protocol

from binance_auto_trader.domain.history import Performance, Trade, TradeHistory
from binance_auto_trader.domain.trading.order import ExecutionSummary, Order
from binance_auto_trader.domain.trading.states import OrderSide


class TradeHistoryPersistencePendingError(RuntimeError):
    """
    클래스 이름: TradeHistoryPersistencePendingError
    기능: 미완료 durable save가 있는 동안 새 history operation을 차단한다.
    작성 날짜: 2026/08/22
    """

    code = "TRADE_HISTORY_PERSISTENCE_PENDING"


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

    def save_this_trade_by_order_id(
        self,
        order_id: int | str,
        trade: Trade,
    ) -> None:
        """
        함수 이름: save_this_trade_by_order_id()
        기능: 한 terminal Trade를 order ID 기준으로 durable 저장한다.
        인자: order_id -> exchange order ID
            trade -> 저장할 canonical Trade
        반환값: 없음
        작성 날짜: 2026/08/22
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


@dataclass(frozen=True, slots=True)
class _PendingPublication:
    """
    클래스 이름: _PendingPublication
    기능: 저장 재시도 성공 뒤 함께 게시할 Trade와 history/performance 후보를 묶는다.
    작성 날짜: 2026/08/22
    """

    trade: Trade
    state: _TradeHistoryLoadState


class TradeHistoryController:
    """
    클래스 이름: TradeHistoryController
    기능: startup 복원과 terminal execution의 durable 저장 및 원자 publication을 조정한다.
    작성 날짜: 2026/08/22
    """

    __slots__ = (
        "_clock",
        "_operation_lock",
        "_pending_publications",
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
        작성 날짜: 2026/08/22
        """
        # runtime Protocol을 만족하지 않는 repository와 잘못된 clock을 state 생성 전에 거부한다.
        if repository is None or not callable(
            getattr(repository, "get_trade_history", None)
        ):
            raise TypeError("repository must provide get_trade_history")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")

        # load와 record가 서로의 candidate를 덮지 않게 operation lock 하나로 직렬화한다.
        self._repository = repository
        self._clock = clock
        self._operation_lock = RLock()
        self._pending_publications: dict[str, _PendingPublication] = {}
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
        작성 날짜: 2026/08/22
        """
        with self._state_lock:
            return self._state.trade_history  # history와 performance가 공유하는 한 state에서 읽는다.

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
            return self._state.performance.get_performance()  # 같은 근거로 KST 날짜 경계만 현재 clock에 맞게 갱신한다.

    @property
    def dirty_order_ids(self) -> frozenset[str]:
        """
        함수 이름: dirty_order_ids()
        기능: durable save 재시도를 기다려 publication이 보류된 order ID를 반환한다.
        인자: 없음
        반환값: 불변 dirty order ID 집합
        작성 날짜: 2026/08/22
        """
        with self._operation_lock:
            return frozenset(
                self._pending_publications
            )  # caller가 retry 대기 index를 변경하지 못하게 복사한다.

    def load_trade_history(self) -> TradeHistory:
        """
        함수 이름: load_trade_history()
        기능: Repository, TradeHistory, Performance 순서로 local 복원 후 결과를 원자 교체한다.
        인자: 없음
        반환값: 새로 publish한 TradeHistory
        작성 날짜: 2026/08/21
        """
        with self._operation_lock:
            # 보류 candidate가 있으면 disk와 published state 중 어느 쪽도 새 load로 덮지 않는다.
            if self._pending_publications:
                raise TradeHistoryPersistencePendingError(
                    "pending trade persistence must be reconciled before load"
                )

            # Repository 결과로 두 domain candidate를 완성한 뒤 한 번에 게시한다.
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

            # history와 performance candidate가 모두 완성된 뒤 공유 state 하나만 교체한다.
            with self._state_lock:
                self._state = next_state

            return next_trade_history  # 반환값도 방금 게시한 state의 동일 객체다.

    def record_order_execution(
        self,
        order: Order,
        summary: ExecutionSummary,
        allocated_cost_basis: Decimal | None = None,
    ) -> Trade:
        """
        함수 이름: record_order_execution()
        기능: realized 계산부터 Trade 생성·후보 반영·durable 저장 후 원자 게시까지 조정한다.
        인자: order -> terminal 주문 의도와 식별자를 보존한 Order
            summary -> 같은 주문의 누적 fill ExecutionSummary
            allocated_cost_basis -> SELL Position 변경 전에 고정한 취득원가
        반환값: durable 저장과 publication을 마친 Trade
        작성 날짜: 2026/08/22
        """
        # aggregate와 summary는 구조가 비슷한 임의 객체가 아닌 canonical domain 타입만 받는다.
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")
        if not isinstance(summary, ExecutionSummary):
            raise TypeError("summary must be an ExecutionSummary")

        with self._operation_lock:
            # 이전 save의 내구성이 불명인 동안 다음 execution candidate 생성을 차단한다.
            if self._pending_publications:
                raise TradeHistoryPersistencePendingError(
                    "pending trade persistence blocks new execution records"
                )

            # 메시지 13.1은 SELL에만 적용하고 BUY의 원가는 반드시 null로 유지한다.
            with self._state_lock:
                current_state = self._state
            realized_result = None
            if order.side is OrderSide.SELL:
                if allocated_cost_basis is None:
                    raise ValueError(
                        "allocated_cost_basis is required for SELL"
                    )
                realized_result = current_state.performance.calculate_realized_result(
                    summary,
                    allocated_cost_basis,
                )
            elif allocated_cost_basis is not None:
                raise ValueError("allocated_cost_basis must be None for BUY")

            # 메시지 13.2~13.4를 공개되지 않은 local candidate에서 순서대로 실행한다.
            trade = Trade.from_order_execution(
                order,
                summary,
                realized_result,
            )
            next_trade_history = TradeHistory(current_state.trade_history.trades)
            next_trade_history.add_trade(trade)
            next_performance = Performance(
                current_state.trade_history.trades,
                clock=self._clock,
            )
            next_performance.apply_new_trade(trade)
            next_state = _TradeHistoryLoadState(
                trade_history=next_trade_history,
                performance=next_performance,
            )
            pending_publication = _PendingPublication(
                trade=trade,
                state=next_state,
            )

            # 메시지 13.5가 성공하기 전에는 history와 performance를 publish하지 않는다.
            save_trade = getattr(
                self._repository,
                "save_this_trade_by_order_id",
                None,
            )
            if not callable(save_trade):
                raise TypeError(
                    "repository must provide save_this_trade_by_order_id"
                )
            try:
                save_trade(trade.order_id, trade)
            except Exception:
                self._pending_publications[trade.order_id] = pending_publication
                raise  # 공개 state는 유지하고 동일 Trade의 save-only retry 근거만 보존한다.

            with self._state_lock:
                self._state = next_state  # 두 공개 snapshot을 같은 state 교체로 게시한다.

            return trade  # durable save와 두 domain publication이 모두 완료된 Trade다.

    def retry_pending_persistence(self, order_id: str) -> Trade:
        """
        함수 이름: retry_pending_persistence()
        기능: 실패한 동일 order의 저장만 재시도하고 성공 시 보류 state를 원자 게시한다.
        인자: order_id -> dirty_order_ids에 포함된 canonical exchange order ID
        반환값: durable 저장과 publication을 마친 기존 Trade
        작성 날짜: 2026/08/22
        """
        # retry key는 repository와 같은 canonical positive-integer 문자열만 허용한다.
        if not isinstance(order_id, str):
            raise TypeError("order_id must be a string")
        if not order_id or not order_id.isascii() or not order_id.isdigit():
            raise ValueError("order_id must be a positive integer string")

        with self._operation_lock:
            # 보류 index에서 정확히 같은 order candidate만 읽어 새 Trade 생성을 피한다.
            pending_publication = self._pending_publications.get(order_id)
            if pending_publication is None:
                raise KeyError(f"order_id {order_id} has no pending persistence")

            # 원 주문을 재제출하지 않고 같은 Trade의 repository save만 재시도한다.
            save_trade = getattr(
                self._repository,
                "save_this_trade_by_order_id",
                None,
            )
            if not callable(save_trade):
                raise TypeError(
                    "repository must provide save_this_trade_by_order_id"
                )
            save_trade(order_id, pending_publication.trade)

            # 재저장 성공 뒤에만 원래 candidate를 게시하고 마지막에 dirty key를 제거한다.
            with self._state_lock:
                self._state = pending_publication.state
            del self._pending_publications[order_id]  # 게시 완료 뒤 dirty lock을 해제한다.

            return pending_publication.trade  # 최초 실패 때 만든 동일 immutable Trade다.
