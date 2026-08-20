"""EA-002부터 EA-008까지의 순수 guard를 검증한다."""

import unittest
from decimal import Decimal

from binance_auto_trader.domain.regime.guards import (
    matches_sideways_regime,
    matches_strong_down_fallback,
    matches_strong_down_regime,
    matches_strong_up_fallback,
    matches_strong_up_regime,
    matches_weak_down_regime,
    matches_weak_up_regime,
)

from tests.unit.regime.factories import make_context


class RegimeGuardTests(unittest.TestCase):
    """
    클래스 이름: RegimeGuardTests
    기능: REGIME 추천 guard의 경계값, 완전성 및 배타성을 테스트한다.
    작성 날짜: 2026/08/14
    """

    def test_slope_boundaries_select_exactly_one_guard(self) -> None:
        """
        함수 이름: test_slope_boundaries_select_exactly_one_guard()
        기능: 네 slope 경계와 인접 구간에서 정확히 한 guard만 참인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        guards = (
            matches_sideways_regime,
            matches_weak_up_regime,
            matches_strong_up_fallback,
            matches_strong_up_regime,
            matches_weak_down_regime,
            matches_strong_down_fallback,
            matches_strong_down_regime,
        )
        slopes = (
            Decimal("-10"),
            Decimal("-0.30"),
            Decimal("-0.299"),
            Decimal("-0.15"),
            Decimal("0"),
            Decimal("0.15"),
            Decimal("0.151"),
            Decimal("0.30"),
            Decimal("10"),
        )

        for slope in slopes:
            context = make_context(ema9_slope=slope)
            match_count = sum(guard(context) for guard in guards)

            with self.subTest(slope=slope):
                self.assertEqual(match_count, 1)

    def test_all_structure_and_price_combinations_are_exclusive(self) -> None:
        """
        함수 이름: test_all_structure_and_price_combinations_are_exclusive()
        기능: 구조 boolean과 가격 위치의 모든 조합에서 정확히 한 guard만 참인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        guards = (
            matches_sideways_regime,
            matches_weak_up_regime,
            matches_strong_up_fallback,
            matches_strong_up_regime,
            matches_weak_down_regime,
            matches_strong_down_fallback,
            matches_strong_down_regime,
        )
        slopes = (
            Decimal("-1"),
            Decimal("-0.30"),
            Decimal("-0.20"),
            Decimal("-0.15"),
            Decimal("0"),
            Decimal("0.15"),
            Decimal("0.20"),
            Decimal("0.30"),
            Decimal("1"),
        )

        for slope in slopes:
            for structure_bits in range(16):
                for current_price in (
                    Decimal("99"),
                    Decimal("100"),
                    Decimal("101"),
                ):
                    context = make_context(
                        ema9_slope=slope,
                        has_higher_high=bool(structure_bits & 1),
                        has_higher_low=bool(structure_bits & 2),
                        has_lower_high=bool(structure_bits & 4),
                        has_lower_low=bool(structure_bits & 8),
                        current_price=current_price,
                        live_ema9=Decimal("100"),
                    )
                    match_count = sum(guard(context) for guard in guards)

                    with self.subTest(
                        slope=slope,
                        structure_bits=structure_bits,
                        current_price=current_price,
                    ):
                        self.assertEqual(match_count, 1)

    def test_strong_up_requires_structure_and_price_position(self) -> None:
        """
        함수 이름: test_strong_up_requires_structure_and_price_position()
        기능: TYPE 2가 HH, HL 및 EMA9 이상 가격을 모두 요구하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        matching_context = make_context(
            ema9_slope=Decimal("0.30"),
            has_higher_high=True,
            has_higher_low=True,
            current_price=Decimal("100"),
            live_ema9=Decimal("100"),
        )
        missing_structure_context = make_context(
            ema9_slope=Decimal("0.30"),
            has_higher_high=True,
            has_higher_low=False,
        )

        self.assertTrue(matches_strong_up_regime(matching_context))
        self.assertTrue(
            matches_strong_up_fallback(missing_structure_context)
        )

    def test_strong_down_fallback_covers_missing_structure(self) -> None:
        """
        함수 이름: test_strong_down_fallback_covers_missing_structure()
        기능: EA-007이 LH와 LL 미충족 강하락 입력까지 처리하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        fallback_context = make_context(
            ema9_slope=Decimal("-0.30"),
            has_lower_high=False,
            has_lower_low=False,
            current_price=Decimal("90"),
            live_ema9=Decimal("100"),
        )
        matching_context = make_context(
            ema9_slope=Decimal("-0.30"),
            has_lower_high=True,
            has_lower_low=True,
            current_price=Decimal("100"),
            live_ema9=Decimal("100"),
        )

        self.assertTrue(matches_strong_down_fallback(fallback_context))
        self.assertFalse(matches_strong_down_regime(fallback_context))
        self.assertTrue(matches_strong_down_regime(matching_context))


if __name__ == "__main__":
    unittest.main()
