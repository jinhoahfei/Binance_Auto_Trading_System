"""거래소 시각 오차와 체결 전 queue 입력이 보유시간·실시간 게시를 중단하지 않는지 검증한다."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from binance_auto_trader.application.trading_controller import TradingSessionStatus
from binance_auto_trader.bootstrap.application import _TradingEventRuntimeWorker
from binance_auto_trader.domain.trading.action_requests import patch as runtime_patch
from binance_auto_trader.domain.trading.conditions import evaluate_condition
from binance_auto_trader.domain.trading.context import MarketEvaluationSnapshot
from binance_auto_trader.domain.trading.states import CaseCPositionState, StrategyType
from binance_auto_trader.transport import BackendEventStream, create_trading_session_update_observer
from tests.integration.active_trading_logic_replay import replay_market_evaluation
from tests.integration.test_buy_sell_flow import FakeOrderScenario, _create_buy_flow_fixture


class PostBuyIndicatorTests(unittest.TestCase):
    """
    클래스 이름: PostBuyIndicatorTests
    기능: 실제 매수·체결 결과·worker·지표 게시 경로에서 시각 오차와 backlog를 재현한다.
    작성 날짜: 2026/09/10
    """

    def setUp(self) -> None:
        """
        함수 이름: setUp()
        기능: 외부 연결을 차단하고 가짜 REST·임시 이력의 Case C flush까지 준비한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        self.enterContext(patch("socket.create_connection", side_effect=AssertionError("external network forbidden")))
        directory = self.enterContext(TemporaryDirectory())
        self.fixture = _create_buy_flow_fixture(directory, FakeOrderScenario.IMMEDIATE_FILLED)
        self.controller = self.fixture.controller
        self.addCleanup(self.controller.close_session_resources)
        self.market = MarketEvaluationSnapshot(
            realtime_price=Decimal("2439.98"), lower_band=Decimal("2452.17"), upper_band=Decimal("2489"),
            realtime_pct_b=Decimal("-0.3236635529213372712908670964037950"),
            current_30m_candle_id="ETHUSDT:30m:post-buy", touch_candle_bbw=Decimal("0.01222"),
            cci_30m_realtime=Decimal("-210"),
        )
        replay_market_evaluation(self.fixture, self.market, "initial-flush")

    def _enqueue_market(self, market: MarketEvaluationSnapshot) -> None:
        """
        함수 이름: _enqueue_market()
        기능: 실제 관측 시각과 source version을 보존한 입력을 처리 없이 queue에 넣는다.
        인자: market -> 해당 관측의 지표 값
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        snapshot = self.controller._market_snapshot
        snapshot.update(snapshot.klines_by_interval)
        source_id = f"post-buy-{snapshot.version}"
        for message_id, event_type in (("1L.1", "KLINE_OBSERVED"), ("1L.2", "MARKET_EVALUATED")):
            self.controller.observe_public_market_boundary(
                message_id=message_id, event_type=event_type,
                source_event_id=source_id, market_version=snapshot.version,
            )
        self.controller.observe_market_evaluation(market, source_event_id=source_id, market_version=snapshot.version)

    def _prepare_buy(self, offset: timedelta) -> MarketEvaluationSnapshot:
        """
        함수 이름: _prepare_buy()
        기능: 거래소의 체결·응답 시각만 로컬 clock보다 앞서게 하고 매수 조건 입력을 준비한다.
        인자: offset -> 실제 거래소 시각과 로컬 시각의 차이
        반환값: 회복 매수를 충족하는 시장 입력
        작성 날짜: 2026/09/10
        """
        self.fixture.clock.advance(timedelta(seconds=1))
        self.fixture.rest_client.fill_time_origin = self.fixture.clock() + offset
        self.fixture.rest_client.clock = lambda: self.fixture.clock() + offset
        return replace(self.market, realtime_price=Decimal("2444.98"), realtime_pct_b=Decimal("-0.2618221034394996491882431970337721"))

    def test_worker_keeps_publishing_holding_indicators_after_future_dated_fill(self) -> None:
        """
        함수 이름: test_worker_keeps_publishing_holding_indicators_after_future_dated_fill()
        기능: 실제 로그의 29ms 오차로 체결해도 worker가 살아 있고 새 시세·시간을 계속 게시한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        entry = self._prepare_buy(timedelta(milliseconds=29))
        self._enqueue_market(entry)
        stream = BackendEventStream(clock=self.fixture.clock)
        observer = create_trading_session_update_observer(stream)
        published = []
        published_event = Event()

        def publish_state() -> object:
            """
            함수 이름: publish_state()
            기능: 실제 worker의 transport 게시 결과를 수집하고 검사 대기를 해제한다.
            인자: 없음
            반환값: 실제 BackendEventEnvelope
            작성 날짜: 2026/09/10
            """
            event = observer(self.controller, "fake")
            published.append(event)
            published_event.set()
            return event

        worker = _TradingEventRuntimeWorker(
            self.controller.run_event_runtime_cycle, self.controller.mark_event_runtime_failed,
            lambda: True, self.controller.snapshot_session, self.controller._session_lock,
            state_update_observer=publish_state, poll_interval_seconds=0.01,
        )
        self.addCleanup(worker.close)
        worker.start()
        self.assertTrue(published_event.wait(2))
        self.assertIs(self.controller.status, TradingSessionStatus.RUNNING)
        self.assertIs(self.fixture.position.owner, StrategyType.CASE_C)
        self.assertIs(self.controller._active_stm.current_state.case_c_position_state, CaseCPositionState.CASE_C_HOLDING)
        entered_at = self.fixture.position.entered_at
        self.assertEqual(entered_at, self.fixture.clock() + timedelta(milliseconds=29))
        self.assertEqual(self.controller.context.market.holding_elapsed, timedelta(0))
        self.assertEqual(self.fixture.history_controller.trade_history.trades[0].executed_at, entered_at)

        # 거래소 시각을 따라잡기 전에도 실시간 값은 갱신되고, 따라잡은 뒤 실제 보유시간이 증가한다.
        previous_sequence = published[-1].sequence
        for delay, pct_b, slope, elapsed in (
            (timedelta(milliseconds=10), "-0.20", "-0.10", timedelta(0)),
            (timedelta(milliseconds=19), "-0.10", "-0.20", timedelta(0)),
            (timedelta(seconds=2), "0.07", "-0.30", timedelta(seconds=2)),
            (timedelta(seconds=2), "0.09", "-0.40", timedelta(seconds=4)),
        ):
            with self.controller._session_lock:
                published_event.clear()
                self.fixture.clock.advance(delay)
                self._enqueue_market(replace(entry, realtime_pct_b=Decimal(pct_b), realtime_ema_slope=Decimal(slope)))
                worker.request_processing()
            self.assertTrue(published_event.wait(2))
            with self.controller._session_lock:
                event = published[-1]
                self.assertGreater(event.sequence, previous_sequence)
                previous_sequence = event.sequence
                self.assertIs(self.controller.status, TradingSessionStatus.RUNNING)
                self.assertEqual(self.controller.context.market.holding_elapsed, elapsed)
                self.assertEqual(self.fixture.position.entered_at, entered_at)
                rows = {row["condition_id"]: row for row in event.payload["trading"]["active_logic"]["indicators"]["conditions"]}
                self.assertEqual(Decimal(rows["c_profit_zone"]["value"]), Decimal(pct_b))
                self.assertEqual(Decimal(rows["c_stop"]["value"]), Decimal(slope))
                self.assertEqual(Decimal(rows["c_time_exit"]["timer"]["remaining_seconds"]), Decimal(3600 - elapsed.total_seconds()))
                self.assertEqual(len(self.fixture.rest_client.submitted_orders), 1)

    def test_queued_market_before_fill_uses_zero_holding_age_and_keeps_source_value(self) -> None:
        """
        함수 이름: test_queued_market_before_fill_uses_zero_holding_age_and_keeps_source_value()
        기능: 체결보다 먼저 적재한 market을 체결 뒤 처리해도 원본 지표를 유지하고 경과 0으로 평가한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        entry = self._prepare_buy(timedelta(milliseconds=100))
        entered_at = self.fixture.rest_client.fill_time_origin
        self._enqueue_market(entry)
        self.fixture.clock.advance(timedelta(milliseconds=20))
        self._enqueue_market(replace(entry, realtime_pct_b=Decimal("0.07")))
        self.fixture.clock.advance(timedelta(milliseconds=180))
        asyncio.run(self.controller.run_event_runtime_cycle())
        self.assertIs(self.controller.status, TradingSessionStatus.RUNNING)
        self.assertEqual(self.fixture.position.entered_at, entered_at)
        self.assertEqual(self.controller.context.market.realtime_pct_b, Decimal("0.07"))
        self.assertEqual(self.controller.context.market.holding_elapsed, timedelta(0))
        self._enqueue_market(replace(entry, realtime_pct_b=Decimal("0.09")))
        asyncio.run(self.controller.run_event_runtime_cycle())
        self.assertEqual(self.controller.context.market.holding_elapsed, timedelta(milliseconds=100))
        self.assertEqual(len(self.fixture.rest_client.submitted_orders), 1)

    def test_time_exit_keeps_exchange_fill_origin_and_exact_boundary(self) -> None:
        """
        함수 이름: test_time_exit_keeps_exchange_fill_origin_and_exact_boundary()
        기능: 초기 0초 처리가 원 체결시각이나 C 60분·B 6시간 청산 경계를 당기지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        entry = self._prepare_buy(timedelta(milliseconds=29))
        self._enqueue_market(entry)
        asyncio.run(self.controller.run_event_runtime_cycle())
        entered_at = self.fixture.position.entered_at
        for condition_id, duration in (("c_time_exit", timedelta(hours=1)), ("b_time_exit", timedelta(hours=6))):
            for offset, expected in ((timedelta(microseconds=-1), False), (timedelta(0), True), (timedelta(microseconds=1), True)):
                enriched = self.controller._enrich_market_evaluation_elapsed(entry, entered_at + duration + offset)
                context = replace(self.controller.context, market=enriched)
                self.assertEqual(evaluate_condition(condition_id, context).satisfied, expected)
                self.assertEqual(enriched.holding_elapsed, duration + offset)
        self.assertEqual(self.fixture.position.entered_at, entered_at)

    def test_future_runtime_timer_is_still_rejected(self) -> None:
        """
        함수 이름: test_future_runtime_timer_is_still_rejected()
        기능: 거래소 체결시각 보정이 동일 clock의 signal·회복 기준 오류까지 무시하지 않게 한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        for field_name in ("signal_time", "timer_base_time"):
            with self.subTest(field_name=field_name):
                original = getattr(self.controller.context.runtime, field_name)
                self.controller._context.apply_runtime_patch(runtime_patch(**{field_name: self.fixture.clock() + timedelta(seconds=1)}))
                with self.assertRaisesRegex(ValueError, field_name):
                    self.controller._enrich_market_evaluation_elapsed(self.market, self.fixture.clock())
                self.controller._context.apply_runtime_patch(runtime_patch(**{field_name: original}))
