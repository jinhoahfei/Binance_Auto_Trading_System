"""외부 주문이 없는 통합 테스트에만 사용할 명시적 Phase 13 위험 정책 fixture를 정의한다."""

from decimal import Decimal

from binance_auto_trader.domain.trading import (
    DailyLossScope,
    ManualKillBehavior,
    RiskPolicy,
)


def create_test_risk_policy() -> RiskPolicy:
    """
    함수 이름: create_test_risk_policy()
    기능: production 기본값이 아닌 deterministic fake 주문 테스트 전용 위험 정책을 생성한다.
    인자: 없음
    반환값: 네트워크 mutation 없는 통합 fixture가 명시적으로 주입할 RiskPolicy
    작성 날짜: 2026/08/24
    """
    # 큰 유한 상한은 기존 fake scenario를 방해하지 않되 production 구성으로 재사용되지 않는다.
    return RiskPolicy(
        version=1,
        max_order_notional=Decimal("1000000"),
        max_position_notional=Decimal("2000000"),
        max_daily_loss=Decimal("1000000"),
        daily_loss_scope=DailyLossScope.REALIZED_AND_UNREALIZED,
        manual_kill_behavior=ManualKillBehavior.BLOCK_NEW_ORDERS,
    )
