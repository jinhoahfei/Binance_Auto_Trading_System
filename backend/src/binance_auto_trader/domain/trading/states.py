"""TradingSTM 상태 구성과 상태 관련 열거형을 정의한다."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..common import RegimeType


class RootState(str, Enum):
    """
    클래스 이름: RootState
    기능: TradingSTM의 최상위 상태를 정의한다.
    작성 날짜: 2026/08/14
    """

    NOT_STARTED = "NOT_STARTED"
    LOWER_TOUCH_WATCH = "LOWER_TOUCH_WATCH"
    TRADE_MANAGEMENT = "TRADE_MANAGEMENT"
    UPPER_BB_STATE_MACHINE = "UPPER_BB_STATE_MACHINE"
    STOPPING = "STOPPING"
    LOGIC_TERMINATED = "LOGIC_TERMINATED"


class OwnershipState(str, Enum):
    """
    클래스 이름: OwnershipState
    기능: 병렬 Region 1의 포지션 소유권 상태를 정의한다.
    작성 날짜: 2026/08/14
    """

    NO_POSITION = "NO_POSITION"
    CASE_B_POSITION_MANAGEMENT = "CASE_B_POSITION_MANAGEMENT"
    CASE_C_POSITION_MANAGEMENT = "CASE_C_POSITION_MANAGEMENT"


class CaseBSignalState(str, Enum):
    """
    클래스 이름: CaseBSignalState
    기능: 병렬 Region 2의 Case B 신호 검사 상태를 정의한다.
    작성 날짜: 2026/08/14
    """

    B_WAIT_TOUCH = "B_WAIT_TOUCH"
    B_WAIT_SIGNAL = "B_WAIT_SIGNAL"
    B_WAIT_PULLBACK = "B_WAIT_PULLBACK"
    B_POSITION_OPEN_SIGNALLED = "B_POSITION_OPEN_SIGNALLED"
    CASE_B_FINAL_STATE = "CASE_B_FINAL_STATE"


class CaseCSignalState(str, Enum):
    """
    클래스 이름: CaseCSignalState
    기능: 병렬 Region 3의 Case C 신호 검사 상태를 정의한다.
    작성 날짜: 2026/08/14
    """

    C_WAIT_SETUP = "C_WAIT_SETUP"
    C_SETUP = "C_SETUP"
    C_POSITION_OPEN_SIGNALLED = "C_POSITION_OPEN_SIGNALLED"
    CASE_C_FINAL_STATE = "CASE_C_FINAL_STATE"


class CaseBPositionState(str, Enum):
    """
    클래스 이름: CaseBPositionState
    기능: Case B 포지션 관리 복합 상태의 하위 상태를 정의한다.
    작성 날짜: 2026/08/14
    """

    CASE_B_HOLDING = "CASE_B_HOLDING"
    CASE_B_TREND_HOLD = "CASE_B_TREND_HOLD"
    CASE_B_CLOSED = "CASE_B_CLOSED"


class CaseCPositionState(str, Enum):
    """
    클래스 이름: CaseCPositionState
    기능: Case C 포지션 관리 복합 상태의 하위 상태를 정의한다.
    작성 날짜: 2026/08/14
    """

    CASE_C_HOLDING = "CASE_C_HOLDING"
    CASE_C_TP_TRAILING = "CASE_C_TP_TRAILING"
    CASE_C_CLOSED = "CASE_C_CLOSED"
    CASE_C_RECOVERY_SUCCEEDED = "CASE_C_RECOVERY_SUCCEEDED"


class StrategyType(str, Enum):
    """
    클래스 이름: StrategyType
    기능: 주문 또는 포지션을 소유하는 Case 전략을 정의한다.
    작성 날짜: 2026/08/14
    """

    CASE_B = "CASE_B"
    CASE_C = "CASE_C"


class OrderSide(str, Enum):
    """
    클래스 이름: OrderSide
    기능: 주문의 매수·매도 방향을 정의한다.
    작성 날짜: 2026/08/14
    """

    BUY = "BUY"
    SELL = "SELL"


class OrderAttemptKind(str, Enum):
    """
    클래스 이름: OrderAttemptKind
    기능: 주문 요청이 최초 시도인지 재시도인지 구분한다.
    작성 날짜: 2026/08/14
    """

    INITIAL = "INITIAL"
    RETRY = "RETRY"


class TradingPhase(str, Enum):
    """
    클래스 이름: TradingPhase
    기능: TradingController가 관리하는 주문 및 종료 처리 단계를 정의한다.
    작성 날짜: 2026/08/14
    """

    IDLE = "IDLE"
    ENTRY_ORDER_PENDING = "ENTRY_ORDER_PENDING"
    EXIT_ORDER_PENDING = "EXIT_ORDER_PENDING"
    STOPPING = "STOPPING"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    TERMINATED = "TERMINATED"


class ExitReason(str, Enum):
    """
    클래스 이름: ExitReason
    기능: Case B와 Case C에서 사용하는 포지션 청산 사유를 정의한다.
    작성 날짜: 2026/08/14
    """

    EMERGENCY_STOP = "EMERGENCY_STOP"
    STOP = "STOP"
    TIME = "TIME"
    TAKE_PROFIT = "TAKE_PROFIT"
    TREND_HOLD = "TREND_HOLD"
    TP_TRAIL = "TP_TRAIL"
    TP_FALLBACK = "TP_FALLBACK"


class PositionReturnState(str, Enum):
    """
    클래스 이름: PositionReturnState
    기능: 청산 주문 처리 중 보존할 포지션 관리 상태를 정의한다.
    작성 날짜: 2026/08/14
    """

    CASE_B_HOLDING = "CASE_B_HOLDING"
    CASE_B_TREND_HOLD = "CASE_B_TREND_HOLD"
    CASE_C_HOLDING = "CASE_C_HOLDING"
    CASE_C_TP_TRAILING = "CASE_C_TP_TRAILING"


@dataclass(frozen=True, slots=True)
class TradingStateConfiguration:
    """
    클래스 이름: TradingStateConfiguration
    기능: 최상위 상태와 활성 병렬 Region 상태를 하나의 불변 snapshot으로 보존한다.
    작성 날짜: 2026/08/14
    """

    root_state: RootState = RootState.NOT_STARTED
    ownership_state: OwnershipState | None = None
    case_b_signal_state: CaseBSignalState | None = None
    case_c_signal_state: CaseCSignalState | None = None
    case_b_position_state: CaseBPositionState | None = None
    case_c_position_state: CaseCPositionState | None = None

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 생성된 상태 구성의 enum 타입과 Region 조합 불변식을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        # 문자열이 enum identity 비교를 통과하지 못한 채 유입되는 것을 차단한다.
        _require_enum("root_state", self.root_state, RootState)
        _require_optional_enum("ownership_state", self.ownership_state, OwnershipState)
        _require_optional_enum(
            "case_b_signal_state",
            self.case_b_signal_state,
            CaseBSignalState,
        )
        _require_optional_enum(
            "case_c_signal_state",
            self.case_c_signal_state,
            CaseCSignalState,
        )
        _require_optional_enum(
            "case_b_position_state",
            self.case_b_position_state,
            CaseBPositionState,
        )
        _require_optional_enum(
            "case_c_position_state",
            self.case_c_position_state,
            CaseCPositionState,
        )

        # TRADE_MANAGEMENT 밖에서는 모든 병렬 Region이 비활성 상태여야 한다.
        if self.root_state is not RootState.TRADE_MANAGEMENT:
            inactive_values = (
                self.ownership_state,
                self.case_b_signal_state,
                self.case_c_signal_state,
                self.case_b_position_state,
                self.case_c_position_state,
            )
            if any(value is not None for value in inactive_values):
                raise ValueError("Regions must be inactive outside TRADE_MANAGEMENT")
            return

        # 복합 상태에 진입한 경우 세 병렬 Region의 기본 상태가 모두 필요하다.
        if self.ownership_state is None:
            raise ValueError("TRADE_MANAGEMENT requires an ownership state")
        if self.case_b_signal_state is None or self.case_c_signal_state is None:
            raise ValueError("TRADE_MANAGEMENT requires both signal Regions")

        # 포지션 소유권과 Case별 포지션 하위 상태가 서로 일치하는지 확인한다.
        if self.ownership_state is OwnershipState.NO_POSITION:
            if (
                self.case_b_position_state is not None
                or self.case_c_position_state is not None
            ):
                raise ValueError("NO_POSITION cannot have a position substate")
        elif self.ownership_state is OwnershipState.CASE_B_POSITION_MANAGEMENT:
            if (
                self.case_b_position_state is None
                or self.case_c_position_state is not None
            ):
                raise ValueError("Case B ownership requires only a Case B substate")
        elif self.ownership_state is OwnershipState.CASE_C_POSITION_MANAGEMENT:
            if (
                self.case_c_position_state is None
                or self.case_b_position_state is not None
            ):
                raise ValueError("Case C ownership requires only a Case C substate")

    @classmethod
    def create_trade_management_initial_state(cls) -> "TradingStateConfiguration":
        """
        함수 이름: create_trade_management_initial_state()
        기능: 세 병렬 Region의 initial transition이 완료된 초기 상태 구성을 생성한다.
        인자: 없음
        반환값: TRADE_MANAGEMENT 초기 상태 구성
        작성 날짜: 2026/08/14
        """
        return cls(
            root_state=RootState.TRADE_MANAGEMENT,
            ownership_state=OwnershipState.NO_POSITION,
            case_b_signal_state=CaseBSignalState.B_WAIT_TOUCH,
            case_c_signal_state=CaseCSignalState.C_WAIT_SETUP,
        )

    @property
    def trade_management_is_complete(self) -> bool:
        """
        함수 이름: trade_management_is_complete()
        기능: 무포지션이며 Case B와 Case C 신호 Region이 모두 Final인지 판정한다.
        인자: 없음
        반환값: TRADE_MANAGEMENT 완료 여부
        작성 날짜: 2026/08/14
        """
        return (
            self.root_state is RootState.TRADE_MANAGEMENT
            and self.ownership_state is OwnershipState.NO_POSITION
            and self.case_b_signal_state is CaseBSignalState.CASE_B_FINAL_STATE
            and self.case_c_signal_state is CaseCSignalState.CASE_C_FINAL_STATE
        )


def _require_enum(field_name: str, value: object, enum_type: type[Enum]) -> None:
    """
    함수 이름: _require_enum()
    기능: 필드 값이 요청된 enum 타입인지 검증한다.
    인자: field_name -> 오류 메시지에 사용할 필드 이름
        value -> 검증할 값
        enum_type -> 허용할 enum 클래스
    반환값: 없음
    작성 날짜: 2026/08/14
    """
    if not isinstance(value, enum_type):
        raise TypeError(f"{field_name} must be {enum_type.__name__}")


def _require_optional_enum(
    field_name: str,
    value: object,
    enum_type: type[Enum],
) -> None:
    """
    함수 이름: _require_optional_enum()
    기능: 선택 필드가 None이거나 요청된 enum 타입인지 검증한다.
    인자: field_name -> 오류 메시지에 사용할 필드 이름
        value -> 검증할 선택 값
        enum_type -> 허용할 enum 클래스
    반환값: 없음
    작성 날짜: 2026/08/14
    """
    if value is not None:
        _require_enum(field_name, value, enum_type)
