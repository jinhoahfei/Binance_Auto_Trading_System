"""Backend 도메인 전체가 공유하는 canonical 열거형을 정의한다."""

from enum import Enum


class RegimeType(str, Enum):
    """
    클래스 이름: RegimeType
    기능: 추천과 사용자 선택에 공통으로 사용하는 canonical REGIME을 정의한다.
    작성 날짜: 2026/08/20
    """

    TYPE_0 = "TYPE_0"
    TYPE_1 = "TYPE_1"
    TYPE_2 = "TYPE_2"
    TYPE_3 = "TYPE_3"
    TYPE_4 = "TYPE_4"
