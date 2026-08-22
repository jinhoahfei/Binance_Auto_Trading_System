"""Phase 8 Order의 immutable 값, fill dedup, 상태 진행과 Decimal 집계를 검증한다."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.trading import (
    ACTIVE_ORDER_STATUSES,
    TERMINAL_ORDER_STATUSES,
    ExecutionSummaryUnavailableError,
    FeeAssetReconciliationRequiredError,
    Fill,
    MixedFeeAssetError,
    Order,
    OrderResult,
    OrderResultConflictError,
    OrderSide,
    OrderStateTransitionError,
    OrderStatus,
    StrategyType,
)


TEST_TIME = datetime(2026, 8, 22, 1, 0, tzinfo=timezone.utc)


def make_fill(
    trade_id: str,
    quantity: Decimal,
    price: Decimal,
    *,
    exchange_order_id: str = "1001",
    fee_amount: Decimal = Decimal("0.01"),
    fee_asset: str = "USDT",
    fee_quote_amount: Decimal | None = None,
    seconds_after: int = 0,
) -> Fill:
    """
    함수 이름: make_fill()
    기능: 고정 주문 ID와 UTC 시각을 가진 단위 테스트용 Fill을 만든다.
    인자: trade_id -> fill dedup 거래 ID
        quantity -> 체결 수량
        price -> fill 가격
        exchange_order_id -> 거래소 주문 ID
        fee_amount -> 원래 수수료 수량
        fee_asset -> 수수료 자산
        fee_quote_amount -> 명시적 quote 수수료 또는 자동 계산이면 None
        seconds_after -> 기준 시각 뒤 초 단위 offset
    반환값: immutable Fill
    작성 날짜: 2026/08/22
    """
    # USDT와 ETH의 명시된 환산 규칙으로 기본 quote fee를 결정한다.
    selected_fee_quote = fee_quote_amount
    if selected_fee_quote is None:
        selected_fee_quote = (
            fee_amount * price if fee_asset == "ETH" else fee_amount
        )

    return Fill(
        exchange_order_id=exchange_order_id,
        trade_id=trade_id,
        quantity=quantity,
        price=price,
        fee_amount=fee_amount,
        fee_asset=fee_asset,
        fee_quote_amount=selected_fee_quote,
        executed_at=TEST_TIME + timedelta(seconds=seconds_after),
    )


def make_order(
    *,
    requested_quantity: Decimal = Decimal("2"),
    submitted_quantity: Decimal = Decimal("1.5"),
    side: OrderSide = OrderSide.BUY,
) -> Order:
    """
    함수 이름: make_order()
    기능: original intent와 filter 후 제출 수량이 분리된 테스트 Order를 만든다.
    인자: requested_quantity -> filter 전 원래 수량
        submitted_quantity -> 실제 제출 수량
        side -> 주문 방향
    반환값: 결과 적용 전 mutable Order
    작성 날짜: 2026/08/22
    """
    return Order(
        intent_id="intent-case-b-1",
        client_order_id="client-case-b-1-0",
        submission_attempt=0,
        symbol="ETHUSDT",
        side=side,
        strategy=StrategyType.CASE_B,
        regime_type=RegimeType.TYPE_0,
        requested_quantity=requested_quantity,
        submitted_quantity=submitted_quantity,
        market_price_at_decision=Decimal("111.25"),
    )


def make_result(
    status: OrderStatus,
    fills: tuple[Fill, ...] = (),
    *,
    exchange_order_id: str | None = "1001",
    seconds_after: int = 0,
    failure_reason: str | None = None,
    retry_after: timedelta | None = None,
) -> OrderResult:
    """
    함수 이름: make_result()
    기능: 같은 local Order와 연결되는 테스트용 normalized OrderResult를 만든다.
    인자: status -> 관찰한 주문 상태
        fills -> 결과에 포함된 실제 Fill tuple
        exchange_order_id -> 거래소 주문 ID 또는 아직 불명이면 None
        seconds_after -> 기준 시각 뒤 처리 시각 offset
        failure_reason -> 선택적 typed failure 설명
        retry_after -> rate-limit 우선 대기 또는 None
    반환값: immutable OrderResult
    작성 날짜: 2026/08/22
    """
    return OrderResult(
        symbol="ETHUSDT",
        client_order_id="client-case-b-1-0",
        exchange_order_id=exchange_order_id,
        status=status,
        fills=fills,
        failure_reason=failure_reason,
        retry_after=retry_after,
        processed_at=TEST_TIME + timedelta(seconds=seconds_after),
    )


class OrderValueTests(unittest.TestCase):
    """
    클래스 이름: OrderValueTests
    기능: Fill, OrderResult와 ExecutionSummary의 immutable typed 계약을 검증한다.
    작성 날짜: 2026/08/22
    """

    def test_fill_result_and_summary_are_frozen_slotted_values(self) -> None:
        """
        함수 이름: test_fill_result_and_summary_are_frozen_slotted_values()
        기능: 세 immutable value를 변경할 수 없고 instance dictionary도 없는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        first_fill = make_fill("11", Decimal("0.5"), Decimal("100"))
        second_fill = make_fill(
            "12",
            Decimal("1"),
            Decimal("130"),
            seconds_after=2,
        )
        order_result = make_result(OrderStatus.FILLED, (first_fill, second_fill))
        order = make_order()
        order.apply_order_result(order_result)
        summary = order.build_execution_summary()

        # frozen DTO는 생성 후 금융 사실을 덮어쓸 수 없어야 한다.
        for frozen_value, field_name, replacement_value in (
            (first_fill, "price", Decimal("999")),
            (order_result, "status", OrderStatus.REJECTED),
            (summary, "executed_amount", Decimal("999")),
        ):
            with self.subTest(frozen_type=type(frozen_value).__name__):
                with self.assertRaises(FrozenInstanceError):
                    setattr(frozen_value, field_name, replacement_value)
                self.assertFalse(hasattr(frozen_value, "__dict__"))

        self.assertFalse(hasattr(order, "__dict__"))  # mutable aggregate도 임의 필드 추가는 막는다.

    def test_rejects_float_non_finite_non_utc_and_invalid_quantity_intent(self) -> None:
        """
        함수 이름: test_rejects_float_non_finite_non_utc_and_invalid_quantity_intent()
        기능: 금융 float·특수 Decimal·naive 시각과 제출량 초과를 fail closed하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # Fill과 Order 양쪽에서 float와 비정상 Decimal이 domain에 들어오지 못하게 한다.
        with self.assertRaises(TypeError):
            make_fill("11", 0.5, Decimal("100"))  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            make_fill("11", Decimal("NaN"), Decimal("100"))
        with self.assertRaises(ValueError):
            replace(
                make_fill("11", Decimal("0.5"), Decimal("100")),
                executed_at=datetime(2026, 8, 22, 1, 0),
            )
        with self.assertRaises(TypeError):
            make_order(requested_quantity=2.0)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            make_order(
                requested_quantity=Decimal("1"),
                submitted_quantity=Decimal("1.1"),
            )

    def test_retry_after_accepts_bounded_timedelta_only(self) -> None:
        """
        함수 이름: test_retry_after_accepts_bounded_timedelta_only()
        기능: ADR-002 Retry-After가 0초부터 30초까지의 timedelta만 허용하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # 경계값은 보존하고 음수·상한 초과·다른 타입은 모두 거부한다.
        self.assertEqual(
            timedelta(seconds=30),
            make_result(
                OrderStatus.UNKNOWN,
                exchange_order_id=None,
                retry_after=timedelta(seconds=30),
            ).retry_after,
        )
        for invalid_retry_after in (
            timedelta(microseconds=-1),
            timedelta(seconds=30, microseconds=1),
            3,
        ):
            with self.subTest(retry_after=invalid_retry_after):
                with self.assertRaises((TypeError, ValueError)):
                    make_result(
                        OrderStatus.UNKNOWN,
                        exchange_order_id=None,
                        retry_after=invalid_retry_after,  # type: ignore[arg-type]
                    )

    def test_normalized_statuses_cover_pending_and_expired_in_match(self) -> None:
        """
        함수 이름: test_normalized_statuses_cover_pending_and_expired_in_match()
        기능: 공식 pending 상태와 match 중 만료 상태가 active·terminal 집합에 정확히 속하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # cancel reconciliation이 문자열 fallback 없이 모든 normalized 상태를 분류해야 한다.
        self.assertIn(OrderStatus.PENDING_NEW, ACTIVE_ORDER_STATUSES)
        self.assertIn(OrderStatus.PENDING_CANCEL, ACTIVE_ORDER_STATUSES)
        self.assertIn(OrderStatus.EXPIRED_IN_MATCH, TERMINAL_ORDER_STATUSES)
        self.assertNotIn(OrderStatus.UNKNOWN, ACTIVE_ORDER_STATUSES)
        self.assertNotIn(OrderStatus.UNKNOWN, TERMINAL_ORDER_STATUSES)

    def test_fee_conversion_uses_each_fill_price_and_rejects_third_asset(self) -> None:
        """
        함수 이름: test_fee_conversion_uses_each_fill_price_and_rejects_third_asset()
        기능: ETH fee를 fill별 가격으로 합산하고 제3 asset을 typed reconciliation으로 닫는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        first_fill = make_fill(
            "11",
            Decimal("0.25"),
            Decimal("100"),
            fee_amount=Decimal("0.001"),
            fee_asset="ETH",
        )
        second_fill = make_fill(
            "12",
            Decimal("0.5"),
            Decimal("200"),
            fee_amount=Decimal("0.003"),
            fee_asset="ETH",
            seconds_after=1,
        )
        order = make_order(submitted_quantity=Decimal("0.75"))
        order.apply_order_result(
            make_result(OrderStatus.FILLED, (first_fill, second_fill))
        )

        # 평균 체결가로 재평가하지 않고 0.001*100 + 0.003*200을 그대로 합산한다.
        summary = order.build_execution_summary()
        self.assertEqual(Decimal("0.004"), summary.fee_amount)
        self.assertEqual(Decimal("0.700"), summary.fee_quote_amount)
        self.assertNotEqual(
            summary.fee_quote_amount,
            summary.fee_amount * summary.average_fill_price,
        )

        with self.assertRaises(FeeAssetReconciliationRequiredError) as context:
            make_fill(
                "13",
                Decimal("0.1"),
                Decimal("100"),
                fee_amount=Decimal("0.001"),
                fee_asset="BNB",
                fee_quote_amount=Decimal("0.1"),
            )
        self.assertEqual("BNB", context.exception.fee_asset)  # typed 오류가 원래 asset을 보존한다.


