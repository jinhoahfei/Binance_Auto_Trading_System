"""Trading lifecycle route가 복구 Position 청산 결과를 정확히 publish하는지 검증한다."""

from decimal import Decimal
from threading import RLock
import unittest

from binance_auto_trader.application.trading_controller import (
    TradingSessionResult,
    TradingSessionSnapshot,
    TradingSessionStatus,
)
from binance_auto_trader.transport import BackendEventStream
from binance_auto_trader.transport.routes import RouteContext
from binance_auto_trader.transport.routes.trading import (
    liquidate_recovered_position,
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
                "schema_version": 2,
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


if __name__ == "__main__":
    unittest.main()
