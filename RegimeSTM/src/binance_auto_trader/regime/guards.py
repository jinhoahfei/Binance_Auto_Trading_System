"""4시간봉 REGIME 추천 Event-Action guard를 순수 함수로 정의한다."""

from decimal import Decimal

from .evaluation import RegimeEvaluationContext


SIDEWAYS_MIN_SLOPE = Decimal("-0.15")
SIDEWAYS_MAX_SLOPE = Decimal("0.15")
STRONG_DOWN_SLOPE = Decimal("-0.30")
STRONG_UP_SLOPE = Decimal("0.30")


def matches_sideways_regime(context: RegimeEvaluationContext) -> bool:
    """
    함수 이름: matches_sideways_regime()
    기능: EA-002의 횡보 기울기 구간이 성립하는지 판정한다.
    인자: context -> 동일 snapshot에서 만든 REGIME 평가 입력
    반환값: EA-002 guard 충족 여부
    작성 날짜: 2026/08/14
    """
    return (
        SIDEWAYS_MIN_SLOPE
        <= context.ema9_slope
        <= SIDEWAYS_MAX_SLOPE
    )


def matches_weak_up_regime(context: RegimeEvaluationContext) -> bool:
    """
    함수 이름: matches_weak_up_regime()
    기능: EA-003의 약상승 기울기 구간이 성립하는지 판정한다.
    인자: context -> 동일 snapshot에서 만든 REGIME 평가 입력
    반환값: EA-003 guard 충족 여부
    작성 날짜: 2026/08/14
    """
    return (
        SIDEWAYS_MAX_SLOPE
        < context.ema9_slope
        < STRONG_UP_SLOPE
    )


def matches_strong_up_fallback(context: RegimeEvaluationContext) -> bool:
    """
    함수 이름: matches_strong_up_fallback()
    기능: EA-004의 강상승 복합조건 실패 분기가 성립하는지 판정한다.
    인자: context -> 동일 snapshot에서 만든 REGIME 평가 입력
    반환값: EA-004 guard 충족 여부
    작성 날짜: 2026/08/14
    """
    strong_up_structure = (
        context.has_higher_high
        and context.has_higher_low
        and context.current_price >= context.live_ema9
    )

    return (
        context.ema9_slope >= STRONG_UP_SLOPE
        and not strong_up_structure
    )


def matches_strong_up_regime(context: RegimeEvaluationContext) -> bool:
    """
    함수 이름: matches_strong_up_regime()
    기능: EA-005의 강상승 slope, 구조 및 가격 조건을 판정한다.
    인자: context -> 동일 snapshot에서 만든 REGIME 평가 입력
    반환값: EA-005 guard 충족 여부
    작성 날짜: 2026/08/14
    """
    return (
        context.ema9_slope >= STRONG_UP_SLOPE
        and context.has_higher_high
        and context.has_higher_low
        and context.current_price >= context.live_ema9
    )


def matches_weak_down_regime(context: RegimeEvaluationContext) -> bool:
    """
    함수 이름: matches_weak_down_regime()
    기능: EA-006의 약하락 기울기 구간이 성립하는지 판정한다.
    인자: context -> 동일 snapshot에서 만든 REGIME 평가 입력
    반환값: EA-006 guard 충족 여부
    작성 날짜: 2026/08/14
    """
    return (
        STRONG_DOWN_SLOPE
        < context.ema9_slope
        < SIDEWAYS_MIN_SLOPE
    )


def matches_strong_down_fallback(
    context: RegimeEvaluationContext,
) -> bool:
    """
    함수 이름: matches_strong_down_fallback()
    기능: EA-007의 강하락 복합조건 전체 실패 분기를 판정한다.
    인자: context -> 동일 snapshot에서 만든 REGIME 평가 입력
    반환값: EA-007 guard 충족 여부
    작성 날짜: 2026/08/14
    """
    strong_down_structure = (
        context.has_lower_high
        and context.has_lower_low
        and context.current_price <= context.live_ema9
    )

    return (
        context.ema9_slope <= STRONG_DOWN_SLOPE
        and not strong_down_structure
    )


def matches_strong_down_regime(context: RegimeEvaluationContext) -> bool:
    """
    함수 이름: matches_strong_down_regime()
    기능: EA-008의 강하락 slope, 구조 및 가격 조건을 판정한다.
    인자: context -> 동일 snapshot에서 만든 REGIME 평가 입력
    반환값: EA-008 guard 충족 여부
    작성 날짜: 2026/08/14
    """
    return (
        context.ema9_slope <= STRONG_DOWN_SLOPE
        and context.has_lower_high
        and context.has_lower_low
        and context.current_price <= context.live_ema9
    )

