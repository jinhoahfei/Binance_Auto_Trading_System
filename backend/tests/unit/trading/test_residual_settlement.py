"""Sub-step 잔여의 원가 보존·실패 내구성·재시작 이력 결속을 검증한다."""

from dataclasses import replace
from decimal import Decimal, localcontext
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from binance_auto_trader.adapters.persistence.residual_repository import ResidualRepository
from binance_auto_trader.application.residual_settlement import ResidualSettlement
from binance_auto_trader.domain.trading.position import Position
from binance_auto_trader.domain.trading.states import OrderSide
from tests.unit.history.factories import make_trade


def closed_lot(quantity: str = "1.9", *, base_fee: bool = True):
    """
    함수 이름: closed_lot()
    기능: ETH 수수료 매수와 전량 요청의 실제 부분 수량 매도를 생성한다.
    인자: quantity -> 실제 매도량, base_fee -> ETH 수수료 유무
    반환값: Trade tuple과 terminal Position
    작성 날짜: 2026/09/08
    """
    buy = make_trade(fee_amount=Decimal("0.002") if base_fee else Decimal("0"), fee_quote_amount=Decimal("0.2") if base_fee else Decimal("0"))
    position = Position()
    position.apply_historical_trade(buy)
    executed = Decimal(quantity)
    allocated = position.get_cost_basis(executed)
    with localcontext() as context:
        context.prec = 34
        amount = executed * Decimal("110")
        pnl = amount - allocated
        rate = (pnl / allocated * 100).quantize(Decimal("0.00000001"))
    sell = make_trade(
        order_id="2", trade_id="trade-2", side=OrderSide.SELL,
        requested_quantity=position.quantity, executed_quantity=executed,
        executed_amount=amount, average_fill_price=Decimal("110"),
        fee_asset="USDT", fee_amount=Decimal("0"), fee_quote_amount=Decimal("0"),
        allocated_cost_basis=allocated, realized_pnl=pnl, realized_return_rate=rate,
    )
    position.apply_historical_trade(sell)
    return (buy, sell), position


