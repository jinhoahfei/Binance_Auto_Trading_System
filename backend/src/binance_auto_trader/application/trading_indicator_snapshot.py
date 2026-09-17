"""STM microstep의 조건 결과를 단계별로 보존하고 표시용 스냅샷을 구성한다."""

from dataclasses import dataclass, field, replace
from datetime import datetime

from ..domain.trading.conditions import TradingCondition, condition_unmet, evaluate_condition, unevaluated_condition
from ..domain.trading.context import TradingContextView
from ..domain.trading.results import TradingSTMResult
from ..domain.trading.states import (
    CaseBSignalState,
    CaseCPositionState,
    CaseCSignalState,
    RootState,
    StrategyType,
    TradingStateConfiguration,
)
from ..domain.trading.stm import TradingSTM
from ..domain.trading.timers import TradingTimerSnapshot
from .trading_indicator_timers import TradingIndicatorTimers


@dataclass(frozen=True, slots=True)
class IndicatorSlot:
    """
    클래스 이름: IndicatorSlot
    기능: 현재 세부 단계가 평가하는 조건의 소속과 식별자를 보존한다.
    작성 날짜: 2026/09/05
    """

    condition_id: str
    strategy: StrategyType | None
    phase: str


@dataclass(frozen=True, slots=True)
class TradingIndicatorEvaluation:
    """
    클래스 이름: TradingIndicatorEvaluation
    기능: 조건 결과와 실제 평가 시각·시장 및 Context 버전을 함께 보존한다.
    작성 날짜: 2026/09/05
    """

    slot: IndicatorSlot
    condition: TradingCondition
    evaluated_at: datetime | None = None
    market_version: int | None = None
    context_version: int | None = None
    timer: TradingTimerSnapshot | None = None


@dataclass(frozen=True, slots=True)
class TradingIndicatorPhase:
    """
    클래스 이름: TradingIndicatorPhase
    기능: 지표가 없는 주문·종료 단계도 Case별 현재 상태와 진입 제한을 보존한다.
    작성 날짜: 2026/09/10
    """

    strategy: StrategyType
    phase: str
    notice: str | None = None


@dataclass(frozen=True, slots=True)
class TradingIndicatorSnapshot:
    """
    클래스 이름: TradingIndicatorSnapshot
    기능: Case별 현재 단계와 해당 단계의 지표 평가를 원자적으로 전달한다.
    작성 날짜: 2026/09/05
    """

    phase_key: str
    notice: str | None
    conditions: tuple[TradingIndicatorEvaluation, ...]
    captured_at: datetime | None = field(default=None, compare=False)  # 조회 시각만으로 background publication을 반복하지 않는다.
    phases: tuple[TradingIndicatorPhase, ...] = ()


# UI용 이름과 색상은 포함하지 않고 단계가 사용하는 조건 ID만 연결한다.
_PHASE_CONDITIONS = {
    "LOWER_TOUCH_WATCH": ("lower_price",),
    "B_WAIT_TOUCH": ("b_touch_bbw",),
    "B_WAIT_SIGNAL": ("b_signal_slope", "b_signal_pct_b", "b_signal_low"),
    "B_WAIT_PULLBACK": ("b_pullback", "b_signal_age"),
    "B_POSITION_OPEN_SIGNALLED": ("b_signal_age",),
    "CASE_B_HOLDING": ("b_profit_zone", "b_take_profit_slope", "b_trend_slope", "b_stop", "b_emergency_stop", "b_time_exit"),
    "CASE_B_TREND_HOLD": ("b_trend_exit_slope", "b_trend_exit_pct_b"),
    "C_WAIT_SETUP": ("c_setup_pct_b", "c_setup_cci"),
    "C_SETUP_FLUSH": ("c_flush", "c_recovery"),
    "C_SETUP_RECOVERY": ("c_new_low", "c_rebound", "c_recovery_window", "c_entry_limit", "c_recovery"),
    "CASE_C_HOLDING": ("c_profit_zone", "c_stop", "c_time_exit"),
    "CASE_C_TP_TRAILING": ("c_trail_fallback", "c_trail_increase", "c_time_exit"),
    "CASE_C_CLOSED": ("c_recovery",),
    "CASE_C_RECOVERY_SUCCEEDED": ("c_handoff",),
}


