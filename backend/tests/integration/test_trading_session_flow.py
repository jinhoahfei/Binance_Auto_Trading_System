"""Phase 7 TradingContext 선택과 start·stop session lifecycle을 통합 검증한다."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta
from decimal import Decimal
from threading import Barrier, Thread
import unittest
from unittest.mock import PropertyMock, patch

from binance_auto_trader.adapters.binance.api_gateway import APIGateway
from binance_auto_trader.adapters.binance.websocket_gateway import (
    WebSocketGateway,
)
from binance_auto_trader.application.regime_controller import RegimeController
from binance_auto_trader.application.trading_controller import (
    ReconciliationCauseCategory,
    ReconciliationCauseStatus,
    TradingController,
    TradingSessionError,
    TradingSessionFailureCode,
    TradingSessionStatus,
)
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.market import MarketSnapshot
from binance_auto_trader.domain.regime import RegimeSTM
from binance_auto_trader.domain.trading.account import Account
from binance_auto_trader.domain.trading.action_requests import (
    CancelPendingOrder,
    CloseLowerEvent,
    ForceSellAll,
    PatchRuntimeContext,
    ReconcileOrder,
    ReevaluationTrigger,
    ResetCaseBContext,
    ResetCaseCContext,
    ScheduleReevaluation,
    patch as runtime_patch,
)
from binance_auto_trader.domain.trading.context import (
    MarketEvaluationSnapshot,
    PendingOrderSnapshot,
    PositionSnapshot,
    TradingContext,
)
from binance_auto_trader.domain.trading.events import (
    EventPriority,
    ForceSellOutcomePayload,
    TradingEvent,
    TradingEventType,
)
from binance_auto_trader.domain.trading.logic_registry import (
    TradingLogicSupportStatus,
)
from binance_auto_trader.domain.trading.order import OrderResult, OrderStatus
from binance_auto_trader.domain.trading.results import TradingSTMResult
from binance_auto_trader.domain.trading.states import (
    OrderAttemptKind,
    OrderSide,
    RootState,
    StrategyType,
    TradingPhase,
)

from tests.integration.test_account_stream_flow import (
    FakeAccountRESTClient,
    FakeSubscription,
    MARKET_UPDATED_AT,
    SynchronousAccountWebSocketClient,
    _ready_market_snapshot,
)


class RecordingTradingContext(TradingContext):
    """
    클래스 이름: RecordingTradingContext
    기능: Controller가 Context Action을 적용한 실제 순서를 통합 테스트에 기록한다.
    작성 날짜: 2026/08/21
    """

    __slots__ = ("mutation_order",)

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 고정 시각 Context와 빈 mutation 순서 기록을 만든다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 고정 시각 production Context를 초기화하고 mutation 순서 기록을 비운다.
        super().__init__(clock=lambda: MARKET_UPDATED_AT)
        self.mutation_order: list[str] = []

    def apply_trading_stm_result(self, result: TradingSTMResult) -> None:
        """
        함수 이름: apply_trading_stm_result()
        기능: runtime patch 위치를 기록한 뒤 production Context 계약을 실행한다.
        인자: result -> patch 하나를 포함한 STM 결과
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # runtime patch 위치를 기록한 뒤 production Context에 적용한다.
        self.mutation_order.append("patch")
        super().apply_trading_stm_result(result)

    def close_lower_event(self, action: CloseLowerEvent) -> None:
        """
        함수 이름: close_lower_event()
        기능: lower-event close 위치를 기록한 뒤 production Context 계약을 실행한다.
        인자: action -> lower-event close 요청
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # lower-event close 위치를 기록한 뒤 production Context에 적용한다.
        self.mutation_order.append("close_lower_event")
        super().close_lower_event(action)

    def reset_case_b_context(self, action: ResetCaseBContext) -> None:
        """
        함수 이름: reset_case_b_context()
        기능: Case B reset 위치를 기록한 뒤 production Context 계약을 실행한다.
        인자: action -> Case B reset 요청
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # Case B reset 위치를 기록한 뒤 production Context에 적용한다.
        self.mutation_order.append("reset_case_b")
        super().reset_case_b_context(action)

    def reset_case_c_context(self, action: ResetCaseCContext) -> None:
        """
        함수 이름: reset_case_c_context()
        기능: Case C reset 위치를 기록한 뒤 production Context 계약을 실행한다.
        인자: action -> Case C reset 요청
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # Case C reset 위치를 기록한 뒤 production Context에 적용한다.
        self.mutation_order.append("reset_case_c")
        super().reset_case_c_context(action)


class FailingSessionSubscription:
    """
    클래스 이름: FailingSessionSubscription
    기능: close 시도 뒤 실패하는 세션 구독 test double을 제공한다.
    작성 날짜: 2026/08/21
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 아직 close를 시도하지 않은 구독을 만든다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.close_attempted = False

    def close(self) -> None:
        """
        함수 이름: close()
        기능: close 시도를 기록하고 진단용 실패를 발생시킨다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # close 시도를 먼저 기록한 뒤 의도한 cleanup 실패를 발생시킨다.
        self.close_attempted = True
        raise RuntimeError("session subscription close failed")


class ReentrantSessionSubscription:
    """
    클래스 이름: ReentrantSessionSubscription
    기능: close callback에서 Controller 등록을 재시도하는 악의적 세션 구독을 모의한다.
    작성 날짜: 2026/08/21
    """

    def __init__(self, close_callback: object) -> None:
        """
        함수 이름: __init__()
        기능: close 중 동기 호출할 callback과 호출 횟수를 보존한다.
        인자: close_callback -> close 시 호출할 무인자 callable
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # close callback 계약을 검증하고 재진입 호출 상태를 초기화한다.
        if not callable(close_callback):
            raise TypeError("close_callback must be callable")
        self._close_callback = close_callback
        self.close_count = 0

    def close(self) -> None:
        """
        함수 이름: close()
        기능: close 횟수를 기록하고 주입 callback으로 Controller에 재진입한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # close 횟수를 기록한 뒤 같은 call stack에서 Controller에 재진입한다.
        self.close_count += 1
        self._close_callback()


def _create_ready_controller(
    *,
    command_enabled: bool = True,
    context: TradingContext | None = None,
    market_stream_recovery_enabled: bool = False,
) -> tuple[
    TradingController,
    RegimeController,
    SynchronousAccountWebSocketClient,
]:
    """
    함수 이름: _create_ready_controller()
    기능: ready market·account·stream과 선택 Controller를 한 통합 fixture로 만든다.
    인자: command_enabled -> fake mode command gate 허용 여부
        context -> 주입할 mutable TradingContext 또는 None
        market_stream_recovery_enabled -> Kline live full-resync gate 활성 여부
    반환값: TradingController, RegimeController와 제어 가능한 WS client tuple
    작성 날짜: 2026/08/21
    """
    # 동일 Account를 공유하는 동기식 account stream gateway를 준비한다.
    account = Account()
    web_socket_client = SynchronousAccountWebSocketClient([])
    web_socket_gateway = WebSocketGateway(
        web_socket_client,
        account_snapshot_callback=account.apply_stream_snapshot,
    )
    # ready market과 command gate를 주입해 TradingController를 조립한다.
    controller = TradingController(
        APIGateway(FakeAccountRESTClient([])),
        web_socket_gateway,
        account,
        _ready_market_snapshot(),
        command_gate=command_enabled,
        context=context,
        market_stream_recovery_enabled=market_stream_recovery_enabled,
        clock=lambda: MARKET_UPDATED_AT,
    )
    # account bootstrap을 완료한 뒤 같은 market과 Controller를 RegimeController에 연결한다.
    controller.load_account()
    regime_controller = RegimeController(
        RegimeSTM(),
        controller._market_snapshot,
        controller,
    )
    return controller, regime_controller, web_socket_client


class TradingSessionSelectionAndStartTests(unittest.TestCase):
    """
    클래스 이름: TradingSessionSelectionAndStartTests
    기능: REGIME 선택, start Guard, 멱등성과 active 변경 금지를 검증한다.
    작성 날짜: 2026/08/21
    """

    def test_market_observation_updates_context_and_opens_lower_event(
        self,
    ) -> None:
        """
        함수 이름: test_market_observation_updates_context_and_opens_lower_event()
        기능: same-version 외부 시장 평가가 private Action 없이 G-02 production path를 시작하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # 지원 REGIME을 선택·시작해 public market observation이 허용되는 session을 준비한다.
        controller, regime_controller, _ = _create_ready_controller()
        selection = regime_controller.set_regime_type(
            RegimeType.TYPE_0,
            command_id="select-market-observation",
            expected_version=0,
        )
        controller.start_trading(
            command_id="start-market-observation",
            expected_version=selection.version,
        )
        market = MarketEvaluationSnapshot(
            realtime_price=Decimal("90"),
            lower_band=Decimal("100"),
            upper_band=Decimal("120"),
            realtime_pct_b=Decimal("-0.5"),
            current_30m_candle_id="ETHUSDT:30m:2026-08-24T00:00:00Z",
            current_30m_low=Decimal("89"),
            current_30m_high=Decimal("101"),
            touch_candle_bbw=Decimal("0.01"),
        )

        # MarketSnapshot provenance와 stable source ID를 전달해 최초 lower touch를 enqueue한다.
        enqueued = controller.observe_market_evaluation(
            market,
            source_event_id="ws-kline-30m-1724457600000",
            market_version=controller._market_snapshot.version,
        )
        duplicate = controller.observe_market_evaluation(
            market,
            source_event_id="ws-kline-30m-1724457600000",
            market_version=controller._market_snapshot.version,
        )

        # 같은 source 재전달은 dedup되고 첫 event만 G-02를 거쳐 lower scope를 연다.
        self.assertIsNotNone(enqueued)
        self.assertIs(enqueued.event_type, TradingEventType.LOWER_BAND_TOUCHED)
        self.assertEqual(market, enqueued.market_evaluation)
        self.assertNotEqual(
            market,
            controller.context.market,
        )  # Queue claim 전에는 뒤 Kline이 첫 event의 Context를 덮지 못하도록 mutation을 보류한다.
        self.assertIsNone(duplicate)
        result = asyncio.run(controller.process_next_event())
        self.assertEqual(("G-02", "O-01", "C-01", "B-01"), result.transition_ids)
        self.assertEqual(market, controller.context.market)
        self.assertIsNotNone(controller.context.runtime.lower_event_id)

    def test_back_to_back_market_observations_preserve_queue_provenance(
        self,
    ) -> None:
        """
        함수 이름: test_back_to_back_market_observations_preserve_queue_provenance()
        기능: 처리 전 연속 시장 평가가 각 source version의 Context와 분류를 보존하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # RUNNING session과 실제 처리 event를 별도로 기록할 observer를 준비한다.
        controller, regime_controller, _ = _create_ready_controller()
        selection = regime_controller.set_regime_type(
            RegimeType.TYPE_0,
            command_id="select-market-queue-provenance",
            expected_version=0,
        )
        controller.start_trading(
            command_id="start-market-queue-provenance",
            expected_version=selection.version,
        )
        event_processor = controller._event_processor
        self.assertIsNotNone(event_processor)
        if event_processor is None:
            raise AssertionError("running session requires an event processor")
        original_observer = event_processor._event_processing_observer
        processed_events: list[TradingEvent] = []

        def record_processing_event(event: TradingEvent | None) -> None:
            """
            함수 이름: record_processing_event()
            기능: processor가 실제로 준비한 event를 기록하고 production trace observer를 유지한다.
            인자: event -> 처리 중인 TradingEvent 또는 microstep 종료 None
            반환값: 없음
            작성 날짜: 2026/08/29
            """
            # 준비된 event만 회귀 검증용으로 보존하고 기존 trace lifecycle을 그대로 호출한다.
            if event is not None:
                processed_events.append(event)
            if original_observer is not None:
                original_observer(event)

        event_processor._event_processing_observer = record_processing_event

        # 첫 version은 밴드 내부, 둘째 version은 하단 접촉으로 서로 다른 분류를 만든다.
        first_market = MarketEvaluationSnapshot(
            realtime_price=Decimal("110"),
            lower_band=Decimal("100"),
            upper_band=Decimal("120"),
            realtime_pct_b=Decimal("0.5"),
            current_30m_candle_id="ETHUSDT:30m:2026-08-29T00:00:00Z",
            current_30m_low=Decimal("105"),
            current_30m_high=Decimal("115"),
        )
        second_market = MarketEvaluationSnapshot(
            realtime_price=Decimal("90"),
            lower_band=Decimal("100"),
            upper_band=Decimal("120"),
            realtime_pct_b=Decimal("-0.5"),
            current_30m_candle_id="ETHUSDT:30m:2026-08-29T00:00:00Z",
            current_30m_low=Decimal("89"),
            current_30m_high=Decimal("115"),
            touch_candle_bbw=Decimal("0.01"),
        )

        # Authoritative snapshot을 한 version씩 전진하며 두 평가를 처리 전에 FIFO queue에 적재한다.
        controller._market_snapshot.update(
            controller._market_snapshot.klines_by_interval
        )
        first_market_version = controller._market_snapshot.version
        first_enqueued = controller.observe_market_evaluation(
            first_market,
            source_event_id="ws-kline-first-market-evaluation",
            market_version=first_market_version,
        )
        controller._market_snapshot.update(
            controller._market_snapshot.klines_by_interval
        )
        second_market_version = controller._market_snapshot.version
        second_enqueued = controller.observe_market_evaluation(
            second_market,
            source_event_id="ws-kline-second-market-evaluation",
            market_version=second_market_version,
        )

        # Queue claim 전 Context는 두 평가 중 어느 것으로도 앞서 갱신되지 않아야 한다.
        self.assertIsNotNone(first_enqueued)
        self.assertIsNotNone(second_enqueued)
        self.assertEqual(first_market_version + 1, second_market_version)
        self.assertNotEqual(first_market, controller.context.market)
        self.assertNotEqual(second_market, controller.context.market)

        # 첫 process는 뒤 version이 아닌 첫 평가로 Context와 MARKET_DATA_UPDATED를 준비한다.
        first_result = asyncio.run(controller.process_next_event())
        self.assertIsNotNone(first_result)
        self.assertEqual((), first_result.transition_ids)
        self.assertEqual(first_market, controller.context.market)
        self.assertEqual(1, len(processed_events))
        self.assertIs(
            processed_events[0].event_type,
            TradingEventType.MARKET_DATA_UPDATED,
        )
        self.assertEqual(first_market, processed_events[0].market_evaluation)
        self.assertEqual(
            first_market_version,
            processed_events[0].market_version,
        )

        # 둘째 process는 둘째 평가로 Context를 전진하고 하단 접촉을 실제 분류한다.
        second_result = asyncio.run(controller.process_next_event())
        self.assertIsNotNone(second_result)
        self.assertEqual(second_market, controller.context.market)
        self.assertEqual(2, len(processed_events))
        self.assertIs(
            processed_events[1].event_type,
            TradingEventType.LOWER_BAND_TOUCHED,
        )
        self.assertEqual(second_market, processed_events[1].market_evaluation)
        self.assertEqual(
            second_market_version,
            processed_events[1].market_version,
        )
        self.assertEqual(
            ("G-02", "O-01", "C-01", "B-01"),
            second_result.transition_ids,
        )
        self.assertIsNone(
            controller.context.pending_order
        )  # 공개 시장 분류 검증은 어떤 주문 제출도 생성하지 않는다.

    def test_market_resync_does_not_auto_resume_after_pending_order_finishes(
        self,
    ) -> None:
        """
        함수 이름: test_market_resync_does_not_auto_resume_after_pending_order_finishes()
        기능: 시장 full-resync 뒤 same-order terminal IDLE patch가 중단 session을 재개하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        # Market gate를 활성화한 RUNNING session에 미해결 BUY identity를 먼저 보존한다.
        controller, regime_controller, _ = _create_ready_controller(
            market_stream_recovery_enabled=True,
        )
        selection = regime_controller.set_regime_type(
            RegimeType.TYPE_0,
            command_id="select-market-disconnect",
            expected_version=0,
        )
        controller.start_trading(
            command_id="start-market-disconnect",
            expected_version=selection.version,
        )
        pending_order = PendingOrderSnapshot(
            order_id="pending-market-disconnect",
            strategy=StrategyType.CASE_B,
            side=OrderSide.BUY,
            attempt_kind=OrderAttemptKind.INITIAL,
        )
        controller.update_pending_order_snapshot(pending_order)
        controller.mark_market_stream_reconciliation_required(
            "kline_stream_disconnected"
        )

        # 새 시장 세대가 준비돼도 market blocker만 해제하고 session과 pending provenance는 유지한다.
        with patch.object(
            WebSocketGateway,
            "kline_live_ready",
            new_callable=PropertyMock,
            return_value=True,
        ):
            controller.complete_market_stream_reconciliation(
                controller._market_snapshot.version
            )

            # 같은 주문의 terminal 처리와 동일하게 pending을 비우고 phase를 IDLE로 바꾼다.
            controller._context.update_pending_order(None)
            controller._context.apply_runtime_patch(
                runtime_patch(trading_phase=TradingPhase.IDLE)
            )
            active_stm = controller._active_stm
            self.assertIsNotNone(active_stm)
            controller._synchronize_status_from_context(active_stm)

            # 시장 source는 복구됐어도 중단된 session provenance는 operator 재조정 전까지 남는다.
            self.assertFalse(
                controller.market_stream_reconciliation_required
            )
            self.assertFalse(controller.command_enabled)
            self.assertTrue(controller.reconciliation_required)

        # RUNNING 중의 중단은 시장 source가 복구되더라도 operator가 확인할 exact cause를 유지한다.
        cause_snapshot = controller.reconciliation_cause_snapshot
        self.assertTrue(cause_snapshot.reconciliation_required)
        self.assertIs(
            cause_snapshot.status,
            ReconciliationCauseStatus.EXACT,
        )
        self.assertIs(
            cause_snapshot.category,
            ReconciliationCauseCategory.MARKET_STREAM_FAILED,
        )
        self.assertIs(
            controller.status,
            TradingSessionStatus.RECONCILIATION_REQUIRED,
        )
        self.assertIsNone(controller.context.pending_order)

    def test_market_observation_rejects_stale_market_version(
        self,
    ) -> None:
        """
        함수 이름: test_market_observation_rejects_stale_market_version()
        기능: MarketSnapshot과 다른 version의 평가가 Context나 queue를 변경하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # RUNNING session과 변경 전 Context version을 준비한다.
        controller, regime_controller, _ = _create_ready_controller()
        selection = regime_controller.set_regime_type(
            RegimeType.TYPE_0,
            command_id="select-stale-market",
            expected_version=0,
        )
        controller.start_trading(
            command_id="start-stale-market",
            expected_version=selection.version,
        )
        context_version_before = controller.context.version

        # 현재보다 앞선 임의 version도 same-version provenance 위반으로 fail closed한다.
        with self.assertRaises(ValueError):
            controller.observe_market_evaluation(
                MarketEvaluationSnapshot(
                    realtime_price=Decimal("110"),
                    lower_band=Decimal("100"),
                    upper_band=Decimal("120"),
                ),
                source_event_id="stale-market-evaluation",
                market_version=controller._market_snapshot.version + 1,
            )

        self.assertEqual(context_version_before, controller.context.version)
        self.assertEqual(0, len(controller._event_queue))

    def test_market_observation_rejects_non_positive_price_without_fallback(
        self,
    ) -> None:
        """
        함수 이름: test_market_observation_rejects_non_positive_price_without_fallback()
        기능: 잘못된 public 평가 가격이 queue나 4시간봉 fallback 주문 경계에 도달하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # RUNNING session과 변경 전 Context를 준비해 public 입력 거부의 원자성을 관찰한다.
        controller, regime_controller, _ = _create_ready_controller()
        selection = regime_controller.set_regime_type(
            RegimeType.TYPE_0,
            command_id="select-invalid-public-price",
            expected_version=0,
        )
        controller.start_trading(
            command_id="start-invalid-public-price",
            expected_version=selection.version,
        )
        context_before = controller.context

        # Public evaluation의 0 가격은 ready 4시간봉 가격으로 대체하지 않고 입구에서 fail closed한다.
        with self.assertRaisesRegex(
            ValueError,
            "market realtime_price must be a positive finite Decimal",
        ):
            controller.observe_market_evaluation(
                MarketEvaluationSnapshot(
                    realtime_price=Decimal("0"),
                    lower_band=Decimal("90"),
                    upper_band=Decimal("110"),
                    current_30m_candle_id=(
                        "ETHUSDT:30m:2026-08-29T00:00:00Z"
                    ),
                ),
                source_event_id="invalid-public-market-price",
                market_version=controller._market_snapshot.version,
            )

        self.assertEqual(context_before, controller.context)
        self.assertEqual(0, len(controller._event_queue))
        self.assertEqual(0, controller._latest_market_evaluation_version)

    def test_supported_selection_starts_exactly_once_and_rejects_active_swap(
        self,
    ) -> None:
        """
        함수 이름: test_supported_selection_starts_exactly_once_and_rejects_active_swap()
        기능: TYPE_0 선택·G-01 start·duplicate와 D09 active 변경 금지를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # ready Controller에서 지원되는 TYPE_0을 최초 version으로 선택한다.
        controller, regime_controller, _ = _create_ready_controller()
        selection = regime_controller.set_regime_type(
            RegimeType.TYPE_0,
            command_id="select-type0",
            expected_version=0,
        )

        # 선택은 Context version을 한 번 올리고 지원 registry를 그대로 공개한다.
        self.assertIs(selection.selected, RegimeType.TYPE_0)
        self.assertIs(
            selection.support_status,
            TradingLogicSupportStatus.SUPPORTED,
        )
        self.assertEqual(1, selection.version)
        # 같은 command fingerprint를 재전송해 start 결과의 exact replay를 유도한다.
        first_start = controller.start_trading(
            command_id="start-session",
            expected_version=selection.version,
        )
        duplicate_start = controller.start_trading(
            command_id="start-session",
            expected_version=selection.version,
        )

        # bool은 int와 같게 비교되더라도 cached command의 version 검증을 우회할 수 없다.
        with self.assertRaises(TypeError):
            controller.start_trading(
                command_id="start-session",
                expected_version=True,
            )

        # 최초·중복 start가 동일 G-01 결과와 active STM을 공유하는지 확인한다.
        self.assertIs(first_start, duplicate_start)
        self.assertIs(first_start.status, TradingSessionStatus.RUNNING)
        self.assertEqual(("G-01",), first_start.transition_ids)
        self.assertIs(
            controller._active_stm.current_state.root_state,
            RootState.LOWER_TOUCH_WATCH,
        )

        # active 변경 실패는 RegimeController와 TradingContext 선택을 모두 보존한다.
        version_before = controller.context.version
        with self.assertRaises(TradingSessionError) as raised:
            regime_controller.set_regime_type(
                RegimeType.TYPE_1,
                command_id="active-swap",
                expected_version=version_before,
            )
        self.assertIs(
            raised.exception.code,
            TradingSessionFailureCode.TRADING_ACTIVE,
        )
        self.assertIs(regime_controller.selected_regime, RegimeType.TYPE_0)
        self.assertIs(controller.context.selected_regime, RegimeType.TYPE_0)
        self.assertEqual(version_before, controller.context.version)
        self.assertFalse(
            hasattr(controller.context, "select_regime")
        )  # mutable Context owner API는 Controller 밖으로 노출하지 않는다.

    def test_old_selection_replay_does_not_split_selection_owners(self) -> None:
        """
        함수 이름: test_old_selection_replay_does_not_split_selection_owners()
        기능: 오래된 command 결과 replay가 현재 RegimeController 선택만 되돌리지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 오래된 TYPE_1 결과를 만든 뒤 authoritative 선택을 TYPE_0으로 전진시킨다.
        controller, regime_controller, _ = _create_ready_controller()
        first_result = regime_controller.set_regime_type(
            RegimeType.TYPE_1,
            command_id="select-type1-old",
            expected_version=0,
        )
        regime_controller.set_regime_type(
            RegimeType.TYPE_0,
            command_id="select-type0-current",
            expected_version=first_result.version,
        )

        # application port의 exact replay 결과와 현재 authoritative 선택은 서로 다른 개념이다.
        replayed_result = regime_controller.set_regime_type(
            RegimeType.TYPE_1,
            command_id="select-type1-old",
            expected_version=0,
        )

        self.assertIs(replayed_result.selected, RegimeType.TYPE_1)
        self.assertIs(regime_controller.selected_regime, RegimeType.TYPE_0)
        self.assertIs(controller.selected_regime, RegimeType.TYPE_0)
        self.assertIs(controller.context.selected_regime, RegimeType.TYPE_0)
        self.assertEqual(2, controller.context.version)

    def test_controller_command_record_cache_is_bounded(self) -> None:
        """
        함수 이름: test_controller_command_record_cache_is_bounded()
        기능: unique command ID가 Controller process memory를 무제한 늘리지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        controller, _, _ = _create_ready_controller()

        # transport를 우회하는 application 호출도 고정된 최근 command 수만 보존한다.
        for command_index in range(1_100):
            controller._store_command_record(
                "bounded-test",
                f"command-{command_index}",
                (command_index,),
                command_index,
            )

        self.assertEqual(1_024, len(controller._command_records))
        self.assertNotIn(
            ("bounded-test", "command-0"),
            controller._command_records,
        )
        self.assertIn(
            ("bounded-test", "command-1099"),
            controller._command_records,
        )

    def test_start_failure_rolls_back_context_and_session_ownership(self) -> None:
        """
        함수 이름: test_start_failure_rolls_back_context_and_session_ownership()
        기능: G-01 publish 전 snapshot 실패가 Context·STM·session을 반쪽 초기화하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        clock_call_count = 0

        def fail_first_snapshot_clock() -> datetime:
            """
            함수 이름: fail_first_snapshot_clock()
            기능: 첫 Context snapshot에서만 실패하고 rollback 검사부터 정상 시각을 반환한다.
            인자: 없음
            반환값: timezone-aware test 시각
            작성 날짜: 2026/08/21
            """
            # 첫 snapshot만 실패시키고 rollback 관찰부터 고정 시각을 제공한다.
            nonlocal clock_call_count
            clock_call_count += 1
            if clock_call_count == 1:
                raise RuntimeError("snapshot clock unavailable")
            return MARKET_UPDATED_AT

        # 첫 snapshot에서 실패하는 Context를 주입하고 지원 REGIME까지 선택한다.
        context = TradingContext(clock=fail_first_snapshot_clock)
        controller, regime_controller, _ = _create_ready_controller(
            context=context,
        )
        selection = regime_controller.set_regime_type(
            RegimeType.TYPE_0,
            command_id="select-before-start-failure",
            expected_version=0,
        )

        # start가 snapshot 오류를 전파하도록 실패를 유도한다.
        with self.assertRaisesRegex(RuntimeError, "snapshot clock unavailable"):
            controller.start_trading(
                command_id="start-that-rolls-back",
                expected_version=selection.version,
            )

        # 선택만 보존되고 Context와 session은 start 전 상태로 복구되는지 확인한다.
        rolled_back_context = controller.context
        self.assertFalse(rolled_back_context.initialized)
        self.assertEqual(selection.version, rolled_back_context.version)
        self.assertIs(
            rolled_back_context.selected_regime,
            RegimeType.TYPE_0,
        )
        self.assertIsNone(controller.session_id)
        self.assertIs(controller.status, TradingSessionStatus.NOT_STARTED)

        # rollback된 동일 Controller가 새 command로 정상 재시작 가능한지 확인한다.
        recovered = controller.start_trading(
            command_id="start-after-rollback",
            expected_version=rolled_back_context.version,
        )
        self.assertIs(recovered.status, TradingSessionStatus.RUNNING)

    def test_unselected_unsupported_offline_and_disabled_start_fail_closed(
        self,
    ) -> None:
        """
        함수 이름: test_unselected_unsupported_offline_and_disabled_start_fail_closed()
        기능: 필수 start 선행 조건 네 종류가 Context mutation 없이 거부되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # REGIME 미선택 상태에서는 NO_SELECTED_REGIME으로 start를 거부한다.
        controller, regime_controller, web_socket_client = (
            _create_ready_controller()
        )
        with self.assertRaises(TradingSessionError) as unselected:
            controller.start_trading(
                command_id="unselected",
                expected_version=0,
            )
        self.assertIs(
            unselected.exception.code,
            TradingSessionFailureCode.NO_SELECTED_REGIME,
        )

        # 지원하지 않는 TYPE_1은 UNSUPPORTED_TRADING_LOGIC으로 start를 거부한다.
        unsupported = regime_controller.set_regime_type(
            RegimeType.TYPE_1,
            command_id="unsupported-select",
            expected_version=0,
        )
        with self.assertRaises(TradingSessionError) as unsupported_start:
            controller.start_trading(
                command_id="unsupported-start",
                expected_version=unsupported.version,
            )
        self.assertIs(
            unsupported_start.exception.code,
            TradingSessionFailureCode.UNSUPPORTED_TRADING_LOGIC,
        )

        # TYPE_0 재선택 뒤 disconnect callback이 connection Guard를 닫는다.
        supported = regime_controller.set_regime_type(
            RegimeType.TYPE_0,
            command_id="supported-select",
            expected_version=unsupported.version,
        )
        self.assertIsNotNone(web_socket_client.on_disconnect)
        web_socket_client.on_disconnect()
        with self.assertRaises(TradingSessionError) as offline_start:
            controller.start_trading(
                command_id="offline-start",
                expected_version=supported.version,
            )
        self.assertIs(
            offline_start.exception.code,
            TradingSessionFailureCode.CONNECTION_NOT_READY,
        )

        # command gate가 닫힌 runtime은 다른 조건이 준비돼도 start를 거부한다.
        disabled, disabled_regime, _ = _create_ready_controller(
            command_enabled=False
        )
        disabled_selection = disabled_regime.set_regime_type(
            RegimeType.TYPE_0,
            command_id="disabled-select",
            expected_version=0,
        )
        with self.assertRaises(TradingSessionError) as disabled_start:
            disabled.start_trading(
                command_id="disabled-start",
                expected_version=disabled_selection.version,
            )
        self.assertIs(
            disabled_start.exception.code,
            TradingSessionFailureCode.COMMAND_DISABLED,
        )

    def test_process_lifetime_reconciliation_blocks_direct_start(self) -> None:
        """
        함수 이름: test_process_lifetime_reconciliation_blocks_direct_start()
        기능: event runtime·process ownership·외부 실행 flag 뒤 direct start가 RUNNING을 열지 못함을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/04
        """
        blocker_names = (
            "event-runtime",
            "process-ownership",
            "external-execution",
        )
        for blocker_name in blocker_names:
            with self.subTest(blocker=blocker_name):
                controller, regime_controller, _ = _create_ready_controller()
                selection = regime_controller.set_regime_type(
                    RegimeType.TYPE_0,
                    command_id=f"select-before-{blocker_name}",
                    expected_version=0,
                )
                selected_stm = controller._selected_stm

                # 세 production origin을 각각 시작 전 Controller에 기록해 잠긴 공개 gate를 만든다.
                if blocker_name == "event-runtime":
                    controller.mark_event_runtime_failed()
                elif blocker_name == "process-ownership":
                    controller.mark_process_ownership_ambiguous(
                        "parent_identity_lost"
                    )
                else:
                    accepted = controller.observe_order_result(
                        OrderResult(
                            symbol="ETHUSDT",
                            client_order_id="manual-external-before-start",
                            exchange_order_id="92004",
                            status=OrderStatus.NEW,
                            processed_at=MARKET_UPDATED_AT,
                        )
                    )
                    self.assertFalse(accepted)  # Prefixless execution은 app-owned 주문으로 수락하지 않는다.

                # Route가 command_enabled를 우회해 Controller를 직접 호출해도 Context와 STM은 시작 전 상태를 유지한다.
                context_before = controller.context
                self.assertFalse(controller.command_enabled)
                with self.assertRaises(TradingSessionError) as blocked_start:
                    controller.start_trading(
                        command_id=f"start-after-{blocker_name}",
                        expected_version=selection.version,
                    )

                self.assertIs(
                    blocked_start.exception.code,
                    TradingSessionFailureCode.POSITION_RECONCILIATION_REQUIRED,
                )
                self.assertEqual(context_before, controller.context)
                self.assertIs(controller.status, TradingSessionStatus.NOT_STARTED)
                self.assertIsNotNone(selected_stm)
                self.assertIs(
                    selected_stm.current_state.root_state,
                    RootState.NOT_STARTED,
                )
                self.assertTrue(controller.reconciliation_required)

    def test_split_command_is_versioned_and_idempotent(self) -> None:
        """
        함수 이름: test_split_command_is_versioned_and_idempotent()
        기능: 분할 비율 command의 version 증가, duplicate와 stale 거부를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 동일 split command를 같은 fingerprint로 두 번 호출해 exact replay를 유도한다.
        controller, _, _ = _create_ready_controller()
        first = controller.update_split_ratios(
            command_id="split-1",
            expected_version=0,
            scale_in=Decimal("0.25"),
            scale_out=Decimal("0.75"),
        )
        duplicate = controller.update_split_ratios(
            command_id="split-1",
            expected_version=0,
            scale_in=Decimal("0.25"),
            scale_out=Decimal("0.75"),
        )

        # 최초 mutation만 version을 올리고 새 stale command는 typed 오류로 거부한다.
        self.assertIs(first, duplicate)
        self.assertEqual(1, first.version)
        with self.assertRaises(TradingSessionError) as stale:
            controller.update_split_ratios(
                command_id="split-stale",
                expected_version=0,
                scale_in=Decimal("0.5"),
                scale_out=Decimal("0.5"),
            )
        self.assertIs(
            stale.exception.code,
            TradingSessionFailureCode.STALE_CONTEXT_VERSION,
        )

    def test_account_market_and_open_position_guards_preserve_start_state(
        self,
    ) -> None:
        """
        함수 이름: test_account_market_and_open_position_guards_preserve_start_state()
        기능: 세 start prerequisite 실패가 Context와 staged STM을 변경하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # account bootstrap을 생략한 Controller로 ACCOUNT_NOT_READY Guard를 검증한다.
        unready_account = Account()
        unready_client = SynchronousAccountWebSocketClient([])
        unready_gateway = WebSocketGateway(
            unready_client,
            account_snapshot_callback=unready_account.apply_stream_snapshot,
        )
        account_controller = TradingController(
            APIGateway(FakeAccountRESTClient([])),
            unready_gateway,
            unready_account,
            _ready_market_snapshot(),
            command_gate=True,
            clock=lambda: MARKET_UPDATED_AT,
        )
        account_regime = RegimeController(
            RegimeSTM(),
            account_controller._market_snapshot,
            account_controller,
        )
        account_selection = account_regime.set_regime_type(
            RegimeType.TYPE_0,
            command_id="account-select",
            expected_version=0,
        )
        with self.assertRaises(TradingSessionError) as account_error:
            account_controller.start_trading(
                command_id="account-start",
                expected_version=account_selection.version,
            )
        self.assertIs(
            account_error.exception.code,
            TradingSessionFailureCode.ACCOUNT_NOT_READY,
        )

        # ready account를 재사용하되 빈 MarketSnapshot으로 MARKET_NOT_READY Guard를 검증한다.
        ready_controller, _, _ = _create_ready_controller()
        market_controller = TradingController(
            ready_controller._api_gateway,
            ready_controller._web_socket_gateway,
            ready_controller.account,
            MarketSnapshot(clock=lambda: MARKET_UPDATED_AT),
            command_gate=True,
            clock=lambda: MARKET_UPDATED_AT,
        )
        market_regime = RegimeController(
            RegimeSTM(),
            market_controller._market_snapshot,
            market_controller,
        )
        market_selection = market_regime.set_regime_type(
            RegimeType.TYPE_0,
            command_id="market-select",
            expected_version=0,
        )
        with self.assertRaises(TradingSessionError) as market_error:
            market_controller.start_trading(
                command_id="market-start",
                expected_version=market_selection.version,
            )
        self.assertIs(
            market_error.exception.code,
            TradingSessionFailureCode.MARKET_NOT_READY,
        )

        # open position 거부가 Context version과 staged STM을 보존하는지 검증한다.
        position_controller, position_regime, _ = _create_ready_controller()
        position_selection = position_regime.set_regime_type(
            RegimeType.TYPE_0,
            command_id="position-select",
            expected_version=0,
        )
        position_controller.update_position_snapshot(
            PositionSnapshot(
                quantity=Decimal("0.5"),
                entry_price=Decimal("2400"),
            ),
            owner=StrategyType.CASE_B,
        )
        version_before = position_controller.context.version
        staged_stm = position_controller._selected_stm
        with self.assertRaises(TradingSessionError) as position_error:
            position_controller.start_trading(
                command_id="position-start",
                expected_version=position_selection.version,
            )
        self.assertIs(
            position_error.exception.code,
            TradingSessionFailureCode.POSITION_RECONCILIATION_REQUIRED,
        )
        self.assertEqual(version_before, position_controller.context.version)
        self.assertIs(
            staged_stm.current_state.root_state,
            RootState.NOT_STARTED,
        )


class TradingSessionStopTests(unittest.TestCase):
    """
    클래스 이름: TradingSessionStopTests
    기능: zero position, force-sell, pending reconciliation과 자원 정리를 검증한다.
    작성 날짜: 2026/08/21
    """

    def _start(self) -> TradingController:
        """
        함수 이름: _start()
        기능: TYPE_0을 선택하고 RUNNING 상태인 ready Controller를 만든다.
        인자: 없음
        반환값: RUNNING TradingController
        작성 날짜: 2026/08/21
        """
        # ready fixture에서 TYPE_0을 선택하고 같은 version으로 session을 시작한다.
        controller, regime_controller, _ = _create_ready_controller()
        selection = regime_controller.set_regime_type(
            RegimeType.TYPE_0,
            command_id="select-type0",
            expected_version=0,
        )
        controller.start_trading(
            command_id="start-session",
            expected_version=selection.version,
        )
        return controller

    def test_zero_position_stop_terminates_without_sell_and_cleans_subscription(
        self,
    ) -> None:
        """
        함수 이름: test_zero_position_stop_terminates_without_sell_and_cleans_subscription()
        기능: G-05가 sell 0회, TERMINATED와 session 구독 정리를 보장하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # RUNNING Controller에 구독을 등록하고 최초·중복·종료 후 stop을 순서대로 호출한다.
        controller = self._start()
        session_subscription = FakeSubscription()
        controller.register_session_subscription(session_subscription)
        version_before = controller.context.version
        stopped = controller.stop_trading(
            command_id="stop-zero",
            expected_version=version_before,
        )
        duplicate = controller.stop_trading(
            command_id="stop-zero",
            expected_version=version_before,
        )
        terminated_no_op = controller.stop_trading(
            command_id="stop-zero-after-terminated",
            expected_version=controller.context.version,
        )

        # 최초 G-05만 전이하고 sell 없이 TERMINATED cleanup이 완료되는지 확인한다.
        self.assertIs(stopped, duplicate)
        self.assertIs(stopped.status, TradingSessionStatus.TERMINATED)
        self.assertEqual(("G-05",), stopped.transition_ids)
        self.assertIs(
            terminated_no_op.status,
            TradingSessionStatus.TERMINATED,
        )
        self.assertFalse(terminated_no_op.transition_ids)
        self.assertFalse(terminated_no_op.action_requests)
        self.assertFalse(
            any(
                isinstance(action, ForceSellAll)
                for action in controller.external_action_requests
            )
        )
        self.assertTrue(session_subscription.closed)
        self.assertEqual(0, controller.pending_schedule_count)

    def test_terminated_session_requires_regime_reselection_before_restart(
        self,
    ) -> None:
        """
        함수 이름: test_terminated_session_requires_regime_reselection_before_restart()
        기능: 정상 종료 뒤 같은 REGIME도 다시 선택해야 새 session을 시작하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # TYPE_0 session을 시작하고 zero-position G-05로 완전히 종료한다.
        controller, regime_controller, _ = _create_ready_controller()
        initial_selection = regime_controller.set_regime_type(
            RegimeType.TYPE_0,
            command_id="select-before-termination",
            expected_version=0,
        )
        first_session = controller.start_trading(
            command_id="start-before-termination",
            expected_version=initial_selection.version,
        )
        stopped = controller.stop_trading(
            command_id="stop-before-reselection",
            expected_version=controller.context.version,
        )

        # 새 selection command 없이 직접 재시작하면 consumed selection Guard로 거부한다.
        with self.assertRaises(TradingSessionError) as missing_reselection:
            controller.start_trading(
                command_id="restart-without-reselection",
                expected_version=stopped.version,
            )
        self.assertIs(
            missing_reselection.exception.code,
            TradingSessionFailureCode.NO_SELECTED_REGIME,
        )

        # 같은 TYPE_0도 명시적으로 다시 선택하면 새 STM과 session ID로 시작한다.
        repeated_selection = regime_controller.set_regime_type(
            RegimeType.TYPE_0,
            command_id="select-after-termination",
            expected_version=stopped.version,
        )
        restarted = controller.start_trading(
            command_id="start-after-reselection",
            expected_version=repeated_selection.version,
        )
        self.assertIs(restarted.status, TradingSessionStatus.RUNNING)
        self.assertNotEqual(first_session.session_id, restarted.session_id)

    def test_zero_position_stop_preserves_context_action_order(self) -> None:
        """
        함수 이름: test_zero_position_stop_preserves_context_action_order()
        기능: G-05의 lower close, Case reset과 runtime patch가 명세 순서로 적용되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # mutation 기록 Context로 session을 시작한 뒤 start 단계 기록을 제거한다.
        recording_context = RecordingTradingContext()
        controller, regime_controller, _ = _create_ready_controller(
            context=recording_context,
        )
        selection = regime_controller.set_regime_type(
            RegimeType.TYPE_0,
            command_id="select-ordered-stop",
            expected_version=0,
        )
        controller.start_trading(
            command_id="start-ordered-stop",
            expected_version=selection.version,
        )
        recording_context.mutation_order.clear()

        # G-05 Action 목록의 Context mutation이 patch 선적용 없이 원래 위치를 유지한다.
        controller.stop_trading(
            command_id="stop-ordered-zero",
            expected_version=controller.context.version,
        )

        self.assertEqual(
            [
                "close_lower_event",
                "reset_case_b",
                "reset_case_c",
                "patch",
            ],
            recording_context.mutation_order,
        )

    def test_subscription_close_failure_keeps_terminal_state_consistent(
        self,
    ) -> None:
        """
        함수 이름: test_subscription_close_failure_keeps_terminal_state_consistent()
        기능: 하나의 close 실패가 나머지 cleanup, terminal publish와 command replay를 막지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 실패·성공 subscription을 함께 등록해 cleanup 격리 조건을 준비한다.
        controller = self._start()
        failing_subscription = FailingSessionSubscription()
        successful_subscription = FakeSubscription()
        controller.register_session_subscription(failing_subscription)
        controller.register_session_subscription(successful_subscription)
        expected_version = controller.context.version

        # cleanup 실패는 진단에 보존하되 이미 commit된 G-05 lifecycle을 반쪽 상태로 만들지 않는다.
        stopped = controller.stop_trading(
            command_id="stop-with-close-failure",
            expected_version=expected_version,
        )
        duplicate = controller.stop_trading(
            command_id="stop-with-close-failure",
            expected_version=expected_version,
        )

        self.assertIs(stopped, duplicate)
        self.assertIs(stopped.status, TradingSessionStatus.TERMINATED)
        self.assertIs(controller.status, TradingSessionStatus.TERMINATED)
        self.assertTrue(failing_subscription.close_attempted)
        self.assertTrue(successful_subscription.closed)
        self.assertEqual(1, len(controller.cleanup_failures))
        self.assertIsInstance(controller.cleanup_failures[0], RuntimeError)

    def test_cleanup_closes_registration_before_subscription_callback(self) -> None:
        """
        함수 이름: test_cleanup_closes_registration_before_subscription_callback()
        기능: session close callback 재진입이 terminal 뒤 새 구독을 유출하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 재진입 등록 결과를 관찰할 subscription과 오류 수집기를 준비한다.
        controller = self._start()
        leaked_subscription = FakeSubscription()
        registration_failures: list[TradingSessionError] = []

        def attempt_reentrant_registration() -> None:
            """
            함수 이름: attempt_reentrant_registration()
            기능: cleanup 도중 새 세션 구독 등록을 시도하고 typed 거부를 기록한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/08/21
            """
            # 새 subscription 등록을 시도하고 예상된 typed 오류만 수집한다.
            try:
                controller.register_session_subscription(leaked_subscription)
            except TradingSessionError as error:
                registration_failures.append(error)

        # 악의적 close callback 구독을 등록하고 stop cleanup의 재진입 차단을 실행한다.
        reentrant_subscription = ReentrantSessionSubscription(
            attempt_reentrant_registration
        )
        controller.register_session_subscription(reentrant_subscription)

        controller.stop_trading(
            command_id="stop-reentrant-cleanup",
            expected_version=controller.context.version,
        )

        # 거부된 handle이 후속 cleanup에도 유출되지 않고 callback도 한 번만 실행되는지 확인한다.
        self.assertEqual(1, reentrant_subscription.close_count)
        self.assertEqual(1, len(registration_failures))
        self.assertIs(
            registration_failures[0].code,
            TradingSessionFailureCode.TRADING_NOT_STARTED,
        )
        self.assertFalse(leaked_subscription.closed)
        controller.close_session_resources()  # 멱등 후속 cleanup이 유출 handle을 닫을 필요가 없어야 한다.
        self.assertFalse(leaked_subscription.closed)

    def test_open_position_stop_generates_one_force_sell_and_blocks_events(
        self,
    ) -> None:
        """
        함수 이름: test_open_position_stop_generates_one_force_sell_and_blocks_events()
        기능: G-06가 실제 포지션에 ForceSellAll 한 번만 만들고 신규 event를 차단하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # open position과 session handle을 준비한 뒤 서로 다른 stop command를 연속 전송한다.
        controller = self._start()
        session_subscription = FakeSubscription()
        controller.register_session_subscription(session_subscription)
        controller.update_position_snapshot(
            PositionSnapshot(
                quantity=Decimal("1"),
                entry_price=Decimal("2400"),
            ),
            owner=StrategyType.CASE_B,
        )
        first_stop = controller.stop_trading(
            command_id="stop-position",
            expected_version=controller.context.version,
        )
        second_stop = controller.stop_trading(
            command_id="stop-position-again",
            expected_version=controller.context.version,
        )

        # 최초 G-06만 ForceSellAll을 만들고 일반 event와 cleanup은 보류하는지 확인한다.
        self.assertIs(first_stop.status, TradingSessionStatus.STOPPING)
        self.assertEqual(("G-06",), first_stop.transition_ids)
        self.assertFalse(second_stop.transition_ids)
        self.assertEqual(
            1,
            sum(
                isinstance(action, ForceSellAll)
                for action in controller.external_action_requests
            ),
        )
        self.assertIsNone(controller.enqueue_event(self._market_event()))
        self.assertFalse(session_subscription.closed)

        # Position 반영 전 terminal success는 버려 같은 stable ID의 정상 재전송을 보존한다.
        premature_outcome = TradingEvent(
            event_type=TradingEventType.FORCE_SELL_FINISHED,
            occurred_at=MARKET_UPDATED_AT,
            priority=EventPriority.ORDER_OUTCOME,
            event_id="force-sell-order-1:finished",
            payload=ForceSellOutcomePayload(),
        )
        self.assertIsNone(controller.enqueue_event(premature_outcome))

        # Phase 8가 체결을 반영한 뒤 전달할 terminal outcome은 STOPPING에서도 허용한다.
        controller.update_position_snapshot(PositionSnapshot(), owner=None)
        missing_payload_outcome = TradingEvent(
            event_type=TradingEventType.FORCE_SELL_FINISHED,
            occurred_at=MARKET_UPDATED_AT,
            priority=EventPriority.ORDER_OUTCOME,
        )
        self.assertIsNone(controller.enqueue_event(missing_payload_outcome))
        outcome = premature_outcome
        self.assertIsNotNone(controller.enqueue_event(outcome))
        self.assertIsNone(
            controller.enqueue_event(outcome)
        )  # stable outcome ID를 다시 전달해도 retry intent가 중복되지 않는다.

        # terminal outcome 대기 중 재-stop은 queue를 비우거나 새 intent를 만들지 않는다.
        progress = controller.stop_trading(
            command_id="stop-position-while-outcome-pending",
            expected_version=controller.context.version,
        )
        self.assertFalse(progress.transition_ids)

        # queue의 terminal outcome을 처리해 G-06F와 session cleanup을 완료한다.
        completion = asyncio.run(controller.process_next_event())
        self.assertEqual(("G-06F",), completion.transition_ids)
        self.assertIs(controller.status, TradingSessionStatus.TERMINATED)
        self.assertTrue(session_subscription.closed)

    def test_public_event_intake_rejects_lifecycle_command_bypass(self) -> None:
        """
        함수 이름: test_public_event_intake_rejects_lifecycle_command_bypass()
        기능: queue API가 start/stop Operation의 version·멱등 Guard를 우회하지 못하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # forged lifecycle event 두 개와 정상 market event를 intake 검증용으로 준비한다.
        controller = self._start()
        stop_bypass = TradingEvent(
            event_type=TradingEventType.STOP_CONFIRMED,
            occurred_at=MARKET_UPDATED_AT,
            priority=EventPriority.USER_COMMAND,
            event_id="forged-stop-command",
        )
        start_bypass = TradingEvent(
            event_type=TradingEventType.LOGIC_STARTED,
            occurred_at=MARKET_UPDATED_AT,
            priority=EventPriority.INTERNAL,
            event_id="forged-start-command",
        )
        valid_market_event = TradingEvent(
            event_type=TradingEventType.MARKET_DATA_UPDATED,
            occurred_at=MARKET_UPDATED_AT,
            priority=EventPriority.MARKET,
            event_id="market-update-1",
        )

        # lifecycle bypass만 버리고 정상 market event와 RUNNING 상태는 보존하는지 확인한다.
        self.assertIsNone(controller.enqueue_event(stop_bypass))
        self.assertIsNone(controller.enqueue_event(start_bypass))
        self.assertIsNotNone(controller.enqueue_event(valid_market_event))
        self.assertIs(controller.status, TradingSessionStatus.RUNNING)

    def test_pending_stop_cancels_then_reconciles_without_force_sell(self) -> None:
        """
        함수 이름: test_pending_stop_cancels_then_reconciles_without_force_sell()
        기능: G-06P가 pending을 우선 취소·재조회하고 새 force-sell을 만들지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # pending buy와 scheduled retry가 있는 RUNNING session을 만든 뒤 stop한다.
        controller = self._start()
        controller.update_pending_order_snapshot(
            PendingOrderSnapshot(
                order_id="order-1",
                strategy=StrategyType.CASE_C,
                side=OrderSide.BUY,
                attempt_kind=OrderAttemptKind.INITIAL,
            )
        )
        controller._execute_action(
            ScheduleReevaluation(
                event_type=TradingEventType.RETRY_C_WAIT_SETUP,
                trigger=ReevaluationTrigger.RETRY_BACKOFF,
                earliest_delay=timedelta(seconds=10),
                reason="pending-stop-cleanup",
            )
        )
        stopped = controller.stop_trading(
            command_id="stop-pending",
            expected_version=controller.context.version,
        )
        external_actions = controller.external_action_requests

        # G-06P는 cancel·reconcile만 내보내고 force-sell과 예약 작업을 남기지 않는다.
        self.assertIs(
            stopped.status,
            TradingSessionStatus.RECONCILIATION_REQUIRED,
        )
        self.assertEqual(("G-06P",), stopped.transition_ids)
        self.assertEqual(
            (CancelPendingOrder, ReconcileOrder),
            tuple(type(action) for action in external_actions),
        )
        self.assertFalse(
            any(isinstance(action, ForceSellAll) for action in external_actions)
        )
        self.assertEqual(0, controller.pending_schedule_count)

        # pending reconciliation 중 force-sell 실패를 가장해도 retry intent를 만들지 않는다.
        forged_failure = TradingEvent(
            event_type=TradingEventType.FORCE_SELL_FAILED,
            occurred_at=MARKET_UPDATED_AT,
            priority=EventPriority.ORDER_OUTCOME,
            event_id="pending-order-1:force-sell-failed",
            payload=ForceSellOutcomePayload(
                execution_applied=False,
                history_persisted=False,
                terminal_unfilled=True,
            ),
        )
        self.assertIsNone(controller.enqueue_event(forged_failure))
        self.assertEqual(
            (CancelPendingOrder, ReconcileOrder),
            tuple(type(action) for action in controller.external_action_requests),
        )

    def test_scheduler_releases_only_after_explicit_due_trigger(self) -> None:
        """
        함수 이름: test_scheduler_releases_only_after_explicit_due_trigger()
        기능: scheduler가 busy loop 없이 due candle trigger에서만 event를 enqueue하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 5초 지연된 candle-boundary 재평가를 scheduler에 등록한다.
        controller = self._start()
        controller._execute_action(
            ScheduleReevaluation(
                event_type=TradingEventType.RETRY_C_WAIT_SETUP,
                trigger=ReevaluationTrigger.NEXT_1M_CLOSE,
                earliest_delay=timedelta(seconds=5),
                reason="test-candle-boundary",
            )
        )

        # due 직전에는 release하지 않고 정확한 due 시각에 한 번만 enqueue하는지 확인한다.
        self.assertEqual(
            (),
            controller.trigger_scheduled_evaluations(
                ReevaluationTrigger.NEXT_1M_CLOSE,
                occurred_at=MARKET_UPDATED_AT + timedelta(seconds=4),
            ),
        )
        released = controller.trigger_scheduled_evaluations(
            ReevaluationTrigger.NEXT_1M_CLOSE,
            occurred_at=MARKET_UPDATED_AT + timedelta(seconds=5),
        )
        self.assertEqual(1, len(released))
        self.assertEqual(0, controller.pending_schedule_count)

    @staticmethod
    def _market_event() -> TradingEvent:
        """
        함수 이름: _market_event()
        기능: STOPPING 상태의 신규 entry 차단 검증에 사용할 시장 event를 만든다.
        인자: 없음
        반환값: queue에 넣으려 시도할 TradingEvent
        작성 날짜: 2026/08/21
        """
        return TradingEvent.create(
            TradingEventType.MARKET_DATA_UPDATED,
            occurred_at=MARKET_UPDATED_AT,
        )


