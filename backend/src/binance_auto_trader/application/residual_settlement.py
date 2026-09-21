"""기존 Position·Trade 사이의 잔여 이관과 durable replay를 조정한다."""

from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from typing import Protocol

from binance_auto_trader.domain.history.trade import Trade
from binance_auto_trader.domain.trading.position import Position
from binance_auto_trader.domain.trading.residual import EarnResidualEvidence, ResidualTransfer, allocate_external_residual, history_digest
from binance_auto_trader.domain.trading.states import ExitReason, OrderSide


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
        self._external_consumptions = ()
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
            context.rounding = ROUND_HALF_EVEN
            quantity = cost = Decimal("0")
            events = [(entry.history_count, entry.quantity, entry.cost_basis) for entry in self.transfers]
            events.extend((count, -used_quantity, -used_cost) for count, used_quantity, used_cost in self._external_consumptions)
            for _, change_quantity, change_cost in sorted(events):
                quantity += change_quantity
                cost += change_cost
            return quantity, cost  # 실제 배분 순서로 재생해 전량 소비 시 원가 반올림 잔량도 남기지 않는다.

    def apply_external_trade(self, position: Position, trade: Trade, history_count: int) -> None:
        """
        함수 이름: apply_external_trade()
        기능: 원래 SELL의 수량·원가를 검증하고 포지션과 잔여의 소비를 같은 이력에 결속한다.
        인자: position -> 현재 lot, trade -> 실제 외부 SELL, history_count -> 적용할 이력 순번
        반환값: 없음
        작성 날짜: 2026/09/20
        """
        if trade.schema_version != 4 or trade.exit_reason is not ExitReason.EXTERNAL_MANUAL or trade.side is not OrderSide.SELL:
            raise ValueError("residual consumption requires an external SELL")
        totals = self.totals
        with localcontext() as context:
            context.prec = 34
            if trade.requested_quantity > position.quantity + totals[0]:
                raise ValueError("external requested quantity exceeds durable principal")
        quantity, cost = allocate_external_residual(position.quantity, trade.executed_quantity, *totals)
        position.apply_historical_trade(trade, residual_quantity=quantity, residual_cost_basis=cost)
        if quantity:
            self._external_consumptions = (*self._external_consumptions, (history_count, quantity, cost))

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
        replay = ResidualSettlement(self.storage)
        for count, trade in enumerate(trades, 1):
            previous_quantity = position.quantity
            if previous_quantity == 0:
                fee_seen = False
            if trade.side is OrderSide.BUY and trade.base_fee_amount > 0:
                fee_seen = True
            entry = entries.get(count)
            effective_step = entry.step_size if entry is not None else current_step_size
            with localcontext() as context:
                context.prec = 34
                external_full_step = (
                    trade.exit_reason is ExitReason.EXTERNAL_MANUAL
                    and effective_step is not None and effective_step > 0
                    and trade.requested_quantity == trade.executed_quantity
                    and trade.executed_quantity == (previous_quantity // effective_step) * effective_step
                )  # 외부 주문의 실제 origQty를 보존하며 매도 가능한 전량이 체결됐는지 확인한다.
            eligible = trade.side is OrderSide.SELL and fee_seen and (trade.requested_quantity == previous_quantity or external_full_step)
            if trade.exit_reason is ExitReason.EXTERNAL_MANUAL:
                replay.apply_external_trade(position, trade, count)
            else:
                position.apply_historical_trade(trade)
            if entry is not None:
                # 이력 내용·전량 매도 의도·원가를 모두 재검증해 장부만 바꿔 Position을 숨기지 못한다.
                if not eligible or entry.history_sha256 != history_digest(trades[:count]):
                    raise ValueError("residual transfer provenance mismatch")
                position.detach_residual(entry.quantity, entry.cost_basis, entry.step_size)
                replay.transfers = (*replay.transfers, entry)
                eligible = False
        position.require_history_accounting_compatibility()
        self.transfers = transfers
        self._external_consumptions = replay._external_consumptions
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

    def matches_earn_custody(self, evidence: EarnResidualEvidence, trades: tuple[Trade, ...]) -> bool:
        """
        함수 이름: matches_earn_custody()
        기능: 각 자동 예치가 당시 잔여와 검증된 상환액 안에 있는지 시간순으로 검증한다.
        인자: evidence -> 완전한 AUTO/SPOT 예치 근거, trades -> 검증된 이력
        반환값: 예치된 원금이 잔여에서 나왔음을 설명하면 True
        작성 날짜: 2026/09/15
        """
        from datetime import datetime, timezone

        if not self.transfers or type(evidence) is not EarnResidualEvidence:
            return False
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        trade_times = []
        first_fill_times = []
        for trade in trades:
            delta = trade.executed_at - epoch
            trade_times.append((delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000)
            first_fill = min((fill.executed_at for fill in trade.fee_fills), default=trade.executed_at)
            delta = first_fill - epoch
            first_fill_times.append((delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000)
        with localcontext() as context:
            context.prec = 34
            deposited = Decimal("0")
            for _, timestamp, amount in sorted(evidence.subscriptions, key=lambda entry: (entry[1], entry[0])):
                available = Decimal("0")
                for transfer in self.transfers:
                    transfer_ms = trade_times[transfer.history_count - 1]
                    if transfer_ms < timestamp:
                        available += transfer.quantity
                # 이미 상환된 원금·검증된 보상의 재예치는 같은 자산을 새 원금으로 중복 계산하지 않는다.
                available += sum((entry[2] for entry in evidence.redemptions if entry[1] < timestamp), Decimal("0"))
                available -= sum((quantity for count, quantity, _ in self._external_consumptions
                    if first_fill_times[count - 1] <= timestamp), Decimal("0"))
                deposited += amount
                if deposited > available:
                    return False  # 전략 보유량의 자동 예치를 잔여 예치로 바꾸지 않는다.
            # 현재는 상환돼 있어도 매도 당시 Earn에 묶여 있던 원금을 소비할 수 없다.
            consumed = Decimal("0")
            for count, quantity, _ in self._external_consumptions:
                timestamp = first_fill_times[count - 1]
                available = sum((entry.quantity for entry in self.transfers
                    if trade_times[entry.history_count - 1] < timestamp), Decimal("0"))
                available += sum((entry[2] for entry in evidence.redemptions if entry[1] < timestamp), Decimal("0"))
                available -= sum((entry[2] for entry in evidence.subscriptions if entry[1] <= timestamp), Decimal("0"))
                consumed += quantity
                if consumed > available:
                    return False
        return True

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
