"""RegimeSTM 불변 값 객체의 입력 계약을 검증한다."""

import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime
from decimal import Decimal

from binance_auto_trader.domain.regime import (
    ApplyRecommendedRegime,
    Interval,
    RegimeEvaluationContext,
    RegimeEvent,
    RegimeEventType,
    RegimeResult,
    RegimeState,
    RegimeType,
    recommended_state_for,
    regime_type_for,
)

from tests.unit.regime.factories import TEST_INSTANT, make_context


class RegimeEvaluationContextTests(unittest.TestCase):
    """
    클래스 이름: RegimeEvaluationContextTests
    기능: RegimeEvaluationContext의 검증과 불변성을 테스트한다.
    작성 날짜: 2026/08/14
    """

    def test_accepts_valid_four_hour_context(self) -> None:
        """
        함수 이름: test_accepts_valid_four_hour_context()
        기능: 유한 Decimal과 4시간 주기의 Context가 생성되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        context = make_context(ema9_slope=Decimal("0.15"))

        self.assertEqual(context.timeframe, Interval.FOUR_HOURS)
        self.assertEqual(context.ema9_slope, Decimal("0.15"))

    def test_rejects_non_finite_decimal_values(self) -> None:
        """
        함수 이름: test_rejects_non_finite_decimal_values()
        기능: NaN과 무한대 Decimal이 Context에 들어갈 수 없는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        invalid_values = (
            Decimal("NaN"),
            Decimal("Infinity"),
            Decimal("-Infinity"),
        )

        for invalid_value in invalid_values:
            with self.subTest(invalid_value=invalid_value):
                with self.assertRaises(ValueError):
                    make_context(ema9_slope=invalid_value)

    def test_rejects_non_positive_price_and_live_ema9(self) -> None:
        """
        함수 이름: test_rejects_non_positive_price_and_live_ema9()
        기능: 현재가와 실시간 EMA9가 0보다 커야 하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        with self.assertRaises(ValueError):
            make_context(current_price=Decimal("0"))

        with self.assertRaises(ValueError):
            make_context(live_ema9=Decimal("-1"))

    def test_rejects_non_four_hour_timeframe(self) -> None:
        """
        함수 이름: test_rejects_non_four_hour_timeframe()
        기능: REGIME Context가 4시간 외의 주기를 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        with self.assertRaises(ValueError):
            RegimeEvaluationContext(
                evaluation_id="evaluation-1",
                symbol="ETHUSDT",
                timeframe=Interval.THIRTY_MINUTES,
                ema9_slope=Decimal("0"),
                has_higher_high=False,
                has_higher_low=False,
                has_lower_high=False,
                has_lower_low=False,
                current_price=Decimal("100"),
                live_ema9=Decimal("100"),
                source_market_version=42,
                source_candle_id=None,
                calculated_at=TEST_INSTANT,
            )

    def test_context_is_frozen(self) -> None:
        """
        함수 이름: test_context_is_frozen()
        기능: 생성된 평가 Context의 필드를 변경할 수 없는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        context = make_context()

        with self.assertRaises(FrozenInstanceError):
            context.ema9_slope = Decimal("1")


class RegimeEventAndActionTests(unittest.TestCase):
    """
    클래스 이름: RegimeEventAndActionTests
    기능: REGIME 이벤트와 Action 요청의 식별자 및 불변성을 테스트한다.
    작성 날짜: 2026/08/14
    """

    def test_four_hour_close_requires_source_candle_id(self) -> None:
        """
        함수 이름: test_four_hour_close_requires_source_candle_id()
        기능: 4시간봉 마감 이벤트에 candle ID가 필수인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        with self.assertRaises(ValueError):
            RegimeEvent(
                event_type=RegimeEventType.FOUR_HOUR_CANDLE_CLOSED,
                event_id="event-1",
                occurred_at=TEST_INSTANT,
                evaluation_id="evaluation-1",
            )

    def test_event_rejects_naive_datetime(self) -> None:
        """
        함수 이름: test_event_rejects_naive_datetime()
        기능: 시간대가 없는 이벤트 시각을 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        with self.assertRaises(ValueError):
            RegimeEvent(
                event_type=RegimeEventType.INITIAL_EVALUATION_REQUESTED,
                event_id="event-1",
                occurred_at=datetime(2026, 8, 14, 12, 0),
                evaluation_id="evaluation-1",
            )

    def test_action_request_is_frozen(self) -> None:
        """
        함수 이름: test_action_request_is_frozen()
        기능: 생성된 추천 적용 Action 요청을 변경할 수 없는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        action_request = ApplyRecommendedRegime(
            evaluation_id="evaluation-1",
            regime_type=RegimeType.TYPE_0,
            source_candle_id=None,
        )

        with self.assertRaises(FrozenInstanceError):
            action_request.regime_type = RegimeType.TYPE_1


class RegimeResultTests(unittest.TestCase):
    """
    클래스 이름: RegimeResultTests
    기능: Controller 추천 결과의 MarketSnapshot provenance 계약을 테스트한다.
    작성 날짜: 2026/08/20
    """

    def test_source_market_version_requires_positive_integer(self) -> None:
        """
        함수 이름: test_source_market_version_requires_positive_integer()
        기능: 성공 RegimeResult가 bool, 0과 음수 source version을 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        valid_result = RegimeResult(
            evaluation_id="evaluation-1",
            recommended_type=RegimeType.TYPE_0,
            previous_recommended_type=None,
            changed=True,
            transition_id="EA-002",
            state=RegimeState.TYPE_0_RECOMMENDED,
            source_market_version=1,
            source_candle_id="ETHUSDT:4h:candle-1",
            calculated_at=TEST_INSTANT,
        )
        invalid_cases = (
            (True, TypeError),
            (0, ValueError),
            (-1, ValueError),
        )

        for invalid_version, expected_error in invalid_cases:
            with self.subTest(invalid_version=invalid_version):
                with self.assertRaises(expected_error):
                    replace(
                        valid_result,
                        source_market_version=invalid_version,
                    )


class RegimeStateMappingTests(unittest.TestCase):
    """
    클래스 이름: RegimeStateMappingTests
    기능: 추천 타입과 추천 상태의 일대일 mapping을 테스트한다.
    작성 날짜: 2026/08/14
    """

    def test_every_regime_type_has_one_recommended_state(self) -> None:
        """
        함수 이름: test_every_regime_type_has_one_recommended_state()
        기능: 다섯 REGIME 타입의 상태 mapping이 양방향으로 일치하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        for regime_type in RegimeType:
            recommended_state = recommended_state_for(regime_type)

            with self.subTest(regime_type=regime_type):
                self.assertIs(
                    regime_type_for(recommended_state),
                    regime_type,
                )

    def test_non_recommended_state_has_no_regime_type(self) -> None:
        """
        함수 이름: test_non_recommended_state_has_no_regime_type()
        기능: 평가 중간 상태를 추천 타입으로 변환할 수 없는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        with self.assertRaises(ValueError):
            regime_type_for(RegimeState.FOUR_HOUR_CANDLE_EVALUATION)


if __name__ == "__main__":
    unittest.main()
