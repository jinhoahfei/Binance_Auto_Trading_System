"""Communication 1.4~1.5.1의 REGIME 추천 vertical slice를 종단 간 검증한다."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any
import unittest

from binance_auto_trader.application import (
    MarketDataController,
    RegimeController,
    RegimeEvaluationError,
    RegimeEvaluationFailureCode,
    RegimeEvaluationTrace,
)
from binance_auto_trader.domain.common import (
    Interval,
    RegimeType,
    SUPPORTED_INTERVALS,
)
from binance_auto_trader.domain.market import Kline, MarketSnapshot
from binance_auto_trader.domain.regime import (
    RegimeEvaluationContext,
    RegimeEvaluationTrigger,
    RegimeEvent,
    RegimeEventType,
    RegimeSTM,
    RegimeSTMResult,
    RegimeState,
)


SYMBOL = "ETHUSDT"
INITIAL_SNAPSHOT_TIME = datetime(
    2026,
    8,
    20,
    1,
    0,
    tzinfo=timezone.utc,
)
NEXT_SNAPSHOT_TIME = datetime(
    2026,
    8,
    20,
    5,
    0,
    tzinfo=timezone.utc,
)
FOUR_HOUR_DURATION = timedelta(hours=4)
CURRENT_FOUR_HOUR_OPEN = datetime(
    2026,
    8,
    20,
    0,
    0,
    tzinfo=timezone.utc,
)
GOLDEN_VECTOR_PATH = (
    Path(__file__).parents[1]
    / "fixtures"
    / "market_snapshots"
    / "indicator_golden_vector.json"
)
EXPECTED_COMMUNICATION_STEPS = (
    "1.4:MarketDataController->RegimeController.calculate_4h_indicators",
    "1.4.1:RegimeController->IndicatorSnapshot.update",
    "1.5:MarketDataController->RegimeController.recommend_regime",
    "1.5.1:RegimeController->RegimeSTM.handle:trigger",
    "1.5.1:RegimeController->RegimeSTM.handle:EVALUATION_READY",
)


class MutableClock:
    """
    클래스 이름: MutableClock
    기능: MarketSnapshot version별 갱신 시각을 결정론적으로 제공한다.
    작성 날짜: 2026/08/20
    """

    def __init__(self, current_time: datetime) -> None:
        """
        함수 이름: __init__()
        기능: 최초 반환할 UTC 시각으로 clock을 초기화한다.
        인자: current_time -> 현재 snapshot 갱신에 사용할 UTC 시각
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self.current_time = current_time

    def __call__(self) -> datetime:
        """
        함수 이름: __call__()
        기능: 테스트가 지정한 현재 UTC 시각을 반환한다.
        인자: 없음
        반환값: 현재 snapshot 갱신 시각
        작성 날짜: 2026/08/20
        """
        return self.current_time


class FakeSubscription:
    """
    클래스 이름: FakeSubscription
    기능: 네트워크 없이 초기 Kline 구독의 close 여부를 기록한다.
    작성 날짜: 2026/08/20
    """

    def __init__(self, operation_trace: list[str]) -> None:
        """
        함수 이름: __init__()
        기능: 외부 호출 순서를 기록할 목록과 열린 상태를 보존한다.
        인자: operation_trace -> fake Gateway 호출 순서 목록
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self._operation_trace = operation_trace
        self.closed = False

    def close(self) -> None:
        """
        함수 이름: close()
        기능: fake 구독을 한 번만 닫고 호출 순서를 기록한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        if self.closed:
            return

        self.closed = True
        self._operation_trace.append("subscription.close")


