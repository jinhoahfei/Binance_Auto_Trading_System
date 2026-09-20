"""Phase 13 신규 BUY에 적용할 불변 위험 정책과 순수 판정 함수를 정의한다."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from enum import Enum
from typing import Final


# 모든 위험 금액 검증과 손실 계산은 float가 섞이지 않은 같은 Decimal 영점을 사용한다.
ZERO_DECIMAL: Final[Decimal] = Decimal("0")
DECIMAL128_PRECISION: Final[int] = 34  # ADR-004의 금융 계산 유효 자릿수를 전역 context와 분리한다.


class RiskPolicyAvailability(str, Enum):
    """
    클래스 이름: RiskPolicyAvailability
    기능: 신규 BUY 평가에 사용할 위험 정책의 명시적 설정 가능 여부를 정의한다.
    작성 날짜: 2026/08/24
    """

    CONFIGURED = "CONFIGURED"
    UNAVAILABLE = "UNAVAILABLE"


class DailyLossScope(str, Enum):
    """
    클래스 이름: DailyLossScope
    기능: KST daily loss에 실현손익만 또는 미실현손익까지 포함할지 정의한다.
    작성 날짜: 2026/08/24
    """

    REALIZED_ONLY = "REALIZED_ONLY"
    REALIZED_AND_UNREALIZED = "REALIZED_AND_UNREALIZED"


class ManualKillBehavior(str, Enum):
    """
    클래스 이름: ManualKillBehavior
    기능: manual kill 활성화 뒤 운영자가 선택할 신규 주문 차단과 안전 정리 정책을 정의한다.
    작성 날짜: 2026/08/24
    """

    BLOCK_NEW_ORDERS = "BLOCK_NEW_ORDERS"
    CANCEL_AND_LIQUIDATE = "CANCEL_AND_LIQUIDATE"


class RiskBlockReason(str, Enum):
    """
    클래스 이름: RiskBlockReason
    기능: 신규 BUY 위험 판정이 fail closed된 첫 번째 typed 사유를 정의한다.
    작성 날짜: 2026/08/24
    """

    RISK_POLICY_UNAVAILABLE = "RISK_POLICY_UNAVAILABLE"
    RISK_POLICY_VERSION_MISMATCH = "RISK_POLICY_VERSION_MISMATCH"
    MANUAL_KILL_SWITCH_ACTIVE = "MANUAL_KILL_SWITCH_ACTIVE"
    RISK_ORDER_NOTIONAL_EXCEEDED = "RISK_ORDER_NOTIONAL_EXCEEDED"
    RISK_DAILY_LOSS_EXCEEDED = "RISK_DAILY_LOSS_EXCEEDED"
    RISK_POSITION_NOTIONAL_EXCEEDED = "RISK_POSITION_NOTIONAL_EXCEEDED"
    RISK_BUY_BUDGET_INSUFFICIENT = "RISK_BUY_BUDGET_INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class RiskPolicyUnavailable:
    """
    클래스 이름: RiskPolicyUnavailable
    기능: configured None 상한과 구분해 위험 정책 자체의 미설정을 명시적으로 표현한다.
    작성 날짜: 2026/08/24
    """

    availability: RiskPolicyAvailability = field(
        default=RiskPolicyAvailability.UNAVAILABLE,
        init=False,
    )


@dataclass(frozen=True, slots=True)
class ManualKillControlState:
    """
    클래스 이름: ManualKillControlState
    기능: 재시작에도 보존할 manual kill 상태, version과 한 성공 command의 provenance를 묶는다.
    작성 날짜: 2026/08/29
    """

    active: bool = False
    version: int = 0
    command_id: str | None = None
    expected_version: int | None = None
    behavior: ManualKillBehavior | None = None
    policy_version: int | None = None

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: manual kill 상태·version과 재시작 멱등 command/policy provenance를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # Python bool이 int 하위 타입인 점을 이용해 control version으로 들어오지 못하게 각각 검증한다.
        if type(self.active) is not bool:
            raise TypeError("active must be a bool")
        _require_exact_version("version", self.version, minimum=0)

        # Command가 없는 값만 파일 부재를 나타내는 canonical inactive version 0 상태다.
        if self.command_id is None:
            if self.version != 0 or self.active:
                raise ValueError(
                    "state without command provenance must be inactive version zero"
                )
            if any(
                value is not None
                for value in (
                    self.expected_version,
                    self.behavior,
                    self.policy_version,
                )
            ):
                raise ValueError(
                    "initial manual-kill state cannot contain partial provenance"
                )
            return

        # 모든 성공 command는 restart 뒤 payload 재사용을 판정할 exact ID와 expected version을 보존한다.
        if not isinstance(self.command_id, str):
            raise TypeError("command_id must be a string")
        if not self.command_id or self.command_id != self.command_id.strip():
            raise ValueError("command_id must be a non-empty trimmed string")
        if self.expected_version is None:
            raise TypeError("expected_version must be an integer")
        _require_exact_version(
            "expected_version",
            self.expected_version,
            minimum=0,
        )
        if self.expected_version not in {self.version, self.version - 1}:
            raise ValueError(
                "expected_version must identify a no-op or one-version toggle"
            )

        # Configured policy의 behavior/version은 함께 있고 unavailable 상태는 둘 다 없어야 한다.
        if (self.behavior is None) is not (self.policy_version is None):
            raise ValueError(
                "behavior and policy_version must both be set or both be absent"
            )
        if self.behavior is not None and not isinstance(
            self.behavior,
            ManualKillBehavior,
        ):
            raise TypeError("behavior must be a ManualKillBehavior or None")
        if self.policy_version is not None:
            _require_exact_version(
                "policy_version",
                self.policy_version,
                minimum=1,
            )


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    """
    클래스 이름: RiskPolicy
    기능: 단건·누적 position·KST daily loss의 유한 또는 명시적 무제한과 운영 동작을 묶는다.
    작성 날짜: 2026/08/24
    """

    version: int
    max_order_notional: Decimal | None
    max_position_notional: Decimal | None
    max_daily_loss: Decimal | None
    daily_loss_scope: DailyLossScope
    manual_kill_behavior: ManualKillBehavior
    availability: RiskPolicyAvailability = field(
        default=RiskPolicyAvailability.CONFIGURED,
        init=False,
    )

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: configured 정책의 version, 유한 또는 명시적 무제한 상한과 enum 조합을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # 승인 정책 version은 bool이나 실수로 우회할 수 없는 1 이상의 정확한 int여야 한다.
        _require_exact_version("version", self.version, minimum=1)

        # None은 configured 무제한으로 보존하고 값이 있는 금융 상한만 양의 유한 Decimal로 검증한다.
        for field_name, value in (
            ("max_order_notional", self.max_order_notional),
            ("max_position_notional", self.max_position_notional),
            ("max_daily_loss", self.max_daily_loss),
        ):
            if value is not None:
                _require_finite_decimal(field_name, value, positive=True)

        # 정책 선택지는 문자열 대신 문서에 확정된 enum identity로만 받는다.
        if not isinstance(self.daily_loss_scope, DailyLossScope):
            raise TypeError("daily_loss_scope must be a DailyLossScope")
        if not isinstance(self.manual_kill_behavior, ManualKillBehavior):
            raise TypeError(
                "manual_kill_behavior must be a ManualKillBehavior"
            )


@dataclass(frozen=True, slots=True)
class RiskBudgetSnapshot:
    """
    클래스 이름: RiskBudgetSnapshot
    기능: 한 BUY 판단의 source version, 현재·예약·후보 노출과 KST 손실 근거를 불변 보존한다.
    작성 날짜: 2026/08/24
    """

    policy_version: int | None
    market_version: int
    account_version: int
    context_version: int
    current_position_notional: Decimal
    reserved_buy_notional: Decimal
    candidate_order_notional: Decimal
    projected_position_notional: Decimal
    daily_realized_pnl: Decimal
    unrealized_pnl: Decimal
    daily_loss: Decimal
    manual_kill_active: bool
    # Legacy 판정에는 관측하지 않은 값을 0으로 합성하지 않는다.
    evaluated_at: datetime | None = None
    strategy_position_notional: Decimal | None = None
    residual_position_notional: Decimal | None = None
    remaining_position_notional: Decimal | None = None
    requested_order_notional: Decimal | None = None
    max_position_notional: Decimal | None = None

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: source version, 유한 Decimal, 노출 합계와 manual kill 타입을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # unavailable snapshot만 policy version이 없을 수 있고 명시된 policy version은 1 이상이어야 한다.
        if self.policy_version is not None:
            _require_exact_version(
                "policy_version",
                self.policy_version,
                minimum=1,
            )

        # 세 source version은 초기 snapshot의 0을 허용하되 bool과 음수 version은 거부한다.
        for field_name, value in (
            ("market_version", self.market_version),
            ("account_version", self.account_version),
            ("context_version", self.context_version),
        ):
            _require_exact_version(field_name, value, minimum=0)

        # 현재·예약·후보·예상 노출과 손실 크기는 음수가 아닌 유한 Decimal로 고정한다.
        for field_name, value in (
            ("current_position_notional", self.current_position_notional),
            ("reserved_buy_notional", self.reserved_buy_notional),
            ("candidate_order_notional", self.candidate_order_notional),
            ("projected_position_notional", self.projected_position_notional),
            ("daily_loss", self.daily_loss),
        ):
            _require_finite_decimal(field_name, value, non_negative=True)

        # 실현·미실현 PnL은 이익과 손실을 모두 표현해야 하므로 부호 없이 유한성만 검증한다.
        _require_finite_decimal("daily_realized_pnl", self.daily_realized_pnl)
        _require_finite_decimal("unrealized_pnl", self.unrealized_pnl)

        # 후보 BUY 뒤 누적 노출은 현재 Position, active 예약과 후보 금액의 정확한 합이어야 한다.
        with localcontext() as decimal_context:
            decimal_context.prec = DECIMAL128_PRECISION
            decimal_context.rounding = ROUND_HALF_EVEN
            expected_projected_notional = (
                self.current_position_notional
                + self.reserved_buy_notional
                + self.candidate_order_notional
            )
        if self.projected_position_notional != expected_projected_notional:
            raise ValueError(
                "projected_position_notional must equal current, reserved, "
                "and candidate notionals"
            )

        if self.evaluated_at is not None and (
            not isinstance(self.evaluated_at, datetime)
            or self.evaluated_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("evaluated_at must be timezone-aware UTC")
        for field_name in (
            "strategy_position_notional", "residual_position_notional",
            "remaining_position_notional", "requested_order_notional", "max_position_notional",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _require_finite_decimal(field_name, value, non_negative=True)
        if (self.strategy_position_notional is None) != (self.residual_position_notional is None):
            raise ValueError("position breakdown must be supplied together")
        with localcontext() as decimal_context:
            decimal_context.prec = DECIMAL128_PRECISION
            decimal_context.rounding = ROUND_HALF_EVEN
            if self.strategy_position_notional is not None and (
                self.strategy_position_notional + self.residual_position_notional
                != self.current_position_notional
            ):
                raise ValueError("position breakdown must equal current position notional")
            if self.remaining_position_notional is not None and (
                self.max_position_notional is None
                or self.remaining_position_notional != max(
                    ZERO_DECIMAL, self.max_position_notional
                    - self.current_position_notional - self.reserved_buy_notional,
                )
            ):
                raise ValueError("remaining position budget is inconsistent")

        if type(self.manual_kill_active) is not bool:
            raise TypeError("manual_kill_active must be a bool")


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """
    클래스 이름: RiskDecision
    기능: 신규 BUY 허용 여부, 첫 차단 사유와 그 판단에 사용한 위험 예산을 불변 보존한다.
    작성 날짜: 2026/08/24
    """

    allowed: bool
    budget: RiskBudgetSnapshot
    block_reason: RiskBlockReason | None = None

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 허용 여부, typed 차단 사유와 위험 예산의 일관된 조합을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # 외부 truthy 값이 허용 판정을 가장하지 못하도록 정확한 bool과 snapshot만 받는다.
        if type(self.allowed) is not bool:
            raise TypeError("allowed must be a bool")
        if not isinstance(self.budget, RiskBudgetSnapshot):
            raise TypeError("budget must be a RiskBudgetSnapshot")

        # 허용에는 차단 사유가 없어야 하고 차단에는 반드시 하나의 typed 사유가 있어야 한다.
        if self.allowed and self.block_reason is not None:
            raise ValueError("allowed risk decision cannot have a block reason")
        if not self.allowed and not isinstance(
            self.block_reason,
            RiskBlockReason,
        ):
            raise ValueError("blocked risk decision requires a RiskBlockReason")


def evaluate_buy_risk(
    policy_state: RiskPolicy | RiskPolicyUnavailable,
    budget: RiskBudgetSnapshot,
) -> RiskDecision:
    """
    함수 이름: evaluate_buy_risk()
    기능: 불변 정책과 예산을 availability부터 누적 position까지 문서 순서대로 순수 평가한다.
    인자: policy_state -> configured RiskPolicy 또는 명시적 RiskPolicyUnavailable
        budget -> 같은 application lock에서 캡처하고 계산한 RiskBudgetSnapshot
    반환값: 허용 여부와 첫 typed 차단 사유를 가진 RiskDecision
    작성 날짜: 2026/08/24
    """
    # 도메인 함수가 duck typing 객체나 mutable mapping을 정책·예산으로 신뢰하지 않게 한다.
    if not isinstance(policy_state, (RiskPolicy, RiskPolicyUnavailable)):
        raise TypeError(
            "policy_state must be a RiskPolicy or RiskPolicyUnavailable"
        )
    if not isinstance(budget, RiskBudgetSnapshot):
        raise TypeError("budget must be a RiskBudgetSnapshot")

    # 1순위 availability와 version 일치는 다른 숫자나 manual kill 판정보다 먼저 fail closed한다.
    if policy_state.availability is RiskPolicyAvailability.UNAVAILABLE:
        return _blocked_decision(
            budget,
            RiskBlockReason.RISK_POLICY_UNAVAILABLE,
        )
    if budget.policy_version != policy_state.version:
        return _blocked_decision(
            budget,
            RiskBlockReason.RISK_POLICY_VERSION_MISMATCH,
        )

    # 2순위 manual kill은 configured 정책의 세부 행동과 무관하게 모든 신규 BUY를 막는다.
    if budget.manual_kill_active:
        return _blocked_decision(
            budget,
            RiskBlockReason.MANUAL_KILL_SWITCH_ACTIVE,
        )

    # 3순위 단건 상한은 configured 유한값이 있고 filter 뒤 후보 notional이 초과할 때만 차단한다.
    if (
        policy_state.max_order_notional is not None
        and budget.candidate_order_notional
        > policy_state.max_order_notional
    ):
        return _blocked_decision(
            budget,
            RiskBlockReason.RISK_ORDER_NOTIONAL_EXCEEDED,
        )

    # 4순위 직전에 snapshot daily loss가 configured scope의 ADR-006 산식과 같은지 검증한다.
    expected_daily_loss = _calculate_daily_loss(
        policy_state.daily_loss_scope,
        budget.daily_realized_pnl,
        budget.unrealized_pnl,
    )
    if budget.daily_loss != expected_daily_loss:
        raise ValueError(
            "daily_loss must match the configured daily_loss_scope"
        )

    # 4순위 daily loss는 configured 유한값이 있을 때 상한에 도달한 순간부터 신규 BUY를 차단한다.
    if (
        policy_state.max_daily_loss is not None
        and budget.daily_loss >= policy_state.max_daily_loss
    ):
        return _blocked_decision(
            budget,
            RiskBlockReason.RISK_DAILY_LOSS_EXCEEDED,
        )

    # 5순위 projected position은 유한 누적 상한이 있고 현재·예약·후보 합계가 넘을 때 차단한다.
    if (
        policy_state.max_position_notional is not None
        and budget.projected_position_notional
        > policy_state.max_position_notional
    ):
        return _blocked_decision(
            budget,
            RiskBlockReason.RISK_POSITION_NOTIONAL_EXCEEDED,
        )

    return RiskDecision(allowed=True, budget=budget)  # 모든 BUY 위험 gate를 순서대로 통과했다.


def _calculate_daily_loss(
    scope: DailyLossScope,
    daily_realized_pnl: Decimal,
    unrealized_pnl: Decimal,
) -> Decimal:
    """
    함수 이름: _calculate_daily_loss()
    기능: configured 범위의 KST PnL 합계에서 음수 부분의 절댓값만 daily loss로 계산한다.
    인자: scope -> 실현손익 또는 실현·미실현손익 범위
        daily_realized_pnl -> KST 거래일의 durable SELL 실현손익
        unrealized_pnl -> 현재 Position의 mark-to-market 미실현손익
    반환값: 0 이상의 daily loss Decimal
    작성 날짜: 2026/08/24
    """
    # 실현 전용 정책은 미실현 변동을 제외하고 결합 정책만 Decimal128 context에서 합산한다.
    with localcontext() as decimal_context:
        decimal_context.prec = DECIMAL128_PRECISION
        decimal_context.rounding = ROUND_HALF_EVEN
        scoped_pnl = daily_realized_pnl
        if scope is DailyLossScope.REALIZED_AND_UNREALIZED:
            scoped_pnl += unrealized_pnl

        return max(
            -scoped_pnl,
            ZERO_DECIMAL,
        )  # 이익 또는 0인 거래일은 daily loss를 0으로 고정한다.


def _blocked_decision(
    budget: RiskBudgetSnapshot,
    block_reason: RiskBlockReason,
) -> RiskDecision:
    """
    함수 이름: _blocked_decision()
    기능: 같은 위험 예산과 첫 typed 사유를 보존한 차단 결정을 생성한다.
    인자: budget -> 판정에 사용한 RiskBudgetSnapshot
        block_reason -> 순서상 가장 먼저 충족된 RiskBlockReason
    반환값: allowed가 False인 RiskDecision
    작성 날짜: 2026/08/24
    """
    return RiskDecision(
        allowed=False,
        budget=budget,
        block_reason=block_reason,
    )  # 차단 경로도 허용 경로와 동일한 authoritative snapshot identity를 보존한다.


def _require_exact_version(
    field_name: str,
    value: object,
    *,
    minimum: int,
) -> None:
    """
    함수 이름: _require_exact_version()
    기능: version 필드가 bool이 아닌 exact int이고 지정한 최솟값 이상인지 검증한다.
    인자: field_name -> 오류에 표시할 version 필드 이름
        value -> 검증할 외부 값
        minimum -> 허용할 가장 작은 정수
    반환값: 없음
    작성 날짜: 2026/08/24
    """
    # bool이 정수 version으로 통과하지 못하게 exact type과 하한을 순서대로 검증한다.
    if type(value) is not int:
        raise TypeError(f"{field_name} must be an exact int")
    if value < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}")


def _require_finite_decimal(
    field_name: str,
    value: object,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> None:
    """
    함수 이름: _require_finite_decimal()
    기능: 금융값의 Decimal 타입, 유한성과 선택한 양수 범위를 검증한다.
    인자: field_name -> 오류에 표시할 금융 필드 이름
        value -> 검증할 외부 값
        positive -> 0보다 큰 값만 허용할지 여부
        non_negative -> 0 이상의 값만 허용할지 여부
    반환값: 없음
    작성 날짜: 2026/08/24
    """
    # int와 float를 자동 변환하지 않아 모든 금융 계산이 처음부터 Decimal로 유지되게 한다.
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be a Decimal")
    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite")

    # 호출자가 요구한 양수 범위를 구분해 policy 상한과 snapshot 금액에 각각 적용한다.
    if positive and value <= ZERO_DECIMAL:
        raise ValueError(f"{field_name} must be positive")
    if non_negative and value < ZERO_DECIMAL:
        raise ValueError(f"{field_name} must not be negative")
