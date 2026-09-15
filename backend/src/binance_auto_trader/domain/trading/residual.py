"""매도 뒤 주문 단위 미만 ETH의 수량·원가와 체결 이력 결속을 정의한다."""

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json

from binance_auto_trader.domain.history.trade import Trade, trade_to_json_object


@dataclass(frozen=True, slots=True)
class EarnResidualEvidence:
    """
    클래스 이름: EarnResidualEvidence
    기능: 현물에서 자동 예치된 ETH 원금과 조회된 현재 보유 근거를 보존한다.
    작성 날짜: 2026/09/15
    """

    # purchase ID, UTC milliseconds, ETH amount. 원금은 매도 가능 잔고가 아니다.
    subscriptions: tuple[tuple[str, int, Decimal], ...]
    quantity: Decimal
    rewards: Decimal
    redemptions: tuple[tuple[str, int, Decimal], ...] = ()

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 중복 없는 예치와 현재 원금·보상 합계를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/15
        """
        from decimal import localcontext

        if not isinstance(self.subscriptions, tuple) or not self.subscriptions:
            raise ValueError("Earn subscriptions are required")
        if not isinstance(self.redemptions, tuple):
            raise ValueError("Earn redemptions must be immutable")
        # purchaseId와 redeemId는 별개 식별자 공간이며 각 이력 안에서만 중복을 거부한다.
        for entries in (self.subscriptions, self.redemptions):
            identities = set()
            for identity, timestamp, amount in entries:
                if not isinstance(identity, str) or not identity.isascii() or not identity.isdigit() or identity in identities:
                    raise ValueError("invalid Earn movement identity")
                identities.add(identity)
                if type(timestamp) is not int or timestamp <= 0 or not isinstance(amount, Decimal) or not amount.is_finite() or amount <= 0:
                    raise ValueError("invalid Earn movement")
        if any(not isinstance(value, Decimal) or not value.is_finite() or value < 0 for value in (self.quantity, self.rewards)):
            raise ValueError("invalid Earn balance")
        with localcontext() as context:
            context.prec = 34
            returned = sum((entry[2] for entry in self.redemptions), Decimal("0"))
            if self.quantity + returned != sum((entry[2] for entry in self.subscriptions), Decimal("0")) + self.rewards:
                raise ValueError("Earn balance does not match verified subscriptions and rewards")


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
