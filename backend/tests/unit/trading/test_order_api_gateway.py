"""APIGateway의 Phase 8 normalized OrderResult 경계와 주문 경로 분리를 검증한다."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import unittest

from binance_auto_trader.adapters.binance.api_gateway import APIGateway
from binance_auto_trader.adapters.binance.mappers import (
    AccountAssetFilter,
    AccountRelevantFilters,
    NotionalFilter,
    OrderPreparationFilterEvidence,
    OrderSubmissionAttemptEvidence,
    QuantityFilter,
    ReferencePrice,
    SymbolTradingRules,
)
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.trading.order import (
    Order,
    OrderResult,
    OrderStatus,
)
from binance_auto_trader.domain.trading.states import (
    ExitReason,
    OrderSide,
    StrategyType,
)


PROCESSED_AT = datetime(2026, 8, 22, 2, 0, tzinfo=timezone.utc)


def _empty_account_relevant_filters() -> AccountRelevantFilters:
    """
    함수 이름: _empty_account_relevant_filters()
    기능: Gateway provenance 단위 테스트용 empty signed-filter DTO를 만든다.
    인자: 없음
    반환값: ETHUSDT에 결속된 AccountRelevantFilters
    작성 날짜: 2026/08/31
    """
    # Credential이나 raw response가 없는 immutable fixture만 Gateway에 전달한다.
    return AccountRelevantFilters(
        symbol="ETHUSDT",
        exchange_order_count_filters=(),
        symbol_order_count_filters=(),
        symbol_quantity_filters=(),
        symbol_notional_filters=(),
        symbol_maximum_position=None,
        passive_symbol_filter_types=frozenset(),
        asset_filters=(),
    )


def _symbol_trading_rules(
    *,
    symbol: str = "ETHUSDT",
) -> SymbolTradingRules:
    """
    함수 이름: _symbol_trading_rules()
    기능: Gateway public rule 반환 계약에 사용할 엄격 symbol rule fixture를 생성한다.
    인자: symbol -> fixture에 결속할 canonical symbol
    반환값: 모든 MARKET filter가 채워진 SymbolTradingRules
    작성 날짜: 2026/08/31
    """
    # 수량과 notional을 모두 채워 raw dictionary를 Gateway 밖으로 노출하지 않는다.
    return SymbolTradingRules(
        symbol=symbol,
        status="TRADING",
        base_asset="ETH",
        quote_asset="USDT",
        base_asset_precision=8,
        order_types=frozenset({"LIMIT", "MARKET"}),
        is_spot_trading_allowed=True,
        lot_size=QuantityFilter(
            filter_type="LOT_SIZE",
            minimum_quantity=Decimal("0.0001"),
            maximum_quantity=Decimal("1000"),
            step_size=Decimal("0.0001"),
        ),
        market_lot_size=QuantityFilter(
            filter_type="MARKET_LOT_SIZE",
            minimum_quantity=Decimal("0.001"),
            maximum_quantity=Decimal("100"),
            step_size=Decimal("0.001"),
        ),
        notional_filters=(
            NotionalFilter(
                filter_type="NOTIONAL",
                minimum_notional=Decimal("10"),
                maximum_notional=Decimal("100000"),
                apply_minimum_to_market=True,
                apply_maximum_to_market=True,
                average_price_minutes=5,
            ),
        ),
    )


class FakeSymbolRulesRESTClient:
    """
    클래스 이름: FakeSymbolRulesRESTClient
    기능: public symbol rule 호출과 설정된 반환값을 별도 order surface 없이 기록한다.
    작성 날짜: 2026/08/31
    """

    def __init__(self, result: object) -> None:
        """
        함수 이름: __init__()
        기능: 설정 반환값과 빈 symbol 호출 기록을 초기화한다.
        인자: result -> fetch_symbol_trading_rules의 반환 후보
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        self.result = result
        self.symbols: list[str] = []
        self.filter_evidence_result: object = None
        self.client_order_ids: list[str] = []
        self.submission_evidence_result: object = None
        self.submission_client_order_ids: list[str] = []
        self.account_filters_result: object = {
            "exchangeFilters": [],
            "symbolFilters": [],
            "assetFilters": [],
        }
        self.account_filter_symbols: list[str] = []
        self.reference_price_result: object = ReferencePrice(
            symbol="ETHUSDT",
            price=Decimal("100"),
            exchange_timestamp=1787374800000,
        )
        self.reference_price_symbols: list[str] = []

    def fetch_symbol_trading_rules(self, *, symbol: str) -> object:
        """
        함수 이름: fetch_symbol_trading_rules()
        기능: 전달된 symbol을 기록하고 설정된 rule 후보를 반환한다.
        인자: symbol -> Gateway가 canonical 형식으로 전달한 symbol
        반환값: 설정된 object
        작성 날짜: 2026/08/31
        """
        self.symbols.append(symbol)  # 호출별 신선 조회 위임을 횟수로 검증할 수 있게 한다.
        return self.result

    def get_order_preparation_filter_evidence(
        self,
        *,
        client_order_id: str,
    ) -> object:
        """
        함수 이름: get_order_preparation_filter_evidence()
        기능: 전달된 order identity를 기록하고 설정한 filter evidence 후보를 반환한다.
        인자: client_order_id -> Gateway가 전달한 application Order identity
        반환값: 설정된 evidence 후보
        작성 날짜: 2026/08/31
        """
        self.client_order_ids.append(client_order_id)  # Cache 우회와 identity 결속을 호출 기록으로 검증한다.
        return self.filter_evidence_result

    def get_order_submission_attempt_evidence(
        self,
        *,
        client_order_id: str,
    ) -> object:
        """
        함수 이름: get_order_submission_attempt_evidence()
        기능: 전달된 order identity를 기록하고 설정한 submission evidence 후보를 반환한다.
        인자: client_order_id -> Gateway가 전달한 application Order identity
        반환값: 설정된 evidence 후보
        작성 날짜: 2026/08/31
        """
        self.submission_client_order_ids.append(client_order_id)  # POST 시작 evidence도 별도 read로 센다.
        return self.submission_evidence_result

    def get_account_filters(self, *, symbol: str) -> object:
        """
        함수 이름: get_account_filters()
        기능: 전달된 symbol을 기록하고 설정한 raw myFilters 후보를 반환한다.
        인자: symbol -> Gateway가 canonical 형식으로 전달한 symbol
        반환값: 설정된 myFilters object
        작성 날짜: 2026/08/31
        """
        self.account_filter_symbols.append(symbol)  # Signed read의 canonical correlation을 기록한다.
        return self.account_filters_result

    def fetch_reference_price(self, *, symbol: str) -> object:
        """
        함수 이름: fetch_reference_price()
        기능: 전달된 symbol을 기록하고 설정한 reference price 후보를 반환한다.
        인자: symbol -> Gateway가 canonical 형식으로 전달한 symbol
        반환값: 설정된 reference price object
        작성 날짜: 2026/08/31
        """
        self.reference_price_symbols.append(symbol)  # Public read의 symbol 결속을 기록한다.
        return self.reference_price_result


