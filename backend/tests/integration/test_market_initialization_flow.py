"""Communication 메시지 1.1~1.3의 시장 데이터 초기화 흐름을 종단 간 검증한다."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from decimal import Decimal
from threading import Event, Thread
import unittest

from binance_auto_trader.adapters.binance.api_gateway import APIGateway
from binance_auto_trader.adapters.binance.websocket_gateway import (
    WebSocketGateway,
)
from binance_auto_trader.application.market_data_controller import (
    MARKET_INTERVALS,
    MarketDataController,
)
from binance_auto_trader.domain.common import Interval, SUPPORTED_INTERVALS
from binance_auto_trader.domain.market import Kline, MarketSnapshot


SYMBOL = "ETHUSDT"
OPEN_TIME_MILLISECONDS = 1_787_184_000_000
FIXED_GATEWAY_TIME = datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc)
FIXED_SNAPSHOT_TIME = datetime(2026, 8, 20, 2, 0, tzinfo=timezone.utc)
INTERVAL_MILLISECONDS = {
    "1m": 60_000,
    "30m": 1_800_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}
DEFAULT_CLOSE_BY_INTERVAL = {
    "1m": "101",
    "30m": "102",
    "4h": "103",
    "1d": "104",
}


def _create_rest_kline_payload(
    interval: str,
    close: str,
    open_time_milliseconds: int = OPEN_TIME_MILLISECONDS,
) -> list[object]:
    """
    함수 이름: _create_rest_kline_payload()
    기능: Binance Spot REST Kline 배열 형식의 정상 fixture 한 행을 만든다.
    인자: interval -> Binance interval 문자열
        close -> OHLC에 사용할 종가 문자열
        open_time_milliseconds -> 봉 시작 Unix millisecond
    반환값: 공식 Kline 배열 schema를 따르는 payload
    작성 날짜: 2026/08/20
    """
    close_decimal = Decimal(close)
    high_decimal = close_decimal + Decimal("1")
    low_decimal = close_decimal - Decimal("1")
    close_time_milliseconds = (
        open_time_milliseconds + INTERVAL_MILLISECONDS[interval] - 1
    )

    return [
        open_time_milliseconds,
        close,
        format(high_decimal, "f"),
        format(low_decimal, "f"),
        close,
        "5",
        close_time_milliseconds,
        "500",
        10,
        "2",
        "200",
        "0",
    ]


def _create_rest_responses(
    close_by_interval: Mapping[str, str] = DEFAULT_CLOSE_BY_INTERVAL,
) -> dict[str, list[list[object]]]:
    """
    함수 이름: _create_rest_responses()
    기능: 네 canonical interval의 Binance REST fixture mapping을 만든다.
    인자: close_by_interval -> 주기별 종가 문자열
    반환값: interval 문자열별 REST Kline 행 목록
    작성 날짜: 2026/08/20
    """
    return {
        interval.value: [
            _create_rest_kline_payload(
                interval.value,
                close_by_interval[interval.value],
            )
        ]
        for interval in SUPPORTED_INTERVALS
    }


def _create_web_socket_kline_payload(
    interval: Interval,
    close: str,
    open_time_milliseconds: int = OPEN_TIME_MILLISECONDS,
    closed: bool = False,
    event_time_milliseconds: int | None = None,
) -> dict[str, object]:
    """
    함수 이름: _create_web_socket_kline_payload()
    기능: Binance Spot WebSocket Kline event 형식의 정상 fixture를 만든다.
    인자: interval -> event가 나타내는 canonical Kline 주기
        close -> OHLC에 사용할 종가 문자열
        open_time_milliseconds -> 봉 시작 Unix millisecond
        closed -> 공식 x 확정봉 flag
        event_time_milliseconds -> 선택적인 공식 E event millisecond
    반환값: 공식 Kline event schema를 따르는 payload
    작성 날짜: 2026/08/20
    """
    close_decimal = Decimal(close)
    high_decimal = close_decimal + Decimal("1")
    low_decimal = close_decimal - Decimal("1")
    close_time_milliseconds = (
        open_time_milliseconds
        + INTERVAL_MILLISECONDS[interval.value]
        - 1
    )

    selected_event_time = (
        open_time_milliseconds
        if event_time_milliseconds is None
        else event_time_milliseconds
    )

    return {
        "e": "kline",
        "E": selected_event_time,
        "s": SYMBOL,
        "k": {
            "t": open_time_milliseconds,
            "T": close_time_milliseconds,
            "s": SYMBOL,
            "i": interval.value,
            "f": 1,
            "L": 1,
            "o": close,
            "c": close,
            "h": format(high_decimal, "f"),
            "l": format(low_decimal, "f"),
            "v": "5",
            "n": 1,
            "x": closed,
            "q": "500",
            "V": "2",
            "Q": "200",
            "B": "0",
        },
    }


class ControlledClock:
    """
    클래스 이름: ControlledClock
    기능: 테스트가 반환 시각과 실패 시점을 제어할 수 있는 UTC clock을 제공한다.
    작성 날짜: 2026/08/20
    """

    def __init__(self, current_time: datetime) -> None:
        """
        함수 이름: __init__()
        기능: 정상 반환할 UTC 시각으로 clock을 초기화한다.
        인자: current_time -> 정상 호출에서 반환할 시각
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self.current_time = current_time
        self.should_fail = False

    def __call__(self) -> datetime:
        """
        함수 이름: __call__()
        기능: 설정에 따라 고정 UTC 시각을 반환하거나 의도한 오류를 발생시킨다.
        인자: 없음
        반환값: 설정된 UTC datetime
        작성 날짜: 2026/08/20
        """
        if self.should_fail:
            raise RuntimeError("controlled clock failure")

        return self.current_time


