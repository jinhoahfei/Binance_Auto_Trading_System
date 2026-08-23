"""Phase 9 pending-order sidecar의 durable replay와 fail-closed 경계를 검증한다."""

from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from binance_auto_trader.adapters.persistence import (
    PendingOrderJournalConflictError,
    PendingOrderJournalCorruptedError,
    TradeHistoryRepository,
    trade_history_repository as repository_module,
)
from binance_auto_trader.application.trade_history_controller import (
    TradeHistoryController,
)
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.trading.order import (
    Order,
    PendingOrderRecoveryLifecycle,
)
from binance_auto_trader.domain.trading.states import (
    ExitReason,
    OrderSide,
    StrategyType,
)


def _make_pending_order(
    *,
    client_order_id: str = "bat-case-b-buy-2",
    side: OrderSide = OrderSide.BUY,
) -> Order:
    """
    함수 이름: _make_pending_order()
    기능: sidecar round-trip에 필요한 모든 제출 전 metadata를 가진 Order를 만든다.
    인자: client_order_id -> journal replay key로 사용할 client order ID
        side -> BUY 또는 SELL 주문 방향
    반환값: 검증된 canonical Order
    작성 날짜: 2026/08/22
    """
    # SELL만 exit reason을 가져야 한다는 Order domain 불변식을 factory에서도 유지한다.
    exit_reason = ExitReason.STOP if side is OrderSide.SELL else None
    return Order(
        intent_id=f"intent-{client_order_id}",
        client_order_id=client_order_id,
        submission_attempt=2,
        symbol="ETHUSDT",
        side=side,
        strategy=StrategyType.CASE_B,
        regime_type=RegimeType.TYPE_2,
        requested_quantity=Decimal("2.50000000"),
        submitted_quantity=Decimal("2.49990000"),
        market_price_at_decision=Decimal("3123.45000000"),
        exit_reason=exit_reason,
    )  # 내부 체결 상태는 기본값인 제출 전 상태로 남긴다.


class _HistoryOnlyRepository:
    """
    클래스 이름: _HistoryOnlyRepository
    기능: optional pending-order operation이 없는 기존 history repository fake를 나타낸다.
    작성 날짜: 2026/08/22
    """

    def get_trade_history(self) -> tuple[object, ...]:
        """
        함수 이름: get_trade_history()
        기능: 기존 controller 조립 계약을 만족하는 빈 거래 tuple을 반환한다.
        인자: 없음
        반환값: 빈 tuple
        작성 날짜: 2026/08/22
        """
        return ()  # pending-order capability와 무관한 기존 history operation이다.