def _order(
    *,
    side: OrderSide = OrderSide.BUY,
    client_order_id: str = "bat-intent-1-attempt-0",
) -> Order:
    """
    함수 이름: _order()
    기능: Gateway operation 호출에 사용할 유효한 ETHUSDT Order fixture를 생성한다.
    인자: side -> BUY 또는 SELL 주문 방향
        client_order_id -> 제출 시도별 고유 client order ID
    반환값: 아직 거래소 결과를 적용하지 않은 Order
    작성 날짜: 2026/08/22
    """
    exit_reason = ExitReason.STOP if side is OrderSide.SELL else None

    return Order(
        intent_id="intent-1",
        client_order_id=client_order_id,
        submission_attempt=0,
        symbol="ETHUSDT",
        side=side,
        strategy=StrategyType.CASE_B,
        regime_type=RegimeType.TYPE_0,
        requested_quantity=Decimal("1"),
        submitted_quantity=Decimal("1"),
        market_price_at_decision=Decimal("3000"),
        exit_reason=exit_reason,
    )


def _order_result(
    order: Order,
    *,
    status: OrderStatus,
    client_order_id: str | None = None,
) -> OrderResult:
    """
    함수 이름: _order_result()
    기능: 지정 Order와 연결된 fill 없는 normalized result fixture를 생성한다.
    인자: order -> symbol과 기본 client ID를 제공할 원 Order
        status -> fake client가 반환할 normalized OrderStatus
        client_order_id -> ID 불일치 검증에 사용할 선택 override
    반환값: immutable OrderResult
    작성 날짜: 2026/08/22
    """
    selected_client_order_id = (
        order.client_order_id
        if client_order_id is None
        else client_order_id
    )

    return OrderResult(
        symbol=order.symbol,
        client_order_id=selected_client_order_id,
        status=status,
        processed_at=PROCESSED_AT,
        exchange_order_id="9001",
    )


