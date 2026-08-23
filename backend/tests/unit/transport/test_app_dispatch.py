"""Loopback HTTP dispatcher가 Trade History raw query를 owner route에 보존하는지 검증한다."""

import unittest
from unittest.mock import patch

from binance_auto_trader.transport.app import (
    LoopbackTransportServer,
    _parse_request_target,
)
from binance_auto_trader.transport.contracts import (
    TransportContractError,
    TransportResponse,
)


class LoopbackTradeHistoryDispatchTests(unittest.TestCase):
    """
    클래스 이름: LoopbackTradeHistoryDispatchTests
    기능: `/v1/trades` query가 decoding 전 계약 owner에 전달되는 경계를 검증한다.
    작성 날짜: 2026/08/23
    """

    def test_trade_history_route_receives_exact_raw_combined_query(self) -> None:
        """
        함수 이름: test_trade_history_route_receives_exact_raw_combined_query()
        기능: period와 side를 하나의 raw query로 route에 전달하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        # Socket bind 없이 dispatcher 자체만 검증하도록 최소 server 인스턴스를 구성한다.
        server = object.__new__(LoopbackTransportServer)
        route_context = object()
        server._route_context = route_context  # type: ignore[assignment]
        expected_response = TransportResponse(
            status=200,
            payload={"ok": True},
        )
        raw_query = "period=last7days&side=sell"

        with patch(
            "binance_auto_trader.transport.app.get_trades",
            return_value=expected_response,
        ) as get_trades:
            response = server._route_http_request(
                ("GET", "/v1/trades"),
                "9f9408c9-c9a3-4e80-82b3-3573054aeb40",
                request_query=raw_query,
            )

        self.assertIs(response, expected_response)
        get_trades.assert_called_once_with(
            "9f9408c9-c9a3-4e80-82b3-3573054aeb40",
            route_context,
            raw_query,
        )  # Strict parser가 duplicate와 blank를 판정하도록 raw text를 유지한다.

    def test_request_target_rejects_raw_fragment_before_query_dispatch(
        self,
    ) -> None:
        """
        함수 이름: test_request_target_rejects_raw_fragment_before_query_dispatch()
        기능: fragment가 붙은 raw Trade History target을 정상 query로 잘라 받지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        request_target = "/v1/trades?period=today&side=all#unexpected"

        # Browser가 보통 보내지 않는 raw fragment도 수동 loopback client 경계에서는 fail closed한다.
        with self.assertRaises(TransportContractError) as raised:
            _parse_request_target(request_target)

        self.assertEqual(raised.exception.code, "MALFORMED_REQUEST")


if __name__ == "__main__":
    unittest.main()
