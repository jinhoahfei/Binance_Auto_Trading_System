"""Binance REST와 WebSocket Kline Gateway의 공식 payload 계약을 검증한다."""

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch

from binance_auto_trader.adapters.binance import (
    APIGateway,
    KlineBufferStateError,
    WebSocketGateway,
)
from binance_auto_trader.adapters.binance import websocket_gateway
from binance_auto_trader.domain.market import (
    Interval,
    SUPPORTED_INTERVALS,
)


UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
DEFAULT_OPEN_TIME_MILLISECONDS = 1_699_920_000_000
FIXED_NOW_MILLISECONDS = DEFAULT_OPEN_TIME_MILLISECONDS + 60_000
FIXED_NOW = UNIX_EPOCH + timedelta(milliseconds=FIXED_NOW_MILLISECONDS)
INTERVAL_MILLISECONDS = {
    "1m": 60_000,
    "30m": 1_800_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def _official_rest_row(
    *,
    interval: str = "1m",
    open_time_milliseconds: int = DEFAULT_OPEN_TIME_MILLISECONDS,
    close_time_milliseconds: int | None = None,
    close_price: str = "101.25",
    overrides: Mapping[int, object] | None = None,
) -> list[object]:
    """
    함수 이름: _official_rest_row()
    기능: Binance Spot REST 문서의 정확한 12필드 Kline row fixture를 만든다.
    인자: interval -> 봉의 Binance interval 문자열
        open_time_milliseconds -> 봉 시작 Unix millisecond
        close_time_milliseconds -> 봉 종료 Unix millisecond
        close_price -> 종가 decimal 문자열
        overrides -> index별 malformed 또는 변형 fixture 값
    반환값: 공식 순서를 따르는 12필드 Kline row
    작성 날짜: 2026/08/20
    """
    selected_close_time = close_time_milliseconds
    if selected_close_time is None:
        selected_close_time = (
            open_time_milliseconds + INTERVAL_MILLISECONDS[interval] - 1
        )

    row: list[object] = [
        open_time_milliseconds,
        "100.10",
        "102.30",
        "99.50",
        close_price,
        "12.500",
        selected_close_time,
        "1265.625",
        42,
        "6.250",
        "632.8125",
        "0",
    ]

    if overrides is not None:
        for field_index, field_value in overrides.items():
            row[field_index] = field_value

    return row


def _official_websocket_event(
    *,
    symbol: str = "ETHUSDT",
    interval: str = "1m",
    open_time_milliseconds: int = DEFAULT_OPEN_TIME_MILLISECONDS,
    close_time_milliseconds: int | None = None,
    close_price: str = "101.25",
    closed: bool = False,
    event_overrides: Mapping[str, object] | None = None,
    kline_overrides: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """
    함수 이름: _official_websocket_event()
    기능: Binance Spot WebSocket 문서의 raw Kline event fixture를 만든다.
    인자: symbol -> payload의 Binance symbol
        interval -> payload의 공식 Kline interval 문자열
        open_time_milliseconds -> 봉 시작 Unix millisecond
        close_time_milliseconds -> 봉 종료 Unix millisecond
        close_price -> 종가 decimal 문자열
        closed -> 공식 x 확정봉 flag
        event_overrides -> 최상위 event 필드 변형값
        kline_overrides -> 내부 k 필드 변형값
    반환값: 공식 raw Kline event object
    작성 날짜: 2026/08/20
    """
    selected_close_time = close_time_milliseconds
    if selected_close_time is None:
        selected_close_time = (
            open_time_milliseconds + INTERVAL_MILLISECONDS[interval] - 1
        )

    kline_payload: dict[str, object] = {
        "t": open_time_milliseconds,
        "T": selected_close_time,
        "s": symbol,
        "i": interval,
        "f": 100,
        "L": 200,
        "o": "100.10",
        "c": close_price,
        "h": "102.30",
        "l": "99.50",
        "v": "12.500",
        "n": 101,
        "x": closed,
        "q": "1265.625",
        "V": "6.250",
        "Q": "632.8125",
        "B": "0",
    }
    if kline_overrides is not None:
        kline_payload.update(kline_overrides)

    event_payload: dict[str, object] = {
        "e": "kline",
        "E": open_time_milliseconds + 1_000,
        "s": symbol,
        "k": kline_payload,
    }
    if event_overrides is not None:
        event_payload.update(event_overrides)

    return event_payload


class FakeRESTClient:
    """
    클래스 이름: FakeRESTClient
    기능: 주기별 REST fixture를 기록 가능한 get_klines 계약으로 제공한다.
    작성 날짜: 2026/08/20
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 네 interval의 정상 fixture와 빈 호출 기록을 초기화한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self.payloads: dict[str, object] = {
            interval.value: [_official_rest_row(interval=interval.value)]
            for interval in SUPPORTED_INTERVALS
        }
        self.calls: list[tuple[str, str, int]] = []

    def get_klines(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int,
    ) -> object:
        """
        함수 이름: get_klines()
        기능: 호출 인자를 기록하고 지정 interval의 fixture 또는 예외를 반환한다.
        인자: symbol -> Gateway가 전달한 정규화 symbol
            interval -> Gateway가 전달한 공식 interval 문자열
            limit -> Gateway가 전달한 조회 개수
        반환값: 설정된 REST payload
        작성 날짜: 2026/08/20
        """
        self.calls.append((symbol, interval, limit))
        payload = self.payloads[interval]
        if isinstance(payload, BaseException):
            raise payload

        return payload


class FakeSubscription:
    """
    클래스 이름: FakeSubscription
    기능: WebSocketGateway가 opaque handle로 보존할 fake 구독을 나타낸다.
    작성 날짜: 2026/08/20
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 종료되지 않은 fake 구독 상태를 초기화한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self.closed = False
        self.close_call_count = 0

    def close(self) -> None:
        """
        함수 이름: close()
        기능: fake 구독이 종료됐음을 기록한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self.close_call_count += 1
        if self.closed:
            raise RuntimeError("subscription was closed more than once")

        self.closed = True


class FakeWebSocketClient:
    """
    클래스 이름: FakeWebSocketClient
    기능: 구독 callback을 보존해 동기적으로 event와 disconnect를 발생시킨다.
    작성 날짜: 2026/08/20
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 빈 구독·callback·호출 기록을 초기화한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.subscriptions: list[FakeSubscription] = []
        self.message_callbacks: list[Callable[[object], None]] = []
        self.disconnect_callbacks: list[Callable[[], None]] = []
        self.disconnect_during_subscribe = False

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
        기능: 구독 인자와 callback을 기록하고 새 opaque handle을 반환한다.
        인자: symbol -> Gateway가 전달한 정규화 symbol
            intervals -> Gateway가 전달한 공식 interval 문자열 tuple
            on_message -> Kline payload callback
            on_disconnect -> 연결 종료 callback
        반환값: 새 fake 구독 handle
        작성 날짜: 2026/08/20
        """
        subscription = FakeSubscription()
        self.calls.append((symbol, intervals))
        self.subscriptions.append(subscription)
        self.message_callbacks.append(on_message)
        self.disconnect_callbacks.append(on_disconnect)
        if self.disconnect_during_subscribe:
            on_disconnect()

        return subscription

    def emit(self, subscription_index: int, payload: object) -> None:
        """
        함수 이름: emit()
        기능: 지정 구독 세대의 message callback에 payload를 전달한다.
        인자: subscription_index -> callback을 선택할 구독 순번
            payload -> callback에 전달할 raw 또는 combined payload
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self.message_callbacks[subscription_index](payload)

    def disconnect(self, subscription_index: int) -> None:
        """
        함수 이름: disconnect()
        기능: 지정 구독 세대의 disconnect callback을 실행한다.
        인자: subscription_index -> callback을 선택할 구독 순번
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self.disconnect_callbacks[subscription_index]()


class APIGatewayTests(unittest.TestCase):
    """
    클래스 이름: APIGatewayTests
    기능: 공식 Binance REST Kline 정규화와 입력 검증을 확인한다.
    작성 날짜: 2026/08/20
    """

    def test_load_all_klines_calls_four_intervals_in_canonical_order(self) -> None:
        """
        함수 이름: test_load_all_klines_calls_four_intervals_in_canonical_order()
        기능: 네 interval을 canonical 순서와 같은 symbol·limit로 한 번씩 조회하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        rest_client = FakeRESTClient()
        clock = Mock(return_value=FIXED_NOW)
        gateway = APIGateway(rest_client, clock=clock)

        result = gateway.load_all_klines("ethusdt", limit=17)

        self.assertEqual(tuple(result), SUPPORTED_INTERVALS)
        self.assertEqual(
            rest_client.calls,
            [
                ("ETHUSDT", interval.value, 17)
                for interval in SUPPORTED_INTERVALS
            ],
        )
        self.assertTrue(
            all(isinstance(result[interval], tuple) for interval in result)
        )
        self.assertEqual(clock.call_count, 2)

    def test_rest_row_uses_direct_decimal_utc_and_shared_cutoff(self) -> None:
        """
        함수 이름: test_rest_row_uses_direct_decimal_utc_and_shared_cutoff()
        기능: REST 문자열 수치·millisecond UTC·close time 기준 확정 여부를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        rest_client = FakeRESTClient()
        rest_client.payloads[Interval.ONE_MINUTE.value] = [
            _official_rest_row(
                interval="1m",
                close_price="101.25000000",
            )
        ]
        rest_client.payloads[Interval.THIRTY_MINUTES.value] = [
            _official_rest_row(interval="30m")
        ]
        clock = Mock(return_value=FIXED_NOW)

        result = APIGateway(rest_client, clock=clock).load_all_klines(
            "ETHUSDT"
        )

        one_minute_kline = result[Interval.ONE_MINUTE][0]
        self.assertEqual(one_minute_kline.open, Decimal("100.10"))
        self.assertEqual(one_minute_kline.close, Decimal("101.25000000"))
        self.assertEqual(one_minute_kline.volume, Decimal("12.500"))
        self.assertIsInstance(one_minute_kline.open, Decimal)
        self.assertEqual(
            one_minute_kline.open_time,
            UNIX_EPOCH
            + timedelta(milliseconds=DEFAULT_OPEN_TIME_MILLISECONDS),
        )
        self.assertIs(one_minute_kline.open_time.tzinfo, timezone.utc)
        self.assertTrue(one_minute_kline.closed)
        self.assertFalse(result[Interval.THIRTY_MINUTES][0].closed)
        self.assertEqual(clock.call_count, 2)

    def test_closed_cutoff_is_sampled_after_all_rest_payloads(self) -> None:
        """
        함수 이름: test_closed_cutoff_is_sampled_after_all_rest_payloads()
        기능: 순차 REST 조회 중 봉 경계가 지나도 최종 cutoff으로 closed를 판정하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        rest_client = FakeRESTClient()
        boundary_milliseconds = (
            DEFAULT_OPEN_TIME_MILLISECONDS
            + INTERVAL_MILLISECONDS[Interval.FOUR_HOURS.value]
        )
        rest_client.payloads[Interval.FOUR_HOURS.value] = [
            _official_rest_row(
                interval=Interval.FOUR_HOURS.value,
            ),
            _official_rest_row(
                interval=Interval.FOUR_HOURS.value,
                open_time_milliseconds=boundary_milliseconds,
            ),
        ]

        def rollover_clock() -> datetime:
            """
            함수 이름: rollover_clock()
            기능: REST 네 호출 전후의 4H 경계 시각을 반환한다.
            인자: 없음
            반환값: 경계 직전 또는 직후 UTC datetime
            작성 날짜: 2026/08/20
            """
            if len(rest_client.calls) < len(SUPPORTED_INTERVALS):
                selected_milliseconds = boundary_milliseconds - 1
            else:
                selected_milliseconds = boundary_milliseconds + 1_000

            return UNIX_EPOCH + timedelta(
                milliseconds=selected_milliseconds
            )

        result = APIGateway(
            rest_client,
            clock=rollover_clock,
        ).load_all_klines("ETHUSDT")

        self.assertEqual(
            [kline.closed for kline in result[Interval.FOUR_HOURS]],
            [True, False],
        )

    def test_rest_close_time_is_inclusive_at_exact_millisecond(self) -> None:
        """
        함수 이름: test_rest_close_time_is_inclusive_at_exact_millisecond()
        기능: clock이 봉의 마지막 millisecond와 같을 때 아직 진행 중인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        rest_client = FakeRESTClient()
        exact_close_time = (
            DEFAULT_OPEN_TIME_MILLISECONDS
            + INTERVAL_MILLISECONDS["1m"]
            - 1
        )
        clock = Mock(
            return_value=(
                UNIX_EPOCH + timedelta(milliseconds=exact_close_time)
            )
        )

        result = APIGateway(rest_client, clock=clock).load_all_klines(
            "ETHUSDT"
        )

        self.assertFalse(result[Interval.ONE_MINUTE][0].closed)

    def test_load_all_klines_rejects_invalid_symbol_and_limit(self) -> None:
        """
        함수 이름: test_load_all_klines_rejects_invalid_symbol_and_limit()
        기능: symbol과 1 이상 1000 이하 정수 limit 계약을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        invalid_symbols: tuple[object, ...] = (
            None,
            "",
            "   ",
            "ETH/USDT",
            "ETH_USDT",
            "이더USDT",
        )
        for invalid_symbol in invalid_symbols:
            with self.subTest(symbol=invalid_symbol):
                with self.assertRaises((TypeError, ValueError)):
                    APIGateway(FakeRESTClient()).load_all_klines(
                        invalid_symbol  # type: ignore[arg-type]
                    )

        invalid_limits: tuple[object, ...] = (
            True,
            False,
            "500",
            Decimal("500"),
            0,
            1001,
            -1,
        )
        for invalid_limit in invalid_limits:
            with self.subTest(limit=invalid_limit):
                with self.assertRaises((TypeError, ValueError)):
                    APIGateway(FakeRESTClient()).load_all_klines(
                        "ETHUSDT",
                        invalid_limit,  # type: ignore[arg-type]
                    )

    def test_load_all_klines_rejects_invalid_clock_before_rest_calls(self) -> None:
        """
        함수 이름: test_load_all_klines_rejects_invalid_clock_before_rest_calls()
        기능: naive·비 UTC·datetime 아닌 clock 결과가 REST 조회 전에 거부되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        invalid_clock_values: tuple[object, ...] = (
            datetime(2026, 8, 20),
            datetime(
                2026,
                8,
                20,
                tzinfo=timezone(timedelta(hours=9)),
            ),
            "2026-08-20T00:00:00Z",
        )

        for invalid_clock_value in invalid_clock_values:
            with self.subTest(clock_value=invalid_clock_value):
                rest_client = FakeRESTClient()
                gateway = APIGateway(
                    rest_client,
                    clock=Mock(return_value=invalid_clock_value),
                )

                with self.assertRaises((TypeError, ValueError)):
                    gateway.load_all_klines("ETHUSDT")

                self.assertEqual(rest_client.calls, [])

    def test_rest_payload_requires_an_array_of_exact_twelve_field_rows(self) -> None:
        """
        함수 이름: test_rest_payload_requires_an_array_of_exact_twelve_field_rows()
        기능: REST outer array와 각 row의 정확한 12필드 구조를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        malformed_payloads: tuple[object, ...] = (
            {"row": _official_rest_row()},
            [_official_rest_row()[:-1]],
            [_official_rest_row() + ["extra"]],
            ["not-a-row"],
        )

        for malformed_payload in malformed_payloads:
            with self.subTest(payload=malformed_payload):
                rest_client = FakeRESTClient()
                rest_client.payloads[Interval.ONE_MINUTE.value] = (
                    malformed_payload
                )

                with self.assertRaises((TypeError, ValueError)):
                    APIGateway(
                        rest_client,
                        clock=Mock(return_value=FIXED_NOW),
                    ).load_all_klines("ETHUSDT")

    def test_rest_payload_rejects_malformed_timestamps_decimals_and_metadata(self) -> None:
        """
        함수 이름: test_rest_payload_rejects_malformed_timestamps_decimals_and_metadata()
        기능: REST timestamp·decimal·metadata 타입과 값이 공식 schema와 다른 경우를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        malformed_rows = (
            _official_rest_row(overrides={0: True}),
            _official_rest_row(overrides={0: -1}),
            _official_rest_row(
                open_time_milliseconds=2_000,
                close_time_milliseconds=1_999,
            ),
            _official_rest_row(
                open_time_milliseconds=(
                    DEFAULT_OPEN_TIME_MILLISECONDS + 1_000
                ),
            ),
            _official_rest_row(
                close_time_milliseconds=(
                    DEFAULT_OPEN_TIME_MILLISECONDS
                    + INTERVAL_MILLISECONDS["1d"]
                    - 1
                ),
            ),
            _official_rest_row(overrides={1: 100}),
            _official_rest_row(overrides={2: "NaN"}),
            _official_rest_row(overrides={5: "Infinity"}),
            _official_rest_row(overrides={7: "-1"}),
            _official_rest_row(overrides={8: True}),
            _official_rest_row(overrides={11: 0}),
        )

        for malformed_row in malformed_rows:
            with self.subTest(row=malformed_row):
                rest_client = FakeRESTClient()
                rest_client.payloads[Interval.ONE_MINUTE.value] = [
                    malformed_row
                ]

                with self.assertRaises((TypeError, ValueError)):
                    APIGateway(
                        rest_client,
                        clock=Mock(return_value=FIXED_NOW),
                    ).load_all_klines("ETHUSDT")

    def test_one_interval_failure_never_returns_a_partial_mapping(self) -> None:
        """
        함수 이름: test_one_interval_failure_never_returns_a_partial_mapping()
        기능: 한 interval REST 실패가 부분 성공 mapping으로 반환되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        rest_client = FakeRESTClient()
        rest_client.payloads[Interval.FOUR_HOURS.value] = RuntimeError(
            "temporary REST failure"
        )
        gateway = APIGateway(
            rest_client,
            clock=Mock(return_value=FIXED_NOW),
        )

        with self.assertRaisesRegex(RuntimeError, "temporary REST failure"):
            gateway.load_all_klines("ETHUSDT")

        self.assertEqual(
            [interval for _, interval, _ in rest_client.calls],
            ["1m", "30m", "4h"],
        )


class WebSocketGatewayTests(unittest.TestCase):
    """
    클래스 이름: WebSocketGatewayTests
    기능: 공식 Binance raw·combined Kline stream과 세대별 buffer 계약을 검증한다.
    작성 날짜: 2026/08/20
    """

    def test_start_uses_normalized_symbol_and_requested_official_intervals(self) -> None:
        """
        함수 이름: test_start_uses_normalized_symbol_and_requested_official_intervals()
        기능: 구독 client에 정규화 symbol과 공식 interval 문자열 tuple을 전달하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        web_socket_client = FakeWebSocketClient()
        gateway = WebSocketGateway(web_socket_client)

        subscription = gateway.start_all_kline_buffering(
            " ethusdt ",
            set(SUPPORTED_INTERVALS),
        )

        self.assertIsNot(subscription, web_socket_client.subscriptions[0])
        self.assertEqual(
            web_socket_client.calls,
            [("ETHUSDT", ("1m", "30m", "4h", "1d"))],
        )

    def test_synchronous_disconnect_during_subscribe_fails_and_closes_handle(
        self,
    ) -> None:
        """
        함수 이름: test_synchronous_disconnect_during_subscribe_fails_and_closes_handle()
        기능: 구독 시작 내부의 동기 disconnect를 성공으로 오인하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        web_socket_client = FakeWebSocketClient()
        web_socket_client.disconnect_during_subscribe = True
        gateway = WebSocketGateway(web_socket_client)

        with self.assertRaises(KlineBufferStateError):
            gateway.start_all_kline_buffering(
                "ETHUSDT",
                set(SUPPORTED_INTERVALS),
            )

        self.assertEqual(len(web_socket_client.subscriptions), 1)
        self.assertEqual(
            web_socket_client.subscriptions[0].close_call_count,
            1,
        )

    def test_raw_object_and_combined_json_are_normalized(self) -> None:
        """
        함수 이름: test_raw_object_and_combined_json_are_normalized()
        기능: raw object와 combined JSON 문자열이 같은 내부 Kline 계약으로 변환되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        web_socket_client = FakeWebSocketClient()
        gateway = WebSocketGateway(web_socket_client)
        subscription = gateway.start_all_kline_buffering(
            "ETHUSDT",
            SUPPORTED_INTERVALS,
        )
        web_socket_client.emit(
            0,
            _official_websocket_event(interval="1m", closed=True),
        )
        combined_payload = {
            "stream": "ethusdt@kline_30m",
            "data": _official_websocket_event(
                interval="30m",
                close_price="101.75",
            ),
        }
        web_socket_client.emit(0, json.dumps(combined_payload))

        drained = gateway.drain_kline_buffer(subscription)

        raw_kline = drained[Interval.ONE_MINUTE][0]
        combined_kline = drained[Interval.THIRTY_MINUTES][0]
        self.assertEqual(raw_kline.symbol, "ETHUSDT")
        self.assertIs(raw_kline.interval, Interval.ONE_MINUTE)
        self.assertEqual(raw_kline.close, Decimal("101.25"))
        self.assertTrue(raw_kline.closed)
        self.assertIs(raw_kline.open_time.tzinfo, timezone.utc)
        self.assertEqual(combined_kline.close, Decimal("101.75"))
        self.assertFalse(combined_kline.closed)
        self.assertEqual(drained[Interval.FOUR_HOURS], ())
        self.assertEqual(drained[Interval.ONE_DAY], ())

    def test_latest_duplicate_wins_results_are_sorted_and_drain_clears(self) -> None:
        """
        함수 이름: test_latest_duplicate_wins_results_are_sorted_and_drain_clears()
        기능: 같은 open time의 마지막 WS 값 우선, 시간 정렬과 원자적 clear를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        web_socket_client = FakeWebSocketClient()
        gateway = WebSocketGateway(web_socket_client)
        subscription = gateway.start_all_kline_buffering(
            "ETHUSDT",
            (Interval.ONE_MINUTE,),
        )
        later_open_time = DEFAULT_OPEN_TIME_MILLISECONDS + 60_000
        web_socket_client.emit(
            0,
            _official_websocket_event(
                open_time_milliseconds=later_open_time,
                close_time_milliseconds=later_open_time + 59_999,
                close_price="101.50",
            ),
        )
        web_socket_client.emit(
            0,
            _official_websocket_event(close_price="101.00"),
        )
        web_socket_client.emit(
            0,
            _official_websocket_event(
                open_time_milliseconds=later_open_time,
                close_time_milliseconds=later_open_time + 59_999,
                close_price="102.00",
            ),
        )

        first_drain = gateway.drain_kline_buffer(subscription)

        self.assertEqual(
            tuple(
                kline.open_time
                for kline in first_drain[Interval.ONE_MINUTE]
            ),
            (
                UNIX_EPOCH
                + timedelta(milliseconds=DEFAULT_OPEN_TIME_MILLISECONDS),
                UNIX_EPOCH + timedelta(milliseconds=later_open_time),
            ),
        )
        self.assertEqual(
            first_drain[Interval.ONE_MINUTE][-1].close,
            Decimal("102.00"),
        )
        with self.assertRaises(KlineBufferStateError):
            gateway.drain_kline_buffer(subscription)

    def test_disconnect_blocks_drain_and_ignores_later_messages(self) -> None:
        """
        함수 이름: test_disconnect_blocks_drain_and_ignores_later_messages()
        기능: disconnect 이후 buffer drain과 뒤늦은 message 적용이 차단되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        web_socket_client = FakeWebSocketClient()
        gateway = WebSocketGateway(web_socket_client)
        subscription = gateway.start_all_kline_buffering(
            "ETHUSDT",
            (Interval.ONE_MINUTE,),
        )
        web_socket_client.emit(0, _official_websocket_event())
        web_socket_client.disconnect(0)
        web_socket_client.emit(0, "malformed stale payload")

        with self.assertRaises(KlineBufferStateError):
            gateway.drain_kline_buffer(subscription)

    def test_new_generation_rejects_old_handle_and_ignores_stale_callbacks(self) -> None:
        """
        함수 이름: test_new_generation_rejects_old_handle_and_ignores_stale_callbacks()
        기능: 새 start가 이전 handle·message·disconnect callback 세대를 무효화하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        web_socket_client = FakeWebSocketClient()
        gateway = WebSocketGateway(web_socket_client)
        stale_subscription = gateway.start_all_kline_buffering(
            "ETHUSDT",
            (Interval.ONE_MINUTE,),
        )
        active_subscription = gateway.start_all_kline_buffering(
            "ETHUSDT",
            (Interval.ONE_MINUTE,),
        )

        self.assertTrue(web_socket_client.subscriptions[0].closed)
        web_socket_client.emit(0, "malformed stale payload")
        web_socket_client.disconnect(0)
        web_socket_client.emit(
            1,
            _official_websocket_event(close_price="102.00"),
        )

        with self.assertRaises(KlineBufferStateError):
            gateway.drain_kline_buffer(stale_subscription)

        drained = gateway.drain_kline_buffer(active_subscription)
        self.assertEqual(
            drained[Interval.ONE_MINUTE][0].close,
            Decimal("102.00"),
        )

    def test_managed_close_allows_retry_with_strict_transport_handle(self) -> None:
        """
        함수 이름: test_managed_close_allows_retry_with_strict_transport_handle()
        기능: callback을 보내지 않고 재close를 거부하는 handle 종료 후 재시도를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        web_socket_client = FakeWebSocketClient()
        gateway = WebSocketGateway(web_socket_client)
        first_subscription = gateway.start_all_kline_buffering(
            "ETHUSDT",
            (Interval.ONE_MINUTE,),
        )

        first_subscription.close()
        second_subscription = gateway.start_all_kline_buffering(
            "ETHUSDT",
            (Interval.ONE_MINUTE,),
        )
        second_subscription.close()

        self.assertEqual(
            tuple(
                subscription.close_call_count
                for subscription in web_socket_client.subscriptions
            ),
            (1, 1),
        )

    def test_messages_for_wrong_symbol_or_unsubscribed_interval_are_rejected(self) -> None:
        """
        함수 이름: test_messages_for_wrong_symbol_or_unsubscribed_interval_are_rejected()
        기능: active 구독의 symbol 또는 interval과 다른 정상형 Kline을 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        web_socket_client = FakeWebSocketClient()
        gateway = WebSocketGateway(web_socket_client)
        gateway.start_all_kline_buffering(
            "ETHUSDT",
            (Interval.ONE_MINUTE,),
        )

        wrong_messages = (
            _official_websocket_event(symbol="BTCUSDT"),
            _official_websocket_event(interval="30m"),
        )
        for wrong_message in wrong_messages:
            with self.subTest(message=wrong_message):
                with self.assertRaises(ValueError):
                    web_socket_client.emit(0, wrong_message)

    def test_malformed_raw_and_combined_payloads_are_rejected(self) -> None:
        """
        함수 이름: test_malformed_raw_and_combined_payloads_are_rejected()
        기능: JSON 구조, event, symbol, stream, interval, timestamp, decimal, x 오류를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        web_socket_client = FakeWebSocketClient()
        gateway = WebSocketGateway(web_socket_client)
        gateway.start_all_kline_buffering(
            "ETHUSDT",
            (Interval.ONE_MINUTE,),
        )
        mismatched_symbol_event = _official_websocket_event()
        mismatched_symbol_kline = dict(mismatched_symbol_event["k"])
        mismatched_symbol_kline["s"] = "BTCUSDT"
        mismatched_symbol_event["k"] = mismatched_symbol_kline

        malformed_payloads: tuple[object, ...] = (
            "not-json",
            [],
            _official_websocket_event(event_overrides={"e": "trade"}),
            _official_websocket_event(event_overrides={"k": None}),
            mismatched_symbol_event,
            {"stream": "ethusdt@kline_1m"},
            {
                "stream": "btcusdt@kline_1m",
                "data": _official_websocket_event(),
            },
            _official_websocket_event(kline_overrides={"i": "2m"}),
            _official_websocket_event(kline_overrides={"t": True}),
            _official_websocket_event(
                open_time_milliseconds=2_000,
                close_time_milliseconds=1_999,
            ),
            _official_websocket_event(
                open_time_milliseconds=(
                    DEFAULT_OPEN_TIME_MILLISECONDS + 1_000
                ),
            ),
            _official_websocket_event(
                close_time_milliseconds=(
                    DEFAULT_OPEN_TIME_MILLISECONDS
                    + INTERVAL_MILLISECONDS["1d"]
                    - 1
                ),
            ),
            _official_websocket_event(kline_overrides={"o": 100}),
            _official_websocket_event(kline_overrides={"c": "NaN"}),
            _official_websocket_event(kline_overrides={"x": "false"}),
        )

        for malformed_payload in malformed_payloads:
            with self.subTest(payload=malformed_payload):
                with self.assertRaises((TypeError, ValueError)):
                    web_socket_client.emit(0, malformed_payload)

    def test_callback_error_is_latched_when_transport_swallows_exception(
        self,
    ) -> None:
        """
        함수 이름: test_callback_error_is_latched_when_transport_swallows_exception()
        기능: transport가 삼킨 callback 검증 오류가 뒤의 buffer commit을 차단하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        invalid_payloads = (
            {"e": "kline", "s": "ETHUSDT"},
            _official_websocket_event(symbol="BTCUSDT"),
            _official_websocket_event(interval="30m"),
        )

        for invalid_payload in invalid_payloads:
            with self.subTest(invalid_payload=invalid_payload):
                web_socket_client = FakeWebSocketClient()
                gateway = WebSocketGateway(web_socket_client)
                subscription = gateway.start_all_kline_buffering(
                    "ETHUSDT",
                    (Interval.ONE_MINUTE,),
                )

                with self.assertRaises((TypeError, ValueError)):
                    web_socket_client.emit(0, invalid_payload)

                with self.assertRaises(KlineBufferStateError):
                    gateway.drain_kline_buffer(subscription)

    def test_drain_waits_for_callback_that_is_already_parsing(self) -> None:
        """
        함수 이름: test_drain_waits_for_callback_that_is_already_parsing()
        기능: drain과 경쟁한 수신 callback의 Kline이 초기 buffer에 포함되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        web_socket_client = FakeWebSocketClient()
        gateway = WebSocketGateway(web_socket_client)
        subscription = gateway.start_all_kline_buffering(
            "ETHUSDT",
            (Interval.ONE_MINUTE,),
        )
        parse_started = Event()
        allow_parse_to_finish = Event()
        drain_finished = Event()
        callback_errors: list[Exception] = []
        drain_results: list[dict[Interval, tuple[object, ...]]] = []
        original_parser = websocket_gateway._parse_websocket_kline

        def parse_after_release(payload: object) -> object:
            """
            함수 이름: parse_after_release()
            기능: callback이 parse 임계 구역에 진입한 시점을 테스트에 노출한다.
            인자: payload -> 정규화할 WebSocket payload
            반환값: production parser가 생성한 Kline
            작성 날짜: 2026/08/20
            """
            parse_started.set()
            if not allow_parse_to_finish.wait(timeout=1):
                raise RuntimeError("test did not release parser")

            return original_parser(payload)

        def emit_message() -> None:
            """
            함수 이름: emit_message()
            기능: 별도 thread에서 수신 callback을 실행한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/08/20
            """
            try:
                web_socket_client.emit(0, _official_websocket_event())
            except Exception as error:
                callback_errors.append(error)

        def drain_buffer() -> None:
            """
            함수 이름: drain_buffer()
            기능: callback과 경쟁하는 buffer drain 결과를 보존한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/08/20
            """
            try:
                drain_results.append(
                    gateway.drain_kline_buffer(subscription)
                )
            finally:
                drain_finished.set()

        with patch.object(
            websocket_gateway,
            "_parse_websocket_kline",
            side_effect=parse_after_release,
        ):
            callback_thread = Thread(target=emit_message)
            callback_thread.start()
            self.assertTrue(parse_started.wait(timeout=1))

            drain_thread = Thread(target=drain_buffer)
            drain_thread.start()
            self.assertFalse(drain_finished.wait(timeout=0.1))
            allow_parse_to_finish.set()
            callback_thread.join(timeout=1)
            drain_thread.join(timeout=1)

        self.assertFalse(callback_thread.is_alive())
        self.assertFalse(drain_thread.is_alive())
        self.assertEqual(callback_errors, [])
        self.assertTrue(drain_finished.is_set())
        self.assertEqual(
            len(drain_results[0][Interval.ONE_MINUTE]),
            1,
        )

    def test_websocket_validates_event_time_and_official_metadata_fields(self) -> None:
        """
        함수 이름: test_websocket_validates_event_time_and_official_metadata_fields()
        기능: 공식 E·trade ID·count·quote volume 필드의 타입과 decimal 형식을 엄격히 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        web_socket_client = FakeWebSocketClient()
        gateway = WebSocketGateway(web_socket_client)
        gateway.start_all_kline_buffering(
            "ETHUSDT",
            (Interval.ONE_MINUTE,),
        )
        malformed_payloads = (
            _official_websocket_event(event_overrides={"E": "invalid"}),
            _official_websocket_event(kline_overrides={"f": True}),
            _official_websocket_event(kline_overrides={"L": -1}),
            _official_websocket_event(kline_overrides={"n": "101"}),
            _official_websocket_event(kline_overrides={"q": 1265}),
            _official_websocket_event(kline_overrides={"V": "NaN"}),
            _official_websocket_event(kline_overrides={"Q": "-1"}),
            _official_websocket_event(kline_overrides={"B": 0}),
        )

        for malformed_payload in malformed_payloads:
            with self.subTest(payload=malformed_payload):
                with self.assertRaises((TypeError, ValueError)):
                    web_socket_client.emit(0, malformed_payload)

    def test_combined_stream_name_is_strictly_lowercase(self) -> None:
        """
        함수 이름: test_combined_stream_name_is_strictly_lowercase()
        기능: Binance 공식 규칙과 다른 대문자 combined stream 이름을 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        web_socket_client = FakeWebSocketClient()
        gateway = WebSocketGateway(web_socket_client)
        gateway.start_all_kline_buffering(
            "ETHUSDT",
            (Interval.ONE_MINUTE,),
        )
        combined_payload = {
            "stream": "ETHUSDT@KLINE_1M",
            "data": _official_websocket_event(),
        }

        with self.assertRaises(ValueError):
            web_socket_client.emit(0, combined_payload)

    def test_start_rejects_invalid_interval_collections(self) -> None:
        """
        함수 이름: test_start_rejects_invalid_interval_collections()
        기능: set/tuple·비어 있지 않음·canonical 값·중복 없음 구독 interval 계약을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        invalid_intervals: tuple[object, ...] = (
            [],
            (),
            ("1m",),
            (Interval.ONE_MINUTE, Interval.ONE_MINUTE),
        )

        for intervals in invalid_intervals:
            with self.subTest(intervals=intervals):
                with self.assertRaises((TypeError, ValueError)):
                    WebSocketGateway(
                        FakeWebSocketClient()
                    ).start_all_kline_buffering(
                        "ETHUSDT",
                        intervals,  # type: ignore[arg-type]
                    )


if __name__ == "__main__":
    unittest.main()
