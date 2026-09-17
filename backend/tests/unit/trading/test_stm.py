"""상태 전이, 우선순위 및 Region broadcast 동작을 검증한다."""

from __future__ import annotations

import re
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.trading.action_requests import (
    CancelPendingOrder,
    CancelScheduledEvaluation,
    CloseLowerEvent,
    ForceSellAll,
    QueueEvent,
    ReconcileOrder,
    StopTradingRuntime,
    SubmitOrder,
)
from binance_auto_trader.domain.trading.context import (
    MarketEvaluationSnapshot,
    PositionSnapshot,
    TradingContextView,
    TradingRuntimeSnapshot,
)
from binance_auto_trader.domain.trading.events import (
    EventPriority,
    ForceSellOutcomePayload,
    TradingEvent,
    TradingEventType,
)
from binance_auto_trader.domain.trading.states import (
    CaseBPositionState,
    CaseBSignalState,
    CaseCPositionState,
    CaseCSignalState,
    ExitReason,
    OrderAttemptKind,
    OrderSide,
    OwnershipState,
    PositionReturnState,
    RootState,
    StrategyType,
    TradingStateConfiguration,
)
from binance_auto_trader.domain.trading.stm import TradingSTM
from binance_auto_trader.domain.trading.transitions.catalog import (
    TRANSITION_IDS,
)


TEST_EVALUATION_TIME = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)


def create_test_context(
    *,
    version: int = 1,
    market: MarketEvaluationSnapshot | None = None,
    runtime: TradingRuntimeSnapshot | None = None,
    position: PositionSnapshot | None = None,
) -> TradingContextView:
    """
    함수 이름: create_test_context()
    기능: 상태 전이 테스트에 사용할 유효한 context snapshot을 생성한다.
    인자: version -> context의 single-writer 버전
        market -> 선택적으로 덮어쓸 시장 평가 snapshot
        runtime -> 선택적으로 덮어쓸 실행 context snapshot
        position -> 선택적으로 덮어쓸 포지션 snapshot
    반환값: 테스트 입력으로 사용할 TradingContextView
    작성 날짜: 2026/08/14
    """
    return TradingContextView(
        version=version,
        evaluated_at=TEST_EVALUATION_TIME,
        market=market or MarketEvaluationSnapshot(),
        runtime=runtime or TradingRuntimeSnapshot(),
        position=position or PositionSnapshot(),
    )


def create_test_event(
    event_type: TradingEventType,
    *,
    sequence: int = 1,
) -> TradingEvent:
    """
    함수 이름: create_test_event()
    기능: 재현 가능한 순번과 식별자를 가진 테스트 event를 생성한다.
    인자: event_type -> 생성할 TradingEventType
        sequence -> event의 queue 순번
    반환값: 테스트 입력으로 사용할 TradingEvent
    작성 날짜: 2026/08/14
    """
    return TradingEvent(
        event_type=event_type,
        occurred_at=TEST_EVALUATION_TIME,
        sequence_number=sequence,
        priority=EventPriority.INTERNAL,
        event_id=f"test-{sequence}-{event_type}",
    )


def create_market_observation(market: MarketEvaluationSnapshot) -> TradingEvent:
    """Controller가 처리 준비를 마친 시장 관측을 순수 STM 테스트에 제공한다."""
    return TradingEvent(
        event_type=TradingEventType.MARKET_DATA_UPDATED,
        occurred_at=TEST_EVALUATION_TIME,
        sequence_number=1,
        priority=EventPriority.MARKET,
        event_id="market:1:policy-test",
        candle_id=market.current_30m_candle_id,
        market_evaluation=market,
        market_version=1,
    )


