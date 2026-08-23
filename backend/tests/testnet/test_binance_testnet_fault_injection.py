"""Opt-in testnet suite에서 timeout·disconnect same-ID 불변식을 결정적으로 주입 검증한다."""

from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
import unittest

from binance_auto_trader.adapters.binance.api_gateway import APIGateway
from binance_auto_trader.adapters.binance.websocket_gateway import (
    WebSocketGateway,
)
from binance_auto_trader.domain.trading.order import (
    Fill,
    Order,
    OrderResult,
    OrderStatus,
)
from binance_auto_trader.domain.trading.states import OrderSide

from tests.testnet._support import (
    READ_ONLY_SKIP_REASON,
    READ_ONLY_TESTNET_REQUESTED,
    build_testnet_order,
    submit_and_wait_for_terminal,
    unix_milliseconds,
)


FAULT_TIME = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
FAULT_EXCHANGE_ORDER_ID = "9001"
FAULT_TRADE_ID = "77"


class _AcceptedResponseTimeoutTransport:
    """
    클래스 이름: _AcceptedResponseTimeoutTransport
    기능: 주문을 한 번 수락한 뒤 submit 응답만 timeout시키고 same-ID query 사실을 보존한다.
    작성 날짜: 2026/08/22
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 비어 있는 accepted 결과 index와 submit/query 호출 trace를 만든다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self.accepted_results: dict[str, OrderResult] = {}
        self.submission_ids: list[str] = []
        self.query_ids: list[str] = []  # 새 제출과 same-ID 조회를 분리해 중복을 검출한다.

    def submit(self, order: Order) -> OrderResult:
        """
        함수 이름: submit()
        기능: canonical FILLED 결과를 accepted index에 저장한 뒤 response timeout을 발생시킨다.
        인자: order -> transport가 수락할 제출 전 Order
        반환값: 정상 반환 없이 TimeoutError 발생
        작성 날짜: 2026/08/22
        """
        if order.client_order_id in self.accepted_results:
            raise AssertionError("duplicate client order submission detected")
        self.submission_ids.append(order.client_order_id)
        fill = Fill(
            exchange_order_id=FAULT_EXCHANGE_ORDER_ID,
            trade_id=FAULT_TRADE_ID,
            quantity=order.submitted_quantity,
            price=order.market_price_at_decision,
            fee_amount=Decimal("0.10"),
            fee_asset="USDT",
            fee_quote_amount=Decimal("0.10"),
            executed_at=FAULT_TIME,
        )
        self.accepted_results[order.client_order_id] = OrderResult(
            symbol=order.symbol,
            client_order_id=order.client_order_id,
            status=OrderStatus.FILLED,
            processed_at=FAULT_TIME,
            exchange_order_id=FAULT_EXCHANGE_ORDER_ID,
            fills=(fill,),
        )

        raise TimeoutError("injected accepted-response timeout")  # exchange 수락 뒤 client response만 유실한다.

    def query(self, order: Order) -> OrderResult:
        """
        함수 이름: query()
        기능: 신규 제출 없이 같은 client ID의 accepted 결과를 반환한다.
        인자: order -> 조회 identity를 가진 기존 Order
        반환값: 앞서 수락한 FILLED OrderResult
        작성 날짜: 2026/08/22
        """
        self.query_ids.append(order.client_order_id)
        return self.accepted_results[order.client_order_id]  # 다른 ID fallback이나 재생성은 허용하지 않는다.


class _TransportBackedRESTClient:
    """
    클래스 이름: _TransportBackedRESTClient
    기능: APIGateway port를 injected accepted-timeout transport에 연결한다.
    작성 날짜: 2026/08/22
    """

    def __init__(self, transport: _AcceptedResponseTimeoutTransport) -> None:
        """
        함수 이름: __init__()
        기능: 결정적 REST fault transport identity를 보존한다.
        인자: transport -> submit/query 동작을 제공할 injected transport
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self._transport = transport  # client port와 fault source를 별도 계층으로 유지한다.

    def submit_order(self, *, order: Order) -> OrderResult:
        """
        함수 이름: submit_order()
        기능: injected transport의 accepted-response timeout submit을 호출한다.
        인자: order -> 제출할 canonical Order
        반환값: 정상 반환 없이 transport TimeoutError 발생
        작성 날짜: 2026/08/22
        """
        return self._transport.submit(order)  # APIGateway 밖에서 timeout을 삼키지 않는다.

    def query_order_result(self, *, order: Order) -> OrderResult:
        """
        함수 이름: query_order_result()
        기능: injected transport의 same-ID query 결과를 반환한다.
        인자: order -> 조회할 기존 Order
        반환값: accepted FILLED OrderResult
        작성 날짜: 2026/08/22
        """
        return self._transport.query(order)  # query path에는 submit operation이 존재하지 않는다.


