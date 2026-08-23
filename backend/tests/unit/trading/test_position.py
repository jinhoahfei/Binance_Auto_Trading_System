"""Position의 Decimal 평균 원가, 소유권과 원자적 execution 반영을 검증한다."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.trading import (
    Position,
    PositionStateSnapshot,
    PositionStatus,
)
from binance_auto_trader.domain.trading.context import (
    PositionSnapshot as TradingContextPositionSnapshot,
)
from binance_auto_trader.domain.trading.order import ExecutionSummary, Fill
from binance_auto_trader.domain.trading.position import (
    LegacyFeeAccountingMigrationRequiredError,
)
from binance_auto_trader.domain.trading.states import (
    ExitReason,
    OrderSide,
    StrategyType,
)

from tests.unit.history.factories import make_trade


FIRST_EXECUTED_AT = datetime(2026, 8, 22, 1, 0, tzinfo=timezone.utc)
SECOND_EXECUTED_AT = datetime(2026, 8, 22, 1, 1, tzinfo=timezone.utc)


def _execution_summary(
    *,
    side: OrderSide,
    quantity: str,
    price: str,
    fee_quote_amount: str = "0",
    fee_amount: str | None = None,
    fee_asset: str = "USDT",
    strategy: StrategyType = StrategyType.CASE_B,
    exit_reason: ExitReason | None = None,
    exchange_order_id: str = "1",
    executed_at: datetime = FIRST_EXECUTED_AT,
) -> ExecutionSummary:
    """
    함수 이름: _execution_summary()
    기능: 한 USDT 또는 ETH fee fill과 정확히 일치하는 immutable execution fixture를 생성한다.
    인자: side -> BUY 또는 SELL 방향
        quantity -> 실제 체결 수량 Decimal 문자열
        price -> 실제 fill 가격 Decimal 문자열
        fee_quote_amount -> quote 환산 수수료 Decimal 문자열
        fee_amount -> 원래 fee asset 수수료 문자열 또는 None이면 quote 값
        fee_asset -> USDT 또는 ETH 수수료 자산
        strategy -> 주문 의도를 소유한 Case 전략
        exit_reason -> SELL 청산 사유 또는 BUY이면 None
        exchange_order_id -> fill과 summary가 공유할 거래소 주문 ID
        executed_at -> 실제 체결 UTC 시각
    반환값: Position에 적용할 ExecutionSummary
    작성 날짜: 2026/08/22
    """
    # 문자열 fixture를 float 경유 없이 Order DTO와 동일한 Decimal 값으로 변환한다.
    executed_quantity = Decimal(quantity)
    average_fill_price = Decimal(price)
    selected_fee_amount = Decimal(
        fee_quote_amount if fee_amount is None else fee_amount
    )

    # 실제 fill 하나에서 quantity, amount, average와 fee aggregate를 동일하게 만든다.
    fill = Fill(
        exchange_order_id=exchange_order_id,
        trade_id=f"trade-{exchange_order_id}",
        quantity=executed_quantity,
        price=average_fill_price,
        fee_amount=selected_fee_amount,
        fee_asset=fee_asset,
        fee_quote_amount=Decimal(fee_quote_amount),
        executed_at=executed_at,
    )
    executed_amount = executed_quantity * average_fill_price

    return ExecutionSummary(
        exchange_order_id=exchange_order_id,
        client_order_id=f"client-{exchange_order_id}",
        symbol="ETHUSDT",
        side=side,
        strategy=strategy,
        regime_type=RegimeType.TYPE_0,
        exit_reason=exit_reason,
        requested_quantity=executed_quantity,
        submitted_quantity=executed_quantity,
        executed_quantity=executed_quantity,
        executed_amount=executed_amount,
        average_fill_price=average_fill_price,
        fee_amount=selected_fee_amount,
        fee_asset=fee_asset,
        fee_quote_amount=Decimal(fee_quote_amount),
        executed_at=executed_at,
        fills=(fill,),
    )


def _corrupt_summary(
    summary: ExecutionSummary,
    **changed_fields: object,
) -> ExecutionSummary:
    """
    함수 이름: _corrupt_summary()
    기능: Position boundary 자체의 방어를 검증하도록 frozen DTO validation을 우회한 fixture를 만든다.
    인자: summary -> 복제할 정상 ExecutionSummary
        changed_fields -> 의도적으로 손상할 field와 값
    반환값: 지정 field만 손상된 ExecutionSummary fixture
    작성 날짜: 2026/08/22
    """
    corrupted_summary = object.__new__(ExecutionSummary)

    # Gateway 경계 뒤 메모리 손상과 같은 비정상 DTO를 재현하되 원본 fixture는 보존한다.
    for summary_field in fields(ExecutionSummary):
        object.__setattr__(
            corrupted_summary,
            summary_field.name,
            getattr(summary, summary_field.name),
        )
    for field_name, field_value in changed_fields.items():
        object.__setattr__(
            corrupted_summary,
            field_name,
            field_value,
        )

    return corrupted_summary  # 실제 ExecutionSummary identity는 유지해 Position 검증까지 도달시킨다.


class PositionTests(unittest.TestCase):
    """
    클래스 이름: PositionTests
    기능: Position의 average-cost 계산, immutable snapshot과 fail-closed mutation을 검증한다.
    작성 날짜: 2026/08/22
    """

    def test_starts_closed_and_exposes_distinct_immutable_snapshot(self) -> None:
        """
        함수 이름: test_starts_closed_and_exposes_distinct_immutable_snapshot()
        기능: 초기값과 Context용 snapshot과 구분되는 frozen Position snapshot을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        position = Position()
        snapshot = position.get_snapshot()

        # Entity snapshot은 기존 TradingContext의 경량 read model과 이름과 타입이 다르다.
        self.assertIsInstance(snapshot, PositionStateSnapshot)
        self.assertIsNot(PositionStateSnapshot, TradingContextPositionSnapshot)
        self.assertEqual(snapshot.symbol, "ETHUSDT")
        self.assertIsNone(snapshot.owner)
        self.assertEqual(snapshot.quantity, Decimal("0"))
        self.assertEqual(snapshot.average_entry_price, Decimal("0"))
        self.assertEqual(snapshot.cost_basis, Decimal("0"))
        self.assertIsNone(snapshot.entered_at)
        self.assertIs(snapshot.status, PositionStatus.CLOSED)
        self.assertIsNone(snapshot.exit_reason)

        # 공개된 이전 snapshot은 Entity 내부나 호출자 양쪽에서 변경할 수 없어야 한다.
        with self.assertRaises(FrozenInstanceError):
            setattr(snapshot, "quantity", Decimal("1"))

    def test_rejects_unsupported_position_symbol(self) -> None:
        """
        함수 이름: test_rejects_unsupported_position_symbol()
        기능: Phase 8의 ETHUSDT 전용 Position이 다른 symbol을 fail closed하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        with self.assertRaises(ValueError):
            Position(symbol="BTCUSDT")

    def test_buy_fill_sets_owner_and_includes_quote_fee_in_cost_basis(self) -> None:
        """
        함수 이름: test_buy_fill_sets_owner_and_includes_quote_fee_in_cost_basis()
        기능: 실제 양수 BUY fill 뒤에만 owner와 fee 포함 평균 원가가 생기는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        position = Position()
        self.assertIsNone(position.owner)  # 주문 의도만으로 owner를 선반영하지 않는다.

        # 2 ETH의 200 USDT 체결 금액과 1 USDT fee를 하나의 BUY로 반영한다.
        position.apply_execution(
            _execution_summary(
                side=OrderSide.BUY,
                quantity="2",
                price="100",
                fee_quote_amount="1",
            )
        )

        snapshot = position.get_snapshot()
        self.assertIs(snapshot.owner, StrategyType.CASE_B)
        self.assertEqual(snapshot.quantity, Decimal("2"))
        self.assertEqual(snapshot.cost_basis, Decimal("201"))
        self.assertEqual(snapshot.average_entry_price, Decimal("100.5"))
        self.assertEqual(snapshot.entered_at, FIRST_EXECUTED_AT)
        self.assertIs(snapshot.status, PositionStatus.OPEN)
        self.assertIsNone(snapshot.exit_reason)

    def test_buy_base_fee_uses_net_quantity_without_double_cost(self) -> None:
        """
        함수 이름: test_buy_base_fee_uses_net_quantity_without_double_cost()
        기능: ETH 수수료 BUY가 net 보유량과 실제 quote debit 한 번만 원가에 반영되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        position = Position()

        # 1 ETH gross BUY에서 0.001 ETH fee를 내면 0.999 ETH와 100 USDT 원가만 남는다.
        position.apply_execution(
            _execution_summary(
                side=OrderSide.BUY,
                quantity="1",
                price="100",
                fee_amount="0.001",
                fee_asset="ETH",
                fee_quote_amount="0.1",
            )
        )

        snapshot = position.get_snapshot()
        self.assertEqual(snapshot.quantity, Decimal("0.999"))
        self.assertEqual(snapshot.cost_basis, Decimal("100"))
        self.assertEqual(
            snapshot.average_entry_price,
            Decimal("100.1001001001001001001001001001001"),
        )

    def test_legacy_v1_base_fee_trade_preserves_gross_accounting_and_blocks_execution(
        self,
    ) -> None:
        """
        함수 이름: test_legacy_v1_base_fee_trade_preserves_gross_accounting_and_blocks_execution()
        기능: v1 ETH-fee BUY의 기존 수량·원가를 보존하고 열린 lot의 신규 execution을 차단한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        position = Position()
        legacy_trade = make_trade(
            schema_version=1,
            executed_quantity=Decimal("1"),
            executed_amount=Decimal("100"),
            average_fill_price=Decimal("100"),
            fee_amount=Decimal("0.001"),
            fee_asset="ETH",
            fee_quote_amount=Decimal("0.1"),
        )

        # v1은 문서화됐던 gross 수량과 quote 환산 fee 포함 원가를 바꾸지 않는다.
        position.apply_historical_trade(legacy_trade)

        snapshot = position.get_snapshot()
        self.assertEqual(snapshot.quantity, Decimal("1"))
        self.assertEqual(snapshot.cost_basis, Decimal("100.1"))
        self.assertEqual(snapshot.average_entry_price, Decimal("100.1"))
        self.assertTrue(position.requires_legacy_fee_accounting_migration)
        with self.assertRaises(
            LegacyFeeAccountingMigrationRequiredError
        ) as captured_error:
            position.require_history_accounting_compatibility()
        self.assertEqual(
            captured_error.exception.code,
            "HISTORY_ACCOUNTING_MIGRATION_REQUIRED",
        )

        # Startup owner가 gate 검사를 빠뜨려도 새 fill이 legacy lot을 섞지 못한다.
        with self.assertRaises(LegacyFeeAccountingMigrationRequiredError):
            position.apply_execution(
                _execution_summary(
                    side=OrderSide.BUY,
                    quantity="1",
                    price="100",
                )
            )
        self.assertEqual(position.get_snapshot(), snapshot)

        # v2 history가 legacy lot을 닫아 migration 표식을 지우는 혼합 replay도 차단한다.
        current_sell = make_trade(
            schema_version=2,
            trade_id="trade-2",
            order_id="2",
            side=OrderSide.SELL,
        )
        with self.assertRaises(LegacyFeeAccountingMigrationRequiredError):
            position.apply_historical_trade(current_sell)
        self.assertEqual(position.get_snapshot(), snapshot)

    def test_v2_base_fee_trade_replays_with_net_asset_flow(self) -> None:
        """
        함수 이름: test_v2_base_fee_trade_replays_with_net_asset_flow()
        기능: v2 ETH-fee BUY를 net 수량과 실제 quote debit 원가로 복원한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        position = Position()
        current_trade = make_trade(
            schema_version=2,
            executed_quantity=Decimal("1"),
            executed_amount=Decimal("100"),
            average_fill_price=Decimal("100"),
            fee_amount=Decimal("0.001"),
            fee_asset="ETH",
            fee_quote_amount=Decimal("0.1"),
        )

        position.apply_historical_trade(current_trade)

        snapshot = position.get_snapshot()
        self.assertEqual(snapshot.quantity, Decimal("0.999"))
        self.assertEqual(snapshot.cost_basis, Decimal("100"))
        self.assertEqual(
            snapshot.average_entry_price,
            Decimal("100.1001001001001001001001001001001"),
        )
        self.assertFalse(position.requires_legacy_fee_accounting_migration)
        position.require_history_accounting_compatibility()  # v2 lot은 별도 migration 없이 거래 가능하다.

    def test_closed_legacy_base_fee_cycle_clears_migration_gate(self) -> None:
        """
        함수 이름: test_closed_legacy_base_fee_cycle_clears_migration_gate()
        기능: v1 base-fee lot이 기존 공식으로 전량 청산되면 이후 거래 gate가 남지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        position = Position()
        legacy_buy = make_trade(schema_version=1)
        legacy_sell = make_trade(
            schema_version=1,
            trade_id="trade-2",
            order_id="2",
            side=OrderSide.SELL,
            executed_quantity=Decimal("2"),
            executed_amount=Decimal("220"),
            average_fill_price=Decimal("110"),
            fee_amount=Decimal("0.002"),
            fee_asset="ETH",
            fee_quote_amount=Decimal("0.22"),
            allocated_cost_basis=Decimal("200.20"),
            realized_pnl=Decimal("19.58"),
            realized_return_rate=Decimal("9.78021978"),
            exit_reason=ExitReason.TAKE_PROFIT,
        )

        # 닫힌 legacy cycle은 과거 파생값을 보존하되 현재 주문 재개를 막지 않는다.
        position.apply_historical_trade(legacy_buy)
        position.apply_historical_trade(legacy_sell)

        self.assertIs(position.status, PositionStatus.CLOSED)
        self.assertFalse(position.requires_legacy_fee_accounting_migration)
        position.require_history_accounting_compatibility()

    def test_sell_base_fee_fails_closed_without_position_mutation(self) -> None:
        """
        함수 이름: test_sell_base_fee_fails_closed_without_position_mutation()
        기능: 별도 base depletion 계약이 없는 ETH 수수료 SELL을 원자적으로 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        position = Position()
        position.apply_execution(
            _execution_summary(
                side=OrderSide.BUY,
                quantity="1",
                price="100",
            )
        )
        snapshot_before = position.get_snapshot()

        # SELL base fee는 gross 매도량 외 추가 ETH depletion과 PnL 정책이 없어 reconciliation 대상이다.
        with self.assertRaisesRegex(ValueError, "SELL base fee"):
            position.apply_execution(
                _execution_summary(
                    side=OrderSide.SELL,
                    quantity="0.5",
                    price="110",
                    fee_amount="0.001",
                    fee_asset="ETH",
                    fee_quote_amount="0.11",
                    exit_reason=ExitReason.TAKE_PROFIT,
                    exchange_order_id="901",
                )
            )

        self.assertEqual(position.get_snapshot(), snapshot_before)

    def test_multiple_buys_recalculate_average_and_preserve_first_entry_time(self) -> None:
        """
        함수 이름: test_multiple_buys_recalculate_average_and_preserve_first_entry_time()
        기능: 동일 owner의 여러 BUY가 Decimal 누적 원가로 평균가를 다시 계산하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        position = Position()

        # 첫 BUY와 다음 scale-in BUY의 금액과 fee를 각각 실제 체결 기준으로 적용한다.
        position.apply_execution(
            _execution_summary(
                side=OrderSide.BUY,
                quantity="2",
                price="100",
                fee_quote_amount="1",
                exchange_order_id="10",
            )
        )
        position.apply_execution(
            _execution_summary(
                side=OrderSide.BUY,
                quantity="1",
                price="121",
                fee_quote_amount="0.2",
                exchange_order_id="11",
                executed_at=SECOND_EXECUTED_AT,
            )
        )

        snapshot = position.get_snapshot()
        self.assertEqual(snapshot.quantity, Decimal("3"))
        self.assertEqual(snapshot.cost_basis, Decimal("322.2"))
        self.assertEqual(snapshot.average_entry_price, Decimal("107.4"))
        self.assertEqual(snapshot.entered_at, FIRST_EXECUTED_AT)
        self.assertIs(snapshot.owner, StrategyType.CASE_B)

    def test_get_cost_basis_precedes_partial_sell_and_exactly_reduces_basis(self) -> None:
        """
        함수 이름: test_get_cost_basis_precedes_partial_sell_and_exactly_reduces_basis()
        기능: SELL 전 고정한 average-cost 배분액과 적용 후 감소액이 정확히 같은지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        position = Position()
        position.apply_execution(
            _execution_summary(
                side=OrderSide.BUY,
                quantity="4",
                price="100",
                fee_quote_amount="4",
                exchange_order_id="20",
            )
        )

        # Performance 계산용 allocated cost basis를 Position mutation보다 먼저 고정한다.
        allocated_cost_basis = position.get_cost_basis(Decimal("1"))
        cost_basis_before = position.cost_basis
        self.assertEqual(allocated_cost_basis, Decimal("101"))

        position.apply_execution(
            _execution_summary(
                side=OrderSide.SELL,
                quantity="1",
                price="120",
                fee_quote_amount="0.1",
                exit_reason=ExitReason.TAKE_PROFIT,
                exchange_order_id="21",
                executed_at=SECOND_EXECUTED_AT,
            )
        )

        snapshot = position.get_snapshot()
        self.assertEqual(snapshot.quantity, Decimal("3"))
        self.assertEqual(
            snapshot.cost_basis,
            cost_basis_before - allocated_cost_basis,
        )
        self.assertEqual(snapshot.cost_basis, Decimal("303"))
        self.assertEqual(snapshot.average_entry_price, Decimal("101"))
        self.assertIs(snapshot.owner, StrategyType.CASE_B)
        self.assertEqual(snapshot.entered_at, FIRST_EXECUTED_AT)
        self.assertIs(snapshot.status, PositionStatus.OPEN)
        self.assertIs(snapshot.exit_reason, ExitReason.TAKE_PROFIT)

    def test_full_sell_clears_quantity_basis_average_owner_and_entry_time(self) -> None:
        """
        함수 이름: test_full_sell_clears_quantity_basis_average_owner_and_entry_time()
        기능: 전량 SELL 뒤 미세 잔여 없이 CLOSED 상태로 정규화하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        position = Position()
        position.apply_execution(
            _execution_summary(
                side=OrderSide.BUY,
                quantity="2",
                price="100",
                fee_quote_amount="1",
                exchange_order_id="30",
            )
        )
        allocated_cost_basis = position.get_cost_basis(Decimal("2"))

        # 전량 청산에는 현재 원가 전체를 고정하고 SELL fee는 Position 원가에 재반영하지 않는다.
        self.assertEqual(allocated_cost_basis, Decimal("201"))
        position.apply_execution(
            _execution_summary(
                side=OrderSide.SELL,
                quantity="2",
                price="110",
                fee_quote_amount="0.2",
                exit_reason=ExitReason.STOP,
                exchange_order_id="31",
                executed_at=SECOND_EXECUTED_AT,
            )
        )

        snapshot = position.get_snapshot()
        self.assertEqual(snapshot.quantity, Decimal("0"))
        self.assertEqual(snapshot.cost_basis, Decimal("0"))
        self.assertEqual(snapshot.average_entry_price, Decimal("0"))
        self.assertIsNone(snapshot.owner)
        self.assertIsNone(snapshot.entered_at)
        self.assertIs(snapshot.status, PositionStatus.CLOSED)
        self.assertIs(snapshot.exit_reason, ExitReason.STOP)

    def test_invalid_cost_basis_requests_do_not_mutate_position(self) -> None:
        """
        함수 이름: test_invalid_cost_basis_requests_do_not_mutate_position()
        기능: CLOSED, zero, non-Decimal과 보유량 초과 원가 요청을 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        position = Position()
        with self.assertRaises(ValueError):
            position.get_cost_basis(Decimal("1"))

        position.apply_execution(
            _execution_summary(
                side=OrderSide.BUY,
                quantity="1",
                price="100",
                exchange_order_id="40",
            )
        )
        snapshot_before = position.get_snapshot()

        # 잘못된 수량 유형과 범위는 같은 OPEN snapshot을 그대로 보존해야 한다.
        invalid_quantities = (Decimal("0"), Decimal("2"), 0.5)
        for invalid_quantity in invalid_quantities:
            with self.subTest(invalid_quantity=invalid_quantity):
                with self.assertRaises((TypeError, ValueError)):
                    position.get_cost_basis(invalid_quantity)  # type: ignore[arg-type]
                self.assertEqual(position.get_snapshot(), snapshot_before)

    def test_zero_bad_symbol_and_bad_side_summaries_are_atomic_no_ops(self) -> None:
        """
        함수 이름: test_zero_bad_symbol_and_bad_side_summaries_are_atomic_no_ops()
        기능: 손상된 summary를 거부하고 owner를 포함한 초기 Position을 전혀 바꾸지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        valid_summary = _execution_summary(
            side=OrderSide.BUY,
            quantity="1",
            price="100",
            exchange_order_id="50",
        )
        invalid_summaries = (
            _corrupt_summary(
                valid_summary,
                executed_quantity=Decimal("0"),
            ),
            _corrupt_summary(valid_summary, symbol="BTCUSDT"),
            _corrupt_summary(valid_summary, side="BUY"),
        )

        for invalid_summary in invalid_summaries:
            with self.subTest(summary=invalid_summary):
                position = Position()
                snapshot_before = position.get_snapshot()

                # Summary validation이 실패하면 owner조차 실제 fill로 오인해 설정하지 않는다.
                with self.assertRaises((TypeError, ValueError)):
                    position.apply_execution(invalid_summary)
                self.assertEqual(position.get_snapshot(), snapshot_before)
                self.assertIsNone(position.owner)

    def test_closed_sell_and_over_sell_leave_state_unchanged(self) -> None:
        """
        함수 이름: test_closed_sell_and_over_sell_leave_state_unchanged()
        기능: 없는 Position 매도와 실제 보유량 초과 매도를 원자적으로 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        position = Position()
        sell_summary = _execution_summary(
            side=OrderSide.SELL,
            quantity="1",
            price="100",
            exit_reason=ExitReason.STOP,
            exchange_order_id="60",
        )

        # CLOSED Position의 매도는 초기 state 전체를 그대로 유지한다.
        initial_snapshot = position.get_snapshot()
        with self.assertRaises(ValueError):
            position.apply_execution(sell_summary)
        self.assertEqual(position.get_snapshot(), initial_snapshot)

        position.apply_execution(
            _execution_summary(
                side=OrderSide.BUY,
                quantity="1",
                price="100",
                exchange_order_id="61",
            )
        )
        open_snapshot = position.get_snapshot()
        over_sell_summary = _execution_summary(
            side=OrderSide.SELL,
            quantity="2",
            price="100",
            exit_reason=ExitReason.STOP,
            exchange_order_id="62",
        )

        # 초과 매도가 거부된 뒤에도 원가, owner와 진입 시각이 같은 snapshot이어야 한다.
        with self.assertRaises(ValueError):
            position.apply_execution(over_sell_summary)
        self.assertEqual(position.get_snapshot(), open_snapshot)

    def test_different_strategy_cannot_buy_or_sell_owned_position(self) -> None:
        """
        함수 이름: test_different_strategy_cannot_buy_or_sell_owned_position()
        기능: 열린 Position의 owner와 다른 전략 execution을 양방향 모두 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        position = Position()
        position.apply_execution(
            _execution_summary(
                side=OrderSide.BUY,
                quantity="2",
                price="100",
                exchange_order_id="70",
            )
        )
        owned_snapshot = position.get_snapshot()

        # Case C가 Case B의 실제 Position을 scale-in하거나 청산할 수 없게 한다.
        conflicting_summaries = (
            _execution_summary(
                side=OrderSide.BUY,
                quantity="1",
                price="100",
                strategy=StrategyType.CASE_C,
                exchange_order_id="71",
            ),
            _execution_summary(
                side=OrderSide.SELL,
                quantity="1",
                price="100",
                strategy=StrategyType.CASE_C,
                exit_reason=ExitReason.STOP,
                exchange_order_id="72",
            ),
        )

        for conflicting_summary in conflicting_summaries:
            with self.subTest(side=conflicting_summary.side):
                with self.assertRaises(ValueError):
                    position.apply_execution(conflicting_summary)
                self.assertEqual(position.get_snapshot(), owned_snapshot)


if __name__ == "__main__":
    unittest.main()