class StateConfigurationTests(unittest.TestCase):
    """
    클래스 이름: StateConfigurationTests
    기능: 병렬 Region에서 허용할 수 없는 상태 조합과 전이 목록을 검증한다.
    작성 날짜: 2026/08/14
    """

    def test_non_composite_root_rejects_active_regions(self) -> None:
        """
        함수 이름: test_non_composite_root_rejects_active_regions()
        기능: 복합 상태가 아닌 root에 활성 Region을 지정하면 예외가 발생하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        with self.assertRaises(ValueError):
            TradingStateConfiguration(
                root_state=RootState.LOWER_TOUCH_WATCH,
                ownership_state=OwnershipState.NO_POSITION,
            )

    def test_trade_management_requires_both_signal_regions(self) -> None:
        """
        함수 이름: test_trade_management_requires_both_signal_regions()
        기능: 거래 관리 복합 상태가 두 신호 Region을 모두 요구하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        with self.assertRaises(ValueError):
            TradingStateConfiguration(
                root_state=RootState.TRADE_MANAGEMENT,
                ownership_state=OwnershipState.NO_POSITION,
            )

    def test_transition_catalog_has_all_109_unique_ids_in_source(self) -> None:
        """
        함수 이름: test_transition_catalog_has_all_109_unique_ids_in_source()
        기능: 109개 전이 ID가 중복 없이 등록되고 구현 코드에서 사용되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        self.assertEqual(109, len(TRANSITION_IDS))
        self.assertEqual(109, len(set(TRANSITION_IDS)))

        transition_directory = (
            Path(__file__).parents[3]
            / "src"
            / "binance_auto_trader"
            / "domain"
            / "trading"
            / "transitions"
        )
        transition_source = "\n".join(
            source_path.read_text(encoding="utf-8")
            for source_path in transition_directory.glob("*_transitions.py")
        )
        emitted_transition_ids = set(
            re.findall(
                r'"((?:G|O|PB|PC|B|C)-\d{2}[A-Z]?)"',
                transition_source,
            )
        )
        self.assertEqual(set(TRANSITION_IDS), emitted_transition_ids)

    def test_stm_decision_modules_have_no_external_effect_dependencies(self) -> None:
        """
        함수 이름: test_stm_decision_modules_have_no_external_effect_dependencies()
        기능: STM 판단 모듈이 외부 통신과 지연 실행 의존성을 갖지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        package_directory = (
            Path(__file__).parents[3]
            / "src"
            / "binance_auto_trader"
            / "domain"
            / "trading"
        )
        decision_files = [
            package_directory / "stm.py",
            *package_directory.glob("transitions/*.py"),
        ]
        forbidden_fragments = (
            "asyncio.sleep",
            "APIGateway",
            "WebSocketGateway",
            "TradeHistoryController",
            "requests.get",
            "requests.post",
            "aiohttp",
            "httpx",
        )
        for decision_file in decision_files:
            decision_source = decision_file.read_text(encoding="utf-8")
            for forbidden_fragment in forbidden_fragments:
                with self.subTest(
                    path=decision_file.name,
                    fragment=forbidden_fragment,
                ):
                    self.assertNotIn(forbidden_fragment, decision_source)


