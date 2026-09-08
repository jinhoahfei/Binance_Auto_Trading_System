"""CSVFileGateway의 golden bytes, streaming과 filesystem fault 정책을 검증한다."""

import base64
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from binance_auto_trader.adapters.filesystem import CSVFileGateway
from binance_auto_trader.adapters.filesystem.csv_file_gateway import (
    _atomic_rename_without_replace,
)
from binance_auto_trader.domain.history import (
    CSVExportIOError,
    CSVExportOptions,
    CSVExportValidationError,
    CSVPeriod,
    DestinationExistsError,
    NoTradesToExportError,
    Trade,
)
from binance_auto_trader.domain.trading.states import OrderSide

from tests.unit.history.factories import make_trade


# Golden bytes와 private fault seam은 production module 이름 하나를 기준으로 참조한다.
GATEWAY_MODULE = "binance_auto_trader.adapters.filesystem.csv_file_gateway"
GOLDEN_FIXTURE_PATH = (
    Path(__file__).parents[2] / "fixtures" / "csv" / "trades_v2_golden.base64"
)


def make_golden_trades() -> tuple[Trade, Trade]:
    """
    함수 이름: make_golden_trades()
    기능: Korean·comma·quote·newline과 mixed JSONL version을 포함한 두 golden Trade를 만든다.
    인자: 없음
    반환값: KST 자정 경계 앞뒤의 v1 BUY와 v2 SELL Trade tuple
    작성 날짜: 2026/08/23
    """
    legacy_trade = make_trade(
        schema_version=1,
        trade_id="legacy-placeholder",
        order_id="101",
        executed_at=datetime(
            2026,
            8,
            20,
            14,
            59,
            59,
            123456,
            tzinfo=timezone.utc,
        ),
        executed_quantity=Decimal("2E+0"),
        executed_amount=Decimal("2E+2"),
        average_fill_price=Decimal("1E+2"),
        fee_amount=Decimal("2E-3"),
        fee_quote_amount=Decimal("2E-1"),
    )
    legacy_trade = replace(
        legacy_trade,
        trade_id='거래,"하나"\n다음',
        client_order_id='client,"매수"\nline',
    )
    current_trade = make_trade(
        schema_version=2,
        trade_id="sell",
        order_id="102",
        executed_at=datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc),
        side=OrderSide.SELL,
    )

    return legacy_trade, current_trade  # 파일 순서와 KST 날짜 경계를 그대로 보존한다.


class CloseTrackingTradeIterator(Iterator[Trade]):
    """
    클래스 이름: CloseTrackingTradeIterator
    기능: gateway가 Trade를 소비한 수와 optional close 호출 여부를 기록한다.
    작성 날짜: 2026/08/23
    """

    def __init__(self, trades: tuple[Trade, ...]) -> None:
        """
        함수 이름: __init__()
        기능: 순차 소비할 Trade tuple과 초기 open 상태를 보존한다.
        인자: trades -> iterator가 순서대로 반환할 Trade tuple
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        self._trades = trades
        self._index = 0
        self.closed = False

    def __iter__(self) -> "CloseTrackingTradeIterator":
        """
        함수 이름: __iter__()
        기능: 동일한 close 추적 iterator 자신을 반환한다.
        인자: 없음
        반환값: self
        작성 날짜: 2026/08/23
        """
        return self

    def __next__(self) -> Trade:
        """
        함수 이름: __next__()
        기능: 다음 Trade를 반환하고 tuple 끝에서는 iteration을 종료한다.
        인자: 없음
        반환값: 현재 index의 Trade
        작성 날짜: 2026/08/23
        """
        if self._index >= len(self._trades):
            raise StopIteration

        trade = self._trades[self._index]
        self._index += 1
        return trade  # 소비 수를 먼저 증가시켜 조기 실패 시점도 test가 관찰하게 한다.

    def close(self) -> None:
        """
        함수 이름: close()
        기능: repository generator와 같은 optional close 호출을 기록한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        self.closed = True  # 실제 descriptor 대신 deterministic close 요청 사실을 기록한다.


