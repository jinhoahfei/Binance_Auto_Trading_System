"""실행 중인 TradingSTM의 전략 표시용 불변 스냅샷을 구성한다."""

from dataclasses import dataclass

from ..domain.common import RegimeType
from ..domain.trading.context import TradingRuntimeSnapshot
from ..domain.trading.states import (
    CaseBSignalState,
    CaseCSignalState,
    RootState,
    StrategyType,
)
from ..domain.trading.stm import TradingSTM
from .trading_indicator_snapshot import TradingIndicatorSnapshot


@dataclass(frozen=True, slots=True)
class TradingLogicSnapshot:
    """
    클래스 이름: TradingLogicSnapshot
    기능: 실제 STM의 REGIME, 최상위 상태와 활성 Case를 표시용으로 보존한다.
    작성 날짜: 2026/09/05
    """

    regime_type: RegimeType
    root_state: RootState
    active_strategies: tuple[StrategyType, ...]
    indicators: TradingIndicatorSnapshot | None = None


def create_trading_logic_snapshot(
    stm: TradingSTM | None,
    runtime: TradingRuntimeSnapshot,
) -> TradingLogicSnapshot | None:
    """
    함수 이름: create_trading_logic_snapshot()
    기능: 주문·포지션 소유 Case를 우선하고 무포지션에서는 활성 신호 Region을 조회한다.
    인자: stm -> 실행 중인 세션 STM 또는 None
        runtime -> 같은 Controller lock에서 읽은 불변 전략 runtime
    반환값: 실행 로직 스냅샷 또는 시작 전·종료 후의 None
    작성 날짜: 2026/09/05
    """
    # 선택만 된 REGIME이나 종료된 STM을 현재 실행 중인 전략으로 노출하지 않는다.
    if stm is None:
        return None
    state = stm.current_state  # 병렬 Region 전체를 하나의 불변 상태에서 읽는다.
    if state.root_state in (RootState.NOT_STARTED, RootState.LOGIC_TERMINATED):
        return None

    # 실제 주문 또는 포지션을 소유한 Case는 다른 신호 Region의 대기 상태보다 우선한다.
    active_strategies: tuple[StrategyType, ...] = ()
    if state.root_state is RootState.LOWER_TOUCH_WATCH:
        active_strategies = ()  # 하단 접촉 전에는 Case 신호 Region 자체가 아직 실행되지 않는다.
    elif runtime.position_owner is not None:
        active_strategies = (runtime.position_owner,)
    elif runtime.pending_strategy is not None and (
        runtime.pending_order_id is not None or runtime.pending_intent_id is not None
    ):
        active_strategies = (runtime.pending_strategy,)
    elif state.root_state is RootState.TRADE_MANAGEMENT:
        # 포지션 진입 전에는 켜져 있고 아직 종료되지 않은 신호 Region을 모두 공개한다.
        signal_strategies: list[StrategyType] = []
        if runtime.case_b_enabled and state.case_b_signal_state not in (
            None,
            CaseBSignalState.CASE_B_FINAL_STATE,
        ):
            signal_strategies.append(StrategyType.CASE_B)
        if runtime.case_c_enabled and state.case_c_signal_state not in (
            None,
            CaseCSignalState.CASE_C_FINAL_STATE,
        ):
            signal_strategies.append(StrategyType.CASE_C)
        active_strategies = tuple(signal_strategies)  # 외부 조회가 내부 목록을 변경하지 못하게 한다.

    # REGIME은 UI 후보 선택값이 아닌 이 세션 STM의 고정된 구성에서 얻는다.
    return TradingLogicSnapshot(
        regime_type=stm.regime_type,
        root_state=state.root_state,
        active_strategies=active_strategies,
    )