class OrderAggregateTests(unittest.TestCase):
    """
    클래스 이름: OrderAggregateTests
    기능: Order의 결과 적용, fill dedup, 상태 진행과 ExecutionSummary 생성을 검증한다.
    작성 날짜: 2026/08/22
    """

    def test_immediate_fill_preserves_intent_and_builds_weighted_summary(self) -> None:
        """
        함수 이름: test_immediate_fill_preserves_intent_and_builds_weighted_summary()
        기능: 즉시 체결에서 원래 intent와 실제 제출량 및 여러 fill aggregate를 보존하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        first_fill = make_fill(
            "11",
            Decimal("0.5"),
            Decimal("100"),
            fee_amount=Decimal("0.05"),
        )
        second_fill = make_fill(
            "12",
            Decimal("1"),
            Decimal("130"),
            fee_amount=Decimal("0.10"),
            seconds_after=2,
        )
        order = make_order()

        # 최초 FILLED 결과 하나로 Message 7과 10의 상태 및 집계를 완성한다.
        order.apply_order_result(
            make_result(OrderStatus.FILLED, (first_fill, second_fill))
        )
        summary = order.build_execution_summary()

        self.assertEqual("intent-case-b-1", order.intent_id)
        self.assertEqual("client-case-b-1-0", order.client_order_id)
        self.assertEqual(0, order.submission_attempt)
        self.assertEqual(Decimal("2"), order.requested_quantity)
        self.assertEqual(Decimal("1.5"), order.submitted_quantity)
        self.assertEqual(OrderStatus.FILLED, order.status)
        self.assertEqual(Decimal("1.5"), order.filled_quantity)
        self.assertEqual(Decimal("180.0"), order.filled_amount)
        self.assertEqual(Decimal("120"), order.fill_price)
        self.assertEqual(Decimal("0.15"), order.fee_quote_amount)
        self.assertEqual("1001", order.order_id)

        # Position과 Trade에 넘길 summary가 같은 Order identity와 마지막 fill 시각을 가진다.
        self.assertEqual("1001", summary.exchange_order_id)
        self.assertEqual("1001", summary.order_id)
        self.assertEqual("client-case-b-1-0", summary.client_order_id)
        self.assertEqual("ETHUSDT", summary.symbol)
        self.assertIs(OrderSide.BUY, summary.side)
        self.assertIs(StrategyType.CASE_B, summary.strategy)
        self.assertIs(RegimeType.TYPE_0, summary.regime_type)
        self.assertIsNone(summary.exit_reason)
        self.assertEqual(Decimal("2"), summary.requested_quantity)
        self.assertEqual(Decimal("1.5"), summary.executed_quantity)
        self.assertEqual(TEST_TIME + timedelta(seconds=2), summary.executed_at)

    def test_unknown_new_partial_filled_progress_and_stale_result_do_not_regress(self) -> None:
        """
        함수 이름: test_unknown_new_partial_filled_progress_and_stale_result_do_not_regress()
        기능: 같은 주문 조회가 UNKNOWN부터 FILLED로 전진하고 늦은 active 결과가 역행시키지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        first_fill = make_fill("11", Decimal("0.5"), Decimal("100"))
        second_fill = make_fill("12", Decimal("1"), Decimal("120"), seconds_after=3)
        order = make_order()

        # exchange ID가 없는 timeout 뒤에도 같은 client ID로 concrete 상태를 계속 조회한다.
        order.apply_order_result(
            make_result(OrderStatus.UNKNOWN, exchange_order_id=None)
        )
        order.reapply_order_result(make_result(OrderStatus.NEW, seconds_after=1))
        order.reapply_order_result(
            make_result(OrderStatus.PARTIALLY_FILLED, (first_fill,), seconds_after=2)
        )
        order.reapply_order_result(make_result(OrderStatus.NEW, seconds_after=1))
        self.assertEqual(OrderStatus.PARTIALLY_FILLED, order.status)

        # terminal 결과 뒤 재전달된 partial snapshot은 fill과 terminal 상태를 모두 중복 적용하지 않는다.
        order.reapply_order_result(
            make_result(
                OrderStatus.FILLED,
                (first_fill, second_fill),
                seconds_after=3,
            )
        )
        order.reapply_order_result(
            make_result(OrderStatus.PARTIALLY_FILLED, (first_fill,), seconds_after=2)
        )
        self.assertEqual(OrderStatus.FILLED, order.status)
        self.assertEqual((first_fill, second_fill), order.fills)
        self.assertEqual(Decimal("1.5"), order.filled_quantity)  # duplicate fill이 수량을 늘리지 않는다.

        with self.assertRaises(OrderStateTransitionError):
            order.reapply_order_result(
                make_result(
                    OrderStatus.CANCELED,
                    (first_fill, second_fill),
                    seconds_after=4,
                )
            )

    def test_duplicate_fill_is_idempotent_and_conflicting_content_is_atomic(self) -> None:
        """
        함수 이름: test_duplicate_fill_is_idempotent_and_conflicting_content_is_atomic()
        기능: 같은 fill 내용은 no-op이고 같은 key의 다른 내용은 Order를 부분 변경하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        original_fill = make_fill("11", Decimal("0.5"), Decimal("100"))
        order = make_order()
        order.apply_order_result(
            make_result(
                OrderStatus.PARTIALLY_FILLED,
                (original_fill, original_fill),
            )
        )
        self.assertEqual((original_fill,), order.fills)

        conflicting_fill = make_fill("11", Decimal("0.5"), Decimal("101"))
        state_before = (
            order.status,
            order.fills,
            order.filled_amount,
            order.processed_at,
        )

        # 충돌 결과 전체를 거부하고 기존 aggregate를 그대로 유지한다.
        with self.assertRaises(OrderResultConflictError):
            order.reapply_order_result(
                make_result(
                    OrderStatus.PARTIALLY_FILLED,
                    (conflicting_fill,),
                    seconds_after=2,
                )
            )
        self.assertEqual(
            state_before,
            (order.status, order.fills, order.filled_amount, order.processed_at),
        )

    def test_active_partial_subset_and_applied_fill_tracking_are_order_scoped(self) -> None:
        """
        함수 이름: test_active_partial_subset_and_applied_fill_tracking_are_order_scoped()
        기능: active partial의 새 fill subset만 요약·Position applied로 표시할 수 있는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        first_fill = make_fill("11", Decimal("0.5"), Decimal("100"))
        order = make_order()
        order.apply_order_result(
            make_result(OrderStatus.PARTIALLY_FILLED, (first_fill,))
        )

        with self.assertRaises(ExecutionSummaryUnavailableError):
            order.build_execution_summary()

        # Controller는 새 fill subset만 terminal 조건 없이 Message 12에 전달한다.
        partial_summary = order.build_execution_summary(
            fills=order.unapplied_fills,
            require_terminal=False,
        )
        self.assertEqual(Decimal("0.5"), partial_summary.executed_quantity)
        order.mark_fills_applied(partial_summary.fills)
        self.assertEqual((), order.unapplied_fills)

        foreign_fill = make_fill("99", Decimal("0.1"), Decimal("90"))
        for operation in (
            lambda: order.build_execution_summary(
                fills=(foreign_fill,),
                require_terminal=False,
            ),
            lambda: order.mark_fills_applied((foreign_fill,)),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(OrderResultConflictError):
                    operation()

    def test_terminal_partial_builds_summary_but_terminal_zero_fill_does_not(self) -> None:
        """
        함수 이름: test_terminal_partial_builds_summary_but_terminal_zero_fill_does_not()
        기능: canceled partial은 실제 체결로 확정하고 terminal zero-fill은 summary로 위장하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        partial_fill = make_fill("11", Decimal("0.5"), Decimal("100"))
        partial_order = make_order()
        partial_order.apply_order_result(
            make_result(OrderStatus.CANCELED, (partial_fill,))
        )
        zero_fill_order = make_order()
        zero_fill_order.apply_order_result(make_result(OrderStatus.REJECTED))

        self.assertEqual(
            Decimal("0.5"),
            partial_order.build_execution_summary().executed_quantity,
        )
        with self.assertRaises(ExecutionSummaryUnavailableError):
            zero_fill_order.build_execution_summary()

    def test_pending_cancel_progress_and_expired_in_match_terminal_partial(self) -> None:
        """
        함수 이름: test_pending_cancel_progress_and_expired_in_match_terminal_partial()
        기능: pending cancel이 stale active 상태로 역행하지 않고 match 중 만료 fill을 확정하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        partial_fill = make_fill("11", Decimal("0.5"), Decimal("100"))
        order = make_order()

        # PENDING_NEW부터 cancel 대기까지 같은 주문의 active 진행 상태를 전진시킨다.
        order.apply_order_result(make_result(OrderStatus.PENDING_NEW))
        order.reapply_order_result(make_result(OrderStatus.NEW, seconds_after=1))
        order.reapply_order_result(
            make_result(OrderStatus.PARTIALLY_FILLED, (partial_fill,), seconds_after=2)
        )
        order.reapply_order_result(
            make_result(OrderStatus.PENDING_CANCEL, (partial_fill,), seconds_after=3)
        )
        order.reapply_order_result(make_result(OrderStatus.NEW, seconds_after=1))
        self.assertEqual(OrderStatus.PENDING_CANCEL, order.status)

        # EXPIRED_IN_MATCH도 실제 partial fill을 버리지 않는 terminal 결과다.
        order.reapply_order_result(
            make_result(
                OrderStatus.EXPIRED_IN_MATCH,
                (partial_fill,),
                seconds_after=4,
            )
        )
        self.assertTrue(order.is_terminal)
        self.assertEqual(
            Decimal("0.5"),
            order.build_execution_summary().executed_quantity,
        )

    def test_mixed_fee_assets_and_order_identity_conflicts_fail_before_commit(self) -> None:
        """
        함수 이름: test_mixed_fee_assets_and_order_identity_conflicts_fail_before_commit()
        기능: 혼합 fee asset과 다른 client order 결과가 Order 상태를 변경하기 전에 거부되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        usdt_fill = make_fill("11", Decimal("0.5"), Decimal("100"))
        eth_fill = make_fill(
            "12",
            Decimal("1"),
            Decimal("120"),
            fee_amount=Decimal("0.001"),
            fee_asset="ETH",
        )
        order = make_order()

        with self.assertRaises(MixedFeeAssetError):
            order.apply_order_result(
                make_result(OrderStatus.FILLED, (usdt_fill, eth_fill))
            )
        self.assertIsNone(order.status)
        self.assertEqual((), order.fills)

        mismatched_result = replace(
            make_result(OrderStatus.NEW),
            client_order_id="another-client-id",
        )
        with self.assertRaises(OrderResultConflictError):
            order.apply_order_result(mismatched_result)
        self.assertIsNone(order.exchange_order_id)  # identity 충돌은 거래소 ID도 commit하지 않는다.

    def test_apply_and_reapply_order_are_explicit(self) -> None:
        """
        함수 이름: test_apply_and_reapply_order_are_explicit()
        기능: 최초 Message 7과 후속 Message 9의 호출 순서를 바꾸거나 중복 호출하지 못하게 한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        order = make_order()
        initial_result = make_result(OrderStatus.NEW)

        with self.assertRaises(OrderStateTransitionError):
            order.reapply_order_result(initial_result)
        order.apply_order_result(initial_result)
        with self.assertRaises(OrderStateTransitionError):
            order.apply_order_result(initial_result)


if __name__ == "__main__":
    unittest.main()