class ResidualSettlementTests(unittest.TestCase):
    """
    클래스 이름: ResidualSettlementTests
    기능: 잔여는 매도가 아니며 단위·출처·저장이 모두 맞아야 분리됨을 검증한다.
    작성 날짜: 2026/09/08
    """

    def test_rounding_requires_current_lot_fee_and_exact_replay(self) -> None:
        """
        함수 이름: test_rounding_requires_current_lot_fee_and_exact_replay()
        기능: 주문 전 내림 허가가 수수료 없는 lot이나 변조 수량으로 확장되지 않게 한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        with TemporaryDirectory() as directory:
            service = ResidualSettlement(ResidualRepository(Path(directory).resolve() / "residual-ledger.json"))
            for fee, expected in ((True, True), (False, False)):
                trades, _ = closed_lot(base_fee=fee)
                position = Position()
                position.apply_historical_trade(trades[0])
                self.assertEqual(service.allows_rounding(position, trades[:1], position.quantity - Decimal("0.01"), Decimal("0.1")), expected)
                self.assertFalse(service.allows_rounding(position, trades[:1], position.quantity - Decimal("0.1"), Decimal("0.1")))

    def test_filter_tightening_and_multi_lot_replay(self) -> None:
        """
        함수 이름: test_filter_tightening_and_multi_lot_replay()
        기능: 현재 단위보다 큰 장부 단위를 거부하고 새 lot의 원가를 이전 잔여와 분리한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        with TemporaryDirectory() as directory:
            service = ResidualSettlement(ResidualRepository(Path(directory).resolve() / "residual-ledger.json"))
            trades, position = closed_lot()
            service.settle(position, trades, Decimal("0.1"))
            with self.assertRaises(ValueError):
                service.restore(Position(), trades, Decimal("0.01"))
            new_buy = replace(trades[0], order_id="3", client_order_id="client-3", trade_id="trade-3")
            restored = Position()
            service.restore(restored, (*trades, new_buy), Decimal("0.1"))
            self.assertEqual(restored.quantity, new_buy.executed_quantity - new_buy.fee_amount)
            self.assertEqual(service.totals[0], Decimal("0.098"))

    def test_cost_conservation_and_restart(self) -> None:
        """
        함수 이름: test_cost_conservation_and_restart()
        기능: 원가·ETH 보존과 fresh replay·동일 이관 멱등성을 검사한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        with TemporaryDirectory() as directory:
            storage = ResidualRepository(Path(directory).resolve() / "residual-ledger.json")
            service = ResidualSettlement(storage)
            trades, position = closed_lot()
            expected = (position.quantity, position.cost_basis)
            self.assertTrue(service.settle(position, trades, Decimal("0.1")))
            self.assertEqual(position.quantity, 0)
            self.assertEqual(service.totals, expected)
            self.assertEqual(expected[0], Decimal("0.098"))
            restored = Position()
            fresh = ResidualSettlement(storage)
            fresh.restore(restored, trades, Decimal("0.1"))
            self.assertEqual(restored.quantity, 0)
            self.assertEqual(fresh.totals, expected)
            self.assertFalse(fresh.settle(restored, trades, Decimal("0.1")))
            self.assertEqual(len(storage.load()), 1)  # 재실행은 매도 Trade나 이관을 추가하지 않는다.

    def test_non_fee_partial_or_step_sized_remainder_is_not_hidden(self) -> None:
        """
        함수 이름: test_non_fee_partial_or_step_sized_remainder_is_not_hidden()
        기능: 수수료 없는 잔여·분할 매도·단위 이상 잔여는 열린 Position으로 보존한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        with TemporaryDirectory() as directory:
            service = ResidualSettlement(ResidualRepository(Path(directory).resolve() / "residual-ledger.json"))
            for quantity, fee in (("1.8", True), ("1.95", False)):
                trades, position = closed_lot(quantity, base_fee=fee)
                snapshot = position.get_snapshot()
                self.assertFalse(service.settle(position, trades, Decimal("0.1")))
                self.assertEqual(position.get_snapshot(), snapshot)
            trades, position = closed_lot()
            trades = (trades[0], replace(trades[1], requested_quantity=Decimal("1.9")))
            self.assertFalse(service.settle(position, trades, Decimal("0.1")))

    def test_storage_failure_does_not_close_position(self) -> None:
        """
        함수 이름: test_storage_failure_does_not_close_position()
        기능: fsync 실패에서는 열린 자산을 보존하고 재시작으로 성공한 rename을 복원한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        with TemporaryDirectory() as directory:
            storage = ResidualRepository(Path(directory).resolve() / "residual-ledger.json")
            service = ResidualSettlement(storage)
            trades, position = closed_lot()
            snapshot = position.get_snapshot()
            with patch.object(storage, "save", side_effect=OSError("disk")):
                with self.assertRaises(OSError):
                    service.settle(position, trades, Decimal("0.1"))
            self.assertEqual(position.get_snapshot(), snapshot)
            service.settle(position, trades, Decimal("0.1"))
            # 저장 완료·메모리 분리 전 process 중단 상태도 단 한 번의 이관으로 복구한다.
            _, old_memory = closed_lot()
            self.assertTrue(service.settle(old_memory, trades, Decimal("0.1")))
            self.assertEqual(len(storage.load()), 1)

    def test_corrupt_history_and_symlink_fail_closed(self) -> None:
        """
        함수 이름: test_corrupt_history_and_symlink_fail_closed()
        기능: 삭제·변조한 체결 prefix와 다른 파일을 가리키는 장부를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        with TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "residual-ledger.json"
            storage = ResidualRepository(path)
            service = ResidualSettlement(storage)
            trades, position = closed_lot()
            service.settle(position, trades, Decimal("0.1"))
            with self.assertRaises(ValueError):
                service.restore(Position(), trades[:1])
            with self.assertRaises(ValueError):
                service.restore(Position(), (replace(trades[0], trade_id="changed"), trades[1]))
            target = path.with_name("original.json")
            path.rename(target)
            path.symlink_to(target)
            with self.assertRaises(ValueError):
                storage.load()
