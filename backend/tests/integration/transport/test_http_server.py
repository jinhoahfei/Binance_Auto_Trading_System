"""Actual loopback HTTP 인증, headers, snapshot과 fail-closed routes를 검증한다."""

from http.client import HTTPConnection
from collections.abc import Mapping
import json
import secrets
from threading import RLock, Thread, local
from time import sleep
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from binance_auto_trader.transport import (
    BackendEventStream,
    LoopbackTransportServer,
    create_account_update_observer,
    create_loopback_transport_application,
)

from tests.unit.transport.test_contracts import _create_ready_runtime


TEST_ORIGIN = "http://127.0.0.1:5173"


class _TrackingLock:
    """
    클래스 이름: _TrackingLock
    기능: snapshot test가 현재 thread의 application lock 보유 여부를 관찰하게 한다.
    작성 날짜: 2026/08/21
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: reentrant lock과 thread-local depth를 초기화한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self._lock = RLock()
        self._local_state = local()

    @property
    def held_by_current_thread(self) -> bool:
        """
        함수 이름: held_by_current_thread()
        기능: 현재 thread가 tracking lock 임계 구역 안인지 반환한다.
        인자: 없음
        반환값: 현재 thread의 lock 보유 여부
        작성 날짜: 2026/08/21
        """
        return getattr(self._local_state, "depth", 0) > 0

    def __enter__(self) -> object:
        """
        함수 이름: __enter__()
        기능: underlying RLock을 얻고 현재 thread depth를 증가시킨다.
        인자: 없음
        반환값: tracking lock 자신
        작성 날짜: 2026/08/21
        """
        self._lock.acquire()
        self._local_state.depth = getattr(self._local_state, "depth", 0) + 1
        return self

    def __exit__(
        self,
        exception_type: object,
        exception_value: object,
        traceback: object,
    ) -> None:
        """
        함수 이름: __exit__()
        기능: 현재 thread depth를 줄이고 underlying RLock을 해제한다.
        인자: exception_type -> 발생 예외 타입 또는 None
            exception_value -> 발생 예외 값 또는 None
            traceback -> 발생 traceback 또는 None
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self._local_state.depth -= 1
        self._lock.release()