class ReconciliationCauseLatchTests(unittest.TestCase):
    """
    클래스 이름: ReconciliationCauseLatchTests
    기능: Controller 재조정 원인의 최초 기록과 중복·충돌 흡수 상태를 검증한다.
    작성 날짜: 2026/08/31
    """

    def _start_controller(self) -> TradingController:
        """
        함수 이름: _start_controller()
        기능: 재조정 origin을 호출할 RUNNING Controller를 결정론적으로 준비한다.
        인자: 없음
        반환값: TYPE_0 세션을 시작한 TradingController
        작성 날짜: 2026/08/31
        """
        # 기존 ready fixture로 account·market 조건을 맞춘 뒤 같은 version에서 세션을 시작한다.
        controller, regime_controller, _ = _create_ready_controller()
        selection = regime_controller.set_regime_type(
            RegimeType.TYPE_0,
            command_id="select-reconciliation-cause",
            expected_version=controller.context.version,
        )
        controller.start_trading(
            command_id="start-reconciliation-cause",
            expected_version=selection.version,
        )
        return controller

    @staticmethod
    def _run_after_barrier(
        barrier: Barrier,
        operation: Callable[[], None],
    ) -> None:
        """
        함수 이름: _run_after_barrier()
        기능: 두 cause origin을 같은 결정론적 시작 경계 뒤 별도 thread에서 호출한다.
        인자: barrier -> main과 두 worker가 공유할 시작 Barrier
            operation -> Barrier 통과 뒤 한 번 호출할 원인 기록 Operation
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        # 모든 participant가 준비될 때까지 기다린 뒤 production 공개 경계를 정확히 한 번 호출한다.
        barrier.wait()
        operation()

    def test_initial_snapshot_reports_missing_without_category(self) -> None:
        """
        함수 이름: test_initial_snapshot_reports_missing_without_category()
        기능: 원인을 기록하지 않은 Controller가 범주를 추측하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        controller, _, _ = _create_ready_controller()

        # Startup blocker 여부와 무관하게 기록되지 않은 원인은 MISSING과 None으로만 공개한다.
        snapshot = controller.reconciliation_cause_snapshot
        self.assertIs(snapshot.status, ReconciliationCauseStatus.MISSING)
        self.assertIsNone(snapshot.category)
        self.assertEqual(
            controller.reconciliation_required,
            snapshot.reconciliation_required,
        )
        with self.assertRaises(FrozenInstanceError):
            snapshot.status = ReconciliationCauseStatus.EXACT  # type: ignore[misc]

    def test_pre_session_market_initializing_gate_keeps_cause_missing(
        self,
    ) -> None:
        """
        함수 이름: test_pre_session_market_initializing_gate_keeps_cause_missing()
        기능: 정상 startup 시장 gate가 effect를 잠그되 실패 cause로 기록되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/04
        """
        controller, _, _ = _create_ready_controller(
            market_stream_recovery_enabled=True,
        )

        # Initialization 기간에는 public blocker를 열되 최초 실패 origin은 생성하지 않는다.
        controller.mark_market_stream_reconciliation_required(
            "market_stream_initializing"
        )
        blocked_snapshot = controller.reconciliation_cause_snapshot
        self.assertTrue(blocked_snapshot.reconciliation_required)
        self.assertTrue(controller.market_stream_reconciliation_required)
        self.assertIs(
            blocked_snapshot.status,
            ReconciliationCauseStatus.MISSING,
        )
        self.assertIsNone(blocked_snapshot.category)

        # Same-version live resync 완료 뒤에는 시장 blocker만 해제하고 monotonic cause는 MISSING을 유지한다.
        with patch.object(
            WebSocketGateway,
            "kline_live_ready",
            new_callable=PropertyMock,
            return_value=True,
        ):
            controller.complete_market_stream_reconciliation(
                controller._market_snapshot.version
            )
        completed_snapshot = controller.reconciliation_cause_snapshot
        self.assertFalse(controller.market_stream_reconciliation_required)
        self.assertEqual(
            controller.reconciliation_required,
            completed_snapshot.reconciliation_required,
        )
        self.assertIs(
            completed_snapshot.status,
            ReconciliationCauseStatus.MISSING,
        )
        self.assertIsNone(completed_snapshot.category)

        # 같은 NOT_STARTED에서도 실제 초기화 실패는 이후 evidence가 보존할 최초 origin으로 기록한다.
        controller.mark_market_stream_reconciliation_required(
            "market_stream_initialization_failed"
        )
        failed_snapshot = controller.reconciliation_cause_snapshot
        self.assertTrue(failed_snapshot.reconciliation_required)
        self.assertIs(
            failed_snapshot.status,
            ReconciliationCauseStatus.EXACT,
        )
        self.assertIs(
            failed_snapshot.category,
            ReconciliationCauseCategory.MARKET_STREAM_FAILED,
        )

    def test_account_stream_origin_records_exact_category_once(self) -> None:
        """
        함수 이름: test_account_stream_origin_records_exact_category_once()
        기능: RUNNING account stream 장애가 지정된 단일 원인 범주로 기록되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        controller = self._start_controller()

        # Credential 없는 typed 사유를 실제 공개 origin에 전달해 atomic snapshot을 읽는다.
        controller.mark_account_stream_reconciliation_required(
            "account_stream_disconnected"
        )
        snapshot = controller.reconciliation_cause_snapshot
        self.assertTrue(snapshot.reconciliation_required)
        self.assertIs(snapshot.status, ReconciliationCauseStatus.EXACT)
        self.assertIs(
            snapshot.category,
            ReconciliationCauseCategory.ACCOUNT_STREAM_UNKNOWN_OR_EXTERNAL_EXECUTION,
        )

    def test_not_started_public_origins_do_not_leave_missing_cause(self) -> None:
        """
        함수 이름: test_not_started_public_origins_do_not_leave_missing_cause()
        기능: NOT_STARTED의 account·market·event blocker도 정확한 원인 하나를 남기는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        cause_cases: tuple[
            tuple[
                str,
                Callable[[TradingController], None],
                ReconciliationCauseCategory,
            ],
            ...,
        ] = (
            (
                "account",
                lambda controller: controller.mark_account_stream_reconciliation_required(
                    "account_stream_disconnected"
                ),
                ReconciliationCauseCategory.ACCOUNT_STREAM_UNKNOWN_OR_EXTERNAL_EXECUTION,
            ),
            (
                "market",
                lambda controller: controller.mark_market_stream_reconciliation_required(
                    "market_stream_disconnected"
                ),
                ReconciliationCauseCategory.MARKET_STREAM_FAILED,
            ),
            (
                "event",
                lambda controller: controller.mark_event_runtime_failed(),
                ReconciliationCauseCategory.EVENT_WORKER_OR_RUNTIME_FAILED,
            ),
        )

        # 각 public origin은 세션을 시작하지 않은 별도 ready Controller에서 독립 검증한다.
        for label, operation, expected_category in cause_cases:
            with self.subTest(origin=label):
                controller, _, _ = _create_ready_controller()
                operation(controller)
                snapshot = controller.reconciliation_cause_snapshot
                self.assertTrue(snapshot.reconciliation_required)
                self.assertIs(snapshot.status, ReconciliationCauseStatus.EXACT)
                self.assertIs(snapshot.category, expected_category)

    def test_same_category_second_record_latches_duplicate(self) -> None:
        """
        함수 이름: test_same_category_second_record_latches_duplicate()
        기능: 같은 origin의 두 번째 기록이 범주를 숨긴 DUPLICATE 흡수 상태가 되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        controller = self._start_controller()
        controller.mark_account_stream_reconciliation_required(
            "account_stream_disconnected"
        )
        controller.mark_account_stream_reconciliation_required(
            "account_stream_callback_failed"
        )

        # 중복을 단일 원인으로 완화하지 않고 이후 다른 origin도 최초 모호성 상태를 바꾸지 않는다.
        duplicate_snapshot = controller.reconciliation_cause_snapshot
        self.assertIs(
            duplicate_snapshot.status,
            ReconciliationCauseStatus.DUPLICATE,
        )
        self.assertIsNone(duplicate_snapshot.category)
        controller.mark_process_ownership_ambiguous("parent_identity_lost")
        self.assertEqual(
            duplicate_snapshot,
            controller.reconciliation_cause_snapshot,
        )

    def test_different_category_second_record_latches_conflict(self) -> None:
        """
        함수 이름: test_different_category_second_record_latches_conflict()
        기능: 서로 다른 두 origin이 category 없는 CONFLICT 흡수 상태가 되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        controller = self._start_controller()
        controller.mark_account_stream_reconciliation_required(
            "account_stream_disconnected"
        )
        controller.mark_event_runtime_failed()

        # 두 category의 도착 순서와 무관하게 외부에는 모호한 상태만 공개한다.
        conflict_snapshot = controller.reconciliation_cause_snapshot
        self.assertTrue(conflict_snapshot.reconciliation_required)
        self.assertIs(
            conflict_snapshot.status,
            ReconciliationCauseStatus.CONFLICT,
        )
        self.assertIsNone(conflict_snapshot.category)
        controller.mark_account_stream_reconciliation_required(
            "account_stream_callback_failed"
        )
        self.assertEqual(
            conflict_snapshot,
            controller.reconciliation_cause_snapshot,
        )

    def test_account_and_event_origin_race_latches_conflict(self) -> None:
        """
        함수 이름: test_account_and_event_origin_race_latches_conflict()
        기능: account와 event origin의 동시 도착이 순서와 무관하게 CONFLICT로 닫히는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        controller = self._start_controller()
        start_barrier = Barrier(3)
        account_thread = Thread(
            target=self._run_after_barrier,
            args=(
                start_barrier,
                lambda: controller.mark_account_stream_reconciliation_required(
                    "account_stream_disconnected"
                ),
            ),
        )
        event_thread = Thread(
            target=self._run_after_barrier,
            args=(start_barrier, controller.mark_event_runtime_failed),
        )

        # Main도 같은 Barrier를 통과시킨 뒤 두 callback이 session lock에서 직렬화될 때까지 기다린다.
        account_thread.start()
        event_thread.start()
        start_barrier.wait()
        account_thread.join(timeout=2)
        event_thread.join(timeout=2)
        self.assertFalse(account_thread.is_alive())
        self.assertFalse(event_thread.is_alive())

        # 어느 origin이 lock을 먼저 얻어도 두 번째 category는 외부 범주를 숨긴 충돌로 고정한다.
        snapshot = controller.reconciliation_cause_snapshot
        self.assertTrue(snapshot.reconciliation_required)
        self.assertIs(snapshot.status, ReconciliationCauseStatus.CONFLICT)
        self.assertIsNone(snapshot.category)

    def test_event_runtime_origin_records_exact_category(self) -> None:
        """
        함수 이름: test_event_runtime_origin_records_exact_category()
        기능: worker가 호출하는 runtime failure origin이 안정된 단일 범주를 남기는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        controller = self._start_controller()

        # 실제 worker 공개 경계와 같은 Operation을 한 번 호출해 secret 없는 category를 검증한다.
        controller.mark_event_runtime_failed()
        snapshot = controller.reconciliation_cause_snapshot
        self.assertIs(snapshot.status, ReconciliationCauseStatus.EXACT)
        self.assertIs(
            snapshot.category,
            ReconciliationCauseCategory.EVENT_WORKER_OR_RUNTIME_FAILED,
        )

    def test_context_patch_failure_keeps_reconciliation_status_and_cause(
        self,
    ) -> None:
        """
        함수 이름: test_context_patch_failure_keeps_reconciliation_status_and_cause()
        기능: 재조정 Context patch 예외 뒤에도 공개 status와 최초 원인이 닫힌 채 유지되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        controller = self._start_controller()

        # Context writer 실패를 실제 event origin에 주입해 status commit 이후 예외가 전파되게 한다.
        with patch.object(
            TradingContext,
            "apply_runtime_patch",
            side_effect=RuntimeError("controlled-reconciliation-patch-failure"),
        ):
            with self.assertRaises(RuntimeError):
                controller.mark_event_runtime_failed()

        # Context phase 전이가 실패해도 신규 command의 공개 blocker와 stable cause는 보존한다.
        self.assertIs(
            controller.status,
            TradingSessionStatus.RECONCILIATION_REQUIRED,
        )
        snapshot = controller.reconciliation_cause_snapshot
        self.assertTrue(snapshot.reconciliation_required)
        self.assertIs(snapshot.status, ReconciliationCauseStatus.EXACT)
        self.assertIs(
            snapshot.category,
            ReconciliationCauseCategory.EVENT_WORKER_OR_RUNTIME_FAILED,
        )

    def test_failure_finalizer_gate_is_atomic_with_terminal_stop(self) -> None:
        """
        함수 이름: test_failure_finalizer_gate_is_atomic_with_terminal_stop()
        기능: finalizer gate와 zero-position STOP 경합이 terminal poison이나 cause latch 오염을 만들지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        terminal_controller = self._start_controller()
        terminal_controller.stop_trading(
            command_id="terminal-before-finalizer-gate",
            expected_version=terminal_controller.context.version,
        )
        terminal_snapshot_before_seal = (
            terminal_controller.reconciliation_cause_snapshot
        )

        # 이미 완결된 zero-exposure terminal은 event blocker와 cause를 새로 만들지 않는다.
        self.assertFalse(
            terminal_controller.seal_event_runtime_failure_gate()
        )
        terminal_snapshot = terminal_controller.reconciliation_cause_snapshot
        self.assertIs(
            terminal_controller.status,
            TradingSessionStatus.TERMINATED,
        )
        self.assertEqual(
            terminal_snapshot_before_seal,
            terminal_snapshot,
        )

        reconciliation_controller = self._start_controller()
        reconciliation_controller.mark_account_stream_reconciliation_required(
            "account_stream_disconnected"
        )
        cause_before_seal = (
            reconciliation_controller.reconciliation_cause_snapshot
        )

        # 이미 닫힌 session은 future reconnect만 event flag로 막고 최초 account cause는 그대로 보존한다.
        self.assertFalse(
            reconciliation_controller.seal_event_runtime_failure_gate()
        )
        self.assertEqual(
            cause_before_seal,
            reconciliation_controller.reconciliation_cause_snapshot,
        )

        for race_index in range(16):
            controller = self._start_controller()
            initial_race_snapshot = controller.reconciliation_cause_snapshot
            start_barrier = Barrier(3)
            unexpected_errors: list[Exception] = []

            def seal_gate_after_barrier() -> None:
                """
                함수 이름: seal_gate_after_barrier()
                기능: main·STOP worker와 같은 시작 경계 뒤 atomic finalizer gate를 호출한다.
                인자: 없음
                반환값: 없음
                작성 날짜: 2026/08/31
                """
                start_barrier.wait()
                try:
                    controller.seal_event_runtime_failure_gate()
                except Exception as error:
                    unexpected_errors.append(error)

            def stop_after_barrier() -> None:
                """
                함수 이름: stop_after_barrier()
                기능: finalizer gate와 같은 시작 경계 뒤 zero-position STOP을 호출한다.
                인자: 없음
                반환값: 없음
                작성 날짜: 2026/08/31
                """
                start_barrier.wait()
                try:
                    controller.stop_trading(
                        command_id=f"atomic-finalizer-stop-{race_index}",
                        expected_version=controller.context.version,
                    )
                except TradingSessionError:
                    if (
                        controller.status
                        is not TradingSessionStatus.RECONCILIATION_REQUIRED
                    ):
                        unexpected_errors.append(
                            AssertionError(
                                "atomic finalizer race raised outside reconciliation"
                            )
                        )

            seal_thread = Thread(target=seal_gate_after_barrier)
            stop_thread = Thread(target=stop_after_barrier)
            seal_thread.start()
            stop_thread.start()
            start_barrier.wait()
            seal_thread.join(timeout=2)
            stop_thread.join(timeout=2)

            # Lock 획득 순서와 무관하게 terminal+event blocker의 불가능한 혼합 상태는 나오지 않는다.
            self.assertFalse(seal_thread.is_alive())
            self.assertFalse(stop_thread.is_alive())
            self.assertEqual([], unexpected_errors)
            race_snapshot = controller.reconciliation_cause_snapshot
            if controller.status is TradingSessionStatus.TERMINATED:
                self.assertEqual(initial_race_snapshot, race_snapshot)
            else:
                self.assertIs(
                    controller.status,
                    TradingSessionStatus.RECONCILIATION_REQUIRED,
                )
                self.assertTrue(race_snapshot.reconciliation_required)
                self.assertIs(
                    race_snapshot.status,
                    ReconciliationCauseStatus.EXACT,
                )
                self.assertIs(
                    race_snapshot.category,
                    ReconciliationCauseCategory.EVENT_WORKER_OR_RUNTIME_FAILED,
                )

    def test_market_stream_origin_records_exact_category(self) -> None:
        """
        함수 이름: test_market_stream_origin_records_exact_category()
        기능: RUNNING Kline stream 장애가 account 장애와 구분된 범주를 남기는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        controller = self._start_controller()

        # 실제 MarketDataController callback 경계와 같은 공개 Operation을 한 번 호출한다.
        controller.mark_market_stream_reconciliation_required(
            "market_stream_disconnected"
        )
        snapshot = controller.reconciliation_cause_snapshot
        self.assertIs(snapshot.status, ReconciliationCauseStatus.EXACT)
        self.assertIs(
            snapshot.category,
            ReconciliationCauseCategory.MARKET_STREAM_FAILED,
        )

    def test_process_ownership_origin_records_exact_category(self) -> None:
        """
        함수 이름: test_process_ownership_origin_records_exact_category()
        기능: runtime ownership 손실이 stream 장애와 구분된 영구 원인을 남기는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        controller = self._start_controller()

        # Parent identity 상실 공개 Operation은 raw process 정보 없이 안정된 범주만 남긴다.
        controller.mark_process_ownership_ambiguous("parent_identity_lost")
        snapshot = controller.reconciliation_cause_snapshot
        self.assertIs(snapshot.status, ReconciliationCauseStatus.EXACT)
        self.assertIs(
            snapshot.category,
            ReconciliationCauseCategory.PROCESS_OWNERSHIP_AMBIGUOUS,
        )


if __name__ == "__main__":
    unittest.main()
