"""Binance 연결 진단이 거래 상태를 변경하지 않고 gateway 상태만 조회하는지 검증한다."""

from threading import Lock
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from binance_auto_trader.transport.event_stream import BackendEventStream
from binance_auto_trader.transport.routes import RouteContext
from binance_auto_trader.transport.routes.connection_status import (
    get_binance_connection_status,
)

REQUEST_ID = "9f9408c9-c9a3-4e80-82b3-3573054aeb40"


class BinanceConnectionStatusRouteTests(unittest.TestCase):
    """
    클래스 이름: BinanceConnectionStatusRouteTests
    기능: 실제 gateway 진단·독립 WebSocket 상태와 조회 실패 경계를 검증한다.
    작성 날짜: 2026/09/05
    """

    def setUp(self) -> None:
        """
        함수 이름: setUp()
        기능: 외부 통신 없이 조회 호출을 관찰할 runtime을 구성한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/05
        """
        self.runtime = SimpleNamespace(
            ready=True,
            application_lock=Lock(),
            api_gateway=SimpleNamespace(fetch_account_snapshot=Mock()),
            web_socket_gateway=SimpleNamespace(
                kline_connected=True,
                account_connected=False,
            ),
        )
        self.context = RouteContext(self.runtime, BackendEventStream())

    def test_diagnosis_releases_application_lock_and_keeps_streams_separate(self) -> None:
        """
        함수 이름: test_diagnosis_releases_application_lock_and_keeps_streams_separate()
        기능: 인증 조회 중 trading lock을 잡지 않고 서로 다른 stream 상태를 반환한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/05
        """
        def probe_account() -> object:
            """
            함수 이름: probe_account()
            기능: 조회 중 application lock 해제를 검증하고 비공개 account 값을 반환한다.
            인자: 없음
            반환값: 응답에 포함하면 안 되는 account test 값
            작성 날짜: 2026/09/05
            """
            self.assertTrue(self.runtime.application_lock.acquire(blocking=False))
            self.runtime.application_lock.release()
            return {"private_account_value": "hidden"}

        self.runtime.api_gateway.fetch_account_snapshot.side_effect = probe_account
        response = get_binance_connection_status(REQUEST_ID, self.context)

        self.assertEqual(response.status, 200)
        self.assertIsInstance(response.payload["data"].pop("checked_at_ms"), int)
        self.assertEqual(response.payload["data"], {
            "api": "online",
            "market_stream": "online",
            "account_stream": "offline",
        })
        self.assertNotIn("private_account_value", str(response.payload))

    def test_api_failure_does_not_guess_websocket_state_or_expose_error(self) -> None:
        """
        함수 이름: test_api_failure_does_not_guess_websocket_state_or_expose_error()
        기능: API 장애에서도 실제 WebSocket 값을 유지하고 오류 원문을 숨긴다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/05
        """
        self.runtime.api_gateway.fetch_account_snapshot.side_effect = RuntimeError("private detail")
        response = get_binance_connection_status(REQUEST_ID, self.context)

        self.assertIsInstance(response.payload["data"].pop("checked_at_ms"), int)
        self.assertEqual(response.payload["data"], {
            "api": "offline",
            "market_stream": "online",
            "account_stream": "offline",
        })
        self.assertNotIn("private detail", str(response.payload))

    def test_unready_runtime_does_not_probe_binance(self) -> None:
        """
        함수 이름: test_unready_runtime_does_not_probe_binance()
        기능: 준비되지 않은 runtime의 진단은 외부 조회 없이 실패한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/05
        """
        self.runtime.ready = False
        response = get_binance_connection_status(REQUEST_ID, self.context)

        self.assertEqual(response.status, 503)
        self.runtime.api_gateway.fetch_account_snapshot.assert_not_called()
