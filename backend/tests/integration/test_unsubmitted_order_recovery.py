"""실제 POST 전 만료의 미전송 확정과 journal crash 경계를 가짜 전송으로 검증한다."""

from datetime import timedelta
import json
import unittest
from unittest.mock import patch

from binance_auto_trader.adapters.binance.spot_rest_client import BinanceSpotRESTClient
from binance_auto_trader.adapters.binance.request_deadline import (
    RequestDeadlineExceeded, request_deadline_scope, submission_check_scope,
)
from binance_auto_trader.adapters.persistence.trade_history_repository import TradeHistoryRepository
from binance_auto_trader.adapters.persistence import PendingOrderJournalCorruptedError
from binance_auto_trader.application.trading_controller import StartupOrderReconciliationError, TradingSessionStatus
from binance_auto_trader.domain.trading.order import (
    OrderResult, OrderResultFailureKind, OrderStatus, PendingOrderRecoveryLifecycle,
)
from binance_auto_trader.domain.trading.events import TradingEventType
from tests.integration.test_order_reconciliation_flow import _submit_case_b_buy
from tests.integration import test_session_recovery as recovery_fixture
from tests.integration.test_testnet_restart_reconciliation_flow import (
    _create_recovery_controller, _FilledSubmissionTestnetRESTClient,
)
from tests.unit.binance.test_spot_rest_client import (
    FIXED_TIME, FIXED_TIME_MILLISECONDS, MutableUTCClock, QueueHTTPTransport,
    _json_response, _order, _preparation_responses, _reference_price_payload,
)


