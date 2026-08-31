"""EMA9와 OLS slope의 interval-independent Decimal 계산을 정의한다."""

from collections.abc import Sequence
from decimal import Decimal, ROUND_HALF_EVEN, localcontext


# 4시간 REGIME과 30분 전략이 같은 EMA9·OLS 수학과 최종 정밀도를 공유한다.
EMA_PERIOD = 9
EMA_ALPHA = Decimal("0.2")
EMA_COMPLEMENT = Decimal("0.8")
SLOPE_SAMPLE_SIZE = 6
SLOPE_QUANTUM = Decimal("0.00000001")
DECIMAL_PRECISION = 34


def calculate_ema9_series(
    close_prices: Sequence[Decimal],
) -> tuple[Decimal, ...]:
    """
    함수 이름: calculate_ema9_series()
    기능: 첫 아홉 종가 평균 seed와 alpha 0.2로 interval-independent EMA9을 계산한다.
    인자: close_prices -> 시간순 확정봉 종가 Decimal sequence
    반환값: 아홉 번째 종가부터 시작하는 EMA9 tuple
    작성 날짜: 2026/08/29
    """
    if len(close_prices) < EMA_PERIOD:
        raise ValueError("at least nine close prices are required for EMA9")
    if any(
        not isinstance(close_price, Decimal)
        for close_price in close_prices
    ):
        raise TypeError("all close prices must be Decimal values")
    if any(
        not close_price.is_finite() or close_price <= Decimal("0")
        for close_price in close_prices
    ):
        raise ValueError("all close prices must be positive and finite")

    # 외부 process Decimal context와 무관하게 유효숫자 34에서 중간 quantize 없이 계산한다.
    with localcontext() as decimal_context:
        decimal_context.prec = DECIMAL_PRECISION
        decimal_context.rounding = ROUND_HALF_EVEN
        seed_total = sum(close_prices[:EMA_PERIOD], Decimal("0"))
        current_ema = seed_total / Decimal(EMA_PERIOD)
        ema_series = [current_ema]
        for close_price in close_prices[EMA_PERIOD:]:
            current_ema = (
                EMA_ALPHA * close_price
                + EMA_COMPLEMENT * current_ema
            )
            ema_series.append(current_ema)

    return tuple(ema_series)  # 확정 시계열을 호출자가 변경하지 못하게 tuple로 반환한다.


def calculate_raw_ols_slope(
    ema_values: Sequence[Decimal],
) -> Decimal:
    """
    함수 이름: calculate_raw_ols_slope()
    기능: 최근 여섯 EMA9에 x=0..5를 대응시킨 최소제곱 직선의 raw 기울기를 계산한다.
    인자: ema_values -> 시간순 EMA9 값 여섯 개
    반환값: 가격 단위/입력 봉인 OLS raw slope
    작성 날짜: 2026/08/29
    """
    if len(ema_values) != SLOPE_SAMPLE_SIZE:
        raise ValueError("exactly six EMA9 values are required for OLS slope")
    if any(not isinstance(ema_value, Decimal) for ema_value in ema_values):
        raise TypeError("all EMA9 values must be Decimal values")
    if any(not ema_value.is_finite() for ema_value in ema_values):
        raise ValueError("all EMA9 values must be finite")

    # 고정 x축과 y 평균을 모두 Decimal로 계산해 float 회귀 오차를 제거한다.
    with localcontext() as decimal_context:
        decimal_context.prec = DECIMAL_PRECISION
        decimal_context.rounding = ROUND_HALF_EVEN
        x_values = tuple(
            Decimal(index)
            for index in range(SLOPE_SAMPLE_SIZE)
        )
        x_mean = Decimal("2.5")
        y_mean = sum(ema_values, Decimal("0")) / Decimal(
            SLOPE_SAMPLE_SIZE
        )
        numerator = sum(
            (
                (x_value - x_mean) * (ema_value - y_mean)
                for x_value, ema_value in zip(x_values, ema_values)
            ),
            Decimal("0"),
        )
        denominator = sum(
            (
                (x_value - x_mean) ** 2
                for x_value in x_values
            ),
            Decimal("0"),
        )
        raw_ols_slope = numerator / denominator

    return raw_ols_slope  # x=0..5의 고정 denominator는 정확히 Decimal("17.5")다.


