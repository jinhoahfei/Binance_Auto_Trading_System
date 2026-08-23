"""CSV export transport parser, mapper와 route의 strict 성공·실패 계약을 검증한다."""

from datetime import date
from threading import RLock
from types import SimpleNamespace
import unittest

from binance_auto_trader.domain.history import (
    CSVExportIOError,
    CSVExportOptions,
    CSVExportResult,
    CSVPeriod,
    DestinationExistsError,
    NoTradesToExportError,
)
from binance_auto_trader.transport.contracts import (
    SCHEMA_VERSION,
    TransportContractError,
    map_csv_export_result,
    parse_csv_export_options,
)
from binance_auto_trader.transport.event_stream import BackendEventStream
from binance_auto_trader.transport.routes import RouteContext
from binance_auto_trader.transport.routes.csv_export import create_csv_export


TEST_REQUEST_ID = "3c73d583-c1c8-4830-8393-cc31639a40fd"
TEST_COMMAND_ID = "f5a4f621-25f8-4dd2-bfb7-1b80e9561423"


def _create_valid_request_body() -> dict[str, object]:
    """
    함수 이름: _create_valid_request_body()
    기능: parser와 route test가 공유할 exact Phase 11 CSV command body를 만든다.
    인자: 없음
    반환값: schema version과 CSV option 전체를 담은 JSON object
    작성 날짜: 2026/08/23
    """
    # UI adapter가 전송하는 key와 wire value를 별도 default 없이 그대로 고정한다.
    return {
        "schema_version": SCHEMA_VERSION,
        "directory": "/Users/oscar/Exports",
        "file_name": "phase11-trades.csv",
        "period": "custom",
        "start_date": "2026-08-17",
        "end_date": "2026-08-23",
        "timezone": "Asia/Seoul",
    }


class _TrackingLock:
    """
    클래스 이름: _TrackingLock
    기능: CSV route가 readiness만 application lock에서 읽고 파일 작업 전에 해제하는지 관찰한다.
    작성 날짜: 2026/08/23
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 내부 재진입 lock과 현재 진입 깊이를 초기화한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        self._lock = RLock()
        self._depth = 0

    @property
    def held(self) -> bool:
        """
        함수 이름: held()
        기능: 현재 test thread가 application lock에 진입했는지 반환한다.
        인자: 없음
        반환값: lock이 한 번 이상 잡혔으면 True
        작성 날짜: 2026/08/23
        """
        return self._depth > 0  # Controller 호출 순간의 global lock 소유 여부를 직접 관찰한다.

    def __enter__(self) -> object:
        """
        함수 이름: __enter__()
        기능: 재진입 lock을 획득하고 진입 깊이를 증가시킨다.
        인자: 없음
        반환값: lock context 자신
        작성 날짜: 2026/08/23
        """
        self._lock.acquire()
        self._depth += 1
        return self

    def __exit__(
        self,
        exception_type: object,
        exception_value: object,
        traceback: object,
    ) -> None:
        """
        함수 이름: __exit__()
        기능: 진입 깊이를 감소시키고 내부 lock을 해제한다.
        인자: exception_type -> 발생한 예외 타입 또는 None
            exception_value -> 발생한 예외 값 또는 None
            traceback -> 발생한 traceback 또는 None
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        self._depth -= 1
        self._lock.release()


