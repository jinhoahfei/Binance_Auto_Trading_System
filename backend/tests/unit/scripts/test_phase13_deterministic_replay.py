"""Phase 13 canonical fault replay의 schema, domain 결과와 byte 안정성을 검증한다."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch as mock_patch
from uuid import UUID


# Backend discovery에서도 repository-root scripts namespace를 import할 수 있게 test 경계만 보강한다.
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.phase13_deterministic_replay import (  # noqa: E402
    EVIDENCE_SCOPE,
    FAULT_POLICIES,
    DeterministicReplayError,
    main,
    replay_trace,
    replay_trace_bytes,
)


TRACE_PATH = (
    REPOSITORY_ROOT
    / "backend"
    / "tests"
    / "fixtures"
    / "phase13"
    / "canonical_fault_trace.json"
)


class PhaseThirteenDeterministicReplayTests(unittest.TestCase):
    """
    클래스 이름: PhaseThirteenDeterministicReplayTests
    기능: P13-05 전체 fault matrix와 offline replay evidence의 fail-closed 계약을 검증한다.
    작성 날짜: 2026/08/25
    """

    def _load_mutable_fixture(self) -> dict[str, object]:
        """
        함수 이름: _load_mutable_fixture()
        기능: tamper negative test에 사용할 canonical fixture 사본을 JSON으로 읽는다.
        인자: 없음
        반환값: 변경 가능한 fixture dictionary
        작성 날짜: 2026/08/25
        """
        loaded_value = json.loads(TRACE_PATH.read_text(encoding="utf-8"))
        if not isinstance(loaded_value, dict):
            self.fail("canonical fixture root must be an object")

        return loaded_value  # test별 deepcopy 없이도 매번 새 JSON tree를 반환한다.

    def _write_temporary_fixture(
        self,
        temporary_directory: str,
        fixture: dict[str, object],
        *,
        file_name: str = "tampered.json",
    ) -> Path:
        """
        함수 이름: _write_temporary_fixture()
        기능: 변경한 JSON object를 격리된 temporary file에 canonical text로 기록한다.
        인자: temporary_directory -> unittest가 소유한 임시 directory
            fixture -> 기록할 JSON object
            file_name -> 임시 fixture 파일명
        반환값: 생성한 임시 fixture 경로
        작성 날짜: 2026/08/25
        """
        fixture_path = Path(temporary_directory) / file_name
        fixture_path.write_text(
            json.dumps(fixture, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        return fixture_path  # production fixture는 변경하지 않고 negative input만 격리한다.

    def test_fixture_covers_complete_fault_matrix_with_explicit_decisions(self) -> None:
        """
        함수 이름: test_fixture_covers_complete_fault_matrix_with_explicit_decisions()
        기능: 모든 P13-05 fault가 owner, state, 신규 주문 허용과 recovery action을 정확히 한 번 선언하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        fixture = self._load_mutable_fixture()
        scenarios = fixture["scenarios"]
        self.assertIsInstance(scenarios, list)
        fault_kinds = [scenario["fault_kind"] for scenario in scenarios]

        # 누락과 중복을 set 및 길이로 함께 검사해 fault 행 하나당 결정표 한 행만 허용한다.
        self.assertEqual(set(fault_kinds), set(FAULT_POLICIES))
        self.assertEqual(len(fault_kinds), len(set(fault_kinds)))
        self.assertEqual(fixture["evidence_scope"], EVIDENCE_SCOPE)
        for scenario in scenarios:
            policy = FAULT_POLICIES[scenario["fault_kind"]]
            self.assertEqual(scenario["category"], policy.category)
            self.assertEqual(
                scenario["expected"],
                {
                    "owner": policy.owner,
                    "state": policy.state,
                    "new_order_allowed": policy.new_order_allowed,
                    "recovery_action": policy.recovery_action,
                },
            )

    def test_repeated_replay_is_byte_stable_and_matches_recorded_digest(self) -> None:
        """
        함수 이름: test_repeated_replay_is_byte_stable_and_matches_recorded_digest()
        기능: 같은 canonical trace를 반복 실행한 JSON bytes와 aggregate digest가 모두 일치하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        # 반복 횟수가 다른 두 replay 결과와 원본 fixture를 독립적으로 불러온다.
        first_bytes = replay_trace_bytes(TRACE_PATH, repeat=7)
        second_bytes = replay_trace_bytes(TRACE_PATH, repeat=3)
        fixture = self._load_mutable_fixture()  # 기록된 기준 digest를 제공한다.
        replay_object = json.loads(first_bytes)

        # Canonical bytes와 fixture-bound digest가 반복 횟수에 흔들리지 않는지 함께 확인한다.
        self.assertEqual(first_bytes, second_bytes)
        self.assertTrue(first_bytes.endswith(b"\n"))
        self.assertEqual(
            replay_object["normalized_digest"],
            fixture["expected_replay_digest"],
        )
        self.assertEqual(replay_object["evidence_scope"], EVIDENCE_SCOPE)

    def test_public_domain_replay_deduplicates_and_preserves_trade_boundaries(self) -> None:
        """
        함수 이름: test_public_domain_replay_deduplicates_and_preserves_trade_boundaries()
        기능: duplicate/out-of-order fill, history 실패와 REMOVE 실패가 공개 domain 결과로 구분되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        replay_object = replay_trace(TRACE_PATH)
        scenarios = {
            scenario["scenario_id"]: scenario
            for scenario in replay_object["scenarios"]
        }

        # duplicate와 stale active result 뒤에도 같은 주문 mutation과 Trade는 하나만 남는다.
        for scenario_id in ("ws_duplicate_event", "ws_out_of_order_event"):
            scenario = scenarios[scenario_id]
            self.assertEqual(scenario["order_mutation_count"], 1)
            self.assertEqual(scenario["order"]["status"], "FILLED")
            self.assertEqual(scenario["position"]["quantity"], "1")
            self.assertEqual(len(scenario["trades"]), 1)
            self.assertEqual(scenario["order"]["unapplied_fill_count"], 0)

        # History append 실패는 Position만 남고, REMOVE 실패는 이미 durable한 Trade를 보존한다.
        history_failure = scenarios["history_append_failure"]
        remove_failure = scenarios["pending_journal_remove_failure"]
        self.assertEqual(history_failure["position"]["quantity"], "0.01")
        self.assertEqual(history_failure["trades"], [])
        self.assertEqual(remove_failure["position"]["quantity"], "0.01")
        self.assertEqual(len(remove_failure["trades"]), 1)

        # ETH fee는 gross fill과 분리된 net Position 수량으로 replay되어 dust mismatch를 숨기지 않는다.
        fee_mismatch = scenarios["fee_dust_mismatch"]
        self.assertEqual(fee_mismatch["order"]["filled_quantity"], "1")
        self.assertEqual(fee_mismatch["position"]["quantity"], "0.999")

    def test_offline_scope_records_no_network_or_private_action_calls(self) -> None:
        """
        함수 이름: test_offline_scope_records_no_network_or_private_action_calls()
        기능: replay가 network mutation이나 production Controller private Action 성공 증거를 만들지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        replay_object = replay_trace(TRACE_PATH)  # 공개 offline trace만 재생한다.

        # 모든 fault scenario가 외부 mutation 없이 단일 mutation/trade 상한을 지키는지 검사한다.
        for scenario in replay_object["scenarios"]:
            self.assertEqual(scenario["evidence_scope"], EVIDENCE_SCOPE)
            self.assertEqual(scenario["network_mutation_port_calls"], 0)
            self.assertEqual(scenario["private_action_calls"], 0)
            self.assertLessEqual(scenario["order_mutation_count"], 1)
            self.assertLessEqual(len(scenario["trades"]), 1)
        scenario_by_id = {  # Crash 경계 전후의 기대 mutation 차이를 조회한다.
            scenario["scenario_id"]: scenario
            for scenario in replay_object["scenarios"]
        }
        self.assertEqual(scenario_by_id["crash_before_submit"]["order_mutation_count"], 0)
        self.assertEqual(scenario_by_id["crash_after_submit"]["order_mutation_count"], 1)

    def test_replay_does_not_depend_on_network_or_host_monotonic_clock(self) -> None:
        """
        함수 이름: test_replay_does_not_depend_on_network_or_host_monotonic_clock()
        기능: socket 연결과 host monotonic clock을 실패시켜도 주입 clock replay가 완료되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        def reject_network(*_arguments: object, **_keyword_arguments: object) -> socket.socket:
            """
            함수 이름: reject_network()
            기능: offline replay 중 발생한 예상 밖 network 연결을 즉시 실패시킨다.
            인자: _arguments -> 사용하지 않는 positional 인자
                _keyword_arguments -> 사용하지 않는 keyword 인자
            반환값: 정상 반환하지 않음
            작성 날짜: 2026/08/25
            """
            raise AssertionError("offline replay attempted a network connection")  # 외부 연결을 차단한다.

        def reject_host_clock() -> float:
            """
            함수 이름: reject_host_clock()
            기능: replay가 주입 monotonic clock 대신 host clock을 읽으면 즉시 실패시킨다.
            인자: 없음
            반환값: 정상 반환하지 않음
            작성 날짜: 2026/08/25
            """
            raise AssertionError("offline replay read the host monotonic clock")  # 비결정 clock을 차단한다.

        # Network와 host clock을 모두 실패 seam으로 바꿔 replay의 주입 경계를 검증한다.
        with (
            mock_patch("socket.create_connection", side_effect=reject_network),
            mock_patch(
                "binance_auto_trader.transport.event_stream.monotonic",
                side_effect=reject_host_clock,
            ),
        ):
            replay_bytes = replay_trace_bytes(TRACE_PATH, repeat=2)

        self.assertIn(  # 차단 seam 아래에서도 canonical replay가 완성되어야 한다.
            b'"record_type":"phase13_deterministic_replay"', replay_bytes
        )

    def test_random_event_ids_are_explicitly_normalized_from_digest(self) -> None:
        """
        함수 이름: test_random_event_ids_are_explicitly_normalized_from_digest()
        기능: 서로 다른 event UUID source가 normalized UI sequence bytes를 바꾸지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        # 서로 다른 임의 UUID source를 같은 canonical trace에 차례로 주입한다.
        first_uuid = UUID("00000000-0000-4000-8000-000000000101")
        second_uuid = UUID("00000000-0000-4000-8000-000000000202")
        with mock_patch(
            "binance_auto_trader.transport.event_stream.uuid4",
            return_value=first_uuid,
        ):
            first_bytes = replay_trace_bytes(TRACE_PATH)
        with mock_patch(
            "binance_auto_trader.transport.event_stream.uuid4",
            return_value=second_uuid,
        ):
            second_bytes = replay_trace_bytes(TRACE_PATH)

        self.assertEqual(first_bytes, second_bytes)  # UUID는 digest 입력에서 정규화되어야 한다.
        self.assertNotIn(str(first_uuid).encode("ascii"), first_bytes)
        self.assertNotIn(str(second_uuid).encode("ascii"), second_bytes)

    def test_policy_or_digest_tampering_is_rejected(self) -> None:
        """
        함수 이름: test_policy_or_digest_tampering_is_rejected()
        기능: 신규 주문 허용 decision 또는 expected digest 변경이 정상 replay로 통과하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            # 정책 결과를 변조한 fixture를 써서 decision table 결속을 먼저 검증한다.
            policy_fixture = self._load_mutable_fixture()
            policy_fixture["scenarios"][0]["expected"]["new_order_allowed"] = True
            policy_path = self._write_temporary_fixture(
                temporary_directory,
                policy_fixture,
                file_name="policy-tamper.json",
            )
            with self.assertRaisesRegex(DeterministicReplayError, "fault policy"):
                replay_trace(policy_path)

            # 정상 정책을 유지한 채 scenario digest만 변조해 byte 결속도 별도로 검증한다.
            digest_fixture = self._load_mutable_fixture()
            digest_fixture["scenarios"][0]["expected_normalized_digest"] = "a" * 64
            digest_path = self._write_temporary_fixture(
                temporary_directory,
                digest_fixture,
                file_name="digest-tamper.json",
            )
            with self.assertRaisesRegex(DeterministicReplayError, "digest mismatched"):
                replay_trace(digest_path)

    def test_fault_gate_rejects_submit_after_new_orders_are_blocked(self) -> None:
        """
        함수 이름: test_fault_gate_rejects_submit_after_new_orders_are_blocked()
        기능: fault policy가 신규 주문을 닫은 뒤 fixture가 새 submit mutation을 삽입하지 못하게 한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = self._load_mutable_fixture()
            scenarios = fixture["scenarios"]
            source_order = deepcopy(scenarios[0]["input_trace"][0])
            blocked_scenario = next(
                scenario
                for scenario in scenarios
                if scenario["scenario_id"] == "ws_disconnect_reconnect"
            )

            # Fault 이후 새 intent와 submit을 넣어 replay가 digest 비교 전에 gate에서 거부하는지 확인한다.
            blocked_scenario["input_trace"].extend(
                (source_order, {"operation": "SUBMIT_ORDER"})
            )
            fixture_path = self._write_temporary_fixture(
                temporary_directory,
                fixture,
                file_name="blocked-submit.json",
            )

            with self.assertRaisesRegex(DeterministicReplayError, "new orders are blocked"):
                replay_trace(fixture_path)

    def test_duplicate_keys_floats_and_secret_like_fields_are_rejected(self) -> None:
        """
        함수 이름: test_duplicate_keys_floats_and_secret_like_fields_are_rejected()
        기능: ambiguous JSON, float 금융 입력과 secret-like fixture 확장을 모두 fail-closed하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            # 중복 key JSON을 raw text로 작성해 decoder overwrite 경계를 검사한다.
            duplicate_path = Path(temporary_directory) / "duplicate.json"
            duplicate_path.write_text(
                '{"schema_version":1,"schema_version":1}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(DeterministicReplayError, "duplicate"):
                replay_trace(duplicate_path)

            # 금융 입력에 JSON float를 넣어 Decimal-only 계약을 검사한다.
            float_fixture = self._load_mutable_fixture()
            float_fixture["scenarios"][0]["input_trace"][0][
                "market_price_at_decision"
            ] = 2500.0
            float_path = self._write_temporary_fixture(
                temporary_directory,
                float_fixture,
                file_name="float.json",
            )
            with self.assertRaisesRegex(DeterministicReplayError, "JSON floats"):
                replay_trace(float_path)

            # 허용 schema 밖 secret-like key를 넣어 redaction 경계를 검사한다.
            secret_fixture = deepcopy(self._load_mutable_fixture())
            secret_fixture["scenarios"][0]["input_trace"][0]["api_secret"] = "not-a-secret"
            secret_path = self._write_temporary_fixture(
                temporary_directory,
                secret_fixture,
                file_name="secret-key.json",
            )
            with self.assertRaisesRegex(DeterministicReplayError, "secret-like key"):
                replay_trace(secret_path)

    def test_cli_replays_fixture_and_returns_zero(self) -> None:
        """
        함수 이름: test_cli_replays_fixture_and_returns_zero()
        기능: 통합 실행기가 호출할 quiet 반복 검증 CLI가 성공 status를 반환하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        self.assertEqual(main([str(TRACE_PATH), "--repeat", "3", "--quiet"]), 0)


if __name__ == "__main__":
    unittest.main()