class FakeAPIGateway:
    """
    클래스 이름: FakeAPIGateway
    기능: golden Kline을 메모리에서 반환하고 Binance REST 접근을 대체한다.
    작성 날짜: 2026/08/20
    """

    def __init__(
        self,
        klines_by_interval: Mapping[Interval, tuple[Kline, ...]],
        operation_trace: list[str],
    ) -> None:
        """
        함수 이름: __init__()
        기능: 초기화에 반환할 Kline과 호출 순서 목록을 보존한다.
        인자: klines_by_interval -> 주기별 golden Kline
            operation_trace -> fake Gateway 호출 순서 목록
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self._klines_by_interval = dict(klines_by_interval)
        self._operation_trace = operation_trace
        self.requested_symbol: str | None = None
        self.requested_limit: int | None = None

    def load_all_klines(
        self,
        symbol: str,
        limit: int,
    ) -> dict[Interval, tuple[Kline, ...]]:
        """
        함수 이름: load_all_klines()
        기능: 네 주기의 정규화된 golden Kline을 복사해 반환한다.
        인자: symbol -> 초기화 대상 거래 symbol
            limit -> 주기별 요청 Kline 상한
        반환값: 주기별 Kline tuple mapping
        작성 날짜: 2026/08/20
        """
        self._operation_trace.append("api_gateway.load_all_klines")
        self.requested_symbol = symbol
        self.requested_limit = limit
        return {
            interval: tuple(klines)
            for interval, klines in self._klines_by_interval.items()
        }

    def set_klines_for_next_load(
        self,
        klines_by_interval: Mapping[Interval, tuple[Kline, ...]],
    ) -> None:
        """
        함수 이름: set_klines_for_next_load()
        기능: 다음 full-resync REST 조회에서 반환할 전체 Kline을 교체한다.
        인자: klines_by_interval -> 다음 조회용 주기별 Kline
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self._klines_by_interval = dict(klines_by_interval)


class FakeWebSocketGateway:
    """
    클래스 이름: FakeWebSocketGateway
    기능: 빈 초기 buffer와 fake 구독으로 Binance WebSocket 접근을 대체한다.
    작성 날짜: 2026/08/20
    """

    def __init__(self, operation_trace: list[str]) -> None:
        """
        함수 이름: __init__()
        기능: 호출 순서 목록과 아직 없는 구독 handle을 초기화한다.
        인자: operation_trace -> fake Gateway 호출 순서 목록
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self._operation_trace = operation_trace
        self.latest_subscription: FakeSubscription | None = None
        self.requested_symbol: str | None = None
        self.requested_intervals: tuple[Interval, ...] = ()

    def start_all_kline_buffering(
        self,
        symbol: str,
        intervals: tuple[Interval, ...],
    ) -> FakeSubscription:
        """
        함수 이름: start_all_kline_buffering()
        기능: REST 조회보다 먼저 fake Kline buffering 구독을 시작한다.
        인자: symbol -> 구독 대상 거래 symbol
            intervals -> 구독 대상 canonical 주기
        반환값: buffer drain과 close에 사용할 fake 구독
        작성 날짜: 2026/08/20
        """
        self._operation_trace.append(
            "web_socket_gateway.start_all_kline_buffering"
        )
        self.requested_symbol = symbol
        self.requested_intervals = tuple(intervals)
        self.latest_subscription = FakeSubscription(self._operation_trace)
        return self.latest_subscription

    def drain_kline_buffer(
        self,
        subscription: FakeSubscription,
    ) -> dict[Interval, tuple[Kline, ...]]:
        """
        함수 이름: drain_kline_buffer()
        기능: 현재 구독의 비어 있는 메모리 Kline buffer를 반환한다.
        인자: subscription -> start가 반환한 fake 구독
        반환값: 네 주기를 모두 포함한 빈 Kline tuple mapping
        작성 날짜: 2026/08/20
        """
        if subscription is not self.latest_subscription:
            raise ValueError("subscription must be the active fake handle")

        self._operation_trace.append("web_socket_gateway.drain_kline_buffer")
        return {
            interval: ()
            for interval in SUPPORTED_INTERVALS
        }


class RecordingRegimeSTM(RegimeSTM):
    """
    클래스 이름: RecordingRegimeSTM
    기능: 실제 RegimeSTM 동작을 유지하며 Controller의 두 microstep 입력을 기록한다.
    작성 날짜: 2026/08/20
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: INITIAL 상태의 실제 STM과 비어 있는 호출 기록을 생성한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        super().__init__()
        self.calls: list[
            tuple[RegimeEvent, RegimeEvaluationContext | None]
        ] = []

    def handle(
        self,
        event: RegimeEvent,
        context: RegimeEvaluationContext | None = None,
    ) -> RegimeSTMResult:
        """
        함수 이름: handle()
        기능: event와 Context를 기록한 뒤 실제 RegimeSTM transition을 실행한다.
        인자: event -> Controller가 전달한 평가 event
            context -> EVALUATION_READY에 전달한 same-version Context
        반환값: 실제 RegimeSTM의 transition 및 Action 결과
        작성 날짜: 2026/08/20
        """
        self.calls.append((event, context))
        return super().handle(event, context)


