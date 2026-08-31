"""Actual loopback HTTP에서 shutdown 202와 idempotency replay를 검증한다."""

import json
import secrets
import unittest
from unittest.mock import patch
from uuid import uuid4

from binance_auto_trader.bootstrap import (
    ShutdownReceiptStatus,
    ShutdownSafetyReceipt,
)
from binance_auto_trader.transport import LoopbackTransportServer
from tests.integration.transport.test_http_server import (
    TEST_ORIGIN,
    _prepare_runtime,
    _request_json,
)


class ShutdownHttpIntegrationTests(unittest.TestCase):
    """
    클래스 이름: ShutdownHttpIntegrationTests
    기능: authenticated HTTP shutdown의 exact 202와 same-key 단일 실행을 검증한다.
    작성 날짜: 2026/08/24
    """

    def test_same_idempotency_key_replays_accepted_202_once(self) -> None:
        """
        함수 이름: test_same_idempotency_key_replays_accepted_202_once()
        기능: 동일 body/key shutdown 두 요청이 owner 한 번과 완전한 202 body 두 개로 끝나는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        token = secrets.token_urlsafe(32)
        server = LoopbackTransportServer(
            _prepare_runtime(ready=True),
            token,
            allowed_origins=(TEST_ORIGIN,),
        )
        server.start()
        request_body = json.dumps(
            {"schema_version": 3, "expected_version": 4},
            separators=(",", ":"),
        )
        receipt = ShutdownSafetyReceipt(
            accepted=True,
            status=ShutdownReceiptStatus.ACCEPTED,
            version=4,
            position_open=False,
            pending_order=False,
            reconciliation_required=False,
        )

        try:
            with patch(
                "binance_auto_trader.transport.routes.system.request_application_shutdown",
                return_value=receipt,
            ) as shutdown_owner:
                responses = [
                    _request_json(
                        server,
                        token,
                        "POST",
                        "/v1/shutdown",
                        body=request_body,
                        request_id=str(uuid4()),
                        idempotency_key="shutdown-same-key",
                    )
                    for _ in range(2)
                ]

            # Replay는 새 request ID envelope를 갖지만 status와 typed data는 최초 결과를 보존한다.
            self.assertEqual([response[0] for response in responses], [202, 202])
            self.assertEqual(
                [response[1]["data"] for response in responses],
                [
                    {"accepted": True, "status": "accepted", "version": 4},
                    {"accepted": True, "status": "accepted", "version": 4},
                ],
            )
            self.assertNotEqual(
                responses[0][1]["request_id"],
                responses[1][1]["request_id"],
            )
            shutdown_owner.assert_called_once()
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