class UnsubmittedOrderRecoveryTests(unittest.TestCase):
    """
    클래스 이름: UnsubmittedOrderRecoveryTests
    기능: 미전송 확정의 영속화 전후 장애와 재시작에서 중복 제출 없는 종료를 확인한다.
    작성 날짜: 2026/09/22
    """

    def setUp(self) -> None:
        """
        함수 이름: setUp()
        기능: 네트워크 차단과 임시 journal을 가진 기존 production 복구 fixture를 조립한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        self.fixture = recovery_fixture.SessionRecoveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.controller = self.fixture.controller
        self.transport = QueueHTTPTransport(_preparation_responses(
            reference_price_payload=_reference_price_payload(price="2500"),
        ))
        self.adapter_clock = MutableUTCClock(FIXED_TIME)
        self.adapter = BinanceSpotRESTClient(
            "audit-dummy-key", "audit-dummy-secret", transport=self.transport,
            clock=self.adapter_clock, result_clock=self.adapter_clock,
        )

    def _submit_expired_order(self) -> tuple[object, ...]:
        """
        함수 이름: _submit_expired_order()
        기능: 실제 adapter의 성공한 준비 뒤 31초를 진행시켜 최초 POST 전 만료를 만든다.
        인자: 없음
        반환값: Controller가 만든 주문 outcome tuple
        작성 날짜: 2026/09/22
        """
        def prepare_order(*, order):
            """
            함수 이름: prepare_order()
            기능: 준비 identity를 유지한 채 journal 전 대기 시간을 제어 시계로 재현한다.
            인자: order -> Controller가 예약한 주문
            반환값: 준비 완료 주문
            작성 날짜: 2026/09/22
            """
            prepared_order = self.adapter.prepare_order(order)
            self.adapter_clock.advance(timedelta(seconds=31))
            return prepared_order

        with patch.object(self.fixture.client, "prepare_order", side_effect=prepare_order), patch.object(
            self.fixture.client, "submit_order", side_effect=self.adapter.submit_order,
        ):
            return _submit_case_b_buy(self.controller)

    def _restart(self):
        """
        함수 이름: _restart()
        기능: 메모리 전송 증거를 공유하지 않는 새 Controller로 같은 durable journal을 읽는다.
        인자: 없음
        반환값: 새 Controller, history, fake REST client
        작성 날짜: 2026/09/22
        """
        client = _FilledSubmissionTestnetRESTClient()
        controller, history, _ = _create_recovery_controller(
            self.fixture.path, client, command_gate=True, order_retry_waiter=lambda delay: None,
        )
        self.addCleanup(controller.close_session_resources)
        return controller, history, client

    def test_expiry_fsyncs_non_submission_and_returns_failure_without_query(self) -> None:
        """
        함수 이름: test_expiry_fsyncs_non_submission_and_returns_failure_without_query()
        기능: POST 없는 만료가 UNKNOWN 대기 대신 fsync·REMOVE와 기존 실패 outcome으로 완료되는지 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        outcomes = self._submit_expired_order()
        self.assertEqual(len(outcomes), 1)
        self.assertIs(self.controller.status, TradingSessionStatus.RUNNING)
        self.assertFalse(self.controller.session_recovery_pending)
        self.assertEqual(self.fixture.history.get_pending_order_recovery_records(), ())
        self.assertEqual(self.fixture.client.query_client_order_ids, [])
        self.assertFalse(any(request["method"] == "POST" for request in self.transport.requests))
        self.assertEqual(tuple(self.controller._submission_attempts_by_intent.values()), (1,))
        journal_path = self.fixture.history._repository.pending_order_storage_path
        events = [json.loads(line) for line in journal_path.read_text().splitlines()]
        self.assertEqual(
            [event.get("lifecycle", event["operation"]) for event in events],
            ["PREPARED", "SUBMITTED", "NOT_SUBMITTED_CONFIRMED", "REMOVE"],
        )
        self.assertEqual([event["schema_version"] for event in events], [4, 4, 5, 4])

    def test_confirmation_write_failure_keeps_gate_until_durable_retry(self) -> None:
        """
        함수 이름: test_confirmation_write_failure_keeps_gate_until_durable_retry()
        기능: 미전송 fsync 전 실패는 gate를 잠그고 동일 결과의 영속화 재시도 뒤에만 복구한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        original_transition = TradeHistoryRepository.transition_pending_order_lifecycle

        def fail_confirmation(repository, client_order_id, lifecycle):
            """
            함수 이름: fail_confirmation()
            기능: SUBMITTED는 저장하고 미전송 확정의 파일 쓰기 직전에만 실패한다.
            인자: repository -> 실제 저장소, client_order_id -> 주문 ID, lifecycle -> 다음 상태
            반환값: 정상 전이이면 없음
            작성 날짜: 2026/09/22
            """
            if lifecycle is PendingOrderRecoveryLifecycle.NOT_SUBMITTED_CONFIRMED:
                raise OSError("injected confirmation write failure")
            return original_transition(repository, client_order_id, lifecycle)

        with patch.object(TradeHistoryRepository, "transition_pending_order_lifecycle", new=fail_confirmation):
            self.assertEqual(self._submit_expired_order(), ())
        self.assertTrue(self.controller.session_recovery_pending)
        self.assertIs(self.controller.status, TradingSessionStatus.RECONCILIATION_REQUIRED)
        self.fixture.recover()
        self.assertEqual(self.fixture.history.get_pending_order_recovery_records(), ())
        self.assertEqual(self.fixture.client.query_client_order_ids, [])
        self.assertFalse(any(request["method"] == "POST" for request in self.transport.requests))

    def test_restart_before_confirmation_never_infers_non_submission_from_absence(self) -> None:
        """
        함수 이름: test_restart_before_confirmation_never_infers_non_submission_from_absence()
        기능: 확정 fsync 전에 죽은 새 실행은 SUBMITTED와 부재 조회만으로 미전송을 추정하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        original_transition = TradeHistoryRepository.transition_pending_order_lifecycle

        def fail_confirmation(repository, client_order_id, lifecycle):
            """
            함수 이름: fail_confirmation()
            기능: 미전송 확정만 durable 기록 전에 차단한다.
            인자: repository -> 저장소, client_order_id -> 주문 ID, lifecycle -> 기록할 상태
            반환값: 정상 저장 시 없음
            작성 날짜: 2026/09/22
            """
            if lifecycle is PendingOrderRecoveryLifecycle.NOT_SUBMITTED_CONFIRMED:
                raise OSError("injected process loss before confirmation")
            return original_transition(repository, client_order_id, lifecycle)

        with patch.object(TradeHistoryRepository, "transition_pending_order_lifecycle", new=fail_confirmation):
            self._submit_expired_order()
        controller, history, client = self._restart()
        pending = history.get_pending_order_recovery_records()[0]
        result = OrderResult(
            symbol=pending.order.symbol, client_order_id=pending.order.client_order_id,
            status=OrderStatus.UNKNOWN, processed_at=self.fixture.clock(),
            failure_kind=OrderResultFailureKind.ORDER_NOT_VISIBLE,
        )
        with patch.object(client, "query_order_result", return_value=result) as query:
            with self.assertRaises(StartupOrderReconciliationError):
                controller.reconcile_startup_state()
        self.assertEqual(query.call_count, 4)
        self.assertEqual(len(history.get_pending_order_recovery_records()), 1)
        self.assertEqual(client.submit_count, 0)

    def test_restart_after_confirmation_cleans_without_order_query(self) -> None:
        """
        함수 이름: test_restart_after_confirmation_cleans_without_order_query()
        기능: fsync 직후 응답 유실과 REMOVE 직전 crash 모두 durable 미전송 증거로 안전하게 정리한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        original_transition = TradeHistoryRepository.transition_pending_order_lifecycle

        def fail_after_confirmation(repository, client_order_id, lifecycle):
            """
            함수 이름: fail_after_confirmation()
            기능: 미전송 확정의 fsync 성공 뒤 호출자에게 오류를 전달한다.
            인자: repository -> 저장소, client_order_id -> 주문 ID, lifecycle -> 기록할 상태
            반환값: 해당 확정 외 정상 전이이면 없음
            작성 날짜: 2026/09/22
            """
            original_transition(repository, client_order_id, lifecycle)
            if lifecycle is PendingOrderRecoveryLifecycle.NOT_SUBMITTED_CONFIRMED:
                raise OSError("injected process loss after confirmation")

        with patch.object(TradeHistoryRepository, "transition_pending_order_lifecycle", new=fail_after_confirmation):
            self._submit_expired_order()
        controller, history, client = self._restart()
        with patch.object(client, "query_order_result", side_effect=AssertionError("no query for durable non-submission")):
            controller.reconcile_startup_state()
        self.assertTrue(controller.startup_reconciliation_complete)
        self.assertEqual(history.get_pending_order_recovery_records(), ())
        self.assertEqual(client.submit_count, 0)

    def test_remove_response_loss_recovers_without_missing_record_transition(self) -> None:
        """
        함수 이름: test_remove_response_loss_recovers_without_missing_record_transition()
        기능: REMOVE가 fsync된 뒤 오류가 나도 없는 pending 재전이 없이 outcome을 복구한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        original_delete = TradeHistoryRepository.delete_pending_order

        def fail_after_remove(repository, client_order_id):
            """
            함수 이름: fail_after_remove()
            기능: REMOVE를 실제 저장한 뒤 응답만 유실시킨다.
            인자: repository -> 저장소, client_order_id -> 제거할 주문 ID
            반환값: 반환하지 않음
            작성 날짜: 2026/09/22
            """
            original_delete(repository, client_order_id)
            raise OSError("injected remove response loss")

        with patch.object(TradeHistoryRepository, "delete_pending_order", new=fail_after_remove):
            self.assertEqual(self._submit_expired_order(), ())
        self.assertTrue(self.controller.session_recovery_pending)
        self.fixture.recover()
        self.assertEqual(self.fixture.history.get_pending_order_recovery_records(), ())
        self.assertEqual(self.fixture.client.query_client_order_ids, [])
        self.assertTrue(next(iter(self.controller._order_states_by_client_id.values())).pending_outcome)

    def test_expiry_after_first_post_keeps_unknown(self) -> None:
        """
        함수 이름: test_expiry_after_first_post_keeps_unknown()
        기능: 첫 POST 뒤 timestamp 재시도 중 만료는 미전송 확정으로 바꾸지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        transport = QueueHTTPTransport([
            *_preparation_responses(),
            _json_response({"code": -1021}, status_code=400),
            _json_response({"serverTime": FIXED_TIME_MILLISECONDS + 31_000}),
        ])
        adapter = BinanceSpotRESTClient(
            "audit-dummy-key", "audit-dummy-secret", transport=transport,
            clock=lambda: FIXED_TIME, result_clock=lambda: FIXED_TIME,
        )
        order = adapter.prepare_order(_order())
        result = adapter.submit_order(order=order)
        self.assertIs(result.status, OrderStatus.UNKNOWN)
        self.assertIsNone(result.failure_kind)
        self.assertEqual(sum(request["method"] == "POST" for request in transport.requests), 1)
        self.assertIsNotNone(adapter.get_order_submission_attempt_evidence(client_order_id=order.client_order_id))

    def test_retry_rechecks_expired_strategy_before_preparation(self) -> None:
        """
        함수 이름: test_retry_rechecks_expired_strategy_before_preparation()
        기능: 미전송 만료 뒤 전략 신호가 유효하지 않으면 재준비와 POST 없이 의도를 만료한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        self._submit_expired_order()
        with patch.object(self.fixture.client, "prepare_order") as prepare, patch.object(
            self.fixture.client, "submit_order",
        ) as submit:
            outcomes = _submit_case_b_buy(self.controller)
        self.assertEqual(len(outcomes), 1)
        self.assertIs(outcomes[0].event_type, TradingEventType.ORDER_PREPARATION_EXPIRED)
        prepare.assert_not_called()
        submit.assert_not_called()
        self.assertEqual(tuple(self.controller._submission_attempts_by_intent.values()), (1,))

    def test_deadline_before_dispatch_is_confirmed_but_post_read_deadline_is_unknown(self) -> None:
        """
        함수 이름: test_deadline_before_dispatch_is_confirmed_but_post_read_deadline_is_unknown()
        기능: 동일한 로컬 deadline도 실제 POST 시작 전후를 구분해 미전송과 UNKNOWN으로 분류한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        order = self.adapter.prepare_order(_order())
        with patch("binance_auto_trader.adapters.binance.request_deadline.monotonic", return_value=0) as clock:
            with request_deadline_scope(1):
                clock.return_value = 2
                result = self.adapter.submit_order(order=order)
        self.assertIs(result.failure_kind, OrderResultFailureKind.NOT_SUBMITTED_EXPIRED)
        self.assertFalse(any(request["method"] == "POST" for request in self.transport.requests))

        transport = QueueHTTPTransport([*_preparation_responses(), RequestDeadlineExceeded("read deadline")])
        adapter = BinanceSpotRESTClient(
            "audit-dummy-key", "audit-dummy-secret", transport=transport,
            clock=lambda: FIXED_TIME, result_clock=lambda: FIXED_TIME,
        )
        result = adapter.submit_order(order=adapter.prepare_order(_order()))
        self.assertIs(result.status, OrderStatus.UNKNOWN)
        self.assertIsNone(result.failure_kind)
        self.assertEqual(sum(request["method"] == "POST" for request in transport.requests), 1)

    def test_missing_dispatch_state_does_not_prove_non_submission(self) -> None:
        """
        함수 이름: test_missing_dispatch_state_does_not_prove_non_submission()
        기능: 메모리 전송 증거가 없다는 사실만으로 만료 응답을 미전송 확정으로 만들지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        order = self.adapter.prepare_order(_order())
        self.adapter._order_dispatch_states.clear()
        self.adapter_clock.advance(timedelta(seconds=31))
        result = self.adapter.submit_order(order=order)
        self.assertIs(result.status, OrderStatus.UNKNOWN)
        self.assertIsNone(result.failure_kind)
        self.assertFalse(any(request["method"] == "POST" for request in self.transport.requests))

    def test_permission_revocation_before_and_after_first_post_is_distinguished(self) -> None:
        """
        함수 이름: test_permission_revocation_before_and_after_first_post_is_distinguished()
        기능: STOP·kill 권한 차단도 최초 POST 전이면 미전송, 첫 POST 뒤이면 UNKNOWN을 유지한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        order = self.adapter.prepare_order(_order())
        with submission_check_scope(lambda: False):
            result = self.adapter.submit_order(order=order)
        self.assertIs(result.failure_kind, OrderResultFailureKind.NOT_SUBMITTED_EXPIRED)
        self.assertEqual(result.failure_reason, "ORDER_PERMISSION_REVOKED_BEFORE_DISPATCH")
        self.assertFalse(any(request["method"] == "POST" for request in self.transport.requests))

        transport = QueueHTTPTransport([
            *_preparation_responses(),
            _json_response({"code": -1021}, status_code=400),
            _json_response({"serverTime": FIXED_TIME_MILLISECONDS}),
        ])
        adapter = BinanceSpotRESTClient(
            "audit-dummy-key", "audit-dummy-secret", transport=transport,
            clock=lambda: FIXED_TIME, result_clock=lambda: FIXED_TIME,
        )
        order = adapter.prepare_order(_order())
        permissions = iter((True, False))
        with submission_check_scope(lambda: next(permissions)):
            result = adapter.submit_order(order=order)
        self.assertIs(result.status, OrderStatus.UNKNOWN)
        self.assertEqual(result.failure_reason, "ORDER_PERMISSION_REVOKED_AFTER_DISPATCH")
        self.assertIsNone(result.failure_kind)
        self.assertEqual(sum(request["method"] == "POST" for request in transport.requests), 1)

    def test_unknown_journal_cannot_advance_to_non_submission(self) -> None:
        """
        함수 이름: test_unknown_journal_cannot_advance_to_non_submission()
        기능: 기존 UNKNOWN 저널에 미전송 전이를 덧붙여 주문 불확실성을 지우지 못하게 한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        repository = TradeHistoryRepository(self.fixture.path)
        order = _order()
        repository.save_pending_order(order)
        repository.transition_pending_order_lifecycle(order.client_order_id, PendingOrderRecoveryLifecycle.UNKNOWN)
        with self.assertRaisesRegex(ValueError, "lifecycle transition is invalid"):
            repository.transition_pending_order_lifecycle(
                order.client_order_id, PendingOrderRecoveryLifecycle.NOT_SUBMITTED_CONFIRMED,
            )

    def test_older_schema_cannot_claim_non_submission(self) -> None:
        """
        함수 이름: test_older_schema_cannot_claim_non_submission()
        기능: 구버전 전이에 신규 미전송 lifecycle을 삽입한 손상 파일을 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        with patch.object(TradeHistoryRepository, "delete_pending_order", side_effect=OSError("leave proof on disk")):
            self._submit_expired_order()
        repository = self.fixture.history._repository
        journal_path = repository.pending_order_storage_path
        events = [json.loads(line) for line in journal_path.read_text().splitlines()]
        events[-1]["schema_version"] = 4
        journal_path.write_text("".join(json.dumps(event) + "\n" for event in events))
        with self.assertRaises(PendingOrderJournalCorruptedError):
            TradeHistoryRepository(self.fixture.path).get_pending_order_recovery_records()

    def test_restart_rejects_exchange_order_conflicting_with_non_submission(self) -> None:
        """
        함수 이름: test_restart_rejects_exchange_order_conflicting_with_non_submission()
        기능: durable 미전송과 같은 client ID의 거래소 주문이 충돌하면 journal을 삭제하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        with patch.object(TradeHistoryRepository, "delete_pending_order", side_effect=OSError("leave proof on disk")):
            self._submit_expired_order()
        controller, history, client = self._restart()
        order = history.get_pending_order_recovery_records()[0].order
        result = OrderResult(
            symbol=order.symbol, client_order_id=order.client_order_id, exchange_order_id="1234",
            status=OrderStatus.NEW, processed_at=self.fixture.clock(),
        )
        with patch.object(client, "list_open_order_results", return_value=(result,)):
            with self.assertRaises(StartupOrderReconciliationError):
                controller.reconcile_startup_state()
        self.assertEqual(len(history.get_pending_order_recovery_records()), 1)
        self.assertEqual(client.submit_count, 0)


if __name__ == "__main__":
    unittest.main()
