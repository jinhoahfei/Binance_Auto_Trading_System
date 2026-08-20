"""REGIME 추천 타입과 RegimeSTM 상태를 정의한다."""

from enum import Enum, auto
from types import MappingProxyType
from typing import Mapping


class RegimeType(Enum):
    """
    클래스 이름: RegimeType
    기능: 4시간봉 평가로 추천하거나 사용자가 선택할 REGIME 타입을 정의한다.
    작성 날짜: 2026/08/14
    """

    TYPE_0 = auto()
    TYPE_1 = auto()
    TYPE_2 = auto()
    TYPE_3 = auto()
    TYPE_4 = auto()


class RegimeState(Enum):
    """
    클래스 이름: RegimeState
    기능: 4시간봉 REGIME 추천 상태 머신의 상태를 정의한다.
    작성 날짜: 2026/08/14
    """

    INITIAL = auto()
    FOUR_HOUR_CANDLE_EVALUATION = auto()
    TYPE_0_RECOMMENDED = auto()
    TYPE_1_RECOMMENDED = auto()
    TYPE_2_RECOMMENDED = auto()
    TYPE_3_RECOMMENDED = auto()
    TYPE_4_RECOMMENDED = auto()


REGIME_STATE_BY_TYPE: Mapping[RegimeType, RegimeState] = MappingProxyType(
    {
        RegimeType.TYPE_0: RegimeState.TYPE_0_RECOMMENDED,
        RegimeType.TYPE_1: RegimeState.TYPE_1_RECOMMENDED,
        RegimeType.TYPE_2: RegimeState.TYPE_2_RECOMMENDED,
        RegimeType.TYPE_3: RegimeState.TYPE_3_RECOMMENDED,
        RegimeType.TYPE_4: RegimeState.TYPE_4_RECOMMENDED,
    }
)

REGIME_TYPE_BY_STATE: Mapping[RegimeState, RegimeType] = MappingProxyType(
    {state: regime_type for regime_type, state in REGIME_STATE_BY_TYPE.items()}
)


def recommended_state_for(regime_type: RegimeType) -> RegimeState:
    """
    함수 이름: recommended_state_for()
    기능: REGIME 타입과 일대일로 연결된 추천 상태를 반환한다.
    인자: regime_type -> 추천 상태를 조회할 REGIME 타입
    반환값: REGIME 타입에 대응하는 추천 상태
    작성 날짜: 2026/08/14
    """
    if not isinstance(regime_type, RegimeType):
        raise TypeError("regime_type must be a RegimeType")

    return REGIME_STATE_BY_TYPE[regime_type]


def regime_type_for(state: RegimeState) -> RegimeType:
    """
    함수 이름: regime_type_for()
    기능: 추천 상태와 일대일로 연결된 REGIME 타입을 반환한다.
    인자: state -> REGIME 타입을 조회할 추천 상태
    반환값: 추천 상태에 대응하는 REGIME 타입
    작성 날짜: 2026/08/14
    """
    if not isinstance(state, RegimeState):
        raise TypeError("state must be a RegimeState")

    try:
        return REGIME_TYPE_BY_STATE[state]
    except KeyError as error:
        raise ValueError("state must be a recommended RegimeState") from error

