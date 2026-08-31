#!/usr/bin/env python3
"""Communication Case 메시지와 production/test 추적성 matrix를 fail-closed 검증한다."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
from typing import Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TRACEABILITY_PATH = Path("Design/Architecture/communication_traceability.json")
ROOT_KEYS = frozenset({"schema_version", "source_document", "entries"})
ENTRY_KEYS = frozenset(
    {
        "case",
        "message_id",
        "caller",
        "receiver",
        "operation",
        "production_owner",
        "positive_test",
        "negative_test",
        "completion_status",
        "gap_reason",
    }
)
OWNER_REFERENCE_KEYS = frozenset({"path", "symbol"})
TEST_REFERENCE_KEYS = frozenset({"path", "test"})
VALID_CASES = frozenset({"CASE_1", "CASE_2", "CASE_3", "CASE_4"})
CASE_BY_SECTION = {
    "4": "CASE_1",
    "5": "CASE_2",
    "6": "CASE_3",
    "7": "CASE_4",
}
CASE_HEADING_PATTERN = re.compile(r"^## ([4-7])\. Case [1-4](?:\s|$)")
MESSAGE_ROW_PATTERN = re.compile(
    r"^\| `(?P<message_id>[^`]+)` "
    r"\| (?P<caller_cell>[^|]+?) "
    r"\| `(?P<operation>[^`]+)` \|"
)
CALLER_RECEIVER_PATTERN = re.compile(
    r"^`(?P<caller>[^`]+) -> (?P<receiver>[^`]+)`$"
)
INTERNAL_OPERATION_PATTERN = re.compile(
    r"^`(?P<caller>[^`]+)` 내부 (?:Operation|branch)$"
)


class TraceabilityValidationError(RuntimeError):
    """
    클래스 이름: TraceabilityValidationError
    기능: 원본 문서 또는 matrix가 fail-closed 추적성 계약을 위반했음을 나타낸다.
    작성 날짜: 2026/08/24
    """


@dataclass(frozen=True)
class TraceabilitySummary:
    """
    클래스 이름: TraceabilitySummary
    기능: 구조 검증을 통과한 전체·완료·GAP 메시지 수를 보존한다.
    작성 날짜: 2026/08/24
    """

    total_count: int
    complete_count: int
    gap_count: int


@dataclass(frozen=True)
class CommunicationMessage:
    """
    클래스 이름: CommunicationMessage
    기능: Communication 문서에서 읽은 하나의 canonical 메시지 계약을 보존한다.
    작성 날짜: 2026/08/24
    """

    case: str
    message_id: str
    caller: str
    receiver: str
    operation: str


@dataclass(frozen=True)
class DeclaredTest:
    """
    클래스 이름: DeclaredTest
    기능: source에서 확인한 test의 manifest 조회 이름과 실제 전체 identity를 보존한다.
    작성 날짜: 2026/08/29
    """

    reference_name: str
    full_name: str


@dataclass(frozen=True)
class TestReferenceIdentity:
    """
    클래스 이름: TestReferenceIdentity
    기능: positive와 negative 증거가 가리키는 실제 파일·test identity를 보존한다.
    작성 날짜: 2026/08/29
    """

    path: Path
    full_name: str


@dataclass(frozen=True)
class JavaScriptToken:
    """
    클래스 이름: JavaScriptToken
    기능: Vitest 선언 판별에 필요한 identifier, 문자열과 구두점 token을 보존한다.
    작성 날짜: 2026/08/29
    """

    kind: str
    value: str


def _require_exact_keys(
    value: Mapping[str, object],
    expected_keys: frozenset[str],
    label: str,
) -> None:
    """
    함수 이름: _require_exact_keys()
    기능: JSON object가 누락·추가 없이 exact key 집합만 갖는지 검증한다.
    인자: value -> 검사할 mapping, expected_keys -> 허용 key, label -> 오류 위치
    반환값: 없음
    작성 날짜: 2026/08/24
    """
    # 누락과 초과 key를 함께 계산해 stale schema를 한 번의 안전한 진단으로 표시한다.
    actual_keys = frozenset(value)
    if actual_keys != expected_keys:
        missing_keys = sorted(expected_keys - actual_keys)
        extra_keys = sorted(actual_keys - expected_keys)
        raise TraceabilityValidationError(
            f"{label} schema mismatch: missing={missing_keys}, extra={extra_keys}"
        )


def _require_non_empty_text(value: object, label: str) -> str:
    """
    함수 이름: _require_non_empty_text()
    기능: JSON 값을 앞뒤 공백 없는 non-empty 문자열로 제한한다.
    인자: value -> 검사할 값, label -> 오류 위치
    반환값: 검증된 문자열
    작성 날짜: 2026/08/24
    """
    # 외부 JSON 문자열은 빈 값, 바깥 공백과 제어문자를 모두 거부해 reference를 정규화한다.
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or any(ord(character) < 0x20 for character in value)
    ):
        raise TraceabilityValidationError(f"{label} must be trimmed non-empty text")
    return value


def _resolve_repository_file(
    repository_root: Path,
    relative_path: object,
    label: str,
) -> Path:
    """
    함수 이름: _resolve_repository_file()
    기능: repository 밖으로 탈출하지 않는 상대 파일 경로를 확인해 반환한다.
    인자: repository_root -> 검증 기준 root, relative_path -> JSON 경로, label -> 오류 위치
    반환값: 존재하는 resolved file path
    작성 날짜: 2026/08/24
    """
    # 절대 경로를 먼저 거부한 뒤 실제 경로가 repository 안의 파일인지 다시 확인한다.
    path_text = _require_non_empty_text(relative_path, label)
    candidate_path = Path(path_text)
    if candidate_path.is_absolute():
        raise TraceabilityValidationError(f"{label} must be repository-relative")

    resolved_root = repository_root.resolve()
    resolved_path = (resolved_root / candidate_path).resolve()
    if not resolved_path.is_relative_to(resolved_root) or not resolved_path.is_file():
        raise TraceabilityValidationError(f"{label} does not identify a repository file")
    return resolved_path


def _validate_reference(
    reference: object,
    *,
    repository_root: Path,
    expected_keys: frozenset[str],
    symbol_key: str,
    label: str,
    nullable: bool,
    source_cache: dict[Path, str],
) -> TestReferenceIdentity | None:
    """
    함수 이름: _validate_reference()
    기능: production/test reference의 exact schema, 파일과 symbol 존재를 검증한다.
    인자: reference -> JSON reference 또는 null, repository_root -> repository root,
        expected_keys -> reference key 계약, symbol_key -> symbol/test key,
        label -> 오류 위치, nullable -> null 허용 여부, source_cache -> 파일 text cache
    반환값: test reference의 실제 identity, production 또는 nullable reference면 None
    작성 날짜: 2026/08/24
    """

    # GAP의 명시적 null만 허용하고 COMPLETE 또는 production의 null은 즉시 거부한다.
    if reference is None:
        if nullable:
            return None
        raise TraceabilityValidationError(f"{label} must not be null")
    if not isinstance(reference, dict):
        raise TraceabilityValidationError(f"{label} must be an object or null")

    _require_exact_keys(reference, expected_keys, label)
    referenced_path = _resolve_repository_file(
        repository_root,
        reference["path"],
        f"{label}.path",
    )
    symbol = _require_non_empty_text(reference[symbol_key], f"{label}.{symbol_key}")

    # Source를 한 번만 읽고 test reference는 단순 부분 문자열보다 강한 선언 경계를 요구한다.
    if referenced_path not in source_cache:
        source_cache[referenced_path] = referenced_path.read_text(encoding="utf-8")
    source_text = source_cache[referenced_path]

    # Test는 실제 선언 identity로 해석하고 production owner는 기존 exact symbol text를 확인한다.
    if symbol_key == "test":
        resolved_test = _resolve_declared_test_reference(
            referenced_path,
            source_text,
            symbol,
            label,
        )
        if resolved_test is None:
            raise TraceabilityValidationError(
                f"{label}.{symbol_key} was not found in {reference['path']}"
            )
        return TestReferenceIdentity(
            path=referenced_path,
            full_name=resolved_test.full_name,
        )
    if symbol not in source_text:
        raise TraceabilityValidationError(
            f"{label}.{symbol_key} was not found in {reference['path']}"
        )
    return None


def _collect_python_test_declarations(source_text: str) -> tuple[DeclaredTest, ...]:
    """
    함수 이름: _collect_python_test_declarations()
    기능: Python AST에서 module 직속 test 함수와 직속 class의 test method만 수집한다.
    인자: source_text -> UTF-8 Python test source
    반환값: manifest 조회 이름과 pytest 전체 identity 목록
    작성 날짜: 2026/08/29
    """
    # 문법 오류 source를 문자열 검색으로 낮춰 수락하지 않고 evidence 검증 실패로 고정한다.
    try:
        module_node = ast.parse(source_text)
    except SyntaxError as error:
        raise TraceabilityValidationError("Python test source could not be parsed") from error

    # Module body와 module 직속 class body 한 단계만 순회해 nested helper 선언을 배제한다.
    declarations: list[DeclaredTest] = []
    function_node_types = (ast.FunctionDef, ast.AsyncFunctionDef)
    for module_member in module_node.body:
        if isinstance(module_member, function_node_types):
            if module_member.name.startswith("test"):
                declarations.append(
                    DeclaredTest(
                        reference_name=module_member.name,
                        full_name=module_member.name,
                    )
                )
            continue
        if not isinstance(module_member, ast.ClassDef):
            continue

        # Class 직속 method만 pytest identity로 만들고 method 안 함수와 nested class는 보지 않는다.
        for class_member in module_member.body:
            if (
                isinstance(class_member, function_node_types)
                and class_member.name.startswith("test")
            ):
                declarations.append(
                    DeclaredTest(
                        reference_name=class_member.name,
                        full_name=f"{module_member.name}::{class_member.name}",
                    )
                )
    return tuple(declarations)


def _read_javascript_literal(
    source_text: str,
    start_index: int,
) -> tuple[JavaScriptToken | None, int]:
    """
    함수 이름: _read_javascript_literal()
    기능: JavaScript quote 또는 template literal 하나를 token으로 읽고 escape를 정규화한다.
    인자: source_text -> JavaScript/TypeScript source, start_index -> quote 시작 위치
    반환값: 닫힌 literal token과 다음 위치, 닫히지 않았으면 None과 source 끝 위치
    작성 날짜: 2026/08/29
    """
    quote_character = source_text[start_index]
    current_index = start_index + 1
    literal_characters: list[str] = []
    is_static_template = True
    simple_escapes = {
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
        "0": "\0",
    }

    # 닫는 quote까지 한 literal로 소비해 내부의 it/test text가 token으로 재해석되지 않게 한다.
    while current_index < len(source_text):
        current_character = source_text[current_index]
        if current_character == quote_character:
            token_kind = (
                "static_template"
                if quote_character == "`" and is_static_template
                else "dynamic_template"
                if quote_character == "`"
                else "string"
            )
            return (
                JavaScriptToken(token_kind, "".join(literal_characters)),
                current_index + 1,
            )
        if (
            quote_character == "`"
            and current_character == "$"
            and current_index + 1 < len(source_text)
            and source_text[current_index + 1] == "{"
        ):
            is_static_template = False  # Runtime interpolation title은 정적 test identity가 아니다.
        if current_character != "\\":
            literal_characters.append(current_character)
            current_index += 1
            continue
        if current_index + 1 >= len(source_text):
            return None, len(source_text)

        # 일반 escape와 line continuation만 복원하고 복합 escape는 원문 의미를 보존한다.
        escaped_character = source_text[current_index + 1]
        if escaped_character == "\n":
            current_index += 2
            continue
        if escaped_character == "\r":
            current_index += 2
            if current_index < len(source_text) and source_text[current_index] == "\n":
                current_index += 1
            continue
        literal_characters.append(simple_escapes.get(escaped_character, escaped_character))
        current_index += 2
    return None, len(source_text)


def _tokenize_javascript(source_text: str) -> tuple[JavaScriptToken, ...]:
    """
    함수 이름: _tokenize_javascript()
    기능: 주석을 제거하고 Vitest 호출 판별에 필요한 JavaScript token만 순서대로 만든다.
    인자: source_text -> UTF-8 JavaScript 또는 TypeScript source
    반환값: identifier, literal과 punctuation token 목록
    작성 날짜: 2026/08/29
    """
    tokens: list[JavaScriptToken] = []
    current_index = 0

    # Source를 한 번 순회하며 comment와 공백은 버리고 literal은 분해하지 않는다.
    while current_index < len(source_text):
        current_character = source_text[current_index]
        next_character = (
            source_text[current_index + 1]
            if current_index + 1 < len(source_text)
            else ""
        )
        if current_character.isspace():
            current_index += 1
            continue
        if current_character == "/" and next_character == "/":
            newline_index = source_text.find("\n", current_index + 2)
            current_index = len(source_text) if newline_index < 0 else newline_index + 1
            continue
        if current_character == "/" and next_character == "*":
            comment_end_index = source_text.find("*/", current_index + 2)
            current_index = (
                len(source_text) if comment_end_index < 0 else comment_end_index + 2
            )
            continue
        if current_character in {"'", '"', "`"}:
            literal_token, current_index = _read_javascript_literal(
                source_text,
                current_index,
            )
            if literal_token is not None:
                tokens.append(literal_token)
            continue
        if current_character.isalpha() or current_character in {"_", "$"}:
            identifier_end_index = current_index + 1
            while identifier_end_index < len(source_text):
                identifier_character = source_text[identifier_end_index]
                if not (
                    identifier_character.isalnum()
                    or identifier_character in {"_", "$"}
                ):
                    break
                identifier_end_index += 1
            tokens.append(
                JavaScriptToken(
                    "identifier",
                    source_text[current_index:identifier_end_index],
                )
            )
            current_index = identifier_end_index
            continue
        tokens.append(JavaScriptToken("punctuation", current_character))
        current_index += 1
    return tuple(tokens)


def _find_matching_parenthesis(
    tokens: tuple[JavaScriptToken, ...],
    opening_index: int,
) -> int | None:
    """
    함수 이름: _find_matching_parenthesis()
    기능: token 목록에서 지정한 여는 괄호와 짝인 닫는 괄호 위치를 찾는다.
    인자: tokens -> JavaScript token 목록, opening_index -> 여는 괄호 token 위치
    반환값: 짝인 닫는 괄호 위치, 닫히지 않았으면 None
    작성 날짜: 2026/08/29
    """
    parenthesis_depth = 0

    # String 내부 괄호는 이미 단일 token이므로 punctuation 괄호만 깊이에 반영한다.
    for token_index in range(opening_index, len(tokens)):
        token_value = tokens[token_index].value
        if token_value == "(":
            parenthesis_depth += 1
        elif token_value == ")":
            parenthesis_depth -= 1
            if parenthesis_depth == 0:
                return token_index
    return None


def _read_vitest_call_title(
    tokens: tuple[JavaScriptToken, ...],
    test_identifier_index: int,
) -> str | None:
    """
    함수 이름: _read_vitest_call_title()
    기능: it/test identifier 뒤 실제 Vitest 호출과 it.each의 정적 title을 읽는다.
    인자: tokens -> JavaScript token 목록, test_identifier_index -> it/test token 위치
    반환값: 실제 선언의 전체 title, 선언 형태가 아니거나 동적 title이면 None
    작성 날짜: 2026/08/29
    """
    allowed_modifiers = frozenset(
        {"concurrent", "each", "fails", "only", "sequential", "skip", "todo"}
    )
    current_index = test_identifier_index + 1
    uses_each = False

    # Vitest가 제공하는 직접 modifier chain만 허용해 임의 object method를 test로 오인하지 않는다.
    while (
        current_index + 1 < len(tokens)
        and tokens[current_index].value == "."
        and tokens[current_index + 1].kind == "identifier"
    ):
        modifier_name = tokens[current_index + 1].value
        if modifier_name not in allowed_modifiers:
            return None
        uses_each = uses_each or modifier_name == "each"
        current_index += 2

    # it.each(data)(title) 또는 tagged table 뒤의 두 번째 호출만 test 선언으로 해석한다.
    if uses_each:
        if current_index >= len(tokens):
            return None
        if tokens[current_index].value == "(":
            data_end_index = _find_matching_parenthesis(tokens, current_index)
            if data_end_index is None:
                return None
            current_index = data_end_index + 1
        elif tokens[current_index].kind in {"static_template", "dynamic_template"}:
            current_index += 1
        else:
            return None

    # 직접 it/test 호출의 첫 인자는 정적 quote 또는 interpolation 없는 template여야 한다.
    if current_index + 1 >= len(tokens) or tokens[current_index].value != "(":
        return None
    title_token = tokens[current_index + 1]
    if title_token.kind not in {"string", "static_template"}:
        return None
    return title_token.value


def _collect_vitest_test_declarations(source_text: str) -> tuple[DeclaredTest, ...]:
    """
    함수 이름: _collect_vitest_test_declarations()
    기능: comment와 임의 문자열이 아닌 실제 it/test 선언 title만 수집한다.
    인자: source_text -> UTF-8 JavaScript 또는 TypeScript test source
    반환값: 실제 Vitest full title 목록
    작성 날짜: 2026/08/29
    """
    tokens = _tokenize_javascript(source_text)
    declarations: list[DeclaredTest] = []

    # Property access의 object.it/test는 제외하고 독립적인 Vitest identifier 호출만 판별한다.
    for token_index, token in enumerate(tokens):
        if token.kind != "identifier" or token.value not in {"it", "test"}:
            continue
        if token_index > 0 and tokens[token_index - 1].value == ".":
            continue
        title = _read_vitest_call_title(tokens, token_index)
        if title is None:
            continue
        declarations.append(DeclaredTest(reference_name=title, full_name=title))
    return tuple(declarations)


def _resolve_declared_test_reference(
    referenced_path: Path,
    source_text: str,
    test_name: str,
    label: str,
) -> DeclaredTest | None:
    """
    함수 이름: _resolve_declared_test_reference()
    기능: manifest test 이름을 source의 유일한 실제 선언 identity로 fail-closed 해석한다.
    인자: referenced_path -> test source 경로, source_text -> UTF-8 source,
        test_name -> manifest 이름, label -> 오류 위치
    반환값: 유일하게 해석된 test 선언, 일치 선언이 없으면 None
    작성 날짜: 2026/08/29
    """
    # Python은 AST 이름만 exact match하고 JS/TS는 기존 colon-prefix alias 계약을 보존한다.
    if referenced_path.suffix == ".py":
        declarations = _collect_python_test_declarations(source_text)
        matching_declarations = [
            declaration
            for declaration in declarations
            if test_name in {declaration.reference_name, declaration.full_name}
        ]
    elif referenced_path.suffix in {".cjs", ".js", ".jsx", ".mjs", ".ts", ".tsx"}:
        declarations = _collect_vitest_test_declarations(source_text)
        matching_declarations = [
            declaration
            for declaration in declarations
            if declaration.full_name == test_name
            or declaration.full_name.startswith(f"{test_name}:")
        ]
    else:
        matching_declarations = []

    # 같은 alias가 둘 이상의 실제 선언을 가리키면 임의 선택 없이 manifest를 거부한다.
    if len(matching_declarations) > 1:
        raise TraceabilityValidationError(
            f"{label}.test resolves to multiple declared tests"
        )
    if not matching_declarations:
        return None
    return matching_declarations[0]


def _load_json_object(json_path: Path) -> dict[str, object]:
    """
    함수 이름: _load_json_object()
    기능: UTF-8 JSON 파일을 object root로 읽고 malformed 입력을 거부한다.
    인자: json_path -> 읽을 traceability JSON
    반환값: JSON object
    작성 날짜: 2026/08/24
    """
    def reject_duplicate_keys(
        object_pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        """
        함수 이름: reject_duplicate_keys()
        기능: 한 JSON object 안의 중복 key를 silent overwrite 대신 추적성 오류로 거부한다.
        인자: object_pairs -> decoder가 원문 순서로 전달한 key/value pair
        반환값: 중복이 없는 JSON object
        작성 날짜: 2026/08/25
        """
        decoded_object: dict[str, object] = {}

        # 첫 값을 보존한 채 같은 key의 두 번째 출현을 즉시 거부한다.
        for key, value in object_pairs:
            if key in decoded_object:
                raise TraceabilityValidationError(
                    "traceability JSON contains a duplicate object key"
                )
            decoded_object[key] = value
        return decoded_object

    def reject_nonstandard_constant(constant: str) -> object:
        """
        함수 이름: reject_nonstandard_constant()
        기능: JSON 표준에 없는 NaN과 Infinity 상수를 manifest에서 거부한다.
        인자: constant -> decoder가 발견한 비표준 숫자 token 이름
        반환값: 정상 입력에서는 반환하지 않음
        작성 날짜: 2026/08/25
        """
        del constant  # 비표준 원문 token은 오류 출력에 반사하지 않는다.
        raise TraceabilityValidationError(
            "traceability JSON contains a non-standard numeric constant"
        )

    # 파일 읽기와 JSON decode 실패를 원문이 없는 하나의 추적성 오류로 정규화한다.
    try:
        payload = json.loads(
            json_path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_nonstandard_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise TraceabilityValidationError("traceability JSON could not be read") from error
    if not isinstance(payload, dict):
        raise TraceabilityValidationError("traceability JSON root must be an object")
    return payload


def _read_communication_messages(source_path: Path) -> dict[tuple[str, str], CommunicationMessage]:
    """
    함수 이름: _read_communication_messages()
    기능: Communication 문서 Case 1~4 표에서 canonical 메시지 계약을 추출한다.
    인자: source_path -> Communication specification Markdown
    반환값: case와 message ID를 key로 한 canonical 메시지 mapping
    작성 날짜: 2026/08/24
    """
    # Communication 원문을 UTF-8 line 목록으로 읽고 내용은 오류 메시지에 반사하지 않는다.
    try:
        source_lines = source_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise TraceabilityValidationError("source Communication document could not be read") from error

    # Case heading 범위 안의 message 표 행만 canonical caller/receiver/operation으로 파싱한다.
    current_case: str | None = None
    messages: dict[tuple[str, str], CommunicationMessage] = {}
    for line in source_lines:
        heading_match = CASE_HEADING_PATTERN.match(line)
        if heading_match is not None:
            current_case = CASE_BY_SECTION[heading_match.group(1)]
            continue
        if line.startswith("## 8."):
            current_case = None
        if current_case is None:
            continue

        row_match = MESSAGE_ROW_PATTERN.match(line)
        if row_match is None:
            continue

        # Lifeline 간 호출과 self-call 표기를 분리해 receiver를 추측하지 않는다.
        caller_cell = row_match.group("caller_cell")
        caller_receiver_match = CALLER_RECEIVER_PATTERN.fullmatch(caller_cell)
        internal_operation_match = INTERNAL_OPERATION_PATTERN.fullmatch(caller_cell)
        if caller_receiver_match is not None:
            caller = caller_receiver_match.group("caller")
            receiver = caller_receiver_match.group("receiver")
        elif internal_operation_match is not None:
            caller = internal_operation_match.group("caller")
            receiver = caller  # 문서의 내부 Operation은 같은 lifeline에 대한 self-call이다.
        else:
            raise TraceabilityValidationError(
                f"Communication row has invalid caller/receiver: {caller_cell}"
            )

        # 같은 Case/message ID는 첫 canonical 행만 허용하고 중복을 즉시 거부한다.
        message = CommunicationMessage(
            case=current_case,
            message_id=row_match.group("message_id"),
            caller=caller,
            receiver=receiver,
            operation=row_match.group("operation"),
        )
        message_key = (message.case, message.message_id)
        if message_key in messages:
            raise TraceabilityValidationError(
                f"Communication document duplicates {message.case}/{message.message_id}"
            )
        messages[message_key] = message

    if not messages or {message.case for message in messages.values()} != VALID_CASES:
        raise TraceabilityValidationError("Communication document must contain all four Cases")
    return messages


def validate_traceability(
    repository_root: Path = REPOSITORY_ROOT,
    traceability_path: Path = TRACEABILITY_PATH,
) -> TraceabilitySummary:
    """
    함수 이름: validate_traceability()
    기능: matrix schema, 문서 전체 coverage, 중복, owner와 test reference를 검증한다.
    인자: repository_root -> repository root, traceability_path -> root 기준 JSON 경로
    반환값: 구조 검증을 통과한 완료/GAP 집계
    작성 날짜: 2026/08/24
    """
    # Manifest와 source 경로를 repository root 안의 실제 파일로 고정하고 root schema를 검증한다.
    resolved_root = repository_root.resolve()
    resolved_traceability_path = (
        traceability_path
        if traceability_path.is_absolute()
        else resolved_root / traceability_path
    ).resolve()
    payload = _load_json_object(resolved_traceability_path)
    _require_exact_keys(payload, ROOT_KEYS, "root")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise TraceabilityValidationError("schema_version must be integer 1")

    # 원문 message 전체와 manifest entry 목록을 먼저 읽어 완전성 대조 기준을 만든다.
    source_document = _resolve_repository_file(
        resolved_root,
        payload["source_document"],
        "source_document",
    )
    canonical_messages = _read_communication_messages(source_document)
    entries = payload["entries"]
    if not isinstance(entries, list) or not entries:
        raise TraceabilityValidationError("entries must be a non-empty array")

    # 각 entry의 identity, 원문 signature, 상태와 evidence reference를 한 번씩 검증한다.
    source_cache: dict[Path, str] = {}
    observed_messages: dict[tuple[str, str], Mapping[str, object]] = {}
    complete_count = 0
    gap_count = 0
    for entry_index, entry in enumerate(entries):
        label = f"entries[{entry_index}]"
        if not isinstance(entry, dict):
            raise TraceabilityValidationError(f"{label} must be an object")
        _require_exact_keys(entry, ENTRY_KEYS, label)

        # Case/message identity는 supported Case와 중복 없는 원문 message에 정확히 대응해야 한다.
        case = _require_non_empty_text(entry["case"], f"{label}.case")
        message_id = _require_non_empty_text(entry["message_id"], f"{label}.message_id")
        if case not in VALID_CASES:
            raise TraceabilityValidationError(f"{label}.case is unsupported")
        message_key = (case, message_id)
        if message_key in observed_messages:
            raise TraceabilityValidationError(f"matrix duplicates {case}/{message_id}")
        observed_messages[message_key] = entry

        canonical_message = canonical_messages.get(message_key)
        if canonical_message is None:
            raise TraceabilityValidationError(f"matrix adds unknown {case}/{message_id}")

        # Caller, receiver와 operation은 문서 변경을 숨기지 않고 원문과 byte 단위로 비교한다.
        for field_name in ("caller", "receiver", "operation"):
            actual_value = _require_non_empty_text(entry[field_name], f"{label}.{field_name}")
            expected_value = getattr(canonical_message, field_name)
            if actual_value != expected_value:
                raise TraceabilityValidationError(
                    f"{case}/{message_id} {field_name} drifted from Communication document"
                )

        # COMPLETE와 GAP는 각각 null gap reason 또는 명시적 non-empty reason만 허용한다.
        completion_status = _require_non_empty_text(
            entry["completion_status"],
            f"{label}.completion_status",
        )
        if completion_status not in {"COMPLETE", "GAP"}:
            raise TraceabilityValidationError(f"{label}.completion_status is unsupported")
        gap_reason = entry["gap_reason"]
        is_gap = completion_status == "GAP"
        if is_gap:
            _require_non_empty_text(gap_reason, f"{label}.gap_reason")
            gap_count += 1
        elif gap_reason is not None:
            raise TraceabilityValidationError(f"{label}.gap_reason must be null when COMPLETE")
        else:
            complete_count += 1

        # GAP만 미구현 owner 또는 아직 없는 branch test를 null로 기록할 수 있다.
        _validate_reference(
            entry["production_owner"],
            repository_root=resolved_root,
            expected_keys=OWNER_REFERENCE_KEYS,
            symbol_key="symbol",
            label=f"{label}.production_owner",
            nullable=is_gap,
            source_cache=source_cache,
        )
        positive_test_identity = _validate_reference(
            entry["positive_test"],
            repository_root=resolved_root,
            expected_keys=TEST_REFERENCE_KEYS,
            symbol_key="test",
            label=f"{label}.positive_test",
            nullable=is_gap,
            source_cache=source_cache,
        )
        negative_test_identity = _validate_reference(
            entry["negative_test"],
            repository_root=resolved_root,
            expected_keys=TEST_REFERENCE_KEYS,
            symbol_key="test",
            label=f"{label}.negative_test",
            nullable=is_gap,
            source_cache=source_cache,
        )

        # Alias text가 달라도 같은 파일의 같은 실제 test로 해석되면 branch 증거 중복이다.
        if not is_gap and positive_test_identity == negative_test_identity:
            raise TraceabilityValidationError(
                f"{label} COMPLETE evidence must use distinct positive and negative tests"
            )

    # 모든 entry를 검증한 뒤 원문 message가 하나도 빠지지 않았는지 최종 대조한다.
    missing_messages = sorted(set(canonical_messages) - set(observed_messages))
    if missing_messages:
        missing_text = ", ".join(f"{case}/{message_id}" for case, message_id in missing_messages)
        raise TraceabilityValidationError(f"matrix omits Communication messages: {missing_text}")
    return TraceabilitySummary(
        total_count=len(entries),
        complete_count=complete_count,
        gap_count=gap_count,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> int:
    """
    함수 이름: main()
    기능: 추적성 구조를 검사하고 GAP가 하나라도 남으면 readiness gate를 실패시킨다.
    인자: argv -> 허용하지 않는 CLI 인자 목록, repository_root -> 검증할 repository root
    반환값: 완료 matrix면 0, malformed 또는 GAP matrix면 1
    작성 날짜: 2026/08/24
    """
    # CLI 인자를 허용하지 않아 모든 호출이 repository의 canonical manifest만 검사하게 한다.
    command_arguments = tuple(sys.argv[1:] if argv is None else argv)
    if command_arguments:
        print("communication traceability checker accepts no arguments", file=sys.stderr)
        return 1
    try:
        summary = validate_traceability(repository_root=repository_root)
    except TraceabilityValidationError as error:
        print(f"communication traceability invalid: {error}", file=sys.stderr)
        return 1

    # 구조 검증이 끝난 집계를 출력하되 GAP 하나도 readiness 성공으로 취급하지 않는다.
    print(
        "communication traceability: "
        f"total={summary.total_count} "
        f"complete={summary.complete_count} "
        f"gap={summary.gap_count}"
    )
    if summary.gap_count:
        print("communication traceability readiness gate is blocked by GAP entries", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
