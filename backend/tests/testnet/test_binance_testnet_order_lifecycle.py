"""Production runtime과 Controller를 통한 Binance Spot Testnet 주문 lifecycle을 검증한다."""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import time
import unittest
from uuid import uuid4

from binance_auto_trader.application import TradingSessionStatus
from binance_auto_trader.bootstrap import (
    ApplicationStatus,
    close_application,
    start_application,
)
from binance_auto_trader.bootstrap.testnet import (
    create_testnet_application_runtime,
    load_testnet_configuration,
    require_testnet_order_permission,
)
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.history import Trade
from binance_auto_trader.domain.trading import SubmitOrder
from binance_auto_trader.domain.trading.action_requests import patch
from binance_auto_trader.domain.trading.order import OrderResult, OrderStatus
from binance_auto_trader.domain.trading.states import (
    OrderAttemptKind,
    OrderSide,
    StrategyType,
    TradingPhase,
)

from tests.testnet._support import (
    ORDER_SKIP_REASON,
    ORDER_TESTNET_REQUESTED,
    seed_verified_closed_history,
)


# 실제 거래소 조회와 Controller의 1·2·4·8초 reconciliation 예산을 모두 포함해 종료 시간을 제한한다.
_ORDER_SETTLEMENT_TIMEOUT_SECONDS = 45
_SESSION_STOP_TIMEOUT_SECONDS = 60
_POLL_INTERVAL_SECONDS = 0.25
_ARTIFACT_ROOT = Path(__file__).resolve().parents[2] / ".testnet-artifacts"


