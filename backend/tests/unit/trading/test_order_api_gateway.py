"""APIGateway의 Phase 8 normalized OrderResult 경계와 주문 경로 분리를 검증한다."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import unittest

from binance_auto_trader.adapters.binance.api_gateway import APIGateway
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


if __name__ == "__main__":
    unittest.main()
