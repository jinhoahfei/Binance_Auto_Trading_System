"""기존 Position·Trade 사이의 잔여 이관과 durable replay를 조정한다."""

from decimal import Decimal, localcontext
from typing import Protocol

from binance_auto_trader.domain.history.trade import Trade
from binance_auto_trader.domain.trading.position import Position
from binance_auto_trader.domain.trading.residual import ResidualTransfer, history_digest
from binance_auto_trader.domain.trading.states import OrderSide


class ResidualStorage(Protocol):
    """
    클래스 이름: ResidualStorage
    기능: 잔여 회계가 파일 adapter를 직접 참조하지 않도록 저장 계약을 정의한다.
    작성 날짜: 2026/09/08
    """

    def load(self) -> tuple[ResidualTransfer, ...]:
        """
        함수 이름: load()
        기능: 검증된 이관 tuple을 읽는다.
        인자: 없음
        반환값: 잔여 이관 tuple
        작성 날짜: 2026/09/08
        """
        ...

    def save(self, previous: tuple[ResidualTransfer, ...], transfer: ResidualTransfer) -> None:
        """
        함수 이름: save()
        기능: 기존 prefix에 단일 이관을 내구 저장한다.
        인자: previous -> 기존 prefix, transfer -> 새 이관
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        ...


class ResidualSettlement:
    """
    클래스 이름: ResidualSettlement
    기능: 수수료 출처와 전량 매도 의도가 증명된 sub-step ETH만 별도 장부로 분리한다.
    작성 날짜: 2026/09/08
    """

    def __init__(self, storage: ResidualStorage) -> None:
        """
        함수 이름: __init__()
        기능: 저장 port와 마지막 검증된 잔여 snapshot을 초기화한다.
        인자: storage -> 잔여 전용 저장 port
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        self.storage = storage
        self.transfers = ()
        self._open_base_fee = False

    @property
    def totals(self) -> tuple[Decimal, Decimal]:
        """
        함수 이름: totals()
        기능: 미실현 잔여 자산의 총 ETH와 원가를 계산한다.
        인자: 없음
        반환값: quantity, cost_basis
        작성 날짜: 2026/09/08
        """
        with localcontext() as context:
            context.prec = 34
            return (
                sum((entry.quantity for entry in self.transfers), Decimal("0")),
                sum((entry.cost_basis for entry in self.transfers), Decimal("0")),
            )  # 잔여 장부에는 실현손익을 생성하지 않는다.

    def restore(self, position: Position, trades: tuple[Trade, ...], current_step_size: Decimal | None = None) -> bool:
        """
        함수 이름: restore()
        기능: Trade와 이관을 원래 순서로 재생하고 마지막 전량 매도의 ETH 수수료 출처를 반환한다.
        인자: position -> 빈 Position, trades -> 검증된 durable 체결 이력, current_step_size -> 현재 공식 LOT_SIZE 단위
        반환값: 마지막 체결이 수수료 있는 lot의 전량 매도 의도이면 True
        작성 날짜: 2026/09/08
        """
        transfers = self.storage.load()
        if transfers and (not isinstance(current_step_size, Decimal) or not current_step_size.is_finite() or current_step_size <= 0 or any(entry.step_size > current_step_size for entry in transfers)):
            raise ValueError("residual step requires current official filter reconciliation")
        entries = {entry.history_count: entry for entry in transfers}
        if len(entries) != len(transfers) or any(count > len(trades) for count in entries):
            raise ValueError("residual ledger has missing or duplicate history")
        fee_seen = False
        eligible = False
        for count, trade in enumerate(trades, 1):
            previous_quantity = position.quantity
            if previous_quantity == 0:
                fee_seen = False
            if trade.side is OrderSide.BUY and trade.base_fee_amount > 0:
                fee_seen = True
            eligible = trade.side is OrderSide.SELL and trade.requested_quantity == previous_quantity and fee_seen
            position.apply_historical_trade(trade)
            entry = entries.get(count)
            if entry is not None:
                # 이력 내용·전량 매도 의도·원가를 모두 재검증해 장부만 바꿔 Position을 숨기지 못한다.
                if not eligible or entry.history_sha256 != history_digest(trades[:count]):
                    raise ValueError("residual transfer provenance mismatch")
                position.detach_residual(entry.quantity, entry.cost_basis, entry.step_size)
                eligible = False
        position.require_history_accounting_compatibility()
        self.transfers = transfers
        self._open_base_fee = fee_seen and position.quantity > 0
        return eligible

    def allows_rounding(self, position: Position, trades: tuple[Trade, ...], submitted: Decimal, step_size: Decimal) -> bool:
        """
        함수 이름: allows_rounding()
        기능: live 전량 매도 전 열린 lot의 ETH 수수료와 durable 수량·원가를 검증한다.
        인자: position -> 현재 lot, trades -> durable 이력, submitted -> filter 수량, step_size -> 공식 단위
        반환값: 출처가 검증된 sub-step 내림만 True
        작성 날짜: 2026/09/08
        """
        replayed = Position()
        self.restore(replayed, trades, step_size)
        # 과거 닫힌 lot의 ETH fee를 새 lot의 수량 내림 허가로 재사용하지 않는다.
        return (
            self._open_base_fee
            and (position.quantity, position.cost_basis) == (replayed.quantity, replayed.cost_basis)
            and 0 < submitted < position.quantity
            and position.quantity - submitted < step_size
        )

    def settle(self, position: Position, trades: tuple[Trade, ...], step_size: Decimal) -> bool:
        """
        함수 이름: settle()
        기능: durable 체결에 결속한 잔여 이관을 저장한 뒤 전략 Position을 닫는다.
        인자: position -> 실제 현재 Position, trades -> 저장 완료 체결, step_size -> 공식 LOT_SIZE 단위
        반환값: 이 호출이 잔여를 분리했으면 True
        작성 날짜: 2026/09/08
        """
        replayed = Position()
        eligible = self.restore(replayed, trades, step_size)
        if self.transfers and self.transfers[-1].history_count == len(trades):
            entry = self.transfers[-1]
            if position.quantity > 0:
                position.detach_residual(entry.quantity, entry.cost_basis, entry.step_size)
                return True  # Rename 성공·메모리 반영 전 중단도 같은 durable 이관 하나로 수렴한다.
            return False
        if not eligible or not isinstance(step_size, Decimal) or not step_size.is_finite() or not 0 < position.quantity < step_size:
            return False
        if (position.quantity, position.cost_basis) != (replayed.quantity, replayed.cost_basis):
            raise ValueError("live Position differs from durable residual replay")
        entry = ResidualTransfer(len(trades), history_digest(trades), position.quantity, position.cost_basis, step_size)

        # fsync가 불명하면 Position을 닫지 않고 caller가 reconciliation을 유지한다.
        self.storage.save(self.transfers, entry)
        self.transfers = (*self.transfers, entry)
        position.detach_residual(entry.quantity, entry.cost_basis, entry.step_size)
        return True