class _InjectedSubscription:
    """
    클래스 이름: _InjectedSubscription
    기능: WebSocketGateway가 닫을 수 있는 멱등 in-memory subscription을 제공한다.
    작성 날짜: 2026/08/22
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 열려 있는 초기 subscription 상태를 만든다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self.closed = False  # 실제 thread나 socket 없이 close state만 기록한다.

    def close(self) -> None:
        """
        함수 이름: close()
        기능: injected subscription을 멱등 종료한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self.closed = True  # 반복 close도 외부 effect 없는 동일 상태로 수렴한다.


class _DisconnectingWebSocketTransport:
    """
    클래스 이름: _DisconnectingWebSocketTransport
    기능: account event와 disconnect callback을 test 순서대로 직접 주입한다.
    작성 날짜: 2026/08/22
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 아직 구독되지 않은 callback과 subscription 상태를 준비한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self.on_message: Callable[[object], None] | None = None
        self.on_disconnect: Callable[[], None] | None = None
        self.subscription = _InjectedSubscription()  # 한 account stream 세대만 deterministic하게 사용한다.

    def subscribe_account_info(
        self,
        *,
        on_message: Callable[[object], None],
        on_disconnect: Callable[[], None],
    ) -> _InjectedSubscription:
        """
        함수 이름: subscribe_account_info()
        기능: Gateway가 제공한 message와 disconnect callback을 보존한다.
        인자: on_message -> executionReport 수신 callback
            on_disconnect -> transport disconnect callback
        반환값: injected subscription handle
        작성 날짜: 2026/08/22
        """
        self.on_message = on_message
        self.on_disconnect = on_disconnect

        return self.subscription  # 연결 startup은 callback 저장과 동시에 완료된 것으로 본다.

    def emit(self, payload: object) -> None:
        """
        함수 이름: emit()
        기능: 저장한 account message callback에 한 payload를 동기 전달한다.
        인자: payload -> 공식 executionReport envelope fixture
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        if self.on_message is None:
            raise AssertionError("account transport is not subscribed")
        self.on_message(payload)  # test thread 안에서 callback 완료까지 결정적으로 기다린다.

    def disconnect(self) -> None:
        """
        함수 이름: disconnect()
        기능: 저장한 disconnect callback을 정확히 한 번 동기 호출한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        if self.on_disconnect is None:
            raise AssertionError("account transport is not subscribed")
        disconnect_callback = self.on_disconnect
        self.on_disconnect = None
        disconnect_callback()  # callback 제거 뒤 호출해 test 자체의 중복 disconnect를 막는다.


def _execution_report(
    order: Order,
    *,
    cumulative_quantity: Decimal,
    last_quantity: Decimal,
    fee_amount: Decimal,
    trade_id: int,
    execution_id: int,
    status: OrderStatus,
) -> dict[str, object]:
    """
    함수 이름: _execution_report()
    기능: partial cumulative sequence에 사용할 공식 executionReport envelope를 만든다.
    인자: order -> client ID와 가격 근거를 제공할 Order
        cumulative_quantity -> event까지의 누적 체결 수량
        last_quantity -> 이번 event의 체결 수량
        fee_amount -> 이번 fill의 USDT 수수료
        trade_id -> 이번 fill의 Binance trade ID
        execution_id -> source ordering execution ID
        status -> PARTIALLY_FILLED 또는 FILLED 상태
    반환값: WebSocketGateway가 해석할 event envelope
    작성 날짜: 2026/08/22
    """
    event_time = unix_milliseconds(FAULT_TIME)
    return {
        "subscriptionId": 0,
        "event": {
            "e": "executionReport",
            "E": event_time,
            "T": event_time,
            "I": execution_id,
            "s": order.symbol,
            "c": order.client_order_id,
            "C": "",
            "i": int(FAULT_EXCHANGE_ORDER_ID),
            "x": "TRADE",
            "X": status.value,
            "z": format(cumulative_quantity, "f"),
            "l": format(last_quantity, "f"),
            "L": format(order.market_price_at_decision, "f"),
            "n": format(fee_amount, "f"),
            "N": "USDT",
            "t": trade_id,
            "r": "NONE",
        },
    }  # 누적 수량은 Gateway가 개별 fill 합계와 정확히 대조한다.


@unittest.skipUnless(
    READ_ONLY_TESTNET_REQUESTED,
    READ_ONLY_SKIP_REASON,
)
class BinanceTestnetFaultInjectionTests(unittest.TestCase):
    """
    클래스 이름: BinanceTestnetFaultInjectionTests
    기능: accepted timeout과 stream disconnect가 same-ID query 및 fill 멱등성을 지키는지 검증한다.
    작성 날짜: 2026/08/22
    """

    def _create_fault_fixture(
        self,
    ) -> tuple[Order, _AcceptedResponseTimeoutTransport, APIGateway]:
        """
        함수 이름: _create_fault_fixture()
        기능: 한 BUY Order와 injected REST transport를 APIGateway에 조립한다.
        인자: 없음
        반환값: Order, transport와 APIGateway tuple
        작성 날짜: 2026/08/22
        """
        order = build_testnet_order(
            side=OrderSide.BUY,
            quantity=Decimal("1"),
            decision_price=Decimal("100"),
            client_order_id="bat-t9-injected-timeout-b",
        )
        transport = _AcceptedResponseTimeoutTransport()
        api_gateway = APIGateway(
            _TransportBackedRESTClient(transport),
            clock=lambda: FAULT_TIME,
        )

        return order, transport, api_gateway  # 각 test가 독립 same-ID trace를 소유한다.

    def test_accepted_response_timeout_queries_same_id_without_resubmit(
        self,
    ) -> None:
        """
        함수 이름: test_accepted_response_timeout_queries_same_id_without_resubmit()
        기능: 수락 뒤 timeout이 신규 submit 없이 동일 client ID query로 FILLED에 수렴하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        order, transport, api_gateway = self._create_fault_fixture()

        # helper는 첫 submit 예외 이후 같은 aggregate query만 수행해야 한다.
        result = submit_and_wait_for_terminal(api_gateway, order)
        self.assertIs(result.status, OrderStatus.FILLED)
        self.assertEqual(transport.submission_ids, [order.client_order_id])
        self.assertEqual(transport.query_ids, [order.client_order_id])
        self.assertEqual(tuple(transport.accepted_results), (order.client_order_id,))
        self.assertEqual(len(order.fills), 1)

    def test_partial_cumulative_stream_then_disconnect_reconciles_without_duplicates(
        self,
    ) -> None:
        """
        함수 이름: test_partial_cumulative_stream_then_disconnect_reconciles_without_duplicates()
        기능: partial 누적 fill, 중복 terminal event와 disconnect 뒤 fill 멱등·reconciliation을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        order = build_testnet_order(
            side=OrderSide.BUY,
            quantity=Decimal("1"),
            decision_price=Decimal("100"),
            client_order_id="bat-t9-injected-partial-b",
        )
        web_socket_transport = _DisconnectingWebSocketTransport()
        observed_results: list[OrderResult] = []
        reconciliation_reasons: list[str] = []

        def observe_result(result: OrderResult) -> None:
            """
            함수 이름: observe_result()
            기능: stream 누적 결과를 같은 Order aggregate에 idempotent 재적용한다.
            인자: result -> WebSocketGateway가 정규화한 OrderResult
            반환값: 없음
            작성 날짜: 2026/08/22
            """
            observed_results.append(result)
            if order.status is None:
                order.apply_order_result(result)
            else:
                order.reapply_order_result(result)  # 각 후속 cumulative fill은 같은 aggregate에 합쳐진다.

        web_socket_gateway = WebSocketGateway(
            web_socket_transport,
            order_result_callback=observe_result,
            reconciliation_required_callback=reconciliation_reasons.append,
        )
        subscription = web_socket_gateway.start_account_info_stream()
        partial_payload = _execution_report(
            order,
            cumulative_quantity=Decimal("0.4"),
            last_quantity=Decimal("0.4"),
            fee_amount=Decimal("0.04"),
            trade_id=76,
            execution_id=1,
            status=OrderStatus.PARTIALLY_FILLED,
        )
        filled_payload = _execution_report(
            order,
            cumulative_quantity=Decimal("1"),
            last_quantity=Decimal("0.6"),
            fee_amount=Decimal("0.06"),
            trade_id=77,
            execution_id=2,
            status=OrderStatus.FILLED,
        )

        # PARTIAL 뒤 terminal cumulative event를 보내고 terminal event 자체는 한 번 중복 주입한다.
        web_socket_transport.emit(partial_payload)
        web_socket_transport.emit(filled_payload)
        web_socket_transport.emit(filled_payload)
        web_socket_transport.disconnect()

        self.assertEqual(len(observed_results), 2)
        self.assertEqual(len(order.fills), 2)
        self.assertEqual(order.filled_quantity, Decimal("1"))
        self.assertIs(order.status, OrderStatus.FILLED)
        self.assertTrue(
            all(
                result.client_order_id == order.client_order_id
                for result in observed_results
            )
        )
        self.assertEqual(reconciliation_reasons, ["account_stream_disconnected"])
        self.assertFalse(web_socket_gateway.account_connected)
        subscription.close()  # explicit owner close 뒤에는 두 번째 reconciliation 알림이 없어야 한다.
        self.assertEqual(reconciliation_reasons, ["account_stream_disconnected"])


if __name__ == "__main__":
    unittest.main()
