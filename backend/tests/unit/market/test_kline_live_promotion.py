"""Kline buffer에서 동일 구독 live observer로 넘어가는 무손실 계약을 검증한다."""

from collections.abc import Callable
from decimal import Decimal
from threading import Event, Thread
import unittest
from unittest.mock import patch

from binance_auto_trader.adapters.binance import (
    KlineBufferStateError,
    WebSocketGateway,
)
from binance_auto_trader.adapters.binance import websocket_gateway
from binance_auto_trader.domain.market import Interval, Kline


OPEN_TIME_MILLISECONDS = 1_699_920_000_000


def _create_kline_event(
    *,
    close: str,
    event_offset_milliseconds: int,
) -> dict[str, object]:
    """
    함수 이름: _create_kline_event()
    기능: 같은 1분봉의 서로 다른 공식 Binance update event fixture를 만든다.
    인자: close -> event가 전달할 종가 문자열
        event_offset_milliseconds -> 봉 시작 뒤 E event 시각 차이
    반환값: 공식 raw Kline event shape의 mapping
    작성 날짜: 2026/08/25
    """
    # OHLC invariant를 유지하면서 close 차이만 관찰할 수 있는 가격 범위를 만든다.
    close_decimal = Decimal(close)
    return {
        "e": "kline",
        "E": OPEN_TIME_MILLISECONDS + event_offset_milliseconds,
        "s": "ETHUSDT",
        "k": {
            "t": OPEN_TIME_MILLISECONDS,
            "T": OPEN_TIME_MILLISECONDS + 59_999,
            "s": "ETHUSDT",
            "i": "1m",
            "f": 1,
            "L": 1,
            "o": close,
            "c": close,
            "h": format(close_decimal + Decimal("1"), "f"),
            "l": format(close_decimal - Decimal("1"), "f"),
            "v": "5",
            "n": 1,
            "x": False,
            "q": "500",
            "V": "2",
            "Q": "200",
            "B": "0",
        },
    }


class FakeSubscription:
    """
    클래스 이름: FakeSubscription
    기능: 동일 transport 구독 유지와 새 generation 종료를 기록한다.
    작성 날짜: 2026/08/25
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 열려 있는 fake 구독과 0회 close 기록을 준비한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        self.closed = False
        self.close_call_count = 0

    def close(self) -> None:
        """
        함수 이름: close()
        기능: transport 구독 종료 횟수와 상태를 기록한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        self.close_call_count += 1
        self.closed = True


