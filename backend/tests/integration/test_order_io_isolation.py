"""느린 주문·복구 I/O와 관측·정지·계좌 결과 사이의 직렬화 경계를 검증한다."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, RLock, Thread
import unittest
from unittest.mock import patch

from binance_auto_trader.application.runtime_diagnostics import RuntimeDiagnostics
from binance_auto_trader.bootstrap.application import _TradingEventRuntimeWorker
from binance_auto_trader.domain.common import RegimeType
from tests.integration.test_order_reconciliation_flow import _submit_case_b_buy
from tests.integration.test_testnet_restart_reconciliation_flow import (
    _create_recovery_controller, _FilledSubmissionTestnetRESTClient,
)


class OrderIoIsolationTests(unittest.TestCase):
    """
    클래스 이름: OrderIoIsolationTests
    기능: 외부 작업이 멈춰도 관측은 응답하고 주문·정지는 단일 owner로 유지되는지 검증한다.
    작성 날짜: 2026/09/22
    """

    def setUp(self):
        """
        함수 이름: setUp()
        기능: 외부 socket을 차단하고 실제 저널을 쓰는 가짜 거래 세션을 준비한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        self.enterContext(patch("socket.create_connection", side_effect=AssertionError("network forbidden")))
        directory = self.enterContext(TemporaryDirectory())
        self.application_lock = RLock()
        self.client = _FilledSubmissionTestnetRESTClient()
        self.controller, self.history, self.position = _create_recovery_controller(
            Path(directory) / "history.jsonl", self.client, command_gate=True,
            application_lock=self.application_lock,
        )
        self.addCleanup(self.controller.close_session_resources)
        self.controller.reconcile_startup_state()
        selection = self.controller.commit_regime_selection(
            RegimeType.TYPE_0, self.controller.fetch_selected_trading_logic(RegimeType.TYPE_0),
            command_id="io-select", expected_version=0,
        )
        self.controller.start_trading(command_id="io-start", expected_version=selection.version)

    def test_runtime_order_and_log_delays_release_all_application_lock_depths(self):
        """
        함수 이름: test_runtime_order_and_log_delays_release_all_application_lock_depths()
        기능: 실제 worker의 주문과 진단 파일이 함께 지연돼도 상태·시장·heartbeat 관측이 완료되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        entered, release, completed, observed = Event(), Event(), Event(), Event()
        sink_entered = Event()
        original_prepare = self.controller._api_gateway.prepare_order
        processed = False

        def stalled_sink(record):
            """
            함수 이름: stalled_sink()
            기능: 느린 파일 기록을 주문 지연과 동시에 주입한다.
            인자: record -> 비동기 writer가 전달한 안전한 진단 record
            반환값: 없음
            작성 날짜: 2026/09/27
            """
            sink_entered.set()
            if not release.wait(3):
                raise TimeoutError("test sink release was not signaled")

        diagnostics = RuntimeDiagnostics(stalled_sink, asynchronous=True, queue_capacity=4)
        self.addCleanup(diagnostics.close)
        self.controller._diagnostics = diagnostics
        diagnostics.record("blocked_sink")
        self.assertTrue(sink_entered.wait(1))

        def prepare(order):
            """
            함수 이름: prepare()
            기능: 외부 주문 준비를 barrier에서 대기시킨다.
            인자: order -> 준비할 가짜 주문
            반환값: 정상 준비 결과
            작성 날짜: 2026/09/22
            """
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test release was not signaled")
            return original_prepare(order)

        async def cycle():
            """
            함수 이름: cycle()
            기능: production effect 예약 아래 정확히 한 번 가짜 BUY를 수행한다.
            인자: 없음
            반환값: 실행 결과 tuple
            작성 날짜: 2026/09/22
            """
            nonlocal processed
            if processed:
                return ()
            processed = True
            with self.controller._session_effect_lock():
                result = _submit_case_b_buy(self.controller)
            completed.set()
            return result

        def observe():
            """
            함수 이름: observe()
            기능: I/O owner와 다른 thread에서 일관된 세션 조회와 시장 생존 관측을 수행한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/09/22
            """
            self.controller.snapshot_session()
            self.controller.note_market_input()
            diagnostics.record("ui_stream_heartbeat")
            self.assertLessEqual(diagnostics.queue_snapshot()["queued_records"], 4)
            observed.set()

        worker = _TradingEventRuntimeWorker(
            cycle, self.controller.mark_event_runtime_failed, lambda: True,
            self.controller.snapshot_session, self.application_lock,
            poll_interval_seconds=0.01,
            diagnostics=diagnostics,
        )
        with patch.object(type(self.controller._api_gateway), "prepare_order", side_effect=prepare):
            worker.start()
            worker.request_processing()
            reader = Thread(target=observe)
            try:
                self.assertTrue(entered.wait(1))
                reader.start()
                self.assertTrue(observed.wait(1), "observation was blocked by external I/O")
                self.assertFalse(completed.is_set())
                self.assertEqual(self.client.submit_count, 0)
            finally:
                release.set()
                reader.join(2)
                worker.close()
        self.assertFalse(worker.failed)
        self.assertTrue(completed.is_set())
        self.assertEqual(self.client.submit_count, 1)
        self.assertEqual(len(self.history.trade_history.trades), 1)
        self.assertGreater(diagnostics.queue_snapshot()["overflow_count"], 0)

    def test_stop_during_preparation_prevents_post_and_preserves_owner(self):
        """
        함수 이름: test_stop_during_preparation_prevents_post_and_preserves_owner()
        기능: 대기 중 접수한 STOP이 준비 후 POST를 막고 같은 세션에서 직렬 종료되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        entered, release, stopped = Event(), Event(), Event()
        errors = []
        original_prepare = self.controller._api_gateway.prepare_order

        def prepare(order):
            """
            함수 이름: prepare()
            기능: 정지 요청이 도착할 때까지 준비 결과 반환을 늦춘다.
            인자: order -> 제출 전 주문
            반환값: 가짜 준비 결과
            작성 날짜: 2026/09/22
            """
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test release was not signaled")
            return original_prepare(order)

        def buy():
            """
            함수 이름: buy()
            기능: 실제 Controller 직렬 owner 아래 BUY와 후속 outcome을 실행한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/09/22
            """
            try:
                with self.application_lock, self.controller._session_effect_lock():
                    outcomes = _submit_case_b_buy(self.controller)
                    self.controller._enqueue_order_outcomes(outcomes)
                    asyncio.run(self.controller.drain_events())
            except BaseException as error:
                errors.append(error)

        def stop():
            """
            함수 이름: stop()
            기능: 다른 thread에서 public STOP을 요청하고 같은 세션의 완료를 기다린다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/09/22
            """
            try:
                with self.application_lock:
                    self.controller.stop_trading(
                        command_id="io-stop", expected_version=self.controller.context.version,
                    )
                stopped.set()
            except BaseException as error:
                errors.append(error)

        with patch.object(type(self.controller._api_gateway), "prepare_order", side_effect=prepare):
            buyer, stopper = Thread(target=buy), Thread(target=stop)
            buyer.start()
            try:
                self.assertTrue(entered.wait(1))
                stopper.start()
                self.assertTrue(self.controller._external_stop_requested.wait(1))
                self.assertTrue(self.application_lock.acquire(timeout=1))
                self.application_lock.release()
                self.assertFalse(stopped.is_set())
            finally:
                release.set()
                buyer.join(3)
                stopper.join(3)
        self.assertEqual(errors, [])
        self.assertTrue(stopped.is_set())
        self.assertEqual(self.client.submit_count, 0)
        self.assertEqual(self.history.get_pending_orders(), ())

    def test_recovery_fetch_yields_observation_without_parallel_effect(self):
        """
        함수 이름: test_recovery_fetch_yields_observation_without_parallel_effect()
        기능: 복구 조회 중 읽기는 진행하지만 두 번째 effect는 첫 owner를 기다리는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        entered, release, second_entered = Event(), Event(), Event()
        errors = []

        def fetch():
            """
            함수 이름: fetch()
            기능: 외부 계좌 조회의 결정적 지연을 주입한다.
            인자: 없음
            반환값: 조회 marker
            작성 날짜: 2026/09/22
            """
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test release was not signaled")
            return "account-snapshot"

        def first():
            """
            함수 이름: first()
            기능: 바깥 application lock이 있는 복구 effect를 실행한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/09/22
            """
            try:
                with self.application_lock, self.controller._session_effect_lock():
                    self.controller._run_external_operation(fetch)
            except BaseException as error:
                errors.append(error)

        def second():
            """
            함수 이름: second()
            기능: 다른 effect가 owner를 추월할 수 없는지 관측한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/09/22
            """
            with self.application_lock, self.controller._session_effect_lock():
                second_entered.set()

        first_thread, second_thread = Thread(target=first), Thread(target=second)
        first_thread.start()
        try:
            self.assertTrue(entered.wait(1))
            second_thread.start()
            self.controller.snapshot_session()
            self.controller.note_market_input()
            self.assertFalse(second_entered.wait(0.05))
        finally:
            release.set()
            first_thread.join(3)
            second_thread.join(3)
        self.assertEqual(errors, [])
        self.assertTrue(second_entered.is_set())
        self.assertIsNone(self.controller._effect_owner)