def select_indicator_slots(
    state: TradingStateConfiguration,
    context: TradingContextView,
) -> tuple[tuple[IndicatorSlot, ...], str, str | None]:
    """
    함수 이름: select_indicator_slots()
    기능: 실제 소유권·주문·병렬 Region으로 현재 평가 대상만 선택한다.
    인자: state -> 같은 microstep의 STM 상태
        context -> 해당 상태의 불변 Context
    반환값: 조건 자리 목록, 단계 식별자, 선택적인 운영 안내 코드
    작성 날짜: 2026/09/05
    """
    runtime = context.runtime
    phases: list[tuple[StrategyType | None, str]] = []
    notice = None

    # 주문 처리와 종료에서는 재평가하지 않는 진입·청산 지표를 즉시 제거한다.
    if state.root_state is RootState.LOWER_TOUCH_WATCH:
        phases.append((None, state.root_state.value))
    elif state.root_state is RootState.TRADE_MANAGEMENT:
        if runtime.pending_order_id or runtime.pending_intent_id or runtime.pending_exit_reason:
            notice = "order_pending"
            if (
                runtime.pending_order_id is None
                and runtime.position_owner is None
                and runtime.pending_exit_reason is None
                and state.case_b_signal_state is CaseBSignalState.B_POSITION_OPEN_SIGNALLED
                and runtime.signal_created
            ):
                phases.append((StrategyType.CASE_B, "B_POSITION_OPEN_SIGNALLED"))
        elif state.case_c_position_state in (
            CaseCPositionState.CASE_C_CLOSED,
            CaseCPositionState.CASE_C_RECOVERY_SUCCEEDED,
        ):
            phases.append((StrategyType.CASE_C, state.case_c_position_state.value))
            if runtime.case_b_enabled and state.case_b_signal_state in (
                CaseBSignalState.B_WAIT_SIGNAL, CaseBSignalState.B_WAIT_PULLBACK,
            ):
                phases.append((StrategyType.CASE_B, state.case_b_signal_state.value))
            notice = "entry_paused"  # 회복을 기다리는 동안 B 신규 진입은 중지된다.
        elif runtime.position_owner is StrategyType.CASE_B and state.case_b_position_state is not None:
            phases.append((StrategyType.CASE_B, state.case_b_position_state.value))
        elif runtime.position_owner is StrategyType.CASE_C and state.case_c_position_state is not None:
            phases.append((StrategyType.CASE_C, state.case_c_position_state.value))
            # B-12/B-13은 매수만 중지한다. C 보유 중에도 B의 확정봉과 신호 유효시간을 보존한다.
            if runtime.case_b_enabled and state.case_b_signal_state in (
                CaseBSignalState.B_WAIT_SIGNAL, CaseBSignalState.B_WAIT_PULLBACK,
            ):
                phases.append((StrategyType.CASE_B, state.case_b_signal_state.value))
        else:
            if runtime.case_b_enabled and state.case_b_signal_state not in (None, CaseBSignalState.CASE_B_FINAL_STATE):
                phases.append((StrategyType.CASE_B, state.case_b_signal_state.value))
            if runtime.case_c_enabled and state.case_c_signal_state not in (None, CaseCSignalState.CASE_C_FINAL_STATE):
                phase = state.case_c_signal_state.value
                if state.case_c_signal_state is CaseCSignalState.C_SETUP:
                    phase = "C_SETUP_FLUSH" if runtime.flush_low is None else "C_SETUP_RECOVERY"
                phases.append((StrategyType.CASE_C, phase))
            if runtime.case_b_entry_paused:
                notice = "entry_paused"
            elif any(phase in ("B_POSITION_OPEN_SIGNALLED", "C_POSITION_OPEN_SIGNALLED") for _, phase in phases):
                notice = "order_pending"  # B 재시도는 신호 유효시간만, C 재시도는 시장 조건 없이 대기한다.
        phases.append((None, "UPPER_SAFE_EXIT"))  # 기존 전송 ID를 유지하며 공통 상단 접촉 조건을 한 번만 둔다.
    else:
        notice = "stopping" if state.root_state is RootState.STOPPING else "inactive"

    # 주문·pause 변화를 단계 키에 포함해 이전 행이나 스크롤 위치를 재사용하지 않는다.
    slots = tuple(
        IndicatorSlot(condition_id, strategy, phase)
        for strategy, phase in phases
        for condition_id in (
            ("upper_safe_exit",) if phase == "UPPER_SAFE_EXIT" else _PHASE_CONDITIONS.get(phase, ())
        )
    )
    phase_key = ":".join((state.root_state.value, *(phase for _, phase in phases), notice or ""))
    return slots, phase_key, notice