class FakeRestClient:
    """
    클래스 이름: FakeRestClient
    기능: 주기별 Binance REST payload와 장애를 결정론적으로 제공한다.
    작성 날짜: 2026/08/20
    """

    def __init__(
        self,
        responses: Mapping[str, object],
        external_trace: list[str],
    ) -> None:
        """
        함수 이름: __init__()
        기능: 응답 fixture와 외부 호출 trace를 보존한다.
        인자: responses -> Binance interval 문자열별 원본 응답
            external_trace -> REST와 WebSocket 외부 호출 순서를 기록할 목록
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self.responses = dict(responses)
        self.external_trace = external_trace
        self.fail_interval: str | None = None
        self.on_request: Callable[[str], None] | None = None
        self.requests: list[tuple[str, str, int]] = []
        self.order_call_count = 0

    def get_klines(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int,
    ) -> object:
        """
        함수 이름: get_klines()
        기능: 요청을 기록하고 설정된 주기의 Binance REST 응답을 반환한다.
        인자: symbol -> 요청된 Binance 거래 symbol
            interval -> 요청된 Binance Kline 주기
            limit -> 요청된 봉 개수 제한
        반환값: 설정된 원본 Binance 응답
        작성 날짜: 2026/08/20
        """
        self.external_trace.append(f"rest:{interval}")
        self.requests.append((symbol, interval, limit))

        if self.on_request is not None:
            self.on_request(interval)

        if interval == self.fail_interval:
            raise RuntimeError(f"REST failure for {interval}")

        return self.responses[interval]

    def place_order(self, *arguments: object, **keywords: object) -> object:
        """
        함수 이름: place_order()
        기능: Phase 2에서 금지된 주문 호출이 발생하면 즉시 테스트를 실패시킨다.
        인자: arguments -> 전달된 위치 인자
            keywords -> 전달된 키워드 인자
        반환값: 정상 경로에서는 반환하지 않음
        작성 날짜: 2026/08/20
        """
        self.order_call_count += 1
        raise AssertionError("order operations are forbidden in Phase 2")


class FakeSubscription:
    """
    클래스 이름: FakeSubscription
    기능: 초기화 실패와 재동기화에서 구독 정리를 검증하는 handle이다.
    작성 날짜: 2026/08/20
    """

    def __init__(self, on_disconnect: Callable[[], None]) -> None:
        """
        함수 이름: __init__()
        기능: disconnect callback과 초기 close 상태를 보존한다.
        인자: on_disconnect -> 구독 종료 시 호출할 callback
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self._on_disconnect = on_disconnect
        self.closed = False

    def close(self) -> None:
        """
        함수 이름: close()
        기능: fake 구독을 한 번만 종료하고 disconnect callback을 호출한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        if self.closed:
            return

        self.closed = True
        self._on_disconnect()


class FakeWebSocketClient:
    """
    클래스 이름: FakeWebSocketClient
    기능: 구독별 Kline과 disconnect callback을 보존하고 테스트에서 직접 실행한다.
    작성 날짜: 2026/08/20
    """

    def __init__(self, external_trace: list[str]) -> None:
        """
        함수 이름: __init__()
        기능: 비어 있는 callback registry와 외부 호출 trace를 초기화한다.
        인자: external_trace -> REST와 WebSocket 외부 호출 순서를 기록할 목록
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self.external_trace = external_trace
        self.subscription_count = 0
        self.latest_handle: FakeSubscription | None = None
        self.subscription_requests: list[tuple[str, tuple[str, ...]]] = []
        self.message_callbacks: dict[object, Callable[[object], None]] = {}
        self.disconnect_callbacks: dict[
            object,
            Callable[..., None],
        ] = {}
        self.account_stream_call_count = 0

    def subscribe_all_kline_streams(
        self,
        *,
        symbol: str,
        intervals: tuple[str, ...],
        on_message: Callable[[object], None],
        on_disconnect: Callable[..., None],
    ) -> FakeSubscription:
        """
        함수 이름: subscribe_all_kline_streams()
        기능: 네 Kline stream callback을 새 구독 handle에 연결한다.
        인자: symbol -> 구독할 Binance 거래 symbol
            intervals -> 구독할 Binance interval 문자열 tuple
            on_message -> 원본 Kline event 수신 callback
            on_disconnect -> 연결 종료 callback
        반환값: Gateway가 그대로 관리할 구독 handle
        작성 날짜: 2026/08/20
        """
        self.external_trace.append("subscribe_all_kline_streams")
        self.subscription_count += 1
        subscription_handle = FakeSubscription(on_disconnect)
        self.latest_handle = subscription_handle
        self.subscription_requests.append((symbol, intervals))
        self.message_callbacks[subscription_handle] = on_message
        self.disconnect_callbacks[subscription_handle] = on_disconnect
        return subscription_handle

    def emit(self, subscription_handle: object, payload: object) -> None:
        """
        함수 이름: emit()
        기능: 지정된 구독이 보존한 원본 Kline callback을 실행한다.
        인자: subscription_handle -> event를 전달할 과거 또는 현재 구독 handle
            payload -> Gateway가 정규화할 원본 Binance event
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self.message_callbacks[subscription_handle](payload)

    def disconnect(self, subscription_handle: object) -> None:
        """
        함수 이름: disconnect()
        기능: 지정된 구독의 disconnect callback을 실행한다.
        인자: subscription_handle -> 연결 종료로 표시할 구독 handle
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self.disconnect_callbacks[subscription_handle]()

    def subscribe_account_info(
        self,
        *arguments: object,
        **keywords: object,
    ) -> object:
        """
        함수 이름: subscribe_account_info()
        기능: Phase 2에서 금지된 account stream 호출이 발생하면 테스트를 실패시킨다.
        인자: arguments -> 전달된 위치 인자
            keywords -> 전달된 키워드 인자
        반환값: 정상 경로에서는 반환하지 않음
        작성 날짜: 2026/08/20
        """
        self.account_stream_call_count += 1
        raise AssertionError("account stream is outside Phase 2")


