"""Phase 13 위험 정책 value object와 신규 BUY 순수 gate를 검증한다."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal, localcontext

from binance_auto_trader.domain.trading import (
    DailyLossScope,
    ManualKillBehavior,
    ManualKillControlState,
    RiskBlockReason,
    RiskBudgetSnapshot,
    RiskDecision,
    RiskPolicy,
    RiskPolicyAvailability,
    RiskPolicyUnavailable,
    evaluate_buy_risk,
)


def create_policy(
    *,
    version: int = 7,
    max_order_notional: Decimal | None = Decimal("10"),
    max_position_notional: Decimal | None = Decimal("100"),
    max_daily_loss: Decimal | None = Decimal("8"),
    daily_loss_scope: DailyLossScope = (
        DailyLossScope.REALIZED_AND_UNREALIZED
    ),
    manual_kill_behavior: ManualKillBehavior = (
        ManualKillBehavior.BLOCK_NEW_ORDERS
    ),
) -> RiskPolicy:
    """
    함수 이름: create_policy()
    기능: 경계값을 개별 교체할 수 있는 configured 위험 정책 fixture를 만든다.
    인자: version -> configured policy version
        max_order_notional -> 단건 BUY notional 상한
        max_position_notional -> 누적 position notional 상한
        max_daily_loss -> KST daily loss 상한
        daily_loss_scope -> daily loss에 포함할 PnL 범위
        manual_kill_behavior -> manual kill 뒤 운영 동작 정책
    반환값: 검증을 통과한 RiskPolicy
    작성 날짜: 2026/08/24
    """
    return RiskPolicy(
        version=version,
        max_order_notional=max_order_notional,
        max_position_notional=max_position_notional,
        max_daily_loss=max_daily_loss,
        daily_loss_scope=daily_loss_scope,
        manual_kill_behavior=manual_kill_behavior,
    )  # 모든 테스트가 같은 명시적 승인값에서 필요한 경계만 바꾼다.


def create_budget(
    *,
    policy_version: int | None = 7,
    market_version: int = 11,
    account_version: int = 13,
    context_version: int = 17,
    current_position_notional: Decimal = Decimal("50"),
    reserved_buy_notional: Decimal = Decimal("20"),
    candidate_order_notional: Decimal = Decimal("10"),
    daily_realized_pnl: Decimal = Decimal("-3"),
    unrealized_pnl: Decimal = Decimal("-2"),
    daily_loss: Decimal = Decimal("5"),
    manual_kill_active: bool = False,
) -> RiskBudgetSnapshot:
    """
    함수 이름: create_budget()
    기능: projected notional을 세 노출의 합으로 계산한 위험 예산 fixture를 만든다.
    인자: policy_version -> session 또는 pending intent가 고정한 policy version
        market_version -> 현재가를 제공한 MarketSnapshot version
        account_version -> 잔액을 제공한 Account version
        context_version -> BUY 판단을 제공한 TradingContext version
        current_position_notional -> 현재 Position mark notional
        reserved_buy_notional -> active·partial·UNKNOWN BUY의 미체결 예약액
        candidate_order_notional -> filter 뒤 후보 BUY decision notional
        daily_realized_pnl -> KST 거래일 durable SELL 실현손익
        unrealized_pnl -> 현재 Position mark-to-market 미실현손익
        daily_loss -> configured 범위에서 계산한 손실 절댓값
        manual_kill_active -> 현재 manual kill 활성 여부
    반환값: 세 노출 합계와 source version을 가진 RiskBudgetSnapshot
    작성 날짜: 2026/08/24
    """
    # Caller가 바꾼 현재·예약·후보 값을 더해 항상 같은 판단 시점의 projected 노출을 만든다.
    projected_position_notional = (
        current_position_notional
        + reserved_buy_notional
        + candidate_order_notional
    )
    return RiskBudgetSnapshot(
        policy_version=policy_version,
        market_version=market_version,
        account_version=account_version,
        context_version=context_version,
        current_position_notional=current_position_notional,
        reserved_buy_notional=reserved_buy_notional,
        candidate_order_notional=candidate_order_notional,
        projected_position_notional=projected_position_notional,
        daily_realized_pnl=daily_realized_pnl,
        unrealized_pnl=unrealized_pnl,
        daily_loss=daily_loss,
        manual_kill_active=manual_kill_active,
    )  # 반환 snapshot은 mutable fixture mapping을 외부에 노출하지 않는다.


def create_budget_values() -> dict[str, object]:
    """
    함수 이름: create_budget_values()
    기능: 생성자 validation의 잘못된 타입과 비유한 값을 교체할 독립 필드 mapping을 만든다.
    인자: 없음
    반환값: 유효한 RiskBudgetSnapshot 생성자 필드 mapping
    작성 날짜: 2026/08/24
    """
    return {
        "policy_version": 7,
        "market_version": 11,
        "account_version": 13,
        "context_version": 17,
        "current_position_notional": Decimal("50"),
        "reserved_buy_notional": Decimal("20"),
        "candidate_order_notional": Decimal("10"),
        "projected_position_notional": Decimal("80"),
        "daily_realized_pnl": Decimal("-3"),
        "unrealized_pnl": Decimal("-2"),
        "daily_loss": Decimal("5"),
        "manual_kill_active": False,
    }  # 각 subTest는 새 mapping 하나만 변경해 실패 필드를 분리한다.


class RiskValueObjectTests(unittest.TestCase):
    """
    클래스 이름: RiskValueObjectTests
    기능: 위험 정책·예산·결정의 explicit state, Decimal과 불변식 validation을 검증한다.
    작성 날짜: 2026/08/24
    """

    def test_manual_kill_control_state_requires_exact_bool_and_version(
        self,
    ) -> None:
        """
        함수 이름: test_manual_kill_control_state_requires_exact_bool_and_version()
        기능: 재시작 control state가 exact bool과 0 이상의 exact int version만 받는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # 파일 부재, version 0 no-op receipt와 첫 durable activation version 1을 명시적으로 허용한다.
        self.assertEqual(0, ManualKillControlState().version)
        self.assertEqual(
            0,
            ManualKillControlState(
                active=False,
                version=0,
                command_id="confirm-inactive-risk-control",
                expected_version=0,
            ).version,
        )
        self.assertTrue(
            ManualKillControlState(
                active=True,
                version=1,
                command_id="activate-risk-control",
                expected_version=0,
            ).active
        )

        # bool/int 상속이나 음수 version이 persistence provenance로 들어가지 못하게 table로 확인한다.
        invalid_cases = (
            ({"active": 1, "version": 0}, TypeError),
            ({"active": False, "version": True}, TypeError),
            ({"active": False, "version": -1}, ValueError),
            ({"active": True, "version": 1}, ValueError),
            (
                {
                    "active": True,
                    "version": 1,
                    "command_id": "activate-risk-control",
                    "expected_version": 2,
                },
                ValueError,
            ),
            (
                {
                    "active": True,
                    "version": 1,
                    "command_id": "activate-risk-control",
                    "expected_version": 0,
                    "behavior": ManualKillBehavior.BLOCK_NEW_ORDERS,
                },
                ValueError,
            ),
        )
        for values, expected_error in invalid_cases:
            with self.subTest(values=values):
                with self.assertRaises(expected_error):
                    ManualKillControlState(**values)  # type: ignore[arg-type]

    def test_policy_availability_is_explicit_and_value_objects_are_frozen(
        self,
    ) -> None:
        """
        함수 이름: test_policy_availability_is_explicit_and_value_objects_are_frozen()
        기능: configured와 unavailable 상태가 null 없이 구분되고 정책이 불변인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # 서로 다른 concrete type이 availability를 고정해 임의 숫자 기본 정책을 만들지 못하게 한다.
        configured_policy = create_policy()
        unavailable_policy = RiskPolicyUnavailable()

        self.assertIs(
            RiskPolicyAvailability.CONFIGURED,
            configured_policy.availability,
        )
        self.assertIs(
            RiskPolicyAvailability.UNAVAILABLE,
            unavailable_policy.availability,
        )
        with self.assertRaises(FrozenInstanceError):
            configured_policy.version = 8  # type: ignore[misc]  # 세션 중 정책 version mutation을 막는다.

    def test_configured_policy_accepts_explicit_unbounded_limits(
        self,
    ) -> None:
        """
        함수 이름: test_configured_policy_accepts_explicit_unbounded_limits()
        기능: 세 금융 상한의 None이 unavailable이 아닌 configured 무제한으로 보존되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # 세 상한을 명시적으로 비운 정책도 version과 운영 선택을 가진 configured 상태여야 한다.
        policy = create_policy(
            max_order_notional=None,
            max_position_notional=None,
            max_daily_loss=None,
            daily_loss_scope=DailyLossScope.REALIZED_ONLY,
            manual_kill_behavior=ManualKillBehavior.CANCEL_AND_LIQUIDATE,
        )

        self.assertIs(RiskPolicyAvailability.CONFIGURED, policy.availability)
        self.assertIsNone(policy.max_order_notional)
        self.assertIsNone(policy.max_position_notional)
        self.assertIsNone(policy.max_daily_loss)
        self.assertIs(DailyLossScope.REALIZED_ONLY, policy.daily_loss_scope)
        self.assertIs(
            ManualKillBehavior.CANCEL_AND_LIQUIDATE,
            policy.manual_kill_behavior,
        )

    def test_policy_rejects_invalid_versions_and_financial_limits(self) -> None:
        """
        함수 이름: test_policy_rejects_invalid_versions_and_financial_limits()
        기능: policy version 우회 타입과 0·음수·비유한·비Decimal 상한을 table로 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        invalid_cases = (
            ("version", True, TypeError),
            ("version", 1.0, TypeError),
            ("version", 0, ValueError),
            ("version", -1, ValueError),
            ("max_order_notional", 10, TypeError),
            ("max_position_notional", 100.0, TypeError),
            ("max_daily_loss", "8", TypeError),
            ("max_order_notional", Decimal("NaN"), ValueError),
            ("max_position_notional", Decimal("Infinity"), ValueError),
            ("max_daily_loss", Decimal("-Infinity"), ValueError),
            ("max_order_notional", Decimal("0"), ValueError),
            ("max_position_notional", Decimal("-0.01"), ValueError),
            ("max_daily_loss", Decimal("0"), ValueError),
        )

        # 매 case는 한 필드만 교체해 정확한 타입·범위 오류가 정책 생성을 막는지 확인한다.
        for field_name, invalid_value, expected_error in invalid_cases:
            with self.subTest(field_name=field_name, invalid_value=invalid_value):
                policy_values: dict[str, object] = {
                    "version": 7,
                    "max_order_notional": Decimal("10"),
                    "max_position_notional": Decimal("100"),
                    "max_daily_loss": Decimal("8"),
                    "daily_loss_scope": (
                        DailyLossScope.REALIZED_AND_UNREALIZED
                    ),
                    "manual_kill_behavior": (
                        ManualKillBehavior.BLOCK_NEW_ORDERS
                    ),
                }
                policy_values[field_name] = invalid_value

                with self.assertRaises(expected_error):
                    RiskPolicy(**policy_values)  # type: ignore[arg-type]  # runtime validation 대상이다.

    def test_policy_rejects_string_enum_substitutes(self) -> None:
        """
        함수 이름: test_policy_rejects_string_enum_substitutes()
        기능: daily loss와 manual kill 선택에 enum 문자열을 직접 저장하지 못하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # 외부 transport 문자열은 도메인 정책 생성 전에 canonical enum으로 변환되어야 한다.
        with self.assertRaises(TypeError):
            create_policy(
                daily_loss_scope="REALIZED_ONLY",  # type: ignore[arg-type]
            )
        with self.assertRaises(TypeError):
            create_policy(
                manual_kill_behavior="BLOCK_NEW_ORDERS",  # type: ignore[arg-type]
            )

    def test_budget_rejects_invalid_versions_financial_values_and_flags(
        self,
    ) -> None:
        """
        함수 이름: test_budget_rejects_invalid_versions_financial_values_and_flags()
        기능: source version, 금융값과 manual kill의 잘못된 타입·범위를 table로 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        invalid_cases = (
            ("policy_version", True, TypeError),
            ("policy_version", 0, ValueError),
            ("market_version", True, TypeError),
            ("account_version", 1.0, TypeError),
            ("context_version", -1, ValueError),
            ("current_position_notional", 50, TypeError),
            ("reserved_buy_notional", Decimal("NaN"), ValueError),
            ("candidate_order_notional", Decimal("Infinity"), ValueError),
            ("projected_position_notional", Decimal("-1"), ValueError),
            ("daily_realized_pnl", Decimal("NaN"), ValueError),
            ("unrealized_pnl", -2.0, TypeError),
            ("daily_loss", Decimal("-0.01"), ValueError),
            ("manual_kill_active", 1, TypeError),
        )

        # 유효 mapping에서 한 값만 교체해 각 source와 금융 경계가 독립적으로 검증되는지 확인한다.
        for field_name, invalid_value, expected_error in invalid_cases:
            with self.subTest(field_name=field_name, invalid_value=invalid_value):
                budget_values = create_budget_values()
                budget_values[field_name] = invalid_value

                with self.assertRaises(expected_error):
                    RiskBudgetSnapshot(**budget_values)  # type: ignore[arg-type]  # runtime validation 대상이다.

    def test_budget_requires_exact_projected_exposure_sum(self) -> None:
        """
        함수 이름: test_budget_requires_exact_projected_exposure_sum()
        기능: projected position이 현재·예약·후보 BUY notional의 정확한 합인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        budget_values = create_budget_values()
        budget_values["projected_position_notional"] = Decimal("79.999")

        # 계산 owner가 누락한 reservation이나 후보 금액은 허용 판정 전에 programming error로 막는다.
        with self.assertRaises(ValueError):
            RiskBudgetSnapshot(**budget_values)  # type: ignore[arg-type]  # 잘못된 합계를 직접 검증한다.

    def test_risk_decision_requires_a_consistent_typed_result(self) -> None:
        """
        함수 이름: test_risk_decision_requires_a_consistent_typed_result()
        기능: 허용과 차단 결과가 block reason 유무 및 snapshot 타입과 일치하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        budget = create_budget()

        # truthy 정수, 허용+사유, 차단+무사유와 문자열 사유 조합을 모두 거부한다.
        with self.assertRaises(TypeError):
            RiskDecision(allowed=1, budget=budget)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            RiskDecision(
                allowed=True,
                budget=budget,
                block_reason=RiskBlockReason.RISK_POLICY_UNAVAILABLE,
            )
        with self.assertRaises(ValueError):
            RiskDecision(allowed=False, budget=budget)
        with self.assertRaises(ValueError):
            RiskDecision(
                allowed=False,
                budget=budget,
                block_reason="RISK_POLICY_UNAVAILABLE",  # type: ignore[arg-type]
            )


class BuyRiskEvaluationTests(unittest.TestCase):
    """
    클래스 이름: BuyRiskEvaluationTests
    기능: availability부터 projected position까지 신규 BUY gate 순서와 경계값을 검증한다.
    작성 날짜: 2026/08/24
    """

    def test_gate_returns_the_first_block_reason_in_documented_order(
        self,
    ) -> None:
        """
        함수 이름: test_gate_returns_the_first_block_reason_in_documented_order()
        기능: 여러 위험 조건이 동시에 참일 때 명세 순서의 첫 typed 사유만 반환하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        policy = create_policy()
        priority_cases = (
            (
                "unavailable_before_everything",
                RiskPolicyUnavailable(),
                create_budget(
                    policy_version=None,
                    current_position_notional=Decimal("100"),
                    reserved_buy_notional=Decimal("20"),
                    candidate_order_notional=Decimal("11"),
                    daily_realized_pnl=Decimal("-10"),
                    unrealized_pnl=Decimal("-10"),
                    daily_loss=Decimal("20"),
                    manual_kill_active=True,
                ),
                RiskBlockReason.RISK_POLICY_UNAVAILABLE,
            ),
            (
                "version_before_manual_kill",
                policy,
                create_budget(
                    policy_version=6,
                    candidate_order_notional=Decimal("11"),
                    daily_realized_pnl=Decimal("-10"),
                    unrealized_pnl=Decimal("-10"),
                    daily_loss=Decimal("20"),
                    manual_kill_active=True,
                ),
                RiskBlockReason.RISK_POLICY_VERSION_MISMATCH,
            ),
            (
                "manual_kill_before_numeric_limits",
                policy,
                create_budget(
                    candidate_order_notional=Decimal("11"),
                    daily_realized_pnl=Decimal("-10"),
                    unrealized_pnl=Decimal("-10"),
                    daily_loss=Decimal("20"),
                    manual_kill_active=True,
                ),
                RiskBlockReason.MANUAL_KILL_SWITCH_ACTIVE,
            ),
            (
                "single_order_before_daily_and_position",
                policy,
                create_budget(
                    current_position_notional=Decimal("90"),
                    reserved_buy_notional=Decimal("10"),
                    candidate_order_notional=Decimal("10.001"),
                    daily_realized_pnl=Decimal("-5"),
                    unrealized_pnl=Decimal("-3"),
                    daily_loss=Decimal("8"),
                ),
                RiskBlockReason.RISK_ORDER_NOTIONAL_EXCEEDED,
            ),
            (
                "daily_before_projected_position",
                policy,
                create_budget(
                    current_position_notional=Decimal("91"),
                    reserved_buy_notional=Decimal("0"),
                    candidate_order_notional=Decimal("10"),
                    daily_realized_pnl=Decimal("-5"),
                    unrealized_pnl=Decimal("-3"),
                    daily_loss=Decimal("8"),
                ),
                RiskBlockReason.RISK_DAILY_LOSS_EXCEEDED,
            ),
            (
                "projected_position_last",
                policy,
                create_budget(
                    current_position_notional=Decimal("91"),
                    reserved_buy_notional=Decimal("0"),
                    candidate_order_notional=Decimal("10"),
                ),
                RiskBlockReason.RISK_POSITION_NOTIONAL_EXCEEDED,
            ),
        )

        # 순서가 뒤인 조건도 함께 참인 fixture로 실제 first-match 계약을 table-driven 검증한다.
        for case_name, policy_state, budget, expected_reason in priority_cases:
            with self.subTest(case_name=case_name):
                decision = evaluate_buy_risk(policy_state, budget)

                self.assertFalse(decision.allowed)
                self.assertIs(expected_reason, decision.block_reason)
                self.assertIs(budget, decision.budget)  # 같은 authoritative capture를 결과에 보존한다.

    def test_numeric_limits_distinguish_below_equal_and_above(
        self,
    ) -> None:
        """
        함수 이름: test_numeric_limits_distinguish_below_equal_and_above()
        기능: 단건·daily loss·누적 position의 below/equal/above 경계 계약을 table로 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        policy = create_policy()
        boundary_cases = (
            (
                "order_below",
                create_budget(candidate_order_notional=Decimal("9.999")),
                None,
            ),
            (
                "order_equal",
                create_budget(candidate_order_notional=Decimal("10")),
                None,
            ),
            (
                "order_above",
                create_budget(candidate_order_notional=Decimal("10.001")),
                RiskBlockReason.RISK_ORDER_NOTIONAL_EXCEEDED,
            ),
            (
                "daily_below",
                create_budget(
                    daily_realized_pnl=Decimal("-5"),
                    unrealized_pnl=Decimal("-2.999"),
                    daily_loss=Decimal("7.999"),
                ),
                None,
            ),
            (
                "daily_equal",
                create_budget(
                    daily_realized_pnl=Decimal("-5"),
                    unrealized_pnl=Decimal("-3"),
                    daily_loss=Decimal("8"),
                ),
                RiskBlockReason.RISK_DAILY_LOSS_EXCEEDED,
            ),
            (
                "daily_above",
                create_budget(
                    daily_realized_pnl=Decimal("-5"),
                    unrealized_pnl=Decimal("-3.001"),
                    daily_loss=Decimal("8.001"),
                ),
                RiskBlockReason.RISK_DAILY_LOSS_EXCEEDED,
            ),
            (
                "position_below",
                create_budget(
                    current_position_notional=Decimal("69.999"),
                ),
                None,
            ),
            (
                "position_equal",
                create_budget(current_position_notional=Decimal("70")),
                None,
            ),
            (
                "position_above",
                create_budget(
                    current_position_notional=Decimal("70.001"),
                ),
                RiskBlockReason.RISK_POSITION_NOTIONAL_EXCEEDED,
            ),
        )

        # 상한과 같을 때 order/position은 허용하고 daily loss만 도달 즉시 차단하는지 확인한다.
        for case_name, budget, expected_reason in boundary_cases:
            with self.subTest(case_name=case_name):
                decision = evaluate_buy_risk(policy, budget)

                self.assertEqual(expected_reason is None, decision.allowed)
                self.assertIs(expected_reason, decision.block_reason)

    def test_explicit_unbounded_limits_skip_only_numeric_blocks(
        self,
    ) -> None:
        """
        함수 이름: test_explicit_unbounded_limits_skip_only_numeric_blocks()
        기능: configured None 상한은 해당 숫자 gate만 생략하고 manual kill과 예산 근거를 보존하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # 큰 노출과 실현손실도 세 상한이 모두 None이면 계산 근거를 지우지 않고 허용해야 한다.
        unbounded_policy = create_policy(
            max_order_notional=None,
            max_position_notional=None,
            max_daily_loss=None,
            daily_loss_scope=DailyLossScope.REALIZED_ONLY,
            manual_kill_behavior=ManualKillBehavior.CANCEL_AND_LIQUIDATE,
        )
        unbounded_budget = create_budget(
            current_position_notional=Decimal("1000000"),
            reserved_buy_notional=Decimal("2000000"),
            candidate_order_notional=Decimal("3000000"),
            daily_realized_pnl=Decimal("-999999"),
            unrealized_pnl=Decimal("-5000000"),
            daily_loss=Decimal("999999"),
        )

        decision = evaluate_buy_risk(unbounded_policy, unbounded_budget)

        self.assertTrue(decision.allowed)
        self.assertIs(unbounded_budget, decision.budget)

        # 무제한 configured 정책도 manual kill이 켜지면 숫자 gate보다 먼저 신규 BUY를 차단한다.
        killed_budget = create_budget(
            current_position_notional=Decimal("1000000"),
            reserved_buy_notional=Decimal("2000000"),
            candidate_order_notional=Decimal("3000000"),
            daily_realized_pnl=Decimal("-999999"),
            unrealized_pnl=Decimal("-5000000"),
            daily_loss=Decimal("999999"),
            manual_kill_active=True,
        )
        killed_decision = evaluate_buy_risk(
            unbounded_policy,
            killed_budget,
        )

        self.assertFalse(killed_decision.allowed)
        self.assertIs(
            RiskBlockReason.MANUAL_KILL_SWITCH_ACTIVE,
            killed_decision.block_reason,
        )

    def test_partial_unbounded_policy_preserves_finite_gate_order(
        self,
    ) -> None:
        """
        함수 이름: test_partial_unbounded_policy_preserves_finite_gate_order()
        기능: 일부 None 상한이 다른 configured 유한 상한의 순서와 경계를 변경하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        partial_cases = (
            (
                "unbounded_order_still_checks_daily",
                create_policy(max_order_notional=None),
                create_budget(
                    candidate_order_notional=Decimal("1000"),
                    current_position_notional=Decimal("0"),
                    reserved_buy_notional=Decimal("0"),
                    daily_realized_pnl=Decimal("-5"),
                    unrealized_pnl=Decimal("-3"),
                    daily_loss=Decimal("8"),
                ),
                RiskBlockReason.RISK_DAILY_LOSS_EXCEEDED,
            ),
            (
                "unbounded_daily_still_checks_position",
                create_policy(max_daily_loss=None),
                create_budget(
                    current_position_notional=Decimal("91"),
                    reserved_buy_notional=Decimal("0"),
                    candidate_order_notional=Decimal("10"),
                    daily_realized_pnl=Decimal("-500"),
                    unrealized_pnl=Decimal("-500"),
                    daily_loss=Decimal("1000"),
                ),
                RiskBlockReason.RISK_POSITION_NOTIONAL_EXCEEDED,
            ),
            (
                "unbounded_position_allows_large_existing_exposure",
                create_policy(max_position_notional=None),
                create_budget(
                    current_position_notional=Decimal("1000"),
                    reserved_buy_notional=Decimal("0"),
                    candidate_order_notional=Decimal("10"),
                ),
                None,
            ),
        )

        # 각 case는 None인 gate만 건너뛰고 뒤의 유한 gate가 원래 순서로 판정되는지 확인한다.
        for case_name, policy, budget, expected_reason in partial_cases:
            with self.subTest(case_name=case_name):
                decision = evaluate_buy_risk(policy, budget)

                self.assertEqual(expected_reason is None, decision.allowed)
                self.assertIs(expected_reason, decision.block_reason)

    def test_daily_loss_scope_uses_only_the_configured_pnl_range(self) -> None:
        """
        함수 이름: test_daily_loss_scope_uses_only_the_configured_pnl_range()
        기능: realized-only와 realized-plus-unrealized 정책이 같은 PnL에서 다른 gate 결과를 내는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        scope_cases = (
            (
                "realized_only_ignores_unrealized_loss",
                create_policy(daily_loss_scope=DailyLossScope.REALIZED_ONLY),
                create_budget(
                    daily_realized_pnl=Decimal("-3"),
                    unrealized_pnl=Decimal("-20"),
                    daily_loss=Decimal("3"),
                ),
                True,
            ),
            (
                "combined_scope_blocks_total_loss",
                create_policy(
                    daily_loss_scope=(
                        DailyLossScope.REALIZED_AND_UNREALIZED
                    )
                ),
                create_budget(
                    daily_realized_pnl=Decimal("-3"),
                    unrealized_pnl=Decimal("-20"),
                    daily_loss=Decimal("23"),
                ),
                False,
            ),
            (
                "positive_combined_pnl_has_zero_loss",
                create_policy(
                    daily_loss_scope=(
                        DailyLossScope.REALIZED_AND_UNREALIZED
                    )
                ),
                create_budget(
                    daily_realized_pnl=Decimal("3"),
                    unrealized_pnl=Decimal("-2"),
                    daily_loss=Decimal("0"),
                ),
                True,
            ),
        )

        # 같은 순수 evaluator가 configured scope별 음수 PnL 절댓값만 사용하도록 검증한다.
        for case_name, policy, budget, expected_allowed in scope_cases:
            with self.subTest(case_name=case_name):
                decision = evaluate_buy_risk(policy, budget)

                self.assertEqual(expected_allowed, decision.allowed)

    def test_both_manual_kill_behaviors_block_new_buys(self) -> None:
        """
        함수 이름: test_both_manual_kill_behaviors_block_new_buys()
        기능: 두 configured manual kill 행동이 모두 즉시 신규 BUY를 차단하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # CANCEL_AND_LIQUIDATE의 외부 effect 여부와 무관하게 domain BUY gate는 두 행동에서 같다.
        for manual_kill_behavior in ManualKillBehavior:
            with self.subTest(manual_kill_behavior=manual_kill_behavior):
                policy = create_policy(
                    manual_kill_behavior=manual_kill_behavior,
                )
                decision = evaluate_buy_risk(
                    policy,
                    create_budget(manual_kill_active=True),
                )

                self.assertFalse(decision.allowed)
                self.assertIs(
                    RiskBlockReason.MANUAL_KILL_SWITCH_ACTIVE,
                    decision.block_reason,
                )

    def test_financial_sums_ignore_the_ambient_decimal_context(self) -> None:
        """
        함수 이름: test_financial_sums_ignore_the_ambient_decimal_context()
        기능: projected exposure와 daily loss 합계가 외부 Decimal precision 변경에 흔들리지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        policy = create_policy(
            max_order_notional=Decimal("1000000000000000000000000000000000"),
            max_position_notional=Decimal(
                "9999999999999999999999999999999999"
            ),
            max_daily_loss=Decimal(
                "9999999999999999999999999999999999"
            ),
        )

        # 호출자 context를 낮춰도 value object와 evaluator는 ADR-004 Decimal128 계산을 사용한다.
        with localcontext() as decimal_context:
            decimal_context.prec = 4
            budget = RiskBudgetSnapshot(
                policy_version=7,
                market_version=11,
                account_version=13,
                context_version=17,
                current_position_notional=Decimal(
                    "123456789012345678901234567890.1"
                ),
                reserved_buy_notional=Decimal("0.2"),
                candidate_order_notional=Decimal("0.3"),
                projected_position_notional=Decimal(
                    "123456789012345678901234567890.6"
                ),
                daily_realized_pnl=Decimal(
                    "-12345678901234567890.1"
                ),
                unrealized_pnl=Decimal("-0.2"),
                daily_loss=Decimal("12345678901234567890.3"),
                manual_kill_active=False,
            )
            decision = evaluate_buy_risk(policy, budget)

        self.assertTrue(decision.allowed)  # 낮은 ambient precision 밖에서도 허용 결과를 보존한다.

    def test_daily_loss_must_match_the_configured_scope(self) -> None:
        """
        함수 이름: test_daily_loss_must_match_the_configured_scope()
        기능: snapshot daily loss가 policy 범위의 PnL 산식과 다르면 fail closed되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        policy = create_policy(
            max_daily_loss=None,
            daily_loss_scope=DailyLossScope.REALIZED_ONLY,
        )
        inconsistent_budget = create_budget(
            daily_realized_pnl=Decimal("-3"),
            unrealized_pnl=Decimal("-20"),
            daily_loss=Decimal("23"),
        )

        # 실현 전용 정책에서 미실현손실을 섞은 snapshot은 허용·typed block 어느 쪽으로도 오인하지 않는다.
        with self.assertRaises(ValueError):
            evaluate_buy_risk(policy, inconsistent_budget)

    def test_evaluator_rejects_untyped_policy_and_budget_inputs(self) -> None:
        """
        함수 이름: test_evaluator_rejects_untyped_policy_and_budget_inputs()
        기능: mapping이나 문자열이 pure domain gate의 typed input을 우회하지 못하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        policy = create_policy()
        budget = create_budget()

        # 외부 adapter가 도메인 객체를 조립하기 전에 evaluator를 호출하면 즉시 타입 오류로 거부한다.
        with self.assertRaises(TypeError):
            evaluate_buy_risk("configured", budget)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            evaluate_buy_risk(policy, {})  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