def select_indicator_phases(
    state: TradingStateConfiguration,
    context: TradingContextView,
) -> tuple[TradingIndicatorPhase, ...]:
    """
    함수 이름: select_indicator_phases()
    기능: 같은 STM snapshot에서 두 Case의 단계와 각 Case에만 적용되는 제한을 읽는다.
    인자: state -> 현재 병렬 Region 상태
        context -> 해당 상태의 불변 Context
    반환값: B·C 순서의 단계 목록 또는 하단 이벤트 밖의 빈 tuple
    작성 날짜: 2026/09/10
    """
    if state.root_state is not RootState.TRADE_MANAGEMENT:
        return ()
    runtime = context.runtime
    phases = []
    for strategy, position_state, signal_state in (
        (StrategyType.CASE_B, state.case_b_position_state, state.case_b_signal_state),
        (StrategyType.CASE_C, state.case_c_position_state, state.case_c_signal_state),
    ):
        current_state = position_state or signal_state
        if current_state is None:
            continue
        phase = current_state.value
        notice = None
        if strategy is StrategyType.CASE_C and signal_state is CaseCSignalState.C_SETUP and position_state is None:
            phase = "C_SETUP_FLUSH" if runtime.flush_low is None else "C_SETUP_RECOVERY"
        if runtime.pending_strategy is strategy and (
            runtime.pending_order_id or runtime.pending_intent_id or runtime.pending_exit_reason
        ):
            notice = "order_pending"
        elif phase in ("B_POSITION_OPEN_SIGNALLED", "C_POSITION_OPEN_SIGNALLED"):
            notice = "order_pending"
        elif strategy is StrategyType.CASE_B and phase == "CASE_B_FINAL_STATE" and not runtime.case_b_enabled:
            notice = "bbw_rejected" if condition_unmet("b_touch_bbw", context) else "case_finished"
        elif phase in ("CASE_B_FINAL_STATE", "CASE_C_FINAL_STATE"):
            notice = "case_finished"
        elif runtime.pending_order_id or runtime.pending_intent_id or runtime.pending_exit_reason:
            notice = "other_order_pending"
        elif strategy is StrategyType.CASE_B and runtime.case_b_entry_paused:
            notice = "entry_paused"
        phases.append(TradingIndicatorPhase(strategy, phase, notice))
    return tuple(phases)


