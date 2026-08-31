"""Trading lifecycle route가 복구 Position 청산 결과를 정확히 publish하는지 검증한다."""

from dataclasses import replace
from decimal import Decimal
from threading import RLock
import unittest

from binance_auto_trader.application.trading_controller import (
    ManualKillResult,
    TradingSessionResult,
    TradingSessionSnapshot,
    TradingSessionStatus,
)
from binance_auto_trader.domain.trading import (
    ManualKillBehavior,
    RiskPolicyAvailability,
)
from binance_auto_trader.transport import BackendEventStream
from binance_auto_trader.transport.routes import RouteContext
from binance_auto_trader.transport.routes.trading import (
    liquidate_recovered_position,
    set_manual_kill,
)


class _RecoveredPositionLiquidationController:
    """
    클래스 이름: _RecoveredPositionLiquidationController
    기능: recovery route의 command 입력과 STOPPING snapshot publication을 기록한다.
    작성 날짜: 2026/08/24
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 비어 있는 호출 기록과 NOT_STARTED 이전 snapshot을 초기화한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        self.calls: list[tuple[str, int]] = []
        self._snapshot = TradingSessionSnapshot(
            status=TradingSessionStatus.NOT_STARTED,
            session_id=None,
            version=4,
            scale_in=Decimal("0.5"),
            scale_out=Decimal("0.5"),
            has_open_position=True,
            command_enabled=True,
            selected=None,
            support_status=None,
            risk_policy_availability=RiskPolicyAvailability.UNAVAILABLE,
            configured_risk_policy_version=None,
            session_risk_policy_version=None,
            risk_control_version=0,
            manual_kill_active=False,
            manual_kill_cleanup_complete=True,
            manual_kill_activation_behavior=None,
            manual_kill_activation_policy_version=None,
            last_risk_decision=None,
            process_ownership_ambiguous=False,
        )

    def liquidate_recovered_position(
        self,
        *,
        command_id: str,
        expected_version: int,
    ) -> TradingSessionResult:
        """
        함수 이름: liquidate_recovered_position()
        기능: route 입력을 기록하고 결정적 STOPPING receipt와 snapshot을 게시한다.
        인자: command_id -> route가 전달한 stable command ID
            expected_version -> route가 검증한 optimistic version
        반환값: version 6의 STOPPING session 결과
        작성 날짜: 2026/08/24
        """
        self.calls.append((command_id, expected_version))
        session_id = "ed4ed1ee-4f63-43f7-87cc-e6d429834262"
        self._snapshot = TradingSessionSnapshot(
            status=TradingSessionStatus.STOPPING,
            session_id=session_id,
            version=6,
            scale_in=Decimal("0.5"),
            scale_out=Decimal("0.5"),
            has_open_position=True,
            command_enabled=True,
            selected=None,
            support_status=None,
            risk_policy_availability=RiskPolicyAvailability.UNAVAILABLE,
            configured_risk_policy_version=None,
            session_risk_policy_version=None,
            risk_control_version=0,
            manual_kill_active=False,
            manual_kill_cleanup_complete=True,
            manual_kill_activation_behavior=None,
            manual_kill_activation_policy_version=None,
            last_risk_decision=None,
            process_ownership_ambiguous=False,
        )

        return TradingSessionResult(
            status=TradingSessionStatus.STOPPING,
            session_id=session_id,
            version=6,
        )  # Route의 202 분기를 선택할 authoritative receipt를 반환한다.

    def snapshot_session(self) -> TradingSessionSnapshot:
        """
        함수 이름: snapshot_session()
        기능: command 직후 route event에 사용할 최신 session snapshot을 반환한다.
        인자: 없음
        반환값: 현재 immutable TradingSessionSnapshot
        작성 날짜: 2026/08/24
        """
        return self._snapshot  # Command receipt와 같은 version 6 publication을 제공한다.


class _RecoveredPositionLiquidationRuntime:
    """
    클래스 이름: _RecoveredPositionLiquidationRuntime
    기능: recovery route가 요구하는 ready runtime 최소 계약을 제공한다.
    작성 날짜: 2026/08/24
    """

    def __init__(
        self,
        controller: _RecoveredPositionLiquidationController,
    ) -> None:
        """
        함수 이름: __init__()
        기능: ready flag, application lock, testnet mode와 Controller를 조립한다.
        인자: controller -> route 호출과 snapshot을 기록할 fake owner
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        self.application_lock = RLock()
        self.ready = True
        self.execution_mode = "testnet"
        self.trading_controller = controller