def _load_golden_vector() -> dict[str, Any]:
    """
    함수 이름: _load_golden_vector()
    기능: ADR-004에서 확정한 4시간봉 golden vector를 읽는다.
    인자: 없음
    반환값: golden 입력과 기대 결과 mapping
    작성 날짜: 2026/08/20
    """
    with GOLDEN_VECTOR_PATH.open(encoding="utf-8") as fixture_file:
        return json.load(fixture_file)


def _make_kline(
    interval: Interval,
    open_time: datetime,
    close_price: Decimal,
    *,
    high_price: Decimal | None = None,
    low_price: Decimal | None = None,
    closed: bool,
) -> Kline:
    """
    함수 이름: _make_kline()
    기능: 명시한 가격과 확정 상태를 가진 ETHUSDT Kline을 생성한다.
    인자: interval -> Kline의 canonical 시간 주기
        open_time -> 봉 시작 UTC 시각
        close_price -> 봉 종가와 기본 시가
        high_price -> 선택적 고가
        low_price -> 선택적 저가
        closed -> 확정봉 여부
    반환값: production MarketSnapshot에 전달할 불변 Kline
    작성 날짜: 2026/08/20
    """
    selected_high = (
        close_price + Decimal("1")
        if high_price is None
        else high_price
    )
    selected_low = (
        close_price - Decimal("1")
        if low_price is None
        else low_price
    )
    return Kline(
        symbol=SYMBOL,
        interval=interval,
        open_time=open_time,
        open=close_price,
        high=selected_high,
        low=selected_low,
        close=close_price,
        volume=Decimal("1"),
        closed=closed,
    )


