"""Shutdown route의 exact DTO, 202 accepted와 typed blocked receipt를 검증한다."""

from threading import RLock
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from binance_auto_trader.bootstrap import (
    ShutdownBlockedError,
    ShutdownReceiptStatus,
    ShutdownSafetyReceipt,
)
from binance_auto_trader.application import TradingSessionStatus
from binance_auto_trader.transport import BackendEventStream, SCHEMA_VERSION
from binance_auto_trader.transport.routes import RouteContext
from binance_auto_trader.transport.routes.system import get_shutdown_state, request_shutdown


class ShutdownRouteTests(unittest.TestCase):
    """
    클래스 이름: ShutdownRouteTests
    기능: Phase 12 shutdown HTTP contract와 secret-free safety details를 검증한다.
    작성 날짜: 2026/08/24
    """

    def _create_context(self) -> RouteContext:
        """
        함수 이름: _create_context()
        기능: owner 호출을 patch할 route에 필요한 최소 runtime lock과 event stream을 만든다.
        인자: 없음
        반환값: shutdown route용 RouteContext
        작성 날짜: 2026/08/24
        """
        # Route owner가 요구하는 application lock과 독립 event stream을 한 context로 조립한다.
        runtime = SimpleNamespace(application_lock=RLock())
        return RouteContext(runtime, BackendEventStream())

    def test_accepted_shutdown_returns_exact_http_202_data_and_closes_stream(
        self,
    ) -> None:
        """
        함수 이름: test_accepted_shutdown_returns_exact_http_202_data_and_closes_stream()
        기능: accepted receipt가 exact data와 HTTP 202를 반환한 뒤 event stream을 닫는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # 종료 owner가 성공한 route context와 exact accepted receipt를 준비한다.
        context = self._create_context()
        request_id = str(uuid4())
        accepted_receipt = ShutdownSafetyReceipt(
            accepted=True,
            status=ShutdownReceiptStatus.ACCEPTED,
            version=7,
            position_open=False,
            pending_order=False,
            reconciliation_required=False,
        )

        # Production route가 owner receipt를 HTTP 202로 옮기고 stream close를 수행하게 한다.
        with patch(
            "binance_auto_trader.transport.routes.system.request_application_shutdown",
            return_value=accepted_receipt,
        ) as shutdown_owner:
            response = request_shutdown(
                request_id,
                context,
                {
                    "schema_version": SCHEMA_VERSION,
                    "expected_version": 7,
                },
                "shutdown-command",
            )

        # 응답 shape, owner 인자와 post-accept stream 상태를 함께 검증한다.
        self.assertEqual(response.status, 202)
        self.assertEqual(
            response.payload["data"],
            {"accepted": True, "status": "accepted", "version": 7},
        )
        shutdown_owner.assert_called_once_with(
            context.runtime,
            command_id="shutdown-command",
            expected_version=7,
        )
        self.assertTrue(context.event_stream.closed)

    def test_shutdown_state_does_not_require_dashboard_aggregates(self) -> None:
        """
        함수 이름: test_shutdown_state_does_not_require_dashboard_aggregates()
        기능: 시장·계좌·지표가 없어도 최소 종료 기준을 반환하고 조회만으로 stream을 닫지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/05
        """
        context = self._create_context()
        context.runtime.ready = True
        context.runtime.trading_controller = SimpleNamespace(
            context=SimpleNamespace(version=7),
            status=TradingSessionStatus.NOT_STARTED,
        )
        response = get_shutdown_state(str(uuid4()), context)

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["data"], {
            "session_id": context.event_stream.session_id,
            "version": 7,
            "status": "not_started",
        })
        self.assertFalse(context.event_stream.closed)  # 종료 허가는 후속 POST owner가 결정한다.

    def test_unready_shutdown_state_is_rejected_without_controller_access(self) -> None:
        """
        함수 이름: test_unready_shutdown_state_is_rejected_without_controller_access()
        기능: 준비 전 Context version을 추측하지 않고 typed not-ready 응답을 유지한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/05
        """
        context = self._create_context()
        context.runtime.ready = False
        response = get_shutdown_state(str(uuid4()), context)

        self.assertEqual(response.status, 503)
        self.assertEqual(response.payload["error"]["code"], "BACKEND_NOT_READY")

    def test_blocked_shutdown_returns_exact_409_details_and_keeps_stream_open(
        self,
    ) -> None:
        """
        함수 이름: test_blocked_shutdown_returns_exact_409_details_and_keeps_stream_open()
        기능: open exposure가 exact typed error details를 반환하고 process stream을 유지하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # Position과 pending order가 모두 열린 authoritative blocked receipt를 준비한다.
        context = self._create_context()
        blocked_receipt = ShutdownSafetyReceipt(
            accepted=False,
            status=ShutdownReceiptStatus.BLOCKED,
            version=9,
            position_open=True,
            pending_order=True,
            reconciliation_required=False,
        )

        # Owner의 typed blocked 오류가 route 밖으로 raw 주문 정보를 반사하지 않게 한다.
        with patch(
            "binance_auto_trader.transport.routes.system.request_application_shutdown",
            side_effect=ShutdownBlockedError(blocked_receipt),
        ):
            response = request_shutdown(
                str(uuid4()),
                context,
                {
                    "schema_version": SCHEMA_VERSION,
                    "expected_version": 9,
                },
                "shutdown-blocked",
            )

        # Exact typed details와 retry 불가 정책을 확인하고 event stream은 계속 열려 있어야 한다.
        self.assertEqual(response.status, 409)
        self.assertEqual(
            response.payload["error"],
            {
                "code": "SHUTDOWN_BLOCKED_BY_OPEN_EXPOSURE",
                "message": "Open exposure must be resolved before shutdown.",
                "retryable": False,
                "details": blocked_receipt.to_blocked_details(),
            },
        )
        self.assertFalse(context.event_stream.closed)

    def test_shutdown_body_rejects_missing_and_extra_fields(self) -> None:
        """
        함수 이름: test_shutdown_body_rejects_missing_and_extra_fields()
        기능: schema와 expected_version 외 필드 또는 누락을 owner 호출 전에 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        invalid_bodies = (
            {"schema_version": SCHEMA_VERSION},
            {
                "schema_version": SCHEMA_VERSION,
                "expected_version": 0,
                "force": True,
            },
        )

        # Strict contract helper는 route owner patch보다 먼저 MALFORMED_REQUEST를 발생시킨다.
        for invalid_body in invalid_bodies:
            with self.subTest(invalid_body=invalid_body):
                with self.assertRaisesRegex(RuntimeError, "fields"):
                    request_shutdown(
                        str(uuid4()),
                        self._create_context(),
                        invalid_body,
                        "shutdown-invalid",
                    )


if __name__ == "__main__":
    unittest.main()
