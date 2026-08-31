"""Loopback HTTP dispatcher가 Trade History raw query를 owner route에 보존하는지 검증한다."""

from collections import deque
from threading import Event, RLock, Thread
import unittest
from unittest.mock import patch

from binance_auto_trader.transport.app import (
    LoopbackTransportServer,
    _parse_request_target,
)
from binance_auto_trader.transport.contracts import (
    SCHEMA_VERSION,
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


class LoopbackCsvExportDispatchTests(unittest.TestCase):
    """
    클래스 이름: LoopbackCsvExportDispatchTests
    기능: POST `/v1/csv-exports` body와 idempotency command ID의 route dispatch를 검증한다.
    작성 날짜: 2026/08/23
    """

    def test_csv_export_route_receives_exact_body_and_command_id(self) -> None:
        """
        함수 이름: test_csv_export_route_receives_exact_body_and_command_id()
        기능: CSV POST의 전체 option body와 stable command ID를 변형 없이 owner route에 전달한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        # Socket bind와 filesystem 진입 없이 dispatcher의 endpoint mapping만 검증한다.
        server = object.__new__(LoopbackTransportServer)
        route_context = object()
        server._route_context = route_context  # type: ignore[assignment]
        expected_response = TransportResponse(
            status=201,
            payload={"ok": True},
        )
        request_id = "9f9408c9-c9a3-4e80-82b3-3573054aeb40"
        command_id = "2522ef0c-d88d-42b3-a22f-fc7bdd09a662"
        request_body = {
            "schema_version": SCHEMA_VERSION,
            "directory": "/Users/oscar/Exports",
            "file_name": "phase11-trades.csv",
            "period": "last7days",
            "start_date": "2026-08-17",
            "end_date": "2026-08-23",
            "timezone": "Asia/Seoul",
        }

        with patch(
            "binance_auto_trader.transport.app.create_csv_export",
            return_value=expected_response,
        ) as create_csv_export:
            response = server._route_http_request(
                ("POST", "/v1/csv-exports"),
                request_id,
                request_body=request_body,
                command_id=command_id,
            )

        self.assertIs(response, expected_response)
        create_csv_export.assert_called_once_with(
            request_id,
            route_context,
            request_body,
            command_id,
        )  # Parser owner가 exact key/value를 검증하도록 body와 identity를 그대로 보존한다.


class LoopbackRecoveredPositionLiquidationDispatchTests(unittest.TestCase):
    """
    클래스 이름: LoopbackRecoveredPositionLiquidationDispatchTests
    기능: 복구 Position 청산 endpoint가 별도 owner route에 body와 command ID를 전달하는지 검증한다.
    작성 날짜: 2026/08/24
    """

    def test_recovery_liquidation_route_receives_exact_body_and_command_id(
        self,
    ) -> None:
        """
        함수 이름: test_recovery_liquidation_route_receives_exact_body_and_command_id()
        기능: recovery liquidation POST의 version body와 stable identity를 변형 없이 전달한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # Socket 없이 새 endpoint의 dispatcher mapping만 독립 검증한다.
        server = object.__new__(LoopbackTransportServer)
        route_context = object()
        server._route_context = route_context  # type: ignore[assignment]
        expected_response = TransportResponse(
            status=202,
            payload={"ok": True},
        )
        request_id = "9f9408c9-c9a3-4e80-82b3-3573054aeb40"
        command_id = "recovered-position-liquidation-command"
        request_body = {
            "schema_version": SCHEMA_VERSION,
            "expected_version": 7,
        }

        with patch(
            "binance_auto_trader.transport.app.liquidate_recovered_position",
            return_value=expected_response,
        ) as liquidate_recovered_position:
            response = server._route_http_request(
                (
                    "POST",
                    "/v1/trading/recovered-position/liquidate",
                ),
                request_id,
                request_body=request_body,
                command_id=command_id,
            )

        self.assertIs(response, expected_response)
        liquidate_recovered_position.assert_called_once_with(
            request_id,
            route_context,
            request_body,
            command_id,
        )  # Route owner가 exact DTO와 별도 idempotency namespace를 최종 검증한다.


class _CommandHandler:
    """
    클래스 이름: _CommandHandler
    기능: socket 없이 command dispatch에 method, body와 idempotency key를 제공한다.
    작성 날짜: 2026/08/24
    """

    def __init__(self, command_key: str) -> None:
        """
        함수 이름: __init__()
        기능: 유일 command key와 strict schema body를 가진 POST handler fake를 만든다.
        인자: command_key -> dispatcher single-flight를 구분할 idempotency key
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        self.command = "POST"
        self.command_key = command_key
        self.request_body = {"schema_version": SCHEMA_VERSION}
        self.raw_body = b'{"schema_version":3}'


class LoopbackIdempotencyConcurrencyTests(unittest.TestCase):
    """
    클래스 이름: LoopbackIdempotencyConcurrencyTests
    기능: key별 single-flight가 서로 다른 장기 command를 전역 직렬화하지 않는지 검증한다.
    작성 날짜: 2026/08/24
    """

    def test_different_idempotency_keys_execute_while_first_route_is_blocked(
        self,
    ) -> None:
        """
        함수 이름: test_different_idempotency_keys_execute_while_first_route_is_blocked()
        기능: 느린 CSV key와 다른 key의 command가 global idempotency lock 밖에서 병행되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        server = object.__new__(LoopbackTransportServer)
        server._idempotency_lock = RLock()
        server._idempotency_records = {}
        server._idempotency_order = deque()
        server._idempotency_flights = {}
        first_route_started = Event()
        allow_first_route_to_finish = Event()
        first_handler = _CommandHandler("phase11-first-key")
        second_handler = _CommandHandler("phase11-second-key")
        responses: dict[str, TransportResponse] = {}
        failures: list[BaseException] = []

        def read_request_body(handler: _CommandHandler) -> tuple[dict[str, object], bytes]:
            """
            함수 이름: read_request_body()
            기능: handler별 strict body와 raw fingerprint bytes를 dispatcher에 제공한다.
            인자: handler -> 현재 thread의 command handler fake
            반환값: parsed JSON object와 exact raw bytes
            작성 날짜: 2026/08/24
            """
            return handler.request_body, handler.raw_body

        def read_command_key(handler: _CommandHandler) -> str:
            """
            함수 이름: read_command_key()
            기능: 현재 handler에 결합된 idempotency key를 반환한다.
            인자: handler -> 현재 thread의 command handler fake
            반환값: command key 문자열
            작성 날짜: 2026/08/24
            """
            return handler.command_key

        def route_command(
            endpoint_key: tuple[str, str],
            request_id: str,
            *,
            request_body: dict[str, object] | None = None,
            command_id: str | None = None,
        ) -> TransportResponse:
            """
            함수 이름: route_command()
            기능: 첫 key만 정지시키고 두 번째 key의 route 진입을 즉시 성공으로 기록한다.
            인자: endpoint_key -> command method/path
                request_id -> 현재 요청 UUID
                request_body -> dispatcher가 전달한 strict body
                command_id -> dispatcher가 전달한 idempotency key
            반환값: key와 request ID를 담은 결정적 성공 응답
            작성 날짜: 2026/08/24
            """
            del endpoint_key, request_body
            if command_id == first_handler.command_key:
                first_route_started.set()
                if not allow_first_route_to_finish.wait(timeout=2.0):
                    raise TimeoutError("first route release was not signalled")
            return TransportResponse(
                status=200,
                payload={
                    "ok": True,
                    "request_id": request_id,
                    "command_id": command_id,
                },
            )

        def dispatch(handler: _CommandHandler, request_id: str) -> None:
            """
            함수 이름: dispatch()
            기능: 한 fake command를 실행해 응답 또는 예상 밖 실패를 thread-safe collection에 기록한다.
            인자: handler -> 실행할 command handler fake
                request_id -> 현재 command correlation UUID
            반환값: 없음
            작성 날짜: 2026/08/24
            """
            try:
                responses[handler.command_key] = server._dispatch_http_request(
                    handler,  # type: ignore[arg-type]
                    "/v1/csv-exports",
                    request_id,
                )
            except BaseException as error:
                failures.append(error)

        server._route_http_request = route_command  # type: ignore[method-assign]
        with patch(
            "binance_auto_trader.transport.app._read_json_request_body",
            side_effect=read_request_body,
        ), patch(
            "binance_auto_trader.transport.app._require_idempotency_key",
            side_effect=read_command_key,
        ):
            first_thread = Thread(
                target=dispatch,
                args=(first_handler, "9f9408c9-c9a3-4e80-82b3-3573054aeb40"),
            )
            first_thread.start()
            self.assertTrue(first_route_started.wait(timeout=1.0))

            # 첫 route가 계속 멈춘 상태에서도 다른 key는 전역 lock을 기다리지 않아야 한다.
            second_thread = Thread(
                target=dispatch,
                args=(second_handler, "e84633a7-2167-4cdf-879c-f3630d2b247d"),
            )
            second_thread.start()
            second_thread.join(timeout=1.0)
            self.assertFalse(second_thread.is_alive())
            self.assertIn(second_handler.command_key, responses)

            allow_first_route_to_finish.set()
            first_thread.join(timeout=2.0)

        self.assertFalse(first_thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(set(responses), {
            first_handler.command_key,
            second_handler.command_key,
        })


if __name__ == "__main__":
    unittest.main()
