"""Live credential namespace와 승인된 저액 위험 정책의 불변 설정 계약이다."""

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from binance_auto_trader.domain.trading import DailyLossScope, ManualKillBehavior, RiskPolicy

LIVE_CREDENTIAL_NAMESPACE = "com.binance-auto.trader.live"
LIVE_NOTIONAL_CAP = Decimal("10")
LIVE_POLICY_VERSION = 1


class LiveConfigurationError(ValueError):
    """
    클래스 이름: LiveConfigurationError
    기능: live 설정·권한·정책 불일치를 secret 없는 오류로 차단한다.
    작성 날짜: 2026/09/08
    """

    code = "LIVE_CONFIGURATION_INVALID"


def create_live_risk_policy() -> RiskPolicy:
    """
    함수 이름: create_live_risk_policy()
    기능: Session 7의 고정 order·position cap과 기존 손실·kill 동작을 조립한다.
    인자: 없음
    반환값: version 1 저액 live RiskPolicy
    작성 날짜: 2026/09/08
    """
    # Controller와 REST permission이 같은 값으로 대조할 immutable 정책을 생성한다.
    return RiskPolicy(
        version=LIVE_POLICY_VERSION,
        max_order_notional=LIVE_NOTIONAL_CAP,
        max_position_notional=LIVE_NOTIONAL_CAP,
        max_daily_loss=None,
        daily_loss_scope=DailyLossScope.REALIZED_ONLY,
        manual_kill_behavior=ManualKillBehavior.CANCEL_AND_LIQUIDATE,
    )  # Daily loss는 계산·게시만 하고 신규 차단 한도를 만들지 않는다.


@dataclass(frozen=True, slots=True)
class LiveConfiguration:
    """
    클래스 이름: LiveConfiguration
    기능: native 확인과 별도 namespace credential만으로 live read/order 권한을 표현한다.
    작성 날짜: 2026/09/08
    """

    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    credential_namespace: str = LIVE_CREDENTIAL_NAMESPACE
    confirmation: str | None = None
    enabled: bool = False
    allow_live_orders: bool = False
    max_notional: Decimal | None = None
    policy_version: int = LIVE_POLICY_VERSION

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: exact scalar·namespace·명시 확인·cap을 network 객체 생성 전에 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        # Testnet credential label과 truthy 값은 live 설정으로 승격하지 않는다.
        if self.credential_namespace != LIVE_CREDENTIAL_NAMESPACE:
            raise LiveConfigurationError("live credential namespace required")
        if type(self.enabled) is not bool or type(self.allow_live_orders) is not bool:
            raise LiveConfigurationError("live flags must be exact booleans")
        if type(self.policy_version) is not int or self.policy_version != LIVE_POLICY_VERSION:
            raise LiveConfigurationError("live policy version mismatch")
        for credential in (self.api_key, self.api_secret):
            if not isinstance(credential, str) or not 1 <= len(credential) <= 512 or any(
                not 33 <= ord(character) <= 126 for character in credential
            ):
                raise LiveConfigurationError("live credential unavailable")

        # Read-only와 disabled는 cap을 들고 주문 권한을 암묵적으로 얻을 수 없다.
        if self.enabled and self.confirmation != "LIVE":
            raise LiveConfigurationError("exact LIVE confirmation required")
        if self.allow_live_orders:
            if not self.enabled or type(self.max_notional) is not Decimal or self.max_notional != LIVE_NOTIONAL_CAP:
                raise LiveConfigurationError("live orders require enabled profile and exact cap 10")
        elif self.max_notional is not None:
            raise LiveConfigurationError("read-only live profile must not carry order permission cap")


def validate_live_history_path(history_path: str | Path) -> Path:
    """
    함수 이름: validate_live_history_path()
    기능: live 전용 저장 위치와 symlink 없는 ancestor를 확인해 Testnet fallback을 차단한다.
    인자: history_path -> live namespace 아래 trade-history.jsonl 절대 경로
    반환값: 검증된 Path
    작성 날짜: 2026/09/08
    """
    # History가 만드는 pending·manual-kill·owner sidecar도 같은 격리 directory를 사용한다.
    selected_path = Path(history_path)
    if (
        not selected_path.is_absolute()
        or ".." in selected_path.parts
        or selected_path.name != "trade-history.jsonl"
        or selected_path.parent.name != LIVE_CREDENTIAL_NAMESPACE
    ):
        raise LiveConfigurationError("live history namespace required")
    for component in (selected_path, *selected_path.parents):
        if component.is_symlink():
            raise LiveConfigurationError("live storage must not use symlinks")
    if selected_path.exists() and selected_path.stat().st_nlink != 1:
        raise LiveConfigurationError("live history must not be hard-linked")
    return selected_path  # 존재하지 않는 live 파일을 Testnet 이력으로 대체하지 않는다.
