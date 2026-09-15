"""공식 public kline에서 BNB 수수료 평가 근거만 읽는 adapter다."""

from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from time import sleep
from binance_auto_trader.domain.trading.fee_valuation import BnbFeeValuation


class BnbValuationUnavailableError(ValueError):
    """
    클래스 이름: BnbValuationUnavailableError
    기능: 공식 가격 봉을 아직 조회하지 못한 재시도 가능 오류를 구분한다.
    작성 날짜: 2026/09/16
    """

    code = "BNB_VALUATION_UNAVAILABLE"


class BnbValuationInvalidError(ValueError):
    """
    클래스 이름: BnbValuationInvalidError
    기능: 공식 가격의 형식과 시각 검증 실패를 구분한다.
    작성 날짜: 2026/09/16
    """

    code = "BNB_VALUATION_INVALID"


class BnbFeeValuator:
    """
    클래스 이름: BnbFeeValuator
    기능: 정확한 과거 1초 구간의 공식 종가를 검증해 평가 근거를 제공한다.
    작성 날짜: 2026/09/09
    """

    def __init__(self, request_json: Callable[..., object], *, wait: Callable[[int], None] = sleep) -> None:
        """
        함수 이름: __init__()
        기능: fixed live REST의 GET 요청 함수를 결속한다.
        인자: request_json -> 기존 bounded REST 요청 함수, wait -> 제한된 재조회 대기 함수
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        if not callable(request_json):
            raise TypeError("request_json must be callable")
        if not callable(wait):
            raise TypeError("wait must be callable")
        self._request_json = request_json  # Credential·endpoint·retry 정책은 기존 adapter가 소유한다.
        self._wait = wait  # Test는 실제 대기 없이 동일 재조회 계약을 검증한다.

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

        # 재조회에서도 원 체결의 직전 구간을 고정해 미래 가격이나 다른 봉을 사용하지 않는다.
        parameters = {
            "symbol": "BNBUSDT", "interval": "1s", "startTime": open_time,
            "endTime": open_time + 999, "limit": 1,
        }
        retry_delays = (1, 1, 2)
        for attempt in range(len(retry_delays) + 1):
            # HTTP 오류는 호출자에게 전파하고, 빈 응답만 같은 구간에서 재확인한다.
            response = self._request_json(
                method="GET", endpoint="/v3/klines", parameters=dict(parameters), signed=False,
            )
            rows = response.payload
            if not isinstance(rows, list) or len(rows) > 1:
                raise BnbValuationInvalidError("invalid BNB valuation kline")
            if not rows:
                failure_reason = "BNB valuation candle unavailable"
            else:
                row = rows[0]
                if not isinstance(row, list) or len(row) != 12:
                    raise BnbValuationInvalidError("invalid BNB valuation kline")
                if type(row[0]) is not int or type(row[6]) is not int or row[0] != open_time or row[6] != open_time + 999:
                    raise BnbValuationInvalidError("BNB valuation window mismatch")
                if type(row[8]) is not int or row[8] < 0:
                    raise BnbValuationInvalidError("invalid BNB valuation trade count")
                if not isinstance(row[4], str):
                    raise BnbValuationInvalidError("BNB rate must be a decimal string")

                try:
                    valuation = BnbFeeValuation(row[0], row[6], Decimal(row[4]))
                except (ValueError, ArithmeticError) as error:
                    raise BnbValuationInvalidError("invalid BNB valuation price") from error
                # 거래 0건도 해당 완료 구간의 공식 종가는 유효하다. 다른 시각 가격을 사용하지 않는다.
                return valuation

            # 빈 응답은 제한 뒤 typed 실패로 돌려주며 임의 환율로 대체하지 않는다.
            if attempt == len(retry_delays):
                raise BnbValuationUnavailableError(failure_reason)
            self._wait(retry_delays[attempt])