class _ManualKillController(_RecoveredPositionLiquidationController):
    """
    클래스 이름: _ManualKillController
    기능: manual kill route 입력과 authoritative risk snapshot publication을 기록한다.
    작성 날짜: 2026/08/25
    """

    def __init__(self, *, cleanup_complete: bool = True) -> None:
        """
        함수 이름: __init__()
        기능: manual-kill route가 공개할 cleanup 완료 상태를 고정한 fake owner를 준비한다.
        인자: cleanup_complete -> command 직후 authoritative cleanup 완료 여부
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        super().__init__()
        self.cleanup_complete = cleanup_complete

    def set_manual_kill(
        self,
        active: bool,
        *,
        command_id: str,
        expected_version: int,
    ) -> ManualKillResult:
        """
        함수 이름: set_manual_kill()
        기능: 입력을 기록하고 unavailable 정책의 versioned manual kill 결과를 반환한다.
        인자: active -> 적용할 kill 활성 상태
            command_id -> route가 전달한 stable command ID
            expected_version -> route가 검증한 risk control version
        반환값: 새 risk control version을 가진 ManualKillResult
        작성 날짜: 2026/08/25
        """
        self.calls.append((command_id, expected_version))
        next_version = expected_version + 1
        self._snapshot = replace(
            self._snapshot,
            manual_kill_active=active,
            manual_kill_cleanup_complete=self.cleanup_complete,
            manual_kill_activation_behavior=(
                ManualKillBehavior.CANCEL_AND_LIQUIDATE
                if active and not self.cleanup_complete
                else None
            ),
            manual_kill_activation_policy_version=(
                1 if active and not self.cleanup_complete else None
            ),
            risk_control_version=next_version,
        )  # Event mapper가 command receipt와 같은 authoritative kill 상태를 읽게 한다.
        return ManualKillResult(
            active=active,
            behavior=(
                ManualKillBehavior.CANCEL_AND_LIQUIDATE
                if not self.cleanup_complete
                else None
            ),
            policy_version=1 if not self.cleanup_complete else None,
            risk_control_version=next_version,
        )


class RecoveredPositionLiquidationRouteTests(unittest.TestCase):
    """
    클래스 이름: RecoveredPositionLiquidationRouteTests
    기능: recovery route의 strict command 전달, 202 response와 event publication을 검증한다.
    작성 날짜: 2026/08/24
    """

    def test_stopping_liquidation_returns_202_and_publishes_same_snapshot(
        self,
    ) -> None:
        """
        함수 이름: test_stopping_liquidation_returns_202_and_publishes_same_snapshot()
        기능: accepted recovery liquidation이 version 6 response와 event를 한 번 내는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        controller = _RecoveredPositionLiquidationController()
        runtime = _RecoveredPositionLiquidationRuntime(controller)
        event_stream = BackendEventStream()
        context = RouteContext(runtime, event_stream)  # type: ignore[arg-type]
        request_id = "9f9408c9-c9a3-4e80-82b3-3573054aeb40"
        command_id = "liquidate-recovered-position-route"

        # Existing stop과 같은 schema/version DTO를 별도 recovery owner에 전달한다.
        response = liquidate_recovered_position(
            request_id,
            context,
            {
                "schema_version": 3,
                "expected_version": 4,
            },
            command_id,
        )

        self.assertEqual(response.status, 202)
        self.assertEqual(controller.calls, [(command_id, 4)])
        self.assertTrue(response.payload["ok"])
        self.assertEqual(
            response.payload["data"],
            {
                "status": "stopping",
                "session_id": "ed4ed1ee-4f63-43f7-87cc-e6d429834262",
                "version": 6,
            },
        )
        published_events = event_stream.replay_after(0).events
        self.assertEqual(len(published_events), 1)
        self.assertEqual(
            published_events[0].event_type,
            "TRADING_SESSION_UPDATED",
        )
        self.assertEqual(published_events[0].aggregate_version, 6)
        self.assertEqual(published_events[0].correlation_id, command_id)

    def test_manual_kill_route_uses_separate_risk_version_and_publishes(
        self,
    ) -> None:
        """
        함수 이름: test_manual_kill_route_uses_separate_risk_version_and_publishes()
        기능: exact bool command가 risk version을 전달하고 unavailable provenance와 snapshot을 게시하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        controller = _ManualKillController()
        runtime = _RecoveredPositionLiquidationRuntime(controller)
        event_stream = BackendEventStream()
        context = RouteContext(runtime, event_stream)  # type: ignore[arg-type]
        request_id = "fe7919a3-5fd4-497a-95f6-099f84b7c679"

        # Active bool과 risk version만 manual kill owner에 전달하고 policy 미설정은 null provenance로 남긴다.
        response = set_manual_kill(
            request_id,
            context,
            {
                "schema_version": 3,
                "active": True,
                "expected_version": 0,
            },
            "activate-manual-kill-route",
        )

        self.assertEqual(200, response.status)
        self.assertEqual(
            {
                "active": True,
                "behavior": None,
                "policy_version": None,
                "risk_control_version": 1,
                "manual_kill_cleanup_complete": True,
            },
            response.payload["data"],
        )
        self.assertEqual(
            [("activate-manual-kill-route", 0)],
            controller.calls,
        )
        published = event_stream.replay_after(0).events
        self.assertEqual(1, len(published))
        self.assertTrue(published[0].payload["trading"]["manual_kill_active"])
        self.assertEqual(
            1,
            published[0].payload["trading"]["risk_control_version"],
        )

    def test_manual_kill_route_returns_202_until_cleanup_completes(
        self,
    ) -> None:
        """
        함수 이름: test_manual_kill_route_returns_202_until_cleanup_completes()
        기능: durable activation 뒤 cleanup false를 response body와 HTTP 202로 함께 공개하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        controller = _ManualKillController(cleanup_complete=False)
        runtime = _RecoveredPositionLiquidationRuntime(controller)
        event_stream = BackendEventStream()
        context = RouteContext(runtime, event_stream)  # type: ignore[arg-type]

        # Route는 activation 성공을 cleanup 완료 200으로 축약하지 않는다.
        response = set_manual_kill(
            "1494d9f4-d8bd-4b94-a62e-af3e61fd42c1",
            context,
            {
                "schema_version": 3,
                "active": True,
                "expected_version": 0,
            },
            "activate-manual-kill-cleanup-pending",
        )

        self.assertEqual(202, response.status)
        self.assertEqual(
            False,
            response.payload["data"]["manual_kill_cleanup_complete"],
        )
        published = event_stream.replay_after(0).events
        self.assertFalse(
            published[0].payload["trading"][
                "manual_kill_cleanup_complete"
            ]
        )


if __name__ == "__main__":
    unittest.main()
