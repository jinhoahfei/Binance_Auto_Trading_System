"""Spot testnet WebSocket transport와 executionReport Gateway 경계를 검증한다."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import hmac
import json
from threading import Event, RLock, Thread
import unittest

from binance_auto_trader.adapters.binance.spot_websocket_client import (
    BinanceSpotWebSocketClient,
    WebSocketSubscriptionError,
    _ConnectionLifecycle,
)
from binance_auto_trader.adapters.binance.websocket_gateway import (
    AccountStreamStateError,
    WebSocketGateway,
)
from binance_auto_trader.domain.trading import Fill, OrderResult, OrderStatus


FIXED_TIMESTAMP_MILLISECONDS = 1_787_356_800_000
API_KEY = "testnet-api-key"
API_SECRET = "testnet-api-secret"
REQUEST_ID = "account-subscription-1"


class _ScriptedSocket:
    """
    클래스 이름: _ScriptedSocket
    기능: background run loop와 수동 frame·disconnect를 제공하는 websocket-client 호환 fake다.
    작성 날짜: 2026/08/22
    """

    def __init__(
        self,
        url: str,
        *,
        on_open: Callable[[object], None],
        on_message: Callable[[object, object], None],
        on_error: Callable[[object, object], None],
        on_close: Callable[[object, object, object], None],
        subscription_status: int,
        subscription_error_code: object,
    ) -> None:
        """
        함수 이름: __init__()
        기능: URL, callback과 signature subscription 응답 상태를 보존한다.
        인자: url -> client가 선택한 testnet URL
            on_open -> transport open callback
            on_message -> transport message callback
            on_error -> transport error callback
            on_close -> transport close callback
            subscription_status -> account ACK에 사용할 HTTP 유사 status
            subscription_error_code -> non-200 ACK에 넣을 untrusted error.code
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self.url = url
        self.sent_payloads: list[str] = []
        self._on_open = on_open
        self._on_message = on_message
        self._on_error = on_error
        self._on_close = on_close
        self._subscription_status = subscription_status
        self._subscription_error_code = subscription_error_code
        self._closed_event = Event()
        self._lock = RLock()
        self._closed = False

    def send(self, payload: str) -> None:
        """
        함수 이름: send()
        기능: account subscription 요청을 기록하고 같은 request ID의 결정적 ACK를 보낸다.
        인자: payload -> client가 전송한 JSON text
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self.sent_payloads.append(payload)
        parsed_payload = json.loads(payload)
        if parsed_payload.get("method") != (
            "userDataStream.subscribe.signature"
        ):
            raise AssertionError("unexpected WebSocket API method")

        # 정상 ACK와 credential 오류 응답을 같은 request correlation 경계로 돌려준다.
        if self._subscription_status == 200:
            response: dict[str, object] = {
                "id": parsed_payload["id"],
                "status": 200,
                "result": {"subscriptionId": 7},
            }
        else:
            response = {
                "id": parsed_payload["id"],
                "status": self._subscription_status,
                "error": {
                    "code": self._subscription_error_code,
                    "msg": "rejected",
                },
            }
        self._on_message(self, json.dumps(response))

    def run_forever(self) -> None:
        """
        함수 이름: run_forever()
        기능: open callback을 호출한 뒤 명시적 close까지 fake 수신 loop를 유지한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self._on_open(self)
        self._closed_event.wait(2)  # 단위 테스트 hang을 막는 상한만 두고 실제 sleep은 하지 않는다.

    def emit(self, payload: object) -> None:
        """
        함수 이름: emit()
        기능: 연결된 client에 JSON frame 하나를 동기 전달한다.
        인자: payload -> JSON 직렬화할 frame object
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self._on_message(self, json.dumps(payload))

    def fail_and_close(self) -> None:
        """
        함수 이름: fail_and_close()
        기능: on_error 뒤 on_close가 이어지는 실제 transport 종료 순서를 재현한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self._on_error(self, RuntimeError("socket failed"))
        self.close()

    def close(self) -> None:
        """
        함수 이름: close()
        기능: fake 연결을 멱등 종료하고 close callback을 한 번 호출한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._closed_event.set()

        self._on_close(self, 1000, "closed")


class _ScriptedSocketFactory:
    """
    클래스 이름: _ScriptedSocketFactory
    기능: 생성 URL과 callback이 연결된 scripted socket을 순서대로 기록한다.
    작성 날짜: 2026/08/22
    """

    def __init__(
        self,
        subscription_status: int = 200,
        subscription_error_code: object = -2015,
    ) -> None:
        """
        함수 이름: __init__()
        기능: account ACK status와 빈 socket 생성 기록을 초기화한다.
        인자: subscription_status -> signature subscription 응답 status
            subscription_error_code -> non-200 ACK에 넣을 error.code
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self.subscription_status = subscription_status
        self.subscription_error_code = subscription_error_code
        self.sockets: list[_ScriptedSocket] = []

    def __call__(
        self,
        url: str,
        *,
        on_open: Callable[[object], None],
        on_message: Callable[[object, object], None],
        on_error: Callable[[object, object], None],
        on_close: Callable[[object, object, object], None],
    ) -> _ScriptedSocket:
        """
        함수 이름: __call__()
        기능: websocket-client callback shape를 보존한 fake socket을 생성한다.
        인자: url -> client가 선택한 URL
            on_open -> transport open callback
            on_message -> transport message callback
            on_error -> transport error callback
            on_close -> transport close callback
        반환값: 새 scripted socket
        작성 날짜: 2026/08/22
        """
        socket = _ScriptedSocket(
            url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
            subscription_status=self.subscription_status,
            subscription_error_code=self.subscription_error_code,
        )
        self.sockets.append(socket)

        return socket


