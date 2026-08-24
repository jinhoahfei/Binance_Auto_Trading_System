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

from binance_auto_trader.domain.history import (
    CSVExportOptions,
    CSVExportResult,
)
from binance_auto_trader.transport import (
    BackendEventStream,
    LoopbackTransportServer,
    create_account_update_observer,
    create_loopback_transport_application,
)

from tests.unit.transport.test_contracts import _create_ready_runtime


TEST_ORIGIN = "http://127.0.0.1:5173"


def _encode_csv_export_body(file_name: str) -> str:
    """
    함수 이름: _encode_csv_export_body()
    기능: actual HTTP test에 사용할 Phase 11 exact CSV command JSON을 생성한다.
    인자: file_name -> valid command와 conflict command를 구분할 안전한 basename
    반환값: schema version 2의 완전한 CSV export JSON 문자열
    작성 날짜: 2026/08/23
    """
    # 실제 command parser가 요구하는 여섯 option field와 schema version을 모두 제공한다.
    request_payload = {
        "schema_version": 2,
        "directory": "/tmp",
        "file_name": file_name,
        "period": "custom",
        "start_date": "2026-08-23",
        "end_date": "2026-08-23",
        "timezone": "Asia/Seoul",
    }

    return json.dumps(
        request_payload,
        separators=(",", ":"),
        sort_keys=True,
    )  # Idempotency fingerprint가 요청 순서가 아닌 canonical test body에 고정된다.