class FakeOrderRESTClient:
    """
    클래스 이름: FakeOrderRESTClient
    기능: operation별 반환값과 전달받은 Order identity를 결정론적으로 기록한다.
    작성 날짜: 2026/08/22
    """

    def __init__(
        self,
        *,
        submit_result: object,
        query_result: object,
        cancel_result: object,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 세 operation의 반환값과 빈 호출 기록을 초기화한다.
        인자: submit_result -> submit_order가 반환할 값
            query_result -> query_order_result가 반환할 값
            cancel_result -> cancel_order가 반환할 값
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # 반환값은 raw dict 거부 테스트를 위해 의도적으로 object 타입으로 보존한다.
        self.submit_result = submit_result
        self.query_result = query_result
        self.cancel_result = cancel_result
        self.submitted_orders: list[Order] = []
        self.queried_orders: list[Order] = []
        self.canceled_orders: list[Order] = []
        self.all_open_results: object = ()
        self.all_recent_results: object = ()
        self.all_open_symbols: list[str] = []
        self.all_recent_calls: list[tuple[str, int]] = []

    def submit_order(self, *, order: Order) -> object:
        """
        함수 이름: submit_order()
        기능: 신규 제출 호출과 정확한 Order identity를 기록한 뒤 설정 결과를 반환한다.
        인자: order -> Gateway가 전달한 Order
        반환값: 설정된 submit 결과
        작성 날짜: 2026/08/22
        """
        self.submitted_orders.append(order)  # 신규 제출 횟수와 identity를 함께 보존한다.
        return self.submit_result

    def query_order_result(self, *, order: Order) -> object:
        """
        함수 이름: query_order_result()
        기능: 기존 주문 조회 호출을 submit과 분리해 기록하고 설정 결과를 반환한다.
        인자: order -> Gateway가 전달한 기존 Order
        반환값: 설정된 query 결과
        작성 날짜: 2026/08/22
        """
        self.queried_orders.append(order)  # 같은 aggregate 조회 여부를 identity로 검증한다.
        return self.query_result

    def cancel_order(self, *, order: Order) -> object:
        """
        함수 이름: cancel_order()
        기능: 기존 주문 취소 호출을 기록하고 설정 결과를 반환한다.
        인자: order -> Gateway가 전달한 기존 Order
        반환값: 설정된 cancel 결과
        작성 날짜: 2026/08/22
        """
        self.canceled_orders.append(order)  # 취소가 별도 submit을 만들지 않는지 확인한다.
        return self.cancel_result

    def list_all_open_order_results(self, *, symbol: str) -> object:
        """
        함수 이름: list_all_open_order_results()
        기능: 격리용 전체 open-order symbol을 기록하고 설정 collection을 반환한다.
        인자: symbol -> Gateway가 전달한 canonical symbol
        반환값: 설정된 전체 open-order 후보
        작성 날짜: 2026/08/31
        """
        self.all_open_symbols.append(symbol)  # Prefix 없는 안전 조회가 별도 port를 탔는지 남긴다.
        return self.all_open_results

    def list_all_recent_order_results(
        self,
        *,
        symbol: str,
        limit: int,
    ) -> object:
        """
        함수 이름: list_all_recent_order_results()
        기능: 격리용 전체 recent-order symbol·limit를 기록하고 설정 collection을 반환한다.
        인자: symbol -> Gateway가 전달한 canonical symbol
            limit -> Gateway가 검증한 bounded limit
        반환값: 설정된 전체 recent-order 후보
        작성 날짜: 2026/08/31
        """
        self.all_recent_calls.append((symbol, limit))
        return self.all_recent_results  # Raw candidate 검증은 Gateway 책임으로 남긴다.


def _fake_client(
    order: Order,
    *,
    submit_result: object | None = None,
    query_result: object | None = None,
    cancel_result: object | None = None,
) -> FakeOrderRESTClient:
    """
    함수 이름: _fake_client()
    기능: override가 없는 operation에 각각 유효한 normalized 기본 결과를 채운다.
    인자: order -> 기본 result의 client ID와 symbol을 제공할 Order
        submit_result -> 선택 submit 반환값
        query_result -> 선택 query 반환값
        cancel_result -> 선택 cancel 반환값
    반환값: 호출 기록이 비어 있는 FakeOrderRESTClient
    작성 날짜: 2026/08/22
    """
    # None은 테스트 override가 아니라 operation별 정상 normalized 결과 선택을 뜻한다.
    selected_submit_result = (
        _order_result(order, status=OrderStatus.NEW)
        if submit_result is None
        else submit_result
    )
    selected_query_result = (
        _order_result(order, status=OrderStatus.PARTIALLY_FILLED)
        if query_result is None
        else query_result
    )
    selected_cancel_result = (
        _order_result(order, status=OrderStatus.CANCELED)
        if cancel_result is None
        else cancel_result
    )

    return FakeOrderRESTClient(
        submit_result=selected_submit_result,
        query_result=selected_query_result,
        cancel_result=selected_cancel_result,
    )


class APIGatewaySymbolRulesTests(unittest.TestCase):
    """
    클래스 이름: APIGatewaySymbolRulesTests
    기능: public symbol rule port의 canonical 입력과 exact domain 반환 경계를 검증한다.
    작성 날짜: 2026/08/31
    """

    def test_fetch_symbol_rules_forwards_each_canonical_public_read(
        self,
    ) -> None:
        """
        함수 이름: test_fetch_symbol_rules_forwards_each_canonical_public_read()
        기능: Gateway가 연속 요청을 각각 port에 위임하고 동일 rule identity를 반환하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        rules = _symbol_trading_rules()
        client = FakeSymbolRulesRESTClient(rules)
        gateway = APIGateway(client)

        # Gateway 자체 cache를 두지 않아 각 호출이 REST client의 fresh GET 계약으로 이어진다.
        first_result = gateway.fetch_symbol_trading_rules(" ethusdt ")
        second_result = gateway.fetch_symbol_trading_rules("ETHUSDT")

        self.assertIs(first_result, rules)
        self.assertIs(second_result, rules)
        self.assertEqual(client.symbols, ["ETHUSDT", "ETHUSDT"])

    def test_fetch_symbol_rules_rejects_raw_or_mismatched_results(self) -> None:
        """
        함수 이름: test_fetch_symbol_rules_rejects_raw_or_mismatched_results()
        기능: raw mapping과 다른 symbol의 domain rule이 Gateway 밖으로 나가지 못하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        invalid_results = (
            ({"symbol": "ETHUSDT"}, TypeError),
            (_symbol_trading_rules(symbol="BTCUSDT"), ValueError),
        )

        # 타입 오류와 symbol correlation 오류를 독립 Gateway로 분리해 인과를 고정한다.
        for invalid_result, expected_error in invalid_results:
            with self.subTest(expected_error=expected_error.__name__):
                client = FakeSymbolRulesRESTClient(invalid_result)
                gateway = APIGateway(client)

                with self.assertRaises(expected_error):
                    gateway.fetch_symbol_trading_rules("ETHUSDT")

                self.assertEqual(client.symbols, ["ETHUSDT"])

    def test_fetch_account_asset_filters_normalizes_signed_payload(
        self,
    ) -> None:
        """
        함수 이름: test_fetch_account_asset_filters_normalizes_signed_payload()
        기능: Gateway가 raw myFilters를 strict MAX_ASSET tuple로 축약하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        client = FakeSymbolRulesRESTClient(_symbol_trading_rules())
        client.account_filters_result = {
            "exchangeFilters": [],
            "symbolFilters": [],
            "assetFilters": [
                {
                    "filterType": "MAX_ASSET",
                    "asset": "USDT",
                    "limit": "250.00000000",
                }
            ],
        }
        gateway = APIGateway(client)

        # Lowercase 입력은 canonical symbol로 전달하고 raw collection은 DTO 하나로 제한한다.
        result = gateway.fetch_account_asset_filters(" ethusdt ")

        self.assertEqual(
            result,
            (
                AccountAssetFilter(
                    filter_type="MAX_ASSET",
                    asset="USDT",
                    maximum_quantity=Decimal("250.00000000"),
                ),
            ),
        )
        self.assertEqual(client.account_filter_symbols, ["ETHUSDT"])

    def test_fetch_reference_price_requires_exact_correlated_dto(self) -> None:
        """
        함수 이름: test_fetch_reference_price_requires_exact_correlated_dto()
        기능: Gateway가 ReferencePrice exact 타입과 요청 symbol 일치를 강제하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        client = FakeSymbolRulesRESTClient(_symbol_trading_rules())
        gateway = APIGateway(client)

        # 정상 DTO identity는 유지하고 raw mapping과 다른 symbol은 각각 차단한다.
        expected_reference_price = client.reference_price_result
        self.assertIs(
            gateway.fetch_reference_price("ethusdt"),
            expected_reference_price,
        )
        client.reference_price_result = {"referencePrice": "100"}
        with self.assertRaises(TypeError):
            gateway.fetch_reference_price("ETHUSDT")
        client.reference_price_result = ReferencePrice(
            symbol="BTCUSDT",
            price=Decimal("100"),
            exchange_timestamp=1787374800000,
        )
        with self.assertRaises(ValueError):
            gateway.fetch_reference_price("ETHUSDT")

    def test_get_order_submission_provenance_is_exact_and_correlated(
        self,
    ) -> None:
        """
        함수 이름: test_get_order_submission_provenance_is_exact_and_correlated()
        기능: filter/submission evidence가 frozen DTO와 요청 client ID를 만족해야 하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        rules = _symbol_trading_rules()
        evidence = OrderPreparationFilterEvidence(
            intent_id="filter-evidence-intent",
            client_order_id="bat-filter-evidence-client",
            side=OrderSide.BUY,
            observed_at=PROCESSED_AT,
            rules=rules,
            account_filters=_empty_account_relevant_filters(),
            account_filters_observed_at=PROCESSED_AT,
            account_open_orders_observed_at=PROCESSED_AT,
            account_open_order_lists_observed_at=PROCESSED_AT,
            account_open_state_verified_empty=True,
            reference_price=ReferencePrice(
                symbol="ETHUSDT",
                price=Decimal("100"),
                exchange_timestamp=1787374800000,
            ),
            reference_price_observed_at=PROCESSED_AT,
        )
        submission_evidence = OrderSubmissionAttemptEvidence(
            intent_id="filter-evidence-intent",
            client_order_id="bat-filter-evidence-client",
            side=OrderSide.BUY,
            attempted_at=PROCESSED_AT,
        )
        client = FakeSymbolRulesRESTClient(rules)
        client.filter_evidence_result = evidence
        client.submission_evidence_result = submission_evidence
        gateway = APIGateway(client)

        # Getter는 network mutation 없이 same-client frozen evidence identity를 그대로 보존한다.
        result = gateway.get_order_preparation_filter_evidence(
            "bat-filter-evidence-client"
        )
        submission_result = gateway.get_order_submission_attempt_evidence(
            "bat-filter-evidence-client"
        )

        self.assertIs(evidence, result)
        self.assertIs(submission_evidence, submission_result)
        self.assertEqual(
            ["bat-filter-evidence-client"],
            client.client_order_ids,
        )
        self.assertEqual(
            ["bat-filter-evidence-client"],
            client.submission_client_order_ids,
        )

        # Raw mapping이나 다른 client identity는 provenance로 확대 해석하지 않는다.
        for invalid_evidence, expected_error in (
            ({"client_order_id": "bat-filter-evidence-client"}, TypeError),
            (
                OrderPreparationFilterEvidence(
                    intent_id="other-filter-intent",
                    client_order_id="bat-other-filter-client",
                    side=OrderSide.BUY,
                    observed_at=PROCESSED_AT,
                    rules=rules,
                    account_filters=_empty_account_relevant_filters(),
                    account_filters_observed_at=PROCESSED_AT,
                    account_open_orders_observed_at=PROCESSED_AT,
                    account_open_order_lists_observed_at=PROCESSED_AT,
                    account_open_state_verified_empty=True,
                    reference_price=ReferencePrice(
                        symbol="ETHUSDT",
                        price=Decimal("100"),
                        exchange_timestamp=1787374800000,
                    ),
                    reference_price_observed_at=PROCESSED_AT,
                ),
                ValueError,
            ),
        ):
            with self.subTest(expected_error=expected_error.__name__):
                client.filter_evidence_result = invalid_evidence
                with self.assertRaises(expected_error):
                    gateway.get_order_preparation_filter_evidence(
                        "bat-filter-evidence-client"
                    )


class APIGatewayOrderTests(unittest.TestCase):
    """
    클래스 이름: APIGatewayOrderTests
    기능: Phase 8 fake order port의 normalized result와 operation 분리를 검증한다.
    작성 날짜: 2026/08/22
    """

    def test_submit_query_and_cancel_return_domain_order_results(self) -> None:
        """
        함수 이름: test_submit_query_and_cancel_return_domain_order_results()
        기능: 세 order operation이 같은 Order를 전달하고 domain OrderResult만 반환하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        order = _order()
        client = _fake_client(order)
        gateway = APIGateway(client)

        # 각 operation을 한 번씩 호출해 fake port의 normalized 결과 identity를 보존한다.
        submitted_result = gateway.submit_order(order)
        queried_result = gateway.query_order_result(order)
        canceled_result = gateway.cancel_order(order)

        self.assertIs(submitted_result, client.submit_result)
        self.assertIs(queried_result, client.query_result)
        self.assertIs(canceled_result, client.cancel_result)
        self.assertIsInstance(submitted_result, OrderResult)
        self.assertIsInstance(queried_result, OrderResult)
        self.assertIsInstance(canceled_result, OrderResult)
        self.assertEqual(client.submitted_orders, [order])
        self.assertEqual(client.queried_orders, [order])
        self.assertEqual(client.canceled_orders, [order])

    def test_all_operations_reject_a_different_client_order_id(self) -> None:
        """
        함수 이름: test_all_operations_reject_a_different_client_order_id()
        기능: submit/query/cancel이 다른 주문의 normalized result를 동일 주문으로 받지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        order = _order()
        mismatched_result = _order_result(
            order,
            status=OrderStatus.NEW,
            client_order_id="different-client-id",
        )

        # operation마다 같은 ID correlation guard가 적용되는지 독립 Gateway로 확인한다.
        operation_names = (
            "submit_order",
            "query_order_result",
            "cancel_order",
        )
        for operation_name in operation_names:
            with self.subTest(operation_name=operation_name):
                client = _fake_client(
                    order,
                    submit_result=mismatched_result,
                    query_result=mismatched_result,
                    cancel_result=mismatched_result,
                )
                gateway = APIGateway(client)

                with self.assertRaises(ValueError):
                    getattr(gateway, operation_name)(order)

    def test_all_operations_reject_raw_dict_results(self) -> None:
        """
        함수 이름: test_all_operations_reject_raw_dict_results()
        기능: fake client의 raw exchange payload가 domain 경계 밖으로 유출되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        order = _order()
        raw_result = {
            "symbol": order.symbol,
            "clientOrderId": order.client_order_id,
            "status": "NEW",
        }

        # 세 operation 모두 mapping이 아니라 normalized OrderResult만 허용해야 한다.
        for operation_name in (
            "submit_order",
            "query_order_result",
            "cancel_order",
        ):
            with self.subTest(operation_name=operation_name):
                client = _fake_client(
                    order,
                    submit_result=raw_result,
                    query_result=raw_result,
                    cancel_result=raw_result,
                )
                gateway = APIGateway(client)

                with self.assertRaises(TypeError):
                    getattr(gateway, operation_name)(order)

    def test_query_uses_only_query_port_and_never_submits_order(self) -> None:
        """
        함수 이름: test_query_uses_only_query_port_and_never_submits_order()
        기능: UNKNOWN/active 주문 조회가 새 주문 제출 operation을 호출하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        order = _order()
        client = _fake_client(order)
        gateway = APIGateway(client)

        queried_result = gateway.query_order_result(order)

        # 조회는 같은 Order identity만 query port에 전달하고 submit/cancel 기록은 비워 둔다.
        self.assertIs(queried_result, client.query_result)
        self.assertEqual(client.queried_orders, [order])
        self.assertEqual(client.submitted_orders, [])
        self.assertEqual(client.canceled_orders, [])

    def test_sell_all_accepts_only_sell_and_reuses_submit_path(self) -> None:
        """
        함수 이름: test_sell_all_accepts_only_sell_and_reuses_submit_path()
        기능: force-sell이 SELL guard 뒤 일반 submit pipeline만 정확히 한 번 재사용하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        sell_order = _order(
            side=OrderSide.SELL,
            client_order_id="bat-force-sell-attempt-0",
        )
        client = _fake_client(sell_order)
        gateway = APIGateway(client)

        # 전량 SELL도 별도 REST operation 없이 일반 submit_order에 동일 Order를 전달한다.
        result = gateway.sell_all_position(sell_order)
        self.assertIs(result, client.submit_result)
        self.assertEqual(client.submitted_orders, [sell_order])
        self.assertEqual(client.queried_orders, [])
        self.assertEqual(client.canceled_orders, [])

        # BUY를 force-sell로 잘못 전달하면 fake client 호출 전에 fail closed한다.
        buy_order = _order(side=OrderSide.BUY)
        with self.assertRaises(ValueError):
            gateway.sell_all_position(buy_order)
        self.assertEqual(client.submitted_orders, [sell_order])

    def test_full_account_order_queries_preserve_non_application_client_ids(
        self,
    ) -> None:
        """
        함수 이름: test_full_account_order_queries_preserve_non_application_client_ids()
        기능: 격리용 open/recent 조회가 manual client ID 결과도 collection guard 뒤 반환하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        order = _order()
        manual_result = _order_result(
            order,
            status=OrderStatus.NEW,
            client_order_id="manual-client-id",
        )
        client = _fake_client(order)
        client.all_open_results = (manual_result,)
        client.all_recent_results = (manual_result,)
        gateway = APIGateway(client)

        # Prefix 없는 두 port 모두 canonical symbol과 caller limit만 받아 같은 manual result를 보존한다.
        self.assertEqual(
            (manual_result,),
            gateway.list_all_open_order_results(" ethusdt "),
        )
        self.assertEqual(
            (manual_result,),
            gateway.list_all_recent_order_results("ETHUSDT", limit=77),
        )
        self.assertEqual(["ETHUSDT"], client.all_open_symbols)
        self.assertEqual([("ETHUSDT", 77)], client.all_recent_calls)


if __name__ == "__main__":
    unittest.main()
