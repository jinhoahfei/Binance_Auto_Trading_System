"""Phase 13 manual kill control journal의 strict durability와 restart replay를 검증한다."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from binance_auto_trader.adapters.persistence import (
    ManualKillControlJournalCorruptedError,
    TradeHistoryRepository,
)
from binance_auto_trader.application import TradeHistoryController
from binance_auto_trader.domain.trading import (
    ManualKillBehavior,
    ManualKillControlState,
)


class ManualKillControlRepositoryTests(unittest.TestCase):
    """
    클래스 이름: ManualKillControlRepositoryTests
    기능: manual kill state가 별도 strict JSONL에서 단조·fail-closed로 복원되는지 검증한다.
    작성 날짜: 2026/08/29
    """

    def setUp(self) -> None:
        """
        함수 이름: setUp()
        기능: 각 테스트에 격리된 history와 manual kill journal 경로를 준비한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        self.temporary_directory = TemporaryDirectory()
        self.history_path = Path(self.temporary_directory.name) / "trades.jsonl"
        self.repository = TradeHistoryRepository(self.history_path)

    def tearDown(self) -> None:
        """
        함수 이름: tearDown()
        기능: 테스트가 만든 임시 journal과 directory를 정리한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        self.temporary_directory.cleanup()  # 테스트 밖에 operator control artifact를 남기지 않는다.

    def test_missing_journal_is_inactive_without_creating_a_file(self) -> None:
        """
        함수 이름: test_missing_journal_is_inactive_without_creating_a_file()
        기능: 최초 startup이 inactive version 0을 반환하되 read-only 조회로 파일을 만들지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        state = self.repository.get_manual_kill_control_state()

        # 파일 부재만 canonical 초기 상태이고 단순 조회는 directory mutation을 만들지 않는다.
        self.assertEqual(ManualKillControlState(), state)
        self.assertFalse(self.repository.manual_kill_control_storage_path.exists())

    def test_toggle_states_survive_fresh_repository_and_controller(self) -> None:
        """
        함수 이름: test_toggle_states_survive_fresh_repository_and_controller()
        기능: activation과 deactivation이 새 repository/controller에서 같은 version으로 복원되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        active_state = ManualKillControlState(
            active=True,
            version=1,
            command_id="activate-manual-kill",
            expected_version=0,
        )
        self.repository.save_manual_kill_control_state(active_state)

        # 새 adapter와 application Controller가 동일한 durable activation을 각각 읽어야 한다.
        restarted_repository = TradeHistoryRepository(self.history_path)
        self.assertEqual(
            active_state,
            restarted_repository.get_manual_kill_control_state(),
        )
        history_controller = TradeHistoryController(restarted_repository)
        self.assertTrue(history_controller.supports_manual_kill_control_recovery)
        self.assertEqual(
            active_state,
            history_controller.get_manual_kill_control_state(),
        )

        # 해제도 version 2 toggle로 기록되고 세 번째 process가 inactive 상태를 복원한다.
        inactive_state = ManualKillControlState(
            active=False,
            version=2,
            command_id="release-manual-kill",
            expected_version=1,
        )
        history_controller.save_manual_kill_control_state(inactive_state)
        final_repository = TradeHistoryRepository(self.history_path)
        self.assertEqual(
            inactive_state,
            final_repository.get_manual_kill_control_state(),
        )
        self.assertEqual(
            2,
            len(
                final_repository.manual_kill_control_storage_path
                .read_text(encoding="utf-8")
                .splitlines()
            ),
        )

    def test_nonconsecutive_or_non_toggling_state_is_rejected(self) -> None:
        """
        함수 이름: test_nonconsecutive_or_non_toggling_state_is_rejected()
        기능: version 건너뛰기와 상태 변화 없는 version 증가가 journal에 기록되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        self.repository.save_manual_kill_control_state(
            ManualKillControlState(
                active=True,
                version=1,
                command_id="activate-before-invalid",
                expected_version=0,
            )
        )
        journal_before_invalid_requests = (
            self.repository.manual_kill_control_storage_path.read_bytes()
        )

        # 두 invalid mutation 모두 append 전에 거부되어 기존 durable activation을 보존한다.
        with self.assertRaises(ValueError):
            self.repository.save_manual_kill_control_state(
                ManualKillControlState(
                    active=False,
                    version=3,
                    command_id="invalid-version-jump",
                    expected_version=2,
                )
            )
        with self.assertRaises(ValueError):
            self.repository.save_manual_kill_control_state(
                ManualKillControlState(
                    active=True,
                    version=2,
                    command_id="invalid-no-toggle",
                    expected_version=1,
                )
            )
        self.assertEqual(
            journal_before_invalid_requests,
            self.repository.manual_kill_control_storage_path.read_bytes(),
        )

    def test_corrupt_or_partial_journal_never_falls_back_to_inactive(self) -> None:
        """
        함수 이름: test_corrupt_or_partial_journal_never_falls_back_to_inactive()
        기능: duplicate key와 partial tail이 재시작에서 inactive 기본값으로 완화되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        corrupt_records = (
            (
                b'{"record_type":"manual_kill_control","schema_version":1,'
                b'"active":true,"active":false,"version":1}\n'
            ),
            (
                json.dumps(
                    {
                        "record_type": "manual_kill_control",
                        "schema_version": 1,
                        "active": True,
                        "version": 1,
                        "command_id": "partial-activation",
                        "expected_version": 0,
                        "behavior": None,
                        "policy_version": None,
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
            ),
        )

        # 각 손상은 별도 fresh repository에서 typed corruption으로 중단돼 state를 공개하지 않는다.
        for corrupt_record in corrupt_records:
            with self.subTest(corrupt_record=corrupt_record):
                journal_path = self.repository.manual_kill_control_storage_path
                journal_path.write_bytes(corrupt_record)
                with self.assertRaises(
                    ManualKillControlJournalCorruptedError
                ):
                    TradeHistoryRepository(
                        self.history_path
                    ).get_manual_kill_control_state()

    def test_existing_empty_journal_is_corruption_not_inactive(self) -> None:
        """
        함수 이름: test_existing_empty_journal_is_corruption_not_inactive()
        기능: 존재하는 0-byte journal이 active state 손실을 inactive 기본값으로 완화하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        journal_path = self.repository.manual_kill_control_storage_path
        journal_path.write_bytes(b"")

        # 경로 부재와 존재하는 빈 artifact를 구분해 재시작을 typed corruption으로 중단한다.
        with self.assertRaises(ManualKillControlJournalCorruptedError) as context:
            TradeHistoryRepository(
                self.history_path
            ).get_manual_kill_control_state()
        self.assertEqual("EmptyJournal", context.exception.reason)

    def test_legacy_toggle_and_v2_noop_receipt_replay_together(self) -> None:
        """
        함수 이름: test_legacy_toggle_and_v2_noop_receipt_replay_together()
        기능: v1 toggle을 호환 복원한 뒤 v2 no-op command도 state 변경 없이 durable 보존하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        legacy_activation = ManualKillControlState(
            active=True,
            version=1,
            command_id="legacy-activation",
            expected_version=0,
        )
        legacy_record = {
            "record_type": "manual_kill_control",
            "schema_version": 1,
            "active": True,
            "version": 1,
            "command_id": "legacy-activation",
            "expected_version": 0,
            "behavior": None,
            "policy_version": None,
        }
        journal_path = self.repository.manual_kill_control_storage_path
        journal_path.write_text(
            json.dumps(legacy_record, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        restarted_repository = TradeHistoryRepository(self.history_path)
        self.assertEqual(
            legacy_activation,
            restarted_repository.get_manual_kill_control_state(),
        )

        # 같은 active/version의 새 command도 v2 receipt로 fsync해 restart ID 의미를 잃지 않는다.
        noop_receipt = ManualKillControlState(
            active=True,
            version=1,
            command_id="confirm-active",
            expected_version=1,
        )
        restarted_repository.save_manual_kill_control_state(noop_receipt)
        final_repository = TradeHistoryRepository(self.history_path)
        self.assertEqual(noop_receipt, final_repository.get_manual_kill_control_state())
        self.assertEqual(
            (legacy_activation, noop_receipt),
            final_repository.get_manual_kill_control_replay(),
        )
        schema_versions = tuple(
            json.loads(line)["schema_version"]
            for line in journal_path.read_text(encoding="utf-8").splitlines()
        )
        self.assertEqual((1, 2), schema_versions)

    def test_command_id_cannot_change_payload_in_bounded_replay(self) -> None:
        """
        함수 이름: test_command_id_cannot_change_payload_in_bounded_replay()
        기능: durable replay 범위의 command ID가 반대 active payload로 재사용되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        activation = ManualKillControlState(
            active=True,
            version=1,
            command_id="stable-command-id",
            expected_version=0,
        )
        self.repository.save_manual_kill_control_state(activation)
        journal_before_reuse = (
            self.repository.manual_kill_control_storage_path.read_bytes()
        )

        # 같은 ID의 다른 fingerprint/result는 append와 in-memory state 변경 전에 거부한다.
        with self.assertRaises(ValueError):
            self.repository.save_manual_kill_control_state(
                ManualKillControlState(
                    active=False,
                    version=2,
                    command_id="stable-command-id",
                    expected_version=1,
                )
            )
        self.assertEqual(
            journal_before_reuse,
            self.repository.manual_kill_control_storage_path.read_bytes(),
        )
        self.assertEqual(activation, self.repository.get_manual_kill_control_state())

    def test_active_noop_cannot_rewrite_cleanup_policy_provenance(self) -> None:
        """
        함수 이름: test_active_noop_cannot_rewrite_cleanup_policy_provenance()
        기능: 활성 epoch의 no-op receipt가 cleanup 행동이나 policy version을 바꾸지 못하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        activation = ManualKillControlState(
            active=True,
            version=1,
            command_id="activate-cancel-and-liquidate",
            expected_version=0,
            behavior=ManualKillBehavior.CANCEL_AND_LIQUIDATE,
            policy_version=1,
        )
        self.repository.save_manual_kill_control_state(activation)
        journal_before_noop = (
            self.repository.manual_kill_control_storage_path.read_bytes()
        )

        # Policy hot-swap 뒤 no-op처럼 보이는 receipt도 최초 activation 의무를 덮어쓰면 거부한다.
        with self.assertRaises(ValueError):
            self.repository.save_manual_kill_control_state(
                ManualKillControlState(
                    active=True,
                    version=1,
                    command_id="rewrite-active-policy",
                    expected_version=1,
                    behavior=ManualKillBehavior.BLOCK_NEW_ORDERS,
                    policy_version=2,
                )
            )

        self.assertEqual(
            journal_before_noop,
            self.repository.manual_kill_control_storage_path.read_bytes(),
        )
        self.assertEqual(
            activation,
            self.repository.get_manual_kill_control_state(),
        )

    def test_cached_repository_rejects_runtime_journal_rollback(
        self,
    ) -> None:
        """
        함수 이름: test_cached_repository_rejects_runtime_journal_rollback()
        기능: 실행 중 empty·missing·valid-prefix rollback이 cached receipt 뒤에 append되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        for mutation_kind in ("empty", "missing", "valid-prefix"):
            with self.subTest(mutation_kind=mutation_kind):
                with TemporaryDirectory() as temporary_directory:
                    history_path = Path(temporary_directory) / "trades.jsonl"
                    repository = TradeHistoryRepository(history_path)
                    repository.save_manual_kill_control_state(
                        ManualKillControlState(
                            active=True,
                            version=1,
                            command_id="runtime-activation",
                            expected_version=0,
                        )
                    )
                    journal_path = repository.manual_kill_control_storage_path
                    valid_prefix = journal_path.read_bytes()
                    repository.save_manual_kill_control_state(
                        ManualKillControlState(
                            active=True,
                            version=1,
                            command_id="runtime-active-noop",
                            expected_version=1,
                        )
                    )

                    # 상태/version이 같은 valid prefix도 no-op provenance를 잃었으므로 drift다.
                    if mutation_kind == "empty":
                        expected_bytes: bytes | None = b""
                        journal_path.write_bytes(expected_bytes)
                    elif mutation_kind == "missing":
                        expected_bytes = None
                        journal_path.unlink()
                    else:
                        expected_bytes = valid_prefix
                        journal_path.write_bytes(expected_bytes)

                    with self.assertRaises(
                        ManualKillControlJournalCorruptedError
                    ):
                        repository.save_manual_kill_control_state(
                            ManualKillControlState(
                                active=False,
                                version=2,
                                command_id="release-after-runtime-drift",
                                expected_version=1,
                            )
                        )

                    # 손상 뒤 release line을 새로 만들지 않아 다음 startup도 문제를 명시적으로 본다.
                    if expected_bytes is None:
                        self.assertFalse(journal_path.exists())
                    else:
                        self.assertEqual(expected_bytes, journal_path.read_bytes())

    def test_shutdown_barrier_rejects_unlinked_cached_journal(self) -> None:
        """
        함수 이름: test_shutdown_barrier_rejects_unlinked_cached_journal()
        기능: active receipt의 실행 중 unlink가 정상 shutdown durability 확인을 우회하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        active_state = ManualKillControlState(
            active=True,
            version=1,
            command_id="active-before-shutdown-unlink",
            expected_version=0,
        )
        self.repository.save_manual_kill_control_state(active_state)
        journal_path = self.repository.manual_kill_control_storage_path
        journal_path.unlink()
        history_controller = TradeHistoryController(self.repository)

        # Cached active receipt가 있으면 path 부재도 strict get을 호출해 정상 종료를 거부한다.
        with self.assertRaises(ManualKillControlJournalCorruptedError) as context:
            history_controller.flush_durable_state()
        self.assertEqual("MissingJournal", context.exception.reason)
        self.assertFalse(journal_path.exists())

        # 새 process는 부재를 최초 inactive로 볼 수 있으므로 이전 process의 종료 ACK가 나가면 안 된다.
        fresh_repository = TradeHistoryRepository(self.history_path)
        self.assertEqual(
            ManualKillControlState(),
            fresh_repository.get_manual_kill_control_state(),
        )


if __name__ == "__main__":
    unittest.main()
