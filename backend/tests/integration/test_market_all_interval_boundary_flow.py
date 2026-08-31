"""UTC 4H·1D 경계의 all-interval atomic Kline coalescing을 검증한다."""

from datetime import datetime, timedelta, timezone
from itertools import permutations
import unittest

from binance_auto_trader.adapters.binance.api_gateway import APIGateway
from binance_auto_trader.adapters.binance.websocket_gateway import (
    WebSocketGateway,
)
from binance_auto_trader.application.market_data_controller import (
    MarketDataController,
    MarketDataStreamStateError,
)
from binance_auto_trader.domain.common import Interval
from binance_auto_trader.domain.market import MarketSnapshot
from binance_auto_trader.domain.regime import RegimeEvaluationTrigger

from tests.integration.test_market_initialization_flow import (
    SYMBOL,
    ControlledClock,
    FakeRestClient,
    FakeWebSocketClient,
    _create_rest_kline_payload,
    _create_web_socket_kline_payload,
)
from tests.integration.test_market_live_stream_flow import (
    RecordingEvaluationBuilder,
    RecordingRegimeController,
    RecordingTradingControllerPort,
)


# 4H 일반 경계와 UTC 자정을 분리해 같은 coordinator의 required source 집합을 검증한다.
FOUR_HOUR_BOUNDARY = datetime(
    2026,
    8,
    20,
    4,
    0,
    tzinfo=timezone.utc,
)
ONE_DAY_BOUNDARY = datetime(
    2026,
    8,
    21,
    0,
    0,
    tzinfo=timezone.utc,
)
BoundarySourceSpec = tuple[Interval, str, int, int, bool]


def _datetime_to_milliseconds(value: datetime) -> int:
    """
    함수 이름: _datetime_to_milliseconds()
    기능: UTC datetime을 WebSocket fixture의 Unix millisecond 정수로 변환한다.
    인자: value -> 변환할 UTC datetime
    반환값: Unix epoch 기준 millisecond
    작성 날짜: 2026/08/29
    """
    return int(value.timestamp() * 1_000)  # Test source의 t와 E를 정수 millisecond로 고정한다.


def _create_all_interval_boundary_flow(
    boundary_time: datetime,
) -> tuple[
    MarketDataController,
    MarketSnapshot,
    ControlledClock,
    FakeRestClient,
    FakeWebSocketClient,
    RecordingEvaluationBuilder,
    RecordingTradingControllerPort,
    RecordingRegimeController,
]:
    """
    함수 이름: _create_all_interval_boundary_flow()
    기능: 지정 UTC 경계 직전의 열린 1m·30m·4H·1D를 가진 production callback 흐름을 만든다.
    인자: boundary_time -> 4시간 정각 또는 UTC 자정 boundary
    반환값: Controller, snapshot/clock, fake clients, builder/observer와 REGIME controller
    작성 날짜: 2026/08/29
    """
    snapshot_time = boundary_time - timedelta(milliseconds=500)
    one_day_open = boundary_time.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    if boundary_time.hour == 0:
        one_day_open -= timedelta(days=1)

    # REST 기준선은 각 interval에서 boundary를 포함하는 마지막 진행봉 하나만 제공한다.
    rest_responses = {
        Interval.ONE_MINUTE.value: [
            _create_rest_kline_payload(
                Interval.ONE_MINUTE.value,
                "119",
                _datetime_to_milliseconds(
                    boundary_time - timedelta(minutes=1)
                ),
            )
        ],
        Interval.THIRTY_MINUTES.value: [
            _create_rest_kline_payload(
                Interval.THIRTY_MINUTES.value,
                "120",
                _datetime_to_milliseconds(
                    boundary_time - timedelta(minutes=30)
                ),
            )
        ],
        Interval.FOUR_HOURS.value: [
            _create_rest_kline_payload(
                Interval.FOUR_HOURS.value,
                "130",
                _datetime_to_milliseconds(
                    boundary_time - timedelta(hours=4)
                ),
            )
        ],
        Interval.ONE_DAY.value: [
            _create_rest_kline_payload(
                Interval.ONE_DAY.value,
                "140",
                _datetime_to_milliseconds(one_day_open),
            )
        ],
    }
    external_trace: list[str] = []
    gateway_clock = ControlledClock(snapshot_time)
    snapshot_clock = ControlledClock(snapshot_time)
    rest_client = FakeRestClient(rest_responses, external_trace)
    web_socket_client = FakeWebSocketClient(external_trace)
    market_snapshot = MarketSnapshot(
        symbol=SYMBOL,
        clock=snapshot_clock,
    )
    evaluation_builder = RecordingEvaluationBuilder()
    trading_controller_port = RecordingTradingControllerPort()
    regime_controller = RecordingRegimeController()
    controller = MarketDataController(
        api_gateway=APIGateway(rest_client, clock=gateway_clock),
        web_socket_gateway=WebSocketGateway(web_socket_client),
        market_snapshot=market_snapshot,
        regime_controller=regime_controller,
        market_evaluation_builder=evaluation_builder,
        trading_market_observer=trading_controller_port,
        market_stream_state_observer=trading_controller_port,
    )
    controller.initialize_market_data()

    # 모든 live source E와 원자 snapshot commit은 실제 boundary 직후 시각을 사용한다.
    snapshot_clock.current_time = boundary_time + timedelta(seconds=1)
    return (
        controller,
        market_snapshot,
        snapshot_clock,
        rest_client,
        web_socket_client,
        evaluation_builder,
        trading_controller_port,
        regime_controller,
    )