class LifetimeTrackedText(str):
    """
    클래스 이름: LifetimeTrackedText
    기능: 대용량 stream에서 동시에 살아 있는 Trade 식별자 수를 CPython 수명으로 추적한다.
    작성 날짜: 2026/08/24
    """

    active_instances = 0
    maximum_active_instances = 0

    def __new__(cls, value: str) -> "LifetimeTrackedText":
        """
        함수 이름: __new__()
        기능: 문자열 instance를 만들며 현재 및 최대 생존 수를 증가시킨다.
        인자: value -> Trade ID에 사용할 문자열
        반환값: 생존 수 추적이 가능한 str subclass instance
        작성 날짜: 2026/08/24
        """
        instance = super().__new__(cls, value)
        cls.active_instances += 1
        cls.maximum_active_instances = max(
            cls.maximum_active_instances,
            cls.active_instances,
        )
        return instance  # Trade의 문자열 validator를 그대로 통과하는 추적 ID다.

    def __del__(self) -> None:
        """
        함수 이름: __del__()
        기능: Trade와 함께 식별자가 해제될 때 현재 생존 수를 감소시킨다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        type(self).active_instances -= 1  # 다음 row 요청 전 해제 여부를 test가 확인한다.


class LargeTradeIterator(Iterator[Trade]):
    """
    클래스 이름: LargeTradeIterator
    기능: Trade 전체 collection 없이 요청된 순간에만 다음 추적 Trade 하나를 생성한다.
    작성 날짜: 2026/08/24
    """

    def __init__(self, template: Trade, trade_count: int) -> None:
        """
        함수 이름: __init__()
        기능: 반복 생성에 사용할 Trade template과 총 row 수를 보존한다.
        인자: template -> 금융 필드를 재사용할 검증 완료 Trade
            trade_count -> 생성할 양수 Trade 수
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        self._template = template
        self._trade_count = trade_count
        self.produced_count = 0
        self.closed = False

    def __iter__(self) -> "LargeTradeIterator":
        """
        함수 이름: __iter__()
        기능: materialization 없는 동일 iterator를 반환한다.
        인자: 없음
        반환값: self
        작성 날짜: 2026/08/24
        """
        return self

    def __next__(self) -> Trade:
        """
        함수 이름: __next__()
        기능: 직전 Trade를 보존하지 않고 다음 identity를 가진 Trade 하나만 생성한다.
        인자: 없음
        반환값: 현재 순번의 새 Trade
        작성 날짜: 2026/08/24
        """
        if self.produced_count >= self._trade_count:
            raise StopIteration

        row_number = self.produced_count + 1
        self.produced_count = row_number
        return replace(
            self._template,
            trade_id=LifetimeTrackedText(f"large-trade-{row_number}"),
            order_id=str(10_000 + row_number),
            client_order_id=f"large-client-{row_number}",
        )  # 반환 뒤 iterator 자신은 생성한 Trade reference를 보존하지 않는다.

    def close(self) -> None:
        """
        함수 이름: close()
        기능: 대용량 export 완료 뒤 gateway의 deterministic close 요청을 기록한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        self.closed = True  # 전체 row 소비 여부와 별개로 resource close 요청을 표시한다.


class CSVFileGatewayTests(unittest.TestCase):
    """
    클래스 이름: CSVFileGatewayTests
    기능: CSV byte 계약, no-replace commit, cleanup과 streaming 소비를 검증한다.
    작성 날짜: 2026/08/23
    """

    def setUp(self) -> None:
        """
        함수 이름: setUp()
        기능: 각 테스트 전용 absolute temporary directory와 CSV option을 생성한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        self.temporary_directory = TemporaryDirectory()
        self.output_directory = Path(self.temporary_directory.name)
        self.options = CSVExportOptions(
            save_location=str(self.output_directory),
            period=CSVPeriod.CUSTOM,
            start_date=datetime(2026, 8, 20).date(),
            end_date=datetime(2026, 8, 21).date(),
            file_name="trades_golden.csv",
        )
        self.gateway = CSVFileGateway()
        self.legacy_trade, self.current_trade = make_golden_trades()

    def tearDown(self) -> None:
        """
        함수 이름: tearDown()
        기능: fault test가 남긴 파일까지 포함해 temporary directory를 제거한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        self.temporary_directory.cleanup()

    def _temporary_paths(self) -> tuple[Path, ...]:
        """
        함수 이름: _temporary_paths()
        기능: 현재 output directory에 남은 gateway temporary 파일을 찾는다.
        인자: 없음
        반환값: 이름이 dot/tmp 형식인 Path tuple
        작성 날짜: 2026/08/23
        """
        return tuple(
            path
            for path in self.output_directory.iterdir()
            if path.name.startswith(".") and path.name.endswith(".tmp")
        )

    def test_writes_byte_exact_golden_with_bom_crlf_rfc4180_and_mixed_versions(
        self,
    ) -> None:
        """
        함수 이름: test_writes_byte_exact_golden_with_bom_crlf_rfc4180_and_mixed_versions()
        기능: 한국어·특수문자·KST·plain Decimal을 포함한 실제 bytes가 golden과 같은지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        result = self.gateway.write_csv(
            iter((self.legacy_trade, self.current_trade)),
            self.options,
        )
        expected_bytes = base64.b64decode(
            GOLDEN_FIXTURE_PATH.read_text(encoding="ascii").strip(),
            validate=True,
        )
        destination_path = Path(result.file_path)

        self.assertTrue(destination_path.is_absolute())
        self.assertEqual(
            destination_path,
            self.output_directory.resolve() / "trades_golden.csv",
        )
        self.assertEqual(result.exported_row_count, 2)
        self.assertEqual(destination_path.read_bytes(), expected_bytes)
        self.assertTrue(expected_bytes.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\r\r\n", expected_bytes)
        self.assertEqual(self._temporary_paths(), ())

        # v1/v2 Trade 모두 첫 field가 원본 회계 version이 아닌 CSV schema literal 2이며 원본 version은 추가 column에 보존한다.
        decoded_text = expected_bytes.decode("utf-8-sig")
        self.assertIn("\r\n2,", decoded_text)
        self.assertIn("\r\n2,sell", decoded_text)

    def test_empty_stream_fails_before_any_filesystem_entry_and_closes_iterator(
        self,
    ) -> None:
        """
        함수 이름: test_empty_stream_fails_before_any_filesystem_entry_and_closes_iterator()
        기능: 빈 stream이 파일·temporary를 만들지 않고 typed 실패한 뒤 iterator를 닫는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        trade_iterator = CloseTrackingTradeIterator(())

        with self.assertRaises(NoTradesToExportError) as captured_error:
            self.gateway.write_csv(trade_iterator, self.options)

        self.assertEqual(captured_error.exception.code, "NO_TRADES_TO_EXPORT")
        self.assertTrue(trade_iterator.closed)
        self.assertEqual(tuple(self.output_directory.iterdir()), ())

    def test_existing_destination_is_never_overwritten_and_iterator_is_closed(
        self,
    ) -> None:
        """
        함수 이름: test_existing_destination_is_never_overwritten_and_iterator_is_closed()
        기능: 기존 destination bytes를 유지하고 temporary 없이 typed 충돌하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        destination_path = self.output_directory / self.options.file_name
        destination_path.write_bytes(b"existing-content")
        trade_iterator = CloseTrackingTradeIterator(
            (self.legacy_trade, self.current_trade)
        )

        with self.assertRaises(DestinationExistsError) as captured_error:
            self.gateway.write_csv(trade_iterator, self.options)

        self.assertEqual(captured_error.exception.code, "DESTINATION_EXISTS")
        self.assertEqual(destination_path.read_bytes(), b"existing-content")
        self.assertEqual(trade_iterator._index, 1)
        self.assertTrue(trade_iterator.closed)
        self.assertEqual(self._temporary_paths(), ())

    def test_relative_missing_and_file_save_locations_are_validation_failures(
        self,
    ) -> None:
        """
        함수 이름: test_relative_missing_and_file_save_locations_are_validation_failures()
        기능: 수정 가능한 상대·미존재·일반 파일 저장 위치를 I/O가 아닌 validation으로 구분한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        ordinary_file = self.output_directory / "ordinary-file"
        ordinary_file.write_text("not a directory", encoding="utf-8")
        invalid_locations = (
            "relative/export",
            str(self.output_directory / "missing"),
            str(ordinary_file),
        )

        for invalid_location in invalid_locations:
            with self.subTest(save_location=invalid_location):
                invalid_options = replace(
                    self.options,
                    save_location=invalid_location,
                )
                trade_iterator = CloseTrackingTradeIterator((self.legacy_trade,))
                with self.assertRaises(CSVExportValidationError) as captured_error:
                    self.gateway.write_csv(trade_iterator, invalid_options)
                self.assertEqual(captured_error.exception.field, "save_location")
                self.assertTrue(trade_iterator.closed)
                self.assertEqual(self._temporary_paths(), ())

    def test_permission_write_fsync_and_commit_faults_are_redacted_and_cleaned(
        self,
    ) -> None:
        """
        함수 이름: test_permission_write_fsync_and_commit_faults_are_redacted_and_cleaned()
        기능: 네 filesystem fault를 typed I/O로 감싸고 path와 temporary를 남기지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        fault_cases = (
            (
                "permission",
                f"{GATEWAY_MODULE}.Path.open",
                PermissionError(f"denied {self.output_directory}"),
                "write",
            ),
            (
                "write",
                f"{GATEWAY_MODULE}._trade_to_csv_row",
                OSError(f"disk full {self.output_directory}"),
                "write",
            ),
            (
                "fsync",
                f"{GATEWAY_MODULE}.os.fsync",
                OSError(f"sync failed {self.output_directory}"),
                "fsync",
            ),
            (
                "commit",
                f"{GATEWAY_MODULE}._atomic_rename_without_replace",
                OSError(f"rename failed {self.output_directory}"),
                "commit",
            ),
        )

        for case_name, patch_target, fault, expected_operation in fault_cases:
            with self.subTest(case_name=case_name):
                trade_iterator = CloseTrackingTradeIterator((self.legacy_trade,))
                with patch(patch_target, side_effect=fault):
                    with self.assertRaises(CSVExportIOError) as captured_error:
                        self.gateway.write_csv(trade_iterator, self.options)

                self.assertEqual(captured_error.exception.code, "CSV_EXPORT_IO_FAILED")
                self.assertEqual(captured_error.exception.operation, expected_operation)
                self.assertNotIn(str(self.output_directory), str(captured_error.exception))
                self.assertTrue(trade_iterator.closed)
                self.assertFalse((self.output_directory / self.options.file_name).exists())
                self.assertEqual(self._temporary_paths(), ())

    def test_commit_race_maps_to_destination_exists_without_overwrite(self) -> None:
        """
        함수 이름: test_commit_race_maps_to_destination_exists_without_overwrite()
        기능: 사전 확인 뒤 경쟁자가 만든 destination도 exclusive rename이 덮어쓰지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        destination_path = self.output_directory / self.options.file_name
        competitor_bytes = b"competitor-won-the-race"

        def create_competing_destination(
            temporary_path: Path,
            final_path: Path,
        ) -> None:
            """
            함수 이름: create_competing_destination()
            기능: commit 순간 다른 writer가 final 이름을 선점한 race를 재현한다.
                인자: temporary_path -> gateway가 rename하려는 temporary이며 사용하지 않음
                final_path -> 경쟁자가 먼저 생성할 destination Path
            반환값: 정상 반환 없이 FileExistsError 발생
            작성 날짜: 2026/08/23
            """
            del temporary_path  # 경쟁 writer는 gateway temporary 내용을 읽지 않는다.
            final_path.write_bytes(competitor_bytes)
            raise FileExistsError("destination was created concurrently")

        with patch(
            f"{GATEWAY_MODULE}._atomic_rename_without_replace",
            side_effect=create_competing_destination,
        ):
            with self.assertRaises(DestinationExistsError):
                self.gateway.write_csv((self.legacy_trade,), self.options)

        self.assertEqual(destination_path.read_bytes(), competitor_bytes)
        self.assertEqual(self._temporary_paths(), ())

    def test_native_atomic_rename_never_replaces_existing_destination(self) -> None:
        """
        함수 이름: test_native_atomic_rename_never_replaces_existing_destination()
        기능: 현재 OS의 실제 exclusive rename primitive가 기존 destination을 보존하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        temporary_path = self.output_directory / ".native-rename.tmp"
        destination_path = self.output_directory / "native-rename.csv"
        temporary_path.write_bytes(b"new-content")
        destination_path.write_bytes(b"existing-content")

        with self.assertRaises(FileExistsError):
            _atomic_rename_without_replace(temporary_path, destination_path)

        self.assertEqual(destination_path.read_bytes(), b"existing-content")
        self.assertEqual(temporary_path.read_bytes(), b"new-content")

    def test_cleanup_failure_is_best_effort_and_does_not_mask_commit_error(
        self,
    ) -> None:
        """
        함수 이름: test_cleanup_failure_is_best_effort_and_does_not_mask_commit_error()
        기능: temporary unlink 장애가 원래 commit typed 실패를 바꾸지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        with patch(
            f"{GATEWAY_MODULE}._atomic_rename_without_replace",
            side_effect=OSError("commit failed"),
        ), patch.object(
            Path,
            "unlink",
            side_effect=PermissionError("cleanup denied"),
        ):
            with self.assertRaises(CSVExportIOError) as captured_error:
                self.gateway.write_csv((self.legacy_trade,), self.options)

        self.assertEqual(captured_error.exception.operation, "commit")
        self.assertFalse((self.output_directory / self.options.file_name).exists())
        self.assertEqual(len(self._temporary_paths()), 1)

    def test_generator_is_consumed_lazily_after_empty_probe_and_temp_creation(
        self,
    ) -> None:
        """
        함수 이름: test_generator_is_consumed_lazily_after_empty_probe_and_temp_creation()
        기능: 첫 next가 파일 생성 전이고 나머지 Trade는 temporary write 중 순차 소비되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        observed_entries: list[tuple[str, ...]] = []

        def stream_trades() -> Iterator[Trade]:
            """
            함수 이름: stream_trades()
            기능: 각 yield 전후 output directory entry를 기록하는 bounded-memory generator다.
            인자: 없음
            반환값: golden Trade 두 개를 순서대로 제공하는 iterator
            작성 날짜: 2026/08/23
            """
            observed_entries.append(
                tuple(path.name for path in self.output_directory.iterdir())
            )
            yield self.legacy_trade
            observed_entries.append(
                tuple(path.name for path in self.output_directory.iterdir())
            )
            yield self.current_trade

        result = self.gateway.write_csv(stream_trades(), self.options)

        self.assertEqual(observed_entries[0], ())
        self.assertEqual(len(observed_entries[1]), 1)
        self.assertTrue(observed_entries[1][0].startswith(".trades_golden.csv."))
        self.assertTrue(observed_entries[1][0].endswith(".tmp"))
        self.assertEqual(result.exported_row_count, 2)
        self.assertEqual(self._temporary_paths(), ())

    def test_large_stream_keeps_one_generated_trade_alive_and_counts_every_row(
        self,
    ) -> None:
        """
        함수 이름: test_large_stream_keeps_one_generated_trade_alive_and_counts_every_row()
        기능: 5천 행을 collection 없이 처리하며 한 시점에 생성 Trade 하나만 보존하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        trade_count = 5_000
        LifetimeTrackedText.active_instances = 0
        LifetimeTrackedText.maximum_active_instances = 0
        trade_iterator = LargeTradeIterator(self.legacy_trade, trade_count)
        large_options = replace(self.options, file_name="large.csv")

        result = self.gateway.write_csv(trade_iterator, large_options)

        self.assertEqual(result.exported_row_count, trade_count)
        self.assertEqual(trade_iterator.produced_count, trade_count)
        self.assertTrue(trade_iterator.closed)
        self.assertEqual(LifetimeTrackedText.maximum_active_instances, 1)
        self.assertEqual(LifetimeTrackedText.active_instances, 0)
        self.assertTrue((self.output_directory / "large.csv").is_file())
        self.assertEqual(self._temporary_paths(), ())

    def test_invalid_directory_is_rejected_even_when_trade_stream_is_empty(
        self,
    ) -> None:
        """
        함수 이름: test_invalid_directory_is_rejected_even_when_trade_stream_is_empty()
        기능: 빈 조회도 backend의 read-only directory 검증을 우회하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        missing_directory = self.output_directory / "missing"
        invalid_options = replace(
            self.options,
            save_location=str(missing_directory),
        )
        trade_iterator = CloseTrackingTradeIterator(())

        with self.assertRaises(CSVExportValidationError) as captured_error:
            self.gateway.write_csv(trade_iterator, invalid_options)

        self.assertEqual(captured_error.exception.field, "save_location")
        self.assertTrue(trade_iterator.closed)
        self.assertFalse(missing_directory.exists())
        self.assertEqual(tuple(self.output_directory.iterdir()), ())


if __name__ == "__main__":
    unittest.main()