class TradingIndicatorStore:
    """
    클래스 이름: TradingIndicatorStore
    기능: 성공한 처리 단위의 평가만 보존하고 다른 단계의 결과가 노출되지 않게 한다.
    작성 날짜: 2026/09/05
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 세션 및 lower-event 범위의 비어 있는 평가 저장소를 만든다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/05
        """
        self._scope: tuple[TradingSTM, str | None] | None = None
        self._evaluations: dict[IndicatorSlot, TradingIndicatorEvaluation] = {}  # mutable 상태는 application만 소유한다.
        self._timers = TradingIndicatorTimers()
        self._market_version = 0
        self._market_origins: dict[str, object] = {}

    def _synchronize(self, stm: TradingSTM, context: TradingContextView, slots: tuple[IndicatorSlot, ...]) -> None:
        """
        함수 이름: _synchronize()
        기능: 세션·lower-event·단계가 바뀌면 사용할 수 없는 이전 평가를 제거한다.
        인자: stm -> 현재 세션 STM
            context -> 같은 lock의 Context
            slots -> 현재 단계가 사용하는 조건 자리
        반환값: 없음
        작성 날짜: 2026/09/05
        """
        scope = (stm, context.runtime.lower_event_id)
        if self._scope != scope:
            self._evaluations.clear()  # 같은 Case로 재진입하더라도 이전 거래의 값을 재사용하지 않는다.
            self._scope = scope
            self._timers = TradingIndicatorTimers()
            self._market_version = 0
            self._market_origins = {}
        self._evaluations = {slot: value for slot, value in self._evaluations.items() if slot in slots}

    def observe(
        self,
        stm: TradingSTM,
        result: TradingSTMResult,
        before: TradingContextView,
        after: TradingContextView,
        market_version: int,
        entered_at: datetime | None = None,
    ) -> None:
        """
        함수 이름: observe()
        기능: 성공한 action batch의 원본 입력으로 평가하고 새 단계와 일치하는 결과만 남긴다.
        인자: stm -> 실행 중인 STM
            result -> 완료된 전이 결과
            before -> 전이가 실제 읽은 Context
            after -> action 적용 후 Context
            market_version -> 실제 적용된 시장 평가 version
            entered_at -> authoritative Position이 보존하는 최초 진입 시각
        반환값: 없음
        작성 날짜: 2026/09/05
        """
        slots, _, _ = select_indicator_slots(result.state_before, before)
        self._synchronize(stm, before, slots)

        # 경과 시간을 만든 시장 입력의 기준은 내부 microstep에서 runtime이 바뀌어도 보존한다.
        if market_version != self._market_version:
            self._market_version = market_version
            self._market_origins = {
                "b_signal_age": before.runtime.signal_time,
                "c_recovery_window": (before.runtime.timer_base_time, before.runtime.timer_base_pct_b, before.runtime.flush_low),
                "b_time_exit": entered_at,
                "c_time_exit": entered_at,
            }
        recovery_origin = (before.runtime.timer_base_time, before.runtime.timer_base_pct_b, before.runtime.flush_low)

        # 확정봉이 아닌 tick은 마지막 확정 평가를 덮어쓰지 않는다.
        for slot in slots:
            if slot.condition_id in ("c_recovery_window", "c_rebound", "c_entry_limit") and self._market_origins.get("c_recovery_window") != recovery_origin:
                continue  # 새 회복 기준에 이전 시장 평가의 경과 시간과 판정을 결합하지 않는다.
            if slot.condition_id == "b_signal_age" and self._market_origins.get("b_signal_age") != before.runtime.signal_time:
                continue
            condition = evaluate_condition(slot.condition_id, before)
            if market_version <= 0:
                continue
            if condition.source == "close_30m" and not before.market.confirmed_30m_close:
                continue
            if condition.source == "close_1m" and not before.market.confirmed_1m_close:
                continue
            previous = self._evaluations.get(slot)
            if previous is not None and condition.source in ("close_1m", "close_30m") and previous.market_version == market_version:
                continue  # PC-15 기준 갱신 뒤에도 비교 당시 이전 기준을 그대로 보존한다.
            self._evaluations[slot] = TradingIndicatorEvaluation(
                slot, condition, before.evaluated_at, market_version, before.version,
            )

        # 같은 단계 안에서 C 기준만 바뀌어도 이전 회차의 동적 기준·시간 판정을 모두 비운다.
        next_recovery_origin = (after.runtime.timer_base_time, after.runtime.timer_base_pct_b, after.runtime.flush_low)
        invalidated = set()
        if next_recovery_origin != recovery_origin:
            invalidated.update(("c_recovery_window", "c_rebound", "c_entry_limit"))
        if before.runtime.signal_time != after.runtime.signal_time:
            invalidated.add("b_signal_age")
        self._evaluations = {slot: value for slot, value in self._evaluations.items() if slot.condition_id not in invalidated}
        self._timers.observe(before, after, entered_at, self._market_origins)

        # 새 단계의 첫 평가 전에는 새 목록의 자리를 미수신으로 남긴다.
        next_slots, _, _ = select_indicator_slots(result.state_after, after)
        self._synchronize(stm, after, next_slots)
        if next_recovery_origin != recovery_origin and after.runtime.timer_base_time is not None:
            # 새 회복 목표는 이미 확정된 runtime 값이므로 회색 판정과 함께 즉시 공개한다.
            for slot in next_slots:
                if slot.condition_id == "c_rebound":
                    condition = replace(unevaluated_condition(slot.condition_id), threshold=after.runtime.entry_pct_b)
                    self._evaluations[slot] = TradingIndicatorEvaluation(slot, condition)  # 새 시장 평가 전까지 값·충족 여부는 비운다.

    def snapshot(self, stm: TradingSTM, context: TradingContextView) -> TradingIndicatorSnapshot:
        """
        함수 이름: snapshot()
        기능: 현재 단계와 저장된 평가를 묶고 미평가 항목의 값을 명시적으로 비운다.
        인자: stm -> 현재 STM
            context -> publication과 동일한 Context
        반환값: 전송 가능한 불변 지표 스냅샷
        작성 날짜: 2026/09/05
        """
        slots, phase_key, notice = select_indicator_slots(stm.current_state, context)
        self._synchronize(stm, context, slots)

        # 아직 평가하지 않은 동적 기준도 현재 runtime 값으로 추측해 표시하지 않는다.
        evaluations = []
        for slot in slots:
            evaluation = self._evaluations.get(slot)
            if evaluation is None:
                condition = unevaluated_condition(slot.condition_id)
                evaluation = TradingIndicatorEvaluation(slot, condition)
            evaluations.append(replace(evaluation, timer=self._timers.get(slot.condition_id)))
        return TradingIndicatorSnapshot(
            phase_key, notice, tuple(evaluations), context.evaluated_at,
            select_indicator_phases(stm.current_state, context),
        )
