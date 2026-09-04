"""결정론적 public Kline에서 production Case 2 전체 경로를 검증한다."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from binance_auto_trader.bootstrap import (
    ApplicationRuntime,
    ApplicationStatus,
    ExecutionMode,
    close_application,
    create_application_runtime,
    start_application,
)
from binance_auto_trader.bootstrap.application import _FAKE_ORDER_CAPABILITY
from binance_auto_trader.application import TradingSessionStatus
from binance_auto_trader.domain.common import Interval, RegimeType
from binance_auto_trader.domain.market import Kline
from binance_auto_trader.domain.trading import (
    ExitReason,
    OrderSide,
    StrategyType,
)
from binance_auto_trader.transport import (
    BackendEventStream,
    create_account_update_observer,
    create_trade_history_update_observer,
    create_trading_session_update_observer,
)

from tests.integration.phase13_risk_fixture import create_test_risk_policy
from tests.integration.test_buy_sell_flow import MutableUtcClock
from tests.integration.test_public_market_case2_flow import (
    CURRENT_THIRTY_MINUTE_OPEN,
    INITIAL_TIME,
    PublicCase2OrderScenario,
    PublicCase2RESTClient,
    _create_rest_kline_row,
)
from tests.testnet._deterministic_public_case2_fixture import (
    create_deterministic_public_case2_klines,
)


# Background production worker를 실제로 사용하되 local test가 무한 대기하지 않게 짧은 상한을 둔다.
_ASYNC_COMPLETION_TIMEOUT_SECONDS = 3.0
_ASYNC_WAIT_SLICE_SECONDS = 0.05
_FOUR_HOUR_CLOSES = tuple(
    Decimal(value)
    for value in (
        "100",
        "101",
        "102",
        "103",
        "104",
        "105",
        "106",
        "107",
        "108",
        "109",
        "110",
        "111",
        "112",
        "113",
    )
)
_FOUR_HOUR_HIGHS = tuple(
    Decimal(value)
    for value in (
        "110",
        "111",
        "115",
        "112",
        "113",
        "116",
        "114",
        "115",
        "117",
        "116",
        "117",
        "118",
        "117",
        "116",
    )
)
_FOUR_HOUR_LOWS = tuple(
    Decimal(value)
    for value in (
        "99",
        "98",
        "97",
        "95",
        "97",
        "98",
        "96",
        "99",
        "100",
        "101",
        "102",
        "103",
        "104",
        "105",
    )
)


class DeterministicProductionPathRESTClient(PublicCase2RESTClient):
    """
    클래스 이름: DeterministicProductionPathRESTClient
    기능: production startup 시장·계좌와 두 immediate fill을 memory 응답으로 제공한다.
    작성 날짜: 2026/09/04
    """

    def __init__(self, clock: MutableUtcClock) -> None:
        """
        함수 이름: __init__()
        기능: Case C immediate fill과 REGIME 계산이 가능한 4시간봉 기준선을 준비한다.
        인자: clock -> market, order와 persistence가 공유할 결정론적 UTC clock
        반환값: 없음
        작성 날짜: 2026/09/04
        """
        super().__init__(
            clock,
            PublicCase2OrderScenario.IMMEDIATE_FILLED,
        )

        # Production startup의 4H EMA9와 strict swing 계산에 필요한 14개 확정봉을 만든다.
        first_four_hour_open = CURRENT_THIRTY_MINUTE_OPEN - timedelta(
            hours=4 * len(_FOUR_HOUR_CLOSES)
        )
        closed_four_hour_rows: list[list[object]] = []
        for candle_index, close_price in enumerate(_FOUR_HOUR_CLOSES):
            kline_row = _create_rest_kline_row(
                Interval.FOUR_HOURS,
                first_four_hour_open + timedelta(hours=4 * candle_index),
                close_price,
            )
            kline_row[2] = format(_FOUR_HOUR_HIGHS[candle_index], "f")
            kline_row[3] = format(_FOUR_HOUR_LOWS[candle_index], "f")
            closed_four_hour_rows.append(kline_row)

        # 마지막 행은 runtime clock을 포함하는 유일한 진행 4시간봉으로 유지한다.
        current_four_hour_row = _create_rest_kline_row(
            Interval.FOUR_HOURS,
            CURRENT_THIRTY_MINUTE_OPEN,
            Decimal("110"),
        )
        current_four_hour_row[2] = "120"
        current_four_hour_row[3] = "100"
        self.market_responses[Interval.FOUR_HOURS.value] = [
            *closed_four_hour_rows,
            current_four_hour_row,
        ]
        self.open_order_queries = 0
        self.recent_order_queries = 0

    def list_open_order_results(
        self,
        *,
        symbol: str,
    ) -> tuple[object, ...]:
        """
        함수 이름: list_open_order_results()
        기능: startup reconciliation에 app-owned open order가 없음을 memory에서 반환한다.
        인자: symbol -> 조회할 Spot symbol
        반환값: 비어 있는 normalized order result tuple
        작성 날짜: 2026/09/04
        """
        if symbol != "ETHUSDT":
            raise AssertionError("unexpected open-order symbol")

        self.open_order_queries += 1
        return ()  # 외부 exchange 조회 없이 zero-open baseline을 제공한다.

    def list_recent_order_results(
        self,
        *,
        symbol: str,
        limit: int,
    ) -> tuple[object, ...]:
        """
        함수 이름: list_recent_order_results()
        기능: startup reconciliation에 최근 app order가 없음을 memory에서 반환한다.
        인자: symbol -> 조회할 Spot symbol
            limit -> production reconciliation 조회 상한
        반환값: 비어 있는 normalized order result tuple
        작성 날짜: 2026/09/04
        """
        if symbol != "ETHUSDT" or limit != 100:
            raise AssertionError("unexpected recent-order request")

        self.recent_order_queries += 1
        return ()  # Fresh local history와 대응할 exchange order도 없다.


class MemorySubscription:
    """
    클래스 이름: MemorySubscription
    기능: production Gateway가 소유할 network-free stream handle의 close 상태를 보존한다.
    작성 날짜: 2026/09/04
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 열려 있고 close 호출이 없는 memory subscription을 만든다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/04
        """
        self.close_count = 0

    def close(self) -> None:
        """
        함수 이름: close()
        기능: production lifecycle이 요청한 멱등 close 횟수를 기록한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/04
        """
        self.close_count += 1  # 실제 socket이나 disconnect callback은 생성하지 않는다.


class DeterministicProductionPathWebSocketClient:
    """
    클래스 이름: DeterministicProductionPathWebSocketClient
    기능: market/account production subscription 경계를 callback registry로만 제공한다.
    작성 날짜: 2026/09/04
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 비어 있는 callback과 subscription 목록을 초기화한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/04
        """
        self.kline_callback: Callable[[object], None] | None = None
        self.kline_disconnect_callback: Callable[[], None] | None = None
        self.account_callback: Callable[[object], None] | None = None
        self.account_disconnect_callback: Callable[[], None] | None = None
        self.subscriptions: list[MemorySubscription] = []

    def subscribe_all_kline_streams(
        self,
        *,
        symbol: str,
        intervals: tuple[str, ...],
        on_message: Callable[[object], None],
        on_disconnect: Callable[[], None],
    ) -> MemorySubscription:
        """
        함수 이름: subscribe_all_kline_streams()
        기능: production Kline callbacks를 저장하고 열린 memory handle을 반환한다.
        인자: symbol -> 구독할 Spot symbol
            intervals -> 구독할 네 Binance interval
            on_message -> raw Kline callback
            on_disconnect -> stream 종료 callback
        반환값: lifecycle close를 기록할 memory subscription
        작성 날짜: 2026/09/04
        """
        if symbol != "ETHUSDT" or intervals != (
            "1m",
            "30m",
            "4h",
            "1d",
        ):
            raise AssertionError("unexpected Kline subscription")

        # Callback은 보존만 하고 fixture Kline은 public MarketDataController 경계에 직접 전달한다.
        self.kline_callback = on_message
        self.kline_disconnect_callback = on_disconnect
        subscription = MemorySubscription()
        self.subscriptions.append(subscription)
        return subscription

    def subscribe_account_info(
        self,
        *,
        on_message: Callable[[object], None],
        on_disconnect: Callable[[], None],
    ) -> MemorySubscription:
        """
        함수 이름: subscribe_account_info()
        기능: production account callbacks를 저장하고 열린 memory handle을 반환한다.
        인자: on_message -> raw account/order event callback
            on_disconnect -> account stream 종료 callback
        반환값: lifecycle close를 기록할 memory subscription
        작성 날짜: 2026/09/04
        """
        # REST account snapshot이 이미 authoritative baseline이므로 startup 중 synthetic patch는 만들지 않는다.
        self.account_callback = on_message
        self.account_disconnect_callback = on_disconnect
        subscription = MemorySubscription()
        self.subscriptions.append(subscription)
        return subscription


@dataclass(slots=True)
class DeterministicProductionPathFixture:
    """
    클래스 이름: DeterministicProductionPathFixture
    기능: ApplicationRuntime과 memory 외부 경계 및 BackendEventStream을 한 묶음으로 보존한다.
    작성 날짜: 2026/09/04
    """

    runtime: ApplicationRuntime
    rest_client: DeterministicProductionPathRESTClient
    web_socket_client: DeterministicProductionPathWebSocketClient
    event_stream: BackendEventStream
    history_path: Path


def _create_deterministic_production_path_fixture(
    temporary_directory: str,
) -> DeterministicProductionPathFixture:
    """
    함수 이름: _create_deterministic_production_path_fixture()
    기능: production runtime, durable repository와 UI event observer를 memory boundary에 조립한다.
    인자: temporary_directory -> history와 pending sidecar를 둘 임시 디렉터리
    반환값: start_application을 호출할 준비가 된 full-runtime fixture
    작성 날짜: 2026/09/04
    """
    clock = MutableUtcClock(INITIAL_TIME)
    rest_client = DeterministicProductionPathRESTClient(clock)
    web_socket_client = DeterministicProductionPathWebSocketClient()
    event_stream = BackendEventStream(clock=clock)
    history_path = Path(temporary_directory) / "deterministic-case2.jsonl"

    def monotonic_time() -> int:
        """
        함수 이름: monotonic_time()
        기능: Case C와 무관한 duration flag가 전진하지 않는 결정론적 monotonic 시각을 반환한다.
        인자: 없음
        반환값: 고정 nanosecond 0
        작성 날짜: 2026/09/04
        """
        return 0  # 세 Case C 입력의 180초 recovery window를 같은 기준으로 유지한다.

    # Production composition root가 entity, controller, worker와 repository identity를 직접 조립한다.
    runtime = create_application_runtime(
        rest_client,
        web_socket_client,
        history_path=history_path,
        execution_mode="fake",
        _fake_order_capability=_FAKE_ORDER_CAPABILITY,
        risk_policy_state=create_test_risk_policy(),
        account_update_observer=create_account_update_observer(event_stream),
        trade_history_update_observer=(
            create_trade_history_update_observer(event_stream)
        ),
        trading_session_update_observer=(
            create_trading_session_update_observer(event_stream)
        ),
        clock=clock,
        monotonic_clock=monotonic_time,
        kline_limit=21,
    )
    return DeterministicProductionPathFixture(
        runtime=runtime,
        rest_client=rest_client,
        web_socket_client=web_socket_client,
        event_stream=event_stream,
        history_path=history_path,
    )


def _wait_for_market_evaluation(
    fixture: DeterministicProductionPathFixture,
    expected_kline: Kline,
) -> None:
    """
    함수 이름: _wait_for_market_evaluation()
    기능: background worker가 정확한 fixture Kline의 production evaluation을 Context에 적용할 때까지 기다린다.
    인자: fixture -> 실행 중인 full-runtime fixture
        expected_kline -> 이번 stage에서 관찰한 public Kline
    반환값: exact price와 low가 Context에 적용되면 없음
    작성 날짜: 2026/09/04
    """
    deadline = time.monotonic() + _ASYNC_COMPLETION_TIMEOUT_SECONDS
    after_sequence = fixture.event_stream.last_sequence

    # EventStream condition을 사용해 worker publication을 기다리고 wall-clock busy loop를 피한다.
    while time.monotonic() < deadline:
        market = fixture.runtime.trading_controller.context.market
        if (
            market.realtime_price == expected_kline.close
            and market.current_30m_low == expected_kline.low
        ):
            return
        replay_batch = fixture.event_stream.wait_for_events(
            after_sequence,
            timeout=_ASYNC_WAIT_SLICE_SECONDS,
        )
        if replay_batch.requires_resync:
            raise AssertionError("backend event replay requires resynchronization")
        if replay_batch.events:
            after_sequence = replay_batch.events[-1].sequence

    raise TimeoutError("production worker did not apply deterministic market input")


def _wait_for_trade_count(
    fixture: DeterministicProductionPathFixture,
    expected_count: int,
) -> None:
    """
    함수 이름: _wait_for_trade_count()
    기능: production worker와 durable observer가 exact Trade 개수를 게시할 때까지 기다린다.
    인자: fixture -> 실행 중인 full-runtime fixture
        expected_count -> 기다릴 authoritative history Trade 개수
    반환값: exact 개수에 도달하면 없음
    작성 날짜: 2026/09/04
    """
    deadline = time.monotonic() + _ASYNC_COMPLETION_TIMEOUT_SECONDS
    after_sequence = fixture.event_stream.last_sequence

    # History count가 목표를 넘으면 중복 order이므로 timeout 대신 즉시 실패한다.
    while time.monotonic() < deadline:
        trade_count = len(fixture.runtime.trade_history.trades)
        if trade_count == expected_count:
            return
        if trade_count > expected_count:
            raise AssertionError("production path created duplicate durable Trades")
        replay_batch = fixture.event_stream.wait_for_events(
            after_sequence,
            timeout=_ASYNC_WAIT_SLICE_SECONDS,
        )
        if replay_batch.requires_resync:
            raise AssertionError("backend event replay requires resynchronization")
        if replay_batch.events:
            after_sequence = replay_batch.events[-1].sequence

    raise TimeoutError("production worker did not publish the expected Trade count")


def _close_deterministic_fixture(
    fixture: DeterministicProductionPathFixture,
) -> None:
    """
    함수 이름: _close_deterministic_fixture()
    기능: 실패 assertion 뒤에도 production runtime과 event stream을 순서대로 닫는다.
    인자: fixture -> 닫을 full-runtime fixture
    반환값: 없음
    작성 날짜: 2026/09/04
    """
    try:
        if fixture.runtime.state.status is not ApplicationStatus.CLOSED:
            close_application(fixture.runtime)
    finally:
        fixture.event_stream.close()  # Runtime observer의 마지막 publication 뒤 waiter를 해제한다.


class DeterministicProductionPathCase2FlowTests(unittest.TestCase):
    """
    클래스 이름: DeterministicProductionPathCase2FlowTests
    기능: public Kline부터 strategy, order, persistence와 UI publication까지의 한 흐름을 검증한다.
    작성 날짜: 2026/09/04
    """

    def test_public_fixture_runs_buy_stop_sell_and_zero_exposure(
        self,
    ) -> None:
        """
        함수 이름: test_public_fixture_runs_buy_stop_sell_and_zero_exposure()
        기능: 결정론적 세 Kline이 production BUY 한 건과 public STOP SELL 한 건만 만드는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/04
        """
        with TemporaryDirectory() as temporary_directory:
            fixture = _create_deterministic_production_path_fixture(
                temporary_directory
            )
            self.addCleanup(_close_deterministic_fixture, fixture)
            ready_state = start_application(fixture.runtime)
            self.assertIs(ready_state.status, ApplicationStatus.READY)
            self.assertIs(fixture.runtime.execution_mode, ExecutionMode.FAKE)

            # TYPE_0, 전량 split과 start는 actual one-shot과 같은 public optimistic-version operation을 쓴다.
            controller = fixture.runtime.trading_controller
            selection = fixture.runtime.regime_controller.set_regime_type(
                RegimeType.TYPE_0,
                command_id="deterministic-case2-select",
                expected_version=controller.context.version,
            )
            split_result = controller.update_split_ratios(
                command_id="deterministic-case2-split",
                expected_version=selection.version,
                scale_in=Decimal("0.5"),
                scale_out=Decimal("1"),
            )
            start_result = controller.start_trading(
                command_id="deterministic-case2-start",
                expected_version=split_result.version,
            )
            self.assertIs(start_result.status, TradingSessionStatus.RUNNING)

            # Fixture는 current production snapshot만 읽고 세 public Kline 외의 업무 결과를 만들지 않는다.
            public_klines = create_deterministic_public_case2_klines(
                fixture.runtime.market_snapshot.get_snapshot()
            )
            self.assertEqual(
                (False, False, False),
                tuple(kline.closed for kline in public_klines.as_tuple()),
            )
            self.assertEqual(
                (public_klines.setup.open_time,) * 3,
                tuple(kline.open_time for kline in public_klines.as_tuple()),
            )
            self.assertEqual(
                tuple(sorted(kline.event_time for kline in public_klines.as_tuple())),
                tuple(kline.event_time for kline in public_klines.as_tuple()),
            )

            # SETUP evaluation은 production builder가 실제 %B와 CCI threshold를 모두 계산한다.
            fixture.runtime.market_data_controller.observe_kline(
                public_klines.setup
            )
            _wait_for_market_evaluation(fixture, public_klines.setup)
            setup_market = controller.context.market
            self.assertLessEqual(setup_market.realtime_pct_b, Decimal("-0.15"))
            self.assertLessEqual(setup_market.cci_30m_realtime, Decimal("-140"))
            self.assertEqual([], fixture.rest_client.submitted_orders)

            # FLUSH evaluation은 더 낮은 누적 low와 -0.25 이하 %B로 entry 기준을 연다.
            fixture.runtime.market_data_controller.observe_kline(
                public_klines.flush
            )
            _wait_for_market_evaluation(fixture, public_klines.flush)
            flush_market = controller.context.market
            flush_entry_percent_b = flush_market.realtime_pct_b + Decimal("0.06")
            self.assertLess(public_klines.flush.low, public_klines.setup.low)
            self.assertLessEqual(flush_market.realtime_pct_b, Decimal("-0.25"))
            self.assertLess(flush_entry_percent_b, Decimal("-0.15"))
            self.assertEqual(public_klines.flush.close, controller.context.runtime.flush_low)
            self.assertEqual([], fixture.rest_client.submitted_orders)

            # RECOVERY만 exact entry threshold를 넘겨 production STM이 유일한 BUY intent를 만든다.
            fixture.runtime.market_data_controller.observe_kline(
                public_klines.recovery
            )
            _wait_for_market_evaluation(fixture, public_klines.recovery)
            _wait_for_trade_count(fixture, 1)
            recovery_market = controller.context.market
            self.assertEqual(public_klines.flush.low, public_klines.recovery.low)
            self.assertGreaterEqual(
                recovery_market.realtime_pct_b,
                flush_entry_percent_b,
            )
            self.assertLess(recovery_market.realtime_pct_b, Decimal("-0.15"))

            # Public 1L trace와 memory exchange 사실은 strategy가 정확히 한 BUY action만 냈음을 결속한다.
            action_boundaries = tuple(
                trace_entry
                for trace_entry in controller.public_market_boundary_trace
                if trace_entry.message_id == "1L.3"
            )
            self.assertEqual(1, len(action_boundaries))
            self.assertEqual(1, len(fixture.rest_client.submitted_orders))
            buy_order = fixture.rest_client.submitted_orders[0]
            self.assertIs(buy_order.side, OrderSide.BUY)
            self.assertIs(buy_order.strategy, StrategyType.CASE_C)
            self.assertGreater(controller.position.quantity, Decimal("0"))

            # Public STOP은 열린 authoritative Position 전량으로 한 SELL만 만들고 worker가 terminal을 게시한다.
            opened_quantity = controller.position.quantity
            stop_result = controller.stop_trading(
                command_id="deterministic-case2-stop",
                expected_version=controller.context.version,
            )
            self.assertIn(
                stop_result.status,
                (
                    TradingSessionStatus.STOPPING,
                    TradingSessionStatus.TERMINATED,
                ),
            )
            _wait_for_trade_count(fixture, 2)
            self.assertEqual(2, len(fixture.rest_client.submitted_orders))
            sell_order = fixture.rest_client.submitted_orders[1]
            self.assertIs(sell_order.side, OrderSide.SELL)
            self.assertIs(sell_order.strategy, StrategyType.CASE_C)
            self.assertIs(sell_order.exit_reason, ExitReason.STOP)
            self.assertEqual(opened_quantity, sell_order.submitted_quantity)

            # BUY·SELL durable history와 Performance가 final zero Position/pending state와 일치해야 한다.
            trades = fixture.runtime.trade_history.trades
            self.assertEqual((OrderSide.BUY, OrderSide.SELL), tuple(trade.side for trade in trades))
            self.assertEqual(
                trades,
                fixture.runtime.trade_history_repository.get_trade_history(),
            )
            self.assertEqual(Decimal("0"), controller.position.quantity)
            self.assertEqual(Decimal("0"), controller.position.cost_basis)
            self.assertIsNone(controller.position.owner)
            self.assertEqual(
                (),
                fixture.runtime.trade_history_controller.get_pending_orders(),
            )
            performance = fixture.runtime.performance
            self.assertEqual(1, performance.completed_sell_count)
            self.assertEqual(trades[1].realized_pnl, performance.realized_pnl)
            self.assertTrue(fixture.history_path.is_file())
            self.assertEqual(
                2,
                len(fixture.history_path.read_text(encoding="utf-8").splitlines()),
            )

            # UI replay는 각 durable Trade마다 ORDER_EXECUTED 다음 PERFORMANCE_UPDATED를 원자 연속 발행한다.
            replay_batch = fixture.event_stream.replay_after(0)
            self.assertFalse(replay_batch.requires_resync)
            trade_events = tuple(
                event_envelope
                for event_envelope in replay_batch.events
                if event_envelope.event_type
                in ("ORDER_EXECUTED", "PERFORMANCE_UPDATED")
            )
            self.assertEqual(
                (
                    "ORDER_EXECUTED",
                    "PERFORMANCE_UPDATED",
                    "ORDER_EXECUTED",
                    "PERFORMANCE_UPDATED",
                ),
                tuple(event_envelope.event_type for event_envelope in trade_events),
            )
            for order_event, performance_event in (
                trade_events[:2],
                trade_events[2:],
            ):
                self.assertEqual(order_event.sequence + 1, performance_event.sequence)
                self.assertEqual(order_event.occurred_at, performance_event.occurred_at)

            # Memory clients만 사용했으며 actual Testnet module은 별도 opt-in 없이는 계속 safe skip된다.
            self.assertEqual(4, len(fixture.rest_client.kline_requests))
            self.assertGreaterEqual(fixture.rest_client.open_order_queries, 1)
            self.assertGreaterEqual(fixture.rest_client.recent_order_queries, 1)


if __name__ == "__main__":
    unittest.main()
