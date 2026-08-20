"""Run-to-completion queue 순서와 context 갱신의 통합 동작을 검증한다."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from binance_auto_trader.trading.action_requests import (
    OpenLowerEvent,
    PatchRuntimeContext,
    QueueEvent,
    ResetCaseBContext,
    ResetCaseCContext,
    SubmitOrder,
)
from binance_auto_trader.trading.context import (
    MarketEvaluationSnapshot,
    PositionSnapshot,
    TradingContextView,
    TradingRuntimeSnapshot,
)
from binance_auto_trader.trading.event_queue import (
    ContextVersionError,
    ReentrantProcessingError,
    RunToCompletionEventProcessor,
    SerialEventQueue,
)
from binance_auto_trader.trading.events import (
    EventPriority,
    TradingEvent,
    TradingEventType,
)
from binance_auto_trader.trading.states import (
    CaseBSignalState,
    CaseCSignalState,
    OwnershipState,
    RootState,
    StrategyType,
    TradingStateConfiguration,
)
from binance_auto_trader.trading.stm import TradingSTM


TEST_EVALUATION_TIME = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)


def create_queued_event(
    event_type: TradingEventType,
    *,
    priority: EventPriority = EventPriority.MARKET,
    event_id: str | None = None,
    lower_event_id: str | None = None,
) -> TradingEvent:
    """
    함수 이름: create_queued_event()
    기능: queue 통합 테스트에 사용할 event를 생성한다.
    인자: event_type -> 생성할 TradingEventType
        priority -> queue에서 적용할 event 우선순위
        event_id -> 중복 제거와 추적에 사용할 선택 식별자
        lower_event_id -> event가 속한 lower-touch 범위의 선택 식별자
    반환값: queue에 삽입할 TradingEvent
    작성 날짜: 2026/08/14
    """
    return TradingEvent(
        event_type=event_type,
        occurred_at=TEST_EVALUATION_TIME,
        priority=priority,
        event_id=event_id,
        lower_event_id=lower_event_id,
    )


class MutableContextHarness:
    """
    클래스 이름: MutableContextHarness
    기능: typed context action을 적용하는 테스트용 TradingController 대역이다.
    작성 날짜: 2026/08/14
    """

    def __init__(self, context_view: TradingContextView) -> None:
        """
        함수 이름: __init__()
        기능: 최초 context와 주문 실행 기록을 초기화한다.
        인자: context_view -> 테스트에서 사용할 최초 TradingContextView
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        self.context_view = context_view
        self.orders: list[SubmitOrder] = []
        self.emit_order_outcome = False

    def snapshot(self) -> TradingContextView:
        """
        함수 이름: snapshot()
        기능: 현재 최신 불변 context snapshot을 반환한다.
        인자: 없음
        반환값: 현재 TradingContextView
        작성 날짜: 2026/08/14
        """
        return self.context_view

    def update_runtime(self, **changes: object) -> None:
        """
        함수 이름: update_runtime()
        기능: runtime 변경을 적용하고 single-writer context 버전을 증가시킨다.
        인자: changes -> TradingRuntimeSnapshot에 적용할 필드별 변경값
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        self.context_view = replace(
            self.context_view,
            version=self.context_view.version + 1,
            runtime=replace(self.context_view.runtime, **changes),
        )

    def execute(self, action: object) -> list[TradingEvent] | None:
        """
        함수 이름: execute()
        기능: context action을 적용하고 설정에 따라 Case C 매수 체결을 모의한다.
        인자: action -> 테스트 Controller가 실행할 action request
        반환값: 생성된 주문 결과 event 목록 또는 결과가 없을 때 None
        작성 날짜: 2026/08/14
        """
        # runtime patch는 명시된 필드만 바꾸고 context 버전을 증가시킨다.
        if isinstance(action, PatchRuntimeContext):
            changes = {change.field.value: change.value for change in action.changes}
            self.update_runtime(**changes)
            return None

        # 새 lower event 정보는 이후 stale event 판정에 사용할 범위로 저장한다.
        if isinstance(action, OpenLowerEvent):
            self.update_runtime(
                lower_event_id=action.lower_event_id,
                touch_time=action.touch_time,
                touch_candle_id=action.candle_id,
                touch_candle_low=action.touch_candle_low,
                lower_band_at_touch=action.lower_band_at_touch,
                touch_candle_bbw=action.touch_candle_bbw,
            )
            return None

        if isinstance(action, (ResetCaseBContext, ResetCaseCContext)):
            self.context_view = replace(self.context_view, version=self.context_view.version + 1)
            return None

        # 주문을 기록한 뒤 필요하면 정규화된 Case C 체결 event를 반환한다.
        if isinstance(action, SubmitOrder):
            self.orders.append(action)
            self.update_runtime(pending_order_id=f"order-{len(self.orders)}")
            if self.emit_order_outcome and action.strategy is StrategyType.CASE_C:
                self.context_view = replace(
                    self.context_view,
                    version=self.context_view.version + 1,
                    runtime=replace(
                        self.context_view.runtime,
                        position_owner=StrategyType.CASE_C,
                        pending_strategy=None,
                        pending_order_side=None,
                        pending_order_id=None,
                        pending_order_attempt_kind=None,
                        pending_intent_id=None,
                    ),
                    position=PositionSnapshot(
                        quantity=Decimal("1"),
                        entry_price=Decimal("90"),
                    ),
                )
                return [
                    create_queued_event(
                        TradingEventType.CASE_C_POSITION_OPENED,
                        priority=EventPriority.ORDER_OUTCOME,
                        lower_event_id=self.context_view.runtime.lower_event_id,
                    )
                ]

        return None


class SerialEventQueueTests(unittest.TestCase):
    """
    클래스 이름: SerialEventQueueTests
    기능: event queue의 우선순위, FIFO 및 중복 제거를 검증한다.
    작성 날짜: 2026/08/14
    """

    def test_internal_event_precedes_already_waiting_market_event(self) -> None:
        """
        함수 이름: test_internal_event_precedes_already_waiting_market_event()
        기능: 내부 event가 먼저 들어온 시장 event보다 우선하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        queue = SerialEventQueue()
        queue.enqueue(
            create_queued_event(
                TradingEventType.RETRY_C_WAIT_SETUP,
                event_id="market",
            )
        )
        queue.enqueue(
            create_queued_event(
                TradingEventType.CASE_C_POSITION_OPENED,
                event_id="internal",
            ),
            internal=True,
        )

        self.assertEqual(TradingEventType.CASE_C_POSITION_OPENED, queue.pop().event_type)
        self.assertEqual(TradingEventType.RETRY_C_WAIT_SETUP, queue.pop().event_type)

    def test_duplicate_event_id_is_suppressed(self) -> None:
        """
        함수 이름: test_duplicate_event_id_is_suppressed()
        기능: 같은 event ID가 두 번 삽입될 때 두 번째 입력을 제거하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        queue = SerialEventQueue()
        first_enqueued_event = queue.enqueue(
            create_queued_event(
                TradingEventType.RETRY_C_WAIT_SETUP,
                event_id="same",
            )
        )
        duplicate_enqueued_event = queue.enqueue(
            create_queued_event(
                TradingEventType.RETRY_C_WAIT_SETUP,
                event_id="same",
            )
        )

        self.assertIsNotNone(first_enqueued_event)
        self.assertIsNone(duplicate_enqueued_event)
        self.assertEqual(1, len(queue))


class RunToCompletionProcessorTests(unittest.IsolatedAsyncioTestCase):
    """
    클래스 이름: RunToCompletionProcessorTests
    기능: action batch 경계와 다중 Region event queue 동작을 검증한다.
    작성 날짜: 2026/08/14
    """

    async def test_case_c_order_reserves_slot_before_case_b_event(self) -> None:
        """
        함수 이름: test_case_c_order_reserves_slot_before_case_b_event()
        기능: Case C 주문 예약이 뒤따르는 Case B event보다 먼저 반영되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        stm = TradingSTM()
        stm._state = TradingStateConfiguration(
            root_state=RootState.TRADE_MANAGEMENT,
            ownership_state=OwnershipState.NO_POSITION,
            case_b_signal_state=CaseBSignalState.B_WAIT_PULLBACK,
            case_c_signal_state=CaseCSignalState.C_SETUP,
        )
        context_view = TradingContextView(
            version=1,
            evaluated_at=TEST_EVALUATION_TIME,
            market=MarketEvaluationSnapshot(
                realtime_price=Decimal("90"),
                realtime_pct_b=Decimal("-0.18"),
                signal_elapsed=timedelta(hours=1),
                case_c_timer_elapsed=timedelta(minutes=2),
            ),
            runtime=TradingRuntimeSnapshot(
                lower_event_id="lower-1",
                signal_created=True,
                allow_new_case_c_setup=True,
                flush_low=Decimal("89"),
                timer_base_pct_b=Decimal("-0.24"),
                timer_base_time=TEST_EVALUATION_TIME - timedelta(minutes=2),
                entry_pct_b=Decimal("-0.18"),
            ),
        )
        harness = MutableContextHarness(context_view)
        queue = SerialEventQueue()
        queue.enqueue(
            create_queued_event(
                TradingEventType.RETRY_CASE_C_SETUP_CONDITION_CHECK,
                lower_event_id="lower-1",
            ),
            internal=True,
        )
        queue.enqueue(
            create_queued_event(
                TradingEventType.RETRY_CASE_B_WAIT_PULLBACK_CONDITION_CHECK,
                lower_event_id="lower-1",
            ),
            internal=True,
        )
        processor = RunToCompletionEventProcessor(
            stm=stm,
            context_provider=harness.snapshot,
            action_executor=harness.execute,
            event_queue=queue,
            clock=lambda: TEST_EVALUATION_TIME,
        )

        case_c_result = await processor.process_next()
        case_b_result = await processor.process_next()

        self.assertEqual(("C-12",), case_c_result.transition_ids)
        self.assertFalse(case_b_result.consumed)
        self.assertEqual(1, len(harness.orders))
        self.assertEqual(StrategyType.CASE_C, harness.orders[0].strategy)

    async def test_g_02_internal_activation_uses_new_lower_event_scope(self) -> None:
        """
        함수 이름: test_g_02_internal_activation_uses_new_lower_event_scope()
        기능: G-02 후속 활성화 event가 새 lower event 범위를 사용하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        stm = TradingSTM()
        stm._state = TradingStateConfiguration(root_state=RootState.LOWER_TOUCH_WATCH)
        context_view = TradingContextView(
            version=1,
            evaluated_at=TEST_EVALUATION_TIME,
            market=MarketEvaluationSnapshot(
                realtime_price=Decimal("99"),
                lower_band=Decimal("100"),
                upper_band=Decimal("120"),
                current_30m_candle_id="30m-1",
                current_30m_low=Decimal("98"),
                touch_candle_bbw=Decimal("0.01"),
            ),
            runtime=TradingRuntimeSnapshot(),
        )
        harness = MutableContextHarness(context_view)
        queue = SerialEventQueue()
        queue.enqueue(create_queued_event(TradingEventType.LOWER_BAND_TOUCHED))
        processor = RunToCompletionEventProcessor(
            stm=stm,
            context_provider=harness.snapshot,
            action_executor=harness.execute,
            event_queue=queue,
            clock=lambda: TEST_EVALUATION_TIME,
        )

        lower_touch_result = await processor.process_next()
        activation_result = await processor.process_next()

        self.assertEqual("G-02", lower_touch_result.transition_ids[0])
        self.assertEqual(("C-02", "B-03"), activation_result.transition_ids)
        self.assertIsNotNone(harness.context_view.runtime.lower_event_id)

    async def test_order_outcome_microstep_precedes_waiting_market_event(self) -> None:
        """
        함수 이름: test_order_outcome_microstep_precedes_waiting_market_event()
        기능: 주문 결과 microstep이 대기 중인 시장 event보다 먼저 처리되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        stm = TradingSTM()
        stm._state = TradingStateConfiguration(
            root_state=RootState.TRADE_MANAGEMENT,
            ownership_state=OwnershipState.NO_POSITION,
            case_b_signal_state=CaseBSignalState.B_WAIT_PULLBACK,
            case_c_signal_state=CaseCSignalState.C_SETUP,
        )
        context_view = TradingContextView(
            version=1,
            evaluated_at=TEST_EVALUATION_TIME,
            market=MarketEvaluationSnapshot(
                realtime_price=Decimal("90"),
                realtime_pct_b=Decimal("-0.18"),
                signal_elapsed=timedelta(hours=1),
                case_c_timer_elapsed=timedelta(minutes=2),
            ),
            runtime=TradingRuntimeSnapshot(
                lower_event_id="lower-1",
                signal_created=True,
                allow_new_case_c_setup=True,
                flush_low=Decimal("89"),
                timer_base_pct_b=Decimal("-0.24"),
                timer_base_time=TEST_EVALUATION_TIME - timedelta(minutes=2),
                entry_pct_b=Decimal("-0.18"),
            ),
        )
        harness = MutableContextHarness(context_view)
        harness.emit_order_outcome = True
        queue = SerialEventQueue()
        queue.enqueue(
            create_queued_event(
                TradingEventType.RETRY_CASE_C_SETUP_CONDITION_CHECK,
                event_id="case-c-buy",
                lower_event_id="lower-1",
            )
        )
        queue.enqueue(
            create_queued_event(
                TradingEventType.RETRY_CASE_B_WAIT_PULLBACK_CONDITION_CHECK,
                event_id="market-after-buy",
                lower_event_id="lower-1",
            )
        )
        processor = RunToCompletionEventProcessor(
            stm=stm,
            context_provider=harness.snapshot,
            action_executor=harness.execute,
            event_queue=queue,
            clock=lambda: TEST_EVALUATION_TIME,
        )

        await processor.process_next()
        position_feedback_result = await processor.process_next()

        self.assertEqual(
            ("O-06", "PC-01", "C-16", "B-13"),
            position_feedback_result.transition_ids,
        )
        self.assertEqual(
            OwnershipState.CASE_C_POSITION_MANAGEMENT,
            stm.current_state.ownership_state,
        )

    async def test_action_cannot_recursively_process_queue(self) -> None:
        """
        함수 이름: test_action_cannot_recursively_process_queue()
        기능: action executor가 event queue를 재귀 처리하지 못하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        stm = TradingSTM()
        stm._state = TradingStateConfiguration(
            root_state=RootState.TRADE_MANAGEMENT,
            ownership_state=OwnershipState.NO_POSITION,
            case_b_signal_state=CaseBSignalState.B_WAIT_TOUCH,
            case_c_signal_state=CaseCSignalState.C_WAIT_SETUP,
        )
        context_view = TradingContextView(
            version=1,
            evaluated_at=TEST_EVALUATION_TIME,
            market=MarketEvaluationSnapshot(),
            runtime=TradingRuntimeSnapshot(
                lower_event_id="lower-1",
                touch_candle_low=Decimal("101"),
                lower_band_at_touch=Decimal("100"),
                touch_candle_bbw=Decimal("0.03"),
            ),
        )
        queue = SerialEventQueue()
        queue.enqueue(
            create_queued_event(TradingEventType.ACTIVATE_TRADE_MANAGEMENT)
        )
        processor: RunToCompletionEventProcessor

        async def recursive_executor(action: object) -> None:
            """
            함수 이름: recursive_executor()
            기능: 재진입 오류 검증을 위해 처리기를 의도적으로 다시 호출한다.
            인자: action -> 처리기가 전달한 action request
            반환값: 없음
            작성 날짜: 2026/08/14
            """
            if not isinstance(action, QueueEvent):
                await processor.process_next()

        processor = RunToCompletionEventProcessor(
            stm=stm,
            context_provider=lambda: context_view,
            action_executor=recursive_executor,
            event_queue=queue,
            clock=lambda: TEST_EVALUATION_TIME,
        )

        with self.assertRaises(ReentrantProcessingError):
            await processor.process_next()

    async def test_context_version_change_blocks_action_batch(self) -> None:
        """
        함수 이름: test_context_version_change_blocks_action_batch()
        기능: 판단 중 context 버전이 바뀌면 action batch가 실행되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        stm = TradingSTM()
        context_view = TradingContextView(
            version=1,
            evaluated_at=TEST_EVALUATION_TIME,
            market=MarketEvaluationSnapshot(),
            runtime=TradingRuntimeSnapshot(),
        )
        provider_calls = 0
        executed_actions: list[object] = []

        def changing_provider() -> TradingContextView:
            """
            함수 이름: changing_provider()
            기능: 두 번째 조회부터 증가한 context 버전을 반환한다.
            인자: 없음
            반환값: 호출 횟수에 따라 버전이 달라지는 TradingContextView
            작성 날짜: 2026/08/14
            """
            nonlocal provider_calls
            provider_calls += 1
            if provider_calls == 1:
                return context_view
            return replace(context_view, version=2)

        queue = SerialEventQueue()
        queue.enqueue(create_queued_event(TradingEventType.LOGIC_STARTED))
        processor = RunToCompletionEventProcessor(
            stm=stm,
            context_provider=changing_provider,
            action_executor=lambda action: executed_actions.append(action),
            event_queue=queue,
            clock=lambda: TEST_EVALUATION_TIME,
        )

        with self.assertRaises(ContextVersionError):
            await processor.process_next()
        self.assertEqual([], executed_actions)


if __name__ == "__main__":
    unittest.main()