class FakeWebSocketClient:
    """
    클래스 이름: FakeWebSocketClient
    기능: generation별 callback과 구독 handle을 외부 I/O 없이 제공한다.
    작성 날짜: 2026/08/25
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 비어 있는 callback과 구독 기록을 초기화한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        self.message_callbacks: list[Callable[[object], None]] = []
        self.disconnect_callbacks: list[Callable[[], None]] = []
        self.subscriptions: list[FakeSubscription] = []

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
        기능: 요청 형식을 확인하고 새 generation callback과 handle을 보존한다.
        인자: symbol -> Gateway가 정규화한 구독 symbol
            intervals -> Gateway가 정규화한 interval 문자열
            on_message -> 원본 payload callback
            on_disconnect -> transport 단절 callback
        반환값: 새 fake transport 구독 handle
        작성 날짜: 2026/08/25
        """
        # Gateway가 요청한 단일 1분봉 구독 외의 호출은 테스트 범위 밖이므로 거부한다.
        if symbol != "ETHUSDT" or intervals != ("1m",):
            raise AssertionError("unexpected Kline subscription")

        subscription = FakeSubscription()
        self.message_callbacks.append(on_message)
        self.disconnect_callbacks.append(on_disconnect)
        self.subscriptions.append(subscription)
        return subscription

    def emit(self, generation_index: int, payload: object) -> None:
        """
        함수 이름: emit()
        기능: 선택한 과거 또는 현재 generation callback에 payload를 전달한다.
        인자: generation_index -> 0부터 시작하는 구독 generation index
            payload -> callback에 전달할 raw event
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        self.message_callbacks[generation_index](payload)


class KlineLivePromotionTests(unittest.TestCase):
    """
    클래스 이름: KlineLivePromotionTests
    기능: promotion 경계 race와 generation·event 중복 제거를 검증한다.
    작성 날짜: 2026/08/25
    """

    def test_handoff_race_delivers_buffer_before_live_on_same_subscription(
        self,
    ) -> None:
        """
        함수 이름: test_handoff_race_delivers_buffer_before_live_on_same_subscription()
        기능: parse 중 promotion이 경쟁해도 buffer event가 live event보다 정확히 한 번 앞서는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        # 첫 callback parse를 멈춘 상태에서 promotion이 같은 Gateway lock을 기다리게 한다.
        client = FakeWebSocketClient()
        gateway = WebSocketGateway(client)
        subscription = gateway.start_all_kline_buffering(
            "ETHUSDT",
            (Interval.ONE_MINUTE,),
        )
        parse_started = Event()
        allow_parse = Event()
        promotion_finished = Event()
        callback_errors: list[Exception] = []
        promotion_results: list[object] = []
        observed: list[Kline] = []
        production_parser = websocket_gateway._parse_websocket_kline

        def parse_after_release(payload: object) -> Kline:
            """
            함수 이름: parse_after_release()
            기능: callback이 Gateway 임계 구역 안에서 parse 중인 경계를 노출한다.
            인자: payload -> production parser에 전달할 raw event
            반환값: production parser가 만든 Kline
            작성 날짜: 2026/08/25
            """
            parse_started.set()
            if not allow_parse.wait(timeout=1):
                raise RuntimeError("test did not release Kline parser")

            return production_parser(payload)

        def emit_buffer_event() -> None:
            """
            함수 이름: emit_buffer_event()
            기능: 별도 thread에서 promotion과 경쟁할 첫 event를 전달한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/08/25
            """
            try:
                client.emit(
                    0,
                    _create_kline_event(
                        close="101",
                        event_offset_milliseconds=1_000,
                    ),
                )
            except Exception as error:
                callback_errors.append(error)

        def promote() -> None:
            """
            함수 이름: promote()
            기능: 별도 thread에서 같은 구독을 live observer로 승격한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/08/25
            """
            try:
                promotion_results.append(
                    gateway.promote_kline_buffer_to_live(
                        observed.append,
                        subscription,
                    )
                )
            except Exception as error:
                callback_errors.append(error)
            finally:
                promotion_finished.set()

        with patch.object(
            websocket_gateway,
            "_parse_websocket_kline",
            side_effect=parse_after_release,
        ):
            callback_thread = Thread(target=emit_buffer_event)
            callback_thread.start()
            self.assertTrue(parse_started.wait(timeout=1))
            promotion_thread = Thread(target=promote)
            promotion_thread.start()
            self.assertFalse(promotion_finished.wait(timeout=0.1))
            allow_parse.set()
            callback_thread.join(timeout=1)
            promotion_thread.join(timeout=1)

        # promotion 완료 뒤 두 번째 update는 buffer가 아니라 동일 observer로 직접 전달된다.
        client.emit(
            0,
            _create_kline_event(
                close="102",
                event_offset_milliseconds=2_000,
            ),
        )

        # 첫 구독이 닫히거나 다시 열리지 않았고 두 event가 정확히 한 번씩 순서를 유지한다.
        self.assertEqual(callback_errors, [])
        self.assertEqual(promotion_results, [subscription])
        self.assertEqual(
            tuple(kline.close for kline in observed),
            (Decimal("101"), Decimal("102")),
        )
        self.assertEqual(len(client.subscriptions), 1)
        self.assertFalse(client.subscriptions[0].closed)

    def test_duplicate_and_stale_generation_events_are_not_redelivered(
        self,
    ) -> None:
        """
        함수 이름: test_duplicate_and_stale_generation_events_are_not_redelivered()
        기능: exact duplicate와 이전 generation callback이 현재 observer를 오염시키지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        # 첫 generation을 승격한 뒤 같은 wire key를 다시 보내 멱등성을 확인한다.
        client = FakeWebSocketClient()
        gateway = WebSocketGateway(client)
        first_observed: list[Kline] = []
        second_observed: list[Kline] = []
        duplicate_event = _create_kline_event(
            close="101",
            event_offset_milliseconds=1_000,
        )
        first_subscription = gateway.start_all_kline_buffering(
            "ETHUSDT",
            (Interval.ONE_MINUTE,),
        )
        client.emit(0, duplicate_event)
        gateway.promote_kline_buffer_to_live(
            first_observed.append,
            first_subscription,
        )
        client.emit(0, duplicate_event)

        # 새 generation 시작 뒤 과거 callback은 malformed여도 무시하고 현재 event만 보존한다.
        second_subscription = gateway.start_all_kline_buffering(
            "ETHUSDT",
            (Interval.ONE_MINUTE,),
        )
        client.emit(0, "malformed stale generation payload")
        client.emit(
            1,
            _create_kline_event(
                close="202",
                event_offset_milliseconds=2_000,
            ),
        )
        gateway.promote_kline_buffer_to_live(
            second_observed.append,
            second_subscription,
        )
        client.emit(
            1,
            _create_kline_event(
                close="202",
                event_offset_milliseconds=2_000,
            ),
        )

        # generation별 observer에는 각 고유 event가 한 번만 남고 이전 handle 승격은 거부된다.
        self.assertEqual(len(first_observed), 1)
        self.assertEqual(len(second_observed), 1)
        self.assertEqual(second_observed[0].close, Decimal("202"))
        self.assertTrue(client.subscriptions[0].closed)
        self.assertFalse(client.subscriptions[1].closed)
        with self.assertRaises(KlineBufferStateError):
            gateway.promote_kline_buffer_to_live(
                first_observed.append,
                first_subscription,
            )

    def test_fingerprint_history_is_bounded_and_evicted_stale_event_is_ignored(
        self,
    ) -> None:
        """
        함수 이름: test_fingerprint_history_is_bounded_and_evicted_stale_event_is_ignored()
        기능: 장시간 stream의 duplicate 기록 상한과 상한 밖 역행 event 차단을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        # 작은 주입 상한으로 eviction을 만들어도 production cursor 규칙은 그대로 사용한다.
        client = FakeWebSocketClient()
        gateway = WebSocketGateway(client)
        observed: list[Kline] = []
        subscription = gateway.start_all_kline_buffering(
            "ETHUSDT",
            (Interval.ONE_MINUTE,),
        )
        gateway.promote_kline_buffer_to_live(
            observed.append,
            subscription,
        )
        events = tuple(
            _create_kline_event(
                close=str(100 + event_offset),
                event_offset_milliseconds=event_offset * 1_000,
            )
            for event_offset in (1, 2, 3)
        )
        with patch.object(
            websocket_gateway,
            "_MAXIMUM_KLINE_EVENT_FINGERPRINTS",
            2,
        ):
            for event in events:
                client.emit(0, event)

            self.assertEqual(gateway.kline_replay_buffer_size, 2)
            client.emit(0, events[0])
            self.assertTrue(gateway.kline_live_ready)

        # Eviction 후 오래된 duplicate는 재전달되지 않고 현재 generation을 fail closed한다.
        self.assertEqual(len(observed), 3)

    def test_disconnect_notifies_current_generation_once_and_close_is_silent(
        self,
    ) -> None:
        """
        함수 이름: test_disconnect_notifies_current_generation_once_and_close_is_silent()
        기능: 비정상 현재 세대만 한 번 알리고 소유자 close와 stale callback은 알리지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        # 첫 세대를 live로 승격한 뒤 동일 disconnect callback을 두 번 실행한다.
        client = FakeWebSocketClient()
        gateway = WebSocketGateway(client)
        reconciliation_reasons: list[str] = []
        first_subscription = gateway.start_all_kline_buffering(
            "ETHUSDT",
            (Interval.ONE_MINUTE,),
            reconciliation_required_callback=(
                reconciliation_reasons.append
            ),
        )
        gateway.promote_kline_buffer_to_live(
            lambda _kline: None,
            first_subscription,
        )
        self.assertTrue(gateway.kline_live_ready)
        client.disconnect_callbacks[0]()
        client.disconnect_callbacks[0]()

        # 최초 현재 세대 장애만 전달되고 stale callback과 다음 세대 소유자 close는 조용하다.
        self.assertFalse(gateway.kline_live_ready)
        self.assertEqual(
            reconciliation_reasons,
            ["kline_stream_disconnected"],
        )
        second_subscription = gateway.start_all_kline_buffering(
            "ETHUSDT",
            (Interval.ONE_MINUTE,),
            reconciliation_required_callback=(
                reconciliation_reasons.append
            ),
        )
        client.disconnect_callbacks[0]()
        second_subscription.close()
        self.assertEqual(
            reconciliation_reasons,
            ["kline_stream_disconnected"],
        )


if __name__ == "__main__":
    unittest.main()
