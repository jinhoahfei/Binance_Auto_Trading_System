"""RegimeSTM의 13개 transition과 결과 계약을 검증한다."""

import unittest
from decimal import Decimal

from binance_auto_trader.domain.regime import (
    ApplyRecommendedRegime,
    RegimeEvaluationTrigger,
    RegimeEventType,
    RegimeSTM,
    RegimeState,
    RegimeType,
    StartRegimeEvaluation,
)

from tests.unit.regime.factories import make_context, make_event


class InitialAndReevaluationTransitionTests(unittest.TestCase):
    """
    클래스 이름: InitialAndReevaluationTransitionTests
    기능: 최초 평가와 다섯 추천 상태의 재평가 시작 transition을 테스트한다.
    작성 날짜: 2026/08/14
    """

    def test_initial_event_selects_ea_001(self) -> None:
        """
        함수 이름: test_initial_event_selects_ea_001()
        기능: INITIAL 상태의 최초 평가 이벤트가 EA-001을 선택하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        state_machine = RegimeSTM()
        event = make_event(
            RegimeEventType.INITIAL_EVALUATION_REQUESTED,
        )

        result = state_machine.handle(event, make_context())

        self.assertTrue(result.consumed)
        self.assertEqual(result.transition_id, "EA-001")
        self.assertIs(result.state_before, RegimeState.INITIAL)
        self.assertIs(
            result.state_after,
            RegimeState.FOUR_HOUR_CANDLE_EVALUATION,
        )
        self.assertIs(state_machine.current_state, result.state_after)
        self.assertEqual(len(result.action_requests), 1)
        action_request = result.action_requests[0]
        self.assertIsInstance(action_request, StartRegimeEvaluation)
        self.assertIs(
            action_request.trigger,
            RegimeEvaluationTrigger.INITIAL,
        )

    def test_recommended_states_select_ea_101_through_ea_105(self) -> None:
        """
        함수 이름: test_recommended_states_select_ea_101_through_ea_105()
        기능: 다섯 추천 상태의 4시간봉 마감 이벤트가 지정된 ID로 전이하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        transition_cases = (
            (RegimeState.TYPE_0_RECOMMENDED, "EA-101"),
            (RegimeState.TYPE_1_RECOMMENDED, "EA-102"),
            (RegimeState.TYPE_2_RECOMMENDED, "EA-103"),
            (RegimeState.TYPE_3_RECOMMENDED, "EA-104"),
            (RegimeState.TYPE_4_RECOMMENDED, "EA-105"),
        )

        for initial_state, expected_transition_id in transition_cases:
            state_machine = RegimeSTM(initial_state=initial_state)
            event = make_event(
                RegimeEventType.FOUR_HOUR_CANDLE_CLOSED,
                source_candle_id="ETHUSDT:4h:2026-08-14T08:00:00Z",
            )
            context = make_context(
                source_candle_id="ETHUSDT:4h:2026-08-14T08:00:00Z",
            )

            result = state_machine.handle(event, context)

            with self.subTest(initial_state=initial_state):
                self.assertEqual(
                    result.transition_id,
                    expected_transition_id,
                )
                self.assertIs(
                    result.state_after,
                    RegimeState.FOUR_HOUR_CANDLE_EVALUATION,
                )
                action_request = result.action_requests[0]
                self.assertIsInstance(
                    action_request,
                    StartRegimeEvaluation,
                )
                self.assertIs(
                    action_request.trigger,
                    RegimeEvaluationTrigger.FOUR_HOUR_CANDLE_CLOSE,
                )