class GlobalTransitionTests(unittest.TestCase):
    """
    클래스 이름: GlobalTransitionTests
    기능: root 진입, 중지 우선순위 및 복합 상태 초기화를 검증한다.
    작성 날짜: 2026/08/14
    """

    def test_g_01_run_enters_lower_touch_watch(self) -> None:
        """
        함수 이름: test_g_01_run_enters_lower_touch_watch()
        기능: G-01이 최초 실행을 lower-touch 감시 상태로 전이시키는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        stm = TradingSTM(RegimeType.TYPE_0)
        result = stm.run(create_test_context())

        self.assertEqual(("G-01",), result.transition_ids)
        self.assertEqual(RootState.LOWER_TOUCH_WATCH, result.state_after.root_state)

    def test_recovery_stop_after_stopping_is_no_op_without_duplicate_force_sell(
        self,
    ) -> None:
        """
        함수 이름: test_recovery_stop_after_stopping_is_no_op_without_duplicate_force_sell()
        기능: Communication 8R.1.1.2의 복구 STOP 재전달이 전량매도를 중복 요청하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/28
        """
        stm = TradingSTM(RegimeType.TYPE_0)
        context = create_test_context(
            position=PositionSnapshot(
                quantity=Decimal("0.25"),
                entry_price=Decimal("100"),
            ),
        )

        # 복구 owner와 같은 fresh STM global STOP이 최초 G-06 전량매도 요청을 만든다.
        first_result = stm.handle(
            create_test_event(TradingEventType.STOP_CONFIRMED),
            context,
        )
        self.assertEqual(first_result.transition_ids, ("G-06",))
        self.assertTrue(any(
            isinstance(action, ForceSellAll)
            for action in first_result.action_requests
        ))  # 최초 복구 STOP만 실제 노출을 닫는 전량매도 action을 생성한다.

        # 이미 STOPPING인 STM에 같은 STOP이 다시 도착해도 두 번째 매도 action은 만들지 않는다.
        duplicate_result = stm.handle(
            create_test_event(TradingEventType.STOP_CONFIRMED, sequence=2),
            context,
        )
        self.assertFalse(duplicate_result.consumed)
        self.assertEqual(duplicate_result.transition_ids, ())
        self.assertFalse(any(
            isinstance(action, ForceSellAll)
            for action in duplicate_result.action_requests
        ))  # 재전달은 기존 STOPPING 상태와 단일 제출 identity를 그대로 보존한다.

    def test_g_02_enters_all_regions_without_recursive_activation(self) -> None:
        """
        함수 이름: test_g_02_enters_all_regions_without_recursive_activation()
        기능: G-02가 재귀 처리 없이 모든 병렬 Region을 초기화하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        stm = TradingSTM(RegimeType.TYPE_0)
        stm.run(create_test_context())
        market = MarketEvaluationSnapshot(
            realtime_price=Decimal("99"),
            lower_band=Decimal("100"),
            upper_band=Decimal("120"),
            current_30m_candle_id="30m-1",
            current_30m_low=Decimal("98"),
            touch_candle_bbw=Decimal("0.01"),
        )
        result = stm.handle(
            create_test_event(TradingEventType.LOWER_BAND_TOUCHED),
            create_test_context(version=2, market=market),
        )

        self.assertEqual("G-02", result.transition_ids[0])
        self.assertEqual(RootState.TRADE_MANAGEMENT, result.state_after.root_state)
        self.assertEqual(OwnershipState.NO_POSITION, result.state_after.ownership_state)
        self.assertTrue(any(isinstance(action, QueueEvent) for action in result.action_requests))

    def test_g_06p_pending_stop_wins_over_position_stop(self) -> None:
        """
        함수 이름: test_g_06p_pending_stop_wins_over_position_stop()
        기능: 미체결 주문이 있을 때 G-06P가 포지션 중지 전이보다 우선하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        # Pending BUY와 실제 포지션 노출이 동시에 있는 trade-management state를 준비한다.
        stm = TradingSTM(RegimeType.TYPE_0)
        stm._state = TradingStateConfiguration.create_trade_management_initial_state()
        runtime = TradingRuntimeSnapshot(
            position_owner=StrategyType.CASE_B,
            pending_strategy=StrategyType.CASE_B,
            pending_order_side=OrderSide.BUY,
            pending_order_id="order-1",
            pending_order_attempt_kind=OrderAttemptKind.INITIAL,
        )

        # STOP_CONFIRMED를 전달해 pending 주문 branch의 우선순위를 판정한다.
        result = stm.handle(
            create_test_event(TradingEventType.STOP_CONFIRMED),
            create_test_context(
                runtime=runtime,
                position=PositionSnapshot(
                    quantity=Decimal("1"),
                    entry_price=Decimal("100"),
                ),
            ),
        )

        # G-06P가 먼저 소비하고 lifecycle을 STOPPING에 두는지 확인한다.
        self.assertEqual(("G-06P",), result.transition_ids)
        self.assertEqual(RootState.STOPPING, result.state_after.root_state)

    def test_g_05_zero_quantity_ignores_stale_position_owner(self) -> None:
        """
        함수 이름: test_g_05_zero_quantity_ignores_stale_position_owner()
        기능: logical owner가 남아도 authoritative 수량이 0이면 매도 없이 종료하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # Stale logical owner만 남고 authoritative 수량은 0인 state를 준비한다.
        stm = TradingSTM(RegimeType.TYPE_0)
        stm._state = TradingStateConfiguration.create_trade_management_initial_state()
        runtime = TradingRuntimeSnapshot(position_owner=StrategyType.CASE_B)

        # Position.quantity 0을 owner보다 우선하는 D-05 경계를 검증한다.
        result = stm.handle(
            create_test_event(TradingEventType.STOP_CONFIRMED),
            create_test_context(runtime=runtime),
        )

        # 무노출 branch가 즉시 종료하며 ForceSellAll을 만들지 않는지 확인한다.
        self.assertEqual(("G-05",), result.transition_ids)
        self.assertFalse(
            any(
                isinstance(action, ForceSellAll)
                for action in result.action_requests
            )
        )  # 0 수량 SELL action은 하나도 생성하지 않는다.

    def test_g_06_open_position_without_owner_requests_force_sell(self) -> None:
        """
        함수 이름: test_g_06_open_position_without_owner_requests_force_sell()
        기능: logical owner가 없어도 authoritative 수량이 양수이면 전량 매도를 요청하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # Logical owner가 없는 trade-management state에 실제 양수 Position을 주입한다.
        stm = TradingSTM(RegimeType.TYPE_0)
        stm._state = TradingStateConfiguration.create_trade_management_initial_state()

        # reconciliation으로 owner를 복구하지 못해도 실제 자산 노출은 매도해야 한다.
        result = stm.handle(
            create_test_event(TradingEventType.STOP_CONFIRMED),
            create_test_context(
                position=PositionSnapshot(
                    quantity=Decimal("0.25"),
                    entry_price=Decimal("100"),
                ),
            ),
        )

        # 실제 노출 branch가 STOPPING과 ForceSellAll 요청을 만드는지 확인한다.
        self.assertEqual(("G-06",), result.transition_ids)
        self.assertTrue(
            any(
                isinstance(action, ForceSellAll)
                for action in result.action_requests
            )
        )  # 실제 양수 수량이 force-sell branch를 선택한다.


class UpperBandContinuationAndStopTests(unittest.TestCase):
    """
    클래스 이름: UpperBandContinuationAndStopTests
    기능: G-07 하단 감시 복귀와 기존 사용자 STOP 후속 전이를 검증한다.
    작성 날짜: 2026/08/21
    """

    def test_force_sell_outcome_payload_rejects_truthy_non_booleans(self) -> None:
        """
        함수 이름: test_force_sell_outcome_payload_rejects_truthy_non_booleans()
        기능: 강제 매도 종료 증거가 정수나 문자열 truthiness를 허용하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 증거 필드는 Python truthiness가 아니라 exact bool만 받아야 한다.
        invalid_flag_values = (1, 0, "true", None)
        for invalid_flag_value in invalid_flag_values:
            with self.subTest(invalid_flag_value=invalid_flag_value):
                with self.assertRaises(TypeError):
                    ForceSellOutcomePayload(
                        execution_applied=invalid_flag_value,  # type: ignore[arg-type]
                    )

    def test_g_07_does_not_trigger_below_upper_band(self) -> None:
        """
        함수 이름: test_g_07_does_not_trigger_below_upper_band()
        기능: 실시간 가격이 상단 BB 미만이면 G-07이 소비되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        stm = TradingSTM(RegimeType.TYPE_0)
        stm._state = TradingStateConfiguration.create_trade_management_initial_state()
        market = MarketEvaluationSnapshot(
            realtime_price=Decimal("119.99999999"),
            upper_band=Decimal("120"),
        )

        result = stm.handle(
            create_test_event(TradingEventType.UPPER_BAND_TOUCHED),
            create_test_context(market=market),
        )

        self.assertFalse(result.consumed)
        self.assertEqual((), result.transition_ids)
        self.assertEqual(RootState.TRADE_MANAGEMENT, result.state_after.root_state)

    def test_g_07_boundary_without_exposure_returns_to_lower_watch(self) -> None:
        """
        함수 이름: test_g_07_boundary_without_exposure_returns_to_lower_watch()
        기능: 상단과 같은 가격에서 세션 종료 없이 하단 감시로 복귀하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/17
        """
        stm = TradingSTM(RegimeType.TYPE_0)
        stm._state = TradingStateConfiguration.create_trade_management_initial_state()
        result = stm.handle(
            create_test_event(TradingEventType.UPPER_BAND_TOUCHED),
            create_test_context(
                market=MarketEvaluationSnapshot(realtime_price=Decimal("120"), upper_band=Decimal("120")),
                runtime=TradingRuntimeSnapshot(position_owner=StrategyType.CASE_B),
            ),
        )
        self.assertEqual(("G-07",), result.transition_ids)
        self.assertEqual(RootState.LOWER_TOUCH_WATCH, result.state_after.root_state)
        close_action = next(action for action in result.action_requests if isinstance(action, CloseLowerEvent))
        self.assertEqual("UPPER_BAND_RETURN_TO_LOWER_WATCH", close_action.reason)
        self.assertEqual(["lower-event"], [action.scope for action in result.action_requests
                                         if isinstance(action, CancelScheduledEvaluation)])
        self.assertFalse(any(isinstance(action, (StopTradingRuntime, ForceSellAll, CancelPendingOrder, ReconcileOrder, SubmitOrder))
                             for action in result.action_requests))

    def test_g_07_preserves_positions_orders_and_preparation_intents(self) -> None:
        """
        함수 이름: test_g_07_preserves_positions_orders_and_preparation_intents()
        기능: B/C 보유·미결 주문·제출 전 의도에서 상단 이벤트가 아무 액션도 만들지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/17
        """
        for strategy in (StrategyType.CASE_B, StrategyType.CASE_C):
            for quantity, order_id, intent_id in (("1", None, None), ("0", "order-g07", None),
                                                 ("1", "order-g07", None), ("0", None, "preparing-g07")):
                with self.subTest(strategy=strategy, quantity=quantity, order_id=order_id, intent_id=intent_id):
                    stm = TradingSTM(RegimeType.TYPE_0)
                    before = TradingStateConfiguration.create_trade_management_initial_state()
                    stm._state = before
                    context = create_test_context(
                        market=MarketEvaluationSnapshot(realtime_price=Decimal("121"), upper_band=Decimal("120")),
                        runtime=TradingRuntimeSnapshot(
                            position_owner=strategy if quantity == "1" else None,
                            pending_strategy=strategy if order_id or intent_id else None,
                            pending_order_side=OrderSide.BUY if order_id or intent_id else None,
                            pending_order_id=order_id, pending_intent_id=intent_id,
                            pending_order_attempt_kind=OrderAttemptKind.INITIAL if order_id or intent_id else None,
                        ),
                        position=PositionSnapshot(quantity=Decimal(quantity),
                                                  entry_price=Decimal("100") if quantity == "1" else None),
                    )
                    result = stm.handle(create_test_event(TradingEventType.UPPER_BAND_TOUCHED), context)
                    self.assertEqual(("G-07",), result.transition_ids)
                    self.assertEqual(before, result.state_after)
                    self.assertEqual((), result.action_requests)

    def test_user_stop_uses_force_sell_then_g_06f_completion(self) -> None:
        """
        함수 이름: test_user_stop_uses_force_sell_then_g_06f_completion()
        기능: 확정 포지션이 G-06 전량 매도와 G-06F 완료 경로로 종료되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # Pending 없이 양수 Position만 존재하는 upper-band 초과 state를 준비한다.
        stm = TradingSTM(RegimeType.TYPE_0)
        stm._state = TradingStateConfiguration.create_trade_management_initial_state()
        runtime = TradingRuntimeSnapshot()
        market = MarketEvaluationSnapshot(
            realtime_price=Decimal("121"),
            upper_band=Decimal("120"),
        )

        # G-06 안전 종료 요청으로 schedule 취소와 최초 전량매도 Action을 생성한다.
        safe_termination = stm.handle(
            create_test_event(TradingEventType.STOP_CONFIRMED),
            create_test_context(
                market=market,
                runtime=runtime,
                position=PositionSnapshot(
                    quantity=Decimal("1"),
                    entry_price=Decimal("100"),
                ),
            ),
        )

        # 최초 G-06 결과가 STOPPING과 non-retry ForceSellAll을 포함하는지 확인한다.
        self.assertEqual(("G-06",), safe_termination.transition_ids)
        self.assertEqual(RootState.STOPPING, safe_termination.state_after.root_state)
        self.assertTrue(
            any(
                isinstance(action, CancelScheduledEvaluation)
                for action in safe_termination.action_requests
            )
        )
        self.assertTrue(
            any(
                isinstance(action, ForceSellAll)
                and not action.retry
                for action in safe_termination.action_requests
            )
        )

        # 구체 체결·저장 증거가 없으면 인자 없는 성공을 추론하지 않는다.
        missing_completion = stm.handle(
            create_test_event(
                TradingEventType.FORCE_SELL_FINISHED,
                sequence=2,
            ),
            create_test_context(version=2),
        )
        self.assertFalse(missing_completion.transition_ids)
        self.assertEqual(
            RootState.STOPPING,
            missing_completion.state_after.root_state,
        )

        # Exact bool 성공 증거가 있는 terminal outcome만 G-06F 완료로 처리한다.
        completion_event = replace(
            create_test_event(TradingEventType.FORCE_SELL_FINISHED, sequence=3),
            payload=ForceSellOutcomePayload(),
        )
        completion = stm.handle(
            completion_event,
            create_test_context(version=2),
        )

        # 검증된 완료 outcome이 lifecycle을 완전 종료하는지 확인한다.
        self.assertEqual(("G-06F",), completion.transition_ids)
        self.assertEqual(RootState.LOGIC_TERMINATED, completion.state_after.root_state)

    def test_user_stop_force_sell_failure_uses_g_06r_retry_policy(self) -> None:
        """
        함수 이름: test_user_stop_force_sell_failure_uses_g_06r_retry_policy()
        기능: 사용자 중지 전량 매도의 terminal 미체결이 기존 G-06R 재시도를 사용하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # Pending 없이 양수 Position을 가진 upper-band 경계 state를 준비한다.
        stm = TradingSTM(RegimeType.TYPE_0)
        stm._state = TradingStateConfiguration.create_trade_management_initial_state()
        runtime = TradingRuntimeSnapshot()
        market = MarketEvaluationSnapshot(
            realtime_price=Decimal("120"),
            upper_band=Decimal("120"),
        )
        # 최초 G-06 전량매도 요청을 발생시켜 STOPPING state로 진입한다.
        stm.handle(
            create_test_event(TradingEventType.STOP_CONFIRMED),
            create_test_context(
                market=market,
                runtime=runtime,
                position=PositionSnapshot(
                    quantity=Decimal("1"),
                    entry_price=Decimal("100"),
                ),
            ),
        )

        # terminal 미체결 payload가 없으면 retry intent를 새로 만들지 않는다.
        missing_failure = stm.handle(
            create_test_event(
                TradingEventType.FORCE_SELL_FAILED,
                sequence=2,
            ),
            create_test_context(
                version=2,
                runtime=runtime,
                position=PositionSnapshot(
                    quantity=Decimal("1"),
                    entry_price=Decimal("100"),
                ),
            ),
        )
        self.assertFalse(missing_failure.transition_ids)
        self.assertFalse(missing_failure.action_requests)

        # Exact terminal-unfilled 증거가 있는 실패 outcome으로 G-06R을 실행한다.
        failure_event = replace(
            create_test_event(TradingEventType.FORCE_SELL_FAILED, sequence=3),
            payload=ForceSellOutcomePayload(
                execution_applied=False,
                history_persisted=False,
                terminal_unfilled=True,
            ),
        )
        failure = stm.handle(
            failure_event,
            create_test_context(
                version=2,
                runtime=runtime,
                position=PositionSnapshot(
                    quantity=Decimal("1"),
                    entry_price=Decimal("100"),
                ),
            ),
        )

        # Retry Action이 명시적으로 재시도 정책을 사용하도록 표시됐는지 확인한다.
        retry_action = next(
            action
            for action in failure.action_requests
            if isinstance(action, ForceSellAll)
        )
        self.assertEqual(("G-06R",), failure.transition_ids)
        self.assertTrue(retry_action.retry)
        self.assertTrue(retry_action.use_retry_policy)


class ParallelRegionTests(unittest.TestCase):
    """
    클래스 이름: ParallelRegionTests
    기능: 고정된 병렬 Region 평가 순서와 동일 event broadcast를 검증한다.
    작성 날짜: 2026/08/14
    """

    def test_activate_evaluates_case_c_before_case_b(self) -> None:
        """
        함수 이름: test_activate_evaluates_case_c_before_case_b()
        기능: 활성화 event에서 Case C 신호 Region을 Case B보다 먼저 평가하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        stm = TradingSTM(RegimeType.TYPE_0)
        stm._state = TradingStateConfiguration.create_trade_management_initial_state()
        runtime = TradingRuntimeSnapshot(
            lower_event_id="lower-1",
            touch_candle_id="30m-1",
            touch_candle_low=Decimal("99"),
            lower_band_at_touch=Decimal("100"),
            touch_candle_bbw=Decimal("0.01"),
            case_b_enabled=True,
            case_c_enabled=True,
            allow_new_case_c_setup=True,
        )
        market = MarketEvaluationSnapshot(
            current_30m_candle_id="30m-1",
            realtime_pct_b=Decimal("-0.20"),
            cci_30m_realtime=Decimal("-150"),
        )

        result = stm.handle(
            create_test_event(TradingEventType.ACTIVATE_TRADE_MANAGEMENT),
            create_test_context(market=market, runtime=runtime),
        )

        self.assertEqual(("C-03", "B-03"), result.transition_ids)
        self.assertEqual(CaseCSignalState.C_SETUP, result.state_after.case_c_signal_state)
        self.assertEqual(CaseBSignalState.B_WAIT_SIGNAL, result.state_after.case_b_signal_state)

    def test_position_open_feedback_is_broadcast_to_three_regions(self) -> None:
        """
        함수 이름: test_position_open_feedback_is_broadcast_to_three_regions()
        기능: 포지션 생성 결과가 관련된 세 Region 모두에 전달되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        stm = TradingSTM(RegimeType.TYPE_0)
        stm._state = TradingStateConfiguration(
            root_state=RootState.TRADE_MANAGEMENT,
            ownership_state=OwnershipState.NO_POSITION,
            case_b_signal_state=CaseBSignalState.B_WAIT_PULLBACK,
            case_c_signal_state=CaseCSignalState.C_POSITION_OPEN_SIGNALLED,
        )
        runtime = TradingRuntimeSnapshot(position_owner=StrategyType.CASE_C)

        result = stm.handle(
            create_test_event(TradingEventType.CASE_C_POSITION_OPENED),
            create_test_context(
                runtime=runtime,
                position=PositionSnapshot(
                    quantity=Decimal("1"),
                    entry_price=Decimal("100"),
                ),
            ),
        )

        self.assertEqual(("O-06", "PC-01", "C-16", "B-13"), result.transition_ids)
        self.assertEqual(
            OwnershipState.CASE_C_POSITION_MANAGEMENT,
            result.state_after.ownership_state,
        )
        self.assertEqual(
            CaseCPositionState.CASE_C_HOLDING,
            result.state_after.case_c_position_state,
        )

    def test_same_market_snapshot_selects_case_c_buy_over_case_b(self) -> None:
        """
        함수 이름: test_same_market_snapshot_selects_case_c_buy_over_case_b()
        기능: 동일 snapshot에서 두 매수가 가능하면 Case C만 선택하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        stm = TradingSTM(RegimeType.TYPE_0)
        stm._state = TradingStateConfiguration(
            root_state=RootState.TRADE_MANAGEMENT,
            ownership_state=OwnershipState.NO_POSITION,
            case_b_signal_state=CaseBSignalState.B_WAIT_PULLBACK,
            case_c_signal_state=CaseCSignalState.C_SETUP,
        )
        market = MarketEvaluationSnapshot(
            realtime_price=Decimal("90"),
            realtime_pct_b=Decimal("-0.18"),
            signal_elapsed=timedelta(hours=1),
            case_c_timer_elapsed=timedelta(minutes=2),
        )
        runtime = TradingRuntimeSnapshot(
            lower_event_id="lower-1",
            signal_created=True,
            allow_new_case_c_setup=True,
            flush_low=Decimal("89"),
            timer_base_pct_b=Decimal("-0.24"),
            timer_base_time=TEST_EVALUATION_TIME - timedelta(minutes=2),
            entry_pct_b=Decimal("-0.18"),
        )

        result = stm.handle(
            create_test_event(TradingEventType.MARKET_DATA_UPDATED),
            create_test_context(market=market, runtime=runtime),
        )

        self.assertEqual(("C-12",), result.transition_ids)
        self.assertEqual(
            CaseBSignalState.B_WAIT_PULLBACK,
            result.state_after.case_b_signal_state,
        )
        submit_orders = [
            action for action in result.action_requests if isinstance(action, SubmitOrder)
        ]
        self.assertEqual(1, len(submit_orders))
        self.assertEqual(StrategyType.CASE_C, submit_orders[0].strategy)


