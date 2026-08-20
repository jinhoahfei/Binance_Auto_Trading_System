"""Binance account REST/WS payload의 AccountSnapshot 정규화를 검증한다."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
import unittest

from binance_auto_trader.adapters.binance.api_gateway import APIGateway
from binance_auto_trader.adapters.binance.websocket_gateway import (
    AccountStreamStateError,
    WebSocketGateway,
)
from binance_auto_trader.domain.trading.account import AccountSnapshot


REST_UPDATED_AT_MILLISECONDS = 1_787_270_400_000
FIRST_STREAM_UPDATE_MILLISECONDS = 1_787_270_460_000
FIRST_STREAM_EVENT_MILLISECONDS = 1_787_270_460_100


def _official_account_payload() -> dict[str, object]:
    """
    함수 이름: _official_account_payload()
    기능: Binance Spot GET /api/v3/account의 계좌 응답 fixture를 만든다.
    인자: 없음
    반환값: balances와 updateTime을 포함한 공식 형식 payload
    작성 날짜: 2026/08/21
    """
    return {
        "makerCommission": 15,
        "takerCommission": 15,
        "buyerCommission": 0,
        "sellerCommission": 0,
        "canTrade": True,
        "canWithdraw": True,
        "canDeposit": True,
        "updateTime": REST_UPDATED_AT_MILLISECONDS,
        "accountType": "SPOT",
        "balances": [
            {"asset": "BTC", "free": "0.01000000", "locked": "0.00100000"},
            {"asset": "ETH", "free": "1.25000000", "locked": "0.25000000"},
            {"asset": "USDT", "free": "125.50", "locked": "4.50"},
        ],
        "permissions": ["SPOT"],
        "uid": 123456789,
    }


def _official_account_event(
    *,
    update_time_milliseconds: int = FIRST_STREAM_UPDATE_MILLISECONDS,
    event_time_milliseconds: int = FIRST_STREAM_EVENT_MILLISECONDS,
    balances: list[dict[str, object]] | None = None,
    include_subscription_id: bool = True,
) -> dict[str, object]:
    """
    함수 이름: _official_account_event()
    기능: 현재 Binance WebSocket API account position event envelope를 만든다.
    인자: update_time_milliseconds -> event.u 계좌 갱신 millisecond
        event_time_milliseconds -> event.E 발생 millisecond
        balances -> event.B에 넣을 partial absolute balance row
        include_subscription_id -> 선택 subscriptionId 포함 여부
    반환값: subscriptionId와 event object를 갖는 공식 형식 payload
    작성 날짜: 2026/08/21
    """
    selected_balances = balances
    if selected_balances is None:
        selected_balances = [
            {"a": "ETH", "f": "0.80000000", "l": "0.10000000"},
        ]

    payload: dict[str, object] = {
        "event": {
            "e": "outboundAccountPosition",
            "E": event_time_milliseconds,
            "u": update_time_milliseconds,
            "B": selected_balances,
        }
    }
    if include_subscription_id:
        payload["subscriptionId"] = 7

    return payload


class FakeSubscription:
    """
    클래스 이름: FakeSubscription
    기능: account stream 세대 교체에서 종료 상태를 보존한다.
    작성 날짜: 2026/08/21
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 종료되지 않은 fake 구독을 초기화한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.closed = False

    def close(self) -> None:
        """
        함수 이름: close()
        기능: fake account stream 구독을 멱등적으로 종료한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.closed = True


class FakeBinanceClient:
    """
    클래스 이름: FakeBinanceClient
    기능: account REST 응답과 WebSocket callback을 결정론적으로 제공한다.
    작성 날짜: 2026/08/21
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 정상 REST fixture와 빈 account stream 호출 기록을 초기화한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.account_payload: object = _official_account_payload()
        self.get_account_call_count = 0
        self.account_message_callbacks: list[Callable[[object], None]] = []
        self.account_disconnect_callbacks: list[Callable[[], None]] = []
        self.account_subscriptions: list[FakeSubscription] = []
        self.account_stream_error: Exception | None = None

    def get_account(self) -> object:
        """
        함수 이름: get_account()
        기능: 호출 횟수를 기록하고 설정된 계좌 응답 또는 예외를 반환한다.
        인자: 없음
        반환값: JSON으로 해석된 Binance account payload
        작성 날짜: 2026/08/21
        """
        self.get_account_call_count += 1
        if isinstance(self.account_payload, BaseException):
            raise self.account_payload

        return self.account_payload

    def get_klines(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int,
    ) -> object:
        """
        함수 이름: get_klines()
        기능: account 테스트에서 market REST 호출을 금지한다.
        인자: symbol -> 예상하지 않은 market symbol
            interval -> 예상하지 않은 Kline interval
            limit -> 예상하지 않은 조회 개수
        반환값: 정상 경로에서 반환하지 않음
        작성 날짜: 2026/08/21
        """
        raise AssertionError(
            f"unexpected Kline request: {symbol} {interval} {limit}"
        )

    def subscribe_all_kline_streams(
        self,
        *,
        symbol: str,
        intervals: tuple[str, ...],
        on_message: Callable[[object], None],
        on_disconnect: Callable[[], None],
    ) -> FakeSubscription:
        """
        함수 이름: subscribe_all_kline_streams()
        기능: account 테스트에서 Kline WebSocket 구독을 금지한다.
        인자: symbol -> 예상하지 않은 market symbol
            intervals -> 예상하지 않은 Kline interval들
            on_message -> 예상하지 않은 message callback
            on_disconnect -> 예상하지 않은 disconnect callback
        반환값: 정상 경로에서 반환하지 않음
        작성 날짜: 2026/08/21
        """
        raise AssertionError(
            f"unexpected Kline subscription: {symbol} {intervals}"
        )

    def subscribe_account_info(
        self,
        on_message: Callable[[object], None],
        on_disconnect: Callable[[], None],
    ) -> FakeSubscription:
        """
        함수 이름: subscribe_account_info()
        기능: account stream callback을 기록하고 opaque 구독 handle을 반환한다.
        인자: on_message -> 계좌 event 수신 callback
            on_disconnect -> account stream 종료 callback
        반환값: 새 fake account 구독 handle
        작성 날짜: 2026/08/21
        """
        if self.account_stream_error is not None:
            raise self.account_stream_error

        subscription = FakeSubscription()
        self.account_message_callbacks.append(on_message)
        self.account_disconnect_callbacks.append(on_disconnect)
        self.account_subscriptions.append(subscription)

        return subscription

    def emit_account_event(
        self,
        subscription_index: int,
        payload: object,
    ) -> None:
        """
        함수 이름: emit_account_event()
        기능: 선택한 account stream 세대의 message callback을 동기 호출한다.
        인자: subscription_index -> 호출할 구독 순번
            payload -> callback으로 전달할 Binance payload
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.account_message_callbacks[subscription_index](payload)


class APIGatewayAccountTests(unittest.TestCase):
    """
    클래스 이름: APIGatewayAccountTests
    기능: 공식 account REST payload의 full AccountSnapshot 변환을 검증한다.
    작성 날짜: 2026/08/21
    """

    def test_fetch_account_snapshot_calls_get_account_and_preserves_all_assets(self) -> None:
        """
        함수 이름: test_fetch_account_snapshot_calls_get_account_and_preserves_all_assets()
        기능: ETH 평가 요청이 get_account을 한 번 호출하고 모든 자산 row를 보존하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        client = FakeBinanceClient()
        gateway = APIGateway(client)

        snapshot = gateway.fetch_account_snapshot("ETH")

        self.assertEqual(client.get_account_call_count, 1)
        self.assertTrue(snapshot.is_full_snapshot)
        self.assertEqual(
            tuple(balance.asset for balance in snapshot.balances),
            ("BTC", "ETH", "USDT"),
        )
        self.assertEqual(snapshot.balances[0].free, Decimal("0.01000000"))
        self.assertEqual(snapshot.balances[1].locked, Decimal("0.25000000"))
        self.assertEqual(snapshot.balances[2].free, Decimal("125.50"))
        self.assertTrue(
            all(
                isinstance(balance.free, Decimal)
                and isinstance(balance.locked, Decimal)
                for balance in snapshot.balances
            )
        )
        self.assertEqual(
            snapshot.updated_at,
            datetime(2026, 8, 21, 0, 0, tzinfo=timezone.utc),
        )
        self.assertIs(snapshot.updated_at.tzinfo, timezone.utc)

    def test_fetch_account_snapshot_rejects_non_eth_valuation_request(self) -> None:
        """
        함수 이름: test_fetch_account_snapshot_rejects_non_eth_valuation_request()
        기능: Phase 4의 ETH 전용 평가 요청이 다른 자산으로 확장되지 않게 한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        client = FakeBinanceClient()
        gateway = APIGateway(client)

        with self.assertRaises((TypeError, ValueError)):
            gateway.fetch_account_snapshot("BTC")

        self.assertEqual(client.get_account_call_count, 0)

    def test_fetch_account_snapshot_rejects_malformed_official_fields(self) -> None:
        """
        함수 이름: test_fetch_account_snapshot_rejects_malformed_official_fields()
        기능: updateTime, balances, asset, free, locked의 잘못된 공식 형식을 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        malformed_payloads: tuple[object, ...] = (
            [],
            {"updateTime": True, "balances": []},
            {"updateTime": REST_UPDATED_AT_MILLISECONDS, "balances": {}},
            {
                "updateTime": REST_UPDATED_AT_MILLISECONDS,
                "balances": [{"asset": "", "free": "1", "locked": "0"}],
            },
            {
                "updateTime": REST_UPDATED_AT_MILLISECONDS,
                "balances": [{"asset": "ETH", "free": 1, "locked": "0"}],
            },
            {
                "updateTime": REST_UPDATED_AT_MILLISECONDS,
                "balances": [{"asset": "ETH", "free": "1", "locked": "-1"}],
            },
            {
                "updateTime": REST_UPDATED_AT_MILLISECONDS,
                "balances": [{"asset": "ETH", "free": "NaN", "locked": "0"}],
            },
        )

        for malformed_payload in malformed_payloads:
            with self.subTest(malformed_payload=malformed_payload):
                client = FakeBinanceClient()
                client.account_payload = malformed_payload

                with self.assertRaises((TypeError, ValueError)):
                    APIGateway(client).fetch_account_snapshot("ETH")

    def test_fetch_account_snapshot_propagates_client_failure(self) -> None:
        """
        함수 이름: test_fetch_account_snapshot_propagates_client_failure()
        기능: account REST client 장애를 빈 snapshot으로 숨기지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        client = FakeBinanceClient()
        client.account_payload = RuntimeError("account REST unavailable")

        with self.assertRaisesRegex(RuntimeError, "account REST unavailable"):
            APIGateway(client).fetch_account_snapshot("ETH")


class WebSocketGatewayAccountTests(unittest.TestCase):
    """
    클래스 이름: WebSocketGatewayAccountTests
    기능: 공식 account event의 partial snapshot, dedup과 stream 세대를 검증한다.
    작성 날짜: 2026/08/21
    """

    def test_account_event_normalizes_current_envelope_to_partial_snapshot(self) -> None:
        """
        함수 이름: test_account_event_normalizes_current_envelope_to_partial_snapshot()
        기능: subscription envelope의 outboundAccountPosition을 partial snapshot으로 변환한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        client = FakeBinanceClient()
        received_snapshots: list[AccountSnapshot] = []
        gateway = WebSocketGateway(
            client,
            account_snapshot_callback=received_snapshots.append,
        )
        gateway.start_account_info_stream()

        client.emit_account_event(0, _official_account_event())

        self.assertEqual(len(received_snapshots), 1)
        snapshot = received_snapshots[0]
        self.assertFalse(snapshot.is_full_snapshot)
        self.assertEqual(snapshot.updated_at.tzinfo, timezone.utc)
        self.assertEqual(
            snapshot.updated_at,
            datetime(2026, 8, 21, 0, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(len(snapshot.balances), 1)
        self.assertEqual(snapshot.balances[0].asset, "ETH")
        self.assertEqual(snapshot.balances[0].free, Decimal("0.80000000"))
        self.assertEqual(snapshot.balances[0].locked, Decimal("0.10000000"))

    def test_account_event_accepts_envelope_without_subscription_id(self) -> None:
        """
        함수 이름: test_account_event_accepts_envelope_without_subscription_id()
        기능: optional subscriptionId가 없는 현재 event envelope도 처리하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        client = FakeBinanceClient()
        received_snapshots: list[AccountSnapshot] = []
        gateway = WebSocketGateway(
            client,
            account_snapshot_callback=received_snapshots.append,
        )
        gateway.start_account_info_stream()

        client.emit_account_event(
            0,
            _official_account_event(include_subscription_id=False),
        )

        self.assertEqual(len(received_snapshots), 1)

    def test_account_stream_deduplicates_stale_and_exact_same_update(self) -> None:
        """
        함수 이름: test_account_stream_deduplicates_stale_and_exact_same_update()
        기능: u watermark 이전과 정확히 같은 u/balances는 무시하고 같은 u의 다른 patch는 적용한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        client = FakeBinanceClient()
        received_snapshots: list[AccountSnapshot] = []
        gateway = WebSocketGateway(
            client,
            account_snapshot_callback=received_snapshots.append,
        )
        gateway.start_account_info_stream()
        first_payload = _official_account_event()

        client.emit_account_event(0, first_payload)
        client.emit_account_event(0, first_payload)
        client.emit_account_event(
            0,
            _official_account_event(
                balances=[{"a": "ETH", "f": "0.8", "l": "0.1"}],
            ),
        )
        client.emit_account_event(
            0,
            _official_account_event(
                update_time_milliseconds=(
                    FIRST_STREAM_UPDATE_MILLISECONDS - 1
                ),
                balances=[{"a": "ETH", "f": "9", "l": "0"}],
            ),
        )
        client.emit_account_event(
            0,
            _official_account_event(
                balances=[{"a": "ETH", "f": "0.7", "l": "0.2"}],
            ),
        )

        self.assertEqual(len(received_snapshots), 2)
        self.assertEqual(
            received_snapshots[0].balances[0].free,
            Decimal("0.80000000"),
        )
        self.assertEqual(
            received_snapshots[1].balances[0].free,
            Decimal("0.7"),
        )
        self.assertEqual(
            received_snapshots[0].updated_at,
            received_snapshots[1].updated_at,
        )

    def test_old_account_stream_generation_is_ignored(self) -> None:
        """
        함수 이름: test_old_account_stream_generation_is_ignored()
        기능: 새 구독 후 이전 세대 callback이 Account snapshot을 발행하지 못하게 한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        client = FakeBinanceClient()
        received_snapshots: list[AccountSnapshot] = []
        gateway = WebSocketGateway(
            client,
            account_snapshot_callback=received_snapshots.append,
        )
        gateway.start_account_info_stream()
        gateway.start_account_info_stream()

        client.emit_account_event(0, _official_account_event())
        client.emit_account_event(1, _official_account_event())

        self.assertEqual(len(received_snapshots), 1)

    def test_account_stream_rejects_malformed_official_events(self) -> None:
        """
        함수 이름: test_account_stream_rejects_malformed_official_events()
        기능: event envelope, type, timestamp, B row의 잘못된 공식 형식을 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        malformed_events: tuple[object, ...] = (
            {},
            {"subscriptionId": 1, "event": []},
            {"event": {"B": []}},
            {"event": {"e": None}},
            {"event": {"e": ""}},
            {
                "event": {
                    "e": "outboundAccountPosition",
                    "E": True,
                    "u": FIRST_STREAM_UPDATE_MILLISECONDS,
                    "B": [],
                }
            },
            {
                "event": {
                    "e": "outboundAccountPosition",
                    "E": FIRST_STREAM_EVENT_MILLISECONDS,
                    "u": "1787270460000",
                    "B": [],
                }
            },
            {
                "event": {
                    "e": "outboundAccountPosition",
                    "E": FIRST_STREAM_EVENT_MILLISECONDS,
                    "u": FIRST_STREAM_UPDATE_MILLISECONDS,
                    "B": [{"a": "ETH", "f": 1, "l": "0"}],
                }
            },
            {
                "event": {
                    "e": "outboundAccountPosition",
                    "E": FIRST_STREAM_EVENT_MILLISECONDS,
                    "u": FIRST_STREAM_UPDATE_MILLISECONDS,
                    "B": [{"a": "ETH", "f": "1", "l": "-1"}],
                }
            },
        )

        for malformed_event in malformed_events:
            with self.subTest(malformed_event=malformed_event):
                client = FakeBinanceClient()
                received_snapshots: list[AccountSnapshot] = []
                gateway = WebSocketGateway(
                    client,
                    account_snapshot_callback=received_snapshots.append,
                )
                gateway.start_account_info_stream()

                with self.assertRaises((TypeError, ValueError)):
                    client.emit_account_event(0, malformed_event)

                self.assertEqual(received_snapshots, [])
                self.assertTrue(client.account_subscriptions[0].closed)

                gateway.start_account_info_stream()

                self.assertEqual(len(client.account_subscriptions), 2)
                self.assertTrue(client.account_subscriptions[0].closed)

    def test_account_stream_closes_transport_when_domain_callback_fails(self) -> None:
        """
        함수 이름: test_account_stream_closes_transport_when_domain_callback_fails()
        기능: Account callback 예외가 비동기 transport 구독을 남기지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        client = FakeBinanceClient()

        def fail_snapshot_callback(snapshot: AccountSnapshot) -> None:
            """
            함수 이름: fail_snapshot_callback()
            기능: 정규화 뒤 domain 적용 실패를 결정론적으로 발생시킨다.
            인자: snapshot -> 검증을 통과한 partial AccountSnapshot
            반환값: 정상 반환 없이 RuntimeError 발생
            작성 날짜: 2026/08/21
            """
            raise RuntimeError("account apply failed")

        gateway = WebSocketGateway(
            client,
            account_snapshot_callback=fail_snapshot_callback,
        )
        gateway.start_account_info_stream()

        with self.assertRaisesRegex(RuntimeError, "account apply failed"):
            client.emit_account_event(0, _official_account_event())

        self.assertTrue(client.account_subscriptions[0].closed)

    def test_account_stream_termination_is_typed_and_closes_transport(self) -> None:
        """
        함수 이름: test_account_stream_termination_is_typed_and_closes_transport()
        기능: 공식 eventStreamTerminated가 정상 연결로 숨지 않고 구독을 정리하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        client = FakeBinanceClient()
        received_snapshots: list[AccountSnapshot] = []
        gateway = WebSocketGateway(
            client,
            account_snapshot_callback=received_snapshots.append,
        )
        gateway.start_account_info_stream()

        with self.assertRaises(AccountStreamStateError):
            client.emit_account_event(
                0,
                {
                    "subscriptionId": 7,
                    "event": {
                        "e": "eventStreamTerminated",
                        "E": FIRST_STREAM_EVENT_MILLISECONDS,
                    },
                },
            )

        self.assertEqual(received_snapshots, [])
        self.assertTrue(client.account_subscriptions[0].closed)

    def test_account_stream_ignores_other_valid_user_data_events(self) -> None:
        """
        함수 이름: test_account_stream_ignores_other_valid_user_data_events()
        기능: 같은 User Data Stream의 balanceUpdate를 오류나 account snapshot으로 오인하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        client = FakeBinanceClient()
        received_snapshots: list[AccountSnapshot] = []
        gateway = WebSocketGateway(
            client,
            account_snapshot_callback=received_snapshots.append,
        )
        gateway.start_account_info_stream()

        client.emit_account_event(
            0,
            {
                "subscriptionId": 7,
                "event": {
                    "e": "balanceUpdate",
                    "E": FIRST_STREAM_EVENT_MILLISECONDS,
                    "a": "ETH",
                    "d": "0.10000000",
                    "T": FIRST_STREAM_UPDATE_MILLISECONDS,
                },
            },
        )

        self.assertEqual(received_snapshots, [])

    def test_account_stream_start_failure_is_not_hidden(self) -> None:
        """
        함수 이름: test_account_stream_start_failure_is_not_hidden()
        기능: subscribe_account_info 장애가 start 성공으로 바뀌지 않고 호출자에게 전파되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        client = FakeBinanceClient()
        client.account_stream_error = RuntimeError(
            "account stream unavailable"
        )
        gateway = WebSocketGateway(
            client,
            account_snapshot_callback=lambda snapshot: None,
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "account stream unavailable",
        ):
            gateway.start_account_info_stream()


if __name__ == "__main__":
    unittest.main()
