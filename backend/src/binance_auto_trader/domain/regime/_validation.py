"""RegimeSTM 불변 값 객체가 공유하는 입력 검증 함수이다."""

from datetime import datetime
from decimal import Decimal


def validate_non_empty_text(value: object, field_name: str) -> None:
    """
    함수 이름: validate_non_empty_text()
    기능: 입력값이 공백이 아닌 문자열인지 검증한다.
    인자: value -> 검증할 입력값
        field_name -> 오류 메시지에 표시할 필드 이름
    반환값: 없음
    작성 날짜: 2026/08/14
    """
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")

    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")


def validate_optional_non_empty_text(
    value: object,
    field_name: str,
) -> None:
    """
    함수 이름: validate_optional_non_empty_text()
    기능: 선택 입력값이 None 또는 공백이 아닌 문자열인지 검증한다.
    인자: value -> 검증할 선택 입력값
        field_name -> 오류 메시지에 표시할 필드 이름
    반환값: 없음
    작성 날짜: 2026/08/14
    """
    if value is not None:
        validate_non_empty_text(value, field_name)


def validate_aware_datetime(value: object, field_name: str) -> None:
    """
    함수 이름: validate_aware_datetime()
    기능: 입력값이 시간대 정보를 가진 datetime인지 검증한다.
    인자: value -> 검증할 입력값
        field_name -> 오류 메시지에 표시할 필드 이름
    반환값: 없음
    작성 날짜: 2026/08/14
    """
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def validate_finite_decimal(value: object, field_name: str) -> None:
    """
    함수 이름: validate_finite_decimal()
    기능: 입력값이 유한한 Decimal인지 검증한다.
    인자: value -> 검증할 입력값
        field_name -> 오류 메시지에 표시할 필드 이름
    반환값: 없음
    작성 날짜: 2026/08/14
    """
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be a Decimal")

    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite")


def validate_non_negative_integer(value: object, field_name: str) -> None:
    """
    함수 이름: validate_non_negative_integer()
    기능: 입력값이 0 이상의 정수인지 검증한다.
    인자: value -> 검증할 입력값
        field_name -> 오류 메시지에 표시할 필드 이름
    반환값: 없음
    작성 날짜: 2026/08/14
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")

    if value < 0:
        raise ValueError(f"{field_name} must not be negative")

