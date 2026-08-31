"""Mutable TradingContext의 typed mutation·version·snapshot 계약을 검증한다."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from decimal import Decimal

from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.trading.account import Account
from binance_auto_trader.domain.trading.action_requests import (
    CloseLowerEvent,
    ForceSellAll,
    OpenLowerEvent,
    PatchRuntimeContext,
    ResetCaseBContext,
    ResetCaseCContext,
    RuntimeField,
    RuntimeFieldChange,
    patch,
)
from binance_auto_trader.domain.trading.context import (
    ContextVersionConflictError,
    PendingOrderSnapshot,
    PositionSnapshot,
    TradingContext,
    TradingContextStateError,
)
from binance_auto_trader.domain.trading.results import TradingSTMResult
from binance_auto_trader.domain.trading.states import (
    OrderAttemptKind,
    OrderSide,
    StrategyType,
    TradingPhase,
    TradingStateConfiguration,
)


TEST_TIME = datetime(2026, 8, 21, 9, 0, tzinfo=UTC)


def create_context() -> TradingContext:
    """
    함수 이름: create_context()
    기능: 고정 시각과 기본 TYPE_0 세션을 갖는 테스트 Context를 생성한다.
    인자: 없음
    반환값: initialize를 완료한 TradingContext
    작성 날짜: 2026/08/21
    """
    context = TradingContext(clock=lambda: TEST_TIME)

    # 실제 API 호출 없이 Context 소유·version 계약만 검증할 Account를 연결한다.
    context.initialize(
        Account(),
        RegimeType.TYPE_0,
        PositionSnapshot(),
        Decimal("0.25"),
        Decimal("0.75"),
    )
    return context


def create_result(
    context_version: int,
    *actions: object,
) -> TradingSTMResult:
    """
    함수 이름: create_result()
    기능: runtime patch 적용 범위를 검증할 최소 TradingSTMResult를 만든다.
    인자: context_version -> 결정에 사용한 Context version
        actions -> 결과에 포함할 typed action request
    반환값: 테스트용 TradingSTMResult
    작성 날짜: 2026/08/21
    """
    # 동일 before·after state와 caller version으로 patch 적용에 필요한 최소 결과를 조립한다.
    state = TradingStateConfiguration()
    return TradingSTMResult(
        decision_id="context-test",
        consumed=True,
        transition_ids=("G-TEST",),
        state_before=state,
        state_after=state,
        action_requests=actions,
        context_version=context_version,
    )


class TradingContextInitializationTests(unittest.TestCase):
    """
    클래스 이름: TradingContextInitializationTests
    기능: session 초기화·설정 검증·snapshot 발행 계약을 검증한다.
    작성 날짜: 2026/08/21
    """

    def test_initialize_connects_inputs_and_resets_previous_runtime(self) -> None:
        """
        함수 이름: test_initialize_connects_inputs_and_resets_previous_runtime()
        기능: initialize가 입력을 연결하고 이전 runtime·pending 의도를 제거하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 이전 runtime·pending 의도를 가진 비초기화 Context와 새 session 입력을 준비한다.
        context = TradingContext(clock=lambda: TEST_TIME)
        context.apply_runtime_patch(
            patch(
                position_owner=StrategyType.CASE_B,
                trading_phase=TradingPhase.STOPPING,
            )
        )
        context.update_pending_order(
            PendingOrderSnapshot(
                order_id="old-order",
                strategy=StrategyType.CASE_B,
                side=OrderSide.BUY,
                attempt_kind=OrderAttemptKind.INITIAL,
            )
        )
        account = Account()
        position = PositionSnapshot(
            quantity=Decimal("0.4"),
            entry_price=Decimal("100"),
        )
        version_before = context.version

        # session 경계에서 전략 runtime은 초기화하되 authoritative 포지션은 입력을 유지한다.
        context.initialize(
            account,
            RegimeType.TYPE_0,
            position,
            Decimal("0.3"),
            Decimal("0.8"),
        )

        # 초기화 결과가 설정을 보존하고 이전 runtime 의도를 모두 지웠는지 확인한다.
        snapshot = context.snapshot()
        self.assertTrue(context.initialized)
        self.assertIs(account, context.account)
        self.assertIs(RegimeType.TYPE_0, context.selected_regime)
        self.assertEqual(Decimal("0.3"), context.scale_in_ratio)
        self.assertEqual(Decimal("0.8"), context.scale_out_ratio)
        self.assertEqual(position, snapshot.position)
        self.assertEqual(TradingPhase.IDLE, snapshot.runtime.trading_phase)
        self.assertIsNone(snapshot.runtime.position_owner)
        self.assertIsNone(snapshot.pending_order)
        self.assertEqual(version_before + 1, snapshot.version)  # initialize는 항상 session version을 만든다.
        self.assertEqual(TEST_TIME, snapshot.evaluated_at)

    def test_recovery_initialize_rejects_invalid_position_without_mutation(self) -> None:
        """
        함수 이름: test_recovery_initialize_rejects_invalid_position_without_mutation()
        기능: Communication 8R.1.1.1의 Context 초기화가 잘못된 복구 Position을 원자적으로 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        context = TradingContext(clock=lambda: TEST_TIME)

        # 복구 경로도 canonical PositionSnapshot 없이는 account나 session version을 게시하지 않는다.
        with self.assertRaisesRegex(TypeError, "position must be a PositionSnapshot"):
            context.initialize(
                Account(),
                RegimeType.TYPE_0,
                object(),  # type: ignore[arg-type]
                Decimal("0.25"),
                Decimal("0.75"),
            )

        self.assertFalse(context.initialized)
        self.assertEqual(context.version, 0)  # 실패한 복구 초기화는 session version을 만들지 않는다.

    def test_selection_and_ratios_increment_only_for_real_changes(self) -> None:
        """
        함수 이름: test_selection_and_ratios_increment_only_for_real_changes()
        기능: pre-start 선택·비율 mutation이 실제 변경에만 version을 증가시키는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        context = TradingContext(clock=lambda: TEST_TIME)

        # 선택과 비율 적용은 각각 하나의 원자적 command version을 생성한다.
        context.select_regime(RegimeType.TYPE_0)
        context.set_split_ratios(Decimal("0.4"), Decimal("0.6"))

        # 두 실제 변경이 만든 version을 확인한 뒤 같은 값을 다시 적용한다.
        self.assertEqual(2, context.version)
        context.select_regime(RegimeType.TYPE_0)
        context.set_split_ratios(Decimal("0.4"), Decimal("0.6"))
        self.assertEqual(2, context.version)  # 멱등 command는 추가 version을 만들지 않는다.

    def test_split_ratio_rejects_non_decimal_non_finite_and_range_errors(self) -> None:
        """
        함수 이름: test_split_ratio_rejects_non_decimal_non_finite_and_range_errors()
        기능: 분할 비율에 float·NaN·범위 밖 값을 저장하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        context = TradingContext(clock=lambda: TEST_TIME)

        # 모든 실패 입력은 Context 설정과 version을 변경하지 않아야 한다.
        with self.assertRaises(TypeError):
            context.set_split_ratios(0.5, Decimal("0.5"))  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            context.set_split_ratios(Decimal("NaN"), Decimal("0.5"))
        with self.assertRaises(ValueError):
            context.set_split_ratios(Decimal("-0.0"), Decimal("0.5"))
        with self.assertRaises(ValueError):
            context.set_split_ratios(Decimal("-0.1"), Decimal("0.5"))
        with self.assertRaises(ValueError):
            context.set_split_ratios(Decimal("0.5"), Decimal("1.1"))

        self.assertEqual(0, context.version)  # 검증 실패는 mutation이 아니다.


class TradingContextMutationTests(unittest.TestCase):
    """
    클래스 이름: TradingContextMutationTests
    기능: typed action 적용·version 충돌·pending 분할 비율 계약을 검증한다.
    작성 날짜: 2026/08/21
    """

    def test_apply_result_applies_only_runtime_patches_once(self) -> None:
        """
        함수 이름: test_apply_result_applies_only_runtime_patches_once()
        기능: STM 결과의 patch만 적용하고 lower·주문 action은 실행하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 순차 patch 사이에 실행 대상이 아닌 lower·주문 action을 넣은 결과를 준비한다.
        context = create_context()
        result = create_result(
            context.version,
            patch(trading_phase=TradingPhase.STOPPING),
            OpenLowerEvent(
                lower_event_id="must-not-open",
                touch_time=TEST_TIME,
                candle_id="30m-1",
                touch_candle_low=Decimal("90"),
                lower_band_at_touch=Decimal("91"),
                touch_candle_bbw=Decimal("0.01"),
            ),
            ForceSellAll(),
            patch(trading_phase=TradingPhase.RECONCILIATION_REQUIRED),
        )
        version_before = context.version

        # 두 patch의 순서는 유지하되 result 적용 전체는 하나의 Context mutation이다.
        context.apply_trading_stm_result(result)

        # 마지막 patch만 runtime에 남고 비patch action은 Context를 변경하지 않는지 확인한다.
        snapshot = context.snapshot()
        self.assertEqual(
            TradingPhase.RECONCILIATION_REQUIRED,
            snapshot.runtime.trading_phase,
        )
        self.assertIsNone(snapshot.runtime.lower_event_id)
        self.assertEqual(version_before + 1, snapshot.version)  # 외부 action은 Context version을 소비하지 않는다.

    def test_stale_result_is_rejected_without_partial_mutation(self) -> None:
        """
        함수 이름: test_stale_result_is_rejected_without_partial_mutation()
        기능: 결정 후 Context가 바뀌면 stale result 패치를 적용하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 결과 생성 뒤 Position mutation으로 Context version을 앞선 stale 상황을 만든다.
        context = create_context()
        result = create_result(
            context.version,
            patch(trading_phase=TradingPhase.STOPPING),
        )
        context.update_position(
            PositionSnapshot(
                quantity=Decimal("1"),
                entry_price=Decimal("100"),
            ),
            StrategyType.CASE_C,
        )
        version_before = context.version

        # version race를 감지한 후에는 어떤 result 필드도 적용하지 않는다.
        with self.assertRaises(ContextVersionConflictError):
            context.apply_trading_stm_result(result)

        # 거부 후 version과 원본 runtime이 모두 그대로인지 확인한다.
        self.assertEqual(version_before, context.version)
        self.assertEqual(TradingPhase.IDLE, context.runtime.trading_phase)  # 기존 runtime을 유지한다.

    def test_invalid_runtime_patch_is_atomic(self) -> None:
        """
        함수 이름: test_invalid_runtime_patch_is_atomic()
        기능: typed runtime 검증에 실패한 patch가 state와 version을 부분 변경하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 첫 필드부터 domain enum 타입이 잘못된 runtime patch를 준비한다.
        context = create_context()
        invalid_patch = PatchRuntimeContext(
            changes=(
                RuntimeFieldChange(
                    RuntimeField.TRADING_PHASE,
                    "STOPPING",
                ),
            )
        )
        version_before = context.version

        # enum 문자열을 TradingPhase로 추측 변환하지 않고 전체 patch를 거부한다.
        with self.assertRaises(TypeError):
            context.apply_runtime_patch(invalid_patch)

        # Patch 검증 실패가 version이나 runtime을 부분 적용하지 않았는지 확인한다.
        self.assertEqual(version_before, context.version)
        self.assertEqual(TradingPhase.IDLE, context.runtime.trading_phase)  # 원본 runtime은 변하지 않는다.

    def test_pending_side_selects_scale_in_and_scale_out_ratios(self) -> None:
        """
        함수 이름: test_pending_side_selects_scale_in_and_scale_out_ratios()
        기능: BUY·SELL pending side가 각각 scale-in·scale-out Decimal을 선택하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        context = create_context()

        # pending Entity 적용이 runtime side도 같은 version으로 동기화하는지 함께 검증한다.
        context.update_pending_order(
            PendingOrderSnapshot(
                order_id="buy-order",
                strategy=StrategyType.CASE_B,
                side=OrderSide.BUY,
                attempt_kind=OrderAttemptKind.INITIAL,
            )
        )
        self.assertEqual(Decimal("0.25"), context.get_split_ratio())

        # Pending Entity를 SELL retry로 교체하면 scale-out 비율을 선택한다.
        context.update_pending_order(
            PendingOrderSnapshot(
                order_id="sell-order",
                strategy=StrategyType.CASE_C,
                side=OrderSide.SELL,
                attempt_kind=OrderAttemptKind.RETRY,
            )
        )
        self.assertEqual(Decimal("0.75"), context.get_split_ratio())

        # Pending Entity 제거 뒤에는 어느 split 비율도 임의 선택하지 않는다.
        context.update_pending_order(None)

        with self.assertRaises(TradingContextStateError):
            context.get_split_ratio()  # pending side 없이 매수 또는 매도 비율을 추측하지 않는다.

    def test_terminal_failure_can_clear_order_while_preserving_intent(self) -> None:
        """
        함수 이름: test_terminal_failure_can_clear_order_while_preserving_intent()
        기능: terminal zero-fill 뒤 같은 intent 재시도에 필요한 ID만 선택 보존하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        context = create_context()
        context.apply_runtime_patch(
            patch(pending_intent_id="case-b-buy-intent")
        )
        context.update_pending_order(
            PendingOrderSnapshot(
                order_id="exchange-order-1",
                strategy=StrategyType.CASE_B,
                side=OrderSide.BUY,
                attempt_kind=OrderAttemptKind.INITIAL,
            )
        )

        # Terminal 결과는 활성 주문 필드를 지우되 재제출 correlation ID만 남긴다.
        context.update_pending_order(
            None,
            preserve_intent_id=True,
        )
        runtime = context.runtime
        self.assertIsNone(context.pending_order)
        self.assertIsNone(runtime.pending_order_id)
        self.assertIsNone(runtime.pending_order_side)
        self.assertEqual("case-b-buy-intent", runtime.pending_intent_id)

        # bool 대체값은 retry 경계의 의미를 모호하게 하므로 mutation 전에 거부한다.
        with self.assertRaises(TypeError):
            context.update_pending_order(
                None,
                preserve_intent_id=1,
            )

    def test_lower_and_case_reset_actions_are_typed_versioned_mutations(self) -> None:
        """
        함수 이름: test_lower_and_case_reset_actions_are_typed_versioned_mutations()
        기능: lower event·Case reset action이 허용된 runtime만 변경하고 version을 증가시키는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # Reset에서 제거할 Case B signal과 Case C setup runtime을 먼저 구성한다.
        context = create_context()
        context.apply_runtime_patch(
            patch(
                signal_created=True,
                signal_candle_id="signal-1",
                signal_time=TEST_TIME,
                case_c_enabled=True,
                allow_new_case_c_setup=True,
                flush_low=Decimal("90"),
            )
        )
        version_before = context.version

        # lower scope open·close와 Case별 reset을 서로 독립된 typed mutation으로 적용한다.
        context.open_lower_event(
            OpenLowerEvent(
                lower_event_id="lower-1",
                touch_time=TEST_TIME,
                candle_id="30m-1",
                touch_candle_low=Decimal("89"),
                lower_band_at_touch=Decimal("90"),
                touch_candle_bbw=Decimal("0.01"),
            )
        )
        context.reset_case_b_context(ResetCaseBContext())
        context.reset_case_c_context(ResetCaseCContext())
        context.close_lower_event(CloseLowerEvent(reason="TEST_COMPLETE"))

        # 네 typed action이 각 한 version을 만들고 대상 runtime을 정리했는지 확인한다.
        snapshot = context.snapshot()
        self.assertEqual(version_before + 4, snapshot.version)
        self.assertIsNone(snapshot.runtime.lower_event_id)
        self.assertFalse(snapshot.runtime.signal_created)
        self.assertFalse(snapshot.runtime.case_c_enabled)
        self.assertIsNone(snapshot.runtime.flush_low)  # 별도 typed reset이 Case C setup 값을 정리한다.


if __name__ == "__main__":
    unittest.main()
