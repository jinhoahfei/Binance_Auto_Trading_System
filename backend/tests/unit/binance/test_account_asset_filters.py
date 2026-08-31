"""공식 myFilters MAX_ASSET와 referencePrice strict mapping을 검증한다."""

from __future__ import annotations

from decimal import Decimal
import unittest

from binance_auto_trader.adapters.binance.mappers import (
    AccountAssetFilter,
    AccountOrderCountFilter,
    AccountRelevantFilters,
    BinancePayloadError,
    NotionalFilter,
    QuantityFilter,
    ReferencePrice,
    SymbolFilterError,
    SymbolTradingRules,
    parse_account_asset_filters,
    parse_account_relevant_filters,
    parse_reference_price,
    validate_account_asset_filters,
    validate_account_relevant_filters,
)
from binance_auto_trader.domain.trading.states import OrderSide


def _account_filters_payload(
    *asset_filters: dict[str, object],
    exchange_filters: tuple[dict[str, object], ...] = (),
    symbol_filters: tuple[dict[str, object], ...] = (),
) -> dict[str, object]:
    """
    함수 이름: _account_filters_payload()
    기능: 공식 myFilters의 세 collection을 가진 JSON fixture를 만든다.
    인자: asset_filters -> assetFilters에 넣을 MAX_ASSET object들
        exchange_filters -> exchangeFilters에 넣을 account count object들
        symbol_filters -> symbolFilters에 넣을 symbol relevant object들
    반환값: strict parser가 소비할 myFilters payload
    작성 날짜: 2026/08/31
    """
    # 세 scope를 서로 바꾸지 않고 공식 root field에 그대로 배치한다.
    return {
        "exchangeFilters": list(exchange_filters),
        "symbolFilters": list(symbol_filters),
        "assetFilters": list(asset_filters),
    }


def _rules(
    *,
    public_relevant_filters: AccountRelevantFilters | None = None,
) -> SymbolTradingRules:
    """
    함수 이름: _rules()
    기능: ETHUSDT MAX_ASSET 검증에 사용할 최소 MARKET symbol 규칙을 만든다.
    인자: public_relevant_filters -> exchangeInfo에서 strict parse한 선택 relevant filter DTO
    반환값: base ETH와 quote USDT를 가진 SymbolTradingRules
    작성 날짜: 2026/08/31
    """
    # Account filter 검증과 무관한 수량·notional 값은 유효한 고정 fixture로 둔다.
    return SymbolTradingRules(
        symbol="ETHUSDT",
        status="TRADING",
        base_asset="ETH",
        quote_asset="USDT",
        base_asset_precision=3,
        order_types=frozenset({"MARKET"}),
        is_spot_trading_allowed=True,
        lot_size=QuantityFilter(
            filter_type="LOT_SIZE",
            minimum_quantity=Decimal("0.001"),
            maximum_quantity=Decimal("1000"),
            step_size=Decimal("0.001"),
        ),
        market_lot_size=QuantityFilter(
            filter_type="MARKET_LOT_SIZE",
            minimum_quantity=Decimal("0.001"),
            maximum_quantity=Decimal("1000"),
            step_size=Decimal("0.001"),
        ),
        notional_filters=(
            NotionalFilter(
                filter_type="MIN_NOTIONAL",
                minimum_notional=Decimal("10"),
                maximum_notional=None,
                apply_minimum_to_market=True,
                apply_maximum_to_market=False,
                average_price_minutes=5,
            ),
        ),
        public_relevant_filters=public_relevant_filters,
    )


