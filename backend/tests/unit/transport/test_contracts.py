"""Transport DTO mapping, Decimal, UTC, REGIME와 generated schema를 검증한다."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
import unittest

from binance_auto_trader.domain.common import Interval, RegimeType
from binance_auto_trader.domain.trading.risk import (
    DailyLossScope,
    ManualKillBehavior,
    RiskBudgetSnapshot,
    RiskDecision,
    RiskPolicyAvailability,
)
from binance_auto_trader.domain.trading.states import (
    ExitReason,
    OrderSide,
    StrategyType,
)
from binance_auto_trader.transport.contracts import (
    SCHEMA_VERSION,
    TransportContractError,
    build_snapshot_dto,
    decimal_from_wire,
    datetime_to_wire,
    decimal_to_wire,
    map_trading_snapshot,
    normalize_json_value,
    ratio_from_wire,
    regime_from_wire,
    regime_to_wire,
    require_command_fields,
    require_expected_version,
    render_typescript_contracts,
)


TEST_SESSION_ID = "3c73d583-c1c8-4830-8393-cc31639a40fd"
TEST_TIME = datetime(2026, 8, 21, 2, 3, 4, 567890, tzinfo=timezone.utc)


class _ExecutionMode(Enum):
    """
    클래스 이름: _ExecutionMode
    기능: runtime execution mode의 value mapping을 재현하는 test enum이다.
    작성 날짜: 2026/08/21
    """

    DISABLED = "disabled"


def _create_ready_runtime() -> SimpleNamespace:
    """
    함수 이름: _create_ready_runtime()
    기능: coherent snapshot mapper가 요구하는 모든 authoritative field를 구성한다.
    인자: 없음
    반환값: 준비된 RuntimeSnapshotSource test double
    작성 날짜: 2026/08/21
    """
    # 같은 source version과 시각을 공유하는 Market·Indicator·REGIME snapshot을 구성한다.
    market_snapshot = SimpleNamespace(
        symbol="ETHUSDT",
        current_eth_price=Decimal("4321.5000"),
        version=7,
        updated_at=TEST_TIME,
    )
    swing_structure = SimpleNamespace(
        swing_highs=(Decimal("4400"), Decimal("4500")),
        swing_lows=(Decimal("4100"), Decimal("4200")),
        has_higher_high=True,
        has_higher_low=True,
        has_lower_high=False,
        has_lower_low=False,
    )
    indicator_snapshot = SimpleNamespace(
        ready=True,
        symbol="ETHUSDT",
        timeframe=Interval.FOUR_HOURS,
        ema9_series=(Decimal("4200.10"), Decimal("4210.20")),
        ema9_slope=Decimal("0.12340000"),
        live_ema9=Decimal("4242.42"),
        current_price=Decimal("4321.5000"),
        source_market_version=7,
        source_candle_id="ETHUSDT:4h:2026-08-21T00:00:00Z",
        calculated_at=TEST_TIME,
        swing_structure=swing_structure,
    )
    regime_controller = SimpleNamespace(
        indicator_snapshot=indicator_snapshot,
        recommended_regime=RegimeType.TYPE_2,
        selected_regime=None,
    )

    # Account balance total은 domain이 제공하는 authoritative 합과 같은 저장 값이다.
    balance = SimpleNamespace(
        asset="ETH",
        free=Decimal("1.25"),
        locked=Decimal("0.50"),
        total=Decimal("1.75"),
    )
    account = SimpleNamespace(
        valuation_asset="ETH",
        balances={"ETH": balance},
        current_price=Decimal("4321.5000"),
        valuation=Decimal("7562.625000"),
        version=3,
        updated_at=TEST_TIME,
    )
    # Phase 7 lifecycle mapper가 읽는 Context와 Controller session snapshot을 구성한다.
    trading_context = SimpleNamespace(
        version=0,
        scale_in_ratio=Decimal("0.5"),
        scale_out_ratio=Decimal("0.5"),
        position=SimpleNamespace(is_open=False),
    )
    trading_controller = SimpleNamespace(
        account=account,
        context=trading_context,
        status="not_started",
        command_enabled=False,
        session_id=None,
        snapshot_session=lambda: SimpleNamespace(
            status="not_started",
            session_id=None,
            version=0,
            scale_in=Decimal("0.5"),
            scale_out=Decimal("0.5"),
            has_open_position=False,
            command_enabled=False,
            risk_policy_availability="UNAVAILABLE",
            configured_risk_policy_version=None,
            max_order_notional=None,
            max_position_notional=None,
            max_daily_loss=None,
            daily_loss_scope=None,
            manual_kill_behavior=None,
            session_risk_policy_version=None,
            risk_control_version=0,
            manual_kill_active=False,
            manual_kill_cleanup_complete=True,
            manual_kill_activation_behavior=None,
            manual_kill_activation_policy_version=None,
            last_risk_decision=None,
            process_ownership_ambiguous=False,
        ),
    )

    # 최근 체결과 집계 성과를 한 trade-history controller fixture로 조립한다.
    trade = SimpleNamespace(
        trade_id="trade-1",
        order_id="123",
        client_order_id="client-1",
        symbol="ETHUSDT",
        executed_at=TEST_TIME,
        side=OrderSide.BUY,
        regime_type=RegimeType.TYPE_2,
        strategy=StrategyType.CASE_B,
        requested_quantity=Decimal("0.10"),
        executed_quantity=Decimal("0.10"),
        executed_amount=Decimal("432.150"),
        average_fill_price=Decimal("4321.50"),
        market_price_at_decision=Decimal("4320.00"),
        fee_amount=Decimal("0.0001"),
        fee_asset="ETH",
        fee_quote_amount=Decimal("0.43215"),
        allocated_cost_basis=None,
        realized_pnl=None,
        realized_return_rate=None,
        exit_reason=None,
    )
    performance = SimpleNamespace(
        daily_return_rate=Decimal("0"),
        cumulative_return_rate=Decimal("0"),
        realized_pnl=Decimal("0"),
        daily_fee=Decimal("0.43215"),
        total_fee=Decimal("0.43215"),
        average_sell_return_rate=Decimal("0"),
        total_profit=Decimal("0"),
        winning_sell_count=0,
        losing_sell_count=0,
        breakeven_sell_count=0,
        completed_sell_count=0,
        win_rate=None,
    )
    trade_history_controller = SimpleNamespace(
        trade_history=SimpleNamespace(trades=(trade,)),
        performance=performance,
    )

    # 모든 owner를 application lock 아래 조회할 수 있는 READY runtime으로 묶는다.
    return SimpleNamespace(
        application_lock=RLock(),
        ready=True,
        execution_mode=_ExecutionMode.DISABLED,
        market_snapshot=market_snapshot,
        regime_controller=regime_controller,
        trading_controller=trading_controller,
        trade_history_controller=trade_history_controller,
    )


class TransportContractTests(unittest.TestCase):
    """
    클래스 이름: TransportContractTests
    기능: wire scalar와 coherent full snapshot의 schema 불변식을 검증한다.
    작성 날짜: 2026/08/21
    """

    def test_decimal_and_datetime_use_plain_string_and_utc_z(self) -> None:
        """
        함수 이름: test_decimal_and_datetime_use_plain_string_and_utc_z()
        기능: exponent Decimal과 offset datetime이 plain text와 UTC Z로 변환되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        offset_time = datetime(
            2026,
            8,
            21,
            11,
            3,
            4,
            567890,
            tzinfo=timezone(timedelta(hours=9)),
        )

        self.assertEqual(decimal_to_wire(Decimal("1E+3")), "1000")
        self.assertEqual(decimal_to_wire(Decimal("0.0100")), "0.0100")
        self.assertEqual(
            datetime_to_wire(offset_time),
            "2026-08-21T02:03:04.567890Z",
        )
        with self.assertRaises(TypeError):
            decimal_to_wire(1.25)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            datetime_to_wire(datetime(2026, 8, 21))
        with self.assertRaises(TypeError):
            normalize_json_value(0.1)

    def test_regime_mapping_accepts_only_the_five_canonical_values(self) -> None:
        """
        함수 이름: test_regime_mapping_accepts_only_the_five_canonical_values()
        기능: TYPE_0~4와 type0~4 사이의 strict bidirectional mapping을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        expected_pairs = (
            (RegimeType.TYPE_0, "type0"),
            (RegimeType.TYPE_1, "type1"),
            (RegimeType.TYPE_2, "type2"),
            (RegimeType.TYPE_3, "type3"),
            (RegimeType.TYPE_4, "type4"),
        )

        # normalization이나 default 없이 exact pair만 왕복시킨다.
        for regime_type, wire_value in expected_pairs:
            with self.subTest(regime_type=regime_type):
                self.assertEqual(regime_to_wire(regime_type), wire_value)
                self.assertIs(regime_from_wire(wire_value), regime_type)
        for invalid_value in ("TYPE_0", "type-0", " type0", "type5", ""):
            with self.subTest(invalid_value=invalid_value):
                with self.assertRaises(TransportContractError):
                    regime_from_wire(invalid_value)

    def test_command_decimal_and_version_fields_are_strict(self) -> None:
        """
        함수 이름: test_command_decimal_and_version_fields_are_strict()
        기능: command DTO가 exact field, Decimal string와 optimistic version만 허용하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 유효한 split command fixture로 exact field와 Decimal 정밀도 보존을 검증한다.
        valid_body = {
            "schema_version": 3,
            "scale_in": "0.40",
            "scale_out": "0.60",
            "expected_version": 3,
        }

        # 정상 DTO는 문자열 정밀도와 version을 손실 없이 복원한다.
        require_command_fields(
            valid_body,
            ("scale_in", "scale_out", "expected_version"),
        )
        self.assertEqual(
            ratio_from_wire(valid_body["scale_in"], "scale_in"),
            Decimal("0.40"),
        )
        self.assertEqual(
            decimal_from_wire("12.300", "amount"),
            Decimal("12.300"),
        )
        self.assertEqual(require_expected_version(valid_body), 3)

        # float, exponent, bool version, ratio 범위와 unknown field를 각각 거부한다.
        invalid_ratios = (
            0.5,
            "1E-1",
            "-0",
            "-0.0",
            "-0.1",
            "1.1",
            "NaN",
        )
        for invalid_ratio in invalid_ratios:
            with self.subTest(invalid_ratio=invalid_ratio):
                with self.assertRaises(TransportContractError):
                    ratio_from_wire(invalid_ratio, "scale_in")
        with self.assertRaises(TransportContractError):
            require_expected_version({"expected_version": True})
        with self.assertRaises(TransportContractError):
            require_command_fields(
                {**valid_body, "unexpected": "value"},
                ("scale_in", "scale_out", "expected_version"),
            )

    def test_snapshot_contains_only_authoritative_financial_fields(self) -> None:
        """
        함수 이름: test_snapshot_contains_only_authoritative_financial_fields()
        기능: full snapshot이 USDT, Decimal string, nullable selected와 canonical trade를 보존하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        runtime = _create_ready_runtime()

        with runtime.application_lock:
            snapshot = build_snapshot_dto(runtime, TEST_SESSION_ID, 9)

        self.assertEqual(snapshot["last_sequence"], 9)
        self.assertEqual(snapshot["market"]["current_price"], "4321.5000")
        self.assertEqual(snapshot["regime"]["recommended"], "type2")
        self.assertIsNone(snapshot["regime"]["selected"])

        # 정책 숫자가 없는 초기 runtime은 null 숫자 대신 explicit unavailable 위험 상태를 공개한다.
        self.assertEqual(
            snapshot["trading"]["risk_policy_availability"],
            "UNAVAILABLE",
        )
        self.assertIsNone(
            snapshot["trading"]["configured_risk_policy_version"]
        )
        self.assertIsNone(snapshot["trading"]["max_order_notional"])
        self.assertIsNone(snapshot["trading"]["max_position_notional"])
        self.assertIsNone(snapshot["trading"]["max_daily_loss"])
        self.assertIsNone(snapshot["trading"]["daily_loss_scope"])
        self.assertIsNone(snapshot["trading"]["manual_kill_behavior"])
        self.assertIsNone(
            snapshot["trading"]["session_risk_policy_version"]
        )
        self.assertEqual(snapshot["trading"]["risk_control_version"], 0)
        self.assertFalse(snapshot["trading"]["manual_kill_active"])
        self.assertTrue(
            snapshot["trading"]["manual_kill_cleanup_complete"]
        )
        self.assertIsNone(
            snapshot["trading"]["manual_kill_activation_behavior"]
        )
        self.assertIsNone(
            snapshot["trading"]["manual_kill_activation_policy_version"]
        )
        self.assertIsNone(snapshot["trading"]["last_risk_budget"])
        self.assertIsNone(snapshot["trading"]["risk_block_reason"])
        self.assertFalse(
            snapshot["trading"]["process_ownership_ambiguous"]
        )
        self.assertEqual(
            snapshot["trading"]["logic_coverage"],
            [
                {
                    "regime_type": "type0",
                    "support_status": "supported",
                    "start_guard": "READY",
                },
                *(
                    {
                        "regime_type": f"type{index}",
                        "support_status": "unsupported",
                        "start_guard": "UNSUPPORTED_TRADING_LOGIC",
                    }
                    for index in range(1, 5)
                ),
            ],
        )
        # Session snapshot에서 추가된 ratio·position·session 필드를 함께 확인한다.
        self.assertEqual(snapshot["trading"]["scale_in"], "0.5")
        self.assertEqual(snapshot["trading"]["scale_out"], "0.5")
        self.assertFalse(snapshot["trading"]["has_open_position"])
        self.assertIsNone(snapshot["trading"]["session_id"])
        self.assertEqual(snapshot["account"]["quote_asset"], "USDT")
        self.assertEqual(snapshot["account"]["balances"][0]["total"], "1.75")

        trade_row = snapshot["recent_trades"][0]
        self.assertEqual(trade_row["average_fill_price"], "4321.50")
        self.assertNotIn("entry_price", trade_row)
        self.assertNotIn("slippage", trade_row)
        self.assertNotIn("krw", json_text := str(snapshot).lower())
        self.assertNotIn("nan", json_text)
        self.assertNotIn("lower_bb", json_text)

    def test_manual_kill_cleanup_field_is_required_by_snapshot_mapper(
        self,
    ) -> None:
        """
        함수 이름: test_manual_kill_cleanup_field_is_required_by_snapshot_mapper()
        기능: cleanup publication 누락을 완료 True로 보정하지 않고 contract 실패로 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        runtime = _create_ready_runtime()
        session_snapshot = runtime.trading_controller.snapshot_session()
        delattr(
            session_snapshot,
            "manual_kill_cleanup_complete",
        )  # 구버전·불완전 snapshot이 완료 상태로 승격되는 경계를 재현한다.
        controller = SimpleNamespace(
            snapshot_session=lambda: session_snapshot,
        )

        # 필수 안전 필드 누락은 명시적 AttributeError로 fail closed해야 한다.
        with self.assertRaisesRegex(
            AttributeError,
            "manual_kill_cleanup_complete",
        ):
            map_trading_snapshot(controller, _ExecutionMode.DISABLED)

    def test_configured_unbounded_risk_policy_maps_to_explicit_wire_nulls(
        self,
    ) -> None:
        """
        함수 이름: test_configured_unbounded_risk_policy_maps_to_explicit_wire_nulls()
        기능: configured policy의 세 무제한 상한과 운영 enum이 unavailable과 다른 wire 값인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # None 상한이어도 BUY 판정에서 계산한 모든 예산·PnL·version 값은 유지한다.
        last_risk_decision = RiskDecision(
            allowed=True,
            budget=RiskBudgetSnapshot(
                policy_version=4,
                market_version=12,
                account_version=8,
                context_version=21,
                current_position_notional=Decimal("125.50"),
                reserved_buy_notional=Decimal("24.25"),
                candidate_order_notional=Decimal("50.25"),
                projected_position_notional=Decimal("200.00"),
                daily_realized_pnl=Decimal("-12.75"),
                unrealized_pnl=Decimal("-3.50"),
                daily_loss=Decimal("12.75"),
                manual_kill_active=False,
            ),
        )
        session_snapshot = SimpleNamespace(
            status="not_started",
            session_id=None,
            version=0,
            scale_in=Decimal("0.5"),
            scale_out=Decimal("0.5"),
            has_open_position=False,
            command_enabled=False,
            risk_policy_availability=RiskPolicyAvailability.CONFIGURED,
            configured_risk_policy_version=4,
            max_order_notional=None,
            max_position_notional=None,
            max_daily_loss=None,
            daily_loss_scope=DailyLossScope.REALIZED_ONLY,
            manual_kill_behavior=ManualKillBehavior.CANCEL_AND_LIQUIDATE,
            session_risk_policy_version=4,
            risk_control_version=0,
            manual_kill_active=False,
            manual_kill_cleanup_complete=True,
            manual_kill_activation_behavior=None,
            manual_kill_activation_policy_version=None,
            last_risk_decision=last_risk_decision,
            process_ownership_ambiguous=False,
        )
        controller = SimpleNamespace(
            snapshot_session=lambda: session_snapshot,
        )

        # normalize_json_object가 null을 보존하고 enum만 stable 문자열로 평탄화해야 한다.
        trading = map_trading_snapshot(controller, _ExecutionMode.DISABLED)

        self.assertEqual("CONFIGURED", trading["risk_policy_availability"])
        self.assertEqual(4, trading["configured_risk_policy_version"])
        self.assertIsNone(trading["max_order_notional"])
        self.assertIsNone(trading["max_position_notional"])
        self.assertIsNone(trading["max_daily_loss"])
        self.assertEqual("REALIZED_ONLY", trading["daily_loss_scope"])
        self.assertEqual(
            "CANCEL_AND_LIQUIDATE",
            trading["manual_kill_behavior"],
        )
        self.assertTrue(trading["last_risk_decision_allowed"])
        self.assertEqual(
            {
                "policy_version": 4,
                "market_version": 12,
                "account_version": 8,
                "context_version": 21,
                "current_position_notional": "125.50",
                "reserved_buy_notional": "24.25",
                "candidate_order_notional": "50.25",
                "projected_position_notional": "200.00",
                "daily_realized_pnl": "-12.75",
                "unrealized_pnl": "-3.50",
                "daily_loss": "12.75",
                "manual_kill_active": False,
            },
            trading["last_risk_budget"],
        )

    def test_manual_kill_activation_provenance_survives_policy_hot_swap(
        self,
    ) -> None:
        """
        함수 이름: test_manual_kill_activation_provenance_survives_policy_hot_swap()
        기능: configured policy와 활성 epoch의 behavior/version을 별도 wire 필드로 보존하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # Configured v5는 신규 주문만 차단하지만 활성 epoch v4는 취소·청산 의무를 유지한다.
        session_snapshot = SimpleNamespace(
            status="reconciliation_required",
            session_id="62c511b2-ea5c-43ac-bc36-e96eb39c85aa",
            version=8,
            scale_in=Decimal("0.5"),
            scale_out=Decimal("0.5"),
            has_open_position=True,
            command_enabled=False,
            risk_policy_availability=RiskPolicyAvailability.CONFIGURED,
            configured_risk_policy_version=5,
            max_order_notional=None,
            max_position_notional=None,
            max_daily_loss=None,
            daily_loss_scope=DailyLossScope.REALIZED_ONLY,
            manual_kill_behavior=ManualKillBehavior.BLOCK_NEW_ORDERS,
            session_risk_policy_version=4,
            risk_control_version=1,
            manual_kill_active=True,
            manual_kill_cleanup_complete=False,
            manual_kill_activation_behavior=(
                ManualKillBehavior.CANCEL_AND_LIQUIDATE
            ),
            manual_kill_activation_policy_version=4,
            last_risk_decision=None,
            process_ownership_ambiguous=False,
        )
        controller = SimpleNamespace(snapshot_session=lambda: session_snapshot)

        trading = map_trading_snapshot(controller, _ExecutionMode.DISABLED)

        self.assertEqual("BLOCK_NEW_ORDERS", trading["manual_kill_behavior"])
        self.assertEqual(5, trading["configured_risk_policy_version"])
        self.assertEqual(
            "CANCEL_AND_LIQUIDATE",
            trading["manual_kill_activation_behavior"],
        )
        self.assertEqual(4, trading["manual_kill_activation_policy_version"])
        self.assertFalse(trading["manual_kill_cleanup_complete"])

    def test_typescript_renderer_is_deterministic_and_matches_checked_in_file(
        self,
    ) -> None:
        """
        함수 이름: test_typescript_renderer_is_deterministic_and_matches_checked_in_file()
        기능: Python authoritative renderer 출력과 UI generated file의 byte drift를 검출한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        first_render = render_typescript_contracts()
        second_render = render_typescript_contracts()
        repository_root = Path(__file__).resolve().parents[4]
        generated_path = (
            repository_root
            / "UI"
            / "src"
            / "shared"
            / "contracts"
            / "backendContracts.generated.ts"
        )

        self.assertEqual(first_render, second_render)
        self.assertIn(
            "'disabled' | 'fake' | 'testnet' | 'live'",
            first_render,
        )
        self.assertTrue(generated_path.exists(), "generated TypeScript file is missing")
        self.assertEqual(generated_path.read_text(encoding="utf-8"), first_render)

    def test_native_launcher_schema_version_matches_python_contract(self) -> None:
        """
        함수 이름: test_native_launcher_schema_version_matches_python_contract()
        기능: Tauri descriptor의 schema 상수가 Python authoritative version과 같은지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # Repository의 native launcher source에서 exact Rust 상수 선언을 읽는다.
        repository_root = Path(__file__).resolve().parents[4]
        native_source_path = (
            repository_root
            / "UI"
            / "apps"
            / "desktop"
            / "src-tauri"
            / "src"
            / "lib.rs"
        )
        native_source = native_source_path.read_text(encoding="utf-8")

        # Python schema가 바뀌고 native descriptor gate가 남는 cross-layer drift를 차단한다.
        self.assertIn(
            f"const BACKEND_SCHEMA_VERSION: u32 = {SCHEMA_VERSION};",
            native_source,
        )


if __name__ == "__main__":
    unittest.main()