class TracingAPIGateway(APIGateway):
    """
    클래스 이름: TracingAPIGateway
    기능: production APIGateway 동작을 유지하며 Controller 호출 순서만 기록한다.
    작성 날짜: 2026/08/20
    """

    def __init__(
        self,
        rest_client: object,
        operation_trace: list[str],
        clock: Callable[[], datetime],
    ) -> None:
        """
        함수 이름: __init__()
        기능: production Gateway와 operation trace를 초기화한다.
        인자: rest_client -> Binance REST fake client
            operation_trace -> Controller operation 순서를 기록할 목록
            clock -> Kline closed 판정용 UTC clock
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        super().__init__(rest_client, clock=clock)
        self._operation_trace = operation_trace

    def load_all_klines(
        self,
        symbol: str,
        limit: int = 500,
    ) -> dict[Interval, tuple[Kline, ...]]:
        """
        함수 이름: load_all_klines()
        기능: 메시지 1.2 호출을 기록한 뒤 production 정규화를 실행한다.
        인자: symbol -> 조회할 Binance 거래 symbol
            limit -> 주기별 조회할 Kline 개수
        반환값: canonical interval별 정규화 Kline tuple
        작성 날짜: 2026/08/20
        """
        self._operation_trace.append("load_all_klines")
        return super().load_all_klines(symbol=symbol, limit=limit)


class TracingWebSocketGateway(WebSocketGateway):
    """
    클래스 이름: TracingWebSocketGateway
    기능: production WebSocketGateway 동작을 유지하며 Controller 호출 순서만 기록한다.
    작성 날짜: 2026/08/20
    """

    def __init__(
        self,
        web_socket_client: object,
        operation_trace: list[str],
    ) -> None:
        """
        함수 이름: __init__()
        기능: production Gateway와 operation trace를 초기화한다.
        인자: web_socket_client -> Binance WebSocket fake client
            operation_trace -> Controller operation 순서를 기록할 목록
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        super().__init__(web_socket_client)
        self._operation_trace = operation_trace

    def start_all_kline_buffering(
        self,
        symbol: str,
        intervals: Iterable[Interval] = SUPPORTED_INTERVALS,
        *,
        reconciliation_required_callback: Callable[[str], object]
        | None = None,
    ) -> object:
        """
        함수 이름: start_all_kline_buffering()
        기능: 메시지 1.1 호출을 기록한 뒤 production 구독을 시작한다.
        인자: symbol -> 구독할 Binance 거래 symbol
            intervals -> 구독할 canonical interval 모음
            reconciliation_required_callback -> 시장 장애를 받을 optional callback
        반환값: fake client가 반환한 구독 handle
        작성 날짜: 2026/08/20
        """
        self._operation_trace.append("start_all_kline_buffering")
        return super().start_all_kline_buffering(
            symbol=symbol,
            intervals=intervals,
            reconciliation_required_callback=(
                reconciliation_required_callback
            ),
        )

    def promote_kline_buffer_to_live(
        self,
        observer: Callable[[Kline], object],
        subscription: object | None = None,
    ) -> object:
        """
        함수 이름: promote_kline_buffer_to_live()
        기능: 메시지 1L 승격 호출을 기록한 뒤 production 동일 구독을 반환한다.
        인자: observer -> 연속 정규화 Kline callback
            subscription -> 검증할 현재 초기화 구독 handle
        반환값: 재구독하지 않은 동일한 구독 handle
        작성 날짜: 2026/08/25
        """
        self._operation_trace.append("promote_kline_buffer_to_live")
        return super().promote_kline_buffer_to_live(
            observer,
            subscription,
        )


