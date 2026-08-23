"""공식 exchangeInfo MARKET quantity와 notional filter mapping을 검증한다."""

from __future__ import annotations

from decimal import Decimal
import unittest

from binance_auto_trader.adapters.binance.mappers import (
    BinancePayloadError,
    SymbolFilterError,
    floor_market_quantity,
    parse_symbol_trading_rules,
    prepare_market_order,
    validate_market_notional,
)
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.trading.order import Order
from binance_auto_trader.domain.trading.states import OrderSide, StrategyType


def _exchange_info_payload(
    *,
    status: str = "TRADING",
    base_asset_precision: int = 5,
    lot_step: str = "0.0001",
    market_step: str = "0.001",
    lot_minimum: str = "0.0001",
    market_minimum: str = "0.001",
    minimum_notional: str = "10",
    maximum_notional: str = "10000",
) -> dict[str, object]:
    """
    함수 이름: _exchange_info_payload()
    기능: symbol filter 단위 테스트에 사용할 현재 공식 exchangeInfo 구조를 만든다.
    인자: status -> symbol status
        base_asset_precision -> base asset 직렬화 precision
        lot_step -> LOT_SIZE stepSize
        market_step -> MARKET_LOT_SIZE stepSize
        lot_minimum -> LOT_SIZE minQty
        market_minimum -> MARKET_LOT_SIZE minQty
        minimum_notional -> NOTIONAL minNotional
        maximum_notional -> NOTIONAL maxNotional
    반환값: ETHUSDT 한 항목을 가진 exchangeInfo payload
    작성 날짜: 2026/08/22
    """
    # quotePrecision 없이도 v3 filter와 base precision만으로 동작하는 fixture를 만든다.
    return {
        "timezone": "UTC",
        "serverTime": 1787331600000,
        "rateLimits": [],
        "symbols": [
            {
                "symbol": "ETHUSDT",
                "status": status,
                "baseAsset": "ETH",
                "baseAssetPrecision": base_asset_precision,
                "quoteAsset": "USDT",
                "orderTypes": ["LIMIT", "MARKET"],
                "isSpotTradingAllowed": True,
                "filters": [
                    {
                        "filterType": "LOT_SIZE",
                        "minQty": lot_minimum,
                        "maxQty": "9000",
                        "stepSize": lot_step,
                    },
                    {
                        "filterType": "MARKET_LOT_SIZE",
                        "minQty": market_minimum,
                        "maxQty": "1000",
                        "stepSize": market_step,
                    },
                    {
                        "filterType": "MIN_NOTIONAL",
                        "minNotional": "5",
                        "applyToMarket": True,
                        "avgPriceMins": 5,
                    },
                    {
                        "filterType": "NOTIONAL",
                        "minNotional": minimum_notional,
                        "applyMinToMarket": True,
                        "maxNotional": maximum_notional,
                        "applyMaxToMarket": True,
                        "avgPriceMins": 5,
                    },
                ],
            }
        ],
    }


def _order(
    *,
    quantity: str = "1.23456",
    market_price: str = "100",
) -> Order:
    """
    함수 이름: _order()
    기능: filter 적용 전 requested/submitted quantity가 같은 유효한 Order를 만든다.
    인자: quantity -> 원래 주문 수량 decimal 문자열
        market_price -> 주문 결정 가격 decimal 문자열
    반환값: 아직 거래소 결과를 적용하지 않은 BUY Order
    작성 날짜: 2026/08/22
    """
    selected_quantity = Decimal(quantity)

    return Order(
        intent_id="symbol-filter-intent",
        client_order_id="bat-symbol-filter-0",
        submission_attempt=0,
        symbol="ETHUSDT",
        side=OrderSide.BUY,
        strategy=StrategyType.CASE_B,
        regime_type=RegimeType.TYPE_0,
        requested_quantity=selected_quantity,
        submitted_quantity=selected_quantity,
        market_price_at_decision=Decimal(market_price),
    )  # 요청과 제출 희망 수량의 초기 동일성을 fixture에서 보존한다.