class _FakeSubscription:
    """
    클래스 이름: _FakeSubscription
    기능: Gateway가 오류 시 실제 account transport를 닫았는지 기록한다.
    작성 날짜: 2026/08/22
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 열려 있는 fake subscription 상태를 초기화한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self.closed = False

    def close(self) -> None:
        """
        함수 이름: close()
        기능: fake subscription을 멱등 종료한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self.closed = True


class _FakeGatewayClient:
    """
    클래스 이름: _FakeGatewayClient
    기능: Gateway account generation별 message·disconnect callback과 handle을 보존한다.
    작성 날짜: 2026/08/22
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 빈 callback과 subscription 기록을 초기화한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self.message_callbacks: list[Callable[[object], None]] = []
        self.disconnect_callbacks: list[Callable[[], None]] = []
        self.subscriptions: list[_FakeSubscription] = []

    def subscribe_account_info(
        self,
        *,
        on_message: Callable[[object], None],
        on_disconnect: Callable[[], None],
    ) -> _FakeSubscription:
        """
        함수 이름: subscribe_account_info()
        기능: 현재 account 구독 callback을 기록하고 새 fake handle을 반환한다.
        인자: on_message -> User Data Stream message callback
            on_disconnect -> 비정상 종료 callback
        반환값: 새 fake subscription
        작성 날짜: 2026/08/22
        """
        subscription = _FakeSubscription()
        self.message_callbacks.append(on_message)
        self.disconnect_callbacks.append(on_disconnect)
        self.subscriptions.append(subscription)

        return subscription

    def emit(self, generation_index: int, payload: object) -> None:
        """
        함수 이름: emit()
        기능: 선택한 account generation callback에 payload를 동기 전달한다.
        인자: generation_index -> 0부터 시작하는 구독 세대 순번
            payload -> 공식 User Data Stream envelope
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self.message_callbacks[generation_index](payload)

    def disconnect(self, generation_index: int) -> None:
        """
        함수 이름: disconnect()
        기능: 선택한 generation의 비정상 종료 callback을 호출한다.
        인자: generation_index -> 0부터 시작하는 구독 세대 순번
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self.disconnect_callbacks[generation_index]()


def _execution_report(
    *,
    transaction_time: int,
    execution_id: int,
    execution_type: str,
    status: str,
    cumulative_quantity: str,
    last_quantity: str = "0",
    last_price: str = "0",
    trade_id: int = -1,
    commission: str = "0",
    commission_asset: str | None = None,
    client_order_id: str = "bat-order-1",
    original_client_order_id: str = "",
    exchange_order_id: int = 7001,
) -> dict[str, object]:
    """
    함수 이름: _execution_report()
    기능: order stream mapper가 사용하는 공식 executionReport 필드를 고정 fixture로 만든다.
    인자: transaction_time -> event.T source millisecond
        execution_id -> event.I 실행 식별자
        execution_type -> event.x 실행 유형
        status -> event.X 주문 상태
        cumulative_quantity -> event.z 누적 체결 수량
        last_quantity -> event.l 마지막 체결 수량
        last_price -> event.L 마지막 체결 가격
        trade_id -> event.t 거래 ID 또는 비체결이면 -1
        commission -> event.n 수수료 수량
        commission_asset -> event.N 수수료 자산 또는 비체결이면 None
        client_order_id -> event.c 현재 client ID
        original_client_order_id -> event.C 취소 대상 원 client ID
        exchange_order_id -> event.i 거래소 주문 ID
    반환값: subscriptionId와 executionReport event envelope
    작성 날짜: 2026/08/22
    """
    return {
        "subscriptionId": 7,
        "event": {
            "e": "executionReport",
            "E": transaction_time + 1,
            "s": "ETHUSDT",
            "c": client_order_id,
            "C": original_client_order_id,
            "x": execution_type,
            "X": status,
            "r": "NONE",
            "i": exchange_order_id,
            "l": last_quantity,
            "z": cumulative_quantity,
            "L": last_price,
            "n": commission,
            "N": commission_asset,
            "T": transaction_time,
            "t": trade_id,
            "I": execution_id,
        },
    }


def _account_position_event(update_time: int) -> dict[str, object]:
    """
    함수 이름: _account_position_event()
    기능: authenticated dispatcher와 Gateway가 함께 처리할 빈 잔고 patch fixture를 만든다.
    인자: update_time -> event와 account update에 사용할 source millisecond
    반환값: subscriptionId를 포함한 outboundAccountPosition envelope
    작성 날짜: 2026/08/23
    """
    return {
        "subscriptionId": 7,
        "event": {
            "e": "outboundAccountPosition",
            "E": update_time,
            "u": update_time,
            "B": [],
        },
    }


class SpotWebSocketClientTests(unittest.TestCase):
    """
    클래스 이름: SpotWebSocketClientTests
    기능: testnet URL, HMAC subscription ACK와 transport 종료 단일 알림을 검증한다.
    작성 날짜: 2026/08/22
    """

    def _create_client(
        self,
        socket_factory: _ScriptedSocketFactory,
    ) -> BinanceSpotWebSocketClient:
        """
        함수 이름: _create_client()
        기능: 고정 timestamp·request ID를 쓰는 결정적 WebSocket client를 만든다.
        인자: socket_factory -> URL과 frame을 관찰할 fake socket factory
        반환값: testnet BinanceSpotWebSocketClient
        작성 날짜: 2026/08/22
        """
        return BinanceSpotWebSocketClient(
            API_KEY,
            API_SECRET,
            socket_factory=socket_factory,
            timestamp_provider=lambda: FIXED_TIMESTAMP_MILLISECONDS,
            request_id_factory=lambda: REQUEST_ID,
            startup_timeout_seconds=1,
        )

    def test_public_kline_uses_testnet_combined_url_and_forwards_frame(
        self,
    ) -> None:
        """
        함수 이름: test_public_kline_uses_testnet_combined_url_and_forwards_frame()
        기능: public Kline URL이 lowercase combined stream이며 credential을 포함하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        factory = _ScriptedSocketFactory()
        received_payloads: list[object] = []
        disconnects: list[str] = []
        client = self._create_client(factory)

        subscription = client.subscribe_all_kline_streams(
            symbol="ETHUSDT",
            intervals=("1m", "4h"),
            on_message=received_payloads.append,
            on_disconnect=lambda: disconnects.append("disconnected"),
        )
        socket = factory.sockets[0]
        socket.emit({"stream": "ethusdt@kline_1m", "data": {"e": "kline"}})

        self.assertEqual(
            socket.url,
            "wss://stream.testnet.binance.vision/stream?streams="
            "ethusdt@kline_1m/ethusdt@kline_4h",
        )
        self.assertNotIn(API_KEY, socket.url)
        self.assertNotIn(API_SECRET, socket.url)
        self.assertEqual(len(received_payloads), 1)
        self.assertEqual(disconnects, [])
        subscription.close()
        self.assertEqual(disconnects, [])

    def test_mainnet_market_stream_preserves_testnet_account_stream(self) -> None:
        """
        함수 이름: test_mainnet_market_stream_preserves_testnet_account_stream()
        기능: 실제 시세 구독·재구독과 서명 계좌 구독이 서로 다른 공식 host를 유지하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/05
        """
        # 같은 client에서 시장 구독을 다시 열어도 계좌 구독 endpoint에는 영향을 주지 않는다.
        factory = _ScriptedSocketFactory()
        client = BinanceSpotWebSocketClient(
            API_KEY, API_SECRET, socket_factory=factory,
            timestamp_provider=lambda: FIXED_TIMESTAMP_MILLISECONDS,
            use_mainnet_market_data=True,
        )
        for _attempt in range(2):
            subscription = client.subscribe_all_kline_streams(
                symbol="ETHUSDT", intervals=("4h",),
                on_message=lambda _message: None, on_disconnect=lambda: None,
            )
            self.assertEqual(factory.sockets[-1].url,
                "wss://data-stream.binance.vision/stream?streams=ethusdt@kline_4h")
            self.assertEqual(factory.sockets[-1].sent_payloads, [])  # 공개 구독은 서명 frame을 보내지 않는다.
            subscription.close()
        account_subscription = client.subscribe_account_info(
            on_message=lambda _message: None, on_disconnect=lambda: None,
        )
        self.addCleanup(account_subscription.close)
        self.assertEqual(factory.sockets[-1].url,
            "wss://ws-api.testnet.binance.vision/ws-api/v3")
        self.assertEqual(len(factory.sockets[-1].sent_payloads), 1)

    def test_public_kline_disconnect_notifies_once_after_start(self) -> None:
        """
        함수 이름: test_public_kline_disconnect_notifies_once_after_start()
        기능: Communication 1.1.1의 production client가 공개 Kline disconnect를 한 번만 알리는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        factory = _ScriptedSocketFactory()
        disconnects: list[str] = []
        client = self._create_client(factory)

        # 정상 startup 뒤 on_error와 on_close가 연속 호출되는 실제 transport 종료 순서를 재현한다.
        subscription = client.subscribe_all_kline_streams(
            symbol="ETHUSDT",
            intervals=("1m", "30m", "4h", "1d"),
            on_message=lambda _payload: None,
            on_disconnect=lambda: disconnects.append("disconnected"),
        )
        factory.sockets[0].fail_and_close()

        self.assertEqual(disconnects, ["disconnected"])
        subscription.close()
        self.assertEqual(disconnects, ["disconnected"])  # owner close도 알림을 중복하지 않는다.

    def test_account_stream_sends_current_hmac_signature_subscription(
        self,
    ) -> None:
        """
        함수 이름: test_account_stream_sends_current_hmac_signature_subscription()
        기능: legacy listenKey 없이 정렬 HMAC 요청을 보내고 ACK 뒤 해당 event만 전달하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        factory = _ScriptedSocketFactory()
        received_payloads: list[object] = []
        payload_delivered = Event()
        client = self._create_client(factory)

        def record_payload(payload: object) -> None:
            """
            함수 이름: record_payload()
            기능: dispatcher가 전달한 payload를 기록하고 비동기 완료를 알린다.
            인자: payload -> 검증된 account event envelope
            반환값: 없음
            작성 날짜: 2026/08/23
            """
            received_payloads.append(payload)
            payload_delivered.set()

        subscription = client.subscribe_account_info(
            on_message=record_payload,
            on_disconnect=lambda: None,
        )
        socket = factory.sockets[0]
        request = json.loads(socket.sent_payloads[0])
        signature_payload = (
            f"apiKey={API_KEY}&recvWindow=5000&"
            f"timestamp={FIXED_TIMESTAMP_MILLISECONDS}"
        )
        expected_signature = hmac.new(
            API_SECRET.encode("utf-8"),
            signature_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        self.assertEqual(
            socket.url,
            "wss://ws-api.testnet.binance.vision/ws-api/v3",
        )
        self.assertNotIn(API_KEY, socket.url)
        self.assertNotIn(API_SECRET, socket.url)
        self.assertEqual(request["id"], REQUEST_ID)
        self.assertEqual(
            request["method"],
            "userDataStream.subscribe.signature",
        )
        self.assertNotIn("listenKey", json.dumps(request))
        self.assertEqual(request["params"]["signature"], expected_signature)
        self.assertNotIn(API_SECRET, json.dumps(request))

        socket.emit(_account_position_event(FIXED_TIMESTAMP_MILLISECONDS))
        self.assertTrue(payload_delivered.wait(timeout=1))
        self.assertTrue(subscription.wait_until_caught_up(1))
        self.assertEqual(len(received_payloads), 1)
        subscription.close()

    def test_account_dispatcher_preserves_fifo_order(self) -> None:
        """
        함수 이름: test_account_dispatcher_preserves_fifo_order()
        기능: 수신 loop와 분리된 단일 worker가 account event source 순서를 보존하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        factory = _ScriptedSocketFactory()
        received_update_times: list[int] = []
        all_payloads_delivered = Event()
        client = self._create_client(factory)

        def record_payload(payload: object) -> None:
            """
            함수 이름: record_payload()
            기능: event update time을 기록하고 세 번째 callback 완료를 알린다.
            인자: payload -> 검증된 account event envelope
            반환값: 없음
            작성 날짜: 2026/08/23
            """
            event = payload["event"]
            received_update_times.append(event["u"])
            if len(received_update_times) == 3:
                all_payloads_delivered.set()

        subscription = client.subscribe_account_info(
            on_message=record_payload,
            on_disconnect=lambda: None,
        )
        socket = factory.sockets[0]

        # 한 receive burst의 세 event를 넣어도 consumer는 source 순서를 바꾸지 않는다.
        for update_time in (1, 2, 3):
            socket.emit(_account_position_event(update_time))

        self.assertTrue(all_payloads_delivered.wait(timeout=1))
        self.assertTrue(subscription.wait_until_caught_up(1))
        self.assertEqual(received_update_times, [1, 2, 3])
        subscription.close()

    def test_account_callback_backlog_does_not_block_disconnect_and_gates_ready(
        self,
    ) -> None:
        """
        함수 이름: test_account_callback_backlog_does_not_block_disconnect_and_gates_ready()
        기능: 막힌 application callback 중 receive 반환·disconnect와 readiness 잠금이 유지되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        factory = _ScriptedSocketFactory()
        callback_started = Event()
        release_callback = Event()
        receive_returned = Event()
        reconciliations: list[str] = []
        client = self._create_client(factory)

        def apply_snapshot(_snapshot: object) -> None:
            """
            함수 이름: apply_snapshot()
            기능: application 처리 지연을 barrier로 재현한다.
            인자: _snapshot -> Gateway가 정규화한 빈 account patch
            반환값: 없음
            작성 날짜: 2026/08/23
            """
            callback_started.set()
            release_callback.wait(timeout=2)

        gateway = WebSocketGateway(
            client,
            account_snapshot_callback=apply_snapshot,
            reconciliation_required_callback=reconciliations.append,
        )
        subscription = gateway.start_account_info_stream()
        socket = factory.sockets[0]

        def emit_account_event() -> None:
            """
            함수 이름: emit_account_event()
            기능: 실제 receive callback이 downstream barrier와 분리되어 반환하는지 기록한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/08/23
            """
            socket.emit(_account_position_event(FIXED_TIMESTAMP_MILLISECONDS))
            receive_returned.set()

        try:
            receiver = Thread(target=emit_account_event, daemon=True)
            receiver.start()
            self.assertTrue(callback_started.wait(timeout=1))
            self.assertTrue(receive_returned.wait(timeout=1))
            self.assertTrue(gateway.account_connected)
            self.assertFalse(gateway.account_caught_up)
            self.assertFalse(gateway.account_ready)

            # 같은 receive 경로가 close를 관찰해 callback barrier보다 먼저 fail closed한다.
            socket.fail_and_close()
            self.assertFalse(gateway.account_connected)
            self.assertEqual(reconciliations, ["account_stream_disconnected"])
        finally:
            release_callback.set()
            receiver.join(timeout=1)
            subscription.close()

    def test_account_dispatcher_overflow_disconnects_instead_of_dropping(
        self,
    ) -> None:
        """
        함수 이름: test_account_dispatcher_overflow_disconnects_instead_of_dropping()
        기능: bounded FIFO overflow가 event 손실 대신 단일 disconnect와 재조정을 요구하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        factory = _ScriptedSocketFactory()
        callback_started = Event()
        release_callback = Event()
        disconnected = Event()
        received_payloads: list[object] = []
        client = BinanceSpotWebSocketClient(
            API_KEY,
            API_SECRET,
            socket_factory=factory,
            timestamp_provider=lambda: FIXED_TIMESTAMP_MILLISECONDS,
            request_id_factory=lambda: REQUEST_ID,
            startup_timeout_seconds=1,
            account_event_queue_capacity=1,
        )

        def block_first_payload(payload: object) -> None:
            """
            함수 이름: block_first_payload()
            기능: 첫 event를 처리 중으로 유지해 capacity 1 queue를 결정적으로 채운다.
            인자: payload -> 검증된 account event envelope
            반환값: 없음
            작성 날짜: 2026/08/23
            """
            received_payloads.append(payload)
            callback_started.set()
            release_callback.wait(timeout=2)

        subscription = client.subscribe_account_info(
            on_message=block_first_payload,
            on_disconnect=disconnected.set,
        )
        socket = factory.sockets[0]

        try:
            socket.emit(_account_position_event(1))
            self.assertTrue(callback_started.wait(timeout=1))
            socket.emit(_account_position_event(2))  # capacity 1 queue를 채운다.
            socket.emit(_account_position_event(3))  # 세 번째 event는 fail closed를 일으킨다.

            self.assertTrue(disconnected.wait(timeout=1))
            self.assertTrue(socket._closed)
            self.assertFalse(subscription.caught_up)
            self.assertEqual(len(received_payloads), 1)
        finally:
            release_callback.set()
            subscription.close()

    def test_account_consumer_failure_disconnects_and_stays_not_ready(
        self,
    ) -> None:
        """
        함수 이름: test_account_consumer_failure_disconnects_and_stays_not_ready()
        기능: downstream callback 예외가 caught-up 재개 없이 transport를 닫는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        factory = _ScriptedSocketFactory()
        disconnected = Event()
        client = self._create_client(factory)

        def reject_payload(_payload: object) -> None:
            """
            함수 이름: reject_payload()
            기능: application account 처리 실패를 결정적으로 발생시킨다.
            인자: _payload -> 사용하지 않는 검증된 account event
            반환값: 정상 반환하지 않음
            작성 날짜: 2026/08/23
            """
            raise RuntimeError("injected consumer failure")

        subscription = client.subscribe_account_info(
            on_message=reject_payload,
            on_disconnect=disconnected.set,
        )
        socket = factory.sockets[0]
        socket.emit(_account_position_event(FIXED_TIMESTAMP_MILLISECONDS))

        self.assertTrue(disconnected.wait(timeout=1))
        self.assertTrue(socket._closed)
        self.assertFalse(subscription.caught_up)
        subscription.close()

    def test_account_subscription_rejection_fails_startup_without_secret(
        self,
    ) -> None:
        """
        함수 이름: test_account_subscription_rejection_fails_startup_without_secret()
        기능: non-200 signature ACK가 handle을 반환하지 않고 예외에 credential을 노출하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        factory = _ScriptedSocketFactory(subscription_status=401)
        client = self._create_client(factory)

        with self.assertRaises(WebSocketSubscriptionError) as error_context:
            client.subscribe_account_info(
                on_message=lambda _payload: None,
                on_disconnect=lambda: None,
            )

        error_text = str(error_context.exception)
        self.assertNotIn(API_KEY, error_text)
        self.assertNotIn(API_SECRET, error_text)
        self.assertTrue(factory.sockets[0]._closed)

    def test_account_subscription_rejection_redacts_untrusted_error_code(
        self,
    ) -> None:
        """
        함수 이름: test_account_subscription_rejection_redacts_untrusted_error_code()
        기능: 비정상 문자열 error.code가 credential canary를 예외에 반사하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        reflected_secret_canary = "testnet-api-secret-canary"
        with self.assertRaises(WebSocketSubscriptionError) as direct_error:
            BinanceSpotWebSocketClient._accept_account_subscription_response(
                {
                    "status": 400,
                    "error": {"code": reflected_secret_canary},
                },
                subscription_id=[None],
                subscription_lock=RLock(),
                lifecycle=_ConnectionLifecycle(lambda: None),
            )

        # 가장 안쪽 ACK validator 자체도 untrusted code 원문 대신 고정 분류만 공개한다.
        direct_error_text = str(direct_error.exception)
        self.assertNotIn(reflected_secret_canary, direct_error_text)
        self.assertIn("code invalid", direct_error_text)

        factory = _ScriptedSocketFactory(
            subscription_status=400,
            subscription_error_code=reflected_secret_canary,
        )
        client = self._create_client(factory)

        # Public startup 경계도 sanitized 원인을 credential 없는 transport 오류로 감싼다.
        with self.assertRaises(WebSocketSubscriptionError) as error_context:
            client.subscribe_account_info(
                on_message=lambda _payload: None,
                on_disconnect=lambda: None,
            )

        error_text = str(error_context.exception)
        self.assertNotIn(reflected_secret_canary, error_text)
        self.assertIn("frame validation failed", error_text)
        self.assertTrue(factory.sockets[0]._closed)

    def test_account_stream_rejects_other_subscription_and_disconnects_once(
        self,
    ) -> None:
        """
        함수 이름: test_account_stream_rejects_other_subscription_and_disconnects_once()
        기능: ACK와 다른 subscriptionId frame이 consumer에 도달하지 않고 단일 disconnect를 내는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        factory = _ScriptedSocketFactory()
        received_payloads: list[object] = []
        disconnects: list[str] = []
        client = self._create_client(factory)
        client.subscribe_account_info(
            on_message=received_payloads.append,
            on_disconnect=lambda: disconnects.append("disconnected"),
        )
        socket = factory.sockets[0]

        socket.emit(
            {
                "subscriptionId": 8,
                "event": {
                    "e": "outboundAccountPosition",
                    "E": FIXED_TIMESTAMP_MILLISECONDS,
                    "u": FIXED_TIMESTAMP_MILLISECONDS,
                    "B": [],
                },
            }
        )
        socket.fail_and_close()

        self.assertEqual(received_payloads, [])
        self.assertEqual(disconnects, ["disconnected"])


class WebSocketGatewayOrderTests(unittest.TestCase):
    """
    클래스 이름: WebSocketGatewayOrderTests
    기능: executionReport 누적 fill, source 순서와 재조정 fail-closed 경계를 검증한다.
    작성 날짜: 2026/08/22
    """

    def test_gateway_lock_is_released_before_application_callback(self) -> None:
        """
        함수 이름: test_gateway_lock_is_released_before_application_callback()
        기능: application lock 대기 callback이 Gateway 상태 조회와 잠금 역전을 만들지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        client = _FakeGatewayClient()
        application_lock = RLock()
        callback_waiting = Event()
        gateway_read_finished = Event()
        emitter_errors: list[BaseException] = []

        def apply_snapshot(_snapshot: object) -> None:
            """
            함수 이름: apply_snapshot()
            기능: callback 진입을 알린 뒤 테스트 application lock으로 publication을 직렬화한다.
            인자: _snapshot -> 이 교착 회귀에서는 내용이 필요 없는 account patch
            반환값: 없음
            작성 날짜: 2026/08/22
            """
            callback_waiting.set()
            with application_lock:
                return  # Gateway lock이 이미 풀린 상태에서만 application lock을 기다린다.

        gateway = WebSocketGateway(
            client,
            account_snapshot_callback=apply_snapshot,
        )
        gateway.start_account_info_stream()
        payload = {
            "subscriptionId": 7,
            "event": {
                "e": "outboundAccountPosition",
                "E": FIXED_TIMESTAMP_MILLISECONDS,
                "u": FIXED_TIMESTAMP_MILLISECONDS,
                "B": [],
            },
        }

        def emit_account_patch() -> None:
            """
            함수 이름: emit_account_patch()
            기능: 실제 Gateway message callback을 별도 transport thread에서 실행한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/08/22
            """
            try:
                client.emit(0, payload)
            except BaseException as error:
                emitter_errors.append(error)

        def read_gateway_state() -> None:
            """
            함수 이름: read_gateway_state()
            기능: application lock owner가 호출할 Gateway readiness 조회를 재현한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/08/22
            """
            gateway.account_connected
            gateway_read_finished.set()

        # Application lock을 보유한 동안 callback이 대기해도 Gateway lock 조회는 즉시 끝나야 한다.
        with application_lock:
            emitter = Thread(target=emit_account_patch, daemon=True)
            emitter.start()
            self.assertTrue(callback_waiting.wait(timeout=1))
            reader = Thread(target=read_gateway_state, daemon=True)
            reader.start()
            gateway_lock_was_available = gateway_read_finished.wait(timeout=1)

        emitter.join(timeout=1)
        reader.join(timeout=1)
        self.assertTrue(gateway_lock_was_available)
        self.assertFalse(emitter.is_alive())
        self.assertFalse(reader.is_alive())
        self.assertEqual(emitter_errors, [])

    def test_rest_rebase_seeds_missed_fill_before_new_stream_generation(
        self,
    ) -> None:
        """
        함수 이름: test_rest_rebase_seeds_missed_fill_before_new_stream_generation()
        기능: disconnect 중 REST로 복구한 fill이 다음 누적 z 검증의 기준이 되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        client = _FakeGatewayClient()
        results: list[OrderResult] = []
        gateway = WebSocketGateway(
            client,
            order_result_callback=results.append,
        )
        gateway.start_account_info_stream()
        client.emit(
            0,
            _execution_report(
                transaction_time=1_000,
                execution_id=1,
                execution_type="TRADE",
                status="PARTIALLY_FILLED",
                cumulative_quantity="0.4",
                last_quantity="0.4",
                last_price="2000",
                trade_id=81,
                commission="0",
                commission_asset="USDT",
            ),
        )
        client.disconnect(0)

        # REST same-ID query가 stream 단절 중 놓친 두 번째 0.2 fill까지 누적으로 반환한다.
        first_fill = results[-1].fills[0]
        missed_fill = Fill(
            exchange_order_id="7001",
            trade_id="82",
            quantity=Decimal("0.2"),
            price=Decimal("2050"),
            fee_amount=Decimal("0"),
            fee_asset="USDT",
            fee_quote_amount=Decimal("0"),
            executed_at=datetime.fromtimestamp(2, tz=timezone.utc),
        )
        gateway.rebase_order_results(
            (
                OrderResult(
                    symbol="ETHUSDT",
                    client_order_id="bat-order-1",
                    status=OrderStatus.PARTIALLY_FILLED,
                    processed_at=datetime.fromtimestamp(2, tz=timezone.utc),
                    exchange_order_id="7001",
                    fills=(first_fill, missed_fill),
                ),
            )
        )
        gateway.start_account_info_stream()

        # 새 stream의 마지막 0.4 fill은 REST 기준 0.6과 합쳐져 누적 1.0으로 정상 발행된다.
        client.emit(
            1,
            _execution_report(
                transaction_time=3_000,
                execution_id=3,
                execution_type="TRADE",
                status="FILLED",
                cumulative_quantity="1.0",
                last_quantity="0.4",
                last_price="2100",
                trade_id=83,
                commission="0",
                commission_asset="USDT",
            ),
        )

        self.assertIs(results[-1].status, OrderStatus.FILLED)
        self.assertEqual(
            tuple(fill.trade_id for fill in results[-1].fills),
            ("81", "82", "83"),
        )

    def test_execution_reports_publish_monotonic_cumulative_order_results(
        self,
    ) -> None:
        """
        함수 이름: test_execution_reports_publish_monotonic_cumulative_order_results()
        기능: NEW·partial·duplicate·stale·FILLED가 새 fill만 한 번 누적해 발행되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        client = _FakeGatewayClient()
        results: list[OrderResult] = []
        reconciliations: list[str] = []
        gateway = WebSocketGateway(
            client,
            order_result_callback=results.append,
            reconciliation_required_callback=reconciliations.append,
        )
        gateway.start_account_info_stream()
        new_event = _execution_report(
            transaction_time=1_000,
            execution_id=1,
            execution_type="NEW",
            status="NEW",
            cumulative_quantity="0",
        )
        first_fill = _execution_report(
            transaction_time=2_000,
            execution_id=2,
            execution_type="TRADE",
            status="PARTIALLY_FILLED",
            cumulative_quantity="0.4",
            last_quantity="0.4",
            last_price="2000",
            trade_id=81,
            commission="0.0004",
            commission_asset="ETH",
        )
        stale_new = _execution_report(
            transaction_time=1_500,
            execution_id=3,
            execution_type="NEW",
            status="NEW",
            cumulative_quantity="0",
        )
        final_fill = _execution_report(
            transaction_time=3_000,
            execution_id=4,
            execution_type="TRADE",
            status="FILLED",
            cumulative_quantity="1.0",
            last_quantity="0.6",
            last_price="2100",
            trade_id=82,
            commission="0.0006",
            commission_asset="ETH",
        )

        # 동일 partial event와 더 오래된 NEW는 결과를 추가하지 않고 두 실제 fill만 누적한다.
        client.emit(0, new_event)
        client.emit(0, first_fill)
        client.emit(0, first_fill)
        client.emit(0, stale_new)
        client.emit(0, final_fill)
        client.emit(0, final_fill)

        self.assertEqual(
            [result.status for result in results],
            [OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED],
        )
        self.assertEqual(len(results[1].fills), 1)
        self.assertEqual(len(results[2].fills), 2)
        self.assertEqual(
            tuple(fill.trade_id for fill in results[2].fills),
            ("81", "82"),
        )
        self.assertEqual(
            sum(
                (fill.quantity for fill in results[2].fills),
                start=Decimal("0"),
            ),
            Decimal("1.0"),
        )
        self.assertEqual(reconciliations, [])
        self.assertTrue(gateway.account_connected)

    def test_cancel_execution_uses_original_client_order_id(self) -> None:
        """
        함수 이름: test_cancel_execution_uses_original_client_order_id()
        기능: 취소 요청 ID인 c 대신 취소 대상 원 주문 ID C가 OrderResult에 유지되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        client = _FakeGatewayClient()
        results: list[OrderResult] = []
        gateway = WebSocketGateway(
            client,
            order_result_callback=results.append,
        )
        gateway.start_account_info_stream()

        client.emit(
            0,
            _execution_report(
                transaction_time=1_000,
                execution_id=1,
                execution_type="CANCELED",
                status="CANCELED",
                cumulative_quantity="0",
                client_order_id="cancel-request-1",
                original_client_order_id="bat-order-1",
            ),
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].client_order_id, "bat-order-1")
        self.assertIs(results[0].status, OrderStatus.CANCELED)

    def test_missing_cumulative_fill_closes_stream_and_requests_reconciliation(
        self,
    ) -> None:
        """
        함수 이름: test_missing_cumulative_fill_closes_stream_and_requests_reconciliation()
        기능: 첫 관찰 누적 수량이 마지막 fill보다 크면 불완전 결과 대신 full reconciliation을 요구한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        client = _FakeGatewayClient()
        results: list[OrderResult] = []
        reconciliations: list[str] = []
        gateway = WebSocketGateway(
            client,
            order_result_callback=results.append,
            reconciliation_required_callback=reconciliations.append,
        )
        gateway.start_account_info_stream()

        with self.assertRaises(AccountStreamStateError):
            client.emit(
                0,
                _execution_report(
                    transaction_time=2_000,
                    execution_id=2,
                    execution_type="TRADE",
                    status="PARTIALLY_FILLED",
                    cumulative_quantity="0.8",
                    last_quantity="0.4",
                    last_price="2000",
                    trade_id=81,
                    commission="0.0004",
                    commission_asset="ETH",
                ),
            )

        self.assertEqual(results, [])
        self.assertFalse(gateway.account_connected)
        self.assertTrue(client.subscriptions[0].closed)
        self.assertEqual(
            reconciliations,
            ["account_stream_processing_failed"],
        )

    def test_out_of_order_unseen_fill_requires_reconciliation(self) -> None:
        """
        함수 이름: test_out_of_order_unseen_fill_requires_reconciliation()
        기능: 최신 cursor 뒤 도착한 미관찰 trade를 조용히 버리지 않고 재조정 lock으로 닫는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        client = _FakeGatewayClient()
        results: list[OrderResult] = []
        reconciliations: list[str] = []
        gateway = WebSocketGateway(
            client,
            order_result_callback=results.append,
            reconciliation_required_callback=reconciliations.append,
        )
        gateway.start_account_info_stream()
        client.emit(
            0,
            _execution_report(
                transaction_time=2_000,
                execution_id=2,
                execution_type="TRADE",
                status="PARTIALLY_FILLED",
                cumulative_quantity="0.6",
                last_quantity="0.6",
                last_price="2100",
                trade_id=82,
                commission="1.26",
                commission_asset="USDT",
            ),
        )

        with self.assertRaises(AccountStreamStateError):
            client.emit(
                0,
                _execution_report(
                    transaction_time=1_000,
                    execution_id=1,
                    execution_type="TRADE",
                    status="PARTIALLY_FILLED",
                    cumulative_quantity="0.4",
                    last_quantity="0.4",
                    last_price="2000",
                    trade_id=81,
                    commission="0.8",
                    commission_asset="USDT",
                ),
            )

        self.assertEqual(len(results), 1)
        self.assertFalse(gateway.account_connected)
        self.assertEqual(len(reconciliations), 1)

    def test_termination_shutdown_and_disconnect_trigger_one_reconciliation(
        self,
    ) -> None:
        """
        함수 이름: test_termination_shutdown_and_disconnect_trigger_one_reconciliation()
        기능: 세 종료 경로가 각각 fail-closed하고 세대별 재조정 callback을 한 번만 호출하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        terminal_events = (
            "eventStreamTerminated",
            "serverShutdown",
        )
        for event_type in terminal_events:
            with self.subTest(event_type=event_type):
                client = _FakeGatewayClient()
                reconciliations: list[str] = []
                gateway = WebSocketGateway(
                    client,
                    order_result_callback=lambda _result: None,
                    reconciliation_required_callback=(
                        reconciliations.append
                    ),
                )
                gateway.start_account_info_stream()

                with self.assertRaises(AccountStreamStateError):
                    client.emit(
                        0,
                        {
                            "subscriptionId": 7,
                            "event": {
                                "e": event_type,
                                "E": FIXED_TIMESTAMP_MILLISECONDS,
                            },
                        },
                    )
                client.disconnect(0)

                self.assertFalse(gateway.account_connected)
                self.assertEqual(len(reconciliations), 1)

        # 순수 transport disconnect도 예외 frame 없이 즉시 fail-closed해야 한다.
        client = _FakeGatewayClient()
        reconciliations = []
        gateway = WebSocketGateway(
            client,
            order_result_callback=lambda _result: None,
            reconciliation_required_callback=reconciliations.append,
        )
        gateway.start_account_info_stream()
        client.disconnect(0)
        client.disconnect(0)

        self.assertFalse(gateway.account_connected)
        self.assertEqual(reconciliations, ["account_stream_disconnected"])

    def test_stale_generation_cannot_publish_or_request_reconciliation(
        self,
    ) -> None:
        """
        함수 이름: test_stale_generation_cannot_publish_or_request_reconciliation()
        기능: 재구독 뒤 이전 generation의 message·disconnect가 현재 stream 상태를 바꾸지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        client = _FakeGatewayClient()
        results: list[OrderResult] = []
        reconciliations: list[str] = []
        gateway = WebSocketGateway(
            client,
            order_result_callback=results.append,
            reconciliation_required_callback=reconciliations.append,
        )
        gateway.start_account_info_stream()
        gateway.start_account_info_stream()

        client.emit(
            0,
            _execution_report(
                transaction_time=1_000,
                execution_id=1,
                execution_type="NEW",
                status="NEW",
                cumulative_quantity="0",
            ),
        )
        client.disconnect(0)
        client.emit(
            1,
            _execution_report(
                transaction_time=2_000,
                execution_id=2,
                execution_type="NEW",
                status="NEW",
                cumulative_quantity="0",
            ),
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(reconciliations, [])
        self.assertTrue(gateway.account_connected)


if __name__ == "__main__":
    unittest.main()
