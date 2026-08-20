"""RegimeController의 STM microstep, 실패 격리와 직렬 실행 계약을 검증한다."""

import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from threading import Event, Lock
from unittest.mock import Mock, patch

import binance_auto_trader.application.regime_controller as regime_module

from binance_auto_trader.application import (
    RegimeController,
    RegimeEvaluationError,
    RegimeEvaluationFailureCode,
)
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.market import (
    IndicatorSnapshot,
    Interval,
    Kline,
    MarketSnapshot,
    SwingStructure,
)
from binance_auto_trader.domain.regime import (
    RegimeEvaluationContext,
    RegimeEvaluationTrigger,
    RegimeEvent,
    RegimeEventType,
    RegimeResult,
    RegimeSTM,
    RegimeSTMResult,
    RegimeState,
    SPECIFICATION_TRANSITION_IDS,
    StartRegimeEvaluation,
)
from tests.unit.market.test_indicator_snapshot import (
    LATEST_CLOSED_CANDLE_ID,
    SNAPSHOT_UPDATED_AT,
    load_golden_vector,
    make_kline,
    make_market_snapshot,
)


SUCCESS_COMMUNICATION_STEPS = (
    "1.4:MarketDataController->RegimeController.calculate_4h_indicators",
    "1.4.1:RegimeController->IndicatorSnapshot.update",
    "1.5:MarketDataController->RegimeController.recommend_regime",
    "1.5.1:RegimeController->RegimeSTM.handle:trigger",
    "1.5.1:RegimeController->RegimeSTM.handle:EVALUATION_READY",
)
FOUR_HOURS = timedelta(hours=4)
RECOMMENDATION_CASES = (
    (
        "EA-002",
        RegimeType.TYPE_0,
        Decimal("0"),
        False,
        False,
        False,
        False,
        Decimal("110"),
    ),
    (
        "EA-003",
        RegimeType.TYPE_1,
        Decimal("0.20"),
        False,
        False,
        False,
        False,
        Decimal("110"),
    ),
    (
        "EA-004",
        RegimeType.TYPE_1,
        Decimal("0.30"),
        False,
        False,
        False,
        False,
        Decimal("109"),
    ),
    (
        "EA-005",
        RegimeType.TYPE_2,
        Decimal("0.30"),
        True,
        True,
        False,
        False,
        Decimal("109"),
    ),
    (
        "EA-006",
        RegimeType.TYPE_3,
        Decimal("-0.20"),
        False,
        False,
        False,
        False,
        Decimal("110"),
    ),
    (
        "EA-007",
        RegimeType.TYPE_3,
        Decimal("-0.30"),
        False,
        False,
        False,
        False,
        Decimal("111"),
    ),
    (
        "EA-008",
        RegimeType.TYPE_4,
        Decimal("-0.30"),
        False,
        False,
        True,
        True,
        Decimal("111"),
    ),
)
START_TRANSITION_CASES = (
    (RegimeState.TYPE_0_RECOMMENDED, "EA-101"),
    (RegimeState.TYPE_1_RECOMMENDED, "EA-102"),
    (RegimeState.TYPE_2_RECOMMENDED, "EA-103"),
    (RegimeState.TYPE_3_RECOMMENDED, "EA-104"),
    (RegimeState.TYPE_4_RECOMMENDED, "EA-105"),
)


