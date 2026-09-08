"""실제 읽기 전용 readiness 도구의 주문 권한·소유권·cleanup 경계를 검증한다."""

from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend/src"))
from scripts import live_runtime_readiness as readiness
from binance_auto_trader.bootstrap.application import ApplicationStatus
from binance_auto_trader.bootstrap.live_configuration import LiveConfiguration


class LiveRuntimeReadinessTests(unittest.TestCase):
    """
    클래스 이름: LiveRuntimeReadinessTests
    기능: 실제 network 없이 readiness 도구가 차단과 정리 실패를 숨기지 않는지 검사한다.
    작성 날짜: 2026/09/08
    """

    def test_order_enabled_configuration_rejected_before_ownership(self) -> None:
        """
        함수 이름: test_order_enabled_configuration_rejected_before_ownership()
        기능: 주문 권한이 있는 설정은 owner 조회와 runtime 생성 전에 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        configuration = Mock(enabled=True, allow_live_orders=True)
        with patch.object(readiness._RuntimeOwnershipLock, "acquire") as acquire:
            with self.assertRaises(ValueError):
                readiness.run_runtime_readiness(configuration, Path("/unused"))
        acquire.assert_not_called()  # Readiness 도구에서 주문 가능 graph를 시작할 수 없다.

    def test_lock_conflict_never_creates_runtime(self) -> None:
        """
        함수 이름: test_lock_conflict_never_creates_runtime()
        기능: native app이 owner를 보유할 때 중복 graph를 시작하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        configuration = LiveConfiguration("key", "secret", enabled=True, confirmation="LIVE")
        with patch.object(readiness, "validate_live_history_path", return_value=Path("/live/trade-history.jsonl")), patch.object(readiness._RuntimeOwnershipLock, "acquire", side_effect=RuntimeError("locked")), patch.object(readiness, "create_live_application_runtime") as create:
            with self.assertRaises(RuntimeError):
                readiness.run_runtime_readiness(configuration, Path("/unused"))
        create.assert_not_called()  # Lock 실패를 새로운 directory로 우회하지 않는다.

    def test_cleanup_failure_propagates_and_releases_owner(self) -> None:
        """
        함수 이름: test_cleanup_failure_propagates_and_releases_owner()
        기능: startup·cleanup 실패에서도 owner를 정리하고 PASS 보고를 반환하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        configuration = LiveConfiguration("key", "secret", enabled=True, confirmation="LIVE")
        owner = Mock()
        with patch.object(readiness, "validate_live_history_path", return_value=Path("/live/trade-history.jsonl")), patch.object(readiness._RuntimeOwnershipLock, "acquire", return_value=owner), patch.object(readiness, "create_live_application_runtime", return_value=Mock()), patch.object(readiness, "start_application", side_effect=RuntimeError("startup")), patch.object(readiness, "close_application", side_effect=RuntimeError("cleanup")) as close:
            with self.assertRaisesRegex(RuntimeError, "cleanup"):
                readiness.run_runtime_readiness(configuration, Path("/unused"))
        close.assert_called_once()
        owner.mark_orphaned.assert_called_once()
        owner.release.assert_called_once()  # 예외가 발생해도 lifetime lock 해제는 실행한다.

    def test_snapshot_failures_remain_false_after_clean_shutdown(self) -> None:
        """
        함수 이름: test_snapshot_failures_remain_false_after_clean_shutdown()
        기능: READY여도 pending·unknown이 있으면 해당 검사를 false로 보존한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        from threading import RLock

        configuration = LiveConfiguration("key", "secret", enabled=True, confirmation="LIVE")
        runtime = Mock(application_lock=RLock(), order_execution_enabled=False)
        controller = runtime.trading_controller
        controller.startup_reconciliation_complete = True
        controller.snapshot_session.return_value.has_open_position = False
        controller.residual_totals = (0, 0)
        controller.pending_order_query_count = 1
        controller.external_execution_reconciliation_required = True
        controller.reconciliation_required = True
        runtime.trade_history_repository.get_pending_orders.return_value = (object(),)
        runtime.trade_history_repository.get_trade_history.return_value = ()
        runtime.api_gateway.has_any_exchange_open_orders.return_value = False
        runtime.api_gateway.has_any_exchange_open_order_lists.return_value = False
        owner = Mock()
        # 정상 종료 사실을 실패한 startup-state 검사와 합쳐 모두 PASS로 만들지 않는다.
        with patch.object(readiness, "validate_live_history_path", return_value=Path("/live/trade-history.jsonl")), patch.object(readiness._RuntimeOwnershipLock, "acquire", return_value=owner), patch.object(readiness, "create_live_application_runtime", return_value=runtime), patch.object(readiness, "start_application", return_value=Mock(ready=True)), patch.object(readiness, "close_application", return_value=Mock(status=ApplicationStatus.CLOSED)):
            checks = readiness.run_runtime_readiness(configuration, Path("/unused"))
        self.assertTrue(checks["closed"])
        self.assertFalse(checks["pending_zero"])
        self.assertFalse(checks["unknown_execution_zero"])
        self.assertFalse(all(checks.values()))
        owner.release.assert_called_once()
