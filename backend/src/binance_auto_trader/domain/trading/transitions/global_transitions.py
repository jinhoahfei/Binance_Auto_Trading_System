"""최상위 상태와 공통 G-01~G-07 transition을 구현한다."""

from __future__ import annotations

from ..action_requests import (
    CancelPendingOrder,
    CancelScheduledEvaluation,
    CloseLowerEvent,
    ForceSellAll,
    ReconcileOrder,
    ResetCaseBContext,
    ResetCaseCContext,
    QueueEvent,
    StopTradingRuntime,
    patch,
)
# 표시와 주문 판단이 같은 순수 조건 평가를 공유한다.
from ..conditions import condition_met
from ..context import TradingContextView
from ..events import ForceSellOutcomePayload, TradingEvent, TradingEventType
from ..states import OrderSide, RootState, TradingPhase, TradingStateConfiguration
from .base import TransitionOutcome, create_transition_outcome
from .helpers import (
    create_lower_event_initialization_patch,
    resolve_lower_event_id,
    is_lower_touch_condition_met,
    create_open_lower_event_action,
)


def _create_inactive_configuration(
    root_state: RootState,
) -> TradingStateConfiguration:
    """
    함수 이름: _create_inactive_configuration()
    기능: 모든 병렬 Region이 비활성인 최상위 상태 구성을 생성한다.
    인자: root_state -> 적용할 최상위 상태
    반환값: Region이 비활성인 TradingStateConfiguration
    작성 날짜: 2026/08/14
    """
    return TradingStateConfiguration(root_state=root_state)


def _handle_upper_band_touch(
    state: TradingStateConfiguration,
    context: TradingContextView,
    *,
    is_market_observation: bool,
) -> TransitionOutcome | None:
    """
    함수 이름: _handle_upper_band_touch()
    기능: 상단 BB에서 노출이 없으면 하단 감시로 복귀하고 주문·포지션 관리는 보존한다.
    인자: state -> 현재 상태 구성, context -> 주문 의도와 실제 포지션을 포함한 컨텍스트
        is_market_observation -> 기존 Case 평가를 계속해야 하는 실제 시장 관측 여부
    반환값: G-07 결과 또는 기존 Case 평가를 계속할 때 None
    작성 날짜: 2026/09/17
    """
    runtime = context.runtime
    # 거래소 주문 ID 발급 전의 준비·재시도 의도도 초기화하지 않는다.
    if (runtime.pending_order_id is not None
            or runtime.pending_intent_id is not None
            or context.position.is_open):
        # 실제 시장 관측은 보유·주문 관리를 계속하고, 명시적 상단 event는 기존 no-op을 유지한다.
        if is_market_observation:
            return None
        return create_transition_outcome("G-07", state, exclusive=True)

    # 세션은 유지하고 이전 하단 이벤트의 신호·타이머만 정리한다.
    return create_transition_outcome(
        "G-07",
        _create_inactive_configuration(RootState.LOWER_TOUCH_WATCH),
        CloseLowerEvent(reason="UPPER_BAND_RETURN_TO_LOWER_WATCH"),
        ResetCaseBContext(),
        ResetCaseCContext(),
        patch(
            position_owner=None,
            pending_strategy=None,
            pending_order_side=None,
            pending_order_id=None,
            pending_order_attempt_kind=None,
            pending_intent_id=None,
            pending_exit_reason=None,
            pending_exit_pct_b=None,
            pending_return_state=None,
            trading_phase=TradingPhase.IDLE,
        ),
        CancelScheduledEvaluation(scope="lower-event"),
        exclusive=True,
    )


