"""CSVExportOptions의 backend validation과 KST preset 해석을 검증한다."""

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone
import unittest

from binance_auto_trader.domain.history import (
    CSVExportError,
    CSVExportIOError,
    CSVExportOptions,
    CSVExportResult,
    CSVExportValidationError,
    CSVPeriod,
    DestinationExistsError,
    NoTradesToExportError,
)


# 모든 테스트는 UI 값 보정 없이 backend 생성자가 받은 값을 직접 검증하도록 구성한다.
TEST_DATE = date(2026, 8, 23)


def make_csv_options(
    *,
    save_location: object = "/tmp/export",
    period: object = CSVPeriod.TODAY,
    start_date: object = TEST_DATE,
    end_date: object = TEST_DATE,
    file_name: object = "trades.csv",
) -> CSVExportOptions:
    """
    함수 이름: make_csv_options()
    기능: 개별 validation field를 교체할 수 있는 CSV option test 값을 생성한다.
    인자: save_location -> 저장 위치 override
        period -> CSV 기간 override
        start_date -> 시작 LocalDate override
        end_date -> 종료 LocalDate override
        file_name -> 파일명 override
    반환값: 검증을 통과한 CSVExportOptions 또는 생성 중 typed validation 오류
    작성 날짜: 2026/08/23
    """
    return CSVExportOptions(
        save_location=save_location,  # type: ignore[arg-type]
        period=period,  # type: ignore[arg-type]
        start_date=start_date,  # type: ignore[arg-type]
        end_date=end_date,  # type: ignore[arg-type]
        file_name=file_name,  # type: ignore[arg-type]
    )