class _LockCheckingEventStream(BackendEventStream):
    """
    클래스 이름: _LockCheckingEventStream
    기능: last_sequence가 application lock 안에서 읽혔는지 기록한다.
    작성 날짜: 2026/08/21
    """

    def __init__(self, application_lock: _TrackingLock) -> None:
        """
        함수 이름: __init__()
        기능: 확인할 application lock과 새 event stream을 조립한다.
        인자: application_lock -> snapshot publication tracking lock
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        super().__init__()
        self.application_lock = application_lock
        self.sequence_read_inside_lock = False

    @property
    def last_sequence(self) -> int:
        """
        함수 이름: last_sequence()
        기능: application lock 보유 여부를 기록한 뒤 실제 last sequence를 반환한다.
        인자: 없음
        반환값: BackendEventStream의 마지막 sequence
        작성 날짜: 2026/08/21
        """
        self.sequence_read_inside_lock = (
            self.application_lock.held_by_current_thread
        )
        return super().last_sequence


def _prepare_runtime(*, ready: bool) -> SimpleNamespace:
    """
    함수 이름: _prepare_runtime()
    기능: HTTP health와 snapshot route에 사용할 runtime test double을 준비한다.
    인자: ready -> application startup 완료 여부
    반환값: RuntimeSnapshotSource compatible object
    작성 날짜: 2026/08/21
    """
    runtime = _create_ready_runtime()
    runtime.ready = ready
    runtime.state = SimpleNamespace(status="READY" if ready else "STARTING")
    return runtime


def _request_json(
    server: LoopbackTransportServer,
    token: str,
    method: str,
    path: str,
    *,
    body: str | None = None,
    request_id: str | None = None,
    origin: str = TEST_ORIGIN,
    host: str | None = None,
    idempotency_key: str | None = None,
    extra_headers: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, object] | None, dict[str, str]]:
    """
    함수 이름: _request_json()
    기능: actual loopback socket으로 security header와 optional JSON body를 전송한다.
    인자: server -> 실행 중인 LoopbackTransportServer
        token -> Bearer session token
        method -> HTTP method
        path -> `/v1/*` request target
        body -> raw JSON text 또는 body 없음
        request_id -> X-Request-Id 또는 header 생략이면 None
        origin -> exact Origin 또는 negative test 값
        host -> exact Host override 또는 descriptor 기반 기본값
        idempotency_key -> command header 또는 None
        extra_headers -> CORS preflight 등에 추가할 header mapping
    반환값: HTTP status, parsed JSON body와 response header mapping
    작성 날짜: 2026/08/21
    """
    descriptor = server.descriptor
    connection = HTTPConnection("127.0.0.1", descriptor.port, timeout=3.0)
    request_headers = {
        "Host": host or f"127.0.0.1:{descriptor.port}",
        "Origin": origin,
        "Authorization": f"Bearer {token}",
    }
    if request_id is not None:
        request_headers["X-Request-Id"] = request_id
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    if idempotency_key is not None:
        request_headers["Idempotency-Key"] = idempotency_key
    if extra_headers is not None:
        request_headers.update(extra_headers)

    connection.request(
        method,
        path,
        body=None if body is None else body.encode("utf-8"),
        headers=request_headers,
    )
    response = connection.getresponse()
    response_bytes = response.read()
    response_headers = {
        header_name.lower(): header_value
        for header_name, header_value in response.getheaders()
    }
    connection.close()
    parsed_body = (
        None
        if not response_bytes
        else json.loads(response_bytes.decode("utf-8"))
    )
    return response.status, parsed_body, response_headers


class LoopbackHttpServerTests(unittest.TestCase):
    """
    클래스 이름: LoopbackHttpServerTests
    기능: actual HTTP network path의 loopback security와 endpoint 계약을 검증한다.
    작성 날짜: 2026/08/21
    """

    def setUp(self) -> None:
        """
        함수 이름: setUp()
        기능: 각 test에 새 random token, runtime과 random-port server를 시작한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.token = secrets.token_urlsafe(32)
        self.runtime = _prepare_runtime(ready=False)
        self.server = LoopbackTransportServer(
            self.runtime,
            self.token,
            allowed_origins=(TEST_ORIGIN,),
        )
        self.server.start()

    def tearDown(self) -> None:
        """
        함수 이름: tearDown()
        기능: 각 test의 HTTP server와 event waiter를 멱등 종료한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.server.stop()

    def test_health_uses_random_loopback_port_and_common_envelope(self) -> None:
        """
        함수 이름: test_health_uses_random_loopback_port_and_common_envelope()
        기능: actual random port health 응답에 secret 없이 session/schema/readiness가 있는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        request_id = str(uuid4())
        status, payload, headers = _request_json(
            self.server,
            self.token,
            "GET",
            "/v1/health",
            request_id=request_id,
        )

        self.assertGreater(self.server.descriptor.port, 0)
        self.assertEqual(status, 200)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["request_id"], request_id)
        self.assertFalse(payload["data"]["ready"])
        self.assertEqual(
            payload["data"]["session_id"],
            self.server.descriptor.session_id,
        )
        self.assertNotIn(self.token, json.dumps(payload))
        self.assertEqual(headers["access-control-allow-origin"], TEST_ORIGIN)
        self.assertEqual(headers["connection"], "close")

    def test_authentication_host_origin_and_request_id_fail_closed(self) -> None:
        """
        함수 이름: test_authentication_host_origin_and_request_id_fail_closed()
        기능: 잘못된 Bearer, Host, Origin과 누락 UUID가 공통 typed failure인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        valid_request_id = str(uuid4())
        bad_auth_status, bad_auth_payload, _ = _request_json(
            self.server,
            secrets.token_urlsafe(32),
            "GET",
            "/v1/health",
            request_id=valid_request_id,
        )
        bad_host_status, _, _ = _request_json(
            self.server,
            self.token,
            "GET",
            "/v1/health",
            request_id=valid_request_id,
            host="localhost:1",
        )
        bad_origin_status, _, _ = _request_json(
            self.server,
            self.token,
            "GET",
            "/v1/health",
            request_id=valid_request_id,
            origin="http://evil.example",
        )
        missing_id_status, missing_id_payload, _ = _request_json(
            self.server,
            self.token,
            "GET",
            "/v1/health",
        )

        self.assertEqual(bad_auth_status, 401)
        self.assertEqual(
            bad_auth_payload["error"]["code"],
            "AUTHENTICATION_REQUIRED",
        )
        self.assertNotIn(self.token, json.dumps(bad_auth_payload))
        self.assertEqual(bad_host_status, 403)
        self.assertEqual(bad_origin_status, 403)
        self.assertEqual(missing_id_status, 400)
        self.assertEqual(missing_id_payload["error"]["code"], "MALFORMED_REQUEST")

    def test_snapshot_is_503_before_ready_and_atomic_after_ready(self) -> None:
        """
        함수 이름: test_snapshot_is_503_before_ready_and_atomic_after_ready()
        기능: ready 이전 snapshot을 막고 state와 last_sequence를 application lock 안에서 읽는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        request_id = str(uuid4())
        not_ready_status, not_ready_payload, _ = _request_json(
            self.server,
            self.token,
            "GET",
            "/v1/snapshot",
            request_id=request_id,
        )
        self.assertEqual(not_ready_status, 503)
        self.assertEqual(
            not_ready_payload["error"]["code"],
            "BACKEND_NOT_READY",
        )

        # 별도 server에서 tracking lock과 sequence read의 lock order를 관찰한다.
        self.server.stop()
        tracking_lock = _TrackingLock()
        self.runtime.application_lock = tracking_lock
        self.runtime.ready = True
        self.runtime.state.status = "READY"
        checking_stream = _LockCheckingEventStream(tracking_lock)
        checking_stream.publish("ACCOUNT_UPDATED", {"version": 1})
        self.server = LoopbackTransportServer(
            self.runtime,
            self.token,
            allowed_origins=(TEST_ORIGIN,),
            event_stream=checking_stream,
        )
        self.server.start()
        ready_status, ready_payload, _ = _request_json(
            self.server,
            self.token,
            "GET",
            "/v1/snapshot",
            request_id=str(uuid4()),
        )

        self.assertEqual(ready_status, 200)
        self.assertEqual(ready_payload["data"]["last_sequence"], 1)
        self.assertTrue(checking_stream.sequence_read_inside_lock)
        self.assertEqual(
            ready_payload["data"]["account"]["quote_asset"],
            "USDT",
        )

    def test_command_json_schema_and_idempotency_are_strict(self) -> None:
        """
        함수 이름: test_command_json_schema_and_idempotency_are_strict()
        기능: duplicate/NaN/schema 오류와 same-key replay/conflict를 actual HTTP로 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.runtime.ready = True
        self.runtime.state.status = "READY"
        idempotency_key = str(uuid4())

        duplicate_status, duplicate_payload, _ = _request_json(
            self.server,
            self.token,
            "POST",
            "/v1/trading/start",
            body='{"schema_version":1,"schema_version":1}',
            request_id=str(uuid4()),
            idempotency_key=str(uuid4()),
        )
        nan_status, _, _ = _request_json(
            self.server,
            self.token,
            "POST",
            "/v1/trading/start",
            body='{"schema_version":1,"value":NaN}',
            request_id=str(uuid4()),
            idempotency_key=str(uuid4()),
        )
        schema_status, schema_payload, _ = _request_json(
            self.server,
            self.token,
            "POST",
            "/v1/trading/start",
            body='{"schema_version":2}',
            request_id=str(uuid4()),
            idempotency_key=str(uuid4()),
        )
        boolean_schema_status, boolean_schema_payload, _ = _request_json(
            self.server,
            self.token,
            "POST",
            "/v1/trading/start",
            body='{"schema_version":true}',
            request_id=str(uuid4()),
            idempotency_key=str(uuid4()),
        )
        first_status, first_payload, _ = _request_json(
            self.server,
            self.token,
            "POST",
            "/v1/trading/start",
            body='{"schema_version":1}',
            request_id=str(uuid4()),
            idempotency_key=idempotency_key,
        )
        second_status, second_payload, _ = _request_json(
            self.server,
            self.token,
            "POST",
            "/v1/trading/start",
            body='{"schema_version":1}',
            request_id=str(uuid4()),
            idempotency_key=idempotency_key,
        )
        conflict_status, conflict_payload, _ = _request_json(
            self.server,
            self.token,
            "POST",
            "/v1/trading/start",
            body='{"schema_version":1,"expected_version":0}',
            request_id=str(uuid4()),
            idempotency_key=idempotency_key,
        )

        self.assertEqual(duplicate_status, 400)
        self.assertEqual(duplicate_payload["error"]["code"], "MALFORMED_REQUEST")
        self.assertEqual(nan_status, 400)
        self.assertEqual(schema_status, 400)
        self.assertEqual(
            schema_payload["error"]["code"],
            "UNSUPPORTED_SCHEMA_VERSION",
        )
        self.assertEqual(boolean_schema_status, 400)
        self.assertEqual(
            boolean_schema_payload["error"]["code"],
            "UNSUPPORTED_SCHEMA_VERSION",
        )
        self.assertEqual(first_status, 503)
        self.assertEqual(first_payload["error"]["code"], "FEATURE_NOT_AVAILABLE")
        self.assertEqual(second_status, first_status)
        self.assertEqual(second_payload, first_payload)
        self.assertEqual(conflict_status, 409)
        self.assertEqual(
            conflict_payload["error"]["code"],
            "IDEMPOTENCY_CONFLICT",
        )

    def test_concurrent_same_key_executes_route_once(self) -> None:
        """
        함수 이름: test_concurrent_same_key_executes_route_once()
        기능: 동일 key/body 동시 command가 single-flight lock 아래 route를 한 번만 실행하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.runtime.ready = True
        self.runtime.state.status = "READY"
        idempotency_key = str(uuid4())
        original_route = self.server._route_http_request
        route_call_count = 0
        route_count_lock = RLock()

        def counting_route(
            endpoint_key: tuple[str, str],
            request_id: str,
        ) -> object:
            """
            함수 이름: counting_route()
            기능: 동시 요청이 실제 route에 진입한 횟수를 기록하고 경합 시간을 만든다.
            인자: endpoint_key -> command method/path
                request_id -> request UUID
            반환값: 원래 route 응답
            작성 날짜: 2026/08/21
            """
            nonlocal route_call_count
            with route_count_lock:
                route_call_count += 1
            sleep(0.1)
            return original_route(endpoint_key, request_id)

        self.server._route_http_request = counting_route  # type: ignore[method-assign]
        responses: list[tuple[int, dict[str, object] | None, dict[str, str]]] = []

        def send_command() -> None:
            """
            함수 이름: send_command()
            기능: 동일 idempotency key와 body의 actual HTTP command를 전송한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/08/21
            """
            responses.append(
                _request_json(
                    self.server,
                    self.token,
                    "POST",
                    "/v1/trading/start",
                    body='{"schema_version":1}',
                    request_id=str(uuid4()),
                    idempotency_key=idempotency_key,
                )
            )

        command_threads = [Thread(target=send_command) for _ in range(2)]
        for command_thread in command_threads:
            command_thread.start()
        for command_thread in command_threads:
            command_thread.join(timeout=3.0)

        self.assertEqual(route_call_count, 1)
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0][1], responses[1][1])

    def test_unknown_method_query_and_cors_preflight_fail_closed(self) -> None:
        """
        함수 이름: test_unknown_method_query_and_cors_preflight_fail_closed()
        기능: HEAD/PUT와 forbidden query가 stdlib HTML이 아닌 envelope이며 exact preflight만 허용되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        request_id = str(uuid4())
        put_status, put_payload, _ = _request_json(
            self.server,
            self.token,
            "PUT",
            "/v1/health",
            request_id=request_id,
        )
        head_status, head_payload, head_headers = _request_json(
            self.server,
            self.token,
            "HEAD",
            "/v1/health",
            request_id=str(uuid4()),
        )
        query_status, query_payload, _ = _request_json(
            self.server,
            self.token,
            "GET",
            "/v1/snapshot?unexpected=1",
            request_id=str(uuid4()),
        )
        preflight_status, preflight_payload, preflight_headers = _request_json(
            self.server,
            self.token,
            "OPTIONS",
            "/v1/health",
            extra_headers={
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization, X-Request-Id",
            },
        )
        bad_preflight_status, _, _ = _request_json(
            self.server,
            self.token,
            "OPTIONS",
            "/v1/health",
            extra_headers={
                "Access-Control-Request-Method": "POST",
            },
        )
        query_preflight_status, query_preflight_payload, _ = _request_json(
            self.server,
            self.token,
            "OPTIONS",
            "/v1/health?unexpected=1",
            extra_headers={
                "Access-Control-Request-Method": "GET",
            },
        )

        self.assertEqual(put_status, 405)
        self.assertEqual(put_payload["error"]["code"], "METHOD_NOT_ALLOWED")
        self.assertEqual(head_status, 405)
        self.assertIsNone(head_payload)
        self.assertGreater(int(head_headers["content-length"]), 0)
        self.assertEqual(query_status, 400)
        self.assertEqual(query_payload["error"]["code"], "MALFORMED_REQUEST")
        self.assertEqual(preflight_status, 204)
        self.assertIsNone(preflight_payload)
        self.assertEqual(
            preflight_headers["access-control-allow-origin"],
            TEST_ORIGIN,
        )
        self.assertEqual(bad_preflight_status, 403)
        self.assertEqual(query_preflight_status, 400)
        self.assertEqual(
            query_preflight_payload["error"]["code"],
            "MALFORMED_REQUEST",
        )

    def test_top_level_composition_shares_account_event_stream(self) -> None:
        """
        함수 이름: test_top_level_composition_shares_account_event_stream()
        기능: runtime factory observer와 server가 같은 stream에 Account.version event를 발행하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        captured_observers = []
        runtime = _prepare_runtime(ready=True)

        def runtime_factory(account_observer: object) -> SimpleNamespace:
            """
            함수 이름: runtime_factory()
            기능: transport가 만든 account observer를 캡처하고 ready runtime을 반환한다.
            인자: account_observer -> bootstrap factory에 주입할 callback
            반환값: ready runtime test double
            작성 날짜: 2026/08/21
            """
            captured_observers.append(account_observer)
            return runtime

        transport_application = create_loopback_transport_application(
            runtime_factory,
            secrets.token_urlsafe(32),
            allowed_origins=(TEST_ORIGIN,),
            start_runtime=lambda current_runtime: current_runtime,
            close_runtime=lambda current_runtime: current_runtime,
        )
        account = runtime.trading_controller.account
        published_event = captured_observers[0](account)

        try:
            self.assertIs(
                transport_application.server.event_stream,
                transport_application.event_stream,
            )
            self.assertEqual(published_event.event_type, "ACCOUNT_UPDATED")
            self.assertEqual(published_event.aggregate_version, account.version)
            self.assertEqual(
                published_event.payload["account"]["version"],
                account.version,
            )
        finally:
            transport_application.stop()


