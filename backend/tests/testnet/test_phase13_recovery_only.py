"""실패한 Session 3의 단일 open BUY를 신규 BUY 없이 exact STOP SELL로 청산한다."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
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
    BINANCE_TESTNET_MAX_NOTIONAL_ENV,
    create_testnet_application_runtime,
    load_testnet_configuration,
    require_phase13_recovery_only_permission,
)
from binance_auto_trader.domain.trading.states import ExitReason, OrderSide

from tests.testnet._support import (
    PHASE13_RECOVERY_ONLY_REQUESTED,
    PHASE13_RECOVERY_ONLY_SKIP_REASON,
    require_empty_all_client_open_orders,
    seed_verified_open_recovery_history,
    verify_exact_recent_order_baseline,
)
from tests.testnet.test_phase13_public_market_case2 import (
    _PHASE13_PROCESS_LEASE_PATH,
    _acquire_phase13_process_lease,
    _publish_new_artifact_bytes,
    _release_phase13_process_lease,
)


# Recovery-only target은 기존 actual과 같은 lease를 쓰고 한 번의 exact SELL을 90초 안에 끝낸다.
_LIQUIDATION_TIMEOUT_SECONDS = 90
_POLL_INTERVAL_SECONDS = 0.10
_ARTIFACT_ROOT = Path(__file__).resolve().parents[2] / ".testnet-artifacts"
_RECOVERY_ONLY_REQUESTED = (
    PHASE13_RECOVERY_ONLY_REQUESTED
    and os.environ.get(BINANCE_TESTNET_MAX_NOTIONAL_ENV) == "10"
)


@unittest.skipUnless(
    _RECOVERY_ONLY_REQUESTED,
    PHASE13_RECOVERY_ONLY_SKIP_REASON,
)
class BinanceTestnetPhaseThirteenRecoveryOnlyTests(unittest.TestCase):
    """
    클래스 이름: BinanceTestnetPhaseThirteenRecoveryOnlyTests
    기능: pin된 단일 Case C BUY를 recovery-only permit의 STOP SELL 한 번으로 닫는다.
    작성 날짜: 2026/09/04
    """

    def setUp(self) -> None:
        """
        함수 이름: setUp()
        기능: 전용 opt-in, 공용 process lease와 open recovery baseline을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/04
        """
        self.configuration = load_testnet_configuration()
        self.maximum_notional = require_phase13_recovery_only_permission(
            self.configuration
        )
        if self.maximum_notional != Decimal("10"):
            raise AssertionError("Phase 13 recovery requires the fixed cap")

        # 다른 actual process보다 먼저 같은 lease를 잡아 account mutation을 직렬화한다.
        _ARTIFACT_ROOT.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.process_lease_descriptor = _acquire_phase13_process_lease(
            _PHASE13_PROCESS_LEASE_PATH
        )
        self.addCleanup(
            _release_phase13_process_lease,
            self.process_lease_descriptor,
        )  # Recovery와 fresh verification이 모두 끝날 때까지 lease를 유지한다.
        started_at = datetime.now(timezone.utc)
        run_id = uuid4()
        self.started_at = started_at
        self.run_id = str(run_id)
        self.artifact_directory = _ARTIFACT_ROOT / (
            "phase13-recovery-only-"
            f"{started_at.strftime('%Y%m%dT%H%M%S%fZ')}-{run_id.hex}"
        )
        self.artifact_directory.mkdir(mode=0o700)
        os.chmod(self.artifact_directory, 0o700)  # Artifact owner 권한을 umask와 무관하게 고정한다.
        self.history_path = self.artifact_directory / "history.jsonl"
        self.baseline_trades = seed_verified_open_recovery_history(
            self.history_path
        )
        self.open_buy = self.baseline_trades[-1]
        self.active_runtime = None

    def tearDown(self) -> None:
        """
        함수 이름: tearDown()
        기능: 실패 시 미사용 recovery permit 한 번만 안전 cleanup에 쓰고 runtime을 닫는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/04
        """
        runtime = self.active_runtime
        if runtime is None:
            return

        # 아직 submit이 없고 state가 exact하면 동일 public recovery Operation을 cleanup으로 한 번만 시도한다.
        try:
            guard = runtime.api_gateway.get_phase13_order_submission_guard_snapshot()
            controller = runtime.trading_controller
            position = controller.position
            if (
                not guard.mutation_started
                and position is not None
                and position.quantity > Decimal("0")
                and not controller.reconciliation_required
                and controller.status is TradingSessionStatus.NOT_STARTED
            ):
                snapshot = controller.snapshot_session()
                controller.liquidate_recovered_position(
                    command_id=f"phase13-recovery-cleanup-{self.run_id}",
                    expected_version=snapshot.version,
                )
                self._wait_for_liquidation_termination(runtime)
        except Exception:
            pass  # 실패 원형과 preserved artifact를 cleanup 예외로 덮지 않는다.
        finally:
            if runtime.state.status is not ApplicationStatus.CLOSED:
                close_application(runtime)
            self.active_runtime = None

    def _wait_for_liquidation_termination(self, runtime: object) -> None:
        """
        함수 이름: _wait_for_liquidation_termination()
        기능: production worker가 STOP SELL outcome을 TERMINATED로 게시할 때까지 bounded 대기한다.
        인자: runtime -> recovery-only production ApplicationRuntime
        반환값: Position 0과 TERMINATED가 함께 관찰되면 없음
        작성 날짜: 2026/09/04
        """
        deadline = time.monotonic() + _LIQUIDATION_TIMEOUT_SECONDS

        # Test가 queue를 직접 drain하지 않고 production event worker의 실제 경계를 관찰한다.
        while time.monotonic() < deadline:
            controller = runtime.trading_controller
            if controller.reconciliation_required:
                raise AssertionError(
                    "Phase 13 recovery entered reconciliation"
                )
            if controller.status is TradingSessionStatus.TERMINATED:
                position = controller.position
                if position is None or position.quantity != Decimal("0"):
                    raise AssertionError(
                        "Phase 13 recovery terminated with exposure"
                    )
                return
            time.sleep(_POLL_INTERVAL_SECONDS)  # 실제 same-ID worker deadline을 앞당기지 않는다.

        raise TimeoutError("Phase 13 recovery did not terminate in time")

    def _verify_fresh_zero_exposure(self, expected_trades: tuple) -> int:
        """
        함수 이름: _verify_fresh_zero_exposure()
        기능: 주문을 만들지 않는 fresh runtime에서 history·Position·account-wide open 0을 검증한다.
        인자: expected_trades -> BUY와 recovery SELL을 포함한 durable Trade tuple
        반환값: exact recent exchange order 개수
        작성 날짜: 2026/09/04
        """
        fresh_runtime = create_testnet_application_runtime(
            history_path=self.history_path
        )
        try:
            fresh_state = start_application(fresh_runtime)
            if fresh_state.status is not ApplicationStatus.READY:
                raise AssertionError("fresh recovery runtime did not become READY")
            controller = fresh_runtime.trading_controller
            position = controller.position
            if position is None or position.quantity != Decimal("0"):
                raise AssertionError("fresh recovery verification found exposure")
            if controller.reconciliation_required:
                raise AssertionError(
                    "fresh recovery verification requires no reconciliation"
                )
            if fresh_runtime.trade_history.trades != expected_trades:
                raise AssertionError("fresh recovery history replay changed")
            if fresh_runtime.trade_history_controller.get_pending_orders():
                raise AssertionError("fresh recovery verification found pending orders")
            if fresh_runtime.api_gateway.has_any_exchange_open_orders():
                raise AssertionError("fresh recovery verification found open orders")
            if fresh_runtime.api_gateway.has_any_exchange_open_order_lists():
                raise AssertionError(
                    "fresh recovery verification found open order lists"
                )
            recent_results = fresh_runtime.api_gateway.list_all_recent_order_results(
                "ETHUSDT",
                limit=1000,
            )
            verify_exact_recent_order_baseline(expected_trades, recent_results)
            return len(recent_results)  # ID 원문 없이 count만 success evidence에 전달한다.
        finally:
            close_application(fresh_runtime)

    def test_exact_stop_sell_closes_preserved_phase13_buy(self) -> None:
        """
        함수 이름: test_exact_stop_sell_closes_preserved_phase13_buy()
        기능: 신규 BUY·cancel 없이 recovery-only STOP SELL 1회와 fresh zero exposure를 증명한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/04
        """
        runtime = create_testnet_application_runtime(history_path=self.history_path)
        self.active_runtime = runtime
        ready_state = start_application(runtime)
        self.assertIs(ready_state.status, ApplicationStatus.READY)
        controller = runtime.trading_controller
        position = controller.position
        self.assertIsNotNone(position)
        self.assertEqual(self.open_buy.executed_quantity, position.quantity)
        self.assertIs(controller.status, TradingSessionStatus.NOT_STARTED)
        self.assertFalse(controller.reconciliation_required)
        self.assertEqual((), runtime.trade_history_controller.get_pending_orders())

        # Exchange recent/open과 실제 free ETH가 pin된 durable Position을 설명할 때만 SELL을 허용한다.
        open_results = runtime.api_gateway.list_all_open_order_results("ETHUSDT")
        require_empty_all_client_open_orders(open_results)
        self.assertFalse(runtime.api_gateway.has_any_exchange_open_orders())
        self.assertFalse(runtime.api_gateway.has_any_exchange_open_order_lists())
        recent_results = runtime.api_gateway.list_all_recent_order_results(
            "ETHUSDT",
            limit=1000,
        )
        verify_exact_recent_order_baseline(self.baseline_trades, recent_results)
        eth_balance = runtime.account.balances.get("ETH")
        if eth_balance is None or eth_balance.free < position.quantity:
            raise AssertionError(
                "Phase 13 recovery requires enough authoritative free ETH"
            )

        # NOT_STARTED snapshot의 public liquidation Operation만 호출해 strategy BUY path를 열지 않는다.
        recovered_quantity = position.quantity
        session_snapshot = controller.snapshot_session()
        liquidation_result = controller.liquidate_recovered_position(
            command_id=f"phase13-recovery-only-{self.run_id}",
            expected_version=session_snapshot.version,
        )
        self.assertIn(
            liquidation_result.status,
            {TradingSessionStatus.STOPPING, TradingSessionStatus.TERMINATED},
        )
        self._wait_for_liquidation_termination(runtime)

        # Permission proxy와 durable history가 SELL 1회·STOP·exact Position 수량을 독립 증명한다.
        guard = runtime.api_gateway.get_phase13_order_submission_guard_snapshot()
        self.assertTrue(guard.submissions_blocked)
        self.assertEqual(1, len(guard.attempts))
        self.assertIs(guard.attempts[0].side, OrderSide.SELL)
        run_trades = runtime.trade_history.trades[len(self.baseline_trades) :]
        self.assertEqual(1, len(run_trades))
        recovery_sell = run_trades[0]
        self.assertIs(recovery_sell.side, OrderSide.SELL)
        self.assertIs(recovery_sell.exit_reason, ExitReason.STOP)
        self.assertEqual(recovered_quantity, recovery_sell.executed_quantity)
        self.assertEqual(Decimal("0"), position.quantity)
        self.assertEqual((), runtime.trade_history_controller.get_pending_orders())

        # 첫 runtime을 닫은 뒤 같은 source history를 fresh process 조립으로 다시 검증한다.
        closed_state = close_application(runtime)
        self.assertIs(closed_state.status, ApplicationStatus.CLOSED)
        self.active_runtime = None
        durable_trades = runtime.trade_history.trades
        fresh_recent_count = self._verify_fresh_zero_exposure(durable_trades)

        # Secret과 raw order ID가 없는 canonical success evidence만 owner-only artifact로 봉인한다.
        completed_at = datetime.now(timezone.utc)
        evidence_body = {
            "schema_version": 1,
            "record_type": "phase13_recovery_only_success",
            "outcome": "SUCCESS",
            "run_id": self.run_id,
            "timestamps": {
                "started_at": self.started_at.isoformat().replace("+00:00", "Z"),
                "completed_at": completed_at.isoformat().replace("+00:00", "Z"),
            },
            "mutation": {
                "buy_count": 0,
                "stop_sell_count": 1,
                "cancel_count": 0,
            },
            "recovery": {
                "authoritative_quantity": str(recovered_quantity),
                "executed_quantity": str(recovery_sell.executed_quantity),
                "final_position_quantity": "0",
            },
            "fresh_verification": {
                "pending_order_count": 0,
                "account_open_orders_empty": True,
                "account_open_order_lists_empty": True,
                "recent_order_count": fresh_recent_count,
                "reconciliation_required": False,
            },
        }
        canonical_bytes = (
            json.dumps(
                evidence_body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        evidence_path = _publish_new_artifact_bytes(
            self.artifact_directory,
            "phase13-recovery-only-success.json",
            canonical_bytes,
        )
        self.assertEqual(
            hashlib.sha256(canonical_bytes).hexdigest(),
            hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        )  # Published evidence가 검증한 canonical bytes와 동일함을 마지막에 대조한다.


if __name__ == "__main__":
    unittest.main()