def _make_non_four_hour_klines(
    snapshot_time: datetime,
) -> dict[Interval, tuple[Kline, ...]]:
    """
    함수 이름: _make_non_four_hour_klines()
    기능: snapshot 시각을 포함하는 1m, 30m, 1d 확정봉과 진행봉을 만든다.
    인자: snapshot_time -> 각 최신 진행봉이 포함해야 하는 UTC 시각
    반환값: FOUR_HOURS를 제외한 주기별 연속 Kline mapping
    작성 날짜: 2026/08/20
    """
    one_minute_open = snapshot_time.replace(second=0, microsecond=0)
    thirty_minute_open = snapshot_time.replace(
        minute=(snapshot_time.minute // 30) * 30,
        second=0,
        microsecond=0,
    )
    one_day_open = snapshot_time.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    current_open_by_interval = {
        Interval.ONE_MINUTE: one_minute_open,
        Interval.THIRTY_MINUTES: thirty_minute_open,
        Interval.ONE_DAY: one_day_open,
    }
    duration_by_interval = {
        Interval.ONE_MINUTE: timedelta(minutes=1),
        Interval.THIRTY_MINUTES: timedelta(minutes=30),
        Interval.ONE_DAY: timedelta(days=1),
    }
    return {
        interval: (
            _make_kline(
                interval,
                current_open - duration_by_interval[interval],
                Decimal("109"),
                closed=True,
            ),
            _make_kline(
                interval,
                current_open,
                Decimal("110"),
                closed=False,
            ),
        )
        for interval, current_open in current_open_by_interval.items()
    }


def _make_golden_market_klines(
    golden_vector: dict[str, Any],
) -> dict[Interval, tuple[Kline, ...]]:
    """
    함수 이름: _make_golden_market_klines()
    기능: golden 4시간봉과 같은 시점의 다른 세 주기를 하나의 mapping으로 만든다.
    인자: golden_vector -> ADR-004 입력 가격 목록
    반환값: 최초 MarketDataController 초기화용 Kline mapping
    작성 날짜: 2026/08/20
    """
    closed_values = golden_vector["closed"]
    close_prices = tuple(
        Decimal(value)
        for value in closed_values["close"]
    )
    high_prices = tuple(
        Decimal(value)
        for value in closed_values["high"]
    )
    low_prices = tuple(
        Decimal(value)
        for value in closed_values["low"]
    )
    first_closed_open = (
        CURRENT_FOUR_HOUR_OPEN
        - FOUR_HOUR_DURATION * len(close_prices)
    )
    closed_klines = tuple(
        _make_kline(
            Interval.FOUR_HOURS,
            first_closed_open + FOUR_HOUR_DURATION * candle_index,
            close_price,
            high_price=high_prices[candle_index],
            low_price=low_prices[candle_index],
            closed=True,
        )
        for candle_index, close_price in enumerate(close_prices)
    )
    open_kline = _make_kline(
        Interval.FOUR_HOURS,
        CURRENT_FOUR_HOUR_OPEN,
        Decimal(golden_vector["current_price"]),
        high_price=Decimal("120"),
        low_price=Decimal("100"),
        closed=False,
    )
    klines_by_interval = _make_non_four_hour_klines(
        INITIAL_SNAPSHOT_TIME
    )
    klines_by_interval[Interval.FOUR_HOURS] = (
        *closed_klines,
        open_kline,
    )
    return klines_by_interval


def _make_next_closed_candle_klines(
    current_klines: Mapping[Interval, tuple[Kline, ...]],
    snapshot_time: datetime,
    current_price: Decimal,
) -> dict[Interval, tuple[Kline, ...]]:
    """
    함수 이름: _make_next_closed_candle_klines()
    기능: 기존 진행 4H 봉을 확정하고 다음 진행봉을 추가한 새 전체 snapshot을 만든다.
    인자: current_klines -> 이전 MarketSnapshot의 주기별 Kline
        snapshot_time -> 새 진행봉을 포함하는 UTC 갱신 시각
        current_price -> 새 진행 4H 봉의 현재 종가
    반환값: 새 candle ID로 재평가할 주기별 Kline mapping
    작성 날짜: 2026/08/20
    """
    previous_four_hour_klines = current_klines[Interval.FOUR_HOURS]
    previous_open_kline = previous_four_hour_klines[-1]
    newly_closed_kline = replace(previous_open_kline, closed=True)
    next_open_kline = _make_kline(
        Interval.FOUR_HOURS,
        previous_open_kline.open_time + FOUR_HOUR_DURATION,
        current_price,
        high_price=current_price + Decimal("10"),
        low_price=current_price - Decimal("10"),
        closed=False,
    )
    next_klines = _make_non_four_hour_klines(snapshot_time)
    next_klines[Interval.FOUR_HOURS] = (
        *previous_four_hour_klines[:-1],
        newly_closed_kline,
        next_open_kline,
    )
    return next_klines


def _make_open_price_update_klines(
    current_klines: Mapping[Interval, tuple[Kline, ...]],
    snapshot_time: datetime,
    current_price: Decimal,
) -> dict[Interval, tuple[Kline, ...]]:
    """
    함수 이름: _make_open_price_update_klines()
    기능: 확정 4H candle ID는 유지하고 진행봉 가격만 바꾼 새 version을 만든다.
    인자: current_klines -> 직전 주기별 Kline mapping
        snapshot_time -> 새 MarketSnapshot 갱신 시각
        current_price -> 대체할 진행 4H 봉 종가
    반환값: 같은 최신 확정 candle ID를 가진 전체 Kline mapping
    작성 날짜: 2026/08/20
    """
    previous_four_hour_klines = current_klines[Interval.FOUR_HOURS]
    previous_open_kline = previous_four_hour_klines[-1]
    updated_open_kline = Kline(
        symbol=previous_open_kline.symbol,
        interval=previous_open_kline.interval,
        open_time=previous_open_kline.open_time,
        open=previous_open_kline.open,
        high=max(previous_open_kline.high, current_price),
        low=min(previous_open_kline.low, current_price),
        close=current_price,
        volume=previous_open_kline.volume,
        closed=False,
    )
    next_klines = _make_non_four_hour_klines(snapshot_time)
    next_klines[Interval.FOUR_HOURS] = (
        *previous_four_hour_klines[:-1],
        updated_open_kline,
    )
    return next_klines


def _make_insufficient_history_klines(
    current_klines: Mapping[Interval, tuple[Kline, ...]],
    snapshot_time: datetime,
) -> dict[Interval, tuple[Kline, ...]]:
    """
    함수 이름: _make_insufficient_history_klines()
    기능: 최신 candle ID를 유지하면서 확정 4H 이력을 열세 개로 줄인다.
    인자: current_klines -> 직전 주기별 Kline mapping
        snapshot_time -> 실패 평가에 사용할 UTC 갱신 시각
    반환값: 지표 최소 입력을 만족하지 않는 전체 Kline mapping
    작성 날짜: 2026/08/20
    """
    next_klines = _make_non_four_hour_klines(snapshot_time)
    next_klines[Interval.FOUR_HOURS] = current_klines[
        Interval.FOUR_HOURS
    ][-14:]
    return next_klines


class RegimeEvaluationFlowTests(unittest.TestCase):
    """
    클래스 이름: RegimeEvaluationFlowTests
    기능: 시장 초기화부터 REGIME 추천, 재평가와 fail-closed 유지까지 검증한다.
    작성 날짜: 2026/08/20
    """

    def setUp(self) -> None:
        """
        함수 이름: setUp()
        기능: fake Gateway와 실제 MarketSnapshot, Controller, RegimeSTM을 조립한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self.golden_vector = _load_golden_vector()
        self.initial_klines = _make_golden_market_klines(
            self.golden_vector
        )
        self.operation_trace: list[str] = []
        self.snapshot_clock = MutableClock(INITIAL_SNAPSHOT_TIME)
        self.api_gateway = FakeAPIGateway(
            self.initial_klines,
            self.operation_trace,
        )
        self.web_socket_gateway = FakeWebSocketGateway(
            self.operation_trace
        )
        self.market_snapshot = MarketSnapshot(clock=self.snapshot_clock)
        self.regime_stm = RecordingRegimeSTM()
        self.regime_controller = RegimeController(
            regime_stm=self.regime_stm,
            market_snapshot=self.market_snapshot,
        )
        self.market_data_controller = MarketDataController(
            api_gateway=self.api_gateway,
            web_socket_gateway=self.web_socket_gateway,
            market_snapshot=self.market_snapshot,
            regime_controller=self.regime_controller,
        )

    def _initialize_golden_flow(self) -> None:
        """
        함수 이름: _initialize_golden_flow()
        기능: fake Gateway를 통한 production 시장 초기화와 최초 추천을 실행한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        initialized_snapshot = (
            self.market_data_controller.initialize_market_data()
        )
        self.assertIs(initialized_snapshot, self.market_snapshot)

    def _assert_communication_trace(
        self,
        evaluation_trace: RegimeEvaluationTrace,
    ) -> None:
        """
        함수 이름: _assert_communication_trace()
        기능: 메시지 번호와 각 caller, receiver가 성공 trace에 모두 기록됐는지 검증한다.
        인자: evaluation_trace -> 성공한 한 REGIME 평가 trace
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self.assertEqual(
            evaluation_trace.communication_steps,
            EXPECTED_COMMUNICATION_STEPS,
        )
        required_message_participants = (
            ("1.4:", "MarketDataController->RegimeController"),
            ("1.4.1:", "RegimeController->IndicatorSnapshot"),
            ("1.5:", "MarketDataController->RegimeController"),
            ("1.5.1:", "RegimeController->RegimeSTM"),
        )
        for message_prefix, participants in required_message_participants:
            matching_steps = tuple(
                communication_step
                for communication_step in evaluation_trace.communication_steps
                if communication_step.startswith(message_prefix)
            )
            with self.subTest(message_prefix=message_prefix):
                self.assertTrue(matching_steps)
                self.assertTrue(
                    all(
                        participants in communication_step
                        for communication_step in matching_steps
                    )
                )

    def test_regime_evaluation_flow_initializes_golden_type_2(self) -> None:
        """
        함수 이름: test_regime_evaluation_flow_initializes_golden_type_2()
        기능: initialize_market_data에서 golden 지표와 EA-001, EA-005 추천을 완성한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self._initialize_golden_flow()

        self.assertEqual(
            self.operation_trace,
            [
                "web_socket_gateway.start_all_kline_buffering",
                "api_gateway.load_all_klines",
                "web_socket_gateway.drain_kline_buffer",
                "subscription.close",
            ],
        )
        self.assertEqual(self.api_gateway.requested_symbol, SYMBOL)
        self.assertEqual(self.api_gateway.requested_limit, 500)
        self.assertEqual(self.web_socket_gateway.requested_symbol, SYMBOL)
        self.assertEqual(
            self.web_socket_gateway.requested_intervals,
            SUPPORTED_INTERVALS,
        )
        self.assertIsNotNone(self.web_socket_gateway.latest_subscription)
        self.assertTrue(self.web_socket_gateway.latest_subscription.closed)
        self.assertTrue(self.market_snapshot.ready)
        self.assertEqual(self.market_snapshot.version, 1)

        indicator_snapshot = self.regime_controller.indicator_snapshot
        self.assertIsNotNone(indicator_snapshot)
        expected_indicators = self.golden_vector["expected"]
        self.assertEqual(
            indicator_snapshot.ema9_series,
            tuple(
                Decimal(value)
                for value in expected_indicators["ema9_series"]
            ),
        )
        self.assertEqual(
            indicator_snapshot.ema9_slope,
            Decimal(expected_indicators["ema9_slope"]),
        )
        self.assertEqual(
            indicator_snapshot.live_ema9,
            Decimal(expected_indicators["live_ema9"]),
        )
        self.assertEqual(
            indicator_snapshot.source_market_version,
            self.market_snapshot.version,
        )

        regime_result = self.regime_controller.last_regime_result
        self.assertIsNotNone(regime_result)
        self.assertIs(regime_result.recommended_type, RegimeType.TYPE_2)
        self.assertEqual(regime_result.transition_id, "EA-005")
        self.assertIs(
            self.regime_controller.recommended_regime,
            RegimeType.TYPE_2,
        )
        self.assertIsNone(self.regime_controller.selected_regime)
        self.assertIs(
            self.regime_stm.current_state,
            RegimeState.TYPE_2_RECOMMENDED,
        )

        evaluation_trace = self.regime_controller.evaluation_traces[-1]
        self._assert_communication_trace(evaluation_trace)
        self.assertEqual(
            evaluation_trace.transition_ids,
            ("EA-001", "EA-005"),
        )
        self.assertEqual(
            evaluation_trace.source_market_version,
            self.market_snapshot.version,
        )
        self.assertEqual(
            evaluation_trace.source_candle_id,
            indicator_snapshot.source_candle_id,
        )
        self.assertEqual(
            evaluation_trace.evaluation_id,
            regime_result.evaluation_id,
        )
        self.assertIn(":INITIAL:1:", evaluation_trace.evaluation_id)
        self.assertEqual(
            evaluation_trace.event_ids,
            (
                (
                    f"{evaluation_trace.evaluation_id}:"
                    "INITIAL_EVALUATION_REQUESTED"
                ),
                f"{evaluation_trace.evaluation_id}:EVALUATION_READY",
            ),
        )

        self.assertEqual(len(self.regime_stm.calls), 2)
        initial_event, initial_context = self.regime_stm.calls[0]
        ready_event, ready_context = self.regime_stm.calls[1]
        self.assertIs(
            initial_event.event_type,
            RegimeEventType.INITIAL_EVALUATION_REQUESTED,
        )
        self.assertIsNone(initial_context)
        self.assertIs(
            ready_event.event_type,
            RegimeEventType.EVALUATION_READY,
        )
        self.assertIsNotNone(ready_context)
        self.assertEqual(
            ready_context.evaluation_id,
            evaluation_trace.evaluation_id,
        )
        self.assertEqual(
            ready_context.source_candle_id,
            evaluation_trace.source_candle_id,
        )
        self.assertEqual(
            ready_context.source_market_version,
            evaluation_trace.source_market_version,
        )

    def test_regime_evaluation_flow_re_evaluates_and_fails_closed(self) -> None:
        """
        함수 이름: test_regime_evaluation_flow_re_evaluates_and_fails_closed()
        기능: 새 4H 마감은 EA-103으로 재평가하고 stale, duplicate, 실패는 정상 추천을 유지한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        self._initialize_golden_flow()
        next_klines = _make_next_closed_candle_klines(
            self.market_snapshot.klines_by_interval,
            NEXT_SNAPSHOT_TIME,
            Decimal("111"),
        )
        self.snapshot_clock.current_time = NEXT_SNAPSHOT_TIME
        self.api_gateway.set_klines_for_next_load(next_klines)
        returned_snapshot = self.market_data_controller.initialize_market_data(
            SYMBOL
        )
        reevaluation_result = self.regime_controller.last_regime_result

        self.assertIs(returned_snapshot, self.market_snapshot)
        self.assertIsNotNone(reevaluation_result)
        self.assertEqual(self.market_snapshot.version, 2)
        self.assertIs(
            reevaluation_result.recommended_type,
            RegimeType.TYPE_2,
        )
        self.assertIs(
            reevaluation_result.previous_recommended_type,
            RegimeType.TYPE_2,
        )
        self.assertFalse(reevaluation_result.changed)
        self.assertEqual(reevaluation_result.transition_id, "EA-005")
        reevaluation_trace = self.regime_controller.evaluation_traces[-1]
        self._assert_communication_trace(reevaluation_trace)
        self.assertEqual(
            reevaluation_trace.transition_ids,
            ("EA-103", "EA-005"),
        )
        self.assertEqual(reevaluation_trace.source_market_version, 2)
        self.assertEqual(
            reevaluation_trace.source_candle_id,
            reevaluation_result.source_candle_id,
        )
        self.assertIn(
            ":FOUR_HOUR_CANDLE_CLOSE:2:",
            reevaluation_trace.evaluation_id,
        )
        self.assertEqual(len(self.regime_stm.calls), 4)
        candle_event, candle_context = self.regime_stm.calls[2]
        ready_event, ready_context = self.regime_stm.calls[3]
        self.assertIs(
            candle_event.event_type,
            RegimeEventType.FOUR_HOUR_CANDLE_CLOSED,
        )
        self.assertIsNone(candle_context)
        self.assertIs(
            ready_event.event_type,
            RegimeEventType.EVALUATION_READY,
        )
        self.assertIsNotNone(ready_context)
        self.assertEqual(
            ready_context.evaluation_id,
            reevaluation_trace.evaluation_id,
        )
        self.assertEqual(ready_context.source_market_version, 2)

        successful_indicator = self.regime_controller.indicator_snapshot
        successful_result = self.regime_controller.last_regime_result
        successful_candle_id = reevaluation_result.source_candle_id
        open_price_update_time = NEXT_SNAPSHOT_TIME + timedelta(minutes=1)
        open_price_update_klines = _make_open_price_update_klines(
            next_klines,
            open_price_update_time,
            Decimal("112"),
        )
        self.snapshot_clock.current_time = open_price_update_time
        self.market_snapshot.update(open_price_update_klines)

        with self.assertRaises(RegimeEvaluationError) as stale_context:
            self.regime_controller.recommend_regime(successful_indicator)

        self.assertIs(
            stale_context.exception.failure_code,
            RegimeEvaluationFailureCode.STALE_MARKET_SNAPSHOT,
        )
        stale_trace = self.regime_controller.evaluation_traces[-1]
        self.assertIs(
            stale_trace.failure_code,
            RegimeEvaluationFailureCode.STALE_MARKET_SNAPSHOT,
        )
        self.assertEqual(stale_trace.source_market_version, 2)
        self.assertEqual(stale_trace.source_candle_id, successful_candle_id)

        duplicate_result = self.regime_controller.evaluate_regime(
            RegimeEvaluationTrigger.FOUR_HOUR_CANDLE_CLOSE,
            self.market_snapshot,
        )
        self.assertIsNone(duplicate_result)
        duplicate_trace = self.regime_controller.evaluation_traces[-1]
        self.assertIs(
            duplicate_trace.failure_code,
            RegimeEvaluationFailureCode.DUPLICATE_FOUR_HOUR_CANDLE,
        )
        self.assertEqual(duplicate_trace.source_market_version, 3)
        self.assertEqual(
            duplicate_trace.source_candle_id,
            successful_candle_id,
        )

        insufficient_time = NEXT_SNAPSHOT_TIME + timedelta(minutes=2)
        insufficient_klines = _make_insufficient_history_klines(
            open_price_update_klines,
            insufficient_time,
        )
        self.snapshot_clock.current_time = insufficient_time
        self.market_snapshot.update(insufficient_klines)
        failure_result = self.regime_controller.evaluate_regime(
            RegimeEvaluationTrigger.FOUR_HOUR_CANDLE_CLOSE,
            self.market_snapshot,
        )

        self.assertIsNone(failure_result)
        failure_trace = self.regime_controller.evaluation_traces[-1]
        self.assertIs(
            failure_trace.failure_code,
            RegimeEvaluationFailureCode.INSUFFICIENT_CLOSED_KLINES,
        )
        self.assertEqual(failure_trace.source_market_version, 4)
        self.assertIs(
            self.regime_controller.last_regime_result,
            successful_result,
        )
        self.assertIs(
            self.regime_controller.recommended_regime,
            RegimeType.TYPE_2,
        )
        self.assertIsNone(self.regime_controller.selected_regime)
        self.assertIs(
            self.regime_stm.current_state,
            RegimeState.TYPE_2_RECOMMENDED,
        )
        self.assertEqual(len(self.regime_stm.calls), 4)
        self.assertEqual(
            tuple(
                trace.failure_code
                for trace in self.regime_controller.evaluation_traces[-3:]
            ),
            (
                RegimeEvaluationFailureCode.STALE_MARKET_SNAPSHOT,
                RegimeEvaluationFailureCode.DUPLICATE_FOUR_HOUR_CANDLE,
                RegimeEvaluationFailureCode.INSUFFICIENT_CLOSED_KLINES,
            ),
        )


if __name__ == "__main__":
    unittest.main()