def _handle_band_touch(
    state: TradingStateConfiguration,
    event: TradingEvent,
    context: TradingContextView,
) -> TransitionOutcome | None:
    """
    함수 이름: _handle_band_touch()
    기능: 시장 관측과 명시적 접촉 event의 G-02·G-03·G-07 조건 및 액션을 한 곳에서 결정한다.
    인자: state -> 현재 상태, event -> 처리할 event, context -> 같은 평가 시점의 불변 컨텍스트
    반환값: 접촉 전이 또는 기존 Case 평가를 계속할 때 None
    작성 날짜: 2026/09/17
    """
    event_type = event.event_type
    runtime = context.runtime
    market = context.market
    # 스냅샷 없는 내부 재평가는 이전 시장 값으로 새 접촉을 만들지 않는다.
    is_market_observation = (
        event_type is TradingEventType.MARKET_DATA_UPDATED
        and event.market_evaluation is not None
    )

    # 상단 우선순위와 주문·포지션 보호를 판단하는 유일한 경로다.
    if (
        (event_type is TradingEventType.UPPER_BAND_TOUCHED
         or (is_market_observation and market.upper_band > 0))
        and condition_met("upper_safe_exit", context)
    ):
        if state.root_state is RootState.TRADE_MANAGEMENT:
            return _handle_upper_band_touch(
                state, context, is_market_observation=is_market_observation,
            )
        return None

    if not is_market_observation and event_type not in (
        TradingEventType.LOWER_BAND_TOUCHED,
        TradingEventType.NEW_30M_LOWER_BAND_TOUCHED,
    ):
        return None
    if is_market_observation and market.lower_band <= 0:
        return None
    if not is_lower_touch_condition_met(context):
        return None

    # 첫 하단 접촉에서는 세 Region initial transition을 같은 microstep에 완료한다.
    if (
        state.root_state is RootState.LOWER_TOUCH_WATCH
        and (event_type is TradingEventType.LOWER_BAND_TOUCHED
             or (is_market_observation and runtime.lower_event_id is None))
    ):
        new_lower_event_id = resolve_lower_event_id(event, context)
        return create_transition_outcome(
            "G-02",
            TradingStateConfiguration.create_trade_management_initial_state(),
            create_open_lower_event_action(event, context),
            create_lower_event_initialization_patch(context),
            patch(position_owner=None),
            QueueEvent(
                event_type=TradingEventType.ACTIVATE_TRADE_MANAGEMENT,
                lower_event_id=new_lower_event_id,
            ),
            extra_transition_ids=("O-01", "C-01", "B-01"),
            exclusive=True,
        )

    # 새 봉 재접촉을 열 수 없어도 시장 관측은 하위 Region의 기존 조건 평가로 이어진다.
    if (
        state.root_state is RootState.TRADE_MANAGEMENT
        and (event_type is TradingEventType.NEW_30M_LOWER_BAND_TOUCHED
             or (is_market_observation
                 and runtime.lower_event_id is not None
                 and market.current_30m_candle_id is not None))
        and runtime.position_owner is None
        and runtime.pending_order_id is None
        and market.current_30m_candle_id != runtime.touch_candle_id
        and (not runtime.case_c_consumed_for_event or runtime.case_c_recovery_confirmed)
    ):
        new_lower_event_id = resolve_lower_event_id(event, context)
        return create_transition_outcome(
            "G-03",
            TradingStateConfiguration.create_trade_management_initial_state(),
            CloseLowerEvent(reason="NEW_30M_LOWER_TOUCH"),
            create_open_lower_event_action(event, context),
            create_lower_event_initialization_patch(context),
            patch(position_owner=None),
            QueueEvent(
                event_type=TradingEventType.ACTIVATE_TRADE_MANAGEMENT,
                lower_event_id=new_lower_event_id,
            ),
            extra_transition_ids=("O-01", "C-01", "B-01"),
            exclusive=True,
        )
    return None


