"""BNB 원 수수료와 구분되는 결정론적 USDT 평가 근거를 정의한다."""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN, localcontext


@dataclass(frozen=True, slots=True)
class BnbFeeValuation:
    """
    클래스 이름: BnbFeeValuation
    기능: 체결 직전 완료된 공식 BNBUSDT 1초봉 종가와 평가 정책을 보존한다.
    작성 날짜: 2026/09/09
    """

    open_time_ms: int
    close_time_ms: int
    rate: Decimal
    source: str = "BINANCE_BNBUSDT_PREVIOUS_1S_CLOSE_V1"

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 고정 평가 정책·정수 UTC 구간·양수 환율을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        # Source를 임의 환율이나 Binance 내부 정산 환율로 대체하지 못하게 한다.
        if self.source != "BINANCE_BNBUSDT_PREVIOUS_1S_CLOSE_V1":
            raise ValueError("unsupported BNB valuation source")
        if type(self.open_time_ms) is not int or type(self.close_time_ms) is not int:
            raise TypeError("valuation timestamps must be integers")
        if self.open_time_ms < 0 or self.open_time_ms % 1000 or self.close_time_ms != self.open_time_ms + 999:
            raise ValueError("valuation requires one complete UTC second")
        if not isinstance(self.rate, Decimal) or not self.rate.is_finite() or self.rate <= 0:
            raise ValueError("valuation requires a positive finite Decimal rate")

    def quote_amount(self, amount: Decimal, executed_at: datetime) -> Decimal:
        """
        함수 이름: quote_amount()
        기능: 체결 바로 이전 완료 구간인지 검증하고 실제 BNB 수량을 USDT로 평가한다.
        인자: amount -> 실제 BNB 수수료, executed_at -> 실제 체결 UTC 시각
        반환값: Decimal128 USDT 평가액
        작성 날짜: 2026/09/09
        """
        if not isinstance(executed_at, datetime) or executed_at.utcoffset() is None:
            raise ValueError("valuation requires aware fill time")
        elapsed = executed_at.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
        second_ms = (elapsed.days * 86400 + elapsed.seconds) * 1000
        if self.close_time_ms != second_ms - 1:
            raise ValueError("valuation is not the immediately preceding second")
        if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
            raise ValueError("BNB commission must be nonnegative Decimal")

        # 환산액은 거래소에서 차감한 USDT가 아니라 명시된 시점의 평가 비용이다.
        with localcontext() as context:
            context.prec = 34
            context.rounding = ROUND_HALF_EVEN
            return amount * self.rate  # 원 BNB 수량은 Fill에 별도로 남는다.
