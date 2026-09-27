"""제출 직전 권한 변경과 늦게 도착한 체결의 단일 반영을 가짜 거래소로 검증한다."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, RLock, Thread, get_ident
import unittest
from unittest.mock import patch

from binance_auto_trader.adapters.binance.request_deadline import check_submission_before_send
from binance_auto_trader.adapters.binance.spot_rest_client import BinanceSpotRESTClient
from binance_auto_trader.application.trading_controller import TradingSessionError, TradingSessionFailureCode
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.trading.account import AccountSnapshot, AssetBalance
from binance_auto_trader.domain.trading.order import OrderSide, OrderStatus
from tests.integration.test_order_reconciliation_flow import _submit_case_b_buy
from tests.integration.test_testnet_restart_reconciliation_flow import (
    _create_recovery_controller, _FilledSubmissionTestnetRESTClient,
)
from tests.unit.binance.test_spot_rest_client import (
    FIXED_TIME, QueueHTTPTransport, _preparation_responses, _reference_price_payload,
)


class OrderSubmissionAdmissionTests(unittest.TestCase):
    """
    클래스 이름: OrderSubmissionAdmissionTests
    기능: 잠금 양보 뒤 실제 전송 권한과 이미 발생한 체결의 보존 경계를 검증한다.
    작성 날짜: 2026/09/27
    """

    def setUp(self):
        """
        함수 이름: setUp()
        기능: 네트워크를 차단하고 실제 durable journal을 가진 실행 중 세션을 준비한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
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
            command_id="admission-select", expected_version=0,
        )
        self.controller.start_trading(command_id="admission-start", expected_version=selection.version)

    def _assert_revoked_before_send(self, revoke):
        """
        함수 이름: _assert_revoked_before_send()
        기능: 실제 adapter의 POST guard 직전 권한 변경이 미전송 fsync와 주문 미전송으로 끝나는지 확인한다.
        인자: revoke -> 외부 작업이 application lock을 양보한 동안 호출할 권한 변경
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        entered, release = Event(), Event()
        errors = []

        def before_request(method, url):
            """
            함수 이름: before_request()
            기능: 전송 증거가 기록되기 전 POST 경계에 결정적인 대기를 삽입한다.
            인자: method -> 요청 종류, url -> 가짜 요청 주소
            반환값: 없음
            작성 날짜: 2026/09/27
            """
            if method == "POST":
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test release was not signaled")

        transport = QueueHTTPTransport(
            _preparation_responses(reference_price_payload=_reference_price_payload(price="2500")),
            request_entry_hook=before_request,
        )
        adapter = BinanceSpotRESTClient(
            "admission-dummy-key", "admission-dummy-secret", transport=transport,
            clock=lambda: FIXED_TIME, result_clock=lambda: FIXED_TIME,
        )

        def buy():
            """
            함수 이름: buy()
            기능: production처럼 application 잠금과 effect owner를 함께 예약해 BUY를 실행한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/09/27
            """
            try:
                with self.application_lock, self.controller._session_effect_lock():
                    _submit_case_b_buy(self.controller)
            except BaseException as error:
                errors.append(error)

        with patch.object(self.client, "prepare_order", side_effect=adapter.prepare_order), patch.object(
            self.client, "submit_order", side_effect=adapter.submit_order,
        ):
            buyer = Thread(target=buy)
            buyer.start()
            try:
                self.assertTrue(entered.wait(1), "submission never reached the transport guard")
                revoke()
                self.controller.snapshot_session()
            finally:
                release.set()
                buyer.join(3)
        self.assertFalse(buyer.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(any(request["method"] == "POST" for request in transport.requests))
        self.assertEqual(self.history.get_pending_orders(), ())
        self.assertEqual(self.history.trade_history.trades, ())
        journal = [json.loads(line) for line in self.history._repository.pending_order_storage_path.read_text().splitlines()]
        self.assertEqual(
            [entry.get("lifecycle", entry["operation"]) for entry in journal],
            ["PREPARED", "SUBMITTED", "NOT_SUBMITTED_CONFIRMED", "REMOVE"],
        )
        self.assertFalse(self.controller._external_operation_active)

    def test_manual_kill_before_wire_confirms_non_submission(self):
        """
        함수 이름: test_manual_kill_before_wire_confirms_non_submission()
        기능: SUBMITTED 기록 뒤 활성화한 kill이 실제 BUY 전송을 차단하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        self._assert_revoked_before_send(lambda: self.controller.set_manual_kill(
            True, command_id="admission-kill", expected_version=0,
        ))
        self.assertTrue(self.controller.manual_kill_active)

    def test_same_version_risk_policy_change_before_wire_confirms_non_submission(self):
        """
        함수 이름: test_same_version_risk_policy_change_before_wire_confirms_non_submission()
        기능: version만 같은 정책의 실제 한도 변경도 기존 예약 제출에 재사용하지 못하게 한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        changed_policy = replace(self.controller.risk_policy_state, max_order_notional=Decimal("1"))
        self._assert_revoked_before_send(lambda: self.controller.replace_risk_policy(changed_policy))

    def test_account_balance_change_before_wire_confirms_non_submission(self):
        """
        함수 이름: test_account_balance_change_before_wire_confirms_non_submission()
        기능: 전송 전 새 잔액이 도착하면 이전 계좌 version으로 계산한 BUY를 차단한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        snapshot = AccountSnapshot(
            balances=(AssetBalance("USDT", Decimal("1"), Decimal("0")),),
            updated_at=FIXED_TIME + timedelta(seconds=1), is_full_snapshot=False,
        )
        self._assert_revoked_before_send(lambda: self.controller._account.apply_stream_snapshot(snapshot))

    def test_account_disconnect_before_wire_confirms_non_submission(self):
        """
        함수 이름: test_account_disconnect_before_wire_confirms_non_submission()
        기능: 전송 직전 계좌 재조정 gate가 닫히면 UNKNOWN 주문을 만들지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        self._assert_revoked_before_send(lambda: self.controller.mark_account_stream_reconciliation_required(
            "stream_disconnected",
        ))

    def test_stop_before_wire_confirms_non_submission_and_finishes(self):
        """
        함수 이름: test_stop_before_wire_confirms_non_submission_and_finishes()
        기능: POST 직전 접수한 STOP이 owner를 기다리면서 신규 전송을 닫고 정상 종료하는지 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        stopped = Event()
        errors = []

        def stop():
            """
            함수 이름: stop()
            기능: 사용자 STOP을 다른 thread에서 요청하고 완료 여부를 기록한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/09/27
            """
            try:
                self.controller.stop_trading(
                    command_id="admission-stop", expected_version=self.controller.context.version,
                )
                stopped.set()
            except BaseException as error:
                errors.append(error)

        stopper = Thread(target=stop)

        def revoke():
            """
            함수 이름: revoke()
            기능: STOP 예약이 기록될 때까지 기다리되 effect 완료는 기다리지 않는다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/09/27
            """
            stopper.start()
            self.assertTrue(self.controller._external_stop_requested.wait(1))
            self.assertFalse(stopped.is_set())

        try:
            self._assert_revoked_before_send(revoke)
        finally:
            if stopper.ident is not None:
                stopper.join(3)
        self.assertEqual(errors, [])
        self.assertTrue(stopped.is_set())
        self.assertFalse(self.controller._external_stop_requested.is_set())
        self.assertIsNone(self.controller._effect_owner)

    def test_submission_attempt_record_is_inside_the_application_guard(self):
        """
        함수 이름: test_submission_attempt_record_is_inside_the_application_guard()
        기능: 권한 확인과 실제 attempt 기록 사이에 STOP이 끼어들 수 없는 임계 구역을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        transport = QueueHTTPTransport([
            *_preparation_responses(reference_price_payload=_reference_price_payload(price="2500")),
            TimeoutError("injected response loss after dispatch"),
        ])
        adapter = BinanceSpotRESTClient(
            "admission-dummy-key", "admission-dummy-secret", transport=transport,
            clock=lambda: FIXED_TIME, result_clock=lambda: FIXED_TIME,
        )
        original_begin = adapter._begin_order_transport_attempt
        attempt_recorded = []

        def begin_attempt(parameters):
            """
            함수 이름: begin_attempt()
            기능: 실제 adapter가 전송 증거를 기록하는 순간 application 잠금 소유 여부를 확인한다.
            인자: parameters -> adapter가 구성한 가짜 주문 매개변수
            반환값: 원 attempt 기록 결과
            작성 날짜: 2026/09/27
            """
            self.assertTrue(self.application_lock._is_owned())
            result = original_begin(parameters)
            attempt_recorded.append(True)
            return result

        with patch.object(self.client, "prepare_order", side_effect=adapter.prepare_order), patch.object(
            self.client, "submit_order", side_effect=adapter.submit_order,
        ), patch.object(adapter, "_begin_order_transport_attempt", new=begin_attempt):
            with self.application_lock, self.controller._session_effect_lock():
                _submit_case_b_buy(self.controller)
        self.assertEqual(attempt_recorded, [True])
        self.assertEqual(sum(request["method"] == "POST" for request in transport.requests), 1)
        state = next(iter(self.controller._order_states_by_client_id.values()))
        self.assertIs(state.order.status, OrderStatus.UNKNOWN)
        self.assertEqual(len(self.history.get_pending_orders()), 1)

    def test_duplicate_stop_during_force_sell_preserves_single_sell_and_releases_latch(self):
        """
        함수 이름: test_duplicate_stop_during_force_sell_preserves_single_sell_and_releases_latch()
        기능: 청산 준비 중 동일 STOP 재전송이 SELL을 막거나 종료 뒤 정지 예약을 남기지 않는지 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        with self.application_lock, self.controller._session_effect_lock():
            outcomes = _submit_case_b_buy(self.controller)
            self.controller._enqueue_order_outcomes(outcomes)
            asyncio.run(self.controller.drain_events())
        self.assertGreater(self.position.quantity, Decimal("0"))
        expected_version = self.controller.context.version
        entered, release = Event(), Event()
        results, errors, submitted_sides = [], [], []
        original_prepare = self.client.prepare_order
        original_submit = self.client.submit_order
        original_effect_lock = self.controller._session_effect_lock
        second_admitted = Event()

        def enter_effect():
            """
            함수 이름: enter_effect()
            기능: 중복 STOP이 admission을 완료하고 owner 대기에 진입하는 경계를 관측한다.
            인자: 없음
            반환값: 원 effect context manager
            작성 날짜: 2026/09/27
            """
            if get_ident() == second.ident:
                second_admitted.set()
            return original_effect_lock()

        def prepare_order(*, order):
            """
            함수 이름: prepare_order()
            기능: 최초 STOP의 청산 준비를 멈춰 동일 명령이 아직 receipt 없는 시점에 재도착하게 한다.
            인자: order -> 청산 주문
            반환값: 원 준비 결과
            작성 날짜: 2026/09/27
            """
            self.assertIs(order.side, OrderSide.SELL)
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test release was not signaled")
            return original_prepare(order=order)

        def submit_order(*, order):
            """
            함수 이름: submit_order()
            기능: production guard를 확인한 SELL에 기존 BUY와 다른 거래소 체결 식별자를 부여한다.
            인자: order -> 준비를 마친 청산 주문
            반환값: 가짜 전량 체결
            작성 날짜: 2026/09/27
            """
            check_submission_before_send(lambda: None)
            self.client.submission_exchange_order_id = "93002"
            self.client.submission_trade_id = "43002"
            submitted_sides.append(order.side)
            return original_submit(order=order)

        def stop():
            """
            함수 이름: stop()
            기능: 두 thread에서 동일 command ID와 version의 STOP을 요청한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/09/27
            """
            try:
                results.append(self.controller.stop_trading(
                    command_id="duplicate-stop", expected_version=expected_version,
                ))
            except BaseException as error:
                errors.append(error)

        with patch.object(self.client, "prepare_order", new=prepare_order), patch.object(
            self.client, "submit_order", new=submit_order,
        ), patch.object(
            self.controller, "_session_effect_lock", new=enter_effect,
        ):
            first, second = Thread(target=stop), Thread(target=stop)
            first.start()
            try:
                self.assertTrue(entered.wait(1), "STOP never reached force-sell preparation")
                reservation_count = len(self.controller._external_stop_reservations)
                with self.assertRaises(TradingSessionError) as rejected:
                    self.controller.stop_trading(
                        command_id="duplicate-stop", expected_version=expected_version + 1,
                    )
                self.assertIs(rejected.exception.code, TradingSessionFailureCode.COMMAND_ID_REUSED)
                self.assertEqual(len(self.controller._external_stop_reservations), reservation_count)
                second.start()
                self.assertTrue(second_admitted.wait(1))
                self.assertTrue(self.controller._external_stop_requested.is_set())
                self.controller.snapshot_session()
            finally:
                release.set()
                first.join(3)
                if second.ident is not None:
                    second.join(3)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(submitted_sides, [OrderSide.SELL])
        self.assertEqual(self.client.submit_count, 2)
        self.assertEqual(len(self.history.trade_history.trades), 2)
        self.assertEqual(self.position.quantity, Decimal("0"))
        self.assertFalse(self.controller._external_stop_requested.is_set())
        self.assertEqual(self.controller._external_stop_reservations, set())
        self.assertEqual(self.controller._inflight_stop_commands, {})
        self.assertIsNone(self.controller._effect_owner)

    def test_reserved_stop_rejects_replaced_session_and_releases_only_its_reservation(self):
        """
        함수 이름: test_reserved_stop_rejects_replaced_session_and_releases_only_its_reservation()
        기능: owner 대기 중 session identity가 바뀌면 STOP을 거부하고 latch와 명령 예약을 정리한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        entered, release = Event(), Event()
        errors = []
        original_session_id = self.controller._session_id

        def fetch():
            """
            함수 이름: fetch()
            기능: session identity 교체 장애를 주입할 수 있도록 가짜 외부 조회를 대기한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/09/27
            """
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test release was not signaled")

        def own_effect():
            """
            함수 이름: own_effect()
            기능: 현재 세션의 외부 작업 owner를 먼저 획득한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/09/27
            """
            try:
                with self.application_lock, self.controller._session_effect_lock():
                    self.controller._run_external_operation(fetch)
            except BaseException as error:
                errors.append(error)

        def stop():
            """
            함수 이름: stop()
            기능: 이전 세션 identity에 STOP을 예약하고 예상된 거부를 수집한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/09/27
            """
            try:
                self.controller.stop_trading(
                    command_id="replaced-session-stop", expected_version=self.controller.context.version,
                )
            except BaseException as error:
                errors.append(error)

        owner, stopper = Thread(target=own_effect), Thread(target=stop)
        owner.start()
        try:
            self.assertTrue(entered.wait(1))
            stopper.start()
            self.assertTrue(self.controller._external_stop_requested.wait(1))
            with self.application_lock:
                self.controller._session_id = "replacement-session"
        finally:
            release.set()
            owner.join(3)
            if stopper.ident is not None:
                stopper.join(3)
            self.controller._session_id = original_session_id
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], TradingSessionError)
        self.assertIs(errors[0].code, TradingSessionFailureCode.INVALID_SESSION_STATE)
        self.assertFalse(self.controller._external_stop_requested.is_set())
        self.assertEqual(self.controller._external_stop_reservations, set())
        self.assertEqual(self.controller._inflight_stop_commands, {})
        self.assertIsNone(self.controller._effect_owner)

    def test_late_post_fill_survives_kill_disconnect_and_duplicate_stream_result(self):
        """
        함수 이름: test_late_post_fill_survives_kill_disconnect_and_duplicate_stream_result()
        기능: 실제 제출 뒤 권한 변경과 중복 stream이 겹쳐도 체결과 이력을 한 번 보존한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        accepted, release, observed = Event(), Event(), Event()
        errors = []
        original_submit = self.client.submit_order

        def submit_order(*, order):
            """
            함수 이름: submit_order()
            기능: 실제 전송 guard를 통과한 가짜 체결의 응답만 barrier에서 지연한다.
            인자: order -> 제출 권한을 예약한 주문
            반환값: 최초 실제 체결 결과
            작성 날짜: 2026/09/27
            """
            check_submission_before_send(lambda: None)
            result = original_submit(order=order)
            accepted.set()
            if not release.wait(3):
                raise TimeoutError("test release was not signaled")
            return result

        def buy():
            """
            함수 이름: buy()
            기능: effect owner 아래 주문을 실행하고 예외를 main thread에 전달한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/09/27
            """
            try:
                with self.application_lock, self.controller._session_effect_lock():
                    _submit_case_b_buy(self.controller)
            except BaseException as error:
                errors.append(error)

        def observe():
            """
            함수 이름: observe()
            기능: 지연된 POST와 같은 체결을 다른 thread에서 중복 관찰한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/09/27
            """
            try:
                self.controller.observe_order_result(self.client.result)
                observed.set()
            except BaseException as error:
                errors.append(error)

        with patch.object(self.client, "submit_order", new=submit_order):
            buyer, observer = Thread(target=buy), Thread(target=observe)
            buyer.start()
            try:
                self.assertTrue(accepted.wait(1))
                self.controller.set_manual_kill(True, command_id="late-fill-kill", expected_version=0)
                self.controller.mark_account_stream_reconciliation_required("stream_disconnected")
                observer.start()
                self.assertFalse(observed.wait(0.05), "stream result overtook the reserved POST owner")
                self.controller.snapshot_session()
            finally:
                release.set()
                buyer.join(3)
                if observer.ident is not None:
                    observer.join(3)
        self.assertEqual(errors, [])
        self.assertTrue(observed.is_set())
        self.assertEqual(self.client.submit_count, 1)
        self.assertEqual(len(self.history.trade_history.trades), 1)
        self.assertEqual(len(self.client.result.fills), 1)
        self.assertEqual(self.position.quantity, self.client.result.fills[0].quantity)
        self.assertEqual(self.history.get_pending_orders(), ())
        state = self.controller._order_states_by_client_id[self.client.result.client_order_id]
        self.assertIs(state.order.status, OrderStatus.FILLED)
        self.assertTrue(self.controller.manual_kill_active)
        self.assertFalse(self.controller._order_pipeline_enabled)
        self.assertIsNone(self.controller._effect_owner)

    def test_stop_after_post_acceptance_retains_fill_and_sells_the_actual_position_once(self):
        """
        함수 이름: test_stop_after_post_acceptance_retains_fill_and_sells_the_actual_position_once()
        기능: BUY 수락 뒤 STOP이 도착해도 늦은 체결을 보존하고 실제 잔여 포지션만 한 번 청산한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        accepted, release, stopped = Event(), Event(), Event()
        errors, submitted_sides = [], []
        original_submit = self.client.submit_order

        def submit_order(*, order):
            """
            함수 이름: submit_order()
            기능: BUY 수락 응답만 지연하고 이후 청산에는 독립 체결 identity를 사용한다.
            인자: order -> 준비된 BUY 또는 STOP SELL
            반환값: 가짜 거래소의 실제 체결 결과
            작성 날짜: 2026/09/27
            """
            check_submission_before_send(lambda: None)
            if order.side is OrderSide.SELL:
                self.client.submission_exchange_order_id = "93002"
                self.client.submission_trade_id = "43002"
            result = original_submit(order=order)
            submitted_sides.append(order.side)
            if order.side is OrderSide.BUY:
                accepted.set()
                if not release.wait(3):
                    raise TimeoutError("test release was not signaled")
            return result

        def buy():
            """
            함수 이름: buy()
            기능: 응답과 outcome을 원 owner가 반영한 뒤 대기 중 STOP에 제어권을 넘긴다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/09/27
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
            기능: 아직 응답을 받지 못한 BUY와 같은 세션에 public STOP을 예약한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/09/27
            """
            try:
                self.controller.stop_trading(
                    command_id="stop-accepted-buy", expected_version=self.controller.context.version,
                )
                stopped.set()
            except BaseException as error:
                errors.append(error)

        with patch.object(self.client, "submit_order", new=submit_order):
            buyer, stopper = Thread(target=buy), Thread(target=stop)
            buyer.start()
            try:
                self.assertTrue(accepted.wait(1))
                stopper.start()
                self.assertTrue(self.controller._external_stop_requested.wait(1))
                self.controller.snapshot_session()
                self.assertFalse(stopped.is_set())
            finally:
                release.set()
                buyer.join(3)
                if stopper.ident is not None:
                    stopper.join(3)
        self.assertEqual(errors, [])
        self.assertTrue(stopped.is_set())
        self.assertEqual(submitted_sides, [OrderSide.BUY, OrderSide.SELL])
        self.assertEqual(self.client.submit_count, 2)
        self.assertEqual(len(self.history.trade_history.trades), 2)
        self.assertEqual(self.position.quantity, Decimal("0"))
        self.assertEqual(self.history.get_pending_orders(), ())
        self.assertFalse(self.controller._external_stop_requested.is_set())
        self.assertIsNone(self.controller._effect_owner)

    def test_external_exception_restores_recursive_lock_and_releases_effect_owner(self):
        """
        함수 이름: test_external_exception_restores_recursive_lock_and_releases_effect_owner()
        기능: 외부 예외가 발생해도 재진입 잠금 깊이와 owner가 누수 없이 복원되는지 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        def fail():
            """
            함수 이름: fail()
            기능: application lock 밖에서 가짜 전송 예외를 발생시킨다.
            인자: 없음
            반환값: 반환하지 않음
            작성 날짜: 2026/09/27
            """
            self.assertFalse(self.application_lock._is_owned())
            raise OSError("injected external failure")

        with self.application_lock, self.application_lock:
            with self.assertRaisesRegex(OSError, "injected external failure"):
                self.controller._run_external_operation(fail)
            self.assertTrue(self.application_lock._is_owned())
            self.assertEqual(self.application_lock._recursion_count(), 2)
        self.assertFalse(self.application_lock._is_owned())
        self.assertFalse(self.controller._external_operation_active)
        self.assertIsNone(self.controller._effect_owner)
