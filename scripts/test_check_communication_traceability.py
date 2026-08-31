"""Communication traceability matrix의 schema, coverage와 readiness gate를 검증한다."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

from scripts.check_communication_traceability import (
    TraceabilityValidationError,
    main,
    validate_traceability,
)


class CommunicationTraceabilityCheckerTests(unittest.TestCase):
    """
    클래스 이름: CommunicationTraceabilityCheckerTests
    기능: exact schema, 문서 coverage, reference 존재와 GAP 차단을 검증한다.
    작성 날짜: 2026/08/24
    """

    def _create_valid_fixture(self, repository_root: Path) -> dict[str, object]:
        """
        함수 이름: _create_valid_fixture()
        기능: 네 Case 한 메시지와 실제 owner/positive/negative symbol을 가진 fixture를 만든다.
        인자: repository_root -> fixture 파일을 생성할 임시 repository root
        반환값: JSON에 기록할 valid traceability payload
        작성 날짜: 2026/08/24
        """

        source_rows = []
        entries = []
        for case_number in range(1, 5):
            case_name = f"CASE_{case_number}"
            message_id = str(case_number)
            caller = f"Caller{case_number}"
            receiver = f"Receiver{case_number}"
            operation = f"operation{case_number}() : void"
            caller_cell = f"`{caller} -> {receiver}`"
            if case_number == 2:
                # 같은 lifeline 내부 Operation도 별도 Communication 메시지로 누락 없이 읽어야 한다.
                receiver = caller
                caller_cell = f"`{caller}` 내부 Operation"
            source_rows.extend(
                (
                    f"## {case_number + 3}. Case {case_number} - Fixture",
                    "",
                    "| 번호 | 호출자 -> 수신자 | Operation | Parameter | Return | 설명 | 동작 과정 |",
                    "|---|---|---|---|---|---|---|",
                    f"| `{message_id}` | {caller_cell} | `{operation}` | 없음 | `void` | fixture | fixture |",
                    "",
                )
            )
            entries.append(
                {
                    "case": case_name,
                    "message_id": message_id,
                    "caller": caller,
                    "receiver": receiver,
                    "operation": operation,
                    "production_owner": {
                        "path": "production.py",
                        "symbol": "def production_owner",
                    },
                    "positive_test": {
                        "path": "tests/test_flow.py",
                        "test": "test_positive",
                    },
                    "negative_test": {
                        "path": "tests/test_flow.py",
                        "test": "test_negative",
                    },
                    "completion_status": "COMPLETE",
                    "gap_reason": None,
                }
            )

        # Reference 검사는 filename 존재뿐 아니라 실제 symbol text도 확인해야 한다.
        (repository_root / "tests").mkdir(parents=True)
        (repository_root / "source.md").write_text("\n".join(source_rows), encoding="utf-8")
        (repository_root / "production.py").write_text(
            "def production_owner():\n    return None\n",
            encoding="utf-8",
        )
        (repository_root / "tests" / "test_flow.py").write_text(
            "def test_positive():\n    pass\n\n"
            "def test_negative():\n    pass\n",
            encoding="utf-8",
        )
        return {
            "schema_version": 1,
            "source_document": "source.md",
            "entries": entries,
        }

    def _write_payload(self, repository_root: Path, payload: dict[str, object]) -> None:
        """
        함수 이름: _write_payload()
        기능: 검사 대상 canonical 경로에 deterministic JSON fixture를 기록한다.
        인자: repository_root -> 임시 repository root, payload -> 기록할 JSON object
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # Canonical manifest parent를 만든 뒤 정렬된 fixture JSON을 한 경로에 기록한다.
        traceability_path = (
            repository_root / "Design" / "Architecture" / "communication_traceability.json"
        )
        traceability_path.parent.mkdir(parents=True)
        traceability_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def test_repository_matrix_covers_every_document_message(self) -> None:
        """
        함수 이름: test_repository_matrix_covers_every_document_message()
        기능: 실제 repository matrix가 전체 문서와 reference 구조 검증을 통과하는지 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        summary = validate_traceability()

        self.assertGreater(summary.total_count, 0)
        self.assertEqual(
            summary.total_count,
            summary.complete_count + summary.gap_count,
        )  # 모든 문서 메시지는 완료 또는 명시 GAP 중 하나여야 한다.

    def test_exact_schema_and_complete_fixture_pass(self) -> None:
        """
        함수 이름: test_exact_schema_and_complete_fixture_pass()
        기능: 네 Case와 complete reference를 가진 exact fixture가 통과하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # 완전한 네 Case fixture를 기록하고 validator 집계가 전부 COMPLETE인지 확인한다.
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository_root = Path(temporary_directory)
            payload = self._create_valid_fixture(repository_root)
            self._write_payload(repository_root, payload)

            summary = validate_traceability(repository_root=repository_root)

            self.assertEqual(summary.total_count, 4)
            self.assertEqual(summary.complete_count, 4)
            self.assertEqual(summary.gap_count, 0)

    def test_duplicate_message_fails_closed(self) -> None:
        """
        함수 이름: test_duplicate_message_fails_closed()
        기능: 같은 Case/message ID를 두 번 기록한 matrix를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # Valid fixture에 첫 entry를 복제해 같은 canonical message의 중복을 주입한다.
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository_root = Path(temporary_directory)
            payload = self._create_valid_fixture(repository_root)
            entries = payload["entries"]
            if not isinstance(entries, list):
                self.fail("fixture entries must be a list")
            entries.append(deepcopy(entries[0]))
            self._write_payload(repository_root, payload)

            with self.assertRaisesRegex(TraceabilityValidationError, "duplicates"):
                validate_traceability(repository_root=repository_root)

    def test_extra_schema_key_fails_closed(self) -> None:
        """
        함수 이름: test_extra_schema_key_fails_closed()
        기능: 해석되지 않는 entry key를 허용하지 않아 schema drift를 차단한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # Valid entry에 허용되지 않은 key 하나를 추가해 exact schema 거부를 확인한다.
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository_root = Path(temporary_directory)
            payload = self._create_valid_fixture(repository_root)
            entries = payload["entries"]
            if not isinstance(entries, list) or not isinstance(entries[0], dict):
                self.fail("fixture first entry must be an object")
            entries[0]["unreviewed_evidence"] = True
            self._write_payload(repository_root, payload)

            with self.assertRaisesRegex(TraceabilityValidationError, "schema mismatch"):
                validate_traceability(repository_root=repository_root)

    def test_duplicate_json_key_fails_closed(self) -> None:
        """
        함수 이름: test_duplicate_json_key_fails_closed()
        기능: JSON decoder가 중복 key의 마지막 값을 신뢰하지 않고 manifest를 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository_root = Path(temporary_directory)
            payload = self._create_valid_fixture(repository_root)
            self._write_payload(repository_root, payload)
            traceability_path = (
                repository_root / "Design" / "Architecture" / "communication_traceability.json"
            )
            source_text = traceability_path.read_text(encoding="utf-8")

            # Root schema_version을 중복 기록해 silent overwrite 공격을 재현한다.
            traceability_path.write_text(
                source_text.replace(
                    '"schema_version": 1,',
                    '"schema_version": 1,\n  "schema_version": 1,',
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(TraceabilityValidationError, "duplicate object key"):
                validate_traceability(repository_root=repository_root)

    def test_nonstandard_json_number_fails_closed(self) -> None:
        """
        함수 이름: test_nonstandard_json_number_fails_closed()
        기능: Python decoder가 허용하는 NaN을 표준 JSON traceability 값으로 수락하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository_root = Path(temporary_directory)
            payload = self._create_valid_fixture(repository_root)
            self._write_payload(repository_root, payload)
            traceability_path = (
                repository_root / "Design" / "Architecture" / "communication_traceability.json"
            )
            source_text = traceability_path.read_text(encoding="utf-8")

            # 숫자 field에 JSON 표준 밖 NaN token을 넣어 decoder fail-closed 경계를 확인한다.
            traceability_path.write_text(
                source_text.replace('"schema_version": 1', '"schema_version": NaN', 1),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(TraceabilityValidationError, "non-standard numeric"):
                validate_traceability(repository_root=repository_root)

    def test_missing_test_symbol_fails_closed(self) -> None:
        """
        함수 이름: test_missing_test_symbol_fails_closed()
        기능: 존재하지 않는 test 이름을 완료 증거로 가리키는 stale reference를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # Positive test reference의 symbol만 존재하지 않는 이름으로 바꿔 stale 근거를 재현한다.
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository_root = Path(temporary_directory)
            payload = self._create_valid_fixture(repository_root)
            entries = payload["entries"]
            if not isinstance(entries, list) or not isinstance(entries[0], dict):
                self.fail("fixture first entry must be an object")
            positive_test = entries[0]["positive_test"]
            if not isinstance(positive_test, dict):
                self.fail("fixture positive_test must be an object")
            positive_test["test"] = "test_missing"
            self._write_payload(repository_root, payload)

            with self.assertRaisesRegex(TraceabilityValidationError, "was not found"):
                validate_traceability(repository_root=repository_root)

    def test_test_name_prefix_is_not_accepted_as_registered_evidence(self) -> None:
        """
        함수 이름: test_test_name_prefix_is_not_accepted_as_registered_evidence()
        기능: 더 긴 test 이름의 부분 문자열을 stale manifest evidence로 수락하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository_root = Path(temporary_directory)
            payload = self._create_valid_fixture(repository_root)
            entries = payload["entries"]
            if not isinstance(entries, list) or not isinstance(entries[0], dict):
                self.fail("fixture first entry must be an object")
            positive_test = entries[0]["positive_test"]
            if not isinstance(positive_test, dict):
                self.fail("fixture positive_test must be an object")

            # 실제 test_positive보다 짧은 prefix만 기록해 substring-only 검사의 우회를 재현한다.
            positive_test["test"] = "test_pos"
            self._write_payload(repository_root, payload)

            with self.assertRaisesRegex(TraceabilityValidationError, "was not found"):
                validate_traceability(repository_root=repository_root)

    def test_complete_entry_requires_distinct_positive_and_negative_tests(self) -> None:
        """
        함수 이름: test_complete_entry_requires_distinct_positive_and_negative_tests()
        기능: 한 test reference를 positive와 negative 증거로 중복한 COMPLETE entry를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository_root = Path(temporary_directory)
            payload = self._create_valid_fixture(repository_root)
            entries = payload["entries"]
            if not isinstance(entries, list) or not isinstance(entries[0], dict):
                self.fail("fixture first entry must be an object")

            # COMPLETE는 서로 다른 branch evidence를 가리켜야 하므로 같은 object 사본도 거부한다.
            entries[0]["negative_test"] = deepcopy(entries[0]["positive_test"])
            self._write_payload(repository_root, payload)

            with self.assertRaisesRegex(TraceabilityValidationError, "distinct"):
                validate_traceability(repository_root=repository_root)

    def test_python_nested_and_comment_test_names_are_not_registered(self) -> None:
        """
        함수 이름: test_python_nested_and_comment_test_names_are_not_registered()
        기능: Python comment와 nested 함수의 test 이름을 module-level evidence로 수락하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository_root = Path(temporary_directory)
            payload = self._create_valid_fixture(repository_root)

            # Comment와 helper 내부에만 positive 이름을 두고 module 직속 negative만 유지한다.
            (repository_root / "tests" / "test_flow.py").write_text(
                "# def test_positive(): pass\n\n"
                "def helper():\n"
                "    def test_positive():\n"
                "        pass\n\n"
                "    class NestedTests:\n"
                "        def test_positive(self):\n"
                "            pass\n\n"
                "def test_negative():\n"
                "    pass\n",
                encoding="utf-8",
            )
            self._write_payload(repository_root, payload)

            with self.assertRaisesRegex(TraceabilityValidationError, "was not found"):
                validate_traceability(repository_root=repository_root)

    def test_python_direct_class_test_methods_are_registered(self) -> None:
        """
        함수 이름: test_python_direct_class_test_methods_are_registered()
        기능: Module 직속 class의 sync·async test method를 실제 pytest identity로 인정한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository_root = Path(temporary_directory)
            payload = self._create_valid_fixture(repository_root)

            # 직속 class body의 두 method를 qualified identity로 수집할 수 있게 fixture를 교체한다.
            (repository_root / "tests" / "test_flow.py").write_text(
                "class TestFlow:\n"
                "    def test_positive(self):\n"
                "        pass\n\n"
                "    async def test_negative(self):\n"
                "        pass\n",
                encoding="utf-8",
            )
            self._write_payload(repository_root, payload)

            summary = validate_traceability(repository_root=repository_root)

            self.assertEqual(summary.complete_count, 4)
            self.assertEqual(summary.gap_count, 0)

    def test_vitest_comment_and_arbitrary_string_are_not_registered(self) -> None:
        """
        함수 이름: test_vitest_comment_and_arbitrary_string_are_not_registered()
        기능: Vitest처럼 보이는 comment, 문자열과 object method를 실제 test 선언에서 제외한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository_root = Path(temporary_directory)
            payload = self._create_valid_fixture(repository_root)
            entries = payload["entries"]
            if not isinstance(entries, list) or not isinstance(entries[0], dict):
                self.fail("fixture first entry must be an object")

            # 첫 entry만 TypeScript source로 옮겨 fake positive와 실제 negative를 함께 검증한다.
            entries[0]["positive_test"] = {
                "path": "tests/test_flow.test.ts",
                "test": "test_positive",
            }
            entries[0]["negative_test"] = {
                "path": "tests/test_flow.test.ts",
                "test": "test_negative",
            }
            (repository_root / "tests" / "test_flow.test.ts").write_text(
                "// it('test_positive: line comment', () => {});\n"
                "/* test('test_positive: block comment', () => {}); */\n"
                'const unused = "it(\'test_positive: arbitrary string\', () => {})";\n'
                "const title = 'test_positive: bare title';\n"
                "helper.test('test_positive: object method', () => {});\n"
                "it('test_negative: actual declaration', () => {});\n",
                encoding="utf-8",
            )
            self._write_payload(repository_root, payload)

            with self.assertRaisesRegex(TraceabilityValidationError, "was not found"):
                validate_traceability(repository_root=repository_root)

    def test_vitest_it_each_and_colon_alias_resolve_to_full_titles(self) -> None:
        """
        함수 이름: test_vitest_it_each_and_colon_alias_resolve_to_full_titles()
        기능: it.each와 direct test의 colon-prefix manifest 이름을 실제 전체 title로 해석한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository_root = Path(temporary_directory)
            payload = self._create_valid_fixture(repository_root)
            entries = payload["entries"]
            if not isinstance(entries, list) or not isinstance(entries[0], dict):
                self.fail("fixture first entry must be an object")

            # Prefix reference가 각각 it.each와 modifier가 있는 direct test 하나로 유일하게 이어진다.
            entries[0]["positive_test"] = {
                "path": "tests/test_flow.test.ts",
                "test": "test_positive",
            }
            entries[0]["negative_test"] = {
                "path": "tests/test_flow.test.ts",
                "test": "test_negative",
            }
            (repository_root / "tests" / "test_flow.test.ts").write_text(
                "it.each([{ value: 1 }, { value: 2 }])(\n"
                "    'test_positive: table branch %s',\n"
                "    ({ value }) => value,\n"
                ");\n"
                "test.only('test_negative: direct branch', () => {});\n",
                encoding="utf-8",
            )
            self._write_payload(repository_root, payload)

            summary = validate_traceability(repository_root=repository_root)

            self.assertEqual(summary.complete_count, 4)
            self.assertEqual(summary.gap_count, 0)

    def test_vitest_aliases_of_one_full_title_are_not_distinct(self) -> None:
        """
        함수 이름: test_vitest_aliases_of_one_full_title_are_not_distinct()
        기능: 서로 다른 manifest 문자열이 같은 Vitest full title을 가리키는 evidence 우회를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository_root = Path(temporary_directory)
            payload = self._create_valid_fixture(repository_root)
            entries = payload["entries"]
            if not isinstance(entries, list) or not isinstance(entries[0], dict):
                self.fail("fixture first entry must be an object")

            # 짧은 alias와 exact title은 JSON object가 달라도 같은 실제 test identity다.
            entries[0]["positive_test"] = {
                "path": "tests/test_flow.test.ts",
                "test": "test_shared",
            }
            entries[0]["negative_test"] = {
                "path": "tests/test_flow.test.ts",
                "test": "test_shared: concrete branch",
            }
            (repository_root / "tests" / "test_flow.test.ts").write_text(
                "it('test_shared: concrete branch', () => {});\n",
                encoding="utf-8",
            )
            self._write_payload(repository_root, payload)

            with self.assertRaisesRegex(TraceabilityValidationError, "distinct"):
                validate_traceability(repository_root=repository_root)

    def test_vitest_ambiguous_colon_alias_fails_closed(self) -> None:
        """
        함수 이름: test_vitest_ambiguous_colon_alias_fails_closed()
        기능: 하나의 colon-prefix alias가 여러 Vitest full title과 일치하면 임의 선택하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository_root = Path(temporary_directory)
            payload = self._create_valid_fixture(repository_root)
            entries = payload["entries"]
            if not isinstance(entries, list) or not isinstance(entries[0], dict):
                self.fail("fixture first entry must be an object")

            # Positive prefix가 두 선언을 동시에 가리키도록 만들어 ambiguity 차단을 확인한다.
            entries[0]["positive_test"] = {
                "path": "tests/test_flow.test.ts",
                "test": "test_shared",
            }
            (repository_root / "tests" / "test_flow.test.ts").write_text(
                "it('test_shared: first branch', () => {});\n"
                "it('test_shared: second branch', () => {});\n",
                encoding="utf-8",
            )
            self._write_payload(repository_root, payload)

            with self.assertRaisesRegex(TraceabilityValidationError, "multiple declared tests"):
                validate_traceability(repository_root=repository_root)

    def test_missing_reference_file_fails_closed(self) -> None:
        """
        함수 이름: test_missing_reference_file_fails_closed()
        기능: repository에 없는 owner 파일을 가리킨 matrix reference를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # Production owner 경로만 repository 밖의 미존재 파일로 바꿔 path 검증을 관찰한다.
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository_root = Path(temporary_directory)
            payload = self._create_valid_fixture(repository_root)
            entries = payload["entries"]
            if not isinstance(entries, list) or not isinstance(entries[0], dict):
                self.fail("fixture first entry must be an object")
            production_owner = entries[0]["production_owner"]
            if not isinstance(production_owner, dict):
                self.fail("fixture production_owner must be an object")
            production_owner["path"] = "missing/production.py"
            self._write_payload(repository_root, payload)

            with self.assertRaisesRegex(TraceabilityValidationError, "repository file"):
                validate_traceability(repository_root=repository_root)

    def test_gap_is_structurally_valid_but_blocks_cli_readiness(self) -> None:
        """
        함수 이름: test_gap_is_structurally_valid_but_blocks_cli_readiness()
        기능: 근거 있는 GAP는 audit에 남기되 executable readiness gate는 실패하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        with tempfile.TemporaryDirectory() as temporary_directory:
            repository_root = Path(temporary_directory)
            payload = self._create_valid_fixture(repository_root)
            entries = payload["entries"]
            if not isinstance(entries, list) or not isinstance(entries[0], dict):
                self.fail("fixture first entry must be an object")
            entries[0]["completion_status"] = "GAP"
            entries[0]["gap_reason"] = "negative branch trace is not implemented"
            entries[0]["negative_test"] = None
            self._write_payload(repository_root, payload)
            standard_output = StringIO()
            standard_error = StringIO()

            # 구조 검증 결과는 GAP를 보존하지만 CLI는 이를 readiness 성공으로 승격하지 않는다.
            summary = validate_traceability(repository_root=repository_root)
            with redirect_stdout(standard_output), redirect_stderr(standard_error):
                exit_code = main([], repository_root=repository_root)

            self.assertEqual(summary.gap_count, 1)
            self.assertEqual(exit_code, 1)
            self.assertIn("gap=1", standard_output.getvalue())
            self.assertIn("blocked by GAP", standard_error.getvalue())


if __name__ == "__main__":
    unittest.main()