class AccountAssetFilterTests(unittest.TestCase):
    """
    클래스 이름: AccountAssetFilterTests
    기능: MAX_ASSET DTO·parser와 base/quote 적용 의미를 검증한다.
    작성 날짜: 2026/08/31
    """

    def test_parser_preserves_exact_max_asset_decimal_tuple(self) -> None:
        """
        함수 이름: test_parser_preserves_exact_max_asset_decimal_tuple()
        기능: 공식 문자열 limit가 순서 있는 immutable Decimal DTO로 변환되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        payload = _account_filters_payload(
            {
                "filterType": "MAX_ASSET",
                "asset": "ETH",
                "limit": "2.50000000",
            },
            {
                "filterType": "MAX_ASSET",
                "asset": "USDT",
                "limit": "250.00000000",
            },
        )

        # Float를 거치지 않은 두 exact 상한과 공식 filter type을 함께 보존한다.
        parsed_filters = parse_account_asset_filters(
            payload,
            "ETHUSDT",
        )  # 입력 순서도 immutable tuple에 유지한다.

        self.assertEqual(
            parsed_filters,
            (
                AccountAssetFilter(
                    filter_type="MAX_ASSET",
                    asset="ETH",
                    maximum_quantity=Decimal("2.50000000"),
                ),
                AccountAssetFilter(
                    filter_type="MAX_ASSET",
                    asset="USDT",
                    maximum_quantity=Decimal("250.00000000"),
                ),
            ),
        )

    def test_parser_rejects_duplicate_asset_and_non_string_limit(self) -> None:
        """
        함수 이름: test_parser_rejects_duplicate_asset_and_non_string_limit()
        기능: 자산 중복과 JSON number limit를 schema 오류로 차단하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        duplicate_payload = _account_filters_payload(
            {"filterType": "MAX_ASSET", "asset": "ETH", "limit": "2"},
            {"filterType": "MAX_ASSET", "asset": "ETH", "limit": "3"},
        )
        numeric_payload = _account_filters_payload(
            {"filterType": "MAX_ASSET", "asset": "ETH", "limit": 2},
        )

        # 어느 경우도 마지막 값 선택이나 float 암시 변환으로 진행하지 않는다.
        with self.assertRaises(BinancePayloadError):
            parse_account_asset_filters(duplicate_payload, "ETHUSDT")
        with self.assertRaises(BinancePayloadError):
            parse_account_asset_filters(numeric_payload, "ETHUSDT")

    def test_parser_preserves_supported_exchange_and_symbol_count_filters(
        self,
    ) -> None:
        """
        함수 이름: test_parser_preserves_supported_exchange_and_symbol_count_filters()
        기능: 공식 exchange·symbol count filter를 scope별 exact DTO로 보존하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        payload = _account_filters_payload(
            exchange_filters=(
                {
                    "filterType": "EXCHANGE_MAX_NUM_ORDERS",
                    "maxNumOrders": 1000,
                },
            ),
            symbol_filters=(
                {
                    "filterType": "MAX_NUM_ORDER_LISTS",
                    "maxNumOrderLists": 20,
                },
            ),
        )

        # Scope와 filter type을 잃지 않아 evaluator가 all-symbol zero snapshot을 요구할 수 있다.
        relevant_filters = parse_account_relevant_filters(
            payload,
            "ETHUSDT",
        )

        self.assertEqual(relevant_filters.symbol, "ETHUSDT")
        self.assertEqual(
            relevant_filters.exchange_order_count_filters,
            (
                AccountOrderCountFilter(
                    filter_type="EXCHANGE_MAX_NUM_ORDERS",
                    maximum_count=1000,
                ),
            ),
        )
        self.assertEqual(
            relevant_filters.symbol_order_count_filters,
            (
                AccountOrderCountFilter(
                    filter_type="MAX_NUM_ORDER_LISTS",
                    maximum_count=20,
                ),
            ),
        )

    def test_parser_accepts_every_documented_plain_market_symbol_filter_type(
        self,
    ) -> None:
        """
        함수 이름: test_parser_accepts_every_documented_plain_market_symbol_filter_type()
        기능: 의미가 공개된 공식 symbol filter 15개와 exchange filter 4개를 exact schema로 해석한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        exchange_filters = (
            {"filterType": "EXCHANGE_MAX_NUM_ORDERS", "maxNumOrders": 1000},
            {
                "filterType": "EXCHANGE_MAX_NUM_ALGO_ORDERS",
                "maxNumAlgoOrders": 200,
            },
            {
                "filterType": "EXCHANGE_MAX_NUM_ICEBERG_ORDERS",
                "maxNumIcebergOrders": 100,
            },
            {
                "filterType": "EXCHANGE_MAX_NUM_ORDER_LISTS",
                "maxNumOrderLists": 50,
            },
        )
        symbol_filters = (
            {
                "filterType": "PRICE_FILTER",
                "minPrice": "0.01",
                "maxPrice": "1000000",
                "tickSize": "0.01",
            },
            {
                "filterType": "PERCENT_PRICE",
                "multiplierUp": "5",
                "multiplierDown": "0.2",
                "avgPriceMins": 5,
            },
            {
                "filterType": "PERCENT_PRICE_BY_SIDE",
                "bidMultiplierUp": "5",
                "bidMultiplierDown": "0.2",
                "askMultiplierUp": "5",
                "askMultiplierDown": "0.2",
                "avgPriceMins": 5,
            },
            {
                "filterType": "LOT_SIZE",
                "minQty": "0.0001",
                "maxQty": "1000",
                "stepSize": "0.0001",
            },
            {
                "filterType": "MIN_NOTIONAL",
                "minNotional": "5",
                "applyToMarket": True,
                "avgPriceMins": 5,
            },
            {
                "filterType": "NOTIONAL",
                "minNotional": "5",
                "applyMinToMarket": True,
                "maxNotional": "1000000",
                "applyMaxToMarket": True,
                "avgPriceMins": 5,
            },
            {"filterType": "ICEBERG_PARTS", "limit": 10},
            {
                "filterType": "MARKET_LOT_SIZE",
                "minQty": "0.0001",
                "maxQty": "1000",
                "stepSize": "0.0001",
            },
            {"filterType": "MAX_NUM_ORDERS", "maxNumOrders": 200},
            {
                "filterType": "MAX_NUM_ALGO_ORDERS",
                "maxNumAlgoOrders": 50,
            },
            {
                "filterType": "MAX_NUM_ICEBERG_ORDERS",
                "maxNumIcebergOrders": 20,
            },
            {"filterType": "MAX_POSITION", "maxPosition": "100"},
            {
                "filterType": "TRAILING_DELTA",
                "minTrailingAboveDelta": 10,
                "maxTrailingAboveDelta": 2000,
                "minTrailingBelowDelta": 10,
                "maxTrailingBelowDelta": 2000,
            },
            {
                "filterType": "MAX_NUM_ORDER_LISTS",
                "maxNumOrderLists": 20,
            },
            {
                "filterType": "MAX_NUM_ORDER_AMENDS",
                "maxNumOrderAmends": 10,
            },
        )

        # T_PLUS_SELL만 공개 평가식이 없어 별도 fail-close test로 남기고 나머지 union을 한 번에 고정한다.
        parsed_filters = parse_account_relevant_filters(
            _account_filters_payload(
                exchange_filters=exchange_filters,
                symbol_filters=symbol_filters,
            ),
            "ETHUSDT",
        )

        self.assertEqual(
            {
                filter_value.filter_type
                for filter_value in parsed_filters.exchange_order_count_filters
            },
            {
                "EXCHANGE_MAX_NUM_ORDERS",
                "EXCHANGE_MAX_NUM_ALGO_ORDERS",
                "EXCHANGE_MAX_NUM_ICEBERG_ORDERS",
                "EXCHANGE_MAX_NUM_ORDER_LISTS",
            },
        )
        self.assertEqual(
            {
                filter_value.filter_type
                for filter_value in parsed_filters.symbol_order_count_filters
            },
            {
                "MAX_NUM_ORDERS",
                "MAX_NUM_ALGO_ORDERS",
                "MAX_NUM_ICEBERG_ORDERS",
                "MAX_NUM_ORDER_LISTS",
                "MAX_NUM_ORDER_AMENDS",
            },
        )
        self.assertEqual(
            {
                filter_value.filter_type
                for filter_value in parsed_filters.symbol_quantity_filters
            },
            {"LOT_SIZE", "MARKET_LOT_SIZE"},
        )
        self.assertEqual(
            {
                filter_value.filter_type
                for filter_value in parsed_filters.symbol_notional_filters
            },
            {"MIN_NOTIONAL", "NOTIONAL"},
        )
        self.assertEqual(
            parsed_filters.symbol_maximum_position,
            Decimal("100"),
        )
        self.assertEqual(
            parsed_filters.passive_symbol_filter_types,
            frozenset(
                {
                    "PRICE_FILTER",
                    "PERCENT_PRICE",
                    "PERCENT_PRICE_BY_SIDE",
                    "ICEBERG_PARTS",
                    "TRAILING_DELTA",
                }
            ),
        )

    def test_base_limit_uses_quantity_and_quote_limit_fails_closed(self) -> None:
        """
        함수 이름: test_base_limit_uses_quantity_and_quote_limit_fails_closed()
        기능: Base는 quantity로 제한하고 공식 가격식이 없는 quote quantity-MARKET은 차단하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        rules = _rules()  # 두 MAX_ASSET가 같은 ETHUSDT base·quote identity를 사용한다.
        filters = parse_account_asset_filters(
            _account_filters_payload(
                {"filterType": "MAX_ASSET", "asset": "ETH", "limit": "2"},
            ),
            "ETHUSDT",
        )

        # Base 수량만 공식 식으로 통과시키고 상한 초과를 고정 code로 구분한다.
        validate_account_asset_filters(
            Decimal("1"),
            Decimal("100"),
            rules,
            filters,
        )
        with self.assertRaisesRegex(SymbolFilterError, "BASE_MAXIMUM"):
            validate_account_asset_filters(
                Decimal("2.1"),
                Decimal("50"),
                rules,
                filters,
            )
        quote_filters = parse_account_asset_filters(
            _account_filters_payload(
                {
                    "filterType": "MAX_ASSET",
                    "asset": "USDT",
                    "limit": "150",
                },
            ),
            "ETHUSDT",
        )
        with self.assertRaisesRegex(SymbolFilterError, "PRICE_UNDEFINED"):
            validate_account_asset_filters(
                Decimal("1"),
                Decimal("100"),
                rules,
                quote_filters,
            )

    def test_plain_market_count_filter_requires_zero_state_and_capacity(
        self,
    ) -> None:
        """
        함수 이름: test_plain_market_count_filter_requires_zero_state_and_capacity()
        기능: Account count filter가 complete zero snapshot과 신규 일반 주문 한 칸을 요구하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        payload = _account_filters_payload(
            exchange_filters=(
                {
                    "filterType": "EXCHANGE_MAX_NUM_ORDERS",
                    "maxNumOrders": 1,
                },
            ),
            symbol_filters=(
                {"filterType": "MAX_NUM_ORDERS", "maxNumOrders": 1},
            ),
        )
        account_filters = parse_account_relevant_filters(payload, "ETHUSDT")
        public_filters = parse_account_relevant_filters(payload, "ETHUSDT")
        rules = _rules(public_relevant_filters=public_filters)

        # Snapshot 부재는 limit 값이 충분해도 차단하고 exact empty에서만 한 MARKET을 허용한다.
        with self.assertRaisesRegex(SymbolFilterError, "COMPLETE_EMPTY"):
            validate_account_relevant_filters(
                Decimal("1"),
                Decimal("100"),
                rules,
                account_filters,
                side=OrderSide.BUY,
                account_open_state_verified_empty=False,
            )
        validate_account_relevant_filters(
            Decimal("1"),
            Decimal("100"),
            rules,
            account_filters,
            side=OrderSide.BUY,
            account_open_state_verified_empty=True,
        )

        blocked_payload = _account_filters_payload(
            exchange_filters=(
                {
                    "filterType": "EXCHANGE_MAX_NUM_ORDERS",
                    "maxNumOrders": 0,
                },
            ),
        )
        blocked_filters = parse_account_relevant_filters(
            blocked_payload,
            "ETHUSDT",
        )
        blocked_rules = _rules(public_relevant_filters=blocked_filters)
        with self.assertRaisesRegex(SymbolFilterError, "MAX_NUM_ORDERS"):
            validate_account_relevant_filters(
                Decimal("1"),
                Decimal("100"),
                blocked_rules,
                blocked_filters,
                side=OrderSide.BUY,
                account_open_state_verified_empty=True,
            )

    def test_signed_and_public_relevant_filter_drift_fails_closed(self) -> None:
        """
        함수 이름: test_signed_and_public_relevant_filter_drift_fails_closed()
        기능: signed passive type과 account count limit가 public exchangeInfo와 다르면 차단한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        public_filters = parse_account_relevant_filters(
            _account_filters_payload(
                exchange_filters=(
                    {
                        "filterType": "EXCHANGE_MAX_NUM_ORDERS",
                        "maxNumOrders": 10,
                    },
                ),
            ),
            "ETHUSDT",
        )
        signed_count_drift = parse_account_relevant_filters(
            _account_filters_payload(
                exchange_filters=(
                    {
                        "filterType": "EXCHANGE_MAX_NUM_ORDERS",
                        "maxNumOrders": 11,
                    },
                ),
            ),
            "ETHUSDT",
        )
        signed_passive_drift = parse_account_relevant_filters(
            _account_filters_payload(
                exchange_filters=(
                    {
                        "filterType": "EXCHANGE_MAX_NUM_ORDERS",
                        "maxNumOrders": 10,
                    },
                ),
                symbol_filters=(
                    {
                        "filterType": "PRICE_FILTER",
                        "minPrice": "0.01",
                        "maxPrice": "1000000",
                        "tickSize": "0.01",
                    },
                ),
            ),
            "ETHUSDT",
        )
        rules = _rules(public_relevant_filters=public_filters)

        # Limit drift와 signed-only passive type 모두 raw 값을 출력하지 않는 고정 error로 막는다.
        for drift_name, account_filters in (
            ("count", signed_count_drift),
            ("passive", signed_passive_drift),
        ):
            with self.subTest(drift_name=drift_name):
                with self.assertRaisesRegex(
                    SymbolFilterError,
                    "SIGNED_.*RULE_MISMATCH",
                ):
                    validate_account_relevant_filters(
                        Decimal("1"),
                        Decimal("100"),
                        rules,
                        account_filters,
                        side=OrderSide.BUY,
                        account_open_state_verified_empty=True,
                    )

    def test_schema_drift_unknown_and_t_plus_sell_fail_closed(self) -> None:
        """
        함수 이름: test_schema_drift_unknown_and_t_plus_sell_fail_closed()
        기능: extra field, unknown type와 의미 미공개 T_PLUS_SELL을 모두 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        extra_field_payload = _account_filters_payload(
            symbol_filters=(
                {
                    "filterType": "MAX_NUM_ORDERS",
                    "maxNumOrders": 10,
                    "unexpected": 1,
                },
            ),
        )
        unknown_payload = _account_filters_payload(
            symbol_filters=(
                {"filterType": "FUTURE_FILTER", "limit": 1},
            ),
        )
        t_plus_payload = _account_filters_payload(
            symbol_filters=(
                {"filterType": "T_PLUS_SELL", "endTime": 1787374800000},
            ),
        )

        # 알 수 없는 값은 오류 문자열에 raw limit나 payload repr 없이 고정 분류로만 나타난다.
        with self.assertRaises(BinancePayloadError):
            parse_account_relevant_filters(extra_field_payload, "ETHUSDT")
        with self.assertRaisesRegex(BinancePayloadError, "unsupported"):
            parse_account_relevant_filters(unknown_payload, "ETHUSDT")
        with self.assertRaisesRegex(BinancePayloadError, "not documented"):
            parse_account_relevant_filters(t_plus_payload, "ETHUSDT")

    def test_unrelated_asset_fails_closed(self) -> None:
        """
        함수 이름: test_unrelated_asset_fails_closed()
        기능: 요청 symbol의 base·quote가 아닌 myFilters 자산을 무시하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        unrelated_filter = (
            AccountAssetFilter(
                filter_type="MAX_ASSET",
                asset="BNB",
                maximum_quantity=Decimal("100"),
            ),
        )

        # Relevant symbol 응답이라는 전제가 깨지면 임의 자산 적용 대신 submit을 차단한다.
        with self.assertRaisesRegex(SymbolFilterError, "SYMBOL_MISMATCH"):
            validate_account_asset_filters(
                Decimal("1"),
                Decimal("100"),
                _rules(),
                unrelated_filter,
            )


class ReferencePriceTests(unittest.TestCase):
    """
    클래스 이름: ReferencePriceTests
    기능: referencePrice의 exact symbol·Decimal·timestamp와 null fail-closed 계약을 검증한다.
    작성 날짜: 2026/08/31
    """

    def test_parser_returns_non_null_reference_price(self) -> None:
        """
        함수 이름: test_parser_returns_non_null_reference_price()
        기능: 공식 세 필드를 exact ReferencePrice DTO로 변환하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        payload = {
            "symbol": "ETHUSDT",
            "referencePrice": "4321.25000000",
            "timestamp": 1787374800000,
        }

        # 거래소 유효 시각과 Decimal scale을 raw object 없이 함께 보존한다.
        reference_price = parse_reference_price(payload, "ETHUSDT")  # Raw mapping은 DTO 뒤 폐기한다.

        self.assertEqual(
            reference_price,
            ReferencePrice(
                symbol="ETHUSDT",
                price=Decimal("4321.25000000"),
                exchange_timestamp=1787374800000,
            ),
        )

    def test_null_and_mismatched_reference_price_fail_closed(self) -> None:
        """
        함수 이름: test_null_and_mismatched_reference_price_fail_closed()
        기능: Null fallback 상태와 다른 symbol 응답을 가격 추정 없이 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        null_payload = {
            "symbol": "ETHUSDT",
            "referencePrice": None,
            "timestamp": 1787374800000,
        }
        mismatched_payload = {
            "symbol": "BTCUSDT",
            "referencePrice": "100",
            "timestamp": 1787374800000,
        }

        # Phase 13 경계에서는 공식 average-price fallback을 로컬에서 추정하지 않는다.
        with self.assertRaisesRegex(BinancePayloadError, "non-null"):
            parse_reference_price(null_payload, "ETHUSDT")
        with self.assertRaisesRegex(BinancePayloadError, "does not match"):
            parse_reference_price(mismatched_payload, "ETHUSDT")


if __name__ == "__main__":
    unittest.main()  # Direct module 실행도 discovery와 같은 strict assertions를 사용한다.