class RecordingRegimeSTM(RegimeSTM):
    """
    클래스 이름: RecordingRegimeSTM
    기능: Controller가 RegimeSTM에 전달한 event와 context를 호출 순서대로 기록한다.
    작성 날짜: 2026/08/20
    """

    def __init__(
        self,
        initial_state: RegimeState = RegimeState.INITIAL,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 지정한 초기 상태와 빈 호출 기록을 가진 RegimeSTM test spy를 만든다.
        인자: initial_state -> spy가 시작할 RegimeState
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        super().__init__(initial_state=initial_state)
        self.calls: list[
            tuple[RegimeEvent, RegimeEvaluationContext | None]
        ] = []

    def handle(
        self,
        event: RegimeEvent,
        context: RegimeEvaluationContext | None = None,
    ) -> RegimeSTMResult:
        """
        함수 이름: handle()
        기능: 호출을 기록한 뒤 production RegimeSTM의 순수 결정을 그대로 반환한다.
        인자: event -> Controller가 전달한 RegimeEvent
            context -> 선택적으로 전달한 평가 Context
        반환값: production RegimeSTMResult
        작성 날짜: 2026/08/20
        """
        self.calls.append((event, context))
        return super().handle(event, context)


class MismatchedStartActionRegimeSTM(RecordingRegimeSTM):
    """
    클래스 이름: MismatchedStartActionRegimeSTM
    기능: 첫 microstep Action의 evaluation ID만 변조해 provenance fault를 만든다.
    작성 날짜: 2026/08/20
    """

    def handle(
        self,
        event: RegimeEvent,
        context: RegimeEvaluationContext | None = None,
    ) -> RegimeSTMResult:
        """
        함수 이름: handle()
        기능: 최초 평가 시작 Action의 evaluation ID를 불일치 값으로 바꿔 반환한다.
        인자: event -> Controller가 전달한 RegimeEvent
            context -> 선택적으로 전달한 평가 Context
        반환값: 첫 Action provenance가 변조된 RegimeSTMResult
        작성 날짜: 2026/08/20
        """
        result = super().handle(event, context)
        if event.event_type is not RegimeEventType.INITIAL_EVALUATION_REQUESTED:
            return result

        action_request = result.action_requests[0]
        if not isinstance(action_request, StartRegimeEvaluation):
            raise AssertionError("initial transition must request evaluation")

        mismatched_action = replace(
            action_request,
            evaluation_id=f"{action_request.evaluation_id}:mismatch",
        )
        return replace(
            result,
            action_requests=(mismatched_action,),
        )


class ContractFaultRegimeSTM(RecordingRegimeSTM):
    """
    클래스 이름: ContractFaultRegimeSTM
    기능: 시작 Action trigger 또는 STM result evaluation ID를 선택적으로 변조한다.
    작성 날짜: 2026/08/20
    """

    def __init__(self, fault_kind: str) -> None:
        """
        함수 이름: __init__()
        기능: 검증할 Action contract fault 종류를 고정한 test spy를 만든다.
        인자: fault_kind -> start_trigger 또는 result_evaluation_id
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        if fault_kind not in {"start_trigger", "result_evaluation_id"}:
            raise ValueError("fault_kind is not supported")

        super().__init__()
        self._fault_kind = fault_kind

    def handle(
        self,
        event: RegimeEvent,
        context: RegimeEvaluationContext | None = None,
    ) -> RegimeSTMResult:
        """
        함수 이름: handle()
        기능: 실제 시작 transition 결과에서 지정한 provenance 필드 하나를 변조한다.
        인자: event -> Controller가 전달한 RegimeEvent
            context -> 선택적으로 전달한 평가 Context
        반환값: 지정된 contract fault를 가진 RegimeSTMResult
        작성 날짜: 2026/08/20
        """
        result = super().handle(event, context)
        if event.event_type is not RegimeEventType.INITIAL_EVALUATION_REQUESTED:
            return result

        if self._fault_kind == "result_evaluation_id":
            return replace(
                result,
                evaluation_id=f"{result.evaluation_id}:mismatch",
            )

        action_request = result.action_requests[0]
        if not isinstance(action_request, StartRegimeEvaluation):
            raise AssertionError("initial transition must request evaluation")

        mismatched_action = replace(
            action_request,
            trigger=RegimeEvaluationTrigger.FOUR_HOUR_CANDLE_CLOSE,
        )
        return replace(
            result,
            action_requests=(mismatched_action,),
        )


class BlockingRegimeSTM(RecordingRegimeSTM):
    """
    클래스 이름: BlockingRegimeSTM
    기능: 첫 STM 호출을 제어해 동시 Controller 평가가 직렬화되는지 관찰한다.
    작성 날짜: 2026/08/20
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 첫 호출 진입과 해제를 제어하는 동기화 primitive와 counter를 만든다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        super().__init__()
        self.first_handle_entered = Event()
        self.release_first_handle = Event()
        self.handle_attempts = 0
        self.maximum_active_handles = 0
        self._active_handles = 0
        self._counter_lock = Lock()

    def handle(
        self,
        event: RegimeEvent,
        context: RegimeEvaluationContext | None = None,
    ) -> RegimeSTMResult:
        """
        함수 이름: handle()
        기능: 첫 호출을 test event까지 대기시키고 동시 활성 handle 최대값을 기록한다.
        인자: event -> Controller가 전달한 RegimeEvent
            context -> 선택적으로 전달한 평가 Context
        반환값: production RegimeSTMResult
        작성 날짜: 2026/08/20
        """
        with self._counter_lock:
            self.handle_attempts += 1
            attempt_number = self.handle_attempts
            self._active_handles += 1
            self.maximum_active_handles = max(
                self.maximum_active_handles,
                self._active_handles,
            )

        try:
            if attempt_number == 1:
                self.first_handle_entered.set()
                if not self.release_first_handle.wait(timeout=5):
                    raise TimeoutError("first RegimeSTM handle was not released")

            return super().handle(event, context)
        finally:
            with self._counter_lock:
                self._active_handles -= 1


class PausingFailureRegimeController(RegimeController):
    """
    클래스 이름: PausingFailureRegimeController
    기능: 안정된 계산 실패 확정과 failure trace 기록 사이의 snapshot 갱신을 제어한다.
    작성 날짜: 2026/08/21
    """

    def __init__(
        self,
        regime_stm: RegimeSTM,
        market_snapshot: MarketSnapshot,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 실패 확정과 예외 재전달을 동기화할 event를 가진 Controller spy를 만든다.
        인자: regime_stm -> 호출 여부를 관찰할 RegimeSTM
            market_snapshot -> 평가할 authoritative MarketSnapshot
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        super().__init__(regime_stm, market_snapshot)
        self.calculation_failure_confirmed = Event()
        self.release_calculation_failure = Event()

    def calculate_4h_indicators(
        self,
        snapshot: MarketSnapshot,
    ) -> IndicatorSnapshot:
        """
        함수 이름: calculate_4h_indicators()
        기능: production 계산 실패가 확정되면 외부 갱신을 기다린 뒤 같은 예외를 재전달한다.
        인자: snapshot -> 실패 provenance를 캡처할 MarketSnapshot
        반환값: production 계산이 성공한 경우의 IndicatorSnapshot
        작성 날짜: 2026/08/21
        """
        try:
            return super().calculate_4h_indicators(snapshot)
        except RegimeEvaluationError:
            self.calculation_failure_confirmed.set()
            if not self.release_calculation_failure.wait(timeout=5):
                raise TimeoutError(
                    "calculation failure was not released"
                )
            raise


def make_versioned_market_snapshot() -> tuple[MarketSnapshot, Mock]:
    """
    함수 이름: make_versioned_market_snapshot()
    기능: 같은 identity에서 다음 4시간봉으로 갱신할 수 있는 golden MarketSnapshot을 만든다.
    인자: 없음
    반환값: version 1 MarketSnapshot과 시각을 제어할 Mock clock
    작성 날짜: 2026/08/20
    """
    initial_snapshot = make_market_snapshot(load_golden_vector())
    mutable_clock = Mock(return_value=SNAPSHOT_UPDATED_AT)
    market_snapshot = MarketSnapshot(clock=mutable_clock)
    market_snapshot.update(initial_snapshot.klines_by_interval)

    return market_snapshot, mutable_clock


def advance_to_next_four_hour_candle(
    market_snapshot: MarketSnapshot,
    mutable_clock: Mock,
) -> None:
    """
    함수 이름: advance_to_next_four_hour_candle()
    기능: 기존 진행 4시간봉을 확정하고 다음 진행봉을 추가해 같은 snapshot을 version 2로 만든다.
    인자: market_snapshot -> 갱신할 version 1 snapshot
        mutable_clock -> snapshot의 갱신 시각을 제공하는 Mock clock
    반환값: 없음
    작성 날짜: 2026/08/20
    """
    next_updated_at = SNAPSHOT_UPDATED_AT + FOUR_HOURS
    mutable_clock.return_value = next_updated_at
    previous_four_hour_klines = market_snapshot.klines_by_interval[
        Interval.FOUR_HOURS
    ]
    previous_open_kline = previous_four_hour_klines[-1]
    next_four_hour_klines = (
        *previous_four_hour_klines[:-1],
        replace(previous_open_kline, closed=True),
        make_kline(
            Interval.FOUR_HOURS,
            previous_open_kline.open_time + FOUR_HOURS,
            Decimal("111"),
            high_price=Decimal("121"),
            low_price=Decimal("101"),
            closed=False,
        ),
    )
    one_minute_klines = (
        make_kline(
            Interval.ONE_MINUTE,
            next_updated_at - timedelta(minutes=1),
            Decimal("110"),
            closed=True,
        ),
        make_kline(
            Interval.ONE_MINUTE,
            next_updated_at,
            Decimal("111"),
            closed=False,
        ),
    )
    thirty_minute_klines = (
        make_kline(
            Interval.THIRTY_MINUTES,
            next_updated_at - timedelta(minutes=30),
            Decimal("110"),
            closed=True,
        ),
        make_kline(
            Interval.THIRTY_MINUTES,
            next_updated_at,
            Decimal("111"),
            closed=False,
        ),
    )
    next_klines_by_interval = {
        Interval.ONE_MINUTE: one_minute_klines,
        Interval.THIRTY_MINUTES: thirty_minute_klines,
        Interval.FOUR_HOURS: next_four_hour_klines,
        Interval.ONE_DAY: market_snapshot.klines_by_interval[
            Interval.ONE_DAY
        ],
    }

    market_snapshot.update(next_klines_by_interval)


def make_swing_structure(
    has_higher_high: bool,
    has_higher_low: bool,
    has_lower_high: bool,
    has_lower_low: bool,
) -> SwingStructure:
    """
    함수 이름: make_swing_structure()
    기능: guard table의 네 방향 flag와 일관된 불변 swing 값을 만든다.
    인자: has_higher_high -> HH guard 값
        has_higher_low -> HL guard 값
        has_lower_high -> LH guard 값
        has_lower_low -> LL guard 값
    반환값: Controller guard 입력용 SwingStructure
    작성 날짜: 2026/08/20
    """
    if has_higher_high:
        swing_highs = (Decimal("100"), Decimal("101"))
    elif has_lower_high:
        swing_highs = (Decimal("101"), Decimal("100"))
    else:
        swing_highs = (Decimal("100"), Decimal("100"))

    if has_higher_low:
        swing_lows = (Decimal("90"), Decimal("91"))
    elif has_lower_low:
        swing_lows = (Decimal("91"), Decimal("90"))
    else:
        swing_lows = (Decimal("90"), Decimal("90"))

    return SwingStructure(
        swing_highs=swing_highs,
        swing_lows=swing_lows,
        has_higher_high=has_higher_high,
        has_higher_low=has_higher_low,
        has_lower_high=has_lower_high,
        has_lower_low=has_lower_low,
    )


def apply_guard_profile(
    indicator_snapshot: IndicatorSnapshot,
    ema9_slope: Decimal,
    has_higher_high: bool,
    has_higher_low: bool,
    has_lower_high: bool,
    has_lower_low: bool,
    live_ema9: Decimal,
) -> None:
    """
    함수 이름: apply_guard_profile()
    기능: 계산 provenance를 유지하며 table-driven guard 지표만 원자적으로 대체한다.
    인자: indicator_snapshot -> Controller가 방금 계산한 지표 snapshot
        ema9_slope -> guard에 전달할 정규화 기울기
        has_higher_high -> HH guard 값
        has_higher_low -> HL guard 값
        has_lower_high -> LH guard 값
        has_lower_low -> LL guard 값
        live_ema9 -> 현재가 비교에 사용할 live EMA9
    반환값: 없음
    작성 날짜: 2026/08/20
    """
    indicator_snapshot.update(
        ema9_series=indicator_snapshot.ema9_series,
        ema9_slope=ema9_slope,
        swing_structure=make_swing_structure(
            has_higher_high,
            has_higher_low,
            has_lower_high,
            has_lower_low,
        ),
        live_ema9=live_ema9,
    )


class RegimeControllerMicrostepTests(unittest.TestCase):
    """
    클래스 이름: RegimeControllerMicrostepTests
    기능: 정상 추천의 두 microstep, 상태 반영과 13개 Event-Action 경로를 테스트한다.
    작성 날짜: 2026/08/20
    """

    def test_initial_evaluation_runs_two_microsteps_and_keeps_selection_separate(
        self,
    ) -> None:
        """
        함수 이름: test_initial_evaluation_runs_two_microsteps_and_keeps_selection_separate()
        기능: initial 평가가 EA-001과 EA-005를 직렬 실행하고 추천만 TYPE_2로 적용하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        market_snapshot = make_market_snapshot(load_golden_vector())
        regime_stm = RecordingRegimeSTM()
        controller = RegimeController(regime_stm, market_snapshot)

        regime_result = controller.evaluate_regime(
            RegimeEvaluationTrigger.INITIAL,
            market_snapshot,
        )

        self.assertIsInstance(regime_result, RegimeResult)
        self.assertIs(regime_result.recommended_type, RegimeType.TYPE_2)
        self.assertIsNone(regime_result.previous_recommended_type)
        self.assertTrue(regime_result.changed)
        self.assertEqual(regime_result.transition_id, "EA-005")
        self.assertIs(regime_result.state, RegimeState.TYPE_2_RECOMMENDED)
        self.assertIs(controller.recommended_regime, RegimeType.TYPE_2)
        self.assertIsNone(controller.selected_regime)
        self.assertIs(controller.last_regime_result, regime_result)
        self.assertIs(regime_stm.current_state, RegimeState.TYPE_2_RECOMMENDED)
        self.assertEqual(len(regime_stm.calls), 2)

        first_event, first_context = regime_stm.calls[0]
        ready_event, ready_context = regime_stm.calls[1]
        self.assertIs(
            first_event.event_type,
            RegimeEventType.INITIAL_EVALUATION_REQUESTED,
        )
        self.assertIsNone(first_context)
        self.assertIs(ready_event.event_type, RegimeEventType.EVALUATION_READY)
        self.assertIsInstance(ready_context, RegimeEvaluationContext)
        self.assertEqual(first_event.evaluation_id, ready_event.evaluation_id)
        self.assertEqual(
            first_event.source_candle_id,
            ready_context.source_candle_id,
        )
        self.assertEqual(
            ready_context.source_market_version,
            market_snapshot.version,
        )

        success_trace = controller.evaluation_traces[-1]
        self.assertEqual(success_trace.communication_steps, SUCCESS_COMMUNICATION_STEPS)
        self.assertEqual(success_trace.transition_ids, ("EA-001", "EA-005"))
        self.assertEqual(
            success_trace.event_ids,
            (first_event.event_id, ready_event.event_id),
        )
        self.assertEqual(success_trace.evaluation_id, regime_result.evaluation_id)
        self.assertIsNone(success_trace.failure_code)

    def test_same_recommendation_on_next_candle_reports_changed_false(self) -> None:
        """
        함수 이름: test_same_recommendation_on_next_candle_reports_changed_false()
        기능: 다음 4시간봉에서도 TYPE_2이면 EA-103 뒤 changed=False로 추천을 유지하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        market_snapshot, mutable_clock = make_versioned_market_snapshot()
        controller = RegimeController(RegimeSTM(), market_snapshot)
        first_result = controller.evaluate_regime(
            RegimeEvaluationTrigger.INITIAL,
            market_snapshot,
        )
        advance_to_next_four_hour_candle(market_snapshot, mutable_clock)

        second_result = controller.evaluate_regime(
            RegimeEvaluationTrigger.FOUR_HOUR_CANDLE_CLOSE,
            market_snapshot,
        )

        self.assertIsInstance(first_result, RegimeResult)
        self.assertIsInstance(second_result, RegimeResult)
        self.assertIs(second_result.recommended_type, RegimeType.TYPE_2)
        self.assertIs(
            second_result.previous_recommended_type,
            RegimeType.TYPE_2,
        )
        self.assertFalse(second_result.changed)
        self.assertEqual(second_result.transition_id, "EA-005")
        self.assertEqual(second_result.source_market_version, 2)
        self.assertNotEqual(
            second_result.source_candle_id,
            first_result.source_candle_id,
        )
        self.assertEqual(
            controller.evaluation_traces[-1].transition_ids,
            ("EA-103", "EA-005"),
        )
        self.assertFalse(controller.evaluation_traces[-1].changed)
        self.assertIsNone(controller.selected_regime)

    def test_controller_covers_all_thirteen_event_action_ids(self) -> None:
        """
        함수 이름: test_controller_covers_all_thirteen_event_action_ids()
        기능: table-driven 지표와 시작 상태로 Controller가 13개 명세 ID를 모두 통과하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        market_snapshot = make_market_snapshot(load_golden_vector())
        covered_transition_ids: set[str] = set()

        for (
            recommendation_id,
            expected_regime,
            ema9_slope,
            has_higher_high,
            has_higher_low,
            has_lower_high,
            has_lower_low,
            live_ema9,
        ) in RECOMMENDATION_CASES:
            with self.subTest(recommendation_id=recommendation_id):
                controller = RegimeController(RegimeSTM(), market_snapshot)
                indicators = controller.calculate_4h_indicators(
                    market_snapshot
                )
                apply_guard_profile(
                    indicators,
                    ema9_slope,
                    has_higher_high,
                    has_higher_low,
                    has_lower_high,
                    has_lower_low,
                    live_ema9,
                )

                recommendation = controller.recommend_regime(indicators)
                transition_ids = controller.evaluation_traces[-1].transition_ids

                self.assertIs(recommendation, expected_regime)
                self.assertEqual(
                    transition_ids,
                    ("EA-001", recommendation_id),
                )
                covered_transition_ids.update(transition_ids)

        sideways_case = RECOMMENDATION_CASES[0]
        for initial_state, start_transition_id in START_TRANSITION_CASES:
            with self.subTest(start_transition_id=start_transition_id):
                controller = RegimeController(
                    RegimeSTM(initial_state=initial_state),
                    market_snapshot,
                )
                indicators = controller.calculate_4h_indicators(
                    market_snapshot
                )
                apply_guard_profile(indicators, *sideways_case[2:])

                recommendation = controller.recommend_regime(indicators)
                transition_ids = controller.evaluation_traces[-1].transition_ids

                self.assertIs(recommendation, RegimeType.TYPE_0)
                self.assertEqual(
                    transition_ids,
                    (start_transition_id, "EA-002"),
                )
                covered_transition_ids.update(transition_ids)

        self.assertEqual(
            covered_transition_ids,
            set(SPECIFICATION_TRANSITION_IDS),
        )


class RegimeControllerFailureTests(unittest.TestCase):
    """
    클래스 이름: RegimeControllerFailureTests
    기능: duplicate, stale, 입력 부족과 Action mismatch가 상태를 오염시키지 않는지 테스트한다.
    작성 날짜: 2026/08/20
    """

    def test_duplicate_candle_is_deduplicated_without_stm_reentry(self) -> None:
        """
        함수 이름: test_duplicate_candle_is_deduplicated_without_stm_reentry()
        기능: 성공 처리한 같은 4H candle의 재평가가 trace만 남기고 STM과 추천을 유지하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        market_snapshot = make_market_snapshot(load_golden_vector())
        regime_stm = RecordingRegimeSTM()
        controller = RegimeController(regime_stm, market_snapshot)
        first_result = controller.evaluate_regime(
            RegimeEvaluationTrigger.INITIAL,
            market_snapshot,
        )
        state_after_success = regime_stm.current_state

        self.assertIsInstance(first_result, RegimeResult)
        for duplicate_trigger in (
            RegimeEvaluationTrigger.INITIAL,
            RegimeEvaluationTrigger.FOUR_HOUR_CANDLE_CLOSE,
        ):
            with self.subTest(duplicate_trigger=duplicate_trigger):
                duplicate_result = controller.evaluate_regime(
                    duplicate_trigger,
                    market_snapshot,
                )
                self.assertIsNone(duplicate_result)

        self.assertEqual(len(regime_stm.calls), 2)
        self.assertIs(regime_stm.current_state, state_after_success)
        self.assertIs(controller.recommended_regime, RegimeType.TYPE_2)
        self.assertIs(controller.last_regime_result, first_result)
        self.assertIsNone(controller.selected_regime)
        self.assertIs(
            controller.last_error.failure_code,
            RegimeEvaluationFailureCode.DUPLICATE_FOUR_HOUR_CANDLE,
        )
        self.assertEqual(controller.last_error.transition_ids, ())

    def test_older_unprocessed_candle_is_rejected_by_watermark(self) -> None:
        """
        함수 이름: test_older_unprocessed_candle_is_rejected_by_watermark()
        기능: 최신 성공 candle보다 과거인 미처리 candle을 version 증가와 무관하게 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        market_snapshot, mutable_clock = make_versioned_market_snapshot()
        regime_stm = RecordingRegimeSTM()
        controller = RegimeController(regime_stm, market_snapshot)
        successful_result = controller.evaluate_regime(
            RegimeEvaluationTrigger.INITIAL,
            market_snapshot,
        )
        successful_state = regime_stm.current_state
        older_updated_at = SNAPSHOT_UPDATED_AT - FOUR_HOURS
        mutable_clock.return_value = older_updated_at
        older_klines = {
            interval: tuple(
                replace(
                    kline,
                    open_time=kline.open_time - FOUR_HOURS,
                )
                for kline in interval_klines
            )
            for interval, interval_klines
            in market_snapshot.klines_by_interval.items()
        }
        market_snapshot.update(older_klines)

        stale_result = controller.evaluate_regime(
            RegimeEvaluationTrigger.FOUR_HOUR_CANDLE_CLOSE,
            market_snapshot,
        )

        self.assertIsInstance(successful_result, RegimeResult)
        self.assertIsNone(stale_result)
        self.assertEqual(len(regime_stm.calls), 2)
        self.assertIs(regime_stm.current_state, successful_state)
        self.assertIs(controller.last_regime_result, successful_result)
        self.assertIs(
            controller.last_error.failure_code,
            RegimeEvaluationFailureCode.STALE_FOUR_HOUR_CANDLE,
        )

    def test_stale_indicator_is_rejected_and_preserves_successful_state(self) -> None:
        """
        함수 이름: test_stale_indicator_is_rejected_and_preserves_successful_state()
        기능: 시장 version 증가 뒤 stale 지표 façade 호출이 STM과 마지막 정상 추천을 보존하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        market_snapshot, mutable_clock = make_versioned_market_snapshot()
        regime_stm = RecordingRegimeSTM()
        controller = RegimeController(regime_stm, market_snapshot)
        first_result = controller.evaluate_regime(
            RegimeEvaluationTrigger.INITIAL,
            market_snapshot,
        )
        stale_indicators = controller.indicator_snapshot
        calls_after_success = len(regime_stm.calls)
        state_after_success = regime_stm.current_state
        advance_to_next_four_hour_candle(market_snapshot, mutable_clock)

        with self.assertRaises(RegimeEvaluationError) as caught_error:
            controller.recommend_regime(stale_indicators)

        self.assertIs(
            caught_error.exception.failure_code,
            RegimeEvaluationFailureCode.STALE_MARKET_SNAPSHOT,
        )
        self.assertEqual(len(regime_stm.calls), calls_after_success)
        self.assertIs(regime_stm.current_state, state_after_success)
        self.assertIs(controller.recommended_regime, RegimeType.TYPE_2)
        self.assertIs(controller.last_regime_result, first_result)
        self.assertIs(
            controller.last_error.failure_code,
            RegimeEvaluationFailureCode.STALE_MARKET_SNAPSHOT,
        )

    def test_insufficient_input_records_failure_without_calling_stm(self) -> None:
        """
        함수 이름: test_insufficient_input_records_failure_without_calling_stm()
        기능: 확정봉 부족이 typed trace를 남기되 STM 호출과 추천 적용을 하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        market_snapshot = make_market_snapshot(
            load_golden_vector(),
            closed_count=13,
        )
        regime_stm = RecordingRegimeSTM()
        controller = RegimeController(regime_stm, market_snapshot)

        regime_result = controller.evaluate_regime(
            RegimeEvaluationTrigger.INITIAL,
            market_snapshot,
        )

        self.assertIsNone(regime_result)
        self.assertEqual(regime_stm.calls, [])
        self.assertIs(regime_stm.current_state, RegimeState.INITIAL)
        self.assertIsNone(controller.recommended_regime)
        self.assertIsNone(controller.last_regime_result)
        failure_trace = controller.last_error
        self.assertIs(
            failure_trace.failure_code,
            RegimeEvaluationFailureCode.INSUFFICIENT_CLOSED_KLINES,
        )
        self.assertEqual(failure_trace.transition_ids, ())
        self.assertEqual(failure_trace.event_ids, ())
        self.assertEqual(failure_trace.source_market_version, 1)

        repeated_result = controller.evaluate_regime(
            RegimeEvaluationTrigger.INITIAL,
            market_snapshot,
        )

        self.assertIsNone(repeated_result)
        self.assertEqual(regime_stm.calls, [])
        self.assertIs(
            controller.last_error.failure_code,
            RegimeEvaluationFailureCode.REPEATED_FAILED_MARKET_VERSION,
        )

    def test_failure_trace_keeps_v1_provenance_after_snapshot_advances(
        self,
    ) -> None:
        """
        함수 이름: test_failure_trace_keeps_v1_provenance_after_snapshot_advances()
        기능: 안정된 v1 계산 실패 뒤 snapshot이 v2가 되어도 trace가 v1 provenance를 보존하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        initial_snapshot = make_market_snapshot(
            load_golden_vector(),
            closed_count=13,
        )
        mutable_clock = Mock(return_value=SNAPSHOT_UPDATED_AT)
        market_snapshot = MarketSnapshot(clock=mutable_clock)
        market_snapshot.update(initial_snapshot.klines_by_interval)
        regime_stm = RecordingRegimeSTM()
        controller = PausingFailureRegimeController(
            regime_stm,
            market_snapshot,
        )
        v1_recorded_at = market_snapshot.updated_at

        with ThreadPoolExecutor(max_workers=1) as executor:
            evaluation_future = executor.submit(
                controller.evaluate_regime,
                RegimeEvaluationTrigger.INITIAL,
                market_snapshot,
            )
            self.assertTrue(
                controller.calculation_failure_confirmed.wait(timeout=2)
            )
            try:
                advance_to_next_four_hour_candle(
                    market_snapshot,
                    mutable_clock,
                )
            finally:
                controller.release_calculation_failure.set()

            regime_result = evaluation_future.result(timeout=5)

        self.assertIsNone(regime_result)
        self.assertEqual(market_snapshot.version, 2)
        self.assertEqual(
            market_snapshot.updated_at,
            SNAPSHOT_UPDATED_AT + FOUR_HOURS,
        )
        self.assertEqual(regime_stm.calls, [])
        failure_trace = controller.last_error
        self.assertIs(
            failure_trace.failure_code,
            RegimeEvaluationFailureCode.INSUFFICIENT_CLOSED_KLINES,
        )
        self.assertEqual(failure_trace.source_market_version, 1)
        self.assertEqual(
            failure_trace.source_candle_id,
            LATEST_CLOSED_CANDLE_ID,
        )
        self.assertEqual(failure_trace.recorded_at, v1_recorded_at)
        self.assertIn(":1:", failure_trace.evaluation_id)
        self.assertIn(
            LATEST_CLOSED_CANDLE_ID,
            failure_trace.evaluation_id,
        )

    def test_repeated_failed_version_trace_keeps_latched_provenance(
        self,
    ) -> None:
        """
        함수 이름: test_repeated_failed_version_trace_keeps_latched_provenance()
        기능: v1 재시도 거부 직후 v2가 도착해도 실패 trace가 v1 provenance를 유지하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        initial_snapshot = make_market_snapshot(
            load_golden_vector(),
            closed_count=13,
        )
        mutable_clock = Mock(return_value=SNAPSHOT_UPDATED_AT)
        market_snapshot = MarketSnapshot(clock=mutable_clock)
        market_snapshot.update(initial_snapshot.klines_by_interval)
        regime_stm = RecordingRegimeSTM()
        controller = PausingFailureRegimeController(
            regime_stm,
            market_snapshot,
        )
        v1_recorded_at = market_snapshot.updated_at
        controller.release_calculation_failure.set()

        first_result = controller.evaluate_regime(
            RegimeEvaluationTrigger.INITIAL,
            market_snapshot,
        )

        self.assertIsNone(first_result)
        controller.calculation_failure_confirmed.clear()
        controller.release_calculation_failure.clear()
        with ThreadPoolExecutor(max_workers=1) as executor:
            evaluation_future = executor.submit(
                controller.evaluate_regime,
                RegimeEvaluationTrigger.INITIAL,
                market_snapshot,
            )
            self.assertTrue(
                controller.calculation_failure_confirmed.wait(timeout=2)
            )
            try:
                advance_to_next_four_hour_candle(
                    market_snapshot,
                    mutable_clock,
                )
            finally:
                controller.release_calculation_failure.set()

            repeated_result = evaluation_future.result(timeout=5)

        self.assertIsNone(repeated_result)
        self.assertEqual(market_snapshot.version, 2)
        self.assertEqual(regime_stm.calls, [])
        failure_trace = controller.last_error
        self.assertIs(
            failure_trace.failure_code,
            RegimeEvaluationFailureCode.REPEATED_FAILED_MARKET_VERSION,
        )
        self.assertEqual(failure_trace.source_market_version, 1)
        self.assertEqual(
            failure_trace.source_candle_id,
            LATEST_CLOSED_CANDLE_ID,
        )
        self.assertEqual(failure_trace.recorded_at, v1_recorded_at)
        self.assertIn(":1:", failure_trace.evaluation_id)
        self.assertIn(
            LATEST_CLOSED_CANDLE_ID,
            failure_trace.evaluation_id,
        )

    def test_failed_calculation_can_retry_after_market_version_changes(
        self,
    ) -> None:
        """
        함수 이름: test_failed_calculation_can_retry_after_market_version_changes()
        기능: 입력 부족 version을 차단한 뒤 새 version의 정상 입력은 다시 계산한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        golden_vector = load_golden_vector()
        insufficient_snapshot = make_market_snapshot(
            golden_vector,
            closed_count=13,
        )
        complete_snapshot = make_market_snapshot(golden_vector)
        mutable_clock = Mock(return_value=SNAPSHOT_UPDATED_AT)
        market_snapshot = MarketSnapshot(clock=mutable_clock)
        market_snapshot.update(insufficient_snapshot.klines_by_interval)
        regime_stm = RecordingRegimeSTM()
        controller = RegimeController(regime_stm, market_snapshot)

        first_result = controller.evaluate_regime(
            RegimeEvaluationTrigger.INITIAL,
            market_snapshot,
        )
        market_snapshot.update(complete_snapshot.klines_by_interval)
        retry_result = controller.evaluate_regime(
            RegimeEvaluationTrigger.INITIAL,
            market_snapshot,
        )

        self.assertIsNone(first_result)
        self.assertIsInstance(retry_result, RegimeResult)
        self.assertEqual(retry_result.source_market_version, 2)
        self.assertEqual(len(regime_stm.calls), 2)
        self.assertIs(controller.recommended_regime, RegimeType.TYPE_2)

    def test_version_change_during_calculation_restarts_from_latest_snapshot(
        self,
    ) -> None:
        """
        함수 이름: test_version_change_during_calculation_restarts_from_latest_snapshot()
        기능: 지표 계산 중 version 변경 시 오래된 계산을 버리고 최신 version으로 재시작한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        market_snapshot, mutable_clock = make_versioned_market_snapshot()
        current_klines = market_snapshot.klines_by_interval
        current_four_hour_klines = current_klines[Interval.FOUR_HOURS]
        updated_open_kline = replace(
            current_four_hour_klines[-1],
            high=Decimal("120"),
            close=Decimal("120"),
        )
        updated_klines = {
            interval: tuple(interval_klines)
            for interval, interval_klines in current_klines.items()
        }
        updated_klines[Interval.FOUR_HOURS] = (
            *current_four_hour_klines[:-1],
            updated_open_kline,
        )
        original_calculate_ema9_series = (
            regime_module._calculate_ema9_series
        )
        calculation_count = 0

        def advance_version_once(
            closed_klines: tuple[Kline, ...],
        ) -> tuple[Decimal, ...]:
            """
            함수 이름: advance_version_once()
            기능: 첫 EMA 계산 중 동일 snapshot을 version 2로 한 번 갱신한다.
            인자: closed_klines -> production EMA helper에 전달할 확정봉
            반환값: production helper가 계산한 EMA9 tuple
            작성 날짜: 2026/08/20
            """
            nonlocal calculation_count
            calculation_count += 1
            if calculation_count == 1:
                mutable_clock.return_value = SNAPSHOT_UPDATED_AT
                market_snapshot.update(updated_klines)

            return original_calculate_ema9_series(closed_klines)

        regime_stm = RecordingRegimeSTM()
        controller = RegimeController(regime_stm, market_snapshot)
        with patch.object(
            regime_module,
            "_calculate_ema9_series",
            side_effect=advance_version_once,
        ):
            regime_result = controller.evaluate_regime(
                RegimeEvaluationTrigger.INITIAL,
                market_snapshot,
            )

        self.assertIsInstance(regime_result, RegimeResult)
        self.assertEqual(regime_result.source_market_version, 2)
        self.assertEqual(controller.indicator_snapshot.current_price, Decimal("120"))
        self.assertEqual(calculation_count, 2)
        self.assertEqual(len(regime_stm.calls), 2)

    def test_action_provenance_mismatch_is_rejected_before_recommendation(self) -> None:
        """
        함수 이름: test_action_provenance_mismatch_is_rejected_before_recommendation()
        기능: 변조된 StartRegimeEvaluation ID를 거부하고 추천 Action을 실행하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        market_snapshot = make_market_snapshot(load_golden_vector())
        regime_stm = MismatchedStartActionRegimeSTM()
        controller = RegimeController(regime_stm, market_snapshot)
        indicators = controller.calculate_4h_indicators(market_snapshot)

        with self.assertRaises(RegimeEvaluationError) as caught_error:
            controller.recommend_regime(indicators)

        self.assertIs(
            caught_error.exception.failure_code,
            RegimeEvaluationFailureCode.ACTION_CONTRACT_MISMATCH,
        )
        self.assertEqual(len(regime_stm.calls), 1)
        self.assertIsNone(controller.recommended_regime)
        self.assertIsNone(controller.last_regime_result)
        self.assertIsNone(controller.selected_regime)
        self.assertIs(
            controller.last_error.failure_code,
            RegimeEvaluationFailureCode.ACTION_CONTRACT_MISMATCH,
        )
        self.assertEqual(
            controller.last_error.communication_steps,
            SUCCESS_COMMUNICATION_STEPS[:4],
        )
        self.assertEqual(len(controller.last_error.event_ids), 1)
        self.assertEqual(controller.last_error.transition_ids, ("EA-001",))
        self.assertIs(
            controller.last_error.state_before,
            RegimeState.INITIAL,
        )
        self.assertIs(
            controller.last_error.state_after,
            RegimeState.FOUR_HOUR_CANDLE_EVALUATION,
        )

    def test_start_trigger_and_result_evaluation_id_are_validated(self) -> None:
        """
        함수 이름: test_start_trigger_and_result_evaluation_id_are_validated()
        기능: 시작 Action trigger와 STM result evaluation ID 변조를 모두 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        for fault_kind in ("start_trigger", "result_evaluation_id"):
            with self.subTest(fault_kind=fault_kind):
                market_snapshot = make_market_snapshot(
                    load_golden_vector()
                )
                regime_stm = ContractFaultRegimeSTM(fault_kind)
                controller = RegimeController(regime_stm, market_snapshot)
                indicators = controller.calculate_4h_indicators(
                    market_snapshot
                )

                with self.assertRaises(RegimeEvaluationError) as caught_error:
                    controller.recommend_regime(indicators)

                self.assertIs(
                    caught_error.exception.failure_code,
                    RegimeEvaluationFailureCode.ACTION_CONTRACT_MISMATCH,
                )
                self.assertEqual(len(regime_stm.calls), 1)
                self.assertIsNone(controller.recommended_regime)
                self.assertEqual(
                    controller.last_error.transition_ids,
                    ("EA-001",),
                )


class RegimeControllerConcurrencyTests(unittest.TestCase):
    """
    클래스 이름: RegimeControllerConcurrencyTests
    기능: 동시에 시작한 평가가 한 Controller에서 끝까지 직렬 처리되는지 테스트한다.
    작성 날짜: 2026/08/20
    """

    def test_concurrent_evaluations_are_serialized_and_second_is_deduplicated(
        self,
    ) -> None:
        """
        함수 이름: test_concurrent_evaluations_are_serialized_and_second_is_deduplicated()
        기능: 첫 두 microstep 중 다른 평가가 STM에 진입하지 않고 이후 duplicate로 종료되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        market_snapshot = make_market_snapshot(load_golden_vector())
        regime_stm = BlockingRegimeSTM()
        controller = RegimeController(regime_stm, market_snapshot)
        second_evaluation_started = Event()

        def run_second_evaluation() -> RegimeResult | None:
            """
            함수 이름: run_second_evaluation()
            기능: 두 번째 worker 시작을 표시한 뒤 동일 candle 평가를 요청한다.
            인자: 없음
            반환값: duplicate이면 None인 Controller 평가 결과
            작성 날짜: 2026/08/20
            """
            second_evaluation_started.set()
            return controller.evaluate_regime(
                RegimeEvaluationTrigger.FOUR_HOUR_CANDLE_CLOSE,
                market_snapshot,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(
                controller.evaluate_regime,
                RegimeEvaluationTrigger.INITIAL,
                market_snapshot,
            )
            self.assertTrue(regime_stm.first_handle_entered.wait(timeout=2))
            second_future = executor.submit(run_second_evaluation)
            self.assertTrue(second_evaluation_started.wait(timeout=2))
            self.assertEqual(regime_stm.handle_attempts, 1)
            regime_stm.release_first_handle.set()
            first_result = first_future.result(timeout=5)
            second_result = second_future.result(timeout=5)

        self.assertIsInstance(first_result, RegimeResult)
        self.assertIsNone(second_result)
        self.assertEqual(regime_stm.handle_attempts, 2)
        self.assertEqual(regime_stm.maximum_active_handles, 1)
        self.assertEqual(
            tuple(trace.failure_code for trace in controller.evaluation_traces),
            (
                None,
                RegimeEvaluationFailureCode.DUPLICATE_FOUR_HOUR_CANDLE,
            ),
        )
        self.assertIs(regime_stm.current_state, RegimeState.TYPE_2_RECOMMENDED)
        self.assertIs(controller.recommended_regime, RegimeType.TYPE_2)


if __name__ == "__main__":
    unittest.main()
