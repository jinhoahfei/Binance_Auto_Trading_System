"""Trade의 불변성과 ADR-004 versioned JSONL strict validation을 검증한다."""

import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from binance_auto_trader.domain.history import (
    FeeAssetConversionRequiredError,
    RealizedResult,
    Trade,
    trade_from_json_object,
    trade_to_json_object,
)
from binance_auto_trader.domain.trading.states import OrderSide

from tests.unit.history.factories import (
    make_order_execution,
    make_trade,
    make_trade_record,
)


class TradeTests(unittest.TestCase):
    """
    클래스 이름: TradeTests
    기능: Trade가 canonical enum, Decimal, UTC와 BUY/SELL 불변식을 지키는지 테스트한다.
    작성 날짜: 2026/08/21
    """

    def test_trade_is_frozen_and_uses_slots(self) -> None:
        """
        함수 이름: test_trade_is_frozen_and_uses_slots()
        기능: Trade field를 생성 후 변경할 수 없고 instance dictionary가 없는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        trade = make_trade()

        with self.assertRaises(FrozenInstanceError):
            trade.executed_amount = Decimal("201")

        self.assertFalse(hasattr(trade, "__dict__"))

    def test_parses_exact_schema_and_preserves_decimal_precision(self) -> None:
        """
        함수 이름: test_parses_exact_schema_and_preserves_decimal_precision()
        기능: exact JSON object가 float 변환 없이 canonical Trade가 되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        record = make_trade_record()
        record["market_price_at_decision"] = "100.12345678901234567890"

        trade = trade_from_json_object(record)

        self.assertEqual(
            trade.market_price_at_decision,
            Decimal("100.12345678901234567890"),
        )
        self.assertEqual(trade.executed_at.tzinfo, timezone.utc)

    def test_rejects_missing_and_extra_schema_fields(self) -> None:
        """
        함수 이름: test_rejects_missing_and_extra_schema_fields()
        기능: versioned JSONL 필수 key 누락과 알 수 없는 key를 모두 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        missing_record = make_trade_record()
        del missing_record["trade_id"]
        extra_record = make_trade_record()
        extra_record["unexpected"] = "value"

        for invalid_record in (missing_record, extra_record):
            with self.subTest(keys=tuple(invalid_record)):
                with self.assertRaises(ValueError):
                    trade_from_json_object(invalid_record)

    def test_rejects_non_integer_schema_version_and_record_type(self) -> None:
        """
        함수 이름: test_rejects_non_integer_schema_version_and_record_type()
        기능: bool을 포함한 잘못된 schema version과 record type을 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        invalid_values = (
            ("schema_version", True),
            ("schema_version", 3),
            ("record_type", "position"),
        )

        for field_name, field_value in invalid_values:
            with self.subTest(field_name=field_name, field_value=field_value):
                record = make_trade_record()
                record[field_name] = field_value
                with self.assertRaises(ValueError):
                    trade_from_json_object(record)

    def test_v1_and_v2_round_trip_without_changing_accounting_identity(
        self,
    ) -> None:
        """
        함수 이름: test_v1_and_v2_round_trip_without_changing_accounting_identity()
        기능: legacy v1과 current v2가 원래 version을 보존하며 서로 다른 Trade인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        current_trade = make_trade()
        legacy_trade = replace(current_trade, schema_version=1)

        # 같은 execution fact라도 회계 의미가 다른 version은 equality에서 합쳐지지 않는다.
        current_round_trip = trade_from_json_object(
            trade_to_json_object(current_trade)
        )
        legacy_round_trip = trade_from_json_object(
            trade_to_json_object(legacy_trade)
        )

        self.assertEqual(current_round_trip.schema_version, 2)
        self.assertEqual(legacy_round_trip.schema_version, 1)
        self.assertEqual(current_round_trip, current_trade)
        self.assertEqual(legacy_round_trip, legacy_trade)
        self.assertNotEqual(current_round_trip, legacy_round_trip)

    def test_rejects_non_plain_decimal_representations(self) -> None:
        """
        함수 이름: test_rejects_non_plain_decimal_representations()
        기능: JSON number, exponent, 특수값, leading zero와 locale decimal을 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        invalid_decimal_values = (
            100,
            100.0,
            "1e2",
            "NaN",
            "Infinity",
            "01.0",
            "+1",
            "1,000.0",
        )

        for invalid_value in invalid_decimal_values:
            with self.subTest(invalid_value=invalid_value):
                record = make_trade_record()
                record["executed_amount"] = invalid_value
                with self.assertRaises(ValueError):
                    trade_from_json_object(record)

    def test_rejects_non_z_json_timestamp_and_non_utc_trade_time(self) -> None:
        """
        함수 이름: test_rejects_non_z_json_timestamp_and_non_utc_trade_time()
        기능: JSON offset timestamp와 직접 생성한 naive/non-UTC datetime을 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        record = make_trade_record()
        record["executed_at"] = "2026-08-21T00:00:00+09:00"

        with self.assertRaises(ValueError):
            trade_from_json_object(record)
        with self.assertRaises(ValueError):
            make_trade(executed_at=datetime(2026, 8, 21, 0, 0))
        with self.assertRaises(ValueError):
            make_trade(
                executed_at=datetime(
                    2026,
                    8,
                    21,
                    0,
                    0,
                    tzinfo=timezone(timedelta(hours=9)),
                )
            )

    def test_enforces_buy_null_and_sell_realized_result_consistency(self) -> None:
        """
        함수 이름: test_enforces_buy_null_and_sell_realized_result_consistency()
        기능: BUY nullable 필드와 SELL fee 포함 PnL·8자리 수익률 일관성을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        buy_trade = make_trade()
        sell_trade = make_trade(side=OrderSide.SELL)

        with self.assertRaises(ValueError):
            replace(buy_trade, realized_pnl=Decimal("1"))
        with self.assertRaises(ValueError):
            replace(sell_trade, realized_pnl=Decimal("9.80"))
        with self.assertRaises(ValueError):
            replace(sell_trade, realized_return_rate=Decimal("9.78021979"))
        with self.assertRaises(ValueError):
            replace(sell_trade, realized_return_rate=Decimal("9.780219780"))

    def test_validates_quote_and_base_fee_conversion_without_fallback(self) -> None:
        """
        함수 이름: test_validates_quote_and_base_fee_conversion_without_fallback()
        기능: USDT 동일값, ETH per-fill aggregate와 제3 asset reconciliation을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        quote_fee_trade = make_trade(
            fee_amount=Decimal("0.20"),
            fee_asset="USDT",
            fee_quote_amount=Decimal("0.20"),
        )
        base_fee_trade = make_trade()

        self.assertEqual(
            quote_fee_trade.fee_quote_amount,
            quote_fee_trade.fee_amount,
        )
        self.assertEqual(
            base_fee_trade.fee_quote_amount,
            base_fee_trade.fee_amount * base_fee_trade.average_fill_price,
        )

        with self.assertRaisesRegex(ValueError, "USDT fee amount"):
            replace(quote_fee_trade, fee_quote_amount=Decimal("999"))

        multi_fill_fee_trade = make_trade(
            average_fill_price=Decimal("150"),
            fee_amount=Decimal("0.0015"),
            fee_quote_amount=Decimal("0.20"),
        )
        self.assertNotEqual(
            multi_fill_fee_trade.fee_quote_amount,
            multi_fill_fee_trade.fee_amount
            * multi_fill_fee_trade.average_fill_price,
        )
        for fee_amount, fee_quote_amount in (
            (Decimal("0"), Decimal("0.01")),
            (Decimal("0.001"), Decimal("0")),
        ):
            with self.subTest(
                fee_amount=fee_amount,
                fee_quote_amount=fee_quote_amount,
            ):
                with self.assertRaisesRegex(ValueError, "zero together"):
                    make_trade(
                        fee_amount=fee_amount,
                        fee_quote_amount=fee_quote_amount,
                    )

        with self.assertRaises(FeeAssetConversionRequiredError) as context:
            replace(
                quote_fee_trade,
                fee_asset="BNB",
                fee_quote_amount=Decimal("0.01"),
            )

        self.assertEqual(
            context.exception.code,
            "FEE_ASSET_CONVERSION_REQUIRED",
        )
        self.assertEqual(context.exception.fee_asset, "BNB")

    def test_json_parser_rejects_inconsistent_fee_quote_amount(self) -> None:
        """
        함수 이름: test_json_parser_rejects_inconsistent_fee_quote_amount()
        기능: durable JSONL의 임의 fee quote 값이 Performance로 유입되지 않게 한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        record = make_trade_record(
            make_trade(
                fee_amount=Decimal("0.20"),
                fee_asset="USDT",
                fee_quote_amount=Decimal("0.20"),
            )
        )
        record["fee_quote_amount"] = "999"

        with self.assertRaisesRegex(ValueError, "USDT fee amount"):
            trade_from_json_object(record)

    def test_writer_object_round_trips_with_canonical_plain_values(self) -> None:
        """
        함수 이름: test_writer_object_round_trips_with_canonical_plain_values()
        기능: production serializer가 Decimal과 UTC를 canonical JSONL 값으로 보존하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        trade = replace(
            make_trade(),
            market_price_at_decision=Decimal("100.12345678901234567890"),
        )

        # serializer와 strict parser를 왕복해 float나 offset 변환이 없는지 확인한다.
        record = trade_to_json_object(trade)
        restored_trade = trade_from_json_object(record)

        self.assertEqual(restored_trade, trade)
        self.assertEqual(
            record["market_price_at_decision"],
            "100.12345678901234567890",
        )
        self.assertTrue(str(record["executed_at"]).endswith("Z"))

    def test_factory_builds_buy_from_order_intent_and_actual_fill(self) -> None:
        """
        함수 이름: test_factory_builds_buy_from_order_intent_and_actual_fill()
        기능: BUY Trade factory가 요청 정보와 실제 fill 정보를 구분해 보존하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        order, summary = make_order_execution(exchange_order_id="301")

        trade = Trade.from_order_execution(order, summary)

        self.assertEqual(trade.schema_version, 2)
        self.assertEqual(trade.trade_id, "trade-301")
        self.assertEqual(trade.order_id, "301")
        self.assertEqual(trade.client_order_id, order.client_order_id)
        self.assertEqual(trade.executed_quantity, summary.executed_quantity)
        self.assertEqual(trade.executed_amount, summary.executed_amount)
        self.assertEqual(trade.average_fill_price, summary.average_fill_price)
        self.assertIsNone(trade.realized_pnl)
        self.assertIsNone(trade.exit_reason)

    def test_factory_rejects_mismatched_execution_identity_without_trade(
        self,
    ) -> None:
        """
        함수 이름: test_factory_rejects_mismatched_execution_identity_without_trade()
        기능: 다른 client order ID의 체결 요약으로 Trade가 생성되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        order, summary = make_order_execution(exchange_order_id="303")
        mismatched_summary = replace(
            summary,
            client_order_id="different-client-order",
        )

        # 주문 의도와 체결 identity가 다르면 durable Trade 생성 전에 즉시 거부한다.
        with self.assertRaisesRegex(
            ValueError,
            "summary client_order_id does not match order",
        ):
            Trade.from_order_execution(order, mismatched_summary)

    def test_factory_builds_sell_with_realized_result_and_exit_reason(self) -> None:
        """
        함수 이름: test_factory_builds_sell_with_realized_result_and_exit_reason()
        기능: SELL Trade factory가 계산된 원가·손익·수익률과 청산 사유를 보존하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        order, summary = make_order_execution(
            exchange_order_id="302",
            side=OrderSide.SELL,
        )
        realized_result = RealizedResult(
            allocated_cost_basis=Decimal("100.10"),
            realized_pnl=Decimal("9.79"),
            realized_return_rate=Decimal("9.78021978"),
        )

        # Trade constructor가 raw cost를 재계산하지 않고 검증된 realized 결과를 사용한다.
        trade = Trade.from_order_execution(
            order,
            summary,
            realized_result,
        )

        self.assertEqual(trade.allocated_cost_basis, Decimal("100.10"))
        self.assertEqual(trade.realized_pnl, Decimal("9.79"))
        self.assertEqual(trade.realized_return_rate, Decimal("9.78021978"))
        self.assertEqual(trade.exit_reason, order.exit_reason)


if __name__ == "__main__":
    unittest.main()