@unittest.skipUnless(
    ORDER_TESTNET_REQUESTED,
    ORDER_SKIP_REASON,
)
class BinanceTestnetOrderLifecycleTests(unittest.TestCase):
    """
    클래스 이름: BinanceTestnetOrderLifecycleTests
    기능: production runtime의 capped BUY, same-ID 확인, force-sell과 close를
        실제 Testnet에서 검증한다.
    작성 날짜: 2026/08/23

    주의: MarketData에서 TradingContext로 시장 평가값을 전달하는 공개 경계는 Phase 9 범위에 없다.
    따라서 BUY 신호 생성만 Phase 8 통합 테스트와 같은 private action seam을 사용하며, 향후 공개
    market-context adapter가 생기면 해당 seam을 실제 시장 event 입력으로 교체해야 한다.
    """

    def setUp(self) -> None:
        """
        함수 이름: setUp()
        기능: 이중 opt-in과 notional cap을 검증하고 격리된 production Testnet runtime을 조립한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        # 주문 opt-in만 켜고 cap을 누락한 실행은 실제 client 생성 전에 설정 오류로 중단한다.
        self.configuration = load_testnet_configuration()
        self.max_notional = require_testnet_order_permission(
            self.configuration
        )
        # 실제 주문 identity는 실패 뒤 same-ID 복구에 필요하므로 run별 ignored 경로에 보존한다.
        run_timestamp = datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S%fZ"
        )
        self.artifact_directory = _ARTIFACT_ROOT / (
            f"phase9-order-lifecycle-{run_timestamp}-{uuid4().hex}"
        )
        self.artifact_directory.mkdir(
            parents=True,
            mode=0o700,
        )
        self.history_path = self.artifact_directory / "history.jsonl"
        self.baseline_trades = seed_verified_closed_history(
            self.history_path
        )  # 이전 실제 run의 Position 0 provenance를 새 account startup에 먼저 인계한다.
        self.runtime = create_testnet_application_runtime(
            history_path=self.history_path,
        )  # 기본 Kline 개수를 유지해 production startup 지표 계산까지 실제로 수행한다.

    def _add_recovery_artifact_note(self, error: BaseException) -> None:
        """
        함수 이름: _add_recovery_artifact_note()
        기능: 실패 예외에 credential 없는 durable 경로와 관찰한 client ID를 덧붙인다.
        인자: error -> Testnet lifecycle 또는 cleanup에서 발생한 예외
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        # Client ID는 same-ID 복구 식별자이며 API key/secret이나 raw exchange payload는 포함하지 않는다.
        client_order_ids = sorted(
            {
                trace.client_order_id
                for trace in self.runtime.trading_controller.order_execution_trace
            }
        )
        identity_suffix = (
            f"; client_order_ids={','.join(client_order_ids)}"
            if client_order_ids
            else "; client_order_ids=none-observed"
        )
        error.add_note(
            "Phase 9 Testnet recovery artifacts preserved at "
            f"{self.artifact_directory}{identity_suffix}"
        )  # 운영자는 이 경로를 다음 startup same-ID reconciliation에 그대로 사용한다.

    def _drain_controller_work(self) -> None:
        """
        함수 이름: _drain_controller_work()
        기능: due same-order 조회를 실행하고 생성된 주문 outcome microstep을 모두 소비한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        # Controller scheduler만 호출하므로 polling 중에도 새 주문을 직접 제출하지 않는다.
        controller = self.runtime.trading_controller
        controller.trigger_order_reconciliation(
            occurred_at=datetime.now(timezone.utc),
        )
        asyncio.run(
            controller.drain_events()
        )  # terminal outcome을 다음 STM microstep까지 완료한다.

    def _client_order_id_for_intent(self, intent_id: str) -> str:
        """
        함수 이름: _client_order_id_for_intent()
        기능: Controller trace에서 한 BUY intent가 사용한 유일한 client order ID를 반환한다.
        인자: intent_id -> test-only BUY 주문 의도 식별자
        반환값: Controller가 결정론적으로 생성한 client order ID
        작성 날짜: 2026/08/23
        """
        # 재제출이 일어나지 않았다는 사실과 모든 단계의 same-ID 상관관계를 함께 검증한다.
        client_order_ids = {
            trace.client_order_id
            for trace in self.runtime.trading_controller.order_execution_trace
            if trace.intent_id == intent_id
        }
        self.assertEqual(1, len(client_order_ids))

        return next(iter(client_order_ids))  # 위 assertion 통과 뒤 유일한 canonical ID만 반환한다.

    def _wait_for_trade(
        self,
        client_order_id: str,
        *,
        timeout_seconds: int = _ORDER_SETTLEMENT_TIMEOUT_SECONDS,
    ) -> Trade:
        """
        함수 이름: _wait_for_trade()
        기능: 새 submit 없이 Controller same-order reconciliation로 지정 주문의
            durable Trade를 기다린다.
        인자: client_order_id -> 최초 Controller 제출에서 생성된 고정 client order ID
            timeout_seconds -> history 확정을 기다릴 최대 초
        반환값: 같은 client order ID로 저장된 terminal Trade
        작성 날짜: 2026/08/23
        """
        deadline = time.monotonic() + timeout_seconds

        # FULL 응답이 active/UNKNOWN이어도 Controller의 기존 Order query만 유한 시간 동안 깨운다.
        while time.monotonic() < deadline:
            matching_trade = next(
                (
                    trade
                    for trade in self.runtime.trade_history.trades
                    if trade.client_order_id == client_order_id
                ),
                None,
            )
            if matching_trade is not None:
                return matching_trade

            self._drain_controller_work()
            time.sleep(
                _POLL_INTERVAL_SECONDS
            )  # scheduler의 실제 due 시각 전에 busy loop하지 않는다.

        raise TimeoutError(
            "testnet order did not become durable within the bounded timeout"
        )

    def _wait_for_recent_order_result(
        self,
        trade: Trade,
        *,
        timeout_seconds: int = _ORDER_SETTLEMENT_TIMEOUT_SECONDS,
    ) -> OrderResult:
        """
        함수 이름: _wait_for_recent_order_result()
        기능: Binance recent history에서 Trade와 같은 client/exchange ID 결과를 bounded polling한다.
        인자: trade -> Controller가 durable 저장한 terminal Trade
            timeout_seconds -> 거래소 recent history 전파를 기다릴 최대 초
        반환값: 두 ID가 모두 같은 normalized OrderResult
        작성 날짜: 2026/08/23
        """
        deadline = time.monotonic() + timeout_seconds

        # raw `/allOrders` payload 대신 APIGateway의 normalized 결과에서 두 ID를 모두 비교한다.
        while time.monotonic() < deadline:
            recent_results = self.runtime.api_gateway.list_recent_order_results(
                "ETHUSDT",
                limit=100,
            )
            matching_result = next(
                (
                    result
                    for result in recent_results
                    if result.client_order_id == trade.client_order_id
                    and result.exchange_order_id == trade.order_id
                ),
                None,
            )
            if matching_result is not None:
                return matching_result

            time.sleep(_POLL_INTERVAL_SECONDS)  # 거래소 read model의 짧은 전파 지연만 기다린다.

        raise TimeoutError(
            "testnet recent history did not expose the same order IDs"
        )

    def _execute_test_buy(self) -> Trade:
        """
        함수 이름: _execute_test_buy()
        기능: 허용된 test-only action seam으로 BUY를 시작하고 Controller가 저장한 Trade를 반환한다.
        인자: 없음
        반환값: production Controller pipeline이 확정한 BUY Trade
        작성 날짜: 2026/08/23

        주의: 이 helper는 전략 시장 신호를 검증하지 않는다. 공개 market-context 연결이 없는 현재
        Phase 9에서 주문 pipeline 이후의 실제 lifecycle만 검증하기 위한 명시적 테스트 seam이다.
        """
        controller = self.runtime.trading_controller
        intent_id = f"phase9-testnet-buy-{uuid4().hex}"

        # Phase 8 통합 테스트 seam은 BUY 의도 예약과 SubmitOrder 생성 두 action에만 한정한다.
        controller._execute_action(
            patch(
                pending_strategy=StrategyType.CASE_B,
                pending_order_side=OrderSide.BUY,
                pending_order_attempt_kind=OrderAttemptKind.INITIAL,
                pending_intent_id=intent_id,
                trading_phase=TradingPhase.ENTRY_ORDER_PENDING,
            )
        )
        outcomes = controller._execute_action(
            SubmitOrder(
                strategy=StrategyType.CASE_B,
                side=OrderSide.BUY,
                attempt_kind=OrderAttemptKind.INITIAL,
                idempotency_key=intent_id,
            )
        )

        # 이후 Position, history와 pending journal은 production Controller가 소유한다.
        for outcome in outcomes:
            accepted_outcome = controller.enqueue_event(outcome)
            self.assertIsNotNone(
                accepted_outcome
            )  # 동기 terminal 결과도 공개 serial event intake로 되돌려 보낸다.
        asyncio.run(controller.drain_events())
        client_order_id = self._client_order_id_for_intent(intent_id)
        trade = self._wait_for_trade(client_order_id)
        self.assertEqual(
            client_order_id,
            self._client_order_id_for_intent(intent_id),
        )  # reconciliation 뒤에도 같은 intent가 새 client ID로 바뀌지 않았음을 확인한다.

        return trade  # terminal Trade도 최초 Controller client ID와 같은 identity를 보존한다.

    def _wait_for_session_termination(
        self,
        *,
        timeout_seconds: int = _SESSION_STOP_TIMEOUT_SECONDS,
    ) -> None:
        """
        함수 이름: _wait_for_session_termination()
        기능: force-sell의 same-order 조회와 Position-bound 잔량 retry를 유한 시간 동안 처리한다.
        인자: timeout_seconds -> TERMINATED 상태를 기다릴 최대 초
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        controller = self.runtime.trading_controller
        deadline = time.monotonic() + timeout_seconds

        # stop이 만든 terminal outcome과 3초 잔량 retry를 production scheduler 경계로만 진행한다.
        while time.monotonic() < deadline:
            asyncio.run(controller.drain_events())
            if controller.status is TradingSessionStatus.TERMINATED:
                return

            self._drain_controller_work()
            time.sleep(_POLL_INTERVAL_SECONDS)  # 실제 retry/query backoff를 건너뛰지 않는다.

        raise TimeoutError(
            "testnet force-sell did not terminate within the bounded timeout"
        )

    def _stop_session_for_cleanup(self) -> None:
        """
        함수 이름: _stop_session_for_cleanup()
        기능: assertion 실패 시에도 production stop 경로로 이번 test Position을 안전하게 정리한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        controller = self.runtime.trading_controller
        if controller.status is TradingSessionStatus.NOT_STARTED:
            # Cold restart runtime은 active session 없이 복구 Position만 가질 수 있다.
            if not controller.snapshot_session().has_open_position:
                return
            controller.liquidate_recovered_position(
                command_id=f"phase9-cleanup-recovered-{uuid4().hex}",
                expected_version=controller.context.version,
            )  # 일반 start/stop 대신 public liquidation-only Operation으로만 노출을 닫는다.
        if controller.status is TradingSessionStatus.TERMINATED:
            return

        # RUNNING에서만 새 stop command를 만들고 진행 중·reconciliation 상태에는 중복 제출하지 않는다.
        if controller.status is TradingSessionStatus.RUNNING:
            controller.stop_trading(
                command_id=f"phase9-cleanup-stop-{uuid4().hex}",
                expected_version=controller.context.version,
            )
        self._wait_for_session_termination()  # 불명 주문 위에 별도 cleanup 주문을 만들지 않는다.

    def _close_runtime_safely(self) -> None:
        """
        함수 이름: _close_runtime_safely()
        기능: 가능한 force-sell cleanup을 먼저 끝내고 application 자원을 반드시 닫는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        if self.runtime.state.status is ApplicationStatus.CLOSED:
            return

        # cleanup 실패도 account stream과 recovery worker close를 건너뛰게 하지 않는다.
        try:
            self._stop_session_for_cleanup()
        except BaseException as error:
            self._add_recovery_artifact_note(error)
            raise
        finally:
            try:
                close_application(self.runtime)
            except BaseException as error:
                self._add_recovery_artifact_note(error)
                raise  # Close 자체 실패에도 durable 복구 위치를 잃지 않는다.

    def test_production_runtime_buy_force_sell_and_close_lifecycle(self) -> None:
        """
        함수 이름: test_production_runtime_buy_force_sell_and_close_lifecycle()
        기능: startup·선택·Controller BUY·force-sell·application close의 실제
            Testnet lifecycle을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23

        주의: BUY trigger만 private action seam을 사용하므로 실제 시장 event-to-signal E2E 증거는 아니다.
        """
        lifecycle_trace: list[str] = []
        try:
            # Production bootstrap이 모든 startup 단계를 끝낸 뒤에만 READY를 게시한다.
            application_state = start_application(self.runtime)
            self.assertIs(application_state.status, ApplicationStatus.READY)
            lifecycle_trace.append("APPLICATION_READY")

            # 사용자 REGIME 선택과 split command를 optimistic version 순서로 production API에 전달한다.
            controller = self.runtime.trading_controller
            selection = self.runtime.regime_controller.set_regime_type(
                RegimeType.TYPE_0,
                command_id=f"phase9-select-{uuid4().hex}",
                expected_version=controller.context.version,
            )
            split_result = controller.update_split_ratios(
                command_id=f"phase9-split-{uuid4().hex}",
                expected_version=selection.version,
                scale_in=Decimal("1"),
                scale_out=Decimal("1"),
            )
            lifecycle_trace.append("REGIME_SELECTED")
            session_result = controller.start_trading(
                command_id=f"phase9-start-{uuid4().hex}",
                expected_version=split_result.version,
            )
            self.assertIs(session_result.status, TradingSessionStatus.RUNNING)
            lifecycle_trace.append("TRADING_RUNNING")

            # BUY 이후 cap, journal, Gateway, Position과 history는 Controller가 처리한다.
            buy_trade = self._execute_test_buy()
            self.assertIs(buy_trade.side, OrderSide.BUY)
            self.assertLessEqual(
                buy_trade.requested_quantity
                * buy_trade.market_price_at_decision,
                self.max_notional,
            )
            lifecycle_trace.append("BUY_HISTORY_DURABLE")

            # Public stop이 authoritative Position을 보고 entry cap 예외인 ForceSellAll을 시작한다.
            trade_count_before_stop = len(self.runtime.trade_history.trades)
            stop_result = controller.stop_trading(
                command_id=f"phase9-stop-{uuid4().hex}",
                expected_version=controller.context.version,
            )
            self.assertIn(
                stop_result.status,
                (
                    TradingSessionStatus.STOPPING,
                    TradingSessionStatus.TERMINATED,
                ),
            )
            lifecycle_trace.append("STOP_REQUESTED")
            self._wait_for_session_termination()
            self.assertIs(controller.status, TradingSessionStatus.TERMINATED)

            # 외부 read model 조회는 노출을 먼저 닫은 뒤 수행해 assertion 대기 중 Position을 남기지 않는다.
            buy_result = self._wait_for_recent_order_result(buy_trade)
            self.assertIs(buy_result.status, OrderStatus.FILLED)

            # 가격 이동과 partial retry가 있어도 이번 stop은 BUY로 얻은 Position만 정확히 닫는다.
            stop_trades = self.runtime.trade_history.trades[
                trade_count_before_stop:
            ]
            self.assertTrue(stop_trades)
            self.assertTrue(
                all(trade.side is OrderSide.SELL for trade in stop_trades)
            )
            self.assertEqual(
                sum(
                    (
                        sell_trade.executed_quantity
                        for sell_trade in stop_trades
                    ),
                    Decimal("0"),
                ),
                buy_trade.executed_quantity,
            )
            for sell_trade in stop_trades:
                sell_result = self._wait_for_recent_order_result(sell_trade)
                self.assertIs(sell_result.status, OrderStatus.FILLED)
            lifecycle_trace.append("FORCE_SELL_HISTORY_DURABLE")

            # Controller 완료 뒤 local pending과 같은 test ID의 Binance open order가 모두 없어야 한다.
            all_test_trades = self.runtime.trade_history.trades
            test_client_order_ids = {
                trade.client_order_id for trade in all_test_trades
            }
            self.assertEqual(
                self.runtime.trade_history_controller.get_pending_orders(),
                (),
            )
            remaining_open_results = (
                self.runtime.api_gateway.list_open_order_results("ETHUSDT")
            )
            self.assertFalse(
                test_client_order_ids
                & {
                    result.client_order_id
                    for result in remaining_open_results
                }
            )
            position = controller.position
            self.assertIsNotNone(position)
            self.assertEqual(Decimal("0"), position.quantity)

            # Published history와 repository replay가 같은 주문 ID 순서를 보존하는지 확인한다.
            persisted_trades = (
                self.runtime.trade_history_repository.get_trade_history()
            )
            self.assertEqual(
                tuple(trade.order_id for trade in all_test_trades),
                tuple(trade.order_id for trade in persisted_trades),
            )

            # 정상 경로에서도 close_application을 명시 호출해 account/recovery 자원 종료를 검증한다.
            closed_state = close_application(self.runtime)
            self.assertIs(closed_state.status, ApplicationStatus.CLOSED)
            lifecycle_trace.append("APPLICATION_CLOSED")
            self.assertEqual(
                tuple(lifecycle_trace),
                (
                    "APPLICATION_READY",
                    "REGIME_SELECTED",
                    "TRADING_RUNNING",
                    "BUY_HISTORY_DURABLE",
                    "STOP_REQUESTED",
                    "FORCE_SELL_HISTORY_DURABLE",
                    "APPLICATION_CLOSED",
                ),
            )
            self.assertTrue(
                self.history_path.exists()
            )  # 성공 증거도 실제 restart 검증 전까지 자동 삭제하지 않는다.
        except BaseException as error:
            self._add_recovery_artifact_note(error)
            raise
        finally:
            self._close_runtime_safely()  # 어느 assertion에서 실패해도 중복 주문 없이 stop 후 close한다.


if __name__ == "__main__":
    unittest.main()