def _create_boundary_source_specs(
    boundary_time: datetime,
    *,
    include_one_day: bool,
) -> dict[str, BoundarySourceSpec]:
    """
    함수 이름: _create_boundary_source_specs()
    기능: 한 boundary의 canonical close/open source를 고정 E와 함께 role별로 만든다.
    인자: boundary_time -> 모든 source가 공유할 UTC boundary
        include_one_day -> UTC 자정의 1D close/open을 포함할지 여부
    반환값: role 이름별 WebSocket emit 인자 tuple
    작성 날짜: 2026/08/29
    """
    boundary_milliseconds = _datetime_to_milliseconds(boundary_time)
    source_specs: dict[str, BoundarySourceSpec] = {
        "one_minute_close": (
            Interval.ONE_MINUTE,
            "121",
            boundary_milliseconds - 60_000,
            boundary_milliseconds + 100,
            True,
        ),
        "thirty_minute_close": (
            Interval.THIRTY_MINUTES,
            "120",
            boundary_milliseconds - 1_800_000,
            boundary_milliseconds + 200,
            True,
        ),
        "four_hour_close": (
            Interval.FOUR_HOURS,
            "130",
            boundary_milliseconds - 14_400_000,
            boundary_milliseconds + 300,
            True,
        ),
        "four_hour_open": (
            Interval.FOUR_HOURS,
            "131",
            boundary_milliseconds,
            boundary_milliseconds + 400,
            False,
        ),
    }
    if include_one_day:
        # UTC 자정은 closed/open 1D를 4H pair 뒤의 canonical source로 추가한다.
        source_specs.update(
            {
                "one_day_close": (
                    Interval.ONE_DAY,
                    "140",
                    boundary_milliseconds - 86_400_000,
                    boundary_milliseconds + 500,
                    True,
                ),
                "one_day_open": (
                    Interval.ONE_DAY,
                    "141",
                    boundary_milliseconds,
                    boundary_milliseconds + 600,
                    False,
                ),
            }
        )
    return source_specs


def _create_valid_arrival_orders(
    *,
    include_one_day: bool,
) -> tuple[tuple[str, ...], ...]:
    """
    함수 이름: _create_valid_arrival_orders()
    기능: interval 내부 close→open 순서를 지키는 모든 cross-stream source permutation을 만든다.
    인자: include_one_day -> 1D close/open role까지 순열에 포함할지 여부
    반환값: 가능한 모든 role arrival-order tuple
    작성 날짜: 2026/08/29
    """
    roles = (
        "one_minute_close",
        "thirty_minute_close",
        "four_hour_close",
        "four_hour_open",
    )
    if include_one_day:
        roles = (*roles, "one_day_close", "one_day_open")

    # 같은 WebSocket interval의 close가 next-open보다 먼저라는 유일한 partial order만 적용한다.
    valid_orders = []
    for arrival_order in permutations(roles):
        if arrival_order.index("four_hour_close") > arrival_order.index(
            "four_hour_open"
        ):
            continue
        if include_one_day and arrival_order.index(
            "one_day_close"
        ) > arrival_order.index("one_day_open"):
            continue
        valid_orders.append(arrival_order)
    return tuple(valid_orders)  # 4H 12개, UTC 자정 180개 순열을 재현 가능하게 고정한다.