class RecommendationTransitionTests(unittest.TestCase):
    """
    클래스 이름: RecommendationTransitionTests
    기능: 평가 상태의 EA-002부터 EA-008 추천 transition을 테스트한다.
    작성 날짜: 2026/08/14
    """

    def test_recommendation_guards_select_expected_transition(self) -> None:
        """
        함수 이름: test_recommendation_guards_select_expected_transition()
        기능: 각 slope와 복합조건 입력이 명세의 추천 transition을 선택하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        transition_cases = (
            (
                make_context(ema9_slope=Decimal("0")),
                "EA-002",
                RegimeType.TYPE_0,
                RegimeState.TYPE_0_RECOMMENDED,
            ),
            (
                make_context(ema9_slope=Decimal("0.16")),
                "EA-003",
                RegimeType.TYPE_1,
                RegimeState.TYPE_1_RECOMMENDED,
            ),
            (
                make_context(ema9_slope=Decimal("0.30")),
                "EA-004",
                RegimeType.TYPE_1,
                RegimeState.TYPE_1_RECOMMENDED,
            ),
            (
                make_context(
                    ema9_slope=Decimal("0.30"),
                    has_higher_high=True,
                    has_higher_low=True,
                    current_price=Decimal("100"),
                    live_ema9=Decimal("100"),
                ),
                "EA-005",
                RegimeType.TYPE_2,
                RegimeState.TYPE_2_RECOMMENDED,
            ),
            (
                make_context(ema9_slope=Decimal("-0.16")),
                "EA-006",
                RegimeType.TYPE_3,
                RegimeState.TYPE_3_RECOMMENDED,
            ),
            (
                make_context(ema9_slope=Decimal("-0.30")),
                "EA-007",
                RegimeType.TYPE_3,
                RegimeState.TYPE_3_RECOMMENDED,
            ),
            (
                make_context(
                    ema9_slope=Decimal("-0.30"),
                    has_lower_high=True,
                    has_lower_low=True,
                    current_price=Decimal("100"),
                    live_ema9=Decimal("100"),
                ),
                "EA-008",
                RegimeType.TYPE_4,
                RegimeState.TYPE_4_RECOMMENDED,
            ),
        )

        for (
            context,
            expected_transition_id,
            expected_regime_type,
            expected_state,
        ) in transition_cases:
            state_machine = RegimeSTM(
                initial_state=RegimeState.FOUR_HOUR_CANDLE_EVALUATION,
            )
            event = make_event(RegimeEventType.EVALUATION_READY)

            result = state_machine.handle(event, context)

            with self.subTest(
                expected_transition_id=expected_transition_id,
            ):
                self.assertEqual(
                    result.transition_id,
                    expected_transition_id,
                )
                self.assertIs(result.state_after, expected_state)
                action_request = result.action_requests[0]
                self.assertIsInstance(
                    action_request,
                    ApplyRecommendedRegime,
                )
                self.assertIs(
                    action_request.regime_type,
                    expected_regime_type,
                )

    def test_sideways_boundaries_belong_to_ea_002(self) -> None:
        """
        함수 이름: test_sideways_boundaries_belong_to_ea_002()
        기능: -0.15와 0.15가 모두 횡보 transition에 포함되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        for slope in (Decimal("-0.15"), Decimal("0.15")):
            state_machine = RegimeSTM(
                initial_state=RegimeState.FOUR_HOUR_CANDLE_EVALUATION,
            )
            event = make_event(RegimeEventType.EVALUATION_READY)

            result = state_machine.handle(
                event,
                make_context(ema9_slope=slope),
            )

            with self.subTest(slope=slope):
                self.assertEqual(result.transition_id, "EA-002")

    def test_ea_007_covers_any_failed_strong_down_composite(self) -> None:
        """
        함수 이름: test_ea_007_covers_any_failed_strong_down_composite()
        기능: 강하락 복합조건의 구조 또는 가격 실패가 모두 EA-007인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        contexts = (
            make_context(
                ema9_slope=Decimal("-1"),
                has_lower_high=False,
                has_lower_low=False,
                current_price=Decimal("90"),
                live_ema9=Decimal("100"),
            ),
            make_context(
                ema9_slope=Decimal("-1"),
                has_lower_high=True,
                has_lower_low=True,
                current_price=Decimal("101"),
                live_ema9=Decimal("100"),
            ),
        )

        for context in contexts:
            state_machine = RegimeSTM(
                initial_state=RegimeState.FOUR_HOUR_CANDLE_EVALUATION,
            )
            event = make_event(RegimeEventType.EVALUATION_READY)

            result = state_machine.handle(event, context)

            with self.subTest(context=context):
                self.assertEqual(result.transition_id, "EA-007")


class RegimeSTMBehaviorTests(unittest.TestCase):
    """
    클래스 이름: RegimeSTMBehaviorTests
    기능: RegimeSTM의 no-op, 결정론 및 입력 연결 검증 동작을 테스트한다.
    작성 날짜: 2026/08/14
    """

    def test_unhandled_event_returns_no_op_without_state_change(self) -> None:
        """
        함수 이름: test_unhandled_event_returns_no_op_without_state_change()
        기능: transition이 없는 이벤트가 명시적 no-op 결과와 기존 상태를 반환하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        state_machine = RegimeSTM()
        event = make_event(RegimeEventType.EVALUATION_READY)

        result = state_machine.handle(event)

        self.assertFalse(result.consumed)
        self.assertIsNone(result.transition_id)
        self.assertEqual(result.action_requests, ())
        self.assertIs(result.state_before, RegimeState.INITIAL)
        self.assertIs(result.state_after, RegimeState.INITIAL)
        self.assertIs(state_machine.current_state, RegimeState.INITIAL)

    def test_same_state_event_and_context_are_deterministic(self) -> None:
        """
        함수 이름: test_same_state_event_and_context_are_deterministic()
        기능: 같은 상태, 이벤트 및 Context가 동일한 결과를 만드는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        first_machine = RegimeSTM(
            initial_state=RegimeState.FOUR_HOUR_CANDLE_EVALUATION,
        )
        second_machine = RegimeSTM(
            initial_state=RegimeState.FOUR_HOUR_CANDLE_EVALUATION,
        )
        event = make_event(RegimeEventType.EVALUATION_READY)
        context = make_context(ema9_slope=Decimal("0.16"))

        first_result = first_machine.handle(event, context)
        second_result = second_machine.handle(event, context)

        self.assertEqual(first_result, second_result)

    def test_evaluation_id_mismatch_fails_without_state_change(self) -> None:
        """
        함수 이름: test_evaluation_id_mismatch_fails_without_state_change()
        기능: 이벤트와 Context의 평가 ID 불일치가 상태 변경 전에 실패하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        state_machine = RegimeSTM(
            initial_state=RegimeState.FOUR_HOUR_CANDLE_EVALUATION,
        )
        event = make_event(
            RegimeEventType.EVALUATION_READY,
            evaluation_id="evaluation-event",
        )
        context = make_context(evaluation_id="evaluation-context")

        with self.assertRaises(ValueError):
            state_machine.handle(event, context)

        self.assertIs(
            state_machine.current_state,
            RegimeState.FOUR_HOUR_CANDLE_EVALUATION,
        )

    def test_evaluation_ready_requires_context(self) -> None:
        """
        함수 이름: test_evaluation_ready_requires_context()
        기능: 평가 상태의 EVALUATION_READY가 Context 없이 처리되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        state_machine = RegimeSTM(
            initial_state=RegimeState.FOUR_HOUR_CANDLE_EVALUATION,
        )
        event = make_event(RegimeEventType.EVALUATION_READY)

        with self.assertRaises(ValueError):
            state_machine.handle(event)

        self.assertIs(
            state_machine.current_state,
            RegimeState.FOUR_HOUR_CANDLE_EVALUATION,
        )

    def test_source_candle_mismatch_fails_without_state_change(self) -> None:
        """
        함수 이름: test_source_candle_mismatch_fails_without_state_change()
        기능: 이벤트와 Context의 candle ID 불일치가 stale 적용을 막는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        state_machine = RegimeSTM(
            initial_state=RegimeState.FOUR_HOUR_CANDLE_EVALUATION,
        )
        event = make_event(
            RegimeEventType.EVALUATION_READY,
            source_candle_id="candle-event",
        )
        context = make_context(source_candle_id="candle-context")

        with self.assertRaises(ValueError):
            state_machine.handle(event, context)

        self.assertIs(
            state_machine.current_state,
            RegimeState.FOUR_HOUR_CANDLE_EVALUATION,
        )


if __name__ == "__main__":
    unittest.main()
