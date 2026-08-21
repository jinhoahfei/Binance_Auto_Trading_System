"""동기식이며 결정론적인 TradingSTM 전이 판단 엔진을 제공한다."""

from __future__ import annotations

from dataclasses import replace
from threading import Lock

from ..common import RegimeType
from .action_requests import QueueEvent, SubmitOrder, TradingActionRequest
from .context import TradingContextView
from .events import EventPriority, TradingEvent, TradingEventType
from .logic_registry import (
    TradingLogicConfiguration,
    require_supported_trading_logic_configuration,
)
from .results import TradingSTMResult
from .states import (
    CaseBPositionState,
    CaseBSignalState,
    CaseCPositionState,
    CaseCSignalState,
    OrderSide,
    RootState,
    StrategyType,
    TradingStateConfiguration,
)
from .transitions.base import TransitionOutcome
from .transitions.case_b_position_transitions import (
    handle_case_b_position_transition,
)
from .transitions.case_b_signal_transitions import handle_case_b_signal_transition
from .transitions.case_c_position_transitions import (
    handle_case_c_position_transition,
)
from .transitions.case_c_signal_transitions import handle_case_c_signal_transition
from .transitions.global_transitions import handle_global_transition
from .transitions.ownership_transitions import handle_ownership_transition


class TradingSTM:
    """
    클래스 이름: TradingSTM
    기능: 거래 상태 구성을 소유하고 부수 효과 없이 전이를 판단한다.
    작성 날짜: 2026/08/14
    """

    def __init__(self, regime_type: RegimeType) -> None:
        """
        함수 이름: __init__()
        기능: 전략 유형과 초기 상태를 저장하고 비재진입 잠금을 준비한다.
        인자: regime_type -> 이 상태 머신이 처리할 전략 유형
        반환값: 없음
        작성 날짜: 2026/08/14
        """
        # 지원 여부를 먼저 검증해 누락 입력과 다른 REGIME의 TYPE_0 fallback을 차단한다.
        self._configuration = require_supported_trading_logic_configuration(
            regime_type
        )

        # 세션이 시작된 뒤 바뀌지 않는 전략 registry와 초기 상태 구성을 저장한다.
        self._state = TradingStateConfiguration()

        # 동시에 두 이벤트가 상태를 변경하지 못하도록 비재진입 잠금을 사용한다.
        self._handle_lock = Lock()

    @classmethod
    def get_stm_instance(cls, regime_type: RegimeType) -> "TradingSTM":
        """
        함수 이름: get_stm_instance()
        기능: 하나의 거래 세션에 전용으로 사용할 상태 머신을 생성한다.
        인자: regime_type -> 생성할 상태 머신의 전략 유형
        반환값: 지정된 전략 유형으로 초기화한 TradingSTM 인스턴스
        작성 날짜: 2026/08/14
        """
        return cls(regime_type)  # 생성자가 support gate와 registry 선택을 단일 수행한다.

    @property
    def regime_type(self) -> RegimeType:
        """
        함수 이름: regime_type()
        기능: 상태 머신 생성 시 선택한 불변 전략 유형을 조회한다.
        인자: 없음
        반환값: 현재 상태 머신의 전략 유형
        작성 날짜: 2026/08/14
        """
        return self._configuration.regime_type  # 별도의 mutable 선택값을 두지 않는다.

    @property
    def configuration(self) -> TradingLogicConfiguration:
        """
        함수 이름: configuration()
        기능: 상태 머신이 선택한 불변 거래 로직 registry 구성을 조회한다.
        인자: 없음
        반환값: 현재 REGIME의 TradingLogicConfiguration
        작성 날짜: 2026/08/21
        """
        return self._configuration  # session 전체에서 같은 frozen 객체를 공개한다.

    @property
    def current_state(self) -> TradingStateConfiguration:
        """
        함수 이름: current_state()
        기능: 현재의 불변 상태 구성 스냅샷을 조회한다.
        인자: 없음
        반환값: 현재 TradingStateConfiguration
        작성 날짜: 2026/08/14
        """
        return self._state

    def run(self, context: TradingContextView) -> TradingSTMResult:
        """
        함수 이름: run()
        기능: 최초 LOGIC_STARTED 마이크로스텝을 한 번만 처리한다.
        인자: context -> 최초 전이 판단에 사용할 거래 컨텍스트 스냅샷
        반환값: 최초 전이의 상태 변경과 액션 요청을 담은 결과
        작성 날짜: 2026/08/14
        """
        if self._state.root_state is not RootState.NOT_STARTED:
            raise RuntimeError("TradingSTM.run() can only be called once")

        # 외부 큐를 거치지 않는 최초 이벤트에도 추적 가능한 고정 식별자를 부여한다.
        event = TradingEvent(
            event_type=TradingEventType.LOGIC_STARTED,
            occurred_at=context.evaluated_at,
            sequence_number=0,
            priority=EventPriority.INTERNAL,
            event_id="logic-started",
        )
        return self.handle(event, context)

    def handle(
        self,
        event: TradingEvent,
        context: TradingContextView,
    ) -> TradingSTMResult:
        """
        함수 이름: handle()
        기능: 하나의 이벤트를 원자적이고 비재진입 방식으로 처리한다.
        인자: event -> 처리할 정규화 이벤트
            context -> 전이 판단 전체에서 사용할 불변 컨텍스트 스냅샷
        반환값: 선택한 전이와 후속 액션 요청을 담은 결과
        작성 날짜: 2026/08/14
        """
        # 재귀 호출이나 동시 호출이 같은 상태를 중복 변경하는 것을 차단한다.
        if not self._handle_lock.acquire(blocking=False):
            raise RuntimeError("TradingSTM.handle() is not reentrant")

        try:
            return self._handle_locked(event, context)
        finally:
            self._handle_lock.release()

    def order_finished(
        self,
        event: TradingEvent,
        context: TradingContextView,
    ) -> TradingSTMResult:
        """
        함수 이름: order_finished()
        기능: 주문 완료 콜백을 구체적인 정규화 결과 이벤트로 처리한다.
        인자: event -> 성공 또는 실패가 명시된 주문 결과 이벤트
            context -> 주문 결과 전이에 사용할 거래 컨텍스트 스냅샷
        반환값: 주문 결과 이벤트를 처리한 상태 머신 결과
        작성 날짜: 2026/08/14
        """
        # 모호한 공통 완료 이벤트 대신 전략과 결과가 확정된 이벤트만 허용한다.
        allowed_events = {
            TradingEventType.CASE_B_POSITION_OPENED,
            TradingEventType.CASE_B_BUY_FAILED,
            TradingEventType.CASE_B_SELL_FILLED,
            TradingEventType.CASE_B_SELL_FAILED,
            TradingEventType.CASE_C_POSITION_OPENED,
            TradingEventType.CASE_C_BUY_FAILED,
            TradingEventType.CASE_C_SELL_FILLED,
            TradingEventType.CASE_C_SELL_FAILED,
            TradingEventType.FORCE_SELL_FINISHED,
            TradingEventType.FORCE_SELL_FAILED,
        }
        if event.event_type not in allowed_events:
            raise ValueError(
                "order_finished() requires a concrete normalized order outcome event"
            )

        return self.handle(event, context)

    def rollback_unpublished_result(
        self,
        result: TradingSTMResult,
    ) -> None:
        """
        함수 이름: rollback_unpublished_result()
        기능: Context version race로 action publication 전 폐기된 최신 STM 결과를 원상 복구한다.
        인자: result -> action 실행 전에 폐기하기로 결정된 최신 TradingSTMResult
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        if not isinstance(result, TradingSTMResult):
            raise TypeError("result must be a TradingSTMResult")

        # 다른 handle 또는 rollback과 겹치면 상태를 추측해서 되돌리지 않는다.
        if not self._handle_lock.acquire(blocking=False):
            raise RuntimeError("TradingSTM rollback is not reentrant")

        try:
            if self._state != result.state_after:
                raise RuntimeError(
                    "Only the latest unpublished TradingSTM result can be rolled back"
                )

            self._state = result.state_before  # Context에 publish되지 않은 전이만 되돌린다.
        finally:
            self._handle_lock.release()

    def _handle_locked(
        self,
        event: TradingEvent,
        context: TradingContextView,
    ) -> TradingSTMResult:
        """
        함수 이름: _handle_locked()
        기능: 각 Region의 전이 후보를 수집하고 충돌을 해결한 뒤 한 번만 커밋한다.
        인자: event -> 현재 마이크로스텝에서 처리할 이벤트
            context -> 모든 Region이 공유할 불변 컨텍스트 스냅샷
        반환값: 원자적으로 확정한 상태 머신 처리 결과
        작성 날짜: 2026/08/14
        """
        state_before = self._state

        # STOP과 강제 매도 같은 전역 전이는 모든 병렬 Region보다 먼저 평가한다.
        global_outcome = handle_global_transition(state_before, event, context)
        if global_outcome is not None:
            return self._commit(event, context, state_before, global_outcome)

        # 복합 상태 밖에서는 하위 Region 이벤트를 소비하지 않는다.
        if state_before.root_state is not RootState.TRADE_MANAGEMENT:
            return self._no_op(event, context, state_before)

        state_after = state_before
        transition_ids: list[str] = []
        accepted_actions: list[TradingActionRequest] = []

        # 포지션 소유권 Region을 먼저 평가하여 배타적 종료를 즉시 확정한다.
        region_one_event = _resolve_region_one_event(state_after, event)
        region_one = self._handle_region_one(
            state_after,
            region_one_event,
            context,
        )
        if region_one is not None:
            state_after = region_one.state_after
            transition_ids.extend(region_one.transition_ids)
            accepted_actions.extend(region_one.action_requests)
            if region_one.exclusive:
                combined = TransitionOutcome(
                    tuple(transition_ids),
                    state_after,
                    tuple(accepted_actions),
                    exclusive=True,
                )
                return self._commit(event, context, state_before, combined)

        # Case C 신호 Region을 Case B보다 먼저 평가하여 동시 매수 충돌 우선순위를 지킨다.
        case_c_event = _resolve_case_c_region_event(state_after, event)
        case_c = handle_case_c_signal_transition(
            state_after,
            case_c_event,
            context,
        )
        if case_c is not None:
            state_after = case_c.state_after
            transition_ids.extend(case_c.transition_ids)
            accepted_actions.extend(case_c.action_requests)

        case_b_event = _resolve_case_b_region_event(state_after, event, context)
        case_b = handle_case_b_signal_transition(
            state_after,
            case_b_event,
            context,
        )
        if case_b is not None and not _does_case_c_buy_win(
            accepted_actions,
            case_b,
        ):
            state_after = case_b.state_after
            transition_ids.extend(case_b.transition_ids)
            accepted_actions.extend(case_b.action_requests)

        # 어느 Region도 이벤트를 소비하지 않았다면 상태를 그대로 유지한다.
        if not transition_ids:
            return self._no_op(event, context, state_before)

        # 모든 병렬 Region이 종료된 순간에만 상위 복합 상태 완료 이벤트를 예약한다.
        if (
            not state_before.trade_management_is_complete
            and state_after.trade_management_is_complete
        ):
            accepted_actions.append(
                QueueEvent(
                    event_type=TradingEventType.TRADE_MANAGEMENT_COMPLETED,
                    lower_event_id=context.runtime.lower_event_id,
                )
            )

        combined = TransitionOutcome(
            tuple(transition_ids),
            state_after,
            tuple(accepted_actions),
        )

        # 여러 Region에서 모은 상태와 액션을 하나의 결과로 묶어 원자적으로 반영한다.
        return self._commit(event, context, state_before, combined)

    @staticmethod
    def _handle_region_one(
        state: TradingStateConfiguration,
        event: TradingEvent,
        context: TradingContextView,
    ) -> TransitionOutcome | None:
        """
        함수 이름: _handle_region_one()
        기능: 소유권 Region에서 현재 활성화된 하나의 하위 상태 머신을 평가한다.
        인자: state -> Region 평가 전 상태 구성
            event -> Region 상태에 맞게 변환된 이벤트
            context -> 전이 조건 평가에 사용할 거래 컨텍스트
        반환값: 선택된 전이 결과 또는 전이가 없을 때 None
        작성 날짜: 2026/08/14
        """
        # 활성 포지션 상태 머신은 소유권 대기 상태보다 우선하여 단 하나만 평가한다.
        if state.case_b_position_state is not None:
            return handle_case_b_position_transition(state, event, context)
        if state.case_c_position_state is not None:
            return handle_case_c_position_transition(state, event, context)

        return handle_ownership_transition(state, event, context)

    def _commit(
        self,
        event: TradingEvent,
        context: TradingContextView,
        state_before: TradingStateConfiguration,
        selected: TransitionOutcome,
    ) -> TradingSTMResult:
        """
        함수 이름: _commit()
        기능: 전이 추적 식별자를 검증하고 전체 상태 구성을 한 번에 교체한다.
        인자: event -> 결과 식별자 생성에 사용할 처리 이벤트
            context -> 결과에 기록할 거래 컨텍스트 스냅샷
            state_before -> 마이크로스텝 처리 전 상태 구성
            selected -> 충돌 해결 후 최종 선택한 전이 결과
        반환값: 커밋된 상태와 액션 요청을 담은 TradingSTMResult
        작성 날짜: 2026/08/14
        """
        # 문서화되지 않은 전이 식별자가 실행 추적에 섞이지 않도록 먼저 검증한다.
        unknown_ids = set(selected.transition_ids).difference(
            self._configuration.transition_ids
        )
        if unknown_ids:
            raise RuntimeError(f"Unknown transition IDs: {sorted(unknown_ids)}")

        # 검증이 끝난 전체 불변 구성을 한 번의 대입으로 공개한다.
        self._state = selected.state_after
        return TradingSTMResult(
            decision_id=_create_decision_id(event, context, selected.transition_ids),
            consumed=True,
            transition_ids=selected.transition_ids,
            state_before=state_before,
            state_after=selected.state_after,
            action_requests=selected.action_requests,
            context_version=context.version,
        )

    @staticmethod
    def _no_op(
        event: TradingEvent,
        context: TradingContextView,
        state: TradingStateConfiguration,
    ) -> TradingSTMResult:
        """
        함수 이름: _no_op()
        기능: 상태를 변경하지 않은 이벤트도 추적 가능한 명시적 결과로 반환한다.
        인자: event -> 처리되지 않은 이벤트
            context -> 결과 식별자와 버전에 사용할 거래 컨텍스트
            state -> 변경 없이 유지할 현재 상태 구성
        반환값: consumed가 False인 TradingSTMResult
        작성 날짜: 2026/08/14
        """
        return TradingSTMResult(
            decision_id=_create_decision_id(event, context, ()),
            consumed=False,
            transition_ids=(),
            state_before=state,
            state_after=state,
            action_requests=(),
            context_version=context.version,
        )


def _does_case_c_buy_win(
    accepted_actions: list[TradingActionRequest],
    case_b_outcome: TransitionOutcome,
) -> bool:
    """
    함수 이름: _does_case_c_buy_win()
    기능: 같은 마이크로스텝의 Case C 매수와 Case B 매수 사이 우선순위를 판단한다.
    인자: accepted_actions -> 앞선 Region 평가에서 이미 채택한 액션 목록
        case_b_outcome -> 뒤이어 평가한 Case B 전이 후보
    반환값: Case C 매수를 유지하고 Case B 후보를 버려야 하면 True
    작성 날짜: 2026/08/14
    """
    # 실제 주문 제출 액션만 비교하여 보조 액션 때문에 충돌로 오인하지 않는다.
    case_c_buy_selected = any(
        isinstance(action, SubmitOrder)
        and action.strategy is StrategyType.CASE_C
        and action.side is OrderSide.BUY
        for action in accepted_actions
    )
    case_b_buy_candidate = any(
        isinstance(action, SubmitOrder)
        and action.strategy is StrategyType.CASE_B
        and action.side is OrderSide.BUY
        for action in case_b_outcome.action_requests
    )
    return case_c_buy_selected and case_b_buy_candidate


def _resolve_region_one_event(
    state: TradingStateConfiguration,
    event: TradingEvent,
) -> TradingEvent:
    """
    함수 이름: _resolve_region_one_event()
    기능: 시장 데이터 갱신 이벤트를 활성 포지션 상태의 재평가 이벤트로 변환한다.
    인자: state -> 이벤트 변환 기준이 되는 현재 상태 구성
        event -> 원본 정규화 이벤트
    반환값: 활성 Region에 맞게 변환했거나 그대로 유지한 이벤트
    작성 날짜: 2026/08/14
    """
    if event.event_type is not TradingEventType.MARKET_DATA_UPDATED:
        return event

    # Case B 포지션의 활성 하위 상태에 대응하는 조건 검사 이벤트를 선택한다.
    if state.case_b_position_state is not None:
        if state.case_b_position_state is CaseBPositionState.CASE_B_TREND_HOLD:
            return replace(
                event,
                event_type=TradingEventType.CASE_B_TREND_HOLD_CONDITION_CHECK,
            )
        if state.case_b_position_state is CaseBPositionState.CASE_B_HOLDING:
            return replace(
                event,
                event_type=TradingEventType.RETRY_CASE_B_CONDITION_CHECK,
            )

    # Case C 포지션의 활성 하위 상태에 대응하는 조건 검사 이벤트를 선택한다.
    if state.case_c_position_state is not None:
        if state.case_c_position_state is CaseCPositionState.CASE_C_HOLDING:
            return replace(
                event,
                event_type=TradingEventType.RETRY_CASE_C_CONDITION_CHECK,
            )
        if state.case_c_position_state is CaseCPositionState.CASE_C_TP_TRAILING:
            return replace(
                event,
                event_type=TradingEventType.RETRY_TP_TRAILING_CONDITION_CHECK,
            )
        if state.case_c_position_state is CaseCPositionState.CASE_C_CLOSED:
            return replace(
                event,
                event_type=TradingEventType.CHECK_CASE_C_RECOVERY,
            )

    return event


def _resolve_case_c_region_event(
    state: TradingStateConfiguration,
    event: TradingEvent,
) -> TradingEvent:
    """
    함수 이름: _resolve_case_c_region_event()
    기능: 시장 데이터 갱신 이벤트를 Case C 신호 Region의 재평가 이벤트로 변환한다.
    인자: state -> 이벤트 변환 기준이 되는 현재 상태 구성
        event -> 원본 정규화 이벤트
    반환값: Case C 신호 상태에 맞게 변환했거나 그대로 유지한 이벤트
    작성 날짜: 2026/08/14
    """
    if event.event_type is not TradingEventType.MARKET_DATA_UPDATED:
        return event

    # 현재 활성 신호 상태에서 수신 가능한 재평가 이벤트만 생성한다.
    if state.case_c_signal_state is CaseCSignalState.C_WAIT_SETUP:
        return replace(event, event_type=TradingEventType.RETRY_C_WAIT_SETUP)
    if state.case_c_signal_state is CaseCSignalState.C_SETUP:
        return replace(
            event,
            event_type=TradingEventType.RETRY_CASE_C_SETUP_CONDITION_CHECK,
        )

    return event


def _resolve_case_b_region_event(
    state: TradingStateConfiguration,
    event: TradingEvent,
    context: TradingContextView,
) -> TradingEvent:
    """
    함수 이름: _resolve_case_b_region_event()
    기능: 시장 데이터 갱신 이벤트를 Case B 신호 Region의 조건 검사 이벤트로 변환한다.
    인자: state -> 이벤트 변환 기준이 되는 현재 상태 구성
        event -> 원본 정규화 이벤트
        context -> 확정 봉 여부를 포함한 거래 컨텍스트
    반환값: Case B 신호 상태에 맞게 변환했거나 그대로 유지한 이벤트
    작성 날짜: 2026/08/14
    """
    if event.event_type is not TradingEventType.MARKET_DATA_UPDATED:
        return event

    # B_WAIT_SIGNAL은 확정된 30분 봉에서만 신호 평가를 시작한다.
    if (
        state.case_b_signal_state is CaseBSignalState.B_WAIT_SIGNAL
        and context.market.confirmed_30m_close
    ):
        return replace(
            event,
            event_type=TradingEventType.THIRTY_MINUTE_CANDLE_CLOSED,
        )

    # 눌림목 대기 상태는 매 시장 갱신에서 조건을 다시 평가한다.
    if state.case_b_signal_state is CaseBSignalState.B_WAIT_PULLBACK:
        return replace(
            event,
            event_type=TradingEventType.RETRY_CASE_B_WAIT_PULLBACK_CONDITION_CHECK,
        )

    return event


def _create_decision_id(
    event: TradingEvent,
    context: TradingContextView,
    transition_ids: tuple[str, ...],
) -> str:
    """
    함수 이름: _create_decision_id()
    기능: 이벤트, 컨텍스트 버전, 전이 목록으로 결정론적 판단 식별자를 만든다.
    인자: event -> 이벤트 식별자 또는 큐 순번을 제공하는 처리 이벤트
        context -> 버전 정보를 제공하는 거래 컨텍스트
        transition_ids -> 이번 마이크로스텝에서 실행된 전이 식별자 목록
    반환값: 추적과 멱등성 상관관계에 사용할 문자열 식별자
    작성 날짜: 2026/08/14
    """
    # 명시적 이벤트 ID가 없을 때도 큐 순번으로 안정적인 추적 키를 만든다.
    event_identity = event.event_id or f"seq-{event.sequence_number}"
    transition_part = "+".join(transition_ids) if transition_ids else "NOOP"

    return f"{event_identity}:v{context.version}:{transition_part}"
