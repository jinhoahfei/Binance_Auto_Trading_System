"""진단 입력을 원본 객체와 분리하고 비밀 없는 JSON 값으로 복사한다."""

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum


_PRIVATE_FIELDS = frozenset({
    "api_key", "api_secret", "secret", "token", "session_token", "signature",
    "authorization", "headers", "body", "url", "payload", "failure_reason", "message",
})


def encode_diagnostic_value(value: object) -> object:
    """
    함수 이름: encode_diagnostic_value()
    기능: 원본 참조와 민감정보 없이 Decimal 정밀도를 보존한 진단 값 트리를 만든다.
    인자: value -> 명시적으로 기록 대상으로 선택한 값
    반환값: 독립된 JSON 호환 값 또는 비공개 타입의 고정 표식
    작성 날짜: 2026/09/22
    """
    # 사용자 객체의 repr이나 deepcopy를 실행하지 않고 허용한 값만 복사한다.
    if isinstance(value, Enum):
        return encode_diagnostic_value(value.value)
    if isinstance(value, Decimal):
        return str(value) if value.is_finite() else None
    if isinstance(value, datetime):
        return value.isoformat(timespec="microseconds")
    if isinstance(value, timedelta):
        return str(Decimal(value // timedelta(microseconds=1)) / Decimal("1000000"))
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        value = {field.name: getattr(value, field.name) for field in fields(value) if not field.name.startswith("_")}
    if isinstance(value, Mapping):
        return {
            key: "[REDACTED]" if key.lower() in _PRIVATE_FIELDS else encode_diagnostic_value(item)
            for key, item in value.items()
            if isinstance(key, str) and not key.startswith("_")
        }
    if isinstance(value, (tuple, list)):
        return [encode_diagnostic_value(item) for item in value]
    return "[UNSUPPORTED]"