def _emit_boundary_source(
    web_socket_client: FakeWebSocketClient,
    source_spec: BoundarySourceSpec,
) -> None:
    """
    함수 이름: _emit_boundary_source()
    기능: 현재 production Gateway 구독에 한 canonical 또는 결함 주입 Kline을 전달한다.
    인자: web_socket_client -> live subscription을 가진 in-memory fake client
        source_spec -> interval, close, t, E와 x flag tuple
    반환값: 없음
    작성 날짜: 2026/08/29
    """
    subscription = web_socket_client.latest_handle
    if subscription is None:
        raise AssertionError("live Kline subscription is missing")
    interval, close, open_time, event_time, closed = source_spec

    # Raw 공식형 payload가 Gateway normalization과 Controller callback을 모두 통과하게 한다.
    web_socket_client.emit(
        subscription,
        _create_web_socket_kline_payload(
            interval,
            close,
            open_time_milliseconds=open_time,
            event_time_milliseconds=event_time,
            closed=closed,
        ),
    )


class MarketAllIntervalBoundaryFlowTests(unittest.TestCase):
    """
    클래스 이름: MarketAllIntervalBoundaryFlowTests
    기능: 4H·1D 동시 rollover의 bounded atomicity, provenance와 REGIME 결합을 검증한다.
    작성 날짜: 2026/08/29
    """

    def test_all_valid_arrival_permutations_commit_one_atomic_version(
        self,
    ) -> None:
        """
        함수 이름: test_all_valid_arrival_permutations_commit_one_atomic_version()
        기능: 4H 12개와 UTC 자정 180개 유효 arrival permutation이 같은 canonical 결과인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        boundary_cases = (
            (FOUR_HOUR_BOUNDARY, False),
            (ONE_DAY_BOUNDARY, True),
        )
        for boundary_time, include_one_day in boundary_cases:
            canonical_source_event_id: str | None = None
            arrival_orders = _create_valid_arrival_orders(
                include_one_day=include_one_day
            )
            expected_order_count = 180 if include_one_day else 12
            self.assertEqual(len(arrival_orders), expected_order_count)

            # 모든 cross-stream 순열은 source tuple과 publication version을 동일하게 만든다.
            for arrival_order in arrival_orders:
                with self.subTest(
                    boundary_time=boundary_time,
                    arrival_order=arrival_order,
                ):
                    (
                        controller,
                        market_snapshot,
                        _snapshot_clock,
                        rest_client,
                        web_socket_client,
                        evaluation_builder,
                        trading_controller_port,
                        regime_controller,
                    ) = _create_all_interval_boundary_flow(boundary_time)
                    source_specs = _create_boundary_source_specs(
                        boundary_time,
                        include_one_day=include_one_day,
                    )
                    version_before = market_snapshot.version
                    regime_count_before = len(regime_controller.calls)

                    for arrival_index, role in enumerate(arrival_order):
                        _emit_boundary_source(
                            web_socket_client,
                            source_specs[role],
                        )
                        if arrival_index < len(arrival_order) - 1:
                            self.assertEqual(
                                market_snapshot.version,
                                version_before,
                            )
                            self.assertEqual(evaluation_builder.calls, [])
                            self.assertEqual(trading_controller_port.calls, [])

                    # 마지막 필수 source만 atomic version, REGIME과 시장 평가를 각각 한 번 만든다.
                    self.assertEqual(
                        market_snapshot.version,
                        version_before + 1,
                    )
                    expected_intervals = (
                        Interval.ONE_MINUTE,
                        Interval.THIRTY_MINUTES,
                        Interval.FOUR_HOURS,
                        Interval.FOUR_HOURS,
                    )
                    expected_closed = (True, True, True, False)
                    if include_one_day:
                        expected_intervals = (
                            *expected_intervals,
                            Interval.ONE_DAY,
                            Interval.ONE_DAY,
                        )
                        expected_closed = (*expected_closed, True, False)
                    boundary_sources = (
                        market_snapshot.update_source_klines
                    )
                    self.assertEqual(
                        tuple(
                            source.interval
                            for source in boundary_sources
                        ),
                        expected_intervals,
                    )
                    self.assertEqual(
                        tuple(source.closed for source in boundary_sources),
                        expected_closed,
                    )
                    self.assertEqual(len(evaluation_builder.calls), 1)
                    self.assertIs(
                        evaluation_builder.calls[0][1].interval,
                        Interval.THIRTY_MINUTES,
                    )
                    self.assertEqual(len(trading_controller_port.calls), 1)
                    evaluation, source_event_id, market_version = (
                        trading_controller_port.calls[0]
                    )
                    self.assertTrue(evaluation.confirmed_1m_close)
                    self.assertTrue(evaluation.confirmed_30m_close)
                    self.assertEqual(market_version, market_snapshot.version)
                    self.assertEqual(
                        len(regime_controller.calls),
                        regime_count_before + 1,
                    )
                    self.assertEqual(
                        regime_controller.calls[-1][0],
                        RegimeEvaluationTrigger.FOUR_HOUR_CANDLE_CLOSE,
                    )
                    self.assertIs(
                        regime_controller.calls[-1][1],
                        market_snapshot,
                    )

                    # Canonical source ID와 최신 upper OPEN은 arrival order에 영향받지 않는다.
                    if canonical_source_event_id is None:
                        canonical_source_event_id = source_event_id
                    self.assertEqual(
                        source_event_id,
                        canonical_source_event_id,
                    )
                    self.assertEqual(
                        market_snapshot.klines_by_interval[
                            Interval.FOUR_HOURS
                        ][-1].open_time,
                        boundary_time,
                    )
                    self.assertFalse(
                        market_snapshot.klines_by_interval[
                            Interval.FOUR_HOURS
                        ][-1].closed
                    )
                    if include_one_day:
                        self.assertEqual(
                            market_snapshot.klines_by_interval[
                                Interval.ONE_DAY
                            ][-1].open_time,
                            boundary_time,
                        )
                        self.assertFalse(
                            market_snapshot.klines_by_interval[
                                Interval.ONE_DAY
                            ][-1].closed
                        )
                    self.assertTrue(controller.market_available)
                    self.assertEqual(rest_client.order_call_count, 0)
                    self.assertEqual(
                        web_socket_client.account_stream_call_count,
                        0,
                    )

    def test_upper_boundary_successor_overflow_fails_without_commit(
        self,
    ) -> None:
        """
        함수 이름: test_upper_boundary_successor_overflow_fails_without_commit()
        기능: 필수 source 대기 중 4H·1D 한 slot을 넘어선 next-open을 fail closed하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        overflow_cases = (
            (
                FOUR_HOUR_BOUNDARY,
                False,
                ("four_hour_close", "four_hour_open", "one_minute_close"),
                Interval.FOUR_HOURS,
                timedelta(hours=4),
            ),
            (
                ONE_DAY_BOUNDARY,
                True,
                (
                    "one_day_close",
                    "one_day_open",
                    "four_hour_close",
                    "four_hour_open",
                    "one_minute_close",
                ),
                Interval.ONE_DAY,
                timedelta(days=1),
            ),
        )
        for (
            boundary_time,
            include_one_day,
            staged_roles,
            overflow_interval,
            overflow_delta,
        ) in overflow_cases:
            with self.subTest(overflow_interval=overflow_interval):
                (
                    controller,
                    market_snapshot,
                    _snapshot_clock,
                    _rest_client,
                    web_socket_client,
                    evaluation_builder,
                    trading_controller_port,
                    _regime_controller,
                ) = _create_all_interval_boundary_flow(boundary_time)
                source_specs = _create_boundary_source_specs(
                    boundary_time,
                    include_one_day=include_one_day,
                )
                version_before = market_snapshot.version
                for role in staged_roles:
                    _emit_boundary_source(
                        web_socket_client,
                        source_specs[role],
                    )

                # 같은 upper interval의 두 번째 next-open은 bounded slot을 초과하므로 publication 전 실패한다.
                overflow_open = boundary_time + overflow_delta
                overflow_spec: BoundarySourceSpec = (
                    overflow_interval,
                    "150",
                    _datetime_to_milliseconds(overflow_open),
                    _datetime_to_milliseconds(
                        overflow_open + timedelta(milliseconds=100)
                    ),
                    False,
                )
                with self.assertRaises(MarketDataStreamStateError):
                    _emit_boundary_source(
                        web_socket_client,
                        overflow_spec,
                    )
                self.assertEqual(market_snapshot.version, version_before)
                self.assertEqual(evaluation_builder.calls, [])
                self.assertEqual(trading_controller_port.calls, [])
                self.assertFalse(controller.market_available)

    def test_mismatched_boundary_and_upper_open_before_close_fail_closed(
        self,
    ) -> None:
        """
        함수 이름: test_mismatched_boundary_and_upper_open_before_close_fail_closed()
        기능: active boundary와 다른 4H close 및 close보다 먼저 온 4H·1D OPEN을 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        error_cases = (
            "mismatched_boundary",
            "four_hour_open_before_close",
            "one_day_open_before_close",
        )
        for error_case in error_cases:
            with self.subTest(error_case=error_case):
                boundary_time = (
                    ONE_DAY_BOUNDARY
                    if error_case == "one_day_open_before_close"
                    else FOUR_HOUR_BOUNDARY
                )
                (
                    controller,
                    market_snapshot,
                    _snapshot_clock,
                    _rest_client,
                    web_socket_client,
                    evaluation_builder,
                    trading_controller_port,
                    _regime_controller,
                ) = _create_all_interval_boundary_flow(
                    boundary_time
                )
                source_specs = _create_boundary_source_specs(
                    boundary_time,
                    include_one_day=(
                        error_case == "one_day_open_before_close"
                    ),
                )
                version_before = market_snapshot.version
                _emit_boundary_source(
                    web_socket_client,
                    source_specs["one_minute_close"],
                )

                # 결함 source는 다른 4H boundary 또는 같은 boundary에서 close보다 먼저 온 upper OPEN이다.
                if error_case == "mismatched_boundary":
                    invalid_time = FOUR_HOUR_BOUNDARY + timedelta(hours=4)
                    invalid_spec: BoundarySourceSpec = (
                        Interval.FOUR_HOURS,
                        "150",
                        _datetime_to_milliseconds(FOUR_HOUR_BOUNDARY),
                        _datetime_to_milliseconds(
                            invalid_time + timedelta(milliseconds=100)
                        ),
                        True,
                    )
                elif error_case == "one_day_open_before_close":
                    invalid_spec = source_specs["one_day_open"]
                else:
                    invalid_spec = source_specs["four_hour_open"]
                with self.assertRaises(MarketDataStreamStateError):
                    _emit_boundary_source(
                        web_socket_client,
                        invalid_spec,
                    )
                self.assertEqual(market_snapshot.version, version_before)
                self.assertEqual(evaluation_builder.calls, [])
                self.assertEqual(trading_controller_port.calls, [])
                self.assertFalse(controller.market_available)

    def test_upper_closed_event_time_before_boundary_fails_closed(
        self,
    ) -> None:
        """
        함수 이름: test_upper_closed_event_time_before_boundary_fails_closed()
        기능: 4H·1D x=true source의 E가 실제 close boundary 전이면 snapshot을 보존하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        event_time_cases = (
            (FOUR_HOUR_BOUNDARY, Interval.FOUR_HOURS, timedelta(hours=4)),
            (ONE_DAY_BOUNDARY, Interval.ONE_DAY, timedelta(days=1)),
        )
        for boundary_time, interval, interval_duration in event_time_cases:
            with self.subTest(interval=interval):
                (
                    controller,
                    market_snapshot,
                    _snapshot_clock,
                    _rest_client,
                    web_socket_client,
                    evaluation_builder,
                    trading_controller_port,
                    _regime_controller,
                ) = _create_all_interval_boundary_flow(boundary_time)
                version_before = market_snapshot.version
                invalid_spec: BoundarySourceSpec = (
                    interval,
                    "150",
                    _datetime_to_milliseconds(
                        boundary_time - interval_duration
                    ),
                    _datetime_to_milliseconds(
                        boundary_time - timedelta(milliseconds=1)
                    ),
                    True,
                )

                # Open 이후라는 형식만 맞아도 x=true E가 close 전이면 final provenance가 아니다.
                with self.assertRaisesRegex(
                    MarketDataStreamStateError,
                    "close boundary",
                ):
                    _emit_boundary_source(
                        web_socket_client,
                        invalid_spec,
                    )
                self.assertEqual(market_snapshot.version, version_before)
                self.assertEqual(evaluation_builder.calls, [])
                self.assertEqual(trading_controller_port.calls, [])
                self.assertFalse(controller.market_available)


if __name__ == "__main__":
    unittest.main()  # 직접 실행과 discovery가 같은 all-interval boundary suite를 사용한다.
