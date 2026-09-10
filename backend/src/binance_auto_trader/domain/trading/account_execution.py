"""외부 체결을 앱의 주문 의도와 구분해 복구하는 거래소 증거를 정의한다."""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from .order import OrderResult, OrderStatus, _aggregate_fills
from .states import OrderSide


@dataclass(frozen=True, slots=True)
class AccountExecution:
    """
    클래스 이름: AccountExecution
    기능: 거래소 주문 방향·원 요청량·생성 시각과 완전한 누적 체결을 결속한다.
    작성 날짜: 2026/09/10
    """

    side: OrderSide
    requested_quantity: Decimal
    created_at: datetime
    result: OrderResult

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 원 주문과 누적 체결의 식별자·시간·수량을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        if not isinstance(self.side, OrderSide) or not isinstance(self.result, OrderResult):
            raise ValueError("account execution requires canonical side and result")
        if not isinstance(self.requested_quantity, Decimal) or not self.requested_quantity.is_finite() or self.requested_quantity <= 0:
            raise ValueError("account execution requires positive original quantity")
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("account execution requires aware creation time")
        object.__setattr__(self, "created_at", self.created_at.astimezone(timezone.utc))
        fills = self.result.fills
        if not fills or len({fill.key for fill in fills}) != len(fills):
            raise ValueError("account execution requires distinct complete fills")
        quantity = _aggregate_fills(fills)[0]
        if self.result.status is OrderStatus.FILLED and quantity != self.requested_quantity:
            raise ValueError("FILLED account execution must explain all original quantity")
        if quantity > self.requested_quantity:
            raise ValueError("account execution exceeds original quantity")
        if any(fill.executed_at < self.created_at or fill.executed_at > self.result.processed_at for fill in fills):
            raise ValueError("account execution time is inconsistent")
