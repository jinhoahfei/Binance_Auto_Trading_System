"""Transport DTO mapping, Decimal, UTC, REGIME와 generated schema를 검증한다."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
import unittest

from binance_auto_trader.domain.common import Interval, RegimeType
from binance_auto_trader.domain.trading.states import (
    ExitReason,
    OrderSide,
    StrategyType,
)
from binance_auto_trader.transport.contracts import (
    TransportContractError,
    build_snapshot_dto,
    datetime_to_wire,
    decimal_to_wire,
    normalize_json_value,
    regime_from_wire,
    regime_to_wire,
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
    trading_controller = SimpleNamespace(account=account)

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
        self.assertEqual(snapshot["account"]["quote_asset"], "USDT")
        self.assertEqual(snapshot["account"]["balances"][0]["total"], "1.75")

        trade_row = snapshot["recent_trades"][0]
        self.assertEqual(trade_row["average_fill_price"], "4321.50")
        self.assertNotIn("entry_price", trade_row)
        self.assertNotIn("slippage", trade_row)
        self.assertNotIn("krw", json_text := str(snapshot).lower())
        self.assertNotIn("nan", json_text)

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


if __name__ == "__main__":
    unittest.main()