def handle_global_transition(
    state: TradingStateConfiguration,
    event: TradingEvent,
    context: TradingContextView,
) -> TransitionOutcome | None:
    """
    함수 이름: handle_global_transition()
    기능: 현재 event에서 가장 우선하는 최상위 또는 공통 transition을 선택한다.
    인자: state -> 현재 전체 상태 구성
        event -> 처리할 TradingEvent
        context -> 같은 평가 시점의 TradingContextView
    반환값: 선택된 TransitionOutcome 또는 적용 가능한 transition이 없으면 None
    작성 날짜: 2026/08/14
    """
    event_type = event.event_type
    runtime = context.runtime

    # STOP은 신규 전략 action보다 우선하며 pending 주문 유무에 따라 경로를 분리한다.
    if event_type is TradingEventType.STOP_CONFIRMED:
        if state.root_state in (RootState.LOGIC_TERMINATED, RootState.STOPPING):
            return None

        # pending 주문은 새 주문을 만들기 전에 취소·재조회·reconciliation한다.
        if runtime.pending_order_id is not None:
            actions = [
                patch(trading_phase=TradingPhase.STOPPING),
            ]
            if runtime.pending_order_side is OrderSide.BUY:
                actions.append(
                    CancelPendingOrder(
                        order_id=runtime.pending_order_id,
                        reason="STOP_CONFIRMED",
                    )
                )
            actions.append(
                ReconcileOrder(
                    order_id=runtime.pending_order_id,
                    stop_after_reconciliation=True,
                )
            )
            return create_transition_outcome(
                "G-06P",
                _create_inactive_configuration(RootState.STOPPING),
                *actions,
                exclusive=True,
            )

        # authoritative Position 수량이 양수인 경우에만 전량 매도를 요청한다.
        if context.position.is_open:
            return create_transition_outcome(
                "G-06",
                _create_inactive_configuration(RootState.STOPPING),
                patch(trading_phase=TradingPhase.STOPPING),
                CancelScheduledEvaluation(scope="trading-strategy"),
                ForceSellAll(),
                exclusive=True,
            )

        # 포지션과 pending 주문이 모두 없으면 즉시 runtime을 종료한다.
        return create_transition_outcome(
            "G-05",
            _create_inactive_configuration(RootState.LOGIC_TERMINATED),
            CloseLowerEvent(reason="STOP_WITHOUT_POSITION"),
            ResetCaseBContext(),
            ResetCaseCContext(),
            patch(
                position_owner=None,
                pending_strategy=None,
                pending_order_side=None,
                pending_order_id=None,
                pending_order_attempt_kind=None,
                pending_intent_id=None,
                trading_phase=TradingPhase.TERMINATED,
            ),
            CancelScheduledEvaluation(scope="trading-session"),
            StopTradingRuntime(reason="STOP_WITHOUT_POSITION"),
            exclusive=True,
        )

    # 강제 매도 체결과 저장이 끝난 뒤에만 STOPPING에서 최종 상태로 전이한다.
    if (
        state.root_state is RootState.STOPPING
        and event_type is TradingEventType.FORCE_SELL_FINISHED
        and not context.position.is_open
        and runtime.pending_order_id is None
        and _is_force_sell_completion_valid(event)
    ):
        return create_transition_outcome(
            "G-06F",
            _create_inactive_configuration(RootState.LOGIC_TERMINATED),
            CloseLowerEvent(reason="FORCE_SELL_FINISHED"),
            ResetCaseBContext(),
            ResetCaseCContext(),
            patch(
                position_owner=None,
                pending_strategy=None,
                pending_order_side=None,
                pending_order_id=None,
                pending_order_attempt_kind=None,
                pending_intent_id=None,
                pending_exit_reason=None,
                pending_exit_pct_b=None,
                pending_return_state=None,
                trading_phase=TradingPhase.TERMINATED,
            ),
            CancelScheduledEvaluation(scope="trading-session"),
            StopTradingRuntime(reason="FORCE_SELL_FINISHED"),
            exclusive=True,
        )

    # terminal 미체결은 같은 포지션의 retry 정책을 사용하며 신규 전략은 재개하지 않는다.
    if (
        state.root_state is RootState.STOPPING
        and event_type is TradingEventType.FORCE_SELL_FAILED
        and context.position.is_open
        and runtime.pending_order_id is None
        and _is_force_sell_failure_terminal(event)
    ):
        return create_transition_outcome(
            "G-06R",
            state,
            ForceSellAll(retry=True, use_retry_policy=True),
            exclusive=True,
        )

    # Controller가 준비한 시장 사실을 현재 상태와 함께 해석한다.
    band_outcome = _handle_band_touch(state, event, context)
    if band_outcome is not None:
        return band_outcome

    # 세션 시작은 Context 초기화 action과 LOWER_TOUCH_WATCH 진입만 수행한다.
    if (
        state.root_state is RootState.NOT_STARTED
        and event_type is TradingEventType.LOGIC_STARTED
    ):
        return create_transition_outcome(
            "G-01",
            _create_inactive_configuration(RootState.LOWER_TOUCH_WATCH),
            ResetCaseBContext(),
            ResetCaseCContext(),
            patch(
                position_owner=None,
                pending_strategy=None,
                pending_order_side=None,
                pending_order_id=None,
                pending_order_attempt_kind=None,
                pending_intent_id=None,
                pending_exit_reason=None,
                pending_exit_pct_b=None,
                pending_return_state=None,
                trading_phase=TradingPhase.IDLE,
            ),
            exclusive=True,
        )

    # 두 signal Region이 Final이고 포지션이 없으면 lower 감시 상태로 복귀한다.
    if (
        state.root_state is RootState.TRADE_MANAGEMENT
        and event_type is TradingEventType.TRADE_MANAGEMENT_COMPLETED
        and runtime.position_owner is None
        and state.trade_management_is_complete
    ):
        return create_transition_outcome(
            "G-04",
            _create_inactive_configuration(RootState.LOWER_TOUCH_WATCH),
            CloseLowerEvent(reason="TRADE_MANAGEMENT_COMPLETED"),
            CancelScheduledEvaluation(scope="lower-event"),
            exclusive=True,
        )

    return None


def _is_force_sell_completion_valid(event: TradingEvent) -> bool:
    """
    함수 이름: _is_force_sell_completion_valid()
    기능: 강제 매도 완료 event의 체결 반영 및 이력 저장 증거를 검증한다.
    인자: event -> FORCE_SELL_FINISHED event
    반환값: 종료 처리가 가능한 완료 event 여부
    작성 날짜: 2026/08/14
    """
    # Typed payload를 추출하고 체결 반영·이력 저장 증거가 모두 참인지 판정한다.
    payload = event.payload
    return (
        isinstance(payload, ForceSellOutcomePayload)
        and payload.execution_applied
        and payload.history_persisted
    )


def _is_force_sell_failure_terminal(event: TradingEvent) -> bool:
    """
    함수 이름: _is_force_sell_failure_terminal()
    기능: 강제 매도 실패가 retry 가능한 terminal 미체결인지 판정한다.
    인자: event -> FORCE_SELL_FAILED event
    반환값: terminal 미체결 확정 여부
    작성 날짜: 2026/08/14
    """
    # Typed payload를 추출하고 retry 가능한 terminal 미체결 증거를 판정한다.
    payload = event.payload
    return isinstance(payload, ForceSellOutcomePayload) and payload.terminal_unfilled