class PendingOrderRecoveryRepositoryTests(unittest.TestCase):
    """
    클래스 이름: PendingOrderRecoveryRepositoryTests
    기능: 별도 event journal의 저장, 재시작 replay, 제거와 손상 차단을 검증한다.
    작성 날짜: 2026/08/22
    """

    def setUp(self) -> None:
        """
        함수 이름: setUp()
        기능: 각 test에 격리된 history 경로와 concrete repository를 준비한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # test마다 독립 directory를 사용해 event 순서와 file 존재 여부를 정확히 관찰한다.
        self.temporary_directory = TemporaryDirectory()
        self.history_path = Path(self.temporary_directory.name) / "trades.jsonl"
        self.repository = TradeHistoryRepository(self.history_path)

    def tearDown(self) -> None:
        """
        함수 이름: tearDown()
        기능: test가 만든 임시 history와 sidecar 파일을 모두 정리한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self.temporary_directory.cleanup()  # TemporaryDirectory 범위 밖에 복구 metadata를 남기지 않는다.

    def test_upsert_uses_separate_sidecar_and_round_trips_only_metadata(
        self,
    ) -> None:
        """
        함수 이름: test_upsert_uses_separate_sidecar_and_round_trips_only_metadata()
        기능: history bytes를 보존하면서 exact Order metadata만 재시작 복원하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        order = _make_pending_order(side=OrderSide.SELL)
        original_history = b"existing-history-bytes\n"
        self.history_path.write_bytes(original_history)

        # pending save가 기존 거래 이력 parser나 파일 내용을 건드리지 않는지 먼저 확인한다.
        self.repository.save_pending_order(order)
        sidecar_path = self.repository.pending_order_storage_path
        self.assertNotEqual(sidecar_path, self.history_path)
        self.assertEqual(self.history_path.read_bytes(), original_history)

        encoded_event = sidecar_path.read_text(encoding="utf-8").strip()
        decoded_event = json.loads(encoded_event)
        self.assertEqual(decoded_event["operation"], "UPSERT")
        self.assertEqual(
            frozenset(decoded_event["order"]),
            frozenset(
                {
                    "client_order_id",
                    "exit_reason",
                    "intent_id",
                    "market_price_at_decision",
                    "regime_type",
                    "requested_quantity",
                    "side",
                    "strategy",
                    "submission_attempt",
                    "submitted_quantity",
                    "symbol",
                }
            ),
        )
        self.assertNotIn("credential", encoded_event.lower())
        self.assertNotIn("raw_response", encoded_event.lower())

        restarted_repository = TradeHistoryRepository(self.history_path)
        self.assertEqual(restarted_repository.get_pending_orders(), (order,))

    def test_duplicate_upsert_replay_returns_one_active_order(self) -> None:
        """
        함수 이름: test_duplicate_upsert_replay_returns_one_active_order()
        기능: crash 재시도로 동일 UPSERT line이 반복되어도 active Order 하나만 복원하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        order = _make_pending_order()
        self.repository.save_pending_order(order)
        sidecar_path = self.repository.pending_order_storage_path
        first_event = sidecar_path.read_bytes()
        self.repository.save_pending_order(order)
        self.assertEqual(sidecar_path.read_bytes(), first_event)

        # 이미 durable한 동일 UPSERT가 재기록된 crash 상황을 raw journal에서 재현한다.
        with sidecar_path.open("ab") as sidecar_file:
            sidecar_file.write(first_event)

        restarted_repository = TradeHistoryRepository(self.history_path)
        self.assertEqual(restarted_repository.get_pending_orders(), (order,))

    def test_submission_rejection_transition_is_durable_and_idempotent(
        self,
    ) -> None:
        """
        함수 이름: test_submission_rejection_transition_is_durable_and_idempotent()
        기능: typed 제출 거부 lifecycle이 한 번 fsync되고 재시작 record에 복원되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        order = _make_pending_order()
        self.repository.save_pending_order(order)
        original_fsync = repository_module.os.fsync

        # 같은 거부를 두 번 기록해도 PREPARED 뒤 TRANSITION 하나만 durable append한다.
        with patch.object(
            repository_module.os,
            "fsync",
            wraps=original_fsync,
        ) as fsync_spy:
            self.repository.mark_pending_order_submission_rejected(
                order.client_order_id
            )
            self.repository.mark_pending_order_submission_rejected(
                order.client_order_id
            )
        self.assertEqual(fsync_spy.call_count, 2)  # 첫 transition의 file과 directory만 sync한다.
        event_lines = self.repository.pending_order_storage_path.read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(len(event_lines), 2)
        self.assertEqual(json.loads(event_lines[0])["schema_version"], 2)
        self.assertEqual(json.loads(event_lines[0])["lifecycle"], "PREPARED")
        self.assertEqual(json.loads(event_lines[1])["operation"], "TRANSITION")

        restarted_repository = TradeHistoryRepository(self.history_path)
        records = restarted_repository.get_pending_order_recovery_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].order, order)
        self.assertIs(
            records[0].lifecycle,
            PendingOrderRecoveryLifecycle.SUBMISSION_REJECTED_CONFIRMED,
        )

    def test_legacy_schema_one_upsert_replays_as_prepared(self) -> None:
        """
        함수 이름: test_legacy_schema_one_upsert_replays_as_prepared()
        기능: Phase 9 초기 sidecar v1 UPSERT를 거부 증거 없는 PREPARED로 호환 복원한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        order = _make_pending_order()
        self.repository.save_pending_order(order)
        sidecar_path = self.repository.pending_order_storage_path
        current_event = json.loads(sidecar_path.read_text(encoding="utf-8"))
        current_event["schema_version"] = 1
        del current_event["lifecycle"]
        sidecar_path.write_text(
            json.dumps(current_event, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        # Legacy record에는 제출 거부 증거가 없으므로 부재 삭제 권한을 절대 부여하지 않는다.
        restarted_repository = TradeHistoryRepository(self.history_path)
        records = restarted_repository.get_pending_order_recovery_records()
        self.assertEqual(len(records), 1)
        self.assertIs(
            records[0].lifecycle,
            PendingOrderRecoveryLifecycle.PREPARED,
        )

    def test_conflicting_upsert_for_same_client_id_fails_before_append(
        self,
    ) -> None:
        """
        함수 이름: test_conflicting_upsert_for_same_client_id_fails_before_append()
        기능: active client ID를 다른 Order metadata로 다시 저장하면 원 journal을 보존하고 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        original_order = _make_pending_order()
        conflicting_order = _make_pending_order()
        conflicting_order.submitted_quantity = Decimal("2.49980000")
        self.repository.save_pending_order(original_order)
        original_sidecar = self.repository.pending_order_storage_path.read_bytes()

        # 같은 client ID의 다른 제출 수량은 새 UPSERT를 쓰거나 memory index를 교체하면 안 된다.
        with self.assertRaises(PendingOrderJournalConflictError) as raised:
            self.repository.save_pending_order(conflicting_order)

        self.assertEqual(
            raised.exception.client_order_id,
            original_order.client_order_id,
        )
        self.assertEqual(
            self.repository.pending_order_storage_path.read_bytes(),
            original_sidecar,
        )
        self.assertEqual(self.repository.get_pending_orders(), (original_order,))

    def test_conflicting_upsert_for_same_client_id_fails_replay(
        self,
    ) -> None:
        """
        함수 이름: test_conflicting_upsert_for_same_client_id_fails_replay()
        기능: sidecar의 같은 client ID가 다른 Order payload로 반복되면 마지막 값을 채택하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        original_order = _make_pending_order()
        self.repository.save_pending_order(original_order)
        sidecar_path = self.repository.pending_order_storage_path
        conflicting_event = json.loads(
            sidecar_path.read_text(encoding="utf-8")
        )
        conflicting_event["order"]["submitted_quantity"] = "2.49980000"

        # 완전한 두 번째 line도 동일 idempotency key의 payload 충돌이면 손상으로 분류한다.
        with sidecar_path.open("a", encoding="utf-8", newline="") as sidecar_file:
            sidecar_file.write(
                json.dumps(conflicting_event, separators=(",", ":")) + "\n"
            )

        restarted_repository = TradeHistoryRepository(self.history_path)
        with self.assertRaises(PendingOrderJournalCorruptedError) as raised:
            restarted_repository.get_pending_orders()
        self.assertEqual(raised.exception.line_number, 2)
        self.assertEqual(
            raised.exception.reason,
            PendingOrderJournalConflictError.__name__,
        )
        self.assertIsNone(raised.exception.__cause__)

    def test_upsert_and_remove_fsync_file_and_parent_directory(self) -> None:
        """
        함수 이름: test_upsert_and_remove_fsync_file_and_parent_directory()
        기능: 각 mutation event가 sidecar file과 directory entry를 모두 fsync하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        order = _make_pending_order()
        original_fsync = repository_module.os.fsync

        # 실제 fsync를 유지한 spy로 UPSERT와 REMOVE의 두 durability 경계를 각각 센다.
        with patch.object(
            repository_module.os,
            "fsync",
            wraps=original_fsync,
        ) as fsync_spy:
            self.repository.save_pending_order(order)
            self.repository.delete_pending_order(order.client_order_id)

        self.assertEqual(fsync_spy.call_count, 4)  # event마다 file과 parent directory를 한 번씩 sync한다.

    def test_remove_is_idempotent_and_replays_to_empty_state(self) -> None:
        """
        함수 이름: test_remove_is_idempotent_and_replays_to_empty_state()
        기능: 같은 client ID의 반복 REMOVE 호출이 한 tombstone과 빈 active state로 수렴하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        order = _make_pending_order()
        self.repository.save_pending_order(order)

        # 첫 REMOVE만 durable event를 만들고 두 번째 호출은 명시적인 멱등 no-op이어야 한다.
        self.repository.delete_pending_order(order.client_order_id)
        self.repository.delete_pending_order(order.client_order_id)
        event_lines = self.repository.pending_order_storage_path.read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(len(event_lines), 2)
        self.assertEqual(json.loads(event_lines[1])["operation"], "REMOVE")

        restarted_repository = TradeHistoryRepository(self.history_path)
        self.assertEqual(restarted_repository.get_pending_orders(), ())

    def test_unknown_metadata_field_fails_closed_without_secret_echo(self) -> None:
        """
        함수 이름: test_unknown_metadata_field_fails_closed_without_secret_echo()
        기능: raw-response형 미지 필드가 있는 sidecar를 무시하지 않고 안전한 오류로 차단하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        order = _make_pending_order()
        self.repository.save_pending_order(order)
        sidecar_path = self.repository.pending_order_storage_path
        decoded_event = json.loads(sidecar_path.read_text(encoding="utf-8"))
        secret_canary = "should-never-appear-in-an-exception"
        decoded_event["order"]["raw_response"] = {"api_key": secret_canary}
        sidecar_path.write_text(
            json.dumps(decoded_event, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        # fail-closed 오류는 손상 line과 안전한 타입만 노출하고 record 원문은 포함하지 않는다.
        restarted_repository = TradeHistoryRepository(self.history_path)
        with self.assertRaises(PendingOrderJournalCorruptedError) as raised:
            restarted_repository.get_pending_orders()
        self.assertEqual(raised.exception.line_number, 1)
        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn(secret_canary, str(raised.exception))

    def test_controller_reports_optional_capability_and_delegates_operations(
        self,
    ) -> None:
        """
        함수 이름: test_controller_reports_optional_capability_and_delegates_operations()
        기능: history-only fake는 false이고 concrete controller는 세 recovery operation을 제공하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        history_only_controller = TradeHistoryController(_HistoryOnlyRepository())
        self.assertFalse(history_only_controller.supports_pending_order_recovery)
        with self.assertRaises(NotImplementedError):
            history_only_controller.get_pending_orders()

        # concrete controller는 같은 method 이름으로 durable repository 경계를 그대로 조정한다.
        controller = TradeHistoryController(self.repository)
        order = _make_pending_order()
        self.assertTrue(controller.supports_pending_order_recovery)
        controller.save_pending_order(order)
        self.assertEqual(controller.get_pending_orders(), (order,))
        controller.mark_pending_order_submission_rejected(
            order.client_order_id
        )
        self.assertIs(
            controller.get_pending_order_recovery_records()[0].lifecycle,
            PendingOrderRecoveryLifecycle.SUBMISSION_REJECTED_CONFIRMED,
        )
        controller.delete_pending_order(order.client_order_id)
        self.assertEqual(controller.get_pending_orders(), ())


if __name__ == "__main__":
    unittest.main()