class LoopbackCompositionFailureTests(unittest.TestCase):
    """
    클래스 이름: LoopbackCompositionFailureTests
    기능: startup 이후 transport assembly 실패가 runtime과 event stream을 정리하는지 검증한다.
    작성 날짜: 2026/08/21
    """

    def test_server_construction_failure_closes_runtime_and_observer_stream(
        self,
    ) -> None:
        """
        함수 이름: test_server_construction_failure_closes_runtime_and_observer_stream()
        기능: bind/server 조립 실패 시 opened runtime과 captured account event stream을 닫는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        runtime = _prepare_runtime(ready=True)
        captured_observers = []
        closed_runtimes = []

        def runtime_factory(account_observer: object) -> SimpleNamespace:
            """
            함수 이름: runtime_factory()
            기능: failure test가 event stream closure를 확인할 observer를 캡처한다.
            인자: account_observer -> transport가 만든 account callback
            반환값: ready runtime test double
            작성 날짜: 2026/08/21
            """
            captured_observers.append(account_observer)
            return runtime

        with patch(
            "binance_auto_trader.transport.app.LoopbackTransportServer",
            side_effect=OSError("controlled bind failure"),
        ), self.assertRaisesRegex(OSError, "controlled bind failure"):
            create_loopback_transport_application(
                runtime_factory,
                secrets.token_urlsafe(32),
                allowed_origins=(TEST_ORIGIN,),
                start_runtime=lambda current_runtime: current_runtime,
                close_runtime=closed_runtimes.append,
            )

        self.assertEqual(closed_runtimes, [runtime])
        with self.assertRaisesRegex(RuntimeError, "closed"):
            captured_observers[0](runtime.trading_controller.account)

    def test_startup_without_ready_postcondition_closes_runtime_and_stream(
        self,
    ) -> None:
        """
        함수 이름: test_startup_without_ready_postcondition_closes_runtime_and_stream()
        기능: startup callback 반환 뒤에도 runtime이 not-ready면 descriptor 조립 없이 정리하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        runtime = _prepare_runtime(ready=False)
        captured_observers = []
        closed_runtimes = []

        def runtime_factory(account_observer: object) -> SimpleNamespace:
            """
            함수 이름: runtime_factory()
            기능: ready 사후조건 failure가 정리할 shared observer와 runtime을 제공한다.
            인자: account_observer -> transport가 만든 account callback
            반환값: not-ready runtime test double
            작성 날짜: 2026/08/21
            """
            captured_observers.append(account_observer)
            return runtime

        with self.assertRaisesRegex(RuntimeError, "ready application"):
            create_loopback_transport_application(
                runtime_factory,
                secrets.token_urlsafe(32),
                allowed_origins=(TEST_ORIGIN,),
                start_runtime=lambda current_runtime: current_runtime,
                close_runtime=closed_runtimes.append,
            )

        self.assertEqual(closed_runtimes, [runtime])
        with self.assertRaisesRegex(RuntimeError, "closed"):
            captured_observers[0](runtime.trading_controller.account)


if __name__ == "__main__":
    unittest.main()
