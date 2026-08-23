"""KST inclusive date와 거래 방향을 결합하는 history query를 정의한다."""

from dataclasses import dataclass
from datetime import date
from enum import Enum


class HistoryPeriod(str, Enum):
    """
    클래스 이름: HistoryPeriod
    기능: 거래 상세 조회의 오늘·최근 7일·최근 30일·전체 기간 preset을 구분한다.
    작성 날짜: 2026/08/23
    """

    TODAY = "TODAY"
    LAST_7_DAYS = "LAST_7_DAYS"
    LAST_30_DAYS = "LAST_30_DAYS"
    ALL = "ALL"


class TradeSide(str, Enum):
    """
    클래스 이름: TradeSide
    기능: 거래 이력 조회에서 전체·매수·매도 방향을 구분한다.
    작성 날짜: 2026/08/21
    """

    ALL = "ALL"
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True, slots=True)
class TradeHistoryQuery:
    """
    클래스 이름: TradeHistoryQuery
    기능: KST 기준 양끝 포함 날짜 범위와 거래 방향을 불변으로 보존한다.
    작성 날짜: 2026/08/21
    """

    start_date: date
    end_date: date
    side: TradeSide = TradeSide.ALL

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 날짜와 canonical side 타입 및 범위 순서를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        if type(self.start_date) is not date:
            raise TypeError("start_date must be a date")
        if type(self.end_date) is not date:
            raise TypeError("end_date must be a date")
        if not isinstance(self.side, TradeSide):
            raise TypeError("side must be the canonical TradeSide")
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