class _SuccessfulCSVExportController:
    """
    클래스 이름: _SuccessfulCSVExportController
    기능: actual HTTP command가 strict option을 전달하면 typed 성공 receipt를 반환한다.
    작성 날짜: 2026/08/23
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 실제 route 실행 횟수와 전달된 canonical option을 기록할 목록을 준비한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        self.received_options: list[CSVExportOptions] = []

    def export_csv(self, options: CSVExportOptions) -> CSVExportResult:
        """
        함수 이름: export_csv()
        기능: 전달 option을 기록하고 transport mapper가 검증할 typed receipt를 반환한다.
        인자: options -> strict request DTO에서 복원한 canonical CSV option
        반환값: absolute path와 양수 row count를 가진 CSVExportResult
        작성 날짜: 2026/08/23
        """
        # Route가 동형 dictionary가 아니라 domain option을 넘겼는지 기록과 검증으로 고정한다.
        if not isinstance(options, CSVExportOptions):
            raise TypeError("options must be CSVExportOptions")
        self.received_options.append(options)

        return CSVExportResult(
            file_path=f"/tmp/{options.file_name}",
            exported_row_count=2,
        )  # HTTP test는 filesystem 대신 이미 게시가 끝난 typed 결과 경계를 재현한다.


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
        # READY runtime과 동일 command를 구분할 stable key·request UUID를 준비한다.
        self.runtime.ready = True
        self.runtime.state.status = "READY"
        idempotency_key = str(uuid4())
        first_request_id = str(uuid4())
        second_request_id = str(uuid4())

        # 중복 key, NaN, schema와 필수 field 오류를 실제 command parser에 각각 전달한다.
        duplicate_status, duplicate_payload, _ = _request_json(
            self.server,
            self.token,
            "POST",
            "/v1/trading/start",
            body='{"schema_version":2,"schema_version":2}',
            request_id=str(uuid4()),
            idempotency_key=str(uuid4()),
        )
        nan_status, _, _ = _request_json(
            self.server,
            self.token,
            "POST",
            "/v1/trading/start",
            body='{"schema_version":2,"value":NaN}',
            request_id=str(uuid4()),
            idempotency_key=str(uuid4()),
        )
        schema_status, schema_payload, _ = _request_json(
            self.server,
            self.token,
            "POST",
            "/v1/trading/start",
            body='{"schema_version":1}',
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
        missing_version_status, missing_version_payload, _ = _request_json(
            self.server,
            self.token,
            "POST",
            "/v1/trading/start",
            body='{"schema_version":2}',
            request_id=str(uuid4()),
            idempotency_key=str(uuid4()),
        )
        # 실제 Phase 11 DTO와 typed receipt를 사용해 same-body replay와 body conflict를 구분한다.
        csv_export_controller = _SuccessfulCSVExportController()
        self.runtime.trade_history_controller = csv_export_controller
        csv_export_body = _encode_csv_export_body("http-idempotent.csv")
        first_status, first_payload, _ = _request_json(
            self.server,
            self.token,
            "POST",
            "/v1/csv-exports",
            body=csv_export_body,
            request_id=first_request_id,
            idempotency_key=idempotency_key,
        )
        second_status, second_payload, _ = _request_json(
            self.server,
            self.token,
            "POST",
            "/v1/csv-exports",
            body=csv_export_body,
            request_id=second_request_id,
            idempotency_key=idempotency_key,
        )
        conflict_status, conflict_payload, _ = _request_json(
            self.server,
            self.token,
            "POST",
            "/v1/csv-exports",
            body=_encode_csv_export_body("http-conflict.csv"),
            request_id=str(uuid4()),
            idempotency_key=idempotency_key,
        )

        # Malformed DTO가 공통 schema error code로 fail closed하는지 확인한다.
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
        self.assertEqual(missing_version_status, 400)
        self.assertEqual(
            missing_version_payload["error"]["code"],
            "MALFORMED_REQUEST",
        )
        # Replay는 현재 request ID를 쓰되 첫 typed receipt를 보존하고 다른 body는 거부한다.
        self.assertEqual(first_status, 201)
        self.assertTrue(first_payload["ok"])
        self.assertEqual(
            first_payload["data"],
            {
                "file_path": "/tmp/http-idempotent.csv",
                "exported_row_count": 2,
            },
        )
        self.assertEqual(second_status, first_status)
        self.assertEqual(first_payload["request_id"], first_request_id)
        self.assertEqual(second_payload["request_id"], second_request_id)
        self.assertEqual(second_payload["data"], first_payload["data"])
        self.assertEqual(len(csv_export_controller.received_options), 1)
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
        # 두 동시 요청이 공유할 READY runtime, typed CSV owner와 route counter를 준비한다.
        self.runtime.ready = True
        self.runtime.state.status = "READY"
        csv_export_controller = _SuccessfulCSVExportController()
        self.runtime.trade_history_controller = csv_export_controller
        csv_export_body = _encode_csv_export_body("http-concurrent.csv")
        idempotency_key = str(uuid4())
        original_route = self.server._route_http_request
        route_call_count = 0
        route_count_lock = RLock()

        def counting_route(
            endpoint_key: tuple[str, str],
            request_id: str,
            *,
            request_body: object | None = None,
            command_id: str | None = None,
        ) -> object:
            """
            함수 이름: counting_route()
            기능: 동시 요청이 실제 route에 진입한 횟수를 기록하고 경합 시간을 만든다.
            인자: endpoint_key -> command method/path
                request_id -> request UUID
                request_body -> dispatcher가 전달한 command DTO
                command_id -> dispatcher가 전달한 idempotency command ID
            반환값: 원래 route 응답
            작성 날짜: 2026/08/21
            """
            nonlocal route_call_count

            # 실제 route 진입 횟수를 lock 아래 기록한 뒤 두 client의 경합 창을 만든다.
            with route_count_lock:
                route_call_count += 1
            sleep(0.1)

            # Counting wrapper가 받은 Phase 7 확장 인자를 원래 dispatcher에 그대로 전달한다.
            return original_route(
                endpoint_key,
                request_id,
                request_body=request_body,  # type: ignore[arg-type]
                command_id=command_id,
            )

        # Counting wrapper를 server에 설치하고 두 thread의 HTTP 응답을 한 목록에 수집한다.
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
                    "/v1/csv-exports",
                    body=csv_export_body,
                    request_id=str(uuid4()),
                    idempotency_key=idempotency_key,
                )
            )

        # 동일 key와 body의 command를 동시에 시작하고 제한 시간 안에 모두 합류시킨다.
        command_threads = [Thread(target=send_command) for _ in range(2)]
        for command_thread in command_threads:
            command_thread.start()
        for command_thread in command_threads:
            command_thread.join(timeout=3.0)

        # Single-flight lock이 route 한 번과 동일한 terminal 결과를 보장하는지 확인한다.
        self.assertEqual(route_call_count, 1)
        self.assertEqual(len(responses), 2)
        self.assertEqual(len(csv_export_controller.received_options), 1)
        self.assertTrue(
            all(response_status == 201 for response_status, _, _ in responses)
        )  # 두 client 모두 최초 created receipt의 replay를 받아야 한다.

        # 멱등 replay도 각 HTTP 요청의 correlation ID를 유지하되 나머지 결과는 같아야 한다.
        first_payload = dict(responses[0][1] or {})
        second_payload = dict(responses[1][1] or {})
        first_request_id = first_payload.pop("request_id")
        second_request_id = second_payload.pop("request_id")
        self.assertNotEqual(first_request_id, second_request_id)
        self.assertEqual(first_payload, second_payload)

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
        trade_preflight_status, trade_preflight_payload, _ = _request_json(
            self.server,
            self.token,
            "OPTIONS",
            "/v1/trades?period=today&side=all",
            extra_headers={
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization, X-Request-Id",
            },
        )
        malformed_trade_preflight_status, malformed_trade_preflight_payload, _ = (
            _request_json(
                self.server,
                self.token,
                "OPTIONS",
                "/v1/trades?period=today&period=all",
                extra_headers={
                    "Access-Control-Request-Method": "GET",
                },
            )
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
        self.assertEqual(trade_preflight_status, 204)
        self.assertIsNone(trade_preflight_payload)
        self.assertEqual(malformed_trade_preflight_status, 400)
        self.assertEqual(
            malformed_trade_preflight_payload["error"]["code"],
            "MALFORMED_REQUEST",
        )

    def test_top_level_composition_shares_application_event_stream(self) -> None:
        """
        함수 이름: test_top_level_composition_shares_application_event_stream()
        기능: Account, history와 trading observer가 server의 같은 sequence stream을 공유하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        captured_account_observers = []
        captured_trade_history_observers = []
        captured_trading_session_observers = []
        runtime = _prepare_runtime(ready=True)

        def runtime_factory(
            account_observer: object,
            trade_history_observer: object,
            trading_session_observer: object,
        ) -> SimpleNamespace:
            """
            함수 이름: runtime_factory()
            기능: transport가 만든 세 observer를 캡처하고 ready runtime을 반환한다.
            인자: account_observer -> bootstrap factory에 주입할 Account callback
                trade_history_observer -> bootstrap factory에 주입할 history callback
                trading_session_observer -> bootstrap factory에 주입할 trading callback
            반환값: ready runtime test double
            작성 날짜: 2026/08/23
            """
            captured_account_observers.append(account_observer)
            captured_trade_history_observers.append(
                trade_history_observer
            )  # History observer도 동일 factory에 주입한다.
            captured_trading_session_observers.append(
                trading_session_observer
            )  # Trading lifecycle publication도 같은 stream callback을 보존한다.
            return runtime

        transport_application = create_loopback_transport_application(
            runtime_factory,
            secrets.token_urlsafe(32),
            allowed_origins=(TEST_ORIGIN,),
            start_runtime=lambda current_runtime: current_runtime,
            close_runtime=lambda current_runtime: current_runtime,
        )
        account = runtime.trading_controller.account
        published_account_event = captured_account_observers[0](account)
        trade = runtime.trade_history_controller.trade_history.trades[0]
        performance = runtime.trade_history_controller.performance
        published_history_events = captured_trade_history_observers[0](
            trade,
            performance,
        )
        published_trading_event = captured_trading_session_observers[0](
            runtime.trading_controller,
            runtime.execution_mode,
        )

        try:
            self.assertIs(
                transport_application.server.event_stream,
                transport_application.event_stream,
            )
            self.assertEqual(
                published_account_event.event_type,
                "ACCOUNT_UPDATED",
            )
            self.assertEqual(
                published_account_event.aggregate_version,
                account.version,
            )
            self.assertEqual(
                published_account_event.payload["account"]["version"],
                account.version,
            )
            self.assertEqual(
                tuple(
                    event.event_type
                    for event in published_history_events
                ),
                ("ORDER_EXECUTED", "PERFORMANCE_UPDATED"),
            )
            self.assertEqual(
                tuple(
                    event.sequence
                    for event in published_history_events
                ),
                (2, 3),
            )  # Account 뒤에 history event 두 개가 gap 없이 연속된다.
            self.assertEqual(
                published_trading_event.event_type,
                "TRADING_SESSION_UPDATED",
            )
            self.assertEqual(published_trading_event.sequence, 4)
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

        def runtime_factory(
            account_observer: object,
            trade_history_observer: object,
            trading_session_observer: object,
        ) -> SimpleNamespace:
            """
            함수 이름: runtime_factory()
            기능: failure test가 event stream closure를 확인할 observer를 캡처한다.
            인자: account_observer -> transport가 만든 account callback
                trade_history_observer -> transport가 만든 history callback
                trading_session_observer -> transport가 만든 trading callback
            반환값: ready runtime test double
            작성 날짜: 2026/08/21
            """
            captured_observers.append(account_observer)
            self.assertTrue(callable(trade_history_observer))  # 실패 정리 대상 stream을 함께 공유한다.
            self.assertTrue(callable(trading_session_observer))
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

        def runtime_factory(
            account_observer: object,
            trade_history_observer: object,
            trading_session_observer: object,
        ) -> SimpleNamespace:
            """
            함수 이름: runtime_factory()
            기능: ready 사후조건 failure가 정리할 shared observer와 runtime을 제공한다.
            인자: account_observer -> transport가 만든 account callback
                trade_history_observer -> transport가 만든 history callback
                trading_session_observer -> transport가 만든 trading callback
            반환값: not-ready runtime test double
            작성 날짜: 2026/08/21
            """
            captured_observers.append(account_observer)
            self.assertTrue(callable(trade_history_observer))  # readiness 실패에서도 두 callback을 열지 않는다.
            self.assertTrue(callable(trading_session_observer))
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
