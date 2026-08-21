"""Phase 7 REGIME·trading command를 actual loopback HTTP 경로에서 검증한다."""

from __future__ import annotations

from collections.abc import Mapping
import json
import secrets
import unittest
from unittest.mock import patch
from uuid import uuid4

from binance_auto_trader.application import MarketDataController
from binance_auto_trader.bootstrap import (
    close_application,
    start_application,
)
from binance_auto_trader.transport import (
    BackendEventStream,
    LoopbackTransportServer,
)
from tests.integration.test_startup_flow import (
    _create_test_runtime,
    _MarketStartupBehavior,
)
from tests.integration.transport.test_http_server import (
    TEST_ORIGIN,
    _request_json,
)
from tests.unit.market.test_indicator_snapshot import SNAPSHOT_UPDATED_AT


class TradingCommandHttpIntegrationTests(unittest.TestCase):
    """
    클래스 이름: TradingCommandHttpIntegrationTests
    기능: fake ApplicationRuntime의 select·split·start·stop HTTP trace와 실패 경계를 검증한다.
    작성 날짜: 2026/08/21
    """

    def setUp(self) -> None:
        """
        함수 이름: setUp()
        기능: golden market·fake Binance boundary로 READY runtime과 actual loopback server를 생성한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 실제 startup Controller를 실행할 fake Binance boundary와 golden market 동작을 준비한다.
        operation_trace: list[str] = []
        (
            self.runtime,
            _,
            self.web_socket_client,
            _,
        ) = _create_test_runtime(operation_trace)
        market_behavior = _MarketStartupBehavior(
            self.runtime,
            operation_trace,
        )

        # 검증된 golden MarketSnapshot을 사용하되 나머지 startup·Controller는 실제 경로를 실행한다.
        with patch.object(
            MarketDataController,
            "initialize_market_data",
            autospec=True,
            side_effect=market_behavior,
        ):
            start_application(self.runtime)

        # Startup이 완료된 runtime을 actual random-port loopback transport에 연결한다.
        self.token = secrets.token_urlsafe(32)
        self.event_stream = BackendEventStream(
            clock=lambda: SNAPSHOT_UPDATED_AT,
        )
        self.server = LoopbackTransportServer(
            self.runtime,
            self.token,
            allowed_origins=(TEST_ORIGIN,),
            event_stream=self.event_stream,
        )
        self.server.start()  # OS가 선택한 random loopback port에서 actual HTTP를 받는다.

    def tearDown(self) -> None:
        """
        함수 이름: tearDown()
        기능: loopback listener와 fake runtime의 WebSocket subscription을 멱등 정리한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # Listener를 먼저 닫은 뒤 startup이 연 account subscription을 회수한다.
        self.server.stop()
        close_application(self.runtime)  # startup이 연 account stream까지 회수한다.

    def _send_command(
        self,
        method: str,
        path: str,
        body: Mapping[str, object],
        *,
        command_id: str | None = None,
    ) -> tuple[int, dict[str, object], str]:
        """
        함수 이름: _send_command()
        기능: strict JSON command를 actual HTTP로 전송하고 response와 사용한 command ID를 반환한다.
        인자: method -> POST 또는 PATCH
            path -> Phase 7 command endpoint
            body -> schema_version을 포함한 command DTO
            command_id -> 재사용할 Idempotency-Key 또는 None
        반환값: HTTP status, 공통 envelope, stable command ID tuple
        작성 날짜: 2026/08/21
        """
        # 지정하거나 새로 만든 stable command ID와 strict JSON body를 actual HTTP로 보낸다.
        selected_command_id = command_id or str(uuid4())
        status, payload, _ = _request_json(
            self.server,
            self.token,
            method,
            path,
            body=json.dumps(
                body,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            request_id=str(uuid4()),
            idempotency_key=selected_command_id,
        )
        if payload is None:
            raise AssertionError("JSON command must return an envelope")

        return status, payload, selected_command_id  # 후속 멱등·event 검증에 같은 ID를 쓴다.

    def _get_snapshot(self) -> dict[str, object]:
        """
        함수 이름: _get_snapshot()
        기능: command 이후 authoritative full snapshot을 actual HTTP로 조회한다.
        인자: 없음
        반환값: 성공 envelope의 data object
        작성 날짜: 2026/08/21
        """
        # 별도 GET 요청으로 command 응답과 독립된 authoritative snapshot을 조회한다.
        status, payload, _ = _request_json(
            self.server,
            self.token,
            "GET",
            "/v1/snapshot",
            request_id=str(uuid4()),
        )
        if payload is None:
            raise AssertionError("snapshot must return a JSON envelope")

        # 성공 envelope의 data object만 후속 lifecycle assertion에 전달한다.
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        data = payload["data"]
        if not isinstance(data, dict):
            raise AssertionError("snapshot data must be an object")

        return data  # command response와 독립된 authoritative publication을 검증한다.

    def test_case1_select_split_start_duplicate_and_zero_position_stop(
        self,
    ) -> None:
        """
        함수 이름: test_case1_select_split_start_duplicate_and_zero_position_stop()
        기능: Case 1 메시지가 version·event·멱등 계약으로 선택에서 무포지션 종료까지 완료되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # REGIME 선택과 split 변경을 선행한 뒤 같은 Context version 계보에서 session을 시작한다.
        selection_status, selection_payload, selection_id = self._send_command(
            "POST",
            "/v1/regime/selection",
            {
                "schema_version": 2,
                "regime_type": "type0",
                "expected_version": 0,
            },
        )
        split_status, split_payload, split_id = self._send_command(
            "PATCH",
            "/v1/trading/split-ratios",
            {
                "schema_version": 2,
                "scale_in": "0.35",
                "scale_out": "0.65",
                "expected_version": 1,
            },
        )
        start_body = {
            "schema_version": 2,
            "expected_version": 2,
        }
        start_status, start_payload, start_id = self._send_command(
            "POST",
            "/v1/trading/start",
            start_body,
        )
        sequence_after_start = self.event_stream.last_sequence

        # 같은 key·body는 route와 STM.run을 재실행하지 않고 첫 response를 replay한다.
        duplicate_status, duplicate_payload, _ = self._send_command(
            "POST",
            "/v1/trading/start",
            start_body,
            command_id=start_id,
        )
        sequence_after_duplicate = self.event_stream.last_sequence

        # 같은 key에 다른 body를 결합하면 기존 성공 결과 대신 conflict를 반환한다.
        conflict_status, conflict_payload, _ = self._send_command(
            "POST",
            "/v1/trading/start",
            {
                "schema_version": 2,
                "expected_version": 3,
            },
            command_id=start_id,
        )

        # Authoritative position이 0인 Case 1 stop은 외부 청산 없이 즉시 종료한다.
        stop_status, stop_payload, stop_id = self._send_command(
            "POST",
            "/v1/trading/stop",
            {
                "schema_version": 2,
                "expected_version": 3,
            },
        )

        # 각 command의 response DTO, version, replay와 conflict 결과를 확인한다.
        self.assertEqual(200, selection_status)
        self.assertEqual(
            {
                "selected": "type0",
                "support_status": "supported",
                "version": 1,
            },
            selection_payload["data"],
        )
        self.assertEqual(200, split_status)
        self.assertEqual(
            {"scale_in": "0.35", "scale_out": "0.65", "version": 2},
            split_payload["data"],
        )
        self.assertEqual(200, start_status)
        self.assertEqual("running", start_payload["data"]["status"])
        self.assertEqual(3, start_payload["data"]["version"])
        self.assertIsNotNone(start_payload["data"]["session_id"])
        self.assertEqual(start_status, duplicate_status)
        self.assertNotEqual(
            start_payload["request_id"],
            duplicate_payload["request_id"],
        )
        self.assertEqual(start_payload["data"], duplicate_payload["data"])
        self.assertEqual(sequence_after_start, sequence_after_duplicate)
        self.assertEqual(409, conflict_status)
        self.assertEqual(
            "IDEMPOTENCY_CONFLICT",
            conflict_payload["error"]["code"],
        )
        self.assertEqual(200, stop_status)
        self.assertEqual("terminated", stop_payload["data"]["status"])
        self.assertEqual(4, stop_payload["data"]["version"])

        # 오직 성공 command만 stable correlation ID와 단조 version event를 하나씩 발행한다.
        replay_batch = self.event_stream.replay_after(0)
        self.assertFalse(replay_batch.requires_resync)
        self.assertEqual(
            (
                "REGIME_SELECTED",
                "TRADING_SESSION_UPDATED",
                "TRADING_SESSION_UPDATED",
                "TRADING_SESSION_UPDATED",
            ),
            tuple(event.event_type for event in replay_batch.events),
        )
        self.assertEqual(
            (selection_id, split_id, start_id, stop_id),
            tuple(event.correlation_id for event in replay_batch.events),
        )
        self.assertEqual(
            (1, 2, 3, 4),
            tuple(event.aggregate_version for event in replay_batch.events),
        )

        # 최종 full snapshot이 event sequence와 종료된 Context를 동일하게 반영하는지 확인한다.
        snapshot = self._get_snapshot()
        self.assertEqual(4, snapshot["last_sequence"])
        self.assertEqual("type0", snapshot["regime"]["selected"])
        self.assertEqual("terminated", snapshot["trading"]["status"])
        self.assertEqual(4, snapshot["trading"]["version"])
        self.assertEqual("0.35", snapshot["trading"]["scale_in"])
        self.assertEqual("0.65", snapshot["trading"]["scale_out"])
        self.assertFalse(snapshot["trading"]["has_open_position"])  # D-05 무포지션 종료를 snapshot으로 재확인한다.

    def test_unselected_unsupported_invalid_regime_and_stale_fail_closed(
        self,
    ) -> None:
        """
        함수 이름: test_unselected_unsupported_invalid_regime_and_stale_fail_closed()
        기능: 미선택·비canonical·미지원·stale command가 Context나 event를 잘못 변경하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 미선택, 비canonical, 미지원과 stale 입력을 독립 command로 전송한다.
        unselected_status, unselected_payload, _ = self._send_command(
            "POST",
            "/v1/trading/start",
            {"schema_version": 2, "expected_version": 0},
        )
        invalid_status, invalid_payload, _ = self._send_command(
            "POST",
            "/v1/regime/selection",
            {
                "schema_version": 2,
                "regime_type": "TYPE_0",
                "expected_version": 0,
            },
        )
        unsupported_status, unsupported_payload, _ = self._send_command(
            "POST",
            "/v1/regime/selection",
            {
                "schema_version": 2,
                "regime_type": "type1",
                "expected_version": 0,
            },
        )
        unsupported_start_status, unsupported_start_payload, _ = (
            self._send_command(
                "POST",
                "/v1/trading/start",
                {"schema_version": 2, "expected_version": 1},
            )
        )
        stale_status, stale_payload, _ = self._send_command(
            "PATCH",
            "/v1/trading/split-ratios",
            {
                "schema_version": 2,
                "scale_in": "0.4",
                "scale_out": "0.6",
                "expected_version": 0,
            },
        )

        # 각 실패 code와 미지원 선택의 유일한 성공 version을 검증한다.
        self.assertEqual(422, unselected_status)
        self.assertEqual(
            "NO_SELECTED_REGIME",
            unselected_payload["error"]["code"],
        )
        self.assertEqual(422, invalid_status)
        self.assertEqual("INVALID_REGIME_TYPE", invalid_payload["error"]["code"])
        self.assertEqual(200, unsupported_status)
        self.assertEqual("type1", unsupported_payload["data"]["selected"])
        self.assertEqual(
            "unsupported",
            unsupported_payload["data"]["support_status"],
        )
        self.assertEqual(1, unsupported_payload["data"]["version"])
        self.assertEqual(422, unsupported_start_status)
        self.assertEqual(
            "UNSUPPORTED_TRADING_LOGIC",
            unsupported_start_payload["error"]["code"],
        )
        self.assertEqual(409, stale_status)
        self.assertEqual(
            "STALE_CONTEXT_VERSION",
            stale_payload["error"]["code"],
        )
        self.assertEqual(
            {"current_version": 1, "expected_version": 0},
            stale_payload["error"]["details"],
        )

        # 미선택·invalid·start·stale 실패는 event를 내지 않고 미지원 선택만 보존한다.
        replay_batch = self.event_stream.replay_after(0)
        self.assertEqual(1, len(replay_batch.events))
        self.assertEqual("REGIME_SELECTED", replay_batch.events[0].event_type)
        self.assertEqual(1, self.runtime.trading_controller.context.version)
        self.assertEqual("not_started", self._get_snapshot()["trading"]["status"])

    def test_disconnected_account_stream_rejects_start_as_retryable_offline(
        self,
    ) -> None:
        """
        함수 이름: test_disconnected_account_stream_rejects_start_as_retryable_offline()
        기능: startup 후 account stream 연결이 끊기면 start를 retryable 503으로 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 지원 REGIME을 선택한 뒤 account stream만 끊어 runtime readiness와 분리한다.
        selection_status, _, _ = self._send_command(
            "POST",
            "/v1/regime/selection",
            {
                "schema_version": 2,
                "regime_type": "type0",
                "expected_version": 0,
            },
        )
        disconnect_callback = self.web_socket_client.account_disconnect_callback
        if disconnect_callback is None:
            raise AssertionError("account disconnect callback must be configured")
        disconnect_callback()  # Runtime READY는 유지하되 command 연결 Guard만 offline으로 바꾸어야 한다.

        # Offline Guard를 같은 stable command ID로 재시도할 start DTO에 적용한다.
        start_body = {"schema_version": 2, "expected_version": 1}
        start_status, start_payload, start_command_id = self._send_command(
            "POST",
            "/v1/trading/start",
            start_body,
        )

        # Retryable offline 실패가 Context version과 event sequence를 보존하는지 확인한다.
        self.assertEqual(200, selection_status)
        self.assertTrue(self.runtime.ready)
        self.assertFalse(self.runtime.web_socket_gateway.account_connected)
        self.assertEqual(503, start_status)
        self.assertEqual(
            "CONNECTION_NOT_READY",
            start_payload["error"]["code"],
        )
        self.assertTrue(start_payload["error"]["retryable"])
        self.assertEqual(1, self.event_stream.last_sequence)
        self.assertEqual(1, self.runtime.trading_controller.context.version)  # 실패 start는 initialize를 호출하지 않는다.

        # 재연결 뒤 같은 command ID를 재전송해 retryable 503이 캐시에 고정되지 않았음을 검증한다.
        self.runtime.trading_controller.load_account()
        retry_status, retry_payload, _ = self._send_command(
            "POST",
            "/v1/trading/start",
            start_body,
            command_id=start_command_id,
        )

        # 같은 command ID의 재시도가 재연결 뒤 정상 start와 새 event를 만드는지 확인한다.
        self.assertTrue(self.runtime.web_socket_gateway.account_connected)
        self.assertEqual(200, retry_status)
        self.assertEqual("running", retry_payload["data"]["status"])
        self.assertEqual(2, retry_payload["data"]["version"])
        self.assertEqual(2, self.event_stream.last_sequence)

    def test_split_ratio_dto_rejects_float_exponent_range_and_unknown_fields(
        self,
    ) -> None:
        """
        함수 이름: test_split_ratio_dto_rejects_float_exponent_range_and_unknown_fields()
        기능: split DTO가 plain Decimal 문자열·exact field·ratio 범위를 엄격히 강제하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # Float, exponent, signed zero, 범위 초과와 unknown field 실패 표를 구성한다.
        invalid_bodies = (
            (
                {
                    "schema_version": 2,
                    "scale_in": 0.4,
                    "scale_out": "0.6",
                    "expected_version": 0,
                },
                400,
                "MALFORMED_REQUEST",
            ),
            (
                {
                    "schema_version": 2,
                    "scale_in": "4E-1",
                    "scale_out": "0.6",
                    "expected_version": 0,
                },
                400,
                "MALFORMED_REQUEST",
            ),
            (
                {
                    "schema_version": 2,
                    "scale_in": "-0.0",
                    "scale_out": "0.6",
                    "expected_version": 0,
                },
                422,
                "INVALID_SPLIT_RATIO",
            ),
            (
                {
                    "schema_version": 2,
                    "scale_in": "0.4",
                    "scale_out": "1.1",
                    "expected_version": 0,
                },
                422,
                "INVALID_SPLIT_RATIO",
            ),
            (
                {
                    "schema_version": 2,
                    "scale_in": "0.4",
                    "scale_out": "0.6",
                    "expected_version": 0,
                    "unexpected": True,
                },
                400,
                "MALFORMED_REQUEST",
            ),
        )

        # 각 실패는 Context version 0을 유지해 동일 expected_version으로 독립 검증한다.
        for body, expected_status, expected_code in invalid_bodies:
            with self.subTest(body=body):
                status, payload, _ = self._send_command(
                    "PATCH",
                    "/v1/trading/split-ratios",
                    body,
                )
                self.assertEqual(expected_status, status)
                self.assertEqual(expected_code, payload["error"]["code"])
                self.assertEqual(0, self.runtime.trading_controller.context.version)

        # 실패 표 이후 같은 version으로 canonical Decimal 문자열 command를 적용한다.
        valid_status, valid_payload, valid_id = self._send_command(
            "PATCH",
            "/v1/trading/split-ratios",
            {
                "schema_version": 2,
                "scale_in": "0.40",
                "scale_out": "0.60",
                "expected_version": 0,
            },
        )

        # 성공 DTO와 정확히 하나 발행된 Context mutation event를 함께 검증한다.
        self.assertEqual(200, valid_status)
        self.assertEqual(
            {"scale_in": "0.40", "scale_out": "0.60", "version": 1},
            valid_payload["data"],
        )
        self.assertEqual(1, self.event_stream.last_sequence)
        split_event = self.event_stream.replay_after(0).events[0]
        self.assertEqual(valid_id, split_event.correlation_id)
        self.assertEqual(1, split_event.aggregate_version)  # 정상 DTO만 하나의 Context mutation event를 낸다.


if __name__ == "__main__":
    unittest.main()
