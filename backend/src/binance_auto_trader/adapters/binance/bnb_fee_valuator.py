"""공식 public kline에서 BNB 수수료 평가 근거만 읽는 adapter다."""

from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from binance_auto_trader.domain.trading.fee_valuation import BnbFeeValuation


class BnbFeeValuator:
    """
    클래스 이름: BnbFeeValuator
    기능: 정확한 과거 1초 구간의 체결 있는 종가를 검증해 평가 근거를 제공한다.
    작성 날짜: 2026/09/09
    """

    def __init__(self, request_json: Callable[..., object]) -> None:
        """
        함수 이름: __init__()
        기능: fixed live REST의 GET 요청 함수를 결속한다.
        인자: request_json -> 기존 bounded REST 요청 함수
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        if not callable(request_json):
            raise TypeError("request_json must be callable")
        self._request_json = request_json  # Credential·endpoint·retry 정책은 기존 adapter가 소유한다.

    def resolve(self, executed_at: datetime) -> BnbFeeValuation:
        """
        함수 이름: resolve()
        기능: 체결 직전 완료된 1초봉을 읽고 미래·빈 구간·잘못된 payload를 거부한다.
        인자: executed_at -> 거래소가 제공한 체결 시각
        반환값: 검증된 BnbFeeValuation
        작성 날짜: 2026/09/09
        """
        if not isinstance(executed_at, datetime) or executed_at.utcoffset() is None:
            raise ValueError("fill time must be aware")
        elapsed = executed_at.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
        open_time = (elapsed.days * 86400 + elapsed.seconds) * 1000 - 1000

        # 현재 ticker나 미래 가격으로 fallback하지 않고 완료된 구간 하나만 조회한다.
        response = self._request_json(method="GET", endpoint="/v3/klines", parameters={
            "symbol": "BNBUSDT", "interval": "1s", "startTime": open_time,
            "endTime": open_time + 999, "limit": 1,
        }, signed=False)
        rows = response.payload
        if not isinstance(rows, list) or len(rows) != 1:
            raise ValueError("BNB valuation candle unavailable")
        row = rows[0]
        if not isinstance(row, list) or len(row) != 12:
            raise ValueError("invalid BNB valuation kline")
        if type(row[0]) is not int or type(row[6]) is not int or row[0] != open_time or row[6] != open_time + 999:
            raise ValueError("BNB valuation window mismatch")
        if type(row[8]) is not int or row[8] <= 0:
            raise ValueError("BNB valuation requires actual market trades")
        if not isinstance(row[4], str):
            raise TypeError("BNB rate must be a decimal string")
        return BnbFeeValuation(row[0], row[6], Decimal(row[4]))  # Domain도 환율과 시간 불변식을 검증한다.