class SymbolFilterTests(unittest.TestCase):
    """
    클래스 이름: SymbolFilterTests
    기능: exchangeInfo parsing과 MARKET 주문 전 fail-closed 수량 보정을 검증한다.
    작성 날짜: 2026/08/22
    """

    def test_prepare_floors_to_common_lot_market_and_precision_step(self) -> None:
        """
        함수 이름: test_prepare_floors_to_common_lot_market_and_precision_step()
        기능: 두 lot step과 base precision을 모두 만족하도록 수량을 내리는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        rules = parse_symbol_trading_rules(
            _exchange_info_payload(),
            "ETHUSDT",
        )
        order = _order()

        # 원 requested intent는 유지하고 제출량만 공통 0.001 grid로 내린다.
        prepared_order = prepare_market_order(order, rules)

        self.assertIs(prepared_order, order)
        self.assertEqual(order.requested_quantity, Decimal("1.23456"))
        self.assertEqual(order.submitted_quantity, Decimal("1.234"))
        self.assertEqual(order.submitted_quantity % Decimal("0.0001"), Decimal("0"))
        self.assertEqual(order.submitted_quantity % Decimal("0.001"), Decimal("0"))

    def test_precision_is_a_floor_even_when_lot_steps_are_disabled(self) -> None:
        """
        함수 이름: test_precision_is_a_floor_even_when_lot_steps_are_disabled()
        기능: 두 stepSize가 0이어도 baseAssetPrecision 수량 자릿수를 지키는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        payload = _exchange_info_payload(
            base_asset_precision=3,
            lot_step="0",
            market_step="0",
        )
        rules = parse_symbol_trading_rules(payload, "ETHUSDT")

        # Precision quantum 0.001이 유일한 활성 간격이 된다.
        floored_quantity = floor_market_quantity(Decimal("1.2349"), rules)

        self.assertEqual(floored_quantity, Decimal("1.234"))

    def test_quantity_below_market_minimum_fails_before_submission(self) -> None:
        """
        함수 이름: test_quantity_below_market_minimum_fails_before_submission()
        기능: 내린 수량이 MARKET_LOT_SIZE minQty보다 작으면 filter 오류인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        rules = parse_symbol_trading_rules(
            _exchange_info_payload(market_minimum="0.01"),
            "ETHUSDT",
        )

        # 요청량 0.0099는 0.009로 내린 뒤 market minimum 0.01을 통과하지 못한다.
        with self.assertRaisesRegex(
            SymbolFilterError,
            "FILTER_MARKET_LOT_SIZE_MINIMUM",
        ):
            floor_market_quantity(Decimal("0.0099"), rules)

    def test_minimum_and_maximum_notional_apply_to_market_orders(self) -> None:
        """
        함수 이름: test_minimum_and_maximum_notional_apply_to_market_orders()
        기능: NOTIONAL의 applyMinToMarket·applyMaxToMarket 조건을 모두 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        rules = parse_symbol_trading_rules(
            _exchange_info_payload(
                minimum_notional="10",
                maximum_notional="100",
            ),
            "ETHUSDT",
        )

        # 정확히 범위 안인 금액은 통과하고 양쪽 경계 밖은 별도 원인으로 거부한다.
        validate_market_notional(Decimal("1"), Decimal("50"), rules)
        with self.assertRaisesRegex(SymbolFilterError, "NOTIONAL_MINIMUM"):
            validate_market_notional(Decimal("0.1"), Decimal("50"), rules)
        with self.assertRaisesRegex(SymbolFilterError, "NOTIONAL_MAXIMUM"):
            validate_market_notional(Decimal("3"), Decimal("50"), rules)

    def test_cancel_only_blocks_new_market_order_but_parses_current_schema(self) -> None:
        """
        함수 이름: test_cancel_only_blocks_new_market_order_but_parses_current_schema()
        기능: 2026 CANCEL_ONLY symbol을 알 수 없는 값으로 깨뜨리지 않고 신규 제출만 막는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        rules = parse_symbol_trading_rules(
            _exchange_info_payload(status="CANCEL_ONLY"),
            "ETHUSDT",
        )

        # CANCEL_ONLY는 query/cancel용 metadata로 읽히지만 prepare 단계에서는 fail closed한다.
        self.assertEqual(rules.status, "CANCEL_ONLY")
        with self.assertRaisesRegex(SymbolFilterError, "CANCEL_ONLY"):
            prepare_market_order(_order(), rules)

    def test_missing_required_market_filter_fails_closed(self) -> None:
        """
        함수 이름: test_missing_required_market_filter_fails_closed()
        기능: exchangeInfo에 MARKET_LOT_SIZE가 없을 때 임의 기본값을 만들지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        payload = _exchange_info_payload()
        symbol_payload = payload["symbols"][0]
        symbol_payload["filters"] = [
            filter_payload
            for filter_payload in symbol_payload["filters"]
            if filter_payload["filterType"] != "MARKET_LOT_SIZE"
        ]

        # 누락된 공식 규칙을 LOT_SIZE로 추측하지 않고 payload 오류로 중단한다.
        with self.assertRaisesRegex(
            BinancePayloadError,
            "LOT_SIZE and MARKET_LOT_SIZE",
        ):
            parse_symbol_trading_rules(payload, "ETHUSDT")


if __name__ == "__main__":
    unittest.main()