def normalize_ols_slope(
    raw_ols_slope: Decimal,
    candidate_price: Decimal,
) -> Decimal:
    """
    함수 이름: normalize_ols_slope()
    기능: raw OLS slope를 실제 대입 가격으로 나눠 percent/입력 봉 단위로 확정한다.
    인자: raw_ols_slope -> 가격 단위/입력 봉인 회귀 기울기
        candidate_price -> 해당 확정 또는 후보 EMA 계산에 실제 대입한 가격
    반환값: 소수점 여덟 자리 ROUND_HALF_EVEN 정규화 slope
    작성 날짜: 2026/08/29
    """
    if not isinstance(raw_ols_slope, Decimal):
        raise TypeError("raw_ols_slope must be a Decimal")
    if not isinstance(candidate_price, Decimal):
        raise TypeError("candidate_price must be a Decimal")
    if not raw_ols_slope.is_finite():
        raise ValueError("raw_ols_slope must be finite")
    if not candidate_price.is_finite() or candidate_price <= Decimal("0"):
        raise ValueError("candidate_price must be positive and finite")

    # 확정식에는 다른 기준가, 시간 환산 또는 연환산 계수를 추가하지 않는다.
    with localcontext() as decimal_context:
        decimal_context.prec = DECIMAL_PRECISION
        decimal_context.rounding = ROUND_HALF_EVEN
        normalized_slope = (
            raw_ols_slope / candidate_price * Decimal("100")
        )
        return normalized_slope.quantize(
            SLOPE_QUANTUM,
            rounding=ROUND_HALF_EVEN,
        )


def calculate_normalized_ols_slope(
    ema_values: Sequence[Decimal],
    candidate_price: Decimal,
) -> Decimal:
    """
    함수 이름: calculate_normalized_ols_slope()
    기능: 여섯 EMA9의 raw OLS와 candidate_price 정규화를 순서대로 계산한다.
    인자: ema_values -> 시간순 EMA9 값 여섯 개
        candidate_price -> 해당 EMA 계산에 실제 대입한 가격
    반환값: percent/입력 봉 단위의 소수점 여덟 자리 slope
    작성 날짜: 2026/08/29
    """
    raw_ols_slope = calculate_raw_ols_slope(ema_values)
    return normalize_ols_slope(raw_ols_slope, candidate_price)


def calculate_candidate_ema9_slope(
    committed_ema_series: Sequence[Decimal],
    candidate_price: Decimal,
) -> Decimal:
    """
    함수 이름: calculate_candidate_ema9_slope()
    기능: 마지막 확정 EMA에 후보 가격을 한 번 적용한 비누적 EMA9 slope를 계산한다.
    인자: committed_ema_series -> 확정봉만 반영한 EMA9 시계열
        candidate_price -> 현재가, tp_price 또는 더 짧은 확정봉 종가
    반환값: 후보 가격을 분모로 사용한 percent/입력 봉 slope
    작성 날짜: 2026/08/29
    """
    if len(committed_ema_series) < SLOPE_SAMPLE_SIZE - 1:
        raise ValueError(
            "five committed EMA9 values are required for a candidate slope"
        )
    if not isinstance(candidate_price, Decimal):
        raise TypeError("candidate_price must be a Decimal")
    if not candidate_price.is_finite() or candidate_price <= Decimal("0"):
        raise ValueError("candidate_price must be positive and finite")

    # 매 후보는 같은 마지막 확정 EMA에서 시작해 이전 realtime 후보를 절대 누적하지 않는다.
    with localcontext() as decimal_context:
        decimal_context.prec = DECIMAL_PRECISION
        decimal_context.rounding = ROUND_HALF_EVEN
        candidate_ema = (
            EMA_ALPHA * candidate_price
            + EMA_COMPLEMENT * committed_ema_series[-1]
        )
        candidate_values = (
            *committed_ema_series[-(SLOPE_SAMPLE_SIZE - 1):],
            candidate_ema,
        )
        return calculate_normalized_ols_slope(
            candidate_values,
            candidate_price,
        )
