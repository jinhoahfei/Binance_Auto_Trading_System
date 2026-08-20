"""Integration test의 Communication 메시지 trace 계약을 검증한다."""

from __future__ import annotations

from collections.abc import Sequence
import unittest


TraceEntry = dict[str, object]
_TRACE_FIELDS = frozenset(
    {
        "message_id",
        "caller",
        "receiver",
        "command_event_id",
        "state_version_before",
        "state_version_after",
        "related_id",
        "result",
        "typed_failure_code",
    }
)
_SECRET_MARKERS = (
    "api_key",
    "apikey",
    "credential",
    "secret",
    "signature",
)


def start_trace_entry(
    trace: list[TraceEntry],
    *,
    message_id: str,
    caller: str,
    receiver: str,
    command_event_id: str,
    state_version_before: int,
    related_id: object,
) -> TraceEntry:
    """
    함수 이름: start_trace_entry()
    기능: 호출 직전의 구조화된 Communication trace 항목을 순서대로 추가한다.
    인자: trace -> trace 항목을 누적할 목록
        message_id -> Communication Diagram 메시지 번호
        caller -> 호출 책임 객체 이름
        receiver -> 수신 책임 객체 이름
        command_event_id -> 같은 초기화 요청을 연결할 식별자
        state_version_before -> 호출 직전 authoritative state version
        related_id -> asset, history 또는 order 관련 식별자
    반환값: 완료 결과를 채울 mutable trace 항목
    작성 날짜: 2026/08/21
    """
    entry: TraceEntry = {
        "message_id": message_id,
        "caller": caller,
        "receiver": receiver,
        "command_event_id": command_event_id,
        "state_version_before": state_version_before,
        "state_version_after": None,
        "related_id": related_id,
        "result": "PENDING",
        "typed_failure_code": None,
    }
    trace.append(entry)

    return entry


def finish_trace_entry(
    entry: TraceEntry,
    *,
    state_version_after: int,
    error: BaseException | None = None,
) -> None:
    """
    함수 이름: finish_trace_entry()
    기능: 호출 결과와 typed failure code 및 종료 state version을 trace에 기록한다.
    인자: entry -> start_trace_entry가 만든 항목
        state_version_after -> 호출 종료 뒤 authoritative state version
        error -> 실패한 경우 원래 예외
    반환값: 없음
    작성 날짜: 2026/08/21
    """
    entry["state_version_after"] = state_version_after
    if error is None:
        entry["result"] = "SUCCESS"
        entry["typed_failure_code"] = None
        return

    entry["result"] = "FAILURE"
    entry["typed_failure_code"] = getattr(
        error,
        "code",
        type(error).__name__,
    )


def assert_trace_contract(
    test_case: unittest.TestCase,
    trace: Sequence[TraceEntry],
    expected_message_ids: Sequence[str],
) -> None:
    """
    함수 이름: assert_trace_contract()
    기능: trace 필드, 메시지 순서, 결과와 secret 비노출을 공통 검증한다.
    인자: test_case -> unittest assertion을 제공할 test instance
        trace -> 검증할 구조화 trace 항목 순서
        expected_message_ids -> Communication Diagram의 기대 메시지 순서
    반환값: 없음
    작성 날짜: 2026/08/21
    """
    test_case.assertEqual(
        tuple(entry["message_id"] for entry in trace),
        tuple(expected_message_ids),
    )
    for entry in trace:
        test_case.assertEqual(frozenset(entry), _TRACE_FIELDS)
        test_case.assertIsInstance(entry["message_id"], str)
        test_case.assertIsInstance(entry["caller"], str)
        test_case.assertIsInstance(entry["receiver"], str)
        test_case.assertNotEqual(entry["caller"], entry["receiver"])
        test_case.assertIsInstance(entry["command_event_id"], str)
        test_case.assertNotEqual(entry["command_event_id"], "")
        test_case.assertIsInstance(entry["state_version_before"], int)
        test_case.assertIsInstance(entry["state_version_after"], int)
        test_case.assertIn(entry["result"], {"SUCCESS", "FAILURE"})
        if entry["result"] == "SUCCESS":
            test_case.assertIsNone(entry["typed_failure_code"])
        else:
            test_case.assertIsInstance(entry["typed_failure_code"], str)

    serialized_trace = repr(tuple(trace)).lower()
    for secret_marker in _SECRET_MARKERS:
        test_case.assertNotIn(secret_marker, serialized_trace)