class TracingMarketSnapshot(MarketSnapshot):
    """
    클래스 이름: TracingMarketSnapshot
    기능: production MarketSnapshot의 원자성을 유지하며 update 호출 순서만 기록한다.
    작성 날짜: 2026/08/20
    """

    def __init__(
        self,
        operation_trace: list[str],
        clock: Callable[[], datetime],
    ) -> None:
        """
        함수 이름: __init__()
        기능: production snapshot과 operation trace를 초기화한다.
        인자: operation_trace -> Controller operation 순서를 기록할 목록
            clock -> snapshot 갱신 UTC 시각을 제공할 clock
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        super().__init__(symbol=SYMBOL, clock=clock)
        self._operation_trace = operation_trace

    def update(
        self,
        klines_by_interval: Mapping[Interval, Iterable[Kline]],
        *,
        source_klines: Iterable[Kline] | None = None,
    ) -> None:
        """
        함수 이름: update()
        기능: 메시지 1.3 호출을 기록한 뒤 production 원자 update를 실행한다.
        인자: klines_by_interval -> REST 봉 뒤에 WebSocket 봉을 배치한 전체 mapping
            source_klines -> 현재 version을 발생시킨 optional WebSocket Kline
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self._operation_trace.append("market_snapshot.update")
        super().update(
            klines_by_interval,
            source_klines=source_klines,
        )


class BlockingMarketSnapshot(TracingMarketSnapshot):
    """
    클래스 이름: BlockingMarketSnapshot
    기능: 동시 초기화 테스트에서 첫 snapshot commit 시점을 제어한다.
    작성 날짜: 2026/08/20
    """

    def __init__(
        self,
        operation_trace: list[str],
        clock: Callable[[], datetime],
    ) -> None:
        """
        함수 이름: __init__()
        기능: production snapshot과 첫 update용 동기화 event를 초기화한다.
        인자: operation_trace -> Controller operation 순서를 기록할 목록
            clock -> snapshot 갱신 UTC 시각을 제공할 clock
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        super().__init__(operation_trace, clock)
        self.first_update_started = Event()
        self.allow_first_update = Event()
        self._update_call_count = 0

    def update(
        self,
        klines_by_interval: Mapping[Interval, Iterable[Kline]],
        *,
        source_klines: Iterable[Kline] | None = None,
    ) -> None:
        """
        함수 이름: update()
        기능: 첫 commit만 테스트 event가 허용할 때까지 대기한 뒤 위임한다.
        인자: klines_by_interval -> 주기별 초기화 Kline mapping
            source_klines -> 현재 version을 발생시킨 optional WebSocket Kline
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self._update_call_count += 1
        if self._update_call_count == 1:
            self.first_update_started.set()
            if not self.allow_first_update.wait(timeout=2):
                raise RuntimeError("test did not release first update")

        super().update(
            klines_by_interval,
            source_klines=source_klines,
        )


class NoOpRegimeController:
    """
    클래스 이름: NoOpRegimeController
    기능: Phase 2 시장 초기화 회귀에서 Phase 3 추천 동작을 격리하는 test double이다.
    작성 날짜: 2026/08/20
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: Phase 2 격리를 유지하면서 same-version 평가 계약을 추적한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # 실제 REGIME 계산은 격리하되 Controller gate가 요구하는 source version은 보존한다.
        self._last_regime_result: _NoOpRegimeResult | None = None

    @property
    def last_regime_result(self) -> "_NoOpRegimeResult | None":
        """
        함수 이름: last_regime_result()
        기능: Phase 2 격리 double에는 적용된 추천 결과가 없음을 반환한다.
        인자: 없음
        반환값: 마지막 same-version 평가 표식 또는 초기 None
        작성 날짜: 2026/08/29
        """
        return self._last_regime_result

    def evaluate_regime(
        self,
        trigger: object,
        market_snapshot: MarketSnapshot,
    ) -> "_NoOpRegimeResult":
        """
        함수 이름: evaluate_regime()
        기능: 시장 초기화 회귀 trace를 바꾸지 않고 추천 평가 요청을 소비한다.
        인자: trigger -> production Controller에 전달되는 평가 trigger
            market_snapshot -> production Controller에 전달되는 시장 snapshot
        반환값: source market version을 담은 격리용 결과
        작성 날짜: 2026/08/29
        """
        del trigger
        self._last_regime_result = _NoOpRegimeResult(
            market_snapshot.version
        )
        return self._last_regime_result

    def reconcile_regime(
        self,
        market_snapshot: MarketSnapshot,
    ) -> "_NoOpRegimeResult":
        """
        함수 이름: reconcile_regime()
        기능: 재초기화 snapshot을 새 version에 결합하는 production 계약만 대역한다.
        인자: market_snapshot -> REST·WebSocket 병합을 마친 snapshot
        반환값: source market version을 담은 격리용 결과
        작성 날짜: 2026/08/29
        """
        self._last_regime_result = _NoOpRegimeResult(
            market_snapshot.version
        )
        return self._last_regime_result


class _NoOpRegimeResult:
    """
    클래스 이름: _NoOpRegimeResult
    기능: Phase 2 test double에서 same-version gate만 충족하는 결과다.
    작성 날짜: 2026/08/29
    """

    def __init__(self, source_market_version: int) -> None:
        """
        함수 이름: __init__()
        기능: 평가가 읽은 MarketSnapshot version을 보존한다.
        인자: source_market_version -> 평가 source version
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        self.source_market_version = source_market_version


