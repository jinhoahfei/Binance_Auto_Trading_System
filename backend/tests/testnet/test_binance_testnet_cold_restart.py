"""Production Testnet process 종료 뒤 open Position의 공개 인수·청산을 검증한다."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch as mock_patch
from uuid import uuid4

from binance_auto_trader.adapters.persistence import TradeHistoryRepository
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
    require_testnet_order_permission,
)
from binance_auto_trader.domain.trading.states import OrderSide
from binance_auto_trader.domain.trading.stm import TradingSTM

from tests.testnet._support import (
    ORDER_SKIP_REASON,
    ORDER_TESTNET_REQUESTED,
    seed_verified_closed_history,
)
from tests.testnet._cold_restart_process import (
    _require_supported_commission_accounting as require_child_commission_accounting,
)


# 별도 process와 세 번의 production startup을 포함하되 무한 대기나 암묵 재제출은 허용하지 않는다.
_PROCESS_A_TIMEOUT_SECONDS = 180
_LIQUIDATION_TIMEOUT_SECONDS = 90
_POLL_INTERVAL_SECONDS = 0.25
_RECEIPT_SCHEMA_VERSION = 1
_ARTIFACT_ROOT = Path(__file__).resolve().parents[2] / ".testnet-artifacts"
_ACCOUNTING_SUPPORTED_FEE_ASSETS = frozenset({"ETH", "USDT"})
_ORDER_CAP_PRESENT = bool(
    os.environ.get(BINANCE_TESTNET_MAX_NOTIONAL_ENV)
)  # Credential과 주문 opt-in 외에 명시적 BUY quote 진입 cap이 없으면 process를 만들지 않는다.
_COLD_RESTART_TESTNET_REQUESTED = (
    ORDER_TESTNET_REQUESTED and _ORDER_CAP_PRESENT
)
_COLD_RESTART_SKIP_REASON = (
    f"{ORDER_SKIP_REASON}; {BINANCE_TESTNET_MAX_NOTIONAL_ENV} is also required"
)


class ColdRestartCommissionPreflightTests(unittest.TestCase):
    """
    클래스 이름: ColdRestartCommissionPreflightTests
    기능: 실제 network 없이 process A와 parent B의 제3 수수료 자산 gate를 검증한다.
    작성 날짜: 2026/08/24
    """

    def test_child_and_parent_reject_enabled_third_asset_discount(self) -> None:
        """
        함수 이름: test_child_and_parent_reject_enabled_third_asset_discount()
        기능: BNB discount 가능 정책이 BUY와 recovery SELL 전에 모두 거부되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # Secret이나 REST payload 대신 APIGateway의 normalized policy만 fake로 주입한다.
        third_asset_policy = Mock(
            can_charge_discount_asset=True,
            discount_asset="BNB",
            market_buy_received_asset_commission_rate=Decimal("0"),
        )
        runtime = Mock()
        runtime.api_gateway.fetch_commission_discount_policy.return_value = (
            third_asset_policy
        )
        parent_harness = BinanceTestnetColdRestartTests(
            "test_fresh_runtime_liquidates_process_a_position_without_resume"
        )

        with self.assertRaisesRegex(RuntimeError, "unsupported fee asset"):
            require_child_commission_accounting(runtime)
        with self.assertRaisesRegex(RuntimeError, "unsupported fee asset"):
            parent_harness._require_supported_recovery_commission_accounting(
                runtime
            )
        self.assertEqual(
            2,
            runtime.api_gateway.fetch_commission_discount_policy.call_count,
        )  # 두 process owner가 서로의 과거 preflight를 신뢰하지 않고 각각 재확인한다.

    def test_child_rejects_buy_fee_while_parent_allows_recovery_sell(
        self,
    ) -> None:
        """
        함수 이름: test_child_rejects_buy_fee_while_parent_allows_recovery_sell()
        기능: 수신 ETH 수수료율은 신규 BUY만 차단하고 이미 열린 Position의 recovery SELL은 허용한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # Discount가 꺼져도 신규 BUY의 taker+buyer 수수료율은 base-asset dust를 만들 수 있다.
        base_asset_fee_policy = Mock(
            can_charge_discount_asset=False,
            discount_asset="BNB",
            market_buy_received_asset_commission_rate=Decimal("0.001"),
        )
        runtime = Mock()
        runtime.api_gateway.fetch_commission_discount_policy.return_value = (
            base_asset_fee_policy
        )
        parent_harness = BinanceTestnetColdRestartTests(
            "test_fresh_runtime_liquidates_process_a_position_without_resume"
        )

        with self.assertRaisesRegex(RuntimeError, "base-asset dust"):
            require_child_commission_accounting(runtime)
        parent_harness._require_supported_recovery_commission_accounting(
            runtime
        )
        self.assertEqual(
            2,
            runtime.api_gateway.fetch_commission_discount_policy.call_count,
        )  # Parent는 과거 BUY 정책 변화로 유일한 exposure-reducing SELL을 막지 않는다.


@unittest.skipUnless(
    _COLD_RESTART_TESTNET_REQUESTED,
    _COLD_RESTART_SKIP_REASON,
)
class BinanceTestnetColdRestartTests(unittest.TestCase):
    """
    클래스 이름: BinanceTestnetColdRestartTests
    기능: process A의 capped BUY를 fresh runtime B가 자동 resume 없이 인수·청산하고
        세 번째 runtime이 Position 0을 재생하는지 실제 Spot Testnet에서 검증한다.
    작성 날짜: 2026/08/24

    주의: 실제 주문은 기존 Testnet 이중 opt-in과 양수 max-notional이 모두 있을 때만 실행된다.
    Child stdout/stderr는 OS 레벨에서 폐기하며 artifact에는 normalized 주문 ID만 보존한다.
    """

    def setUp(self) -> None:
        """
        함수 이름: setUp()
        기능: production 설정과 cap을 재검증하고 run별 durable artifact 경로를 만든다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # 잘못된 non-empty cap도 child 전에 production loader가 fail closed하도록 검증한다.
        self.configuration = load_testnet_configuration()
        self.max_notional = require_testnet_order_permission(
            self.configuration
        )
        run_timestamp = datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S%fZ"
        )
        self.artifact_directory = _ARTIFACT_ROOT / (
            f"phase9-cold-restart-{run_timestamp}-{uuid4().hex}"
        )
        self.artifact_directory.mkdir(
            parents=True,
            mode=0o700,
        )
        self.history_path = self.artifact_directory / "history.jsonl"
        self.receipt_path = self.artifact_directory / "process-a-receipt.json"
        self.baseline_trades = seed_verified_closed_history(
            self.history_path
        )  # 같은 Testnet account의 완료 주문을 Position 0 baseline으로 먼저 고정한다.
        self.baseline_trade_count = len(self.baseline_trades)
        self.active_runtime: object | None = None

    def _build_child_environment(self) -> dict[str, str]:
        """
        함수 이름: _build_child_environment()
        기능: credential 값을 출력하지 않고 process A가 같은 backend package를 import할 환경을 만든다.
        인자: 없음
        반환값: parent 환경과 명시적 backend Python path를 가진 child mapping
        작성 날짜: 2026/08/24
        """
        # Secret은 inherited environment에만 남기고 command argument나 receipt에는 복사하지 않는다.
        child_environment = dict(os.environ)
        backend_root = Path(__file__).resolve().parents[2]
        source_root = backend_root / "src"
        python_paths = [str(source_root), str(backend_root)]
        existing_python_path = child_environment.get("PYTHONPATH")
        if existing_python_path:
            python_paths.append(existing_python_path)
        child_environment["PYTHONPATH"] = os.pathsep.join(python_paths)

        return child_environment  # subprocess API에만 전달하고 assertion이나 예외에는 포함하지 않는다.

    def _run_process_a(self) -> None:
        """
        함수 이름: _run_process_a()
        기능: 별도 Python owner가 durable BUY를 남기고 normal close 없이 끝나도록 실행한다.
        인자: 없음
        반환값: process A가 성공 exit하고 success receipt를 남기면 없음
        작성 날짜: 2026/08/24
        """
        backend_root = Path(__file__).resolve().parents[2]
        child_command = (
            sys.executable,
            "-m",
            "tests.testnet._cold_restart_process",
            str(self.history_path),
            str(self.receipt_path),
        )

        # stdin/stdout/stderr를 모두 닫아 secret이나 Binance 응답 원문이 parent log에 섞이지 않게 한다.
        try:
            completed_process = subprocess.run(
                child_command,
                cwd=backend_root,
                env=self._build_child_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                check=False,
                timeout=_PROCESS_A_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            raise TimeoutError(
                "process A exceeded the bounded cold-restart timeout"
            ) from None  # TimeoutExpired의 captured stream이나 command repr는 노출하지 않는다.

        receipt = self._read_process_a_receipt()
        if completed_process.returncode != 0:
            failure_type = receipt.get("failure_type", "UnknownChildFailure")
            raise RuntimeError(
                f"process A failed before handoff: {failure_type}"
            )  # 외부 예외 메시지 대신 child가 기록한 class 이름만 진단한다.
        self._require_success_receipt(receipt)

    def _read_process_a_receipt(self) -> dict[str, object]:
        """
        함수 이름: _read_process_a_receipt()
        기능: process A의 local normalized receipt를 strict JSON object로 읽는다.
        인자: 없음
        반환값: 허용된 scalar field만 가진 receipt dictionary
        작성 날짜: 2026/08/24
        """
        # 이 파일은 Binance payload가 아니라 child가 생성한 네 개 이하의 normalized scalar다.
        try:
            decoded_receipt = json.loads(
                self.receipt_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise RuntimeError("process A did not leave a valid local receipt") from None
        if not isinstance(decoded_receipt, dict):
            raise RuntimeError("process A receipt must be a JSON object")
        allowed_keys = {
            "schema_version",
            "status",
            "client_order_id",
            "order_id",
            "failure_type",
        }
        if not set(decoded_receipt).issubset(allowed_keys):
            raise RuntimeError("process A receipt contains an unsupported field")
        if decoded_receipt.get("schema_version") != _RECEIPT_SCHEMA_VERSION:
            raise RuntimeError("process A receipt schema is unsupported")

        return decoded_receipt  # raw response body를 허용하지 않는 local handoff만 반환한다.

    def _require_success_receipt(self, receipt: dict[str, object]) -> None:
        """
        함수 이름: _require_success_receipt()
        기능: process A receipt가 정확한 durable BUY handoff 필드를 가졌는지 검증한다.
        인자: receipt -> strict local receipt dictionary
        반환값: 성공 schema이면 없음
        작성 날짜: 2026/08/24
        """
        # 성공 receipt는 상태와 두 normalized ID 외의 failure 정보를 포함할 수 없다.
        expected_keys = {
            "schema_version",
            "status",
            "client_order_id",
            "order_id",
        }
        if set(receipt) != expected_keys:
            raise RuntimeError("process A success receipt fields are incomplete")
        if receipt["status"] != "BUY_HISTORY_DURABLE":
            raise RuntimeError("process A did not confirm durable BUY history")
        client_order_id = receipt["client_order_id"]
        order_id = receipt["order_id"]
        if not isinstance(client_order_id, str) or not client_order_id.startswith(
            "bat-"
        ):
            raise RuntimeError("process A client order ID is invalid")
        if not isinstance(order_id, str) or not order_id.isdecimal():
            raise RuntimeError("process A exchange order ID is invalid")

    def _create_and_start_fresh_runtime(self) -> object:
        """
        함수 이름: _create_and_start_fresh_runtime()
        기능: 같은 artifact history로 새 production Testnet runtime을 만들고 READY까지 시작한다.
        인자: 없음
        반환값: startup reconciliation을 완료한 fresh ApplicationRuntime
        작성 날짜: 2026/08/24
        """
        # Factory에는 credential이 아닌 path만 전달하고 실제 설정은 고정 Testnet loader가 읽는다.
        runtime = create_testnet_application_runtime(
            history_path=self.history_path,
        )
        application_state = start_application(runtime)
        if application_state.status is not ApplicationStatus.READY:
            raise RuntimeError("fresh Testnet runtime did not become READY")

        return runtime  # caller가 public recovery 또는 close lifetime을 소유한다.

    def _require_supported_recovery_commission_accounting(
        self,
        runtime: object,
    ) -> None:
        """
        함수 이름: _require_supported_recovery_commission_accounting()
        기능: recovery SELL이 지원하지 않는 제3 자산 수수료를 만들 수 있으면 제출 전에 차단한다.
        인자: runtime -> READY production Testnet ApplicationRuntime
        반환값: recovery SELL 수수료를 ETH 또는 USDT로 회계할 수 있으면 없음
        작성 날짜: 2026/08/24
        """
        # 공식 account commission 응답은 recovery 시점에도 secret-safe policy로 다시 읽는다.
        commission_policy = (
            runtime.api_gateway.fetch_commission_discount_policy("ETHUSDT")
        )
        if (
            commission_policy.can_charge_discount_asset
            and commission_policy.discount_asset
            not in _ACCOUNTING_SUPPORTED_FEE_ASSETS
        ):
            raise RuntimeError(
                "recovery commission policy permits an unsupported fee asset"
            )  # BNB 등 제3 자산 fill이 durable history를 막기 전에 SELL mutation을 차단한다.

    def _wait_for_liquidation_termination(self, runtime: object) -> None:
        """
        함수 이름: _wait_for_liquidation_termination()
        기능: recovery SELL의 queue, same-ID query와 residual retry를 bounded public 경계로 진행한다.
        인자: runtime -> liquidation-only session을 소유한 production runtime
        반환값: Controller가 TERMINATED와 Position 0에 도달하면 없음
        작성 날짜: 2026/08/24
        """
        controller = runtime.trading_controller
        deadline = time.monotonic() + _LIQUIDATION_TIMEOUT_SECONDS

        # 즉시 outcome drain과 due same-ID/retry trigger를 분리해 재제출 판단을 Controller에 맡긴다.
        while time.monotonic() < deadline:
            asyncio.run(controller.drain_events())
            if controller.status is TradingSessionStatus.TERMINATED:
                position = controller.position
                if position is None or position.quantity != Decimal("0"):
                    raise RuntimeError(
                        "terminated recovery session still has an open Position"
                    )
                return

            controller.trigger_order_reconciliation(
                occurred_at=datetime.now(timezone.utc),
            )
            asyncio.run(controller.drain_events())
            time.sleep(
                _POLL_INTERVAL_SECONDS
            )  # Query와 force-sell retry의 production due 시각을 건너뛰지 않는다.

        raise TimeoutError("recovered-position liquidation did not terminate in time")

    def _assert_same_id_liquidation_trace(self, runtime: object) -> None:
        """
        함수 이름: _assert_same_id_liquidation_trace()
        기능: 각 recovery SELL attempt가 한 Gateway submit과 동일 ID query만 사용했는지 검증한다.
        인자: runtime -> 종료된 liquidation-only production runtime
        반환값: same-ID와 단일 submit 불변식이 성립하면 없음
        작성 날짜: 2026/08/24
        """
        # 메시지 6은 Gateway mutation 경계이며 residual retry마다 새 ID를 정확히 한 번만 허용한다.
        trace_entries = runtime.trading_controller.order_execution_trace
        submission_entries = tuple(
            trace for trace in trace_entries if trace.message_id == "6"
        )
        self.assertTrue(submission_entries)
        submitted_client_order_ids = tuple(
            trace.client_order_id for trace in submission_entries
        )
        self.assertEqual(
            len(submitted_client_order_ids),
            len(set(submitted_client_order_ids)),
        )  # accepted-response timeout도 같은 ID query로만 이어져 두 번째 POST가 없어야 한다.

        # 메시지 6.1과 7/8/9 계열은 자신을 만든 submit attempt의 client ID를 바꿀 수 없다.
        submitted_id_set = set(submitted_client_order_ids)
        order_boundary_message_ids = {
            "6.1",
            "7",
            "8",
            "8.1",
            "8.2",
            "9",
            "10",
            "11",
            "12",
            "13",
            "13.1",
            "13.2",
            "13.3",
            "13.4",
            "13.5",
            "13.5.1",
            "14",
        }
        for trace in trace_entries:
            if trace.message_id not in order_boundary_message_ids:
                continue
            self.assertIn(trace.client_order_id, submitted_id_set)
        for client_order_id in submitted_client_order_ids:
            self.assertEqual(
                1,
                sum(
                    trace.message_id == "6.1"
                    and trace.client_order_id == client_order_id
                    for trace in trace_entries
                ),
            )  # Gateway return 단계도 같은 attempt마다 정확히 한 번만 기록한다.

    def _liquidate_runtime_for_cleanup(self, runtime: object) -> None:
        """
        함수 이름: _liquidate_runtime_for_cleanup()
        기능: 실패 중 READY runtime의 recovered open Position을 public recovery Operation으로 닫는다.
        인자: runtime -> 같은 artifact를 소유한 production runtime
        반환값: Position이 없거나 public 청산이 TERMINATED에 도달하면 없음
        작성 날짜: 2026/08/24
        """
        controller = runtime.trading_controller
        session_snapshot = controller.snapshot_session()
        if (
            not session_snapshot.has_open_position
            and controller.status is not TradingSessionStatus.STOPPING
        ):
            return

        # Fresh recovery는 NOT_STARTED에서만 새 liquidation intent를 만들고 진행 중이면 drain한다.
        if controller.status is TradingSessionStatus.NOT_STARTED:
            self._require_supported_recovery_commission_accounting(runtime)
            controller.liquidate_recovered_position(
                command_id=f"phase9-cold-cleanup-{uuid4().hex}",
                expected_version=session_snapshot.version,
            )
        elif controller.status not in (
            TradingSessionStatus.STOPPING,
            TradingSessionStatus.RECONCILIATION_REQUIRED,
        ):
            raise RuntimeError(
                "cleanup found an incompatible recovered-position session"
            )
        self._wait_for_liquidation_termination(runtime)

    def _cleanup_after_failure(self) -> None:
        """
        함수 이름: _cleanup_after_failure()
        기능: active 또는 fresh runtime으로 durable exposure를 복구 청산하고 모든 자원을 닫는다.
        인자: 없음
        반환값: 노출이 없거나 public recovery cleanup이 성공하면 없음
        작성 날짜: 2026/08/24
        """
        cleanup_failure_types: list[str] = []
        candidate_runtime = self.active_runtime
        self.active_runtime = None

        # 이미 READY인 B runtime을 먼저 사용해 in-flight same-ID query와 retry state를 보존한다.
        if candidate_runtime is not None:
            try:
                if candidate_runtime.state.status is ApplicationStatus.READY:
                    self._liquidate_runtime_for_cleanup(candidate_runtime)
                    close_application(candidate_runtime)
                    return
            except BaseException as error:
                cleanup_failure_types.append(type(error).__name__)
            finally:
                try:
                    close_application(candidate_runtime)
                except BaseException as error:
                    cleanup_failure_types.append(type(error).__name__)

        # Process A가 BUY 뒤 실패했을 수 있으므로 같은 history의 새 owner에서 한 번 더 인수한다.
        cleanup_runtime: object | None = None
        try:
            cleanup_runtime = self._create_and_start_fresh_runtime()
            self._liquidate_runtime_for_cleanup(cleanup_runtime)
        except BaseException as error:
            cleanup_failure_types.append(type(error).__name__)
        finally:
            if cleanup_runtime is not None:
                try:
                    close_application(cleanup_runtime)
                except BaseException as error:
                    cleanup_failure_types.append(type(error).__name__)

        if cleanup_failure_types:
            failure_summary = ",".join(cleanup_failure_types)
            raise RuntimeError(
                f"cold-restart cleanup failed with: {failure_summary}"
            )  # 외부 예외 메시지나 raw payload 대신 class 이름만 반환한다.

    def _add_artifact_note(self, error: BaseException) -> None:
        """
        함수 이름: _add_artifact_note()
        기능: 실패 예외에 secret 없는 artifact 경로와 보존된 client order ID를 덧붙인다.
        인자: error -> test 또는 cleanup에서 발생한 예외
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        client_order_ids: set[str] = set()

        # Receipt와 canonical repository만 읽어 raw JSONL line이나 Binance payload는 노출하지 않는다.
        try:
            receipt = self._read_process_a_receipt()
            receipt_client_order_id = receipt.get("client_order_id")
            if isinstance(receipt_client_order_id, str):
                client_order_ids.add(receipt_client_order_id)
        except BaseException:
            pass
        try:
            repository = TradeHistoryRepository(self.history_path)
            client_order_ids.update(
                trade.client_order_id for trade in repository.get_trade_history()
            )
            client_order_ids.update(
                record.order.client_order_id
                for record in repository.get_pending_order_recovery_records()
            )  # BUY response가 불명이어도 PREPARED sidecar의 same-ID 복구 identity를 보존한다.
        except BaseException:
            pass
        if self.active_runtime is not None:
            client_order_ids.update(
                trace.client_order_id
                for trace in self.active_runtime.trading_controller.order_execution_trace
            )  # Parent B의 residual SELL ID도 cleanup 실패 진단에서 삭제하지 않는다.
        identity_text = (
            ",".join(sorted(client_order_ids))
            if client_order_ids
            else "none-observed"
        )
        error.add_note(
            "Phase 9 cold-restart artifacts preserved at "
            f"{self.artifact_directory}; client_order_ids={identity_text}"
        )  # 자동 삭제하지 않는 경로로 다음 same-ID recovery에 필요한 identity를 남긴다.

    def test_fresh_runtime_liquidates_process_a_position_without_resume(
        self,
    ) -> None:
        """
        함수 이름: test_fresh_runtime_liquidates_process_a_position_without_resume()
        기능: 실제 process A BUY를 runtime B가 신규 BUY 없이 복구 청산하고 세 번째 replay를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        primary_error: BaseException | None = None
        try:
            # Process A는 durable BUY receipt 직후 close_application 없이 os._exit로 owner를 끝낸다.
            self._run_process_a()
            receipt = self._read_process_a_receipt()
            self._require_success_receipt(receipt)

            # Runtime B startup은 history와 recent execution을 재조정하되 어떤 신규 submit도 하지 않는다.
            recovered_runtime = self._create_and_start_fresh_runtime()
            self.active_runtime = recovered_runtime
            recovered_controller = recovered_runtime.trading_controller
            recovered_snapshot = recovered_controller.snapshot_session()
            recovered_position = recovered_controller.position
            recovered_trades = recovered_runtime.trade_history.trades
            self.assertIs(
                recovered_controller.status,
                TradingSessionStatus.NOT_STARTED,
            )
            self.assertTrue(recovered_snapshot.has_open_position)
            self.assertFalse(recovered_controller.context.initialized)
            self.assertIsNotNone(recovered_position)
            self.assertGreater(recovered_position.quantity, Decimal("0"))
            self.assertEqual(
                self.baseline_trade_count + 1,
                len(recovered_trades),
            )
            self.assertEqual(
                self.baseline_trades,
                recovered_trades[: self.baseline_trade_count],
            )
            process_a_buy = recovered_trades[-1]
            self.assertIs(process_a_buy.side, OrderSide.BUY)
            self.assertEqual(
                receipt["client_order_id"],
                process_a_buy.client_order_id,
            )
            self.assertEqual(receipt["order_id"], process_a_buy.order_id)
            self.assertLessEqual(
                process_a_buy.requested_quantity
                * process_a_buy.market_price_at_decision,
                self.max_notional,
            )
            self.assertFalse(
                any(
                    trace.message_id == "6"
                    for trace in recovered_controller.order_execution_trace
                )
            )  # Startup reconciliation은 REST 조회만 수행하고 Gateway submit trace를 만들지 않는다.

            # Signed commission preflight를 반복한 뒤 public recovery Operation만 호출한다.
            self._require_supported_recovery_commission_accounting(
                recovered_runtime
            )
            liquidation_command_id = f"phase9-cold-liquidate-{uuid4().hex}"
            with mock_patch.object(
                TradingSTM,
                "run",
                autospec=True,
                side_effect=AssertionError(
                    "recovered-position liquidation must not resume trading"
                ),
            ) as forbidden_run:
                liquidation_result = (
                    recovered_controller.liquidate_recovered_position(
                        command_id=liquidation_command_id,
                        expected_version=recovered_snapshot.version,
                    )
                )
            forbidden_run.assert_not_called()
            self.assertIs(
                liquidation_result.status,
                TradingSessionStatus.STOPPING,
            )
            self.assertEqual(("G-06",), liquidation_result.transition_ids)

            # Exact duplicate receipt는 같은 expected version으로 replay되고 두 번째 POST를 만들지 않는다.
            submit_count_before_duplicate = sum(
                trace.message_id == "6"
                for trace in recovered_controller.order_execution_trace
            )
            duplicate_result = (
                recovered_controller.liquidate_recovered_position(
                    command_id=liquidation_command_id,
                    expected_version=recovered_snapshot.version,
                )
            )
            self.assertEqual(liquidation_result, duplicate_result)
            self.assertEqual(
                submit_count_before_duplicate,
                sum(
                    trace.message_id == "6"
                    for trace in recovered_controller.order_execution_trace
                ),
            )

            # Queue drain과 due same-ID/retry trigger가 G-06F와 Position 0까지 완료한다.
            self._wait_for_liquidation_termination(recovered_runtime)
            self.assertIs(
                recovered_controller.status,
                TradingSessionStatus.TERMINATED,
            )
            self.assertFalse(
                recovered_controller.snapshot_session().has_open_position
            )
            self.assertEqual(
                (),
                recovered_runtime.trade_history_controller.get_pending_orders(),
            )
            self.assertEqual(
                (),
                recovered_runtime.trade_history_controller.get_pending_order_recovery_records(),
            )
            self._assert_same_id_liquidation_trace(recovered_runtime)

            # Process A BUY 뒤에 추가된 모든 durable Trade는 recovery SELL이어야 한다.
            liquidated_trades = recovered_runtime.trade_history.trades
            sell_trades = liquidated_trades[
                self.baseline_trade_count + 1:
            ]
            self.assertTrue(sell_trades)
            self.assertTrue(
                all(trade.side is OrderSide.SELL for trade in sell_trades)
            )
            self.assertEqual(
                len({trade.client_order_id for trade in liquidated_trades}),
                len(liquidated_trades),
            )
            persisted_trades = (
                recovered_runtime.trade_history_repository.get_trade_history()
            )
            self.assertEqual(liquidated_trades, persisted_trades)

            # Runtime B를 Position 0에서 닫은 뒤 세 번째 runtime이 같은 history를 submit 없이 재생한다.
            close_application(recovered_runtime)
            self.active_runtime = None
            replay_runtime = self._create_and_start_fresh_runtime()
            self.active_runtime = replay_runtime
            replay_controller = replay_runtime.trading_controller
            replay_position = replay_controller.position
            self.assertIs(
                replay_controller.status,
                TradingSessionStatus.NOT_STARTED,
            )
            self.assertFalse(replay_controller.snapshot_session().has_open_position)
            self.assertIsNotNone(replay_position)
            self.assertEqual(Decimal("0"), replay_position.quantity)
            self.assertEqual(
                liquidated_trades,
                replay_runtime.trade_history.trades,
            )
            self.assertFalse(
                any(
                    trace.message_id == "6"
                    for trace in replay_controller.order_execution_trace
                )
            )  # 세 번째 startup도 durable BUY/SELL replay 외 주문 effect를 만들지 않는다.
            self.assertEqual(
                (),
                replay_runtime.trade_history_controller.get_pending_orders(),
            )
            close_application(replay_runtime)
            self.active_runtime = None
        except BaseException as error:
            primary_error = error
            self._add_artifact_note(error)
            raise
        finally:
            if primary_error is not None:
                try:
                    self._cleanup_after_failure()
                except BaseException as cleanup_error:
                    primary_error.add_note(
                        "Recovery cleanup failed with "
                        f"{type(cleanup_error).__name__}; artifacts were preserved."
                    )  # Cleanup exception 원문이나 외부 payload는 primary failure에 연결하지 않는다.


if __name__ == "__main__":
    unittest.main()
