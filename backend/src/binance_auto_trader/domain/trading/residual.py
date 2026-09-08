"""매도 뒤 주문 단위 미만 ETH의 수량·원가와 체결 이력 결속을 정의한다."""

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json

from binance_auto_trader.domain.history.trade import Trade, trade_to_json_object


def history_digest(trades: tuple[Trade, ...]) -> str:
    """
    함수 이름: history_digest()
    기능: 잔여 이관 시점까지의 canonical 체결 원문을 순서 포함 SHA-256으로 결속한다.
    인자: trades -> 저장 완료한 체결 prefix
    반환값: hex SHA-256 문자열
    작성 날짜: 2026/09/08
    """
    payload = [trade_to_json_object(trade) for trade in trades]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()  # 원래 매수량·fee·매도량·원가도 함께 결속한다.


@dataclass(frozen=True, slots=True)
class ResidualTransfer:
    """
    클래스 이름: ResidualTransfer
    기능: 전략에서 분리했지만 여전히 보유하는 ETH와 미실현 원가를 불변으로 보존한다.
    작성 날짜: 2026/09/08
    """

    history_count: int
    history_sha256: str
    quantity: Decimal
    cost_basis: Decimal
    step_size: Decimal

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 이력 prefix와 주문 단위 미만 양수 잔여라는 정책 경계를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        if type(self.history_count) is not int or self.history_count < 2:
            raise ValueError("residual requires BUY and SELL history")
        if not isinstance(self.history_sha256, str) or len(self.history_sha256) != 64 or any(character not in "0123456789abcdef" for character in self.history_sha256):
            raise ValueError("invalid residual history digest")
        # Decimal만 허용해 소량 자산을 float 반올림으로 소실시키지 않는다.
        for value in (self.quantity, self.cost_basis, self.step_size):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError("residual values must be positive finite Decimal")
        if self.quantity >= self.step_size:
            raise ValueError("residual must be smaller than LOT_SIZE stepSize")