class MarketInitializationFlowTests(unittest.TestCase):
    """
    클래스 이름: MarketInitializationFlowTests
    기능: fake Binance client로 시장 초기화 순서, 병합, 장애와 재연결을 검증한다.
    작성 날짜: 2026/08/20
    """

    def setUp(self) -> None:
        """
        함수 이름: setUp()
        기능: 각 test에 독립적인 fake client, production Gateway와 snapshot을 조립한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self.operation_trace: list[str] = []
        self.external_trace: list[str] = []
        self.gateway_clock = ControlledClock(FIXED_GATEWAY_TIME)
        self.snapshot_clock = ControlledClock(FIXED_SNAPSHOT_TIME)
        self.rest_client = FakeRestClient(
            _create_rest_responses(),
            self.external_trace,
        )
        self.web_socket_client = FakeWebSocketClient(self.external_trace)
        self.api_gateway = TracingAPIGateway(
            rest_client=self.rest_client,
            operation_trace=self.operation_trace,
            clock=self.gateway_clock,
        )
        self.web_socket_gateway = TracingWebSocketGateway(
            web_socket_client=self.web_socket_client,
            operation_trace=self.operation_trace,
        )
        self.market_snapshot = TracingMarketSnapshot(
            operation_trace=self.operation_trace,
            clock=self.snapshot_clock,
        )
        self.regime_controller = NoOpRegimeController()
        self.controller = MarketDataController(
            api_gateway=self.api_gateway,
            web_socket_gateway=self.web_socket_gateway,
            market_snapshot=self.market_snapshot,
            regime_controller=self.regime_controller,
        )

    def _capture_snapshot_state(self) -> tuple[object, ...]:
        """
        함수 이름: _capture_snapshot_state()
        기능: 실패 전후 원자성을 비교할 수 있도록 모든 공개 snapshot 상태를 묶는다.
        인자: 없음
        반환값: Kline mapping identity와 가격, version, 시각, ready 상태
        작성 날짜: 2026/08/20
        """
        return (
            self.market_snapshot.klines_by_interval,
            self.market_snapshot.current_eth_price,
            self.market_snapshot.version,
            self.market_snapshot.updated_at,
            self.market_snapshot.ready,
        )

    def _assert_snapshot_state_unchanged(
        self,
        expected_state: tuple[object, ...],
    ) -> None:
        """
        함수 이름: _assert_snapshot_state_unchanged()
        기능: snapshot mapping identity와 나머지 공개 값이 모두 유지됐는지 검증한다.
        인자: expected_state -> 실패 전에 캡처한 공개 snapshot 상태
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        actual_state = self._capture_snapshot_state()

        self.assertIs(actual_state[0], expected_state[0])
        self.assertEqual(actual_state[1:], expected_state[1:])

    def test_initialization_follows_messages_and_loads_four_intervals(
        self,
    ) -> None:
        """
        함수 이름: test_initialization_follows_messages_and_loads_four_intervals()
        기능: 메시지 1.1, 1.2, 동일 구독 승격, 1.3 순서와 네 주기 결과를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        result = self.controller.initialize_market_data()

        self.assertIs(result, self.market_snapshot)
        self.assertEqual(
            self.operation_trace,
            [
                "start_all_kline_buffering",
                "load_all_klines",
                "promote_kline_buffer_to_live",
                "market_snapshot.update",
            ],
        )
        self.assertEqual(
            self.external_trace,
            [
                "subscribe_all_kline_streams",
                "rest:1m",
                "rest:30m",
                "rest:4h",
                "rest:1d",
            ],
        )
        self.assertEqual(
            self.web_socket_client.subscription_requests,
            [(SYMBOL, tuple(interval.value for interval in MARKET_INTERVALS))],
        )
        self.assertEqual(
            self.rest_client.requests,
            [
                (SYMBOL, interval.value, 500)
                for interval in SUPPORTED_INTERVALS
            ],
        )
        self.assertEqual(
            tuple(self.market_snapshot.klines_by_interval),
            SUPPORTED_INTERVALS,
        )
        self.assertTrue(
            all(
                len(self.market_snapshot.klines_by_interval[interval]) == 1
                for interval in SUPPORTED_INTERVALS
            )
        )
        self.assertTrue(self.market_snapshot.ready)
        self.assertEqual(self.market_snapshot.version, 1)
        self.assertEqual(
            self.market_snapshot.current_eth_price,
            Decimal("103"),
        )
        completed_subscription = self.web_socket_client.latest_handle
        self.assertIsNotNone(completed_subscription)
        self.assertFalse(completed_subscription.closed)
        self.assertEqual(self.rest_client.order_call_count, 0)
        self.assertEqual(
            self.web_socket_client.account_stream_call_count,
            0,
        )

    def test_web_socket_value_wins_when_received_during_rest(self) -> None:
        """
        함수 이름: test_web_socket_value_wins_when_received_during_rest()
        기능: REST 대기 중 같은 4시간봉을 받으면 WebSocket 값이 최종 snapshot에 남는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        def emit_during_first_rest_request(interval: str) -> None:
            """
            함수 이름: emit_during_first_rest_request()
            기능: 첫 REST 요청이 반환되기 전에 같은 open_time의 4시간봉을 전달한다.
            인자: interval -> 현재 REST 요청 주기
            반환값: 없음
            작성 날짜: 2026/08/20
            """
            if interval == Interval.ONE_MINUTE.value:
                self.web_socket_client.emit(
                    self.web_socket_client.latest_handle,
                    _create_web_socket_kline_payload(
                        Interval.FOUR_HOURS,
                        "203",
                    ),
                )

        self.rest_client.on_request = emit_during_first_rest_request

        self.controller.initialize_market_data()

        four_hour_klines = self.market_snapshot.klines_by_interval[
            Interval.FOUR_HOURS
        ]
        self.assertEqual(len(four_hour_klines), 1)
        self.assertEqual(four_hour_klines[0].close, Decimal("203"))
        self.assertEqual(
            self.market_snapshot.current_eth_price,
            Decimal("203"),
        )

    def test_malformed_rest_payload_preserves_preexisting_snapshot(
        self,
    ) -> None:
        """
        함수 이름: test_malformed_rest_payload_preserves_preexisting_snapshot()
        기능: Binance REST payload가 잘못돼도 준비된 snapshot 전체와 version을 유지하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self.controller.initialize_market_data()
        expected_state = self._capture_snapshot_state()
        self.rest_client.responses[Interval.THIRTY_MINUTES.value] = [
            ["malformed"]
        ]

        with self.assertRaises((TypeError, ValueError)):
            self.controller.initialize_market_data()

        self._assert_snapshot_state_unchanged(expected_state)

    def test_malformed_web_socket_payload_preserves_snapshot(self) -> None:
        """
        함수 이름: test_malformed_web_socket_payload_preserves_snapshot()
        기능: 초기 buffer의 잘못된 WebSocket event가 snapshot을 부분 갱신하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self.controller.initialize_market_data()
        expected_state = self._capture_snapshot_state()

        def emit_malformed_event(interval: str) -> None:
            """
            함수 이름: emit_malformed_event()
            기능: 새 초기화의 첫 REST 요청 중 필수 Kline 필드가 없는 event를 전달한다.
            인자: interval -> 현재 REST 요청 주기
            반환값: 없음
            작성 날짜: 2026/08/20
            """
            if interval == Interval.ONE_MINUTE.value:
                self.web_socket_client.emit(
                    self.web_socket_client.latest_handle,
                    {"e": "kline", "s": SYMBOL, "k": {"i": "4h"}},
                )

        self.rest_client.on_request = emit_malformed_event

        with self.assertRaises((TypeError, ValueError)):
            self.controller.initialize_market_data()

        self._assert_snapshot_state_unchanged(expected_state)

    def test_one_interval_rest_failure_does_not_publish_partial_ready(
        self,
    ) -> None:
        """
        함수 이름: test_one_interval_rest_failure_does_not_publish_partial_ready()
        기능: 한 주기 조회 실패가 빈 snapshot을 ready 또는 부분 데이터로 표시하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        expected_state = self._capture_snapshot_state()
        self.rest_client.fail_interval = Interval.FOUR_HOURS.value

        with self.assertRaises(RuntimeError):
            self.controller.initialize_market_data()

        failed_subscription = self.web_socket_client.latest_handle
        self.assertIsNotNone(failed_subscription)
        self.assertTrue(failed_subscription.closed)
        self._assert_snapshot_state_unchanged(expected_state)
        self.assertFalse(self.market_snapshot.ready)
        self.assertEqual(self.market_snapshot.version, 0)
        self.assertTrue(
            all(
                not self.market_snapshot.klines_by_interval[interval]
                for interval in SUPPORTED_INTERVALS
            )
        )
        self.assertNotIn(
            "promote_kline_buffer_to_live",
            self.operation_trace,
        )
        self.assertNotIn("market_snapshot.update", self.operation_trace)

    def test_rest_failure_preserves_ready_snapshot_and_version(self) -> None:
        """
        함수 이름: test_rest_failure_preserves_ready_snapshot_and_version()
        기능: 재초기화 REST 오류가 기존 ready snapshot의 객체 상태를 바꾸지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self.controller.initialize_market_data()
        expected_state = self._capture_snapshot_state()
        self.rest_client.fail_interval = Interval.ONE_DAY.value

        with self.assertRaises(RuntimeError):
            self.controller.initialize_market_data()

        self._assert_snapshot_state_unchanged(expected_state)

    def test_disconnect_before_drain_preserves_ready_snapshot(self) -> None:
        """
        함수 이름: test_disconnect_before_drain_preserves_ready_snapshot()
        기능: REST 조회 중 WebSocket 단절이 기존 snapshot과 version을 유지하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self.controller.initialize_market_data()
        expected_state = self._capture_snapshot_state()

        def disconnect_during_rest(interval: str) -> None:
            """
            함수 이름: disconnect_during_rest()
            기능: 새 초기화의 첫 REST 요청 중 현재 Kline 구독을 끊는다.
            인자: interval -> 현재 REST 요청 주기
            반환값: 없음
            작성 날짜: 2026/08/20
            """
            if interval == Interval.ONE_MINUTE.value:
                self.web_socket_client.disconnect(
                    self.web_socket_client.latest_handle
                )

        self.rest_client.on_request = disconnect_during_rest

        with self.assertRaises(RuntimeError):
            self.controller.initialize_market_data()

        self._assert_snapshot_state_unchanged(expected_state)
        self.assertNotIn("market_snapshot.update", self.operation_trace[4:])

    def test_drain_failure_preserves_ready_snapshot(self) -> None:
        """
        함수 이름: test_drain_failure_preserves_ready_snapshot()
        기능: 이미 배출된 구독 handle의 drain 오류가 기존 snapshot을 바꾸지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self.controller.initialize_market_data()
        expected_state = self._capture_snapshot_state()

        def invalidate_subscription_early(interval: str) -> None:
            """
            함수 이름: invalidate_subscription_early()
            기능: Controller drain 전에 새 구독을 시작해 기존 handle을 stale로 만든다.
            인자: interval -> 현재 REST 요청 주기
            반환값: 없음
            작성 날짜: 2026/08/20
            """
            if interval == Interval.ONE_MINUTE.value:
                self.web_socket_gateway.start_all_kline_buffering(
                    symbol=SYMBOL,
                    intervals=SUPPORTED_INTERVALS,
                )

        self.rest_client.on_request = invalidate_subscription_early

        with self.assertRaises(RuntimeError):
            self.controller.initialize_market_data()

        self._assert_snapshot_state_unchanged(expected_state)

    def test_snapshot_update_failure_is_atomic(self) -> None:
        """
        함수 이름: test_snapshot_update_failure_is_atomic()
        기능: 최종 update 시각 생성 실패에도 기존 snapshot state와 version을 유지하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self.controller.initialize_market_data()
        expected_state = self._capture_snapshot_state()
        self.snapshot_clock.should_fail = True

        with self.assertRaises(RuntimeError):
            self.controller.initialize_market_data()

        failed_subscription = self.web_socket_client.latest_handle
        self.assertIsNotNone(failed_subscription)
        self.assertTrue(failed_subscription.closed)
        self._assert_snapshot_state_unchanged(expected_state)

    def test_reconnect_deduplicates_candles_and_increments_version(self) -> None:
        """
        함수 이름: test_reconnect_deduplicates_candles_and_increments_version()
        기능: disconnect 실패 후 전체 재시도가 중복 없이 version을 증가시키는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        first_result = self.controller.initialize_market_data()
        first_version = self.market_snapshot.version

        def disconnect_during_resynchronization(interval: str) -> None:
            """
            함수 이름: disconnect_during_resynchronization()
            기능: 첫 재동기화 REST 중 현재 초기화 구독을 끊는다.
            인자: interval -> 현재 REST 요청 주기
            반환값: 없음
            작성 날짜: 2026/08/20
            """
            if interval == Interval.ONE_MINUTE.value:
                self.web_socket_client.disconnect(
                    self.web_socket_client.latest_handle
                )

        self.rest_client.on_request = disconnect_during_resynchronization
        with self.assertRaises(RuntimeError):
            self.controller.initialize_market_data()

        self.assertEqual(self.market_snapshot.version, first_version)
        self.assertEqual(
            self.market_snapshot.current_eth_price,
            Decimal("103"),
        )

        def emit_reconnect_duplicates(interval: str) -> None:
            """
            함수 이름: emit_reconnect_duplicates()
            기능: 재연결 REST 중 같은 key의 WebSocket 봉 두 개를 차례로 전달한다.
            인자: interval -> 현재 REST 요청 주기
            반환값: 없음
            작성 날짜: 2026/08/20
            """
            if interval == Interval.ONE_MINUTE.value:
                current_handle = self.web_socket_client.latest_handle
                self.web_socket_client.emit(
                    current_handle,
                    _create_web_socket_kline_payload(
                        Interval.FOUR_HOURS,
                        "303",
                    ),
                )
                self.web_socket_client.emit(
                    current_handle,
                    _create_web_socket_kline_payload(
                        Interval.FOUR_HOURS,
                        "403",
                        event_time_milliseconds=(
                            OPEN_TIME_MILLISECONDS + 1
                        ),
                    ),
                )

        self.rest_client.on_request = emit_reconnect_duplicates

        second_result = self.controller.initialize_market_data()

        self.assertIs(first_result, second_result)
        self.assertIs(second_result, self.market_snapshot)
        self.assertEqual(first_version, 1)
        self.assertEqual(self.market_snapshot.version, 2)
        four_hour_klines = self.market_snapshot.klines_by_interval[
            Interval.FOUR_HOURS
        ]
        self.assertEqual(len(four_hour_klines), 1)
        self.assertEqual(four_hour_klines[0].close, Decimal("403"))
        for interval in SUPPORTED_INTERVALS:
            deduplication_keys = {
                (kline.symbol, kline.interval, kline.open_time)
                for kline in self.market_snapshot.klines_by_interval[
                    interval
                ]
            }
            self.assertEqual(
                len(deduplication_keys),
                len(self.market_snapshot.klines_by_interval[interval]),
            )

    def test_stale_callback_cannot_pollute_reinitialized_snapshot(self) -> None:
        """
        함수 이름: test_stale_callback_cannot_pollute_reinitialized_snapshot()
        기능: 이전 구독 callback이 재초기화 buffer와 snapshot 값을 바꾸지 못하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self.controller.initialize_market_data()
        stale_handle = self.web_socket_client.latest_handle

        def emit_stale_and_current_events(interval: str) -> None:
            """
            함수 이름: emit_stale_and_current_events()
            기능: 새 구독 중 이전 callback과 현재 callback에 충돌하는 4시간봉을 전달한다.
            인자: interval -> 현재 REST 요청 주기
            반환값: 없음
            작성 날짜: 2026/08/20
            """
            if interval == Interval.ONE_MINUTE.value:
                self.web_socket_client.emit(
                    stale_handle,
                    _create_web_socket_kline_payload(
                        Interval.FOUR_HOURS,
                        "999",
                    ),
                )
                self.web_socket_client.emit(
                    self.web_socket_client.latest_handle,
                    _create_web_socket_kline_payload(
                        Interval.FOUR_HOURS,
                        "503",
                    ),
                )

        self.rest_client.on_request = emit_stale_and_current_events

        self.controller.initialize_market_data()

        self.assertEqual(self.market_snapshot.version, 2)
        self.assertEqual(
            self.market_snapshot.current_eth_price,
            Decimal("503"),
        )

    def test_concurrent_initializations_cannot_commit_stale_data(self) -> None:
        """
        함수 이름: test_concurrent_initializations_cannot_commit_stale_data()
        기능: 늦게 완료된 이전 시도가 더 최신 초기화 결과를 덮지 못하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        blocking_snapshot = BlockingMarketSnapshot(
            operation_trace=self.operation_trace,
            clock=self.snapshot_clock,
        )
        controller = MarketDataController(
            api_gateway=self.api_gateway,
            web_socket_gateway=self.web_socket_gateway,
            market_snapshot=blocking_snapshot,
            regime_controller=self.regime_controller,
        )
        initialization_errors: list[Exception] = []

        def initialize() -> None:
            """
            함수 이름: initialize()
            기능: 별도 thread에서 시장 데이터 초기화를 실행한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/08/20
            """
            try:
                controller.initialize_market_data()
            except Exception as error:
                initialization_errors.append(error)

        first_thread = Thread(target=initialize)
        first_thread.start()
        self.assertTrue(blocking_snapshot.first_update_started.wait(timeout=1))

        next_closes = dict(DEFAULT_CLOSE_BY_INTERVAL)
        next_closes[Interval.FOUR_HOURS.value] = "222"
        self.rest_client.responses = _create_rest_responses(next_closes)
        second_rest_started = Event()

        def record_second_rest_request(interval: str) -> None:
            """
            함수 이름: record_second_rest_request()
            기능: 두 번째 초기화가 REST 단계에 진입했음을 기록한다.
            인자: interval -> 조회 중인 Binance interval
            반환값: 없음
            작성 날짜: 2026/08/20
            """
            second_rest_started.set()

        self.rest_client.on_request = record_second_rest_request
        second_thread = Thread(target=initialize)
        second_thread.start()
        second_started_before_release = second_rest_started.wait(timeout=0.1)
        blocking_snapshot.allow_first_update.set()
        first_thread.join(timeout=2)
        second_thread.join(timeout=2)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertFalse(second_started_before_release)
        self.assertEqual(initialization_errors, [])
        self.assertEqual(blocking_snapshot.version, 2)
        self.assertEqual(
            blocking_snapshot.current_eth_price,
            Decimal("222"),
        )

    def test_constructor_rejects_invalid_kline_limit_without_calls(self) -> None:
        """
        함수 이름: test_constructor_rejects_invalid_kline_limit_without_calls()
        기능: Binance 허용 범위를 벗어난 limit을 외부 호출 전에 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        invalid_limits = (True, Decimal("1"), 0, 1001)

        for invalid_limit in invalid_limits:
            with self.subTest(invalid_limit=invalid_limit):
                expected_error = (
                    TypeError
                    if isinstance(invalid_limit, (bool, Decimal))
                    else ValueError
                )
                with self.assertRaises(expected_error):
                    MarketDataController(
                        api_gateway=self.api_gateway,
                        web_socket_gateway=self.web_socket_gateway,
                        market_snapshot=self.market_snapshot,
                        regime_controller=self.regime_controller,
                        kline_limit=invalid_limit,
                    )

        self.assertEqual(self.operation_trace, [])
        self.assertEqual(self.external_trace, [])


if __name__ == "__main__":
    unittest.main()