class _CsvExportController:
    """
    클래스 이름: _CsvExportController
    기능: CSV route의 canonical option, lock 경계와 설정된 결과·오류를 기록하는 test double이다.
    작성 날짜: 2026/08/23
    """

    def __init__(
        self,
        application_lock: _TrackingLock,
        *,
        result: object | None = None,
        error: Exception | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 성공 결과 또는 발생시킬 오류와 호출 기록을 초기화한다.
        인자: application_lock -> route의 global lock 소유 여부를 관찰할 lock
            result -> 성공 시 반환할 CSVExportResult 대역
            error -> export 진입 시 발생시킬 optional 예외
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        self._application_lock = application_lock
        self._result = result
        self._error = error
        self.calls: list[CSVExportOptions] = []

    def export_csv(self, options: CSVExportOptions) -> object:
        """
        함수 이름: export_csv()
        기능: canonical CSV option을 기록하고 설정된 결과를 반환하거나 오류를 발생시킨다.
        인자: options -> transport parser가 복원한 CSVExportOptions
        반환값: 설정된 export 결과 대역
        작성 날짜: 2026/08/23
        """
        if self._application_lock.held:
            raise AssertionError("CSV export must not hold the application lock")

        self.calls.append(options)  # Parser가 만든 domain option을 변형 없이 기록한다.
        if self._error is not None:
            raise self._error
        return self._result


def _create_route_context(
    *,
    ready: bool = True,
    result: object | None = None,
    error: Exception | None = None,
) -> tuple[RouteContext, _CsvExportController]:
    """
    함수 이름: _create_route_context()
    기능: readiness와 CSV Controller 결과를 선택할 수 있는 route context를 만든다.
    인자: ready -> application startup 완료 여부
        result -> Controller가 반환할 optional 결과
        error -> Controller가 발생시킬 optional 예외
    반환값: RouteContext와 호출 기록용 Controller tuple
    작성 날짜: 2026/08/23
    """
    application_lock = _TrackingLock()
    controller = _CsvExportController(
        application_lock,
        result=result,
        error=error,
    )
    runtime = SimpleNamespace(
        application_lock=application_lock,
        ready=ready,
        trade_history_controller=controller,
    )

    # RouteContext의 event stream 타입 검증을 충족하되 CSV route에는 event를 발행하지 않는다.
    return RouteContext(runtime, BackendEventStream()), controller


class CsvExportContractTests(unittest.TestCase):
    """
    클래스 이름: CsvExportContractTests
    기능: CSV command parser와 success mapper의 exact wire 계약을 검증한다.
    작성 날짜: 2026/08/23
    """

    def test_parser_restores_every_canonical_period_without_defaults(self) -> None:
        """
        함수 이름: test_parser_restores_every_canonical_period_without_defaults()
        기능: 네 wire period를 exact domain enum과 LocalDate option으로 복원한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        period_pairs = (
            ("today", CSVPeriod.TODAY),
            ("last7days", CSVPeriod.WEEKLY),
            ("last30days", CSVPeriod.MONTHLY),
            ("custom", CSVPeriod.CUSTOM),
        )

        # 요청 날짜는 preset에서도 parser가 임의로 바꾸지 않고 Controller가 KST clock으로 확정한다.
        for period_wire, expected_period in period_pairs:
            with self.subTest(period=period_wire):
                request_body = _create_valid_request_body()
                request_body["period"] = period_wire

                options = parse_csv_export_options(request_body)

                self.assertIs(options.period, expected_period)
                self.assertEqual(options.save_location, "/Users/oscar/Exports")
                self.assertEqual(options.file_name, "phase11-trades.csv")
                self.assertEqual(options.start_date, date(2026, 8, 17))
                self.assertEqual(options.end_date, date(2026, 8, 23))

    def test_parser_rejects_shape_scalar_date_timezone_and_domain_errors(
        self,
    ) -> None:
        """
        함수 이름: test_parser_rejects_shape_scalar_date_timezone_and_domain_errors()
        기능: exact key, schema, string, calendar, timezone, filename과 date-range 실패를 typed 응답으로 구분한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        invalid_cases = (
            (
                "missing directory",
                lambda body: body.pop("directory"),
                "MALFORMED_REQUEST",
                400,
                None,
            ),
            (
                "unknown key",
                lambda body: body.update({"future_value": "unexpected"}),
                "MALFORMED_REQUEST",
                400,
                None,
            ),
            (
                "schema mismatch",
                lambda body: body.update({"schema_version": SCHEMA_VERSION + 1}),
                "UNSUPPORTED_SCHEMA_VERSION",
                400,
                None,
            ),
            (
                "non-string directory",
                lambda body: body.update({"directory": 7}),
                "INVALID_CSV_EXPORT_OPTIONS",
                422,
                "directory",
            ),
            (
                "unsupported period",
                lambda body: body.update({"period": "WEEKLY"}),
                "INVALID_CSV_EXPORT_OPTIONS",
                422,
                "period",
            ),
            (
                "wrong timezone",
                lambda body: body.update({"timezone": "UTC"}),
                "INVALID_CSV_EXPORT_OPTIONS",
                422,
                "timezone",
            ),
            (
                "invalid calendar date",
                lambda body: body.update({"start_date": "2026-02-30"}),
                "INVALID_CSV_EXPORT_OPTIONS",
                422,
                "start_date",
            ),
            (
                "filename traversal",
                lambda body: body.update({"file_name": "../secret.csv"}),
                "INVALID_CSV_EXPORT_OPTIONS",
                422,
                "file_name",
            ),
            (
                "directory whitespace",
                lambda body: body.update({"directory": " /private/export"}),
                "INVALID_CSV_EXPORT_OPTIONS",
                422,
                "directory",
            ),
            (
                "reversed date range",
                lambda body: body.update(
                    {
                        "start_date": "2026-08-24",
                        "end_date": "2026-08-23",
                    }
                ),
                "INVALID_CSV_EXPORT_OPTIONS",
                422,
                "date_range",
            ),
        )

        # 각 실패는 user value를 반사하지 않는 code, status와 optional field만 노출한다.
        for label, mutate, expected_code, expected_status, expected_field in invalid_cases:
            with self.subTest(label=label):
                request_body = _create_valid_request_body()
                mutate(request_body)

                with self.assertRaises(TransportContractError) as raised:
                    parse_csv_export_options(request_body)

                self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(raised.exception.status, expected_status)
                if expected_field is not None:
                    self.assertEqual(
                        raised.exception.details.get("field"),
                        expected_field,
                    )

    def test_mapper_returns_only_absolute_path_and_positive_row_count(self) -> None:
        """
        함수 이름: test_mapper_returns_only_absolute_path_and_positive_row_count()
        기능: canonical CSVExportResult를 exact 두 key receipt로 변환하고 동형 객체를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        result = CSVExportResult(
            file_path="/Users/oscar/Exports/phase11-trades.csv",
            exported_row_count=17,
        )

        self.assertEqual(
            map_csv_export_result(result),
            {
                "file_path": "/Users/oscar/Exports/phase11-trades.csv",
                "exported_row_count": 17,
            },
        )
        with self.assertRaises(TypeError):
            map_csv_export_result(
                SimpleNamespace(
                    file_path="/private/forged.csv",
                    exported_row_count=1,
                )
            )  # Canonical result 타입 외의 duck-typed receipt는 허용하지 않는다.
        with self.assertRaisesRegex(ValueError, "absolute"):
            map_csv_export_result(
                CSVExportResult(
                    file_path="relative/forged.csv",
                    exported_row_count=1,
                )
            )  # 잘못 조립된 writer의 상대 경로도 transport 성공 envelope로 반사하지 않는다.


class CsvExportRouteTests(unittest.TestCase):
    """
    클래스 이름: CsvExportRouteTests
    기능: CSV route의 성공, readiness, validation과 allowlisted·unknown 실패 응답을 검증한다.
    작성 날짜: 2026/08/23
    """

    def test_returns_created_receipt_and_calls_controller_outside_global_lock(
        self,
    ) -> None:
        """
        함수 이름: test_returns_created_receipt_and_calls_controller_outside_global_lock()
        기능: valid option이 Controller에 전달되고 actual receipt가 201로 반환되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        result = CSVExportResult(
            file_path="/Users/oscar/Exports/phase11-trades.csv",
            exported_row_count=17,
        )
        context, controller = _create_route_context(result=result)

        response = create_csv_export(
            TEST_REQUEST_ID,
            context,
            _create_valid_request_body(),
            TEST_COMMAND_ID,
        )

        self.assertEqual(response.status, 201)
        self.assertTrue(response.payload["ok"])
        self.assertEqual(
            response.payload["data"],
            {
                "file_path": "/Users/oscar/Exports/phase11-trades.csv",
                "exported_row_count": 17,
            },
        )
        self.assertEqual(len(controller.calls), 1)
        self.assertIs(controller.calls[0].period, CSVPeriod.CUSTOM)
        self.assertEqual(controller.calls[0].start_date, date(2026, 8, 17))
        self.assertEqual(controller.calls[0].end_date, date(2026, 8, 23))

    def test_returns_not_ready_before_calling_controller(self) -> None:
        """
        함수 이름: test_returns_not_ready_before_calling_controller()
        기능: startup 미완료 runtime을 retryable BACKEND_NOT_READY로 닫고 Controller를 호출하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        context, controller = _create_route_context(ready=False)

        response = create_csv_export(
            TEST_REQUEST_ID,
            context,
            _create_valid_request_body(),
            TEST_COMMAND_ID,
        )

        self.assertEqual(response.status, 503)
        self.assertEqual(response.payload["error"]["code"], "BACKEND_NOT_READY")
        self.assertTrue(response.payload["error"]["retryable"])
        self.assertEqual(controller.calls, [])

    def test_returns_validation_failure_before_calling_controller(self) -> None:
        """
        함수 이름: test_returns_validation_failure_before_calling_controller()
        기능: traversal filename을 typed 422로 거부하고 Controller와 filesystem에 진입하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        request_body = _create_valid_request_body()
        request_body["file_name"] = "../secret.csv"
        context, controller = _create_route_context()

        response = create_csv_export(
            TEST_REQUEST_ID,
            context,
            request_body,
            TEST_COMMAND_ID,
        )

        self.assertEqual(response.status, 422)
        self.assertEqual(
            response.payload["error"]["code"],
            "INVALID_CSV_EXPORT_OPTIONS",
        )
        self.assertEqual(response.payload["error"]["details"], {"field": "file_name"})
        self.assertEqual(controller.calls, [])

    def test_maps_no_trades_destination_exists_and_io_failures(self) -> None:
        """
        함수 이름: test_maps_no_trades_destination_exists_and_io_failures()
        기능: ADR-004 empty, no-overwrite와 filesystem 오류를 각 typed HTTP 계약으로 변환한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        error_cases = (
            (NoTradesToExportError(), "NO_TRADES_TO_EXPORT", 422, False),
            (DestinationExistsError(), "DESTINATION_EXISTS", 409, False),
            (CSVExportIOError("write"), "CSV_EXPORT_IO_FAILED", 503, True),
        )

        # 각 allowlisted domain 실패는 안정적 code, status와 retryability로만 표현한다.
        for error, expected_code, expected_status, expected_retryable in error_cases:
            with self.subTest(code=expected_code):
                context, controller = _create_route_context(error=error)

                response = create_csv_export(
                    TEST_REQUEST_ID,
                    context,
                    _create_valid_request_body(),
                    TEST_COMMAND_ID,
                )

                self.assertEqual(response.status, expected_status)
                self.assertEqual(response.payload["error"]["code"], expected_code)
                self.assertEqual(
                    response.payload["error"]["retryable"],
                    expected_retryable,
                )
                self.assertEqual(len(controller.calls), 1)

    def test_redacts_unknown_failure_as_retryable_io_error(self) -> None:
        """
        함수 이름: test_redacts_unknown_failure_as_retryable_io_error()
        기능: 예상 밖 Controller 예외의 경로·secret 원문을 숨기고 안전한 503으로 닫는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        secret_text = "/Users/oscar/private.csv api_key=top-secret"
        context, controller = _create_route_context(
            error=RuntimeError(secret_text),
        )

        response = create_csv_export(
            TEST_REQUEST_ID,
            context,
            _create_valid_request_body(),
            TEST_COMMAND_ID,
        )

        response_text = str(response.payload)
        self.assertEqual(response.status, 503)
        self.assertEqual(
            response.payload["error"]["code"],
            "CSV_EXPORT_IO_FAILED",
        )
        self.assertEqual(
            response.payload["error"]["message"],
            "The CSV file could not be written safely.",
        )
        self.assertTrue(response.payload["error"]["retryable"])
        self.assertNotIn("top-secret", response_text)
        self.assertNotIn("private.csv", response_text)
        self.assertEqual(len(controller.calls), 1)


if __name__ == "__main__":
    unittest.main()