class CSVExportOptionsTests(unittest.TestCase):
    """
    클래스 이름: CSVExportOptionsTests
    기능: CSV option의 enum, 경로·파일명·날짜 validation과 정규화를 검증한다.
    작성 날짜: 2026/08/23
    """

    def test_period_members_and_case_insensitive_extension_are_exact(self) -> None:
        """
        함수 이름: test_period_members_and_case_insensitive_extension_are_exact()
        기능: 네 canonical period와 대소문자 무관 단일 .csv 확장자 정규화를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        self.assertEqual(
            tuple(CSVPeriod),
            (
                CSVPeriod.TODAY,
                CSVPeriod.WEEKLY,
                CSVPeriod.MONTHLY,
                CSVPeriod.CUSTOM,
            ),
        )
        self.assertEqual(make_csv_options(file_name="trades").file_name, "trades.csv")
        self.assertEqual(
            make_csv_options(file_name="trades.CSV").file_name,
            "trades.CSV",
        )
        self.assertEqual(
            make_csv_options(file_name="trades.csv").file_name,
            "trades.csv",
        )

    def test_presets_resolve_inclusive_dates_from_current_kst_date(self) -> None:
        """
        함수 이름: test_presets_resolve_inclusive_dates_from_current_kst_date()
        기능: TODAY/WEEKLY/MONTHLY가 현재 KST 날짜에서 0/6/29일을 되돌아가는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        expected_ranges = {
            CSVPeriod.TODAY: (date(2026, 8, 23), date(2026, 8, 23)),
            CSVPeriod.WEEKLY: (date(2026, 8, 17), date(2026, 8, 23)),
            CSVPeriod.MONTHLY: (date(2026, 7, 25), date(2026, 8, 23)),
        }

        # preset은 UI가 보낸 기존 범위가 아니라 호출 시점의 KST today를 authoritative하게 쓴다.
        for period, expected_range in expected_ranges.items():
            with self.subTest(period=period):
                options = make_csv_options(
                    period=period,
                    start_date=date(2020, 1, 1),
                    end_date=date(2020, 1, 2),
                )
                self.assertEqual(options.resolve_dates(TEST_DATE), expected_range)

    def test_custom_period_keeps_exact_inclusive_dates(self) -> None:
        """
        함수 이름: test_custom_period_keeps_exact_inclusive_dates()
        기능: CUSTOM이 현재 날짜와 무관하게 선택한 시작일·종료일을 그대로 유지하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        options = make_csv_options(
            period=CSVPeriod.CUSTOM,
            start_date=date(2024, 2, 29),
            end_date=date(2026, 8, 1),
        )

        self.assertEqual(
            options.resolve_dates(TEST_DATE),
            (date(2024, 2, 29), date(2026, 8, 1)),
        )

    def test_save_location_rejects_non_string_empty_whitespace_and_control(
        self,
    ) -> None:
        """
        함수 이름: test_save_location_rejects_non_string_empty_whitespace_and_control()
        기능: 저장 위치의 구조 오류를 field가 있는 typed validation으로 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        invalid_locations = (None, 7, "", " ", " /tmp/export", "/tmp/export\n")

        for invalid_location in invalid_locations:
            with self.subTest(save_location=repr(invalid_location)):
                with self.assertRaises(CSVExportValidationError) as captured_error:
                    make_csv_options(save_location=invalid_location)
                self.assertEqual(captured_error.exception.field, "save_location")
                self.assertEqual(
                    captured_error.exception.code,
                    "INVALID_CSV_EXPORT_OPTIONS",
                )

    def test_file_name_rejects_empty_outer_whitespace_and_path_attacks(
        self,
    ) -> None:
        """
        함수 이름: test_file_name_rejects_empty_outer_whitespace_and_path_attacks()
        기능: 빈 이름, separator, traversal, forbidden/control 문자를 모두 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        invalid_file_names = (
            None,
            7,
            "",
            " ",
            " trades.csv",
            "trades.csv ",
            "../trades.csv",
            r"..\trades.csv",
            "trades..csv",
            "trades/name.csv",
            r"trades\name.csv",
            "trades<name.csv",
            "trades>name.csv",
            "trades:name.csv",
            'trades"name.csv',
            "trades|name.csv",
            "trades?name.csv",
            "trades*name.csv",
            "trades\u0000name.csv",
            "trades\nname.csv",
            ".csv",
            "trades.",
        )

        for invalid_file_name in invalid_file_names:
            with self.subTest(file_name=repr(invalid_file_name)):
                with self.assertRaises(CSVExportValidationError) as captured_error:
                    make_csv_options(file_name=invalid_file_name)
                self.assertEqual(captured_error.exception.field, "file_name")

    def test_file_name_rejects_windows_reserved_basename_case_insensitively(
        self,
    ) -> None:
        """
        함수 이름: test_file_name_rejects_windows_reserved_basename_case_insensitively()
        기능: Windows device basename을 확장자와 대소문자에 관계없이 차단하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        reserved_names = (
            "CON",
            "con.csv",
            "PrN.report",
            "aux.CSV",
            "NUL.txt.csv",
            "com1",
            "COM9.report.csv",
            "CON .csv",
            "lpt1.CsV",
            "lpt1 .report.csv",
            "LPT9.output",
        )

        for reserved_name in reserved_names:
            with self.subTest(file_name=reserved_name):
                with self.assertRaises(CSVExportValidationError) as captured_error:
                    make_csv_options(file_name=reserved_name)
                self.assertEqual(
                    captured_error.exception.reason,
                    "must_not_use_a_windows_reserved_name",
                )

    def test_period_date_types_order_and_current_date_are_strict(self) -> None:
        """
        함수 이름: test_period_date_types_order_and_current_date_are_strict()
        기능: enum 대체 문자열, datetime, 문자열 날짜와 역전 범위를 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        invalid_fields = (
            ("period", "TODAY"),
            ("start_date", "2026-08-23"),
            ("end_date", datetime(2026, 8, 23, tzinfo=timezone.utc)),
        )
        for field_name, invalid_value in invalid_fields:
            with self.subTest(field_name=field_name):
                with self.assertRaises(CSVExportValidationError) as captured_error:
                    make_csv_options(**{field_name: invalid_value})
                self.assertEqual(captured_error.exception.field, field_name)

        with self.assertRaises(CSVExportValidationError) as captured_range_error:
            make_csv_options(
                start_date=date(2026, 8, 24),
                end_date=date(2026, 8, 23),
            )
        self.assertEqual(captured_range_error.exception.field, "date_range")

        options = make_csv_options()
        with self.assertRaises(CSVExportValidationError) as captured_today_error:
            options.resolve_dates(datetime(2026, 8, 23, tzinfo=timezone.utc))
        self.assertEqual(captured_today_error.exception.field, "current_kst_date")

    def test_options_and_success_result_are_immutable_and_result_count_is_positive(
        self,
    ) -> None:
        """
        함수 이름: test_options_and_success_result_are_immutable_and_result_count_is_positive()
        기능: option/result 불변성과 성공 결과의 비어 있지 않은 경로·양수 count를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        options = make_csv_options()
        result = CSVExportResult(
            file_path="/tmp/export/trades.csv",
            exported_row_count=1,
        )

        with self.assertRaises(FrozenInstanceError):
            options.file_name = "changed.csv"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            result.exported_row_count = 2  # type: ignore[misc]
        for invalid_count in (0, -1, True, "1"):
            with self.subTest(exported_row_count=invalid_count):
                with self.assertRaises((TypeError, ValueError)):
                    CSVExportResult(
                        file_path="/tmp/export/trades.csv",
                        exported_row_count=invalid_count,  # type: ignore[arg-type]
                    )

    def test_export_failures_share_stable_typed_hierarchy_and_codes(self) -> None:
        """
        함수 이름: test_export_failures_share_stable_typed_hierarchy_and_codes()
        기능: route가 분기할 네 CSV 실패가 공통 base와 안정된 code를 갖는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        errors = (
            CSVExportValidationError("file_name", "must_not_be_empty"),
            NoTradesToExportError(),
            DestinationExistsError(),
            CSVExportIOError("write"),
        )
        expected_codes = (
            "INVALID_CSV_EXPORT_OPTIONS",
            "NO_TRADES_TO_EXPORT",
            "DESTINATION_EXISTS",
            "CSV_EXPORT_IO_FAILED",
        )

        self.assertTrue(all(isinstance(error, CSVExportError) for error in errors))
        self.assertEqual(tuple(error.code for error in errors), expected_codes)


if __name__ == "__main__":
    unittest.main()