class PriorityAndBoundaryTests(unittest.TestCase):
    """
    클래스 이름: PriorityAndBoundaryTests
    기능: Event-Action Table의 guard 우선순위와 정확한 시간 경계를 검증한다.
    작성 날짜: 2026/08/14
    """

    def test_case_c_unavailable_values_do_not_confirm_recovery_or_trailing_exit(self) -> None:
        """
        함수 이름: test_case_c_unavailable_values_do_not_confirm_recovery_or_trailing_exit()
        기능: 판정 불가 값을 회복 성공이나 EMA 비증가로 간주하지 않는지 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        from binance_auto_trader.domain.trading.transitions.case_c_position_transitions import _select_trailing_event

        stm = TradingSTM(RegimeType.TYPE_0)
        stm._state = replace(
            TradingStateConfiguration.create_trade_management_initial_state(),
            ownership_state=OwnershipState.CASE_C_POSITION_MANAGEMENT,
            case_c_position_state=CaseCPositionState.CASE_C_CLOSED,
        )
        context = create_test_context(
            market=MarketEvaluationSnapshot(realtime_pct_b=Decimal("NaN")),
            runtime=TradingRuntimeSnapshot(case_c_consumed_for_event=True, case_b_entry_paused=True),
        )
        result = stm.handle(create_test_event(TradingEventType.CHECK_CASE_C_RECOVERY), context)
        self.assertNotIn("PC-26", result.transition_ids)
        self.assertIs(result.state_after.case_c_position_state, CaseCPositionState.CASE_C_CLOSED)
        trailing = replace(context, market=MarketEvaluationSnapshot(
            realtime_pct_b=Decimal("0.2"), confirmed_1m_close=True,
            current_close_ema_slope=Decimal("NaN"),
        ), runtime=replace(context.runtime, previous_trail_ema_slope=Decimal("0.1")))
        self.assertIsNone(_select_trailing_event(trailing))
        expired = replace(trailing, market=replace(trailing.market, holding_elapsed=timedelta(hours=1)))
        self.assertIs(_select_trailing_event(expired), TradingEventType.CASE_C_TIME_EXIT)

    def test_pb_03_emergency_stop_has_priority_over_all_other_exits(self) -> None:
        """
        함수 이름: test_pb_03_emergency_stop_has_priority_over_all_other_exits()
        기능: PB-03 비상 중지가 다른 Case B 종료 조건보다 우선하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        stm = TradingSTM(RegimeType.TYPE_0)
        stm._state = TradingStateConfiguration(
            root_state=RootState.TRADE_MANAGEMENT,
            ownership_state=OwnershipState.CASE_B_POSITION_MANAGEMENT,
            case_b_signal_state=CaseBSignalState.CASE_B_FINAL_STATE,
            case_c_signal_state=CaseCSignalState.CASE_C_FINAL_STATE,
            case_b_position_state=CaseBPositionState.CASE_B_HOLDING,
        )
        market = MarketEvaluationSnapshot(
            realtime_price=Decimal("98"),
            confirmed_30m_close=True,
            ema_slope_30m_close=Decimal("-0.10"),
            realtime_ema_slope=Decimal("0.09"),
            holding_elapsed=timedelta(hours=6),
            pct_b_at_least_060_for_5s=True,
            realtime_slope_above_008_for_5s=True,
        )
        runtime = TradingRuntimeSnapshot(position_owner=StrategyType.CASE_B)

        result = stm.handle(
            create_test_event(TradingEventType.START_CASE_B_CONDITION_CHECK),
            create_test_context(
                market=market,
                runtime=runtime,
                position=PositionSnapshot(
                    quantity=Decimal("1"),
                    entry_price=Decimal("100"),
                ),
            ),
        )

        self.assertEqual(("PB-03",), result.transition_ids)
        queued_action = next(
            action
            for action in result.action_requests
            if isinstance(action, QueueEvent)
        )
        self.assertEqual(
            TradingEventType.CASE_B_EMERGENCY_STOP,
            queued_action.event_type,
        )

    def test_pc_03_profit_zone_has_priority_over_stop_and_time(self) -> None:
        """
        함수 이름: test_pc_03_profit_zone_has_priority_over_stop_and_time()
        기능: PC-03 수익 구간 진입이 중지 및 시간 종료보다 우선하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        stm = TradingSTM(RegimeType.TYPE_0)
        stm._state = TradingStateConfiguration(
            root_state=RootState.TRADE_MANAGEMENT,
            ownership_state=OwnershipState.CASE_C_POSITION_MANAGEMENT,
            case_b_signal_state=CaseBSignalState.CASE_B_FINAL_STATE,
            case_c_signal_state=CaseCSignalState.CASE_C_FINAL_STATE,
            case_c_position_state=CaseCPositionState.CASE_C_HOLDING,
        )
        market = MarketEvaluationSnapshot(
            realtime_pct_b=Decimal("0.10"),
            holding_elapsed=timedelta(minutes=60),
            realtime_slope_at_most_minus_055_for_3m=True,
        )
        runtime = TradingRuntimeSnapshot(position_owner=StrategyType.CASE_C)

        result = stm.handle(
            create_test_event(TradingEventType.START_CASE_C_CONDITION_CHECK),
            create_test_context(market=market, runtime=runtime),
        )

        self.assertEqual(("PC-03",), result.transition_ids)
        queued_action = next(
            action
            for action in result.action_requests
            if isinstance(action, QueueEvent)
        )
        self.assertEqual(
            TradingEventType.CASE_C_ENTER_PROFIT_ZONE,
            queued_action.event_type,
        )

    def test_b_06_allows_exact_three_hour_boundary(self) -> None:
        """
        함수 이름: test_b_06_allows_exact_three_hour_boundary()
        기능: B-06 매수 조건이 정확히 3시간인 경곗값을 포함하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        stm = TradingSTM(RegimeType.TYPE_0)
        stm._state = TradingStateConfiguration(
            root_state=RootState.TRADE_MANAGEMENT,
            ownership_state=OwnershipState.NO_POSITION,
            case_b_signal_state=CaseBSignalState.B_WAIT_PULLBACK,
            case_c_signal_state=CaseCSignalState.CASE_C_FINAL_STATE,
        )
        market = MarketEvaluationSnapshot(
            signal_elapsed=timedelta(hours=3),
            realtime_pct_b=Decimal("0.30"),
        )
        runtime = TradingRuntimeSnapshot(signal_created=True)

        result = stm.handle(
            create_test_event(
                TradingEventType.START_CASE_B_WAIT_PULLBACK_CONDITION_CHECK
            ),
            create_test_context(market=market, runtime=runtime),
        )

        self.assertEqual(("B-06",), result.transition_ids)
        self.assertTrue(any(isinstance(action, SubmitOrder) for action in result.action_requests))

    def test_c_10_lower_flush_wins_before_three_minute_restart(self) -> None:
        """
        함수 이름: test_c_10_lower_flush_wins_before_three_minute_restart()
        기능: C-10 lower flush가 3분 timer 재시작보다 먼저 선택되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        stm = TradingSTM(RegimeType.TYPE_0)
        stm._state = TradingStateConfiguration(
            root_state=RootState.TRADE_MANAGEMENT,
            ownership_state=OwnershipState.NO_POSITION,
            case_b_signal_state=CaseBSignalState.CASE_B_FINAL_STATE,
            case_c_signal_state=CaseCSignalState.C_SETUP,
        )
        market = MarketEvaluationSnapshot(
            realtime_price=Decimal("89"),
            realtime_pct_b=Decimal("-0.30"),
            case_c_timer_elapsed=timedelta(minutes=4),
        )
        runtime = TradingRuntimeSnapshot(
            allow_new_case_c_setup=True,
            flush_low=Decimal("90"),
            timer_base_pct_b=Decimal("-0.25"),
            timer_base_time=TEST_EVALUATION_TIME - timedelta(minutes=4),
            entry_pct_b=Decimal("-0.19"),
        )

        result = stm.handle(
            create_test_event(TradingEventType.RETRY_CASE_C_SETUP_CONDITION_CHECK),
            create_test_context(market=market, runtime=runtime),
        )

        self.assertEqual(("C-10",), result.transition_ids)

    def test_sell_filled_is_required_before_case_b_closed(self) -> None:
        """
        함수 이름: test_sell_filled_is_required_before_case_b_closed()
        기능: Case B가 매도 체결 결과를 받은 뒤에만 CLOSED로 전이하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        stm = TradingSTM(RegimeType.TYPE_0)
        stm._state = TradingStateConfiguration(
            root_state=RootState.TRADE_MANAGEMENT,
            ownership_state=OwnershipState.CASE_B_POSITION_MANAGEMENT,
            case_b_signal_state=CaseBSignalState.CASE_B_FINAL_STATE,
            case_c_signal_state=CaseCSignalState.CASE_C_FINAL_STATE,
            case_b_position_state=CaseBPositionState.CASE_B_HOLDING,
        )
        pending_runtime = TradingRuntimeSnapshot(
            position_owner=StrategyType.CASE_B,
            pending_exit_reason=ExitReason.STOP,
            pending_return_state=PositionReturnState.CASE_B_HOLDING,
        )
        no_op_result = stm.handle(
            create_test_event(TradingEventType.RETRY_CASE_B_CONDITION_CHECK),
            create_test_context(runtime=pending_runtime),
        )
        self.assertFalse(no_op_result.consumed)
        self.assertEqual(
            CaseBPositionState.CASE_B_HOLDING,
            stm.current_state.case_b_position_state,
        )

        filled_runtime = replace(
            pending_runtime,
            position_owner=None,
            case_b_exit_reason=ExitReason.STOP,
        )
        result = stm.handle(
            create_test_event(TradingEventType.CASE_B_SELL_FILLED, sequence=2),
            create_test_context(version=2, runtime=filled_runtime),
        )
        self.assertEqual(("PB-23F",), result.transition_ids)
        self.assertEqual(CaseBPositionState.CASE_B_CLOSED, stm.current_state.case_b_position_state)


if __name__ == "__main__":
    unittest.main()
