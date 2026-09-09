"""실제 Controller·STM·주문·worker를 실행하고 파일만 읽어 거래 흐름을 검증한다."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from functools import partial
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
import unittest
from unittest.mock import Mock, patch

from binance_auto_trader.adapters.filesystem.diagnostic_log_writer import DiagnosticLogWriter
from binance_auto_trader.application.runtime_diagnostics import RuntimeDiagnostics
from binance_auto_trader.application.trading_controller import TradingController
from binance_auto_trader.bootstrap import close_application, create_application_runtime, start_application
from binance_auto_trader.bootstrap.application import _TradingEventRuntimeWorker
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.trading.context import MarketEvaluationSnapshot
from tests.integration.active_trading_logic_replay import replay_entry_scenario, replay_market_evaluation, replay_recovery_timer_scenario
from tests.integration.test_buy_sell_flow import FakeOrderScenario, _create_buy_flow_fixture, _execute_case_b_buy
from tests.integration.test_deterministic_production_path_case2_flow import (
    _create_deterministic_production_path_fixture,
    _close_deterministic_fixture,
    _wait_for_market_evaluation,
    _wait_for_trade_count,
)
from tests.testnet._deterministic_public_case2_fixture import create_deterministic_public_case2_klines


class RuntimeLoggingFlowTests(unittest.TestCase):
    """
    클래스 이름: RuntimeLoggingFlowTests
    기능: offline 실행 결과가 파일의 조건·상태·체결·실패 정보와 일치하는지 검증한다.
    작성 날짜: 2026/09/09
    """

    artifact_directory: Path | None = None  # 검증 script가 지정하면 실제 Log_History에 증거를 유지한다.

    def _create_diagnostics(self, temporary_directory: str) -> tuple[DiagnosticLogWriter, RuntimeDiagnostics]:
        """
        함수 이름: _create_diagnostics()
        기능: 시나리오별 독립 파일과 주입 가능한 production 진단 경계를 만든다.
        인자: temporary_directory -> 기본 시험 artifact 위치
        반환값: 파일 writer와 application 진단 경계
        작성 날짜: 2026/09/09
        """
        directory = self.artifact_directory or Path(temporary_directory)
        writer = DiagnosticLogWriter(directory, "fake")
        diagnostics = RuntimeDiagnostics(writer)
        diagnostics.record("verification_scenario_started", scenario=self.id(), live_orders_sent=0)
        return writer, diagnostics  # 모든 주문 client는 기존 memory fixture만 사용한다.

    def _read_records(self, writer: DiagnosticLogWriter, diagnostics: RuntimeDiagnostics) -> list[dict]:
        """
        함수 이름: _read_records()
        기능: 실제 파일 전체를 parsing하고 누락·순서·실행 환경을 검증한다.
        인자: writer -> 검증 파일 위치, diagnostics -> 누락 횟수 관찰 경계
        반환값: 순서대로 복원한 JSON 사건 목록
        작성 날짜: 2026/09/09
        """
        records = [json.loads(line) for line in writer.path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(diagnostics.failure_count, 0)
        self.assertEqual([record["sequence"] for record in records], list(range(1, len(records) + 1)))
        self.assertTrue(all(record["execution_mode"] == "fake" for record in records))
        self.assertTrue(all(record["dropped_records_before"] == 0 for record in records))
        return records

    def test_case_b_and_c_entry_trailing_exit_are_reconstructable(self) -> None:
        """
        함수 이름: test_case_b_and_c_entry_trailing_exit_are_reconstructable()
        기능: B 회복·눌림·trend와 C flush·회복·trailing·B 인계의 실제 전이와 체결을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        for strategy in ("CASE_B", "CASE_C"):
            with self.subTest(strategy=strategy), TemporaryDirectory() as temporary_directory:
                writer, diagnostics = self._create_diagnostics(temporary_directory)
                with patch("tests.integration.test_buy_sell_flow.TradingController", partial(TradingController, diagnostics=diagnostics)):
                    report = replay_entry_scenario(strategy)
                records = self._read_records(writer, diagnostics)
                evaluations = [record["details"] for record in records if record["event"] == "strategy_evaluated"]

                # 독립 replay가 확인한 모든 전이가 파일에도 원본 before/after와 함께 있어야 한다.
                recorded_transitions = {transition for evaluation in evaluations for transition in evaluation["transition_ids"]}
                expected_transitions = {transition for step in report["steps"] for transition in step["transition_ids"]}
                self.assertTrue(expected_transitions <= recorded_transitions)
                entry_transition = "B-09" if strategy == "CASE_B" else "C-12"
                entry = next(evaluation for evaluation in evaluations if entry_transition in evaluation["transition_ids"])
                self.assertIsNotNone(entry["session_id"])
                self.assertIsNotNone(entry["lower_event_id"])
                self.assertIn("condition_comparisons", entry["evaluation"])
                self.assertEqual(entry["evaluation"]["market"]["realtime_pct_b"], "0.2" if strategy == "CASE_B" else "-0.24")
                self.assertTrue(any(evaluation["state_after"]["root_state"] == "LOGIC_TERMINATED" for evaluation in evaluations))

                # NEW·FILLED와 이력 commit이 같은 client ID로 연결되고 수량·수수료가 보존된다.
                received = [record["details"]["result"] for record in records if record["event"] == "order_result_received"]
                self.assertTrue({"NEW", "FILLED"} <= {result["status"] for result in received})
                filled = [result for result in received if result["status"] == "FILLED"]
                self.assertTrue(all(result["fills"] and result["fills"][0]["fee_asset"] for result in filled))
                steps = [record["details"] for record in records if record["event"] == "order_step"]
                self.assertTrue(any(step["trace"]["message_id"] == "13.5.1" for step in steps))
                self.assertEqual(len({result["client_order_id"] for result in received}), 2)
                self.assertEqual(report["fake_order_count"], 2)
                committed = [record["details"] for record in records if record["event"] == "trade_committed"]
                self.assertEqual(len(committed), 2)
                self.assertIsNotNone(committed[-1]["trade"]["realized_pnl"])
                self.assertIn("daily_return_rate", committed[-1]["performance"])
                self.assertTrue(any(record["event"] == "session_state_changed" and record["details"]["status_after"] == "terminated" for record in records))

    def test_case_c_timer_resets_and_wait_comparisons_are_recorded(self) -> None:
        """
        함수 이름: test_case_c_timer_resets_and_wait_comparisons_are_recorded()
        기능: 180초 경계·만료·더 낮은 저점 갱신과 매수하지 않은 이유를 파일로 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        with TemporaryDirectory() as temporary_directory:
            writer, diagnostics = self._create_diagnostics(temporary_directory)
            with patch("tests.integration.test_buy_sell_flow.TradingController", partial(TradingController, diagnostics=diagnostics)):
                report = replay_recovery_timer_scenario()
            records = self._read_records(writer, diagnostics)
            evaluations = [record["details"] for record in records if record["event"] == "strategy_evaluated"]

            # 경과 시간은 실제 Controller가 산출한 값을 확인하고 단순 timer 문자열만 검사하지 않는다.
            expired = next(evaluation for evaluation in evaluations if "C-11" in evaluation["transition_ids"])
            new_low = next(evaluation for evaluation in evaluations if "C-10" in evaluation["transition_ids"])
            self.assertGreater(Decimal(expired["evaluation"]["market"]["case_c_timer_elapsed"]), Decimal("180"))
            self.assertNotEqual(expired["evaluation"]["runtime"]["timer_base_time"], expired["runtime_after"]["timer_base_time"])
            self.assertEqual(new_low["runtime_after"]["flush_low"], "4300")
            self.assertEqual(new_low["runtime_after"]["timer_base_pct_b"], "-0.333")
            self.assertEqual(report["fake_order_count"], 0)

    def test_case_b_automatic_take_profit_returns_to_lower_watch(self) -> None:
        """
        함수 이름: test_case_b_automatic_take_profit_returns_to_lower_watch()
        기능: B 매수·5초 익절 대기·자동 매도·하단 감시 복귀가 실제 로그로 이어지는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        with TemporaryDirectory() as temporary_directory:
            writer, diagnostics = self._create_diagnostics(temporary_directory)
            with patch("tests.integration.test_buy_sell_flow.TradingController", partial(TradingController, diagnostics=diagnostics)):
                fixture = _create_buy_flow_fixture(temporary_directory, FakeOrderScenario.NEW_THEN_FILLED)
            controller = fixture.controller
            try:
                # 하단 접촉봉의 BBW와 확정 회복봉을 구분하고 실제 매수 신호를 생성한다.
                controller.update_split_ratios(command_id="b-example-split", expected_version=controller.context.version,
                    scale_in=Decimal("0.5"), scale_out=Decimal("1"))
                market = MarketEvaluationSnapshot(
                    realtime_price=Decimal("4300"), lower_band=Decimal("4300"), upper_band=Decimal("4350"),
                    realtime_pct_b=Decimal("0"), current_30m_candle_id="ETHUSDT:30m:example-touch",
                    current_30m_low=Decimal("4290"), current_30m_high=Decimal("4340"),
                    touch_candle_bbw=Decimal("50") / Decimal("4325"),
                )
                replay_market_evaluation(fixture, market, "example-lower-touch")
                fixture.clock.advance(timedelta(minutes=29, seconds=58))
                market = replace(market, realtime_price=Decimal("4330"), realtime_pct_b=Decimal("0.60"),
                    confirmed_30m_close=True, confirmed_30m_close_time=fixture.clock() + timedelta(seconds=1),
                    current_30m_candle_id="ETHUSDT:30m:example-signal", current_30m_low=Decimal("4305"),
                    pct_b_close=Decimal("0.60"), ema_slope_30m_close=Decimal("0.01"),
                    current_closed_candle_low=Decimal("4305"),
                    previous_3_closed_candle_lows=(Decimal("4290"), Decimal("4295"), Decimal("4300")))
                replay_market_evaluation(fixture, market, "example-signal-close")

                # 신호 2분 뒤 눌림으로 NEW 매수를 제출하고 같은 주문의 체결을 조회한다.
                fixture.clock.advance(timedelta(seconds=119))
                market = replace(market, realtime_price=Decimal("4310"), realtime_pct_b=Decimal("0.20"),
                    confirmed_30m_close=False, confirmed_30m_close_time=None)
                replay_market_evaluation(fixture, market, "example-buy-pullback")
                fixture.clock.advance(timedelta(seconds=1))
                fixture.rest_client.fill_time_origin = fixture.clock()  # 가짜 체결 시각도 모의 시장 시각과 맞춘다.
                controller.trigger_order_reconciliation(occurred_at=fixture.clock())
                asyncio.run(controller.drain_events(max_microsteps=100))
                self.assertGreater(fixture.position.quantity, Decimal("0"))

                # 가격 기준만 충족한 시점에는 기다리고 5초 유지 판정 이후에만 자동 익절한다.
                fixture.clock.advance(timedelta(seconds=119))
                market = replace(market, realtime_price=Decimal("4331"), realtime_pct_b=Decimal("0.62"),
                    realtime_ema_slope=Decimal("0.06"))
                replay_market_evaluation(fixture, market, "example-profit-zone-start")
                self.assertEqual(len(fixture.rest_client.submitted_orders), 1)
                fixture.clock.advance(timedelta(seconds=4))
                fixture.rest_client.scenario = FakeOrderScenario.IMMEDIATE_FILLED
                fixture.rest_client.fill_time_origin = fixture.clock() + timedelta(seconds=1)
                market = replace(market, pct_b_at_least_060_for_5s=True)
                replay_market_evaluation(fixture, market, "example-profit-zone-held-5s")
                self.assertEqual(len(fixture.rest_client.submitted_orders), 2)
                self.assertEqual(fixture.position.quantity, Decimal("0"))

                # 체결 기록만으로 성공 처리하지 않고 청산 완료와 다음 하단 감시 복귀까지 확인한다.
                records = self._read_records(writer, diagnostics)
                evaluations = [record["details"] for record in records if record["event"] == "strategy_evaluated"]
                transitions = {transition for evaluation in evaluations for transition in evaluation["transition_ids"]}
                self.assertTrue({"B-03", "B-05", "B-09", "PB-12", "PB-23F", "PB-24"} <= transitions)
                self.assertEqual(evaluations[-1]["state_after"]["root_state"], "LOWER_TOUCH_WATCH")
                self.assertIsNone(evaluations[-1]["runtime_after"]["pending_exit_reason"])
                self.assertIsNone(evaluations[-1]["runtime_after"]["lower_event_id"])
                trades = [record["details"]["trade"] for record in records if record["event"] == "trade_committed"]
                self.assertEqual([trade["side"] for trade in trades], ["BUY", "SELL"])
                self.assertEqual(trades[-1]["exit_reason"], "TAKE_PROFIT")
                self.assertEqual(Decimal(trades[0]["executed_amount"]), Decimal("50"))
                self.assertEqual(Decimal(trades[-1]["average_fill_price"]), Decimal("4331"))
                self.assertGreater(Decimal(trades[-1]["realized_pnl"]), Decimal("0"))
                profit_checks = [item["condition"]["satisfied"] for evaluation in evaluations
                    for item in evaluation["evaluation"]["condition_comparisons"]
                    if item["condition"]["condition_id"] == "b_profit_zone" and item["condition"]["value"] == "0.62"]
                self.assertIn(False, profit_checks)
                self.assertIn(True, profit_checks)
            finally:
                controller.close_session_resources()  # 일반 익절을 검증하므로 stop 명령으로 포지션을 청산하지 않는다.

    def test_timeout_queries_same_order_and_logs_safe_error_origin(self) -> None:
        """
        함수 이름: test_timeout_queries_same_order_and_logs_safe_error_origin()
        기능: 전송 timeout의 원문 제거와 UNKNOWN·동일 ID 조회·최종 체결 기록을 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        with TemporaryDirectory() as temporary_directory:
            writer, diagnostics = self._create_diagnostics(temporary_directory)
            with patch("tests.integration.test_buy_sell_flow.TradingController", partial(TradingController, diagnostics=diagnostics)):
                fixture = _create_buy_flow_fixture(temporary_directory, FakeOrderScenario.NEW_THEN_FILLED)
            try:
                original_submit = fixture.rest_client.submit_order

                def accept_then_timeout(*, order):
                    """
                    함수 이름: accept_then_timeout()
                    기능: memory 거래소가 주문을 수락한 뒤 응답만 유실된 상황을 재현한다.
                    인자: order -> production Controller가 만든 주문
                    반환값: TimeoutError 발생
                    작성 날짜: 2026/09/09
                    """
                    original_submit(order=order)
                    raise TimeoutError("credential-marker-do-not-log")  # 예외 원문 배제도 함께 시험한다.

                with patch.object(fixture.rest_client, "submit_order", side_effect=accept_then_timeout):
                    _execute_case_b_buy(fixture, "log-timeout-intent")
                fixture.controller.trigger_order_reconciliation(occurred_at=fixture.clock.advance(timedelta(seconds=1)))
                records = self._read_records(writer, diagnostics)
                self.assertNotIn("credential-marker-do-not-log", writer.path.read_text())
                errors = [record["details"] for record in records if record["event"] == "operation_failed"]
                self.assertTrue(any(cause["exception_type"] == "TimeoutError" and cause["frames"] for error in errors for cause in error["causes"]))
                results = [record["details"]["result"] for record in records if record["event"] == "order_result_received"]
                self.assertEqual([result["status"] for result in results], ["UNKNOWN", "FILLED"])
                self.assertEqual(len({result["client_order_id"] for result in results}), 1)
                self.assertEqual(len(fixture.rest_client.submitted_orders), 1)
            finally:
                fixture.controller.close_session_resources()

    def test_persistence_failure_logs_stage_and_reconciliation_gate(self) -> None:
        """
        함수 이름: test_persistence_failure_logs_stage_and_reconciliation_gate()
        기능: 체결 후 이력 저장 장애의 저장소 단계·원인 위치와 gate 잠금을 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        with TemporaryDirectory() as temporary_directory:
            writer, diagnostics = self._create_diagnostics(temporary_directory)
            with patch("tests.integration.test_buy_sell_flow.TradingController", partial(TradingController, diagnostics=diagnostics)):
                fixture = _create_buy_flow_fixture(temporary_directory, FakeOrderScenario.IMMEDIATE_FILLED)
            try:
                with patch("binance_auto_trader.adapters.persistence.trade_history_repository.os.fsync", side_effect=OSError("disk-private-marker")):
                    _execute_case_b_buy(fixture, "log-persistence-intent")
                records = self._read_records(writer, diagnostics)
                self.assertNotIn("disk-private-marker", writer.path.read_text())
                failures = [record["details"] for record in records if record["event"] == "order_step" and record["level"] == "ERROR"]
                self.assertTrue(any(failure["trace"]["failure_code"] == "HISTORY_PERSISTENCE_FAILED" for failure in failures))
                self.assertTrue(any(record["event"] == "reconciliation_required" for record in records))
            finally:
                fixture.controller.close_session_resources()

    def test_upper_band_termination_and_failed_event_keep_their_inputs(self) -> None:
        """
        함수 이름: test_upper_band_termination_and_failed_event_keep_their_inputs()
        기능: 상단 안전 종료와 예외 중단의 원본 시장 입력을 구분해 기록하는지 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        for fail_action in (False, True):
            with self.subTest(fail_action=fail_action), TemporaryDirectory() as temporary_directory:
                writer, diagnostics = self._create_diagnostics(temporary_directory)
                with patch("tests.integration.test_buy_sell_flow.TradingController", partial(TradingController, diagnostics=diagnostics)):
                    fixture = _create_buy_flow_fixture(temporary_directory, FakeOrderScenario.NEW_THEN_FILLED)
                try:
                    market = MarketEvaluationSnapshot(realtime_price=Decimal("4320"), lower_band=Decimal("4350"), upper_band=Decimal("4500"), realtime_pct_b=Decimal("-0.2"), touch_candle_bbw=Decimal("0.01"))
                    if fail_action:
                        with patch.object(type(fixture.controller._context), "open_lower_event", side_effect=RuntimeError("private-event-marker")):
                            with self.assertRaises(RuntimeError):
                                replay_market_evaluation(fixture, market, "failure-input")
                    else:
                        replay_market_evaluation(fixture, market, "lower-input")
                        upper_market = MarketEvaluationSnapshot(realtime_price=Decimal("4500"), lower_band=Decimal("4350"), upper_band=Decimal("4500"), realtime_pct_b=Decimal("1"))
                        replay_market_evaluation(fixture, upper_market, "upper-input")
                    records = self._read_records(writer, diagnostics)
                    if fail_action:
                        self.assertTrue(any(record["event"] == "evaluation_started" for record in records))
                        self.assertTrue(any(record["event"] == "operation_failed" for record in records))
                        self.assertNotIn("private-event-marker", writer.path.read_text())
                    else:
                        self.assertTrue(any("G-07" in record["details"].get("transition_ids", []) for record in records))
                        self.assertIn("UPPER_BAND_SAFE_TERMINATION", writer.path.read_text())
                finally:
                    fixture.controller.close_session_resources()

    def test_full_bootstrap_kline_worker_and_shutdown_write_to_file(self) -> None:
        """
        함수 이름: test_full_bootstrap_kline_worker_and_shutdown_write_to_file()
        기능: public Kline에서 실제 지표 계산·background 주문·startup·종료까지 파일 기록을 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        with TemporaryDirectory() as temporary_directory:
            log_directory = self.artifact_directory or Path(temporary_directory) / "logs"
            with patch("tests.integration.test_deterministic_production_path_case2_flow.create_application_runtime", partial(create_application_runtime, log_directory=log_directory)):
                fixture = _create_deterministic_production_path_fixture(temporary_directory)
            try:
                runtime = fixture.runtime
                start_application(runtime)
                controller = runtime.trading_controller
                selection = runtime.regime_controller.set_regime_type(RegimeType.TYPE_0, command_id="log-select", expected_version=controller.context.version)
                controller.start_trading(command_id="log-start", expected_version=selection.version)

                # 실제 Kline 입력과 production 지표 계산기가 Case C 주문을 발생시키게 한다.
                public_klines = create_deterministic_public_case2_klines(runtime.market_snapshot.get_snapshot())
                for kline in public_klines.as_tuple():
                    runtime.market_data_controller.observe_kline(kline)
                    _wait_for_market_evaluation(fixture, kline)
                _wait_for_trade_count(fixture, 1)
                controller.stop_trading(command_id="log-stop", expected_version=controller.context.version)
                _wait_for_trade_count(fixture, 2)
                close_application(runtime)

                # 파일에서만 startup·worker 생존·체결·종료와 소스 version을 복원한다.
                records = [json.loads(line) for path in log_directory.glob("*.log") for line in path.read_text().splitlines()]
                run_id = next(record["run_id"] for record in reversed(records) if record["event"] == "runtime_configured")
                records = [record for record in records if record["run_id"] == run_id]
                self.assertTrue({"logging_started", "runtime_configured", "startup_step", "runtime_heartbeat", "order_result_received"} <= {record["event"] for record in records})
                self.assertTrue(any(record["details"].get("state_after") == "CLOSED" for record in records))
                self.assertTrue(any(record["event"] == "strategy_evaluated" and "C-12" in record["details"]["transition_ids"] for record in records))
                self.assertEqual(runtime.diagnostics.failure_count, 0)
            finally:
                _close_deterministic_fixture(fixture)

    def test_market_builder_failure_records_input_and_internal_location(self) -> None:
        """
        함수 이름: test_market_builder_failure_records_input_and_internal_location()
        기능: STM 도달 전 지표 계산 실패도 원본 Kline·version·내부 발생 위치를 남기는지 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        with TemporaryDirectory() as temporary_directory:
            log_directory = self.artifact_directory or Path(temporary_directory) / "logs"
            with patch("tests.integration.test_deterministic_production_path_case2_flow.create_application_runtime", partial(create_application_runtime, log_directory=log_directory)):
                fixture = _create_deterministic_production_path_fixture(temporary_directory)
            try:
                start_application(fixture.runtime)
                controller = fixture.runtime.market_data_controller
                kline = create_deterministic_public_case2_klines(fixture.runtime.market_snapshot.get_snapshot()).setup

                # 지표 builder 경계만 실패시키고 실제 market Controller의 예외 경로를 통과한다.
                with patch.object(type(controller._market_evaluation_builder), "__call__", side_effect=ValueError("private-builder-marker")):
                    with self.assertRaises(ValueError):
                        controller.observe_kline(kline)
                records = [json.loads(line) for path in log_directory.glob("*.log") for line in path.read_text().splitlines()]
                failures = [record["details"] for record in records if record["event"] == "operation_failed" and record["details"]["stage"] == "market_kline_processing"]
                self.assertTrue(failures)
                self.assertEqual(failures[-1]["kline"]["close"], str(kline.close))
                self.assertTrue(failures[-1]["causes"][0]["frames"])
                self.assertNotIn("private-builder-marker", json.dumps(records))
            finally:
                _close_deterministic_fixture(fixture)

    def test_background_worker_failure_logs_stage_before_closing_gate(self) -> None:
        """
        함수 이름: test_background_worker_failure_logs_stage_before_closing_gate()
        기능: background 예외의 stage와 fail-close 실행을 로그 파일과 callback 양쪽에서 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        with TemporaryDirectory() as temporary_directory:
            writer, diagnostics = self._create_diagnostics(temporary_directory)

            async def fail_runtime_cycle():
                """
                함수 이름: fail_runtime_cycle()
                기능: worker가 실행하는 비동기 cycle에서 통제된 실패를 발생시킨다.
                인자: 없음
                반환값: RuntimeError 발생
                작성 날짜: 2026/09/09
                """
                raise RuntimeError("private-worker-marker")  # 원문 없이 실패 단계만 기록되어야 한다.

            fail_closed = Mock()
            worker = _TradingEventRuntimeWorker(fail_runtime_cycle, fail_closed, lambda: True, lambda: None, RLock(), poll_interval_seconds=0.001, diagnostics=diagnostics)
            try:
                worker.start()
                active_thread = worker._active_thread
                if active_thread is not None:
                    active_thread.join(timeout=2)
                self.assertTrue(worker.failed)
                fail_closed.assert_called_once()
                records = self._read_records(writer, diagnostics)
                failure = next(record["details"] for record in records if record["event"] == "operation_failed")
                self.assertEqual(failure["stage"], "event_runtime_worker")
                self.assertEqual(failure["failure_stage"], "RUNTIME_CYCLE")
                self.assertNotIn("private-worker-marker", writer.path.read_text())
            finally:
                worker.close()
